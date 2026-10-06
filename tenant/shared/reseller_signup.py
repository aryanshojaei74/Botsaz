"""
reseller_signup.py — «پنلِ نمایندگی» به‌صورتِ خودسرویس
(=== PATCH_RESELLER_SELF_SIGNUP_V1 ===).

درخواستِ ادمین (ایلیا): کاربر خودش بتواند نمایندگی بگیرد، بدونِ اینکه
مالکِ ربات دستی برایش یوزر/پسورد بسازد.

فرقش با ماژول‌هایِ موجود:
  • tenant/vpn_partners.py = همکارِ *دستی*؛ ادمین آیدی را ثبت و یوزر/پسورد
    را خودش می‌سازد. متنِ فعلیِ کاربر هم همین را می‌گوید: «از مدیرِ ربات
    بخواه نام کاربری و رمز برایت بسازد».
  • tenant/reseller.py = پله‌هایِ *تخفیف* بر اساسِ شارژ/خرید.
  • این ماژول = فقط «درِ ورود»: شرطِ حداقلِ موجودی را چک می‌کند و اگر
    برقرار بود، همان VpnPartner و همان دسترسیِ پنلِ وب را خودکار می‌سازد.

عمداً هیچ منطقِ تخفیف یا قیمتی اینجا نیست: طبقِ خواستهٔ ادمین، درصدِ
تخفیف همان چیزی است که در «🏷 نمایندگی خودکار» تعریف شده. رکوردی که
اینجا ساخته می‌شود discount_percent=0 دارد و
vpn_partners.partner_discount_percent() بزرگ‌ترینِ «دستی» و «خودکار» را
برمی‌گرداند — پس پلهٔ خودکار خودش اثر می‌گذارد و اگر اینجا عددی می‌گذاشتیم
همان را خنثی می‌کرد.

حداقلِ موجودی *شرط* است نه هزینه: چیزی از کیفِ پول کم نمی‌شود.

تنظیمات (Setting، per-bot):
    rsl_signup_min_balance — حداقلِ موجودیِ لازم به تومان ("0" = بدونِ شرط)

«⚙️ ساخت کانفیگ» و «🎁 دریافت تست» عمداً اینجا نیستند: هر دو از قبل داخلِ
همان پنلِ تحتِ وب هستند، و صداکردنشان از یک callback نیازمندِ ساختنِ یک
Message جعلی است که from_userش خودِ ربات نباشد — وگرنه اکشن‌هایی که
msg.from_user.id را می‌خوانند سفارش را به‌نامِ ربات ثبت می‌کنند. تا وقتی
دقیقاً روشن نشده این دو دکمه باید به کدام پنل/پلن وصل شوند، نیمه‌کاره
اضافه‌کردنشان از نبودنشان بدتر است.

callbackها با پیشوند: rsp:
"""
from __future__ import annotations

import re as _re
import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)
from sqlalchemy import select

from config import Session
from models import BotUser, VpnPartner

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

reseller_signup_router = Router()


# ─────────────────────────── تنظیمات ───────────────────────────
async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return (await get_setting(bot_id, key, default)) or default


async def _set(bot_id: int, key: str, val: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, val)


async def min_balance(bot_id: int) -> int:
    raw = (await _get(bot_id, "rsl_signup_min_balance", "0")).strip()
    try:
        return max(0, int(float(raw.replace(",", ""))))
    except ValueError:
        # مقدارِ خراب نباید درِ نمایندگی را قفل کند؛ یعنی «بدونِ شرط».
        logger.warning("rsl_signup_min_balance نامعتبر برایِ bot %s: %r", bot_id, raw)
        return 0


# ─────────────────────── وضعیتِ کاربر ───────────────────────
async def _partner(bot_id: int, tg_id: int) -> VpnPartner | None:
    async with Session() as s:
        return await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.telegram_id == tg_id))


async def _balance(bot_id: int, tg_id: int) -> int:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    return int(float(u.balance)) if u else 0


def _has_panel(p: VpnPartner | None) -> bool:
    """نماینده‌ای که واقعاً پنلِ قابلِ استفاده دارد."""
    return bool(p and p.active and p.web_enabled and p.web_username)


