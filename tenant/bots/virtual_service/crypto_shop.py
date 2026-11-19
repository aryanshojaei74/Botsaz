"""«فروشِ رمزارز» (Sajad): کنارِ تون، ترون (TRX) و تتر (USDT) هم قابلِ فروش است.

=== PATCH_CRYPTO_SHOP_V1 ===
• دکمهٔ «فروشِ تون» (action=ton_shop) همان می‌ماند؛ اگر مالک ترون یا تتر را روشن کرده باشد، اول صفحهٔ «🪙 خرید رمز ارز» با
  دکمهٔ هر ارز (تون / ترون / تتر) می‌آید؛ وگرنه همان فروشِ تونِ قبلی بدونِ هیچ تغییر.
• هر ارز: قیمتِ امروز (دستی یا خودکار: ترون = قیمتِ دلاریِ TRX × نرخِ دلار، تتر = نرخِ دلار؛ با درصدِ سود)، بازهٔ مقدار، بسته‌هایِ آماده
  یا مقدارِ دلخواه، آدرسِ کیف‌پول (اعتبارسنجیِ شبکه)، فاکتور و پرداخت از کیف‌پولِ ربات.
• تحویل همیشه دستی است (ارسالِ خودکارِ ترون/تتر نداریم): سفارش با آدرس به مالک/ادمین می‌رود، «✅ ارسال شد» یا «↩️ بازگشتِ وجه».
• نرخِ فاکتور هنگامِ صدورِ فاکتور ثابت می‌شود (۱۵ دقیقه)؛ با نرخِ خودکار، پرداخت با همان قیمتِ دیده‌شده انجام می‌شود.
• سفارش‌ها در همان جدولِ GiftOrder با gift_id=«cxsell_{coin}_{centi}» ذخیره می‌شوند (centi = صدم‌واحد، شبکه در dest_memo).
"""
from __future__ import annotations

import html as _h
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, GiftOrder, WalletTxn, order_no

logger = _get_logger(__name__)

crypto_shop_router = Router(name="crypto_shop")

CENTI = 100
INVOICE_TTL = 15 * 60
COINS: dict[str, dict] = {
    "trx": {"sym": "TRX", "name": "ترون", "icon": "🔴", "picks": (5, 7, 10), "min": 1.0, "max": 10000.0,       # Sajad: «از ۵۰ پیشنهاد نده؛ ۵ و ۷ و ۱۰»
            "net_fixed": "TRC20"},
    "usdt": {"sym": "USDT", "name": "تتر", "icon": "💵", "picks": (5, 10, 20, 50, 100), "min": 2.0, "max": 1000.0,
             "net_fixed": None},
}
USDT_NETS = ("TRC20", "BEP20", "ERC20", "TON")
PREFIX = "cxsell_"


class CxFSM(StatesGroup):
    amount_in = State()
    addr_in = State()
    adm_val = State()


# ───────────── تنظیمات و نرخ ─────────────
async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant.bots.virtual_service import smm as S          # همان منبعِ تنظیماتِ فروشِ تون (یک مسیرِ خواندن)
    return await S._get_setting(bot_id, key, default)


async def _ss(bot_id: int, key: str, value: str) -> None:
    from tenant.bots.virtual_service import smm as S
    await S._set_setting(bot_id, key, value)


def _f(v, default: float) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


async def coin_cfg(bot_id: int, coin: str) -> dict:
    d = COINS[coin]
    return {
        "on": (await _gs(bot_id, f"cx_{coin}_on", "0")) == "1",
        "auto": (await _gs(bot_id, f"cx_{coin}_auto", "0")) == "1",
        "margin": max(0.0, min(200.0, _f(await _gs(bot_id, f"cx_{coin}_margin", "5"), 5.0))),
        "manual": max(0.0, _f(await _gs(bot_id, f"cx_{coin}_rate", "0"), 0.0)),
        "min": max(0.01, _f(await _gs(bot_id, f"cx_{coin}_min", str(d["min"])), d["min"])),
        "max": max(0.01, _f(await _gs(bot_id, f"cx_{coin}_max", str(d["max"])), d["max"])),
    }


async def network_of(bot_id: int, coin: str) -> str:
    fixed = COINS[coin]["net_fixed"]
    if fixed:
        return fixed
    net = (await _gs(bot_id, "cx_usdt_net", "TRC20")).strip().upper()
    return net if net in USDT_NETS else "TRC20"


_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_TTL = 300.0


async def _fetch_trx_usd() -> float:
    """قیمتِ هر TRX به دلار: CoinGecko ← Binance (بدونِ کلید). صفر = ناموفق. کش ۵ دقیقه‌ای."""
    c = _PRICE_CACHE.get("trx")
    if c and time.time() - c[1] < _PRICE_TTL:
        return c[0]
    try:
        import aiohttp
        t = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=t) as s:
            for url, pick in (("https://api.coingecko.com/api/v3/simple/price?ids=tron&vs_currencies=usd",
                               lambda d: d["tron"]["usd"]),
                              ("https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT", lambda d: d["price"])):
                try:
                    async with s.get(url) as r:
                        val = float(pick(await r.json(content_type=None)))
                    if val > 0:
                        _PRICE_CACHE["trx"] = (val, time.time())
                        return val
                except Exception:  # noqa: BLE001 — منبعِ بعدی
                    logger.info("crypto_shop: price source failed: %s", url.split("/")[2])
    except Exception:  # noqa: BLE001
        logger.exception("crypto_shop: TRX price fetch failed")
    return 0.0


async def rate_of(bot_id: int, coin: str) -> float:
    """قیمتِ هر ۱ واحدِ ارز به تومان. خودکار شکست بخورد ⇒ نرخِ دستیِ ذخیره‌شده؛ صفر یعنی قابلِ فروش نیست."""
    cfg = await coin_cfg(bot_id, coin)
    if cfg["auto"]:
        try:
            from tenant.gateways.stars_rate import usd_toman
            usd = float(await usd_toman() or 0)
            if usd > 0:
                unit_usd = 1.0 if coin == "usdt" else float(await _fetch_trx_usd() or 0)
                if unit_usd > 0:
                    return unit_usd * usd * (1.0 + cfg["margin"] / 100.0)
        except Exception:  # noqa: BLE001
            logger.exception("crypto_shop: auto rate failed; using the manual rate")
    return cfg["manual"]


