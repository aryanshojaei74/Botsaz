"""PATCH_AI_MENU_KNOWLEDGE_V1 — دستیارِ هوش‌مصنوعی همهٔ دکمه‌ها (با زیرمنو) و عملکردشان و واقعیت‌هایِ احراز را می‌داند."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import menu_knowledge as MK
from tenant.shared import kyc as K
from tenant.bots.virtual_service import smm_ai as AI


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, MK, AI, tenant, K):
        if hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    async with m() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=6, owner_id=2, token="6:T", username="c", field="virtual_service", plan="pro", status="active"))
        s.add(models.MainButton(id=1, bot_id=5, row=0, col=0, label="💰 کیف پول", action="wallet"))
        s.add(models.MainButton(id=2, bot_id=5, row=0, col=1, label="📚 بیشتر", action="submenu"))
        s.add(models.MainButton(id=3, bot_id=5, row=1, col=0, label="📜 قوانین", action="skip", message="قوانین ما: احترام به همه\nو رعایت مقررات", parent_id=2))
        s.add(models.MainButton(id=4, bot_id=5, row=1, col=1, label="🔥 ویژه", action="custom_unknown_x"))
        s.add(models.MainButton(id=9, bot_id=6, row=0, col=0, label="دکمهٔ رباتِ دیگر", action="wallet"))
        await s.commit()
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_every_button_with_path_and_function_and_no_other_bot(env):
    lines = await MK.build_menu_knowledge(5)
    txt = "\n".join(lines)
    assert "مسیر «💰 کیف پول»" in txt and "موجودی" in txt                              # عملکردِ اکشن
    assert "مسیر «📚 بیشتر ← 📜 قوانین»" in txt                                         # مسیرِ زیرمنو
    assert "قوانین ما: احترام به همه و رعایت مقررات" in txt                             # پیامِ ثابت (یک‌خطی‌شده)
    assert "یک زیرمنو باز می‌کند: 📜 قوانین" in txt
    assert "مسیر «🔥 ویژه»" in txt                                                      # اکشنِ ناشناخته هم حذف نمی‌شود
    assert "رباتِ دیگر" not in txt
    assert all(ln.startswith("- ") for ln in lines)                                    # هر دکمه یک خطِ مستقل برایِ fit_context
    assert "\n" not in "".join(l.rstrip("\n") for l in lines[:1])


@pytest.mark.asyncio
async def test_menu_changes_show_up_immediately(env):
    before = "\n".join(await MK.build_menu_knowledge(5))
    async with env() as s:
        s.add(models.MainButton(id=20, bot_id=5, row=3, col=0, label="🆕 دکمهٔ تازه", action="ticket"))
        await s.commit()
    after = "\n".join(await MK.build_menu_knowledge(5))
    assert "دکمهٔ تازه" not in before and "دکمهٔ تازه" in after and "تیکت" in after


@pytest.mark.asyncio
async def test_live_context_contains_buttons_and_kyc_levels(env):
    await tenant.set_setting(5, "kyc_enabled", "1")
    ctx = await AI._live_bot_context(5, type("R", (), {"field": "virtual_service"})())
    assert "مسیر «📚 بیشتر ← 📜 قوانین»" in ctx
    assert "۳ سطح" in ctx and "سطح ۲ (احرازِ کامل)" in ctx and "500,000" in ctx
    # با بودجهٔ کم، خطوطِ مرتبط با سؤال می‌مانند
    small = AI.fit_context(ctx, "سطح احراز چنده؟", 1500)
    assert "احرازِ هویت" in small


@pytest.mark.asyncio
async def test_kyc_off_is_stated(env):
    out = "\n".join(await MK.kyc_facts(6))
    assert "فعال نیست" in out
