"""
نقطهٔ ورود پلتفرم: یک سرور وبهوک که آپدیت‌ها را بین ربات مادر و
ربات‌های مستأجر بر اساس توکنِ داخل آدرس مسیریابی می‌کند.

  /webhook/platform        → ربات بات‌ساز (مادر)
  /webhook/{tenant_token}  → ربات مستأجرِ متناظر با آن توکن
"""
import asyncio
import datetime
import os
from fa_num import fa_days  # PATCH_FA_DAYS_V1
import shutil

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ButtonStyle
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Update, InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request, Response
from sqlalchemy import select, update

from config import (PLATFORM_BOT_TOKEN, WEBHOOK_BASE, Session, init_db,
                    ARCHIVE_DAYS, PAID_PLAN_ARCHIVE_DAYS,
                    archive_retention_days,
                    purge_bot, PLATFORM_ADMIN_ID, DATABASE_URL)
from models import Bot as BotRow
from models import VpnPanel, PanelAccount, Setting
from platform_bot import (
    router as platform_router, swap_platform_poll_once, BRAND_BIO, ptc_daily_snapshot_loop,
    _temp_bot,
)
from tenant import tenant_router, swap_poll_loop, fallback_router
from tenant.shared.ad_broadcast import ad_broadcast_loop, ad_scheduled_posts_loop
from tenant.shared.web_view import web_router
from tenant.gateways.payment_page import pay_router
from tenant.shared.tutorial_page import tutorial_router
from tenant.shared.file_link import file_link_web_router  # === PATCH_FILE_LINK_CONVERTER_V1 ===

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

# همهٔ ربات‌ها پیش‌فرض پیام‌ها را با HTML می‌فرستند (رفع باگ بولد)
DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)

app = FastAPI()
app.include_router(web_router)
app.include_router(pay_router)
app.include_router(tutorial_router)
app.include_router(file_link_web_router)  # === PATCH_FILE_LINK_CONVERTER_V1 ===

platform_dp = Dispatcher()
platform_dp.include_router(platform_router)

tenant_dp = Dispatcher()
tenant_dp.include_router(tenant_router)
tenant_dp.include_router(fallback_router)   # آخر از همه: خودترمیمی حالت‌های گیرکرده

platform_bot = Bot(token=PLATFORM_BOT_TOKEN, default=DEFAULTS) if PLATFORM_BOT_TOKEN else None

_tenant_bots: dict[str, Bot] = {}
_platform_bot_username: str | None = None  # کش یک‌باره در on_startup؛ برای ساختِ دیپ‌لینکِ t.me/<username>?start=...
# کشِ کوتاه‌مدتِ وضعیتِ ربات‌ها تا در هر تک‌آپدیت به دیتابیس کوئری نزنیم
# (زیرِ بار، این کوئری‌ها صف می‌شدند و باعثِ کندی و خطا می‌شدند).
# مقدار: token -> (status, expires_monotonic)
_tenant_status_cache: dict[str, tuple[str | None, float]] = {}
_TENANT_STATUS_TTL = 5.0  # ثانیه؛ تغییرِ وضعیت (قفل/خاموش/حذف) حداکثر با این تأخیر اعمال می‌شود


def _clear_local_tenant_cache(token: str | None = None) -> None:
    """فقط کشِ وضعیتِ همین پروسه را پاک می‌کند (بدونِ انتشارِ NOTIFY).
    این تابع را listenerِ tenant_status_bus هم صدا می‌زند تا حلقهٔ
    publish→receive→publish→... بی‌نهایت پیش نیاید."""
    if token:
        _tenant_status_cache.pop(token, None)
    else:
        _tenant_status_cache.clear()


def invalidate_tenant_status(token: str | None = None):
    """کشِ وضعیت را باطل می‌کند تا تغییرِ وضعیت (paused/locked/حذف) بلافاصله
    اعمال شود. اگر token داده نشود، کلِ کش پاک می‌شود.

    === PATCH_TENANT_STATUS_BUS_V1 ===
    قبلاً این تابع فقط کشِ همین پروسه را پاک می‌کرد؛ اگر بات‌ساز (پلتفرم) و
    بات‌های مستأجر روی دو پروسه/سرویسِ جدا اجرا شوند، پروسهٔ دیگر تا انقضای
    TTL (حداکثر ۵ ثانیه) بی‌خبر می‌ماند. الان علاوه بر پاک‌کردنِ محلی، یک
    NOTIFY هم (از طریقِ Postgres) به بقیهٔ پروسه‌ها می‌فرستد تا اثر همون
    لحظه، در همه‌جا اعمال شود. اگر ارسال به هر دلیلی (قطعیِ لحظه‌ایِ
    دیتابیس و…) ناموفق باشد، فقط لاگ می‌شود — کشِ ۵ثانیه‌ای هم‌چنان
    fallbackِ امن است و هیچ فلوی کاربری کرش نمی‌کند.
    """
    _clear_local_tenant_cache(token)
    try:
        import tenant_status_bus
        _spawn(tenant_status_bus.publish_tenant_invalidate(token))
    except RuntimeError:
        # هیچ event loopِ در-حالِ‌اجرایی نیست (مثلاً موقعِ import/تست خارج
        # از asyncio) — بی‌خیال شو، کشِ محلی که بالا پاک شد کافیست.
        pass
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (publish_tenant_invalidate)")


async def _get_tenant_bot(token: str) -> tuple[Bot | None, str | None]:
    """ربات مستأجر را همراه با وضعیتش برمی‌گرداند.
    وضعیت در هر درخواست تازه از دیتابیس خوانده می‌شود تا تغییرات (خاموش/روشن،
    قفل ادمین، توکن خراب) بلافاصله اعمال شوند (بدون وابستگی به کش).

    برای ربات‌های «paused» (موقتاً توسط مالک خاموش‌شده) و «locked» (قفل‌شده
    توسط ادمینِ پلتفرم) هم یک Bot معتبر برمی‌گردانیم — چون توکن هنوز درست
    است — تا بشود پیامِ مناسب را به کاربر داد؛ فقط برای توکن‌های واقعاً
    خراب/نامعتبر None می‌دهیم."""
    # وضعیت را از کشِ کوتاه‌مدت بخوان؛ فقط اگر منقضی شده به دیتابیس برو.
    now_mono = asyncio.get_event_loop().time()
    cached = _tenant_status_cache.get(token)
    if cached is not None and cached[1] > now_mono:
        status = cached[0]
    else:
        async with Session() as s:
            status = await s.scalar(select(BotRow.status).where(BotRow.token == token))
        _tenant_status_cache[token] = (status, now_mono + _TENANT_STATUS_TTL)
    if not status or status == "disabled_bad_token":
        _tenant_bots.pop(token, None)
        return None, status
    bot = _tenant_bots.get(token)
    if bot is None:
        bot = Bot(token=token, default=DEFAULTS)
        _tenant_bots[token] = bot
    return bot, status


def _platform_deep_link(payload: str) -> str | None:
    """لینکِ عمیقِ t.me/<بات‌ساز>?start=<payload> — برای دکمه‌ای که کاربر را
    مستقیم به بخشِ مشخصی از خودِ بات‌ساز (نه ربات مستأجر) می‌برد. اگر
    یوزرنیمِ بات‌ساز هنوز کش نشده باشد (مثلاً بلافاصله بعدِ استارت)، None
    برمی‌گرداند تا کدِ صدازننده بدونِ دکمه ادامه دهد."""
    if not _platform_bot_username:
        return None
    return f"https://t.me/{_platform_bot_username}?start={payload}"


# === PATCH_PARTNER_REACTIVATION_LINK_V1 ===
async def _reactivation_link(brow) -> str | None:
    """لینکِ «فعال‌سازی/بازگردانیِ ربات» را می‌سازد و به مقصدِ درست می‌فرستد:

    اگر این رباتِ مستأجر از طریقِ یک **ربات همکار** ساخته/فروخته شده باشد،
    کاربر باید به همان ربات همکار برود (نه بات‌سازِ اصلی) تا آنجا تمدید/فعال
    کند. با payloadِ pubot_<id> مستقیم روی صفحهٔ همان ربات در پنلِ «ربات‌های
    من»ِ همکار می‌نشیند. در غیر این صورت (ساختِ مستقیم از پلتفرم) همان لینکِ
    بات‌سازِ اصلی برگردانده می‌شود.
    """
    try:
        from models import PartnerSale, PartnerBot
        async with Session() as s:
            sale = await s.scalar(select(PartnerSale).where(
                PartnerSale.sold_bot_id == brow.id))
            if sale:
                pbot = await s.get(PartnerBot, sale.partner_bot_id)
                if pbot:
                    prow = await s.get(BotRow, pbot.bot_id)
                    if prow and prow.username:
                        return f"https://t.me/{prow.username}?start=pubot_{brow.id}"
    except Exception:
        logger.exception("ساختِ لینکِ فعال‌سازیِ ربات همکار ناموفق بود؛ به لینکِ پلتفرم برمی‌گردیم")
    # پیش‌فرض: رباتِ مستقیمِ پلتفرم
    return _platform_deep_link(f"mb_{brow.id}")