async def enabled_coins(bot_id: int) -> list[str]:
    out = []
    for c in COINS:
        cfg = await coin_cfg(bot_id, c)
        if cfg["on"] and await rate_of(bot_id, c) > 0:
            out.append(c)
    return out


# ───────────── ابزارها ─────────────
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫", "01234567890123456789.")


def parse_amount(raw: str | None) -> float | None:
    t = (raw or "").strip().translate(_DIG).replace(",", "").replace("٬", "").replace("،", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", t):
        return None
    v = round(float(t), 2)
    return v if v > 0 else None


_TRON_RX = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
_EVM_RX = re.compile(r"0x[0-9a-fA-F]{40}")


def valid_address(net: str, addr: str) -> bool:
    a = (addr or "").strip()
    if net == "TRC20":
        return bool(_TRON_RX.fullmatch(a))
    if net in ("BEP20", "ERC20"):
        return bool(_EVM_RX.fullmatch(a))
    if net == "TON":
        from tenant.bots.virtual_service.smm import _looks_like_ton_address
        return bool(_looks_like_ton_address(a))
    return False


def _addr_hint(net: str) -> str:
    return {"TRC20": "شبکهٔ ترون (TRC20): آدرس با <code>T</code> شروع می‌شود و ۳۴ کاراکتر است.",
            "BEP20": "شبکهٔ BSC (BEP20): آدرس با <code>0x</code> شروع می‌شود (۴۲ کاراکتر).",
            "ERC20": "شبکهٔ اتریوم (ERC20): آدرس با <code>0x</code> شروع می‌شود (۴۲ کاراکتر).",
            "TON": "شبکهٔ TON: آدرس با <code>UQ</code> یا <code>EQ</code> شروع می‌شود."}.get(net, "")


def _g(x: float) -> str:
    """۱۲٫۵ ⇒ «12.5»، ۱۰۰۰۰۰۰ ⇒ «1000000» (بدونِ نمادِ علمی مثلِ 1e+06)."""
    t = ("%.2f" % x).rstrip("0").rstrip(".")
    return t or "0"


async def _style(bot_id: int, key: str, default):
    from tenant.bots.virtual_service import smm as S
    return await S._vs_style(bot_id, key, default)


# ───────────── سمتِ مشتری ─────────────
async def show_hub(bot: Bot, target: Message, bot_id: int, state: FSMContext | None = None) -> None:
    """صفحهٔ «خرید رمز ارز»: تون (اگر فروشش آماده است) + ترون/تتر (اگر روشن‌اند)."""
    from tenant.bots.virtual_service import smm as S
    coins = await enabled_coins(bot_id)
    ton_ok = (await S._ton_sell_settings(bot_id))[0] > 0
    if state is not None:
        await state.set_state(None)
    st = await _style(bot_id, "ibtn:vs_choice", ButtonStyle.PRIMARY)
    kw = {"style": st} if st is not None else {}
    btns = []
    if ton_ok:
        btns.append(InlineKeyboardButton(text="💎 خرید ارز تون", callback_data="cx:ton", **kw))
    for c in coins:
        btns.append(InlineKeyboardButton(text=COINS[c]["icon"] + " خرید ارز " + COINS[c]["name"], callback_data=f"cx:c:{c}", **kw))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="fsm:cancel", style=ButtonStyle.DANGER)])
    await target.answer("🪙 <b>خرید رمز ارز</b>\n\nارز موردنظر را انتخاب کنید:", parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _row(cb_or_msg, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    return row


@crypto_shop_router.callback_query(F.data == "cx:hub")
async def cx_hub(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(cb, bot)
    await cb.answer()
    if row:
        await state.clear()
        await show_hub(bot, cb.message, row.id, state)


@crypto_shop_router.callback_query(F.data == "cx:ton")
async def cx_ton(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import show_ton_shop_to_user
    row = await _row(cb, bot)
    await cb.answer()
    if row:
        await show_ton_shop_to_user(bot, cb.message, row.id, state, skip_hub=True)


async def _coin_page(target, bot: Bot, state: FSMContext, bot_id: int, coin: str) -> bool:
    cfg = await coin_cfg(bot_id, coin)
    rate = await rate_of(bot_id, coin)
    d = COINS[coin]
    if not cfg["on"] or rate <= 0:
        return False
    await state.update_data(cx_coin=coin)
    await state.set_state(CxFSM.amount_in)
    net = await network_of(bot_id, coin)
    st = await _style(bot_id, "ibtn:vs_choice", ButtonStyle.PRIMARY)
    kw = {"style": st} if st is not None else {}
    picks = [q for q in d["picks"] if cfg["min"] <= q <= cfg["max"]]
    btns = [InlineKeyboardButton(text=f"{d['icon']} {_g(q)} {d['sym']} — {round(q * rate):,} تومان",
                                 callback_data=f"cx:q:{coin}:{_g(q)}", **kw) for q in picks]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="✍️ مقدارِ دلخواه", callback_data=f"cx:qc:{coin}", **kw)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cx:hub", style=ButtonStyle.DANGER)])
    await target.answer(
        f"{d['icon']} <b>خریدِ {d['name']} ({d['sym']})</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🌐 شبکه: <b>{net}</b>\n"
        f"📈 نرخِ امروز: هر ۱ {d['sym']} = <b>{rate:,.0f} تومان</b>\n"
        f"📏 بازهٔ سفارش: <b>{_g(cfg['min'])}</b> تا <b>{_g(cfg['max'])}</b> {d['sym']}\n"
        "━━━━━━━━━━━━━━━\n"
        "👇 یکی از بسته‌هایِ آماده را بزن، یا «✍️ مقدارِ دلخواه» را انتخاب کن و عدد را بفرست (اعشار مجاز است):",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:c:(trx|usdt)$"))
async def cx_coin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(cb, bot)
    if not row:
        return await cb.answer()
    coin = cb.data.split(":")[2]
    if not await _coin_page(cb.message, bot, state, row.id, coin):
        return await cb.answer("این ارز فعلاً در دسترس نیست.", show_alert=True)
    await cb.answer()


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:qc:(trx|usdt)$"))
async def cx_custom(cb: CallbackQuery, state: FSMContext):
    coin = cb.data.split(":")[2]
    await state.update_data(cx_coin=coin)
    await state.set_state(CxFSM.amount_in)
    await cb.answer("مقدارِ دلخواهت را بفرست (مثلاً 12.5).", show_alert=True)


async def _amount_chosen(answer, bot: Bot, state: FSMContext, row, coin: str, amount: float) -> None:
    cfg = await coin_cfg(row.id, coin)
    d = COINS[coin]
    net = await network_of(row.id, coin)
    if not cfg["on"] or await rate_of(row.id, coin) <= 0:
        await state.clear()
        await answer("این ارز فعلاً در دسترس نیست.")
        return
    if amount < cfg["min"] or amount > cfg["max"]:
        await answer(f"❌ مقدار باید بینِ {_g(cfg['min'])} تا {_g(cfg['max'])} {d['sym']} باشد. دوباره بفرست:")
        return
    await state.update_data(cx_coin=coin, cx_amount=amount, cx_net=net)
    await state.set_state(CxFSM.addr_in)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"cx:c:{coin}", style=ButtonStyle.DANGER)]])
    await answer(
        f"✅ مقدار: <b>{_g(amount)} {d['sym']}</b>\n\n"
        f"👛 حالا آدرسِ کیف‌پولِ <b>{d['name']}</b> خودت را بفرست.\n"
        f"🌐 شبکه: <b>{net}</b> — {_addr_hint(net)}\n\n"
        "⚠️ آدرس را دقیق و فقط برایِ همین شبکه بفرست؛ ارسال به آدرس/شبکهٔ اشتباه قابلِ بازگشت نیست.",
        parse_mode="HTML", reply_markup=kb)


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:q:(trx|usdt):[0-9.]+$"))
async def cx_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(cb, bot)
    if not row:
        return await cb.answer()
    _, _, coin, raw = cb.data.split(":")
    amount = parse_amount(raw)
    if amount is None:
        return await cb.answer("مقدار نامعتبر.", show_alert=True)
    await cb.answer()
    await _amount_chosen(cb.message.answer, bot, state, row, coin, amount)


