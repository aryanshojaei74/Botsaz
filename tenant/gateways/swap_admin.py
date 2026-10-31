"""
swap_admin.py — هندلرهای aiogram مربوط به درگاه «سواپ‌ولت» (SwapPay):
  • تنظیمات ادمین (کلید API، آدرس‌های واریز، حداقل/حداکثر، ارزهای فعال)
  • جریان شارژ کیف پول کاربر (انتخاب ارز، دریافت مبلغ، فاکتور express-withdraw)
  • pollerِ پس‌زمینه که هر ۴۵ ثانیه واریزهای جدید را خودکار تشخیص و شارژ می‌کند

از همان الگوی ماژول‌های مستقلِ دیگرِ پروژه پیروی می‌کند (نک: tenant/shop.py،
tenant/card_wallet.py): Router و StatesGroup مستقلِ خودش را دارد و توابعِ
مشترکِ tenant/__init__.py را با import محلی (داخل خودِ تابع) می‌گیرد تا از
circular import جلوگیری شود.

منطقِ خالصِ دیتابیس/دفترداریِ شارژها (SwapCharge، mark_paid با آپدیتِ اتمیک)
در tenant/swap_charges.py است؛ این فایل لایهٔ رابطِ تلگرام است.
"""
import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser, WalletTxn

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

swap_admin_router = Router()

SWAP_NETWORK_HINT = {"USDT": "TRON یا BSC", "TON": "TON", "TRX": "TRON"}


class SwapFSM(StatesGroup):
    coin_pick = State()    # شارژ کاربر: انتخاب ارز
    amount = State()        # شارژ کاربر: انتظارِ مبلغ به تومان
    min = State()            # تنظیم ادمین: حداقل مبلغ واریز
    max = State()            # تنظیم ادمین: حداکثر مبلغ واریز
    edit_field = State()     # تنظیم ادمین: ویرایش فیلد خاص (API/آدرس)


# ═══════════════════════ شارژ کیف پول کاربر ═══════════════════════

