"""
apay_admin.py — PATCH_APAY_GATEWAY_V1

پنلِ تنظیماتِ درگاهِ «آپی» (کارت‌به‌کارتِ آنی — apaygate.shop) برایِ مالکِ ربات + جریانِ شارژِ کاربر.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from config import Session
from models import ApayCharge, BotUser
from tenant.gateways import apay as AP

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

apay_router = Router()


class ApFSM(StatesGroup):
    token = State()
    secret = State()
    amount = State()
    min_amount = State()
    max_amount = State()
    display_name = State()   # === PATCH_APAY_DISPLAY_NAME_V1 ===


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
    hook = await AP.webhook_url(bot_id)
    disp = await get_display_name(bot_id)

    txt = ("💳 <b>درگاهِ آپی (کارت‌به‌کارتِ آنی)</b>\n\n"
           "کاربر مبلغ را وارد می‌کند، در صفحهٔ پرداختِ آپی شمارهٔ کارتِ تو را می‌بیند و "
           "کارت‌به‌کارت می‌کند؛ به‌محضِ رسیدنِ پیامکِ بانک به گوشیِ تو، کیف پولش "
           "<b>خودکار</b> شارژ می‌شود. پول مستقیم به کارتِ خودت می‌آید.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکنِ API: <code>{_mask(tok)}</code>\n"
           f"🏷 اسمِ نمایشی برایِ کاربر: <b>{disp}</b>\n"
           f"🔐 کلیدِ وب‌هوک: {'✅ ثبت شده' if sec else '— (اختیاری؛ بدونِ آن هم با استعلام کار می‌کند)'}\n"
           f"🔽 حداقلِ مبلغ: <b>{min_amount:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{max_amount:,}' if max_amount else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n\n"
           "📖 برایِ راه‌اندازیِ قدم‌به‌قدم و اتصالِ گوشیِ اندروید/آیفون، «📖 راهنما» را بزن.\n\n"
           + (f"🔗 آدرسِ وب‌هوک:\n<code>{hook}</code>\n\n" if hook else "")
           + "💡 کارمزدِ آپی برایِ هر پرداختِ موفق ۱٬۰۰۰ تومان است و از کیف پولِ حسابِ "
           "آپیِ تو کم می‌شود (نه از مشتری)؛ آن کیف پول را شارژ نگه دار.\n"
           "💡 مشتری باید <b>دقیقاً</b> مبلغِ نمایش‌داده‌شده در صفحهٔ پرداخت را واریز کند "
           "(چند تومانِ یکتا اضافه دارد)؛ کیف پولش به اندازهٔ مبلغی که وارد کرده شارژ می‌شود.")

    rows = [
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"),
            callback_data="ap:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ توکن",
                              callback_data="ap:setkey", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="🔐 کلیدِ وب‌هوک",
                              callback_data="ap:setsecret", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🏷 اسمِ نمایشی: {disp}",
                              callback_data="ap:setname", style=ButtonStyle.PRIMARY)],
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
               "۵. «💰 شارژ کیف پول» در رباتِ آپی را برایِ کارمزد (هر پرداخت ۱٬۰۰۰ تومان) شارژ کن.\n"
               "۶. «✅ فعال‌کردن» را بزن و با «🧪 تستِ اتصال و موجودی» چک کن.\n\n"
               "⚡️ <b>اختیاری (شارژِ فوری):</b> در رباتِ آپی از «👤 پروفایل» webhook secret را بردار و "
               "اینجا «🔐 کلیدِ وب‌هوک» را بزن"
               + (f"؛ آدرسِ وب‌هوک:\n<code>{hook}</code>" if hook else ".")
               + "\nبدونِ این مرحله هم ربات هر دقیقه خودش پرداخت‌ها را استعلام می‌کند.")
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
@apay_router.callback_query(F.data.in_({"apu:start", "w:apay"}))
async def apu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await AP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
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

    try:
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await msg.answer(why)
    except Exception:
        logger.exception("apay: بررسیِ سقف ناموفق بود")

    token = await AP._get(row.id, "apay_token", "")
    if not token:
        return await msg.answer("درگاه تنظیم نشده است.")

    oid = AP.new_order_id()
    wait = await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    code, res = await AP.create_payment(
        token, amount_toman=int(amount), order_id=oid,
        description="شارژ کیف پول",   # بدونِ آیدیِ کاربر؛ در صفحهٔ پرداخت دیده می‌شود
        callback_url=await AP.webhook_url(row.id),
        return_url=(f"https://t.me/{row.username}" if row.username else ""))
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        # دامنهٔ وب‌هوک را نپذیرفت؛ بدونِ callback (با استعلامِ دوره‌ای) دوباره
        code, res = await AP.create_payment(
            token, amount_toman=int(amount), order_id=oid,
            return_url=(f"https://t.me/{row.username}" if row.username else ""))
    if code not in (200, 201) or not isinstance(res, dict) or not res.get("pay_url"):
        logger.warning("apay: ساختِ پرداخت ناموفق (bot=%s): %s %s", row.id, code, str(res)[:300])
        return await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + AP.err_text(code, res))

    pay_rial = int(res.get("pay_amount") or 0)
    async with Session() as s:
        s.add(ApayCharge(
            bot_id=row.id, user_telegram_id=msg.from_user.id, order_id=oid,
            payment_id=str(res.get("id") or ""), amount_toman=int(amount),
            pay_amount_rial=pay_rial or None, pay_url=str(res["pay_url"])[:300],
            status="waiting"))
        await s.commit()
    try:
        await wait.delete()
    except Exception:
        logger.debug("apay: حذفِ پیامِ انتظار ناموفق بود")

    card, holder = await AP.card_for(token, res)
    text, kb = AP.invoice_view(
        title=await get_display_name(row.id), pay_rial=pay_rial, card=card, holder=holder,
        order_id=oid, pay_url=str(res["pay_url"]), check_cb=f"apu:check:{oid}",
        cancel_cb=f"apu:cancel:{oid}")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


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
    await cb.answer("در حالِ استعلام…")
    ok, msg_, _credit = await AP.settle_charge(row.id, oid)
    if not ok:
        await cb.message.answer("ℹ️ " + msg_)
    # در صورتِ موفقیت، settle_charge خودش رسیدِ شارژ را برایِ کاربر فرستاده است.


@apay_router.callback_query(F.data.startswith("apu:cancel:"))
async def apu_cancel(cb: CallbackQuery, bot: Bot):
    """لغو فقط سمتِ ماست؛ اگر کاربر بعداً همان فاکتور را پرداخت کند، وب‌هوکِ آپی
    همچنان کیف پولش را شارژ می‌کند (پول نمی‌سوزد)."""
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
        ch.status = "cancelled"
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("apay: تمیزکاریِ ظاهریِ پیام ناموفق بود")
    from tenant import inline_back_kb
    await cb.message.answer(
        "❌ فاکتور لغو شد.\n\nاگر قبل از لغو واریز کرده بودی نگران نباش؛ "
        "به‌محضِ تأییدِ بانک کیف پولت خودکار شارژ می‌شود.",
        reply_markup=await inline_back_kb(row.id))
