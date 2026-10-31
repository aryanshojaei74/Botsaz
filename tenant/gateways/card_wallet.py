"""
card_wallet.py — هندلرهای aiogram مربوط به «شارژ کیف پول با کارت‌به‌کارت»:
  • انتخاب مبلغ (پیش‌فرض یا دلخواه)
  • در صورت فعال‌بودن تشخیصِ خودکار رسید (OCR): پرسیدنِ ۴ رقمِ آخرِ کارت/شبا/حساب مبدأ
  • نمایشِ شماره‌کارت مقصد و دریافتِ رسید (عکس یا متن)
  • تاییدِ خودکار (OCR) یا ارجاع به تاییدِ دستیِ ادمین

⚠️ نکتهٔ مهم دربارهٔ scope (طبق CARD_HANDOFF.md): کارت‌به‌کارت در این پروژه
سه حوزه دارد — (۱) شارژ کیف‌پول [همینجا]، (۲) خریدِ مستقیمِ کانفیگ/سرویس با
کارت، (۳) تشخیصِ خودکار OCR (tenant/auto_receipt.py، فایل جدا). این ماژول
**فقط** حوزهٔ (۱) است. حوزهٔ (۲) — یعنی `_maybe_gate_card_invoice`،
`card_order_sender_id_type`، `card_order_sender_card4` که از یک FSM کاملاً
جدا (`CardSenderCardFSM`) استفاده می‌کنند — عمداً در tenant/__init__.py
باقی مانده چون به جریانِ خریدِ فروشگاه/VPN وابسته است و باید جداگانه (با
دقتِ بیشتر) بررسی و منتقل شود.

همچنین `w:back` (بازگشت به کیف پول) و هندلرهای `chadm:*` (تاییدِ دستیِ
ادمین برای سفارش) عمداً در tenant/__init__.py مانده‌اند چون هر دو به‌طور
مشترک توسط چند حوزهٔ دیگر هم استفاده می‌شوند (نه فقط شارژِ کیف‌پول با کارت).

از همان الگوی ماژول‌های مستقلِ دیگر پیروی می‌کند: Router و StatesGroup
مستقلِ خودش را دارد و توابعِ مشترکِ tenant/__init__.py را با import محلی
می‌گیرد تا از circular import جلوگیری شود.
"""
import datetime as _dt
import html as _html  # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
import re

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler  # PATCH_RECEIPT_STATE_CANCEL_V1
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from config import Session
from models import BotUser, Order, WalletTxn, MainButton
from plans import Feature, has_feature

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)

print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

card_wallet_router = Router()
# === PATCH_RECEIPT_STATE_CANCEL_V1 ===
# باگِ گزارش‌شده: کاربر وسطِ ارسالِ رسید دکمهٔ «⬅️ بازگشت به منو» را می‌زد،
# ولی چون آن متن عدد ندارد، ربات می‌گفت «متن رسید باید شامل عدد باشد» و
# کاربر در همان مرحله گیر می‌کرد. این تابع متن‌هایِ خروج را می‌شناسد.
def _is_exit_text(txt: str) -> bool:
    t = (txt or "").strip()
    return t in ("⬅️ بازگشت به منو", "بازگشت به منو", "❌ لغو",
                 "❌ لغو / بازگشت", "لغو", "بازگشت", "/start", "/cancel")


