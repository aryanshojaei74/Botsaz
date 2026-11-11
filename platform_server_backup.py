# -*- coding: utf-8 -*-
"""
platform_server_backup.py — «📦 بک‌آپِ انتقالِ سرور» (PATCH_SERVER_BACKUP_V1).

یک فایلِ زیپِ رمزگذاری‌شده با هر چیزی که برایِ بالا آوردنِ بات‌ساز روی یک سرورِ تازه لازم است:
  db/       dumpِ دیتابیس (کاربران، کیف‌پول‌ها، سفارش‌ها، ربات‌ها، همه‌چیز)
  app/      فایل‌هایِ پوشهٔ پروژه (/opt/botsaz): کد، assets، فایل‌هایِ داده، .env (اگر روشن باشد)
  system/   سرویس‌هایِ systemd و تنظیماتِ nginx (conf.d، sites-enabled، نگاشتِ مینی‌اپ‌ها)
  restore.sh + RESTORE.txt   راهنما و اسکریپتِ بازگردانی

ارسال به تلگرامِ مدیر؛ زیپ با «رمزِ بک‌آپِ بیرونی» (همان رمزِ /offsite) رمزگذاری می‌شود (چون .env توکن و کلید دارد)؛
بدونِ رمز چیزی فرستاده نمی‌شود. تکه‌ها ≤ ۴۵ مگابایت. بازگشایی: scripts/decrypt_backup.py.

تنظیماتِ PlatformSetting: svb_enabled، svb_hours (۱..۷۲۰ ساعت)، svb_env ("1": .env هم برود)، svb_chat،
svb_last، svb_info. رمز: offsite_pass (مشترک با /offsite).
"""
from __future__ import annotations

import asyncio
import datetime
import glob
import html as _html
import os
import shutil
import tempfile
import zipfile

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

import backup_crypto as BC
from config import PLATFORM_ADMIN_ID
from platform_rubika import _get, _set

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_server_backup_router = Router()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PART_BYTES = 45 * 1024 * 1024
MIN_PASS = 10
MAX_FILE_BYTES = 300 * 1024 * 1024
MAX_TOTAL_BYTES = 1500 * 1024 * 1024
HOURS_PRESETS = (6, 12, 24, 48, 72, 168)
K_EN, K_HOURS, K_ENV, K_CHAT, K_LAST, K_INFO = "svb_enabled", "svb_hours", "svb_env", "svb_chat", "svb_last", "svb_info"
K_PASS = "offsite_pass"

