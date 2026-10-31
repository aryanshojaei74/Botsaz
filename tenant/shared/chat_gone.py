"""هندلرِ سراسریِ خطا برایِ «chat not found» (چتِ حذف‌شده/کاربرِ غیرفعال)."""
import logging

from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


async def swallow_chat_gone(event) -> bool:
    """هندلر به چتی پیام می‌فرستاده که دیگر وجود ندارد؛ خطای کد نیست و کاری از ما
    برنمی‌آید، پس فقط info لاگ می‌شود نه traceback."""
    exc = getattr(event, "exception", None)
    if not isinstance(exc, TelegramBadRequest):
        return False
    t = str(exc).lower()
    if "chat not found" not in t and "user is deactivated" not in t:
        return False
    cb = getattr(getattr(event, "update", None), "callback_query", None)
    if cb is not None:
        try:
            await cb.answer()
        except Exception:
            logger.debug("بستنِ اسپینر بعد از chat-not-found ناموفق بود")
    logger.info("chat not found نادیده گرفته شد: %s", exc)
    return True
