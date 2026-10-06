"""
bot_utils.py — کمک‌کننده‌های عمومیِ Bot که هیچ وابستگیِ پلتفرم/مستأجرِ
خاصی ندارند (بی‌طرف، برای استفادهٔ هر دو سرویس).

=== PATCH_SPLIT_TEMP_BOT_V1 ===
کلاسِ _temp_bot قبلاً داخلِ platform_bot.py بود. وقتی main.py می‌خواست از
آن استفاده کند (مثلاً برای حلقه‌های پس‌زمینه‌ای مثلِ card_order_expiry_loop
که مالِ سمتِ مستأجرهاست)، مجبور بود کلِ platform_bot.py (۱۳هزار+ خط، شاملِ
تمامِ منطقِ ادمینیِ بات‌ساز) را import کند. این یعنی اگر قرار باشد بات‌ساز
و بات‌های مستأجر در دو پروسهٔ جدا اجرا شوند (main_platform.py /
main_tenant.py)، پروسهٔ مستأجر همچنان به‌خاطرِ همین یک کلاسِ کوچک، کلِ
platform_bot.py را import می‌کرد — یعنی اگر platform_bot.py وقتِ importشدن
(نه اجراشدن) خطا می‌داد، پروسهٔ مستأجر هم بالا نمی‌آمد؛ دقیقاً همان
وابستگیِ ناخواسته‌ای که قرار بود با اسپلیت از بین برود.

با انتقالِ این کلاس به این فایلِ بی‌طرف و مستقل، platform_bot.py هم مثلِ
قبل `from bot_utils import _temp_bot` می‌کند (بدونِ تغییرِ رفتار)، ولی
main_tenant.py دیگر لازم نیست platform_bot.py را import کند.
"""
from __future__ import annotations

from aiogram import Bot

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


class _temp_bot:
    """Context manager برای ساختِ یک Bot موقتِ کوتاه‌عمر (مثلاً برای تست توکن،
    ارسالِ یک پیام، یا delete/set_webhook) که همیشه session داخلی‌اش را می‌بندد
    — حتی اگر فراخوانیِ API وسط کار Exception بدهد. جایگزینِ الگویِ
    ``tb = Bot(token=t); await tb.X(); await tb.session.close()`` که اگر
    ``tb.X()`` خطا بدهد، خطِ close() هرگز اجرا نمی‌شود و connection نشتی
    می‌کند.

    استفاده:
        async with _temp_bot(token) as tb:
            await tb.get_me()
    """
    def __init__(self, token: str, **kwargs):
        self._token = token
        self._kwargs = kwargs
        self.bot: Bot | None = None

    async def __aenter__(self) -> Bot:
        self.bot = Bot(token=self._token, **self._kwargs)
        return self.bot

    async def __aexit__(self, *exc):
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return False


# === PATCH_PARTNER_SALES_V1 ===
async def set_tenant_webhook(token: str) -> bool:
    """تنظیمِ وب‌هوکِ یک رباتِ مستأجر با ۳ بار تلاشِ مجدد.

    نسخهٔ بی‌طرفِ همان تابعِ platform_bot.set_tenant_webhook، این‌جا آورده
    شد چون سمتِ مستأجر هم لازمش دارد (فروشِ ربات توسطِ همکار) و طبقِ
    قاعدهٔ پروژه حق ندارد platform_bot.py را import کند.
    """
    import asyncio
    from config import WEBHOOK_BASE

    if not WEBHOOK_BASE:
        return False
    url = f"{WEBHOOK_BASE}/webhook/{token}"
    for attempt in range(3):
        try:
            async with _temp_bot(token) as tb:
                await tb.set_webhook(url, drop_pending_updates=True)
            return True
        except Exception:
            logger.exception("set_tenant_webhook ناموفق (تلاش %s/3)", attempt + 1)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return False


# === PATCH_TOKEN_RECLAIM_V1 ===
# مشکلی که این تابع حل می‌کند:
# «حذفِ ربات» در این پروژه ردیف را پاک نمی‌کند، فقط بایگانی‌اش می‌کند
# (status=paused و archived_at پر می‌شود) — که برایِ نگه‌داشتنِ تاریخچه
# درست است. ولی ستونِ token یکتاست و همهٔ چک‌هایِ «توکن تکراری» بدونِ
# توجه به بایگانی‌بودن انجام می‌شدند. نتیجه: کاربری که رباتش را حذف
# می‌کرد، دیگر **هیچ‌وقت** نمی‌توانست همان توکن را دوباره ثبت کند و پیامِ
# گمراه‌کنندهٔ «این توکن قبلاً ثبت شده» می‌گرفت — حتی برایِ رباتی که
# خودش پاکش کرده بود.
#
# راه‌حل: اگر ردیفِ تکراری بایگانی‌شده است و مالکش همین کاربر است،
# توکنش آزاد می‌شود (با یک مقدارِ جانشینِ یکتا، تا هم قیدِ unique نشکند
# و هم ردیفِ تاریخچه دست‌نخورده بماند).

