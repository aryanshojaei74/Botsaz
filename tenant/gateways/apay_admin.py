"""
apay_admin.py — PATCH_APAY_GATEWAY_V1

پنلِ تنظیماتِ درگاهِ «آپی» (کارت‌به‌کارتِ آنی — apaygate.shop) برایِ مالکِ ربات + جریانِ شارژِ کاربر.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select, update as sa_update

from config import Session
from models import ApayCharge, BotUser
from tenant.gateways import apay as AP

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

apay_router = Router()

# === PATCH_APAY_ONE_OPEN_INVOICE_V1 === هر کاربر در هر ربات هم‌زمان فقط یک فاکتور باز دارد
_invoice_locks: dict[tuple[int, int], "asyncio.Lock"] = {}


class ApFSM(StatesGroup):
    token = State()
    secret = State()
    amount = State()
    min_amount = State()
    max_amount = State()
    display_name = State()   # === PATCH_APAY_DISPLAY_NAME_V1 ===
    fee_amount = State()     # === PATCH_APAY_FEE_PAYER_V1 ===
    log_channel = State()    # === PATCH_APAY_LOG_CHANNEL_V1 ===


APAY_BOT = "APayGateBot"
APK_URL = "https://apaygate.shop/download/apay-forwarder.apk"
# لینکِ شورتکاتِ امضاشدهٔ آیفون از /config.js ِ سایتِ آپی خوانده می‌شود (AP.site_links)؛
# این فقط برایِ جایگزینیِ دستی است اگر روزی لازم شد.
IOS_SHORTCUT_URL = ""


DEFAULT_DISPLAY_NAME = "آپی"


async def get_display_name(bot_id: int) -> str:
    """اسمی که کاربر برایِ این درگاه می‌بیند (کیف پول، پرداختِ محصول، فاکتور)."""
    v = ((await AP._get(bot_id, "apay_display_name", "")) or "").strip()
    return v[:40] if v else DEFAULT_DISPLAY_NAME


def _mask(v: str) -> str:
    return (v[:6] + "…" + v[-4:]) if v and len(v) > 12 else ("ثبت شده" if v else "—")


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    tok = await AP._get(bot_id, "apay_token", "")
    sec = await AP._get(bot_id, "apay_secret", "")
    on = await AP._get(bot_id, "apay_enabled", "0") == "1"
    show_wallet = await AP._get(bot_id, "apay_show_wallet", "1") == "1"
    show_purchase = await AP._get(bot_id, "apay_show_purchase", "1") == "1"
    min_amount = await AP.get_min_amount(bot_id)
    max_amount = await AP.get_max_amount(bot_id)
    disp = await get_display_name(bot_id)
    fee_payer, fee_amt = await AP.get_fee_cfg(bot_id)   # PATCH_APAY_FEE_PAYER_V1

    txt = ("💳 <b>درگاهِ آپی (کارت‌به‌کارتِ آنی)</b>\n\n"
           "کاربر مبلغ را وارد می‌کند، در صفحهٔ پرداختِ آپی شمارهٔ کارتِ تو را می‌بیند و "
           "کارت‌به‌کارت می‌کند؛ به‌محضِ رسیدنِ پیامکِ بانک به گوشیِ تو، کیف پولش "
           "<b>خودکار</b> شارژ می‌شود. پول مستقیم به کارتِ خودت می‌آید.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکنِ API: <code>{_mask(tok)}</code>\n"
           f"🏷 اسمِ نمایشی برایِ کاربر: <b>{disp}</b>\n"
           f"💸 کارمزدِ هر پرداخت: <b>{fee_amt:,}</b> تومان — پرداخت توسطِ <b>{AP.FEE_PAYERS[fee_payer]}</b>\n"
           f"🔽 حداقلِ مبلغ: <b>{min_amount:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{max_amount:,}' if max_amount else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n\n"
           "📖 برایِ راه‌اندازیِ قدم‌به‌قدم و اتصالِ گوشیِ اندروید/آیفون، «📖 راهنما» را بزن.\n\n"
           "ℹ️ <b>نیازی به ثبت وب‌هوک در آپی نیست.</b> ربات خودش پرداخت‌ها را استعلام می‌کند و کیف پول مشتری را شارژ می‌کند.\n\n"
           + f"💡 کارمزدِ آپی برایِ هر پرداختِ موفق {AP.FEE_TOMAN:,} تومان است و از کیف پولِ حسابِ "
           "آپیِ تو کم می‌شود (نه از مشتری)؛ آن کیف پول را شارژ نگه دار.\n"
           "💡 مشتری باید <b>دقیقاً</b> مبلغِ نمایش‌داده‌شده در صفحهٔ پرداخت را واریز کند "
           "(چند تومانِ یکتا اضافه دارد)؛ کیف پولش به اندازهٔ مبلغی که وارد کرده شارژ می‌شود.")

    # === PATCH_APAY_SAME_CARD_WARN_V1 ===
    # آپی همهٔ پیامک‌هایِ بانکِ آن کارت را می‌خواند؛ اگر کارت‌به‌کارتِ عادیِ ربات هم همان کارت را
    # نشان بدهد، واریزِ عادی (که رسیدش باید دستی تأیید شود) می‌تواند توسطِ آپی هم تأیید و شارژ شود.
    if on:
        from tenant import get_setting as _gs
        if (await _gs(bot_id, "card_enabled", "1") == "1"
                and ((await _gs(bot_id, "card_number", "")) or (await _gs(bot_id, "card_number2", "")))):
            txt += ("\n\n⚠️ <b>کارت‌به‌کارتِ عادی هم فعال است.</b> اگر کارتِ آپی همان کارتِ کارت‌به‌کارتِ عادی باشد، "
                    "آپی واریزِ مشتریانِ «عادی» را هم می‌بیند و ممکن است خودکار تأیید کند (و رسیدِ دستی هم "
                    "جداگانه می‌آید → شارژِ دوبار). راهِ امن: برایِ آپی یک <b>کارتِ جدا</b> بگیر، یا "
                    "کارت‌به‌کارتِ عادی را خاموش کن.")
    rows = [
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"),
            callback_data="ap:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ توکن",
                              callback_data="ap:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🏷 اسمِ نمایشی: {disp}",
                              callback_data="ap:setname", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=("💸 کارمزد با: " + ("👤 خریدار" if fee_payer == "buyer" else "🏪 فروشنده")),
                              callback_data="ap:feepayer", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"💸 مبلغِ کارمزد: {fee_amt:,}",
                              callback_data="ap:feeamt", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقل: {min_amount:,}",
                              callback_data="ap:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {max_amount:,}" if max_amount else "🔼 حداکثر: بدونِ سقف",
                              callback_data="ap:setmax", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}",
            callback_data="ap:show_wallet",
            style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(
            text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
            callback_data="ap:show_purchase",
            style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="📢 لاگِ پرداخت‌هایِ آپی در کانال",
                              callback_data="ap:log", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🧪 تستِ اتصال و موجودی",
                              callback_data="ap:test", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🤖 رباتِ آپی", url=f"https://t.me/{APAY_BOT}"),
         InlineKeyboardButton(text="📖 راهنما", callback_data="ap:guide",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت",
                              callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


# ═════════════════ راهنما ═════════════════
async def _guide_content(bot_id: int, page: str):
    return await guide_content(page, await AP.webhook_url(bot_id), "ap")


async def guide_content(page: str, hook: str, cb_prefix: str):
    """صفحه‌هایِ راهنما؛ مشترک بینِ رباتِ مستأجر (cb_prefix="ap") و بات‌سازِ اصلی ("papay")."""
    back = [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"{cb_prefix}:guide",
                                 style=ButtonStyle.DANGER)]
    bot_btn = [InlineKeyboardButton(text="🤖 رباتِ آپی", url=f"https://t.me/{APAY_BOT}")]
    links = await AP.site_links() if page in ("android", "ios") else {}
    if page == "android":
        txt = ("🤖 <b>اتصالِ گوشیِ اندروید</b>\n\n"
               "گوشی‌ای را انتخاب کن که سیم‌کارتِ پیامکِ بانکت رویِ آن است (اندروید ۷ به بالا).\n\n"
               "۱. اپِ آپی را با دکمهٔ «📥 دانلودِ اپ اندروید» نصب کن و دسترسیِ پیامک را بده.\n"
               "   🛡 اگر موقعِ نصب هشدارِ امنیتی (Play Protect / «برنامهٔ ناامن») آمد، "
               "«جزئیاتِ بیشتر» را بزن و <b>«در هر صورت نصب شود» (Install anyway)</b> را انتخاب کن. "
               "این هشدار فقط تا گرفتنِ تأییدِ امنیتیِ گوگل نمایش داده می‌شود و بعد از آن دیگر نمی‌آید.\n"
               f"۲. در @{APAY_BOT} از بخشِ «💳 کارت‌ها» کارت را انتخاب کن تا لینکِ اتصال "
               "(<code>apaygate.shop/pair/…</code>) را بدهد.\n"
               "۳. لینک را رویِ همان گوشی باز کن و «باز کردن در اپ» را بزن "
               "(یا آدرسِ وب‌هوکِ گوشی را داخلِ اپ بچسبان).\n"
               "۴. در اپ، فرستندهٔ پیامکِ بانک را در فیلتر بنویس، «ذخیره» و بعد «تستِ اتصال» را بزن.\n"
               "۵. ⚠️ بهینه‌سازیِ باتری را برایِ اپ خاموش و «اجرایِ خودکار / auto-start» را روشن کن؛ "
               "در شیائومی، سامسونگ، هوآوی و… وگرنه گوشی اپ را می‌بندد و پیامک‌ها نمی‌رسند.\n\n"
               "✅ اگر گوشی خاموش یا بی‌اینترنت باشد، پیامک‌ها در صف می‌مانند و به‌محضِ وصل‌شدن ارسال می‌شوند.")
        rows = [[InlineKeyboardButton(text="📥 دانلودِ اپ اندروید", url=links.get("apk") or APK_URL)],
                bot_btn, back]
    elif page == "ios":
        ios_url = IOS_SHORTCUT_URL or links.get("ios") or ""
        warn = ("⚠️ آیفون پیامک‌هایِ ناموفق را دوباره نمی‌فرستد: اگر هنگامِ رسیدنِ پیامک گوشی خاموش "
                "یا بی‌اینترنت باشد، آن پرداخت خودکار تأیید نمی‌شود. برایِ فروشِ زیاد گوشیِ اندروید بهتر است.")
        if ios_url:
            txt = ("🍎 <b>اتصالِ آیفون (شورتکات)</b>\n\n"
                   f"۱. در @{APAY_BOT} از بخشِ «گوشی‌ها» یک <b>آیفون</b> اضافه کن و "
                   "<b>کدِ اتصال</b> (کلیدِ گوشی) را کپی کن.\n"
                   "۲. دکمهٔ «➕ افزودنِ شورتکاتِ آپی» را بزن و در اپِ Shortcuts «<b>Add Shortcut</b>» را بزن.\n"
                   "۳. شورتکاتِ «APay» را یک‌بار اجرا کن، «🔑 تنظیمات» را بزن و کدِ اتصال را paste کن؛ "
                   "باید «وصل شد» ببینی.\n"
                   "۴. اپِ <b>Shortcuts</b> ← <b>Automation</b> ← <b>New Automation</b> ← <b>Message</b>؛ "
                   "در <b>Sender</b> فرستندهٔ پیامکِ بانک را انتخاب کن، <b>Run Immediately</b> را روشن کن "
                   "و شورتکاتِ «APay» را به‌عنوانِ کار انتخاب کن.\n"
                   "۵. در شورتکات «🧪 تستِ اتصال» را بزن؛ گوشی در رباتِ آپی 🟢 می‌شود.\n\n" + warn)
            rows = [[InlineKeyboardButton(text="➕ افزودنِ شورتکاتِ آپی", url=ios_url)], bot_btn, back]
        else:
            txt = ("🍎 <b>اتصالِ آیفون (شورتکات)</b>\n\n"
                   "لینکِ شورتکات الان از سایتِ آپی در دسترس نیست؛ می‌توانی آن را از صفحهٔ "
                   "<code>apaygate.shop/docs#ios</code> بگیری یا دستی بسازی:\n\n"
                   f"۱. در @{APAY_BOT} از بخشِ «گوشی‌ها» یک <b>آیفون</b> اضافه کن و "
                   "<b>کلیدِ گوشی</b> (کدِ اتصال) را کپی کن.\n"
                   "۲. اپِ <b>Shortcuts</b> ← تبِ <b>Automation</b> ← <b>New Automation</b> ← <b>Message</b>.\n"
                   "۳. در <b>Sender</b> فرستندهٔ پیامکِ بانک را انتخاب کن، "
                   "<b>Run Immediately</b> را روشن کن و <b>Next</b> ← <b>New Blank Automation</b>.\n"
                   "۴. اکشنِ <b>Get Contents of URL</b> را اضافه کن و این آدرس را بنویس "
                   "(به‌جایِ KEY کلیدِ گوشی را بگذار):\n"
                   "<code>https://apaygate.shop/hook/sms/KEY</code>\n"
                   "۵. رویِ فلشِ اکشن بزن: <b>Method</b> = <b>POST</b> و <b>Request Body</b> = <b>JSON</b>.\n"
                   "۶. دو فیلد از نوعِ Text اضافه کن:\n"
                   "   • <code>message</code> ← مقدار: <b>Shortcut Input</b> و از رویش <b>Content</b> را انتخاب کن\n"
                   "   • <code>sender</code> ← مقدار: <b>Shortcut Input</b> و از رویش <b>Sender</b> را انتخاب کن\n"
                   "۷. <b>Done</b> را بزن. با یک واریزِ کوچک تست کن؛ گوشی در رباتِ آپی 🟢 می‌شود.\n\n" + warn)
            rows = [[InlineKeyboardButton(text="🌐 راهنمایِ آیفون در سایتِ آپی",
                                          url="https://apaygate.shop/docs#ios")], bot_btn, back]
    else:
        txt = ("📖 <b>راهنمایِ درگاهِ آپی</b>\n\n"
               f"۱. در @{APAY_BOT} استارت بزن و ثبت‌نام کن.\n"
               "۲. از «💳 کارت‌ها» کارتِ بانکی و <b>فرستندهٔ پیامکِ بانک</b> را اضافه کن.\n"
               "۳. گوشی‌ای را که پیامکِ بانک رویش می‌آید وصل کن — دکمهٔ «🤖 اندروید» یا «🍎 آیفون» پایین.\n"
               "۴. از «🔑 توکن API» توکن (<code>apay_…</code>) بگیر و در همین ربات «🔑 ثبت / تغییرِ توکن» را بزن.\n"
               f"۵. «💰 شارژ کیف پول» در رباتِ آپی را برایِ کارمزد (هر پرداخت {AP.FEE_TOMAN:,} تومان) شارژ کن.\n"
               "۶. «✅ فعال‌کردن» را بزن و با «🧪 تستِ اتصال و موجودی» چک کن.\n\n"
               + ("⚡️ <b>اختیاری (شارژ فوری):</b> در ربات آپی از «👤 پروفایل» webhook secret را بردار و "
                  "اینجا «🔐 کلید وب‌هوک» را بزن"
                  + (f"؛ آدرس وب‌هوک:\n<code>{hook}</code>" if hook else ".")
                  + "\nبدون این مرحله هم ربات هر دقیقه خودش پرداخت‌ها را استعلام می‌کند."
                  if cb_prefix == "papay" else
                  # === PATCH_APAY_NO_WEBHOOK_UI_V1 === مشتریان مستأجر نیازی به وب‌هوک ندارند و گیج می‌شدند
                  "ℹ️ <b>نیازی به ثبت وب‌هوک در آپی نیست.</b> ربات خودش پرداخت‌ها را استعلام می‌کند و بعد از "
                  "رسیدن پیامک بانک، کیف پول مشتری را خودکار شارژ می‌کند."))
        rows = [
            [InlineKeyboardButton(text="🤖 اتصالِ اندروید", callback_data=f"{cb_prefix}:guide:android",
                                  style=ButtonStyle.SUCCESS),
             InlineKeyboardButton(text="🍎 اتصالِ آیفون", callback_data=f"{cb_prefix}:guide:ios",
                                  style=ButtonStyle.PRIMARY)],
            bot_btn,
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"{cb_prefix}:menu",
                                  style=ButtonStyle.DANGER)],
        ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


@apay_router.callback_query(F.data.startswith("ap:guide"))
async def ap_guide(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    page = cb.data.split(":", 2)[2] if cb.data.count(":") == 2 else ""
    txt, kb = await _guide_content(row.id, page)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML",
                                   disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML",
                                disable_web_page_preview=True)
    await cb.answer()


async def _show(target, bot_id):
    txt, kb = await _menu_content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@apay_router.callback_query(F.data.in_({"set:apay", "ap:menu"}))
async def ap_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@apay_router.callback_query(F.data == "ap:toggle")
async def ap_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AP._get(row.id, "apay_enabled", "0") == "1"
    if not cur and not await AP._get(row.id, "apay_token", ""):
        return await cb.answer("اول توکنِ API را ثبت کن.", show_alert=True)
    await AP._set(row.id, "apay_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


@apay_router.callback_query(F.data.in_({"ap:show_wallet", "ap:show_purchase"}))
async def ap_show_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = "apay_show_wallet" if cb.data == "ap:show_wallet" else "apay_show_purchase"
    cur = await AP._get(row.id, key, "1") == "1"
    await AP._set(row.id, key, "0" if cur else "1")
    await cb.answer("پنهان شد" if cur else "نمایش داده می‌شود")
    await _show(cb, row.id)


@apay_router.callback_query(F.data.in_({"ap:setmin", "ap:setmax"}))
async def ap_setlimit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if cb.data == "ap:setmin":
        cur = await AP.get_min_amount(row.id)
        await state.set_state(ApFSM.min_amount)
        txt = (f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>).\n"
               f"کمترین مقدارِ مجاز در آپی {AP.API_MIN_TOMAN:,} تومان است.")
    else:
        cur = await AP.get_max_amount(row.id)
        await state.set_state(ApFSM.max_amount)
        txt = (f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: "
               f"<b>{(f'{cur:,}' if cur else 'بدونِ سقف')}</b>).\nبرایِ برداشتنِ سقف ۰ بفرست.")
    await cb.message.answer(txt, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.min_amount, F.text)
async def ap_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n < AP.API_MIN_TOMAN:
        return await msg.answer(f"عدد باید حداقل {AP.API_MIN_TOMAN:,} تومان باشد.")
    await AP._set(row.id, "apay_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@apay_router.message(ApFSM.max_amount, F.text)
async def ap_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await AP._set(row.id, "apay_max_amount", str(int(n)))
    await msg.answer("✅ حداکثرِ مبلغ " + ("برداشته شد." if int(n) == 0 else f"روی {int(n):,} تومان تنظیم شد."))
    await _show(msg, row.id)


# === PATCH_APAY_FEE_PAYER_V1 ===
@apay_router.callback_query(F.data == "ap:feepayer")
async def ap_feepayer(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    payer, _a = await AP.get_fee_cfg(row.id)
    nxt = "seller" if payer == "buyer" else "buyer"
    await AP._set(row.id, "apay_fee_payer", nxt)
    await cb.answer("کارمزد از این پس با " + ("خریدار است (رویِ فاکتور اضافه می‌شود)" if nxt == "buyer"
                                              else "فروشنده است (از کیف پولِ آپیِ شما)"), show_alert=True)
    await _show(cb, row.id)


@apay_router.callback_query(F.data == "ap:feeamt")
async def ap_feeamt(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _p, cur = await AP.get_fee_cfg(row.id)
    await state.set_state(ApFSM.fee_amount)
    await cb.message.answer(
        f"💸 مبلغِ کارمزدِ هر پرداخت را به تومان بفرست (فعلاً: <b>{cur:,}</b>).\n"
        f"کارمزدِ فعلیِ خودِ آپی {AP.FEE_TOMAN:,} تومان است. این عدد فقط وقتی «کارمزد با خریدار» "
        "باشد رویِ فاکتورِ مشتری اضافه می‌شود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.fee_amount, F.text)
async def ap_feeamt_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0 or n > 1_000_000:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await AP._set(row.id, "apay_fee_amount", str(int(n)))
    await msg.answer(f"✅ کارمزد رویِ {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


# === PATCH_APAY_DISPLAY_NAME_V1 ===
@apay_router.callback_query(F.data == "ap:setname")
async def ap_setname(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """فقط برچسبِ نمایشی برایِ کاربر عوض می‌شود؛ اتصال همان apaygate.shop است."""
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_display_name(row.id)
    await state.set_state(ApFSM.display_name)
    await cb.message.answer(
        f"🏷 اسمی که می‌خواهی به‌جایِ «{DEFAULT_DISPLAY_NAME}» به کاربر نشان داده شود را بفرست "
        f"(فعلاً: <b>{cur}</b>؛ حداکثر ۴۰ حرف). این اسم عیناً رویِ دکمهٔ کیف پول و پرداختِ محصول "
        "می‌نشیند؛ مثلاً <code>💳 کارت به کارت آنی</code>.\n\n"
        "برایِ بازگشت به اسمِ پیش‌فرض، کلمهٔ <code>پیش‌فرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.display_name, F.text)
async def ap_setname_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    name = (msg.text or "").strip()
    if name in ("پیش‌فرض", "پیشفرض", "default", "-"):
        await AP._set(row.id, "apay_display_name", "")
        await msg.answer(f"✅ اسمِ نمایشی به پیش‌فرض (<b>{DEFAULT_DISPLAY_NAME}</b>) برگشت.",
                         parse_mode="HTML")
    elif not name:
        return await msg.answer("اسمِ نامعتبر. دوباره امتحان کن.")
    else:
        await AP._set(row.id, "apay_display_name", name[:40])
        await msg.answer(f"✅ اسمِ نمایشی رویِ «{name[:40]}» تنظیم شد.")
    await _show(msg, row.id)


@apay_router.callback_query(F.data == "ap:setkey")
async def ap_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(ApFSM.token)
    await cb.message.answer(
        "🔑 <b>توکنِ API</b> (که با <code>apay_</code> شروع می‌شود) را از رباتِ آپی "
        "بگیر و این‌جا بفرست.\n\nاین توکن فقط روی سرورِ ما نگه‌داری می‌شود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.token, F.text)
async def ap_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    tok = (msg.text or "").strip()
    try:
        await msg.delete()      # توکن در تاریخچهٔ چت نماند
    except Exception:
        logger.debug("apay: حذفِ پیامِ توکن ناموفق بود")
    if len(tok) < 10 or " " in tok:
        return await msg.answer("توکن نامعتبر است. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ توکن…")
    ok, why = await AP.validate_token(tok)
    if not ok:
        return await msg.answer(f"❌ {why}")
    await AP._set(row.id, "apay_token", tok)
    await msg.answer("✅ توکن ذخیره شد.\n" + why)
    await _show(msg, row.id)


@apay_router.callback_query(F.data == "ap:setsecret")
async def ap_setsecret(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    hook = await AP.webhook_url(row.id)
    await state.set_state(ApFSM.secret)
    await cb.message.answer(
        "🔐 <b>کلیدِ امضایِ وب‌هوک</b> (webhook secret) را از رباتِ آپی بفرست.\n\n"
        + (f"آدرسِ وب‌هوکی که باید در آپی ثبت کنی:\n<code>{hook}</code>\n\n" if hook else "")
        + "برایِ حذفِ کلید، عددِ ۰ بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.secret, F.text)
async def ap_setsecret_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    sec = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        logger.debug("apay: حذفِ پیامِ کلید ناموفق بود")
    if sec in {"0", "۰"}:
        await AP._set(row.id, "apay_secret", "")
        await msg.answer("✅ کلیدِ وب‌هوک حذف شد.")
    elif len(sec) < 8 or " " in sec:
        return await msg.answer("کلید نامعتبر است. دوباره امتحان کن.")
    else:
        await AP._set(row.id, "apay_secret", sec)
        await msg.answer("✅ کلیدِ وب‌هوک ذخیره شد.")
    await _show(msg, row.id)


@apay_router.callback_query(F.data == "ap:test")
async def ap_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    tok = await AP._get(row.id, "apay_token", "")
    if not tok:
        return await cb.answer("اول توکن را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await AP.validate_token(tok)
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


# ═════════════════ جریانِ کاربر ═════════════════
async def _apu_block_if_open(target, row, tg_id: int) -> bool:
    """اگر کاربر فاکتور بازی دارد، به‌جای ساختن فاکتور دوم «یا پرداخت کن یا لغو کن» را می‌دهد (True)."""
    ch = await AP.open_invoice(row.id, tg_id)
    if ch is None:
        return False
    age = max(0, int((datetime.utcnow() - (ch.created_at or datetime.utcnow())).total_seconds() // 60))
    rows = []
    if (ch.pay_url or "").startswith("http"):
        rows.append([InlineKeyboardButton(text="💳 رفتن به صفحهٔ پرداخت", url=ch.pay_url)])
    rows.append([InlineKeyboardButton(text="🔄 بررسی پرداخت", callback_data=f"apu:check:{ch.order_id}"),
                 InlineKeyboardButton(text="❌ لغو این فاکتور", callback_data=f"apu:cancel:{ch.order_id}",
                                      style=ButtonStyle.DANGER)])
    await target.answer(
        "⚠️ <b>شما یک فاکتور باز دارید</b>\n\n"
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b> — {age} دقیقه پیش ساخته شده.\n\n"
        "برای ساختن فاکتور جدید، اول همین فاکتور را <b>پرداخت کن</b> یا <b>لغوش کن</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    return True


@apay_router.callback_query(F.data.in_({"apu:start", "w:apay"}))
async def apu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await AP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    await state.clear()
    if await _apu_block_if_open(cb.message, row, cb.from_user.id):      # PATCH_APAY_ONE_OPEN_INVOICE_V1
        return await cb.answer()
    min_amount = await AP.get_min_amount(row.id)
    max_amount = await AP.get_max_amount(row.id)
    rng = f"حداقل: <b>{min_amount:,}</b> تومان"
    if max_amount:
        rng += f" | حداکثر: <b>{max_amount:,}</b> تومان"
    await state.set_state(ApFSM.amount)
    await cb.message.answer(
        f"💳 چه مبلغی می‌خواهی شارژ کنی؟ (تومان)\n\n{rng}",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.amount, F.text)
async def apu_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = fa_to_int(msg.text)
    min_amount = await AP.get_min_amount(row.id)
    max_amount = await AP.get_max_amount(row.id) or AP.API_MAX_TOMAN
    if not amount or amount < min_amount:
        return await msg.answer(f"حداقلِ مبلغ {min_amount:,} تومان است. دوباره بفرست.")
    if amount > min(max_amount, AP.API_MAX_TOMAN):
        return await msg.answer(f"حداکثرِ مبلغ {min(max_amount, AP.API_MAX_TOMAN):,} تومان است. دوباره بفرست.")
    await state.clear()
    await _apu_create_charge(msg, bot, row, int(amount), msg.from_user.id)


# === PATCH_APAY_DIRECT_PURCHASE_V1 ===
# پرداختِ مستقیم: مبلغ همان کسریِ سفارش است (نه انتخابِ دستیِ مبلغ)، و بعد از پرداخت دکمهٔ
# «تکمیلِ خرید» می‌آید — دقیقاً مثلِ آبان‌گیت‌وی. تکمیلِ نهایی (pay:wallet / vpay / vc:pay) خودش دوباره
# قیمت و موجودی را حساب می‌کند، پس دست‌کاریِ callback امنیتی ایجاد نمی‌کند.
async def _apu_direct_need(cb: CallbackQuery, row, total_price: int, bal: int):
    need = int(total_price) - int(bal)
    if need <= 0:
        await cb.answer("موجودیِ کیف‌پولت کافی است — همان «✅ پرداخت از کیف پول» را بزن.", show_alert=True)
        return None
    min_amount = await AP.get_min_amount(row.id)
    max_amount = min(await AP.get_max_amount(row.id) or AP.API_MAX_TOMAN, AP.API_MAX_TOMAN)
    if need < min_amount:
        need = min_amount          # کمتر از حداقلِ درگاه؛ باقی‌اش در کیف پول می‌ماند
    if need > max_amount:
        await cb.answer(f"مبلغِ لازم از سقفِ درگاه ({max_amount:,} تومان) بیشتر است. از روشِ دیگری پرداخت کن.",
                        show_alert=True)
        return None
    return need


async def _apu_user_balance(row, tg_id: int) -> int:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == tg_id))
    return int(float(u.balance or 0)) if u else 0


@apay_router.callback_query(F.data.startswith("w:apay:vpn:"))
async def apu_start_for_vpn_plan(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """w:apay:vpn:{plan_id}:{قیمتِ_کامل}"""
    from tenant import get_bot_row
    from models import VpnPlan
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await AP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    try:
        _, _, _, plan_id_s, price_s = cb.data.split(":")
        plan_id, total = int(plan_id_s), int(price_s)
    except (ValueError, IndexError):
        return await cb.answer()
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
    if not plan or plan.bot_id != row.id or not plan.active:
        return await cb.answer("این پلن دیگر در دسترس نیست.", show_alert=True)
    if await _apu_block_if_open(cb.message, row, cb.from_user.id):      # PATCH_APAY_ONE_OPEN_INVOICE_V1
        return await cb.answer()
    need = await _apu_direct_need(cb, row, total, await _apu_user_balance(row, cb.from_user.id))
    if need is None:
        return
    await state.clear()
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    await _apu_create_charge(cb.message, bot, row, need, cb.from_user.id, for_order=-plan_id)


@apay_router.callback_query(F.data.startswith("w:apay:vc:"))
async def apu_start_for_vc(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """w:apay:vc:{gb}:{days}:{count}:{مبلغِ_فاکتور}"""
    from tenant import get_bot_row
    from tenant.gateways.abangateway_admin import vc_pack
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await AP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    try:
        _, _, _, gb_s, days_s, count_s, price_s = cb.data.split(":")
        gb, days, count, total = int(gb_s), int(days_s), int(count_s), int(price_s)
    except (ValueError, IndexError):
        return await cb.answer()
    if not (0 <= gb <= 4095 and 0 <= days <= 2047 and 1 <= count <= 31) or total <= 0:
        return await cb.answer()
    if await _apu_block_if_open(cb.message, row, cb.from_user.id):      # PATCH_APAY_ONE_OPEN_INVOICE_V1
        return await cb.answer()
    need = await _apu_direct_need(cb, row, total, await _apu_user_balance(row, cb.from_user.id))
    if need is None:
        return
    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count)
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    await _apu_create_charge(cb.message, bot, row, need, cb.from_user.id, for_order=vc_pack(gb, days, count))


@apay_router.callback_query(F.data.regexp(r"^w:apay:\d+$"))
async def apu_start_for_order(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """w:apay:{order_id} — پرداختِ مستقیمِ یک سفارشِ فروشگاه/محصول."""
    from tenant import get_bot_row
    from models import Order
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await AP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    oid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        o = await s.get(Order, oid)
        if not o or o.bot_id != row.id or o.status != "pending":
            return await cb.answer("این سفارش دیگر معتبر نیست.", show_alert=True)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u or o.user_id != u.id:
            return await cb.answer("این سفارش مالِ تو نیست.", show_alert=True)
        total, bal = int(float(o.amount)), int(float(u.balance or 0))
    if await _apu_block_if_open(cb.message, row, cb.from_user.id):      # PATCH_APAY_ONE_OPEN_INVOICE_V1
        return await cb.answer()
    need = await _apu_direct_need(cb, row, total, bal)
    if need is None:
        return
    await state.clear()
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    await _apu_create_charge(cb.message, bot, row, need, cb.from_user.id, for_order=oid)




async def _apu_create_charge(msg: Message, bot: Bot, row, amount: int, tg_id: int,
                             for_order: int | None = None) -> None:
    # === PATCH_APAY_ONE_OPEN_INVOICE_V1 === بررسی «فاکتور باز» و ساخت فاکتور زیر یک قفل هر-کاربر،
    # تا دو کلیک پشت‌سرهم دو فاکتور نسازد.
    async with _invoice_locks.setdefault((int(row.id), int(tg_id)), asyncio.Lock()):
        if await _apu_block_if_open(msg, row, tg_id):
            return
        await _apu_create_charge_locked(msg, bot, row, amount, tg_id, for_order)


async def _apu_create_charge_locked(msg: Message, bot: Bot, row, amount: int, tg_id: int,
                                    for_order: int | None = None) -> None:
    """فاکتورِ آپی می‌سازد و نشان می‌دهد. for_order: پرداختِ مستقیمِ یک سفارش/پلن/خریدِ دلخواه
    (=== PATCH_APAY_DIRECT_PURCHASE_V1 ===)؛ بعد از پرداخت دکمهٔ «تکمیلِ خرید» می‌آید."""
    try:
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == tg_id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await msg.answer(why)
    except Exception:
        logger.exception("apay: بررسیِ سقف ناموفق بود")

    token = await AP._get(row.id, "apay_token", "")
    if not token:
        return await msg.answer("درگاه تنظیم نشده است.")

    oid = AP.new_order_id()
    _payer, _famt = await AP.get_fee_cfg(row.id)          # PATCH_APAY_FEE_PAYER_V1
    fee = AP.fee_for_buyer(_payer, _famt)
    if int(amount) + fee > AP.API_MAX_TOMAN:
        return await msg.answer(f"حداکثرِ مبلغ با کارمزد {AP.API_MAX_TOMAN:,} تومان است.")
    wait = await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    code, res = await AP.create_payment(
        token, amount_toman=int(amount) + fee, order_id=oid,
        description="شارژ کیف پول",   # بدونِ آیدیِ کاربر؛ در صفحهٔ پرداخت دیده می‌شود
        callback_url=await AP.webhook_url(row.id),
        return_url=(f"https://t.me/{row.username}" if row.username else ""))
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        # دامنهٔ وب‌هوک را نپذیرفت؛ بدونِ callback (با استعلامِ دوره‌ای) دوباره
        code, res = await AP.create_payment(
            token, amount_toman=int(amount) + fee, order_id=oid,
            return_url=(f"https://t.me/{row.username}" if row.username else ""))
    if code not in (200, 201) or not isinstance(res, dict) or not res.get("pay_url"):
        logger.warning("apay: ساختِ پرداخت ناموفق (bot=%s): %s %s", row.id, code, str(res)[:300])
        return await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + AP.err_text(code, res))

    # === PATCH_APAY_DOC_PAYABLE_V1 === مستندات: «دقیقاً payable_rial را به مشتری نشان دهید» (اگر نبود، pay_amount)
    pay_rial = int(res.get("payable_rial") or res.get("pay_amount") or 0)
    async with Session() as s:
        s.add(ApayCharge(
            bot_id=row.id, user_telegram_id=tg_id, order_id=oid,
            payment_id=str(res.get("id") or ""), amount_toman=int(amount), fee_toman=fee,
            pay_amount_rial=pay_rial or None, pay_url=str(res["pay_url"])[:300],
            status="waiting", for_order_id=for_order))
        await s.commit()
    try:
        await wait.delete()
    except Exception:
        logger.debug("apay: حذفِ پیامِ انتظار ناموفق بود")

    card, holder = await AP.card_for(token, res)
    text, kb = AP.invoice_view(
        title=await get_display_name(row.id), pay_rial=pay_rial, card=card, holder=holder,
        order_id=oid, pay_url=str(res["pay_url"]), check_cb=f"apu:check:{oid}",
        cancel_cb=f"apu:cancel:{oid}", fee_toman=fee, credit_toman=int(amount))
    sent = await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    # === PATCH_APAY_FAST_WATCH_V1 ===
    try:
        async with Session() as s:
            ch = await s.scalar(select(ApayCharge).where(
                ApayCharge.bot_id == row.id, ApayCharge.order_id == oid))
            if ch:
                ch.chat_id, ch.msg_id = sent.chat.id, sent.message_id
                await s.commit()
    except Exception:
        logger.exception("apay: ذخیرهٔ شناسهٔ پیامِ فاکتور ناموفق بود")
    from tenant.gateways.apay_log import log_event as _alog         # PATCH_APAY_LOG_CHANNEL_V1
    await _alog(row.id, "created", user_tg=tg_id, amount_toman=int(amount), pay_rial=pay_rial,
                fee_toman=fee, order_id=oid)
    AP.watch_tenant_charge(row.id, oid, str(res.get("id") or ""))




@apay_router.callback_query(F.data.startswith("apu:check:"))
async def apu_check(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == row.id, ApayCharge.order_id == oid))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
    if ch.status == "paid":
        return await cb.answer("✅ این فاکتور پرداخت و کیف پولت شارژ شده است.", show_alert=True)
    # PATCH_GATEWAY_INPLACE_V1: «هنوز پرداخت نشده» اعلانِ بالایِ صفحه است نه پیامِ جدید
    ok, msg_, _credit = await AP.settle_charge(row.id, oid)
    if not ok:
        from tenant.gateways import inplace as IP
        await IP.toast(cb, "ℹ️ " + msg_)
    else:
        await cb.answer("✅ پرداخت تأیید شد.")
    # در صورتِ موفقیت، settle_charge خودش رسیدِ شارژ را برایِ کاربر فرستاده و فاکتور را پاک می‌کند.


@apay_router.callback_query(F.data.startswith("apu:cancel:"))
async def apu_cancel(cb: CallbackQuery, bot: Bot):
    """لغو (PATCH_APAY_REMOTE_CANCEL_V1): سمتِ آپی هم لغو می‌شود تا واریزِ بعدی به این فاکتور تطبیق نخورد؛ اگر لغوِ
    سمتِ آپی ممکن نبود (شبکه)، رفتارِ قبلی: لغوِ محلی و ادامهٔ پایش تا ۳۰ دقیقه."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == row.id, ApayCharge.order_id == oid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
    # === PATCH_APAY_REMOTE_CANCEL_V1 === اول سمتِ آپی هم لغو کن تا واریزِ بعدی (مثلاً کارت‌به‌کارتِ عادیِ هم‌مبلغ)
    # به این فاکتور تطبیق نخورد و کیف پول بی‌دلیل شارژ نشود. اگر آپی بگوید پرداخت شده بود، همان‌جا تسویه می‌شود.
    remote = await AP.cancel_remote(row.id, oid)
    if remote == "paid":
        return await cb.answer("✅ پرداختت قبلاً تأیید شده بود و کیف پولت شارژ شد.", show_alert=True)
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == row.id, ApayCharge.order_id == oid))
        if not ch or ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        ch.status = "cancelled"
        ask_chat, ask_msg, inv_chat, inv_msg = ch.admin_ask_chat, ch.admin_ask_msg, ch.chat_id, ch.msg_id
        _cinfo = (int(ch.amount_toman or 0), int(ch.pay_amount_rial or 0), int(ch.fee_toman or 0))
        await s.commit()
    from tenant.gateways.apay_log import log_event as _alog         # PATCH_APAY_LOG_CHANNEL_V1
    await _alog(row.id, "cancelled", user_tg=cb.from_user.id, amount_toman=_cinfo[0], pay_rial=_cinfo[1],
                fee_toman=_cinfo[2], order_id=oid,
                note=("لغو توسطِ کاربر (در آپی هم لغو شد)" if remote == "cancelled" else "لغو توسطِ کاربر"))
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("apay: تمیزکاریِ ظاهریِ پیام ناموفق بود")
    # === PATCH_APAY_ASK_CLEANUP_V2 === لغو کاربر ⇒ پیام «واریز شد؟»ی مالک دیگر لازم نیست
    await AP._drop_admin_ask(row.id, ask_chat, ask_msg)
    if inv_chat and inv_msg and inv_msg != cb.message.message_id:       # لغو از پیام «فاکتور باز دارید» زده شده
        try:
            await bot.edit_message_reply_markup(chat_id=inv_chat, message_id=inv_msg, reply_markup=None)
        except Exception:
            logger.debug("apay: دکمه‌های پیام اصلی فاکتور برداشته نشد")
    from tenant import inline_back_kb
    from tenant.gateways import inplace as IP               # PATCH_GATEWAY_INPLACE_V1: همان پیام «لغو شد» می‌شود
    await IP.show(
        cb,
        ("❌ <b>فاکتور لغو شد.</b>\n\nاگر قبل از لغو واریز کرده بودی، رسیدت را برایِ پشتیبانی بفرست تا دستی شارژ شود."
         if remote == "cancelled" else
         "❌ <b>فاکتور لغو شد.</b>\n\nاگر قبل از لغو واریز کرده بودی نگران نباش؛ "
         "به‌محضِ تأییدِ بانک کیف پولت خودکار شارژ می‌شود."),
        await inline_back_kb(row.id))