@swap_admin_router.callback_query(F.data == "w:swap")
async def w_swap(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if row.plan == "free":
        await cb.answer("🔒 شارژ با سواپ‌ولت در پلن پایه فعال نیست.", show_alert=True)
        return
    api_key = await get_setting(row.id, "swap_api_key", "")
    addr_map = {
        "USDT": await get_setting(row.id, "swap_addr_usdt", ""),
        "TON":  await get_setting(row.id, "swap_addr_ton", ""),
        "TRX":  await get_setting(row.id, "swap_addr_trx", ""),
    }
    if not api_key or not any(addr_map.values()):
        await cb.answer("درگاه سواپ‌ولت هنوز توسط مدیر ربات تنظیم نشده است.", show_alert=True)
        return

    # خواندن ارزهای فعال از تنظیمات ادمین (فقط آنهایی که آدرس دارند)
    enabled_coins_str = await get_setting(row.id, "swap_enabled_coins", "USDT,TON,TRX")
    enabled_coins = [c.strip().upper() for c in enabled_coins_str.split(",") if c.strip() and addr_map.get(c.strip().upper())]
    if not enabled_coins:
        enabled_coins = [c for c, a in addr_map.items() if a]

    coin_labels = {"USDT": "💵 تتر (USDT)", "TON": "💎 تون (TON)", "TRX": "⚡ ترون (TRX)"}
    btns = []
    for coin in enabled_coins:
        label = coin_labels.get(coin, coin)
        btns.append([InlineKeyboardButton(text=label, callback_data=f"wswcoin:{coin}", style=ButtonStyle.PRIMARY)])
    btns.append([InlineKeyboardButton(text="⬅️ بازگشت به کیف پول", callback_data="w:back", style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=btns)

    await state.set_state(SwapFSM.coin_pick)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("🟦 <b>شارژ با سواپ‌ولت</b>\n\nبا کدام ارز می‌خواهی پرداخت کنی؟",
                            reply_markup=kb, parse_mode="HTML")


@swap_admin_router.callback_query(SwapFSM.coin_pick, F.data.startswith("wswcoin:"))
async def w_swap_coin_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    coin = cb.data.split(":")[1].upper()
    await state.update_data(swap_selected_coin=coin)
    await state.set_state(SwapFSM.amount)
    await cb.answer()

    # خواندن حداقل/حداکثر از تنظیمات
    swap_min = int(await get_setting(row.id, "swap_min_toman", "10000") or "10000")
    swap_max_str = await get_setting(row.id, "swap_max_toman", "0")
    swap_max = int(swap_max_str or "0")

    min_txt = f"{swap_min:,}"
    max_txt = f" (حداکثر {swap_max:,})" if swap_max > 0 else ""
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"🟦 مبلغ شارژ را به <b>تومان</b> بفرست (فقط عدد، حداقل {min_txt}){max_txt}:",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML"
    )


@swap_admin_router.message(SwapFSM.amount, F.text)
async def w_swap_amount(msg: Message, state: FSMContext, bot: Bot):
    import swappay
    from tenant.gateways import swap_charges
    from decimal import Decimal
    from tenant import get_bot_row, get_setting, fa_to_int, rial

    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    amount = fa_to_int(msg.text)

    # خواندن حداقل/حداکثر از تنظیمات ادمین
    swap_min = int(await get_setting(row.id, "swap_min_toman", "10000") or "10000")
    swap_max_str = await get_setting(row.id, "swap_max_toman", "0")
    swap_max = int(swap_max_str or "0")

    if not amount or amount < swap_min:
        await msg.answer(f"مبلغ نامعتبر است. حداقل {swap_min:,} تومان بفرست.")
        return
    if swap_max > 0 and amount > swap_max:
        await msg.answer(f"مبلغ وارد‌شده بیشتر از حداکثر مجاز ({swap_max:,} تومان) است.")
        return

    # === PATCH_KYC_GATE_ALL_PATHS_V1 === سقفِ احرازِ هویت
    from tenant.shared.kyc import gate as _kyc_gate
    _why = await _kyc_gate(row.id, msg.from_user.id, amount)
    if _why:
        await msg.answer(_why)
        return

    # خواندن ارز انتخاب‌شده توسط کاربر
    data = await state.get_data()
    coin = (data.get("swap_selected_coin") or await get_setting(row.id, "swap_coin", "USDT") or "USDT").upper()
    await state.clear()

    api_key = await get_setting(row.id, "swap_api_key", "")
    # آدرس متناظر با ارز انتخاب‌شده توسط کاربر
    _coin_addr_key = {"USDT": "swap_addr_usdt", "TON": "swap_addr_ton", "TRX": "swap_addr_trx"}
    address = await get_setting(row.id, _coin_addr_key.get(coin, "swap_addr_usdt"), "")
    _coin_to_network = {"USDT": "TRON", "TON": "TON", "TRX": "TRON"}
    network = _coin_to_network.get(coin, coin)
    if not api_key or not address:
        await msg.answer("درگاه سواپ‌ولت درست تنظیم نشده. به پشتیبانی اطلاع بده.")
        return

    client = swappay.SwapPayClient(api_key)
    # نرخ زنده: قیمت هر واحد توکن بر حسب IRT (ریال)
    try:
        prices = await client.get_prices()
        price_irt = Decimal(str(prices.get(f"{coin}/IRT", "0")))
    except swappay.SwapPayError as e:
        await msg.answer(f"❌ ارتباط با سواپ‌ولت ناموفق بود: {e}")
        return
    if price_irt <= 0:
        await msg.answer("❌ دریافت نرخ لحظه‌ای ناموفق بود. بعداً دوباره تلاش کن.")
        return

    # تومان → ریال → مقدار توکن (IRT در سواپ‌ولت = ریال، amount کاربر = تومان)
    base_amount = Decimal(amount) / price_irt
    expected = await swap_charges.make_unique_amount(row.id, coin, base_amount)

    # cursor شروع: بزرگ‌ترین id فعلی تا فقط واریزهای بعد از این لحظه بررسی شوند
    try:
        recent = await client.list_transactions(limit=1)
        last_seen = int(recent[0]["id"]) if recent else 0
    except swappay.SwapPayError:
        last_seen = 0

    charge_id = await swap_charges.create_charge(
        row.id, msg.from_user.id, amount, coin, network, expected, last_seen)

    # ساخت شماره فاکتور یکتا
    import hashlib, urllib.parse
    _inv_raw = f"SWAP-{row.id}-{charge_id}-{msg.from_user.id}"
    invoice_number = hashlib.sha256(_inv_raw.encode()).hexdigest().upper()[:26]

    net_txt = network or SWAP_NETWORK_HINT.get(coin, "")
    coin_emoji = {"USDT": "💵", "TON": "💎", "TRX": "⚡"}.get(coin, "🪙")
    coin_label = {"USDT": "تتر (USDT)", "TON": "تون (TON)", "TRX": "ترون (TRX)"}.get(coin, coin)

    # لینک express-withdraw سواپ‌ولت (WebApp - داخل تلگرام باز می‌شه)
    # network باید همیشه باشه تا سواپ‌ولت بتونه نرخ رو نمایش بده
    _net_map = {"USDT": "TRON", "TON": "TON", "TRX": "TRON"}
    _express_net = _net_map.get(coin, coin)
    express_url = (
        f"https://swapwallet.app/express-withdraw"
        f"?amount={urllib.parse.quote(str(expected))}&coin={coin}&network={_express_net}&address={urllib.parse.quote(address)}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 پرداخت سریع با سواپ‌ولت", url=express_url, style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"wsx:cancel:{charge_id}", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"✅ <b>فاکتور با موفقیت ایجاد شد</b>\n\n"
        f"<b>#</b> شماره فاکتور:\n"
        f"<code>{invoice_number}</code>\n\n"
        f"<b>💳</b> مبلغ : <b>{expected} {coin_emoji} {coin_label}</b> - بدون احتساب کارمزد شبکه\n\n"
        f"لطفا مبلغ <b>{expected}</b> را به آدرس زیر واریز کنید.\n"
        f"{coin_emoji} آدرس کیف پول:\n"
        f"<code>{address}</code>\n\n"
        f"🔷 شبکه: <b>{net_txt}</b>\n\n"
        f"🚫 توجه: فیلد Memo را خالی بگذارید.\n\n"
        f"⚠️ این آدرس فقط تا <b>120 دقیقه</b> آینده معتبر است. ⏳\n"
        f"💡 اگر مبلغ پرداختی در صفحه سواپ‌ولت صفر نشان داد، نگران نباشید — مقدار رمزارز درست است و پرداخت انجام می‌شود.\n\n"
        f"✅ پس از پرداخت موفق، کیف پول شما به مقدار <b>{rial(amount)}</b> به طور "
        f"<b>خودکار</b> شارژ میگردد.",
        reply_markup=kb,
        parse_mode="HTML"
    )


@swap_admin_router.callback_query(F.data.startswith("wsx:cancel:"))
async def w_swap_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant.gateways import swap_charges
    from tenant import get_bot_row, get_or_create_user, show_wallet
    cid = int(cb.data.split(":")[2])
    c = await swap_charges.get_charge(cid)
    if c and c.status == "waiting":
        async with Session() as s:
            cc = await s.get(swap_charges.SwapCharge, cid)
            cc.status = "canceled"
            await s.commit()
    await state.clear()
    row = await get_bot_row(bot)
    if row:
        async with Session() as s:
            u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name,
                                         cb.from_user.username or "")
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await show_wallet(cb.message, row.id, u)
    await cb.answer("لغو شد.")


@swap_admin_router.callback_query(F.data.startswith("wsx:check:"))
async def w_swap_check(cb: CallbackQuery, bot: Bot):
    import swappay
    from tenant.gateways import swap_charges
    from decimal import Decimal
    from tenant import get_bot_row, rial, get_setting

    cid = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    c = await swap_charges.get_charge(cid)
    if not row or not c or c.bot_id != row.id:
        await cb.answer("درخواست یافت نشد.", show_alert=True)
        return
    if c.status == "paid":
        await cb.answer("این شارژ قبلاً تایید و اعمال شده است.", show_alert=True)
        return
    if c.status != "waiting":
        await cb.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
        return

    api_key = await get_setting(row.id, "swap_api_key", "")
    client = swappay.SwapPayClient(api_key)
    try:
        incoming = await client.detect_incoming(after_id=int(c.last_seen_id or 0),
                                                 tokens=(c.coin,))
    except swappay.SwapPayError as e:
        await cb.answer(f"خطا در بررسی: {e}", show_alert=True)
        return

    want = Decimal(c.expected_amount)
    match = None
    for tx in incoming:
        detail = tx.get("cryptoDeposit") or tx.get("transfer") or {}
        amt = swappay.Money.parse(detail.get("amount"))
        if not amt or amt.unit != c.coin:
            continue
        if amt.number != want:
            continue
        if await swap_charges.is_tx_used(row.id, int(tx["id"])):
            continue
        match = tx
        break

    if not match:
        await cb.answer("هنوز واریزی با این مقدار دیده نشد. اگر همین الان فرستادی، چند دقیقه بعد دوباره بزن.",
                        show_alert=True)
        return

    # ارزش واقعی واریز به تومان = userCurrencyValue (ریال) ÷ ۱۰
    detail = match.get("cryptoDeposit") or match.get("transfer") or {}
    irt = swappay.Money.parse(detail.get("userCurrencyValue"))
    credited = int(irt.number) if irt else int(c.amount_toman)
    if credited <= 0:
        credited = int(c.amount_toman)

    ok = await swap_charges.mark_paid(cid, int(match["id"]), credited)
    if not ok:
        await cb.answer("این واریز قبلاً پردازش شده است.", show_alert=True)
        return

    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == c.user_telegram_id).with_for_update())
        if u is None:
            u = BotUser(bot_id=row.id, telegram_id=c.user_telegram_id)
            s.add(u)
            await s.flush()
        u.balance = float(u.balance) + credited
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=credited, kind="swap_charge",
                        note=f"شارژ با سواپ‌ولت ({c.expected_amount} {c.coin})"))
        _kyc_uid = u.id
        await s.commit()
        new_bal = u.balance
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    from tenant.shared.kyc import after_charge as _ac
    await _ac(row.id, _kyc_uid)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    from tenant.gateways.wallet_receipt import build_wallet_charge_message
    wtext, wkb = build_wallet_charge_message(credited, new_bal, "swap")
    await cb.message.answer(wtext, reply_markup=wkb)
    await cb.answer("شارژ شد ✅")
    # اطلاع به ادمین (در صورت روشن بودن تنظیم)
    await _notify_admin_crypto_charge(bot, row.id, c.user_telegram_id,
                                      credited, c.coin, new_bal)