async def reclaim_token_if_archived(session, token: str, owner_id: int,
                                    exclude_bot_id: int | None = None):
    """اگر این توکن مالِ رباتِ بایگانی‌شدهٔ خودِ همین کاربر است، آزادش کن.

    خروجی: (آزاد شد؟, ردیفِ تکراریِ باقی‌مانده یا None)
      • (False, None)  → توکن اصلاً تکراری نیست، آزاد است
      • (True,  None)  → تکراری بود ولی آزاد شد، می‌شود ادامه داد
      • (False, row)   → تکراریِ واقعی (رباتِ فعال یا مالِ کسِ دیگر)
    """
    from sqlalchemy import select
    from models import Bot as BotRow

    dup = await session.scalar(select(BotRow).where(BotRow.token == token))
    if not dup:
        return False, None
    if exclude_bot_id is not None and dup.id == exclude_bot_id:
        return False, None
    # فقط رباتِ بایگانی‌شدهٔ خودِ همین کاربر قابلِ آزادسازی است.
    if dup.archived_at is None or dup.owner_id != owner_id:
        return False, dup
    dup.token = f"released#{dup.id}"
    await session.flush()
    return True, None


# === PATCH_RELEASED_TOKEN_TWIN_V1 ===
# وقتی مالک رباتش را حذف (بایگانی) می‌کند و بعد با *همان توکن* دوباره
# می‌سازد (خیلی رایج در ربات‌هایِ همکار: مشتری از داخلِ رباتِ همکار حذف و
# دوباره می‌سازد)، reclaim_token_if_archived توکنِ رکوردِ قدیمی را به
# «released#<id>» عوض می‌کند و یک رکوردِ *جدید* با آیدیِ دیگر ساخته می‌شود.
# هر دو رکورد یک یوزرنیم دارند؛ ولی تلگرام فقط به رکوردِ جدید وصل است.
# اگر ادمین رویِ رکوردِ قدیمی «دسترسیِ پشتیبانی» بزند، AdminSupport برایِ
# آیدیِ مرده ثبت می‌شود و در رباتِ واقعی هیچ پنلی نمی‌بیند.
RELEASED_TOKEN_PREFIX = "released#"


def is_released_token(token) -> bool:
    """آیا این توکن مالِ رکوردی است که توکنش به رکوردِ دیگری داده شده؟"""
    return bool(token) and str(token).startswith(RELEASED_TOKEN_PREFIX)


async def live_twin_bot(session, row):
    """رکوردِ زندهٔ همان رباتِ تلگرامی که توکنِ این رکوردِ قدیمی را گرفته.
    اولویت با همان مالک و جدیدترین رکورد است؛ اگر پیدا نشد None."""
    from sqlalchemy import select, func
    from models import Bot as BotRow

    if not row or not row.username:
        return None
    q = (select(BotRow)
         .where(func.lower(BotRow.username) == row.username.lower(),
                BotRow.id != row.id,
                BotRow.archived_at.is_(None),
                BotRow.token.isnot(None),
                ~BotRow.token.startswith(RELEASED_TOKEN_PREFIX))
         .order_by((BotRow.owner_id == row.owner_id).desc(), BotRow.id.desc())
         .limit(1))
    return await session.scalar(q)


# === PATCH_SELF_OWNERSHIP_LOCK_V1 ===
# تعویضِ توکن و انتقالِ مالکیت به‌دستِ خودِ مالک، دو حالت باید قفل باشد:
#   ۱) رباتِ تست (۲ روزهٔ همکار یا تستِ پلنِ خودِ بات‌ساز) تا وقتی خریداری
#      نشده — وگرنه کسی با یک تستِ رایگان توکن را عوض/مالکیت را منتقل
#      می‌کند و عملاً یک رباتِ دائمیِ مجانی می‌سازد.
#   ۲) پلنِ رایگان — طبقِ تصمیمِ مدیریت، فقط با اجازهٔ مدیرِ بات‌ساز.
# خودِ مدیرِ بات‌ساز از مسیرهای ادمینی (abotswapreq / abotxfer / pat:*)
# همچنان بدونِ محدودیت این کارها را انجام می‌دهد؛ این گِیت فقط رویِ
# مسیرهایِ خودِ مالک/همکار گذاشته می‌شود.
TRIAL_LOCK_MSG = (
    "🔒 این ربات هنوز در <b>دورهٔ تست</b> است.\n\n"
    "تا وقتی تست تمام نشده و پلن خریداری نشود، «تعویض توکن» و "
    "«انتقال مالکیت» غیرفعال است. بعد از خرید، خودکار باز می‌شود."
)
FREE_LOCK_MSG = (
    "🔒 در پلنِ <b>پایه (رایگان)</b> «تعویض توکن» و «انتقال مالکیت» "
    "غیرفعال است.\n\n"
    "با ارتقا به پلنِ رشد یا حرفه‌ای باز می‌شود؛ در غیر این صورت فقط "
    "مدیرِ بات‌ساز می‌تواند این کار را برایت انجام دهد — به پشتیبانی پیام بده."
)


