"""
tenant/partner_user.py — بخشِ کاربریِ «ربات همکار» (سمتِ خریدار).

=== PATCH_PARTNER_USER_V1 ===
چیزهایی که کاربرِ ربات همکار می‌بیند:
    • 👤 مشخصات کاربری
    • 🤖 ربات‌های من  (ربات‌هایی که از همین همکار خریده)
    • ⬆️ ارتقا پلن

نکتهٔ طراحی — چرا «ویرایش دکمه‌ها» این‌جا نیست:
رباتِ خریداری‌شده یک رباتِ مستأجرِ کاملاً عادی است و خودش پنلِ مدیریتِ
کامل دارد. خریدار دکمه‌ها و چیدمانِ صفحهٔ اصلی را **داخلِ رباتِ خودش**
با tadm:homebtns ویرایش می‌کند. تکرارِ آن این‌جا یعنی نگهداریِ دو نسخه
از یک ویزارد — پس فقط به آن راهنمایی می‌شود.

چیزهایی که *باید* این‌جا باشند، چون از داخلِ رباتِ فروخته‌شده قابلِ
انجام نیستند: تعویضِ توکن، واگذاریِ مالکیت، حذفِ ربات، و ارتقایِ پلن
(چون پول و موجودی نزدِ همکار است).

ثبتِ تیکت از ماژولِ موجودِ tenant/support_ticket.py می‌آید (ticket:menu).
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, func

from config import Session
from models import (
    Bot as BotRow, BotUser, PartnerBot, PartnerSale, Setting, WalletTxn,
)
from bot_utils import set_tenant_webhook
from partner_rules import jalali, log_stock  # PATCH_PARTNER_V4 / STOCK_LOG_V1
# === PATCH_TRIAL_UPGRADE_INPLACE_V1 ===
from tenant.partner_bot.partner_sales import SOLD_BOT_DAYS

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_user_router = Router()

_PLAN_FA = {"growth": "🚀 رشد", "pro": "💎 حرفه‌ای", "free": "پایه"}


class PartnerUserFSM(StatesGroup):
    new_token = State()
    transfer_to = State()


async def _ctx(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot, BotUser] | None:
    """ربات همکار + کاربرِ فعلی. برایِ هر عملیاتِ کاربری لازم است."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    uid = cb_or_msg.from_user.id
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
        bu = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == uid))
    from config import PLATFORM_ADMIN_ID as _PA   # PATCH_BAN_ONLY_MAIN_ADMIN_EXEMPT_V1
    _bu_banned = bool(bu and bu.is_banned) and not (_PA and int(getattr(bu, "telegram_id", 0) or 0) == int(_PA))
    if not pb or not bu or _bu_banned:
        return None
    return row, pb, bu


async def _sale_price(bot_id: int, plan: str) -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == f"sale_price_{plan}"))
    try:
        return int(r.value) if r and r.value else 0
    except ValueError:
        return 0


# ────────── مشخصات کاربری ──────────

