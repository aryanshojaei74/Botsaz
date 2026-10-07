"""=== PATCH_MARKETPLACE_V1 ===

«فروشِ رباتِ اختصاصیِ آماده» — فروشگاهِ پروژه‌هایِ آمادهٔ بات‌ساز.

جریانِ کاربر:
    🛍 فروش ربات اختصاصی آماده
        └── پروژه (مثلاً «دانلودر توییتر»)
              ├── 🖼 تصاویر پروژه
              ├── 🧪 تستِ ۲۰ دقیقه‌ای (پیش از خرید)
              └── 🛒 خرید پروژه
                    ├── 📥 خرید و دریافتِ فایلِ پروژه
                    └── 🚀 خرید و فعال‌سازی رویِ VPSِ بات‌ساز

جریانِ مدیر (پنل ← 🛍 فروشگاهِ پروژه‌ها):
    • افزودن/ویرایش/حذفِ پروژه، آپلودِ zip و تصاویر، قیمت، مدتِ تست
    • 👥 خریدارانِ پروژه‌ها — چه خریده، موجودی‌اش، رویِ چه رباتی فعال
      کرده، و اگر رویِ VPS فعال کرده وضعیتِ همان اپ

فعال‌سازیِ VPS از راهِ همان APIِ ادمینِ سرویسِ میزبانی انجام می‌شود
(همان مسیری که ابزارهایِ ادمینِ اپ‌ها از آن استفاده می‌کنند)، نه دستکاریِ
مستقیمِ دیتابیس — چون ساختِ کانتینر و نوشتنِ فایل فقط از آنجا ممکن است.
"""
from __future__ import annotations

import datetime
import os
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
)
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from models import (
    HostedApp, MarketProject, MarketPurchase, PlatformTxn, PlatformUser,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

market_router = Router()

BTN_MARKET = "🛍 فروش ربات اختصاصی آماده"


# === PATCH_MARKET_PROVISION_V1 ===
# باید بالاترین‌جایِ فایل تعریف شود: هندلرهایِ ProvisionFSM در میانهٔ فایل
# دکوریت می‌شوند و دکوراتور در زمانِ ایمپورت اجرا می‌شود.
class ProvisionFSM(StatesGroup):
    token = State()
    admin_id = State()

# جاییکه zipِ پروژه‌ها نگه داشته می‌شود (خارج از پوشهٔ اپ‌ها)
PROJECT_DIR = os.getenv("MARKET_PROJECT_DIR", "/opt/botsaz/market_projects")


def _fa_money(n) -> int:
    try:
        return int(float(n or 0))
    except (TypeError, ValueError):
        return 0


# ─────────────────────────── کمکی‌ها ───────────────────────────

async def _project(pid: int) -> MarketProject | None:
    async with Session() as s:
        return await s.get(MarketProject, pid)


async def _active_projects() -> list[MarketProject]:
    async with Session() as s:
        return list((await s.scalars(
            select(MarketProject)
            .where(MarketProject.is_active.is_(True))
            .order_by(MarketProject.sort_order.asc(), MarketProject.id.desc())
        )).all())


def _images(p: MarketProject) -> list[str]:
    return [x for x in (p.image_ids or "").split(",") if x.strip()]


async def _has_bought(tg_id: int, pid: int) -> MarketPurchase | None:
    async with Session() as s:
        return await s.scalar(
            select(MarketPurchase).where(
                MarketPurchase.user_telegram_id == tg_id,
                MarketPurchase.project_id == pid,
                MarketPurchase.kind != "trial",
            ).order_by(MarketPurchase.id.desc()))


# هر خریدِ پولی فقط یک بار رویِ VPS نصب می‌شود (۳۰ روز میزبانی همراهش است).
# hosted_app_id=-1 یعنی «در حالِ نصب» — جلویِ دو نصبِ هم‌زمان با یک خرید را می‌گیرد.
_VPS_CLAIM_PENDING = -1


async def _vps_credit_available(tg_id: int, pid: int) -> bool:
    async with Session() as s:
        return bool(await s.scalar(select(MarketPurchase.id).where(
            MarketPurchase.user_telegram_id == tg_id,
            MarketPurchase.project_id == pid,
            MarketPurchase.kind != "trial",
            MarketPurchase.hosted_app_id.is_(None)).limit(1)))


async def _claim_vps_credit(tg_id: int, pid: int) -> int | None:
    async with Session() as s:
        pu = await s.scalar(select(MarketPurchase).where(
            MarketPurchase.user_telegram_id == tg_id,
            MarketPurchase.project_id == pid,
            MarketPurchase.kind != "trial",
            MarketPurchase.hosted_app_id.is_(None),
        ).order_by(MarketPurchase.id).limit(1).with_for_update(skip_locked=True))
        if not pu:
            return None
        pu.hosted_app_id = _VPS_CLAIM_PENDING
        await s.commit()
        return pu.id


async def _settle_vps_credit(purchase_id: int, app_id: int | None) -> None:
    """app_id=None یعنی نصب ناموفق بود و اعتبار برمی‌گردد."""
    async with Session() as s:
        pu = await s.get(MarketPurchase, purchase_id, with_for_update=True)
        if pu and pu.hosted_app_id == _VPS_CLAIM_PENDING:
            pu.hosted_app_id = app_id
            await s.commit()


# ─────────────────────── فروشگاه (کاربر) ───────────────────────

# === PATCH_MARKET_TERMS_V1 ===
_TERMS_TEXT = (
    "📜 <b>قوانین خرید ربات اختصاصی</b>\n\n"
    "قبل از ورود، لطفاً این چند بند را بخوان:\n\n"
    "1️⃣ <b>خرید قطعی است و مبلغ برگشت داده نمی‌شود.</b>\n"
    "پس از پرداخت، سورس/فایلِ پروژه در اختیارت قرار می‌گیرد و به دلیلِ "
    "ماهیتِ دیجیتالِ محصول، امکانِ عودتِ وجه وجود ندارد.\n\n"
    "2️⃣ <b>تست دقیقاً برای همین گذاشته شده.</b>\n"
    "روی پروژه‌هایی که تست دارند، یک نسخهٔ واقعی روی سرورِ ما برایت "
    "بالا می‌آید و می‌توانی کامل باهاش کار کنی. حتماً <u>قبل از واریز</u> "
    "تست بگیر و تصاویر پروژه را ببین.\n\n"
    "3️⃣ <b>خرید یعنی رضایت و آگاهی کامل.</b>\n"
    "با زدنِ دکمهٔ تأیید، می‌پذیری که با آگاهی از امکانات و محدودیت‌های "
    "پروژه خرید می‌کنی و بعداً بابتِ «انتظارِ متفاوت» درخواستِ عودت نداری.\n\n"
    "4️⃣ <b>پشتیبانی روی نصب و راه‌اندازی است</b>، نه توسعهٔ امکاناتِ جدید "
    "یا شخصی‌سازیِ رایگان.\n\n"
    "5️⃣ <b>بازفروشِ سورس مجاز نیست.</b> پروژه برای استفادهٔ خودت است.\n\n"
    "6️⃣ مسئولیتِ محتوا و نحوهٔ استفاده از رباتِ خریداری‌شده با خودِ توست.\n\n"
    "اگر با موارد بالا موافقی، دکمهٔ زیر را بزن 👇"
)


def _terms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ قوانین را می‌پذیرم و ادامه می‌دهم",
                              callback_data="mk:agree", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🏠 بازگشت به خانه", callback_data="mk:exit",
                              style=ButtonStyle.DANGER)],
    ])


