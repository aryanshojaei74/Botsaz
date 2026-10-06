"""
tenant/partner_panel.py — پنلِ مدیریتِ «ربات همکار» (سمتِ مستأجر).

=== PATCH_PARTNER_PANEL_V1 ===
مهم: ربات همکار آپدیت‌هایش را از main_tenant.py می‌گیرد، نه سرویسِ
پلتفرم. پس کلِ پنلش باید همین‌جا در tenant/ باشد و طبقِ قاعدهٔ پروژه
حق ندارد platform_bot.py را import کند.

نتیجهٔ عملی: چیزهایی که در platform_bot.py ثابتِ ماژول‌اند (قیمتِ پلن‌ها،
شمارهٔ کارت) اینجا مستقیم از جدولِ Setting با bot_id=0 خوانده می‌شوند —
همان جایی که خودِ get_pset/set_pset می‌نویسند. پس تغییرِ قیمت از پنلِ
مدیرِ پلتفرم بلافاصله اینجا هم اثر می‌کند.

فازِ ۴: «🛒 خرید ربات جهت فروش» + آمارِ خرید/فروش/موجودی.
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

from config import Session, PLATFORM_BOT_TOKEN, PLATFORM_ADMIN_ID
from models import (
    Bot as BotRow, PlatformUser, PlatformTxn, Setting,
    PartnerBot, PartnerSale, PartnerStockOrder,
)

from partner_rules import (
    LOW_STOCK_GRACE_DAYS, MIN_ACTIVE_SALES, min_active_sales, MAX_ORDER_QTY,
    jalali,  # === PATCH_PARTNER_V4 ===
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_panel_router = Router()

# قیمتِ پایهٔ پلن‌ها (فقط fallback؛ مقدارِ واقعی از Setting خوانده می‌شود)
_FALLBACK_PRICE = {"growth": 5_000_000, "pro": 15_000_000}
_PLAN_TITLE = {"growth": "رشد", "pro": "حرفه‌ای"}


class PartnerBuyFSM(StatesGroup):
    qty_growth = State()
    qty_pro = State()
    receipt = State()


# ────────── خواندنِ تنظیماتِ سطحِ پلتفرم (بدونِ import از platform_bot) ──────────

async def _pset(key: str, default: str = "") -> str:
    async with Session() as s:
        r = await s.scalar(
            select(Setting).where(Setting.bot_id == 0, Setting.key == key))
    return r.value if (r and r.value is not None) else default


# === PATCH_PARTNER_ORDER_SMS_AUTO_V1 ===
# مبلغِ اختصاصی برایِ خریدِ عمدهٔ همکار، هم‌راستا با همون مکانیزمِ
# «شارژِ خودکارِ کیف‌پول» (platform_bot._sms_pick_unique_amount) — ولی
# مستقل و بدون import از platform_bot (طبقِ قاعدهٔ بالای فایل)، چون
# فقط از رویِ همون جدولِ Setting (bot_id=0) و PlatformTxn می‌خونه که
# هر دو سرویس مشترکاً بهش دسترسی دارن.
async def _pp_pick_unique_amount(base_toman: int) -> int:
    import random
    from datetime import datetime, timedelta
    off_min = int((await _pset("platform_sms_offset_min", "100")) or "100")
    off_max = int((await _pset("platform_sms_offset_max", "4999")) or "4999")
    off_min = max(10, min(off_min, 50_000))
    off_max = max(off_min + 1, min(off_max, 50_000))
    ttl_min = max(2, min(int((await _pset("platform_sms_ttl_min", "15")) or "15"), 120))
    cutoff = datetime.utcnow() - timedelta(minutes=ttl_min)
    async with Session() as s:
        pending = (await s.scalars(select(PlatformTxn).where(
            PlatformTxn.kind == "sms_pending", PlatformTxn.created_at >= cutoff))).all()
        taken = {int(float(p.amount)) for p in pending}
    for _ in range(30):
        candidate = base_toman + random.randint(off_min, off_max)
        if candidate not in taken:
            return candidate
    return base_toman + random.randint(off_min, off_max) + random.randint(1, 500)


async def base_price(plan: str) -> int:
    """قیمتِ پایهٔ پلن از تنظیماتِ پلتفرم (همان کلیدی که ادمین ویرایش می‌کند)."""
    raw = await _pset(f"plan_price_{plan}", "")
    if raw.isdigit():
        return int(raw)
    return _FALLBACK_PRICE.get(plan, 0)


def rial(n) -> str:
    try:
        return f"{int(n):,} تومان"
    except Exception:
        return f"{n} تومان"


# ────────── دسترسی ──────────

async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    """فقط مالکِ یک رباتِ همکارِ فعال اجازهٔ ورود دارد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    uid = cb_or_msg.from_user.id
    if uid != row.owner_id:
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb or pb.status != "active":
        return None
    return row, pb


