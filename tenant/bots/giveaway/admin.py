"""
tenant/bots/giveaway/admin.py — پنلِ مدیریتِ رباتِ قرعه‌کشی (PATCH_GIVEAWAY_BOT_V1).

ساخت (ویزارد) ← مدیریت ← پایان/قرعه ← تحویلِ جایزه ← اعلان به گروه/کانال/کاربران ← خروجیِ CSV.
همهٔ صفحه‌ها با ادیتِ همان پیام عوض می‌شوند و دکمهٔ بازگشت دارند.
"""
from __future__ import annotations

import asyncio
import html as _html
import csv
import io
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select

from config import Session
from models import GiveawayChat, GiveawayEntry

from tenant.bots.giveaway import announce as A
from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway.common import (HOME_ROW, back_row, btn, card_text, edit_by_id, edit_or_send, esc, fa,
                                         fmt_time, is_admin, kb, medal, parse_hours, prize_lines, results_text, time_left)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

giveaway_admin_router = Router(name="giveaway_admin")
MENU = "gw:adm"


class GwAdminFSM(StatesGroup):
    title = State()
    desc = State()
    winners = State()
    prize_title = State()
    prize_content = State()
    prize_amount = State()
    custom_time = State()
    time_edit = State()
    chat_add = State()


async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _guard(event, bot: Bot):
    """ردیفِ ربات اگر کاربر ادمین/مالک است؛ وگرنه None (و پاسخِ مناسب)."""
    row = await _row(bot)
    uid = event.from_user.id
    if not row or not await is_admin(row, uid):
        if hasattr(event, "data"):
            try:
                await event.answer("فقط مدیرِ ربات.", show_alert=True)
            except Exception:      # noqa: BLE001
                pass
        return None
    return row


def _n(v) -> str:
    return fa(f"{int(v):,}")


# ═══════════════════════ منوی اصلی ═══════════════════════

async def menu_content(row, bot: Bot | None = None) -> tuple[str, object]:
    act = await E.active_giveaways(row.id)
    chats = await A.saved_chats(row.id)
    text = ("🎰 <b>مدیریتِ قرعه‌کشی</b>\n\n"
            f"🟢 قرعه‌کشی‌هایِ فعال: <b>{fa(len(act))}</b>\n"
            f"📣 مقصدهایِ اعلان (گروه/کانال): <b>{fa(len(chats))}</b>")
    if bot is not None:        # PATCH_GIVEAWAY_FJ_HEALTH_V1
        try:
            bad = await A.forcejoin_problems(bot, row.id)
        except Exception:      # noqa: BLE001
            logger.exception("giveaway: بررسیِ سلامتِ عضویتِ اجباری ناموفق بود")
            bad = []
        if bad:
            text += ("\n\n⚠️ <b>عضویتِ اجباری کار نمی‌کند</b> (کاربرها بدونِ عضویت رد می‌شوند):\n"
                     + "\n".join(f"• {_html.escape(b)}" for b in bad[:6])
                     + "\nربات را در آن کانال/گروه <b>ادمین</b> کن، بعد دوباره همین منو را باز کن.")
    return text, kb(
        [btn("➕ ساختِ قرعه‌کشیِ جدید", "gw:a:new", style=ButtonStyle.SUCCESS)],
        [btn(f"📋 قرعه‌کشی‌هایِ فعال ({fa(len(act))})", "gw:a:act", style=ButtonStyle.PRIMARY),
         btn("🏁 تاریخچه و نتایج", "gw:a:hist", style=ButtonStyle.PRIMARY)],
        [btn("📣 گروه/کانال‌هایِ اعلان", "gw:a:chats", style=ButtonStyle.PRIMARY),
         btn("📢 عضویتِ اجباری", "tadm:forcejoin", style=ButtonStyle.PRIMARY)],
        [btn("⚙️ تنظیمات", "gw:a:set", style=ButtonStyle.PRIMARY)],
        HOME_ROW)


async def show_menu(event, bot: Bot, row) -> None:
    text, markup = await menu_content(row, bot)
    await edit_or_send(event, text, markup)


@giveaway_admin_router.callback_query(F.data == MENU)
async def adm_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await show_menu(cb, bot, row)
    await cb.answer()


@giveaway_admin_router.callback_query(F.data == "gw:a:cancel")
async def adm_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await cb.answer("لغو شد.")
    await show_menu(cb, bot, row)


# ═══════════════════════ ویزاردِ ساخت ═══════════════════════

def _w(d: dict) -> dict:
    return d.get("gw_w") or {}


async def _ask(event_or_ids, bot: Bot, state: FSMContext, text: str, markup) -> None:
    """صفحهٔ وزارد را در همان پیام (ذخیره‌شده) ادیت می‌کند."""
    d = await state.get_data()
    if isinstance(event_or_ids, CallbackQuery) or (hasattr(event_or_ids, "data") and hasattr(event_or_ids, "message")):
        m = await edit_or_send(event_or_ids, text, markup)
        if m is not None:
            await state.update_data(gw_chat=m.chat.id, gw_msg=m.message_id)
        return
    ok = await edit_by_id(bot, d.get("gw_chat"), d.get("gw_msg"), text, markup)
    if not ok:
        m = await event_or_ids.answer(text, reply_markup=markup, parse_mode="HTML")
        await state.update_data(gw_chat=m.chat.id, gw_msg=m.message_id)