@card_wallet_router.callback_query(F.data == "w:buy_products")
async def wallet_buy_products(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازکردنِ مستقیمِ اولین دکمهٔ فعالِ «خرید محصول» همین ربات."""
    from tenant import get_bot_row, get_or_create_user, _exec_main_button
    row = await get_bot_row(bot)
    if not row:
        await cb.answer("ربات پیدا نشد.", show_alert=True)
        return
    async with Session() as s:
        button = await s.scalar(select(MainButton).where(
            MainButton.bot_id == row.id,
            MainButton.parent_id.is_(None),
            MainButton.action == "buy_product",
        ).order_by(MainButton.row, MainButton.col))
    if not button:
        await cb.answer("فعلاً محصولی برای خرید تنظیم نشده است.", show_alert=True)
        return
    user, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name,
        cb.from_user.username or "", return_created=True)
    await _exec_main_button(button, cb.message, bot, state, row, user, row.id, cb=cb)


WALLET_PRESETS = [50000, 100000, 200000, 500000]


class CardWalletFSM(StatesGroup):
    amount = State()          # مبلغِ دلخواه (ورودیِ متنی)
    sender_card4 = State()     # ۴ رقمِ آخرِ کارت/شبا/حسابِ مبدأ (فقط اگر OCR فعال باشد)
    receipt = State()          # انتظارِ عکس/متنِ رسید
    confirm = State()          # تاییدِ نهاییِ کاربر قبل از ارسال


# ═══════════════════════ ورودی‌ها (کیبورد ساده + دکمهٔ اینلاین) ═══════════════════════

async def wallet_charge_text(msg: Message, bot: Bot, state: FSMContext):
    """نسخهٔ حالتِ کیبوردیِ ساده برای شروع شارژ با کارت‌به‌کارت."""
    from tenant import get_bot_row, get_setting, support_button, pay_phone_gate
    await state.clear()
    row = await get_bot_row(bot)
    if not row:
        return
    # === PATCH_PAY_PHONE_VERIFY_V1 ===
    if await pay_phone_gate(msg, row, msg.from_user.id):
        return
    card_enabled = await get_setting(row.id, "card_enabled", "1")
    if card_enabled != "1":
        card_support = await get_setting(row.id, "card_support_contact", "")
        sup_btn = await support_button(row.id, "💬 پیام به پشتیبانی", contact=card_support)
        await msg.answer(
            "❌ <b>کارت به کارت غیرفعال است</b>\n\n"
            "در حال حاضر امکان شارژ از طریق کارت‌به‌کارت وجود ندارد.\n"
            "برای اطلاعات بیشتر با پشتیبانی تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[sup_btn]]) if sup_btn else None,
        )
        return
    colored = bool(has_feature(row.plan, Feature.COLORED_BUTTONS))
    _txt, _kb = await _charge_prompt_and_kb(row.id, colored)   # PATCH_WALLET_LIMITS_V1
    await msg.answer(_txt, reply_markup=_kb)


def wallet_charge_kb(colored: bool = False, presets=None) -> InlineKeyboardMarkup:
    from tenant import rial
    succ = ButtonStyle.SUCCESS if colored else None
    prim = ButtonStyle.PRIMARY if colored else None
    dang = ButtonStyle.DANGER if colored else None
    # === PATCH_WALLET_LIMITS_V1 === presets=None ⇒ همهٔ مبلغ‌هایِ ثابت (رفتارِ قبلی)
    plist = list(WALLET_PRESETS if presets is None else presets)
    rows = [[InlineKeyboardButton(text=rial(p), callback_data=f"wcamt:{p}", style=succ) for p in plist[i:i + 2]]
            for i in range(0, len(plist), 2)]
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه", callback_data="wcamt:custom", style=prim)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=dang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _charge_prompt_and_kb(bot_id: int, colored: bool):
    """(متنِ انتخابِ مبلغ، کیبورد) با رعایتِ حداقل/حداکثرِ تنظیم‌شدهٔ ادمین."""
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    presets = await _wl.allowed_presets(bot_id, "card", WALLET_PRESETS)
    text = "💳 مبلغ شارژ را انتخاب کن یا «✏️ مبلغ دلخواه» را بزن:"
    if await _wl.customized(bot_id, "card"):
        text += f"\n({await _wl.range_hint(bot_id, 'card')})"
    return text, wallet_charge_kb(colored, presets)


@card_wallet_router.callback_query(F.data == "w:charge")
async def w_charge_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import (get_bot_row, get_setting, support_button, edit_or_replace,
                        pay_phone_gate)
    await state.clear()
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_PAY_PHONE_VERIFY_V1 ===
    if await pay_phone_gate(cb.message, row, cb.from_user.id):
        await cb.answer()
        return
    card_enabled = await get_setting(row.id, "card_enabled", "1")
    if card_enabled != "1":
        kb_rows = []
        card_support = await get_setting(row.id, "card_support_contact", "")
        sup_btn = await support_button(row.id, "💬 پیام به پشتیبانی", contact=card_support)
        if sup_btn:
            kb_rows.append([sup_btn])
        kb_rows.append([InlineKeyboardButton(
            text="🔙 بازگشت به کیف پول", callback_data="w:back",
            style=ButtonStyle.DANGER if has_feature(row.plan, Feature.COLORED_BUTTONS) else None)])
        await edit_or_replace(
            cb.message,
            "❌ <b>کارت به کارت غیرفعال است</b>\n\n"
            "در حال حاضر امکان شارژ از طریق کارت‌به‌کارت وجود ندارد.\n"
            "برای اطلاعات بیشتر با پشتیبانی تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
        await cb.answer()
        return
    colored = bool(has_feature(row.plan, Feature.COLORED_BUTTONS))
    _txt, _kb = await _charge_prompt_and_kb(row.id, colored)   # PATCH_WALLET_LIMITS_V1
    await edit_or_replace(cb.message, _txt, reply_markup=_kb)
    await cb.answer()


@card_wallet_router.callback_query(F.data.startswith("wcamt:"))
async def w_charge_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import smart_cancel_kb
    val = cb.data.split(":")[1]
    if val == "custom":
        await state.set_state(CardWalletFSM.amount)
        await cb.answer()
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        # === PATCH_WALLET_LIMITS_V1 === بدونِ تنظیمِ ادمین دقیقاً همان متنِ قبلی
        from tenant import get_bot_row
        from tenant.gateways import wallet_limits as _wl
        _row = await get_bot_row(bot)
        if _row and await _wl.customized(_row.id, "card"):
            _p = f"💳 مبلغ دلخواه را به تومان بفرست (فقط عدد، {await _wl.range_hint(_row.id, 'card')}):"
        else:
            _p = "💳 مبلغ دلخواه را به تومان بفرست (فقط عدد، حداقل ۱۰٬۰۰۰):"
        await cb.message.answer(_p, reply_markup=await smart_cancel_kb(bot))
        return
    await cb.answer()
    await _wallet_charge_proceed(cb.message, state, bot, int(val), cb.from_user.id)


async def _wallet_charge_proceed(message: Message, state: FSMContext, bot: Bot,
                                 amount: int, uid: int):
    from tenant import get_bot_row, get_setting, rial, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        return
    # === PATCH_WALLET_LIMITS_V1 === نقطهٔ مشترکِ دکمهٔ مبلغِ ثابت و مبلغِ دلخواه
    from tenant.gateways import wallet_limits as _wl
    if not await _wl.guard(message, row.id, "card", amount):
        await state.clear()
        return
    # === PATCH_KYC_GATE_BEFORE_CARD_NUMBER_V1 ===
    # باگِ گزارش‌شدهٔ ادمین: قبلاً سقفِ احراز فقط در لحظهٔ «ثبت و ارسالِ» رسید
    # چک می‌شد (تویِ _wrc_ok_impl) — یعنی کاربر اول شماره‌کارت را می‌دید،
    # واریز می‌کرد، رسید می‌فرستاد و بعد از همهٔ اینها می‌فهمید که باید
    # احراز کند. حالا همینجا، بلافاصله بعد از انتخابِ مبلغ و قبل از هر
    # نمایشی از شماره‌کارت، سقف چک می‌شود.
    try:
        from tenant.shared.kyc import check_charge
        u = await get_or_create_user(row.id, uid)
        _ok, _why = await check_charge(row.id, u, amount)
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (card, پیش از نمایشِ شماره‌کارت)")
        _ok, _why = True, ""
    if not _ok:
        await state.clear()
        kyc_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu",
                                  style=ButtonStyle.SUCCESS if has_feature(row.plan, Feature.COLORED_BUTTONS) else None)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back",
                                  style=ButtonStyle.DANGER if has_feature(row.plan, Feature.COLORED_BUTTONS) else None)],
        ])
        await message.answer(_why, reply_markup=kyc_kb)
        return
    await state.update_data(amount=amount)
    # PATCH_SENDER_CARD_CHECK — پرسیدنِ ۴ رقمِ آخرِ کارتِ مبدأ فقط وقتی لازم است
    # که تشخیصِ خودکارِ رسید فعال باشد (چون فقط همان‌جا برای کراس‌چک با OCR استفاده می‌شود).
    from tenant.gateways.card_purchase import sender_gate_enabled  # === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===
    if await sender_gate_enabled(row.id):
        await state.set_state(CardWalletFSM.sender_card4)
        await state.update_data(sender_id_type="card")  # PATCH_ADD_SHEBA_ACCOUNT_ID_TYPE
        _sidtype_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏦 با شبا واریز می‌کنم", callback_data="wsidtype:sheba"),
            InlineKeyboardButton(text="📄 با شماره‌حساب واریز می‌کنم", callback_data="wsidtype:account"),
        ]])
        await message.answer(
            f"💳 <b>شارژ کیف پول</b>\n\n"
            f"مبلغ: <b>{rial(amount)}</b>\n\n"
            f"🔢 لطفاً ۴ رقمِ آخرِ کارتی که می‌خواهید از آن کارت‌به‌کارت کنید را ارسال کنید.\n\n"
            f"🏦 در صورتی که به‌جایِ کارت‌به‌کارت، با شبا یا شماره‌حساب واریز می‌کنید، "
            f"به‌جایِ ارسالِ عدد، از دکمه‌هایِ زیر استفاده کنید:",
            reply_markup=_sidtype_kb,
        )
        return
    await _wallet_show_destination_card(message, state, bot, row, amount)


# PATCH_ADD_SHEBA_ACCOUNT_ID_TYPE
@card_wallet_router.callback_query(CardWalletFSM.sender_card4, F.data.startswith("wsidtype:"))
async def wallet_charge_sender_id_type(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """کاربر نوعِ شناسه (شبا/حساب) رو برای شارژِ کیف‌پول انتخاب کرد."""
    from tenant import smart_cancel_kb
    id_type = cb.data.split(":")[1]
    await state.update_data(sender_id_type=id_type)
    label = "شبا" if id_type == "sheba" else "شماره‌حساب"
    prompt = f"🔢 لطفاً ۴ رقمِ آخرِ {label}ی که می‌خواهید از آن واریز کنید را ارسال کنید:"
    try:
        await cb.message.edit_text(prompt, reply_markup=await smart_cancel_kb(bot))
    except Exception:
        await cb.message.answer(prompt, reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


# PATCH_SENDER_CARD_CHECK
async def _wallet_show_destination_card(message: Message, state: FSMContext, bot: Bot,
                                        row, amount: int):
    from tenant import get_setting, rial
    card = await get_setting(row.id, "card_number", "(شماره کارت ثبت نشده)")
    card_holder = await get_setting(row.id, "card_holder", "")
    await state.set_state(CardWalletFSM.receipt)
    colored = has_feature(row.plan, Feature.COLORED_BUTTONS)
    dang = ButtonStyle.DANGER if colored else None
    holder_line = f"👤 به نام: <b>{card_holder}</b>\n" if card_holder else ""
    from tenant.gateways.card_display import extra_card_block   # PATCH_CARD_BOTH_V1
    holder_line += await extra_card_block(row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به کیف پول", callback_data="w:back", style=dang)],
    ])
    await message.answer(
        f"💳 <b>شارژ کیف پول</b>\n\n"
        f"مبلغ: <b>{rial(amount)}</b>\n\n"
        f"💳 شماره کارت:\n<code>{card}</code>\n"
        f"{holder_line}\n"
        f"پس از واریز، رسید (عکس یا متن) را ارسال کنید:\n\n"
        f"⏳ <b>مهلت پرداخت: ۱۰ دقیقه</b> (تا ساعت "
        f"{(_dt.datetime.utcnow() + _dt.timedelta(hours=3, minutes=40)).strftime('%H:%M')})\n"
        "اگر تا این زمان رسید ارسال نشود، درخواست خودکار لغو می‌شود.",
        reply_markup=kb,
    )


@card_wallet_router.message(CardWalletFSM.sender_card4)
async def w_charge_sender_card4(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row
    digits = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r'\D', '', digits)
    if len(digits) != 4:
        await msg.answer("لطفاً دقیقاً ۴ رقمِ آخر را به‌صورتِ عدد ارسال کنید (مثلاً 1234).")
        return
    row = await get_bot_row(bot)
    if not row:
        return
    await state.update_data(sender_card4=digits)
    data = await state.get_data()
    await _wallet_show_destination_card(msg, state, bot, row, data["amount"])


@card_wallet_router.message(CardWalletFSM.amount)
async def w_charge_amount(msg: Message, state: FSMContext, bot: Bot):
    from tenant import fa_to_int, get_bot_row
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    amount = fa_to_int(msg.text)
    _row = await get_bot_row(bot)
    if not amount or not _row:
        await msg.answer("مبلغ نامعتبر است؛ یک عدد به تومان بفرست.")
        return
    if not await _wl.guard(msg, _row.id, "card", amount):
        return                       # در همین مرحله می‌ماند تا مبلغِ درست بفرستد
    await _wallet_charge_proceed(msg, state, bot, amount, msg.from_user.id)


async def _receipt_state_alive(msg: Message, state: FSMContext) -> bool:
    """=== PATCH_RECEIPT_STATE_ALIVE_V1 === بعد از قطعیِ سرویس، تلگرام آپدیت‌هایِ صف‌شده را یک‌جا دوباره می‌فرستد؛
    اگر درخواستِ شارژ در این فاصله تمام/لغو شده باشد، stateِ receipt بدونِ «amount» می‌ماند و هندلر KeyError می‌داد.
    در این حالت state پاک می‌شود و به کاربر گفته می‌شود از اول شروع کند."""
    data = await state.get_data()
    if data.get("amount"):
        return True
    await state.clear()
    await msg.answer("⌛ این درخواستِ شارژ دیگر فعال نیست (قبلاً ثبت یا لغو شده). اگر لازم است، دوباره از «شارژ کیف پول» شروع کن.")
    return False


@card_wallet_router.message(CardWalletFSM.receipt, F.photo)
async def w_charge_receipt(msg: Message, state: FSMContext):
    from tenant import rial
    if not await _receipt_state_alive(msg, state):
        return
    await state.update_data(file_id=msg.photo[-1].file_id,
                            file_unique_id=msg.photo[-1].file_unique_id, receipt_text=None)
    await state.set_state(CardWalletFSM.confirm)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ثبت و ارسال", callback_data="wrc:ok", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ ارسال رسید دیگر", callback_data="wrc:edit", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو", callback_data="wrc:cancel", style=ButtonStyle.DANGER)],
    ])
    await msg.answer_photo(
        data["file_id"],
        caption=f"رسید برای مبلغ {rial(data['amount'])}.\nثبت شود؟",
        reply_markup=kb,
    )


@card_wallet_router.message(CardWalletFSM.receipt, F.text)
async def w_charge_receipt_text(msg: Message, state: FSMContext):
    """قبول رسید متنی (متن کپی‌شده از اپ بانکی)"""
    from tenant import rial
    # === PATCH_RECEIPT_STATE_CANCEL_V1 === اول خروج، بعد اعتبارسنجی
    if _is_exit_text(msg.text):
        await state.clear()
        raise SkipHandler
    if not msg.text or not msg.text.strip() or not any(ch.isdigit() for ch in msg.text):
        await msg.answer("⚠️ متن رسید باید شامل عدد باشد (مثلاً مبلغ یا کد پیگیری). لطفاً رسید را دوباره ارسال کنید.")
        return
    if not await _receipt_state_alive(msg, state):
        return
    await state.update_data(file_id=None, file_unique_id=None, receipt_text=msg.text)
    await state.set_state(CardWalletFSM.confirm)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ثبت و ارسال", callback_data="wrc:ok", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ ارسال رسید دیگر", callback_data="wrc:edit", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو", callback_data="wrc:cancel", style=ButtonStyle.DANGER)],
    ])
    # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
    await msg.answer(
        f"📋 <b>رسید متنی دریافت شد</b>\n\n"
        f"مبلغ: {rial(data['amount'])}\n\n"
        f"<blockquote>{_html.escape(msg.text[:500])}</blockquote>\n\n"
        f"ثبت شود؟",
        reply_markup=kb,
    )


@card_wallet_router.message(CardWalletFSM.receipt)
async def w_charge_receipt_wrong(msg: Message):
    await msg.answer("لطفاً عکس رسید یا متن رسید را ارسال کنید.")


@card_wallet_router.message(CardWalletFSM.confirm, F.photo)
async def w_charge_confirm_newphoto(msg: Message, state: FSMContext):
    await state.update_data(file_id=msg.photo[-1].file_id,
                            file_unique_id=msg.photo[-1].file_unique_id)
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ثبت و ارسال", callback_data="wrc:ok", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ ارسال رسید دیگر", callback_data="wrc:edit", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو", callback_data="wrc:cancel", style=ButtonStyle.DANGER)],
    ])
    await msg.answer_photo(data["file_id"], caption="رسید جدید دریافت شد. ثبت شود؟", reply_markup=kb)


@card_wallet_router.callback_query(CardWalletFSM.confirm, F.data == "wrc:edit")
async def wrc_edit(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import smart_cancel_kb
    await state.set_state(CardWalletFSM.receipt)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("عکس رسید یا متن رسید جدید را ارسال کنید:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@card_wallet_router.callback_query(CardWalletFSM.confirm, F.data == "wrc:cancel")
async def wrc_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, main_kb_or_remove
    await state.clear()
    row = await get_bot_row(bot)
    kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id) if row else ReplyKeyboardRemove()
    await cb.message.answer("لغو شد.", reply_markup=kb)
    await cb.answer()


# === PATCH_WALLET_CONFIRM_HARD_LOCK ===
# قفلِ in-memory و همزمان (بدون هیچ await قبل از قفل‌شدن) که تپِ دوم را
# حتی قبل از رسیدنِ درخواست به دیتابیس رد می‌کند. چون asyncio تک‌رشته‌ای
# است، بینِ چک و ست‌کردنِ این مجموعه هیچ نقطهٔ تعویضِ context وجود ندارد،
# پس نیازی به asyncio.Lock یا هماهنگیِ اضافه نیست.
_wallet_confirm_inflight: set[int] = set()


@card_wallet_router.callback_query(CardWalletFSM.confirm, F.data == "wrc:ok")
async def wrc_ok(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    if uid in _wallet_confirm_inflight:
        # تپِ دوم (یا بیشتر) — همینجا و فوری رد می‌شود، بدون هیچ پردازشی
        await cb.answer()
        return
    _wallet_confirm_inflight.add(uid)
    try:
        await _wrc_ok_impl(cb, state, bot)
    finally:
        _wallet_confirm_inflight.discard(uid)


async def _wrc_ok_impl(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import (get_bot_row, get_setting, main_kb_or_remove, rial,
                        evaluate_receipt, _notify_receipt_admins, _admin_user_block)  # === PATCH_RECEIPT_CHANNEL_V1 ===
    data = await state.get_data()
    amount = data["amount"]
    file_id = data.get("file_id")
    file_unique_id = data.get("file_unique_id")
    receipt_text = data.get("receipt_text")
    sender_card4 = data.get("sender_card4")
    sender_id_type = data.get("sender_id_type", "card")  # PATCH_ADD_SHEBA_ACCOUNT_ID_TYPE
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        await cb.answer()
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u:
            await cb.answer()
            return
        # === PATCH_KYC_CAP_V1 === سقفِ احرازِ هویت
        try:
            from tenant.shared.kyc import check_charge
            _ok, _why = await check_charge(row.id, u, amount)
        except Exception:
            logger.exception("kyc: بررسیِ سقف ناموفق بود (card)")
            _ok, _why = True, ""
        if not _ok:
            await cb.message.answer(_why)
            await cb.answer()
            return
        # ثبت سفارش شارژ به عنوان یک Order با pay_method=card و status=awaiting_admin
        o = Order(bot_id=row.id, user_id=u.id, volume_gb=0, days=0,
                  count=1, amount=amount, pay_method="card_charge",
                  status="awaiting_admin", receipt_file_id=file_id,
                  receipt_file_unique_id=file_unique_id)
        s.add(o)
        await s.flush()
        oid = o.id
        await s.commit()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
    await cb.message.answer(
        "✅ رسید ثبت شد. بعد از تایید ادمین، کیف پولت شارژ می‌شود.",
        reply_markup=kb,
    )

    # ── تایید خودکار (در صورت فعال بودن) ── PATCH_AUTO_RECEIPT_RIAL_TOMAN_AND_FAIL_NOTICE
    auto_fail_reason = None
    if await get_setting(row.id, "auto_approve_receipts", "0") == "1":
        from tenant.gateways.card_display import receipt_dest_values as _rdv   # PATCH_CARD_BOTH_V1
        card_number, sheba_number, account_number = await _rdv(row.id)
        check = await evaluate_receipt(bot, receipt_text, file_id, amount, card_number, None,
                                       expected_sender_last4=sender_card4,
                                       expected_sender_id_type=sender_id_type,
                                       expected_sheba_number=sheba_number,
                                       expected_account_number=account_number,
                                       bot_id=row.id)
        if check.ok:
            done = await _auto_approve_wallet_charge(bot, row, oid, check, file_id, receipt_text,
                                                      file_unique_id=file_unique_id)
            if done:
                await cb.answer("تایید خودکار انجام شد ✅")
                return
        from tenant.gateways.auto_receipt import duplicate_receipt_reason
        _dup_reason = await duplicate_receipt_reason(f"tenant:{row.id}", check, file_unique_id) if check.ok else None
        auto_fail_reason = _dup_reason or check.reason

    # اطلاع به مالک و ادمین‌های پشتیبانی (تایید دستی)
    kb_admin = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data=f"chadm:ok:{oid}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"chadm:no:{oid}"),
    ]])
    notif = (
        f"💳 <b>رسید پرداخت</b>\n\n"
        f"{await _admin_user_block(u, cb.from_user.id, bot_id=row.id)}\n"
        f"💵 مبلغ سفارش: {rial(amount)}\n"
        + (f"🔢 {'شبا' if sender_id_type == 'sheba' else 'حسابِ' if sender_id_type == 'account' else 'کارتِ'} مبدأ (اعلامِ کاربر): ****{sender_card4}\n" if sender_card4 else "")
        + f"سفارش: #{oid}"
    )
    if auto_fail_reason:
        notif += (
            f"\n\n🤖 تایید خودکار فعال است ولی این رسید به‌صورت خودکار تایید نشد.\n"
            f"دلیل: {auto_fail_reason}"
        )
    if receipt_text:
        notif += f"\n\n📋 رسید متنی:\n<blockquote>{_html.escape(receipt_text[:500])}</blockquote>"  # PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1
    await _notify_receipt_admins(bot, row, notif, kb_admin, file_id)  # === PATCH_RECEIPT_CHANNEL_V1 ===
    await cb.answer("ثبت شد")


async def _auto_approve_wallet_charge(bot: Bot, row, oid: int, check, file_id: str | None,
                                      receipt_text: str | None,
                                      file_unique_id: str | None = None) -> bool:
    """تایید خودکارِ سفارشِ شارژِ کیف‌پول (وقتی مبلغ/شماره‌کارت/تاریخِ رسید با
    سفارش مطابقت داشته باشد). کیف‌پول را شارژ می‌کند و گزارشِ کامل را برای
    ادمین می‌فرستد تا در صورتِ فیک بودن با دکمهٔ «لغو» برگردانده شود."""
    from tenant import rial, inline_back_kb, _notify_receipt_admins, _admin_user_block  # === PATCH_RECEIPT_CHANNEL_V1 ===
    # جلوگیری از رسیدِ تکراری (هر ربات scope جدا دارد)
    from tenant.gateways.auto_receipt import (receipt_fingerprint, photo_fingerprint,
                                              claim_receipts, release_receipts)
    _dup_scope = f"tenant:{row.id}"
    _dup_fp = receipt_fingerprint(check)
    _photo_fp = photo_fingerprint(file_unique_id)
    # PATCH_RECEIPT_CLAIM_ATOMIC_V1: ثبتِ اتمیکِ رسید *قبل* از شارژ (تکراری ⇒ تأییدِ دستی)
    _fps = [_photo_fp, _dup_fp]
    if not _dup_fp:   # PATCH_RECEIPT_REQUIRE_FP_V1: بدونِ پیگیری/تاریخ‌وساعت تکراری‌بودن سنجیدنی نیست ⇒ بررسیِ دستی
        return False
    if not await claim_receipts(_dup_scope, _fps):
        return False
    async with Session() as s:
        # قفلِ سفارش: هم‌زمانیِ تأییدِ خودکار و تأییدِ دستیِ ادمین (chadm_decide هم
        # FOR UPDATE می‌گیرد) قبلاً می‌توانست کیف پول را دوبار شارژ کند.
        o = await s.get(Order, oid, with_for_update=True)
        if not o or o.bot_id != row.id or o.status != "awaiting_admin":
            await release_receipts(_dup_scope, _fps)
            return False
        u = await s.get(BotUser, o.user_id, with_for_update=True)
        amount = float(o.amount)
        u.balance = float(u.balance) + amount
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=amount,
                        kind="charge", note=f"AUTO:شارژ خودکار سفارش #{oid}"))
        o.status = "paid"
        _kyc_uid = u.id
        await s.commit()
        tg_id = u.telegram_id
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    from tenant.shared.kyc import after_charge as _ac
    await _ac(row.id, _kyc_uid)
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
        wtext, wkb = await build_wallet_charge_message_for_bot(row.id, amount, u.balance, "card")
        await bot.send_message(tg_id, wtext, reply_markup=wkb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_TX_LOG_CHANNEL_V1 ===
    try:
        from tenant.shared.tx_channel import tx_log
        _c4 = getattr(check, "card_last4", "") or ""
        await tx_log(bot, row.id, buyer=u, amount=amount, gateway="card",
                     old_balance=(float(u.balance) - float(amount)),
                     new_balance=float(u.balance),
                     card=(f"****{_c4}" if _c4 else ""))
    except Exception:
        logger.exception("tx_log (card)")
    detail_lines = [f"🔎 مبلغِ خوانده‌شده: {rial(check.amount)}"]
    if check.card_last4:
        detail_lines.append(f"💳 ۴ رقمِ آخرِ کارت روی رسید: <code>{check.card_last4}</code>")
    if check.date_jalali:
        detail_lines.append(f"📅 تاریخِ رسید: {check.date_jalali}")
    report = (
        f"🤖 <b>تایید خودکار رسید — شارژ کیف پول</b>\n\n"
        f"{await _admin_user_block(u, tg_id, bot_id=row.id)}\n"
        f"💵 مبلغ سفارش: {rial(amount)}\n"
        + "\n".join(detail_lines) + "\n"
        f"✅ نتیجهٔ بررسی: {check.reason}\n"
        f"سفارش: #{oid}\n\n"
        f"✅ به‌صورت خودکار تایید و کیف پول کاربر شارژ شد.\n"
        f"⚠️ اگر این رسید فیک/مشکوک است، با دکمهٔ زیر لغو کنید."
    )
    if receipt_text:
        report += f"\n\n📋 رسید متنی:\n<blockquote>{_html.escape(receipt_text[:500])}</blockquote>"  # PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 لغو (رسید فیک بود)", callback_data=f"chadm_revoke:{oid}"),
    ]])
    await _notify_receipt_admins(bot, row, report, kb, file_id)  # === PATCH_RECEIPT_CHANNEL_V1 ===
    return True
