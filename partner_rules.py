"""
partner_rules.py — ثابت‌هایِ مشترکِ «ربات همکار».

=== PATCH_PARTNER_PANEL_V1 ===
این فایل عمداً هیچ وابستگی‌ای ندارد (نه به platform_bot، نه به tenant)
تا هر دو سرویس بتوانند بدونِ خطرِ import دوری از آن استفاده کنند.
قواعدِ عددیِ همکاری فقط همین‌جا تعریف می‌شوند تا دو سرویس هیچ‌وقت
دو عددِ متفاوت را ملاک قرار ندهند.
"""
from __future__ import annotations

# مهلتِ ارفاقی وقتی ربات‌های فعالِ همکار زیرِ حدِ نصاب می‌رود (روز)
LOW_STOCK_GRACE_DAYS = 30

# حدِ نصابِ ربات‌های فعالِ فروخته‌شده
MIN_ACTIVE_SALES = 10

# درصدِ تخفیفِ پیش‌فرضِ همکار رویِ قیمتِ پایهٔ پلن‌ها
DEFAULT_PARTNER_DISCOUNT = 30

# مدتِ رباتِ تستِ رایگانی که همکار می‌تواند به کاربرش بدهد (روز)
PARTNER_TRIAL_DAYS = 2

# سقفِ تعداد در یک سفارشِ خریدِ عمده
MAX_ORDER_QTY = 100


# === PATCH_PARTNER_V4 ===
# تاریخِ شمسی به وقتِ تهران. این‌جا گذاشته شد چون هم سرویسِ پلتفرم و هم
# سرویسِ مستأجر لازمش دارند و partner_rules.py تنها فایلِ بی‌وابستگیِ
# مشترکِ بینِ آن دو است (نسخهٔ platform_bot.to_jalali برایِ tenant قابل
# import نیست).
import datetime as _dt

TEHRAN_TZ = _dt.timezone(_dt.timedelta(hours=3, minutes=30))

_JMONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def to_tehran(dt: "_dt.datetime | None") -> "_dt.datetime | None":
    """datetimeِ بدونِ تایم‌زون را به وقتِ تهران تفسیر/تبدیل می‌کند.

    مقادیرِ دیتابیس naive ذخیره می‌شوند و با datetime.now() سرور نوشته
    شده‌اند؛ پس اگر تایم‌زونِ سرور UTC باشد باید جابه‌جا شوند.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # naive = ساعتِ محلیِ سرور
        dt = dt.astimezone() if hasattr(dt, "astimezone") else dt
        if dt.tzinfo is None:
            return dt
    return dt.astimezone(TEHRAN_TZ)


def jalali(dt: "_dt.datetime | None", with_time: bool = False,
           month_name: bool = False) -> str:
    """تاریخِ شمسی به وقتِ تهران.

    with_time=True  → «۱۴۰۵/۰۵/۰۹ — ۲۳:۴۲»
    month_name=True → «۹ مرداد ۱۴۰۵»
    """
    if dt is None:
        return "—"
    try:
        import jdatetime
        local = to_tehran(dt) or dt
        j = jdatetime.datetime.fromgregorian(
            datetime=local.replace(tzinfo=None))
        if month_name:
            out = f"{j.day} {_JMONTHS[j.month - 1]} {j.year}"
            return f"{out} — {j:%H:%M}" if with_time else out
        return j.strftime("%Y/%m/%d — %H:%M" if with_time else "%Y/%m/%d")
    except Exception:
        local = to_tehran(dt) or dt
        return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def now_tehran() -> "_dt.datetime":
    """اکنون، به وقتِ تهران و بدونِ تایم‌زون (هم‌شکل با مقادیرِ دیتابیس)."""
    return _dt.datetime.now(TEHRAN_TZ).replace(tzinfo=None)


# === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
# حدِ نصاب دیگر فقط عددِ ثابتِ بالا نیست: مدیرِ پلتفرم می‌تواند از پنلِ
# «🤝 ربات‌های همکار» عوضش کند. مقدار در جدولِ settings با bot_id=0 (همان
# جایی که get_pset/set_pset در platform_bot می‌نویسند) ذخیره می‌شود تا هر
# دو سرویس — پلتفرم و مستأجر — یک عدد را ببینند.
#
# ایمپورت‌ها عمداً داخلِ تابع‌اند تا این فایل در زمانِ import همچنان
# بی‌وابستگی بماند (همان قاعده‌ای که بالای فایل نوشته شده).
PARTNER_MIN_ACTIVE_KEY = "partner_min_active"


async def min_active_sales() -> int:
    """حدِ نصابِ فعلی. اگر تنظیم نشده یا خراب بود، به عددِ ثابت برمی‌گردد."""
    try:
        from sqlalchemy import select
        from config import Session
        from models import Setting
        async with Session() as s:
            r = await s.scalar(select(Setting).where(
                Setting.bot_id == 0, Setting.key == PARTNER_MIN_ACTIVE_KEY))
        if r and str(r.value).strip().isdigit():
            return int(str(r.value).strip())
    except Exception:
        import logging
        logging.getLogger("partner_rules").exception(
            "خواندنِ حدِ نصابِ فروشِ فعال ناموفق بود")
    return MIN_ACTIVE_SALES


async def set_min_active_sales(value: int) -> None:
    """ذخیرهٔ حدِ نصابِ جدید (۰ یعنی قاعده عملاً خاموش)."""
    from sqlalchemy import select
    from config import Session
    from models import Setting
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == 0, Setting.key == PARTNER_MIN_ACTIVE_KEY))
        if r:
            r.value = str(int(value))
        else:
            s.add(Setting(bot_id=0, key=PARTNER_MIN_ACTIVE_KEY, value=str(int(value))))
        await s.commit()


# === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
# گزارشِ مدیر: ربات‌هایِ همکار خودبه‌خود قطع می‌شدند و بعضی همکاران پیامِ
# «پلن را تمدید کن» می‌گرفتند — در حالی که مدلِ همکاری اصلاً تمدیدِ دوره‌ای
# ندارد؛ تنها قاعدهٔ بقایِ همکار «حدِ نصابِ ربات‌های فعال» است.
#
# ریشه: هنگامِ فعال‌سازی، هم BotRow.plan_expires_at و هم PartnerBot.expires_at
# روی «۳۰ روز بعد» ست می‌شد. از آن‌جا دو حلقهٔ مستقل سراغش می‌آمدند:
# subscription_loop (یادآوریِ تمدید + بایگانیِ nonpayment) و partner_rules_loop
# (غیرفعال‌سازی سرِ expires_at). هیچ‌کدام ربطی به حدِ نصاب نداشت، برای همین
# عوض‌کردنِ حدِ نصاب به ۷ هم جلویشان را نمی‌گرفت.
#
# حالا این رفتار پشتِ یک کلید است و پیش‌فرض **خاموش** است: رباتِ همکار
# بی‌انقضا می‌ماند و فقط با قاعدهٔ حدِ نصاب یا دستِ خودِ مدیر خوابانده می‌شود.
# اگر مدیری بخواهد مدلِ اشتراکِ دوره‌ای داشته باشد، از پنل روشنش می‌کند.
PARTNER_TERM_KEY = "partner_term_enabled"


async def partner_term_enabled() -> bool:
    """آیا «انقضایِ دوره‌ایِ رباتِ همکار» فعال است؟ پیش‌فرض: خیر."""
    try:
        from sqlalchemy import select
        from config import Session
        from models import Setting
        async with Session() as s:
            r = await s.scalar(select(Setting).where(
                Setting.bot_id == 0, Setting.key == PARTNER_TERM_KEY))
        return bool(r and str(r.value).strip() == "1")
    except Exception:
        return False


async def set_partner_term_enabled(on: bool) -> None:
    from sqlalchemy import select
    from config import Session
    from models import Setting
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == 0, Setting.key == PARTNER_TERM_KEY))
        if r:
            r.value = "1" if on else "0"
        else:
            s.add(Setting(bot_id=0, key=PARTNER_TERM_KEY, value="1" if on else "0"))
        await s.commit()


# === PATCH_PARTNER_STOCK_LOG_V1 ===
async def log_stock(session, partner_bot_id: int, plan: str, delta: int,
                    reason: str, note: str = "", balance_after: int | None = None):
    """ثبتِ یک حرکتِ انبار در همان تراکنشی که موجودی را عوض کرده است.

    عمداً commit نمی‌کند؛ صداکننده خودش commit می‌کند تا اگر خریدی وسطِ
    راه شکست خورد، لاگِ دروغین باقی نماند.
    """
    try:
        from models import PartnerStockLog
        session.add(PartnerStockLog(
            partner_bot_id=partner_bot_id, plan=plan, delta=delta,
            reason=reason, note=(note or "")[:160], balance_after=balance_after))
    except Exception:
        # لاگِ انبار هیچ‌وقت نباید جلویِ خودِ فروش را بگیرد
        import logging
        logging.getLogger("partner_rules").exception("ثبتِ حرکتِ انبار ناموفق بود")
