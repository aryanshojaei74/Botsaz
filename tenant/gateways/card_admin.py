"""
card_admin.py — هندلرهای aiogram مربوط به «تنظیمات ادمینِ کارت‌به‌کارت»:
  • ثبت/ویرایش/حذف شماره کارت و نام صاحب کارت
  • فعال/غیرفعال کردن کارت‌به‌کارت
  • پشتیبانیِ اختصاصیِ کارت (جایگزینِ پشتیبانیِ عمومی هنگام غیرفعال بودن کارت)

این اولین قدم از تفکیکِ کارت‌به‌کارت است (طبق CARD_HANDOFF.md): فقط بخشِ
تنظیماتِ ادمین که خودکفاترین و کم‌ریسک‌ترین قسمت است. جریان‌های شارژِ کیف‌پول
و خریدِ مستقیمِ کانفیگ (که به fromهای دیگر گره خورده‌اند) در قدم‌های بعدی
جدا خواهند شد.

از همان الگوی ماژول‌های مستقلِ دیگر پیروی می‌کند (نک: tenant/stars_admin.py):
Router و StatesGroup مستقلِ خودش را دارد و توابعِ مشترکِ tenant/__init__.py
را با import محلی (داخل خودِ تابع) می‌گیرد تا از circular import جلوگیری شود.

نکته دربارهٔ `set_card_toggle` (کال‌بک `set:card_toggle`): آن هندلر عمداً در
tenant/__init__.py می‌ماند، چون به تابعِ منویِ اصلیِ تنظیمات
(`tadm_settings`) گره خورده که در همان فایل است. این ماژول فقط پنلِ
اختصاصیِ کارت (`set:card` و زیرمجموعه‌هایش) را دارد.
"""
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

card_admin_router = Router()


class CardFSM(StatesGroup):
    number = State()     # ثبت/ویرایش شماره کارت
    holder = State()      # ثبت/ویرایش نام صاحب کارت
    support = State()     # ثبت/ویرایش پشتیبانیِ اختصاصیِ کارت
    # === PATCH_CARD_DEST_ID_EDIT_V1 ===
    # شبا و شماره‌حسابِ مقصد فقط داخلِ ویزاردِ «فعال‌کردنِ تاییدِ خودکار»
    # پرسیده می‌شدند و اینجا فقط *نمایش* داده می‌شدند. یعنی مالکی که آن
    # مرحله را با «-» رد کرده بود (یا قبل از اضافه‌شدنِ این فیلدها تایید
    # خودکار را روشن کرده بود) هیچ راهی برای ثبتِ بعدی‌شان نداشت — و
    # رسیدهایی که فقط شبا نشان می‌دهند همیشه به تاییدِ دستی می‌رفتند.
    sheba = State()       # ثبت/ویرایش شبای مقصد
    account = State()     # ثبت/ویرایش شمارهٔ حسابِ مقصد
    number2 = State()     # === PATCH_CARD_SECOND_V1 === شمارهٔ کارتِ دوم (ذخیره/جایگزین)
    holder2 = State()     # نامِ صاحبِ کارتِ دوم


