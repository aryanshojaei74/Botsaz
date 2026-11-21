"""PATCH_NEXT_DISCOUNT_V1 (Kasra): «بعد از خرید بگوید در خریدِ بعدی ۱۰٪ تخفیف می‌گیری» — کدِ شخصیِ یک‌بارمصرف بعد از هر خریدِ موفق."""
import types
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.gateways.discount_engine as DE
import tenant.shared.next_discount as ND

BOT, BUYER, OTHER = 5, 111, 222


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    import sys
    orig = config.Session
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=1, token="5:T", username="shop", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield mk
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _row(plan="pro"):
    return types.SimpleNamespace(id=BOT, plan=plan, username="shop", owner_id=1)


def _buyer(uid=BUYER):
    return types.SimpleNamespace(telegram_id=uid, username="b", full_name="خریدار")


async def _on(pct=10, days=30, minimum=0):
    await tenant.set_setting(BOT, ND.K_ON, "1")
    await tenant.set_setting(BOT, ND.K_PCT, str(pct))
    await tenant.set_setting(BOT, ND.K_DAYS, str(days))
    await tenant.set_setting(BOT, ND.K_MIN, str(minimum))


async def _codes(db):
    async with db() as s:
        return (await s.scalars(select(models.DiscountCode))).all()


@pytest.mark.asyncio
async def test_off_by_default_issues_nothing(db):
    bot = FakeBot()
    assert await ND.issue(bot, _row(), _buyer(), 50_000) is None
    assert await _codes(db) == [] and bot.sent == []


@pytest.mark.asyncio
async def test_issues_personal_one_time_code_and_tells_the_buyer(db):
    await _on(pct=10, days=30)
    bot = FakeBot()
    code = await ND.issue(bot, _row(), _buyer(), 50_000)
    assert code and code.startswith("NX-")
    (c,) = await _codes(db)
    assert c.percent == 10 and c.max_uses == 1 and c.restricted_user_id == BUYER and c.disc_type == "percent"
    assert c.expires_at and timedelta(days=29) < c.expires_at - datetime.utcnow() <= timedelta(days=30)
    (uid, text), = bot.sent
    assert uid == BUYER and code in text and "10" in text and "تخفیف" in text


@pytest.mark.asyncio
async def test_code_works_only_for_the_buyer_and_only_once(db):
    await _on()
    code = await ND.issue(FakeBot(), _row(), _buyer(), 50_000)
    ok = await DE.validate_discount(bot_id=BOT, code=code, telegram_id=BUYER, price=100_000)
    assert ok.ok and ok.discount == 10_000 and ok.final_price == 90_000
    other = await DE.validate_discount(bot_id=BOT, code=code, telegram_id=OTHER, price=100_000)
    assert not other.ok                                                      # کدِ شخصی: برایِ کسِ دیگر معتبر نیست
    async with db() as s:
        await DE.record_usage(s, code_id=ok.code_id, bot_id=BOT, telegram_id=BUYER, saved_amount=ok.discount)
        await s.commit()
    again = await DE.validate_discount(bot_id=BOT, code=code, telegram_id=BUYER, price=100_000)
    assert not again.ok                                                      # یک‌بارمصرف


@pytest.mark.asyncio
async def test_no_second_code_while_one_is_active_but_new_one_after_use(db):
    await _on()
    bot = FakeBot()
    first = await ND.issue(bot, _row(), _buyer(), 50_000)
    assert await ND.issue(bot, _row(), _buyer(), 50_000) is None            # هنوز استفاده‌نشده ⇒ کدِ تازه نمی‌سازد
    assert len(await _codes(db)) == 1
    async with db() as s:
        c = (await s.scalars(select(models.DiscountCode))).one()
        c.used_count = 1
        await s.commit()
    second = await ND.issue(bot, _row(), _buyer(), 50_000)
    assert second and second != first and len(await _codes(db)) == 2        # بعد از مصرف ⇒ برایِ خریدِ بعدی دوباره


