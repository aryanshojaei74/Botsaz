"""
web_panel.py — «پنل تحت وب» برایِ مالکِ رباتِ مستأجر.

=== PATCH_WEB_PANEL_V1 ===

فرقش با web_view.py: آن یک صفحهٔ *عمومی* برایِ خودِ مشتریِ نهایی است (نمایشِ
مصرفِ یک کانفیگ با توکنِ لینک). این یکی پنلِ *مدیریتیِ* خودِ مالکِ ربات است و
پشتِ ورود قرار دارد.

── امنیت ──
هیچ رمزی وجود ندارد و هیچ‌جا ذخیره نمی‌شود. جریانِ ورود:
  ۱) مالک داخلِ رباتِ خودش دستورِ /panel را می‌زند.
  ۲) یک توکنِ یک‌بارمصرفِ ۵ دقیقه‌ای ساخته می‌شود و لینکش فقط در همان چتِ
     تلگرامِ او فرستاده می‌شود — یعنی مالکیت با خودِ تلگرام اثبات می‌شود.
  ۳) با بازکردنِ لینک، توکن مصرف می‌شود (دوباره کار نمی‌کند) و یک توکنِ
     سشنِ ۱۲ ساعته در کوکیِ HttpOnly می‌نشیند.

هر درخواست، سشن را از دیتابیس می‌خواند و دوباره چک می‌کند که:
  • منقضی/باطل نشده باشد،
  • کاربر هنوز مالک یا ادمینِ همان ربات باشد،
  • ربات هنوز پلنِ حرفه‌ای داشته باشد.
این چک‌ها عمداً در هر درخواست تکرار می‌شوند (نه فقط موقعِ ورود)، تا اگر
دسترسیِ کسی گرفته شد یا پلن پایین آمد، سشنِ بازش همان لحظه بی‌اثر شود.

این نسخه عمداً «خواندنی + چند عملِ کم‌خطر» است. هیچ توکنِ ربات، کلیدِ APIِ
پنلِ VPN یا رمزی در این پنل نه نمایش داده می‌شود و نه قابلِ تغییر است.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from config import Session, WEBHOOK_BASE
from models import (AdminWebSession, Bot as BotRow, BotUser, Order, PanelAccount)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

web_panel_router = APIRouter(prefix="/panel")

_COOKIE = "botsaz_panel"
_LOGIN_TTL = _dt.timedelta(minutes=5)
_SESSION_TTL = _dt.timedelta(hours=12)
_PAGE_SIZE = 25


# ─────────────── ورود ───────────────

async def create_login_link(bot_id: int, telegram_id: int) -> str | None:
    """توکنِ یک‌بارمصرف می‌سازد و لینکِ ورود را برمی‌گرداند.

    === PATCH_PANEL_CUSTOM_DOMAIN_V1 ===
    اگر مالکِ ربات دامنهٔ اختصاصیِ تاییدشده داشته باشد، لینک زیرِ همان
    دامنه ساخته می‌شود — نه دامنهٔ مشترکِ پلتفرم. اینطوری نه نماینده و نه
    مشتریِ او آدرسِ وبهوکِ بات‌ساز را نمی‌بیند.
    """
    from tenant.gateways.custom_domain import get_bot_webhook_base
    base = (await get_bot_webhook_base(bot_id) or "").rstrip("/")
    if not base:
        return None
    tok = secrets.token_urlsafe(32)
    now = _dt.datetime.utcnow()
    async with Session() as s:
        # سشن‌های ورودِ مصرف‌نشدهٔ قبلیِ همین کاربر را باطل کن تا چند لینکِ
        # زنده هم‌زمان وجود نداشته باشد.
        old = (await s.scalars(select(AdminWebSession).where(
            AdminWebSession.bot_id == bot_id,
            AdminWebSession.telegram_id == telegram_id,
            AdminWebSession.used_at.is_(None),
            AdminWebSession.revoked == False))).all()
        for o in old:
            o.revoked = True
        s.add(AdminWebSession(
            bot_id=bot_id, telegram_id=telegram_id, login_token=tok,
            login_expires_at=now + _LOGIN_TTL))
        await s.commit()
    return f"{base}/panel/login/{tok}"


async def _plan_allows(bot_id: int) -> bool:
    from plans import Feature, has_feature
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
    return bool(r) and has_feature(r.plan, Feature.WEB_PANEL)


async def _resolve(request: Request):
    """سشنِ معتبرِ این درخواست، یا None."""
    tok = request.cookies.get(_COOKIE)
    if not tok:
        return None
    now = _dt.datetime.utcnow()
    async with Session() as s:
        sess = await s.scalar(select(AdminWebSession).where(
            AdminWebSession.session_token == tok,
            AdminWebSession.revoked == False))
        if not sess or not sess.session_expires_at or sess.session_expires_at < now:
            return None
        bot_id, tg_id = sess.bot_id, sess.telegram_id
        r = await s.get(BotRow, bot_id)
    if not r:
        return None
    # هنوز مالک/ادمین هست؟ (در هر درخواست، نه فقط موقعِ ورود)
    from tenant import is_owner_or_admin
    if not await is_owner_or_admin(bot_id, tg_id, r.owner_id):
        return None
    if not await _plan_allows(bot_id):
        return None
    return {"bot_id": bot_id, "telegram_id": tg_id, "row": r}


@web_panel_router.get("/login/{token}")
async def panel_login(token: str):
    now = _dt.datetime.utcnow()
    async with Session() as s:
        sess = await s.scalar(select(AdminWebSession).where(
            AdminWebSession.login_token == token,
            AdminWebSession.revoked == False))
        if (not sess or sess.used_at is not None
                or not sess.login_expires_at or sess.login_expires_at < now):
            return HTMLResponse(_shell("ورود ناموفق", _msg(
                "این لینک منقضی شده یا قبلاً استفاده شده.<br>"
                "داخلِ رباتت دوباره <code>/panel</code> را بزن.")), status_code=403)
        sess.used_at = now
        sess.login_token = None          # توکنِ ورود دیگر قابلِ استفاده نیست
        sess.session_token = secrets.token_urlsafe(32)
        sess.session_expires_at = now + _SESSION_TTL
        await s.commit()
        stok = sess.session_token
    resp = RedirectResponse("/panel/", status_code=303)
    resp.set_cookie(_COOKIE, stok, httponly=True, samesite="lax",
                    secure=WEBHOOK_BASE.startswith("https"),
                    max_age=int(_SESSION_TTL.total_seconds()))
    return resp


@web_panel_router.get("/logout")
async def panel_logout(request: Request):
    tok = request.cookies.get(_COOKIE)
    if tok:
        async with Session() as s:
            sess = await s.scalar(select(AdminWebSession).where(
                AdminWebSession.session_token == tok))
            if sess:
                sess.revoked = True
                await s.commit()
    resp = RedirectResponse("/panel/", status_code=303)
    resp.delete_cookie(_COOKIE)
    return resp


# ─────────────── قالبِ HTML ───────────────

def _shell(title: str, body: str, nav: str = "") -> str:
    """=== PATCH_WEB_PANEL_THEME_V1 ===
    پوستهٔ بصریِ کلِ پنل. فونتِ وزیرمتن از CDN می‌آید ولی پشتِ سرش زنجیرهٔ
    فونت‌هایِ سیستمیِ فارسی هست، تا اگر مرورگرِ کاربر به CDN نرسید (که در
    ایران محتمل است) صفحه همچنان درست و فارسی بماند — نه اینکه به فونتِ
    لاتینِ پیش‌فرض بیفتد و به‌هم بریزد.
    """
    return f"""<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)}</title>
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<style>
@import url('https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css');
:root{{
  --bg:#0a0c12; --bg2:#12151f; --surface:#161a26; --surface2:#1d2231;
  --line:#272e40; --line2:#39425a;
  --tx:#eef1f7; --tx2:#8f9bb5;
  --acc:#5b8cff; --acc2:#7c5cff; --ok:#2fbf71; --dan:#e0554b;
  --r:16px; --sh:0 1px 0 rgba(255,255,255,.05) inset, 0 18px 40px -18px rgba(0,0,0,.9);
}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{margin:0;padding:0}}
body{{
  font-family:Vazirmatn,'Vazirmatn FD','IRANSans','Segoe UI',Tahoma,system-ui,sans-serif;
  background:var(--bg); color:var(--tx); min-height:100vh;
  font-feature-settings:"ss01";
  background-image:
    radial-gradient(900px 500px at 85% -10%, rgba(124,92,255,.16), transparent 60%),
    radial-gradient(700px 400px at 10% 0%, rgba(91,140,255,.14), transparent 60%);
  background-attachment:fixed;
}}
/* ── سربرگ ── */
header{{
  position:sticky; top:0; z-index:20;
  background:rgba(14,17,26,.82); backdrop-filter:blur(14px);
  border-bottom:1px solid var(--line);
  padding:13px 20px; display:flex; flex-wrap:wrap; gap:12px;
  align-items:center; justify-content:space-between;
}}
header h1{{
  margin:0; font-size:16px; font-weight:800; letter-spacing:-.2px;
  background:linear-gradient(92deg,var(--tx),var(--acc) 60%,var(--acc2));
  -webkit-background-clip:text; background-clip:text; color:transparent;
}}
nav{{display:flex;gap:4px;flex-wrap:wrap}}
nav a{{
  color:var(--tx2); text-decoration:none; font-size:13.5px; font-weight:600;
  padding:7px 13px; border-radius:11px; border:1px solid transparent;
  transition:.22s cubic-bezier(.2,.8,.2,1);
}}
nav a:hover{{color:var(--tx); background:var(--surface2); transform:translateY(-1px)}}
nav a.on{{
  color:#fff; background:linear-gradient(160deg,var(--acc),var(--acc2));
  box-shadow:0 8px 20px -8px var(--acc), 0 1px 0 rgba(255,255,255,.25) inset;
}}
main{{padding:22px 18px 60px; max-width:1060px; margin:0 auto;
  animation:rise .5s cubic-bezier(.2,.9,.25,1) both}}
