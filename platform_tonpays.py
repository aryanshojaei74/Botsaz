"""
platform_tonpays.py — درگاه TonPays برایِ خودِ رباتِ بات‌ساز (PATCH_TONPAYS_PLATFORM_V1).

همان منطقِ tenant/tonpays.py ولی برایِ کیف پولِ کاربرانِ بات‌ساز. کلاینتِ API
از همان ماژولِ مستأجر import می‌شود تا منطقِ ارتباط با درگاه فقط یک‌جا نوشته
شده باشد و اگر روزی درگاه عوض شد، دو جا از هم جدا نیفتند.

⚠️ همان نکتهٔ امنیتی: TonPays کال‌بک را امضا نمی‌کند (نه HMAC، نه هیچ امضایِ
دیگری) و فقط API-Key را در هدر پس می‌فرستد. پس بدنهٔ کال‌بک اینجا هم هرگز
ملاکِ شارژ نیست؛ همیشه با /invoices/check از خودِ درگاه تایید گرفته می‌شود و
مبلغ با آنچه خودمان ثبت کرده‌ایم مقایسه می‌گردد.
"""
import asyncio
import secrets

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, update as sa_update

from config import Session, WEBHOOK_BASE
from logging_setup import get_logger as _get_logger
from models import PlatformTonPaysCharge, PlatformTxn, PlatformUser

# کلاینتِ مشترک — منطقِ HTTP فقط یک‌جا نوشته شده است
from tenant.gateways.tonpays import (
    CHANGE_CARD_COOLDOWN, build_invoice_view, change_card,
    DISCLAIMER, ERROR_FA, MIN_AMOUNT, NEED_ACTION_TEXT, RECEIPT_MAX_BYTES,
    STATUS_FA as _STATUS_FA_SHARED, TonPaysError, check_invoice, create_invoice,
    is_custom_order, pick_card, upload_receipt, validate_api_key,
)

logger = _get_logger(__name__)

platform_tonpays_router = Router(name="platform_tonpays")

STALE_AFTER_MIN = 60
_POLL_SCHEDULE = [15, 15, 15, 15, 30, 30, 30, 60, 60, 60, 90, 90, 120, 120, 180, 180]


class PTonFSM(StatesGroup):
    api_key = State()
    amount = State()
    card = State()
    holder = State()
    receipt = State()   # === PATCH_TONPAYS_CUSTOM_V1 ===


def _rial(n) -> str:
    return f"{int(n):,} تومان"


def _new_order_id(custom: bool = False) -> str:
    return ("pc" if custom else "pt") + secrets.token_hex(6)


# === PATCH_TONPAYS_CUSTOM_V1 === نوعِ درگاه (معمولی/کاستوم-تلگرام) + کلیدِ مربوطه
async def is_custom_mode() -> bool:
    from platform_bot import get_pset
    return (await get_pset("ptonpays_mode", "")) == "custom"


async def _get_key(custom: bool) -> str:
    from platform_bot import get_pset
    return (await get_pset("ptonpays_custom_key" if custom else "ptonpays_api_key", "")) or ""


_LAST_STATUS: dict[str, str] = {}
_NEED_ACTION_NOTIFIED: set[str] = set()


def _receipt_kb(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 ارسال فیش واریزی",
                              callback_data=f"ptonp:rcpt:{order_id}")]])


def _callback_url() -> str | None:
    return f"{WEBHOOK_BASE}/tonpays/platform/callback" if WEBHOOK_BASE else None


