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
    # گزارشِ فدایی: ثبتِ سفارشِ استارز/تون/ری‌اکشن با کیف‌پول و رسیدِ کارت‌به‌کارت
    assert VO.rule_key("✅ پرداختت ثبت شد — سفارشت برایِ ادمین فرستاده شد.\n🔖 کد پیگیری: <code>8054</code>") == "order_placed_manual"
    assert VO.rule_key("✅ پرداختت ثبت شد — سفارشت برایِ ثبتِ دستیِ ری‌اکشن فرستاده شد.") == "order_placed_manual"
    assert VO.rule_key("✅ رسید دریافت شد و برایِ تاییدِ ادمین فرستاده شد.\n🔖 کد پیگیری: <code>1</code>") == "order_placed_manual"
    assert VO.rule_key("↩️ سفارش تون تو (#5) لغو شد") == "refunded"       # زنجیره: refunded ← rejected ← canceled ← failed
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


async def test_custom_placed_text_replaces_paid_message(db):
    db["smm_txt_order_placed_manual"] = "سفارش {{tracking_code}} ({{product_name}}) ثبت شد 🙏"
    out = await VO.override_text(
        3, 555, "✅ پرداختت ثبت شد — سفارشت برایِ ادمین فرستاده شد.\n🔖 کد پیگیری: <code>TRK12345</code>")
    assert out == "سفارش TRK12345 (⭐ 200 استارز) ثبت شد 🙏"


# ───────── PATCH_VS_CUSTOM_TEXTS_CHAINS_V1: زنجیرهٔ کلیدها + پیام‌های تازه‌شناخته‌شده ─────────
def test_chains_and_newly_recognised_messages():
    assert VO.rule_keys("✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nبرای تحویل دستی ادمین ارسال شد.") == ("order_placed_manual", "sent")
    assert VO.rule_keys("❌ متاسفانه سفارشت رد شد.")[0] == "rejected"
    assert VO.rule_keys("❌ متاسفانه ارسال گیفت ناموفق بود (…)")[0] == "failed"
    assert VO.rule_keys("❌ متاسفانه فعال‌سازی پرمیوم ناموفق بود")[0] == "failed"
    assert VO.rule_keys("❌ متاسفانه سفارشت توسط ارائه‌دهنده لغو شد و مبلغ 5 تومان به کیف‌پولت برگشت.")[0] == "canceled"
    # پیام‌های تکمیلی که قبلا هیچ‌وقت شناخته نمی‌شدند
    for done in ("✅ گیفت با موفقیت ارسال شد!\n5 تومان از کیف‌پولت کسر شد.", "✅ گیفتت با موفقیت ارسال شد! 🎉\n🔖 کد پیگیری: <code>1</code>",
                 "✅ پرمیوم 3 ماهه با موفقیت فعال شد!\n5 تومان", "✅ سفارش تون تو (#12) ارسال شد!\n💎 1 TON"):
        assert VO.rule_key(done) == "completed", done
    # پیام‌هایی که به ادمین می‌روند نباید دست بخورند
    assert VO.rule_key("✅ پرمیوم سفارش #5 با TGTools تحویل داده شد.") is None
    assert VO.rule_key("✅ سفارش استارز #5 با TGTools تحویل داده شد (100 ⭐).") is None
    assert VO.rule_key("🤖 ارسال خودکار استارز — سفارش #5") is None


async def test_registration_falls_back_to_the_provider_text_for_gift_orders(db):
    db["smm_txt_sent"] = "ثبت شد ✅ {{tracking_code}}"
    reg = "✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nبرای تحویل دستی ادمین ارسال شد.\n🔖 کد پیگیری: <code>TRK12345</code>"
    assert await VO.override_text(3, 555, reg) == "ثبت شد ✅ TRK12345"
    db["smm_txt_order_placed_manual"] = "اولویت با من {{product_name}}"
    assert await VO.override_text(3, 555, reg) == "اولویت با من ⭐ 200 استارز"
    # سفارش SMM (کدی که GiftOrder نیست): فقط کلید اصلی؛ متن «ثبت روی پنل» جای پیام ثبت دستی SMM نمی‌نشیند
    db["smm_txt_order_placed_manual"] = ""
    smm_reg = "✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n🔖 کد پیگیری: <code>NOPE999</code>"
    assert await VO.override_text(3, 555, smm_reg) is None


async def test_cancellation_messages_use_the_first_text_the_owner_wrote(db):
    msg = "↩️ سفارش تون تو (#TRK12345) لغو شد و مبلغ 20,000 تومان به کیف‌پولت بازگشت."
    assert await VO.override_text(3, 555, msg) is None
    db["smm_txt_canceled"] = "لغو شد {{tracking_code}}"
    assert await VO.override_text(3, 555, msg) == "لغو شد TRK12345"          # فقط «لغو توسط پنل» ثبت شده
    db["smm_txt_rejected"] = "رد شد"
    assert await VO.override_text(3, 555, msg) == "رد شد"                      # rejected از canceled جلوتر است
    db["smm_txt_refunded"] = "برگشت {{order_id}}"
    assert await VO.override_text(3, 555, msg) == "برگشت TRK12345"             # refunded برای بازگشت تون از همه جلوتر


async def test_balance_and_order_variables_work_for_stars_ton_and_gift_messages(db):
    import config
    import models
    async with config.Session() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = 12345
        await s.commit()
    db["smm_txt_completed"] = ("موجودی {{balance}} | جدید {{new_balance}} | {{product_name}} | {{platform}} | {{category}} | "
                               "{{status}} | {{order_id}}")
    # پیام تون تأییدشده کد پیگیری را بدون <code> و فقط با # دارد
    out = await VO.override_text(3, 555, "✅ سفارش تون تو (#TRK12345) ارسال شد!\n💎 1 TON به آدرست واریز شد.\n🔖 کد پیگیری: TRK12345")
    assert out == "موجودی 12,345 | جدید 12,345 | ⭐ 200 استارز | تلگرام | تلگرام | ✅ ارسال شد | TRK12345"


def test_matching_ignores_vowel_marks_in_the_default_texts():
    # کد برنامه گاهی «برای/تایید» با کسره می‌نویسد؛ مقایسه روی متن بدون اعراب است (اینجا با کد یونیکد تا ثابت بماند)
    kasra = "\u0650"
    assert VO.rule_key(f"✅ رسید دریافت شد و برا{kasra}ی تایید{kasra} ادمین فرستاده شد.") == "order_placed_manual"
    assert VO.rule_key("✅ رسید دریافت شد و برای تایید ادمین فرستاده شد.") == "order_placed_manual"
    assert VO.rule_key(f"↩️ سفارش{kasra} تون{kasra} تو (#5) لغو شد") == "refunded"
