"""PATCH_CUSTOMER_UNIFIED_ORDERS_V1 — «سفارش‌های من» (سمت مشتری) سفارش‌های استارز/تون/گیفت/ری‌اکشن/پریمیوم را هم نشان می‌دهد."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm, models):
        if hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", maker)
    t0 = datetime(2026, 10, 11, 12, 0, 0)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=111, username="buyer"))
        s.add(models.BotUser(id=2, bot_id=5, telegram_id=222, username="other"))
        await s.commit()
        s.add(models.SmmOrder(bot_id=5, user_telegram_id=111, service_id="1", link="https://t.me/abc", quantity=100,
                              price_toman=1000, status="sent", created_at=t0, tracking_code="1001"))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="starsell_500", gift_emoji="⭐", star_count=500,
                               price_toman=90000, dest_telegram_id=111, dest_label="خودم", status="sent",
                               created_at=t0 + timedelta(minutes=1), tracking_code="1002"))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="tonsell_150", gift_emoji="💎", star_count=150,
                               price_toman=300000, dest_telegram_id=111, dest_label="wallet", status="refunded",
                               created_at=t0 + timedelta(minutes=2), tracking_code="1003"))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="15", gift_emoji="🧸", star_count=15,
                               price_toman=67973, quantity=2, dest_telegram_id=111,
                               dest_label="خودم\n🆔 آیدی: 111", status="awaiting_admin",
                               created_at=t0 + timedelta(minutes=3), tracking_code="1004"))
        # سفارش کاربر دیگر هرگز نباید دیده شود
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=2, gift_id="srx_20", gift_emoji="⭐️", star_count=20,
                               price_toman=5000, dest_telegram_id=222, dest_label="x", status="sent",
                               created_at=t0 + timedelta(minutes=4), tracking_code="1005"))
        await s.commit()
    yield maker
    await engine.dispose()


def _flat(kb):
    return [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_list_shows_smm_and_gift_family_newest_first_for_this_user_only(db):
    text, kb = await smm._smm_myorders_paginated_content(5, 111, 1)
    flat = _flat(kb)
    codes = [t for t, _c in flat if t in ("1001", "1002", "1003", "1004", "1005")]
    assert codes == ["1004", "1003", "1002", "1001"]                       # جدیدترین اول؛ سفارش 1005 مال کاربر دیگر است
    cbs = [c for _t, c in flat]
    assert any(c.startswith("smmu:gview:") for c in cbs) and any(c.startswith("smmu:oview:") for c in cbs)
    joined = " ".join(t for t, _c in flat)
    assert "استارز" in joined and "TON" in joined and "گیفت" in joined
    assert "67,973" not in joined and "135,946ت" in joined                  # مبلغ کل = قیمت واحد × تعداد
    assert "هنوز هیچ سفارشی" not in text and "صفحهٔ 1 از 1" in text


@pytest.mark.asyncio
async def test_user_with_only_gift_orders_does_not_see_the_empty_message(db):
    async with db() as s:
        s.add(models.BotUser(id=3, bot_id=5, telegram_id=333))
        await s.commit()
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=3, gift_id="premium_3", gift_emoji="🌟", star_count=1000,
                               price_toman=500000, dest_telegram_id=333, dest_label="x", status="awaiting_admin",
                               tracking_code="2001"))
        await s.commit()
    text, kb = await smm._smm_myorders_paginated_content(5, 333, 1)
    assert "هنوز هیچ سفارشی" not in text and any("پریمیوم" in t for t, _c in _flat(kb))
    text2, _ = await smm._smm_myorders_paginated_content(5, 999, 1)        # کاربری که اصلا سفارش ندارد
    assert "هنوز هیچ سفارشی" in text2


@pytest.mark.asyncio
async def test_pagination_merges_both_kinds_by_time(db):
    t0 = datetime(2026, 10, 12, 0, 0, 0)
    async with db() as s:
        for i in range(10):
            s.add(models.SmmOrder(bot_id=5, user_telegram_id=111, service_id="1", link="l", quantity=1, price_toman=1,
                                  status="completed", created_at=t0 + timedelta(minutes=2 * i), tracking_code=f"3{i:03d}"))
            s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="starsell_50", gift_emoji="⭐", star_count=50,
                                   price_toman=10, dest_telegram_id=111, dest_label="x", status="sent",
                                   created_at=t0 + timedelta(minutes=2 * i + 1), tracking_code=f"4{i:03d}"))
        await s.commit()
    total_codes = []
    for page in (1, 2, 3):
        text, kb = await smm._smm_myorders_paginated_content(5, 111, page)
        total_codes += [t for t, c in _flat(kb) if c and c.startswith(("smmu:oview:", "smmu:gview:")) and t.isdigit() and len(t) == 4]
        assert f"صفحهٔ {page} از 2" in text or page == 3
    # ۴ سفارش اولیه + ۲۰ سفارش تازه = ۲۴ → ۲ صفحه (۱۵ + ۹)
    assert len(set(total_codes)) == 24 and total_codes[0] == "4009"          # جدیدترین: آخرین گیفت تازه


@pytest.mark.asyncio
async def test_detail_view_is_owner_only_and_hides_internals(db):
    async with db() as s:
        from sqlalchemy import select
        gid = (await s.scalar(select(models.GiftOrder).where(models.GiftOrder.tracking_code == "1004"))).id
        other = (await s.scalar(select(models.GiftOrder).where(models.GiftOrder.tracking_code == "1005"))).id
    text, kb = await smm._smm_my_gift_detail_content(5, 111, gid, 1)
    assert "جزئیات" in text
    assert "<code>1004</code>" in text and "135,946 تومان" in text and "منتظر تأیید" in text
    assert "🆔 آیدی: 111" in text
    cbs = [c for _t, c in _flat(kb)]
    assert "gift:support:1004" in cbs and "smmu:myorders:1" in cbs
    # سفارش دیگران / سفارش ناموجود
    bad, kb2 = await smm._smm_my_gift_detail_content(5, 111, other, 1)
    assert "پیدا نشد" in bad and "1005" not in bad
    bad2, _ = await smm._smm_my_gift_detail_content(5, 111, 99999, 1)
    assert "پیدا نشد" in bad2
    bad3, _ = await smm._smm_my_gift_detail_content(6, 111, gid, 1)         # ربات دیگر
    assert "پیدا نشد" in bad3


@pytest.mark.asyncio
async def test_direct_entry_keeps_the_real_home_button_in_gift_rows(db):
    _t, kb = await smm._smm_myorders_paginated_content(5, 111, 1, home_cb="mbtn:home")
    cbs = [c for _t, c in _flat(kb)]
    assert any(c.startswith("smmu:gview:") and c.endswith(":1:mbtn") for c in cbs)
    assert "mbtn:home" in cbs