EXCLUDE_DIRS = {"__pycache__", ".git", ".github", "backups", "venv", ".venv", "node_modules", "tests",
                ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDE_SUFFIX = (".pyc", ".log", ".db-wal", ".db-shm", ".tmp", ".swp")
EXCLUDE_NAMES = {"error.log", "perf_out.txt", "botsaz.db"}
SYSTEM_GLOBS = (
    "/etc/systemd/system/botsaz-*.service",
    "/etc/systemd/system/botsaz-*.timer",
    "/etc/nginx/conf.d/*.conf",
    "/etc/nginx/sites-enabled/*",
    "/etc/nginx/miniapp-hosts.d/*.map",
    "/etc/nginx/miniapp-game.d/*.map",
)

_running = asyncio.Lock()


class SvbFSM(StatesGroup):
    hours = State()
    chat = State()


RESTORE_SH = r"""#!/usr/bin/env bash
set -euo pipefail

ROOT="${BOTSAZ_ROOT:-/opt/botsaz}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = "0" ] || { echo "run as root"; exit 1; }
[ -d "$HERE/app" ] || { echo "app/ folder not found next to this script"; exit 1; }

echo "== 1/6 system packages"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip postgresql postgresql-client nginx rsync unzip
fi

echo "== 2/6 project files -> $ROOT"
mkdir -p "$ROOT"
rsync -a "$HERE/app/" "$ROOT/"

echo "== 3/6 python environment"
if [ ! -x "$ROOT/venv/bin/python" ]; then python3 -m venv "$ROOT/venv"; fi
"$ROOT/venv/bin/pip" install -U pip
"$ROOT/venv/bin/pip" install -r "$ROOT/requirements.txt"

echo "== 4/6 database"
DUMP="$(ls "$HERE"/db/*.dump 2>/dev/null | head -n1 || true)"
SQLITE="$(ls "$HERE"/db/*.db 2>/dev/null | head -n1 || true)"
if [ -n "$DUMP" ]; then
  [ -f "$ROOT/.env" ] || { echo ".env missing in backup; create $ROOT/.env with DATABASE_URL then run again"; exit 1; }
  DB_URL="$(grep -E '^DATABASE_URL=' "$ROOT/.env" | head -n1 | cut -d= -f2- | tr -d '"' | sed -E 's#^(postgres(ql)?)(\+[a-z0-9]+)?://#postgresql://#')"
  echo "database: ${DB_URL%%@*}@..."
  read -r -p "create the database role/db from .env and restore the dump? [y/N] " ans
  if [ "$ans" = "y" ]; then
    USER_="$(printf '%s' "$DB_URL" | sed -E 's#^postgresql://([^:]+):.*#\1#')"
    PASS_="$(printf '%s' "$DB_URL" | sed -E 's#^postgresql://[^:]+:([^@]+)@.*#\1#')"
    NAME_="$(printf '%s' "$DB_URL" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"
    su postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='$USER_'\"" | grep -q 1 || su postgres -c "psql -c \"CREATE ROLE $USER_ LOGIN PASSWORD '$PASS_'\""
    su postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='$NAME_'\"" | grep -q 1 || su postgres -c "createdb -O $USER_ $NAME_"
    pg_restore --no-owner --no-privileges --clean --if-exists -d "$DB_URL" "$DUMP" || echo "pg_restore reported warnings (often harmless); check the data"
  fi
elif [ -n "$SQLITE" ]; then
  cp -f "$SQLITE" "$ROOT/botsaz.db"
fi

echo "== 5/6 services and nginx"
if [ -d "$HERE/system/etc/systemd/system" ]; then cp -f "$HERE"/system/etc/systemd/system/* /etc/systemd/system/; fi
if [ -d "$HERE/system/etc/nginx" ]; then rsync -a "$HERE/system/etc/nginx/" /etc/nginx/; fi
systemctl daemon-reload
nginx -t || echo "nginx config test failed: fix it (SSL certificate files may need to be copied) before reloading"

echo "== 6/6 start"
read -r -p "enable and start all botsaz services now? [y/N] " ans2
if [ "$ans2" = "y" ]; then
  for u in /etc/systemd/system/botsaz-*.service; do systemctl enable --now "$(basename "$u")" || true; done
  systemctl reload nginx || true
fi
echo "done. check: systemctl list-units 'botsaz-*'"
"""

RESTORE_TXT = """Botsaz server backup

1) Decrypt (needs the backup password):
   pip install cryptography
   python3 decrypt_backup.py <all .part files or the .enc file> -o server.zip
   (decrypt_backup.py and backup_crypto.py are in the project repository: scripts/ and the project root)
2) unzip server.zip -d botsaz-restore && cd botsaz-restore
3) bash restore.sh
4) Point the domains' DNS to the new server, copy SSL certificate files if nginx needs them
   (for example /etc/ssl/supraplus), then: systemctl reload nginx

Not included: SSL certificates and private keys, separately hosted apps (for example /opt/botsazvip), docker containers.
"""


def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}"


async def _hours() -> int:
    try:
        v = int((await _get(K_HOURS, "24")) or 24)
    except ValueError:
        v = 24
    return min(720, max(1, v))


def _fmt_interval(h: int) -> str:
    return f"{h // 24} روز" if h % 24 == 0 and h >= 24 else f"{h} ساعت"


def _skip_file(name: str) -> bool:
    return name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIX)


