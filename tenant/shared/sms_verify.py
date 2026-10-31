"""
sms_verify.py — تأییدِ شماره با «کدِ پیامکی» (PATCH_SMS_VERIFY_V1).

روشِ پیش‌فرضِ تأیید همان «اشتراکِ مخاطبِ تلگرام» است؛ اگر مالک «پنل پیامکی» وصل کند و روشِ تأیید را «پیامک» بگذارد،
کاربر شمارهٔ ایرانی‌اش را می‌نویسد، یک کدِ ۵رقمی با پنلِ پیامکیِ مالک برایش می‌رود و با واردکردنِ کد تأیید می‌شود.
همان گیت‌هایِ موجود (تأیید در ورود / پیش از پرداخت) از همین روش استفاده می‌کنند؛ خاموش/روشنِ خودِ گیت‌ها تغییر نکرده.

پنل‌هایِ پشتیبانی‌شده: کاوه‌نگار (verify/lookup) و sms.ir (send/verify) — هر دو با «قالبِ تأیید».
محدودیت‌ها: فاصلهٔ ۶۰ ثانیه، حداکثر ۳ ارسال در ۳۰ دقیقه برایِ هر کاربر و ۵ ارسال در روز برایِ هر شماره، اعتبارِ کد ۵ دقیقه،
حداکثر ۵ تلاش؛ یک شماره فقط روی یک حسابِ تاییدشده در همین ربات.
callbackها: phv:sms (دریافتِ کد) · psms:* (تنظیماتِ مالک)
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

sms_verify_router = Router(name="sms_verify")

PROVIDERS = {"kavenegar": "کاوه‌نگار", "smsir": "sms.ir"}
CODE_TTL = 300
SEND_GAP = 60
MAX_TRIES = 5
_SENDS: dict[tuple[int, int], list[float]] = {}        # (bot, user) → زمانِ ارسال‌ها
_NUM_SENDS: dict[tuple[int, str], list[float]] = {}    # (bot, number) → زمانِ ارسال‌ها


class SmsVerifyFSM(StatesGroup):
    number = State()
    code = State()


class SmsCfgFSM(StatesGroup):
    key = State()
    template = State()
    param = State()
    test = State()


_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm_mobile(text: str | None) -> str | None:
    """«۰۹۱۲…» / «+98912…» / «98912…» ⇒ «989121234567»؛ نامعتبر ⇒ None."""
    t = re.sub(r"[\s\-()+]", "", (text or "").translate(_AR))
    if t.startswith("0098"):
        t = t[2:]
    if t.startswith("98") and len(t) == 12:
        t = "0" + t[2:]
    if re.fullmatch(r"09\d{9}", t):
        return "98" + t[1:]
    return None


def local_mobile(phone98: str) -> str:
    return "0" + phone98[2:]


def make_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(5))


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


# ───────────────────────── تنظیمات ─────────────────────────

async def cfg(bot_id: int) -> dict:
    from tenant import get_setting
    return {
        "method": (await get_setting(bot_id, "phone_verify_method", "contact")) or "contact",
        "provider": (await get_setting(bot_id, "sms_provider", "kavenegar")) or "kavenegar",
        "key": (await get_setting(bot_id, "sms_api_key", "") or "").strip(),
        "template": (await get_setting(bot_id, "sms_template", "") or "").strip(),
        "param": (await get_setting(bot_id, "sms_param", "CODE") or "CODE").strip() or "CODE",
    }


def ready(c: dict) -> bool:
    return bool(c["key"] and c["template"] and c["provider"] in PROVIDERS)


async def sms_mode(bot_id: int) -> bool:
    c = await cfg(bot_id)
    return c["method"] == "sms" and ready(c)


# ───────────────────────── ارسال با پنلِ پیامکی ─────────────────────────

async def send_code(c: dict, mobile_local: str, code: str, http=None) -> tuple[bool, str]:
    """(موفق؟, توضیح). http: برایِ تست (async fn(method, url, **kw) → (status, json))."""
    http = http or _http
    try:
        if c["provider"] == "kavenegar":
            url = f"https://api.kavenegar.com/v1/{c['key']}/verify/lookup.json"
            st, js = await http("GET", url, params={"receptor": mobile_local, "token": code, "template": c["template"]})
            ok = st == 200 and isinstance(js, dict) and (js.get("return") or {}).get("status") == 200
            return ok, "" if ok else f"HTTP {st}: {str((js or {}).get('return') or js)[:120]}"
        if c["provider"] == "smsir":
            tid = int(c["template"]) if str(c["template"]).isdigit() else 0
            st, js = await http("POST", "https://api.sms.ir/v1/send/verify",
                                headers={"X-API-KEY": c["key"], "Content-Type": "application/json", "Accept": "application/json"},
                                json={"mobile": mobile_local, "templateId": tid,
                                      "parameters": [{"name": c["param"], "value": code}]})
            ok = st == 200 and isinstance(js, dict) and js.get("status") == 1
            return ok, "" if ok else f"HTTP {st}: {str((js or {}).get('message') or js)[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    return False, "پنلِ پیامکیِ نامعتبر"


async def _http(method: str, url: str, **kw):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.request(method, url, **kw) as r:
            try:
                js = await r.json(content_type=None)
            except Exception:      # noqa: BLE001
                js = None
            return r.status, js


def rate_ok(bot_id: int, uid: int, phone: str, now: float | None = None) -> tuple[bool, str]:
    now = now or time.time()
    mine = [t for t in _SENDS.get((bot_id, uid), []) if now - t < 1800]
    if mine and now - mine[-1] < SEND_GAP:
        return False, f"⏳ {int(SEND_GAP - (now - mine[-1])) + 1} ثانیهٔ دیگر می‌توانی دوباره کد بگیری."
    if len(mine) >= 3:
        return False, "⛔️ تعدادِ درخواستِ کد زیاد بود؛ نیم ساعت دیگر دوباره امتحان کن."
    num = [t for t in _NUM_SENDS.get((bot_id, phone), []) if now - t < 86400]
    if len(num) >= 5:
        return False, "⛔️ برایِ این شماره امروز بیش از حد کد فرستاده شده؛ فردا دوباره امتحان کن."
    _SENDS[(bot_id, uid)] = mine + [now]
    _NUM_SENDS[(bot_id, phone)] = num + [now]
    return True, ""


async def number_taken(bot_id: int, phone: str, uid: int) -> bool:
    async with Session() as s:
        other = await s.scalar(select(BotUser.id).where(
            BotUser.bot_id == bot_id, BotUser.phone_number == phone, BotUser.phone_verified == True,   # noqa: E712
            BotUser.telegram_id != uid))
    return other is not None


# ───────────────────────── مسیرِ کاربر ─────────────────────────

async def sms_prompt(message: Message, bot_id: int, pay: bool = False) -> bool:
    """اگر روشِ تأیید «پیامک» است، دکمهٔ دریافتِ کد را نشان می‌دهد و True برمی‌گرداند؛ وگرنه False (مسیرِ مخاطب)."""
    if not await sms_mode(bot_id):
        return False
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📨 دریافتِ کدِ تأیید با پیامک", callback_data="phv:sms",
                                                                      style=ButtonStyle.SUCCESS)]])
    await message.answer(
        ("📱 <b>تاییدِ شماره‌همراه پیش از پرداخت</b>\n\n" if pay else "📱 <b>تاییدِ شماره‌همراه</b>\n\n")
        + "برایِ ادامه، شمارهٔ موبایلت را وارد کن؛ یک کدِ تأیید با پیامک برایت می‌فرستیم.\n"
          "⚠️ فقط شمارهٔ ایرانی (۰۹…) پذیرفته می‌شود.", reply_markup=kb, parse_mode="HTML")
    return True


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ انصراف", callback_data="phv:cancel",
                                                                        style=ButtonStyle.DANGER)]])


@sms_verify_router.callback_query(F.data == "phv:sms")
async def ask_number(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await sms_mode(row.id):
        return await cb.answer("تأیید با پیامک فعلاً فعال نیست.", show_alert=True)
    await state.set_state(SmsVerifyFSM.number)
    await cb.message.answer("📲 شمارهٔ موبایلت را بفرست (مثلاً <code>09123456789</code>):", reply_markup=_cancel_kb(), parse_mode="HTML")
    await cb.answer()


@sms_verify_router.callback_query(F.data == "phv:cancel")
async def cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("انصراف داده شد.")
    try:
        await cb.message.delete()
    except Exception:      # noqa: BLE001
        pass


@sms_verify_router.message(SmsVerifyFSM.number, F.text)
async def got_number(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    phone = norm_mobile(msg.text)
    if not phone:
        return await msg.answer("❌ شماره معتبر نیست. شمارهٔ ایرانی را مثل <code>09123456789</code> بفرست:", parse_mode="HTML")
    if await number_taken(row.id, phone, msg.from_user.id):
        return await msg.answer("❌ این شماره قبلاً برایِ حسابِ دیگری تأیید شده است.")
    ok, why = rate_ok(row.id, msg.from_user.id, phone)
    if not ok:
        return await msg.answer(why)
    c = await cfg(row.id)
    code = make_code()
    sent, err = await send_code(c, local_mobile(phone), code)
    if not sent:
        logger.warning("sms_verify: ارسالِ کد ناموفق (bot=%s): %s", row.id, err)
        try:
            await bot.send_message(row.owner_id, f"⚠️ ارسالِ کدِ پیامکیِ تأیید ناموفق بود:\n<code>{err}</code>\nتنظیمِ «پنل پیامکی» را بررسی کن.",
                                   parse_mode="HTML")
        except Exception:      # noqa: BLE001
            logger.info("sms_verify: خبرِ خطا به مالک نرسید")
        await state.clear()
        return await msg.answer("❌ ارسالِ پیامک ممکن نشد؛ کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.")
    salt = secrets.token_hex(4)
    await state.set_state(SmsVerifyFSM.code)
    await state.update_data(sv_phone=phone, sv_hash=_hash(code, salt), sv_salt=salt, sv_exp=time.time() + CODE_TTL, sv_tries=0)
    await msg.answer("✅ کدِ ۵رقمی به شمارهٔ تو پیامک شد. آن را همین‌جا بفرست (اعتبار: ۵ دقیقه):", reply_markup=_cancel_kb())


@sms_verify_router.message(SmsVerifyFSM.code, F.text)
async def got_code(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, _phone_verified_ok
    row = await get_bot_row(bot)
    if not row:
        return
    d = await state.get_data()
    if time.time() > float(d.get("sv_exp") or 0):
        await state.clear()
        return await msg.answer("⌛️ کد منقضی شد. دوباره از دکمهٔ «دریافتِ کد» شروع کن.")
    tries = int(d.get("sv_tries") or 0) + 1
    entered = (msg.text or "").translate(_AR).strip()
    if _hash(entered, d.get("sv_salt", "")) != d.get("sv_hash"):
        if tries >= MAX_TRIES:
            await state.clear()
            return await msg.answer("⛔️ تعدادِ تلاش‌هایِ اشتباه زیاد بود. دوباره کد بگیر.")
        await state.update_data(sv_tries=tries)
        return await msg.answer(f"❌ کد درست نیست ({MAX_TRIES - tries} تلاشِ دیگر).")
    phone = d["sv_phone"]
    if await number_taken(row.id, phone, msg.from_user.id):
        await state.clear()
        return await msg.answer("❌ این شماره قبلاً برایِ حسابِ دیگری تأیید شده است.")
    await state.clear()
    await _phone_verified_ok(msg, bot, state, row, phone)


# ───────────────────────── تنظیماتِ مالک ─────────────────────────

def _mask(k: str) -> str:
    return "—" if not k else (k[:3] + "…" + k[-3:] if len(k) > 8 else "•••")


async def _menu_view(bot_id: int):
    c = await cfg(bot_id)
    on = c["method"] == "sms"
    text = ("📨 <b>پنل پیامکی (تأییدِ شماره با کد)</b>\n\n"
            f"روشِ تأیید: <b>{'📨 پیامک' if on else '📱 اشتراکِ مخاطبِ تلگرام'}</b>\n"
            f"سرویس: <b>{PROVIDERS.get(c['provider'], c['provider'])}</b>\n"
            f"🔑 کلیدِ API: <code>{_mask(c['key'])}</code>\n"
            f"🧾 قالبِ تأیید: <code>{c['template'] or '—'}</code>"
            + (f"\n🏷 نامِ پارامترِ کد: <code>{c['param']}</code>" if c["provider"] == "smsir" else "")
            + f"\nوضعیت: {'✅ آماده' if ready(c) else '⚠️ ناقص (کلید و قالب لازم است)'}\n\n"
            "با «روشِ تأیید = پیامک»، همان گیت‌هایِ فعالِ تأییدِ شماره (ورود / پیش از پرداخت) به‌جایِ مخاطبِ تلگرام "
            "کدِ پیامکی می‌گیرند. خودِ گیت‌ها را از همان تنظیماتِ قبلی روشن/خاموش کن.")
    rows = [
        [InlineKeyboardButton(text=("🔁 روشِ تأیید: پیامک ✅" if on else "🔁 روشِ تأیید: مخاطبِ تلگرام"), callback_data="psms:method",
                              style=ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🏢 سرویس: {PROVIDERS.get(c['provider'], c['provider'])}", callback_data="psms:prov")],
        [InlineKeyboardButton(text="🔑 کلیدِ API", callback_data="psms:key"),
         InlineKeyboardButton(text="🧾 قالبِ تأیید", callback_data="psms:tpl")],
    ]
    if c["provider"] == "smsir":
        rows.append([InlineKeyboardButton(text="🏷 نامِ پارامترِ کد", callback_data="psms:param")])
    rows.append([InlineKeyboardButton(text="🧪 ارسالِ پیامکِ تست", callback_data="psms:test", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb_or_msg, row):
    text, kb = await _menu_view(row.id)
    if isinstance(cb_or_msg, CallbackQuery):
        try:
            return await cb_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:      # noqa: BLE001
            return await cb_or_msg.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb_or_msg.answer(text, reply_markup=kb, parse_mode="HTML")


@sms_verify_router.callback_query(F.data == "psms:menu")
async def menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row)
    await cb.answer()


@sms_verify_router.callback_query(F.data.in_({"psms:method", "psms:prov"}))
async def cycle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if cb.data == "psms:method":
        c = await cfg(row.id)
        new = "contact" if c["method"] == "sms" else "sms"
        if new == "sms" and not ready(c):
            return await cb.answer("اول کلید و قالبِ پنل پیامکی را وارد کن.", show_alert=True)
        await set_setting(row.id, "phone_verify_method", new)
    else:
        keys = list(PROVIDERS)
        cur = await get_setting(row.id, "sms_provider", "kavenegar")
        await set_setting(row.id, "sms_provider", keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0])
    await _show(cb, row)
    await cb.answer("انجام شد.")


_ASK = {"key": (SmsCfgFSM.key, "🔑 کلیدِ API پنلِ پیامکی را بفرست (بعد از دریافت، پیامت پاک می‌شود):"),
        "tpl": (SmsCfgFSM.template, "🧾 نامِ قالبِ تأیید را بفرست (کاوه‌نگار: نامِ قالب؛ sms.ir: شناسهٔ عددیِ قالب). "
                                    "قالب باید یک متغیرِ کد داشته باشد:"),
        "param": (SmsCfgFSM.param, "🏷 نامِ پارامترِ کد در قالبِ sms.ir را بفرست (مثلاً <code>CODE</code>):"),
        "test": (SmsCfgFSM.test, "🧪 شمارهٔ موبایلی که پیامکِ تست به آن برود را بفرست:")}


@sms_verify_router.callback_query(F.data.regexp(r"^psms:(key|tpl|param|test)$"))
async def ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    st, text = _ASK[cb.data.split(":")[1]]
    await state.set_state(st)
    await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ لغو", callback_data="psms:menu", style=ButtonStyle.DANGER)]]), parse_mode="HTML")
    await cb.answer()


async def _save_text(msg: Message, bot: Bot, state: FSMContext, setting: str, delete: bool = False):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        return
    val = (msg.text or "").strip()
    if delete:
        try:
            await msg.delete()
        except Exception:      # noqa: BLE001
            pass
    if not val or len(val) > 200:
        return await msg.answer("مقدار نامعتبر است.")
    await set_setting(row.id, setting, val)
    await state.clear()
    await _show(msg, row)


@sms_verify_router.message(SmsCfgFSM.key, F.text)
async def save_key(msg: Message, bot: Bot, state: FSMContext):
    await _save_text(msg, bot, state, "sms_api_key", delete=True)


@sms_verify_router.message(SmsCfgFSM.template, F.text)
async def save_tpl(msg: Message, bot: Bot, state: FSMContext):
    await _save_text(msg, bot, state, "sms_template")


@sms_verify_router.message(SmsCfgFSM.param, F.text)
async def save_param(msg: Message, bot: Bot, state: FSMContext):
    await _save_text(msg, bot, state, "sms_param")


@sms_verify_router.message(SmsCfgFSM.test, F.text)
async def send_test(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg
    row = await _owner_ok_msg(msg, bot)
    if not row:
        return
    phone = norm_mobile(msg.text)
    if not phone:
        return await msg.answer("شمارهٔ معتبر نیست (مثل 09123456789).")
    c = await cfg(row.id)
    if not ready(c):
        await state.clear()
        return await msg.answer("اول کلید و قالب را وارد کن.")
    ok, err = await send_code(c, local_mobile(phone), make_code())
    await state.clear()
    await msg.answer("✅ پیامکِ تست ارسال شد." if ok else f"❌ ارسال نشد:\n<code>{err}</code>", parse_mode="HTML")
    await _show(msg, row)
