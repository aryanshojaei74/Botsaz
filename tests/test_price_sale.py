"""PATCH_PRICE_LOOKUP_SALE_V1 — فروش قیمت‌یاب از ربات اصلی (پیش‌فرض ۱۵۰ هزار تومان)."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_addons
import config
import models
import platform_price_sale as PS


@pytest.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", S)
    monkeypatch.setattr(PS, "Session", S)
    pset = {}

    async def gp(k, d=""):
        return pset.get(k, d)

    async def sp(k, v):
        pset[k] = v
    import platform_bot
    monkeypatch.setattr(platform_bot, "get_pset", gp)
    monkeypatch.setattr(platform_bot, "set_pset", sp)
    bot_addons.invalidate()
    yield S, pset
    bot_addons.invalidate()
    await eng.dispose()


class _U:
    def __init__(self, uid):
        self.id, self.full_name, self.username = uid, "x", "x"


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)

    async def edit_text(self, text, **kw):
        self.sent.append(text)


class _Bot:
    async def send_message(self, *a, **k):
        pass


class _Cb:
    def __init__(self, uid, data):
        self.from_user, self.data, self.message, self.bot, self.alerts = _U(uid), data, _Msg(), _Bot(), []

    async def answer(self, text=None, show_alert=False, **kw):
        self.alerts.append(text)


async def _setup(S, balance):
    async with S() as s:
        s.add(models.PlatformUser(telegram_id=7, full_name="o", balance=balance))
        b = models.Bot(owner_id=7, token="t:1", field="booking", plan="free", username="shopbot")
        s.add(b)
        await s.commit()
        return b.id


@pytest.mark.asyncio
async def test_default_price_and_buy_deducts_once(db):
    S, _ = db
    assert await PS.sale_price() == 150_000
    bid = await _setup(S, 200_000)
    assert await bot_addons.addon_state(bid, PS.ADDON) == "none"
    await PS.ppq_buy(_Cb(7, f"ppq:buy:{bid}"))
    cb2 = _Cb(7, f"ppq:buy:{bid}")
    await PS.ppq_buy(cb2)                                          # خرید دوباره ⇒ کسر دوباره ندارد
    async with S() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 7))
        assert float(u.balance) == 50_000
    bot_addons.invalidate()
    assert await bot_addons.price_active(bid) is True
    assert "قبلا خریداری" in cb2.alerts[-1]


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_activate(db, monkeypatch):
    S, _ = db
    bid = await _setup(S, 10_000)
    called = []

    async def ins(cb, price, note=""):
        called.append(price)
    import platform_bot
    monkeypatch.setattr(platform_bot, "_insufficient_balance", ins)
    await PS.ppq_buy(_Cb(7, f"ppq:buy:{bid}"))
    assert called == [150_000]
    bot_addons.invalidate()
    assert await bot_addons.addon_state(bid, PS.ADDON) == "none"


@pytest.mark.asyncio
async def test_admin_toggle_off_and_on(db, monkeypatch):
    S, _ = db
    bid = await _setup(S, 200_000)
    await PS.ppq_buy(_Cb(7, f"ppq:buy:{bid}"))
    adm = _Cb(config.PLATFORM_ADMIN_ID, f"ppq:t:{bid}:0")
    monkeypatch.setattr(PS, "PLATFORM_ADMIN_ID", config.PLATFORM_ADMIN_ID)
    await PS.ppq_list(adm)
    assert await bot_addons.addon_state(bid, PS.ADDON) == "off"
    assert "⛔" in str(await PS.list_content(0))
    await PS.ppq_list(_Cb(config.PLATFORM_ADMIN_ID, f"ppq:t:{bid}:0"))
    assert await bot_addons.price_active(bid) is True
    # کاربر عادی نمی‌تواند
    other = _Cb(999, f"ppq:t:{bid}:0")
    await PS.ppq_list(other)
    assert await bot_addons.price_active(bid) is True and "فقط مدیر" in other.alerts[-1]


@pytest.mark.asyncio
async def test_hub_and_sale_toggle_hides_button(db):
    S, pset = db
    bid = await _setup(S, 0)
    text, _ = await PS.hub_content()
    assert "150,000 تومان" in text and "ربات‌های خریدار: <b>0</b>" in text
    assert await PS.mybot_button(bid) is not None
    pset["pq_sale_enabled"] = "off"
    assert await PS.mybot_button(bid) is None


def test_wired():
    import inspect
    import platform_admin_menu
    import platform_bot
    assert '"ppq:hub"' in inspect.getsource(platform_admin_menu)
    src = inspect.getsource(platform_bot)
    assert "platform_price_sale_router" in src and "_pq_btn(bot_id)" in src
