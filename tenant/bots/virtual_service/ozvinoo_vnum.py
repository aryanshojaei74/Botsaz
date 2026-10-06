"""OzVinoo Telegram virtual-number provider integration for tenant SMM bots.

The provider is disabled until an owner adds a Bearer token and explicitly
turns it on. Countries are mirrored into the existing SmmService table so the
existing product/category and wallet/order paths remain compatible.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from config import Session
from models import Bot as BotRow, BotUser, SmmCategory, SmmOrder, SmmService, WalletTxn, Setting

logger = logging.getLogger(__name__)
ozvinoo_router = Router()

_OZ_BASE = "https://api.ozvinoo.xyz"
_OZ_NUMBERS = "/telegram-numbers/numbers/"
_OZ_SERVICES = "/telegram-numbers/number-services/"
_OZ_CANCEL = "/numbers/cancelOrder/"
_OZ_PROVIDER_SETTING = "ozvinoo_providers"
_OZ_CATEGORY = "شمارهٔ مجازیِ تلگرام"
_OZ_FIELDS_LOGGED: set = set()  # PATCH_OZVINOO_COST_FIELD_V1


class OzVinooFSM(StatesGroup):
    token = State()
    margin = State()
    item_price = State()
    cost_adjust = State()   # PATCH_OZVINOO_COST_ADJUST_V1
    price_sync = State()    # PATCH_OZVINOO_AUTO_PRICE_SYNC_V1


def _digits(value) -> str:
    return str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def _norm_int(value, default=0) -> int:
    try:
        return int(float(_digits(value).replace(",", "").replace("٬", "").strip()))
    except (TypeError, ValueError):
        return default


def _clean_provider_list(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    result = []
    for p in data:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        result.append({
            "id": str(p.get("id")),
            "name": str(p.get("name") or "OzVinoo"),
            "token": str(p.get("token") or "").strip(),
            "active": bool(p.get("active", False)),
            "margin_pct": float(p.get("margin_pct", 30) or 0),
            "none_report": bool(p.get("none_report", True)),
            "tier_both": bool(p.get("tier_both", False)),
            "last_sync": p.get("last_sync"),
            "last_count": int(p.get("last_count", 0) or 0),
            "last_error": str(p.get("last_error") or ""),
        })
    return result


async def _setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _save_providers(bot_id: int, providers: list[dict]) -> None:
    from tenant import set_setting
    await set_setting(bot_id, _OZ_PROVIDER_SETTING, json.dumps(providers, ensure_ascii=False))


async def _json_setting(bot_id: int, key: str, default):
    try:
        return json.loads(await _setting(bot_id, key, json.dumps(default, ensure_ascii=False)))
    except Exception:
        return default


async def _save_json_setting(bot_id: int, key: str, value) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, json.dumps(value, ensure_ascii=False))


async def _oz_price_overrides(bot_id: int) -> dict[str, int]:
    raw = await _json_setting(bot_id, "ozvinoo_price_overrides", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if str(v).isdigit() and int(v) > 0}


async def _oz_disabled_services(bot_id: int) -> set[str]:
    raw = await _json_setting(bot_id, "ozvinoo_disabled_services", [])
    return {str(x) for x in raw} if isinstance(raw, list) else set()


async def _providers(bot_id: int) -> list[dict]:
    return _clean_provider_list(await _setting(bot_id, _OZ_PROVIDER_SETTING, "[]"))


async def _owner(obj, bot: Bot):
    from tenant.bots.virtual_service.smm import _owner
    return await _owner(obj, bot)


async def _require_access(cb: CallbackQuery, row) -> bool:
    from tenant.bots.virtual_service.smm import _require_smm_api_access
    return await _require_smm_api_access(cb, row)


def _provider(providers: list[dict], provider_id: str) -> dict | None:
    return next((p for p in providers if p.get("id") == str(provider_id)), None)


def _mask(token: str) -> str:
    if not token:
        return "ثبت نشده"
    if len(token) <= 10:
        return "••••••••"
    return f"{token[:4]}…{token[-4:]}"


def _provider_detail(provider: dict) -> tuple[str, InlineKeyboardMarkup]:
    status = "✅ روشن" if provider.get("active") else "❌ خاموش"
    token_status = "✅ ثبت شده" if provider.get("token") else "❌ ثبت نشده"
    last = provider.get("last_sync") or "هنوز همگام‌سازی نشده"
    error = provider.get("last_error")
    text = (
        f"📱 <b>سرویس شمارهٔ مجازی خودکار — {html.escape(provider['name'])}</b>\n\n"
        f"وضعیت: {status}\n"
        f"کلید API: {token_status} (<code>{html.escape(_mask(provider.get('token', '')))}</code>)\n"
        f"درصد سود: {provider.get('margin_pct', 30):g}٪\n"
        f"آخرین همگام‌سازی: {html.escape(str(last))}\n"
        f"کشورها: {provider.get('last_count', 0)}"
    )
    # === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
    # وضعیت اینجا از Setting خوانده نمی‌شود (تابع sync نیست)؛ متنش را
    # هندلرِ صفحه اضافه می‌کند. اینجا فقط دکمه.
    # === PATCH_OZVINOO_NONE_REPORT_TOGGLE_V1 ===
    # مستنداتِ Callinoo: پارامترِ `none_report` تعیین می‌کند کدام «ردهٔ
    # قیمت» برگردانده شود. true یعنی شماره‌هایِ «سالم بدونِ ریپورت» که
    # گران‌ترند. کد از اول این را true هاردکد کرده بود و هیچ راهی برایِ
    # تغییرش نبود — علتِ اصلیِ شکایتِ «قیمتِ خرید ۱۰ هزار بالاتر از تعرفهٔ
    # پنلِ من است».
    _nr = bool(provider.get("none_report", True))
    text += ("\n📶 ردهٔ شماره: "
             + ("<b>هر دو رده</b> (کاربر انتخاب می‌کند: بدونِ ریپورت یا ارزان)" if provider.get("tier_both")
                else "<b>بدونِ ریپورت</b> (کیفیتِ بالاتر، گران‌تر)" if _nr
                else "<b>معمولی</b> (ارزان‌تر)"))
    # === PATCH_OZVINOO_COST_ADJUST_V1 ===
    _adj = float(provider.get("cost_adjust", 0) or 0)
    if _adj:
        text += f"\n🧾 اصلاحِ قیمتِ خرید: {_adj:+,.0f} تومان"
    if error:
        text += f"\n⚠️ آخرین خطا: {html.escape(error[:240])}"
    rows = [
        [InlineKeyboardButton(text="🔑 تغییر کلید API", callback_data=f"ozv:token:{provider['id']}")],
        [InlineKeyboardButton(text="🔄 همگام‌سازی کشورها و موجودی", callback_data=f"ozv:sync:{provider['id']}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"{'🟢 خاموش‌کردن' if provider.get('active') else '🟢 فعال‌کردن'} سرویس", callback_data=f"ozv:toggle:{provider['id']}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="💰 تنظیم درصد سود", callback_data=f"ozv:margin:{provider['id']}"),
         InlineKeyboardButton(text="📂 دسته‌بندی کشورها", callback_data=f"ozv:cats:{provider['id']}")],
        # === PATCH_OZVINOO_COST_ADJUST_V1 ===
        [InlineKeyboardButton(text="🧾 اصلاحِ قیمتِ خرید", callback_data=f"ozv:costadj:{provider['id']}")],
        # === PATCH_OZVINOO_NONE_REPORT_TOGGLE_V1 ===
        [InlineKeyboardButton(
            text=("📶 ردهٔ شماره: هر دو (انتخابِ کاربر)" if provider.get("tier_both")
                  else "📶 ردهٔ شماره: بدونِ ریپورت (گران‌تر)" if provider.get("none_report", True)
                  else "📶 ردهٔ شماره: معمولی (ارزان‌تر)"),
            callback_data=f"ozv:nonereport:{provider['id']}")],
        # === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
        [InlineKeyboardButton(text="⏱ استعلامِ خودکارِ قیمت",
                              callback_data=f"ozv:pricesync:{provider['id']}")],
        [InlineKeyboardButton(text="📋 مدیریت کشورهای فروش", callback_data=f"ozv:items:{provider['id']}:0", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🧪 تست اتصال", callback_data=f"ozv:test:{provider['id']}")],
        [InlineKeyboardButton(text="🗑 حذف ارائه‌دهنده", callback_data=f"ozv:delete:{provider['id']}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ozv:menu", style=ButtonStyle.DANGER)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    providers = await _providers(bot_id)
    lines = [
        "📱 <b>مدیریت شمارهٔ مجازی خودکار</b>\n",
        "اتصال به API OzVinoo برای دریافت شمارهٔ تلگرام، دریافت کد و لغو سفارش.",
        "پرداخت مشتری از کیف‌پول داخلی ربات و به تومان انجام می‌شود.",
    ]
    rows = []
    if not providers:
        lines.append("\nهنوز ارائه‌دهنده‌ای اضافه نشده است.")
    for p in providers:
        mark = "🟢" if p.get("active") else "🔴"
        lines.append(f"\n{mark} {html.escape(p['name'])} — {p.get('last_count', 0)} کشور")
        rows.append([InlineKeyboardButton(text=f"{mark} {p['name']}", callback_data=f"ozv:provider:{p['id']}")])
    rows.append([InlineKeyboardButton(text="➕ افزودن سرویس شمارهٔ مجازی خودکار", callback_data="ozv:add", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="📘 راهنمای اتصال OzVinoo", callback_data="ozv:help")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@ozvinoo_router.callback_query(F.data == "ozv:menu")
async def ozv_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    text, kb = await _menu_content(row.id)
    await _render(cb, text, kb)
    await cb.answer()


@ozvinoo_router.callback_query(F.data == "ozv:add")
async def ozv_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    await state.set_state(OzVinooFSM.token)
    await cb.message.answer(
        "🔑 <b>کلید API حساب OzVinoo را بفرستید.</b>\n\n"
        "کلید با فرمت Bearer در سمت سرور ذخیره می‌شود و در پیام‌های عمومی نمایش داده نمی‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="ozv:cancel_input", style=ButtonStyle.DANGER)],
        ]), parse_mode="HTML")
    await cb.answer()


@ozvinoo_router.callback_query(F.data.startswith("ozv:token:"))
async def ozv_change_token(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    provider_id = cb.data.split(":", 2)[2]
    if not _provider(await _providers(row.id), provider_id):
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    await state.update_data(provider_id=provider_id)
    await state.set_state(OzVinooFSM.token)
    await cb.message.answer("🔑 کلید API جدید را بفرستید. برای لغو، /لغو را ارسال کنید.")
    await cb.answer()


@ozvinoo_router.message(OzVinooFSM.token, F.text)
async def ozv_token_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    token = (msg.text or "").strip()
    if _is_escape(token):   # PATCH_OZVINOO_FSM_ESCAPE_V1
        await state.clear()
        text, kb = await _menu_content(row.id)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    if len(token) < 8 or any(ch.isspace() for ch in token):
        await msg.answer("❌ کلید نامعتبر است. کلید API را کامل و بدون فاصله بفرستید.")
        return
    data = await state.get_data()
    provider_id = data.get("provider_id")
    providers = await _providers(row.id)
    if provider_id:
        p = _provider(providers, provider_id)
    else:
        idx = len(providers) + 1
        provider_id = f"ozv{idx}"
        p = None
    if p is None:
        p = {"id": provider_id, "name": "OzVinoo — شمارهٔ مجازی تلگرام", "active": False,
             "margin_pct": 30, "none_report": True, "last_sync": None, "last_count": 0, "last_error": ""}
        providers.append(p)
    p["token"] = token
    p["last_error"] = ""
    await _save_providers(row.id, providers)
    await state.clear()
    await msg.answer("✅ کلید API ذخیره شد. اکنون از دکمهٔ «همگام‌سازی کشورها» برای دریافت فهرست کشورها استفاده کنید.")
    text, kb = _provider_detail(p)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@ozvinoo_router.callback_query(F.data == "ozv:cancel_input")
async def ozv_cancel_input(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    await state.clear()
    if row:
        text, kb = await _menu_content(row.id)
        await _render(cb, text, kb)
    await cb.answer("لغو شد")


@ozvinoo_router.callback_query(F.data.startswith("ozv:provider:"))
async def ozv_provider_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    provider_id = cb.data.split(":", 2)[2]
    p = _provider(await _providers(row.id), provider_id)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    text, kb = _provider_detail(p)
    await _render(cb, text, kb)
    await cb.answer()


async def _oz_request(provider: dict, method: str, path: str, *, params=None, body=None):
    token = str(provider.get("token") or "").strip()
    if not token:
        return 0, {"status": False, "message": "API token is not configured"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, _OZ_BASE + path, headers=headers, params=params, json=body) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = {"status": False, "message": (await response.text())[:300]}
                return response.status, payload
    except Exception as exc:
        return 0, {"status": False, "message": str(exc)}


def _api_message(status_code: int, payload) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("detail") or f"خطای HTTP {status_code}")
    return f"خطای HTTP {status_code}"


def _api_data(payload):
    if isinstance(payload, dict):
        return payload.get("data") if "data" in payload else payload
    return payload


def _extract_country_id(item: dict):
    for key in ("country_id", "id", "service_id", "country", "range"):
        value = item.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _country_available(item: dict) -> bool:
    count = str(item.get("count") or item.get("available") or "").lower()
    if any(x in count for x in ("❌", "ناموجود", "unavailable", "false", "0")):
        return False
    return True


# === PATCH_OZVINOO_NAME_NO_DOUBLE_FLAG_V1 ===
# پرچمِ کشورها در یونیکد از دو «Regional Indicator» (U+1F1E6..U+1F1FF)
# ساخته می‌شود. این تابع همهٔ آن‌ها را از نام پاک می‌کند تا پرچم دقیقاً
# یک‌بار — و همیشه در ابتدای نام — بنشیند.
def _strip_flags(text: str) -> str:
    out = "".join(ch for ch in (text or "")
                  if not (0x1F1E6 <= ord(ch) <= 0x1F1FF))
    return " ".join(out.split())


def _clean_country_name(country, emoji) -> str:
    """«🇺🇸 آمریکا 🇺🇸» → «🇺🇸 آمریکا»"""
    bare = _strip_flags(str(country or "")).strip() or "شمارهٔ تلگرام"
    flag = str(emoji or "").strip()
    if not flag:
        # اگر emoji جدا نیامده بود، شاید پرچم فقط داخلِ نام بوده
        flag = "".join(ch for ch in str(country or "")
                       if 0x1F1E6 <= ord(ch) <= 0x1F1FF)[:2]
    return f"{flag or '📱'} {bare}"


# === PATCH_OZVINOO_FSM_ESCAPE_V1 ===
# باگِ گزارش‌شده: ادمین واردِ «🧾 اصلاحِ قیمتِ خرید» شد، بعد دکمهٔ «بازگشت»
# کیبورد را زد و گیر کرد — چون هندلرِ انتظارِ عدد، متنِ دکمه را هم یک
# «عددِ نامعتبر» می‌دید و بی‌نهایت همان پیامِ خطا را تکرار می‌کرد:
#     ❌ عددِ معتبر بفرست؛ مثلاً -10000 یا 0.
# تنها راهِ خروج، ری‌استارتِ کاملِ ربات با /start بود.
#
# این همان تلهٔ ناوبری است که در home_sticker_admin هم بود. هندلرِ
# ozv_token_input فقط «لغو» را می‌شناخت و «بازگشت» را نه؛ margin و
# item_price هیچ راهِ خروجی نداشتند. حالا هر چهار ورودی یک محافظِ مشترک
# دارند.
_OZ_ESCAPE_WORDS = {
    "بازگشت", "⬅️ بازگشت", "لغو", "❌ لغو", "/لغو", "cancel", "انصراف",
    "بستن", "❌ بستن", "منوی اصلی", "🏠 منوی اصلی", "خانه", "/cancel", "/start",
}


def _is_escape(text: str) -> bool:
    """آیا کاربر می‌خواهد از این مرحله بیرون بیاید؟"""
    t = (text or "").strip()
    if not t:
        return False
    if t in _OZ_ESCAPE_WORDS:
        return True
    # دکمه‌هایِ کیبورد معمولاً ایموجی + متن‌اند؛ متنِ خالص را هم چک کن
    bare = "".join(ch for ch in t if ch.isalpha() or ch.isspace()).strip()
    return bare in _OZ_ESCAPE_WORDS


async def _escape_to_menu(msg, state, bot_id: int) -> None:
    """حالت را پاک می‌کند و به منویِ اصلیِ شمارهٔ مجازی برمی‌گردد."""
    await state.clear()
    text, kb = await _menu_content(bot_id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _sync_provider(bot_id: int, provider: dict) -> tuple[bool, str, int]:
    # === PATCH_OZVINOO_SYNC_RACE_FIX_V1 ===
    # گزارش: یک ادمین دکمهٔ «همگام‌سازیِ دستی» را زد درست همان لحظه‌ای که
    # حلقهٔ خودکارِ پس‌زمینه (ozvinoo_price_sync_loop) هم داشت همین ربات را
    # سینک می‌کرد. هر دو اجرا هم‌زمان روی همان ۱۵۳ کشور، برایِ هر کدام جدا
    # «SELECT... اگر نبود INSERT» را رد کردند — و چون هیچ‌کدام هنوز commit
    # نکرده بودند وقتی دیگری SELECT می‌زد، هردو فکر کردند سرویس وجود ندارد
    # و هردو اضافه‌اش کردند. نتیجه: هر کشور دقیقاً دوبار (۱۵۳ → ۳۰۶).
    # قفلِ per-bot زیر از دو سینکِ هم‌زمانِ یک ربات جلوگیری می‌کند. برایِ
    # دیتایِ خراب‌شدهٔ از قبل، fix_vnum_duplicate_countries.py را جداگانه
    # اجرا کن (پاک‌سازی + افزودنِ UNIQUE INDEX به‌عنوانِ backstop).
    async with _sync_lock(bot_id):
        return await _sync_provider_locked(bot_id, provider)


_SYNC_LOCKS: dict[int, asyncio.Lock] = {}


def _sync_lock(bot_id: int):
    lock = _SYNC_LOCKS.get(bot_id)
    if lock is None:
        lock = asyncio.Lock()
        _SYNC_LOCKS[bot_id] = lock
    return lock


async def _sync_provider_locked(bot_id: int, provider: dict) -> tuple[bool, str, int]:
    # === PATCH_OZVINOO_TIER_BOTH_V1 ===
    # حالتِ «هر دو رده»: هر دو فهرست (بدونِ ریپورت + ارزان) گرفته می‌شود.
    # ردیف‌هایِ بدونِ ریپورت همان service_id قدیمی را دارند؛ ردیف‌هایِ ارزان
    # پسوندِ «:c» می‌گیرند تا هیچ سفارش/تنظیمِ قبلی به‌هم نخورد.
    if provider.get("tier_both"):
        _tiers = [(True, ""), (False, ":c")]
    else:
        _tiers = [(bool(provider.get("none_report", True)), "")]
    _tier_data = []
    for _nr, _suf in _tiers:
        status, payload = await _oz_request(provider, "GET", _OZ_NUMBERS, body={"none_report": _nr})
        if status < 200 or status >= 300 or (isinstance(payload, dict) and payload.get("status") is False):
            return False, _api_message(status, payload), 0
        data = _api_data(payload)
        if not isinstance(data, list):
            return False, "پاسخ فهرست کشورها از API قابل‌خواندن نبود.", 0
        _tier_data.append((_suf, data))
    seen = set()
    price_overrides = await _oz_price_overrides(bot_id)
    disabled_services = await _oz_disabled_services(bot_id)
    async with Session() as s:
      for _suf, data in _tier_data:
        for item in data:
            if not isinstance(item, dict):
                continue
            country_id = _extract_country_id(item)
            if country_id is None:
                continue
            # === PATCH_OZVINOO_COST_FIELD_V1 (بازبینی‌شده) ===
            # مستنداتِ رسمیِ Callinoo بررسی شد: هر آیتمِ فهرستِ کشورها فقط
            # این فیلدها را دارد —
            #     country, price, range, count, emoji
            # یعنی *هیچ* فیلدِ جداگانه‌ای برایِ قیمتِ تمام‌شده وجود ندارد و
            # `price` تنها عددِ موجود است. حلقهٔ زیر عمداً نگه داشته شده
            # (بی‌هزینه است و اگر روزی API فیلدِ هزینه اضافه کرد خودکار
            # استفاده می‌شود) ولی امروز همیشه خالی برمی‌گردد.
            #
            # اختلافِ قیمت با تعرفهٔ پنل از جایِ دیگری می‌آید: پارامترِ
            # `none_report` — به PATCH_OZVINOO_NONE_REPORT_TOGGLE_V1 نگاه کن.
            cost = 0.0
            for _f in ("cost", "buy_price", "price_buy", "base_price",
                       "purchase_price", "wholesale_price"):
                cost = float(item.get(_f) or 0)
                if cost > 0:
                    break
            price = cost or float(item.get("price") or 0)
            # === PATCH_OZVINOO_COST_ADJUST_V1 ===
            # گزارشِ ادمین: «قیمتِ خریدی که اینجا زده با قیمتِ خریدِ توی پنل
            # یکی نیست، حدودِ ۱۰ هزار گران‌تره؛ درست سود نمی‌کشه».
            # عددی که API می‌دهد لزوماً هزینهٔ تمام‌شدهٔ ما نیست (کارمزد،
            # واحدِ متفاوت، یا قیمتِ فروشِ خودِ ارائه‌دهنده). به‌جایِ حدس‌زدن،
            # مالک می‌تواند یک اصلاحِ ثابت تعریف کند تا مبنایِ سود دقیقاً
            # همان چیزی شود که واقعاً می‌پردازد.
            price = max(0.0, price + float(provider.get("cost_adjust", 0) or 0))
            if price <= 0:
                continue
            if not _OZ_FIELDS_LOGGED:
                # یک‌بار در هر ری‌استارت: کلیدهایِ واقعیِ پاسخِ API را لاگ کن
                # تا اگر نامِ فیلدِ هزینه چیزِ دیگری بود، بدونِ حدس‌زدن
                # معلوم شود و به فهرستِ بالا اضافه‌اش کنیم.
                logger.warning("OzVinoo فیلدهایِ یک آیتمِ نمونه: %s", sorted(item.keys()))
                _OZ_FIELDS_LOGGED.add(True)
            service_id = f"ozv:{provider['id']}:{country_id}{_suf}"
            seen.add(service_id)
            available = _country_available(item)
            # === PATCH_OZVINOO_NAME_NO_DOUBLE_FLAG_V1 ===
            # API پرچم را هم جدا (`emoji`) و هم داخلِ `country` می‌فرستد —
            # گاهی حتی دو بار. چسباندنِ بی‌قید باعثِ «🇺🇸 آمریکا 🇺🇸» می‌شد
            # (گزارشِ ادمین: «چرا دو بار و با فاصله نشون می‌ده»). پس اول هر
            # پرچمی را از نام پاک می‌کنیم، بعد دقیقاً یک‌بار جلو می‌گذاریم.
            name = _clean_country_name(item.get("country"), item.get("emoji"))
            if _suf:
                name = f"{name} (ارزان)"
            # قیمت OzVinoo برای هر شماره است؛ SmmService آن را برای هر ۱۰۰۰ ذخیره می‌کند.
            sell = price_overrides.get(service_id) or (int(round(price * (1 + float(provider.get("margin_pct", 30) or 0) / 100))) * 1000)
            svc = await s.scalar(select(SmmService).where(
                SmmService.bot_id == bot_id, SmmService.service_id == service_id))
            if not svc:
                svc = SmmService(bot_id=bot_id, service_id=service_id, name=name,
                                 category=_OZ_CATEGORY, panel_rate=price, sell_rate=sell,
                                 min_qty=1, max_qty=1, is_active=bool(provider.get("active") and available) and service_id not in disabled_services,
                                 is_manual=False, platform_key="telegram", type_key="virtual_number")
                s.add(svc)
            else:
                svc.name = name
                svc.category = _OZ_CATEGORY
                svc.panel_rate = price
                svc.sell_rate = sell
                svc.is_active = bool(provider.get("active") and available) and service_id not in disabled_services
                svc.provider_desc = f"موجودی: {item.get('count') or 'نامشخص'} | پیش‌شماره: {item.get('range') or '—'}"
        existing = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.service_id.like(f"ozv:{provider['id']}:%")))).all()
        for svc in existing:
            if svc.service_id not in seen:
                svc.is_active = False
        await s.commit()
    return True, f"{len(seen)} کشور همگام شد.", len(seen)


@ozvinoo_router.callback_query(F.data.startswith("ozv:sync:"))
async def ozv_sync(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    providers = await _providers(row.id)
    p = _provider(providers, pid)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    await cb.answer("⏳ در حال دریافت کشورها…")
    ok, message, count = await _sync_provider(row.id, p)
    p["last_sync"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    p["last_count"] = count
    p["last_error"] = "" if ok else message
    await _save_providers(row.id, providers)
    text, kb = _provider_detail(p)
    prefix = "✅ " if ok else "❌ "
    await _render(cb, prefix + message + "\n\n" + text, kb)


@ozvinoo_router.callback_query(F.data.startswith("ozv:test:"))
async def ozv_test(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    p = _provider(await _providers(row.id), pid)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    status, payload = await _oz_request(p, "GET", _OZ_NUMBERS, body={"none_report": True})
    if 200 <= status < 300 and not (isinstance(payload, dict) and payload.get("status") is False):
        await cb.answer("✅ اتصال و کلید معتبر است", show_alert=True)
    else:
        await cb.answer("❌ اتصال ناموفق: " + _api_message(status, payload), show_alert=True)


@ozvinoo_router.callback_query(F.data.startswith("ozv:toggle:"))
async def ozv_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    providers = await _providers(row.id)
    p = _provider(providers, pid)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    if not p.get("token"):
        await cb.answer("ابتدا کلید API را ثبت کنید.", show_alert=True)
        return
    p["active"] = not bool(p.get("active"))
    await _save_providers(row.id, providers)
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.service_id.like(f"ozv:{pid}:%")))).all()
        for svc in svcs:
            svc.is_active = bool(p["active"])
        await s.commit()
    text, kb = _provider_detail(p)
    await _render(cb, text, kb)
    await cb.answer("✅ وضعیت تغییر کرد")


# === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
@ozvinoo_router.callback_query(F.data.startswith("ozv:pricesync:"))
async def ozv_price_sync_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    cur = await _price_sync_minutes(row.id)
    await state.update_data(provider_id=cb.data.split(":", 2)[2])
    await state.set_state(OzVinooFSM.price_sync)
    await cb.message.answer(
        "⏱ <b>استعلامِ خودکارِ قیمت</b>\n\n"
        "تعرفهٔ ارائه‌دهنده مدام تغییر می‌کند. اگر ربات قیمت را خودش "
        "به‌روز نکند، با نرخِ کهنه می‌فروشد — یعنی یا زیرِ قیمتِ خرید "
        "(ضرر) یا گران‌تر از بازار.\n\n"
        f"وضعیتِ فعلی: <b>{('هر ' + str(cur) + ' دقیقه') if cur else 'خاموش'}</b>\n\n"
        "عددِ دقیقه را بفرست (پیشنهاد: <code>10</code>)\n"
        "• کمترین مقدار ۲ دقیقه است\n"
        "• <code>0</code> یعنی خاموش (فقط همگام‌سازیِ دستی)",
        parse_mode="HTML")
    await cb.answer()


@ozvinoo_router.message(OzVinooFSM.price_sync, F.text)
async def ozv_price_sync_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if _is_escape(msg.text):
        await _escape_to_menu(msg, state, row.id)
        return
    val = _norm_int(msg.text, -1)
    if val < 0 or val > 1440:
        await msg.answer("❌ عددی بین ۰ تا ۱۴۴۰ بفرست (۰ یعنی خاموش).")
        return
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == row.id, Setting.key == "ozv_price_sync_min"))
        if r:
            r.value = str(val)
        else:
            s.add(Setting(bot_id=row.id, key="ozv_price_sync_min", value=str(val)))
        await s.commit()
    await state.clear()
    eff = await _price_sync_minutes(row.id)
    await msg.answer(
        ("✅ استعلامِ خودکارِ قیمت خاموش شد؛ قیمت‌ها فقط با همگام‌سازیِ "
         "دستی به‌روز می‌شوند." if not eff else
         f"✅ از این پس هر <b>{eff} دقیقه</b> قیمت و موجودی خودکار استعلام "
         "می‌شود.\n\n<i>قیمت‌هایِ دستیِ هر کشور و «اصلاحِ قیمتِ خرید» "
         "دست‌نخورده می‌مانند.</i>"),
        parse_mode="HTML")
    data = await state.get_data()
    p = _provider(await _providers(row.id), data.get("provider_id"))
    if p:
        text, kb = _provider_detail(p)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_OZVINOO_NONE_REPORT_TOGGLE_V1 ===
@ozvinoo_router.callback_query(F.data.startswith("ozv:nonereport:"))
async def ozv_none_report_toggle(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ ردهٔ قیمتِ شماره‌ها بینِ «بدونِ ریپورت» و «معمولی».

    طبقِ مستندات، همین یک پارامتر هم فهرستِ قیمت را عوض می‌کند و هم
    کیفیتِ شماره‌ای که موقعِ سفارش رزرو می‌شود — و کد در هر دو نقطه از
    همین مقدار استفاده می‌کند، پس قیمت و کیفیت همیشه هم‌خوان می‌مانند.
    """
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    providers = await _providers(row.id)
    p = _provider(providers, pid)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    # چرخه: بدونِ ریپورت → معمولی(ارزان) → هر دو (انتخابِ کاربر) → بدونِ ریپورت
    if p.get("tier_both"):
        p["tier_both"] = False
        p["none_report"] = True
    elif p.get("none_report", True):
        p["none_report"] = False
    else:
        p["tier_both"] = True
        p["none_report"] = True
    await _save_providers(row.id, providers)
    text, kb = _provider_detail(p)
    await _render(cb, text, kb)
    await cb.answer(
        ("📶 حالتِ «هر دو رده» فعال شد؛ کاربر قبل از لیست انتخاب می‌کند: بدونِ ریپورت یا ارزان. "
         if p.get("tier_both") else
         "📶 ردهٔ «بدونِ ریپورت» فعال شد (گران‌تر، کیفیت بالاتر). "
         if p["none_report"] else
         "📶 ردهٔ «معمولی» فعال شد (ارزان‌تر). ")
        + "حالا حتماً «🔄 همگام‌سازی» را بزن تا قیمت‌ها به‌روز شوند.",
        show_alert=True)


