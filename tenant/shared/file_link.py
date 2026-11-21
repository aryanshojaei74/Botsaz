"""
file_link.py — ربات‌هایِ نوعِ «تبدیل فایل↔لینک» (bot_field == "file_link").

دو کارِ اصلی:
  ۱) هر فایلی که کاربر به ربات بفرسته (سند/عکس/ویدیو/صدا/گیف) رویِ دیسکِ
     سرور ذخیره می‌شه و یک لینکِ دانلودِ مستقیمِ HTTP بهش برمی‌گرده —
     لینکی که با هر مرورگری (بدونِ نیاز به تلگرام) باز می‌شه و فایل
     دانلود می‌شه.
  ۲) هر لینکی که کاربر بفرسته (چه لینکِ خودِ همین ربات، چه هر لینکِ
     دیگه‌ای رویِ اینترنت) دانلود می‌شه و به‌عنوانِ فایل توی همون چت
     تحویل داده می‌شه.

این ماژول دو Router جدا داره:
  • file_link_router      → روترِ aiogram، باید داخلِ tenant_router
    include بشه (نگاه کن به پایینِ tenant/__init__.py).
  • file_link_web_router   → روترِ FastAPI، باید داخلِ app (main_tenant.py)
    include بشه — همون‌جایی که web_router/pay_router هم include شدن.
"""
from __future__ import annotations

import mimetypes
import os
import secrets
from pathlib import Path

import aiohttp
from aiogram import Bot, F, Router
from aiogram.types import FSInputFile, Message
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func as sa_func, select

from config import FILE_LINK_STORAGE_DIR, Session, WEBHOOK_BASE
from models import Bot as BotRow, FileLink
from plans import MAX_SINGLE_FILE_MB, Plan, STORAGE_QUOTA_MB

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

file_link_router = Router()
file_link_web_router = APIRouter()

_MB = 1024 * 1024
# دانلودِ لینکِ خارجی برای فرستادن‌اش به‌عنوانِ فایلِ تلگرام سقفِ خودِ
# API تلگرام رو داره (آپلودِ معمولیِ بات: ~۵۰ مگابایت)؛ محافظه‌کارانه‌تر
# نگه‌ش می‌داریم تا کاربر یهو با یه دانلودِ خیلی بزرگ سرور رو قفل نکنه.
_MAX_EXTERNAL_DOWNLOAD_MB = 45


async def _get_bot_row_by_token(token: str) -> "BotRow | None":
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == token))


async def _bot_field_is_file_link(message: Message, bot: Bot) -> bool:
    row = await _get_bot_row_by_token(bot.token)
    return bool(row and row.field == "file_link")