def collect_app_files(root: str, include_env: bool) -> tuple[list[tuple[str, str]], int]:
    """[(مسیرِ واقعی، نام در زیپ)] و حجمِ کل؛ فایل‌هایِ خیلی بزرگ رد می‌شوند."""
    out: list[tuple[str, str]] = []
    total = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            if _skip_file(fn) or (fn == ".env" and not include_env):
                continue
            full = os.path.join(dp, fn)
            try:
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                logger.warning("server backup: فایلِ بزرگ رد شد: %s (%s MB)", full, _mb(size))
                continue
            total += size
            out.append((full, "app/" + os.path.relpath(full, root).replace(os.sep, "/")))
    return out, total


def collect_system_files() -> list[tuple[str, str]]:
    out = []
    for pat in SYSTEM_GLOBS:
        for p in sorted(glob.glob(pat)):
            if p.endswith((".bak", ".bak-mts")) or ".bak" in os.path.basename(p) or not os.path.isfile(p):
                continue
            out.append((p, "system" + p))
    return out


_ZIP_MIN_TS = 315532800   # 1980-01-01؛ زیپ زمانِ قبل از آن را نمی‌پذیرد (فایل‌هایِ کپی‌شده/ساخته‌شده با mtime صفر)


def _zip_add(z: zipfile.ZipFile, full: str, arc: str) -> None:
    import time
    st = os.stat(full)
    zi = zipfile.ZipInfo(arc, date_time=time.localtime(max(st.st_mtime, _ZIP_MIN_TS))[:6])
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (st.st_mode & 0xFFFF) << 16
    with open(full, "rb") as src, z.open(zi, "w", force_zip64=True) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)


def _build_zip(dump_path: str, out_zip: str, include_env: bool, root: str = BASE_DIR) -> tuple[int, int]:
    """(تعدادِ فایل، حجمِ خامِ فایل‌ها)."""
    files, total = collect_app_files(root, include_env)
    if total > MAX_TOTAL_BYTES:
        raise RuntimeError(f"حجمِ فایل‌هایِ پروژه ({_mb(total)} MB) از سقفِ {_mb(MAX_TOTAL_BYTES)} MB بیشتر است.")
    sysf = collect_system_files()
    n = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        _zip_add(z, dump_path, "db/" + os.path.basename(dump_path))
        n += 1
        for full, arc in files + sysf:
            try:
                _zip_add(z, full, arc)
                n += 1
            except OSError:
                logger.warning("server backup: خواندنِ فایل ممکن نشد: %s", full)
        z.writestr("restore.sh", RESTORE_SH)
        z.writestr("RESTORE.txt", RESTORE_TXT)
    return n, total


async def run_server_backup(bot, now: datetime.datetime | None = None, part_bytes: int = PART_BYTES) -> tuple[bool, str]:
    if _running.locked():
        return False, "یک بک‌آپ همین الان در حال اجراست."
    async with _running:
        now = now or datetime.datetime.now()
        passphrase = (await _get(K_PASS)).strip()
        if len(passphrase) < MIN_PASS:
            return False, "رمزِ بک‌آپ تنظیم نشده (با /offsite ثبتش کن؛ بدونِ رمز چیزی فرستاده نمی‌شود)."
        include_env = (await _get(K_ENV, "1")) == "1"
        dest = (await _get(K_CHAT)).strip() or PLATFORM_ADMIN_ID
        try:
            dest = int(dest)
        except (TypeError, ValueError):
            pass
        stamp = now.strftime("%Y%m%d-%H%M")
        from platform_backup import make_dump
        with tempfile.TemporaryDirectory(prefix="svbackup-") as tmp:
            dump = await make_dump(tmp)
            zpath = os.path.join(tmp, f"botsaz-server-{stamp}.zip")
            count, raw = await asyncio.to_thread(_build_zip, dump, zpath, include_env, BASE_DIR)
            enc = zpath + ".enc"
            await asyncio.to_thread(BC.encrypt_file, zpath, enc, passphrase)
            digest = await asyncio.to_thread(BC.sha256_of, enc)
            parts = await asyncio.to_thread(BC.split_file, enc, part_bytes)
            total_bytes = 0
            for i, p in enumerate(parts, 1):
                size = os.path.getsize(p)
                cap = (f"📦 بک‌آپِ انتقالِ سرور — {stamp}\n"
                       f"تکهٔ {i} از {len(parts)} · {_mb(size)} MB · {count} فایل\n"
                       f"SHA256 کل: <code>{digest[:16]}…</code>\n"
                       "بازگردانی: scripts/decrypt_backup.py ← unzip ← bash restore.sh (راهنما داخلِ زیپ: RESTORE.txt)")
                await bot.send_document(dest, FSInputFile(p), caption=cap, parse_mode="HTML", request_timeout=900)
                total_bytes += size
        info = f"{now:%Y-%m-%d %H:%M} · {len(parts)} فایل · {_mb(total_bytes)} MB"
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


