"""
kyc_lock.py — PATCH_KYC_LOCK_V1

گزارشِ فدایی: «وضعیتِ حساب برایِ ما محدودشده می‌آید ولی کاربر در ربات آزاد است و کارهایش را می‌کند.»
دلیل: kyc_blocked فقط در مسیرهایِ «شارژ» چک می‌شد (check_charge). بقیهٔ ربات (خرید، انتقالِ موجودی،
بازیِ گروهی، …) اصلاً نمی‌دانست حساب محدود است.

این میان‌افزار (روی tenant_router، پیام + کال‌بک) برایِ کاربرِ محدودشده همه‌چیز را می‌بندد:
  • خصوصی: فقط /start و مسیرِ احرازِ هویت (کال‌بک‌هایِ kycu: و ویزاردِ KycUserFSM) باز است؛ بقیه
    پیامِ «حسابِ شما محدود شده، احرازِ هویت را انجام بده» + دکمهٔ احراز می‌گیرند.
  • گروه: فقط وقتی با ربات تعامل کند (دستور، «بازی/شرط …»، «انتقال …»، «موجودی»، تاس، بازی‌هایِ گروهی)
    مسدود می‌شود و در گروه می‌شنود که حسابش محدود است؛ چت‌هایِ عادیِ گروه دست‌نخورده است.
مالک/ادمین‌هایِ ربات و ادمینِ پلتفرم معاف‌اند. اگر احرازِ هویت برایِ ربات خاموش باشد اثری ندارد.
کشِ ۱۰ثانیه‌ایِ وضعیت، هزینهٔ دیتابیس را کم می‌کند (بعد از رفعِ محدودیت حداکثر ۱۰ ثانیه تا بازشدن).
"""
from __future__ import annotations

import re
import time

from sqlalchemy import select

from config import Session
from models import BotUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

CACHE_TTL = 10.0
NOTICE_GAP = 20.0

_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_last_notice: dict[tuple[int, int], float] = {}

PRIVATE_NOTICE = ("🔒 حسابِ شما محدود شده است.\n\n"
                  "تا انجامِ احرازِ هویتِ خواسته‌شده نمی‌توانی از ربات استفاده کنی "
                  "(خرید، شارژ، انتقالِ موجودی، بازی‌هایِ گروهی و …).\n"
                  "روی دکمهٔ زیر بزن و مراحلِ احرازِ هویت را کامل کن 👇")
CB_NOTICE = "🔒 حسابِ شما محدود شده؛ ابتدا احرازِ هویتِ خواسته‌شده را انجام بده."
GROUP_NOTICE = ("⛔️ {name}، حسابِ شما محدود شده است؛ ابتدا در پیوی رباتْ احرازِ هویتِ خواسته‌شده را انجام بده. "
                "تا آن موقع بازی و انتقال در گروه برایت ممکن نیست.")

_TRANSFER_PREFIX = "انتقال"
_BALANCE_WORD = "موجودی"
_BET_RE = re.compile(r"^(?:بازی|شرطبندی|شرط\s*بندی)(?:\s+[۰-۹0-9,،]+)?$", re.IGNORECASE)


def invalidate(bot_id: int, tg_id: int) -> None:
    _cache.pop((bot_id, tg_id), None)


async def is_locked(bot_id: int, tg_id: int, now: float | None = None) -> bool:
    """کاربرِ این ربات محدودشده است و احرازِ هویت برایِ ربات روشن است؟ (کش ۱۰ثانیه)"""
    now = time.monotonic() if now is None else now
    hit = _cache.get((bot_id, tg_id))
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    locked = False
    try:
        from tenant.shared import kyc as K
        if await K.enabled(bot_id):
            async with Session() as s:
                blk = await s.scalar(select(BotUser.kyc_blocked).where(
                    BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
            locked = bool(blk)
    except Exception:
        logger.exception("[KYC_LOCK] چکِ وضعیت ناموفق بود (bot=%s tg=%s)", bot_id, tg_id)
    _cache[(bot_id, tg_id)] = (now, locked)
    return locked


def _should_notice(bot_id: int, tg_id: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    k = (bot_id, tg_id)
    if now - _last_notice.get(k, float("-inf")) >= NOTICE_GAP:
        _last_notice[k] = now
        return True
    return False


def group_interaction(text: str | None, has_dice: bool = False) -> bool:
    """آیا این پیامِ گروه تعامل با ربات است (دستور/بازی/انتقال/موجودی/تاس)؟ چتِ عادی false است."""
    if has_dice:
        return True
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("/") or t.startswith(_TRANSFER_PREFIX) or t == _BALANCE_WORD or _BET_RE.match(t):
        return True
    try:
        import platform_wager
        return platform_wager.parse_command(t) is not None
    except Exception:
        return False


def private_allowed(text: str | None, fsm_state: str | None) -> bool:
    """در پیوی برایِ کاربرِ محدودشده فقط /start و ویزاردِ احرازِ هویت باز است."""
    t = (text or "").strip().lower()
    if t == "/start" or t.startswith("/start "):
        return True
    return bool(fsm_state) and fsm_state.startswith("KycUserFSM")


def _kyc_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu")]])


async def message_gate(event, bot_id: int, fsm_state: str | None) -> bool:
    """True = پیام را به هندلرها بده؛ False = مسدود (و پیامِ مناسب فرستاده شد)."""
    u = event.from_user
    if not await is_locked(bot_id, u.id):
        return True
    chat_type = getattr(event.chat, "type", "private")
    if chat_type == "private":
        if private_allowed(event.text, fsm_state):
            return True
        if _should_notice(bot_id, u.id):
            try:
                await event.answer(PRIVATE_NOTICE, reply_markup=_kyc_kb())
            except Exception:
                logger.debug("[KYC_LOCK] پیامِ محدودیت ارسال نشد")
        return False
    if group_interaction(event.text, bool(getattr(event, "dice", None))):
        if _should_notice(bot_id, u.id):
            try:
                await event.reply(GROUP_NOTICE.format(name=u.full_name or "کاربر"))
            except Exception:
                logger.debug("[KYC_LOCK] پیامِ محدودیتِ گروه ارسال نشد")
        return False
    return True


async def callback_gate(event, bot_id: int) -> bool:
    """True = کال‌بک را به هندلرها بده؛ False = مسدود (با پاپ‌آپ)."""
    u = event.from_user
    if not await is_locked(bot_id, u.id):
        return True
    data = event.data or ""
    chat_type = getattr(getattr(getattr(event, "message", None), "chat", None), "type", "private")
    if chat_type == "private" and (data.startswith("kycu:") or data == "noop"):
        return True
    try:
        await event.answer(CB_NOTICE, show_alert=True)
    except Exception:
        logger.debug("[KYC_LOCK] پاسخِ کال‌بکِ محدودیت ممکن نشد")
    return False


async def notify_blocked(bot_id: int, user_id: int) -> None:
    """بعد از مسدودشدنِ خودکار (عبور از سقفِ سطح ۱) به کاربر خبر بده؛ قبلاً هیچ پیامی نمی‌رفت."""
    try:
        from bot_utils import _temp_bot
        from models import Bot as BotRow
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
            u = await s.get(BotUser, user_id)
        if not row or not u:
            return
        invalidate(bot_id, u.telegram_id)
        async with _temp_bot(row.token) as tb:
            await tb.send_message(u.telegram_id, PRIVATE_NOTICE, reply_markup=_kyc_kb())
    except Exception:
        logger.exception("[KYC_LOCK] اطلاع‌رسانیِ محدودیت به کاربر ناموفق بود (bot=%s user=%s)", bot_id, user_id)