async def _is_super(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return uid == PLATFORM_ADMIN_ID


async def is_available() -> bool:
    from platform_bot import get_pset
    return (await get_pset("ptonpays_enabled", "off")) == "on" and \
        bool(await _get_key(await is_custom_mode()))


# ═══════════════ هستهٔ امن: تایید و شارژ ═══════════════

async def settle_charge(order_id: str) -> tuple[bool, str, int]:
    """تنها نقطه‌ای که موجودی را زیاد می‌کند؛ همیشه با استعلامِ مستقل از درگاه."""
    from platform_bot import get_pset
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.order_id == order_id))
        if not ch:
            return False, "فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        invoice_id, expected = ch.invoice_id, int(ch.request_amount)
        payer_tg = int(ch.user_telegram_id)

    _custom = is_custom_order(order_id)   # PATCH_TONPAYS_CUSTOM_V1
    api_key = await _get_key(_custom)
    if not api_key or not invoice_id:
        return False, "درگاه تنظیم نشده است.", 0

    try:
        info = await check_invoice(api_key, invoice_id, custom=_custom)
    except Exception as e:
        logger.warning("platform tonpays check failed %s: %s", order_id, e)
        return False, f"استعلام از درگاه ناموفق بود: {e}", 0

    # === PATCH_TONPAYS_DOCS_V2 === paid=true یا status=completed
    from tenant.gateways.tonpays import is_paid as _is_paid, STATUS_FA as _STATUS_FA
    if not _is_paid(info):
        _st = info.get('status') or 'نامشخص'
        _LAST_STATUS[order_id] = _st   # PATCH_TONPAYS_CUSTOM_V1
        _hint = ""
        if _custom and _st in ("pending", "need_action"):
            _hint = " اگر واریز کرده‌ای، «📎 ارسال فیش» را بزن و تصویرِ فیش را بفرست."
        return False, f"هنوز پرداخت تایید نشده (وضعیت: {_STATUS_FA.get(_st, _st)}).{_hint}", 0

    reported = int(info.get("request_amount") or 0)
    if reported and reported < expected:
        logger.error("platform tonpays amount mismatch %s: %s != %s",
                     order_id, expected, reported)
        return False, "مبلغِ اعلام‌شده با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

    credit = expected
    async with Session() as s:
        claim = await s.execute(
            sa_update(PlatformTonPaysCharge)
            .where(PlatformTonPaysCharge.order_id == order_id,
                   PlatformTonPaysCharge.status != "paid")
            .values(status="paid"))
        if claim.rowcount == 0:
            await s.rollback()
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        from platform_bot import get_user
        u = await get_user(s, payer_tg, for_update=True)
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(PlatformTxn(user_telegram_id=payer_tg, amount=credit,
                              kind="charge", note="شارژ با درگاه TonPays"))
        await s.commit()
    return True, "پرداخت تایید و کیف پول شارژ شد.", credit


# ═══════════════════════ تنظیماتِ ادمین ═══════════════════════

