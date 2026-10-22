# -*- coding: utf-8 -*-
"""
platform_offsite_backup.py — بک‌آپِ رمزگذاری‌شدهٔ بیرونِ سرور (PATCH_OFFSITE_BACKUP_V1).

دلیل: بک‌آپ‌هایِ خودکارِ قبلی (pg_dump هر ۶ ساعت) فقط در /opt/botsaz/backups روی همین سرور می‌ماند؛
وقتی خودِ سرور خراب/معلق شود، بک‌آپ هم با آن از دسترس می‌رود. این ماژول هر N ساعت آخرین dump (و اختیاری
فایلِ .env) را با رمزِ مدیر رمزگذاری می‌کند و به تلگرامِ مدیر می‌فرستد (تکه‌هایِ ≤ ۴۵ مگابایت).

بدونِ رمز هیچ‌چیز فرستاده نمی‌شود. بازیابی: scripts/decrypt_backup.py.
رمز را جایی جدا از سرور نگه دار؛ بدونش بک‌آپ‌ها غیرقابل‌بازگشایی‌اند.

کلیدهایِ PlatformSetting: offsite_enabled، offsite_pass، offsite_hours، offsite_env ("1": .env هم برود)،
offsite_chat (مقصدِ اختیاری؛ پیش‌فرض پی‌ویِ مدیر)، offsite_last (ISO)، offsite_last_info.
فرمان: /offsite (فقط PLATFORM_ADMIN_ID).
"""
from __future__ import annotations

import asyncio
import datetime
import glob
import html as _html
import os
import tempfile

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

import backup_crypto as BC
from config import PLATFORM_ADMIN_ID
from platform_rubika import _get, _set          # ذخیرهٔ کلید/مقدارِ پلتفرم (مشترک)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_offsite_router = Router()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
ENV_PATH = os.path.join(BASE_DIR, ".env")
PART_BYTES = 45 * 1024 * 1024
HOURS_CHOICES = (6, 12, 24, 48)
MIN_PASS = 10
STALE_LOCAL_HOURS = 12            # dumpِ محلی کهنه‌تر از این ⇒ قبل از ارسال یک dumpِ تازه می‌گیریم
K_EN, K_PASS, K_HOURS, K_ENV, K_CHAT, K_LAST, K_INFO = (
    "offsite_enabled", "offsite_pass", "offsite_hours", "offsite_env", "offsite_chat", "offsite_last",
    "offsite_last_info")

_running = asyncio.Lock()


class OffsiteFSM(StatesGroup):
    passphrase = State()
    chat = State()


def _latest_dump() -> str | None:
    files = glob.glob(os.path.join(BACKUP_DIR, "botsaz-pg-*.dump")) + glob.glob(os.path.join(BACKUP_DIR, "botsaz-*.db"))
    return max(files, key=os.path.getmtime) if files else None


async def _hours() -> int:
    try:
        v = int((await _get(K_HOURS, "24")) or 24)
    except ValueError:
        v = 24
    return v if v in HOURS_CHOICES else 24


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}"


async def _ensure_fresh_dump() -> str | None:
    """آخرین dumpِ محلی؛ اگر نبود/کهنه بود یکی تازه می‌گیرد (فقط PostgreSQL)."""
    path = _latest_dump()
    fresh = path and (datetime.datetime.now().timestamp() - os.path.getmtime(path)) < STALE_LOCAL_HOURS * 3600
    if fresh:
        return path
    try:
        import main_platform
        from config import DATABASE_URL
        if DATABASE_URL.startswith("postgresql"):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            await main_platform._pg_backup_once()
            path = _latest_dump() or path
    except Exception:
        logger.exception("offsite: گرفتنِ dumpِ تازه ناموفق بود؛ از آخرین نسخهٔ موجود استفاده می‌شود")
    return path


def _encrypt_and_split(src: str, passphrase: str, workdir: str, label: str, part_bytes: int) -> tuple[list[str], str]:
    """(تکه‌ها، sha256 کلِ فایلِ رمزشده) — هش برایِ اطمینان از سالمِ رسیدنِ همهٔ تکه‌ها."""
    enc = os.path.join(workdir, f"{label}.enc")
    BC.encrypt_file(src, enc, passphrase)
    digest = BC.sha256_of(enc)
    return BC.split_file(enc, part_bytes), digest