# === PATCH_APAY_ADMIN_ASK_V1 === پاسخِ ادمین به «واریز شد؟» (بعد از ۵ دقیقه)
@apay_router.callback_query(F.data.startswith("apadm:"))
async def ap_admin_answer(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        _p, act, cid = cb.data.split(":")
        cid = int(cid)
    except ValueError:
        return await cb.answer()
    if act == "ok":
        ok, msg = await AP.manual_settle(row.id, cid, cb.from_user.id)
        tail = "\n\n✅ <b>تأیید شد و کیف پولِ کاربر شارژ گردید.</b>" if ok else f"\n\n⚠️ {msg}"
    else:
        ok, msg = True, "ثبت شد."
        tail = "\n\n❌ <b>واریز نشده — شارژی انجام نشد.</b>"
    try:
        await cb.message.edit_text((cb.message.html_text or "") + tail, reply_markup=None)
    except Exception:
        logger.info("apay: ویرایشِ پیامِ پرسش ممکن نشد")
    # === PATCH_APAY_ASK_CLEANUP_V2 === پرسش جواب داده شد؛ پیامش (با نتیجه) می‌ماند و دیگر پاک نمی‌شود
    async with Session() as s:
        await s.execute(sa_update(ApayCharge).where(ApayCharge.id == cid, ApayCharge.bot_id == row.id)
                        .values(admin_ask_chat=None, admin_ask_msg=None))
        await s.commit()
    await cb.answer(msg if not ok else ("شارژ شد ✅" if act == "ok" else "ثبت شد"), show_alert=not ok)


# ═════════════════ لاگِ پرداخت‌هایِ آپی در کانال ═════════════════
# === PATCH_APAY_LOG_CHANNEL_V1 ===
async def _log_content(bot_id: int):
    from tenant.gateways import apay_log as AL
    ch = ((await AP._get(bot_id, "apay_log_channel", "")) or "").strip()
    txt = ("📢 <b>لاگِ پرداخت‌هایِ آپی</b>\n\n"
           "هر پرداختِ آپی با مشخصاتِ کاربر (نام، آیدی، یوزرنیم)، مبلغ و ساعت در کانالی که انتخاب کنی "
           "پست می‌شود. هر وضعیت را جدا روشن/خاموش کن:\n"
           "⏳ در حال انجام = فاکتور ساخته شد و منتظرِ واریز است\n"
           "✅ انجام‌شده = واریز تأیید و کیف پول شارژ شد\n"
           "❌ لغوشده = کاربر فاکتور را لغو کرد\n"
           "⌛️ منقضی‌شده = مهلتِ فاکتور بدونِ واریز تمام شد\n\n")
    if ch:
        txt += f"وضعیت: ✅ متصل به <code>{ch}</code>"
    else:
        txt += ("وضعیت: ❌ کانال تنظیم نشده\n"
                "تا وقتی کانالی وصل نکنی، فقط «انجام‌شده» (اگر کانالِ لاگِ تراکنش‌ها هم نداری) به پیویِ تو می‌آید.")
    rows = [[InlineKeyboardButton(text="🔗 اتصال / تغییرِ کانال", callback_data="ap:log:set",
                                  style=ButtonStyle.PRIMARY)]]
    if ch:
        rows.append([InlineKeyboardButton(text="🧪 ارسالِ پیامِ تست", callback_data="ap:log:test",
                                          style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="🗑 حذفِ اتصال", callback_data="ap:log:clear",
                                          style=ButtonStyle.DANGER)])
    for key, (label, _tag, emoji, _d) in AL.EVENTS.items():
        on = await AL.event_enabled(bot_id, key)
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {label}: {'✅ روشن' if on else '❌ خاموش'}", callback_data=f"ap:log:ev:{key}",
            style=(ButtonStyle.SUCCESS if on else ButtonStyle.DANGER))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ap:menu", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_log(target, bot_id: int):
    txt, kb = await _log_content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@apay_router.callback_query(F.data == "ap:log")