async def _menu_kb() -> InlineKeyboardMarkup:
    from platform_bot import get_pset
    from premium_emoji import hx_button
    on = (await get_pset("ptonpays_enabled", "off")) == "on"
    _custom = await is_custom_mode()   # PATCH_TONPAYS_CUSTOM_V1
    has_key = bool(await _get_key(_custom))
    rows = [
        [hx_button("🔀 نوعِ درگاه: " + ("کاستوم (تلگرام)" if _custom else "معمولی"),
                   callback_data="ptonp:mode")],
        [hx_button("🔑 ثبت / تغییر کلید API" + (" (کاستوم)" if _custom else ""),
                   callback_data="ptonp:setkey")],
        [hx_button("🧪 تست اتصال", callback_data="ptonp:test")],
    ]
    if has_key:
        rows.append([hx_button(("💠 درگاه: ✅ فعال" if on else "💠 درگاه: ❌ غیرفعال"),
                               callback_data="ptonp:toggle",
                               style=(ButtonStyle.SUCCESS if on else ButtonStyle.DANGER))])
    else:
        rows.append([hx_button("⚠️ اول کلید API را ثبت کن", callback_data="ptonp:setkey")])
    if on:
        card_mode = await get_pset("ptonpays_card_mode", "")
        card_lbl = {"1": "✅ فعال (داخلِ ربات)",
                    "0": "❌ غیرفعال"}.get(card_mode, "❓ هنوز مشخص نیست")
        if not _custom:
            rows.append([hx_button(f"💳 نمایش شمارهٔ کارت: {card_lbl}",
                                   callback_data="ptonp:cardmode")])
        man = await get_pset("ptonpays_card_number", "")
        rows.append([hx_button("🏦 کارتِ مقصد (دستی): " + (man if man else "ثبت نشده"),
                               callback_data="ptonp:setcard")])
    rows.append([hx_button("⬅️ بازگشت به پنل", callback_data="adm:back",
                           style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@platform_tonpays_router.callback_query(F.data.in_({"set:ptonpays", "ptonp:menu"}))
async def ptonp_menu(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.clear()
    _custom = await is_custom_mode()   # PATCH_TONPAYS_CUSTOM_V1
    key = await _get_key(_custom)
    on = (await get_pset("ptonpays_enabled", "off")) == "on"
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("ثبت نشده" if not key else "ثبت شده")
    txt = (
        "💠 <b>درگاه TonPays — بات‌ساز</b>\n\n"
        "درگاهِ کارت‌به‌کارتِ خودکار (مبلغ به تومان). کاربر مبلغ را واریز می‌کند و "
        "درگاه با «مبلغِ یکتا» واریز را خودکار تشخیص می‌دهد.\n\n"
        f"نوعِ درگاه: <b>{'کاستوم (بستر تلگرام)' if _custom else 'معمولی'}</b>\n"
        f"کلید API: <code>{masked}</code>\n"
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n\n"
        + DISCLAIMER
    )
    kb = await _menu_kb()
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    try:
        await cb.answer()
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")


# === PATCH_TONPAYS_CUSTOM_V1 ===
@platform_tonpays_router.callback_query(F.data == "ptonp:mode")
async def ptonp_mode(cb: CallbackQuery, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    cur = await is_custom_mode()
    await set_pset("ptonpays_mode", "" if cur else "custom")
    await ptonp_menu(cb, state)


@platform_tonpays_router.callback_query(F.data == "ptonp:setkey")
async def ptonp_setkey(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(PTonFSM.api_key)
    if await is_custom_mode():
        await cb.message.answer(
            "🔑 <b>کلیدِ بستر تلگرامِ درگاه کاستوم</b> را از «پنل فروشندگان ← تنظیمات ← "
            "درگاه کاستوم» بگیر و بفرست.\n⚠️ کلیدِ بستر وب‌سایت اینجا کار نمی‌کند.",
            parse_mode="HTML")
    else:
        await cb.message.answer(
            "🔑 کلید API را از منویِ «مستندات API» در رباتِ TonPays کپی کن و بفرست:")
    await cb.answer()


@platform_tonpays_router.message(PTonFSM.api_key, F.text)
async def ptonp_setkey_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    key = (msg.text or "").strip()
    if len(key) < 8:
        await msg.answer("کلید خیلی کوتاه است. دوباره بفرست:")
        return
    await msg.answer("⏳ در حال بررسی کلید…")
    _custom = await is_custom_mode()   # PATCH_TONPAYS_CUSTOM_V1
    ok, note = await validate_api_key(key, custom=_custom)
    if not ok:
        await msg.answer(f"❌ {note}\n\nدوباره امتحان کن یا لغو کن.")
        return
    await set_pset("ptonpays_custom_key" if _custom else "ptonpays_api_key", key)
    await state.clear()
    await msg.answer(f"✅ {note}\nکلید ذخیره شد. حالا درگاه را فعال کن.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="💠 بازگشت به درگاه",
                                               callback_data="ptonp:menu")]]))


@platform_tonpays_router.callback_query(F.data == "ptonp:test")
async def ptonp_test(cb: CallbackQuery):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    _custom = await is_custom_mode()   # PATCH_TONPAYS_CUSTOM_V1
    key = await _get_key(_custom)
    if not key:
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    await cb.answer("در حال تست…")
    ok, note = await validate_api_key(key, custom=_custom)
    await cb.message.answer(("✅ " if ok else "❌ ") + note)


@platform_tonpays_router.callback_query(F.data == "ptonp:toggle")
async def ptonp_toggle(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset, set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    if not await _get_key(await is_custom_mode()):   # PATCH_TONPAYS_CUSTOM_V1
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    cur = await get_pset("ptonpays_enabled", "off")
    if cur == "on":
        await set_pset("ptonpays_enabled", "off")
        await ptonp_menu(cb, state)
        return
    if (await get_pset("ptonpays_accepted", "off")) != "on":
        await cb.message.answer(
            "📌 <b>قبل از فعال‌سازی، این را بخوان و تایید کن</b>\n\n" + DISCLAIMER,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ می‌پذیرم و فعال کن",
                                      callback_data="ptonp:accept")],
                [InlineKeyboardButton(text="❌ نمی‌پذیرم", callback_data="ptonp:menu")],
            ]))
        await cb.answer()
        return
    await set_pset("ptonpays_enabled", "on")
    await ptonp_menu(cb, state)


