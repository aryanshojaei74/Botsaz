"""
platform_promoters.py — «معرفِ فروش» در رباتِ اصلیِ بات‌ساز (PATCH_PLATFORM_PROMOTER_V1).

مدیر برایِ یک نفر (اینفلوئنسر/همکار/دوست) یک لینک و کد می‌سازد و درصدِ تخفیف تعیین می‌کند.
هر کس با آن لینک (t.me/ربات?start=pr_کد) یا با دستورِ «/code کد» بیاید، فقط روی
«خریدِ اولِ» خودش آن درصد تخفیف می‌گیرد؛ بعد از اولین خریدِ پولی تخفیف مصرف می‌شود.

قواعد:
• «خریدِ اول» = کاربر تا حالا هیچ خریدِ پولیِ پلن (PlanOrder با مبلغ > ۰) نداشته باشد.
• تخفیفِ معرف با تخفیفِ رفرالِ خودِ کاربر جمع نمی‌شود؛ بزرگ‌ترِ این دو اعمال می‌شود.
• مصرف یک‌بار برایِ هر کاربر است (UniqueConstraint روی PlatformPromoUse).
• معرفِ اول می‌ماند: اگر کاربر قبلاً با لینکِ دیگری آمده، لینکِ دوم جایگزینش نمی‌شود.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import PlanOrder, PlatformPromoter, PlatformPromoUse, PlatformUser

logger = _get_logger(__name__)

promo_router = Router(name="platform_promoters")

PREFIX = "pr_"
CODE_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
MAX_PERCENT = 90


class PromoFSM(StatesGroup):
    name = State()
    code = State()
    percent = State()
    edit_percent = State()


@dataclass
class PromoInfo:
    promoter_id: int
    name: str
    percent: int


def _hx(*a, **k):
    from premium_emoji import hx_button
    return hx_button(*a, **k)


def _fa(text: str) -> str:
    return (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


# ═══════════════════ منطقِ اصلی ═══════════════════
async def has_paid_before(s, uid: int) -> bool:
    n = await s.scalar(select(func.count()).select_from(PlanOrder).where(
        PlanOrder.user_telegram_id == uid, PlanOrder.amount > 0))
    return bool(n)


async def promo_for(uid: int) -> PromoInfo | None:
    """تخفیفِ معرفِ قابل‌اعمال برایِ این کاربر (یا None)."""
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid))
        if not u or not u.promoter_id:
            return None
        pr = await s.get(PlatformPromoter, u.promoter_id)
        if not pr or not pr.active or int(pr.percent or 0) <= 0:
            return None
        if await s.scalar(select(PlatformPromoUse.id).where(PlatformPromoUse.user_telegram_id == uid)):
            return None
        if await has_paid_before(s, uid):
            return None
        return PromoInfo(pr.id, pr.name, int(pr.percent))


async def attach(uid: int, code: str) -> tuple[bool, str]:
    """کد/لینکِ معرف را به کاربر وصل می‌کند. خروجی: (موفق؟، پیام برایِ کاربر)."""
    code = (code or "").strip()
    async with Session() as s:
        pr = await s.scalar(select(PlatformPromoter).where(
            func.lower(PlatformPromoter.code) == code.lower()))
        if not pr or not pr.active:
            return False, "❌ این کد معتبر نیست یا غیرفعال شده است."
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid))
        if not u:
            return False, "ابتدا /start را بزن."
        if await has_paid_before(s, uid) or await s.scalar(
                select(PlatformPromoUse.id).where(PlatformPromoUse.user_telegram_id == uid)):
            return False, "ℹ️ این تخفیف فقط برایِ «خریدِ اول» است و شما قبلاً خرید داشته‌اید."
        if u.promoter_id and u.promoter_id != pr.id:
            return False, "ℹ️ شما قبلاً با یک کدِ معرفِ دیگر آمده‌اید؛ همان کد فعال می‌ماند."
        u.promoter_id = pr.id
        await s.commit()
        return True, (f"🎁 <b>تخفیفِ خریدِ اول فعال شد!</b>\n\n"
                      f"<b>{int(pr.percent)}٪</b> تخفیف روی اولین خریدِ پلنِ شما اعمال می‌شود "
                      "(خودکار، موقعِ پرداخت).")


async def finalize(s, uid: int, info: PromoInfo | None, paid: int) -> int:
    """مصرفِ تخفیف را در همان تراکنشِ پرداخت ثبت می‌کند (s همان session است؛ commit با فراخواننده).
    مبلغِ تخفیف برگردانده می‌شود؛ اگر قبلاً مصرف شده بود 0."""
    if not info or paid <= 0:
        return 0
    if await s.scalar(select(PlatformPromoUse.id).where(PlatformPromoUse.user_telegram_id == uid)):
        return 0
    pct = max(1, min(info.percent, 99))
    disc = int(round(paid * pct / (100 - pct)))
    s.add(PlatformPromoUse(user_telegram_id=uid, promoter_id=info.promoter_id,
                           percent=pct, paid_toman=int(paid), discount_toman=disc))
    return disc


async def notify_admin(bot_or_none, uid: int, info: PromoInfo, paid: int, disc: int, what: str = "") -> None:
    try:
        from main_platform import platform_bot as bot
    except Exception:
        bot = bot_or_none
    if not bot or not PLATFORM_ADMIN_ID:
        return
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            "🤝 <b>خریدِ اول با کدِ معرف</b>\n\n"
            f"👤 معرف: <b>{info.name}</b> ({info.percent}٪)\n"
            f"🧑 خریدار: <code>{uid}</code>\n"
            f"💰 پرداختی: <b>{int(paid):,} تومان</b> | 🎁 تخفیف: <b>{int(disc):,} تومان</b>"
            + (f"\n🛒 {what}" if what else ""))
    except Exception:
        logger.exception("promoters: اطلاع به مدیر ناموفق بود")


def pct_with_promo(base_disc: int, info: PromoInfo | None) -> tuple[int, bool]:
    """بزرگ‌ترِ تخفیفِ رفرال و معرف. خروجی: (درصد، آیا معرف اعمال شد؟)."""
    if info and info.percent > base_disc:
        return info.percent, True
    return base_disc, False


# ═══════════════════ دستور کاربر ═══════════════════
@promo_router.message(Command("code"))
async def cmd_code(msg: Message):
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("🎟 کدِ معرف را بعد از دستور بنویس. مثال:\n<code>/code ali</code>")
    code = parts[1].strip()
    if code.lower().startswith(PREFIX):
        code = code[len(PREFIX):]
    async with Session() as s:
        from platform_bot import get_user
        await get_user(s, msg.from_user.id, msg.from_user.full_name, username=msg.from_user.username)
        await s.commit()
    _ok, text = await attach(msg.from_user.id, code)
    await msg.answer(text)


# ═══════════════════ پنلِ مدیر ═══════════════════
async def _is_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and uid == PLATFORM_ADMIN_ID


async def _stats(s, pid: int) -> tuple[int, int, int]:
    joined = await s.scalar(select(func.count()).select_from(PlatformUser).where(
        PlatformUser.promoter_id == pid)) or 0
    used = await s.scalar(select(func.count()).select_from(PlatformPromoUse).where(
        PlatformPromoUse.promoter_id == pid)) or 0
    paid = await s.scalar(select(func.coalesce(func.sum(PlatformPromoUse.paid_toman), 0)).where(
        PlatformPromoUse.promoter_id == pid)) or 0
    return int(joined), int(used), int(paid)


async def _list_view() -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        rows = (await s.scalars(select(PlatformPromoter).order_by(PlatformPromoter.id.desc()))).all()
        data = [(r, await _stats(s, r.id)) for r in rows]
    txt = ("🤝 <b>معرفِ فروش (تخفیفِ خریدِ اول)</b>\n\n"
           "برایِ هر نفر یک لینک/کد بساز؛ هر کس با آن بیاید، فقط روی <b>خریدِ اولش</b> درصدِ تعیین‌شده تخفیف می‌گیرد.\n"
           f"تعدادِ معرف‌ها: <b>{len(rows)}</b>")
    kb = [[_hx("➕ افزودنِ معرفِ جدید", callback_data="prm:add", style=ButtonStyle.SUCCESS)]]
    for r, (joined, used, _paid) in data[:40]:
        kb.append([_hx(f"{'🟢' if r.active else '🔴'} {r.name} — {r.percent}٪ | 👥{joined} 🛒{used}",
                       callback_data=f"prm:v:{r.id}")])
    kb.append([_hx("⬅️ بازگشت", callback_data="adm:settings", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=kb)


async def _detail_view(bot, pid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with Session() as s:
        pr = await s.get(PlatformPromoter, pid)
        if not pr:
            return None
        joined, used, paid = await _stats(s, pid)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={PREFIX}{pr.code}"
    txt = (f"🤝 <b>{pr.name}</b>\n\n"
           f"وضعیت: {'🟢 فعال' if pr.active else '🔴 غیرفعال'}\n"
           f"🎁 تخفیفِ خریدِ اول: <b>{pr.percent}٪</b>\n"
           f"🔑 کد: <code>{pr.code}</code>\n\n"
           f"🔗 لینکِ اختصاصی (برایِ همین نفر):\n<code>{link}</code>\n\n"
           f"🎟 یا کاربر در ربات بنویسد:\n<code>/code {pr.code}</code>\n\n"
           f"📊 آمار:\n• 👥 واردشده با لینک: <b>{joined}</b>\n"
           f"• 🛒 خریدِ اولِ انجام‌شده: <b>{used}</b>\n"
           f"• 💰 جمعِ پرداختیِ آن‌ها: <b>{paid:,}</b> تومان")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_hx("⛔️ غیرفعال‌کردن" if pr.active else "✅ فعال‌کردن", callback_data=f"prm:t:{pid}",
             style=(ButtonStyle.DANGER if pr.active else ButtonStyle.SUCCESS))],
        [_hx("🎁 تغییرِ درصد", callback_data=f"prm:p:{pid}")],
        [_hx("🗑 حذف", callback_data=f"prm:d:{pid}", style=ButtonStyle.DANGER)],
        [_hx("⬅️ بازگشت", callback_data="prm:menu", style=ButtonStyle.DANGER)],
    ])
    return txt, kb


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_hx("❌ لغو", callback_data="prm:menu")]])


async def _show(target, txt, kb) -> None:
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await m.answer(txt, reply_markup=kb, disable_web_page_preview=True)


@promo_router.callback_query(F.data.in_({"set:prm", "prm:menu"}))
async def prm_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.clear()
    txt, kb = await _list_view()
    await _show(cb, txt, kb)
    await cb.answer()


@promo_router.callback_query(F.data.startswith("prm:v:"))
async def prm_view(cb: CallbackQuery, bot):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    res = await _detail_view(bot, int(cb.data.split(":")[2]))
    if not res:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await _show(cb, *res)
    await cb.answer()


@promo_router.callback_query(F.data == "prm:add")
async def prm_add(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.set_state(PromoFSM.name)
    await _show(cb, "✏️ <b>نام یا یوزرنیمِ این معرف</b> را بفرست (فقط برایِ شناساییِ خودت):", _cancel_kb())
    await cb.answer()


@promo_router.message(PromoFSM.name, F.text)
async def prm_name(msg: Message, state: FSMContext):
    if not await _is_admin(msg.from_user.id):
        return
    name = (msg.text or "").strip()[:80]
    if not name:
        return await msg.answer("نام خالی است.", reply_markup=_cancel_kb())
    await state.update_data(pname=name)
    await state.set_state(PromoFSM.code)
    await msg.answer("🔑 <b>کدِ لینک</b> را بفرست (انگلیسی/عدد/_ ، ۳ تا ۲۴ حرف؛ مثلاً <code>ali</code>).\n"
                     "برایِ ساختِ خودکار فقط «-» بفرست.", reply_markup=_cancel_kb())


@promo_router.message(PromoFSM.code, F.text)
async def prm_code(msg: Message, state: FSMContext):
    if not await _is_admin(msg.from_user.id):
        return
    code = (msg.text or "").strip()
    if code == "-":
        code = "p" + secrets.token_hex(3)
    if not CODE_RE.match(code):
        return await msg.answer("❌ کد باید انگلیسی/عدد/_ و ۳ تا ۲۴ حرف باشد.", reply_markup=_cancel_kb())
    async with Session() as s:
        if await s.scalar(select(PlatformPromoter.id).where(
                func.lower(PlatformPromoter.code) == code.lower())):
            return await msg.answer("❌ این کد قبلاً ثبت شده؛ یکی دیگر بفرست.", reply_markup=_cancel_kb())
    await state.update_data(pcode=code)
    await state.set_state(PromoFSM.percent)
    await msg.answer(f"🎁 <b>درصدِ تخفیفِ خریدِ اول</b> را بفرست (۱ تا {MAX_PERCENT}):", reply_markup=_cancel_kb())


def _parse_percent(txt: str) -> int | None:
    t = _fa(txt).replace("%", "").replace("٪", "").strip()
    if not t.isdigit():
        return None
    v = int(t)
    return v if 1 <= v <= MAX_PERCENT else None


@promo_router.message(PromoFSM.percent, F.text)
async def prm_percent_new(msg: Message, state: FSMContext, bot):
    if not await _is_admin(msg.from_user.id):
        return
    pct = _parse_percent(msg.text)
    if pct is None:
        return await msg.answer(f"❌ عددی بینِ ۱ تا {MAX_PERCENT} بفرست.", reply_markup=_cancel_kb())
    d = await state.get_data()
    async with Session() as s:
        pr = PlatformPromoter(code=d["pcode"], name=d["pname"], percent=pct, active=True)
        s.add(pr)
        await s.commit()
        pid = pr.id
    await state.clear()
    await msg.answer("✅ معرف ساخته شد.")
    res = await _detail_view(bot, pid)
    if res:
        await msg.answer(res[0], reply_markup=res[1], disable_web_page_preview=True)


@promo_router.callback_query(F.data.startswith("prm:t:"))
async def prm_toggle(cb: CallbackQuery, bot):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        pr = await s.get(PlatformPromoter, pid)
        if not pr:
            return await cb.answer("پیدا نشد.", show_alert=True)
        pr.active = not pr.active
        await s.commit()
    res = await _detail_view(bot, pid)
    if res:
        await _show(cb, *res)
    await cb.answer("انجام شد")


@promo_router.callback_query(F.data.startswith("prm:p:"))
async def prm_percent_ask(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.update_data(edit_pid=int(cb.data.split(":")[2]))
    await state.set_state(PromoFSM.edit_percent)
    await _show(cb, f"🎁 درصدِ جدید را بفرست (۱ تا {MAX_PERCENT}):", _cancel_kb())
    await cb.answer()


@promo_router.message(PromoFSM.edit_percent, F.text)
async def prm_percent_edit(msg: Message, state: FSMContext, bot):
    if not await _is_admin(msg.from_user.id):
        return
    pct = _parse_percent(msg.text)
    if pct is None:
        return await msg.answer(f"❌ عددی بینِ ۱ تا {MAX_PERCENT} بفرست.", reply_markup=_cancel_kb())
    pid = int((await state.get_data()).get("edit_pid") or 0)
    async with Session() as s:
        pr = await s.get(PlatformPromoter, pid)
        if not pr:
            await state.clear()
            return await msg.answer("پیدا نشد.")
        pr.percent = pct
        await s.commit()
    await state.clear()
    res = await _detail_view(bot, pid)
    if res:
        await msg.answer("✅ ذخیره شد.")
        await msg.answer(res[0], reply_markup=res[1], disable_web_page_preview=True)


@promo_router.callback_query(F.data.startswith("prm:d:"))
async def prm_delete_ask(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    pid = int(cb.data.split(":")[2])
    await _show(cb, "⚠️ این معرف حذف شود؟ لینکش دیگر کار نمی‌کند (آمارِ خریدهای قبلی می‌ماند).",
                InlineKeyboardMarkup(inline_keyboard=[
                    [_hx("🗑 بله، حذف", callback_data=f"prm:dok:{pid}", style=ButtonStyle.DANGER)],
                    [_hx("⬅️ انصراف", callback_data=f"prm:v:{pid}")]]))
    await cb.answer()


@promo_router.callback_query(F.data.startswith("prm:dok:"))
async def prm_delete(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        pr = await s.get(PlatformPromoter, pid)
        if pr:
            await s.delete(pr)
            await s.commit()
    txt, kb = await _list_view()
    await _show(cb, txt, kb)
    await cb.answer("حذف شد")
