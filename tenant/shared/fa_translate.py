"""fa_translate.py — ترجمهٔ خودکارِ توضیحاتِ ارائه‌دهنده به فارسی (=== PATCH_SMM_DESC_TRANSLATE_V1 ===).

گزارشِ فدایی: «یه API خارجی وصل کردم؛ توضیحاتش انگلیسیه و ربات خودکار فارسی نمی‌کنه». توضیحاتِ ارائه‌دهنده (کیفیت، سرعت، ریفیل…)
معمولاً انگلیسی است. این ماژول فقط متنی را ترجمه می‌کند که عمدتاً لاتین باشد، با پایه‌ترین نقطهٔ ترجمهٔ عمومیِ گوگل
(بدون کلید). نتیجه در ردیفِ همان سرویس ذخیره می‌شود (ستون provider_desc_fa) تا هر متن فقط یک‌بار ترجمه شود؛ اگر سرویسِ ترجمه
جواب ندهد متنِ اصلی نشان داده می‌شود (هرگز خطا به مشتری نمی‌رسد) و تا ۵ دقیقه دوباره تلاش نمی‌شود.
"""
from __future__ import annotations

import hashlib
import re
import time

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
_CHUNK = 1200                       # نویسهٔ هر درخواست (GET)
_MAX_TEXT = 6000                    # متنِ بلندتر از این ترجمه نمی‌شود
_TIMEOUT = 6.0
_NEG_TTL = 300.0
_NEG: dict[str, float] = {}
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC = re.compile(r"[؀-ۿ]")


def needs_translation(text: str | None) -> bool:
    """True اگر متن عمدتاً لاتین باشد (حداقل ۸ حرف لاتین و بیش از نیمِ حروفِ متن)."""
    t = text or ""
    lat, ara = len(_LATIN.findall(t)), len(_ARABIC.findall(t))
    return lat >= 8 and lat > ara


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def split_chunks(text: str, size: int = _CHUNK) -> list[str]:
    """متن را خط‌به‌خط به تکه‌هایِ حداکثر size نویسه می‌شکند (خطِ بلندتر از size جدا بریده می‌شود)."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        add = line if not cur else cur + "\n" + line
        if len(add) > size:
            chunks.append(cur)
            cur = line
        else:
            cur = add
    if cur:
        chunks.append(cur)
    return chunks


async def _fetch(chunk: str) -> str | None:
    try:
        import aiohttp
    except Exception:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as s:
            async with s.get(_ENDPOINT, params={"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t", "q": chunk}) as r:
                if r.status != 200:
                    logger.info("fa_translate: HTTP %s", r.status)
                    return None
                data = await r.json(content_type=None)
        return "".join(str(p[0]) for p in (data[0] or []) if p and p[0])
    except Exception as e:  # noqa: BLE001
        logger.info("fa_translate: %s", type(e).__name__)
        return None


async def translate_fa(text: str, fetch=None) -> str | None:
    """ترجمهٔ فارسی، یا None اگر ممکن نشد/لازم نبود. fetch برای تست قابل‌تعویض است."""
    text = (text or "").strip()
    if not text or len(text) > _MAX_TEXT or not needs_translation(text):
        return None
    h = text_hash(text)
    if _NEG.get(h, 0) > time.monotonic():
        return None
    out = []
    for ch in split_chunks(text):
        tr = await (fetch or _fetch)(ch)
        if not tr:
            _NEG[h] = time.monotonic() + _NEG_TTL
            return None
        out.append(tr)
    res = "\n".join(o.rstrip("\n") for o in out).strip()
    return res or None
