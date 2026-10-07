"""
platform_blu.py — درگاه پرداخت «بلو / بلوپال» برایِ رباتِ بات‌ساز
(PATCH_BLU_PLATFORM_V1).

مستنداتِ رسمی: https://blupal.net/documentation
  POST /api/v1/invoices/create        {amount(ریال), card_number?}
  GET  /api/v1/invoices/{invoice_id}  → status: PENDING/PAID/EXPIRED/CANCELED
  POST /api/v1/sandbox/invoices/{id}/simulate   (فقط با کلیدِ blu_test_)
  احراز هویت: هدر X-API-Key

سه نکته‌ای که کلِ طراحیِ این ماژول را شکل داده:

۱) **مبلغ به ریال است، نه تومان.** کیف پولِ بات‌ساز تومانی است، پس هرجا با
   بلو حرف می‌زنیم ریال و هرجا با کاربر، تومان. هر دو مقدار در دیتابیس ذخیره
   می‌شود تا هیچ‌جا تبدیلِ حدسی لازم نباشد.

۲) **وبهوک امضا ندارد** (نه HMAC، نه هیچ توکنی) و آدرسش در داشبوردِ خودِ بلو
   تنظیم می‌شود، نه هنگامِ ساختِ فاکتور. یعنی هرکسی که آدرسِ وبهوکِ ما را
   بداند می‌تواند یک POST جعلیِ «PAID» بفرستد. برایِ همین بدنهٔ وبهوک اینجا
   **هرگز ملاکِ شارژ نیست**؛ فقط invoice_id از آن خوانده می‌شود و بعد
   مستقیماً از خودِ بلو استعلام می‌گیریم.

۳) **وبهوک ممکن است چند بار بیاید** (خودشان تا ۳ بار retry می‌کنند)، پس شارژ
   باید idempotent باشد — با قفلِ اتمی روی وضعیتِ فاکتور انجام شده است.
"""
import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, update as sa_update

from config import Session, WEBHOOK_BASE
from logging_setup import get_logger as _get_logger
from models import PlatformBluCharge, PlatformTxn, PlatformUser

logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

API_BASE = "https://blupal.net/api"
_TIMEOUT = 20

# حداقلِ مبلغ طبقِ مستندات: ۱۰۰٬۰۰۰ ریال = ۱۰٬۰۰۰ تومان
MIN_RIAL = 100_000
MIN_TOMAN = MIN_RIAL // 10

# نامی که *کاربر* می‌بیند. عمداً اسمِ سرویس‌دهنده نیست تا اگر روزی درگاه عوض
# شد، تجربهٔ کاربر تغییر نکند.
USER_LABEL = "💳 کارت به کارت هوشمند"

# مبالغِ پیشنهادیِ آماده (تومان) — برایِ اینکه کاربر مجبور نباشد تایپ کند.
PRESET_AMOUNTS = [150_000, 250_000, 350_000, 500_000]

STALE_AFTER_MIN = 60
_POLL_SCHEDULE = [15, 15, 15, 15, 30, 30, 30, 60, 60, 60, 90, 90, 120, 120, 180, 180]

blu_router = Router(name="platform_blu")


class BluFSM(StatesGroup):
    api_key = State()
    amount = State()
    card = State()


def _toman(n) -> str:
    return f"{int(n):,} تومان"


def _callback_url() -> str | None:
    return f"{WEBHOOK_BASE}/blu/callback" if WEBHOOK_BASE else None


async def _is_super(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return uid == PLATFORM_ADMIN_ID


# ═══════════════════════ کلاینتِ API ═══════════════════════

async def _request(method: str, path: str, api_key: str,
                   json_body: dict | None = None) -> tuple[int, dict]:
    if aiohttp is None:
        raise RuntimeError("aiohttp نصب نیست.")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, API_BASE + path,
                             headers=headers, json=json_body) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:300]}
            return r.status, (data if isinstance(data, dict) else {"data": data})


