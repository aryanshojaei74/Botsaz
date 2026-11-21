"""PATCH_PARTNER_PANEL_GB_PRICE_V1 — قیمتِ هر گیگِ اختصاصیِ همکار رویِ هر پنل، به‌جایِ درصد
(درخواستِ WHALE SHOP: «پنلِ فلان هر گیگ ۲ هزار، برایِ یه همکار ۱ هزار»)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_partners as VP

PANEL = "pasargad#2"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(VP, "Session", maker)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="w", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.VpnPartner(id=1, bot_id=4, telegram_id=77, discount_percent=1,
                                panel_keys=f"{PANEL},sanai", apply_plans=True,
                                apply_custom=False, active=True))
        await s.commit()
    yield maker
    await engine.dispose()


async def _disc(price, panel=PANEL, gb=50, scope="plans", uid=77):
    return await VP.partner_discount(bot_id=4, telegram_id=uid, price=price, panel_key=panel,
                                     scope=scope, include_auto_reseller=False, gb=gb)


@pytest.mark.asyncio
async def test_gb_price_replaces_percent_on_that_panel(db):
    await VP._set_panel_gb_price(1, PANEL, 1000)
    r = await _disc(100_000, gb=50)                 # ۵۰ گیگ × ۱۰۰۰ = ۵۰٬۰۰۰
    assert r.ok and r.final_price == 50_000 and r.discount == 50_000
    r = await _disc(100_000, panel="sanai", gb=50)  # پنلِ دیگر: همان درصدِ کلی (۱٪)
    assert r.ok and r.final_price == 99_000
    r = await _disc(100_000, gb=0)                  # پلنِ نامحدود: درصد
    assert r.final_price == 99_000
    r = await _disc(30_000, gb=50)                  # هیچ‌وقت گران‌تر از قیمتِ عادی نمی‌شود
    assert r.final_price == 30_000
    r = await _disc(100_000, uid=999)               # کاربرِ عادی
    assert not r.ok and r.final_price == 100_000


@pytest.mark.asyncio
async def test_scope_and_panel_rules_still_apply(db):
    await VP._set_panel_gb_price(1, PANEL, 1000)
    assert await VP.partner_gb_price(4, 77, PANEL, "plans") == 1000
    assert await VP.partner_gb_price(4, 77, PANEL, "custom") == 0   # فروشِ دلخواه تیک ندارد
    assert await VP.partner_gb_price(4, 77, "marzban", "plans") == 0
    await VP._set_panel_gb_price(1, PANEL, None)
    assert await VP.partner_gb_price(4, 77, PANEL, "plans") == 0


class _Msg(SimpleNamespace):
    async def answer(self, text, **k):
        self.sent = getattr(self, "sent", []) + [text]


class _State:
    def __init__(self, d):
        self.d = d

    async def get_data(self):
        return dict(self.d)

    async def clear(self):
        self.d = {}


@pytest.mark.asyncio
@pytest.mark.parametrize("text,gbp,pct", [("1000ت", 1000, None), ("۱۵۰۰", 1500, None),
                                          ("گیگی 800", 800, None), ("20", None, 20)])
async def test_admin_input_parsing(db, monkeypatch, text, gbp, pct):
    async def owner(msg, bot):
        return SimpleNamespace(id=4)

    async def label(bid, key):
        return "پاسارگاد"
    monkeypatch.setattr(VP, "_owner_msg", owner)
    monkeypatch.setattr(VP, "panel_label", label)
    await VP._set_panel_gb_price(1, PANEL, 999)          # مقدارِ قبلی
    m = _Msg(text=text)
    await VP.vprt_panel_pct_save(m, None, _State({"vprt_pct_pid": 1, "vprt_pct_key": PANEL}))
    async with db() as s:
        p = await s.get(models.VpnPartner, 1)
    assert VP._panel_gb_prices(p).get(PANEL) == gbp
    assert VP._panel_discounts(p).get(PANEL) == pct


@pytest.mark.asyncio
async def test_custom_sale_uses_gb_price(db, monkeypatch):
    from tenant.bots.vpn import vpn_user as VU
    async with db() as s:
        p = await s.get(models.VpnPartner, 1)
        p.apply_custom = True
        await s.commit()
    await VP._set_panel_gb_price(1, PANEL, 1000)

    async def vc_all(bot_id, key):
        return {"vc_price_gb": "2000", "vc_price_day": "300"}

    async def tier(cfg, *a, **k):
        return cfg
    monkeypatch.setattr(VU, "_vc_all", vc_all)
    monkeypatch.setattr(VU, "_apply_vc_tier", tier)
    cfg = await VU._vc_all_for_user(4, 77, PANEL)
    assert cfg["vc_price_gb"] == "1000" and cfg["vc_price_day"] == "300"
    cfg = await VU._vc_all_for_user(4, 555, PANEL)        # کاربرِ عادی
    assert cfg["vc_price_gb"] == "2000"
