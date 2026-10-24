"""
tenant/partner_sales.py — فروشِ ربات توسطِ «ربات همکار» به کاربرانِ خودش.

=== PATCH_PARTNER_SALES_V1 ===
این‌جا سمتِ *خریدار* است: کاربرِ ربات همکار وارد «🤖 ساخت ربات» می‌شود،
پلن (رشد/حرفه‌ای) یا تستِ ۲ روزه را انتخاب می‌کند، از کیفِ پولِ همان
رباتِ همکار پول می‌دهد، و توکنِ رباتِ خودش را می‌فرستد.

تصمیم‌هایِ عمدی:
  • توکن را **خودِ خریدار** از BotFather می‌آورد و owner_id همان خریدار
    ثبت می‌شود — تا مالکیت از ابتدا دستِ او باشد و بعداً سرِ مالکیت
    اختلاف پیش نیاید.
  • رباتِ فروخته‌شده یک رباتِ مستأجرِ کاملاً عادیِ همین پلتفرم است
    (plan=growth/pro)؛ فقط از طریقِ PartnerSale به همکار وصل می‌شود.
  • تستِ ۲ روزه از انبارِ همکار **کم نمی‌شود** (نمونهٔ نمایشی است) و در
    حدِ نصابِ ۱۰ تایی هم شمرده نمی‌شود.
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

from config import Session
from models import (
    Bot as BotRow, BotUser, PartnerBot, PartnerSale, Setting, WalletTxn,
)
from partner_rules import (
    LOW_STOCK_GRACE_DAYS, MIN_ACTIVE_SALES, min_active_sales, log_stock,
    PARTNER_TRIAL_DAYS,
    jalali,  # === PATCH_PARTNER_V4 ===
)
from bot_utils import set_tenant_webhook

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_sales_router = Router()

# مدتِ اعتبارِ رباتی که همکار می‌فروشد (روز) — هم‌تراز با PLAN_DAYS پلتفرم
SOLD_BOT_DAYS = 30

_PLAN_TITLE = {"growth": "🚀 رشد", "pro": "💎 حرفه‌ای"}


class PartnerSaleFSM(StatesGroup):
    token = State()
    receipt = State()   # === PATCH_PARTNER_V20 === رسیدِ کارت‌به‌کارت


# ────────── کمکی‌ها ──────────

# === PATCH_PARTNER_V27 ===
# کشِ کوتاهِ ردیفِ PartnerBot. get_bot_row خودش کش دارد ولی این کوئری
# در هر کلیک تکرار می‌شد و روی Postgres محسوس بود. ۱۰ ثانیه کافی است:
# موجودیِ انبار در لحظهٔ خرید دوباره از دیتابیس خوانده می‌شود، پس این
# کش هیچ تصمیمِ مالی‌ای را کهنه نمی‌کند.
import time as _time

_PB_CACHE: dict[int, tuple["PartnerBot", float]] = {}
_PB_CACHE_TTL = 10.0


def invalidate_pb_cache(bot_id: int | None = None) -> None:
    """بعد از هر تغییرِ PartnerBot (خرید، توقف، تخفیف) صدا زده شود."""
    if bot_id is None:
        _PB_CACHE.clear()
    else:
        _PB_CACHE.pop(bot_id, None)


async def _partner_of(bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    """اگر این ربات یک رباتِ همکارِ فعال است، ردیف‌هایش را برمی‌گرداند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None

    hit = _PB_CACHE.get(row.id)
    if hit and hit[1] > _time.monotonic():
        pb = hit[0]
    else:
        async with Session() as s:
            pb = await s.scalar(select(PartnerBot).where(
                PartnerBot.bot_id == row.id))
        if pb:
            _PB_CACHE[row.id] = (pb, _time.monotonic() + _PB_CACHE_TTL)

    if not pb or pb.status != "active":
        return None
    return row, pb


async def sale_price(bot_id: int, plan: str, telegram_id: int | None = None) -> int:
    """قیمتی که *همکار* برای فروش تعیین کرده (در Setting همان ربات).

    === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
    اگر telegram_id داده شود و همکار «تخفیف بر اساسِ رتبه» را در «🏆
    برترین مشتریان» روشن کرده باشد، درصدِ تخفیفِ رتبهٔ همان خریدار (اگر
    جزوِ ۵ نفرِ برترِ ماهِ اخیر باشد) رویِ قیمت اعمال می‌شود. برایِ ربات‌هایِ
    غیرِ همکار یا وقتی telegram_id داده نشود، رفتار دقیقاً مثلِ قبل است.
    """
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == f"sale_price_{plan}"))
    try:
        price = int(r.value) if r and r.value else 0
    except ValueError:
        price = 0
    if price <= 0 or telegram_id is None:
        return price
    try:
        from tenant.shared.top_customers import get_user_rank, tc_rank_enabled, tc_rank_percent
        if not await tc_rank_enabled(bot_id):
            return price
        rank = await get_user_rank(bot_id, telegram_id)
        if rank is None:
            return price
        pct = await tc_rank_percent(bot_id, rank)
        if pct <= 0:
            return price
        return max(0, price - round(price * min(100, pct) / 100))
    except Exception:
        logger.exception("اعمالِ تخفیفِ رتبه‌ایِ برترین‌مشتریان روی قیمتِ رباتِ همکار ناموفق بود")
        return price


async def refresh_low_stock_timer(partner_bot_id: int) -> None:
    """نسخهٔ سمتِ مستأجرِ همان تابعِ platform_partner (منطق یکسان).

    زیرِ حدِ نصاب → تایمر شروع می‌شود؛ بالای حدِ نصاب → پاک می‌شود.
    """
    async with Session() as s:
        n = await s.scalar(
            select(func.count(PartnerSale.id))
            .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == partner_bot_id,
                   PartnerSale.kind == "sold",
                   BotRow.status == "active",
                   BotRow.archived_at.is_(None))) or 0
        pb = await s.get(PartnerBot, partner_bot_id)
        if not pb:
            return
        # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
        if n >= await min_active_sales():
            if pb.low_stock_since is not None:
                pb.low_stock_since = None
                pb.low_stock_notice = None
                await s.commit()
        else:
            if pb.low_stock_since is None:
                pb.low_stock_since = datetime.datetime.now()
                await s.commit()