@platform_tonpays_router.callback_query(F.data == "ptonp:accept")
async def ptonp_accept(cb: CallbackQuery, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await set_pset("ptonpays_accepted", "on")
    await set_pset("ptonpays_enabled", "on")
    await ptonp_menu(cb, state)


@platform_tonpays_router.callback_query(F.data == "ptonp:cardmode")
async def ptonp_cardmode(cb: CallbackQuery):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    mode = await get_pset("ptonpays_card_mode", "")
    if mode == "1":
        head = "✅ <b>فعال است.</b> کلِ پرداخت داخلِ خودِ ربات انجام می‌شود."
    elif mode == "0":
        head = ("❌ <b>غیرفعال است.</b> درگاه شمارهٔ کارت را نمی‌فرستد، پس کاربر "
                "باید برایِ دیدنِ اطلاعاتِ واریز از ربات خارج شود.")
    else:
        head = ("❓ <b>هنوز مشخص نیست</b> — بعد از ساختِ اولین فاکتورِ واقعی "
                "معلوم می‌شود (API راهی برایِ پرسیدنش ندارد).")
    await cb.message.answer(
        f"💳 <b>نمایش شمارهٔ کارت داخلِ ربات</b>\n\n{head}\n\n"
        "🔧 APIِ TonPays فقط چهار اندپوینت دارد و راهی برایِ روشن‌کردنِ این "
        "قابلیت از بیرون ندارد. یا از پشتیبانیِ TonPays بخواه برایِ کلیدت "
        "فعالش کنند، یا ساده‌تر: از «🏦 کارتِ مقصد (دستی)» استفاده کن و شمارهٔ "
        "کارت را یک‌بار خودت ثبت کن — چون کارتِ مقصد ثابت است و تشخیصِ پرداخت "
        "از رویِ «مبلغِ یکتا» انجام می‌شود، نتیجه دقیقاً یکی است.",
        parse_mode="HTML")
    await cb.answer()


@platform_tonpays_router.callback_query(F.data == "ptonp:setcard")
async def ptonp_setcard(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(PTonFSM.card)
    await cb.message.answer(
        "🏦 <b>شمارهٔ کارتِ مقصد</b>\n\n"
        "همان شمارهٔ کارتی که TonPays برایِ دریافتِ واریزها می‌دهد را بفرست (۱۶ رقم).\n\n"
        "⚠️ <b>خیلی مهم:</b> باید دقیقاً همان کارتی باشد که TonPays رصد می‌کند؛ "
        "وگرنه پولِ کاربر به جایی می‌رود که درگاه نمی‌بیند و پرداخت هیچ‌وقت "
        "تایید نمی‌شود.\n\nبرایِ پاک‌کردن، <code>0</code> را بفرست.",
        parse_mode="HTML")
    await cb.answer()


@platform_tonpays_router.message(PTonFSM.card, F.text)
async def ptonp_setcard_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw == "0":
        await set_pset("ptonpays_card_number", "")
        await set_pset("ptonpays_card_holder", "")
        await state.clear()
        await msg.answer("✅ کارتِ دستی پاک شد.")
        return
    digits = "".join(ch for ch in raw.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")) if ch.isdigit())
    if len(digits) != 16:
        await msg.answer("شمارهٔ کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست:")
        return
    await set_pset("ptonpays_card_number", digits)
    await state.set_state(PTonFSM.holder)
    await msg.answer("🧑 نامِ صاحبِ کارت را بفرست (یا <code>-</code> برایِ رد کردن):",
                     parse_mode="HTML")


@platform_tonpays_router.message(PTonFSM.holder, F.text)
async def ptonp_setholder_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    name = (msg.text or "").strip()
    await set_pset("ptonpays_card_holder", "" if name == "-" else name[:60])
    await state.clear()
    await msg.answer("✅ ذخیره شد. از این به بعد پرداخت کاملاً داخلِ ربات انجام می‌شود.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="💠 بازگشت به درگاه",
                                               callback_data="ptonp:menu")]]))