async def wallet_swap_text(msg: Message, bot: Bot, state: FSMContext):
    """نسخهٔ حالتِ کیبوردیِ ساده (بدون دکمهٔ اینلاین) برای شروع شارژ سواپ‌ولت —
    از منوی اصلی وقتی ربات در حالت کیبورد است صدا زده می‌شود."""
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        return
    if row.plan == "free":
        await msg.answer("🔒 شارژ با سواپ‌ولت در پلن پایه فعال نیست.")
        return
    api_key = await get_setting(row.id, "swap_api_key", "")
    addr_map = {
        "USDT": await get_setting(row.id, "swap_addr_usdt", ""),
        "TON":  await get_setting(row.id, "swap_addr_ton", ""),
        "TRX":  await get_setting(row.id, "swap_addr_trx", ""),
    }
    if not api_key or not any(addr_map.values()):
        await msg.answer("درگاه سواپ‌ولت هنوز توسط مدیر ربات تنظیم نشده است.")
        return
    enabled_coins_str = await get_setting(row.id, "swap_enabled_coins", "USDT,TON,TRX")
    enabled_coins = [c.strip().upper() for c in enabled_coins_str.split(",")
                     if c.strip() and addr_map.get(c.strip().upper())]
    if not enabled_coins:
        enabled_coins = [c for c, a in addr_map.items() if a]
    coin_labels = {"USDT": "💵 تتر (USDT)", "TON": "💎 تون (TON)", "TRX": "⚡ ترون (TRX)"}
    btns = []
    for coin in enabled_coins:
        label = coin_labels.get(coin, coin)
        btns.append([InlineKeyboardButton(text=label, callback_data=f"wswcoin:{coin}", style=ButtonStyle.PRIMARY)])
    btns.append([InlineKeyboardButton(text="⬅️ بازگشت به کیف پول", callback_data="w:back",
                                      style=ButtonStyle.DANGER)])
    await state.set_state(SwapFSM.coin_pick)
    await msg.answer("🟦 <b>شارژ با سواپ‌ولت</b>\n\nبا کدام ارز می‌خواهی پرداخت کنی؟",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")


# ═══════════════════════ poller پس‌زمینه (تایید خودکار) ═══════════════════════

async def _notify_admin_crypto_charge(bot_or_token, bot_id: int, user_tg: int,
                                      credited: int, coin: str, new_bal=None):
    """اگر تنظیمِ «اطلاع از شارژ کریپتو» روشن باشد، به ادمین/مالکِ ربات خبر می‌دهد.

    bot_or_token می‌تواند یک شیء Bot یا توکن رشته‌ای باشد (برای poller پس‌زمینه).
    تنظیم در Setting با کلید swap_notify_admin ذخیره می‌شود (پیش‌فرض روشن).
    """
    from tenant import get_setting
    try:
        notify = await get_setting(bot_id, "swap_notify_admin", "1")
        if notify == "0":
            return
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow or not brow.owner_id:
            return
        # نام کاربر (در صورت وجود)
        uname = ""
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == user_tg))
            if u and getattr(u, "full_name", None):
                uname = f"\n👤 کاربر: {u.full_name}"
        from tenant import rial
        bal_line = f"\n💰 موجودی جدید کاربر: {rial(new_bal)}" if new_bal is not None else ""
        text = (
            "🟦 <b>شارژ کیف پول با ارز دیجیتال</b>\n\n"
            f"✅ یک کاربر کیف پولش را شارژ کرد و تایید شد.\n"
            f"💵 مبلغ: <b>{rial(credited)}</b>\n"
            f"🪙 ارز: <b>{coin}</b>\n"
            f"🆔 آیدی عددی: <code>{user_tg}</code>{uname}{bal_line}"
        )
        # ارسال با شیء Bot یا ساخت موقت از توکن
        if isinstance(bot_or_token, str):
            from aiogram import Bot as _Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            tb = _Bot(token=bot_or_token,
                      default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await tb.send_message(brow.owner_id, text)
            finally:
                try:
                    await tb.session.close()
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        else:
            await bot_or_token.send_message(brow.owner_id, text, parse_mode="HTML")
    except Exception as e:
        print("notify admin crypto charge error:", e)


async def _swap_credit_wallet(bot_id: int, user_tg: int, credited: int, note: str):
    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == user_tg).with_for_update())
        if u is None:
            u = BotUser(bot_id=bot_id, telegram_id=user_tg)
            s.add(u)
            await s.flush()
        u.balance = float(u.balance) + credited
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credited,
                        kind="swap_charge", note=note))
        _kyc_uid = u.id
        _bal_out = u.balance
        await s.commit()
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    from tenant.shared.kyc import after_charge as _ac
    await _ac(bot_id, _kyc_uid)
    return _bal_out


