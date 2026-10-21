"""stock_give.py — «افزودن دستیِ کانفیگ انبار به کاربر» (=== PATCH_STOCK_GIVE_TO_USER_V1 ===).

درخواستِ MidnightSoul: همان‌طور که ادمین می‌تواند سرویسِ پنلِ خودکار را دستی به یک کاربر وصل کند، بتواند کانفیگِ دستیِ انبار
(آماده یا نامحدود) را هم دستی به یک کاربر بدهد. مسیر: انبار ← «🎁 افزودن کانفیگ انبار به کاربر» ← آیدی عددیِ کاربر ←
آماده/نامحدود ← انتخابِ کانفیگ ← تأیید. کانفیگ فروخته‌شده می‌شود (owner_user_id = کاربر)، برایش ارسال می‌شود و در
«سرویس‌هایِ من» می‌ماند. هیچ پولی کم/اضافه نمی‌شود (هدیه/تحویلِ دستی)؛ قفلِ ردیف جلوی دوبار دادنِ یک کانفیگ را می‌گیرد.
"""
from __future__ import annotations

import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, Config

logger = _get_logger(__name__)
sg_router = Router(name="stock_give")
PAGE = 8
_KIND_ICON = {"wireguard": "🛡️", "panel": "🧰"}


class SgFSM(StatesGroup):
    uid = State()


def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


async def _admin_row(cb_or_msg, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    uid = cb_or_msg.from_user.id
    if not row or not await is_owner_or_admin(row.id, uid, row.owner_id):
        if not isinstance(cb_or_msg, Message):
            await cb_or_msg.answer("دسترسی ندارید", show_alert=True)
        return None
    from plans import Feature, has_feature
    if not has_feature(row.plan, Feature.MANUAL_INVENTORY):
        if not isinstance(cb_or_msg, Message):
            await cb_or_msg.answer("🔒 این بخش در پلنِ فعلی در دسترس نیست.", show_alert=True)
        return None
    return row


def label(c: Config) -> str:
    vol = "♾️ نامحدود" if not c.volume_gb else f"{c.volume_gb} گیگ"
    cat = f" | {c.category}" if c.category else ""
    return f"{_KIND_ICON.get(c.kind or '', '')}#{c.code} | {vol} | {c.days} روز{cat}".strip()


async def counts(bot_id: int) -> tuple[int, int]:
    """(آماده، نامحدود) — فقط موجودِ انبار."""
    async with Session() as s:
        ready = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == bot_id, Config.status == "in_stock", Config.volume_gb != 0))
        unl = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == bot_id, Config.status == "in_stock", Config.volume_gb == 0))
    return int(ready or 0), int(unl or 0)


async def list_stock(bot_id: int, unlimited: bool, page: int):
    q = select(Config).where(Config.bot_id == bot_id, Config.status == "in_stock")
    q = q.where(Config.volume_gb == 0) if unlimited else q.where(Config.volume_gb != 0)
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(q.subquery())) or 0)
        rows = (await s.scalars(q.order_by(Config.id).offset(page * PAGE).limit(PAGE))).all()
    return rows, total


async def assign(bot_id: int, user_pk: int, cfg_id: int):
    """کانفیگ را (با قفلِ ردیف) به کاربر می‌دهد. خروجی: (cfg یا None، خطا)."""
    async with Session() as s:
        u = await s.get(BotUser, user_pk)
        if not u or u.bot_id != bot_id:
            return None, "کاربر پیدا نشد."
        cfg = await s.scalar(select(Config).where(Config.id == cfg_id, Config.bot_id == bot_id).with_for_update())
        if not cfg or cfg.status != "in_stock":
            return None, "این کانفیگ دیگر در انبار نیست (فروخته یا حذف شده)."
        cfg.status = "sold"
        cfg.owner_user_id = u.id
        await s.commit()
        return cfg, ""


@sg_router.callback_query(F.data == "sg:menu")
async def sg_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    await state.clear()
    await state.set_state(SgFSM.uid)
    await cb.message.answer(
        "🎁 <b>افزودن کانفیگ انبار به کاربر</b>\n\nآیدی عددیِ تلگرامِ کاربر را بفرست "
        "(کاربر باید حداقل یک‌بار ربات را استارت کرده باشد).",
        parse_mode="HTML", reply_markup=_kb([[_btn("❌ لغو", "sg:cancel", ButtonStyle.DANGER)]]))
    await cb.answer()


@sg_router.callback_query(F.data == "sg:cancel")
async def sg_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("لغو شد.")
    await cb.answer()