# === PATCH_OZVINOO_COST_ADJUST_V1 ===
@ozvinoo_router.callback_query(F.data.startswith("ozv:costadj:"))
async def ozv_costadj_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """اصلاحِ قیمتِ خرید: عددی که به قیمتِ پایهٔ API اضافه/کم می‌شود."""
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    p = _provider(await _providers(row.id), pid)
    if not p:
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    cur = float(p.get("cost_adjust", 0) or 0)
    margin = float(p.get("margin_pct", 30) or 0)
    await state.update_data(provider_id=pid)
    await state.set_state(OzVinooFSM.cost_adjust)
    await cb.message.answer(
        "🧾 <b>اصلاحِ قیمتِ خرید</b>\n\n"
        "اگر قیمتی که ربات به‌عنوانِ «قیمتِ خرید» حساب می‌کند با چیزی که "
        "واقعاً در پنلِ ارائه‌دهنده می‌پردازی فرق دارد، اختلاف را اینجا بده "
        "تا سود رویِ عددِ درست حساب شود.\n\n"
        f"مقدارِ فعلی: <b>{cur:+,.0f}</b> تومان\n"
        f"درصدِ سود: <b>{margin:g}٪</b>\n\n"
        "• عددِ منفی یعنی قیمتِ خرید کمتر است — مثلاً <code>-10000</code>\n"
        "• عددِ مثبت یعنی بیشتر — مثلاً <code>5000</code>\n"
        "• <code>0</code> یعنی بدونِ اصلاح\n\n"
        "<i>بعد از ثبت، حتماً «🔄 همگام‌سازی» را بزن تا رویِ قیمت‌های فعلی اعمال شود.</i>",
        parse_mode="HTML")
    await cb.answer()


