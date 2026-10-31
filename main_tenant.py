"""
main_tenant.py — نقطهٔ ورودِ مستقلِ بات‌های مستأجر.

=== PATCH_SPLIT_MAIN_V1 ===
نگاه کن به بالای main_platform.py برای توضیحِ کاملِ چراییِ این جداسازی.
این فایل عمداً platform_bot.py (۱۳هزار+ خط) را import نمی‌کند — فقط چیزی
که واقعاً لازم دارد (bot_utils._temp_bot، webhook_utils) — تا اگر روزی
platform_bot.py وقتِ importشدن خطا بدهد، این سرویس همچنان بالا بیاید.

اجرا (نمونهٔ systemd/uvicorn):
    uvicorn main_tenant:app --host 127.0.0.1 --port 8001

مسیرها:
  POST /webhook/{token}   → آپدیت‌های هر بات مستأجر
  POST /nowpay/ipn        → تأییدِ پرداختِ ارز دیجیتالِ کیف‌پولِ کاربرانِ مستأجر
  POST /tonpays/callback  → کال‌بکِ درگاه TonPays (با تاییدِ مستقلِ سمتِ ما)
  POST /pay/{tc}          → صفحهٔ پرداخت (مشترک با main_platform.py)
  GET  /health
  GET  /swap-pay
  و مسیرهای دیگرِ tenant.shared.web_view / tenant.shared.tutorial_page
"""
from __future__ import annotations

import asyncio
import datetime
import time
import os

# === PATCH_ASYNCIO_EXECUTOR_SIZE_V1 ===
# (توضیحِ کامل پایین‌ترِ همین فایل، کنارِ on_startup، چون باید داخلِ همون
# event loopی اجرا بشه که uvicorn/uvloop واقعاً استفاده می‌کنه — نه لوپِ
# موقتی‌ای که ماژول در زمانِ import ممکنه بسازه.)
from concurrent.futures import ThreadPoolExecutor as _TPE

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage  # === PATCH_REDIS_FSM_STORAGE_V1 ===
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.types import Update, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError  # PATCH_MESSAGE_NOT_MODIFIED_GLOBAL_V1
from fastapi import FastAPI, Request, Response
from sqlalchemy import select, update  # === PATCH_TENANT_ACTIVITY_TRACKING_V1 ===
from config import PLATFORM_BOT_TOKEN, Session, init_db
from models import Bot as BotRow
from models import VpnPanel, PanelAccount, Setting

from tenant import tenant_router, swap_poll_loop, fallback_router
from tenant.shared.ad_broadcast import ad_broadcast_loop, ad_scheduled_posts_loop
from tenant.shared.web_view import web_router
from tenant.shared.web_panel import web_panel_router  # === PATCH_WEB_PANEL_V1 ===
from tenant.gateways.payment_page import pay_router
from tenant.shared.tutorial_page import tutorial_router
from tenant.shared.file_link import file_link_web_router  # === PATCH_FILE_LINK_CONVERTER_V1 ===
from tenant.shared.miniapp import miniapp_router  # === PATCH_MINIAPP_V1 ===
from tenant.bots.virtual_service.nft_market import nft_market_router  # === PATCH_NFT_MARKET_V1 ===
from tenant.bots.virtual_service.nft_p2p import nft_p2p_api_router  # === PATCH_P2P_MARKET_V1 ===
from tenant.shared.public_api import public_api_router  # === PATCH_PUBLIC_API_V1 ===

from bot_utils import _temp_bot
from webhook_utils import spawn, maintenance_reply, notify_processing_error

import tenant_status_bus

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

# === PATCH_NO_LINK_PREVIEW_V1 === کارتِ پیش‌نمایشِ لینک پیش‌فرض خاموش است؛ جاهایی که لازم است (گیفتِ NFT و ...) صریحاً روشنش می‌کنند
DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)

app = FastAPI()
app.include_router(web_router)
app.include_router(web_panel_router)  # === PATCH_WEB_PANEL_V1 ===
app.include_router(pay_router)
app.include_router(tutorial_router)
app.include_router(file_link_web_router)  # === PATCH_FILE_LINK_CONVERTER_V1 ===
app.include_router(miniapp_router)  # === PATCH_MINIAPP_V1 ===
app.include_router(nft_market_router)  # === PATCH_NFT_MARKET_V1 ===
app.include_router(nft_p2p_api_router)  # === PATCH_P2P_MARKET_V1 ===
app.include_router(public_api_router)  # === PATCH_PUBLIC_API_V1 ===

tenant_dp = Dispatcher(storage=RedisStorage.from_url(
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
    key_builder=DefaultKeyBuilder(with_bot_id=True)))
# === PATCH_REDIS_FSM_STORAGE_V1 ===
# قبلاً Dispatcher() بدونِ storage ساخته می‌شد — یعنی aiogram به‌صورتِ
# پیش‌فرض از MemoryStorage (درون‌حافظه‌ای) استفاده می‌کرد. تا وقتی این
# سرویس با یک ورکرِ تک اجرا می‌شد مشکلی نداشت، ولی این یعنی هیچ‌وقت
# نمی‌شد بیش از یک ورکر اجرا کرد — چون حالتِ گفتگو (FSM) در حافظهٔ همان
# یک پروسه می‌ماند و اگر پیامِ بعدیِ یک کاربر به ورکرِ دیگری برسد، ربات
# اصلاً از مرحله‌ای که کاربر در آن بود خبر ندارد (باگِ تصادفی‌شدنِ
# گفتگوهایِ چندمرحله‌ای، مثلِ «ساختِ سرویسِ دستی»). حالا با Redis، همهٔ
# ورکرها به یک حافظهٔ مشترک وصل‌اند.
#
# نکتهٔ حیاتی که با تست پیدا شد: KeyBuilderِ پیش‌فرضِ aiogram
# (with_bot_id=False) اصلاً bot_id را در کلید نمی‌گذارد! یعنی بدونِ این
# پارامتر، حالتِ گفتگویِ یک کاربر بینِ همهٔ ۸۴+ ربات (که حالا یک Redis
# مشترک دارند) قاطی می‌شد — کاربری که هم‌زمان با دو ربات کار می‌کند، در
# رباتِ دوم انگار وسطِ گفتگویِ رباتِ اول می‌ماند. با with_bot_id=True،
# هر رباتی کاملاً ایزوله می‌ماند — دقیقاً مثلِ قبل، فقط حالا بینِ ورکرها
# هم مشترک است.
# === PATCH_REPLY_LABEL_RESTORE_V1 ===
# دکمه‌هایِ کیبوردِ پایینِ صفحه‌ای که ایموجی‌شان آیکونِ پرمیوم شده، متنِ بدونِ ایموجی می‌فرستند و
# هندلرها (تطابقِ دقیق با «🏠 بازگشت به خانه» و ...) جواب نمی‌دادند. قبل از هر هندلر برمی‌گردد.
from reply_label_registry import install_reply_label_restore
install_reply_label_restore(tenant_dp)

# === PATCH_GLASS_EDIT_GENERIC_V1 ===
# منویِ شیشه‌ای: رویِ *هر* دکمهٔ کاربر همان پیام ویرایش شود، نه پیامِ تازه. کلیدِ خاموش: GLASS_EDIT_GENERIC=0
from tenant.shared.glass_generic import install_glass_generic
install_glass_generic(tenant_router)
# === PATCH_PARTNER_V8 ===
# باید *قبل* از tenant_router ثبت شود تا هندلرهایِ هدایتِ ربات همکار
# بر هندلرهایِ عمومیِ tadm:sec:* اولویت داشته باشند.
from tenant.partner_bot.partner_menu import partner_menu_router as _pnr_menu_router
# === PATCH_TENANT_WORKER_KIND_V1 ===
# دقیقاً همان منطقِ tenant/__init__.py._include — این‌جا هم رعایت می‌شود
# چون این ثبت رویِ dispatcher است، نه tenant_router؛ آن تابع این‌جا در
# دسترس نیست. اگر TENANT_BOT_KIND ست شده و partner_bot در آن نبود، این
# روتر ثبت نمی‌شود.
import os as _os_wk
_wk_kinds = set(filter(None, _os_wk.environ.get("TENANT_BOT_KIND", "").split(",")))
if not _wk_kinds or "partner_bot" in _wk_kinds:
    tenant_dp.include_router(_pnr_menu_router)
# === PATCH_TENANT_HOME_STICKER_V1 ===
from tenant.shared.home_sticker_admin import home_sticker_admin_router
tenant_dp.include_router(home_sticker_admin_router)
tenant_dp.include_router(tenant_router)
tenant_dp.include_router(fallback_router)   # آخر از همه: خودترمیمی حالت‌های گیرکرده


# === PATCH_MESSAGE_NOT_MODIFIED_GLOBAL_V1 ===
# «message is not modified» یک خطایِ واقعی نیست: یعنی ادمین/کاربر دکمه‌ای
# را زده که همان صفحهٔ فعلی را دوباره می‌سازد (تپِ دوباره رویِ یک دکمه،
# برگشت به همان صفحه، یا تیک‌زدنی که چیزی را عوض نکرده). تلگرام رد می‌کند
# و — چون edit_text در ۸۷ نقطه بدونِ try/except صدا زده می‌شود — کلِ
# پردازشِ آپدیت می‌ترکد. نتیجه برایِ کاربر بدتر از خودِ خطاست: چون
# `cb.answer()` بعد از edit است و هرگز اجرا نمی‌شود، اسپینرِ روی دکمه
# تا چند ثانیه می‌چرخد و انگار ربات هنگ کرده.
#
# به‌جایِ وصله‌کردنِ ۸۷ نقطه (که نقطهٔ ۸۸ام را هم فردا کسی بدونِ محافظ
# اضافه می‌کند)، یک هندلرِ سراسری: این حالتِ خاص بی‌صدا بلعیده می‌شود و
# فقط اسپینرِ دکمه بسته می‌شود. هر خطایِ دیگری دست‌نخورده بالا می‌رود تا
# در لاگ دیده شود.
@tenant_dp.errors()
async def _tenant_swallow_not_modified(event) -> bool:
    exc = getattr(event, "exception", None)
    if not isinstance(exc, TelegramBadRequest):
        return False
    if "message is not modified" not in str(exc):
        return False
    cb = getattr(getattr(event, "update", None), "callback_query", None)
    if cb is not None:
        try:
            await cb.answer()          # اسپینر را ببند، سکوت کامل
        except Exception:
            logger.debug("بستنِ اسپینرِ callback بعد از not-modified ناموفق بود")
    logger.debug("not-modified نادیده گرفته شد (صفحه از قبل همین بود)")
    return True                        # یعنی رسیدگی شد؛ به‌عنوانِ خطا لاگ نشود


from tenant.shared.chat_gone import swallow_chat_gone as _tenant_swallow_chat_gone

tenant_dp.errors()(_tenant_swallow_chat_gone)


# === PATCH_RAPID_REPEAT_THROTTLE_V1 ===
# وقتی کاربر (از رویِ کندیِ حس‌شده یا عجله) ظرفِ چند ثانیه، ده بار پشتِ‌سرِهم
# دقیقاً همون اکشن رو تکرار می‌کنه — همون دکمه، همون دستور، یا /start — همه‌شون
# وارد صفِ پردازش می‌شن و بارِ اضافه می‌سازن (که خودش کندی رو بدتر می‌کنه).
# این میان‌افزار، تکرارِ سریعِ همون اکشن رو تشخیص می‌ده: بعدِ دهمین بار،
# به‌جایِ پردازشِ کامل، یک پیامِ کوتاه می‌ده و تا ۵ ثانیه همون اکشن را
# بی‌سروصدا نادیده می‌گیرد (بدونِ ارسالِ پیامِ تکراری). اکشن‌های دیگر (مثلاً
# یه دکمهٔ متفاوت) در همین حین عادی پردازش می‌شوند — این فقط جلویِ
# تکرارِ *همون* کلیک/پیام رو می‌گیرد، نه کلِ ربات را برای کاربر قفل نمی‌کند.
import time as _rrt_time