@keyframes rise{{from{{opacity:0;transform:translateY(14px)}}}}
/* ── کارت‌های آمار (سه‌بعدی) ── */
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
  gap:14px;margin-bottom:24px;perspective:1100px}}
.card{{
  position:relative; overflow:hidden; padding:17px 16px;
  border-radius:var(--r); border:1px solid var(--line); box-shadow:var(--sh);
  background:linear-gradient(158deg,var(--surface2),var(--surface) 70%);
  transform-style:preserve-3d;
  transition:transform .3s cubic-bezier(.2,.8,.2,1), box-shadow .3s, border-color .3s;
}}
.card::before{{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(120deg,rgba(255,255,255,.07),transparent 42%)}}
.card::after{{content:"";position:absolute;top:-45%;right:-12%;width:130px;height:130px;
  border-radius:50%;background:radial-gradient(circle,rgba(91,140,255,.3),transparent 68%);
  pointer-events:none}}
.card:hover{{transform:translateY(-5px) rotateX(5deg) rotateY(-5deg);
  border-color:var(--line2);
  box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 26px 48px -18px rgba(0,0,0,.95)}}
.card .n{{font-size:25px;font-weight:800;letter-spacing:-.5px;
  font-variant-numeric:tabular-nums;position:relative}}
.card .l{{font-size:11.5px;color:var(--tx2);margin-top:5px;font-weight:600;position:relative}}
/* ── جدول ── */
table{{width:100%;border-collapse:collapse;background:var(--surface);
  border-radius:var(--r);border:1px solid var(--line);box-shadow:var(--sh);
  overflow:hidden;display:table}}
@media(max-width:700px){{table{{display:block;overflow-x:auto;white-space:nowrap}}}}
th,td{{padding:12px 13px;text-align:right;font-size:13px;border-bottom:1px solid var(--line)}}
th{{background:linear-gradient(180deg,var(--surface2),var(--surface));
  color:var(--tx2);font-weight:700;font-size:11.5px;letter-spacing:.3px;
  position:sticky;top:0}}