# ────────── آمار ──────────

async def partner_stats(pb_id: int) -> dict:
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            return {}
        sold = await s.scalar(
            select(func.count(PartnerSale.id))
            .where(PartnerSale.partner_bot_id == pb_id,
                   PartnerSale.kind == "sold")) or 0
        trials = await s.scalar(
            select(func.count(PartnerSale.id))
            .where(PartnerSale.partner_bot_id == pb_id,
                   PartnerSale.kind == "trial")) or 0
        active = await s.scalar(
            select(func.count(PartnerSale.id))
            .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == pb_id,
                   PartnerSale.kind == "sold",
                   BotRow.status == "active",
                   BotRow.archived_at.is_(None))) or 0
        revenue = await s.scalar(
            select(func.coalesce(func.sum(PartnerSale.price), 0))
            .where(PartnerSale.partner_bot_id == pb_id)) or 0
        return {
            "bought_growth": pb.bought_growth, "bought_pro": pb.bought_pro,
            "stock_growth": pb.stock_growth, "stock_pro": pb.stock_pro,
            "sold": sold, "trials": trials, "active": active,
            "revenue": int(revenue), "discount": pb.discount_percent,
            "expires_at": pb.expires_at, "low_stock_since": pb.low_stock_since,
        }


@partner_panel_router.callback_query(F.data == "pp:stats")
async def pp_stats(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    st = await partner_stats(pb.id)

    warn = ""
    if st.get("low_stock_since"):
        deadline = st["low_stock_since"] + datetime.timedelta(days=LOW_STOCK_GRACE_DAYS)
        left = (deadline - datetime.datetime.now()).days
        warn = (f"\n\n⚠️ <b>هشدار حد نصاب</b>\nربات‌های فعال شما "
                f"({st['active']}) کمتر از {await min_active_sales()} تاست. "
                f"تا <b>{max(0, left)} روز</b> دیگر فرصت داری.")

    await cb.message.edit_text(
        "📊 <b>آمار کلی ربات همکار</b>\n\n"
        f"🏷 تخفیف همکاری: <b>{st['discount']}٪</b>\n"
        f"⏳ اعتبار تا: {jalali(st['expires_at'])}\n\n"
        f"🛒 خریداری‌شده: رشد <b>{st['bought_growth']}</b> | حرفه‌ای <b>{st['bought_pro']}</b>\n"
        f"📦 موجودی آماده: رشد <b>{st['stock_growth']}</b> | حرفه‌ای <b>{st['stock_pro']}</b>\n"
        f"✅ فروخته‌شده: <b>{st['sold']}</b> (فعال: {st['active']})\n"
        f"🎁 تست‌های داده‌شده: <b>{st['trials']}</b>\n"
        f"💰 مجموع فروش: <b>{rial(st['revenue'])}</b>"
        + warn,
        reply_markup=_stats_kb())   # PATCH_PARTNER_STOCK_LOG_V1
    await cb.answer()


# === PATCH_PARTNER_STOCK_LOG_V1 ===
def _stats_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📜 تاریخچهٔ انبار", callback_data="pp:stocklog",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="pp:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    return b.as_markup()


_STOCK_REASON_FA = {
    "buy": "🛒 خرید از بات‌ساز",
    "sale": "💰 فروش به کاربر",
    "renew": "🔄 تمدید ربات",
    "upgrade": "⬆️ ارتقا به حرفه‌ای",
    "upgrade_from_trial": "⬆️ ارتقای ربات تست",
    "admin": "🛠 تنظیم توسط مدیر بات‌ساز",
    "refund": "↩️ برگشت به انبار",
}


@partner_panel_router.callback_query(F.data == "pp:stocklog")
async def pp_stock_log(cb: CallbackQuery, bot: Bot):
    """۲۰ حرکتِ آخرِ انبار، با دلیلِ هرکدام.

    این صفحه بعد از شکایتِ «انبارم بی‌دلیل کم شد» اضافه شد؛ بیشترِ آن
    موارد ارتقای رباتِ رشد به حرفه‌ای بود که واحدِ حرفه‌ای مصرف می‌کرد
    ولی فروشِ تازه‌ای ثبت نمی‌شد.
    """
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    from models import PartnerStockLog
    async with Session() as s:
        rows = (await s.scalars(
            select(PartnerStockLog)
            .where(PartnerStockLog.partner_bot_id == pb.id)
            .order_by(PartnerStockLog.id.desc()).limit(20))).all()

    if not rows:
        body = ("هنوز حرکتی ثبت نشده.\n\nاین دفترچه از لحظهٔ به‌روزرسانی "
                "شروع به ثبت می‌کند، پس خرید و فروش‌های قدیمی‌تر در آن نیست.")
    else:
        lines = []
        for r in rows:
            plan_fa = "رشد" if r.plan == "growth" else "حرفه‌ای"
            sign = f"{r.delta:+d}"
            after = f" → موجودی {r.balance_after}" if r.balance_after is not None else ""
            lines.append(
                f"{jalali(r.created_at)}\n"
                f"  {_STOCK_REASON_FA.get(r.reason, r.reason)} · {plan_fa} "
                f"<b>{sign}</b>{after}")
        body = "\n".join(lines)

    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت به آمار", callback_data="pp:stats",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "📜 <b>تاریخچهٔ انبار</b>\n"
        f"📦 موجودی فعلی: رشد <b>{pb.stock_growth}</b> | "
        f"حرفه‌ای <b>{pb.stock_pro}</b>\n\n" + body,
        reply_markup=b.as_markup())
    await cb.answer()