class _RapidRepeatThrottle:
    # === PATCH_RAPID_THROTTLE_10_5_V1 ===
    # قبلاً ۳ بار در ۸ ثانیه بود؛ طبقِ درخواستِ ادمین برده شد رویِ ۱۰ بار
    # در ۵ ثانیه (سخت‌گیریِ کمتر — کاربرهایِ معمولی که چندبار پشتِ‌سرِهم
    # سریع کلیک می‌کنن دیگه به این راحتی گیرِ محدودیت نمی‌افتن).
    WINDOW = 5.0     # اگه به همین اندازه ثانیه، ۱۰ بار تکرار بشه → اسپم حساب می‌شه
    COOLDOWN = 5.0   # بعدِ تشخیص، تا ۵ ثانیه همان اکشن نادیده گرفته می‌شود
    LIMIT = 10

    def __init__(self) -> None:
        self._hits: dict[tuple, list[float]] = {}
        self._muted_until: dict[tuple, float] = {}

    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        if bot is None:
            return await handler(event, data)

        cq = getattr(event, "callback_query", None)
        msg = getattr(event, "message", None)
        if cq is not None:
            user = cq.from_user
            action = f"cb:{cq.data}"
        elif msg is not None and msg.from_user is not None:
            user = msg.from_user
            action = f"msg:{msg.text or msg.content_type}"
        else:
            return await handler(event, data)

        now = _rrt_time.monotonic()
        key = (bot.id, user.id, action)

        muted_until = self._muted_until.get(key)
        if muted_until is not None:
            if muted_until > now:
                # داخلِ سکوتِ کول‌داون — کاملاً نادیده بگیر (بدونِ پیامِ
                # تکراری، تا خودِ پیام‌دادن هم به اسپم دامن نزند)
                # === PATCH_THROTTLE_DEBUG_V1 ===
                logger.info(f"[RAPID_THROTTLE] بی‌صدا نادیده گرفته شد | key={key}")
                if cq is not None:
                    try:
                        await bot.answer_callback_query(cq.id)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                return
            self._muted_until.pop(key, None)
            self._hits.pop(key, None)

        hits = self._hits.setdefault(key, [])
        hits.append(now)
        hits[:] = [t for t in hits if now - t <= self.WINDOW]

        if len(hits) >= self.LIMIT:
            self._muted_until[key] = now + self.COOLDOWN
            self._hits.pop(key, None)
            # === PATCH_THROTTLE_DEBUG_V1 ===
            logger.info(f"[RAPID_THROTTLE] تازه فعال شد (موت تا {self.COOLDOWN}s) | key={key}")
            text = "⏳ یه کم آروم‌تر — لطفاً ۵ ثانیهٔ دیگه دوباره امتحان کن."
            try:
                if cq is not None:
                    await bot.answer_callback_query(cq.id, text=text, show_alert=True)
                elif msg is not None:
                    await bot.send_message(msg.chat.id, text)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (rapid-repeat throttle)")
            return

        return await handler(event, data)


tenant_dp.update.outer_middleware(_RapidRepeatThrottle())

_tenant_bots: dict[str, Bot] = {}
_platform_bot_username: str | None = None  # فقط برای ساختِ دیپ‌لینکِ بازگشت به بات‌ساز
# کشِ کوتاه‌مدتِ وضعیتِ ربات‌ها تا در هر تک‌آپدیت به دیتابیس کوئری نزنیم
_tenant_status_cache: dict[str, tuple[str | None, float]] = {}
_TENANT_STATUS_TTL = 5.0  # ثانیه؛ fallback اگر NOTIFYِ tenant_status_bus هر دلیلی نرسید


def _clear_local_tenant_cache(token: str | None = None) -> None:
    """کشِ وضعیت + Bot instanceِ کش‌شدهٔ این توکن (یا همه، اگر token=None) را
    پاک می‌کند. هم وقتی خودِ این پروسه یک بات را حذف/تغییر می‌دهد صدا زده
    می‌شود، هم به‌عنوانِ callbackِ listenerِ tenant_status_bus (وقتی
    main_platform.py سیگنالِ NOTIFY می‌فرستد).

    MARKER_BOTROW_CACHE_CROSSPROCESS_V1: قبلاً فقط همین دو کشِ سبک پاک می‌شدن؛ کشِ کاملِ BotRow در
    tenant/__init__.py (`_BOT_ROW_CACHE` — پایهٔ is_owner_or_admin و کلِ
    پنلِ مدیریت) دست‌نخورده می‌موند، پس بعدِ اقداماتِ ادمینِ پلتفرم
    (بازگردانی از بایگانی و مشابه) تا ۲۰ ثانیه یا بیشتر همچنان نسخهٔ کهنه
    برمی‌گشت."""
    if token:
        _tenant_status_cache.pop(token, None)
        _close_bot_later(_tenant_bots.pop(token, None))
    else:
        _tenant_status_cache.clear()
        for _b in list(_tenant_bots.values()):
            _close_bot_later(_b)
        _tenant_bots.clear()
    try:
        from tenant import invalidate_bot_row_cache
        invalidate_bot_row_cache(token)
    except Exception:
        logger.exception("MARKER_BOTROW_CACHE_CROSSPROCESS_V1: باطل‌سازیِ کشِ BotRow ناموفق بود")


def _close_bot_later(bot: "Bot | None", delay: float = 60.0) -> None:
    """نشستِ HTTPِ یک Botِ کنارگذاشته‌شده را با کمی تأخیر می‌بندد.

    قبلاً Bot فقط از کش pop می‌شد و نشستِ aiohttpاش هیچ‌وقت بسته نمی‌شد —
    error.log پر از «Unclosed client session / Unclosed connector» بود. تأخیر
    برایِ این است که درخواست‌هایِ در جریان با همین Bot وسطِ کار نشکنند."""
    if bot is None:
        return

    async def _close():
        await asyncio.sleep(delay)
        try:
            await bot.session.close()
        except Exception:
            logger.debug("بستنِ نشستِ Botِ کنارگذاشته ناموفق بود", exc_info=True)

    try:
        asyncio.get_running_loop().create_task(_close())
    except RuntimeError:
        pass  # بیرون از event loop (مثلاً تست‌هایِ همگام) — چیزی برایِ بستن نیست


async def _get_tenant_bot(token: str) -> tuple[Bot | None, str | None]:
    """ربات مستأجر را همراه با وضعیتش برمی‌گرداند. وضعیت در هر درخواست تازه
    از دیتابیس خوانده می‌شود تا تغییرات (خاموش/روشن، قفل ادمین، توکن خراب)
    بلافاصله اعمال شوند (بدون وابستگی به کش)."""
    now_mono = asyncio.get_event_loop().time()
    cached = _tenant_status_cache.get(token)
    if cached is not None and cached[1] > now_mono:
        status = cached[0]
    else:
        async with Session() as s:
            status = await s.scalar(select(BotRow.status).where(BotRow.token == token))
        _tenant_status_cache[token] = (status, now_mono + _TENANT_STATUS_TTL)
    if not status or status == "disabled_bad_token":
        _close_bot_later(_tenant_bots.pop(token, None))
        return None, status
    bot = _tenant_bots.get(token)
    if bot is None:
        bot = Bot(token=token, default=DEFAULTS)
        _tenant_bots[token] = bot
    return bot, status


def _platform_deep_link(payload: str) -> str | None:
    """لینکِ عمیقِ t.me/<بات‌ساز>?start=<payload> — برای دکمه‌ای که کاربر را
    مستقیم به بخشِ مشخصی از خودِ بات‌ساز می‌برد."""
    if not _platform_bot_username:
        return None
    return f"https://t.me/{_platform_bot_username}?start={payload}"


# ───────────────────── حلقه‌های پس‌زمینهٔ مستأجر ─────────────────────

async def _warm_tenant_bots():
    """کانکشنِ HTTPS ربات‌های مستأجرِ فعال را از قبل گرم می‌کند."""
    try:
        async with Session() as s:
            tokens = (await s.scalars(
                select(BotRow.token).where(BotRow.status == "active"))).all()
    except Exception as e:
        print("warm_tenant_bots query error:", e)
        return
    sem = asyncio.Semaphore(20)

    # === PATCH_WARM_TENANT_SKIP_ALREADY_WARM_V1 ===
    # توکن‌هایی که از قبل یک Bot-instanceِ زنده دارند نیازی به بازسازی
    # ندارند — فقط توکن‌هایِ تازه (که هنوز در _tenant_bots نیستند) گرم
    # می‌شوند. این جلویِ ساختِ ۸۷ کانکشنِ زائدِ HTTPS در هر ۳ دقیقه را
    # می‌گیرد.
    already_warm = sum(1 for t in tokens if t in _tenant_bots)
    # PATCH_SHARED_TG_SSL_CONTEXT_V1 — توکنی که گرم نشد (باطل/حذف‌شده) یک ساعت دوباره امتحان نمی‌شود
    _now_w = time.monotonic()
    to_warm = [t for t in tokens if t not in _tenant_bots and _WARM_FAIL_UNTIL.get(t, 0) <= _now_w]

    async def _warm_one(token: str):
        async with sem:
            b = None
            try:
                b = Bot(token=token, default=DEFAULTS)
                await b.get_me()
                old_b = _tenant_bots.get(token)
                _tenant_bots[token] = b
                if old_b is not None and old_b is not b:
                    try:
                        await old_b.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            except Exception:
                _WARM_FAIL_UNTIL[token] = time.monotonic() + 3600
                if b is not None:
                    try:
                        await b.session.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if to_warm:
        await asyncio.gather(*(_warm_one(t) for t in to_warm), return_exceptions=True)
    print(f"warm_tenant_bots: {already_warm} از قبل گرم بودند، {len(to_warm)} تازه گرم شد "
          f"({len(_tenant_bots)}/{len(tokens)} مجموع)")


_WARM_FAIL_UNTIL: dict[str, float] = {}


async def _warm_tenant_bots_loop():
    while True:
        await asyncio.sleep(180)
        try:
            await _warm_tenant_bots()
        except Exception as e:
            print("warm_tenant_bots_loop error:", e)


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


# === PATCH_UNAUTHORIZED_TOKEN_QUIET_V1 ===
# لاگ: ۱۱۰+ traceback از TelegramUnauthorizedError — توکنِ رباتِ مستأجر باطل
# (revoke) شده و حلقهٔ هشدار برایِ *هر* اکانتِ آن ربات یک traceback کامل می‌ریخت.
# حالا برایِ هر ربات فقط یک warning در هر ساعت ثبت می‌شود.
_UNAUTH_LOGGED: dict = {}


def _log_unauthorized(tbot) -> None:
    import time as _t
    try:
        key = getattr(tbot, "id", None) or id(tbot)
    except Exception:
        key = id(tbot)
    now = _t.monotonic()
    if now - _UNAUTH_LOGGED.get(key, -1e9) > 3600:
        _UNAUTH_LOGGED[key] = now
        logger.warning("توکنِ رباتِ %s باطل شده (Unauthorized) — ارسال‌ها برایِ این ربات رد می‌شود.", key)



