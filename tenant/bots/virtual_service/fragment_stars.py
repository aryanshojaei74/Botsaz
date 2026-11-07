"""
tenant/fragment_stars.py — اتصال به «Fragment» (سرویسِ غیررسمیِ خریدِ خودکارِ
استارزِ تلگرام) با یک کیف‌پولِ TONِ اختصاصی که خودِ سیستم می‌سازد.

=== PATCH_FRAGMENT_WALLET_V1 ===
تصمیمِ عمدی: به‌جایِ گرفتنِ seedِ کیف‌پولِ شخصیِ مالکِ ربات (که یعنی دسترسیِ
کاملِ سرورِ غیررسمیِ Fragment به کل دارایی‌اش)، خودِ سیستم یک کیف‌پولِ تازه و
جدا برایِ همین‌کار می‌سازد. مالکِ ربات فقط موجودیِ لازم را به این آدرس واریز
می‌کند — یعنی بدترین حالتِ نشتِ این کلید، فقط همین موجودیِ محدود است، نه
دارایی‌هایِ دیگرِ او.

فازِ ۱ (همین فایل): ساختِ کیف‌پول + نمایشِ آدرس + بررسیِ موجودی.
فازِ ۲ (بعداً): خودِ فراخوانیِ Fragment API برایِ خریدِ استارز و فلویِ سفارش.
"""
from __future__ import annotations

import datetime as _dt
import json as _json

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sqlalchemy import select

from config import Session
from config import TONCENTER_API_KEY  # === MARKER_TONCENTER_RATELIMIT_V1 ===
from models import Bot as BotRow, FragmentWallet

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

fragment_router = Router(name="fragment")

TONCENTER_BASE = "https://toncenter.com/api/v2"
_TIMEOUT = 12
_REQUIRED_COOKIE_KEYS = ("stel_ssid", "stel_dt", "stel_token", "stel_ton_token")


# === PATCH_FRAGMENT_PURCHASE_V1 ===
class FragmentFSM(StatesGroup):
    cookies = State()
    tonapi_key = State()
    test_username = State()
    test_amount = State()
    # === PATCH_FRAGMENT_WITHDRAW_V1 ===
    wd_address = State()
    wd_amount = State()


async def _owner(cb: CallbackQuery, bot: Bot):
    """همان الگویِ چکِ مجوزِ tenant/smm.py:_owner — عمداً این‌جا هم تکرار شده
    تا این ماژول به داخلیاتِ smm.py وابسته نباشد (طبقِ قاعدهٔ پروژه، ماژول‌هایِ
    تننت نباید به‌هم قفل بشوند)."""
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        return None
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("⛔️ فقط ادمین به این بخش دسترسی دارد.", show_alert=True)
        return None
    return row


def _friendly_address(wallet_contract) -> str:
    """آدرسِ non-bounceable و قابلِ‌نمایش به کاربر (برایِ واریز)."""
    return wallet_contract.address.to_string(True, True, False)


async def _create_wallet(bot_id: int) -> FragmentWallet:
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    from wallet_crypto import encrypt_secret

    # v4r2 چون مستنداتِ Fragment API دقیقاً همین نسخه را برایِ حالتِ بدونِ
    # KYC می‌خواهد.
    mnemonics, _pub, _priv, wallet = Wallets.create(WalletVersionEnum.v4r2, workchain=0)
    address = _friendly_address(wallet)
    encrypted = encrypt_secret(" ".join(mnemonics))

    # === PATCH_FRAGMENT_WALLET_RACE_V1 ===
    # لاگ: «duplicate key ... fragment_wallets_bot_id_key» — دو درخواستِ هم‌زمان برایِ یک ربات هر دو کیفِ تازه می‌ساختند.
    # برنده‌نشده کیفِ برنده را برمی‌گرداند (کیفِ دومِ ساخته‌شده دور ریخته می‌شود؛ پولی در آن نیست).
    from sqlalchemy.exc import IntegrityError as _IE
    async with Session() as s:
        row = FragmentWallet(bot_id=bot_id, address=address, wallet_version="v4r2",
                             encrypted_mnemonic=encrypted)
        s.add(row)
        try:
            await s.commit()
        except _IE:
            await s.rollback()
            existing = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == bot_id))
            if existing is None:
                raise
            return existing
        await s.refresh(row)
        return row



# === MARKER_TONCENTER_RATELIMIT_V1 ===
def _toncenter_headers() -> dict:
    return {"X-Api-Key": TONCENTER_API_KEY} if TONCENTER_API_KEY else {}


