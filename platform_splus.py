# -*- coding: utf-8 -*-
"""
platform_splus.py — پلِ «سروش پلاس → پی‌وی مدیرِ بات‌ساز» (PATCH_PLATFORM_SPLUS_BRIDGE_V1).

همان کارِ platform_rubika.py برایِ پیام‌رسانِ سروش پلاس: مدیر ربات را از «بات فادر»
سروش (splus.ir/botfather) می‌سازد، توکن را با /splus ثبت می‌کند؛ پیام‌هایِ کاربران
(متن/عکس/فایل/ویدیو/ویس) با مشخصاتشان به پی‌ویِ مدیر می‌آید و ریپلایِ مدیر برایِ
کاربر فرستاده می‌شود.

API سروش پلاس شبیهِ Bot API تلگرام است: https://api.splus.ir/bot<TOKEN>/<method>،
پاسخ {"ok":..,"result":..}، getUpdates با offset، پیام شاملِ from/chat/text/contact/photo/
document. دریافت با polling (بدونِ وب‌هوک). شناسهٔ چتِ سروش داخلِ خطِ «🆔 چت سروش» است
تا ریپلایِ مدیر بدونِ ذخیرهٔ وضعیت کار کند.

کلیدهایِ PlatformSetting: splus_token، splus_enabled، splus_offset، splus_phone:<chat_id>.
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

from config import PLATFORM_ADMIN_ID
from platform_rubika import _get, _set          # ذخیرهٔ کلید/مقدارِ پلتفرم (مشترک)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_splus_router = Router()

API_BASE = "https://api.splus.ir/bot"
FILE_BASES = ("https://api.splus.ir/file/bot", "https://api.splus.ir/bot")
K_TOKEN, K_ENABLED, K_OFFSET = "splus_token", "splus_enabled", "splus_offset"
POLL_IDLE_SECS = 2.0
POLL_ERR_SECS = 15.0
ACK_DELETE_SECS = 3.0
MAX_FILE_BYTES = 45 * 1024 * 1024
_MARK = "🆔 چت سروش:"
_CHAT_RE = re.compile(r"چت سروش:\s*(-?\d+)")
_WARN_COOLDOWN = 3600.0
_last_warn = float("-inf")
_bg_tasks: set = set()
POLL_LONG_SECS = 10
TIMEOUT_WARN_AFTER = 6      # تایم‌اوتِ پشتِ‌سرهم تا قبلِ هشدار به مدیر
_timeouts = 0

class SplusFSM(StatesGroup):
    token = State()


# ─────────────── کلاینتِ API ───────────────

async def api_call(token: str, method: str, body: dict | None = None, timeout: float = 25,
                   form: aiohttp.FormData | None = None) -> dict:
    """تماسِ POST به API سروش. همیشه dict: {"ok": True, "result": ...} یا {"ok": False, "error": "..."}."""
    url = f"{API_BASE}{token}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            kw = {"data": form} if form is not None else {"json": body or {}}
            async with http.post(url, **kw) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    return {"ok": False, "error": f"HTTP {r.status}: {(await r.text(errors='replace'))[:200]}"}
                if not isinstance(data, dict):
                    return {"ok": False, "error": "پاسخِ نامعتبر از سروش"}
                if not data.get("ok"):
                    data["error"] = str(data.get("description") or data.get("error") or f"HTTP {r.status}")
                return data
    except asyncio.TimeoutError:
        return {"ok": False, "error": "تایم‌اوت"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─────────────── پارسِ آپدیت ───────────────

def _largest_photo(photos) -> dict:
    ph = [p for p in (photos or []) if isinstance(p, dict) and p.get("file_id")]
    return max(ph, key=lambda p: int(p.get("file_size") or 0) or int(p.get("width") or 0)) if ph else {}


def parse_updates(resp: dict) -> tuple[list[dict], int | None]:
    """(پیام‌ها، آخرین update_id). فقط پیامِ کاربر در چتِ خصوصی؛ بقیه رد می‌شوند.

    هر مورد: chat_id, user_id, first_name, last_name, username, text, kind
    (text|file|contact|other)، و برایِ فایل: file_id, file_name, file_type(photo|video|voice|audio|document)،
    برایِ شماره: phone."""
    last = None
    out: list[dict] = []
    for u in resp.get("result") or []:
        if not isinstance(u, dict):
            continue
        if isinstance(u.get("update_id"), int):
            last = max(last or 0, u["update_id"])
        m = u.get("message")
        if not isinstance(m, dict):
            continue
        chat = m.get("chat") if isinstance(m.get("chat"), dict) else {}
        frm = m.get("from") if isinstance(m.get("from"), dict) else {}
        if chat.get("id") is None or str(chat.get("type") or "private") != "private" or frm.get("is_bot"):
            continue
        item = {"chat_id": str(chat["id"]), "user_id": str(frm.get("id") or chat["id"]),
                "first_name": str(frm.get("first_name") or chat.get("first_name") or ""),
                "last_name": str(frm.get("last_name") or chat.get("last_name") or ""),
                "username": str(frm.get("username") or chat.get("username") or ""),
                "text": str(m.get("text") or m.get("caption") or ""), "kind": "other",
                "file_id": "", "file_name": "", "file_type": "", "phone": "",
                "message_id": m.get("message_id")}
        c = m.get("contact") if isinstance(m.get("contact"), dict) else None
        if c and c.get("phone_number") and str(c.get("user_id") or item["user_id"]) == item["user_id"]:
            item.update(kind="contact", phone=str(c["phone_number"]))
        elif m.get("photo"):
            p = _largest_photo(m["photo"])
            item.update(kind="file", file_id=str(p.get("file_id") or ""), file_type="photo", file_name="photo.jpg")
        else:
            for key, ftype in (("document", "document"), ("video", "video"), ("voice", "voice"),
                               ("audio", "audio"), ("animation", "document"), ("video_note", "video")):
                f = m.get(key)
                if isinstance(f, dict) and f.get("file_id"):
                    item.update(kind="file", file_id=str(f["file_id"]), file_type=ftype,
                                file_name=str(f.get("file_name") or key))
                    break
            else:
                if item["text"]:
                    item["kind"] = "text"
        out.append(item)
    return out, last


def chat_id_from_text(text: str | None) -> str | None:
    m = _CHAT_RE.search(text or "")
    return m.group(1) if m else None


# ─────────────── ارسال به کاربر / فایل ───────────────

def _build_keypad(menu: list[list[str]]) -> dict:
    """منوی دکمه‌ای (ردیف‌هایی از برچسب) → ReplyKeyboard سروش؛ دکمهٔ «ارسالِ شماره» request_contact دارد."""
    from platform_msg_tickets import BTN_PHONE
    return {"keyboard": [[({"text": lab, "request_contact": True} if lab == BTN_PHONE else {"text": lab})
                          for lab in row] for row in menu],
            "resize_keyboard": True, "is_persistent": True}


async def _send_user(token: str, chat_id: str, text: str, keypad: dict | None = None) -> dict:
    body = {"chat_id": chat_id, "text": text}
    if keypad:
        body["reply_markup"] = keypad
        r = await api_call(token, "sendMessage", body)
        if r.get("ok"):
            return r
        logger.warning("splus: ارسال با کیبورد رد شد، بدونِ کیبورد می‌فرستم: %s", r.get("error"))
        body.pop("reply_markup")
    return await api_call(token, "sendMessage", body)


async def _delete_later(token: str, chat_id: str, message_id, secs: float = ACK_DELETE_SECS) -> None:
    await asyncio.sleep(secs)
    await api_call(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)


async def _ack(token: str, chat_id: str, text: str) -> None:
    """پیامِ تأیید؛ بعد از چند ثانیه پاک می‌شود تا چت شلوغ نشود."""
    r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    if r.get("ok") and res.get("message_id"):
        t = asyncio.create_task(_delete_later(token, chat_id, res["message_id"]))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


async def download_file(token: str, file_id: str) -> bytes | None:
    """getFile → file_path → دانلود (الگویِ تلگرام: /file/bot<TOKEN>/<path>)؛ بزرگ یا خطا ⇒ None."""
    r = await api_call(token, "getFile", {"file_id": file_id}, timeout=20)
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    path = str(res.get("file_path") or "")
    if not r.get("ok") or not path:
        logger.warning("splus: getFile ناموفق: %s", r.get("error"))
        return None
    urls = [path] if path.startswith("http") else [f"{b}{token}/{path.lstrip('/')}" for b in FILE_BASES]
    for url in urls:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
                async with http.get(url) as resp:
                    if resp.status != 200 or int(resp.headers.get("Content-Length") or 0) > MAX_FILE_BYTES:
                        continue
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        buf.extend(chunk)
                        if len(buf) > MAX_FILE_BYTES:
                            return None
                    return bytes(buf) if buf else None
        except Exception:
            logger.exception("splus: دانلودِ فایل ناموفق (%s)", url.split(token)[0])
    return None


# ─────────────── پردازش و حلقه ───────────────

class SplusChannel:
    """آدابتورِ سروش پلاس برایِ سیستمِ تیکت (platform_msg_tickets.Channel)."""
    key = "splus"
    label = "سروش پلاس"

    def __init__(self, token: str):
        self.token = token

    async def send(self, chat_id: str, text: str, menu=None) -> None:
        await _send_user(self.token, chat_id, text, _build_keypad(menu) if menu else None)

    async def ack(self, chat_id: str, text: str) -> None:
        await _ack(self.token, chat_id, text)

    async def download(self, file_id: str):
        return await download_file(self.token, file_id)

    async def send_file(self, chat_id: str, data: bytes, name: str, ftype: str, caption: str = "") -> bool:
        method, field = {"photo": ("sendPhoto", "photo"), "video": ("sendVideo", "video"),
                         "voice": ("sendVoice", "voice"), "audio": ("sendAudio", "audio")}.get(
            ftype, ("sendDocument", "document"))
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field(field, data, filename=name)
        if caption:
            form.add_field("caption", caption)
        return bool((await api_call(self.token, method, timeout=120, form=form)).get("ok"))


async def make_channel() -> "SplusChannel | None":
    token = (await _get(K_TOKEN)).strip()
    return SplusChannel(token) if token else None


async def _handle(bot, token: str, m: dict) -> int:
    """رویدادِ سروش را به سیستمِ تیکت می‌سپارد؛ ۱ اگر چیزی به مدیر فرستاده شد."""
    from platform_msg_tickets import handle_user
    ev = {"chat_id": m["chat_id"], "user_id": m["user_id"],
          "name": " ".join(x for x in (m["first_name"], m["last_name"]) if x),
          "username": m["username"], "text": m["text"], "kind": m["kind"], "phone": m["phone"],
          "file_id": m["file_id"], "file_name": m["file_name"], "file_type": m["file_type"]}
    return await handle_user(bot, SplusChannel(token), ev)


async def _warn_admin(bot, text: str) -> None:
    global _last_warn
    now = time.monotonic()
    if now - _last_warn < _WARN_COOLDOWN or not PLATFORM_ADMIN_ID:
        return
    _last_warn = now
    try:
        await bot.send_message(PLATFORM_ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        logger.exception("splus: هشدار به مدیر ارسال نشد")


async def poll_once(bot) -> int:
    """یک دور polling. تعدادِ پیام‌هایِ فورواردشده؛ -1 خطا؛ 0 خاموش/بدونِ توکن/بدونِ پیام."""
    token = (await _get(K_TOKEN)).strip()
    if not token or (await _get(K_ENABLED, "1")) != "1" or not PLATFORM_ADMIN_ID:
        return 0
    offset = (await _get(K_OFFSET)).strip()
    # long-poll کوتاه: سروش وقتی پیامِ تازه نیست پاسخ را نگه می‌دارد؛ با timeout=0 هم گاهی تا
    # ۲۵ ثانیه معطل می‌ماند و «تایم‌اوت» می‌خورد. پس صریحاً ۱۰ ثانیه می‌خواهیم و مهلتِ کلاینت بیشتر است.
    body: dict = {"limit": 50, "timeout": POLL_LONG_SECS}
    if offset.isdigit():
        body["offset"] = int(offset)
    resp = await api_call(token, "getUpdates", body, timeout=POLL_LONG_SECS + 20)
    global _timeouts
    if not resp.get("ok") and resp.get("error") == "تایم‌اوت":
        # دورِ بدونِ پیام (یا کندیِ لحظه‌ای) خطا نیست؛ فقط اگر چند بارِ پشتِ‌سرهم شد هشدار بده
        _timeouts += 1
        if _timeouts < TIMEOUT_WARN_AFTER:
            return 0
    elif resp.get("ok"):
        _timeouts = 0
    if not resp.get("ok"):
        err = str(resp.get("error") or resp)[:200]
        logger.warning("splus: getUpdates ناموفق: %s", err)
        await _warn_admin(bot, "⚠️ <b>اتصال به سروش پلاس برقرار نیست</b>\n"
                               f"علت: <code>{_html.escape(err)}</code>\n"
                               "توکن را با /splus بررسی کن. این هشدار حداکثر ساعتی یک‌بار می‌آید.")
        return -1
    msgs, last = parse_updates(resp)
    sent = 0
    for m in msgs:
        try:
            sent += await _handle(bot, token, m)
        except Exception:
            logger.exception("splus: پردازشِ پیام ناموفق")
    if last is not None:
        await _set(K_OFFSET, str(last + 1))
    return sent


async def splus_poll_loop(platform_bot) -> None:
    while True:
        try:
            n = await poll_once(platform_bot)
            await asyncio.sleep(POLL_ERR_SECS if n < 0 else POLL_IDLE_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("splus: خطای حلقه")
            await asyncio.sleep(POLL_ERR_SECS)


# ─────────────── پنلِ مدیر: /splus ───────────────

def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    tok = (await _get(K_TOKEN)).strip()
    on = (await _get(K_ENABLED, "1")) == "1"
    state = "ثبت نشده" if not tok else ("🟢 فعال" if on else "🔴 خاموش")
    text = ("📨 <b>پلِ سروش پلاس</b>\n\n"
            f"وضعیت: {state}\n\n"
            "پیام‌هایی که به رباتِ سروشِ تو داده می‌شود به همین پی‌وی می‌آید؛ "
            "روی هر پیام ریپلای کنی، جوابت در سروش فرستاده می‌شود.")
    rows = [[InlineKeyboardButton(text="🔑 ثبت/تغییرِ توکن", callback_data="spl:token", style=ButtonStyle.PRIMARY)]]
    if tok:
        rows.append([InlineKeyboardButton(text="🧪 تست اتصال", callback_data="spl:test"),
                     InlineKeyboardButton(text="🔴 خاموش" if on else "🟢 روشن", callback_data="spl:toggle")])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ توکن", callback_data="spl:del", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_splus_router.message(Command("splus"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def spl_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_splus_router.callback_query(F.data.startswith("spl:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def spl_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    if act == "token":
        await state.set_state(SplusFSM.token)
        await cb.message.answer("توکنِ رباتِ سروش را بفرست (از «بات فادر» سروش پلاس).\n"
                                "پیامِ توکن بعد از ثبت پاک می‌شود. برای انصراف /splus بزن.")
        return await cb.answer()
    if act == "toggle":
        await _set(K_ENABLED, "0" if (await _get(K_ENABLED, "1")) == "1" else "1")
    elif act == "del":
        await _set(K_TOKEN, "")
        await _set(K_OFFSET, "")
    elif act == "test":
        r = await api_call((await _get(K_TOKEN)).strip(), "getMe", {}, timeout=15)
        me = r.get("result") if isinstance(r.get("result"), dict) else {}
        if r.get("ok") and me:
            await cb.answer(f"✅ وصل است: {me.get('first_name') or me.get('username') or 'ربات'}", show_alert=True)
        else:
            await cb.answer("❌ " + str(r.get("error") or "توکن نامعتبر")[:150], show_alert=True)
        return
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_splus_router.message(SplusFSM.token, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def spl_token(msg: Message, state: FSMContext):
    tok = (msg.text or "").strip()
    try:
        await msg.delete()          # توکن در چت نماند
    except Exception:
        pass
    if tok.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_\-:.]{10,200}", tok):
        await msg.answer("❌ توکن معتبر نیست؛ دوباره بفرست یا /splus بزن.")
        return
    r = await api_call(tok, "getMe", {}, timeout=15)
    if not (r.get("ok") and r.get("result")):
        await msg.answer("❌ سروش این توکن را قبول نکرد: " + _html.escape(str(r.get("error"))[:150]))
        return
    await state.clear()
    await _set(K_TOKEN, tok)
    await _set(K_OFFSET, "")
    await _set(K_ENABLED, "1")
    text, kb = await _panel()
    await msg.answer("✅ توکن ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ─────────────── جوابِ مدیر ───────────────

def _is_splus_reply(m: Message) -> bool:
    r = m.reply_to_message
    return bool(_is_admin(getattr(m.from_user, "id", None)) and r and (r.text or r.caption)
                and chat_id_from_text(r.text or r.caption) and m.text and not m.text.startswith("/"))


def _is_splus_media_reply(m: Message) -> bool:
    r = m.reply_to_message
    return bool(_is_admin(getattr(m.from_user, "id", None)) and r and (r.text or r.caption)
                and chat_id_from_text(r.text or r.caption) and (m.photo or m.document or m.video or m.voice or m.audio))


@platform_splus_router.message(F.func(_is_splus_reply))
async def spl_reply(msg: Message):
    chat_id = chat_id_from_text(msg.reply_to_message.text or msg.reply_to_message.caption)
    token = (await _get(K_TOKEN)).strip()
    if not token:
        return await msg.reply("❌ توکنِ سروش ثبت نیست (/splus).")
    r = await api_call(token, "sendMessage", {"chat_id": chat_id, "text": msg.text})
    await msg.reply("✅ در سروش ارسال شد." if r.get("ok")
                    else "❌ ارسال نشد: " + _html.escape(str(r.get("error"))[:150]))


@platform_splus_router.message(F.func(_is_splus_media_reply))
async def spl_media_reply(msg: Message):
    """جوابِ مدیر با عکس/فایل/ویدیو/ویس ⇒ برایِ کاربرِ سروش (با کپشنِ مدیر)."""
    chat_id = chat_id_from_text(msg.reply_to_message.text or msg.reply_to_message.caption)
    token = (await _get(K_TOKEN)).strip()
    if not token:
        return await msg.reply("❌ توکنِ سروش ثبت نیست (/splus).")
    if msg.photo:
        fid, name, method, field = msg.photo[-1].file_id, "photo.jpg", "sendPhoto", "photo"
    elif msg.video:
        fid, name, method, field = msg.video.file_id, msg.video.file_name or "video.mp4", "sendVideo", "video"
    elif msg.voice:
        fid, name, method, field = msg.voice.file_id, "voice.ogg", "sendVoice", "voice"
    elif msg.audio:
        fid, name, method, field = msg.audio.file_id, msg.audio.file_name or "audio.mp3", "sendAudio", "audio"
    else:
        fid, name, method, field = msg.document.file_id, msg.document.file_name or "file", "sendDocument", "document"
    try:
        buf = await msg.bot.download(fid)
        data = buf.read() if buf else b""
    except Exception:
        data = b""
    if not data:
        return await msg.reply("❌ فایل از تلگرام دریافت نشد (شاید بزرگ‌تر از ۲۰ مگابایت است).")
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field(field, data, filename=name)
    if msg.caption:
        form.add_field("caption", msg.caption)
    r = await api_call(token, method, timeout=120, form=form)
    await msg.reply("✅ فایل در سروش ارسال شد." if r.get("ok")
                    else "❌ ارسال نشد: " + _html.escape(str(r.get("error"))[:150]))