_MISSING_CLIENT_LOGGED: dict[int, float] = {}


def _missing_client_should_log(account_id) -> bool:
    """هشدارِ «کلاینت رویِ پنل نیست» برایِ هر حساب حداکثر روزی یک‌بار.
    PATCH_LOG_THROTTLE_PERSIST_V1: زمان رویِ دیسک می‌ماند تا ری‌استارت/پروسهٔ دیگر دوباره لاگ نکند."""
    from log_throttle import should_log
    return should_log(f"vpnmiss:{account_id}", 86_400)


async def vpn_expiry_loop():
    """اخطار روزانه به کاربران VPN خودکار + حذف خودکار سرویس‌های منقضی‌شده."""
    from aiogram.exceptions import TelegramForbiddenError
    from tenant.shared.panel_clients.vpn_panel import GB
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    from tenant.bots.vpn.vpn_user import new_svc_code

    GRACE_DAYS = 2



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
                    changed = False
                    for a in accts:
                        if not a.svc_code:
                            a.svc_code = await new_svc_code(s, bot_id)
                            changed = True
                    if changed:
                        await s.commit()
                    # === PATCH_EXPIRY_WARN_QUOTA_V1 === شمارندهٔ اخطار هم لازم است
                    # === PATCH_METERED_PLAN_V1 === سرویس‌هایِ اعتباری: «اتمامِ حجم» یعنی تمامِ موجودی؛ اخطار/حذفِ حجمی نه
                    from models import MeteredAccount as _MA
                    _metered_ids = set((await s.scalars(select(_MA.account_id).where(
                        _MA.bot_id == bot_id, _MA.status.in_(("active", "no_credit"))))).all())
                    acct_list = [(a.id, a.email, a.user_telegram_id, a.plan_title,
                                  a.expiry_notice, a.svc_code, a.volume_gb, a.days,
                                  a.expires_at, int(a.expiry_warn_count or 0),
                                  a.traffic_out_at)  # PATCH_TRAFFIC_OUT_CLEANUP_V1
                                 for a in accts]
                if not brow or brow.status != "active" or not acct_list:
                    continue
                client = make_panel_client_from_fields(
                    panel_key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound, api_key=api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                try:
                    clients = await client.list_clients()
                except Exception as e:
                    # قبلاً بی‌صدا رد می‌شد؛ پنلی که همیشه شکست بخورد هیچ
                    # هشدارِ انقضا/حجمی برایِ مشتری‌هایش نمی‌فرستاد و ردی هم نبود.
                    logger.warning("vpn_expiry_loop: خواندنِ کاربرانِ پنلِ %s (bot_id=%s) ناموفق؛ "
                                   "این دور رد شد: %s", panel_key, bot_id,
                                   (str(e) or type(e).__name__)[:200])
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
                # === PATCH_EXPIRY_WARN_QUOTA_V1 ===
                # سقفِ اخطارِ پس از انقضا، قابلِ تنظیم توسطِ مالکِ ربات
                # (کلیدِ Setting: expiry_warn_limit). پیش‌فرض ۳ اخطار.
                try:
                    warn_limit = int(await _setting_get(bot_id, "expiry_warn_limit", "3"))
                except (TypeError, ValueError):
                    warn_limit = 3
                warn_limit = max(1, min(warn_limit, 10))
                try:
                    for (aid, email, tg_id, title, last_notice, code,
                         vol_gb, p_days, exp_at, warn_cnt,
                         traffic_out_at) in acct_list:
                        # کانفیگ‌های تست رایگان چرخهٔ جداگانه‌ای دارند
                        # (tenant/trial_cleanup.py): یک اخطار + حذفِ کامل
                        # چند ساعت بعد. اینجا دست‌نخورده رد می‌شوند.
                        if title and title.strip() == "تست رایگان":
                            continue
                        c = by_email.get(email)
                        # === PATCH_VPN_ALERT_MISSING_CLIENT_LOG_V1 ===
                        # باگِ گزارش‌شده (تیکت #408): اگر پنل هیچ کلاینتی با
                        # این email برنگرداند (مثلاً یوزرنیم روی پنل تغییر
                        # کرده یا اکانت دوباره ساخته شده)، c برابر None
                        # می‌شد، cap صفر می‌شد، و کل بررسیِ حجم/انقضا برایِ
                        # این حساب بی‌صدا رد می‌شد — نه پیامی، نه لاگی، هیچ
                        # اثری از خطا. حالا حداقل یک لاگ ثبت می‌شود تا این
                        # حالت قابلِ‌ردیابی باشد، نه یک سکوتِ کاملاً نامرئی.
                        # هر دور (هر ~۲۰ دقیقه) برایِ همان حساب تکرار می‌شد و error.log
                        # را با ده‌ها هزار خطِ تکراری پر می‌کرد؛ حالا روزی یک‌بار.
                        # PATCH_VPN_MISSING_CLIENT_SUMMARY_V1 — هزاران حسابِ حذف‌شده رویِ پنل روزی
                        # ده‌ها هزار خط می‌ساخت؛ حالا برایِ هر (ربات، پنل) روزی یک خط.
                        if c is None and _missing_client_should_log(f"{bot_id}:{panel_key}"):
                            logger.warning(
                                "vpn_expiry_loop: پنلِ %s (bot_id=%s) برایِ بعضی حساب‌ها (مثلاً %r، "
                                "PanelAccount#%s) کلاینتی برنگرداند — احتمالاً رویِ پنل حذف شده‌اند؛ "
                                "هشدارِ حجم/انقضایشان رد شد (روزی یک‌بار گزارش می‌شود).",
                                panel_key, bot_id, email, aid)
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

                        # === PATCH_EXPIRY_WARN_QUOTA_V1 ===
                        # قبلاً حذف صرفاً زمانی بود (GRACE_DAYS روز پس از
                        # انقضا) — یعنی ممکن بود کاربر فقط یکی دو اخطار
                        # گرفته باشد و سرویسش پاک شود. حالا مبنا «تعدادِ
                        # اخطارِ واقعاً ارسال‌شده» است: بعد از سومین اخطار
                        # (سقفِ قابلِ تنظیمِ warn_limit) سرویس هم از پنل و
                        # هم از «کانفیگ‌های من» حذف می‌شود. مهلتِ زمانی فقط
                        # به‌عنوانِ سوپاپِ اطمینان می‌ماند (اگر کاربر ربات
                        # را بلاک کرده باشد و اخطارها به مقصد نرسند، حساب
                        # تا ابد روی پنل نماند).
                        _hard_grace_ms = (warn_limit + GRACE_DAYS) * 86_400_000
                        if exp > 0 and (
                                (now_ms > exp and warn_cnt >= warn_limit)
                                or now_ms > exp + _hard_grace_ms):
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
                                f"<blockquote>⚠️ پس از انقضای سرویس، {warn_limit} "
                                "اخطار برای شما ارسال شد و تمدیدی انجام نگرفت. "
                                "این سرویس اکنون هم از پنل و هم از بخش "
                                "«کانفیگ‌های من» حذف شده و قابل بازیابی نیست. "
                                "در صورت نیاز می‌توانید سرویس جدید ایجاد کنید.</blockquote>")
                            try:
                                await tbot.send_message(tg_id, msg)
                            except TelegramUnauthorizedError:
                                _log_unauthorized(tbot)
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            continue

                        # === PATCH_VPN_ALERT_OVERWRITE_FIX_V1 ===
                        # باگِ گزارش‌شده (تیکت #408): وقتی هم هشدارِ حجم و
                        # هم هشدارِ روزهایِ باقی‌مانده هم‌زمان صادق بود،
                        # چونِ warn یک متغیرِ تکی بود و بلاکِ «روزهایِ
                        # باقی‌مانده» بدونِ چک‌کردنِ اینکه قبلاً warn ست شده
                        # یا نه دوباره رویش می‌نوشت، پیامِ حجم بی‌صدا از
                        # بین می‌رفت و اصلاً فرستاده نمی‌شد. حالا هر دو نوع
                        # هشدار (اگر هرکدام صادق باشد) جمع می‌شوند و با هم
                        # در یک پیام فرستاده می‌شوند — هیچ‌کدام گم نمی‌شود.
                        warns = []
                        if cap > 0 and aid not in _metered_ids:   # PATCH_METERED_PLAN_V1
                            left_gb = max(cap - used, 0) / GB
                            if left_gb <= 0:
                                # === PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
                                # باگِ گزارش‌شده: «هر روز داره برای کاربر
                                # ارسال می‌شه، روزِ سومه». علتش این بود که
                                # حذفِ خودکار فقط به `exp` (انقضایِ زمانی)
                                # نگاه می‌کرد؛ سرویسی که حجمش تمام شده ولی
                                # هنوز تاریخش نگذشته، نه شمرده می‌شد و نه
                                # هیچ‌وقت به حذف می‌رسید — پس همان هشدار تا
                                # ابد هر روز تکرار می‌شد.
                                #
                                # حالا دقیقاً مثلِ کانفیگِ تست: نخستین‌بار
                                # مهر زده می‌شود و یک اخطار می‌رود؛ حذفِ
                                # واقعی را حلقهٔ سبکِ traffic_cleanup چند
                                # ساعت بعد انجام می‌دهد (پیش‌فرض ۵ ساعت).
                                if traffic_out_at is None:
                                    async with Session() as s:
                                        a2 = await s.get(PanelAccount, aid)
                                        if a2:
                                            a2.traffic_out_at = datetime.datetime.utcnow()
                                            await s.commit()
                                    warns.append(
                                        "⚠️ سرویس شما به دلیل <b>اتمام ترافیک</b> غیرفعال شد.\n\n"
                                        "<blockquote>‼️ با خرید <b>ترافیک اضافه</b> یا "
                                        "<b>تمدید سرویس</b>، اتصال خود را همین حالا بازیابی "
                                        "کنید و اینترنت پایدار و بدون محدودیت را برای خود "
                                        "حفظ کنید!</blockquote>")
                                # اگر قبلاً مهر خورده، دیگر هیچ اخطاری تکرار
                                # نمی‌شود — یا کاربر ترافیک می‌خرد یا حذف می‌شود.
                            elif used > 0 and left_gb <= _eff_gb_threshold(
                                    gb_threshold_f, cap / GB):
                                warns.append(
                                    "⚠️ حجم سرویس شما <b>رو به اتمام</b> است "
                                    f"(باقی‌مانده حدود <b>{round(left_gb, 2)} گیگ</b>).\n\n"
                                    "<blockquote>‼️ با خرید <b>ترافیک اضافه</b> یا "
                                    "<b>تمدید سرویس</b>، پیش از قطعی اتصال خود را تضمین "
                                    "کنید!</blockquote>")
                        # === PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
                        # کاربر ترافیک خریده یا سرویس تمدید شده → مهر باطل
                        # شود تا چرخهٔ حذف متوقف و از صفر شروع شود.
                        if cap > 0 and (cap - used) / GB > 0 and traffic_out_at is not None:
                            async with Session() as s:
                                a2 = await s.get(PanelAccount, aid)
                                if a2:
                                    a2.traffic_out_at = None
                                    await s.commit()
                            logger.info("traffic_out ریست شد برایِ %r (حجم دوباره شارژ شد)", email)

                        # === PATCH_EXPIRY_WARN_QUOTA_V1 ===
                        # اخطارِ پس از انقضا حالا شماره‌دار است تا کاربر
                        # دقیقاً بداند چند اخطار تا حذفِ کامل باقی مانده.
                        expired_warn = False
                        if exp > 0:
                            days_left = (exp - now_ms) / 86_400_000
                            if days_left < 0:
                                expired_warn = True
                                _n = warn_cnt + 1
                                _left = max(warn_limit - _n, 0)
                                warns.append(
                                    "⛔️ سرویس شما <b>منقضی شده</b> است.\n"
                                    f"🔔 اخطار <b>{_n}</b> از <b>{warn_limit}</b>\n\n"
                                    "<blockquote>‼️ "
                                    + (f"تنها <b>{_left} اخطار</b> دیگر باقی مانده؛ پس از آن "
                                       if _left else "این <b>آخرین اخطار</b> است؛ ")
                                    + "سرویس شما به‌صورت خودکار و دائمی از پنل و از بخش "
                                      "«کانفیگ‌های من» <b>حذف</b> می‌شود!</blockquote>")
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
                            except TelegramUnauthorizedError:
                                _log_unauthorized(tbot)
                            except TelegramForbiddenError:
                                pass
                            except Exception:
                                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                            async with Session() as s:
                                a2 = await s.get(PanelAccount, aid)
                                if a2:
                                    a2.expiry_notice = today
                                    # === PATCH_EXPIRY_WARN_QUOTA_V1 ===
                                    # فقط اخطارِ «منقضی شده» شمرده می‌شود؛
                                    # هشدارِ حجم یا «۳ روز مانده» سهمی از
                                    # سه اخطارِ حذف نمی‌گیرد.
                                    if expired_warn:
                                        a2.expiry_warn_count = int(a2.expiry_warn_count or 0) + 1
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
        await asyncio.sleep(12 * 3600)


