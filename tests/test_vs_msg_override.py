"""فدایی: متن‌هایِ ثبت/تکمیل/ردِ سفارش باید برایِ استارز/تون/گیفت/ری‌اکشن هم همانی باشد که مالک تنظیم کرده."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import vs_msg_override as VO

pytestmark = pytest.mark.asyncio


def test_rules():
    assert VO.rule_key("✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nxx") == "order_placed_manual"
    assert VO.rule_key("✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n⭐ ...") == "completed"
    assert VO.rule_key("❌ متاسفانه سفارشت رد شد.") == "rejected"
    assert VO.rule_key("↩️ سفارشِ تونِ تو (#5) لغو شد") == "rejected"
    # شمارهٔ مجازی (حاوی خودِ شماره) و متن‌هایِ SMM دست نمی‌خورند
    assert VO.rule_key("✅ <b>سفارش شما با موفقیت تکمیل شد!</b> 🎉") is None
    assert VO.rule_key("✅ سفارشت با موفقیت تکمیل شد. با تشکر") is None


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", maker)
    custom = {}

    async def gs(bid, key, default=""):
        return custom.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    async with maker() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.flush()
        s.add(models.BotUser(id=1, bot_id=3, telegram_id=555, full_name="Ali"))
        await s.flush()
        s.add(models.GiftOrder(bot_id=3, buyer_user_id=1, gift_id="starsell_100", gift_emoji="⭐", star_count=100,
                               price_toman=20000, dest_telegram_id=555, dest_label="خودم", status="sent",
                               tracking_code="TRK12345", quantity=2))
        await s.commit()
    yield custom
    await eng.dispose()


async def test_custom_text_replaces_default_with_variables(db):
    db["smm_txt_completed"] = "سلام! سفارش {{tracking_code}} ({{product_name}} × {{quantity}}) انجام شد 🌹"
    out = await VO.override_text(3, 555, "✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n🔖 کد پیگیری: <code>TRK12345</code>")
    assert out == "سلام! سفارش TRK12345 (⭐ 200 استارز × 2) انجام شد 🌹"


async def test_no_custom_text_means_no_change(db):
    assert await VO.override_text(3, 555, "❌ متاسفانه سفارشت رد شد.\n🔖 کد پیگیری: <code>TRK12345</code>") is None
