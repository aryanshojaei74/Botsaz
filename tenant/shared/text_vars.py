"""
text_vars.py — موتورِ مشترکِ «متغیر» برایِ همهٔ متن‌هایِ قابل‌ویرایشِ ربات.

=== PATCH_TEXT_VARS_V1 ===
گزارشِ ادمین (فدایی): «هوشِ مصنوعیِ رباتت این لیستِ متغیر را داد ({{product_name}}،
{{quantity}}، …) ولی هیچ‌کدام کار نمی‌کند؛ یک لیستِ کاملِ متغیرِ عمومی بساز که در متنِ
حساب کاربری، وضعیتِ سفارش‌ها و بقیهٔ متن‌ها بشود استفاده‌شان کرد».

علتِ اصلی: اصلاً سیستمِ متغیری وجود نداشت. خوش‌آمد فقط سه متغیرِ تک‌آکولادی را
می‌شناخت و متن‌هایِ سفارش هیچ متغیری نداشتند؛ اسم‌هایی که «هوشِ مصنوعی» ساخته بود
هیچ‌جا پیاده نشده بود. این ماژول یک موتورِ واحد است:

  • هم «{{name}}» و هم «{name}» را می‌فهمد (اسم‌هایی که به ادمین گفته شده کار کنند)
  • فقط اسم‌هایِ شناخته‌شده جایگزین می‌شوند؛ اسمِ ناشناس/غلطِ املایی دست‌نخورده می‌ماند
    تا ادمین در پیام ببیند و اصلاحش کند
  • مقدارها HTML-escape می‌شوند (لینک/نامِ کاربر می‌تواند & یا < داشته باشد و پیام را بشکند)
  • مقدارهایی که کوئریِ دیتابیس می‌خواهند فقط وقتی حساب می‌شوند که متن واقعاً استفاده‌شان
    کرده باشد
  • اسم‌هایِ قدیمیِ خوش‌آمد ({name}, {fullname}, {id}) به‌صورتِ alias حفظ شده‌اند
  • هیچ‌وقت exception نمی‌دهد؛ اگر چیزی خراب شود متنِ خام برمی‌گردد
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# گروه‌ها: کلید → عنوان (برایِ نمایشِ لیست)
GROUPS: dict[str, str] = {
    "user": "👤 کاربر — در همهٔ متن‌ها",
    "bot":  "🤖 ربات و زمان — در همهٔ متن‌ها",
    "order": "🧾 سفارش — فقط در متن‌هایِ وضعیتِ سفارش",
}

# نام → (گروه، توضیح، نمونه)
VARS: dict[str, tuple[str, str, str]] = {
    # ── کاربر ──
    "user_name":    ("user", "نامِ کوچکِ کاربر", "علی"),
    "full_name":    ("user", "نامِ کاملِ کاربر", "علی رضایی"),
    "username":     ("user", "یوزرنیمِ کاربر با @ (اگر نداشت «—»)", "@ali"),
    "user_id":      ("user", "آیدیِ عددیِ تلگرامِ کاربر", "123456789"),
    "balance":      ("user", "موجودیِ کیف‌پول (تومان)", "50,000"),
    "orders_count": ("user", "تعدادِ سفارش‌هایِ ثبت‌شدهٔ کاربر", "12"),
    "join_date":    ("user", "تاریخِ عضویتِ کاربر (شمسی)", "۱۴۰۵/۰۷/۰۶"),
    # === PATCH_PROFILE_TEMPLATE_V1 === درخواستِ ادمین (فدایی): متغیرهایِ کارتِ احرازشده، تلفن، سطحِ
    # احراز، وضعیتِ احراز، زیرمجموعه‌ها، تراکنش کل، خریدِ زیرمجموعه‌ها، پاداشِ دریافتی و ...
    "join_time":      ("user", "ساعتِ عضویتِ کاربر", "01:10:22"),
    "phone":          ("user", "شمارهٔ تلفنِ ثبت‌شدهٔ کاربر (اگر نداشت «—»)", "09121234567"),
    "kyc_card":       ("user", "شمارهٔ کارتِ احرازشدهٔ کاربر (اگر احراز نکرده «—»)", "6037-99**-****-1234"),
    "kyc_level":      ("user", "سطحِ احرازِ هویتِ کاربر (عدد: ۰، ۱ یا ۲)", "1"),
    "kyc_level_name": ("user", "نامِ سطحِ احرازِ هویت", "سطح ۱ (کارت تأییدشده)"),
    "kyc_status":     ("user", "وضعیتِ احراز: ✅ احرازشده / ❌ احراز نشده / ⛔ محدود (اگر احراز خاموش است «—»)", "✅ احرازشده"),
    "account_status": ("user", "وضعیتِ اکانت (✅ فعال / ❌ غیرفعال)", "✅ فعال"),
    "profile_orders": ("user", "تعدادِ سفارش‌هایِ کاربر همان‌طور که در «مشخصات شما» شمرده می‌شود", "12"),
    "smm_orders":     ("user", "تعدادِ سفارش‌هایِ SMM (خدماتِ مجازی)ِ ثبت‌شدهٔ کاربر", "12"),
    "active_services": ("user", "تعدادِ سرویس‌هایِ فعالِ کاربر", "3"),
    "total_spent":    ("user", "تراکنشِ کل: مجموعِ سفارش‌هایِ پرداخت‌شده (تومان)", "250,000"),
    "ref_count":      ("user", "تعدادِ زیرمجموعه‌هایِ دعوت‌شده", "5"),
    "ref_orders":     ("user", "تعدادِ خریدهایِ زیرمجموعه‌ها", "8"),
    "ref_spent":      ("user", "مجموعِ خریدِ زیرمجموعه‌ها (تومان)", "1,200,000"),
    "ref_earned":     ("user", "اعتبارِ پاداشِ دریافتیِ تا الان از دعوت (تومان)", "10,000"),
    "ref_summary":    ("user", "بلوکِ کاملِ «زیرمجموعه‌گیری» همان‌طور که در متنِ پیش‌فرض است", "🔗 زیرمجموعه‌های دعوت‌شده: 5 نفر …"),
    # ── ربات و زمان ──
    "bot_username": ("bot", "یوزرنیمِ ربات با @", "@myshop_bot"),
    "date":         ("bot", "تاریخِ امروز (شمسی)", "۱۴۰۵/۰۷/۰۶"),
    "time":         ("bot", "ساعتِ فعلی", "۱۴:۳۰"),
    # ── سفارش ──
    "order_id":       ("order", "شمارهٔ سفارش (همان کدِ پیگیری)", "123"),
    "tracking_code":  ("order", "کدِ پیگیری (همان کدی که مشتری برایِ پشتیبانی می‌فرستد)", "SMM-123"),
    "product_name":   ("order", "نامِ محصول/سرویس", "ممبر گروه"),
    "category":       ("order", "نامِ دسته‌بندیِ محصول", "ممبر کانال"),
    # PATCH_ORDER_VARS_PLATFORM_V1 — «نوعِ اپلیکیشن» (پلتفرمِ دسته/سرویس)، جدا از نامِ دسته
    "platform":       ("order", "نوعِ اپلیکیشن/پلتفرم (تلگرام، اینستاگرام، روبینو…)", "تلگرام"),
    "quantity":       ("order", "تعدادِ سفارش", "1,000"),
    "amount":         ("order", "مبلغِ پرداختی (تومان)", "700"),
    "price_per_1000": ("order", "قیمتِ هر ۱۰۰۰ عدد (تومان)", "700"),
    "link":           ("order", "لینکِ سفارش", "https://t.me/example"),
    "order_time":     ("order", "تاریخ و ساعتِ ثبتِ سفارش", "۱۴۰۵/۰۷/۰۶ — ۱۴:۳۰"),
    "order_date":     ("order", "تاریخِ ثبتِ سفارش", "۱۴۰۵/۰۷/۰۶"),
    "status":         ("order", "وضعیتِ فعلیِ سفارش", "✅ تکمیل"),
    "service_id":     ("order", "شناسهٔ سرویس در پنل", "47"),
    # === PATCH_SMM_PARTIAL_V1 === مخصوصِ پیامِ «نیمه‌تمام» (در بقیهٔ وضعیت‌ها هم مقدارِ معقول دارند)
    # === PATCH_SMM_REFILL_V1 ===
    "refill_id":      ("order", "کدِ پیگیریِ آخرین درخواستِ ریفیلِ این سفارش", "48211"),
    "refill_status":  ("order", "وضعیتِ آخرین ریفیلِ این سفارش (در حال انجام / انجام شد / رد شد)", "انجام شد"),
    "paid_amount":    ("order", "مبلغِ پرداختی برایِ سفارش (تومان)", "172"),
    "refund_amount":  ("order", "مبلغی که به کیف‌پول برگشت (تومان)", "16"),
    "final_amount":   ("order", "مبلغِ نهاییِ سفارش = پرداختی − بازگشتی (تومان)", "156"),
    "new_balance":    ("order", "موجودیِ فعلیِ کیف‌پولِ کاربر (تومان)", "132,155"),
    "completed_qty":  ("order", "مقدارِ تحویل‌شده/تکمیل‌شده", "318"),
    "remaining_qty":  ("order", "مقدارِ باقی‌مانده (تحویل‌نشده)", "32"),
}

# اسم‌هایِ قدیمی که قبلاً در متنِ خوش‌آمد کار می‌کردند و باید همچنان کار کنند.
ALIASES: dict[str, str] = {"name": "user_name", "fullname": "full_name", "id": "user_id",
                           "app": "platform"}

# نامِ متغیر می‌تواند رقم هم داشته باشد (مثلاً price_per_1000)، ولی با رقم شروع نمی‌شود.
_PATTERN = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}|\{([a-z_][a-z0-9_]*)\}")


def canonical(name: str) -> str | None:
    """اسمِ نهاییِ متغیر، یا None اگر ناشناس است."""
    name = ALIASES.get(name, name)
    return name if name in VARS else None


def find_names(text: str) -> set[str]:
    """متغیرهایِ شناخته‌شده‌ای که واقعاً در متن استفاده شده‌اند."""
    out: set[str] = set()
    for m in _PATTERN.finditer(text or ""):
        c = canonical(m.group(1) or m.group(2))
        if c:
            out.add(c)
    return out


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v), quote=False)


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "—"


def _jalali(dt) -> tuple[str, str]:
    try:
        from tenant import _jalali_date_time
        return _jalali_date_time(dt)
    except Exception:
        return ("—", "—")


_KYC_LEVEL_NAMES = {0: "بدون احراز", 1: "سطح ۱ (کارت تأییدشده)", 2: "سطح ۲ (احراز کامل)"}


async def _user_values(names: set[str], bot_id: int | None, user, tg_user) -> dict[str, str]:
    """مقدارِ متغیرهایِ گروهِ کاربر. user = ردیفِ BotUser، tg_user = aiogram User (یکی کافی است)."""
    v: dict[str, str] = {}
    full = None
    first = None
    uname = None
    uid = None
    if user is not None:
        full = getattr(user, "full_name", None)
        uname = getattr(user, "username", None)
        uid = getattr(user, "telegram_id", None)
        first = (full or "").split()[0] if (full or "").strip() else None
    if tg_user is not None:
        # نامِ کوچک/کاملِ زندهٔ خودِ تلگرام دقیق‌تر است و رفتارِ قدیمیِ خوش‌آمد را حفظ می‌کند.
        first = getattr(tg_user, "first_name", None) or getattr(tg_user, "full_name", None) or first
        full = getattr(tg_user, "full_name", None) or full
        uname = getattr(tg_user, "username", None) or uname
        uid = getattr(tg_user, "id", None) or uid
    first = first or full or "دوست عزیز"
    full = full or first
    if "user_name" in names:
        v["user_name"] = _esc(first)
    if "full_name" in names:
        v["full_name"] = _esc(full)
    if "username" in names:
        v["username"] = _esc(f"@{uname}") if uname else "—"
    if "user_id" in names:
        v["user_id"] = _esc(uid if uid is not None else "—")

    # مقدارهایی که دیتابیس می‌خواهند — فقط اگر واقعاً لازم باشند
    # === PATCH_PROFILE_TEMPLATE_V1 ===
    _PROFILE_ROW_VARS = {"join_time", "phone", "kyc_card", "kyc_level", "kyc_level_name", "kyc_status"}
    _PROFILE_STATS_VARS = {"account_status", "profile_orders", "smm_orders", "active_services",
                           "total_spent", "ref_count", "ref_orders", "ref_spent", "ref_earned",
                           "ref_summary"}
    need_db = names & ({"balance", "orders_count", "join_date"} | _PROFILE_ROW_VARS | _PROFILE_STATS_VARS)
    if need_db and bot_id is not None and uid is not None:
        try:
            from sqlalchemy import func, select
            from config import Session
            from models import BotUser, Order, SmmOrder
            async with Session() as s:
                bu = user if (user is not None and getattr(user, "id", None)) else await s.scalar(
                    select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == uid))
                if "balance" in need_db:
                    v["balance"] = _fmt_int(bu.balance) if bu else "0"
                if "join_date" in need_db:
                    ja = getattr(bu, "joined_at", None) if bu else None
                    v["join_date"] = _jalali(ja)[0] if ja else "—"
                if "orders_count" in need_db:
                    n_smm = await s.scalar(select(func.count()).select_from(SmmOrder).where(
                        SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == uid)) or 0
                    n_shop = 0
                    if bu is not None:
                        n_shop = await s.scalar(select(func.count()).select_from(Order).where(
                            Order.bot_id == bot_id, Order.user_id == bu.id,
                            Order.status == "paid")) or 0
                    v["orders_count"] = str(int(n_smm) + int(n_shop))
                # === PATCH_PROFILE_TEMPLATE_V1 ===
                if "join_time" in need_db:
                    ja = getattr(bu, "joined_at", None) if bu else None
                    v["join_time"] = _jalali(ja)[1] if ja else "—"
                if names & {"phone", "kyc_card", "kyc_level", "kyc_level_name", "kyc_status"}:
                    lvl = int(getattr(bu, "kyc_level", 0) or 0) if bu else 0
                    v["phone"] = _esc(getattr(bu, "phone_number", None) or "—") if bu else "—"
                    v["kyc_card"] = _esc(getattr(bu, "kyc_card", None) or "—") if bu else "—"
                    v["kyc_level"] = str(lvl)
                    v["kyc_level_name"] = _KYC_LEVEL_NAMES.get(lvl, _KYC_LEVEL_NAMES[2] if lvl > 2 else "—")
                    if "kyc_status" in names:
                        from tenant import get_setting
                        if (await get_setting(bot_id, "kyc_enabled", "0")) != "1":
                            v["kyc_status"] = "—"
                        elif bu is not None and getattr(bu, "kyc_blocked", False):
                            v["kyc_status"] = "⛔ محدود"
                        elif lvl >= 1:
                            v["kyc_status"] = "✅ احرازشده"
                        else:
                            v["kyc_status"] = "❌ احراز نشده"
            if names & _PROFILE_STATS_VARS and bu is not None:
                from tenant.shared import profile_stats as _ps
                from models import Bot as _BotRow
                async with Session() as s2:
                    _brow = await s2.get(_BotRow, bot_id)
                st = await _ps.compute(bot_id, bu, is_virtual_service=bool(_brow and _brow.field == "virtual_service"))
                v["account_status"] = _ps.account_status_text(st.account_active)
                v["profile_orders"] = str(st.orders_count)
                v["smm_orders"] = str(st.smm_orders)
                v["active_services"] = str(st.active_services)
                v["total_spent"] = f"{st.total_spent:,}"
                v["ref_count"] = str(st.ref_count)
                v["ref_orders"] = str(st.inv_orders)
                v["ref_spent"] = f"{st.inv_spent:,}"
                v["ref_earned"] = f"{st.ref_total_earned:,}"
                v["ref_summary"] = _ps.ref_summary(st)          # HTMLِ قابلِ‌اعتماد؛ escape نمی‌شود
        except Exception:
            logger.exception("[text_vars] خواندنِ متغیرهایِ دیتابیسیِ کاربر ناموفق بود")
    elif "balance" in need_db and user is not None:
        v["balance"] = _fmt_int(getattr(user, "balance", 0))
    return v


async def _bot_values(names: set[str], bot_id: int | None) -> dict[str, str]:
    import datetime as _dt
    v: dict[str, str] = {}
    if "bot_username" in names and bot_id is not None:
        try:
            from config import Session
            from models import Bot as BotRow
            async with Session() as s:
                b = await s.get(BotRow, bot_id)
            v["bot_username"] = _esc(f"@{b.username}") if b and b.username else "—"
        except Exception:
            logger.exception("[text_vars] خواندنِ یوزرنیمِ ربات ناموفق بود")
            v["bot_username"] = "—"
    if names & {"date", "time"}:
        jd, jt = _jalali(_dt.datetime.utcnow())
        v["date"], v["time"] = _esc(jd), _esc(jt)
    return v


async def _order_values(names: set[str], bot_id: int | None, order) -> dict[str, str]:
    v: dict[str, str] = {}
    o = order
    qty = int(getattr(o, "quantity", 0) or 0)
    amount = float(getattr(o, "price_toman", 0) or 0)
    # === PATCH_ORDER_CODE_UNIFIED_V1 ===
    # گزارشِ فدایی: «کدِ سفارشی که در پیام می‌آید با لیستِ سفارشاتِ پنلِ مدیریت یکی نیست». {{order_id}}
    # شناسهٔ داخلیِ دیتابیس بود و لیستِ ادمین/«سفارش‌های من» کدِ پیگیری را نشان می‌دهند. حالا هر دو
    # متغیر یک عدد می‌دهند (کدِ پیگیری؛ فقط برایِ سفارش‌هایِ خیلی قدیمیِ بدونِ کد، همان شناسه).
    if "order_id" in names:
        v["order_id"] = _esc(getattr(o, "tracking_code", None) or o.id)
    if "tracking_code" in names:
        v["tracking_code"] = _esc(getattr(o, "tracking_code", None) or f"SMM-{o.id}")
    if "product_name" in names:
        v["product_name"] = _esc(getattr(o, "service_name", None) or "—")
    if "quantity" in names:
        v["quantity"] = _fmt_int(qty)
    if "amount" in names:
        v["amount"] = _fmt_int(amount)
    if "price_per_1000" in names:
        # === PATCH_ORDER_VARS_PLATFORM_V1 ===
        # گزارشِ فدایی: «قیمت هر ۱۰۰۰ عدد» غلط بود. قبلاً مبلغِ کل ÷ تعداد بود؛ با حداقلِ مبلغِ سفارش
        # یا گردکردن (مثلاً ۱۰ عدد = ۱۰۰ تومان) عدد بی‌ربط (۱۰٬۰۰۰) می‌شد. حالا نرخِ واقعیِ هر ۱۰۰۰ِ
        # همان سرویس (همان که در فروشگاه نشان داده می‌شود)؛ فقط اگر سرویس دیگر نیست، از رویِ مبلغ.
        p1000 = None
        _spk = getattr(o, "smm_service_pk", None)
        if _spk and bot_id is not None:
            try:
                from config import Session
                from models import SmmService
                async with Session() as s:
                    _svc = await s.get(SmmService, _spk)
                if _svc is not None:
                    from tenant.bots.virtual_service.smm import compute_sell_price
                    p1000 = await compute_sell_price(bot_id, _svc, 1000)
            except Exception:
                logger.exception("[text_vars] نرخِ هر ۱۰۰۰ِ سرویس خوانده نشد")
        actual = (amount / qty * 1000) if qty > 0 else None
        if p1000 and p1000 > 0:
            # مبلغِ واقعی کمتر از نرخِ فهرست یعنی تخفیف (مثلاً تخفیفِ اختصاصیِ API) → همان نرخِ پرداخت‌شده؛
            # بیشتر بودنش فقط اثرِ حداقلِ مبلغ/گردکردن است → نرخِ فهرست.
            v["price_per_1000"] = _fmt_int(min(p1000, actual) if actual else p1000)
        else:
            v["price_per_1000"] = _fmt_int(actual) if actual is not None else "—"
    if "link" in names:
        v["link"] = _esc(getattr(o, "link", "") or "—")
    if "service_id" in names:
        v["service_id"] = _esc(getattr(o, "service_id", "") or "—")
    if names & {"order_time", "order_date"}:
        ca = getattr(o, "created_at", None)
        jd, jt = _jalali(ca) if ca else ("—", "—")
        v["order_date"] = _esc(jd)
        v["order_time"] = _esc(f"{jd} — {jt}" if ca else "—")
    if "status" in names:
        st = getattr(o, "status", "") or ""
        try:
            from tenant.bots.virtual_service.smm import _SMM_STATUS_LABEL
            st = _SMM_STATUS_LABEL.get(st, st)
        except Exception:
            logger.exception("[text_vars] برچسبِ فارسیِ وضعیتِ سفارش خوانده نشد (کدِ خام نشان داده شد)")
        v["status"] = _esc(st or "—")
    # === PATCH_SMM_PARTIAL_V1 ===
    if names & {"paid_amount", "refund_amount", "final_amount", "completed_qty", "remaining_qty"}:
        st = getattr(o, "status", "") or ""
        rem = getattr(o, "remaining", None)
        ref = getattr(o, "refunded_toman", None)
        if ref is None:
            # سفارشِ کاملاً برگشت‌خورده: کلِ مبلغ؛ بقیه: صفر
            ref = amount if st in ("refunded", "canceled", "cancelled") else 0
        ref = float(ref or 0)
        if rem is None:
            rem = 0 if st == "completed" else (qty if st in ("refunded", "canceled", "cancelled") else None)
        v["paid_amount"] = _fmt_int(amount)
        v["refund_amount"] = _fmt_int(ref)
        v["final_amount"] = _fmt_int(max(0.0, amount - ref))
        v["completed_qty"] = _fmt_int(max(0, qty - int(rem))) if rem is not None else "—"
        v["remaining_qty"] = _fmt_int(rem) if rem is not None else "—"
    if "new_balance" in names:
        bal = "—"
        try:
            from sqlalchemy import select
            from config import Session
            from models import BotUser
            tg = getattr(o, "user_telegram_id", None)
            if bot_id is not None and tg is not None:
                async with Session() as s:
                    bu = await s.scalar(select(BotUser).where(
                        BotUser.bot_id == bot_id, BotUser.telegram_id == tg))
                if bu is not None:
                    bal = _fmt_int(bu.balance)
        except Exception:
            logger.exception("[text_vars] خواندنِ موجودیِ جدیدِ کاربر ناموفق بود")
        v["new_balance"] = bal
    # === PATCH_SMM_REFILL_V1 ===
    if names & {"refill_id", "refill_status"}:
        rid, rst = "—", "—"
        try:
            from sqlalchemy import select
            from config import Session
            from models import SmmRefill
            async with Session() as s:
                rf = await s.scalar(select(SmmRefill).where(SmmRefill.order_pk == o.id)
                                    .order_by(SmmRefill.id.desc()).limit(1))
            if rf is not None:
                rid = _esc(rf.panel_refill_id or "—")
                rst = {"requesting": "در حال ثبت", "pending": "در انتظار", "in_progress": "در حال انجام",
                       "completed": "انجام شد", "rejected": "رد شد", "expired": "بی‌نتیجه"}.get(rf.status, rf.status)
        except Exception:
            logger.exception("[text_vars] خواندنِ ریفیلِ سفارش ناموفق بود")
        v["refill_id"], v["refill_status"] = rid, rst
    if "category" in names:
        cat = "—"
        pk = getattr(o, "smm_service_pk", None)
        if pk:
            try:
                from config import Session
                from models import SmmCategory, SmmService
                async with Session() as s:
                    svc = await s.get(SmmService, pk)
                    if svc is not None:
                        c = await s.get(SmmCategory, svc.category_pk) if svc.category_pk else None
                        if c is not None and c.name:
                            cat = c.name
                        elif getattr(svc, "platform_key", None):
                            from tenant.bots.virtual_service import smm_api
                            cat = smm_api.platform_label(svc.platform_key)
            except Exception:
                logger.exception("[text_vars] پیداکردنِ دستهٔ محصولِ سفارش ناموفق بود")
        v["category"] = _esc(cat)
    if "platform" in names:
        plat = "—"
        pk = getattr(o, "smm_service_pk", None)
        if pk:
            try:
                from config import Session
                from models import SmmCategory, SmmService
                from tenant.bots.virtual_service import smm_api
                from tenant.bots.virtual_service.smm import _svc_platform_type
                async with Session() as s:
                    svc = await s.get(SmmService, pk)
                    c = await s.get(SmmCategory, svc.category_pk) if (svc is not None and svc.category_pk) else None
                if svc is not None:
                    key = c.platform if (c is not None and c.platform and c.platform != "other") \
                        else _svc_platform_type(svc)[0]
                    if key and key != "other":
                        lbl = smm_api.platform_label(key)
                        try:
                            from tenant.bots.virtual_service.smm import platform_label_for_bot
                            lbl = await platform_label_for_bot(bot_id, key) if bot_id is not None else lbl
                        except Exception:
                            logger.exception("[text_vars] نامِ سفارشیِ پلتفرم خوانده نشد (نامِ پیش‌فرض)")
                        plat = re.sub(r"^[^\w]+", "", lbl).strip() or lbl   # بدونِ ایموجیِ اول
            except Exception:
                logger.exception("[text_vars] پیداکردنِ پلتفرمِ سفارش ناموفق بود")
        v["platform"] = _esc(plat)
    return v


async def render_text(text: str, *, bot_id: int | None = None, user=None, tg_user=None,
                      order=None) -> str:
    """متغیرهایِ {{name}} / {name} را در text جایگزین می‌کند. هرگز exception نمی‌دهد."""
    if not text:
        return text
    try:
        names = find_names(text)
        if not names:
            return text
        values: dict[str, str] = {}
        user_names = {n for n in names if VARS[n][0] == "user"}
        bot_names = {n for n in names if VARS[n][0] == "bot"}
        order_names = {n for n in names if VARS[n][0] == "order"}
        if user_names and (user is not None or tg_user is not None):
            values.update(await _user_values(user_names, bot_id, user, tg_user))
        if bot_names:
            values.update(await _bot_values(bot_names, bot_id))
        if order_names and order is not None:
            values.update(await _order_values(order_names, bot_id, order))

        def _sub(m: re.Match) -> str:
            c = canonical(m.group(1) or m.group(2))
            # متغیرِ ناشناس یا خارج از زمینهٔ این متن (مثلاً «سفارش» در متنِ خوش‌آمد)
            # عیناً می‌ماند تا ادمین ببیند.
            return values[c] if (c and c in values) else m.group(0)

        return _PATTERN.sub(_sub, text)
    except Exception:
        logger.exception("[text_vars] رندرِ متغیرها ناموفق بود؛ متنِ خام برگردانده شد")
        return text


def variables_help_text(context: str = "all") -> str:
    """متنِ HTMLِ لیستِ کاملِ متغیرها برایِ نمایش به ادمین.
    context: "all" | "order" — در حالتِ order گروهِ سفارش هم توضیح داده می‌شود."""
    lines = ["🔤 <b>لیستِ متغیرهایِ متن‌ها</b>", "",
             "هرکدام را دقیقاً همین‌طور داخلِ متن بنویس؛ ربات موقعِ ارسال جایش مقدارِ واقعی می‌گذارد. "
             "هر دو شکلِ <code>{{name}}</code> و <code>{name}</code> کار می‌کند."]
    for gkey, gtitle in GROUPS.items():
        lines += ["", f"<b>{gtitle}</b>"]
        for name, (g, desc, example) in VARS.items():
            if g != gkey:
                continue
            lines.append(f"<code>{{{{{name}}}}}</code> — {desc}  <i>(مثلاً {_esc(example)})</i>")
    lines += ["", "ℹ️ اسم‌هایِ قدیمیِ <code>{name}</code> (نامِ کوچک)، <code>{fullname}</code> و "
                  "<code>{id}</code> (آیدیِ کاربر) همچنان کار می‌کنند.",
              "ℹ️ متغیرِ گروهِ «سفارش» فقط در متن‌هایِ وضعیتِ سفارش مقدار می‌گیرد؛ در بقیهٔ "
              "متن‌ها همان‌طور نوشته‌شده می‌ماند.",
              "ℹ️ اگر اسمی را اشتباه بنویسی، جایگزین نمی‌شود و عیناً در پیام دیده می‌شود."]
    return "\n".join(lines)