def _back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت", callback_data="pp:home",
             style=ButtonStyle.DANGER)  # === PATCH_PARTNER_V6 ===
    b.adjust(1)
    return b.as_markup()


# ────────── خرید ربات جهت فروش ──────────

def _buy_kb(g: int, p: int, price_g: int, price_p: int, disc: int) -> InlineKeyboardMarkup:
    """شمارندهٔ شیشه‌ای: عدد روی خودِ دکمه می‌نشیند."""
    total = g * price_g + p * price_p
    rows = [
        [InlineKeyboardButton(text="➖", callback_data=f"pp:buy:g:{max(0, g - 1)}:{p}",
                             style=ButtonStyle.DANGER),
         InlineKeyboardButton(text=f"🚀 رشد: {g}", callback_data="pp:noop",
                             style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="➕", callback_data=f"pp:buy:g:{g + 1}:{p}",
                             style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="➖", callback_data=f"pp:buy:p:{g}:{max(0, p - 1)}",
                             style=ButtonStyle.DANGER),
         InlineKeyboardButton(text=f"💎 حرفه‌ای: {p}", callback_data="pp:noop",
                             style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="➕", callback_data=f"pp:buy:p:{g}:{p + 1}",
                             style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=f"💰 مبلغ نهایی: {total:,} تومان", callback_data="pp:noop",
                             style=ButtonStyle.PRIMARY)],
    ]
    if total > 0:
        rows.append([InlineKeyboardButton(
            text="✅ تایید و پرداخت", callback_data=f"pp:order:{g}:{p}",
            style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🔙 لغو و برگشت", callback_data="pp:home",
                                     style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _buy_text(disc: int, price_g: int, price_p: int) -> str:
    bg = await base_price("growth")
    bp = await base_price("pro")
    return (
        "🛒 <b>خرید ربات جهت فروش</b>\n\n"
        f"🏷 تخفیف همکاری شما: <b>{disc}٪</b> روی قیمت پایه\n\n"
        f"🚀 رشد: <s>{bg:,}</s> → <b>{price_g:,}</b> تومان\n"
        f"💎 حرفه‌ای: <s>{bp:,}</s> → <b>{price_p:,}</b> تومان\n\n"
        "با دکمه‌های ➕ و ➖ تعداد را مشخص کن:"
    )


async def _partner_prices(pb: PartnerBot) -> tuple[int, int]:
    """قیمتِ همکار = قیمتِ پایه منهایِ درصدِ تخفیفِ همکار.

    عمداً رویِ قیمتِ *پایه* اعمال می‌شود، نه رویِ قیمتِ بعد از تخفیف‌هایِ
    عمومیِ موقت (plan_disc_*) — تا تخفیف‌ها رویِ هم انباشته نشوند.
    """
    disc = max(0, min(90, pb.discount_percent or 0))
    # === PATCH_TIMED_DISCOUNT_V1 ===
    # تخفیفِ زمان‌دارِ «ربات همکار» (اگر مدیرِ پلتفرم فعالش کرده باشد) با
    # تخفیفِ خودِ همکار جمع نمی‌شود؛ بزرگ‌ترشان اعمال می‌شود، وگرنه دو
    # تخفیفِ ۳۰٪ می‌شد ۶۰٪ و قیمت‌ها بی‌هوا نصف می‌شد.
    try:
        from platform_bot import sched_disc_pct
        disc = max(disc, sched_disc_pct("partner"))
    except Exception:
        logger.exception("خواندنِ تخفیفِ زمان‌دارِ همکار ناموفق بود؛ تخفیفِ عادی اعمال شد")
    g = await base_price("growth")
    p = await base_price("pro")
    return g * (100 - disc) // 100, p * (100 - disc) // 100


@partner_panel_router.callback_query(F.data == "pp:noop")
async def pp_noop(cb: CallbackQuery):
    await cb.answer()


@partner_panel_router.callback_query(F.data == "pp:buy")
async def pp_buy_start(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    pg, pp = await _partner_prices(pb)
    await cb.message.edit_text(
        await _buy_text(pb.discount_percent, pg, pp),
        reply_markup=_buy_kb(0, 0, pg, pp, pb.discount_percent))
    await cb.answer()


@partner_panel_router.callback_query(F.data.startswith("pp:buy:"))
async def pp_buy_adjust(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    parts = cb.data.split(":")
    g, p = int(parts[3]), int(parts[4])
    if g > MAX_ORDER_QTY or p > MAX_ORDER_QTY:
        await cb.answer(f"حداکثر {MAX_ORDER_QTY} عدد در هر سفارش", show_alert=True)
        return
    pg, pp = await _partner_prices(pb)
    try:
        await cb.message.edit_reply_markup(
            reply_markup=_buy_kb(g, p, pg, pp, pb.discount_percent))
    except Exception:
        logger.exception("ویرایشِ شمارندهٔ خرید ناموفق بود")
    await cb.answer()


@partner_panel_router.callback_query(F.data.startswith("pp:order:"))
async def pp_pay_choose(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    parts = cb.data.split(":")
    g, p = int(parts[2]), int(parts[3])
    if g + p == 0:
        await cb.answer("اول تعداد را مشخص کن", show_alert=True)
        return

    pg, pp = await _partner_prices(pb)
    total = g * pg + p * pp
    await state.update_data(pp_g=g, pp_p=p, pp_total=total, pp_pb=pb.id)

    async with Session() as s:
        u = await s.scalar(
            select(PlatformUser).where(PlatformUser.telegram_id == cb.from_user.id))
        bal = int(u.balance) if u else 0

    b = InlineKeyboardBuilder()
    b.button(text=f"👛 پرداخت از کیف پول ({bal:,})", callback_data="pp:pay:wallet",
             style=ButtonStyle.SUCCESS)
    b.button(text="💳 کارت به کارت", callback_data="pp:pay:card",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 برگشت", callback_data="pp:buy",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "🧾 <b>تایید سفارش</b>\n\n"
        f"🚀 رشد: {g} عدد\n"
        f"💎 حرفه‌ای: {p} عدد\n"
        f"💰 مبلغ نهایی: <b>{total:,} تومان</b>\n\n"
        f"👛 موجودی کیف پول شما: {bal:,} تومان\n\n"
        "روش پرداخت را انتخاب کن:",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_panel_router.callback_query(F.data == "pp:pay:wallet")
async def pp_pay_wallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    data = await state.get_data()
    g, p, total, pb_id = (data.get("pp_g"), data.get("pp_p"),
                          data.get("pp_total"), data.get("pp_pb"))
    if not pb_id or total is None:
        await cb.answer("سفارش منقضی شده؛ دوباره شروع کن", show_alert=True)
        return

    uid = cb.from_user.id
    async with Session() as s:
        u = await s.scalar(
            select(PlatformUser).where(PlatformUser.telegram_id == uid).with_for_update())
        if not u or float(u.balance) < total:
            bal = int(u.balance) if u else 0
            await cb.answer(
                f"موجودی کافی نیست. لازم: {total:,} — موجودی: {bal:,}", show_alert=True)
            return
        u.balance = float(u.balance) - total

        pb = await s.get(PartnerBot, pb_id)
        pb.stock_growth += g
        pb.stock_pro += p
        pb.bought_growth += g
        pb.bought_pro += p

        s.add(PartnerStockOrder(
            partner_bot_id=pb_id, qty_growth=g, qty_pro=p, amount=total,
            method="wallet", status="approved",
            decided_at=datetime.datetime.now()))
        s.add(PlatformTxn(user_telegram_id=uid, amount=-total, kind="purchase",
                          note=f"خرید عمده ربات همکار: {g} رشد + {p} حرفه‌ای"))
        await s.commit()
        new_g, new_p = pb.stock_growth, pb.stock_pro
        pb_bot_id = pb.bot_id

    # === PATCH_PARTNER_V27 ===
    try:
        from tenant.partner_bot.partner_sales import invalidate_pb_cache
        # قبلاً invalidate_pb_cache(row.id) بود ولی `row` در این تابع تعریف
        # نشده — NameError بی‌صدا گرفته می‌شد و کشِ موجودی باطل نمی‌شد.
        invalidate_pb_cache(pb_bot_id)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ PartnerBot ناموفق بود")

    await state.clear()
    await cb.message.edit_text(
        "✅ <b>پرداخت انجام شد</b>\n\n"
        f"مبلغ {total:,} تومان از کیف پولت کسر شد.\n\n"
        f"📦 موجودی جدید:\n🚀 رشد: <b>{new_g}</b>\n💎 حرفه‌ای: <b>{new_p}</b>",
        reply_markup=_back_kb())
    await cb.answer()
    await _notify_platform_admin(
        f"🛒 <b>خرید عمدهٔ همکار (کیف پول)</b>\n\n"
        f"👤 همکار: <code>{uid}</code>\n"
        f"🚀 رشد: {g} | 💎 حرفه‌ای: {p}\n"
        f"💰 مبلغ: {total:,} تومان")


@partner_panel_router.callback_query(F.data == "pp:pay:card")
async def pp_pay_card(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    data = await state.get_data()
    total = data.get("pp_total")
    if total is None:
        await cb.answer("سفارش منقضی شده؛ دوباره شروع کن", show_alert=True)
        return

    card = await _pset("platform_card", "")
    holder = await _pset("card_holder", "")
    if not card:
        await cb.answer("پرداخت کارت‌به‌کارت فعلاً در دسترس نیست.", show_alert=True)
        return

    g, p, pb_id = data.get("pp_g"), data.get("pp_p"), data.get("pp_pb")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 برگشت", callback_data=f"pp:order:{g}:{p}",
                             style=ButtonStyle.DANGER)]])

    # === PATCH_PARTNER_ORDER_SMS_AUTO_V1 ===
    # اگه تاییدِ خودکارِ کارت‌به‌کارت (همون سیستمِ شارژِ آنیِ کیف‌پول) روشنه،
    # خریدِ عمدهٔ همکار هم از همین مسیر رد میشه: مبلغِ اختصاصی می‌گیره،
    # با رسیدنِ پیامکِ بانکی خودکار تایید و به موجودیِ همکار اضافه میشه —
    # دیگه نیازی به عکسِ رسید و تاییدِ دستیِ ادمین نیست.
    if (await _pset("platform_sms_enabled", "0")) == "1":
        async with Session() as s:
            o = PartnerStockOrder(
                partner_bot_id=pb_id, qty_growth=g, qty_pro=p, amount=total,
                method="card_auto", status="pending")
            s.add(o)
            await s.commit()
            await s.refresh(o)
            order_id = o.id

        unique_amount = await _pp_pick_unique_amount(total)
        ttl_min = max(2, min(int((await _pset("platform_sms_ttl_min", "15")) or "15"), 120))
        # === PATCH_PARTNER_ORDER_SMS_AUTO_INVOICE_DELETE_V1 ===
        # همینِ پیامِ فعلی (که الان editش می‌کنیم) خودِ فاکتوره؛ mid/chat رو
        # از قبل تویِ note ذخیره می‌کنیم تا بعدِ تاییدِ خودکار، خودِ همین
        # پیام پاک بشه (هماهنگ با فاکتورِ شارژِ کیف‌پول).
        async with Session() as s:
            s.add(PlatformTxn(
                user_telegram_id=cb.from_user.id, amount=float(unique_amount),
                kind="sms_pending",
                note=f"card={card}|pp_order={order_id}|mid={cb.message.message_id}|chat={cb.message.chat.id}"))
            await s.commit()

        await state.clear()
        await cb.message.edit_text(
            "📲 <b>خریدِ عمده — کارت‌به‌کارتِ خودکار (تاییدِ آنی)</b>\n\n"
            f"❗ برای تاییدِ خودکار، <b>دقیقاً</b> این مبلغ رو واریز کن:\n\n"
            f"💰 <code>{unique_amount:,}</code> تومان\n\n"
            f"💳 <code>{card}</code>\n"
            f"🧑 {holder or '—'}\n\n"
            f"⏰ مهلت: {ttl_min} دقیقه. اگه مبلغ رو تغییر بدی، خودکار تایید نمیشه.\n"
            "به‌محضِ ثبتِ پیامکِ بانکی، رشد/حرفه‌ای‌های خریداری‌شده خودکار به "
            "موجودیِ ربات همکارت اضافه میشن؛ نیازی به فرستادنِ رسید نیست.\n\n"
            f"🧾 سفارش #{order_id}",
            reply_markup=back_kb)
        await cb.answer()
        return

    # مسیرِ قدیمی (وقتی تاییدِ خودکار خاموش باشه): عکسِ رسید + تاییدِ دستیِ ادمین
    await state.set_state(PartnerBuyFSM.receipt)
    await cb.message.edit_text(
        f"💳 <b>کارت به کارت</b>\n\n"
        f"مبلغ <b>{total:,} تومان</b> را به کارت زیر واریز کن:\n\n"
        f"<code>{card}</code>\n"
        f"🧑 {holder or '—'}\n\n"
        "سپس <b>عکس رسید</b> را همین‌جا بفرست.\n"
        "بعد از تایید مدیریت، ربات‌ها به موجودی‌ات اضافه می‌شوند.",
        reply_markup=back_kb)
    await cb.answer()


@partner_panel_router.message(PartnerBuyFSM.receipt, F.photo)
async def pp_receipt_received(msg: Message, bot: Bot, state: FSMContext):
    ok = await _partner_ok(msg, bot)
    if not ok:
        return
    data = await state.get_data()
    g, p, total, pb_id = (data.get("pp_g"), data.get("pp_p"),
                          data.get("pp_total"), data.get("pp_pb"))
    await state.clear()
    if not pb_id:
        await msg.answer("سفارش منقضی شده؛ دوباره از پنل شروع کن.")
        return

    file_id = msg.photo[-1].file_id
    async with Session() as s:
        o = PartnerStockOrder(
            partner_bot_id=pb_id, qty_growth=g, qty_pro=p, amount=total,
            method="card", status="pending", receipt_file_id=file_id)
        s.add(o)
        await s.commit()
        order_id = o.id

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data=f"ppord:{order_id}:y",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"ppord:{order_id}:n",
                             style=ButtonStyle.DANGER),
    ]])
    sent = await _notify_platform_admin(
        f"💳 <b>رسید خرید عمدهٔ همکار</b>\n\n"
        f"👤 همکار: <code>{msg.from_user.id}</code>\n"
        f"🚀 رشد: {g} | 💎 حرفه‌ای: {p}\n"
        f"💰 مبلغ: {total:,} تومان\n"
        f"🧾 سفارش #{order_id}",
        photo=file_id, kb=kb, src_bot=bot)

    # === PATCH_PARTNER_V14 ===
    # به کاربر حقیقت را بگو: سفارش در هر حال ثبت شده، ولی اگر پیام به
    # مدیریت نرسیده باید بداند تا پیگیری کند.
    if sent:
        await msg.answer(
            f"✅ رسید دریافت شد و برای بررسی ارسال گردید (سفارش #{order_id}).\n"
            "به‌محض تایید، ربات‌ها به موجودی‌ات اضافه می‌شوند.")
    else:
        await msg.answer(
            f"⚠️ سفارش #{order_id} ثبت شد، ولی ارسالِ خودکار به مدیریت "
            "ناموفق بود.\n\n"
            "لطفاً از بخش «🆘 ارجاع به پشتیبانی بات‌ساز» شمارهٔ سفارش را "
            "اطلاع بده تا دستی بررسی شود.")