async def ap_log_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show_log(cb, row.id)
    await cb.answer()


@apay_router.callback_query(F.data.startswith("ap:log:ev:"))
async def ap_log_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    from tenant.gateways import apay_log as AL
    row = await _owner_ok(cb, bot)
    if not row:
        return
    ev = cb.data.rsplit(":", 1)[-1]
    if ev not in AL.EVENTS:
        return await cb.answer()
    cur = await AL.event_enabled(row.id, ev)
    await AP._set(row.id, f"apay_log_ev_{ev}", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "روشن شد.")
    await _show_log(cb, row.id)


@apay_router.callback_query(F.data == "ap:log:set")
async def ap_log_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(ApFSM.log_channel)
    await cb.message.answer(
        "یک پیام از کانالِ موردنظر را فوروارد کن، یا آیدیِ عددیِ کانال (مثلاً <code>-1001234567890</code>) "
        "را بفرست.\n\n⚠️ ربات باید در آن کانال ادمین باشد.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@apay_router.message(ApFSM.log_channel)
async def ap_log_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    if not row:
        return
    chat_id = None
    fwd = getattr(msg, "forward_from_chat", None)
    if fwd is not None and getattr(fwd, "type", "") == "channel":
        chat_id = fwd.id
    else:
        t = (msg.text or "").strip()
        if t.lstrip("-").isdigit():
            chat_id = int(t)
    if chat_id is None:
        return await msg.answer("یک پیام از کانال فوروارد کن یا آیدیِ عددیِ کانال را بفرست.")
    try:
        await bot.send_message(chat_id, "✅ کانالِ لاگِ پرداخت‌هایِ آپی وصل شد.")
    except Exception as e:
        import html as _h
        return await msg.answer("❌ ارسال به این کانال ممکن نشد. مطمئن شو ربات در کانال <b>ادمین</b> است.\n\n"
                                f"<code>{_h.escape(str(e))}</code>", parse_mode="HTML")
    await AP._set(row.id, "apay_log_channel", str(chat_id))
    await state.clear()
    await msg.answer("✅ ذخیره شد.")
    await _show_log(msg, row.id)


@apay_router.callback_query(F.data == "ap:log:clear")
async def ap_log_clear(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await AP._set(row.id, "apay_log_channel", "")
    await cb.answer("اتصال حذف شد.")
    await _show_log(cb, row.id)


@apay_router.callback_query(F.data == "ap:log:test")
async def ap_log_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    from tenant.gateways import apay_log as AL
    row = await _owner_ok(cb, bot)
    if not row:
        return
    ch = ((await AP._get(row.id, "apay_log_channel", "")) or "").strip()
    if not ch:
        return await cb.answer("کانالی تنظیم نشده.", show_alert=True)
    text = AL.build_text("paid", user=BotUser(full_name="کاربرِ نمونه", username="example"),
                         user_tg=cb.from_user.id, amount_toman=150_000, pay_rial=1_500_960,
                         order_id="TEST-ORDER", balance=200_000, note="پیامِ تست")
    try:
        await bot.send_message(int(ch) if ch.lstrip("-").isdigit() else ch, text, parse_mode="HTML")
    except Exception as e:
        return await cb.answer(f"ارسال نشد: {str(e)[:150]}", show_alert=True)
    await cb.answer("پیامِ تست ارسال شد.", show_alert=True)
