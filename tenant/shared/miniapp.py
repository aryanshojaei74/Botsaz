"""
miniapp.py — «مینی‌اپ» فروشگاهیِ هر ربات (Telegram WebApp).

چهار تب: خانه / خرید / سرویس‌ها / پروفایل — دقیقاً معادلِ منویِ زیرینِ
طراحی‌شده. داده‌ها زنده از دیتابیسِ همان ربات خوانده می‌شوند؛ خودِ خرید
داخلِ این صفحه انجام نمی‌شود — با زدنِ «خرید این پلن» کاربر با یک
دیپ‌لینک به چتِ خودِ ربات برمی‌گردد و همان جریانِ پرداختِ همیشگی
(کیف‌پول/کارت‌به‌کارت/تخفیف و...) که در tenant/vpn_user.py هست اجرا
می‌شود — اینجا چیزی از منطقِ پرداخت را بازسازی نمی‌کنیم.

احرازِ هویت: Telegram WebApp initData — امضا با HMAC-SHA256 و کلیدِ
مشتق‌شده از توکنِ خودِ همان ربات (طبق مستندِ رسمیِ تلگرام) بررسی می‌شود؛
یعنی کاربر فقط با initData معتبرِ همان ربات به داده‌های همان ربات دسترسی
دارد و امکانِ خواندنِ اطلاعاتِ رباتِ دیگری وجود ندارد.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import html as _html
import json
import os
import pathlib
import secrets
import time
import json as _json  # PATCH_MINIAPP_TERMS_V1
from urllib.parse import parse_qsl

from fastapi import APIRouter, Body, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func as sa_func

from config import Session
from models import (
    Bot as BotRow, BotUser, MiniAppCategory, PanelAccount, VpnPanel, VpnPlan,
    Order, WalletTxn, MainButton, ReferralLink, Setting,
    Config,  # === PATCH_MINIAPP_STOCK_CATS_V1 ===
    MiniAppProduct, MiniAppProductOrder,  # PATCH_MINIAPP_MANUAL_PRODUCTS_V1
    order_no,  # شناسهٔ واحدِ سفارش
)
from tenant.shared.web_view import ensure_web_token, _panel_for_account

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

miniapp_router = APIRouter()

_SECRET_CACHE = ""
_INITDATA_MAX_AGE = 24 * 3600  # ثانیه — initData قدیمی‌تر از این رد می‌شود


# ───────────────────────── احراز هویت ─────────────────────────

def _check_init_data(init_data: str, bot_token: str) -> dict | None:
    """initData را طبقِ الگوریتمِ رسمیِ تلگرام اعتبارسنجی می‌کند؛
    در صورتِ موفقیت دیکشنریِ پارس‌شده (شاملِ user) را برمی‌گرداند."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > _INITDATA_MAX_AGE:
                return None
        except ValueError:
            pass
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    out = dict(pairs)
    if "user" in out:
        try:
            out["user"] = json.loads(out["user"])
        except (json.JSONDecodeError, TypeError):
            out["user"] = None
    return out



# === PATCH_MINIAPP_KB_DIRECT_V1 ===
# مینی‌اپی که مستقیم از دکمهٔ کیبورد باز شود، طبقِ مستنداتِ رسمیِ تلگرام
# initData نمی‌گیرد (خالی است) — پس راهِ همیشگیِ احرازِ هویت کار نمی‌کند.
# راهکار: چون کیبورد برای هر کاربر جداگانه ساخته و فرستاده می‌شود، همان
# موقعِ ساختِ کیبورد یک توکنِ امضاشدهٔ مخصوصِ همان کاربر داخلِ آدرسِ وب‌اپ
# می‌گذاریم و مینی‌اپ در نبودِ initData از آن استفاده می‌کند.
_KB_TOKEN_MAX_AGE = 7 * 24 * 3600  # ثانیه


def _server_secret() -> str:
    """کلیدِ امضایِ سمتِ سرور.

    عمداً از توکنِ خودِ رباتِ مستأجر استفاده *نمی‌شود*: صاحبِ ربات توکنِ
    رباتش را دارد، پس می‌توانست برایِ هر کاربرِ دلخواه توکن بسازد و به‌جای
    او واردِ مینی‌اپ شود. این کلید فقط رویِ سرورِ ما وجود دارد.
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    env = os.environ.get("MINIAPP_SECRET", "").strip()
    if env:
        _SECRET_CACHE = env
        return _SECRET_CACHE
    path = pathlib.Path(__file__).resolve().parent.parent / ".miniapp_secret"
    try:
        if path.exists():
            _SECRET_CACHE = path.read_text().strip()
        if not _SECRET_CACHE:
            _SECRET_CACHE = secrets.token_urlsafe(48)
            path.write_text(_SECRET_CACHE)
            try:
                path.chmod(0o600)
            except OSError:
                logger.warning("تنظیمِ دسترسیِ فایلِ کلیدِ مینی‌اپ ناموفق بود")
    except OSError:
        # اگر نوشتن ممکن نبود، یک کلیدِ درون‌حافظه‌ای می‌سازیم؛ با هر
        # ری‌استارت عوض می‌شود و کاربران باید دوباره /start بزنند.
        logger.exception("خواندن/نوشتنِ فایلِ کلیدِ مینی‌اپ ناموفق بود")
        _SECRET_CACHE = secrets.token_urlsafe(48)
    return _SECRET_CACHE


def make_kb_token(bot_token: str, tg_id: int, issued_at: int | None = None,
                  bot_id: int | None = None) -> str:
    """توکنِ «tgid.timestamp.hmac». امضا با کلیدِ سرور — نه توکنِ ربات."""
    ts = int(issued_at if issued_at is not None else time.time())
    payload = f"{int(tg_id)}.{ts}"
    scope = f"MiniAppKB:{bot_id if bot_id is not None else bot_token}"
    sig = hmac.new(_server_secret().encode(), f"{scope}|{payload}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _check_kb_token(token: str, bot_token: str, bot_id: int | None = None) -> int | None:
    """در صورتِ اعتبار، آی‌دیِ تلگرامِ کاربر را برمی‌گرداند."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    tg_raw, ts_raw, sig = parts
    try:
        tg_id, ts = int(tg_raw), int(ts_raw)
    except ValueError:
        return None
    if time.time() - ts > _KB_TOKEN_MAX_AGE:
        return None
    scope = f"MiniAppKB:{bot_id if bot_id is not None else bot_token}"
    expected = hmac.new(_server_secret().encode(), f"{scope}|{tg_id}.{ts}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    return tg_id


async def _authed_context(bot_id: int, x_init_data: str | None,
                          x_kb_token: str | None = None):
    """رباتِ فعال + کاربرِ نهاییِ احرازشده را برمی‌گرداند، یا HTTPException می‌زند."""
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        if not row or not row.miniapp_enabled:
            raise HTTPException(status_code=404, detail="miniapp_disabled")
        parsed = _check_init_data(x_init_data or "", row.token)
        if parsed and parsed.get("user"):
            tg_user = parsed["user"]
            tg_id = int(tg_user["id"])
        else:
            # === PATCH_MINIAPP_KB_DIRECT_V1 === مسیرِ دکمهٔ کیبورد
            tg_id = _check_kb_token(x_kb_token or "", row.token, bot_id)
            if not tg_id:
                raise HTTPException(status_code=401, detail="invalid_init_data")
            tg_user = {"id": tg_id}
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        if not u:
            u = BotUser(bot_id=bot_id, telegram_id=tg_id,
                        full_name=(tg_user.get("first_name", "") + (
                            (" " + tg_user["last_name"]) if tg_user.get("last_name") else "")
                            ) or str(tg_id),
                        username=tg_user.get("username") or "")
            s.add(u)
            await s.commit()
            await s.refresh(u)
        return row, u


def _rial(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


# ───────────────────────── تمِ رنگیِ مینی‌اپ ─────────────────────────
# === PATCH_MINIAPP_THEME_V1 ===
# درخواستِ ادمین: مینی‌اپ قابلِ‌شخصی‌سازی باشه (رنگِ دکمه‌ها/تأکید)،
# پیش‌فرضش هم قرمز باشه (قبلاً نارنجی بود، ثابت و غیرقابلِ‌تغییر).
# چون کلِ CSS از قبل با متغیرهایِ --accent/--accent-2/--accent-dim نوشته
# شده بود، فقط کافی بود این سه‌تا رو placeholder کنیم؛ نیازی به بازنویسیِ
# صدها خطِ CSS نبود.
_DEFAULT_ACCENT = "#ef4444"  # قرمز — پیش‌فرضِ جدید


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return 239, 68, 68  # قرمزِ پیش‌فرض در صورتِ هگزِ نامعتبر


def _lighten(h: str, amount: float = 0.25) -> str:
    r, g, b = _hex_to_rgb(h)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _darken(h: str, amount: float = 0.06) -> str:
    """=== PATCH_MINIAPP_SECONDARY_COLOR_V1 ===
    _lighten با مقدارِ منفی روی رنگ‌هایِ روشن بی‌اثر است (برایِ سفید
    (255-255)*x همیشه صفر می‌شود)، پس تیره‌کردن تابعِ خودش را می‌خواهد.
    """
    r, g, b = _hex_to_rgb(h)
    f = max(0.0, 1.0 - amount)
    return f"#{int(r * f):02x}{int(g * f):02x}{int(b * f):02x}"


def _rgba(h: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(h)
    return f"rgba({r},{g},{b},{alpha})"


# === PATCH_MINIAPP_BG_THEME_V1 ===
async def _miniapp_bg(bot_id: int) -> str:
    from tenant import get_setting
    color = (await get_setting(bot_id, "miniapp_bg_color", "")).strip()
    if color.startswith("#") and len(color) in (4, 7):
        return color
    return ""


def _is_dark(hex_color: str) -> bool:
    r, g, b = _hex_to_rgb(hex_color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 140


async def _miniapp_card(bot_id: int) -> str:
    """=== PATCH_MINIAPP_SECONDARY_COLOR_V1 ===
    رنگِ «ثانویه»: پس‌زمینهٔ کارت‌ها و باکس‌ها. تا امروز همیشه از رویِ رنگِ
    پس‌زمینه ساخته می‌شد (کمی روشن‌ترش)، پس فروشنده نمی‌توانست مثلاً
    پس‌زمینه را تیره و کارت‌ها را مشکیِ خالص کند. خالی = همان رفتارِ
    خودکارِ قبلی.
    """
    from tenant import get_setting
    color = (await get_setting(bot_id, "miniapp_card_color", "")).strip()
    if color.startswith("#") and len(color) in (4, 7):
        return color
    return ""


def _theme_css(bg: str, card_override: str = "") -> str:
    # === PATCH_MINIAPP_SECONDARY_COLOR_V1 ===
    # رنگِ ثانویه می‌تواند بدونِ تعیینِ پس‌زمینه هم ست شود، پس دیگر زودهنگام
    # برنمی‌گردیم؛ اگر bg نباشد از پیش‌فرضِ تمِ تیره استفاده می‌کنیم.
    if not bg and not card_override:
        return ""
    base = bg or "#000000"
    dark = _is_dark(base)
    if card_override:
        card = card_override
        # کارتِ تیره کمی روشن‌تر، کارتِ روشن کمی تیره‌تر — تا لایهٔ دوم
        # همیشه از لایهٔ اول تفکیک شود.
        card2 = (_lighten(card_override, 0.06) if _is_dark(card_override)
                 else _darken(card_override, 0.04))
    else:
        card = _lighten(base, 0.07) if dark else "#ffffff"
        card2 = _lighten(base, 0.11) if dark else "#fbfbfc"
    line = "rgba(255,255,255,.08)" if dark else "rgba(10,10,20,.08)"
    ink = "#f4f5f7" if dark else "#111114"
    sub = "#9a9aa4" if dark else "#6c6c75"
    sub2 = "#68686f" if dark else "#9a9aa2"
    # اگر فقط رنگِ ثانویه ست شده باشد، پس‌زمینه دست‌نخورده می‌ماند
    _bg_part = f"--bg:{bg}; " if bg else ""
    block = (f"{_bg_part}--card:{card}; --card-2:{card2}; --line:{line};"
             f" --ink:{ink}; --sub:{sub}; --sub2:{sub2};")
    return ("\n/* PATCH_MINIAPP_BG_THEME_V1 */\n"
            f":root{{ {block} }}\n"
            f"body.light{{ {block} }}\n"
            f"body, body.light {{ background: var(--bg); color: var(--ink); }}\n")


async def _miniapp_accent(bot_id: int) -> str:
    from tenant import get_setting
    color = (await get_setting(bot_id, "miniapp_accent_color", "")).strip()
    if color and color.startswith("#") and len(color) in (4, 7):
        return color
    return _DEFAULT_ACCENT


# ───────────────────────── اسکریپتِ تلگرام (سلف‌هاست) ─────────────────────────
# === PATCH_MINIAPP_SELF_HOST_TG_JS_V1 ===
_TG_JS_URL = "https://telegram.org/js/telegram-web-app.js"
_TG_JS_CACHE = {"ts": 0.0, "body": b""}
_TG_JS_TTL = 6 * 3600
_TG_JS_FILE = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "_tg_webapp_cache.js"
_TG_JS_FALLBACK = (b"/* fallback: loading from telegram.org */\n"
                   b"document.write('<script src=\"https://telegram.org/js/telegram-web-app.js\"><\\/script>');")


async def _fetch_tg_js() -> bytes:
    import aiohttp
    t = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=t) as sess:
        async with sess.get(_TG_JS_URL, headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            body = await r.read()
    if len(body) < 2000 or b"Telegram" not in body:
        raise RuntimeError("پاسخِ نامعتبر")
    return body


@miniapp_router.get("/miniapp/_assets/tgwebapp.js")
async def miniapp_tg_js():
    from fastapi.responses import Response
    now = time.time()
    body = _TG_JS_CACHE["body"]
    if not body or now - _TG_JS_CACHE["ts"] > _TG_JS_TTL:
        try:
            body = await _fetch_tg_js()
            _TG_JS_CACHE.update(ts=now, body=body)
            try:
                _TG_JS_FILE.write_bytes(body)
            except Exception:
                logger.debug("نوشتنِ کشِ دیسکیِ اسکریپتِ تلگرام ممکن نشد (بی‌اهمیت)")
        except Exception as e:  # noqa: BLE001
            logger.warning("miniapp: دریافتِ telegram-web-app.js ناموفق: %s", e)
            if not body:
                try:
                    body = _TG_JS_FILE.read_bytes()
                    _TG_JS_CACHE.update(ts=now - _TG_JS_TTL + 300, body=body)  # ۵ دقیقه بعد دوباره تلاش
                except Exception:
                    body = _TG_JS_FALLBACK
            else:
                _TG_JS_CACHE["ts"] = now - _TG_JS_TTL + 300
    return Response(content=body, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})


# ───────────────────────── صفحه (SPA) ─────────────────────────

@miniapp_router.get("/miniapp/{bot_id}", response_class=HTMLResponse)
async def miniapp_page(bot_id: int):
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        platform_row = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "miniapp_feature_enabled"))
    platform_on = (platform_row.value if (platform_row and platform_row.value is not None) else "on") != "off"
    if not row or not row.miniapp_enabled or not platform_on:
        return HTMLResponse(content=_DISABLED_HTML, status_code=404,
                             headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                                      "Pragma": "no-cache", "Expires": "0"})
    bot_username = row.username or ""
    # === PATCH_MINIAPP_THEME_V1 ===
    accent = await _miniapp_accent(bot_id)
    # === PATCH_MINIAPP_TERMS_V1 ===
    # واژگانِ رابطِ کاربری بر اساسِ نوعِ ربات. در رباتِ خدماتِ مجازی چیزی که
    # فروخته می‌شود «اشتراک/سرویس» نیست، «محصول» است — درخواستِ مستأجر.
    # فروشِ VPN همان واژگانِ قبلی را نگه می‌دارد.
    _vs = (row.field or "") == "virtual_service"
    _terms = {
        "item":  "محصول" if _vs else "سرویس",
        "items": "محصولات" if _vs else "سرویس‌ها",
        "mine":  "محصولات من" if _vs else "اشتراک‌های من",
        "buy":   "خرید محصولات" if _vs else "خرید اشتراک",
        "tab":   "محصولات" if _vs else "اشتراک‌ها",
        "vs":    bool(_vs),   # PATCH_MINIAPP_VS_NO_SUBS_V1
    }
    html = (_PAGE_HTML.replace("__TERMS__", _json.dumps(_terms, ensure_ascii=False))
            .replace("__BOT_ID__", str(bot_id))
            .replace("__BOT_TITLE__", _html.escape(bot_username or "ربات"))
            .replace("__BOT_USERNAME__", _html.escape(("@" + bot_username) if bot_username else "ربات"))
            .replace("__BOT_LINK__", _html.escape(("https://t.me/" + bot_username) if bot_username else "#"))
            .replace("__ACCENT__", accent)
            .replace("__ACCENT_2__", _lighten(accent, 0.22))
            .replace("__ACCENT_DIM__", _rgba(accent, 0.16))
            .replace("__ACCENT_GLOW__", _rgba(accent, 0.32))
            .replace("__BUILD_ID__", str(int(time.time()))))
    # === PATCH_MINIAPP_BG_THEME_V1 ===
    _css = _theme_css(await _miniapp_bg(bot_id), await _miniapp_card(bot_id))
    if _css:
        html = html.replace("</style>", _css + "</style>", 1)
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0",
    })


# ───────────────────────── API ─────────────────────────

_ORDER_STATUS_FA = {
    "paid": ("موفق", False),
    "awaiting_admin": ("در حال بررسی", False),
    "awaiting_receipt": ("در انتظار رسید", False),
    "pending": ("در انتظار پرداخت", False),
    "rejected": ("ناموفق", True),
}


async def _ref_reward_info(bot_id: int) -> dict:
    """پاداشِ دعوت دقیقاً مثلِ خودِ ربات: ثابت/درصدی، و اینکه کی پرداخت می‌شود."""
    from tenant import get_setting
    enabled = (await get_setting(bot_id, "referral_reward_enabled", "1")) == "1"
    rtype = await get_setting(bot_id, "referral_reward_type", "fixed")
    pct = await get_setting(bot_id, "referral_reward_pct", "5")
    mode = await get_setting(bot_id, "referral_reward_mode", "join")
    try:
        amt = int(float(await get_setting(bot_id, "referral_reward", "0") or 0))
    except (TypeError, ValueError):
        amt = 0
    if not enabled:
        return {"reward": "—", "reward_label": "پاداش", "reward_note": "پاداش دعوت فعلاً غیرفعال است."}
    if rtype == "percent_all":
        return {"reward": f"{pct}٪", "reward_label": "از هر خریدِ دوستت",
                "reward_note": "مادام‌العمر؛ از تمامِ خریدهایِ دوستانت سهم می‌گیری."}
    if rtype == "percent_first":
        return {"reward": f"{pct}٪", "reward_label": "از اولین خریدِ دوستت",
                "reward_note": "بعد از اولین خریدِ موفقِ دوستت پرداخت می‌شود."}
    note = ("به‌محضِ ورودِ دوستت از لینک، به کیف‌پولت اضافه می‌شود." if mode == "join"
            else "بعد از اولین خریدِ موفقِ دوستت پرداخت می‌شود (نه با ورود).")
    return {"reward": _rial(amt), "reward_label": "پاداش هر نفر (ت)", "reward_note": note}


async def _wheel_flag(bot_id: int) -> dict:
    from tenant.shared.miniapp_wheel import public_flag
    return await public_flag(bot_id)


@miniapp_router.get("/api/miniapp/{bot_id}/home")
async def api_home(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    _is_vs = (row.field or "") == "virtual_service"   # PATCH_MINIAPP_VS_NO_SUBS_V1
    async with Session() as s:
        total_orders = await s.scalar(select(sa_func.count(Order.id)).where(
            Order.bot_id == bot_id, Order.user_id == u.id, Order.status == "paid"))
        total_spent = await s.scalar(select(sa_func.coalesce(sa_func.sum(Order.amount), 0)).where(
            Order.bot_id == bot_id, Order.user_id == u.id, Order.status == "paid"))
        active_services = await s.scalar(select(sa_func.count(PanelAccount.id)).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == u.telegram_id))

        # پشتیبانی: فقط اگر مستأجر حداقل یک دکمهٔ فعال با پشتیبانی روشن تعریف کرده باشد
        support_row = (await s.execute(select(MainButton.support_contact).where(
            MainButton.bot_id == bot_id, MainButton.support_btn == True,
            MainButton.support_contact.isnot(None)
        ).limit(1))).first()
        support_enabled = bool(support_row and support_row[0])
        support_contact = support_row[0] if support_row else None

        # لینک دعوت دوستان (get-or-create)
        ref = await s.scalar(select(ReferralLink).where(
            ReferralLink.bot_id == bot_id, ReferralLink.user_id == u.id))
        if not ref:
            for _ in range(5):
                code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
                clash = await s.scalar(select(ReferralLink.id).where(ReferralLink.code == code))
                if not clash:
                    ref = ReferralLink(bot_id=bot_id, user_id=u.id, code=code)
                    s.add(ref)
                    await s.flush()
                    break
        ref_code = ref.code if ref else None

        recent_orders = (await s.execute(select(Order).where(
            Order.bot_id == bot_id, Order.user_id == u.id
        ).order_by(Order.id.desc()).limit(6))).scalars().all()
        await s.commit()

    txns = []
    for o in recent_orders:
        label, failed = _ORDER_STATUS_FA.get(o.status, (o.status, False))
        txns.append({
            "title": "خرید محصول" if _is_vs else "خرید اشتراک",
            "amount": _rial(o.amount),
            "status": label,
            "failed": failed,
            "date": o.created_at.strftime("%Y/%m/%d") if getattr(o, "created_at", None) else "",
        })

    support_link = None
    if support_contact:
        c = support_contact.strip()
        if c.startswith("http://") or c.startswith("https://") or c.startswith("t.me/"):
            support_link = c if "://" in c else "https://" + c
        elif c.startswith("@"):
            support_link = "https://t.me/" + c[1:]
        else:
            support_link = "https://t.me/" + c

    referral_link = f"https://t.me/{row.username}?start=ref_{ref_code}" if (row.username and ref_code) else None
    referral_info = {
        "link": referral_link,
        "clicks": int(ref.click_count or 0) if ref else 0,
        "joins": int(ref.join_count or 0) if ref else 0,
        "reward": _rial(ref.reward_per_join) if ref else "0",
    } if referral_link else None
    # === PATCH_MINIAPP_REF_REWARD_V1 === پاداشِ واقعی طبقِ تنظیماتِ ربات (نه فیلدِ قدیمیِ لینک)
    if referral_info:
        referral_info.update(await _ref_reward_info(bot_id))

    return JSONResponse({
        "ok": True,
        "user": {"name": u.full_name or "کاربر", "username": u.username or ""},
        "bot_title": row.username or "",
        "wallet_balance": _rial(u.balance),
        "total_orders": int(total_orders or 0),
        "total_spent": _rial(total_spent),
        "active_services": int(active_services or 0),
        "support_enabled": support_enabled,
        "support_link": support_link,
        "referral_link": referral_link,
        "referral": referral_info,
        "recent_txns": txns,
        "wheel": await _wheel_flag(bot_id),   # PATCH_MINIAPP_WHEEL_V1
    })



# === PATCH_MINIAPP_STOCK_CATS_V1 ===
def _stock_list(cat) -> list[str]:
    raw = (getattr(cat, "stock_categories", None) or "").strip()
    return [x for x in (p.strip() for p in raw.split(",")) if x]


async def _stock_packs(s, bot_id: int, names: list[str]) -> list[dict]:
    """بسته‌هایِ آمادهٔ انبار را (مثلِ خودِ ربات) بر اساسِ حجم/روز/کاربر/قیمت
    گروه می‌کند و موجودیِ هر گروه را برمی‌گرداند."""
    if not names:
        return []
    q = select(Config).where(
        Config.bot_id == bot_id, Config.status == "in_stock", Config.price > 0)
    real = [n for n in names if n != "-"]
    conds = []
    if real:
        conds.append(Config.category.in_(real))
    if "-" in names:
        conds.append(Config.category.is_(None))
    if len(conds) == 1:
        q = q.where(conds[0])
    else:
        from sqlalchemy import or_ as _or
        q = q.where(_or(*conds))
    cfgs = (await s.scalars(q)).all()
    groups: dict[tuple, int] = {}
    for c in cfgs:
        key = (c.volume_gb, c.days, c.user_count or 0, int(c.price))
        groups[key] = groups.get(key, 0) + 1
    out = []
    for (vol, days, users, price), stock in sorted(groups.items()):
        out.append({
            "pack": f"{vol}_{days}_{users}_{price}",
            "title": ("♾️ نامحدود" if vol == 0 else f"{vol} گیگ") + f" · {days} روز",
            "volume_gb": vol,
            "days": days,
            "price": _rial(price),
            "unlimited_volume": vol == 0,
            "unlimited_days": days == 0,
            "limit_ip": users,
            "stock": stock,
        })
    return out


