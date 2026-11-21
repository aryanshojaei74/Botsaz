# -*- coding: utf-8 -*-
"""
platform_partner_promo.py — پنلِ مدیرِ «🎁 جشنوارهٔ خرید عمده» (PATCH_PARTNER_PROMO_V1).

مسیر: پنل مدیریت ← 🤝 ربات‌هایِ همکار ← 🎁 جشنوارهٔ خرید عمده.
منطقِ هدیه در partner_promo.py است؛ این‌جا فقط تنظیمات: روشن/خاموش، شروع و پایان (شمسی، وقتِ تهران)،
قاعدهٔ «به‌ازای هر N ربات M ربات هدیه» برایِ حرفه‌ای و رشد، و متنِ آمادهٔ اطلاعیه.
"""
from __future__ import annotations

import asyncio
import html as _html

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import partner_promo as PP
from partner_rules import jalali
from config import PLATFORM_ADMIN_ID, Session

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_promo_router = Router()


class PromoFSM(StatesGroup):
    channel = State()
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
            "⏰ زمان‌ها به وقتِ تهران‌اند. شمارندهٔ خریدها از لحظهٔ «شروع» حساب می‌شود.\n\n"
            "📨 اطلاعیه‌ها خودکار می‌روند: <b>شروع</b>، <b>۲ ساعت مانده به پایان</b> و <b>پایان</b> — برایِ همهٔ همکاران (پیامِ خصوصی) "
            "و در صورتِ وصل‌بودنِ کانال، به‌صورتِ پست در کانال.")
    rows = [
        [_btn(("⛔ خاموش‌کردنِ جشنواره" if cfg["on"] else "🟢 روشن‌کردنِ جشنواره"), "ppr:toggle",
              ButtonStyle.DANGER if cfg["on"] else ButtonStyle.SUCCESS)],
        [_btn("🕒 تعیینِ شروع", "ppr:start", ButtonStyle.PRIMARY), _btn("🕓 تعیینِ پایان", "ppr:end", ButtonStyle.PRIMARY)],
        [_btn(f"💎 قاعدهٔ حرفه‌ای ({cfg['pro_buy']}→{cfg['pro_free']})", "ppr:pro", ButtonStyle.PRIMARY)],
        [_btn(f"🚀 قاعدهٔ رشد ({cfg['growth_buy']}→{cfg['growth_free']})", "ppr:growth", ButtonStyle.PRIMARY)],
        [_btn(f"📣 کانالِ اطلاعیه: {_html.escape(cfg['channel']) if cfg['channel'] else 'وصل نشده'}", "ppr:channel", ButtonStyle.PRIMARY)],
        [_btn(("📨 ارسالِ خودکار به همکاران: روشن ✅" if cfg["notify"] else "📨 ارسالِ خودکار به همکاران: خاموش"), "ppr:notify")],
        [_btn("📤 ارسالِ دستیِ اطلاعیه", "ppr:send"), _btn("📝 متنِ اطلاعیه", "ppr:text")],
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


_KIND_LABEL = {"start": "🟢 شروع", "soon": "⏰ ۲ ساعتِ مانده", "end": "🏁 پایان"}


async def partner_owner_ids() -> list[int]:
    from models import PartnerBot
    from sqlalchemy import select
    async with Session() as s:
        ids = (await s.scalars(select(PartnerBot.owner_id).where(PartnerBot.status == "active").distinct())).all()
    return [int(i) for i in ids if i]


async def _channel_markup(bot) -> InlineKeyboardMarkup | None:
    try:
        me = await bot.get_me()
        if me.username:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 ورود به ربات و خرید", url=f"https://t.me/{me.username}")]])
    except Exception:
        logger.exception("ppromo: گرفتنِ یوزرنیمِ ربات برایِ دکمهٔ پستِ کانال ناموفق بود")
    return None


async def broadcast(bot, kind: str, cfg: dict, *, partners: bool = True, delay: float = 0.06) -> dict:
    """اطلاعیه را به همکاران (پیامِ خصوصی) و کانال می‌فرستد؛ خطایِ هر مقصد جدا گزارش می‌شود، نه جلویِ بقیه را می‌گیرد."""
    text = PP.announcement_text(kind, cfg)
    res = {"sent": 0, "failed": 0, "total": 0, "channel": None}
    if partners:
        ids = await partner_owner_ids()
        res["total"] = len(ids)
        for uid in ids:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                res["sent"] += 1
            except Exception:
                res["failed"] += 1
                logger.info("ppromo: پیام به همکار %s نرسید", uid, exc_info=True)
            if delay:
                await asyncio.sleep(delay)
    if cfg.get("channel"):
        try:
            ch = cfg["channel"]
            await bot.send_message(int(ch) if ch.lstrip("-").isdigit() else ch, text, parse_mode="HTML",
                                   reply_markup=await _channel_markup(bot))
            res["channel"] = True
        except Exception as e:
            res["channel"] = False
            logger.warning("ppromo: ارسال به کانال %s ناموفق بود: %s", cfg.get("channel"), e)
    return res


def _summary(kind: str, res: dict, cfg: dict) -> str:
    ch = ("✅ کانال" if res["channel"] else "❌ کانال (ربات را ادمینِ کانال با اجازهٔ ارسال کن)") if res["channel"] is not None else "—"
    return (f"📣 اطلاعیهٔ جشنواره ({_KIND_LABEL[kind]}) ارسال شد\n"
            f"👥 همکاران: {res['sent']} از {res['total']}" + (f" (ناموفق: {res['failed']})" if res["failed"] else "") + f"\n{ch}")


async def run_due(bot, now=None) -> list[tuple[str, dict]]:
    """یک دور بررسی: اطلاعیه‌هایِ موعدرسیده را می‌فرستد. علامتِ «ارسال شد» *قبل* از ارسال ثبت می‌شود تا ری‌استارت/خطا پیامِ تکراری نسازد."""
    cfg = await PP.load()
    now = now or PP.now_local()
    done = []
    for kind in PP.due_kinds(cfg, now):
        await PP.set_value(f"ppromo_sent_{kind}", PP.round_id(cfg))
        res = await broadcast(bot, kind, cfg, partners=cfg["notify"])
        done.append((kind, res))
        if PLATFORM_ADMIN_ID:
            try:
                await bot.send_message(PLATFORM_ADMIN_ID, _summary(kind, res, cfg))
            except Exception:
                logger.exception("ppromo: گزارشِ ارسال به مدیر نرسید")
    return done


async def partner_promo_loop(platform_bot) -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await run_due(platform_bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ppromo: خطایِ حلقهٔ اطلاعیهٔ جشنواره")
        await asyncio.sleep(60)


@partner_promo_router.callback_query(F.data == "ppr:text")
async def ppr_text(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    cfg = await PP.load()
    if not (cfg["start"] and cfg["end"]):
        return await cb.answer("اول شروع و پایان را تعیین کن.", show_alert=True)
    await cb.message.answer("\n\n— — —\n\n".join(PP.announcement_text(k, cfg) for k in ("start", "soon", "end")),
                            reply_markup=_back_kb(), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.callback_query(F.data == "ppr:notify")
async def ppr_notify(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    cfg = await PP.load()
    await PP.set_value("ppromo_notify", "0" if cfg["notify"] else "1")
    await _show(cb)
    await cb.answer("ارسال به همکاران خاموش شد (کانال همچنان کار می‌کند)" if cfg["notify"] else "ارسال به همکاران روشن شد")


@partner_promo_router.callback_query(F.data == "ppr:channel")
async def ppr_channel(cb: CallbackQuery, state: FSMContext):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    await state.set_state(PromoFSM.channel)
    await cb.message.answer(
        "📣 <b>کانالِ اطلاعیه</b>\n\nیوزرنیمِ کانال (مثل <code>@mychannel</code>) یا آیدیِ عددی (<code>-1001234567890</code>) را بفرست. "
        "ربات باید در آن کانال <b>ادمین</b> و دارایِ اجازهٔ ارسالِ پست باشد.\nبرایِ برداشتنِ کانال <code>حذف</code> بفرست.",
        reply_markup=_back_kb(), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.message(PromoFSM.channel, F.text)
async def ppr_channel_in(msg: Message, state: FSMContext):
    if not await _allowed(msg.from_user.id):
        return
    val = (msg.text or "").strip()
    if val.startswith("/"):
        return
    if val in ("حذف", "delete"):
        await PP.set_value("ppromo_channel", "")
    elif val.lstrip("-").isdigit() or (val.startswith("@") and len(val) >= 5 and val[1:].replace("_", "").isalnum()):
        await PP.set_value("ppromo_channel", val)
    else:
        return await msg.answer("❌ یوزرنیم مثل <code>@mychannel</code> یا آیدیِ عددی بفرست (یا <code>حذف</code>).",
                                reply_markup=_back_kb(), parse_mode="HTML")
    await state.clear()
    text, kb = await _panel()
    await msg.answer("✅ ذخیره شد. با «📤 ارسالِ دستیِ اطلاعیه» می‌توانی همین حالا امتحانش کنی.\n\n" + text,
                     reply_markup=kb, parse_mode="HTML")


@partner_promo_router.callback_query(F.data == "ppr:send")
async def ppr_send_menu(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    cfg = await PP.load()
    if not (cfg["start"] and cfg["end"]):
        return await cb.answer("اول شروع و پایان را تعیین کن.", show_alert=True)
    n = len(await partner_owner_ids())
    rows = [[_btn(f"{_KIND_LABEL[k]}", f"ppr:sendask:{k}", ButtonStyle.PRIMARY)] for k in ("start", "soon", "end")]
    rows.append([_btn("⬅️ بازگشت", "ppr:menu", ButtonStyle.DANGER)])
    await cb.message.answer(f"📤 کدام اطلاعیه همین حالا ارسال شود؟\n👥 همکارِ فعال: {n}"
                            + (f" · 📣 کانال: {_html.escape(cfg['channel'])}" if cfg["channel"] else " · کانال وصل نیست"),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.callback_query(F.data.startswith("ppr:sendask:"))
async def ppr_send_ask(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    kind = cb.data.split(":")[2]
    if kind not in _KIND_LABEL:
        return await cb.answer()
    cfg = await PP.load()
    n = len(await partner_owner_ids())
    await cb.message.answer(
        f"⚠️ اطلاعیهٔ «{_KIND_LABEL[kind]}» برایِ {n} همکار" + (" و کانال" if cfg["channel"] else "") + " ارسال شود؟\n\n"
        + PP.announcement_text(kind, cfg),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ ارسال", f"ppr:sendgo:{kind}", ButtonStyle.SUCCESS)],
            [_btn("⬅️ انصراف", "ppr:send", ButtonStyle.DANGER)]]), parse_mode="HTML")
    await cb.answer()


@partner_promo_router.callback_query(F.data.startswith("ppr:sendgo:"))
async def ppr_send_go(cb: CallbackQuery):
    if not await _allowed(cb.from_user.id):
        return await cb.answer("دسترسی ندارید", show_alert=True)
    kind = cb.data.split(":")[2]
    if kind not in _KIND_LABEL:
        return await cb.answer()
    cfg = await PP.load()
    await cb.answer("در حال ارسال…")
    res = await broadcast(cb.bot, kind, cfg, partners=True)
    await cb.message.answer(_summary(kind, res, cfg), reply_markup=_back_kb())
