"""
tenant/partner_menu.py — منویِ پنلِ مدیریتِ «ربات همکار».

=== PATCH_PARTNER_MENU_V1 ===
فلسفهٔ این فایل: تقریباً هیچ‌چیز از نو نوشته نشده. بیشترِ چیزهایی که
برایِ پنلِ همکار خواسته شده بود، از قبل در tenant/__init__.py وجود دارد
و فقط باید با callbackِ درستش صدا زده شود:

    مدیریت کاربران      → tadm:users
    سفارش‌ها            → tadm:orders
    ارسال همگانی        → tadm:broadcast
    آمار ربات           → tadm:stats
    دکمه‌های صفحه اصلی  → tadm:homebtns
    نوع/رنگ دکمه‌ها     → tadm:btn_colors
    ارسال به کانال/گروه → tadm:chpost
    زیرمجموعه‌گیری      → tadm:ref_report
    نفرات برتر          → tc:menu   (tenant/top_customers.py)
    مدیریت تیکت‌ها      → tadm:tickets (tenant/support_ticket.py)

چیزهایی که واقعاً جدیدند و این‌جا نوشته شده‌اند:
    • خانهٔ پنل و زیرمنویِ تنظیمات (مخصوصِ همکار)
    • کلیدِ فعال/غیرفعالِ دکمهٔ تستِ ۲ روزه
    • حالتِ آپدیت (خاموشیِ موقت برایِ کاربران، بازِ باز برایِ مالک)
    • ارجاع به پشتیبانیِ بات‌ساز (پل به رباتِ مادر)

محدودسازی: هندلرهایِ بالا خودشان مالکیت را چک می‌کنند، و پلنِ partner
مجموعهٔ فیچرِ محدودی دارد؛ پس هرچه در آن مجموعه نباشد خودبه‌خود بسته است.
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from config import Session, PLATFORM_BOT_TOKEN, PLATFORM_ADMIN_ID
from models import (
    Bot as BotRow, PartnerBot, PartnerEscalation, PartnerEscalationMsg,
)
from partner_rules import jalali  # === PATCH_PARTNER_V4 ===

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_menu_router = Router()


class PartnerMenuFSM(StatesGroup):
    escalate = State()


async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    # === MARKER_PARTNER_PANEL_SUPPORT_ACCESS_V1 ===
    # قبلاً فقط مالکِ دقیق قبول می‌شد؛ ادمینِ پشتیبانیِ ثبت‌شده در
    # AdminSupport (همون «دسترسیِ پشتیبانی» که از پنلِ پلتفرم فعال
    # می‌شه) هیچ‌وقت رد نمی‌شد — برخلافِ بقیهٔ پنلِ مدیریت که با
    # is_owner_or_admin چک می‌شه.
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    if not await is_owner_or_admin(row.id, cb_or_msg.from_user.id, row.owner_id):
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb:
        return None
    return row, pb


# ────────── خانهٔ پنل ──────────

# === PATCH_PARTNER_V7 ===
async def partner_home_kb(bot_id: int) -> InlineKeyboardMarkup:
    """کیبوردِ پنلِ مدیریتِ ربات همکار.

    از هندلر جدا شد چون tadmin_kb در tenant/__init__.py هم لازمش دارد
    (تا دکمهٔ «🛠 پنل مدیریت» مستقیم همین را باز کند، نه منویِ عمومی).

    === PATCH_PARTNER_V35 ===
    چیدمان دستی است، نه adjust: برچسب‌هایِ بلند («ارسال به کانال و گروه»،
    «دکمه‌های صفحهٔ اصلی»، «نوع منو») در ردیفِ دوتایی بریده می‌شدند، پس
    تک‌ردیفه می‌آیند. کوتاه‌ها جفت می‌شوند تا صفحه فشرده بماند.
    گروه‌بندی: فروش → آمار → محتوا → ظاهر → تنظیمات.
    """
    from tenant import get_setting

    _menu_label = ("💠 شیشه‌ای"
                   if await get_setting(bot_id, "menu_mode", "reply") == "inline"
                   else "⌨️ کیبورد")

    # === PATCH_PARTNER_BTN_COLORS_V1 ===
    # رنگ‌هایی که همکار در «مدیریت رنگ دکمه‌های اینلاین» انتخاب می‌کند
    # باید واقعاً روی همین دکمه‌ها بنشیند؛ قبلاً استایل‌ها ثابت بودند و
    # آن صفحه عملاً هیچ اثری نداشت.
    from tenant import ibtn_style as _ibs
    _ov: dict[str, ButtonStyle | None] = {}
    for _k in ("ibtn:pp_buy", "ibtn:pp_prices", "ibtn:pp_finance",
               "ibtn:pp_stats", "ibtn:pp_top", "ibtn:adm_stats",
               "ibtn:pp_ref", "ibtn:adm_broadcast", "ibtn:pp_homebtns",
               "ibtn:pp_menumode", "ibtn:adm_settings", "ibtn:pp_settings",
               "ibtn:adm_home", "ibtn:adm_polls", "ibtn:adm_refpoints"):
        try:
            _ov[_k] = await _ibs(bot_id, _k)
        except Exception:
            _ov[_k] = None

    def _b(text: str, cb: str, style: ButtonStyle,
           key: str | None = None) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=cb,
            style=(_ov.get(key) or style) if key else style)

    S, P, D = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER

    rows = [
        # ── فروش و مالی ──
        [_b("🛒 خرید ربات جهت فروش", "pp:buy", S, "ibtn:pp_buy")],
        [_b("🏷 قیمت فروش", "pp:prices", S, "ibtn:pp_prices"),
         _b("💰 مالی و کیف پول", "pp:finance", S, "ibtn:pp_finance")],

        # ── آمار و کاربران ──
        [_b("📊 آمار کلی ربات", "pp:stats", P, "ibtn:pp_stats"),
         _b("🏆 نفرات برتر", "tc:menu", P, "ibtn:pp_top")],
        # === PATCH_PARTNER_STATS_MOVE_V1 ===
        # قبلاً دکمهٔ «آمار کلی ربات» (tadm:stats — آمارِ واقعیِ ربات:
        # اعضا، سفارش‌ها، درآمد و...) فقط داخلِ «تنظیمات عمومی ربات» بود.
        # طبقِ درخواست، به پنلِ مدیریت منتقل شد و نامش به «آمار و
        # گزارشات» تغییر کرد تا با «📊 آمار کلی ربات» بالا (که آمارِ
        # همکاری/موجودیِ خودِ همکار است، نه آمارِ ربات) اشتباه نشود.
        [_b("📈 آمار و گزارشات", "tadm:stats", P, "ibtn:adm_stats")],
        # === PATCH_PARTNER_REFERRAL_BTN_FIX_V1 ===
        # قبلاً "pp:referral" بود که هیچ هندلری نداشت (دکمه کار نمی‌کرد)؛
        # طبق نقشهٔ راهِ بالای فایل، مستقیم به هندلرِ آمادهٔ گزارشِ
        # زیرمجموعه‌گیری در tenant/__init__.py وصل می‌شود.
        [_b("👥 زیرمجموعه‌گیری", "tadm:ref_report", P, "ibtn:pp_ref")],
        # === PATCH_PARTNER_REFPOINTS_BTN_V1 ===
        # درخواستِ همکار: پیامِ ربات می‌گفت «از دکمه‌ها و تنظیمات روشنش
        # کن» ولی این پنل (partner_home_kb) اصلاً همچین دکمه‌ای نداره —
        # چیدمانش کاملاً دستی و مسطح است، نه یک زیرمنویِ گروه‌بندی‌شده.
        # مثلِ نظرسنجی (بالا)، این هم مستقیم این‌جا اضافه شد؛ هندلرِ
        # rpt:menu از قبل با owner_id چک می‌کند، نه با پلن.
        [_b("🏅 امتیازگیری با رفرال", "rpt:menu", P, "ibtn:adm_refpoints")],

        # ── محتوا ──
        [_b("📢 ارسال به کانال و گروه", "tadm:chpost", P, "ibtn:adm_broadcast")],
        # === PATCH_PARTNER_POLLS_BTN_V1 ===
        # درخواستِ همکار: قابلیتِ «ارسالِ گروهیِ نظرسنجی» که در پنلِ ادمینِ
        # ربات‌هایِ عادی هست (tadm:polls در tenant/extras.py — ساختِ
        # نظرسنجی و ارسالش به گروهِ متصل)، تویِ پنلِ ربات همکار هم دیده
        # نمی‌شد. خودِ هندلرِ tadm:polls از قبل با owner_id چک می‌کند (نه
        # با پلن)، پس همکار (که owner همینِ رباته) از قبل مجاز بود؛ فقط
        # دکمه‌اش این‌جا نبود.
        [_b("📊 نظرسنجی", "tadm:polls", P, "ibtn:adm_polls")],

        # ── ظاهر ──
        [_b("🏠 دکمه‌های صفحهٔ اصلی", "tadm:homebtns", P, "ibtn:pp_homebtns")],
        [_b("🎨 مدیریت رنگ دکمه‌های اینلاین", "tadm:btn_colors", P)],
        [_b(f"🎛 نوع منو: {_menu_label}", "pp:menumode", P, "ibtn:pp_menumode")],
        # === PATCH_PARTNER_V36 ===
        # هابِ تنظیماتِ عمومیِ ربات (پیامِ خوش‌آمد، معرفیِ پشتیبانی،
        # عضویتِ اجباری، آنتی‌اسپم) — از قبل در tenant/__init__.py
        # کامل پیاده شده بود، فقط در پنلِ همکار دکمه‌اش نبود.
        [_b("🤖 تنظیمات عمومی ربات", "set:hub_general", P, "ibtn:adm_settings")],

        # ── تنظیمات و خروج ──
        [_b("⚙️ تنظیمات", "pp:settings", P, "ibtn:pp_settings")],
        [_b("🏠 بازگشت به خانه", "tadm:home", D, "ibtn:adm_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def partner_home_text(bot_id: int) -> str:
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == bot_id))
        br = await s.get(BotRow, bot_id)
    if not pb:
        return "🤝 <b>پنل مدیریت ربات همکار</b>"
    head = "🛠 <b>ربات در حالت آپدیت است</b>\n\n" if (br and br.status != "active") else ""
    return (
        f"{head}🤝 <b>پنل مدیریت ربات همکار</b>\n\n"
        f"📦 موجودی: رشد <b>{pb.stock_growth}</b> | حرفه‌ای <b>{pb.stock_pro}</b>\n"
        f"🏷 تخفیف همکاری: <b>{pb.discount_percent}٪</b>\n"
        f"⏳ اعتبار تا: {jalali(pb.expires_at)}")


@partner_menu_router.callback_query(F.data == "pp:home")
async def pp_home(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok
    await cb.message.edit_text(await partner_home_text(row.id),
                               reply_markup=await partner_home_kb(row.id))
    await cb.answer()


# ────────── زیرمنویِ تنظیمات ──────────

@partner_menu_router.callback_query(F.data == "pp:settings")
async def pp_settings(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    # === PATCH_PARTNER_V12 ===
    # وضعیت را تازه از دیتابیس بخوان: بعد از تغییرِ حالتِ آپدیت،
    # ردیفِ کش‌شده هنوز مقدارِ قدیمی را دارد و برچسبِ دکمه عوض نمی‌شد.
    async with Session() as _s:
        _fresh = await _s.get(BotRow, row.id)
        _bot_status = _fresh.status if _fresh else row.status
        _pb_fresh = await _s.scalar(
            select(PartnerBot).where(PartnerBot.bot_id == row.id))
        if _pb_fresh:
            pb = _pb_fresh

    trial_txt = "🟢 روشن" if pb.trial_enabled else "🔴 خاموش"
    upd_txt = "🔴 روشن" if _bot_status != "active" else "🟢 خاموش"

    # === PATCH_PARTNER_V35 ===
    # برچسب‌هایِ بلند تک‌ردیفه، کوتاه‌ها جفت — وگرنه در گوشی بریده
    # می‌شوند («مدیریت کاربران» و «ارجاع به پشتیبانی» بلندترین‌ها بودند).
    def _b(text: str, cbd: str, style: ButtonStyle) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cbd, style=style)

    S, P, D = ButtonStyle.SUCCESS, ButtonStyle.PRIMARY, ButtonStyle.DANGER

    rows = [
        # ── ارتباط با کاربران ──
        [_b("📢 ارسال همگانی", "tadm:broadcast", P),
         _b("👥 مدیریت کاربران", "tadm:users", P)],
        # === PATCH_PARTNER_CUSTOMERS_V1 ===
        # «مدیریت کاربران» با جستجویِ آیدی کار می‌کند؛ این یکی فهرستِ
        # خریدارها و ربات‌هایشان را مرور می‌کند.
        [_b("🧑‍💼 مشتریان و ربات‌هایشان", "pcu:list:0", S)],
        [_b("🎫 مدیریت تیکت‌ها", "tadm:tickets", P),
         _b("🧾 سفارش‌ها", "tadm:orders", P)],

        # ── کلیدهای وضعیت ──
        [_b(f"🎁 دکمه تست ۲ روزه: {trial_txt}", "pp:trial", P)],
        [_b(f"🛠 حالت آپدیت: {upd_txt}", "pp:update", P)],

        # === PATCH_PARTNER_SUBADMIN_TENANT_V1 ===
        # افزودن/حذفِ ادمینِ فرعیِ خودِ همکار در رباتش (جدول AdminSupport
        # که قبلاً برایِ ربات‌های مستأجرِ معمولی وجود داشت، حالا برایِ
        # ربات همکار هم در دسترس است).
        [_b("👮 ادمین‌های ربات", "tadm:admins", P)],

        # === PATCH_PARTNER_SALE_FIELDS_V1 ===
        [_b("🤖 انتخاب ربات‌ها جهت فروش", "pp:salefields", S)],

        # === PATCH_TENANT_PREMIUM_EMOJI_V1 ===
        [_b("⭐ افزودن ایموجی پرمیوم", "pemj:menu:0", P)],

        # ── پشتیبانی و خروج ──
        [_b("🆘 ارجاع به پشتیبانی بات‌ساز", "pp:escalate", D)],
        [_b("🔙 بازگشت", "pp:home", D)],
    ]
    await cb.message.edit_text(
        "⚙️ <b>تنظیمات ربات همکار</b>\n\n"
        "برای موارد پیچیده‌تر که خودت نمی‌توانی حل کنی، از «ارجاع به "
        "پشتیبانی بات‌ساز» استفاده کن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ────────── کلیدِ دکمهٔ تستِ ۲ روزه ──────────

@partner_menu_router.callback_query(F.data == "pp:trial")
async def pp_trial_toggle(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    async with Session() as s:
        row = await s.get(PartnerBot, pb.id)
        row.trial_enabled = not row.trial_enabled
        new = row.trial_enabled
        await s.commit()
    # === PATCH_PARTNER_V27 ===
    try:
        from tenant.partner_bot.partner_sales import invalidate_pb_cache
        invalidate_pb_cache(_row.id)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ PartnerBot ناموفق بود")
    await cb.answer("دکمهٔ تست " + ("روشن شد" if new else "خاموش شد"))
    await pp_settings(cb, bot)


# ────────── حالتِ آپدیت ──────────

@partner_menu_router.callback_query(F.data == "pp:update")
async def pp_update_toggle(cb: CallbackQuery, bot: Bot):
    """خاموشیِ موقتِ ربات برایِ کاربران.

    از همان مکانیزمِ موجودِ main_tenant.py استفاده می‌کند: وقتی
    Bot.status != "active"، کاربران پیامِ «ربات موقتاً غیرفعال است»
    می‌گیرند ولی *مالک* همچنان دسترسیِ کامل دارد — دقیقاً همان چیزی که
    حالتِ آپدیت باید باشد.
    """
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok

    if pb.status != "active":
        await cb.answer(
            "ربات همکار شما توسط مدیریت متوقف شده و تا رفع آن، "
            "حالت آپدیت در دسترس نیست.", show_alert=True)
        return

    async with Session() as s:
        br = await s.get(BotRow, row.id)
        if br.status == "active":
            br.status = "paused"
            br.lock_reason = "حالت آپدیت (توسط مالک ربات)"
            new_state = "روشن"
        else:
            br.status = "active"
            br.lock_reason = None
            new_state = "خاموش"
        token = br.token
        await s.commit()

    # === PATCH_PARTNER_V11 ===
    # بدونِ باطل‌سازیِ کش، تغییر تا ۲۰ ثانیه اثر نمی‌کرد و کاربر فکر
    # می‌کرد دکمه کار نکرده. هر دو کش پاک می‌شوند:
    #   • _BOT_ROW_CACHE  → همین پروسه
    #   • publish_...     → پروسه‌هایِ دیگر (از راهِ Postgres NOTIFY)
    try:
        from tenant import invalidate_bot_row_cache
        invalidate_bot_row_cache(token)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ محلی ناموفق بود")
    try:
        from tenant_status_bus import publish_tenant_invalidate
        await publish_tenant_invalidate(token)
    except Exception:
        logger.exception("انتشارِ سیگنالِ باطل‌سازی ناموفق بود")

    await cb.answer(f"حالت آپدیت {new_state} شد")
    await pp_settings(cb, bot)


# ────────── ارجاع به پشتیبانیِ بات‌ساز ──────────

@partner_menu_router.callback_query(F.data == "pp:escalate")
async def pp_escalate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_PARTNER_V12 === فهرستِ تیکت‌هایِ ارجاع + ثبتِ جدید."""
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok

    async with Session() as s:
        rows = (await s.scalars(
            select(PartnerEscalation)
            .where(PartnerEscalation.partner_bot_id == pb.id)
            .order_by(PartnerEscalation.id.desc()).limit(10))).all()
        items = [(e.id, e.subject, e.status, e.updated_at) for e in rows]

    b = InlineKeyboardBuilder()
    b.button(text="➕ تیکت جدید", callback_data="pp:esc:new",
             style=ButtonStyle.SUCCESS)
    for eid, subj, st, _upd in items:
        mark = {"open": "🟡", "answered": "🟢", "closed": "⚪️"}.get(st, "🟡")
        b.button(text=f"{mark} #{eid} — {(subj or '—')[:28]}",
                 callback_data=f"pp:esc:view:{eid}",
                 style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="pp:settings",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        "🆘 <b>ارجاع به پشتیبانی بات‌ساز</b>\n\n"
        + ("تیکت‌های شما:" if items else "هنوز تیکتی ثبت نکرده‌ای.")
        + "\n\n🟡 در انتظار پاسخ · 🟢 پاسخ داده شده · ⚪️ بسته",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_menu_router.callback_query(F.data == "pp:esc:new")
async def pp_esc_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    await state.set_state(PartnerMenuFSM.escalate)
    await cb.message.edit_text(
        "🆘 <b>تیکت جدید</b>\n\n"
        "مشکل یا درخواستت را کامل بنویس و بفرست. پیام مستقیم برای "
        "مدیریت بات‌ساز می‌رود و او به همهٔ اطلاعات ربات و کاربرانت "
        "دسترسی دارد تا بررسی کند.")
    await cb.answer()


@partner_menu_router.callback_query(F.data.startswith("pp:esc:view:"))
async def pp_esc_view(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    eid = int(cb.data.split(":")[3])

    async with Session() as s:
        e = await s.get(PartnerEscalation, eid)
        if not e or e.partner_bot_id != pb.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        msgs = (await s.scalars(
            select(PartnerEscalationMsg)
            .where(PartnerEscalationMsg.escalation_id == eid)
            .order_by(PartnerEscalationMsg.id))).all()
        rows = [(x.sender, x.text, x.created_at) for x in msgs]
        st = e.status

    lines = [f"🎫 <b>تیکت #{eid}</b>\n"]
    for sender, text, created in rows[-15:]:
        who = "🛡 پشتیبانی بات‌ساز" if sender == "admin" else "👤 شما"
        lines.append(f"{who} — {jalali(created, with_time=True)}\n{text}\n")

    b = InlineKeyboardBuilder()
    if st != "closed":
        b.button(text="✍️ پاسخ", callback_data=f"pp:esc:reply:{eid}",
                 style=ButtonStyle.SUCCESS)
    b.button(text="🔙 بازگشت", callback_data="pp:escalate",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@partner_menu_router.callback_query(F.data.startswith("pp:esc:reply:"))
async def pp_esc_reply(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    eid = int(cb.data.split(":")[3])
    await state.update_data(pp_esc=eid)
    await state.set_state(PartnerMenuFSM.escalate)
    await cb.message.edit_text("✍️ پاسخت را بنویس و بفرست:")
    await cb.answer()


@partner_menu_router.message(PartnerMenuFSM.escalate)
async def pp_escalate_send(msg: Message, bot: Bot, state: FSMContext):
    ok = await _partner_ok(msg, bot)
    if not ok:
        return
    row, pb = ok
    data = await state.get_data()
    eid = data.get("pp_esc")
    text = (msg.text or "").strip()
    await state.clear()
    if not text:
        await msg.answer(
            "پیام خالی بود. دوباره تلاش کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 بازگشت", callback_data="pp:escalate",
                                     style=ButtonStyle.DANGER)]]))
        return

    async with Session() as s:
        if eid:
            e = await s.get(PartnerEscalation, eid)
            if not e or e.partner_bot_id != pb.id:
                await msg.answer("تیکت پیدا نشد.")
                return
            e.status = "open"
            e.updated_at = datetime.datetime.now()
        else:
            e = PartnerEscalation(
                partner_bot_id=pb.id, owner_id=row.owner_id,
                subject=text[:60], status="open")
            s.add(e)
            await s.flush()
            eid = e.id
        s.add(PartnerEscalationMsg(
            escalation_id=eid, sender="partner", text=text[:4000]))
        await s.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ پاسخ",
                              callback_data=f"pae:reply:{eid}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🤝 ربات همکار این شخص",
                              callback_data=f"pa:view:{pb.id}",
                              style=ButtonStyle.PRIMARY)],
    ])
    sent = await _notify_platform_admin(
        f"🆘 <b>ارجاع از ربات همکار</b> — تیکت #{eid}\n\n"
        f"🤖 ربات: @{row.username or '—'}\n"
        f"👤 صاحب: <code>{row.owner_id}</code>\n\n"
        f"📝 متن:\n{text}", kb)

    if sent:
        await msg.answer(
            f"✅ تیکت #{eid} ثبت و برای پشتیبانی بات‌ساز ارسال شد. "
            "پاسخ از همین ربات برایت می‌آید.")
    else:
        await msg.answer("⚠️ ارسال ناموفق بود. کمی بعد دوباره تلاش کن.")


