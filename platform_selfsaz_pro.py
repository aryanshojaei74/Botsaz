"""SelfSaz — لایهٔ «حرفه‌ای»: پلن‌های چندماهه، قفل ضد دابل‌تپ، قوانین، یادآور انقضا،
هشدار قطعی سرویس و آمار مدیر.

منطق خالص (قیمت پلن، دسته‌بندی یادآور، آمار) جدا از تلگرام نگه داشته شده تا تست‌پذیر باشد.
سرویس مستقل /opt/selfsaz فقط از طریق selfsaz_api (HTTP) صدا زده می‌شود.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# ماه → روزِ اعتبار (سقف سرویس ۳۶۶ روز است)
PLAN_DAYS = {1: 30, 3: 90, 6: 180, 12: 365}
DEFAULT_DISCOUNT = {1: 0, 3: 5, 6: 10, 12: 15}
DISC_KEY = "selfsaz:disc"
TOS_KEY = "selfsaz:tos:{uid}"
REM_KEY = "selfsaz:rem:{aid}:{exp}:{tag}"
ERR_KEY = "selfsaz:err:{aid}"

CHECK_EVERY = 300          # ثانیه
DOWN_AFTER_FAILS = 3       # چند بررسیِ پیاپیِ ناموفق تا هشدار قطعی


# ───────────── قیمت پلن ─────────────
def parse_discounts(raw: str | None) -> dict[int, int]:
    """«3:5,6:10,12:15» یا JSON → {ماه: درصد}. ورودی خراب ⇒ پیش‌فرض."""
    out = dict(DEFAULT_DISCOUNT)
    if not raw:
        return out
    try:
        data = json.loads(raw) if raw.strip().startswith("{") else dict(
            part.split(":", 1) for part in raw.replace("،", ",").split(",") if ":" in part)
        for k, v in data.items():
            m, p = int(k), int(v)
            if m in PLAN_DAYS and 0 <= p <= 90:
                out[m] = p
    except Exception:
        return dict(DEFAULT_DISCOUNT)
    out[1] = 0
    return out


def plan_price(base: int, months: int, discount_pct: int = 0) -> int:
    """قیمت کل n ماه با درصد تخفیف؛ به نزدیک‌ترین تومان گرد می‌شود."""
    base = max(0, int(base))
    pct = min(90, max(0, int(discount_pct)))
    return int(round(base * months * (100 - pct) / 100.0))


def plan_options(base: int, discounts: dict[int, int]) -> list[dict]:
    rows = []
    for months, days in PLAN_DAYS.items():
        pct = discounts.get(months, 0)
        rows.append({"months": months, "days": days, "discount": pct,
                     "price": plan_price(base, months, pct),
                     "full": plan_price(base, months, 0)})
    return rows


# ───────────── قفل ضد دابل‌تپ ─────────────
_BUSY: set[int] = set()


def try_acquire(uid: int) -> bool:
    if uid in _BUSY:
        return False
    _BUSY.add(uid)
    return True


def release(uid: int) -> None:
    _BUSY.discard(uid)


# ───────────── قوانین استفاده ─────────────
TOS_TEXT = (
    "📜 <b>قوانین و مسئولیت استفاده از سلف‌ساز</b>\n\n"
    "• سلف‌ساز روی <b>حساب تلگرام خودِ شما</b> اجرا می‌شود؛ مسئولیت هر پیام، حذف، "
    "بن/سکوت و دستوری که ارسال شود با شماست.\n"
    "• ارسال انبوه، اسپم، مزاحمت و توهین به دیگران ممنوع است و می‌تواند باعث "
    "محدودیت یا مسدود شدن حساب شما توسط خودِ تلگرام شود؛ این محدودیت‌ها از سمت ما نیست.\n"
    "• ما سشن حساب را فقط برای اجرای همین سرویس نگه می‌داریم. هر زمان با «حذف سلف / خروج "
    "از حساب» سشن از سرویس پاک می‌شود. برای امنیت بیشتر می‌توانید از تنظیمات تلگرام "
    "(Devices) نشستِ سلف را هم ببندید.\n"
    "• هزینهٔ اشتراک پس از فعال‌سازی موفق بازگردانده نمی‌شود، مگر فعال‌سازی ناموفق باشد "
    "(در آن حالت خودکار به کیف پول برمی‌گردد).\n\n"
    "با ادامه، این موارد را می‌پذیرید."
)


async def tos_accepted(uid: int) -> bool:
    from platform_bot import get_pset
    return (await get_pset(TOS_KEY.format(uid=uid), "")) == "1"


async def accept_tos(uid: int) -> None:
    from platform_bot import set_pset
    await set_pset(TOS_KEY.format(uid=uid), "1")


# ───────────── پیام‌های خطا ─────────────
def friendly_error(raw: str | None) -> str:
    """جزئیات داخلی سرویس (مسیر، stack، آدرس) هرگز به کاربر نمایش داده نمی‌شود."""
    s = str(raw or "").lower()
    if "trial already used" in s:
        return "دورهٔ آزمایشی قبلاً استفاده شده است"
    if "not found" in s:
        return "حساب سلف‌ساز پیدا نشد"
    if "disabled" in s or "غیرفعال" in s:
        return "سلف‌ساز موقتاً غیرفعال است"
    if "api" in s and ("id" in s or "hash" in s):
        return "سرویس هنوز کامل پیکربندی نشده؛ با پشتیبانی تماس بگیرید"
    return "سرویس سلف‌ساز موقتاً در دسترس نیست؛ چند دقیقهٔ دیگر دوباره تلاش کنید"


# ───────────── زمان‌ها و دسته‌بندی یادآور ─────────────
def parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        v = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def reminder_tag(expires_at, now: datetime) -> str | None:
    """'3d' (≤۷۲ساعت)، '1d' (≤۲۴ساعت)، 'exp' (تا ۴۸ساعت پس از انقضا) یا None."""
    exp = parse_dt(expires_at)
    if not exp:
        return None
    left = exp - now
    if left > timedelta(hours=72):
        return None
    if left > timedelta(hours=24):
        return "3d"
    if left > timedelta(0):
        return "1d"
    if -left <= timedelta(hours=48):
        return "exp"
    return None


REMINDER_TEXT = {
    "3d": "⏰ اشتراک سلف‌ساز شما کمتر از ۳ روز دیگر تمام می‌شود.",
    "1d": "⏰ اشتراک سلف‌ساز شما کمتر از ۲۴ ساعت دیگر تمام می‌شود.",
    "exp": "⛔️ اشتراک سلف‌ساز شما تمام شد و سلف متوقف شده است.",
}


def compute_stats(accounts: list[dict], now: datetime) -> dict:
    total = active = expired = errors = connected_idle = expiring7 = trials = 0
    for a in accounts:
        total += 1
        exp = parse_dt(a.get("expires_at"))
        is_active = bool(exp and exp > now)
        if is_active:
            active += 1
            if exp - now <= timedelta(days=7):
                expiring7 += 1
        elif exp:
            expired += 1
        else:
            connected_idle += 1
        if str(a.get("status")) == "error":
            errors += 1
        if int(a.get("trial_used") or 0) and not is_active:
            trials += 1
    return {"total": total, "active": active, "expired": expired, "errors": errors,
            "never_paid": connected_idle, "expiring7": expiring7, "trial_ended": trials}


def stats_text(st: dict, revenue_30d: int, revenue_all: int, count_30d: int) -> str:
    return (
        "📊 <b>آمار سلف‌ساز</b>\n\n"
        f"👥 کل حساب‌ها: <b>{st['total']}</b>\n"
        f"🟢 اشتراک فعال: <b>{st['active']}</b>\n"
        f"⏳ تا ۷ روز دیگر تمام می‌شود: <b>{st['expiring7']}</b>\n"
        f"🔴 منقضی: <b>{st['expired']}</b>\n"
        f"🕳 هنوز اشتراک نخریده: <b>{st['never_paid']}</b>\n"
        f"🎁 تست تمام‌شده بدون خرید: <b>{st['trial_ended']}</b>\n"
        f"⚠️ در وضعیت خطا: <b>{st['errors']}</b>\n\n"
        f"💰 فروش ۳۰ روز اخیر: <b>{revenue_30d:,}</b> تومان ({count_30d} خرید)\n"
        f"💰 فروش کل: <b>{revenue_all:,}</b> تومان"
    )


async def revenue(now: datetime | None = None) -> tuple[int, int, int]:
    """(۳۰ روز، کل، تعداد ۳۰ روز) از PlatformTxn؛ بازپرداخت‌ها کسر می‌شوند."""
    from sqlalchemy import select
    from config import Session
    from models import PlatformTxn
    now = now or datetime.utcnow()
    since = now - timedelta(days=30)
    total = last = cnt = 0
    async with Session() as s:
        rows = (await s.execute(select(PlatformTxn).where(
            PlatformTxn.note.like("%سلف‌ساز%")))).scalars().all()
    for t in rows:
        amt = int(float(t.amount or 0))
        if t.kind == "purchase":
            total += -amt
            ts = getattr(t, "created_at", None)
            if ts is not None and ts.replace(tzinfo=None) >= since.replace(tzinfo=None):
                last += -amt
                cnt += 1
        elif t.kind == "refund":
            total -= amt
            ts = getattr(t, "created_at", None)
            if ts is not None and ts.replace(tzinfo=None) >= since.replace(tzinfo=None):
                last -= amt
    return max(0, last), max(0, total), cnt


# ───────────── حلقهٔ نگهبان ─────────────
_state = {"fails": 0, "down_alerted": False, "sup_alerted": False}
SUPERVISOR_STALE_SEC = 180


async def _send(bot, uid: int, text: str, kb=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception as e:  # کاربر بات را بلاک کرده و…
        logger.info("selfsaz notify %s failed: %s", uid, e)
        return False


def _renew_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 تمدید سلف‌ساز", callback_data="pss:status")]])


async def watch_once(bot, api, admin_id: int, now: datetime | None = None) -> dict:
    """یک دور بررسی: قطعی سرویس، یادآور انقضا، تغییر وضعیت به error."""
    from platform_bot import get_pset, set_pset
    now = now or datetime.now(timezone.utc)
    res = await api("GET", "/v1/admin/accounts")
    out = {"down": False, "recovered": False, "reminders": 0, "errors": 0}
    if res is None:
        _state["fails"] += 1
        if _state["fails"] >= DOWN_AFTER_FAILS and not _state["down_alerted"]:
            _state["down_alerted"] = True
            out["down"] = True
            await _send(bot, admin_id,
                        "🚨 <b>سرویس سلف‌ساز پاسخ نمی‌دهد</b>\n"
                        f"{_state['fails']} بررسی پیاپی ناموفق بود. وضعیت را روی سرور چک کنید:\n"
                        "<code>systemctl status selfsaz-api selfsaz-supervisor</code>")
        return out
    if _state["down_alerted"]:
        _state["down_alerted"] = False
        out["recovered"] = True
        await _send(bot, admin_id, "✅ سرویس سلف‌ساز دوباره در دسترس است.")
    _state["fails"] = 0

    health = await api("GET", "/health") or {}
    age = health.get("supervisor_age")          # فقط سرویسِ سخت‌شده این فیلد را دارد
    if isinstance(age, (int, float)) and age > SUPERVISOR_STALE_SEC:
        if not _state["sup_alerted"]:
            _state["sup_alerted"] = True
            out["supervisor_stale"] = True
            await _send(bot, admin_id,
                        "🚨 <b>supervisor سلف‌ساز زنده نیست</b>\n"
                        f"آخرین heartbeat {int(age)} ثانیه پیش بود؛ workerها دیگر مدیریت نمی‌شوند.\n"
                        "<code>systemctl restart selfsaz-worker</code>")
    elif _state["sup_alerted"]:
        _state["sup_alerted"] = False
        await _send(bot, admin_id, "✅ supervisor سلف‌ساز دوباره فعال است.")

    for a in res.get("accounts", []):
        aid, owner = a.get("id"), a.get("owner_id")
        if not aid or not owner:
            continue
        tag = reminder_tag(a.get("expires_at"), now)
        if tag:
            key = REM_KEY.format(aid=aid, exp=a.get("expires_at"), tag=tag)
            if (await get_pset(key, "")) != "1":
                await set_pset(key, "1")          # اول علامت می‌زنیم؛ هیچ‌وقت دوبار نفرستد
                if await _send(bot, int(owner), REMINDER_TEXT[tag], _renew_kb()):
                    out["reminders"] += 1
        ekey = ERR_KEY.format(aid=aid)
        status = str(a.get("status") or "")
        prev = await get_pset(ekey, "")
        if status == "error" and prev != "error":
            await set_pset(ekey, "error")
            out["errors"] += 1
            await _send(bot, int(owner),
                        "⚠️ سلف شما به مشکل خورد و متوقف شد (معمولاً به‌خاطر باطل شدن نشست تلگرام). "
                        "از پنل سلف‌ساز «اتصال دوباره حساب تلگرام» را بزنید.")
            await _send(bot, admin_id, f"⚠️ سلف‌ساز: حساب <code>{owner}</code> در وضعیت خطا رفت.")
        elif status != "error" and prev == "error":
            await set_pset(ekey, "")
    return out


async def selfsaz_watch_loop(platform_bot=None):
    from config import PLATFORM_ADMIN_ID
    from platform_selfsaz import selfsaz_api
    await asyncio.sleep(30)
    while True:
        try:
            if platform_bot is not None:
                await watch_once(platform_bot, selfsaz_api, PLATFORM_ADMIN_ID)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("selfsaz_watch_loop")
        await asyncio.sleep(CHECK_EVERY)