@ozvinoo_router.message(OzVinooFSM.cost_adjust, F.text)
async def ozv_costadj_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if _is_escape(msg.text):   # PATCH_OZVINOO_FSM_ESCAPE_V1
        await _escape_to_menu(msg, state, row.id)
        return
    raw = _digits(msg.text).replace(",", "").replace("٬", "").strip()
    try:
        adj = float(raw)
    except ValueError:
        await msg.answer("❌ عددِ معتبر بفرست؛ مثلاً <code>-10000</code> یا <code>0</code>.",
                         parse_mode="HTML")
        return
    if abs(adj) > 10_000_000:
        await msg.answer("❌ عدد بیش‌ازحد بزرگ است.")
        return
    data = await state.get_data()
    providers = await _providers(row.id)
    p = _provider(providers, data.get("provider_id"))
    if not p:
        await state.clear()
        await msg.answer("ارائه‌دهنده پیدا نشد.")
        return
    p["cost_adjust"] = adj
    await _save_providers(row.id, providers)
    await state.clear()
    margin = float(p.get("margin_pct", 30) or 0)
    # یک مثالِ ملموس تا مالک ببیند دقیقاً چه اتفاقی می‌افتد
    sample = 88_500.0
    old_sell = int(round(sample * (1 + margin / 100)))
    new_base = max(0.0, sample + adj)
    new_sell = int(round(new_base * (1 + margin / 100)))
    await msg.answer(
        f"✅ اصلاحِ قیمتِ خرید رویِ <b>{adj:+,.0f}</b> تومان تنظیم شد.\n\n"
        f"<b>مثال</b> — اگر API قیمتِ {sample:,.0f} بدهد و سودت {margin:g}٪ باشد:\n"
        f"• قبل: مبنا {sample:,.0f} → فروش {old_sell:,}\n"
        f"• بعد: مبنا {new_base:,.0f} → فروش {new_sell:,}\n\n"
        "حالا «🔄 همگام‌سازی کشورها و موجودی» را بزن تا رویِ همهٔ کشورها اعمال شود.",
        parse_mode="HTML")
    text, kb = _provider_detail(p)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@ozvinoo_router.callback_query(F.data.startswith("ozv:margin:"))