_ERR_FA = {
    "unauthorized": "کلید API نامعتبر است یا وجود ندارد.",
    "amount_required": "مبلغ ارسال نشده است.",
    "amount_too_low": f"مبلغ کمتر از حداقلِ مجاز است ({MIN_TOMAN:,} تومان).",
    "no_active_card": "هیچ کارتِ فعالی در حسابِ بلو ثبت نشده — از داشبوردِ بلو "
                      "یک کارت اضافه و فعال کن.",
    "invalid_invoice_id": "شناسهٔ فاکتور نامعتبر است.",
    "not_found": "فاکتور پیدا نشد.",
    "mode_mismatch": "کلید و فاکتور هم‌حالت نیستند (تست در برابرِ اصلی).",
    "invalid_state": "فاکتور در وضعیتِ مناسب نیست.",
}


def _err_text(data: dict, status: int) -> str:
    """پیامِ خطایِ خوانا از پاسخِ بلو. قالبِ خطا: {success:false, error, message}"""
    code = (data or {}).get("error")
    if code and code in _ERR_FA:
        return _ERR_FA[code]
    msg = (data or {}).get("message")
    if msg:
        return str(msg)
    if code:
        return str(code)
    return f"خطای HTTP {status}"


def is_sandbox_key(key: str) -> bool:
    return (key or "").startswith("blu_test_")


async def create_invoice(api_key: str, amount_rial: int,
                         card_number: str | None = None) -> dict:
    body: dict = {"amount": int(amount_rial)}
    if card_number:
        body["card_number"] = card_number
    status, data = await _request("POST", "/v1/invoices/create", api_key, body)
    if status in (200, 201) and data.get("success"):
        return data
    raise RuntimeError(_err_text(data, status))


async def get_invoice(api_key: str, invoice_id: int) -> dict:
    """استعلامِ مستقلِ وضعیت — تنها منبعِ معتبر برایِ تاییدِ پرداخت."""
    status, data = await _request("GET", f"/v1/invoices/{int(invoice_id)}", api_key)
    if status == 200 and data.get("success"):
        return data
    raise RuntimeError(_err_text(data, status))


async def simulate_payment(api_key: str, invoice_id: int,
                           scenario: str = "success") -> dict:
    """فقط در حالتِ Sandbox — برایِ تستِ کلِ مسیر بدونِ پولِ واقعی."""
    status, data = await _request(
        "POST", f"/v1/sandbox/invoices/{int(invoice_id)}/simulate",
        api_key, {"scenario": scenario})
    if status == 200 and data.get("success"):
        return data
    raise RuntimeError(_err_text(data, status))


async def validate_api_key(api_key: str) -> tuple[bool, str]:
    """اعتبارسنجیِ کلید با یک استعلامِ بی‌خطر رویِ فاکتورِ ناموجود.

    عمداً فاکتورِ آزمایشی نمی‌سازیم تا حسابِ کاربر پر از فاکتورِ بی‌مصرف نشود.
    کلیدِ بد ۴۰۱ می‌دهد، کلیدِ خوب ۴۰۴ (فاکتور پیدا نشد).
    """
    if not api_key.startswith(("blu_test_", "blu_live_")):
        return False, ("قالبِ کلید درست نیست؛ کلیدِ بلو با blu_test_ یا "
                       "blu_live_ شروع می‌شود.")
    try:
        status, data = await _request("GET", "/v1/invoices/999999999", api_key)
    except Exception as e:
        return False, f"خطا در ارتباط با بلو: {e}"
    if status == 401 or (data or {}).get("error") == "unauthorized":
        return False, "کلید API پذیرفته نشد."
    if status in (200, 400, 404):
        mode = "آزمایشی (Sandbox)" if is_sandbox_key(api_key) else "اصلی (Live)"
        return True, f"کلید معتبر است — حالت: {mode}"
    return False, f"پاسخِ غیرمنتظره از بلو (HTTP {status})."


# ═══════════════ هستهٔ امن: تایید و شارژ ═══════════════