async def show_market(msg: Message, skip_terms: bool = False) -> None:
    """صفحهٔ اصلیِ فروشگاه — از دکمهٔ صفحهٔ اصلی صدا زده می‌شود.

    اولین ورودِ هر کاربر، صفحهٔ قوانین را می‌بیند. پذیرش یک‌بار ذخیره
    می‌شود (Setting سطحِ پلتفرم) تا هر بار جلویِ راهش سبز نشود، ولی
    رکوردش برایِ اختلافاتِ احتمالی باقی می‌ماند.
    """
    projects = await _active_projects()
    if not projects:
        await msg.answer("🛍 فعلاً پروژه‌ای برایِ فروش ثبت نشده. بعداً سر بزن.")
        return
    if not skip_terms and not await _terms_accepted(msg.chat.id):
        await msg.answer(_TERMS_TEXT, reply_markup=_terms_kb())
        return
    rows = [[InlineKeyboardButton(
        text=f"🤖 {p.title} — {_fa_money(p.price):,} تومان",
        callback_data=f"mk:p:{p.id}", style=ButtonStyle.SUCCESS)] for p in projects]
    await msg.answer(
        "🛍 <b>ربات‌های اختصاصیِ آماده</b>\n\n"
        "پروژه‌هایِ آماده‌ای که می‌توانی فایلش را بگیری و خودت اجرا کنی، "
        "یا همین‌جا رویِ سرورِ اختصاصیِ بات‌ساز برایت فعال کنیم.\n\n"
        "رویِ هر پروژه بزن تا جزئیاتش را ببینی 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _project_kb(p: MarketProject, bought: bool) -> InlineKeyboardMarkup:
    rows = []
    if _images(p):
        rows.append([InlineKeyboardButton(
            text="🖼 تصاویر پروژه", callback_data=f"mk:img:{p.id}",
            style=ButtonStyle.PRIMARY)])
    if p.trial_minutes and not bought:
        rows.append([InlineKeyboardButton(
            text=f"🧪 تستِ {p.trial_minutes} دقیقه‌ای (رایگان)",
            callback_data=f"mk:trial:{p.id}", style=ButtonStyle.PRIMARY)])
    if bought:
        rows.append([InlineKeyboardButton(
            text="📥 دریافتِ دوبارهٔ فایل", callback_data=f"mk:dl:{p.id}",
            style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(
            text="🚀 فعال‌سازی رویِ VPSِ بات‌ساز", callback_data=f"mk:vps:{p.id}",
            style=ButtonStyle.SUCCESS)])
    else:
        rows.append([InlineKeyboardButton(
            text=f"🛒 خرید پروژه — {_fa_money(p.price):,} تومان",
            callback_data=f"mk:buy:{p.id}", style=ButtonStyle.SUCCESS)])
    # === PATCH_MARKET_TERMS_V1 ===
    rows.append([InlineKeyboardButton(text="📜 قوانین خرید", callback_data="mk:terms")])
    rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت به فروشگاه", callback_data="mk:home",
        style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _project_text(p: MarketProject, bought: bool) -> str:
    txt = f"🤖 <b>{p.title}</b>\n\n"
    if p.description:
        txt += p.description + "\n\n"
    txt += f"💵 قیمت: <b>{_fa_money(p.price):,} تومان</b>\n"
    if p.trial_minutes:
        txt += f"🧪 تستِ رایگان: {p.trial_minutes} دقیقه\n"
    if bought:
        txt += "\n✅ این پروژه را قبلاً خریده‌ای."
    return txt


# === PATCH_MARKET_TERMS_V1 ===
async def _terms_accepted(tg_id: int) -> bool:
    from platform_bot import get_pset
    return (await get_pset(f"mk_terms:{tg_id}", "")) == "1"


@market_router.callback_query(F.data == "mk:agree")
async def mk_agree(cb: CallbackQuery):
    from platform_bot import set_pset
    await set_pset(f"mk_terms:{cb.from_user.id}",  "1")
    await cb.answer("✅ ثبت شد.")
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await show_market(cb.message, skip_terms=True)


@market_router.callback_query(F.data == "mk:exit")
async def mk_exit(cb: CallbackQuery):
    from platform_bot import smart_home_kb
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("🏠 به منوی اصلی برگشتی.",
                            reply_markup=await smart_home_kb(cb.from_user.id))
    await cb.answer()


@market_router.callback_query(F.data == "mk:terms")
async def mk_terms_show(cb: CallbackQuery):
    await cb.message.answer(_TERMS_TEXT)
    await cb.answer()


@market_router.callback_query(F.data == "mk:home")
async def mk_home(cb: CallbackQuery):
    projects = await _active_projects()
    rows = [[InlineKeyboardButton(
        text=f"🤖 {p.title} — {_fa_money(p.price):,} تومان",
        callback_data=f"mk:p:{p.id}", style=ButtonStyle.SUCCESS)] for p in projects]
    try:
        await cb.message.edit_text(
            "🛍 <b>ربات‌های اختصاصیِ آماده</b>\n\nرویِ هر پروژه بزن 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


@market_router.callback_query(F.data.startswith("mk:p:"))
async def mk_project(cb: CallbackQuery):
    p = await _project(int(cb.data.split(":")[2]))
    if not p or not p.is_active:
        await cb.answer("این پروژه دیگر موجود نیست.", show_alert=True)
        return
    bought = bool(await _has_bought(cb.from_user.id, p.id))
    try:
        await cb.message.edit_text(_project_text(p, bought), reply_markup=_project_kb(p, bought))
    except Exception:
        await cb.message.answer(_project_text(p, bought), reply_markup=_project_kb(p, bought))
    await cb.answer()


@market_router.callback_query(F.data.startswith("mk:img:"))
async def mk_images(cb: CallbackQuery, bot: Bot):
    p = await _project(int(cb.data.split(":")[2]))
    if not p:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    imgs = _images(p)
    if not imgs:
        await cb.answer("تصویری ثبت نشده.", show_alert=True)
        return
    await cb.answer("در حال ارسال تصاویر…")
    # === PATCH_MARKET_ALBUM_V1 ===
    # آلبوم به‌جایِ چند پیامِ جدا: تلگرام هر media_group را حداکثر ۱۰ تایی
    # می‌پذیرد و کپشن فقط رویِ عضوِ اول دیده می‌شود.
    caption = f"🖼 تصاویرِ «{p.title}»"
    group = [InputMediaPhoto(media=fid,
                             caption=(caption if i == 0 else None))
             for i, fid in enumerate(imgs[:10])]
    try:
        if len(group) == 1:
            await bot.send_photo(cb.from_user.id, imgs[0], caption=caption)
        else:
            await bot.send_media_group(cb.from_user.id, media=group)
    except Exception:
        logger.exception("ارسالِ آلبومِ تصاویر ناموفق بود؛ تک‌تک می‌فرستیم")
        # اگر یکی از file_idها خراب باشد کلِ آلبوم رد می‌شود، پس
        # به‌عنوانِ پشتیبان تک‌تک می‌فرستیم تا کاربر دست‌خالی نماند.
        for fid in imgs[:10]:
            try:
                await bot.send_photo(cb.from_user.id, fid)
            except Exception:
                logger.exception("ارسالِ تصویرِ پروژه ناموفق بود")


# ─────────────────────────── خرید ───────────────────────────

@market_router.callback_query(F.data.startswith("mk:buy:"))
async def mk_buy_menu(cb: CallbackQuery):
    p = await _project(int(cb.data.split(":")[2]))
    if not p or not p.is_active:
        await cb.answer("این پروژه دیگر موجود نیست.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🛒 <b>خرید «{p.title}»</b>\n\n"
        f"💵 مبلغ: <b>{_fa_money(p.price):,} تومان</b> (از کیفِ پول کم می‌شود)\n\n"
        "یکی از دو حالت را انتخاب کن:\n\n"
        "📥 <b>خرید و دریافتِ پروژه</b>\n"
        "فایلِ کاملِ پروژه برایت فرستاده می‌شود؛ خودت هرجا خواستی اجرا کن.\n\n"
        "🚀 <b>خرید و فعال‌سازی رویِ VPSِ اختصاصیِ بات‌ساز</b>\n"
        "پروژه رویِ سرورِ ما نصب و اجرا می‌شود؛ نیازی به سرورِ شخصی نداری.\n\n"
        # === PATCH_MARKET_TERMS_V1 ===
        # یادآوریِ صریح دقیقاً در لحظهٔ پرداخت — نه فقط در صفحهٔ اول.
        "⚠️ <b>یادآوری:</b> با پرداخت، خرید <b>قطعی</b> است و مبلغ "
        "برگشت داده نمی‌شود. اگر هنوز تست نگرفته‌ای، اول تست بگیر."
        + ("" if not p.trial_minutes else
           f" (تستِ {p.trial_minutes} دقیقه‌ای رایگان است.)"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 خرید و دریافتِ پروژه",
                                  callback_data=f"mk:pay:download:{p.id}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="🚀 خرید و فعال‌سازی رویِ VPS بات‌ساز",
                                  callback_data=f"mk:pay:vps:{p.id}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"mk:p:{p.id}",
                                  style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


@market_router.callback_query(F.data.startswith("mk:pay:"))
async def mk_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        _, _, kind, pid_raw = cb.data.split(":")
        pid = int(pid_raw)
    except ValueError:
        await cb.answer()
        return
    if kind not in ("download", "vps"):
        await cb.answer()
        return
    p = await _project(pid)
    if not p or not p.is_active:
        await cb.answer("این پروژه دیگر موجود نیست.", show_alert=True)
        return
    price = _fa_money(p.price)

    already = await _has_bought(cb.from_user.id, pid)
    # === PATCH_BIG_CHARGE_APPROVAL_V1 ===
    # پروژه‌هایِ گران هم مثلِ شارژِ سنگین اول هماهنگی می‌خواهند.
    if not already:
        from platform_bot import _big_charge_gate
        if await _big_charge_gate(cb.message, state, price,
                                  kind=f"market:{p.id}", subject=p.title):
            return
    if not already:
        # کسرِ موجودی با قفلِ ردیف تا دوبار کسر نشود
        from platform_bot import get_user
        async with Session() as s:
            u = await get_user(s, cb.from_user.id, cb.from_user.full_name,
                               for_update=True, username=cb.from_user.username)
            # بازبینی زیرِ قفل: دو کلیکِ هم‌زمان دو بار پول کم نکند
            dup = await s.scalar(select(MarketPurchase.id).where(
                MarketPurchase.user_telegram_id == cb.from_user.id,
                MarketPurchase.project_id == pid,
                MarketPurchase.kind != "trial").limit(1))
            if dup:
                await cb.answer("این پروژه را قبلاً خریده‌ای.", show_alert=True)
                return
            if _fa_money(u.balance) < price:
                await cb.answer(
                    f"❌ موجودیِ کیفِ پولت کافی نیست.\n"
                    f"لازم: {price:,} تومان — موجودی: {_fa_money(u.balance):,} تومان",
                    show_alert=True)
                return
            u.balance = _fa_money(u.balance) - price
            s.add(MarketPurchase(project_id=pid, user_telegram_id=cb.from_user.id,
                                 kind=kind, price_paid=price))
            s.add(PlatformTxn(user_telegram_id=cb.from_user.id, amount=-float(price),
                              kind="purchase", note=f"خریدِ پروژه: {p.title[:60]}"))
            await s.commit()
        await cb.answer("✅ پرداخت انجام شد.")
        try:
            await bot.send_message(
                PLATFORM_ADMIN_ID,
                f"🛍 <b>خریدِ پروژه</b>\n\n"
                f"👤 <code>{cb.from_user.id}</code>\n"
                f"📦 {p.title}\n💵 {price:,} تومان\n"
                f"🔧 حالت: {'دریافتِ فایل' if kind == 'download' else 'فعال‌سازی رویِ VPS'}")
        except Exception:
            logger.exception("اطلاع به مدیر ناموفق بود")
    else:
        await cb.answer()

    if kind == "download":
        await _send_project_file(cb, bot, p)
    else:
        # === PATCH_MARKET_PROVISION_V1 ===
        if not await _vps_credit_available(cb.from_user.id, pid):
            await bot.send_message(cb.from_user.id, _VPS_USED_MSG)
            return
        await _vps_start(cb, p, state, trial=False)


async def _send_project_file(cb: CallbackQuery, bot: Bot, p: MarketProject) -> None:
    if not p.zip_file_id and not p.zip_path:
        await bot.send_message(
            cb.from_user.id,
            "⚠️ فایلِ این پروژه هنوز آپلود نشده. با پشتیبانی تماس بگیر؛ "
            "خریدت ثبت شده و از دست نمی‌رود.")
        return
    try:
        if p.zip_file_id:
            await bot.send_document(cb.from_user.id, p.zip_file_id,
                                    caption=f"📦 <b>{p.title}</b>\n\nموفق باشی 🌱")
        else:
            with open(p.zip_path, "rb") as fh:
                await bot.send_document(
                    cb.from_user.id,
                    BufferedInputFile(fh.read(), filename=f"{p.title}.zip"),
                    caption=f"📦 <b>{p.title}</b>")
    except Exception:
        logger.exception("ارسالِ فایلِ پروژه ناموفق بود")
        await bot.send_message(cb.from_user.id,
                               "⚠️ ارسالِ فایل ناموفق بود؛ به پشتیبانی پیام بده.")


@market_router.callback_query(F.data.startswith("mk:dl:"))
async def mk_download_again(cb: CallbackQuery, bot: Bot):
    p = await _project(int(cb.data.split(":")[2]))
    if not p or not await _has_bought(cb.from_user.id, p.id):
        await cb.answer("اول باید پروژه را بخری.", show_alert=True)
        return
    await cb.answer("در حال ارسال…")
    await _send_project_file(cb, bot, p)


# ───────────────── فعال‌سازی رویِ VPS بات‌ساز ─────────────────

# === PATCH_MARKET_PROVISION_V1 ===
async def _vps_start(cb: CallbackQuery, p: MarketProject,
                     state: FSMContext | None = None, trial: bool = False) -> None:
    """شروعِ فعال‌سازیِ خودکار رویِ VPSِ بات‌ساز.

    اول توکنِ ربات و آیدیِ مدیر را می‌گیریم، بعد پروژه را روی سرور
    می‌سازیم. بدونِ توکن، رباتِ آپلودشده بالا نمی‌آید و کاربر فکر می‌کند
    خرابیِ ماست.
    """
    if state is None:
        await cb.message.answer("⚠️ خطای داخلی؛ دوباره از فروشگاه امتحان کن.")
        return
    await state.set_state(ProvisionFSM.token)
    await state.update_data(mkp_pid=p.id, mkp_trial=trial)
    await cb.message.answer(
        f"🚀 <b>فعال‌سازیِ «{p.title}» رویِ سرورِ اختصاصیِ بات‌ساز</b>\n\n"
        + ("🧪 این یک نسخهٔ آزمایشی است و بعد از "
           f"{p.trial_minutes} دقیقه خاموش می‌شود.\n\n" if trial else "")
        + "۱️⃣ <b>توکنِ رباتت</b> را بفرست.\n"
        "توکن را از @BotFather بگیر — چیزی شبیهِ:\n"
        "<code>123456789:AAE...</code>")


async def _provision(bot: Bot, user_id: int, p: MarketProject,
                     token: str, admin_id: int, trial: bool) -> None:
    """ساختِ اپ رویِ سرویسِ میزبانی و ثبتِ خرید."""

    # نامِ یکتا برایِ اپ: نامِ پروژه + آیدیِ کاربر
    base = re.sub(r"[^a-z0-9]+", "", (p.title or "bot").lower())[:12] or "bot"
    name = f"{base}{user_id % 100000}"

    if not p.zip_file_id:
        await bot.send_message(user_id,
            "⚠️ فایلِ این پروژه هنوز آپلود نشده. به پشتیبانی پیام بده.")
        return

    claim_id = None
    if trial:
        async with Session() as s:
            if await s.scalar(select(MarketPurchase.id).where(
                    MarketPurchase.user_telegram_id == user_id,
                    MarketPurchase.project_id == p.id,
                    MarketPurchase.kind == "trial").limit(1)):
                await bot.send_message(user_id, "قبلاً تستِ این پروژه را گرفته‌ای.")
                return
    else:
        claim_id = await _claim_vps_credit(user_id, p.id)
        if not claim_id:
            await bot.send_message(user_id, _VPS_USED_MSG)
            return
    try:
        await _provision_body(bot, user_id, p, token, admin_id, trial, name, claim_id)
    finally:
        if claim_id:
            # اگر نصب به هر دلیلی ثبت نشد، اعتبارِ خرید برمی‌گردد
            await _settle_vps_credit(claim_id, None)


_VPS_USED_MSG = ("⚠️ نصبِ رویِ VPS برایِ این خرید قبلاً انجام شده. برایِ تمدید یا "
                 "مدیریتِ اپ از بخشِ میزبانی استفاده کن یا تیکت بزن.")


async def _provision_body(bot: Bot, user_id: int, p: MarketProject, token: str,
                          admin_id: int, trial: bool, name: str,
                          claim_id: int | None) -> None:
    from platform_bot import _vip_api
    await bot.send_message(user_id, "⏳ در حال نصبِ پروژه رویِ سرور… (تا یک دقیقه)")

    # دانلودِ zip از تلگرام
    try:
        f = await bot.get_file(p.zip_file_id)
        buf = await bot.download_file(f.file_path)
        data = buf.read()
    except Exception:
        logger.exception("دانلودِ zipِ پروژه ناموفق بود")
        await bot.send_message(user_id, "⚠️ دریافتِ فایلِ پروژه ناموفق بود؛ تیکت بزن.")
        return

    import json as _json
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("owner_id", str(user_id))
    form.add_field("name", name)
# === PATCH_MARKET_ENV_KEYS_V1 ===
    # نام‌هایِ رایج را همه با هم ست می‌کنیم؛ پروژه‌ها سلیقه‌ایِ مختلف
    # دارند و اگر یکی از قلم بیفتد ربات بالا نمی‌آید (مثلاً OWNER_ID که
    # نبودش باعثِ حلقهٔ کرش شد).
    env_map = {
        "BOT_TOKEN": token, "TOKEN": token, "TELEGRAM_TOKEN": token,
        "TELEGRAM_BOT_TOKEN": token, "API_TOKEN": token,
        "ADMIN_ID": str(admin_id), "ADMIN_IDS": str(admin_id),
        "OWNER_ID": str(admin_id), "OWNER_IDS": str(admin_id),
        "ADMIN": str(admin_id), "SUDO_ID": str(admin_id),
        "SUDO_USERS": str(admin_id),
    }
    # متغیرهایِ اختصاصیِ همین پروژه (تعیین‌شده در پنلِ مدیریت) آخر اعمال
    # می‌شوند تا بتوانند مقادیرِ بالا را هم بازنویسی کنند.
    for line in (p.env_extra or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k:
            env_map[k] = (v.strip()
                          .replace("{token}", token)
                          .replace("{admin}", str(admin_id)))
    form.add_field("env", _json.dumps(env_map))
    if trial:
        form.add_field("trial_minutes", str(p.trial_minutes or 20))
    else:
        form.add_field("paid_days", "30")
    form.add_field("file", data, filename=f"{name}.zip",
                   content_type="application/zip")

    ok, res = await _vip_api("POST", "/api/hosting/admin/provision", data=form)
    if not ok:
        await bot.send_message(user_id, f"⚠️ نصب ناموفق بود:\n{res}\n\nتیکت بزن تا دستی انجامش دهیم.")
        try:
            await bot.send_message(PLATFORM_ADMIN_ID,
                f"⚠️ نصبِ خودکارِ پروژه ناموفق\n👤 <code>{user_id}</code>\n📦 {p.title}\n{res}")
        except Exception:
            logger.exception("اطلاع به مدیر ناموفق بود")
        return

    app_id = (res or {}).get("app_id")
    if trial:
        async with Session() as s:
            s.add(MarketPurchase(
                project_id=p.id, user_telegram_id=user_id,
                kind="trial", price_paid=0,
                hosted_app_id=app_id,
                trial_expires_at=datetime.datetime.now() + datetime.timedelta(
                    minutes=p.trial_minutes or 20)))
            await s.commit()
    else:
        # همان ردیفِ خرید به اپ وصل می‌شود (قبلاً ردیفِ تازه با قیمتِ کامل ثبت
        # می‌شد و آمارِ فروش را دو برابر نشان می‌داد)
        await _settle_vps_credit(claim_id, int(app_id) if app_id else 0)

# === PATCH_MARKET_BOTLINK_V1 ===
    # یوزرنیمِ رباتِ کاربر را از خودِ توکن می‌گیریم تا لینکِ مستقیم بدهیم؛
    # اگر getMe جواب نداد یعنی توکن باطل است — همان‌جا صریح می‌گوییم چون
    # علتِ رایجِ «نصب شد ولی ربات کار نمی‌کند» همین است.
    username = ""
    try:
        import aiohttp as _ah
        async with _ah.ClientSession(
                timeout=_ah.ClientTimeout(total=10)) as _s:
            async with _s.get(f"https://api.telegram.org/bot{token}/getMe") as r:
                _j = await r.json()
                username = ((_j.get("result") or {}).get("username") or "")
    except Exception:
        logger.exception("getMe برایِ توکنِ کاربر ناموفق بود")

    _kb = None
    if username:
        _kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"🤖 رفتن به @{username}", url=f"https://t.me/{username}",
            style=ButtonStyle.SUCCESS)]])

    await bot.send_message(
        user_id,
        f"✅ <b>«{p.title}» رویِ سرورِ اختصاصیِ بات‌ساز فعال شد!</b>\n\n"
        + (f"🧪 نسخهٔ آزمایشی — تا {p.trial_minutes} دقیقه دیگر فعال است.\n\n"
           if trial else "")
        + (f"🤖 ربات: @{username}\n\n" if username else
           "⚠️ نتوانستیم رباتت را با این توکن پیدا کنیم؛ توکن را دوباره چک کن.\n\n")
        + "از همین ربات، بخشِ <b>«⚡️ سرور فعال‌سازی هوشمند»</b> می‌توانی "
        "پروژه‌ات را ببینی و مدیریتش کنی: روشن/خاموش، لاگ، فایل‌ها و "
        "متغیرهایِ <code>.env</code>.\n\n"
        "اگر ربات جواب نداد، اول <b>لاگ</b> را از همان بخش ببین.",
        reply_markup=_kb)
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            f"🚀 <b>نصبِ خودکارِ پروژه</b>\n👤 <code>{user_id}</code>\n"
            f"📦 {p.title}\n⚡️ اپ: <code>{name}</code>"
            + ("\n🧪 تست" if trial else ""))
    except Exception:
        logger.exception("اطلاع به مدیر ناموفق بود")


@market_router.message(ProvisionFSM.token, F.text)
async def mk_prov_token(msg: Message, state: FSMContext):
    tok = (msg.text or "").strip()
    if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", tok):
        await msg.answer("❌ این توکن معتبر به‌نظر نمی‌رسد. دوباره بفرست "
                         "یا /cancel را بزن.")
        return
    await state.update_data(mkp_token=tok)
    await state.set_state(ProvisionFSM.admin_id)
    await msg.answer(
        "۲️⃣ حالا <b>آیدیِ عددیِ مدیرِ ربات</b> را بفرست.\n"
        "اگر خودت مدیری، همین را بفرست: <code>"
        + str(msg.from_user.id) + "</code>")


@market_router.message(ProvisionFSM.admin_id, F.text)
async def mk_prov_admin(msg: Message, state: FSMContext, bot: Bot):
    raw = re.sub(r"[^\d]", "", msg.text or "")
    if not raw:
        await msg.answer("❌ آیدیِ عددی بفرست.")
        return
    data = await state.get_data()
    await state.clear()
    p = await _project(int(data.get("mkp_pid") or 0))
    if not p:
        await msg.answer("پروژه پیدا نشد.")
        return
    await _provision(bot, msg.from_user.id, p, data.get("mkp_token", ""),
                     int(raw), bool(data.get("mkp_trial")))


@market_router.callback_query(F.data.startswith("mk:vps:"))
async def mk_vps_again(cb: CallbackQuery, state: FSMContext):
    p = await _project(int(cb.data.split(":")[2]))
    if not p or not await _has_bought(cb.from_user.id, p.id):
        await cb.answer("اول باید پروژه را بخری.", show_alert=True)
        return
    if not await _vps_credit_available(cb.from_user.id, p.id):
        await cb.answer(_VPS_USED_MSG, show_alert=True)
        return
    await cb.answer()
    await _vps_start(cb, p, state, trial=False)


# ─────────────────────── تستِ پیش از خرید ───────────────────────

@market_router.callback_query(F.data.startswith("mk:trial:"))
async def mk_trial(cb: CallbackQuery, bot: Bot, state: FSMContext):
    p = await _project(int(cb.data.split(":")[2]))
    if not p or not p.is_active or not p.trial_minutes:
        await cb.answer("تست برایِ این پروژه فعال نیست.", show_alert=True)
        return
    async with Session() as s:
        used = await s.scalar(select(MarketPurchase.id).where(
            MarketPurchase.user_telegram_id == cb.from_user.id,
            MarketPurchase.project_id == p.id,
            MarketPurchase.kind == "trial").limit(1))
    if used:
        await cb.answer("قبلاً تستِ این پروژه را گرفته‌ای.", show_alert=True)
        return
    await cb.answer()
    # === PATCH_MARKET_PROVISION_V1 ===
    # تست هم مثلِ خرید رویِ VPS نصب می‌شود؛ فقط عمرِ اپ کوتاه است.
    await _vps_start(cb, p, state, trial=True)


# ═══════════════════════ پنلِ مدیریت ═══════════════════════

class MarketFSM(StatesGroup):
    # === PATCH_MARKET_EDIT_FIELDS_V1 ===
    edit = State()
    title = State()
    desc = State()
    price = State()
    trial = State()
    zip = State()
    image = State()


def _admin_only(uid: int) -> bool:
    return uid == PLATFORM_ADMIN_ID


async def _admin_hub() -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        projects = list((await s.scalars(
            select(MarketProject).order_by(MarketProject.id.desc()))).all())
        sales = await s.scalar(select(func.count(MarketPurchase.id)).where(
            MarketPurchase.kind != "trial")) or 0
        revenue = await s.scalar(select(func.sum(MarketPurchase.price_paid)).where(
            MarketPurchase.kind != "trial")) or 0
    txt = ("🛍 <b>فروشگاهِ پروژه‌ها</b>\n\n"
           f"📦 پروژه‌ها: {len(projects)}\n"
           f"🧾 فروش: {sales} مورد\n"
           f"💰 درآمد: {_fa_money(revenue):,} تومان")
    rows = [[InlineKeyboardButton(
        text=f"{'🟢' if p.is_active else '🔴'} {p.title} — {_fa_money(p.price):,}",
        callback_data=f"mka:p:{p.id}")] for p in projects]
    rows.append([InlineKeyboardButton(text="➕ افزودنِ پروژه",
                                      callback_data="mka:new",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="👥 خریدارانِ پروژه‌ها",
                                      callback_data="mka:buyers:0",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به پنل",
                                      callback_data="adm:back",
                                      style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


@market_router.callback_query(F.data == "mka:hub")
async def mka_hub(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    txt, kb = await _admin_hub()
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@market_router.callback_query(F.data == "mka:new")
async def mka_new(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(MarketFSM.title)
    await cb.message.answer("📦 نامِ پروژه را بفرست (مثلاً «دانلودرِ توییتر»):")
    await cb.answer()


@market_router.message(MarketFSM.title, F.text)
async def mka_title(msg: Message, state: FSMContext):
    title = (msg.text or "").strip()[:120]
    if not title:
        await msg.answer("نام خالی است؛ دوباره بفرست.")
        return
    await state.update_data(mk_title=title)
    await state.set_state(MarketFSM.desc)
    await msg.answer("📝 توضیحاتِ پروژه را بفرست (یا /skip):")


@market_router.message(MarketFSM.desc, F.text)
async def mka_desc(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    await state.update_data(mk_desc="" if raw == "/skip" else (msg.html_text or raw))
    await state.set_state(MarketFSM.price)
    await msg.answer("💵 قیمت به تومان (فقط عدد):")


@market_router.message(MarketFSM.price, F.text)
async def mka_price(msg: Message, state: FSMContext):
    raw = re.sub(r"[^\d]", "", msg.text or "")
    if not raw:
        await msg.answer("عدد نبود؛ دوباره بفرست.")
        return
    await state.update_data(mk_price=int(raw))
    await state.set_state(MarketFSM.trial)
    await msg.answer("🧪 مدتِ تستِ رایگان به دقیقه (۰ یعنی بدونِ تست، پیشنهاد: ۲۰):")


@market_router.message(MarketFSM.trial, F.text)
async def mka_trial(msg: Message, state: FSMContext):
    raw = re.sub(r"[^\d]", "", msg.text or "")
    data = await state.get_data()
    async with Session() as s:
        p = MarketProject(
            title=data.get("mk_title", ""),
            description=data.get("mk_desc", ""),
            price=int(data.get("mk_price", 0)),
            trial_minutes=int(raw or 0),
            is_active=False)
        s.add(p)
        await s.commit()
        pid = p.id
    await state.clear()
    await msg.answer(
        f"✅ پروژه ساخته شد (فعلاً غیرفعال).\n\n"
        "حالا فایلِ zip و تصاویرش را از همین صفحه اضافه کن، بعد فعالش کن.",
        reply_markup=await _project_admin_kb(pid))


async def _project_admin_kb(pid: int) -> InlineKeyboardMarkup:
    p = await _project(pid)
    if not p:
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[
        # === PATCH_MARKET_EDIT_FIELDS_V1 ===
        [InlineKeyboardButton(text="📝 ویرایشِ توضیحات",
                              callback_data=f"mka:ed:desc:{pid}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="✏️ ویرایشِ عنوان",
                              callback_data=f"mka:ed:title:{pid}"),
         InlineKeyboardButton(text="💵 ویرایشِ قیمت",
                              callback_data=f"mka:ed:price:{pid}")],
        [InlineKeyboardButton(text="🧪 مدتِ تست",
                              callback_data=f"mka:ed:trial:{pid}"),
         # === PATCH_MARKET_ENV_KEYS_V1 ===
         InlineKeyboardButton(text="🔑 متغیرهای پروژه",
                              callback_data=f"mka:ed:env:{pid}")],
        [InlineKeyboardButton(text="📦 آپلود/تعویضِ فایلِ zip",
                              callback_data=f"mka:zip:{pid}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🖼 افزودنِ تصویر ({len(_images(p))})",
                              callback_data=f"mka:img:{pid}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🗑 حذفِ همهٔ تصاویر",
                              callback_data=f"mka:imgdel:{pid}")],
        [InlineKeyboardButton(
            text=("🔴 غیرفعال کن" if p.is_active else "🟢 فعال کن"),
            callback_data=f"mka:toggle:{pid}",
            style=(ButtonStyle.DANGER if p.is_active else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🗑 حذفِ پروژه", callback_data=f"mka:del:{pid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="mka:hub",
                              style=ButtonStyle.DANGER)],
    ])


async def _project_admin_text(pid: int) -> str:
    p = await _project(pid)
    if not p:
        return "پیدا نشد."
    async with Session() as s:
        sold = await s.scalar(select(func.count(MarketPurchase.id)).where(
            MarketPurchase.project_id == pid,
            MarketPurchase.kind != "trial")) or 0
    return (f"📦 <b>{p.title}</b>\n\n"
            f"{(p.description or '—')}\n\n"
            f"💵 قیمت: {_fa_money(p.price):,} تومان\n"
            f"🧪 تست: {p.trial_minutes} دقیقه\n"
            f"📁 فایل: {'✅ دارد' if (p.zip_file_id or p.zip_path) else '❌ ندارد'}\n"
            f"🖼 تصاویر: {len(_images(p))}\n"
            # === PATCH_MARKET_ENV_KEYS_V1 ===
            f"🔑 متغیرهایِ اضافی: {len([x for x in (p.env_extra or '').splitlines() if x.strip()])}\n"
            f"🔌 وضعیت: {'🟢 فعال' if p.is_active else '🔴 غیرفعال'}\n"
            f"🧾 فروش: {sold} مورد")


@market_router.callback_query(F.data.startswith("mka:p:"))
async def mka_project(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    pid = int(cb.data.split(":")[2])
    try:
        await cb.message.edit_text(await _project_admin_text(pid),
                                   reply_markup=await _project_admin_kb(pid))
    except Exception:
        await cb.message.answer(await _project_admin_text(pid),
                                reply_markup=await _project_admin_kb(pid))
    await cb.answer()


# === PATCH_MARKET_EDIT_FIELDS_V1 ===
_EDIT_FIELDS = {
    "desc":  ("📝 متنِ جدیدِ توضیحات را بفرست.\n"
              "می‌توانی از قالب‌بندیِ تلگرام (بولد، ایتالیک، ایموجی) استفاده کنی؛ "
              "عیناً همان‌طور به کاربر نشان داده می‌شود.", "description"),
    "title": ("✏️ عنوانِ جدیدِ پروژه را بفرست:", "title"),
    "price": ("💵 قیمتِ جدید به تومان (فقط عدد):", "price"),
    "trial": ("🧪 مدتِ تستِ رایگان به دقیقه (۰ یعنی بدونِ تست):", "trial_minutes"),
    # === PATCH_MARKET_ENV_KEYS_V1 ===
    "env": ("🔑 متغیرهایِ اضافیِ این پروژه را بفرست — یکی در هر خط:\n\n"
            "<code>OWNER_ID={admin}\nCHANNEL=@mychannel</code>\n\n"
            "• <code>{token}</code> و <code>{admin}</code> با توکن و آیدیِ "
            "کاربر جایگزین می‌شوند.\n"
            "• برایِ پاک‌کردن، یک خطِ تیره (-) بفرست.\n\n"
            "این‌ها علاوه بر متغیرهایِ پیش‌فرض (BOT_TOKEN، ADMIN_ID، "
            "OWNER_ID و…) اعمال می‌شوند.", "env_extra"),
}


@market_router.callback_query(F.data.startswith("mka:ed:"))
async def mka_edit_ask(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    _, _, field, pid_raw = cb.data.split(":")
    if field not in _EDIT_FIELDS:
        await cb.answer("نامعتبر", show_alert=True)
        return
    await state.set_state(MarketFSM.edit)
    await state.update_data(mk_pid=int(pid_raw), mk_field=field)
    await cb.message.answer(_EDIT_FIELDS[field][0])
    await cb.answer()


@market_router.message(MarketFSM.edit, F.text)
async def mka_edit_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = int(data.get("mk_pid") or 0)
    field = data.get("mk_field", "")
    spec = _EDIT_FIELDS.get(field)
    if not spec:
        await state.clear()
        return
    col = spec[1]
    raw = (msg.text or "").strip()

    if field in ("price", "trial"):
        digits = re.sub(r"[^\d]", "", raw)
        if digits == "":
            await msg.answer("عدد معتبر بفرست:")
            return
        value = int(digits)
    elif field == "env":
        # === PATCH_MARKET_ENV_KEYS_V1 ===
        value = "" if raw == "-" else raw
    elif field == "desc":
        # html_text قالب‌بندیِ ادمین را حفظ می‌کند؛ متنِ خام آن را از بین می‌برد.
        value = msg.html_text or raw
    else:
        value = raw[:120]
        if not value:
            await msg.answer("متن خالی است؛ دوباره بفرست:")
            return

    await state.clear()
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if not p:
            await msg.answer("پروژه پیدا نشد.")
            return
        setattr(p, col, value)
        await s.commit()
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(await _project_admin_text(pid),
                     reply_markup=await _project_admin_kb(pid))


@market_router.callback_query(F.data.startswith("mka:zip:"))
async def mka_zip_ask(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(MarketFSM.zip)
    await state.update_data(mk_pid=int(cb.data.split(":")[2]))
    await cb.message.answer("📦 فایلِ zipِ پروژه را همین‌جا بفرست:")
    await cb.answer()


@market_router.message(MarketFSM.zip, F.document)
async def mka_zip_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = int(data.get("mk_pid") or 0)
    await state.clear()
    doc = msg.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        await msg.answer("❌ فقط فایلِ zip قابلِ قبول است.")
        return
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if not p:
            await msg.answer("پروژه پیدا نشد.")
            return
        p.zip_file_id = doc.file_id
        await s.commit()
    await msg.answer("✅ فایلِ پروژه ذخیره شد.",
                     reply_markup=await _project_admin_kb(pid))


@market_router.callback_query(F.data.startswith("mka:img:"))
async def mka_img_ask(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(MarketFSM.image)
    await state.update_data(mk_pid=int(cb.data.split(":")[2]))
    await cb.message.answer(
        "🖼 تصویرِ پروژه را بفرست. می‌توانی چند تصویر پشتِ هم بفرستی؛ "
        "وقتی تمام شد /done را بزن.")
    await cb.answer()


@market_router.message(MarketFSM.image, F.photo)
async def mka_img_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = int(data.get("mk_pid") or 0)
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if not p:
            await state.clear()
            await msg.answer("پروژه پیدا نشد.")
            return
        ids = [x for x in (p.image_ids or "").split(",") if x]
        ids.append(msg.photo[-1].file_id)
        p.image_ids = ",".join(ids[:10])
        await s.commit()
        n = len(ids[:10])
    await msg.answer(f"✅ تصویر ذخیره شد ({n} تصویر). بازهم بفرست یا /done.")


@market_router.message(MarketFSM.image, F.text == "/done")
async def mka_img_done(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = int(data.get("mk_pid") or 0)
    await state.clear()
    await msg.answer(await _project_admin_text(pid),
                     reply_markup=await _project_admin_kb(pid))


@market_router.callback_query(F.data.startswith("mka:imgdel:"))
async def mka_img_del(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if p:
            p.image_ids = ""
            await s.commit()
    await cb.answer("تصاویر حذف شد.")
    await cb.message.edit_text(await _project_admin_text(pid),
                               reply_markup=await _project_admin_kb(pid))


@market_router.callback_query(F.data.startswith("mka:toggle:"))
async def mka_toggle(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if not p:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if not p.is_active and not (p.zip_file_id or p.zip_path):
            await cb.answer("اول فایلِ zipِ پروژه را آپلود کن.", show_alert=True)
            return
        p.is_active = not p.is_active
        await s.commit()
        now_on = p.is_active
    await cb.answer("🟢 فعال شد." if now_on else "🔴 غیرفعال شد.")
    await cb.message.edit_text(await _project_admin_text(pid),
                               reply_markup=await _project_admin_kb(pid))


@market_router.callback_query(F.data.startswith("mka:del:"))
async def mka_del(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(MarketProject, pid)
        if p:
            has_buyers = await s.scalar(select(MarketPurchase.id).where(
                MarketPurchase.project_id == pid).limit(1))
            if has_buyers:
                # خریدها به پروژه ارجاع دارند؛ حذفِ واقعی خطا می‌دهد و سابقهٔ
                # خریداران را هم از بین می‌برد — پس فقط از فروشگاه برداشته می‌شود
                p.is_active = False
                await s.commit()
                await cb.answer("این پروژه خریدار دارد؛ غیرفعال شد و از فروشگاه برداشته شد.",
                                show_alert=True)
                txt, kb = await _admin_hub()
                await cb.message.edit_text(txt, reply_markup=kb)
                return
            await s.delete(p)
            await s.commit()
    await cb.answer("حذف شد.")
    txt, kb = await _admin_hub()
    await cb.message.edit_text(txt, reply_markup=kb)


# ──────────────────── خریداران (پنلِ مدیر) ────────────────────

_BUYERS_PAGE = 10


@market_router.callback_query(F.data.startswith("mka:buyers:"))
async def mka_buyers(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    page = int(cb.data.split(":")[2])
    async with Session() as s:
        rows_db = (await s.execute(
            select(MarketPurchase.user_telegram_id,
                   func.count(MarketPurchase.id),
                   func.sum(MarketPurchase.price_paid))
            .where(MarketPurchase.kind != "trial")
            .group_by(MarketPurchase.user_telegram_id)
            .order_by(func.sum(MarketPurchase.price_paid).desc())
            .limit(_BUYERS_PAGE).offset(page * _BUYERS_PAGE))).all()
        total = await s.scalar(select(func.count(func.distinct(
            MarketPurchase.user_telegram_id))).where(
            MarketPurchase.kind != "trial")) or 0

    lines = [f"👥 <b>خریدارانِ پروژه‌ها</b> ({total} نفر)\n"]
    rows = []
    for tg_id, cnt, spent in rows_db:
        async with Session() as s:
            u = await s.scalar(select(PlatformUser).where(
                PlatformUser.telegram_id == tg_id))
        name = (u.full_name if u and u.full_name else str(tg_id))[:24]
        lines.append(f"• {name} — {cnt} خرید / {_fa_money(spent):,} تومان")
        rows.append([InlineKeyboardButton(
            text=f"👤 {name} ({cnt})", callback_data=f"mka:buyer:{tg_id}")])
    if not rows_db:
        lines.append("هنوز خریدی ثبت نشده.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی",
                                        callback_data=f"mka:buyers:{page-1}"))
    if (page + 1) * _BUYERS_PAGE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️",
                                        callback_data=f"mka:buyers:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="mka:hub",
                                      style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text("\n".join(lines),
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer("\n".join(lines),
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@market_router.callback_query(F.data.startswith("mka:buyer:"))
async def mka_buyer(cb: CallbackQuery):
    if not _admin_only(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    tg_id = int(cb.data.split(":")[2])
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(
            PlatformUser.telegram_id == tg_id))
        purchases = list((await s.scalars(
            select(MarketPurchase).where(
                MarketPurchase.user_telegram_id == tg_id)
            .order_by(MarketPurchase.id.desc()))).all())
        titles = {p.id: p.title for p in (await s.scalars(
            select(MarketProject))).all()}
        # اپ‌هایِ همین کاربر رویِ سرورِ فعال‌سازی
        apps = list((await s.scalars(select(HostedApp).where(
            HostedApp.owner_id == tg_id).order_by(HostedApp.id.desc()))).all())

    lines = [f"👤 <b>{(u.full_name if u else '') or tg_id}</b>",
             f"🆔 <code>{tg_id}</code>"]
    if u:
        lines.append(f"👛 موجودی: {_fa_money(u.balance):,} تومان")
        if u.username:
            lines.append(f"🔗 @{u.username}")

    lines.append("\n🧾 <b>خریدها</b>")
    if not purchases:
        lines.append("—")
    for pu in purchases[:15]:
        kind_fa = {"download": "📥 دریافتِ فایل",
                   "vps": "🚀 فعال‌سازی رویِ VPS",
                   "trial": "🧪 تست"}.get(pu.kind, pu.kind)
        line = f"• {titles.get(pu.project_id, '؟')} — {kind_fa}"
        if pu.price_paid:
            line += f" — {_fa_money(pu.price_paid):,} تومان"
        if pu.bot_username:
            line += f"\n   🤖 رویِ @{pu.bot_username}"
        lines.append(line)

    lines.append("\n⚡️ <b>سرورهایِ فعالِ این کاربر</b>")
    if not apps:
        lines.append("—")
    rows = []
    for a in apps[:10]:
        lines.append(f"• <code>{a.name}</code> — {a.status}"
                     + (f" — نوع: {a.runtime}" if a.runtime else ""))
        rows.append([InlineKeyboardButton(
            text=f"⚡️ {a.name}", callback_data=f"vip:app:{a.id}")])

    rows.append([InlineKeyboardButton(text="⬅️ بازگشت",
                                      callback_data="mka:buyers:0",
                                      style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text("\n".join(lines),
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer("\n".join(lines),
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ═══════════════════ پایانِ خودکارِ تستِ پروژه ═══════════════════
# === PATCH_MARKET_TRIAL_EXPIRY_V1 ===
# گزارش: «رباتِ اختصاصیِ تست بعدِ تمام‌شدنِ مدتِ تست قطع نمی‌شود».
# علت: مدتِ تست فقط به‌صورتِ پارامتر به سرویسِ میزبانی داده می‌شد و بعدِ آن
# هیچ‌چیزی در بات‌ساز پایانِ آن را اجرا نمی‌کرد (trial_expires_at فقط ذخیره
# می‌شد). سرویسِ میزبانی هم یک ساعت مهلتِ رایگانِ اولیه دارد، پس اپ می‌ماند.
#
# راهِ ایمن و هم‌راستا با دکمهٔ «⏸ خاموش‌کردن» پنلِ ادمین: وضعیتِ اپ را
# «suspended» می‌کنیم؛ ناظرِ سرویسِ میزبانی ظرفِ ~۲ دقیقه کانتینر را متوقف
# می‌کند. پولی‌شدن (paid_until در آینده) یا بایگانی/خاموشیِ قبلی را نادیده می‌گیریم.

async def expire_market_trials_once(bot: Bot | None = None) -> int:
    now = datetime.datetime.now()
    async with Session() as s:
        due = (await s.scalars(select(MarketPurchase).where(
            MarketPurchase.kind == "trial",
            MarketPurchase.trial_ended_at.is_(None),
            MarketPurchase.trial_expires_at.isnot(None),
            MarketPurchase.trial_expires_at <= now,
            MarketPurchase.hosted_app_id.isnot(None)).limit(50))).all()
        todo = []
        for pu in due:
            pu.trial_ended_at = now
            app = await s.get(HostedApp, pu.hosted_app_id)
            if not app:
                continue
            if app.paid_until and app.paid_until > now:
                continue            # در این فاصله پولی شده/تمدید شده
            if app.status in ("archived", "suspended"):
                continue
            app.status = "suspended"
            proj = await s.get(MarketProject, pu.project_id)
            todo.append((pu.user_telegram_id, app.name,
                         (proj.title if proj else "پروژه"), pu.project_id))
        await s.commit()
    for uid, name, title, pid in todo:
        logger.info("market trial: اپِ %s (کاربر %s) خاموش شد", name, uid)
        if bot is None:
            continue
        try:
            await bot.send_message(
                uid,
                f"⏰ <b>تستِ «{title}» تمام شد</b>\n\n"
                "ربات خاموش شد. اگر از آن راضی بودی، می‌توانی پروژه را بخری و همان‌جا فعالش کنی.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="🛒 خرید پروژه", callback_data=f"mk:p:{pid}",
                    style=ButtonStyle.SUCCESS)]]))
        except Exception:
            logger.exception("اطلاعِ پایانِ تست به کاربر نرسید")
    return len(todo)


async def market_trial_expiry_loop(interval_sec: int = 60, platform_bot: Bot | None = None):
    import asyncio
    await asyncio.sleep(30)
    while True:
        try:
            await expire_market_trials_once(platform_bot)
        except Exception:
            logger.exception("market_trial_expiry_loop خطا داد (حلقه ادامه می‌دهد)")
        await asyncio.sleep(interval_sec)

