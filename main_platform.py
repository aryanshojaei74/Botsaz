"""
main_platform.py — نقطهٔ ورودِ مستقلِ بات‌ساز (پلتفرم/مادر).

=== PATCH_SPLIT_MAIN_V1 ===
این فایل حاصلِ جداسازیِ main.py به دو سرویسِ کاملاً مستقل است:
  main_platform.py  → فقط ربات بات‌ساز (این فایل)
  main_tenant.py    → فقط ربات‌های مستأجر

چرا: قبلاً main.py یک FastAPI app تک‌پروسه‌ای بود که هم بات‌ساز و هم همهٔ
بات‌های مستأجر را سرو می‌کرد. خطای هر آپدیت جدا catch می‌شد، ولی اگر خودِ
پروسه (نه یک آپدیت) کرش می‌کرد — کرش در استارتاپ، OOM، باگی در یک importِ
مشترک — همه‌چیز با هم می‌خوابید. با این جداسازی، این فایل فقط به کدِ
platform_bot.py وابسته است، نه به tenant/__init__.py (۱۰هزار+ خط) — یعنی
باگی در کدِ اختصاصیِ بات‌های مستأجر دیگر این سرویس را نمی‌خواباند و برعکس.

اجرا (نمونهٔ systemd/uvicorn):
    uvicorn main_platform:app --host 127.0.0.1 --port 8000

مسیرها:
  POST /webhook/platform      → آپدیت‌های ربات بات‌ساز
  POST /nowpay/platform/ipn   → تأییدِ پرداختِ ارز دیجیتالِ کیف‌پولِ پلتفرم
  POST /tonpays/platform/callback → کال‌بکِ TonPays (با تاییدِ مستقلِ سمتِ ما)
  POST /blu/callback          → وبهوکِ درگاه بلو (با تاییدِ مستقلِ سمتِ ما)
  GET  /zarinpal/platform/callback → بازگشت از درگاهِ زرین‌پال (شارژِ ریالیِ کیف‌پولِ پلتفرم)
  GET  /idpay/platform/callback → بازگشت از درگاهِ آیدی‌پی (شارژِ ریالیِ کیف‌پولِ پلتفرم)
  POST /pay/{tracking_code}   → صفحهٔ پرداختِ ارز دیجیتال (مشترک؛ هم برای
                                 شارژِ کیف‌پولِ پلتفرم، هم برای شارژِ کیف‌پولِ
                                 هر بات مستأجر — با فیلدِ kind تشخیص داده
                                 می‌شود؛ به همین دلیل main_tenant.py هم
                                 همین pay_router را include می‌کند)
  GET  /health
"""
from __future__ import annotations
from fa_num import fa_days  # PATCH_FA_DAYS_V1

import asyncio
import datetime
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ButtonStyle
from aiogram.fsm.storage.redis import RedisStorage  # === PATCH_REDIS_FSM_STORAGE_V1 ===
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.types import Update, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest  # PATCH_MESSAGE_NOT_MODIFIED_GLOBAL_V1
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select  # === PATCH_DUP_ARCHIVED_TWIN_V1 === func


from config import platform_webhook_secret
from config import (PLATFORM_BOT_TOKEN, WEBHOOK_BASE, Session, init_db,
                    ARCHIVE_DAYS, PAID_PLAN_ARCHIVE_DAYS,
                    archive_retention_days, purge_bot, PLATFORM_ADMIN_ID,
                    DATABASE_URL)
from models import Bot as BotRow
from models import Setting
from models import PlatformSetting  # === PATCH_DUP_BOT_AUTOFIX_V1 ===
from platform_bot import (
    router as platform_router, swap_platform_poll_once, BRAND_BIO,
    ptc_daily_snapshot_loop,
)
from bot_utils import _temp_bot
from webhook_utils import spawn, notify_processing_error

import tenant_status_bus

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)

app = FastAPI()
try:
    from tenant.gateways.payment_page import pay_router
    app.include_router(pay_router)
except Exception:
    logger.exception("خطای نادیده‌گرفته‌شده (include pay_router)")

platform_bot = Bot(token=PLATFORM_BOT_TOKEN, default=DEFAULTS) if PLATFORM_BOT_TOKEN else None
platform_dp = Dispatcher(storage=RedisStorage.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    key_builder=DefaultKeyBuilder(with_bot_id=True)))
# === PATCH_REDIS_FSM_STORAGE_V1 ===
# نگاه کن به توضیحِ کاملِ همین پچ در main_tenant.py. اینجا فقط یک ربات
# (بات‌سازِ خودش) وجود دارد، نه ۸۴+ تا، ولی برایِ هماهنگی و آماده‌بودن
# برایِ چند-ورکری‌شدنِ احتمالیِ آینده، همان تغییر اعمال شد. چون Redisِ
# مشترکی با main_tenant.py استفاده می‌شود، with_bot_id=True لازم است تا
# اگر یک روز چند رباتِ ادمینیِ دیگر هم به این سرویس اضافه شد، تداخل
# نکنند.
platform_dp.include_router(platform_router)


# === PATCH_MESSAGE_NOT_MODIFIED_GLOBAL_V1 ===
# همان محافظِ سرویسِ مستأجر، برایِ رباتِ پلتفرم. اینجا ۱۱۲ نقطهٔ
# edit_* بدونِ try/except وجود دارد؛ تپِ دوباره رویِ یک دکمه کلِ آپدیت را
# می‌ترکاند و اسپینرِ دکمه باز می‌ماند. جزئیاتِ بیشتر در main_tenant.py.
@platform_dp.errors()
async def _platform_swallow_not_modified(event) -> bool:
    exc = getattr(event, "exception", None)
    if not isinstance(exc, TelegramBadRequest):
        return False
    if "message is not modified" not in str(exc):
        return False
    cb = getattr(getattr(event, "update", None), "callback_query", None)
    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.debug("بستنِ اسپینرِ callback بعد از not-modified ناموفق بود")
    logger.debug("not-modified نادیده گرفته شد (صفحه از قبل همین بود)")
    return True

_platform_bot_username: str | None = None  # کش یک‌باره در on_startup؛ برای دیپ‌لینک


# ───────────────────── حلقه‌های پس‌زمینهٔ پلتفرم ─────────────────────

async def _pg_conn_params_from_url(url: str) -> dict:
    from urllib.parse import urlparse, unquote
    clean = url.split("+", 1)[0] + "://" + url.split("://", 1)[1] if "+" in url.split("://")[0] else url
    p = urlparse(clean)
    return {
        "host": p.hostname or "localhost",
        "port": str(p.port or 5432),
        "user": unquote(p.username or "postgres"),
        "password": unquote(p.password or ""),
        "dbname": (p.path or "/").lstrip("/") or "postgres",
    }


