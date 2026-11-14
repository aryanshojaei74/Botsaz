"""
platform_booking.py — دروازهٔ «در حال تست» برایِ ساختِ رباتِ رزرو و نوبت‌دهی (PATCH_BOOKING_V2).

• هر کاربر می‌تواند «درخواستِ دسترسی» بدهد؛ مدیرِ اصلی با دکمه (۴/۷/۳۰ روز، دائمی) یا دستور مدت را تعیین می‌کند.
• دستورها (فقط مدیرِ اصلی، چتِ خصوصی):  /bookgrant <آیدی> [روز]   ·   /bookrevoke <آیدی>   ·   /booklist
• دسترسیِ چندروزه = ساختِ رایگانِ «پلنِ حرفه‌ای» با همان مدت؛ بعد از پایانِ مدت ربات طبقِ قانونِ عادیِ پلن بایگانی می‌شود.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import PLATFORM_ADMIN_ID

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

booking_gate_router = Router(name="booking_gate")


def _is_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and int(uid) == int(PLATFORM_ADMIN_ID)


def _btn(text, cb, style=None):
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


async def ensure_trial(tg_id: int, name: str = "") -> bool:
    """دسترسیِ محدود (روزی) ⇒ تستِ رایگانِ پلنِ حرفه‌ای با همان مدت (تا ساختِ ربات بدونِ پرداخت ممکن باشد)."""
    from tenant.bots.booking import gate
    days = await gate.remaining_days(tg_id)
    if not days:
        return False
    import plan_trials
    st, _info = await plan_trials.grant_direct(int(tg_id), name or "", "pro", days)
    return st in ("ok", "exists_active")


async def create_gate(cb: CallbackQuery) -> bool:
    """True = اجازهٔ ادامه؛ وگرنه صفحهٔ «در حال تست + ثبتِ درخواست» نشان داده می‌شود."""
    from tenant.bots.booking import gate
    ok, why = await gate.status(cb.from_user.id)
    if ok:
        return True
    head = "⏰ مدتِ تستِ قبلیِ تو تمام شده.\n\n" if why == "expired" else ""
    text = (head + "📅 <b>ربات رزرو و نوبت‌دهی</b> — 🧪 <i>در حال تست</i>\n\n"
            "مشتری خدمات را می‌بیند، روز و ساعتِ خالی را انتخاب می‌کند و نوبتش ثبت می‌شود؛ مالک مشخصاتِ مشتری را می‌گیرد "
            "و تأیید/رد می‌کند. یادآوریِ خودکار، ساعتِ کاری، پرسنل و روزهایِ تعطیل هم دارد.\n\n"
            "این نوعِ ربات الان در <b>مرحلهٔ تست</b> است و فقط برایِ چند نفر که مدیر دسترسی بدهد باز است "
            "(آن هم فقط در پلنِ حرفه‌ای). بعد از تست و رفعِ ایرادها برایِ همه فعال می‌شود.\n"
            "می‌توانی درخواستِ دسترسی ثبت کنی؛ بعد از تأییدِ مدیر خبرت می‌کنیم.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📨 ثبتِ درخواست", "bookreq", ButtonStyle.SUCCESS)],
        [_btn("⬅️ بازگشت", "freerules:cancel", ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()
    return False


@booking_gate_router.callback_query(F.data == "bookreq")
async def request_access(cb: CallbackQuery):
    u = cb.from_user
    if not PLATFORM_ADMIN_ID:
        return await cb.answer("ثبتِ درخواست فعلاً ممکن نیست.", show_alert=True)
    who = f"{u.full_name} (@{u.username})" if u.username else u.full_name
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("۴ روز", f"bookg:{u.id}:4", ButtonStyle.SUCCESS), _btn("۷ روز", f"bookg:{u.id}:7", ButtonStyle.SUCCESS),
         _btn("۳۰ روز", f"bookg:{u.id}:30", ButtonStyle.SUCCESS)],
        [_btn("♾ دائمی", f"bookg:{u.id}:0", ButtonStyle.PRIMARY), _btn("❌ رد", f"bookg:{u.id}:-1", ButtonStyle.DANGER)]])
    try:
        await cb.bot.send_message(PLATFORM_ADMIN_ID, f"📥 درخواستِ دسترسیِ «ربات رزرو و نوبت‌دهی (تست)»\n{who}\n🆔 <code>{u.id}</code>",
                                  reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.exception("booking: ارسالِ درخواست به مدیر ناموفق بود")
        return await cb.answer("ثبتِ درخواست ممکن نشد؛ بعداً امتحان کن.", show_alert=True)
    await cb.answer("✅ درخواستت برایِ مدیر رفت.", show_alert=True)


@booking_gate_router.callback_query(F.data.startswith("bookg:"))
async def admin_decide(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیرِ اصلی.", show_alert=True)
    from tenant.bots.booking import gate
    _, tg, days = cb.data.split(":")
    tg, days = int(tg), int(days)
    if days < 0:
        txt, note = "❌ رد شد.", "❌ درخواستِ دسترسیِ تو به «ربات رزرو و نوبت‌دهی» رد شد."
    else:
        await gate.grant(tg, days)
        if days > 0:
            try:
                await ensure_trial(tg)
            except Exception:      # noqa: BLE001
                logger.exception("booking: ساختِ تستِ رایگانِ پلنِ حرفه‌ای ناموفق بود")
        span = "دائمی" if days == 0 else f"{days} روز"
        txt, note = (f"✅ دسترسی داده شد ({span}).",
                     f"✅ دسترسیِ تست به «ربات رزرو و نوبت‌دهی» برایِ تو فعال شد ({span}). از «ساخت ربات» بساز و اگر باگی دیدی یا امکانی خواستی به ما بگو.")
    try:
        await cb.bot.send_message(tg, note)
    except Exception:      # noqa: BLE001
        logger.info("booking: خبرِ تصمیم به کاربر نرسید")
    try:
        await cb.message.edit_text(cb.message.text + f"\n\n{txt}")
    except Exception:      # noqa: BLE001
        pass
    await cb.answer(txt)


@booking_gate_router.message(Command("bookgrant"), F.chat.type == "private")
async def cmd_grant(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.booking import gate
    parts = (msg.text or "").split()[1:]
    if not parts or not parts[0].lstrip("-").isdigit():
        return await msg.answer("فرمت: <code>/bookgrant آیدی-عددی [روز]</code> (بدونِ روز = دائمی)", parse_mode="HTML")
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    await gate.grant(int(parts[0]), days)
    if days > 0:
        try:
            await ensure_trial(int(parts[0]))
        except Exception:      # noqa: BLE001
            logger.exception("booking: ساختِ تستِ رایگانِ پلنِ حرفه‌ای ناموفق بود")
    await msg.answer(f"✅ دسترسی داده شد: <code>{parts[0]}</code> — {'دائمی' if not days else f'{days} روز'}", parse_mode="HTML")


@booking_gate_router.message(Command("bookrevoke"), F.chat.type == "private")
async def cmd_revoke(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.booking import gate
    parts = (msg.text or "").split()[1:]
    if not parts or not parts[0].lstrip("-").isdigit():
        return await msg.answer("فرمت: <code>/bookrevoke آیدی-عددی</code>", parse_mode="HTML")
    had = await gate.revoke(int(parts[0]))
    await msg.answer("🗑 دسترسی برداشته شد." if had else "این آیدی دسترسی نداشت.")


@booking_gate_router.message(Command("booklist"), F.chat.type == "private")
async def cmd_list(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    from tenant.bots.booking import gate
    g = await gate.grants()
    if not g:
        return await msg.answer("هیچ دسترسیِ ثبت‌شده‌ای نیست.")
    lines = [f"• <code>{k}</code> — {'دائمی' if not v else v[:16].replace('T', ' ')}" for k, v in g.items()]
    await msg.answer("📅 دسترسی‌هایِ «ربات رزرو و نوبت‌دهی»:\n" + "\n".join(lines), parse_mode="HTML")
