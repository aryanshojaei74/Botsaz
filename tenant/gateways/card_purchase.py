"""
card_purchase.py — هندلرهای aiogram مربوط به «خریدِ مستقیمِ کانفیگ/سرویس با
کارت‌به‌کارت» (نه شارژِ کیف‌پول — آن در tenant/card_wallet.py است):

  • شاخهٔ «card» از دیسپچرِ عمومیِ pay_method (که در tenant/__init__.py
    می‌ماند، چون «wallet»/«cancel» هم آنجا مدیریت می‌شوند)
  • گیتِ اختیاریِ OCR (پرسیدنِ ۴ رقمِ آخرِ کارت/شبا/حساب قبل از نمایشِ فاکتور)
  • پرداختِ کارتی برای «انبار دستی» (mcfg_card)
  • دریافتِ رسید (عکس/متن) — هندلرهای سراسریِ StateFilter(None)
  • تاییدِ نهاییِ کاربر و تاییدِ خودکار/دستیِ ادمین

⚠️ نکاتِ scope (طبق CARD_HANDOFF.md و بررسی‌های این نشست):
  • خریدِ مستقیمِ VPN از پنل (panel auto-provision) در tenant/vpn_user.py
    زندگی می‌کند و pay_method="vpn_card" دارد؛ آن فایل مستقل است و این‌جا
    فقط چیزهایی که pay_method در ("card","mcfg_card") دارند منتقل شده‌اند.
    نکتهٔ جانبی: vpn_user.py گیتِ OCR (سنجشِ ۴رقمِ‌آخر) را صدا نمی‌زند؛ این
    را جداگانه در گزارشِ این نشست به کاربر اطلاع بده/بررسی کن.
  • هندلرهای `chadm:*` (تاییدِ دستیِ شارژِ کیف‌پول) عمداً در tenant/__init__.py
    مانده‌اند چون در جاهای دیگر (نه فقط کارت) هم استفاده می‌شوند.
  • `_admin_user_block` و `_notify_admins` مشترکِ چند حوزه‌اند و منتقل نشده‌اند.
  • `_ManualCfgNameFSM` / `_ManualCfgDiscountFSM` (نام‌گذاری و کدِ تخفیفِ
    انبارِ دستی، مراحلِ پیش از پرداخت) هم منتقل نشده‌اند — به‌طور مستقیم به
    این ماژول وابسته نیستند (فقط دیتا از طریق state به اشتراک می‌گذارند).

از همان الگوی ماژول‌های مستقلِ دیگر پیروی می‌کند: Router و StatesGroup
مستقلِ خودش را دارد (به‌جای استفادهٔ مجدد از AdminFSM/WalletFSM/
CardSenderCardFSM غول‌پیکر) و توابعِ مشترکِ tenant/__init__.py را با
import محلی می‌گیرد تا از circular import جلوگیری شود.
"""
import datetime as _dt
import html as _html  # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler  # PATCH_RECEIPT_STATE_CANCEL_V1
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser, Config, Order
from models import order_no  # شناسهٔ واحدِ سفارش

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

card_purchase_router = Router()


class CardPurchaseFSM(StatesGroup):
    sender_card4 = State()   # گیتِ OCR: ۴ رقمِ آخرِ کارت/شبا/حسابِ مبدأ (قبل از فاکتور)
    reject_reason = State()   # ادمین: دلیلِ ردِ رسید


# ═══════════════════ گیتِ OCR (پرسیدنِ ۴ رقمِ آخر قبل از فاکتور) ═══════════════════

# === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===
async def _card_both_block(bot_id: int) -> str:   # PATCH_CARD_BOTH_V1
    from tenant.gateways.card_display import extra_card_block
    return await extra_card_block(bot_id)


async def sender_gate_enabled(bot_id: int) -> bool:
    """آیا باید ۴ رقمِ آخرِ کارت/شبا/حسابِ مبدأ را از کاربر پرسید؟

    گزارشِ ادمین (ایلیا): وقتی تأییدِ خودکارِ رسید روشن است، پرسیدنِ ۴ رقمِ
    آخر عملاً بی‌فایده است — کاربر می‌تواند رسیدِ جعلی یا رسیدِ شخصِ دیگری
    را بفرستد و همان ۴ رقم را از رویِ خودِ همان رسید وارد کند، پس این چک
    هیچ امنیتی اضافه نمی‌کند؛ ضمناً برایِ پرداخت‌هایِ بلو فقط شبا نشان داده
    می‌شود و «کارت» اصلاً معنایی ندارد. حالا ادمین می‌تواند این پرسش را
    جدا خاموش کند (کلیدِ receipt_sender_gate در تنظیماتِ کارت). پیش‌فرض
    «روشن» است تا رفتارِ ربات‌هایِ موجود بدونِ انتخابِ خودِ ادمین عوض نشود.
    اگر خاموش باشد، sender_card4 هیچ‌وقت ذخیره نمی‌شود و بررسیِ رسید
    (auto_receipt) آن معیار را خودکار حذف می‌کند."""
    from tenant import get_setting
    if await get_setting(bot_id, "auto_approve_receipts", "0") != "1":
        return False
    return await get_setting(bot_id, "receipt_sender_gate", "1") != "0"

async def maybe_gate_card_invoice(target, state: FSMContext, bot_id: int, oid: int,
                                  invoice_text: str, cancel_cb: str) -> bool:
    """اگر تشخیصِ خودکارِ رسید فعال باشد، به‌جای نمایشِ مستقیمِ پیش‌فاکتور، اول
    ۴ رقمِ آخرِ کارتِ مبدأ را از کاربر می‌پرسد (برای کراس‌چک با OCR) و پیش‌فاکتور
    را برای بعد از پاسخ ذخیره می‌کند. خروجی True یعنی «گیت شد، فراخوان چیزِ
    دیگری نفرستد»؛ False یعنی auto-approve خاموش است و باید مثلِ قبل رفتار کند.

    فراخوانی می‌شود از: (۱) شاخهٔ «card» در pay_method، (۲) manual_cfg_paycard.
    توجه: tenant/vpn_user.py (خریدِ مستقیمِ پنل) این گیت را صدا نمی‌زند."""
    if not await sender_gate_enabled(bot_id):   # === PATCH_RECEIPT_SENDER_GATE_TOGGLE_V1 ===
        return False
    await state.update_data(oid=oid, pending_invoice_text=invoice_text, pending_cancel_cb=cancel_cb,
                            sender_id_type="card")
    await state.set_state(CardPurchaseFSM.sender_card4)
    prompt = ("🔢 قبل از نمایشِ شماره‌کارت، لطفاً ۴ رقمِ آخرِ کارتی که می‌خواهید از آن "
              "کارت‌به‌کارت کنید را ارسال کنید.\n\n"
              "🏦 در صورتی که به‌جایِ کارت‌به‌کارت، با شبا یا شماره‌حساب واریز می‌کنید، "
              "به‌جایِ ارسالِ عدد، از دکمه‌هایِ زیر استفاده کنید:")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 با شبا واریز می‌کنم", callback_data="sidtype:sheba"),
         InlineKeyboardButton(text="📄 با شماره‌حساب واریز می‌کنم", callback_data="sidtype:account")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=cancel_cb)],
    ])
    try:
        await target.message.edit_text(prompt, reply_markup=kb)
    except Exception:
        await target.message.answer(prompt, reply_markup=kb)
    return True