_CANCEL = [btn("❌ انصراف", "gw:a:cancel", style=ButtonStyle.DANGER)]


@giveaway_admin_router.callback_query(F.data == "gw:a:new")
async def wiz_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await state.update_data(gw_w={"title": "", "winners": 1, "prizes": [], "phone": False, "captcha": True, "ref": 1,
                                  "hide": True, "notify": True, "hours": None, "desc": ""})
    await state.set_state(GwAdminFSM.title)
    await _ask(cb, bot, state, "📝 <b>عنوانِ قرعه‌کشی</b> را بفرست:\n\nمثال: <code>۵۰ گیگ کانفیگ رایگان</code>", kb(_CANCEL))
    await cb.answer()


@giveaway_admin_router.message(GwAdminFSM.title, F.text)
async def wiz_title(msg: Message, bot: Bot, state: FSMContext):
    t = (msg.text or "").strip()
    if not t or len(t) > 160:
        return await msg.answer("❌ عنوان باید بینِ ۱ تا ۱۶۰ حرف باشد.")
    d = await state.get_data()
    w = _w(d)
    w["title"] = t
    await state.update_data(gw_w=w)
    await state.set_state(GwAdminFSM.desc)
    await _ask(msg, bot, state, f"🎰 <b>{esc(t)}</b>\n\n📄 <b>توضیحات</b> (اختیاری) را بفرست — شرایط، قوانین، …",
               kb([btn("⏭ بدونِ توضیحات", "gw:a:nodesc", style=ButtonStyle.PRIMARY)], _CANCEL))


async def _ask_winners(event, bot, state):
    await state.set_state(GwAdminFSM.winners)
    await _ask(event, bot, state, "🏆 <b>تعدادِ برنده‌ها</b> را بفرست (عددِ ۱ تا ۱۰۰):", kb(_CANCEL))


@giveaway_admin_router.message(GwAdminFSM.desc, F.text)
async def wiz_desc(msg: Message, bot: Bot, state: FSMContext):
    d = await state.get_data()
    w = _w(d)
    w["desc"] = (msg.html_text or msg.text or "").strip()[:1500]
    await state.update_data(gw_w=w)
    await _ask_winners(msg, bot, state)


