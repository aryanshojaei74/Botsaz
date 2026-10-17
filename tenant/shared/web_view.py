"""
web_view.py — صفحهٔ عمومیِ «مشاهدهٔ زندهٔ کانفیگ» برای کانفیگ‌های خودکار
(ساخته‌شده روی پنل: سنایی/علیرضا/مرزبان/پاسارگارد).

هیچ ورود/رمزی لازم نیست: هر PanelAccount یک web_token یکتا و تصادفی دارد؛
لینک صفحه فقط با داشتن این توکن قابل‌دسترسی است (مثل لینک‌های اشتراکِ خودِ
پنل‌ها). صفحه اطلاعات را زنده (با فراخوانی دوره‌ای API) به‌روز می‌کند.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import secrets

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser, PanelAccount, VpnPanel

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

web_router = APIRouter()

_GB = 1024 ** 3

# ── برنامه‌های پیشنهادی، دسته‌بندی‌شده بر اساسِ سیستم‌عامل ──
# === PATCH_WEB_VIEW_APPS_BY_PLATFORM_V1 ===
# هر پلتفرم می‌تونه چند برنامه داشته باشه؛ اولی که "recommended": True داره
# به‌عنوانِ پیشنهادِ ویژه (با بج) بالای گروه نشون داده می‌شه. لینک‌هایِ
# دسکتاپ/TV از GitHub Releases هستن (صفحه‌ی «آخرین نسخه»، نه فایلِ نسخه‌ی
# ثابت) که با آپدیتِ برنامه خودکار درست می‌مونن.
_APPS = [
    {"platform": "android", "name": "Hiddify", "recommended": True,
     "note": "پایدار، رایگان و متن‌باز",
     "url": "https://play.google.com/store/apps/details?id=app.hiddify.com"},
    {"platform": "android", "name": "v2RayTun",
     "url": "https://play.google.com/store/apps/details?id=com.v2raytun.android"},
    {"platform": "android", "name": "v2rayNG",
     "url": "https://github.com/2dust/v2rayNG/releases/latest"},

    {"platform": "ios", "name": "Streisand", "recommended": True,
     "note": "پایدارترین نرم‌افزار برای استفاده از سرویس در iOS",
     "url": "https://apps.apple.com/app/streisand/id6450534064"},
    {"platform": "ios", "name": "Hiddify",
     "url": "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532"},

    {"platform": "windows", "name": "Hiddify", "recommended": True,
     "note": "نصب و اتصال با یک کلیک",
     "url": "https://github.com/hiddify/hiddify-app/releases/latest"},
    {"platform": "windows", "name": "v2rayN",
     "url": "https://github.com/2dust/v2rayN/releases/latest"},

    {"platform": "macos", "name": "Hiddify", "recommended": True,
     "note": "نصب و اتصال با یک کلیک",
     "url": "https://github.com/hiddify/hiddify-app/releases/latest"},

    {"platform": "linux", "name": "Hiddify", "recommended": True,
     "note": "نصب و اتصال با یک کلیک",
     "url": "https://github.com/hiddify/hiddify-app/releases/latest"},

    {"platform": "appletv", "name": "Hiddify (نصبِ دستی)",
     "note": "اپ‌استورِ Apple TV هنوز اپِ رسمی ندارد؛ راهنمای نصب از سایتِ Hiddify",
     "url": "https://github.com/hiddify/hiddify-app"},

    {"platform": "androidtv", "name": "Hiddify",
     "note": "فایلِ APK را دانلود و رویِ Android TV نصب کن",
     "url": "https://github.com/hiddify/hiddify-app/releases/latest"},
]

_PLATFORM_LABELS = {
    "ios": ("🍎", "iOS"), "android": ("🤖", "اندروید"),
    "windows": ("🪟", "ویندوز"), "macos": ("🍎", "macOS"),
    "linux": ("🐧", "لینوکس"), "appletv": ("📺", "Apple TV"),
    "androidtv": ("📺", "Android TV"),
}
_PLATFORM_ORDER = ["ios", "android", "windows", "macos", "linux", "appletv", "androidtv"]


async def ensure_web_token(s, account: PanelAccount) -> str:
    """اگر اکانت هنوز توکنِ صفحهٔ وب نداشت، یکی می‌سازد و برمی‌گرداند."""
    if account.web_token:
        return account.web_token
    for _ in range(5):
        tok = secrets.token_urlsafe(24)
        clash = await s.scalar(select(PanelAccount.id).where(PanelAccount.web_token == tok))
        if not clash:
            account.web_token = tok
            await s.flush()
            return tok
    # عملاً غیرممکن است به اینجا برسد
    account.web_token = secrets.token_urlsafe(32)
    await s.flush()
    return account.web_token


async def _panel_for_account(s, bot_id: int, account) -> "VpnPanel | None":
    pk = getattr(account, "panel_key", None)
    if pk:
        p = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == pk))
        if p:
            return p
    return await s.scalar(select(VpnPanel).where(
        VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))


async def _gather_data(token: str) -> dict | None:
    async with Session() as s:
        acc = await s.scalar(select(PanelAccount).where(PanelAccount.web_token == token))
        if not acc:
            return None
        row = await s.get(BotRow, acc.bot_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == acc.bot_id, BotUser.telegram_id == acc.user_telegram_id))
        panel = await _panel_for_account(s, acc.bot_id, acc)
        title = acc.plan_title or "کانفیگ VPN"
        vol = int(acc.volume_gb or 0)
        days = int(acc.days or 0)
        sub_link = acc.sub_link or ""
        expires_at = acc.expires_at
        email = acc.email
        bot_title = (row.username if row and row.username else "") if row else ""

    usage = None
    links: list[str] = []
    if panel:
        from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
        client = make_panel_client(panel)
        try:
            usage = await client.get_usage(email)
        except Exception:
            usage = None
        try:
            links = await client.get_client_links(email) or []
        except Exception:
            links = []
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    up = int((usage or {}).get("up", 0) or 0)
    down = int((usage or {}).get("down", 0) or 0)
    used_bytes = up + down
    cap_bytes = int((usage or {}).get("total", 0) or 0)
    used_gb = round(used_bytes / _GB, 2)

    if cap_bytes:
        cap_gb = round(cap_bytes / _GB, 2)
        remaining_gb = max(round(cap_gb - used_gb, 2), 0)
        percent = min(int(used_bytes / cap_bytes * 100), 100)
        unlimited = False
    elif vol:
        cap_gb = vol
        remaining_gb = max(round(vol - used_gb, 2), 0)
        percent = min(int((used_gb / vol) * 100), 100) if vol else 0
        unlimited = False
    else:
        cap_gb = None
        remaining_gb = None
        percent = 0
        unlimited = True

    raw_expiry_ms = int((usage or {}).get("expiryTime", 0) or 0)
    if raw_expiry_ms:
        expiry_dt = _dt.datetime.utcfromtimestamp(raw_expiry_ms / 1000)
    else:
        expiry_dt = expires_at
    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M") if expiry_dt else "بدون انقضا"
    no_expiry = days == 0 and not raw_expiry_ms

    # === PATCH_WEB_VIEW_LAST_ONLINE_V1 ===
    # فقط پنل‌هایی که واقعاً این داده رو دارن (مثلاً مرزبان) پرش می‌کنن؛
    # بقیه None می‌مونه و تویِ صفحه نشون داده نمی‌شه — عددِ ساختگی نمی‌سازیم.
    last_online_ms = int((usage or {}).get("last_online_ms", 0) or 0)
    last_online_str = (_dt.datetime.utcfromtimestamp(last_online_ms / 1000)
                       .strftime("%Y-%m-%d %H:%M") if last_online_ms else None)
    client_app = (usage or {}).get("client_app") or None

    buyer_name = (u.full_name if (u and u.full_name) else None) or "کاربر مهمان"
    buyer_username = f"@{u.username}" if (u and u.username) else None

    return {
        "ok": True,
        "buyer_name": buyer_name,
        "buyer_username": buyer_username,
        "title": title,
        "bot_title": bot_title,
        "volume_gb": vol,
        "days": days,
        "used_gb": used_gb,
        "cap_gb": cap_gb,
        "remaining_gb": remaining_gb,
        "percent": percent,
        "expiry": ("بدون انقضا" if no_expiry else expiry_str),
        "last_online": last_online_str,
        "client_app": client_app,
        "sub_link": sub_link,
        "servers": links,
        "unlimited": unlimited,
        "updated_at": _dt.datetime.now().strftime("%H:%M:%S"),
    }


def _esc(v) -> str:
    return _html.escape(str(v), quote=True)


# === PATCH_WEB_VIEW_APPS_BY_PLATFORM_V1 ===
def _apps_html() -> str:
    """گروه‌هایِ تاشوندهٔ برنامه به‌ازایِ هر سیستم‌عامل — پلتفرمِ خودِ کاربر
    (که با data-platform-detect در جاوااسکریپت تشخیص داده می‌شود) بازِ باز
    شروع می‌شود، بقیه بسته‌اند."""
    groups = []
    for pf in _PLATFORM_ORDER:
        apps = [a for a in _APPS if a["platform"] == pf]
        if not apps:
            continue
        icon, label = _PLATFORM_LABELS[pf]
        cards = []
        for a in apps:
            rec_badge = '<span class="app-badge">پیشنهادی</span>' if a.get("recommended") else ""
            note = f'<div class="app-note">{_esc(a["note"])}</div>' if a.get("note") else ""
            cards.append(
                f'<div class="app-card{" recommended" if a.get("recommended") else ""}">'
                f'<div class="app-card-top">'
                f'<span class="app-icon">{icon}</span>'
                f'<div class="app-card-text"><span class="app-name">{_esc(a["name"])}</span>{rec_badge}</div>'
                f'</div>{note}'
                f'<a class="app-dl-btn" href="{_esc(a["url"])}" target="_blank" rel="noopener">دانلود ⬇</a>'
                '</div>'
            )
        groups.append(
            f'<div class="plat-group" data-platform="{pf}">'
            f'<button class="plat-head" data-toggle-platform="{pf}">'
            f'<span class="plat-head-left"><span class="plat-icon">{icon}</span>{label}</span>'
            f'<span class="plat-you" data-you-badge="{pf}" hidden>سیستم شما</span>'
            f'<span class="plat-chevron">⌄</span></button>'
            f'<div class="plat-body" data-body="{pf}"><div class="app-grid">'
            + "".join(cards) + '</div></div></div>'
        )
    return '<div class="plat-list">' + "".join(groups) + '</div>'


def _servers_html(servers: list[str]) -> str:
    if not servers:
        return '<div class="empty-note">فعلاً سروری برای نمایش تکی وجود ندارد؛ از لینک اشتراک بالا استفاده کن.</div>'
    rows = []
    for i, link in enumerate(servers, 1):
        safe = _esc(link)
        proto = link.split("://", 1)[0].upper() if "://" in link else "LINK"
        rows.append(
            '<div class="srv-row">'
            f'<div class="srv-proto">{_esc(proto)}</div>'
            f'<div class="srv-link" id="srv-{i}">{safe}</div>'
            f'<button class="icon-btn" data-qr-target="srv-{i}" title="نمایش QR" aria-label="نمایش QR">▦</button>'
            f'<button class="icon-btn" data-copy-target="srv-{i}" title="کپی" aria-label="کپی">⧉</button>'
            '</div>'
        )
    all_ids = ",".join(f"srv-{i}" for i in range(1, len(servers) + 1))
    return (
        f'<div class="srv-toolbar"><button class="copy-btn full-copy" '
        f'data-copy-all="{all_ids}" data-copy-sep="&#10;">📋 کپیِ همهٔ کانفیگ‌ها</button></div>'
        '<div class="srv-list">' + "\n".join(rows) + '</div>'
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover"/>
<meta name="theme-color" content="#0b0f1d"/>
<meta name="color-scheme" content="dark"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<title>اطلاعات کانفیگ — {title}</title>
<style>
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    margin: 0; min-height: 100vh;
    padding: max(16px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right)) 40px max(14px, env(safe-area-inset-left));
    font-family: 'Vazirmatn', 'Segoe UI', Tahoma, sans-serif;
    background: radial-gradient(circle at 20% -10%, #1b2440 0%, #0b0f1d 55%, #060810 100%);
    background-attachment: fixed;
    color: #eef1ff;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 480px; margin: 0 auto; }}
  .live-dot {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: #35e08a; margin-left: 6px;
    box-shadow: 0 0 0 0 rgba(53,224,138,.6);
    animation: pulse 1.6s infinite;
  }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(53,224,138,.55); }}
    70% {{ box-shadow: 0 0 0 8px rgba(53,224,138,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(53,224,138,0); }}
  }}
  .header {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
  }}
  .avatar {{
    width: 50px; height: 50px; border-radius: 15px; flex-shrink: 0;
    background: linear-gradient(135deg,#6c5ce7,#00cec9);
    display: flex; align-items: center; justify-content: center;
    font-size: 21px; font-weight: 700; color: #fff;
    box-shadow: 0 4px 14px rgba(108,92,231,.35);
  }}
  .buyer-name {{ font-size: 16.5px; font-weight: 700; }}
  .buyer-sub {{ font-size: 12.5px; color: #9aa3c7; margin-top: 2px; }}
  .card {{
    background: rgba(255,255,255,.045);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 18px; padding: 16px; margin-bottom: 14px;
    backdrop-filter: blur(6px);
  }}
  .card-title {{
    font-size: 13px; color: #9aa3c7; margin-bottom: 10px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .plan-title {{ font-size: 16px; font-weight: 700; margin-bottom: 14px; }}
  .stats-row {{ display: flex; justify-content: space-between; margin-bottom: 10px; }}
  .stat {{ text-align: center; flex: 1; }}
  .stat b {{ display: block; font-size: 17px; }}
  .stat span {{ font-size: 11.5px; color: #9aa3c7; }}
  .bar-outer {{
    width: 100%; height: 12px; border-radius: 8px; background: rgba(255,255,255,.08);
    overflow: hidden; margin: 6px 0 4px;
  }}
  .bar-inner {{
    height: 100%; border-radius: 8px;
    background: linear-gradient(90deg,#00cec9,#6c5ce7);
    transition: width .5s ease;
  }}
  .bar-label {{ text-align: center; font-size: 12px; color: #9aa3c7; }}
  .expiry-line {{ margin-top: 10px; font-size: 13px; color: #cbd0f0; text-align: center; }}
  .updated-note {{ text-align: center; font-size: 11px; color: #6a7398; margin-top: 8px; }}
  .sub-box {{
    background: rgba(0,0,0,.28); border-radius: 12px; padding: 10px 12px;
    font-family: monospace; font-size: 12px; word-break: break-all;
    direction: ltr; text-align: left; color: #cfe7ff; margin-bottom: 10px;
  }}
  .copy-btn {{
    background: linear-gradient(135deg,#6c5ce7,#00cec9); border: none; color: #fff;
    padding: 9px 16px; min-height: 38px; border-radius: 10px; font-size: 12.5px; font-weight: 700;
    cursor: pointer; white-space: nowrap; touch-action: manipulation;
  }}
  .copy-btn:active {{ transform: scale(.96); }}
  .full-copy {{ width: 100%; padding: 12px; min-height: 46px; }}

  /* === PATCH_WEB_VIEW_GAUGE_V1 — گیج دایره‌ای مصرف === */
  .gauge-wrap {{ display: flex; justify-content: center; margin: 4px 0 16px; }}
  .gauge-svg {{ width: 148px; height: 148px; transform: rotate(-90deg); }}
  .gauge-track {{ fill: none; stroke: rgba(255,255,255,.08); stroke-width: 12; }}
  .gauge-fill {{
    fill: none; stroke: url(#gaugeGrad); stroke-width: 12; stroke-linecap: round;
    transition: stroke-dashoffset .6s ease;
  }}
  .gauge-center {{
    position: relative; width: 148px; height: 148px; margin: 0 auto;
  }}
  .gauge-text {{
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }}
  .gauge-pct {{ font-size: 26px; font-weight: 800; line-height: 1; }}
  .gauge-pct-label {{ font-size: 11px; color: #9aa3c7; margin-top: 4px; }}
  .info-list {{ display: flex; flex-direction: column; gap: 8px; margin-top: 4px; }}
  .info-row {{
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12.5px; color: #cbd0f0; padding: 2px 0;
  }}
  .info-row span:first-child {{ color: #9aa3c7; }}
  .info-row b {{ font-weight: 700; color: #eef1ff; }}

  /* === سرورها با دکمه‌هایِ آیکونی و کپیِ همه === */
  .srv-toolbar {{ margin-bottom: 10px; }}
  .srv-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .srv-row {{
    display: flex; align-items: center; gap: 8px;
    background: rgba(0,0,0,.22); border-radius: 12px; padding: 8px 10px;
  }}
  .srv-proto {{
    flex-shrink: 0; font-size: 10px; font-weight: 700; letter-spacing: .3px;
    color: #35e08a; background: rgba(53,224,138,.12); border-radius: 7px;
    padding: 4px 7px;
  }}
  .srv-link {{
    flex: 1; font-family: monospace; font-size: 11px; color: #cfe7ff;
    direction: ltr; text-align: left; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .icon-btn {{
    flex-shrink: 0; width: 32px; height: 32px; border-radius: 9px; border: none;
    background: rgba(255,255,255,.08); color: #eef1ff; font-size: 14px;
    cursor: pointer; touch-action: manipulation;
  }}
  .icon-btn:active {{ transform: scale(.92); background: rgba(255,255,255,.16); }}
  .empty-note {{ font-size: 12.5px; color: #9aa3c7; text-align: center; padding: 10px 0; }}

  /* === PATCH_WEB_VIEW_APPS_BY_PLATFORM_V1 — گروه‌هایِ تاشوندهٔ برنامه === */
  .plat-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .plat-group {{
    background: rgba(255,255,255,.03); border: 1px solid rgba(255,255,255,.07);
    border-radius: 14px; overflow: hidden;
  }}
  .plat-head {{
    width: 100%; display: flex; align-items: center; justify-content: space-between;
    background: none; border: none; color: #eef1ff; padding: 12px 14px;
    font-family: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer;
    touch-action: manipulation;
  }}
  .plat-head-left {{ display: flex; align-items: center; gap: 8px; }}
  .plat-icon {{ font-size: 16px; }}
  .plat-you {{
    font-size: 10.5px; color: #35e08a; background: rgba(53,224,138,.12);
    border-radius: 7px; padding: 3px 8px; margin-inline-start: 8px;
  }}
  .plat-chevron {{ color: #9aa3c7; transition: transform .25s ease; margin-inline-start: 8px; }}
  .plat-group.open .plat-chevron {{ transform: rotate(180deg); }}
  .plat-body {{ display: none; padding: 0 14px 14px; }}
  .plat-group.open .plat-body {{ display: block; }}
  .app-grid {{ display: flex; flex-direction: column; gap: 8px; }}
  .app-card {{
    background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.08);
    border-radius: 12px; padding: 10px 12px;
  }}
  .app-card.recommended {{ border-color: rgba(53,224,138,.4); background: rgba(53,224,138,.06); }}
  .app-card-top {{ display: flex; align-items: center; gap: 8px; }}
  .app-icon {{ font-size: 16px; }}
  .app-card-text {{ display: flex; align-items: center; gap: 7px; flex: 1; }}
  .app-name {{ font-size: 12.5px; font-weight: 700; }}
  .app-badge {{
    font-size: 10px; color: #0b0f1d; background: #35e08a; border-radius: 6px;
    padding: 2px 6px; font-weight: 700;
  }}
  .app-note {{ font-size: 11px; color: #9aa3c7; margin: 5px 0 8px; }}
  .app-dl-btn {{
    display: block; text-align: center; text-decoration: none; color: #eef1ff;
    background: rgba(255,255,255,.08); border-radius: 9px; padding: 8px; font-size: 12px;
    font-weight: 700; touch-action: manipulation;
  }}
  .app-card.recommended .app-dl-btn {{ background: linear-gradient(135deg,#6c5ce7,#00cec9); }}

  /* === مودالِ QR === */
  .qr-modal {{
    position: fixed; inset: 0; background: rgba(6,8,16,.82); backdrop-filter: blur(3px);
    display: none; align-items: center; justify-content: center; z-index: 50; padding: 24px;
  }}
  .qr-modal.show {{ display: flex; }}
  .qr-box {{
    background: #10152a; border: 1px solid rgba(255,255,255,.1); border-radius: 18px;
    padding: 18px; max-width: 300px; width: 100%; text-align: center;
  }}
  .qr-box img {{ width: 100%; max-width: 230px; border-radius: 10px; background: #fff; padding: 8px; }}
  .qr-close {{
    margin-top: 14px; width: 100%; padding: 10px; border-radius: 10px; border: none;
    background: rgba(255,255,255,.08); color: #eef1ff; font-family: inherit; font-size: 13px;
    cursor: pointer;
  }}

  .toast {{
    position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%) translateY(20px);
    background: #16a765; color: #fff; padding: 8px 18px; border-radius: 20px;
    font-size: 13px; opacity: 0; transition: all .25s ease; pointer-events: none;
  }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
  .footer-note {{ text-align: center; font-size: 11px; color: #5c6690; margin-top: 18px; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="avatar">{avatar_letter}</div>
    <div>
      <div class="buyer-name">{buyer_name}</div>
      <div class="buyer-sub">{buyer_sub}</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      <span>📦 وضعیت زندهٔ کانفیگ</span>
      <span><span class="live-dot"></span>زنده</span>
    </div>
    <div class="plan-title" id="plan-title">{plan_title}</div>

    <div class="gauge-wrap">
      <div class="gauge-center">
        <svg class="gauge-svg" viewBox="0 0 120 120">
          <defs>
            <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stop-color="#00cec9"/>
              <stop offset="100%" stop-color="#6c5ce7"/>
            </linearGradient>
          </defs>
          <circle class="gauge-track" cx="60" cy="60" r="52"/>
          <circle class="gauge-fill" id="gauge-fill" cx="60" cy="60" r="52"
                  stroke-dasharray="{gauge_circumference}"
                  stroke-dashoffset="{gauge_offset}"/>
        </svg>
        <div class="gauge-text">
          <div class="gauge-pct" id="gauge-pct">{percent}٪</div>
          <div class="gauge-pct-label">مصرف‌شده</div>
        </div>
      </div>
    </div>

    <div class="stats-row">
      <div class="stat"><b id="stat-used">{used_gb}</b><span>مصرف‌شده (GB)</span></div>
      <div class="stat"><b id="stat-remaining">{remaining_disp}</b><span>باقی‌مانده (GB)</span></div>
      <div class="stat"><b id="stat-cap">{cap_disp}</b><span>کل حجم (GB)</span></div>
    </div>

    <div class="info-list">
      <div class="info-row"><span>⏳ تاریخ انقضا</span><b id="expiry-line">{expiry}</b></div>
      {last_online_row}
    </div>
    <div class="updated-note">آخرین به‌روزرسانی: <span id="updated-at">{updated_at}</span></div>
  </div>

  <div class="card">
    <div class="card-title"><span>🔗 لینک اشتراک (Subscription)</span></div>
    <div class="sub-box" id="sub-link">{sub_link_disp}</div>
    <div style="display:flex; gap:8px;">
      <button class="copy-btn full-copy" data-copy-target="sub-link" style="flex:1;">📋 کپی لینک اشتراک</button>
      <button class="icon-btn" data-qr-target="sub-link" title="نمایش QR" aria-label="نمایش QR"
              style="width:46px; height:auto;">▦</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title"><span>🖥 سرورهای داخل اشتراک</span></div>
    {servers_html}
  </div>

  <div class="card">
    <div class="card-title"><span>📱 برنامه‌هایِ اتصال به‌ازایِ هر سیستم‌عامل</span></div>
    {apps_html}
  </div>

  <div class="footer-note">این صفحه هر ۱۲ ثانیه به‌صورت خودکار به‌روزرسانی می‌شود.</div>
</div>

<div class="toast" id="toast">کپی شد ✅</div>

<div class="qr-modal" id="qr-modal">
  <div class="qr-box">
    <img id="qr-img" src="" alt="QR"/>
    <button class="qr-close" id="qr-close">بستن</button>
  </div>
</div>

<script>
  (function () {{
    var tg = window.Telegram && window.Telegram.WebApp;
    if (!tg) return;
    try {{ tg.ready(); }} catch (e) {{}}
    try {{ tg.expand(); }} catch (e) {{}}
    try {{ tg.setHeaderColor('#0b0f1d'); }} catch (e) {{}}
    try {{ tg.setBackgroundColor('#0b0f1d'); }} catch (e) {{}}
    try {{ tg.disableVerticalSwipes(); }} catch (e) {{}}
  }})();

  const TOKEN = {token_json};

  function showToast(msg) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 1400);
  }}

  function copyText(text) {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(() => showToast('کپی شد ✅'))
        .catch(() => fallbackCopy(text));
    }} else {{
      fallbackCopy(text);
    }}
  }}

  function fallbackCopy(text) {{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try {{ document.execCommand('copy'); showToast('کپی شد ✅'); }}
    catch (e) {{ showToast('کپی نشد، دستی انتخاب کن'); }}
    document.body.removeChild(ta);
  }}

  document.addEventListener('click', function (e) {{
    const btn = e.target.closest('.copy-btn');
    if (!btn) return;
    if (btn.hasAttribute('data-copy-all')) {{
      const ids = btn.getAttribute('data-copy-all').split(',');
      const sep = btn.getAttribute('data-copy-sep') || '\\n';
      const text = ids.map(id => {{
        const el = document.getElementById(id);
        return el ? el.textContent.trim() : '';
      }}).filter(Boolean).join(sep);
      if (text) copyText(text);
      return;
    }}
    const targetId = btn.getAttribute('data-copy-target');
    const el = document.getElementById(targetId);
    if (el) copyText(el.textContent.trim());
  }});

  // === PATCH_WEB_VIEW_QR_MODAL_V1 ===
  // از یک سرویسِ عمومیِ رندرِ QR استفاده می‌کنیم (بدونِ نیاز به کتابخانه‌ی
  // جاوااسکریپت)؛ فقط وقتی کاربر رویِ دکمه‌ی QR بزنه لود می‌شه، نه پیش‌فرض
  // برایِ همه‌ی لینک‌ها — که هم سریع‌تره هم لینک‌ها رو بی‌دلیل به بیرون درز نمی‌ده.
  function openQr(text) {{
    const img = document.getElementById('qr-img');
    img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=260x260&data=' + encodeURIComponent(text);
    document.getElementById('qr-modal').classList.add('show');
  }}
  document.addEventListener('click', function (e) {{
    const qbtn = e.target.closest('[data-qr-target]');
    if (qbtn) {{
      const el = document.getElementById(qbtn.getAttribute('data-qr-target'));
      if (el) openQr(el.textContent.trim());
      return;
    }}
    if (e.target.id === 'qr-close' || e.target.id === 'qr-modal') {{
      document.getElementById('qr-modal').classList.remove('show');
    }}
  }});

  // === PATCH_WEB_VIEW_APPS_BY_PLATFORM_V1 ===
  document.addEventListener('click', function (e) {{
    const head = e.target.closest('[data-toggle-platform]');
    if (!head) return;
    head.closest('.plat-group').classList.toggle('open');
  }});

  (function detectPlatformAndOpen() {{
    const ua = navigator.userAgent || '';
    let plat = null;
    if (/iPad|iPhone|iPod/.test(ua)) plat = 'ios';
    else if (/Android/.test(ua)) plat = /TV/.test(ua) ? 'androidtv' : 'android';
    else if (/Windows/.test(ua)) plat = 'windows';
    else if (/Macintosh|Mac OS X/.test(ua)) plat = 'macos';
    else if (/Linux/.test(ua)) plat = 'linux';
    if (!plat) return;
    const group = document.querySelector('.plat-group[data-platform="' + plat + '"]');
    const badge = document.querySelector('[data-you-badge="' + plat + '"]');
    if (badge) badge.hidden = false;
    if (group) group.classList.add('open');
  }})();

  function setGauge(percent) {{
    const CIRC = {gauge_circumference};
    const offset = CIRC * (1 - Math.min(Math.max(percent, 0), 100) / 100);
    document.getElementById('gauge-fill').style.strokeDashoffset = offset;
    document.getElementById('gauge-pct').textContent = percent + '٪';
  }}

  async function refreshLive() {{
    try {{
      const res = await fetch('/api/cfg/' + TOKEN, {{ cache: 'no-store' }});
      if (!res.ok) return;
      const d = await res.json();
      if (!d || !d.ok) return;
      document.getElementById('stat-used').textContent = d.used_gb;
      document.getElementById('stat-remaining').textContent =
        (d.unlimited ? '∞' : (d.remaining_gb ?? '—'));
      document.getElementById('stat-cap').textContent =
        (d.unlimited ? '∞' : (d.cap_gb ?? '—'));
      setGauge(d.percent);
      document.getElementById('expiry-line').textContent = d.expiry;
      document.getElementById('updated-at').textContent = d.updated_at;
    }} catch (e) {{ /* شبکه موقتاً در دسترس نبود؛ در چرخهٔ بعدی دوباره تلاش می‌شود */ }}
  }}

  setInterval(refreshLive, 12000);
</script>
</body>
</html>"""