# === PATCH_RECEIPT_STATE_CANCEL_V1 ===
# باگِ گزارش‌شده: کاربر وسطِ ارسالِ رسید دکمهٔ «⬅️ بازگشت به منو» را می‌زد،
# ولی چون آن متن عدد ندارد، ربات می‌گفت «متن رسید باید شامل عدد باشد» و
# کاربر در همان مرحله گیر می‌کرد. این تابع متن‌هایِ خروج را می‌شناسد.
def _is_exit_text(txt: str) -> bool:
    t = (txt or "").strip()
    return t in ("⬅️ بازگشت به منو", "بازگشت به منو", "❌ لغو",
                 "❌ لغو / بازگشت", "لغو", "بازگشت", "/start", "/cancel")


@card_purchase_router.callback_query(CardPurchaseFSM.sender_card4, F.data.startswith("sidtype:"))
async def card_order_sender_id_type(cb: CallbackQuery, state: FSMContext):
    """کاربر نوعِ شناسه (شبا/حساب) رو انتخاب کرد؛ پیام رو مطابقش عوض می‌کنیم."""
    id_type = cb.data.split(":")[1]
    data = await state.get_data()
    cancel_cb = data.get("pending_cancel_cb")
    await state.update_data(sender_id_type=id_type)
    label = "شبا" if id_type == "sheba" else "شماره‌حساب"
    prompt = f"🔢 لطفاً ۴ رقمِ آخرِ {label}ی که می‌خواهید از آن واریز کنید را ارسال کنید:"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data=cancel_cb)]]) \
        if cancel_cb else None
    try:
        await cb.message.edit_text(prompt, reply_markup=kb)
    except Exception:
        await cb.message.answer(prompt, reply_markup=kb)
    await cb.answer()


@card_purchase_router.message(CardPurchaseFSM.sender_card4)
async def card_order_sender_card4(msg: Message, state: FSMContext):
    import re
    digits = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r'\D', '', digits)
    if len(digits) != 4:
        await msg.answer("لطفاً دقیقاً ۴ رقمِ آخر را به‌صورتِ عدد ارسال کنید (مثلاً 1234).")
        return
    data = await state.get_data()
    oid = data.get("oid")
    text = data.get("pending_invoice_text")
    cancel_cb = data.get("pending_cancel_cb")
    id_type = data.get("sender_id_type", "card")
    await state.clear()
    if not oid or not text:
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, msg.bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if o and o.status == "awaiting_receipt":
            o.sender_card4 = digits
            o.sender_id_type = id_type
            await s.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data=cancel_cb)]]) \
        if cancel_cb else None
    await msg.answer(text, reply_markup=kb)


# ═══════════════════ شاخهٔ «card» از pay_method (فروشگاهِ معمولی) ═══════════════════

