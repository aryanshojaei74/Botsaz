"""=== PATCH_PRODUCT_RATING_V1 ===
نظرسنجیِ خودکارِ رضایت از خرید.

جریانِ کار:
  ۱) ادمین از «پنل مدیریت → 🎛 دکمه‌ها و تنظیمات → ⭐ نظرسنجیِ خرید» این
     قابلیت رو روشن می‌کنه و تعدادِ روزِ تأخیر رو تنظیم می‌کنه (پیش‌فرض ۲ روز).
  ۲) یه حلقهٔ پس‌زمینه (تویِ main_tenant.py) هر ساعت سفارش‌هایِ پرداخت‌شده‌ای
     رو پیدا می‌کنه که به اون تعدادِ روز رسیدن و هنوز ازشون نظرسنجی نشده،
     و برایِ خریدار پیامِ امتیازدهی می‌فرسته.
  ۳) کاربر یکی از ۵ دکمهٔ ستاره رو می‌زنه → امتیاز ذخیره می‌شه، به ادمین
     اطلاع می‌ره، و میانگینِ امتیازِ همون محصول در توضیحاتش به بقیهٔ
     کاربران نشون داده می‌شه (فقط تا وقتی قابلیت روشنه).
"""
from __future__ import annotations

import datetime
import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle  # PATCH_PRATE_BUTTONSTYLE_SCOPE_V1
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger
from models import BotUser, Config, Order, ProductRating

logger = get_logger(__name__)

product_rating_router = Router(name="product_rating")

DEFAULT_DELAY_DAYS = 2
_STAR_LABELS = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}


class RatingFSM(StatesGroup):
    delay_days = State()


# ─────────────────────────── تنظیمات ادمین ───────────────────────────

async def is_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "product_rating_enabled", "off")) == "on"


async def delay_days(bot_id: int) -> int:
    from tenant import get_setting, fa_to_int
    raw = await get_setting(bot_id, "product_rating_delay_days", str(DEFAULT_DELAY_DAYS))
    return fa_to_int(raw) or DEFAULT_DELAY_DAYS


async def _settings_view(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    on = await is_enabled(bot_id)
    days = await delay_days(bot_id)
    text = (
        "⭐ <b>نظرسنجیِ خرید</b>\n\n"
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n"
        f"زمانِ ارسال: <b>{days} روز</b> بعد از خرید\n\n"
        "وقتی روشن باشه، این مدت بعد از هر خریدِ موفق، یه پیام برایِ خریدار "
        "فرستاده می‌شه و ازش خواسته می‌شه بین ۱ تا ۵ ستاره امتیاز بده. "
        "میانگینِ امتیازها هم در توضیحاتِ همون محصول به بقیهٔ کاربران "
        "نمایش داده می‌شه (با خاموش‌کردنِ این گزینه، نمایش هم متوقف می‌شه)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("🟢 نظرسنجی: روشن" if on else "🔴 نظرسنجی: خاموش"),
            callback_data="prate:toggle",
            style=(ButtonStyle.SUCCESS if on else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"⏱ تغییرِ زمانِ ارسال ({days} روز)",
                              callback_data="prate:setdays")],
        [InlineKeyboardButton(text="📊 گزارشِ امتیازها", callback_data="prate:report")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings",
                              style=ButtonStyle.DANGER)],
    ])
    return text, kb


@product_rating_router.callback_query(F.data == "set:productrating")
async def prate_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text, kb = await _settings_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@product_rating_router.callback_query(F.data == "prate:toggle")
async def prate_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "product_rating_enabled", "off")
    await set_setting(row.id, "product_rating_enabled", "off" if cur == "on" else "on")
    text, kb = await _settings_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("✅ ذخیره شد")


@product_rating_router.callback_query(F.data == "prate:setdays")
async def prate_setdays_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(RatingFSM.delay_days)
    await cb.message.answer(
        "⏱ چند روز بعد از خرید، پیامِ نظرسنجی فرستاده بشه؟\n"
        "یه عدد بین ۱ تا ۳۰ بفرست (مثلاً <code>2</code>):")
    await cb.answer()


@product_rating_router.message(RatingFSM.delay_days, F.text)
async def prate_setdays_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, fa_to_int, set_setting, tadmin_kb
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    n = fa_to_int(msg.text or "")
    if not n or n < 1 or n > 30:
        await msg.answer("یه عددِ معتبر بین ۱ تا ۳۰ بفرست:")
        return
    await state.clear()
    await set_setting(row.id, "product_rating_delay_days", str(n))
    await msg.answer(f"✅ تنظیم شد: {n} روز بعد از خرید.",
                     reply_markup=await tadmin_kb(row.id))


