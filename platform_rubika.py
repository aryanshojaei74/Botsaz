# -*- coding: utf-8 -*-
"""
platform_rubika.py — پلِ «روبیکا → پی‌وی مدیرِ بات‌ساز» (PATCH_PLATFORM_RUBIKA_BRIDGE_V1).

مدیر یک ربات در روبیکا (از «بات فادر») می‌سازد و توکنش را با /rubika در رباتِ
اصلی ثبت می‌کند. هر پیامی که کسی به آن ربات بدهد، همین‌جا به پی‌ویِ مدیر
(PLATFORM_ADMIN_ID) می‌آید؛ مدیر هم اگر روی آن پیام «ریپلای» کند، جوابش در
روبیکا برای همان کاربر فرستاده می‌شود.

دریافتِ پیام با polling (getUpdates) انجام می‌شود، نه وب‌هوک: آدرسِ عمومی/SSL
نمی‌خواهد و با هر شکلِ پاسخِ پنل کار می‌کند. حلقه در worker_platform اجرا می‌شود.

کلیدهایِ PlatformSetting: rubika_token، rubika_enabled ("1"/"0")، rubika_offset.
جوابِ مدیر بدونِ ذخیرهٔ وضعیت کار می‌کند: شناسهٔ چتِ روبیکا داخلِ همان پیامِ
فورواردشده (خطِ «🆔 چت») است و از روی ریپلای خوانده می‌شود.
"""
from __future__ import annotations

import asyncio
import html as _html
import re
import time

import aiohttp
from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import PlatformSetting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_rubika_router = Router()

API_BASE = "https://botapi.rubika.ir/v3"
K_TOKEN, K_ENABLED, K_OFFSET = "rubika_token", "rubika_enabled", "rubika_offset"
POLL_IDLE_SECS = 2.0
POLL_ERR_SECS = 15.0
_MARK = "🆔 چت روبیکا:"
_CHAT_RE = re.compile(r"چت روبیکا:\s*([A-Za-z0-9_\-]+)")
_WARN_COOLDOWN = 3600.0
_last_warn = float('-inf')


class RubikaFSM(StatesGroup):
    token = State()


# ─────────────── تنظیمات ───────────────

