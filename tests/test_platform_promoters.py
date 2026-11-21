"""PATCH_PLATFORM_PROMOTER_V1 — معرفِ فروش: تخفیفِ فقط-خریدِ-اول با لینک/کد."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_promoters as PR


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PR):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=11, full_name="A", balance=0))
        s.add(models.PlatformUser(telegram_id=12, full_name="B", balance=0))
        s.add(models.PlatformPromoter(id=1, code="ali", name="Ali", percent=20, active=True))
        s.add(models.PlatformPromoter(id=2, code="off", name="Off", percent=30, active=False))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_attach_and_first_purchase_discount(db):
    assert await PR.promo_for(11) is None                       # هنوز وصل نشده
    ok, _t = await PR.attach(11, "ALI")                         # حساس به بزرگی/کوچکیِ حروف نیست
    assert ok
    info = await PR.promo_for(11)
    assert info and info.percent == 20 and info.promoter_id == 1
    assert PR.pct_with_promo(7, info) == (20, True)             # بزرگ‌ترِ دو تخفیف
    assert PR.pct_with_promo(25, info) == (25, False)
    assert await PR.promo_for(12) is None                       # فقط همان کاربر


@pytest.mark.asyncio
async def test_inactive_unknown_and_first_touch_wins(db):
    assert not (await PR.attach(11, "off"))[0]
    assert not (await PR.attach(11, "nope"))[0]
    async with db() as s:
        s.add(models.PlatformPromoter(id=3, code="sara", name="Sara", percent=5, active=True))
        await s.commit()
    assert (await PR.attach(11, "ali"))[0]
    assert not (await PR.attach(11, "sara"))[0]                 # معرفِ اول می‌ماند
    assert (await PR.promo_for(11)).promoter_id == 1


@pytest.mark.asyncio
async def test_only_first_purchase_counts(db):
    async with db() as s:
        s.add(models.PlanOrder(user_telegram_id=12, plan="growth", amount=0))   # تست/رایگان = خرید نیست
        await s.commit()
    assert (await PR.attach(12, "ali"))[0] and await PR.promo_for(12)
    async with db() as s:
        s.add(models.PlanOrder(user_telegram_id=11, plan="growth", amount=50_000))
        await s.commit()
    assert not (await PR.attach(11, "ali"))[0]                  # قبلاً خرید داشته
    async with db() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 12))
        s.add(models.PlanOrder(user_telegram_id=12, plan="pro", amount=80_000))
        await s.commit()
    assert await PR.promo_for(12) is None                       # بعد از اولین خریدِ پولی تمام


@pytest.mark.asyncio
async def test_finalize_once_and_stats(db):
    await PR.attach(11, "ali")
    info = await PR.promo_for(11)
    async with db() as s:
        d = await PR.finalize(s, 11, info, 80_000)              # ۲۰٪ تخفیف → ۸۰٬۰۰۰ پرداخت = ۲۰٬۰۰۰ تخفیف
        await s.commit()
    assert d == 20_000
    async with db() as s:
        assert await PR.finalize(s, 11, info, 80_000) == 0      # دوبار مصرف نمی‌شود
        assert await PR._stats(s, 1) == (1, 1, 80_000)
    assert await PR.promo_for(11) is None


@pytest.mark.asyncio
async def test_admin_views_have_link_and_stats(db):
    class FakeBot:
        async def get_me(self):
            class M:
                username = "BotsazBot"
            return M()
    txt, kb = await PR._list_view()
    assert "Ali" in str(kb.inline_keyboard) and "prm:add" in str(kb.inline_keyboard)
    t, k = await PR._detail_view(FakeBot(), 1)
    assert "https://t.me/BotsazBot?start=pr_ali" in t and "/code ali" in t and "20٪" in t
    assert await PR._detail_view(FakeBot(), 999) is None
    assert PR._parse_percent("۲۰٪") == 20 and PR._parse_percent("0") is None and PR._parse_percent("95") is None
