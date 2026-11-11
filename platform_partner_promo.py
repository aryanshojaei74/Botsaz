# -*- coding: utf-8 -*-
"""
platform_partner_promo.py — پنلِ مدیرِ «🎁 جشنوارهٔ خرید عمده» (PATCH_PARTNER_PROMO_V1).

مسیر: پنل مدیریت ← 🤝 ربات‌هایِ همکار ← 🎁 جشنوارهٔ خرید عمده.
منطقِ هدیه در partner_promo.py است؛ این‌جا فقط تنظیمات: روشن/خاموش، شروع و پایان (شمسی، وقتِ تهران)،
قاعدهٔ «به‌ازای هر N ربات M ربات هدیه» برایِ حرفه‌ای و رشد، و متنِ آمادهٔ اطلاعیه.
"""
from __future__ import annotations

import html as _html

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import partner_promo as PP
from partner_rules import jalali

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_promo_router = Router()


class PromoFSM(StatesGroup):
    start = State()
    end = State()
    pro = State()
    growth = State()


async def _allowed(uid: int) -> bool:
    from platform_partner_admin import _can_access
    return await _can_access(uid)


def _btn(text, cb, style=None):
    return (InlineKeyboardButton(text=text, callback_data=cb, style=style) if style
            else InlineKeyboardButton(text=text, callback_data=cb))


def _back_kb(to: str = "ppr:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ بازگشت", to, ButtonStyle.DANGER)]])


