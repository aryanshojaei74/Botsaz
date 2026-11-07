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
import posixpath
import re
import secrets
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from config import Session
from models import HostedApp, HostedAppEnv, PlatformTxn, PlatformUser, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

DOMAIN = os.getenv("MINIAPP_DOMAIN", "supraplus.ir").strip().lower()
MAP_DIR = Path(os.getenv("MINIAPP_MAP_DIR", "/etc/nginx/miniapp-hosts.d"))
# === حالتِ «فقط فایلِ استاتیک» ===
# کاربر zipِ HTML/JS (خروجیِ build) را آپلود می‌کند؛ بدونِ کانتینر. nginx این ساب‌دامین‌ها را به خودِ سرویسِ botsazvip
# (STATIC_UPSTREAM) می‌فرستد و middleware (serve_static) فایل را از STATIC_ROOT/<نامِ اپ>/ سرو می‌کند.
STATIC_ROOT = Path(os.getenv("MINIAPP_STATIC_DIR", "/opt/botsazvip/miniapps"))
STATIC_UPSTREAM = os.getenv("MINIAPP_STATIC_UPSTREAM", "127.0.0.1:8002")
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_UNZIPPED_BYTES = 60 * 1024 * 1024
MAX_FILES = 2000
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
            if is_active(a, now) and has_static(a.name):
                changed += int(write_map(a.name, a.miniapp_sub, STATIC_UPSTREAM))     # استاتیک: کانتینر لازم نیست
            elif is_active(a, now) and a.status == "running":
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


# ───────────────────────── حالتِ استاتیک: آپلود و سرو ─────────────────────────
def static_dir(app_name: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,29}", app_name or ""):
        raise ValueError("نامِ اپ نامعتبر")
    return STATIC_ROOT / app_name


def has_static(app_name: str) -> bool:
    try:
        return (static_dir(app_name) / "index.html").is_file()
    except ValueError:
        return False


def _safe_member(name: str) -> str | None:
    """نامِ امنِ نسبیِ داخلِ zip یا None (مسیرِ مطلق، .. ، بک‌اسلش، فایلِ مخفی)."""
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        return None
    norm = posixpath.normpath(name)
    parts = norm.split("/")
    if norm.startswith("..") or ".." in parts:
        return None
    if any(p.startswith(".") and p != ".well-known" for p in parts):      # .git, .env و ... هرگز
        return None
    if any(p.lower() in ("__macosx", "node_modules") for p in parts):
        return None
    return norm