async def _maintenance_reply(bot: Bot, data: dict, text: str | None = None,
                             reply_markup: "InlineKeyboardMarkup | None" = None) -> None:
    """برای ربات‌های موقتاً خاموش‌شده (paused) یا قفل‌شده توسط ادمین (locked):
    به‌جای پردازشِ کاملِ آپدیت، فقط یک پیامِ کوتاه به کاربر می‌دهد — روی پیامِ
    معمولی (/start یا زدنِ دکمهٔ متنی) با ارسال پیام، و روی دکمهٔ اینلاین با
    یک Alert سبک (بدون نیاز به فرستادنِ پیامِ جدید). اگر reply_markup داده
    شود (مثلاً دکمهٔ راهنمای فعال‌سازی)، چون Alertِ تلگرام دکمه پشتیبانی
    نمی‌کند، به‌جای Alert یک پیامِ معمولی همراه با دکمه فرستاده می‌شود."""
    text = text or "🛠 ربات در حال به‌روزرسانی می‌باشد. لطفاً چند دقیقهٔ دیگر دوباره امتحان کنید."
    try:
        if "callback_query" in data:
            cq = data["callback_query"]
            cq_id = cq.get("id")
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if reply_markup and chat_id:
                if cq_id:
                    try:
                        await bot.answer_callback_query(cq_id)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
                return
            if cq_id:
                try:
                    await bot.answer_callback_query(cq_id, text=text, show_alert=True)
                    return
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            if chat_id:
                await bot.send_message(chat_id, text)
            return
        msg = data.get("message") or data.get("edited_message")
        if msg:
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def backup_loop():
    """هر ۶ ساعت یک بک‌آپِ فشرده از دیتابیس در پوشهٔ backups می‌گیرد
    (نگه‌داری ۲۸ نسخهٔ اخیر ≈ ۷ روز).

    === PATCH_POSTGRES_BACKUP_V1 ===
    قبلاً این تابع فقط برای SQLite کار می‌کرد (کپیِ مستقیمِ فایلِ botsaz.db).
    از وقتی دیتابیس روی Postgres رفت، این تابع عملاً هیچ بک‌آپی نمی‌گرفت
    (فقط چک `os.path.exists("botsaz.db")` که همیشه False بود، بی‌سروصدا رد
    می‌شد) — یعنی هیچ محافظتی در برابرِ از دست رفتنِ دیتا وجود نداشت. حالا
    برای Postgres از `pg_dump` (اجرا‌شده به‌صورتِ async subprocess، بدونِ
    بلاک‌کردنِ event loop) با خروجیِ فشرده (`--format=custom`) استفاده
    می‌شود؛ برای SQLite (اگر جایی هنوز استفاده بشه) همان رفتارِ قبلی حفظ
    شده.
    """
    os.makedirs("backups", exist_ok=True)
    is_pg = DATABASE_URL.startswith("postgresql")
    while True:
        try:
            if is_pg:
                await _pg_backup_once()
            elif os.path.exists("botsaz.db"):
                # ادغام WAL در فایل اصلی تا بکاپِ کپی‌شده کامل باشد
                try:
                    from sqlalchemy import text
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


def _pg_conn_params_from_url(url: str) -> dict:
    """DATABASE_URL (postgresql[+driver]://user:pass@host:port/db) را به
    فیلدهای مجزا برای pg_dump تبدیل می‌کند."""
    from urllib.parse import urlparse, unquote
    # pg_dump با درایورهای +asyncpg/+psycopg آشنا نیست؛ فقط طرحِ postgresql لازم است
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
    params = _pg_conn_params_from_url(DATABASE_URL)
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
        # فایلِ ناقص را نگه نداریم
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(f"pg_dump با کد {proc.returncode} شکست خورد: {stderr.decode(errors='ignore')[:500]}")
    # چرخشِ بک‌آپ‌های قدیمی — فقط ۲۸ نسخهٔ اخیر نگه داشته می‌شود
    files = sorted(f for f in os.listdir("backups") if f.startswith("botsaz-pg-") and f.endswith(".dump"))
    for old in files[:-28]:
        try:
            os.remove(os.path.join("backups", old))
        except OSError:
            pass
    print(f"[backup] pg_dump موفق: {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)")


INACTIVITY_WARN_DAYS = 5    # بعد از این‌همه روز بی‌فعالیتی، اخطار می‌رود
INACTIVITY_GRACE_DAYS = 3  # بعد از اخطار، این‌همه روز فرصت برای فعالیت دوباره




async def inactivity_loop():
    """به ربات‌های بی‌فعالیت (۵ روز بدون هیچ آپدیتی) اخطار می‌دهد و در صورت
    بی‌پاسخی (۳ روز دیگر بدون فعالیت)، دقیقاً مثل عدم‌تمدید پلن به بایگانی
    می‌فرستد — حذف نهایی‌شان را همان subscription_loop موجود انجام می‌دهد."""
    await asyncio.sleep(30)  # صبر تا کامل‌شدن استارتاپ
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
                        # === PATCH_INACTIVITY_SKIP_IF_TIME_LEFT_V1 ===
                        # همان دلیلِ main_platform.py: تا وقتی پلنِ ربات
                        # منقضی نشده، بی‌فعالیتی نباید باعثِ پازشدن شود.
                        if b.plan_expires_at and b.plan_expires_at > now:
                            continue
                        baseline = b.last_activity_at or b.created_at
                        if not baseline:
                            continue

                        if b.inactivity_warned_at:
                            # فعالیت تازه‌ای بعد از اخطار ثبت شده → اخطار را لغو کن
                            if b.last_activity_at and b.last_activity_at > b.inactivity_warned_at:
                                b.inactivity_warned_at = None
                                await s.commit()
                                continue
                            # بعد از مهلت پاسخ، هنوز فعالیتی نبوده → بایگانی
                            if now > b.inactivity_warned_at + datetime.timedelta(days=INACTIVITY_GRACE_DAYS):
                                b.status = "paused"
                                b.archived_at = now
                                b.archive_reason = "inactivity"
                                _tenant_bots.pop(b.token, None)
                                owner, uname, token, plan = b.owner_id, b.username, b.token, b.plan
                                await s.commit()
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

                        # هنوز اخطاری نرفته — بررسی بی‌فعالی ۵ روزه
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
                                pass  # ادمین بات‌ساز رو بلاک کرده
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
                                pass  # ادمین ربات خودشو بلاک کرده
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        except Exception as e:
            print("inactivity loop error:", e)
        await asyncio.sleep(6 * 3600)  # هر ۶ ساعت یک‌بار بررسی


async def subscription_loop():
    """یادآوری سررسید پلن (از ۵ روز قبل، روزی یک‌بار).
    ربات‌های خریداری‌شده (پلن غیر رایگان): هرگز حذف نمی‌شوند؛ بعد از سررسید تا
    روز ۳۲ام پیام‌های تذکر روزانه می‌گیرند و در روز ۳۲ام فقط به بایگانی می‌روند
    و همان‌جا می‌مانند تا با پرداخت فعال شوند.
    ربات‌های پلن رایگان: رفتار قبلی (بایگانی ۲ روز بعد، حذف بعد از مهلت بایگانی)."""
    PAID_ARCHIVE_DAYS = 32   # روز ۳۲ام بعد از سررسید → بایگانی (فقط پلن‌های خریداری‌شده)
    REMIND_BEFORE_DAYS = 5   # شروع یادآوری از ۵ روز قبل از سررسید
    await asyncio.sleep(20)  # صبر تا کامل‌شدن استارتاپ
    while True:
        try:
            if platform_bot:
                now = datetime.datetime.now()
                today = now.strftime("%Y-%m-%d")
                to_delete = []  # (bot_id, owner_id, username) — حذف بعد از بستن session
                async with Session() as s:
                    bots = (await s.scalars(select(BotRow))).all()
                    for b in bots:
                        purchased = (b.plan or "free") != "free"

                        # ۱) حذف نهایی: مهلتِ بایگانی بسته به دلیل و پلن فرق می‌کند
                        # (رایگان: ۱۰ روز — رشد/حرفه‌ای: ۶۰ روز — حذف‌شده‌توسطِ‌کاربر: ۳۰ روز)
                        if b.archived_at and now > b.archived_at + datetime.timedelta(
                                days=archive_retention_days(b.plan, b.archive_reason)):
                            to_delete.append((b.id, b.owner_id, b.username))
                            continue
                        if b.archived_at:
                            continue  # در بایگانی — تا پرداخت/بازگردانی همان‌جا می‌ماند

                        if not b.plan_expires_at:
                            continue
                        days_left = (b.plan_expires_at - now).days