async def swap_poll_once(max_age_minutes: int = 120):
    """یک‌بار همهٔ درخواست‌های در انتظار سواپ‌ولت را بررسی و در صورت واریز شارژ می‌کند.
    این تابع را از اسکجولر اصلی هر ۳۰–۶۰ ثانیه صدا بزن (یا از swap_poll_loop استفاده کن)."""
    import swappay
    from tenant.gateways import swap_charges
    from decimal import Decimal
    from tenant import get_setting, rial, inline_back_kb

    try:
        await swap_charges.expire_older_than(max_age_minutes)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    waiting = await swap_charges.list_waiting(max_age_minutes=max_age_minutes)
    if not waiting:
        return 0

    by_bot: dict[int, list] = {}
    for c in waiting:
        by_bot.setdefault(c["bot_id"], []).append(c)

    credited_count = 0
    for bot_id, charges in by_bot.items():
        if bot_id <= 0:
            continue  # شارژهای پلتفرم (bot_id=0) را poller پلتفرم رسیدگی می‌کند
        api_key = await get_setting(bot_id, "swap_api_key", "")
        if not api_key:
            continue
        coins = tuple({c["coin"] for c in charges})
        after = min(int(c["last_seen_id"] or 0) for c in charges)
        client = swappay.SwapPayClient(api_key)
        try:
            incoming = await client.detect_incoming(after_id=after, tokens=coins)
        except swappay.SwapPayError:
            continue
        if not incoming:
            continue

        # توکن ربات برای اطلاع‌رسانی به کاربر
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        token = brow.token if brow else None
        notify_bot = Bot(token=token) if token else None
        try:
            for c in charges:
                want = Decimal(c["expected_amount"])
                for tx in incoming:
                    detail = tx.get("cryptoDeposit") or tx.get("transfer") or {}
                    amt = swappay.Money.parse(detail.get("amount"))
                    if not amt or amt.unit != c["coin"] or amt.number != want:
                        continue
                    if await swap_charges.is_tx_used(bot_id, int(tx["id"])):
                        continue
                    irt = swappay.Money.parse(detail.get("userCurrencyValue"))
                    credited = int(irt.number) if irt else int(c["amount_toman"])
                    if credited <= 0:
                        credited = int(c["amount_toman"])
                    ok = await swap_charges.mark_paid(c["id"], int(tx["id"]), credited)
                    if not ok:
                        break
                    new_bal = await _swap_credit_wallet(
                        bot_id, c["user_telegram_id"], credited,
                        f"شارژ با سواپ‌ولت ({c['expected_amount']} {c['coin']})")
                    credited_count += 1
                    if notify_bot:
                        try:
                            await notify_bot.send_message(
                                c["user_telegram_id"],
                                f"✅ پرداخت سواپ‌ولت شما دریافت شد و کیف پولت "
                                f"<b>{rial(credited)}</b> شارژ شد.\nموجودی جدید: <b>{rial(new_bal)}</b>",
                                parse_mode="HTML",
                                reply_markup=await inline_back_kb(bot_id))
                        except Exception:
                            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                        # اطلاع به ادمین (در صورت روشن بودن تنظیم)
                        try:
                            await _notify_admin_crypto_charge(
                                notify_bot, bot_id, c["user_telegram_id"],
                                credited, c["coin"], new_bal)
                        except Exception:
                            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                    break
        finally:
            if notify_bot:
                try:
                    await notify_bot.session.close()
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return credited_count


