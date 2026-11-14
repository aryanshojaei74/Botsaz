"""
platform_backup.py — بک‌آپِ کاملِ خودِ بات‌ساز + اعلانِ بک‌آپ به مالکِ همهٔ ربات‌ها (PATCH_PLATFORM_BACKUP_V1).

فقط ادمینِ اصلی (PLATFORM_ADMIN_ID):
  • «💾 بک‌آپِ کاملِ بات‌ساز»: همین الان یک pg_dump (یا کپیِ فایلِ SQLite) می‌گیرد و در پیوی می‌فرستد؛ فایل‌هایِ
    بزرگ‌تر از ۴۵ مگابایت تکه‌تکه می‌شوند (سقفِ آپلودِ تلگرام ۵۰ مگابایت است) — با `cat x.part* > x.dump` سرِ هم می‌شوند.
  • «📣 اعلانِ بک‌آپ به مالکان»: از طریقِ *خودِ هر ربات* برایِ مالکش پیامی با دکمهٔ «🗄 گرفتنِ بک‌آپِ کامل» می‌فرستد
    (دکمه کالبکِ همان رباتِ مالک را می‌زند: tadm:bkfull:export).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import shutil
import tempfile
from html import escape as _esc

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardMarkup)
from sqlalchemy import select

from config import DATABASE_URL, PLATFORM_ADMIN_ID, Session
from models import Bot as BotRow
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_backup_router = Router()

TG_PART = 45 * 1024 * 1024
ANNOUNCE_TEXT = (
    "🗄 <b>بک‌آپِ کاملِ رباتت را بگیر</b>\n\n"
    "برایِ اینکه اگر روزی سرور یا ربات مشکل پیدا کرد، همهٔ اطلاعاتت (کاربران، موجودیِ کیف‌پول‌ها، سفارش‌ها، "
    "تیکت‌ها، دکمه‌ها و تنظیمات) را داشته باشی، همین حالا یک بک‌آپِ کامل بگیر و فایل را جایِ امنی نگه دار. "
    "اگر ربات را از نو ساختی، همین فایل را در «💾 بک‌آپِ کامل ← 📥 بازگردانیِ کامل» بده تا همه‌چیز برگردد.\n\n"
    "⏰ می‌توانی بک‌آپِ خودکارِ هفتگی را هم روشن کنی تا فایل خودش در پیوی‌ات بیاید.\n"
    "🔐 فایل را با کسی به اشتراک نگذار.")


def is_platform_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and int(uid) == int(PLATFORM_ADMIN_ID)


def _pg_params(url: str) -> dict:
    from urllib.parse import urlparse, unquote
    scheme, rest = url.split("://", 1)
    clean = scheme.split("+", 1)[0] + "://" + rest
    p = urlparse(clean)
    return {"host": p.hostname or "localhost", "port": str(p.port or 5432),
            "user": unquote(p.username or "postgres"), "password": unquote(p.password or ""),
            "dbname": (p.path or "/").lstrip("/") or "postgres"}


async def make_dump(dir_path: str) -> str:
    """فایلِ بک‌آپ را در dir_path می‌سازد و مسیرش را برمی‌گرداند."""
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if DATABASE_URL.startswith("postgresql"):
        out = os.path.join(dir_path, f"botsaz-pg-{ts}.dump")
        prm = _pg_params(DATABASE_URL)
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "-h", prm["host"], "-p", prm["port"], "-U", prm["user"], "-d", prm["dbname"],
            "--format=custom", "--no-owner", "--no-privileges", "-f", out,
            env={**os.environ, "PGPASSWORD": prm["password"]},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump کد {proc.returncode}: {err.decode(errors='ignore')[:300]}")
        return out
    src = "botsaz.db"
    if not os.path.exists(src):
        raise RuntimeError("فایلِ دیتابیس پیدا نشد")
    out = os.path.join(dir_path, f"botsaz-{ts}.db")
    shutil.copy(src, out)
    return out


def split_file(path: str, part_size: int = TG_PART) -> list[str]:
    size = os.path.getsize(path)
    if size <= part_size:
        return [path]
    parts = []
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            p = f"{path}.part{i:03d}"
            with open(p, "wb") as o:
                o.write(chunk)
            parts.append(p)
            i += 1
    return parts


async def send_dump_to(bot: Bot, chat_id: int) -> tuple[int, int]:
    """(تعدادِ فایل‌هایِ ارسال‌شده، حجمِ کل بایت)."""
    tmp = tempfile.mkdtemp(prefix="botsaz_dump_")
    try:
        path = await make_dump(tmp)
        total = os.path.getsize(path)
        parts = split_file(path)
        for i, p in enumerate(parts, 1):
            cap = (f"💾 بک‌آپِ کاملِ دیتابیسِ بات‌ساز — {_dt.datetime.now():%Y-%m-%d %H:%M}\n"
                   f"حجم: {total / 1024 / 1024:.1f} MB")
            if len(parts) > 1:
                cap += (f"\nبخشِ {i} از {len(parts)} — سرِ هم کردن: <code>cat "
                        f"{os.path.basename(path)}.part* &gt; {os.path.basename(path)}</code>")
            await bot.send_document(chat_id, FSInputFile(p), caption=cap, parse_mode="HTML")
        return len(parts), total
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def backup_buttons():
    """ردیفِ دکمه‌هایِ بخشِ مدیریتِ ربات‌ها (برایِ platform_bot)."""
    return [hx_button("💾 بک‌آپِ کاملِ بات‌ساز", callback_data="pbk:now", style=ButtonStyle.SUCCESS),
            hx_button("📣 اعلانِ بک‌آپ به مالکان", callback_data="pbk:ann", style=ButtonStyle.PRIMARY)]


@platform_backup_router.callback_query(F.data == "pbk:now")
async def pbk_now(cb: CallbackQuery, bot: Bot):
    if not is_platform_admin(cb.from_user.id):
        await cb.answer("فقط ادمینِ اصلی", show_alert=True)
        return
    await cb.answer("⏳ در حالِ ساختنِ بک‌آپ…")
    try:
        n, total = await send_dump_to(bot, cb.from_user.id)
    except Exception as e:  # noqa: BLE001
        logger.exception("platform backup failed")
        await cb.message.answer(f"❌ بک‌آپ ناموفق بود: {_esc(str(e))[:300]}")
        return
    await cb.message.answer(f"✅ بک‌آپِ کامل فرستاده شد ({n} فایل، {total / 1024 / 1024:.1f} MB).")


async def _owner_bots() -> list:
    async with Session() as s:
        return list((await s.scalars(select(BotRow).where(
            BotRow.status == "active", BotRow.archived_at.is_(None)))).all())


@platform_backup_router.callback_query(F.data == "pbk:ann")
async def pbk_announce_ask(cb: CallbackQuery):
    if not is_platform_admin(cb.from_user.id):
        await cb.answer("فقط ادمینِ اصلی", show_alert=True)
        return
    bots = await _owner_bots()
    await cb.message.answer(
        f"📣 این پیام از طریقِ <b>خودِ هر ربات</b> برایِ مالکِ آن ({len(bots)} ربات فعال) فرستاده می‌شود، "
        "با دکمهٔ «🗄 گرفتنِ بک‌آپِ کامل»:\n\n" + ANNOUNCE_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("✅ ارسال برایِ همه", callback_data="pbk:ann:go", style=ButtonStyle.SUCCESS)],
            [hx_button("❌ انصراف", callback_data="pbk:ann:no", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@platform_backup_router.callback_query(F.data == "pbk:ann:no")
async def pbk_announce_no(cb: CallbackQuery):
    await cb.answer("انصراف شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("pbk: پاک‌کردنِ کیبورد ممکن نشد (بی‌اهمیت)")


def owner_kb() -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗄 گرفتنِ بک‌آپِ کامل", callback_data="tadm:bkfull:export", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⏰ روشن‌کردنِ بک‌آپِ خودکارِ هفتگی", callback_data="tadm:bkfull:auto7",
                              style=ButtonStyle.PRIMARY)],
    ])


async def announce_to_owners(progress=None) -> dict:
    """پیامِ بک‌آپ را از طریقِ هر ربات برایِ مالکش می‌فرستد. {sent, failed, total}."""
    bots = await _owner_bots()
    sent = failed = 0
    for i, b in enumerate(bots, 1):
        tb = Bot(token=b.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tb.send_message(b.owner_id, ANNOUNCE_TEXT, reply_markup=owner_kb())
            sent += 1
        except Exception:  # noqa: BLE001  (مالک ربات را استارت نکرده/بلاک کرده/توکنِ خراب)
            failed += 1
            logger.info("اعلانِ بک‌آپ برایِ رباتِ %s ارسال نشد", b.id)
        finally:
            try:
                await tb.session.close()
            except Exception:  # noqa: BLE001
                logger.debug("بستنِ سشن ناموفق بود")
        if progress and i % 25 == 0:
            await progress(i, len(bots), sent, failed)
        await asyncio.sleep(0.05)
    return {"sent": sent, "failed": failed, "total": len(bots)}


@platform_backup_router.callback_query(F.data == "pbk:ann:go")
async def pbk_announce_go(cb: CallbackQuery):
    if not is_platform_admin(cb.from_user.id):
        await cb.answer("فقط ادمینِ اصلی", show_alert=True)
        return
    await cb.answer("⏳ در حالِ ارسال…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("pbk: پاک‌کردنِ کیبورد ممکن نشد (بی‌اهمیت)")
    status = await cb.message.answer("⏳ ارسال شروع شد…")

    async def _progress(i, n, sent, failed):
        try:
            await status.edit_text(f"⏳ {i}/{n} — موفق: {sent} | ناموفق: {failed}")
        except Exception:
            logger.debug("pbk: به‌روزرسانیِ پیشرفت ممکن نشد")
    res = await announce_to_owners(_progress)
    await status.edit_text(
        f"✅ پایان — از {res['total']} ربات، پیام برایِ {res['sent']} مالک رفت؛ {res['failed']} مورد ناموفق "
        "(مالک رباتش را استارت نکرده یا بلاک کرده).")
