# -*- coding: utf-8 -*-
"""
platform_sadmin.py — «پنل مدیریتِ بات‌ساز در سروش پلاس» (PATCH_PLATFORM_SADMIN_V1).

مدیر یک رباتِ جداگانه در سروش (از بات فادر، غیر از رباتِ پشتیبانی) می‌سازد و توکنش را با /sadmin در
رباتِ تلگرامِ بات‌ساز ثبت می‌کند. چون به سروش اعتماد کامل نیست، ورود به پنل با «کدِ دسترسی» است که فقط
در تلگرام تعیین/ساخته می‌شود:
  • کدِ ثابت (به‌صورتِ هشِ scrypt ذخیره می‌شود، نه خودِ کد) یا کدِ «یک‌بارمصرفِ ۵دقیقه‌ای» که از تلگرام می‌گیری؛
  • ۵ تلاشِ اشتباه ⇒ ۱۵ دقیقه قفل + هشدار به تلگرامِ مدیر؛ نشستِ لغزان (پیش‌فرض ۳۰ دقیقه)؛
  • کارهایِ خطرناک (حذفِ ربات، تغییرِ کیف پول، ارسالِ همگانی، بک‌آپ) دوباره کد می‌خواهند (step-up)؛
  • هر ورود و هر کارِ مهم به تلگرامِ مدیر گزارش می‌شود؛ «قطعِ همهٔ نشست‌ها» از تلگرام ممکن است.
پیام حاویِ کد بعد از خواندن از چتِ سروش پاک می‌شود.

منطقِ کارها (آمار/ربات/کاربر/کیف پول/همگانی): platform_sadmin_actions.py.
کلیدهایِ PlatformSetting: sadm_token، sadm_enabled، sadm_codehash، sadm_otp، sadm_ttl، sadm_epoch، sadm_offset،
sadm_log، psdm:* (نشست/وضعیتِ گفتگو/شکست‌ها برایِ هر چت)، psdm_bc (کارِ ارسالِ همگانی)، psdm_bc_stop.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import html as _html
import json
import os
import re
import secrets
import time

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import platform_sadmin_actions as A
from config import PLATFORM_ADMIN_ID
from platform_rubika import _get, _set          # ذخیرهٔ کلید/مقدارِ پلتفرم (مشترک)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_sadmin_router = Router()

K_TOKEN, K_EN, K_HASH, K_OTP, K_TTL, K_EPOCH, K_OFFSET, K_LOG = (
    "sadm_token", "sadm_enabled", "sadm_codehash", "sadm_otp", "sadm_ttl", "sadm_epoch", "sadm_offset", "sadm_log")
K_BC, K_BC_STOP = "psdm_bc", "psdm_bc_stop"
TTL_CHOICES = (10, 30, 60, 120)
MIN_CODE = 8
OTP_MINUTES = 5
CHAT_FAILS, CHAT_WINDOW_MIN, CHAT_LOCK_MIN = 5, 15, 15
GLOBAL_FAILS, GLOBAL_WINDOW_MIN, GLOBAL_LOCK_MIN = 15, 60, 30
POLL_LONG_SECS = 10
MSG_CHUNK = 3500

BTN_MENU, BTN_LOGOUT, BTN_CANCEL = "🏠 منو", "🚪 خروج", "❌ لغو"
BTN_STATS, BTN_BOTS, BTN_USERS, BTN_WALLET, BTN_BC, BTN_SYS = (
    "📊 آمار", "🤖 ربات‌ها", "👥 کاربران", "💰 کیف پول", "📣 پیام همگانی", "🖥 سیستم")
BTN_RECENT, BTN_EXP, BTN_ARCH, BTN_LOCKED = "📋 جدیدترین‌ها", "⏳ نزدیکِ انقضا", "📦 بایگانی‌شده‌ها", "🔒 قفل‌شده‌ها"
BTN_LOCK, BTN_UNLOCK, BTN_ADDDAYS, BTN_DELETE = "🔒 قفل", "🔓 رفع قفل", "➕ افزودنِ روز", "🗑 حذفِ ربات"
BTN_BACK_BOTS, BTN_BACK_USERS = "🔙 ربات‌ها", "🔙 کاربران"
BTN_PLAN = "🎚 تغییرِ پلن"
PLAN_BTNS = {"رایگان": "free", "رشد": "growth", "حرفه‌ای": "pro"}
BTN_ADJ, BTN_BAN, BTN_UNBAN = "💰 تغییرِ موجودی", "🚫 بن", "✅ رفعِ بن"
BTN_NOBTN, BTN_SEND, BTN_TEST, BTN_STOPBC = "⏭ بدونِ دکمه", "✅ ارسالِ نهایی", "🧪 فقط تست به تلگرامِ من", "🛑 توقفِ ارسالِ همگانی"
BTN_BACKUP, BTN_REFRESH = "🗄 بک‌آپِ بیرونی همین الان", "🔄 به‌روزرسانی"

MAIN_ROWS = [[BTN_STATS, BTN_BOTS], [BTN_USERS, BTN_WALLET], [BTN_BC, BTN_SYS], [BTN_LOGOUT]]
_UI_LABELS = {v for k, v in dict(globals()).items() if k.startswith("BTN_") and isinstance(v, str)}


# ─────────────── کدِ دسترسی ───────────────

def hash_code(code: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(code.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def check_hash(code: str, stored: str) -> bool:
    try:
        salt_hex, h_hex = stored.split("$", 1)
        h = hashlib.scrypt(code.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except (ValueError, AttributeError):
        return False


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


async def _jget(key: str) -> dict:
    try:
        v = json.loads(await _get(key) or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


async def _jset(key: str, obj: dict | None) -> None:
    await _set(key, json.dumps(obj, ensure_ascii=False) if obj else "")


def _k(prefix: str, chat: str) -> str:
    return f"psdm:{prefix}:{chat}"[:60]


async def verify_code(code: str, now: datetime.datetime) -> str | None:
    """«static» / «otp» (یک‌بارمصرف؛ بعد از موفقیت باطل می‌شود) / None."""
    code = (code or "").strip()
    if not code:
        return None
    stored = (await _get(K_HASH)).strip()
    if stored and check_hash(code, stored):
        return "static"
    otp = await _jget(K_OTP)
    exp = _parse(otp.get("x"))
    if otp.get("h") and exp and now <= exp and check_hash(code, otp["h"]):
        await _jset(K_OTP, None)
        return "otp"
    return None


async def _fail_state(key: str, now: datetime.datetime) -> dict:
    st = await _jget(key)
    lock = _parse(st.get("l"))
    return {"n": int(st.get("n") or 0), "w": st.get("w"), "l": _iso(lock) if lock and lock > now else None}


async def locked_until(chat: str, now: datetime.datetime) -> datetime.datetime | None:
    for key in (_k("f", chat), "psdm:gf"):
        lk = _parse((await _fail_state(key, now)).get("l"))
        if lk and lk > now:
            return lk
    return None


async def register_fail(chat: str, now: datetime.datetime) -> tuple[int, bool, bool]:
    """(تعدادِ شکستِ این چت، چت قفل شد؟، کلِ ورودها قفل شد؟)."""
    out = []
    for key, limit, win, lock_min in ((_k("f", chat), CHAT_FAILS, CHAT_WINDOW_MIN, CHAT_LOCK_MIN),
                                      ("psdm:gf", GLOBAL_FAILS, GLOBAL_WINDOW_MIN, GLOBAL_LOCK_MIN)):
        st = await _fail_state(key, now)
        w = _parse(st["w"])
        if not w or now - w > datetime.timedelta(minutes=win):
            st["n"], st["w"] = 0, _iso(now)
        st["n"] += 1
        locked = st["n"] >= limit
        if locked:
            st["l"], st["n"], st["w"] = _iso(now + datetime.timedelta(minutes=lock_min)), 0, None
        await _jset(key, st)
        out.append((st["n"], locked))
    return out[0][0], out[0][1], out[1][1]


async def clear_fails(chat: str) -> None:
    await _jset(_k("f", chat), None)


# ─────────────── نشست ───────────────

async def _ttl_min() -> int:
    try:
        v = int((await _get(K_TTL, "30")) or 30)
    except ValueError:
        v = 30
    return v if v in TTL_CHOICES else 30


async def _epoch() -> str:
    return (await _get(K_EPOCH, "0")) or "0"


async def start_session(chat: str, uid: str, name: str, now: datetime.datetime) -> None:
    await _jset(_k("s", chat), {"ep": await _epoch(), "u": uid, "n": name,
                                "x": _iso(now + datetime.timedelta(minutes=await _ttl_min()))})
    await _jset(_k("t", chat), {"s": "main", "d": {}})


async def session_valid(chat: str, uid: str, now: datetime.datetime, touch: bool = True) -> dict | None:
    ss = await _jget(_k("s", chat))
    exp = _parse(ss.get("x"))
    if not ss or ss.get("ep") != await _epoch() or ss.get("u") != uid or not exp or now > exp:
        return None
    if touch:
        ss["x"] = _iso(now + datetime.timedelta(minutes=await _ttl_min()))
        await _jset(_k("s", chat), ss)
    return ss


async def end_session(chat: str) -> None:
    await _jset(_k("s", chat), None)
    await _jset(_k("t", chat), None)


async def log_event(kind: str, name: str, uid: str, now: datetime.datetime) -> None:
    ev = (await _jget(K_LOG)).get("e", [])
    ev.append({"t": _iso(now), "k": kind, "n": name, "u": uid})
    await _jset(K_LOG, {"e": ev[-20:]})


# ─────────────── ارتباط با سروش ───────────────

def kp(rows: list[list[str]] | None) -> dict | None:
    if rows is None:
        return None
    if not rows:
        return {"remove_keyboard": True}
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "is_persistent": True}


class SoroushIO:
    """کلاینتِ کوچکِ سروش؛ تست‌ها یک نمونهٔ جعلی با همین سه متد می‌دهند."""

    def __init__(self, token: str):
        self.token = token

    async def send(self, chat: str, text: str, rows: list[list[str]] | None = None) -> None:
        from platform_splus import api_call
        parts = [text[i:i + MSG_CHUNK] for i in range(0, len(text), MSG_CHUNK)] or [""]
        for i, part in enumerate(parts):
            body = {"chat_id": chat, "text": part or "—"}
            if rows is not None and i == len(parts) - 1:
                body["reply_markup"] = kp(rows)
            r = await api_call(self.token, "sendMessage", body)
            if not r.get("ok") and "reply_markup" in body:
                body.pop("reply_markup")
                await api_call(self.token, "sendMessage", body)

    async def delete(self, chat: str, message_id) -> None:
        if message_id:
            from platform_splus import api_call
            await api_call(self.token, "deleteMessage", {"chat_id": chat, "message_id": message_id}, timeout=15)

    async def download(self, file_id: str) -> bytes | None:
        from platform_splus import download_file
        return await download_file(self.token, file_id)


# ─────────────── کنسول ───────────────

def _num(s: str) -> int | None:
    t = (s or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    t = re.sub(r"[،,٬\s]", "", t)        # جداکننده‌هایِ هزارگان (فارسی/عربی/لاتین) و فاصله
    return int(t) if re.fullmatch(r"[+-]?\d+", t) else None


class Console:
    def __init__(self, io, tg_bot, now_fn=None):
        self.io, self.tg, self.now_fn = io, tg_bot, now_fn or datetime.datetime.now
        self.bg: list[asyncio.Task] = []

    # --- کمک‌ها ---
    async def _say(self, chat: str, text: str, rows=None) -> None:
        await self.io.send(chat, text, rows)

    async def _audit(self, text: str) -> None:
        if self.tg is None or not PLATFORM_ADMIN_ID:
            return
        try:
            await self.tg.send_message(PLATFORM_ADMIN_ID, "🛡 پنلِ سروش: " + text, parse_mode=None)
        except Exception:
            logger.exception("sadmin: گزارش به تلگرامِ مدیر ارسال نشد")

    async def _state(self, chat: str) -> dict:
        st = await _jget(_k("t", chat))
        return st if st.get("s") else {"s": "main", "d": {}}

    async def _go(self, chat: str, s: str, d: dict | None = None) -> None:
        await _jset(_k("t", chat), {"s": s, "d": d or {}})

    async def _menu(self, chat: str, note: str = "") -> None:
        await self._go(chat, "main")
        await self._say(chat, (note + "\n\n" if note else "") + "🛡 پنلِ مدیریتِ بات‌ساز — یکی را انتخاب کن 👇", MAIN_ROWS)

    # --- ورودی اصلی ---
    async def handle(self, m: dict) -> None:
        now = self.now_fn()
        chat, uid = m["chat_id"], m["user_id"]
        if (await _get(K_EN, "0")) != "1":
            await self._say(chat, "⛔ این پنل فعال نیست.")
            return
        sess = await session_valid(chat, uid, now)
        if not sess:
            await self._unauth(m, now)
            return
        await self._dispatch(m, now)

    async def _unauth(self, m: dict, now: datetime.datetime) -> None:
        chat, uid, txt = m["chat_id"], m["user_id"], (m["text"] or "").strip()
        lk = await locked_until(chat, now)
        if lk:
            await self._say(chat, f"⏳ ورود موقتاً قفل است؛ بعد از {lk:%H:%M} دوباره امتحان کن.")
            return
        # دکمه‌هایِ منو (بعد از انقضایِ نشست) نه «کد»اند و نه باید از سهمیهٔ تلاش کم کنند
        if txt.lower() in ("", "/start", "start", "/menu") or m["kind"] != "text" or txt in _UI_LABELS:
            await self._say(chat, "🔐 پنلِ مدیریتِ بات‌ساز\nکدِ دسترسی را بفرست (کدی که در تلگرام ساخته‌ای).", [])
            return
        how = await verify_code(txt, now)
        await self.io.delete(chat, m.get("message_id"))          # کد در چتِ سروش نماند
        if how:
            await clear_fails(chat)
            name = " ".join(x for x in (m.get("first_name"), m.get("last_name")) if x) or "—"
            await start_session(chat, uid, name, now)
            await log_event("login-" + how, name, uid, now)
            await self._audit(f"ورود موفق ({'کدِ یک‌بارمصرف' if how == 'otp' else 'کدِ ثابت'})\n"
                              f"👤 {name}" + (f" @{m['username']}" if m.get("username") else "") + f"\n🆔 سروش: {uid}")
            await self._menu(chat, "✅ وارد شدی.")
            return
        n, chat_locked, glob_locked = await register_fail(chat, now)
        await log_event("fail", m.get("first_name") or "—", uid, now)
        if chat_locked or glob_locked:
            await self._audit(f"⚠️ {'چندین' if glob_locked else CHAT_FAILS} تلاشِ ناموفقِ ورود "
                              f"(سروش {uid}) — ورود {'کلاً' if glob_locked else 'برایِ این چت'} قفل شد.")
            await self._say(chat, "⛔ تلاشِ بیش‌ازحد؛ ورود موقتاً قفل شد.")
        else:
            await self._say(chat, f"❌ کدِ نادرست ({n}/{CHAT_FAILS})")

    # --- مسیریابی ---
    async def _dispatch(self, m: dict, now: datetime.datetime) -> None:
        chat, txt = m["chat_id"], (m["text"] or "").strip()
        st = await self._state(chat)
        s, d = st["s"], st["d"]
        if txt in (BTN_MENU, "/menu", "/start", "منو"):
            return await self._menu(chat)
        if txt in (BTN_LOGOUT, "/logout"):
            await end_session(chat)
            await self._audit("خروج از پنل")
            return await self._say(chat, "👋 از پنل خارج شدی.", [])
        if txt in (BTN_STOPBC, "توقف"):
            await _set(K_BC_STOP, "1")
            return await self._say(chat, "🛑 درخواستِ توقفِ ارسالِ همگانی ثبت شد.", MAIN_ROWS)
        if s == "stepup":
            return await self._stepup(chat, m, d, now)
        if txt == BTN_CANCEL:
            return await self._menu(chat, "لغو شد.")
        # دکمه‌هایِ منویِ اصلی
        top = {BTN_STATS: self._stats, BTN_BOTS: self._bots_menu, BTN_USERS: self._users_menu,
               BTN_WALLET: self._wallet_menu, BTN_BC: self._bc_menu, BTN_SYS: self._sys}
        if txt in top:
            return await top[txt](chat)
        handler = getattr(self, "_s_" + s.replace(":", "_"), None)
        if handler is None:
            return await self._menu(chat)
        await handler(chat, m, d, now)

    # --- آمار / سیستم ---
    async def _stats(self, chat: str) -> None:
        await self._say(chat, await A.stats_text(self.now_fn()), MAIN_ROWS)

    async def _sys(self, chat: str) -> None:
        import glob
        from models import PlatformTicket
        from config import Session
        from sqlalchemy import func, select
        now = self.now_fn()
        base = os.path.dirname(os.path.abspath(__file__))
        dumps = glob.glob(os.path.join(base, "backups", "botsaz-pg-*.dump")) + glob.glob(os.path.join(base, "backups", "botsaz-*.db"))
        if dumps:
            newest = max(dumps, key=os.path.getmtime)
            dump_txt = (f"{os.path.basename(newest)} · {os.path.getsize(newest) / 1024 / 1024:.1f}MB · "
                        f"{A._ago(datetime.datetime.fromtimestamp(os.path.getmtime(newest)), now)}")
        else:
            dump_txt = "نیست"
        async with Session() as s:
            open_t = int(await s.scalar(select(func.count()).select_from(PlatformTicket).where(
                PlatformTicket.status != "closed")) or 0)
        ev = (await _jget(K_LOG)).get("e", [])[-5:]
        logins = "\n".join(f"  {e['t'][:16].replace('T', ' ')} · {e['k']} · {e['n']}" for e in reversed(ev)) or "  —"
        text = ("🖥 سیستم\n\n"
                f"🗄 آخرین بک‌آپِ محلی: {dump_txt}\n"
                f"☁️ آخرین بک‌آپِ بیرونی: {(await _get('offsite_last_info')) or 'هنوز نرفته'}\n"
                f"🎫 تیکتِ باز (روبیکا/سروش): {open_t}\n"
                f"⏱ مدتِ نشست: {await _ttl_min()} دقیقه\n\n📋 آخرین ورودها:\n{logins}")
        await self._go(chat, "sys")
        await self._say(chat, text, [[BTN_BACKUP, BTN_REFRESH], [BTN_MENU]])

    async def _s_sys(self, chat, m, d, now) -> None:
        txt = (m["text"] or "").strip()
        if txt == BTN_REFRESH:
            return await self._sys(chat)
        if txt == BTN_BACKUP:
            return await self._ask_stepup(chat, {"a": "sys_backup", "label": "ارسالِ بک‌آپِ بیرونی به تلگرام"})
        await self._sys(chat)

    # --- ربات‌ها ---
    async def _bots_menu(self, chat: str, note: str = "") -> None:
        await self._go(chat, "bots")
        await self._say(chat, (note + "\n\n" if note else "") +
                        "🤖 ربات‌ها\nیوزرنیم، «#آیدی» (مثل #123) یا آیدیِ مالک را بفرست؛ یا یکی از دکمه‌ها:",
                        [[BTN_RECENT, BTN_EXP], [BTN_ARCH, BTN_LOCKED], [BTN_MENU]])

    async def _bot_list(self, chat: str, bots, title: str) -> None:
        if not bots:
            return await self._say(chat, "چیزی پیدا نشد.")
        now = self.now_fn()
        await self._say(chat, f"{title}\n\n" + "\n".join(A.bot_line(b, now) for b in bots) +
                        "\n\nبرایِ بازکردنِ کارت «#آیدی» بفرست (مثل #" + str(bots[0].id) + ").")

    async def _s_bots(self, chat, m, d, now) -> None:
        txt = (m["text"] or "").strip()
        presets = {BTN_RECENT: ("recent", "📋 جدیدترین ربات‌ها"), BTN_EXP: ("expiring", "⏳ نزدیکِ انقضا"),
                   BTN_ARCH: ("archived", "📦 بایگانی‌شده‌ها"), BTN_LOCKED: ("locked", "🔒 قفل‌شده‌ها")}
        if txt in presets:
            return await self._bot_list(chat, await A.preset_bots(presets[txt][0], now), presets[txt][1])
        found = await A.search_bots(txt)
        if len(found) == 1 or (txt.startswith("#") and found):
            return await self._bot_card(chat, found[0].id)
        await self._bot_list(chat, found, f"نتیجهٔ «{txt}»")

    async def _bot_card(self, chat: str, bot_id: int) -> None:
        card = await A.bot_card(bot_id, self.now_fn())
        if not card:
            return await self._say(chat, "ربات پیدا نشد.")
        await self._go(chat, "bot_card", {"bot": bot_id})
        await self._say(chat, card, [[BTN_LOCK, BTN_UNLOCK], [BTN_ADDDAYS, BTN_PLAN], [BTN_DELETE],
                                     [BTN_BACK_BOTS, BTN_MENU]])

    async def _s_bot_card(self, chat, m, d, now) -> None:
        txt, bid = (m["text"] or "").strip(), d.get("bot")
        if txt == BTN_BACK_BOTS:
            return await self._bots_menu(chat)
        if txt == BTN_LOCK:
            await self._go(chat, "bot_lock", d)
            return await self._say(chat, "دلیلِ قفل را بفرست (به مالکِ ربات نشان داده می‌شود):", [[BTN_CANCEL]])
        if txt == BTN_UNLOCK:
            ok, msg = await A.unlock_bot(bid, self.tg)
            await self._audit(f"رفعِ قفلِ ربات #{bid}")
            return await self._after(chat, ok, msg, lambda: self._bot_card(chat, bid))
        if txt == BTN_ADDDAYS:
            await self._go(chat, "bot_days", d)
            return await self._say(chat, "چند روز اضافه شود؟ (۱ تا ۳۶۵)", [[BTN_CANCEL]])
        if txt == BTN_PLAN:
            await self._go(chat, "bot_plan", d)
            return await self._say(chat, "🎚 پلنِ جدید را انتخاب کن (بدونِ پرداخت اعمال می‌شود):",
                                   [list(PLAN_BTNS), [BTN_CANCEL]])
        if txt == BTN_DELETE:
            await self._go(chat, "bot_del", d)
            return await self._say(chat, "⚠️ حذفِ ربات برگشت‌ناپذیر است.\nدلیل را بفرست (به مالک نشان داده می‌شود) یا «-» بفرست:",
                                   [[BTN_CANCEL]])
        await self._bot_card(chat, bid)

    async def _after(self, chat: str, ok: bool, msg: str, again) -> None:
        await self._say(chat, ("✅ " if ok else "❌ ") + msg)
        await again()

    async def _s_bot_lock(self, chat, m, d, now) -> None:
        ok, msg = await A.lock_bot(d["bot"], m["text"], self.tg)
        if ok:
            await self._audit(f"قفلِ ربات #{d['bot']}: {m['text'][:80]}")
        await self._after(chat, ok, msg, lambda: self._bot_card(chat, d["bot"]))

    async def _s_bot_plan(self, chat, m, d, now) -> None:
        plan = PLAN_BTNS.get((m["text"] or "").strip())
        if not plan:
            return await self._say(chat, "یکی از دکمه‌ها را انتخاب کن.")
        ok, msg = await A.set_bot_plan(d["bot"], plan, self.tg)
        if ok:
            await self._audit(f"تغییرِ پلنِ ربات #{d['bot']} → {plan}")
        await self._after(chat, ok, msg, lambda: self._bot_card(chat, d["bot"]))

    async def _s_bot_days(self, chat, m, d, now) -> None:
        n = _num(m["text"])
        if n is None:
            return await self._say(chat, "❌ یک عددِ ۱ تا ۳۶۵ بفرست.")
        ok, msg = await A.add_days_to_bot(d["bot"], n, now, self.tg)
        if ok:
            await self._audit(f"افزودنِ {n} روز به ربات #{d['bot']}")
        await self._after(chat, ok, msg, lambda: self._bot_card(chat, d["bot"]))

    async def _s_bot_del(self, chat, m, d, now) -> None:
        reason = (m["text"] or "").strip() or "-"
        await self._ask_stepup(chat, {"a": "del_bot", "bot": d["bot"], "reason": reason[:300],
                                      "label": f"حذفِ برگشت‌ناپذیرِ ربات #{d['bot']}"})

    # --- کاربران و کیف پول ---
    async def _users_menu(self, chat: str, intent: str = "", note: str = "") -> None:
        await self._go(chat, "users", {"intent": intent})
        await self._say(chat, (note + "\n\n" if note else "") +
                        ("💰 کیف پول — " if intent == "wallet" else "👥 کاربران — ") +
                        "آیدیِ عددیِ تلگرام، یوزرنیم یا بخشی از نام را بفرست:", [[BTN_MENU]])

    async def _wallet_menu(self, chat: str) -> None:
        await self._users_menu(chat, "wallet")

    async def _s_users(self, chat, m, d, now) -> None:
        found = await A.search_users(m["text"])
        if not found:
            return await self._say(chat, "کاربری پیدا نشد؛ دوباره بفرست.")
        if len(found) == 1:
            return await self._user_card(chat, found[0].telegram_id, d.get("intent", ""))
        await self._say(chat, "نتایج:\n\n" + "\n".join(A.user_line(u) for u in found) +
                        "\n\nآیدیِ عددیِ کاربرِ موردنظر را بفرست.")

    async def _user_card(self, chat: str, tg_id: int, intent: str = "") -> None:
        card = await A.user_card(tg_id, self.now_fn())
        if not card:
            return await self._say(chat, "کاربر پیدا نشد.")
        await self._go(chat, "user_card", {"tg": tg_id})
        await self._say(chat, card, [[BTN_ADJ, BTN_BAN, BTN_UNBAN], [BTN_BACK_USERS, BTN_MENU]])
        if intent == "wallet":
            await self._wallet_prompt(chat, tg_id)

    async def _wallet_prompt(self, chat: str, tg_id: int) -> None:
        await self._go(chat, "wallet_amt", {"tg": tg_id})
        await self._say(chat, "مبلغ را بفرست: مثبت = افزایش، منفی = کسر؛ بعدش (اختیاری) توضیح.\n"
                              "مثال: 50000 جبرانِ قطعی   یا   -20000 اصلاحِ اشتباه", [[BTN_CANCEL]])

    async def _s_user_card(self, chat, m, d, now) -> None:
        txt, tg = (m["text"] or "").strip(), d["tg"]
        if txt == BTN_BACK_USERS:
            return await self._users_menu(chat)
        if txt == BTN_ADJ:
            return await self._wallet_prompt(chat, tg)
        if txt in (BTN_BAN, BTN_UNBAN):
            ok, msg = await A.set_ban(tg, txt == BTN_BAN)
            await self._audit(f"{'بن' if txt == BTN_BAN else 'رفعِ بن'} کاربر {tg}")
            return await self._after(chat, ok, msg, lambda: self._user_card(chat, tg))
        await self._user_card(chat, tg)

    async def _s_wallet_amt(self, chat, m, d, now) -> None:
        parts = (m["text"] or "").strip().split(None, 1)
        delta = _num(parts[0]) if parts else None
        if not delta:
            return await self._say(chat, "❌ مبلغ نامعتبر؛ مثال: 50000 یا -20000 توضیح")
        note = parts[1][:150] if len(parts) > 1 else ""
        await self._ask_stepup(chat, {"a": "wallet", "tg": d["tg"], "delta": delta, "note": note,
                                      "label": f"{'افزایش' if delta > 0 else 'کسر'} {abs(delta):,} تومان "
                                               f"{'به' if delta > 0 else 'از'} کیف‌پولِ کاربر {d['tg']}"})

    # --- پیامِ همگانی ---
    async def _bc_menu(self, chat: str) -> None:
        now = self.now_fn()
        lines = []
        for key, label in A.AUDIENCES:
            lines.append(f"• {label}: {len(await A.audience_ids(key, now))}")
        await self._go(chat, "bc_aud")
        await self._say(chat, "📣 پیامِ همگانی (پیشرفته)\nمخاطب را انتخاب کن:\n\n" + "\n".join(lines),
                        [[label] for _, label in A.AUDIENCES] + [[BTN_STOPBC], [BTN_MENU]])

    async def _s_bc_aud(self, chat, m, d, now) -> None:
        key = next((k for k, lbl in A.AUDIENCES if lbl == (m["text"] or "").strip()), None)
        if not key:
            return await self._say(chat, "یکی از مخاطب‌ها را از دکمه‌ها انتخاب کن.")
        ids = await A.audience_ids(key, now)
        if not ids:
            return await self._say(chat, "این مخاطب خالی است.")
        await self._go(chat, "bc_compose", {"aud": key})
        await self._say(chat,
                        f"مخاطب: {A.AUDIENCE_LABEL[key]} — {len(ids)} نفر\n\n"
                        "پیام را بفرست: متن، یا عکس/ویدیو/فایل/ویس با کپشن.\n"
                        "قالب: **بولد**  __ایتالیک__  ~~خط‌خورده~~  `کد`  [متن](https://لینک)\n"
                        "(یا با «html:» در ابتدا، تگ‌هایِ تلگرام را مستقیم بنویس)", [[BTN_CANCEL]])

    async def _s_bc_compose(self, chat, m, d, now) -> None:
        if m["kind"] not in ("text", "file"):
            return await self._say(chat, "متن یا فایل بفرست.")
        html_text = A.render_text(m["text"] or "") if (m["text"] or "") else ""
        src_msg = None
        try:
            if m["kind"] == "file":
                data = await self.io.download(m["file_id"])
                if not data:
                    return await self._say(chat, "❌ فایل از سروش گرفته نشد (شاید بزرگ است). متن یا فایلِ دیگری بفرست.")
                from aiogram.types import BufferedInputFile
                f = BufferedInputFile(data, filename=m.get("file_name") or "file")
                send = {"photo": self.tg.send_photo, "video": self.tg.send_video, "voice": self.tg.send_voice,
                        "audio": self.tg.send_audio}.get(m.get("file_type"), self.tg.send_document)
                kw = {"caption": html_text or None, "parse_mode": "HTML"} if m.get("file_type") != "voice" else {}
                msg = await send(PLATFORM_ADMIN_ID, f, **kw)
            else:
                msg = await self.tg.send_message(PLATFORM_ADMIN_ID, html_text, parse_mode="HTML")
            src_msg = msg.message_id
        except Exception as e:
            logger.exception("sadmin: ساختِ پیش‌نمایشِ همگانی ناموفق")
            return await self._say(chat, f"❌ ساختِ پیام ناموفق (فرمت/تگ؟): {type(e).__name__}: {str(e)[:120]}\n"
                                         "اصلاح کن و دوباره بفرست.")
        d = {**d, "src_msg": src_msg, "descr": (m["text"] or "[رسانه]")[:60]}
        await self._go(chat, "bc_button", d)
        await self._say(chat, "👁 پیش‌نمایش به تلگرامِ خودت فرستاده شد.\n"
                              "می‌خواهی زیرِ پیام دکمهٔ لینک باشد؟ بنویس:\nمتنِ دکمه | https://لینک\nیا «بدونِ دکمه».",
                        [[BTN_NOBTN], [BTN_CANCEL]])

    async def _s_bc_button(self, chat, m, d, now) -> None:
        txt = (m["text"] or "").strip()
        btn = None
        if txt != BTN_NOBTN:
            btn = A.parse_button(txt)
            if not btn:
                return await self._say(chat, "❌ قالب: متنِ دکمه | https://لینک (متن ≤۴۰ حرف). یا «بدونِ دکمه».")
        d = {**d, "button": list(btn) if btn else None}
        ids = await A.audience_ids(d["aud"], now)
        await self._go(chat, "bc_confirm", d)
        mins = max(1, round(len(ids) * 0.06 / 60))
        await self._say(chat, f"📋 خلاصه\nمخاطب: {A.AUDIENCE_LABEL[d['aud']]} — {len(ids)} نفر\n"
                              f"پیام: {d.get('descr')}\nدکمه: {btn[0] if btn else 'ندارد'}\n"
                              f"زمانِ تقریبی: ~{mins} دقیقه",
                        [[BTN_SEND], [BTN_TEST], [BTN_CANCEL]])

    async def _s_bc_confirm(self, chat, m, d, now) -> None:
        txt = (m["text"] or "").strip()
        if txt == BTN_TEST:
            ok = await self._copy_to_admin(d)
            return await self._say(chat, "🧪 نسخهٔ تست به تلگرامِ تو فرستاده شد." if ok else "❌ ارسالِ تست ناموفق بود.")
        if txt == BTN_SEND:
            return await self._ask_stepup(chat, {"a": "bc_send", "d": d, "label": f"ارسالِ همگانی به «{A.AUDIENCE_LABEL[d['aud']]}»"})
        await self._say(chat, "یکی از دکمه‌ها را بزن.")

    async def _copy_to_admin(self, d: dict) -> bool:
        from aiogram.types import InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
        kb = IM(inline_keyboard=[[IB(text=d["button"][0], url=d["button"][1])]]) if d.get("button") else None
        try:
            await self.tg.copy_message(chat_id=PLATFORM_ADMIN_ID, from_chat_id=PLATFORM_ADMIN_ID,
                                       message_id=d["src_msg"], reply_markup=kb)
            return True
        except Exception:
            logger.exception("sadmin: ارسالِ تست ناموفق")
            return False

    # --- step-up ---
    async def _ask_stepup(self, chat: str, pend: dict) -> None:
        await self._go(chat, "stepup", {"pend": pend})
        await self._say(chat, f"🔐 تأییدِ نهایی\n{pend.get('label', '')}\n\nبرایِ اجرا، دوباره کدِ دسترسی را بفرست "
                              "(یا «❌ لغو»).", [[BTN_CANCEL]])

    async def _stepup(self, chat: str, m: dict, d: dict, now: datetime.datetime) -> None:
        txt = (m["text"] or "").strip()
        if txt == BTN_CANCEL:
            return await self._menu(chat, "لغو شد.")
        lk = await locked_until(chat, now)
        if lk:
            await self._say(chat, f"⏳ موقتاً قفل است؛ بعد از {lk:%H:%M}.")
            return
        how = await verify_code(txt, now)
        await self.io.delete(chat, m.get("message_id"))
        if not how:
            n, cl, gl = await register_fail(chat, now)
            if cl or gl:
                await end_session(chat)
                await self._audit("⚠️ تلاشِ ناموفقِ تأییدِ کارِ حساس — نشست بسته شد.")
                return await self._say(chat, "⛔ تلاشِ بیش‌ازحد؛ نشست بسته و ورود قفل شد.", [])
            return await self._say(chat, f"❌ کدِ نادرست ({n}/{CHAT_FAILS})")
        await clear_fails(chat)
        res = await self._exec(chat, d["pend"])
        await self._menu(chat, res)

    async def _exec(self, chat: str, p: dict) -> str:
        a = p.get("a")
        if a == "del_bot":
            ok, msg = await A.delete_bot(p["bot"], p.get("reason", "-"), self.tg)
            await self._audit(f"حذفِ ربات #{p['bot']}: {'انجام شد' if ok else msg}")
            return ("✅ " if ok else "❌ ") + msg
        if a == "wallet":
            ok, msg = await A.adjust_wallet(p["tg"], int(p["delta"]), p.get("note", ""), self.tg)
            await self._audit(f"کیف پول کاربر {p['tg']}: {p['delta']:+,} — {'انجام شد' if ok else msg}")
            return ("✅ " if ok else "❌ ") + msg
        if a == "sys_backup":
            from platform_offsite_backup import run_backup
            ok, info = await run_backup(self.tg)
            await self._audit(f"بک‌آپِ بیرونی: {info}")
            return ("✅ بک‌آپ فرستاده شد: " if ok else "❌ ") + info
        if a == "bc_send":
            return await self._start_broadcast(chat, p["d"])
        return "کارِ ناشناخته."

    # --- اجرایِ ارسالِ همگانی ---
    async def _start_broadcast(self, chat: str, d: dict) -> str:
        cur = await _jget(K_BC)
        if cur.get("status") == "sending":
            return "❌ یک ارسالِ همگانی هنوز در حالِ اجراست؛ اول «توقف» را بزن یا صبر کن."
        ids = await A.audience_ids(d["aud"], self.now_fn())
        if not ids:
            return "❌ مخاطب خالی است."
        job = {"id": int(time.time()), "ids": ids, "idx": 0, "sent": 0, "failed": 0, "src_chat": PLATFORM_ADMIN_ID,
               "src_msg": d["src_msg"], "button": d.get("button"), "status": "sending", "chat": chat,
               "aud": A.AUDIENCE_LABEL[d["aud"]]}
        await _jset(K_BC, job)
        await _set(K_BC_STOP, "")
        await self._audit(f"شروعِ ارسالِ همگانی به «{job['aud']}» ({len(ids)} نفر)")
        self.bg.append(asyncio.create_task(self.run_job(job)))
        return f"🚀 ارسال شروع شد: {len(ids)} نفر. (پیشرفت اینجا اعلام می‌شود؛ «{BTN_STOPBC}» برایِ توقف)"

    async def run_job(self, job: dict) -> None:
        last = {"t": 0.0}

        async def save(j):
            await _jset(K_BC, j)

        async def stop():
            return (await _get(K_BC_STOP)) == "1"

        async def progress(j):
            if time.monotonic() - last["t"] >= 60:
                last["t"] = time.monotonic()
                await self._say(job["chat"], f"📣 پیشرفت: {j['idx']}/{len(j['ids'])} · موفق {j['sent']} · ناموفق {j['failed']}")

        try:
            job = await A.run_broadcast(job, self.tg, save, stop, progress)
        except Exception:
            logger.exception("sadmin: ارسالِ همگانی با خطا قطع شد")
            job["status"] = "interrupted"
            await _jset(K_BC, job)
        word = {"done": "✅ تمام شد", "cancelled": "🛑 متوقف شد", "interrupted": "⚠️ با خطا قطع شد"}.get(job["status"], job["status"])
        text = f"📣 ارسالِ همگانی {word}\nموفق: {job['sent']} · ناموفق: {job['failed']} · از {len(job['ids'])}"
        await self._say(job["chat"], text, MAIN_ROWS)
        await self._audit(text)

    async def resume_job(self) -> None:
        """بعد از ری‌استارتِ ورکر، ارسالِ نیمه‌کاره از همان‌جا ادامه می‌یابد."""
        job = await _jget(K_BC)
        if job.get("status") == "sending" and job.get("ids"):
            logger.info("sadmin: ازسرگیریِ ارسالِ همگانی از %s/%s", job.get("idx"), len(job["ids"]))
            self.bg.append(asyncio.create_task(self.run_job(job)))


# ─────────────── حلقهٔ polling ───────────────

_last_warn = float("-inf")


async def sadmin_loop(platform_bot) -> None:
    global _last_warn
    from platform_splus import api_call, parse_updates
    await asyncio.sleep(90)
    resumed = False
    timeouts = 0
    while True:
        try:
            token = (await _get(K_TOKEN)).strip()
            if not token or (await _get(K_EN, "0")) != "1":
                await asyncio.sleep(10)
                continue
            console = Console(SoroushIO(token), platform_bot)
            if not resumed:
                resumed = True
                await console.resume_job()
            off = (await _get(K_OFFSET)).strip()
            body = {"limit": 30, "timeout": POLL_LONG_SECS}
            if off.isdigit():
                body["offset"] = int(off)
            resp = await api_call(token, "getUpdates", body, timeout=POLL_LONG_SECS + 20)
            if not resp.get("ok"):
                timeouts += 1 if resp.get("error") == "تایم‌اوت" else 99
                if timeouts >= 6 and time.monotonic() - _last_warn > 3600 and PLATFORM_ADMIN_ID:
                    _last_warn = time.monotonic()
                    await platform_bot.send_message(PLATFORM_ADMIN_ID, "⚠️ اتصال به رباتِ پنلِ سروش برقرار نیست: "
                                                    + str(resp.get("error"))[:150])
                await asyncio.sleep(5 if resp.get("error") == "تایم‌اوت" else 15)
                continue
            timeouts = 0
            msgs, last = parse_updates(resp)
            for m in msgs:
                try:
                    await console.handle(m)
                except Exception:
                    logger.exception("sadmin: پردازشِ پیام ناموفق")
            if last is not None:
                await _set(K_OFFSET, str(last + 1))
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sadmin: خطای حلقه")
            await asyncio.sleep(15)


# ─────────────── تنظیمات در تلگرام: /sadmin ───────────────

class SadminFSM(StatesGroup):
    code = State()
    token = State()


def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    en = (await _get(K_EN, "0")) == "1"
    has_tok = bool((await _get(K_TOKEN)).strip())
    has_code = bool((await _get(K_HASH)).strip())
    otp = await _jget(K_OTP)
    otp_live = bool(otp.get("h") and (_parse(otp.get("x")) or datetime.datetime.min) > datetime.datetime.now())
    ev = (await _jget(K_LOG)).get("e", [])[-5:]
    logins = "\n".join(f"{e['t'][:16].replace('T', ' ')} · {e['k']} · {_html.escape(e['n'])}" for e in reversed(ev)) or "—"
    text = ("🛡 <b>پنلِ مدیریتِ سروش</b>\n\n"
            f"وضعیت: {'🟢 فعال' if en else '🔴 خاموش'}\n"
            f"رباتِ سروش: {'✅ ثبت شده' if has_tok else '⚠️ ثبت نشده'}\n"
            f"کدِ ثابت: {'✅ تنظیم شده' if has_code else '— ندارد'}"
            f" · کدِ یک‌بارمصرف: {'⏳ فعال' if otp_live else '—'}\n"
            f"مدتِ نشست: <b>{await _ttl_min()} دقیقه</b>\n\n"
            f"📋 آخرین رویدادها:\n<code>{logins}</code>\n\n"
            "ورود در سروش: رباتِ پنل را باز کن و کدِ دسترسی را بفرست. کارهایِ حساس دوباره کد می‌خواهند.\n"
            "⚠️ رباتِ پنل باید غیر از رباتِ پشتیبانیِ سروش باشد.")
    rows = [
        [InlineKeyboardButton(text="🔑 ثبتِ کدِ ثابت", callback_data="psdm:code", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="🎲 کدِ تصادفی", callback_data="psdm:rand")],
        [InlineKeyboardButton(text="🎟 کدِ یک‌بارمصرف (۵ دقیقه)", callback_data="psdm:otp")],
        [InlineKeyboardButton(text="🔌 ثبتِ توکنِ رباتِ پنل", callback_data="psdm:token")],
        [InlineKeyboardButton(text=("⛔ خاموش‌کردن" if en else "🟢 روشن‌کردن"), callback_data="psdm:toggle"),
         InlineKeyboardButton(text="⏱ مدتِ نشست", callback_data="psdm:ttl")],
        [InlineKeyboardButton(text="🚪 قطعِ همهٔ نشست‌ها", callback_data="psdm:kill", style=ButtonStyle.DANGER)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_sadmin_router.message(Command("sadmin"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def sd_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_sadmin_router.callback_query(F.data.startswith("psdm:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def sd_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    if act == "code":
        await state.set_state(SadminFSM.code)
        await cb.message.answer(f"کدِ دسترسی را بفرست (حداقل {MIN_CODE} حرف؛ پیامت پاک می‌شود و فقط هشِ آن ذخیره می‌شود).\nلغو: /sadmin")
        return await cb.answer()
    if act == "token":
        await state.set_state(SadminFSM.token)
        await cb.message.answer("توکنِ رباتِ پنلِ سروش را بفرست (غیر از رباتِ پشتیبانی). پیامت پاک می‌شود. لغو: /sadmin")
        return await cb.answer()
    if act == "rand":
        code = secrets.token_urlsafe(9)
        await _set(K_HASH, hash_code(code))
        await cb.message.answer(f"🎲 کدِ جدید (فقط همین یک‌بار نشان داده می‌شود؛ جایی امن ذخیره کن و این پیام را پاک کن):\n<code>{code}</code>",
                                parse_mode="HTML")
    elif act == "otp":
        code = "".join(secrets.choice("0123456789") for _ in range(8))
        await _jset(K_OTP, {"h": hash_code(code), "x": _iso(datetime.datetime.now() + datetime.timedelta(minutes=OTP_MINUTES))})
        await cb.message.answer(f"🎟 کدِ یک‌بارمصرف ({OTP_MINUTES} دقیقه، فقط یک ورود):\n<code>{code}</code>", parse_mode="HTML")
    elif act == "toggle":
        if (await _get(K_EN, "0")) != "1" and not (await _get(K_TOKEN)).strip():
            return await cb.answer("اول توکنِ ربات را ثبت کن.", show_alert=True)
        await _set(K_EN, "0" if (await _get(K_EN, "0")) == "1" else "1")
    elif act == "ttl":
        cur = await _ttl_min()
        await _set(K_TTL, str(TTL_CHOICES[(TTL_CHOICES.index(cur) + 1) % len(TTL_CHOICES)]))
    elif act == "kill":
        await _set(K_EPOCH, str(int(await _epoch()) + 1))
        await cb.answer("همهٔ نشست‌ها قطع شد.", show_alert=True)
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_sadmin_router.message(SadminFSM.code, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def sd_code_in(msg: Message, state: FSMContext):
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    try:
        await msg.delete()
    except Exception:
        logger.warning("sadmin: پاک‌کردنِ پیامِ کد ممکن نشد")
    if len(val) < MIN_CODE:
        return await msg.answer(f"❌ حداقل {MIN_CODE} حرف.")
    await _set(K_HASH, hash_code(val))
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ کدِ ثابت ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_sadmin_router.message(SadminFSM.token, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def sd_token_in(msg: Message, state: FSMContext):
    from platform_splus import api_call
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    try:
        await msg.delete()
    except Exception:
        logger.warning("sadmin: پاک‌کردنِ پیامِ توکن ممکن نشد")
    if not re.fullmatch(r"[A-Za-z0-9_\-:.]{10,200}", val):
        return await msg.answer("❌ توکن معتبر نیست.")
    if val == (await _get("splus_token")).strip():
        return await msg.answer("❌ این همان توکنِ رباتِ پشتیبانیِ سروش است؛ یک رباتِ جدا بساز (دو حلقه نباید یک توکن را بخوانند).")
    r = await api_call(val, "getMe", {}, timeout=15)
    if not (r.get("ok") and r.get("result")):
        return await msg.answer("❌ سروش این توکن را قبول نکرد: " + _html.escape(str(r.get("error"))[:150]))
    await _set(K_TOKEN, val)
    await _set(K_OFFSET, "")
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ توکن ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
