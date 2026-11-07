"""
log_report.py — گزارشِ خوانا از خطاهای ثبت‌شده در error.log، مستقیم از داخلِ
پنلِ مدیریتِ بات‌ساز (بدونِ نیاز به SSH زدن به سرور).

چون logging_setup.py فقط سطحِ WARNING به بالا را در error.log می‌نویسد (نگاه
کن به logging_setup.py)، خودِ فایل از قبل «پیش‌فیلترشده» است — هر چیزی که
اینجا هست، واقعاً یک خطای بالقوه است، نه لاگِ عادیِ INFO.

قالبِ هر خط طبق logging_setup.py:
    2026-07-17 02:09:38 [ERROR] main: پیام خطا
    Traceback (most recent call last):
      ...
    ExceptionType: جزئیات

این ماژول:
  ۱) فایل(های) error.log را می‌خواند (شاملِ نسخه‌های چرخانده‌شده‌ی .1 و .N.gz
     طبق botsaz-logrotate.conf).
  ۲) خط‌ها را به «رخدادها» (timestamp, سطح, نامِ ماژول/بخش, پیام, traceback)
     می‌شکند.
  ۳) رخدادهای یک بازه‌ی زمانی را بر اساسِ (بخش + خطِ آخرِ traceback) گروه
     می‌کند تا خطاهای تکراری (مثلاً «bot was blocked by the user» که ممکن
     است صدها بار برای کاربرهای مختلف بیفتد) به‌جای صدها خط جدا، یک ردیفِ
     خلاصه با تعداد و آخرین زمانِ رخداد نشان داده شود.
  ۴) نتیجه را یا مستقیم در پیام (اگر کوتاه بود) یا به‌صورتِ فایلِ متنی
     (اگر طولانی بود) به مدیر می‌فرستد.
"""
from __future__ import annotations

import datetime
import glob
import gzip
import os
import re

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import PLATFORM_ADMIN_ID

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

log_report_router = Router()

_LOG_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>[A-Z]+)\] (?P<name>[\w.]+): (?P<msg>.*)$"
)


def _log_files_newest_first() -> list[str]:
    """error.log فعلی + نسخه‌های چرخانده‌شده (.1 خام، .N.gz فشرده) —
    جدیدترین اول، طبق ترتیبِ botsaz-logrotate.conf."""
    files = []
    if os.path.exists(_LOG_BASE):
        files.append(_LOG_BASE)
    p1 = _LOG_BASE + ".1"
    if os.path.exists(p1):
        files.append(p1)
    gz_files = sorted(glob.glob(_LOG_BASE + ".*.gz"))
    files.extend(gz_files)
    return files


def _iter_lines(path: str):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                yield from f
        else:
            with open(path, "rt", encoding="utf-8", errors="replace") as f:
                yield from f
    except OSError:
        return


def _parse_entries(since: datetime.datetime | None):
    """رخدادها را (جدیدترین اول) برمی‌گرداند؛ اگر since داده شود، فقط
    رخدادهایی که زمان‌شان >= since است برگردانده می‌شوند. فایل‌ها را از
    جدیدترین به قدیمی‌ترین می‌خواند و به‌محضِ رسیدن به رخدادهای قدیمی‌تر از
    since (در فایلِ قدیمی‌تر)، متوقف می‌شود — برای این‌که خواندنِ فایل‌های
    ۵۰ مگابایتی برای «فقط امروز» کندتر از حدِ لازم نشود.
    """
    entries = []
    cur = None
    for path in _log_files_newest_first():
        file_entries = []
        cur = None
        for line in _iter_lines(path):
            line = line.rstrip("\n")
            m = _LINE_RE.match(line)
            if m:
                if cur:
                    file_entries.append(cur)
                cur = {
                    "ts_raw": m.group("ts"), "level": m.group("level"),
                    "name": m.group("name"), "msg": m.group("msg"), "tb": [],
                }
            elif cur is not None and line.strip():
                cur["tb"].append(line.strip())
        if cur:
            file_entries.append(cur)
        entries.extend(file_entries)
        # اگر همین فایل قدیمی‌ترین رخدادش هنوز داخلِ بازه بود، فایلِ قبلی
        # (قدیمی‌تر) هم لازم است خوانده شود؛ وگرنه دیگر نیازی نیست.
        if since and file_entries:
            oldest_ts = _parse_ts(file_entries[-1]["ts_raw"])
            if oldest_ts and oldest_ts < since:
                break

    parsed = []
    for e in entries:
        dt = _parse_ts(e["ts_raw"])
        if dt is None:
            continue
        if since and dt < since:
            continue
        if e["level"] not in ("ERROR", "WARNING", "CRITICAL"):
            continue
        parsed.append((dt, e))
    parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed


