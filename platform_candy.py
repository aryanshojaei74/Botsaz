"""platform_candy.py — سقفِ روزانه و «جایزهٔ ۱۰۰ شیرینی» برایِ بازی‌هایِ رایگانِ گروهِ بات‌ساز اصلی.

=== PATCH_CANDY_REWARD_V1 ===
• هر کاربر در روز حداکثر pgc_cap_day (پیش‌فرض ۲۵) شیرینی می‌گیرد؛ پس رسیدن به ۱۰۰ شیرینی کمی زمان می‌برد و با یک شبه فارمِ
  چند اکانتی نمی‌شود.
• وقتی مجموعِ شیرینیِ کاربر به pgc_goal (پیش‌فرض ۱۰۰) برسد، pgc_amount (پیش‌فرض ۷٬۰۰۰ تومان) به کیف‌پولِ بات‌ساز اصلیِ او اضافه
  می‌شود؛ هر کاربر فقط یک بار. سقفِ تعدادِ جایزه‌ها pgc_max (پیش‌فرض ۲۰ نفر) = سقفِ کلِ هزینه. فقط وقتی «تم هالوین» روشن است.
• خود جایزه پولِ واقعیِ بات‌ساز است (PlatformUser.balance + دفترِ PlatformTxn)، ثبتِ اتمیک و یک‌بار.
• تنظیم: مدیر در پی‌وی ربات اصلی می‌نویسد «/hwreward مبلغ هدف سقفِ‌نفرات سقفِ‌روزانه»؛ بدونِ عدد = نمایشِ مقدارِ فعلی.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from config import Session
from models import PlatformTxn, PlatformUser, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

KEYS = {"on": "pgc_on", "amount": "pgc_amount", "goal": "pgc_goal", "max": "pgc_max", "cap": "pgc_cap_day"}
DEFAULTS = {"on": "1", "amount": "7000", "goal": "100", "max": "20", "cap": "25"}
_LOCK = asyncio.Lock()


async def _pget(key: str, default: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(key, default)


async def _pset(key: str, value: str) -> None:
    from platform_bot import set_pset
    await set_pset(key, value)


async def cfg() -> dict:
    out = {}
    for k, key in KEYS.items():
        v = (await _pget(key, DEFAULTS[k])) or DEFAULTS[k]
        out[k] = (v == "1") if k == "on" else max(0, int(v)) if str(v).isdigit() else int(DEFAULTS[k])
    return out


async def set_numbers(amount: int, goal: int, max_users: int, cap: int) -> None:
    for k, v in (("amount", amount), ("goal", goal), ("max", max_users), ("cap", cap)):
        await _pset(KEYS[k], str(int(v)))


async def toggle() -> bool:
    new = "0" if (await cfg())["on"] else "1"
    await _pset(KEYS["on"], new)
    return new == "1"


# ───────────────────────── سقفِ روزانه ─────────────────────────

async def apply_cap(uid: int, pts: int, day: str, cap: int) -> int:
    """امتیازی که واقعاً داده می‌شود (۰ تا pts)؛ شمارندهٔ روزانه را زیاد می‌کند. cap=۰ ⇒ بدونِ سقف."""
    if cap <= 0:
        return pts
    key = f"pggd:{uid}"
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        d, n = "", 0
        if r and "|" in (r.value or ""):
            d, _, n_s = r.value.partition("|")
            n = int(n_s) if n_s.isdigit() else 0
        if d != day:
            n = 0
        grant = max(0, min(pts, cap - n))
        val = f"{day}|{n + grant}"
        if r:
            r.value = val
        else:
            s.add(Setting(bot_id=0, key=key, value=val))
        await s.commit()
    return grant


async def used_today(uid: int, day: str) -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == f"pggd:{uid}"))
    if r and "|" in (r.value or ""):
        d, _, n = r.value.partition("|")
        return int(n) if d == day and n.isdigit() else 0
    return 0


# ───────────────────────── جایزه ─────────────────────────

async def paid_count() -> int:
    return int((await _pget("pgc_paid", "0")) or 0)


async def claimed(uid: int) -> bool:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == f"pggr:{uid}"))
    return bool(r and r.value == "1")


async def candy_active() -> bool:
    """مودِ شیرینی فعال است؟ هم کلیدِ شیرینی (pgc_on) و هم «تم هالوین» (با مهلتش) باید روشن باشند؛ وگرنه بازی‌هایِ رایگان
    شیرینی نمی‌دهند (قبلاً فقط پرداختِ جایزهٔ پولی گیت می‌شد و امتیازِ شیرینی بعد از خاموش‌کردن هم ادامه داشت)."""
    try:
        import halloween as HW
        return bool((await cfg())["on"]) and await HW.is_on()
    except Exception:  # noqa: BLE001
        return False


async def try_reward(uid: int, total: int) -> dict:
    """status: off | below | already | budget | no_user | paid (+ amount)."""
    c = await cfg()
    try:
        import halloween as HW
        theme_on = await HW.is_on()
    except Exception:  # noqa: BLE001
        theme_on = False
    if not c["on"] or not theme_on or c["amount"] <= 0:
        return {"status": "off", "cfg": c}
    if total < c["goal"]:
        return {"status": "below", "cfg": c}
    async with _LOCK:
        if await claimed(uid):
            return {"status": "already", "cfg": c}
        if c["max"] and await paid_count() >= c["max"]:
            return {"status": "budget", "cfg": c}
        async with Session() as s:
            u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid).with_for_update())
            if u is None:
                return {"status": "no_user", "cfg": c}
            u.balance = Decimal(str(u.balance or 0)) + Decimal(c["amount"])
            s.add(PlatformTxn(user_telegram_id=uid, amount=c["amount"], kind="halloween_reward",
                              note=f"جایزهٔ {c['goal']} شیرینی هالوین"))
            r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == f"pggr:{uid}"))
            if r:
                r.value = "1"
            else:
                s.add(Setting(bot_id=0, key=f"pggr:{uid}", value="1"))
            pc = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "pgc_paid"))
            if pc:
                pc.value = str(int(pc.value or 0) + 1)
            else:
                s.add(Setting(bot_id=0, key="pgc_paid", value="1"))
            await s.commit()
    logger.info("halloween reward paid: uid=%s amount=%s", uid, c["amount"])
    return {"status": "paid", "amount": c["amount"], "cfg": c}