async def ozv_margin_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    if not _provider(await _providers(row.id), pid):
        await cb.answer("ارائه‌دهنده پیدا نشد", show_alert=True)
        return
    await state.update_data(provider_id=pid)
    await state.set_state(OzVinooFSM.margin)
    await cb.message.answer("💰 درصد سود جدید را بفرستید؛ مثلاً ۳۰")
    await cb.answer()


@ozvinoo_router.message(OzVinooFSM.margin, F.text)
async def ozv_margin_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if _is_escape(msg.text):   # PATCH_OZVINOO_FSM_ESCAPE_V1
        await _escape_to_menu(msg, state, row.id)
        return
    margin = _norm_int(msg.text, -1)
    if margin < 0 or margin > 1000:
        await msg.answer("❌ درصد نامعتبر است. عددی بین ۰ تا ۱۰۰۰ بفرستید.")
        return
    data = await state.get_data()
    pid = data.get("provider_id")
    providers = await _providers(row.id)
    p = _provider(providers, pid)
    if not p:
        await state.clear()
        await msg.answer("ارائه‌دهنده پیدا نشد.")
        return
    p["margin_pct"] = margin
    await _save_providers(row.id, providers)
    await state.clear()
    await msg.answer(f"✅ درصد سود روی {margin}٪ تنظیم شد. برای اعمال روی قیمت‌های فعلی، همگام‌سازی را بزنید.")
    text, kb = _provider_detail(p)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _oz_country_items_content(bot_id: int, provider_id: str, page: int = 0):
    page = max(0, int(page))
    # === PATCH_OZVINOO_ITEMS_2ROW_V2 ===
    # هر کشور حالا دو ردیف می‌گیرد، پس تعدادِ هر صفحه نصف شد تا کیبورد
    # از سقفِ عملیِ تلگرام رد نشود و صفحه هم بی‌انتها اسکرول نخورد.
    per_page = 8
    async with Session() as s:
        services = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id,
            SmmService.service_id.like(f"ozv:{provider_id}:%")
        ).order_by(SmmService.name, SmmService.id))).all()
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == bot_id, SmmCategory.is_active == True
        ).order_by(SmmCategory.id))).all()  # noqa: E712
    category_names = {c.id: c.name for c in cats}
    if not services:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ابتدا همگام‌سازی کنید", callback_data=f"ozv:sync:{provider_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"ozv:provider:{provider_id}", style=ButtonStyle.DANGER)],
        ])
        return "📋 <b>مدیریت کشورهای فروش</b>\n\nهنوز کشوری همگام نشده است.", kb
    start, end = page * per_page, page * per_page + per_page
    # === PATCH_OZVINOO_ITEMS_2ROW_V2 ===
    # نسخهٔ چهارستونه (V1) ستون‌ها را تراز کرد ولی خودِ نامِ کشور را له
    # کرد: در یک‌چهارمِ عرضِ صفحه «سودان جنوبی» می‌شد «جنوبی س» و
    # «آفریقای مرکزی» می‌شد «مرکزی آف» — یعنی مهم‌ترین ستون ناخوانا شد.
    #
    # طبقِ درخواستِ ادمین، حالا هر کشور دو ردیف است:
    #     ┌───────────── نامِ کشور (تمامِ عرض) ─────────────┐
    #     │  دسته   │   قیمت   │   موجود                  │
    # نام تمامِ عرض را دارد پس هیچ‌وقت بریده نمی‌شود، و سه مقدارِ کوتاه
    # زیرش جا می‌شوند. هر پنج دکمه به همان صفحهٔ جزئیات می‌روند.
    # === PATCH_OZVINOO_BULK_TOGGLE_V1 ===
    # گزارشِ ادمین (فدایی): «یه گزینه غیرفعال‌سازیِ همه و فعال‌سازیِ همه برایِ فروش بذار؛ دونه‌دونه
    # مجبور نباشیم — اگه بخوایم کلاً فقط یه شماره از پنلِ خودکار رو بفروشیم».
    rows = [[
        InlineKeyboardButton(text="🟢 فعال‌کردنِ همه", callback_data=f"ozv:allask:on:{provider_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="🔴 غیرفعال‌کردنِ همه", callback_data=f"ozv:allask:off:{provider_id}", style=ButtonStyle.DANGER),
    ], [
        InlineKeyboardButton(text="📂 دسته", callback_data="noop"),
        InlineKeyboardButton(text="💰 قیمت", callback_data="noop"),
        InlineKeyboardButton(text="🔢 موجود", callback_data="noop"),
    ]]
    for svc in services[start:end]:
        active = bool(svc.is_active)
        mark = "🟢" if active else "🔴"
        price = int(float(svc.sell_rate or 0) / 1000)
        cat = category_names.get(svc.category_pk, "—")
        cbd = f"ozv:item:{svc.id}:{page}"
        style = ButtonStyle.SUCCESS if active else ButtonStyle.DANGER
        rows.append([
            InlineKeyboardButton(text=svc.name, callback_data=cbd, style=style)])
        rows.append([
            InlineKeyboardButton(text=cat[:16], callback_data=cbd, style=style),
            InlineKeyboardButton(text=f"{price:,}", callback_data=cbd, style=style),
            InlineKeyboardButton(text=mark, callback_data=cbd, style=style),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ozv:items:{provider_id}:{page-1}"))
    if end < len(services):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ozv:items:{provider_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 همگام‌سازی", callback_data=f"ozv:sync:{provider_id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"ozv:provider:{provider_id}", style=ButtonStyle.DANGER)])
    # === PATCH_OZVINOO_ITEMS_4COL_V1 ===
    _on = sum(1 for v in services if v.is_active)
    _pages = max(1, -(-len(services) // per_page))
    text = (f"📋 <b>مدیریت کشورهای فروش</b>\n\n"
            f"🌍 {len(services)} کشور · 🟢 {_on} فعال · 🔴 {len(services) - _on} غیرفعال\n"
            f"📄 صفحهٔ {page + 1} از {_pages}\n\n"
            "رویِ هر ردیف بزن تا قیمت، وضعیتِ فروش و دسته‌اش را تنظیم کنی.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _oz_country_detail_content(bot_id: int, svc_id: int, page: int = 0):
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != bot_id or not str(svc.service_id or "").startswith("ozv:"):
            return None
        cat = await s.get(SmmCategory, svc.category_pk) if svc.category_pk else None
    parts = str(svc.service_id).split(":", 2)
    provider_id = parts[1] if len(parts) >= 2 else ""
    price = int(float(svc.sell_rate or 0) / 1000)
    text = (f"📱 <b>مدیریت کشور فروش</b>\n\nنام: {html.escape(svc.name)}\n"
            # === PATCH_OZVINOO_COST_ADJUST_V1 ===
            # سودِ واقعی را صریح نشان بده تا مالک بتواند با پنلِ ارائه‌دهنده
            # مقایسه کند و ببیند اصلاحِ قیمتِ خرید لازم است یا نه.
            f"قیمت خرید (بعد از اصلاح): {float(svc.panel_rate or 0):,.0f} تومان\n"
            f"قیمت فروش: {price:,} تومان\n"
            f"سود: {price - float(svc.panel_rate or 0):,.0f} تومان"
            + (f" ({(price / float(svc.panel_rate)) * 100 - 100:.0f}٪)"
               if float(svc.panel_rate or 0) > 0 else "") + "\n"
            f"وضعیت فروش: {'✅ فعال' if svc.is_active else '❌ غیرفعال'}\n"
            f"دسته: {html.escape(cat.name if cat else 'بدون دسته')}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 غیرفعال‌کردن" if svc.is_active else "🟢 فعال‌کردن", callback_data=f"ozv:itemtoggle:{svc.id}:{page}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="💰 تغییر قیمت فروش", callback_data=f"ozv:itemprice:{svc.id}:{page}"),
         InlineKeyboardButton(text="📂 انتخاب دسته", callback_data=f"ozv:itemcat:{svc.id}:{page}")],
        [InlineKeyboardButton(text="⬅️ بازگشت به کشورها", callback_data=f"ozv:items:{provider_id}:{page}", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@ozvinoo_router.callback_query(F.data.startswith("ozv:items:"))
async def ozv_items(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    text, kb = await _oz_country_items_content(row.id, parts[2], _norm_int(parts[3], 0))
    await _render(cb, text, kb)
    await cb.answer()


@ozvinoo_router.callback_query(F.data.startswith("ozv:item:"))
async def ozv_item_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    content = await _oz_country_detail_content(row.id, _norm_int(parts[2], 0), _norm_int(parts[3], 0))
    if not content:
        await cb.answer("کشور پیدا نشد", show_alert=True)
        return
    await _render(cb, content[0], content[1])
    await cb.answer()


# === PATCH_OZVINOO_BULK_TOGGLE_V1 ===
async def _oz_provider_service_rows(session, bot_id: int, provider_id: str):
    return (await session.scalars(select(SmmService).where(
        SmmService.bot_id == bot_id,
        SmmService.service_id.like(f"ozv:{provider_id}:%")))).all()


@ozvinoo_router.callback_query(F.data.startswith("ozv:allask:"))
async def ozv_all_ask(cb: CallbackQuery, bot: Bot):
    """مرحلهٔ تأیید برایِ «فعال/غیرفعال‌کردنِ همهٔ کشورها» — یک لمسِ اشتباه نباید کلِ فروش را ببندد."""
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    mode, pid = parts[2], parts[3]
    if mode not in ("on", "off"):
        return await cb.answer("نامعتبر", show_alert=True)
    async with Session() as s:
        total = len(await _oz_provider_service_rows(s, row.id, pid))
    if not total:
        return await cb.answer("هنوز کشوری همگام نشده است.", show_alert=True)
    if mode == "off":
        text = (f"🔴 <b>غیرفعال‌کردنِ همهٔ {total} کشور</b>\n\n"
                "هیچ‌کدام برایِ خرید در دسترس نخواهد بود تا دوباره روشن‌شان کنی. "
                "بعد از آن می‌توانی فقط کشورِ دلخواه را از همین فهرست روشن کنی.\n"
                "این تنظیم با همگام‌سازی هم دوباره روشن نمی‌شود.")
    else:
        text = (f"🟢 <b>فعال‌کردنِ همهٔ {total} کشور</b>\n\n"
                "همهٔ کشورها از فهرستِ «غیرفعال‌شده‌ها» بیرون می‌آیند و ربات همین الان با "
                "ارائه‌دهنده همگام می‌شود؛ فقط کشورهایی که ارائه‌دهنده برایشان موجودی دارد "
                "واقعاً برایِ فروش روشن می‌شوند (کشورِ بدونِ موجودی فروخته نمی‌شود).")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، انجام بده", callback_data=f"ozv:allgo:{mode}:{pid}",
                              style=ButtonStyle.SUCCESS if mode == "on" else ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"ozv:items:{pid}:0", style=ButtonStyle.PRIMARY)],
    ])
    await _render(cb, text, kb)
    await cb.answer()


@ozvinoo_router.callback_query(F.data.startswith("ozv:allgo:"))
async def ozv_all_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    mode, pid = parts[2], parts[3]
    if mode not in ("on", "off"):
        return await cb.answer("نامعتبر", show_alert=True)
    note = ""
    # فهرستِ پایدارِ «غیرفعال‌شده‌ها» (همان چیزی که sync هم می‌خواند) همیشه هم‌زمان به‌روز می‌شود.
    async with Session() as s:
        rows_ = await _oz_provider_service_rows(s, row.id, pid)
        keys = {str(v.service_id) for v in rows_}
        disabled = await _oz_disabled_services(row.id)
        if mode == "off":
            for v in rows_:
                v.is_active = False
            disabled |= keys
            await s.commit()
            await _save_json_setting(row.id, "ozvinoo_disabled_services", sorted(disabled))
            done = len(rows_)
            note = f"🔴 {done} کشور غیرفعال شد."
        else:
            disabled -= keys
            await s.commit()
            await _save_json_setting(row.id, "ozvinoo_disabled_services", sorted(disabled))
    if mode == "on":
        providers = await _providers(row.id)
        p = _provider(providers, pid)
        ok, message, count = (False, "ارائه‌دهنده پیدا نشد", 0)
        if p:
            await cb.answer("⏳ در حالِ همگام‌سازیِ موجودی…")
            ok, message, count = await _sync_provider(row.id, p)
        if ok:
            async with Session() as s:
                on_now = sum(1 for v in await _oz_provider_service_rows(s, row.id, pid) if v.is_active)
            note = (f"🟢 فهرستِ غیرفعال‌شده‌ها خالی شد و {count} کشور همگام شد؛ "
                    f"{on_now} کشور (دارایِ موجودی) روشن است.")
        else:
            # همگام‌سازی شکست خورد (قطعیِ ارائه‌دهنده و مانندِ آن). فهرستِ خاموش‌ها همین حالا خالی است، پس با
            # اولین همگامِ موفق وضعیتِ درست (بر پایهٔ موجودی) اعمال می‌شود؛ تا آن موقع چیزی را کورکورانه
            # روشن نمی‌کنیم تا کشورِ بدونِ موجودی فروخته نشود.
            note = ("⚠️ فهرستِ غیرفعال‌شده‌ها خالی شد، ولی همگام‌سازی با ارائه‌دهنده شکست خورد "
                    f"({message}). با «🔄 همگام‌سازی» دوباره امتحان کن تا کشورهایِ دارایِ موجودی روشن شوند.")
    text, kb = await _oz_country_items_content(row.id, pid, 0)
    await _render(cb, note + "\n\n" + text, kb)
    if mode == "off":      # در حالتِ «on» پیش‌تر (پیش از همگام‌سازیِ طولانی) پاسخ داده شده است
        await cb.answer(note[:180])


@ozvinoo_router.callback_query(F.data.startswith("ozv:itemtoggle:"))
async def ozv_item_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    svc_id, page = _norm_int(parts[2], 0), _norm_int(parts[3], 0)
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != row.id or not str(svc.service_id or "").startswith("ozv:"):
            await cb.answer("کشور پیدا نشد", show_alert=True)
            return
        svc.is_active = not bool(svc.is_active)
        key = str(svc.service_id)
        disabled = await _oz_disabled_services(row.id)
        (disabled.discard(key) if svc.is_active else disabled.add(key))
        await s.commit()
    await _save_json_setting(row.id, "ozvinoo_disabled_services", sorted(disabled))
    content = await _oz_country_detail_content(row.id, svc_id, page)
    if content:
        await _render(cb, content[0], content[1])
    await cb.answer("✅ وضعیت فروش تغییر کرد")


@ozvinoo_router.callback_query(F.data.startswith("ozv:itemprice:"))
async def ozv_item_price_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    svc_id, page = _norm_int(parts[2], 0), _norm_int(parts[3], 0)
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != row.id:
            await cb.answer("کشور پیدا نشد", show_alert=True)
            return
        current = int(float(svc.sell_rate or 0) / 1000)
    await state.update_data(ozv_service_id=svc_id, ozv_items_page=page)
    await state.set_state(OzVinooFSM.item_price)
    await cb.message.answer(f"💰 قیمت فروش جدید را به تومان بفرستید. قیمت فعلی: {current:,}")
    await cb.answer()


@ozvinoo_router.message(OzVinooFSM.item_price, F.text)
async def ozv_item_price_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    if _is_escape(msg.text):   # PATCH_OZVINOO_FSM_ESCAPE_V1
        await _escape_to_menu(msg, state, row.id)
        return
    value = _norm_int(msg.text, 0)
    if value <= 0:
        await msg.answer("❌ مبلغ نامعتبر است؛ یک عدد مثبت به تومان بفرستید.")
        return
    data = await state.get_data()
    svc_id, page = _norm_int(data.get("ozv_service_id"), 0), _norm_int(data.get("ozv_items_page"), 0)
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != row.id or not str(svc.service_id or "").startswith("ozv:"):
            await state.clear()
            await msg.answer("کشور پیدا نشد.")
            return
        svc.sell_rate = value * 1000
        overrides = await _oz_price_overrides(row.id)
        overrides[str(svc.service_id)] = value * 1000
        await s.commit()
    await _save_json_setting(row.id, "ozvinoo_price_overrides", overrides)
    await state.clear()
    content = await _oz_country_detail_content(row.id, svc_id, page)
    await msg.answer("✅ قیمت فروش ذخیره شد.")
    if content:
        await msg.answer(content[0], reply_markup=content[1], parse_mode="HTML")


@ozvinoo_router.callback_query(F.data.startswith("ozv:itemcat:"))
async def ozv_item_category(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    svc_id, page = _norm_int(parts[2], 0), _norm_int(parts[3], 0)
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True
        ).order_by(SmmCategory.id))).all()  # noqa: E712
    rows = [[InlineKeyboardButton(text="🚫 بدون دسته", callback_data=f"ozv:itemcatset:{svc_id}:0:{page}")]]
    for cat in cats[:80]:
        rows.append([InlineKeyboardButton(text=f"📂 {cat.name[:45]}", callback_data=f"ozv:itemcatset:{svc_id}:{cat.id}:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"ozv:item:{svc_id}:{page}", style=ButtonStyle.DANGER)])
    await _render(cb, "📂 <b>انتخاب دستهٔ فروش</b>\n\nدستهٔ کشور را انتخاب کنید:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@ozvinoo_router.callback_query(F.data.startswith("ozv:itemcatset:"))
async def ozv_item_category_set(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    parts = cb.data.split(":")
    svc_id, cat_id, page = _norm_int(parts[2], 0), _norm_int(parts[3], 0), _norm_int(parts[4], 0)
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != row.id or not str(svc.service_id or "").startswith("ozv:"):
            await cb.answer("کشور پیدا نشد", show_alert=True)
            return
        svc.category_pk = None if cat_id == 0 else cat_id
        await s.commit()
    content = await _oz_country_detail_content(row.id, svc_id, page)
    if content:
        await _render(cb, content[0], content[1])
    await cb.answer("✅ دسته ذخیره شد")


@ozvinoo_router.callback_query(F.data.startswith("ozv:cats:"))
async def ozv_categories(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.service_id.like(f"ozv:{pid}:%")).order_by(SmmService.id))).all()
    rows = []
    for svc in svcs[:80]:
        rows.append([InlineKeyboardButton(text=f"📂 {svc.name[:45]}", callback_data=f"smm:svc:cat:{svc.id}:my")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"ozv:provider:{pid}", style=ButtonStyle.DANGER)])
    text = "📂 <b>دسته‌بندی کشورهای OzVinoo</b>\n\nبرای هر کشور دستهٔ فروش را انتخاب کنید:"
    await _render(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@ozvinoo_router.callback_query(F.data == "ozv:help")
async def ozv_help(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    text = (
        "📘 <b>راهنمای اتصال شمارهٔ مجازی خودکار</b>\n\n"
        "۱) از پنل OzVinoo کلید Bearer را دریافت و در بخش افزودن ارائه‌دهنده ثبت کنید.\n"
        "۲) همگام‌سازی کشورها را بزنید تا کشورها و قیمت پایه وارد شوند.\n"
        "۳) درصد سود را تنظیم کنید و دوباره همگام‌سازی کنید.\n"
        "۴) سرویس را روشن کنید؛ کشورها در فروشگاه شمارهٔ مجازی نمایش داده می‌شوند.\n"
        "۵) برای هر کشور می‌توانید دستهٔ موجود ربات را انتخاب کنید.\n\n"
        "پس از خرید، شماره نمایش داده می‌شود و کد با دکمهٔ «دریافت کد» یا polling پس‌زمینه بررسی می‌شود. در صورت لغو قابل‌قبول از طرف API، مبلغ داخلی به کیف‌پول کاربر برمی‌گردد."
    )
    await _render(cb, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ozv:menu", style=ButtonStyle.DANGER)]
    ]))
    await cb.answer()


def _tier_none_report(provider: dict, svc) -> bool:
    """ردهٔ این ردیف: «:c» = ارزان (none_report=False)؛ در حالتِ هر دو رده، بقیه بدونِ ریپورت."""
    sid = str(getattr(svc, "service_id", "") or "")
    if sid.endswith(":c"):
        return False
    if provider.get("tier_both"):
        return True
    return bool(provider.get("none_report", True))


async def _service_provider(bot_id: int, svc: SmmService) -> tuple[dict | None, int | None]:
    parts = str(svc.service_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ozv":
        return None, None
    providers = await _providers(bot_id)
    # پسوندِ «:c» = ردهٔ ارزان در حالتِ «هر دو رده» (PATCH_OZVINOO_TIER_BOTH_V1)
    return _provider(providers, parts[1]), _norm_int(parts[2].split(":")[0], 0)


async def _refund_order(bot: Bot, order_id: int, note: str, text: str | None = None) -> bool:
    async with Session() as s:
        order = await s.get(SmmOrder, order_id, with_for_update=True)
        if not order or order.status in ("refunded", "canceled", "completed"):
            return False
        buyer = await s.scalar(select(BotUser).where(
            BotUser.bot_id == order.bot_id, BotUser.telegram_id == order.user_telegram_id).with_for_update())
        if not buyer:
            return False
        amount = int(order.price_toman or 0)
        buyer.balance = float(buyer.balance) + amount
        s.add(WalletTxn(bot_id=order.bot_id, user_id=buyer.id, amount=amount,
                        kind="smm_refund", note=note))
        order.status = "refunded"
        order.panel_status = "canceled"
        await s.commit()
    try:
        await bot.send_message(order.user_telegram_id,
                               (text or "↩️ سفارش لغو شد و {amount} تومان به کیف‌پولت برگشت داده شد.").replace("{amount}", f"{amount:,}"))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


async def _process_code(bot: Bot, order: SmmOrder, provider: dict, api_order_id: int) -> str:
    status, payload = await _oz_request(provider, "GET", _OZ_SERVICES, body={"order_id": api_order_id})
    data = _api_data(payload)
    if status == 202 or (isinstance(payload, dict) and payload.get("status_code") == 202):
        return "pending"
    if status < 200 or status >= 300 or not isinstance(data, dict):
        if status in (404, 409):
            return "missing"
        return "error"
    code = str(data.get("code") or "").strip()
    if not code:
        return "pending"
    async with Session() as s:
        current = await s.get(SmmOrder, order.id, with_for_update=True)
        if not current or current.status in ("refunded", "canceled"):
            return "canceled"          # PATCH_VNUM_AUDIT_V1: لغوشده «کد ارسال شد» نیست
        if current.status == "completed":
            return "completed"
        current.status = "completed"
        current.panel_status = "completed"
        current.note = (current.note or "") + f" — کد: {code}"
        await s.commit()
    try:
        # === PATCH_VNUM_RECODE_V1 === پیامِ کد + دکمه‌هایِ کپی و «دریافت مجدد کد»
        await bot.send_message(order.user_telegram_id, _code_message(code, order.link),
                               reply_markup=_code_kb(order.id, code), parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return "completed"


# === PATCH_VNUM_RECODE_V1 ===
# درخواستِ مالک (تیکت #780): مثلِ @CallinooBot، خریدار بعد از گرفتنِ کد هر وقت
# لازم شد (کد را دیر زد، اشتباه شد، یا بعداً رویِ یک کلاینتِ دیگر مثلِ
# تلگراف/نایسگرام لاگین کرد) از همان سفارش دوباره کد بگیرد.
# روش: همان استعلامِ order_id دوباره از OzVinoo پرسیده می‌شود. اگر کدِ تازه‌ای
# آمده بود (غیر از آخرین کدی که فرستادیم) آن را می‌فرستیم؛ اگر همان کدِ قبلی
# بود، راهنمایی می‌کنیم که اول در اپ درخواستِ کد بدهد. هزینه‌ای کسر نمی‌شود.
_RECODE_COOLDOWN_SEC = 15
_RECODE_LAST: dict[int, float] = {}
_CODE_IN_NOTE = re.compile(r"کد: (\S+)")


def _code_message(code: str, phone: str | None) -> str:
    phone_line = f"☎️ شماره مجازی: <code>{html.escape(str(phone))}</code>\n" if phone else ""
    return (
        "🎉 <b>کد ورود با موفقیت دریافت شد</b>\n\n"
        f"🔢 کد ورود: <code>{html.escape(code)}</code>\n{phone_line}\n"
        "🔄 اگر کد را نتوانستید وارد کنید (اشتباه بود، دیر شد و…) یا بعداً خواستید "
        "همین شماره را در یک تلگرامِ دیگر (مثل تلگراف یا نایسگرام) وارد کنید: "
        "اول در همان اپ درخواستِ کد بدهید، بعد دکمهٔ «🔄 دریافت مجدد کد» را بزنید."
    )


def _code_kb(order_id: int, code: str) -> InlineKeyboardMarkup:
    from aiogram.types import CopyTextButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 کپی کد ورود", copy_text=CopyTextButton(text=str(code)[:256]))],
        [InlineKeyboardButton(text="🔄 دریافت مجدد کد", callback_data=f"vnum:ozre:{order_id}",
                              style=ButtonStyle.PRIMARY)],
    ])


@ozvinoo_router.callback_query(F.data.startswith("vnum:ozre:"))
async def ozv_recode(cb: CallbackQuery, bot: Bot):
    import time as _time
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    local_id = _norm_int(cb.data.split(":")[-1], 0)
    async with Session() as s:
        order = await s.get(SmmOrder, local_id)
    if (not order or order.bot_id != row.id or order.user_telegram_id != cb.from_user.id
            or not str(order.service_id).startswith("ozv:")):
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    if order.status != "completed":
        await cb.answer("هنوز کدِ اولِ این شماره نرسیده؛ از دکمهٔ «📩 دریافت کد پیامک» استفاده کنید.",
                        show_alert=True)
        return
    now = _time.monotonic()
    if now - _RECODE_LAST.get(order.id, 0) < _RECODE_COOLDOWN_SEC:
        await cb.answer("⏳ چند ثانیه صبر کنید و دوباره بزنید.", show_alert=True)
        return
    _RECODE_LAST[order.id] = now
    provider, _ = await _service_provider(row.id, await _service_from_order(order))
    if not provider:
        await cb.answer("ارائه‌دهنده در دسترس نیست؛ به پشتیبانی پیام بدهید.", show_alert=True)
        return
    status, payload = await _oz_request(provider, "GET", _OZ_SERVICES,
                                        body={"order_id": _norm_int(order.panel_order_id, 0)})
    data = _api_data(payload)
    code = str((data or {}).get("code") or "").strip() if isinstance(data, dict) else ""
    if status in (404, 409):
        await cb.answer("❌ ارائه‌دهنده دیگر کدی برای این شماره نگه نمی‌دارد. به پشتیبانی پیام بدهید.",
                        show_alert=True)
        return
    if not code and not (200 <= status < 300):
        await cb.answer("❌ استعلام از ارائه‌دهنده موقتاً ناموفق بود؛ کمی بعد دوباره امتحان کنید.",
                        show_alert=True)
        return
    if not code:
        await cb.answer("⏳ هنوز کد جدیدی نرسیده. اول در اپِ تلگرام درخواستِ کد بدهید، "
                        "چند ثانیه بعد دوباره این دکمه را بزنید.", show_alert=True)
        return
    previous = _CODE_IN_NOTE.findall(order.note or "")
    if previous and previous[-1] == code:
        await cb.answer("ℹ️ کد جدیدی نیامده؛ آخرین کد همان قبلی است: " + code +
                        "\n\nاگر کد جدید خواسته‌اید، اول در اپِ تلگرام «ارسال مجدد کد» را بزنید و "
                        "چند ثانیه بعد دوباره امتحان کنید.", show_alert=True)
        return
    async with Session() as s:
        cur = await s.get(SmmOrder, order.id, with_for_update=True)
        if cur:
            cur.note = ((cur.note or "") + f" — کد: {code}")[-2000:]
            await s.commit()
    await cb.answer("✅ کد جدید رسید")
    await cb.message.answer(_code_message(code, order.link),
                            reply_markup=_code_kb(order.id, code), parse_mode="HTML")


@ozvinoo_router.callback_query(F.data == "vnum:ozinfo")
async def ozv_info(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await cb.answer("کشورهای این بخش از ارائه‌دهندهٔ OzVinoo همگام می‌شوند.", show_alert=True)


@ozvinoo_router.callback_query(F.data.startswith("vnum:ozbuy:"))
async def ozv_buy(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _purchase_alert
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    service_pk = _norm_int(cb.data.split(":")[-1], 0)
    async with Session() as s:
        country = await s.get(SmmService, service_pk)
    if not country or country.bot_id != row.id or not country.is_active or not str(country.service_id).startswith("ozv:"):
        await cb.answer("این سرویس دیگر فعال نیست.", show_alert=True)
        return
    provider, api_country_id = await _service_provider(row.id, country)
    if not provider or not provider.get("active") or not provider.get("token") or not api_country_id:
        await cb.answer("این سرویس فعلاً آمادهٔ استفاده نیست.", show_alert=True)
        return
    real_price = int(country.sell_rate) // 1000
    buyer, _ = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "", return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id)
        if not b or float(b.balance) < real_price:
            await cb.answer(f"❌ موجودی کافی نیست. کمبود: {real_price - int(float(b.balance) if b else 0):,} تومان", show_alert=True)
            return
    # === PATCH_VNUM_AUDIT_V1 === دو تپِ پشتِ‌سرهم = دو شماره و دو بار کسرِ پول؛ یکی در لحظه.
    _guard = (row.id, cb.from_user.id)
    if _guard in _BUY_INFLIGHT:
        await cb.answer("⏳ خریدِ قبلی شما در حال انجام است.", show_alert=True)
        return
    _BUY_INFLIGHT.add(_guard)
    try:
        await _ozv_buy_locked(cb, bot, row, country, provider, api_country_id, buyer, real_price)
    finally:
        _BUY_INFLIGHT.discard(_guard)


_BUY_INFLIGHT: set = set()


async def _ozv_buy_locked(cb, bot, row, country, provider, api_country_id, buyer, real_price):
    from tenant import _purchase_alert
    await cb.answer("⏳ در حال رزرو شماره…")
    status, payload = await _oz_request(provider, "POST", _OZ_NUMBERS, body={"country_id": api_country_id, "none_report": _tier_none_report(provider, country)})
    data = _api_data(payload)
    if status < 200 or status >= 300 or (isinstance(payload, dict) and payload.get("status") is False) or not isinstance(data, dict):
        await cb.message.answer(f"❌ رزرو شماره انجام نشد: {_api_message(status, payload)}\nوجهی از کیف‌پولت کسر نشد.")
        return
    api_order_id = _norm_int(data.get("order_id") or data.get("id"), 0)
    phone = str(data.get("number") or data.get("phone") or "").strip()
    if not api_order_id or not phone:
        await cb.message.answer("❌ پاسخ ارائه‌دهنده ناقص بود؛ وجهی از کیف‌پولت کسر نشد.")
        return
    try:
        from models import next_tracking as _next_tracking  # PATCH_SEQ_TRACKING_V1
        trk = await _next_tracking(row.id)
        async with Session() as s:
            b = await s.scalar(select(BotUser).where(
                BotUser.id == buyer.id, BotUser.bot_id == row.id).with_for_update())
            if not b or float(b.balance) < real_price:
                await _oz_request(provider, "POST", _OZ_CANCEL, body={"order_id": api_order_id})
                await cb.message.answer("❌ موجودی در لحظهٔ نهایی کافی نبود؛ سفارش ارائه‌دهنده لغو شد.")
                return
            b.balance = float(b.balance) - real_price
            order = SmmOrder(bot_id=row.id, user_telegram_id=cb.from_user.id,
                             smm_service_pk=country.id, service_id=country.service_id,
                             service_name=country.name, link=phone, quantity=1,
                             price_toman=real_price, status="sent",
                             panel_order_id=str(api_order_id), panel_status="pending",
                             tracking_code=trk, note=f"خرید خودکار شماره از OzVinoo — {country.name}")
            s.add(order)
            s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-real_price,
                            kind="smm_order", note=f"خرید شمارهٔ مجازی OzVinoo — {country.name}"))
            await s.commit()
            local_order_id = order.id
    except Exception:
        # شماره از ارائه‌دهنده گرفته شده ولی ثبتِ سفارش/کسرِ پول خطا داد؛ شماره را آزاد کن تا
        # پولی هدر نرود و کاربر هم چیزی کسر نشده.
        logger.exception("OzVinoo buy: خطای دیتابیس بعد از رزروِ شماره؛ لغوِ شمارهٔ ارائه‌دهنده")
        try:
            await _oz_request(provider, "POST", _OZ_CANCEL, body={"order_id": api_order_id})
        except Exception:
            logger.exception("OzVinoo buy: لغوِ شمارهٔ رزروشده هم ناموفق بود")
        await cb.message.answer("❌ خطای موقت؛ وجهی از کیف‌پولت کسر نشد. دوباره امتحان کن.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 دریافت کد پیامک", callback_data=f"vnum:ozcode:{local_order_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو سفارش و بازگشت وجه", callback_data=f"vnum:ozcancel:{local_order_id}", style=ButtonStyle.DANGER)],
    ])
    await cb.message.answer(
        f"✅ <b>شماره آماده شد</b> 🎉\n\n🌍 کشور: {html.escape(country.name)}\n"
        f"📱 شماره: <code>{html.escape(phone)}</code>\n💳 مبلغ: {real_price:,} تومان\n"
        f"🔑 کد پیگیری: <code>{trk}</code>\n\nپس از دریافت کد، دکمهٔ زیر را بزنید.",
        reply_markup=kb, parse_mode="HTML")
    try:
        await _purchase_alert(bot, row, buyer=buyer, section="خدمات مجازی تلگرام",
                              detail=f"📱 شمارهٔ مجازی (خودکار) — {country.name}",
                              amount=real_price, order_id=local_order_id, product_id=country.id,
                              tracking_code=trk, count=1)
    except Exception:
        logger.exception("OzVinoo purchase alert failed")


