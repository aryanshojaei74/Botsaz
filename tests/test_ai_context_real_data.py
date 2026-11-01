"""PATCH_AI_REAL_PRICE_V1 / PATCH_AI_REAL_BUTTON_PATH_V1 — دستیارِ هوش مصنوعی (SINA): «بوست ۰ تومان» و ارجاع به دکمه‌ای که در منو نیست."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.smm as SMM
import tenant.bots.virtual_service.smm_ai as AI


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SMM, AI):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    SMM._PRICE_MEMO.clear()
    async with m() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.commit()
    yield m
    await engine.dispose()


async def _ctx():
    return await AI._live_bot_context(7, type("R", (), {"field": "virtual_service"})(), 1001)


@pytest.mark.asyncio
async def test_margin_mode_service_price_is_real_not_zero(env):
    """در حالتِ «درصد سود» ستونِ sell_rate خالی است؛ قبلاً دستیار «۰ تومان» می‌گفت."""
    async with env() as s:
        s.add(models.SmmService(bot_id=7, service_id="9", name="بوست ۱ روزه", category="تلگرام", min_qty=10, max_qty=1000,
                                panel_rate=10000, sell_rate=0, is_active=True))
        await s.commit()
    txt = await _ctx()
    line = next(l for l in txt.splitlines() if "بوست ۱ روزه" in l)
    assert "قیمت حداقلِ سفارش" in line and "ثبت نشده" not in line
    import re
    nums = [int(x.replace(",", "")) for x in re.findall(r"([\d,]+) تومان", line)]
    assert nums and all(n > 0 for n in nums), line                        # هیچ قیمتِ صفرِ دروغین


@pytest.mark.asyncio
async def test_unpriced_manual_service_says_not_set_instead_of_zero(env):
    async with env() as s:
        s.add(models.SmmService(bot_id=7, service_id="m1", name="سرویسِ دستیِ بدونِ قیمت", category="x", min_qty=1, max_qty=5,
                                panel_rate=0, sell_rate=0, is_active=True, is_manual=True))
        await s.commit()
    line = next(l for l in (await _ctx()).splitlines() if "سرویسِ دستیِ بدونِ قیمت" in l)
    assert "ثبت نشده" in line and "حدس نزن" in line and "۰ تومان" not in line


@pytest.mark.asyncio
async def test_manual_price_is_used_when_set(env):
    async with env() as s:
        s.add(models.SmmService(bot_id=7, service_id="a", name="سرویسِ دستی", category="x", min_qty=100, max_qty=5000,
                                panel_rate=0, sell_rate=2000, is_active=True))
        await s.commit()
    line = next(l for l in (await _ctx()).splitlines() if "سرویسِ دستی؛" in l)
    assert "200 تومان" in line or "۲۰۰ تومان" in line or "200" in line     # ۱۰۰ عدد × ۲۰۰۰ در ۱۰۰۰
    assert "2,000 تومان" in line                                             # قیمتِ ۱۰۰۰ عدد


@pytest.mark.asyncio
async def test_stars_path_uses_real_button_label_with_parent_and_never_the_old_hardcoded_name(env):
    from tenant import set_setting
    await set_setting(7, "stars_sell_rate", "4646")
    tenant._SETTING_CACHE.clear()
    async with env() as s:
        parent = models.MainButton(bot_id=7, row=0, col=0, label="🛒 خرید ویژه", action="submenu")
        s.add(parent)
        await s.flush()
        s.add(models.MainButton(bot_id=7, row=1, col=0, label="⭐ خرید استارز", action="stars_shop", parent_id=parent.id))
        await s.commit()
    txt = await _ctx()
    stars = next(l for l in txt.splitlines() if l.startswith("فروش استارز"))
    assert "«🛒 خرید ویژه ← ⭐ خرید استارز»" in stars and "استارز و تون" not in stars


@pytest.mark.asyncio
async def test_no_stars_button_in_menu_means_no_invented_button(env):
    """SINA: «این دکمه‌ای که میگه برا خیلی وقت پیشه، اصلاً نیست» — دکمه‌ای نیست ⇒ نامی ساخته نشود."""
    from tenant import set_setting
    await set_setting(7, "stars_sell_rate", "4646")
    tenant._SETTING_CACHE.clear()
    stars = next(l for l in (await _ctx()).splitlines() if l.startswith("فروش استارز"))
    assert "استارز و تون" not in stars and "دکمهٔ مستقیمی" in stars and "حدس نزن" in stars
    assert await AI._action_path(7, "stars_shop") is None


@pytest.mark.asyncio
async def test_menu_paths_export_text_tree_and_settings_button(env):
    """PATCH_AI_MENU_PATHS_EXPORT_V1 — «بخش مسیرها کجاست؟»: متنِ مسیرها از رویِ منویِ همان لحظه."""
    assert "هنوز دکمه‌ای" in await AI.build_menu_paths_text(7)
    async with env() as s:
        parent = models.MainButton(bot_id=7, row=0, col=0, label="🛒 خرید ویژه", action="submenu")
        s.add(parent)
        await s.flush()
        s.add(models.MainButton(bot_id=7, row=1, col=0, label="⭐ خرید استارز", action="stars_shop", parent_id=parent.id))
        s.add(models.MainButton(bot_id=7, row=0, col=1, label="ℹ️ راهنما", action=None, message="متنِ راهنما"))
        s.add(models.MainButton(bot_id=7, row=2, col=0, label="👛 کیف پول", action="wallet"))
        await s.commit()
    txt = await AI.build_menu_paths_text(7)
    lines = txt.splitlines()
    assert any(l.startswith("- «🛒 خرید ویژه»") and "زیرمنو" in l for l in lines)
    assert any(l.startswith("  - «⭐ خرید استارز»") and "فروشِ استارز" in l for l in lines)       # زیرمنو با تورفتگی + نامِ فارسیِ عملکرد
    assert any("«ℹ️ راهنما» ← نمایشِ پیام" in l for l in lines) and any("«👛 کیف پول»" in l for l in lines)
    text, kbm = await AI.smm_ai_settings_content(7, "smm")
    assert "smmai:paths:smm" in [b.callback_data for r in kbm.inline_keyboard for b in r]


def test_paths_callback_has_a_handler():
    import re
    from pathlib import Path
    src = (Path(AI.__file__)).read_text(encoding="utf-8")
    assert re.search(r'callback_query\(F\.data\.startswith\("smmai:paths:"\)\)', src)
