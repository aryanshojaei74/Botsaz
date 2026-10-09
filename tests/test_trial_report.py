"""PATCH_TRIAL_REPORT_CHANNEL_V1 — گزارشِ اکانتِ تست در کانالِ مالک."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from plans import Feature, has_feature
from tenant.shared import trial_report as TR


def test_plan_gating_growth_and_pro_only():
    assert has_feature("growth", Feature.TRIAL_REPORT_CHANNEL)
    assert has_feature("pro", Feature.TRIAL_REPORT_CHANNEL)
    assert not has_feature("free", Feature.TRIAL_REPORT_CHANNEL)


def test_build_report_content_and_escaping():
    t = TR.build_report(user_id=7, username="ali", full_name="A<b>", link="https://x/s?a=1&b=2",
                        volume="0.1 گیگ", duration="1 ساعت", panel="GAMING", source="panel")
    assert t.startswith("#تست_جدید")
    assert "<code>https://x/s?a=1&amp;b=2</code>" in t and "A&lt;b&gt;" in t
    assert "⏱ 1 ساعت | 📦 0.1 گیگ" in t and "🚀 پنل: GAMING" in t
    assert "id: <code>7</code>" in t and "@ali" in t
    t2 = TR.build_report(user_id=7, username=None, full_name="B", link="L", volume="v", duration="d",
                         panel=None, source="stock", show_link=False)
    assert "لینک" not in t2 and "انبار دستی" in t2 and "—" in t2


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales",
                         plan="growth", status="active"))
        s.add(models.Bot(id=6, owner_id=1, token="6:T", username="y", field="vpn_sales",
                         plan="free", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


class U:
    telegram_id = 99
    full_name = "T"
    username = None


@pytest.mark.asyncio
async def test_report_posts_only_when_channel_set_and_respects_link_toggle(db):
    b = FakeBot()
    await TR.report(b, 5, U(), link="L1", volume="1 گیگ", duration="1 روز", panel_name="P")
    assert b.sent == []                                         # کانال تنظیم نشده
    await tenant.set_setting(5, "trial_report_channel", "-1001")
    await TR.report(b, 5, U(), tg_username="zed", link="L1", volume="1 گیگ", duration="1 روز", panel_name="P")
    assert b.sent[0][0] == -1001 and "L1" in b.sent[0][1] and "@zed" in b.sent[0][1]
    await tenant.set_setting(5, "trial_report_link", "0")
    await TR.report(b, 5, U(), link="SECRET", volume="v", duration="d", panel_name="P")
    assert "SECRET" not in b.sent[1][1]


@pytest.mark.asyncio
async def test_hook_is_plan_gated_and_never_raises(db):
    await tenant.set_setting(5, "trial_report_channel", "-1001")
    await tenant.set_setting(6, "trial_report_channel", "-1002")
    b = FakeBot()
    await tenant._trial_report_post(b, 6, U(), None, "L", "v", "d", None, "stock")   # پلن رایگان → هیچ
    assert b.sent == []
    await tenant._trial_report_post(b, 5, U(), None, "L", "v", "d", None, "stock")
    assert len(b.sent) == 1

    class Boom(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("blocked")
    await tenant._trial_report_post(Boom(), 5, U(), None, "L", "v", "d", None, "stock")  # خطا قورت می‌شود


# ── مسیرِ کاملِ دکمه‌های «تست رایگان»: کیبوردی (msg.from_user=کاربر) و اینلاین (msg.from_user=ربات) ──
class _FakeClient:
    async def create_account(self, email, **kw):
        return {"uuid": "u", "sub_id": "s", "email": email, "sub_link": "https://sub.example/xyz"}

    async def close(self):
        pass


@pytest_asyncio.fixture
async def trial_env(monkeypatch, db):
    maker = db
    async with maker() as s:
        s.add(models.VpnPanel(bot_id=5, panel_key="sanai", base_url="http://p", username="u",
                              password="p", enabled=True))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=99, full_name="Tester", balance=0))
        s.add(models.MainButton(id=3, bot_id=5, row=0, col=0, label="🎁 تست", action="free_trial"))
        await s.commit()
    import tenant.bots.vpn.vpn_user as VU
    for mod in (VU,):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(tenant, "_trial_panel_client", lambda panel: _FakeClient())

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(VU, "_send_config_delivery", noop)

    async def email(*a, **k):
        return "mail1"
    monkeypatch.setattr(VU, "_make_config_email", email)
    await tenant.set_setting(5, "trial_report_channel", "-1009")
    await tenant.set_setting(5, "trial_report_link", "1")
    return maker


class _Msg:
    def __init__(self, uname, chat_id=99):
        class FU:
            username = uname
            id = chat_id
            is_bot = (uname == "TheBotName")
        self.from_user = FU()
        self.answers = []

    async def answer(self, text, **k):
        self.answers.append(text)


class _Bot(FakeBot):
    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_username", ["real_user", "TheBotName"])   # کیبوردی / اینلاین
async def test_free_trial_button_posts_report_for_keyboard_and_inline(trial_env, sender_username):
    from sqlalchemy import select
    maker = trial_env
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 99))
    cfg = {"trial_mode": "auto", "trial_gb": 1, "trial_days": 1, "trial_claim_limit": 0,
           "trial_panel_key": "sanai"}
    b = _Bot()
    await tenant.do_free_trial(_Msg(sender_username), b, 5, u, btn_cfg=cfg, mb_id=3)
    posts = [t for c, t in b.sent if c == -1009]
    assert posts, f"گزارش ارسال نشد؛ پیام‌ها: {[t for _, t in b.sent]}"
    assert "https://sub.example/xyz" in posts[0] and "id: <code>99</code>" in posts[0]
    assert "@TheBotName" not in posts[0]          # یوزرنیمِ ربات (دکمهٔ اینلاین) نباید جایِ کاربر بیاید


def test_feature_is_vpn_only_in_plan_listing():
    from plans import FEATURE_BOT_TYPES, features_by_bot_type, Plan
    assert FEATURE_BOT_TYPES[Feature.TRIAL_REPORT_CHANNEL] == frozenset({"vpn_sales"})
    assert Feature.TRIAL_REPORT_CHANNEL in features_by_bot_type(Plan.GROWTH, "vpn_sales")
    assert Feature.TRIAL_REPORT_CHANNEL not in features_by_bot_type(Plan.GROWTH, "virtual_service")
    assert Feature.TRIAL_REPORT_CHANNEL not in features_by_bot_type(Plan.PRO, "digital_shop")


@pytest.mark.asyncio
async def test_finance_menu_hides_trial_report_for_non_vpn_bots(db):
    maker = db
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="smm", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.Bot(id=8, owner_id=1, token="8:T", username="vpn", field="vpn_sales",
                         plan="pro", status="active"))
        await s.commit()
    smm = [cb for _l, cb in await tenant._sec_finance_opts(7, "pro")]
    vpn = [cb for _l, cb in await tenant._sec_finance_opts(8, "pro")]
    assert "set:trrep" not in smm and "set:trrep" in vpn