async def _notify_platform_admin(text: str, kb=None) -> bool:
    if not (PLATFORM_BOT_TOKEN and PLATFORM_ADMIN_ID):
        return False
    try:
        # === PATCH_PARTNER_V12 ===
        # بدونِ DefaultBotProperties تگ‌هایِ HTML خام نمایش داده می‌شدند
        # (<b>...</b> عیناً در پیام دیده می‌شد).
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        pbot = Bot(token=PLATFORM_BOT_TOKEN,
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await pbot.send_message(PLATFORM_ADMIN_ID, text, reply_markup=kb)
        finally:
            await pbot.session.close()
        return True
    except Exception:
        logger.exception("ارجاع به پشتیبانیِ پلتفرم ناموفق بود")
        return False


# ======================= هدایتِ زیربخش‌هایِ منویِ عمومی =======================
# === PATCH_PARTNER_V8 ===
# ماژول‌هایِ بازاستفاده‌شده (نفرات برتر، ارسال به کانال، دکمه‌های صفحهٔ
# اصلی، زیرمجموعه‌گیری، نوع دکمه‌ها) دکمهٔ بازگشتشان به یکی از پنج
# زیربخشِ منویِ عمومیِ مستأجر می‌رود — tadm:sec:stats و امثالش.
#
# آن زیربخش‌ها در پنلِ همکار وجود ندارند، پس کاربر یا در منویِ قدیمی
# می‌افتاد یا صفحه گیر می‌کرد. این هندلر برایِ ربات‌هایِ همکار جلویشان
# را می‌گیرد و به پنلِ خودِ همکار برمی‌گرداند.
#
# چون partner_menu_router قبل از tenant_router اصلی به دیسپچر اضافه
# می‌شود، این هندلر اولویت دارد؛ و چون فقط برایِ plan=="partner" پاسخ
# می‌دهد، بقیهٔ ربات‌ها دست‌نخورده می‌مانند.

# صفحاتی که *واقعاً* در پنلِ همکار وجود دارند و باید عبور کنند.
# هرچه در این فهرست نباشد، به پنلِ همکار هدایت می‌شود.
#
# چرا فهرستِ سفید و نه سیاه: مقصدهایِ بازگشتِ ماژول‌هایِ بازاستفاده‌شده
# متنوع‌اند (tadm:sec:stats، tadm:stats، tadm:menu، ...) و هر بار که یکی
# جا می‌افتاد، کاربر در صفحه‌ای می‌افتاد که در پنلِ همکار وجود ندارد.
# با فهرستِ سفید، هر مقصدِ ناشناخته‌ای خودبه‌خود امن می‌شود.
_PARTNER_ALLOWED_TADM = {
    "tadm:home",        # بازگشت به صفحهٔ اصلیِ ربات
    "tadm:chpost",      # ارسال به کانال و گروه
    "tadm:ref_report",  # زیرمجموعه‌گیری
    "tadm:homebtns",    # دکمه‌های صفحهٔ اصلی
    "tadm:btn_colors",  # مدیریت رنگ دکمه‌های اینلاین
    "tadm:orders",      # سفارش‌ها
    # === PATCH_PARTNER_ORDERS_DETAIL_V1 ===
    # این دو زیرِ همون صفحهٔ «سفارش‌ها و رسیدها» صدا زده می‌شن (کلیک روی
    # یکی از سفارش‌های اخیر، یا دکمهٔ «حذف سفارش» رویِ رسیدهایِ در
    # انتظار)؛ چون در فهرست نبودن، pp_sec_redirect قبل از رسیدن به
    # هندلرِ واقعی‌شون (tenant/__init__.py) کاربر رو به خانهٔ پنلِ همکار
    # پرت می‌کرد — یعنی این دو دکمه از دیدِ کاربر «هیچ کاری نمی‌کردن».
    "tadm:order_detail",
    "tadm:order_del",
    "tadm:users",       # مدیریت کاربران
    "tadm:broadcast",   # ارسال همگانی
    "tadm:order_search",
    "tadm:tickets",
    # === PATCH_PARTNER_V15 === صفحاتِ بخشِ مالی
    "tadm:bulkwallet",
    "tadm:buyinfo",
    # === PATCH_PARTNER_V36 === زیربخشِ عضویتِ اجباری در هابِ عمومی
    "tadm:forcejoin",
    # === PATCH_PARTNER_TRUST_STATS_FIX_V1 ===
    # این دو، از داخلِ هابِ «تنظیماتِ عمومیِ ربات» (set:hub_general) صدا
    # زده می‌شن. قبلاً در این فهرست نبودن، پس pp_sec_redirect جلویِ
    # tadm:trustreport و tadm:stats رو می‌گرفت و کاربر رو، به‌جایِ بازِ
    # صفحهٔ واقعی، برمی‌گردوند به خانهٔ پنلِ همکار — یعنی این دو دکمه
    # عملاً کار نمی‌کردن. هندلرهایِ خودشون (trust_report.py و
    # tenant/__init__.py:tadm_stats) دقیقاً همون چیزیه که ربات‌هایِ
    # مستأجر هم استفاده می‌کنن، پس امکاناتش عیناً یکسان می‌شه.
    "tadm:trustreport",
    "tadm:stats",
    "tadm:ulist",        # لیستِ اعضا/خریداران/بلاک‌شده‌ها/مسدودها (زیرِ آمار)
    "tadm:uinfo",         # کارتِ یک کاربر از داخلِ همون لیست‌ها
    "tadm:banned_list",   # لیستِ کاربرانِ مسدود (زیرِ آمار)
    # === PATCH_PARTNER_SUBADMIN_TENANT_V1 ===
    "tadm:admins",        # ادمین‌های پشتیبانیِ ربات (افزودن/حذفِ ادمینِ فرعی)
}

# === PATCH_PARTNER_V12 ===
# "tadm:menu" عمداً از فهرستِ سفید برداشته شد. در حالتِ کیبورد،
# tadmin_kb یک ReplyKeyboardMarkup برمی‌گرداند و tadm_menu آن را با
# edit_text می‌فرستد — نتیجه: کاربر ناگهان از منویِ شیشه‌ای به کیبوردِ
# فیزیکی پرت می‌شد. حالا خودمان با پنلِ اینلاینِ همکار جوابش می‌دهیم.


def _partner_needs_redirect(data: str) -> bool:
    """آیا این callbackِ tadm باید به پنلِ همکار هدایت شود؟

    فقط فضایِ نامِ tadm: بررسی می‌شود. فضاهایِ دیگر (set:, bw:, tc:,
    ticket:, w:) صفحاتِ مستقلی هستند که خودشان بازگشتِ درست دارند.
    """
    if not data.startswith("tadm:"):
        return False
    if data in _PARTNER_ALLOWED_TADM:
        return False
    # زیرشاخه‌هایِ صفحاتِ مجاز هم عبور می‌کنند (مثلاً tadm:chpost:addbtn)
    return not any(data.startswith(a + ":") for a in _PARTNER_ALLOWED_TADM)


@partner_menu_router.callback_query(
    F.data.func(_partner_needs_redirect))
async def pp_sec_redirect(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        # رباتِ همکار نیست → هیچ کاری نکن تا هندلرِ اصلی جواب دهد.
        raise SkipHandler
    row, _pb = ok
    try:
        await cb.message.edit_text(await partner_home_text(row.id),
                                   reply_markup=await partner_home_kb(row.id))
    except Exception:
        await cb.message.answer(await partner_home_text(row.id),
                                reply_markup=await partner_home_kb(row.id))
    await cb.answer()


# === PATCH_PARTNER_V9 ===
@partner_menu_router.callback_query(F.data == "pp:menumode")
async def pp_menu_mode(cb: CallbackQuery, bot: Bot):
    """تغییرِ نوعِ منویِ صفحهٔ اصلیِ ربات: کیبوردِ پایین ↔ شیشه‌ای.

    از همان کلیدِ menu_mode استفاده می‌کند که بقیهٔ ربات‌هایِ مستأجر
    دارند، پس تمامِ منطقِ رندرِ موجود بدونِ تغییر کار می‌کند.
    """
    from tenant import get_setting, set_setting

    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok

    cur = await get_setting(row.id, "menu_mode", "reply")
    new = "inline" if cur != "inline" else "reply"
    await set_setting(row.id, "menu_mode", new)
    await cb.answer(
        ("💠 منوی شیشه‌ای فعال شد. کاربران با /start منوی جدید را می‌بینند."
         if new == "inline" else
         "⌨️ کیبورد پایین فعال شد. کاربران با /start منوی جدید را می‌بینند."),
        show_alert=True)
    # === PATCH_PARTNER_V11 === دکمه حالا در پنلِ اصلی است
    await pp_home(cb, bot)


# ======================= مالی و کیف پول =======================
# === PATCH_PARTNER_V15 ===
# همان زیرمنویِ مالیِ ربات‌هایِ مستأجر، ولی هرس‌شده: گزینه‌هایی که به
# فروشِ کانفیگ/محصول مربوطند (کدِ تخفیف، تاییدِ خودکارِ رسید، کانالِ
# رسید) این‌جا معنی ندارند. هندلرهایشان همان‌هایِ موجودند — هیچ کدِ
# پرداختی از نو نوشته نشده.

@partner_menu_router.callback_query(F.data == "pp:finance")
async def pp_finance(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok

    from tenant import get_setting

    card = (await get_setting(row.id, "card_number", "") or "").strip()
    card_on = await get_setting(row.id, "card_enabled", "1") == "1"
    wallet_charge = await get_setting(
        row.id, "wallet_charge_in_purchase_enabled", "0") == "1"

    b = InlineKeyboardBuilder()
    b.button(text="💳 شماره کارت و کارت‌به‌کارت", callback_data="set:card",
             style=ButtonStyle.SUCCESS)
    b.button(
        text=("🟢 کارت‌به‌کارت: فعال" if card_on else "🔴 کارت‌به‌کارت: غیرفعال"),
        callback_data="pp:cardtoggle",
        style=(ButtonStyle.SUCCESS if card_on else ButtonStyle.DANGER))
    b.button(text="🪙 درگاه ارز دیجیتال (NOWPayments)", callback_data="set:crypto",
             style=ButtonStyle.PRIMARY)
    b.button(text="🟦 درگاه سواپ‌ولت (SwapPay)", callback_data="set:swap",
             style=ButtonStyle.PRIMARY)
    b.button(text="⭐ پرداخت با استارز تلگرام", callback_data="set:stars",
             style=ButtonStyle.PRIMARY)
    b.button(text="💠 درگاه تون‌پیز (TonPays)", callback_data="set:tonpays",
             style=ButtonStyle.PRIMARY)
    b.button(
        text=("🔋 شارژ کیف‌پول در صفحهٔ خرید: ✅" if wallet_charge
              else "🔋 شارژ کیف‌پول در صفحهٔ خرید: ❌"),
        callback_data="set:wallet_charge_toggle",
        style=ButtonStyle.PRIMARY)
    b.button(text="💸 شارژ/کسر همگانی کیف پول", callback_data="tadm:bulkwallet",
             style=ButtonStyle.PRIMARY)
    b.button(text="🧾 سفارش‌ها و رسیدها", callback_data="tadm:orders",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="pp:home",
             style=ButtonStyle.DANGER)
    b.adjust(1, 1, 2, 2, 1, 1, 1, 1)

    if card:
        masked = card if len(card) < 8 else f"{card[:4]}****{card[-4:]}"
        card_line = (f"💳 شماره کارت: <code>{masked}</code>\n"
                     f"وضعیت نمایش در خرید: "
                     f"<b>{'نمایش داده می‌شود' if card_on else 'مخفی است'}</b>")
    else:
        card_line = ("💳 شماره کارت: <b>تنظیم نشده</b>\n"
                     "تا وقتی ثبت نکنی، کارت‌به‌کارت به کاربرانت نمایش "
                     "داده نمی‌شود.")

    await cb.message.edit_text(
        "💰 <b>مالی و کیف پول</b>\n\n"
        f"{card_line}\n\n"
        "ℹ️ این تنظیمات مربوط به پولی است که <b>از کاربران خودت</b> "
        "می‌گیری — نه پرداختِ خودت به بات‌ساز.",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_menu_router.callback_query(F.data == "pp:cardtoggle")
async def pp_card_toggle(cb: CallbackQuery, bot: Bot):
    """نمایش/مخفی‌کردنِ کارت‌به‌کارت در صفحهٔ خریدِ کاربران."""
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok

    from tenant import get_setting, set_setting

    card = (await get_setting(row.id, "card_number", "") or "").strip()
    now_on = await get_setting(row.id, "card_enabled", "1") == "1"

    if not now_on and not card:
        await cb.answer(
            "اول شمارهٔ کارت را ثبت کن، بعد فعالش کن.", show_alert=True)
        return

    await set_setting(row.id, "card_enabled", "0" if now_on else "1")
    await cb.answer("کارت‌به‌کارت " + ("مخفی شد" if now_on else "نمایش داده می‌شود"))
    await pp_finance(cb, bot)


# ────────── انتخابِ نوعِ ربات‌هایی که برایِ فروش نمایش داده می‌شوند ──────────
# === PATCH_PARTNER_SALE_FIELDS_V1 ===
# انتخابِ اینجا هم در فروش (ps:field)، هم در «تغییرِ نوعِ ربات» و هم در
# ویزاردِ شخصی‌سازیِ دکمه‌ها اعمال می‌شود — پس مشتری هیچ‌جا نوعی را نمی‌بیند
# که مالکِ همکار نفروخته است.


async def _salefields_view(bot_id: int):
    from tenant.partner_bot.partner_build import FIELDS, allowed_fields, ACTIVE_FIELDS
    picked = {k for k, _ in await allowed_fields(bot_id)}
    rows = []
    # === PATCH_LOCK_TO_THREE_FIELDS_V1 ===
    # نوع‌هایِ قفل‌شده اصلاً اینجا نشون داده نمی‌شن — تیک‌زدنشون بی‌اثر بود
    # و فقط گیج‌کننده می‌شد.
    for key, label in FIELDS:
        if key not in ACTIVE_FIELDS:
            continue
        mark = "✅" if key in picked else "⬜️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {label}", callback_data=f"pp:sf:{key}",
            style=(ButtonStyle.SUCCESS if key in picked else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="pp:settings",
                                      style=ButtonStyle.DANGER)])
    text = (
        "🤖 <b>انتخاب ربات‌ها جهت فروش</b>\n\n"
        "تیکِ هر نوع را بزن تا در فروشگاهِ رباتت به مشتری‌ها نشان داده شود.\n\n"
        "این انتخاب همه‌جا اعمال می‌شود: خرید، تمدید، تغییرِ نوعِ ربات و "
        "شخصی‌سازیِ دکمه‌ها.\n\n"
        "⚠️ اگر همه را برداری، دوباره همه نمایش داده می‌شوند "
        "(فروشگاهِ خالی به‌نظر می‌رسد ربات خراب است)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@partner_menu_router.callback_query(F.data == "pp:salefields")
async def pp_salefields(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok
    text, kb = await _salefields_view(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@partner_menu_router.callback_query(F.data.startswith("pp:sf:"))
async def pp_salefield_toggle(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, _pb = ok
    from tenant.partner_bot.partner_build import FIELDS, allowed_fields, set_allowed_fields, ACTIVE_FIELDS
    key = cb.data.split(":", 2)[2]
    if key not in dict(FIELDS) or key not in ACTIVE_FIELDS:  # PATCH_LOCK_TO_THREE_FIELDS_V1
        await cb.answer("نامعتبر", show_alert=True)
        return
    picked = {k for k, _ in await allowed_fields(row.id)}
    if key in picked:
        picked.discard(key)
    else:
        picked.add(key)
    await set_allowed_fields(row.id, picked)
    text, kb = await _salefields_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام به‌روزرسانیِ فهرستِ نوع‌ها")
    await cb.answer("ذخیره شد")