# ─────────────────────── ساختِ خودکارِ دسترسی ───────────────────────
def _auto_username(bot_id: int, tg_id: int) -> str:
    """نامِ کاربریِ خودکار و بی‌تداخل.

    web_username در *کلِ سامانه* یکتاست (ورود بدونِ انتخابِ ربات انجام
    می‌شود)، نه به‌ازای هر ربات. اگر اجازه می‌دادیم کاربر خودش نام انتخاب
    کند، دو نفر در دو ربات مختلف سرِ یک نام دعوا می‌کردند و باید حلقهٔ
    «این نام گرفته شده» می‌ساختیم. ترکیبِ bot_id و آیدیِ تلگرام ذاتاً یکتا
    است و همین مشکل را از بین می‌برد.
    """
    return f"r{int(bot_id)}_{int(tg_id)}"


async def create_panel_access(bot_id: int, tg_id: int) -> tuple[str, str] | None:
    """VpnPartner و دسترسیِ پنلِ وب را می‌سازد و (یوزرنیم، پسوردِ خام)
    برمی‌گرداند. پسورد فقط همین یک‌بار در دسترس است — در دیتابیس هش
    می‌شود و هیچ‌جا دوباره قابلِ خواندن نیست."""
    from tenant.shared.web_panel import hash_password
    uname = _auto_username(bot_id, tg_id)
    if not _re.fullmatch(r"[A-Za-z0-9_]{3,32}", uname):
        logger.error("نامِ کاربریِ تولیدشده نامعتبر است: %r", uname)
        return None
    pw = secrets.token_urlsafe(9)
    async with Session() as s:
        clash = await s.scalar(select(VpnPartner).where(
            VpnPartner.web_username == uname,
            VpnPartner.telegram_id != tg_id))
        if clash:
            logger.error("تداخلِ نامِ کاربریِ نماینده: %s", uname)
            return None
        p = await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.telegram_id == tg_id))
        if not p:
            p = VpnPartner(
                bot_id=bot_id, telegram_id=tg_id,
                note="نمایندگیِ خودکار (ثبت‌نامِ خودِ کاربر)",
                # عمداً صفر — تخفیف از «🏷 نمایندگی خودکار» می‌آید.
                discount_percent=0,
                apply_all=True, apply_plans=True, apply_custom=True)
            s.add(p)
        p.active = True
        p.web_username = uname
        p.web_password_hash = hash_password(pw)
        p.web_enabled = True
        p.web_fail_count = 0
        p.web_locked_until = None
        await s.commit()
    return uname, pw


# ─────────────────────────── نمایش ───────────────────────────
async def _panel_url(bot_id: int) -> str:
    from tenant.gateways.custom_domain import get_bot_webhook_base
    from config import WEBHOOK_BASE as _WB
    base = (await get_bot_webhook_base(bot_id) or _WB or "").rstrip("/")
    return f"{base}/panel/r/login" if base.startswith("https://") else ""