async def handle_card_payment_method(cb: CallbackQuery, state: FSMContext, bot: Bot, bot_id: int,
                                     oid: int, amount: float, volume_gb: int, days: int, count: int):
    """پیاده‌سازیِ شاخهٔ method=="card" از pay_method. از تنانتِ اصلی با
    import محلی صدا زده می‌شود تا pay_method (که «wallet»/«cancel» را هم
    مدیریت می‌کند) دیسپچرِ نازک باقی بماند."""
    from tenant import get_setting, support_button, edit_or_replace, _attach_invoice, rial, get_or_create_user
    card_enabled = await get_setting(bot_id, "card_enabled", "1")
    card_purchase_enabled = await get_setting(bot_id, "card_purchase_enabled", "1")
    # === PATCH_PAY_PHONE_GATE_CARD_BUY_V1 === شماره پیش از نمایشِ شمارهٔ کارت (فروشگاهِ معمولی)
    if card_enabled == "1" and card_purchase_enabled == "1":
        from tenant import get_bot_row as _gbr, pay_phone_gate as _ppg
        _row_pg = await _gbr(bot)
        if _row_pg and await _ppg(cb.message, _row_pg, cb.from_user.id):
            await cb.answer("📱 اول شماره‌ات را تایید کن.", show_alert=True)
            return
    if card_enabled != "1" or card_purchase_enabled != "1":
        card_support = await get_setting(bot_id, "card_support_contact", "")
        sup_btn = await support_button(bot_id, "💬 پیام به پشتیبانی", contact=card_support)
        kb_rows = [[sup_btn]] if sup_btn else []
        kb_rows.append([InlineKeyboardButton(text="💰 پرداخت از کیف پول", callback_data=f"pay:wallet:{oid}")])
        kb_rows.append([InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"pay:cancel:{oid}")])
        await edit_or_replace(
            cb.message,
            "❌ <b>کارت به کارت غیرفعال است</b>\n\n"
            "در حال حاضر امکان پرداخت از طریق کارت‌به‌کارت وجود ندارد.\n"
            "برای اطلاعات بیشتر با پشتیبانی تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
        await cb.answer()
        return
    # === PATCH_KYC_GATE_CARD_PURCHASE_V1 ===
    # این مسیر (خریدِ مستقیمِ کانفیگ/سرویس با کارت) اصلاً سقفِ احرازِ هویت را
    # چک نمی‌کرد — کاربر همیشه شماره‌کارت را می‌دید و می‌توانست بدونِ هیچ
    # محدودیتی واریز کند. طبق همون منطقی که برایِ شارژِ کیف‌پول اصلاح شد،
    # اینجا هم باید *قبل* از نمایشِ شماره‌کارت چک شود.
    try:
        from tenant.shared.kyc import check_charge
        _u = await get_or_create_user(bot_id, cb.from_user.id)
        _ok, _why = await check_charge(bot_id, _u, amount)
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (card purchase, پیش از نمایشِ شماره‌کارت)")
        _ok, _why = True, ""
    if not _ok:
        kb_rows = [
            [InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="💰 پرداخت از کیف پول", callback_data=f"pay:wallet:{oid}")],
            [InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"pay:cancel:{oid}")],
        ]
        await edit_or_replace(cb.message, _why, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await cb.answer()
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        o.status = "awaiting_receipt"
        o.pay_method = "card"
        await s.commit()
    card        = await get_setting(bot_id, "card_number", "(شماره کارت ثبت نشده)")
    card_holder = await get_setting(bot_id, "card_holder", "")
    inv_no, trk = await _attach_invoice(oid, bot_id)
    vol  = "نامحدود" if volume_gb == 0 else f"{volume_gb} گیگ"
    dur  = "بی‌انقضا" if days == 0 else f"{days} روز"
    _deadline_ir = _dt.datetime.utcnow() + _dt.timedelta(hours=3, minutes=30) + _dt.timedelta(minutes=10)
    _deadline_str = _deadline_ir.strftime("%H:%M")
    text = (
        "🧾 <b>پیش فاکتور</b>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 سرویس: <b>{vol} / {dur}</b>\n"
        f"🔢 تعداد: <b>{count} عدد</b>\n"
        f"💰 مبلغ قابل پرداخت: <b>{rial(amount)}</b>\n"
        f"🔖 شماره فاکتور: <code>{inv_no}</code>\n"
        f"🆔 کد پیگیری: <code>{trk}</code>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💳 <b>اطلاعات واریز</b>\n\n"
        f"<code>{card}</code>\n"
        + (f"👤 به نام: <b>{card_holder}</b>\n" if card_holder else "")
        + await _card_both_block(bot_id)
        + f"\n💵 مبلغ: <code>{int(amount):,} تومان</code>\n\n"
        "⬆️ روی شماره کارت یا مبلغ بزن تا کپی شود.\n"
        "📸 پس از واریز، <b>متن یا عکس رسید</b> را اینجا ارسال کن.\n\n"
        f"⏳ <b>مهلت پرداخت: ۱۰ دقیقه</b> (تا ساعت {_deadline_str})\n"
        "اگر تا این زمان رسید ارسال نشود، سفارش خودکار لغو می‌شود."
    )
    _paycard_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"pay:cancel:{oid}")
    ]])
    try:
        if await maybe_gate_card_invoice(cb, state, bot_id, oid, text, f"pay:cancel:{oid}"):
            await cb.answer()
            return
        await cb.message.edit_text(text, reply_markup=_paycard_kb)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        try:
            await cb.message.answer(text, reply_markup=_paycard_kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


# ═══════════════════ پرداختِ کارتی برای انبار دستی (mcfg_card) ═══════════════════

@card_purchase_router.callback_query(F.data.startswith("mcfg:paycard:"))
async def manual_cfg_paycard(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پرداخت کارت‌به‌کارت برای انبار دستی: یک کانفیگ مشخص رزرو می‌شود تا تایید ادمین."""
    from tenant import get_bot_row, get_setting, _attach_invoice, rial
    _, _, vol, days, users, price = cb.data.split(":")
    vol, days, users, price = int(vol), int(days), int(users), int(price)
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    bot_id = row.id
    card_enabled = await get_setting(bot_id, "card_enabled", "1")
    card_purchase_enabled = await get_setting(bot_id, "card_purchase_enabled", "1")
    if card_enabled != "1" or card_purchase_enabled != "1":
        await cb.answer("❌ کارت‌به‌کارت برای خرید غیرفعال است.", show_alert=True)
        return
    # === PATCH_PAY_PHONE_GATE_CARD_BUY_V1 === شماره پیش از نمایشِ شمارهٔ کارت
    from tenant import pay_phone_gate
    if await pay_phone_gate(cb.message, row, cb.from_user.id):
        await cb.answer("📱 اول شماره‌ات را تایید کن.", show_alert=True)
        return
    # === PATCH_KYC_GATE_CARD_PURCHASE_V1 ===
    # همون گیتِ سقفِ احراز که برایِ شاخهٔ فروشگاهِ معمولی اضافه شد؛ اینجا هم
    # باید *قبل* از رزروِ کانفیگ و نمایشِ شماره‌کارت چک شود، نه بعد از رسید.
    try:
        from tenant.shared.kyc import check_charge
        from tenant import get_or_create_user
        _u = await get_or_create_user(bot_id, cb.from_user.id)
        _ok, _why = await check_charge(bot_id, _u, price)
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (mcfg card purchase, پیش از نمایشِ شماره‌کارت)")
        _ok, _why = True, ""
    if not _ok:
        await cb.message.answer(_why, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu", style=ButtonStyle.SUCCESS)]]))
        await cb.answer()
        return
    _data = await state.get_data()
    label = (_data.get("mcfg_custom_label") or "").strip()[:60] or None
    stored_disc_code = _data.get("mcfg_disc_code")
    category = _data.get("mcfg_category")
    final_price = price
    applied_code = None
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id))
        if u is None:
            u = BotUser(bot_id=bot_id, telegram_id=cb.from_user.id,
                        full_name=cb.from_user.full_name)
            s.add(u)
            await s.flush()
        cfg_q = select(Config).where(
            Config.bot_id == bot_id, Config.status == "in_stock", Config.price == price,
            Config.volume_gb == vol, Config.days == days,
            Config.user_count == users)
        if category:
            cfg_q = cfg_q.where(Config.category.in_(category.split(",")))
        cfg = await s.scalar(cfg_q.limit(1))
        if not cfg:
            await cb.answer("این بسته دیگر موجود نیست.", show_alert=True)
            return
        disc_code_id = None
        if stored_disc_code:
            from tenant.gateways.discount_engine import validate_discount
            res = await validate_discount(
                bot_id=bot_id, code=stored_disc_code, telegram_id=cb.from_user.id,
                price=price, panel_key=None, plan_id=None, button_id=_data.get("mcfg_mb_id"), session=s)
            if res.ok and res.discount > 0:
                final_price = res.final_price
                applied_code = res.code
                disc_code_id = res.code_id
        cfg.status = "reserved"
        o = Order(bot_id=bot_id, user_id=u.id, volume_gb=vol, days=days,
                  count=1, amount=final_price, pay_method="mcfg_card", status="awaiting_receipt",
                  custom_label=label, reserved_config_id=cfg.id, discount_code=applied_code)
        s.add(o)
        if disc_code_id:
            from tenant.gateways.discount_engine import record_usage
            await record_usage(s, code_id=disc_code_id, bot_id=bot_id, telegram_id=cb.from_user.id,
                               saved_amount=res.discount)
        await s.commit()
        oid = o.id
    await state.clear()
    await cb.answer()
    card        = await get_setting(bot_id, "card_number", "(شماره کارت ثبت نشده)")
    card_holder = await get_setting(bot_id, "card_holder", "")
    inv_no, trk = await _attach_invoice(oid, bot_id)
    vol_lbl = "♾️ نامحدود" if vol == 0 else f"{vol} گیگ"
    users_lbl = f"\n👥 کاربر: {users}" if users else ""
    label_line = f"\n✏️ نام دلخواه: <b>{label}</b>" if label else ""
    disc_line = f"\n🏷 کد {applied_code}: −{rial(price - final_price)} تومان" if applied_code else ""
    text = (
        "🧾 <b>پیش فاکتور</b>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 سرویس: <b>{vol_lbl}</b>\n"
        f"📅 مدت: <b>{'♾️ بی‌انقضا' if not days else f'{days} روز'}</b>{users_lbl}{label_line}{disc_line}\n"
        f"💰 مبلغ قابل پرداخت: <b>{rial(final_price)}</b>\n"
        f"🔖 شماره فاکتور: <code>{inv_no}</code>\n"
        f"🆔 کد پیگیری: <code>{trk}</code>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💳 <b>اطلاعات واریز</b>\n\n"
        f"<code>{card}</code>\n"
        + (f"👤 به نام: <b>{card_holder}</b>\n" if card_holder else "")
        + await _card_both_block(bot_id)
        + f"\n💵 مبلغ: <code>{int(final_price):,} تومان</code>\n\n"
        "⬆️ روی شماره کارت یا مبلغ بزن تا کپی شود.\n"
        "📸 پس از واریز، <b>متن یا عکس رسید</b> را اینجا ارسال کن.\n\n"
        f"⏳ <b>مهلت پرداخت: ۱۰ دقیقه</b> (تا ساعت "
        f"{(_dt.datetime.utcnow() + _dt.timedelta(hours=3, minutes=40)).strftime('%H:%M')})\n"
        "اگر تا این زمان رسید ارسال نشود، سفارش خودکار لغو می‌شود و کانفیگ آزاد می‌شود."
    )
    try:
        if await maybe_gate_card_invoice(cb, state, bot_id, oid, text, f"mcfgpay:cancel:{oid}"):
            await cb.answer()
            return
        await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data=f"mcfgpay:cancel:{oid}")]]))
    except Exception:
        await cb.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data=f"mcfgpay:cancel:{oid}")]]))


@card_purchase_router.callback_query(F.data.startswith("mcfgpay:cancel:"))
async def manual_cfg_paycard_cancel(cb: CallbackQuery, bot: Bot):
    """لغو سفارش کارت‌به‌کارتِ انبار دستی و آزادسازی کانفیگ رزروشده."""
    from tenant import get_bot_row, get_setting, main_kb_or_remove, main_inline_kb
    oid = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if o is not None:
            # فقط صاحبِ سفارش می‌تواند لغوش کند (وگرنه هر کاربر سفارشِ دیگران را لغو می‌کرد)
            _owner = await s.get(BotUser, o.user_id)
            if not _owner or _owner.telegram_id != cb.from_user.id:
                o = None
        if o and o.status == "awaiting_receipt" and o.pay_method == "mcfg_card":
            if o.reserved_config_id:
                cfg = await s.get(Config, o.reserved_config_id)
                if cfg and cfg.status == "reserved":
                    cfg.status = "in_stock"
            o.status = "rejected"
            await s.commit()
    await cb.answer("لغو شد")
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    row = await get_bot_row(bot)
    if not row:
        return
    if await get_setting(row.id, "menu_mode", "reply") == "inline":
        ikb = await main_inline_kb(row.id, row.owner_id, cb.from_user.id)
        if ikb:
            await cb.message.answer("🏠 منوی اصلی:", reply_markup=ikb)
    else:
        kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
        await cb.message.answer("🏠 منوی اصلی:", reply_markup=kb)


# ═══════════════════ دریافتِ رسید (هندلرهای سراسریِ StateFilter(None)) ═══════════════════

@card_purchase_router.message(StateFilter(None), F.photo)
async def card_receipt_upload(msg: Message, bot: Bot):
    """دریافت رسید عکسی کارت‌به‌کارت — هم سفارش‌های card معمولی و هم vpn_card (پنل خودکار)."""
    from tenant import get_bot_row, rial
    row = await get_bot_row(bot)
    if not row:
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        if not u:
            return
        o = await s.scalar(select(Order).where(
            Order.bot_id == row.id, Order.user_id == u.id,
            Order.status == "awaiting_receipt",
            Order.pay_method.in_(["card", "vpn_card", "mcfg_card"])
        ).order_by(Order.id.desc()))
        if not o:
            return
        o.receipt_file_id = msg.photo[-1].file_id
        o.receipt_file_unique_id = msg.photo[-1].file_unique_id
        o.receipt_text = None
        oid = o.id
        amount = float(o.amount)
        pay_method = o.pay_method
        gb, days, count = o.volume_gb, o.days, o.count
        await s.commit()

    confirm_cb = f"rc_confirm:{oid}"
    edit_cb    = f"rc_edit:{oid}"
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ارسال", callback_data=confirm_cb)],
        [InlineKeyboardButton(text="✏️ ویرایش رسید", callback_data=edit_cb)],
    ])
    if pay_method in ("vpn_card", "mcfg_card"):
        vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
        dur = "بی‌انقضا" if days == 0 else f"{days} روز"
        preview_caption = (
            f"📸 <b>پیش‌نمایش رسید</b>\n\n"
            f"سفارش: {vol}/{dur} × {count} عدد\n"
            f"مبلغ: {rial(amount)}\n\n"
            f"رسید درست است؟"
        )
    else:
        preview_caption = (
            f"📸 <b>پیش‌نمایش رسید</b>\n\n"
            f"سفارش: #{await order_no(Order, oid)}\n"
            f"مبلغ: {rial(amount)}\n\n"
            f"رسید درست است؟"
        )
    await msg.answer_photo(
        msg.photo[-1].file_id,
        caption=preview_caption,
        reply_markup=user_kb,
        parse_mode="HTML",
    )


@card_purchase_router.message(StateFilter(None), F.chat.type == "private",
                              F.text & ~F.text.startswith("/"))
async def card_receipt_upload_text(msg: Message, bot: Bot):
    """دریافت رسید متنی کارت‌به‌کارت برای سفارش‌های card/vpn_card در انتظار رسید.

    === PATCH_CARD_RECEIPT_PRIVATE_ONLY_V1 ===
    باگِ سیستمی: این هندلر هیچ فیلترِ نوعِ‌چتی نداشت، پس رویِ *هر* پیامِ
    متنی، حتی داخلِ گروه، هم مچ می‌شد. وقتی هیچ سفارشِ در‌انتظارِ‌رسیدی
    برایِ فرستنده پیدا نمی‌کرد (حالتِ عادیِ هر پیامِ گروهی)، فقط بی‌سروصدا
    return می‌کرد — بدونِ SkipHandler. چون aiogram این را «پردازش‌شده»
    حساب می‌کرد، هیچ‌وقت نوبت به روترهایِ بعدی (از جمله community_router،
    که منطقِ واقعیِ بن/اخطار/سکوت/فیلترِ کلمات آنجاست) نمی‌رسید — این علتِ
    ریشه‌ایِ گزارشِ «مدیریتِ گروه و کانال کلاً کار نمی‌کند» بود. رسیدِ
    کارت‌به‌کارت ذاتاً یک مفهومِ چتِ‌خصوصی است (مشتری همیشه در پیوی با
    ربات رسید می‌فرستد، نه در گروه)، پس محدودکردنِ فیلتر به private هم
    درست است و هم این باگ را کامل می‌بندد — نه فقط برایِ ربات‌هایِ
    community_mgmt، برایِ همهٔ انواعِ ربات.
    """
    from tenant import get_bot_row, rial
    row = await get_bot_row(bot)
    if not row:
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        if not u:
            return
        o = await s.scalar(select(Order).where(
            Order.bot_id == row.id, Order.user_id == u.id,
            Order.status == "awaiting_receipt",
            Order.pay_method.in_(["card", "vpn_card", "mcfg_card"])
        ).order_by(Order.id.desc()))
        if not o:
            return
        if not msg.text or not msg.text.strip() or not any(ch.isdigit() for ch in msg.text):
            await msg.answer("⚠️ متن رسید باید شامل عدد باشد (مثلاً مبلغ یا کد پیگیری). لطفاً رسید را دوباره ارسال کنید.")
            return
        o.receipt_file_id = None
        o.receipt_file_unique_id = None
        o.receipt_text = msg.text
        oid = o.id
        amount = float(o.amount)
        pay_method = o.pay_method
        gb, days, count = o.volume_gb, o.days, o.count
        await s.commit()

    confirm_cb = f"rc_confirm:{oid}"
    edit_cb    = f"rc_edit:{oid}"
    user_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ارسال", callback_data=confirm_cb)],
        [InlineKeyboardButton(text="✏️ ویرایش رسید", callback_data=edit_cb)],
    ])
    if pay_method in ("vpn_card", "mcfg_card"):
        vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
        dur = "بی‌انقضا" if days == 0 else f"{days} روز"
        # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
        # msg.text متنِ آزادِ خودِ کاربره؛ بدونِ escape، وجودِ < یا > توش این
        # پیامِ HTML-mode رو خراب می‌کنه و ارسالش به خودِ کاربر (پیش‌نمایشِ
        # رسید) شکست می‌خوره — یعنی کاربر حتی به مرحلهٔ «تأیید و ارسال» هم
        # نمی‌رسه و کلِ سفارش همون‌جا بی‌صدا گم می‌شه.
        preview_text = (
            f"📋 <b>پیش‌نمایش رسید متنی</b>\n\n"
            f"سفارش: {vol}/{dur} × {count} عدد\n"
            f"مبلغ: {rial(amount)}\n\n"
            f"<blockquote>{_html.escape(msg.text[:500])}</blockquote>\n\n"
            f"رسید درست است؟"
        )
    else:
        preview_text = (
            f"📋 <b>پیش‌نمایش رسید متنی</b>\n\n"
            f"سفارش: #{await order_no(Order, oid)}\n"
            f"مبلغ: {rial(amount)}\n\n"
            f"<blockquote>{_html.escape(msg.text[:500])}</blockquote>\n\n"
            f"رسید درست است؟"
        )
    await msg.answer(preview_text, reply_markup=user_kb, parse_mode="HTML")


@card_purchase_router.callback_query(F.data.startswith("rc_edit:"))
async def receipt_edit(cb: CallbackQuery, bot: Bot):
    """کاربر می‌خواد رسید رو عوض کنه — سفارش رو برمی‌گردونیم به awaiting_receipt."""
    oid = int(cb.data.split(":")[1])
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if not o or o.status != "awaiting_receipt":
            await cb.answer("سفارش یافت نشد یا قبلاً پردازش شده.", show_alert=True)
            return
        o.receipt_file_id = None
        await s.commit()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("📸 عکس رسید یا متن رسید جدید را ارسال کنید:")


@card_purchase_router.callback_query(F.data.startswith("rc_confirm:"))
async def receipt_confirm(cb: CallbackQuery, bot: Bot):
    """کاربر رسید رو تأیید کرد — ارسال به ادمین."""
    from tenant import get_bot_row, get_setting, rial, inline_back_kb, evaluate_receipt, _notify_receipt_admins, _admin_user_block  # === PATCH_RECEIPT_CHANNEL_V1 ===
    from tenant import order_source_lines  # === PATCH_ORDER_SOURCE_LINE_V1 ===
    oid = int(cb.data.split(":")[1])
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if not o or o.status != "awaiting_receipt":
            await cb.answer("این رسید قبلاً پردازش شده.", show_alert=True)
            return
        u = await s.get(BotUser, o.user_id)
        o.status = "awaiting_admin"
        amount     = float(o.amount)
        pay_method = o.pay_method
        file_id    = o.receipt_file_id
        file_unique_id = o.receipt_file_unique_id
        gb, days, count = o.volume_gb, o.days, o.count
        tracking_code = o.tracking_code
        # === PATCH_ORDER_SOURCE_LINE_V1 === پنل/بستهٔ دقیقِ این سفارش
        src_lines = await order_source_lines(o)
        await s.commit()
        if u:
            await s.refresh(u)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if pay_method == "vpn_card":
        vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
        dur = "بی‌انقضا" if days == 0 else f"{days} روز"
        user_text  = (
            f"✅ رسید ثبت شد.\n\n"
            f"سفارش: {vol}/{dur} × {count} عدد\n"
            f"مبلغ: {rial(amount)}\n\n"
            f"منتظر تأیید ادمین بمانید."
        )
        adm_caption = (
            f"💳 <b>رسید پرداخت</b>\n\n"
            f"{await _admin_user_block(u, cb.from_user.id, tracking_code, bot_id=row.id)}\n"
            f"📦 سرویس خریداری‌شده: {vol}/{dur} × {count} عدد\n"
            + (f"{src_lines}\n" if src_lines else "")
            + f"💵 مبلغ سفارش: {rial(amount)}\n"
            f"سفارش: #{await order_no(Order, oid)}"
        )
        adm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأیید و ساخت کانفیگ", callback_data=f"vcadm:ok:{oid}")],
            [InlineKeyboardButton(text="❌ رد پرداخت", callback_data=f"vcadm:no:{oid}")],
        ])
    else:
        user_text   = f"✅ رسید سفارش #{await order_no(Order, oid)} ثبت شد.\nمنتظر تایید ادمین بمانید."
        vol_txt = "♾️ نامحدود" if gb == 0 else f"{gb} گیگ"
        dur_txt = "♾️ بی‌انقضا" if not days else f"{days} روز"
        adm_caption = (
            f"💳 <b>رسید پرداخت</b>\n\n"
            f"{await _admin_user_block(u, cb.from_user.id, tracking_code, bot_id=row.id)}\n"
            f"📦 سرویس خریداری‌شده: {vol_txt} / {dur_txt} × {count} عدد\n"
            + (f"{src_lines}\n" if src_lines else "")
            + f"💵 مبلغ سفارش: {rial(amount)}\n"
            f"سفارش: #{await order_no(Order, oid)}"
        )
        adm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تایید پرداخت", callback_data=f"rcadm:ok:{oid}")],
            [InlineKeyboardButton(text="❌ رد پرداخت", callback_data=f"rcadm:no:{oid}")],
        ])

    await cb.message.answer(user_text, reply_markup=await inline_back_kb(row.id))
    async with Session() as s2:
        o2 = await s2.get(Order, oid)
        if o2 is not None and await _is_foreign(o2, bot):   # PATCH_TENANT_SCOPE_V1
            o2 = None
        receipt_text = getattr(o2, "receipt_text", None) if o2 else None
    if receipt_text and not file_id:
        adm_caption += f"\n\n📋 رسید متنی:\n<blockquote>{_html.escape(receipt_text[:500])}</blockquote>"  # PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1

    # ── تایید خودکار ──
    # === PATCH_AUTO_APPROVE_VPN_CARD_V1 ===
    # قبلاً فقط ("card", "mcfg_card") بود و vpn_card صراحتاً کنار گذاشته شده
    # بود — یعنی برایِ ربات‌هایی که سرویس را مستقیم از پنل می‌فروشند، تاییدِ
    # خودکار هرگز اجرا نمی‌شد و ادمین فکر می‌کرد تنظیمات کار نمی‌کند (تیکت #468).
    # === PATCH_AUTO_APPROVE_BLOCK_CRASH_SAFETY_V1 ===
    # قبلاً فقط دورِ خودِ توابعِ auto_approve_*_order یه try/except بود
    # (PATCH_AUTO_APPROVE_CRASH_SAFETY_V1)، ولی خودِ `evaluate_receipt()`
    # (فراخوانیِ AI) و `duplicate_receipt_reason()` بیرون از اون try بودن.
    # گزارشِ ادمین: «وقتی تاییدِ خودکار روشن بود اصلاً رسید برام نمی‌اومد؛
    # وقتی خاموشش کردم دوباره رسید می‌رسه» — یعنی یه اکسپشنِ دیگه (نه
    # لزوماً تویِ auto_approve_*_order) وسطِ همین بلوک می‌ترکید و کلِ تابع
    # قبل از رسیدن به `_notify_receipt_admins` در انتهای تابع کرش می‌کرد.
    # حالا کلِ بلوکِ «تاییدِ خودکار» (از evaluate_receipt تا duplicate_
    # receipt_reason) زیرِ یک try/except مشترک است: هر اکسپشنی هرجایِ این
    # بلوک رخ بدهد، فقط باعثِ افتادن به مسیرِ ادمینِ دستی می‌شود (رسید هیچ‌
    # وقت گم نمی‌شود)، نه کرشِ کاملِ تابع.
    if pay_method in ("card", "mcfg_card", "vpn_card") and await get_setting(row.id, "auto_approve_receipts", "0") == "1":
        try:
            from tenant.gateways.card_display import receipt_dest_values as _rdv   # PATCH_CARD_BOTH_V1
            card_number, sheba_number, account_number = await _rdv(row.id)
            async with Session() as s4:
                o4 = await s4.get(Order, oid)
                if o4 is not None and await _is_foreign(o4, bot):   # PATCH_TENANT_SCOPE_V1
                    o4 = None
                created_at = o4.created_at if o4 else None
                sender_card4 = o4.sender_card4 if o4 else None
                sender_id_type = o4.sender_id_type if o4 else "card"
            check = await evaluate_receipt(bot, receipt_text, file_id, amount, card_number, created_at,
                                           expected_sender_last4=sender_card4,
                                           expected_sender_id_type=sender_id_type,
                                           expected_sheba_number=sheba_number,
                                           expected_account_number=account_number,
                                           bot_id=row.id)
            if check.ok:
                done = False
                try:
                    if pay_method == "vpn_card":
                        # === PATCH_AUTO_APPROVE_VPN_CARD_V1 ===
                        from tenant.bots.vpn.vpn_user import auto_approve_vpn_card_order
                        done = await auto_approve_vpn_card_order(bot, row, oid, check, file_id, receipt_text,
                                                                 file_unique_id=file_unique_id)
                    else:
                        done = await _auto_approve_config_order(bot, row, oid, check, file_id, receipt_text,
                                                                file_unique_id=file_unique_id)
                except Exception:
                    logger.exception(
                        "receipt_confirm: تاییدِ خودکار برایِ سفارش #%s با خطای غیرمنتظره شکست خورد؛ "
                        "به ادمین ارجاع داده می‌شود.", oid)
                if done:
                    await cb.answer("تایید خودکار انجام شد ✅")
                    return
            from tenant.gateways.auto_receipt import duplicate_receipt_reason
            _dup_reason = await duplicate_receipt_reason(f"tenant:{row.id}", check, file_unique_id) if check.ok else None
            adm_caption += (
                f"\n\n🤖 تایید خودکار فعال است ولی این رسید به‌صورت خودکار تایید نشد.\n"
                f"دلیل: {_dup_reason or check.reason}"
            )
        except Exception:
            logger.exception(
                "receipt_confirm: بلوکِ تاییدِ خودکار برایِ سفارش #%s با خطای غیرمنتظره شکست خورد "
                "(قبل از رسیدن به تاییدِ دستی) — رسید همچنان به ادمین ارجاع داده می‌شود.", oid)
            adm_caption += (
                "\n\n🤖 تایید خودکار فعال است ولی بررسی با خطای فنی مواجه شد؛ "
                "این رسید نیاز به تاییدِ دستی دارد."
            )

    await _notify_receipt_admins(bot, row, adm_caption, adm_kb, file_id)  # === PATCH_RECEIPT_CHANNEL_V1 ===
    await cb.answer("رسید ارسال شد ✅")


async def _auto_approve_config_order(bot: Bot, row, oid: int, check, file_id: str | None,
                                     receipt_text: str | None,
                                     file_unique_id: str | None = None) -> bool:
    """تایید خودکارِ سفارشِ خریدِ کانفیگ (کارت‌به‌کارت) وقتی مبلغ/شماره‌کارت/
    تاریخِ رسید با سفارش مطابقت داشته باشد. کانفیگ را تخصیص و برای کاربر
    ارسال می‌کند و گزارشِ کامل را برای ادمین می‌فرستد تا در صورتِ فیک بودن
    لغو شود."""
    from tenant import rial, inline_back_kb, send_config_to_user, _notify_receipt_admins, _admin_user_block, _purchase_alert  # === PATCH_RECEIPT_CHANNEL_V1 ===
    from tenant.gateways.auto_receipt import (receipt_fingerprint, photo_fingerprint,
                                              claim_receipts, release_receipts)
    _dup_scope = f"tenant:{row.id}"
    _dup_fp = receipt_fingerprint(check)
    _photo_fp = photo_fingerprint(file_unique_id)
    _fps = [_photo_fp, _dup_fp]
    if not await claim_receipts(_dup_scope, _fps):
        return False
    _receipt_done = False   # PATCH_RECEIPT_CLAIM_ATOMIC_V1
    try:
        async with Session() as s:
            # === PATCH_WALLET_ROW_LOCK_V1 + PATCH_CONFIG_STOCK_ROW_LOCK_V1 ===
            o = await s.get(Order, oid, with_for_update=True)
            if not o or o.bot_id != row.id or o.status != "awaiting_admin":
                return False
            u = await s.get(BotUser, o.user_id, with_for_update=True)
            amount = float(o.amount)
            # === PATCH_STOCK_MANUAL_DELIVERY_V1 ===
            from tenant.bots.vpn import stock_delivery as _sd
            _sd_manual = await _sd.is_on(row.id)
            sd_queued_id = None
            if o.pay_method == "mcfg_card":
                cfg = (await s.get(Config, o.reserved_config_id, with_for_update=True)
                      if o.reserved_config_id else None)
                if not cfg or cfg.status != "reserved":
                    return False
                if _sd_manual:
                    cfg.status = "in_stock"          # رزرو آزاد؛ ادمین کانفیگِ تازه می‌فرستد
                    cfg.owner_user_id = None
                    assigned = []
                else:
                    cfg.status = "sold"
                    cfg.owner_user_id = u.id
                    cfg.custom_label = o.custom_label
                    assigned = [cfg]
                o.status = "paid"
            else:
                cfgs = (await s.scalars(
                    select(Config).where(
                        Config.bot_id == row.id, Config.status == "in_stock", Config.price == 0,
                        Config.volume_gb == o.volume_gb, Config.days == o.days
                    ).with_for_update().limit(o.count)
                )).all()
                if len(cfgs) < o.count:
                    return False
                o.status = "paid"
                assigned = []
                if not _sd_manual:
                    for cfg in cfgs[:o.count]:
                        cfg.status = "sold"
                        cfg.owner_user_id = u.id
                        assigned.append(cfg)
            if _sd_manual:
                _sd_obj = _sd.queue(s, bot_id=row.id, user_id=u.id, order_id=o.id, volume_gb=o.volume_gb,
                                    days=o.days, count=o.count, amount=amount, label=o.custom_label)
                await s.flush()
                sd_queued_id = _sd_obj.id
            await s.commit()
            assigned_ids = [c.id for c in assigned]
            tg_id = u.telegram_id
        _receipt_done = True
    finally:
        if not _receipt_done:
            await release_receipts(_dup_scope, _fps)

    from tenant.shared.extras import handle_referral_purchase
    await handle_referral_purchase(row.id, tg_id, bot, amount=amount)
    if sd_queued_id:        # PATCH_STOCK_MANUAL_DELIVERY_V1: بدونِ ارسالِ کانفیگ؛ ادمین دستی تحویل می‌دهد
        await _sd.announce(bot, sd_queued_id)
    else:
        async with Session() as s3:
            assigned_cfgs = (await s3.scalars(select(Config).where(Config.id.in_(assigned_ids)))).all()
            for cfg in assigned_cfgs:
                await send_config_to_user(bot, tg_id, cfg)
        try:
            await bot.send_message(tg_id, "✅ پرداخت شما به‌صورت خودکار تایید شد!",
                                   reply_markup=await inline_back_kb(row.id))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    detail_lines = [f"🔎 مبلغِ خوانده‌شده: {rial(check.amount)}"]
    if check.card_last4:
        detail_lines.append(f"💳 ۴ رقمِ آخرِ کارت روی رسید: <code>{check.card_last4}</code>")
    if check.date_jalali:
        detail_lines.append(f"📅 تاریخِ رسید: {check.date_jalali}")
    report = (
        f"🤖 <b>تایید خودکار رسید — خرید کانفیگ</b>\n\n"
        f"{await _admin_user_block(u, tg_id, bot_id=row.id)}\n"
        f"💵 مبلغ سفارش: {rial(amount)}\n"
        + "\n".join(detail_lines) + "\n"
        f"✅ نتیجهٔ بررسی: {check.reason}\n"
        f"سفارش: #{await order_no(Order, oid)}\n\n"
        f"✅ به‌صورت خودکار تایید و کانفیگ برای کاربر ارسال شد.\n"
        f"⚠️ اگر این رسید فیک/مشکوک است، با دکمهٔ زیر لغو کنید."
    )
    if receipt_text:
        report += f"\n\n📋 رسید متنی:\n<blockquote>{_html.escape(receipt_text[:500])}</blockquote>"  # PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 لغو (رسید فیک بود)", callback_data=f"rcadm_revoke:{oid}"),
    ]])
    await _notify_receipt_admins(bot, row, report, kb, file_id)  # === PATCH_RECEIPT_CHANNEL_V1 ===
    # === PATCH_PURCHASE_ALERT_CARD_COVERAGE_V1 ===
    # قبلاً «اطلاع خودکار خرید» (تاگلی که ادمین می‌تونه روشن/خاموش کنه) فقط
    # روی مسیرهای خریدِ کیف‌پولی و فروشگاه صدا زده می‌شد؛ کلِ مسیرِ خریدِ
    # کارت‌به‌کارت (چه تاییدِ دستی چه تاییدِ خودکارِ AI) این نوتیفیکیشنِ سبک رو
    # اصلاً نداشت — جدا از گزارشِ کاملِ AI بالا که همیشه (بدونِ توجه به این
    # تاگل) فرستاده می‌شه.
    try:
        detail_txt = (assigned_cfgs[0].code if assigned_cfgs else f"{len(assigned)} کانفیگ")
        # === PATCH_ORDER_BUTTON_ID_V1 ===
        _bname = None
        if getattr(o, "button_id", None):
            from models import MainButton
            async with Session() as _s:
                _mb = await _s.get(MainButton, o.button_id)
                _bname = _mb.label if _mb else None
        await _purchase_alert(bot, row, buyer=u, section="کارت‌به‌کارت (تاییدِ خودکار)",
                              detail=detail_txt, amount=amount, order_id=oid, button_name=_bname)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


# ═══════════════════ تاییدِ دستیِ ادمین (rcadm:*) ═══════════════════

# === PATCH_RCADM_HARD_LOCK ===
# قفلِ in-memory و همزمان (بدونِ هیچ await قبل از قفل‌شدن) برای دکمهٔ
# تاییدِ دستیِ ادمین روی رسیدِ خریدِ کانفیگ. چون این تراکنش پیش از رسیدن
# به with_for_update چند await دارد (get_bot_row، is_owner_or_admin) و
# قفلِ ردیفیِ دیتابیس (بسته به نوعِ دیتابیس، مثلاً SQLite) همیشه واقعاً
# مانع از پردازشِ هم‌زمان نمی‌شود، تپِ دومِ ادمین قبل از هر await همین‌جا
# رد می‌شود؛ کلید = شناسهٔ سفارش (oid)، چون تپِ دوم همان سفارش را صدا می‌زند.
_rcadm_decide_inflight: set[int] = set()


@card_purchase_router.callback_query(F.data.startswith("rcadm:"))
async def rcadm_decide(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, _rcadm_action, _rcadm_oid = cb.data.split(":")
    _rcadm_oid = int(_rcadm_oid)
    if _rcadm_oid in _rcadm_decide_inflight:
        # تپِ دوم (یا بیشتر) روی همین سفارش — همینجا و فوری رد می‌شود
        await cb.answer("در حالِ پردازش... لطفاً دوباره نزن.", show_alert=False)
        return
    _rcadm_decide_inflight.add(_rcadm_oid)
    try:
        await _rcadm_decide_impl(cb, bot, state)
    finally:
        _rcadm_decide_inflight.discard(_rcadm_oid)


async def _rcadm_decide_impl(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, send_config_to_user, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    _, action, oid = cb.data.split(":")
    oid = int(oid)
    async with Session() as s:
        o = await s.get(Order, oid, with_for_update=True)
        if not o or o.status != "awaiting_admin":
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        u = await s.get(BotUser, o.user_id, with_for_update=True)
        if action == "ok":
            # === PATCH_STOCK_MANUAL_DELIVERY_V1 ===
            from tenant.bots.vpn import stock_delivery as _sd
            _sd_manual = await _sd.is_on(row.id)
            sd_queued_id = None
            if o.pay_method == "mcfg_card":
                # === PATCH_CONFIG_STOCK_ROW_LOCK_V1 ===
                cfg = (await s.get(Config, o.reserved_config_id, with_for_update=True)
                      if o.reserved_config_id else None)
                if not cfg or cfg.status != "reserved":
                    await cb.answer("⚠️ کانفیگِ رزروشده دیگر معتبر نیست. تأیید لغو شد.", show_alert=True)
                    return
                if _sd_manual:
                    cfg.status = "in_stock"          # رزرو آزاد؛ ادمین کانفیگِ تازه می‌فرستد
                    cfg.owner_user_id = None
                    assigned = []
                else:
                    cfg.status = "sold"
                    cfg.owner_user_id = u.id
                    cfg.custom_label = o.custom_label
                    assigned = [cfg]
                o.status = "paid"
                if _sd_manual:
                    _sd_obj = _sd.queue(s, bot_id=row.id, user_id=u.id, order_id=o.id, volume_gb=o.volume_gb,
                                        days=o.days, count=o.count, amount=float(o.amount or 0), label=o.custom_label)
                    await s.flush()
                    sd_queued_id = _sd_obj.id
                await s.commit()
            else:
                cfgs = (await s.scalars(
                    select(Config).where(
                        Config.bot_id == row.id, Config.status == "in_stock", Config.price == 0,
                        Config.volume_gb == o.volume_gb, Config.days == o.days
                    ).with_for_update().limit(o.count)
                )).all()
                if len(cfgs) < o.count:
                    await cb.answer(
                        f"⚠️ موجودی کانفیگ کافی نیست! ({len(cfgs)} از {o.count} موجود). تأیید لغو شد.",
                        show_alert=True
                    )
                    return
                o.status = "paid"
                assigned = []
                if _sd_manual:
                    _sd_obj = _sd.queue(s, bot_id=row.id, user_id=u.id, order_id=o.id, volume_gb=o.volume_gb,
                                        days=o.days, count=o.count, amount=float(o.amount or 0), label=o.custom_label)
                    await s.flush()
                    sd_queued_id = _sd_obj.id
                else:
                    for cfg in cfgs[:o.count]:
                        cfg.status = "sold"
                        cfg.owner_user_id = u.id
                        assigned.append(cfg)
                await s.commit()
            from tenant.shared.extras import handle_referral_purchase
            # === PATCH_DELIVERY_GUARD_V1 === خطایِ پاداشِ دعوت نباید کلِ تأیید را بکُشد (دکمه‌ها می‌ماند و مشتری چیزی نمی‌گرفت)
            try:
                await handle_referral_purchase(row.id, u.telegram_id, bot, amount=float(o.amount or 0))
            except Exception:
                logger.exception("پاداشِ دعوت ناموفق بود (تأیید ادامه می‌یابد)")
            await cb.answer("تایید شد ✅")
            try:
                original = cb.message.caption or cb.message.text or ""
                done_text = original + f"\n\n✅ <b>تایید شد توسط ادمین</b> @{cb.from_user.username or cb.from_user.full_name}"
                if cb.message.caption is not None:
                    await cb.message.edit_caption(caption=done_text, reply_markup=None)
                else:
                    await cb.message.edit_text(done_text, reply_markup=None)
            except Exception:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            try:
                if sd_queued_id:        # PATCH_STOCK_MANUAL_DELIVERY_V1
                    await _sd.announce(bot, sd_queued_id)
                else:
                    for cfg in assigned:
                        await send_config_to_user(bot, u.telegram_id, cfg)
                    await bot.send_message(u.telegram_id, "✅ پرداخت شما تایید شد!",
                                           reply_markup=await inline_back_kb(row.id))
            except Exception as _e:
                # === PATCH_DELIVERY_GUARD_V1 === تحویل به مشتری شکست خورد؛ ادمین باید همان لحظه بفهمد
                logger.exception("تحویلِ سفارشِ تأییدشده به مشتری ناموفق بود (order=%s)", oid)
                from tenant.shared.delivery_guard import alert_admin_delivery_failed
                await alert_admin_delivery_failed(
                    bot, cb.from_user.id, f"سفارش #{await order_no(Order, oid)} ({o.volume_gb} گیگ / {o.days} روز)",
                    u, _e, cfgs=assigned)
            # === PATCH_PURCHASE_ALERT_CARD_COVERAGE_V1 ===
            try:
                from tenant import _purchase_alert
                detail_txt = (assigned[0].code if assigned else "کانفیگ")
                # === PATCH_ORDER_BUTTON_ID_V1 ===
                _bname = None
                if getattr(o, "button_id", None):
                    from models import MainButton
                    async with Session() as _s:
                        _mb = await _s.get(MainButton, o.button_id)
                        _bname = _mb.label if _mb else None
                await _purchase_alert(bot, row, buyer=u, section="کارت‌به‌کارت (تاییدِ دستیِ ادمین)",
                                      detail=detail_txt, amount=float(o.amount),
                                      order_id=o.id, tracking_code=o.tracking_code, count=o.count,
                                      button_name=_bname)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        else:
            await cb.answer()
            await state.set_state(CardPurchaseFSM.reject_reason)
            await state.update_data(reject_oid=oid, reject_msg_id=cb.message.message_id,
                                    reject_chat_id=cb.message.chat.id, reject_kind="rc")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⛔️ رد بدون دلیل", callback_data=f"rcadm_nodeny:{oid}")],
                [InlineKeyboardButton(text="❌ لغو", callback_data=f"rcadm_cancel:{oid}")],
            ])
            await cb.message.answer(
                "📝 <b>دلیل رد پرداخت را بنویسید</b> تا برای کاربر ارسال شود:\n"
                "(یا یکی از دکمه‌های زیر را انتخاب کنید)",
                reply_markup=kb)


@card_purchase_router.callback_query(F.data.startswith("rcadm_nodeny:"))
async def rcadm_nodeny(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """رد پرداخت بدون دلیل."""
    from tenant import get_bot_row, is_owner_or_admin, inline_back_kb
    await state.clear()
    oid = int(cb.data.split(":")[1])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if not o or o.status != "awaiting_admin":
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        u = await s.get(BotUser, o.user_id)
        if o.pay_method == "mcfg_card" and o.reserved_config_id:
            cfg = await s.get(Config, o.reserved_config_id)
            if cfg and cfg.status == "reserved":
                cfg.status = "in_stock"
        o.status = "rejected"
        await s.commit()
    await cb.answer("رد شد ❌")
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n❌ <b>رد شد توسط ادمین</b> @{cb.from_user.username or cb.from_user.full_name}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None)
        else:
            await cb.message.edit_text(done_text, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await bot.send_message(u.telegram_id, "❌ پرداخت شما تایید نشد. با پشتیبانی تماس بگیرید.",
                           reply_markup=await inline_back_kb(row.id))


@card_purchase_router.callback_query(F.data.startswith("rcadm_cancel:"))
async def rcadm_cancel(cb: CallbackQuery, state: FSMContext):
    """لغو عملیات رد رسید."""
    await state.clear()
    await cb.answer("لغو شد")
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@card_purchase_router.message(CardPurchaseFSM.reject_reason)
async def rcadm_reason_text(msg: Message, bot: Bot, state: FSMContext):
    """دریافت دلیل رد از ادمین و ارسال به کاربر."""
    from tenant import get_bot_row, inline_back_kb
    data = await state.get_data()
    if data.get("reject_kind") != "rc":
        return
    await state.clear()
    oid = data.get("reject_oid")
    if not oid:
        return
    row = await get_bot_row(bot)
    if not row:
        return
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if not o or o.status != "awaiting_admin":
            await msg.answer("⚠️ این سفارش قبلاً رسیدگی شده.")
            return
        u = await s.get(BotUser, o.user_id)
        if o.pay_method == "mcfg_card" and o.reserved_config_id:
            cfg = await s.get(Config, o.reserved_config_id)
            if cfg and cfg.status == "reserved":
                cfg.status = "in_stock"
        o.status = "rejected"
        await s.commit()
    reason = msg.text or msg.caption or "—"
    reject_msg_id = data.get("reject_msg_id")
    reject_chat_id = data.get("reject_chat_id")
    if reject_msg_id and reject_chat_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=reject_chat_id, message_id=reject_msg_id, reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await msg.answer(f"✅ رد شد و دلیل برای کاربر ارسال شد.\n\n❌ <b>دلیل:</b> {reason}")
    await bot.send_message(
        u.telegram_id,
        f"❌ <b>پرداخت شما تایید نشد.</b>\n\n"
        f"📋 دلیل: {reason}\n\n"
        "در صورت نیاز با پشتیبانی تماس بگیرید.",
        reply_markup=await inline_back_kb(row.id))


# === PATCH_REVOKE_CONFIRM_V1 === لغو برگشت‌ناپذیر است؛ قبل از اجرا تأیید می‌خواهد.
@card_purchase_router.callback_query(F.data.startswith("rcadm_revoke:"))
async def rcadm_revoke_ask(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    oid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ بله؛ سفارش لغو شود", callback_data=f"rcadm_revoke_ok:{oid}")],
        [InlineKeyboardButton(text="↩️ انصراف", callback_data=f"rcadm_revoke_no:{oid}")],
    ])
    await cb.answer("مطمئنی؟ سفارش لغو می‌شود و به کاربر اطلاع داده می‌شود.", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("[revoke] نمایشِ تأیید ممکن نشد")


@card_purchase_router.callback_query(F.data.startswith("rcadm_revoke_no:"))
async def rcadm_revoke_no(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 لغو (رسید فیک بود)", callback_data=f"rcadm_revoke:{oid}")]])
    await cb.answer("انصراف داده شد؛ سفارش سرِ جایش است.")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("[revoke] برگرداندنِ دکمه ممکن نشد")


@card_purchase_router.callback_query(F.data.startswith("rcadm_revoke_ok:"))
async def rcadm_revoke(cb: CallbackQuery, bot: Bot):
    """لغوِ یک سفارشِ خریدِ کانفیگ که پیش‌تر به‌صورت خودکار تایید شده بود.
    برای سفارش‌های mcfg_card کانفیگ به انبار برمی‌گردد؛ برای سفارش‌های
    عادی (انبارِ چندتایی) به دلیلِ نبودِ ردگیریِ دقیقِ کانفیگ‌های تخصیص‌یافته،
    فقط وضعیتِ سفارش تغییر می‌کند و باید ادمین انبار را دستی بررسی کند."""
    from tenant import get_bot_row, is_owner_or_admin, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    oid = int(cb.data.split(":")[1])
    async with Session() as s:
        o = await s.get(Order, oid)
        if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
            o = None
        if not o or o.status != "paid" or o.pay_method not in ("card", "mcfg_card"):
            await cb.answer("این سفارش قابلِ لغو نیست (شاید قبلاً لغو شده).", show_alert=True)
            return
        u = await s.get(BotUser, o.user_id)
        note = ""
        if o.pay_method == "mcfg_card" and o.reserved_config_id:
            cfg = await s.get(Config, o.reserved_config_id)
            if cfg and cfg.owner_user_id == u.id:
                cfg.status = "in_stock"
                cfg.owner_user_id = None
                note = "کانفیگ به انبار بازگردانده شد."
        else:
            note = "⚠️ این سفارش از نوعِ انبارِ چندتایی است؛ کانفیگِ تخصیص‌یافته باید دستی بررسی/باطل شود."
        o.status = "fake_reverted"
        await s.commit()
        tg_id = u.telegram_id
    admin_name = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n🚫 <b>لغو شد (رسید فیک) توسط</b> {admin_name}\n{note}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None)
        else:
            await cb.message.edit_text(done_text, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("لغو شد 🚫")
    try:
        await bot.send_message(
            tg_id,
            f"⚠️ سفارشِ #{await order_no(Order, oid)} به دلیلِ نامعتبر بودنِ رسید لغو شد.\n"
            "در صورتِ اعتراض با پشتیبانی تماس بگیرید.",
            reply_markup=await inline_back_kb(row.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