@crypto_shop_router.message(CxFSM.amount_in, F.text)
async def cx_amount_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(msg, bot)
    if not row:
        return
    coin = (await state.get_data()).get("cx_coin")
    if coin not in COINS:
        await state.clear()
        await msg.answer("جلسه منقضی شد؛ دوباره از «خرید رمز ارز» شروع کن.")
        return
    amount = parse_amount(msg.text)
    if amount is None:
        await msg.answer("❌ مقدارِ نامعتبر. فقط عدد بفرست (مثلاً 12.5):")
        return
    await _amount_chosen(msg.answer, bot, state, row, coin, amount)


@crypto_shop_router.message(CxFSM.addr_in, F.text)
async def cx_addr_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant.shared import vs_pay as _vp
    row = await _row(msg, bot)
    if not row:
        return
    data = await state.get_data()
    coin, amount, net = data.get("cx_coin"), data.get("cx_amount"), data.get("cx_net")
    if coin not in COINS or not amount or not net:
        await state.clear()
        await msg.answer("جلسه منقضی شد؛ دوباره از «خرید رمز ارز» شروع کن.")
        return
    addr = (msg.text or "").strip()
    if not valid_address(net, addr):
        await msg.answer(f"❌ این آدرس برایِ شبکهٔ {net} معتبر نیست.\n{_addr_hint(net)}\nدوباره بفرست:", parse_mode="HTML")
        return
    rate = await rate_of(row.id, coin)
    cfg = await coin_cfg(row.id, coin)
    if rate <= 0 or not cfg["on"] or amount < cfg["min"] or amount > cfg["max"]:
        await state.clear()
        await msg.answer("این سفارش دیگر معتبر نیست؛ دوباره از «خرید رمز ارز» شروع کن.")
        return
    price = round(amount * rate)
    await state.update_data(cx_addr=addr, cx_price=price, cx_rate=rate, cx_ts=time.time())
    await state.set_state(None)
    d = COINS[coin]
    pay_rows = await _vp.pay_rows(row.id, msg.from_user.id, int(price), "cx:pay")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *pay_rows, [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cx:hub", style=ButtonStyle.DANGER)]])
    await msg.answer(
        "🧾 <b>فاکتورِ خرید</b>\n\n"
        f"کالا: {d['icon']} {_g(amount)} {d['sym']}\n"
        f"🌐 شبکه: {net}\n"
        f"مقصد: <code>{_h.escape(addr)}</code>\n"
        f"📈 نرخ: هر ۱ {d['sym']} = {rate:,.0f} تومان\n"
        f"قیمت: {price:,} تومان\n\n"
        f"مبلغِ قابل‌پرداخت: {price:,} تومان (نرخ تا ۱۵ دقیقه ثابت است)" + ("" if pay_rows else _vp.NO_METHOD_NOTE),
        parse_mode="HTML", reply_markup=kb)


@crypto_shop_router.callback_query(F.data == "cx:pay")
async def cx_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_or_create_user
    from tenant.bots.virtual_service import smm as S
    from tenant.shared import vs_pay as _vp
    from models import alloc_tracking_in as _alloc_in
    row = await _row(cb, bot)
    if not row:
        return await cb.answer()
    if not await _vp.wallet_pay_allowed(row.id):
        return await cb.answer("❌ پرداخت از کیف‌پول در این ربات غیرفعال است.", show_alert=True)
    data = await state.get_data()
    coin, amount, addr, net = data.get("cx_coin"), data.get("cx_amount"), data.get("cx_addr"), data.get("cx_net")
    price, ts = int(data.get("cx_price") or 0), float(data.get("cx_ts") or 0)
    if coin not in COINS or not amount or not addr or price <= 0:
        return await cb.answer("این فاکتور منقضی شده؛ دوباره از «خرید رمز ارز» شروع کن.", show_alert=True)
    if time.time() - ts > INVOICE_TTL:
        await state.clear()
        return await cb.answer("نرخِ این فاکتور منقضی شد؛ دوباره از «خرید رمز ارز» شروع کن.", show_alert=True)
    cfg = await coin_cfg(row.id, coin)
    if not cfg["on"] or amount < cfg["min"] or amount > cfg["max"] or not valid_address(net, addr):
        await state.clear()
        return await cb.answer("این سفارش دیگر معتبر نیست؛ دوباره از «خرید رمز ارز» شروع کن.", show_alert=True)
    d = COINS[coin]
    buyer, _ = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
                                        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)       # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        if float(b.balance) < price:
            await s.rollback()
            await cb.answer("❌ موجودی کافی نیست.")
            await _vp.insufficient_notice(cb.message, row.id, cb.from_user.id, int(price), cb.data)
            return
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ {_g(amount)} {d['name']} ({d['sym']})"))
        trk = await _alloc_in(s, row.id)
        centi = int(round(amount * CENTI))
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=f"{PREFIX}{coin}_{centi}", gift_emoji=d["icon"],
                          star_count=centi, price_toman=price, dest_telegram_id=cb.from_user.id, dest_label=addr[:120],
                          dest_memo=f"net:{net}", status="awaiting_admin", pay_method="wallet", tracking_code=trk)
        s.add(order)
        await s.commit()
        oid = order.id
    await state.clear()
    await cb.answer("✅ پرداخت شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        logger.debug("cx: removing invoice buttons failed")
    await cb.message.answer(
        "✅ <b>سفارشت ثبت شد</b>\n\n"
        f"{d['icon']} مقدار: {_g(amount)} {d['sym']}\n🌐 شبکه: {net}\n🎯 آدرس: <code>{_h.escape(addr)}</code>\n"
        f"💰 پرداخت‌شده: {price:,} تومان\n🔖 کد پیگیری: <code>{trk}</code>\n\nبه‌محضِ ارسال، پیامِ تکمیل می‌آید.",
        parse_mode="HTML", reply_markup=await S._gift_support_kb(trk))
    # === PATCH_TRON_AUTOSEND_V1 === ترون/تتر روی شبکهٔ TRC20 و ارسالِ خودکارِ روشن ⇒ از کیف‌پولِ ترونِ ربات می‌رود
    from tenant.bots.virtual_service import tron_autosend as TA
    auto, auto_note = "disabled", ""
    if net == "TRC20":
        auto, auto_note = await TA.deliver_order(bot, row, oid)
    if auto == "sent":
        await _sent_notify(bot, row, oid)
        try:
            await S._notify_manual_order(
                bot, row, f"🤖 <b>{d['name']}ِ سفارش #{await order_no(GiftOrder, oid)} خودکار ارسال شد</b>\n"
                          f"{d['icon']} {_g(amount)} {d['sym']} → <code>{_h.escape(addr)}</code>\n💰 {price:,} تومان · 🔖 {trk}")
        except Exception:  # noqa: BLE001
            logger.exception("cx: auto-send notice failed")
        return
    await _admin_notice(bot, row, oid, coin, amount, price, net, addr, trk, cb.from_user, auto, auto_note)


async def _admin_notice(bot: Bot, row, oid: int, coin: str, amount: float, price: int, net: str, addr: str, trk: str,
                        user, auto: str = "disabled", auto_note: str = "") -> None:
    from tenant.bots.virtual_service import smm as S
    d = COINS[coin]
    line = ""
    if auto == "manual":
        line = f"\n⚠️ ارسالِ خودکار انجام نشد: {_h.escape(auto_note)}\n"
    elif auto == "unconfirmed":
        line = ("\n⚠️ <b>ارسالِ خودکار نامشخص ماند</b> (" + _h.escape(auto_note) + ").\n"
                "قبل از هر کاری هشِ تراکنش را روی بلاک‌چین (tronscan) چک کن؛ اگر رفته «ارسال شد» را بزن، اگر نه دستی بفرست و بعد تأیید کن "
                "(یا بازپرداخت). ارسالِ خودکارِ بعدی تا ۱۵ دقیقه متوقف است.\n")
    rows = [[InlineKeyboardButton(text="✅ ارسال شد", callback_data=f"cx:done:{oid}", style=ButtonStyle.SUCCESS),
             InlineKeyboardButton(text="↩️ رد و بازگشتِ وجه", callback_data=f"cx:refund:{oid}", style=ButtonStyle.DANGER)]]
    if auto == "manual":          # فقط وقتی قطعاً نرفته؛ «نامشخص» عمداً دکمهٔ تکرار ندارد (خطرِ ارسالِ دوباره)
        rows.append([InlineKeyboardButton(text="🤖 ارسالِ خودکارِ مجدد", callback_data=f"cx:retry:{oid}", style=ButtonStyle.PRIMARY)])
    if auto == "unconfirmed":
        async with Session() as s:
            o = await s.get(GiftOrder, oid)
        if o is not None and o.tx_hash:
            rows.append([InlineKeyboardButton(text="🔎 هشِ تراکنش (tronscan)", url=f"https://tronscan.org/#/transaction/{o.tx_hash}")])
    uname = f"@{user.username}" if getattr(user, "username", None) else "ندارد"
    try:
        await S._notify_manual_order(
            bot, row,
            f"{d['icon']} سفارشِ {d['name']} ({d['sym']}) — #{await order_no(GiftOrder, oid)}\n"
            f"مقدار: {_g(amount)} {d['sym']}\n💰 {price:,} تومان\n🌐 شبکه: <b>{net}</b>\n" + line +
            f"🎯 آدرس: <code>{_h.escape(addr)}</code>\n"
            f"👤 خریدار: {_h.escape(getattr(user, 'full_name', '') or '—')} · {uname} · <code>{user.id}</code>\n"
            f"🔖 کد: {trk}\n\nاین مقدار را باید خودت واقعاً به آدرسِ بالا (همین شبکه) بفرستی، بعد «✅ ارسال شد» را بزن.",
            InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:  # noqa: BLE001
        logger.exception("cx: notifying the admin failed (order %s stays awaiting)", oid)


async def _sent_notify(bot: Bot, row, oid: int) -> None:
    """بعد از تحویل (دستی یا خودکار): پیام به مشتری (با دکمهٔ هشِ تراکنش اگر داریم) + گزارشِ کانالِ اعتمادسازی."""
    from tenant.bots.virtual_service import smm as S
    async with Session() as s:
        o = await s.get(GiftOrder, oid)
        buyer = await s.get(BotUser, o.buyer_user_id) if o else None
    if o is None:
        return
    coin, amount, net = _parse_order(o)
    d = COINS[coin]
    if buyer is not None:
        kb = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="هـش تـراکـنـش 👌", url=f"https://tronscan.org/#/transaction/{o.tx_hash}")]]) if o.tx_hash else None)
        try:
            await bot.send_message(
                buyer.telegram_id,
                f"✅ <b>سفارشِ {d['name']}ِ تو (#{await order_no(GiftOrder, oid)}) ارسال شد!</b>\n\n"
                f"{d['icon']} مقدار: {_g(amount)} {d['sym']}\n🌐 شبکه: {net}\n🎯 آدرس: <code>{_h.escape(o.dest_label or '')}</code>\n"
                f"🔖 کد پیگیری: {o.tracking_code}", parse_mode="HTML", reply_markup=kb)
        except Exception:  # noqa: BLE001
            logger.exception("cx: notifying the buyer failed")
    try:
        await S._notify_gift_order_trust_success(bot, row, oid)
    except Exception:  # noqa: BLE001
        logger.exception("cx: trust report failed")