# ═══════════════════════ جریانِ کاربر ═══════════════════════

@platform_tonpays_router.callback_query(F.data == "charge_ptonpays")
async def charge_ptonpays(cb: CallbackQuery, state: FSMContext):
    if not await is_available():
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.set_state(PTonFSM.amount)
    await cb.message.answer(
        "💠 <b>شارژ کیف پول با TonPays</b>\n\n"
        "این یک درگاهِ کارت‌به‌کارتِ خودکارِ <b>شخصِ ثالث</b> است و متعلق به "
        "بات‌ساز نیست. استفاده از آن اختیاری است و مسئولیتِ پرداخت از این طریق "
        "با خودِ شماست؛ اگر ترجیح می‌دهی از روش‌هایِ پرداختِ دیگر استفاده کن.\n\n"
        f"مبلغِ موردنظر را به <b>تومان</b> بفرست (حداقل <b>{MIN_AMOUNT:,} تومان</b>):",
        parse_mode="HTML")
    await cb.answer()


@platform_tonpays_router.message(PTonFSM.amount, F.text)
async def ptonp_amount(msg: Message, bot: Bot, state: FSMContext):
    from platform_bot import fa_to_int, get_pset, set_pset
    amount = fa_to_int(msg.text)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست:")
        return
    if amount < MIN_AMOUNT:
        await state.set_state(PTonFSM.amount)
        await msg.answer(
            f"❌ حداقلِ مبلغ برایِ این درگاه <b>{MIN_AMOUNT:,} تومان</b> است.\n"
            "یک مبلغِ بیشتر بفرست:", parse_mode="HTML")
        return
    await state.clear()

    _custom = await is_custom_mode()   # PATCH_TONPAYS_CUSTOM_V1
    api_key = await _get_key(_custom)
    order_id = _new_order_id(_custom)
    await msg.answer("⏳ در حال ساختِ فاکتور…")
    try:
        inv = await create_invoice(api_key, amount, order_id, _callback_url(),
                                   buyer_chat_id=msg.from_user.id,
                                   custom=_custom)  # PATCH_TONPAYS_DOCS_V2
    except Exception as e:
        await msg.answer(f"❌ ساختِ فاکتور ناموفق بود.\n{e}")
        return

    invoice_id = inv.get("invoice_id") or ""
    final_amount = int(inv.get("final_amount") or amount)
    invoice_url = inv.get("invoice_url") or ""
    web_invoice_url = inv.get("web_invoice_url") or ""  # === PATCH_TONPAYS_DOCS_V2 ===

    async with Session() as s:
        s.add(PlatformTonPaysCharge(
            user_telegram_id=msg.from_user.id, order_id=order_id,
            invoice_id=invoice_id, request_amount=amount,
            final_amount=final_amount, status="waiting"))
        await s.commit()

    card = inv.get("card_number")
    card_name = inv.get("card_name")
    if _custom:   # PATCH_TONPAYS_CUSTOM_V1 — کارت همیشه در پاسخ است (ممکن است ماسک باشد)
        card, card_name = pick_card(inv, await get_pset("ptonpays_card_number", ""),
                                    await get_pset("ptonpays_card_holder", ""))
    else:
        await set_pset("ptonpays_card_mode", "1" if card else "0")
    if not card:
        card = await get_pset("ptonpays_card_number", "")
        card_name = await get_pset("ptonpays_card_holder", "")

    _text, _kb = build_invoice_view(
        "ptonp", order_id, amount, final_amount, card, card_name, _custom,
        invoice_url=invoice_url, web_invoice_url=web_invoice_url, rial_fn=_rial)
    await msg.answer(_text, parse_mode="HTML", reply_markup=_kb)
    asyncio.create_task(_auto_poll(bot, order_id, msg.chat.id))