@ozvinoo_router.callback_query(F.data.startswith("vnum:ozcode:"))
async def ozv_code(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    local_id = _norm_int(cb.data.split(":")[-1], 0)
    async with Session() as s:
        order = await s.get(SmmOrder, local_id)
    if (not order or order.bot_id != row.id or order.user_telegram_id != cb.from_user.id
            or not str(order.service_id).startswith("ozv:")):
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    provider, _ = await _service_provider(row.id, await _service_from_order(order))
    if not provider:
        await cb.answer("ارائه‌دهنده پیدا نشد.", show_alert=True)
        return
    result = await _process_code(bot, order, provider, _norm_int(order.panel_order_id, 0))
    if result == "pending":
        await cb.answer("⏳ هنوز کدی دریافت نشده؛ کمی بعد دوباره امتحان کنید.", show_alert=True)
    elif result == "completed":
        await cb.answer("✅ کد ارسال شد")
    elif result == "canceled":
        await cb.answer("این سفارش لغو شده و وجه برگشته است.", show_alert=True)
    elif result == "missing":
        await cb.answer("❌ سفارش در ارائه‌دهنده پیدا نشد.", show_alert=True)
    else:
        await cb.answer("❌ دریافت کد موقتاً ناموفق بود.", show_alert=True)


async def _service_from_order(order: SmmOrder) -> SmmService:
    async with Session() as s:
        svc = await s.get(SmmService, order.smm_service_pk) if order.smm_service_pk else None
    return svc


@ozvinoo_router.callback_query(F.data.startswith("vnum:ozcancel:"))
async def ozv_cancel_order(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    local_id = _norm_int(cb.data.split(":")[-1], 0)
    async with Session() as s:
        order = await s.get(SmmOrder, local_id)
        svc = await s.get(SmmService, order.smm_service_pk) if order and order.smm_service_pk else None
    # === PATCH_VNUM_AUDIT_V1 === فقط صاحبِ سفارش، و فقط تا وقتی کد نیامده (status=sent).
    # قبلاً هر کاربرِ همین ربات می‌توانست با callback_dataِ دستی سفارشِ دیگران را لغو کند و
    # برایِ سفارشِ تحویل‌شده هم درخواستِ لغو به ارائه‌دهنده می‌رفت.
    if not order or order.bot_id != row.id or not svc or order.user_telegram_id != cb.from_user.id:
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    if order.status != "sent":
        await cb.answer("این سفارش دیگر قابل لغو نیست.", show_alert=True)
        return
    provider, _ = await _service_provider(row.id, svc)
    if not provider:
        await cb.answer("ارائه‌دهنده پیدا نشد.", show_alert=True)
        return
    status, payload = await _oz_request(provider, "POST", _OZ_CANCEL, body={"order_id": _norm_int(order.panel_order_id, 0)})
    # === PATCH_OZV_CANCEL_LOG_V1 === پاسخِ واقعیِ ارائه‌دهنده ثبت می‌شود تا «لغو شد ولی پول برنگشت»
    # قابلِ بررسی باشد، و دلیلِ رد (اگر داد) به‌جایِ پیامِ کلی به کاربر نشان داده می‌شود.
    logger.info("[ozv-cancel] order=%s api_order=%s http=%s payload=%s", local_id,
                order.panel_order_id, status, str(payload)[:300])
    if status not in (200, 201) and not (isinstance(payload, dict) and "cancel" in str(payload.get("message", "")).lower()):
        _why = _api_message(status, payload)
        await cb.answer(("❌ لغو سفارش توسط ارائه‌دهنده پذیرفته نشد.\n" + _why)[:190], show_alert=True)
        return
    if await _refund_order(bot, local_id, "بازگشت وجه سفارش شمارهٔ مجازی OzVinoo"):
        await cb.answer("✅ سفارش لغو و وجه برگشت داده شد")
    else:
        await cb.answer("سفارش قبلاً نهایی شده است.", show_alert=True)


# === PATCH_OZV_AUTOCANCEL_V1 ===
# درخواستِ ادمین: «اگر ۱۰ دقیقه کد نیامد، شماره خودکار از پنل لغو شود».
# فقط وقتی مبلغ برمی‌گردد که خودِ OzVinoo لغو را بپذیرد (مثل دکمهٔ دستیِ لغو)؛ اگر رد کند
# (مثلاً «Failed to update flood time») وجهی برنمی‌گردد و چند دقیقه بعد دوباره تلاش می‌شود.
# مدت با Setting «ozv_autocancel_min» قابلِ تنظیم است (پیش‌فرض ۱۰، صفر = خاموش).
OZV_AUTOCANCEL_DEFAULT_MIN = 10
_AUTOCANCEL_RETRY_SEC = 120
_AUTOCANCEL_GIVEUP_SEC = 6 * 3600
_STUCK_MARK = "⚠️ اعلانِ سفارشِ گیرکرده به مالک"
_AUTOCANCEL_LAST: dict[int, float] = {}


async def _ozv_alert_stuck(bot: Bot, order: SmmOrder, age: float) -> None:
    """یک‌بار به مالکِ ربات: این سفارش کد نگرفته و ارائه‌دهنده لغوش نمی‌کند؛ دستی رسیدگی شود."""
    async with Session() as s:
        cur = await s.get(SmmOrder, order.id, with_for_update=True)
        if not cur or cur.status != "sent" or _STUCK_MARK in (cur.note or ""):
            return
        brow = await s.get(BotRow, cur.bot_id)
        cur.note = ((cur.note or "") + f" | {_STUCK_MARK}")[-2000:]
        await s.commit()
        owner_id = brow.owner_id if brow else None
        trk, uid, price, phone = cur.tracking_code, cur.user_telegram_id, int(cur.price_toman or 0), cur.link
    if not owner_id:
        return
    try:
        await bot.send_message(
            owner_id,
            "⚠️ <b>سفارشِ شمارهٔ مجازی گیرکرده</b>\n\n"
            f"کد پیگیری: <code>{trk}</code>\nکاربر: <code>{uid}</code>\n"
            f"شماره: <code>{html.escape(str(phone))}</code>\nمبلغ: {price:,} تومان\n"
            f"سن سفارش: {int(age // 3600)} ساعت\n\n"
            "کدی نرسیده و ارائه‌دهنده لغوش را رد کرده، پس به‌صورت خودکار وجه برنگشته. "
            "اگر مطمئن هستید، از «مدیریت کیف‌پول کاربر» مبلغ را دستی برگردانید و در OzVinoo پیگیری کنید.",
            parse_mode="HTML")
    except Exception:
        logger.exception("ozv: اعلانِ سفارشِ گیرکرده به مالک ارسال نشد")


async def _autocancel_minutes(bot_id: int) -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "ozv_autocancel_min"))
    raw = (r.value if r and r.value is not None else "")
    try:
        v = int(float(raw)) if str(raw).strip() != "" else OZV_AUTOCANCEL_DEFAULT_MIN
    except (TypeError, ValueError):
        v = OZV_AUTOCANCEL_DEFAULT_MIN
    return max(0, min(v, 240))