@crypto_shop_router.callback_query(F.data.startswith("cx:retry:"))
async def cx_retry(cb: CallbackQuery, bot: Bot):
    """«🤖 ارسالِ خودکارِ مجدد» (مالک): فقط وقتی قطعاً ارسال نشده (awaiting_admin)."""
    from tenant.bots.virtual_service import smm as S
    from tenant.bots.virtual_service import tron_autosend as TA
    row = await S._owner(cb, bot)
    if not row:
        return
    try:
        oid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    await cb.answer("⏳ در حالِ ارسال…")
    res, note = await TA.deliver_order(bot, row, oid, force=True)
    if res == "sent":
        await S._admin_msg_close(cb, "🤖 خودکار ارسال شد")
        await _sent_notify(bot, row, oid)
        return
    await cb.message.answer(("⚠️ ارسالِ خودکارِ مجدد هم انجام نشد: " if res == "manual" else "⚠️ نامشخص: ") + _h.escape(note))


# ───────────── تحویل / بازگشتِ وجه (ادمین) ─────────────
def _parse_order(o: GiftOrder) -> tuple[str, float, str]:
    parts = (o.gift_id or "").split("_")
    coin = parts[1] if len(parts) > 1 and parts[1] in COINS else "usdt"
    net = (o.dest_memo or "").replace("net:", "") or "TRC20"
    return coin, (o.star_count or 0) / CENTI, net


