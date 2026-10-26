"""halloween.py — «🎃 تم هالوین» برایِ همهٔ ربات‌هایِ بات‌ساز (مدیرِ پلتفرم با یک دکمه روشن/خاموش می‌کند).

کلیدِ سراسری: Setting(bot_id=0, key="halloween_on") + مهلتِ خودکار «halloween_until» (UTC iso)؛ بعد از مهلت خودبه‌خود خاموش
می‌شود تا فراموش‌شده نماند. هیچ دادهٔ رباتی دست‌کاری نمی‌شود؛ هر جا لازم است، لحظهٔ نمایش کلید خوانده می‌شود، پس خاموش‌کردن
فوری و کاملاً برگشت‌پذیر است.

اثرِ روشن‌بودن (برایِ هر ربات مشتری): فقط مینی‌اپ — اگر مالک رنگِ پس‌زمینه/کارت نگذاشته باشد، بنفشِ تیره (مالکِ رنگ‌گذار دست‌نخورده).
(بازیِ «Trick or Treat» در ربات‌هایِ مشتری حذف شد تا با شیرینی‌هایِ گروهِ بات‌ساز قاطی نشود؛ جایزهٔ شیرینی فقط برایِ بازی‌هایِ
گروهِ بات‌ساز است — platform_group_game / platform_candy.)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import select

from config import Session
from models import Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

KEY_ON, KEY_UNTIL = "halloween_on", "halloween_until"
THEME_BG, THEME_CARD = "#1b0f2e", "#2a1744"
DEFAULT_DAYS = 7
_CACHE: dict = {"v": None, "t": 0.0}
_TTL = 8.0



def _tehran_day(now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    return (now + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")


async def _pget(key: str) -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
    return (r.value or "") if r else ""


async def _pset(key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()
    _CACHE["t"] = 0.0


async def is_on(now: datetime | None = None) -> bool:
    """روشن بودنِ تم؛ مهلتِ گذشته = خاموش. نتیجه ۸ ثانیه کش می‌شود (همهٔ پردازش‌ها زود هم‌خوان می‌شوند)."""
    t = time.monotonic()
    if now is None and _CACHE["v"] is not None and t - _CACHE["t"] < _TTL:
        return _CACHE["v"]
    try:
        on = (await _pget(KEY_ON)) == "1"
        if on:
            until = (await _pget(KEY_UNTIL)).strip()
            if until:
                try:
                    on = (now or datetime.utcnow()) < datetime.fromisoformat(until)
                except ValueError:
                    pass
    except Exception:
        logger.exception("halloween: خواندنِ وضعیت ناموفق بود (خاموش فرض شد)")
        return False
    if now is None:
        _CACHE.update(v=on, t=t)
    return on


async def set_on(on: bool, days: int = DEFAULT_DAYS, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    await _pset(KEY_ON, "1" if on else "0")
    await _pset(KEY_UNTIL, (now + timedelta(days=days)).isoformat() if on else "")


async def theme_colors(bot_id: int, kind: str) -> str:
    """رنگِ تمِ هالوین برایِ مینی‌اپ (kind: bg|card)؛ خالی اگر خاموش است."""
    return (THEME_BG if kind == "bg" else THEME_CARD) if await is_on() else ""