async def render(bot_id: int, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبوردِ «پنلِ نمایندگی» متناسب با وضعیتِ همین کاربر."""
    p = await _partner(bot_id, tg_id)
    need = await min_balance(bot_id)
    have = await _balance(bot_id, tg_id)

    # ── حالتِ ۱: هنوز پنل ندارد ──
    if not _has_panel(p):
        rows = [
            [InlineKeyboardButton(text="💰 افزایش موجودی",
                                  callback_data="rsp:topup", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="🛠 ساخت پنل",
                                  callback_data="rsp:create", style=ButtonStyle.PRIMARY)],
        ]
        if need and have < need:
            txt = ("🏷 <b>پنلِ نمایندگی</b>\n\n"
                   f"برایِ فعال‌سازیِ نمایندگی باید موجودیِ کیفِ پولت "
                   f"حداقل <b>{need:,} تومان</b> باشد.\n"
                   f"موجودیِ فعلی‌ات: <b>{have:,} تومان</b>\n"
                   f"کسریِ باقی‌مانده: <b>{need - have:,} تومان</b>\n\n"
                   "این مبلغ فقط <b>شرط</b> است و از کیفِ پولت کم نمی‌شود.")
        else:
            txt = ("🏷 <b>پنلِ نمایندگی</b>\n\n"
                   f"موجودیِ فعلی‌ات: <b>{have:,} تومان</b>\n"
                   "شرطِ موجودی برقرار است ✅\n\n"
                   "با زدنِ «🛠 ساخت پنل»، نام کاربری و رمزِ ورودت ساخته "
                   "می‌شود و لینکِ پنل برایت می‌آید.")
        return txt, InlineKeyboardMarkup(inline_keyboard=rows)

    # ── حالتِ ۲: پنل دارد ──
    rows = []
    url = await _panel_url(bot_id)
    if url:
        rows.append([InlineKeyboardButton(text="🌐 ورود به پنل", web_app=WebAppInfo(url=url))])
    rows.append([InlineKeyboardButton(text="📋 کانفیگ‌های من",
                                      callback_data="rsme:cfgs", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="💰 کیف پول",
                                      callback_data="rsp:topup", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🔄 بروزرسانی",
                                      callback_data="rsp:home", style=ButtonStyle.PRIMARY)])

    warn = ""
    if need and have < need:
        # طبقِ خواستهٔ ادمین: پنل باطل نمی‌شود، فقط تا شارژِ دوباره
        # هشدار می‌گیرد و امکاناتِ خرید محدود می‌ماند.
        warn = (f"\n\n⚠️ موجودی‌ات زیرِ حدِ نمایندگی ({need:,} تومان) است. "
                "تا شارژِ دوباره، بخشی از امکاناتِ خرید برایت محدود می‌شود.")
    txt = ("🏷 <b>پنلِ نمایندگی</b>\n\n"
           f"👤 نام کاربری: <code>{p.web_username}</code>\n"
           f"💰 موجودی: <b>{have:,} تومان</b>\n\n"
           "در پنلِ تحتِ وب می‌توانی پلن بسازی، فروشِ حجم و روزِ دلخواه "
           "بزنی و تست بگیری." + warn)
    if not url:
        txt += "\n\n⚠️ آدرسِ امنِ پنل تنظیم نشده؛ به مدیرِ ربات بگو."
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def show(msg: Message, bot: Bot, bot_id: int) -> None:
    """ورودیِ اکشنِ دکمه (`reseller_panel`)."""
    txt, kb = await render(bot_id, msg.from_user.id)
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


# ─────────────────────────── هندلرها ───────────────────────────
async def _row(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


@reseller_signup_router.callback_query(F.data == "rsp:home")
async def rsp_home(cb: CallbackQuery, bot: Bot):
    row = await _row(cb, bot)
    if not row:
        await cb.answer()
        return
    txt, kb = await render(row.id, cb.from_user.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@reseller_signup_router.callback_query(F.data == "rsp:topup")
async def rsp_topup(cb: CallbackQuery, bot: Bot):
    row = await _row(cb, bot)
    if not row:
        await cb.answer()
        return
    from tenant import get_or_create_user, show_wallet
    u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name,
                                 cb.from_user.username or "")
    await cb.answer()
    await show_wallet(cb.message, row.id, u)


@reseller_signup_router.callback_query(F.data == "rsp:create")
async def rsp_create(cb: CallbackQuery, bot: Bot):
    row = await _row(cb, bot)
    if not row:
        await cb.answer()
        return
    tg_id = cb.from_user.id
    p = await _partner(row.id, tg_id)
    if _has_panel(p):
        await cb.answer("پنلت از قبل ساخته شده.")
        await rsp_home(cb, bot)
        return

    need = await min_balance(row.id)
    have = await _balance(row.id, tg_id)
    if need and have < need:
        await cb.answer(
            f"موجودی‌ات کافی نیست.\nلازم: {need:,} تومان\nموجودی: {have:,} تومان",
            show_alert=True)
        return

    made = await create_panel_access(row.id, tg_id)
    if not made:
        await cb.answer("ساختِ پنل ناموفق بود؛ به پشتیبانی اطلاع بده.", show_alert=True)
        return
    uname, pw = made
    url = await _panel_url(row.id)
    kb = None
    if url:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🌐 ورود به پنل", web_app=WebAppInfo(url=url))]])
    await cb.answer("✅ پنلت ساخته شد")
    await cb.message.answer(
        "✅ <b>نمایندگی‌ات فعال شد!</b>\n\n"
        f"👤 نام کاربری: <code>{uname}</code>\n"
        f"🔑 رمز عبور: <code>{pw}</code>\n\n"
        "⚠️ این رمز فقط همین یک‌بار نمایش داده می‌شود — جایی امن ذخیره‌اش کن.\n"
        "اگر گمش کردی، از پشتیبانی بخواه رمزِ جدید بسازد.",
        reply_markup=kb, parse_mode="HTML")
    txt, kb2 = await render(row.id, tg_id)
    await cb.message.answer(txt, reply_markup=kb2, parse_mode="HTML")