@partner_user_router.callback_query(F.data == "pu:me")
async def pu_me(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok

    async with Session() as s:
        bought = await s.scalar(select(func.count(PartnerSale.id)).where(
            PartnerSale.partner_bot_id == pb.id,
            PartnerSale.buyer_telegram_id == bu.telegram_id,
            PartnerSale.kind == "sold")) or 0
        trials = await s.scalar(select(func.count(PartnerSale.id)).where(
            PartnerSale.partner_bot_id == pb.id,
            PartnerSale.buyer_telegram_id == bu.telegram_id,
            PartnerSale.kind == "trial")) or 0
        spent = await s.scalar(
            select(func.coalesce(func.sum(PartnerSale.price), 0)).where(
                PartnerSale.partner_bot_id == pb.id,
                PartnerSale.buyer_telegram_id == bu.telegram_id)) or 0

    b = InlineKeyboardBuilder()
    b.button(text="🤖 ربات‌های من", callback_data="pu:bots",
             style=ButtonStyle.SUCCESS)  # === PATCH_PARTNER_V6 ===
    b.button(text="👛 شارژ کیف پول", callback_data="w:charge",
             style=ButtonStyle.SUCCESS)
    b.button(text="🎫 پشتیبانی", callback_data="ticket:menu",
             style=ButtonStyle.PRIMARY)
    b.adjust(1, 2)

    await cb.message.edit_text(
        "👤 <b>مشخصات کاربری</b>\n\n"
        f"نام: {bu.full_name or '—'}\n"
        f"آیدی عددی: <code>{bu.telegram_id}</code>\n"
        f"یوزرنیم: {('@' + bu.username) if bu.username else '—'}\n"
        f"📅 عضویت: {jalali(bu.joined_at)}\n\n"
        f"👛 موجودی: <b>{int(bu.balance):,} تومان</b>\n"
        f"🤖 ربات‌های خریداری‌شده: <b>{bought}</b>\n"
        f"🎁 ربات تست: <b>{trials}</b>\n"
        f"💵 مجموع خرید: <b>{int(spent):,} تومان</b>",
        reply_markup=b.as_markup())
    await cb.answer()


# ────────── ربات‌های من ──────────

@partner_user_router.callback_query(F.data == "pu:bots")
async def pu_bots(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok

    async with Session() as s:
        rows = (await s.execute(
            select(PartnerSale, BotRow)
            .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == pb.id,
                   PartnerSale.buyer_telegram_id == bu.telegram_id,
                   # === PATCH_PARTNER_V25 ===
                   # ربات‌هایِ حذف‌شده در بایگانی‌اند، نه فهرستِ کاربر.
                   BotRow.archived_at.is_(None))
            .order_by(PartnerSale.id.desc()))).all()
        items = [(ps.id, br.id, br.username, br.plan, br.status,
                  br.plan_expires_at, ps.kind) for ps, br in rows]

    if not items:
        b = InlineKeyboardBuilder()
        b.button(text="🤖 ساخت ربات", callback_data="ps:menu",
                 style=ButtonStyle.SUCCESS)
        # === PATCH_PARTNER_MYBOTS_BACK_FIX_V1 ===
        # این صفحه از دکمهٔ سطحِ‌بالایِ «🤖 ربات‌های من» توی خانهٔ ربات باز
        # می‌شود (نه از داخلِ «مشخصات کاربری»)، پس بازگشتش باید به خانه
        # باشد، نه به پروفایل.
        b.button(text="🔙 بازگشت", callback_data="mbtn:home",
             style=ButtonStyle.DANGER)
        b.adjust(1)
        await cb.message.edit_text(
            "هنوز رباتی نداری. از دکمهٔ زیر می‌تونی اولین رباتت را بسازی.",
            reply_markup=b.as_markup())
        await cb.answer()
        return

    b = InlineKeyboardBuilder()
    for _sid, bid, uname, plan, status, _exp, kind in items:
        mark = "🟢" if status == "active" else "⏸"
        tag = "🎁" if kind == "trial" else ""
        # === PATCH_PARTNER_V6 ===
        # رنگ گویایِ وضعیت: فعال سبز، متوقف قرمز.
        b.button(text=f"{mark}{tag} @{uname or '—'} · {_PLAN_FA.get(plan, plan)}",
                 callback_data=f"pu:bot:{bid}",
                 style=(ButtonStyle.SUCCESS if status == "active"
                        else ButtonStyle.DANGER))
    # === PATCH_PARTNER_MYBOTS_BACK_FIX_V1 ===
    b.button(text="🔙 بازگشت", callback_data="mbtn:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        f"🤖 <b>ربات‌های من</b> ({len(items)} مورد)\n\n"
        "روی هرکدام بزن تا مدیریتش کنی.",
        reply_markup=b.as_markup())
    await cb.answer()


async def _owns(pb_id: int, uid: int, bot_id: int) -> tuple | None:
    """بررسیِ اینکه این ربات واقعاً از همین همکار و متعلق به همین کاربر است."""
    async with Session() as s:
        ps = await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb_id,
            PartnerSale.sold_bot_id == bot_id,
            PartnerSale.buyer_telegram_id == uid))
        if not ps:
            return None
        br = await s.get(BotRow, bot_id)
        if not br or br.owner_id != uid:
            return None
        return ps.id, ps.kind, br.username, br.plan, br.status, br.plan_expires_at


