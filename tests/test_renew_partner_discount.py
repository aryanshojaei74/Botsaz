"""PATCH_RENEW_PARTNER_DISCOUNT_V1 — تخفیفِ همکار روی تمدید (قبلاً فقط روی خرید بود)."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.vpn.vpn_partners as VP
import tenant.bots.vpn.vpn_user as VU

PK = "sanai"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, VU, VP):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="w", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.VpnPanel(bot_id=4, panel_key=PK, base_url="http://p", username="u",
                              password="p", enabled=True))
        s.add(models.VpnPlan(id=9, bot_id=4, panel_key=PK, title="P30", volume_gb=30, days=30,
                             price=100_000, active=True))
        s.add(models.BotUser(id=1, bot_id=4, telegram_id=77, balance=500_000))     # همکار
        s.add(models.BotUser(id=2, bot_id=4, telegram_id=88, balance=500_000))     # کاربرِ عادی
        s.add(models.PanelAccount(id=5, bot_id=4, user_telegram_id=77, email="e77", panel_key=PK,
                                  volume_gb=30, days=30, status="active"))
        s.add(models.PanelAccount(id=6, bot_id=4, user_telegram_id=88, email="e88", panel_key=PK,
                                  volume_gb=30, days=30, status="active"))
        s.add(models.VpnPartner(bot_id=4, telegram_id=77, discount_percent=20, panel_keys=PK,
                                apply_plans=True, apply_custom=False, active=True))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_price_helper_scopes(db):
    assert await VU._renew_partner_price(4, 77, 100_000, PK, "plans", 30) == 80_000
    assert await VU._renew_partner_price(4, 88, 100_000, PK, "plans", 30) == 100_000     # غیرِهمکار
    assert await VU._renew_partner_price(4, 77, 100_000, PK, "custom") == 100_000        # apply_custom خاموش
    assert await VU._renew_partner_price(4, 77, 100_000, "other", "plans", 30) == 100_000  # پنلِ دیگر


class _Msg:
    async def answer(self, *a, **k):
        pass

    async def edit_text(self, *a, **k):
        pass


class _Cb:
    def __init__(self, uid, data):
        self.from_user = type("U", (), {"id": uid, "username": "u", "full_name": "n"})()
        self.data = data
        self.message = _Msg()
        self.alerts = []

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)


class _Bot:
    token = "4:T"


async def _renew(monkeypatch, uid, aid):
    class FakeClient:
        calls = []

        async def renew(self, email, days):
            FakeClient.calls.append(("renew", email, days))

        async def add_traffic(self, email, gb):
            FakeClient.calls.append(("gb", email, gb))

        async def close(self):
            pass
    import tenant.shared.panel_clients.panel_pool as pool
    monkeypatch.setattr(pool, "get_sync", lambda *a, **k: FakeClient())

    async def noop(*a, **k):
        return None
    for name in ("_progress_edit", "_purchase_alert"):
        if hasattr(VU, name):
            monkeypatch.setattr(VU, name, noop)
    monkeypatch.setattr(tenant, "_purchase_alert", noop, raising=False)
    monkeypatch.setattr(VU, "_bg", lambda *a, **k: None)
    cb = _Cb(uid, f"vrenp:{aid}:9")
    await VU._vpn_renew_pay_impl(cb, _Bot())
    return cb


@pytest.mark.asyncio
async def test_plan_renewal_charges_discounted_price_for_partner_only(db, monkeypatch):
    maker = db
    await _renew(monkeypatch, 77, 5)
    await _renew(monkeypatch, 88, 6)
    async with maker() as s:
        p = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 77))
        n = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 88))
    assert float(p.balance) == 420_000          # ۱۰۰٬۰۰۰ − ۲۰٪
    assert float(n.balance) == 400_000          # قیمتِ کامل