@platform_tonpays_router.callback_query(F.data.startswith("ptonp:verify:"))
async def ptonp_verify(cb: CallbackQuery):
    order_id = cb.data.split(":")[2]
    await cb.answer("در حال استعلام از درگاه…")
    ok, note, credit = await settle_charge(order_id)
    if ok:
        await cb.message.answer(
            f"✅ {note}\n\nمبلغِ <b>{_rial(credit)}</b> به کیف پولت اضافه شد.",
            parse_mode="HTML")
    else:
        await cb.message.answer(
            f"ℹ️ {note}",
            reply_markup=_receipt_kb(order_id) if is_custom_order(order_id) else None)


# ═══════════ تعویضِ کارت (درگاهِ کاستوم) ═══════════
# === PATCH_TONPAYS_CHANGE_CARD_V1 === (همان منطقِ مستأجر؛ ببین tenant/gateways/tonpays.py)
_CHG_LAST: dict[str, float] = {}


@platform_tonpays_router.callback_query(F.data.startswith("ptonp:chg:"))
async def ptonp_change_card(cb: CallbackQuery):
    import time as _t
    from platform_bot import get_pset
    order_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != cb.from_user.id or not is_custom_order(order_id):
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً پرداخت شده است.", show_alert=True)
        return
    last = _CHG_LAST.get(order_id)
    left = max(0, int(CHANGE_CARD_COOLDOWN - (_t.time() - last))) if last else 0
    if left:
        await cb.answer(f"⏳ برایِ تعویضِ دوباره {left} ثانیهٔ دیگر صبر کن.", show_alert=True)
        return
    api_key = await _get_key(True)
    if not api_key or not ch.invoice_id:
        await cb.answer("درگاه تنظیم نشده است.", show_alert=True)
        return
    try:
        res = await change_card(api_key, ch.invoice_id)
    except TonPaysError as e:
        if e.status == 429 or "COOLDOWN" in (e.code or "").upper():
            _CHG_LAST[order_id] = _t.time()
            await cb.answer(f"⏳ تعویضِ کارت محدود است؛ حدودِ {CHANGE_CARD_COOLDOWN} ثانیه بعد دوباره بزن.",
                            show_alert=True)
        else:
            await cb.answer(f"❌ {e}", show_alert=True)
        return
    except Exception:
        logger.exception("platform tonpays change_card failed %s", order_id)
        await cb.answer("❌ تعویضِ کارت ناموفق بود؛ کمی بعد دوباره امتحان کن.", show_alert=True)
        return
    _CHG_LAST[order_id] = _t.time()
    card, card_name = pick_card(res, await get_pset("ptonpays_card_number", ""),
                                await get_pset("ptonpays_card_holder", ""))
    final_amount = int(res.get("final_amount") or ch.final_amount or ch.request_amount)
    if final_amount != int(ch.final_amount or 0):
        try:
            async with Session() as s:
                await s.execute(sa_update(PlatformTonPaysCharge).where(
                    PlatformTonPaysCharge.order_id == order_id).values(final_amount=final_amount))
                await s.commit()
        except Exception:
            logger.exception("platform tonpays: ثبتِ مبلغِ جدید بعد از تعویضِ کارت ناموفق بود")
    exhausted = bool(res.get("change_card_exhausted"))
    can_change = bool(res.get("show_change_card", True)) and not exhausted
    text, kb = build_invoice_view(
        "ptonp", order_id, int(ch.request_amount), final_amount, card, card_name, True,
        rial_fn=_rial, can_change=can_change)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:      # noqa: BLE001
        if "not modified" not in str(e).lower():
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    if exhausted:
        await cb.answer("✅ کارت عوض شد. این آخرین کارتِ موجود بود؛ دیگر تعویض ممکن نیست.", show_alert=True)
    else:
        await cb.answer("✅ کارتِ جدید نمایش داده شد. اگر باز مشکل داشت، بعد از ۶۰ ثانیه می‌توانی دوباره تعویض کنی.",
                        show_alert=True)