async def run_backup(bot, force: bool = False, part_bytes: int = PART_BYTES,
                     now: datetime.datetime | None = None) -> tuple[bool, str]:
    """یک دور: dump → رمزگذاری → تکه‌تکه → تلگرام. (موفق؟، پیامِ وضعیت). بدونِ رمز چیزی نمی‌فرستد."""
    if _running.locked():
        return False, "یک بک‌آپ همین الان در حال اجراست."
    async with _running:
        now = now or datetime.datetime.now()
        passphrase = (await _get(K_PASS)).strip()
        if len(passphrase) < MIN_PASS:
            return False, "رمزِ بک‌آپ تنظیم نشده (بدونِ رمز چیزی به بیرون فرستاده نمی‌شود)."
        dump = await _ensure_fresh_dump()
        if not dump:
            return False, "هیچ فایلِ بک‌آپِ محلی پیدا نشد."
        dest = (await _get(K_CHAT)).strip() or PLATFORM_ADMIN_ID
        try:
            dest = int(dest)
        except (TypeError, ValueError):
            pass
        stamp = now.strftime("%Y%m%d-%H%M")
        sources = [(dump, f"botsaz-{stamp}-{os.path.basename(dump).rsplit('.', 1)[-1]}")]
        if (await _get(K_ENV, "1")) == "1" and os.path.exists(ENV_PATH):
            sources.append((ENV_PATH, f"botsaz-{stamp}-env"))
        sent_parts, total_bytes = 0, 0
        with tempfile.TemporaryDirectory(prefix="offsite-") as tmp:
            for src, label in sources:
                parts, digest = await asyncio.to_thread(_encrypt_and_split, src, passphrase, tmp, label, part_bytes)
                for i, p in enumerate(parts, 1):
                    size = os.path.getsize(p)
                    cap = (f"🗄 بک‌آپِ رمزگذاری‌شده — {_html.escape(label)}\n"
                           f"تکهٔ {i} از {len(parts)} · {_mb(size)} MB\n"
                           f"SHA256 کلِ فایل: <code>{digest[:16]}…</code>\n"
                           "بازیابی: scripts/decrypt_backup.py (رمز را لازم داری)")
                    # تایم‌اوتِ پیش‌فرضِ سشنِ تلگرام ۱۲ ثانیه است (PATCH_FAST_TIMEOUT_V1)؛ آپلودِ ۴۵ مگابایتی بیشتر می‌خواهد
                    await bot.send_document(dest, FSInputFile(p), caption=cap, parse_mode="HTML", request_timeout=900)
                    sent_parts += 1
                    total_bytes += size
        info = f"{now:%Y-%m-%d %H:%M} · {sent_parts} فایل · {_mb(total_bytes)} MB"
        await _set(K_LAST, now.isoformat())
        await _set(K_INFO, info)
        return True, info


async def due(now: datetime.datetime | None = None) -> bool:
    if (await _get(K_EN, "0")) != "1":
        return False
    now = now or datetime.datetime.now()
    last = (await _get(K_LAST)).strip()
    try:
        return (not last) or now - datetime.datetime.fromisoformat(last) >= datetime.timedelta(hours=await _hours())
    except ValueError:
        return True


_last_warn = float("-inf")