async def vpn_first_connect_loop(interval: int = 90):
    """رصدِ «اولین اتصال» کانفیگ‌های خودکار.

    === PATCH_VPN_FIRST_CONNECT_REUSE_CLIENT_V1 ===
    کلاینتِ هر پنل بینِ چرخه‌ها cache می‌شود تا هر ۹۰ ثانیه لاگینِ تازه
    به پنل نزنیم (که باعثِ اسپم‌شدنِ نوتیفیکیشنِ «ورودِ موفق» در بعضی
    پنل‌ها مثلِ PasarGuard می‌شد). کلاینت فقط در صورتِ خطا یا تغییرِ
    اطلاعاتِ اتصال از نو ساخته می‌شود.
    """
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    await asyncio.sleep(25)
    _panel_skip_until: dict[tuple, float] = {}
    _PANEL_SKIP_SECONDS = 15 * 60
    _panel_clients: dict[tuple, object] = {}  # PATCH_VPN_FIRST_CONNECT_REUSE_CLIENT_V1: کلاینتِ زنده و لاگین‌شده هر پنل
    # === PATCH_FIRST_CONNECT_ADAPTIVE_V1 ===
    # لاگِ واقعی: ۱۷۰۰+ بار «GET /api/users» (کلِ کاربرانِ پنل، تا ۲۷ ثانیه) فقط در دو روز.
    # علت: هر ۹۰ ثانیه برایِ هر پنل که حتی یک کانفیگِ «هنوز وصل‌نشده» دارد کلِ لیستِ کاربران
    # خوانده می‌شد — و کاربری که هیچ‌وقت وصل نشود برایِ همیشه همین بار را روی پنلِ مشتری می‌گذاشت.
    # پنلِ شلوغ/کند هم دکمه‌هایِ کاربر (سرویس‌ها، کانفیگ، خرید) را کند می‌کرد.
    # حالا فاصلهٔ چک بر اساسِ «جوان‌ترین کانفیگِ وصل‌نشده» است: تازه‌ساخته ← سریع، قدیمی ← کم.
    _seen_at: dict[int, float] = {}
    _next_poll: dict[tuple, float] = {}
    while True:
        try:
            async with Session() as s:
                panels = (await s.scalars(select(VpnPanel).where(VpnPanel.enabled == True))).all()
                panel_list = [(p.bot_id, p.base_url, p.username, p.password,
                               p.sub_base or "", p.inbound_ids, p.panel_key, p.api_key) for p in panels]  # PATCH_PANEL_API_KEY_PROPAGATION_V1
            for bot_id, base, user, pw, sub_base, inbound, panel_key, api_key in panel_list:  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                _pk = (bot_id, base)
                # === PATCH_VPN_FIRST_CONNECT_REUSE_CLIENT_V1 ===
                # اگر یوزرنیم/رمز/ساب‌بیس/اینباند/نوعِ پنل عوض شده باشد، کلیدِ
                # کش هم عوض می‌شود تا خودکار کلاینتِ قدیمی دور ریخته شود.
                _ck = (bot_id, base, user, pw, sub_base, tuple(inbound or []), panel_key, api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                _skip = _panel_skip_until.get(_pk)
                if _skip is not None and _skip > asyncio.get_event_loop().time():
                    continue
                async with Session() as s:
                    brow = await s.get(BotRow, bot_id)
                    accts = (await s.scalars(select(PanelAccount).where(
                        PanelAccount.bot_id == bot_id,
                        PanelAccount.first_conn_notified == False))).all()
                    acct_list = [(a.id, a.email, a.user_telegram_id, a.plan_title) for a in accts]
                if not brow or brow.status != "active" or not acct_list:
                    continue
                _now_m = asyncio.get_event_loop().time()
                for _a in acct_list:
                    _seen_at.setdefault(_a[0], _now_m)
                _youngest = min(_now_m - _seen_at[_a[0]] for _a in acct_list)
                if _youngest < 30 * 60:
                    _gap = 90
                elif _youngest < 6 * 3600:
                    _gap = 5 * 60
                elif _youngest < 48 * 3600:
                    _gap = 20 * 60
                else:
                    _gap = 60 * 60
                if _now_m - _next_poll.get(_pk, -1e9) < _gap - 10:
                    continue
                _next_poll[_pk] = _now_m   # زمانِ آخرین چک (نام قدیمی؛ مقدار = «آخرین بار»)
                client = _panel_clients.get(_ck)
                if client is None:
                    client = make_panel_client_from_fields(
                        panel_key, base, user, pw, sub_base=sub_base, inbound_ids_str=inbound,
                        api_key=api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1
                    _panel_clients[_ck] = client
                try:
                    clients = await client.list_clients()
                except Exception:
                    try:
                        await client.close()
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                    _panel_clients.pop(_ck, None)
                    _panel_skip_until[_pk] = asyncio.get_event_loop().time() + _PANEL_SKIP_SECONDS
                    continue
                _panel_skip_until.pop(_pk, None)
                by_email = {c.get("email"): c for c in clients if c.get("email")}

                tbot = Bot(token=brow.token, default=DEFAULTS)
                try:
                    for aid, email, tg_id, title in acct_list:
                        c = by_email.get(email)
                        if not c:
                            continue
                        tr = c.get("traffic") if isinstance(c.get("traffic"), dict) else c
                        up = int(tr.get("up", 0) or 0)
                        down = int(tr.get("down", 0) or 0)
                        if (up + down) <= 0:
                            continue
                        text = (
                            "🎉 <b>اولین اتصال شما با موفقیت انجام شد!</b>\n\n"
                            f"سرویس «{title or 'کانفیگ VPN'}» فعال شد و اتصالت با موفقیت برقرار گردید ✅\n"
                            "از این به بعد از بخش «سرویس‌های من» می‌تونی وضعیت مصرف، "
                            "حجم باقی‌مانده و تاریخ انقضا رو هر لحظه ببینی."
                        )
                        try:
                            await tbot.send_message(tg_id, text)
                        except TelegramUnauthorizedError:
                            _log_unauthorized(tbot)
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


async def card_order_expiry_loop(timeout_min: int = 10, interval_sec: int = 60):
    """سفارش‌های کارت‌به‌کارتی که بیش از timeout_min دقیقه منتظرِ رسید مانده‌اند
    را خودکار لغو می‌کند."""
    from models import Order, Config
    await asyncio.sleep(25)
    while True:
        try:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=timeout_min)
            to_notify = []
            async with Session() as s:
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
                        continue
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


# === PATCH_TONPAYS_PERSISTENT_POLL_V1 ===
async def tonpays_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ TonPays را استعلام و در صورتِ پرداخت تسویه می‌کند.

    پیگیریِ لحظه‌ایِ داخلِ خودِ ربات در حافظه است و با ری‌استارتِ سرویس از بین
    می‌رود؛ این حلقه از رویِ دیتابیس کار می‌کند، پس فاکتورهایِ باز بعد از
    ری‌استارت هم بدونِ دخالتِ کاربر تسویه می‌شوند.
    """
    from tenant.gateways.tonpays import poll_late_once, poll_pending_once
    await asyncio.sleep(45)
    while True:
        try:
            await poll_pending_once()
            await poll_late_once()  # PATCH_TONPAYS_LATE_SWEEP_V1
        except Exception:
            import traceback
            print("tonpays_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_ZARINPAL_PENDING_POLL_V1 ===
async def zarinpal_wallet_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ شارژِ کیفِ پولِ زرین‌پال را استعلام می‌کند و در
    صورتِ پرداختِ موفق (که به هر دلیلی از طریقِ کالبکِ HTTP خودکار پردازش
    نشده — مثلاً به‌خاطرِ روتینگِ نادرستِ دامنه) خودکار تسویه می‌کند."""
    from tenant.gateways.zarinpal_wallet import zarinpal_poll_once
    await asyncio.sleep(50)
    while True:
        try:
            await zarinpal_poll_once()
        except Exception:
            import traceback
            print("zarinpal_wallet_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_ZIBAL_GATEWAY_V1 ===
async def zibal_wallet_poll_loop(interval_sec: int = 60):
    """هر دقیقه فاکتورهایِ بازِ شارژِ کیفِ پولِ زیبال را استعلام می‌کند و در
    صورتِ پرداختِ موفق (که به هر دلیلی از طریقِ کالبکِ HTTP خودکار پردازش
    نشده) خودکار تسویه می‌کند."""
    from tenant.gateways.zibal_wallet import zibal_poll_once
    await asyncio.sleep(55)
    while True:
        try:
            await zibal_poll_once()
        except Exception:
            import traceback
            print("zibal_wallet_poll_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)



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


# === PATCH_SMM_PROVIDER_DETAIL_V1 ===
async def smm_panel_health_loop(interval_sec: int = 600):
    """هر ۱۰ دقیقه، وضعیتِ اتصال/موجودیِ ارائه‌دهندگانی که «بررسیِ خودکار»
    رویشان روشن است (SmmPanel.auto_check_enabled) را زنده می‌گیرد و کش
    می‌کند — همان تصمیمِ ادمین (سند references/smm-menu-redesign.md) که این
    باید یک جابِ زمان‌بندی‌شدهٔ واقعی باشد، نه فقط دکمهٔ دستیِ بروزرسانی."""
    from tenant.bots.virtual_service.smm import smm_panel_health_check_once
    await asyncio.sleep(60)
    while True:
        try:
            await smm_panel_health_check_once()
        except Exception:
            import traceback
            print("smm_panel_health_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_SMM_RATE_PREWARM_V1 ===
# گزارشِ واقعی: مشتری روی «لایک» زد و ~۱۵ ثانیه معطل موند. علتش این بود
# که compute_sell_price (تویِ حالتِ نرخِ خودکار) نرخِ دلار به تومان رو
# زنده از یه زنجیرهٔ fallback (BrsApi → بایننس P2P → navasan) می‌گرفت —
# کشش ۱ ساعته بود، ولی وقتی کش سرد بود (اولین درخواستِ بعدِ انقضا)، اگه
# اولین منبع کند/از دسترس خارج بود، مشتری باید صبر می‌کرد تا زنجیره به
# منبعِ بعدی برسه. الان این حلقه هر ۵۰ دقیقه (قبل از انقضایِ کشِ ۶۰
# دقیقه‌ای) از پشتِ‌صحنه نرخ رو تازه نگه می‌داره، پس هیچ مشتریِ واقعی‌ای
# دیگه منتظرِ این زنجیره نمی‌مونه — کش همیشه گرمه.
async def smm_rate_prewarm_loop(interval_sec: int = 3000):
    from tenant.gateways.crypto_pay import get_usd_to_toman_rate
    await asyncio.sleep(30)
    while True:
        try:
            await get_usd_to_toman_rate()
        except Exception:
            import traceback
            print("smm_rate_prewarm_loop error:\n" + traceback.format_exc())
        await asyncio.sleep(interval_sec)


# === PATCH_PRODUCT_RATING_V1 ===
async def product_rating_loop(interval_sec: int = 3600):
    """هر ساعت، سفارش‌هایِ پرداخت‌شده‌ای رو پیدا می‌کنه که به تعدادِ روزِ
    تنظیم‌شده رسیدن و هنوز ازشون نظرسنجی نشده، و پیامِ امتیازدهی می‌فرسته."""
    from sqlalchemy import and_
    from models import (BotUser, Order, ProductOrder, ProductRating,
                        Product as ShopProduct, Config as ConfigRow,
                        SmmOrder)
    from tenant.shared.product_rating import (delay_days, is_enabled,
                                        send_rating_request)

    await asyncio.sleep(120)
    while True:
        try:
            async with Session() as s:
                bots = (await s.scalars(
                    select(BotRow).where(BotRow.status == "active"))).all()
            for brow in bots:
                try:
                    if not await is_enabled(brow.id):
                        continue
                    days = await delay_days(brow.id)
                    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
                    pending = []
                    async with Session() as s:
                        # ── سفارش‌هایِ کانفیگ/VPN (جدولِ orders) ──
                        seen_cfg = select(ProductRating.order_id).where(
                            ProductRating.bot_id == brow.id,
                            ProductRating.order_kind == "cfg")
                        for o in (await s.scalars(
                            select(Order).where(and_(
                                Order.bot_id == brow.id,
                                Order.status == "paid",
                                Order.created_at <= cutoff,
                                Order.id.notin_(seen_cfg),
                            )).limit(30))).all():
                            buyer = await s.get(BotUser, o.user_id)
                            if not buyer:
                                continue
                            # === امتیاز بر اساسِ دسته‌بندی ذخیره می‌شه ===
                            # کانفیگ‌هایِ انبار مدام مصرف/جایگزین می‌شن، پس
                            # آیدیِ تکشون ناپایداره؛ دستهٔ اون کانفیگ ثابت
                            # می‌مونه و امتیازها روی هم جمع می‌شن.
                            _cat = None
                            if o.reserved_config_id:
                                _c = await s.get(ConfigRow, o.reserved_config_id)
                                _cat = (getattr(_c, "category", None) or "").strip() or None
                            pending.append((o.id, buyer.telegram_id,
                                            o.custom_label or _cat or "سرویس خریداری‌شده"))
                            # ردیف همین‌جا ساخته می‌شه تا حتی اگه ارسال شکست
                            # بخوره، دوباره‌فرستادنِ بی‌پایان اتفاق نیفته
                            s.add(ProductRating(
                                bot_id=brow.id, order_kind="cfg", order_id=o.id,
                                user_id=o.user_id,
                                item_key=(f"cat:{_cat}" if _cat else None),
                                stars=None))
                        # ── سفارش‌هایِ فروشگاه (جدولِ product_orders) ──
                        seen_shop = select(ProductRating.order_id).where(
                            ProductRating.bot_id == brow.id,
                            ProductRating.order_kind == "shop")
                        for po in (await s.scalars(
                            select(ProductOrder).where(and_(
                                ProductOrder.bot_id == brow.id,
                                ProductOrder.status == "paid",
                                ProductOrder.created_at <= cutoff,
                                ProductOrder.id.notin_(seen_shop),
                            )).limit(30))).all():
                            buyer = await s.get(BotUser, po.user_id)
                            if not buyer:
                                continue
                            prod = await s.get(ShopProduct, po.product_id)
                            pending.append((po.id, buyer.telegram_id,
                                            (prod.title if prod else "محصول خریداری‌شده")))
                            s.add(ProductRating(
                                bot_id=brow.id, order_kind="shop", order_id=po.id,
                                user_id=po.user_id,
                                item_key=f"shop:{po.product_id}", stars=None))
                        # ── سفارش‌هایِ خدماتِ مجازی (جدولِ smm_orders) ──
                        seen_smm = select(ProductRating.order_id).where(
                            ProductRating.bot_id == brow.id,
                            ProductRating.order_kind == "smm")
                        for so in (await s.scalars(
                            select(SmmOrder).where(and_(
                                SmmOrder.bot_id == brow.id,
                                SmmOrder.status == "completed",
                                SmmOrder.created_at <= cutoff,
                                SmmOrder.id.notin_(seen_smm),
                            )).limit(30))).all():
                            # SmmOrder با telegram_id کار می‌کنه، ولی
                            # ProductRating.user_id به bot_users.id اشاره داره
                            buyer = await s.scalar(select(BotUser).where(
                                BotUser.bot_id == brow.id,
                                BotUser.telegram_id == so.user_telegram_id))
                            if not buyer:
                                continue
                            pending.append((so.id, so.user_telegram_id,
                                            so.service_name or "سرویسِ خریداری‌شده"))
                            s.add(ProductRating(
                                bot_id=brow.id, order_kind="smm", order_id=so.id,
                                user_id=buyer.id,
                                item_key=(f"smm:{so.smm_service_pk}"
                                          if so.smm_service_pk else None),
                                stars=None))
                        await s.commit()
                    if not pending:
                        continue
                    tbot = Bot(token=brow.token, default=DEFAULTS)
                    try:
                        for oid, tg_id, title in pending:
                            await send_rating_request(tbot, brow.id, oid, tg_id, title, days)
                            await asyncio.sleep(0.4)
                    finally:
                        await tbot.session.close()
                except Exception:
                    logger.exception("product_rating_loop: bot_id=%s", brow.id)
        except Exception:
            logger.exception("product_rating_loop outer")
        await asyncio.sleep(interval_sec)


async def crypto_poll_loop_tenant():
    from tenant.gateways.payment_page import crypto_poll_loop as _cpl
    await _cpl(kind="tenant")


# ───────────────────────────── وبهوک/IPN ─────────────────────────────

# === PATCH_TENANT_ACTIVITY_TRACKING_V1 ===
# باگِ ریشه‌ای: از وقتی main.py به main_platform.py + main_tenant.py تقسیم
# شد، این بخش (ثبتِ last_activity_at) که تویِ main.py هست، هیچ‌وقت به
# main_tenant.py منتقل نشده بود! یعنی موتورِ واقعیِ ربات‌هایِ مستأجر
# (main_tenant.py) هیچ‌وقت last_activity_at رو آپدیت نمی‌کرد — فارغ از
# اینکه ربات چقدر واقعاً شلوغ باشه. نتیجه: inactivity_loop (که تویِ
# main_platform.py اجرا می‌شه) دیر یا زود همه‌ی ربات‌ها رو، حتی کاملاً
# فعال‌ها رو، «غیرفعال» تشخیص می‌داد و اخطارِ حذف/بایگانی می‌فرستاد —
# دقیقاً همون چیزی که گزارش شد.
_last_activity_write: dict[str, float] = {}
_ACTIVITY_WRITE_MIN_INTERVAL = 120  # ثانیه؛ نیازی نیست هر آپدیت نوشته شود


async def _record_tenant_activity(token: str) -> None:
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


async def _process_tenant(bot: Bot, data: dict, token: str):
    _t0 = asyncio.get_event_loop().time()
    try:
        import perfctx as _pf
        _pf_acc = _pf.begin()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (perfctx.begin)")
        _pf = None
        _pf_acc = None
    try:
        upd = Update.model_validate(data, context={"bot": bot})
        await tenant_dp.feed_update(bot, upd)
    except Exception:
        logger.exception(f"tenant webhook error (token …{token[-6:]})")
        await notify_processing_error(bot, data)
    finally:
        _elapsed = asyncio.get_event_loop().time() - _t0
        if _elapsed > 2.0:
            _kind = next((k for k in ("message", "callback_query", "my_chat_member")
                         if k in data), "?")
            # === PATCH_SLOW_UPDATE_DETAIL_V1 ===
            # اندازه‌گیریِ لایهٔ پنل ثابت کرد گلوگاه آن‌جا نیست: در نیم‌ساعتی
            # که ۷ آپدیتِ کند داشتیم، *هیچ* عملیاتِ پنلی از ۲ ثانیه رد نشد.
            # پس این هشدار باید بگوید کدام دکمه/دستور کند بوده، وگرنه فقط
            # می‌دانیم «چیزی کند است» و نه «چه چیزی».
            _what = "?"
            try:
                if "callback_query" in data:
                    _what = (data["callback_query"].get("data") or "?")[:60]
                elif "message" in data:
                    _m = data["message"]
                    if _m.get("text"):
                        _what = _m["text"][:40]
                    elif _m.get("photo"):
                        _what = "<عکس>"
                    elif _m.get("contact"):
                        _what = "<شمارهٔ تماس>"
                    elif _m.get("document"):
                        _what = "<فایل>"
                    else:
                        _what = "<" + ",".join(
                            k for k in _m
                            if k not in ("message_id", "from", "chat", "date")
                        )[:40] + ">"
            except Exception:
                logger.exception("خواندنِ جزئیاتِ آپدیتِ کند ناموفق بود")
            logger.warning(
                f"slow update (token …{token[-6:]}, kind={_kind}, "
                f"{_elapsed:.1f}s) → {_what}"
                + (f" | {_pf.fmt(_pf_acc)}" if _pf else ""))
    # === PATCH_TENANT_ACTIVITY_TRACKING_V1 ===
    await _record_tenant_activity(token)


# === PATCH_SUPPORT_ACCESS_INACTIVE_BOT_V1 ===
async def _is_support_admin(token: str, tg_id: int) -> bool:
    """آیا این کاربر ادمینِ پشتیبانیِ همین ربات است؟

    === PATCH_OWNER_ARCHIVE_BYPASS_FIX_V1 ===
    قبلاً این تابع خودِ مالکِ ربات رو هم بی‌قیدوشرط True برمی‌گردوند، یعنی
    مالک صرف‌نظر از وضعیتِ ربات (حتی بایگانی‌شده به‌خاطرِ عدم‌تمدید/عدمِ
    فعالیت، یا قفل‌شده توسطِ پلتفرم) کاملاً از گیتِ وضعیت رد می‌شد و ربات
    براش عادی کار می‌کرد — انگار اصلاً بایگانی نشده. مشتری‌های دیگه درست
    بلاک می‌شدن ولی خودِ مالک نه؛ نتیجه: مالک متوجهٔ واقعیِ بایگانی‌شدن
    نمی‌شد و پیامِ «برای بازگردانی پرداخت کن» رو هم (که پایین‌تر برای
    owner در حالتِ archived نوشته می‌شه) هرگز نمی‌دید، چون اصلاً به اون
    بخش از کد نمی‌رسید.
    حالا این تابع فقط ادمینِ پشتیبانیِ واقعاً ثبت‌شده در AdminSupport رو
    چک می‌کنه؛ مالک از همون مسیرِ عادیِ زیر (که برایِ هر status پیامِ
    مناسب می‌فرسته) رد می‌شه.
    """
    from models import AdminSupport
    try:
        async with Session() as s:
            row = await s.scalar(select(BotRow).where(BotRow.token == token))
            if not row:
                return False
            adm = await s.scalar(select(AdminSupport).where(
                AdminSupport.bot_id == row.id,
                AdminSupport.telegram_id == tg_id))
        return adm is not None
    except Exception:
        logger.exception("support-admin check failed")
        return False


@app.post("/webhook/{token}")
async def tenant_webhook(token: str, request: Request):
    bot, status = await _get_tenant_bot(token)
    if not bot:
        return Response()
    try:
        data = await request.json()
    except Exception:
        import traceback
        print(f"tenant webhook parse error (token …{token[-6:]}):\n" + traceback.format_exc())
        return Response()
    # === PATCH_RAW_UPDATE_DEBUG_V1 ===
    # لاگِ بی‌قیدوشرط — قبل از هر فیلتر/روتری. اگه این خط برایِ یه پیام
    # نیومد، یعنی خودِ تلگرام اصلاً این آپدیت رو به این وبهوک نفرستاده
    # (نه اینکه کدِ ما نادیده گرفتتش).
    try:
        _msg_dbg = data.get("message")
        if _msg_dbg:
            logger.info(
                f"[RAW_UPDATE] token=…{token[-6:]} chat_id={_msg_dbg.get('chat', {}).get('id')} "
                f"chat_type={_msg_dbg.get('chat', {}).get('type')} "
                f"text={_msg_dbg.get('text')!r} from={_msg_dbg.get('from', {}).get('id')}")
    except Exception:
        logger.exception("[RAW_UPDATE] خطا در لاگِ خام")
    if status != "active":
        maint_text = None
        maint_kb = None
        sender_id = (((data.get("message") or data.get("edited_message") or {})
                      .get("from") or {}).get("id")) or \
                    (((data.get("callback_query") or {}).get("from") or {}).get("id"))
        # === PATCH_SUPPORT_ACCESS_INACTIVE_BOT_V1 ===
        # ادمینِ پشتیبانیِ ثبت‌شده در جدولِ AdminSupport (همان چیزی که دکمهٔ
        # «فعال‌کردنِ دسترسیِ پشتیبانی» در پنلِ پلتفرم می‌سازد) باید بتواند
        # رباتِ غیرفعال/قفل/بایگانی‌شده را هم باز کند — وگرنه دقیقاً همان
        # رباتی که برای رفعِ مشکلش دسترسی گرفته‌ایم، جوابِ ما را نمی‌دهد.
        if sender_id and await _is_support_admin(token, int(sender_id)):
            spawn(_process_tenant(bot, data, token))
            return Response()
        # === PATCH_OWNER_SELF_PAUSE_UNLOCK_V1 ===
        # وقتی مالک خودش ربات را از منوی ربات خاموش می‌کند (status="paused")،
        # قبلاً همهٔ آپدیت‌ها (از جمله پیام‌های خودِ مالک) بلاک می‌شد و پیامِ
        # عمومیِ «موقتاً غیرفعال است» نشان داده می‌شد؛ در نتیجه مالک هرگز به
        # دکمهٔ «روشن‌کردن» در منوی ربات نمی‌رسید و ربات برایِ همیشه خاموش
        # می‌ماند.
        #
        # نکته‌ی مهم: status="paused" فقط توسطِ خودِ مالک ست نمی‌شود؛ پازِ
        # خودکارِ سیستم (تخلفِ مکررِ بیو، در main.py/main_platform.py) هم از
        # همین مقدار status="paused" استفاده می‌کند. اگر فقط status را چک
        # می‌کردیم، مالک می‌توانست پازِ خودکارِ سیستم را هم (بدونِ رفعِ واقعیِ
        # تخلف) با همین ترفند دور بزند. برای همین علاوه بر status، باید
        # lock_reason را هم چک کنیم: فقط وقتی متنِ آن نشان‌دهندهٔ
        # خاموش‌کردنِ دستیِ خودِ مالک باشد (نه پازِ خودکارِ سیستم) اجازهٔ
        # عبور می‌دهیم. آرشیو هم مستثنی است (archived_at باید خالی باشد).
        if status == "paused" and sender_id:
            async with Session() as s:
                brow = await s.scalar(select(BotRow).where(BotRow.token == token))
            # === PATCH_SELF_PAUSE_REASONS_V1 ===
            # قبلاً فقط پیشوندِ «توسط مالکِ ربات» شناخته می‌شد. «حالت
            # آپدیت» در پنلِ همکار (tenant/partner_menu.py) متنِ دیگری
            # می‌نویسد، پس مالک بعد از خاموش‌کردن، خودش هم پشتِ پیامِ
            # «موقتاً غیرفعال است» گیر می‌افتاد و هیچ راهی برایِ روشن‌کردن
            # نداشت. هر پیشوندی که اینجا اضافه می‌شود باید *فقط* توسطِ
            # خودِ مالک نوشته شده باشد، نه پازِ خودکارِ سیستم.
            _SELF_PAUSE_PREFIXES = (
                "توسط مالکِ ربات",
                "حالت آپدیت (توسط مالک ربات)",
            )
            is_self_pause = bool(
                brow and brow.lock_reason
                and brow.lock_reason.startswith(_SELF_PAUSE_PREFIXES)
            )
            if (brow and brow.archived_at is None and is_self_pause
                    and int(sender_id) == brow.owner_id):
                spawn(_process_tenant(bot, data, token))
                return Response()
        if status == "locked":
            async with Session() as s:
                brow = await s.scalar(select(BotRow).where(BotRow.token == token))
            reason = (brow.lock_reason if brow else "").strip() if brow and brow.lock_reason else ""
            maint_text = (
                f"🔒 این ربات توسط مدیریت قفل شده است.\n\nدلیل: {reason}" if reason else
                "🔒 این ربات توسط مدیریت قفل شده است.\n\n"
                "برای رفعِ قفل با پشتیبانیِ بات‌ساز در تماس باش."
            )
        elif status == "admin_paused":
            maint_text = (
                "🔴 این ربات توسط مدیریت بات‌ساز غیرفعال شده است.\n\n"
                "برای اطلاعِ بیشتر یا رفعِ غیرفعال‌سازی با پشتیبانیِ بات‌ساز در تماس باش."
            )
        else:
            async with Session() as s:
                brow = await s.scalar(select(BotRow).where(BotRow.token == token))
            if brow and brow.archived_at and brow.archive_reason == "user_deleted":
                maint_text = "🗑 این ربات توسط مالکش حذف شده و دیگر در دسترس نیست."
            elif brow and brow.archived_at and sender_id == brow.owner_id:
# === PATCH_ARCHIVED_START_DIRECT_RENEW_V1 ===
                # قبلاً به صفحهٔ جزئیاتِ ربات (mb_) می‌رفت؛ الان مستقیم
                # به صفحهٔ تمدید/پرداخت (mbup_) می‌ره.
                link = _platform_deep_link(f"mbup_{brow.id}")
                if link:
                    maint_kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔓 رفتن به فعال‌سازیِ ربات", url=link)
                    ]])
                if brow.archive_reason == "inactivity":
                    maint_text = (
                        "🗄 ربات شما به دلیل <b>عدم فعالیت</b> به «بایگانی» "
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
            # === PATCH_SELF_PAUSE_REASONS_V1 ===
            # اگر مالک خودش ربات را خاموش کرده ولی به‌دلیلی از درِ عبورِ
            # بالا رد نشده (مثلاً از حسابِ دیگری پیام می‌دهد)، دستِ‌کم
            # پیامِ گنگِ «موقتاً غیرفعال» نگیرد و بداند چه شده.
            elif (brow and brow.status == "paused" and brow.lock_reason
                  and brow.lock_reason.startswith((
                      "توسط مالکِ ربات", "حالت آپدیت (توسط مالک ربات)"))):
                maint_text = (
                    "🛠 این ربات توسط مالک در حالت آپدیت قرار گرفته؛ لطفاً بعداً دوباره تلاش کنید."
                    "\n🙏🏻 از صبر شما سپاسگزاریم."
                )
            else:
                maint_text = "🗄 این ربات موقتاً غیرفعال است. لطفاً بعداً دوباره امتحان کنید."
        spawn(maintenance_reply(bot, data, text=maint_text, reply_markup=maint_kb))
        return Response()
    spawn(_process_tenant(bot, data, token))
    return Response()



# === PATCH_APAY_GATEWAY_V1 ===
@app.post("/apay/webhook/{bot_id}")
async def apay_webhook(bot_id: int, request: Request):
    """وب‌هوکِ درگاهِ آپی؛ امضا (اگر کلید ثبت شده) چک می‌شود و تسویه فقط با verify ِ خودِ آپی."""
    from tenant.gateways.apay import handle_webhook
    raw = await request.body()
    try:
        code = await handle_webhook(bot_id, raw, request.headers.get("X-APay-Timestamp", ""),
                                    request.headers.get("X-APay-Signature", ""))
    except Exception:
        logger.exception("apay webhook error (bot=%s)", bot_id)
        code = 500
    return Response(status_code=code)


@app.post("/hesabpay/webhook")
async def hesabpay_webhook(request: Request):
    """دریافت و تأیید webhook حساب‌پی؛ شارژ فقط بعد از verify و به‌صورت idempotent."""
    from sqlalchemy import select as _select
    from models import HesabPayCharge
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    reference = str((data or {}).get("user_id") or "").strip()
    signature = str((data or {}).get("signature") or "").strip()
    timestamp = str((data or {}).get("timestamp") or "").strip()
    if not reference or not signature or not timestamp:
        return Response(status_code=400)
    try:
        async with Session() as s:
            charge = await s.scalar(_select(HesabPayCharge).where(HesabPayCharge.reference == reference))
        if not charge:
            return Response(status_code=404)
        from tenant.gateways.hesabpay import verify_signature, settle_webhook
        if not await verify_signature(charge.bot_id, signature, timestamp):
            logger.warning("hesabpay: webhook signature rejected for %s", reference)
            return Response(status_code=401)
        status, _note, new_balance = await settle_webhook(reference, data)
        if status == "credited":
            async with Session() as s:
                brow = await s.get(BotRow, charge.bot_id)
            if brow:
                tbot = Bot(token=brow.token, default=DEFAULTS)
                try:
                    from tenant.gateways.wallet_receipt import build_wallet_charge_message
                    text, kb = build_wallet_charge_message(int(charge.amount_toman), new_balance, "hesabpay", ref_id=str(data.get("transaction_id") or ""))
                    await tbot.send_message(charge.user_telegram_id, text, reply_markup=kb)
                finally:
                    await tbot.session.close()
        if status in ("credited", "already", "ignored"):
            return Response(status_code=200)
        return Response(status_code=400)
    except Exception:
        logger.exception("hesabpay webhook error")
        return Response(status_code=500)


@app.get("/hesabpay/success")
async def hesabpay_success():
    return Response("<html><body dir='rtl'><h2>پرداخت ثبت شد</h2><p>نتیجهٔ نهایی پس از تأیید امن درگاه به ربات ارسال می‌شود.</p></body></html>", media_type="text/html")


@app.get("/hesabpay/failure")
async def hesabpay_failure():
    return Response("<html><body dir='rtl'><h2>پرداخت تکمیل نشد</h2><p>می‌توانید به ربات برگردید و دوباره تلاش کنید.</p></body></html>", media_type="text/html")

@app.post("/tonpays/callback")
async def tonpays_callback(request: Request):
    """کال‌بکِ درگاه TonPays.

    === PATCH_TONPAYS_V1 ===
    ⚠️ کال‌بکِ جدیدِ TonPays هدرهایِ X-TonPays-Signature / -Event / -Delivery-Id
    دارد ولی الگوریتمِ امضا در مستندات نیامده، پس راستی‌آزمایی‌اش ممکن نیست.
    بدنهٔ این درخواست *ذاتاً غیرقابلِ اعتماد* است: هر کسی می‌تواند یک POST با
    «paid: true» بفرستد.

    === PATCH_TONPAYS_DOCS_V2 ===
    هدرِ X-API-Key در وب‌هوک همان کلیدِ فروشگاه است. اگر با کلیدِ ذخیره‌شدهٔ
    همان ربات نخواند، درخواست بدونِ هیچ استعلامی رد می‌شود — این جلوی این را
    می‌گیرد که کال‌بکِ جعلیِ انبوه، سقفِ ۶۰ استعلام/دقیقهٔ کلید را بسوزاند.
    فاکتور هم علاوه بر order_id با invoice_id پیدا می‌شود (بدنهٔ جدید هر دو را دارد).

    برایِ همین اینجا هیچ چیزی از بدنه به‌عنوانِ حقیقت پذیرفته نمی‌شود — فقط
    order_id خوانده می‌شود تا بدانیم کدام فاکتور را باید *مستقیماً از خودِ
    TonPays* دوباره استعلام کنیم. کلِ منطقِ تایید و شارژ در settle_charge است
    که با /invoices/check از درگاه تایید می‌گیرد و مبلغ را با دیتابیسِ خودمان
    مقایسه می‌کند. یعنی کال‌بکِ جعلی حداکثر باعثِ یک استعلامِ اضافه می‌شود،
    نه یک ریال شارژِ ناحق.
    """
    from models import TonPaysCharge
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
                ch = await s.scalar(_select(TonPaysCharge).where(
                    TonPaysCharge.order_id == order_id))
            else:
                ch = await s.scalar(_select(TonPaysCharge).where(
                    TonPaysCharge.invoice_id == invoice_id_cb))
        if not ch or ch.status == "paid":
            return Response()
        order_id = ch.order_id
        bot_id, tg_id = ch.bot_id, ch.user_telegram_id

        # === PATCH_TONPAYS_DOCS_V2 === کلیدِ هدر باید کلیدِ همین رباتِ ما باشد
        import hmac as _hmac
        # === PATCH_TONPAYS_CUSTOM_V1 === فاکتورِ کاستوم (پیشوندِ tc) با کلیدِ کاستوم
        from tenant.gateways.tonpays import get_key as _tp_key, is_custom_order as _tp_is_custom
        _stored = await _tp_key(bot_id, _tp_is_custom(order_id))
        _hdr = request.headers.get("x-api-key", "")
        if not _stored or not _hmac.compare_digest(_hdr.encode(), _stored.encode()):
            logger.warning("tonpays callback: X-API-Key نمی‌خواند (order=%s) — رد شد", order_id)
            return Response(status_code=401)

        from tenant.gateways.tonpays import settle_charge
        ok, _note, credit = await settle_charge(bot_id, order_id)
        if ok and credit > 0:
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if brow:
                tbot = Bot(token=brow.token, default=DEFAULTS)
                try:
                    async with Session() as s:
                        from models import BotUser as _BU
                        u = await s.scalar(_select(_BU).where(
                            _BU.bot_id == bot_id, _BU.telegram_id == tg_id))
                        new_balance = float(u.balance) if u else credit
                    from tenant.gateways.wallet_receipt import build_wallet_charge_message
                    _msg, _kb = build_wallet_charge_message(credit, new_balance, "card")
                    await tbot.send_message(tg_id, _msg, reply_markup=_kb)
                except Exception:
                    logger.exception("tonpays: notify failed")
                finally:
                    await tbot.session.close()
    except Exception:
        import traceback
        print("tonpays callback error:\n" + traceback.format_exc())
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
                return Response()
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


# === PATCH_ZARINPAL_FEE_V1 ===
# receipt_text یا فقط «zarinpal_authority=X» است، یا «...|fee=N» وقتی
# کارمزد با پرداخت‌کننده باشد. مبلغی که به درگاه رفته amount+fee است و
# verify باید *دقیقاً همان* را بفرستد، وگرنه زرین‌پال تأیید نمی‌کند.
def _zp_parse_receipt(txt: str) -> tuple[str, int]:
    raw = (txt or "").strip()
    fee = 0
    if "|fee=" in raw:
        raw, _, fee_s = raw.partition("|fee=")
        try:
            fee = max(0, int(fee_s.strip()))
        except ValueError:
            fee = 0
    return raw.replace("zarinpal_authority=", "", 1).strip(), fee


# === PATCH_GATEWAY_REDIRECT_LINK_V1 ===
# صفحهٔ ریدایرکتِ «خودکار» (tenant/gateway_redirect.py). کاربر از تلگرام به
# https://<دامنهٔ callbackِ مالک>/gw/<gw>/start/<ref> می‌آید و مرورگرش از این
# صفحه به درگاه می‌رود، پس درگاه ارجاع‌دهندهٔ همان دامنه را می‌بیند. به هاست
# وابسته نیست؛ هر دامنه‌ای که به این سرور برسد کار می‌کند.
@app.get("/gw/{gw}/start/{ref}")
async def gateway_redirect_page(gw: str, ref: str):
    from tenant.gateways.gateway_redirect import GATEWAYS, REF_RE, direct_startpay
    if gw not in GATEWAYS or not REF_RE.match(ref or ""):
        return Response(content="<meta charset='utf-8'><h2>لینکِ پرداخت نامعتبر است.</h2>",
                        media_type="text/html; charset=utf-8", status_code=404)
    url = direct_startpay(gw, ref)  # فقط الگوی ثابتِ خودِ درگاه + ref اعتبارسنجی‌شده
    label = GATEWAYS[gw]["label"]
    body = (
        "<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='origin'>"
        f"<title>انتقال به درگاه {label}</title>"
        "<style>body{font-family:Tahoma,sans-serif;background:#f3f4f6;margin:0;display:flex;"
        "min-height:100vh;align-items:center;justify-content:center}"
        ".b{background:#fff;border-radius:16px;padding:28px;max-width:380px;width:90%;"
        "text-align:center;box-shadow:0 4px 20px rgba(0,0,0,.08)}"
        "a{display:block;margin-top:18px;padding:14px;border-radius:12px;background:#0a7c3e;"
        "color:#fff;text-decoration:none;font-weight:bold}</style></head><body>"
        f"<div class='b'><h2>🔐 پرداخت امن {label}</h2><p>در حال انتقال به درگاه…</p>"
        f"<a id='go' href='{url}'>ورود به درگاه پرداخت</a></div>"
        f"<script>setTimeout(function(){{location.href={url!r};}},700);</script>"
        "</body></html>")
    return Response(content=body, media_type="text/html; charset=utf-8",
                    headers={"Referrer-Policy": "origin", "Cache-Control": "no-store"})


@app.get("/zarinpal/tenant/pay/{order_id}")
async def zarinpal_tenant_invoice(order_id: int):
    """صفحهٔ میانی فاکتور؛ برای حفظ referrer دامنهٔ پذیرنده قبل از StartPay."""
    from html import escape
    from models import Order
    from sqlalchemy import select as _select
    async with Session() as s:
        order = await s.scalar(_select(Order).where(
            Order.id == order_id, Order.pay_method == "zarinpal_wallet"))
    if not order or order.status != "pending" or not order.receipt_text:
        return Response(content="<meta charset='utf-8'><h2>فاکتور معتبر نیست یا قبلاً پردازش شده است.</h2>", media_type="text/html; charset=utf-8", status_code=404)
    authority, _fee = _zp_parse_receipt(order.receipt_text)
    if not authority:
        return Response(content="<meta charset='utf-8'><h2>اطلاعات فاکتور ناقص است.</h2>", media_type="text/html; charset=utf-8", status_code=400)
    amount = int(order.amount or 0) + _fee
    import platform_zarinpal
    pay_url = platform_zarinpal.startpay_url(authority)
    body = (
        "<html><meta charset='utf-8'><title>فاکتور پرداخت</title>"
        "<body dir='rtl' style='font-family:sans-serif;max-width:560px;margin:40px auto;padding:24px;text-align:center'>"
        f"<h2>🔐 فاکتور پرداخت زرین‌پال</h2>"
        + (f"<p>مبلغ شارژ: <b>{int(order.amount or 0):,} تومان</b><br>"
           f"کارمزد درگاه: <b>{_fee:,} تومان</b></p>" if _fee else "")
        + f"<p>مبلغ قابل پرداخت: <b>{amount:,} تومان</b></p>"
        "<p>پس از بررسی مبلغ، روی دکمهٔ پرداخت بزنید.</p>"
        f"<a href='{escape(pay_url, quote=True)}' style='display:inline-block;padding:14px 24px;background:#168de2;color:#fff;text-decoration:none;border-radius:8px'>ادامه به درگاه زرین‌پال</a>"
        "</body></html>"
    )
    return Response(content=body, media_type="text/html; charset=utf-8")

@app.get("/zarinpal/tenant/callback")
async def zarinpal_tenant_callback(request: Request):
    """بازگشت زرین‌پال برای شارژ کیف پول کاربران ربات‌های مستأجر.
    این مسیر عمداً از Callback پلتفرم و IPN نواپیمنتس جداست.
    """
    from models import BotUser, Order, Setting, WalletTxn
    from sqlalchemy import select as _select, update as _update
    import platform_zarinpal
    authority = (request.query_params.get("Authority") or "").strip()
    status = (request.query_params.get("Status") or "").upper()
    def page(title: str, body: str) -> Response:
        return Response(content=f"<html><meta charset='utf-8'><title>{title}</title><body dir='rtl' style='font-family:sans-serif;text-align:center;padding:40px'><h2>{title}</h2><p>{body}</p></body></html>", media_type="text/html; charset=utf-8")
    if not authority:
        return page("پرداخت نامعتبر", "شناسه تراکنش پیدا نشد.")
    if status != "OK":
        return page("پرداخت لغو شد", "پرداخت توسط کاربر لغو یا ناموفق شد.")
    notify = None
    async with Session() as s:
        order = await s.scalar(_select(Order).where(
            Order.pay_method == "zarinpal_wallet",
            Order.receipt_text.contains(f"zarinpal_authority={authority}"),
        ).with_for_update())
        if not order:
            return page("تراکنش نامعتبر", "سفارش مربوط به این پرداخت پیدا نشد.")
        if order.status == "paid":
            return page("پرداخت قبلاً ثبت شده", "این پرداخت قبلاً به کیف پول اضافه شده است.")
        merchant_rec = await s.scalar(_select(Setting).where(
            Setting.bot_id == order.bot_id, Setting.key == "zarinpal_merchant_id"))
        merchant = merchant_rec.value if merchant_rec else ""
        amount = int(order.amount or 0)
        # === PATCH_ZARINPAL_FEE_V1 ===
        _, _fee = _zp_parse_receipt(order.receipt_text)
        gateway_amount = amount + _fee
        if not merchant or amount <= 0:
            return page("تنظیمات ناقص", "درگاه این ربات به‌درستی تنظیم نشده است.")
        ok, ref_id = await platform_zarinpal.verify_payment(
            merchant, gateway_amount, authority)
        if not ok:
            return page("پرداخت تأیید نشد", str(ref_id))
        claim = await s.execute(_update(Order).where(
            Order.id == order.id, Order.status != "paid").values(status="paid"))
        if claim.rowcount == 0:
            await s.rollback()
            return page("پرداخت قبلاً ثبت شده", "این پرداخت قبلاً پردازش شده است.")
        user = await s.scalar(_select(BotUser).where(BotUser.id == order.user_id).with_for_update())
        if not user:
            await s.rollback()
            return page("کاربر پیدا نشد", "حساب کاربری این پرداخت موجود نیست.")
        user.balance = float(user.balance or 0) + amount
        s.add(WalletTxn(bot_id=order.bot_id, user_id=user.id, amount=amount,
                        kind="zarinpal_charge", note=f"زرین‌پال؛ ref_id={ref_id}"))
        notify = (order.bot_id, user.telegram_id, amount, float(user.balance))
        _inv_order_id = order.id  # PATCH_INVOICE_MSG_EDIT_V1
        await s.commit()
    if notify:
        bot_id, telegram_id, credited, new_balance = notify
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if brow:
            tbot = Bot(token=brow.token, default=DEFAULTS)
            # === PATCH_INVOICE_MSG_EDIT_V1 === پیامِ فاکتور → «تأیید شد و حسابت شارژ شد»
            try:
                from tenant.gateways import invoice_msg
                await invoice_msg.finalize(tbot, bot_id, "zp", _inv_order_id, credited, ref_id)
            except Exception:
                logger.exception("tenant ZarinPal: ادیتِ پیامِ فاکتور ناموفق بود")
            try:
                from tenant.gateways.wallet_receipt import build_wallet_charge_message
                text, kb = build_wallet_charge_message(credited, new_balance, "zarinpal")
                await tbot.send_message(telegram_id, text, reply_markup=kb)
            except Exception:
                logger.exception("tenant ZarinPal notification failed")
            finally:
                await tbot.session.close()
    return page("پرداخت موفق", "مبلغ با موفقیت به کیف پول شما اضافه شد. اکنون می‌توانید به ربات برگردید.")


# === PATCH_ZIBAL_GATEWAY_V1 ===
@app.get("/zibal/tenant/callback")
async def zibal_tenant_callback(request: Request):
    """بازگشتِ زیبال برایِ شارژِ کیفِ پولِ کاربرانِ ربات‌هایِ مستأجر.

    برخلافِ کالبکِ زرین‌پالِ بالا که منطقِ تسویه رو این‌جا تکرار کرده،
    این یکی مستقیم _settle_zibal_order رو صدا می‌زنه (همون تابعی که
    پولِ پس‌زمینه و دکمهٔ «بررسیِ دستی» هم استفاده می‌کنن) — یعنی قفلِ
    کارتِ KYC سطح۱ این‌جا هم اعمال می‌شه، بدونِ نیاز به منطقِ تکراری.
    """
    from tenant.gateways.zibal_wallet import _settle_zibal_order, _notify_credited
    track_id = (request.query_params.get("trackId") or "").strip()
    success = (request.query_params.get("success") or "")
    def page(title: str, body: str) -> Response:
        return Response(content=f"<html><meta charset='utf-8'><title>{title}</title><body dir='rtl' style='font-family:sans-serif;text-align:center;padding:40px'><h2>{title}</h2><p>{body}</p></body></html>", media_type="text/html; charset=utf-8")
    if not track_id:
        return page("پرداخت نامعتبر", "شناسه تراکنش پیدا نشد.")
    if success not in ("1", "true", "True"):
        return page("پرداخت لغو شد", "پرداخت توسط کاربر لغو یا ناموفق شد.")
    from models import Order
    from sqlalchemy import select as _select
    async with Session() as s:
        order = await s.scalar(_select(Order).where(
            Order.pay_method == "zibal_wallet",
            Order.receipt_text.contains(f"zibal_track={track_id}")))
    if not order:
        return page("تراکنش نامعتبر", "سفارش مربوط به این پرداخت پیدا نشد.")
    status, msg, amount, bot_id, telegram_id, new_balance, ref_id = await _settle_zibal_order(order.id)
    if status == "credited" and bot_id and telegram_id:
        try:
            await _notify_credited(bot_id, telegram_id, amount, new_balance, ref_id, order_id=order.id)
        except Exception:
            logger.exception("tenant Zibal notification failed")
        return page("پرداخت موفق", "مبلغ با موفقیت به کیف پول شما اضافه شد. اکنون می‌توانید به ربات برگردید.")
    if status == "already":
        return page("پرداخت قبلاً ثبت شده", "این پرداخت قبلاً به کیف پول اضافه شده است.")
    if status == "not_paid":
        return page("پرداخت تأیید نشد", str(msg))
    return page("خطا", str(msg))


async def health():
    return {"ok": True}


# === PATCH_CUSTOM_DOMAIN_V1 ===
# === PATCH_SERVER_STATUS_V1 === همان مینی‌اپِ «وضعیتِ سرورها» روی اپِ تننت هم هست: nginx مسیرِ پیش‌فرض (location /) را به
# این پورت می‌فرستد، پس بدونِ هیچ تغییری در nginx هم آدرسِ https://<دامنه>/status کار می‌کند. هر دو از یک دیتابیس می‌خوانند.
@app.get("/status")
async def server_status_page_t():
    from fastapi.responses import HTMLResponse
    from server_status_page import PAGE
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/status/api")
async def server_status_api_t():
    from fastapi.responses import JSONResponse
    from server_status import build_report
    try:
        return JSONResponse(await build_report(), headers={"Cache-Control": "no-store"})
    except Exception:
        logger.exception("server_status: ساختِ گزارش ناموفق بود")
        return JSONResponse({"error": "unavailable"}, status_code=503)


@app.get("/domain-ask")
async def domain_ask(domain: str = ""):
    """اندپوینتِ «ask» برایِ on-demand TLSِ Caddy — قبل از اینکه Caddy برایِ
    یک دامنه از Let's Encrypt گواهی بگیرد، همین آدرس را صدا می‌زند
    (`GET /domain-ask?domain=xxx`). فقط برایِ دامنه‌هایی 200 برمی‌گردانیم که
    توسطِ یک مالکِ رباتِ واقعی وصل و از رویِ رکوردِ TXT تایید شده باشند —
    وگرنه هرکسی می‌توانست با فرستادنِ دامنه‌هایِ دلخواه، سرور را مجبور به
    درخواستِ گواهیِ بی‌نهایت از Let's Encrypt کند (که ریت‌لیمیت می‌شود).
    ببین `tenant/custom_domain.py` و `deploy/Caddyfile`."""
    if not domain:
        return Response(status_code=400)
    from models import BotCustomDomain
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(
            BotCustomDomain.domain == domain.lower(), BotCustomDomain.status == "verified"))
    if d:
        return Response(status_code=200)
    return Response(status_code=404)


@app.get("/swap-pay")
async def swap_pay_redirect(url: str = ""):
    """صفحه redirect برای باز کردن express-withdraw سواپ‌ولت از داخل مینی‌اپ تلگرام."""
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
  <script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
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


# ───────────────────────────── استارتاپ ─────────────────────────────

# === PATCH_BG_LOOPS_MOVED_TO_WORKER_V1 ===
# قبلاً ۱۲ حلقه‌ی پس‌زمینه (vpn_expiry_loop و بقیه) همین‌جا، تو پروسه‌ی
# HTTP، اسپان می‌شدن. مشکل: این حلقه‌ها با پنل‌های VPN بیرونی (که بعضاً کند
# جواب می‌دن یا IPv6ِ غیرقابل‌دسترس دارن) کار می‌کنن، و چون همه رو یه
# event loop اجرا می‌شدن، یه پنلِ کند می‌تونست پردازشِ webhookِ کاربرها رو
# هم عقب بندازه (دیدیمش: «slow update ...15s» + تایم‌اوتِ Redis).
#
# راه‌حل: این حلقه‌ها دیگه اینجا اسپان نمی‌شن. یه پروسه‌ی کاملاً مجزا
# (worker_tenant.py, سرویسِ systemd جداگانه‌ی botsaz-tenant-worker) اجراشون
# می‌کنه. اونجوری هرچقدر هم یه پنلِ VPN کند/خراب باشه، تاثیری روی
# پاسخ‌دهیِ webhookهای همین پروسه نداره — دو تا event loopِ کاملاً جدا،
# دو تا پروسه‌ی کاملاً جدا.
#
# (advisory lock دیگه لازم نیست چون این پروسه دیگه هیچ حلقه‌ای اسپان
# نمی‌کنه؛ حتی اگه چند نسخه از این پروسه هم بالا بیاد، هر کدوم فقط
# HTTP سرو می‌کنه، تکراری اسپان نمی‌شه.)


async def on_startup():
    try:
        import loopwatch
        loopwatch.start("tenant")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (loopwatch)")
    # === PATCH_ASYNCIO_EXECUTOR_SIZE_V1 ===
    # aiohttp (کلاینتِ همه‌ی پنل‌های VPN) هر TCPConnectorِ جدید رو بدون
    # resolverِ async می‌سازه، یعنی DNS lookup از طریقِ ThreadPoolExecutorِ
    # پیش‌فرضِ asyncio انجام می‌شه. برایِ ایمنیِ بیشتر (حتی با اینکه دیگه
    # حلقه‌های پنل اینجا اجرا نمی‌شن) سایزِ executor رو بزرگ نگه می‌داریم.
    try:
        asyncio.get_running_loop().set_default_executor(
            _TPE(max_workers=64, thread_name_prefix="botsaz-io"))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (تنظیمِ executor بزرگ‌تر)")
    await init_db()
    try:
        from tenant.bots.virtual_service.smm import ensure_smm_columns
        await ensure_smm_columns()
    except Exception as e:
        print("ensure_smm_columns error:", e)
    # === PATCH_SPLIT_PLATFORM_USERNAME_V1 ===
    # قبلاً یوزرنیمِ بات‌ساز از خودِ platform_bot.get_me() (که فقط سمتِ
    # پلتفرم موجود بود) کش می‌شد. این پروسه دیگه platform_bot رو import
    # نمی‌کنه، پس با یه Bot موقتِ سبک (فقط برای همین یه get_me) خودش
    # مستقیماً از توکن (اگه در این پروسه هم به‌عنوان env در دسترس باشه)
    # می‌خونتش. اگه توکن اینجا تنظیم نشده، فقط دکمهٔ دیپ‌لینکِ «بازگشت به
    # بات‌ساز» (در پیامِ بایگانی) نمایش داده نمی‌شود؛ هیچ فلوی دیگری اثر
    # نمی‌بیند.
    if PLATFORM_BOT_TOKEN:
        global _platform_bot_username
        try:
            async with _temp_bot(PLATFORM_BOT_TOKEN) as tb:
                me = await tb.get_me()
                _platform_bot_username = me.username
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (resolve _platform_bot_username)")
    # شنوندهٔ NOTIFYِ بین‌پروسه‌ای: وقتی main_platform.py یه بات رو حذف/قفل/
    # بایگانی می‌کنه، همین‌جا فوری کشِ وضعیت + Bot instanceِ کش‌شده پاک می‌شه.
    try:
        spawn(tenant_status_bus.run_tenant_invalidate_listener(_clear_local_tenant_cache))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (راه‌اندازیِ tenant_status_bus listener)")


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await on_startup()
    yield


app.router.lifespan_context = _lifespan
