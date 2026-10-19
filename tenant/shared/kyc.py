"""
kyc.py — PATCH_KYC_V1

احراز هویتِ سه‌سطحیِ کاربرانِ رباتِ مستأجر.

  سطح ۰ — بدون احراز. سقفِ شارژِ روزانه (پیش‌فرض ۵۰۰٬۰۰۰ تومان).
  سطح ۱ — کارتِ بانکیِ تأییدشده. سقفِ بالاتر (پیش‌فرض ۲٬۰۰۰٬۰۰۰).
           عبور از سقف ⇒ حساب خودکار مسدود می‌شود تا سطح ۲ انجام شود.
  سطح ۲ — مدارکِ کامل (نوعِ فایل را مالکِ ربات تعیین می‌کند). بدون محدودیت.

کلیدهایِ تنظیمات (per-bot):
  kyc_enabled        "1"/"0"
  kyc_cap_l0         سقفِ روزانه سطح ۰
  kyc_cap_l1         سقفِ روزانهٔ سطح ۱
  kyc_l2_media       video_note | video | photo | any
  kyc_l2_text        متنِ دستورالعملِ سطح ۲
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from config import Session
from models import BotUser, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

DEF_CAP_L0 = 500_000
DEF_CAP_L1 = 2_000_000

_MEDIA_LABELS = {
    "video_note": "ویدیو مسیج (پیامِ ویدیویی گِرد)",
    "video":      "ویدیو",
    "photo":      "عکس",
    "any":        "عکس یا ویدیو یا ویدیو مسیج",
}


async def enabled(bot_id: int) -> bool:
    from tenant import get_setting
    if (await get_setting(bot_id, "kyc_enabled", "0")) != "1":
        return False
    # === PATCH_KYC_PLAN_GATE_V1 ===
    # احرازِ هویت از پلنِ رایگان برداشته شد (دکمه‌اش از تنظیمات رفت)، ولی رباتی که قبلاً روشنش کرده بود
    # همچنان محدودیت اعمال می‌کرد و مالک راهی برایِ خاموش‌کردنش نداشت. حالا اگر پلنِ ربات این قابلیت را
    # ندارد، محدودیت اعمال نمی‌شود (تنظیمِ ذخیره‌شده می‌ماند تا با ارتقا برگردد).
    try:
        from tenant import get_bot_row_by_id
        from plans import Feature, has_feature
        row = await get_bot_row_by_id(bot_id)
        if row is not None and not has_feature(row.plan, Feature.KYC_VERIFY):
            return False
    except Exception:
        logger.exception("[KYC] چکِ پلن ناموفق بود؛ طبقِ تنظیمِ ذخیره‌شده")
    return True


async def cap_for(bot_id: int, level: int) -> int:
    """سقفِ شارژِ روزانه برایِ این سطح. صفر = بدون محدودیت."""
    from tenant import get_setting
    if level >= 2:
        return 0
    key = "kyc_cap_l1" if level == 1 else "kyc_cap_l0"
    dflt = DEF_CAP_L1 if level == 1 else DEF_CAP_L0
    try:
        return int(float(await get_setting(bot_id, key, str(dflt)) or dflt))
    except Exception:
        return dflt


# === PATCH_KYC_REAL_DEPOSITS_V1 ===
# فقط «واریزِ واقعی» به سقف حساب می‌شود؛ بازگشتِ وجه، هدیه، پاداش و شارژِ دستیِ ادمین نه.
_NOT_DEPOSIT_KINDS = ("refund", "smm_refund", "bet_refund", "admin", "referral",
                      "welcome_bonus", "prize", "revert")


async def window_start(bot_id: int) -> datetime:
    """ابتدای بازهٔ «روزانه». rolling (پیش‌فرض) = ۲۴ ساعتِ گذشته؛
    calendar = از نیمه‌شبِ تهران (صفرشدنِ سقف سرِ ساعتِ ۰۰:۰۰)."""
    from tenant import get_setting
    if (await get_setting(bot_id, "kyc_window", "rolling")) == "calendar":
        now_t = datetime.utcnow() + timedelta(hours=3, minutes=30)   # تهران (بدونِ ساعتِ تابستانی)
        mid_t = now_t.replace(hour=0, minute=0, second=0, microsecond=0)
        return mid_t - timedelta(hours=3, minutes=30)
    return datetime.utcnow() - timedelta(hours=24)


# === PATCH_KYC_DIRECT_CARD_V1 ===
# گزارشِ فدایی: «کارت‌به‌کارتِ مستقیم از صفحهٔ فاکتور سقف نداشت؛ هرچقدر می‌شد خرید.» پرداختِ مستقیمِ کارتی
# (بدونِ شارژِ کیف پول) هیچ WalletTxn نمی‌سازد، پس هرگز در مصرفِ روزانه جمع نمی‌شد. حالا سفارش‌هایی که رسیدشان
# رسیده/تأیید شده هم شمرده می‌شوند (سفارشِ رهاشده یا ردشده و بازگشت‌خورده نه).
_GIFT_CARD_NOT_PAID = ("awaiting_receipt", "pending", "rejected", "refunded", "canceled", "cancelled",
                       "expired", "returned", "fake_reverted")
_ORDER_CARD_PAID = ("awaiting_admin", "paid", "completed", "delivered")


async def direct_card_today(s, bot_id: int, user_id: int, since) -> int:
    """مجموعِ خریدهایِ مستقیمِ کارت‌به‌کارتِ این کاربر در بازهٔ روزانه (خدماتِ مجازی + سفارشِ ربات‌هایِ VPN)."""
    from models import GiftOrder, Order
    total = 0
    try:
        g = await s.scalar(select(func.coalesce(func.sum(
            GiftOrder.price_toman * func.coalesce(GiftOrder.quantity, 1)), 0)).where(
            GiftOrder.bot_id == bot_id, GiftOrder.buyer_user_id == user_id,
            GiftOrder.pay_method == "card", GiftOrder.status.notin_(_GIFT_CARD_NOT_PAID),
            GiftOrder.created_at >= since))
        total += int(float(g or 0))
        o = await s.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(
            Order.bot_id == bot_id, Order.user_id == user_id,
            Order.pay_method.in_(("card", "vpn_card")), Order.status.in_(_ORDER_CARD_PAID),
            Order.created_at >= since))
        total += int(float(o or 0))
    except Exception:
        logger.exception("[KYC] شمارشِ خریدهایِ کارتیِ مستقیم ناموفق بود (bot=%s user=%s)", bot_id, user_id)
    return total


async def charged_today(bot_id: int, user_id: int) -> int:
    """مجموعِ واریزهایِ واقعیِ بازهٔ روزانه (نک: window_start) + خریدهایِ مستقیمِ کارت‌به‌کارت."""
    since = await window_start(bot_id)
    async with Session() as s:
        v = await s.scalar(
            select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
                WalletTxn.bot_id == bot_id,
                WalletTxn.user_id == user_id,
                WalletTxn.amount > 0,
                WalletTxn.kind.notin_(_NOT_DEPOSIT_KINDS),
                WalletTxn.created_at >= since))
        direct = await direct_card_today(s, bot_id, user_id, since)
    try:
        return int(float(v or 0)) + direct
    except Exception:
        return direct


async def check_charge(bot_id: int, user, amount) -> tuple[bool, str]:
    """آیا این شارژ مجاز است؟ (ok, پیامِ خطا)

    این تابع باید *قبل* از ساختِ فاکتور صدا زده شود تا کاربر پول ندهد و
    بعد رد شود.
    """
    if not await enabled(bot_id):
        return True, ""
    if user is None:
        return True, ""

    if getattr(user, "kyc_blocked", False):
        return False, (
            "🔒 حسابِ شما موقتاً محدود شده است.\n\n"
            "برایِ رفعِ محدودیت باید احرازِ هویتِ سطحِ بعدی را انجام دهی.")

    level = int(getattr(user, "kyc_level", 0) or 0)
    cap = await cap_for(bot_id, level)
    if cap <= 0:
        return True, ""

    used = await charged_today(bot_id, user.id)
    try:
        amount = int(float(amount or 0))
    except Exception:
        amount = 0

    if used + amount <= cap:
        return True, ""

    # === PATCH_KYC_L1_BLOCK_V1 / PATCH_KYC_BLOCK_AFTER_PAY_V1 ===
    # گزارش (Fadayi): سطح ۱ فقط با «درخواستِ» شارژِ بالاتر از سقف، همان لحظه و قبل از پرداخت
    # مسدود می‌شد. حالا درخواست آزاد است و مسدودسازی بعد از *پرداختِ واقعی* انجام می‌شود
    # (note_charge ← after_charge). سطح ۰ مثلِ قبل فقط همان روز نمی‌تواند شارژ کند.
    if level == 1:
        return True, ""

    remain = max(0, cap - used)
    msg = (f"⚠️ سقفِ شارژِ روزانهٔ شما {cap:,} تومان است.\n"
           f"در ۲۴ ساعتِ گذشته {used:,} تومان شارژ کرده‌ای؛ "
           f"تا سقف {remain:,} تومان باقی مانده.\n\n")
    msg += ("برایِ افزایشِ سقف، از «🛡 احرازِ هویت» سطحِ بعدی را انجام بده."
            if level < 2 else "برایِ بررسی با پشتیبانی تماس بگیر.")
    return False, msg


async def note_charge(bot_id: int, user_id: int) -> None:
    """بعد از هر شارژِ موفق: اگر کاربرِ سطح ۱ از سقف رد شده، حسابش را
    خودکار مسدود کن تا احرازِ سطح ۲ را انجام دهد (خواسته صریحِ ادمین)."""
    if not await enabled(bot_id):
        return
    async with Session() as s:
        u = await s.get(BotUser, user_id)
        if not u or int(u.kyc_level or 0) != 1 or u.kyc_blocked:
            return
        cap = await cap_for(bot_id, 1)
        if cap <= 0:
            return
        used = await charged_today(bot_id, user_id)
        if used > cap:
            u.kyc_blocked = True
            u.kyc_note = f"عبور از سقفِ سطح ۱ ({used:,} > {cap:,})"
            await s.commit()
            logger.info("[KYC] bot=%s user=%s مسدود شد (used=%s cap=%s)",
                        bot_id, user_id, used, cap)


async def gate(bot_id: int, telegram_id: int, amount) -> str | None:
    """محافظِ آمادهٔ مسیرهایِ شارژ. اگر مجاز نبود، متنِ خطا را برمی‌گرداند.

    === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    قبلاً هر مسیرِ شارژ خودش کاربر را از دیتابیس می‌خواند و check_charge را
    صدا می‌زد؛ نتیجه این شد که فقط ۵ مسیر از ۹ مسیر گیت داشتند و بقیه
    (ارز دیجیتال، سواپ، استارز، واریزِ مستقیم) کاملاً باز بودند. حالا همهٔ
    مسیرها همین یک تابع را صدا می‌زنند.

    خطایِ داخلی هرگز جلویِ شارژ را نمی‌گیرد (None برمی‌گرداند) — قطعِ
    فروش بدترین حالتِ ممکن است.
    """
    try:
        if not await enabled(bot_id):
            return None
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        ok, why = await check_charge(bot_id, u, amount)
        return None if ok else why
    except Exception:
        logger.exception("[KYC] gate ناموفق بود (bot=%s tg=%s)", bot_id, telegram_id)
        return None


async def after_charge(bot_id: int, user_id: int) -> None:
    """بعد از هر شارژِ موفق صدا زده می‌شود. هرگز استثنا پرتاب نمی‌کند.

    === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    باگ: note_charge نوشته شده بود ولی در کلِ پروژه هیچ‌جا صدا زده نمی‌شد،
    یعنی «مسدودسازیِ خودکارِ سطح ۱ بعد از عبور از سقف» عملاً مرده بود.
    """
    try:
        await note_charge(bot_id, user_id)
    except Exception:
        logger.exception("[KYC] after_charge ناموفق بود (bot=%s user=%s)",
                         bot_id, user_id)


# ═══ === PATCH_KYC_CARD_CHANGE_V1 === تغییرِ کارتِ احرازشده ═══
async def card_change_policy(bot_id: int) -> tuple[int, int]:
    """(حداکثرِ دفعات, در چند روز). حداکثرِ ۰ = تغییرِ کارت ممنوع."""
    from tenant import get_setting

    def _i(v, d):
        try:
            return int(float(v))
        except Exception:
            return d
    return (max(0, _i(await get_setting(bot_id, "kyc_card_change_max", "1"), 1)),
            max(1, _i(await get_setting(bot_id, "kyc_card_change_days", "15"), 15)))


async def card_change_status(bot_id: int, user_id: int) -> tuple[bool, str]:
    """(مجاز؟, توضیح). درخواست‌هایِ تأییدشده و در انتظار در بازهٔ مجاز شمرده می‌شوند."""
    from models import KycRequest
    mx, days = await card_change_policy(bot_id)
    if mx <= 0:
        return False, "تغییرِ کارت در این ربات فعال نیست."
    since = datetime.utcnow() - timedelta(days=days)
    async with Session() as s:
        rows = (await s.scalars(select(KycRequest.created_at).where(
            KycRequest.bot_id == bot_id, KycRequest.user_id == user_id,
            KycRequest.kind == "card_change",
            KycRequest.status.in_(("approved", "pending")),
            KycRequest.created_at >= since).order_by(KycRequest.created_at))).all()
    if len(rows) < mx:
        return True, f"{mx - len(rows)} بار از {mx} بار تغییرِ کارت در {days} روز باقی مانده."
    nxt = rows[len(rows) - mx] + timedelta(days=days)
    left = max(1, int((nxt - datetime.utcnow()).total_seconds() // 3600) + 1)
    when = f"{left // 24} روز و {left % 24} ساعت" if left >= 24 else f"{left} ساعت"
    return False, f"سقفِ تغییرِ کارت ({mx} بار در {days} روز) پر شده؛ حدودِ {when} دیگر می‌توانی دوباره تغییر بدهی."


def media_label(key: str) -> str:
    return _MEDIA_LABELS.get(key, _MEDIA_LABELS["any"])