async def _get(key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
    return row.value if row else default


async def _set(key: str, value: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if row:
            row.value = value
        else:
            s.add(PlatformSetting(key=key, value=value))
        await s.commit()


# ─────────────── کلاینتِ API ───────────────

async def api_call(token: str, method: str, body: dict | None = None, timeout: float = 25) -> dict:
    """یک تماسِ POST به API روبیکا. همیشه dict برمی‌گرداند؛ در خطا {"status": "ERR", "error": "..."}."""
    url = f"{API_BASE}/{token}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.post(url, json=body or {}) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    txt = (await r.text(errors="replace"))[:200]
                    return {"status": "ERR", "error": f"HTTP {r.status}: {txt}"}
                if isinstance(data, dict):
                    if r.status != 200 and data.get("status") in (None, "OK"):
                        data["status"] = "ERR"
                        data.setdefault("error", f"HTTP {r.status}")
                    return data
                return {"status": "ERR", "error": "پاسخِ نامعتبر از روبیکا"}
    except asyncio.TimeoutError:
        return {"status": "ERR", "error": "تایم‌اوت"}
    except Exception as e:
        return {"status": "ERR", "error": f"{type(e).__name__}: {e}"}


def _item(chat_id, nm: dict, kind: str) -> dict:
    aux = nm.get("aux_data") if isinstance(nm.get("aux_data"), dict) else {}
    cm = nm.get("contact_message") if isinstance(nm.get("contact_message"), dict) else {}
    return {"chat_id": str(chat_id or ""), "sender_id": str(nm.get("sender_id") or ""),
            "text": str(nm.get("text") or ""), "message_id": str(nm.get("message_id") or ""),
            "kind": kind, "button_id": str(aux.get("button_id") or ""),
            "phone": str(cm.get("phone_number") or ""),
            "first_name": str(cm.get("first_name") or ""), "last_name": str(cm.get("last_name") or "")}


def parse_updates(resp: dict) -> tuple[list[dict], str | None]:
    """پاسخِ getUpdates را به (فهرستِ پیام‌ها، next_offset_id) تبدیل می‌کند.

    هر مورد: {"chat_id","sender_id","text","message_id","kind","button_id","phone",...}
    kind: text | file | contact (کاربر شمارهٔ خودش را داد) | start (استارتِ ربات) | other.
    پیام‌هایِ خودِ ربات و رویدادهایِ دیگر (توقف…) رد می‌شوند.
    ساختارِ مستندشده: data.updates[].{type, chat_id, new_message{...}} و
    next_offset_id؛ شکلِ قدیمیِ inline_message هم پشتیبانی می‌شود."""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    nxt = data.get("next_offset_id")
    out: list[dict] = []
    for u in data.get("updates") or []:
        if not isinstance(u, dict):
            continue
        if isinstance(u.get("inline_message"), dict):
            m = u["inline_message"]
            out.append(_item(m.get("chat_id"), m, "text"))
            continue
        typ = str(u.get("type") or "")
        if typ == "StartedBot":
            out.append(_item(u.get("chat_id"), {}, "start"))
            continue
        nm = u.get("new_message") if isinstance(u.get("new_message"), dict) else None
        if typ not in ("NewMessage", "") or nm is None:
            continue
        if str(nm.get("sender_type") or "").lower() == "bot":
            continue
        if isinstance(nm.get("contact_message"), dict) and nm["contact_message"].get("phone_number"):
            kind = "contact"
        elif nm.get("text"):
            kind = "text"
        else:
            kind = "file" if (nm.get("file") or nm.get("file_inline")) else "other"
        out.append(_item(u.get("chat_id") or nm.get("chat_id"), nm, kind))
    return [x for x in out if x["chat_id"]], (str(nxt) if nxt not in (None, "") else None)


def format_admin_text(m: dict, prof: dict | None = None) -> str:
    """پیامِ مدیر: مشخصاتِ فرستنده (نام/یوزرنیم/شماره) + متنِ پیام. prof از get_profile می‌آید."""
    prof = prof or {}
    if m["kind"] == "contact":
        body = "📱 <i>شمارهٔ خودش را به اشتراک گذاشت.</i>"
    elif m["text"]:
        body = _html.escape(m["text"])
    elif m["kind"] == "file":
        body = "📎 <i>فایل/رسانه (فقط متن پشتیبانی می‌شود؛ در خودِ روبیکا ببین)</i>"
    else:
        body = "<i>پیامِ بدونِ متن</i>"
    name = " ".join(x for x in (prof.get("first_name"), prof.get("last_name")) if x) or "—"
    lines = ["📩 <b>پیام تازه از روبیکا</b>",
             f"👤 نام: {_html.escape(name)}"]
    if prof.get("username"):
        lines.append(f"🔗 یوزرنیم: @{_html.escape(str(prof['username']).lstrip('@'))}")
    lines.append(f"📱 شماره: <code>{_html.escape(prof['phone'])}</code>" if prof.get("phone")
                 else "📱 شماره: <i>ثبت نشده (کاربر هنوز نفرستاده)</i>")
    lines.append(f"🆔 شناسهٔ کاربر: <code>{_html.escape(prof.get('user_id') or m['sender_id'] or '—')}</code>")
    lines.append(f"{_MARK} <code>{_html.escape(m['chat_id'])}</code>")
    lines += ["", body, "", "<i>برای جواب‌دادن روی همین پیام ریپلای کن.</i>"]
    return "\n".join(lines)


def chat_id_from_text(text: str | None) -> str | None:
    m = _CHAT_RE.search(text or "")
    return m.group(1) if m else None


# ─────────────── حلقهٔ polling ───────────────

async def _warn_admin(bot, text: str) -> None:
    global _last_warn
    now = time.monotonic()
    if now - _last_warn < _WARN_COOLDOWN or not PLATFORM_ADMIN_ID:
        return
    _last_warn = now
    try:
        await bot.send_message(PLATFORM_ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        logger.exception("rubika: هشدار به مدیر ارسال نشد")


BTN_CONTACT = "💬 پیام به ادمین"
BTN_PHONE = "📱 ارسالِ شمارهٔ من"
WELCOME = ("سلام 👋\nبرای ارتباط با ادمین دکمهٔ «" + BTN_CONTACT + "» را بزن و پیامت را بنویس.\n"
           "اگر خواستی با دکمهٔ «" + BTN_PHONE + "» شماره‌ات را هم بفرست تا ادمین بشناسدت.")
ASK_MSG = "✍️ پیامت را همین‌جا بنویس؛ مستقیم برای ادمین می‌رسد و جوابش را همین‌جا می‌گیری."
GOT_PHONE = "✅ شمارهٔ تو ثبت شد و برای ادمین فرستاده شد."
GOT_MSG = "✅ پیامت به ادمین رسید."

_PROF_TTL = 3600.0
_prof_cache: dict[str, tuple[float, dict]] = {}


def _keypad() -> dict:
    return {"rows": [
        {"buttons": [{"id": "contact", "type": "Simple", "button_text": BTN_CONTACT}]},
        {"buttons": [{"id": "phone", "type": "AskMyPhoneNumber", "button_text": BTN_PHONE}]},
    ], "resize_keyboard": True, "on_time_keyboard": False}


async def _send_user(token: str, chat_id: str, text: str, keypad: bool = False) -> None:
    """به کاربرِ روبیکا پیام می‌دهد؛ اگر پنل کیبورد را نپذیرفت، بدونِ کیبورد دوباره می‌فرستد."""
    if keypad:
        r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                                    "chat_keypad": _keypad(), "chat_keypad_type": "New"})
        if r.get("status") == "OK":
            return
        logger.warning("rubika: ارسال با کیبورد رد شد، بدونِ کیبورد می‌فرستم: %s", r.get("error") or r.get("status"))
    await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


async def get_profile(token: str, chat_id: str) -> dict:
    """نام/یوزرنیم/شناسهٔ کاربر از getChat (کش یک‌ساعته) + شمارهٔ ثبت‌شده. در خطا فقط شماره."""
    now = time.monotonic()
    hit = _prof_cache.get(chat_id)
    if hit and now - hit[0] < _PROF_TTL:
        prof = dict(hit[1])
    else:
        prof = {}
        r = await api_call(token, "getChat", {"chat_id": chat_id}, timeout=15)
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        c = d.get("chat") if isinstance(d.get("chat"), dict) else {}
        if r.get("status") == "OK" and c:
            prof = {"first_name": str(c.get("first_name") or ""), "last_name": str(c.get("last_name") or ""),
                    "username": str(c.get("username") or ""), "user_id": str(c.get("user_id") or "")}
            _prof_cache[chat_id] = (now, prof)
    phone = (await _get(f"rubika_phone:{chat_id}")).strip()
    if phone:
        prof["phone"] = phone
    return prof


async def _handle(bot, token: str, m: dict) -> int:
    """یک پیام را پردازش می‌کند؛ ۱ اگر به مدیر فرستاده شد."""
    chat_id, txt = m["chat_id"], m["text"].strip()
    if m["kind"] == "start" or txt.lower() in ("/start", "start", "شروع"):
        await _send_user(token, chat_id, WELCOME, keypad=True)
        return 0
    if m["button_id"] == "contact" or txt == BTN_CONTACT:
        await _send_user(token, chat_id, ASK_MSG, keypad=True)
        return 0
    if m["kind"] == "contact":
        await _set(f"rubika_phone:{chat_id}", m["phone"])
        prof = await get_profile(token, chat_id)
        for k, v in (("first_name", m["first_name"]), ("last_name", m["last_name"])):
            if v and not prof.get(k):
                prof[k] = v
        await bot.send_message(PLATFORM_ADMIN_ID, format_admin_text(m, prof), parse_mode="HTML")
        await _send_user(token, chat_id, GOT_PHONE)
        return 1
    if txt == BTN_PHONE or m["kind"] == "other":
        return 0
    prof = await get_profile(token, chat_id)
    await bot.send_message(PLATFORM_ADMIN_ID, format_admin_text(m, prof), parse_mode="HTML")
    await _send_user(token, chat_id, GOT_MSG)
    return 1


async def poll_once(bot) -> int:
    """یک دور: پیام‌هایِ تازه را بگیر و پردازش کن. تعدادِ پیام‌هایِ فورواردشده به مدیر را برمی‌گرداند
    (-1 یعنی خطا، 0 یعنی ربات خاموش/بدونِ توکن/بدونِ پیامِ تازه)."""
    token = (await _get(K_TOKEN)).strip()
    if not token or (await _get(K_ENABLED, "1")) != "1" or not PLATFORM_ADMIN_ID:
        return 0
    offset = (await _get(K_OFFSET)).strip()
    body: dict = {"limit": 50}
    if offset:
        body["offset_id"] = offset
    resp = await api_call(token, "getUpdates", body)
    if resp.get("status") != "OK" and "data" not in resp:
        err = str(resp.get("error") or resp.get("status") or resp)[:200]
        logger.warning("rubika: getUpdates ناموفق: %s", err)
        await _warn_admin(bot, "⚠️ <b>اتصال به روبیکا برقرار نیست</b>\n"
                               f"علت: <code>{_html.escape(err)}</code>\n"
                               "توکن را با /rubika بررسی کن. این هشدار حداکثر ساعتی یک‌بار می‌آید.")
        return -1
    msgs, nxt = parse_updates(resp)
    sent = 0
    for m in msgs:
        try:
            sent += await _handle(bot, token, m)
        except Exception:
            logger.exception("rubika: پردازشِ پیام ناموفق")
    if nxt and nxt != offset:
        await _set(K_OFFSET, nxt)     # بعد از پردازش، تا پیامی گم نشود (در بدترین حالت یک‌بار تکراری می‌آید)
    return sent


async def rubika_poll_loop(platform_bot) -> None:
    while True:
        try:
            n = await poll_once(platform_bot)
            await asyncio.sleep(POLL_ERR_SECS if n < 0 else POLL_IDLE_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rubika: خطای حلقه")
            await asyncio.sleep(POLL_ERR_SECS)


# ─────────────── پنلِ مدیر: /rubika ───────────────

def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    tok = (await _get(K_TOKEN)).strip()
    on = (await _get(K_ENABLED, "1")) == "1"
    state = "ثبت نشده" if not tok else ("🟢 فعال" if on else "🔴 خاموش")
    text = ("📨 <b>پلِ روبیکا</b>\n\n"
            f"وضعیت: {state}\n\n"
            "پیام‌هایی که به رباتِ روبیکایِ تو داده می‌شود به همین پی‌وی می‌آید؛ "
            "روی هر پیام ریپلای کنی، جوابت در روبیکا فرستاده می‌شود.")
    rows = [[InlineKeyboardButton(text="🔑 ثبت/تغییرِ توکن", callback_data="rbk:token", style=ButtonStyle.PRIMARY)]]
    if tok:
        rows.append([InlineKeyboardButton(text="🧪 تست اتصال", callback_data="rbk:test"),
                     InlineKeyboardButton(text="🔴 خاموش" if on else "🟢 روشن", callback_data="rbk:toggle")])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ توکن", callback_data="rbk:del", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_rubika_router.message(Command("rubika"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def rbk_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_rubika_router.callback_query(F.data.startswith("rbk:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def rbk_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    if act == "token":
        await state.set_state(RubikaFSM.token)
        await cb.message.answer("توکنِ رباتِ روبیکا را بفرست (از «بات فادر»).\n"
                                "پیامِ توکن بعد از ثبت پاک می‌شود. برای انصراف /rubika بزن.")
        return await cb.answer()
    if act == "toggle":
        await _set(K_ENABLED, "0" if (await _get(K_ENABLED, "1")) == "1" else "1")
    elif act == "del":
        await _set(K_TOKEN, "")
        await _set(K_OFFSET, "")
    elif act == "test":
        r = await api_call((await _get(K_TOKEN)).strip(), "getMe", {}, timeout=15)
        bot = (r.get("data") or {}).get("bot") if isinstance(r.get("data"), dict) else None
        if r.get("status") == "OK" and isinstance(bot, dict):
            name = bot.get("bot_title") or bot.get("username") or bot.get("bot_id") or "ربات"
            await cb.answer(f"✅ وصل است: {name}", show_alert=True)
        else:
            await cb.answer("❌ " + str(r.get("error") or r.get("status") or "توکن نامعتبر")[:150], show_alert=True)
        return
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_rubika_router.message(RubikaFSM.token, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def rbk_token(msg: Message, state: FSMContext):
    tok = (msg.text or "").strip()
    try:
        await msg.delete()          # توکن در چت نماند
    except Exception:
        pass
    if tok.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_\-:.]{10,200}", tok):
        await msg.answer("❌ توکن معتبر نیست؛ دوباره بفرست یا /rubika بزن.")
        return
    r = await api_call(tok, "getMe", {}, timeout=15)
    if r.get("status") != "OK":
        await msg.answer("❌ روبیکا این توکن را قبول نکرد: " + _html.escape(str(r.get("error") or r.get("status"))[:150]))
        return
    await state.clear()
    await _set(K_TOKEN, tok)
    await _set(K_OFFSET, "")
    await _set(K_ENABLED, "1")
    text, kb = await _panel()
    await msg.answer("✅ توکن ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


def _is_rubika_reply(m: Message) -> bool:
    r = m.reply_to_message
    return bool(_is_admin(getattr(m.from_user, "id", None)) and r and (r.text or r.caption)
                and chat_id_from_text(r.text or r.caption) and m.text and not m.text.startswith("/"))


@platform_rubika_router.message(F.func(_is_rubika_reply))
async def rbk_reply(msg: Message):
    chat_id = chat_id_from_text(msg.reply_to_message.text or msg.reply_to_message.caption)
    token = (await _get(K_TOKEN)).strip()
    if not token:
        return await msg.reply("❌ توکنِ روبیکا ثبت نیست (/rubika).")
    r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": msg.text})
    if r.get("status") == "OK":
        await msg.reply("✅ در روبیکا ارسال شد.")
    else:
        await msg.reply("❌ ارسال نشد: " + _html.escape(str(r.get("error") or r.get("status"))[:150]))