async def _ozv_maybe_autocancel(bot: Bot, order: SmmOrder, provider: dict) -> bool:
    import time as _time
    import datetime as _dt
    minutes = await _autocancel_minutes(order.bot_id)
    if minutes <= 0 or not order.created_at:
        return False
    age = (_dt.datetime.utcnow() - order.created_at).total_seconds()
    if age < minutes * 60:
        return False
    # === PATCH_OZV_AUTOCANCEL_GIVEUP_V1 === سفارش‌هایِ چندروزهٔ گیرکرده (که ارائه‌دهنده دیگر لغوشان
    # نمی‌کند: «Failed to update flood time») نباید تا ابد هر چند دقیقه به ارائه‌دهنده فشار بیاورند.
    # بعد از _AUTOCANCEL_GIVEUP_SEC دیگر لغو نمی‌زنیم و فقط یک‌بار به مالکِ ربات خبر می‌دهیم تا دستی
    # (با بازگشتِ وجهِ دستی) رسیدگی کند.
    if age > _AUTOCANCEL_GIVEUP_SEC:
        await _ozv_alert_stuck(bot, order, age)
        return False
    now = _time.monotonic()
    retry_gap = _AUTOCANCEL_RETRY_SEC if age < minutes * 60 + 1800 else 600
    if now - _AUTOCANCEL_LAST.get(order.id, 0) < retry_gap:
        return False
    _AUTOCANCEL_LAST[order.id] = now
    status, payload = await _oz_request(provider, "POST", _OZ_CANCEL,
                                        body={"order_id": _norm_int(order.panel_order_id, 0)})
    logger.info("[ozv-autocancel] order=%s age=%.0fs http=%s payload=%s", order.id, age, status, str(payload)[:300])
    ok = status in (200, 201) or (isinstance(payload, dict) and "cancel" in str(payload.get("message", "")).lower())
    if not ok:
        return False
    refunded = await _refund_order(
        bot, order.id, "لغو خودکار (کد نرسید) سفارش شمارهٔ مجازی OzVinoo",
        text=f"⏱ تا {minutes} دقیقه کدی نرسید؛ سفارش خودکار لغو شد و {{amount}} تومان به کیف‌پولت برگشت داده شد.")
    _AUTOCANCEL_LAST.pop(order.id, None)
    return refunded