async def offsite_backup_loop(platform_bot) -> None:
    """هر ۱۵ دقیقه چک می‌کند؛ موعد که رسید بک‌آپ می‌فرستد. شکست ⇒ حداکثر ساعتی یک‌بار هشدار."""
    global _last_warn
    await asyncio.sleep(120)
    while True:
        try:
            if await due():
                ok, msg = await run_backup(platform_bot)
                if not ok:
                    import time
                    if time.monotonic() - _last_warn > 3600 and PLATFORM_ADMIN_ID:
                        _last_warn = time.monotonic()
                        await platform_bot.send_message(
                            PLATFORM_ADMIN_ID, f"⚠️ بک‌آپِ بیرونی انجام نشد: {_html.escape(msg)}\nبا /offsite بررسی کن.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("offsite: خطای حلقه")
            try:
                import time
                if time.monotonic() - _last_warn > 3600 and PLATFORM_ADMIN_ID:
                    _last_warn = time.monotonic()
                    await platform_bot.send_message(PLATFORM_ADMIN_ID, "⚠️ بک‌آپِ بیرونی با خطا مواجه شد؛ لاگ را ببین.")
            except Exception:
                pass
        await asyncio.sleep(900)


# ─────────────── پنلِ مدیر ───────────────

def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    en = (await _get(K_EN, "0")) == "1"
    has_pass = len((await _get(K_PASS)).strip()) >= MIN_PASS
    hrs = await _hours()
    inc_env = (await _get(K_ENV, "1")) == "1"
    chat = (await _get(K_CHAT)).strip()
    last = (await _get(K_INFO)).strip() or "هنوز بک‌آپی نرفته"
    local = _latest_dump()
    local_txt = (f"{os.path.basename(local)} ({_mb(os.path.getsize(local))} MB)" if local else "نیست")
    text = ("🗄 <b>بک‌آپِ بیرونِ سرور (رمزگذاری‌شده)</b>\n\n"
            f"وضعیت: {'🟢 فعال' if en else '🔴 خاموش'}\n"
            f"رمز: {'✅ تنظیم شده' if has_pass else '⚠️ تنظیم نشده — بدونِ رمز چیزی نمی‌رود'}\n"
            f"هر: <b>{hrs} ساعت</b>\n"
            f"شاملِ .env: {'بله' if inc_env else 'خیر'}\n"
            f"مقصد: {_html.escape(chat) if chat else 'پی‌ویِ تلگرامِ مدیر'}\n"
            f"آخرین ارسال: {_html.escape(last)}\n"
            f"آخرین dumpِ محلی: <code>{_html.escape(local_txt)}</code>\n\n"
            "⚠️ رمز را جدا از سرور نگه دار (جایِ امن/یادداشتِ شخصی)؛ بدونِ آن بک‌آپ‌ها باز نمی‌شوند و من هم نمی‌توانم بازیابی‌شان کنم.")
    rows = [
        [InlineKeyboardButton(text="🔑 ثبت/تغییرِ رمز", callback_data="ofs:pass", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=("⛔ خاموش‌کردن" if en else "🟢 روشن‌کردن"), callback_data="ofs:toggle"),
         InlineKeyboardButton(text="🕒 تغییرِ بازه", callback_data="ofs:hours")],
        [InlineKeyboardButton(text=f"{'✅' if inc_env else '⬜️'} همراهِ .env", callback_data="ofs:env"),
         InlineKeyboardButton(text="📍 مقصد", callback_data="ofs:chat")],
        [InlineKeyboardButton(text="▶️ همین الان بفرست", callback_data="ofs:run", style=ButtonStyle.SUCCESS)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_offsite_router.message(Command("offsite"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def ofs_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_offsite_router.callback_query(F.data.startswith("ofs:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def ofs_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    if act == "pass":
        await state.set_state(OffsiteFSM.passphrase)
        await cb.message.answer(f"رمزِ بک‌آپ را بفرست (حداقل {MIN_PASS} حرف؛ پیامت بعد از ثبت پاک می‌شود).\n"
                                "این رمز را جایی جدا از سرور هم ذخیره کن. لغو: /offsite")
        return await cb.answer()
    if act == "chat":
        await state.set_state(OffsiteFSM.chat)
        await cb.message.answer("آیدیِ عددیِ چت/کانالِ مقصد را بفرست (ربات باید آنجا اجازهٔ ارسال داشته باشد)،\n"
                                "یا بنویس «پیش‌فرض» تا به پی‌ویِ خودت برود. لغو: /offsite")
        return await cb.answer()
    if act == "run":
        await cb.answer("در حال ساخت و ارسالِ بک‌آپ… (ممکن است چند دقیقه طول بکشد)", show_alert=True)
        try:
            ok, info = await run_backup(cb.bot)
        except Exception as e:
            logger.exception("offsite: اجرای دستی ناموفق")
            ok, info = False, f"{type(e).__name__}: {str(e)[:150]}"
        await cb.message.answer(("✅ بک‌آپ فرستاده شد: " if ok else "❌ انجام نشد: ") + _html.escape(info))
        text, kb = await _panel()
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        return
    if act == "toggle":
        await _set(K_EN, "0" if (await _get(K_EN, "0")) == "1" else "1")
    elif act == "hours":
        cur = await _hours()
        await _set(K_HOURS, str(HOURS_CHOICES[(HOURS_CHOICES.index(cur) + 1) % len(HOURS_CHOICES)]))
    elif act == "env":
        await _set(K_ENV, "0" if (await _get(K_ENV, "1")) == "1" else "1")
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_offsite_router.message(OffsiteFSM.passphrase, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def ofs_pass_in(msg: Message, state: FSMContext):
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    try:
        await msg.delete()           # رمز در چت نماند
    except Exception:
        pass
    if len(val) < MIN_PASS:
        return await msg.answer(f"❌ رمز باید حداقل {MIN_PASS} حرف باشد. دوباره بفرست.")
    await _set(K_PASS, val)
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ رمز ثبت شد. آن را جایی جدا از سرور هم یادداشت کن.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_offsite_router.message(OffsiteFSM.chat, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def ofs_chat_in(msg: Message, state: FSMContext):
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    if val in ("پیش‌فرض", "پیشفرض", "default"):
        await _set(K_CHAT, "")
    elif val.lstrip("-").isdigit():
        await _set(K_CHAT, val)
    else:
        return await msg.answer("❌ فقط آیدیِ عددی (مثل -1001234567890) یا «پیش‌فرض».")
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ مقصد ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
