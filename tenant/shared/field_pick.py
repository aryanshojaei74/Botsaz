"""
tenant/field_pick.py — تعیینِ «نوع ربات» برایِ ربات‌هایی که زمینه ندارند.

=== PATCH_EMPTY_FIELD_PICKER_V1 ===
ربات‌هایی که از داخلِ ربات‌هایِ همکار فروخته می‌شوند با
field=(data.get("ps_field") or "") ساخته می‌شوند. در مسیرِ «ادامهٔ ساخت»
(ps:mk — سفارشِ پرداخت‌شده، یا وقتی بارِ اول توکن تکراری بوده) اصلاً
ps_field در FSM نیست، پس ربات با زمینهٔ خالی ساخته می‌شود. اگر مشتری
ویزاردِ چیدمان (partner_build) را کامل نکند، زمینه برای همیشه خالی
می‌ماند و بخشِ «📦 سرویس‌ها و محصولات» — حتی رویِ پلنِ رشد/حرفه‌ای —
فقط «🔒 این بخش هنوز چیزی نداره (نیازِ ارتقای پلن)» نشان می‌داد.

این ماژول فقط وقتی زمینه خالی/نامعتبر است یک انتخاب‌گرِ یک‌باره نشان
می‌دهد. برایِ ربات‌هایی که زمینه دارند هیچ اثری ندارد (تغییرِ نوعِ رباتِ
دارایِ زمینه همچنان فقط از مسیرهایِ پولیِ/ادمینیِ موجود است).
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Session
from models import Bot as BotRow

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

field_pick_router = Router()

BTN_PICK_FIELD = "🧭 تعیین نوع ربات"

# همهٔ زمینه‌هایی که پنلِ مدیریت برایشان محتوا دارد (معتبر = دست نزن).
KNOWN_FIELDS = {"vpn_sales", "digital_shop", "booking", "virtual_service",
                "ad_bot", "community_mgmt", "funstat", "escrow_bot"}   # PATCH_ESCROW_BOT_KIND_V1


def needs_field(field: str | None) -> bool:
    return (field or "") not in KNOWN_FIELDS


def _choices() -> list[tuple[str, str]]:
    # همان قفلِ سراسریِ سه‌زمینه‌ای که ساختِ رباتِ جدید دارد.
    from tenant.partner_bot.partner_build import FIELDS, ACTIVE_FIELDS
    return [(k, lbl) for k, lbl in FIELDS if k in ACTIVE_FIELDS]


def _picker_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=lbl, callback_data=f"tfld:set:{k}",
                                  style=ButtonStyle.SUCCESS)]
            for k, lbl in _choices()]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:menu",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


PICKER_TEXT = (
    "🧭 <b>نوع ربات هنوز تعیین نشده</b>\n\n"
    "برای اینکه بخشِ «سرویس‌ها و محصولات» امکاناتِ درست را نشان دهد، "
    "مشخص کن ربات تو چه کاری انجام می‌دهد.\n\n"
    "⚠️ این انتخاب فقط یک‌بار انجام می‌شود."
)


async def show_field_picker(msg: Message) -> None:
    """نمایشِ انتخاب‌گر (برایِ حالتِ کیبوردی، از دیسپچرِ متنی صدا زده می‌شود)."""
    await msg.answer(PICKER_TEXT, reply_markup=_picker_kb())


@field_pick_router.callback_query(F.data == "tfld:menu")
async def tfld_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    if not needs_field(row.field):
        await cb.answer("نوع ربات قبلاً تعیین شده است.", show_alert=True)
        return
    try:
        await cb.message.edit_text(PICKER_TEXT, reply_markup=_picker_kb())
    except Exception:
        await cb.message.answer(PICKER_TEXT, reply_markup=_picker_kb())
    await cb.answer()


@field_pick_router.callback_query(F.data.startswith("tfld:set:"))
async def tfld_set(cb: CallbackQuery, bot: Bot):
    from tenant import (get_bot_row, is_owner_or_admin, invalidate_bot_row_cache,
                        _sec_products_text, _sec_products_kb)
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    field = cb.data.split(":", 2)[2]
    if field not in dict(_choices()):
        await cb.answer("این نوع ربات قابلِ انتخاب نیست.", show_alert=True)
        return
    async with Session() as s:
        br = await s.get(BotRow, row.id)
        if not br:
            await cb.answer()
            return
        # فقط وقتی واقعاً خالی است؛ وگرنه راهِ رایگانِ دورزدنِ «تغییر نوع» می‌شد.
        if not needs_field(br.field):
            await cb.answer("نوع ربات قبلاً تعیین شده است.", show_alert=True)
            return
        br.field = field
        plan, token = br.plan, br.token
        await s.commit()
    invalidate_bot_row_cache(token)
    try:
        import tenant_status_bus
        await tenant_status_bus.publish_tenant_invalidate(token)
    except Exception:
        logger.exception("اطلاع‌رسانیِ تغییرِ زمینه به پروسه‌هایِ دیگر ناموفق بود")
    kb = await _sec_products_kb(row.id, field, plan)
    await cb.answer("✅ نوع ربات ثبت شد")
    if isinstance(kb, InlineKeyboardMarkup):
        try:
            await cb.message.edit_text(_sec_products_text(field), reply_markup=kb)
            return
        except Exception:
            logger.debug("ویرایشِ پیام ممکن نشد؛ پیامِ تازه فرستاده می‌شود")
    await cb.message.answer(_sec_products_text(field), reply_markup=kb)