# === PATCH_PARTNER_V21 ===
def _back_kb() -> InlineKeyboardMarkup:
    """دکمهٔ بازگشت برایِ پیام‌هایی که وگرنه بن‌بست‌اند."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="ps:menu",
                             style=ButtonStyle.DANGER)]])


# ────────── منویِ خرید ──────────
# === PATCH_PARTNER_FIELD_BEFORE_PLAN_V1_MARKER ===
# ترتیب دقیقاً مثلِ ربات اصلی بات‌ساز شد: اول زمینهٔ ربات (VPN/فروشگاه/
# خدمات/تبلیغاتی)، بعد پلن (رشد/حرفه‌ای/تست). نگاه کن به platform_bot.py:
# choose_field() → show_pitch(). قبلاً برعکس بود و کاربر اول پول می‌داد
# یا تست می‌گرفت، بعد می‌فهمید قرار است چه بسازد.

async def _ps_show_plans(cb: CallbackQuery, row, pb):
    # === PATCH_PARTNER_TOP_CUSTOMERS_V1 ===
    # قیمتِ نشان‌داده‌شده هم باید تخفیفِ رتبه را نشان بدهد، نه فقط قیمتِ
    # نهاییِ زمانِ پرداخت — وگرنه خریدار عددی می‌بیند که بعداً عوض می‌شود.
    pg = await sale_price(row.id, "growth", cb.from_user.id)
    pp = await sale_price(row.id, "pro", cb.from_user.id)

    b = InlineKeyboardBuilder()
    if pb.stock_growth > 0 and pg > 0:
        b.button(text=f"🚀 رشد — {pg:,} تومان", callback_data="ps:pick:growth",
                 style=ButtonStyle.SUCCESS)  # === PATCH_PARTNER_V6 ===
    if pb.stock_pro > 0 and pp > 0:
        b.button(text=f"💎 حرفه‌ای — {pp:,} تومان", callback_data="ps:pick:pro",
                 style=ButtonStyle.SUCCESS)
    if pb.trial_enabled:
        b.button(text=f"🎁 درخواست تست {PARTNER_TRIAL_DAYS} روزه",
                 callback_data="ps:pick:trial", style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_V6 ===
    # قبلاً به "ps:home" اشاره می‌کرد که هیچ هندلری نداشت — دکمه بی‌کار بود.
    b.button(text="🔙 بازگشت به خانه", callback_data="ps:back",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    # === PATCH_PARTNER_V21 ===
    # کاربر باید بداند دقیقاً چه می‌خرد و تفاوتِ پلن‌ها چیست، وگرنه
    # کورکورانه انتخاب می‌کند و بعد شاکی می‌شود.
    lines = [
        "🤖 <b>ساخت ربات تلگرام</b>\n",
        "با خرید هر پلن، یک <b>ربات اختصاصی</b> برای خودت می‌سازی که "
        "کاملاً در اختیار توست: دکمه‌هایش را می‌چینی، کاربر می‌گیری و "
        "می‌توانی باهاش کسب‌وکار راه بیندازی.\n",
        "<b>مراحل کار:</b>",  # === PATCH_PARTNER_SALES_KEYCAP_FIX_V1 ===
        "1️⃣ پلن مورد نظرت را انتخاب کن",
        "2️⃣ هزینه را پرداخت کن (کیف پول یا کارت‌به‌کارت)",
        "3️⃣ در تلگرام به <code>@BotFather</code> برو و یک ربات بساز",
        "4️⃣ توکنی که می‌دهد را برای من بفرست",
        "5️⃣ دکمه‌های صفحهٔ اصلی رباتت را می‌چینی — تمام!\n",
    ]

    if pb.stock_growth > 0 and pg > 0:
        lines.append(f"🚀 <b>رشد</b> — {pg:,} تومان")
        lines.append("   امکانات متوسط، مناسب شروع کار\n")
    if pb.stock_pro > 0 and pp > 0:
        lines.append(f"💎 <b>حرفه‌ای</b> — {pp:,} تومان")
        lines.append("   همهٔ امکانات، بدون محدودیت\n")
    if pb.trial_enabled:
        lines.append(f"🎁 <b>تست {PARTNER_TRIAL_DAYS} روزه</b> — رایگان")
        lines.append("   برای امتحان‌کردن، قبل از خرید\n")

    if pb.stock_growth <= 0 and pb.stock_pro <= 0:
        lines.append("⚠️ در حال حاضر موجودی ربات تمام شده است. "
                     "کمی بعد دوباره سر بزن.")
    else:
        lines.append("👇 یکی از گزینه‌های زیر را انتخاب کن:")

    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@partner_sales_router.callback_query(F.data == "ps:menu")
async def ps_menu(cb: CallbackQuery, bot: Bot, state: FSMContext = None):
    # === PATCH_PARTNER_V7 ===
    # پاک‌کردنِ FSM لازم است: کاربر ممکن است از صفحهٔ «توکن را بفرست»
    # برگشته باشد و اگر state بماند، پیامِ بعدی‌اش اشتباهاً توکن فرض
    # می‌شود. مقدارِ پیش‌فرضِ None برایِ فراخوانی‌هایِ داخلی است
    # (مثلاً از _run_button_action که state پاس نمی‌دهد).
    if state is not None:
        await state.clear()
    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok

    if state is not None:
        await state.update_data(ps_pb=pb.id, ps_bot=row.id)

    # === PATCH_PARTNER_RESUME_ORDER_V1 ===
    # اگر این کاربر یک سفارشِ کارت‌به‌کارتِ *تاییدشده ولی مصرف‌نشده* دارد،
    # نباید دوباره به مسیرِ خرید فرستاده شود. قبلاً تنها راهِ ادامه، دکمهٔ
    # داخلِ همان پیامِ قدیمیِ تایید بود؛ اگر کاربر آن پیام را گم می‌کرد یا
    # ارسالِ توکنش شکست می‌خورد (مثلاً توکن تکراری بود)، این‌جا فقط پلن‌هایِ
    # خرید را می‌دید و فکر می‌کرد پولش سوخته و باید دوباره پول بدهد.
    from models import PartnerCardOrder
    async with Session() as s:
        paid = (await s.scalars(
            select(PartnerCardOrder)
            .where(PartnerCardOrder.partner_bot_id == pb.id,
                   PartnerCardOrder.buyer_telegram_id == cb.from_user.id,
                   PartnerCardOrder.status == "approved",
                   PartnerCardOrder.sold_bot_id.is_(None))
            .order_by(PartnerCardOrder.id.desc()))).all()
    if paid:
        rb = InlineKeyboardBuilder()
        for o in paid:
            rb.button(text=f"✅ ادامهٔ ساخت — {_PLAN_TITLE.get(o.plan, o.plan)} (سفارش #{o.id})",
                      callback_data=f"ps:mk:{o.id}", style=ButtonStyle.SUCCESS)
        rb.button(text="🔙 بازگشت به خانه", callback_data="ps:back",
                  style=ButtonStyle.DANGER)
        rb.adjust(1)
        await cb.message.edit_text(
            "💰 <b>شما یک سفارشِ پرداخت‌شده دارید</b>\n\n"
            "پولت سرِ جایش است و لازم نیست دوباره پرداخت کنی — فقط توکنِ "
            "رباتت را بفرست تا ربات ساخته شود.\n\n"
            "اگر قبلاً پیامِ «این توکن قبلاً ثبت شده» گرفتی، یعنی همان ربات "
            "روی یک ثبتِ فعالِ دیگر است؛ اول از «🤖 ربات‌های من» حذفش کن یا "
            "از BotFather یک رباتِ تازه بساز، بعد دکمهٔ زیر را بزن.",
            reply_markup=rb.as_markup())
        await cb.answer()
        return

    # === PATCH_PARTNER_SALE_FIELDS_V1 === فقط نوع‌هایی که مالکِ همکار فعال کرده
    from tenant.partner_bot.partner_build import allowed_fields
    fb = InlineKeyboardBuilder()
    for key, label in await allowed_fields(row.id):
        fb.button(text=label, callback_data=f"ps:field:{key}",
                  style=ButtonStyle.SUCCESS)
    fb.button(text="🔙 بازگشت به خانه", callback_data="ps:back",
              style=ButtonStyle.DANGER)
    fb.adjust(1)
    await cb.message.edit_text(
        "🤖 <b>ربات تو قرار است چه‌کار کند؟</b>\n\n"
        "بر اساسِ انتخابت، امکانات و دکمه‌های مناسب همان کار برایت "
        "آماده می‌شود. بعداً هم می‌توانی عوضش کنی.",
        reply_markup=fb.as_markup())
    await cb.answer()


@partner_sales_router.callback_query(F.data.startswith("ps:field:"))
async def ps_field_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok

    # === PATCH_PARTNER_SALE_FIELDS_V1 ===
    from tenant.partner_bot.partner_build import field_allowed
    field = cb.data.split(":", 2)[2]
    if not await field_allowed(row.id, field):
        await cb.answer("این نوع ربات فعلاً ارائه نمی‌شود", show_alert=True)
        return

    if state is not None:
        await state.update_data(ps_field=field, ps_pb=pb.id, ps_bot=row.id)
    await _ps_show_plans(cb, row, pb)


@partner_sales_router.callback_query(F.data.startswith("ps:pick:"))
async def ps_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    kind = cb.data.split(":")[2]
    uid = cb.from_user.id

    # === PATCH_PARTNER_TRIAL_PLAN_PICK_V1_MARKER ===
    if kind == "trial":
        if not pb.trial_enabled:
            await cb.answer("تست فعلاً غیرفعال است.", show_alert=True)
            return
        # === PATCH_TRIAL_NO_RENEW_V1 ===
        # محدودیتِ *سراسری*: هر کاربر فقط یک تست در کلِ بات‌ساز.
        # قبلاً فقط همین رباتِ همکار بررسی می‌شد، پس کاربر می‌توانست از
        # هر همکارِ دیگری یک تستِ تازه بگیرد و عملاً همیشه رایگان بماند.
        # هم تست‌هایِ همکاری شمرده می‌شوند هم تست‌هایِ بات‌سازِ اصلی.
        async with Session() as s:
            used = await s.scalar(
                select(func.count(PartnerSale.id)).where(
                    PartnerSale.buyer_telegram_id == uid,
                    PartnerSale.kind == "trial")) or 0
            if not used:
                try:
                    from plan_trials import PlanTrialRequest
                    used = await s.scalar(
                        select(func.count(PlanTrialRequest.id)).where(
                            PlanTrialRequest.user_telegram_id == uid,
                            PlanTrialRequest.status.in_(
                                ("approved", "used", "expired")))) or 0
                except Exception:
                    logger.exception("بررسیِ تستِ بات‌سازِ اصلی ناموفق بود")
        if used:
            await cb.answer(
                "شما قبلاً از تست رایگان استفاده کرده‌اید. "
                "تست فقط یک‌بار برای هر کاربر است.", show_alert=True)
            return

        # === PATCH_PARTNER_TRIAL_PLAN_PICK_V1 ===
        # به‌جای ثابت‌بودنِ پلن رشد، از کاربر می‌پرسیم کدام پلن را
        # می‌خواهد تست کند — دقیقاً مثل تستِ پلن در ربات اصلی بات‌ساز.
        # انتخاب نهایی در ps:trialplan: انجام و ps_plan همان‌جا ست
        # می‌شود؛ چون امکاناتِ ربات از روی BotRow.plan اعمال می‌شود،
        # همین یک تغییر کافی است تا امکاناتِ رشد/حرفه‌ای موقعِ انتخاب
        # درست اعمال شوند.
        await state.update_data(ps_pb=pb.id, ps_bot=row.id)
        tb = InlineKeyboardBuilder()
        tb.button(text="🚀 رشد", callback_data="ps:trialplan:growth",
                  style=ButtonStyle.SUCCESS)
        tb.button(text="💎 حرفه‌ای", callback_data="ps:trialplan:pro",
                  style=ButtonStyle.SUCCESS)
        tb.button(text="🔙 بازگشت", callback_data="ps:menu",
                  style=ButtonStyle.DANGER)
        tb.adjust(1)
        await cb.message.edit_text(
            f"🎁 <b>تست {PARTNER_TRIAL_DAYS} روزه</b>\n\n"
            "کدوم پلن رو می‌خوای تست کنی؟\n"
            "🚀 <b>رشد</b> — امکانات متوسط، مناسب شروع کار\n"
            "💎 <b>حرفه‌ای</b> — همهٔ امکانات، بدون محدودیت",
            reply_markup=tb.as_markup())
        await cb.answer()
        return

    if kind not in ("growth", "pro"):
        await cb.answer()
        return

    stock = pb.stock_growth if kind == "growth" else pb.stock_pro
    if stock <= 0:
        await cb.answer("موجودی این پلن تمام شده است.", show_alert=True)
        return

    price = await sale_price(row.id, kind, uid)  # PATCH_PARTNER_TOP_CUSTOMERS_V1
    if price <= 0:
        await cb.answer("قیمت این پلن هنوز تنظیم نشده است.", show_alert=True)
        return

    async with Session() as s:
        bu = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == uid))
        bal = int(bu.balance) if bu else 0

    # === PATCH_PARTNER_V20 ===
    # به‌جایِ اجبارِ کیف پول، انتخابِ روشِ پرداخت. کارت‌به‌کارت فقط وقتی
    # نشان داده می‌شود که همکار شمارهٔ کارت ثبت کرده و فعالش گذاشته باشد.
    from tenant import get_setting

    card = (await get_setting(row.id, "card_number", "") or "").strip()
    card_on = await get_setting(row.id, "card_enabled", "1") == "1"
    card_ok = bool(card) and card_on

    await state.update_data(ps_plan=kind, ps_kind="sold", ps_price=price,
                            ps_pb=pb.id, ps_bot=row.id)

    # === PATCH_WALLET_TOPUP_ROWS_V1 ===
    # فقط روش‌هایِ شارژی که واقعاً فعال‌اند پیشنهاد می‌شوند؛ قبلاً دکمهٔ
    # ثابتِ «🔋 شارژ کیف پول» همیشه به کارت‌به‌کارت می‌رفت و اگر خاموش بود
    # کاربر پیامِ «کارت به کارت غیرفعال است» می‌گرفت و بن‌بست می‌شد.
    from tenant import wallet_topup_rows
    # === PATCH_WALLET_TOPUP_SKIP_CARD_V1 ===
    # همان اصلاحی که در vpn_user.py انجام شد: اگر «کارت به کارتِ» مستقیم
    # همین پایین‌تر نشان داده می‌شود (card_ok)، دیگر نباید «شارژ
    # کارت‌به‌کارت» را هم جداگانه پیشنهاد داد — گیج‌کننده و تکراری است.
    topup_rows = await wallet_topup_rows(row.id, skip_card=card_ok) if bal < price else []

    b = InlineKeyboardBuilder()
    if bal >= price:
        b.button(text=f"👛 پرداخت از کیف پول ({bal:,})",
                 callback_data="ps:pay:wallet", style=ButtonStyle.SUCCESS)
    if card_ok:
        b.button(text="💳 کارت به کارت", callback_data="ps:pay:card",
                 style=ButtonStyle.SUCCESS)
    for _row in topup_rows:
        for _btn in _row:
            b.button(text=_btn.text, callback_data=_btn.callback_data,
                     style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="ps:menu",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    if bal < price:
        head = (f"❌ <b>موجودی کافی نیست</b>\n\n"
                f"قیمت: {price:,} تومان\n"
                f"موجودی شما: {bal:,} تومان\n\n"
                + ("اول کیف پولت را شارژ کن، بعد دوباره همین پلن را بزن."
                   if topup_rows else
                   "برای شارژِ کیف پول با پشتیبانی تماس بگیر."))
    else:
        head = (f"🧾 <b>{_PLAN_TITLE[kind]}</b> — {price:,} تومان\n\n"
                f"👛 موجودی شما: {bal:,} تومان\n\n"
                "روش پرداخت را انتخاب کن:")

    await cb.message.edit_text(head, reply_markup=b.as_markup())
    await cb.answer()
    return


# ────────── انتخابِ پلنِ تست (رشد/حرفه‌ای) ──────────

@partner_sales_router.callback_query(F.data.startswith("ps:trialplan:"))
async def ps_trialplan_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """کاربر پلنِ موردِنظر برای تست را انتخاب کرد — حالا مرحلهٔ توکن."""
    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    if not pb.trial_enabled:
        await cb.answer("تست فعلاً غیرفعال است.", show_alert=True)
        return

    plan = cb.data.split(":")[2]
    if plan not in ("growth", "pro"):
        await cb.answer()
        return

    data = await state.get_data()
    if not data.get("ps_pb"):
        await cb.answer("این مرحله منقضی شده. دوباره از منو شروع کن.",
                         show_alert=True)
        return

    await state.update_data(ps_plan=plan, ps_kind="trial", ps_price=0)
    await state.set_state(PartnerSaleFSM.token)
    bk = InlineKeyboardBuilder()
    bk.button(text="🔙 بازگشت", callback_data="ps:menu",
              style=ButtonStyle.DANGER)
    bk.adjust(1)
    await cb.message.edit_text(
        f"🎁 <b>تست {PARTNER_TRIAL_DAYS} روزه — {_PLAN_TITLE[plan]}</b>\n\n"
        + "<b>ساخت ربات در ۴ قدم:</b>\n"
        "۱. وارد <code>@BotFather</code> شو\n"
        "۲. دستور <code>/newbot</code> را بفرست\n"
        "۳. یک نام و یک آیدی (که به <code>bot</code> ختم شود) بده\n"
        "۴. توکنی که می‌دهد را <b>همین‌جا</b> بفرست\n\n"
        "⚠️ توکن مثل رمز عبور است — به کسی نده."
        + f"\n\n⏳ این ربات بعد از {PARTNER_TRIAL_DAYS} روز غیرفعال می‌شود.",
        reply_markup=bk.as_markup())
    await cb.answer()


@partner_sales_router.callback_query(F.data == "ps:pay:wallet")
async def ps_pay_wallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پرداخت از کیف پول → مستقیم به مرحلهٔ توکن."""
    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    data = await state.get_data()
    kind = data.get("ps_plan")
    price = int(data.get("ps_price") or 0)
    if not kind:
        await cb.answer("این مرحله منقضی شده", show_alert=True)
        return

    # === PATCH_PARTNER_PAY_BEFORE_TOKEN_V1 ===
    # قبلاً پول *در لحظهٔ ارسالِ توکن* کسر می‌شد و این‌جا فقط یک پیام
    # نشان داده می‌شد. مشکلش این بود که اگر ارسالِ توکن به هر دلیلی
    # شکست می‌خورد (توکنِ تکراری، بستنِ ربات، رفتنِ کاربر)، هیچ ردی از
    # «این آدم پلن را انتخاب کرده» نمی‌ماند و کاربر برمی‌گشت به صفحهٔ
    # خرید — دقیقاً همان چیزی که مشتری‌ها را نگران می‌کرد.
    # حالا مثلِ مسیرِ کارت‌به‌کارت: اول پرداخت، بعد توکن. پرداخت یک
    # سفارشِ «تاییدشدهٔ مصرف‌نشده» می‌سازد که هر زمان قابلِ ادامه است.
    from models import PartnerCardOrder

    async with Session() as s:
        bu = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id,
            BotUser.telegram_id == cb.from_user.id).with_for_update())
        bal = int(bu.balance) if bu else 0
        if bal < price:
            await cb.answer("موجودی کیف پولت کافی نیست.", show_alert=True)
            return

        # موجودیِ انبار را همین‌جا هم چک کن — نباید پولی بگیریم که
        # جنسش موجود نیست.
        pb_now = await s.get(PartnerBot, pb.id, with_for_update=True)  # موجودیِ انبار: دو خریدِ هم‌زمان آخرین عدد را نبرند
        stock = (pb_now.stock_growth if kind == "growth" else pb_now.stock_pro) if pb_now else 0
        if not pb_now or pb_now.status != "active" or stock <= 0:
            await cb.answer("متأسفانه موجودی این پلن تمام شد.", show_alert=True)
            return

        bu.balance = float(bu.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=bu.id, amount=-price,
                        kind="purchase",
                        note=f"خرید ربات {kind} (پرداخت پیش از ارسال توکن)"))
        order = PartnerCardOrder(
            partner_bot_id=pb.id, buyer_telegram_id=cb.from_user.id,
            plan=kind, price=price, receipt_file_id=None,
            status="approved", decided_at=datetime.datetime.now())
        s.add(order)
        await s.commit()
        order_id = order.id
        new_bal = int(bu.balance)

    # ps_paid_by="card" یعنی «پول قبلاً گرفته شده، در مرحلهٔ توکن دوباره
    # کسر نکن» — همان پرچمی که مسیرِ کارت‌به‌کارت استفاده می‌کند.
    await state.update_data(ps_paid_by="card", ps_order=order_id)
    await state.set_state(PartnerSaleFSM.token)
    # === PATCH_PARTNER_V7 ===
    bk = InlineKeyboardBuilder()
    bk.button(text="🔙 بازگشت", callback_data="ps:menu",
              style=ButtonStyle.DANGER)
    bk.adjust(1)
    await cb.message.edit_text(
        f"✅ <b>پرداخت انجام شد</b> — {_PLAN_TITLE[kind]}\n"
        f"💰 {price:,} تومان از کیف پولت کسر شد "
        f"(موجودی: {new_bal:,} تومان)\n\n"
        + "<b>ساخت ربات در ۴ قدم:</b>\n"
        "۱. وارد <code>@BotFather</code> شو\n"
        "۲. دستور <code>/newbot</code> را بفرست\n"
        "۳. یک نام و یک آیدی (که به <code>bot</code> ختم شود) بده\n"
        "۴. توکنی که می‌دهد را <b>همین‌جا</b> بفرست\n\n"
        "⚠️ توکن مثل رمز عبور است — به کسی نده.\n\n"
        "ℹ️ اگر الان توکن نداری اشکالی ندارد؛ پرداختت ثبت شده و هر وقت "
        "برگردی از «🤖 ساخت ربات» می‌توانی ادامه بدهی.",
        reply_markup=bk.as_markup())
    await cb.answer()


