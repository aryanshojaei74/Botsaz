"""
platform_funstat.py — بخشِ «🔎 Fun Stat» در رباتِ اصلیِ بات‌ساز (PATCH_FUNSTAT_V1).

سرویسِ funstat.info (API مستند: /swagger) آیدی عددیِ تلگرام را از روی @یوزرنیم (و برعکس) پیدا می‌کند و
تاریخچهٔ نام/یوزرنیم و گروه‌هایِ عمومیِ یک حساب را از دادهٔ عمومیِ گروه‌ها می‌دهد.

مرحلهٔ فعلی = «تست»، نه فروش:
  • هر کاربر می‌تواند «درخواستِ دسترسی» بدهد؛ ادمین (فقط ادمینِ اصلی) تأیید می‌کند و مدت تعیین می‌کند
    (۱/۳/۷ روز، دائمی، یا هر عددی) — بعدِ پایانِ مدت دسترسی خودکار بسته می‌شود.
  • وقتی خواستی عمومی/پولی شود: «حالتِ عمومی» (با سقفِ روزانه) آماده است؛ قیمت‌گذاری بعداً روی همین ساختار
    سوار می‌شود (لاگِ هزینهٔ هر جست‌وجو ثبت می‌شود).

عمداً *پیاده نشده* (حریمِ خصوصیِ بالا، ابزارِ تعقیب): خواندنِ متنِ پیام‌هایِ یک کاربر، جست‌وجویِ متنِ پیام‌ها،
گراف‌ِ هدیه/گروه‌هایِ مشترک، لیستِ اعضایِ گروه‌ها. فقط هویتِ عمومی + تاریخچهٔ نام/یوزرنیم + آمارِ کلی + گروه‌هایِ
عمومی (دارایِ یوزرنیم). مدیر یک «فهرستِ مسدود» دارد که آن آیدی‌ها هرگز جست‌وجو نمی‌شوند (ادمین‌ها پیش‌فرض داخلش‌اند).

توکن/آدرسِ سرویس فقط از پنلِ ادمین (نه گفتگو/کد) تنظیم می‌شود و پیامِ حاویِ توکن بعد از دریافت پاک می‌شود.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from models import FunstatAccess, FunstatLog, FunstatRequest, PlatformSetting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

funstat_router = Router(name="funstat")

DEFAULT_BASE = "https://funstat.info"
_TIMEOUT = 20


# ───────────────────────── تنظیمات (PlatformSetting) ─────────────────────────

async def _get(key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
    return r.value if (r and r.value is not None) else default


async def _set(key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if r:
            r.value = value
        else:
            s.add(PlatformSetting(key=key, value=value))
        await s.commit()


async def _cfg() -> dict:
    return {
        "token": _clean_token(await _get("funstat_token")),
        "base": ((await _get("funstat_base", DEFAULT_BASE)).strip() or DEFAULT_BASE).rstrip("/"),
        "mode": (await _get("funstat_mode", "test")) or "test",          # test | public
        "enabled": (await _get("funstat_enabled", "1")) != "0",
        "limit": _int(await _get("funstat_daily_limit", "20"), 20),
        "balance": await _get("funstat_last_balance", ""),
    }


def _clean_token(t: str) -> str:
    """توکن را از «Bearer »، کوتیشن و فاصله پاک می‌کند (خطای رایجِ کپی)."""
    t = (t or "").strip().strip("\"'`").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _int(v, d=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return d


def _is_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and int(uid) == int(PLATFORM_ADMIN_ID)


async def _blocked_ids() -> set[int]:
    try:
        raw = json.loads(await _get("funstat_block", "[]"))
        out = {int(x) for x in raw}
    except Exception:
        out = set()
    if PLATFORM_ADMIN_ID:
        out.add(int(PLATFORM_ADMIN_ID))
    return out


# ───────────────────────── کلاینتِ API ─────────────────────────

class FunstatError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


async def _api(path: str, params: list[tuple[str, object]] | None = None) -> dict:
    """GET با Bearer. خروجی: JSON کامل (success/tech/data). خطا → FunstatError."""
    import aiohttp
    c = await _cfg()
    if not c["token"]:
        raise FunstatError("no_token", "توکنِ سرویس هنوز تنظیم نشده.")
    url = f"{c['base']}{path}"
    headers = {"Authorization": f"Bearer {c['token']}", "Accept": "application/json"}
    raw = ""
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t, headers=headers) as s:
            async with s.get(url, params=params or []) as r:
                status = r.status
                raw = await r.text(errors="replace")
    except asyncio.TimeoutError:
        raise FunstatError("timeout", "سرویس دیر جواب داد.")
    except Exception as e:  # noqa: BLE001
        raise FunstatError("network", f"اتصال به سرویس ناموفق بود: {type(e).__name__}: {str(e)[:100]}")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    snippet = re.sub(r"\s+", " ", raw or "")[:140]
    if status in (401, 403):
        raise FunstatError("auth", f"توکن نامعتبر است یا موجودی/دسترسیِ اکانتِ سرویس تمام شده (HTTP {status}) {snippet}")
    if status == 429:
        raise FunstatError("rate", "سرویس موقتاً محدود کرده؛ چند لحظه بعد دوباره.")
    if status >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("title") or data.get("message") or "")
        raise FunstatError("http", f"خطای سرویس (HTTP {status}) {(detail or snippet)[:140]}")
    if not isinstance(data, dict) or (not data and raw.strip()[:1] not in ("{", "[")):
        raise FunstatError("http", f"پاسخِ نامعتبر از سرویس (HTTP {status}): {snippet[:100]}")
    if isinstance(data, dict) and data.get("success") is False:
        raise FunstatError("api", "سرویس درخواست را نپذیرفت. " + snippet[:100])
    tech = (data or {}).get("tech") if isinstance(data, dict) else None
    if isinstance(tech, dict) and tech.get("current_ballance") is not None:
        try:
            await _set("funstat_last_balance", f"{float(tech['current_ballance']):.2f}")
        except Exception:
            logger.debug("ثبتِ موجودیِ سرویس ممکن نشد (بی‌اهمیت)")
    return data if isinstance(data, dict) else {"data": data}


def _cost_of(data: dict) -> float:
    try:
        return float(((data or {}).get("tech") or {}).get("request_cost") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _log(uid: int, action: str, query: str, cost: float, ok: bool) -> None:
    try:
        async with Session() as s:
            s.add(FunstatLog(telegram_id=uid, action=(action or "")[:30], query=(query or "")[:200],
                             cost=cost, ok=ok))
            await s.commit()
    except Exception:
        logger.exception("ثبتِ لاگِ Fun Stat ناموفق بود")


async def _used_today(uid: int) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with Session() as s:
        n = await s.scalar(select(func.count(FunstatLog.id)).where(
            FunstatLog.telegram_id == uid, FunstatLog.created_at >= start, FunstatLog.ok == True))  # noqa: E712
    return int(n or 0)


# ───────────────────────── دسترسی ─────────────────────────

def _fa_date(dt) -> str:
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=dt).strftime("%Y/%m/%d")
    except Exception:
        return dt.strftime("%Y-%m-%d")


async def _access(uid: int) -> tuple[bool, str]:
    """(مجاز؟, دلیل/توضیحِ کوتاه)."""
    c = await _cfg()
    if not c["enabled"] and not _is_admin(uid):
        return False, "off"
    if _is_admin(uid):
        return True, "admin"
    async with Session() as s:
        a = await s.scalar(select(FunstatAccess).where(FunstatAccess.telegram_id == uid))
    if a and (a.expires_at is None or a.expires_at > datetime.utcnow()):
        return True, "granted"
    if c["mode"] == "public":
        return True, "public"
    return False, ("expired" if a else "none")


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def _btn(text: str, cb: str, style=None) -> InlineKeyboardButton:
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


class FunstatFSM(StatesGroup):
    query = State()
    token = State()
    base = State()
    limit = State()
    grant = State()
    block = State()


# ───────────────────────── سمتِ کاربر ─────────────────────────

INTRO = (
    "🔎 <b>Fun Stat</b>\n\n"
    "آیدیِ عددیِ یک حساب را از روی @یوزرنیم پیدا کن (یا برعکس) و تاریخچهٔ نام و یوزرنیم و "
    "گروه‌هایِ عمومیِ آن را ببین.\n\n"
    "فقط یک <b>@یوزرنیم</b> یا <b>آیدیِ عددی</b> بفرست."
)


async def show_funstat(msg: Message, state: FSMContext | None = None) -> None:
    uid = msg.from_user.id
    ok, why = await _access(uid)
    if ok:
        if state is not None:
            await state.set_state(FunstatFSM.query)
        extra = ""
        if why == "granted":
            async with Session() as s:
                a = await s.scalar(select(FunstatAccess).where(FunstatAccess.telegram_id == uid))
            if a and a.expires_at:
                extra = f"\n\n⏳ دسترسیِ تستِ تو تا <b>{_fa_date(a.expires_at)}</b> فعال است."
        await msg.answer(INTRO + extra, reply_markup=_kb([_btn("❌ خروج", "fst:exit", ButtonStyle.DANGER)]))
        return
    if why == "off":
        await msg.answer("🔎 این بخش فعلاً غیرفعال است.")
        return
    async with Session() as s:
        pend = await s.scalar(select(FunstatRequest).where(
            FunstatRequest.telegram_id == uid, FunstatRequest.status == "pending"))
    head = ("⏰ دسترسیِ تستِ قبلیِ تو تمام شده.\n\n" if why == "expired" else "")
    text = (head + "🔎 <b>Fun Stat</b>\n\n"
            "این بخش الان در <b>مرحلهٔ تست</b> است و فقط برایِ کاربرانی که مدیر دسترسی بدهد فعال است.\n"
            "می‌توانی درخواستِ دسترسی بفرستی؛ بعد از تأییدِ مدیر خبرت می‌کنیم.")
    if pend:
        text += "\n\n✅ درخواستِ تو ثبت شده و در انتظارِ تأییدِ مدیر است."
        kb = None
    else:
        kb = _kb([_btn("📨 درخواستِ دسترسی", "fst:req", ButtonStyle.SUCCESS)])
    await msg.answer(text, reply_markup=kb)


@funstat_router.message(Command("funstat"))
async def cmd_funstat(msg: Message, state: FSMContext):
    await show_funstat(msg, state)


@funstat_router.callback_query(F.data == "fst:open")
async def fst_open(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """ورود از لیستِ «ساخت ربات»."""
    await cb.answer()
    await state.clear()
    if cb.message is None:
        return
    fake = cb.message.model_copy(update={"from_user": cb.from_user}).as_(bot)
    await show_funstat(fake, state)


@funstat_router.callback_query(F.data == "fst:exit")
async def fst_exit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("بسته شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("حذفِ دکمه ممکن نشد (بی‌اهمیت)")


@funstat_router.callback_query(F.data == "fst:req")
async def fst_request(cb: CallbackQuery, bot: Bot):
    u = cb.from_user
    async with Session() as s:
        pend = await s.scalar(select(FunstatRequest).where(
            FunstatRequest.telegram_id == u.id, FunstatRequest.status == "pending"))
        if not pend:
            s.add(FunstatRequest(telegram_id=u.id, username=u.username, full_name=(u.full_name or "")[:120]))
            await s.commit()
            fresh = True
        else:
            fresh = False
    await cb.answer("✅ درخواستت ثبت شد" if fresh else "درخواستت قبلاً ثبت شده", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("حذفِ دکمه ممکن نشد (بی‌اهمیت)")
    if fresh and PLATFORM_ADMIN_ID:
        uname = f"@{u.username}" if u.username else "—"
        try:
            await bot.send_message(
                PLATFORM_ADMIN_ID,
                f"🔎 <b>درخواستِ دسترسیِ Fun Stat</b>\n\n"
                f"👤 {html.escape(u.full_name or '—')} ({uname})\n🆔 <code>{u.id}</code>\n\nچند روز تست بدهم؟",
                reply_markup=_kb(
                    [_btn("۱ روز", f"fst:ok:{u.id}:1"), _btn("۳ روز", f"fst:ok:{u.id}:3"),
                     _btn("۷ روز", f"fst:ok:{u.id}:7")],
                    [_btn("♾ دائمی", f"fst:ok:{u.id}:0", ButtonStyle.SUCCESS),
                     _btn("❌ رد", f"fst:no:{u.id}", ButtonStyle.DANGER)]))
        except Exception:
            logger.exception("اطلاعِ درخواستِ Fun Stat به ادمین ناموفق بود")


# ───────── جست‌وجو ─────────

_USER_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def _fmt_user(u: dict) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "—"
    un = f"@{u['username']}" if u.get("username") else "—"
    flags = []
    if u.get("is_bot"):
        flags.append("🤖 ربات")
    flags.append("🟢 فعال" if u.get("is_active", True) else "⚪️ حذف/غیرفعال")
    if u.get("has_premium"):
        flags.append("⭐ پرمیوم")
    return (f"👤 <b>{html.escape(name)}</b>\n🏷 یوزرنیم: {html.escape(un)}\n"
            f"🆔 آیدی: <code>{u.get('id')}</code>\n" + " · ".join(flags))


def _result_kb(uid: int) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("📜 تاریخچهٔ نام", f"fst:q:names:{uid}"), _btn("🏷 تاریخچهٔ یوزرنیم", f"fst:q:unames:{uid}")],
        [_btn("📊 آمارِ کلی", f"fst:q:stats:{uid}"), _btn("👥 گروه‌هایِ عمومی", f"fst:q:groups:{uid}")],
        [_btn("⭐ اعتبار", f"fst:q:rep:{uid}")],
        [_btn("🔎 جست‌وجویِ تازه", "fst:again", ButtonStyle.PRIMARY)])


async def _gate(uid: int, cost_class: str = "q") -> str | None:
    """None = مجاز؛ وگرنه پیامِ رد."""
    ok, why = await _access(uid)
    if not ok:
        return "دسترسی نداری یا مدتش تمام شده."
    if not _is_admin(uid):
        c = await _cfg()
        if c["limit"] > 0 and await _used_today(uid) >= c["limit"]:
            return f"سقفِ جست‌وجویِ امروزت ({c['limit']}) پر شده؛ فردا دوباره."
    return None


async def _notify_admin_error(bot: Bot, e: FunstatError) -> None:
    if e.code not in ("auth", "no_token"):
        return
    last = _int(await _get("funstat_err_ts", "0"))
    if time.time() - last < 3600 or not PLATFORM_ADMIN_ID:
        return
    await _set("funstat_err_ts", str(int(time.time())))
    try:
        await bot.send_message(PLATFORM_ADMIN_ID,
                               f"⚠️ <b>Fun Stat</b>: {html.escape(e.message)}\nاز «🔎 Fun Stat» در پنلِ مدیریت بررسی کن.")
    except Exception:
        logger.debug("اطلاعِ خطا به ادمین ممکن نشد (بی‌اهمیت)")


@funstat_router.message(StateFilter(FunstatFSM.query), F.text)
async def fst_query(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    raw = (msg.text or "").strip()
    if raw.startswith("/"):
        await state.clear()
        return
    deny = await _gate(uid)
    if deny:
        await msg.answer("⛔️ " + deny)
        return
    q = raw.replace("https://t.me/", "").replace("t.me/", "").strip()
    digits = q.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    blocked = await _blocked_ids()
    try:
        if digits.lstrip("-").isdigit():
            tid = int(digits)
            if tid in blocked:
                await msg.answer("⛔️ جست‌وجویِ این آیدی ممکن نیست.")
                return
            wait = await msg.answer("⏳ در حالِ جست‌وجو…")
            data = await _api("/api/v1/users/basic_info_by_id", [("id", tid)])
            action = "by_id"
        else:
            m = _USER_RE.match(q)
            if not m:
                await msg.answer("فقط @یوزرنیم (۴ تا ۳۲ حرفِ انگلیسی/عدد/_) یا آیدیِ عددی بفرست.")
                return
            wait = await msg.answer("⏳ در حالِ جست‌وجو…")
            data = await _api("/api/v1/users/resolve_username", [("name", m.group(1))])
            action = "by_username"
    except FunstatError as e:
        await _log(uid, "lookup", q, 0, False)
        await msg.answer("⚠️ " + e.message)
        await _notify_admin_error(bot, e)
        return
    cost = _cost_of(data)
    await _log(uid, action, q, cost, True)
    try:
        await wait.delete()
    except Exception:
        logger.debug("حذفِ پیامِ انتظار ممکن نشد (بی‌اهمیت)")
    items = [x for x in (data.get("data") or []) if isinstance(x, dict)]
    if not items:
        await msg.answer("چیزی پیدا نشد. این حساب در پایگاهِ داده نیست یا یوزرنیم تغییر کرده.")
        return
    u = items[0]
    if int(u.get("id") or 0) in blocked:
        await msg.answer("⛔️ جست‌وجویِ این حساب ممکن نیست.")
        return
    await msg.answer(_fmt_user(u), reply_markup=_result_kb(int(u["id"])))


@funstat_router.callback_query(F.data == "fst:again")
async def fst_again(cb: CallbackQuery, state: FSMContext):
    ok, _ = await _access(cb.from_user.id)
    if not ok:
        await cb.answer("دسترسی نداری.", show_alert=True)
        return
    await state.set_state(FunstatFSM.query)
    await cb.message.answer("@یوزرنیم یا آیدیِ عددیِ بعدی را بفرست:")
    await cb.answer()


def _dt(s) -> str:
    try:
        return str(s)[:10]
    except Exception:
        return "—"


async def detail_text(kind: str, tid: int) -> tuple[str, dict]:
    """متنِ یک بخشِ جزئیاتِ یک حساب + پاسخِ خامِ API (برایِ هزینه). خطا → FunstatError / ValueError."""
    if kind == "names":
        d = await _api(f"/api/v1/users/{tid}/names")
        rows = [f"• {html.escape(str(x.get('name') or '—'))} — {_dt(x.get('date_time'))}"
                for x in (d.get("data") or [])[:40]]
        text = "📜 <b>تاریخچهٔ نام</b>\n\n" + ("\n".join(rows) if rows else "موردی ثبت نشده.")
    elif kind == "unames":
        d = await _api(f"/api/v1/users/{tid}/usernames")
        rows = [f"• @{html.escape(str(x.get('name') or '—'))} — {_dt(x.get('date_time'))}"
                for x in (d.get("data") or [])[:40]]
        text = "🏷 <b>تاریخچهٔ یوزرنیم</b>\n\n" + ("\n".join(rows) if rows else "موردی ثبت نشده.")
    elif kind == "stats":
        d = await _api(f"/api/v1/users/{tid}/stats_min")
        s = d.get("data") or {}
        text = ("📊 <b>آمارِ کلی</b>\n\n"
                f"💬 مجموعِ پیام‌ها: {s.get('total_msg_count', 0):,}\n"
                f"👥 گروه‌ها: {s.get('total_groups', 0):,} (فعال در {s.get('msg_in_groups_count', 0):,})\n"
                f"🛡 ادمین در: {s.get('adm_in_groups', 0)} گروه\n"
                f"🏷 یوزرنیم‌هایِ قبلی: {s.get('usernames_count', 0)} · 📜 نام‌هایِ قبلی: {s.get('names_count', 0)}\n"
                f"📅 اولین پیام: {_dt(s.get('first_msg_date'))} · آخرین: {_dt(s.get('last_msg_date'))}")
    elif kind == "groups":
        d = await _api(f"/api/v1/users/{tid}/groups")
        allg = [x for x in (d.get("data") or []) if isinstance(x, dict)]
        pub = [x for x in allg if (x.get("chat") or {}).get("username")]
        pub.sort(key=lambda x: int(x.get("messagesCount") or 0), reverse=True)
        rows = []
        for x in pub[:15]:
            ch = x["chat"]
            rows.append(f"• <a href=\"https://t.me/{html.escape(ch['username'])}\">{html.escape(str(ch.get('title') or ch['username']))}</a>"
                        f" — {int(x.get('messagesCount') or 0):,} پیام"
                        + (" · 🛡 ادمین" if x.get("isAdmin") else "") + (" · خارج‌شده" if x.get("isLeft") else ""))
        hidden = len(allg) - len(pub)
        text = (f"👥 <b>گروه‌هایِ عمومی</b> ({len(pub)} مورد)\n\n"
                + ("\n".join(rows) if rows else "گروهِ عمومی‌ای ثبت نشده.")
                + (f"\n\n🔒 {hidden} گروهِ خصوصی عمداً نمایش داده نمی‌شود." if hidden > 0 else ""))
    elif kind == "rep":
        d = await _api("/api/v1/users/reputation", [("id", tid)])
        s = d.get("data") if isinstance(d.get("data"), dict) else {k: v for k, v in d.items() if k not in ("tech", "success")}
        rows = [f"• {html.escape(str(k))}: {html.escape(str(v))[:60]}" for k, v in list(s.items())[:12]
                if not isinstance(v, (dict, list))]
        text = "⭐ <b>اعتبار</b>\n\n" + ("\n".join(rows) if rows else "اطلاعاتی نیست.")
    else:
        raise ValueError(kind)
    return text, d


async def lookup(q_raw: str) -> tuple[str, object, float, str]:
    """جست‌وجوی یک @یوزرنیم/آیدی. خروجی: ("ok", user_dict, cost, action) یا ("msg", متنِ پیام، 0, "").
    خطای سرویس → FunstatError."""
    q = (q_raw or "").strip().replace("https://t.me/", "").replace("t.me/", "").strip()
    digits = q.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    blocked = await _blocked_ids()
    if digits.lstrip("-").isdigit():
        tid = int(digits)
        if tid in blocked:
            return "msg", "⛔️ جست‌وجویِ این آیدی ممکن نیست.", 0.0, ""
        data = await _api("/api/v1/users/basic_info_by_id", [("id", tid)])
        action = "by_id"
    else:
        m = _USER_RE.match(q)
        if not m:
            return "msg", "فقط @یوزرنیم (۴ تا ۳۲ حرفِ انگلیسی/عدد/_) یا آیدیِ عددی بفرست.", 0.0, ""
        data = await _api("/api/v1/users/resolve_username", [("name", m.group(1))])
        action = "by_username"
    items = [x for x in (data.get("data") or []) if isinstance(x, dict)]
    if not items:
        return "msg", "چیزی پیدا نشد. این حساب در پایگاهِ داده نیست یا یوزرنیم تغییر کرده.", _cost_of(data), action
    u = items[0]
    if int(u.get("id") or 0) in blocked:
        return "msg", "⛔️ جست‌وجویِ این حساب ممکن نیست.", 0.0, ""
    return "ok", u, _cost_of(data), action


@funstat_router.callback_query(F.data.startswith("fst:q:"))
async def fst_detail(cb: CallbackQuery, bot: Bot):
    _, _, kind, tid_s = cb.data.split(":")
    tid = int(tid_s)
    uid = cb.from_user.id
    if tid in await _blocked_ids():
        await cb.answer("ممکن نیست.", show_alert=True)
        return
    deny = await _gate(uid)
    if deny:
        await cb.answer(deny, show_alert=True)
        return
    await cb.answer("⏳")
    try:
        text, d = await detail_text(kind, tid)
    except ValueError:
        await cb.answer("نامعتبر", show_alert=True)
        return
    except FunstatError as e:
        await _log(uid, f"detail_{kind}", str(tid), 0, False)
        await cb.message.answer("⚠️ " + e.message)
        await _notify_admin_error(bot, e)
        return
    await _log(uid, f"detail_{kind}", str(tid), _cost_of(d), True)
    await cb.message.answer(text, parse_mode="HTML", disable_web_page_preview=True,
                            reply_markup=_kb([_btn("🔎 جست‌وجویِ تازه", "fst:again", ButtonStyle.PRIMARY)]))


async def create_gate(cb: CallbackQuery) -> bool:
    """دروازهٔ «ساخت ربات ← Fun Stat». True = اجازهٔ ادامه؛ وگرنه صفحهٔ «ثبت درخواست» نشان داده می‌شود."""
    uid = cb.from_user.id
    ok, why = await _access(uid)
    if ok:
        return True
    if why == "off":
        await cb.answer("🔎 این بخش فعلاً غیرفعال است.", show_alert=True)
        return False
    async with Session() as s:
        pend = await s.scalar(select(FunstatRequest).where(
            FunstatRequest.telegram_id == uid, FunstatRequest.status == "pending"))
    head = ("⏰ دسترسیِ تستِ قبلیِ تو تمام شده.\n\n" if why == "expired" else "")
    text = (head + "🔎 <b>ربات Fun Stat</b>\n\n"
            "این نوعِ ربات الان در <b>مرحلهٔ تست</b> است و فقط برایِ چند نفر که مدیر دسترسی بدهد باز است. "
            "بعد از تست و رفعِ ایرادها برایِ همه فعال می‌شود.\n"
            "می‌توانی درخواستِ دسترسی ثبت کنی؛ بعد از تأییدِ مدیر خبرت می‌کنیم.")
    rows = []
    if pend:
        text += "\n\n✅ درخواستِ تو ثبت شده و در انتظارِ تأییدِ مدیر است."
    else:
        rows.append([_btn("📨 ثبتِ درخواست", "fst:req", ButtonStyle.SUCCESS)])
    rows.append([_btn("⬅️ بازگشت", "freerules:cancel", ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text(text, reply_markup=_kb(*rows))
    except Exception:
        await cb.message.answer(text, reply_markup=_kb(*rows))
    await cb.answer()
    return False


# ───────────────────────── پنلِ ادمین ─────────────────────────

async def _admin_text() -> tuple[str, InlineKeyboardMarkup]:
    c = await _cfg()
    async with Session() as s:
        grants = int(await s.scalar(select(func.count(FunstatAccess.id)).where(
            (FunstatAccess.expires_at.is_(None)) | (FunstatAccess.expires_at > datetime.utcnow()))) or 0)
        pend = int(await s.scalar(select(func.count(FunstatRequest.id)).where(
            FunstatRequest.status == "pending")) or 0)
        start = datetime.utcnow() - timedelta(days=1)
        used = int(await s.scalar(select(func.count(FunstatLog.id)).where(
            FunstatLog.created_at >= start, FunstatLog.ok == True)) or 0)  # noqa: E712
    text = (
        "🔎 <b>Fun Stat — مدیریت</b>\n\n"
        f"🔑 توکن: {'✅ ثبت شده' if c['token'] else '❌ ثبت نشده'}\n"
        f"🌐 آدرسِ سرویس: <code>{html.escape(c['base'])}</code>\n"
        f"🧪 حالت: <b>{'تست (فقط دسترسی‌داده‌شده‌ها)' if c['mode'] == 'test' else 'عمومی (همه، با سقفِ روزانه)'}</b>\n"
        f"🔌 وضعیت: {'🟢 روشن' if c['enabled'] else '🔴 خاموش'}\n"
        f"🔢 سقفِ روزانهٔ هر کاربر: {c['limit'] or 'بدون سقف'}\n"
        f"💰 آخرین موجودیِ اکانتِ سرویس: {c['balance'] or '—'}\n\n"
        f"👥 دسترسی‌هایِ فعال: {grants} · 📥 درخواست‌هایِ منتظر: {pend} · 🔎 جست‌وجو ۲۴ساعت: {used}"
    )
    kb = _kb(
        [_btn("🔑 تنظیمِ توکن", "fst:a:token"), _btn("🌐 آدرسِ سرویس", "fst:a:base")],
        [_btn("🧪/🌍 تغییرِ حالت (تست ↔ عمومی)", "fst:a:mode")],
        [_btn("🔌 روشن/خاموش", "fst:a:toggle"), _btn("🔢 سقفِ روزانه", "fst:a:limit")],
        [_btn(f"📥 درخواست‌ها ({pend})", "fst:a:reqs", ButtonStyle.PRIMARY),
         _btn(f"👥 دسترسی‌ها ({grants})", "fst:a:grants", ButtonStyle.PRIMARY)],
        [_btn("➕ اعطایِ دسترسیِ دستی", "fst:a:grant", ButtonStyle.SUCCESS),
         _btn("🚫 فهرستِ مسدود", "fst:a:block")],
        [_btn("💰 بررسیِ موجودی/اتصال", "fst:a:test")],
        [_btn("⬅️ بازگشت", "adm:cat:settings", ButtonStyle.DANGER)])
    return text, kb


async def _admin_guard(cb: CallbackQuery) -> bool:
    if not _is_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return False
    return True


async def _show_admin(target, edit: bool = True) -> None:
    text, kb = await _admin_text()
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception:
            logger.debug("ویرایشِ پنلِ Fun Stat ممکن نشد؛ پیامِ تازه")
    await target.answer(text, reply_markup=kb)


@funstat_router.callback_query(F.data == "fst:adm")
async def fst_adm(cb: CallbackQuery, state: FSMContext):
    if not await _admin_guard(cb):
        return
    await state.clear()
    await _show_admin(cb.message)
    await cb.answer()


@funstat_router.callback_query(F.data.startswith("fst:a:"))
async def fst_admin_action(cb: CallbackQuery, state: FSMContext):
    if not await _admin_guard(cb):
        return
    act = cb.data.split(":")[2]
    cancel = _kb([_btn("❌ انصراف", "fst:adm", ButtonStyle.DANGER)])
    if act == "token":
        await state.set_state(FunstatFSM.token)
        await cb.message.answer("🔑 توکنِ API را بفرست (پیامت بعد از دریافت خودکار پاک می‌شود):", reply_markup=cancel)
    elif act == "base":
        await state.set_state(FunstatFSM.base)
        await cb.message.answer(f"🌐 آدرسِ پایهٔ سرویس را بفرست (پیش‌فرض: <code>{DEFAULT_BASE}</code>):", reply_markup=cancel)
    elif act == "limit":
        await state.set_state(FunstatFSM.limit)
        await cb.message.answer("🔢 سقفِ جست‌وجویِ روزانهٔ هر کاربر را بفرست (۰ = بدونِ سقف):", reply_markup=cancel)
    elif act == "grant":
        await state.set_state(FunstatFSM.grant)
        await cb.message.answer("➕ آیدیِ عددیِ کاربر و تعدادِ روز را بفرست، مثل: <code>123456789 3</code>\n"
                                "(روزِ ۰ = دائمی)", reply_markup=cancel)
    elif act == "block":
        await state.set_state(FunstatFSM.block)
        bl = sorted(await _blocked_ids())
        await cb.message.answer("🚫 فهرستِ مسدود (این آیدی‌ها هرگز جست‌وجو نمی‌شوند):\n"
                                + (", ".join(f"<code>{x}</code>" for x in bl) or "—")
                                + "\n\nبرایِ افزودن آیدی بفرست؛ برایِ حذف <code>-آیدی</code> (مثلاً <code>-123</code>).",
                                reply_markup=cancel)
    elif act == "mode":
        c = await _cfg()
        await _set("funstat_mode", "public" if c["mode"] == "test" else "test")
        await _show_admin(cb.message)
        await cb.answer("✅ تغییر کرد")
        return
    elif act == "toggle":
        c = await _cfg()
        await _set("funstat_enabled", "0" if c["enabled"] else "1")
        await _show_admin(cb.message)
        await cb.answer("✅ تغییر کرد")
        return
    elif act == "test":
        await cb.answer("⏳")
        try:
            d = await _api(f"/api/v1/users/{PLATFORM_ADMIN_ID}/stats_min")
            bal = ((d.get("tech") or {}).get("current_ballance"))
            await cb.message.answer(f"✅ اتصال برقرار است.\n💰 موجودیِ اکانتِ سرویس: <b>{bal}</b>")
        except FunstatError as e:
            await cb.message.answer("❌ " + e.message)
        await _show_admin(cb.message, edit=False)
        return
    elif act == "reqs":
        async with Session() as s:
            rows = (await s.scalars(select(FunstatRequest).where(FunstatRequest.status == "pending")
                                    .order_by(FunstatRequest.id).limit(15))).all()
        if not rows:
            await cb.answer("درخواستِ منتظری نیست.", show_alert=True)
            return
        for r in rows:
            await cb.message.answer(
                f"📥 <b>درخواست</b>\n👤 {html.escape(r.full_name or '—')} "
                f"{('@' + r.username) if r.username else ''}\n🆔 <code>{r.telegram_id}</code>",
                reply_markup=_kb(
                    [_btn("۱ روز", f"fst:ok:{r.telegram_id}:1"), _btn("۳ روز", f"fst:ok:{r.telegram_id}:3"),
                     _btn("۷ روز", f"fst:ok:{r.telegram_id}:7")],
                    [_btn("♾ دائمی", f"fst:ok:{r.telegram_id}:0", ButtonStyle.SUCCESS),
                     _btn("❌ رد", f"fst:no:{r.telegram_id}", ButtonStyle.DANGER)]))
    elif act == "grants":
        async with Session() as s:
            rows = (await s.scalars(select(FunstatAccess).order_by(FunstatAccess.id.desc()).limit(25))).all()
        now = datetime.utcnow()
        lines, btns = [], []
        for a in rows:
            alive = a.expires_at is None or a.expires_at > now
            lines.append(f"{'🟢' if alive else '⚪️'} <code>{a.telegram_id}</code> — "
                         + ("دائمی" if a.expires_at is None else f"تا {_fa_date(a.expires_at)}"))
            btns.append([_btn(f"🗑 لغو {a.telegram_id}", f"fst:rv:{a.telegram_id}", ButtonStyle.DANGER)])
        await cb.message.answer("👥 <b>دسترسی‌ها</b>\n\n" + ("\n".join(lines) or "هنوز کسی ندارد."),
                                reply_markup=_kb(*btns, [_btn("⬅️ بازگشت", "fst:adm")]))
    await cb.answer()


async def _grant(tid: int, days: int, by: int, note: str = "") -> datetime | None:
    exp = None if days <= 0 else datetime.utcnow() + timedelta(days=days)
    async with Session() as s:
        a = await s.scalar(select(FunstatAccess).where(FunstatAccess.telegram_id == tid))
        if a:
            a.expires_at, a.granted_by, a.note = exp, by, note[:200]
        else:
            s.add(FunstatAccess(telegram_id=tid, granted_by=by, expires_at=exp, note=note[:200]))
        reqs = (await s.scalars(select(FunstatRequest).where(
            FunstatRequest.telegram_id == tid, FunstatRequest.status == "pending"))).all()
        for r in reqs:
            r.status = "approved"
        await s.commit()
    return exp


@funstat_router.callback_query(F.data.startswith("fst:ok:"))
async def fst_ok(cb: CallbackQuery, bot: Bot):
    if not await _admin_guard(cb):
        return
    _, _, tid_s, days_s = cb.data.split(":")
    tid, days = int(tid_s), int(days_s)
    exp = await _grant(tid, days, cb.from_user.id)
    when = "دائمی" if exp is None else f"تا {_fa_date(exp)}"
    try:
        await cb.message.edit_text(f"✅ دسترسیِ <code>{tid}</code> فعال شد ({when}).")
    except Exception:
        logger.debug("ویرایشِ پیامِ درخواست ممکن نشد (بی‌اهمیت)")
    await cb.answer("✅")
    try:
        await bot.send_message(tid, f"✅ دسترسیِ تستِ <b>Fun Stat</b> برایت فعال شد ({when}).\n"
                                    "برایِ ساختِ رباتِ Fun Stat خودت: «ساخت ربات» ← «🔎 Fun Stat» را بزن و مراحل را مثلِ بقیهٔ ربات‌ها برو.")
    except Exception:
        logger.info("اطلاعِ فعال‌شدنِ Fun Stat به کاربر نرسید (ربات را بلاک کرده؟)")


@funstat_router.callback_query(F.data.startswith("fst:no:"))
async def fst_no(cb: CallbackQuery, bot: Bot):
    if not await _admin_guard(cb):
        return
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        for r in (await s.scalars(select(FunstatRequest).where(
                FunstatRequest.telegram_id == tid, FunstatRequest.status == "pending"))).all():
            r.status = "rejected"
        await s.commit()
    try:
        await cb.message.edit_text(f"❌ درخواستِ <code>{tid}</code> رد شد.")
    except Exception:
        logger.debug("ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    await cb.answer("رد شد")
    try:
        await bot.send_message(tid, "ℹ️ درخواستِ دسترسی به Fun Stat فعلاً تأیید نشد.")
    except Exception:
        logger.debug("اطلاعِ ردِ درخواست به کاربر نرسید (بی‌اهمیت)")


@funstat_router.callback_query(F.data.startswith("fst:rv:"))
async def fst_revoke(cb: CallbackQuery, bot: Bot):
    if not await _admin_guard(cb):
        return
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        a = await s.scalar(select(FunstatAccess).where(FunstatAccess.telegram_id == tid))
        if a:
            await s.delete(a)
            await s.commit()
    await cb.answer("🗑 دسترسی لغو شد", show_alert=True)


async def _admin_msg_guard(msg: Message) -> bool:
    return _is_admin(msg.from_user.id)


@funstat_router.message(StateFilter(FunstatFSM.token), F.text)
async def fst_set_token(msg: Message, state: FSMContext):
    if not await _admin_msg_guard(msg):
        return
    tok = _clean_token(msg.text or "")
    try:
        await msg.delete()          # توکن در چت نماند
    except Exception:
        logger.debug("حذفِ پیامِ توکن ممکن نشد (بی‌اهمیت)")
    if len(tok) < 20 or " " in tok:
        await msg.answer("توکن معتبر به‌نظر نمی‌رسد. دوباره بفرست یا انصراف بزن.")
        return
    await _set("funstat_token", tok)
    await state.clear()
    await msg.answer("✅ توکن ذخیره شد (پیامِ شما پاک شد).")
    await _show_admin(msg, edit=False)


@funstat_router.message(StateFilter(FunstatFSM.base), F.text)
async def fst_set_base(msg: Message, state: FSMContext):
    if not await _admin_msg_guard(msg):
        return
    base = (msg.text or "").strip().rstrip("/")
    if not base.startswith("https://") or " " in base or "." not in base:
        await msg.answer("آدرس باید با https:// شروع شود، مثل https://funstat.info")
        return
    await _set("funstat_base", base)
    await state.clear()
    await msg.answer("✅ ذخیره شد.")
    await _show_admin(msg, edit=False)


@funstat_router.message(StateFilter(FunstatFSM.limit), F.text)
async def fst_set_limit(msg: Message, state: FSMContext):
    if not await _admin_msg_guard(msg):
        return
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit():
        await msg.answer("فقط عدد (۰ = بدونِ سقف).")
        return
    await _set("funstat_daily_limit", raw)
    await state.clear()
    await _show_admin(msg, edit=False)


@funstat_router.message(StateFilter(FunstatFSM.grant), F.text)
async def fst_manual_grant(msg: Message, state: FSMContext, bot: Bot):
    if not await _admin_msg_guard(msg):
        return
    parts = (msg.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await msg.answer("قالب: <code>آیدی روز</code> — مثل <code>123456789 3</code>")
        return
    tid, days = int(parts[0]), int(parts[1])
    exp = await _grant(tid, days, msg.from_user.id, "manual")
    await state.clear()
    when = "دائمی" if exp is None else f"تا {_fa_date(exp)}"
    await msg.answer(f"✅ دسترسیِ <code>{tid}</code> فعال شد ({when}).")
    try:
        await bot.send_message(tid, f"✅ دسترسیِ تستِ <b>Fun Stat</b> برایت فعال شد ({when}). /funstat")
    except Exception:
        logger.info("اطلاعِ دسترسیِ دستی به کاربر نرسید (بی‌اهمیت)")


@funstat_router.message(StateFilter(FunstatFSM.block), F.text)
async def fst_block_edit(msg: Message, state: FSMContext):
    if not await _admin_msg_guard(msg):
        return
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    remove = raw.startswith("-")
    num = raw.lstrip("-")
    if not num.isdigit():
        await msg.answer("فقط آیدیِ عددی (برایِ حذف با - جلویش).")
        return
    try:
        cur = {int(x) for x in json.loads(await _get("funstat_block", "[]"))}
    except Exception:
        cur = set()
    (cur.discard if remove else cur.add)(int(num))
    await _set("funstat_block", json.dumps(sorted(cur)))
    await state.clear()
    await msg.answer("✅ به‌روز شد.")
    await _show_admin(msg, edit=False)