@partner_panel_router.message(PartnerBuyFSM.receipt)
async def pp_receipt_wrong(msg: Message):
    await msg.answer("لطفاً <b>عکس</b> رسید را بفرست (نه متن).")


# ────────── اطلاع به مدیرِ پلتفرم (از سرویسِ مستأجر) ──────────

async def _notify_platform_admin(text: str, photo: str | None = None,
                                 kb: InlineKeyboardMarkup | None = None,
                                 src_bot: Bot | None = None) -> bool:
    """پیام به ادمینِ پلتفرم از داخلِ سرویسِ مستأجر.

    === PATCH_PARTNER_V14 ===
    سه نکتهٔ مهم که قبلاً باگ بودند:

    ۱. **file_id بینِ ربات‌ها قابلِ انتقال نیست.** عکسی که با توکنِ
       رباتِ همکار آپلود شده، با توکنِ ربات مادر قابلِ ارسال نیست
       (خطایِ «wrong file identifier»). پس فایل باید با رباتِ مبدأ
       دانلود و به‌صورتِ بایت به ربات مادر داده شود. src_bot همان
       رباتِ مبدأ است.

    ۲. **parse_mode لازم است**، وگرنه تگ‌هایِ <b> خام دیده می‌شوند.

    ۳. **نتیجه برگردانده می‌شود** تا صدازننده بتواند به کاربر بگوید
       ارسال ناموفق بوده — قبلاً خطا فقط لاگ می‌شد و کاربر پیامِ
       «✅ ارسال شد» می‌دید در حالی که هیچ‌چیز نرسیده بود.
    """
    if not (PLATFORM_BOT_TOKEN and PLATFORM_ADMIN_ID):
        logger.error("PLATFORM_BOT_TOKEN یا PLATFORM_ADMIN_ID تنظیم نشده")
        return False

    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    payload = None
    if photo and src_bot is not None:
        try:
            from aiogram.types import BufferedInputFile
            import io

            buf = io.BytesIO()
            await src_bot.download(photo, destination=buf)
            payload = BufferedInputFile(buf.getvalue(), filename="receipt.jpg")
        except Exception:
            logger.exception("دانلودِ رسید از رباتِ همکار ناموفق بود")
            payload = None

    try:
        pb = Bot(token=PLATFORM_BOT_TOKEN,
                 default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            if payload is not None:
                await pb.send_photo(PLATFORM_ADMIN_ID, payload,
                                    caption=text, reply_markup=kb)
            else:
                # اگر عکس نرسید، دستِ‌کم متن برسد تا سفارش گم نشود.
                note = "\n\n⚠️ رسید ضمیمه نشد؛ از پنل همکار قابلِ پیگیری است."
                await pb.send_message(PLATFORM_ADMIN_ID,
                                      text + (note if photo else ""),
                                      reply_markup=kb)
        finally:
            await pb.session.close()
        return True
    except Exception:
        logger.exception("اطلاع‌رسانی به ادمینِ پلتفرم ناموفق بود")
        return False


# ======================= تنظیمِ قیمتِ فروش =======================
# === PATCH_PARTNER_PRICING_V1 ===
# قیمتی که همکار به کاربرانِ خودش می‌فروشد. تا وقتی ست نشده، آن پلن در
# منویِ خریدِ کاربر ظاهر نمی‌شود (نگاه کن به tenant/partner_sales.py).

class PartnerPriceFSM(StatesGroup):
    value = State()


async def _sale_price(bot_id: int, plan: str) -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == f"sale_price_{plan}"))
    try:
        return int(r.value) if r and r.value else 0
    except ValueError:
        return 0


