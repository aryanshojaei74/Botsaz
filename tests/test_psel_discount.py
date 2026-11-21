"""PATCH_PSEL_DISCOUNT_V1 — کد تخفیف در خریدِ پنل (همان موتورِ رباتِ VPN)."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    import tenant.gateways.discount_engine as DE
    for mod in (config, SV, tenant, DE):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    FakeAPI.log, FakeAPI.fail_create, FakeAPI.new_style = [], False, False
    async with m() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=100_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p", username="s", password="x", enabled=True))
        s.add(models.PanelSellerPackage(id=5, bot_id=7, panel_id=3, title="P", data_gb=50, max_users=5, days=30, price_toman=40_000, active=True))
        s.add(models.DiscountCode(id=1, bot_id=7, code="OFF25", percent=25, disc_type="percent", is_active=True))
        s.add(models.DiscountCode(id=2, bot_id=7, code="MINUS5K", disc_type="amount", amount=5_000, is_active=True))
        s.add(models.DiscountCode(id=3, bot_id=7, code="FREE", percent=100, disc_type="percent", is_active=True))
        s.add(models.DiscountCode(id=4, bot_id=7, code="OLD", percent=50, disc_type="percent", is_active=True,
                                  expires_at=datetime.utcnow() - timedelta(days=1)))
        s.add(models.DiscountCode(id=5, bot_id=7, code="ONLYIR", percent=50, disc_type="percent", is_active=True, panel_key="other"))
        await s.commit()
    yield m
    await engine.dispose()


async def _state(m):
    async with m() as s:
        u = await s.get(models.BotUser, 1)
        codes = {c.id: c.used_count for c in (await s.scalars(select(models.DiscountCode))).all()}
        usages = list((await s.scalars(select(models.DiscountUsage))).all())
    return u.balance, codes, usages


@pytest.mark.asyncio
async def test_percent_code_charges_discounted_price_and_records_usage(env):
    r = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="off25")        # حروف مهم نیست
    assert r["status"] == "ok" and r["paid"] == 30_000 and r["discount"] == 10_000
    bal, codes, usages = await _state(env)
    assert bal == 70_000 and codes[1] == 1 and len(usages) == 1 and usages[0].saved_amount == 10_000
    async with env() as s:
        acc = await s.get(models.PanelSellerAccount, r["account_id"])
    assert acc.price_toman == 30_000                                                # فروشِ ثبت‌شده = مبلغِ واقعی


@pytest.mark.asyncio
async def test_fixed_amount_and_full_discount(env):
    assert (await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="MINUS5K"))["paid"] == 35_000
    r = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="FREE")
    assert r["status"] == "ok" and r["paid"] == 0
    bal, codes, _ = await _state(env)
    assert bal == 65_000 and codes[3] == 1                                         # رایگان شد ولی مصرف ثبت شد


@pytest.mark.asyncio
async def test_one_use_per_user_and_bad_codes_never_charge(env):
    assert (await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25"))["status"] == "ok"
    again = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25")
    assert again["status"] == "bad_discount" and "قبلاً" in again["reason"]
    for bad in ("OLD", "NOPE", "ONLYIR"):
        r = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code=bad)
        assert r["status"] == "bad_discount", bad
    bal, _, usages = await _state(env)
    assert bal == 70_000 and len(usages) == 1                                       # فقط همان یک خریدِ موفق


@pytest.mark.asyncio
async def test_panel_failure_refunds_money_and_returns_the_code(env):
    FakeAPI.fail_create = True
    r = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25")
    assert r["status"] == "panel_failed"
    bal, codes, usages = await _state(env)
    assert bal == 100_000 and codes[1] == 0 and usages == []                        # کد سوخت نشد
    FakeAPI.fail_create = False
    assert (await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25"))["status"] == "ok"


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_burn_code(env):
    async with env() as s:
        (await s.get(models.BotUser, 1)).balance = 1_000
        await s.commit()
    r = await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25")
    assert r["status"] == "insufficient" and r["need"] == 30_000
    _, codes, usages = await _state(env)
    assert codes[1] == 0 and usages == []


@pytest.mark.asyncio
async def test_max_uses_cap(env):
    async with env() as s:
        c = await s.get(models.DiscountCode, 1)
        c.max_uses = 1
        s.add(models.BotUser(id=2, bot_id=7, telegram_id=1002, full_name="B", balance=100_000))
        await s.commit()
    assert (await SV.buy(7, 1001, 5, api_factory=FakeAPI, discount_code="OFF25"))["status"] == "ok"
    assert (await SV.buy(7, 1002, 5, api_factory=FakeAPI, discount_code="OFF25"))["status"] == "bad_discount"


@pytest.mark.asyncio
async def test_quote_preview_does_not_record_and_custom_volume_works(env):
    q = await SV.quote_discount(7, 1001, "OFF25", package_id=5)
    assert q["ok"] and q["price"] == 40_000 and q["final"] == 30_000 and q["discount"] == 10_000
    assert (await SV.quote_discount(7, 1001, "NOPE", package_id=5))["ok"] is False
    assert (await SV.quote_discount(7, 1001, "OFF25", package_id=999))["ok"] is False
    _, codes, usages = await _state(env)
    assert codes[1] == 0 and usages == []
    await tenant.set_setting(7, "psel_custom_price_gb", "1000")
    cfg = await SV.custom_settings(7)
    gb = max(cfg["min_gb"], 10)
    qc = await SV.quote_discount(7, 1001, "MINUS5K", gb=gb)
    assert qc["ok"] and qc["final"] == gb * cfg["price"] - 5_000
    r = await SV.buy_custom(7, 1001, gb, api_factory=FakeAPI, discount_code="MINUS5K")
    assert r["status"] == "ok" and r["paid"] == gb * cfg["price"] - 5_000


# ── مسیرِ دکمه‌ها (FSM): دکمهٔ «کد تخفیف دارم» → ارسالِ کد → خرید با همان کد ──
class _Msg:
    def __init__(self, text=None):
        self.text, self.sent, self.edited = text, [], []
        self.from_user = type("U", (), {"id": 1001, "full_name": "Ali", "username": "ali"})()

    async def answer(self, text, **k):
        self.sent.append((text, k.get("reply_markup")))
        return self

    async def edit_text(self, text, **k):
        self.edited.append((text, k.get("reply_markup")))
        return self


class _Cb:
    def __init__(self, data):
        self.data, self.message, self.answers = data, _Msg(), []
        self.from_user = self.message.from_user

    async def answer(self, text=None, **k):
        self.answers.append(text)


def _cbs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r if b.callback_data]


@pytest.mark.asyncio
async def test_fsm_flow_apply_code_then_buy_charges_discounted_price(env, monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    import tenant.bots.panel_seller.user as PU
    import tenant.bots.panel_seller.common as CM
    row = type("R", (), {"id": 7, "owner_id": 500, "plan": "pro", "field": "panel_seller"})()
    monkeypatch.setattr(PU, "Session", env)
    monkeypatch.setattr(PU, "_row", lambda bot: _coro(row))

    async def ok_gate(r): return True, ""
    monkeypatch.setattr(PU, "sales_gate", ok_gate)
    monkeypatch.setattr(SV, "_api", FakeAPI)

    async def goc(*a, **k): return None
    monkeypatch.setattr(tenant, "get_or_create_user", goc)
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=7, chat_id=1001, user_id=1001))

    # ۱) صفحهٔ بسته دکمهٔ کد تخفیف دارد
    cb = _Cb("psel:b:5")
    await PU.buy_detail(cb, None)
    assert "psel:dc:p:5" in _cbs(cb.message.edited[0][1])
    # ۲) دکمه → منتظرِ کد
    cb = _Cb("psel:dc:p:5")
    await PU.disc_ask(cb, None, st)
    assert await st.get_state() == PU.PselBuyFSM.discount.state
    # ۳) کدِ بد ⇒ خطا و در حالتِ انتظار می‌ماند
    m = _Msg("NOPE")
    await PU.disc_in(m, None, st)
    assert "❌" in m.sent[0][0] and await st.get_state() == PU.PselBuyFSM.discount.state
    # ۴) کدِ درست ⇒ قیمتِ جدید + دکمهٔ خرید
    m = _Msg("OFF25")
    await PU.disc_in(m, None, st)
    assert "30" in m.sent[0][0].replace(",", "").replace("٬", "") or "۳۰" in m.sent[0][0]
    assert "psel:bok:5" in _cbs(m.sent[0][1]) and await st.get_state() is None
    # ۵) خرید ⇒ با همان کد و مبلغِ تخفیف‌خورده
    cb = _Cb("psel:bok:5")
    await PU.buy_do(cb, None, st)
    bal, codes, usages = await _state(env)
    assert bal == 70_000 and codes[1] == 1 and len(usages) == 1
    assert "dc_code" not in await st.get_data()                          # بعد از خرید پاک شد
    # ۶) خریدِ دوباره بدونِ کد = قیمتِ کامل
    cb = _Cb("psel:bok:5")
    await PU.buy_do(cb, None, st)
    bal, codes, _ = await _state(env)
    assert bal == 30_000 and codes[1] == 1


@pytest.mark.asyncio
async def test_code_for_one_package_is_not_applied_to_another(env, monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    import tenant.bots.panel_seller.user as PU
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=7, chat_id=1, user_id=1))
    await st.set_data({"dc_code": "OFF25", "dc_ctx": "p:5"})
    assert await PU._disc_code(st, "p:5") == "OFF25"
    assert await PU._disc_code(st, "p:6") is None and await PU._disc_code(st, "g:5") is None


async def _coro(v):
    return v