def _bot_kb(bot_id: int, plan: str, kind: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # === PATCH_PARTNER_V19 ===
    b.button(text="🎨 دکمه‌های صفحهٔ اصلی", callback_data=f"pb:start:{bot_id}",
             style=ButtonStyle.SUCCESS)
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 ===
    # رباتِ تست: تا وقتی خریداری نشده، نه تعویض توکن نه واگذاریِ مالکیت.
    # (پلنِ رایگان هم همین‌طور؛ چکِ کاملش سرورساید در خودِ هندلرهاست.)
    _swap_open = kind != "trial" and plan not in ("free", "", None)
    if _swap_open:
        b.button(text="🔑 تعویض توکن", callback_data=f"pu:token:{bot_id}",
                 style=ButtonStyle.PRIMARY)
        b.button(text="👑 واگذاری مالکیت", callback_data=f"pu:tr:{bot_id}",
                 style=ButtonStyle.PRIMARY)
    # === PATCH_TRIAL_NO_RENEW_V1 ===
    # رباتِ تست تمدید نمی‌شود، ولی باید راهِ *خرید* داشته باشد — وگرنه
    # کاربری که تست را پسندیده هیچ دکمه‌ای برایِ ادامه ندارد و ربات
    # بی‌صدا منقضی می‌شود. این بدترین حالت برایِ فروش است.
    if kind == "trial":
        # === PATCH_TRIAL_UPGRADE_INPLACE_V1 ===
        # قبلاً این دکمه به ps:menu می‌رفت که جریانِ ساختِ رباتِ *جدید* است؛
        # کاربری که روی رباتِ تستش کلی چیدمان و تنظیمات انجام داده بود،
        # با زدنش پیامِ «توکنِ یک رباتِ تازه بفرست» می‌گرفت و گیر می‌کرد.
        # حالا همان ربات سرِ جایش ارتقا می‌یابد و همه‌چیزش حفظ می‌شود.
        b.button(text="⬆️ ارتقا و فعال‌سازی ۳۰ روزه",
                 callback_data=f"pu:tup:{bot_id}", style=ButtonStyle.SUCCESS)
    elif plan == "growth":
        b.button(text="🔄 تمدید ۳۰ روزه", callback_data=f"pu:ren:{bot_id}",
                 style=ButtonStyle.SUCCESS)
        b.button(text="⬆️ ارتقا به حرفه‌ای", callback_data=f"pu:up:{bot_id}",
                 style=ButtonStyle.SUCCESS)
    elif plan == "pro":
        b.button(text="🔄 تمدید ۳۰ روزه", callback_data=f"pu:ren:{bot_id}",
                 style=ButtonStyle.SUCCESS)
    b.button(text="🗑 حذف ربات", callback_data=f"pu:del:{bot_id}",
             style=ButtonStyle.DANGER)
    b.button(text="🔙 بازگشت", callback_data="pu:bots",
             style=ButtonStyle.DANGER)
    # === PATCH_TRIAL_UPGRADE_INPLACE_V1 ===
    # چیدمان با adjust(1) ساده شد: تعدادِ دکمه‌ها حالا به پلن بستگی دارد و
    # الگویِ ثابتِ قبلی با اضافه‌شدنِ «تمدید» به‌هم می‌ریخت.
    b.adjust(1)
    return b.as_markup()


@partner_user_router.callback_query(F.data.startswith("pu:bot:"))
async def pu_bot_detail(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    _sid, kind, uname, plan, status, exp = info

    await cb.message.edit_text(
        f"🤖 <b>@{uname or '—'}</b>\n\n"
        f"پلن: {_PLAN_FA.get(plan, plan)}"
        + (" — <b>تست رایگان</b>" if kind == "trial" else "") + "\n"
        f"وضعیت: {'🟢 فعال' if status == 'active' else '⏸ متوقف'}\n"
        f"⏳ اعتبار تا: {jalali(exp)}\n\n"
        + ("\n💡 <b>این ربات در حالت تست است.</b> با «⬆️ ارتقا و فعال‌سازی "
           "۳۰ روزه» می‌توانی همین ربات را — با همهٔ تنظیمات و دکمه‌هایی "
           "که ساخته‌ای — به پلن کامل تبدیل کنی. لازم نیست ربات جدید "
           "بسازی.\n"
           if kind == "trial" else "")
        + "\nℹ️ با «🎨 دکمه‌های صفحهٔ اصلی» می‌توانی چیدمان رباتت را عوض کنی.",
        reply_markup=_bot_kb(bot_id, plan, kind))
    await cb.answer()


# ────────── تعویض توکن ──────────

@partner_user_router.callback_query(F.data.startswith("pu:token:"))
async def pu_token_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owns(pb.id, bu.telegram_id, bot_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 === چکِ سرورساید (دکمهٔ قدیمی هم بی‌اثر شود)
    from bot_utils import owner_swap_lock_reason
    from models import Bot as _BotRow
    async with Session() as _s:
        _lock = await owner_swap_lock_reason(_s, await _s.get(_BotRow, bot_id))
    if _lock:
        await cb.answer(_lock.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    await state.update_data(pu_bot=bot_id)
    await state.set_state(PartnerUserFSM.new_token)
    await cb.message.edit_text(
        "🔑 <b>تعویض توکن</b>\n\n"
        "توکن جدید را از @BotFather بگیر و همین‌جا بفرست.\n\n"
        "⚠️ بعد از تعویض، ربات قبلی دیگر کار نمی‌کند و همهٔ اطلاعات "
        "(کاربران، سفارش‌ها، تنظیمات) به توکن جدید منتقل می‌شود.")
    await cb.answer()


@partner_user_router.message(PartnerUserFSM.new_token)
async def pu_token_save(msg: Message, bot: Bot, state: FSMContext):
    from bot_utils import _temp_bot

    ok = await _ctx(msg, bot)
    if not ok:
        return
    _row, pb, bu = ok
    data = await state.get_data()
    bot_id = data.get("pu_bot")
    token = (msg.text or "").strip()

    if ":" not in token or len(token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return
    if not bot_id or not await _owns(pb.id, bu.telegram_id, bot_id):
        await state.clear()
        await msg.answer("این مرحله منقضی شده.")
        return

    try:
        async with _temp_bot(token) as tmp:
            me = await tmp.get_me()
    except Exception:
        await msg.answer("نتونستم با این توکن وصل بشم. درست کپی شده؟")
        return

    async with Session() as s:
        from bot_utils import reclaim_token_if_archived
        _freed, dup = await reclaim_token_if_archived(
            s, token, bu.telegram_id, exclude_bot_id=bot_id)  # PATCH_TOKEN_RECLAIM_V1
        if dup:
            await msg.answer(
                "این توکن همین حالا رویِ یک رباتِ فعالِ دیگر ثبت است. "
                "اگر مالِ خودته اول حذفش کن، بعد دوباره بفرست.")
            return
        br = await s.get(BotRow, bot_id)
        br.token = token
        br.username = me.username
        await s.commit()

    await state.clear()
    hook_ok = await set_tenant_webhook(token)
    # === PATCH_PARTNER_V35 ===
    _kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 ربات‌های من", callback_data="pu:bots",
                             style=ButtonStyle.SUCCESS)]])
    await msg.answer(
        f"✅ توکن ربات به @{me.username} تغییر کرد."
        + ("" if hook_ok else
           "\n\n⚠️ اتصال برقرار نشد؛ به پشتیبانی همین ربات پیام بده."),
        reply_markup=_kb)


# ────────── واگذاری مالکیت ──────────

@partner_user_router.callback_query(F.data.startswith("pu:tr:"))
async def pu_transfer_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owns(pb.id, bu.telegram_id, bot_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 === چکِ سرورساید (دکمهٔ قدیمی هم بی‌اثر شود)
    from bot_utils import owner_swap_lock_reason
    from models import Bot as _BotRow
    async with Session() as _s:
        _lock = await owner_swap_lock_reason(_s, await _s.get(_BotRow, bot_id))
    if _lock:
        await cb.answer(_lock.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    await state.update_data(pu_bot=bot_id)
    await state.set_state(PartnerUserFSM.transfer_to)
    await cb.message.edit_text(
        "👑 <b>واگذاری مالکیت</b>\n\n"
        "آیدی عددی شخص جدید را بفرست.\n\n"
        "⚠️ او باید <b>قبلاً همین ربات را استارت کرده باشد</b>، وگرنه "
        "انتقال انجام نمی‌شود.\n"
        "⚠️ این کار <b>برگشت‌ناپذیر</b> است و دیگر به ربات دسترسی نداری.")
    await cb.answer()


@partner_user_router.message(PartnerUserFSM.transfer_to)
async def pu_transfer_do(msg: Message, bot: Bot, state: FSMContext):
    ok = await _ctx(msg, bot)
    if not ok:
        return
    row, pb, bu = ok
    data = await state.get_data()
    bot_id = data.get("pu_bot")
    raw = (msg.text or "").strip()

    if not raw.isdigit():
        await msg.answer("فقط آیدی عددی بفرست.")
        return
    target = int(raw)
    if target == bu.telegram_id:
        await msg.answer("نمی‌توانی به خودت واگذار کنی.")
        return
    if not bot_id or not await _owns(pb.id, bu.telegram_id, bot_id):
        await state.clear()
        await msg.answer("این مرحله منقضی شده.")
        return

    async with Session() as s:
        # گیرنده باید کاربرِ همین رباتِ همکار باشد — جلوی واگذاری به
        # آیدیِ اشتباه/جعلی را می‌گیرد.
        tgt = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == target))
        if not tgt:
            await msg.answer(
                "این شخص هنوز ربات را استارت نکرده. اول از او بخواه "
                "ربات را استارت کند، بعد دوباره تلاش کن.")
            return
        br = await s.get(BotRow, bot_id)
        br.owner_id = target
        ps = await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb.id,
            PartnerSale.sold_bot_id == bot_id))
        if ps:
            ps.buyer_telegram_id = target
        uname = br.username
        await s.commit()

    await state.clear()
    # === PATCH_PARTNER_V35 ===
    _kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 ربات‌های من", callback_data="pu:bots",
                             style=ButtonStyle.SUCCESS)]])
    await msg.answer(
        f"✅ مالکیت @{uname or '—'} به <code>{target}</code> واگذار شد.",
        reply_markup=_kb)
    try:
        await bot.send_message(
            target, f"👑 مالکیت ربات @{uname or '—'} به شما واگذار شد.")
    except Exception:
        logger.exception("اطلاعِ واگذاریِ مالکیت به گیرنده ناموفق بود")


