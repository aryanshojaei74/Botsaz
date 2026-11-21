"""
platform_feedback_admin.py — صفحهٔ «📩 نظرات کاربران» در پنلِ مدیریت.

=== PATCH_FEEDBACK_ADMIN_V1 ===
باگی که check_health.py پیدا کرد: دکمهٔ «📩 نظرات کاربران» در پنلِ
مدیریت وجود داشت ولی *هیچ هندلری* نداشت — کاربران نظر و باگ می‌فرستادند،
در جدولِ platform_feedbacks ذخیره می‌شد، و هیچ‌کس نمی‌توانست ببیندشان.

جدولِ PlatformFeedback از قبل ستونِ admin_reply و replied_at دارد، پس
پاسخ‌دادن هم پیش‌بینی شده بوده و فقط رابطش ساخته نشده.

طبقِ قاعدهٔ پروژه ماژولِ جدا است، نه افزودن به platform_bot.py.
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, func

from config import Session, PLATFORM_ADMIN_ID
from models import PlatformFeedback

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

feedback_admin_router = Router()

PAGE_SIZE = 8

TYPE_FA = {"bug": "🐞 باگ", "suggest": "💡 پیشنهاد", "review": "⭐️ نظر"}


class FbFSM(StatesGroup):
    reply = State()


def _admin_only(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


def _fmt(dt: datetime.datetime | None) -> str:
    if not dt:
        return "—"
    try:
        from partner_rules import jalali
        return jalali(dt, with_time=True)
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


# ────────── فهرست ──────────

@feedback_admin_router.callback_query(F.data == "adm:feedbacks")
async def fb_home(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await _show_list(cb, "all", 0)


@feedback_admin_router.callback_query(F.data.startswith("fba:list:"))
async def fb_list(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    parts = cb.data.split(":")
    filt = parts[2] if len(parts) > 2 else "all"
    try:
        offset = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        offset = 0
    await _show_list(cb, filt, offset)


async def _show_list(cb: CallbackQuery, filt: str, offset: int) -> None:
    async with Session() as s:
        base = select(PlatformFeedback)
        if filt in ("bug", "suggest", "review"):
            base = base.where(PlatformFeedback.fb_type == filt)
        elif filt == "unanswered":
            base = base.where(PlatformFeedback.admin_reply.is_(None))

        total = await s.scalar(
            select(func.count()).select_from(base.subquery())) or 0
        rows = (await s.scalars(
            base.order_by(PlatformFeedback.id.desc())
            .offset(offset).limit(PAGE_SIZE))).all()
        items = [(r.id, r.fb_type, r.user_name, r.text,
                  bool(r.admin_reply), r.created_at) for r in rows]

        counts = {}
        for k in ("bug", "suggest", "review"):
            counts[k] = await s.scalar(
                select(func.count(PlatformFeedback.id))
                .where(PlatformFeedback.fb_type == k)) or 0
        counts["unanswered"] = await s.scalar(
            select(func.count(PlatformFeedback.id))
            .where(PlatformFeedback.admin_reply.is_(None))) or 0

    b = InlineKeyboardBuilder()
    # فیلترها
    b.button(text=f"همه ({sum(counts[k] for k in ('bug','suggest','review'))})",
             callback_data="fba:list:all:0",
             style=ButtonStyle.SUCCESS if filt == "all" else ButtonStyle.PRIMARY)
    b.button(text=f"🐞 باگ ({counts['bug']})", callback_data="fba:list:bug:0",
             style=ButtonStyle.SUCCESS if filt == "bug" else ButtonStyle.PRIMARY)
    b.button(text=f"💡 پیشنهاد ({counts['suggest']})",
             callback_data="fba:list:suggest:0",
             style=ButtonStyle.SUCCESS if filt == "suggest" else ButtonStyle.PRIMARY)
    b.button(text=f"⭐️ نظر ({counts['review']})",
             callback_data="fba:list:review:0",
             style=ButtonStyle.SUCCESS if filt == "review" else ButtonStyle.PRIMARY)
    b.button(text=f"📭 بی‌پاسخ ({counts['unanswered']})",
             callback_data="fba:list:unanswered:0",
             style=ButtonStyle.DANGER if filt != "unanswered" else ButtonStyle.SUCCESS)
    b.adjust(2, 2, 1)

    if not items:
        b.button(text="🔙 بازگشت", callback_data="adm:back",
                 style=ButtonStyle.DANGER)
        b.adjust(2, 2, 1, 1)
        await cb.message.edit_text(
            "📩 <b>نظرات کاربران</b>\n\nدر این دسته چیزی ثبت نشده است.",
            reply_markup=b.as_markup())
        await cb.answer()
        return

    for fid, ftype, uname, text, replied, _created in items:
        mark = "✅" if replied else "📭"
        preview = (text or "").replace("\n", " ")[:26]
        b.button(text=f"{mark} {TYPE_FA.get(ftype, ftype)} — {preview}",
                 callback_data=f"fba:view:{fid}",
                 style=ButtonStyle.PRIMARY)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"fba:list:{filt}:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(("بعدی ➡️", f"fba:list:{filt}:{offset + PAGE_SIZE}"))
    for t, c in nav:
        b.button(text=t, callback_data=c, style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="adm:back",
             style=ButtonStyle.DANGER)
    b.adjust(2, 2, 1, *([1] * len(items)), len(nav) or 1, 1)

    await cb.message.edit_text(
        f"📩 <b>نظرات کاربران</b> ({total} مورد)\n\n"
        "📭 بی‌پاسخ · ✅ پاسخ داده شده",
        reply_markup=b.as_markup())
    await cb.answer()


# ────────── نمایش یک مورد ──────────

@feedback_admin_router.callback_query(F.data.startswith("fba:view:"))
async def fb_view(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    fid = int(cb.data.split(":")[2])
    async with Session() as s:
        fb = await s.get(PlatformFeedback, fid)
        if not fb:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        d = (fb.fb_type, fb.user_telegram_id, fb.user_name, fb.text,
             fb.admin_reply, fb.created_at, fb.replied_at)

    ftype, uid, uname, text, reply, created, replied_at = d

    lines = [
        f"{TYPE_FA.get(ftype, ftype)} — <b>#{fid}</b>\n",
        f"👤 {uname or '—'} (<code>{uid}</code>)",
        f"📅 {_fmt(created)}\n",
        text or "—",
    ]
    if reply:
        lines.append(f"\n🛡 <b>پاسخ شما</b> — {_fmt(replied_at)}\n{reply}")

    b = InlineKeyboardBuilder()
    b.button(text="✍️ پاسخ" if not reply else "✍️ ویرایش پاسخ",
             callback_data=f"fba:reply:{fid}", style=ButtonStyle.SUCCESS)
    b.button(text="🗑 حذف", callback_data=f"fba:del:{fid}",
             style=ButtonStyle.DANGER)
    b.button(text="🔙 بازگشت", callback_data="fba:list:all:0",
             style=ButtonStyle.DANGER)
    b.adjust(2, 1)

    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


# ────────── پاسخ ──────────

@feedback_admin_router.callback_query(F.data.startswith("fba:reply:"))
async def fb_reply_ask(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    fid = int(cb.data.split(":")[2])
    await state.update_data(fb_id=fid)
    await state.set_state(FbFSM.reply)
    await cb.message.answer(
        f"✍️ پاسخت به مورد #{fid} را بنویس.\n"
        "پیام مستقیم برای کاربر ارسال می‌شود.")
    await cb.answer()


@feedback_admin_router.message(FbFSM.reply)
async def fb_reply_send(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    data = await state.get_data()
    fid = data.get("fb_id")
    text = (msg.text or "").strip()
    await state.clear()
    if not fid or not text:
        await msg.answer("پیام نامعتبر بود.")
        return

    async with Session() as s:
        fb = await s.get(PlatformFeedback, fid)
        if not fb:
            await msg.answer("این مورد پیدا نشد.")
            return
        fb.admin_reply = text[:4000]
        fb.replied_at = datetime.datetime.now()
        target = fb.user_telegram_id
        original = (fb.text or "")[:150]
        await s.commit()

    ok = False
    try:
        await bot.send_message(
            target,
            "🛡 <b>پاسخ پشتیبانی بات‌ساز</b>\n\n"
            f"دربارهٔ پیام شما:\n<i>{original}</i>\n\n"
            f"─────────\n{text}")
        ok = True
    except Exception:
        logger.exception("ارسالِ پاسخِ بازخورد به کاربر ناموفق بود")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 بازگشت به فهرست",
                             callback_data="fba:list:all:0",
                             style=ButtonStyle.PRIMARY)]])
    await msg.answer(
        "✅ پاسخ ثبت و ارسال شد." if ok else
        "⚠️ پاسخ ثبت شد ولی ارسال به کاربر ناموفق بود "
        "(شاید ربات را بلاک کرده).",
        reply_markup=kb)


# ────────── حذف ──────────

@feedback_admin_router.callback_query(F.data.startswith("fba:del:"))
async def fb_del_ask(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    fid = int(cb.data.split(":")[2])
    b = InlineKeyboardBuilder()
    b.button(text="🗑 بله، حذف کن", callback_data=f"fba:delok:{fid}",
             style=ButtonStyle.DANGER)
    b.button(text="🔙 انصراف", callback_data=f"fba:view:{fid}",
             style=ButtonStyle.PRIMARY)
    b.adjust(1)
    await cb.message.edit_text(
        f"🗑 مورد #{fid} حذف شود؟ این کار برگشت‌ناپذیر است.",
        reply_markup=b.as_markup())
    await cb.answer()


@feedback_admin_router.callback_query(F.data.startswith("fba:delok:"))
async def fb_del_do(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    fid = int(cb.data.split(":")[2])
    async with Session() as s:
        fb = await s.get(PlatformFeedback, fid)
        if fb:
            await s.delete(fb)
            await s.commit()
    await cb.answer("حذف شد")
    await _show_list(cb, "all", 0)