@miniapp_router.get("/api/miniapp/{bot_id}/categories")
async def api_categories(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        cats = (await s.scalars(select(MiniAppCategory).where(
            MiniAppCategory.bot_id == bot_id, MiniAppCategory.active == True
        ).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all()
        uncategorized = await s.scalar(select(sa_func.count(VpnPlan.id)).where(
            VpnPlan.bot_id == bot_id, VpnPlan.active == True,
            VpnPlan.miniapp_category_id.is_(None)))
        # === PATCH_HIDE_ALLSERVERS_TOGGLE_FIX_V1 ===
        # باگ: پایین‌تر «همه سرورها» فقط وقتی اضافه می‌شد که uncategorized
        # صفر نباشد — یعنی اگر مالک همهٔ پلن‌ها را دسته‌بندی کرده بود،
        # دکمهٔ «نمایشش بده» در پنل هیچ اثری نداشت، چون آن شرط همیشه رد
        # می‌شد، مستقل از این‌که ادمین چه چیزی زده. حالا اول می‌بینیم آیا
        # ادمین اصلاً دستی این تنظیم را لمس کرده (ردیفِ Setting وجود دارد)
        # یا نه؛ اگر دستی تنظیم کرده، انتخابش را بدونِ قید‌وشرط اجرا می‌کنیم.
        hide_setting = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "miniapp_hide_all"))
    out = [{"id": c.id, "title": c.title, "icon": c.icon or "🛰"} for c in cats]
    if hide_setting is not None:
        show_all_servers = hide_setting.value != "1"
    else:
        # هرگز دستی تنظیم نشده — رفتارِ قبلی: فقط وقتی چیزی برایِ نمایش
        # هست (پلنِ بی‌دسته) خودکار نشانش بده، وگرنه دکمهٔ گیج‌کننده‌ای
        # اضافه نکن.
        show_all_servers = bool(uncategorized)
    if show_all_servers:
        out.append({"id": 0, "title": "همه سرورها", "icon": "🌐"})
    return JSONResponse({"ok": True, "categories": out})


@miniapp_router.get("/api/miniapp/{bot_id}/plans/{category_id}")
async def api_plans(bot_id: int, category_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        q = select(VpnPlan).where(VpnPlan.bot_id == bot_id, VpnPlan.active == True)
        q = q.where(VpnPlan.miniapp_category_id.is_(None)) if category_id == 0 \
            else q.where(VpnPlan.miniapp_category_id == category_id)
        plans = (await s.scalars(q.order_by(VpnPlan.id))).all()
        # === PATCH_MINIAPP_STOCK_CATS_V1 ===
        packs = []
        if category_id:
            _cat = await s.get(MiniAppCategory, category_id)
            if _cat and _cat.bot_id == bot_id:
                packs = await _stock_packs(s, bot_id, _stock_list(_cat))
    out = [{
        "id": p.id,
        "title": p.title,
        "volume_gb": p.volume_gb,
        "days": p.days,
        "price": _rial(p.price),
        "unlimited_volume": p.volume_gb == 0,
        "unlimited_days": p.days == 0,
        "limit_ip": p.limit_ip,
    } for p in plans]
    # === PATCH_MINIAPP_VC_TRIAL_V1 ===
    # اگر برایِ پنلِ پشتِ یکی از پلن‌هایِ همین دسته «حجم/روز دلخواه» فعال
    # باشد، یک کارتِ +/− هم نشان می‌دهیم. قیمت‌گذاری اینجا بازسازی نمی‌شود؛
    # فقط از همان تابعِ خودِ ربات (_vc_all_for_user) برای تشخیصِ فعال‌بودن
    # استفاده می‌شود.
    custom = []
    seen_panels = {p.panel_key for p in plans if p.panel_key}
    if seen_panels:
        from tenant.bots.vpn.vpn_user import _vc_all_for_user
        from tenant.bots.vpn.vpn_admin import panel_label
        for pk in sorted(seen_panels):
            try:
                cfg = await _vc_all_for_user(bot_id, u.telegram_id, pk)
            except Exception:
                logger.exception("خطا در خواندنِ تنظیماتِ حجم دلخواه برایِ پنلِ %s", pk)
                continue
            if cfg.get("vc_enabled") == "1":
                try:
                    label = await panel_label(bot_id, pk)
                except Exception:
                    label = None
                custom.append({"panel_key": pk, "label": label or pk})
    return JSONResponse({"ok": True, "plans": out, "packs": packs, "custom": custom})


# === PATCH_MINIAPP_VC_TRIAL_V1 ===
@miniapp_router.get("/api/miniapp/{bot_id}/vc/{panel_key}")
async def api_vc_quote(bot_id: int, panel_key: str,
                        gb: int | None = None, days: int | None = None, count: int | None = None,
                        x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    """پیش‌نمایشِ زندهٔ خریدِ حجم/روز دلخواه (+/−) برای مینی‌اپ. عمداً از همان
    تابعِ قیمت‌گذاریِ خودِ ربات (tenant/vpn_user.py::_vc_all_for_user) استفاده
    می‌کند تا منطقِ تعرفه‌هایِ پلکانی/تخفیفِ نفراتِ برتر/همکار اینجا دوباره
    نوشته نشود — مینی‌اپ فقط همان عددها را می‌خواند."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _vc_gb_cost
    cfg = await _vc_all_for_user(bot_id, u.telegram_id, panel_key, days=days, gb=gb)
    if cfg.get("vc_enabled") != "1":
        raise HTTPException(status_code=404, detail="vc_disabled")
    mn_gb, mx_gb = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
    mn_d, mx_d = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
    step_gb = int(cfg["vc_step_gb"]) or 1
    step_d = int(cfg["vc_step_days"]) or 1
    mx_c = max(1, int(cfg["vc_max_count"]))
    eff_gb = max(mn_gb, min(int(gb) if gb is not None else mn_gb, mx_gb))
    eff_days = max(mn_d, min(int(days) if days is not None else mn_d, mx_d))
    eff_count = max(1, min(int(count) if count is not None else 1, mx_c))
    # اگر با تغییرِ روز پله‌یِ حجمی عوض شده باشد، با حجمِ گِردشده دوباره بخوان
    cfg = await _vc_all_for_user(bot_id, u.telegram_id, panel_key, days=eff_days, gb=eff_gb)
    pg, pd = int(cfg["vc_price_gb"]), int(cfg["vc_price_day"])
    total = (_vc_gb_cost(cfg, eff_gb) + eff_days * pd) * eff_count
    return JSONResponse({
        "ok": True,
        "gb": eff_gb, "days": eff_days, "count": eff_count,
        "min_gb": mn_gb, "max_gb": mx_gb, "step_gb": step_gb,
        "min_days": mn_d, "max_days": mx_d, "step_days": step_d,
        "max_count": mx_c,
        "locked_gb": mn_gb == mx_gb, "locked_days": mn_d == mx_d,
        "price_gb": pg, "price_day": pd,
        "total": _rial(total),
    })


@miniapp_router.get("/api/miniapp/{bot_id}/vc/{panel_key}/buy")
async def api_vc_buy_link(bot_id: int, panel_key: str, gb: int, days: int, count: int = 1,
                          x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    """دیپ‌لینکِ خریدِ حجم/روزِ دلخواهِ انتخاب‌شده در مینی‌اپ — خودِ پرداخت،
    مثلِ همیشه، داخلِ چتِ ربات ادامه پیدا می‌کند (هندلرِ start=vcbuy_ در
    tenant/__init__.py که مستقیماً _vc_render را صدا می‌زند)."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not panel_key.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="bad_panel")
    from tenant.bots.vpn.vpn_user import _vc_all_for_user
    cfg = await _vc_all_for_user(bot_id, u.telegram_id, panel_key, days=days, gb=gb)
    if cfg.get("vc_enabled") != "1":
        raise HTTPException(status_code=404, detail="vc_disabled")
    mn_gb, mx_gb = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
    mn_d, mx_d = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
    mx_c = max(1, int(cfg["vc_max_count"]))
    gb = max(mn_gb, min(int(gb), mx_gb))
    days = max(mn_d, min(int(days), mx_d))
    count = max(1, min(int(count), mx_c))
    if not row.username:
        raise HTTPException(status_code=409, detail="bot_username_unknown")
    return JSONResponse({
        "ok": True,
        "deep_link": f"https://t.me/{row.username}?start=vcbuy_{panel_key}_{gb}_{days}_{count}"})


@miniapp_router.get("/api/miniapp/{bot_id}/trial")
async def api_trial_info(bot_id: int, x_init_data: str | None = Header(default=None),
                         x_kb_token: str | None = Header(default=None)):
    """آیا این ربات دکمهٔ «تست رایگان VPN» فعال دارد و همین کاربر هنوز
    سهمیه‌اش تمام نشده — تا مینی‌اپ فقط وقتی کارت را نشان دهد که واقعاً
    چیزی برای گرفتن هست."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        mb = await s.scalar(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.action == "free_trial"
        ).order_by(MainButton.id))
    if not mb:
        return JSONResponse({"ok": True, "available": False})
    btn_cfg = {}
    if mb.config:
        try:
            btn_cfg = json.loads(mb.config) or {}
        except Exception:
            btn_cfg = {}
    if btn_cfg.get("trial_mode"):
        from tenant import _check_button_trial_allowance
        allowed, _err = await _check_button_trial_allowance(mb.id, u.telegram_id, btn_cfg)
    else:
        from tenant import get_setting
        from models import TrialUsage
        if await get_setting(bot_id, "trial_enabled", "0") != "1":
            return JSONResponse({"ok": True, "available": False})
        async with Session() as s:
            used = await s.scalar(select(TrialUsage).where(
                TrialUsage.bot_id == bot_id, TrialUsage.user_telegram_id == u.telegram_id))
        allowed = not bool(used)
    return JSONResponse({"ok": True, "available": bool(allowed), "title": mb.label or "🎁 تست رایگان VPN"})


@miniapp_router.get("/api/miniapp/{bot_id}/trial/link")
async def api_trial_link(bot_id: int, x_init_data: str | None = Header(default=None),
                         x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        mb = await s.scalar(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.action == "free_trial"
        ).order_by(MainButton.id))
    if not mb:
        raise HTTPException(status_code=404, detail="trial_not_configured")
    if not row.username:
        raise HTTPException(status_code=409, detail="bot_username_unknown")
    return JSONResponse({"ok": True, "deep_link": f"https://t.me/{row.username}?start=trial_{mb.id}"})


@miniapp_router.get("/api/miniapp/{bot_id}/services")
async def api_services(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        accs = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == u.telegram_id
        ).order_by(PanelAccount.id.desc()))).all()
        out = []
        now = _dt.datetime.utcnow()
        for a in accs:
            expired = bool(a.expires_at and a.expires_at < now)
            token = await ensure_web_token(s, a)
            out.append({
                "id": a.id,
                "title": a.plan_title or "سرویس",
                "volume_gb": a.volume_gb,
                "days": a.days,
                "expires_at": a.expires_at.strftime("%Y-%m-%d") if a.expires_at else None,
                "expired": expired,
                "sub_link": a.sub_link or "",
                "cfg_token": token,
            })
        await s.commit()
    return JSONResponse({"ok": True, "services": out})


@miniapp_router.get("/api/miniapp/{bot_id}/profile")
async def api_profile(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    return JSONResponse({
        "ok": True,
        "name": u.full_name or "کاربر مهمان",
        "username": f"@{u.username}" if u.username else "",
        "telegram_id": u.telegram_id,
        "wallet_balance": _rial(u.balance),
        "bot_title": row.username or "",
    })


async def _get_owned_account(s, bot_id: int, u, account_id: int) -> PanelAccount:
    acc = await s.get(PanelAccount, account_id)
    if not acc or acc.bot_id != bot_id or acc.user_telegram_id != u.telegram_id:
        raise HTTPException(status_code=404, detail="not_found")
    return acc


@miniapp_router.post("/api/miniapp/{bot_id}/services/{account_id}/reset_link")
async def api_service_reset_link(bot_id: int, account_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """تولید لینک اتصال (ساب) جدید برای یک کانفیگ — لینک قبلی باطل می‌شود."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        acc = await _get_owned_account(s, bot_id, u, account_id)
        panel = await _panel_for_account(s, bot_id, acc)
        if not panel:
            raise HTTPException(status_code=409, detail="panel_not_found")
        from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
        client = make_panel_client(panel)
        try:
            # === PATCH_RESET_SUB_ROTATE_UUID_V1 === خروجی سه‌تایی شد
            _res = await client.reset_sub(acc.email)
        except Exception:
            raise HTTPException(status_code=502, detail="panel_error")
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("بستنِ کلاینتِ پنل ناموفق بود")
        if isinstance(_res, tuple):
            new_link, new_uuid, new_sub_id = _res
        else:
            new_link, new_uuid, new_sub_id = _res, None, None
        if not new_link:
            raise HTTPException(status_code=502, detail="panel_error")
        acc.sub_link = new_link
        if new_uuid:
            acc.uuid = new_uuid
        if new_sub_id:
            acc.sub_id = new_sub_id
        await s.commit()
    return JSONResponse({"ok": True, "sub_link": new_link})


@miniapp_router.post("/api/miniapp/{bot_id}/services/{account_id}/delete")
async def api_service_delete(bot_id: int, account_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """حذف کامل یک کانفیگ — هم از پنل و هم از دیتابیس."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        acc = await _get_owned_account(s, bot_id, u, account_id)
        panel = await _panel_for_account(s, bot_id, acc)
        if panel:
            from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
            client = make_panel_client(panel)
            try:
                await client.delete_account(acc.email)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            finally:
                try:
                    await client.close()
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await s.delete(acc)
        await s.commit()
    return JSONResponse({"ok": True})


@miniapp_router.get("/api/miniapp/{bot_id}/wallet/topup/init")
async def api_wallet_topup_init(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """اطلاعات لازم برای شروع شارژ کارت‌به‌کارت: وضعیت فعال‌بودن، شماره کارت،
    نام صاحب حساب، و مبالغ پیشنهادی — دقیقاً همون تنظیماتی که تو خودِ ربات استفاده می‌شه."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    from tenant import get_setting
    card_enabled = await get_setting(row.id, "card_enabled", "1") == "1"
    if not card_enabled:
        support_contact = await get_setting(row.id, "card_support_contact", "")
        return JSONResponse({
            "ok": True, "enabled": False,
            "support_link": (f"https://t.me/{support_contact.lstrip('@')}" if support_contact else None),
        })
    card_number = await get_setting(row.id, "card_number", "")
    card_holder = await get_setting(row.id, "card_holder", "")
    return JSONResponse({
        "ok": True, "enabled": True,
        "card_number": card_number,
        "card_holder": card_holder,
        **(await _card2_info(row.id)),   # PATCH_CARD_BOTH_V1
        "presets": [50000, 100000, 200000, 500000],
    })


async def _card2_info(bot_id: int) -> dict:
    """=== PATCH_CARD_BOTH_V1 === کارتِ دوم فقط وقتی مالک «نمایشِ هر دو کارت» را روشن کرده."""
    from tenant.gateways.card_display import both_enabled
    if not await both_enabled(bot_id):
        return {}
    from tenant import get_setting
    return {"card_number2": await get_setting(bot_id, "card_number2", ""),
            "card_holder2": await get_setting(bot_id, "card_holder2", "")}


@miniapp_router.post("/api/miniapp/{bot_id}/wallet/topup/submit")
async def api_wallet_topup_submit(
    bot_id: int,
    amount: int = Form(...),
    receipt: UploadFile = File(...),
    x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None),
):
    """ثبتِ درخواستِ شارژِ کارت‌به‌کارت از مینی‌اپ: دقیقاً همون مسیرِ تاییدِ
    ادمین/کانالِ رسید که تو خودِ ربات استفاده می‌شه (سفارش awaiting_admin +
    دکمه‌های chadm:ok / chadm:no) — پس نیازی به تغییرِ منطقِ تاییدِ کیف‌پول نیست."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    from tenant import get_setting, _notify_receipt_admins, _admin_user_block, rial as _rial_fmt
    from aiogram import Bot as AiogramBot
    from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

    if amount < 1000:
        raise HTTPException(status_code=400, detail="invalid_amount")
    card_enabled = await get_setting(row.id, "card_enabled", "1") == "1"
    if not card_enabled:
        raise HTTPException(status_code=409, detail="card_disabled")

    content = await receipt.read()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="invalid_receipt")

    tg_bot = AiogramBot(token=row.token)
    try:
        # پروب: عکس رو یه‌بار برای صاحبِ ربات می‌فرستیم فقط تا file_id تلگرامی
        # بگیریم، بلافاصله خودِ پیام رو پاک می‌کنیم تا تکراری/شلوغ نشه.
        probe = await tg_bot.send_photo(
            row.owner_id,
            BufferedInputFile(content, filename="receipt.jpg"),
        )
        file_id = probe.photo[-1].file_id if probe.photo else None
        try:
            await tg_bot.delete_message(row.owner_id, probe.message_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if not file_id:
            raise HTTPException(status_code=502, detail="upload_failed")

        async with Session() as s:
            o = Order(bot_id=row.id, user_id=u.id, volume_gb=0, days=0,
                      count=1, amount=amount, pay_method="card_charge",
                      status="awaiting_admin", receipt_file_id=file_id)
            s.add(o)
            await s.flush()
            oid = o.id
            await s.commit()

        kb_admin = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید", callback_data=f"chadm:ok:{oid}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"chadm:no:{oid}"),
        ]])
        notif = (
            "💳 <b>رسید پرداخت (از مینی‌اپ)</b>\n\n"
            f"{await _admin_user_block(u, u.telegram_id, bot_id=row.id)}\n"
            f"💵 مبلغ سفارش: {_rial_fmt(amount)}\n"
            f"سفارش: #{await order_no(Order, oid)}"
        )
        await _notify_receipt_admins(tg_bot, row, notif, kb_admin, file_id)
    finally:
        await tg_bot.session.close()

    return JSONResponse({"ok": True, "order_id": oid})


@miniapp_router.get("/api/miniapp/{bot_id}/buy/{plan_id}")
async def api_buy_link(bot_id: int, plan_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """لینکِ برگشت به چتِ ربات را برمی‌گرداند — خودِ خرید/پرداخت داخلِ
    خودِ ربات (با همون جریانِ همیشگی) انجام می‌شود، نه اینجا."""
    row, _u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != bot_id or not plan.active:
            raise HTTPException(status_code=404, detail="plan_not_found")
    if not row.username:
        raise HTTPException(status_code=409, detail="bot_username_unknown")
    return JSONResponse({"ok": True, "deep_link": f"https://t.me/{row.username}?start=vpay_{plan_id}"})


# === PATCH_MINIAPP_STOCK_CATS_V1 ===
@miniapp_router.get("/api/miniapp/{bot_id}/buypack/{pack}")
async def api_buy_pack_link(bot_id: int, pack: str, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """دیپ‌لینکِ خریدِ یک بستهٔ آمادهٔ انبار. مثلِ خریدِ پلن، خودِ پرداخت
    داخلِ ربات (هندلرِ mcfg:buy در tenant/__init__.py) انجام می‌شود."""
    row, _u = await _authed_context(bot_id, x_init_data, x_kb_token)
    parts = pack.split("_")
    if len(parts) != 4 or not all(x.lstrip("-").isdigit() for x in parts):
        raise HTTPException(status_code=400, detail="bad_pack")
    vol, days, users, price = (int(x) for x in parts)
    async with Session() as s:
        exists = await s.scalar(select(Config.id).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.volume_gb == vol, Config.days == days,
            Config.user_count == users, Config.price == price))
    if not exists:
        raise HTTPException(status_code=404, detail="pack_not_found")
    if not row.username:
        raise HTTPException(status_code=409, detail="bot_username_unknown")
    return JSONResponse({
        "ok": True,
        "deep_link": f"https://t.me/{row.username}?start=mbuy_{vol}_{days}_{users}_{price}"})


# === PATCH_MINIAPP_VSERVICES_V1 ===
# محصولاتِ «خدمات مجازی» که در مینی‌اپ نمایش داده می‌شوند. کلیدها همان
# action هایِ MainButton هستند، پس فهرست دقیقاً همان چیزی می‌شود که
# مستأجر خودش در «دکمه‌ها» فعال کرده — نه یک لیستِ ثابتِ حدسی.
_VS_PRODUCTS = {
    "gift_shop":          ("🎁", "گیفت تلگرام",        "ارسال گیفت به هر اکانت"),
    "stars_shop":         ("⭐", "استارز تلگرام",       "شارژ استارز برای هر آیدی"),
    "premium_shop":       ("🌟", "پریمیوم تلگرام",      "اشتراک رسمی تلگرام"),
    "ton_shop":           ("💎", "تون (TON)",           "خرید ارز تون"),
    "star_reaction_shop": ("⭐️", "ری‌اکشن استارزی",     "ری‌اکشن روی پست کانال"),
    "vnum_shop":          ("📱", "شمارهٔ مجازی",        "شمارهٔ کشورهای مختلف"),
    "smm_shop":           ("🛰", "خدمات شبکه‌های اجتماعی", "فالوور، لایک، بازدید"),
}


# === PATCH_MINIAPP_RENAME_WEB_SERVICES_V1 ===
# دکمه‌هایِ «سرویس‌هایِ وب» (ممبر/ویو/لایک/ری‌اکشن/فالوور، پلتفرمِ SMM، شمارهٔ مجازی…)
# در کاتالوگِ خریدِ مینی‌اپ نمی‌آیند (هرکدام فلویِ خودشان را دارند)، ولی ادمین باید بتواند
# اسمشان را از همان بخشِ «مدیریت» عوض کند؛ نامِ دکمه همان MainButton.label است و در ربات هم عوض می‌شود.
_VS_RENAME_EXTRA = (
    "vs_member", "vs_reaction", "vs_view", "vs_like", "vs_vote", "vs_comment",
    "vs_ig_follower", "vs_ig_view", "vs_ig_like",
    "smm_platform", "smm_direct_product", "smm_myorders",
    "vnum_shop_manual", "vnum_shop_auto", "nft_market",
)


@miniapp_router.get("/api/miniapp/{bot_id}/vservices")
async def api_vservices(bot_id: int, x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    """کاتالوگِ محصولاتِ خدماتِ مجازیِ این ربات.

    خودِ خرید داخلِ ربات انجام می‌شود (هر محصول یک گفتگویِ چندمرحله‌ای
    دارد: مقصد، تعداد، تأییدِ ادمین). پس اینجا فقط کاتالوگ + دیپ‌لینک
    برمی‌گردد و منطقِ خرید بازسازی نمی‌شود — همان الگویی که برایِ خریدِ
    پلن و بستهٔ انبار هم استفاده شده.
    """
    row, _u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id,
            MainButton.action.in_(list(_VS_PRODUCTS))
        ).order_by(MainButton.row, MainButton.col))).all()

    items, seen = [], set()
    for b in btns:
        act = b.action or ""
        if act in seen or act not in _VS_PRODUCTS:
            continue
        seen.add(act)
        icon, title, desc = _VS_PRODUCTS[act]
        items.append({
            "key": act,
            "icon": icon,
            # برچسبِ خودِ مستأجر مقدم است؛ او ممکن است اسمِ دیگری گذاشته باشد.
            "title": (b.label or title).strip(),
            "desc": desc,
            "deep_link": (f"https://t.me/{row.username}?start=vsvc_{act}"
                          if row.username else None),
        })
    # === PATCH_MINIAPP_SMM_CATS_V1 ===
    # هر دکمهٔ SMMِ صفحهٔ اصلی (اتصالِ سریع / پلتفرم) یک ورودیِ جدا با همان نام و همان مسیرِ ربات؛
    # دکمهٔ «خدمات SMM» (smm_shop) مسیرِ خودکارِ پلتفرم←نوع را نشان می‌دهد.
    from models import MainButton as _MB
    async with Session() as _s2:
        _sbtns = (await _s2.scalars(select(_MB).where(
            _MB.bot_id == bot_id, _MB.action.in_(["smm_direct_product", "smm_platform"]))
            .order_by(_MB.row, _MB.col))).all()
    for _b in _sbtns:
        items.append({"key": "smm_btn", "mb_id": _b.id, "icon": "🛰",
                      "title": (_b.label or "خدمات SMM").strip(), "desc": "سفارشِ همین‌جا", "deep_link": None})
    return JSONResponse({"ok": True, "items": items})


# ═══════ ثبتِ سفارشِ خدماتِ مجازی داخلِ خودِ مینی‌اپ ═══════
# === PATCH_VS_MINIAPP_ORDER_V1 ===
# تا امروز مینی‌اپ برایِ هر خرید فقط یک دیپ‌لینک می‌ساخت و کاربر را به چتِ
# ربات پرت می‌کرد. حالا سفارش همان‌جا ثبت می‌شود.
#
# اصلِ کار: هیچ منطقِ پولی این‌جا بازنویسی نشده. تابعِ place_stars_order در
# tenant/smm.py از دلِ هندلرِ ربات بیرون کشیده شد و هر دو طرف همان را صدا
# می‌زنند — یعنی کسرِ موجودی، ثبتِ سفارش، کدِ پیگیری، پستِ کانالِ اعتماد و
# خبر به ادمین دقیقاً یکی است و امکان ندارد دو مسیر از هم واگرا شوند.
#
# فعلاً فقط «استارز» پیاده شده چون ورودی‌اش ساده است (تعداد + مقصد) و
# الگویِ مرجع برایِ بقیهٔ محصولات است؛ هرکدام ورودیِ متفاوتی دارند
# (گیفت: نوعِ گیفت، شمارهٔ مجازی: کشور/سرویس، SMM: لینک + تعداد).


# ═══════ محصولاتِ دستیِ مینی‌اپ (تعریف و خریدِ درون‌اپی) ═══════
# === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 ===
# مستأجر خودش محصول (نام/عکس/قیمت) تعریف می‌کند و مشتری همان‌جا می‌خرد.
# تحویل دستی است — همان الگویِ سفارشِ استارز: پول کسر، سفارش ثبت، کدِ
# پیگیری به مشتری، اطلاع به ادمین.


def _mp_json(p: MiniAppProduct, *, owner: bool = False) -> dict:
    d = {"id": p.id, "title": p.title, "desc": p.description or "",
         "image": p.image_url or "", "price": int(p.price or 0),
         "price_fa": f"{int(p.price or 0):,}",
         "input_label": p.input_label or "",
         "stock": p.stock, "active": bool(p.is_active)}
    if owner:
        d["sort_order"] = p.sort_order
    return d


@miniapp_router.get("/api/miniapp/{bot_id}/products")
async def api_mp_list(bot_id: int, x_init_data: str | None = Header(default=None),
                      x_kb_token: str | None = Header(default=None)):
    """کاتالوگِ محصولاتِ دستی برایِ مشتری (فقط فعال‌ها و موجودها)."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        items = (await s.scalars(select(MiniAppProduct).where(
            MiniAppProduct.bot_id == bot_id, MiniAppProduct.is_active.is_(True))
            .order_by(MiniAppProduct.sort_order, MiniAppProduct.id))).all()
    out = [_mp_json(p) for p in items
           if p.stock is None or int(p.stock) > 0]
    bal = int(float(u.balance or 0))
    return JSONResponse({"ok": True, "items": out,
                         "balance": bal, "balance_fa": f"{bal:,}"})