td{{font-variant-numeric:tabular-nums}}
tbody tr,table tr{{transition:background .18s}}
table tr:hover td{{background:rgba(91,140,255,.055)}}
tr:last-child td{{border-bottom:none}}
/* ── ورودی و دکمه (سه‌بعدی، با فشارِ واقعی) ── */
input{{
  font:inherit;font-size:13.5px;color:var(--tx);
  background:var(--bg2);border:1px solid var(--line);border-radius:12px;
  padding:10px 13px;transition:.2s;
  box-shadow:0 2px 6px rgba(0,0,0,.35) inset;
}}
input::placeholder{{color:#5d6884}}
input:focus{{outline:none;border-color:var(--acc);
  box-shadow:0 0 0 3px rgba(91,140,255,.17),0 2px 6px rgba(0,0,0,.35) inset}}
button{{
  font:inherit;font-size:13.5px;font-weight:700;color:#fff;cursor:pointer;
  border:none;border-radius:12px;padding:10px 18px;position:relative;
  background:linear-gradient(170deg,var(--acc),var(--acc2));
  box-shadow:0 5px 0 -1px #2d47a8, 0 12px 22px -10px var(--acc),
             0 1px 0 rgba(255,255,255,.32) inset;
  transform:translateY(0);
  transition:transform .1s ease, box-shadow .1s ease, filter .2s;
}}
button:hover{{filter:brightness(1.09)}}
button:active{{transform:translateY(4px);
  box-shadow:0 1px 0 -1px #2d47a8, 0 4px 10px -8px var(--acc),
             0 1px 0 rgba(255,255,255,.22) inset}}
button.d{{background:linear-gradient(170deg,#ef6a5e,var(--dan));
  box-shadow:0 5px 0 -1px #8d2a22, 0 12px 22px -10px var(--dan),
             0 1px 0 rgba(255,255,255,.3) inset}}
button.d:active{{box-shadow:0 1px 0 -1px #8d2a22, 0 4px 10px -8px var(--dan),
             0 1px 0 rgba(255,255,255,.2) inset}}
button.g{{background:linear-gradient(170deg,#43d98a,var(--ok));
  box-shadow:0 5px 0 -1px #1a7846, 0 12px 22px -10px var(--ok),
             0 1px 0 rgba(255,255,255,.3) inset}}
/* ── پیام ── */
.msg{{
  background:linear-gradient(158deg,var(--surface2),var(--surface) 70%);
  border:1px solid var(--line); border-radius:var(--r);
  padding:26px 22px; text-align:center; line-height:2.1; font-size:14px;
  box-shadow:var(--sh); margin-bottom:18px;
}}
code{{background:var(--bg2);border:1px solid var(--line);padding:2px 7px;
  border-radius:7px;font-size:12.5px;font-family:ui-monospace,Menlo,Consolas,monospace;
  direction:ltr;display:inline-block}}
a{{color:var(--acc)}}
.pg{{margin-top:16px;display:flex;gap:9px;flex-wrap:wrap}}
.pg a{{color:var(--tx2);text-decoration:none;background:var(--surface);
  border:1px solid var(--line);padding:9px 16px;border-radius:12px;
  font-size:13px;font-weight:600;transition:.22s}}
.pg a:hover{{color:var(--tx);border-color:var(--acc);transform:translateY(-2px);
  box-shadow:0 10px 20px -12px var(--acc)}}
form{{margin:0}}
@media(max-width:560px){{
  main{{padding:16px 12px 50px}} header{{padding:11px 14px}}
  .card .n{{font-size:21px}} th,td{{padding:10px 9px;font-size:12.5px}}
}}
@media(prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}
</style></head><body>
<header><h1>{_html.escape(title)}</h1><nav>{nav}</nav></header>
<main>{body}</main></body></html>"""


def _msg(t: str) -> str:
    return f'<div class="msg">{t}</div>'


def _nav(active: str, uname: str | None) -> str:
    items = [("/panel/", "داشبورد", "home"),
             ("/panel/users", "کاربران", "users"),
             ("/panel/orders", "سفارش‌ها", "orders"),
             ("/panel/services", "سرویس‌ها", "services")]
    out = "".join(
        f'<a href="{u}" class="{"on" if k == active else ""}">{t}</a>'
        for u, t, k in items)
    return out + '<a href="/panel/logout">خروج</a>'


def _need_login() -> HTMLResponse:
    return HTMLResponse(_shell("پنل مدیریت", _msg(
        "برای ورود، داخلِ رباتِ خودت دستورِ <code>/panel</code> را بفرست "
        "و روی لینکی که می‌آید بزن.<br>"
        "اگر قبلاً وارد بودی، سشنت منقضی شده است.")), status_code=401)


def _fa(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


# ─────────────── صفحه‌ها ───────────────

@web_panel_router.get("/")
async def panel_home(request: Request):
    ctx = await _resolve(request)
    if not ctx:
        return _need_login()
    bot_id = ctx["bot_id"]
    now = _dt.datetime.utcnow()
    async with Session() as s:
        users = int(await s.scalar(select(func.count()).select_from(BotUser).where(
            BotUser.bot_id == bot_id)) or 0)
        new_7d = int(await s.scalar(select(func.count()).select_from(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.joined_at >= now - _dt.timedelta(days=7))) or 0)
        banned = int(await s.scalar(select(func.count()).select_from(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.is_banned == True)) or 0)
        orders = int(await s.scalar(select(func.count()).select_from(Order).where(
            Order.bot_id == bot_id, Order.status == "paid")) or 0)
        revenue = int(await s.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(
            Order.bot_id == bot_id, Order.status == "paid")) or 0)
        balances = int(await s.scalar(select(func.coalesce(func.sum(BotUser.balance), 0)).where(
            BotUser.bot_id == bot_id)) or 0)
        services = int(await s.scalar(select(func.count()).select_from(PanelAccount).where(
            PanelAccount.bot_id == bot_id)) or 0)
        pending = int(await s.scalar(select(func.count()).select_from(Order).where(
            Order.bot_id == bot_id, Order.status == "awaiting_admin")) or 0)
    cards = [("کاربران", _fa(users)), ("کاربر جدید (۷ روز)", _fa(new_7d)),
             ("مسدود", _fa(banned)), ("سفارشِ پرداخت‌شده", _fa(orders)),
             ("درآمد (تومان)", _fa(revenue)), ("مجموعِ کیف پول‌ها", _fa(balances)),
             ("سرویسِ ساخته‌شده", _fa(services)), ("در انتظارِ تأیید", _fa(pending))]
    body = '<div class="cards">' + "".join(
        f'<div class="card"><div class="n">{v}</div><div class="l">{l}</div></div>'
        for l, v in cards) + "</div>"
    if pending:
        body += _msg(f"⏳ {_fa(pending)} سفارش در انتظارِ تأییدِ توست. "
                     "تأیید/ردِ رسید فقط داخلِ خودِ ربات انجام می‌شود.")
    uname = ctx["row"].username
    return HTMLResponse(_shell(f"پنل @{uname}", body, _nav("home", uname)))


@web_panel_router.get("/users")
async def panel_users(request: Request, q: str = "", page: int = 0):
    ctx = await _resolve(request)
    if not ctx:
        return _need_login()
    bot_id = ctx["bot_id"]
    page = max(0, int(page or 0))
    stmt = select(BotUser).where(BotUser.bot_id == bot_id)
    if q.strip():
        term = f"%{q.strip()}%"
        if q.strip().isdigit():
            stmt = stmt.where(BotUser.telegram_id == int(q.strip()))
        else:
            stmt = stmt.where(BotUser.full_name.ilike(term) | BotUser.username.ilike(term))
    async with Session() as s:
        rows = (await s.scalars(stmt.order_by(BotUser.id.desc())
                                .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).all()
        data = [(u.id, u.telegram_id, u.full_name, u.username, int(u.balance or 0),
                 u.is_banned, u.joined_at) for u in rows]
    trs = ""
    for uid, tg, name, un, bal, ban, joined in data:
        trs += (
            "<tr>"
            f"<td><code>{tg}</code></td>"
            f"<td>{_html.escape(name or '—')}</td>"
            f"<td>{('@' + _html.escape(un)) if un else '—'}</td>"
            f"<td>{_fa(bal)}</td>"
            f"<td>{'🔴 مسدود' if ban else '🟢 عادی'}</td>"
            f"<td>{joined.strftime('%Y-%m-%d') if joined else '—'}</td>"
            '<td><form method="post" action="/panel/users/ban" style="margin:0">'
            f'<input type="hidden" name="user_id" value="{uid}">'
            f'<button class="{"" if ban else "d"}">'
            f'{"رفعِ مسدودی" if ban else "مسدود کن"}</button></form></td>'
            "</tr>")
    body = (
        '<form method="get" style="margin-bottom:14px;display:flex;gap:8px">'
        f'<input name="q" value="{_html.escape(q)}" placeholder="جست‌وجو: نام، یوزرنیم یا آیدی عددی">'
        '<button>جست‌وجو</button></form>'
        "<table><tr><th>آیدی</th><th>نام</th><th>یوزرنیم</th><th>کیف پول</th>"
        "<th>وضعیت</th><th>عضویت</th><th></th></tr>"
        + (trs or '<tr><td colspan="7">چیزی پیدا نشد.</td></tr>') + "</table>")
    qs = f"&q={_html.escape(q)}" if q else ""
    body += '<div class="pg">'
    if page > 0:
        body += f'<a href="/panel/users?page={page-1}{qs}">→ قبلی</a>'
    if len(data) == _PAGE_SIZE:
        body += f'<a href="/panel/users?page={page+1}{qs}">بعدی ←</a>'
    body += "</div>"
    return HTMLResponse(_shell("کاربران", body, _nav("users", ctx["row"].username)))


@web_panel_router.post("/users/ban")
async def panel_users_ban(request: Request, user_id: int = Form(...)):
    ctx = await _resolve(request)
    if not ctx:
        return _need_login()
    async with Session() as s:
        u = await s.get(BotUser, user_id)
        # bot_id را دوباره چک کن — وگرنه با دست‌کاریِ فرم می‌شد کاربرِ رباتِ
        # دیگری را مسدود کرد.
        from config import PLATFORM_ADMIN_ID as _PA   # PATCH_BAN_ONLY_MAIN_ADMIN_EXEMPT_V1
        if u and u.bot_id == ctx["bot_id"] and not (_PA and int(u.telegram_id) == int(_PA)):
            u.is_banned = not u.is_banned
            await s.commit()
    return RedirectResponse("/panel/users", status_code=303)


@web_panel_router.get("/orders")
async def panel_orders(request: Request, page: int = 0):
    ctx = await _resolve(request)
    if not ctx:
        return _need_login()
    page = max(0, int(page or 0))
    async with Session() as s:
        rows = (await s.scalars(select(Order).where(Order.bot_id == ctx["bot_id"])
                                .order_by(Order.id.desc())
                                .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).all()
        data = []
        for o in rows:
            u = await s.get(BotUser, o.user_id)
            data.append((o.id, u.telegram_id if u else "—", int(o.amount or 0),
                         o.pay_method, o.status, o.tracking_code, o.created_at))
    labels = {"paid": "🟢 پرداخت‌شده", "rejected": "🔴 رد شده",
              "awaiting_admin": "⏳ در انتظارِ تأیید",
              "awaiting_receipt": "🧾 منتظرِ رسید", "cancelled": "⚪️ لغو شده"}
    trs = "".join(
        f"<tr><td>#{oid}</td><td><code>{tg}</code></td><td>{_fa(amt)}</td>"
        f"<td>{_html.escape(pm or '—')}</td><td>{labels.get(st, st)}</td>"
        f"<td><code>{_html.escape(trk or '—')}</code></td>"
        f"<td>{c.strftime('%Y-%m-%d %H:%M') if c else '—'}</td></tr>"
        for oid, tg, amt, pm, st, trk, c in data)
    body = ("<table><tr><th>سفارش</th><th>کاربر</th><th>مبلغ</th><th>روشِ پرداخت</th>"
            "<th>وضعیت</th><th>کدِ پیگیری</th><th>تاریخ</th></tr>"
            + (trs or '<tr><td colspan="7">سفارشی نیست.</td></tr>') + "</table>")
    body += '<div class="pg">'
    if page > 0:
        body += f'<a href="/panel/orders?page={page-1}">→ قبلی</a>'
    if len(data) == _PAGE_SIZE:
        body += f'<a href="/panel/orders?page={page+1}">بعدی ←</a>'
    body += "</div>"
    return HTMLResponse(_shell("سفارش‌ها", body, _nav("orders", ctx["row"].username)))


@web_panel_router.get("/services")
async def panel_services(request: Request, page: int = 0):
    ctx = await _resolve(request)
    if not ctx:
        return _need_login()
    page = max(0, int(page or 0))
    async with Session() as s:
        rows = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == ctx["bot_id"]).order_by(PanelAccount.id.desc())
            .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).all()
        data = [(a.svc_code, a.user_telegram_id, a.panel_key, a.plan_title,
                 a.volume_gb, a.days, a.status, a.expires_at) for a in rows]
    trs = "".join(
        f"<tr><td><code>{_html.escape(str(code or '—'))}</code></td>"
        f"<td><code>{tg}</code></td><td>{_html.escape(pk or '—')}</td>"
        f"<td>{_html.escape(title or '—')}</td>"
        f"<td>{'نامحدود' if not gb else str(gb) + ' گیگ'}</td>"
        f"<td>{'بی‌انقضا' if not d else str(d) + ' روز'}</td>"
        f"<td>{_html.escape(st or '—')}</td>"
        f"<td>{exp.strftime('%Y-%m-%d') if exp else '—'}</td></tr>"
        for code, tg, pk, title, gb, d, st, exp in data)
    body = ("<table><tr><th>کد</th><th>کاربر</th><th>پنل</th><th>عنوان</th>"
            "<th>حجم</th><th>مدت</th><th>وضعیت</th><th>انقضا</th></tr>"
            + (trs or '<tr><td colspan="8">سرویسی نیست.</td></tr>') + "</table>")
    body += '<div class="pg">'
    if page > 0:
        body += f'<a href="/panel/services?page={page-1}">→ قبلی</a>'
    if len(data) == _PAGE_SIZE:
        body += f'<a href="/panel/services?page={page+1}">بعدی ←</a>'
    body += "</div>"
    return HTMLResponse(_shell("سرویس‌ها", body, _nav("services", ctx["row"].username)))


# ═══════════════ PATCH_RESELLER_WEB_V1 — پنلِ نماینده ═══════════════
#
# جدا از پنلِ بالا (که مالِ خودِ مالکِ ربات است و با تلگرام وارد می‌شود).
# این یکی برایِ کسی است که مالک به او نمایندگی داده: با یوزر/پسورد وارد
# می‌شود، از پنل‌هایی که مالک برایش تیک زده کانفیگ می‌سازد، و هزینه‌اش از
# کیفِ پولِ خودش در همان ربات کم می‌شود (با درصدِ تخفیفِ نمایندگی‌اش).
#
# ⚠️ اصلِ طراحی: نماینده هرگز به تنظیماتِ پنل، آدرس، یا کلیدِ API دسترسی
# ندارد — فقط «بساز» و «ببین». هر ساخت، پول را *قبل* از تماس با پنل کم
# می‌کند و اگر ساخت شکست خورد، همان لحظه برمی‌گرداند.

import hashlib as _hashlib

_RS_COOKIE = "botsaz_reseller"
_RS_SESSION_TTL = _dt.timedelta(hours=8)
_RS_MAX_FAILS = 5
_RS_LOCK = _dt.timedelta(minutes=15)


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = _hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"pbkdf2$200000${salt}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored or stored.count("$") != 3:
        return False
    try:
        _, iters, salt, want = stored.split("$")
        dk = _hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(dk.hex(), want)
    except Exception:
        return False


async def _rs_resolve(request: Request):
    """نمایندهٔ واردشدهٔ این درخواست، یا None."""
    from models import VpnPartner
    tok = request.cookies.get(_RS_COOKIE)
    if not tok:
        return None
    now = _dt.datetime.utcnow()
    async with Session() as s:
        sess = await s.scalar(select(AdminWebSession).where(
            AdminWebSession.session_token == tok,
            AdminWebSession.revoked == False))
        if not sess or not sess.session_expires_at or sess.session_expires_at < now:
            return None
        # نمایندگی هنوز برقرار است؟ (در هر درخواست، نه فقط موقعِ ورود)
        p = await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == sess.bot_id,
            VpnPartner.telegram_id == sess.telegram_id))
        if not p or not p.active or not p.web_enabled:
            return None
        r = await s.get(BotRow, sess.bot_id)
        if not r:
            return None
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == sess.bot_id, BotUser.telegram_id == sess.telegram_id))
        data = {"bot_id": sess.bot_id, "telegram_id": sess.telegram_id,
                "percent": int(p.discount_percent or 0),
                "panels": [k.strip() for k in (p.panel_keys or "").split(",") if k.strip()],
                "apply_all": bool(getattr(p, "apply_all", False)),
                "balance": int(u.balance or 0) if u else 0,
                "note": p.note, "username": p.web_username, "row": r,
                # === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
                "trial_limit": int(getattr(p, "trial_daily_limit", 0) or 0)}
    if not await _plan_allows(data["bot_id"]):
        return None
    return data


def _rs_nav(active: str) -> str:
    # === PATCH_RESELLER_WEB_CUSTOM_TRIAL_V1 ===
    items = [("/panel/r/", "پلن آماده", "new"),
             ("/panel/r/custom", "فروش آنلاین", "custom"),
             ("/panel/r/trial", "تست رایگان", "trial"),
             ("/panel/r/list", "کانفیگ‌های من", "list")]
    return "".join(
        f'<a href="{u}" class="{"on" if k == active else ""}">{t}</a>'
        for u, t, k in items) + '<a href="/panel/r/logout">خروج</a>'


def _rs_login_page(err: str = "") -> str:
    """=== PATCH_WEB_PANEL_THEME_V1 === صفحهٔ ورودِ نماینده."""
    e = (f'<div style="color:#ff8b82;background:rgba(224,85,75,.1);'
         f'border:1px solid rgba(224,85,75,.35);border-radius:12px;'
         f'padding:11px;font-size:13px;font-weight:600">{_html.escape(err)}</div>'
         ) if err else ""
    return _shell("ورود نماینده", f"""<div style="min-height:74vh;display:flex;
align-items:center;justify-content:center;perspective:1200px">
<div class="card" style="width:100%;max-width:360px;padding:34px 28px;text-align:center">
  <div style="font-size:40px;line-height:1;margin-bottom:6px">🛡</div>
  <h2 style="margin:0 0 4px;font-size:20px;font-weight:800;letter-spacing:-.4px;
     background:linear-gradient(92deg,var(--tx),var(--acc) 55%,var(--acc2));
     -webkit-background-clip:text;background-clip:text;color:transparent;
     position:relative">پنل نمایندگی</h2>
  <p style="margin:0 0 22px;color:var(--tx2);font-size:12.5px;position:relative">
     برای ورود، نام کاربری و رمزت را وارد کن</p>
  <form method="post" action="/panel/r/login"
        style="display:grid;gap:12px;position:relative">
    {e}
    <input name="username" placeholder="نام کاربری" autocomplete="username"
           style="text-align:center">
    <input name="password" type="password" placeholder="رمز عبور"
           autocomplete="current-password" style="text-align:center">
    <button style="width:100%;padding:12px;font-size:14.5px;margin-top:2px">ورود</button>
  </form>
  <p style="margin:20px 0 0;font-size:11.5px;color:var(--tx2);line-height:1.9;
     position:relative">نام کاربری و رمز را مدیرِ ربات به تو می‌دهد.</p>
</div></div>""")


@web_panel_router.get("/r/login")
async def rs_login_form():
    return HTMLResponse(_rs_login_page())


@web_panel_router.post("/r/login")
async def rs_login(username: str = Form(""), password: str = Form("")):
    from models import VpnPartner
    now = _dt.datetime.utcnow()
    uname = (username or "").strip()
    if not uname or not password:
        return HTMLResponse(_rs_login_page("نام کاربری و رمز را وارد کن."), status_code=400)
    async with Session() as s:
        p = await s.scalar(select(VpnPartner).where(VpnPartner.web_username == uname))
        # پیامِ خطا عمداً یکسان است تا نشود فهمید کدام نام کاربری وجود دارد.
        bad = HTMLResponse(_rs_login_page("نام کاربری یا رمز اشتباه است."), status_code=401)
        if not p or not p.web_enabled or not p.active:
            return bad
        if p.web_locked_until and p.web_locked_until > now:
            return HTMLResponse(_rs_login_page(
                "به‌خاطرِ تلاش‌های ناموفق، ورود موقتاً قفل شده. کمی بعد دوباره امتحان کن."),
                status_code=429)
        if not verify_password(password, p.web_password_hash):
            p.web_fail_count = int(p.web_fail_count or 0) + 1
            if p.web_fail_count >= _RS_MAX_FAILS:
                p.web_locked_until = now + _RS_LOCK
                p.web_fail_count = 0
            await s.commit()
            return bad
        p.web_fail_count = 0
        p.web_locked_until = None
        sess = AdminWebSession(
            bot_id=p.bot_id, telegram_id=p.telegram_id,
            session_token=secrets.token_urlsafe(32),
            session_expires_at=now + _RS_SESSION_TTL, used_at=now)
        s.add(sess)
        await s.commit()
        stok = sess.session_token
    resp = RedirectResponse("/panel/r/", status_code=303)
    resp.set_cookie(_RS_COOKIE, stok, httponly=True, samesite="lax",
                    secure=WEBHOOK_BASE.startswith("https"),
                    max_age=int(_RS_SESSION_TTL.total_seconds()))
    return resp


@web_panel_router.get("/r/logout")
async def rs_logout(request: Request):
    tok = request.cookies.get(_RS_COOKIE)
    if tok:
        async with Session() as s:
            sess = await s.scalar(select(AdminWebSession).where(
                AdminWebSession.session_token == tok))
            if sess:
                sess.revoked = True
                await s.commit()
    resp = RedirectResponse("/panel/r/login", status_code=303)
    resp.delete_cookie(_RS_COOKIE)
    return resp


async def _rs_plans(bot_id: int, panels: list[str], apply_all: bool):
    """پلن‌هایِ فعالی که این نماینده اجازهٔ ساخت از رویشان را دارد."""
    from models import VpnPlan
    async with Session() as s:
        stmt = select(VpnPlan).where(VpnPlan.bot_id == bot_id, VpnPlan.active == True)
        if not apply_all and panels:
            stmt = stmt.where(VpnPlan.panel_key.in_(panels))
        elif not apply_all and not panels:
            return []
        rows = (await s.scalars(stmt.order_by(VpnPlan.sort_order, VpnPlan.id))).all()
        return [(p.id, p.title, p.panel_key, int(p.price or 0),
                 p.volume_gb, p.days) for p in rows]


def _rs_price(base: int, percent: int) -> int:
    return max(0, int(round(base * (100 - max(0, min(100, percent))) / 100)))


@web_panel_router.get("/r/")
async def rs_home(request: Request, msg: str = "", err: str = ""):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    plans = await _rs_plans(ctx["bot_id"], ctx["panels"], ctx["apply_all"])
    head = (f'<div class="cards">'
            f'<div class="card"><div class="n">{_fa(ctx["balance"])}</div>'
            f'<div class="l">موجودیِ کیف پول (تومان)</div></div>'
            f'<div class="card"><div class="n">{ctx["percent"]}٪</div>'
            f'<div class="l">تخفیفِ نمایندگی</div></div></div>')
    head += await _rs_panel_discounts(ctx)   # PATCH_RESELLER_WEB_PER_PANEL_V1
    if msg:
        head += _msg(_html.escape(msg))
    if err:
        head += f'<div class="msg" style="color:#e06c6c">{_html.escape(err)}</div>'
    if not plans:
        return HTMLResponse(_shell("ساخت کانفیگ", head + _msg(
            "هنوز هیچ پلنی برای تو فعال نشده. با مدیرِ ربات تماس بگیر."),
            _rs_nav("new")))
    trs = ""
    for pid, title, pk, price, gb, days in plans:
        final = _rs_price(price, ctx["percent"])
        trs += (
            "<tr>"
            f"<td>{_html.escape(title or '—')}</td>"
            f"<td>{'نامحدود' if not gb else str(gb) + ' گیگ'}</td>"
            f"<td>{'بی‌انقضا' if not days else str(days) + ' روز'}</td>"
            f"<td>{_fa(price)}</td><td><b>{_fa(final)}</b></td>"
            '<td><form method="post" action="/panel/r/create" style="margin:0;display:flex;gap:6px">'
            f'<input type="hidden" name="plan_id" value="{pid}">'
            '<input name="count" value="1" size="2" style="width:52px">'
            '<button>بساز</button></form></td></tr>')
    body = head + (
        "<table><tr><th>پلن</th><th>حجم</th><th>مدت</th><th>قیمت</th>"
        "<th>قیمتِ تو</th><th></th></tr>" + trs + "</table>")
    return HTMLResponse(_shell("ساخت کانفیگ", body, _rs_nav("new")))


@web_panel_router.post("/r/create")
async def rs_create(request: Request, plan_id: int = Form(...), count: str = Form("1")):
    from models import VpnPlan, VpnPanel, WalletTxn
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    bot_id, tg_id = ctx["bot_id"], ctx["telegram_id"]
    try:
        n = max(1, min(20, int(str(count).strip())))
    except Exception:
        n = 1

    def back(msg="", err=""):
        from urllib.parse import quote
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(f"/panel/r/{q}", status_code=303)

    # ── مجوز + قیمت + کسرِ اتمیکِ کیف پول، همه در یک تراکنش ──
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != bot_id or not plan.active:
            return back(err="این پلن در دسترس نیست.")
        if not ctx["apply_all"] and plan.panel_key not in ctx["panels"]:
            return back(err="اجازهٔ ساخت از این پنل را نداری.")
        unit = _rs_price(int(plan.price or 0), ctx["percent"])
        total = unit * n
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id).with_for_update())
        if not u:
            return back(err="حسابِ کاربریِ تو در این ربات پیدا نشد.")
        if int(u.balance or 0) < total:
            return back(err=f"موجودیِ کیف پولت کافی نیست. لازم: {_fa(total)} تومان.")
        u.balance = int(u.balance or 0) - total
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-total, kind="purchase",
                        note=f"ساختِ {n} کانفیگ از پنلِ نمایندگی ({plan.title})"))
        await s.commit()
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == plan.panel_key))
        gb, days, title, pkey = plan.volume_gb, plan.days, plan.title, plan.panel_key
        uid, uname = u.id, u.username

    async def _refund(amount: int, why: str):
        """پول را برمی‌گرداند. بدونِ این، شکستِ پنل یعنی نماینده پول داده
        و چیزی نگرفته."""
        async with Session() as s2:
            u2 = await s2.scalar(select(BotUser).where(BotUser.id == uid).with_for_update())
            if u2:
                u2.balance = int(u2.balance or 0) + amount
                s2.add(WalletTxn(bot_id=bot_id, user_id=uid, amount=amount,
                                 kind="charge", note=f"بازگشتِ وجه — {why}"))
                await s2.commit()

    if not panel or not panel.enabled:
        await _refund(total, "پنل در دسترس نبود")
        return back(err="پنلِ این پلن در دسترس نیست. مبلغ به کیف پولت برگشت.")

    from tenant.bots.vpn.vpn_user import (_client_from_panel, _make_config_email, new_svc_code,
                                 _expires_at_from_ms)
    from tenant.shared.panel_clients.vpn_panel import gen_uuid, gen_sub_id
    client = _client_from_panel(panel)
    made = 0
    _gb_i = int(gb) if gb else 0   # PATCH_WEBPANEL_GBI_FIX_V1 (قبلاً تعریف نشده بود → NameError)
    try:
        await client.login()
        for _ in range(n):
            try:
                email = await _make_config_email(bot_id, pkey, ctx["row"].username, tg_id, uname)
                acc = await client.create_account(email, gb=gb, days=days, limit_ip=0,
                                                  uuid_str=gen_uuid(), sub_id=gen_sub_id())
                async with Session() as s3:
                    s3.add(PanelAccount(
                        bot_id=bot_id, user_telegram_id=tg_id, email=email, panel_key=pkey,
                        uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                        sub_link=acc.get("sub_link") or "", volume_gb=_gb_i, days=days,
                        plan_title=title or _plan_title_fa(gb, days), status="active",
                        reseller_made=True,   # PATCH_RESELLER_SEPARATE_CONFIGS_V1
                        svc_code=await new_svc_code(s3, bot_id),
                        expires_at=_expires_at_from_ms(acc.get("expiry_ms")),
                        first_conn_notified=False))
                    await s3.commit()
                made += 1
            except Exception as e:
                logger.warning("پنلِ نماینده: ساختِ کانفیگ ناموفق (bot=%s panel=%s): %s",
                               bot_id, pkey, e)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("پنلِ نماینده: بستنِ کلاینت ناموفق")

    if made < n:
        await _refund(unit * (n - made), f"{n - made} کانفیگ ساخته نشد")
    if made == 0:
        return back(err="ساختِ کانفیگ ناموفق بود. مبلغ کامل به کیف پولت برگشت.")

    # === PATCH_RESELLER_WEB_V1 ===
    # مالک باید بداند نماینده چه ساخته و چقدر از کیفِ پولش رفته — این مسیر
    # کاملاً بیرونِ تلگرام اتفاق می‌افتد، پس بدونِ این اعلان، فروش برایِ
    # مالک نامرئی می‌ماند (و کانالِ اعتمادسازی هم خالی می‌ماند).
    try:
        from aiogram import Bot as _AiogramBot
        from tenant import _purchase_alert
        async with Session() as s4:
            _u = await s4.get(BotUser, uid)
            _row = await s4.get(BotRow, bot_id)
        if _row and _u:
            _tb = _AiogramBot(token=_row.token)
            try:
                await _purchase_alert(
                    _tb, _row, buyer=_u, section="پنلِ وبِ نماینده",
                    detail=f"{title or ''} × {made} عدد", amount=float(unit * made),
                    count=made)
            finally:
                await _tb.session.close()
    except Exception:
        logger.exception("پنلِ نماینده: اطلاعِ خرید به مالک ناموفق بود (نادیده گرفته شد)")

    extra = "" if made == n else f" ({n - made} مورد ناموفق بود و پولش برگشت)"
    return back(msg=f"✅ {made} کانفیگ ساخته شد{extra}. از «کانفیگ‌های من» ببینشان.")


# ═══════════════ PATCH_RESELLER_WEB_CUSTOM_TRIAL_V1 ═══════════════
# نماینده تا حالا فقط «پلن آماده» را در وب می‌دید. دو چیزی که در تلگرام
# داشت اینجا نبود: فروشِ آنلاینِ حجم/روزِ دلخواه، و تستِ رایگان. هر دو
# همان توابعِ قیمت‌گذاریِ ربات را صدا می‌زنند تا قیمت دو جا حساب نشود.

# === PATCH_RESELLER_PANEL_LABEL_FIX_V1 ===
# باگ: مدلِ VpnPanel اصلاً ستونِ title ندارد و صفحه با AttributeError
# می‌افتاد (همان «Internal Server Error»). نامِ نمایشیِ پنل از همان
# نگاشتِ PANEL_LABELS می‌آید که بقیهٔ ربات هم استفاده می‌کند.
async def _panel_label(p) -> str:
    # === PATCH_PANEL_CUSTOM_LABEL_V1 === نامِ دلخواهِ مالکِ ربات، اگر گذاشته
    try:
        from tenant.bots.vpn.vpn_admin import panel_label
        return await panel_label(p.bot_id, p.panel_key)
    except Exception:
        return p.panel_key


# === PATCH_RESELLER_TITLE_FA_V1 ===
# عنوانِ کانفیگ در همه‌جا دیده می‌شود — از «سرویس‌های من» تا پیامِ کانالِ
# اعتمادسازی. «5G / 365D» آنجا وصله‌ای انگلیسی وسطِ متنِ فارسی بود.
def _plan_title_fa(gb, days) -> str:
    try:
        g = float(gb)
    except (TypeError, ValueError):
        g = 0
    vol = "نامحدود" if g <= 0 else (
        f"{int(round(g * 1024))} مگ" if g < 1 else
        f"{int(g) if g == int(g) else g} گیگ")
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 0
    dur = "بی‌انقضا" if d <= 0 else f"{d} روز"
    return f"{vol} / {dur}"


async def _rs_make_accounts(bot_id: int, tg_id: int, uname, panel, pkey: str,
                            gb, days: int, count: int, title: str,
                            hours: int = 0) -> int:
    """ساختِ count کانفیگ رویِ پنل. خروجی: تعدادِ موفق."""
    from tenant.bots.vpn.vpn_user import (_client_from_panel, _make_config_email,
                                 new_svc_code, _expires_at_from_ms)
    from tenant.shared.panel_clients.vpn_panel import gen_uuid, gen_sub_id
    async with Session() as s0:
        row = await s0.get(BotRow, bot_id)
    client = _client_from_panel(panel)
    made = 0
    try:
        await client.login()
        for _ in range(count):
            try:
                email = await _make_config_email(
                    bot_id, pkey, row.username if row else "", tg_id, uname)
                # === PATCH_RESELLER_TRIAL_CFG_FIX_V1 ===
                # زیرِ یک گیگ باید به مگابایت داده شود، وگرنه گِرد می‌شود
                # به صفر و پنل آن را «نامحدود» می‌فهمد.
                _gb_i, _mb = int(gb), None
                if float(gb) < 1:
                    _gb_i, _mb = 0, max(1, int(round(float(gb) * 1024)))
                kw = {"gb": _gb_i, "days": days, "limit_ip": 0,
                      "uuid_str": gen_uuid(), "sub_id": gen_sub_id()}
                if _mb:
                    kw["mb"] = _mb
                if hours:
                    kw["hours"] = hours
                try:
                    acc = await client.create_account(email, **kw)
                except TypeError:
                    # پنلی که پارامترِ ساعت یا مگابایت را نمی‌شناسد
                    kw.pop("hours", None)
                    kw.pop("mb", None)
                    acc = await client.create_account(email, **kw)
                async with Session() as s3:
                    s3.add(PanelAccount(
                        bot_id=bot_id, user_telegram_id=tg_id, email=email,
                        panel_key=pkey, uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                        sub_link=acc.get("sub_link") or "", volume_gb=gb, days=days,
                        plan_title=title, status="active",
                        reseller_made=True,   # PATCH_RESELLER_SEPARATE_CONFIGS_V1
                        svc_code=await new_svc_code(s3, bot_id),
                        expires_at=_expires_at_from_ms(acc.get("expiry_ms")),
                        first_conn_notified=False))
                    await s3.commit()
                made += 1
            except Exception as e:
                logger.warning("پنلِ نماینده: ساختِ کانفیگ ناموفق (bot=%s panel=%s): %s",
                               bot_id, pkey, e)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("پنلِ نماینده: بستنِ کلاینت ناموفق")
    return made


async def _rs_panel_row(bot_id: int, pkey: str):
    from models import VpnPanel
    async with Session() as s:
        return await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == pkey))


async def _rs_allowed_panels(ctx) -> list:
    """پنل‌های روشنی که این نماینده اجازهٔ کار با آن‌ها را دارد."""
    from models import VpnPanel
    async with Session() as s:
        rows = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == ctx["bot_id"], VpnPanel.enabled == True))).all()
    if ctx["apply_all"]:
        return list(rows)
    allowed = set(ctx["panels"])
    return [p for p in rows if p.panel_key in allowed]


async def _rs_notify_owner(bot_id: int, user_row_id: int, section: str,
                           detail: str, amount: float, count: int) -> None:
    """اعلانِ فروش به مالکِ ربات — این مسیر کاملاً بیرونِ تلگرام است و
    بدونِ این اعلان، فروشِ نماینده برای مالک نامرئی می‌ماند."""
    try:
        from aiogram import Bot as _AiogramBot
        from tenant import _purchase_alert
        async with Session() as s:
            _u = await s.get(BotUser, user_row_id)
            _row = await s.get(BotRow, bot_id)
        if _row and _u:
            _tb = _AiogramBot(token=_row.token)
            try:
                await _purchase_alert(_tb, _row, buyer=_u, section=section,
                                      detail=detail, amount=amount, count=count)
            finally:
                await _tb.session.close()
    except Exception:
        logger.exception("پنلِ نماینده: اطلاعِ خرید به مالک ناموفق بود (نادیده گرفته شد)")


async def _rs_panel_discounts(ctx) -> str:
    """=== PATCH_RESELLER_WEB_PER_PANEL_V1 ===
    تخفیفِ هر پنل جدا شده، پس نماینده باید ببیند رویِ کدام پنل چقدر
    تخفیف دارد — وگرنه عددِ کلیِ بالایِ صفحه گمراهش می‌کند.
    """
    try:
        from tenant.bots.vpn.vpn_partners import partner_discount_percent
        rows = []
        for p in await _rs_allowed_panels(ctx):
            pct = await partner_discount_percent(
                ctx["bot_id"], ctx["telegram_id"], p.panel_key, "plans")
            rows.append(f'<div class="kv"><span>{_html.escape(await _panel_label(p))}</span>'
                        f'<b>{pct}٪</b></div>')
        if not rows:
            return ""
        return ('<div class="card"><div class="meta" style="margin-bottom:8px">'
                'تخفیفِ تو روی هر پنل</div>' + "".join(rows) + '</div>')
    except Exception:
        logger.exception("نمایشِ تخفیفِ پنلیِ نماینده ناموفق بود")
        return ""


def _rs_head(ctx, msg: str = "", err: str = "") -> str:
    out = ('<div class="cards">'
           f'<div class="card"><div class="n">{_fa(ctx["balance"])}</div>'
           '<div class="l">موجودیِ کیف پول (تومان)</div></div>'
           f'<div class="card"><div class="n">{ctx["percent"]}٪</div>'
           '<div class="l">تخفیفِ نمایندگی</div></div></div>')
    if msg:
        out += _msg(_html.escape(msg))
    if err:
        out += f'<div class="msg" style="color:#e06c6c">{_html.escape(err)}</div>'
    return out


def _rs_back(path: str, msg: str = "", err: str = ""):
    from urllib.parse import quote
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/panel/r/{path}{q}", status_code=303)


# ─────────────── فروشِ آنلاین (حجم/روزِ دلخواه) ───────────────

# === PATCH_RESELLER_WEB_ERRORS_V1 ===
# صفحاتِ تازه در سرور «Internal Server Error» می‌دادند و هیچ ردی هم در
# لاگ نمی‌ماند، چون FastAPI خطا را می‌بلعید. این دکوراتور تِرِیس‌بکِ کامل
# را در error.log می‌نویسد و به‌جای صفحهٔ سفید، پیامِ فارسی نشان می‌دهد.
def _rs_guard(fn):
    import functools

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except Exception:
            logger.exception("پنلِ نماینده: خطا در %s", fn.__name__)
            return HTMLResponse(_shell(
                "خطا",
                _msg("متأسفیم، این صفحه خطا داد. به مدیرِ ربات اطلاع بده تا "
                     "لاگِ سرور را ببیند."),
                _rs_nav("")), status_code=500)
    return wrapper


@web_panel_router.get("/r/custom")
@_rs_guard
async def rs_custom_form(request: Request, msg: str = "", err: str = ""):
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _vc
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    live = []
    for p in await _rs_allowed_panels(ctx):
        if (await _vc(ctx["bot_id"], "vc_enabled", p.panel_key)) == "1":
            live.append(p)
    head = _rs_head(ctx, msg, err) + await _rs_panel_discounts(ctx)
    if not live:
        return HTMLResponse(_shell("فروش آنلاین", head + _msg(
            "فروشِ آنلاین (حجم/روزِ دلخواه) روی پنل‌هایِ تو فعال نیست. "
            "با مدیرِ ربات تماس بگیر."), _rs_nav("custom")))

    forms = ""
    for p in live:
        cfg = await _vc_all_for_user(ctx["bot_id"], ctx["telegram_id"], p.panel_key,
                                     is_dedicated_panel=True)
        pg, pd = int(cfg["vc_price_gb"]), int(cfg["vc_price_day"])
        forms += (
            '<div class="card" style="margin-bottom:14px;text-align:right">'
            f'<h3 style="margin:0 0 10px">{_html.escape(await _panel_label(p))}</h3>'
            '<p style="color:var(--tx2);font-size:12.5px;margin:0 0 12px">'
            f'هر گیگ {_fa(pg)} تومان · هر روز {_fa(pd)} تومان<br>'
            f'حجم {cfg["vc_min_gb"]} تا {cfg["vc_max_gb"]} گیگ · '
            f'مدت {cfg["vc_min_days"]} تا {cfg["vc_max_days"]} روز</p>'
            '<form method="post" action="/panel/r/custom" '
            'style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f'<input type="hidden" name="panel_key" value="{_html.escape(p.panel_key)}">'
            f'<label>حجم (گیگ) <input name="gb" value="{cfg["vc_min_gb"]}" '
            'style="width:80px"></label>'
            f'<label>مدت (روز) <input name="days" value="{cfg["vc_min_days"]}" '
            'style="width:80px"></label>'
            '<label>تعداد <input name="count" value="1" style="width:64px"></label>'
            '<button>ساخت</button></form></div>')
    return HTMLResponse(_shell("فروش آنلاین", head + forms, _rs_nav("custom")))


@web_panel_router.post("/r/custom")
@_rs_guard
async def rs_custom_create(request: Request, panel_key: str = Form(...),
                           gb: str = Form("0"), days: str = Form("0"),
                           count: str = Form("1")):
    from models import WalletTxn
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _vc_gb_cost, _vc
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    bot_id, tg_id = ctx["bot_id"], ctx["telegram_id"]

    if not ctx["apply_all"] and panel_key not in ctx["panels"]:
        return _rs_back("custom", err="اجازهٔ ساخت از این پنل را نداری.")
    if (await _vc(bot_id, "vc_enabled", panel_key)) != "1":
        return _rs_back("custom", err="فروشِ آنلاین روی این پنل فعال نیست.")
    try:
        gb_i, days_i = int(str(gb).strip()), int(str(days).strip())
        n = max(1, min(20, int(str(count).strip())))
    except ValueError:
        return _rs_back("custom", err="حجم، مدت یا تعداد نامعتبر است.")

    cfg = await _vc_all_for_user(bot_id, tg_id, panel_key, days=days_i, gb=gb_i,
                                 is_dedicated_panel=True)
    lo_g, hi_g = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
    lo_d, hi_d = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
    if not (lo_g <= gb_i <= hi_g):
        return _rs_back("custom", err=f"حجم باید بین {lo_g} تا {hi_g} گیگ باشد.")
    if not (lo_d <= days_i <= hi_d):
        return _rs_back("custom", err=f"مدت باید بین {lo_d} تا {hi_d} روز باشد.")

    # تخفیفِ نمایندگی از قبل داخلِ _vc_all_for_user اعمال شده، پس اینجا
    # دوباره اعمالش نمی‌کنیم — وگرنه تخفیف دوبار خورده می‌شد.
    unit = _vc_gb_cost(cfg, gb_i) + days_i * int(cfg["vc_price_day"])
    if unit <= 0:
        return _rs_back("custom", err="قیمت‌گذاریِ این پنل نامعتبر است.")
    total = unit * n

    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id).with_for_update())
        if not u:
            return _rs_back("custom", err="حسابِ کاربریِ تو در این ربات پیدا نشد.")
        if int(u.balance or 0) < total:
            return _rs_back("custom",
                            err=f"موجودیِ کیف پولت کافی نیست. لازم: {_fa(total)} تومان.")
        u.balance = int(u.balance or 0) - total
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-total, kind="purchase",
                        note=f"فروش آنلاین از پنلِ نمایندگی ({_plan_title_fa(gb_i, days_i)} × {n})"))
        await s.commit()
        uid, uname = u.id, u.username

    async def _refund(amount: int, why: str):
        async with Session() as s2:
            u2 = await s2.scalar(select(BotUser).where(
                BotUser.id == uid).with_for_update())
            if u2:
                u2.balance = int(u2.balance or 0) + amount
                s2.add(WalletTxn(bot_id=bot_id, user_id=uid, amount=amount,
                                 kind="charge", note=f"بازگشتِ وجه — {why}"))
                await s2.commit()

    panel = await _rs_panel_row(bot_id, panel_key)
    if not panel or not panel.enabled:
        await _refund(total, "پنل در دسترس نبود")
        return _rs_back("custom", err="پنل در دسترس نیست. مبلغ به کیف پولت برگشت.")

    made = await _rs_make_accounts(bot_id, tg_id, uname, panel, panel_key,
                                   gb_i, days_i, n, _plan_title_fa(gb_i, days_i))
    if made < n:
        await _refund(unit * (n - made), f"{n - made} کانفیگ ساخته نشد")
    if made == 0:
        return _rs_back("custom", err="ساخت ناموفق بود. مبلغ کامل برگشت.")

    await _rs_notify_owner(bot_id, uid, "پنلِ وبِ نماینده (فروش آنلاین)",
                           f"{_plan_title_fa(gb_i, days_i)} × {made} عدد",
                           float(unit * made), made)
    extra = "" if made == n else f" ({n - made} مورد ناموفق بود و پولش برگشت)"
    return _rs_back("custom",
                    msg=f"✅ {made} کانفیگ ساخته شد{extra}. در «کانفیگ‌های من» ببینشان.")


# ─────────────── تستِ رایگانِ نماینده ───────────────

_RS_TRIAL_TITLE = "🎁 تست نمایندگی"


async def _rs_trial_cfg(bot_id: int, panel_key: str = "") -> tuple[float, int, int]:
    """(گیگ، روز، ساعت) تستِ همان پنل.

    === PATCH_RESELLER_TRIAL_PER_PANEL_V1 ===
    باگِ گزارش‌شده: تستِ پنلِ «اکو» ۱۰۰ مگ / ۱ روز بود ولی نماینده ۲ گیگ /
    ۳ روز می‌گرفت. علتش این بود که اینجا فقط تنظیماتِ *سراسریِ* ربات خوانده
    می‌شد، در حالی که مالکِ ربات تنظیماتِ تست را رویِ **دکمهٔ تستِ هر پنل**
    می‌گذارد (هر دکمه config خودش را دارد با trial_panel_key). حالا اول
    دنبالِ دکمهٔ تستِ همین پنل می‌گردیم و فقط اگر پیدا نشد، به تنظیماتِ
    سراسری برمی‌گردیم.
    """
    from tenant import get_setting

    def _num(v, default, cast=float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    if panel_key:
        try:
            import json as _json
            from models import MainButton
            async with Session() as s:
                btns = (await s.scalars(select(MainButton).where(
                    MainButton.bot_id == bot_id))).all()
            for b in btns:
                if not b.config:
                    continue
                try:
                    cfg = _json.loads(b.config) or {}
                except Exception:
                    continue
                # PATCH_TRIAL_STALE_PANEL_KEYS_V1 — انتخابِ چندپنلی (trial_panel_keys) هم شناخته شود
                if cfg.get("trial_panel_key") != panel_key and \
                        panel_key not in [k for k in (cfg.get("trial_panel_keys") or "").split(",") if k]:
                    continue
                if not any(k in cfg for k in ("trial_gb", "trial_days", "trial_hours")):
                    continue
                return (_num(cfg.get("trial_gb"), 2.0),
                        _num(cfg.get("trial_days"), 3, int),
                        _num(cfg.get("trial_hours"), 0, int))
        except Exception:
            logger.exception("خواندنِ تنظیماتِ تستِ پنل ناموفق بود؛ تنظیماتِ سراسری "
                             "استفاده می‌شود (bot_id=%s panel=%s)", bot_id, panel_key)

    return (_num(await get_setting(bot_id, "trial_gb", "2"), 2.0),
            _num(await get_setting(bot_id, "trial_days", "3"), 3, int),
            _num(await get_setting(bot_id, "trial_hours", "0"), 0, int))


def _trial_label(gb: float, days: int, hours: int) -> str:
    """برچسبِ خوانا — زیرِ ۱ گیگ با مگابایت، مثلِ خودِ ربات."""
    vol = (f"{int(round(gb * 1024))} مگابایت" if gb < 1
           else f"{int(gb) if gb == int(gb) else gb} گیگ")
    dur = f"{hours} ساعت" if hours else f"{days} روز"
    return f"{vol} / {dur}"


async def _rs_trial_used_today(bot_id: int, tg_id: int) -> int:
    """تعدادِ تستی که این نماینده امروز گرفته.

    === PATCH_RESELLER_TRIAL_LOG_V1 ===
    قبلاً از رویِ خودِ کانفیگ‌هایِ موجود شمرده می‌شد و نماینده کافی بود
    تستش را از «سرویس‌های من» حذف کند تا سهمیه‌اش برگردد. حالا مبنا یک
    دفترِ جداست که با حذفِ کانفیگ پاک نمی‌شود.
    """
    from models import ResellerTrialLog
    start = _dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with Session() as s:
        return await s.scalar(
            select(func.count()).select_from(ResellerTrialLog).where(
                ResellerTrialLog.bot_id == bot_id,
                ResellerTrialLog.telegram_id == tg_id,
                ResellerTrialLog.created_at >= start)) or 0


async def _rs_trial_log(bot_id: int, tg_id: int, panel_key: str, n: int) -> None:
    """ثبتِ n تستِ ساخته‌شده. فقط بعد از ساختِ موفق صدا زده می‌شود، پس
    تستی که روی پنل شکست بخورد سهمیه را نمی‌سوزاند."""
    from models import ResellerTrialLog
    try:
        async with Session() as s:
            for _ in range(max(0, n)):
                s.add(ResellerTrialLog(bot_id=bot_id, telegram_id=tg_id,
                                       panel_key=panel_key))
            await s.commit()
    except Exception:
        logger.exception("ثبتِ سهمیهٔ تستِ نماینده ناموفق بود (bot_id=%s)", bot_id)


@web_panel_router.get("/r/trial")
@_rs_guard
async def rs_trial_form(request: Request, msg: str = "", err: str = ""):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    limit = ctx["trial_limit"]
    if limit <= 0:
        return HTMLResponse(_shell("تست رایگان", _msg(
            "سهمیهٔ تستِ رایگان برای تو فعال نشده. از مدیرِ ربات بخواه "
            "سهمیهٔ روزانه برایت تعیین کند."), _rs_nav("trial")))
    used = await _rs_trial_used_today(ctx["bot_id"], ctx["telegram_id"])
    left = max(0, limit - used)
    head = ('<div class="cards">'
            f'<div class="card"><div class="n">{_fa(left)}</div>'
            '<div class="l">تستِ باقی‌ماندهٔ امروز</div></div>'
            f'<div class="card"><div class="n">{_fa(limit)}</div>'
            '<div class="l">سهمیهٔ روزانه</div></div></div>')
    if msg:
        head += _msg(_html.escape(msg))
    if err:
        head += f'<div class="msg" style="color:#e06c6c">{_html.escape(err)}</div>'

    panels = await _rs_allowed_panels(ctx)
    if not panels:
        return HTMLResponse(_shell("تست رایگان", head + _msg(
            "هیچ پنلی برای تو فعال نیست."), _rs_nav("trial")))
    # === PATCH_RESELLER_TRIAL_PER_PANEL_V1 ===
    # هر پنل تنظیماتِ تستِ خودش را دارد، پس مشخصاتِ هر کدام کنارِ نامش
    # نوشته می‌شود؛ قبلاً یک عددِ واحد بالایِ صفحه بود و گمراه‌کننده.
    opts = []
    for p in panels:
        g, d, h = await _rs_trial_cfg(ctx["bot_id"], p.panel_key)
        opts.append(f'<option value="{_html.escape(p.panel_key)}">'
                    f'{_html.escape(await _panel_label(p))} — '
                    f'{_html.escape(_trial_label(g, d, h))}</option>')
    form = ('<form method="post" action="/panel/r/trial" '
            'style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f'<label>پنل <select name="panel_key">{"".join(opts)}</select></label>'
            '<label>تعداد <input name="count" value="1" style="width:64px"></label>'
            '<button>ساختِ تست</button></form>') if left > 0 else _msg(
        "سهمیهٔ امروزت تمام شده. فردا دوباره سر بزن.")
    body = head + (
        '<div class="card" style="text-align:right">'
        '<h3 style="margin:0 0 10px">تستِ رایگان</h3>'
        '<p style="color:var(--tx2);font-size:12.5px;margin:0 0 12px">'
        'حجم و مدتِ تست را مدیرِ ربات برایِ هر پنل جدا تعیین کرده و کنارِ '
        'نامِ همان پنل نوشته شده. رایگان است و فقط از سهمیهٔ روزانه‌ات کم '
        'می‌شود؛ سهمیه هر شبانه‌روز از نو پر می‌شود.</p>'
        + form + '</div>')
    return HTMLResponse(_shell("تست رایگان", body, _rs_nav("trial")))


@web_panel_router.post("/r/trial")
@_rs_guard
async def rs_trial_create(request: Request, panel_key: str = Form(...),
                          count: str = Form("1")):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    bot_id, tg_id = ctx["bot_id"], ctx["telegram_id"]

    if ctx["trial_limit"] <= 0:
        return _rs_back("trial", err="سهمیهٔ تست برای تو فعال نیست.")
    if not ctx["apply_all"] and panel_key not in ctx["panels"]:
        return _rs_back("trial", err="اجازهٔ ساخت از این پنل را نداری.")
    try:
        n = max(1, int(str(count).strip()))
    except ValueError:
        n = 1

    left = max(0, ctx["trial_limit"] - await _rs_trial_used_today(bot_id, tg_id))
    if left <= 0:
        return _rs_back("trial", err="سهمیهٔ امروزت تمام شده.")
    n = min(n, left)

    t_gb, t_days, t_hours = await _rs_trial_cfg(bot_id, panel_key)
    panel = await _rs_panel_row(bot_id, panel_key)
    if not panel or not panel.enabled:
        return _rs_back("trial", err="این پنل در دسترس نیست.")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        uname, uid = (u.username if u else None), (u.id if u else 0)

    made = await _rs_make_accounts(bot_id, tg_id, uname, panel, panel_key,
                                   t_gb, t_days, n, _RS_TRIAL_TITLE, hours=t_hours)
    if made == 0:
        return _rs_back("trial", err="ساختِ تست ناموفق بود. سهمیه‌ات مصرف نشد.")
    # === PATCH_RESELLER_TRIAL_LOG_V1 === سهمیه بعد از ساختِ موفق ثبت می‌شود
    await _rs_trial_log(bot_id, tg_id, panel_key, made)
    if uid:
        await _rs_notify_owner(bot_id, uid, "پنلِ وبِ نماینده (تست رایگان)",
                               f"{_trial_label(t_gb, t_days, t_hours)} × {made} عدد",
                               0.0, made)
    return _rs_back("trial", msg=f"✅ {made} تست ساخته شد. در «کانفیگ‌های من» ببینشان.")


@web_panel_router.get("/r/list")
async def rs_list(request: Request, page: int = 0, msg: str = "", err: str = ""):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    page = max(0, int(page or 0))
    async with Session() as s:
        rows = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == ctx["bot_id"],
            PanelAccount.user_telegram_id == ctx["telegram_id"])
            .order_by(PanelAccount.id.desc())
            .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).all()
        data = [(a.id, a.svc_code, a.plan_title, a.volume_gb, a.days, a.status,
                 a.expires_at, a.sub_link) for a in rows]
    # === PATCH_RESELLER_WEB_RENEW_DELETE_V1 ===
    head = ""
    if msg:
        head += _msg(_html.escape(msg))
    if err:
        head += f'<div class="msg" style="color:#e06c6c">{_html.escape(err)}</div>'
    trs = "".join(
        f"<tr><td><code>{_html.escape(str(code or '—'))}</code></td>"
        f"<td>{_html.escape(title or '—')}</td>"
        f"<td>{'نامحدود' if not gb else str(gb) + ' گیگ'}</td>"
        f"<td>{'بی‌انقضا' if not d else str(d) + ' روز'}</td>"
        f"<td>{_html.escape(st or '—')}</td>"
        f"<td>{exp.strftime('%Y-%m-%d') if exp else '—'}</td>"
        f"<td><code style=\"font-size:11px\">{_html.escape(link or '—')}</code></td>"
        f'<td style="white-space:nowrap">'
        f'<a href="/panel/r/renew/{aid}" style="color:#6fb1ff;text-decoration:none">تمدید</a>'
        f' &nbsp;<a href="/panel/r/delete/{aid}" style="color:#e06c6c;text-decoration:none">حذف</a>'
        f"</td></tr>"
        for aid, code, title, gb, d, st, exp, link in data)
    body = head + ("<table><tr><th>کد</th><th>پلن</th><th>حجم</th><th>مدت</th>"
            "<th>وضعیت</th><th>انقضا</th><th>لینکِ ساب</th><th></th></tr>"
            + (trs or '<tr><td colspan="8">هنوز کانفیگی نساخته‌ای.</td></tr>') + "</table>")
    body += '<div class="pg">'
    if page > 0:
        body += f'<a href="/panel/r/list?page={page-1}">→ قبلی</a>'
    if len(data) == _PAGE_SIZE:
        body += f'<a href="/panel/r/list?page={page+1}">بعدی ←</a>'
    body += "</div>"
    return HTMLResponse(_shell("کانفیگ‌های من", body, _rs_nav("list")))


# ─────────── PATCH_RESELLER_WEB_RENEW_DELETE_V1 — تمدید و حذف ───────────
#
# هر دو فقط رویِ کانفیگ‌هایِ خودِ همان نماینده کار می‌کنند: مالکیت با
# (bot_id, user_telegram_id) دوباره از دیتابیس چک می‌شود، نه از فرم — وگرنه
# با دست‌کاریِ شناسه می‌شد سرویسِ نمایندهٔ دیگری را تمدید یا حذف کرد.

async def _rs_owned(bot_id: int, tg_id: int, acc_id: int):
    """کانفیگ اگر واقعاً مالِ این نماینده باشد، وگرنه None."""
    async with Session() as s:
        a = await s.get(PanelAccount, acc_id)
        if not a or a.bot_id != bot_id or a.user_telegram_id != tg_id:
            return None
        return a


async def _rs_renew_plans(bot_id: int, acc, panels: list[str], apply_all: bool):
    """پلن‌هایِ مجازِ همان پنلِ این کانفیگ — تمدید فقط با پلن‌های خودِ آن پنل
    معنی دارد، چون حجم/روز روی همان اکانت اضافه می‌شود."""
    from models import VpnPlan
    if not apply_all and acc.panel_key not in panels:
        return []
    async with Session() as s:
        rows = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.active == True,
            VpnPlan.panel_key == acc.panel_key).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
        return [(p.id, p.title, int(p.price or 0), p.volume_gb, p.days) for p in rows]


@web_panel_router.get("/r/renew/{acc_id}")
async def rs_renew_form(request: Request, acc_id: int):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    acc = await _rs_owned(ctx["bot_id"], ctx["telegram_id"], acc_id)
    if not acc:
        return HTMLResponse(_shell("تمدید", _msg("این سرویس پیدا نشد."),
                                   _rs_nav("list")), status_code=404)
    plans = await _rs_renew_plans(ctx["bot_id"], acc, ctx["panels"], ctx["apply_all"])
    head = (f'<div class="cards">'
            f'<div class="card"><div class="n">{_html.escape(str(acc.svc_code or "—"))}</div>'
            f'<div class="l">سرویسِ انتخابی</div></div>'
            f'<div class="card"><div class="n">{_fa(ctx["balance"])}</div>'
            f'<div class="l">موجودیِ کیف پول</div></div></div>')
    if not plans:
        return HTMLResponse(_shell("تمدید", head + _msg(
            "برای پنلِ این سرویس پلنِ فعالی نداری."), _rs_nav("list")))
    trs = ""
    for pid, title, price, gb, days in plans:
        final = _rs_price(price, ctx["percent"])
        trs += ("<tr>"
                f"<td>{_html.escape(title or '—')}</td>"
                f"<td>+{'۰' if not gb else str(gb)} گیگ</td>"
                f"<td>+{'۰' if not days else str(days)} روز</td>"
                f"<td><b>{_fa(final)}</b></td>"
                '<td><form method="post" action="/panel/r/renew" style="margin:0">'
                f'<input type="hidden" name="acc_id" value="{acc_id}">'
                f'<input type="hidden" name="plan_id" value="{pid}">'
                '<button>تمدید کن</button></form></td></tr>')
    body = head + ("<table><tr><th>پلن</th><th>حجم</th><th>مدت</th>"
                   "<th>قیمتِ تو</th><th></th></tr>" + trs + "</table>"
                   '<div class="pg"><a href="/panel/r/list">→ بازگشت</a></div>')
    return HTMLResponse(_shell("تمدید سرویس", body, _rs_nav("list")))


@web_panel_router.post("/r/renew")
async def rs_renew(request: Request, acc_id: int = Form(...), plan_id: int = Form(...)):
    from models import VpnPlan, VpnPanel, WalletTxn
    from urllib.parse import quote
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    bot_id, tg_id = ctx["bot_id"], ctx["telegram_id"]

    def back(msg="", err=""):
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(f"/panel/r/list{q}", status_code=303)

    acc = await _rs_owned(bot_id, tg_id, acc_id)
    if not acc:
        return back(err="این سرویس پیدا نشد.")

    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if (not plan or plan.bot_id != bot_id or not plan.active
                or plan.panel_key != acc.panel_key):
            return back(err="این پلن برای تمدیدِ این سرویس معتبر نیست.")
        if not ctx["apply_all"] and plan.panel_key not in ctx["panels"]:
            return back(err="اجازهٔ کار با این پنل را نداری.")
        price = _rs_price(int(plan.price or 0), ctx["percent"])
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id).with_for_update())
        if not u or int(u.balance or 0) < price:
            return back(err=f"موجودی کافی نیست. لازم: {_fa(price)} تومان.")
        u.balance = int(u.balance or 0) - price
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price, kind="purchase",
                        note=f"تمدید از پنلِ نمایندگی: {plan.title}"))
        await s.commit()
        uid = u.id
        add_gb, add_days, ptitle = plan.volume_gb, plan.days, plan.title
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == acc.panel_key))
        email = acc.email

    async def _refund(why: str):
        async with Session() as s2:
            u2 = await s2.scalar(select(BotUser).where(BotUser.id == uid).with_for_update())
            if u2:
                u2.balance = int(u2.balance or 0) + price
                s2.add(WalletTxn(bot_id=bot_id, user_id=uid, amount=price,
                                 kind="charge", note=f"بازگشتِ وجه — {why}"))
                await s2.commit()

    if not panel or not panel.enabled:
        await _refund("پنل در دسترس نبود")
        return back(err="پنل در دسترس نیست. مبلغ برگشت.")

    from tenant.bots.vpn.vpn_user import _client_from_panel
    client = _client_from_panel(panel)
    try:
        await client.login()
        if add_days:
            await client.renew(email, add_days)
        if add_gb:
            await client.add_traffic(email, add_gb)
    except Exception as e:
        logger.warning("پنلِ نماینده: تمدید ناموفق (bot=%s email=%s): %s", bot_id, email, e)
        await _refund("خطای پنل هنگام تمدید")
        try:
            await client.close()
        except Exception:
            logger.exception("پنلِ نماینده: بستنِ کلاینت ناموفق")
        return back(err="تمدید ناموفق بود. مبلغ کامل به کیف پولت برگشت.")
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("پنلِ نماینده: بستنِ کلاینت ناموفق")

    # همان به‌روزرسانیِ محلیِ مسیرِ تمدیدِ تلگرام
    async with Session() as s3:
        a2 = await s3.get(PanelAccount, acc_id)
        if a2:
            a2.expiry_notice = None
            a2.status = "active"
            if add_days:
                base = (a2.expires_at if a2.expires_at and a2.expires_at > _dt.datetime.utcnow()
                        else _dt.datetime.utcnow())
                a2.expires_at = base + _dt.timedelta(days=add_days)
                a2.days = (a2.days or 0) + add_days
            if add_gb:
                a2.volume_gb = (a2.volume_gb or 0) + add_gb
                a2.traffic_out_at = None   # PATCH_TRAFFIC_RENEW_GUARD_V1
            await s3.commit()
    return back(msg=f"✅ سرویس تمدید شد ({ptitle}).")


@web_panel_router.get("/r/delete/{acc_id}")
async def rs_delete_confirm(request: Request, acc_id: int):
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    acc = await _rs_owned(ctx["bot_id"], ctx["telegram_id"], acc_id)
    if not acc:
        return HTMLResponse(_shell("حذف", _msg("این سرویس پیدا نشد."),
                                   _rs_nav("list")), status_code=404)
    body = _msg(
        f"می‌خواهی سرویسِ <code>{_html.escape(str(acc.svc_code or '—'))}</code> "
        f"({_html.escape(acc.plan_title or '—')}) را حذف کنی؟<br><br>"
        "⚠️ کانفیگ از روی پنل هم پاک می‌شود و مشتریِ تو فوراً قطع خواهد شد. "
        "<b>این کار برگشت‌پذیر نیست و پولی هم برنمی‌گردد.</b><br><br>"
        '<form method="post" action="/panel/r/delete" style="display:flex;gap:8px;justify-content:center">'
        f'<input type="hidden" name="acc_id" value="{acc_id}">'
        '<button class="d">بله، حذف کن</button></form>'
        '<a href="/panel/r/list" style="color:#9db2d3">انصراف</a>')
    return HTMLResponse(_shell("حذف سرویس", body, _rs_nav("list")))


@web_panel_router.post("/r/delete")
async def rs_delete(request: Request, acc_id: int = Form(...)):
    from models import VpnPanel
    from urllib.parse import quote
    ctx = await _rs_resolve(request)
    if not ctx:
        return HTMLResponse(_rs_login_page(), status_code=401)
    acc = await _rs_owned(ctx["bot_id"], ctx["telegram_id"], acc_id)
    if not acc:
        return RedirectResponse(f"/panel/r/list?err={quote('این سرویس پیدا نشد.')}",
                                status_code=303)
    email, pkey, code = acc.email, acc.panel_key, acc.svc_code
    async with Session() as s:
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == ctx["bot_id"], VpnPanel.panel_key == pkey))
    if panel:
        from tenant.bots.vpn.vpn_user import _client_from_panel
        client = _client_from_panel(panel)
        try:
            await client.login()
            await client.delete_account(email)
        except Exception:
            logger.exception("پنلِ نماینده: حذفِ %s از پنل ناموفق بود", email)
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("پنلِ نماینده: بستنِ کلاینت ناموفق")
    # ردیفِ محلی در هر حال حذف می‌شود؛ اگر پنل جواب نداده باشد، لاگ بالا
    # هست و ادمین می‌تواند دستی پاک کند — ولی نماینده نباید با یک سرویسِ
    # «حذف‌شده ولی هنوز در لیست» گیر کند.
    async with Session() as s2:
        a2 = await s2.get(PanelAccount, acc_id)
        if a2:
            await s2.delete(a2)
            await s2.commit()
    return RedirectResponse(
        f"/panel/r/list?msg={quote(f'سرویس {code or acc_id} حذف شد.')}", status_code=303)