_NOT_FOUND_HTML = """<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="theme-color" content="#0b0f1d"/>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;700&display=swap" rel="stylesheet"/>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<title>یافت نشد</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:radial-gradient(circle at 20% -10%, #1b2440 0%, #0b0f1d 55%, #060810 100%);
       color:#eef1ff;font-family:'Vazirmatn','Segoe UI',Tahoma,sans-serif;text-align:center;padding:20px}
  .icon{font-size:40px;margin-bottom:14px}
  .msg{font-size:15px;color:#cbd0f0}
</style></head>
<body><div>
  <div class="icon">🔍</div>
  <div class="msg">این لینک نامعتبر است یا کانفیگ حذف شده.</div>
</div>
<script>
  (function () {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (!tg) return;
    try { tg.ready(); } catch (e) {}
    try { tg.expand(); } catch (e) {}
    try { tg.setHeaderColor('#0b0f1d'); } catch (e) {}
    try { tg.setBackgroundColor('#0b0f1d'); } catch (e) {}
  })();
</script>
</body></html>"""

_GAUGE_R = 52
_GAUGE_CIRC = round(2 * 3.14159265358979 * _GAUGE_R, 3)


def _render_page(token: str, data: dict) -> str:
    remaining_disp = "∞" if data["unlimited"] else data["remaining_gb"]
    cap_disp = "∞" if data["unlimited"] else data["cap_gb"]
    sub_link = data["sub_link"] or "این سرویس ساب‌لینک ندارد."
    percent = int(data["percent"])
    gauge_offset = round(_GAUGE_CIRC * (1 - min(max(percent, 0), 100) / 100), 3)

    # === PATCH_WEB_VIEW_LAST_ONLINE_V1 ===
    last_online_row = ""
    if data.get("last_online"):
        app_suffix = f" ({_esc(data['client_app'])})" if data.get("client_app") else ""
        last_online_row = (
            f'<div class="info-row"><span>🟢 آخرین اتصال</span>'
            f'<b>{_esc(data["last_online"])}{app_suffix}</b></div>'
        )

    return _PAGE_TEMPLATE.format(
        title=_esc(data["title"]),
        avatar_letter=_esc((data["buyer_name"] or "؟")[0]),
        buyer_name=_esc(data["buyer_name"]),
        buyer_sub=_esc(data["buyer_username"] or data["bot_title"] or "کاربر ربات"),
        plan_title=_esc(data["title"]),
        used_gb=_esc(data["used_gb"]),
        remaining_disp=_esc(remaining_disp),
        cap_disp=_esc(cap_disp),
        percent=percent,
        gauge_circumference=_GAUGE_CIRC,
        gauge_offset=gauge_offset,
        expiry=_esc(data["expiry"]),
        last_online_row=last_online_row,
        updated_at=_esc(data["updated_at"]),
        sub_link_disp=_esc(sub_link),
        servers_html=_servers_html(data["servers"]),
        apps_html=_apps_html(),
        token_json=json.dumps(token),
    )


@web_router.get("/cfg/{token}", response_class=HTMLResponse)
async def cfg_web_page(token: str):
    data = await _gather_data(token)
    if not data:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)
    return HTMLResponse(content=_render_page(token, data))


@web_router.get("/api/cfg/{token}")
async def cfg_web_api(token: str):
    data = await _gather_data(token)
    if not data:
        return JSONResponse(content={"ok": False}, status_code=404)
    return JSONResponse(content=data)