def install_static(app_name: str, zip_path: str | os.PathLike) -> Result:
    """zip را امن باز می‌کند و جایگزینِ نسخهٔ قبلی می‌کند. باید index.html (در ریشه یا داخلِ یک پوشهٔ ریشه) داشته باشد."""
    zp = Path(zip_path)
    try:
        if zp.stat().st_size > MAX_ZIP_BYTES:
            return Result(False, f"حجمِ zip بیشتر از {MAX_ZIP_BYTES // (1024 * 1024)} مگابایت است.")
        zf = zipfile.ZipFile(zp)
    except (OSError, zipfile.BadZipFile):
        return Result(False, "فایل zip معتبر نیست.")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            return Result(False, "zip خالی است.")
        if len(infos) > MAX_FILES:
            return Result(False, f"تعدادِ فایل‌ها بیشتر از {MAX_FILES} است.")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
            return Result(False, f"حجمِ بازشده بیشتر از {MAX_UNZIPPED_BYTES // (1024 * 1024)} مگابایت است.")
        names = {}
        for i in infos:
            mode = (i.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                return Result(False, "zip نباید لینکِ نمادین (symlink) داشته باشد.")
            n = _safe_member(i.filename)
            if n is None:
                continue                                   # فایل‌هایِ مخفی/خطرناک بی‌صدا رد می‌شوند؛ مسیرِ مخرب (..) هم
            if ".." in i.filename.replace("\\", "/").split("/"):
                return Result(False, "zip مسیرِ نامعتبر (..) دارد.")
            names[i] = n
        if not names:
            return Result(False, "فایلِ قابل‌استفاده‌ای در zip نیست.")
        # اگر همهٔ فایل‌ها داخلِ یک پوشهٔ ریشه‌اند، همان پوشه برداشته می‌شود
        tops = {n.split("/")[0] for n in names.values() if "/" in n}
        flat = [n for n in names.values() if "/" not in n]
        strip = ""
        if not flat and len(tops) == 1:
            strip = next(iter(tops)) + "/"
        rel = {i: (n[len(strip):] if strip and n.startswith(strip) else n) for i, n in names.items()}
        if "index.html" not in rel.values():
            return Result(False, "فایلِ index.html در ریشهٔ zip (یا داخلِ پوشهٔ اصلی) پیدا نشد.")
        dest = static_dir(app_name)
        STATIC_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{app_name}-", dir=STATIC_ROOT))
        try:
            for i, r in rel.items():
                target = (tmp / r).resolve()
                if tmp.resolve() not in target.parents:
                    return Result(False, "zip مسیرِ نامعتبر دارد.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(i) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            old = None
            if dest.exists():
                old = STATIC_ROOT / f".{app_name}.old-{int(time.time())}"
                os.replace(dest, old)
            os.replace(tmp, dest)
            if old:
                shutil.rmtree(old, ignore_errors=True)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return Result(False, f"نوشتنِ فایل‌ها ناموفق بود: {e}")
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
    _HOST_CACHE.clear()
    return Result(True, f"{len(rel)} فایل بارگذاری شد.")


def remove_static(app_name: str) -> bool:
    d = static_dir(app_name)
    existed = d.exists()
    shutil.rmtree(d, ignore_errors=True)
    _HOST_CACHE.clear()
    return existed


def resolve_static(app_name: str, rel_path: str) -> Path | None:
    """مسیرِ فایل (ضدِ path-traversal). مسیرِ بدونِ پسوند (روتِ SPA) → index.html."""
    root = static_dir(app_name).resolve()
    p = posixpath.normpath("/" + (rel_path or "").lstrip("/")).lstrip("/")
    if any(seg.startswith(".") and seg != ".well-known" for seg in p.split("/") if seg):
        return None
    cand = (root / p).resolve() if p else root
    if root != cand and root not in cand.parents:
        return None
    if cand.is_dir():
        cand = cand / "index.html"
    if cand.is_file():
        return cand
    if "." not in posixpath.basename(p):                       # مسیرِ SPA (/profile) → index.html
        idx = root / "index.html"
        return idx if idx.is_file() else None
    return None


_HOST_CACHE: dict[str, tuple[float, str | None]] = {}       # host -> (زمان، نامِ اپ فعال یا None)
_HOST_TTL = 20.0


async def _app_for_host(host: str) -> str | None:
    suffix = "." + DOMAIN
    host = (host or "").split(":")[0].lower()
    if not host.endswith(suffix):
        return None
    sub = host[: -len(suffix)]
    if not _SUB_RE.match(sub):
        return None
    now = time.monotonic()
    hit = _HOST_CACHE.get(host)
    if hit and now - hit[0] < _HOST_TTL:
        return hit[1]
    async with Session() as s:
        a = await s.scalar(select(HostedApp).where(HostedApp.miniapp_sub == sub))
    name = a.name if (a is not None and is_active(a) and has_static(a.name)) else None
    _HOST_CACHE[host] = (now, name)
    return name


async def serve_static(host: str, path: str):
    """پاسخِ FastAPI برایِ درخواستِ یک ساب‌دامینِ مینی‌اپِ استاتیک؛ None اگر این host مالِ ما نیست."""
    from fastapi.responses import FileResponse, PlainTextResponse
    suffix = "." + DOMAIN
    if not (host or "").split(":")[0].lower().endswith(suffix):
        return None
    name = await _app_for_host(host)
    if not name:
        return PlainTextResponse("Not found", status_code=404)
    f = resolve_static(name, path)
    if f is None:
        return PlainTextResponse("Not found", status_code=404)
    html = f.suffix.lower() in (".html", ".htm")
    return FileResponse(str(f), headers={
        "Cache-Control": "no-cache" if html else "public, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