@crypto_shop_router.callback_query(F.data.startswith("cx:done:"))
async def cx_done(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    try:
        oid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    async with Session() as s:
        o = await s.get(GiftOrder, oid, with_for_update=True)
        if not o or o.bot_id != row.id or not (o.gift_id or "").startswith(PREFIX):
            return await cb.answer("سفارش یافت نشد.", show_alert=True)
        if o.status not in ("awaiting_admin", "sending"):      # sending = ارسالِ خودکارِ نامشخص؛ ادمین بعد از چکِ بلاک‌چین تأیید می‌کند
            return await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        o.status = "sent"
        o.fail_reason = None
        await s.commit()
    await cb.answer("✅ به‌عنوان ارسال‌شده ثبت شد.")
    await S._admin_msg_close(cb, "✅ ارسال شد")
    await _sent_notify(bot, row, oid)


@crypto_shop_router.callback_query(F.data.startswith("cx:refund:"))
async def cx_refund(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    try:
        oid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return await cb.answer()
    async with Session() as s:
        o = await s.get(GiftOrder, oid, with_for_update=True)
        if not o or o.bot_id != row.id or not (o.gift_id or "").startswith(PREFIX):
            return await cb.answer("سفارش یافت نشد.", show_alert=True)
        if o.status not in ("awaiting_admin", "sending"):
            return await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        no_txt = await order_no(GiftOrder, oid)          # پیش از هر تغییر (کوئریِ جدا وسطِ تراکنش نباید به تغییراتِ flush‌شده دست بزند)
        o.status = "refunded"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        refunded = int(o.price_toman or 0)
        coin, _a, _n = _parse_order(o)
        if buyer:
            buyer.balance = float(buyer.balance or 0) + refunded
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=refunded, kind="charge",
                            note=f"بازپرداختِ سفارشِ {COINS[coin]['name']} #{no_txt}"))
        buyer_tg = buyer.telegram_id if buyer else None
        await s.commit()
    await cb.answer("↩️ بازپرداخت شد.")
    await S._admin_msg_close(cb, "↩️ بازپرداخت شد")
    if buyer_tg:
        try:
            await bot.send_message(buyer_tg, f"↩️ سفارشِ {COINS[coin]['name']}ِ تو (#{await order_no(GiftOrder, oid)}) لغو شد و "
                                             f"مبلغِ {refunded:,} تومان به کیف‌پولت بازگشت.")
        except Exception:  # noqa: BLE001
            logger.exception("cx: refund notice failed")