# ────────── دریافتِ توکن و ساختِ ربات ──────────

@partner_sales_router.message(PartnerSaleFSM.token)
async def ps_token(msg: Message, bot: Bot, state: FSMContext):
    from bot_utils import _temp_bot

    token = (msg.text or "").strip()
    if ":" not in token or len(token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return

    data = await state.get_data()
    plan = data.get("ps_plan")
    kind = data.get("ps_kind")
    price = int(data.get("ps_price") or 0)
    pb_id = data.get("ps_pb")
    seller_bot_id = data.get("ps_bot")
    paid_by = data.get("ps_paid_by") or "wallet"   # === PATCH_PARTNER_V20 ===
    card_order = data.get("ps_order")
    if not (plan and pb_id):
        await state.clear()
        await msg.answer("این مرحله منقضی شده. دوباره از منو شروع کن.",
                         reply_markup=_back_kb())
        return

    try:
        async with _temp_bot(token) as tmp:
            me = await tmp.get_me()
    except Exception:
        await msg.answer("نتونستم با این توکن وصل بشم. درست کپی شده؟")
        return

    uid = msg.from_user.id
    days = PARTNER_TRIAL_DAYS if kind == "trial" else SOLD_BOT_DAYS

    async with Session() as s:
        from bot_utils import reclaim_token_if_archived
        _freed, dup = await reclaim_token_if_archived(s, token, uid)  # PATCH_TOKEN_RECLAIM_V1
        if dup:
            await msg.answer(
                "این توکن همین حالا رویِ یک رباتِ فعال ثبت است.\n\n"
                "اگر این ربات مالِ خودته، اول از «🤖 ربات‌های من» حذفش کن، "
                "بعد همین توکن را دوباره بفرست. وگرنه از BotFather یک "
                "رباتِ تازه بساز و توکنِ آن را بفرست.")
            return

        pb = await s.get(PartnerBot, pb_id, with_for_update=True)  # موجودیِ انبار: دو خریدِ هم‌زمان آخرین عدد را نبرند
        if not pb or pb.status != "active":
            await state.clear()
            await msg.answer("این ربات همکار در حال حاضر فعال نیست.",
                             reply_markup=_back_kb())
            return

        # بازبینیِ دوبارهٔ موجودیِ انبار و کیفِ پول در همین تراکنش —
        # بینِ انتخابِ پلن و ارسالِ توکن ممکن است هر دو عوض شده باشند.
        if kind == "sold":
            stock = pb.stock_growth if plan == "growth" else pb.stock_pro
            if stock <= 0:
                await state.clear()
                # === PATCH_PARTNER_PAY_BEFORE_TOKEN_V1 ===
                # حالا که پول *قبل* از این مرحله گرفته می‌شود، این پیام
                # نباید کاربر را نگران کند: سفارشش مصرف‌نشده باقی می‌ماند
                # و هر وقت موجودی برگردد از همان «🤖 ساخت ربات» ادامه
                # می‌دهد.
                await msg.answer(
                    "متأسفانه موجودی این پلن همین الان تمام شد.\n\n"
                    + ("پرداختت محفوظ است — هر وقت موجودی برگردد از "
                       "«🤖 ساخت ربات» ادامه بده، یا برای عودت وجه به "
                       "پشتیبانی همین ربات پیام بده."
                       if paid_by == "card" else
                       "هنوز مبلغی از تو کسر نشده."),
                    reply_markup=_back_kb())
                return

            # === PATCH_PARTNER_V20 ===
            # اگر با کارت‌به‌کارت پرداخت شده، پول قبلاً بیرون از کیف پول
            # داده شده و همکار رسیدش را تایید کرده — دوباره کسر نکن.
            if paid_by != "card":
                bu = await s.scalar(select(BotUser).where(
                    BotUser.bot_id == seller_bot_id,
                    BotUser.telegram_id == uid).with_for_update())
                if not bu or float(bu.balance) < price:
                    await state.clear()
                    await msg.answer("موجودی کیف پولت کافی نیست.",
                                     reply_markup=_back_kb())
                    return
                bu.balance = float(bu.balance) - price
                s.add(WalletTxn(bot_id=seller_bot_id, user_id=bu.id,
                                amount=-price, kind="purchase",
                                note=f"خرید ربات {plan} از @{me.username}"))

            if plan == "growth":
                pb.stock_growth -= 1
            else:
                pb.stock_pro -= 1
            # === PATCH_PARTNER_STOCK_LOG_V1 ===
            await log_stock(
                s, pb.id, plan, -1, "sale",
                f"فروش به کاربر {uid}",
                pb.stock_growth if plan == "growth" else pb.stock_pro)

        expiry = datetime.datetime.now() + datetime.timedelta(days=days)
        sold = BotRow(owner_id=uid, token=token, username=me.username,
                      # === PATCH_PARTNER_V26 ===
                      # زمینه عمداً خالی می‌ماند تا کاربر خودش در
                      # ویزارد انتخابش کند. قبلاً "vpn_sales" ثابت بود و
                      # هرچه کاربر انتخاب می‌کرد، فهرستِ عملکردها باز هم
                      # VPN نشان می‌داد.
                      # === PATCH_PARTNER_FIELD_BEFORE_PLAN_V1 ===
                      # قبلاً همیشه خالی بود؛ الان کاربر پیش از خرید در
                      # ps:field: انتخاب کرده، پس همان‌جا اعمال می‌شود.
                      field=(data.get("ps_field") or ""), plan=plan, status="active",
                      plan_expires_at=expiry)
        s.add(sold)
        await s.flush()

        s.add(Setting(bot_id=sold.id, key="card_number", value=""))
        s.add(Setting(bot_id=sold.id, key="volume_options", value="10,30,50,100"))
        s.add(Setting(bot_id=sold.id, key="day_options", value="30,60,90"))
        s.add(Setting(bot_id=sold.id, key="count_options", value="1"))

        s.add(PartnerSale(partner_bot_id=pb_id, sold_bot_id=sold.id,
                          buyer_telegram_id=uid, plan=plan,
                          price=price, kind=kind))
        # === PATCH_PARTNER_V20 ===
        if card_order:
            from models import PartnerCardOrder
            co = await s.get(PartnerCardOrder, card_order)
            if co:
                co.sold_bot_id = sold.id
        await s.commit()
        sold_id = sold.id
        owner_id = pb.owner_id

    invalidate_pb_cache(seller_bot_id)   # === PATCH_PARTNER_V27 ===
    await refresh_low_stock_timer(pb_id)

    hook_ok = await set_tenant_webhook(token)
    hook_note = "" if hook_ok else (
        "\n\n⚠️ اتصال ربات برقرار نشد. به پشتیبانی همین ربات پیام بده.")

    await state.clear()
    label = "🎁 تست" if kind == "trial" else _PLAN_TITLE[plan]

    # === PATCH_PARTNER_V19 ===
    # ویزاردِ چیدمان داخلِ *همین* ربات باز می‌شود — کاربر لازم نیست
    # جایی برود و اصلاً نمی‌فهمد بات‌سازی پشتِ ماجراست.
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎨 چیدن دکمه‌های ربات",
                             callback_data=f"pb:start:{sold_id}",
                             style=ButtonStyle.SUCCESS)]])

    await msg.answer(
        f"✅ ربات @{me.username} ساخته شد!\n\n"
        f"پلن: {label}\n"
        f"⏳ اعتبار تا: {jalali(expiry)}\n"
        f"👑 مالک ربات: شما\n\n"
        "🎨 حالا دکمه‌های صفحهٔ اصلی رباتت را بچین — روی دکمهٔ زیر بزن."
        + hook_note,
        reply_markup=kb)

    # اطلاع به صاحبِ رباتِ همکار
    try:
        await bot.send_message(
            owner_id,
            ("🎁 <b>یک ربات تست داده شد</b>" if kind == "trial"
             else "💰 <b>یک ربات فروخته شد</b>") + "\n\n"
            f"👤 خریدار: <code>{uid}</code>\n"
            f"🤖 ربات: @{me.username}\n"
            f"💼 پلن: {plan}\n"
            + (f"💵 مبلغ: {price:,} تومان" if price else "🎁 رایگان (تست)"))
    except Exception:
        logger.exception("اطلاعِ فروش به صاحبِ رباتِ همکار ناموفق بود")

    # === PATCH_PARTNER_TRUST_CHANNEL_V1 ===
    # فروشِ ربات در رباتِ همکار هیچ‌وقت _purchase_alert را صدا نمی‌زد، پس
    # نه «اطلاع خودکار خرید» به ادمین می‌رفت و نه چیزی در «کانال
    # اعتمادسازی» پست می‌شد — دقیقاً همان اشکالی که قبلاً ربات‌هایِ
    # virtual_service (tenant/smm.py) داشتند. حالا مثل بقیهٔ مسیرهایِ خرید
    # از همان نقطهٔ مشترک رد می‌شود.
    # تستِ رایگان عمداً پست نمی‌شود: کانالِ اعتمادسازی برایِ نشان‌دادنِ
    # فروشِ واقعی است و پرکردنش با تست‌هایِ رایگان دقیقاً همان اعتمادی را
    # که قرار بود بسازد از بین می‌برد.
    if kind != "trial":
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                seller_row = await s.get(BotRow, seller_bot_id)
                buyer = await s.scalar(
                    select(BotUser).where(BotUser.bot_id == seller_bot_id,
                                          BotUser.telegram_id == uid))
            if seller_row:
                await _purchase_alert(
                    bot, seller_row, buyer=buyer,
                    section="فروش ربات (همکاری)",
                    detail=f"ربات آماده — پلن {label}",
                    amount=price, count=1)
        except Exception:
            # هیچ‌وقت نباید جلویِ تکمیلِ فروش را بگیرد
            logger.exception("گزارشِ خرید/کانالِ اعتمادسازیِ فروشِ همکار ناموفق بود")

    # اگر با این فروش زیرِ حدِ نصاب ماند، به همکار هشدار بده
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        low_since = pb.low_stock_since if pb else None
    if low_since:
        deadline = low_since + datetime.timedelta(days=LOW_STOCK_GRACE_DAYS)
        left = max(0, (deadline - datetime.datetime.now()).days)
        try:
            await bot.send_message(
                owner_id,
                f"⚠️ ربات‌های فعال شما هنوز کمتر از {await min_active_sales()} تاست. "
                f"<b>{left} روز</b> تا مهلت باقی مانده.")
        except Exception:
            logger.exception("هشدارِ حدِ نصاب ارسال نشد")


