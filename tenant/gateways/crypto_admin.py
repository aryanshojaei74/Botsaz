"""
crypto_admin.py — هندلرهای aiogram مربوط به درگاه ارز دیجیتال (NOWPayments):
  • تنظیمات ادمین (API Key، IPN Secret، نرخ تبدیل)
  • جریان شارژ کیف پول کاربر (WebApp داخل تلگرام برای وارد کردن مبلغ/ارز، یا
    در نبودِ HTTPS معتبر، روش قدیمیِ پرسش در چت)

از همان الگوی ماژول‌های مستقلِ دیگرِ پروژه پیروی می‌کند (نک:
tenant/swap_admin.py): Router و StatesGroup مستقلِ خودش را دارد و توابعِ
مشترکِ tenant/__init__.py را با import محلی (داخل خودِ تابع) می‌گیرد.

منطقِ خالصِ ارتباط با API نوپیمنت (create_invoice، validate_api_key،
get_rate_smart، verify_ipn_signature) در tenant/crypto_pay.py است.
صفحهٔ پرداختِ WebApp (create_draft/ChargeDraft) در tenant/payment_page.py است.
"""
from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)

from config import Session, WEBHOOK_BASE
from models import CryptoCharge

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

crypto_admin_router = Router()


class CryptoFSM(StatesGroup):
    currency = State()   # شارژ کاربر: انتخاب واحد (تومان/دلار) - فقط در نبودِ WebApp
    amount = State()       # شارژ کاربر: انتظارِ مبلغ
    api = State()           # تنظیم ادمین: API Key
    ipn = State()           # تنظیم ادمین: IPN Secret
    rate = State()          # تنظیم ادمین: نرخ تبدیل دلار→تومان


def _crypto_currency_kb(picked: str = "") -> InlineKeyboardMarkup:
    """کیبورد انتخاب واحد پرداخت: تومان یا دلار (فقط در فالبکِ بدون WebApp).

    === PATCH_CRYPTO_CURRENCY_TICK_V1 ===
    بعد از انتخاب، کنارِ همان گزینه یک ✅ می‌نشیند و رنگش سبز می‌شود تا
    کاربر بداند کدام واحد را زده. قبلاً هیچ نشانه‌ای نبود و کاربر موقعِ
    فرستادنِ مبلغ نمی‌دانست دارد تومان می‌نویسد یا دلار.
    """
    def _lbl(code: str, text: str) -> str:
        return f"✅ {text}" if picked == code else text

    def _st(code: str):
        return ButtonStyle.SUCCESS if picked == code else ButtonStyle.PRIMARY

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=_lbl("toman", "🇮🇷 تومان"),
                                 callback_data="wccy:toman", style=_st("toman")),
            InlineKeyboardButton(text=_lbl("usd", "🇺🇸 دلار (USD)"),
                                 callback_data="wccy:usd", style=_st("usd")),
        ],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="w:back", style=ButtonStyle.DANGER)],
    ])


# ═══════════════════════ شارژ کیف پول کاربر ═══════════════════════

async def wallet_crypto_text(msg: Message, bot: Bot, state: FSMContext):
    """نسخهٔ حالتِ کیبوردیِ ساده برای شروع شارژ ارز دیجیتال."""
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        return
    if row.plan == "free":
        await msg.answer(
            "🔒 شارژ با ارز دیجیتال در پلن پایه فعال نیست.\n"
            "برای استفاده باید به پلن رشد یا حرفه‌ای ارتقا بدهی.")
        return
    api_key = await get_setting(row.id, "nowpay_api_key", "")
    nowpay_enabled = await get_setting(row.id, "nowpay_enabled", "1")
    if not api_key:
        await msg.answer("درگاه ارز دیجیتال هنوز توسط مدیر ربات تنظیم نشده است.")
        return
    if nowpay_enabled != "1":
        await msg.answer("درگاه ارز دیجیتال در حال حاضر غیرفعال است.")
        return
    await state.set_state(CryptoFSM.currency)
    await msg.answer(
        "🪙 <b>پرداخت با ارز دیجیتال</b>\n\n"
        "مبلغ را به چه واحدی وارد می‌کنی؟",
        reply_markup=_crypto_currency_kb(),
        parse_mode="HTML"
    )