async def _pg_backup_once():
    """یک بک‌آپِ فشردهٔ Postgres با pg_dump می‌گیرد (async، بدونِ بلاک‌کردنِ
    event loop) و بک‌آپ‌های قدیمی‌تر از ۲۸تا (≈۷ روز، هر ۶ ساعت یکی) را حذف
    می‌کند."""
    import os
    params = await _pg_conn_params_from_url(DATABASE_URL)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join("backups", f"botsaz-pg-{ts}.dump")
    env = {**os.environ, "PGPASSWORD": params["password"]}
    proc = await asyncio.create_subprocess_exec(
        "pg_dump",
        "-h", params["host"], "-p", params["port"],
        "-U", params["user"], "-d", params["dbname"],
        "--format=custom", "--no-owner", "--no-privileges",
        "-f", out_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(f"pg_dump با کد {proc.returncode} شکست خورد: {stderr.decode(errors='ignore')[:500]}")
    files = sorted(f for f in os.listdir("backups") if f.startswith("botsaz-pg-") and f.endswith(".dump"))
    for old in files[:-28]:
        try:
            os.remove(os.path.join("backups", old))
        except OSError:
            pass
    print(f"[backup] pg_dump موفق: {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)")


async def backup_loop():
    """هر ۶ ساعت یک بک‌آپِ فشرده از کلِ دیتابیس می‌گیرد (نگه‌داری ۲۸ نسخهٔ
    اخیر ≈ ۷ روز). این بک‌آپ کلِ دیتابیسِ مشترک است (پلتفرم + مستأجرها)،
    برای همین فقط در همین سرویس (نه main_tenant.py) اجرا می‌شود تا دوبار
    گرفته نشود."""
    import os
    os.makedirs("backups", exist_ok=True)
    is_pg = DATABASE_URL.startswith("postgresql")
    while True:
        try:
            if is_pg:
                await _pg_backup_once()
            elif os.path.exists("botsaz.db"):
                import shutil
                from sqlalchemy import text
                try:
                    async with Session() as s:
                        await s.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                except Exception as e:
                    print("checkpoint error:", e)
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
                shutil.copy("botsaz.db", f"backups/botsaz-{ts}.db")
                files = sorted(f for f in os.listdir("backups") if f.endswith(".db"))
                for old in files[:-48]:
                    os.remove(os.path.join("backups", old))
        except Exception as e:
            print("backup error:", e)
        await asyncio.sleep(6 * 3600)


INACTIVITY_WARN_DAYS = 5
INACTIVITY_GRACE_DAYS = 3


async def inactivity_loop():
    """به ربات‌های بی‌فعالیت (۵ روز بدون هیچ آپدیتی) اخطار می‌دهد و در صورت
    بی‌پاسخی (۳ روز دیگر بدون فعالیت)، دقیقاً مثل عدم‌تمدید پلن به بایگانی
    می‌فرستد — حذف نهایی‌شان را همان subscription_loop انجام می‌دهد."""
    from aiogram.exceptions import TelegramForbiddenError
    await asyncio.sleep(30)
    while True:
        try:
            if platform_bot:
                now = datetime.datetime.now()
                warn_cutoff = now - datetime.timedelta(days=INACTIVITY_WARN_DAYS)
                async with Session() as s:
                    bots = (await s.scalars(
                        select(BotRow).where(BotRow.status == "active", BotRow.archived_at.is_(None))
                    )).all()
                    for b in bots:
                        # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
                        # رباتِ همکار از چکِ بی‌فعالیتی معاف است: قاعدهٔ
                        # بقایِ آن «حدِ نصابِ ربات‌های فعال» است که
                        # partner_rules_loop اعمالش می‌کند، نه رفت‌وآمدِ
                        # روزانهٔ خودِ همکار در ربات.
                        if (b.plan or "") == "partner":
                            continue
                        # === PATCH_INACTIVITY_SKIP_IF_TIME_LEFT_V1 ===
                        # این حلقه صرفاً بر اساسِ «بی‌فعالیتی» ربات را پاز/بایگانی
                        # می‌کرد، حتی اگر پلنِ ربات هنوز کلی روزِ باقی‌مانده
                        # داشت — یعنی مشتری‌ای که پول داده و پلنش هنوز منقضی
                        # نشده، فقط چون سراغِ رباتش نرفته بود، بی‌هیچ ربطی به
                        # پرداخت، ربات‌اش «به‌طور نامشخص» پاز می‌شد. حالا اگر
                        # هنوز به سررسیدِ پلن نرسیده باشیم، این ربات کلاً از
                        # چکِ بی‌فعالیتی معاف است.
                        if b.plan_expires_at and b.plan_expires_at > now:
                            continue
                        baseline = b.last_activity_at or b.created_at
                        if not baseline:
                            continue

                        if b.inactivity_warned_at:
                            if b.last_activity_at and b.last_activity_at > b.inactivity_warned_at:
                                b.inactivity_warned_at = None
                                await s.commit()
                                continue
                            if now > b.inactivity_warned_at + datetime.timedelta(days=INACTIVITY_GRACE_DAYS):
                                b.status = "paused"
                                b.archived_at = now
                                b.archive_reason = "inactivity"
                                owner, uname, token, plan = b.owner_id, b.username, b.token, b.plan
                                await s.commit()
                                # === PATCH_SPLIT_CROSS_PROCESS_INVALIDATE_V1 ===
                                # قبلاً اینجا مستقیم _tenant_bots.pop(token) صدا
                                # زده می‌شد چون همه‌چیز یک پروسه بود. حالا که
                                # main_tenant.py جداست، به‌جاش سیگنالِ
                                # بین‌پروسه‌ای می‌فرستیم — همون کاری که پروسهٔ
                                # مستأجر با آن، هم کشِ وضعیت هم Bot instance
                                # کش‌شده‌ی این توکن را پاک می‌کند.
                                await tenant_status_bus.publish_tenant_invalidate(token)
                                _retention = archive_retention_days(plan, "inactivity")
                                try:
                                    await platform_bot.send_message(
                                        owner,
                                        f"🗄 ربات @{uname} به دلیل عدم پاسخ به اخطار غیرفعالی، "
                                        "به «بایگانی» منتقل شد.\n"
                                        f"تا {_retention} روز فرصت داری از «🗂 ربات‌های من ← 🗄 بایگانی» "
                                        "آن را بازگردانی کنی؛ در غیر این صورت برای همیشه حذف می‌شود.",
                                    )
                                except Exception:
                                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                                try:
                                    async with _temp_bot(token) as tb:
                                        await tb.send_message(
                                            owner,
                                            "🗄 این ربات به دلیل عدم فعالیت بایگانی شد.",
                                        )
                                except Exception:
                                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            continue

                        if baseline <= warn_cutoff:
                            b.inactivity_warned_at = now
                            owner, uname, token = b.owner_id, b.username, b.token
                            await s.commit()
                            grace_txt = f"{INACTIVITY_GRACE_DAYS} روز"
                            try:
                                await platform_bot.send_message(
                                    owner,
                                    f"⚠️ ربات @{uname} از {INACTIVITY_WARN_DAYS} روز پیش هیچ فعالیتی "
                                    f"(پیام/کلیک کاربر) نداشته است.\n"
                                    f"اگر تا {grace_txt} دیگر فعالیتی در آن ثبت نشود، به‌طور خودکار به "
                                    "بایگانی منتقل و پس از مهلت بایگانی برای همیشه حذف خواهد شد.\n"
                                    "برای جلوگیری از این اتفاق، کافی است کاربری در ربات پیام/خریدی انجام دهد "
                                    "یا خودت در ربات فعالیتی داشته باشی.",
                                )
                            except TelegramForbiddenError:
                                pass
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            try:
                                async with _temp_bot(token) as tb:
                                    await tb.send_message(
                                        owner,
                                        f"⚠️ این ربات {INACTIVITY_WARN_DAYS} روز است فعالیتی نداشته. "
                                        f"در صورت ادامهٔ بی‌فعالیتی تا {grace_txt} دیگر، بایگانی می‌شود.",
                                    )
                            except TelegramForbiddenError:
                                pass
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        except Exception as e:
            print("inactivity loop error:", e)
        await asyncio.sleep(6 * 3600)


def _token_of(bots, bot_id):
    for b in bots:
        if b.id == bot_id:
            return b.token
    return None


async def subscription_loop():
    """یادآوری سررسید پلن (از ۵ روز قبل، روزی یک‌بار) + بایگانی/حذفِ نهایی."""
    PAID_ARCHIVE_DAYS = 32
    REMIND_BEFORE_DAYS = 5
    await asyncio.sleep(20)
    while True:
        try:
            if platform_bot:
                now = datetime.datetime.now()
                today = now.strftime("%Y-%m-%d")
                to_delete = []
                # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
                from partner_rules import partner_term_enabled
                _partner_term_on = await partner_term_enabled()
                async with Session() as s:
                    bots = (await s.scalars(select(BotRow))).all()
                    for b in bots:
                      # === PATCH_SUBLOOP_PER_BOT_ISOLATION_V1 ===
                      # قبلاً کلِ حلقه داخلِ یک try بود: اولین رباتی که هر خطایی
                      # می‌داد (NOTIFYِ ناموفق، ارسالِ پیام به کاربری که ربات را
                      # بلاک کرده و …) کلِ پاس را می‌کشت و بقیهٔ ربات‌ها بررسی
                      # نمی‌شدند — و چون ترتیب ثابت است، عملاً هیچ‌وقت. نتیجه:
                      # ربات‌هایی با سررسیدِ گذشته که هنوز فعال مانده‌اند.
                      try:
                          # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
                          # رباتِ همکار مدلِ «تمدیدِ پلن» ندارد؛ تا وقتی
                          # مدیر انقضایِ دوره‌ای را روشن نکرده باشد، نه
                          # یادآوریِ تمدید می‌گیرد و نه بابتِ nonpayment
                          # بایگانی می‌شود.
                          if (b.plan or "") == "partner" and not _partner_term_on:
                              continue
                          purchased = (b.plan or "free") != "free"
                          # === PATCH_KEEP_MERGED_TOMBSTONES_V1 ===
                          # ردیفِ «سنگِ‌قبر»ِ ادغامِ رباتِ تکراری (scripts/dupbots.py) عمداً هرگز DELETE
                          # نمی‌شود: تراکنش‌ها/کاربرانِ ادغام‌شده هنوز به آن (و کاربرانش) اشاره می‌کنند و
                          # حذفش هر ۶ ساعت ForeignKeyViolation می‌داد (ربات ۳۱۵ و ۹۹).
                          if b.archive_reason == "merged_duplicate" or "#merged" in (b.username or ""):
                              continue
                          # === PATCH_INCONSISTENT_ARCHIVE_SELFHEAL_V1 ===
                          # حالتِ ناسازگار: status="active" ولی archived_at هنوز
                          # ست است (مثلاً یه مسیرِ تمدید/فعال‌سازیِ دیگر یادش رفته
                          # archived_at را پاک کند). این ترکیب باعث می‌شد ربات
                          # برای همیشه از چکِ سررسیدِ زیر معاف بماند. این‌جا خودکار
                          # تصحیح می‌شود تا دوباره وارد چرخهٔ عادی شود.
                          if b.archived_at and b.status == "active":
                              b.archived_at = None
                              b.archive_reason = None
                              await s.commit()
                          if b.archived_at and now > b.archived_at + datetime.timedelta(
                                  days=archive_retention_days(b.plan, b.archive_reason)):
                              to_delete.append((b.id, b.owner_id, b.username))
                              continue
                          if b.archived_at:
                              continue
                          if not b.plan_expires_at:
                              continue
                          days_left = (b.plan_expires_at - now).days
  # === PATCH_IMMEDIATE_PAID_EXPIRY_V2 ===
                          # قبلاً: پلن خریداری‌شده ۳۲ روز و پلن رایگان ۲ روز مهلت داشت.
                          # الان: هر دو صفر — دقیقاً سرِ سررسید (plan_expires_at) بایگانی می‌شود.
                          grace_days = 0
                          deadline = b.plan_expires_at + datetime.timedelta(days=grace_days)
                          # === PATCH_PLAN_UPGRADE_BANK_V1 ===
                          # حرفه‌ای تمام شد و زمانِ رشد نگه‌داری شده → بایگانی نکن؛
                          # ربات را به رشد با زمانِ باقی‌مانده برگردان.
                          if now > deadline and b.banked_plan and (b.banked_seconds or 0) > 0:
                              _from_plan = b.plan
                              b.plan = b.banked_plan
                              b.plan_expires_at = now + datetime.timedelta(seconds=int(b.banked_seconds))
                              b.sub_months = b.banked_sub_months or 1
                              _left_days = max(1, round(int(b.banked_seconds) / 86400))
                              b.banked_plan = None
                              b.banked_seconds = None
                              b.banked_sub_months = None
                              b.last_expiry_notice = None
                              token = b.token
                              _to_plan = b.plan
                              await s.commit()
                              await tenant_status_bus.publish_tenant_invalidate(token)
                              try:
                                  from plans import Plan as _Pl, PLAN_INFO as _PI
                                  await platform_bot.send_message(
                                      b.owner_id,
                                      f"ℹ️ دورهٔ پلنِ «{_PI[_Pl(_from_plan)]['title']}» ربات "
                                      f"@{b.username} تمام شد و به پلنِ «{_PI[_Pl(_to_plan)]['title']}» "
                                      f"برگشت؛ {_left_days} روز از آن باقی مانده است.")
                              except Exception:
                                  logger.exception("اعلانِ برگشت به پلنِ نگه‌داری‌شده ناموفق بود")
                              # === PATCH_PLAN_BANK_ADMIN_NOTICE_V1 === خبر به مدیرِ پلتفرم
                              try:
                                  if PLATFORM_ADMIN_ID:
                                      await platform_bot.send_message(
                                          PLATFORM_ADMIN_ID,
                                          f"🔁 برگشتِ پلن: ربات @{b.username} (مالک {b.owner_id})\n"
                                          f"دورهٔ «{_PI[_Pl(_from_plan)]['title']}» تمام شد و به "
                                          f"«{_PI[_Pl(_to_plan)]['title']}» با {_left_days} روزِ باقی‌مانده برگشت.")
                              except Exception:
                                  logger.exception("اعلانِ برگشتِ پلن به مدیر ناموفق بود")
                              continue
                          if now > deadline:
                              b.status = "paused"
                              b.archived_at = now
                              # === PATCH_TRIAL_ARCHIVE_V1 ===
                              # ربات‌هایی که از یک رباتِ همکار به‌عنوانِ
                              # تست گرفته شده‌اند بدهکار نیستند؛ دلیلِ
                              # جداگانه می‌گیرند تا در فهرستِ بایگانی از
                              # بدحساب‌ها قابلِ تفکیک باشند.
                              _is_trial = False
                              try:
                                  from models import PartnerSale
                                  _is_trial = bool(await s.scalar(
                                      select(PartnerSale.id).where(
                                          PartnerSale.sold_bot_id == b.id,
                                          PartnerSale.kind == "trial")))
                              except Exception:
                                  logger.exception(
                                      "تشخیصِ رباتِ تست هنگامِ بایگانی ناموفق بود")
                              b.archive_reason = (
                                  "trial_expired" if _is_trial else "nonpayment")
                              token = b.token
                              await s.commit()
                              await tenant_status_bus.publish_tenant_invalidate(token)
                              if purchased:
                                  note = (
                                      f"🗄 ربات @{b.username} به دلیل عدم تمدید پلن به «بایگانی» منتقل شد.\n\n"
                                      "✅ نگران نباش — ربات و اطلاعاتش فوراً حذف نمی‌شود.\n"
                                      f"تا <b>{PAID_PLAN_ARCHIVE_DAYS} روز</b> فرصت داری از «🗂 ربات‌های من ← "
                                      "🗄 بایگانی» پلن را پرداخت کنی تا بلافاصله از بایگانی خارج و فعال شود؛ "
                                      "در غیر این صورت بعد از این مهلت برای همیشه حذف می‌شود."
                                  )
                              else:
                                  note = (
                                      f"🗄 ربات @{b.username} به دلیل عدم تمدید پلن به «بایگانی» منتقل شد.\n"
                                      f"تا {ARCHIVE_DAYS} روز فرصت داری با پرداخت/تمدید آن را بازگردانی؛ "
                                      "در غیر این صورت ربات و اطلاعاتش برای همیشه حذف می‌شود.\n"
                                      "بازگردانی از «🗂 ربات‌های من ← 🗄 بایگانی»."
                                  )
                              try:
                                  await platform_bot.send_message(b.owner_id, note)
                              except Exception:
                                  logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                              # === PATCH_PARTNER_ORPHAN_RENEW_NOTICE_V1 ===
                              try:
                                  from partner_orphan_notice import notice_due as _orph_due, send_notice as _orph_send
                                  if purchased and not _is_trial and await _orph_due(s, b):
                                      await _orph_send(b, platform_bot, days_left=None, archived=True)
                              except Exception:
                                  logger.exception("پیامِ تمدید در بات‌سازِ اصلی (خریدارِ همکارِ غیرفعال) ارسال نشد")
                              continue
                          if days_left <= REMIND_BEFORE_DAYS and b.last_expiry_notice != today:
                              b.last_expiry_notice = today
                              await s.commit()
                              # === PATCH_RENEW_REMINDER_DIRECT_BUTTON_V1 ===
                              # دکمه مستقیم به صفحهٔ تمدید/ارتقایِ همان ربات
                              # می‌رود (mbup:)، بدونِ مرحلهٔ اضافه.
                              #
                              # === PATCH_REMINDER_SHOW_BOT_V1 ===
                              # یوزرنیم دوباره به متن برگشت: کاربری که چند ربات
                              # دارد از «این ربات» نمی‌فهمد کدام یکی سررسید شده
                              # و باید حدس بزند. دکمه که به رباتِ درست می‌رود،
                              # ولی متن هم باید خودش گویا باشد.
                              _who = f"@{b.username}" if b.username else f"#{b.id}"
                              if days_left > 0:
                                  msg = (f"⏰ یادآوری: پلنِ ربات <b>{_who}</b> "
                                         f"<b>{fa_days(days_left)}</b> به سررسید می‌رسد.")
                              elif days_left == 0:
                                  msg = (f"⏰ پلنِ ربات <b>{_who}</b> <b>امروز</b> "
                                         "به سررسید می‌رسد.")
                              else:
                                  grace = max((deadline - now).days, 0)
                                  if purchased:
                                      msg = (f"⚠️ پلنِ ربات <b>{_who}</b> منقضی شده است.\n"
                                             f"تا <b>{grace} روز دیگر</b> اگر تمدید نشود، ربات به بایگانی "
                                             "می‌رود (حذف نمی‌شود و با پرداخت دوباره فعال می‌شود).")
                                  else:
                                      msg = (f"⚠️ پلنِ ربات <b>{_who}</b> منقضی شده است. تا حدود "
                                             f"{grace} روز دیگر اگر تمدید نشود، به بایگانی می‌رود.")
                              renew_kb = InlineKeyboardMarkup(inline_keyboard=[[
                                  InlineKeyboardButton(text="💳 ارتقا و تمدیدِ ربات", callback_data=f"mbup:{b.id}",
                                                       style=ButtonStyle.PRIMARY),
                              ]])
                              # === PATCH_PARTNER_ORPHAN_RENEW_NOTICE_V1 ===
                              # خریدارِ رباتِ یک همکارِ غیرفعال: پیام از طریقِ خودِ رباتش با دکمهٔ
                              # شیشه‌ای به بات‌سازِ اصلی (بات‌سازِ اصلی ممکن است هرگز استارت نشده باشد).
                              from partner_orphan_notice import notice_due as _orph_due, send_notice as _orph_send
                              if purchased and await _orph_due(s, b):
                                  if await _orph_send(b, platform_bot, days_left=days_left):
                                      continue
                              try:
                                  await platform_bot.send_message(b.owner_id, msg, reply_markup=renew_kb)
                              except Exception as e:
                                  # کاربر ممکن است ربات را block کرده باشد یا شناسهٔ چت دیگر معتبر نباشد؛
                                  # این خطا نباید با traceback کامل، حلقهٔ اشتراک را شلوغ کند.
                                  detail = str(e).lower()
                                  if (
                                      'chat not found' in detail
                                      or 'bot was blocked' in detail
                                      or 'user is deactivated' in detail
                                  ):
                                      logger.info(
                                          'اعلان تمدید برای owner_id=%s ارسال نشد؛ چت نامعتبر یا مسدود است',
                                          b.owner_id
                                      )
                                  else:
                                      logger.exception(
                                          'خطای ارسال اعلان تمدید برای owner_id=%s', b.owner_id
                                      )
                      except Exception:
                          logger.exception("subscription loop: ردشدن از رباتِ id=%s",
                                           getattr(b, "id", None))

                # رفرنس‌های partner_sales را یک‌بار برای کل دسته می‌خوانیم؛
                # کوئری جداگانه برای هر ربات در زمان پاک‌سازی باعث فشار غیرضروری به PostgreSQL می‌شد.
                linked_partner_sale_bot_ids: set[int] = set()
                partner_sale_lookup_ok = True
                if to_delete:
                    try:
                        from models import PartnerSale
                        delete_ids = [bot_id for bot_id, _, _ in to_delete]
                        linked_partner_sale_bot_ids = set((await s.scalars(
                            select(PartnerSale.sold_bot_id).where(
                                PartnerSale.sold_bot_id.in_(delete_ids)
                            )
                        )).all())
                    except Exception:
                        partner_sale_lookup_ok = False
                        logger.exception(
                            'خواندن دسته‌ای مرجع partner_sales ناموفق بود؛ حذف کل دسته متوقف شد'
                        )
                for bot_id, owner_id, username in to_delete:
                    # === PATCH_SUBLOOP_PURGE_PER_BOT_ISOLATION_V1 ===
                    # همان کلاسِ باگی که قبلاً برایِ حلقهٔ اسکن رفع شده بود
                    # (PATCH_SUBLOOP_PER_BOT_ISOLATION_V1)، اینجا در حلقهٔ
                    # حذفِ نهایی هم بود: اگر purge_bot برایِ یک ربات به هر
                    # دلیلی شکست بخورد (مثلاً یک ارجاعِ خارجیِ باقی‌مانده
                    # در جدولِ دیگر)، کلِ این try/except بیرونی (که فقط
                    # لاگ می‌کند) اجرا می‌شود و بقیهٔ ربات‌هایِ همین
                    # دسته دیگر پردازش نمی‌شوند — تا چرخهٔ ۶ساعتهٔ بعدی.
                    try:
                        # ربات‌هایی که در partner_sales مرجع دارند نباید حذف شوند؛
                        # حذف مستقیم آن‌ها باعث ForeignKeyViolation و تکرار خطا می‌شود.
                        if not partner_sale_lookup_ok:
                            continue
                        if bot_id in linked_partner_sale_bot_ids:
                            continue
                        dead_token = _token_of(bots, bot_id)
                        if dead_token:
                            await tenant_status_bus.publish_tenant_invalidate(dead_token)
                        token = await purge_bot(bot_id)
                        if token:
                            await tenant_status_bus.publish_tenant_invalidate(token)
                            try:
                                async with _temp_bot(token) as tb:
                                    await tb.delete_webhook()
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                        try:
                            await platform_bot.send_message(
                                owner_id,
                                f"🗑 ربات @{username} پس از {ARCHIVE_DAYS} روز ماندن در بایگانی و عدم تمدید، "
                                "برای همیشه حذف شد.",
                            )
                        except Exception:
                            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                    except Exception:
                        logger.exception("subscription loop: ردشدن از حذفِ نهاییِ رباتِ id=%s (بقیهٔ دسته ادامه پیدا می‌کند)",
                                         bot_id)
        except Exception:
            # === PATCH_SUBLOOP_PER_BOT_ISOLATION_V1 === لاگِ کامل به‌جای print
            logger.exception("subscription loop error")
        await asyncio.sleep(6 * 3600)


async def platform_swap_poll_loop(interval: int = 45):
    """تایید خودکار شارژ سواپ‌ولتِ کیف پول پلتفرم (bot_id=0) و اطلاع به کاربر."""
    await asyncio.sleep(15)
    while True:
        try:
            notifs = await swap_platform_poll_once()
            if notifs and platform_bot:
                for tg_id, credited in notifs:
                    try:
                        await platform_bot.send_message(
                            tg_id,
                            f"✅ پرداخت سواپ‌ولت شما دریافت شد و کیف پولت "
                            f"{int(credited):,} تومان شارژ شد.")
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        except Exception as e:
            print("platform swap poll error:", e)
        await asyncio.sleep(interval)


# === MARKER_ZARINPAL_PLATFORM_RECONCILE_V1 ===
async def zarinpal_platform_reconcile_loop(interval: int = 300):
    """جبرانِ فاکتورهایِ زرین‌پالی که پول کسر شده ولی چون مرورگرِ کاربر به
    /zarinpal/platform/callback برنگشته، کیف‌پول خودکار شارژ نشده. هر
    ``interval`` ثانیه فاکتورهایِ «zarinpal_pending»ِ بینِ ۲ دقیقه تا ۲۴
    ساعتِ پیش را دوباره نزدِ زرین‌پال استعلام می‌گیرد. idempotent: مثلِ
    دکمهٔ «بررسی پرداخت» و خودِ callback، قبل از verify چک می‌کند
    «zarinpal_charge»یِ همان authority از قبل ثبت نشده باشد."""
    import datetime as _dt
    from sqlalchemy import select as _select
    import platform_zarinpal
    from models import PlatformTxn
    from platform_bot import get_pset, _platform_zarinpal_credit

    await asyncio.sleep(30)
    while True:
        try:
            mid = await get_pset("platform_zarinpal_merchant_id", "")
            if not mid:
                await asyncio.sleep(interval)
                continue
            now = _dt.datetime.now()
            cutoff_new = now - _dt.timedelta(minutes=2)
            cutoff_old = now - _dt.timedelta(hours=24)
            async with Session() as s:
                pendings = (await s.scalars(
                    _select(PlatformTxn).where(
                        PlatformTxn.kind == "zarinpal_pending",
                        PlatformTxn.created_at <= cutoff_new,
                        PlatformTxn.created_at >= cutoff_old,
                    ))).all()
            for p in pendings:
                try:
                    note = p.note or ""
                    if "authority=" not in note:
                        continue
                    authority = note.split("authority=", 1)[1].split("|", 1)[0]
                    async with Session() as s:
                        already = await s.scalar(
                            _select(PlatformTxn).where(
                                PlatformTxn.kind == "zarinpal_charge",
                                PlatformTxn.note.contains(f"authority={authority}"),
                            ))
                    if already:
                        continue
                    amount_toman = int(float(p.amount))
                    ok, res = await platform_zarinpal.verify_payment(mid, amount_toman, authority)
                    if not ok:
                        continue
                    new_bal = await _platform_zarinpal_credit(
                        p.user_telegram_id, amount_toman,
                        f"authority={authority}|ref_id={res}|auto_reconcile=1",
                        marker=f"authority={authority}")
                    if new_bal is None:      # هم‌زمان توسطِ کالبک/دکمه شارژ شد
                        continue
                    async with Session() as s:
                        p2 = await s.scalar(
                            _select(PlatformTxn).where(
                                PlatformTxn.kind == "zarinpal_pending",
                                PlatformTxn.note.contains(f"authority={authority}"),
                            ).with_for_update())
                        if p2:
                            p2.kind = "zarinpal_pending_done"
                            await s.commit()
                    try:
                        if platform_bot:
                            await platform_bot.send_message(
                                p.user_telegram_id,
                                "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\n"
                                f"💰 مبلغ واریزی: <b>{amount_toman:,} تومان</b>\n"
                                f"👛 موجودی فعلی: <b>{new_bal:,.0f} تومان</b>\n"
                                "🧾 روش پرداخت: 🏦 زرین‌پال")
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                except Exception:
                    logger.exception(
                        "zarinpal reconcile: خطا روی یک فاکتورِ pending (بقیه ادامه پیدا می‌کند)")
        except Exception:
            logger.exception("zarinpal platform reconcile loop error")
        await asyncio.sleep(interval)


async def _setting_get(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _setting_set(bot_id: int, key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


BIO_WARN_COOLDOWN_HOURS = 6
BIO_VIOLATION_LIMIT = 5


# === PATCH_DUP_ARCHIVED_TWIN_V1 ===
# === PATCH_DUP_BOT_AUTOFIX_V1 ===
# گزارشِ ادمین: این هشدار هر بار که سرویس ری‌استارت می‌شد (چون _seen فقط
# تویِ حافظه بود) و هر ساعت هم که سرویس بالا می‌ماند، برایِ همون
# موردهایِ حل‌نشده دوباره می‌اومد — و چون ابزارِ ادغام (dupbots.py)
# دستوریه، ادمین مجبور بود دستی رو سرور بزنه.
#
# حالا این حلقه اول خودش تلاش می‌کنه: اگه دقیقاً یکی از توکن‌هایِ گروه
# زنده باشه (و مالکِ همه‌شون یکی باشه)، خودکار با dupbots.py ادغام
# می‌کنه — همون گاردهایِ امنیتیِ خودِ dupbots.py (چک‌کردنِ توکنِ زنده،
# چک‌کردنِ یکی‌بودنِ مالک) رعایت می‌شه، فقط دیگه نیازی به دستِ ادمین
# نیست. فقط برایِ موردهایِ واقعاً مبهم (هیچ توکنی زنده نیست، یا بیش از
# یکی زنده‌ست) به ادمین خبر می‌ده — و اون‌وقت هم، به‌جایِ حافظه‌یِ
# ناپایدار، تویِ platform_settings ثبت می‌کنه که آخرین‌بار کِی خبر داده،
# تا با هر ری‌استارت دوباره اسپم نشه (کول‌داونِ ۲۴ ساعته).
_DUP_ALERT_COOLDOWN_HOURS = 24
_DUP_NO_ACTION = "بدونِ اقدام"


async def _dup_autofix_group(uname: str, ids: list[int]) -> str | None:
    """=== PATCH_DUP_ARCHIVED_AUTOFIX_V2 ===
    قاعده‌های خودکار (بدونِ نیازِ دستِ ادمین):
      ۱) ردیف‌هایِ بایگانی‌شده «دوقلویِ مرده» حساب می‌شوند و توکنشان مهم نیست
         (بایگانی توکن را در تلگرام باطل نمی‌کند، برای همین قبلاً «چند توکنِ زنده»
         دیده می‌شد و گزارشِ مبهم می‌آمد).
      ۲) اگر چند ردیفِ *فعال* باشد، همان منطقِ قبلی: مالکِ یکسان + دقیقاً یک توکنِ زنده.
      ۳) ردیفِ باقی‌مانده = فعال، وگرنه جدیدترینِ بایگانی. دوقلوهایِ هم‌مالک ادغام
         می‌شوند؛ دوقلوهایِ مالکِ دیگر فقط سنگِ‌قبر می‌شوند (داده‌شان دست‌نخورده).
      ۴) گروهی که *همه*‌اش بایگانی و مالک‌هایش فرق دارد، خطری ندارد: بی‌صدا رد می‌شود.
    فقط وقتی چند ردیفِ فعالِ واقعاً مبهم باشد None برمی‌گرداند (اطلاع به مدیر)."""
    import os as _os
    import sys as _sys
    _scripts_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    import dupbots as _D  # noqa: E402
    from sqlalchemy import text as text_sql

    async with Session() as s:
        conn = await s.connection()
        rows = {bid: await _D.bot_row(conn, bid) for bid in ids}
        arch = {bid: bool((await conn.execute(text_sql(
            "SELECT archived_at IS NOT NULL FROM bots WHERE id=:i"), {"i": bid})).scalar())
            for bid in ids}
    if len(rows) < 2 or any(r is None for r in rows.values()):
        return None
    active_ids = [b for b in ids if not arch[b]]
    arch_ids = [b for b in ids if arch[b]]
    notes: list[str] = []

    if len(active_ids) >= 2:
        if len({rows[b][2] for b in active_ids}) > 1:
            return None
        alive = {b: await _D.token_alive(rows[b][3]) for b in active_ids}
        if any(a is None for a in alive.values()):
            return None
        alive_ids = [b for b, a in alive.items() if a]
        if len(alive_ids) != 1:
            return None
        k = alive_ids[0]
        for d in [b for b in active_ids if b != k]:
            if not await _D.cmd_merge(k, d, menu_from=k, apply=True):
                return None
            notes.append(f"drop={d}")
        active_ids = [k]

    if active_ids:
        keep = active_ids[0]
    elif arch_ids:
        keep = max(arch_ids, key=lambda b: (rows[b][6], b))
    else:
        return None
    for d in [b for b in arch_ids if b != keep]:
        if rows[d][2] == rows[keep][2]:
            ok = await _D.cmd_merge(keep, d, menu_from=keep, apply=True, twin=True)
        elif active_ids:
            ok = await _D.tombstone_only(d)
        else:
            continue   # همه بایگانی + مالکِ متفاوت: خطری ندارد
        if ok:
            notes.append(f"drop={d}")
    if not notes:
        return f"@{uname}: فقط بایگانی با مالک‌هایِ متفاوت — بی‌خطر، {_DUP_NO_ACTION}"
    return f"@{uname}: خودکار ادغام شد (keep={keep}, {', '.join(notes)})"


async def _dup_alert_recently_sent(uname: str) -> bool:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(
            PlatformSetting.key == f"dup_alert:{uname}"))
        if not row:
            return False
        try:
            last = datetime.datetime.fromisoformat(row.value)
        except Exception:
            return False
        return (datetime.datetime.utcnow() - last) < datetime.timedelta(
            hours=_DUP_ALERT_COOLDOWN_HOURS)


async def _dup_alert_mark_sent(uname: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(
            PlatformSetting.key == f"dup_alert:{uname}"))
        now_iso = datetime.datetime.utcnow().isoformat()
        if row:
            row.value = now_iso
        else:
            s.add(PlatformSetting(key=f"dup_alert:{uname}", value=now_iso))
        await s.commit()


async def dup_bot_watch_loop(interval_min: int = 60):
    """نگهبانِ ربات‌هایِ تکراری.

    گاردِ ساخت جلویِ ساختِ ردیفِ دوم را می‌گیرد، ولی اگر از مسیرِ دیگری
    (مهاجرتِ توکن، بازیابیِ بکاپ، دستکاریِ دستی) باز هم تکراری ساخته شود،
    این حلقه ظرفِ یک ساعت پیدایش می‌کند.

    موردهایِ بدونِ ابهام (دقیقاً یک توکنِ زنده، مالکِ یکسان) را خودش
    خودکار ادغام می‌کند. فقط موردهایِ واقعاً مبهم را به مدیر خبر می‌دهد —
    چون ادغامِ خودکارِ حدسی از خودِ تکراری بدتر است.
    """
    await asyncio.sleep(90)
    while True:
        try:
            async with Session() as s:
                rows = (await s.execute(
                    select(func.lower(BotRow.username), func.count(BotRow.id))
                    .where(BotRow.username.is_not(None),
                           func.coalesce(BotRow.archive_reason, "") != "merged_duplicate")
                    .group_by(func.lower(BotRow.username))
                    .having(func.count(BotRow.id) > 1))).all()
            for uname, cnt in rows:
                async with Session() as s:
                    dups = (await s.scalars(select(BotRow).where(
                        func.lower(BotRow.username) == uname,
                        func.coalesce(BotRow.archive_reason, "") != "merged_duplicate"
                    ).order_by(BotRow.id))).all()
                ids = [b.id for b in dups]

                try:
                    fixed = await _dup_autofix_group(uname, ids)
                except Exception:
                    fixed = None
                    logger.exception("ادغامِ خودکارِ @%s ناموفق بود", uname)
                if fixed:
                    # «بدونِ اقدام» هر ساعت برایِ همان گروه تکرار می‌شد (صدها WARNING)
                    if fixed.endswith(_DUP_NO_ACTION):
                        logger.debug("ربات تکراری: %s", fixed)
                    else:
                        logger.warning("ربات تکراری خودکار ادغام شد: %s", fixed)
                    continue

                logger.warning("ربات تکراری (نیازِ تصمیمِ دستی): @%s (%s ردیف)", uname, cnt)
                if await _dup_alert_recently_sent(uname):
                    continue  # === PATCH_DUP_BOT_AUTOFIX_V1 === کول‌داون، اسپم نشه
                detail = "\n".join(
                    f"• id={b.id} — {'🗄 بایگانی' if b.archived_at else '🟢 فعال'}"
                    f" — پلن {b.plan} — ساخت {b.created_at:%Y-%m-%d}"
                    for b in dups)
                if platform_bot:
                    try:
                        await platform_bot.send_message(
                            PLATFORM_ADMIN_ID,
                            f"⚠️ <b>رباتِ تکراریِ مبهم</b> (نتونستم خودکار حلش کنم)\n\n"
                            f"🤖 @{uname} — {cnt} ردیف\n{detail}\n\n"
                            f"یا هیچ توکنی زنده نیست، یا بیش از یکی زنده‌ست، یا "
                            f"مالک‌ها فرق دارن — این‌ها رو فقط دستی می‌شه تصمیم گرفت.\n"
                            f"برایِ بررسی:\n"
                            f"<code>venv/bin/python scripts/dupbots.py show "
                            f"--keep {dups[0].id} --drop {dups[-1].id}</code>\n\n"
                            f"<i>تا {_DUP_ALERT_COOLDOWN_HOURS} ساعتِ دیگه دوباره یادآوری نمی‌شه.</i>",
                            parse_mode="HTML")
                        await _dup_alert_mark_sent(uname)
                    except Exception:
                        logger.exception("اطلاعِ رباتِ تکراری به مدیر ناموفق بود")
        except Exception:
            logger.exception("dup_bot_watch_loop error")
        await asyncio.sleep(interval_min * 60)



async def brand_bio_loop(interval_min: int = 30):
    """=== PATCH_REMOVE_BIO_LOCK_V1 ===
    «قفلِ بیو» حذف شد.

    این حلقه قبلاً هر ۳۰ دقیقه بیویِ همهٔ ربات‌هایِ رایگان را چک می‌کرد،
    اگر مالک عوضش کرده بود به‌زور تبلیغِ بات‌ساز را برمی‌گرداند، تخلف را
    می‌شمرد و در آستانهٔ مشخصی **کلِ ربات را غیرفعال می‌کرد**.

    حالا به یک پاک‌سازیِ یک‌باره تبدیل شده: در اولین اجرا، هر رباتِ رایگانی
    که بیویش دقیقاً همان تبلیغِ قدیمی است خالی می‌شود و بعد حلقه تمام
    می‌شود. بیویی که خودِ مالک نوشته دست‌نخورده می‌ماند.

    خودِ تابع حذف نشد تا worker_platform (که آن را زمان‌بندی می‌کند) با
    دیپلویِ ناقص نشکند.
    """
    await asyncio.sleep(30)
    cleaned = 0
    try:
        async with Session() as s:
            rows = (await s.scalars(
                select(BotRow).where(BotRow.plan == "free", BotRow.status == "active")
            )).all()
            bots = [(b.token, b.username) for b in rows]
        for token, username in bots:
            try:
                tb = Bot(token=token, default=DEFAULTS)
                try:
                    cur = (await tb.get_my_short_description()).short_description or ""
                    if cur.strip() == BRAND_BIO.strip():
                        await tb.set_my_short_description(short_description="")
                        cleaned += 1
                finally:
                    await tb.session.close()
            except Exception as e:
                print(f"brand_bio cleanup skip @{username or '?'}: {e}")
            await asyncio.sleep(0.5)
        print(f"[brand_bio] قفلِ بیو حذف شد؛ {cleaned} ربات پاک‌سازی شد.")
    except Exception as e:
        print("brand_bio cleanup error:", e)
    # هیچ حلقه‌ای دیگر لازم نیست؛ تسک همین‌جا تمام می‌شود.


# === PATCH_TONPAYS_PLATFORM_V1 ===
async def tonpays_platform_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ TonPaysِ بات‌ساز را استعلام و تسویه می‌کند.

    پیگیریِ لحظه‌ای در حافظه است و با ری‌استارت از بین می‌رود؛ این حلقه از رویِ
    دیتابیس کار می‌کند تا هیچ پرداختی بی‌جواب نماند.
    """
    from platform_tonpays import poll_pending_once
    await asyncio.sleep(45)
    while True:
        try:
            await poll_pending_once()
        except Exception:
            import traceback
            print("tonpays_platform_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_BLU_PLATFORM_V1 ===
async def blu_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ بلو را استعلام و تسویه می‌کند (پشتیبانِ وبهوک)."""
    from platform_blu import poll_pending_once
    await asyncio.sleep(50)
    while True:
        try:
            await poll_pending_once()
        except Exception:
            import traceback
            print("blu_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_PLATFORM_APAY_V1 ===
async def apay_platform_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ آپیِ بات‌ساز را استعلام و تسویه می‌کند (پشتیبانِ وب‌هوک)."""
    from platform_apay import poll_pending_once
    await asyncio.sleep(55)
    while True:
        try:
            await poll_pending_once()
        except Exception:
            logger.exception("apay_platform_poll_loop error")
        try:
            from platform_apay import ask_admin_pending
            await ask_admin_pending()            # PATCH_APAY_ADMIN_ASK_V1
        except Exception:
            logger.exception("apay_platform ask_admin error")
        await asyncio.sleep(interval_sec)


async def crypto_poll_loop_platform():
    from tenant.gateways.payment_page import crypto_poll_loop as _cpl
    await _cpl(kind="platform")


# ───────────────────────────── وبهوک/IPN ─────────────────────────────

async def _process_platform(data: dict):
    try:
        update = Update.model_validate(data, context={"bot": platform_bot})
        await platform_dp.feed_update(platform_bot, update)
    except Exception:
        logger.exception("platform webhook error — update handling failed")
        try:
            await notify_processing_error(platform_bot, data)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


_PLATFORM_WH_RESET_AT = [0.0]


def _schedule_platform_webhook_reset() -> None:
    import time as _t
    if not platform_bot or not WEBHOOK_BASE or _t.monotonic() - _PLATFORM_WH_RESET_AT[0] < 300:
        return
    _PLATFORM_WH_RESET_AT[0] = _t.monotonic()

    async def _reset():
        try:
            from webhook_utils import PLATFORM_ALLOWED_UPDATES
            await platform_bot.set_webhook(
                f"{WEBHOOK_BASE}/webhook/platform", drop_pending_updates=False,
                secret_token=(platform_webhook_secret() or None),
                allowed_updates=PLATFORM_ALLOWED_UPDATES)
            logger.warning("وبهوکِ ربات‌ساز با secret_token دوباره ثبت شد")
        except Exception:
            logger.exception("ثبتِ دوبارهٔ وبهوکِ ربات‌ساز ناموفق بود")
    spawn(_reset())


@app.post("/webhook/platform")
async def platform_webhook(request: Request):
    # === PATCH_PLATFORM_WEBHOOK_SECRET_V1 === فقط آپدیت‌هایِ واقعیِ تلگرام
    from config import platform_webhook_request_ok
    if not platform_webhook_request_ok(request):
        # اگر وبهوک هنوز بدونِ secret ثبت مانده باشد (مثلاً set_webhookِ استارت‌آپ
        # شکست خورده)، آپدیت‌هایِ واقعی هم رد می‌شوند؛ پس یک ثبتِ دوباره (حداکثر
        # هر ۵ دقیقه) زمان‌بندی می‌کنیم. تلگرام آپدیتِ ردشده را دوباره می‌فرستد.
        _schedule_platform_webhook_reset()
        return Response(status_code=403)
    if not platform_bot:
        return Response(status_code=503)
    try:
        data = await request.json()
        spawn(_process_platform(data))
    except Exception:
        import traceback
        print("platform webhook parse error:\n" + traceback.format_exc())
    return Response()


# === PATCH_PLATFORM_APAY_V1 ===
@app.post("/apay/platform/webhook")
async def apay_platform_webhook(request: Request):
    """وب‌هوکِ آپی برایِ کیف پولِ بات‌ساز؛ تسویه فقط با verify ِ خودِ آپی."""
    from platform_apay import handle_webhook
    raw = await request.body()
    try:
        code = await handle_webhook(raw, request.headers.get("X-APay-Timestamp", ""),
                                    request.headers.get("X-APay-Signature", ""))
    except Exception:
        logger.exception("platform apay webhook error")
        code = 500
    return Response(status_code=code)


@app.post("/blu/callback")
async def blu_callback(request: Request):
    """وبهوکِ درگاه بلو (blupal.net).

    === PATCH_BLU_PLATFORM_V1 ===
    ⚠️ بلو وبهوک را امضا نمی‌کند و آدرسش در داشبوردِ خودشان ثبت می‌شود، پس
    بدنهٔ این درخواست ذاتاً غیرقابلِ اعتماد است. اینجا فقط invoice_id خوانده
    می‌شود تا بدانیم کدام فاکتور را باید *مستقیماً از خودِ بلو* استعلام کنیم؛
    تاییدِ واقعی و شارژ در settle_charge انجام می‌شود.

    ضمناً مستنداتشان می‌گوید وبهوک ممکن است چند بار ارسال شود و باید ظرفِ ۱۰
    ثانیه ۲۰۰ برگردانیم — settle_charge با قفلِ اتمی idempotent است و اینجا
    هم سریع پاسخ می‌دهیم.
    """
    from models import PlatformBluCharge
    from sqlalchemy import select as _select
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    invoice_id = (data or {}).get("invoice_id")
    if not invoice_id:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    try:
        async with Session() as s:
            ch = await s.scalar(_select(PlatformBluCharge).where(
                PlatformBluCharge.invoice_id == int(invoice_id)))
        if ch and ch.status != "PAID":
            tg_id = ch.user_telegram_id
            from platform_blu import settle_charge
            ok, _note, credit = await settle_charge(int(invoice_id))
            if ok and credit > 0 and platform_bot:
                try:
                    await platform_bot.send_message(
                        tg_id,
                        "✅ <b>پرداختت تایید شد!</b>\n\n"
                        f"مبلغِ <b>{int(credit):,} تومان</b> به کیف پولت اضافه شد.")
                except Exception:
                    logger.exception("blu: notify failed")
    except Exception:
        import traceback
        print("blu callback error:\n" + traceback.format_exc())
    return JSONResponse({"received": True})


@app.post("/tonpays/platform/callback")
async def tonpays_platform_callback(request: Request):
    """کال‌بکِ TonPays برایِ کیف پولِ بات‌ساز.

    === PATCH_TONPAYS_PLATFORM_V1 ===
    ⚠️ الگوریتمِ هدرِ X-TonPays-Signature در مستندات نیامده، پس بدنه ذاتاً
    غیرقابلِ اعتماد است. (PATCH_TONPAYS_DOCS_V2: هدرِ X-API-Key باید با کلیدِ
    ذخیره‌شده بخواند، وگرنه بدونِ استعلام رد می‌شود.)
    اینجا فقط order_id خوانده می‌شود تا بدانیم کدام فاکتور را باید *مستقیماً از
    خودِ TonPays* استعلام کنیم؛ تاییدِ واقعی و شارژ در settle_charge انجام
    می‌شود. یعنی کال‌بکِ جعلی حداکثر یک استعلامِ اضافه می‌سازد.
    """
    from models import PlatformTonPaysCharge
    from sqlalchemy import select as _select
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    order_id = (data or {}).get("order_id") or ""
    invoice_id_cb = (data or {}).get("invoice_id") or ""  # PATCH_TONPAYS_DOCS_V2
    if not order_id and not invoice_id_cb:
        return Response()
    try:
        async with Session() as s:
            if order_id:
                ch = await s.scalar(_select(PlatformTonPaysCharge).where(
                    PlatformTonPaysCharge.order_id == order_id))
            else:
                ch = await s.scalar(_select(PlatformTonPaysCharge).where(
                    PlatformTonPaysCharge.invoice_id == invoice_id_cb))
        if not ch or ch.status == "paid":
            return Response()
        order_id = ch.order_id
        tg_id = ch.user_telegram_id
        # === PATCH_TONPAYS_DOCS_V2 ===
        import hmac as _hmac
        from platform_bot import get_pset as _gp
        # === PATCH_TONPAYS_CUSTOM_V1 === فاکتورِ کاستوم (پیشوندِ pc) با کلیدِ کاستوم
        from tenant.gateways.tonpays import is_custom_order as _tp_is_custom
        _stored = await _gp("ptonpays_custom_key" if _tp_is_custom(order_id) else "ptonpays_api_key", "")
        _hdr = request.headers.get("x-api-key", "")
        if not _stored or not _hmac.compare_digest(_hdr.encode(), _stored.encode()):
            logger.warning("tonpays platform callback: X-API-Key نمی‌خواند (order=%s)", order_id)
            return Response(status_code=401)
        from platform_tonpays import settle_charge
        ok, _note, credit = await settle_charge(order_id)
        if ok and credit > 0 and platform_bot:
            try:
                await platform_bot.send_message(
                    tg_id,
                    "✅ <b>پرداختت تایید شد!</b>\n\n"
                    f"مبلغِ <b>{int(credit):,} تومان</b> به کیف پولت اضافه شد.")
            except Exception:
                logger.exception("tonpays platform: notify failed")
    except Exception:
        import traceback
        print("tonpays platform callback error:\n" + traceback.format_exc())
    return Response()


@app.post("/nowpay/platform/ipn")
async def nowpay_platform_ipn(request: Request):
    """IPN اختصاصی NOWPayments برای شارژ کیف پول خودِ بات‌ساز (PlatformUser)."""
    from tenant.gateways.crypto_pay import verify_ipn_signature
    from models import PlatformTxn, PlatformUser
    from platform_bot import get_pset
    from sqlalchemy import select as _select
    try:
        raw = await request.body()
        sig = request.headers.get("x-nowpayments-sig", "")
        data = await request.json()
        order_id = data.get("order_id", "")
        pay_status = (data.get("payment_status") or "").lower()
        if not order_id or not order_id.startswith("plt_"):
            return Response()
        parts = order_id.split("_")
        if len(parts) < 3:
            return Response()
        tg_id = int(parts[1])
        ipn_secret = await get_pset("platform_nowpay_ipn_secret", "")
        if ipn_secret and not verify_ipn_signature(raw, sig, ipn_secret):
            print("platform nowpay IPN: bad signature for", order_id)
            return Response(status_code=403)
        if pay_status not in ("finished", "confirmed"):
            return Response()
        if not ipn_secret:
            # === PATCH_NOWPAY_IPN_NO_SECRET_V1 ===
            # بدونِ IPN secret قبلاً هیچ امضایی چک نمی‌شد و هر کسی با یک POST
            # «payment_status=finished» فاکتورِ پرداخت‌نشدهٔ خودش را شارژ می‌کرد.
            # حالا وضعیت مستقیماً از خودِ NOWPayments استعلام می‌شود.
            from tenant.gateways.crypto_pay import get_payment_status
            _api_key = await get_pset("platform_nowpay_api_key", "")
            _pid = data.get("payment_id")
            _real = await get_payment_status(_api_key, _pid) if (_api_key and _pid) else None
            if (not isinstance(_real, dict)
                    or str(_real.get("order_id") or "") != order_id
                    or (_real.get("payment_status") or "").lower() not in ("finished", "confirmed")):
                logger.warning("platform nowpay IPN بدونِ secret و تأییدنشده از API رد شد: %s", order_id)
                return Response(status_code=403)
        async with Session() as s:
            pending = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.user_telegram_id == tg_id,
                    PlatformTxn.kind == "nowpay_pending",
                    PlatformTxn.note.contains(f"order_id={order_id}"),
                ).with_for_update())
            if not pending:
                return Response()
            note = pending.note or ""
            credit = 0
            for part in note.split("|"):
                if part.startswith("toman="):
                    try:
                        credit = int(part.split("=")[1])
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            fee_pct = float(await get_pset("platform_nowpay_fee_percent", "0") or "0")
            if fee_pct > 0:
                credit = int(credit * (1 - fee_pct / 100))
            if credit <= 0:
                return Response()
            pu = await s.scalar(_select(PlatformUser).where(
                PlatformUser.telegram_id == tg_id).with_for_update())
            if pu:
                pu.balance = float(pu.balance or 0) + credit
            s.add(PlatformTxn(
                user_telegram_id=tg_id,
                amount=credit,
                kind="nowpay_charge",
                note=f"شارژ با ارز دیجیتال — order: {order_id}",
            ))
            pending.kind = "nowpay_done"
            await s.commit()
        try:
            from tenant.gateways.wallet_receipt import tehran_date_time
            jdate, jtime = tehran_date_time()
            bal_line = f"👛 موجودی فعلی: <b>{pu.balance:,.0f} تومان</b>\n" if pu else ""
            await platform_bot.send_message(
                tg_id,
                "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\n"
                "┈┅━━⊷⊰💳⊱⊶━━┅┈\n"
                f"💰 مبلغ واریزی: <b>{credit:,} تومان</b>\n"
                f"{bal_line}"
                "🧾 روش پرداخت: 🪙 ارز دیجیتال (NOWPayments)\n"
                f"📅 تاریخ: {jdate}   🕒 ساعت: {jtime}\n"
                "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
                "🙏 ممنون از اعتمادت! برای دیدنِ موجودی، «💰 کیف پول» را بزن.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except Exception:
        import traceback
        print("platform nowpay IPN error:\n" + traceback.format_exc())
    return Response()


def _zarinpal_result_page(ok: bool, title: str, body: str) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    icon = "✅" if ok else "❌"
    html = f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: Tahoma, sans-serif; background:#0f172a; color:#e2e8f0;
       display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
.card {{ background:#1e293b; padding:32px; border-radius:16px; text-align:center; max-width:420px; }}
h1 {{ color:{color}; font-size:22px; }}
p {{ color:#94a3b8; line-height:1.8; }}
</style></head>
<body><div class="card"><h1>{icon} {title}</h1><p>{body}</p></div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/zarinpal/platform/callback")
async def zarinpal_platform_callback(request: Request):
    """بازگشتِ کاربر از درگاهِ زرین‌پال بعدِ پرداخت (یا انصراف) — شارژِ کیف‌پولِ
    خودِ بات‌ساز (PlatformUser). چون این یک ریدایرکتِ مرورگر است نه IPN،
    استعلام همین‌جا انجام می‌شود؛ برای اطمینانِ بیشتر، دکمهٔ «بررسیِ پرداخت»
    توی خودِ ربات هم همین مسیر را از سمتِ کاربر تکرار می‌کند."""
    import platform_zarinpal
    from sqlalchemy import select as _select
    from models import PlatformTxn
    from platform_bot import get_pset, get_user
    authority = request.query_params.get("Authority", "")
    status = request.query_params.get("Status", "")
    if not authority:
        return _zarinpal_result_page(False, "پرداخت نامعتبر", "شناسهٔ تراکنش پیدا نشد.")

    try:
        async with Session() as s:
            pending = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "zarinpal_pending",
                    PlatformTxn.note.contains(f"authority={authority}"),
                ))
            already = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "zarinpal_charge",
                    PlatformTxn.note.contains(f"authority={authority}"),
                ))
        if already:
            return _zarinpal_result_page(
                True, "قبلاً تأیید شده", "این پرداخت قبلاً تأیید و کیف‌پولت شارژ شده. می‌توانی به ربات برگردی.")
        if not pending:
            return _zarinpal_result_page(
                False, "تراکنش پیدا نشد", "این تراکنش در سیستم ثبت نشده. با پشتیبانی تماس بگیر.")

        if status != "OK":
            return _zarinpal_result_page(
                False, "پرداخت لغو شد", "پرداخت توسط شما لغو شد. می‌توانی به ربات برگردی و دوباره تلاش کنی.")

        tg_id = pending.user_telegram_id
        amount_toman = int(float(pending.amount))
        mid = await get_pset("platform_zarinpal_merchant_id", "")
        ok, res = await platform_zarinpal.verify_payment(mid, amount_toman, authority)
        if not ok:
            return _zarinpal_result_page(
                False, "تأیید پرداخت ناموفق", f"جزئیات: {res}\nاگر مبلغ کسر شده، از دکمهٔ «بررسی پرداخت» توی ربات استفاده کن.")

        async with Session() as s:
            # === PATCH_ZARINPAL_CALLBACK_ONCE_V1 ===
            # اول ردیفِ pending قفل و مصرف می‌شود، بعد شارژ. قبلاً شارژ بی‌قید انجام
            # می‌شد؛ دو درخواستِ هم‌زمان (رفرشِ صفحه + دکمهٔ «بررسی پرداخت») هر دو
            # verify موفق (۱۰۰ و ۱۰۱) می‌گرفتند و کیف پول دوبار شارژ می‌شد.
            p2 = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "zarinpal_pending",
                    PlatformTxn.note.contains(f"authority={authority}"),
                ).with_for_update())
            if not p2:
                return _zarinpal_result_page(
                    True, "قبلاً تأیید شده", "این پرداخت قبلاً تأیید و کیف‌پولت شارژ شده. می‌توانی به ربات برگردی.")
            p2.kind = "zarinpal_pending_done"
            u = await get_user(s, tg_id, for_update=True)
            u.balance = float(u.balance) + amount_toman
            s.add(PlatformTxn(
                user_telegram_id=tg_id, amount=float(amount_toman),
                kind="zarinpal_charge", note=f"authority={authority}|ref_id={res}"))
            new_bal = u.balance
            await s.commit()

        try:
            await platform_bot.send_message(
                tg_id,
                "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\n"
                f"💰 مبلغ واریزی: <b>{amount_toman:,} تومان</b>\n"
                f"👛 موجودی فعلی: <b>{new_bal:,.0f} تومان</b>\n"
                "🧾 روش پرداخت: 🏦 زرین‌پال")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

        return _zarinpal_result_page(
            True, "پرداخت موفق", "کیف‌پولت شارژ شد. می‌توانی این صفحه را ببندی و به ربات برگردی.")
    except Exception:
        import traceback
        print("zarinpal callback error:\n" + traceback.format_exc())
        return _zarinpal_result_page(
            False, "خطای غیرمنتظره", "مشکلی پیش آمد. اگر مبلغ کسر شده، از دکمهٔ «بررسی پرداخت» توی ربات استفاده کن.")


# === PATCH_PLATFORM_IDPAY_GATEWAY_V1 ===
@app.get("/idpay/platform/callback")
async def idpay_platform_callback(request: Request):
    """بازگشتِ کاربر از درگاهِ آیدی‌پی بعدِ پرداخت (یا انصراف) — شارژِ کیف‌پولِ
    خودِ بات‌ساز (PlatformUser). آیدی‌پی با query paramsِ id/order_id/status
    برمی‌گردونه؛ چون این یک ریدایرکتِ مرورگر است نه IPN، استعلام همین‌جا
    انجام می‌شود؛ برای اطمینانِ بیشتر، دکمهٔ «بررسیِ پرداخت» توی خودِ ربات
    هم همین مسیر را از سمتِ کاربر تکرار می‌کند."""
    import platform_idpay
    from sqlalchemy import select as _select
    from models import PlatformTxn
    from platform_bot import get_pset, get_user
    tx_id = request.query_params.get("id", "")
    order_id = request.query_params.get("order_id", "")
    if not tx_id or not order_id:
        return _zarinpal_result_page(False, "پرداخت نامعتبر", "شناسهٔ تراکنش پیدا نشد.")

    try:
        async with Session() as s:
            pending = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "idpay_pending",
                    PlatformTxn.note.contains(f"order_id={order_id}"),
                ))
            already = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "idpay_charge",
                    PlatformTxn.note.contains(f"order_id={order_id}"),
                ))
        if already:
            return _zarinpal_result_page(
                True, "قبلاً تأیید شده", "این پرداخت قبلاً تأیید و کیف‌پولت شارژ شده. می‌توانی به ربات برگردی.")
        if not pending:
            return _zarinpal_result_page(
                False, "تراکنش پیدا نشد", "این تراکنش در سیستم ثبت نشده. با پشتیبانی تماس بگیر.")

        # === PATCH_PLATFORM_IDPAY_GATEWAY_V1 ===
        # برخلافِ زرین‌پال، آیدی‌پی تویِ ریدایرکتِ GET پارامترِ «وضعیت/لغو»
        # مستندی نداره (فقط id و order_id) — پس تشخیصِ موفقیت/لغو صرفاً از
        # رویِ نتیجهٔ خودِ verify انجام می‌شه، نه حدس‌زدنِ یه پارامترِ دیگه.
        tg_id = pending.user_telegram_id
        api_key = await get_pset("platform_idpay_api_key", "")
        ok, res, verified_amount = await platform_idpay.verify_payment(api_key, tx_id, order_id)
        if not ok:
            return _zarinpal_result_page(
                False, "تأیید پرداخت ناموفق", f"جزئیات: {res}\nاگر مبلغ کسر شده، از دکمهٔ «بررسی پرداخت» توی ربات استفاده کن.")

        amount_toman = verified_amount or int(float(pending.amount))
        async with Session() as s:
            # === PATCH_GATEWAY_CREDIT_ONCE_V1 === اول مصرفِ pending (با قفل)، بعد شارژ
            p2 = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.kind == "idpay_pending",
                    PlatformTxn.note.contains(f"order_id={order_id}"),
                ).with_for_update())
            if not p2:
                return _zarinpal_result_page(
                    True, "قبلاً تأیید شده", "این پرداخت قبلاً تأیید و کیف‌پولت شارژ شده. می‌توانی به ربات برگردی.")
            p2.kind = "idpay_pending_done"
            u = await get_user(s, tg_id, for_update=True)
            u.balance = float(u.balance) + amount_toman
            s.add(PlatformTxn(
                user_telegram_id=tg_id, amount=float(amount_toman),
                kind="idpay_charge", note=f"order_id={order_id}|track_id={res}"))
            new_bal = u.balance
            await s.commit()

        try:
            await platform_bot.send_message(
                tg_id,
                "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\n"
                f"💰 مبلغ واریزی: <b>{amount_toman:,} تومان</b>\n"
                f"👛 موجودی فعلی: <b>{new_bal:,.0f} تومان</b>\n"
                "🧾 روش پرداخت: 🏧 آیدی‌پی")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

        return _zarinpal_result_page(
            True, "پرداخت موفق", "کیف‌پولت شارژ شد. می‌توانی این صفحه را ببندی و به ربات برگردی.")
    except Exception:
        import traceback
        print("idpay callback error:\n" + traceback.format_exc())
        return _zarinpal_result_page(
            False, "خطای غیرمنتظره", "مشکلی پیش آمد. اگر مبلغ کسر شده، از دکمهٔ «بررسی پرداخت» توی ربات استفاده کن.")


@app.get("/health")
async def health():
    return {"ok": True}


# === PATCH_SERVER_STATUS_V1 === مینی‌اپِ «وضعیتِ سرورها» (عمومی؛ بدونِ هیچ منبعِ خارجی تا در ایران هم باز شود)
@app.get("/status", response_class=HTMLResponse)
async def server_status_page():
    from server_status_page import PAGE
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/status/api")
async def server_status_api():
    from server_status import build_report
    try:
        return JSONResponse(await build_report(), headers={"Cache-Control": "no-store"})
    except Exception:
        logger.exception("server_status: ساختِ گزارش ناموفق بود")
        return JSONResponse({"error": "unavailable"}, status_code=503)


# === PATCH_PLATFORM_SMS_AUTOCHARGE_V1 ===
@app.post("/sms/platform/deposit/{secret}")
async def sms_platform_deposit(secret: str, request: Request):
    """وبهوکِ فورواردِ پیامکِ بانکی برایِ شارژِ خودکارِ کیفِ پولِ پلتفرم با
    مبلغِ اختصاصی. اپِ فورواردرِ پیامک (رویِ گوشیِ متصل به حسابِ بانکی)
    این آدرس رو با متنِ خامِ هر پیامکِ واریزی صدا می‌زنه.

    چون فرمتِ دقیقِ بدنه‌ی درخواست بینِ اپ‌هایِ مختلفِ فورواردر فرق می‌کنه
    (بعضی JSON، بعضی form، بعضی فقط query)، این‌جا هر سه حالت امتحان
    می‌شه و متنِ پیامک از رویِ چند اسمِ رایجِ فیلد (text/message/body/sms)
    خونده می‌شه — نه یه فرمتِ سخت‌گیرانه‌یِ واحد."""
    from platform_bot import get_pset, _sms_extract_rial_amounts, _sms_match_and_credit
    import hmac as _hmac
    expected = await get_pset("platform_sms_secret", "")
    if not expected or not _hmac.compare_digest(secret.encode(), expected.encode()):
        return JSONResponse(status_code=403, content={"ok": False, "error": "invalid secret"})
    if (await get_pset("platform_sms_enabled", "0")) != "1":
        return JSONResponse(status_code=200, content={"ok": False, "error": "disabled"})

    text = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            for k in ("text", "message", "body", "sms", "content", "msg"):
                if body.get(k):
                    text = str(body[k])
                    break
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not text:
        try:
            form = await request.form()
            for k in ("text", "message", "body", "sms", "content", "msg"):
                if form.get(k):
                    text = str(form[k])
                    break
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not text:
        for k in ("text", "message", "body", "sms", "content", "msg"):
            v = request.query_params.get(k)
            if v:
                text = v
                break

    if not text:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no sms text found in request"})

    # === PATCH_SMS_DEPOSIT_HARDEN_V1 ===
    # اگر ادمین فهرستِ فرستنده‌هایِ مجاز (شمارهٔ/نامِ پیامکیِ بانک، با کاما) را در
    # platform_sms_senders گذاشته باشد، پیامکِ هر فرستندهٔ دیگری — مثلاً پیامکِ جعلی
    # که کسی مستقیم به همان گوشی فرستاده — نادیده گرفته می‌شود.
    allowed = [x.strip().lower() for x in (await get_pset("platform_sms_senders", "")).split(",") if x.strip()]
    if allowed:
        sender = ""
        for src in (locals().get("body") if isinstance(locals().get("body"), dict) else {},
                    request.query_params):
            for k in ("from", "sender", "number", "phone", "address", "originator"):
                if src.get(k):
                    sender = str(src.get(k))
                    break
            if sender:
                break
        if sender.strip().lower().lstrip("+") not in {a.lstrip("+") for a in allowed}:
            logger.warning("sms deposit: فرستندهٔ غیرمجاز نادیده گرفته شد: %r", sender[:40])
            return JSONResponse(status_code=200, content={"ok": False, "error": "sender not allowed"})

    amounts = _sms_extract_rial_amounts(text)
    matched_any = False
    for amt in amounts:
        if await _sms_match_and_credit(amt):
            matched_any = True
    return JSONResponse(status_code=200, content={"ok": True, "matched": matched_any, "amounts_seen": amounts})


@app.get("/pcard-view")
async def platform_card_miniapp(request: Request):
    """مینی‌اپِ نمایشِ شماره‌کارت برایِ پرداختِ کارت‌به‌کارتِ کیف‌پولِ پلتفرم.
    شماره‌کارت/نام/مبلغ از کوئری‌استرینگ می‌آید (همون چیزی که تویِ پیامِ ربات
    نشون داده شده) تا دقیقاً همون کارتی که تو چت دیده، تو مینی‌اپ هم ببینه —
    نه یه انتخابِ تصادفیِ تازه که ممکنه با کارتِ دوم فرق کنه.

    === PATCH_PLATFORM_CARD_MINIAPP_REDESIGN_V1 ===
    طراحیِ کامل عوض شد: کارت به‌شکلِ واقعیِ یه کارتِ بانکی (نسبتِ ابعادِ
    ISO 7810، تراشه، فونتِ اعشاریِ اسپیس‌دار برایِ شماره) به‌عنوانِ المانِ
    اصلیِ صفحه، پالتِ رسمی/معتبر (سرمه‌ای تیره + برنزیِ گرم به‌جایِ
    گرادیانِ بنفشِ عمومی). دکمهٔ «پرداخت کردم» کاربر رو برمی‌گردونه به
    چتِ ربات (چون آپلودِ عکس از داخلِ خودِ مینی‌اپ ممکن نیست) تا همون‌جا
    مرحلهٔ ارسالِ رسید ادامه پیدا کنه — دقیقاً هماهنگ با state موجودِ
    Charge.sender_card که از قبل منتظرِ ۴ رقمِ آخر/رسیده."""
    import html as _h
    card = _h.escape((request.query_params.get("card") or "").strip())
    holder = _h.escape((request.query_params.get("holder") or "").strip())
    amount = _h.escape((request.query_params.get("amount") or "").strip())
    card_spaced = " ".join(card[i:i+4] for i in range(0, len(card), 4)) if card else "کارت ثبت نشده"
    amount_line = (f'<p class="ctx">مبلغِ موردِ نظر برایِ واریز: <b>{amount} تومان</b></p>'
                  if amount else "")
    html = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>واریز کارت‌به‌کارت</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<style>
  @font-face {{
    font-family: 'Vazirmatn';
    src: url('https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/Vazirmatn-Regular.woff2') format('woff2');
    font-weight: 400; font-display: swap;
  }}
  @font-face {{
    font-family: 'Vazirmatn';
    src: url('https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/Vazirmatn-Medium.woff2') format('woff2');
    font-weight: 500; font-display: swap;
  }}
  @font-face {{
    font-family: 'Vazirmatn';
    src: url('https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/Vazirmatn-Bold.woff2') format('woff2');
    font-weight: 700; font-display: swap;
  }}
  :root {{
    --paper: #EFEBE2; --ink: #14161D; --ink2: #1F2330; --brass: #B9863C;
    --brass-dark: #93672B; --muted: #6B6E76; --good: #2E7A57; --line: #DCD6C8;
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ height: 100%; }}
  body {{
    margin: 0; padding: 22px 18px calc(22px + env(safe-area-inset-bottom));
    font-family: 'Vazirmatn', -apple-system, "Segoe UI", Tahoma, sans-serif;
    background: var(--paper); color: var(--ink);
    display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
    min-height: 100%;
  }}
  .ctx {{ font-size: 14px; color: var(--muted); margin: 4px 0 22px; text-align: center; }}
  .ctx b {{ color: var(--ink); font-weight: 700; }}

  .cardwrap {{ width: 100%; max-width: 360px; perspective: 900px; }}
  .card {{
    position: relative; width: 100%; aspect-ratio: 1.586 / 1;
    border-radius: 18px; overflow: hidden;
    background: linear-gradient(155deg, var(--ink2) 0%, var(--ink) 65%);
    box-shadow: 0 18px 34px -14px rgba(20,22,29,.55);
    padding: 20px 22px; color: #F3EFE4;
    opacity: 0; transform: translateY(14px) scale(.98);
    animation: rise .5s cubic-bezier(.2,.8,.2,1) .05s forwards;
  }}
  @keyframes rise {{ to {{ opacity: 1; transform: none; }} }}
  .card::after {{
    content: ""; position: absolute; inset: 0;
    background: radial-gradient(120% 90% at 85% -10%, rgba(185,134,60,.22), transparent 55%);
    pointer-events: none;
  }}
  .chip {{ width: 38px; height: 28px; border-radius: 6px;
    background: linear-gradient(155deg,#E9C989,#B9863C 60%,#8C6329);
    position: relative; }}
  .chip::before {{ content: ""; position: absolute; inset: 5px;
    border: 1px solid rgba(20,16,8,.35); border-radius: 3px; }}
  .brand {{ position: absolute; top: 20px; left: 22px; font-size: 12px;
    letter-spacing: .04em; color: rgba(243,239,228,.55); }}
  .num {{
    position: relative; z-index: 1; direction: ltr; text-align: left;
    font: 500 21px/1.3 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
    letter-spacing: .06em; font-variant-numeric: tabular-nums;
    margin-top: 30px; word-spacing: .18em;
  }}
  .holder {{ position: relative; z-index: 1; margin-top: 16px;
    font-size: 13px; color: rgba(243,239,228,.8); }}

  .actions {{ width: 100%; max-width: 360px; margin-top: 22px; }}
  .btn {{
    width: 100%; padding: 15px; border-radius: 13px; font-size: 15.5px;
    font-family: inherit; font-weight: 600; cursor: pointer; border: none;
    transition: transform .12s ease;
  }}
  .btn:active {{ transform: scale(.97); }}
  .btn + .btn {{ margin-top: 10px; }}
  .btn-ghost {{
    background: transparent; color: var(--ink); border: 1.5px solid var(--line);
  }}
  .btn-solid {{ background: var(--brass); color: #1D1305; }}
  .btn-solid:active {{ background: var(--brass-dark); }}

  .note {{ max-width: 360px; margin-top: 18px; text-align: center;
    font-size: 12.5px; line-height: 1.9; color: var(--muted); }}

  .toast {{
    position: fixed; bottom: calc(22px + env(safe-area-inset-bottom)); left: 50%;
    transform: translateX(-50%) translateY(8px); background: var(--ink); color: #F3EFE4;
    padding: 10px 18px; border-radius: 11px; font-size: 13.5px;
    opacity: 0; pointer-events: none; transition: opacity .2s, transform .2s;
  }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}

  @media (prefers-reduced-motion: reduce) {{
    .card {{ animation: none; opacity: 1; transform: none; }}
    .btn, .toast {{ transition: none; }}
  }}
</style>
</head>
<body>
  {amount_line}

  <div class="cardwrap">
    <div class="card">
      <div class="brand">بات‌ساز · واریز مستقیم</div>
      <div class="chip"></div>
      <div class="num" id="cardnum">{card_spaced}</div>
      <div class="holder">{holder or "—"}</div>
    </div>
  </div>

  <div class="actions">
    <button class="btn btn-ghost" onclick="copyCard()">کپیِ شماره کارت</button>
    <button class="btn btn-solid" onclick="paidClicked()">واریز کردم</button>
  </div>

  <p class="note">بعد از زدنِ «واریز کردم» به ربات برمی‌گردی تا رسید یا عکسِ پرداخت را همان‌جا بفرستی.</p>

  <div class="toast" id="toast"></div>

<script>
  const tg = window.Telegram?.WebApp;
  if (tg) {{ tg.ready(); tg.expand(); }}
  function showToast(t) {{
    const el = document.getElementById('toast');
    el.textContent = t; el.classList.add('show');
    setTimeout(() => el.classList.remove('show'), 1600);
  }}
  function copyCard() {{
    const raw = "{card}";
    navigator.clipboard.writeText(raw).then(() => showToast('شماره کارت کپی شد'))
      .catch(() => showToast('کپی نشد — دستی انتخاب کن'));
  }}
  function paidClicked() {{
    if (tg && tg.sendData) {{
      tg.sendData(JSON.stringify({{ action: "paid" }}));
      tg.close();
    }} else {{
      showToast('حالا به ربات برگرد و ادامه بده');
    }}
  }}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ───────────────────────────── استارتاپ ─────────────────────────────

async def on_startup():
    await init_db()
    try:
        from plans import reload_plan_features
        async with Session() as s:
            await reload_plan_features(s)
    except Exception as e:
        print("reload_plan_features error:", e)
    try:
        from platform_bot import load_plans_paid
        await load_plans_paid()
    except Exception as e:
        print("load_plans_paid error:", e)
    if platform_bot and WEBHOOK_BASE:
        # === PATCH_FAKE_JOIN_GUARD_V1 ===
        from webhook_utils import PLATFORM_ALLOWED_UPDATES
        # === PATCH_WEBHOOK_SETUP_SINGLE_WORKER_V1 ===
        # باگِ کشف‌شده هنگامِ راه‌اندازیِ چندورکری: قبلاً هر ورکر در
        # استارت‌آپِ خودش set_webhook را صدا می‌زد — با ۲+ ورکر که تقریباً
        # هم‌زمان بالا می‌آیند، تلگرام برایِ متدِ SetWebhook محدودیتِ
        # ضدِسیل (flood control) اعمال می‌کند، ورکرِ دوم با
        # TelegramRetryAfter کرش می‌کرد (این خطا جایی گرفته نمی‌شد)، و
        # چون uvicorn ورکرِ مرده را دوباره بالا نمی‌آورد، آن ورکر برایِ
        # همیشه از بین می‌رفت. حالا با یک قفلِ Redis فقط اولین ورکر
        # واقعاً webhook را ثبت می‌کند (که کاملاً هم کافی است — webhook
        # یک تنظیمِ سراسری است، نیازی نیست هر ورکر جدا ثبتش کند)، و کلِ
        # فراخوانی هم داخلِ try/except است تا حتی اگر روزی باز خطایی داد،
        # دیگر هرگز استارت‌آپِ کلِ ورکر را نکشد.
        try:
            got_lock = True
            try:
                got_lock = bool(await platform_dp.storage.redis.set(
                    "webhook_setup_lock:platform", "1", nx=True, ex=30))
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قفلِ Redisِ webhook در دسترس نبود)")
            if got_lock:
                await platform_bot.set_webhook(
                    f"{WEBHOOK_BASE}/webhook/platform", drop_pending_updates=True,
                    secret_token=(platform_webhook_secret() or None),
                    allowed_updates=PLATFORM_ALLOWED_UPDATES)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (set_webhook ناموفق بود — استارت‌آپ ادامه پیدا می‌کند)")
    if platform_bot:
        global _platform_bot_username
        try:
            me = await platform_bot.get_me()
            _platform_bot_username = me.username
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_PLATFORM_BG_LOOPS_MOVED_TO_WORKER_V1 ===
    # قبلاً این ۸ حلقه (backup_loop و بقیه) همین‌جا، تو پروسه‌ی HTTP، اسپان
    # می‌شدن. مشکل: بعضی از این حلقه‌ها (platform_swap_poll_loop,
    # crypto_poll_loop_platform, tonpays_platform_poll_loop, blu_poll_loop, ...)
    # با سرویس‌های بیرونیِ کند کار می‌کنن، و چون همه رو یه event loop اجرا
    # می‌شدن، یه سرویسِ کند می‌تونست پاسخ‌دهیِ webhookِ platform_bot رو هم
    # عقب بندازه — دقیقاً همون الگویی که رو main_tenant.py دیدیم و فیکس شد.
    #
    # راه‌حل: این حلقه‌ها دیگه اینجا اسپان نمی‌شن. یه پروسه‌ی کاملاً مجزا
    # (worker_platform.py, سرویسِ systemدِ جداگانه‌ی botsaz-platform-worker)
    # اجراشون می‌کنه.
    pass


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await on_startup()
    yield


app.router.lifespan_context = _lifespan
