"""PATCH_RENEW_SERVICE_PICKER_V1 — دکمهٔ صفحهٔ اصلیِ «تمدید سرویس»: فهرستِ سرویس‌هایِ قابل‌تمدید ← مستقیم vren:."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import vpn_user as VU


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, VU):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.flush()
        s.add(models.VpnPanel(bot_id=9, panel_key="sanai", base_url="http://x", username="u", password="p", enabled=True))
        s.add(models.VpnPanel(bot_id=9, panel_key="off", base_url="http://y", username="u", password="p", enabled=False))
        for i, (pk, st, rs) in enumerate((("sanai", "active", False), ("sanai", "expired", False), ("off", "active", False),
                                          ("sanai", "hidden", False), ("sanai", "active", True)), start=1):
            s.add(models.PanelAccount(bot_id=9, user_telegram_id=1001, email=f"e{i}", panel_key=pk, status=st,
                                      reseller_made=rs, volume_gb=10, days=30, plan_title=f"سرویس {i}"))
        s.add(models.PanelAccount(bot_id=9, user_telegram_id=2002, email="other", panel_key="sanai", status="active",
                                  volume_gb=5, days=30, plan_title="دیگری"))
        await s.commit()
    yield maker
    await engine.dispose()


async def _renew(monkeypatch, on: bool):
    async def fake_vc(bot_id, key, panel_key=""):
        return "1" if (key == "renew_enabled" and on) else "0"
    monkeypatch.setattr(VU, "_vc", fake_vc)


@pytest.mark.asyncio
async def test_lists_only_own_renewable_services(db, monkeypatch):
    await _renew(monkeypatch, True)
    got = await VU.renewable_services(9, 1001)
    # فقط e1 و e2 (پنل فعال، مخفی/نمایندگی/پنلِ خاموش/کاربر دیگر حذف می‌شوند)؛ جدیدترین اول
    assert [t for _i, t, _s in got] and len(got) == 2
    assert {s for _i, _t, s in got} == {"active", "expired"}
    assert await VU.renewable_services(9, 3003) == []


@pytest.mark.asyncio
async def test_respects_admin_renew_switch(db, monkeypatch):
    await _renew(monkeypatch, False)
    assert await VU.renewable_services(9, 1001) == []


@pytest.mark.asyncio
async def test_show_builds_vren_buttons_and_empty_message(db, monkeypatch):
    await _renew(monkeypatch, True)
    out = []

    async def fake_back(bot_id):
        return []
    monkeypatch.setattr(tenant, "inline_back_row", fake_back)

    class Msg:
        async def answer(self, text, **kw):
            out.append((text, kw.get("reply_markup")))
    await VU.show_renew_services(None, Msg(), 9, 1001)
    kb = out[-1][1]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert len(datas) == 2 and all(d.startswith("vren:") for d in datas)
    await VU.show_renew_services(None, Msg(), 9, 3003)
    assert "پیدا نشد" in out[-1][0]


def test_action_is_registered():
    assert "renew_service" in tenant._SIMPLE_ACTIONS
    assert tenant._SIMPLE_ACTIONS["renew_service"][2] == frozenset({"vpn_sales"})