# === PATCH_IMMEDIATE_PAID_EXPIRY_V1 ===
                        # قبلاً: پلن خریداری‌شده ۳۲ روز و پلن رایگان ۲ روز مهلت داشت.
                        # الان: هر دو صفر — دقیقاً سرِ سررسید (plan_expires_at) بایگانی می‌شود.
                        grace_days = 0
                        deadline = b.plan_expires_at + datetime.timedelta(days=grace_days)

                        # ۲) پایان مهلت: انتقال به بایگانی
                        if now > deadline:
                            b.status = "paused"
                            b.archived_at = now
                            b.archive_reason = "nonpayment"
                            _tenant_bots.pop(b.token, None)
                            await s.commit()
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
                            continue

                        # ۳) از ۵ روز قبل از سررسید تا پایان مهلت: یادآوری روزی یک‌بار
                        if days_left <= REMIND_BEFORE_DAYS and b.last_expiry_notice != today:
                            b.last_expiry_notice = today
                            await s.commit()
                            renew_hint = ("برای تمدید (۱ یا ۲ ماهه) به «🗂 ربات‌های من» برو و "
                                          "روی ربات بزن.")
                            if days_left > 0:
                                msg = (f"⏰ یادآوری: پلن ربات @{b.username} <b>{fa_days(days_left)}</b> "
                                       f"به سررسید می‌رسد.\n{renew_hint}")
                            elif days_left == 0:
                                msg = (f"⏰ پلن ربات @{b.username} <b>امروز</b> به سررسید می‌رسد.\n"
                                       + renew_hint)
                            else:
                                grace = max((deadline - now).days, 0)
                                if purchased:
                                    msg = (f"⚠️ پلن ربات @{b.username} منقضی شده است.\n"
                                           f"تا <b>{grace} روز دیگر</b> اگر تمدید نشود، ربات به بایگانی "
                                           "می‌رود (حذف نمی‌شود و با پرداخت دوباره فعال می‌شود).\n"
                                           + renew_hint)
                                else:
                                    msg = (f"⚠️ پلن ربات @{b.username} منقضی شده است. تا حدود "
                                           f"{grace} روز دیگر اگر تمدید نشود، به بایگانی می‌رود.")
                            renew_kb = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="💳 تمدید ربات", callback_data=f"mb:{b.id}",
                                                     style=ButtonStyle.PRIMARY),
                            ]])
                            try:
                                await platform_bot.send_message(b.owner_id, msg, reply_markup=renew_kb)
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

                # حذف نهایی ربات‌های منقضی‌شدهٔ بایگانی + پاک‌سازی وب‌هوک و اطلاع به مالک
                for bot_id, owner_id, username in to_delete:
                    # ربات‌های فروخته‌شده توسط ربات همکار، مرجع partner_sales دارند؛
                    # حذف مستقیم آن‌ها باعث ForeignKeyViolation و تکرار خطا در هر چرخه می‌شود.
                    # تا زمان تعیین تکلیف فروش، فقط در بایگانی باقی می‌مانند و هیچ داده‌ای حذف نمی‌شود.
                    try:
                        from models import PartnerSale
                        linked_sale = await s.scalar(
                            select(PartnerSale.id).where(PartnerSale.sold_bot_id == bot_id).limit(1)
                        )
                    except Exception:
                        logger.exception('بررسی مرجع partner_sales برای bot_id=%s ناموفق بود؛ حذف متوقف شد', bot_id)
                        continue
                    if linked_sale is not None:
                        logger.warning(
                            'حذف نهایی bot_id=%s متوقف شد؛ مرجع partner_sales=%s وجود دارد',
                            bot_id, linked_sale
                        )
                        continue
                    _tenant_bots.pop(_token_of(bots, bot_id), None)
                    token = await purge_bot(bot_id)
                    if token:
                        _tenant_bots.pop(token, None)
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
        except Exception as e:
            print("subscription loop error:", e)
        await asyncio.sleep(6 * 3600)  # هر ۶ ساعت یک‌بار بررسی


def _token_of(bots, bot_id):
    for b in bots:
        if b.id == bot_id:
            return b.token
    return None


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


# === PATCH_LOW_VOLUME_SMART_THRESHOLD_V1 ===
# باگ: آستانهٔ هشدار ثابت بود (پیش‌فرض ۲ گیگ)، پس یک سرویسِ ۲ گیگی از همان
# لحظهٔ خرید — با صفر مصرف — پیامِ «حجمت رو به اتمام است» می‌گرفت و مشتری
# فکر می‌کرد سرویسِ خراب فروخته‌ایم.
#
# دو اصلاح: (۱) تا وقتی کاربر اصلاً مصرفی نداشته، هیچ هشداری فرستاده نمی‌شود
# (شرطِ used > 0 در محلِ فراخوانی)؛ (۲) آستانه برایِ بسته‌هایِ کوچک نسبی
# می‌شود — یعنی ۲۵٪ حجمِ بسته، ولی هیچ‌وقت بیشتر از آستانهٔ تنظیم‌شدهٔ مالک.
#   بستهٔ ۲ گیگ  → هشدار در ۰.۵ گیگِ باقی‌مانده
#   بستهٔ ۵۰ گیگ → هشدار در ۲ گیگِ باقی‌مانده (همان تنظیمِ مالک)
def _eff_gb_threshold(configured: float, cap_gb: float) -> float:
    if cap_gb <= 0:
        return configured
    return min(configured, max(cap_gb * 0.25, 0.1))