async def _ozv_check_one(bot: Bot, order: SmmOrder) -> None:
    async with Session() as s:
        svc = await s.get(SmmService, order.smm_service_pk) if order.smm_service_pk else None
    if not svc:
        return
    provider, _ = await _service_provider(order.bot_id, svc)
    if not provider:
        return
    result = await _process_code(bot, order, provider, _norm_int(order.panel_order_id, 0))
    if result == "pending":
        await _ozv_maybe_autocancel(bot, order, provider)
        return
    if result == "missing":
        status, payload = await _oz_request(provider, "POST", _OZ_CANCEL, body={"order_id": _norm_int(order.panel_order_id, 0)})
        cancel_ok = status in (200, 201) or (isinstance(payload, dict) and "cancel" in str(payload.get("message", "")).lower())
        if cancel_ok:
            await _refund_order(bot, order.id, "لغو خودکار سفارش شمارهٔ مجازی OzVinoo")


async def ozvinoo_poll_once() -> int:
    async with Session() as s:
        orders = (await s.scalars(select(SmmOrder).where(
            SmmOrder.status == "sent", SmmOrder.service_id.like("ozv:%")).order_by(SmmOrder.id.desc()).limit(300))).all()
        bots = {o.bot_id: await s.get(BotRow, o.bot_id) for o in orders}
    # === PATCH_VNUM_AUDIT_V1 === جدیدترین‌ها اول (قبلاً ۱۰۰ سفارشِ قدیمیِ گیرکرده جایِ بقیه را می‌گرفتند)
    # و برایِ هر ربات فقط یک Bot/session ساخته می‌شود، نه یکی به‌ازایِ هر سفارش.
    done = 0
    live: dict[int, Bot] = {}
    try:
        for order in orders:
            brow = bots.get(order.bot_id)
            if not brow or brow.status != "active" or not brow.token:
                continue
            bot = live.get(order.bot_id)
            if bot is None:
                bot = live[order.bot_id] = Bot(token=brow.token)
            try:
                await _ozv_check_one(bot, order)
                done += 1
            except Exception:
                logger.exception("OzVinoo polling failed for order %s", order.id)
    finally:
        for b in live.values():
            try:
                await b.session.close()
            except Exception:
                logger.exception("OzVinoo poll: بستنِ session")
    return done


# === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
# گزارشِ ادمین: «قیمتِ خرید هی بالا پایین می‌شه... کاش به‌جایِ این دکمه،
# قیمت رو همش خودش استعلام بگیره، مثلاً هر ۱۰ دقیقه».
#
# او درست می‌گوید و ریشه را هم درست تشخیص داده. تا امروز `ozvinoo_poll_loop`
# فقط وضعیتِ *سفارش‌ها* را چک می‌کرد و هیچ‌وقت قیمت‌ها را به‌روز نمی‌کرد.
# قیمت‌ها فقط وقتی عوض می‌شدند که مالک دستی «🔄 همگام‌سازی» را می‌زد — پس
# بینِ دو همگام‌سازی، ربات با نرخِ کهنه می‌فروخت. اگر تعرفهٔ Callinoo بالا
# می‌رفت، فروش زیرِ قیمتِ تمام‌شده انجام می‌شد (ضرر)، و اگر پایین می‌آمد،
# گران‌تر از بازار (ازدست‌رفتنِ فروش). «بالا پایین شدن» که او می‌دید،
# اختلافِ همین نرخِ کهنه با تعرفهٔ زندهٔ پنلش بود.
#
# محاسبهٔ سود خودش سالم است و بازبینی شد:
#     نمایش = round(API_price × (1 + margin/100))
# یعنی نه ×۱۰ و نه +۱۰ در کار نیست؛ فقط ورودی کهنه بود.
#
# فاصلهٔ به‌روزرسانی برایِ هر ربات جدا قابلِ تنظیم است:
#     Setting: ozv_price_sync_min   (پیش‌فرض "10" دقیقه، صفر = خاموش)

OZV_PRICE_SYNC_DEFAULT_MIN = 10


async def _price_sync_minutes(bot_id: int) -> int:
    """فاصلهٔ استعلامِ خودکارِ قیمت به دقیقه. صفر یعنی خاموش."""
    async with Session() as s:
        r = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "ozv_price_sync_min"))
    try:
        v = int(float((r.value if r else "") or OZV_PRICE_SYNC_DEFAULT_MIN))
    except (TypeError, ValueError):
        return OZV_PRICE_SYNC_DEFAULT_MIN
    if v <= 0:
        return 0
    # کفِ ۲ دقیقه تا یک تنظیمِ اشتباه، API را زیرِ بار نبرد
    return max(2, min(v, 1440))


_last_price_sync: dict[str, float] = {}


async def ozvinoo_price_sync_once() -> int:
    """قیمت و موجودیِ همهٔ ارائه‌دهنده‌هایِ فعال را از API تازه می‌کند.

    از همان `_sync_provider` استفاده می‌کند که همگام‌سازیِ دستی می‌زند، پس
    رفتار دقیقاً یکی است — از جمله احترام به قیمت‌هایِ دستیِ هر کشور
    (price_overrides) و به «اصلاحِ قیمتِ خرید».
    """
    import time as _time
    now = _time.monotonic()
    synced = 0
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(
            Setting.key == "ozvinoo_providers"))).all()
        bot_ids = [r.bot_id for r in rows]
    for bot_id in bot_ids:
        every = await _price_sync_minutes(bot_id)
        if not every:
            continue
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active":
            continue
        for p in await _providers(bot_id):
            if not p.get("enabled", True) or not p.get("token"):
                continue
            mark = f"{bot_id}:{p.get('id')}"
            last = _last_price_sync.get(mark, 0.0)
            if now - last < every * 60:
                continue
            _last_price_sync[mark] = now
            try:
                ok, msg, count = await _sync_provider(bot_id, p)
                if ok:
                    synced += 1
                    logger.info("OzVinoo: قیمتِ %d کشور برایِ ربات %s تازه شد.",
                                count, bot_id)
                else:
                    # === PATCH_OZV_AUTH_BACKOFF_V1 ===
                    # لاگِ سرور: ۴۵۰ هشدارِ «Toekn is wrong» در ۲۴ ساعت — کلیدِ API چند رباتِ
                    # مشتری اشتباه/منقضی است و هر چرخه دوباره امتحان و لاگ می‌شد. حالا بعد از
                    # خطا ۶ ساعت صبر می‌کنیم و فقط همان یک بار (به‌جایِ هر چرخه) لاگ می‌شود.
                    _last_price_sync[mark] = now + 6 * 3600 - every * 60
                    p["last_error"] = str(msg)[:200]
                    logger.warning("OzVinoo: استعلامِ خودکارِ قیمت ناموفق "
                                   "(bot=%s): %s — ۶ ساعتِ بعد دوباره", bot_id, msg)
            except Exception:
                logger.exception("OzVinoo: استعلامِ خودکارِ قیمت شکست خورد "
                                 "(bot=%s)", bot_id)
        # ذخیرهٔ تغییراتِ last_sync/last_error رویِ خودِ ارائه‌دهنده‌ها
        try:
            await _save_providers(bot_id, await _providers(bot_id))
        except Exception:
            logger.exception("OzVinoo: ذخیرهٔ وضعیتِ ارائه‌دهنده ناموفق بود")
    return synced


async def ozvinoo_price_sync_loop(interval_sec: int = 60):
    """هر دقیقه بیدار می‌شود ولی فقط ارائه‌دهنده‌هایی را می‌زند که مهلتشان
    رسیده — پس فاصلهٔ واقعی همان چیزی است که مالک تنظیم کرده."""
    await asyncio.sleep(45)
    while True:
        try:
            await ozvinoo_price_sync_once()
        except Exception:
            logger.exception("ozvinoo_price_sync_loop failed")
        await asyncio.sleep(interval_sec)


async def ozvinoo_poll_loop(interval_sec: int = 35):
    await asyncio.sleep(20)
    while True:
        try:
            await ozvinoo_poll_once()
        except Exception:
            logger.exception("OzVinoo background poll loop failed")
        await asyncio.sleep(interval_sec)


@ozvinoo_router.callback_query(F.data.startswith("ozv:delete:"))
async def ozv_delete(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row or not await _require_access(cb, row):
        return
    pid = cb.data.split(":", 2)[2]
    providers = await _providers(row.id)
    providers = [p for p in providers if p.get("id") != pid]
    await _save_providers(row.id, providers)
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.service_id.like(f"ozv:{pid}:%")))).all()
        for svc in svcs:
            svc.is_active = False
        await s.commit()
    text, kb = await _menu_content(row.id)
    await _render(cb, "✅ ارائه‌دهنده غیرفعال شد و سرویس‌هایش از فروش خارج شدند.\n\n" + text, kb)
    await cb.answer("حذف شد")


# Public helper for the keyboard-text dispatcher.
async def ozvinoo_menu_for_text(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    return await _menu_content(bot_id)


async def ozvinoo_shop_items(bot_id: int) -> list[SmmService]:
    """کشورهای فعال OzVinoo را برای فروشگاه شمارهٔ مجازی فعلی برمی‌گرداند."""
    async with Session() as s:
        return (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id,
            SmmService.is_active == True,
            SmmService.service_id.like("ozv:%")
        ).order_by(SmmService.id))).all()  # noqa: E712


# Public helper for the worker; kept separate from Telegram handlers.
__all__ = ["ozvinoo_router", "ozvinoo_poll_loop", "ozvinoo_poll_once",
           "ozvinoo_price_sync_loop", "ozvinoo_price_sync_once", "ozvinoo_menu_for_text", "ozvinoo_shop_items"]


# Compatibility helper used by handlers above.
async def _service_provider(bot_id: int, svc: SmmService) -> tuple[dict | None, int | None]:
    parts = str(svc.service_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ozv":
        return None, None
    providers = await _providers(bot_id)
    # پسوندِ «:c» = ردهٔ ارزان در حالتِ «هر دو رده» (PATCH_OZVINOO_TIER_BOTH_V1)
    return _provider(providers, parts[1]), _norm_int(parts[2].split(":")[0], 0)