# === PATCH_PARTNER_V6 ===
@partner_sales_router.callback_query(F.data == "ps:back")
async def ps_back_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازگشت از منویِ خریدِ ربات به صفحهٔ اصلی.

    پیامِ اینلاین حذف و کیبوردِ اصلیِ ربات دوباره فرستاده می‌شود — چون
    صفحهٔ اصلیِ ربات مستأجر کیبوردِ ریپلای است، نه اینلاین، و نمی‌شود
    با edit_text به آن برگشت.
    """
    await state.clear()
    from tenant import get_bot_row, main_kb_or_remove

    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("حذفِ پیامِ منویِ خرید ناموفق بود")
    kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
    await cb.message.answer("🏠 به صفحهٔ اصلی برگشتی.", reply_markup=kb)
    await cb.answer()


# === PATCH_PARTNER_V19 ===
@partner_sales_router.callback_query(F.data.startswith("pb:start:"))
async def ps_open_builder(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازکردنِ ویزاردِ چیدمان برایِ رباتی که همین الان خریده شد."""
    from tenant.partner_bot.partner_build import start_build

    bot_id = int(cb.data.split(":")[2])
    await cb.answer()
    await start_build(bot_id, cb.message.model_copy(
        update={"from_user": cb.from_user}), state, bot)