async def vpn_expiry_loop():
    """اخطار روزانه به کاربران VPN خودکار (اتمام حجم / نزدیکی انقضا / انقضا) +
    حذف خودکار سرویس‌های منقضی‌شده‌ای که تا ۲ روز بعد از انقضا تمدید نشده‌اند.

    پیام‌ها با قالب جدید (نام سرویس + کد سرویس + مشخصات خرید + blockquote) و
    دکمهٔ اینلاین «مشاهدهٔ همین سرویس» ارسال می‌شوند."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from tenant.shared.panel_clients.vpn_panel import GB
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    from tenant.bots.vpn.vpn_user import new_svc_code

    GRACE_DAYS = 2  # مهلت تمدید بعد از انقضا؛ بعدش حذف خودکار

    def _svc_info_block(title, code, vol_gb, days, email=""):
        vol = "نامحدود" if not vol_gb else f"{vol_gb} گیگ"
        dur = "بی‌انقضا" if not days else f"{days} روز"
        return (f"💎 نام سرویس: <b>{title or 'VPN'}</b>\n"
                f"🗂 کد سرویس: <code>{code or '—'}</code>\n"
                + (f"👤 نام کاربری پنل: <code>{email}</code>\n" if email else "")
                + f"📦 حجم خریداری‌شده: <b>{vol}</b>\n"
                f"📅 مدت سرویس: <b>{dur}</b>")

    def _kb(aid):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📡 مشاهده و تمدید همین سرویس", callback_data=f"vsvc:{aid}")]])

    await asyncio.sleep(40)
    while True:
        try:
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            async with Session() as s:
                panels = (await s.scalars(select(VpnPanel).where(VpnPanel.enabled == True))).all()
                panel_list = [(p.bot_id, p.base_url, p.username, p.password,
                               p.sub_base or "", p.inbound_ids, p.panel_key, p.api_key) for p in panels]  # PATCH_PANEL_API_KEY_PROPAGATION_V1
            for bot_id, base, user, pw, sub_base, inbound, panel_key, api_key in panel_list:  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                async with Session() as s:
                    brow = await s.get(BotRow, bot_id)
                    accts = (await s.scalars(select(PanelAccount).where(
                        PanelAccount.bot_id == bot_id,
                        PanelAccount.panel_key == panel_key))).all()
                    # backfill کد سرویس برای اکانت‌های قدیمی
                    changed = False
                    for a in accts:
                        if not a.svc_code:
                            a.svc_code = await new_svc_code(s, bot_id)
                            changed = True
                    if changed:
                        await s.commit()
                    acct_list = [(a.id, a.email, a.user_telegram_id, a.plan_title,
                                  a.expiry_notice, a.svc_code, a.volume_gb, a.days,
                                  a.expires_at, a.depleted_at) for a in accts]
                if not brow or brow.status != "active" or not acct_list:
                    continue
                client = make_panel_client_from_fields(
                    panel_key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound, api_key=api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                try:
                    clients = await client.list_clients()
                except Exception:
                    await client.close()
                    continue
                by_email = {c.get("email"): c for c in clients if c.get("email")}

                tbot = Bot(token=brow.token, default=DEFAULTS)
                gb_threshold = await _setting_get(bot_id, f"notify_gb_threshold_{panel_key}", "")
                if not gb_threshold:
                    gb_threshold = await _setting_get(bot_id, "notify_gb_threshold", "2")
                days_threshold = await _setting_get(bot_id, f"notify_days_threshold_{panel_key}", "")
                if not days_threshold:
                    days_threshold = await _setting_get(bot_id, "notify_days_threshold", "5")
                try:
                    gb_threshold_f = float(gb_threshold)
                except (TypeError, ValueError):
                    gb_threshold_f = 2.0
                try:
                    days_threshold_f = float(days_threshold)
                except (TypeError, ValueError):
                    days_threshold_f = 5.0
                try:
                    for (aid, email, tg_id, title, last_notice, code,
                         vol_gb, p_days, exp_at, depleted_at) in acct_list:
                        # اکانت‌های تست رایگان نیازی به این پیام‌ها ندارند
                        if title and title.strip() == "تست رایگان":
                            continue
                        c = by_email.get(email)
                        # === PATCH_VPN_ALERT_MISSING_CLIENT_LOG_V1 ===
                        # باگِ گزارش‌شده (تیکت #408): اگر پنل هیچ کلاینتی با
                        # این email برنگرداند، c برابر None می‌شد، cap صفر
                        # می‌شد، و کل بررسیِ حجم/انقضا بی‌صدا رد می‌شد. حالا
                        # حداقل یک لاگ ثبت می‌شود تا قابلِ‌ردیابی باشد.
                        if c is None:
                            logger.warning(
                                "vpn_expiry_loop: پنلِ %s هیچ کلاینتی با email=%r برای "
                                "PanelAccount#%s (bot_id=%s) برنگرداند — هشدارِ حجم/انقضا "
                                "برایِ این حساب رد شد.", panel_key, email, aid, bot_id)
                        tr = (c.get("traffic") if c and isinstance(c.get("traffic"), dict)
                              else c) if c else None
                        up = int(tr.get("up", 0) or 0) if tr else 0
                        down = int(tr.get("down", 0) or 0) if tr else 0
                        cap = int((c.get("totalGB", tr.get("total", 0)) if c else 0) or 0)
                        exp = int((c.get("expiryTime", 0) if c else 0) or 0)
                        if not exp and exp_at:
                            exp = int(exp_at.timestamp() * 1000)
                        used = up + down
                        info = _svc_info_block(title, code, vol_gb, p_days, email)

                        # ── ۱) حذف خودکار: ۲ روز بعد از انقضا بدون تمدید ──
                        if exp > 0 and now_ms > exp + GRACE_DAYS * 86_400_000:
                            try:
                                await client.delete_account(email)
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            async with Session() as s:
                                a2 = await s.get(PanelAccount, aid)
                                if a2:
                                    await s.delete(a2)
                                    await s.commit()
                            msg = (
                                "🔥 <b>هشدار مهم!</b>\n"
                                "⛔️ سرویس شما به دلیل <b>عدم تمدید پس از انقضا</b> "
                                "به‌صورت دائمی حذف شد.\n\n"
                                f"{info}\n\n"
                                "<blockquote>⚠️ پس از انقضای سرویس، مهلت "
                                f"{GRACE_DAYS} روزهٔ تمدید بدون اقدام پایان یافت. "
                                "این سرویس اکنون حذف شده و قابل بازیابی نیست. "
                                "در صورت نیاز می‌توانید سرویس جدید ایجاد کنید.</blockquote>")
                            try:
                                await tbot.send_message(tg_id, msg)
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            continue

                        # === PATCH_DEPLETED_CLEANUP_V1 ===
                        # ── ۱.۵) حجمِ تمام‌شده: مهلتِ تمدید و سپس حذف ──
                        # قبلاً فقط سرویسِ *منقضی* حذف می‌شد؛ سرویسی که
                        # حجمش تمام شده بود ولی تاریخش نگذشته بود تا ابد
                        # می‌ماند و هر روز پیامِ «ترافیک تمام شد» می‌گرفت.
                        _now_dt = datetime.datetime.now()
                        _is_depleted = cap > 0 and used >= cap
                        if not _is_depleted:
                            # حجم دوباره شارژ شده — مهلت از نو
                            if depleted_at is not None:
                                async with Session() as s:
                                    a2 = await s.get(PanelAccount, aid)
                                    if a2:
                                        a2.depleted_at = None
                                        await s.commit()
                                depleted_at = None
                        else:
                            if depleted_at is None:
                                async with Session() as s:
                                    a2 = await s.get(PanelAccount, aid)
                                    if a2:
                                        a2.depleted_at = _now_dt
                                        await s.commit()
                                depleted_at = _now_dt
                            _dep_age = (_now_dt - depleted_at).total_seconds()
                            if _dep_age >= GRACE_DAYS * 86_400:
                                try:
                                    await client.delete_account(email)
                                except Exception:
                                    logger.exception("حذفِ سرویسِ حجم‌تمام‌شده از پنل ناموفق بود")
                                async with Session() as s:
                                    a2 = await s.get(PanelAccount, aid)
                                    if a2:
                                        await s.delete(a2)
                                        await s.commit()
                                msg = (
                                    "🔥 <b>هشدار مهم!</b>\n"
                                    "⛔️ سرویس شما به دلیل <b>اتمام ترافیک و عدم تمدید</b> "
                                    "به‌صورت دائمی حذف شد.\n\n"
                                    f"{info}\n\n"
                                    "<blockquote>⚠️ پس از اتمام حجم، مهلت "
                                    f"{GRACE_DAYS} روزهٔ خریدِ ترافیک اضافه یا تمدید "
                                    "بدون اقدام پایان یافت. این سرویس اکنون حذف شده و "
                                    "قابل بازیابی نیست. در صورت نیاز می‌توانید سرویس "
                                    "جدید ایجاد کنید.</blockquote>")
                                try:
                                    await tbot.send_message(tg_id, msg)
                                except TelegramForbiddenError:
                                    pass
                                except Exception:
                                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                                continue

                        # ── ۲) هشدارهای روزانه ──
                        # === PATCH_VPN_ALERT_OVERWRITE_FIX_V1 ===
                        # باگِ گزارش‌شده (تیکت #408): warn قبلاً یک متغیرِ
                        # تکی بود و بلاکِ «روزهایِ باقی‌مانده» بدونِ چک‌کردنِ
                        # اینکه قبلاً warn ست شده یا نه دوباره رویش
                        # می‌نوشت، پس اگر هم هشدارِ حجم و هم هشدارِ انقضا
                        # هم‌زمان صادق بود، پیامِ حجم بی‌صدا گم می‌شد و اصلاً
                        # فرستاده نمی‌شد. حالا هر دو نوع هشدار (هرکدام صادق
                        # باشد) جمع می‌شوند و با هم در یک پیام می‌روند.
                        warns = []
                        # === PATCH_DEPLETED_CLEANUP_V1 ===
                        # برایِ سرویسِ حجم‌تمام‌شده فقط دو پیام می‌رود: روزِ
                        # اول و روزِ آخرِ مهلت. بینِ این دو سکوت — قبلاً هر
                        # روز تکرار می‌شد و کاربران شکایت داشتند.
                        _dep_warn_ok = True
                        if _is_depleted and depleted_at is not None:
                            _dep_days = (_now_dt - depleted_at).total_seconds() / 86_400
                            _dep_warn_ok = (_dep_days < 1) or (_dep_days >= GRACE_DAYS - 0.5)
                        if cap > 0:
                            left_gb = max(cap - used, 0) / GB
                            if left_gb <= 0:
                                if not _dep_warn_ok:
                                    continue
                                _left_h = max(
                                    int(GRACE_DAYS * 24
                                        - (_now_dt - depleted_at).total_seconds() / 3600), 1
                                ) if depleted_at else GRACE_DAYS * 24
                                warns.append(
                                    "⚠️ سرویس شما به دلیل <b>اتمام ترافیک</b> غیرفعال شد.\n\n"
                                    "<blockquote>‼️ با خرید <b>ترافیک اضافه</b> یا "
                                    "<b>تمدید سرویس</b>، اتصال خود را همین حالا بازیابی "
                                    "کنید.\n"
                                    f"⏳ حدود <b>{_left_h} ساعت</b> مهلت باقی مانده؛ پس از "
                                    "آن سرویس به‌صورت <b>خودکار و دائمی حذف</b> "
                                    "می‌شود!</blockquote>")
                            elif used > 0 and left_gb <= _eff_gb_threshold(
                                    gb_threshold_f, cap / GB):
                                warns.append(
                                    "⚠️ حجم سرویس شما <b>رو به اتمام</b> است "
                                    f"(باقی‌مانده حدود <b>{round(left_gb, 2)} گیگ</b>).\n\n"
                                    "<blockquote>‼️ با خرید <b>ترافیک اضافه</b> یا "
                                    "<b>تمدید سرویس</b>، پیش از قطعی اتصال خود را تضمین "
                                    "کنید!</blockquote>")
                        if exp > 0:
                            days_left = (exp - now_ms) / 86_400_000
                            if days_left < 0:
                                grace_left = GRACE_DAYS + days_left
                                warns.append(
                                    "⛔️ سرویس شما <b>منقضی شده</b> است.\n\n"
                                    "<blockquote>‼️ فقط حدود "
                                    f"<b>{max(int(grace_left) + 1, 1)} روز</b> مهلت تمدید "
                                    "باقی مانده؛ در صورت عدم تمدید، سرویس به‌صورت "
                                    "<b>خودکار و دائمی حذف</b> خواهد شد!</blockquote>")
                            elif days_left <= days_threshold_f:
                                warns.append(
                                    "⏰ سرویس شما تا حدود "
                                    f"<b>{int(days_left) + 1} روز دیگر</b> منقضی می‌شود.\n\n"
                                    "<blockquote>‼️ با <b>تمدید سرویس</b>، اتصال پایدار و "
                                    "بدون قطعی را برای خود حفظ کنید!</blockquote>")
                        warn = None
                        if warns:
                            warn = "🔥 <b>هشدار مهم!</b>\n" + "\n\n".join(warns) + f"\n\n{info}"
                        if warn and last_notice != today:
                            try:
                                await tbot.send_message(tg_id, warn, reply_markup=_kb(aid))
                            except TelegramForbiddenError:
                                # === PATCH_SILENT_BLOCKED_V1 ===
                                # کاربر بات رو بلاک کرده — انتظار داریم، spam
                                # نمی‌کنیم توی لاگ. فقط expiry_notice رو ست
                                # می‌کنیم تا دفعه‌ی بعد دوباره تلاش نکنه.
                                pass
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            async with Session() as s:
                                a2 = await s.get(PanelAccount, aid)
                                if a2:
                                    a2.expiry_notice = today
                                    await s.commit()
                finally:
                    try:
                        await client.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                    try:
                        await tbot.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        except Exception as e:
            print("vpn expiry loop error:", e)
        await asyncio.sleep(12 * 3600)  # هر ۱۲ ساعت یک‌بار


async def vpn_first_connect_loop(interval: int = 90):
    """رصدِ «اولین اتصال» کانفیگ‌های خودکار (فقط سرویس‌های تازه‌خریداری‌شده،
    یعنی first_conn_notified صراحتاً False است). به‌محض اینکه اولین بایت مصرف
    روی یک چنین سرویسی دیده شود (یعنی کاربر برای اولین‌بار با کانفیگش وصل شده)،
    یک پیام تبریک/راهنما در ربات برایش می‌فرستد.

    اکانت‌های قدیمیِ قبل از این قابلیت مقدار first_conn_notified‌شان NULL است
    و عمداً نادیده گرفته می‌شوند تا برای مشتری‌های فعلی پیام اسپم نشود؛ این
    قابلیت فقط برای خریدهای از این به بعد فعال است."""
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    await asyncio.sleep(25)
    # === PATCH_FIRSTCONN_CIRCUIT_BREAKER_V1 ===
    # پنلی که خراب/کند است (مثلاً آدرسش عوض شده و هر تماس ۱۵ ثانیه طول می‌کشد
    # تا timeout بخورد) قبلاً هر ۹۰ ثانیه دوباره و دوباره امتحان می‌شد و هر
    # چرخهٔ این loop را چندین‌برابر کند می‌کرد. حالا پنلی که fail کند تا ۱۵
    # دقیقه رد می‌شود (circuit breaker) و بعد دوباره فرصت می‌گیرد.
    _panel_skip_until: dict[tuple, float] = {}
    _PANEL_SKIP_SECONDS = 15 * 60
    while True:
        try:
            async with Session() as s:
                panels = (await s.scalars(select(VpnPanel).where(VpnPanel.enabled == True))).all()
                panel_list = [(p.bot_id, p.base_url, p.username, p.password,
                               p.sub_base or "", p.inbound_ids, p.panel_key, p.api_key) for p in panels]  # PATCH_PANEL_API_KEY_PROPAGATION_V1
            for bot_id, base, user, pw, sub_base, inbound, panel_key, api_key in panel_list:  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                _pk = (bot_id, base)
                _skip = _panel_skip_until.get(_pk)
                if _skip is not None and _skip > asyncio.get_event_loop().time():
                    continue
                async with Session() as s:
                    brow = await s.get(BotRow, bot_id)
                    accts = (await s.scalars(select(PanelAccount).where(
                        PanelAccount.bot_id == bot_id,
                        PanelAccount.first_conn_notified == False))).all()
                    # === PATCH_PURCHASE_FOLLOWUP_V1 === created_at و followup هم لازم است
                    acct_list = [(a.id, a.email, a.user_telegram_id, a.plan_title,
                                  a.created_at, bool(a.followup_prompt_sent)) for a in accts]
                if not brow or brow.status != "active" or not acct_list:
                    continue
                # === PATCH_PURCHASE_FOLLOWUP_V1 === تنظیماتِ «پیگیری خرید» این ربات
                _fup_on = (await _setting_get(bot_id, "followup_enabled", "0")) == "1"
                try:
                    _fup_hours = int(await _setting_get(bot_id, "followup_hours", "2") or "2")
                except ValueError:
                    _fup_hours = 2
                client = make_panel_client_from_fields(
                    panel_key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound, api_key=api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                try:
                    clients = await client.list_clients()
                except Exception:
                    await client.close()
                    _panel_skip_until[_pk] = asyncio.get_event_loop().time() + _PANEL_SKIP_SECONDS
                    continue
                _panel_skip_until.pop(_pk, None)
                by_email = {c.get("email"): c for c in clients if c.get("email")}
                await client.close()

                tbot = Bot(token=brow.token, default=DEFAULTS)
                try:
                    for aid, email, tg_id, title, created_at, fup_sent in acct_list:
                        c = by_email.get(email)
                        if not c:
                            continue  # این اکانت روی این پنل نیست؛ نوبتِ پنل خودش می‌رسد
                        tr = c.get("traffic") if isinstance(c.get("traffic"), dict) else c
                        up = int(tr.get("up", 0) or 0)
                        down = int(tr.get("down", 0) or 0)
                        if (up + down) <= 0:
                            # === PATCH_PURCHASE_FOLLOWUP_V1 ===
                            # هنوز وصل نشده. اگر «پیگیری خرید» روشن است و به‌اندازهٔ
                            # کافی از ساختِ سرویس گذشته و هنوز پیگیری نفرستاده‌ایم،
                            # دو دکمهٔ «متصل نشدم / فرصت نداشتم» را بفرست.
                            if _fup_on and not fup_sent and created_at is not None:
                                age_h = (datetime.datetime.now() - created_at).total_seconds() / 3600.0
                                if age_h >= _fup_hours:
                                    fup_text = (
                                        "👋 سلام! دیدیم سرویس‌ات را گرفتی ولی هنوز اتصالی ازش ندیدیم.\n\n"
                                        "همه‌چیز مرتبه؟ اگر در وصل‌شدن به مشکل خوردی، بگو تا کمکت کنیم 👇"
                                    )
                                    fup_kb = InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="❌ متصل نشدم",
                                                              callback_data=f"vfup:cant:{aid}")],
                                        [InlineKeyboardButton(text="⏳ هنوز فرصت تست نداشتم",
                                                              callback_data=f"vfup:later:{aid}")],
                                    ])
                                    try:
                                        await tbot.send_message(tg_id, fup_text, reply_markup=fup_kb)
                                    except Exception:
                                        logger.exception("خطای نادیده‌گرفته‌شده (ارسالِ پیگیری خرید)")
                                    async with Session() as s:
                                        a2 = await s.get(PanelAccount, aid)
                                        if a2:
                                            a2.followup_prompt_sent = True
                                            await s.commit()
                            continue  # در هر صورت هنوز اتصالی نبوده
                        text = (
                            "🎉 <b>اولین اتصال شما با موفقیت انجام شد!</b>\n\n"
                            f"سرویس «{title or 'کانفیگ VPN'}» فعال شد و اتصالت با موفقیت برقرار گردید ✅\n"
                            "از این به بعد از بخش «سرویس‌های من» می‌تونی وضعیت مصرف، "
                            "حجم باقی‌مانده و تاریخ انقضا رو هر لحظه ببینی."
                        )
                        try:
                            await tbot.send_message(tg_id, text)
                        except Exception:
                            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                        async with Session() as s:
                            a2 = await s.get(PanelAccount, aid)
                            if a2:
                                a2.first_conn_notified = True
                                await s.commit()
                finally:
                    try:
                        await tbot.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        except Exception as e:
            print("vpn first-connect loop error:", e)
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


# حداقل فاصلهٔ بین دو هشدار برای یک ربات (ضدِ اسپم)
BIO_WARN_COOLDOWN_HOURS = 6

# پس از این تعداد تخلف، ربات به‌صورت خودکار غیرفعال (paused) می‌شود
BIO_VIOLATION_LIMIT = 5


async def card_order_expiry_loop(timeout_min: int = 10, interval_sec: int = 60):
    """سفارش‌های کارت‌به‌کارتی که بیش از timeout_min دقیقه منتظرِ رسید مانده‌اند را
    خودکار لغو می‌کند: وضعیتِ سفارش را «rejected» و کانفیگِ رزروشده را به «موجود»
    برمی‌گرداند، و به کاربر پیامِ لغو می‌دهد. جلوی گیرکردنِ کانفیگ در حالتِ رزرو
    و انقضای پرداختِ نیمه‌کاره را می‌گیرد."""
    from models import Order, Config
    await asyncio.sleep(25)  # صبر تا کامل‌شدن استارتاپ
    while True:
        try:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=timeout_min)
            to_notify = []  # (token, telegram_id, order_id)
            async with Session() as s:
                # سفارش‌هایی که باید منقضی شوند:
                #  ۱) awaiting_receipt: کارت‌به‌کارت انتخاب شده ولی رسید نیامده.
                #  ۲) pendingِ دارای رزرو: کانفیگِ انبار را قفل کرده ولی رها شده.
                # سفارشِ pendingِ بدونِ رزرو دست‌نخورده می‌ماند (ضرری ندارد و کاربر
                # ممکن است هنوز در حالِ انتخابِ روشِ پرداخت باشد).
                stale = (await s.scalars(
                    select(Order).where(
                        Order.status.in_(["awaiting_receipt", "pending"]),
                        Order.created_at < cutoff,
                    )
                )).all()
                for o in stale:
                    is_awaiting = o.status == "awaiting_receipt"
                    has_reserved = bool(o.reserved_config_id)
                    if not is_awaiting and not has_reserved:
                        continue  # pendingِ بدونِ رزرو → کاری نکن
                    # آزادسازیِ کانفیگِ رزروشده
                    if o.reserved_config_id:
                        cfg = await s.get(Config, o.reserved_config_id)
                        if cfg and cfg.status == "reserved":
                            cfg.status = "in_stock"
                            cfg.owner_user_id = None
                    o.status = "rejected"
                    if is_awaiting:
                        from models import BotUser
                        bot_row = await s.get(BotRow, o.bot_id)
                        u = await s.get(BotUser, o.user_id)
                        if bot_row and u and bot_row.token:
                            to_notify.append((bot_row.token, u.telegram_id, o.id))
                await s.commit()
            # ارسالِ پیامِ لغو به کاربران (بیرون از session)
            for token, tg_id, oid in to_notify:
                try:
                    tb = Bot(token=token, default=DEFAULTS)
                    try:
                        await tb.send_message(
                            tg_id,
                            "⏳ مهلتِ ۱۰ دقیقه‌ایِ پرداختِ کارت‌به‌کارت تمام شد و رسیدی "
                            "دریافت نشد، بنابراین سفارش لغو شد.\n"
                            "اگر هنوز مایل به خرید هستی، دوباره از منو اقدام کن.")
                    finally:
                        await tb.session.close()
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                await asyncio.sleep(0.3)
        except Exception as e:
            print("card_order_expiry_loop error:", e)
        await asyncio.sleep(interval_sec)


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


async def crypto_poll_loop():
    """تأیید خودکار پرداخت‌های ارز دیجیتال (NOWPayments) مستقل از IPN.

    هر ۳۰ ثانیه همهٔ چرک‌نویس‌های شارژِ در انتظار (waiting/priced) که شناسهٔ
    پرداخت نوپیمنت دارند را مستقیم از درگاه استعلام می‌کند؛ اگر روی شبکه
    finished/confirmed شده باشند، کیف پول را شارژ کرده و به کاربر در تلگرام
    پیام می‌دهد. idempotent است (اگر IPN زودتر شارژ کرده باشد، دوباره شارژ
    نمی‌کند). این کار مشکلِ «پرداخت انجام شد ولی شارژ نشد و پیامی نیامد» را
    بدون نیاز به دکمهٔ دستیِ کاربر یا رسیدنِ IPN حل می‌کند.
    """
    import datetime as _dt
    from tenant.gateways.payment_page import (
        ChargeDraft, ensure_table, _active_check_tenant, _active_check_platform,
    )
    await asyncio.sleep(30)  # مهلت راه‌اندازی اولیه
    while True:
        try:
            await ensure_table()
            cutoff = _dt.datetime.now() - _dt.timedelta(days=2)
            async with Session() as s:
                drafts = (await s.scalars(select(ChargeDraft).where(
                    ChargeDraft.status.in_(("waiting", "priced")),
                    ChargeDraft.np_payment_id != "",
                    ChargeDraft.np_payment_id.isnot(None),
                    ChargeDraft.created_at >= cutoff,
                ))).all()
            for d in drafts:
                kind = getattr(d, "kind", "tenant") or "tenant"
                try:
                    ok = (await _active_check_platform(d) if kind == "platform"
                          else await _active_check_tenant(d))
                    if ok:
                        async with Session() as s:
                            row = await s.scalar(select(ChargeDraft).where(
                                ChargeDraft.tracking_code == d.tracking_code))
                            if row and row.status != "paid":
                                row.status = "paid"
                                await s.commit()
                except Exception:
                    import traceback
                    print("crypto_poll_loop item error:\n" + traceback.format_exc())
                await asyncio.sleep(0.3)  # فشار نیاوردن به API نوپیمنت
        except Exception:
            import traceback
            print("crypto_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(30)


async def smm_poll_loop():
    """پیگیری خودکار وضعیت سفارش‌های SMM از پنل (هر ۲ دقیقه)."""
    from tenant.bots.virtual_service.smm import smm_status_poll_once
    await asyncio.sleep(50)
    while True:
        try:
            await smm_status_poll_once()
        except Exception:
            import traceback
            print("smm_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(120)


async def _warm_tenant_bots():
    """کانکشنِ HTTPS ربات‌های مستأجرِ فعال را از قبل گرم می‌کند تا اولین
    برخوردِ کاربر با ربات (بعد از ری‌استارت/بی‌کاری سرور) معطلِ برقراریِ
    اتصال (TLS handshake) با سرورهای تلگرام نماند — همان چیزی که باعث
    می‌شود «دفعهٔ اول که وارد ربات می‌شوی کند است، بعدش دیگر سریع است»."""
    try:
        async with Session() as s:
            tokens = (await s.scalars(
                select(BotRow.token).where(BotRow.status == "active"))).all()
    except Exception as e:
        print("warm_tenant_bots query error:", e)
        return
    sem = asyncio.Semaphore(20)

    async def _warm_one(token: str):
        async with sem:
            b = None
            try:
                b = Bot(token=token, default=DEFAULTS)
                await b.get_me()
                # PATCH_CLOSE_OLD_WARM_SESSION_V1 — این تابع هر ۳ دقیقه دوباره
                # اجرا می‌شود (_warm_tenant_bots_loop) و برای همان توکن‌ها یک
                # Bot تازه می‌سازد. اگر توکن از قبل در _tenant_bots بود، آبجکتِ
                # Bot قبلی و aiohttp session داخلی‌اش قبلاً هیچ‌وقت بسته نمی‌شد
                # و فقط ref آن گم می‌شد — دقیقاً منبعِ لاگِ «Unclosed client
                # session» با هر بار اجرای این حلقه برای ده‌ها ربات. حالا قبل
                # از جایگزینی، سشنِ قدیمی (در صورت وجود) بسته می‌شود.
                old_b = _tenant_bots.get(token)
                _tenant_bots[token] = b
                if old_b is not None and old_b is not b:
                    try:
                        await old_b.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            except Exception:
                # مهم: اگر get_me() ناموفق شد (توکن revoke شده، timeout، و
                # غیره)، Bot را در _tenant_bots نگه نمی‌داریم، پس باید
                # session داخلی‌اش را همین‌جا ببندیم؛ وگرنه هر بار که سرور
                # ری‌استارت می‌شود چند ده اتصالِ aiohttp باز/نشتی می‌ماند
                # (لاگِ «Unclosed client session» دقیقاً همین‌جاست).
                if b is not None:
                    try:
                        await b.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    await asyncio.gather(*(_warm_one(t) for t in tokens), return_exceptions=True)
    print(f"warm_tenant_bots: {len(_tenant_bots)}/{len(tokens)} ربات از قبل گرم شد")


async def _warm_tenant_bots_loop():
    """PATCH_KEEP_WARM_LOOP_V1 — قبلاً _warm_tenant_bots فقط یک‌بار موقعِ
    استارتِ سرویس اجرا می‌شد؛ بعدِ چند دقیقه بی‌کاریِ یک بات، کانکشنِ
    TCP/TLSاش به تلگرام (توسطِ خودِ سرورِ تلگرام یا لایه‌های میانی) بسته
    می‌شد و اولین ریکوئستِ بعدی (مثلاً /start کاربر) باید از صفر وصل می‌شد —
    همون تاخیرِ ۳ تا ۴ ثانیه‌ای که کاربرها بعدِ چند دقیقه بی‌کاری حس
    می‌کردن. حالا هر ۳ دقیقه دوباره همهٔ بات‌های فعال را گرم نگه می‌دارد."""
    while True:
        await asyncio.sleep(180)
        try:
            await _warm_tenant_bots()
        except Exception as e:
            print("warm_tenant_bots_loop error:", e)


async def on_startup():
    await init_db()
    # اطمینان از وجود ستون‌های جدید جدول‌های SMM (migration مطمئن)
    try:
        from tenant.bots.virtual_service.smm import ensure_smm_columns
        await ensure_smm_columns()
    except Exception as e:
        print("ensure_smm_columns error:", e)
    # بارگذاری تنظیمات قابلیت‌های پلن (override های ادمین) تا بعد از ری‌استارت هم پابرجا بماند
    try:
        from plans import reload_plan_features
        async with Session() as s:
            await reload_plan_features(s)
    except Exception as e:
        print("reload_plan_features error:", e)
    # بارگذاری وضعیت پولی/رایگان بودن پلن‌ها از دیتابیس
    try:
        from platform_bot import load_plans_paid
        await load_plans_paid()
    except Exception as e:
        print("load_plans_paid error:", e)
    if platform_bot and WEBHOOK_BASE:
        # === PATCH_FAKE_JOIN_GUARD_V1 ===
        from webhook_utils import PLATFORM_ALLOWED_UPDATES
        await platform_bot.set_webhook(
            f"{WEBHOOK_BASE}/webhook/platform", drop_pending_updates=True,
            allowed_updates=PLATFORM_ALLOWED_UPDATES)
    if platform_bot:
        global _platform_bot_username
        try:
            me = await platform_bot.get_me()
            _platform_bot_username = me.username
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_BGTASK_STRONG_REF_V1 ===
    # قبلاً این ۱۲ حلقهٔ پس‌زمینه با asyncio.create_task ساده اجرا می‌شدند،
    # بدونِ نگه‌داشتنِ هیچ رفرنسی — طبقِ مستندِ خودِ asyncio، تسکی که هیچ‌جا
    # رفرنس نداشته باشد ممکن است وسطِ اجرا توسطِ garbage collector بی‌صدا و
    # بدونِ هیچ خطایی نابود شود (یعنی مثلاً حلقهٔ بک‌آپ یا انقضای VPN می‌توانست
    # برای همیشه متوقف شود بدونِ اینکه هیچ لاگی از آن بمانَد). حالا از همان
    # مجموعهٔ _bg_tasks که _spawn استفاده می‌کند بهره می‌بریم.
    for _coro in (
        _warm_tenant_bots(), _warm_tenant_bots_loop(), backup_loop(), subscription_loop(), inactivity_loop(),
        vpn_expiry_loop(), vpn_first_connect_loop(), crypto_poll_loop(), smm_poll_loop(),
        swap_poll_loop(), platform_swap_poll_loop(), ptc_daily_snapshot_loop(),
        brand_bio_loop(), card_order_expiry_loop(),
        ad_broadcast_loop(), ad_scheduled_posts_loop(),
    ):
        _t = asyncio.create_task(_coro)
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)
    # === PATCH_TENANT_STATUS_BUS_V1 ===
    # شنوندهٔ NOTIFYِ بین‌پروسه‌ای؛ وقتی یه پروسهٔ دیگه (مثلاً بات‌ساز، اگه
    # جدا از مستأجرها اجرا بشه) invalidate_tenant_status صدا بزنه، همین‌جا
    # هم فوری اثر می‌کنه (به‌جای صبر تا انقضای TTLِ ۵ثانیه‌ای). روی SQLite
    # یا اگه Postgres لحظه‌ای در دسترس نباشه، بی‌خطر no-op می‌شود.
    try:
        import tenant_status_bus
        _t = asyncio.create_task(
            tenant_status_bus.run_tenant_invalidate_listener(_clear_local_tenant_cache))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (راه‌اندازیِ tenant_status_bus listener)")


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """PATCH_FASTAPI_LIFESPAN_V1 — جایگزینِ @app.on_event("startup") که در
    نسخه‌های جدیدِ FastAPI منسوخ (deprecated) شده. رفتار دقیقاً همان است."""
    await on_startup()
    yield


app.router.lifespan_context = _lifespan


_bg_tasks: set = set()


def _spawn(coro):
    """اجرای پردازش آپدیت در پس‌زمینه تا پاسخِ وبهوک فوراً به تلگرام برگردد.

    این کار باعث می‌شود تلگرام منتظرِ پایان پردازش (کوئری‌ها/ارسال پیام) نماند؛
    در نتیجه پاسخ‌دهی سریع‌تر می‌شود و زیر بار، آپدیت‌ها صف/تایم‌اوت نمی‌شوند.
    """
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _process_platform(data: dict):
    try:
        update = Update.model_validate(data, context={"bot": platform_bot})
        await platform_dp.feed_update(platform_bot, update)
    except Exception:
        # === PATCH_LOG_TRACEBACK_V1 ===
        # قبلاً traceback با print() به stdout می‌رفت، پس توی error.log
        # ظاهر نمی‌شد و خطاهای ERR-XXXX عملاً بی‌سر و ته می‌موند. حالا با
        # logger.exception می‌ره داخلِ لاگِ اصلی که با کدِ خطا در همون پیام
        # بعدی قابلِ ردیابی‌ست.
        # === PATCH_ERR_CODE_LINK_V2 ===
        import datetime as _dt_err
        _err_code = _dt_err.datetime.now().strftime("%H%M%S")
        logger.exception(f"[ERR-{_err_code}] platform webhook error — update handling failed")
        # === PATCH_PLATFORM_NOTIFY_ERROR_V1 ===
        # قبلاً اینجا هیچ پیامی به کاربر نمی‌رفت (برخلافِ _process_tenant) و
        # کاربر با سکوتِ کامل مواجه می‌شد — انگار ربات اصلاً جواب نداده،
        # مجبور می‌شد /start را چندبار بزند. حالا مثل تننت، پیامِ خطا داده می‌شود.
        try:
            await _notify_processing_error(platform_bot, data, _err_code)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _notify_processing_error(bot: Bot, data: dict, err_code: str | None = None) -> None:
    """وقتی حین پردازشِ یک آپدیت استثنای مدیریت‌نشده‌ای رخ می‌دهد (مثلاً باگی در
    یک هندلر که پیش‌بینی نشده)، به‌جای سکوتِ کامل (که تا این‌جا باعث می‌شد مثلاً
    «شارژ کیف پول» بی‌هیچ پیام یا خطایی بی‌نتیجه بماند و کاربر فکر کند ربات جواب
    نداده)، یک پیامِ کوتاهِ خطا به فرستنده می‌دهد. این خودِ خطا را رفع نمی‌کند،
    ولی از سکوتِ گیج‌کننده جلوگیری می‌کند و امکانِ ردیابیِ خطای واقعی از لاگ سرور
    (که همان لحظه چاپ می‌شود) را برای دیباگ باقی می‌گذارد.

    === PATCH_ERROR_CODE_V1 / PATCH_ERR_CODE_LINK_V2 ===
    err_code: اگر صداکننده (مثلِ _process_tenant) کدِ خطا را همان لحظه‌ای که
    traceback را لاگ می‌کند تولید کرده باشد، همان را پاس می‌دهد تا کدی که
    کاربر می‌بیند دقیقاً همانی باشد که کنارِ traceback در error.log ثبت شده —
    وگرنه (fallback) همین‌جا یکی ساخته می‌شود.
    """
    import datetime as _dt
    if not err_code:
        err_code = _dt.datetime.now().strftime("%H%M%S")
    text = (f"⚠️ خطایی پیش‌بینی‌نشده رخ داد. لطفاً دوباره تلاش کن؛ "
            f"در صورت تکرار به پشتیبانی اطلاع بده و این کد رو بفرست: "
            f"<code>ERR-{err_code}</code>")
    # لاگِ خطا با همین کد تا وقتی کاربر گزارش کرد، توی لاگ قابلِ جستجوست:
    logger.error(f"[ERR-{err_code}] update handling failed — "
                 f"see traceback above (chat_id="
                 f"{_extract_chat_id(data)}, kind={_extract_update_kind(data)})")
    try:
        if "callback_query" in data:
            cq = data["callback_query"]
            cq_id = cq.get("id")
            if cq_id:
                try:
                    # answer_callback_query max 200 chars — پس کد رو کوتاه‌تر می‌فرستیم
                    await bot.answer_callback_query(
                        cq_id, text=f"⚠️ خطای پیش‌بینی‌نشده. کد: ERR-{err_code}",
                        show_alert=True)
                    return
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            return
        msg = data.get("message") or data.get("edited_message")
        if msg:
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


def _extract_chat_id(data: dict):
    try:
        if "callback_query" in data:
            return ((data["callback_query"].get("message") or {}).get("chat") or {}).get("id")
        msg = data.get("message") or data.get("edited_message")
        if msg:
            return (msg.get("chat") or {}).get("id")
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    return None


def _extract_update_kind(data: dict) -> str:
    for k in ("message", "callback_query", "my_chat_member", "chat_member",
              "edited_message", "channel_post", "chat_join_request"):
        if k in data:
            return k
    return "?"


_last_activity_write: dict[str, float] = {}
_ACTIVITY_WRITE_MIN_INTERVAL = 120  # ثانیه؛ نیازی نیست هر آپدیت نوشته شود


async def _process_tenant(bot: Bot, data: dict, token: str):
    # === PATCH_SLOW_UPDATE_TIMING_LOG_V1 ===
    # به‌جای نیازِ به هماهنگیِ دستیِ py-spy (که سخته دقیقاً لحظهٔ کندی رو
    # بگیره)، خودِ کد زمانِ هر آپدیت رو اندازه می‌گیره و اگه بیش از ۲ ثانیه
    # طول کشید، خودش با جزئیات (شاملِ نوعِ آپدیت) لاگ می‌کنه — بدونِ نیاز به
    # تست دستی، خودِ اولین کندیِ بعدی ثبت می‌شود.
    _t0 = asyncio.get_event_loop().time()
    try:
        upd = Update.model_validate(data, context={"bot": bot})
        await tenant_dp.feed_update(bot, upd)
    except Exception:
        # === PATCH_LOG_TRACEBACK_V1 / PATCH_ERR_CODE_LINK_V2 ===
        # کدِ خطا را همین‌جا (کنارِ traceback) می‌سازیم و داخلِ خطِ لاگ می‌گذاریم،
        # بعد همان را به _notify_processing_error می‌دهیم تا کدی که کاربر می‌بیند
        # دقیقاً همانی باشد که در error.log کنارِ traceback نوشته شده — برای
        # پیدا کردنِ فوریِ خطا با grep روی ERR-XXXXXX.
        import datetime as _dt_err
        err_code = _dt_err.datetime.now().strftime("%H%M%S")
        logger.exception(f"[ERR-{err_code}] tenant webhook error (token …{token[-6:]})")
        await _notify_processing_error(bot, data, err_code)
    finally:
        _elapsed = asyncio.get_event_loop().time() - _t0
        if _elapsed > 2.0:
            _kind = next((k for k in ("message", "callback_query", "my_chat_member")
                         if k in data), "?")
            # === PATCH_SLOW_UPDATE_DETAIL_V1 ===
            # علاوه بر نوعِ آپدیت، متنِ پیام/دیتای دکمه رو هم لاگ می‌کنیم تا
            # دقیقاً بفهمیم کدوم پیام/دکمه باعثِ کندی شده (بدونِ این، فقط
            # می‌دونستیم «یه پیام کند بود»، نه کدوم).
            _detail = ""
            try:
                if _kind == "message":
                    _detail = (data.get("message", {}).get("text") or
                              ("<file/media>" if data.get("message") else ""))[:60]
                elif _kind == "callback_query":
                    _detail = (data.get("callback_query", {}).get("data") or "")[:60]
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            print(f"[SLOW_UPDATE] {_elapsed:.2f}s token=…{token[-6:]} kind={_kind} detail={_detail!r}")
    # ثبت آخرین فعالیت ربات (برای اخطار/بایگانی خودکار غیرفعالی).
    # نوشتنِ این مقدار روی هر تک آپدیت (هر پیام/هر تپ دکمه) فشار نوشتنِ
    # غیرضروری روی SQLite ایجاد می‌کند (نویسنده‌ها سریالی‌اند و صف می‌شوند
    # → کندی/نیاز به دوبار زدنِ دکمه زیرِ بار). چون این مقدار فقط برای
    # تشخیصِ «غیرفعالیِ چندروزه» استفاده می‌شود، دقتِ ثانیه‌ای لازم نیست؛
    # حداکثر هر ۲ دقیقه یک‌بار برای هر ربات نوشته می‌شود.
    now_ts = asyncio.get_event_loop().time()
    last = _last_activity_write.get(token, 0.0)
    if now_ts - last < _ACTIVITY_WRITE_MIN_INTERVAL:
        return
    _last_activity_write[token] = now_ts
    try:
        async with Session() as s:
            await s.execute(
                update(BotRow).where(BotRow.token == token)
                .values(last_activity_at=datetime.datetime.now())
            )
            await s.commit()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@app.post("/webhook/platform")
async def platform_webhook(request: Request):
    if not platform_bot:
        return Response(status_code=503)
    try:
        data = await request.json()
        _spawn(_process_platform(data))
    except Exception:
        import traceback
        print("platform webhook parse error:\n" + traceback.format_exc())
    return Response()


@app.post("/webhook/{token}")
async def tenant_webhook(token: str, request: Request):
    bot, status = await _get_tenant_bot(token)
    if not bot:
        # ربات ناموجود/توکن خراب: 200 می‌دهیم تا تلگرام تلاش مجدد نکند
        return Response()
    try:
        data = await request.json()
    except Exception:
        import traceback
        print(f"tenant webhook parse error (token …{token[-6:]}):\n" + traceback.format_exc())
        return Response()
    if status != "active":
        # ربات موقتاً خاموش (paused) یا قفل‌شده توسط ادمین (locked): پردازشِ
        # کامل را رد می‌کنیم، فقط یک پیامِ مناسب (و در صورتِ امکان، دکمهٔ
        # راهنمای فعال‌سازی) به مالک می‌دهیم.
        maint_text = None
        maint_kb = None
        sender_id = (((data.get("message") or data.get("edited_message") or {})
                      .get("from") or {}).get("id")) or \
                    (((data.get("callback_query") or {}).get("from") or {}).get("id"))
        if status == "locked":
            async with Session() as s:
                brow = await s.scalar(select(BotRow).where(BotRow.token == token))
            reason = (brow.lock_reason if brow else "").strip() if brow and brow.lock_reason else ""
            # قفلِ ادمین معمولاً به‌خاطرِ تخلف است و راهِ خودسرویس ندارد؛ برای
            # همین دکمه‌ای اضافه نمی‌شود، فقط دلیل شفاف گفته می‌شود.
            maint_text = (
                f"🔒 این ربات توسط مدیریت قفل شده است.\n\nدلیل: {reason}" if reason else
                "🔒 این ربات توسط مدیریت قفل شده است.\n\n"
                "برای رفعِ قفل با پشتیبانیِ بات‌ساز در تماس باش."
            )
        elif status == "admin_paused":
            # غیرفعال‌سازیِ توسط ادمینِ پلتفرم: هیچ‌کس (حتی مالک) نمی‌تواند دور بزند.
            maint_text = (
                "🔴 این ربات توسط مدیریت بات‌ساز غیرفعال شده است.\n\n"
                "برای اطلاعِ بیشتر یا رفعِ غیرفعال‌سازی با پشتیبانیِ بات‌ساز در تماس باش."
            )
        else:
            # بایگانی (به دلیلِ عدم‌تمدیدِ پلن یا عدم‌فعالیتِ طولانی) یا خاموشیِ دستیِ خودِ مالک
            async with Session() as s:
                brow = await s.scalar(select(BotRow).where(BotRow.token == token))
            if brow and brow.archived_at and brow.archive_reason == "user_deleted":
                # این ربات توسط خودِ مالک حذف شده؛ به قصد اینکه برایِ کاربر
                # کاملاً «تمام‌شده» به‌نظر برسه، هیچ دکمه‌ی بازگردانی نشون داده
                # نمی‌شه (بازیابیِ واقعی فقط از طریقِ پشتیبانیِ بات‌ساز ممکنه).
                maint_text = "🗑 این ربات توسط مالکش حذف شده و دیگر در دسترس نیست."
            elif brow and brow.archived_at and sender_id == brow.owner_id:
                link = await _reactivation_link(brow)  # === PATCH_PARTNER_REACTIVATION_LINK_V1 ===
                if link:
                    maint_kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔓 رفتن به فعال‌سازیِ ربات", url=link)
                    ]])
                if brow.archive_reason == "inactivity":
                    maint_text = (
                        "🗄 ربات شما به دلیل <b>عدم فعالیت</b> (هیچ پیام/کلیکی در "
                        f"{INACTIVITY_WARN_DAYS}+{INACTIVITY_GRACE_DAYS} روزِ اخیر) به «بایگانی» "
                        "منتقل شده است.\n\n"
                        "✅ ربات و اطلاعاتش <b>حذف نشده</b>؛ با دکمهٔ زیر می‌تونی از بایگانی خارجش کنی."
                    )
                elif brow.archive_reason == "nonpayment":
                    maint_text = (
                        "🗄 ربات شما به دلیل <b>تمدید نکردنِ پلن</b> به «بایگانی» منتقل شده است.\n\n"
                        "✅ ربات و اطلاعاتش <b>حذف نشده</b>؛ با دکمهٔ زیر می‌تونی پلن رو تمدید و "
                        "بلافاصله از بایگانی خارجش کنی."
                    )
                else:
                    maint_text = (
                        "🗄 ربات شما به «بایگانی» منتقل شده است.\n\n"
                        "✅ ربات و اطلاعاتش <b>حذف نشده</b>؛ با دکمهٔ زیر می‌تونی دوباره فعالش کنی."
                    )
            elif brow and brow.archived_at:
                maint_text = "🗄 این ربات موقتاً غیرفعال است. لطفاً بعداً دوباره امتحان کنید."
            elif brow and not brow.archived_at and sender_id == brow.owner_id:
                # ربات به‌صورت دستی توسط خودِ مالک خاموش (paused ساده) شده — نه
                # بایگانیِ عدم‌تمدید و نه قفلِ ادمین. پیامِ خودِ مالک را عادی پردازش
                # کن (نه پیامِ نگه‌داری) تا بتواند دوباره همان‌جا دکمهٔ «روشن کردن
                # ربات» را بزند؛ وگرنه چون همین چک هر آپدیتی را قبل از رسیدن به آن
                # دکمه رد می‌کند، مالک برای همیشه پشتِ این پیام قفل می‌ماند.
                _spawn(_process_tenant(bot, data, token))
                return Response()
            else:
                maint_text = "🗄 این ربات موقتاً غیرفعال است. لطفاً بعداً دوباره امتحان کنید."
        _spawn(_maintenance_reply(bot, data, text=maint_text, reply_markup=maint_kb))
        return Response()
    _spawn(_process_tenant(bot, data, token))
    return Response()


@app.post("/nowpay/ipn")
async def nowpay_ipn(request: Request):
    """دریافت تأیید پرداخت ارز دیجیتال از NOWPayments و شارژ کیف پول."""
    from tenant.gateways.crypto_pay import verify_ipn_signature
    from models import CryptoCharge, BotUser, WalletTxn, Setting
    from sqlalchemy import select as _select
    from sqlalchemy import update as _update
    try:
        raw = await request.body()
        sig = request.headers.get("x-nowpayments-sig", "")
        data = await request.json()
        order_id = data.get("order_id", "")
        pay_status = (data.get("payment_status") or "").lower()
        if not order_id:
            return Response()
        notify = None
        async with Session() as s:
            ch = await s.scalar(_select(CryptoCharge).where(CryptoCharge.order_id == order_id))
            if not ch or ch.status == "paid":
                return Response()
            sec = await s.scalar(_select(Setting).where(
                Setting.bot_id == ch.bot_id, Setting.key == "nowpay_ipn_secret"))
            ipn_secret = sec.value if sec else ""
            if not verify_ipn_signature(raw, sig, ipn_secret):
                print("nowpay IPN: bad signature for", order_id)
                return Response(status_code=403)
            if pay_status not in ("finished", "confirmed"):
                return Response()  # فقط پرداخت‌های نهاییِ موفق
            # اگر amount_toman صفر است (کاربر دلار وارد کرده)، از نرخ دستی ادمین محاسبه می‌شود
            credit = float(ch.amount_toman)
            if credit <= 0 and ch.usd_amount:
                from models import Setting as _Setting
                rate_rec = await s.scalar(_select(_Setting).where(
                    _Setting.bot_id == ch.bot_id, _Setting.key == "toman_per_usd"))
                try:
                    manual_rate = float(rate_rec.value) if rate_rec else 0
                except Exception:
                    manual_rate = 0
                if manual_rate > 0:
                    credit = round(float(ch.usd_amount) * manual_rate)

            # ادعای اتمیکِ این شارژ با UPDATE ... WHERE status != 'paid': اگر
            # یک فراخوانیِ هم‌زمانِ دیگر (وبهوکِ تکراری، یا race با چکِ دستی)
            # قبلاً برنده شده باشد، rowcount اینجا صفر می‌شود و دوباره شارژ
            # نمی‌کنیم — به‌جای read-then-write قدیم که بین آن دو race ممکن بود.
            claim = await s.execute(
                _update(CryptoCharge)
                .where(CryptoCharge.order_id == order_id, CryptoCharge.status != "paid")
                .values(status="paid", amount_toman=credit)
            )
            if claim.rowcount == 0:
                await s.rollback()
                return Response()

            u = await s.scalar(_select(BotUser).where(
                BotUser.bot_id == ch.bot_id, BotUser.telegram_id == ch.user_telegram_id).with_for_update())
            if u and credit > 0:
                u.balance = float(u.balance) + credit
                s.add(WalletTxn(bot_id=ch.bot_id, user_id=u.id,
                                amount=credit, kind="crypto_charge",
                                note="شارژ با ارز دیجیتال"))
            await s.commit()
            notify = (ch.bot_id, ch.user_telegram_id, credit,
                      float(u.balance) if u else None)
        if notify:
            bot_id, tg_id, amt, new_balance = notify
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if brow:
                tbot = Bot(token=brow.token, default=DEFAULTS)
                try:
                    from tenant.gateways.wallet_receipt import build_wallet_charge_message
                    _msg, _kb = build_wallet_charge_message(
                        amt, new_balance if new_balance is not None else amt, "crypto")
                    await tbot.send_message(tg_id, _msg, reply_markup=_kb)
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                finally:
                    await tbot.session.close()
    except Exception:
        import traceback
        print("nowpay IPN error:\n" + traceback.format_exc())
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
        # پارس order_id: plt_{tg_id}_{timestamp}
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
        # پیدا کردن PlatformTxn در انتظار
        async with Session() as s:
            # === PATCH_WALLET_ROW_LOCK_V1 ===
            pending = await s.scalar(
                _select(PlatformTxn).where(
                    PlatformTxn.user_telegram_id == tg_id,
                    PlatformTxn.kind == "nowpay_pending",
                    PlatformTxn.note.contains(f"order_id={order_id}"),
                ).with_for_update())
            if not pending:
                return Response()
            # خواندن مبلغ تومان از note
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
            # بروزرسانی موجودی
            pu = await s.scalar(_select(PlatformUser).where(
                PlatformUser.telegram_id == tg_id).with_for_update())
            if pu:
                pu.balance = float(pu.balance or 0) + credit
            # ثبت تراکنش نهایی
            s.add(PlatformTxn(
                user_telegram_id=tg_id,
                amount=credit,
                kind="nowpay_charge",
                note=f"شارژ با ارز دیجیتال — order: {order_id}",
            ))
            pending.kind = "nowpay_done"  # علامت‌گذاری به‌عنوان پردازش‌شده
            await s.commit()
        # اطلاع به کاربر
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


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/swap-pay")
async def swap_pay_redirect(url: str = ""):
    """
    صفحه redirect برای باز کردن express-withdraw سواپ‌ولت از داخل مینی‌اپ تلگرام.
    کاربر روی دکمه WebApp می‌زنه → این صفحه باز می‌شه → فوری به URL سواپ‌ولت redirect می‌شه.
    چون در مینی‌اپ هستیم، تلگرام openLink را بدون popup اجرا می‌کند.
    """
    import urllib.parse
    safe_url = ""
    if url:
        decoded = urllib.parse.unquote(url)
        if decoded.startswith("https://swapwallet.app/"):
            safe_url = decoded
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh">
  <script>
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg) tg.ready();
    var target = {repr(safe_url)};
    if (target) {{
      if (tg && tg.openLink) {{
        tg.openLink(target);
        setTimeout(function(){{ if(tg.close) tg.close(); }}, 800);
      }} else {{
        window.location.href = target;
      }}
    }}
  </script>
  <p style="color:#fff;font-family:sans-serif;font-size:14px">در حال انتقال به سواپ‌ولت...</p>
</body>
</html>"""
    return Response(content=html, media_type="text/html")