@pytest.mark.asyncio
async def test_expired_code_does_not_block_a_new_one(db):
    await _on()
    bot = FakeBot()
    await ND.issue(bot, _row(), _buyer(), 50_000)
    async with db() as s:
        c = (await s.scalars(select(models.DiscountCode))).one()
        c.expires_at = datetime.utcnow() - timedelta(days=1)
        await s.commit()
    assert await ND.issue(bot, _row(), _buyer(), 50_000)


@pytest.mark.asyncio
async def test_minimum_purchase_and_zero_amount_and_no_expiry(db):
    await _on(days=0, minimum=100_000)
    bot = FakeBot()
    assert await ND.issue(bot, _row(), _buyer(), 99_999) is None            # کمتر از حداقل
    assert await ND.issue(bot, _row(), _buyer(), 0) is None
    assert await ND.issue(bot, _row(), _buyer(), 100_000)
    assert (await _codes(db))[0].expires_at is None                          # ۰ روز = بدونِ انقضا


@pytest.mark.asyncio
async def test_plan_without_discount_codes_gets_nothing(db):
    await _on()
    bot = FakeBot()
    assert await ND.issue(bot, _row(plan="free"), _buyer(), 50_000) is None
    assert await ND.issue(bot, _row(), None, 50_000) is None                # خریدارِ نامشخص
    assert await _codes(db) == []


@pytest.mark.asyncio
async def test_blocked_buyer_keeps_the_code_and_never_raises(db):
    await _on()

    class Boom(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("Forbidden: bot was blocked by the user")
    assert await ND.issue(Boom(), _row(), _buyer(), 50_000)
    assert len(await _codes(db)) == 1


@pytest.mark.asyncio
async def test_purchase_alert_issues_for_real_purchases_but_not_for_free_trials(db, monkeypatch):
    await _on()

    async def noop(*a, **k): return None
    monkeypatch.setattr(tenant, "_notify_admins", noop)
    bot = FakeBot()
    async with db() as s:
        row = await s.get(models.Bot, BOT)
    await tenant._purchase_alert(bot, row, buyer=_buyer(), section="سرویس VPN", detail="۱۰ گیگ", amount=50_000, public=False)
    assert await _codes(db) == []                                            # تستِ رایگان/نماینده: کد نمی‌دهد
    await tenant._purchase_alert(bot, row, buyer=_buyer(), section="سرویس VPN", detail="۱۰ گیگ", amount=50_000)
    assert len(await _codes(db)) == 1
    assert any(uid == BUYER and "تخفیف" in t for uid, t in bot.sent)


@pytest.mark.asyncio
async def test_admin_menu_toggles_and_presets(db, monkeypatch):
    row = _row()

    async def gate(cb, bot, feature): return row
    monkeypatch.setattr(tenant, "_gate", gate)

    class Msg:
        def __init__(self): self.texts = []

        async def edit_text(self, text, **k): self.texts.append(text)

        async def answer(self, text, **k): self.texts.append(text)

    class Cb:
        def __init__(self, data):
            self.data, self.message, self.answers = data, Msg(), []

        async def answer(self, text="", **k): self.answers.append(text)
    for data in ("nxd:menu", "nxd:tog", "nxd:p:20", "nxd:d:60", "nxd:m:100000"):
        cb = Cb(data)
        await ND.nxd_menu(cb, FakeBot())
    cfg = await ND.load_cfg(BOT)
    assert cfg == {"on": True, "pct": 20, "days": 60, "min": 100_000}
    assert "تخفیفِ خریدِ بعدی" in cb.message.texts[-1] and "✅ روشن" in cb.message.texts[-1]
    await ND.nxd_menu(Cb("nxd:p:77"), FakeBot())                              # مقدارِ نامعتبر نادیده گرفته می‌شود
    assert (await ND.load_cfg(BOT))["pct"] == 20