@crypto_shop_router.callback_query(F.data.startswith("cx:orders:"))
async def cx_orders(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        orders = (await s.scalars(select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.gift_id.like(PREFIX + "%"), GiftOrder.status.in_(("awaiting_admin", "sending")))
            .order_by(GiftOrder.id.desc()).limit(10))).all()
        data = [(o.id, _parse_order(o), o.price_toman, o.dest_label, o.tracking_code) for o in orders]
    if not data:
        return await cb.answer("سفارشِ در انتظاری نیست.", show_alert=True)
    rows, lines = [], ["🪙 <b>سفارش‌هایِ در انتظارِ ترون/تتر</b>\n"]
    for oid, (coin, amount, net), price, addr, trk in data:
        no = await order_no(GiftOrder, oid)
        lines.append(f"#{no} — {_g(amount)} {COINS[coin]['sym']} ({net}) — {int(price or 0):,} تومان\n🎯 <code>{_h.escape(addr or '')}</code>  🔖 {trk}")
        rows.append([InlineKeyboardButton(text=f"✅ تأیید ارسال #{no}", callback_data=f"cx:done:{oid}")])
        rows.append([InlineKeyboardButton(text=f"❌ لغو و بازپرداخت #{no}", callback_data=f"cx:refund:{oid}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cx:adm", style=ButtonStyle.DANGER)])
    await cb.message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ───────────── پنلِ ادمین ─────────────
