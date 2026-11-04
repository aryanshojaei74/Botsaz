"""PATCH_PTC_EVERYWHERE_V1 — تخفیفِ «نفراتِ برتر» در همهٔ مسیرهایِ خریدِ پلن (Whale Support)."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, pb):
        monkeypatch.setattr(m, "Session", mk)
    import platform_promoters, platform_user_discounts
    for m in (platform_promoters, platform_user_discounts):
        if hasattr(m, "Session"):
            monkeypatch.setattr(m, "Session", mk)
    yield mk
    await eng.dispose()


def test_price_after():
    assert pb.price_after(100_000, 30, 0) == 70_000
    assert pb.price_after(100_000, 0, 15_000) == 85_000
    assert pb.price_after(10_000, 90, 50_000) == 0


@pytest.mark.asyncio
async def test_display_discount_uses_top_customer_percent(db, monkeypatch):
    async def ptc(uid):
        return "percent", 30
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert pct == 30 and flat == 0 and "نفراتِ برتر" in note and "۳۰" not in note and "30" in note


@pytest.mark.asyncio
async def test_display_discount_keeps_bigger_other_discount(db, monkeypatch):
    async def ptc(uid):
        return "percent", 10
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    monkeypatch.setattr(pb, "referral_discount", lambda n: 20)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert pct == 20 and note == ""            # تخفیفِ بزرگ‌تر می‌ماند و یادداشتِ نفراتِ برتر نمی‌آید


@pytest.mark.asyncio
async def test_display_discount_flat(db, monkeypatch):
    async def ptc(uid):
        return "flat", 25_000
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert flat == 25_000 and pct >= 0 and "ماه" in note


@pytest.mark.asyncio
async def test_upgrade_list_shows_users_permanent_discount(db, monkeypatch):
    """PATCH_PLAN_LIST_USER_DISCOUNT_V1 (MAHDI) — صفحهٔ «ارتقا پلن» باید قیمتِ بعد از تخفیفِ شخصیِ کاربر را نشان دهد."""
    from decimal import Decimal
    monkeypatch.setattr(pb, "plans_paid_enabled", lambda: True)
    monkeypatch.setattr(pb, "plan_price_final", lambda p: (200_000, 200_000, 0))

    async def ptc(uid):
        return "percent", 0
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    async with db() as s:
        s.add(models.Bot(id=9, owner_id=555, token="9:T", username="b9", field="vpn_sales", plan="pro", status="active"))
        s.add(models.PlatformUserDiscount(user_telegram_id=555, percent=15, active=True, plans="growth,pro"))
        await s.commit()
    captured = {}

    class _M:
        async def edit_text(self, text, reply_markup=None, **k):
            captured["kb"] = reply_markup

    class _U:
        id = 555

    class _Cb:
        data = "mbup:9"
        from_user = _U()
        message = _M()

        async def answer(self, *a, **k):
            pass

    await pb.my_bot_upgrade(_Cb())
    labels = [b.text for row in captured["kb"].inline_keyboard for b in row]
    assert any("170,000" in t and "15" in t for t in labels), labels         # ۲۰۰٬۰۰۰ − ۱۵٪
