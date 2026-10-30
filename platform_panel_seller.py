"""
platform_panel_seller.py — دروازهٔ «در حال تست» برایِ ساختِ رباتِ پنل‌فروشی (PATCH_PANEL_SELLER_V1).

• هر کاربر می‌تواند «درخواستِ دسترسی» بدهد؛ مدیرِ اصلی با دکمه (۳/۷/۳۰ روز، دائمی) یا دستور مدت را تعیین می‌کند.
• دستورها (فقط مدیرِ اصلی، چتِ خصوصی):  /pselgrant <آیدی> [روز]   ·   /pselrevoke <آیدی>   ·   /pselist
• ذخیره و منطقِ مدت در tenant/bots/panel_seller/gate.py است.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import PLATFORM_ADMIN_ID

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panel_seller_gate_router = Router(name="panel_seller_gate")


def _is_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and int(uid) == int(PLATFORM_ADMIN_ID)


def _btn(text, cb, style=None):
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


async def create_gate(cb: CallbackQuery) -> bool:
    """True = اجازهٔ ادامه؛ وگرنه صفحهٔ «در حال تست + ثبتِ درخواست» نشان داده می‌شود."""
    from tenant.bots.panel_seller import gate
    ok, why = await gate.status(cb.from_user.id)
    if ok:
        return True
    head = "⏰ مدتِ تستِ قبلیِ تو تمام شده.\n\n" if why == "expired" else ""
    text = (head + "🧩 <b>ربات پنل‌فروشی</b> — 🧪 <i>در حال تست</i>\n\n"
            "این نوعِ ربات الان در <b>مرحلهٔ تست</b> است و فقط برایِ چند نفر که مدیر دسترسی بدهد باز است "
            "(آن هم فقط در پلنِ حرفه‌ای). بعد از تست و رفعِ ایرادها برایِ همه فعال می‌شود.\n"
            "می‌توانی درخواستِ دسترسی ثبت کنی؛ بعد از تأییدِ مدیر خبرت می‌کنیم.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📨 ثبتِ درخواست", "pselreq", ButtonStyle.SUCCESS)],
        [_btn("⬅️ بازگشت", "freerules:cancel", ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    return False


@panel_seller_gate_router.callback_query(F.data == "pselreq")
async def request_access(cb: CallbackQuery):
    u = cb.from_user
    if not PLATFORM_ADMIN_ID:
        return await cb.answer("ثبتِ درخواست فعلاً ممکن نیست.", show_alert=True)
    who = f"{u.full_name} (@{u.username})" if u.username else u.full_name
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("۳ روز", f"pselg:{u.id}:3", ButtonStyle.SUCCESS), _btn("۷ روز", f"pselg:{u.id}:7", ButtonStyle.SUCCESS),
         _btn("۳۰ روز", f"pselg:{u.id}:30", ButtonStyle.SUCCESS)],
        [_btn("♾ دائمی", f"pselg:{u.id}:0", ButtonStyle.PRIMARY), _btn("❌ رد", f"pselg:{u.id}:-1", ButtonStyle.DANGER)]])
    try:
        await cb.bot.send_message(PLATFORM_ADMIN_ID, f"📥 درخواستِ دسترسیِ «ربات پنل‌فروشی (تست)»\n{who}\n🆔 <code>{u.id}</code>",
                                  reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: ارسالِ درخواست به مدیر ناموفق بود")
        return await cb.answer("ثبتِ درخواست ممکن نشد؛ بعداً امتحان کن.", show_alert=True)
    await cb.answer("✅ درخواستت برایِ مدیر رفت.", show_alert=True)


@panel_seller_gate_router.callback_query(F.data.startswith("pselg:"))
async def admin_decide(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    from tenant.bots.panel_seller import gate
    _, tg, days = cb.data.split(":")
    tg, days = int(tg), int(days)
    if days < 0:
        txt, note = "❌ رد شد.", "❌ درخواستِ دسترسیِ تو به «ربات پنل‌فروشی» رد شد."
    else:
        await gate.grant(tg, days)
        span = "دائمی" if days == 0 else f"{days} روز"
        txt, note = f"✅ دسترسی داده شد ({span}).", f"✅ دسترسیِ تست به «ربات پنل‌فروشی» برایِ تو فعال شد ({span}). از «ساخت ربات» بساز."
    try:
        await cb.bot.send_message(tg, note)
    except Exception:      # noqa: BLE001
        logger.info("panel_seller: خبرِ تصمیم به کاربر نرسید")
    try:
        await cb.message.edit_text(cb.message.text + f"\n\n{txt}")
    except Exception:      # noqa: BLE001
        pass
    await cb.answer(txt)


@panel_seller_gate_router.message(Command("pselgrant"), F.chat.type == "private")
async def cmd_grant(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.panel_seller import gate
    parts = (msg.text or "").split()[1:]
    if not parts or not parts[0].lstrip("-").isdigit():
        return await msg.answer("فرمت: <code>/pselgrant آیدی-عددی [روز]</code> (بدونِ روز = دائمی)", parse_mode="HTML")
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    await gate.grant(int(parts[0]), days)
    await msg.answer(f"✅ دسترسی داده شد: <code>{parts[0]}</code> — {'دائمی' if not days else f'{days} روز'}", parse_mode="HTML")


@panel_seller_gate_router.message(Command("pselrevoke"), F.chat.type == "private")
async def cmd_revoke(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.panel_seller import gate
    parts = (msg.text or "").split()[1:]
    if not parts or not parts[0].lstrip("-").isdigit():
        return await msg.answer("فرمت: <code>/pselrevoke آیدی-عددی</code>", parse_mode="HTML")
    had = await gate.revoke(int(parts[0]))
    await msg.answer("🗑 دسترسی برداشته شد." if had else "این آیدی دسترسی نداشت.")


@panel_seller_gate_router.message(Command("pselist"), F.chat.type == "private")
async def cmd_list(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.panel_seller import gate
    g = await gate.grants()
    if not g:
        return await msg.answer("هیچ دسترسیِ ثبت‌شده‌ای نیست.")
    lines = [f"• <code>{k}</code> — {'دائمی' if not v else v[:16].replace('T', ' ')}" for k, v in g.items()]
    await msg.answer("🧩 دسترسی‌هایِ «ربات پنل‌فروشی»:\n" + "\n".join(lines), parse_mode="HTML")