async def admin_home(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows, lines = [], ["🪙 <b>ترون و تتر (فروشِ رمزارز)</b>\n",
                       "کنارِ تون، مشتری می‌تواند ترون و تتر هم بخرد. اگر «ارسالِ خودکار» روشن و کیف‌پولِ ترون شارژ باشد، ربات خودش "
                       "روی بلاک‌چین می‌فرستد؛ وگرنه سفارش با آدرسِ مشتری برایِ تو می‌آید و «✅ ارسال شد» را می‌زنی.\n"]
    for c, d in COINS.items():
        cfg = await coin_cfg(bot_id, c)
        rate = await rate_of(bot_id, c)
        on = cfg["on"] and rate > 0
        lines.append(f"{d['icon']} {d['name']}: {'✅ روشن' if on else ('⚠️ روشن ولی نرخ ندارد' if cfg['on'] else '⛔️ خاموش')}"
                     + (f" — {rate:,.0f} تومان" if rate > 0 else ""))
        rows.append([InlineKeyboardButton(text=f"{d['icon']} تنظیمِ {d['name']}", callback_data=f"cx:adm:{c}", style=ButtonStyle.PRIMARY)])
    from tenant.bots.virtual_service import smm as S
    from tenant.bots.virtual_service import tron_autosend as TA
    ton_rate = (await S._ton_sell_settings(bot_id))[0]
    lines.insert(2, f"💎 تون: {'✅ آماده' if ton_rate > 0 else '⚠️ نرخ ندارد'}" + (f" — {ton_rate:,.0f} تومان" if ton_rate > 0 else "")
                 + " (تنظیمِ کامل: «💎 تنظیماتِ تون»)")
    tcfg = await TA.settings(bot_id)
    addr = await TA.wallet_address(bot_id)
    lines.append("")
    lines.append(f"👛 کیف‌پولِ ترون: {'✅ ساخته شده' if addr else '⛔️ ساخته نشده'} · ارسالِ خودکار: "
                 f"{'✅ روشن' if (tcfg['enabled'] and addr) else '⛔️ خاموش'}")
    rows.append([InlineKeyboardButton(text="💎 تنظیماتِ تون", callback_data="ton:menu", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="👛 کیف‌پولِ ترون و ارسالِ خودکار", callback_data="cx:w", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🧾 سفارش‌هایِ در انتظار", callback_data="cx:orders:0")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_coin(bot_id: int, coin: str) -> tuple[str, InlineKeyboardMarkup]:
    d = COINS[coin]
    cfg = await coin_cfg(bot_id, coin)
    rate = await rate_of(bot_id, coin)
    net = await network_of(bot_id, coin)
    text = (f"{d['icon']} <b>فروشِ {d['name']} ({d['sym']})</b>\n\n"
            f"وضعیت: <b>{'✅ روشن' if cfg['on'] else '⛔️ خاموش'}</b>\n"
            f"💱 نرخِ دستی: <b>{cfg['manual']:,.0f}</b> تومان\n"
            f"🔄 نرخِ خودکار: <b>{'روشن' if cfg['auto'] else 'خاموش'}</b> (درصدِ سود: {_g(cfg['margin'])}٪)\n"
            f"📈 نرخِ فعلیِ فروش: <b>{rate:,.0f}</b> تومان\n"
            f"📏 بازه: {_g(cfg['min'])} تا {_g(cfg['max'])} {d['sym']}\n"
            f"🌐 شبکه: <b>{net}</b>\n\n"
            "ℹ️ نرخِ خودکار: تتر = نرخِ دلار؛ ترون = قیمتِ دلاریِ TRX × نرخِ دلار؛ هر دو + درصدِ سود. اگر دریافتِ نرخ شکست بخورد، "
            "نرخِ دستی استفاده می‌شود.")
    rows = [
        [InlineKeyboardButton(text="🔴 خاموش‌کردن" if cfg["on"] else "🟢 روشن‌کردن", callback_data=f"cx:t:{coin}:on",
                              style=ButtonStyle.DANGER if cfg["on"] else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=f"💱 نرخِ دستی ({cfg['manual']:,.0f})", callback_data=f"cx:s:{coin}:rate")],
        [InlineKeyboardButton(text="🔄 نرخِ خودکار: ✅ روشن" if cfg["auto"] else "🔄 نرخِ خودکار: خاموش", callback_data=f"cx:t:{coin}:auto",
                              style=ButtonStyle.SUCCESS if cfg["auto"] else ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({_g(cfg['margin'])}٪)", callback_data=f"cx:s:{coin}:margin")],
        [InlineKeyboardButton(text=f"🔽 حداقل ({_g(cfg['min'])})", callback_data=f"cx:s:{coin}:min"),
         InlineKeyboardButton(text=f"🔼 حداکثر ({_g(cfg['max'])})", callback_data=f"cx:s:{coin}:max")],
    ]
    if coin == "usdt":
        rows.append([InlineKeyboardButton(text=f"🌐 شبکهٔ تتر: {net} (تغییر)", callback_data="cx:net")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cx:adm", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001
        if "not modified" not in str(e).lower():
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@crypto_shop_router.callback_query(F.data == "cx:adm")
async def cx_adm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await admin_home(row.id)
    await _edit(cb, text, kb)
    await cb.answer()


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:adm:(trx|usdt)$"))
async def cx_adm_coin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await admin_coin(row.id, cb.data.split(":")[2])
    await _edit(cb, text, kb)
    await cb.answer()


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:t:(trx|usdt):(on|auto)$"))
async def cx_toggle(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    _, _, coin, what = cb.data.split(":")
    key = f"cx_{coin}_{what}"
    cur = (await _gs(row.id, key, "0")) == "1"
    await _ss(row.id, key, "0" if cur else "1")
    await cb.answer("خاموش شد" if cur else "روشن شد")
    text, kb = await admin_coin(row.id, coin)
    await _edit(cb, text, kb)


@crypto_shop_router.callback_query(F.data == "cx:net")
async def cx_net(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    cur = await network_of(row.id, "usdt")
    nxt = USDT_NETS[(USDT_NETS.index(cur) + 1) % len(USDT_NETS)]
    await _ss(row.id, "cx_usdt_net", nxt)
    await cb.answer("شبکه: " + nxt)
    text, kb = await admin_coin(row.id, "usdt")
    await _edit(cb, text, kb)


_FIELD_FA = {"rate": "نرخِ دستی (تومان برایِ هر ۱ واحد)", "margin": "درصدِ سود (۰ تا ۲۰۰)", "min": "حداقلِ مقدار", "max": "حداکثرِ مقدار"}


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:s:(trx|usdt):(rate|margin|min|max)$"))
async def cx_set_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    _, _, coin, field = cb.data.split(":")
    await state.set_state(CxFSM.adm_val)
    await state.update_data(cx_adm_coin=coin, cx_adm_field=field)
    await cb.message.answer(f"✏️ {_FIELD_FA[field]} را بفرست (فقط عدد؛ برایِ انصراف /cancel):")
    await cb.answer()


@crypto_shop_router.message(CxFSM.adm_val, F.text)
async def cx_set_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(msg, bot)
    if not row:
        return
    data = await state.get_data()
    coin, field = data.get("cx_adm_coin"), data.get("cx_adm_field")
    if (msg.text or "").strip().lower() in ("/cancel", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if coin == "wallet" and field in ("cap:trx", "cap:usdt", "apikey"):
        await wallet_field_save(msg, state, row, field)
        return
    if coin not in COINS or field not in _FIELD_FA:
        await state.clear()
        return
    raw = (msg.text or "").strip().translate(_DIG).replace(",", "").replace("٬", "").replace("،", "")
    try:
        val = float(raw)
    except ValueError:
        await msg.answer("❌ فقط عدد بفرست (یا /cancel):")
        return
    lim = {"rate": (0, 1e9), "margin": (0, 200), "min": (0.01, 1e7), "max": (0.01, 1e7)}[field]
    if not lim[0] <= val <= lim[1]:
        await msg.answer(f"❌ عدد باید بینِ {_g(lim[0])} و {_g(lim[1])} باشد:")
        return
    await _ss(row.id, f"cx_{coin}_{field}", _g(val) if field != "rate" else str(int(round(val))))
    await state.clear()
    text, kb = await admin_coin(row.id, coin)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ───────────── کیف‌پولِ ترون و ارسالِ خودکار (مالک) ─────────────
async def wallet_view(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.virtual_service import tron_autosend as TA
    cfg = await TA.settings(bot_id)
    addr = await TA.wallet_address(bot_id)
    rows = []
    if not addr:
        text = ("👛 <b>کیف‌پولِ ترون (ارسالِ خودکارِ TRX و USDT)</b>\n\n"
                "هنوز کیف‌پولی ساخته نشده. با «🆕 ساختِ کیف‌پول» یک کیف‌پولِ جدیدِ ترونِ اختصاصیِ همین ربات ساخته می‌شود؛ کلیدِ خصوصی "
                "رمزنگاری‌شده در ربات می‌ماند. بعد آدرسش را شارژ کن (TRX برایِ کارمزد و فروشِ ترون، USDT-TRC20 برایِ فروشِ تتر).\n\n"
                "⚠️ فقط به‌اندازهٔ لازم شارژ کن؛ این یک کیف‌پولِ داغ (هات‌ولت) است.")
        rows.append([InlineKeyboardButton(text="🆕 ساختِ کیف‌پول", callback_data="cx:w:new", style=ButtonStyle.SUCCESS)])
    else:
        trx, usdt = await TA.balances(bot_id)
        text = ("👛 <b>کیف‌پولِ ترون (ارسالِ خودکارِ TRX و USDT)</b>\n\n"
                f"آدرس: <code>{_h.escape(addr)}</code>\n"
                f"🔴 موجودیِ TRX: <b>{'؟' if trx is None else format(trx, ',.2f')}</b>\n"
                f"💵 موجودیِ USDT (TRC20): <b>{'؟' if usdt is None else format(usdt, ',.2f')}</b>\n\n"
                f"🤖 ارسالِ خودکار: <b>{'✅ روشن' if cfg['enabled'] else '⛔️ خاموش'}</b>\n"
                f"🛡 سقفِ هر سفارش: TRX <b>{_g(cfg['cap_trx'])}</b> · USDT <b>{_g(cfg['cap_usdt'])}</b>\n"
                f"🔑 کلیدِ TronGrid: <b>{'ثبت شده' if cfg['api_key'] else 'ندارد (محدودیتِ نرخِ عمومی)'}</b>\n\n"
                "ℹ️ برایِ ارسالِ USDT حداقل ۳۰ TRX در همین کیف‌پول لازم است (کارمزدِ انرژیِ شبکه). فقط شبکهٔ TRC20 خودکار می‌شود؛ "
                "سفارش‌هایِ شبکه‌هایِ دیگر دستی می‌مانند. اگر خطایِ نامشخص رخ دهد، ارسالِ خودکار ۱۵ دقیقه می‌ایستد و سفارش برایِ چکِ دستی می‌ماند.")
        rows += [
            [InlineKeyboardButton(text="🔴 خاموش‌کردنِ ارسالِ خودکار" if cfg["enabled"] else "🟢 روشن‌کردنِ ارسالِ خودکار",
                                  callback_data="cx:w:auto", style=ButtonStyle.DANGER if cfg["enabled"] else ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="🔄 به‌روزرسانیِ موجودی", callback_data="cx:w", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text=f"🛡 سقفِ TRX ({_g(cfg['cap_trx'])})", callback_data="cx:w:cap:trx"),
             InlineKeyboardButton(text=f"🛡 سقفِ USDT ({_g(cfg['cap_usdt'])})", callback_data="cx:w:cap:usdt")],
            [InlineKeyboardButton(text="🔑 کلیدِ TronGrid (اختیاری)", callback_data="cx:w:apikey")],
            [InlineKeyboardButton(text="🗝 نمایشِ کلیدِ خصوصی (پشتیبان)", callback_data="cx:w:key")],
        ]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cx:adm", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@crypto_shop_router.callback_query(F.data == "cx:w")
async def cx_wallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await wallet_view(row.id)
    await _edit(cb, text, kb)
    await cb.answer()


@crypto_shop_router.callback_query(F.data == "cx:w:new")
async def cx_wallet_new(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    from tenant.bots.virtual_service import tron_autosend as TA
    row = await S._owner(cb, bot)
    if not row:
        return
    ok, info = await TA.create_wallet(row.id)
    if not ok:
        return await cb.answer(info[:190], show_alert=True)
    await cb.answer("✅ کیف‌پول ساخته شد")
    await cb.message.answer(
        "✅ <b>کیف‌پولِ ترون ساخته شد</b>\n\n"
        f"آدرس: <code>{_h.escape(info)}</code>\n\n"
        "حتماً از «🗝 نمایشِ کلیدِ خصوصی» یک نسخهٔ پشتیبان (جایِ امن، آفلاین) نگه دار؛ اگر ربات پاک شود کلید هم پاک می‌شود و پولِ داخلِ آن از دست می‌رود.",
        parse_mode="HTML")
    text, kb = await wallet_view(row.id)
    await _edit(cb, text, kb)


@crypto_shop_router.callback_query(F.data == "cx:w:auto")
async def cx_wallet_auto(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    from tenant.bots.virtual_service import tron_autosend as TA
    row = await S._owner(cb, bot)
    if not row:
        return
    if not await TA.wallet_address(row.id):
        return await cb.answer("اول کیف‌پول را بساز.", show_alert=True)
    cur = (await TA.settings(row.id))["enabled"]
    await _ss(row.id, "cx_tron_auto", "0" if cur else "1")
    await cb.answer("خاموش شد" if cur else "ارسالِ خودکار روشن شد")
    text, kb = await wallet_view(row.id)
    await _edit(cb, text, kb)


@crypto_shop_router.callback_query(F.data == "cx:w:key")
async def cx_wallet_key(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm as S
    from tenant.bots.virtual_service import tron_autosend as TA
    row = await S._owner(cb, bot)
    if not row or row.owner_id != cb.from_user.id:      # فقط مالکِ اصلی (نه ادمینِ فرعی)
        if row:
            await cb.answer("⛔️ فقط مالکِ ربات می‌تواند کلید را ببیند.", show_alert=True)
        return
    key = await TA.reveal_private_key(row.id)
    if not key:
        return await cb.answer("کلید خوانده نشد.", show_alert=True)
    await cb.answer()
    m = await cb.message.answer(
        "🗝 <b>کلیدِ خصوصیِ کیف‌پولِ ترون</b>\n\n<code>" + key + "</code>\n\n"
        "⚠️ این کلید به‌تنهایی به همهٔ پولِ کیف‌پول دسترسی می‌دهد. جایِ امن و آفلاین نگه دار و به هیچ‌کس نده. "
        "این پیام بعد از ۶۰ ثانیه خودکار پاک می‌شود.", parse_mode="HTML")
    import asyncio

    async def _gone():
        await asyncio.sleep(60)
        try:
            await m.delete()
        except Exception:  # noqa: BLE001
            logger.info("cx: deleting the key message failed")
    asyncio.create_task(_gone())


_W_FIELDS = {"cap:trx": ("cx_tron_cap_trx", "سقفِ ارسالِ خودکارِ هر سفارشِ TRX (عدد)"),
             "cap:usdt": ("cx_tron_cap_usdt", "سقفِ ارسالِ خودکارِ هر سفارشِ USDT (عدد)"),
             "apikey": ("cx_tron_apikey", "کلیدِ API از trongrid.io (برایِ نرخِ بالاتر؛ «-» = حذف)")}


@crypto_shop_router.callback_query(F.data.regexp(r"^cx:w:(cap:(trx|usdt)|apikey)$"))
async def cx_wallet_field(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service import smm as S
    row = await S._owner(cb, bot)
    if not row:
        return
    field = cb.data[len("cx:w:"):]
    await state.set_state(CxFSM.adm_val)
    await state.update_data(cx_adm_coin="wallet", cx_adm_field=field)
    await cb.message.answer(f"✏️ {_W_FIELDS[field][1]} را بفرست (برایِ انصراف /cancel):")
    await cb.answer()


async def wallet_field_save(msg: Message, state: FSMContext, row, field: str) -> None:
    key, _label = _W_FIELDS[field]
    raw = (msg.text or "").strip()
    if field == "apikey":
        val = "" if raw == "-" else raw
        if val and not re.fullmatch(r"[0-9a-fA-F-]{16,64}", val):
            await msg.answer("❌ کلیدِ نامعتبر. (فقط حروفِ hex و خط‌تیره؛ یا «-» برایِ حذف)")
            return
    else:
        t = raw.translate(_DIG).replace(",", "").replace("٬", "").replace("،", "")
        try:
            num = float(t)
        except ValueError:
            await msg.answer("❌ فقط عدد بفرست (یا /cancel):")
            return
        if not 0 <= num <= 1e7:
            await msg.answer("❌ عدد باید بینِ ۰ و ۱۰٬۰۰۰٬۰۰۰ باشد:")
            return
        val = _g(num)
    await _ss(row.id, key, val)
    await state.clear()
    text, kb = await wallet_view(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")