async def settle_charge(invoice_id: int) -> tuple[bool, str, int]:
    """تنها نقطه‌ای که موجودی را زیاد می‌کند.

    همیشه با استعلامِ مستقل از بلو تایید می‌گیرد و مبلغ را با آنچه خودمان
    ثبت کرده‌ایم مقایسه می‌کند. idempotent است (وبهوکِ تکراری ضرری ندارد).
    """
    from platform_bot import get_pset
    async with Session() as s:
        ch = await s.scalar(select(PlatformBluCharge).where(
            PlatformBluCharge.invoice_id == int(invoice_id)))
        if not ch:
            return False, "فاکتور پیدا نشد.", 0
        if ch.status == "PAID":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        expected_rial, payer_tg = int(ch.amount_rial), int(ch.user_telegram_id)
        credit_toman = int(ch.amount_toman)

    api_key = await get_pset("blu_api_key", "")
    if not api_key:
        return False, "درگاه تنظیم نشده است.", 0

    try:
        info = await get_invoice(api_key, invoice_id)
    except Exception as e:
        logger.warning("blu get_invoice failed %s: %s", invoice_id, e)
        return False, f"استعلام از بلو ناموفق بود: {e}", 0

    st = (info.get("status") or "").upper()
    if st != "PAID":
        human = {"PENDING": "هنوز پرداخت نشده", "EXPIRED": "فاکتور منقضی شده",
                 "CANCELED": "فاکتور لغو شده"}.get(st, f"وضعیت: {st or 'نامشخص'}")
        return False, human + ".", 0

    reported = int(info.get("amount") or 0)
    if reported and reported < expected_rial:
        logger.error("blu amount mismatch %s: expected %s got %s",
                     invoice_id, expected_rial, reported)
        return False, "مبلغِ اعلام‌شده با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

    async with Session() as s:
        claim = await s.execute(
            sa_update(PlatformBluCharge)
            .where(PlatformBluCharge.invoice_id == int(invoice_id),
                   PlatformBluCharge.status != "PAID")
            .values(status="PAID"))
        if claim.rowcount == 0:
            await s.rollback()
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        from platform_bot import get_user
        u = await get_user(s, payer_tg, for_update=True)
        if u:
            u.balance = int(u.balance or 0) + credit_toman
            s.add(PlatformTxn(user_telegram_id=payer_tg, amount=credit_toman,
                              kind="charge", note="شارژ با درگاه بلو"))
        await s.commit()
    return True, "پرداخت تایید و کیف پول شارژ شد.", credit_toman


# ═══════════════════════ تنظیماتِ ادمین ═══════════════════════

DISCLAIMER = (
    "📌 <b>سلبِ مسئولیت:</b> «بلو / بلوپال» یک سرویسِ شخصِ ثالث است و متعلق به "
    "بات‌ساز نیست و توسطِ بات‌ساز اداره یا تایید نمی‌شود. اگر این درگاه از نظرِ "
    "امنیتی یا مالی مشکلی داشته باشد — از جمله پرداختِ تاییدنشده، تسویه‌نشدنِ "
    "وجه یا تعطیل‌شدنِ سرویس — <b>بات‌ساز هیچ مسئولیتی نمی‌پذیرد</b>. استفاده "
    "از آن اختیاری و با ریسکِ خودِ شماست.\n\n"
    "⚠️ <b>نکتهٔ امنیتی:</b> وبهوکِ بلو امضایِ رمزنگاری‌شده ندارد، پس ربات "
    "هیچ‌وقت به آن اعتماد نمی‌کند و هر پرداخت را مستقیم از خودِ بلو دوباره "
    "استعلام می‌گیرد و بعد شارژ می‌کند."
)