# ═══════════ ارسالِ فیش (درگاهِ کاستوم) ═══════════
# === PATCH_TONPAYS_CUSTOM_V1 === طبقِ مستندات دکمهٔ ارسالِ فیش اجباری است.

@platform_tonpays_router.callback_query(F.data.startswith("ptonp:rcpt:"))
async def ptonp_receipt_start(cb: CallbackQuery, state: FSMContext):
    order_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != cb.from_user.id or not is_custom_order(order_id):
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً تأیید و شارژ شده است.", show_alert=True)
        return
    await state.set_state(PTonFSM.receipt)
    await state.update_data(ptonp_receipt_order=order_id)
    await cb.message.answer(
        f"📎 <b>ارسال فیش واریزی</b> — فاکتورِ <code>{order_id}</code>\n\n"
        "تصویرِ فیش را همین‌جا به‌صورتِ <b>عکس</b> بفرست (حداکثر ۱۰ مگابایت).",
        parse_mode="HTML")
    await cb.answer()


@platform_tonpays_router.message(PTonFSM.receipt, F.photo | F.document)
async def ptonp_receipt_file(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("ptonp_receipt_order") or ""
    if not order_id:
        await state.clear()
        return
    if msg.photo:
        f = msg.photo[-1]
        fname, ctype = "receipt.jpg", "image/jpeg"
    else:
        f = msg.document
        ctype = (f.mime_type or "").lower()
        if not ctype.startswith("image/"):
            await msg.answer("❌ فقط تصویرِ فیش (JPG/PNG) پذیرفته می‌شود. دوباره بفرست:")
            return
        fname = f.file_name or "receipt.jpg"
    if f.file_size and f.file_size > RECEIPT_MAX_BYTES:
        await msg.answer("❌ " + ERROR_FA["RECEIPT_TOO_LARGE"])
        return
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != msg.from_user.id:
        await state.clear()
        await msg.answer("این فاکتور پیدا نشد.")
        return
    api_key = await _get_key(True)
    if not api_key or not ch.invoice_id:
        await state.clear()
        await msg.answer("درگاه تنظیم نشده است.")
        return
    await msg.answer("⏳ در حال ارسالِ فیش…")
    try:
        buf = await bot.download(f.file_id)
        raw = buf.read() if buf else b""
        if not raw:
            raise RuntimeError("دانلودِ فایل از تلگرام ناموفق بود.")
        res = await upload_receipt(api_key, ch.invoice_id, raw, fname, ctype)
    except TonPaysError as e:
        if e.code in ("INVALID_RECEIPT_TYPE", "RECEIPT_TOO_LARGE"):
            await msg.answer(f"❌ {e}\nدوباره بفرست:")
            return
        await state.clear()
        await msg.answer(f"❌ ارسالِ فیش ناموفق بود.\n{e}", reply_markup=_receipt_kb(order_id))
        return
    except Exception as e:
        logger.exception("platform tonpays receipt upload failed %s", order_id)
        await state.clear()
        await msg.answer(f"❌ ارسالِ فیش ناموفق بود: {e}", reply_markup=_receipt_kb(order_id))
        return
    await state.clear()
    _NEED_ACTION_NOTIFIED.discard(order_id)
    _st = (res or {}).get("status")
    await msg.answer(
        "✅ <b>فیش دریافت شد.</b>\nبعد از بررسی و تأییدِ درگاه، مبلغ خودکار به "
        "کیف پولت اضافه می‌شود و همین‌جا خبرت می‌کنم.\n\n"
        f"وضعیت: {_STATUS_FA_SHARED.get(_st, _st or 'در حال بررسی')}",
        parse_mode="HTML")
    asyncio.create_task(_auto_poll(bot, order_id, msg.chat.id))


@platform_tonpays_router.callback_query(F.data.startswith("ptonp:cancel:"))
async def ptonp_cancel(cb: CallbackQuery):
    order_id = cb.data.split(":")[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.order_id == order_id))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        # لغوِ اتمی: نوشتنِ بی‌شرط می‌توانست وضعیتِ «paid» را که هم‌زمان ثبت
        # شده بود به «cancelled» برگرداند و فاکتور دوباره قابلِ تسویه می‌شد
        res = await s.execute(
            sa_update(PlatformTonPaysCharge)
            .where(PlatformTonPaysCharge.order_id == order_id,
                   PlatformTonPaysCharge.status != "paid")
            .values(status="cancelled"))
        await s.commit()
        if res.rowcount == 0:
            await cb.answer("این فاکتور قبلاً پرداخت شده.", show_alert=True)
            return
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    await cb.message.answer(
        "❌ فاکتور لغو شد.\n\n⚠️ اگر قبلِ لغو واریز کرده بودی، با پشتیبانی تماس بگیر.")