def _bot_storage_dir(bot_id: int) -> Path:
    p = Path(FILE_LINK_STORAGE_DIR) / str(bot_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _gen_unique_code(s) -> str:
    for _ in range(6):
        code = secrets.token_urlsafe(7).replace("_", "").replace("-", "")[:9]
        clash = await s.scalar(select(FileLink.id).where(FileLink.code == code))
        if not clash:
            return code
    # عملاً غیرممکنه به اینجا برسیم
    return secrets.token_urlsafe(12)


async def _storage_used_mb(s, bot_id: int) -> float:
    total = await s.scalar(
        select(sa_func.coalesce(sa_func.sum(FileLink.size_bytes), 0))
        .where(FileLink.bot_id == bot_id)
    )
    return (total or 0) / _MB


def _download_link(code: str) -> str:
    base = WEBHOOK_BASE.rstrip("/") if WEBHOOK_BASE else ""
    return f"{base}/f/{code}"


# ══════════════════════ فایل → لینک ══════════════════════

@file_link_router.message(
    F.document | F.photo | F.video | F.audio | F.voice | F.animation,
    _bot_field_is_file_link,
)
async def upload_file(message: Message, bot: Bot):
    row = await _get_bot_row_by_token(bot.token)
    if not row:
        return

    if message.document:
        tg_file, orig_name = message.document, (message.document.file_name or "file")
    elif message.video:
        tg_file, orig_name = message.video, (message.video.file_name or "video.mp4")
    elif message.audio:
        tg_file, orig_name = message.audio, (message.audio.file_name or "audio.mp3")
    elif message.voice:
        tg_file, orig_name = message.voice, "voice.ogg"
    elif message.animation:
        tg_file, orig_name = message.animation, (message.animation.file_name or "animation.mp4")
    else:  # عکس — بزرگ‌ترین سایز
        tg_file, orig_name = message.photo[-1], "photo.jpg"

    size_mb = (tg_file.file_size or 0) / _MB
    if size_mb > MAX_SINGLE_FILE_MB:
        await message.reply(
            f"❌ حجمِ این فایل ({size_mb:.1f} مگابایت) از سقفِ مجازِ هر فایل "
            f"({MAX_SINGLE_FILE_MB} مگابایت) بیشتره."
        )
        return

    try:
        plan = Plan(row.plan)
    except ValueError:
        plan = Plan.FREE
    quota_mb = STORAGE_QUOTA_MB.get(plan, STORAGE_QUOTA_MB[Plan.FREE])

    async with Session() as s:
        used_mb = await _storage_used_mb(s, row.id)
        if used_mb + size_mb > quota_mb:
            await message.reply(
                f"❌ فضایِ ذخیره‌سازیِ این ربات پر شده "
                f"({used_mb:.0f}/{quota_mb} مگابایت طبقِ پلنِ فعلی).\n"
                f"برایِ ادامه، یا فایل‌هایِ قدیمی رو حذف کن یا پلنِ ربات رو ارتقا بده."
            )
            return

        code = await _gen_unique_code(s)
        ext = Path(orig_name).suffix
        dest_dir = _bot_storage_dir(row.id)
        dest_path = dest_dir / f"{code}{ext}"

        wait_msg = await message.reply("⏳ در حالِ ذخیره‌سازیِ فایل...")
        try:
            await bot.download(tg_file.file_id, destination=dest_path)
        except Exception:
            logger.exception("خطا در دانلود/ذخیرهٔ فایلِ کاربر (file_link)")
            await wait_msg.edit_text("❌ در ذخیره‌سازیِ فایل خطایی پیش اومد؛ دوباره امتحان کن.")
            return

        actual_size = dest_path.stat().st_size if dest_path.exists() else (tg_file.file_size or 0)
        mime_type = mimetypes.guess_type(orig_name)[0]

        s.add(FileLink(
            bot_id=row.id,
            owner_telegram_id=message.from_user.id,
            code=code,
            orig_filename=orig_name,
            disk_path=str(dest_path),
            mime_type=mime_type,
            size_bytes=actual_size,
        ))
        await s.commit()

    link = _download_link(code)
    if WEBHOOK_BASE:
        text = (
            f"✅ فایل با موفقیت ذخیره شد.\n\n"
            f"🔗 لینکِ دانلودِ مستقیم:\n{link}\n\n"
            f"📄 نام: <code>{orig_name}</code>\n"
            f"📦 حجم: {actual_size / _MB:.2f} مگابایت"
        )
    else:
        # === اگه WEBHOOK_BASE تنظیم نشده باشه، لینکِ دانلودِ عمومی کار نمی‌کنه ===
        text = (
            "⚠️ فایل رویِ سرور ذخیره شد، ولی چون آدرسِ عمومیِ سرور "
            "(WEBHOOK_BASE) تنظیم نشده، لینکِ دانلود ساخته نمی‌شه.\n"
            "این مقدار رو در تنظیماتِ سرور ست کن تا لینک‌ها فعال بشن.\n"
            f"کدِ فایل: <code>{code}</code>"
        )
    try:
        await wait_msg.edit_text(text, disable_web_page_preview=True)
    except Exception:
        await message.answer(text, disable_web_page_preview=True)


# ══════════════════════ لینک → فایل ══════════════════════

@file_link_router.message(F.text.regexp(r"^https?://\S+$"), _bot_field_is_file_link)
async def link_to_file(message: Message, bot: Bot):
    row = await _get_bot_row_by_token(bot.token)
    if not row:
        return
    url = message.text.strip()

    # اگه خودِ همین لینکِ ماست (WEBHOOK_BASE + /f/code)، بدونِ دانلودِ
    # دوباره، مستقیم از دیسک برایِ کاربر می‌فرستیم.
    if WEBHOOK_BASE and url.startswith(f"{WEBHOOK_BASE.rstrip('/')}/f/"):
        code = url.rsplit("/", 1)[-1]
        async with Session() as s:
            fl = await s.scalar(select(FileLink).where(FileLink.code == code, FileLink.bot_id == row.id))
        if not fl or not os.path.exists(fl.disk_path):
            await message.reply("❌ فایلی با این لینک پیدا نشد.")
            return
        try:
            await message.answer_document(FSInputFile(fl.disk_path, filename=fl.orig_filename))
        except Exception:
            logger.exception("خطا در ارسالِ فایلِ محلی (file_link)")
            await message.reply("❌ در ارسالِ فایل خطایی پیش اومد.")
        return

    wait_msg = await message.reply("⏳ در حالِ دانلودِ لینک...")
    tmp_path = _bot_storage_dir(row.id) / f"_tmp_{secrets.token_hex(6)}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    await wait_msg.edit_text(f"❌ دانلود ناموفق بود (کد {resp.status}).")
                    return
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > _MAX_EXTERNAL_DOWNLOAD_MB * _MB:
                    await wait_msg.edit_text(
                        f"❌ حجمِ فایلِ این لینک از سقفِ مجاز "
                        f"({_MAX_EXTERNAL_DOWNLOAD_MB} مگابایت) بیشتره."
                    )
                    return
                written = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        written += len(chunk)
                        if written > _MAX_EXTERNAL_DOWNLOAD_MB * _MB:
                            await wait_msg.edit_text(
                                f"❌ حجمِ فایلِ این لینک از سقفِ مجاز "
                                f"({_MAX_EXTERNAL_DOWNLOAD_MB} مگابایت) بیشتره."
                            )
                            return
                        f.write(chunk)

                cd = resp.headers.get("Content-Disposition", "")
                name = None
                if "filename=" in cd:
                    name = cd.split("filename=")[-1].strip('"; ')
                if not name:
                    name = url.rsplit("/", 1)[-1].split("?")[0] or "downloaded_file"

        await wait_msg.edit_text("⏳ در حالِ ارسالِ فایل...")
        await message.answer_document(FSInputFile(tmp_path, filename=name))
        try:
            await wait_msg.delete()
        except Exception:
            logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    except Exception:
        logger.exception("خطا در دانلود/ارسالِ لینکِ خارجی (file_link)")
        try:
            await wait_msg.edit_text("❌ در دانلود یا ارسالِ این لینک خطایی پیش اومد.")
        except Exception:
            logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# ══════════════════════ دانلودِ HTTP مستقیم ══════════════════════

@file_link_web_router.get("/f/{code}")
async def download_file(code: str):
    async with Session() as s:
        row = await s.scalar(select(FileLink).where(FileLink.code == code))
        if not row or not os.path.exists(row.disk_path):
            return JSONResponse({"error": "فایل پیدا نشد"}, status_code=404)
        row.download_count += 1
        await s.commit()
        disk_path, orig_filename, mime_type = row.disk_path, row.orig_filename, row.mime_type

    return FileResponse(
        disk_path,
        filename=orig_filename,
        media_type=mime_type or "application/octet-stream",
    )