async def _menu_kb() -> InlineKeyboardMarkup:
    from platform_bot import get_pset
    from premium_emoji import hx_button
    key = await get_pset("blu_api_key", "")
    on = (await get_pset("blu_enabled", "off")) == "on"
    rows = [
        [hx_button("🔑 ثبت / تغییر کلید API", callback_data="blu:setkey")],
        [hx_button("🧪 تست اتصال", callback_data="blu:test")],
    ]
    if key:
        rows.append([hx_button(("🔵 درگاه: ✅ فعال" if on else "🔵 درگاه: ❌ غیرفعال"),
                               callback_data="blu:toggle",
                               style=(ButtonStyle.SUCCESS if on else ButtonStyle.DANGER))])
    else:
        rows.append([hx_button("⚠️ اول کلید API را ثبت کن", callback_data="blu:setkey")])
    if on:
        card = await get_pset("blu_card_number", "")
        rows.append([hx_button("🏦 کارتِ دلخواهِ واریز: " + (card or "پیش‌فرضِ حساب"),
                               callback_data="blu:setcard")])
        # === PATCH_BLU_VISIBILITY_V1 ===
        in_wallet = (await get_pset("blu_show_wallet", "on")) == "on"
        in_buy = (await get_pset("blu_show_purchase", "on")) == "on"
        rows.append([hx_button(
            "👛 نمایش در کیف پول: " + ("✅ فعال" if in_wallet else "❌ غیرفعال"),
            callback_data="blu:vis:wallet")])
        rows.append([hx_button(
            "🛒 نمایش در پرداخت محصول: " + ("✅ فعال" if in_buy else "❌ غیرفعال"),
            callback_data="blu:vis:purchase")])
        rows.append([hx_button("🔗 آدرسِ وبهوک (باید در داشبوردِ بلو ثبت شود)",
                               callback_data="blu:hook")])
        if is_sandbox_key(key):
            rows.append([hx_button("🧪 راهنمای تستِ Sandbox", callback_data="blu:sbhelp")])
    rows.append([hx_button("⬅️ بازگشت به پنل", callback_data="adm:back",
                           style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@blu_router.callback_query(F.data.in_({"set:blu", "blu:menu"}))
async def blu_menu(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.clear()
    key = await get_pset("blu_api_key", "")
    on = (await get_pset("blu_enabled", "off")) == "on"
    masked = (key[:12] + "…" + key[-4:]) if len(key) > 20 else (key or "ثبت نشده")
    mode = ("🧪 آزمایشی (Sandbox)" if is_sandbox_key(key)
            else "🟢 اصلی (Live)") if key else "—"
    txt = (
        "🔵 <b>درگاه بلو (blupal.net)</b>\n\n"
        "درگاهِ کارت‌به‌کارتِ خودکار. کاربر مبلغِ یکتا را به کارت واریز می‌کند و "
        "بلو واریز را خودکار تشخیص می‌دهد.\n\n"
        f"کلید API: <code>{masked}</code>\n"
        f"حالت: {mode}\n"
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
        f"حداقلِ مبلغ: <b>{MIN_TOMAN:,} تومان</b>\n\n"
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


@blu_router.callback_query(F.data == "blu:hook")
async def blu_hook(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    url = _callback_url()
    if not url:
        await cb.message.answer(
            "⚠️ <code>WEBHOOK_BASE</code> رویِ سرور تنظیم نشده، پس آدرسِ وبهوک "
            "ساخته نمی‌شود.\n\nنگران نباش: ربات مستقل از وبهوک هم هر فاکتور را "
            "خودکار پیگیری می‌کند، فقط چند ثانیه دیرتر تایید می‌شود.",
            parse_mode="HTML")
        await cb.answer()
        return
    await cb.message.answer(
        "🔗 <b>آدرسِ وبهوک</b>\n\n"
        "برخلافِ بعضی درگاه‌ها، بلو آدرسِ وبهوک را هنگامِ ساختِ فاکتور نمی‌گیرد؛ "
        "باید یک‌بار در داشبوردِ خودشان ثبتش کنی:\n\n"
        "داشبوردِ بلو ← «مدیریت API Key» ← کلیدت را باز کن ← فیلدِ "
        "<b>Webhook URL</b> را با این پر کن:\n\n"
        f"<code>{url}</code>\n\n"
        "💡 اگر ثبتش نکنی هم چیزی خراب نمی‌شود — ربات خودش هر فاکتور را "
        "پیگیری می‌کند. وبهوک فقط تایید را چند ثانیه سریع‌تر می‌کند.",
        parse_mode="HTML")
    await cb.answer()


@blu_router.callback_query(F.data == "blu:sbhelp")
async def blu_sbhelp(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await cb.message.answer(
        "🧪 <b>حالتِ آزمایشی فعال است</b>\n\n"
        "کلیدت با <code>blu_test_</code> شروع می‌شود، یعنی هیچ پولِ واقعی "
        "جابه‌جا نمی‌شود. می‌توانی کلِ مسیر را تست کنی: یک شارژ شروع کن، و بعد "
        "رویِ فاکتور دکمهٔ «🧪 شبیه‌سازی پرداخت» را بزن تا ببینی شارژ درست "
        "انجام می‌شود.\n\n"
        "⚠️ برایِ استفادهٔ واقعی، کلیدِ <code>blu_live_</code> را جایگزین کن.",
        parse_mode="HTML")
    await cb.answer()


@blu_router.callback_query(F.data == "blu:setkey")
async def blu_setkey(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(BluFSM.api_key)
    await cb.message.answer(
        "🔑 کلید API را از داشبوردِ بلو (بخشِ «مدیریت API Key») کپی کن و بفرست.\n\n"
        "برایِ تست بدونِ پولِ واقعی از کلیدِ <code>blu_test_...</code> و برایِ "
        "استفادهٔ واقعی از <code>blu_live_...</code> استفاده کن.",
        parse_mode="HTML")
    await cb.answer()


@blu_router.message(BluFSM.api_key, F.text)
async def blu_setkey_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    key = (msg.text or "").strip()
    await msg.answer("⏳ در حال بررسی کلید…")
    ok, note = await validate_api_key(key)
    if not ok:
        await msg.answer(f"❌ {note}\n\nدوباره امتحان کن یا لغو کن.")
        return
    await set_pset("blu_api_key", key)
    await state.clear()
    await msg.answer(
        f"✅ {note}\nکلید ذخیره شد. حالا درگاه را فعال کن و آدرسِ وبهوک را در "
        "داشبوردِ بلو ثبت کن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔵 بازگشت به درگاه", callback_data="blu:menu")]]))


@blu_router.callback_query(F.data == "blu:test")
async def blu_test(cb: CallbackQuery):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    key = await get_pset("blu_api_key", "")
    if not key:
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    await cb.answer("در حال تست…")
    ok, note = await validate_api_key(key)
    await cb.message.answer(("✅ " if ok else "❌ ") + note)


@blu_router.callback_query(F.data == "blu:toggle")
async def blu_toggle(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset, set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    if not await get_pset("blu_api_key", ""):
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    cur = await get_pset("blu_enabled", "off")
    await set_pset("blu_enabled", "off" if cur == "on" else "on")
    await blu_menu(cb, state)


@blu_router.callback_query(F.data.startswith("blu:vis:"))
async def blu_visibility(cb: CallbackQuery, state: FSMContext):
    """روشن/خاموش‌کردنِ نمایشِ درگاه در «کیف پول» یا «پرداختِ محصول»."""
    from platform_bot import get_pset, set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    which = cb.data.split(":")[2]
    key = "blu_show_wallet" if which == "wallet" else "blu_show_purchase"
    cur = await get_pset(key, "on")
    await set_pset(key, "off" if cur == "on" else "on")
    await blu_menu(cb, state)


@blu_router.callback_query(F.data == "blu:setcard")
async def blu_setcard(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(BluFSM.card)
    await cb.message.answer(
        "🏦 اگر می‌خواهی واریزها به کارتِ مشخصی برود، شمارهٔ آن کارت را بفرست "
        "(۱۶ رقم).\n\nباید یکی از کارت‌هایِ <b>فعالِ حسابِ بلوی خودت</b> باشد؛ "
        "اگر با کارت‌هایِ حسابت نخواند، بلو خودش یک کارتِ فعال انتخاب می‌کند.\n\n"
        "برایِ استفاده از کارتِ پیش‌فرضِ حساب، <code>0</code> بفرست.",
        parse_mode="HTML")
    await cb.answer()


@blu_router.message(BluFSM.card, F.text)
async def blu_setcard_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw == "0":
        await set_pset("blu_card_number", "")
        await state.clear()
        await msg.answer("✅ کارتِ دلخواه پاک شد؛ از کارتِ پیش‌فرضِ حساب استفاده می‌شود.")
        return
    digits = "".join(c for c in raw.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")) if c.isdigit())
    if len(digits) != 16:
        await msg.answer("شمارهٔ کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست:")
        return
    await set_pset("blu_card_number", digits)
    await state.clear()
    await msg.answer("✅ ذخیره شد.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="🔵 بازگشت به درگاه",
                                               callback_data="blu:menu")]]))


# ═══════════════════════ جریانِ کاربر ═══════════════════════

async def is_available(where: str = "") -> bool:
    """آیا درگاه در دسترس است؟

    where می‌تواند "wallet" یا "purchase" باشد تا تاگلِ نمایشِ همان بخش هم
    چک شود؛ بدونِ آن فقط فعال‌بودنِ خودِ درگاه بررسی می‌شود.
    """
    from platform_bot import get_pset
    if (await get_pset("blu_enabled", "off")) != "on":
        return False
    if not await get_pset("blu_api_key", ""):
        return False
    if where == "wallet":
        return (await get_pset("blu_show_wallet", "on")) == "on"
    if where == "purchase":
        return (await get_pset("blu_show_purchase", "on")) == "on"
    return True


def _amount_kb() -> InlineKeyboardMarkup:
    """صفحهٔ انتخابِ مبلغ: چند مبلغِ آمادهٔ پرکاربرد + مبلغِ دلخواه + بازگشت."""
    rows, pair = [], []
    for amt in PRESET_AMOUNTS:
        pair.append(InlineKeyboardButton(text=f"{amt // 1000:,} هزار تومان",
                                         callback_data=f"blu:amt:{amt}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه",
                                      callback_data="blu:amt:custom")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="blu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _amount_text() -> str:
    return (
        f"{USER_LABEL}\n\n"
        "مبلغی که می‌خواهی کیف پولت شارژ شود را انتخاب کن، یا «مبلغ دلخواه» را "
        "بزن و عدد خودت را بفرست.\n\n"
        f"⚠️ کمتر از <b>{MIN_TOMAN:,} تومان</b> امکان‌پذیر نیست.\n\n"
        "<i>این یک درگاهِ کارت‌به‌کارتِ خودکارِ شخصِ ثالث است؛ استفاده از آن "
        "اختیاری است.</i>"
    )


@blu_router.callback_query(F.data == "charge_blu")
async def charge_blu(cb: CallbackQuery, state: FSMContext):
    if not await is_available("wallet"):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    try:
        await cb.message.edit_text(_amount_text(), reply_markup=_amount_kb(),
                                   parse_mode="HTML")
    except Exception:
        await cb.message.answer(_amount_text(), reply_markup=_amount_kb(),
                                parse_mode="HTML")
    await cb.answer()


@blu_router.callback_query(F.data == "charge_blu_buy")
async def charge_blu_buy(cb: CallbackQuery, state: FSMContext):
    """همان صفحه، ولی از مسیرِ «پرداختِ محصول» (کمبودِ موجودی هنگامِ خرید)."""
    if not await is_available("purchase"):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    await cb.message.answer(_amount_text(), reply_markup=_amount_kb(),
                            parse_mode="HTML")
    await cb.answer()


@blu_router.callback_query(F.data == "blu:back")
async def blu_back(cb: CallbackQuery, state: FSMContext):
    """بازگشت از صفحهٔ انتخابِ مبلغ به کیف پول."""
    await state.clear()
    try:
        from platform_bot import _platform_wallet_kb
        await cb.message.edit_text(
            "💰 <b>کیف پول</b>\n\nیکی از روش‌های شارژ را انتخاب کن:",
            reply_markup=await _platform_wallet_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer("برگشتیم. از منوی «💰 کیف پول» ادامه بده.")
    await cb.answer()


@blu_router.callback_query(F.data.startswith("blu:amt:"))
async def blu_amount_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await is_available():
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    what = cb.data.split(":")[2]
    if what == "custom":
        await state.set_state(BluFSM.amount)
        await cb.message.answer(
            "✏️ مبلغِ موردنظرت را به <b>تومان</b> بفرست "
            f"(حداقل <b>{MIN_TOMAN:,} تومان</b>):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="charge_blu")]]),
            parse_mode="HTML")
        await cb.answer()
        return
    try:
        amount = int(what)
    except ValueError:
        await cb.answer()
        return
    if amount < MIN_TOMAN:
        await cb.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    await state.clear()
    await cb.answer("در حال ساختِ فاکتور…")
    await _make_invoice(cb.message, bot, cb.from_user.id, amount)


@blu_router.message(BluFSM.amount, F.text)
async def blu_amount(msg: Message, bot: Bot, state: FSMContext):
    from platform_bot import fa_to_int
    amount = fa_to_int(msg.text)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست:")
        return
    if amount < MIN_TOMAN:
        await state.set_state(BluFSM.amount)
        await msg.answer(
            f"❌ کمتر از <b>{MIN_TOMAN:,} تومان</b> امکان‌پذیر نیست.\n"
            "یک مبلغِ بیشتر بفرست:", parse_mode="HTML")
        return
    await state.clear()
    await _make_invoice(msg, bot, msg.from_user.id, amount)


async def _make_invoice(target: Message, bot: Bot, user_id: int, amount: int):
    """ساختِ فاکتور و نمایشِ آن. هم از دکمه‌هایِ آماده و هم از مبلغِ دلخواه
    صدا زده می‌شود تا منطق فقط یک‌جا باشد."""
    from platform_bot import get_pset
    msg = target
    api_key = await get_pset("blu_api_key", "")
    card_pref = await get_pset("blu_card_number", "") or None
    amount_rial = amount * 10          # بلو ریالی کار می‌کند
    await msg.answer("⏳ در حال ساختِ فاکتور…")
    try:
        inv = await create_invoice(api_key, amount_rial, card_pref)
    except Exception as e:
        await msg.answer(f"❌ ساختِ فاکتور ناموفق بود.\n{e}")
        return

    invoice_id = int(inv.get("invoice_id") or 0)
    if not invoice_id:
        await msg.answer("❌ ساختِ فاکتور ناموفق بود (شناسهٔ فاکتور برنگشت). دوباره امتحان کن.")
        return
    final_rial = int(inv.get("final_amount") or amount_rial)
    card = inv.get("card_number") or ""
    mode = inv.get("mode") or "live"
    pay_link = inv.get("payment_link") or ""

    async with Session() as s:
        s.add(PlatformBluCharge(
            user_telegram_id=user_id, invoice_id=invoice_id,
            amount_toman=amount, amount_rial=amount_rial,
            final_amount_rial=final_rial, card_number=card,
            mode=mode, status="PENDING"))
        await s.commit()

    lines = [
        "🔵 <b>فاکتور شارژ کیف پول</b>\n",
        f"مبلغِ شارژ: <b>{_toman(amount)}</b>",
        f"شمارهٔ فاکتور: <code>{invoice_id}</code>\n",
    ]
    if card:
        lines += ["💳 <b>شمارهٔ کارت</b> (برایِ کپی رویش بزن):", f"<code>{card}</code>"]
    lines += [
        "\n💵 <b>مبلغِ دقیقِ واریزی</b> (رویِ عدد بزن تا کپی شود):",
        f"<code>{final_rial}</code>",
        f"یعنی <b>{final_rial:,} ریال</b>",
        "\n⚠️ حتماً <b>دقیقاً همین عدد</b> را واریز کن. مبلغ عمداً چند ریال با "
        "مبلغِ شارژ فرق دارد تا واریزِ تو خودکار شناسایی شود؛ اگر عددِ گرد یا "
        "متفاوتی بفرستی، پرداخت تشخیص داده نمی‌شود.",
        "\n🔄 بعد از واریز همین‌جا منتظر بمان — وضعیت خودکار بررسی می‌شود.",
    ]
    if inv.get("expires_at"):
        lines.append(f"\n⏳ مهلتِ اعتبارِ فاکتور: <code>{inv['expires_at']}</code>")
    if mode == "sandbox":
        lines.append("\n🧪 <b>حالتِ آزمایشی</b> — پولِ واقعی جابه‌جا نمی‌شود.")

    rows = [[InlineKeyboardButton(text="✅ پرداخت کردم، بررسی کن",
                                  callback_data=f"blu:verify:{invoice_id}")]]
    if not card and pay_link:
        rows.insert(0, [InlineKeyboardButton(text="🧾 صفحهٔ پرداخت", url=pay_link)])
    if mode == "sandbox":
        rows.append([InlineKeyboardButton(text="🧪 شبیه‌سازی پرداخت (تست)",
                                          callback_data=f"blu:sim:{invoice_id}")])
    rows.append([InlineKeyboardButton(text="❌ لغو این فاکتور",
                                      callback_data=f"blu:cancel:{invoice_id}")])
    await msg.answer("\n".join(lines), parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    asyncio.create_task(_auto_poll(bot, invoice_id, msg.chat.id))


@blu_router.callback_query(F.data.startswith("blu:verify:"))
async def blu_verify(cb: CallbackQuery):
    invoice_id = int(cb.data.split(":")[2])
    await cb.answer("در حال استعلام از بلو…")
    ok, note, credit = await settle_charge(invoice_id)
    if ok:
        await cb.message.answer(
            f"✅ {note}\n\nمبلغِ <b>{_toman(credit)}</b> به کیف پولت اضافه شد.",
            parse_mode="HTML")
    else:
        await cb.message.answer(f"ℹ️ {note}")


@blu_router.callback_query(F.data.startswith("blu:sim:"))
async def blu_sim(cb: CallbackQuery):
    """شبیه‌سازیِ پرداخت — فقط در حالتِ Sandbox و فقط برایِ سوپرادمین."""
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین می‌تواند شبیه‌سازی کند.", show_alert=True)
        return
    invoice_id = int(cb.data.split(":")[2])
    key = await get_pset("blu_api_key", "")
    if not is_sandbox_key(key):
        await cb.answer("شبیه‌سازی فقط با کلیدِ آزمایشی ممکن است.", show_alert=True)
        return
    await cb.answer("در حال شبیه‌سازی…")
    try:
        await simulate_payment(key, invoice_id, "success")
    except Exception as e:
        await cb.message.answer(f"❌ شبیه‌سازی ناموفق: {e}")
        return
    ok, note, credit = await settle_charge(invoice_id)
    await cb.message.answer(
        (f"✅ تست موفق بود — {_toman(credit)} شارژ شد." if ok else f"ℹ️ {note}"))


@blu_router.callback_query(F.data.startswith("blu:cancel:"))
async def blu_cancel(cb: CallbackQuery):
    invoice_id = int(cb.data.split(":")[2])
    async with Session() as s:
        ch = await s.scalar(select(PlatformBluCharge).where(
            PlatformBluCharge.invoice_id == invoice_id))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        # لغوِ اتمی: نوشتنِ بی‌شرط می‌توانست «PAID»ِ هم‌زمان را بازنویسی کند
        res = await s.execute(
            sa_update(PlatformBluCharge)
            .where(PlatformBluCharge.invoice_id == invoice_id,
                   PlatformBluCharge.status != "PAID")
            .values(status="CANCELED"))
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

async def _auto_poll(bot: Bot, invoice_id: int, chat_id: int) -> None:
    for delay in _POLL_SCHEDULE:
        await asyncio.sleep(delay)
        try:
            async with Session() as s:
                ch = await s.scalar(select(PlatformBluCharge).where(
                    PlatformBluCharge.invoice_id == invoice_id))
            if not ch or ch.status != "PENDING":
                return
            ok, _note, credit = await settle_charge(invoice_id)
            if ok:
                await bot.send_message(
                    chat_id,
                    "✅ <b>پرداختت تایید شد!</b>\n\n"
                    f"مبلغِ <b>{_toman(credit)}</b> به کیف پولت اضافه شد.",
                    parse_mode="HTML")
                return
        except Exception:
            logger.exception("blu auto-poll %s", invoice_id)


async def poll_pending_once() -> None:
    """جابِ دوره‌ای — فاکتورهایِ باز را بعد از ری‌استارت هم دنبال می‌کند."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=STALE_AFTER_MIN)
    async with Session() as s:
        pend = (await s.scalars(select(PlatformBluCharge).where(
            PlatformBluCharge.status == "PENDING",
            PlatformBluCharge.created_at >= cutoff))).all()
        items = [(c.invoice_id, c.user_telegram_id) for c in pend]
    if not items:
        return
    from config import PLATFORM_BOT_TOKEN
    if not PLATFORM_BOT_TOKEN:
        return
    from aiogram import Bot as _Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    for invoice_id, tg_id in items:
        try:
            ok, _note, credit = await settle_charge(invoice_id)
        except Exception:
            logger.exception("blu sweeper %s", invoice_id)
            continue
        if not ok or credit <= 0:
            continue
        tbot = _Bot(token=PLATFORM_BOT_TOKEN,
                    default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tbot.send_message(
                tg_id, "✅ <b>پرداختت تایید شد!</b>\n\n"
                       f"مبلغِ <b>{_toman(credit)}</b> به کیف پولت اضافه شد.")
        except Exception:
            logger.exception("blu sweeper notify %s", invoice_id)
        finally:
            await tbot.session.close()