# ═══════════════ پیگیریِ خودکار ═══════════════

async def _auto_poll(bot: Bot, order_id: str, chat_id: int) -> None:
    for delay in _POLL_SCHEDULE:
        await asyncio.sleep(delay)
        try:
            async with Session() as s:
                ch = await s.scalar(select(PlatformTonPaysCharge).where(
                    PlatformTonPaysCharge.order_id == order_id))
            if not ch or ch.status != "waiting":
                return
            ok, _note, credit = await settle_charge(order_id)
            if ok:
                await bot.send_message(
                    chat_id,
                    "✅ <b>پرداختت تایید شد!</b>\n\n"
                    f"مبلغِ <b>{_rial(credit)}</b> به کیف پولت اضافه شد.",
                    parse_mode="HTML")
                return
            # === PATCH_TONPAYS_CUSTOM_V1 === تأییدِ خودکار نشد ⇒ از خریدار فیش بخواه
            if (is_custom_order(order_id) and _LAST_STATUS.get(order_id) == "need_action"
                    and order_id not in _NEED_ACTION_NOTIFIED):
                _NEED_ACTION_NOTIFIED.add(order_id)
                await bot.send_message(
                    chat_id, NEED_ACTION_TEXT.format(oid=order_id),
                    parse_mode="HTML", reply_markup=_receipt_kb(order_id))
        except Exception:
            logger.exception("platform tonpays auto-poll %s", order_id)


async def poll_pending_once() -> None:
    """جابِ دوره‌ای — فاکتورهایِ باز را بعد از ری‌استارت هم دنبال می‌کند."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_AFTER_MIN)
    async with Session() as s:
        pend = (await s.scalars(select(PlatformTonPaysCharge).where(
            PlatformTonPaysCharge.status == "waiting",
            PlatformTonPaysCharge.created_at >= cutoff))).all()
        items = [(c.order_id, c.user_telegram_id) for c in pend]
    if not items:
        return
    from config import PLATFORM_BOT_TOKEN
    if not PLATFORM_BOT_TOKEN:
        return
    from aiogram import Bot as _Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    for order_id, tg_id in items:
        try:
            ok, _note, credit = await settle_charge(order_id)
        except Exception:
            logger.exception("platform tonpays sweeper %s", order_id)
            continue
        # === PATCH_TONPAYS_CUSTOM_V1 === need_action ⇒ یک‌بار از خریدار فیش بخواه
        if (not ok and is_custom_order(order_id) and _LAST_STATUS.get(order_id) == "need_action"
                and order_id not in _NEED_ACTION_NOTIFIED):
            _NEED_ACTION_NOTIFIED.add(order_id)
            _nb = _Bot(token=PLATFORM_BOT_TOKEN,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await _nb.send_message(tg_id, NEED_ACTION_TEXT.format(oid=order_id),
                                       reply_markup=_receipt_kb(order_id))
            except Exception:
                logger.exception("platform tonpays sweeper need_action notify %s", order_id)
            finally:
                await _nb.session.close()
        if not ok or credit <= 0:
            continue
        tbot = _Bot(token=PLATFORM_BOT_TOKEN,
                    default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tbot.send_message(
                tg_id, "✅ <b>پرداختت تایید شد!</b>\n\n"
                       f"مبلغِ <b>{_rial(credit)}</b> به کیف پولت اضافه شد.")
        except Exception:
            logger.exception("platform tonpays sweeper notify %s", order_id)
        finally:
            await tbot.session.close()
