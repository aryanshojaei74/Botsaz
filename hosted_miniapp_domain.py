"""hosted_miniapp_domain.py — ساب‌دامینِ خودکارِ مینی‌اپ برایِ رباتِ میزبانی‌شدهٔ کاربر (PATCH_MINIAPP_DOMAIN_V1).

هر اپِ میزبانی‌شده می‌تواند با یک اشتراکِ ماهانهٔ جدا (پیش‌فرض ۱۰۰٬۰۰۰ تومان) یک ساب‌دامینِ ثابتِ HTTPS بگیرد:
    https://m1a2b3c4d.supraplus.ir  →  کانتینرِ همان اپ (پورتِ واقعیِ برنامه، مثلِ مسیرِ /u/<name>/)

معماری (روی سرور):
  Cloudflare (*.supraplus.ir، Proxied) → nginx stream (SNI) → 127.0.0.1:8443 → بلوکِ *.supraplus.ir
  → map $host → «IP:PORT» کانتینر. هر اپ یک فایلِ جدا در MAP_DIR دارد: «<host> <ip>:<port>;» و nginx با
  `include /etc/nginx/miniapp-hosts.d/*.map;` همه را می‌خواند. IPِ کانتینر با هر ری‌استارت عوض می‌شود، پس sync()
  (از سرویسِ botsazvip هر ~۶۰ ثانیه) نگاشت‌ها را تازه می‌کند و اشتراکِ تمام‌شده را برمی‌دارد.

این ماژول به خودِ docker وابسته نیست: sync() یک شیِ `dm` (docker_manager سرویسِ botsazvip) می‌گیرد.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from config import Session
from models import HostedApp, HostedAppEnv, PlatformTxn, PlatformUser, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

DOMAIN = os.getenv("MINIAPP_DOMAIN", "supraplus.ir").strip().lower()
MAP_DIR = Path(os.getenv("MINIAPP_MAP_DIR", "/etc/nginx/miniapp-hosts.d"))
PRICE_KEY = "vip_miniapp_price"
DEFAULT_PRICE = 100_000
PERIOD_DAYS = 30
_SUB_RE = re.compile(r"^[a-z][a-z0-9]{5,15}$")
_UP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$")


@dataclass
class Result:
    ok: bool
    message: str
    url: str = ""
    until: dt.datetime | None = None
    need: int = 0


def host_of(sub: str) -> str:
    return f"{sub}.{DOMAIN}"


def url_of(sub: str) -> str:
    return f"https://{host_of(sub)}"


def new_sub() -> str:
    """ساب‌دامینِ تصادفی (نه نامِ دلخواه) تا اسم‌هایِ فریبنده مثل bank-login ممکن نباشد."""
    return "m" + secrets.token_hex(4)


def is_active(a, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    return bool(getattr(a, "miniapp_sub", None) and getattr(a, "miniapp_until", None) and a.miniapp_until > now)


async def get_price() -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == PRICE_KEY))
    try:
        v = int(float(r.value)) if (r and r.value not in (None, "")) else DEFAULT_PRICE
    except (TypeError, ValueError):
        v = DEFAULT_PRICE
    return max(0, v)


# ───────────────────────── فایل‌هایِ نگاشتِ nginx ─────────────────────────
def _map_path(app_name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,29}", app_name or ""):
        raise ValueError("نامِ اپ نامعتبر")
    return MAP_DIR / f"{app_name}.map"


def write_map(app_name: str, sub: str, upstream: str) -> bool:
    """نگاشتِ این اپ را می‌نویسد. True اگر محتوا تغییر کرد."""
    if not _SUB_RE.match(sub or "") or not _UP_RE.match(upstream or ""):
        raise ValueError("ساب‌دامین یا upstream نامعتبر")
    path = _map_path(app_name)
    text = f"{host_of(sub)} {upstream};\n"
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)          # نوشتنِ اتمیک؛ nginx هرگز فایلِ نیمه‌کاره نمی‌بیند
    return True


def remove_map(app_name: str) -> bool:
    try:
        _map_path(app_name).unlink()
        return True
    except FileNotFoundError:
        return False


async def _run(*cmd: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), timeout=30)
    return p.returncode or 0, (err or out or b"").decode(errors="replace")


async def reload_nginx(run=None) -> str | None:
    """nginx -t و بعد reload؛ خروجی: پیامِ خطا یا None."""
    run = run or _run
    code, out = await run("nginx", "-t")
    if code != 0:
        return f"تنظیماتِ nginx معتبر نشد: {out[-300:]}"
    code, out = await run("systemctl", "reload", "nginx")
    return None if code == 0 else f"ریلودِ nginx ناموفق بود: {out[-300:]}"


# ───────────────────────── خرید / مدیریت ─────────────────────────
async def _unique_sub(s) -> str:
    for _ in range(8):
        sub = new_sub()
        if not await s.scalar(select(HostedApp.id).where(HostedApp.miniapp_sub == sub)):
            return sub
    raise RuntimeError("ساختِ ساب‌دامینِ یکتا ممکن نشد")


async def _extend(app_id: int, days: int, *, charge: bool) -> Result:
    now = dt.datetime.now()
    async with Session() as s:
        a = await s.get(HostedApp, app_id, with_for_update=True)
        if a is None:
            return Result(False, "اپ پیدا نشد.")
        if a.status == "archived":
            return Result(False, "این اپ بایگانی شده است.")
        price = await get_price() if charge else 0
        if price > 0:
            u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == a.owner_id).with_for_update())
            bal = int(float(u.balance or 0)) if u else 0
            if bal < price:
                return Result(False, f"موجودیِ کیف‌پول کافی نیست ({price:,} تومان لازم است).", need=price - bal)
            u.balance = bal - price
            s.add(PlatformTxn(user_telegram_id=a.owner_id, amount=-price, kind="miniapp", note=f"مینی‌اپ {a.name}"[:200]))
        if not a.miniapp_sub:
            a.miniapp_sub = await _unique_sub(s)
        base = a.miniapp_until if (a.miniapp_until and a.miniapp_until > now) else now
        a.miniapp_until = base + dt.timedelta(days=days)
        sub, until = a.miniapp_sub, a.miniapp_until
        await s.commit()
    await _set_env(app_id, "MINIAPP_URL", url_of(sub))
    return Result(True, "مینی‌اپ فعال شد.", url_of(sub), until)


async def buy(app_id: int, months: int = 1) -> Result:
    """اشتراکِ ماهانه با کسر از کیف‌پولِ مالک (PlatformTxn ثبت می‌شود)."""
    return await _extend(app_id, PERIOD_DAYS * max(1, int(months)), charge=True)


async def grant(app_id: int, days: int = PERIOD_DAYS) -> Result:
    """فعال‌سازیِ رایگان توسطِ مدیر."""
    return await _extend(app_id, int(days), charge=False)


async def disable(app_id: int) -> bool:
    """اشتراک را همین الان تمام می‌کند و نگاشت را برمی‌دارد (ساب‌دامین برایِ تمدیدِ بعدی رزرو می‌ماند)."""
    async with Session() as s:
        a = await s.get(HostedApp, app_id, with_for_update=True)
        if a is None:
            return False
        a.miniapp_until = None
        name = a.name
        await s.commit()
    removed = remove_map(name)
    if removed:
        await reload_nginx()
    return True


async def _set_env(app_id: int, key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(HostedAppEnv).where(HostedAppEnv.app_id == app_id, HostedAppEnv.key == key))
        if r:
            r.value = value
        else:
            s.add(HostedAppEnv(app_id=app_id, key=key, value=value))
        await s.commit()


async def _env_dict(app_id: int) -> dict[str, str]:
    async with Session() as s:
        rows = (await s.scalars(select(HostedAppEnv).where(HostedAppEnv.app_id == app_id))).all()
    return {r.key: r.value for r in rows}


async def set_menu_button(app_id: int, text: str = "🌐 باز کردن"):
    """Menu Button رباتِ همین اپ را به ساب‌دامینش وصل می‌کند (hosted_miniapp.set_for_app)."""
    import hosted_miniapp as HM
    async with Session() as s:
        a = await s.get(HostedApp, app_id)
    if a is None or not a.miniapp_sub:
        return HM.Result(False, "اول مینی‌اپ را فعال کن.")
    return await HM.set_for_app(await _env_dict(app_id), url_of(a.miniapp_sub), text)


async def sync(dm, *, run=None, now: dt.datetime | None = None) -> dict:
    """نگاشت‌ها را با وضعیتِ فعلی هماهنگ می‌کند (از botsazvip هر ~۶۰ ثانیه صدا زده شود).
    اپِ فعال و روشن ⇒ نگاشتِ تازه (IPِ کانتینر عوض می‌شود)؛ اشتراکِ تمام‌شده/اپِ خاموش/اپِ حذف‌شده ⇒ نگاشت برداشته می‌شود.
    خطایِ تشخیصِ پورت/IP برایِ یک اپ، نگاشتِ قبلیِ همان اپ را دست نمی‌زند."""
    now = now or dt.datetime.now()
    changed = removed = 0
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp).where(HostedApp.miniapp_sub.is_not(None)))).all()
    known = {a.name for a in apps}
    for a in apps:
        try:
            if is_active(a, now) and a.status == "running":
                port = await dm.detect_listen_port(a.name) or dm.guess_internal_port(await _env_dict(a.id))
                ip = await dm.container_ip(a.name)
                if ip and port:
                    changed += int(write_map(a.name, a.miniapp_sub, f"{ip}:{int(port)}"))
            else:
                removed += int(remove_map(a.name))
        except Exception:
            logger.exception("miniapp sync: اپِ %s رد شد", a.name)
    try:
        for f in MAP_DIR.glob("*.map"):
            if f.stem not in known:
                f.unlink()
                removed += 1
    except OSError:
        logger.exception("miniapp sync: پاک‌سازیِ فایل‌هایِ یتیم ناموفق")
    err = None
    if changed or removed:
        err = await reload_nginx(run)
        if err:
            logger.error("miniapp sync: %s", err)
    return {"changed": changed, "removed": removed, "error": err}
