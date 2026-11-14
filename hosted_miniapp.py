"""hosted_miniapp.py — ثبتِ «مینی‌اپ» برایِ رباتِ میزبانی‌شدهٔ کاربر (PATCH_HOSTED_MINIAPP_V1).

کاربر کدِ رباتش را روی سرورِ فعال‌سازی اجرا کرده و می‌خواهد لینکِ مینی‌اپ را به رباتش وصل کند. دو راه:
  ۱) دستی در BotFather (راهنمایِ متنیِ guide_text()).
  ۲) از دکمهٔ «🌐 ثبت مینی‌اپ» در مینی‌اپِ میزبانی (سرویسِ botsazvip) که این ماژول را صدا می‌زند:
     توکنِ ربات از متغیرهایِ محیطیِ همان اپ پیدا می‌شود و با setChatMenuButton دکمهٔ «Menu» رباتِ او به مینی‌اپ وصل می‌شود.

هیچ‌جا توکن لاگ/برگردانده نمی‌شود؛ فقط نامِ کاربریِ ربات.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
_TOKEN_KEYS = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TG_BOT_TOKEN", "API_TOKEN", "TOKEN", "BOT_API_TOKEN")


@dataclass
class Result:
    ok: bool
    message: str
    bot_username: str = ""


def validate_url(url: str | None) -> str | None:
    """آدرسِ معتبرِ https یا None. تلگرام برایِ وب‌اپ فقط https می‌پذیرد؛ t.me/لوکال/IP رد می‌شود."""
    u = (url or "").strip()
    if not u or " " in u or len(u) > 500:
        return None
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or "." not in host or host in ("t.me", "telegram.me") or host.endswith(".local"):
        return None
    if re.fullmatch(r"[\d.]+", host) or host in ("localhost",):
        return None
    return u


def find_bot_token(envs: dict[str, str] | list[tuple[str, str]]) -> str | None:
    """توکنِ ربات را از متغیرهایِ محیطیِ اپ پیدا می‌کند: اول کلیدهایِ معمول، بعد هر مقداری که شکلِ توکن دارد."""
    items = dict(envs) if not isinstance(envs, dict) else dict(envs)
    norm = {str(k).strip().upper(): str(v).strip().strip("'\"") for k, v in items.items()}
    for k in _TOKEN_KEYS:
        if _TOKEN_RE.match(norm.get(k, "")):
            return norm[k]
    for v in norm.values():
        if _TOKEN_RE.match(v):
            return v
    return None


# === PATCH_MULTI_BOT_MINIAPP_V1 === (MTS Arena) یک مینی‌اپ می‌تواند برایِ چند ربات (مثلاً رباتِ تست + رباتِ اصلی) کار کند:
# توکن‌ها در BOT_TOKENS (جدا با ویرگول/فاصله/خطِ جدید) و/یا BOT_TOKEN و بقیهٔ کلیدهایِ معمول.
_TOKENS_KEYS = ("BOT_TOKENS", "TELEGRAM_BOT_TOKENS", "TG_BOT_TOKENS")


def find_bot_tokens(envs: dict[str, str] | list[tuple[str, str]]) -> list[str]:
    """همهٔ توکن‌هایِ معتبرِ اپ (بدونِ تکرار، با حفظِ ترتیب): کلیدهایِ چندتایی، کلیدهایِ تکیِ معمول، و هر مقدارِ توکن‌مانند."""
    items = dict(envs) if not isinstance(envs, dict) else dict(envs)
    norm = {str(k).strip().upper(): str(v).strip().strip("'\"") for k, v in items.items()}
    out: list[str] = []

    def _add(t: str) -> None:
        t = (t or "").strip().strip("'\"")
        if _TOKEN_RE.match(t) and t not in out:
            out.append(t)
    for k in _TOKENS_KEYS:
        for part in re.split(r"[,\s;]+", norm.get(k, "")):
            _add(part)
    for k in _TOKEN_KEYS:
        _add(norm.get(k, ""))
    for k, v in norm.items():
        if k in _TOKENS_KEYS or k in _TOKEN_KEYS:
            continue
        for part in re.split(r"[,\s;]+", v):
            _add(part)
    return out


async def _call(token: str, method: str, payload: dict | None = None, *, session: aiohttp.ClientSession | None = None) -> dict:
    own = session is None
    s = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        async with s.post(f"{API}/bot{token}/{method}", json=payload or {}) as r:
            try:
                return await r.json(content_type=None)
            except Exception:      # noqa: BLE001
                return {"ok": False, "description": f"HTTP {r.status}"}
    finally:
        if own:
            await s.close()


async def set_menu_button(token: str, url: str, text: str = "🌐 باز کردن", *, call=_call) -> Result:
    """دکمهٔ «Menu» رباتِ کاربر را به مینی‌اپ وصل می‌کند (همهٔ چت‌ها)."""
    good = validate_url(url)
    if not good:
        return Result(False, "آدرس باید با https:// شروع شود و یک دامنهٔ واقعی باشد (لینکِ t.me یا آی‌پی قبول نیست).")
    label = (text or "🌐 باز کردن").strip()[:16] or "🌐 باز کردن"
    me = await call(token, "getMe")
    if not me.get("ok"):
        return Result(False, "توکنِ ربات نامعتبر است یا تلگرام جواب نداد؛ توکن را در «متغیرها (.env)» بررسی کن.")
    uname = (me.get("result") or {}).get("username") or ""
    res = await call(token, "setChatMenuButton", {"menu_button": {"type": "web_app", "text": label, "web_app": {"url": good}}})
    if not res.get("ok"):
        return Result(False, f"تلگرام قبول نکرد: {str(res.get('description') or '')[:150]}", uname)
    return Result(True, f"✅ مینی‌اپ به رباتِ @{uname} وصل شد. تلگرام را ببند و دوباره باز کن و روی دکمهٔ Menu کنارِ کادرِ پیام بزن.", uname)


async def clear_menu_button(token: str, *, call=_call) -> Result:
    res = await call(token, "setChatMenuButton", {"menu_button": {"type": "default"}})
    return Result(bool(res.get("ok")), "✅ دکمهٔ مینی‌اپ برداشته شد." if res.get("ok") else f"ناموفق: {str(res.get('description') or '')[:150]}")


async def set_for_app(envs, url: str, text: str = "🌐 باز کردن", *, call=_call) -> Result:
    """envs: متغیرهایِ محیطیِ اپِ میزبانی‌شده (dict یا جفت‌ها). توکن را خودش پیدا می‌کند."""
    tokens = find_bot_tokens(envs)
    if not tokens:
        return Result(False, "توکنِ ربات در «متغیرها (.env)» پیدا نشد. اول BOT_TOKEN=... را آن‌جا ذخیره کن.")
    if len(tokens) == 1:
        return await set_menu_button(tokens[0], url, text, call=call)
    # چند ربات (BOT_TOKENS): برایِ همه وصل می‌کنیم و نتیجهٔ هر کدام را جدا گزارش می‌دهیم
    results = [await set_menu_button(t, url, text, call=call) for t in tokens]
    ok = any(r.ok for r in results)
    lines = [(f"@{r.bot_username}: " if r.bot_username else "") + r.message for r in results]
    return Result(ok, "\n".join(lines), next((r.bot_username for r in results if r.bot_username), ""))


async def fill_bot_usernames(limit: int = 60, *, call=_call) -> int:
    """یوزرنیمِ رباتِ اپ‌هایی که هنوز ثبت نشده را با getMe (توکنِ همان اپ از envها) پیدا و ذخیره می‌کند. تعدادِ ثبت‌شده‌ها را برمی‌گرداند."""
    import asyncio
    from sqlalchemy import select
    from config import Session
    from models import HostedApp, HostedAppEnv
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp).where(HostedApp.bot_username.is_(None))
                                .order_by(HostedApp.id.desc()).limit(limit))).all()
        envs: dict[int, dict[str, str]] = {}
        if apps:
            rows = (await s.scalars(select(HostedAppEnv).where(HostedAppEnv.app_id.in_([a.id for a in apps])))).all()
            for r in rows:
                envs.setdefault(r.app_id, {})[r.key] = r.value
    jobs = [(a.id, find_bot_token(envs.get(a.id, {}))) for a in apps]
    jobs = [(i, t) for i, t in jobs if t]
    if not jobs:
        return 0
    sem = asyncio.Semaphore(8)

    async def one(app_id: int, token: str):
        async with sem:
            try:
                me = await call(token, "getMe")
            except Exception:
                logger.exception("fill_bot_usernames: getMe ناموفق (app=%s)", app_id)
                return app_id, ""
            return app_id, ((me.get("result") or {}).get("username") or "") if me.get("ok") else ""
    found = [r for r in await asyncio.gather(*(one(i, t) for i, t in jobs)) if r[1]]
    if found:
        async with Session() as s:
            for app_id, uname in found:
                a = await s.get(HostedApp, app_id)
                if a is not None:
                    a.bot_username = uname[:64]
            await s.commit()
    return len(found)


def guide_text() -> str:
    return (
        "🌐 <b>ثبتِ مینی‌اپ برایِ رباتِ خودت</b>\n\n"
        "<b>راهِ ۱ — با دکمه (ساده‌تر):</b>\nروی «🌐 ثبت مینی‌اپ» بزن، آدرسِ <code>https://...</code> مینی‌اپ را بفرست؛ "
        "توکنِ رباتت خودکار از «متغیرها (.env)» خوانده می‌شود.\n\n"
        "<b>راهِ ۲ — دستی در BotFather:</b>\n"
        "۱) به @BotFather برو و /mybots را بزن و رباتت را انتخاب کن.\n"
        "۲) Bot Settings ← Menu Button ← Configure menu button.\n"
        "۳) آدرسِ https مینی‌اپ و بعد نامِ دکمه (مثلاً «🌐 باز کردن») را بفرست.\n\n"
        "<b>راهِ ۳ — داخلِ کدِ ربات:</b> دکمهٔ اینلاین با <code>WebAppInfo(url=...)</code> یا "
        "<code>setChatMenuButton</code>.\n\n"
        "⚠️ آدرس باید <b>https</b> و دامنهٔ واقعی باشد؛ لینکِ <code>t.me/...</code> یا آی‌پی قبول نمی‌شود."
    )