async def server_backup_loop(platform_bot) -> None:
    global _last_warn
    import time
    await asyncio.sleep(180)
    while True:
        try:
            if await due():
                ok, msg = await run_server_backup(platform_bot)
                if not ok and time.monotonic() - _last_warn > 3600 and PLATFORM_ADMIN_ID:
                    _last_warn = time.monotonic()
                    await platform_bot.send_message(
                        PLATFORM_ADMIN_ID, f"⚠️ بک‌آپِ انتقالِ سرور انجام نشد: {_html.escape(msg)}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("server backup: خطای حلقه")
            try:
                if time.monotonic() - _last_warn > 3600 and PLATFORM_ADMIN_ID:
                    _last_warn = time.monotonic()
                    await platform_bot.send_message(PLATFORM_ADMIN_ID, "⚠️ بک‌آپِ انتقالِ سرور با خطا مواجه شد؛ لاگ را ببین.")
            except Exception:
                logger.exception("server backup: هشدارِ خطا به مدیر ارسال نشد")
        await asyncio.sleep(600)


# ─────────────── پنلِ مدیر ───────────────

async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    en = (await _get(K_EN, "0")) == "1"
    has_pass = len((await _get(K_PASS)).strip()) >= MIN_PASS
    hrs = await _hours()
    inc_env = (await _get(K_ENV, "1")) == "1"
    chat = (await _get(K_CHAT)).strip()
    last = (await _get(K_INFO)).strip() or "هنوز بک‌آپی نرفته"
    text = ("📦 <b>بک‌آپِ انتقالِ سرور (زیپِ رمزگذاری‌شده)</b>\n\n"
            "یک فایل با دیتابیس + فایل‌هایِ پروژه + تنظیماتِ سرویس‌ها و nginx + اسکریپتِ بازگردانی؛ "
            "برایِ بالا آوردنِ سریعِ همه‌چیز روی یک سرورِ دیگر.\n\n"
            f"ارسالِ خودکار: {'🟢 روشن' if en else '🔴 خاموش'}\n"
            f"هر: <b>{_fmt_interval(hrs)}</b>\n"
            f"رمز: {'✅ تنظیم شده (همان رمزِ /offsite)' if has_pass else '⚠️ تنظیم نشده — با /offsite ثبت کن'}\n"
            f"شاملِ .env: {'بله' if inc_env else 'خیر'}\n"
            f"مقصد: {_html.escape(chat) if chat else 'پی‌ویِ تلگرامِ مدیر'}\n"
            f"آخرین ارسال: {_html.escape(last)}\n\n"
            "⚠️ رمز را جدا از سرور نگه دار؛ بدونِ آن فایل باز نمی‌شود.")
    rows = [
        [InlineKeyboardButton(text="▶️ همین الان بفرست", callback_data="svb:run", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=("⛔ خاموش‌کردنِ ارسالِ خودکار" if en else "🟢 روشن‌کردنِ ارسالِ خودکار"),
                              callback_data="svb:toggle", style=(ButtonStyle.DANGER if en else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text="🕒 بازهٔ پیش‌فرض (چرخشی)", callback_data="svb:cycle"),
         InlineKeyboardButton(text="✏️ بازهٔ دلخواه", callback_data="svb:hours")],
        [InlineKeyboardButton(text=f"{'✅' if inc_env else '⬜️'} همراهِ .env", callback_data="svb:env"),
         InlineKeyboardButton(text="📍 مقصد", callback_data="svb:chat")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def server_backup_button() -> list:
    return [InlineKeyboardButton(text="📦 بک‌آپِ انتقالِ سرور (زیپ)", callback_data="svb:menu", style=ButtonStyle.SUCCESS)]


@platform_server_backup_router.message(Command("serverbackup"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def svb_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _refresh(cb: CallbackQuery):
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_server_backup_router.callback_query(F.data.startswith("svb:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def svb_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    if act == "menu":
        await state.clear()
        await _refresh(cb)
        return await cb.answer()
    if act == "run":
        if len((await _get(K_PASS)).strip()) < MIN_PASS:
            return await cb.answer("اول رمزِ بک‌آپ را با /offsite ثبت کن (بدونِ رمز چیزی فرستاده نمی‌شود).", show_alert=True)
        await cb.answer("در حال ساخت و ارسال… (ممکن است چند دقیقه طول بکشد)", show_alert=True)
        try:
            ok, info = await run_server_backup(cb.bot)
        except Exception as e:
            logger.exception("server backup: اجرای دستی ناموفق")
            ok, info = False, f"{type(e).__name__}: {str(e)[:150]}"
        await cb.message.answer(("✅ بک‌آپ فرستاده شد: " if ok else "❌ انجام نشد: ") + _html.escape(info))
        return await _refresh(cb)
    if act == "hours":
        await state.set_state(SvbFSM.hours)
        await cb.message.answer("هر چند ساعت یک‌بار بفرستم؟ یک عدد بین ۱ تا ۷۲۰ بفرست.\n"
                                "مثلاً <code>12</code> = هر ۱۲ ساعت، <code>48</code> = هر ۲ روز، <code>168</code> = هر هفته.\n"
                                "لغو: /serverbackup", parse_mode="HTML")
        return await cb.answer()
    if act == "chat":
        await state.set_state(SvbFSM.chat)
        await cb.message.answer("آیدیِ عددیِ چت/کانالِ مقصد را بفرست (ربات باید آنجا اجازهٔ ارسال داشته باشد)،\n"
                                "یا «پیش‌فرض» تا به پی‌ویِ خودت برود. لغو: /serverbackup")
        return await cb.answer()
    if act == "toggle":
        await _set(K_EN, "0" if (await _get(K_EN, "0")) == "1" else "1")
    elif act == "cycle":
        cur = await _hours()
        nxt = next((p for p in HOURS_PRESETS if p > cur), HOURS_PRESETS[0])
        await _set(K_HOURS, str(nxt))
    elif act == "env":
        await _set(K_ENV, "0" if (await _get(K_ENV, "1")) == "1" else "1")
    await _refresh(cb)
    await cb.answer()


@platform_server_backup_router.message(SvbFSM.hours, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def svb_hours_in(msg: Message, state: FSMContext):
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    val = val.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not val.isdigit() or not (1 <= int(val) <= 720):
        return await msg.answer("❌ یک عدد بین ۱ تا ۷۲۰ بفرست (ساعت).")
    await _set(K_HOURS, str(int(val)))
    await state.clear()
    text, kb = await _panel()
    await msg.answer(f"✅ از این به بعد هر {_fmt_interval(int(val))} (اگر ارسالِ خودکار روشن باشد).\n\n" + text,
                     reply_markup=kb, parse_mode="HTML")


@platform_server_backup_router.message(SvbFSM.chat, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def svb_chat_in(msg: Message, state: FSMContext):
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
