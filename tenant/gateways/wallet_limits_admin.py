"""
tenant/gateways/wallet_limits_admin.py — صفحهٔ «📏 حداقل و حداکثر واریز» در بخشِ مالی.

=== PATCH_WALLET_LIMITS_V1 ===
مسیر: پنل مدیریت ← مالی و کیف پول ← 📏 حداقل و حداکثر واریز هر درگاه.
همهٔ درگاه‌هایِ *فعالِ* ربات فهرست می‌شوند؛ رویِ هر کدام بزنی حداقل/حداکثرش را جدا تنظیم می‌کنی.
منطق در wallet_limits.py است؛ این‌جا فقط صفحه‌ها.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.enums import ButtonStyle

from logging_setup import get_logger as _get_logger
from tenant.gateways import wallet_limits as WL

logger = _get_logger(__name__)
wallet_limits_admin_router = Router(name="wallet_limits_admin")


class WlimFSM(StatesGroup):
    min_amount = State()
    max_amount = State()


def _fmt_range(mn: int, mx: int) -> str:
    return f"{mn:,} تا " + (f"{mx:,}" if mx else "بدونِ سقف")


async def _menu_content(bot_id: int):
    gws = await WL.active_gateways(bot_id)
    lines = [
        "📏 <b>حداقل و حداکثر واریز</b>\n",
        "برایِ هر درگاهِ فعال جدا تعیین کن کاربر حداقل و حداکثر چقدر می‌تواند شارژ کند "
        "(به تومان). دکمه‌هایِ مبلغِ ثابتی که بیرونِ بازه بمانند نشان داده نمی‌شوند.\n",
    ]
    rows = []
    if not gws:
        lines.append("هنوز هیچ درگاهِ شارژی برایِ این ربات فعال نیست.")
    for g in gws:
        mn, mx = await WL.get_limits(bot_id, g.key)
        lines.append(f"{g.label}: <b>{_fmt_range(mn, mx)}</b>")
        rows.append([InlineKeyboardButton(text=f"{g.label} — {_fmt_range(mn, mx)}",
                                          callback_data=f"wlim:g:{g.key}", style=ButtonStyle.PRIMARY)])
    lines.append("\nدرگاه‌هایِ غیرفعال این‌جا نمی‌آیند.")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _gw_content(bot_id: int, key: str):
    g = WL.GATEWAYS[key]
    mn, mx = await WL.get_limits(bot_id, key)
    text = (f"📏 <b>{g.label}</b>\n\n"
            f"🔽 حداقلِ واریز: <b>{mn:,}</b> تومان"
            + ("" if mn != g.default_min else " (پیش‌فرض)") + "\n"
            f"🔼 حداکثرِ واریز: <b>{(f'{mx:,} تومان' if mx else 'بدونِ سقف')}</b>\n")
    if g.note:
        text += f"\nℹ️ {g.note}\n"
    text += f"\nکمترین حداقلِ مجاز برایِ این درگاه: {g.floor_min:,} تومان."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ حداقل", callback_data=f"wlim:mn:{key}", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="✏️ حداکثر", callback_data=f"wlim:mx:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="♻️ برگشت به پیش‌فرض", callback_data=f"wlim:rs:{key}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="wlim:menu", style=ButtonStyle.DANGER)],
    ])
    return text, kb


async def _show(target, text: str, kb) -> None:
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


def _key_from(data: str) -> str | None:
    key = data.split(":")[2] if data.count(":") >= 2 else ""
    return key if key in WL.GATEWAYS else None


@wallet_limits_admin_router.callback_query(F.data == "wlim:menu")
async def wlim_menu(cb: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if state is not None:
        await state.clear()
    text, kb = await _menu_content(row.id)
    await cb.answer()
    await _show(cb, text, kb)


@wallet_limits_admin_router.callback_query(F.data.startswith("wlim:g:"))
async def wlim_gateway(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = _key_from(cb.data)
    if not key:
        await cb.answer()
        return
    await state.clear()
    text, kb = await _gw_content(row.id, key)
    await cb.answer()
    await _show(cb, text, kb)


@wallet_limits_admin_router.callback_query(F.data.startswith("wlim:mn:") | F.data.startswith("wlim:mx:"))
async def wlim_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = _key_from(cb.data)
    if not key:
        await cb.answer()
        return
    is_min = cb.data.startswith("wlim:mn:")
    g = WL.GATEWAYS[key]
    mn, mx = await WL.get_limits(row.id, key)
    await state.set_state(WlimFSM.min_amount if is_min else WlimFSM.max_amount)
    await state.update_data(wlim_key=key)
    await cb.answer()
    if is_min:
        prompt = (f"🔽 <b>حداقلِ واریز {g.label}</b> را به تومان بفرست.\n"
                  f"الان: <b>{mn:,}</b> — کمترینِ مجاز: {g.floor_min:,}\n"
                  f"برایِ برگشت به پیش‌فرض ({g.default_min:,}) عدد <b>0</b> بفرست.")
    else:
        prompt = (f"🔼 <b>حداکثرِ واریز {g.label}</b> را به تومان بفرست.\n"
                  f"الان: <b>{(f'{mx:,}' if mx else 'بدونِ سقف')}</b>\n"
                  "برایِ برداشتنِ سقف عدد <b>0</b> بفرست.")
    await cb.message.answer(prompt, parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


async def _save(msg: Message, bot: Bot, state: FSMContext, is_min: bool) -> None:
    from tenant import _owner_ok_msg, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    data = await state.get_data()
    key = data.get("wlim_key")
    if not row or key not in WL.GATEWAYS:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    n = fa_to_int(raw) if raw not in ("0", "۰", "٠") else 0
    if n is None:
        await msg.answer("❌ عددِ نامعتبر. فقط عدد به تومان بفرست (یا 0).")
        return                                   # در همان مرحله می‌ماند
    try:
        if is_min:
            await WL.set_min(row.id, key, n)
        else:
            await WL.set_max(row.id, key, n)
    except WL.LimitError as e:
        await msg.answer(f"❌ {e}\nدوباره یک عددِ دیگر بفرست، یا «انصراف».")
        return                                   # در همان مرحله می‌ماند
    await state.clear()
    mn, mx = await WL.get_limits(row.id, key)
    await msg.answer(f"✅ ذخیره شد. بازهٔ مجاز اکنون: <b>{_fmt_range(mn, mx)}</b> تومان.", parse_mode="HTML")
    text, kb = await _gw_content(row.id, key)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@wallet_limits_admin_router.message(StateFilter(WlimFSM.min_amount), F.text)
async def wlim_save_min(msg: Message, bot: Bot, state: FSMContext):
    await _save(msg, bot, state, True)


@wallet_limits_admin_router.message(StateFilter(WlimFSM.max_amount), F.text)
async def wlim_save_max(msg: Message, bot: Bot, state: FSMContext):
    await _save(msg, bot, state, False)


@wallet_limits_admin_router.callback_query(F.data.startswith("wlim:rs:"))
async def wlim_reset(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = _key_from(cb.data)
    if not key:
        await cb.answer()
        return
    await WL.reset(row.id, key)
    await state.clear()
    await cb.answer("♻️ به پیش‌فرض برگشت.", show_alert=False)
    text, kb = await _gw_content(row.id, key)
    await _show(cb, text, kb)