# ────────── حذف ربات ──────────

@partner_user_router.callback_query(F.data.startswith("pu:del:"))
async def pu_delete_ask(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owns(pb.id, bu.telegram_id, bot_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="🗑 بله، حذف کن", callback_data=f"pu:delok:{bot_id}",
             style=ButtonStyle.DANGER)
    b.button(text="🔙 انصراف", callback_data=f"pu:bot:{bot_id}",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "🗑 <b>حذف ربات</b>\n\n"
        "مطمئنی؟ ربات <b>خاموش</b> می‌شود، از فهرست ربات‌هایت خارج "
        "می‌گردد و به <b>بایگانی</b> می‌رود.\n\n"
        "⚠️ <b>هیچ مبلغی بازگردانده نمی‌شود.</b>\n"
        "⚠️ برای بازگرداندن باید با پشتیبانی تماس بگیری.",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_user_router.callback_query(F.data.startswith("pu:delok:"))
async def pu_delete_do(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owns(pb.id, bu.telegram_id, bot_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return

    async with Session() as s:
        br = await s.get(BotRow, bot_id)
        br.status = "paused"
        br.archived_at = datetime.datetime.now()
        br.archive_reason = "user_deleted"
        br.lock_reason = "حذف‌شده توسط مالک"
        token = br.token
        await s.commit()

    # === PATCH_PARTNER_V25 ===
    # وبهوک را هم قطع کن، وگرنه ربات همچنان به پیام‌ها جواب می‌دهد و
    # کاربر فکر می‌کند حذف نشده.
    try:
        from bot_utils import _temp_bot
        async with _temp_bot(token) as tb:
            await tb.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("قطعِ وبهوکِ رباتِ حذف‌شده ناموفق بود")

    # حذفِ ربات یعنی یکی از ربات‌های فعالِ همکار کم شد → تایمرِ حدِ نصاب
    # === PATCH_PARTNER_DELETE_CRASH_FIX_V1 ===
    # این تابع (و رندرِ لیستِ زیرش) قبلاً بدونِ try/except بودند — یعنی اگه
    # هرکدام به هر دلیلی (مثلاً یک لحظه قطعیِ دیتابیس) خطا می‌داد، با اینکه
    # خودِ حذف/بایگانی‌شدنِ ربات (بالاتر) قبلاً با موفقیت commit شده بود،
    # کاربر به‌جایِ تاییدِ حذف، با پیامِ عمومیِ «ERR-XXXXXX» مواجه می‌شد و فکر
    # می‌کرد اصلاً نتونسته حذف کنه.
    try:
        from tenant.partner_bot.partner_sales import refresh_low_stock_timer
        await refresh_low_stock_timer(pb.id)
    except Exception:
        logger.exception("refresh_low_stock_timer بعدِ حذفِ ربات ناموفق بود")

    await cb.answer("ربات حذف و بایگانی شد")
    try:
        await pu_bots(cb, bot)
    except Exception:
        logger.exception("رندرِ فهرستِ ربات‌ها بعدِ حذف ناموفق بود")


# ────────── ارتقا پلن ──────────

# === PATCH_TRIAL_UPGRADE_INPLACE_V1 ===
# ارتقایِ رباتِ تست به یک پلنِ واقعی، و تمدیدِ ۳۰ روزهٔ ربات‌های خریداری‌شده.
# هر دو *در جای خود* انجام می‌شوند: توکن، دکمه‌ها، تنظیمات و کاربرانِ ربات
# دست‌نخورده می‌مانند.

async def _plan_pick_screen(cb, row, pb, bu, bot_id: int, mode: str):
    """صفحهٔ انتخابِ پلن برایِ ارتقا/تمدید. mode: "tup" یا "ren"."""
    pg = await _sale_price(row.id, "growth")
    pp = await _sale_price(row.id, "pro")
    b = InlineKeyboardBuilder()
    lines = []
    if mode == "tup":
        lines.append("⬆️ <b>ارتقا و فعال‌سازی ۳۰ روزه</b>\n")
        lines.append("همین ربات با تمام تنظیمات و دکمه‌هایت فعال می‌شود؛ "
                     "نیازی به ساختِ رباتِ جدید یا توکنِ تازه نیست.\n")
    else:
        lines.append("🔄 <b>تمدید ۳۰ روزه</b>\n")
        lines.append("۳۰ روز به اعتبارِ فعلیِ ربات اضافه می‌شود.\n")
    any_plan = False
    if pg > 0 and pb.stock_growth > 0:
        any_plan = True
        b.button(text=f"🚀 رشد — {pg:,} تومان",
                 callback_data=f"pu:{mode}ok:{bot_id}:growth", style=ButtonStyle.SUCCESS)
        lines.append(f"🚀 رشد: <b>{pg:,}</b> تومان")
    if pp > 0 and pb.stock_pro > 0:
        any_plan = True
        b.button(text=f"💎 حرفه‌ای — {pp:,} تومان",
                 callback_data=f"pu:{mode}ok:{bot_id}:pro", style=ButtonStyle.SUCCESS)
        lines.append(f"💎 حرفه‌ای: <b>{pp:,}</b> تومان")
    if not any_plan:
        await cb.answer(
            "فعلاً هیچ پلنی برای فروش موجود نیست. با پشتیبانی همین ربات "
            "تماس بگیر.", show_alert=True)
        return
    lines.append(f"\n👛 موجودی کیف پول شما: <b>{int(bu.balance):,}</b> تومان")
    b.button(text="🔙 انصراف", callback_data=f"pu:bot:{bot_id}", style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@partner_user_router.callback_query(F.data.startswith("pu:tup:"))
async def pu_trial_upgrade_ask(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info or info[1] != "trial":
        await cb.answer("این ربات در حالت تست نیست.", show_alert=True)
        return
    await _plan_pick_screen(cb, row, pb, bu, bot_id, "tup")


@partner_user_router.callback_query(F.data.startswith("pu:ren:"))
async def pu_renew_ask(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info or info[1] == "trial":
        await cb.answer("این ربات قابل تمدید نیست.", show_alert=True)
        return
    await _plan_pick_screen(cb, row, pb, bu, bot_id, "ren")


async def _do_plan_purchase(cb: CallbackQuery, bot: Bot, mode: str):
    """اجرایِ واقعیِ ارتقا (tup) یا تمدید (ren) — همه در یک تراکنش."""
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok
    parts = cb.data.split(":")
    bot_id, want_plan = int(parts[2]), parts[3]
    if want_plan not in ("growth", "pro"):
        await cb.answer("پلن نامعتبر", show_alert=True)
        return
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    is_trial = info[1] == "trial"
    if mode == "tup" and not is_trial:
        await cb.answer("این ربات در حالت تست نیست.", show_alert=True)
        return
    if mode == "ren" and is_trial:
        await cb.answer("رباتِ تست تمدید نمی‌شود؛ اول ارتقا بده.", show_alert=True)
        return

    price = await _sale_price(row.id, want_plan)
    if price <= 0:
        await cb.answer("قیمتِ این پلن تنظیم نشده است.", show_alert=True)
        return

    await cb.answer("در حال انجام…")
    async with Session() as s:
        pbr = await s.get(PartnerBot, pb.id)
        stock = pbr.stock_growth if want_plan == "growth" else pbr.stock_pro
        if stock <= 0:
            await cb.answer("موجودی این پلن تمام شد.", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id,
            BotUser.telegram_id == bu.telegram_id).with_for_update())
        if not u or float(u.balance) < price:
            await cb.answer("موجودی کیف پولت کافی نیست.", show_alert=True)
            return

        br = await s.get(BotRow, bot_id)
        if not br:
            await cb.answer("ربات پیدا نشد.", show_alert=True)
            return

        u.balance = float(u.balance) - price
        if want_plan == "growth":
            pbr.stock_growth -= 1
        else:
            pbr.stock_pro -= 1
        # === PATCH_PARTNER_STOCK_LOG_V1 ===
        await log_stock(
            s, pbr.id, want_plan, -1,
            "upgrade_from_trial" if mode == "tup" else "renew",
            ("ارتقای ربات تست" if mode == "tup" else "تمدید ربات")
            + f" (@{br.username})",
            pbr.stock_growth if want_plan == "growth" else pbr.stock_pro)

        now = datetime.datetime.now()
        if mode == "tup":
            # تست → پلنِ واقعی: مهلت از همین لحظه، نه از بقیهٔ تست.
            new_exp = now + datetime.timedelta(days=SOLD_BOT_DAYS)
            note = f"ارتقای @{br.username} از تست به {want_plan}"
            section = "ارتقای ربات تست (همکاری)"
        else:
            # تمدید: اگر هنوز اعتبار دارد، از انتهای همان؛ وگرنه از حالا.
            base = br.plan_expires_at if (br.plan_expires_at and
                                          br.plan_expires_at > now) else now
            new_exp = base + datetime.timedelta(days=SOLD_BOT_DAYS)
            note = f"تمدید @{br.username} ({want_plan})"
            section = "تمدید ربات (همکاری)"
        br.plan = want_plan
        br.plan_expires_at = new_exp
        br.status = "active"

        ps = await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb.id,
            PartnerSale.sold_bot_id == bot_id))
        if ps:
            ps.plan = want_plan
            ps.price = float(ps.price or 0) + price
            if mode == "tup":
                ps.kind = "sold"   # دیگر تست نیست (مقدارِ پیش‌فرضِ مدل)
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-price,
                        kind="purchase", note=note))
        uname = br.username
        new_bal = float(u.balance)
        tok = br.token
        owner = pbr.owner_id
        await s.commit()

    try:
        from tenant import _purchase_alert
        await _purchase_alert(bot, row, buyer=bu, section=section,
                              detail=f"@{uname} → {_PLAN_FA.get(want_plan, want_plan)}",
                              amount=price, count=1)
    except Exception:
        logger.exception("گزارشِ خریدِ ارتقا/تمدید ناموفق بود")
    try:
        from tenant.partner_bot.partner_sales import invalidate_pb_cache
        invalidate_pb_cache(row.id)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ PartnerBot ناموفق بود")
    try:
        from tenant import invalidate_bot_row_cache
        invalidate_bot_row_cache(tok)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ ربات ناموفق بود")
    try:
        from tenant_status_bus import publish_tenant_invalidate
        await publish_tenant_invalidate(tok)
    except Exception:
        logger.exception("انتشارِ سیگنالِ باطل‌سازی ناموفق بود")

    _kb = InlineKeyboardBuilder()
    _kb.button(text="🤖 ربات‌های من", callback_data="pu:bots", style=ButtonStyle.SUCCESS)
    _kb.adjust(1)
    head = ("✅ ربات با موفقیت ارتقا یافت و فعال شد."
            if mode == "tup" else "✅ ربات تمدید شد.")
    await cb.message.edit_text(
        f"{head}\n\n"
        f"🤖 @{uname or '—'}\n"
        f"📦 پلن: {_PLAN_FA.get(want_plan, want_plan)}\n"
        f"⏳ اعتبار تا: {jalali(new_exp)}\n"
        f"💵 مبلغ: {price:,} تومان\n"
        f"👛 موجودی جدید: {int(new_bal):,} تومان\n\n"
        + ("ℹ️ همهٔ دکمه‌ها و تنظیماتِ قبلیِ رباتت دست‌نخورده باقی مانده."
           if mode == "tup" else ""),
        reply_markup=_kb.as_markup())
    try:
        await bot.send_message(
            owner,
            ("⬆️ <b>ارتقای ربات تست</b>" if mode == "tup" else "🔄 <b>تمدید ربات</b>")
            + f"\n\n👤 کاربر: <code>{bu.telegram_id}</code>\n"
              f"🤖 ربات: @{uname or '—'}\n"
              f"📦 پلن: {_PLAN_FA.get(want_plan, want_plan)}\n"
              f"💵 مبلغ: {price:,} تومان")
    except Exception:
        logger.exception("اطلاعِ ارتقا/تمدید به صاحبِ رباتِ همکار ناموفق بود")


@partner_user_router.callback_query(F.data.startswith("pu:tupok:"))
async def pu_trial_upgrade_do(cb: CallbackQuery, bot: Bot):
    await _do_plan_purchase(cb, bot, "tup")


@partner_user_router.callback_query(F.data.startswith("pu:renok:"))
async def pu_renew_do(cb: CallbackQuery, bot: Bot):
    await _do_plan_purchase(cb, bot, "ren")


@partner_user_router.callback_query(F.data.startswith("pu:up:"))
async def pu_upgrade_ask(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info or info[3] != "growth" or info[1] == "trial":
        await cb.answer("این ربات قابل ارتقا نیست.", show_alert=True)
        return

    if pb.stock_pro <= 0:
        await cb.answer(
            "موجودی پلن حرفه‌ای تمام شده. بعداً دوباره تلاش کن.",
            show_alert=True)
        return

    pg = await _sale_price(row.id, "growth")
    pp = await _sale_price(row.id, "pro")
    if pp <= 0:
        await cb.answer("قیمت پلن حرفه‌ای تنظیم نشده است.", show_alert=True)
        return
    diff = max(0, pp - pg)
    if diff <= 0:   # === PATCH_PARTNER_V32 ===
        await cb.answer(
            "ارتقا در حال حاضر ممکن نیست (قیمت‌گذاری فروشنده نامعتبر است).",
            show_alert=True)
        return

    b = InlineKeyboardBuilder()
    b.button(text=f"✅ پرداخت {diff:,} تومان", callback_data=f"pu:upok:{bot_id}",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 انصراف", callback_data=f"pu:bot:{bot_id}",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "⬆️ <b>ارتقا به حرفه‌ای</b>\n\n"
        f"قیمت حرفه‌ای: {pp:,} تومان\n"
        f"قیمت پرداختیِ قبلی: {pg:,} تومان\n"
        f"💰 مابه‌التفاوت: <b>{diff:,} تومان</b>\n\n"
        f"👛 موجودی شما: {int(bu.balance):,} تومان\n\n"
        "ℹ️ تاریخ انقضای ربات تغییر نمی‌کند.",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_user_router.callback_query(F.data.startswith("pu:upok:"))
async def pu_upgrade_do(cb: CallbackQuery, bot: Bot):
    ok = await _ctx(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb, bu = ok
    bot_id = int(cb.data.split(":")[2])
    info = await _owns(pb.id, bu.telegram_id, bot_id)
    if not info or info[3] != "growth" or info[1] == "trial":
        await cb.answer("این ربات قابل ارتقا نیست.", show_alert=True)
        return

    pg = await _sale_price(row.id, "growth")
    pp = await _sale_price(row.id, "pro")
    diff = max(0, pp - pg)

    # === PATCH_PARTNER_V32 ===
    # اگر فروشنده قیمتِ حرفه‌ای را برابر یا کمتر از رشد گذاشته باشد،
    # diff صفر می‌شود و ارتقا *رایگان* می‌شد — یعنی یک رباتِ حرفه‌ای
    # مجانی از انبارِ همکار می‌رفت. تا قیمت درست نشود، ارتقا نداریم.
    if diff <= 0:
        await cb.answer(
            "ارتقا در حال حاضر ممکن نیست (قیمت‌گذاری فروشنده نامعتبر است). "
            "با پشتیبانی همین ربات تماس بگیر.", show_alert=True)
        return

    async with Session() as s:
        # همه‌چیز دوباره در همین تراکنش چک می‌شود: بین دیدنِ صفحه و زدنِ
        # دکمه ممکن است موجودیِ انبار یا کیفِ پول عوض شده باشد.
        pbr = await s.get(PartnerBot, pb.id)
        if pbr.stock_pro <= 0:
            await cb.answer("موجودی حرفه‌ای تمام شد.", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id,
            BotUser.telegram_id == bu.telegram_id).with_for_update())
        if not u or float(u.balance) < diff:
            await cb.answer("موجودی کیف پولت کافی نیست.", show_alert=True)
            return

        u.balance = float(u.balance) - diff
        pbr.stock_pro -= 1
        # === PATCH_PARTNER_UPGRADE_REFUND_GROWTH_V1 ===
        # این ربات دیگر «رشد» نیست، پس واحدِ رشدی که موقعِ فروش مصرف شده
        # بود آزاد می‌شود و به انبار برمی‌گردد. قبلاً برنمی‌گشت و همکار
        # می‌دید انبارش دو واحد کم شده در حالی که فقط یک ربات فروخته بود.
        pbr.stock_growth += 1
        br = await s.get(BotRow, bot_id)
        _uname_for_log = br.username if br else ""
        await log_stock(s, pbr.id, "pro", -1, "upgrade",
                        f"ارتقای @{_uname_for_log} از رشد به حرفه‌ای",
                        pbr.stock_pro)
        await log_stock(s, pbr.id, "growth", +1, "upgrade",
                        f"آزادشدنِ واحدِ رشد بابتِ ارتقای @{_uname_for_log}",
                        pbr.stock_growth)
        br.plan = "pro"
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-diff,
                        kind="purchase",
                        note=f"ارتقای @{br.username} به حرفه‌ای"))
        ps = await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb.id,
            PartnerSale.sold_bot_id == bot_id))
        if ps:
            ps.plan = "pro"
            ps.price = float(ps.price or 0) + diff
        uname = br.username
        new_bal = float(u.balance)
        upgraded_token = br.token
        owner = pbr.owner_id
        await s.commit()

    # === PATCH_PARTNER_UPGRADE_ALERT_V1 ===
    # ارتقا به «حرفه‌ای» یک فروشِ واقعی است (پول از کیف پول کم می‌شود و از
    # انبارِ همکار یکی برداشته می‌شود) ولی به قلابِ مشترکِ گزارشِ خرید وصل
    # نبود؛ پس نه در «کانال اعتمادسازی» پست می‌شد و نه «اطلاع خودکار خرید»
    # برایِ همکار می‌آمد. همان اشکالی که قبلاً فروشِ ربات و ربات‌هایِ SMM
    # داشتند.
    try:
        from tenant import _purchase_alert
        await _purchase_alert(bot, row, buyer=bu,
                              section="ارتقای ربات (همکاری)",
                              detail=f"@{uname} → 💎 حرفه‌ای",
                              amount=diff, count=1)
    except Exception:
        logger.exception("گزارشِ خریدِ ارتقا ناموفق بود")

    # === PATCH_PARTNER_V27 ===
    try:
        from tenant.partner_bot.partner_sales import invalidate_pb_cache
        invalidate_pb_cache(row.id)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ PartnerBot ناموفق بود")

    # === PATCH_PARTNER_V32 ===
    # کشِ ردیفِ *رباتِ ارتقایافته* هم باید پاک شود، وگرنه تا ۲۰ ثانیه
    # هنوز plan="growth" دیده می‌شود و فیچرهایِ حرفه‌ای کار نمی‌کنند.
    try:
        from tenant import invalidate_bot_row_cache
        invalidate_bot_row_cache(upgraded_token)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ رباتِ ارتقایافته ناموفق بود")
    try:
        from tenant_status_bus import publish_tenant_invalidate
        await publish_tenant_invalidate(upgraded_token)
    except Exception:
        logger.exception("انتشارِ سیگنالِ باطل‌سازی ناموفق بود")

    await cb.answer("ارتقا انجام شد")
    # === PATCH_PARTNER_V32 === دکمهٔ بازگشت (قبلاً صفحه بن‌بست بود)
    _kb = InlineKeyboardBuilder()
    _kb.button(text="🤖 ربات‌های من", callback_data="pu:bots",
               style=ButtonStyle.SUCCESS)
    _kb.adjust(1)
    await cb.message.edit_text(
        f"✅ ربات @{uname or '—'} به پلن <b>حرفه‌ای</b> ارتقا یافت.\n\n"
        f"💵 مبلغ {diff:,} تومان از کیف پولت کسر شد.\n"
        f"👛 موجودی جدید: {int(new_bal):,} تومان\n\n"
        "ℹ️ تاریخ انقضای ربات تغییر نکرده است.",
        reply_markup=_kb.as_markup())
    try:
        await bot.send_message(
            owner,
            f"⬆️ <b>ارتقای پلن</b>\n\n"
            f"👤 کاربر: <code>{bu.telegram_id}</code>\n"
            f"🤖 ربات: @{uname or '—'}\n"
            f"💵 مبلغ: {diff:,} تومان")
    except Exception:
        logger.exception("اطلاعِ ارتقا به صاحبِ رباتِ همکار ناموفق بود")