# ======================= کارت‌به‌کارتِ خریدِ ربات =======================
# === PATCH_PARTNER_V20 ===
# مسیرِ دوم برایِ خریدِ ربات: کاربر رسید می‌فرستد، همکار تایید می‌کند،
# و *تازه آن‌وقت* کاربر توکن می‌فرستد و ربات ساخته می‌شود.
#
# ترتیب عمدی است: تا رسید تایید نشده هیچ‌چیز از انبارِ همکار کم نمی‌شود
# و هیچ رباتی ساخته نمی‌شود.

@partner_sales_router.callback_query(F.data == "ps:pay:card")
async def ps_pay_card(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_setting

    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    data = await state.get_data()
    kind = data.get("ps_plan")
    price = int(data.get("ps_price") or 0)
    if not kind or price <= 0:
        await cb.answer("این مرحله منقضی شده", show_alert=True)
        return

    card = (await get_setting(row.id, "card_number", "") or "").strip()
    holder = (await get_setting(row.id, "card_holder", "") or "").strip()
    if not card or await get_setting(row.id, "card_enabled", "1") != "1":
        await cb.answer("کارت‌به‌کارت در حال حاضر فعال نیست.", show_alert=True)
        return

    await state.set_state(PartnerSaleFSM.receipt)
    # === PATCH_PARTNER_V21 === بدونِ این دکمه کاربر در صفحه گیر می‌کرد.
    bk = InlineKeyboardBuilder()
    bk.button(text="🔙 انصراف و بازگشت", callback_data="ps:menu",
              style=ButtonStyle.DANGER)
    bk.adjust(1)
    await cb.message.edit_text(
        f"💳 <b>کارت به کارت</b>\n\n"
        f"💼 پلن: {_PLAN_TITLE[kind]}\n"
        f"💰 مبلغ: <b>{price:,} تومان</b>\n\n"
        f"شماره کارت:\n<code>{card}</code>\n"
        + (f"🧑 به نام: {holder}\n" if holder else "")
        + "\n<b>مراحل:</b>\n"
        "۱. مبلغ بالا را کارت‌به‌کارت کن\n"
        "۲. <b>عکس رسید</b> را همین‌جا بفرست\n"
        "۳. بعد از تایید فروشنده، توکن رباتت را می‌فرستی\n\n"
        "⚠️ رسید باید واضح و خوانا باشد.",
        reply_markup=bk.as_markup())
    await cb.answer()


@partner_sales_router.message(PartnerSaleFSM.receipt, F.photo)
async def ps_receipt(msg: Message, bot: Bot, state: FSMContext):
    from models import PartnerCardOrder

    ok = await _partner_of(bot)
    if not ok:
        return
    row, pb = ok
    data = await state.get_data()
    kind = data.get("ps_plan")
    price = int(data.get("ps_price") or 0)
    await state.clear()
    if not kind:
        await msg.answer("این مرحله منقضی شده. دوباره از منو شروع کن.",
                         reply_markup=_back_kb())
        return

    file_id = msg.photo[-1].file_id
    async with Session() as s:
        o = PartnerCardOrder(
            partner_bot_id=pb.id, buyer_telegram_id=msg.from_user.id,
            plan=kind, price=price, receipt_file_id=file_id, status="pending")
        s.add(o)
        await s.commit()
        order_id = o.id
        owner = pb.owner_id

    await msg.answer(
        f"✅ رسید دریافت شد (سفارش #{order_id}).\n\n"
        "به‌محض تایید، پیام می‌گیری و می‌توانی توکن رباتت را بفرستی.")

    # اطلاع به همکار — با همان ربات، پس file_id معتبر است.
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data=f"psord:{order_id}:y",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"psord:{order_id}:n",
                             style=ButtonStyle.DANGER),
    ]])
    try:
        await bot.send_photo(
            owner, file_id,
            caption=(f"💳 <b>رسید خرید ربات</b>\n\n"
                     f"👤 خریدار: <code>{msg.from_user.id}</code>\n"
                     f"💼 پلن: {_PLAN_TITLE[kind]}\n"
                     f"💰 مبلغ: {price:,} تومان\n"
                     f"🧾 سفارش #{order_id}"),
            reply_markup=kb)
    except Exception:
        logger.exception("ارسالِ رسیدِ خریدِ ربات به همکار ناموفق بود")
        await msg.answer(
            "⚠️ سفارشت ثبت شد ولی اطلاع‌رسانی به فروشنده ناموفق بود. "
            f"شمارهٔ سفارش (#{order_id}) را از بخش پشتیبانی پیگیری کن.",
            reply_markup=_back_kb())