@crypto_admin_router.callback_query(F.data == "w:crypto")
async def w_crypto(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if row.plan == "free":
        await cb.answer(
            "🔒 شارژ با ارز دیجیتال در پلن پایه فعال نیست.\n"
            "برای استفاده باید به پلن رشد یا حرفه‌ای ارتقا بدهی.",
            show_alert=True)
        return
    api_key = await get_setting(row.id, "nowpay_api_key", "")
    nowpay_enabled = await get_setting(row.id, "nowpay_enabled", "1")
    if not api_key:
        await cb.answer("درگاه ارز دیجیتال هنوز توسط مدیر ربات تنظیم نشده است.", show_alert=True)
        return
    if nowpay_enabled != "1":
        await cb.answer("درگاه ارز دیجیتال در حال حاضر غیرفعال است.", show_alert=True)
        return

    # ── ساخت چرک‌نویس شارژ + باز کردن سایت واسط به‌صورت WebApp ──
    # مبلغ و واحد (تومان/دلار) و ارز، همه داخل خود صفحه انتخاب می‌شوند.
    from tenant.gateways.payment_page import create_draft
    # === PATCH_CUSTOM_DOMAIN_V1 ===
    # قبلاً همیشه از دامنهٔ سراسریِ WEBHOOK_BASE استفاده می‌شد؛ حالا اگر
    # این ربات دامنهٔ اختصاصیِ تاییدشده داشته باشد، لینکِ پرداخت زیرِ همان
    # دامنه ساخته می‌شود.
    from tenant.gateways.custom_domain import get_bot_webhook_base
    effective_base = await get_bot_webhook_base(row.id)
    await state.clear()
    await cb.answer()

    if not effective_base or not effective_base.startswith("https://"):
        # اگر دامنهٔ https نبود، به روش قدیمی (پرسش در چت) برگرد
        await state.set_state(CryptoFSM.currency)
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(
            "🪙 <b>پرداخت با ارز دیجیتال</b>\n\n"
            "مبلغ را به چه واحدی وارد می‌کنی؟",
            reply_markup=_crypto_currency_kb(), parse_mode="HTML")
        return

    tc = await create_draft(row.id, cb.from_user.id)
    pay_url = f"{effective_base}/pay/{tc}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت با ارز دیجیتال",
                              web_app=WebAppInfo(url=pay_url))],
        [InlineKeyboardButton(text="🔍 بررسی پرداخت", callback_data=f"tnp:check:{tc}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت به کیف پول", callback_data="w:back", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🪙 <b>شارژ با ارز دیجیتال</b>\n\n"
        f"📮 کد پیگیری: <code>{tc}</code>\n\n"
        "روی دکمهٔ «پرداخت با ارز دیجیتال» بزن تا صفحهٔ پرداخت داخل تلگرام باز شود. "
        "آن‌جا مبلغ را وارد می‌کنی و ارز پرداخت را انتخاب می‌کنی.\n"
        "بعد از تأیید شبکه، کیف پولت خودکار شارژ می‌شود.\n\n"
        "اگر پرداخت کردی ولی چند دقیقه بعد شارژ نشد، روی «🔍 بررسی پرداخت» بزن.",
        reply_markup=kb, parse_mode="HTML")


@crypto_admin_router.callback_query(F.data.startswith("tnp:check:"))
async def tnp_check_payment(cb: CallbackQuery, bot: Bot):
    """دکمهٔ «بررسی پرداخت» زیر فاکتورِ ارز دیجیتالِ کاربر در ربات مستأجر:
    وقتی IPN نرسیده، خودِ کاربر با زدن این دکمه وضعیت را مستقیم از NOWPayments
    استعلام می‌گیرد و در صورتِ finished/confirmed بودن، کیف پولش بلافاصله شارژ
    می‌شود (بدون نیاز به ادمین یا صبر برای حلقهٔ پس‌زمینه)."""
    tc = cb.data.split(":", 2)[2]
    await cb.answer("⏳ در حال بررسی پرداخت...")

    from sqlalchemy import select as _select
    from tenant.gateways.payment_page import ChargeDraft, ensure_table, _active_check_tenant

    await ensure_table()
    async with Session() as s:
        draft = await s.scalar(_select(ChargeDraft).where(ChargeDraft.tracking_code == tc))

    if not draft:
        await cb.message.answer("❌ این فاکتور پیدا نشد؛ با پشتیبانی تماس بگیر.")
        return
    if draft.user_telegram_id != cb.from_user.id:
        await cb.answer("این فاکتور مربوط به تو نیست.", show_alert=True)
        return
    if draft.status == "paid":
        await cb.message.answer("✅ این فاکتور قبلاً تأیید شده و کیف پولت شارژ شده است.")
        return

    try:
        paid_now = await _active_check_tenant(draft)
    except Exception:
        paid_now = False

    if paid_now:
        async with Session() as s:
            d = await s.scalar(_select(ChargeDraft).where(ChargeDraft.tracking_code == tc))
            if d and d.status != "paid":
                d.status = "paid"
                await s.commit()
        await cb.message.answer("✅ پرداخت تأیید شد.")
    else:
        await cb.message.answer(
            "⏳ هنوز پرداخت تو روی شبکه تأیید یا ثبت نشده.\n"
            "اگر همین الان واریز کردی، چند دقیقه صبر کن (بسته به شبکه ممکن است طول بکشد) "
            "و دوباره روی «🔍 بررسی پرداخت» بزن.\n"
            "اگر مطمئنی پرداخت انجام شده و بعد از چند بار تلاش هم شارژ نشد، "
            "شناسهٔ تراکنش (Transaction Hash) را برای پشتیبانی بفرست.")


@crypto_admin_router.callback_query(CryptoFSM.currency, F.data.startswith("wccy:"))
async def w_crypto_pick_currency(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        await cb.answer()
        return
    currency = cb.data.split(":", 1)[1]   # "toman" یا "usd"
    await state.update_data(crypto_currency=currency)
    await state.set_state(CryptoFSM.amount)
    # === PATCH_CRYPTO_CURRENCY_TICK_V1 ===
    # اول همان پیامِ انتخاب را با تیکِ گزینهٔ انتخاب‌شده به‌روز می‌کنیم تا
    # بازخوردِ فوری بگیرد، بعد سراغِ پرسشِ مبلغ می‌رویم.
    await cb.answer("🇮🇷 تومان انتخاب شد" if currency == "toman"
                    else "🇺🇸 دلار انتخاب شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=_crypto_currency_kb(currency))
    except Exception:
        logger.debug("به‌روزرسانیِ تیکِ واحدِ پرداخت ممکن نشد (بی‌اهمیت)")
    if currency == "toman":
        await cb.message.answer(
            "✅ واحد: <b>🇮🇷 تومان</b>\n\n"
            "🪙 مبلغ را به <b>تومان</b> بفرست (فقط عدد، حداقل ۱۰۰٬۰۰۰):",
            reply_markup=await smart_cancel_kb(bot),
            parse_mode="HTML"
        )
    else:
        await cb.message.answer(
            "✅ واحد: <b>🇺🇸 دلار (USD)</b>\n\n"
            "🪙 مبلغ را به <b>دلار (USD)</b> بفرست (فقط عدد، حداقل ۱):\n"
            "مثال: <code>5</code> یا <code>10.5</code>",
            reply_markup=await smart_cancel_kb(bot),
            parse_mode="HTML"
        )


@crypto_admin_router.message(CryptoFSM.amount, F.text)
async def w_crypto_amount(msg: Message, state: FSMContext, bot: Bot):
    import uuid as _uuid
    from tenant.gateways import crypto_pay
    from tenant import get_bot_row, get_setting, fa_to_int, rial, inline_back_kb

    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return

    data = await state.get_data()
    currency = data.get("crypto_currency", "usd")
    await state.clear()

    api_key = await get_setting(row.id, "nowpay_api_key", "")
    if not api_key:
        await msg.answer("❌ درگاه ارز دیجیتال توسط مدیر ربات تنظیم نشده.")
        return

    if currency == "toman":
        # ── ورود به تومان: تبدیل به دلار با نرخ دستی ادمین ──
        amount_toman = fa_to_int(msg.text)
        if not amount_toman:
            await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست.")
            return
        # === PATCH_WALLET_LIMITS_V1 === (پیش‌فرضِ حداقل همان ۱۰۰٬۰۰۰ قبلی)
        from tenant.gateways import wallet_limits as _wl
        if not await _wl.guard(msg, row.id, "crypto", amount_toman):
            return

        manual_rate_str = await get_setting(row.id, "toman_per_usd", "0")
        try:
            rate_v = float(manual_rate_str)
        except ValueError:
            rate_v = 0

        # اگر ادمین نرخ دستی نگذاشته، نرخ آنلاین بازار آزاد (بایننس P2P و...) گرفته می‌شود
        if rate_v <= 0:
            from tenant.gateways.crypto_pay import get_rate_smart
            brsapi_key = await get_setting(row.id, "brsapi_key", "")
            rate_v = await get_rate_smart(brsapi_key)

        if rate_v <= 0:
            await msg.answer(
                "❌ دریافت نرخ دلار ناموفق بود.\n"
                "لطفاً کمی بعد دوباره امتحان کن یا با پشتیبانی تماس بگیر."
            )
            return

        usd = round(amount_toman / rate_v, 2)
        if usd < 0.5:
            usd = 0.5
        amount_toman_final = amount_toman
        desc = f"شارژ کیف پول {amount_toman:,} تومان"
        invoice_label = f"مبلغ: <b>{rial(amount_toman_final)}</b>\nمعادل دلاری: <b>~{usd}$</b>"
    else:
        # ── ورود به دلار: مستقیم به NOWPayments ──
        raw = msg.text.strip().replace(",", "").replace("٫", ".")
        # تبدیل اعداد فارسی
        persian_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
        raw = raw.translate(persian_map)
        try:
            usd = float(raw)
        except ValueError:
            await msg.answer("مبلغ نامعتبر است. یک عدد دلاری بفرست (مثل ۵ یا ۱۰٫۵).")
            return
        if usd < 0.5:
            await msg.answer("حداقل مبلغ ۰.۵ دلار است.")
            return
        usd = round(usd, 2)

        # مبلغ تومانی بر اساس نرخ دستی ادمین، یا نرخ آنلاین اگر تنظیم نشده
        manual_rate_str = await get_setting(row.id, "toman_per_usd", "0")
        try:
            rate_v = float(manual_rate_str)
        except ValueError:
            rate_v = 0
        if rate_v <= 0:
            from tenant.gateways.crypto_pay import get_rate_smart
            brsapi_key = await get_setting(row.id, "brsapi_key", "")
            rate_v = await get_rate_smart(brsapi_key)
        amount_toman_final = int(usd * rate_v) if rate_v > 0 else 0
        desc = f"شارژ کیف پول {usd}$"
        if amount_toman_final > 0:
            invoice_label = f"مبلغ: <b>{usd}$</b>\nمعادل تومانی: <b>{rial(amount_toman_final)}</b>"
        else:
            invoice_label = f"مبلغ: <b>{usd}$</b>"

    # === PATCH_KYC_GATE_ALL_PATHS_V1 === سقفِ احرازِ هویت
    # اینجا (بعد از هر دو شاخهٔ تومانی/دلاری و قبل از ساختِ فاکتور) گذاشته
    # شد تا کاربر پول ندهد و بعد رد شود. در شاخهٔ دلاری معادلِ تومانی مبنا
    # است؛ اگر نرخ در دسترس نبود صفر می‌شود و گیت طبیعتاً رد نمی‌کند.
    _kyc_amt = amount_toman if currency == "toman" else amount_toman_final
    from tenant.shared.kyc import gate as _kyc_gate
    _why = await _kyc_gate(row.id, msg.from_user.id, _kyc_amt)
    if _why:
        await msg.answer(_why)
        return

    if not WEBHOOK_BASE:
        await msg.answer("آدرس وب‌هوک تنظیم نشده؛ به پشتیبانی اطلاع بده.")
        return

    wait_msg = await msg.answer("⏳ در حال ساخت فاکتور پرداخت...")

    order_id = f"bs-{row.id}-{msg.from_user.id}-{_uuid.uuid4().hex[:8]}"
    ipn_url = f"{WEBHOOK_BASE}/nowpay/ipn"
    inv = await crypto_pay.create_invoice(
        api_key, price_amount=usd, order_id=order_id,
        order_description=desc,
        ipn_callback_url=ipn_url, price_currency="usd")

    try:
        await wait_msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if not inv or not inv.get("invoice_url"):
        await msg.answer(
            "❌ ساخت لینک پرداخت ناموفق بود.\n"
            "ممکن است API Key اشتباه باشد یا NOWPayments در دسترس نباشد.\n"
            "بعداً دوباره تلاش کن.",
            reply_markup=await inline_back_kb(row.id)
        )
        return

    async with Session() as s:
        s.add(CryptoCharge(bot_id=row.id, user_telegram_id=msg.from_user.id,
                           order_id=order_id, amount_toman=amount_toman_final, usd_amount=usd,
                           status="waiting"))
        await s.commit()

    # دکمهٔ پرداخت به‌صورت مینی‌اپ (WebApp) داخل تلگرام باز می‌شود.
    _iurl = inv["invoice_url"]
    if _iurl.startswith("https://"):
        _pay_btn = InlineKeyboardButton(text="🪙 پرداخت با ارز دیجیتال",
                                        web_app=WebAppInfo(url=_iurl))
    else:
        _pay_btn = InlineKeyboardButton(text="🪙 پرداخت با ارز دیجیتال", url=_iurl)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_pay_btn],
        [InlineKeyboardButton(text="⬅️ بازگشت به کیف پول", callback_data="w:back", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"🪙 <b>فاکتور پرداخت با ارز دیجیتال</b>\n\n"
        f"{invoice_label}\n\n"
        "روی دکمه زیر بزن تا صفحهٔ پرداخت داخل تلگرام باز شود و پرداخت را تکمیل کن.\n"
        "بعد از تأیید شبکه، کیف پولت خودکار شارژ می‌شود.",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ═══════════════════════ تنظیمات ادمین ═══════════════════════

@crypto_admin_router.callback_query(F.data == "set:crypto")
async def set_crypto_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if row.plan == "free":
        await cb.answer("این بخش مخصوص پلن رشد/حرفه‌ای است.", show_alert=True)
        return
    ipn_url = f"{WEBHOOK_BASE}/nowpay/ipn"
    cur_key = await get_setting(row.id, "nowpay_api_key", "")
    cur_enabled = await get_setting(row.id, "nowpay_enabled", "1")
    # اگر قبلاً تنظیم شده، وضعیت و toggle نشان بده
    if cur_key:
        status_text = "✅ فعال" if cur_enabled == "1" else "❌ غیرفعال (در کیف پول: «به‌زودی»)"
        toggle_text = "⛔️ غیرفعال‌کردن درگاه" if cur_enabled == "1" else "✅ فعال‌کردن درگاه"
        # === PATCH_CUSTOM_DOMAIN_V1 ===
        # درخواستِ ادمین: دامنهٔ اختصاصی رو دقیقاً همین‌جا (کنارِ تنظیماتِ
        # خودِ درگاهِ ارز دیجیتال) نشون بده، نه یه گزینهٔ گمشده تو تنظیماتِ
        # عمومی — چون تنها جایی که واقعاً ازش استفاده می‌شه همینجاست.
        from tenant.gateways.custom_domain import get_bot_webhook_base
        from models import BotCustomDomain
        from sqlalchemy import select as _select2
        async with Session() as _s:
            _d = await _s.scalar(_select2(BotCustomDomain).where(BotCustomDomain.bot_id == row.id))
        if _d and _d.status == "verified":
            domain_line = f"🌐 دامنهٔ لینکِ پرداخت: <code>{_d.domain}</code> (اختصاصی)"
        else:
            eff = await get_bot_webhook_base(row.id)
            domain_line = (f"🌐 دامنهٔ لینکِ پرداخت: <code>{eff or '—'}</code> "
                          f"(مشترکِ پلتفرم — می‌تونی دامنهٔ خودت رو وصل کنی)")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="set:crypto_toggle", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="🔄 تغییر API Key / تنظیمات مجدد", callback_data="set:crypto_reconfig", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="🌐 دامنهٔ اختصاصیِ لینکِ پرداخت", callback_data="cdom:menu")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
        ])
        await cb.message.answer(
            f"🪙 <b>درگاه ارز دیجیتال (NOWPayments)</b>\n\n"
            f"وضعیت: {status_text}\n"
            f"{domain_line}\n\n"
            f"📌 IPN URL:\n<code>{ipn_url}</code>",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await cb.answer()
        return
    await state.set_state(CryptoFSM.api)
    await cb.message.answer(
        "🪙 <b>تنظیم درگاه ارز دیجیتال (NOWPayments)</b>\n\n"
        "۱/۳ — <b>API Key</b> اکانت NOWPayments خود را بفرست.\n"
        "(از پنل NOWPayments → Settings → API keys)\n\n"
        "⚠️ مهم: در پنل NOWPayments، بخش IPN را فعال کن و این آدرس را ثبت کن:\n"
        f"<code>{ipn_url}</code>",
        reply_markup=await smart_cancel_kb(bot),
        parse_mode="HTML"
    )
    await cb.answer()


@crypto_admin_router.callback_query(F.data == "set:crypto_toggle")
async def set_crypto_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "nowpay_enabled", "1")
    new_val = "0" if cur == "1" else "1"
    await set_setting(row.id, "nowpay_enabled", new_val)
    status = "✅ فعال شد" if new_val == "1" else "❌ غیرفعال شد"
    await cb.answer(f"درگاه ارز دیجیتال {status}", show_alert=True)
    await set_crypto_start(cb, bot, state)


@crypto_admin_router.callback_query(F.data == "set:crypto_reconfig")
async def set_crypto_reconfig(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    ipn_url = f"{WEBHOOK_BASE}/nowpay/ipn"
    await state.set_state(CryptoFSM.api)
    await cb.message.answer(
        "🪙 <b>تنظیم مجدد درگاه ارز دیجیتال (NOWPayments)</b>\n\n"
        "۱/۳ — <b>API Key</b> جدید را بفرست.\n\n"
        "⚠️ مهم: آدرس IPN را در پنل NOWPayments ثبت کن:\n"
        f"<code>{ipn_url}</code>",
        reply_markup=await smart_cancel_kb(bot),
        parse_mode="HTML"
    )
    await cb.answer()


@crypto_admin_router.message(CryptoFSM.api, F.text)
async def set_crypto_api(msg: Message, state: FSMContext, bot: Bot):
    from tenant.gateways import crypto_pay
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    api_key = msg.text.strip()

    # اعتبارسنجی واقعی API Key
    wait = await msg.answer("⏳ در حال بررسی API Key...")
    ok, reason = await crypto_pay.validate_api_key(api_key)
    try:
        await wait.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if not ok:
        await msg.answer(
            f"❌ <b>API Key نامعتبر است</b>\n\n{reason}\n\n"
            "دوباره API Key را بفرست یا /cancel را بزن.",
            parse_mode="HTML",
            reply_markup=await smart_cancel_kb(bot)
        )
        return

    await state.update_data(nowpay_api_key=api_key)
    await state.set_state(CryptoFSM.ipn)
    await msg.answer(
        "✅ API Key تایید شد!\n\n"
        "۲/۳ — <b>IPN Secret Key</b> را بفرست.\n"
        "(از پنل NOWPayments → Settings → IPN → IPN Secret Key)\n\n"
        "اگه IPN Secret نداری، یکی در پنل NOWPayments بساز.",
        reply_markup=await smart_cancel_kb(bot),
        parse_mode="HTML"
    )


@crypto_admin_router.message(CryptoFSM.ipn, F.text)
async def set_crypto_ipn(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    ipn_secret = msg.text.strip()
    await state.update_data(nowpay_ipn_secret=ipn_secret)
    await state.set_state(CryptoFSM.rate)
    await msg.answer(
        "✅ IPN Secret ذخیره شد!\n\n"
        "۳/۳ — <b>نرخ تبدیل دلار به تومان</b> را وارد کن (فقط عدد):\n"
        "این نرخ برای تبدیل مبلغ تومانی کاربر به دلار استفاده می‌شود.\n"
        "مثال: <code>95000</code>",
        reply_markup=await smart_cancel_kb(bot),
        parse_mode="HTML"
    )


@crypto_admin_router.message(CryptoFSM.rate, F.text)
async def set_crypto_rate(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    raw = msg.text.strip().replace(",", "").replace("٬", "")
    persian_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    raw = raw.translate(persian_map)
    try:
        rate = float(raw)
    except ValueError:
        await msg.answer("عدد نامعتبر است. نرخ را به صورت عدد بفرست (مثل ۹۵۰۰۰).")
        return
    if rate < 1000:
        await msg.answer("نرخ خیلی کم است. حداقل ۱۰۰۰ تومان باشد.")
        return

    data = await state.get_data()
    await state.clear()

    await set_setting(row.id, "nowpay_api_key", data.get("nowpay_api_key", ""))
    await set_setting(row.id, "nowpay_ipn_secret", data.get("nowpay_ipn_secret", ""))
    await set_setting(row.id, "toman_per_usd", str(int(rate)))
    await set_setting(row.id, "nowpay_enabled", "1")

    ipn_url = f"{WEBHOOK_BASE}/nowpay/ipn"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        "✅ <b>درگاه ارز دیجیتال (NOWPayments) فعال شد!</b>\n\n"
        f"💱 نرخ تبدیل: <b>{int(rate):,} تومان/دلار</b>\n\n"
        "📌 کاربران می‌توانند مبلغ را به تومان یا دلار وارد کنند.\n\n"
        "⚠️ مطمئن شو در پنل NOWPayments این آدرس IPN ثبت شده:\n"
        f"<code>{ipn_url}</code>",
        reply_markup=back_kb,
        parse_mode="HTML"
    )