@giveaway_admin_router.callback_query(F.data == "gw:a:nodesc")
async def wiz_nodesc(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await _ask_winners(cb, bot, state)
    await cb.answer()


@giveaway_admin_router.message(GwAdminFSM.winners, F.text)
async def wiz_winners(msg: Message, bot: Bot, state: FSMContext):
    t = E.to_ascii_digits(msg.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= E.MAX_WINNERS:
        return await msg.answer(f"❌ عددی بینِ ۱ تا {E.MAX_WINNERS} بفرست.")
    d = await state.get_data()
    w = _w(d)
    w["winners"], w["prizes"] = int(t), []
    await state.update_data(gw_w=w)
    await _ask_prize_title(msg, bot, state)


async def _ask_prize_title(event, bot, state):
    d = await state.get_data()
    w = _w(d)
    i = len(w["prizes"]) + 1
    await state.set_state(GwAdminFSM.prize_title)
    await _ask(event, bot, state,
               f"🎁 <b>جایزهٔ {fa(i)}</b> از {fa(w['winners'])}\n\n✏️ عنوانِ جایزه را بفرست (مثلاً <code>۵۰ گیگ</code>):",
               kb(_CANCEL))


@giveaway_admin_router.message(GwAdminFSM.prize_title, F.text)
async def wiz_prize_title(msg: Message, bot: Bot, state: FSMContext):
    t = (msg.text or "").strip()
    if not t or len(t) > 200:
        return await msg.answer("❌ عنوانِ جایزه باید بینِ ۱ تا ۲۰۰ حرف باشد.")
    await state.update_data(gw_ptitle=t)
    d = await state.get_data()
    w = _w(d)
    rows = [[btn("⏭ بدونِ محتوا (تحویلِ دستی)", "gw:a:pc:none", style=ButtonStyle.PRIMARY)],
            [btn("💰 جایزهٔ کیف‌پول (شارژِ خودکار)", "gw:a:pc:wallet", style=ButtonStyle.SUCCESS)]]
    if len(w["prizes"]) == 0 and w["winners"] > 1:
        rows.append([btn("🔁 همین جایزه برایِ همهٔ برنده‌ها (بدونِ محتوا)", "gw:a:pc:all", style=ButtonStyle.PRIMARY)])
    rows.append(_CANCEL)
    await state.set_state(GwAdminFSM.prize_content)
    await _ask(msg, bot, state,
               f"🎁 <b>{esc(t)}</b>\n\n📎 <b>خودِ جایزه</b> را بفرست تا بعد از قرعه برایِ برنده ارسال شود "
               "(متن، عکس، ویدیو، فایل، کانفیگ/کد…)\n\nیا یکی از دکمه‌هایِ زیر:", kb(*rows))


async def _prize_done(event, bot, state, prize: dict, fill_all: bool = False):
    d = await state.get_data()
    w = _w(d)
    w["prizes"].append(prize)
    if fill_all:
        w["prizes"] = [prize] * w["winners"]
    await state.update_data(gw_w=w)
    if len(w["prizes"]) < w["winners"]:
        return await _ask_prize_title(event, bot, state)
    await state.set_state(None)
    await _options(event, bot, state)


@giveaway_admin_router.message(GwAdminFSM.prize_content, F.text | F.photo | F.video | F.document | F.animation | F.audio | F.voice)
async def wiz_prize_content(msg: Message, bot: Bot, state: FSMContext):
    d = await state.get_data()
    title = d.get("gw_ptitle") or "جایزه"
    p = {"title": title, "kind": "text", "file_id": None, "text": None, "amount": 0}
    if msg.text:
        p["text"] = msg.html_text
    elif msg.photo:
        p.update(kind="photo", file_id=msg.photo[-1].file_id, text=msg.caption and msg.html_text)
    elif msg.video:
        p.update(kind="video", file_id=msg.video.file_id, text=msg.caption and msg.html_text)
    elif msg.document:
        p.update(kind="document", file_id=msg.document.file_id, text=msg.caption and msg.html_text)
    elif msg.animation:
        p.update(kind="animation", file_id=msg.animation.file_id, text=msg.caption and msg.html_text)
    elif msg.audio:
        p.update(kind="audio", file_id=msg.audio.file_id, text=msg.caption and msg.html_text)
    elif msg.voice:
        p.update(kind="voice", file_id=msg.voice.file_id)
    await _prize_done(msg, bot, state, p)


@giveaway_admin_router.callback_query(F.data.startswith("gw:a:pc:"))
async def wiz_prize_choice(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    d = await state.get_data()
    title = d.get("gw_ptitle") or "جایزه"
    what = cb.data.split(":")[3]
    await cb.answer()
    if what == "wallet":
        await state.set_state(GwAdminFSM.prize_amount)
        return await _ask(cb, bot, state, f"💰 <b>{esc(title)}</b>\n\nمبلغِ شارژِ کیف‌پولِ برنده را (به تومان) بفرست:", kb(_CANCEL))
    p = {"title": title, "kind": "title", "file_id": None, "text": None, "amount": 0}
    await _prize_done(cb, bot, state, p, fill_all=(what == "all"))


@giveaway_admin_router.message(GwAdminFSM.prize_amount, F.text)
async def wiz_prize_amount(msg: Message, bot: Bot, state: FSMContext):
    t = E.to_ascii_digits(msg.text or "").replace(",", "").replace("٬", "").strip()
    if not t.isdigit() or not 1000 <= int(t) <= 100_000_000:
        return await msg.answer("❌ مبلغی بینِ ۱٬۰۰۰ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان بفرست.")
    d = await state.get_data()
    await _prize_done(msg, bot, state, {"title": d.get("gw_ptitle") or "شارژ کیف‌پول", "kind": "wallet",
                                        "file_id": None, "text": None, "amount": int(t)})


# ── گزینه‌ها ──

_REF_CYCLE = [0, 1, 2, 3, 5]
_HOURS_CYCLE = [None, 1, 6, 12, 24, 72, 168]


def _hours_lbl(h) -> str:
    if not h:
        return "بدونِ پایانِ خودکار (دستی)"
    return f"{fa(h // 24)} روز" if h >= 24 and h % 24 == 0 else f"{fa(h)} ساعت"


def _onoff(v) -> str:
    return "✅ روشن" if v else "⛔️ خاموش"


async def _options(event, bot, state):
    d = await state.get_data()
    w = _w(d)
    text = ("⚙️ <b>گزینه‌هایِ قرعه‌کشی</b>\n\nهرکدام را بزن تا تغییر کند، بعد «▶️ ادامه».\n\n"
            "• <b>سؤالِ ریاضی</b>: جلویِ اکانت‌هایِ فیک/رباتی را می‌گیرد.\n"
            "• <b>شمارهٔ ایرانی</b>: فقط شمارهٔ خودِ کاربر (+98) و هر شماره فقط یک‌بار.\n"
            "• <b>شانسِ دعوت</b>: هر نفری که با لینکِ کاربر ثبت‌نام کند، شانسِ او را بیشتر می‌کند.")
    markup = kb(
        [btn(f"🧮 سؤالِ ریاضی: {_onoff(w['captcha'])}", "gw:a:o:captcha", style=ButtonStyle.PRIMARY)],
        [btn(f"☎️ شمارهٔ ایرانی اجباری: {_onoff(w['phone'])}", "gw:a:o:phone", style=ButtonStyle.PRIMARY)],
        [btn(f"👥 شانسِ هر دعوت: {fa(w['ref'])}" if w["ref"] else "👥 شانسِ دعوت: ⛔️ خاموش", "gw:a:o:ref", style=ButtonStyle.PRIMARY)],
        [btn(f"🪪 برندگان در اعلانِ عمومی: {'ناشناس (فقط شناسه)' if w['hide'] else 'با نام'}", "gw:a:o:hide", style=ButtonStyle.PRIMARY)],
        [btn(f"🔔 اطلاعیه به همهٔ کاربران: {_onoff(w['notify'])}", "gw:a:o:notify", style=ButtonStyle.PRIMARY)],
        [btn(f"⏰ پایانِ خودکار: {_hours_lbl(w['hours'])}", "gw:a:o:hours", style=ButtonStyle.PRIMARY),
         btn("✏️ زمانِ دلخواه", "gw:a:o:custom", style=ButtonStyle.PRIMARY)],
        [btn("▶️ ادامه", "gw:a:sum", style=ButtonStyle.SUCCESS)], _CANCEL)
    await _ask(event, bot, state, text, markup)


@giveaway_admin_router.callback_query(F.data.startswith("gw:a:o:"))
async def wiz_option(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    d = await state.get_data()
    if "gw_w" not in d:
        return await cb.answer("نشستِ ساخت منقضی شد؛ دوباره «➕ ساخت» را بزن.", show_alert=True)
    w = _w(d)
    key = cb.data.split(":")[3]
    if key in ("captcha", "phone", "hide", "notify"):
        w[key] = not w[key]
    elif key == "ref":
        w["ref"] = _REF_CYCLE[(_REF_CYCLE.index(w["ref"]) + 1) % len(_REF_CYCLE)] if w["ref"] in _REF_CYCLE else 1
    elif key == "hours":
        w["hours"] = _HOURS_CYCLE[(_HOURS_CYCLE.index(w["hours"]) + 1) % len(_HOURS_CYCLE)] if w["hours"] in _HOURS_CYCLE else None
    elif key == "custom":
        await state.set_state(GwAdminFSM.custom_time)
        await cb.answer()
        return await _ask(cb, bot, state, "⏰ مدتِ قرعه‌کشی را بفرست:\n\n<code>6h</code> یا <code>2d</code> یا <code>1d 12h</code> "
                                          "(حداقل ۱ ساعت، حداکثر ۳۰ روز)", kb(back_row("gw:a:sum", "⬅️ بازگشت"), _CANCEL))
    await state.update_data(gw_w=w)
    await cb.answer()
    await _options(cb, bot, state)


@giveaway_admin_router.message(GwAdminFSM.custom_time, F.text)
async def wiz_custom_time(msg: Message, bot: Bot, state: FSMContext):
    h = parse_hours(msg.text)
    if h is None:
        return await msg.answer("❌ فرمت نامعتبر. مثال: <code>6h</code> یا <code>2d</code> یا <code>1d 12h</code>", parse_mode="HTML")
    d = await state.get_data()
    w = _w(d)
    w["hours"] = h
    await state.update_data(gw_w=w)
    await state.set_state(None)
    await _options(msg, bot, state)


def _summary(w: dict) -> str:
    lines = [f"📋 <b>خلاصهٔ قرعه‌کشی</b>", "", f"🎰 عنوان: <b>{esc(w['title'])}</b>"]
    if w.get("desc"):
        lines.append("📄 توضیحات: دارد")
    lines += [f"🏆 تعدادِ برنده: <b>{fa(w['winners'])}</b>", "", "🎁 جوایز:"]
    for i, p in enumerate(w["prizes"], start=1):
        kind = {"title": "تحویلِ دستی", "wallet": f"کیف‌پول {_n(p['amount'])} تومان", "text": "متن"}.get(p["kind"], "فایل/مدیا")
        lines.append(f"{medal(i)} {esc(p['title'])} — <i>{kind}</i>")
    lines += ["", f"🧮 سؤالِ ریاضی: {_onoff(w['captcha'])}", f"☎️ شمارهٔ ایرانی: {_onoff(w['phone'])}",
              f"👥 شانسِ دعوت: {fa(w['ref']) if w['ref'] else 'خاموش'}", f"🔔 اطلاعیه به همه: {_onoff(w['notify'])}",
              f"⏰ پایان: {_hours_lbl(w['hours'])}", "", "از اطلاعاتِ واردشده مطمئنی؟"]
    return "\n".join(lines)


@giveaway_admin_router.callback_query(F.data == "gw:a:sum")
async def wiz_summary(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    d = await state.get_data()
    if "gw_w" not in d:
        return await cb.answer("نشستِ ساخت منقضی شد.", show_alert=True)
    await state.set_state(None)
    await _ask(cb, bot, state, _summary(_w(d)), kb(
        [btn("✅ تأیید و شروع", "gw:a:ok", style=ButtonStyle.SUCCESS)],
        [btn("✏️ ویرایشِ گزینه‌ها", "gw:a:optsback", style=ButtonStyle.PRIMARY)], _CANCEL))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data == "gw:a:optsback")
async def wiz_opts_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await _options(cb, bot, state)
    await cb.answer()


@giveaway_admin_router.callback_query(F.data == "gw:a:ok")
async def wiz_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_setting
    row = await _guard(cb, bot)
    if not row:
        return
    d = await state.get_data()
    w = _w(d)
    if not w.get("title") or not w.get("prizes"):
        return await cb.answer("نشستِ ساخت منقضی شد؛ دوباره بساز.", show_alert=True)
    await state.clear()
    now = datetime.utcnow()
    g = await E.create_giveaway(row.id, E.NewGiveaway(
        title=w["title"], winners_count=w["winners"], description=w.get("desc") or "", require_phone=w["phone"],
        captcha=w["captcha"], ref_bonus=w["ref"], hide_winners=w["hide"], notify_all=w["notify"],
        ends_at=(now + timedelta(hours=w["hours"])) if w["hours"] else None, created_by=cb.from_user.id),
        [E.NewPrize(p["title"], p["kind"], p["file_id"], p["text"], p["amount"]) for p in w["prizes"]])
    await cb.answer("✅ ساخته شد")
    posted = 0
    errs: list[str] = []
    if await get_setting(row.id, "gw_post_on_create", "1") == "1":
        posted, errs = await A.post_announcement(bot, row, g)
    if g.notify_all:
        asyncio.create_task(A.announce_start(bot, row, g))
    note = [f"✅ <b>قرعه‌کشی «{esc(g.title)}» ساخته و شروع شد!</b>"]
    if posted:
        note.append(f"📣 اعلان در {fa(posted)} گروه/کانال ارسال شد.")
    if errs:
        note.append("⚠️ ارسال نشد: " + "؛ ".join(esc(e) for e in errs[:3]))
    if g.notify_all:
        note.append("🔔 اطلاعیه در حالِ ارسال به کاربران است.")
    await edit_or_send(cb, "\n".join(note), kb([btn("📋 مدیریتِ این قرعه‌کشی", f"gw:a:g:{g.id}", style=ButtonStyle.SUCCESS)],
                                               [btn("⬅️ منویِ قرعه‌کشی", MENU, style=ButtonStyle.DANGER)]))


# ═══════════════════════ فهرست و جزئیات ═══════════════════════

async def _list(event, row, statuses, title: str, empty: str) -> None:
    gs = await E.recent_giveaways(row.id, 20, statuses)
    rows = []
    for g in gs:
        n = await E.count_entries(g.id)
        ic = {"active": "🟢", "ended": "🏁", "cancelled": "⛔️"}.get(g.status, "•")
        rows.append([btn(f"{ic} {g.title[:30]} · {fa(n)} نفر", f"gw:a:g:{g.id}", style=ButtonStyle.PRIMARY)])
    await edit_or_send(event, f"{title}\n\n" + ("" if gs else empty), kb(*rows, back_row(MENU)))


@giveaway_admin_router.callback_query(F.data == "gw:a:act")
async def adm_active(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    await _list(cb, row, ("active",), "📋 <b>قرعه‌کشی‌هایِ فعال</b>", "قرعه‌کشیِ فعالی نیست.")
    await cb.answer()


@giveaway_admin_router.callback_query(F.data == "gw:a:hist")
async def adm_hist(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    await _list(cb, row, ("ended", "cancelled"), "🏁 <b>تاریخچه</b>", "هنوز قرعه‌کشیِ پایان‌یافته‌ای نیست.")
    await cb.answer()


async def _detail(event, bot: Bot, row, gid: int, note: str = "") -> None:
    g = await E.get_giveaway(gid, row.id)
    if g is None:
        return await edit_or_send(event, "پیدا نشد.", kb(back_row(MENU)))
    prizes = await E.prizes_of(gid)
    n = await E.count_entries(gid)
    ch = await E.total_chances(gid)
    text = card_text(g, prizes, n) + f"\n🎯 مجموعِ شانس‌ها: <b>{fa(ch)}</b>"
    if g.ends_at and g.status == "active":
        pass
    if note:
        text = note + "\n\n" + text
    if g.status == "active":
        rows = [[btn("🏁 پایان و قرعه‌کشی", f"gw:a:end:{gid}", style=ButtonStyle.SUCCESS)],
                [btn("📣 اعلان به گروه/کانال", f"gw:a:post:{gid}", style=ButtonStyle.PRIMARY),
                 btn("📨 اطلاعیه به همه", f"gw:a:bc:{gid}", style=ButtonStyle.PRIMARY)],
                [btn("⏰ زمانِ پایان", f"gw:a:time:{gid}", style=ButtonStyle.PRIMARY),
                 btn("👥 فهرست (CSV)", f"gw:a:csv:{gid}", style=ButtonStyle.PRIMARY)],
                [btn("❌ لغوِ قرعه‌کشی", f"gw:a:xl:{gid}", style=ButtonStyle.DANGER)]]
    else:
        rows = [[btn("🏆 جزئیاتِ برندگان", f"gw:a:win:{gid}", style=ButtonStyle.SUCCESS)] if g.status == "ended" else [],
                [btn("👥 فهرست (CSV)", f"gw:a:csv:{gid}", style=ButtonStyle.PRIMARY)]]
    rows.append(back_row("gw:a:act" if g.status == "active" else "gw:a:hist"))
    await edit_or_send(event, text, kb(*rows))


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:g:\d+$"))
async def adm_detail(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await _detail(cb, bot, row, int(cb.data.split(":")[3]))
    await cb.answer()


# ═══════════════════════ پایان و قرعه ═══════════════════════

@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:end:\d+$"))
async def adm_end_ask(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "active":
        return await cb.answer("این قرعه‌کشی فعال نیست.", show_alert=True)
    n = await E.count_entries(gid)
    await edit_or_send(cb, f"🏁 <b>پایانِ قرعه‌کشی «{esc(g.title)}»</b>\n\n👥 شرکت‌کنندگان: {fa(n)}\n"
                           f"🏆 برنده‌ها: {fa(g.winners_count)}\n\nلفت‌دهنده‌هایِ کانالِ اجباری حذف می‌شوند، برنده‌ها وزنی انتخاب و "
                           "جایزه‌ها برایشان ارسال می‌شود. <b>برگشت‌پذیر نیست.</b> ادامه؟",
                       kb([btn("✅ بله، قرعه‌کشی کن", f"gw:a:endok:{gid}", style=ButtonStyle.SUCCESS)],
                          back_row(f"gw:a:g:{gid}", "❌ انصراف")))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:endok:\d+$"))
async def adm_end_do(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    await cb.answer("⏳ در حالِ انجام…")
    await edit_or_send(cb, "⏳ <b>در حالِ چکِ لفت، قرعه‌کشی و ارسالِ جایزه‌ها…</b>\n(برایِ شرکت‌کنندهٔ زیاد ممکن است چند دقیقه طول بکشد)", None)
    res = await A.finish_giveaway(bot, row, gid, notify_admin_chat=cb.from_user.id)
    if not res.ok:
        return await edit_or_send(cb, "⚠️ این قرعه‌کشی قبلاً پایان یافته یا پیدا نشد.", kb(back_row(f"gw:a:g:{gid}")))
    await edit_or_send(cb, "✅ <b>قرعه‌کشی انجام شد.</b> گزارشِ کامل در پیامِ بعدی است.",
                       kb([btn("🏆 جزئیاتِ برندگان", f"gw:a:win:{gid}", style=ButtonStyle.SUCCESS)], back_row(MENU, "⬅️ منویِ قرعه‌کشی")))


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:win:\d+$"))
async def adm_winners(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None:
        return await cb.answer()
    ws = await E.winners_of(gid)
    lines = [f"🏆 <b>برندگانِ «{esc(g.title)}»</b> (جزئیاتِ مدیریتی)", ""]
    for w, e, p in ws:
        who = esc(e.name or "کاربر") + (f" @{esc(e.username)}" if e.username else "")
        lines.append(f"{medal(w.rank)} <a href=\"tg://user?id={e.user_tg}\">{who}</a> — <code>{e.user_tg}</code>"
                     f"\n    🪪 <code>{esc(e.code)}</code>" + (f" · ☎️ {esc(e.phone)}" if e.phone else "")
                     + (f"\n    🎁 {esc(p.title)}" if p else "") + f" · {'✅ تحویل‌شده' if w.delivered else '⚠️ تحویل‌نشده'}")
    if not ws:
        lines.append("برنده‌ای نبود.")
    pending = any(not w.delivered for w, _, _ in ws)
    rows = [[btn("🔁 ارسالِ مجددِ جوایزِ تحویل‌نشده", f"gw:a:redo:{gid}", style=ButtonStyle.PRIMARY)]] if pending else []
    rows += [[btn("📣 ارسالِ نتایج به گروه/کانال", f"gw:a:pubres:{gid}", style=ButtonStyle.PRIMARY)], back_row(f"gw:a:g:{gid}")]
    await edit_or_send(cb, "\n".join(lines), kb(*rows))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:redo:\d+$"))
async def adm_redo(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    ok, bad = await A.redeliver_pending(bot, row, int(cb.data.split(":")[3]))
    await cb.answer(f"✅ {ok} ارسال شد، ⚠️ {bad} ناموفق" if (ok or bad) else "موردِ تحویل‌نشده‌ای نیست.", show_alert=True)


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:pubres:\d+$"))
async def adm_pub_results(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    chats = await A.saved_chats(row.id)
    if g is None or g.status != "ended" or not chats:
        return await cb.answer("مقصدی ثبت نشده یا قرعه‌کشی پایان نیافته.", show_alert=True)
    ws = await E.winners_of(gid)
    async with Session() as s:
        left = len((await s.scalars(select(GiveawayEntry.id).where(GiveawayEntry.giveaway_id == gid,
                                                                    GiveawayEntry.left == True))).all())  # noqa: E712
    text = results_text(g, ws, await E.count_entries(gid), left, public=True)
    ok = 0
    for c in chats:
        try:
            await bot.send_message(c.chat_id, text, parse_mode="HTML")
            ok += 1
        except Exception as e:      # noqa: BLE001
            logger.info("giveaway: ارسالِ نتایج به %s نشد: %s", c.chat_id, e)
    await cb.answer(f"✅ به {ok} مقصد فرستاده شد.", show_alert=True)


# ═══════════════════════ سایرِ عملیات روی قرعه‌کشیِ فعال ═══════════════════════

@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:post:\d+$"))
async def adm_post(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "active":
        return await cb.answer("فعال نیست.", show_alert=True)
    if not await A.saved_chats(row.id):
        await cb.answer("اول یک گروه/کانال اضافه کن.", show_alert=True)
        return await _chats(cb, row)
    ok, errs = await A.post_announcement(bot, row, g)
    await cb.answer(f"✅ در {ok} مقصد ارسال شد" + (f" | ⚠️ {len(errs)} خطا" if errs else ""), show_alert=True)


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:bc:\d+$"))
async def adm_bc_ask(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    n = len(await A.user_ids(row.id))
    await edit_or_send(cb, f"📨 اطلاعیهٔ قرعه‌کشی برایِ <b>{fa(n)}</b> کاربرِ ربات ارسال شود؟",
                       kb([btn("✅ ارسال", f"gw:a:bcok:{gid}", style=ButtonStyle.SUCCESS)], back_row(f"gw:a:g:{gid}", "❌ انصراف")))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:bcok:\d+$"))
async def adm_bc_do(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "active":
        return await cb.answer("فعال نیست.", show_alert=True)
    asyncio.create_task(A.announce_start(bot, row, g))
    await cb.answer("📨 ارسال شروع شد.", show_alert=True)
    await _detail(cb, bot, row, gid)


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:csv:\d+$"))
async def adm_csv(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None:
        return await cb.answer()
    async with Session() as s:
        es = list((await s.scalars(select(GiveawayEntry).where(GiveawayEntry.giveaway_id == gid)
                                   .order_by(GiveawayEntry.id))).all())
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["telegram_id", "name", "username", "phone", "chances", "invited_by", "code", "left", "joined_at_utc"])
    for e in es:
        wr.writerow([e.user_tg, e.name or "", e.username or "", e.phone or "", E.chances(e.bonus), e.invited_by or "",
                     e.code, int(bool(e.left)), e.joined_at.strftime("%Y-%m-%d %H:%M") if e.joined_at else ""])
    await cb.message.answer_document(BufferedInputFile(("\ufeff" + buf.getvalue()).encode("utf-8"),
                                                       filename=f"giveaway_{gid}_entries.csv"),
                                     caption=f"👥 شرکت‌کنندگانِ «{esc(g.title)}»: {fa(len(es))} نفر", parse_mode="HTML")
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:xl:\d+$"))
async def adm_cancel_ask(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    await edit_or_send(cb, "❌ این قرعه‌کشی <b>لغو</b> شود؟ (برنده‌ای انتخاب نمی‌شود)",
                       kb([btn("✅ بله، لغو کن", f"gw:a:xlok:{gid}", style=ButtonStyle.DANGER)], back_row(f"gw:a:g:{gid}", "↩️ نه")))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:xlok:\d+$"))
async def adm_cancel_do(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    ok = await E.cancel(gid, row.id)
    g = await E.get_giveaway(gid, row.id)
    if ok and g:
        await A.refresh_posts(bot, g, f"⛔️ <b>قرعه‌کشیِ «{esc(g.title)}» لغو شد.</b>", None)
    await cb.answer("لغو شد." if ok else "فعال نیست.")
    await _detail(cb, bot, row, gid)


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:time:\d+$"))
async def adm_time(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[3])
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "active":
        return await cb.answer("فعال نیست.", show_alert=True)
    await state.set_state(None)
    await edit_or_send(cb, f"⏰ <b>زمانِ پایانِ «{esc(g.title)}»</b>\n\nالان: {fmt_time(g.ends_at) if g.ends_at else 'دستی'}"
                           + (f" ({time_left(g.ends_at)} مانده)" if g.ends_at else ""),
                       kb([btn("+۱ ساعت", f"gw:a:tset:{gid}:p1", style=ButtonStyle.PRIMARY),
                           btn("+۶ ساعت", f"gw:a:tset:{gid}:p6", style=ButtonStyle.PRIMARY),
                           btn("+۲۴ ساعت", f"gw:a:tset:{gid}:p24", style=ButtonStyle.PRIMARY)],
                          [btn("پایان تا ۱ ساعتِ دیگر", f"gw:a:tset:{gid}:s1", style=ButtonStyle.PRIMARY),
                           btn("پایان تا ۲۴ ساعتِ دیگر", f"gw:a:tset:{gid}:s24", style=ButtonStyle.PRIMARY)],
                          [btn("✏️ زمانِ دلخواه", f"gw:a:tset:{gid}:c", style=ButtonStyle.SUCCESS)],
                          [btn("🗑 بدونِ پایانِ خودکار", f"gw:a:tset:{gid}:n", style=ButtonStyle.DANGER)],
                          back_row(f"gw:a:g:{gid}")))
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:tset:\d+:\w+$"))
async def adm_time_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, _, gid_s, what = cb.data.split(":")
    gid = int(gid_s)
    if what == "c":
        await state.set_state(GwAdminFSM.time_edit)
        await state.update_data(gw_edit=gid)
        await cb.answer()
        return await edit_or_send(cb, "✏️ از «الان» تا پایان چقدر؟ مثل <code>6h</code> یا <code>2d</code>", kb(back_row(f"gw:a:time:{gid}")))
    async with Session() as s:
        g = await s.get(E.Giveaway, gid)
        if g is None or g.bot_id != row.id or g.status != "active":
            return await cb.answer("فعال نیست.", show_alert=True)
        now = datetime.utcnow()
        if what == "n":
            g.ends_at = None
        elif what.startswith("p"):
            g.ends_at = (g.ends_at if g.ends_at and g.ends_at > now else now) + timedelta(hours=int(what[1:]))
        elif what.startswith("s"):
            g.ends_at = now + timedelta(hours=int(what[1:]))
        await s.commit()
    await cb.answer("✅ ذخیره شد")
    await _detail(cb, bot, row, gid)


@giveaway_admin_router.message(GwAdminFSM.time_edit, F.text)
async def adm_time_edit_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    h = parse_hours(msg.text)
    if h is None:
        return await msg.answer("❌ فرمت نامعتبر. مثال: <code>6h</code> یا <code>2d</code>", parse_mode="HTML")
    gid = (await state.get_data()).get("gw_edit")
    await state.clear()
    async with Session() as s:
        g = await s.get(E.Giveaway, gid)
        if g is not None and g.bot_id == row.id and g.status == "active":
            g.ends_at = datetime.utcnow() + timedelta(hours=h)
            await s.commit()
    await _detail(msg, bot, row, gid, note="✅ زمانِ پایان تغییر کرد.")


# ═══════════════════════ مقصدهایِ اعلان ═══════════════════════

async def _chats(event, row) -> None:
    cs = await A.saved_chats(row.id)
    rows = [[btn(f"{'📣' if c.kind == 'channel' else '👥'} {(c.title or c.chat_id)}"[:40], "noop"),
             btn("🗑", f"gw:a:chdel:{c.id}", style=ButtonStyle.DANGER)] for c in cs]
    await edit_or_send(event, "📣 <b>گروه/کانال‌هایِ اعلان</b>\n\nاعلانِ شروع (با دکمهٔ شرکت) و نتایج به این مقصدها می‌رود. "
                              "ربات باید آن‌جا <b>ادمین</b> باشد."
                       + ("" if cs else "\n\nهنوز مقصدی اضافه نشده."),
                       kb(*rows, [btn("➕ افزودنِ گروه/کانال", "gw:a:chadd", style=ButtonStyle.SUCCESS)], back_row(MENU)))


@giveaway_admin_router.callback_query(F.data == "gw:a:chats")
async def adm_chats(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(None)
    await _chats(cb, row)
    await cb.answer()


@giveaway_admin_router.callback_query(F.data == "gw:a:chadd")
async def adm_chat_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.set_state(GwAdminFSM.chat_add)
    await edit_or_send(cb, "➕ <b>افزودنِ مقصد</b>\n\n• یک پیام از همان گروه/کانال برایِ من فوروارد کن\n"
                           "• یا <code>@username</code>\n• یا آیدیِ عددی <code>-100…</code>\n\nقبلش رباتِ خودت را آن‌جا ادمین کن.",
                       kb(back_row("gw:a:chats")))
    await cb.answer()


@giveaway_admin_router.message(GwAdminFSM.chat_add)
async def adm_chat_add_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return
    from tenant.shared.backup_full import _dest_from_message
    target = _dest_from_message(msg)
    if target is None:
        return await msg.answer("❌ شناخته نشد. فوروارد کن یا @username / آیدیِ عددی بفرست.")
    try:
        chat = await bot.get_chat(target)
        await bot.send_message(chat.id, "✅ این‌جا به‌عنوانِ مقصدِ اعلانِ قرعه‌کشی ثبت شد.")
    except Exception as e:      # noqa: BLE001
        logger.info("giveaway chat add: %s", e)
        return await msg.answer("❌ ارسال به این مقصد ممکن نشد؛ رباتِ خودت را ادمین کن (با اجازهٔ ارسالِ پیام) و دوباره بفرست.")
    async with Session() as s:
        if await s.scalar(select(GiveawayChat.id).where(GiveawayChat.bot_id == row.id, GiveawayChat.chat_id == chat.id)) is None:
            s.add(GiveawayChat(bot_id=row.id, chat_id=chat.id, title=(chat.title or chat.username or str(chat.id))[:120],
                               kind="channel" if chat.type == "channel" else "group"))
            await s.commit()
    await state.clear()
    await _chats(msg, row)


@giveaway_admin_router.callback_query(F.data.regexp(r"^gw:a:chdel:\d+$"))
async def adm_chat_del(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        c = await s.get(GiveawayChat, int(cb.data.split(":")[3]))
        if c is not None and c.bot_id == row.id:
            await s.delete(c)
            await s.commit()
    await cb.answer("حذف شد.")
    await _chats(cb, row)


# ═══════════════════════ تنظیمات ═══════════════════════

_SETTINGS = {
    "gw_post_on_create": ("📣 اعلانِ خودکار در گروه/کانال بعد از ساخت", "1"),
    "gw_start_direct": ("🎰 /start مستقیم قرعه‌کشیِ فعال را نشان بدهد", "1"),
}


async def _settings(event, row) -> None:
    from tenant import get_setting
    rows = []
    for key, (label, dflt) in _SETTINGS.items():
        on = (await get_setting(row.id, key, dflt)) == "1"
        rows.append([btn(f"{label}: {'✅' if on else '⛔️'}", f"gw:a:st:{key}", style=ButtonStyle.PRIMARY)])
    await edit_or_send(event, "⚙️ <b>تنظیماتِ قرعه‌کشی</b>", kb(*rows, back_row(MENU)))


@giveaway_admin_router.callback_query(F.data == "gw:a:set")
async def adm_settings(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    await _settings(cb, row)
    await cb.answer()


@giveaway_admin_router.callback_query(F.data.startswith("gw:a:st:"))
async def adm_setting_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _guard(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 3)[3]
    if key not in _SETTINGS:
        return await cb.answer()
    cur = await get_setting(row.id, key, _SETTINGS[key][1])
    await set_setting(row.id, key, "0" if cur == "1" else "1")
    await cb.answer("ذخیره شد")
    await _settings(cb, row)


# ═══════════════════════ دستورهایِ میان‌بر در پیوی ═══════════════════════

@giveaway_admin_router.message(Command("gwadmin"), F.chat.type == "private")
async def cmd_gwadmin(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row or row.field != "giveaway":
        return
    await state.clear()
    await show_menu(msg, bot, row)


@giveaway_admin_router.message(Command("cancel"), StateFilter(GwAdminFSM))
async def cmd_cancel(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    await state.clear()
    if row:
        await show_menu(msg, bot, row)