@miniapp_router.post("/api/miniapp/{bot_id}/products/{product_id}/buy")
async def api_mp_buy(bot_id: int, product_id: int, payload: dict = Body(...),
                     x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    """خریدِ محصولِ دستی از کیف‌پول — کلِ کسر و ثبت در یک تراکنش."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    user_input = (payload.get("input") or "").strip()[:400]

    from tenant import _gen_tracking
    async with Session() as s:
        p = await s.get(MiniAppProduct, product_id)
        if not p or p.bot_id != bot_id or not p.is_active:
            raise HTTPException(status_code=404, detail="not_found")
        if p.stock is not None and int(p.stock) <= 0:
            return JSONResponse({"ok": False, "message": "موجودیِ این محصول تمام شده."},
                                status_code=409)
        if p.input_label and not user_input:
            return JSONResponse({"ok": False, "message": f"«{p.input_label}» را وارد کن."},
                                status_code=400)
        price = int(p.price or 0)
        buyer = await s.get(BotUser, u.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        bal = int(float(buyer.balance or 0))
        if bal < price:
            return JSONResponse(
                {"ok": False,
                 "message": f"موجودیِ کیف‌پول کافی نیست. کمبود: {price - bal:,} تومان"},
                status_code=402)
        buyer.balance = bal - price
        s.add(WalletTxn(bot_id=bot_id, user_id=buyer.id, amount=-price,
                        kind="purchase", note=f"خریدِ {p.title}"))
        if p.stock is not None:
            # با شرطِ موجودی آپدیت می‌شود تا دو خریدِ هم‌زمان منفی‌اش نکنند
            p.stock = max(0, int(p.stock) - 1)
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, bot_id)
        order = MiniAppProductOrder(
            bot_id=bot_id, product_id=p.id, user_id=buyer.id,
            title_snapshot=p.title, amount=price, user_input=user_input or None,
            tracking_code=trk, status="pending")
        s.add(order)
        await s.commit()
        await s.refresh(order)
        oid, title, new_bal = order.id, p.title, int(float(buyer.balance or 0))

    await _mp_notify_admin(bot_id, oid)
    # === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 ===
    # هر مسیرِ فروش باید از _purchase_alert رد شود، وگرنه کانالِ اعتمادسازی
    # و «اطلاعِ خودکارِ خرید» برایِ این مسیر خاموش می‌ماند (همان چیزی که
    # scripts/audit.py چک می‌کند).
    await _mp_purchase_alert(bot_id, oid)
    return JSONResponse({"ok": True, "order_id": oid, "tracking": trk,
                         "title": title, "price_fa": f"{price:,}",
                         "balance_fa": f"{new_bal:,}"})


async def _mp_purchase_alert(bot_id: int, order_id: int) -> None:
    """گزارشِ خرید به کانالِ اعتمادسازی و اطلاعِ خودکارِ خرید."""
    try:
        from aiogram import Bot as _AioBot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from tenant import _purchase_alert
        async with Session() as s:
            o = await s.get(MiniAppProductOrder, order_id)
            if not o:
                return
            buyer = await s.get(BotUser, o.user_id)
            brow = await s.get(BotRow, bot_id)
            if not brow:
                return
            token, detail = brow.token, o.title_snapshot
            amount = int(float(o.amount or 0))
        tbot = _AioBot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await _purchase_alert(tbot, brow, buyer=buyer, section="سرویس‌های مینی‌اپ",
                                  detail=detail, amount=amount,
                                  order_id=order_id, count=1)
        finally:
            try:
                await tbot.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        logger.exception("miniapp: گزارشِ خریدِ محصولِ دستی ارسال نشد")


async def _mp_notify_admin(bot_id: int, order_id: int) -> None:
    """خبرِ سفارشِ تازه به ادمین. خطایش سفارش را برنمی‌گرداند."""
    try:
        from aiogram import Bot as _AioBot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from tenant import _notify_admins
        async with Session() as s:
            o = await s.get(MiniAppProductOrder, order_id)
            if not o:
                return
            buyer = await s.get(BotUser, o.user_id)
            brow = await s.get(BotRow, bot_id)
            if not brow:
                return
            who = (buyer.full_name or str(buyer.telegram_id)) if buyer else "؟"
            wid = buyer.telegram_id if buyer else 0
            txt = (f"🛍 <b>سفارشِ جدید از مینی‌اپ</b> — #{o.tracking_code or o.id}\n\n"
                   f"محصول: <b>{o.title_snapshot}</b>\n"
                   f"مبلغ: <b>{int(float(o.amount)):,}</b> تومان\n"
                   f"خریدار: {who} (<code>{wid}</code>)\n"
                   + (f"ورودیِ مشتری: <code>{o.user_input}</code>\n" if o.user_input else "")
                   + f"🔖 کد پیگیری: <code>{o.tracking_code}</code>\n\n"
                   "بعد از تحویل، از تبِ «مدیریت» مینی‌اپ وضعیتش را «تحویل شد» کن.")
            token = brow.token
        tbot = _AioBot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await _notify_admins(tbot, brow, txt)
        finally:
            try:
                await tbot.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        logger.exception("miniapp: خبرِ سفارشِ محصولِ دستی به ادمین نرسید")


# ───── مدیریتِ محصولاتِ دستی (فقط مالک/ادمین) ─────

@miniapp_router.get("/api/miniapp/{bot_id}/admin/products")
async def api_mp_admin_list(bot_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _miniapp_is_admin(row, u):
        raise HTTPException(status_code=403, detail="not_admin")
    async with Session() as s:
        items = (await s.scalars(select(MiniAppProduct).where(
            MiniAppProduct.bot_id == bot_id)
            .order_by(MiniAppProduct.sort_order, MiniAppProduct.id))).all()
        orders = (await s.execute(
            select(MiniAppProductOrder, BotUser)
            .join(BotUser, BotUser.id == MiniAppProductOrder.user_id)
            .where(MiniAppProductOrder.bot_id == bot_id)
            .order_by(MiniAppProductOrder.id.desc()).limit(50))).all()
    return JSONResponse({
        "ok": True,
        "items": [_mp_json(p, owner=True) for p in items],
        "orders": [{"id": o.id, "title": o.title_snapshot,
                    "amount_fa": f"{int(float(o.amount)):,}",
                    "status": o.status, "tracking": o.tracking_code or "",
                    "input": o.user_input or "",
                    "buyer": (b.full_name or str(b.telegram_id))}
                   for o, b in orders],
    })


@miniapp_router.post("/api/miniapp/{bot_id}/admin/product")
async def api_mp_admin_save(bot_id: int, payload: dict = Body(...),
                            x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _miniapp_is_admin(row, u):
        raise HTTPException(status_code=403, detail="not_admin")
    title = (payload.get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(status_code=400, detail="no_title")

    def _num(k, default=0):
        try:
            return max(0, int(float(payload.get(k) or default)))
        except (TypeError, ValueError):
            return default

    raw_stock = payload.get("stock")
    stock = None if raw_stock in (None, "", "-") else _num("stock")

    async with Session() as s:
        pid = payload.get("id")
        if pid:
            p = await s.get(MiniAppProduct, int(pid))
            if not p or p.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
        else:
            p = MiniAppProduct(bot_id=bot_id)
            s.add(p)
        p.title = title
        p.description = (payload.get("desc") or "").strip()[:400] or None
        p.image_url = (payload.get("image") or "").strip()[:500] or None
        p.price = _num("price")
        p.input_label = (payload.get("input_label") or "").strip()[:120] or None
        p.stock = stock
        p.sort_order = _num("sort_order")
        if payload.get("active") is not None:
            p.is_active = bool(payload["active"])
        await s.commit()
        await s.refresh(p)
        return JSONResponse({"ok": True, "item": _mp_json(p, owner=True)})


@miniapp_router.post("/api/miniapp/{bot_id}/admin/product/{product_id}/{action}")
async def api_mp_admin_action(bot_id: int, product_id: int, action: str,
                              x_init_data: str | None = Header(default=None),
                              x_kb_token: str | None = Header(default=None)):
    """delete محصول، یا deliver/reject یک سفارش (product_id = شناسهٔ سفارش)."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _miniapp_is_admin(row, u):
        raise HTTPException(status_code=403, detail="not_admin")

    if action == "delete":
        async with Session() as s:
            p = await s.get(MiniAppProduct, product_id, with_for_update=True)
            if not p or p.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
            # سفارش‌هایِ گذشته نگه داشته می‌شوند (عنوان در خودشان ذخیره
            # شده)، فقط محصول از کاتالوگ خارج می‌شود.
            p.is_active = False
            await s.commit()
        return JSONResponse({"ok": True})

    if action in ("deliver", "reject"):
        import datetime as _dt
        async with Session() as s:
            o = await s.get(MiniAppProductOrder, product_id, with_for_update=True)
            if not o or o.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
            if o.status != "pending":
                raise HTTPException(status_code=409, detail="bad_status")
            if action == "deliver":
                o.status = "delivered"
                o.delivered_at = _dt.datetime.utcnow()
            else:
                o.status = "rejected"
                buyer = await s.get(BotUser, o.user_id, with_for_update=True)
                if buyer:
                    buyer.balance = int(float(buyer.balance or 0)) + int(float(o.amount))
                    s.add(WalletTxn(bot_id=bot_id, user_id=buyer.id,
                                    amount=int(float(o.amount)), kind="refund",
                                    note=f"عودتِ سفارشِ #{o.id}"))
                prod = await s.get(MiniAppProduct, o.product_id, with_for_update=True)
                if prod and prod.stock is not None:
                    prod.stock = int(prod.stock) + 1
            await s.commit()
        await _mp_notify_buyer(bot_id, product_id, action)
        return JSONResponse({"ok": True})

    raise HTTPException(status_code=400, detail="bad_action")


async def _mp_notify_buyer(bot_id: int, order_id: int, action: str) -> None:
    try:
        from aiogram import Bot as _AioBot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        async with Session() as s:
            o = await s.get(MiniAppProductOrder, order_id)
            brow = await s.get(BotRow, bot_id)
            if not o or not brow:
                return
            buyer = await s.get(BotUser, o.user_id)
            if not buyer:
                return
            uid, token = buyer.telegram_id, brow.token
            if action == "deliver":
                txt = (f"✅ سفارشت تحویل شد.\n\n<b>{o.title_snapshot}</b>\n"
                       f"🔖 کد پیگیری: <code>{o.tracking_code}</code>")
            else:
                txt = (f"↩️ سفارشِ <b>{o.title_snapshot}</b> لغو شد و مبلغِ "
                       f"<b>{int(float(o.amount)):,}</b> تومان به کیف‌پولت برگشت.")
        tbot = _AioBot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tbot.send_message(uid, txt)
        finally:
            try:
                await tbot.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        logger.exception("miniapp: اطلاع به خریدارِ محصولِ دستی نرسید")


async def _vs_product_enabled(bot_id: int, action: str) -> bool:
    """آیا این محصولِ خدماتِ مجازی رویِ صفحهٔ اصلیِ ربات فعال است؟"""
    async with Session() as s:
        return bool(await s.scalar(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.action == action)))


@miniapp_router.get("/api/miniapp/{bot_id}/vs/stars/info")
async def api_vs_stars_info(bot_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    """نرخ، بازهٔ مجاز و موجودیِ کاربر — برایِ ساختنِ فرمِ سفارش."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    # === PATCH_VS_MINIAPP_ORDER_V1 ===
    # «فعال بودن» این محصول یعنی مستأجر دکمه‌اش را در صفحهٔ اصلی گذاشته
    # باشد — همان معیاری که api_vservices برایِ کاتالوگ استفاده می‌کند.
    # تنظیمِ جداگانه‌ای به نامِ stars_sell_enabled وجود ندارد.
    if not await _vs_product_enabled(bot_id, "stars_shop"):
        return JSONResponse({"ok": False, "enabled": False})
    from tenant.bots.virtual_service.smm import _stars_sell_settings
    rate, qmin, qmax = await _stars_sell_settings(bot_id)
    bal = int(float(u.balance or 0))
    return JSONResponse({
        "ok": True, "enabled": True,
        "rate": float(rate), "min": int(qmin), "max": int(qmax),
        "balance": bal, "balance_fa": f"{bal:,}",
        "self_username": u.username or "",
    })


@miniapp_router.post("/api/miniapp/{bot_id}/vs/stars/order")
async def api_vs_stars_order(bot_id: int, payload: dict = Body(...),
                             x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
    """ثبتِ سفارشِ استارز — همان مسیرِ ربات، بدونِ خروج از مینی‌اپ."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    from tenant.bots.virtual_service.smm import place_stars_order, StarsOrderError
    if not await _vs_product_enabled(bot_id, "stars_shop"):
        raise HTTPException(status_code=403, detail="disabled")
    try:
        qty = int(payload.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_qty")
    dest_username = (payload.get("dest_username") or "").strip().lstrip("@")

    # === PATCH_STARS_UNSTARTED_DEST_V1 ===
    # قبلاً اگر یوزرنیمِ مقصد توی BotUser نبود (یعنی این ربات را استارت
    # نکرده)، سفارش رد می‌شد. این بخش تحویلِ خودکار ندارد (ادمین دستی از
    # فرگمنت می‌فرستد)، پس نیازی به user_id واقعی نیست — dest_id=0 به‌همراهِ
    # خودِ یوزرنیم کافی است.
    dest_id = u.telegram_id
    resolved_username = ""
    if dest_username and dest_username.lower() != (u.username or "").lower():
        async with Session() as s:
            target = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.username.ilike(dest_username)))
        if target:
            dest_id = target.telegram_id
        else:
            dest_id = 0
            resolved_username = dest_username

    from aiogram import Bot as _AioBot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    tbot = _AioBot(token=row.token,
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        res = await place_stars_order(
            tbot, row, buyer_tg_id=u.telegram_id, buyer_name=u.full_name or "",
            buyer_username=u.username or "", dest_id=dest_id, qty=qty,
            dest_username=resolved_username)
    except StarsOrderError as e:
        return JSONResponse({"ok": False, "error": "rejected", "message": str(e)},
                            status_code=402)
    except Exception:                                        # noqa: BLE001
        logger.exception("miniapp: ثبتِ سفارشِ استارز ناموفق بود")
        raise HTTPException(status_code=500, detail="order_failed")
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass

    async with Session() as s:
        fresh = await s.get(BotUser, u.id)
        new_bal = int(float(fresh.balance or 0)) if fresh else 0
    return JSONResponse({"ok": True, "tracking": res["tracking"],
                         "price_fa": f"{int(res['price']):,}",
                         "balance_fa": f"{new_bal:,}"})


# ═══════════ سفارشِ خدماتِ SMM داخلِ مینی‌اپ ═══════════
# === PATCH_MINIAPP_SMM_ORDER_V1 ===
# درخواستِ مستأجر: «توی ربات روی خدمات می‌زنند دسته‌ها می‌آید؛ توی مینی‌اپ هم روی ثبت سفارش
# بزنند دسته‌ها بیاید، محصولاتِ SMM بیاید و همان‌جا سفارش ثبت کنند.»
#
# هیچ منطقِ پول/سفارشی اینجا بازنویسی نشده: درختِ پلتفرم‌ها/نوع‌ها/سرویس‌ها از همان
# _user_active_tree، قیمت از compute_sell_price، تخفیفِ خودکار از _smm_auto_discount،
# اعتبارسنجیِ لینک از _check_links_for_category و خودِ ثبت از smm_place_order می‌آید —
# یعنی دقیقاً همان کدی که دکمه‌هایِ ربات اجرا می‌کنند.

async def _smm_enabled(bot_id: int) -> bool:
    for act in ("smm_shop", "smm_platform", "smm_direct_product"):
        if await _vs_product_enabled(bot_id, act):
            return True
    return False


async def _smm_svc_json(bot_id: int, v, with_price: bool = True) -> dict:
    from tenant.bots.virtual_service.smm import compute_sell_price
    d = {"id": v.id, "name": v.name, "min": int(v.min_qty or 0), "max": int(v.max_qty or 0),
         "desc": ((v.description or v.provider_desc or "").strip())[:400]}
    if with_price:
        try:
            d["price_1000"] = int(await compute_sell_price(bot_id, v, 1000))
        except Exception:                                    # noqa: BLE001
            logger.exception("miniapp smm: محاسبهٔ قیمت ناموفق بود")
            d["price_1000"] = 0
    return d


async def _smm_scope(bot_id: int, mb_id: int | None):
    """(سرویس‌هایِ در دسترس، حالت). حالت: cat = دسته‌هایِ دستیِ ادمین | plat = پلتفرم/نوع | single | auto.

    === PATCH_MINIAPP_SMM_CATS_V1 ===
    گزارش: «دسته‌هایِ خودکار می‌آید، دسته‌هایی که من در ربات ساخته‌ام نیامده». در ربات، دسته‌هایِ
    دستیِ ادمین از طریقِ «دکمهٔ اتصالِ سریع» (smm_direct_product) با smm_category_ids دیده می‌شوند؛
    مینی‌اپ حالا برایِ هر دکمهٔ SMMِ صفحهٔ اصلی همان مسیر را می‌سازد (همان توابعِ ربات).
    """
    from models import SmmService
    from tenant.bots.virtual_service import smm as _smm
    if mb_id:
        async with Session() as s:
            mb = await s.get(MainButton, mb_id)
        if not mb or mb.bot_id != bot_id:
            return [], "none"
        if mb.action == "smm_direct_product":
            svcs = await _smm._resolve_direct_product_services(bot_id, mb)
            cfg = _smm._mb_direct_cfg(mb)
            if cfg.get("smm_category_ids"):
                return svcs, "cat"
            if len(svcs) == 1:
                return svcs, "single"
            return svcs, "plat"
        if mb.action == "smm_platform":
            try:
                cfg = _json.loads(mb.config) if mb.config else {}
            except Exception:                                # noqa: BLE001
                cfg = {}
            pk = [x for x in (cfg.get("platforms") or []) if x] or \
                ([cfg["platform"]] if cfg.get("platform") else [])
            async with Session() as s:
                allv = (await s.scalars(select(SmmService).where(
                    SmmService.bot_id == bot_id, SmmService.is_active == True))).all()   # noqa: E712
            return [v for v in allv if not pk or _smm._svc_platform_type(v)[0] in pk], "plat"
    async with Session() as s:
        allv = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True)   # noqa: E712
            .order_by(SmmService.id))).all()
    return allv, "auto"