def _parse_ts(raw: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _signature(e: dict) -> str:
    """آخرین خطِ traceback (معمولاً «ExceptionType: پیام») اگر بود، وگرنه
    خودِ پیامِ لاگ — برای گروه‌بندیِ خطاهای «همان مشکل، دفعاتِ مختلف»."""
    if e["tb"]:
        return e["tb"][-1][:200]
    return e["msg"][:200]


import html as _html


def _build_report(since: datetime.datetime | None, title: str, max_chars: int = 3500) -> str:
    parsed = _parse_entries(since)
    if not parsed:
        return f"✅ <b>{title}</b>\n\nهیچ خطا/هشداری در این بازه ثبت نشده."

    groups: dict[tuple, dict] = {}
    for dt, e in parsed:
        key = (e["name"], _signature(e))
        g = groups.get(key)
        if g is None:
            groups[key] = {"count": 1, "first": dt, "last": dt, "level": e["level"],
                            "name": e["name"], "sig": _signature(e), "msg": e["msg"]}
        else:
            g["count"] += 1
            if dt > g["last"]:
                g["last"] = dt
            if dt < g["first"]:
                g["first"] = dt

    ordered = sorted(groups.values(), key=lambda g: g["last"], reverse=True)

    header = f"🩺 <b>{title}</b>\nمجموع رخدادها: <b>{len(parsed)}</b> — دسته‌های متفاوت: <b>{len(ordered)}</b>\n\n"
    # === فیکسِ حیاتیِ دوم: قبلاً وقتی گزارش از حدِ یک پیام بلندتر می‌شد،
    # کاراکترشمارانه (text[:3300]) بریده می‌شد — دقیقاً وسطِ یه تگِ <code>
    # قطع می‌شد و تلگرام با خطای «can't find end tag» کلِ پیام رو رد می‌کرد.
    # الان به‌جایِ بریدنِ متنِ نهایی، از اول فقط تا وقتی گروه اضافه می‌کنیم که
    # جا داشته باشه — هر بلاکِ اضافه‌شده کاملاً خودکفاست (تگِ باز و بسته با
    # هم اضافه می‌شن)، پس هیچ‌وقت وسطِ یه تگ بریده نمی‌شه.
    LIMIT = 20
    reserve = 60  # جا برایِ خطِ «… و N دستهٔ دیگر» در انتها
    blocks = []
    shown = 0
    cur_len = len(header)
    for g in ordered[:LIMIT]:
        icon = "🔴" if g["level"] in ("ERROR", "CRITICAL") else "🟡"
        times = f"آخرین: {g['last'].strftime('%H:%M')}"
        if g["count"] > 1:
            times += f" — اولین: {g['first'].strftime('%m-%d %H:%M')} — {g['count']} بار"
        sig = g["sig"] if g["sig"] != g["msg"] else g["msg"]
        safe_name = _html.escape(g["name"])
        safe_sig = _html.escape(sig)
        block = f"{icon} <b>{safe_name}</b> ({times})\n<code>{safe_sig}</code>\n\n"
        if cur_len + len(block) + reserve > max_chars and shown > 0:
            break  # حداقل یه گروه رو همیشه نشون بده، حتی اگه خودش هم بزرگ باشه
        blocks.append(block)
        cur_len += len(block)
        shown += 1

    omitted = len(ordered) - shown
    text = header + "".join(blocks).rstrip()
    if omitted > 0:
        text += f"\n\n… و {omitted} دستهٔ دیگر (فایلِ کامل رو دانلود کن)."
    return text


def _report_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 امروز", callback_data="errlog:today"),
         InlineKeyboardButton(text="🟠 ۲۴ ساعت اخیر", callback_data="errlog:24h")],
        [InlineKeyboardButton(text="🟡 ۷ روز اخیر", callback_data="errlog:7d")],
        [InlineKeyboardButton(text="📎 دانلود فایل کامل", callback_data="errlog:download")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:settings")],
    ])