async def card_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبوردِ پنلِ تنظیماتِ کارت — از چند هندلر مشترک استفاده می‌شود."""
    from tenant import get_setting
    card = await get_setting(bot_id, "card_number", "")
    holder = await get_setting(bot_id, "card_holder", "")
    enabled = await get_setting(bot_id, "card_enabled", "1") == "1"
    purchase_enabled = await get_setting(bot_id, "card_purchase_enabled", "1") == "1"
    support = await get_setting(bot_id, "card_support_contact", "")
    sheba = await get_setting(bot_id, "card_sheba", "")
    account = await get_setting(bot_id, "card_account_number", "")
    auto_approve = await get_setting(bot_id, "auto_approve_receipts", "0") == "1"
    sender_gate = await get_setting(bot_id, "receipt_sender_gate", "1") != "0"  # === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===

    card2 = await get_setting(bot_id, "card_number2", "")   # === PATCH_CARD_SECOND_V1 ===
    holder2 = await get_setting(bot_id, "card_holder2", "")
    show_both = await get_setting(bot_id, "card_show_both", "0") == "1"   # PATCH_CARD_BOTH_V1
    lines = ["💳 <b>شماره کارت و کارت‌به‌کارت</b>", ""]
    lines.append(f"شماره کارت: <code>{card}</code>" if card else "شماره کارت: <i>ثبت نشده</i>")
    lines.append(f"صاحب کارت: <b>{holder}</b>" if holder else "صاحب کارت: <i>ثبت نشده</i>")
    lines.append(f"وضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}")
    lines.append(f"وضعیت در «خرید»: {'✅ فعال' if purchase_enabled else '❌ غیرفعال (فقط برای خرید)'}")
    if support:
        lines.append(f"پشتیبانیِ اختصاصیِ کارت: {support}")
    else:
        lines.append("پشتیبانیِ اختصاصیِ کارت: <i>تنظیم نشده (از پشتیبانیِ عمومیِ ربات استفاده می‌شود)</i>")
    if card2:
        lines.append(f"🔁 کارتِ دوم (ذخیره): <code>{card2}</code>" + (f" — {holder2}" if holder2 else ""))
        lines.append("👀 به مشتری: " + ("<b>هر دو کارت نشان داده می‌شود</b>" if show_both
                                       else "فقط کارتِ اصلی (با «🔀» می‌توانی هر دو را نشان بدهی)"))
    # === PATCH_AUTO_APPROVE_MULTI_DEST_ID_V1 ===
    # === PATCH_CARD_DEST_ID_EDIT_V1 === همیشه نشان داده می‌شوند
    lines.append("")
    lines.append("🤖 تایید خودکار رسید: "
                 + ("<b>فعال</b>" if auto_approve else "<i>غیرفعال</i>"))
    if auto_approve:   # === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===
        lines.append("🔢 پرسیدنِ ۴ رقمِ آخرِ کارتِ مبدأ: "
                     + ("<b>روشن</b>" if sender_gate else "<i>خاموش</i>"))
    lines.append(f"شبای مقصد: <code>{sheba}</code>" if sheba else "شبای مقصد: <i>ثبت نشده</i>")
    lines.append(f"حسابِ مقصد: <code>{account}</code>" if account else "حسابِ مقصد: <i>ثبت نشده</i>")
    if auto_approve and not sheba:
        lines.append("")
        lines.append("⚠️ بعضی بانک‌ها در رسید فقط <b>شبا</b> را نشان می‌دهند و شماره کارت "
                     "مقصد در رسید نیست. تا وقتی شبای مقصد ثبت نشده، آن رسیدها خودکار "
                     "تایید نمی‌شوند و به بررسیِ دستی می‌روند.")
    lines.append("")
    lines.append("💡 وقتی کارت‌به‌کارت «غیرفعال» باشد، به‌جای شماره کارت، کاربر به همین "
                 "پشتیبانی هدایت می‌شود (چه تو کیف پول، چه موقعِ خرید).")
    lines.append("💡 با دکمهٔ «وضعیت در خرید» می‌تونی کارت‌به‌کارت رو فقط تو «خرید» غیرفعال کنی، "
                 "بدون اینکه رو شارژِ کیف‌پول تأثیر بذاره.")

    rows = [
        [InlineKeyboardButton(text="✏️ ثبت / ویرایش شماره کارت", callback_data="card:edit", style=ButtonStyle.PRIMARY)],
    ]
    if card or holder:
        rows.append([InlineKeyboardButton(text="🗑 حذف شماره کارت", callback_data="card:delete", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(
        text="🔴 غیرفعال کن" if enabled else "🟢 فعال کن",
        callback_data="card:toggle", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(
        text="🔴 غیرفعال کن در خرید" if purchase_enabled else "🟢 فعال کن در خرید",
        callback_data="card:purchase_toggle", style=ButtonStyle.DANGER)])
    # === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 === فقط وقتی تأییدِ خودکار روشن است معنا دارد
    if auto_approve:
        rows.append([InlineKeyboardButton(
            text=("🔴 خاموش‌کردنِ پرسیدنِ ۴ رقمِ آخرِ کارتِ مبدأ" if sender_gate
                  else "🟢 روشن‌کردنِ پرسیدنِ ۴ رقمِ آخرِ کارتِ مبدأ"),
            callback_data="card:sender_gate_toggle", style=ButtonStyle.PRIMARY)])
    # === PATCH_CARD_DEST_ID_EDIT_V1 ===
    rows.append([InlineKeyboardButton(
        text=("✏️ ویرایش شبای مقصد" if sheba else "🏦 ثبت شبای مقصد"),
        callback_data="card:sheba_edit", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=("✏️ ویرایش حسابِ مقصد" if account else "🔢 ثبت شمارهٔ حسابِ مقصد"),
        callback_data="card:account_edit", style=ButtonStyle.PRIMARY)])
    # === PATCH_CARD_SECOND_V1 === کارتِ دوم: با یک دکمه جای کارتِ اصلی می‌نشیند
    rows.append([InlineKeyboardButton(
        text=("✏️ ویرایش کارتِ دوم" if card2 else "➕ ثبتِ کارتِ دوم (جایگزین)"),
        callback_data="card:edit2", style=ButtonStyle.PRIMARY)])
    if card2:
        rows.append([InlineKeyboardButton(
            text=("🔀 نمایشِ هر دو کارت به مشتری: ✅ روشن" if show_both else "🔀 نمایشِ هر دو کارت به مشتری: ⛔️ خاموش"),
            callback_data="card:both_toggle",
            style=(ButtonStyle.DANGER if show_both else ButtonStyle.SUCCESS))])
        rows.append([InlineKeyboardButton(text="🔁 جابه‌جاییِ کارتِ اصلی و کارتِ دوم",
                                          callback_data="card:swap", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ کارتِ دوم",
                                          callback_data="card:delete2", style=ButtonStyle.DANGER)])
    # === PATCH_RECEIPT_AI_OWN_KEY_V1 ===
    from tenant.gateways.receipt_ai_key import get_creds as _rai_get
    _rai_on = bool(await _rai_get(bot_id))
    rows.append([InlineKeyboardButton(
        text=("🔑 هوش مصنوعیِ رسید: ✅ کلیدِ خودت ثبت است" if _rai_on else "🔑 ثبتِ توکنِ آروان برایِ تایید خودکار"),
        callback_data="rai:menu", style=(ButtonStyle.SUCCESS if _rai_on else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="💬 تنظیم/ویرایش پشتیبانیِ کارت", callback_data="card:support_edit", style=ButtonStyle.PRIMARY)])
    if support:
        rows.append([InlineKeyboardButton(text="🗑 حذف پشتیبانیِ کارت", callback_data="card:support_delete", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@card_admin_router.callback_query(F.data == "set:card")
async def set_card_start(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@card_admin_router.callback_query(F.data == "set:card_holder")
async def set_card_holder_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ویرایش مستقیم نام صاحب کارت (دکمه‌ای که قبلاً handler نداشت)."""
    from tenant import _owner_ok, smart_cancel_kb
    if not await _owner_ok(cb, bot):
        return
    await state.set_state(CardFSM.holder)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("🧑 نام و نام‌خانوادگیِ صاحبِ کارت را بفرست:\nمثال: علی احمدی",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.callback_query(F.data == "card:edit")
async def card_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    if not await _owner_ok(cb, bot):
        return
    await state.set_state(CardFSM.number)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("💳 شماره کارت را بفرست:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.message(CardFSM.number, F.text)
async def set_card_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, main_kb_or_remove, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    await set_setting(row.id, "card_number", raw)
    await state.set_state(CardFSM.holder)
    await msg.answer(
        "🧑 حالا نام و نام‌خانوادگیِ صاحبِ کارت را بفرست:\nمثال: علی احمدی",
        reply_markup=await smart_cancel_kb(bot))


@card_admin_router.message(CardFSM.holder, F.text)
async def set_card_holder_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    await set_setting(row.id, "card_holder", raw)
    await state.clear()
    text, kb = await card_admin_content(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:delete")
async def card_delete(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "card_number", "")
    await set_setting(row.id, "card_holder", "")
    await cb.answer("🗑 حذف شد")
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


# === PATCH_CARD_SECOND_V1 ===
async def _card_refresh(cb: CallbackQuery, bot_id: int):
    text, kb = await card_admin_content(bot_id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:edit2")
async def card_edit2_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    if not await _owner_ok(cb, bot):
        return
    await state.set_state(CardFSM.number2)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        "💳 شمارهٔ <b>کارتِ دوم</b> را بفرست.\n"
        "این کارت فقط ذخیره می‌شود و تا وقتی «جابه‌جایی» را نزنی، به مشتری نشان داده نمی‌شود.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.message(CardFSM.number2, F.text)
async def card_number2_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    digits = _digits_only(raw)
    if len(digits) != 16:
        await msg.answer("شمارهٔ کارت باید ۱۶ رقم باشد. دوباره بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "card_number2", digits)
    await state.set_state(CardFSM.holder2)
    await msg.answer("🧑 نام و نام‌خانوادگیِ صاحبِ کارتِ دوم را بفرست:",
                     reply_markup=await smart_cancel_kb(bot))


@card_admin_router.message(CardFSM.holder2, F.text)
async def card_holder2_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw not in ("❌ لغو", "❌ لغو / بازگشت"):
        await set_setting(row.id, "card_holder2", raw)
    await state.clear()
    text, kb = await card_admin_content(row.id)
    await msg.answer("✅ کارتِ دوم ذخیره شد. هر وقت کارتِ اصلی مشکل داشت، «🔁 جابه‌جایی» را بزن.\n\n" + text,
                     reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:swap")
async def card_swap(cb: CallbackQuery, bot: Bot):
    """جای کارتِ اصلی و دوم (شماره/نام/شبا/حساب) عوض می‌شود؛ بقیهٔ ربات که فقط
    کارتِ اصلی را می‌خواند، بدونِ هیچ تغییری از همان لحظه کارتِ جدید را نشان می‌دهد."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await get_setting(row.id, "card_number2", ""):
        await cb.answer("اول کارتِ دوم را ثبت کن.", show_alert=True)
        return
    for a, b in (("card_number", "card_number2"), ("card_holder", "card_holder2"),
                 ("card_sheba", "card_sheba2"), ("card_account_number", "card_account_number2")):
        va = await get_setting(row.id, a, "")
        vb = await get_setting(row.id, b, "")
        await set_setting(row.id, a, vb)
        await set_setting(row.id, b, va)
    await cb.answer("🔁 جابه‌جا شد؛ کارتِ دوم حالا کارتِ اصلی است.", show_alert=True)
    await _card_refresh(cb, row.id)


@card_admin_router.callback_query(F.data == "card:both_toggle")
async def card_both_toggle(cb: CallbackQuery, bot: Bot):
    """=== PATCH_CARD_BOTH_V1 === نمایشِ هر دو کارت (اصلی + دوم) به مشتری."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await get_setting(row.id, "card_number2", ""):
        return await cb.answer("اول کارتِ دوم را ثبت کن.", show_alert=True)
    cur = await get_setting(row.id, "card_show_both", "0")
    await set_setting(row.id, "card_show_both", "0" if cur == "1" else "1")
    await cb.answer("✅ مشتری حالا هر دو کارت را می‌بیند." if cur != "1" else "⛔️ مشتری فقط کارتِ اصلی را می‌بیند.",
                    show_alert=True)
    await _card_refresh(cb, row.id)


@card_admin_router.callback_query(F.data == "card:delete2")
async def card_delete2(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    for k in ("card_number2", "card_holder2", "card_sheba2", "card_account_number2"):
        await set_setting(row.id, k, "")
    await cb.answer("🗑 حذف شد")
    await _card_refresh(cb, row.id)


@card_admin_router.callback_query(F.data == "card:toggle")
async def card_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "card_enabled", "1")
    await set_setting(row.id, "card_enabled", "0" if cur == "1" else "1")
    await cb.answer("✅ تغییر کرد")
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


# === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===
@card_admin_router.callback_query(F.data == "card:sender_gate_toggle")
async def card_sender_gate_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش‌کردنِ پرسیدنِ ۴ رقمِ آخرِ کارت/شبا/حسابِ مبدأ (مستقل از تأییدِ خودکار)."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "receipt_sender_gate", "1")
    await set_setting(row.id, "receipt_sender_gate", "0" if cur != "0" else "1")
    await cb.answer("✅ تغییر کرد")
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:purchase_toggle")
async def card_purchase_toggle(cb: CallbackQuery, bot: Bot):
    """فعال/غیرفعال کردنِ کارت‌به‌کارت *فقط* در بخشِ خرید (روی شارژِ کیف‌پول اثر ندارد)."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "card_purchase_enabled", "1")
    await set_setting(row.id, "card_purchase_enabled", "0" if cur == "1" else "1")
    await cb.answer("✅ تغییر کرد")
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:support_edit")
async def card_support_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(CardFSM.support)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "💬 آیدی/یوزرنیمِ پشتیبانیِ مخصوصِ کارت‌به‌کارت را بفرست\n"
        "(مثلاً <code>@support_id</code> یا آیدی عددی).\n"
        "اگه چیزی نفرستی، از پشتیبانیِ عمومیِ ربات استفاده می‌شود.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.message(CardFSM.support, F.text)
async def card_support_edit_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, normalize_support, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    url, _label = normalize_support(raw)
    if not url:
        await msg.answer("فرمت نامعتبر است. آیدی عددی، یوزرنیم یا لینک بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "card_support_contact", raw)
    await state.clear()
    text, kb = await card_admin_content(row.id)
    await msg.answer("✅ پشتیبانیِ کارت ذخیره شد.\n\n" + text, reply_markup=kb)


# === PATCH_CARD_DEST_ID_EDIT_V1 ===
# ثبت/ویرایشِ مستقیمِ شبا و شمارهٔ حسابِ مقصد، بدونِ نیاز به خاموش/روشن‌کردنِ
# دوبارهٔ «تایید خودکار رسید». همان کلیدهایِ Setting که ویزاردِ تاییدِ خودکار
# می‌نویسد (card_sheba / card_account_number) اینجا هم نوشته می‌شوند، پس
# auto_receipt بدونِ هیچ تغییری از آن‌ها استفاده می‌کند.

def _digits_only(raw: str) -> str:
    """فقط رقم‌ها — «IR»، فاصله، خط‌تیره و ارقامِ فارسی نادیده گرفته می‌شوند."""
    import re as _re
    fa = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return _re.sub(r"\D", "", (raw or "").translate(fa))


@card_admin_router.callback_query(F.data == "card:sheba_edit")
async def card_sheba_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "card_sheba", "")
    await state.set_state(CardFSM.sheba)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🏦 <b>شبای مقصد</b>\n\n"
        "شمارهٔ شبای حسابی که پول به آن واریز می‌شود را بفرست "
        "(با <code>IR</code> یا بدونش، فرقی ندارد).\n\n"
        "این عدد فقط برایِ تاییدِ خودکارِ رسید استفاده می‌شود: بعضی بانک‌ها در "
        "رسید شماره کارتِ مقصد را نشان نمی‌دهند و فقط شبا را می‌نویسند؛ با ثبتِ "
        "این عدد، آن رسیدها هم خودکار تایید می‌شوند.\n\n"
        + (f"شمارهٔ فعلی: <code>{cur}</code>\n\n" if cur else "")
        + "برای <b>حذفِ</b> شمارهٔ ثبت‌شده «-» بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.message(CardFSM.sheba, F.text)
async def card_sheba_edit_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    if raw == "-":
        await set_setting(row.id, "card_sheba", "")
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer("🗑 شبای مقصد حذف شد.\n\n" + text, reply_markup=kb)
        return
    digits = _digits_only(raw)
    # شبای ایران ۲۴ رقم است (IR + ۲۴ رقم). کوتاه‌تر از این یعنی اشتباهِ تایپی.
    if len(digits) != 24:
        await msg.answer(
            "❌ شبا باید <b>۲۴ رقم</b> باشد (بدونِ <code>IR</code>).\n"
            f"چیزی که فرستادی {len(digits)} رقم داشت. دوباره بفرست:",
            reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "card_sheba", "IR" + digits)
    await state.clear()
    text, kb = await card_admin_content(row.id)
    await msg.answer("✅ شبای مقصد ذخیره شد.\n\n" + text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:account_edit")
async def card_account_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "card_account_number", "")
    await state.set_state(CardFSM.account)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔢 <b>شمارهٔ حسابِ مقصد</b>\n\n"
        "شمارهٔ حسابِ بانکی‌ات را بفرست. بعضی رسیدها به‌جای کارت و شبا، فقط "
        "شمارهٔ حساب را نشان می‌دهند.\n\n"
        + (f"شمارهٔ فعلی: <code>{cur}</code>\n\n" if cur else "")
        + "برای <b>حذفِ</b> شمارهٔ ثبت‌شده «-» بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_admin_router.message(CardFSM.account, F.text)
async def card_account_edit_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    if raw == "-":
        await set_setting(row.id, "card_account_number", "")
        await state.clear()
        text, kb = await card_admin_content(row.id)
        await msg.answer("🗑 حسابِ مقصد حذف شد.\n\n" + text, reply_markup=kb)
        return
    digits = _digits_only(raw)
    # شمارهٔ حساب بین بانک‌ها فرق دارد؛ فقط یک حداقلِ منطقی چک می‌شود.
    if len(digits) < 6:
        await msg.answer("❌ شمارهٔ حساب خیلی کوتاه است. دوباره بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "card_account_number", digits)
    await state.clear()
    text, kb = await card_admin_content(row.id)
    await msg.answer("✅ حسابِ مقصد ذخیره شد.\n\n" + text, reply_markup=kb)


@card_admin_router.callback_query(F.data == "card:support_delete")
async def card_support_delete(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "card_support_contact", "")
    await cb.answer("🗑 حذف شد")
    text, kb = await card_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