async def _set_sale_price(bot_id: int, plan: str, value: int) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == f"sale_price_{plan}"))
        if r:
            r.value = str(value)
        else:
            s.add(Setting(bot_id=bot_id, key=f"sale_price_{plan}", value=str(value)))
        await s.commit()


@partner_panel_router.callback_query(F.data == "pp:prices")
async def pp_prices(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    sg = await _sale_price(row.id, "growth")
    sp = await _sale_price(row.id, "pro")
    cg, cp = await _partner_prices(pb)

    def _fmt(sale: int, cost: int) -> str:
        if sale <= 0:
            return "تنظیم‌نشده ⚠️"
        profit = sale - cost
        sign = "+" if profit >= 0 else ""
        return f"{sale:,} (سود: {sign}{profit:,})"

    b = InlineKeyboardBuilder()
    b.button(text="🚀 تغییر قیمت رشد", callback_data="pp:price:growth",
             style=ButtonStyle.SUCCESS)
    b.button(text="💎 تغییر قیمت حرفه‌ای", callback_data="pp:price:pro",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 بازگشت", callback_data="pp:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        "🏷 <b>قیمت فروش شما</b>\n\n"
        f"🚀 رشد\nقیمت تمام‌شده: {cg:,}\nقیمت فروش: {_fmt(sg, cg)}\n\n"
        f"💎 حرفه‌ای\nقیمت تمام‌شده: {cp:,}\nقیمت فروش: {_fmt(sp, cp)}\n\n"
        "⚠️ تا وقتی قیمت یک پلن تنظیم نشده باشد، آن پلن به کاربرانت "
        "نمایش داده نمی‌شود.",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_panel_router.callback_query(F.data.startswith("pp:price:"))
async def pp_price_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    plan = cb.data.split(":")[2]
    if plan not in ("growth", "pro"):
        await cb.answer()
        return
    cg, cp = await _partner_prices(pb)
    cost = cg if plan == "growth" else cp

    await state.update_data(pp_price_plan=plan, pp_price_bot=row.id, pp_cost=cost)
    await state.set_state(PartnerPriceFSM.value)
    await cb.message.edit_text(
        f"🏷 قیمت فروش <b>{_PLAN_TITLE[plan]}</b>\n\n"
        f"قیمت تمام‌شده برای شما: <b>{cost:,} تومان</b>\n\n"
        "مبلغ فروش را فقط با عدد بفرست (به تومان). مثال: <code>6000000</code>")
    await cb.answer()


@partner_panel_router.message(PartnerPriceFSM.value)
async def pp_price_save(msg: Message, bot: Bot, state: FSMContext):
    ok = await _partner_ok(msg, bot)
    if not ok:
        return
    raw = (msg.text or "").strip().replace(",", "").replace("٬", "")
    if not raw.isdigit():
        await msg.answer("فقط عدد بفرست. مثال: <code>6000000</code>")
        return
    value = int(raw)
    if value <= 0:
        await msg.answer("مبلغ باید بزرگ‌تر از صفر باشد.")
        return

    data = await state.get_data()
    plan = data.get("pp_price_plan")
    bot_id = data.get("pp_price_bot")
    cost = int(data.get("pp_cost") or 0)
    await state.clear()
    if not plan or not bot_id:
        await msg.answer("این مرحله منقضی شده. دوباره از پنل شروع کن.")
        return

    await _set_sale_price(bot_id, plan, value)

    warn = ""
    if value < cost:
        warn = (f"\n\n⚠️ <b>توجه:</b> این مبلغ از قیمت تمام‌شده "
                f"({cost:,}) کمتر است — روی هر فروش ضرر می‌کنی.")
    await msg.answer(
        f"✅ قیمت فروش {_PLAN_TITLE[plan]} روی <b>{value:,} تومان</b> تنظیم شد."
        + warn,
        reply_markup=_back_kb())