def _fmt(dt) -> str:
    return jalali(dt, with_time=True) if dt else "—"


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    cfg = await PP.load()
    now = PP.now_local()
    live = PP.is_active(cfg, now)
    if not cfg["on"]:
        state = "🔴 خاموش"
    elif not cfg["start"] or not cfg["end"]:
        state = "⚠️ روشن ولی تاریخ تنظیم نشده"
    elif now < cfg["start"]:
        state = "🟡 روشن؛ هنوز شروع نشده"
    elif now >= cfg["end"]:
        state = "⏹ تمام شده"
    else:
        state = "🟢 فعال (همین الان)"
    text = ("🎁 <b>جشنوارهٔ خرید عمده</b>\n\n"
            "هر همکاری که در بازهٔ زیر خریدِ عمده کند، به‌ازای هر N ربات M ربات هدیه می‌گیرد. خریدها تجمعی حساب می‌شوند "
            "(دو خریدِ تکی هم جمع می‌شوند) و هدیه خودکار به انبارش اضافه می‌شود.\n\n"
            f"وضعیت: {state}\n"
            f"شروع: <b>{_fmt(cfg['start'])}</b>\n"
            f"پایان: <b>{_fmt(cfg['end'])}</b>\n\n"
            f"{_html.escape(PP.rule_text(cfg))}\n\n"
            "⏰ زمان‌ها به وقتِ تهران‌اند. شمارندهٔ خریدها از لحظهٔ «شروع» حساب می‌شود.")
    rows = [
        [_btn(("⛔ خاموش‌کردنِ جشنواره" if cfg["on"] else "🟢 روشن‌کردنِ جشنواره"), "ppr:toggle",
              ButtonStyle.DANGER if cfg["on"] else ButtonStyle.SUCCESS)],
        [_btn("🕒 تعیینِ شروع", "ppr:start", ButtonStyle.PRIMARY), _btn("🕓 تعیینِ پایان", "ppr:end", ButtonStyle.PRIMARY)],
        [_btn(f"💎 قاعدهٔ حرفه‌ای ({cfg['pro_buy']}→{cfg['pro_free']})", "ppr:pro", ButtonStyle.PRIMARY)],
        [_btn(f"🚀 قاعدهٔ رشد ({cfg['growth_buy']}→{cfg['growth_free']})", "ppr:growth", ButtonStyle.PRIMARY)],
        [_btn("📝 متنِ آمادهٔ اطلاعیه", "ppr:text")],
        [_btn("⬅️ بازگشت", "pa:home", ButtonStyle.DANGER)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery) -> None:
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@partner_promo_router.callback_query(F.data == "ppr:menu")
async def ppr_menu(cb: CallbackQuery, state: FSMContext):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    await state.clear()
    await _show(cb)
    await cb.answer()


@partner_promo_router.callback_query(F.data == "ppr:toggle")
async def ppr_toggle(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    cfg = await PP.load()
    if not cfg["on"] and not (cfg["start"] and cfg["end"]):
        return await cb.answer("اول شروع و پایانِ جشنواره را تعیین کن.", show_alert=True)
    if not cfg["on"] and cfg["end"] <= PP.now_local():
        return await cb.answer("زمانِ پایان گذشته؛ پایانِ جدید تعیین کن.", show_alert=True)
    await PP.set_value("ppromo_on", "0" if cfg["on"] else "1")
    await _show(cb)
    await cb.answer("جشنواره روشن شد" if not cfg["on"] else "جشنواره خاموش شد")


_DATE_HELP = ("به شکلِ <code>1405/08/25 18:30</code> بفرست (شمسی، وقتِ تهران؛ ساعت اختیاری است و پیش‌فرضش ۰۰:۰۰).")


async def _ask_date(cb: CallbackQuery, state: FSMContext, st, title: str) -> None:
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    await state.set_state(st)
    await cb.message.answer(f"{title}\n\n{_DATE_HELP}", reply_markup=_back_kb(), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.callback_query(F.data == "ppr:start")
async def ppr_start(cb: CallbackQuery, state: FSMContext):
    await _ask_date(cb, state, PromoFSM.start, "🕒 <b>شروعِ جشنواره</b>")


@partner_promo_router.callback_query(F.data == "ppr:end")
async def ppr_end(cb: CallbackQuery, state: FSMContext):
    await _ask_date(cb, state, PromoFSM.end, "🕓 <b>پایانِ جشنواره</b>")


async def _date_in(msg: Message, state: FSMContext, key: str) -> None:
    if not await _allowed(msg.from_user.id):
        return
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    d = PP.parse_jalali_datetime(val)
    if not d:
        return await msg.answer("❌ تاریخ نامعتبر است. مثل <code>1405/08/25 18:30</code> بفرست.",
                                reply_markup=_back_kb(), parse_mode="HTML")
    cfg = await PP.load()
    other = cfg["end"] if key == "ppromo_start" else cfg["start"]
    if other and ((key == "ppromo_start" and d >= other) or (key == "ppromo_end" and d <= other)):
        return await msg.answer("❌ پایان باید بعد از شروع باشد.", reply_markup=_back_kb())
    await PP.set_value(key, d.strftime("%Y-%m-%dT%H:%M"))
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@partner_promo_router.message(PromoFSM.start, F.text)
async def ppr_start_in(msg: Message, state: FSMContext):
    await _date_in(msg, state, "ppromo_start")


@partner_promo_router.message(PromoFSM.end, F.text)
async def ppr_end_in(msg: Message, state: FSMContext):
    await _date_in(msg, state, "ppromo_end")


async def _ask_rule(cb: CallbackQuery, state: FSMContext, st, title: str) -> None:
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    await state.set_state(st)
    await cb.message.answer(
        f"{title}\n\nدو عدد بفرست: «به‌ازای هر چند ربات» و «چند ربات هدیه». مثلاً <code>2 1</code> یعنی با خریدِ هر ۲ ربات، ۱ ربات هدیه.\n"
        f"برایِ خاموش‌کردنِ این قاعده <code>0 0</code> بفرست. (حداکثر: خرید {PP.MAX_BUY}، هدیه {PP.MAX_FREE})",
        reply_markup=_back_kb(), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.callback_query(F.data == "ppr:pro")
async def ppr_pro(cb: CallbackQuery, state: FSMContext):
    await _ask_rule(cb, state, PromoFSM.pro, "💎 <b>قاعدهٔ ربات حرفه‌ای</b>")


@partner_promo_router.callback_query(F.data == "ppr:growth")
async def ppr_growth(cb: CallbackQuery, state: FSMContext):
    await _ask_rule(cb, state, PromoFSM.growth, "🚀 <b>قاعدهٔ ربات رشد</b>")


def _parse_rule(raw: str) -> tuple[int, int] | None:
    t = (raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    parts = t.replace("،", " ").replace(",", " ").replace("→", " ").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        return None
    buy, free = int(parts[0]), int(parts[1])
    if (buy == 0) != (free == 0) or buy > PP.MAX_BUY or free > PP.MAX_FREE:
        return None
    return buy, free


async def _rule_in(msg: Message, state: FSMContext, plan: str) -> None:
    if not await _allowed(msg.from_user.id):
        return
    if (msg.text or "").strip().startswith("/"):
        return
    r = _parse_rule(msg.text)
    if not r:
        return await msg.answer(f"❌ دو عددِ معتبر بفرست، مثل <code>2 1</code> (یا <code>0 0</code> برایِ خاموش).",
                                reply_markup=_back_kb(), parse_mode="HTML")
    await PP.set_value(f"ppromo_{plan}_buy", str(r[0]))
    await PP.set_value(f"ppromo_{plan}_free", str(r[1]))
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@partner_promo_router.message(PromoFSM.pro, F.text)
async def ppr_pro_in(msg: Message, state: FSMContext):
    await _rule_in(msg, state, "pro")


@partner_promo_router.message(PromoFSM.growth, F.text)
async def ppr_growth_in(msg: Message, state: FSMContext):
    await _rule_in(msg, state, "growth")


def announcement_text(cfg: dict) -> str:
    lines = ["🎁 <b>جشنوارهٔ خرید عمده ربات</b>", "",
             f"از <b>{_fmt(cfg['start'])}</b> تا <b>{_fmt(cfg['end'])}</b> فرصت داری:", ""]
    rules = PP.rule_text(cfg)
    lines.append(rules if rules != "هیچ قاعده‌ای تنظیم نشده" else "—")
    lines += ["", "هدیه خودکار و بلافاصله بعد از تأییدِ خرید به انبارت اضافه می‌شود. "
                  "خریدهایِ این بازه با هم جمع می‌شوند.", "", "از پنل ربات همکارت دکمهٔ «🛒 خرید ربات جهت فروش» را بزن و همین حالا خرید کن."]
    return "\n".join(lines)


@partner_promo_router.callback_query(F.data == "ppr:text")
async def ppr_text(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    cfg = await PP.load()
    if not (cfg["start"] and cfg["end"]):
        return await cb.answer("اول شروع و پایان را تعیین کن.", show_alert=True)
    await cb.message.answer(announcement_text(cfg) + "\n\n— — —\nاین متن را با «📢 پیام همگانی به همکاران» برایِ همکاران بفرست.",
                            reply_markup=_back_kb(), parse_mode="HTML")
    await cb.answer()