@log_report_router.callback_query(F.data == "errlog:menu")
async def errlog_menu(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    files = _log_files_newest_first()
    sizes = ", ".join(f"{os.path.basename(p)} ({os.path.getsize(p)//1024} کیلوبایت)" for p in files) or "—"
    await cb.message.edit_text(
        "🩺 <b>گزارش خطاهای ربات</b>\n\n"
        "این گزارش مستقیم از فایلِ لاگِ خطاهای سرور خونده می‌شه — می‌تونی یه بازه رو "
        "انتخاب کنی تا خلاصه‌ی خطاها (به‌همراهِ ساعت و اینکه از کدوم بخشِ کد بوده) رو ببینی، "
        "یا کلِ فایل رو دانلود کنی تا برای بررسیِ دقیق‌تر بفرستی.\n\n"
        f"📁 فایل‌های موجود: {sizes}",
        reply_markup=_report_menu_kb())
    await cb.answer()


async def _send_report(cb: CallbackQuery, since: datetime.datetime | None, title: str):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await cb.answer("⏳ در حال بررسی لاگ‌ها…")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="errlog:menu")]
    ])
    try:
        text = _build_report(since, title)
        if len(text) <= 3500:
            try:
                await cb.message.edit_text(text, reply_markup=back_kb)
            except Exception:
                await cb.message.answer(text, reply_markup=back_kb)
            return
        # === فیکسِ حیاتیِ سوم: قبلاً اینجا هم متنِ HTML‌دار با text[:3300]
        # کاراکترشمارانه بریده می‌شد که همون خطرِ «قطع‌شدنِ وسطِ تگ» رو داشت.
        # این حالت (یه تک‌گروه به‌تنهایی بزرگ‌تر از max_chars) خیلی نادره
        # چون _build_report خودش قبلاً اندازه رو کنترل کرده؛ ولی برای
        # اطمینانِ کامل، اینجا از یه پیامِ کاملاً امن (بدونِ HTML از رویِ
        # دادهٔ خام) استفاده می‌کنیم و متنِ کاملِ خام رو به‌صورتِ فایل می‌فرستیم.
        doc = BufferedInputFile(text.encode("utf-8"), filename="error_report.txt")
        preview = f"🩺 <b>{title}</b>\n\nگزارش طولانی‌تر از یک پیام بود؛ کاملش رو به‌صورتِ فایل فرستادم."
        try:
            await cb.message.edit_text(preview, reply_markup=back_kb)
        except Exception:
            await cb.message.answer(preview, reply_markup=back_kb)
        await cb.message.answer_document(doc, caption="📎 گزارشِ کامل")
    except Exception as e:
        # === قبلاً اگه اینجا هرجایی خطا می‌خورد (مثلاً باگِ HTML escape که
        # پایین‌تر فیکس شد)، چون cb.answer یک‌بار قبلش صدا زده شده بود، هیچ
        # پیامِ قابل‌مشاهده‌ای به ادمین نمی‌رسید — دقیقاً همون «هیچی نشون
        # نمی‌ده» که گزارش شده بود. الان به‌جایِ سکوت، خودِ خطا رو نشون می‌ده.
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        try:
            await cb.message.answer(f"❌ گزارش‌گیری ناموفق بود: {e}", reply_markup=back_kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@log_report_router.callback_query(F.data == "errlog:today")
async def errlog_today(cb: CallbackQuery):
    since = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    await _send_report(cb, since, "خطاهای امروز")


@log_report_router.callback_query(F.data == "errlog:24h")
async def errlog_24h(cb: CallbackQuery):
    since = datetime.datetime.now() - datetime.timedelta(hours=24)
    await _send_report(cb, since, "خطاهای ۲۴ ساعت اخیر")


@log_report_router.callback_query(F.data == "errlog:7d")
async def errlog_7d(cb: CallbackQuery):
    since = datetime.datetime.now() - datetime.timedelta(days=7)
    await _send_report(cb, since, "خطاهای ۷ روز اخیر")


@log_report_router.callback_query(F.data == "errlog:download")
async def errlog_download(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    files = _log_files_newest_first()
    if not files:
        await cb.answer("فایل لاگی پیدا نشد.", show_alert=True)
        return
    await cb.answer("⏳ در حال ارسال…")
    for path in files:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > 45:
            await cb.message.answer(
                f"⚠️ {os.path.basename(path)} حدود {size_mb:.0f} مگابایته و برای ارسالِ مستقیم "
                "توی تلگرام خیلی بزرگه؛ از طریقِ SSH/SFTP از سرور بگیرش.")
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            await cb.message.answer(f"❌ خواندن {os.path.basename(path)} ناموفق بود: {e}")
            continue
        doc = BufferedInputFile(data, filename=os.path.basename(path))
        await cb.message.answer_document(doc)