async def bot_is_in_trial(session, bot_id: int) -> bool:
    """آیا این ربات یک تستِ مصرف‌نشده است (هنوز خریداری نشده)؟"""
    import datetime
    from sqlalchemy import select, or_

    # اگر برایِ این ربات سفارشِ پلنِ پولی ثبت شده باشد، دیگر تست نیست.
    try:
        from models import PlanOrder
        paid = await session.scalar(
            select(PlanOrder.id).where(PlanOrder.target_bot_id == bot_id,
                                       PlanOrder.amount > 0).limit(1))
        if paid:
            return False
    except Exception:
        logger.exception("bot_is_in_trial: چکِ PlanOrder ناموفق")

    # الف) تستِ ۲ روزهٔ رباتِ همکار
    try:
        from models import PartnerSale
        sold = await session.scalar(
            select(PartnerSale.kind).where(PartnerSale.sold_bot_id == bot_id,
                                           PartnerSale.kind == "sold").limit(1))
        if sold:
            return False
        trial = await session.scalar(
            select(PartnerSale.id).where(PartnerSale.sold_bot_id == bot_id,
                                         PartnerSale.kind == "trial").limit(1))
        if trial:
            return True
    except Exception:
        logger.exception("bot_is_in_trial: چکِ PartnerSale ناموفق")

    # ب) تستِ پلن در خودِ بات‌ساز
    # === PATCH_TRIAL_STATUS_AWARE_CHECK_V1 ===
    # باگ: قبلاً هر سطرِ PlanTrialRequest با همین bot_id (حتی status=
    # rejected/expired/used، یا حتی بعد از خریدِ واقعیِ بعدی) برای همیشه
    # این ربات رو «رویِ تست» نشون می‌داد و کاربرانِ رشد/حرفه‌ایِ واقعاً
    # پول‌داده رو قفل می‌کرد. حالا فقط تستِ *در جریان* (approved، لغونشده،
    # و منقضی‌نشده) یا در انتظارِ تایید (pending) به‌حساب می‌آد.
    try:
        from plan_trials import PlanTrialRequest
        now = datetime.datetime.now()
        t = await session.scalar(
            select(PlanTrialRequest.id).where(
                PlanTrialRequest.bot_id == bot_id,
                PlanTrialRequest.revoked_at.is_(None),
                PlanTrialRequest.status.in_(("pending", "approved")),
                or_(PlanTrialRequest.expires_at.is_(None),
                    PlanTrialRequest.expires_at > now),
            ).limit(1))
        if t:
            return True
    except Exception:
        logger.exception("bot_is_in_trial: چکِ PlanTrialRequest ناموفق")
    return False


async def owner_swap_lock_reason(session, bot_row) -> str | None:
    """اگر مالک اجازهٔ تعویض توکن/انتقال مالکیت ندارد، متنِ دلیل؛ وگرنه None."""
    from plans import Feature, has_feature

    if bot_row is None:
        return None
    if await bot_is_in_trial(session, bot_row.id):
        return TRIAL_LOCK_MSG
    if not has_feature(bot_row.plan, Feature.SELF_TOKEN_SWAP):
        return FREE_LOCK_MSG
    return None


# === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
# وقتی کاربر رویِ دکمهٔ پیامی می‌زند که تلگرام دیگر محتوایش را به بات
# نمی‌دهد (پیامِ قدیمی‌تر از ۴۸ ساعت، یا پیامِ پاک‌شده در گروه)، آبجکتِ
# `cb.message` از نوعِ `Message` نیست بلکه `InaccessibleMessage` است —
# فقط `chat` و `message_id` دارد و بس. هر دسترسی به `.photo` / `.document`
# / `.delete()` روی آن `AttributeError` می‌دهد و هندلر وسطِ کار می‌ترکد
# (در error.log با پیامِ
#   AttributeError: 'InaccessibleMessage' object has no attribute 'photo'
# دیده شده).
#
# این دو هلپر همان چکِ همیشگی را می‌کنند ولی رویِ پیامِ دسترسی‌ناپذیر
# به‌جایِ کرش، `False` برمی‌گردانند — یعنی هندلر مسیرِ «پیامِ متنی» را
# می‌رود که در آن حالت هم درست کار می‌کند (edit_text شکست می‌خورد و
# except موجود پیامِ تازه می‌فرستد).

def is_live_message(m) -> bool:
    """True فقط اگر m یک Messageِ کاملِ قابلِ خواندن/ویرایش باشد."""
    from aiogram.types import Message as _Message
    return isinstance(m, _Message)


def has_media(m) -> bool:
    """آیا پیام عکس/ویدیو/فایل دارد؟ رویِ InaccessibleMessage → False."""
    if not is_live_message(m):
        return False
    return bool(m.photo or m.video or m.document)
