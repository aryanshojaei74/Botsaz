"""
abangateway_admin.py — PATCH_ABANGATEWAY_V1

پنلِ تنظیماتِ درگاهِ آبان گیت‌وی برایِ مالکِ ربات + جریانِ شارژِ کاربر.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, CopyTextButton, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from config import Session
from models import BotUser, AbanGatewayCharge
from plans import Feature, has_feature
from tenant.gateways import abangateway as AG

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

abangateway_router = Router()


class AbFSM(StatesGroup):
    token = State()
    amount = State()
    min_amount = State()
    max_amount = State()
    display_name = State()  # === PATCH_ABANGATEWAY_DISPLAY_NAME_V1 ===


# === PATCH_ABANGATEWAY_QUICK_AMOUNTS_V1 ===
# دقیقاً مثلِ دکمه‌هایِ پیش‌فرضِ شارژِ کارت‌به‌کارت (tenant/card_wallet.py:
# WALLET_PRESETS) — چند دکمهٔ مبلغِ آماده + «مبلغ دلخواه» + «لغو».
AB_PRESETS = [50000, 100000, 200000, 500000]


def ab_amount_kb(min_amount: int, max_amount: int, colored: bool = False) -> InlineKeyboardMarkup:
    from tenant import rial
    succ = ButtonStyle.SUCCESS if colored else None
    prim = ButtonStyle.PRIMARY if colored else None
    dang = ButtonStyle.DANGER if colored else None
    presets = [p for p in AB_PRESETS if p >= min_amount and (not max_amount or p <= max_amount)]
    rows = []
    for i in range(0, len(presets), 2):
        pair = presets[i:i + 2]
        rows.append([InlineKeyboardButton(text=rial(p), callback_data=f"abpre:{p}", style=succ)
                     for p in pair])
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه", callback_data="abpre:custom", style=prim)])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="abpre:cancel", style=dang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_card(card_number: str | None) -> str:
    # === PATCH_ABANGATEWAY_CARD_COPY_FIX_V1 ===
    # گزارشِ ادمین (ایلیا): خط‌تیره‌هایِ بینِ چهاررقمی‌ها باعث می‌شد وقتی
    # کاربر رویِ کدِ کپی‌شونده (<code>) می‌زد، همان خط‌تیره‌ها هم کپی
    # می‌شدند و پیست‌کردن در اپِ بانک (که فقط رقم می‌خواهد) خراب می‌شد.
    # حالا فقط خودِ رقم‌ها، بدونِ هیچ جداکننده‌ای، برگردانده می‌شود.
    if not card_number:
        return "—"
    digits = "".join(c for c in card_number if c.isdigit())
    return digits or card_number


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    tok = await AG._get(bot_id, "abangateway_token", "")
    on = await AG._get(bot_id, "abangateway_enabled", "0") == "1"
    show_wallet = await AG._get(bot_id, "abangateway_show_wallet", "1") == "1"
    show_purchase = await AG._get(bot_id, "abangateway_show_purchase", "1") == "1"
    # === PATCH_ABANGATEWAY_REPLACE_CARD_V1 === / === PATCH_ABANGATEWAY_PAYMENT_PAGE_TOGGLE_V1 ===
    replace_card = await AG._get(bot_id, "abangateway_replace_card", "0") == "1"
    show_page = await AG._get(bot_id, "abangateway_show_payment_page", "1") != "0"
    min_amount = await AG.get_min_amount(bot_id)
    max_amount = await AG.get_max_amount(bot_id)
    disp_name = await AG.get_display_name(bot_id)  # === PATCH_ABANGATEWAY_DISPLAY_NAME_V1 ===
    masked = (tok[:5] + "…" + tok[-4:]) if tok and len(tok) > 12 else (tok or "—")
    mode = "🧪 آزمایشی" if tok.startswith("ag_test_") else ("🔴 واقعی" if tok else "—")

    txt = ("💳 <b>درگاهِ آبان گیت‌وی</b>\n\n"
           "کارت‌به‌کارتِ خودکار با تأییدِ آنی: کاربر کارت‌به‌کارت می‌کند، سیستمِ "
           "خودِ آبان گیت‌وی پیامکِ بانک را می‌خواند و ربات ظرفِ چند ثانیه کیفِ "
           "پولش را شارژ می‌کند.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکن: <code>{masked}</code> ({mode})\n"
           f"🏷 اسمِ نمایشی به کاربر: <b>{disp_name}</b>\n"
           f"🔽 حداقلِ مبلغ: <b>{min_amount:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{max_amount:,}' if max_amount else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n"
           f"🔁 جایگزینِ کارت‌به‌کارتِ دستی: {'✅' if replace_card else '❌'}\n"
           f"🌐 دکمهٔ «صفحهٔ پرداخت» (ورود به سایتِ درگاه): {'✅' if show_page else '❌'}\n\n"
           "همه‌جایِ ربات (نمایش، ورودی، شارژ) فقط تومان است؛ تبدیل به ریال "
           "برایِ ارسال به درگاه کاملاً داخلی و خودکار انجام می‌شود.\n\n"
           "🔑 توکن را از پنلِ abangateway.ir یا ربات <code>@Abangw_bot</code> بگیر "
           "(پیشوندِ <code>ag_live_</code> برایِ واقعی، <code>ag_test_</code> برایِ آزمایشی).")

    rows = [
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"),
            callback_data="ab:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ توکن",
                              callback_data="ab:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🏷 تغییرِ اسمِ نمایشی: {disp_name}",
                              callback_data="ab:setname", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مبلغ: {min_amount:,}",
                              callback_data="ab:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {max_amount:,}" if max_amount else "🔼 حداکثر: بدونِ سقف",
                              callback_data="ab:setmax", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}",
            callback_data="ab:show_wallet",
            style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(
            text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
            callback_data="ab:show_purchase",
            style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(
            text=f"🔁 جایگزینِ کارت‌به‌کارتِ دستی: {'✅' if replace_card else '❌'}",
            callback_data="ab:replace_card",
            style=(ButtonStyle.SUCCESS if replace_card else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(
            text=f"🌐 دکمهٔ صفحهٔ پرداخت: {'✅' if show_page else '❌'}",
            callback_data="ab:show_page",
            style=(ButtonStyle.SUCCESS if show_page else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="🧪 تستِ اتصال",
                              callback_data="ab:test", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📖 راهنما، ثبت‌نام و نصبِ اپ",
                              callback_data="ab:guide", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت",
                              callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_ABANGATEWAY_GUIDE_V1 ===
# درخواستِ ادمین: راهنمایِ ثبت‌نام و نصبِ اپ (با دکمه‌هایِ اینلاینِ مستقیم به
# لینکِ دانلود/ثبت‌نام، نه فقط متن) + تذکراتِ استفاده، همین‌جا تو تنظیماتِ
# مالی/کیفِ‌پول، کنارِ خودِ درگاه.
_AG_SITE = "https://abangateway.ir"

_AG_GUIDE_TEXT = (
    "📖 <b>راهنمایِ راه‌اندازیِ آبان گیت‌وی</b>\n\n"
    "۱️⃣ رویِ «📝 ساختِ حسابِ رایگان» بزن و یک حساب بساز (یا اگر از قبل داری، "
    "«🔑 ورود به حساب»).\n"
    "۲️⃣ کارتِ بانکی‌ای که می‌خواهی پول‌ها به آن بنشیند را در پنلِ آبان گیت‌وی "
    "ثبت کن.\n"
    "۳️⃣ اپِ آبان گیت‌وی را رویِ همان گوشی‌ای نصب کن که سیمکارتِ بانکی و پیامک‌هایش "
    "را دریافت می‌کند — اندروید از گوگل‌پلی یا بازار، آیفون با شورتکاتِ مخصوص.\n"
    "۴️⃣ داخلِ پنلِ آبان گیت‌وی (یا با ربات تلگرامِ @Abangw_bot)، یک توکنِ API "
    "بساز (پیشوندِ <code>ag_live_</code>).\n"
    "۵️⃣ برگرد به همین صفحه (⬅️ بازگشت) و توکن را با «🔑 ثبت / تغییرِ توکن» ثبت کن.\n\n"
    "اگه گیر کردی، «📚 آموزشِ شروع به کار» یا «🌐 وب‌سایتِ آبان گیت‌وی» رو بزن.\n\n"
    "⚠️ <b>تذکراتِ مهمِ استفاده:</b>\n"
    "• اپِ گوشی باید همیشه نصب، روشن و متصل بمونه؛ تشخیصِ خودکارِ واریز از رویِ "
    "همون پیامکِ بانکه — اگه گوشی خاموش/بدونِ سیمکارت بمونه، پرداخت‌ها خودکار "
    "تأیید نمی‌شن (طبقِ خودِ آبان گیت‌وی حتی با قطعیِ اینترنتِ گوشی هم با یک "
    "پیامکِ امن به سرور کار متوقف نمی‌شه، ولی خودِ گوشی و سیمکارت باید فعال باشن).\n"
    "• کاربر باید دقیقاً همون مبلغِ «قابلِ‌پرداخت»ی که ربات نشون می‌ده رو واریز "
    "کنه (نه مبلغِ گردشده) — چند ریالِ اضافه‌ش دقیقاً همونیه که فاکتورش رو از "
    "بقیه جدا می‌کنه؛ اگه عددِ دیگه‌ای بزنه، تشخیصِ خودکار کار نمی‌کنه.\n"
    "• توکنِ API رو جایی منتشر نکن و تو کدِ سمتِ مرورگر/کلاینت نذار — هرکی "
    "توکن رو داشته باشه می‌تونه به‌جایِ تو فاکتور بسازه.\n"
    "• اگه کارتِ ثبت‌شده رو عوض کنی، لازمه دوباره توکن/تنظیماتِ آبان گیت‌وی رو "
    "چک کنی تا واریزی‌هایِ جدید درست تشخیص داده بشن.\n"
    "• این یک سرویسِ مستقل از بات‌ساز است؛ در دسترس‌بودن، کارمزد و پشتیبانیِ "
    "فنی‌اش را خودِ آبان گیت‌وی مدیریت می‌کند."
)


def _guide_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ساختِ حسابِ رایگان", url=f"{_AG_SITE}/signup"),
         InlineKeyboardButton(text="🔑 ورود به حساب", url=f"{_AG_SITE}/login")],
        [InlineKeyboardButton(text="📚 آموزشِ شروع به کار", url=f"{_AG_SITE}/docs/start")],
        [InlineKeyboardButton(text="🤖 ربات تلگرامِ آبان گیت‌وی (دریافتِ توکن)", url="https://t.me/Abangw_bot")],
        [InlineKeyboardButton(text="🤖 نصبِ اپِ اندروید (گوگل‌پلی)",
                              url="https://play.google.com/store/apps/details?id=ir.abangateway.forwarder")],
        [InlineKeyboardButton(text="🛒 نصبِ اپِ اندروید (بازار)",
                              url="https://cafebazaar.ir/app/?id=ir.abangateway.forwarder&ref=share")],
        [InlineKeyboardButton(text="🍏 نصبِ شورتکاتِ آیفون", url=f"{_AG_SITE}/docs/iphone-shortcut")],
        [InlineKeyboardButton(text="🧠 چطور واریزی به فاکتور تشخیص داده می‌شود",
                              url=f"{_AG_SITE}/docs/how-matching-works")],
        [InlineKeyboardButton(text="🌐 وب‌سایتِ آبان گیت‌وی", url=_AG_SITE)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:abangateway", style=ButtonStyle.DANGER)],
    ])


@abangateway_router.callback_query(F.data == "ab:guide")
async def ab_guide(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        await cb.message.edit_text(_AG_GUIDE_TEXT, reply_markup=_guide_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(_AG_GUIDE_TEXT, reply_markup=_guide_kb(), parse_mode="HTML")
    await cb.answer()


async def _show(target, bot_id):
    txt, kb = await _menu_content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@abangateway_router.callback_query(F.data.in_({"set:abangateway", "ab:menu"}))
async def ab_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@abangateway_router.callback_query(F.data == "ab:toggle")
async def ab_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG._get(row.id, "abangateway_enabled", "0") == "1"
    if not cur and not await AG._get(row.id, "abangateway_token", ""):
        return await cb.answer("اول توکنِ درگاه را ثبت کن.", show_alert=True)
    await AG._set(row.id, "abangateway_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


@abangateway_router.callback_query(F.data == "ab:show_wallet")
async def ab_show_wallet(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG._get(row.id, "abangateway_show_wallet", "1") == "1"
    await AG._set(row.id, "abangateway_show_wallet", "0" if cur else "1")
    await cb.answer("در کیف پول: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@abangateway_router.callback_query(F.data == "ab:show_purchase")
async def ab_show_purchase(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG._get(row.id, "abangateway_show_purchase", "1") == "1"
    await AG._set(row.id, "abangateway_show_purchase", "0" if cur else "1")
    await cb.answer("موقعِ پرداختِ محصول: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


# === PATCH_ABANGATEWAY_REPLACE_CARD_V1 ===
@abangateway_router.callback_query(F.data == "ab:replace_card")
async def ab_replace_card(cb: CallbackQuery, bot: Bot):
    """آبان گیت‌وی را به‌جایِ کارت‌به‌کارتِ دستی بگذار (یا برگردان). تا وقتی خودِ
    درگاه فعال و متصل نباشد، هیچ اثری ندارد — پس ادمین بی‌روشِ پرداخت نمی‌ماند."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG._get(row.id, "abangateway_replace_card", "0") == "1"
    if not cur and not await AG.is_available(row.id):
        return await cb.answer(
            "اول درگاه را فعال کن و توکن را ثبت کن؛ وگرنه کارت‌به‌کارتِ دستی "
            "را هم از دست می‌دهی.", show_alert=True)
    await AG._set(row.id, "abangateway_replace_card", "0" if cur else "1")
    await cb.answer("کارت‌به‌کارتِ دستی: " + ("دوباره نمایش داده می‌شود" if cur
                                              else "با آبان گیت‌وی جایگزین شد"))
    await _show(cb, row.id)