async def _toncenter_call(method: str, url: str, **kwargs):
    """یک درخواست به toncenter با ۳ بار تلاشِ خودکار وقتی جواب
    «Ratelimit» یا HTTP 429 باشد (backoff: ۱.۵، ۳، ۶ ثانیه).
    خروجی: (data_dict_or_None, last_error_str)."""
    import asyncio
    delays = (1.5, 3.0, 6.0)
    last_err = ""
    for attempt, delay in enumerate((0.0,) + delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            t = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.request(method, url, headers=_toncenter_headers(), **kwargs) as r:
                    status = r.status
                    data = await r.json(content_type=None)
        except Exception as e:                               # noqa: BLE001
            last_err = f"{type(e).__name__}"
            continue
        is_ratelimited = status == 429 or (
            isinstance(data, dict) and "ratelimit" in str(data.get("error") or "").lower())
        if is_ratelimited:
            last_err = "Ratelimit exceed"
            continue
        return data, ""
    return None, last_err or "Ratelimit exceed"


async def fetch_balance_ton(address: str) -> float | None:
    """موجودیِ زنده از toncenter (به TON). خطا → None (نه صفر، تا با
    «واقعاً صفره» قاطی نشود). MARKER_TONCENTER_RATELIMIT_V1: با retry رویِ ریت‌لیمیت."""
    if aiohttp is None:
        return None
    url = f"{TONCENTER_BASE}/getAddressBalance"
    data, err = await _toncenter_call("GET", url, params={"address": address})
    if data is None:
        logger.warning("fragment_stars: toncenter پاسخ نداد (%s): %s", address, err)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int(data.get("result") or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None


def _detail_kb(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بررسیِ موجودی", callback_data="smm:fragment:refresh")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)],
    ])


async def _detail_text_kb(wallet: FragmentWallet) -> tuple[str, InlineKeyboardMarkup]:
    # === PATCH_FRAGMENT_AUTO_STARS_V1 === وضعیتِ «فروشِ خودکار»
    try:
        from tenant import get_setting as _gs
        _auto_on = (await _gs(wallet.bot_id, "fragment_auto", "0")) == "1"
    except Exception:
        logger.exception("fragment: خواندنِ وضعیتِ فروشِ خودکار ناموفق بود")
        _auto_on = False
    checked = ("هنوز بررسی نشده" if not wallet.last_balance_checked_at else
               wallet.last_balance_checked_at.strftime("%Y-%m-%d %H:%M UTC"))
    # === PATCH_FRAGMENT_PURCHASE_V1 ===
    configured = bool(wallet.encrypted_fragment_cookies and wallet.encrypted_tonapi_key)
    conn_line = ("🟢 وصل شده — خریدِ استارز آماده‌ست" if configured
                 else "🔴 هنوز وصل نشده — کوکی/کلید لازمه")
    text = (
        "⭐️ <b>استارزِ خودکار — فرگمنت</b>\n\n"
        "این یک کیف‌پولِ TONِ <b>جدا و اختصاصیِ</b> همین ربات است — نه کیف‌پولِ "
        "شخصیِ تو. فقط همین‌جا موجودی نگه‌دار، نه بیشتر از نیازِ گردشِ فروش.\n\n"
        f"📥 آدرسِ واریز:\n<code>{wallet.address}</code>\n\n"
        f"💰 آخرین موجودیِ ثبت‌شده: <b>{float(wallet.last_balance_ton):.4f} TON</b>\n"
        f"🕒 آخرین بررسی: {checked}\n\n"
        f"🔗 وضعیتِ اتصال: {conn_line}"
        + ("\n\n🤖 <b>فروشِ خودکار روشن است:</b> هر سفارشِ استارزِ پرداخت‌شده (۵۰ تا ۵٬۰۰۰ ⭐، با یوزرنیمِ مقصد) "
           "همان لحظه با TONِ همین ولت خریده و ارسال می‌شود. هر مشکل/ابهامی ⇒ سفارش به تحویلِ دستی برمی‌گردد و "
           "به تو خبر می‌دهد." if _auto_on else "")
    )
    rows = [
        [InlineKeyboardButton(text="📋 کپیِ آدرس", copy_text=CopyTextButton(text=wallet.address))],
        [InlineKeyboardButton(text="🔄 بررسیِ موجودی", callback_data="smm:fragment:refresh")],
        [InlineKeyboardButton(
            text=("🔧 ویرایشِ اتصال (کوکی/کلید)" if configured else "🔧 وصل‌کردن به Fragment"),
            callback_data="smm:fragment:connect", style=ButtonStyle.SUCCESS)],
    ]
    if configured:
        rows.append([InlineKeyboardButton(text="🧪 تستِ خریدِ استارز", callback_data="smm:fragment:testbuy",
                                          style=ButtonStyle.PRIMARY)])
        # === PATCH_FRAGMENT_AUTO_STARS_V1 ===
        rows.append([InlineKeyboardButton(
            text=("🤖 فروشِ خودکار: ✅ روشن (برایِ خاموش‌کردن بزن)" if _auto_on
                  else "🤖 فروشِ خودکار: ⛔ خاموش (برایِ روشن‌کردن بزن)"),
            callback_data="smm:fragment:autotoggle",
            style=ButtonStyle.DANGER if _auto_on else ButtonStyle.SUCCESS)])
    # === PATCH_FRAGMENT_WITHDRAW_V1 ===
    # راهِ خروجِ پول. مستقل از وضعیتِ اتصال نشان داده می‌شود، چون دقیقاً
    # همان کاربری که هنوز وصل نشده ممکن است بخواهد پولش را پس بگیرد.
    rows.append([InlineKeyboardButton(text="💸 برداشتِ TON", callback_data="smm:fragment:wd",
                                      style=ButtonStyle.DANGER)])
    # === PATCH_FRAGMENT_HELP_V1 ===
    rows.append([InlineKeyboardButton(text="📘 راهنمایِ کاملِ راه‌اندازی",
                                      callback_data="smm:fragment:help",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@fragment_router.callback_query(F.data == "smm:fragment:menu")
async def fragment_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    await cb.answer()
    if wallet:
        text, kb = await _detail_text_kb(wallet)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            await cb.message.answer(text, reply_markup=kb)
        return
    text = (
        "🔗 <b>اتصال به فرگمنت (استارزِ خودکار)</b>\n\n"
        "⚠️ این یک سرویسِ <b>غیررسمی</b> است (نه از تلگرام، نه از Fragment.com) — "
        "برایِ خریدِ خودکار، کلیدِ یک کیف‌پولِ TON لازم است.\n\n"
        "برایِ ایمنی، به‌جایِ گرفتنِ کیف‌پولِ شخصیِ تو، یک کیف‌پولِ <b>تازه و "
        "جدا</b> مخصوصِ همین ربات می‌سازیم؛ فقط همون مقداری که لازم داری بهش "
        "واریز کن، نه بیشتر.\n\n"
        "ادامه بدیم؟"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، کیف‌پول بساز", callback_data="smm:fragment:create",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data="smm:panels", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@fragment_router.callback_query(F.data == "smm:fragment:create")
async def fragment_create(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        existing = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    if existing:
        await cb.answer("قبلاً ساخته شده.")
    else:
        await cb.answer("⏳ در حالِ ساختنِ کیف‌پول…")
        try:
            existing = await _create_wallet(row.id)
        except Exception:
            logger.exception("fragment_stars: ساختِ کیف‌پول ناموفق")
            try:
                await cb.message.answer(
                    "⛔️ ساختِ کیف‌پول ناموفق بود — اگه ادامه داشت به پشتیبانیِ بات‌ساز بگو.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده")
            return
    text, kb = await _detail_text_kb(existing)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@fragment_router.callback_query(F.data == "smm:fragment:autotoggle")
async def fragment_auto_toggle(cb: CallbackQuery, bot: Bot):
    """PATCH_FRAGMENT_AUTO_STARS_V1 — روشن/خاموش‌کردنِ فروشِ خودکارِ استارز با ولتِ فرگمنت (فقط مالک)."""
    row = await _owner_only_strict(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    if not wallet or not (wallet.encrypted_fragment_cookies and wallet.encrypted_tonapi_key):
        await cb.answer("اول باید ولت کامل وصل شده باشد.", show_alert=True)
        return
    cur = (await get_setting(row.id, "fragment_auto", "0")) == "1"
    await set_setting(row.id, "fragment_auto", "0" if cur else "1")
    await cb.answer("⛔ فروشِ خودکار خاموش شد" if cur else "✅ فروشِ خودکار روشن شد")
    text, kb = await _detail_text_kb(wallet)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:   # noqa: BLE001
        if "not modified" not in str(e).lower():
            logger.exception("fragment: به‌روزرسانیِ صفحه ممکن نشد")


@fragment_router.callback_query(F.data == "smm:fragment:refresh")
async def fragment_refresh(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    if not wallet:
        await cb.answer("هنوز کیف‌پولی ساخته نشده.", show_alert=True)
        return
    await cb.answer("⏳ در حالِ گرفتنِ موجودیِ زنده…")
    bal = await fetch_balance_ton(wallet.address)
    if bal is not None:
        async with Session() as s:
            w2 = await s.get(FragmentWallet, wallet.id)
            if w2:
                w2.last_balance_ton = bal
                w2.last_balance_checked_at = _dt.datetime.utcnow()
                await s.commit()
                wallet = w2
    else:
        try:
            await cb.message.answer("⚠️ گرفتنِ موجودیِ زنده ناموفق بود؛ عددِ قبلی نمایش داده می‌شود.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده")
    text, kb = await _detail_text_kb(wallet)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


# ═══════════════════ PATCH_FRAGMENT_PURCHASE_V1 ═══════════════════
# فازِ ۲: اتصالِ واقعی به Fragment (از طریقِ pyfragment، مستقیم با کوکیِ
# سشنِ خودِ اکانتِ مالک — نه یک سرویسِ واسطِ ناشناس) و خریدِ آزمایشیِ استارز.

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ لغو", callback_data="smm:fragment:menu", style=ButtonStyle.DANGER)],
    ])


@fragment_router.callback_query(F.data == "smm:fragment:connect")
async def fragment_connect_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(FragmentFSM.cookies)
    await cb.answer()
    text = (
        "🔗 <b>وصل‌کردن به Fragment</b>\n\n"
        "این کوکی‌ها از اکانتِ <b>خودِ تو</b> رو Fragment.com می‌گیریم (نه هیچ "
        "سرویسِ واسطی) تا خرید مستقیماً از طریقِ اکانتِ خودت انجام بشه.\n\n"
        "۱. برو <code>fragment.com</code>، با اکانتِ تلگرامت لاگین کن و کیف‌پولِ "
        "TONـت رو وصل کن.\n"
        "۲. با افزونه‌ی مرورگرِ <b>Cookie-Editor</b>، این چهار مقدار رو پیدا کن: "
        "<code>stel_ssid</code>، <code>stel_dt</code>، <code>stel_token</code>، "
        "<code>stel_ton_token</code>\n"
        "۳. به یکی از این دو شکل بفرست:\n"
        "   • یه JSON: <code>{\"stel_ssid\":\"...\",\"stel_dt\":\"...\",\"stel_token\":\"...\",\"stel_ton_token\":\"...\"}</code>\n"
        "   • یا چهار خط جدا، هرکدوم به‌شکلِ <code>کلید=مقدار</code>\n\n"
        "⚠️ این کوکی‌ها دسترسیِ کامل به اکانتِ Fragmentِ تو می‌دن — فقط اینجا "
        "(تویِ همین چتِ خصوصیِ خودت با ربات) بفرستشون."
    )
    try:
        await cb.message.edit_text(text, reply_markup=_cancel_kb())
    except Exception:
        await cb.message.answer(text, reply_markup=_cancel_kb())


# === PATCH_FRAGMENT_FSM_MENU_BOUNCE_V1 ===
# اگه به‌جایِ واقعاً واردکردنِ کوکی/کلید، کاربر رویِ یه دکمهٔ منو زده باشه
# (که به‌شکلِ یه پیامِ متنیِ ساده می‌رسه و همین‌جا به‌عنوانِ ورودیِ نامعتبر
# قاپیده می‌شد)، این تابع تشخیص میده، استیت رو پاک می‌کنه، و پیام رو به
# هندلرِ اصلیِ دکمه‌هایِ تنانت می‌سپاره — به‌جایِ اینکه بی‌نهایت خطایِ
# «فرمت درست نبود» نشون بده و کاربر تویِ این استیت گیر بیفته.
async def _bounce_if_menu_button(msg: Message, bot: Bot, state: FSMContext,
                                 real_input_hint: bool) -> bool:
    """real_input_hint=True یعنی متن واقعاً به فرمتِ ورودیِ موردِ انتظار
    (کوکی/کلید) شبیهه، پس حتی اگه تصادفاً اسمِ یه دکمه هم باشه (خیلی
    بعیده)، بازم بذار همون خطایِ فرمتِ رایج نشون داده بشه."""
    if real_input_hint:
        return False
    raw = (msg.text or "").strip()
    if not raw:
        return False
    from tenant import get_bot_row, tenant_buttons
    row = await get_bot_row(bot)
    if not row:
        return False
    await state.clear()
    await tenant_buttons(msg, bot, state)
    return True


def _parse_cookies(raw: str) -> dict[str, str] | None:
    raw = raw.strip()
    parsed: dict[str, str] = {}
    try:
        data = _json.loads(raw)
        if isinstance(data, dict):
            parsed = {str(k): str(v) for k, v in data.items()}
    except Exception:
        for line in raw.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                parsed[k.strip()] = v.strip()
    if not all(k in parsed and parsed[k] for k in _REQUIRED_COOKIE_KEYS):
        return None
    return {k: parsed[k] for k in _REQUIRED_COOKIE_KEYS}


@fragment_router.message(FragmentFSM.cookies, F.text)
async def fragment_connect_cookies(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    raw = msg.text or ""
    # فرمتِ واقعیِ کوکی همیشه یا JSON هست («{») یا خط‌های کلید=مقدار
    # («=»)؛ اگه پیام هیچ‌کدوم رو نداره، به‌جایِ کوکی، احتمالاً کاربر
    # رویِ یه دکمهٔ منو زده (که این استیت داشت به‌اشتباه به‌عنوانِ
    # کوکیِ نامعتبر قاپیدش) — بذار به منو برگرده.
    if await _bounce_if_menu_button(msg, bot, state, real_input_hint=("=" in raw or "{" in raw)):
        return
    cookies = _parse_cookies(raw)
    if not cookies:
        await msg.answer(
            "❌ فرمت درست نبود. هر چهار مقدار (stel_ssid, stel_dt, stel_token, "
            "stel_ton_token) لازمه. دوباره بفرست:", reply_markup=_cancel_kb())
        return
    await state.update_data(fg_cookies=cookies)
    await state.set_state(FragmentFSM.tonapi_key)
    await msg.answer(
        "✅ کوکی‌ها دریافت شد.\n\n"
        "حالا کلیدِ <b>tonapi.io</b> رو بفرست (از <code>tonconsole.com</code> "
        "می‌گیری — رایگانه):", reply_markup=_cancel_kb())


@fragment_router.message(FragmentFSM.tonapi_key, F.text)
async def fragment_connect_tonapi(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    from wallet_crypto import encrypt_secret
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    key = (msg.text or "").strip()
    # کلیدهایِ tonapi.io همیشه رشته‌هایِ لاتینِ بدونِ فاصله‌ان؛ اگه متن
    # فارسی/دارایِ فاصله باشه (مشخصهٔ دکمه‌هایِ منو)، یعنی کاربر داره
    # ناوبری می‌کنه، نه کلید می‌فرسته.
    looks_like_key = bool(key) and " " not in key and not any(ord(c) > 127 for c in key)
    if await _bounce_if_menu_button(msg, bot, state, real_input_hint=looks_like_key):
        return
    if len(key) < 10:
        await msg.answer("❌ این کلید معتبر به‌نظر نمی‌رسه. دوباره بفرست:", reply_markup=_cancel_kb())
        return
    data = await state.get_data()
    cookies = data.get("fg_cookies")
    await state.clear()
    if not cookies:
        await msg.answer("⚠️ نشستِ ثبت منقضی شد؛ از اول شروع کن.")
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
        if not wallet:
            await msg.answer("⚠️ اول باید کیف‌پول ساخته شده باشه.")
            return
        wallet.encrypted_fragment_cookies = encrypt_secret(_json.dumps(cookies))
        wallet.encrypted_tonapi_key = encrypt_secret(key)
        await s.commit()
    await msg.answer("✅ اتصال ثبت شد! حالا می‌تونی از «🧪 تستِ خریدِ استارز» استفاده کنی.")
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    text, kb = await _detail_text_kb(wallet)
    await msg.answer(text, reply_markup=kb)


# === PATCH_FRAGMENT_HINTS_V1 === (گزارشِ مشتری: «هش و کوکی‌ها را بلاک می‌کند و خرید انجام نمی‌شود»)
# pyfragment «هش» را خودش از صفحهٔ fragment.com (با همان کوکی‌ها) می‌خواند؛ مالک هیچ هشی وارد نمی‌کند. اگر کوکی‌ها لاگین/معتبر
# نباشند یا فرگمنت درخواستِ سرور را رد کند، خطایِ «نشد هش را بخوانم» برمی‌گردد. این تابع علتِ محتمل و راهِ حل را فارسی می‌گوید.
def fragment_hint(e: Exception) -> str | None:
    try:
        import pyfragment as _pf
    except ImportError:
        return "کتابخانهٔ pyfragment روی سرور نصب نیست."
    table = (
        (_pf.CookieError, "کوکی‌ها ناقص یا منقضی‌اند. باید هر چهار کوکیِ stel_ssid / stel_dt / stel_token / stel_ton_token "
                          "را از مرورگری که <b>همین حالا</b> در fragment.com لاگین است (و کیف‌پولِ TON وصل کرده) بگیری."),
        (_pf.FragmentPageError, "فرگمنت صفحه/«هش» را به ربات نداد. دو دلیلِ رایج: ۱) کوکی‌ها منقضی شده یا لاگین نیستند — "
                                "دوباره در مرورگر لاگین کن، بلافاصله کوکیِ تازه را در «ویرایشِ اتصال» بفرست و در مرورگر «خروج» نزن "
                                "(خروج کوکی را باطل می‌کند)؛ ۲) فرگمنت موقتاً درخواستِ سرور را محدود کرده — چند دقیقه صبر کن و دوباره امتحان کن."),
        (_pf.UserNotFoundError, "فرگمنت این یوزرنیم را پیدا نکرد (اشتباه یا غیرقابلِ دریافت)."),
        (_pf.VerificationError, "فرگمنت برایِ این حساب احرازِ هویت (KYC) می‌خواهد؛ باید در خودِ fragment.com تکمیل شود."),
        (_pf.WalletError, "ولتِ TONِ ربات مشکل دارد (معمولاً موجودی کافی نیست)؛ شارژش کن."),
        (_pf.TransactionError, "ساخت/ارسالِ تراکنش ناموفق بود؛ قبل از تلاشِ دوباره موجودی و تراکنش‌هایِ ولت را ببین."),
        (_pf.ConfigurationError, "تنظیماتِ اتصال (کوکی/کلیدِ TonAPI) ناقص است؛ دوباره وصل کن."),
        (_pf.FragmentAPIError, "فرگمنت درخواست را نپذیرفت (معمولاً هشِ کهنه یا نشستِ منقضی)؛ کوکیِ تازه بده و دوباره امتحان کن."),
    )
    for cls, msg in table:
        if isinstance(e, cls):
            return msg
    return None


async def _get_fragment_client(wallet: FragmentWallet):
    """کلاینتِ pyfragment رو با اعتبارنامه‌هایِ رمزگشایی‌شده می‌سازه.
    خطای import یعنی هنوز رو سرور نصب نشده (pip install pyfragment)."""
    from pyfragment import FragmentClient
    from wallet_crypto import decrypt_secret
    mnemonic = decrypt_secret(wallet.encrypted_mnemonic)
    cookies = _json.loads(decrypt_secret(wallet.encrypted_fragment_cookies))
    tonapi_key = decrypt_secret(wallet.encrypted_tonapi_key)
    return FragmentClient(seed=mnemonic, api_key=tonapi_key, cookies=cookies)


@fragment_router.callback_query(F.data == "smm:fragment:testbuy")
async def fragment_testbuy_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    if not wallet or not wallet.encrypted_fragment_cookies:
        await cb.answer("اول باید وصل کنی.", show_alert=True)
        return
    await state.set_state(FragmentFSM.test_username)
    await cb.answer()
    await cb.message.answer(
        "🧪 <b>تستِ خریدِ استارز</b>\n\nیوزرنیمِ گیرنده رو بفرست (بدونِ @، مثلاً "
        "<code>durov</code>):", reply_markup=_cancel_kb())


@fragment_router.message(FragmentFSM.test_username, F.text)
async def fragment_testbuy_username(msg: Message, bot: Bot, state: FSMContext):
    uname = (msg.text or "").strip().lstrip("@")
    if not uname:
        await msg.answer("❌ یوزرنیمِ معتبر بفرست:", reply_markup=_cancel_kb())
        return
    await state.update_data(fg_test_username=uname)
    await state.set_state(FragmentFSM.test_amount)
    await msg.answer("چند تا استارز؟ (حداقل ۵۰):", reply_markup=_cancel_kb())


@fragment_router.message(FragmentFSM.test_amount, F.text)
async def fragment_testbuy_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    raw = (msg.text or "").strip()
    if not raw.isdigit() or int(raw) < 50:
        await msg.answer("❌ عددی حداقل ۵۰ بفرست:", reply_markup=_cancel_kb())
        return
    amount = int(raw)
    data = await state.get_data()
    uname = data.get("fg_test_username")
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not uname:
        await msg.answer("⚠️ نشست منقضی شد؛ از اول شروع کن.")
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == row.id))
    if not wallet:
        await msg.answer("⚠️ کیف‌پول پیدا نشد.")
        return
    wait_msg = await msg.answer("⏳ در حالِ ارسالِ درخواستِ خرید به Fragment…")
    try:
        client = await _get_fragment_client(wallet)
        async with client:
            result = await client.purchase_stars(uname, amount=amount)
        await wait_msg.edit_text(
            f"✅ ارسال شد!\n\nگیرنده: @{result.username}\nتعداد: {result.amount} ⭐️\n"
            f"شناسه‌ی تراکنش: <code>{result.transaction_id}</code>")
    except ImportError:
        await wait_msg.edit_text(
            "⛔️ کتابخونه‌ی <code>pyfragment</code> هنوز رو سرور نصب نشده. "
            "ادمینِ فنی باید <code>pip install pyfragment</code> رو بزنه.")
    except Exception as e:
        logger.exception("fragment_stars: خرید ناموفق")
        hint = fragment_hint(e)
        await wait_msg.edit_text(
            f"❌ خرید ناموفق بود: {type(e).__name__}: {str(e)[:300]}\n\n"
            + (f"🔎 <b>علت:</b> {hint}" if hint else
               "دلایلِ محتمل: کوکی‌ها منقضی شدن (دوباره وصل کن)، موجودیِ کیف‌پول "
               "کافی نیست، یا یوزرنیم/شرایطِ گیرنده مشکل داره."))


# ═══════════════════ برداشتِ TON از کیف‌پولِ ربات ═══════════════════
# === PATCH_FRAGMENT_WITHDRAW_V1 ===
# گزارشِ پشتیبانی: کاربری به این آدرس TON واریز کرده بود و بعد می‌خواست
# پسش بگیرد — ولی هیچ راهی نبود. seedِ این کیف‌پول را خودِ سیستم می‌سازد و
# رمزنگاری‌شده نگه می‌دارد و هیچ‌وقت به مالک نشان نمی‌دهد، و تا امروز هیچ
# تابعِ انتقالی هم وجود نداشت؛ یعنی پول عملاً فقط برایِ خریدِ استارز
# قابلِ‌استفاده بود. این بخش همان راهِ خروج را اضافه می‌کند.
#
# تصمیم‌هایِ عمدی:
#   • فقط «مالکِ ربات» — نه ادمینِ فرعی. برداشتِ پول از ادمینِ فرعی
#     ریسکِ بی‌موردی است و _owner_only_strict همین را تضمین می‌کند.
#   • «همه» با send_mode=128 فرستاده می‌شود (کلِ موجودی منهایِ کارمزد،
#     محاسبه‌اش با خودِ شبکه) تا ما مجبور نباشیم کارمزد را حدس بزنیم و
#     تراکنش سرِ چند هزارمِ TON رد شود.
#   • مبلغِ مشخص با send_mode=3 و رزروِ _FEE_RESERVE_TON برایِ کارمزد.
#   • قبل از ارسال، یک صفحهٔ تاییدِ جدا با آدرس و مبلغ نشان داده می‌شود.

_FEE_RESERVE_TON = 0.05      # برایِ کارمزدِ شبکه کنار گذاشته می‌شود
_MIN_WITHDRAW_TON = 0.01


async def _owner_only_strict(cb: CallbackQuery, bot: Bot):
    """برخلافِ _owner (که ادمینِ فرعی را هم قبول می‌کند)، برداشتِ پول فقط
    برایِ خودِ مالکِ ربات مجاز است."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    if int(row.owner_id) != int(cb.from_user.id):
        await cb.answer("فقط مالکِ ربات می‌تواند برداشت کند.", show_alert=True)
        return None
    return row


async def _owner_only_strict_msg(msg: Message, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or int(row.owner_id) != int(msg.from_user.id):
        return None
    return row


def _valid_ton_address(addr: str) -> bool:
    try:
        from tonsdk.utils import Address
        Address(addr)
        return True
    except Exception:                                        # noqa: BLE001
        return False


async def _get_seqno(address: str) -> int | None:
    """seqno فعلیِ کیف‌پول — بدونِ آن نمی‌شود تراکنشِ معتبر ساخت.
    MARKER_TONCENTER_RATELIMIT_V1: با retry رویِ ریت‌لیمیت."""
    if aiohttp is None:
        return None
    data, err = await _toncenter_call(
        "GET", f"{TONCENTER_BASE}/getWalletInformation", params={"address": address})
    if data is None:
        logger.warning("fragment_withdraw: getWalletInformation نشد: %s", err)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int((data.get("result") or {}).get("seqno") or 0)
    except (TypeError, ValueError):
        return None


async def _send_boc(boc_b64: str) -> tuple[bool, str]:
    """MARKER_TONCENTER_RATELIMIT_V1: با retry رویِ ریت‌لیمیت — قبلاً یک ریت‌لیمیتِ گذرا کلِ
    برداشت را شکست می‌داد."""
    if aiohttp is None:
        return False, "aiohttp نصب نیست."
    data, err = await _toncenter_call(
        "POST", f"{TONCENTER_BASE}/sendBoc", json={"boc": boc_b64})
    if data is None:
        return False, f"ارسال به شبکه نشد: {err}"
    if isinstance(data, dict) and data.get("ok"):
        return True, "ارسال شد."
    err2 = ""
    if isinstance(data, dict):
        err2 = str(data.get("error") or data.get("result") or "")[:200]
    return False, err2 or "شبکه تراکنش را نپذیرفت."


async def do_withdraw(bot_id: int, to_addr: str, amount_ton: float | None
                      ) -> tuple[bool, str]:
    """انتقالِ TON از کیف‌پولِ ربات. amount_ton=None یعنی «همهٔ موجودی».

    هیچ‌وقت استثنا به بیرون نمی‌دهد؛ همیشه (ok, پیام) برمی‌گرداند.
    """
    import base64
    try:
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import to_nano
        from wallet_crypto import decrypt_secret
    except Exception as e:                                   # noqa: BLE001
        return False, f"کتابخانهٔ TON در دسترس نیست ({type(e).__name__})."

    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(
            FragmentWallet.bot_id == bot_id))
    if not wallet:
        return False, "کیف‌پولی برایِ این ربات ساخته نشده."

    bal = await fetch_balance_ton(wallet.address)
    if bal is None:
        return False, "موجودی از شبکه خوانده نشد. کمی بعد دوباره امتحان کن."
    if bal <= 0:
        return False, "موجودیِ کیف‌پول صفر است."

    send_all = amount_ton is None
    if not send_all:
        if amount_ton < _MIN_WITHDRAW_TON:
            return False, f"حداقلِ برداشت {_MIN_WITHDRAW_TON} TON است."
        if amount_ton > bal - _FEE_RESERVE_TON:
            return False, (f"موجودی کافی نیست. حداکثرِ قابلِ‌برداشت با احتسابِ "
                           f"کارمزد: {max(0.0, bal - _FEE_RESERVE_TON):.4f} TON "
                           f"(یا گزینهٔ «همه» را بزن).")

    try:
        mnemonic = decrypt_secret(wallet.encrypted_mnemonic)
    except Exception:                                        # noqa: BLE001
        logger.exception("fragment_withdraw: رمزگشاییِ seed نشد (bot_id=%s)", bot_id)
        return False, "کلیدِ کیف‌پول قابلِ‌بازگشایی نبود."

    seqno = await _get_seqno(wallet.address)
    if seqno is None:
        return False, "وضعیتِ کیف‌پول از شبکه خوانده نشد. کمی بعد دوباره امتحان کن."

    try:
        _m, _pub, _priv, wc = Wallets.from_mnemonics(
            mnemonic.split(), version=WalletVersionEnum.v4r2, workchain=0)
        # 128 = کلِ موجودی را بفرست (کارمزد را خودِ شبکه کم می‌کند)
        # 3  = مبلغِ مشخص، کارمزد از موجودیِ باقی‌مانده
        query = wc.create_transfer_message(
            to_addr=to_addr,
            amount=0 if send_all else to_nano(float(amount_ton), "ton"),
            seqno=seqno,
            send_mode=128 if send_all else 3)
        boc = base64.b64encode(query["message"].to_boc(False)).decode()
    except Exception as e:                                   # noqa: BLE001
        logger.exception("fragment_withdraw: ساختِ تراکنش نشد (bot_id=%s)", bot_id)
        return False, f"ساختِ تراکنش نشد ({type(e).__name__})."

    ok, note = await _send_boc(boc)
    if not ok:
        return False, note

    logger.info("fragment_withdraw: bot_id=%s → %s مبلغ=%s",
                bot_id, to_addr, "ALL" if send_all else amount_ton)
    return True, ("تراکنش به شبکه ارسال شد. معمولاً کمتر از یک دقیقه طول می‌کشد "
                  "تا در مقصد بنشیند.")


@fragment_router.callback_query(F.data == "smm:fragment:wd")
async def fragment_wd_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_only_strict(cb, bot)
    if not row:
        return
    async with Session() as s:
        wallet = await s.scalar(select(FragmentWallet).where(
            FragmentWallet.bot_id == row.id))
    if not wallet:
        await cb.answer("کیف‌پولی ساخته نشده.", show_alert=True)
        return
    bal = await fetch_balance_ton(wallet.address)
    await state.set_state(FragmentFSM.wd_address)
    await state.update_data(wd_bal=(bal if bal is not None else 0.0))
    await cb.answer()
    bal_line = (f"{bal:.4f} TON" if bal is not None
                else "خوانده نشد (شبکه جواب نداد)")
    text = (
        "💸 <b>برداشتِ TON</b>\n\n"
        f"موجودیِ فعلی: <b>{bal_line}</b>\n\n"
        "آدرسِ کیف‌پولِ مقصد را بفرست (آدرسِ TON).\n\n"
        "⚠️ آدرس را با دقت بررسی کن — تراکنشِ بلاک‌چین "
        "<b>برگشت‌پذیر نیست</b> و ارسال به آدرسِ اشتباه یعنی از دست رفتنِ پول."
    )
    from tenant import smart_cancel_kb
    try:
        await cb.message.edit_text(text, reply_markup=await smart_cancel_kb(bot))
    except Exception:                                        # noqa: BLE001
        await cb.message.answer(text, reply_markup=await smart_cancel_kb(bot))


@fragment_router.message(FragmentFSM.wd_address, F.text)
async def fragment_wd_address(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_only_strict_msg(msg, bot)
    if not row:
        await state.clear()
        return
    addr = (msg.text or "").strip()
    # === PATCH_FRAGMENT_WITHDRAW_V1 ===
    # همان محافظِ FSMِ این ماژول: اگر کاربر به‌جایِ آدرس رویِ یک دکمهٔ منو
    # زده باشد، نباید تویِ این استیت گیر بیفتد.
    if await _bounce_if_menu_button(msg, bot, state,
                                    real_input_hint=_valid_ton_address(addr)):
        return
    if not _valid_ton_address(addr):
        await msg.answer("این آدرسِ TON معتبر نیست. دوباره بفرست:")
        return
    data = await state.get_data()
    bal = float(data.get("wd_bal") or 0.0)
    await state.update_data(wd_addr=addr)
    await state.set_state(FragmentFSM.wd_amount)
    await msg.answer(
        f"✅ مقصد ثبت شد:\n<code>{addr}</code>\n\n"
        f"حالا مبلغ را به TON بفرست (مثلاً <code>1.5</code>).\n"
        f"برایِ برداشتِ کلِ موجودی، کلمهٔ <code>همه</code> را بفرست.\n\n"
        f"موجودی: <b>{bal:.4f} TON</b> — "
        f"در حالتِ مبلغِ مشخص، حدودِ {_FEE_RESERVE_TON} TON برایِ کارمزدِ شبکه کنار می‌ماند.")


@fragment_router.message(FragmentFSM.wd_amount, F.text)
async def fragment_wd_amount(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_only_strict_msg(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    _looks_amount = bool(raw) and (
        raw in ("همه", "همهٔ موجودی", "all", "max")
        or raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                                       "01234567890123456789")
                         ).replace(",", "").replace(".", "", 1).isdigit())
    if await _bounce_if_menu_button(msg, bot, state, real_input_hint=_looks_amount):
        return
    data = await state.get_data()
    addr = data.get("wd_addr") or ""
    if not addr:
        await state.clear()
        await msg.answer("جلسه منقضی شد. دوباره از منویِ کیف‌پول شروع کن.")
        return

    if raw in ("همه", "همهٔ موجودی", "all", "max"):
        amount = None
        amount_lbl = "کلِ موجودی"
    else:
        norm = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                                           "01234567890123456789")).replace(",", "")
        try:
            amount = float(norm)
        except ValueError:
            await msg.answer("عدد معتبر نیست. مبلغ را به TON بفرست یا کلمهٔ «همه» را:")
            return
        if amount < _MIN_WITHDRAW_TON:
            await msg.answer(f"حداقلِ برداشت {_MIN_WITHDRAW_TON} TON است. دوباره بفرست:")
            return
        amount_lbl = f"{amount:.4f} TON"

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ تایید و ارسال",
            callback_data=f"smm:fragment:wdgo:{'all' if amount is None else amount}",
            style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data="smm:fragment:menu",
                              style=ButtonStyle.DANGER)],
    ])
    # آدرس در متنِ همین پیامِ تایید می‌ماند تا در callback جا نشود
    # (callback_data سقفِ ۶۴ بایت دارد و آدرسِ TON خودش ۴۸ کاراکتر است).
    from tenant import set_setting
    await set_setting(row.id, "_fragment_wd_pending", addr)
    await msg.answer(
        "🔎 <b>تاییدِ نهاییِ برداشت</b>\n\n"
        f"مقصد:\n<code>{addr}</code>\n\n"
        f"مبلغ: <b>{amount_lbl}</b>\n\n"
        "⚠️ بعد از تایید، تراکنش رویِ بلاک‌چین ثبت می‌شود و "
        "<b>هیچ راهی برایِ برگرداندنش نیست</b>. آدرس را یک بارِ دیگر چک کن.",
        reply_markup=kb)


@fragment_router.callback_query(F.data.startswith("smm:fragment:wdgo:"))
async def fragment_wd_go(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_only_strict(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    addr = await get_setting(row.id, "_fragment_wd_pending", "")
    if not addr:
        await cb.answer("جلسه منقضی شد. دوباره شروع کن.", show_alert=True)
        return
    raw = cb.data.split(":", 3)[3]
    amount = None if raw == "all" else float(raw)

    await cb.answer("در حال ارسال…")
    try:
        await cb.message.edit_text("⏳ در حال ساخت و ارسالِ تراکنش…", reply_markup=None)
    except Exception:                                        # noqa: BLE001
        pass

    ok, note = await do_withdraw(row.id, addr, amount)
    await set_setting(row.id, "_fragment_wd_pending", "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به کیف‌پول",
                              callback_data="smm:fragment:menu",
                              style=ButtonStyle.PRIMARY)]])
    txt = (f"✅ <b>برداشت انجام شد</b>\n\n{note}\n\nمقصد:\n<code>{addr}</code>"
           if ok else f"❌ <b>برداشت انجام نشد</b>\n\n{note}")
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:                                        # noqa: BLE001
        await cb.message.answer(txt, reply_markup=kb)


# ═══════════════════════ راهنمایِ کامل ═══════════════════════
# === PATCH_FRAGMENT_HELP_V1 ===
# پیامِ تلگرام سقفِ ۴۰۹۶ کاراکتر دارد و راهنمایِ کاملِ این بخش خیلی بیشتر
# از آن است، پس به‌جایِ یک پیامِ غول‌پیکر (که یا تراکنده می‌شود یا اصلاً
# ارسال نمی‌شود) به چند صفحهٔ کوتاه تقسیم شده. هر صفحه مستقل خوانده
# می‌شود تا کاربر مجبور نباشد همه را پشتِ‌هم بخواند.

_HELP_PAGES: list[tuple[str, str]] = [
    ("۱) این بخش چیست؟", (
        "⭐️ <b>استارزِ خودکار — فرگمنت</b>\n\n"
        "با این بخش، وقتی مشتری از رباتت استارز می‌خرد، ربات <b>خودکار</b> "
        "استارز را از Fragment می‌خرد و تحویل می‌دهد — بدونِ دخالتِ تو.\n\n"
        "<b>چطور کار می‌کند؟</b>\n"
        "۱. ربات یک کیف‌پولِ TONِ <b>تازه و جدا</b> مخصوصِ همین ربات می‌سازد.\n"
        "۲. تو به آن آدرس TON واریز می‌کنی.\n"
        "۳. ربات را به اکانتِ Fragmentِ خودت وصل می‌کنی.\n"
        "۴. از آن به بعد، هر سفارش خودکار انجام می‌شود.\n\n"
        "<b>چرا کیف‌پولِ جدا؟</b>\n"
        "چون Fragment یک سرویسِ غیررسمی است. اگر کلیدِ کیف‌پولِ شخصی‌ات را "
        "می‌دادیم، بدترین حالتِ نشت یعنی از دست رفتنِ <i>کلِ</i> دارایی‌ات. "
        "با کیف‌پولِ جدا، بدترین حالت فقط همان موجودیِ محدودِ داخلش است.\n\n"
        "⚠️ <b>پس فقط به‌اندازهٔ گردشِ فروشت شارژ کن، نه بیشتر.</b>\n\n"
        "⚠️ این سرویس نه از تلگرام است نه از Fragment.com — غیررسمی است و "
        "بات‌ساز مسئولیتی بابتِ آن نمی‌پذیرد."
    )),
    ("۲) شارژِ کیف‌پول", (
        "💰 <b>شارژِ کیف‌پولِ ربات</b>\n\n"
        "<b>۱.</b> در همین صفحه، دکمهٔ «📋 کپیِ آدرس» را بزن.\n\n"
        "<b>۲.</b> از هر صرافی یا کیف‌پولی (Tonkeeper، والتِ تلگرام، صرافیِ "
        "ایرانی و…) به همان آدرس <b>TON</b> بفرست.\n\n"
        "<b>۳.</b> چند دقیقه بعد، دکمهٔ «🔄 بررسیِ موجودی» را بزن تا عددِ "
        "تازه نمایش داده شود.\n\n"
        "⚠️ <b>نکته‌هایِ مهم</b>\n"
        "• فقط <b>TON</b> بفرست. اگر ارزِ دیگری (USDT، جتون و…) بفرستی، "
        "ربات نمی‌شناسدش.\n"
        "• حتماً رویِ شبکهٔ <b>TON</b> بفرست، نه شبکهٔ دیگر.\n"
        "• عددِ «آخرین موجودیِ ثبت‌شده» <b>زنده نیست</b> — تاریخِ زیرش را "
        "نگاه کن. اگر تازه واریز کرده‌ای و عدد عوض نشده، یعنی فقط هنوز "
        "بررسی نشده؛ دکمهٔ «🔄 بررسیِ موجودی» را بزن.\n"
        "• اگر بعد از چند بار بررسی هنوز صفر بود، تراکنش را در "
        "<code>tonviewer.com</code> با همان آدرس چک کن."
    )),
    ("۳) وصل‌کردن به Fragment", (
        "🔗 <b>وصل‌کردن به Fragment</b>\n\n"
        "تا این کار را نکنی، وضعیت «🔴 هنوز وصل نشده» می‌ماند و "
        "<b>هیچ سفارشی خودکار انجام نمی‌شود</b> — حتی اگر کیف‌پول پر باشد.\n\n"
        "دو چیز لازم است:\n\n"
        "━━━ <b>الف) کوکی‌هایِ Fragment</b> ━━━\n"
        "<b>۱.</b> با مرورگرِ کامپیوتر برو به <code>fragment.com</code>\n"
        "<b>۲.</b> با اکانتِ تلگرامت لاگین کن.\n"
        "<b>۳.</b> کیف‌پولِ TONـت را همان‌جا وصل کن (دکمهٔ Connect Wallet).\n"
        "<b>۴.</b> افزونهٔ <b>Cookie-Editor</b> را نصب کن و رویِ همان صفحه بازش کن.\n"
        "<b>۵.</b> این چهار مقدار را بردار:\n"
        "<code>stel_ssid</code>\n<code>stel_dt</code>\n"
        "<code>stel_token</code>\n<code>stel_ton_token</code>\n"
        "<b>۶.</b> در ربات، «🔧 وصل‌کردن به Fragment» را بزن و بفرست — "
        "یا به‌شکلِ JSON، یا چهار خطِ جدا به‌شکلِ <code>کلید=مقدار</code>.\n\n"
        "━━━ <b>ب) کلیدِ tonapi.io</b> ━━━\n"
        "برو <code>tonconsole.com</code>، ثبت‌نام کن و یک API key بساز "
        "(رایگان است)، بعد همان را در ربات بفرست.\n\n"
        "⚠️ <b>امنیت:</b> این کوکی‌ها دسترسیِ کامل به اکانتِ Fragmentِ تو "
        "می‌دهند. فقط در چتِ خصوصیِ خودت با ربات بفرست — به هیچ‌کس دیگر، "
        "حتی پشتیبانی، نده."
    )),
    ("۴) تست و شروعِ فروش", (
        "🧪 <b>تست کن، بعد بفروش</b>\n\n"
        "بعد از وصل‌شدن، وضعیت «🟢 وصل شده» می‌شود و دکمهٔ "
        "«🧪 تستِ خریدِ استارز» ظاهر می‌شود.\n\n"
        "<b>حتماً اول این را بزن</b> و یک خریدِ کوچک (مثلاً ۵۰ استارز) "
        "برایِ یوزرنیمِ خودت انجام بده. اگر موفق بود، خیالت راحت است که "
        "سفارشِ مشتریِ واقعی هم درست انجام می‌شود.\n\n"
        "<b>چرا تست مهم است؟</b>\n"
        "اگر بدونِ تست بفروشی و چیزی خراب باشد، پولِ مشتری کسر شده ولی "
        "استارز نرفته — و باید دستی حلش کنی.\n\n"
        "<b>بعد از تستِ موفق:</b>\n"
        "• کیف‌پول را به‌اندازهٔ گردشِ روزانه‌ات شارژ نگه دار.\n"
        "• هر چند وقت «🔄 بررسیِ موجودی» را بزن؛ اگر موجودی تمام شود "
        "سفارش‌ها انجام نمی‌شوند.\n"
        "• قیمتِ فروشت را از بخشِ نرخِ استارز تنظیم کن."
    )),
    ("۵) برداشتِ پول", (
        "💸 <b>برداشتِ TON از کیف‌پول</b>\n\n"
        "هر وقت خواستی پولت را بیرون بکشی، دکمهٔ «💸 برداشتِ TON» را بزن.\n\n"
        "<b>مراحل:</b>\n"
        "۱. آدرسِ کیف‌پولِ مقصد را بفرست.\n"
        "۲. مبلغ را به TON بفرست — یا کلمهٔ <code>همه</code> برایِ کلِ موجودی.\n"
        "۳. صفحهٔ تایید را با دقت بخوان و تایید کن.\n\n"
        "<b>نکته‌ها:</b>\n"
        "• گزینهٔ «همه» کلِ موجودی را می‌فرستد و کارمزد را خودِ شبکه کم می‌کند.\n"
        "• در حالتِ مبلغِ مشخص، حدودِ ۰.۰۵ TON برایِ کارمزدِ شبکه کنار می‌ماند.\n"
        "• حداقلِ برداشت ۰.۰۱ TON است.\n"
        "• فقط <b>مالکِ ربات</b> می‌تواند برداشت کند، نه ادمین‌هایِ فرعی.\n\n"
        "🚨 <b>هشدارِ جدی:</b> تراکنشِ بلاک‌چین <b>برگشت‌پذیر نیست</b>. "
        "اگر آدرس را اشتباه بزنی، پول برایِ همیشه از دست می‌رود. "
        "قبل از تایید، آدرس را دو بار چک کن."
    )),
    ("۶) عیب‌یابی", (
        "🛠 <b>مشکلاتِ رایج</b>\n\n"
        "<b>❓ موجودی صفر نشان می‌دهد ولی واریز کرده‌ام</b>\n"
        "عدد زنده نیست. «🔄 بررسیِ موجودی» را بزن. اگر باز صفر بود، "
        "آدرس را در <code>tonviewer.com</code> چک کن که تراکنش واقعاً "
        "نشسته باشد و رویِ شبکهٔ TON فرستاده باشی.\n\n"
        "<b>❓ وضعیت «🔴 هنوز وصل نشده» است</b>\n"
        "کوکی‌ها یا کلیدِ tonapi ثبت نشده. صفحهٔ ۳ همین راهنما را ببین. "
        "داشتنِ موجودی به‌تنهایی کافی نیست.\n\n"
        "<b>❓ کوکی‌ها را دادم ولی باز وصل نشد</b>\n"
        "معمولاً یکی از چهار مقدار ناقص کپی شده. دوباره از Cookie-Editor "
        "برشان دار — مطمئن شو کلِ مقدار را گرفته‌ای، نه بخشی از آن.\n\n"
        "<b>❓ قبلاً کار می‌کرد، حالا نه</b>\n"
        "کوکی‌هایِ Fragment بعد از مدتی منقضی می‌شوند یا با خروج از اکانت "
        "باطل می‌شوند. کافیست دوباره از fragment.com بگیری و با "
        "«🔧 ویرایشِ اتصال» به‌روز کنی.\n\n"
        "<b>❓ تستِ خرید خطا می‌دهد</b>\n"
        "متنِ دقیقِ خطا را برایِ پشتیبانی بفرست — بدونِ آن نمی‌شود فهمید "
        "مشکل از موجودی است، از کوکی، یا از خودِ Fragment."
    )),
]


def _help_kb(idx: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی",
                                        callback_data=f"smm:fragment:help:{idx - 1}"))
    if idx < len(_HELP_PAGES) - 1:
        nav.append(InlineKeyboardButton(text="بعدی ➡️",
                                        callback_data=f"smm:fragment:help:{idx + 1}"))
    if nav:
        rows.append(nav)
    # فهرستِ سریع، تا کاربر مجبور نباشد صفحه‌به‌صفحه جلو برود
    for i, (title, _) in enumerate(_HELP_PAGES):
        if i != idx:
            rows.append([InlineKeyboardButton(
                text=title, callback_data=f"smm:fragment:help:{i}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به کیف‌پول",
                                      callback_data="smm:fragment:menu",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@fragment_router.callback_query(F.data == "smm:fragment:help")
@fragment_router.callback_query(F.data.startswith("smm:fragment:help:"))
async def fragment_help(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    idx = 0
    if cb.data.startswith("smm:fragment:help:"):
        try:
            idx = max(0, min(len(_HELP_PAGES) - 1, int(cb.data.rsplit(":", 1)[1])))
        except (ValueError, IndexError):
            idx = 0
    title, body = _HELP_PAGES[idx]
    text = f"{body}\n\n<i>صفحهٔ {idx + 1} از {len(_HELP_PAGES)}</i>"
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=_help_kb(idx))
    except Exception:                                        # noqa: BLE001
        await cb.message.answer(text, reply_markup=_help_kb(idx))
