"""PATCH_PSEL_FREE_TEST_V1 / PATCH_PSEL_UNLIMITED_DAYS_V1 / PATCH_PSEL_OWNER_FAIL_NOTICE_V1 — Whale Support."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import common as PC
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller import user as PU
from tests.test_panel_seller import FakeAPI


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, SV, tenant):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    FakeAPI.log, FakeAPI.fail_create = [], False
    async with m() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=1_000_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p", username="s", password="x", enabled=True))
        s.add(models.PanelSellerPackage(id=1, bot_id=7, panel_id=3, title="نامحدود", data_gb=100, max_users=10, days=0,
                                        price_toman=300_000, active=True))
        await s.commit()
    yield m
    await engine.dispose()


def test_days_text_and_pk_line_show_unlimited():
    assert PC.days_text(0) == "♾ نامحدود" and PC.days_text(None) == "♾ نامحدود" and "۳۰" in PC.days_text(30)
    pk = NS(title="x", data_gb=10, max_users=5, days=0, price_toman=1000)
    assert "♾ نامحدود" in PC.pk_line(pk) and "روز" not in PC.pk_line(pk)
    assert PC.exp_text(None) == "♾ نامحدود"


@pytest.mark.asyncio
async def test_zero_day_package_creates_account_without_expiry_and_renews_without_expiry(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok"
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
    assert a.expires_at is None                                       # قبلاً «۰ ⇒ ۳۰ روز» می‌شد
    r2 = await SV.renew(7, r["account_id"], 1001, api_factory=FakeAPI)
    assert r2["status"] == "ok" and r2["expires_at"] is None


@pytest.mark.asyncio
async def test_unlimited_account_is_never_suspended_for_expiry(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    out = await SV.enforce_once(7, api_factory=FakeAPI, now=datetime.utcnow() + timedelta(days=5000))
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
    assert a.status == "active" and not any("مهلت" in m for _tg, m in out)


# ─────────── تستِ رایگان ───────────
async def _enable(m, **kw):
    from tenant import set_setting
    await set_setting(7, "psel_test_enabled", "1")
    for k, v in kw.items():
        await set_setting(7, f"psel_test_{k}", str(v))
    tenant._SETTING_CACHE.clear()


@pytest.mark.asyncio
async def test_test_section_off_by_default(env):
    assert await SV.test_available(7, 1001) is False
    assert (await SV.buy_test(7, 1001, api_factory=FakeAPI))["status"] == "disabled"


@pytest.mark.asyncio
async def test_free_test_is_one_per_user_with_owner_limits_and_no_charge(env):
    await _enable(env, hours=48, gb=2, users=4)
    assert await SV.test_available(7, 1001) is True
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI)
    assert r["status"] == "ok" and (r["hours"], r["gb"], r["users"]) == (48, 2, 4)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 1001))
    assert a.title == SV.TEST_TITLE and a.data_gb == 2 and a.max_users == 4 and a.package_id is None and a.price_toman == 0
    assert 47 <= (a.expires_at - datetime.utcnow()).total_seconds() / 3600 <= 48.1 and u.balance == 1_000_000
    assert ("create", r["username"], r["password"]) in FakeAPI.log
    assert await SV.test_available(7, 1001) is False
    assert (await SV.buy_test(7, 1001, api_factory=FakeAPI))["status"] == "test_used"
    assert await SV.test_available(7, 2002) is True                    # مشتریِ دیگر می‌تواند


@pytest.mark.asyncio
async def test_deleted_test_panel_still_counts_as_used(env):
    await _enable(env)
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
        a.status = "deleted"
        await s.commit()
    assert await SV.test_available(7, 1001) is False


@pytest.mark.asyncio
async def test_test_panel_failure_returns_error_and_does_not_burn_the_test(env):
    await _enable(env)

    class Bad(FakeAPI):
        async def create_admin(self, u, p): raise RuntimeError("403 Forbidden: sudo required")
    r = await SV.buy_test(7, 1001, api_factory=Bad)
    assert r["status"] == "panel_failed" and "sudo" in r["error"]
    assert await SV.test_available(7, 1001) is True                    # حساب ساخته نشد ⇒ تست سوخت نشده


# ─────────── اعلانِ علتِ خطا به مالک ───────────
@pytest.mark.asyncio
async def test_owner_gets_exact_panel_error_throttled(monkeypatch):
    sent = []

    async def _na(bot, row, text, *a, **k): sent.append(text)
    monkeypatch.setattr(tenant, "_notify_admins", _na)
    import log_throttle
    monkeypatch.setattr(log_throttle, "should_log", lambda key, secs: not sent)     # دومی throttle می‌شود
    row = NS(id=7)
    await PU.tell_owner_failure(object(), row, 1001, "ساختِ پنل", "HTTP 403: not sudo")
    await PU.tell_owner_failure(object(), row, 1002, "ساختِ پنل", "again")
    assert len(sent) == 1 and "HTTP 403: not sudo" in sent[0] and "1001" in sent[0] and "sudo" in sent[0]


# ─────────── اعلانِ تست‌ها: ادمین + کانالِ لاگ (PATCH_PSEL_TEST_NOTIFY_V1) ───────────
def test_parse_channel_variants():
    for raw, want in [("@my_channel", "@my_channel"), ("my_channel", "@my_channel"), ("t.me/my_channel", "@my_channel"),
                      ("https://t.me/my_channel/", "@my_channel"), ("-1001234567890", "-1001234567890")]:
        assert SV.parse_channel(raw) == want, raw
    for bad in ("", "ab", "-12", "my channel", "@a", "https://example.com/x", "javascript:1"):
        assert SV.parse_channel(bad) is None, bad


class _TBot:
    def __init__(self, fail_channel=False):
        self.sent, self.fail_channel = [], fail_channel

    async def send_message(self, chat, text, **k):
        if self.fail_channel and str(chat).startswith(("@", "-100")):
            raise RuntimeError("Forbidden: bot is not a member of the channel chat")
        self.sent.append((chat, text))


@pytest.mark.asyncio
async def test_test_taken_notifies_admins_and_channel_with_who_and_how_much(env, monkeypatch):
    await _enable(env, hours=24, gb=10, users=30)
    from tenant import set_setting
    await set_setting(7, "psel_test_notify", "1")
    await set_setting(7, "psel_test_log_channel", "@whale_log")
    tenant._SETTING_CACHE.clear()
    admin_msgs, alerts = [], []

    async def na(bot, row, text, *a, **k): admin_msgs.append(text)

    async def alert(*a, **k): alerts.append(1)
    monkeypatch.setattr(tenant, "_notify_admins", na)
    monkeypatch.setattr(tenant, "_purchase_alert", alert)
    bot, row = _TBot(), NS(id=7, owner_id=500)
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI, bot=bot, row=row)
    assert r["status"] == "ok"
    assert len(admin_msgs) == 1 and len(bot.sent) == 1 and bot.sent[0][0] == "@whale_log"
    for txt in (admin_msgs[0], bot.sent[0][1]):
        assert "Ali" in txt and "1001" in txt and "10 گیگ" in txt and "24 ساعت" in txt and r["username"] in txt     # چه کسی، چه حجمی
    assert not alerts                                                                       # اعلانِ عمومی تکراری نشد


@pytest.mark.asyncio
async def test_only_channel_or_only_admins_and_default_off_uses_generic_alert(env, monkeypatch):
    await _enable(env)
    admin_msgs, alerts = [], []

    async def na(bot, row, text, *a, **k): admin_msgs.append(text)

    async def alert(*a, **k): alerts.append(1)
    monkeypatch.setattr(tenant, "_notify_admins", na)
    monkeypatch.setattr(tenant, "_purchase_alert", alert)
    row = NS(id=7, owner_id=500)
    # پیش‌فرض خاموش ⇒ رفتارِ قبلی (اعلانِ عمومیِ خرید)
    b1 = _TBot()
    await SV.buy_test(7, 1001, api_factory=FakeAPI, bot=b1, row=row)
    assert alerts == [1] and not admin_msgs and not b1.sent
    # فقط کانال
    from tenant import set_setting
    await set_setting(7, "psel_test_log_channel", "-1001234567890")
    tenant._SETTING_CACHE.clear()
    async with env() as s:                                                                  # مشتریِ دومِ تست
        s.add(models.BotUser(id=2, bot_id=7, telegram_id=2002, full_name="Sara", balance=0))
        await s.commit()
    b2 = _TBot()
    await SV.buy_test(7, 2002, api_factory=FakeAPI, bot=b2, row=row)
    assert len(b2.sent) == 1 and not admin_msgs and alerts == [1]


@pytest.mark.asyncio
async def test_broken_channel_never_breaks_the_test_purchase(env, monkeypatch):
    await _enable(env)
    from tenant import set_setting
    await set_setting(7, "psel_test_log_channel", "@gone")
    await set_setting(7, "psel_test_notify", "1")
    tenant._SETTING_CACHE.clear()
    admin_msgs = []

    async def na(bot, row, text, *a, **k): admin_msgs.append(text)
    monkeypatch.setattr(tenant, "_notify_admins", na)
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI, bot=_TBot(fail_channel=True), row=NS(id=7, owner_id=500))
    assert r["status"] == "ok" and len(admin_msgs) == 1                                     # کانال خراب ⇒ باز هم تست ساخته و ادمین خبر شد


@pytest.mark.asyncio
async def test_owner_ui_toggle_and_channel_connect_validation(env, monkeypatch):
    from tenant.bots.panel_seller import admin as AM

    async def _guard(event, bot): return NS(id=7, owner_id=500, field="panel_seller")
    monkeypatch.setattr(AM, "_guard", _guard)

    class St:
        async def set_state(self, s): self.s = s
        async def clear(self): self.s = None

    class Msg:
        def __init__(self, text): self.text, self.out = text, []
        async def answer(self, text, **k): self.out.append(text)

    class Bot_:
        def __init__(self, ok): self.ok, self.sent = ok, []
        async def send_message(self, chat, text, **k):
            if not self.ok:
                raise RuntimeError("Forbidden: bot is not a member")
            self.sent.append(chat)
    from tenant import get_setting
    st = St()
    bad = Msg("not a channel!")
    await AM.test_channel_save(bad, Bot_(True), st)
    assert "نامعتبر" in bad.out[-1] and await get_setting(7, "psel_test_log_channel", "") == ""
    m2 = Msg("@whale_log")
    await AM.test_channel_save(m2, Bot_(False), st)                                         # ربات ادمین نیست
    assert "ادمین کن" in m2.out[-1] and await get_setting(7, "psel_test_log_channel", "") == ""
    tenant._SETTING_CACHE.clear()
    bot_ok = Bot_(True)
    m3 = Msg("https://t.me/whale_log")
    await AM.test_channel_save(m3, bot_ok, st)
    tenant._SETTING_CACHE.clear()
    assert bot_ok.sent == ["@whale_log"] and await get_setting(7, "psel_test_log_channel", "") == "@whale_log"
    m4 = Msg("-")
    await AM.test_channel_save(m4, Bot_(True), st)
    tenant._SETTING_CACHE.clear()
    assert await get_setting(7, "psel_test_log_channel", "") == ""