async def swap_poll_loop(interval: int = 45):
    """حلقهٔ همیشگی poller. از entrypoint اصلی با asyncio.create_task(swap_poll_loop()) اجرا کن."""
    while True:
        try:
            await swap_poll_once()
        except Exception as e:
            print("swap_poll_loop error:", e)
        await asyncio.sleep(interval)


# ═══════════════════════ تنظیمات ادمین ═══════════════════════

async def _swap_admin_menu(target, bot_id: int, *, answer=False):
    """نمایش منوی مدیریت درگاه سواپ‌ولت."""
    from tenant import get_setting, edit_or_replace
    api_key = await get_setting(bot_id, "swap_api_key", "")
    addr_usdt = await get_setting(bot_id, "swap_addr_usdt", "")
    addr_ton  = await get_setting(bot_id, "swap_addr_ton", "")
    addr_trx  = await get_setting(bot_id, "swap_addr_trx", "")
    min_v = int(await get_setting(bot_id, "swap_min_toman", "10000") or "10000")
    max_v = int(await get_setting(bot_id, "swap_max_toman", "0") or "0")
    coins = await get_setting(bot_id, "swap_enabled_coins", "USDT,TON,TRX")
    notify_on = await get_setting(bot_id, "swap_notify_admin", "1") != "0"

    def _fmt(addr): return f"<code>{addr[:10]}…</code>" if addr else "❌ تنظیم‌نشده"

    txt = (
        "🟦 <b>مدیریت درگاه سواپ‌ولت</b>\n\n"
        f"🔑 کلید API: {'✅ ثبت‌شده' if api_key else '❌ ثبت‌نشده'}\n"
        f"💵 آدرس USDT: {_fmt(addr_usdt)}\n"
        f"💎 آدرس TON:  {_fmt(addr_ton)}\n"
        f"⚡ آدرس TRX:  {_fmt(addr_trx)}\n"
        f"💰 حداقل: {min_v:,} | حداکثر: {max_v:,} {'بدون محدودیت' if max_v == 0 else f'{max_v:,} تومان'}\n"
        f"🪙 ارزهای فعال: {coins}\n"
        f"🔔 اطلاع از شارژ کریپتو: {'✅ روشن' if notify_on else '❌ خاموش'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 ویرایش کلید API", callback_data="swadm:edit:api", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💵 آدرس USDT", callback_data="swadm:edit:usdt", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="💎 آدرس TON", callback_data="swadm:edit:ton", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="⚡ آدرس TRX", callback_data="swadm:edit:trx", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💰 حداقل/حداکثر", callback_data="swadm:edit:limits", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🪙 ارزهای فعال", callback_data="swadm:edit:coins", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(
            text=f"🔔 اطلاع از شارژ کریپتو: {'✅ روشن' if notify_on else '❌ خاموش'}",
            callback_data="swadm:notify", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🗑 حذف تمام تنظیمات سواپ", callback_data="swadm:delete",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    if answer:
        await target.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    else:
        await edit_or_replace(target.message, txt, reply_markup=kb, parse_mode="HTML")


@swap_admin_router.callback_query(F.data == "set:swap")
async def set_swap_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if row.plan == "free":
        await cb.answer("این بخش مخصوص پلن رشد/حرفه‌ای است.", show_alert=True)
        return
    await cb.answer()
    await _swap_admin_menu(cb, row.id, answer=True)


@swap_admin_router.callback_query(F.data == "swadm:notify")
async def swadm_notify_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش کردن اطلاع‌رسانی به ادمین هنگام شارژ کیف پول با ارز دیجیتال."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "swap_notify_admin", "1")
    new_val = "0" if cur != "0" else "1"
    await set_setting(row.id, "swap_notify_admin", new_val)
    await cb.answer("🔔 روشن شد" if new_val == "1" else "🔕 خاموش شد")
    await _swap_admin_menu(cb, row.id)


@swap_admin_router.callback_query(F.data.startswith("swadm:edit:"))
async def swap_edit_dispatch(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    field = cb.data.split(":")[2]
    await cb.answer()

    if field == "api":
        await state.update_data(swap_edit_field="api", swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await state.set_state(SwapFSM.edit_field)
        cur = await get_setting(row.id, "swap_api_key", "")
        await edit_or_replace(cb.message,
            f"🔑 <b>ویرایش کلید API سواپ‌ولت</b>\n"
            f"فعلی: {'ثبت‌شده ✅' if cur else 'ندارد'}\n\n"
            "کلید جدید را بفرست (apikey-...):",
            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")

    elif field == "usdt":
        await state.update_data(swap_edit_field="usdt", swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await state.set_state(SwapFSM.edit_field)
        cur = await get_setting(row.id, "swap_addr_usdt", "")
        await edit_or_replace(cb.message,
            f"💵 <b>آدرس واریز USDT (شبکه TRON/TRC20)</b>\n"
            f"فعلی: {cur or 'ندارد'}\n\n"
            "آدرس جدید را بفرست (برای حذف عدد 0 بفرست):",
            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")

    elif field == "ton":
        await state.update_data(swap_edit_field="ton", swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await state.set_state(SwapFSM.edit_field)
        cur = await get_setting(row.id, "swap_addr_ton", "")
        await edit_or_replace(cb.message,
            f"💎 <b>آدرس واریز TON</b>\n"
            f"فعلی: {cur or 'ندارد'}\n\n"
            "آدرس جدید را بفرست (برای حذف عدد 0 بفرست):",
            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")

    elif field == "trx":
        await state.update_data(swap_edit_field="trx", swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await state.set_state(SwapFSM.edit_field)
        cur = await get_setting(row.id, "swap_addr_trx", "")
        await edit_or_replace(cb.message,
            f"⚡ <b>آدرس واریز TRX (شبکه TRON)</b>\n"
            f"فعلی: {cur or 'ندارد'}\n\n"
            "آدرس جدید را بفرست (برای حذف عدد 0 بفرست):",
            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")

    elif field == "limits":
        await state.update_data(swap_edit_field="limits_min", swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await state.set_state(SwapFSM.min)
        cur_min = int(await get_setting(row.id, "swap_min_toman", "10000") or "10000")
        await edit_or_replace(cb.message,
            f"💰 <b>حداقل مبلغ واریز</b> (فعلی: {cur_min:,} تومان)\n"
            "مقدار جدید را به تومان بفرست:",
            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")

    elif field == "coins":
        cur_coins_str = await get_setting(row.id, "swap_enabled_coins", "USDT,TON,TRX")
        selected = set(cur_coins_str.split(","))
        await state.update_data(swap_enabled_coins_set=list(selected), swap_edit_field="coins",
                                  swap_menu_chat_id=cb.message.chat.id,
                                  swap_menu_msg_id=cb.message.message_id)
        await edit_or_replace(cb.message,
            "🪙 ارزهایی که کاربر می‌تواند انتخاب کند:",
            reply_markup=_swap_coins_admin_kb(selected), parse_mode="HTML")


@swap_admin_router.message(SwapFSM.edit_field, F.text)
async def swap_edit_field_handler(msg: Message, state: FSMContext, bot: Bot):
    import swappay
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        await msg.answer("⛔️ این تنظیم فقط برایِ مالکِ ربات است.")  # PATCH_SWAP_AUTH_FALLBACK_V1: قبلاً بی‌صدا رد می‌شد
        return
    data = await state.get_data()
    field = data.get("swap_edit_field", "")
    menu_chat_id = data.get("swap_menu_chat_id")
    menu_msg_id = data.get("swap_menu_msg_id")
    val = msg.text.strip()
    await state.clear()

    note = None
    if field == "api":
        # === PATCH_SWAP_AUTH_FALLBACK_V1 === علتِ دقیق نشان داده می‌شود (نه فقط «نامعتبر»)
        ok, why = await swappay.SwapPayClient(val.replace("Bearer ", "").strip().strip("\"'")).check_auth_detail()
        val = swappay.clean_key(val)  # PATCH_SWAP_CLEAN_KEY_V1
        if not ok:
            note = f"❌ کلید API ذخیره نشد.\n{why}"
        else:
            await set_setting(row.id, "swap_api_key", val)
            note = "✅ کلید API ذخیره شد."

    elif field == "usdt":
        if val == "0":
            await set_setting(row.id, "swap_addr_usdt", "")
            note = "✅ آدرس USDT حذف شد."
        elif len(val) < 10:
            note = "❌ آدرس نامعتبر است."
        else:
            await set_setting(row.id, "swap_addr_usdt", val)
            note = "✅ آدرس USDT ذخیره شد."

    elif field == "ton":
        if val == "0":
            await set_setting(row.id, "swap_addr_ton", "")
            note = "✅ آدرس TON حذف شد."
        elif len(val) < 10:
            note = "❌ آدرس نامعتبر است."
        else:
            await set_setting(row.id, "swap_addr_ton", val)
            note = "✅ آدرس TON ذخیره شد."

    elif field == "trx":
        if val == "0":
            await set_setting(row.id, "swap_addr_trx", "")
            note = "✅ آدرس TRX حذف شد."
        elif len(val) < 10:
            note = "❌ آدرس نامعتبر است."
        else:
            await set_setting(row.id, "swap_addr_trx", val)
            note = "✅ آدرس TRX ذخیره شد."

    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    await _swap_admin_menu_msg(bot, row.id, chat_id=menu_chat_id, msg_id=menu_msg_id, note=note)


async def _swap_admin_menu_msg(bot: Bot, bot_id: int, *, chat_id=None, msg_id=None, note: str | None = None):
    """نمایش/به‌روزرسانی منوی سواپ. اگر chat_id/msg_id داده شود همان پیام ادیت می‌شود."""
    from tenant import get_setting
    api_key = await get_setting(bot_id, "swap_api_key", "")
    addr_usdt = await get_setting(bot_id, "swap_addr_usdt", "")
    addr_ton  = await get_setting(bot_id, "swap_addr_ton", "")
    addr_trx  = await get_setting(bot_id, "swap_addr_trx", "")
    min_v = int(await get_setting(bot_id, "swap_min_toman", "10000") or "10000")
    max_v = int(await get_setting(bot_id, "swap_max_toman", "0") or "0")
    coins = await get_setting(bot_id, "swap_enabled_coins", "USDT,TON,TRX")
    notify_on = await get_setting(bot_id, "swap_notify_admin", "1") != "0"

    def _fmt(addr): return f"<code>{addr[:10]}…</code>" if addr else "❌ تنظیم‌نشده"

    txt = (
        ((note + "\n\n") if note else "") +
        "🟦 <b>مدیریت درگاه سواپ‌ولت</b>\n\n"
        f"🔑 کلید API: {'✅ ثبت‌شده' if api_key else '❌ ثبت‌نشده'}\n"
        f"💵 آدرس USDT: {_fmt(addr_usdt)}\n"
        f"💎 آدرس TON:  {_fmt(addr_ton)}\n"
        f"⚡ آدرس TRX:  {_fmt(addr_trx)}\n"
        f"💰 حداقل: {min_v:,} | حداکثر: {max_v:,} {'بدون محدودیت' if max_v == 0 else f'{max_v:,} تومان'}\n"
        f"🪙 ارزهای فعال: {coins}\n"
        f"🔔 اطلاع از شارژ کریپتو: {'✅ روشن' if notify_on else '❌ خاموش'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 ویرایش کلید API", callback_data="swadm:edit:api", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💵 آدرس USDT", callback_data="swadm:edit:usdt", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="💎 آدرس TON", callback_data="swadm:edit:ton", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="⚡ آدرس TRX", callback_data="swadm:edit:trx", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💰 حداقل/حداکثر", callback_data="swadm:edit:limits", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🪙 ارزهای فعال", callback_data="swadm:edit:coins", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(
            text=f"🔔 اطلاع از شارژ کریپتو: {'✅ روشن' if notify_on else '❌ خاموش'}",
            callback_data="swadm:notify", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🗑 حذف تمام تنظیمات سواپ", callback_data="swadm:delete",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])

    if chat_id and msg_id:
        try:
            await bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id,
                                         reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")
            return

    # نباید برسیم اینجا در فلوهای نرمال؛ صرفاً fallback ایمن
    raise RuntimeError("_swap_admin_menu_msg requires chat_id/msg_id")


# ─── حداقل/حداکثر ───
@swap_admin_router.message(SwapFSM.min, F.text)
async def set_swap_min(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, get_setting, smart_cancel_kb, fa_to_int
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if not val or val < 1000:
        await msg.answer("مقدار نامعتبر است. حداقل ۱۰۰۰ تومان باشد:")
        return
    await set_setting(row.id, "swap_min_toman", str(val))
    await state.set_state(SwapFSM.max)
    data = await state.get_data()
    menu_chat_id = data.get("swap_menu_chat_id")
    menu_msg_id = data.get("swap_menu_msg_id")
    cur_max = int(await get_setting(row.id, "swap_max_toman", "0") or "0")
    txt = (
        f"✅ حداقل {val:,} تومان ذخیره شد.\n\n"
        f"💰 <b>حداکثر مبلغ واریز</b> (فعلی: {cur_max:,} تومان)\n"
        "برای بدون محدودیت عدد <b>0</b> بفرست:"
    )
    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if menu_chat_id and menu_msg_id:
        try:
            await bot.edit_message_text(txt, chat_id=menu_chat_id, message_id=menu_msg_id,
                                         reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await msg.answer(txt, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@swap_admin_router.message(SwapFSM.max, F.text)
async def set_swap_max(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting, fa_to_int
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        await state.clear()
        return
    val_str = (msg.text or "").strip()
    val = fa_to_int(val_str) if val_str != "0" else 0
    if val is None:
        val = 0
    await set_setting(row.id, "swap_max_toman", str(val))
    data = await state.get_data()
    menu_chat_id = data.get("swap_menu_chat_id")
    menu_msg_id = data.get("swap_menu_msg_id")
    await state.clear()
    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    note = f"✅ حداکثر {'بدون محدودیت' if val == 0 else f'{val:,} تومان'} ذخیره شد."
    await _swap_admin_menu_msg(bot, row.id, chat_id=menu_chat_id, msg_id=menu_msg_id, note=note)


# ─── ارزهای فعال ───
def _swap_coins_admin_kb(selected: set) -> InlineKeyboardMarkup:
    coins = [("USDT", "💵 تتر (USDT)"), ("TON", "💎 تون (TON)"), ("TRX", "⚡ ترون (TRX)")]
    btns = []
    for coin, label in coins:
        check = "✅ " if coin in selected else "⬜ "
        btns.append([InlineKeyboardButton(text=check + label, callback_data=f"swcoinstog:{coin}", style=ButtonStyle.PRIMARY)])
    btns.append([InlineKeyboardButton(text="✔️ ذخیره", callback_data="swcoins:done",
                                      style=ButtonStyle.SUCCESS)])
    btns.append([InlineKeyboardButton(text="❌ لغو", callback_data="swcoins:cancel",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=btns)


@swap_admin_router.callback_query(F.data == "swcoins:cancel")
async def swap_coins_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:
        await cb.answer()
        return
    await state.clear()
    await _swap_admin_menu_msg(bot, row.id, chat_id=cb.message.chat.id,
                                msg_id=cb.message.message_id)
    await cb.answer()


@swap_admin_router.callback_query(F.data.startswith("swcoinstog:"))
async def swap_coins_toggle(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:
        await cb.answer()
        return
    coin = cb.data.split(":")[1]
    data = await state.get_data()
    selected = set(data.get("swap_enabled_coins_set", ["USDT", "TON", "TRX"]))
    if coin in selected:
        selected.discard(coin)
    else:
        selected.add(coin)
    await state.update_data(swap_enabled_coins_set=list(selected))
    await cb.message.edit_reply_markup(reply_markup=_swap_coins_admin_kb(selected))
    await cb.answer()


@swap_admin_router.callback_query(F.data == "swcoins:done")
async def swap_coins_done(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:
        await cb.answer()
        return
    data = await state.get_data()
    selected = list(data.get("swap_enabled_coins_set", ["USDT", "TON", "TRX"]))
    if not selected:
        await cb.answer("حداقل یک ارز باید انتخاب شود.", show_alert=True)
        return
    await state.clear()
    await set_setting(row.id, "swap_enabled_coins", ",".join(selected))
    note = f"✅ ارزهای فعال: {', '.join(selected)} ذخیره شد."
    await _swap_admin_menu_msg(bot, row.id, chat_id=cb.message.chat.id,
                                msg_id=cb.message.message_id, note=note)
    await cb.answer()


# ─── حذف تمام تنظیمات سواپ ───
@swap_admin_router.callback_query(F.data == "swadm:delete")
async def swap_delete_confirm(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 بله، حذف کن", callback_data="swadm:delete:confirm",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ لغو", callback_data="set:swap", style=ButtonStyle.DANGER)],
    ])
    await edit_or_replace(cb.message,
        "⚠️ <b>آیا مطمئنی؟</b>\n\n"
        "تمام تنظیمات سواپ‌ولت (کلید API، آدرس‌ها، حداقل/حداکثر) پاک می‌شود.\n"
        "درخواست‌های در انتظار کاربران همچنان در دیتابیس می‌مانند.",
        reply_markup=kb, parse_mode="HTML")


@swap_admin_router.callback_query(F.data == "swadm:delete:confirm")
async def swap_delete_do(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting, edit_or_replace
    row = await _owner_ok(cb, bot)
    if not row:
        return
    for key in ("swap_api_key", "swap_addr_usdt", "swap_addr_ton", "swap_addr_trx",
                "swap_address", "swap_coin", "swap_network",
                "swap_min_toman", "swap_max_toman", "swap_enabled_coins"):
        await set_setting(row.id, key, "")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    await edit_or_replace(cb.message, "✅ تمام تنظیمات سواپ‌ولت حذف شد.",
                          reply_markup=back_kb)
    await cb.answer()