@miniapp_router.get("/api/miniapp/{bot_id}/vs/smm/tree")
async def api_vs_smm_tree(bot_id: int, mb: int = 0,
                          x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    """گروه‌ها (دسته/پلتفرم) و نوع‌هایِ سرویسِ فعال — همان صفحهٔ اولِ «خدمات» در ربات."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _smm_enabled(bot_id):
        return JSONResponse({"ok": False, "enabled": False})
    from tenant import get_setting
    from tenant.bots.virtual_service import smm_api
    from tenant.bots.virtual_service import smm as _smm
    svcs, mode = await _smm_scope(bot_id, mb or None)
    bal = int(float(u.balance or 0))
    base = {"ok": True, "enabled": True, "balance": bal, "balance_fa": f"{bal:,}", "mode": mode}
    ov = await _smm._platform_label_overrides(bot_id)
    groups = []
    if mode == "single" and svcs:
        base["single"] = await _smm_svc_json(bot_id, svcs[0])
    elif mode == "cat":
        by: dict[int, list] = {}
        for v in svcs:
            if v.category_pk:
                by.setdefault(v.category_pk, []).append(v)
        names = await _smm._qlc_category_names(bot_id, list(by.keys()))
        for cid, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
            types: dict[str, int] = {}
            for v in items:
                t = _smm._svc_platform_type(v)[1]
                types[t] = types.get(t, 0) + 1
            groups.append({"key": str(cid), "label": names.get(cid, "؟"), "count": len(items),
                           "types": [{"key": t, "label": smm_api.service_type_label(t), "count": n}
                                     for t, n in sorted(types.items(), key=lambda kv: -kv[1])]})
        base["depth"] = 2 if (await get_setting(bot_id, "qlc_depth", "3")) == "2" else 3
    else:
        by_p: dict[str, dict[str, int]] = {}
        for v in svcs:
            pk_, tk_ = _smm._svc_platform_type(v)
            by_p.setdefault(pk_, {})
            by_p[pk_][tk_] = by_p[pk_].get(tk_, 0) + 1
        for pk_, types in sorted(by_p.items(), key=lambda kv: -sum(kv[1].values())):
            groups.append({"key": pk_, "label": ov.get(pk_, smm_api.platform_label(pk_)),
                           "count": sum(types.values()),
                           "types": [{"key": t, "label": smm_api.service_type_label(t), "count": n}
                                     for t, n in sorted(types.items(), key=lambda kv: -kv[1])]})
    base["groups"] = groups
    base["platforms"] = groups          # سازگاریِ عقب‌رو
    return JSONResponse(base)


@miniapp_router.get("/api/miniapp/{bot_id}/vs/smm/services")
async def api_vs_smm_services(bot_id: int, plat: str = "", typ: str = "__all__", mb: int = 0,
                              x_init_data: str | None = Header(default=None),
                              x_kb_token: str | None = Header(default=None)):
    """سرویس‌هایِ یک گروه (+نوع) با قیمتِ هر ۱۰۰۰ و بازهٔ مجاز. plat = کلیدِ پلتفرم یا شناسهٔ دسته."""
    await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _smm_enabled(bot_id):
        return JSONResponse({"ok": False, "enabled": False})
    from datetime import datetime as _dt
    from tenant import get_setting
    from tenant.bots.virtual_service import smm as _smm
    svcs, mode = await _smm_scope(bot_id, mb or None)
    if mode == "cat":
        entries = [v for v in svcs if str(v.category_pk) == plat]
    else:
        entries = [v for v in svcs if _smm._svc_platform_type(v)[0] == plat]
    if typ and typ != "__all__":
        entries = [v for v in entries if _smm._svc_platform_type(v)[1] == typ]
    order = await get_setting(bot_id, "smm_sort_order", "none")
    if order == "price_asc":
        entries.sort(key=lambda x: float(x.sell_rate or x.panel_rate or 0))
    elif order == "price_desc":
        entries.sort(key=lambda x: float(x.sell_rate or x.panel_rate or 0), reverse=True)
    elif order == "new_first":
        entries.sort(key=lambda x: x.created_at or _dt.min, reverse=True)
    elif order == "old_first":
        entries.sort(key=lambda x: x.created_at or _dt.min)
    elif order == "manual":
        entries.sort(key=lambda x: (x.sort_order or 0, x.id))
    return JSONResponse({"ok": True, "services": [await _smm_svc_json(bot_id, v) for v in entries[:200]]})


async def _smm_resolve(bot_id: int, service_id: int):
    from models import SmmService
    async with Session() as s:
        v = await s.get(SmmService, service_id)
    if not v or v.bot_id != bot_id or not v.is_active:
        return None
    return v


@miniapp_router.post("/api/miniapp/{bot_id}/vs/smm/quote")
async def api_vs_smm_quote(bot_id: int, payload: dict = Body(...),
                           x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    """قیمتِ نهایی (با تخفیفِ خودکار) + اعتبارسنجیِ تعداد/لینک، قبل از ثبت."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _smm_enabled(bot_id):
        raise HTTPException(status_code=403, detail="disabled")
    from tenant.bots.virtual_service.smm import (
        compute_sell_price, _smm_auto_discount, _check_links_for_category)
    try:
        sid = int(payload.get("service_id") or 0)
        qty = int(payload.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_input")
    v = await _smm_resolve(bot_id, sid)
    if not v:
        return JSONResponse({"ok": False, "message": "این سرویس دیگر در دسترس نیست."}, status_code=404)
    out = {"ok": True, "valid": True, "message": "", "price": 0, "final": 0, "discount": 0,
           "balance": int(float(u.balance or 0))}
    if qty <= 0:
        out.update(valid=False, message="تعداد را وارد کن.")
    elif v.min_qty and qty < v.min_qty:
        out.update(valid=False, message=f"حداقل تعداد {v.min_qty:,} است.")
    elif v.max_qty and qty > v.max_qty:
        out.update(valid=False, message=f"حداکثر تعداد {v.max_qty:,} است.")
    if out["valid"]:
        price = int(await compute_sell_price(bot_id, v, qty))
        if price <= 0:
            out.update(valid=False, message="قیمتِ این سرویس تنظیم نشده.")
        else:
            final = price
            ad = await _smm_auto_discount(bot_id, u.telegram_id, price)
            if ad:
                final = int(ad["final"])
            out.update(price=price, final=final, discount=price - final,
                       enough=out["balance"] >= final)
    link = (payload.get("link") or "").strip()
    if link:
        from models import SmmCategory
        import re as _re
        async with Session() as s:
            cat = await s.get(SmmCategory, v.category_pk) if v.category_pk else None
        wrong, hint = await _check_links_for_category(cat, [link])
        if wrong:
            out.update(valid=False, link_ok=False,
                       message=_re.sub(r"<[^>]+>", "", "این لینک برایِ این سرویس درست نیست. " + (hint or "")))
        else:
            out["link_ok"] = True
    return JSONResponse(out)


@miniapp_router.post("/api/miniapp/{bot_id}/vs/smm/order")
async def api_vs_smm_order(bot_id: int, payload: dict = Body(...),
                           x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    """ثبتِ سفارشِ SMM — همان مسیرِ ربات (smm_place_order)، بدونِ خروج از مینی‌اپ."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _smm_enabled(bot_id):
        raise HTTPException(status_code=403, detail="disabled")
    from tenant.bots.virtual_service.smm import (
        smm_place_order, compute_sell_price, _check_links_for_category)
    try:
        sid = int(payload.get("service_id") or 0)
        qty = int(payload.get("qty") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_input")
    link = (payload.get("link") or "").strip()
    v = await _smm_resolve(bot_id, sid)
    if not v:
        return JSONResponse({"ok": False, "message": "این سرویس دیگر در دسترس نیست."}, status_code=404)
    if not (link.startswith("http://") or link.startswith("https://") or link.startswith("@")
            or "t.me/" in link):
        return JSONResponse({"ok": False, "message": "لینکِ معتبر وارد کن (مثلاً https://t.me/...)."}, status_code=400)
    if qty <= 0 or (v.min_qty and qty < v.min_qty) or (v.max_qty and qty > v.max_qty):
        return JSONResponse({"ok": False, "message": "تعدادِ واردشده در بازهٔ مجاز نیست."}, status_code=400)
    from models import SmmCategory
    async with Session() as s:
        cat = await s.get(SmmCategory, v.category_pk) if v.category_pk else None
    wrong, _hint = await _check_links_for_category(cat, [link])
    if wrong:
        return JSONResponse({"ok": False, "message": "این لینک برایِ این سرویس درست نیست."}, status_code=400)
    price = int(await compute_sell_price(bot_id, v, qty))
    if price <= 0:
        return JSONResponse({"ok": False, "message": "قیمتِ این سرویس تنظیم نشده."}, status_code=409)

    class _Sink:
        """جایگزینِ Message: پیام‌هایِ مسیرِ ثبت (تأیید/خطا) به چتِ خودِ کاربر می‌رود."""
        def __init__(self, bot, user):
            self._bot, self.from_user = bot, type("U", (), {
                "id": user.telegram_id, "full_name": user.full_name or "", "username": user.username or ""})()
            self.chat = type("C", (), {"id": user.telegram_id})()

        async def answer(self, text, **kw):
            kw.pop("parse_mode", None)
            return await self._bot.send_message(self.chat.id, text, **kw)

    from aiogram import Bot as _AioBot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    tbot = _AioBot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        res = await smm_place_order(tbot, row, _Sink(tbot, u), v, link, qty, price, None)
    except Exception:                                        # noqa: BLE001
        logger.exception("miniapp: ثبتِ سفارشِ SMM ناموفق بود")
        raise HTTPException(status_code=500, detail="order_failed")
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass
    async with Session() as s:
        fresh = await s.get(BotUser, u.id)
        new_bal = int(float(fresh.balance or 0)) if fresh else 0
    st = res.get("status")
    if st == "insufficient":
        return JSONResponse({"ok": False, "error": "insufficient",
                             "message": f"موجودیِ کیف پولت کافی نیست (لازم: {res.get('need', 0):,} تومان).",
                             "balance_fa": f"{new_bal:,}"}, status_code=402)
    if st == "panel_failed":
        return JSONResponse({"ok": False, "error": "panel_failed",
                             "message": "ثبتِ سفارش روی پنل با مشکل مواجه شد و مبلغ به کیف پولت برگشت. کمی بعد دوباره امتحان کن.",
                             "balance_fa": f"{new_bal:,}"}, status_code=502)
    return JSONResponse({"ok": True, "tracking": res.get("tracking"),
                         "price_fa": f"{int(res.get('price') or 0):,}", "balance_fa": f"{new_bal:,}"})


# ═══════════ پنلِ مدیریتِ داخلِ مینی‌اپ (تغییرِ نامِ محصولات) ═══════════
# === PATCH_MINIAPP_ADMIN_RENAME_V1 ===
# گزارشِ مستأجر: اسمِ محصولاتی که تویِ مینی‌اپ دیده می‌شوند از داخلِ خودِ
# مینی‌اپ قابلِ تغییر نبود و باید هر بار برمی‌گشت تویِ چتِ ربات و از منویِ
# دکمه‌ها عوضش می‌کرد.
#
# هیچ فیلدِ تازه‌ای ساخته نشده: همان جاهایی که از قبل عنوان از آن‌ها خوانده
# می‌شد این‌جا نوشته می‌شوند — یعنی تغییر هم در مینی‌اپ و هم در خودِ ربات
# هم‌زمان دیده می‌شود و دو منبعِ حقیقتِ جدا از هم درست نمی‌شود:
#   • vservice → MainButton.label
#   • plan     → VpnPlan.title
#   • category → MiniAppCategory.title


async def _miniapp_is_admin(row, u) -> bool:
    if not row or not u:
        return False
    try:
        from tenant import is_owner_or_admin
        return await is_owner_or_admin(row.id, int(u.telegram_id), int(row.owner_id))
    except Exception:                                        # noqa: BLE001
        return int(row.owner_id) == int(u.telegram_id)


@miniapp_router.get("/api/miniapp/{bot_id}/admin/catalog")
async def api_admin_catalog(bot_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    """هرچه در مینی‌اپ عنوان دارد و قابلِ تغییرِ نام است."""
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _miniapp_is_admin(row, u):
        raise HTTPException(status_code=403, detail="not_admin")

    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id,
            MainButton.action.in_(list(_VS_PRODUCTS))
        ).order_by(MainButton.row, MainButton.col))).all()
        web_btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id,
            MainButton.action.in_(list(_VS_RENAME_EXTRA))
        ).order_by(MainButton.row, MainButton.col))).all()
        cats = (await s.scalars(select(MiniAppCategory).where(
            MiniAppCategory.bot_id == bot_id
        ).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all()
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id).order_by(VpnPlan.id))).all()

    vs, seen = [], set()
    for b in btns:
        act = b.action or ""
        if act in seen or act not in _VS_PRODUCTS:
            continue
        seen.add(act)
        icon, title, _desc = _VS_PRODUCTS[act]
        vs.append({"kind": "vservice", "id": b.id, "icon": icon,
                   "name": (b.label or title).strip(), "default": title})
    web = [{"kind": "vservice", "id": b.id, "icon": "🌐",
            "name": (b.label or b.action or "").strip(), "default": b.action or ""}
           for b in web_btns if (b.label or "").strip()]
    groups = [
        {"key": "vservice", "label": "🧰 خدماتِ مجازی", "items": vs},
        {"key": "category", "label": "🗂 دسته‌بندی‌ها",
         "items": [{"kind": "category", "id": c.id, "icon": c.icon or "🛰",
                    "name": c.title, "active": bool(c.active)} for c in cats]},
        {"key": "plan", "label": "📦 پلن‌ها",
         "items": [{"kind": "plan", "id": p.id, "icon": "📦", "name": p.title,
                    "active": bool(p.active),
                    "hint": f"{p.volume_gb or 0}گیگ / {p.days or 0}روز"} for p in plans]},
    ]
    if web:
        groups.insert(1, {"key": "webservice", "label": "🌐 سرویس‌هایِ وب و شبکه‌هایِ اجتماعی", "items": web})
    return JSONResponse({"ok": True, "groups": groups})


@miniapp_router.post("/api/miniapp/{bot_id}/admin/rename")
async def api_admin_rename(bot_id: int, payload: dict = Body(...),
                           x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    row, u = await _authed_context(bot_id, x_init_data, x_kb_token)
    if not await _miniapp_is_admin(row, u):
        raise HTTPException(status_code=403, detail="not_admin")

    kind = (payload.get("kind") or "").strip()
    name = (payload.get("name") or "").strip()
    try:
        item_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_id")
    if not name:
        raise HTTPException(status_code=400, detail="empty_name")
    # سقفِ طولِ برچسبِ دکمهٔ تلگرام؛ بلندتر از این در کیبورد بد نمایش می‌دهد
    name = name[:64]

    _MODELS = {"vservice": (MainButton, "label"),
               "category": (MiniAppCategory, "title"),
               "plan": (VpnPlan, "title")}
    if kind not in _MODELS:
        raise HTTPException(status_code=400, detail="bad_kind")
    model, field = _MODELS[kind]

    async with Session() as s:
        obj = await s.get(model, item_id)
        if not obj or obj.bot_id != bot_id:
            raise HTTPException(status_code=404, detail="not_found")
        if kind == "vservice" and (obj.action or "") not in _VS_PRODUCTS and (obj.action or "") not in _VS_RENAME_EXTRA:
            # جلوگیری از تغییرِ نامِ دکمه‌هایی که محصولِ خدماتِ مجازی نیستند
            raise HTTPException(status_code=400, detail="bad_kind")
        setattr(obj, field, name)
        await s.commit()
    return JSONResponse({"ok": True, "name": name})


_DISABLED_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>غیرفعال</title></head><body style="background:#000;color:#f2f2f4;
font-family:Tahoma,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;text-align:center"><div>مینی‌اپ این ربات فعال نیست.</div></body></html>"""

_PAGE_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover"/>
<meta name="theme-color" content="#000000"/>
<meta name="color-scheme" content="dark light"/>
<!-- === PATCH_MINIAPP_SELF_HOST_TG_JS_V1 ===
     گزارشِ مشتری (MD04): مینی‌اپ بدونِ فیلترشکن «خطا، دوباره امتحان کن» می‌داد؛ علت: اسکریپتِ رسمیِ تلگرام از
     telegram.org لود می‌شد که در بعضی اینترنت‌هایِ ایران بسته است ⇒ window.Telegram نبود ⇒ initData خالی ⇒ احرازِ هویت رد.
     حالا اسکریپت از خودِ سرورِ ما (با کش) سرو می‌شود؛ فونتِ گوگل هم بلاک‌کننده نیست. -->
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" media="print" onload="this.media='all'"/>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<title>__BOT_TITLE__</title>
<!-- build: __BUILD_ID__ -->
<meta name="build-id" content="__BUILD_ID__"/>
<style>
  :root{
    --accent:__ACCENT__; --accent-2:__ACCENT_2__; --accent-dim: __ACCENT_DIM__;
    --bg:#000000; --card:#131316; --card-2:#18181c; --line: rgba(255,255,255,.08);
    --ink:#f4f5f7; --sub:#9a9aa4; --sub2:#68686f;
    --shadow: 0 10px 28px rgba(0,0,0,.45);
    --glow: 0 12px 26px __ACCENT_GLOW__;
    --green:#34c76f; --red:#ff5b5f; --amber:#ffb020;
  }
  body.light{
    --bg:#f2f3f5; --card:#ffffff; --card-2:#fbfbfc; --line: rgba(10,10,20,.08);
    --ink:#111114; --sub:#6c6c75; --sub2:#9a9aa2;
    --shadow: 0 8px 22px rgba(20,20,30,.08);
    --glow: 0 10px 22px __ACCENT_GLOW__;
  }
  body.light .card, body.light .plan-card, body.light .list-item, body.light .tile,
  body.light .stat-box, body.light .settings-row, body.light .modal-card {
    box-shadow: 0 2px 10px rgba(20,20,40,.07), 0 0 0 1px rgba(20,20,40,.05);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html,body{ height:100%; overflow-x:hidden; }
  body {
    margin: 0; font-family: 'Vazirmatn','Segoe UI',Tahoma,sans-serif;
    background: var(--bg); color: var(--ink); padding-bottom: 104px; min-height: 100vh;
    overflow-y:auto; -webkit-overflow-scrolling:touch; transition: background .4s ease, color .3s ease;
  }
  .glow-spot {
    position: fixed; top: -120px; right: -80px; width: 340px; height: 340px; z-index: 0;
    background: radial-gradient(circle, rgba(255,122,26,.20), transparent 70%); pointer-events:none;
  }
  .wrap { padding: 0 16px; position: relative; z-index: 1; }
  .content { position: relative; z-index: 1; }
  .row { display: flex; align-items: center; gap: 10px; }

  .topbar { padding: 18px 16px 4px; display:flex; align-items:center; justify-content:space-between; position:relative; z-index:1; }
  .brand { display:flex; align-items:center; gap:9px; text-decoration:none; }
  .brand-ic {
    width: 40px; height: 40px; border-radius: 13px; display:flex; align-items:center; justify-content:center;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color:#fff; flex-shrink:0; box-shadow: var(--glow);
  }
  .brand-name { font-size: 14px; font-weight: 800; color: var(--ink); line-height:1.3; }
  .brand-sub { display:flex; align-items:center; gap:4px; font-size: 11px; color: var(--sub); direction:ltr; margin-top:1px; }
  .brand-sub svg { color:#2aa9e0; }
  .theme-toggle {
    width: 40px; height: 40px; border-radius: 13px; border: 1px solid var(--line);
    background: var(--card); display:flex; align-items:center; justify-content:center;
    color: var(--ink); cursor:pointer; transition: transform .25s cubic-bezier(.34,1.56,.64,1);
  }
  .theme-toggle:active { transform: scale(.82) rotate(-10deg); }

  .greet-name { font-size: 19px; font-weight: 800; }
  .greet-sub { font-size: 12.5px; color: var(--sub); margin-top: 3px; }
  .greet-avatar {
    width: 46px; height: 46px; border-radius: 50%; flex-shrink:0; font-weight: 900; font-size: 17px;
    display:flex; align-items:center; justify-content:center; color:#fff;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: var(--glow);
  }

  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 22px; padding: 18px; margin-top: 14px; box-shadow: var(--shadow);
  }
  .wallet-card {
    background: linear-gradient(120deg, var(--accent) 0%, var(--accent-2) 100%);
    border-radius: 24px; padding: 20px; margin-top: 16px; color: #fff; position:relative; overflow:hidden;
    box-shadow: var(--glow);
  }
  .wallet-card::after{
    content:""; position:absolute; inset:0; pointer-events:none;
    background: radial-gradient(circle at 90% -30%, rgba(255,255,255,.35), transparent 55%);
  }
  .wallet-label { font-size: 12.5px; font-weight: 700; opacity: .82; position:relative; }
  .wallet-amount { font-size: 30px; font-weight: 900; margin-top: 8px; position:relative; }
  .wallet-btn {
    margin-top: 16px; background: rgba(10,8,4,.82); color:#fff; border:none; border-radius: 999px;
    padding: 12px 22px; font-weight: 800; font-size: 13px; display:inline-flex; align-items:center; gap:6px;
    position:relative; cursor:pointer;
  }

  .stat-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; margin-top: 14px; }
  body.vs #profile-stats, body.vs .stat-grid:not([style]) { grid-template-columns: repeat(2,1fr); }
  .stat-box {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 16px; padding: 14px 6px; text-align: center;
  }
  .stat-box b { display: block; font-size: 16px; margin-bottom: 4px; }
  .stat-box span { font-size: 11px; color: var(--sub); }
  .section-title { font-size: 13.5px; color: var(--ink); margin: 22px 2px 10px; display: flex; justify-content: space-between; font-weight:800; }
  .section-title .more { font-size: 11.5px; color: var(--accent); font-weight:700; }

  .grid2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 6px; }
  .tile {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 16px;
    display:flex; flex-direction:column; gap: 10px; transition: transform .16s cubic-bezier(.34,1.56,.64,1);
  }
  .tile:active { transform: scale(.955); }
  .tile-ic {
    width: 38px; height: 38px; border-radius: 12px; background: var(--accent-dim); color: var(--accent);
    display:flex; align-items:center; justify-content:center;
  }
  .tile-ttl { font-weight: 800; font-size: 13px; }
  .tile-sub { font-size: 11px; color: var(--sub); }

  .list-item {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    background: var(--card); border: 1px solid var(--line);
    border-radius: 18px; padding: 16px; margin-bottom: 10px; cursor: pointer;
    transition: transform .16s cubic-bezier(.34,1.56,.64,1);
  }
  .list-item:active { transform: scale(.97); }
  .list-item .ttl { font-weight: 800; font-size: 14px; }
  .list-item .sub { font-size: 11.5px; color: var(--sub); margin-top: 3px; }
  .list-ic {
    width: 42px; height: 42px; border-radius: 13px; background: var(--accent-dim); color: var(--accent);
    display:flex; align-items:center; justify-content:center; flex-shrink:0;
  }

  .plan-card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 20px; padding: 17px; margin-bottom: 12px; box-shadow: var(--shadow);
  }
  .plan-top { display: flex; justify-content: space-between; align-items: center; font-weight: 800; font-size: 14.5px; }
  .plan-price {
    background: var(--accent-dim); color: var(--accent);
    border-radius: 10px; padding: 5px 11px; font-size: 12.5px; font-weight:800;
  }
  .chip-row { display: flex; gap: 8px; margin: 11px 0; flex-wrap: wrap; }
  .chip {
    background: var(--card-2); border:1px solid var(--line); border-radius: 10px; padding: 6px 11px; font-size: 11.5px; color: var(--sub);
  }

  .filter-row { display:flex; gap:8px; overflow-x:auto; padding-bottom:2px; margin: 12px 0 16px; }
  .filter-row::-webkit-scrollbar{ display:none; }
  .filter-chip {
    flex-shrink:0; padding: 9px 16px; border-radius: 999px; font-size: 12.5px; font-weight:700;
    background: var(--card); border:1px solid var(--line); color: var(--sub); transition: all .18s ease;
  }
  .filter-chip.active { background: var(--accent); color:#fff; border-color: transparent; }
  .search-box {
    display:flex; align-items:center; gap:8px; background: var(--card); border:1px solid var(--line);
    border-radius: 15px; padding: 12px 14px; color: var(--sub); margin-top: 14px;
  }
  .search-box input {
    background: none; border: none; outline: none; color: var(--ink); font-family: inherit;
    font-size: 13px; flex: 1; text-align: right;
  }
  .search-box input::placeholder { color: var(--sub2); }

  .status-badge { font-size: 10.5px; font-weight: 800; padding: 4px 11px; border-radius: 20px; white-space:nowrap; }
  .status-green { background: rgba(52,199,111,.15); color: var(--green); }
  .status-amber { background: rgba(255,176,32,.16); color: var(--amber); }
  .status-red { background: rgba(255,91,95,.15); color: var(--red); }
  .status-gray { background: var(--card-2); color: var(--sub); }

  .progress-track { height: 7px; border-radius: 6px; background: var(--card-2); overflow:hidden; margin-top: 10px; }
  .progress-fill { height: 100%; border-radius: 6px; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }

  .jelly { -webkit-tap-highlight-color: transparent; transition: transform .12s ease; }
  .jelly:active { transform: scale(.92); }
  @keyframes jellyPop {
    0%   { transform: scale(1); }
    30%  { transform: scale(.86, 1.14); }
    50%  { transform: scale(1.10, .90); }
    70%  { transform: scale(.96, 1.05); }
    100% { transform: scale(1); }
  }
  .jelly.pop { animation: jellyPop .55s cubic-bezier(.36,1.9,.4,1); }

  .buy-btn {
    width: 100%; border: none; border-radius: 999px; padding: 16px; font-weight: 800; font-size: 14.5px;
    background: linear-gradient(120deg, var(--accent) 0%, var(--accent-2) 100%); color: #fff;
    box-shadow: var(--glow); cursor:pointer;
  }
  .buy-btn:disabled{ opacity:.65; }
  .buy-btn.ghost {
    background: var(--card); color: var(--ink); border: 1px solid var(--line); box-shadow: none;
  }
  .btn-row { display:flex; gap: 10px; }
  .btn-row .buy-btn { flex: 1; padding: 13px; font-size: 13px; }

  .empty { text-align: center; padding: 70px 20px; color: var(--sub); }
  .empty .icon { margin-bottom: 12px; display:flex; justify-content:center; color: var(--sub2); }
  .back-btn {
    width: 100%; background: var(--card); border: 1px solid var(--line);
    color: var(--ink); border-radius: 999px; padding: 13px; margin-top: 8px; font-size: 13px; cursor:pointer; font-weight:700;
  }

  .settings-row {
    display:flex; align-items:center; justify-content:space-between; gap: 12px;
    background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 16px; margin-bottom: 10px;
  }
  .settings-row .ttl { font-weight: 800; font-size: 13.5px; }
  .settings-row .sub { font-size: 11px; color: var(--sub); margin-top: 3px; }
  .segmented { display:flex; gap:6px; background: var(--card-2); border-radius: 999px; padding: 4px; flex-shrink:0; }
  .seg-opt { padding: 8px 14px; border-radius: 999px; font-size: 12px; font-weight:700; color: var(--sub); white-space:nowrap; transition: all .2s ease; }
  .seg-opt.active { background: var(--accent); color:#fff; }
  .switch { width: 46px; height: 27px; border-radius: 999px; background: var(--card-2); border:1px solid var(--line); position:relative; flex-shrink:0; }
  .switch::after { content:""; position:absolute; top:3px; right:3px; width:19px; height:19px; border-radius:50%; background: var(--sub); transition: all .22s ease; }
  .switch.on { background: var(--accent-dim); }
  .switch.on::after { right: 22px; background: var(--accent); }
  .id-chip {
    display:inline-flex; align-items:center; gap:6px; background: var(--card-2); border:1px solid var(--line);
    border-radius: 999px; padding: 7px 13px; font-size: 11.5px; color: var(--sub); direction:ltr;
  }

  .tabbar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 8;
    background: var(--card); border-top: 1px solid var(--line); border-radius: 26px 26px 0 0;
    display: flex; align-items:flex-end; padding: 10px 6px calc(6px + env(safe-area-inset-bottom));
    box-shadow: 0 -10px 30px rgba(0,0,0,.25);
  }
  .tab { flex: 1; text-align: center; font-size: 10.5px; color: var(--sub2); padding: 4px 2px; transition: transform .16s cubic-bezier(.34,1.56,.64,1), color .2s ease; }
  .tab:active { transform: scale(.88); }
  .tab .ic { display:flex; justify-content:center; margin-bottom: 4px; }
  .tab.active { color: var(--accent); font-weight: 800; }
  .tab.center { flex: 0 0 64px; margin-top: -30px; }
  .tab.center .ic-circle {
    width: 58px; height: 58px; border-radius: 50%; margin: 0 auto 4px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color:#fff;
    display:flex; align-items:center; justify-content:center; border: 5px solid var(--card);
    box-shadow: 0 12px 26px rgba(255,122,26,.5);
  }
  .tab.center span:last-child { color: var(--accent); font-weight: 800; }

  .skeleton { opacity: .5; text-align: center; padding: 60px 20px; font-size: 12px; color: var(--sub); }

  /* === PATCH_MINIAPP_WHEEL_FAB_V1 === دکمهٔ گردِ شناورِ گردونه (پایین-چپ) */
  #wheel-fab {
    position: fixed; left: 14px; bottom: calc(86px + env(safe-area-inset-bottom)); z-index: 60;
    width: 54px; height: 54px; border-radius: 50%; display: none; align-items: center; justify-content: center;
    font-size: 28px; line-height: 1; cursor: pointer; user-select: none;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    box-shadow: 0 6px 18px rgba(0,0,0,.38); border: 2px solid rgba(255,255,255,.55);
    animation: whfab 2.6s ease-in-out infinite;
  }
  #wheel-fab:active { transform: scale(.9); }
  @keyframes whfab { 0%,100% { transform: translateY(0) rotate(0); } 50% { transform: translateY(-5px) rotate(12deg); } }
  .modal-overlay {
    position: fixed; inset: 0; z-index: 20; background: rgba(0,0,0,.6);
    backdrop-filter: blur(3px); display:flex; align-items:flex-end; justify-content:center; animation: fadeIn .18s ease;
  }
  @keyframes fadeIn { from{opacity:0} to{opacity:1} }
  .modal-card {
    width: 100%; max-width: 480px; background: var(--card); border: 1px solid var(--line);
    border-radius: 26px 26px 0 0; padding: 20px 18px calc(22px + env(safe-area-inset-bottom));
    box-shadow: 0 -10px 30px rgba(0,0,0,.4); animation: slideUp .22s cubic-bezier(.22,1,.36,1);
  }
  @keyframes slideUp { from{ transform: translateY(24px); opacity:0 } to{ transform: translateY(0); opacity:1 } }
  .modal-head { display:flex; align-items:center; justify-content:space-between; margin-bottom: 12px; }
  .modal-close {
    width: 34px; height: 34px; border-radius: 11px; background: var(--card-2); border:1px solid var(--line);
    display:flex; align-items:center; justify-content:center; transform: rotate(180deg); color: var(--sub);
  }
  .sub-box {
    background: var(--card-2); border: 1px solid var(--line); border-radius: 13px; padding: 13px;
    font-size: 11.5px; overflow-wrap: anywhere; direction: ltr; text-align: left; color: var(--ink);
    min-width: 0;
  }
  .card-number-box {
    font-size: 18px; font-weight: 800; letter-spacing: 1px; white-space: nowrap;
    overflow-x: auto; text-align: center; direction: ltr; color: var(--ink);
  }
  .card-holder-name { font-size: 14px; font-weight: 700; color: var(--ink); text-align: center; margin-top: 10px; }
  .empty-note { font-size: 12.5px; color: var(--sub); text-align: center; }

  .profile-hero{ text-align:center; padding: 24px 16px; }
  .profile-avatar{
    width: 82px; height: 82px; margin: 0 auto 14px; border-radius: 50%; font-size: 30px;
    display:flex; align-items:center; justify-content:center; color:#fff; font-weight:900;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: var(--glow);
    border: 3px solid var(--line);
  }
  .profile-name{ font-weight: 900; font-size: 18px; }
  .profile-sub{ font-size: 12.5px; color: var(--sub); margin-top: 4px; direction:ltr; }
  .profile-id{ margin-top: 12px; }
</style>
</head>
<body>
<div class="glow-spot"></div>
<div class="content">
<div id="app"></div>

<div class="tabbar">
  <div class="tab jelly" data-tab="account"><span class="ic" data-ic="user"></span>حساب کاربری</div>
  <div class="tab jelly" data-tab="wallet"><span class="ic" data-ic="wallet"></span>کیف پول</div>
  <div class="tab center jelly" data-tab="buy"><span class="ic-circle" data-ic="cart"></span><span>خرید</span></div>
  <div class="tab jelly" data-tab="services"><span class="ic" data-ic="box"></span><span id="tab-svc-lbl">اشتراک‌ها</span></div>
  <div class="tab jelly active" data-tab="home"><span class="ic" data-ic="home"></span>خانه</div>
</div>
</div>

<script>
(function(){
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { try{tg.ready();}catch(e){} try{tg.expand();}catch(e){}
    try{tg.disableVerticalSwipes();}catch(e){} }

  var BOT_ID = "__BOT_ID__";
  /* === PATCH_MINIAPP_TERMS_V1 === واژگان بر اساسِ نوعِ ربات */
  var T = __TERMS__;
  try { document.getElementById("tab-svc-lbl").textContent = T.tab; } catch(e){}
  /* === PATCH_MINIAPP_VS_NO_SUBS_V1 === رباتِ خدماتِ مجازی «اشتراکِ من/سرویسِ فعال» ندارد */
  if (T.vs) { try { document.body.classList.add('vs'); } catch(e){} try { document.querySelector('.tab[data-tab="services"]').style.display = 'none'; } catch(e){} }
  var BOT_USERNAME = "__BOT_USERNAME__";
  var BOT_LINK = "__BOT_LINK__";
  var INIT_DATA = tg ? tg.initData : "";
  /* === PATCH_MINIAPP_KB_DIRECT_V1 ===
     وقتی مینی‌اپ از دکمهٔ کیبورد باز شود تلگرام initData نمی‌دهد؛ در آن
     حالت از توکنِ امضاشده‌ای که داخلِ خودِ آدرس گذاشته شده استفاده می‌کنیم. */
  var KB_TOKEN = "";
  try {
    KB_TOKEN = new URLSearchParams(window.location.search).get('t') || "";
  } catch (e) { KB_TOKEN = ""; }

  function authHeaders() {
    var h = {};
    if (INIT_DATA) h['X-Init-Data'] = INIT_DATA;
    if (KB_TOKEN) h['X-Kb-Token'] = KB_TOKEN;
    return h;
  }
  var app = document.getElementById('app');
  var state = { tab: 'home', category: null, svcFilter: 'active', svcQuery: '', vcPanel: null };

  // ── ست آیکون خطی ──
  function ic(paths, extra){
    return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"' + (extra||'') + '>' + paths + '</svg>';
  }
  var ICON = {
    home:   ic('<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9a1 1 0 0 0 1 1H9.5v-6h5v6h3a1 1 0 0 0 1-1v-9"/>'),
    cart:   ic('<circle cx="9" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/><path d="M2.5 3h2.2l2 12.2a1.8 1.8 0 0 0 1.8 1.5h9a1.8 1.8 0 0 0 1.75-1.4L21 7.5H6.1"/>', ' width="24" height="24"'),
    box:    ic('<path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5z"/><path d="M3.7 7.7 12 12l8.3-4.3"/><path d="M12 12v9"/>'),
    user:   ic('<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c1.4-3.6 4.4-5.4 7.5-5.4s6.1 1.8 7.5 5.4"/>'),
    wallet: ic('<rect x="3" y="6.5" width="18" height="12.5" rx="3"/><path d="M16.2 12.3h2.3"/><path d="M3.3 9.5h17.4"/>'),
    sun:    ic('<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.4M12 19.1v2.4M4.6 4.6l1.7 1.7M17.7 17.7l1.7 1.7M2.5 12h2.4M19.1 12h2.4M4.6 19.4l1.7-1.7M17.7 6.3l1.7-1.7"/>'),
    moon:   ic('<path d="M20.2 14.6A8.6 8.6 0 1 1 9.4 3.8a7 7 0 0 0 10.8 10.8z"/>'),
    bot:    ic('<rect x="4" y="8.5" width="16" height="11" rx="3.2"/><path d="M12 8.5V5"/><circle cx="12" cy="3.6" r="1.3"/><circle cx="9" cy="14" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="14" r="1.3" fill="currentColor" stroke="none"/><path d="M8 18.2h8"/>'),
    tg:     ic('<path d="m20.5 4-17 6.7c-1 .4-1 1.5.1 1.8l4.2 1.3 1.6 5c.3.9 1.3 1.1 1.9.4l2.3-2.5 4.3 3.2c.8.6 1.9.2 2.1-.8l2.8-13.2c.2-1-.7-1.7-1.6-1.3z" fill="currentColor" stroke="none"/>'),
    empty:  ic('<path d="M3.5 9.5 12 4l8.5 5.5"/><path d="M4.2 10v8.3a1 1 0 0 0 1 1h13.6a1 1 0 0 0 1-1V10"/><path d="M9 14.2h6"/>', ' width="34" height="34"'),
    back:   ic('<path d="M15 5 8 12l7 7"/>'),
    chevron: ic('<path d="M9 5 16 12l-7 7"/>', ' width="18" height="18"'),
    send:   ic('<path d="M4.5 11.5 20 4l-6.5 16-3-6.7z"/><path d="M13.5 13.3 20 4"/>'),
    gift:   ic('<rect x="3" y="9" width="18" height="12" rx="2"/><path d="M3 13h18"/><path d="M12 9v12"/><path d="M12 9c-1.6 0-4-1-4-3.2A2.3 2.3 0 0 1 10.3 3.5C12 3.5 12 6.5 12 9z"/><path d="M12 9c1.6 0 4-1 4-3.2A2.3 2.3 0 0 0 13.7 3.5C12 3.5 12 6.5 12 9z"/>'),
    history:ic('<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 8v4.5l3 2"/>'),
    help:   ic('<circle cx="12" cy="12" r="9"/><path d="M9.5 9.3a2.5 2.5 0 0 1 4.8 1c0 1.6-2.3 1.8-2.3 3.4"/><path d="M12 17.3h.01"/>'),
    guide:  ic('<path d="M4 4.5h13a3 3 0 0 1 3 3v12L17 17H4z"/><path d="M8 9h9M8 12.5h6"/>'),
    plus:   ic('<path d="M12 5v14M5 12h14"/>'),
    search: ic('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.4-3.4"/>'),
    bolt:   ic('<path d="M13 3 5 13.5h5.3L10.5 21 19 10.2h-5.3z"/>'),
    shield: ic('<path d="M12 3 4.5 6v6c0 4.6 3.1 7.9 7.5 9 4.4-1.1 7.5-4.4 7.5-9V6z"/><path d="m9.2 12 2 2 3.6-4"/>'),
    copy:   ic('<rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>'),
  };
  document.querySelectorAll('.tab [data-ic]').forEach(function(el){
    var k = el.getAttribute('data-ic');
    if (ICON[k]) el.innerHTML = ICON[k];
  });

  // ── تم روز/شب/خودکار ──
  function applyTheme(mode){
    var effective = mode;
    if (mode === 'auto') {
      var tgScheme = tg && tg.colorScheme;
      effective = tgScheme || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    }
    document.body.classList.toggle('light', effective === 'light');
    if (tg) {
      try{ tg.setHeaderColor(effective === 'light' ? '#f2f3f5' : '#000000'); }catch(e){}
      try{ tg.setBackgroundColor(effective === 'light' ? '#f2f3f5' : '#000000'); }catch(e){}
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', effective === 'light' ? '#f2f3f5' : '#000000');
  }
  var themeMode = 'dark';
  try { themeMode = localStorage.getItem('bs_theme') || 'dark'; } catch(e) {}
  applyTheme(themeMode);
  function setThemeMode(mode){
    themeMode = mode;
    applyTheme(mode);
    try { localStorage.setItem('bs_theme', mode); } catch(e) {}
  }

  document.addEventListener('click', function(e){
    var j = e.target.closest('.jelly');
    if (!j) return;
    j.classList.remove('pop');
    void j.offsetWidth;
    j.classList.add('pop');
  });

  function api(path, method) {
    return fetch('/api/miniapp/' + BOT_ID + path, { method: method || 'GET', headers: authHeaders() })
      .then(function(r){ if(!r.ok) throw new Error('http_'+r.status); return r.json(); });
  }
  /* === PATCH_MINIAPP_ADMIN_RENAME_V1 ===
     api() فقط متد می‌گیرد و بدنه نمی‌فرستد؛ برایِ POSTِ JSON این جداست
     تا هیچ‌کدام از صدازدن‌هایِ موجودِ api() دست نخورد. */
  function apiJson(path, data) {
    var h = authHeaders();
    h['Content-Type'] = 'application/json';
    return fetch('/api/miniapp/' + BOT_ID + path,
                 { method: 'POST', headers: h, body: JSON.stringify(data) })
      .then(function(r){
        /* === PATCH_VS_MINIAPP_ORDER_V1 ===
           بدنهٔ خطا هم برگردانده می‌شود تا پیامِ دقیقِ سرور (مثلاً «موجودی
           کافی نیست») به کاربر نشان داده شود، نه یک «خطا»ی کلی. */
        return r.json().catch(function(){ return null; }).then(function(body){
          if (!r.ok) {
            var e = new Error('http_' + r.status);
            e.status = r.status; e.body = body;
            throw e;
          }
          return body;
        });
      });
  }
  function apiUpload(path, formData) {
    return fetch('/api/miniapp/' + BOT_ID + path, { method: 'POST', headers: authHeaders(), body: formData })
      .then(function(r){ if(!r.ok) throw new Error('http_'+r.status); return r.json(); });
  }

  function openBot(deepLink) {
    if (tg && tg.openTelegramLink) {
      tg.openTelegramLink(deepLink);
      setTimeout(function(){ try{ tg.close(); }catch(e){} }, 250);
    } else {
      window.location.href = deepLink;
    }
  }

  function setTab(t) {
    state.tab = t;
    document.querySelectorAll('.tab').forEach(function(el){
      el.classList.toggle('active', el.getAttribute('data-tab') === t);
    });
    render();
  }
  document.querySelectorAll('.tab').forEach(function(el){
    el.addEventListener('click', function(){ state.category = null; state.vcPanel = null; setTab(el.getAttribute('data-tab')); });
  });

  function render() {
    app.innerHTML = '<div class="skeleton">در حال بارگذاری…</div>';
    if (state.tab === 'home') return renderHome();
    if (state.tab === 'buy') {
      /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
      if (state.vcPanel) return renderCustomVC(state.vcPanel);
      return state.category === null ? renderCategories() : renderPlans(state.category);
    }
    if (state.tab === 'services') return renderServices();
    if (state.tab === 'wallet') return renderWallet();
    if (state.tab === 'account') return renderProfile();
  }

  function topbarHtml(){
    return '<div class="topbar"><a class="brand jelly" href="'+BOT_LINK+'" target="_blank" rel="noopener">' +
      '<div class="brand-ic">'+ICON.bot+'</div>' +
      '<div><div class="brand-name">'+esc(BOT_USERNAME.replace("@","")||"ربات")+'</div>' +
      '<div class="brand-sub">'+ICON.tg+'<span>@'+esc(BOT_USERNAME.replace("@",""))+'</span></div></div>' +
      '</a>' +
      '<div class="theme-toggle jelly" id="theme-btn">' + (document.body.classList.contains('light') ? ICON.sun : ICON.moon) + '</div></div>';
  }
  function bindTopbar(){
    var btn = document.getElementById('theme-btn');
    if (btn) btn.onclick = function(){
      var order = ['dark','light','auto']; var next = order[(order.indexOf(themeMode)+1) % order.length];
      setThemeMode(next);
      btn.innerHTML = document.body.classList.contains('light') ? ICON.sun : ICON.moon;
    };
  }

  function statBox(v, label){ return '<div class="stat-box"><b>'+v+'</b><span>'+label+'</span></div>'; }

  function renderHome() {
    api('/home').then(function(d){
      var tgUser = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
      var photo = tgUser && tgUser.photo_url;
      var avatarHtml = photo
        ? '<img src="'+esc(photo)+'" style="width:100%;height:100%;object-fit:cover;border-radius:50%"/>'
        : (d.user.name||'؟').charAt(0);
      var quickTiles = tile('cart', 'خرید '+T.item+' جدید', 'بدون رفتن به ربات', 'go-buy') +
            (T.vs ? '' : tile('box', 'اشتراک‌های من', 'مدیریت '+T.items, 'go-services'));
      if (d.referral_link) quickTiles += tile('gift', 'دعوت دوستان', 'پاداش هر معرفی', 'go-referral');
      if (d.support_enabled) quickTiles += tile('help', 'پشتیبانی', 'ارتباط با تیم ما', 'go-support');
      if (d.wheel && d.wheel.enabled) quickTiles += tile('gift', esc(d.wheel.title||'🎡 گردونهٔ شانس'), 'بچرخون و جایزه بگیر', 'go-wheel');   /* PATCH_MINIAPP_WHEEL_V1 */

      var txnsHtml = '';
      if (d.recent_txns && d.recent_txns.length) {
        txnsHtml = '<div class="section-title"><span>تاریخچهٔ تراکنش‌ها</span><span class="more jelly" id="go-history-all">مشاهدهٔ همه</span></div>';
        d.recent_txns.forEach(function(t){
          txnsHtml += '<div class="list-item" style="cursor:default">' +
            '<div class="row"><div class="list-ic">'+ICON.history+'</div><div>' +
            '<div class="ttl">'+esc(t.title)+'</div><div class="sub">'+esc(t.date)+'</div>' +
            '</div></div><div style="text-align:left"><div class="'+(t.failed?'':'')+'" style="font-weight:800;font-size:13px;'+(t.failed?'text-decoration:line-through;color:var(--sub2)':'color:var(--ink)')+'">'+t.amount+' ت</div>' +
            '<span class="status-badge '+(t.failed?'status-red':'status-green')+'" style="margin-top:4px;display:inline-block">'+esc(t.status)+'</span></div></div>';
        });
      }

      app.innerHTML = topbarHtml() +
        '<div class="wrap" style="padding-top:6px">' +
          '<div class="row" style="justify-content:space-between">' +
            '<div><div class="greet-name">سلام، '+esc(d.user.name)+' 👋</div>' +
            '<div class="greet-sub">به ربات '+esc(BOT_USERNAME.replace("@",""))+' خوش اومدی</div></div>' +
            '<div class="greet-avatar">'+avatarHtml+'</div>' +
          '</div>' +
          '<div class="wallet-card"><div class="wallet-label">موجودی کیف پول</div>' +
            '<div class="wallet-amount">' + d.wallet_balance + ' تومان</div>' +
            '<button class="wallet-btn jelly" id="go-topup">'+ICON.plus+' افزایش موجودی</button>' +
          '</div>' +
          '<div class="stat-grid">' +
            statBox(d.total_orders, 'مجموع خرید') +
            statBox(d.total_spent, 'کل خرید (ت)') +
            (T.vs ? '' : statBox(d.active_services, T.item+' فعال')) +
          '</div>' +
          '<div class="section-title"><span>دسترسی سریع</span></div>' +
          '<div class="grid2">' + quickTiles + '</div>' +
          txnsHtml +
        '</div>';
      bindTopbar();
      document.getElementById('go-buy').onclick = function(){ setTab('buy'); };
      var _gsv = document.getElementById('go-services');
      if (_gsv) _gsv.onclick = function(){ setTab('services'); };
      var refEl = document.getElementById('go-referral');
      if (refEl) refEl.onclick = function(){ showReferralModal(d.referral); };
      var whEl = document.getElementById('go-wheel');   /* PATCH_MINIAPP_WHEEL_V1 */
      if (whEl) whEl.onclick = function(){ openWheel(); };
      var supEl = document.getElementById('go-support');
      if (supEl) supEl.onclick = function(){ openBot(d.support_link || BOT_LINK); };
      var histEl = document.getElementById('go-history-all');
      if (histEl) histEl.onclick = function(){ setTab('wallet'); };
      document.getElementById('go-topup').onclick = function(){ showTopupModal(); };
    }).catch(showError);
  }
  function tile(iconKey, ttl, sub, id){
    return '<div class="tile jelly" id="'+id+'"><div class="tile-ic">'+ICON[iconKey]+'</div>' +
      '<div><div class="tile-ttl">'+ttl+'</div><div class="tile-sub">'+sub+'</div></div></div>';
  }

  function showReferralModal(ref){
    if (!ref || !ref.link) return;
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay jelly';
    var qr = 'https://api.qrserver.com/v1/create-qr-code/?size=170x170&data=' + encodeURIComponent(ref.link);
    overlay.innerHTML =
      '<div class="modal-card">' +
        '<div class="modal-head"><div class="ttl" style="font-size:15px;font-weight:800">دعوت از دوستان</div>' +
        '<div class="modal-close jelly" id="ref-close">'+ICON.back+'</div></div>' +
        '<div style="font-size:12.5px;color:var(--sub);margin-bottom:14px">لینک زیر رو برای دوستانت بفرست؛ به‌ازای هر عضو جدید پاداش می‌گیری.</div>' +
        '<div class="grid2" style="margin-bottom:6px">' +
          '<div class="stat-box"><b>'+ref.joins+'</b><span>عضو دعوت‌شده</span></div>' +
          '<div class="stat-box"><b>'+esc(ref.reward)+'</b><span>'+esc(ref.reward_label||'پاداش هر نفر (ت)')+'</span></div>' +
        '</div>' +
        (ref.reward_note ? '<div style="font-size:12px;color:var(--sub);margin-top:10px;line-height:1.7">💡 '+esc(ref.reward_note)+'</div>' : '') +
        '<div class="row" style="align-items:flex-start;gap:12px;margin-top:14px">' +
          '<div class="sub-box" id="ref-link" style="flex:1">'+esc(ref.link)+'</div>' +
          '<img src="'+qr+'" style="width:64px;height:64px;border-radius:10px;background:#fff;padding:4px"/>' +
        '</div>' +
        '<div class="btn-row" style="margin-top:10px">' +
          '<button class="back-btn jelly" id="ref-copy" style="flex:1"><span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی لینک</span></span></button>' +
          '<button class="buy-btn jelly" id="ref-share" style="flex:1"><span class="row" style="justify-content:center;gap:6px">'+ICON.send+'<span>اشتراک‌گذاری</span></span></button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    document.getElementById('ref-close').onclick = function(){ overlay.remove(); };
    overlay.addEventListener('click', function(e){ if (e.target === overlay) overlay.remove(); });
    document.getElementById('ref-copy').onclick = function(){
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(ref.link);
      var b = document.getElementById('ref-copy');
      b.innerHTML = '<span class="row" style="justify-content:center;gap:6px">✓<span>کپی شد</span></span>';
      setTimeout(function(){ b.innerHTML = '<span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی لینک</span></span>'; }, 1400);
    };
    document.getElementById('ref-share').onclick = function(){
      var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(ref.link) + '&text=' + encodeURIComponent('بیا با این لینک عضو شو 🎁');
      if (tg && tg.openTelegramLink) tg.openTelegramLink(shareUrl);
      else window.open(shareUrl, '_blank');
    };
  }

  function showTopupModal(){
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay jelly';
    overlay.innerHTML =
      '<div class="modal-card" id="topup-body">' +
        '<div class="modal-head"><div class="ttl" style="font-size:15px;font-weight:800">شارژ کیف پول</div>' +
        '<div class="modal-close jelly" id="topup-close">'+ICON.back+'</div></div>' +
        '<div id="topup-content" class="skeleton" style="padding:26px 0">در حال دریافت اطلاعات…</div>' +
      '</div>';
    document.body.appendChild(overlay);
    document.getElementById('topup-close').onclick = function(){ overlay.remove(); };
    overlay.addEventListener('click', function(e){ if (e.target === overlay) overlay.remove(); });

    var box = document.getElementById('topup-content');
    var chosenAmount = 0;
    var chosenFile = null;

    function stepAmount(info){
      box.className = '';
      var presetsHtml = '';
      info.presets.forEach(function(p){
        presetsHtml += '<div class="filter-chip jelly" data-amt="'+p+'" style="flex:1;text-align:center;padding:12px 6px">'+p.toLocaleString('en-US')+' ت</div>';
      });
      box.innerHTML =
        '<div style="font-size:12.5px;color:var(--sub);margin-bottom:12px">مبلغ شارژ رو انتخاب کن یا خودت وارد کن:</div>' +
        '<div class="row" style="gap:8px;flex-wrap:wrap">' + presetsHtml + '</div>' +
        '<div class="search-box" style="margin-top:12px"><input id="topup-custom" type="number" inputmode="numeric" placeholder="مبلغ دلخواه (تومان)"/></div>' +
        '<button class="buy-btn jelly" id="topup-next" style="margin-top:16px">ادامه</button>';
      document.querySelectorAll('[data-amt]').forEach(function(el){
        el.onclick = function(){
          document.querySelectorAll('[data-amt]').forEach(function(o){ o.classList.remove('active'); });
          el.classList.add('active');
          document.getElementById('topup-custom').value = el.getAttribute('data-amt');
        };
      });
      document.getElementById('topup-next').onclick = function(){
        var v = parseInt(document.getElementById('topup-custom').value, 10);
        if (!v || v < 1000) { alert('مبلغ معتبر وارد کن.'); return; }
        chosenAmount = v;
        stepReceipt(info);
      };
    }

    function stepReceipt(info){
      box.innerHTML =
        '<div class="stat-box" style="margin-bottom:14px"><b>'+chosenAmount.toLocaleString('en-US')+' تومان</b><span>مبلغ انتخابی</span></div>' +
        '<div class="section-title" style="margin-top:0"><span>شمارهٔ کارت مقصد</span></div>' +
        '<div class="card" style="padding:16px;margin-top:8px">' +
          '<div class="card-number-box" id="topup-card">'+esc(info.card_number || '—')+'</div>' +
          (info.card_holder ? '<div class="card-holder-name">به نام: '+esc(info.card_holder)+'</div>' : '') +
          (info.card_number2 ? '<div class="card-holder-name" style="margin-top:10px">🔁 یا کارتِ دوم:</div><div class="card-number-box">'+esc(info.card_number2)+'</div>' +
            (info.card_holder2 ? '<div class="card-holder-name">به نام: '+esc(info.card_holder2)+'</div>' : '') : '') +
          '<button class="back-btn jelly" id="topup-copy-card" style="margin-top:12px;width:100%">' +
            '<span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی شمارهٔ کارت</span></span>' +
          '</button>' +
        '</div>' +
        '<div class="empty-note" style="text-align:right;margin-top:10px;line-height:1.9">' +
          '💡 مبلغ <b style="color:var(--ink)">'+chosenAmount.toLocaleString('en-US')+' تومان</b> رو دقیقاً به شمارهٔ کارت بالا کارت‌به‌کارت کن، ' +
          'بعد از انتقال، از رسید پرداخت یه عکس بگیر و پایین آپلودش کن. بعد از تایید ادمین، همین مبلغ خودکار به کیف پولت اضافه می‌شه.' +
        '</div>' +
        '<div class="section-title"><span>آپلود رسید پرداخت</span></div>' +
        '<label class="list-item jelly" id="topup-upload-box" style="cursor:pointer">' +
          '<div class="row"><div class="list-ic">'+ICON.send+'</div><div>' +
          '<div class="ttl" id="topup-file-label">انتخاب عکس رسید</div><div class="sub">فرمت jpg/png، حداکثر ۱۰ مگابایت</div>' +
          '</div></div>' +
          '<input type="file" id="topup-file-input" accept="image/*" style="display:none"/>' +
        '</label>' +
        '<div id="topup-preview" style="margin-top:10px"></div>' +
        '<div class="btn-row" style="margin-top:16px">' +
          '<button class="back-btn jelly" id="topup-back" style="flex:1">بازگشت</button>' +
          '<button class="buy-btn jelly" id="topup-send" style="flex:2" disabled>ارسال رسید</button>' +
        '</div>';
      document.getElementById('topup-back').onclick = function(){ stepAmount(info); };
      document.getElementById('topup-copy-card').onclick = function(){
        if (info.card_number && navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(info.card_number);
        var b = document.getElementById('topup-copy-card');
        b.innerHTML = '<span class="row" style="justify-content:center;gap:6px">✓<span>کپی شد</span></span>';
        setTimeout(function(){ b.innerHTML = '<span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی شمارهٔ کارت</span></span>'; }, 1200);
      };
      var fileInput = document.getElementById('topup-file-input');
      fileInput.onchange = function(){
        var f = fileInput.files && fileInput.files[0];
        var sendBtn = document.getElementById('topup-send');
        if (!f) { chosenFile = null; sendBtn.disabled = true; return; }
        chosenFile = f;
        document.getElementById('topup-file-label').textContent = f.name;
        var url = URL.createObjectURL(f);
        document.getElementById('topup-preview').innerHTML = '<img src="'+url+'" style="width:100%;border-radius:14px;max-height:220px;object-fit:cover"/>';
        sendBtn.disabled = false;
      };
      document.getElementById('topup-send').onclick = function(){
        if (!chosenFile) return;
        var sendBtn = document.getElementById('topup-send');
        sendBtn.disabled = true;
        sendBtn.textContent = 'در حال ارسال…';
        var fd = new FormData();
        fd.append('amount', chosenAmount);
        fd.append('receipt', chosenFile);
        apiUpload('/wallet/topup/submit', fd).then(function(){
          box.innerHTML =
            '<div class="empty" style="padding:30px 10px">' +
              '<div class="icon">'+ICON.shield+'</div>' +
              '<div style="font-weight:800;color:var(--ink)">رسید ثبت شد ✅</div>' +
              '<div style="font-size:12px;margin-top:6px">بعد از تایید ادمین، کیف پولت به‌صورت خودکار شارژ می‌شود.</div>' +
            '</div>' +
            '<button class="buy-btn jelly" id="topup-done" style="margin-top:6px">باشه</button>';
          document.getElementById('topup-done').onclick = function(){ overlay.remove(); if (state.tab==='wallet') render(); };
        }).catch(function(){
          sendBtn.disabled = false;
          sendBtn.textContent = 'ارسال رسید';
          alert('ارسال رسید ناموفق بود. دوباره امتحان کن.');
        });
      };
    }

    api('/wallet/topup/init').then(function(info){
      if (!info.enabled) {
        box.className = '';
        box.innerHTML = '<div class="empty-note" style="padding:20px 0">شارژ کارت‌به‌کارت در حال حاضر غیرفعال است.</div>' +
          '<button class="buy-btn jelly" id="topup-fallback" style="margin-top:10px">ارتباط با پشتیبانی</button>';
        document.getElementById('topup-fallback').onclick = function(){ overlay.remove(); openBot(info.support_link || BOT_LINK); };
        return;
      }
      stepAmount(info);
    }).catch(function(){
      overlay.remove();
      showError();
    });
  }

  function renderWallet() {
    api('/home').then(function(d){
      var html = topbarHtml() + '<div class="wrap">' +
        '<div class="section-title" style="margin-top:8px"><span>کیف پول</span></div>' +
        '<div class="wallet-card"><div class="wallet-label">موجودی فعلی</div>' +
          '<div class="wallet-amount">' + d.wallet_balance + ' تومان</div>' +
          '<button class="wallet-btn jelly" id="wl-topup" style="margin-top:16px;width:100%;justify-content:center;display:flex">'+ICON.plus+' شارژ کیف پول</button>' +
        '</div>' +
        '<div class="section-title"><span>خلاصهٔ خرید</span></div>' +
        '<div class="stat-grid" style="grid-template-columns:1fr 1fr">' +
          statBox(d.total_orders, 'مجموع خرید') + statBox(d.total_spent, 'کل خرید (تومان)') +
        '</div>';
      if (d.recent_txns && d.recent_txns.length) {
        html += '<div class="section-title"><span>تاریخچهٔ تراکنش‌ها</span></div>';
        d.recent_txns.forEach(function(t){
          html += '<div class="list-item" style="cursor:default">' +
            '<div class="row"><div class="list-ic">'+ICON.history+'</div><div>' +
            '<div class="ttl">'+esc(t.title)+'</div><div class="sub">'+esc(t.date)+'</div>' +
            '</div></div><div style="text-align:left"><div style="font-weight:800;font-size:13px;'+(t.failed?'text-decoration:line-through;color:var(--sub2)':'color:var(--ink)')+'">'+t.amount+' ت</div>' +
            '<span class="status-badge '+(t.failed?'status-red':'status-green')+'" style="margin-top:4px;display:inline-block">'+esc(t.status)+'</span></div></div>';
        });
      }
      html += '</div>';
      app.innerHTML = html;
      bindTopbar();
      document.getElementById('wl-topup').onclick = function(){ showTopupModal(); };
    }).catch(showError);
  }

  function renderCategories() {
    /* === PATCH_MINIAPP_VSERVICES_V1 === */
    /* صفحهٔ «خرید» حالا هم پلن‌هایِ VPN را نشان می‌دهد هم محصولاتِ خدماتِ
       مجازی. هر ربات فقط چیزی را می‌بیند که مستأجرش فعال کرده، پس برایِ
       ربات‌هایِ VPN هیچ تغییرِ ظاهری‌ای ندارد و برعکس. */
    Promise.all([
      api('/categories').catch(function(){ return {categories: []}; }),
      api('/vservices').catch(function(){ return {items: []}; }),
      api('/trial').catch(function(){ return {available: false}; })
    ]).then(function(res){
      var cats = (res[0] && res[0].categories) || [];
      var vs = (res[1] && res[1].items) || [];
      var trial = res[2] || {available: false};
      if (!cats.length && !vs.length && !trial.available) {
        app.innerHTML = topbarHtml() + emptyBlock('فعلاً '+T.item+'ی برای فروش تعریف نشده');
        bindTopbar(); return;
      }
      var html = topbarHtml() + '<div class="wrap">';
      /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
      if (trial.available) {
        html += '<div class="list-item jelly" id="go-trial" style="border-color:var(--accent)">' +
          '<div class="row"><div class="list-ic">'+ICON.gift+'</div>' +
          '<div><div class="ttl">'+esc(trial.title||'🎁 تست رایگان VPN')+'</div>' +
          '<div style="font-size:11px;color:var(--sub)">رایگان و یک‌بار برای هر کاربر</div></div></div>' +
          ICON.chevron+'</div>';
      }
      if (vs.length) {
        html += '<div class="section-title" style="margin-top:8px"><span>خدمات مجازی</span></div>' +
          '<div style="font-size:12px;color:var(--sub);margin:-4px 2px 4px">' +
          'برای ادامهٔ خرید، وارد ربات می‌شوی</div>';
        vs.forEach(function(it){
          html += '<div class="list-item jelly" data-vs="'+esc(it.deep_link||'')+'" data-vsact="'+esc(it.key||'')+'" data-mb="'+esc(String(it.mb_id||''))+'">' +
            '<div class="row"><div class="list-ic" style="font-size:17px">'+esc(it.icon)+'</div>' +
            '<div><div class="ttl">'+esc(it.title)+'</div>' +
            '<div style="font-size:11px;color:var(--sub)">'+esc(it.desc||'')+'</div></div></div>' +
            ICON.chevron+'</div>';
        });
      }
      if (cats.length) {
        html += '<div class="section-title" style="margin-top:14px"><span>'+T.buy+'</span></div>' +
          '<div style="font-size:12px;color:var(--sub);margin:-4px 2px 4px">نوع پلن خود را انتخاب کنید</div>';
        cats.forEach(function(c){
          html += '<div class="list-item jelly" data-cat="'+c.id+'"><div class="row"><div class="list-ic">'+ICON.box+'</div>' +
            '<div class="ttl">'+esc(c.title)+'</div></div>'+ICON.chevron+'</div>';
        });
      }
      /* === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 === */
      html += '<div id="mp-zone"></div>';
      html += '</div>';
      app.innerHTML = html;
      bindTopbar();
      renderManualProducts();
      /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
      var trialEl = document.getElementById('go-trial');
      if (trialEl) trialEl.onclick = function(){
        trialEl.style.pointerEvents = 'none';
        api('/trial/link').then(function(r){ openBot(r.deep_link); })
          .catch(function(){ trialEl.style.pointerEvents = ''; showError(); });
      };
      document.querySelectorAll('[data-cat]').forEach(function(el){
        el.onclick = function(){ state.category = parseInt(el.getAttribute('data-cat'),10); render(); };
      });
      /* === PATCH_MINIAPP_VSERVICES_V1 === */
      document.querySelectorAll('[data-vs]').forEach(function(el){
        el.onclick = function(){
          /* === PATCH_VS_MINIAPP_ORDER_V1 ===
             محصولاتی که سفارشِ درون‌اپی دارند همین‌جا باز می‌شوند؛ بقیه
             مثلِ قبل به چتِ ربات می‌روند. */
          if (el.getAttribute('data-vsact') === 'stars_shop') { openStarsOrder(); return; }
          if (el.getAttribute('data-vsact') === 'smm_shop') { openSmmOrder(0); return; }   /* PATCH_MINIAPP_SMM_ORDER_V1 */
          if (el.getAttribute('data-vsact') === 'smm_btn') { openSmmOrder(parseInt(el.getAttribute('data-mb'),10)||0); return; }
          var link = el.getAttribute('data-vs');
          if (!link) return;
          if (window.Telegram && Telegram.WebApp && Telegram.WebApp.openTelegramLink) {
            Telegram.WebApp.openTelegramLink(link);
            Telegram.WebApp.close();
          } else { window.open(link, '_blank'); }
        };
      });
    }).catch(showError);
  }

  function renderPlans(catId) {
    api('/plans/' + catId).then(function(d){
      var packs = d.packs || [];
      var custom = d.custom || [];
      if (!d.plans.length && !packs.length && !custom.length) { app.innerHTML = topbarHtml() + emptyBlock('پلنی در این دسته نیست'); bindTopbar(); return; }
      var html = topbarHtml() + '<div class="wrap">';
      /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
      if (custom.length) {
        html += '<div class="section-title" style="margin-top:8px"><span>🎯 حجم/روز دلخواه</span></div>';
        custom.forEach(function(c){
          html += '<div class="list-item jelly" data-vc="'+esc(c.panel_key)+'"><div class="row"><div class="list-ic">'+ICON.bolt+'</div>' +
            '<div><div class="ttl">بسازِ خودت</div><div class="sub">'+esc(c.label)+' — با ➕/➖ حجم و مدت رو تنظیم کن</div></div></div>'+ICON.chevron+'</div>';
        });
      }
      if (d.plans.length || packs.length) {
        html += '<div class="section-title" style="margin-top:14px"><span>یکی از '+T.items+' رو انتخاب کن</span></div>';
      }
      d.plans.forEach(function(p){
        var vol = p.unlimited_volume ? 'نامحدود' : (p.volume_gb + ' گیگ');
        var days = p.unlimited_days ? 'بی‌انقضا' : (p.days + ' روز');
        html += '<div class="plan-card">' +
          '<div class="plan-top"><span>'+esc(p.title)+'</span><span class="plan-price">'+p.price+' تومان</span></div>' +
          '<div class="chip-row"><span class="chip">'+vol+'</span><span class="chip">'+days+'</span>' +
          (p.limit_ip ? '<span class="chip">'+p.limit_ip+' کاربر</span>' : '') + '</div>' +
          '<button class="buy-btn jelly" data-plan="'+p.id+'"><span class="row" style="justify-content:center;gap:8px">'+ICON.cart+'<span>خرید این پلن</span></span></button>' +
        '</div>';
      });
      /* === PATCH_MINIAPP_STOCK_CATS_V1 === بسته‌های آمادهٔ انبار */
      if (packs.length) {
        html += '<div class="section-title" style="margin-top:14px"><span>📦 بسته‌های آماده</span></div>';
        packs.forEach(function(p){
          var vol = p.unlimited_volume ? 'نامحدود' : (p.volume_gb + ' گیگ');
          var days = p.unlimited_days ? 'بی‌انقضا' : (p.days + ' روز');
          html += '<div class="plan-card">' +
            '<div class="plan-top"><span>'+esc(p.title)+'</span><span class="plan-price">'+p.price+' تومان</span></div>' +
            '<div class="chip-row"><span class="chip">'+vol+'</span><span class="chip">'+days+'</span>' +
            (p.limit_ip ? '<span class="chip">'+p.limit_ip+' کاربر</span>' : '') +
            '<span class="chip">موجودی '+p.stock+'</span></div>' +
            '<button class="buy-btn jelly" data-pack="'+p.pack+'"><span class="row" style="justify-content:center;gap:8px">'+ICON.cart+'<span>خرید این بسته</span></span></button>' +
          '</div>';
        });
      }
      html += '<button class="back-btn jelly" id="go-back"><span class="row" style="justify-content:center;gap:6px">'+ICON.back+'<span>بازگشت</span></span></button></div>';
      app.innerHTML = html;
      bindTopbar();
      document.getElementById('go-back').onclick = function(){ state.category = null; render(); };
      /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
      document.querySelectorAll('[data-vc]').forEach(function(el){
        el.onclick = function(){ state.vcPanel = el.getAttribute('data-vc'); render(); };
      });
      document.querySelectorAll('[data-pack]').forEach(function(el){
        el.onclick = function(){
          var btn = el;
          btn.disabled = true;
          btn.innerHTML = 'در حال انتقال به ربات…';
          api('/buypack/' + btn.getAttribute('data-pack'))
            .then(function(r){ openBot(r.deep_link); })
            .catch(function(){ btn.disabled = false; showError(); });
        };
      });
      document.querySelectorAll('[data-plan]').forEach(function(el){
        el.onclick = function(){
          var btn = el;
          btn.disabled = true;
          btn.innerHTML = 'در حال انتقال به ربات…';
          var pid = btn.getAttribute('data-plan');
          api('/buy/' + pid).then(function(r){ openBot(r.deep_link); }).catch(function(){ btn.disabled = false; showError(); });
        };
      });
    }).catch(showError);
  }

  /* === PATCH_MINIAPP_VC_TRIAL_V1 === */
  /* رابطِ +/− برایِ خریدِ حجم/روزِ دلخواه داخلِ مینی‌اپ. هر بار که کاربر
     ➕/➖ می‌زند، دوباره از سرور (که خودش از تابعِ قیمت‌گذاریِ خودِ ربات
     استفاده می‌کند) قیمتِ تازه را می‌گیریم — هیچ محاسبهٔ قیمتی اینجا
     دوباره نوشته نمی‌شود. خودِ پرداخت، مثلِ همیشه، با دیپ‌لینک به چتِ
     ربات ادامه پیدا می‌کند. */
  function renderCustomVC(panelKey) {
    var vc = { gb: null, days: null, count: null, loading: false };
    function load(gb, days, count){
      if (vc.loading) return;
      vc.loading = true;
      var qs = [];
      if (gb != null) qs.push('gb=' + gb);
      if (days != null) qs.push('days=' + days);
      if (count != null) qs.push('count=' + count);
      api('/vc/' + panelKey + (qs.length ? '?' + qs.join('&') : ''))
        .then(function(d){ vc.loading = false; vc.gb = d.gb; vc.days = d.days; vc.count = d.count; paint(d); })
        .catch(function(){ vc.loading = false; showError(); });
    }
    function row(label, val, unit, dec, inc, locked){
      if (locked) {
        return '<div class="list-item" style="margin-bottom:10px"><div class="ttl">'+label+': '+val+' '+unit+' 🔒</div></div>';
      }
      return '<div class="btn-row" style="margin-bottom:10px">' +
        '<button class="back-btn jelly vc-btn" data-act="'+dec+'" style="flex:0 0 52px;color:var(--red)">➖</button>' +
        '<div class="stat-box" style="flex:1"><b>'+val+' '+unit+'</b><span>'+label+'</span></div>' +
        '<button class="back-btn jelly vc-btn" data-act="'+inc+'" style="flex:0 0 52px;color:var(--green)">➕</button>' +
      '</div>';
    }
    function paint(d){
      var html = topbarHtml() + '<div class="wrap">' +
        '<div class="section-title" style="margin-top:8px"><span>🎯 حجم/روز دلخواه</span></div>' +
        '<div class="plan-card">' +
        row('حجم', d.gb, 'گیگ', 'gb-', 'gb+', d.locked_gb) +
        row('مدت', d.days, 'روز', 'd-', 'd+', d.locked_days) +
        (d.max_count > 1 ? row('تعداد', d.count, 'عدد', 'c-', 'c+', false) : '') +
        '<div class="plan-top" style="margin-top:6px"><span>مجموع</span><span class="plan-price">'+d.total+' تومان</span></div>' +
        '</div>' +
        '<button class="buy-btn jelly" id="vc-pay"><span class="row" style="justify-content:center;gap:8px">'+ICON.cart+'<span>ادامه در ربات</span></span></button>' +
        '<button class="back-btn jelly" id="vc-back" style="margin-top:10px"><span class="row" style="justify-content:center;gap:6px">'+ICON.back+'<span>بازگشت</span></span></button>' +
        '</div>';
      app.innerHTML = html;
      bindTopbar();
      document.getElementById('vc-back').onclick = function(){ state.vcPanel = null; render(); };
      document.querySelectorAll('.vc-btn').forEach(function(el){
        el.onclick = function(){
          var g = vc.gb, dd = vc.days, c = vc.count;
          var step = 1; // === PATCH_MINIAPP_VC_TRIAL_V1 === گامِ واقعی را سرور خودش رعایت می‌کند
          switch (el.getAttribute('data-act')) {
            case 'gb-': g = Math.max(0, g - (d.step_gb || 1)); break;
            case 'gb+': g = g + (d.step_gb || 1); break;
            case 'd-':  dd = Math.max(0, dd - (d.step_days || 1)); break;
            case 'd+':  dd = dd + (d.step_days || 1); break;
            case 'c-':  c = Math.max(1, c - 1); break;
            case 'c+':  c = c + 1; break;
          }
          load(g, dd, c);
        };
      });
      var payBtn = document.getElementById('vc-pay');
      payBtn.onclick = function(){
        payBtn.disabled = true;
        payBtn.innerHTML = 'در حال انتقال به ربات…';
        api('/vc/' + panelKey + '/buy?gb=' + vc.gb + '&days=' + vc.days + '&count=' + vc.count)
          .then(function(r){ openBot(r.deep_link); })
          .catch(function(){ payBtn.disabled = false; showError(); });
      };
    }
    load(null, null, null);
  }

  function svcStatus(sv){
    if (sv.expired) return { key:'expired', label:'منقضی‌شده', cls:'status-red' };
    if (sv.expires_at) {
      var diff = (new Date(sv.expires_at + 'T00:00:00') - new Date()) / 86400000;
      if (diff <= 3) return { key:'soon', label:'در آستانهٔ انقضا', cls:'status-amber' };
    }
    return { key:'active', label:'فعال', cls:'status-green' };
  }

  function renderServices() {
    api('/services').then(function(d){
      if (!d.services.length) { app.innerHTML = topbarHtml() + emptyBlock(T.item+' فعالی نداری', 'از تبِ «خرید» همین‌جا یه سرویس بگیر'); bindTopbar(); return; }
      var all = d.services.map(function(sv){ sv._st = svcStatus(sv); return sv; });

      function draw(){
        var filtered = all.filter(function(sv){
          if (state.svcFilter !== 'all' && sv._st.key !== state.svcFilter) return false;
          if (state.svcQuery && sv.title.toLowerCase().indexOf(state.svcQuery.toLowerCase()) === -1) return false;
          return true;
        });
        var html = topbarHtml() + '<div class="wrap">' +
          '<div class="section-title" style="margin-top:8px"><span>'+T.mine+'</span></div>' +
          '<div style="font-size:12px;color:var(--sub);margin:-4px 2px 10px">'+all.length+' اشتراک</div>' +
          '<div class="search-box">'+ICON.search+'<input id="svc-search" placeholder="جستجوی نام اشتراک..." value="'+esc(state.svcQuery)+'"/></div>' +
          '<div class="filter-row">' +
            filterChip('active','فعال') + filterChip('soon','در آستانه انقضا') + filterChip('expired','منقضی') + filterChip('all','همه') +
          '</div>';
        if (!filtered.length) {
          html += emptyBlock('چیزی پیدا نشد', 'فیلتر یا جستجو رو تغییر بده');
        } else {
          filtered.forEach(function(sv){
            html += '<div class="list-item jelly" data-svc="'+sv.id+'">' +
              '<div class="row"><div class="list-ic">'+ICON.box+'</div><div>' +
              '<div class="ttl">'+esc(sv.title)+'</div>' +
              '<div class="sub">'+(sv.expires_at || 'بدون انقضا')+'</div>' +
              '</div></div><span class="status-badge '+sv._st.cls+'">'+sv._st.label+'</span></div>';
          });
        }
        html += '</div>';
        app.innerHTML = html;
        bindTopbar();
        document.querySelectorAll('.filter-chip').forEach(function(el){
          el.onclick = function(){ state.svcFilter = el.getAttribute('data-f'); draw(); };
        });
        var s = document.getElementById('svc-search');
        if (s) { s.oninput = function(){ state.svcQuery = s.value; }; s.onkeyup = function(){ state.svcQuery = s.value; draw(); document.getElementById('svc-search').focus(); }; }
        document.querySelectorAll('[data-svc]').forEach(function(el){
          el.onclick = function(){
            var sv = all.find(function(x){ return String(x.id) === el.getAttribute('data-svc'); });
            if (sv) showServiceDetail(sv);
          };
        });
      }
      function filterChip(key, label){
        return '<div class="filter-chip jelly'+(state.svcFilter===key?' active':'')+'" data-f="'+key+'">'+label+'</div>';
      }
      draw();
    }).catch(showError);
  }

  function showServiceDetail(sv){
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay jelly';
    var st = sv._st || svcStatus(sv);
    overlay.innerHTML =
      '<div class="modal-card" id="modal-body">' +
        '<div class="modal-head"><div class="ttl" style="font-size:15px;font-weight:800">'+esc(sv.title)+'</div>' +
        '<div class="modal-close jelly" id="modal-close">'+ICON.back+'</div></div>' +
        '<span class="status-badge '+st.cls+'">'+st.label+'</span>' +
        '<div id="modal-live" class="skeleton" style="padding:26px 0">در حال دریافت اطلاعات زنده…</div>' +
      '</div>';
    document.body.appendChild(overlay);
    document.getElementById('modal-close').onclick = function(){ overlay.remove(); };
    overlay.addEventListener('click', function(e){ if (e.target === overlay) overlay.remove(); });

    var liveBox = document.getElementById('modal-live');

    function daysLeftFrom(dateStr){
      if (!dateStr) return null;
      var d = new Date(dateStr.replace(' ', 'T'));
      if (isNaN(d.getTime())) return null;
      var diff = Math.ceil((d - new Date()) / 86400000);
      return diff;
    }

    function renderStatic(){
      var subBlock = sv.sub_link
        ? subLinkBlock(sv.sub_link)
        : '<div class="empty-note" style="padding:14px 0">این سرویس ساب‌لینک ذخیره‌شده‌ای ندارد.</div>';
      var dleft = daysLeftFrom(sv.expires_at);
      liveBox.className = '';
      liveBox.innerHTML =
        '<div class="chip-row" style="margin-top:14px">' +
          '<span class="chip">'+(sv.volume_gb ? sv.volume_gb+' گیگ' : 'نامحدود')+'</span>' +
          '<span class="chip">'+(sv.days ? sv.days+' روز' : 'بی‌انقضا')+'</span>' +
        '</div>' +
        '<div class="grid2" style="margin-top:6px">' +
          '<div class="stat-box"><b>'+(dleft!==null ? (dleft>0?dleft:0) : '—')+'</b><span>روز باقی‌مانده</span></div>' +
          '<div class="stat-box"><b>'+(sv.expires_at || 'بدون انقضا')+'</b><span>تاریخ تمدید</span></div>' +
        '</div>' +
        subBlock +
        actionButtons() ;
      bindModalActions(sv.sub_link);
    }

    function renderLive(data){
      liveBox.className = '';
      var subBlock = data.sub_link
        ? subLinkBlock(data.sub_link)
        : '<div class="empty-note" style="padding:14px 0">این سرویس ساب‌لینک ذخیره‌شده‌ای ندارد.</div>';
      var progressBlock = '';
      if (!data.unlimited && data.cap_gb) {
        progressBlock =
          '<div class="card" style="padding:14px;margin-top:16px">' +
            '<div class="row" style="justify-content:space-between"><span class="tile-sub">ظرفیت کل</span><span class="tile-sub">باقی‌مانده حجم</span></div>' +
            '<div class="row" style="justify-content:space-between;margin-top:2px">' +
              '<b style="font-size:15px">'+data.cap_gb+' گیگ</b><b style="font-size:15px;color:var(--accent)">'+data.remaining_gb+' گیگ</b>' +
            '</div>' +
            '<div class="progress-track"><div class="progress-fill" style="width:'+data.percent+'%"></div></div>' +
          '</div>';
      }
      var dleft = daysLeftFrom(data.expiry);
      liveBox.innerHTML =
        progressBlock +
        '<div class="grid2" style="margin-top:14px">' +
          '<div class="stat-box"><b>'+(dleft!==null ? (dleft>0?dleft:0) : '—')+'</b><span>روز باقی‌مانده</span></div>' +
          '<div class="stat-box"><b>'+esc(data.expiry)+'</b><span>تاریخ تمدید</span></div>' +
        '</div>' +
        '<div class="stat-grid" style="grid-template-columns:1fr 1fr;margin-top:10px">' +
          statBox(data.used_gb+' گیگ', 'مصرف‌شده') +
          statBox((data.remaining_gb!=null ? data.remaining_gb+' گیگ' : 'نامحدود'), 'باقی‌مانده') +
        '</div>' +
        subBlock +
        actionButtons();
      bindModalActions(data.sub_link);
    }

    function subLinkBlock(link){
      var qr = 'https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=' + encodeURIComponent(link);
      return '<div class="section-title" style="margin-top:16px"><span>لینک اشتراک (Subscription)</span></div>' +
        '<div class="row" style="align-items:flex-start;gap:12px">' +
          '<div class="sub-box" id="modal-sub" style="flex:1">'+esc(link)+'</div>' +
          '<img src="'+qr+'" style="width:64px;height:64px;border-radius:10px;background:#fff;padding:4px"/>' +
        '</div>' +
        '<div class="btn-row" style="margin-top:8px">' +
          '<button class="back-btn jelly" id="modal-copy" style="flex:1"><span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی</span></span></button>' +
          '<button class="back-btn jelly" id="modal-open" style="flex:1"><span class="row" style="justify-content:center;gap:6px">'+ICON.send+'<span>باز کردن لینک</span></span></button>' +
        '</div>';
    }

    function actionButtons(){
      return '<div class="btn-row" style="margin-top:16px">' +
          '<button class="buy-btn ghost jelly" id="modal-reset" style="flex:1"><span class="row" style="justify-content:center;gap:6px">'+ICON.history+'<span>لینک جدید</span></span></button>' +
          '<button class="buy-btn ghost jelly" id="modal-delete" style="flex:1;color:var(--red)"><span class="row" style="justify-content:center;gap:6px">'+ICON.back.replace('m15','m9')+'<span>حذف کانفیگ</span></span></button>' +
        '</div>' +
        '<button class="buy-btn jelly" id="modal-renew" style="margin-top:10px">' +
          '<span class="row" style="justify-content:center;gap:8px">'+ICON.bolt+'<span>تمدید در ربات</span></span></button>';
    }

    function bindModalActions(link){
      var copyBtn = document.getElementById('modal-copy');
      if (copyBtn) copyBtn.onclick = function(){
        if (link && navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(link);
        copyBtn.innerHTML = '<span class="row" style="justify-content:center;gap:6px">✓<span>کپی شد</span></span>';
        setTimeout(function(){ copyBtn.innerHTML = '<span class="row" style="justify-content:center;gap:6px">'+ICON.copy+'<span>کپی</span></span>'; }, 1400);
      };
      var openBtn = document.getElementById('modal-open');
      if (openBtn) openBtn.onclick = function(){ if (link) { if (tg && tg.openLink) tg.openLink(link); else window.open(link,'_blank'); } };
      var renewBtn = document.getElementById('modal-renew');
      if (renewBtn) renewBtn.onclick = function(){ overlay.remove(); openBot(BOT_LINK); };

      var resetBtn = document.getElementById('modal-reset');
      if (resetBtn) resetBtn.onclick = function(){
        resetBtn.disabled = true;
        resetBtn.innerHTML = 'در حال تولید…';
        api('/services/' + sv.id + '/reset_link', 'POST').then(function(r){
          sv.sub_link = r.sub_link;
          if (sv.cfg_token) { fetch('/api/cfg/'+sv.cfg_token).then(function(x){return x.json();}).then(function(d2){ if(d2&&d2.ok) renderLive(d2); else renderStatic(); }); }
          else renderStatic();
        }).catch(function(){
          resetBtn.disabled = false;
          resetBtn.innerHTML = '<span class="row" style="justify-content:center;gap:6px">'+ICON.history+'<span>لینک جدید</span></span>';
          alert('تولید لینک جدید ناموفق بود. دوباره امتحان کن.');
        });
      };
      var delBtn = document.getElementById('modal-delete');
      if (delBtn) delBtn.onclick = function(){
        if (!confirm('این کانفیگ برای همیشه حذف می‌شود. مطمئنی؟')) return;
        delBtn.disabled = true;
        delBtn.innerHTML = 'در حال حذف…';
        api('/services/' + sv.id + '/delete', 'POST').then(function(){
          overlay.remove();
          renderServices();
        }).catch(function(){
          delBtn.disabled = false;
          alert('حذف ناموفق بود. دوباره امتحان کن.');
        });
      };
    }

    if (sv.cfg_token) {
      fetch('/api/cfg/' + sv.cfg_token).then(function(r){ return r.json(); }).then(function(data){
        if (data && data.ok) renderLive(data); else renderStatic();
      }).catch(renderStatic);
    } else {
      renderStatic();
    }
  }

  /* ═══ === PATCH_MINIAPP_MANUAL_PRODUCTS_V1 === محصولاتِ دستی ═══ */
  function renderManualProducts(){
    var host = document.getElementById('mp-zone');
    if (!host) return;
    api('/products').then(function(d){
      if (!d || !d.ok || !d.items.length) return;
      var html = '<div class="section-title" style="margin-top:14px"><span>🛍 '+T.items+'</span></div>';
      d.items.forEach(function(it){
        html += '<div class="list-item jelly mp-item" data-mp="'+it.id+'">'+
          '<div class="row">'+
            (it.image
              ? '<img src="'+esc(it.image)+'" style="width:42px;height:42px;border-radius:10px;object-fit:cover"/>'
              : '<div class="list-ic">'+ICON.box+'</div>')+
            '<div><div class="ttl">'+esc(it.title)+'</div>'+
              '<div class="sub">'+esc(it.desc || '')+
                (it.desc ? ' — ' : '')+it.price_fa+' تومان</div></div>'+
          '</div>'+ICON.chevron+'</div>';
      });
      host.innerHTML = html;
      host.querySelectorAll('[data-mp]').forEach(function(el){
        el.onclick = function(){
          var it = d.items.filter(function(x){ return x.id == el.getAttribute('data-mp'); })[0];
          if (it) openProductBuy(it, d.balance_fa);
        };
      });
    }).catch(function(){});
  }

  function openProductBuy(it, balanceFa){
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;'+
      'display:flex;align-items:flex-end';
    ov.innerHTML = '<div id="mp-box" style="background:var(--card);width:100%;'+
      'border-radius:20px 20px 0 0;padding:18px;max-height:92vh;overflow:auto;'+
      'padding-bottom:calc(18px + env(safe-area-inset-bottom))"></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e){ if (e.target === ov) ov.remove(); });
    var box = ov.querySelector('#mp-box');
    function close(){ try{ document.body.removeChild(ov); }catch(e){} }

    box.innerHTML =
      (it.image ? '<img src="'+esc(it.image)+'" style="width:100%;max-height:170px;'+
        'object-fit:cover;border-radius:14px;margin-bottom:12px"/>' : '')+
      '<div style="font-size:15px;font-weight:800;margin-bottom:4px">'+esc(it.title)+'</div>'+
      (it.desc ? '<div style="font-size:12.5px;color:var(--sub);line-height:1.9;'+
        'margin-bottom:10px">'+esc(it.desc)+'</div>' : '')+
      '<div style="background:var(--card-2);border-radius:12px;padding:12px;'+
        'font-size:13.5px;font-weight:800;margin-bottom:12px">قیمت: '+it.price_fa+' تومان'+
        '<div style="font-size:11.5px;color:var(--sub);font-weight:400;margin-top:4px">'+
          'موجودیِ تو: '+balanceFa+' تومان</div></div>'+
      (it.input_label
        ? '<div style="font-size:11.5px;color:var(--sub);margin-bottom:6px">'+
            esc(it.input_label)+'</div>'+
          '<input id="mp-in" style="width:100%;height:44px;border-radius:12px;'+
            'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
            'padding:0 12px;font-size:13px;font-family:inherit;margin-bottom:12px"/>'
        : '')+
      '<div style="display:flex;gap:8px">'+
        '<button id="mp-cancel" style="flex:1;padding:13px;border-radius:12px;'+
          'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
          'font-family:inherit;font-size:13px;font-weight:800">انصراف</button>'+
        '<button id="mp-go" class="buy-btn jelly" style="flex:2;margin:0">خرید</button>'+
      '</div>';
    box.querySelector('#mp-cancel').onclick = close;
    box.querySelector('#mp-go').onclick = function(){
      var btn = box.querySelector('#mp-go');
      var inEl = box.querySelector('#mp-in');
      btn.disabled = true; btn.textContent = 'در حال ثبت…';
      apiJson('/products/'+it.id+'/buy', { input: inEl ? inEl.value : '' })
        .then(function(r){
          box.innerHTML =
            '<div class="empty" style="padding:28px 10px">'+
              '<div class="icon">'+ICON.shield+'</div>'+
              '<div style="font-weight:800;color:var(--ink)">سفارش ثبت شد ✅</div>'+
              '<div style="font-size:12px;margin-top:8px;line-height:2">'+
                esc(r.title)+'<br/>مبلغ: '+r.price_fa+' تومان<br/>'+
                'موجودیِ جدید: '+r.balance_fa+' تومان<br/>'+
                'کد پیگیری: <code>'+esc(r.tracking)+'</code></div>'+
              '<div style="font-size:11.5px;color:var(--sub);margin-top:10px">'+
                'بعد از تحویل توسطِ ادمین، به تو اطلاع داده می‌شود.</div>'+
            '</div>'+
            '<button class="buy-btn jelly" id="mp-done" style="margin-top:6px">باشه</button>';
          box.querySelector('#mp-done').onclick = function(){ close(); render(); };
        })
        .catch(function(err){
          btn.disabled = false; btn.textContent = 'خرید';
          var m = (err && err.body && err.body.message) || 'ثبتِ سفارش ناموفق بود.';
          try{ tg && tg.showAlert(m); }catch(e){ alert(m); }
        });
    };
  }

  /* ───── مدیریتِ محصولاتِ دستی ───── */
  function renderMpAdmin(host){
    api('/admin/products').then(function(d){
      if (!d || !d.ok) return;
      var html = '<div style="font-size:12px;font-weight:800;margin:14px 0 7px">🛍 '+T.items+'ِ دستی</div>'+
        '<div style="font-size:11.5px;color:var(--sub);line-height:1.9;margin-bottom:9px">'+
          'سرویسِ دلخواهت رو با عکس و قیمت اضافه کن؛ مشتری همین‌جا می‌خره و '+
          'سفارشش برایِ تو می‌آد.</div>'+
        '<button class="buy-btn jelly" id="mp-new" style="margin:0 0 10px">➕ '+T.item+'ِ جدید</button>';
      (d.items || []).forEach(function(p){
        html += '<div class="adm-line" style="display:flex;align-items:center;gap:9px;'+
          'padding:9px 0;border-bottom:1px solid var(--line)">'+
          (p.image ? '<img src="'+esc(p.image)+'" style="width:30px;height:30px;'+
            'border-radius:8px;object-fit:cover"/>' : '<span style="font-size:16px">📦</span>')+
          '<span style="flex:1;min-width:0;font-size:12.5px;font-weight:700">'+esc(p.title)+
            '<span style="color:var(--sub);font-weight:400"> — '+p.price_fa+' ت'+
            (p.stock === null ? '' : ' · موجودی '+p.stock)+
            (p.active ? '' : ' · غیرفعال')+'</span></span>'+
          '<span class="mp-ed" data-mp-ed=\''+esc(JSON.stringify(p))+'\' '+
            'style="font-size:12px;color:var(--accent);font-weight:800">✏️</span></div>';
      });
      var pend = (d.orders || []).filter(function(o){ return o.status === 'pending'; });
      if (pend.length) {
        html += '<div style="font-size:12px;font-weight:800;margin:14px 0 7px">'+
          '📋 سفارش‌های در انتظار ('+pend.length+')</div>';
        pend.forEach(function(o){
          html += '<div style="padding:9px 0;border-bottom:1px solid var(--line)">'+
            '<div style="font-size:12.5px;font-weight:700">#'+o.id+' · '+esc(o.title)+
              ' — '+o.amount_fa+' ت</div>'+
            '<div style="font-size:11px;color:var(--sub);margin-top:2px">'+esc(o.buyer)+
              (o.input ? ' · '+esc(o.input) : '')+' · '+esc(o.tracking)+'</div>'+
            '<div style="display:flex;gap:6px;margin-top:7px">'+
              '<span class="mp-act" data-mp-o="'+o.id+'" data-mp-a="deliver" '+
                'style="flex:1;text-align:center;padding:7px;border-radius:9px;'+
                'background:var(--card-2);border:1px solid var(--line);font-size:11px;'+
                'font-weight:800">✅ تحویل شد</span>'+
              '<span class="mp-act" data-mp-o="'+o.id+'" data-mp-a="reject" '+
                'style="flex:1;text-align:center;padding:7px;border-radius:9px;'+
                'background:var(--card-2);border:1px solid var(--line);font-size:11px;'+
                'font-weight:800">↩️ لغو و عودت</span>'+
            '</div></div>';
        });
      }
      host.innerHTML = html;
      host.querySelector('#mp-new').onclick = function(){ editProduct(null); };
      host.querySelectorAll('.mp-ed').forEach(function(el){
        el.onclick = function(){
          try { editProduct(JSON.parse(el.getAttribute('data-mp-ed'))); } catch(e){}
        };
      });
      host.querySelectorAll('.mp-act').forEach(function(el){
        el.onclick = function(){
          el.style.pointerEvents = 'none';
          apiJson('/admin/product/'+el.getAttribute('data-mp-o')+'/'+el.getAttribute('data-mp-a'), {})
            .then(function(){ renderMpAdmin(host); })
            .catch(function(){ el.style.pointerEvents = ''; });
        };
      });
    }).catch(function(){});
  }

  function editProduct(p){
    p = p || {title:'',desc:'',image:'',price:0,input_label:'',stock:null,active:true};
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;'+
      'display:flex;align-items:flex-end';
    ov.innerHTML = '<div id="pe-box" style="background:var(--card);width:100%;'+
      'border-radius:20px 20px 0 0;padding:18px;max-height:92vh;overflow:auto;'+
      'padding-bottom:calc(18px + env(safe-area-inset-bottom))"></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e){ if (e.target === ov) ov.remove(); });
    var box = ov.querySelector('#pe-box');
    function close(){ try{ document.body.removeChild(ov); }catch(e){} }
    function fld(id,label,val,type,ph){
      return '<div style="font-size:11.5px;color:var(--sub);margin:8px 0 5px">'+label+'</div>'+
        '<input id="'+id+'" type="'+(type||'text')+'" value="'+esc(val==null?'':val)+'" '+
        (ph?'placeholder="'+esc(ph)+'" ':'')+
        'style="width:100%;height:44px;border-radius:12px;background:var(--card-2);'+
        'border:1px solid var(--line);color:var(--ink);padding:0 12px;font-size:13px;'+
        'font-family:inherit"/>';
    }
    box.innerHTML =
      '<div style="font-size:15px;font-weight:800;margin-bottom:6px">'+
        (p.id ? '✏️ ویرایشِ سرویس' : '➕ سرویسِ جدید')+'</div>'+
      fld('pe-title','نامِ '+T.item+' *',p.title)+
      fld('pe-desc','توضیحِ کوتاه',p.desc)+
      fld('pe-image','لینکِ عکس',p.image,'text','https://…')+
      fld('pe-price','قیمت (تومان) *',p.price,'number')+
      fld('pe-input','ورودی از مشتری (خالی = ندارد)',p.input_label,'text','مثلاً: لینکِ پیج')+
      fld('pe-stock','موجودی (خالی = نامحدود)',p.stock==null?'':p.stock,'number')+
      '<div style="font-size:11.5px;color:var(--sub);margin:8px 0 5px">وضعیت</div>'+
      '<select id="pe-active" style="width:100%;height:44px;border-radius:12px;'+
        'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
        'padding:0 12px;font-size:13px;font-family:inherit">'+
        '<option value="1"'+(p.active?' selected':'')+'>فعال</option>'+
        '<option value="0"'+(p.active?'':' selected')+'>غیرفعال</option></select>'+
      '<div style="display:flex;gap:8px;margin-top:14px">'+
        '<button id="pe-cancel" style="flex:1;padding:13px;border-radius:12px;'+
          'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
          'font-family:inherit;font-size:13px;font-weight:800">انصراف</button>'+
        '<button id="pe-save" class="buy-btn jelly" style="flex:2;margin:0">💾 ذخیره</button>'+
      '</div>'+
      (p.id ? '<button id="pe-del" style="width:100%;margin-top:8px;padding:12px;'+
        'border-radius:12px;background:transparent;border:1px solid var(--line);'+
        'color:var(--sub);font-family:inherit;font-size:12px">🗑 حذف از کاتالوگ</button>' : '');
    box.querySelector('#pe-cancel').onclick = close;
    var delEl = box.querySelector('#pe-del');
    if (delEl) delEl.onclick = function(){
      apiJson('/admin/product/'+p.id+'/delete', {}).then(function(){
        close(); var h=document.getElementById('mp-admin-zone'); if(h) renderMpAdmin(h);
      }).catch(function(){});
    };
    box.querySelector('#pe-save').onclick = function(){
      var g = function(i){ return (box.querySelector('#'+i)||{}).value || ''; };
      if (!g('pe-title').trim()) { try{ tg && tg.showAlert('نامِ سرویس را وارد کن.'); }catch(e){} return; }
      var btn = box.querySelector('#pe-save');
      btn.disabled = true; btn.textContent = 'در حال ذخیره…';
      apiJson('/admin/product', {
        id: p.id || null, title: g('pe-title'), desc: g('pe-desc'),
        image: g('pe-image'), price: g('pe-price'),
        input_label: g('pe-input'),
        stock: g('pe-stock') === '' ? null : g('pe-stock'),
        active: g('pe-active') === '1'
      }).then(function(){
        close(); var h=document.getElementById('mp-admin-zone'); if(h) renderMpAdmin(h);
      }).catch(function(){
        btn.disabled = false; btn.textContent = '💾 ذخیره';
        try{ tg && tg.showAlert('ذخیره نشد.'); }catch(e){}
      });
    };
  }

  /* ═══ === PATCH_VS_MINIAPP_ORDER_V1 === سفارشِ استارز داخلِ مینی‌اپ ═══ */
  /* === PATCH_MINIAPP_WHEEL_V1 === گردونهٔ شانس */
  function openWheel(){
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;display:flex;align-items:flex-end';
    ov.innerHTML = '<div id="wh-box" style="background:var(--card);width:100%;border-radius:20px 20px 0 0;padding:18px;max-height:94vh;overflow:auto;padding-bottom:calc(18px + env(safe-area-inset-bottom));text-align:center">'+
      '<div class="empty-note" style="padding:24px 0">در حال بارگذاری…</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e){ if (e.target === ov) ov.remove(); });
    var box = ov.querySelector('#wh-box');
    var COLORS = ['#ef4444','#f59e0b','#10b981','#3b82f6','#8b5cf6','#ec4899','#14b8a6','#f97316','#6366f1','#84cc16','#06b6d4','#d946ef'];
    var rot = 0, busy = false, waitTimer = null;
    function drawWheel(cv, sl){
      var S = 300, dpr = window.devicePixelRatio || 1;
      cv.width = S*dpr; cv.height = S*dpr; cv.style.width = S+'px'; cv.style.height = S+'px';
      var c = cv.getContext('2d'); c.scale(dpr, dpr);
      var n = sl.length, A = 2*Math.PI/n, R = S/2 - 4;
      for (var i=0;i<n;i++){
        var a0 = -Math.PI/2 + i*A, a1 = a0 + A;
        c.beginPath(); c.moveTo(S/2,S/2); c.arc(S/2,S/2,R,a0,a1); c.closePath();
        c.fillStyle = COLORS[i % COLORS.length]; c.fill();
        c.strokeStyle = 'rgba(255,255,255,.85)'; c.lineWidth = 2; c.stroke();
        c.save(); c.translate(S/2,S/2); c.rotate(a0 + A/2);
        c.fillStyle = '#fff'; c.font = 'bold 12px sans-serif'; c.textAlign = 'right'; c.textBaseline = 'middle';
        var t = sl[i].label; if (t.length > 14) t = t.slice(0,13) + '…';
        c.fillText(t, R - 12, 0); c.restore();
      }
      c.beginPath(); c.arc(S/2,S/2,16,0,2*Math.PI); c.fillStyle = '#fff'; c.fill();
    }
    function fmtWait(sec){
      var h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s2 = sec%60;
      return (h?h+':':'') + (m<10&&h?'0':'') + m + ':' + (s2<10?'0':'') + s2;
    }
    function paint(d){
      if (waitTimer){ clearInterval(waitTimer); waitTimer = null; }
      if (!d || !d.ok || !d.enabled){
        box.innerHTML = '<div class="empty-note" style="padding:20px 0">گردونه فعلاً فعال نیست.</div>'; return;
      }
      var info = d.max_total ? ('تعدادِ چرخش‌هایِ تو: '+d.total+' از '+d.max_total) :
        (d.per_period+' بار در هر '+d.period_hours+' ساعت');
      box.innerHTML =
        '<div style="font-size:16px;font-weight:800;margin-bottom:4px">'+esc(d.title)+'</div>'+
        '<div style="font-size:12px;color:var(--sub);margin-bottom:12px">'+esc(info)+'</div>'+
        '<div style="position:relative;width:300px;height:312px;margin:0 auto">'+
          '<div style="position:absolute;top:0;left:50%;margin-left:-12px;z-index:2;width:0;height:0;border-left:12px solid transparent;border-right:12px solid transparent;border-top:22px solid var(--accent)"></div>'+
          '<canvas id="wh-cv" style="position:absolute;top:12px;left:0;border-radius:50%;box-shadow:0 6px 24px rgba(0,0,0,.35);transition:transform 4.2s cubic-bezier(.12,.7,.1,1)"></canvas>'+
        '</div>'+
        '<div id="wh-msg" style="min-height:44px;margin:12px 0 8px;font-size:14px;font-weight:700"></div>'+
        '<button id="wh-go" class="buy-btn jelly" style="width:100%"></button>';
      var cv = box.querySelector('#wh-cv');
      drawWheel(cv, d.slices);
      cv.style.transform = 'rotate('+rot+'deg)';
      var go = box.querySelector('#wh-go'), msg = box.querySelector('#wh-msg');
      function setBtn(){
        if (d.exhausted){ go.disabled = true; go.textContent = 'سقفِ چرخش‌هایِ تو تمام شده'; return; }
        if (d.left <= 0){
          go.disabled = true;
          var left = d.wait;
          go.textContent = 'نوبتِ بعدی: ' + fmtWait(left);
          waitTimer = setInterval(function(){
            left -= 1;
            if (left <= 0){ clearInterval(waitTimer); waitTimer = null; refresh(); return; }
            go.textContent = 'نوبتِ بعدی: ' + fmtWait(left);
          }, 1000);
          return;
        }
        go.disabled = false; go.textContent = '🎡 بچرخون' + (d.left > 1 ? ' ('+d.left+' بار مانده)' : '');
      }
      setBtn();
      go.onclick = function(){
        if (busy || go.disabled) return;
        busy = true; go.disabled = true; msg.textContent = '';
        apiJson('/wheel/spin', {}).then(function(r){
          var n = d.slices.length, A = 360/n, center = (r.index + 0.5) * A;
          rot = rot + 360*6 + (360 - (rot % 360)) - center;
          cv.style.transform = 'rotate('+rot+'deg)';
          setTimeout(function(){
            busy = false;
            var txt;
            if (r.type === 'none') txt = '😅 این بار پوچ شد؛ دفعهٔ بعد!';
            else if (r.type === 'balance') txt = '🎉 برنده شدی: ' + esc(r.label) + ' به کیف‌پولت اضافه شد!';
            else txt = '🎉 برنده شدی: ' + esc(r.label) + '<br>کدِ تخفیف: <b style="user-select:all">' + esc(r.code||'') + '</b> (۳۰ روز اعتبار، یک‌بار مصرف)';
            msg.innerHTML = txt;
            d.left = r.left; d.wait = r.wait; d.exhausted = r.exhausted; d.total = (d.total||0) + 1;
            setBtn();
          }, 4400);
        }).catch(function(e){
          busy = false;
          var b = e && e.body;
          msg.textContent = (b && b.msg) ? b.msg : 'مشکلی پیش اومد؛ دوباره امتحان کن.';
          refresh();
        });
      };
    }
    function refresh(){ api('/wheel').then(paint).catch(function(){ paint(null); }); }
    refresh();
  }

  function openStarsOrder(){
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;'+
      'display:flex;align-items:flex-end';
    ov.innerHTML = '<div id="st-box" style="background:var(--card);width:100%;'+
      'border-radius:20px 20px 0 0;padding:18px;max-height:92vh;overflow:auto;'+
      'padding-bottom:calc(18px + env(safe-area-inset-bottom))">'+
      '<div class="empty-note" style="padding:24px 0">در حال بارگذاری…</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e){ if (e.target === ov) ov.remove(); });
    var box = ov.querySelector('#st-box');
    function close(){ try{ document.body.removeChild(ov); }catch(e){} }

    api('/vs/stars/info').then(function(d){
      if (!d || !d.ok || !d.enabled){
        box.innerHTML = '<div class="empty-note" style="padding:20px 0">'+
          'خریدِ استارز فعلاً فعال نیست.</div>';
        return;
      }
      var qty = d.min;
      function total(){ return Math.round(qty * d.rate); }
      function paint(){
        box.innerHTML =
          '<div style="font-size:15px;font-weight:800;margin-bottom:4px">⭐ خریدِ استارزِ تلگرام</div>'+
          '<div style="font-size:12px;color:var(--sub);margin-bottom:14px">'+
            'نرخ: هر ⭐ = '+Math.round(d.rate).toLocaleString("en-US")+' تومان · موجودیِ تو: '+d.balance_fa+' تومان</div>'+
          '<div style="font-size:11.5px;color:var(--sub);margin-bottom:6px">تعداد استارز '+
            '('+d.min.toLocaleString("en-US")+' تا '+d.max.toLocaleString("en-US")+')</div>'+
          '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'+
            '<button id="st-plus" style="width:44px;height:44px;border-radius:50%;'+
              'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
              'font-size:20px;font-family:inherit">+</button>'+
            '<input id="st-qty" type="number" value="'+qty+'" style="flex:1;height:44px;'+
              'border-radius:12px;background:var(--card-2);border:1px solid var(--line);'+
              'color:var(--ink);text-align:center;font-size:16px;font-weight:800;'+
              'font-family:inherit"/>'+
            '<button id="st-minus" style="width:44px;height:44px;border-radius:50%;'+
              'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
              'font-size:20px;font-family:inherit">−</button>'+
          '</div>'+
          '<div style="font-size:11.5px;color:var(--sub);margin-bottom:6px">'+
            'مقصد (خالی = برایِ خودم)</div>'+
          '<input id="st-dest" dir="ltr" placeholder="@username" style="width:100%;height:44px;'+
            'border-radius:12px;background:var(--card-2);border:1px solid var(--line);'+
            'color:var(--ink);padding:0 12px;font-size:13px;font-family:inherit;margin-bottom:12px"/>'+
          '<div id="st-total" style="background:var(--card-2);border-radius:12px;padding:12px;'+
            'font-size:13.5px;font-weight:800;margin-bottom:12px">مبلغ: '+total().toLocaleString("en-US")+' تومان</div>'+
          '<div style="display:flex;gap:8px">'+
            '<button id="st-cancel" style="flex:1;padding:13px;border-radius:12px;'+
              'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
              'font-family:inherit;font-size:13px;font-weight:800">انصراف</button>'+
            '<button id="st-go" class="buy-btn jelly" style="flex:2;margin:0">ثبتِ سفارش</button>'+
          '</div>'+
          '<div style="font-size:11px;color:var(--sub);margin-top:10px;line-height:1.9">'+
            'برایِ کاربرِ دیگر، آن شخص باید یوزرنیمِ عمومی داشته باشد و قبلاً رباتت را استارت کرده باشد.</div>';
        var input = box.querySelector('#st-qty');
        function sync(v){
          qty = Math.max(d.min, Math.min(d.max, parseInt(v,10) || d.min));
          input.value = qty;
          box.querySelector('#st-total').textContent = 'مبلغ: '+total().toLocaleString("en-US")+' تومان';
        }
        var step = Math.max(1, Math.round(d.min/2));
        box.querySelector('#st-plus').onclick  = function(){ sync(qty + step); };
        box.querySelector('#st-minus').onclick = function(){ sync(qty - step); };
        input.oninput = function(){ sync(input.value); };
        box.querySelector('#st-cancel').onclick = close;
        box.querySelector('#st-go').onclick = function(){
          var btn = box.querySelector('#st-go');
          btn.disabled = true; btn.textContent = 'در حال ثبت…';
          apiJson('/vs/stars/order', {
            qty: qty, dest_username: box.querySelector('#st-dest').value || ''
          }).then(function(r){
            box.innerHTML =
              '<div class="empty" style="padding:28px 10px">'+
                '<div class="icon">'+ICON.shield+'</div>'+
                '<div style="font-weight:800;color:var(--ink)">سفارش ثبت شد ✅</div>'+
                '<div style="font-size:12px;margin-top:8px;line-height:2">'+
                  'مبلغ: '+r.price_fa+' تومان<br/>'+
                  'موجودیِ جدید: '+r.balance_fa+' تومان<br/>'+
                  'کد پیگیری: <code>'+esc(r.tracking)+'</code></div>'+
                '<div style="font-size:11.5px;color:var(--sub);margin-top:10px">'+
                  'بعد از ارسالِ استارز توسطِ ادمین، به تو اطلاع داده می‌شود.</div>'+
              '</div>'+
              '<button class="buy-btn jelly" id="st-done" style="margin-top:6px">باشه</button>';
            box.querySelector('#st-done').onclick = function(){ close(); render(); };
          }).catch(function(err){
            btn.disabled = false; btn.textContent = 'ثبتِ سفارش';
            var m = (err && err.body && err.body.message) || 'ثبتِ سفارش ناموفق بود.';
            try{ tg && tg.showAlert(m); }catch(e){ alert(m); }
          });
        };
      }
      paint();
    }).catch(function(){
      box.innerHTML = '<div class="empty-note" style="padding:20px 0">خطا در بارگذاری.</div>';
    });
  }

  /* ═══ === PATCH_MINIAPP_SMM_ORDER_V1 === سفارشِ خدماتِ SMM داخلِ مینی‌اپ ═══
     پلتفرم ← نوع (دسته) ← سرویس ← لینک و تعداد ← ثبت. همان مسیرِ ربات است. */
  function openSmmOrder(mbId){
    mbId = mbId || 0;
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;display:flex;align-items:flex-end';
    ov.innerHTML = '<div id="sm-box" style="background:var(--card);width:100%;border-radius:22px 22px 0 0;'+
      'padding:16px 16px calc(18px + env(safe-area-inset-bottom));height:88vh;overflow:auto"></div>';
    document.body.appendChild(ov);
    var box = ov.querySelector('#sm-box');
    function close(){ try{ document.body.removeChild(ov); }catch(e){} }
    ov.addEventListener('click', function(e){ if (e.target === ov) close(); });
    var BTN = 'width:36px;height:36px;border-radius:50%;background:var(--card-2);border:1px solid var(--line);color:var(--ink);font-size:15px;font-family:inherit;flex:none';
    var INP = 'width:100%;height:46px;border-radius:12px;background:var(--card-2);border:1px solid var(--line);color:var(--ink);padding:0 12px;font-size:13.5px;font-family:inherit;margin-bottom:10px';
    function fa(n){ try{ return Number(n||0).toLocaleString('en-US'); }catch(e){ return n; } }
    function head(title, sub, backFn){
      box.innerHTML = '<div style="display:flex;align-items:center;gap:9px;margin-bottom:14px">'+
        (backFn ? '<button id="sm-back" style="'+BTN+'">→</button>' : '')+
        '<div style="flex:1;min-width:0"><div style="font-size:15px;font-weight:800">'+title+'</div>'+
        (sub ? '<div style="font-size:11.5px;color:var(--sub);margin-top:3px">'+sub+'</div>' : '')+'</div>'+
        '<button id="sm-x" style="'+BTN+'">✕</button></div><div id="sm-body"></div>';
      box.querySelector('#sm-x').onclick = close;
      if (backFn) box.querySelector('#sm-back').onclick = backFn;
      return box.querySelector('#sm-body');
    }
    function item(ic, ttl, sub, id){
      return '<div class="list-item jelly" data-i="'+id+'"><div class="row"><div class="list-ic" style="font-size:17px">'+ic+'</div>'+
        '<div><div class="ttl">'+esc(ttl)+'</div>'+(sub?'<div style="font-size:11px;color:var(--sub)">'+esc(sub)+'</div>':'')+'</div></div>'+ICON.chevron+'</div>';
    }
    var tree = null;

    function viewPlatforms(){
      var body = head('🛰 خدماتِ شبکه‌هایِ اجتماعی', 'دسته را انتخاب کن', null);
      body.innerHTML = '<div class="empty-note" style="padding:24px 0">در حال بارگذاری…</div>';
      api('/vs/smm/tree'+(mbId?'?mb='+mbId:'')).then(function(d){
        if (!d || !d.ok){ body.innerHTML = '<div class="empty-note" style="padding:20px 0">این بخش فعلاً فعال نیست.</div>'; return; }
        tree = d;
        if (d.mode === 'single' && d.single){ viewOrder({key:'',label:''}, {key:'__all__',label:''}, d.single, true); return; }
        if (!d.groups.length){ body.innerHTML = '<div class="empty-note" style="padding:20px 0">فعلاً سرویسی برایِ فروش نیست.</div>'; return; }
        var isCat = d.mode === 'cat';
        body.innerHTML = '<div style="font-size:11.5px;color:var(--sub);margin:-6px 2px 10px">موجودیِ تو: '+d.balance_fa+' تومان</div>'+
          d.groups.map(function(p,i){ return item(isCat ? '📦' : '🌐', p.label, p.count+' سرویس', i); }).join('');
        body.querySelectorAll('[data-i]').forEach(function(el){
          el.onclick = function(){
            var g = tree.groups[+el.getAttribute('data-i')];
            /* عمقِ ۲ (تنظیمِ ادمین): مرحلهٔ «نوع» رد می‌شود و سرویس‌ها مستقیم می‌آیند */
            if (d.mode === 'cat' && d.depth === 2) viewServices(g, {key:'__all__', label:''});
            else viewTypes(g);
          };
        });
      }).catch(function(){ body.innerHTML = '<div class="empty-note" style="padding:20px 0">خطا در بارگذاری.</div>'; });
    }

    function viewTypes(p){
      var body = head(esc(p.label), 'نوعِ سرویس را انتخاب کن', viewPlatforms);
      if (p.types.length === 1){ viewServices(p, p.types[0], viewPlatforms); return; }
      body.innerHTML = p.types.map(function(t,i){ return item('📂', t.label, t.count+' سرویس', i); }).join('');
      body.querySelectorAll('[data-i]').forEach(function(el){
        el.onclick = function(){ viewServices(p, p.types[+el.getAttribute('data-i')]); };
      });
    }

    function viewServices(p, t, backTo){
      var body = head(esc(p.label)+(t.label ? ' — '+esc(t.label) : ''), 'سرویسِ موردنظر را انتخاب کن',
        backTo || ((t.key === '__all__') ? viewPlatforms : function(){ viewTypes(p); }));
      body.innerHTML = '<div class="empty-note" style="padding:24px 0">در حال بارگذاری…</div>';
      api('/vs/smm/services?plat='+encodeURIComponent(p.key)+'&typ='+encodeURIComponent(t.key)+(mbId?'&mb='+mbId:'')).then(function(d){
        var list = (d && d.services) || [];
        if (!list.length){ body.innerHTML = '<div class="empty-note" style="padding:20px 0">سرویسی در این دسته نیست.</div>'; return; }
        body.innerHTML = list.map(function(v,i){
          return item('🛍', v.name, (v.price_1000 ? fa(v.price_1000)+' تومان به‌ازایِ هر ۱۰۰۰' : ''), i);
        }).join('');
        body.querySelectorAll('[data-i]').forEach(function(el){
          el.onclick = function(){ viewOrder(p, t, list[+el.getAttribute('data-i')], false, backTo); };
        });
      }).catch(function(){ body.innerHTML = '<div class="empty-note" style="padding:20px 0">خطا در بارگذاری.</div>'; });
    }

    function viewOrder(p, t, v, noBack, backTo){
      var body = head(esc(v.name), (v.min||v.max) ? 'تعداد: '+fa(v.min)+' تا '+fa(v.max) : '', noBack ? null : function(){ viewServices(p, t, backTo); });
      var qty = v.min || 100, timer = null, last = null;
      body.innerHTML =
        (v.desc ? '<div style="font-size:12px;color:var(--sub);line-height:1.9;margin-bottom:12px;background:var(--card-2);border-radius:12px;padding:10px">'+esc(v.desc)+'</div>' : '')+
        '<div style="font-size:11.5px;color:var(--sub);margin-bottom:6px">🔗 لینک</div>'+
        '<input id="sm-link" dir="ltr" placeholder="https://t.me/..." style="'+INP+'"/>'+
        '<div style="font-size:11.5px;color:var(--sub);margin-bottom:6px">🔢 تعداد</div>'+
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'+
          '<button id="sm-plus" style="'+BTN+';width:44px;height:44px;font-size:20px">+</button>'+
          '<input id="sm-qty" type="number" inputmode="numeric" value="'+qty+'" style="flex:1;height:44px;border-radius:12px;background:var(--card-2);border:1px solid var(--line);color:var(--ink);text-align:center;font-size:16px;font-weight:800;font-family:inherit"/>'+
          '<button id="sm-minus" style="'+BTN+';width:44px;height:44px;font-size:20px">−</button></div>'+
        '<div id="sm-q" style="background:var(--card-2);border-radius:14px;padding:13px;font-size:13px;line-height:2;margin-bottom:12px">…</div>'+
        '<button id="sm-go" class="buy-btn jelly" style="margin:0" disabled>ثبتِ سفارش</button>';
      var linkEl = body.querySelector('#sm-link'), qtyEl = body.querySelector('#sm-qty'),
          qEl = body.querySelector('#sm-q'), goEl = body.querySelector('#sm-go');
      var step = Math.max(1, Math.round((v.min || 100) / 2));
      function quote(){
        qty = parseInt(qtyEl.value, 10) || 0;
        apiJson('/vs/smm/quote', {service_id: v.id, qty: qty, link: linkEl.value}).then(function(r){
          last = r;
          var h = '';
          if (r.final){
            h += 'مبلغ: <b>'+fa(r.final)+' تومان</b>';
            if (r.discount > 0) h += ' <span style="color:var(--green)">(تخفیف −'+fa(r.discount)+')</span>';
            h += '<br/>موجودیِ تو: '+fa(r.balance)+' تومان';
            if (r.enough === false) h += '<br/><span style="color:var(--red)">موجودی کافی نیست؛ اول کیف پولت را شارژ کن.</span>';
          }
          if (r.message) h += (h?'<br/>':'')+'<span style="color:var(--red)">'+esc(r.message)+'</span>';
          qEl.innerHTML = h || '…';
          goEl.disabled = !(r.valid && r.final && linkEl.value.trim() && r.enough !== false);
        }).catch(function(){ qEl.textContent = 'خطا در محاسبه.'; goEl.disabled = true; });
      }
      function schedule(){ goEl.disabled = true; clearTimeout(timer); timer = setTimeout(quote, 350); }
      function sync(n){ var m = v.max || 1e9, lo = v.min || 1; qtyEl.value = Math.max(lo, Math.min(m, parseInt(n,10) || lo)); schedule(); }
      body.querySelector('#sm-plus').onclick = function(){ sync((parseInt(qtyEl.value,10)||0) + step); };
      body.querySelector('#sm-minus').onclick = function(){ sync((parseInt(qtyEl.value,10)||0) - step); };
      qtyEl.oninput = schedule; linkEl.oninput = schedule;
      goEl.onclick = function(){
        goEl.disabled = true; goEl.textContent = 'در حال ثبت…';
        apiJson('/vs/smm/order', {service_id: v.id, qty: parseInt(qtyEl.value,10)||0, link: linkEl.value.trim()}).then(function(r){
          box.innerHTML =
            '<div class="empty" style="padding:34px 10px"><div class="icon">'+ICON.shield+'</div>'+
            '<div style="font-weight:800;color:var(--ink);font-size:15px">سفارش ثبت شد ✅</div>'+
            '<div style="font-size:12.5px;margin-top:10px;line-height:2.1">'+esc(v.name)+'<br/>مبلغ: '+r.price_fa+' تومان<br/>موجودیِ جدید: '+r.balance_fa+' تومان<br/>کد پیگیری: <code>'+esc(r.tracking||'')+'</code></div>'+
            '<div style="font-size:11.5px;color:var(--sub);margin-top:10px">وضعیتِ سفارش در چتِ ربات به تو اطلاع داده می‌شود.</div></div>'+
            '<button class="buy-btn jelly" id="sm-done" style="margin-top:6px">باشه</button>';
          box.querySelector('#sm-done').onclick = function(){ close(); render(); };
        }).catch(function(err){
          goEl.disabled = false; goEl.textContent = 'ثبتِ سفارش';
          var m = (err && err.body && err.body.message) || 'ثبتِ سفارش ناموفق بود.';
          try{ tg && tg.showAlert(m); }catch(e){ alert(m); }
          quote();
        });
      };
      quote();
    }
    viewPlatforms();
  }

  /* ═══ === PATCH_MINIAPP_ADMIN_RENAME_V1 === پنلِ مدیریتِ داخلِ مینی‌اپ ═══ */
  function renderAdminZone(){
    var box = document.getElementById('admin-zone');
    if (!box) return;
    api('/admin/catalog').then(function(d){
      if (!d || !d.ok) return;                 /* کاربرِ عادی: چیزی نشان نده */
      var html = '<div class="section-title"><span>⚙️ مدیریت</span></div>' +
        '<div class="card" style="padding:14px">' +
        '<div style="font-size:12.5px;color:var(--sub);line-height:1.9;margin-bottom:10px">' +
        'نامِ محصولاتی که مشتری در همین مینی‌اپ می‌بیند را از این‌جا عوض کن. ' +
        'همین نام در خودِ ربات هم عوض می‌شود.</div>';
      (d.groups||[]).forEach(function(g){
        if (!g.items || !g.items.length) return;
        html += '<div style="font-size:12px;font-weight:800;margin:12px 0 7px">'+esc(g.label)+'</div>';
        g.items.forEach(function(it){
          html += '<div class="adm-line" data-kind="'+esc(it.kind)+'" data-id="'+it.id+'" '+
            'data-name="'+esc(it.name)+'" style="display:flex;align-items:center;gap:9px;'+
            'padding:9px 0;border-bottom:1px solid var(--line);cursor:pointer">'+
            '<span style="font-size:17px">'+esc(it.icon||'•')+'</span>'+
            '<span style="flex:1;min-width:0;font-size:12.5px;font-weight:700;'+
            'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(it.name)+
            (it.hint?'<span style="color:var(--sub2);font-weight:400"> — '+esc(it.hint)+'</span>':'')+
            '</span><span style="font-size:12px;color:var(--accent);font-weight:800">✏️</span></div>';
        });
      });
      html += '<div id="mp-admin-zone"></div>';
      html += '</div>';
      box.innerHTML = html;
      var mpz = document.getElementById('mp-admin-zone');
      if (mpz) renderMpAdmin(mpz);
      box.querySelectorAll('.adm-line[data-kind]').forEach(function(el){
        el.addEventListener('click', function(){
          openRename(el.getAttribute('data-kind'), el.getAttribute('data-id'),
                     el.getAttribute('data-name'));
        });
      });
    }).catch(function(){ /* ادمین نیست یا خطا — بی‌صدا رد شو */ });
  }

  function openRename(kind, id, current){
    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:999;'+
      'display:flex;align-items:flex-end';
    ov.innerHTML = '<div style="background:var(--card);width:100%;border-radius:20px 20px 0 0;'+
      'padding:18px;padding-bottom:calc(18px + env(safe-area-inset-bottom))">'+
      '<div style="font-size:14.5px;font-weight:800;margin-bottom:12px">✏️ تغییرِ نام</div>'+
      '<input id="adm-name" value="'+esc(current)+'" maxlength="64" '+
      'style="width:100%;height:46px;border-radius:12px;background:var(--card-2);'+
      'border:1px solid var(--line);color:var(--ink);padding:0 12px;font-size:13.5px;'+
      'font-family:inherit;margin-bottom:12px"/>'+
      '<div style="display:flex;gap:8px">'+
      '<button id="adm-cancel" style="flex:1;padding:13px;border-radius:12px;'+
      'background:var(--card-2);border:1px solid var(--line);color:var(--ink);'+
      'font-family:inherit;font-size:13px;font-weight:800">انصراف</button>'+
      '<button id="adm-save" style="flex:2;padding:13px;border-radius:12px;'+
      'background:var(--accent);border:none;color:#fff;font-family:inherit;'+
      'font-size:13px;font-weight:800">💾 ذخیره</button></div></div>';
    document.body.appendChild(ov);
    var input = ov.querySelector('#adm-name');
    input.focus();
    function close(){ try{ document.body.removeChild(ov); }catch(e){} }
    ov.addEventListener('click', function(e){ if (e.target === ov) close(); });
    ov.querySelector('#adm-cancel').addEventListener('click', close);
    ov.querySelector('#adm-save').addEventListener('click', function(){
      var name = (input.value||'').trim();
      if (!name) { input.focus(); return; }
      var btn = ov.querySelector('#adm-save');
      btn.disabled = true; btn.textContent = 'در حال ذخیره…';
      apiJson('/admin/rename', {kind: kind, id: parseInt(id,10), name: name})
        .then(function(){ close(); renderAdminZone(); })
        .catch(function(){ btn.disabled = false; btn.textContent = '💾 ذخیره';
          try{ tg && tg.showAlert('ذخیره نشد.'); }catch(e){} });
    });
  }

  function renderProfile() {
    api('/profile').then(function(d){
      var tgUser = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
      var photo = tgUser && tgUser.photo_url;
      var avatarHtml = photo
        ? '<img src="'+esc(photo)+'" style="width:100%;height:100%;object-fit:cover;border-radius:50%"/>'
        : (d.name||'؟').charAt(0);
      app.innerHTML = topbarHtml() +
        '<div class="wrap">' +
          '<div class="card profile-hero">' +
            '<div class="profile-avatar">' + avatarHtml + '</div>' +
            '<div class="profile-name">'+esc(d.name)+'</div>' +
            '<div class="profile-sub">@'+esc((d.username||'').replace('@',''))+'</div>' +
            '<div class="profile-id"><span class="id-chip" id="copy-id">'+ICON.copy+'<span>'+d.telegram_id+'</span></span></div>' +
          '</div>' +
          '<div class="section-title"><span>تنظیمات</span></div>' +
          '<div class="settings-row">' +
            '<div class="segmented" id="theme-seg">' +
              segOpt('dark','تیره') + segOpt('light','روشن') + segOpt('auto','خودکار') +
            '</div>' +
            '<div><div class="ttl">حالت نمایش</div><div class="sub">روشن، تیره یا خودکار</div></div>' +
          '</div>' +
          '<div class="section-title"><span>خلاصهٔ حساب کاربری</span></div>' +
          '<div class="stat-grid" id="profile-stats">' +
            statBox('…', 'مجموع خرید') + statBox('…', 'کل خرید (ت)') + (T.vs ? '' : statBox('…', T.item+' فعال')) +
          '</div>' +
          '<div class="section-title"><span>دسترسی سریع</span></div>' +
          '<div class="grid2">' +
            tile('history', 'تاریخچه تراکنش‌ها', 'گزارش پرداخت‌ها', 'ac-history') +
            tile('gift', 'دعوت دوستان', 'پاداش به ازای هر دعوت', 'ac-invite') +
            tile('help', 'پشتیبانی', 'ارتباط با تیم ما', 'ac-support') +
            tile('wallet', 'کیف پول', 'موجودی و شارژ', 'ac-wallet') +
          '</div>' +
          /* === PATCH_MINIAPP_ADMIN_RENAME_V1 === فقط برایِ مالک/ادمین */
          '<div id="admin-zone"></div>' +
        '</div>';
      bindTopbar();
      renderAdminZone();
      var homeData = null;
      api('/home').then(function(h){
        homeData = h;
        var box = document.getElementById('profile-stats');
        if (box) box.innerHTML = statBox(h.total_orders, 'مجموع خرید') + statBox(h.total_spent, 'کل خرید (ت)') + (T.vs ? '' : statBox(h.active_services, T.item+' فعال'));
        var supEl = document.getElementById('ac-support');
        if (supEl && !h.support_enabled) supEl.style.display = 'none';
        var invEl = document.getElementById('ac-invite');
        if (invEl) invEl.onclick = function(){ showReferralModal(h.referral); };
        var supBtn = document.getElementById('ac-support');
        if (supBtn) supBtn.onclick = function(){ openBot(h.support_link || BOT_LINK); };
      }).catch(function(){});
      document.querySelectorAll('#theme-seg .seg-opt').forEach(function(el){
        el.onclick = function(){
          setThemeMode(el.getAttribute('data-m'));
          document.querySelectorAll('#theme-seg .seg-opt').forEach(function(o){ o.classList.toggle('active', o===el); });
          var btn = document.getElementById('theme-btn');
          if (btn) btn.innerHTML = document.body.classList.contains('light') ? ICON.sun : ICON.moon;
        };
      });
      document.getElementById('ac-history').onclick = function(){ setTab('wallet'); };
      document.getElementById('ac-wallet').onclick = function(){ setTab('wallet'); };
      var idChip = document.getElementById('copy-id');
      if (idChip) idChip.onclick = function(){
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(String(d.telegram_id));
      };
    }).catch(showError);
  }
  function segOpt(key,label){
    return '<div class="seg-opt jelly'+(themeMode===key?' active':'')+'" data-m="'+key+'">'+label+'</div>';
  }

  function emptyBlock(title, sub) {
    return '<div class="wrap"><div class="empty"><div class="icon">'+ICON.empty+'</div><div>'+title+'</div>' +
      (sub ? '<div style="font-size:12px;margin-top:6px">'+sub+'</div>' : '') + '</div></div>';
  }
  function showError(err){
    /* === PATCH_MINIAPP_SELF_HOST_TG_JS_V1 === علتِ فنی (کوتاه) هم نشان داده می‌شود تا عیب‌یابی ممکن شود */
    var why = '';
    try {
      if (!INIT_DATA && !KB_TOKEN) why = 'اسکریپتِ تلگرام لود نشد (اینترنت/فیلتر) — صفحه را ببند و دوباره از ربات باز کن';
      else if (err && err.message) why = String(err.message).slice(0, 80);
    } catch(e) {}
    app.innerHTML = topbarHtml() + emptyBlock('مشکلی پیش اومد', 'دوباره امتحان کن' +
      (why ? '<div style="margin-top:8px;font-size:11px;opacity:.7;direction:ltr;word-break:break-all">' + esc(why) + '</div>' : ''));
    bindTopbar();
  }
  function esc(s){ var d=document.createElement('div'); d.textContent = s==null?'':s; return d.innerHTML; }

  /* === PATCH_MINIAPP_WHEEL_FAB_V1 === */
  (function(){
    var fab = document.createElement('div');
    fab.id = 'wheel-fab'; fab.textContent = '🎡';
    fab.onclick = function(){ openWheel(); };
    document.body.appendChild(fab);
    api('/wheel').then(function(d){ if (d && d.ok && d.enabled) fab.style.display = 'flex'; }).catch(function(){});
  })();
  setTab('home');
})();
</script>
</body>
</html>"""


# === PATCH_MINIAPP_WHEEL_V1 === روت‌هایِ گردونه (آخرِ فایل تا miniapp_router و توابعش آماده باشند)
from tenant.shared import miniapp_wheel as _miniapp_wheel  # noqa: E402
_miniapp_wheel._install_routes()
