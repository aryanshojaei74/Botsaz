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

from bridge_alerts import OutageGate, short_reason     # PATCH_BRIDGE_ALERT_QUIET_V1
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
    fl = nm.get("file") if isinstance(nm.get("file"), dict) else (
        nm.get("file_inline") if isinstance(nm.get("file_inline"), dict) else {})
    return {"chat_id": str(chat_id or ""), "sender_id": str(nm.get("sender_id") or ""),
            "text": str(nm.get("text") or ""), "message_id": str(nm.get("message_id") or ""),
            "kind": kind, "button_id": str(aux.get("button_id") or ""),
            "file_id": str(fl.get("file_id") or ""), "file_name": str(fl.get("file_name") or ""),
            "file_size": int(fl.get("size") or fl.get("file_size") or 0) if str(fl.get("size") or fl.get("file_size") or 0).isdigit() else 0,
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
        elif nm.get("file") or nm.get("file_inline"):
            kind = "file"          # متنِ همراهش (اگر باشد) کپشن است
        elif nm.get("text"):
            kind = "text"
        else:
            kind = "other"
        out.append(_item(u.get("chat_id") or nm.get("chat_id"), nm, kind))
    return [x for x in out if x["chat_id"]], (str(nxt) if nxt not in (None, "") else None)


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


_PROF_TTL = 3600.0
_prof_cache: dict[str, tuple[float, dict]] = {}


def _build_keypad(menu: list[list[str]]) -> dict:
    """منوی دکمه‌ای (ردیف‌هایی از برچسب) → chat_keypad روبیکا. دکمهٔ «ارسالِ شماره» از نوعِ AskMyPhoneNumber است."""
    from platform_msg_tickets import BTN_PHONE
    rows = []
    for i, row in enumerate(menu):
        rows.append({"buttons": [{"id": f"b{i}_{j}", "type": "AskMyPhoneNumber" if lab == BTN_PHONE else "Simple",
                                  "button_text": lab} for j, lab in enumerate(row)]})
    return {"rows": rows, "resize_keyboard": True, "on_time_keyboard": False}


async def _send_user(token: str, chat_id: str, text: str, keypad: dict | None = None) -> None:
    """به کاربرِ روبیکا پیام می‌دهد؛ اگر پنل کیبورد را نپذیرفت، بدونِ کیبورد دوباره می‌فرستد."""
    if keypad:
        r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                                    "chat_keypad": keypad, "chat_keypad_type": "New"})
        if r.get("status") == "OK":
            return
        logger.warning("rubika: ارسال با کیبورد رد شد، بدونِ کیبورد می‌فرستم: %s", r.get("error") or r.get("status"))
    await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


_bg_tasks: set = set()
ACK_DELETE_SECS = 3.0
MAX_FILE_BYTES = 45 * 1024 * 1024
_IMG = (".jpg", ".jpeg", ".png", ".webp")
_VID = (".mp4", ".mov", ".mkv", ".webm")
_AUD = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")


async def _delete_later(token: str, chat_id: str, message_id: str, secs: float = ACK_DELETE_SECS) -> None:
    await asyncio.sleep(secs)
    await api_call(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)


async def _ack(token: str, chat_id: str, text: str) -> None:
    """پیامِ تأیید به کاربر؛ بعد از چند ثانیه پاک می‌شود تا چتِ روبیکا شلوغ نشود."""
    r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    mid = d.get("message_id")
    if r.get("status") == "OK" and mid:
        t = asyncio.create_task(_delete_later(token, chat_id, str(mid)))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


async def download_file(token: str, file_id: str) -> bytes | None:
    """فایلِ روبیکا را با getFile → download_url می‌گیرد؛ بزرگ‌تر از سقفِ تلگرام یا خطا ⇒ None."""
    r = await api_call(token, "getFile", {"file_id": file_id}, timeout=20)
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    url = d.get("download_url") or d.get("url")
    if r.get("status") != "OK" or not url:
        logger.warning("rubika: getFile ناموفق: %s", r.get("error") or r.get("status"))
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
            async with http.get(url) as resp:
                if resp.status != 200 or int(resp.headers.get("Content-Length") or 0) > MAX_FILE_BYTES:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    buf.extend(chunk)
                    if len(buf) > MAX_FILE_BYTES:
                        return None
                return bytes(buf)
    except Exception:
        logger.exception("rubika: دانلودِ فایل ناموفق")
        return None


async def upload_file(token: str, data: bytes, name: str, ftype: str) -> str | None:
    """requestSendFile → آپلود → file_id (برایِ جوابِ مدیر با فایل)."""
    r = await api_call(token, "requestSendFile", {"type": ftype}, timeout=20)
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    url = d.get("upload_url")
    if r.get("status") != "OK" or not url:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("file", data, filename=name)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
            async with http.post(url, data=form) as resp:
                j = await resp.json(content_type=None)
        dd = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else (j if isinstance(j, dict) else {})
        return str(dd.get("file_id") or "") or None
    except Exception:
        logger.exception("rubika: آپلودِ فایل ناموفق")
        return None


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


