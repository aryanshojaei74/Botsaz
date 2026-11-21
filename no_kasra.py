"""PATCH_NO_KASRA_V1 (درخواست مدیر): کسره (U+0650) در هیچ پیامی که ربات‌ها می‌فرستند نباشد.

چرا اینجا و نه در سورس: کسره در حدود ۸۰ هزار جای کد هست (متن‌ها، برچسب دکمه‌ها، هندلرهایی که متن دکمهٔ کیبوردِ پایین را عیناً
مقایسه می‌کنند). حذفش از سورس کیبوردهایِ باز در چتِ کاربران را از کار می‌اندازد. پس درست پیش از ارسال به تلگرام برداشته می‌شود:
  • متنِ پیام، کپشن، متنِ هشدارِ دکمه، متنِ پیامِ اینلاین
  • برچسبِ دکمه‌هایِ شیشه‌ای (inline) — این‌ها با callback_data کار می‌کنند و متنشان مهم نیست
دست‌نخورده می‌ماند: کیبوردِ پایینِ صفحه (reply keyboard؛ متنش پیش از این با هندلرها مقایسه می‌شود) و هر پیامی که entity دستی دارد
(حذفِ نویسه جایِ entityها را جابه‌جا می‌کند).
"""
from __future__ import annotations

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
KASRA = "ِ"


def clean(text):
    return text.replace(KASRA, "") if isinstance(text, str) and KASRA in text else text


def _set(obj, name: str, value) -> None:
    try:
        setattr(obj, name, value)
    except Exception:  # noqa: BLE001 — مدلِ فریزشده
        object.__setattr__(obj, name, value)


def _strip_attr(obj, name: str) -> None:
    v = getattr(obj, name, None)
    nv = clean(v)
    if nv is not v:
        _set(obj, name, nv)


def _strip_markup(markup) -> None:
    rows = getattr(markup, "inline_keyboard", None)          # فقط شیشه‌ای؛ ReplyKeyboardMarkup اصلاً inline_keyboard ندارد
    if not rows:
        return
    for row in rows:
        for btn in row:
            _strip_attr(btn, "text")


def _strip_content(content) -> None:
    if content is not None and not getattr(content, "entities", None):
        _strip_attr(content, "message_text")


def strip_method(method) -> None:
    """درجا؛ هرگز استثنا نمی‌دهد."""
    try:
        if not getattr(method, "entities", None):
            _strip_attr(method, "text")
        if not getattr(method, "caption_entities", None):
            _strip_attr(method, "caption")
        markup = getattr(method, "reply_markup", None)
        if markup is not None:
            _strip_markup(markup)
        for res in (getattr(method, "results", None) or []):         # AnswerInlineQuery
            _strip_attr(res, "title")
            _strip_attr(res, "description")
            _strip_content(getattr(res, "input_message_content", None))
            rm = getattr(res, "reply_markup", None)
            if rm is not None:
                _strip_markup(rm)
    except Exception:  # noqa: BLE001 — تمیزکاری بی‌اهمیت است؛ پیام باید بی‌هیچ مانعی برود
        logger.debug("no_kasra: strip skipped", exc_info=True)