@partner_sales_router.message(PartnerSaleFSM.receipt)
async def ps_receipt_wrong(msg: Message):
    await msg.answer("لطفاً <b>عکس</b> رسید را بفرست (نه متن).")


@partner_sales_router.callback_query(F.data.startswith("psord:"))
async def ps_order_decide(cb: CallbackQuery, bot: Bot):
    """تاییدِ رسید توسطِ صاحبِ رباتِ همکار."""
    from models import PartnerCardOrder

    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    if cb.from_user.id != row.owner_id:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    _, raw_id, verdict = cb.data.split(":")
    order_id = int(raw_id)

    async with Session() as s:
        o = await s.get(PartnerCardOrder, order_id)
        if not o or o.partner_bot_id != pb.id:
            await cb.answer("سفارش پیدا نشد", show_alert=True)
            return
        if o.status != "pending":
            await cb.answer("این سفارش قبلاً تعیین تکلیف شده", show_alert=True)
            return

        # موجودیِ انبار را همین‌جا دوباره چک کن: از زمانِ ثبتِ سفارش تا
        # حالا ممکن است تمام شده باشد.
        if verdict == "y":
            stock = pb.stock_growth if o.plan == "growth" else pb.stock_pro
            if stock <= 0:
                await cb.answer(
                    "موجودی این پلن تمام شده — نمی‌توانی تایید کنی.",
                    show_alert=True)
                return

        o.status = "approved" if verdict == "y" else "rejected"
        o.decided_at = datetime.datetime.now()
        buyer, plan, price = o.buyer_telegram_id, o.plan, int(o.price or 0)
        await s.commit()

    if verdict == "y":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🤖 ارسال توکن و ساخت ربات",
                                 callback_data=f"ps:mk:{order_id}",
                                 style=ButtonStyle.SUCCESS)]])
        user_txt = (
            f"✅ <b>پرداخت شما تایید شد</b> (سفارش #{order_id})\n\n"
            f"💼 پلن: {_PLAN_TITLE.get(plan, plan)}\n\n"
            "حالا روی دکمهٔ زیر بزن و توکن رباتت را بفرست.")
        admin_txt = f"✅ سفارش #{order_id} تایید شد."
    else:
        kb = None
        user_txt = (f"❌ <b>رسید شما تایید نشد</b> (سفارش #{order_id})\n\n"
                    "برای پیگیری با پشتیبانی همین ربات تماس بگیر.")
        admin_txt = f"❌ سفارش #{order_id} رد شد."

    try:
        await bot.send_message(buyer, user_txt, reply_markup=kb)
    except Exception:
        logger.exception("اطلاعِ نتیجهٔ سفارش به خریدار ناموفق بود")

    try:
        await cb.message.edit_caption(caption=admin_txt)
    except Exception:
        try:
            await cb.message.edit_text(admin_txt)
        except Exception:
            logger.exception("ویرایشِ پیامِ سفارش ناموفق بود")
    await cb.answer()