def _ftype_of(name: str) -> str:
    low = (name or "").lower()
    if low.endswith(_IMG):
        return "photo"
    if low.endswith(_VID):
        return "video"
    if low.endswith(_AUD):
        return "audio"
    return "document"


class RubikaChannel:
    """آدابتورِ روبیکا برایِ سیستمِ تیکت (platform_msg_tickets.Channel)."""
    key = "rubika"
    label = "روبیکا"

    def __init__(self, token: str):
        self.token = token

    async def send(self, chat_id: str, text: str, menu=None) -> None:
        await _send_user(self.token, chat_id, text, _build_keypad(menu) if menu else None)

    async def ack(self, chat_id: str, text: str) -> None:
        await _ack(self.token, chat_id, text)

    async def download(self, file_id: str):
        return await download_file(self.token, file_id)

    async def send_file(self, chat_id: str, data: bytes, name: str, ftype: str, caption: str = "") -> bool:
        kind = {"photo": "Image", "video": "Video", "voice": "Voice", "audio": "Music"}.get(ftype, "File")
        rid = await upload_file(self.token, data, name, kind)
        if not rid:
            return False
        body = {"chat_id": chat_id, "file_id": rid}
        if caption:
            body["text"] = caption
        return (await api_call(self.token, "sendFile", body)).get("status") == "OK"


async def make_channel() -> "RubikaChannel | None":
    token = (await _get(K_TOKEN)).strip()
    return RubikaChannel(token) if token else None


async def _handle(bot, token: str, m: dict) -> int:
    """رویدادِ روبیکا را به سیستمِ تیکت می‌سپارد؛ ۱ اگر چیزی به مدیر فرستاده شد."""
    from platform_msg_tickets import handle_user
    prof = {} if m["kind"] == "start" else await get_profile(token, m["chat_id"])
    name = " ".join(x for x in (prof.get("first_name") or m.get("first_name"),
                                prof.get("last_name") or m.get("last_name")) if x)
    ev = {"chat_id": m["chat_id"], "user_id": prof.get("user_id") or m["sender_id"], "name": name,
          "username": prof.get("username", ""), "text": m["text"], "kind": m["kind"], "phone": m["phone"],
          "file_id": m["file_id"], "file_name": m["file_name"], "file_type": _ftype_of(m["file_name"])}
    return await handle_user(bot, RubikaChannel(token), ev)


_gate = OutageGate()


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
        logger.warning("rubika: getUpdates ناموفق: %s", short_reason(err))
        if _gate.failed():        # فقط قطعیِ پیوسته (≥ ۴۵ دقیقه)، و حداکثر هر ۱۲ ساعت یک‌بار
            await _warn_admin(bot, "⚠️ <b>اتصال به روبیکا بیش از ۴۵ دقیقه است برقرار نیست</b>\n"
                                   f"علت: <code>{_html.escape(short_reason(err))}</code>\n"
                                   "توکن را با /rubika بررسی کن.")
        return -1
    _gate.ok()
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


def _is_rubika_media_reply(m: Message) -> bool:
    r = m.reply_to_message
    return bool(_is_admin(getattr(m.from_user, "id", None)) and r and (r.text or r.caption)
                and chat_id_from_text(r.text or r.caption) and (m.photo or m.document or m.video or m.voice or m.audio))


@platform_rubika_router.message(F.func(_is_rubika_media_reply))
async def rbk_media_reply(msg: Message):
    """جوابِ مدیر با عکس/فایل/ویدیو/ویس ⇒ برایِ کاربرِ روبیکا (با کپشنِ مدیر)."""
    chat_id = chat_id_from_text(msg.reply_to_message.text or msg.reply_to_message.caption)
    token = (await _get(K_TOKEN)).strip()
    if not token:
        return await msg.reply("❌ توکنِ روبیکا ثبت نیست (/rubika).")
    if msg.photo:
        fid, name, ftype = msg.photo[-1].file_id, "photo.jpg", "Image"
    elif msg.video:
        fid, name, ftype = msg.video.file_id, msg.video.file_name or "video.mp4", "Video"
    elif msg.voice:
        fid, name, ftype = msg.voice.file_id, "voice.ogg", "Voice"
    elif msg.audio:
        fid, name, ftype = msg.audio.file_id, msg.audio.file_name or "audio.mp3", "Music"
    else:
        fid, name, ftype = msg.document.file_id, msg.document.file_name or "file", "File"
    try:
        buf = await msg.bot.download(fid)
        data = buf.read() if buf else b""
    except Exception:
        data = b""
    if not data:
        return await msg.reply("❌ فایل از تلگرام دریافت نشد (شاید بزرگ‌تر از ۲۰ مگابایت است).")
    rid = await upload_file(token, data, name, ftype)
    if not rid:
        return await msg.reply("❌ آپلود در روبیکا ناموفق بود.")
    body = {"chat_id": chat_id, "file_id": rid}
    if msg.caption:
        body["text"] = msg.caption
    r = await api_call(token, "sendFile", body)
    await msg.reply("✅ فایل در روبیکا ارسال شد." if r.get("status") == "OK"
                    else "❌ ارسال نشد: " + _html.escape(str(r.get("error") or r.get("status"))[:150]))


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
