"""
platform_partner_manage.py — بخشِ «مدیریت همکاران» جدا از platform_partner_admin.py.

=== PATCH_PARTNER_MANAGE_V1 ===
یک صفحهٔ واحد برایِ پیدا کردنِ سریعِ یک همکار، با سه راهِ جستجو:

    1) 🔎 با یوزرنیمِ ربات همکار   (pm:s:bot)
    2) 🔎 با آیدیِ عددیِ صاحبِ ربات (pm:s:uid)
    3) 🔎 با نام / یوزرنیمِ صاحب    (pm:s:name)

نتیجهٔ هر جستجو فهرستی از دکمه‌هایِ اینلاینِ شیشه‌ای است؛ با زدنِ هرکدام
«کارتِ کاملِ همکار» باز می‌شود: مشخصاتِ خودِ ربات، صاحبش، انبار، فروش،
تسویه و همهٔ دکمه‌هایِ مدیریتیِ موجود.

جزئیات و اکشن‌ها عمداً از platform_partner_admin.py دوباره استفاده می‌شوند
(همان _partner_detail/_detail_text/_detail_kb) تا دو نسخهٔ واگرا از یک
کارت نداشته باشیم.

دسترسی: فقط PLATFORM_ADMIN_ID.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select, func, or_

from config import Session, PLATFORM_ADMIN_ID
from models import (
    Bot as BotRow, PlatformUser,
    PartnerBot, PartnerSale,
)
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_manage_router = Router()

PAGE_SIZE = 8

# برچسبِ فارسیِ هر نوع جستجو + راهنمایِ ورودی
SEARCH_MODES = {
    "bot":  ("🤖 با یوزرنیمِ ربات همکار",
             "یوزرنیمِ ربات همکار را بفرست (با یا بدونِ @).\n"
             "بخشی از یوزرنیم هم کافی است."),
    "uid":  ("🆔 با آیدیِ عددیِ صاحبِ ربات",
             "آیدیِ عددیِ تلگرامِ صاحبِ ربات همکار را بفرست."),
    "name": ("👤 با نام یا یوزرنیمِ صاحب",
             "نام یا یوزرنیمِ صاحبِ ربات همکار را بفرست.\n"
             "بخشی از نام هم کافی است."),
}


class PartnerManageFSM(StatesGroup):
    search = State()


def _admin_only(uid: int) -> bool:
    return uid == PLATFORM_ADMIN_ID


# ======================= صفحهٔ «مدیریت همکاران» =======================

@partner_manage_router.callback_query(F.data == "pm:home")
async def pm_home(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        total = await s.scalar(select(func.count(PartnerBot.id))) or 0
        active = await s.scalar(select(func.count(PartnerBot.id))
                                .where(PartnerBot.status == "active")) or 0
        paused = await s.scalar(select(func.count(PartnerBot.id))
                                .where(PartnerBot.status == "paused")) or 0

    b = InlineKeyboardBuilder()
    for mode, (label, _hint) in SEARCH_MODES.items():
        b.button(text=f"🔎 جستجو {label}", callback_data=f"pm:s:{mode}",
                 style=ButtonStyle.PRIMARY)
    b.button(text="📋 نمایشِ همهٔ همکاران", callback_data="pm:all:0",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 بازگشت", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        "🗂 <b>مدیریت همکاران</b>\n\n"
        f"👥 مجموع همکاران: <b>{total}</b>\n"
        f"🟢 فعال: <b>{active}</b> · ⏸ متوقف: <b>{paused}</b>\n\n"
        "با یکی از سه راهِ زیر همکارِ موردنظرت را پیدا کن؛ بعد رویِ اسمش "
        "بزن تا کارتِ کاملش (ربات، صاحب، انبار، فروش و تسویه) باز شود.",
        reply_markup=b.as_markup())
    await cb.answer()


# ======================= جستجو =======================

@partner_manage_router.callback_query(F.data.startswith("pm:s:"))
async def pm_search_start(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    mode = cb.data.split(":")[2]
    if mode not in SEARCH_MODES:
        await cb.answer("نامعتبر", show_alert=True)
        return
    label, hint = SEARCH_MODES[mode]
    await state.set_state(PartnerManageFSM.search)
    await state.update_data(pm_mode=mode)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🔙 انصراف", callback_data="pm:home", style=ButtonStyle.DANGER)]])
    await cb.message.answer(f"🔎 <b>جستجو {label}</b>\n\n{hint}", reply_markup=kb)
    await cb.answer()


async def _search(mode: str, term: str, offset: int = 0) -> tuple[list, int]:
    """جستجویِ همکاران بر اساسِ نوعِ انتخاب‌شده.

    خروجی: (ردیف‌ها, تعدادِ کل) — هر ردیف:
    (pb_id, status, owner_id, bot_username, owner_name, low_stock)
    """
    like = f"%{term}%"
    async with Session() as s:
        q = (select(PartnerBot, BotRow, PlatformUser)
             .join(BotRow, BotRow.id == PartnerBot.bot_id)
             .outerjoin(PlatformUser,
                        PlatformUser.telegram_id == PartnerBot.owner_id))
        if mode == "bot":
            q = q.where(BotRow.username.ilike(like))
        elif mode == "uid":
            if not term.isdigit():
                return [], 0
            q = q.where(PartnerBot.owner_id == int(term))
        elif mode == "name":
            q = q.where(or_(PlatformUser.full_name.ilike(like),
                            PlatformUser.username.ilike(like)))
        # mode == "all" → بدونِ فیلتر

        total = await s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = (await s.execute(q.order_by(PartnerBot.id.desc())
                                .offset(offset).limit(PAGE_SIZE))).all()
        return ([(pb.id, pb.status, pb.owner_id,
                  br.username if br else None,
                  pu.full_name if pu else None,
                  pb.low_stock_since is not None)
                 for pb, br, pu in rows], total)


def _results_kb(rows: list, mode: str, term: str,
                offset: int, total: int) -> InlineKeyboardMarkup:
    """فهرستِ نتایج به‌صورتِ دکمه‌هایِ اینلاینِ شیشه‌ای."""
    b = InlineKeyboardBuilder()
    for pb_id, status, owner, uname, oname, low in rows:
        mark = "🟢" if status == "active" else ("⏸" if status == "paused" else "🔴")
        warn = "⚠️" if low else ""
        who = oname or str(owner)
        b.button(text=f"{mark}{warn} @{uname or '—'} · {who}",
                 callback_data=f"pm:view:{pb_id}",
                 style=(ButtonStyle.SUCCESS if status == "active"
                        else ButtonStyle.DANGER))
    b.adjust(1)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"pm:page:{mode}:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(("بعدی ➡️", f"pm:page:{mode}:{offset + PAGE_SIZE}"))
    for text, cbd in nav:
        b.button(text=text, callback_data=cbd, style=ButtonStyle.PRIMARY)
    if nav:
        b.adjust(*([1] * len(rows)), len(nav))

    b.button(text="🔙 مدیریت همکاران", callback_data="pm:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    return b.as_markup()


def _results_text(mode: str, term: str, total: int, shown: int, offset: int) -> str:
    label = SEARCH_MODES.get(mode, ("همهٔ همکاران", ""))[0]
    head = (f"🔎 <b>نتایجِ جستجو {label}</b>\n«{term}»"
            if term else "📋 <b>همهٔ همکاران</b>")
    return (f"{head}\n\n"
            f"{total} مورد پیدا شد — نمایشِ {offset + 1} تا {offset + shown}\n"
            "🟢 فعال · ⏸ متوقف · ⚠️ زیر حد نصاب\n\n"
            "رویِ هرکدام بزن تا اطلاعاتِ کاملش بیاید.")


@partner_manage_router.message(PartnerManageFSM.search)
async def pm_search_do(msg: Message, state: FSMContext):
    if not _admin_only(msg.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    mode = data.get("pm_mode") or "bot"
    term = (msg.text or "").strip().lstrip("@")
    await state.clear()
    # === PATCH_PARTNER_BACKNAV_V1 === بن‌بستِ بدونِ دکمه نداشته باشیم
    back = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🔙 مدیریت همکاران", callback_data="pm:home",
                   style=ButtonStyle.DANGER)]])
    if not term:
        await msg.answer("عبارتِ جستجو خالی بود.", reply_markup=back)
        return
    if mode == "uid" and not term.isdigit():
        await msg.answer("❌ برایِ این حالت فقط آیدیِ عددی قابل‌قبول است.",
                         reply_markup=back)
        return

    rows, total = await _search(mode, term)
    if not rows:
        await msg.answer("😕 هیچ همکاری با این مشخصات پیدا نشد.", reply_markup=back)
        return
    # صفحه‌بندیِ نتایج به عبارتِ جستجو نیاز دارد؛ در state نگه می‌داریم.
    await state.update_data(pm_mode=mode, pm_term=term)
    await msg.answer(_results_text(mode, term, total, len(rows), 0),
                     reply_markup=_results_kb(rows, mode, term, 0, total))


@partner_manage_router.callback_query(F.data.startswith("pm:page:"))
async def pm_page(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _, _, mode, raw_off = cb.data.split(":", 3)
    offset = int(raw_off)
    term = (await state.get_data()).get("pm_term", "")
    rows, total = await _search(mode, term, offset)
    if not rows:
        await cb.answer("چیزی نمانده.", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            _results_text(mode, term, total, len(rows), offset),
            reply_markup=_results_kb(rows, mode, term, offset, total))
    except Exception:
        logger.exception("ویرایشِ صفحهٔ نتایجِ همکاران ناموفق بود")
    await cb.answer()


@partner_manage_router.callback_query(F.data.startswith("pm:all:"))
async def pm_all(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    offset = int(cb.data.split(":")[2])
    await state.update_data(pm_mode="all", pm_term="")
    rows, total = await _search("all", "", offset)
    if not rows:
        await cb.answer("هیچ ربات همکاری ثبت نشده است.", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            _results_text("all", "", total, len(rows), offset),
            reply_markup=_results_kb(rows, "all", "", offset, total))
    except Exception:
        logger.exception("نمایشِ فهرستِ کاملِ همکاران ناموفق بود")
    await cb.answer()


# ======================= کارتِ کاملِ همکار =======================

async def _extra_info(pb_id: int, bot_id: int, owner_id: int) -> str:
    """چند خطِ تکمیلی که در کارتِ استانداردِ پنل نیست."""
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        created = pb.created_at if pb else None
        trial_on = bool(pb.trial_enabled) if pb else False
        last_sale = await s.scalar(
            select(func.max(PartnerSale.created_at))
            .where(PartnerSale.partner_bot_id == pb_id))
        pu = await s.scalar(select(PlatformUser)
                            .where(PlatformUser.telegram_id == owner_id))
        uname = pu.username if pu else None
        banned = bool(pu.is_banned) if pu else False

    from partner_rules import jalali
    lines = [
        f"\n🆔 شناسهٔ همکار: <code>{pb_id}</code> · شناسهٔ ربات: <code>{bot_id}</code>",
        f"📅 تاریخِ همکاری: {jalali(created)}",
        f"🧪 دکمهٔ تست در رباتش: {'🟢 فعال' if trial_on else '🔴 خاموش'}",
        f"🕒 آخرین فروش/تست: {jalali(last_sale) if last_sale else '—'}",
    ]
    if uname:
        lines.append(f"📇 یوزرنیمِ صاحب: @{uname}")
    if banned:
        lines.append("⛔️ صاحبِ این ربات در بات‌ساز <b>بن</b> است.")
    return "\n".join(lines)


@partner_manage_router.callback_query(F.data.startswith("pm:view:"))
async def pm_view(cb: CallbackQuery):
    """کارتِ کاملِ همکار — همان کارتِ پنل + چند خطِ تکمیلی."""
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    # lazy — تا دو ماژول به هم گره نخورند
    from platform_partner_admin import _partner_detail, _detail_text, _detail_kb

    pb_id = int(cb.data.split(":")[2])
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return

    text = _detail_text(d) + "\n" + await _extra_info(pb_id, d["bot_id"], d["owner"])
    kb = _detail_kb(d)
    # یک ردیفِ بازگشت به همین بخش، بدونِ دست‌زدن به کیبوردِ اصلیِ پنل.
    rows = list(kb.inline_keyboard) + [
        [hx_button("🔙 مدیریت همکاران", callback_data="pm:home",
                   style=ButtonStyle.DANGER)]]
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ======================= کارتِ کاملِ یک شخصِ همکار =======================
# === PATCH_PARTNER_USERS_INLINE_V1 ===
# فهرستِ «مدیریت کاربری همکاران» (pa:users در platform_partner_admin.py)
# حالا دکمه‌ای است؛ این‌جا کارتِ خودِ شخص باز می‌شود — یک نفر ممکن است چند
# ربات همکار داشته باشد، پس ربات‌هایش هم به‌صورتِ دکمه فهرست می‌شوند.

async def _owner_card(owner_id: int) -> tuple[str, list[tuple[int, str, str | None]]] | None:
    async with Session() as s:
        pu = await s.scalar(select(PlatformUser)
                            .where(PlatformUser.telegram_id == owner_id))
        rows = (await s.execute(
            select(PartnerBot, BotRow)
            .join(BotRow, BotRow.id == PartnerBot.bot_id)
            .where(PartnerBot.owner_id == owner_id)
            .order_by(PartnerBot.id.desc()))).all()
        if not rows and not pu:
            return None
        pb_ids = [pb.id for pb, _ in rows]

        bought = sold = trials = revenue = paid = 0
        if pb_ids:
            from models import PartnerStockOrder
            bought = await s.scalar(
                select(func.coalesce(
                    func.sum(PartnerStockOrder.qty_growth + PartnerStockOrder.qty_pro), 0))
                .where(PartnerStockOrder.partner_bot_id.in_(pb_ids),
                       PartnerStockOrder.status == "approved")) or 0
            paid = await s.scalar(
                select(func.coalesce(func.sum(PartnerStockOrder.amount), 0))
                .where(PartnerStockOrder.partner_bot_id.in_(pb_ids),
                       PartnerStockOrder.status == "approved")) or 0
            sold = await s.scalar(
                select(func.count(PartnerSale.id))
                .where(PartnerSale.partner_bot_id.in_(pb_ids),
                       PartnerSale.kind == "sold")) or 0
            trials = await s.scalar(
                select(func.count(PartnerSale.id))
                .where(PartnerSale.partner_bot_id.in_(pb_ids),
                       PartnerSale.kind == "trial")) or 0
            revenue = await s.scalar(
                select(func.coalesce(func.sum(PartnerSale.price), 0))
                .where(PartnerSale.partner_bot_id.in_(pb_ids))) or 0

        bots = [(pb.id, pb.status, br.username if br else None) for pb, br in rows]
        name = pu.full_name if pu else None
        uname = pu.username if pu else None
        balance = int(pu.balance) if pu else 0
        banned = bool(pu.is_banned) if pu else False
        joined = pu.created_at if pu else None

    from partner_rules import jalali
    text = (
        f"{'🚫 ' if banned else '👤 '}<b>{name or '—'}</b>\n"
        f"🆔 <code>{owner_id}</code>"
        + (f" · @{uname}" if uname else "") + "\n"
        f"📅 عضویت در بات‌ساز: {jalali(joined)}\n"
        f"👛 کیف پول: {balance:,} تومان\n"
        + ("⛔️ این کاربر در بات‌ساز <b>بن</b> است.\n" if banned else "")
        + f"\n🤝 ربات همکار: <b>{len(bots)}</b>\n"
        f"🛒 ربات خریده: <b>{int(bought)}</b>\n"
        f"✅ فروخته: <b>{int(sold)}</b> · 🎁 تست داده: <b>{int(trials)}</b>\n\n"
        f"💵 پرداختی به ما: {int(paid):,} تومان\n"
        f"💰 فروشِ او: {int(revenue):,} تومان\n"
        f"⚖️ اختلاف: {int(revenue) - int(paid):,} تومان\n\n"
        "برایِ دیدنِ جزئیات و مدیریتِ هر ربات، رویِ آن بزن:"
    )
    return text, bots


@partner_manage_router.callback_query(F.data.startswith("pmu:view:"))
async def pmu_view(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    owner_id = int(cb.data.split(":")[2])
    card = await _owner_card(owner_id)
    if not card:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    text, bots = card

    b = InlineKeyboardBuilder()
    for pb_id, status, uname in bots:
        mark = "🟢" if status == "active" else ("⏸" if status == "paused" else "🔴")
        b.button(text=f"{mark} @{uname or '—'}", callback_data=f"pm:view:{pb_id}",
                 style=(ButtonStyle.SUCCESS if status == "active" else ButtonStyle.DANGER))
    if bots:
        # پیام مستقیم به این شخص، از مسیرِ همان ربات همکارِ اولش
        b.button(text="✉️ پیام به این شخص", callback_data=f"pa:msg:{bots[0][0]}",
                 style=ButtonStyle.PRIMARY)
    b.button(text="🔙 فهرست همکاران", callback_data="pa:users:0",
             style=ButtonStyle.PRIMARY)
    b.button(text="🏠 ربات‌های همکار", callback_data="pa:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    try:
        await cb.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        await cb.message.answer(text, reply_markup=b.as_markup())
    await cb.answer()