@partner_sales_router.callback_query(F.data.startswith("ps:mk:"))
async def ps_make_from_order(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بعد از تاییدِ رسید: کاربر توکن می‌فرستد."""
    from models import PartnerCardOrder

    ok = await _partner_of(bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    order_id = int(cb.data.split(":")[2])

    async with Session() as s:
        o = await s.get(PartnerCardOrder, order_id)
        if not o or o.buyer_telegram_id != cb.from_user.id:
            await cb.answer("سفارش پیدا نشد", show_alert=True)
            return
        if o.status != "approved":
            await cb.answer("این سفارش تایید نشده است.", show_alert=True)
            return
        if o.sold_bot_id:
            await cb.answer("برای این سفارش قبلاً ربات ساخته شده.",
                            show_alert=True)
            return
        plan, price = o.plan, int(o.price or 0)

    # ps_paid_by="card" یعنی در مرحلهٔ ساخت، پول از کیف پول کسر نشود.
    await state.update_data(ps_plan=plan, ps_kind="sold", ps_price=price,
                            ps_pb=pb.id, ps_bot=row.id,
                            ps_paid_by="card", ps_order=order_id)
    await state.set_state(PartnerSaleFSM.token)
    bk = InlineKeyboardBuilder()
    bk.button(text="🔙 بازگشت", callback_data="ps:menu",
              style=ButtonStyle.DANGER)
    bk.adjust(1)
    await cb.message.edit_text(
        f"✅ <b>{_PLAN_TITLE.get(plan, plan)}</b> — پرداخت‌شده ✓\n\n"
        + "<b>ساخت ربات در ۴ قدم:</b>\n"
        "۱. وارد <code>@BotFather</code> شو\n"
        "۲. دستور <code>/newbot</code> را بفرست\n"
        "۳. یک نام و یک آیدی (که به <code>bot</code> ختم شود) بده\n"
        "۴. توکنی که می‌دهد را <b>همین‌جا</b> بفرست\n\n"
        "⚠️ توکن مثل رمز عبور است — به کسی نده.",
        reply_markup=bk.as_markup())
    await cb.answer()
