"""تیکت #849 (ManiKhomam): کلیدِ پنلِ حذف‌شده روی دکمهٔ تست می‌ماند و از پنلِ دیگر هم تست می‌ساخت؛ و حجمِ اختصاصیِ دکمه
تنظیماتِ کلی را بی‌صدا می‌پوشاند (PATCH_TRIAL_STALE_PANEL_KEYS_V1 / PATCH_TRIAL_GLOBAL_DIFF_V1)."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant

pytestmark = pytest.mark.asyncio


async def _panel(s, bot_id, key, enabled=True):
    s.add(models.VpnPanel(bot_id=bot_id, base_url=f"https://{key}.example:1/x", username="u", password="p",
                          panel_key=key, enabled=enabled))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=344284721, token="1:T", username="DayLetterVpnBot", field="vpn_sales", plan="growth",
                       status="active")
        s.add(b)
        await s.flush()
        await _panel(s, b.id, "marzban")
        await _panel(s, b.id, "pasargad")
        mb = models.MainButton(bot_id=b.id, row=0, col=0, label="تست رایگان 🎁", action="free_trial", config=json.dumps(
            {"trial_panel_keys": "upanel_mizban,pasargad", "trial_mode": "auto", "trial_days": "1",
             "trial_gb": "0.25", "trial_users": "0"}))
        s.add(mb)
        await s.commit()
        ids = (b.id, mb.id)
    await tenant.set_setting(ids[0], "trial_gb", "0.3")
    await tenant.set_setting(ids[0], "trial_days", "1")
    yield SimpleNamespace(maker=maker, bot_id=ids[0], mb_id=ids[1])
    await engine.dispose()


async def _cfg(env):
    async with env.maker() as s:
        return json.loads((await s.get(models.MainButton, env.mb_id)).config)


async def test_claim_never_picks_a_dead_panel_key(env):
    cfg = await _cfg(env)
    picks = {await tenant._trial_pick_panel_key(env.bot_id, cfg) for _ in range(60)}
    assert picks == {"pasargad"}                       # قبلاً نیمی از دفعات «upanel_mizban» بود ⇒ پنلِ دیگر


async def test_all_ticked_panels_dead_falls_back_to_none(env):
    assert await tenant._trial_pick_panel_key(env.bot_id, {"trial_panel_keys": "gone1,gone2"}) is None
    assert await tenant._trial_pick_panel_key(env.bot_id, {"trial_panel_key": "marzban"}) == "marzban"
    assert await tenant._trial_pick_panel_key(env.bot_id, {}) is None


async def test_two_live_ticked_panels_are_still_random_between_them(env):
    picks = {await tenant._trial_pick_panel_key(env.bot_id, {"trial_panel_keys": "marzban,pasargad,gone"})
             for _ in range(80)}
    assert picks == {"marzban", "pasargad"}


async def test_prune_removes_dead_keys_and_is_idempotent(env):
    assert await tenant._trial_prune_panel_keys(env.mb_id, env.bot_id) is True
    assert (await _cfg(env))["trial_panel_keys"] == "pasargad"
    assert await tenant._trial_prune_panel_keys(env.mb_id, env.bot_id) is False
    assert (await _cfg(env))["trial_gb"] == "0.25"     # بقیهٔ تنظیماتِ دکمه دست‌نخورده


async def test_prune_all_dead_clears_the_key(env):
    await tenant._trial_cfg_update(env.mb_id, trial_panel_keys="x,y")
    assert await tenant._trial_prune_panel_keys(env.mb_id, env.bot_id) is True
    assert "trial_panel_keys" not in await _cfg(env)


async def test_fill_global_flags_button_values_that_differ(env):
    filled = await tenant._trial_fill_global(env.bot_id, await _cfg(env))
    assert [k for k, _a, _b in filled["_global_diff"]] == ["gb"]        # روز برابر است (۱ و ۱)
    assert "اولویت دارد" in tenant._trial_summary_text(filled) and "حجم" in tenant._trial_summary_text(filled)
    same = await tenant._trial_fill_global(env.bot_id, {"trial_mode": "auto", "trial_gb": "0.3", "trial_days": "1"})
    assert not same.get("_global_diff")


async def test_global_page_lists_overriding_buttons_and_sync_clears_them(env, monkeypatch):
    ov = await tenant._trial_overriding_buttons(env.bot_id)
    assert ov and ov[0][0] == "تست رایگان 🎁" and "حجم" in ov[0][1]
    shown = []

    async def owner_ok(_cb, _bot):
        return SimpleNamespace(id=env.bot_id, plan="growth")

    async def menu(_cb, _bot):
        shown.append(1)

    async def ans(*a, **k):
        shown.append(("answer", a))
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "set_trial_menu", menu)
    await tenant.set_trial_sync_buttons(SimpleNamespace(answer=ans), object())
    cfg = await _cfg(env)
    assert "trial_gb" not in cfg and "trial_days" not in cfg and cfg["trial_mode"] == "auto"   # نوع/پنل/کاربر می‌ماند
    assert await tenant._trial_overriding_buttons(env.bot_id) == []
    filled = await tenant._trial_fill_global(env.bot_id, cfg)
    assert filled["trial_gb"] == "0.3" and filled["trial_days"] == "1"
    assert shown and shown[-1] == 1


async def test_reseller_trial_lookup_understands_multi_panel_keys(env, monkeypatch):
    from tenant.shared import web_panel as WP
    monkeypatch.setattr(WP, "Session", env.maker)
    async with env.maker() as s:
        mb = await s.get(models.MainButton, env.mb_id)
        mb.config = json.dumps({"trial_mode": "auto", "trial_panel_keys": "marzban,pasargad", "trial_gb": "0.5",
                                "trial_days": "2"})
        await s.commit()
    assert await WP._rs_trial_cfg(env.bot_id, "marzban") == (0.5, 2, 0)      # قبلاً کلیدِ چندتایی دیده نمی‌شد ⇒ مقدارِ سراسری
    assert await WP._rs_trial_cfg(env.bot_id, "pasargad") == (0.5, 2, 0)
    assert await WP._rs_trial_cfg(env.bot_id, "sanai") == (0.3, 1, 0)        # پنلِ غیرِ انتخابی ⇒ تنظیماتِ کلی
