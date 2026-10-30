"""
gate.py — مرحلهٔ «در حال تست» (PATCH_PANEL_SELLER_V1).

ساختِ رباتِ «پنل‌فروشی» فقط برایِ کاربرانی باز است که مدیرِ اصلی دسترسی داده باشد، با مدتِ مشخص (روز) یا دائمی.
ذخیره: PlatformSetting(key="psel_beta") = JSON {"<telegram_id>": "<iso expires | ''>"}.
بعد از پایانِ مدت، فروشِ آن ربات بسته می‌شود (خودِ ربات و داده‌ها می‌مانند).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from config import Session
from models import PlatformSetting

KEY = "psel_beta"


async def _load() -> dict:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == KEY))
    try:
        d = json.loads(r.value) if (r and r.value) else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


async def _save(d: dict) -> None:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == KEY))
        val = json.dumps(d, ensure_ascii=False)
        if r:
            r.value = val
        else:
            s.add(PlatformSetting(key=KEY, value=val))
        await s.commit()


async def grant(tg_id: int, days: int = 0, now: datetime | None = None) -> str:
    """days<=0 ⇒ دائمی. خروجی: متنِ پایانِ دسترسی ('' = دائمی)."""
    d = await _load()
    exp = ""
    if days and days > 0:
        exp = ((now or datetime.utcnow()) + timedelta(days=int(days))).isoformat()
    d[str(int(tg_id))] = exp
    await _save(d)
    return exp


async def revoke(tg_id: int) -> bool:
    d = await _load()
    had = d.pop(str(int(tg_id)), None) is not None
    await _save(d)
    return had


async def grants() -> dict[int, str]:
    return {int(k): v for k, v in (await _load()).items() if str(k).lstrip("-").isdigit()}


async def status(tg_id: int, now: datetime | None = None) -> tuple[bool, str]:
    """(مجاز؟, دلیل). دلیل: ok | none | expired."""
    d = await _load()
    k = str(int(tg_id))
    if k not in d:
        return False, "none"
    exp = d[k]
    if not exp:
        return True, "ok"
    try:
        if datetime.fromisoformat(exp) > (now or datetime.utcnow()):
            return True, "ok"
    except ValueError:
        return False, "expired"
    return False, "expired"


async def allowed(tg_id: int) -> bool:
    return (await status(tg_id))[0]
