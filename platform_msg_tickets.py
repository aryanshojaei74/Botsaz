# -*- coding: utf-8 -*-
"""
platform_msg_tickets.py — سیستمِ تیکتِ پشتیبانیِ رباتِ روبیکا و سروش پلاس (PATCH_PLATFORM_TICKETS_V1).

(جدا از platform_tickets.py که تیکتِ داخلِ تلگرامِ خودِ بات‌ساز است.)

به‌جایِ «هر پیام مستقیم به پی‌وی»، کاربر از منویِ دکمه‌ای تیکت می‌سازد:
  🎫 ثبت تیکت جدید ← انتخابِ دسته ← نوشتنِ مشکل (متن/عکس/فایل) ← شمارهٔ پیگیری BS-<id>
  📂 تیکت‌های من (وضعیت: باز / پاسخ داده‌شده / بسته)، 🔒 بستنِ تیکت، 📱 ارسالِ شماره،
  و بعد از بسته‌شدنِ تیکت توسطِ مدیر، نظرسنجیِ رضایت (عالی/خوب/ضعیف).
پیام‌هایِ بعدیِ کاربر تا وقتی تیکت باز است به همان تیکت اضافه می‌شود (رشتهٔ گفتگو).

مدیر در پی‌ویِ تلگرام کارتِ تیکت را با دکمهٔ «🔒 بستن» می‌گیرد و با ریپلای جواب می‌دهد
(متن یا عکس/فایل)؛ /tickets فهرستِ تیکت‌هایِ باز را نشان می‌دهد.

این فایل مستقل از پیام‌رسان است: هر پیام‌رسان (platform_rubika / platform_splus) یک آدابتورِ
Channel می‌دهد (send / ack / send_file / download) و رویدادِ کاربر را به handle_user می‌سپارد.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from models import PlatformTicket, PlatformTicketMsg
from platform_rubika import _get, _set          # ذخیرهٔ کلید/مقدارِ پلتفرم (مشترک)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_tickets_router = Router()

MAX_OPEN = 3
BTN_NEW = "🎫 ثبت تیکت جدید"
BTN_MY = "📂 تیکت‌های من"
BTN_ABOUT = "ℹ️ راهنما"
BTN_PHONE = "📱 ارسالِ شمارهٔ من"
BTN_CLOSE = "🔒 بستنِ تیکت"
BTN_CANCEL = "🔙 انصراف"
BTN_FAQ = "❓ سوالات متداول"
K_WELCOME, K_FAQ = "tkm_welcome", "tkm_faq"
K_REMIND, K_REMIND_LAST = "tkm_remind_hours", "tkm_remind_last"
REMIND_CHOICES = (0, 1, 3, 6, 12)      # ساعت؛ ۰ = خاموش
QUIET_FROM, QUIET_TO = 0, 8            # ساعتِ تهران: یادآوری نمی‌فرستیم
MAX_FAQ, FAQ_Q_MAX, FAQ_A_MAX = 10, 40, 900
RATES = {"😍 عالی": 3, "🙂 خوب": 2, "😕 ضعیف": 1}
CATS = [("pay", "💳 پرداخت و شارژ"), ("bot", "🤖 ساخت و مدیریتِ ربات"), ("order", "🛒 سفارش و فروش"),
        ("bug", "🐞 مشکلِ فنی"), ("idea", "💡 پیشنهاد و انتقاد"), ("other", "📝 سایر موارد")]
CAT_LABEL = dict(CATS)
CAT_BY_LABEL = {v: k for k, v in CATS}
STATUS_LABEL = {"open": "🟢 باز", "answered": "💬 پاسخ داده شد", "closed": "🔒 بسته"}

WELCOME = ("👋 به پشتیبانیِ بات‌ساز خوش اومدی!\n\n"
           "🎫 مشکل یا سؤالت رو به‌صورت تیکت برامون بفرست؛ شمارهٔ پیگیری می‌گیری و پاسخ همین‌جا برات میاد.\n"
           "از منویِ پایین انتخاب کن 👇")
ABOUT = ("ℹ️ راهنما\n\n"
         "• «🎫 ثبت تیکت جدید» ← دسته رو انتخاب کن ← مشکلت رو بنویس (متن، عکس یا فایل).\n"
         "• تا وقتی تیکتت بازه، هر پیامی بنویسی به همون تیکت اضافه می‌شه.\n"
         "• «📂 تیکت‌های من» وضعیتِ تیکت‌هات رو نشون می‌ده.\n"
         "• «📱 ارسالِ شمارهٔ من» به ما کمک می‌کنه سریع‌تر پیدات کنیم.\n"
         "• هر زمان مشکل حل شد، «🔒 بستنِ تیکت» رو بزن.")


# ─────────────── آدابتورِ پیام‌رسان ───────────────

class Channel:
    """رابطِ مشترکِ پیام‌رسان‌ها. key هم در جدولِ تیکت ذخیره می‌شود."""
    key = ""
    label = ""

    async def send(self, chat_id: str, text: str, menu: list[list[str]] | None = None) -> None: ...

    async def ack(self, chat_id: str, text: str) -> None:
        """پیامِ گذرا؛ بعد از چند ثانیه پاک می‌شود."""
        await self.send(chat_id, text)

    async def send_file(self, chat_id: str, data: bytes, name: str, ftype: str, caption: str = "") -> bool:
        return False

    async def download(self, file_id: str) -> bytes | None:
        return None


async def get_channel(key: str) -> Channel | None:
    """آدابتورِ آمادهٔ استفاده (با توکنِ ذخیره‌شده) یا None."""
    if key == "rubika":
        from platform_rubika import make_channel
        return await make_channel()
    if key == "splus":
        from platform_splus import make_channel
        return await make_channel()
    return None


# ─────────────── وضعیتِ گفتگو و شماره ───────────────

def _skey(prefix: str, ch: Channel, chat: str) -> str:
    return f"{prefix}:{ch.key[0]}:{chat}"[:60]


async def _state(ch: Channel, chat: str) -> str:
    return (await _get(_skey("tks", ch, chat))).strip()


async def _set_state(ch: Channel, chat: str, val: str) -> None:
    await _set(_skey("tks", ch, chat), val)


async def _saved_phone(ch: Channel, chat: str) -> str:
    return (await _get(_skey("tkp", ch, chat))).strip()


# ─────────────── دیتابیس ───────────────

async def _active_ticket(ch: Channel, chat: str) -> PlatformTicket | None:
    async with Session() as s:
        return await s.scalar(select(PlatformTicket).where(
            PlatformTicket.channel == ch.key, PlatformTicket.chat_id == chat,
            PlatformTicket.status != "closed").order_by(PlatformTicket.id.desc()))


async def _count_active(ch: Channel, chat: str) -> int:
    async with Session() as s:
        return int(await s.scalar(select(func.count()).select_from(PlatformTicket).where(
            PlatformTicket.channel == ch.key, PlatformTicket.chat_id == chat,
            PlatformTicket.status != "closed")) or 0)


async def _touch(tid: int, status: str | None = None, **extra) -> PlatformTicket | None:
    async with Session() as s:
        t = await s.get(PlatformTicket, tid)
        if t is None:
            return None
        if status:
            t.status = status
        for k, v in extra.items():
            setattr(t, k, v)
        t.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(t)
        return t


async def _log_msg(tid: int, sender: str, kind: str, text: str) -> None:
    async with Session() as s:
        s.add(PlatformTicketMsg(ticket_id=tid, sender=sender, kind=kind, text=(text or "")[:4000]))
        await s.commit()


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    mins = max(0, int((datetime.utcnow() - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} دقیقه پیش"
    if mins < 60 * 24:
        return f"{mins // 60} ساعت پیش"
    return f"{mins // (60 * 24)} روز پیش"


# ─────────────── متنِ خوش‌آمد و سوالاتِ متداول (قابلِ ویرایش با /tkset) ───────────────

async def _welcome_text() -> str:
    return (await _get(K_WELCOME)).strip() or WELCOME


async def _faq() -> list[dict]:
    try:
        raw = json.loads(await _get(K_FAQ) or "[]")
    except Exception:
        return []
    return [x for x in raw if isinstance(x, dict) and x.get("q") and x.get("a")][:MAX_FAQ]


async def _save_faq(items: list[dict]) -> None:
    await _set(K_FAQ, json.dumps(items, ensure_ascii=False))


def _faq_menu(items: list[dict]) -> list[list[str]]:
    return [[x["q"]] for x in items] + [[BTN_NEW, BTN_CANCEL]]


# ─────────────── منوها ───────────────

async def _menu(ch: Channel, chat: str) -> list[list[str]]:
    rows = [[BTN_NEW], [BTN_MY, BTN_ABOUT]]
    if await _faq():
        rows.insert(1, [BTN_FAQ])
    if await _active_ticket(ch, chat):
        rows.insert(1, [BTN_CLOSE])
    if not await _saved_phone(ch, chat):
        rows.append([BTN_PHONE])
    return rows


def _cat_menu() -> list[list[str]]:
    labels = [v for _, v in CATS]
    return [labels[i:i + 2] for i in range(0, len(labels), 2)] + [[BTN_CANCEL]]


# ─────────────── کارتِ مدیر ───────────────

def _card(t: PlatformTicket, ch: Channel, body: str, kind: str, new: bool) -> str:
    title = "🎫 <b>تیکت جدید</b>" if new else "💬 <b>پیام جدید در تیکت</b>"
    lines = [f"{title} <code>BS-{t.id}</code>",
             f"🏷 دسته: {_html.escape(CAT_LABEL.get(t.category, t.category))} · 📡 {ch.label}",
             f"👤 نام: {_html.escape(t.name or '—')}"]
    if t.username:
        lines.append(f"🔗 یوزرنیم: @{_html.escape(t.username.lstrip('@'))}")
    lines.append(f"📱 شماره: <code>{_html.escape(t.phone)}</code>" if t.phone else "📱 شماره: <i>ثبت نشده</i>")
    lines.append(f"🆔 کاربر: <code>{_html.escape(t.user_id or '—')}</code>")
    if kind == "file":
        lines.append("📎 <i>فایل پیوست شد</i>")
    lines += ["", _html.escape(body[:700]) if body else ("" if kind == "file" else "<i>بدونِ متن</i>"),
              "", "<i>برای پاسخ، روی همین پیام ریپلای کن.</i>"]
    return "\n".join(lines)


def _kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 بستنِ تیکت", callback_data=f"mtk:close:{tid}", style=ButtonStyle.DANGER)]])


async def _to_admin(bot, ch: Channel, t: PlatformTicket, ev: dict, new: bool) -> None:
    from aiogram.types import BufferedInputFile
    cap = _card(t, ch, ev.get("text") or "", ev["kind"], new)
    if ev["kind"] == "file" and ev.get("file_id"):
        data = await ch.download(ev["file_id"])
        if data:
            inp = BufferedInputFile(data, filename=ev.get("file_name") or "file")
            ft = ev.get("file_type") or "document"
            try:
                if ft == "photo" and len(data) <= 10 * 1024 * 1024:
                    await bot.send_photo(PLATFORM_ADMIN_ID, inp, caption=cap, parse_mode="HTML", reply_markup=_kb(t.id))
                elif ft == "video":
                    await bot.send_video(PLATFORM_ADMIN_ID, inp, caption=cap, parse_mode="HTML", reply_markup=_kb(t.id))
                elif ft == "voice":
                    await bot.send_voice(PLATFORM_ADMIN_ID, inp, caption=cap, parse_mode="HTML", reply_markup=_kb(t.id))
                elif ft == "audio":
                    await bot.send_audio(PLATFORM_ADMIN_ID, inp, caption=cap, parse_mode="HTML", reply_markup=_kb(t.id))
                else:
                    await bot.send_document(PLATFORM_ADMIN_ID, inp, caption=cap, parse_mode="HTML", reply_markup=_kb(t.id))
                return
            except Exception:
                logger.exception("tickets: ارسالِ فایل به مدیر ناموفق")
        cap += f"\n\n⚠️ <i>فایل به تلگرام نرسید؛ در خودِ {ch.label} ببین.</i>"
    await bot.send_message(PLATFORM_ADMIN_ID, cap, parse_mode="HTML", reply_markup=_kb(t.id))


async def _admin_note(bot, t: PlatformTicket, text: str) -> None:
    try:
        await bot.send_message(PLATFORM_ADMIN_ID, f"ℹ️ تیکت <code>BS-{t.id}</code>: {text}", parse_mode="HTML")
    except Exception:
        logger.exception("tickets: یادداشت به مدیر ارسال نشد")


# ─────────────── پیامِ کاربر ───────────────

async def handle_user(bot, ch: Channel, ev: dict) -> int:
    """یک رویدادِ کاربر را پردازش می‌کند؛ ۱ اگر چیزی به مدیر فرستاده شد."""
    chat, kind = ev["chat_id"], ev["kind"]
    txt = (ev.get("text") or "").strip()
    low = (txt.split()[0].split("@")[0].lower() if txt else "")
    state = await _state(ch, chat)

    if kind == "contact":
        return await _on_contact(bot, ch, ev)
    if kind == "start" or low in ("/start", "start", "شروع", "/menu", "منو"):
        await _set_state(ch, chat, "")
        await ch.send(chat, await _welcome_text(), await _menu(ch, chat))
        return 0
    if state.startswith("rate:"):
        if txt in RATES:
            return await _on_rate(bot, ch, chat, int(state.split(":", 1)[1] or 0), RATES[txt])
        await _set_state(ch, chat, "")
        state = ""
    if txt == BTN_CANCEL:
        await _set_state(ch, chat, "")
        await ch.send(chat, "انصراف داده شد.", await _menu(ch, chat))
        return 0
    if txt == BTN_FAQ:
        items = await _faq()
        if not items:
            await ch.send(chat, "فعلاً سؤالِ متداولی ثبت نشده.", await _menu(ch, chat))
            return 0
        await _set_state(ch, chat, "faq")
        await ch.send(chat, "❓ سؤالت کدومه؟ یکی رو انتخاب کن 👇", _faq_menu(items))
        return 0
    if state == "faq":
        hit = next((x for x in await _faq() if x["q"] == txt), None)
        if hit:
            await ch.send(chat, f"❓ {hit['q']}\n\n{hit['a']}\n\nهنوز مشکلت حل نشد؟ «{BTN_NEW}» رو بزن و تیکت بساز.",
                          _faq_menu(await _faq()))
            return 0
        await _set_state(ch, chat, "")
        state = ""
    if txt == BTN_NEW:
        if await _count_active(ch, chat) >= MAX_OPEN:
            await ch.send(chat, f"⚠️ الان {MAX_OPEN} تیکتِ باز داری. اول یکی رو ببند یا منتظر پاسخ بمون.",
                          await _menu(ch, chat))
            return 0
        await _set_state(ch, chat, "cat")
        await ch.send(chat, "🏷 موضوعِ تیکتت کدومه؟", _cat_menu())
        return 0
    if txt == BTN_MY:
        await ch.send(chat, await _my_tickets_text(ch, chat), await _menu(ch, chat))
        return 0
    if txt == BTN_ABOUT:
        await ch.send(chat, ABOUT, await _menu(ch, chat))
        return 0
    if txt == BTN_CLOSE:
        t = await _active_ticket(ch, chat)
        if not t:
            await ch.send(chat, "تیکتِ بازی نداری.", await _menu(ch, chat))
            return 0
        await close_ticket(bot, t.id, by="user")
        return 1
    if txt == BTN_PHONE:
        return 0       # دکمهٔ اشتراکِ شماره با پیامِ contact می‌آید، نه متن
    if state == "cat":
        key = CAT_BY_LABEL.get(txt)
        if not key:
            await ch.send(chat, "لطفاً یکی از دسته‌ها رو از منو انتخاب کن 👇", _cat_menu())
            return 0
        await _set_state(ch, chat, f"write:{key}")
        await ch.send(chat, f"✍️ حالا مشکل یا سؤالت رو بنویس ({CAT_LABEL[key]}).\n"
                            "می‌تونی عکس یا فایل هم بفرستی. برای انصراف «" + BTN_CANCEL + "» رو بزن.",
                      [[BTN_CANCEL]])
        return 0
    if state.startswith("write:") and kind in ("text", "file"):
        return await _create_ticket(bot, ch, ev, state.split(":", 1)[1])
    if kind in ("text", "file"):
        t = await _active_ticket(ch, chat)
        if t:
            return await _append(bot, ch, ev, t)
        await ch.send(chat, "برای ارتباط با پشتیبانی، اول «" + BTN_NEW + "» رو بزن 👇", await _menu(ch, chat))
    return 0


async def _my_tickets_text(ch: Channel, chat: str) -> str:
    async with Session() as s:
        rows = (await s.scalars(select(PlatformTicket).where(
            PlatformTicket.channel == ch.key, PlatformTicket.chat_id == chat)
            .order_by(PlatformTicket.id.desc()).limit(5))).all()
    if not rows:
        return "هنوز تیکتی نساختی. «" + BTN_NEW + "» رو بزن."
    lines = ["📂 تیکت‌هایِ تو:", ""]
    for t in rows:
        lines.append(f"{STATUS_LABEL.get(t.status, t.status)} — BS-{t.id} — "
                     f"{CAT_LABEL.get(t.category, t.category)} ({_ago(t.updated_at)})")
    return "\n".join(lines)


async def _on_contact(bot, ch: Channel, ev: dict) -> int:
    chat, phone = ev["chat_id"], ev["phone"]
    await _set(_skey("tkp", ch, chat), phone)
    sent = 0
    async with Session() as s:
        rows = (await s.scalars(select(PlatformTicket).where(
            PlatformTicket.channel == ch.key, PlatformTicket.chat_id == chat,
            PlatformTicket.status != "closed"))).all()
        for t in rows:
            t.phone = phone
        await s.commit()
    if rows:
        await _admin_note(bot, rows[-1], f"کاربر شمارهٔ خودش را داد: <code>{_html.escape(phone)}</code>")
        sent = 1
    await ch.ack(chat, "✅ شمارهٔ تو ثبت شد.")
    await ch.send(chat, "منو به‌روز شد 👇", await _menu(ch, chat))
    return sent


async def _create_ticket(bot, ch: Channel, ev: dict, cat: str) -> int:
    chat = ev["chat_id"]
    if cat not in CAT_LABEL:
        cat = "other"
    phone = await _saved_phone(ch, chat)
    subject = (ev.get("text") or ev.get("file_name") or "")[:200]
    async with Session() as s:
        t = PlatformTicket(channel=ch.key, chat_id=chat, user_id=ev.get("user_id"), name=(ev.get("name") or "")[:120],
                           username=(ev.get("username") or "")[:64], phone=phone or None, category=cat,
                           status="open", subject=subject)
        s.add(t)
        await s.flush()
        s.add(PlatformTicketMsg(ticket_id=t.id, sender="user", kind=ev["kind"], text=ev.get("text") or ""))
        await s.commit()
        await s.refresh(t)
    await _set_state(ch, chat, "")
    await _to_admin(bot, ch, t, ev, new=True)
    await ch.send(chat, f"✅ تیکتت ثبت شد\n\n🎫 شمارهٔ پیگیری: BS-{t.id}\n🏷 دسته: {CAT_LABEL[cat]}\n\n"
                        "پاسخ پشتیبانی همین‌جا برات میاد. اگه مطلبِ دیگه‌ای داری همین‌جا بنویس تا به همین تیکت اضافه بشه.",
                  await _menu(ch, chat))
    return 1


async def _append(bot, ch: Channel, ev: dict, t: PlatformTicket) -> int:
    await _log_msg(t.id, "user", ev["kind"], ev.get("text") or "")
    t = await _touch(t.id, status="open") or t
    await _to_admin(bot, ch, t, ev, new=False)
    await ch.ack(ev["chat_id"], f"✅ به تیکت BS-{t.id} اضافه شد.")
    return 1


async def _on_rate(bot, ch: Channel, chat: str, tid: int, score: int) -> int:
    t = await _touch(tid, rating=score)
    await _set_state(ch, chat, "")
    await ch.send(chat, "🙏 ممنون از نظرت!", await _menu(ch, chat))
    if t:
        name = {3: "😍 عالی", 2: "🙂 خوب", 1: "😕 ضعیف"}[score]
        await _admin_note(bot, t, f"رضایتِ کاربر: {name}")
        return 1
    return 0


# ─────────────── بستنِ تیکت ───────────────

async def close_ticket(bot, tid: int, by: str) -> PlatformTicket | None:
    t = await _touch(tid, status="closed", closed_at=datetime.utcnow())
    if not t:
        return None
    ch = await get_channel(t.channel)
    if ch:
        if by == "admin":
            await _set_state(ch, t.chat_id, f"rate:{t.id}")
            await ch.send(t.chat_id, f"🔒 تیکتِ BS-{t.id} بسته شد.\n\nاز پشتیبانی چقدر راضی بودی؟ 👇",
                          [list(RATES), [BTN_NEW]])
        else:
            await ch.send(t.chat_id, f"🔒 تیکتِ BS-{t.id} بسته شد. هر وقت لازم شد تیکت جدید بساز.",
                          await _menu(ch, t.chat_id))
    if by == "user":
        await _admin_note(bot, t, "کاربر تیکت را بست 🔒")
    return t


# ─────────────── پنلِ مدیر ───────────────

def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


def ticket_id_from(text: str | None) -> int | None:
    """شمارهٔ تیکت از خطِ اولِ پیامِ مدیر (BS-<id>)؛ خطِ اول تا متنِ کاربر قاطی نشود."""
    first = (text or "").split("\n", 1)[0]
    m = re.search(r"BS-(\d+)", first)
    return int(m.group(1)) if m else None


@platform_tickets_router.message(Command("tickets"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def tk_list(msg: Message):
    async with Session() as s:
        rows = (await s.scalars(select(PlatformTicket).where(PlatformTicket.status != "closed")
                                .order_by(PlatformTicket.updated_at.desc()).limit(15))).all()
        total = int(await s.scalar(select(func.count()).select_from(PlatformTicket)) or 0)
        good = int(await s.scalar(select(func.count()).select_from(PlatformTicket).where(PlatformTicket.rating == 3)) or 0)
        rated = int(await s.scalar(select(func.count()).select_from(PlatformTicket).where(PlatformTicket.rating.is_not(None))) or 0)
    lines = [f"🎫 <b>تیکت‌هایِ باز</b> ({len(rows)})"]
    for t in rows:
        lines.append(f"{STATUS_LABEL.get(t.status, t.status)} · <code>BS-{t.id}</code> · "
                     f"{'روبیکا' if t.channel == 'rubika' else 'سروش'} · "
                     f"{_html.escape(CAT_LABEL.get(t.category, t.category))} · "
                     f"{_html.escape(t.name or '—')} · {_ago(t.updated_at)}")
    if not rows:
        lines.append("— تیکتِ بازی نیست 🎉")
    lines.append(f"\nکل تیکت‌ها: {total}" + (f" · رضایتِ «عالی»: {good} از {rated}" if rated else ""))
    await msg.answer("\n".join(lines), parse_mode="HTML")


@platform_tickets_router.callback_query(F.data.startswith("mtk:close:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def tk_close_cb(cb: CallbackQuery):
    tid = int(cb.data.rsplit(":", 1)[1])
    t = await close_ticket(cb.bot, tid, by="admin")
    await cb.answer("🔒 تیکت بسته شد" if t else "تیکت پیدا نشد", show_alert=not t)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


def _reply_ticket_id(m: Message) -> int | None:
    r = m.reply_to_message
    if not (_is_admin(getattr(m.from_user, "id", None)) and r):
        return None
    return ticket_id_from(r.text or r.caption)


def _is_ticket_text_reply(m: Message) -> bool:
    return bool(m.text and not m.text.startswith("/") and _reply_ticket_id(m))


def _is_ticket_media_reply(m: Message) -> bool:
    return bool((m.photo or m.document or m.video or m.voice or m.audio) and _reply_ticket_id(m))


async def _load_for_reply(msg: Message):
    tid = _reply_ticket_id(msg)
    async with Session() as s:
        t = await s.get(PlatformTicket, tid)
    if not t:
        await msg.reply("❌ این تیکت پیدا نشد.")
        return None, None
    ch = await get_channel(t.channel)
    if not ch:
        await msg.reply(f"❌ ربات {t.channel} تنظیم نیست (/rubika یا /splus).")
        return None, None
    return t, ch


@platform_tickets_router.message(F.func(_is_ticket_text_reply))
async def tk_reply(msg: Message):
    t, ch = await _load_for_reply(msg)
    if not t:
        return
    await ch.send(t.chat_id, f"💬 پاسخ پشتیبانی — تیکت BS-{t.id}\n\n{msg.text}")
    await _log_msg(t.id, "admin", "text", msg.text)
    if t.status != "closed":
        await _touch(t.id, status="answered")
    await msg.reply("✅ برایِ کاربر فرستاده شد." + (" (تیکت بسته است)" if t.status == "closed" else ""))


@platform_tickets_router.message(F.func(_is_ticket_media_reply))
async def tk_media_reply(msg: Message):
    t, ch = await _load_for_reply(msg)
    if not t:
        return
    if msg.photo:
        fid, name, ftype = msg.photo[-1].file_id, "photo.jpg", "photo"
    elif msg.video:
        fid, name, ftype = msg.video.file_id, msg.video.file_name or "video.mp4", "video"
    elif msg.voice:
        fid, name, ftype = msg.voice.file_id, "voice.ogg", "voice"
    elif msg.audio:
        fid, name, ftype = msg.audio.file_id, msg.audio.file_name or "audio.mp3", "audio"
    else:
        fid, name, ftype = msg.document.file_id, msg.document.file_name or "file", "document"
    try:
        buf = await msg.bot.download(fid)
        data = buf.read() if buf else b""
    except Exception:
        data = b""
    if not data:
        return await msg.reply("❌ فایل از تلگرام دریافت نشد (شاید بزرگ‌تر از ۲۰ مگابایت است).")
    cap = f"💬 پاسخ پشتیبانی — تیکت BS-{t.id}" + (f"\n\n{msg.caption}" if msg.caption else "")
    ok = await ch.send_file(t.chat_id, data, name, ftype, cap)
    if ok:
        await _log_msg(t.id, "admin", "file", msg.caption or name)
        if t.status != "closed":
            await _touch(t.id, status="answered")
    await msg.reply("✅ فایل برایِ کاربر فرستاده شد." if ok else "❌ ارسالِ فایل ناموفق بود.")


# ─────────────── یادآوریِ تیکت‌هایِ بی‌پاسخ ───────────────

async def _remind_hours() -> int:
    try:
        v = int((await _get(K_REMIND, "3")) or 3)
    except ValueError:
        v = 3
    return v if v in REMIND_CHOICES else 3


def _tehran_hour(now: datetime) -> int:
    return (now + timedelta(hours=3, minutes=30)).hour


async def remind_once(bot, now: datetime | None = None) -> int:
    """اگر تیکتِ «باز» (هنوز بدونِ پاسخِ مدیر) بیش از N ساعت مانده باشد، یک پیامِ خلاصه به مدیر می‌فرستد؛
    حداکثر هر N ساعت یک‌بار و نه در ساعاتِ خواب (۰ تا ۸ تهران). تعدادِ تیکت‌هایِ گزارش‌شده را برمی‌گرداند."""
    now = now or datetime.utcnow()
    hours = await _remind_hours()
    if not hours or not PLATFORM_ADMIN_ID or QUIET_FROM <= _tehran_hour(now) < QUIET_TO:
        return 0
    last = (await _get(K_REMIND_LAST)).strip()
    try:
        if last and now - datetime.fromisoformat(last) < timedelta(hours=hours):
            return 0
    except ValueError:
        pass
    async with Session() as s:
        rows = (await s.scalars(select(PlatformTicket).where(
            PlatformTicket.status == "open",
            PlatformTicket.updated_at <= now - timedelta(hours=hours))
            .order_by(PlatformTicket.updated_at.asc()).limit(10))).all()
    if not rows:
        return 0
    lines = [f"⏰ <b>{len(rows)} تیکتِ بی‌پاسخ</b> (بیش از {hours} ساعت)"]
    for t in rows:
        lines.append(f"<code>BS-{t.id}</code> · {'روبیکا' if t.channel == 'rubika' else 'سروش'} · "
                     f"{_html.escape(CAT_LABEL.get(t.category, t.category))} · {_html.escape(t.name or '—')} · "
                     f"{_ago(t.updated_at)}")
    lines.append("\nبرای دیدنِ کارتشان /tickets را بزن.")
    try:
        await bot.send_message(PLATFORM_ADMIN_ID, "\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("tickets: یادآوری ارسال نشد")
        return 0
    await _set(K_REMIND_LAST, now.isoformat())
    return len(rows)


async def ticket_reminder_loop(platform_bot) -> None:
    while True:
        try:
            await remind_once(platform_bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tickets: خطای حلقهٔ یادآوری")
        await asyncio.sleep(600)


# ─────────────── تنظیماتِ مدیر: /tkset (خوش‌آمد + سوالاتِ متداول) ───────────────

class TkSetFSM(StatesGroup):
    welcome = State()
    faq_q = State()
    faq_a = State()


async def _set_panel() -> tuple[str, InlineKeyboardMarkup]:
    custom = (await _get(K_WELCOME)).strip()
    items = await _faq()
    hrs = await _remind_hours()
    text = ("⚙️ <b>تنظیماتِ تیکتِ روبیکا/سروش</b>\n\n"
            f"👋 متنِ خوش‌آمد: {'<i>سفارشی</i>' if custom else '<i>پیش‌فرض</i>'}\n"
            f"❓ سوالاتِ متداول: <b>{len(items)}</b> از {MAX_FAQ}\n"
            f"⏰ یادآوریِ تیکتِ بی‌پاسخ: <b>{('هر ' + str(hrs) + ' ساعت') if hrs else 'خاموش'}</b>\n\n"
            "سوالاتِ متداول به‌صورتِ دکمه در منویِ کاربر می‌آیند و جوابشان را بدونِ تیکت می‌گیرند.")
    rows = [[InlineKeyboardButton(text="✏️ ویرایشِ متنِ خوش‌آمد", callback_data="mtks:welcome")],
            [InlineKeyboardButton(text="⏰ تغییرِ زمانِ یادآوری", callback_data="mtks:remind")]]
    if custom:
        rows.append([InlineKeyboardButton(text="♻️ برگشت به متنِ پیش‌فرض", callback_data="mtks:wreset")])
    if len(items) < MAX_FAQ:
        rows.append([InlineKeyboardButton(text="➕ افزودنِ سؤالِ متداول", callback_data="mtks:add", style=ButtonStyle.SUCCESS)])
    for i, x in enumerate(items):
        rows.append([InlineKeyboardButton(text=f"🗑 {x['q']}", callback_data=f"mtks:del:{i}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_tickets_router.message(Command("tkset"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def tks_cmd(msg: Message, state: FSMContext):
    await state.clear()
    text, kb = await _set_panel()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_tickets_router.callback_query(F.data.startswith("mtks:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def tks_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 2)[1]
    if act == "welcome":
        await state.set_state(TkSetFSM.welcome)
        await cb.message.answer("متنِ جدیدِ خوش‌آمد را بفرست (حداکثر ۷۰۰ حرف). لغو: /tkset")
        return await cb.answer()
    if act == "add":
        await state.set_state(TkSetFSM.faq_q)
        await cb.message.answer(f"❓ متنِ سؤال را بفرست (حداکثر {FAQ_Q_MAX} حرف؛ همین روی دکمه می‌نشیند). لغو: /tkset")
        return await cb.answer()
    if act == "remind":
        cur = await _remind_hours()
        await _set(K_REMIND, str(REMIND_CHOICES[(REMIND_CHOICES.index(cur) + 1) % len(REMIND_CHOICES)]))
    elif act == "wreset":
        await _set(K_WELCOME, "")
    elif act == "del":
        items = await _faq()
        i = int(cb.data.rsplit(":", 1)[1])
        if 0 <= i < len(items):
            items.pop(i)
            await _save_faq(items)
    text, kb = await _set_panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_tickets_router.message(TkSetFSM.welcome, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def tks_welcome_in(msg: Message, state: FSMContext):
    if msg.text.startswith("/"):
        return
    await _set(K_WELCOME, msg.text.strip()[:700])
    await state.clear()
    text, kb = await _set_panel()
    await msg.answer("✅ متنِ خوش‌آمد ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_tickets_router.message(TkSetFSM.faq_q, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def tks_faq_q(msg: Message, state: FSMContext):
    if msg.text.startswith("/"):
        return
    q = msg.text.strip()
    if len(q) > FAQ_Q_MAX or q in (BTN_NEW, BTN_MY, BTN_ABOUT, BTN_FAQ, BTN_CLOSE, BTN_CANCEL, BTN_PHONE) \
            or any(x["q"] == q for x in await _faq()):
        await msg.answer(f"❌ سؤال باید یکتا و حداکثر {FAQ_Q_MAX} حرف باشد. دوباره بفرست.")
        return
    await state.update_data(q=q)
    await state.set_state(TkSetFSM.faq_a)
    await msg.answer("✍️ حالا پاسخِ این سؤال را بفرست.")


@platform_tickets_router.message(TkSetFSM.faq_a, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def tks_faq_a(msg: Message, state: FSMContext):
    if msg.text.startswith("/"):
        return
    q = (await state.get_data()).get("q")
    items = await _faq()
    if q and len(items) < MAX_FAQ:
        items.append({"q": q, "a": msg.text.strip()[:FAQ_A_MAX]})
        await _save_faq(items)
    await state.clear()
    text, kb = await _set_panel()
    await msg.answer("✅ سؤال اضافه شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