@sg_router.message(StateFilter(SgFSM.uid))
async def sg_uid(msg: Message, bot: Bot, state: FSMContext):
    row = await _admin_row(msg, bot)
    if not row:
        await state.clear()
        return
    raw = _fa(msg.text or "")
    if not raw.lstrip("-").isdigit():
        await msg.answer("❌ فقط آیدی عددی بفرست (مثلاً 123456789).")
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == int(raw)))
    if not u:
        await msg.answer("❌ این کاربر در ربات شما پیدا نشد؛ باید اول ربات را استارت کند. دوباره آیدی بفرست یا لغو کن.")
        return
    await state.clear()
    ready, unl = await counts(row.id)
    name = _html.escape(u.full_name or str(u.telegram_id))
    await msg.answer(
        f"👤 کاربر: <b>{name}</b> (<code>{u.telegram_id}</code>)\n\nکدام نوع کانفیگ را بدهم؟",
        parse_mode="HTML",
        reply_markup=_kb([[_btn(f"🛒 آماده ({ready})", f"sg:t:{u.id}:r:0", ButtonStyle.PRIMARY),
                           _btn(f"♾️ نامحدود ({unl})", f"sg:t:{u.id}:u:0", ButtonStyle.PRIMARY)],
                          [_btn("❌ لغو", "sg:cancel", ButtonStyle.DANGER)]]))


@sg_router.callback_query(F.data.regexp(r"^sg:t:\d+:[ru]:\d+$"))
async def sg_list(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    _p, _t, upk, kind, page = cb.data.split(":")
    page = int(page)
    rows, total = await list_stock(row.id, kind == "u", page)
    if not rows:
        await cb.answer("موجودی این نوع در انبار صفر است.", show_alert=True)
        return
    kb = [[_btn(label(c), f"sg:p:{upk}:{c.id}")] for c in rows]
    nav = []
    if page > 0:
        nav.append(_btn("◀️ قبلی", f"sg:t:{upk}:{kind}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(_btn("بعدی ▶️", f"sg:t:{upk}:{kind}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([_btn("❌ لغو", "sg:cancel", ButtonStyle.DANGER)])
    await cb.message.answer(f"کانفیگ را انتخاب کن ({total} مورد در انبار):", reply_markup=_kb(kb))
    await cb.answer()


@sg_router.callback_query(F.data.regexp(r"^sg:p:\d+:\d+$"))
async def sg_pick(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    _p, _x, upk, cid = cb.data.split(":")
    async with Session() as s:
        c = await s.get(Config, int(cid))
        u = await s.get(BotUser, int(upk))
    if not c or c.bot_id != row.id or c.status != "in_stock" or not u or u.bot_id != row.id:
        await cb.answer("این کانفیگ یا کاربر دیگر معتبر نیست.", show_alert=True)
        return
    await cb.message.answer(
        f"تأیید می‌کنی؟\n\n📦 {_html.escape(label(c))}\n👤 {_html.escape(u.full_name or '')} (<code>{u.telegram_id}</code>)\n\n"
        "کانفیگ برای کاربر ارسال می‌شود و از انبار خارج می‌شود؛ هیچ مبلغی از او کم نمی‌شود.",
        parse_mode="HTML",
        reply_markup=_kb([[_btn("✅ تأیید و ارسال", f"sg:ok:{upk}:{cid}", ButtonStyle.SUCCESS)],
                          [_btn("❌ لغو", "sg:cancel", ButtonStyle.DANGER)]]))
    await cb.answer()


@sg_router.callback_query(F.data.regexp(r"^sg:ok:\d+:\d+$"))
async def sg_ok(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    _p, _x, upk, cid = cb.data.split(":")
    cfg, err = await assign(row.id, int(upk), int(cid))
    if not cfg:
        await cb.answer(err, show_alert=True)
        return
    async with Session() as s:
        u = await s.get(BotUser, int(upk))
    try:
        from tenant import send_config_to_user
        await send_config_to_user(bot, u.telegram_id, cfg)
        await cb.message.answer(f"✅ کانفیگ #{cfg.code} برای کاربر <code>{u.telegram_id}</code> ارسال شد و در «سرویس‌هایِ من»ش هست.",
                                parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        logger.exception("sg: ارسال کانفیگ ناموفق (cfg=%s)", cfg.id)
        await cb.message.answer(
            f"⚠️ کانفیگ #{cfg.code} به کاربر اختصاص یافت ولی ارسال پیام ناموفق بود ({_html.escape(str(e)[:120])}). "
            "کاربر آن را در «سرویس‌هایِ من» می‌بیند.")
    await cb.answer()
