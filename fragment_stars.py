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


async def fetch_balance_ton(address: str) -> float | None:
    """موجودیِ زنده از toncenter (به TON). خطا → None (نه صفر، تا با
    «واقعاً صفره» قاطی نشود)."""
    if aiohttp is None:
        return None
    url = f"{TONCENTER_BASE}/getAddressBalance"
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(url, params={"address": address}) as r:
                data = await r.json(content_type=None)
    except Exception:
        logger.warning("fragment_stars: toncenter پاسخ نداد (%s)", address, exc_info=True)
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
        await wait_msg.edit_text(
            f"❌ خرید ناموفق بود: {type(e).__name__}: {e}\n\n"
            "دلایلِ محتمل: کوکی‌ها منقضی شدن (دوباره وصل کن)، موجودیِ کیف‌پول "
            "کافی نیست، یا یوزرنیم/شرایطِ گیرنده مشکل داره.")