# === PATCH_ABANGATEWAY_PAYMENT_PAGE_TOGGLE_V1 ===
@abangateway_router.callback_query(F.data == "ab:show_page")
async def ab_show_page(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG._get(row.id, "abangateway_show_payment_page", "1") != "0"
    await AG._set(row.id, "abangateway_show_payment_page", "0" if cur else "1")
    await cb.answer("دکمهٔ صفحهٔ پرداخت: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@abangateway_router.callback_query(F.data == "ab:setmin")
async def ab_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG.get_min_amount(row.id)
    await state.set_state(AbFSM.min_amount)
    await cb.message.answer(
        f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>).",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@abangateway_router.message(AbFSM.min_amount, F.text)
async def ab_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n <= 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await AG._set(row.id, "abangateway_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@abangateway_router.callback_query(F.data == "ab:setmax")
async def ab_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG.get_max_amount(row.id)
    await state.set_state(AbFSM.max_amount)
    await cb.message.answer(
        f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: "
        f"<b>{(f'{cur:,}' if cur else 'بدونِ سقف')}</b>).\n\n"
        "برای برداشتنِ سقف، عددِ ۰ بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@abangateway_router.message(AbFSM.max_amount, F.text)
async def ab_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await AG._set(row.id, "abangateway_max_amount", str(int(n)))
    await msg.answer("✅ حداکثرِ مبلغ " + (f"روی {int(n):,} تومان تنظیم شد." if n else "برداشته شد (بدونِ سقف)."))
    await _show(msg, row.id)


# === PATCH_ABANGATEWAY_DISPLAY_NAME_V1 ===
@abangateway_router.callback_query(F.data == "ab:setname")
async def ab_setname(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """مالکِ ربات می‌تواند به‌جایِ «آبان گیت‌وی» هر اسمی که دوست دارد به
    کاربر نشان بدهد (تویِ کیفِ پول و پرداختِ محصول) — خودِ اتصال/API تغییری
    نمی‌کند، فقط برچسبِ نمایشی."""
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await AG.get_display_name(row.id)
    await state.set_state(AbFSM.display_name)
    await cb.message.answer(
        f"🏷 اسمی که می‌خواهی به‌جایِ «آبان گیت‌وی» به کاربر نشان داده شود را "
        f"بفرست (فعلاً: <b>{cur}</b>؛ حداکثر ۴۰ حرف).\n\n"
        "برایِ بازگشت به اسمِ پیش‌فرض، کلمهٔ <code>پیش‌فرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@abangateway_router.message(AbFSM.display_name, F.text)
async def ab_setname_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    name = (msg.text or "").strip()
    if name in ("پیش‌فرض", "پیشفرض", "default", "-"):
        await AG._set(row.id, "abangateway_display_name", "")
        await msg.answer(f"✅ اسمِ نمایشی به پیش‌فرض (<b>{AG.DEFAULT_DISPLAY_NAME}</b>) برگشت.", parse_mode="HTML")
    elif not name:
        await msg.answer("اسمِ نامعتبر. دوباره امتحان کن.")
        return
    else:
        name = name[:40]
        await AG._set(row.id, "abangateway_display_name", name)
        await msg.answer(f"✅ اسمِ نمایشی رویِ «{name}» تنظیم شد.")
    await _show(msg, row.id)


@abangateway_router.callback_query(F.data == "ab:setkey")
async def ab_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(AbFSM.token)
    await cb.message.answer(
        "🔑 توکنِ حسابِ آبان گیت‌وی را بفرست (با پیشوندِ <code>ag_live_</code> یا "
        "<code>ag_test_</code>).\n\nهمین‌الان تستِ خودکار انجام و اگر معتبر بود "
        "ذخیره می‌شود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@abangateway_router.message(AbFSM.token, F.text)
async def ab_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    tok = (msg.text or "").strip()
    if len(tok) < 10:
        return await msg.answer("توکن خیلی کوتاه است. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ توکن…")
    ok, why = await AG.validate_token(row.id, tok, row.username or "")
    if not ok:
        return await msg.answer(f"❌ {why}")
    await AG._set(row.id, "abangateway_token", tok)
    await msg.answer(why, parse_mode="HTML")
    await _show(msg, row.id)


@abangateway_router.callback_query(F.data == "ab:test")
async def ab_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    tok = await AG._get(row.id, "abangateway_token", "")
    if not tok:
        return await cb.answer("اول توکن را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await AG.validate_token(row.id, tok, row.username or "")
    await cb.message.answer(why if ok else f"❌ {why}", parse_mode="HTML")


# ═════════════════ جریانِ کاربر ═════════════════
# === PATCH_ABANGATEWAY_QUICK_AMOUNTS_V1 ===
# قبلاً فقط ورودیِ متنیِ آزاد بود؛ حالا دقیقاً مثلِ شارژِ کارت‌به‌کارت، اول
# چند دکمهٔ مبلغِ آماده (۵۰/۱۰۰/۲۰۰/۵۰۰ هزار تومان، درونِ محدودهٔ مجاز)
# + دکمهٔ «مبلغ دلخواه» + دکمهٔ «لغو» نشان داده می‌شود.
# === PATCH_ABANGATEWAY_VPN_PLAN_RESUME_V1 ===
@abangateway_router.callback_query(F.data.startswith("w:abangateway:vpn:"))
async def abu_start_for_vpn_plan(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پرداختِ مستقیمِ یک پلنِ VPN (نه سفارشِ فروشگاه — VpnPlan اصلاً Order
    نمی‌سازد). فرمت: w:abangateway:vpn:{plan_id}:{قیمتِ_کامل_تومان}.

    قیمت همان لحظه‌ای است که صفحهٔ خرید نشانش داده (شاملِ هر تخفیفی)؛
    این‌جا فقط کسری‌اش با موجودیِ لحظه‌ای حساب می‌شود، دقیقاً مثلِ نسخهٔ
    Order-محور (abu_start_for_order). اگر کاربر callback_data را دست‌کاری
    کند حداکثر می‌تواند مبلغِ فاکتورِ خودش را کم کند — چون تکمیلِ نهاییِ
    خرید (vpay:{plan_id}) مستقل و دوباره از رویِ خودِ پلن و موجودیِ واقعی
    محاسبه می‌شود، امنیتی به آن وابسته نیست.
    """
    from tenant import get_bot_row
    from models import VpnPlan
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if await AG._get(row.id, "abangateway_enabled", "0") != "1":
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    try:
        _, _, _, plan_id_s, price_s = cb.data.split(":")
        plan_id, total_price = int(plan_id_s), int(price_s)
    except (ValueError, IndexError):
        return await cb.answer()

    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != row.id or not plan.active:
            return await cb.answer("این پلن دیگر در دسترس نیست.", show_alert=True)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        need = total_price - int(float(u.balance or 0)) if u else total_price

    if need <= 0:
        # === PATCH_ABANGATEWAY_REPLACE_CARD_V1 ===
        # در حالتِ «جایگزینِ کارت‌به‌کارت»، این دکمه همان نقشِ کارت‌به‌کارتِ مستقیم را
        # دارد؛ پس حتی با موجودیِ کافی، کاربر می‌تواند کلِ مبلغ را از این راه بدهد
        # (موجودیِ قبلی‌اش دست‌نخورده می‌ماند). در حالتِ عادی مثلِ قبل جلوگیری می‌شود.
        if await AG.replaces_card(row.id):
            need = total_price
        else:
            return await cb.answer(
                "موجودیِ کیف‌پولت کافی است — همان «✅ پرداخت از کیف پول» را بزن.",
                show_alert=True)

    min_amount = await AG.get_min_amount(row.id)
    max_amount = await AG.get_max_amount(row.id)
    if need < min_amount:
        need = min_amount  # کمتر از حداقلِ درگاه؛ باقی‌اش در کیفِ پول می‌ماند
    if max_amount and need > max_amount:
        return await cb.answer(
            f"مبلغِ لازم از سقفِ درگاه ({max_amount:,} تومان) بیشتر است. "
            "از روشِ دیگری پرداخت کن.", show_alert=True)

    await state.clear()
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    # === PATCH_ABANGATEWAY_VPN_PLAN_RESUME_V1 ===
    # منفی‌بودنِ for_order یعنی «رزومِ پلنِ VPN»؛ نگاه کن به توضیحِ کاملش
    # در _notify_paid (tenant/gateways/abangateway.py).
    await _abu_create_charge(cb.message, bot, row, int(need),
                             cb.from_user.id, for_order=-plan_id)




# === PATCH_ABANGATEWAY_VC_DIRECT_V1 ===
# رمزگذاریِ «خریدِ دلخواهِ VPN» در ستونِ Integerِ for_order_id (بدونِ migration):
#   −(1_000_000_000 + gb·2^16 + days·2^5 + count) ؛ پلن‌ها −plan_id (خیلی کوچک‌تر) می‌مانند.
VC_RESUME_BASE = 1_000_000_000


def vc_pack(gb: int, days: int, count: int) -> int:
    return -(VC_RESUME_BASE + (gb << 16) + (days << 5) + count)


def vc_unpack(for_order: int) -> tuple[int, int, int] | None:
    v = -int(for_order) - VC_RESUME_BASE
    if v < 0:
        return None
    return v >> 16, (v >> 5) & 0x7FF, v & 0x1F


@abangateway_router.callback_query(F.data.startswith("w:abangateway:vc:"))
async def abu_start_for_vc(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پرداختِ مستقیمِ خریدِ دلخواهِ VPN: w:abangateway:vc:{gb}:{days}:{count}:{مبلغِ_فاکتور}.
    فقط مبلغِ کسری با موجودی حساب می‌شود؛ تکمیلِ خرید (vc:pay) خودش دوباره قیمت و موجودی
    را از نو حساب می‌کند، پس دست‌کاریِ callback امنیتی ایجاد نمی‌کند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if await AG._get(row.id, "abangateway_enabled", "0") != "1":
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    try:
        _, _, _, gb_s, days_s, count_s, price_s = cb.data.split(":")
        gb, days, count, total_price = int(gb_s), int(days_s), int(count_s), int(price_s)
    except (ValueError, IndexError):
        return await cb.answer()
    if not (0 <= gb <= 4095 and 0 <= days <= 2047 and 1 <= count <= 31) or total_price <= 0:
        return await cb.answer()
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        need = total_price - int(float(u.balance or 0)) if u else total_price
    if need <= 0:
        if await AG.replaces_card(row.id):
            need = total_price
        else:
            return await cb.answer(
                "موجودیِ کیف‌پولت کافی است — همان «✅ پرداخت از کیف پول» را بزن.",
                show_alert=True)
    min_amount = await AG.get_min_amount(row.id)
    max_amount = await AG.get_max_amount(row.id)
    if need < min_amount:
        need = min_amount
    if max_amount and need > max_amount:
        return await cb.answer(
            f"مبلغِ لازم از سقفِ درگاه ({max_amount:,} تومان) بیشتر است. "
            "از روشِ دیگری پرداخت کن.", show_alert=True)
    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count)
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    await _abu_create_charge(cb.message, bot, row, int(need),
                             cb.from_user.id, for_order=vc_pack(gb, days, count))


@abangateway_router.callback_query(F.data.startswith("w:abangateway:"))
async def abu_start_for_order(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_GATEWAY_PAY_FOR_ORDER_V1 ===
    پرداختِ مستقیمِ یک سفارش: مبلغ خودکار همان کسریِ سفارش است، پس نه
    انتخابِ دستیِ مبلغ لازم است نه احتمالِ کم‌زدن.
    """
    from tenant import get_bot_row
    from models import Order
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if await AG._get(row.id, "abangateway_enabled", "0") != "1":
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    try:
        oid = int(cb.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return await cb.answer()

    async with Session() as s:
        o = await s.get(Order, oid)
        if not o or o.bot_id != row.id or o.status != "pending":
            return await cb.answer("این سفارش دیگر معتبر نیست.", show_alert=True)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u or o.user_id != u.id:
            return await cb.answer("این سفارش مالِ تو نیست.", show_alert=True)
        need = int(float(o.amount)) - int(float(u.balance or 0))

    if need <= 0:
        return await cb.answer(
            "موجودیِ کیف‌پولت کافی است — همان «💰 پرداخت از کیف پول» را بزن.",
            show_alert=True)

    min_amount = await AG.get_min_amount(row.id)
    max_amount = await AG.get_max_amount(row.id)
    if need < min_amount:
        # کمتر از حداقلِ درگاه؛ همان حداقل شارژ می‌شود و باقی‌اش در
        # کیفِ پول می‌ماند — بهتر از بن‌بست.
        need = min_amount
    if max_amount and need > max_amount:
        return await cb.answer(
            f"مبلغِ این سفارش از سقفِ درگاه ({max_amount:,} تومان) بیشتر است. "
            "از روشِ دیگری پرداخت کن.", show_alert=True)

    await state.clear()
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    await _abu_create_charge(cb.message, bot, row, int(need),
                             cb.from_user.id, for_order=oid)


@abangateway_router.callback_query(F.data.in_({"abu:start", "w:abangateway"}))
async def abu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, edit_or_replace
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if await AG._get(row.id, "abangateway_enabled", "0") != "1":
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    await state.clear()
    disp_name = await AG.get_display_name(row.id)
    min_amount = await AG.get_min_amount(row.id)
    max_amount = await AG.get_max_amount(row.id)
    rng = f"حداقل: <b>{min_amount:,}</b> تومان"
    if max_amount:
        rng += f" | حداکثر: <b>{max_amount:,}</b> تومان"
    colored = bool(has_feature(row.plan, Feature.COLORED_BUTTONS))
    kb = ab_amount_kb(min_amount, max_amount, colored)
    txt = f"💳 <b>شارژ با {disp_name}</b>\n\nمبلغ را انتخاب کن یا «✏️ مبلغ دلخواه» را بزن.\n\n{rng}"
    try:
        await edit_or_replace(cb.message, txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@abangateway_router.callback_query(F.data.startswith("abpre:"))
async def abu_pick_amount(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """انتخابِ یکی از دکمه‌هایِ مبلغِ آماده، یا «مبلغ دلخواه»، یا «لغو»."""
    from tenant import get_bot_row, smart_cancel_kb, edit_or_replace
    val = cb.data.split(":", 1)[1]
    if val == "cancel":
        await state.clear()
        await cb.answer("لغو شد.")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
        return
    if val == "custom":
        row = await get_bot_row(bot)
        min_amount = await AG.get_min_amount(row.id) if row else AG.MIN_AMOUNT
        await state.set_state(AbFSM.amount)
        await cb.answer()
        await cb.message.answer(
            f"💳 مبلغِ دلخواه را به تومان بفرست (فقط عدد، حداقل {min_amount:,}):",
            reply_markup=await smart_cancel_kb(bot))
        return
    try:
        amount = int(val)
    except ValueError:
        await cb.answer()
        return
    await cb.answer("⏳ در حالِ ساختِ فاکتور…")
    row = await get_bot_row(bot)
    if not row:
        return
    await _abu_create_charge(cb.message, bot, row, amount, cb.from_user.id)


@abangateway_router.message(AbFSM.amount, F.text)
async def abu_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = fa_to_int(msg.text)
    min_amount = await AG.get_min_amount(row.id)
    max_amount = await AG.get_max_amount(row.id)
    if not amount or amount < min_amount:
        return await msg.answer(f"حداقلِ مبلغ {min_amount:,} تومان است. دوباره بفرست.")
    if max_amount and amount > max_amount:
        return await msg.answer(f"حداکثرِ مبلغ {max_amount:,} تومان است. دوباره بفرست.")
    await state.clear()
    await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    await _abu_create_charge(msg, bot, row, int(amount), msg.from_user.id)


async def _abu_create_charge(message: Message, bot: Bot, row, amount: int, tg_id: int,
                             for_order: int | None = None) -> None:
    """ساختِ فاکتور و نمایشِ کارتِ مقصد — مشترک بینِ مسیرِ دکمهٔ آمادهٔ مبلغ و
    مسیرِ ورودیِ متنیِ مبلغِ دلخواه.

    === PATCH_GATEWAY_PAY_FOR_ORDER_V1 ===
    for_order: اگر این پرداخت از دلِ «پرداختِ محصول» آمده، شمارهٔ همان سفارش.
    """
    disp_name = await AG.get_display_name(row.id)  # === PATCH_ABANGATEWAY_DISPLAY_NAME_V1 ===
    # سقفِ احرازِ هویت
    try:
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == tg_id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await message.answer(why)
    except Exception:
        logger.exception("abangateway: بررسیِ سقف ناموفق بود")

    token = await AG._get(row.id, "abangateway_token", "")
    if not token:
        return await message.answer("درگاه تنظیم نشده است.")

    oid = AG.new_order_id()
    ok, err, body = await AG.create_invoice_safe(row.id, token, int(amount), oid, row.username or "")
    if not ok:
        return await message.answer(f"❌ ساختِ فاکتور با «{disp_name}» ناموفق بود.\n{err}")

    invoice_id = body.get("invoice_id")
    payable_toman = int(body.get("payable_toman") or amount)
    card_number = body.get("card_number") or ""
    card_holder = body.get("card_holder") or ""
    payment_url = body.get("payment_url") or ""

    async with Session() as s:
        s.add(AbanGatewayCharge(
            bot_id=row.id, user_telegram_id=tg_id,
            order_id=oid, invoice_id=invoice_id,
            amount_toman=int(amount), payable_toman=payable_toman,
            card_number=card_number, card_holder=card_holder, status="waiting",
            for_order_id=for_order))
        await s.commit()

    kb_rows = []
    # === PATCH_ABANGATEWAY_PAYMENT_PAGE_TOGGLE_V1 ===
    # گزارشِ ادمین (ایلیا): امکانِ خاموش‌کردنِ «صفحهٔ پرداخت» که کاربر را وارد
    # سایتِ درگاه می‌کند. با خاموش‌بودن، کاربر فقط شمارهٔ کارت را داخلِ
    # همین چت می‌بیند و همان‌جا واریز می‌کند. پیش‌فرض «روشن» = رفتارِ قبلی.
    if payment_url and await AG._get(row.id, "abangateway_show_payment_page", "1") != "0":
        kb_rows.append([InlineKeyboardButton(text="💳 صفحهٔ پرداخت", url=payment_url)])
    # === PATCH_ABANGATEWAY_INVOICE_UX_V1 ===
    # قالبِ فاکتور مثلِ «شارژ خودکار کارت‌به‌کارت»ِ رباتِ اصلی شد: مبلغ به تومان و ریال،
    # دکمه‌هایِ «کپیِ واقعی» (copy_text) برایِ مبلغِ ریالی و شمارهٔ کارت، دکمهٔ پشتیبانی
    # (اگر مالک آدرسِ پشتیبانی ثبت کرده باشد)، و حذفِ خودکارِ پیامِ فاکتور بعد از شارژ.
    if card_number:
        kb_rows.append([
            InlineKeyboardButton(text="📋 کپی مبلغ (ریال)",
                                 copy_text=CopyTextButton(text=str(payable_toman * 10))),
            InlineKeyboardButton(text="📋 کپی شماره کارت",
                                 copy_text=CopyTextButton(text=_fmt_card(card_number))),
        ])
    else:
        kb_rows.append([InlineKeyboardButton(text="📋 کپی مبلغ (ریال)",
                                             copy_text=CopyTextButton(text=str(payable_toman * 10)))])
    kb_rows.append([InlineKeyboardButton(text="🔍 بررسیِ پرداخت",
                                         callback_data=f"abu:check:{oid}", style=ButtonStyle.SUCCESS)])
    try:
        from tenant import support_button
        _sb = await support_button(row.id, text="💬 پیام به پشتیبانی (در صورت شارژ نشدن)")
        if _sb:
            kb_rows.append([_sb])
    except Exception:
        logger.exception("abangateway: دکمهٔ پشتیبانی ساخته نشد")
    kb_rows.append([InlineKeyboardButton(text="❌ لغو سفارش",
                                         callback_data=f"abu:cancel:{oid}", style=ButtonStyle.DANGER)])

    card_line = (f"💳 <code>{_fmt_card(card_number)}</code>\n"
                if card_number else "")
    holder_line = f"🧑 {card_holder}\n" if card_holder else ""
    _deadline = AG.deadline_minutes(body)
    _deadline_line = (f"⏰ مهلت: {_deadline} دقیقه. " if _deadline else "")
    sent = await message.answer(
        "📲 <b>شارژ خودکار کارت‌به‌کارت</b>\n\n"
        "❗ برای تأییدِ خودکار، <b>دقیقاً</b> این مبلغ را واریز کن:\n\n"
        f"💰 <code>{payable_toman:,}</code> تومان\n"
        f"🌐 <code>{payable_toman * 10:,}</code> ریال\n\n"
        f"{card_line}{holder_line}\n"
        f"{_deadline_line}چند رقمِ آخرِ مبلغ، کدِ اختصاصیِ فاکتورِ توست. اگر مبلغ را تغییر "
        f"بدهی، خودکار تأیید نمی‌شود. تمامِ مبلغِ شارژ ({int(amount):,} تومان) به کیف پول اضافه می‌شود.\n\n"
        "<i>💡 برایِ کپی، کافیه روی خودِ عددها بزنی، یا از دکمه‌هایِ زیر استفاده کنی.</i>\n"
        f"🔒 درگاه: «{disp_name}»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    # شناسهٔ پیامِ فاکتور برایِ حذفِ خودکار بعد از شارژ (بدونِ ستونِ تازه در دیتابیس)
    try:
        await AG.remember_invoice_msg(row.id, oid, sent.chat.id, sent.message_id)
    except Exception:
        logger.exception("abangateway: ذخیرهٔ شناسهٔ پیامِ فاکتور ناموفق بود")


@abangateway_router.callback_query(F.data.startswith("abu:check:"))
async def abu_check(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    # PATCH_GATEWAY_INPLACE_V1: settle_charge خودش پیامِ «شارژ شد» (یا دکمهٔ تکمیلِ خرید) را می‌فرستد؛ اینجا پیامِ
    # تکراری نمی‌فرستیم — فقط اعلانِ بالا و برداشتنِ دکمه‌هایِ فاکتورِ مرده.
    from tenant.gateways import inplace as IP
    ok, msg_, credit = await AG.settle_charge(row.id, oid)
    if ok:
        await cb.answer("✅ پرداخت تأیید و کیف پولت شارژ شد.")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            logger.debug("abangateway: برداشتنِ دکمه‌هایِ فاکتور ممکن نشد")
    else:
        # === PATCH_ABANGATEWAY_ADMIN_NOTIFY_V1 ===
        # کاربر ادعا کرد پرداخت کرده ولی هنوز تأیید نشده — اگه بعدِ چند
        # دقیقه هم همچنان تأیید نیاد، حلقهٔ پس‌زمینه خودش به ادمین خبر می‌ده.
        try:
            await AG.mark_claimed(row.id, oid)
        except Exception:
            logger.exception("abangateway: ثبتِ ادعایِ پرداخت ناموفق بود")
        await IP.toast(cb, "ℹ️ " + msg_)


@abangateway_router.callback_query(F.data.startswith("abu:cancel:"))
async def abu_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ فاکتورِ پرداخت‌نشده — هم سمتِ ما، هم روی خودِ درگاه (تا ظرفیتِ
    رزروشدهٔ کارت آزاد شود). اگر کاربر واقعاً کارت‌به‌کارت کرده باشد، پولش
    نمی‌سوزد: طبقِ مستندات اگر واریزی بعد از لغو برسد، به صفِ بررسیِ دستیِ
    آبان گیت‌وی می‌رود و گم نمی‌شود؛ رکوردِ خودمان هم پاک نمی‌شود."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == row.id, AbanGatewayCharge.order_id == oid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        if ch.status == "paid":
            await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
            return
        ch.status = "cancelled"
        invoice_id = ch.invoice_id
        await s.commit()
    if invoice_id:
        try:
            token = await AG._get(row.id, "abangateway_token", "")
            if token:
                await AG.cancel_invoice(token, invoice_id)
        except Exception:
            logger.exception("abangateway: لغوِ فاکتور رویِ درگاه ناموفق بود (بی‌اهمیت)")
    await cb.answer("فاکتور لغو شد.")
    from tenant import inline_back_kb
    from tenant.gateways import inplace as IP               # PATCH_GATEWAY_INPLACE_V1: همان پیام «لغو شد» می‌شود
    await IP.show(cb, IP.CANCELLED_TEXT, await inline_back_kb(row.id))


# ═════════════════ تصمیمِ ادمین رویِ ادعایِ پرداختِ تأییدنشده ═════════════════
# === PATCH_ABANGATEWAY_ADMIN_NOTIFY_V1 ===
@abangateway_router.callback_query(F.data.startswith("ab:claim:ok:"))
async def ab_claim_approve(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    oid = cb.data.split(":", 3)[3]
    from datetime import datetime
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == row.id,
            AbanGatewayCharge.order_id == oid).with_for_update())
        if not ch:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        # === PATCH_ABANGATEWAY_CLAIM_EXPIRED_V1 ===
        # گزارشِ Mike: «این دکمه کار نمی‌کنه». هشدار ۵ دقیقه بعد از ادعا می‌آید؛
        # تا ادمین دکمه را بزند فاکتور رویِ درگاه منقضی شده و حلقهٔ پس‌زمینه
        # ردیف را «expired» کرده — دکمه فقط «قبلاً پردازش شده» می‌گفت و هیچ
        # راهی برایِ شارژِ دستی نمی‌ماند (دقیقاً همان حالتِ پیامکِ دیرِ بانک).
        # حالا فقط «paid» و «rejected» نهایی‌اند؛ منقضی/لغوشده هنوز قابلِ تصمیم است.
        if ch.status in ("paid", "rejected"):
            await cb.answer("این فاکتور قبلاً " + ("شارژ شده" if ch.status == "paid" else "رد شده")
                            + " است.", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            return
        credit = int(ch.amount_toman or 0)
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.ref_id = f"manual-admin:{cb.from_user.id}"
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id,
            BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            from models import WalletTxn
            s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=credit,
                            # === PATCH_ABANGATEWAY_MANUAL_KIND_LEN_V1 ===
                            # ستونِ WalletTxn.kind فقط ۲۰ کاراکتر است؛ «abangateway_manual_admin»
                            # (۲۴ حرف) در PostgreSQL خطایِ truncation می‌داد و کلِ تأییدِ دستی
                            # بی‌صدا شکست می‌خورد (دکمه هیچ واکنشی نداشت).
                            kind="abangw_manual",
                            note=f"تأییدِ دستیِ ادمین رویِ ادعایِ پرداخت؛ order={oid}"))
            _uid, _bal = u.telegram_id, float(u.balance)
        else:
            _uid = _bal = None
        await s.commit()
    await cb.answer("✅ شارژ شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✅ تأیید شد و کیفِ‌پولِ کاربر دستی شارژ شد.")
    if _uid:
        try:
            from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
            t, k = await build_wallet_charge_message_for_bot(row.id, credit, _bal, "card")
            await bot.send_message(_uid, t, reply_markup=k)
        except Exception:
            try:
                await bot.send_message(_uid, f"✅ {credit:,} تومان به کیفِ پولت اضافه شد.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@abangateway_router.callback_query(F.data.startswith("ab:claim:no:"))
async def ab_claim_reject(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    oid = cb.data.split(":", 3)[3]
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == row.id,
            AbanGatewayCharge.order_id == oid).with_for_update())
        if not ch:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        # === PATCH_ABANGATEWAY_CLAIM_EXPIRED_V1 ===
        # گزارشِ Mike: «این دکمه کار نمی‌کنه». هشدار ۵ دقیقه بعد از ادعا می‌آید؛
        # تا ادمین دکمه را بزند فاکتور رویِ درگاه منقضی شده و حلقهٔ پس‌زمینه
        # ردیف را «expired» کرده — دکمه فقط «قبلاً پردازش شده» می‌گفت و هیچ
        # راهی برایِ شارژِ دستی نمی‌ماند (دقیقاً همان حالتِ پیامکِ دیرِ بانک).
        # حالا فقط «paid» و «rejected» نهایی‌اند؛ منقضی/لغوشده هنوز قابلِ تصمیم است.
        if ch.status in ("paid", "rejected"):
            await cb.answer("این فاکتور قبلاً " + ("شارژ شده" if ch.status == "paid" else "رد شده")
                            + " است.", show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            return
        ch.status = "rejected"
        tg_id = ch.user_telegram_id
        await s.commit()
    await cb.answer("رد شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("❌ ثبت شد؛ کیفِ‌پول شارژ نشد.")
    try:
        await bot.send_message(
            tg_id,
            "❌ متأسفانه پرداختِ اعلام‌شده‌ات هنوز از طرفِ درگاه یا ادمین تأیید "
            "نشده. اگه واقعاً واریز کردی، رسیدِ بانکی رو برایِ پشتیبانیِ ربات بفرست.")
    except Exception:
        logger.exception("abangateway: اطلاع به کاربر برایِ ردِ ادعا ناموفق بود")