@product_rating_router.callback_query(F.data == "prate:report")
async def prate_report(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    async with Session() as s:
        total = await s.scalar(
            select(func.count()).select_from(ProductRating).where(
                ProductRating.bot_id == row.id, ProductRating.stars.isnot(None))) or 0
        avg = await s.scalar(
            select(func.avg(ProductRating.stars)).where(
                ProductRating.bot_id == row.id, ProductRating.stars.isnot(None)))
        rows = (await s.execute(
            select(ProductRating.stars, func.count()).where(
                ProductRating.bot_id == row.id, ProductRating.stars.isnot(None))
            .group_by(ProductRating.stars))).all()
    if not total:
        text = "📊 <b>گزارشِ امتیازها</b>\n\nهنوز امتیازی ثبت نشده."
    else:
        dist = {int(k): v for k, v in rows}
        lines = [f"📊 <b>گزارشِ امتیازها</b>\n",
                 f"میانگین: <b>{float(avg):.1f}</b> از ۵  (بر اساسِ {total} نظر)\n"]
        for st in (5, 4, 3, 2, 1):
            lines.append(f"{_STAR_LABELS[st]} — {dist.get(st, 0)} نفر")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:productrating", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


# ─────────────────── نمایشِ میانگینِ امتیاز در توضیحاتِ محصول ───────────────────

def cfg_key(config_id) -> str | None:
    return f"cfg:{config_id}" if config_id else None


def shop_key(product_id) -> str | None:
    return f"shop:{product_id}" if product_id else None


def smm_key(service_id) -> str | None:
    return f"smm:{service_id}" if service_id else None


def plan_key(plan_id) -> str | None:
    """شناسهٔ امتیازِ یک پلنِ آمادهٔ VPN (هر پلن امتیازِ جدا داره)."""
    return f"plan:{plan_id}" if plan_id else None


def cat_key(category) -> str | None:
    """شناسهٔ امتیاز بر اساسِ *دسته‌بندی*.

    چرا دسته‌بندی بهتر از آیدیِ تکیِ پلن/کانفیگه: پلن‌ها و کانفیگ‌هایِ آماده
    تویِ این پروژه دسته‌بندی می‌شن (مثلاً «پلن حرفه‌ای» / «پلن اقتصادی»)، و
    کانفیگ‌هایِ انبار مدام مصرف و جایگزین می‌شن — یعنی آیدیِ تکشون ناپایداره
    و امتیازها تکه‌تکه می‌شد. دسته‌بندی ثابت می‌مونه، پس امتیازها روی هم
    جمع می‌شن و میانگینِ معنادارتری می‌ده."""
    c = (category or "").strip()
    return f"cat:{c}" if c else None


async def rating_line(bot_id: int, item_key: str | None) -> str:
    """یک خطِ آمادهٔ «امتیازِ این محصول» برایِ چسبوندن به توضیحاتِ محصول.

    item_key شناسهٔ یکتایِ محصوله؛ با هلپرهایِ زیر ساخته می‌شه:
        cfg_key(config_id) / shop_key(product_id) / smm_key(service_id)

    اگه قابلیت خاموش باشه یا هنوز امتیازی ثبت نشده باشه، رشتهٔ خالی برمی‌گردونه
    (یعنی هیچ‌چیزی به صفحهٔ محصول اضافه نمی‌شه).
    """
    if not item_key or not await is_enabled(bot_id):
        return ""
    async with Session() as s:
        avg = await s.scalar(
            select(func.avg(ProductRating.stars)).where(
                ProductRating.bot_id == bot_id,
                ProductRating.item_key == item_key,
                ProductRating.stars.isnot(None)))
        cnt = await s.scalar(
            select(func.count()).select_from(ProductRating).where(
                ProductRating.bot_id == bot_id,
                ProductRating.item_key == item_key,
                ProductRating.stars.isnot(None))) or 0
    if not cnt or avg is None:
        return ""
    rounded = int(round(float(avg)))
    stars = "⭐" * max(1, min(5, rounded))
    return f"\n{stars} امتیازِ این محصول: <b>{float(avg):.1f}</b> از ۵ ({cnt} نظر)"


# ─────────────────────── ارسالِ درخواستِ امتیاز ───────────────────────

def _rating_kb(order_id: int, ticket_label: str) -> InlineKeyboardMarkup:
    stars_row = [
        InlineKeyboardButton(text=f"{i}⭐", callback_data=f"prate:v:{order_id}:{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        stars_row,
        # از همون callbackِ موجودِ «ثبتِ تیکتِ جدید» استفاده می‌کنیم تا دقیقاً
        # همون جریانِ پشتیبانیِ ربات اجرا بشه (نه یه مسیرِ موازیِ جدید).
        [InlineKeyboardButton(text=ticket_label, callback_data="ticket:new")],
    ])


async def send_rating_request(tbot: Bot, bot_id: int, order_id: int,
                               user_tg_id: int, product_title: str,
                               days_passed: int) -> bool:
    """پیامِ نظرسنجی رو برایِ خریدار می‌فرسته. True یعنی فرستاده شد."""
    from tenant import get_setting
    ticket_label = await get_setting(bot_id, "product_rating_ticket_label",
                                     "💬 اگر نظری داری به ما بگو")
    text = (
        "⭐ <b>نظرت برامون مهمه!</b>\n\n"
        f"{days_passed} روز از خریدِ «{_html.escape(product_title)}» می‌گذره.\n"
        "کیفیتش برامون مهم هست — امتیازِ خودت رو ثبت کن:"
    )
    try:
        await tbot.send_message(user_tg_id, text,
                                reply_markup=_rating_kb(order_id, ticket_label))
        return True
    except Exception:
        logger.exception("ارسالِ درخواستِ امتیاز شکست خورد (order=%s)", order_id)
        return False


# ─────────────────────── دریافتِ امتیاز از کاربر ───────────────────────

@product_rating_router.callback_query(F.data.startswith("prate:v:"))
async def prate_vote(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    try:
        _, _, oid_s, stars_s = cb.data.split(":")
        order_id, stars = int(oid_s), int(stars_s)
    except Exception:
        await cb.answer()
        return
    if not 1 <= stars <= 5:
        await cb.answer()
        return

    async with Session() as s:
        # === PATCH_RATING_ORDER_NO_V1 ===
        # order_id به‌تنهایی بینِ انواعِ سفارش یکتا نیست؛ اول رکوردِ *همین کاربر* را بردار
        rec = await s.scalar(select(ProductRating).join(
            BotUser, BotUser.id == ProductRating.user_id).where(
            ProductRating.bot_id == row.id, ProductRating.order_id == order_id,
            BotUser.telegram_id == cb.from_user.id, ProductRating.stars.is_(None)))
        if rec is None:
            rec = await s.scalar(select(ProductRating).where(
                ProductRating.bot_id == row.id, ProductRating.order_id == order_id,
                ProductRating.stars.is_(None)))
        if rec is None:
            rec = await s.scalar(select(ProductRating).where(
                ProductRating.bot_id == row.id, ProductRating.order_id == order_id))
        if not rec:
            await cb.answer("این نظرسنجی دیگر معتبر نیست.", show_alert=True)
            return
        if rec.stars is not None:
            await cb.answer("قبلاً امتیاز دادی — ممنون! 🙏", show_alert=True)
            return
        buyer = await s.get(BotUser, rec.user_id)
        if not buyer or buyer.telegram_id != cb.from_user.id:
            await cb.answer("این نظرسنجی برایِ تو نیست.", show_alert=True)
            return
        rec.stars = stars
        rec.rated_at = datetime.datetime.now()
        _kind = rec.order_kind
        await s.commit()
    # شمارهٔ سفارش برایِ نمایش = همان شناسهٔ واحدِ سفارش (کدِ پیگیری)، نه pk داخلی
    try:
        from models import order_no as _order_no, ProductOrder, SmmOrder
        _model = {"cfg": Order, "shop": ProductOrder, "smm": SmmOrder}.get(_kind)
        shown_no = await _order_no(_model, order_id) if _model else str(order_id)
    except Exception:
        logger.exception("شمارهٔ نمایشیِ سفارش برایِ امتیاز خوانده نشد")
        shown_no = str(order_id)

    await cb.answer("✅ ثبت شد، ممنون از وقتت!")
    try:
        await cb.message.edit_text(
            f"{_STAR_LABELS[stars]}\n\nامتیازت ثبت شد — ممنون که نظرت رو گفتی 🙏")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (ویرایشِ پیامِ نظرسنجی)")

    # اطلاع به ادمین
    try:
        uname = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
        await bot.send_message(
            row.owner_id,
            f"⭐ <b>امتیازِ جدید برایِ یک خرید</b>\n\n"
            f"{_STAR_LABELS[stars]} ({stars} از ۵)\n"
            f"👤 خریدار: {_html.escape(uname)}\n"
            f"🧾 شمارهٔ سفارش: <code>{shown_no}</code>")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ امتیاز به ادمین)")
