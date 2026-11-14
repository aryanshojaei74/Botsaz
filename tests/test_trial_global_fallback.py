"""PATCH_TRIAL_GLOBAL_FALLBACK_V1 — دکمهٔ «تست رایگان» باید حجم/مدتِ ثبت‌نشده را از تنظیماتِ کلیِ تست بخواند (تیکت #849)."""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.flush()
        s.add(models.MainButton(id=3, bot_id=9, row=0, col=0, label="تست", action="free_trial", config=json.dumps({"trial_mode": "auto"})))
        await s.commit()
    await tenant.set_setting(9, "trial_gb", "0.5")
    await tenant.set_setting(9, "trial_days", "7")
    tenant._SETTING_CACHE.clear()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_unset_values_come_from_global_settings(db):
    cfg = await tenant._trial_fill_global(9, {"trial_mode": "auto"})
    assert cfg["trial_gb"] == "0.5" and cfg["trial_days"] == "7" and cfg["_from_global"] == ["gb", "duration"]


@pytest.mark.asyncio
async def test_explicit_button_values_win_and_duration_is_one_unit(db):
    cfg = await tenant._trial_fill_global(9, {"trial_gb": "3", "trial_days": "2"})
    assert cfg["trial_gb"] == "3" and cfg["trial_days"] == "2" and "_from_global" not in cfg
    cfg2 = await tenant._trial_fill_global(9, {"trial_hours": "6"})          # فقط ساعت ست شده ⇒ مدت از دکمه، نه ترکیب
    assert "trial_days" not in cfg2 and cfg2["trial_hours"] == "6" and cfg2["trial_gb"] == "0.5"


@pytest.mark.asyncio
async def test_update_stores_only_explicit_values_but_returns_filled(db):
    out = await tenant._trial_cfg_update(3, trial_users="2")
    assert out["trial_gb"] == "0.5" and out["trial_days"] == "7"
    async with db() as s:
        raw = json.loads((await s.get(models.MainButton, 3)).config)
    assert "trial_gb" not in raw and "trial_days" not in raw and raw["trial_users"] == "2"


@pytest.mark.asyncio
async def test_delivery_uses_global_values_for_button_without_explicit_ones(db, monkeypatch):
    got = {}

    async def fake_deliver(bot, bot_id, u, mode, gb, days, panel_key, back_kb, **k):
        got.update(gb=gb, days=days, hours=k.get("hours"))
    monkeypatch.setattr(tenant, "_trial_deliver", fake_deliver)

    async def allow(*a, **k):
        return True, ""
    monkeypatch.setattr(tenant, "_check_button_trial_allowance", allow)
    from types import SimpleNamespace

    class Msg:
        from_user = SimpleNamespace(id=5, username="x", is_bot=False)

        async def answer(self, *a, **k):
            return None
    u = SimpleNamespace(telegram_id=5, username="x", id=1)
    await tenant.do_free_trial(Msg(), SimpleNamespace(), 9, u, btn_cfg={"trial_mode": "auto"}, mb_id=3)
    assert got == {"gb": 0.5, "days": 7, "hours": 0}
    got.clear()
    await tenant.do_free_trial(Msg(), SimpleNamespace(), 9, u, btn_cfg={"trial_mode": "auto", "trial_gb": "4", "trial_days": "1"}, mb_id=3)
    assert got == {"gb": 4.0, "days": 1, "hours": 0}
