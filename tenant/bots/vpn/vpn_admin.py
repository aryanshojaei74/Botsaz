"""
vpn_admin.py — پنل مدیریت «VPN خودکار» برای صاحب ربات فروشندهٔ VPN.

فقط مالک ربات (نه ادمین‌های پشتیبانی) به این بخش دسترسی دارد چون شامل
اعتبارنامهٔ حساس پنل است.

قابلیت‌ها:
  • اتصال به چند پنل (سنایی / پاسارگاد / مرزبان / علیرضا)
  • نمایش اطلاعات همهٔ پنل‌های متصل در بالای منو
  • ثبت/ویرایش تنظیمات هر پنل X-UI (آدرس، یوزر، رمز، آدرس ساب، inboundها)
  • تست اتصال به هر پنل
  • فعال/غیرفعال‌کردن فروش خودکار برای هر پنل
  • تعریف/حذف پلن‌های فروش (حجم/مدت/قیمت/محدودیت IP)

callbackها: همه با پیشوند tvpn:
  tvpn:menu              — منوی اصلی VPN خودکار
  tvpn:panels            — لیست پنل‌ها (انتخاب پنل برای مدیریت)
  tvpn:panel:{key}       — منوی مدیریت یک پنل خاص
  tvpn:setup:{key}       — شروع ثبت/ویرایش یک پنل
  tvpn:test:{key}        — تست اتصال به یک پنل
  tvpn:toggle:{key}      — فعال/غیرفعال کردن یک پنل
  tvpn:delete:{key}         — درخواست حذف یک پنل
  tvpn:delete_ok:{key}      — تأیید حذف یک پنل
  tvpn:refresh_subs:{key}   — بروزرسانی اینباندها و ساب‌لینک کاربران
"""
from html import escape as _esc_html
import json
import re
# === PATCH_VPN_PANEL_ERROR_HTML_CRASH_V1 ===
import html as _html_escape_mod  # برای escape کردنِ متنِ خامِ پاسخِ پنل قبل از ارسال در پیامِ HTML

import asyncio  # PATCH_QR_BACKGROUND_V1

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,  # PATCH_QR_BACKGROUND_V1
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, VpnPanel, VpnPlan, Setting
from tenant.shared.panel_clients.vpn_panel import PanelError
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
from plans import has_feature, Feature, upgrade_hint

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

vpn_admin_router = Router()

# ─────────────── ثوابت پنل‌ها ───────────────
class _PanelLabels(dict):
    """دیکشنریِ برچسبِ پنل‌ها که کلیدهای نمونه‌دار («pasargad#2») را هم
    می‌فهمد — تا همهٔ جاهایی که از PANEL_LABELS.get(key, key) استفاده می‌کنند
    (گزارش‌ها، سبد خرید، نفراتِ برتر، سلامتِ پلتفرم) بدونِ تغییر، اسمِ درست و
    شمارهٔ پنل را نشان بدهند. === PATCH_MULTI_PANEL_INSTANCES_V1 ==="""

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.get(self, key)
        base, sep, inst = str(key or "").partition("#")
        if sep and inst.isdigit() and dict.__contains__(self, base):
            return f"{dict.get(self, base)} #{inst}"
        return default


PANEL_LABELS = _PanelLabels({
    "sanai":          "🔵 پنل سنایی",
    "pasargad":       "🟣 پنل پاسارگاد",
    "pasarguard":     "🛡️ پنل PasarGuard",
    "marzban":        "🟠 پنل مرزبان",
    "marzneshin":     "⚪️ پنل مرزنشین",
    "hiddify":        "🟢 پنل هیدیفای",
    "rebecca":        "🔴 پنل ربکا",
    "alireza":        "🟢 پنل علیرضا",
    "upanel_mizban":  "🟡 یوپنل (مرزبان)",
    "shahan":         "🟤 پنل شاهان",
    # === PATCH_NODEX_PANEL_V1 === فقط پلنِ حرفه‌ای (نگاه کن به _panel_labels_for_plan)
    "nodex":          "🔷 پنل نودکس",
    # === PATCH_DEGERIS_PANEL_V1 === فقط پلنِ حرفه‌ای
    "degeris":        "🟣 پنل DEGERIS (ثنایی جدید)",
    # === PATCH_EYLAN_PANEL_V1 === فقط پلنِ حرفه‌ای
    "eylan":          "🟠 پنل ایلان (EylanPanel)",
})
PANEL_KEYS = list(PANEL_LABELS.keys())

# === PATCH_NODEX_PANEL_V1 ===
# پنل‌هایی که علاوه‌بر PANEL_INTEGRATION (رشد+)، یک Feature اختصاصیِ
# دیگر هم لازم دارند تا در لیستِ انتخاب نشان داده شوند. فعلاً فقط نودکس
# (پلنِ حرفه‌ای). برایِ پنلِ بعدی که بخواهد فقط حرفه‌ای باشد، همین‌جا
# یک ردیفِ دیگر اضافه کن.
_PANEL_EXTRA_FEATURE_GATE = {"nodex": Feature.NODEX_PANEL, "degeris": Feature.DEGERIS_PANEL,
                             "eylan": Feature.EYLAN_PANEL}


def _panel_labels_for_plan(plan) -> dict:
    """PANEL_LABELS را برای این پلن فیلتر می‌کند (پنل‌هایِ گیت‌شده که این
    پلن دسترسی ندارد حذف می‌شوند)."""
    out = {}
    for key, label in PANEL_LABELS.items():
        gate = _PANEL_EXTRA_FEATURE_GATE.get(panel_base_key(key))
        if gate and not has_feature(plan, gate):
            continue
        out[key] = label
    return out


# === PATCH_MULTI_PANEL_INSTANCES_V1 ===
# چند پنل از یک نوع: panel_key نمونه‌های بعدی به شکل «<نوع>#<شماره>» ذخیره
# می‌شود (مثلاً pasargad#2). نمونهٔ اول همان کلید ساده می‌ماند، پس هیچ ردیفِ
# قدیمی‌ای (VpnPanel / VpnPlan.panel_key / PanelAccount.panel_key / کلیدهای
# vc_* در Setting) دست نمی‌خورد و قیدِ یکتای (bot_id, panel_key) هم سرِ جایش
# باقی می‌ماند — یعنی این تغییر هیچ مهاجرتِ دیتابیسی لازم ندارد.


def panel_base_key(key: str | None) -> str:
    """نوعِ پنل را از کلیدِ نمونه بیرون می‌کشد: «pasargad#3» → «pasargad»."""
    return (key or "").split("#", 1)[0]


_bk = panel_base_key  # اسمِ کوتاه برای مقایسه‌های داخلِ همین فایل


def panel_instance_no(key: str | None) -> int:
    """شمارهٔ نمونه؛ ۱ برای کلیدِ ساده."""
    _, _, inst = (key or "").partition("#")
    return int(inst) if inst.isdigit() else 1


def is_panel_key(key: str | None) -> bool:
    """کلیدِ معتبر است؟ (هم کلیدِ ساده هم نمونهٔ شماره‌دار)."""
    return _bk(key) in PANEL_LABELS and (
        "#" not in (key or "") or str(key).split("#", 1)[1].isdigit())


async def next_panel_instance_key(bot_id: int, base_key: str) -> str:
    """اولین کلیدِ نمونهٔ آزاد برای این نوع پنل در این ربات."""
    panels = await get_all_panels(bot_id)
    used = {p.panel_key for p in panels}
    if base_key not in used:
        return base_key
    n = 2
    while f"{base_key}#{n}" in used:
        n += 1
    return f"{base_key}#{n}"


# ─────────────── تنظیمات فروش آنلاین (حجم/روز/تعداد دلخواه) — per-panel ───────────────
# کلیدهای پایه (بدون prefix پنل) برای تنظیمات مشترک (renew, notify)
_VC_DEFAULTS = {
    "vc_enabled": "0",
    "vc_min_gb": "5", "vc_max_gb": "100", "vc_step_gb": "5",
    "vc_min_days": "30", "vc_max_days": "90", "vc_step_days": "30",
    "vc_max_count": "5",
    # === PATCH_VC_SIMPLE_MODE_V1 === حالتِ ساده: کاربر فقط عددِ حجم را می‌فرستد؛ مدت ثابتِ ادمین است
    "vc_simple": "0", "vc_simple_days": "30",
    "vc_price_gb": "1000", "vc_price_day": "500",
    # === PATCH_RESELLER_GB_PRICING_V1 === خالی یعنی «تنظیم نشده»
    "rs_price_gb": "0", "rs_price_day": "0",
    # حالتِ فروش: "general" یعنی همیشه از بازهٔ حجم/مدت/قیمتِ عمومیِ بالا
    # استفاده شود (حتی اگر تعرفه‌های مدت‌دار هم تعریف شده باشند)؛
    # "tiers" یعنی تعرفه‌های مدت‌دار حاکم باشند. این تنظیم برای این است که
    # فقط یکی از این دو روش هم‌زمان روی خریدِ کاربر اثر بگذارد.
    "vc_pricing_mode": "general",
    "renew_enabled": "1",
    "renew_price_gb": "1000",
    "renew_price_day": "500",
    "renew_min_gb": "10",
    "renew_max_gb": "100",
    "renew_step_gb": "10",
    "renew_min_days": "7",
    "renew_max_days": "90",
    "renew_step_days": "7",
    "notify_gb_threshold": "2",
    "notify_days_threshold": "5",
    # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
    "traffic_delete_hours": "5",
}

# کلیدهای vc که به ازای هر پنل جداگانه ذخیره می‌شوند
_VC_PANEL_KEYS = [
    "vc_enabled", "vc_min_gb", "vc_max_gb", "vc_step_gb",
    "vc_min_days", "vc_max_days", "vc_step_days",
    "vc_max_count", "vc_price_gb", "vc_price_day", "vc_pricing_mode",
    "vc_simple", "vc_simple_days",
    "renew_enabled", "renew_price_gb", "renew_price_day",
    "renew_min_gb", "renew_max_gb", "renew_step_gb",
    "renew_min_days", "renew_max_days", "renew_step_days",
    "notify_gb_threshold", "notify_days_threshold",
    # === PATCH_RESELLER_GB_PRICING_V1 ===
    "rs_price_gb", "rs_price_day",
    # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
    "traffic_delete_hours",
]


def _pvc_key(panel_key: str, key: str) -> str:
    """کلید ذخیره‌سازی per-panel. برای کلیدهای vc_* الگوی قدیمی حفظ می‌شود
    (vc_enabled → vc_sanai_enabled) تا داده‌های قبلاً ذخیره‌شده خراب نشوند؛
    برای بقیهٔ کلیدها (renew_*/notify_*) پسوندِ پنل به انتها اضافه می‌شود
    (renew_price_gb → renew_price_gb_sanai)."""
    if key.startswith("vc_"):
        return f"vc_{panel_key}_{key[3:]}"
    return f"{key}_{panel_key}"


# ─────────────── تعرفه‌های مدت‌دار (قفل ماهانه) — فروش آنلاین ───────────────
# هر تعرفه یک بازهٔ روز (days_min–days_max) دارد و برای همان بازه، بازهٔ حجمِ
# مخصوصِ خودش (gb_min/gb_max/gb_step) و قیمتِ مخصوصِ خودش (price_gb/price_day)
# را تعریف می‌کند. مثلاً: «۳۰ روزه: ۵ تا ۵۰ گیگ، ۱۲۰۰ ت/گیگ» جدا از
# «۶۰ روزه: ۱۰ تا ۱۰۰ گیگ، ۱۰۰۰ ت/گیگ». اگر تعرفه‌ای تعریف نشده باشد، رفتار
# قدیمی (یک بازهٔ کلی برای کل ربات) بدون تغییر ادامه پیدا می‌کند.
import json as _json  # noqa: F811 (نگه‌داشتن نامِ محلی برای وضوح در این بخش؛ همان json بالای فایل)


def _vc_tiers_key(panel_key: str) -> str:
    return f"vc_tiers_{panel_key or 'default'}"


async def vc_tiers_get(bot_id: int, panel_key: str = "") -> list[dict]:
    """فهرستِ تعرفه‌های مدت‌دارِ این پنل را برمی‌گرداند (مرتب بر اساسِ days_min)."""
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == _vc_tiers_key(panel_key)))
    if not row or not row.value:
        return []
    try:
        tiers = _json.loads(row.value)
        if not isinstance(tiers, list):
            return []
        return sorted(tiers, key=lambda t: t.get("days_min", 0))
    except Exception:
        return []


async def vc_tiers_set(bot_id: int, panel_key: str, tiers: list[dict]) -> None:
    db_key = _vc_tiers_key(panel_key)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
        if row:
            row.value = _json.dumps(tiers, ensure_ascii=False)
        else:
            s.add(Setting(bot_id=bot_id, key=db_key, value=_json.dumps(tiers, ensure_ascii=False)))
        await s.commit()


def _tier_overlap(tiers: list[dict], new: dict, edit_idx: int | None) -> str | None:
    """=== PATCH_VPN_TIER_CUMULATIVE_V1 ===
    اگر پلهٔ جدید با پله‌ای که بازهٔ روزِ یکسان دارد در حجم هم‌پوشانی کند،
    پیامِ فارسیِ راهنما برمی‌گرداند؛ وگرنه None."""
    try:
        d_min, d_max = int(new["days_min"]), int(new["days_max"])
        g_min, g_max = int(new["gb_min"]), int(new["gb_max"])
    except (KeyError, TypeError, ValueError):
        return None
    for i, t in enumerate(tiers):
        if edit_idx is not None and i == edit_idx:
            continue
        try:
            td_min, td_max = int(t.get("days_min", 0)), int(t.get("days_max", 0))
            tg_min, tg_max = int(t.get("gb_min", 0)), int(t.get("gb_max", 0))
        except (TypeError, ValueError):
            continue
        # بازهٔ روز اصلاً مشترکی ندارد → مشکلی نیست
        if d_max < td_min or d_min > td_max:
            continue
        # بازهٔ حجم هم‌پوشانی دارد؟
        if g_min <= tg_max and g_max >= tg_min:
            return (f"⚠️ برای {td_min}–{td_max} روز، پلهٔ {tg_min}–{tg_max} گیگ "
                    f"از قبل وجود دارد و با {g_min}–{g_max} تداخل دارد.\n\n"
                    f"حداقل حجمِ این پله را از {tg_max + 1} به بالا انتخاب کن.")
    return None


def vc_gb_cost(brackets: list[tuple[int, int]], gb: int) -> int:
    """=== PATCH_VPN_TIER_CUMULATIVE_V1 ===
    هزینهٔ حجم به‌صورتِ **تجمعی** (مثلِ پله‌هایِ مالیاتی).

    brackets: [(سقفِ پله, قیمتِ هر گیگ), ...] مرتب از کوچک به بزرگ.
    مثال: پله‌هایِ ۵–۱۰ با ۱۰۰۰ و ۱۱–۲۰ با ۲۰۰۰ → brackets=[(10,1000),(20,2000)]
        ۱۵ گیگ = ۱۰ گیگِ اول×۱۰۰۰ + ۵ گیگِ بعدی×۲۰۰۰ = ۲۰,۰۰۰ تومان
    حجمِ بیشتر از آخرین سقف، با قیمتِ آخرین پله حساب می‌شود.
    """
    if not brackets:
        return 0
    cost = 0
    prev = 0
    for gmax, price in brackets:
        take = max(0, min(gb, gmax) - prev)
        cost += take * price
        prev = max(prev, gmax)
        if gb <= gmax:
            return cost
    cost += max(0, gb - prev) * brackets[-1][1]      # فراتر از آخرین پله
    return cost


def vc_tiers_for_days(tiers: list[dict], days: int) -> list[dict]:
    """=== PATCH_VPN_TIER_GB_BRACKETS_V1 ===
    همهٔ تعرفه‌هایی که بازهٔ روزشان شاملِ `days` است — مرتب بر اساسِ حجمِ
    شروع. چند تعرفه با بازهٔ روزِ یکسان ولی بازهٔ حجمِ متفاوت = «پله‌های
    حجمی» برایِ همان مدت (مثلاً ۳۰ روزه: زیر ۵۰ گیگ ۳۵۰۰ ت، بالای ۵۰ گیگ
    ۳۰۰۰ ت)."""
    out = []
    for t in tiers:
        try:
            if int(t.get("days_min", 0)) <= days <= int(t.get("days_max", 0)):
                out.append(t)
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda t: int(t.get("gb_min", 0) or 0))


def vc_tier_for_days(tiers: list[dict], days: int,
                     gb: int | None = None) -> dict | None:
    """تعرفهٔ منطبق با `days` (و اگر داده شود، با حجمِ `gb`).

    === PATCH_VPN_TIER_GB_BRACKETS_V1 ===
    قبلاً فقط رویِ بازهٔ روز تطبیق می‌داد و **اولین** منطبق را برمی‌گرداند؛
    برایِ همین اگر ادمین برایِ یک مدت چند پلهٔ حجمی می‌ساخت، فقط پلهٔ اول
    عمل می‌کرد و بقیه مرده بودند. حالا بینِ تعرفه‌هایِ هم‌مدت، آن‌که بازهٔ
    حجمش شاملِ `gb` است انتخاب می‌شود.

    اگر `gb` داده نشود یا در هیچ پله‌ای نیفتد، نزدیک‌ترین پله انتخاب
    می‌شود (پایین‌ترین پله وقتی حجم از همه کمتر است، بالاترین وقتی از همه
    بیشتر) — تا هیچ‌وقت بی‌قیمت نمانیم. با یک تعرفه در هر بازهٔ روز،
    رفتار دقیقاً مثلِ قبل است.
    """
    matched = vc_tiers_for_days(tiers, days)
    if not matched:
        return None
    if gb is None or len(matched) == 1:
        return matched[0]
    for t in matched:
        try:
            if int(t.get("gb_min", 0)) <= gb <= int(t.get("gb_max", 0)):
                return t
        except (TypeError, ValueError):
            continue
    # بیرونِ همهٔ پله‌ها → نزدیک‌ترین پله
    def _dist(t: dict) -> int:
        try:
            lo, hi = int(t.get("gb_min", 0)), int(t.get("gb_max", 0))
        except (TypeError, ValueError):
            return 10 ** 9
        return 0 if lo <= gb <= hi else (lo - gb if gb < lo else gb - hi)
    return min(matched, key=_dist)


async def _vc_get(bot_id: int, key: str, panel_key: str = "") -> str:
    """گرفتن مقدار یک تنظیم vc — اگر panel_key داده شود، per-panel می‌خواند.
    برای کلیدهای renew_*/notify_* که تازه per-panel شده‌اند، اگر مقدارِ
    مخصوصِ آن پنل هنوز ذخیره نشده باشد، برای سازگاری با قبل از مقدارِ
    عمومیِ قدیمی (بدون پسوندِ پنل) استفاده می‌شود، نه مستقیم مقدار پیش‌فرض."""
    db_key = _pvc_key(panel_key, key) if panel_key and key in _VC_PANEL_KEYS else key
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
    if row:
        return row.value
    if panel_key and db_key != key:
        async with Session() as s:
            legacy = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if legacy:
            return legacy.value
    return _VC_DEFAULTS.get(key, "")


async def _vc_set(bot_id: int, key: str, value: str, panel_key: str = ""):
    """ذخیره یک تنظیم vc — اگر panel_key داده شود، per-panel ذخیره می‌کند."""
    db_key = _pvc_key(panel_key, key) if panel_key and key in _VC_PANEL_KEYS else key
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=db_key, value=value))
        await s.commit()


async def _vc_all(bot_id: int, panel_key: str = "") -> dict:
    """گرفتن همهٔ تنظیمات vc — اگر panel_key داده شود، per-panel می‌خواند."""
    out = {}
    for k in _VC_DEFAULTS:
        out[k] = await _vc_get(bot_id, k, panel_key)
    return out


# ─────────────── تنظیم «نام سرورها مشتری» — per-panel ───────────────
# نامی که در زمان خرید و در ساب‌لینک (داخل اپ کلاینت کاربر) به‌جای نام
# پیش‌فرض سرور نمایش داده می‌شود. ادمین می‌تواند پیشوند دلخواه (مثلاً نام
# ربات خودش) و نوع شناسهٔ کاربر (یوزرنیم یا آیدی عددی) را تعیین کند.
# مقدارها به ازای هر پنل جدا ذخیره می‌شوند: srvname_prefix_{panel_key} / srvname_idtype_{panel_key}
# (همین کلیدها در tenant/vpn_user.py برای ساخت واقعی نام کانفیگ خوانده می‌شوند.)
_SRVNAME_DEFAULTS = {"srvname_prefix": "", "srvname_idtype": "id"}  # idtype: id | username


def _srvname_pkey(panel_key: str, key: str) -> str:
    return f"{key}_{panel_key}"


async def _srvname_get(bot_id: int, key: str, panel_key: str) -> str:
    db_key = _srvname_pkey(panel_key, key)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
    return row.value if row else _SRVNAME_DEFAULTS.get(key, "")


async def _srvname_set(bot_id: int, key: str, value: str, panel_key: str):
    db_key = _srvname_pkey(panel_key, key)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=db_key, value=value))
        await s.commit()


async def get_srvname_cfg(bot_id: int, panel_key: str) -> dict:
    """تنظیمات نام‌گذاری سرور این پنل: {'prefix': ..., 'idtype': 'id'|'username'}."""
    return {
        "prefix": await _srvname_get(bot_id, "srvname_prefix", panel_key),
        "idtype": await _srvname_get(bot_id, "srvname_idtype", panel_key),
    }


def _srvname_preview(prefix: str, idtype: str, bot_username: str | None,
                      tg_id: int, tg_username: str | None) -> str:
    """فقط برای نمایش پیش‌نمایش در پنل مدیریت — منطق ساخت واقعی نام (به‌همراه
    پسوند یکتا) در tenant/vpn_user.py → build_srv_name/_make_config_email است."""
    base = re.sub(r"[^a-zA-Z0-9_]", "", prefix or "")[:20]
    if not base:
        raw = (bot_username or "bot").lower()
        base = re.sub(r"[^a-z0-9]", "", raw)
        if base.endswith("bot") and len(base) > 3:
            base = base[:-3]
        base = base[:12] or "bot"
    if idtype == "custom":  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
        # فقط پیش‌نمایش است؛ نام واقعی از اسمی که خودِ کاربر موقعِ خرید وارد
        # می‌کند ساخته می‌شود (و اگر وارد نکند، اول به یوزرنیمش، بعد آیدیِ
        # عددی برمی‌گردد — نگاه کن به PATCH_SRVNAME_CUSTOM_SKIP_USERNAME_V1).
        ident = "NameByUser"
    elif idtype == "username" and tg_username:
        ident = re.sub(r"[^a-zA-Z0-9_]", "", tg_username)[:20] or str(tg_id)
    else:
        ident = str(tg_id)
    return f"{base}{ident}"


# ─────────────── یادداشت‌های per-panel (فاکتور / تحویل کانفیگ) ───────────────
# دو یادداشت دلخواه که ادمین برای هر پنل تنظیم می‌کند:
#   • note_invoice_{panel_key}  → زیر قیمت در «پیش‌فاکتور» به خریدار نمایش داده می‌شود.
#   • note_delivery_{panel_key} → در پیام «تحویل کانفیگ» به کاربر نمایش داده می‌شود.
# هر دو با همان الگوی کلید-مقدارِ Setting ذخیره می‌شوند (بدون نیاز به مهاجرت دیتابیس).
def _note_pkey(panel_key: str, kind: str) -> str:
    return f"note_{kind}_{panel_key}"


async def get_panel_note(bot_id: int, panel_key: str, kind: str) -> str:
    """یادداشت ذخیره‌شدهٔ این پنل را برمی‌گرداند (kind: 'invoice' | 'delivery').
    اگر تنظیم نشده باشد رشتهٔ خالی برمی‌گرداند. این تابع از فایل‌های دیگر
    (vpn_user برای فاکتور/تحویل) هم فراخوانی می‌شود."""
    if kind not in ("invoice", "delivery"):
        return ""
    db_key = _note_pkey(panel_key, kind)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == db_key))
    return (row.value if row and row.value else "").strip()


async def set_panel_note(bot_id: int, panel_key: str, kind: str, value: str):
    db_key = _note_pkey(panel_key, kind)
    value = (value or "").strip()
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == db_key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=db_key, value=value))
        await s.commit()


# ─────────────── FSM ───────────────
class VpnPanelFSM(StatesGroup):
    panel_key = State()   # کلید پنلی که داریم تنظیم می‌کنیم (در state.data)
    base_url = State()
    username = State()
    password = State()
    # === PATCH_PANEL_API_KEY_AUTH_V1 ===
    api_key = State()
    sub_base = State()
    inbound_ids = State()


class VpnResellerPriceFSM(StatesGroup):   # PATCH_RESELLER_GB_PRICING_V1
    prices = State()


class VpnCustomFSM(StatesGroup):
    gb_range = State()
    days_range = State()
    max_count = State()
    prices = State()
    tier_wizard = State()   # افزودن تعرفهٔ مدت‌دار با دکمه‌های شیشه‌ای، فیلد به فیلد
    ip_limit = State()      # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
    simple_days = State()   # === PATCH_VC_SIMPLE_MODE_V1 ===


# === PATCH_PANEL_CUSTOM_LABEL_V1 ===
class VpnPanelLabelFSM(StatesGroup):
    label = State()


class VpnSrvNameFSM(StatesGroup):
    prefix = State()


class VpnNoteFSM(StatesGroup):
    invoice = State()    # یادداشت زیر قیمت در پیش‌فاکتور (panel_key در state.data)
    delivery = State()   # یادداشت در توضیحات تحویل کانفیگ (panel_key در state.data)


class VpnRenewFSM(StatesGroup):
    gb_range = State()
    days_range = State()
    prices = State()


class VpnNotifyFSM(StatesGroup):
    gb_threshold = State()
    days_threshold = State()
    # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
    delete_hours = State()


class VpnCodeSearchFSM(StatesGroup):
    code = State()      # جستجوی کانفیگ با کد سرویس
    add_gb = State()    # مقدار حجم برای افزودن (aid در state.data)
    add_days = State()  # تعداد روز برای افزودن
    rename = State()    # نام جدید سرویس


class VpnPlanEditFSM(StatesGroup):
    value = State()   # مقدار جدید یکی از فیلدهای پلن (field/plan_id در state.data)


class VpnPlanFSM(StatesGroup):
    title = State()
    gb = State()
    days = State()
    price = State()
    limit_ip = State()
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    category = State()


# === PATCH_VPN_PLAN_CATEGORY_RENAME_V1 ===
class VpnCatRenameFSM(StatesGroup):
    name = State()


# ─────────────── کمک‌تابع‌ها ───────────────
def rial(n) -> str:
    return f"{int(n):,} تومان"


def fa_to_int(text: str):
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    t = (text or "").translate(trans).replace(",", "").replace("،", "").strip()
    try:
        n = int(float(t))
    except (ValueError, TypeError, OverflowError):   # «inf»/«1e400» قبلاً OverflowError می‌داد
        return None
    # بزرگ‌تر از این در هیچ ستونی جا نمی‌شود (پولی‌ها NUMERIC(14,0) هستند)
    return n if abs(n) <= 10 ** 13 else None


async def _row(bot: Bot) -> BotRow | None:
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == bot.token))


def _fa_num(s: str) -> str:
    return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


# === PATCH_VPN_ADMIN_OWNER_SUPPORT_FIX_V1 ===
# قبلاً فقط برابریِ مستقیمِ from_user.id == row.owner_id چک می‌شد؛
# ادمین‌های پشتیبانیِ ثبت‌شده در جدولِ AdminSupport (از طریقِ «دسترسیِ
# پشتیبانی» در پنلِ ادمینِ پلتفرم) با پیامِ «فقط مالک ربات...» رد می‌شدند
# — هرچند از طریقِ is_staff/main_kb به همین بخش راه پیدا کرده بودند.
# هماهنگ با is_owner_or_admin در tenant/__init__.py.
async def _owner(cb: CallbackQuery, bot: Bot) -> BotRow | None:
    from tenant import is_owner_or_admin  # lazy: جلوگیری از ایمپورتِ حلقوی
    row = await _row(bot)
    if not row:
        await cb.answer()
        return None
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط مالک ربات به این بخش دسترسی دارد.", show_alert=True)
        return None
    if not has_feature(row.plan, Feature.PANEL_INTEGRATION):
        await cb.answer("🔒 «VPN خودکار» مخصوص پلن‌های رشد و حرفه‌ای است.", show_alert=True)
        return None
    return row


async def _owner_msg(msg: Message, bot: Bot) -> BotRow | None:
    from tenant import is_owner_or_admin  # lazy: جلوگیری از ایمپورتِ حلقوی
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return None
    if not has_feature(row.plan, Feature.PANEL_INTEGRATION):
        return None
    return row


async def get_panel(bot_id: int, panel_key: str = "sanai") -> VpnPanel | None:
    """گرفتن یک پنل خاص بر اساس bot_id و panel_key."""
    async with Session() as s:
        return await s.scalar(
            select(VpnPanel).where(VpnPanel.bot_id == bot_id, VpnPanel.panel_key == panel_key))


async def get_all_panels(bot_id: int) -> list[VpnPanel]:
    """گرفتن همهٔ پنل‌های متصل این ربات."""
    async with Session() as s:
        result = await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id))
        return list(result.all())


async def get_any_enabled_panel(bot_id: int) -> VpnPanel | None:
    """اولین پنل فعال — برای عملیات خودکار (ساخت کانفیگ)."""
    async with Session() as s:
        return await s.scalar(
            select(VpnPanel).where(VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ لغو", callback_data="tvpn:cancel", style=ButtonStyle.DANGER)]
    ])


# === PATCH_PANEL_CUSTOM_LABEL_V1 ===
# نامِ پنل‌ها تا حالا ثابت بود («🟠 پنل مرزبان»، و برایِ نمونهٔ دوم
# «... #2»). مالکِ ربات نمی‌توانست اسمِ معنادار بگذارد (مثلاً «سرور آلمان»).
# نامِ دلخواه در Setting با کلیدِ panel_label_<panel_key> ذخیره می‌شود؛
# اگر تنظیم نشده باشد، دقیقاً همان نامِ قبلی برمی‌گردد — پس هیچ رباتی
# رفتارش عوض نمی‌شود مگر خودش بخواهد.
def panel_label_key(key: str) -> str:
    return f"panel_label_{key}"


async def panel_label(bot_id: int, key: str) -> str:
    """نامِ نمایشیِ پنل: دلخواهِ مالک، وگرنه نامِ پیش‌فرض."""
    try:
        from tenant import get_setting
        custom = (await get_setting(bot_id, panel_label_key(key), "")).strip()
        if custom:
            return custom
    except Exception:
        logger.exception("خواندنِ نامِ دلخواهِ پنل ناموفق بود؛ نامِ پیش‌فرض استفاده شد")
    return PANEL_LABELS.get(key, key)


# ─────────────── ساخت متن اطلاعات پنل‌های متصل ───────────────
async def _panels_info_text(panels: list[VpnPanel]) -> str:
    """نمایش اطلاعات همهٔ پنل‌های متصل در بالای منو."""
    if not panels:
        return ""
    lines = ["┈┅━━⊷⊰✨⊱⊶━━┅┈", "🔌 <b>پنل‌های متصل:</b>"]
    for p in panels:
        # === PATCH_PANEL_CUSTOM_LABEL_V1 ===
        label = await panel_label(p.bot_id, p.panel_key)
        status = "🟢 فعال" if p.enabled else "🔴 غیرفعال"
        lines.append(
            f"  {label}\n"
            f"  وضعیت: {status}\n"
            f"  آدرس: <code>{p.base_url}</code>\n"
            f"  ساب: <code>{p.sub_base or '—'}</code>\n"
            f"  inboundها: {p.inbound_ids or 'همهٔ فعال‌ها'}"
        )
    lines.append("┈┅━━⊷⊰✨⊱⊶━━┅┈")
    return "\n".join(lines)


# ─────────────── کیبورد اصلی VPN خودکار ───────────────
# === PATCH_NODEX_PANEL_V1 ===
async def _bot_plan(bot_id: int):
    if not bot_id:
        return None
    async with Session() as s:
        return await s.scalar(select(BotRow.plan).where(BotRow.id == bot_id))


async def menu_kb(panels: list[VpnPanel], bot_id: int = 0) -> "InlineKeyboardMarkup | ReplyKeyboardMarkup":
    from tenant import ibtn_style as _ibs, get_setting
    panel_map = {p.panel_key: p for p in panels}

    if bot_id and await get_setting(bot_id, "menu_mode", "reply") != "inline":
        from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
        rows = []
        # === PATCH_PANEL_CUSTOM_LABEL_BASE_REPLY_KB_V1 ===
        # قبلاً اینجا از «label» خامِ PANEL_LABELS (اسمِ پیش‌فرض) استفاده
        # می‌شد؛ یعنی تغییرِ اسمِ دلخواهِ پنل‌های اصلی توی حالتِ ریپلای هیچ
        # اثری نداشت (برخلافِ حالتِ inline که از panel_label() استفاده
        # می‌کند). حالا هر دو حالت یکسان از panel_label() می‌خوانند.
        # === PATCH_NODEX_PANEL_V1 === فقط پنل‌هایی که این پلن به آن‌ها دسترسی دارد
        _plan = await _bot_plan(bot_id)
        for key in _panel_labels_for_plan(_plan):
            label = await panel_label(bot_id, key)
            icon = "✅" if panel_map.get(key) else "➕"
            suffix = " (متصل)" if panel_map.get(key) else ""
            # === PATCH_VPN_PANEL_COLOR_V1 ===
            btn_style = ButtonStyle.SUCCESS if panel_map.get(key) else ButtonStyle.DANGER
            rows.append([KeyboardButton(text=f"{icon} اتصال به {label}{suffix}", style=btn_style)])
        # === PATCH_MULTI_PANEL_INSTANCES_V1 ===
        for p in panels:
            if "#" in (p.panel_key or ""):
                rows.append([KeyboardButton(
                    text=f"✅ اتصال به {await panel_label(bot_id, p.panel_key)} (متصل)",
                    style=ButtonStyle.SUCCESS)])  # === PATCH_VPN_PANEL_COLOR_V1 ===
        # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
        # قبلاً این دکمه فقط تو حالتِ inline بود؛ چون پیش‌فرضِ menu_mode
        # «reply»ه، اکثرِ ربات‌ها اصلاً راهی برایِ بازکردنِ «حساب‌های همکار»
        # نداشتن (نه باگِ نمایشی، دکمه واقعاً وجود نداشت).
        rows.append([KeyboardButton(text="🔍 جستجوی کانفیگ با کد سرویس", style=ButtonStyle.PRIMARY)])  # === PATCH_VPN_PANEL_COLOR_V1 ===
        rows.append([KeyboardButton(text="🤝 حساب‌های همکار (تخفیف)", style=ButtonStyle.PRIMARY)])  # === PATCH_VPN_PANEL_COLOR_V1 ===
        rows.append([KeyboardButton(text="🧹 سرویس‌های منقضی‌شده (حذف دستی)", style=ButtonStyle.PRIMARY)])  # PATCH_SVC_CLEANUP_V1
        rows.append([KeyboardButton(text="⬅️ بازگشت به سرویس‌ها و محصولات")])
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    rows = []
    # === PATCH_NODEX_PANEL_V1 === فقط پنل‌هایی که این پلن به آن‌ها دسترسی دارد
    _plan = await _bot_plan(bot_id)
    for key, label in _panel_labels_for_plan(_plan).items():
        p = panel_map.get(key)
        if p:
            icon = "✅"
            suffix = " (متصل)"
            label = await panel_label(bot_id, key)   # PATCH_PANEL_CUSTOM_LABEL_V1
            # === PATCH_VPN_PANEL_COLOR_V1 ===
            # پنلِ متصل‌شده رنگِ سبز بگیره، جدا از استایلِ قابلِ‌تنظیمِ
            # «ibtn:vpn_setup» — تا وضعیتِ اتصال بصری هم مشخص باشه.
            btn_style = ButtonStyle.SUCCESS
        else:
            icon = "➕"
            suffix = ""
            btn_style = await _ibs(bot_id, "ibtn:vpn_setup")
        rows.append([InlineKeyboardButton(
            text=f"{icon} اتصال به {label}{suffix}",
            callback_data=f"tvpn:panel:{key}",
            style=btn_style)])

    # === PATCH_MULTI_PANEL_INSTANCES_V1 ===
    # نمونه‌های اضافیِ هر نوع پنل (pasargad#2 و ...) ردیفِ مستقلِ خودشان را
    # می‌گیرند؛ حلقهٔ بالا فقط نمونهٔ اولِ هر نوع را نشان می‌دهد.
    for p in panels:
        if "#" not in (p.panel_key or ""):
            continue
        rows.append([InlineKeyboardButton(
            text=f"✅ اتصال به {await panel_label(bot_id, p.panel_key)} (متصل)",
            callback_data=f"tvpn:panel:{p.panel_key}",
            style=ButtonStyle.SUCCESS)])  # === PATCH_VPN_PANEL_COLOR_V1 ===

    rows.append([InlineKeyboardButton(
        text="🔍 جستجوی کانفیگ با کد سرویس",
        callback_data="tvpn:codesearch",
        style=ButtonStyle.PRIMARY)])  # === PATCH_VPN_PANEL_COLOR_V1 ===

    # === PATCH_VPN_PARTNER_V1 ===
    # «حساب‌های همکار» مشترکِ کلِ ربات است (نه مخصوصِ یک پنلِ خاص)، برای
    # همین این‌جا در منویِ اصلیِ VPN خودکار می‌آید نه داخلِ منویِ یک پنل.
    # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
    # origin="vpn" یعنی «بازگشت» باید برگرده همینجا (VPN خودکار)، نه به
    # نمایندگیِ خودکار — قبلاً این تفکیک نبود و برایِ پلن‌هایِ حرفه‌ای
    # همیشه اشتباه به نمایندگی برمی‌گشت.
    rows.append([InlineKeyboardButton(
        text="🤝 حساب‌های همکار (تخفیف)",
        callback_data="vprt:list:vpn",
        style=ButtonStyle.PRIMARY)])  # === PATCH_VPN_PANEL_COLOR_V1 ===
    # === PATCH_SVC_CLEANUP_V1 === (تیکت #863) حذفِ دستیِ سرویس‌هایِ منقضی‌نشده‌حذف‌شده
    from tenant.bots.vpn.svc_cleanup import entry_button as _svc_cleanup_btn
    rows.append([_svc_cleanup_btn()])

    # === PATCH_QR_BACKGROUND_V1 ===
    # دکمه‌اش به «🎛 دکمه‌ها و تنظیمات» منتقل شد، چون فقط مالِ پنلِ خودکار
    # نیست و کانفیگ‌هایِ آمادهٔ انبار را هم شامل می‌شود.

    # دکمه‌های «پلن‌های فروش»، «تمدید» و «اطلاع اتمام حجم» اکنون همگی داخل
    # منوی مدیریتِ هر پنل هستند (نه سراسری) — چون هر پنل قیمت/تنظیماتِ
    # مستقلِ خودش را دارد.
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products",
                                      style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────── کیبورد مدیریت یک پنل خاص ───────────────
async def _plans_count(bot_id: int, panel_key: str) -> tuple[int, int]:
    """تعداد (کل، فعال) پلن‌های فروش این پنل."""
    async with Session() as s:
        plans = (await s.scalars(
            select(VpnPlan).where(VpnPlan.bot_id == bot_id, VpnPlan.panel_key == panel_key)
        )).all()
    total = len(plans)
    active = sum(1 for p in plans if p.active)
    return total, active


async def panel_manage_kb(panel: VpnPanel | None, key: str, bot_id: int = 0,
                          plan: str = "pro") -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    label = await panel_label(bot_id, key)   # PATCH_PANEL_CUSTOM_LABEL_V1
    rows = []
    if panel:
        toggle = ("🟢 فروش: فعال" if panel.enabled else "🔴 فروش: غیرفعال")
        rows.append([InlineKeyboardButton(text=toggle, callback_data=f"tvpn:toggle:{key}",
                                          style=await _ibs(bot_id, "ibtn:vpn_toggle"))])
        rows.append([InlineKeyboardButton(text="🔌 تست اتصال", callback_data=f"tvpn:test:{key}",
                                          style=await _ibs(bot_id, "ibtn:vpn_test"))])
    rows.append([InlineKeyboardButton(
        text=(f"✏️ ویرایش تنظیمات {label}" if panel else f"➕ ثبت تنظیمات {label}"),
        callback_data=f"tvpn:setup:{key}",
        style=await _ibs(bot_id, "ibtn:vpn_setup"))])
    if panel:
        # === PATCH_PANEL_CUSTOM_LABEL_V1 ===
        rows.append([InlineKeyboardButton(
            text="🏷 نام نمایشی پنل",
            callback_data=f"tvpn:rename:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_setup"))])
    if panel:
        total, active = await _plans_count(bot_id, key)
        if active:
            plans_text = f"📦 پلن‌های فروش ✅ ({active} فعال)"
        elif total:
            plans_text = f"📦 پلن‌های فروش ({total} غیرفعال)"
        else:
            plans_text = "📦 پلن‌های فروش"
        rows.append([InlineKeyboardButton(
            text=plans_text,
            callback_data=f"tvpn:plans:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_plans"))])
        rows.append([InlineKeyboardButton(
            text="🛒 فروش آنلاین (حجم/روز دلخواه)",
            callback_data=f"tvpn:custom:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_custom"))])
        rows.append([InlineKeyboardButton(
            text="♻️ تنظیمات تمدید / افزایش حجم",
            callback_data=f"tvpn:renew:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_renew_cfg"))])
        rows.append([InlineKeyboardButton(
            text="🔔 تنظیمات اطلاع از اتمام حجم",
            callback_data=f"tvpn:notify:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_renew_cfg"))])
        # === PATCH_PURCHASE_FOLLOWUP_V1 === پیگیریِ خرید (متصل‌نشدنِ کاربر)
        from tenant import get_setting as _gs
        _fup_on = (await _gs(bot_id, "followup_enabled", "0")) == "1"
        _fup_hours = await _gs(bot_id, "followup_hours", "2") or "2"
        rows.append([InlineKeyboardButton(
            text=("📞 پیگیری خرید: ✅ روشن" if _fup_on else "📞 پیگیری خرید: ⛔️ خاموش"),
            callback_data=f"tvpn:fup_toggle:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_renew_cfg"))])
        if _fup_on:
            rows.append([InlineKeyboardButton(
                text=f"⏱ فاصلهٔ پیگیری: {_fup_hours} ساعت",
                callback_data=f"tvpn:fup_hours:{key}",
                style=await _ibs(bot_id, "ibtn:vpn_renew_cfg"))])
        # === PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1 ===
        # فقط برایِ پنل‌هایی که واقعاً on_hold را پیاده می‌کنند نشان داده
        # می‌شود؛ رویِ بقیه دکمه‌ای که هیچ اثری ندارد فقط گیج‌کننده است.
        from tenant.bots.vpn.vpn_user import ONHOLD_PANELS as _OHP, purchase_on_hold_for as _pohf
        if _bk(key) in _OHP:
            _poh_on = await _pohf(bot_id, key)
            rows.append([InlineKeyboardButton(
                text=("⏳ شروعِ زمانِ فروشیِ این پنل: ⏸ از اولین اتصال" if _poh_on
                      else "⏳ شروعِ زمانِ فروشیِ این پنل: ▶️ از لحظهٔ خرید"),
                callback_data=f"tvpn:onhold:{key}",
                style=await _ibs(bot_id, "ibtn:vpn_renew_cfg"))])
        rows.append([InlineKeyboardButton(
            text="📝 تنظیم نام سرورها مشتری",
            callback_data=f"tvpn:srvname:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_custom"))])
        rows.append([InlineKeyboardButton(
            text="🧾 ثبت یادداشت در فاکتور محصول",
            callback_data=f"tvpn:noteinv:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_custom"))])
        rows.append([InlineKeyboardButton(
            text="📦 ثبت یادداشت در توضیحات تحویل کانفیگ",
            callback_data=f"tvpn:notedel:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_custom"))])
        rows.append([InlineKeyboardButton(
            text="🔄 بروزرسانی اینباندها و ساب‌لینک کاربران",
            callback_data=f"tvpn:refresh_subs:{key}",
            style=await _ibs(bot_id, "ibtn:vpn_test"))])
        # === PATCH_PANEL_AUTOSYNC_V1 ===
        # همان کارِ دکمهٔ بالا، ولی خودکار هر ۲ ساعت (tenant/panel_autosync.py).
        from tenant.shared.panel_clients.panel_autosync import autosync_button as _autosync_btn
        _ab = _autosync_btn(key, panel)
        if _ab:
            rows.append(_ab)
        # === PATCH_PANEL_TOOLS_V1 === (تیکت #824)
        from tenant.shared.panel_clients.panel_tools import tools_buttons as _ptl_btns
        rows.extend(_ptl_btns(key))
        # === PATCH_PANEL_RELINK_V1 ===
        # بازسازیِ لینک‌ها بعدِ تعویضِ پنل (فقط پلنِ حرفه‌ای) — برخلافِ
        # دکمهٔ بالا، به کاربرانی که لینکشان عوض شده هم خبر می‌دهد.
        from tenant.shared.panel_clients.panel_relink import relink_button as _relink_btn
        _rb = _relink_btn(key, plan)
        if _rb:
            rows.append(_rb)
        # === PATCH_MULTI_PANEL_INSTANCES_V1 ===
        # === PATCH_PLAN_FEATURE_GATING_V1 ===
        # چند پنلِ هم‌نوع (پاسارگاد ۱/۲/۳) طبقِ خواستهٔ مدیر فقط پلنِ
        # حرفه‌ای است؛ برایِ رشد/رایگان دکمهٔ قفل‌شده با توضیح نشان
        # داده می‌شود، نه حذفِ بی‌صدا.
        if has_feature(plan, Feature.MULTI_PANEL_INSTANCES):
            rows.append([InlineKeyboardButton(
                text=f"➕ ثبت پنل دیگر از نوع {PANEL_LABELS.get(_bk(key), _bk(key))}",
                callback_data=f"tvpn:newinst:{_bk(key)}",
                style=await _ibs(bot_id, "ibtn:vpn_setup"))])
        else:
            rows.append([InlineKeyboardButton(
                text="🔒 چند پنلِ هم‌نوع (ویژهٔ پلن حرفه‌ای)",
                callback_data="tvpn:newinst_locked",
                style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(text=f"🗑 حذف {label}", callback_data=f"tvpn:delete:{key}",
                                          style=await _ibs(bot_id, "ibtn:vpn_delete"))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به VPN خودکار", callback_data="tvpn:menu",
                                      style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)




# ─────────────── منوی اصلی VPN خودکار ───────────────
@vpn_admin_router.callback_query(F.data == "tvpn:menu")
async def vpn_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    panels = await get_all_panels(row.id)
    panels_info = await _panels_info_text(panels)

    if panels:
        intro = (
            "🛰 <b>VPN خودکار (چند پنل)</b>\n\n"
            f"{panels_info}\n\n"
            "برای مدیریت هر پنل روی دکمه‌اش بزن:"
        )
    else:
        intro = (
            "🛰 <b>VPN خودکار (چند پنل)</b>\n\n"
            "هنوز هیچ پنلی متصل نشده.\n"
            "از دکمه‌های زیر یک پنل انتخاب و اطلاعاتش را وارد کن:"
        )
    try:
        await cb.message.edit_text(intro, reply_markup=await menu_kb(panels, row.id))
    except Exception:
        await cb.message.answer(intro, reply_markup=await menu_kb(panels, row.id))
    await cb.answer()


@vpn_admin_router.callback_query(F.data == "tvpn:cancel")
async def vpn_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await vpn_menu(cb, bot)


# ─────────────── منوی یک پنل خاص ───────────────
async def _vpn_panel_menu_content(row, key: str) -> tuple[str, InlineKeyboardMarkup]:
    label = await panel_label(row.id, key)   # PATCH_PANEL_CUSTOM_LABEL_V1
    panel = await get_panel(row.id, key)
    if panel:
        status = "🟢 فعال" if panel.enabled else "🔴 غیرفعال"
        info = (
            f"🛰 <b>{label}</b>\n\n"
            f"وضعیت فروش: {status}\n"
            f"آدرس پنل: <code>{panel.base_url}</code>\n"
            f"آدرس ساب: <code>{panel.sub_base or '—'}</code>\n"
            f"inboundها: {panel.inbound_ids or 'همهٔ فعال‌ها'}\n\n"
            "از دکمه‌ها مدیریت کن:"
        )
    else:
        info = (
            f"🛰 <b>{label}</b>\n\n"
            f"هنوز به این پنل متصل نشده‌ای.\n"
            f"با دکمهٔ «➕ ثبت تنظیمات» اطلاعات اتصال را وارد کن."
        )
    return info, await panel_manage_kb(panel, key, row.id, plan=row.plan)


# === PATCH_PANEL_CUSTOM_LABEL_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:rename:"))
async def vpn_panel_rename_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """گرفتنِ نامِ نمایشیِ دلخواه برایِ این پنل."""
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    cur = await panel_label(row.id, key)
    await state.set_state(VpnPanelLabelFSM.label)
    await state.update_data(panel_label_key=key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ بازگشت به نام پیش‌فرض",
                              callback_data=f"tvpn:rename_reset:{key}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"tvpn:panel:{key}",
                              style=ButtonStyle.DANGER)],
    ])
    await cb.answer()
    await cb.message.answer(
        "🏷 <b>نام نمایشی پنل</b>\n\n"
        f"نام فعلی: <b>{_html_escape_mod.escape(cur)}</b>\n"
        f"نام پیش‌فرض: <b>{_html_escape_mod.escape(PANEL_LABELS.get(key, key))}</b>\n\n"
        "نام تازه را بفرست — مثلاً <code>🇩🇪 سرور آلمان</code>.\n\n"
        "این نام همه‌جا دیده می‌شود: فهرست پنل‌ها، پنل تحت وب نماینده و "
        "گزارش‌ها. روی لینک‌ها و کانفیگ‌های موجود هیچ اثری ندارد.",
        reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:rename_reset:"))
async def vpn_panel_rename_reset(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    from tenant import set_setting
    await state.clear()
    await set_setting(row.id, panel_label_key(key), "")
    await cb.answer("به نام پیش‌فرض بازگشت")
    info, kb = await _vpn_panel_menu_content(row, key)
    await cb.message.answer(info, reply_markup=kb)


@vpn_admin_router.message(VpnPanelLabelFSM.label, F.text)
async def vpn_panel_rename_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("panel_label_key") or ""
    name = (msg.text or "").strip()
    if not name or len(name) > 40:
        await msg.answer("نام باید بین ۱ تا ۴۰ کاراکتر باشد. دوباره بفرست:")
        return
    await state.clear()
    from tenant import set_setting
    await set_setting(row.id, panel_label_key(key), name)
    await msg.answer(f"✅ نام این پنل به <b>{_html_escape_mod.escape(name)}</b> تغییر کرد.")
    info, kb = await _vpn_panel_menu_content(row, key)
    await msg.answer(info, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:panel:"))
async def vpn_panel_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    # === PATCH_NODEX_PANEL_V1 ===
    _gate = _PANEL_EXTRA_FEATURE_GATE.get(panel_base_key(key))
    if _gate and not has_feature(row.plan, _gate):
        await cb.answer("🔒 این پنل مخصوصِ پلنِ حرفه‌ای است.", show_alert=True)
        return
    info, kb = await _vpn_panel_menu_content(row, key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)
    await cb.answer()


# === PATCH_PURCHASE_FOLLOWUP_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:fup_toggle:"))
async def vpn_followup_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش کردنِ «پیگیری خرید» (پیام به کاربری که بعد از N ساعت وصل نشده)."""
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    from tenant import get_setting, set_setting
    cur = (await get_setting(row.id, "followup_enabled", "0")) == "1"
    await set_setting(row.id, "followup_enabled", "0" if cur else "1")
    # === PATCH_TON_SELL_V1 / FIX: cb.answer محدود به ~۲۰۰ کاراکتر است
    # (MESSAGE_TOO_LONG). پس alert را کوتاه نگه می‌داریم و توضیحِ کامل را
    # به‌صورتِ پیامِ معمولی (بدونِ محدودیتِ طول) می‌فرستیم.
    if not cur:
        await cb.answer("پیگیری خرید روشن شد ✅", show_alert=True)
        try:
            await cb.message.answer(
                "📞 <b>پیگیری خرید روشن شد.</b>\n\n"
                "از این پس اگر کاربری بعد از چند ساعت هنوز به سرویسش وصل نشده باشد، "
                "دو دکمهٔ «متصل نشدم / فرصت نداشتم» برایش می‌آید؛ و اگر «متصل نشدم» را "
                "بزند، به تو هم اطلاع داده می‌شود.\n\n"
                "⚠️ مطمئن شو آدرسِ پشتیبانی را در تنظیماتِ ربات ثبت کرده‌ای، وگرنه "
                "دکمهٔ چت پشتیبانی به کاربر نمایش داده نمی‌شود.",
                parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        await cb.answer("پیگیری خرید خاموش شد ⛔️", show_alert=True)
    info, kb = await _vpn_panel_menu_content(row, key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:fup_hours:"))
async def vpn_followup_hours(cb: CallbackQuery, bot: Bot):
    """چرخهٔ فاصلهٔ پیگیری بین مقادیرِ رایج: ۲ → ۶ → ۱۲ → ۲۴ → ۲."""
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    from tenant import get_setting, set_setting
    order = ["2", "6", "12", "24"]
    cur = await get_setting(row.id, "followup_hours", "2") or "2"
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "2"
    await set_setting(row.id, "followup_hours", nxt)
    await cb.answer(f"فاصلهٔ پیگیری: {nxt} ساعت")
    info, kb = await _vpn_panel_menu_content(row, key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)


# === PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:onhold:"))
async def vpn_panel_onhold_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش کردنِ on_hold فقط برایِ همین یک پنل.

    مقدارِ «مؤثرِ» فعلی (که ممکن است از تنظیمِ سراسری ارث رسیده باشد) را
    می‌خوانیم و برعکسش را صریحاً رویِ همین پنل ذخیره می‌کنیم؛ از این به بعد
    این پنل دیگر از تنظیمِ سراسری پیروی نمی‌کند، ولی بقیهٔ پنل‌ها دست‌نخورده
    می‌مانند."""
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    from tenant.bots.vpn.vpn_user import ONHOLD_PANELS, purchase_on_hold_for, purchase_on_hold_key
    if _bk(key) not in ONHOLD_PANELS:
        await cb.answer("این نوع پنل از «شروع از اولین اتصال» پشتیبانی نمی‌کند.", show_alert=True)
        return
    from tenant import set_setting
    cur = await purchase_on_hold_for(row.id, key)
    await set_setting(row.id, purchase_on_hold_key(key), "0" if cur else "1")
    if cur:
        await cb.answer("▶️ این پنل: زمان از لحظهٔ خرید شروع می‌شود.", show_alert=True)
    else:
        await cb.answer("⏸ این پنل: زمان از اولین اتصالِ مشتری شروع می‌شود.\n"
                        "روی تمدید اثری ندارد.", show_alert=True)
    info, kb = await _vpn_panel_menu_content(row, key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)


# === PATCH_MULTI_PANEL_INSTANCES_V1 ===
# === PATCH_PLAN_FEATURE_GATING_V1 ===
@vpn_admin_router.callback_query(F.data == "tvpn:newinst_locked")
async def vpn_new_instance_locked(cb: CallbackQuery):
    await cb.answer(upgrade_hint(Feature.MULTI_PANEL_INSTANCES), show_alert=True)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:newinst:"))
async def vpn_new_instance(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ثبتِ یک پنلِ دیگر از همان نوع (مثلاً پاسارگادِ دوم)."""
    row = await _owner(cb, bot)
    if not row:
        return
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # بازبینیِ لحظه‌ای، مستقل از پنهان/قفل‌بودنِ دکمه — تا کسی با
    # پیامِ قدیمی/کش‌شده دور نزند.
    if not has_feature(row.plan, Feature.MULTI_PANEL_INSTANCES):
        await cb.answer(upgrade_hint(Feature.MULTI_PANEL_INSTANCES), show_alert=True)
        return
    base = _bk(cb.data.split(":", 2)[2])
    if base not in PANEL_LABELS:
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    new_key = await next_panel_instance_key(row.id, base)
    info, kb = await _vpn_panel_menu_content(row, new_key)
    try:
        await cb.message.edit_text(info, reply_markup=kb)
    except Exception:
        await cb.message.answer(info, reply_markup=kb)
    await cb.answer("با «➕ ثبت تنظیمات» اطلاعاتِ این پنل را وارد کن")


# ─────────────── ثبت/ویرایش تنظیمات هر پنل ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:setup:"))
async def vpn_setup(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    label = PANEL_LABELS.get(key, key)
    await state.update_data(panel_key=key)
    await state.set_state(VpnPanelFSM.base_url)

    if _bk(key) in ("pasargad", "pasarguard"):
        step1_body = (
            "۱/۵ — آدرس کامل پنل را با https و همراه با پورت بفرست. "
            "اگر آدرس داشبورد را داری لازم نیست انتهای /dashboard را حذف کنی، "
            "خودش به‌صورت خودکار نادیده گرفته می‌شود. مثل:\n"
            "<code>https://panel.example.com:8000</code>"
        )
    elif _bk(key) in ("marzban", "rebecca"):
        step1_body = (
            "۱/۵ — آدرس کامل پنل را با https و همراه با پورت بفرست "
            "(همان آدرسی که با آن وارد پنل می‌شوی، بدون مسیر /dashboard "
            "یا /sub در انتها)، مثل:\n"
            "<code>https://panel.example.com:8000</code>"
        )
    elif _bk(key) == "marzneshin":
        step1_body = (
            "۱/۵ — آدرس کامل پنل مرزنشین را با https و همراه با پورت بفرست "
            "(همان آدرسی که با آن وارد پنل می‌شوی، بدون مسیر /dashboard "
            "در انتها)، مثل:\n"
            "<code>https://panel.example.com:8000</code>"
        )
    elif _bk(key) == "hiddify":
        step1_body = (
            "۱/۵ — آدرس پایهٔ پنل هیدیفای را بفرست، <b>بدون</b> بخشِ مخفیِ "
            "وسطِ آدرس (اون رو مرحلهٔ بعد جدا می‌پرسم). یعنی اگه آدرسِ کاملِ "
            "پنلت اینه:\n"
            "<code>https://panel.example.com/AbCd1234/admin/</code>\n"
            "فقط این بخش رو بفرست:\n"
            "<code>https://panel.example.com</code>"
        )
    elif _bk(key) == "upanel_mizban":
        step1_body = (
            "۱/۵ — آدرس کامل پنل یوپنل (مرزبان) را با https و همراه با پورت بفرست "
            "(همان آدرسی که با آن وارد پنل می‌شوی، بدون مسیر /dashboard یا /developer "
            "در انتها)، مثل:\n"
            "<code>https://panel.example.com:2053</code>"
        )
    elif _bk(key) == "eylan":      # === PATCH_EYLAN_PANEL_V1 ===
        step1_body = (
            "۱/۵ — آدرسِ پنلِ ایلان را با https و پورت (همان دامنه/آی‌پی که API رویش است، "
            "بدونِ /api در انتها) بفرست، مثل:\n"
            "<code>https://panel.example.com:2053</code>\n\n"
            "⚠️ این پنل با «کلیدِ API» کار می‌کند نه یوزرنیم/رمز؛ در مرحلهٔ بعد توضیح می‌دهم."
        )
    elif _bk(key) == "shahan":
        step1_body = (
            "۱/۵ — آدرس پایهٔ پنل شاهان را با https و همراه با پورت (بدون مسیرِ "
            "/apiV1/api.php در انتها — خودِ ربات اضافه‌اش می‌کند) بفرست، مثل:\n"
            "<code>https://panel.example.com</code>\n\n"
            "⚠️ این پنل با «توکن» کار می‌کند نه یوزرنیم/رمز؛ در مرحلهٔ بعد "
            "توضیح می‌دهم."
        )
    else:  # sanai / alireza → پنل 3X-UI
        step1_body = (
            "۱/۵ — آدرس کامل پنل را با http یا https و همراه با پورت بفرست "
            "(همان آدرسی که با آن وارد پنل می‌شوی، با مسیر اضافه‌ای که در انتهای آدرس دارد)، مثل:\n"
            "<code>https://panel.example.com:54321/dhdhdhds</code>"
        )

    await cb.message.answer(
        f"⚙️ ثبت تنظیمات <b>{label}</b>\n\n{step1_body}",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnPanelFSM.base_url, F.text)
async def vpn_setup_base(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("آدرس باید با http یا https شروع شود. دوباره بفرست:", reply_markup=cancel_kb())
        return
    await state.update_data(base_url=url.rstrip("/"))
    await state.set_state(VpnPanelFSM.username)
    data = await state.get_data()
    if _bk(data.get("panel_key") or "") == "eylan":      # === PATCH_EYLAN_PANEL_V1 === (پنلِ دوم کلیدِ «eylan#2» دارد)
        await msg.answer(
            "۲/۵ — پنلِ ایلان یوزرنیم/رمز ندارد و فقط با «کلیدِ API» وصل می‌شود؛ "
            "این مرحله استفاده نمی‌شود. فقط «-» بفرست:",
            reply_markup=cancel_kb())
        return
    if data.get("panel_key") == "shahan":
        await msg.answer(
            "۲/۵ — پنل شاهان یوزرنیم/رمزِ جدا ندارد (فقط توکن). این مرحله "
            "استفاده نمی‌شود؛ فقط «-» بفرست تا رد شود:",
            reply_markup=cancel_kb())
        return
    if data.get("panel_key") == "hiddify":
        await msg.answer(
            "۲/۵ — بخشِ مخفیِ وسطِ آدرسِ پنل (proxy_path) را بفرست. توی پنلِ "
            "هیدیفای، بالای صفحه یا توی آدرس‌بار مرورگر وقتی لاگین کردی می‌بینیش؛ "
            "مثلاً اگه آدرس اینه:\n"
            "<code>https://panel.example.com/AbCd1234/admin/</code>\n"
            "همون <code>AbCd1234</code> رو بفرست:",
            reply_markup=cancel_kb())
        return
    if data.get("panel_key") in ("pasargad", "pasarguard"):
        # === PATCH_PANEL_API_KEY_AUTH_V1 ===
        # این دو پنل از نسخهٔ ۵.۱ به بعد، علاوه بر یوزرنیم/پسورد، اتصال با
        # «API Key» ثابت رو هم پشتیبانی می‌کنن — بذار ادمین انتخاب کنه.
        await msg.answer(
            "۲/۵ — چطور می‌خوای وارد پنل بشی؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 یوزرنیم/پسورد", callback_data="tvpn:authmode:userpass", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton(text="🔑 API Key", callback_data="tvpn:authmode:apikey", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton(text="❌ لغو", callback_data="tvpn:menu", style=ButtonStyle.DANGER)],
            ]))
        return
    await msg.answer("۲/۵ — نام کاربری ورود به پنل را بفرست:", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPanelFSM.username, F.text)
async def vpn_setup_user(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    await state.update_data(username=msg.text.strip())
    await state.set_state(VpnPanelFSM.password)
    data = await state.get_data()
    if _bk(data.get("panel_key") or "") == "eylan":      # === PATCH_EYLAN_PANEL_V1 === (پنلِ دوم کلیدِ «eylan#2» دارد)
        await msg.answer(
            "۳/۵ — <b>کلیدِ API</b> پنلِ ایلان را بفرست (X-API-KEY؛ از پنل، بخشِ تنظیماتِ API "
            "ادمینِ اصلی یا نماینده). کلیدِ نماینده فقط کاربرانِ همان نماینده را می‌بیند:",
            reply_markup=cancel_kb(), parse_mode="HTML")
        return
    if data.get("panel_key") == "shahan":
        await msg.answer(
            "۳/۵ — توکنِ API پنل شاهان را بفرست (از پنلِ ادمینِ شاهان می‌گیری):",
            reply_markup=cancel_kb())
        return
    if data.get("panel_key") == "hiddify":
        await msg.answer(
            "۳/۵ — «Hiddify-API-Key» را بفرست — همون UUID ادمینه؛ توی پنل برو "
            "بخشِ <b>Admin → Profile</b> (یا تنظیماتِ ادمین)، کنارِ اسمِ ادمین یه "
            "رشته‌ی طولانیِ شبیهِ <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code> "
            "هست؛ همونو کپی کن:",
            reply_markup=cancel_kb())
        return
    await msg.answer("۳/۵ — رمز عبور پنل را بفرست:", reply_markup=cancel_kb())


# === PATCH_SKIP_SUB_BASE_STEP_V1 ===
# گزارشِ ادمین: «این مرحله‌ای که ساب رو می‌پرسه رو نشون نده؛ بعضی‌ها
# نمی‌دونن و یه چیزی می‌زنن و باعثِ خرابیِ کانفیگ‌ها می‌شه».
#
# بررسی شد و حق با اوست، ولی نه برایِ *همهٔ* پنل‌ها:
#   • خانوادهٔ مرزبان (مرزبان، ربکا، پاسارگاد، PasarGuard، یوپنل،
#     مرزنشین) و شاهان → لینکِ ساب را خودِ پنل می‌سازد و این مقدار
#     اصلاً استفاده نمی‌شود. متنِ خودِ مرحله هم می‌گفت «اختیاری است».
#     برایِ این‌ها مرحله کاملاً حذف شد.
#   • سنایی و علیرضا → بدونِ این مقدار `sub_link` خالی می‌ماند
#     (vpn_panel.sub_link فقط وقتی sub_base هست لینک می‌سازد). حذفش
#     یعنی هیچ کاربری ساب‌لینک نگیرد — همان خرابی، از سمتِ دیگر.
#     پس مرحله می‌ماند ولی ورودی اعتبارسنجی می‌شود تا متنِ بی‌ربط ثبت نشود.
#   • هیدیفای → این فیلد proxy_path است، معنیِ واقعی دارد؛ می‌ماند.
_SUB_BASE_AUTO = frozenset({
    "marzban", "rebecca", "pasargad", "pasarguard",
    "upanel_mizban", "marzneshin", "shahan", "eylan",
})


def _skips_sub_base(key: str) -> bool:
    """آیا این پنل ساب را خودش می‌سازد و نیازی به پرسیدن نیست؟"""
    return _bk(key) in _SUB_BASE_AUTO


async def _vpn_setup_step4_text(key: str) -> str:
    """PATCH_PANEL_API_KEY_AUTH_V1 — متنِ مرحلهٔ ۴/۵ (ساب‌بیس)، جدا شده تا
    هم مسیرِ یوزرنیم/پسورد هم مسیرِ API Key بتونن ازش استفاده کنن."""
    if _bk(key) in ("marzban", "rebecca", "pasargad", "pasarguard", "upanel_mizban", "marzneshin"):
        _pword = {"marzban": "مرزبان", "rebecca": "ربکا", "pasargad": "پاسارگاد", "pasarguard": "PasarGuard",
                  "upanel_mizban": "یوپنل (مرزبان)", "marzneshin": "مرزنشین"}
        panel_word = _pword.get(key, key)
        return (
            "۴/۵ — آدرس پایهٔ ساب (Subscription) را بفرست. "
            f"در پنل {panel_word} لینک ساب را خودِ پنل می‌سازد، پس این مرحله "
            "اختیاری است؛ اگر نمی‌دانی چی بفرستی همان «-» را بفرست:"
        )
    if _bk(key) == "shahan":
        return (
            "۴/۵ — پنل شاهان ساب‌لینکِ یکپارچه ندارد (لینک‌ها مستقیم از خودِ "
            "پنل گرفته می‌شوند)؛ این مرحله استفاده نمی‌شود. فقط «-» بفرست:"
        )
    if _bk(key) == "hiddify":
        return (
            "۴/۵ — اگه بخشِ مخفیِ آدرسِ کاربرها (proxy_path_client) با همون "
            "بخشِ مخفیِ ادمین که مرحلهٔ ۲ فرستادی فرق داره، اینجا بفرستش؛ "
            "بیشترِ نصب‌های هیدیفای این دوتا رو یکی می‌ذارن، پس اگه مطمئن "
            "نیستی همون «-» رو بفرست تا از همون مقدارِ مرحلهٔ ۲ استفاده بشه:"
        )
    return (  # sanai / alireza
        "۴/۵ — آدرس پایهٔ ساب (Subscription) پنل را بفرست؛ همان آدرسی که "
        "لینک‌های اشتراک کاربران از آن ساخته می‌شود (می‌تواند با آدرس "
        "مرحلهٔ ۱ فرق داشته باشد، مثلاً دامنه یا پورت متفاوت). اگر نداری «-» بفرست:\n"
        "<code>https://sub.example.com:2096</code>"
    )


# === PATCH_SKIP_SUB_BASE_STEP_V1 ===
async def _goto_inbound_step(msg: Message, state: FSMContext, key: str) -> None:
    """مرحلهٔ ۵/۵ (اینباند/گروه) را نشان می‌دهد — بدونِ تکرارِ کد."""
    await state.set_state(VpnPanelFSM.inbound_ids)
    await _vpn_setup_step5_prompt(msg, key)


@vpn_admin_router.message(VpnPanelFSM.password, F.text)
async def vpn_setup_pass(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    await state.update_data(password=msg.text.strip())
    data = await state.get_data()
    key = data.get("panel_key", "sanai")
    # === PATCH_SKIP_SUB_BASE_STEP_V1 ===
    # پنل‌هایی که ساب را خودشان می‌سازند: مرحله پرسیده نمی‌شود و مقدار
    # خالی ذخیره می‌گردد — همان چیزی که قبلاً هم با فرستادنِ «-» اتفاق
    # می‌افتاد، منتها حالا امکانِ واردکردنِ مقدارِ اشتباه وجود ندارد.
    if _skips_sub_base(key):
        await state.update_data(sub_base="")
        await _goto_inbound_step(msg, state, key)
        return
    await state.set_state(VpnPanelFSM.sub_base)
    await msg.answer(await _vpn_setup_step4_text(key), reply_markup=cancel_kb())


# === PATCH_PANEL_API_KEY_AUTH_V1 ===
@vpn_admin_router.callback_query(VpnPanelFSM.username, F.data.startswith("tvpn:authmode:"))
async def vpn_setup_authmode_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """انتخابِ روشِ ورود برایِ پنل‌هایی که هم یوزرنیم/پسورد هم API Key
    پشتیبانی می‌کنن (پاسارگارد/PasarGuard از نسخهٔ ۵.۱ به بعد)."""
    row = await _owner(cb, bot)
    if not row:
        return
    mode = cb.data.split(":")[2]
    if mode == "apikey":
        await state.set_state(VpnPanelFSM.api_key)
        try:
            await cb.message.edit_text(
                "۲/۴ — API Key پنل را بفرست (از پنل، بخشِ «API Keys» ساخته می‌شه):",
                reply_markup=None)
        except Exception:
            await cb.message.answer("۲/۴ — API Key پنل را بفرست:")
        await cb.answer()
        return
    # userpass
    await state.set_state(VpnPanelFSM.username)
    try:
        await cb.message.edit_text("۲/۵ — نام کاربری ورود به پنل را بفرست:", reply_markup=None)
    except Exception:
        await cb.message.answer("۲/۵ — نام کاربری ورود به پنل را بفرست:")
    await cb.answer()


@vpn_admin_router.message(VpnPanelFSM.api_key, F.text)
async def vpn_setup_apikey(msg: Message, bot: Bot, state: FSMContext):
    """PATCH_PANEL_API_KEY_AUTH_V1 — دریافتِ API Key؛ یوزرنیم/پسورد خالی
    ذخیره می‌شن (چون این پنل با کلید وارد می‌شه، نه این دوتا)."""
    if not await _owner_msg(msg, bot):
        return
    await state.update_data(api_key=msg.text.strip(), username="", password="")
    await state.set_state(VpnPanelFSM.sub_base)
    data = await state.get_data()
    key = data.get("panel_key", "sanai")
    await msg.answer(await _vpn_setup_step4_text(key), reply_markup=cancel_kb())


# === PATCH_SKIP_SUB_BASE_STEP_V1 ===
async def _vpn_setup_step5_prompt(msg: Message, key: str) -> None:
    """متنِ مرحلهٔ ۵/۵ — از داخلِ vpn_setup_sub بیرون کشیده شد تا مسیرِ
    پنل‌هایی که مرحلهٔ ساب را رد می‌کنند هم بتواند از آن استفاده کند."""
    if _bk(key) in ("marzban", "rebecca", "upanel_mizban"):
        _pname = {"marzban": "مرزبان", "rebecca": "ربکا", "upanel_mizban": "یوپنل (مرزبان)"}.get(key, key)
        await msg.answer(
            f"۵/۵ — نام (tag) اینباندهای {_pname} را با کاما بفرست "
            f"(مثل <code>VLESS TCP REALITY,VMess TCP</code>؛ دقیقاً همان نامی که "
            f"در پنل زیر «Inbounds» نوشته شده، با حروف بزرگ/کوچک یکسان). "
            "اگر می‌خواهی روی همهٔ inboundها ساخته شود «همه» بفرست:",
            reply_markup=cancel_kb())
    elif _bk(key) in ("pasargad", "pasarguard"):
        _gword = "پاسارگاد" if key == "pasargad" else "PasarGuard"
        await msg.answer(
            f"۵/۵ — شناسهٔ <b>گروه‌ها</b>ی {_gword} را با کاما بفرست (عدد، مثل "
            "<code>1,2</code>؛ نه نام inbound — همان آی‌دی گروهی که در پنل "
            f"{_gword} به کاربر تخصیص می‌دهی). "
            "اگر می‌خواهی روی همهٔ گروه‌های موجود ساخته شود «همه» بفرست:",
            reply_markup=cancel_kb())
    elif _bk(key) == "marzneshin":
        await msg.answer(
            "۵/۵ — شناسهٔ <b>Service</b>های مرزنشین را با کاما بفرست (عدد، مثل "
            "<code>1,2</code> — همان آی‌دی سرویسی که در پنل مرزنشین، بخش "
            "«Services»، تعریف کرده‌ای؛ کاربر به این سرویس‌ها وصل می‌شود نه "
            "مستقیم به inbound). اگر می‌خواهی روی همهٔ سرویس‌های موجود ساخته "
            "شود «همه» بفرست:",
            reply_markup=cancel_kb())
    elif _bk(key) == "eylan":      # === PATCH_EYLAN_PANEL_V1 ===
        await msg.answer(
            "۵/۵ — پروتکل‌هایی که کاربرِ خریدار بگیرد را با کاما بفرست:\n"
            "• <code>openvpn</code>، <code>l2tp</code>، <code>cisco</code>، <code>wg1</code> (وایرگاردِ پیش‌فرض)\n"
            "• نامِ وایرگاردهایِ اضافه (مثلِ <code>wg2</code>)\n"
            "• تگِ اینباندهایِ سینگ‌باکس (مثلِ <code>reality-main</code>)\n"
            "• <code>node:1</code> برایِ محدودکردن به یک نود\n"
            "مثال: <code>openvpn,wg1,reality-main</code>\n\n"
            "برایِ پیش‌فرض (OpenVPN + همهٔ وایرگارد و سینگ‌باکس‌هایی که کلید اجازه دارد) «همه» بفرست:",
            reply_markup=cancel_kb(), parse_mode="HTML")
    elif _bk(key) == "shahan":
        await msg.answer(
            "۵/۵ — پنل شاهان مفهومِ inbound/گروه ندارد؛ این مرحله استفاده "
            "نمی‌شود. فقط «-» بفرست تا تنظیمات ذخیره شود:",
            reply_markup=cancel_kb())
    elif _bk(key) == "hiddify":
        await msg.answer(
            "۵/۵ — پنل هیدیفای مفهومِ inbound/سرویس برایِ انتخاب ندارد "
            "(کاملاً خودکاره)؛ این مرحله استفاده نمی‌شود. فقط «-» بفرست تا "
            "تنظیمات ذخیره شود:",
            reply_markup=cancel_kb())
    else:  # sanai / alireza
        await msg.answer(
            "۵/۵ — شناسهٔ inboundها را با کاما بفرست (عدد، مثل <code>1,2</code>؛ "
            "همان آی‌دی که در لیست inboundهای پنل کنار هرکدام نوشته شده است). "
            "اگر می‌خواهی روی همهٔ inboundهای فعال ساخته شود «همه» بفرست:",
            reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPanelFSM.sub_base, F.text)
async def vpn_setup_sub(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    sub = msg.text.strip()
    sub = "" if sub in ("-", "—", "خالی", "ندارم", "نمیدونم", "نمی‌دانم") else sub.rstrip("/")
    data = await state.get_data()
    key = data.get("panel_key", "sanai")
    # === PATCH_SKIP_SUB_BASE_STEP_V1 ===
    # این مرحله فقط برایِ پنل‌هایی می‌ماند که واقعاً به ساب‌بیس نیاز دارند
    # (سنایی/علیرضا) یا معنیِ دیگری دارد (هیدیفای: proxy_path). شکایتِ
    # ادمین این بود که «یه چیزی می‌زنن و کانفیگ خراب می‌شه» — پس به‌جایِ
    # قبولِ هر متنی، ورودی اعتبارسنجی می‌شود و خطایِ راهنما می‌گیرد.
    if sub and _bk(key) != "hiddify":
        _ok = sub.startswith(("http://", "https://")) and "." in sub and " " not in sub
        if not _ok:
            await msg.answer(
                "❌ این آدرسِ معتبری نیست.\n\n"
                "باید با <code>http://</code> یا <code>https://</code> شروع شود، "
                "مثل:\n<code>https://sub.example.com:2096</code>\n\n"
                "اگر آدرسِ سابِ جدایی نداری، فقط «<b>-</b>» بفرست.",
                reply_markup=cancel_kb(), parse_mode="HTML")
            return
    await state.update_data(sub_base=sub)
    await state.set_state(VpnPanelFSM.inbound_ids)
    await _vpn_setup_step5_prompt(msg, key)


@vpn_admin_router.message(VpnPanelFSM.inbound_ids, F.text)
async def vpn_setup_inbounds(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    data = await state.get_data()
    key = data.get("panel_key", "sanai")
    fetch_all = raw in ("همه", "all", "*")
    inbound = ""

    # === PATCH_PANEL_SETUP_STATE_GUARD_V1 ===
    # اگه استیتِ مراحلِ قبلی (آدرس/یوزرنیم/رمز) به هر دلیلی خالی/ناقص شده
    # باشه (ری‌استارتِ بات، تایم‌اوتِ طولانی، /start وسطِ کار)، ادامهٔ این
    # مرحله (که چندجا مستقیم data["base_url"]/... می‌خونه) با KeyError کرش
    # می‌کرد و ERR-XXXXXX نشون می‌داد؛ الان به‌جاش پیامِ روشن می‌ده.
    if not data.get("base_url") or data.get("username") is None or data.get("password") is None:
        await state.clear()
        await msg.answer(
            "❌ اطلاعاتِ این مرحله ناقص/منقضی شده (شاید ربات ری‌استارت شده یا خیلی طول کشیده).\n"
            "لطفاً از «➕ ثبت تنظیمات» دوباره از مرحلهٔ اول شروع کن.")
        return

    # شاهان/هیدیفای اصلاً مفهومِ inbound/گروه ندارند — هرچه ادمین اینجا بفرستد
    # نادیده گرفته می‌شود (بدونِ این میان‌بر، فرستادنِ «همه» چون پنل همیشه
    # لیستِ خالی برمی‌گرداند، ادمین را تو یه پیامِ خطا-و-دوباره‌بفرست گیر می‌انداخت).
    if key in ("shahan", "hiddify") or (_bk(key) == "eylan" and fetch_all):
        fetch_all = False      # ایلان + «همه» = پیش‌فرضِ خودِ کلاینت (inbound خالی) — PATCH_EYLAN_PANEL_V1
        inbound = ""
    elif not fetch_all:
        if _bk(key) in ("marzban", "rebecca", "upanel_mizban", "eylan"):
            # مرزبان / ربکا / یوپنل: شناسهٔ inbound رشتهٔ «tag» است، نه عدد
            tags = [p.strip() for p in raw.replace("،", ",").split(",") if p.strip()]
            if not tags:
                await msg.answer(
                    "حداقل یک نام inbound بفرست (یا «همه»):", reply_markup=cancel_kb())
                return
            inbound = ",".join(tags)
        else:
            nums = [p.strip() for p in raw.replace("،", ",").split(",") if p.strip()]
            if any(fa_to_int(n) is None for n in nums):
                await msg.answer("فقط عدد و کاما مجاز است. دوباره بفرست (یا «همه»):", reply_markup=cancel_kb())
                return
            inbound = ",".join(str(fa_to_int(n)) for n in nums)

    if fetch_all:
        print(f"[vpn_setup_inbounds] START key={key} bot_id={getattr(row,'id','?')} url={data.get('base_url')}", flush=True)
        await msg.answer("⏳ در حال گرفتن لیست همهٔ inboundها از پنل… (ممکن است تا ۳۰ ثانیه طول بکشد)")
        print("[vpn_setup_inbounds] sent 'در حال گرفتن' message", flush=True)
        from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
        import asyncio as _asyncio
        from urllib.parse import urlparse as _urlparse

        # ۰) چکِ سریعِ اتصالِ خام (TCP) قبل از هرچیز — اگر خودِ سرور اصلاً به
        # آدرس/پورتِ پنل دسترسی ندارد (فایروال، آی‌پی اشتباه، پنل خاموش)،
        # به‌جای معطل‌ماندنِ کاربر تا ۳۰ ثانیه، در چند ثانیه خطای واضح بده.
        print("[vpn_setup_inbounds] before TCP pre-check", flush=True)
        try:
            _u = _urlparse(data["base_url"])
            _host = _u.hostname
            _port = _u.port or (443 if _u.scheme == "https" else 80)
            print(f"[vpn_setup_inbounds] TCP pre-check host={_host} port={_port}", flush=True)
            if _host:
                _fut = _asyncio.open_connection(_host, _port)
                _reader, _writer = await _asyncio.wait_for(_fut, timeout=8)
                print("[vpn_setup_inbounds] TCP pre-check connected OK", flush=True)
                # فقط close() کافیست؛ منتظرِ wait_closed() نمی‌مانیم چون این یک
                # اتصالِ TCP خامِ یک‌بارمصرف است (نه هندشیکِ TLS) و بدونِ timeout
                # ممکن است close شدنش هیچ‌وقت تمام نشود و کل مرحله ساکت گیر کند.
                _writer.close()
                print("[vpn_setup_inbounds] TCP pre-check writer closed", flush=True)
        except Exception as e:
            print(f"[vpn_setup_inbounds] TCP pre-check failed for key={key} bot={row.id} "
                  f"url={data.get('base_url')}: {e!r}", flush=True)
            await msg.answer(
                "❌ سرورِ ربات نمی‌تواند اصلاً به آدرس/پورتِ این پنل وصل شود "
                "(قبل از هر چیز، اتصالِ پایه برقرار نشد).\n\n"
                "معمولاً یعنی:\n"
                "• فایروالِ پنل فقط آی‌پیِ خاصی (مثل آی‌پیِ گوشی/کامپیوترت) را اجازه می‌دهد، نه سرورِ ربات\n"
                "• پورت اشتباه است یا پنل روشن نیست\n\n"
                "از روی خودِ سرورِ ربات (با SSH) این را تست کن تا مطمئن شوی:\n"
                f"<code>curl -m 10 -k {data['base_url']}</code>\n\n"
                "بعد از رفع مشکل، دوباره بفرست (یا «همه»):",
                reply_markup=cancel_kb())
            return

        print("[vpn_setup_inbounds] TCP pre-check block done, about to build probe client", flush=True)
        _PROBE_TIMEOUT = 25
        probe = make_panel_client_from_fields(
            key, data["base_url"], data["username"], data["password"],
            sub_base=data.get("sub_base", ""), inbound_ids_str=None,
            api_key=data.get("api_key"))  # === PATCH_PANEL_API_KEY_AUTH_V1 ===
        probe.timeout = _PROBE_TIMEOUT
        print(f"[vpn_setup_inbounds] probe client built: {type(probe).__name__}", flush=True)

        async def _fetch_inbounds():
            print("[vpn_setup_inbounds] _fetch_inbounds: calling probe.login()", flush=True)
            await probe.login()
            print("[vpn_setup_inbounds] _fetch_inbounds: login done, calling list_inbounds()", flush=True)
            r = await probe.list_inbounds()
            print(f"[vpn_setup_inbounds] _fetch_inbounds: list_inbounds done, got {len(r) if r else 0} items", flush=True)
            return r

        try:
            print("[vpn_setup_inbounds] about to await wait_for(_fetch_inbounds)", flush=True)
            inbounds = await _asyncio.wait_for(_fetch_inbounds(), timeout=_PROBE_TIMEOUT + 5)
            print(f"[vpn_setup_inbounds] wait_for returned, inbounds count={len(inbounds) if inbounds else 0}", flush=True)
            active = [ib for ib in inbounds if ib.get("enable", True)]
            if _bk(key) in ("marzban", "rebecca", "upanel_mizban"):
                all_ids = [str(ib["id"]) for ib in active if ib.get("id")]
            else:
                all_ids = [str(int(ib["id"])) for ib in active if ib.get("id") is not None]
            if not all_ids:
                # === PATCH_PASARGAD_GROUPS_CF403_V1 ===
                if _bk(key) in ("pasargad", "pasarguard"):
                    _why = _html_escape_mod.escape(
                        str(getattr(probe, "_groups_diag", "") or ""))[:300]
                    await msg.answer(
                        "⚠️ لاگین موفق بود، ولی هیچ <b>گروهی</b> روی پنل پاسارگاد "
                        "پیدا نشد.\n\n"
                        "در پاسارگاد کاربر به «گروه» وصل می‌شود، نه مستقیم به "
                        "inbound؛ پس فعال‌بودنِ inbound کافی نیست. در پنل: "
                        "<b>Groups ← Create Group</b> را بزن، inboundهای موردنظر را "
                        "داخلش انتخاب کن و ذخیره کن. بعد دوباره «همه» بفرست "
                        "(یا آی‌دیِ گروه، مثل <code>1</code>).\n\n"
                        "اگر گروه داری و باز هم این پیام آمد، ادمینِ واردشده باید "
                        "<b>sudo</b> باشد."
                        + (f"\n\nجزئیات: <code>{_why}</code>" if _why else ""),
                        reply_markup=cancel_kb())
                    return
                await msg.answer(
                    "⚠️ هیچ inbound فعالی روی پنل پیدا نشد. دوباره بفرست:",
                    reply_markup=cancel_kb())
                return
            inbound = ",".join(all_ids)
        except PanelError as e:
            print(f"[vpn_setup_inbounds] PanelError for key={key} bot={row.id}: {e}")
            # متنِ خطا ممکن است حاویِ بدنهٔ خامِ پاسخِ پنل باشد (حتی HTML)؛
            # بدونِ escape داخلِ پیامِ HTML کل هندلر را کرش می‌داد (ERR-205112).
            _safe_e = _html_escape_mod.escape(str(e))[:500]
            await msg.answer(
                f"❌ اتصال به پنل ناموفق بود:\n{_safe_e}\nدوباره بفرست:",
                reply_markup=cancel_kb())
            return
        except (_asyncio.TimeoutError, TimeoutError):
            print(f"[vpn_setup_inbounds] TIMEOUT for key={key} bot={row.id} "
                  f"url={data.get('base_url')} after {_PROBE_TIMEOUT + 5}s")
            await msg.answer(
                "❌ اتصال به پنل بیش از حد طول کشید (پنل پاسخ نداد). "
                "آدرس/پورت/فایروال پنل را چک کن و دوباره بفرست:",
                reply_markup=cancel_kb())
            return
        except Exception as e:
            print(f"[vpn_setup_inbounds] Unexpected error for key={key} bot={row.id}: {e!r}")
            _safe_e = _html_escape_mod.escape(str(e))[:500]
            await msg.answer(
                f"❌ خطای غیرمنتظره:\n{_safe_e}\nدوباره بفرست:",
                reply_markup=cancel_kb())
            return
        finally:
            try:
                await probe.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await state.clear()

    # === PATCH_PANEL_SETUP_STATE_GUARD_V1 ===
    # اگه بینِ مراحلِ ثبتِ پنل، استیت به هر دلیلی (ری‌استارتِ بات، تایم‌اوتِ
    # طولانی، زدنِ /start وسطِ کار) خالی/ناقص شده باشه، دسترسیِ مستقیم به
    # data["base_url"]/["username"]/["password"] با KeyError کرش می‌کرد و
    # همون ERR-XXXXXX رو نشون می‌داد؛ الان به‌جاش پیامِ روشن می‌ده.
    base_url = data.get("base_url")
    username = data.get("username")
    password = data.get("password")
    if not base_url or username is None or password is None:
        await msg.answer(
            "❌ اطلاعاتِ این مرحله ناقص/منقضی شده (شاید ربات ری‌استارت شده یا خیلی طول کشیده).\n"
            "لطفاً از «➕ ثبت تنظیمات» دوباره از مرحلهٔ اول شروع کن.")
        return

    async with Session() as s:
        panel = await s.scalar(
            select(VpnPanel).where(VpnPanel.bot_id == row.id, VpnPanel.panel_key == key))
        if panel:
            panel.base_url = base_url
            panel.username = username
            panel.password = password
            panel.api_key = data.get("api_key")  # === PATCH_PANEL_API_KEY_AUTH_V1 ===
            panel.sub_base = data.get("sub_base", "")
            panel.inbound_ids = inbound
        else:
            panel = VpnPanel(
                bot_id=row.id, panel_key=key, base_url=base_url,
                username=username, password=password,
                api_key=data.get("api_key"),  # === PATCH_PANEL_API_KEY_AUTH_V1 ===
                sub_base=data.get("sub_base", ""),
                inbound_ids=inbound, enabled=False)
            s.add(panel)
        await s.commit()

    label = PANEL_LABELS.get(key, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 تست اتصال", callback_data=f"tvpn:test:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:panel:{key}", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"✅ تنظیمات <b>{label}</b> ذخیره شد.\n"
        "حالا «🔌 تست اتصال» را بزن و اگر موفق بود، فروش خودکار را فعال کن.",
        reply_markup=kb)


# ─────────────── تست اتصال ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:test:"))
async def vpn_test(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    panel = await get_panel(row.id, key)
    if not panel:
        await cb.answer("اول تنظیمات پنل را ثبت کن.", show_alert=True)
        return
    label = PANEL_LABELS.get(key, key)
    await cb.answer(f"در حال اتصال به {label}…")
    client = make_panel_client(panel)
    try:
        res = await client.test_connection()
        ids = ", ".join(str(i) for i in res["inbounds"])
        await cb.message.answer(
            f"✅ اتصال به <b>{label}</b> موفق بود.\ninboundهای فعال: {ids}")
    except PanelError as e:
        await cb.message.answer(f"❌ اتصال به <b>{label}</b> ناموفق:\n{e}")
    except Exception as e:
        await cb.message.answer(f"❌ خطای غیرمنتظره:\n{e}")
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ─────────────── فعال/غیرفعال‌کردن فروش ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:toggle:"))
async def vpn_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    async with Session() as s:
        panel = await s.scalar(
            select(VpnPanel).where(VpnPanel.bot_id == row.id, VpnPanel.panel_key == key))
        if panel:
            panel.enabled = not panel.enabled
            await s.commit()
    # بازگشت به منوی همان پنل
    fake_cb = type("CB", (), {
        "data": f"tvpn:panel:{key}", "answer": cb.answer,
        "message": cb.message, "from_user": cb.from_user
    })()
    await vpn_panel_menu(fake_cb, bot)


# ─────────────── حذف پنل ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:delete:"))
async def vpn_delete_confirm(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    label = PANEL_LABELS.get(key, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚠️ بله، {label} را حذف کن",
                              callback_data=f"tvpn:delete_ok:{key}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"tvpn:panel:{key}", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(
            f"⚠️ <b>آیا مطمئنی؟</b>\n\n"
            f"با حذف {label}، تنظیمات اتصال و <b>پلن‌های فروشِ همین پنل</b> هم پاک می‌شود.\n"
            "کانفیگ‌های قبلاً ساخته‌شده دست‌نخورده می‌مانند.",
            reply_markup=kb)
    except Exception:
        await cb.message.answer(f"پنل {label} حذف شود؟", reply_markup=kb)
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:delete_ok:"))
async def vpn_delete_ok(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    label = PANEL_LABELS.get(key, key)
    async with Session() as s:
        panel = await s.scalar(
            select(VpnPanel).where(VpnPanel.bot_id == row.id, VpnPanel.panel_key == key))
        if panel:
            await s.delete(panel)
            # === PATCH_PANEL_DELETE_PLANS_V1 === پلن‌هایِ فروشِ این پنل هم حذف شوند (plan_id در سفارش‌ها FK نیست)
            from models import VpnPlan
            for _pl in (await s.scalars(select(VpnPlan).where(
                    VpnPlan.bot_id == row.id, VpnPlan.panel_key == key))).all():
                await s.delete(_pl)
            await s.commit()
    await cb.answer(f"{label} حذف شد", show_alert=False)
    await vpn_menu(cb, bot)


# ─────────────── فروش آنلاین (حجم/روز/تعداد دلخواه) ───────────────
async def _vc_kb(cfg: dict, panel_key: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    en = cfg.get("vc_enabled") == "1"
    toggle = "🟢 فروش آنلاین: فعال" if en else "🔴 فروش آنلاین: غیرفعال"
    label = PANEL_LABELS.get(panel_key, panel_key)
    mode = cfg.get("vc_pricing_mode", "general")
    mode_toggle = ("🎚 روشِ قیمت‌گذاری: تعرفه‌های مدت‌دار (بزن برای تغییر به بازهٔ عمومی)"
                  if mode == "tiers" else
                  "📐 روشِ قیمت‌گذاری: بازهٔ عمومی (بزن برای تغییر به تعرفه‌های مدت‌دار)")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"tvpn:custom_toggle:{panel_key}",
                              style=await _ibs(bot_id, "ibtn:vpn_toggle"))],
        [InlineKeyboardButton(text=mode_toggle, callback_data=f"tvpn:custom_mode:{panel_key}", style=ButtonStyle.PRIMARY)],
        # === PATCH_VC_SIMPLE_MODE_V1 ===
        [InlineKeyboardButton(
            text=("📝 حالتِ ساده (فقط حجم): ✅ روشن" if cfg.get("vc_simple") == "1"
                  else "📝 حالتِ ساده (فقط حجم): ⬜️ خاموش"),
            callback_data=f"tvpn:custom_simple:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"📅 مدتِ ثابتِ حالتِ ساده: {cfg.get('vc_simple_days') or 30} روز",
                              callback_data=f"tvpn:custom_simpledays:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📦 بازهٔ حجم (گیگ)", callback_data=f"tvpn:custom_gb:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📅 بازهٔ مدت (روز)", callback_data=f"tvpn:custom_days:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔢 حداکثر تعداد", callback_data=f"tvpn:custom_count:{panel_key}", style=ButtonStyle.PRIMARY)],
        # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
        [InlineKeyboardButton(text="🔒 محدودیتِ IP این پنل",
                              callback_data=f"tvpn:panel_iplimit:{panel_key}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💵 قیمت هر گیگ/روز", callback_data=f"tvpn:custom_price:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🎚 تعرفه‌های مدت‌دار (قفل ماهانه)", callback_data=f"tvpn:custom_tiers:{panel_key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"⬅️ بازگشت به {label}", callback_data=f"tvpn:panel:{panel_key}",
                              style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)],
    ])


def _vc_text(cfg: dict, panel_key: str = "", tier_count: int = 0) -> str:
    en = "✅ فعال" if cfg.get("vc_enabled") == "1" else "❌ غیرفعال"
    label = PANEL_LABELS.get(panel_key, panel_key)
    header = f"🛒 <b>فروش آنلاین — {label}</b>" if panel_key else "🛒 <b>فروش آنلاین</b>"
    mode = cfg.get("vc_pricing_mode", "general")
    if mode == "tiers":
        mode_line = (f"🎚 روشِ فعال: <b>تعرفه‌های مدت‌دار</b> ({tier_count} مورد) — بازهٔ حجم/قیمتِ عمومیِ زیر "
                     "نادیده گرفته می‌شود و فقط برای بازه‌هایی که هیچ تعرفه‌ای پوششش نمی‌دهد به‌کار می‌رود.\n")
    else:
        mode_line = "📐 روشِ فعال: <b>بازهٔ عمومی</b> (زیر) — تعرفه‌های مدت‌دار (حتی اگر ساخته شده باشند) اعمال نمی‌شوند.\n"
    # === PATCH_RESELLER_SETTINGS_MOVED_V1 ===
    # تعرفهٔ نمایندگان از این صفحه به «👮 ادمین‌ها و پشتیبانی → 🏷 نمایندگی»
    # منتقل شد. دلیلش: این صفحه تنظیماتِ فروش به *همهٔ کاربران* است و
    # داشتنِ تنظیمِ نمایندگی وسطِ آن، مالکِ ربات را سردرگم می‌کرد.
    _rs_g = str(cfg.get("rs_price_gb") or "").strip()
    _rs_price_line = ""
    if _rs_g.isdigit() and int(_rs_g) > 0:
        _rs_price_line = (f"🏷 تعرفهٔ نمایندگان (از بخشِ نمایندگی): "
                          f"{int(_rs_g):,} تومان/گیگ، "
                          f"{int(cfg.get('rs_price_day') or 0):,} تومان/روز\n")
    return (
        f"{header}\n\n"
        f"وضعیت: {en}\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{mode_line}"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 حجم: از {cfg['vc_min_gb']} تا {cfg['vc_max_gb']} گیگ (گام {cfg['vc_step_gb']})\n"
        f"📅 مدت: از {cfg['vc_min_days']} تا {cfg['vc_max_days']} روز (گام {cfg['vc_step_days']})\n"
        f"🔢 حداکثر تعداد در هر سفارش: {cfg['vc_max_count']}\n"
        + (f"📝 حالتِ ساده: <b>روشن</b> — کاربر فقط عددِ حجم را می‌فرستد؛ مدتِ ثابت: {cfg.get('vc_simple_days') or 30} روز\n"
           if cfg.get("vc_simple") == "1" else "") +
        f"💵 قیمت هر گیگ: {int(cfg['vc_price_gb']):,} تومان\n"
        f"💵 قیمت هر روز: {int(cfg['vc_price_day']):,} تومان\n"
        f"{_rs_price_line}"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💡 فرمول قیمت: (حجم×قیمت‌گیگ + روز×قیمت‌روز) × تعداد\n\n"
        "برای تغییر هر مورد، روی دکمه‌اش بزن."
    )


# ─────────────── بروزرسانی اینباندها و ساب‌لینک کاربران ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:refresh_subs:"))
async def vpn_refresh_subs(cb: CallbackQuery, bot: Bot):
    """بروزرسانی اینباندهای پنل و ساب‌لینک همهٔ کاربران این پنل."""
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    label = PANEL_LABELS.get(key, key)
    panel = await get_panel(row.id, key)
    if not panel:
        await cb.answer("اول تنظیمات پنل را ثبت کن.", show_alert=True)
        return

    await cb.answer(f"⏳ در حال بروزرسانی {label}…")
    await cb.message.answer(f"⏳ در حال گرفتن لیست اینباندهای <b>{label}</b> از پنل…")

    client = make_panel_client(panel)
    try:
        # ─── ۱) گرفتن لیست اینباندهای فعال از پنل ───
        await client.login()
        inbounds = await client.list_inbounds()
        active_inbounds = [ib for ib in inbounds if ib.get("enable", True)]
        if _bk(key) in ("marzban", "rebecca", "upanel_mizban", "pasarguard", "pasargad"):
            # در این پنل‌ها شناسهٔ اینباند رشتهٔ «tag» است، نه عدد
            active_ids = [str(ib["id"]) for ib in active_inbounds if ib.get("id")]
            active_names = ", ".join(str(ib.get("tag") or ib.get("id")) for ib in active_inbounds)
        else:
            active_ids = [int(ib["id"]) for ib in active_inbounds]
            active_names = ", ".join(
                f"#{ib['id']} {ib.get('remark', ib.get('tag', ''))}" for ib in active_inbounds
            )

        # ─── ۲) بروزرسانی inbound_ids در دیتابیس ───
        old_inbound_str = panel.inbound_ids or "همه"
        if active_ids:
            new_inbound_str = ",".join(str(i) for i in active_ids)
            if panel.inbound_ids != new_inbound_str:
                async with Session() as s:
                    p2 = await s.get(VpnPanel, panel.id)
                    if p2:
                        p2.inbound_ids = new_inbound_str
                        await s.commit()
                inbound_msg = (f"✅ اینباندها بروز شدند:\n"
                               f"   قبل: <code>{old_inbound_str}</code>\n"
                               f"   بعد: <code>{new_inbound_str}</code>")
            else:
                inbound_msg = f"ℹ️ اینباندها تغییری نداشتند: <code>{old_inbound_str}</code>"
        else:
            inbound_msg = "⚠️ هیچ اینباند فعالی در پنل پیدا نشد. inbound_ids تغییر نکرد."

        await cb.message.answer(
            f"🔌 <b>وضعیت اینباندهای {label}:</b>\n"
            f"{inbound_msg}\n\n"
            f"تعداد اینباند فعال: <b>{len(active_inbounds)}</b>\n"
            f"اینباندهای فعال پنل: {active_names or '—'}"
        )

        # ─── ۳) بروزرسانی ساب‌لینک همهٔ کاربران این پنل ───
        await cb.message.answer("⏳ در حال بروزرسانی ساب‌لینک کاربران…")

        # کوئری اکانت‌های فعال این پنل
        from sqlalchemy import select as _sel
        from models import PanelAccount as _PA
        async with Session() as s:
            accts = (await s.scalars(
                _sel(_PA).where(
                    _PA.bot_id == row.id,
                    _PA.panel_key == key,
                    _PA.status == "active"
                )
            )).all()
            acct_list = [(a.id, a.email, a.sub_id) for a in accts]

        if not acct_list:
            # ممکنه panel_key برای اکانت‌های قدیمی خالی باشه. فقط در صورتی که این
            # ربات تنها همین یک پنل را دارد، اکانت‌های بدون panel_key را به این
            # پنل نسبت می‌دهیم (وگرنه ممکن است اکانت پنل دیگری را خراب کنیم).
            all_panels = await get_all_panels(row.id)
            if len(all_panels) <= 1:
                async with Session() as s:
                    accts2 = (await s.scalars(
                        _sel(_PA).where(
                            _PA.bot_id == row.id,
                            _PA.status == "active",
                            (_PA.panel_key == None) | (_PA.panel_key == ""),  # noqa: E711
                        )
                    )).all()
                    acct_list = [(a.id, a.email, a.sub_id) for a in accts2]
                    # همزمان panel_key را هم تثبیت کن تا دفعهٔ بعد درست فیلتر شود
                    for a in accts2:
                        a.panel_key = key
                    if accts2:
                        await s.commit()
                if acct_list:
                    await cb.message.answer(
                        f"ℹ️ {len(acct_list)} اکانت بدون پنلِ مشخص پیدا شد و چون این ربات "
                        f"فقط یک پنل دارد، به <b>{label}</b> نسبت داده شد."
                    )
            else:
                await cb.message.answer(
                    "ℹ️ چند پنل فعال داری و بعضی اکانت‌ها پنلِ مشخص ندارند؛ برای جلوگیری "
                    "از خرابی، فقط اکانت‌هایی که دقیقاً به این پنل ثبت شده‌اند بروزرسانی شدند."
                )

        if not acct_list:
            await cb.message.answer("ℹ️ هیچ کاربر فعالی برای بروزرسانی پیدا نشد.")
        else:
            updated = 0
            failed_list = []
            for aid, email, sub_id in acct_list:
                try:
                    # === PATCH_FIX_REFRESH_SUBS_UPANEL_V1 ===
                    # upanel_mizban هم خانوادهٔ مرزبان است (لینکِ ساب را خودش
                    # برمی‌گرداند)؛ قبلاً اینجا نبود و باعث می‌شد بروزرسانیِ
                    # ساب‌لینکِ همهٔ کاربرانِ این پنل با «sub_base تنظیم نشده»
                    # شکست بخورد (چون در ادامه از مسیرِ sub_id+sub_base استفاده
                    # می‌شد که برایِ این پنل بی‌معناست).
                    if _bk(key) in ("marzban", "rebecca", "pasargad", "pasarguard", "marzneshin", "upanel_mizban"):
                        # مرزبان/ربکا/پاسارگاد/PasarGuard/مرزنشین لینک ساب را خودشان
                        # برمی‌گردانند؛ از روی sub_id قابل بازسازی نیست — همیشه از پنل می‌خوانیم.
                        cur = await client.get_client(email)
                        new_sub_link = (cur or {}).get("sub_link") or ""
                        if not new_sub_link:
                            failed_list.append(email)
                            continue
                    else:
                        if not sub_id:
                            # sub_id ندارد — از پنل بخوان
                            cur = await client.get_client(email)
                            sub_id = (cur or {}).get("subId")
                            if not sub_id:
                                failed_list.append(email)
                                continue
                        new_sub_link = client.sub_link(sub_id)
                        if not new_sub_link:
                            failed_list.append(f"{email}: sub_base تنظیم نشده")
                            continue
                    async with Session() as s:
                        a2 = await s.get(_PA, aid)
                        if a2:
                            a2.sub_link = new_sub_link
                            if not a2.sub_id:
                                a2.sub_id = sub_id
                            await s.commit()
                    updated += 1
                except Exception as _e:
                    failed_list.append(f"{email}: {_e}")

            result_lines = [
                "✅ <b>بروزرسانی ساب‌لینک‌ها تمام شد:</b>",
                f"• بروز شده: {updated} کاربر از {len(acct_list)}",
            ]
            if failed_list:
                result_lines.append(f"• ناموفق: {len(failed_list)}")
                for e in failed_list[:5]:
                    result_lines.append(f"  — {e}")
                if len(failed_list) > 5:
                    result_lines.append(f"  … و {len(failed_list)-5} مورد دیگر")
            result_lines.append(
                "\n💡 کاربران با ارسال /start یا بروزرسانی ساب‌لینک در اپ، سرورهای جدید را دریافت می‌کنند."
            )
            await cb.message.answer("\n".join(result_lines))

    except PanelError as e:
        await cb.message.answer(f"❌ خطا در اتصال به <b>{label}</b>:\n{e}")
    except Exception as e:
        import traceback
        await cb.message.answer(f"❌ خطای غیرمنتظره:\n{e}")
        print(f"[refresh_subs] error: {traceback.format_exc()}", flush=True)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    await cb.message.answer(
        "برای بازگشت به منوی پنل:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"⬅️ بازگشت به {label}", callback_data=f"tvpn:panel:{key}", style=ButtonStyle.DANGER)
        ]])
    )


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom:"))
async def vpn_custom(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cfg = await _vc_all(row.id, panel_key)
    try:
        await cb.message.edit_text(_vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                                   reply_markup=await _vc_kb(cfg, panel_key, row.id))
    except Exception:
        await cb.message.answer(_vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                                reply_markup=await _vc_kb(cfg, panel_key, row.id))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_toggle:"))
async def vpn_custom_toggle(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _vc_get(row.id, "vc_enabled", panel_key)
    await _vc_set(row.id, "vc_enabled", "0" if cur == "1" else "1", panel_key)
    cfg = await _vc_all(row.id, panel_key)
    try:
        await cb.message.edit_text(_vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                                   reply_markup=await _vc_kb(cfg, panel_key, row.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


# === PATCH_VC_SIMPLE_MODE_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_simple:"))
async def vpn_custom_simple_toggle(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _vc_get(row.id, "vc_simple", panel_key)
    await _vc_set(row.id, "vc_simple", "0" if cur == "1" else "1", panel_key)
    cfg = await _vc_all(row.id, panel_key)
    try:
        await cb.message.edit_text(_vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                                   reply_markup=await _vc_kb(cfg, panel_key, row.id))
    except Exception:
        logger.info("vc simple: ویرایشِ پنل ممکن نشد (بی‌اهمیت)")
    await cb.answer("حالتِ ساده " + ("روشن شد" if cur != "1" else "خاموش شد"))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_simpledays:"))
async def vpn_custom_simpledays(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.simple_days)
    await state.update_data(vc_panel_key=panel_key)
    cfg = await _vc_all(row.id, panel_key)
    await cb.answer()
    await cb.message.answer(f"📅 مدتِ ثابتِ سرویس‌هایی که با «حالتِ ساده» خریداری می‌شوند را به روز بفرست (عدد):\n"
                            f"فعلی: {cfg.get('vc_simple_days') or 30}", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnCustomFSM.simple_days, F.text)
async def vpn_custom_simpledays_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    n = fa_to_int(msg.text.strip())
    if n is None or not (1 <= n <= 3650):
        await msg.answer("عددِ معتبر (۱ تا ۳۶۵۰) بفرست:", reply_markup=cancel_kb())
        return
    await _vc_set(row.id, "vc_simple_days", str(n), panel_key)
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer("✅ ذخیره شد.\n\n" + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                     reply_markup=await _vc_kb(cfg, panel_key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_mode:"))
async def vpn_custom_mode_toggle(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _vc_get(row.id, "vc_pricing_mode", panel_key)
    new_mode = "general" if cur == "tiers" else "tiers"
    if new_mode == "tiers" and not await vc_tiers_get(row.id, panel_key):
        await cb.answer("اول باید حداقل یک تعرفه بسازی (پایین‌تر، «تعرفه‌های مدت‌دار»).", show_alert=True)
        return
    await _vc_set(row.id, "vc_pricing_mode", new_mode, panel_key)
    cfg = await _vc_all(row.id, panel_key)
    try:
        await cb.message.edit_text(_vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                                   reply_markup=await _vc_kb(cfg, panel_key, row.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("✅ روشِ قیمت‌گذاری تغییر یافت")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_gb:"))
async def vpn_custom_gb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.gb_range)
    await state.update_data(vc_panel_key=panel_key)
    cfg = await _vc_all(row.id, panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"بازهٔ حجم را وارد کن: «حداقل حداکثر گام» با فاصله\n"
        f"مثال: <code>5 100 5</code>\n"
        f"فعلی: {cfg['vc_min_gb']} تا {cfg['vc_max_gb']} گیگ (گام {cfg['vc_step_gb']})",
        reply_markup=cancel_kb())


@vpn_admin_router.message(VpnCustomFSM.gb_range, F.text)
async def vpn_custom_gb_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 3 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("سه عدد با فاصله بفرست: حداقل حداکثر گام", reply_markup=cancel_kb())
        return
    mn, mx, st = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "vc_min_gb", str(mn), panel_key)
    await _vc_set(row.id, "vc_max_gb", str(mx), panel_key)
    await _vc_set(row.id, "vc_step_gb", str(st), panel_key)
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer("✅ ذخیره شد.\n\n" + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                     reply_markup=await _vc_kb(cfg, panel_key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_days:"))
async def vpn_custom_days(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.days_range)
    await state.update_data(vc_panel_key=panel_key)
    cfg = await _vc_all(row.id, panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"بازهٔ روز را وارد کن: «حداقل حداکثر گام»\n"
        f"مثال: <code>30 90 30</code>\n"
        f"فعلی: {cfg['vc_min_days']} تا {cfg['vc_max_days']} روز (گام {cfg['vc_step_days']})",
        reply_markup=cancel_kb())


@vpn_admin_router.message(VpnCustomFSM.days_range, F.text)
async def vpn_custom_days_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 3 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("سه عدد با فاصله بفرست: حداقل حداکثر گام", reply_markup=cancel_kb())
        return
    mn, mx, st = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "vc_min_days", str(mn), panel_key)
    await _vc_set(row.id, "vc_max_days", str(mx), panel_key)
    await _vc_set(row.id, "vc_step_days", str(st), panel_key)
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer("✅ ذخیره شد.\n\n" + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                     reply_markup=await _vc_kb(cfg, panel_key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_count:"))
async def vpn_custom_count(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.max_count)
    await state.update_data(vc_panel_key=panel_key)
    cfg = await _vc_all(row.id, panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"حداکثر تعداد در هر سفارش را بفرست (عدد):\nفعلی: {cfg['vc_max_count']}",
        reply_markup=cancel_kb())


@vpn_admin_router.message(VpnCustomFSM.max_count, F.text)
async def vpn_custom_count_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    n = fa_to_int(msg.text.strip())
    if n is None or n < 1:
        await msg.answer("عدد معتبر (حداقل ۱) بفرست:", reply_markup=cancel_kb())
        return
    await _vc_set(row.id, "vc_max_count", str(n), panel_key)
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer("✅ ذخیره شد.\n\n" + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                     reply_markup=await _vc_kb(cfg, panel_key, row.id))


# === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
# محدودیتِ تعدادِ کاربرِ هم‌زمان (IP) رویِ خودِ پنل. تا امروز فقط «پلن»
# می‌توانست محدودیت داشته باشد، پس خریدِ «حجم/روزِ دلخواه» — که پلنی ندارد —
# همیشه بدونِ محدودیت ساخته می‌شد.
_IP_LIMIT_PANELS = {"pasargad", "pasarguard", "rebecca", "shahan", "sanai", "alireza", "degeris", "eylan"}


@vpn_admin_router.callback_query(F.data.startswith("tvpn:panel_iplimit:"))
async def vpn_panel_iplimit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":", 2)[2]
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        p = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.panel_key == panel_key))
    cur = int(getattr(p, "default_limit_ip", 0) or 0) if p else 0
    supported = panel_base_key(panel_key) in _IP_LIMIT_PANELS
    await state.set_state(VpnCustomFSM.ip_limit)
    await state.update_data(vc_panel_key=panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    note = ("" if supported else
            "\n\n⚠️ این نوع پنل محدودیتِ IP را پشتیبانی نمی‌کند و عدد را "
            "نادیده می‌گیرد. (پشتیبانی‌شده: پاسارگاد، ربکا، شاهان، X-UI)")
    await cb.message.answer(
        "🔒 <b>محدودیتِ پیش‌فرضِ IP این پنل</b>\n\n"
        "تعدادِ کاربرِ هم‌زمانی که هر کانفیگِ ساخته‌شده رویِ این پنل اجازه "
        "دارد. عدد بفرست — <code>0</code> یعنی بدونِ محدودیت.\n\n"
        f"فعلی: <b>{cur if cur else 'بدونِ محدودیت'}</b>\n\n"
        "<i>روی خریدِ حجم/روزِ دلخواه همیشه اعمال می‌شود؛ روی خریدِ پلن فقط "
        "وقتی که خودِ پلن عددی نداشته باشد.</i>" + note,
        reply_markup=cancel_kb())


@vpn_admin_router.message(VpnCustomFSM.ip_limit, F.text)
async def vpn_panel_iplimit_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("عدد معتبر (۰ یا بیشتر) بفرست:", reply_markup=cancel_kb())
        return
    async with Session() as s:
        p = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.panel_key == panel_key))
        if p:
            p.default_limit_ip = n
            await s.commit()
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer(
        ("✅ ذخیره شد — بدونِ محدودیت." if n == 0
         else f"✅ ذخیره شد — هر کانفیگ حداکثر {n} کاربرِ هم‌زمان.") + "\n\n"
        + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
        reply_markup=await _vc_kb(cfg, panel_key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_price:"))
async def vpn_custom_price(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.prices)
    await state.update_data(vc_panel_key=panel_key)
    cfg = await _vc_all(row.id, panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"قیمت هر گیگ و هر روز را با فاصله بفرست (تومان):\n"
        f"مثال: <code>1000 500</code>\n"
        f"فعلی: {int(cfg['vc_price_gb']):,} تومان/گیگ، {int(cfg['vc_price_day']):,} تومان/روز",
        reply_markup=cancel_kb())


# === PATCH_RESELLER_GB_PRICING_V1 ===
# === PATCH_RESELLER_SETTINGS_MOVED_V1 ===
# مسیرِ قدیمی: کسی که دکمهٔ ذخیره‌شده یا پیامِ قدیمی را بزند، به‌جایِ
# «کال‌بکِ نامعتبر» راهنماییِ درست بگیرد.
@vpn_admin_router.callback_query(F.data.startswith("tvpn:rsprice:"))
async def vpn_reseller_price_moved(cb: CallbackQuery, bot: Bot):
    await cb.answer(
        "این تنظیم منتقل شد به: پنل مدیریت → ادمین‌ها و پشتیبانی → "
        "نمایندگی → تعرفهٔ گیگیِ نمایندگان", show_alert=True)


@vpn_admin_router.message(VpnCustomFSM.prices, F.text)
async def vpn_custom_price_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 2 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("دو عدد با فاصله بفرست: قیمت‌گیگ قیمت‌روز", reply_markup=cancel_kb())
        return
    pg, pd = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "vc_price_gb", str(pg), panel_key)
    await _vc_set(row.id, "vc_price_day", str(pd), panel_key)
    await state.clear()
    cfg = await _vc_all(row.id, panel_key)
    await msg.answer("✅ ذخیره شد.\n\n" + _vc_text(cfg, panel_key, len(await vc_tiers_get(row.id, panel_key))),
                     reply_markup=await _vc_kb(cfg, panel_key, row.id))


# ─────────────── تعرفه‌های مدت‌دار (قفل ماهانه) ───────────────
def _vc_tier_line(t: dict, idx: int) -> str:
    return (f"{idx}. 📅 {t.get('days_min')}–{t.get('days_max')} روز (گام {t.get('days_step', 1)})  ⟵  "
           f"📦 {t.get('gb_min')}–{t.get('gb_max')} گیگ (گام {t.get('gb_step')})  ⟵  "
           f"💵 {int(t.get('price_gb', 0)):,}ت/گیگ، {int(t.get('price_day', 0)):,}ت/روز")


async def _vc_tiers_kb(tiers: list[dict], panel_key: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    rows = []
    for i, t in enumerate(tiers):
        rows.append([
            InlineKeyboardButton(text=f"✏️ ویرایش تعرفهٔ {i+1} ({t.get('days_min')}–{t.get('days_max')} روز)",
                                 callback_data=f"tvpn:custom_tier_edit:{panel_key}:{i}"),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"tvpn:custom_tier_del:{panel_key}:{i}", style=ButtonStyle.DANGER),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن تعرفهٔ جدید",
                                      callback_data=f"tvpn:custom_tier_add:{panel_key}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:custom:{panel_key}",
                                      style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vc_tiers_text(tiers: list[dict]) -> str:
    header = ("🎚 <b>تعرفه‌های مدت‌دار (قفل ماهانه)</b>\n\n"
             "برای هر بازهٔ روز (مثلاً یک‌ماهه/دوماهه/سه‌ماهه) می‌تونی یه بازهٔ حجمِ "
             "مخصوص و یه قیمتِ مخصوص تعریف کنی — وقتی مشتری اون مدت رو انتخاب می‌کنه، "
             "خودکار همون حجم و قیمت اعمال می‌شه.\n"
             "اگه برای یه بازهٔ روز تعرفه تعریف نشده باشه، از تنظیماتِ کلیِ بالا "
             "(بازهٔ حجم/قیمتِ عمومی) استفاده می‌شه.\n\n")
    if not tiers:
        return header + "فعلاً هیچ تعرفه‌ای تعریف نشده."
    return header + "\n".join(_vc_tier_line(t, i + 1) for i, t in enumerate(tiers))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_tiers:"))
async def vpn_custom_tiers(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    tiers = await vc_tiers_get(row.id, panel_key)
    try:
        await cb.message.edit_text(_vc_tiers_text(tiers),
                                   reply_markup=await _vc_tiers_kb(tiers, panel_key, row.id))
    except Exception:
        await cb.message.answer(_vc_tiers_text(tiers),
                                reply_markup=await _vc_tiers_kb(tiers, panel_key, row.id))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_tier_del:"))
async def vpn_custom_tier_del(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    panel_key, idx = parts[2], int(parts[3])
    row = await _owner(cb, bot)
    if not row:
        return
    tiers = await vc_tiers_get(row.id, panel_key)
    if 0 <= idx < len(tiers):
        tiers.pop(idx)
        await vc_tiers_set(row.id, panel_key, tiers)
    try:
        await cb.message.edit_text(_vc_tiers_text(tiers),
                                   reply_markup=await _vc_tiers_kb(tiers, panel_key, row.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("🗑 حذف شد")


_TIER_WIZ_FIELDS = [
    ("days_min",  "حداقل روز"),
    ("days_max",  "حداکثر روز"),
    ("days_step", "گام روز"),
    ("gb_min",    "حداقل گیگ"),
    ("gb_max",    "حداکثر گیگ"),
    ("gb_step",   "گام گیگ"),
    ("price_gb",  "قیمت گیگ (تومان)"),
    ("price_day", "قیمت روز (تومان)"),
]


def _tier_wiz_text(fields: dict, editing: bool = False) -> str:
    n = len(fields)
    title = "✏️ ویرایش تعرفه" if editing else "🎚 افزودن تعرفهٔ جدید"
    return (
        f"<b>{title}</b>\n\n"
        f"({n} از {len(_TIER_WIZ_FIELDS)} مورد پر شده)\n\n"
        "روی هرکدوم از دکمه‌های زیر بزن و عددش رو بفرست (صفر هم مجاز است).\n"
        "وقتی همه پر شدن، دکمهٔ «✅ ثبت تعرفه» فعال می‌شود."
    )


def _tier_wiz_kb(fields: dict, panel_key: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label in _TIER_WIZ_FIELDS:
        if key in fields:
            rows.append([InlineKeyboardButton(text=f"✅ {label}: {fields[key]:,}",
                                              callback_data=f"tvpn:twz:{key}", style=ButtonStyle.SUCCESS)])
        else:
            rows.append([InlineKeyboardButton(text=f"▫️ {label}: بزن و وارد کن",
                                              callback_data=f"tvpn:twz:{key}", style=ButtonStyle.DANGER)])
    if len(fields) == len(_TIER_WIZ_FIELDS):
        rows.append([InlineKeyboardButton(text="✅ ثبت تعرفه", callback_data="tvpn:twz:submit",
                                          style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="tvpn:twz:cancel",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_tier_add:"))
async def vpn_custom_tier_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCustomFSM.tier_wizard)
    await state.update_data(vc_panel_key=panel_key, twz_fields={}, twz_wait=None, twz_edit_idx=None)
    await cb.answer()
    try:
        await cb.message.edit_text(_tier_wiz_text({}), reply_markup=_tier_wiz_kb({}, panel_key))
    except Exception:
        await cb.message.answer(_tier_wiz_text({}), reply_markup=_tier_wiz_kb({}, panel_key))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:custom_tier_edit:"))
async def vpn_custom_tier_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    panel_key, idx = parts[2], int(parts[3])
    row = await _owner(cb, bot)
    if not row:
        return
    tiers = await vc_tiers_get(row.id, panel_key)
    if not (0 <= idx < len(tiers)):
        await cb.answer("این تعرفه دیگر وجود ندارد.", show_alert=True)
        return
    fields = {k: tiers[idx].get(k) for k, _ in _TIER_WIZ_FIELDS if k in tiers[idx]}
    await state.set_state(VpnCustomFSM.tier_wizard)
    await state.update_data(vc_panel_key=panel_key, twz_fields=fields, twz_wait=None, twz_edit_idx=idx)
    await cb.answer()
    try:
        await cb.message.edit_text(_tier_wiz_text(fields, editing=True),
                                   reply_markup=_tier_wiz_kb(fields, panel_key))
    except Exception:
        await cb.message.answer(_tier_wiz_text(fields, editing=True),
                                reply_markup=_tier_wiz_kb(fields, panel_key))


@vpn_admin_router.callback_query(VpnCustomFSM.tier_wizard, F.data.startswith("tvpn:twz:"))
async def vpn_tier_wiz_button(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    fields = dict(data.get("twz_fields") or {})
    edit_idx = data.get("twz_edit_idx")

    if key == "cancel":
        await state.clear()
        tiers = await vc_tiers_get(row.id, panel_key)
        await cb.answer("لغو شد")
        try:
            await cb.message.edit_text(_vc_tiers_text(tiers),
                                       reply_markup=await _vc_tiers_kb(tiers, panel_key, row.id))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return

    if key == "submit":
        if len(fields) != len(_TIER_WIZ_FIELDS):
            await cb.answer("هنوز همهٔ موارد پر نشده.", show_alert=True)
            return
        d_min, d_max = fields["days_min"], fields["days_max"]
        d_step = fields.get("days_step", 1)
        g_min, g_max, g_step = fields["gb_min"], fields["gb_max"], fields["gb_step"]
        if d_min > d_max or g_min > g_max or g_step < 1 or d_step < 1:
            await cb.answer("مقادیر نامعتبرند؛ حداقل نباید از حداکثر بیشتر باشد و گام‌ها حداقل ۱ باشند.",
                            show_alert=True)
            return
        tiers = await vc_tiers_get(row.id, panel_key)
        # === PATCH_VPN_TIER_CUMULATIVE_V1 ===
        # پله‌هایِ حجمیِ یک بازهٔ روز نباید هم‌پوشانی داشته باشند، وگرنه
        # معلوم نیست حجمِ مرزی مالِ کدام پله است. به‌جایِ حدس‌زدن، همین‌جا
        # جلویش گرفته می‌شود و به ادمین گفته می‌شود از چه عددی شروع کند.
        clash = _tier_overlap(tiers, fields, edit_idx)
        if clash:
            await cb.answer(clash, show_alert=True)
            return
        if edit_idx is not None and 0 <= edit_idx < len(tiers):
            tiers[edit_idx] = dict(fields)
            done_msg = "✅ تعرفه ویرایش شد"
        else:
            tiers.append(dict(fields))
            done_msg = "✅ تعرفه اضافه شد"
        await vc_tiers_set(row.id, panel_key, tiers)
        await state.clear()
        tiers = await vc_tiers_get(row.id, panel_key)
        await cb.answer(done_msg)
        try:
            await cb.message.edit_text(f"{done_msg}.\n\n" + _vc_tiers_text(tiers),
                                       reply_markup=await _vc_tiers_kb(tiers, panel_key, row.id))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return

    # انتخاب یکی از فیلدها → منتظرِ عددِ بعدی که ادمین تایپ می‌کند
    label = dict(_TIER_WIZ_FIELDS).get(key, key)
    await state.update_data(twz_wait=key, twz_msg_id=cb.message.message_id, twz_chat_id=cb.message.chat.id)
    await cb.answer()
    try:
        await cb.message.edit_text(
            f"🔢 <b>{label}</b> را به عدد بفرست (صفر هم مجاز است):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data="tvpn:twz:cancel", style=ButtonStyle.DANGER)
            ]]))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@vpn_admin_router.message(VpnCustomFSM.tier_wizard, F.text)
async def vpn_tier_wiz_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("vc_panel_key", "")
    wait_key = data.get("twz_wait")
    wiz_msg_id = data.get("twz_msg_id")
    wiz_chat_id = data.get("twz_chat_id")
    fields = dict(data.get("twz_fields") or {})

    # پیامِ ادمین بلافاصله حذف می‌شود تا فقط دکمه‌های شیشه‌ای با مقادیرِ
    # داخلشان روی صفحه بمانند (طبق درخواست: «پیام ادمین دیلیت و درون دکمهٔ
    # شیشه‌ای بشینه»).
    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if not wait_key or not wiz_msg_id:
        return  # حالتِ نامعتبر؛ چیزی برای پر کردن نبود

    n = fa_to_int((msg.text or "").strip())
    if n is None or n < 0:
        try:
            await bot.edit_message_text(
                chat_id=wiz_chat_id, message_id=wiz_msg_id,
                text=f"❌ عددِ نامعتبر. یک عددِ صفر یا مثبت بفرست:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="❌ لغو", callback_data="tvpn:twz:cancel", style=ButtonStyle.DANGER)
                ]]))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return

    fields[wait_key] = n
    await state.update_data(twz_fields=fields, twz_wait=None)
    try:
        await bot.edit_message_text(
            chat_id=wiz_chat_id, message_id=wiz_msg_id,
            text=_tier_wiz_text(fields, editing=data.get("twz_edit_idx") is not None),
            reply_markup=_tier_wiz_kb(fields, panel_key))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
# نامی که موقع خرید و در ساب‌لینک (داخل اپ کلاینت کاربر) به‌جای نام پیش‌فرض
# سرور به مشتری نمایش داده می‌شود — قابل تنظیم جدا برای هر پنل.
_SRVNAME_IDTYPE_LABELS = {  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    "id": "آیدی عددی کاربر",
    "username": "یوزرنیم کاربر",
    "custom": "نام دلخواه کاربر (وارد شده موقعِ خرید)",
}


def _srvname_text(cfg: dict, panel_key: str, admin_id: int, admin_username: str | None) -> str:
    label = PANEL_LABELS.get(panel_key, panel_key)
    prefix_disp = cfg["prefix"] or "پیش‌فرض (نام ربات)"
    idtype_disp = _SRVNAME_IDTYPE_LABELS.get(cfg["idtype"], "آیدی عددی کاربر")  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    example = _srvname_preview(cfg["prefix"], cfg["idtype"], None, admin_id, admin_username)
    extra_note = (  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
        "\n\n⚠️ توجه: در حالتِ «نام دلخواه کاربر»، این نام همون اسمیه که کاربر "
        "موقعِ خرید (در مرحلهٔ «یک نام دلخواه برای این کانفیگ بفرست») وارد "
        "می‌کند. اگه کاربر اسمی وارد نکنه و رد بشه («بدون نام»)، به‌صورتِ "
        "خودکار از یوزرنیمِ تلگرامِ خودش استفاده می‌شود؛ اگه یوزرنیم هم نداشت، "
        "از آیدیِ عددی."  # === PATCH_SRVNAME_CUSTOM_SKIP_USERNAME_V1 ===
        if cfg["idtype"] == "custom" else ""
    )
    return (
        f"📝 <b>نام سرورها مشتری — {label}</b>\n\n"
        "این نام، موقع خرید و داخل ساب‌لینک (اپ کلاینت کاربر) به‌جای نام پیش‌فرض "
        "سرور به مشتری نمایش داده می‌شود.\n\n"
        f"▫️ نام پایه: <b>{prefix_disp}</b>\n"
        f"▫️ شناسهٔ کاربر: <b>{idtype_disp}</b>\n\n"
        f"نمونه برای شما: <code>{example}…</code>\n"
        "(در پایان هر نام چند کاراکتر تصادفی هم برای یکتا بودن اضافه می‌شود.)"
        f"{extra_note}"
    )


_SRVNAME_IDTYPE_CYCLE = {"id": "username", "username": "custom", "custom": "id"}  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
_SRVNAME_IDTYPE_TOGGLE_BTN = {  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    "id": "🔢 شناسه: آیدی عددی ← تغییر به یوزرنیم",
    "username": "👤 شناسه: یوزرنیم ← تغییر به نام دلخواه کاربر",
    "custom": "✍️ شناسه: نام دلخواه کاربر ← تغییر به آیدی عددی",
}


async def _srvname_kb(cfg: dict, panel_key: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    label = PANEL_LABELS.get(panel_key, panel_key)
    id_toggle = _SRVNAME_IDTYPE_TOGGLE_BTN.get(cfg["idtype"], _SRVNAME_IDTYPE_TOGGLE_BTN["id"])  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    rows = [
        [InlineKeyboardButton(text="✏️ تغییر نام پایه", callback_data=f"tvpn:srvname_prefix:{panel_key}",
                              style=await _ibs(bot_id, "ibtn:vpn_custom"))],
        [InlineKeyboardButton(text=id_toggle, callback_data=f"tvpn:srvname_idtype:{panel_key}",
                              style=await _ibs(bot_id, "ibtn:vpn_custom"))],
    ]
    if cfg["prefix"]:
        rows.append([InlineKeyboardButton(
            text="♻️ بازگشت به نام پیش‌فرض (نام ربات)",
            callback_data=f"tvpn:srvname_reset:{panel_key}",
            style=await _ibs(bot_id, "ibtn:vpn_delete"))])
    rows.append([InlineKeyboardButton(
        text="🔄 اعمال نام‌ها روی اکانت‌های موجود",
        callback_data=f"tvpn:srvname_apply:{panel_key}",
        style=await _ibs(bot_id, "ibtn:vpn_custom"))])
    rows.append([InlineKeyboardButton(text=f"⬅️ بازگشت به {label}", callback_data=f"tvpn:panel:{panel_key}",
                                      style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:srvname:"))
async def vpn_srvname(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cfg = await get_srvname_cfg(row.id, panel_key)
    text = _srvname_text(cfg, panel_key, cb.from_user.id, cb.from_user.username)
    kb = await _srvname_kb(cfg, panel_key, row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:srvname_idtype:"))
async def vpn_srvname_idtype_toggle(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _srvname_get(row.id, "srvname_idtype", panel_key)
    new_val = _SRVNAME_IDTYPE_CYCLE.get(cur, "username")  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    await _srvname_set(row.id, "srvname_idtype", new_val, panel_key)
    cfg = await get_srvname_cfg(row.id, panel_key)
    text = _srvname_text(cfg, panel_key, cb.from_user.id, cb.from_user.username)
    kb = await _srvname_kb(cfg, panel_key, row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("✅ ذخیره شد.")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:srvname_reset:"))
async def vpn_srvname_reset(cb: CallbackQuery, bot: Bot):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await _srvname_set(row.id, "srvname_prefix", "", panel_key)
    cfg = await get_srvname_cfg(row.id, panel_key)
    text = _srvname_text(cfg, panel_key, cb.from_user.id, cb.from_user.username)
    kb = await _srvname_kb(cfg, panel_key, row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("✅ به نام پیش‌فرض برگشت.")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:srvname_prefix:"))
async def vpn_srvname_prefix_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnSrvNameFSM.prefix)
    await state.update_data(srvname_panel_key=panel_key)
    cfg = await get_srvname_cfg(row.id, panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    cur_disp = cfg["prefix"] or "پیش‌فرض (نام ربات)"
    await cb.message.answer(
        "✏️ نام پایه‌ای که می‌خوای به‌جای نام ربات نمایش داده شود را بفرست\n"
        "(فقط حروف/عدد انگلیسی، بدون فاصله — مثال: <code>BotSaz</code>)\n\n"
        f"فعلی: <b>{cur_disp}</b>",
        reply_markup=cancel_kb())


@vpn_admin_router.message(VpnSrvNameFSM.prefix, F.text)
async def vpn_srvname_prefix_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("srvname_panel_key", "")
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", msg.text.strip())[:20]
    if not cleaned:
        await msg.answer(
            "فقط حروف/عدد انگلیسی قبول است (بدون فاصله). دوباره بفرست:",
            reply_markup=cancel_kb())
        return
    await _srvname_set(row.id, "srvname_prefix", cleaned, panel_key)
    await state.clear()
    cfg = await get_srvname_cfg(row.id, panel_key)
    text = "✅ ذخیره شد.\n\n" + _srvname_text(cfg, panel_key, msg.from_user.id, msg.from_user.username)
    await msg.answer(text, reply_markup=await _srvname_kb(cfg, panel_key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:srvname_apply:"))
async def vpn_srvname_apply(cb: CallbackQuery, bot: Bot):
    """نام سرورِ تنظیم‌شده را روی همهٔ اکانت‌های موجودِ همین پنل اعمال می‌کند.

    فقط اکانت‌های فعالِ همین پنل بازنام‌گذاری می‌شوند (نه پنل‌های دیگر). در
    پنل‌های مرزبان/پاسارگاد چون نام‌کاربری همان شناسهٔ اکانت است، عملیات
    پشتیبانی نمی‌شود و به ادمین اطلاع داده می‌شود.
    """
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    if not is_panel_key(panel_key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    label = PANEL_LABELS.get(panel_key, panel_key)
    panel = await get_panel(row.id, panel_key)
    if not panel:
        await cb.answer("اول تنظیمات پنل را ثبت کن.", show_alert=True)
        return

    # پنل‌های مرزبان‌بنیان: تغییر نام روی اکانت موجود ممکن نیست
    if _bk(panel_key) in ("marzban", "rebecca", "pasargad", "pasarguard", "marzneshin"):
        await cb.answer()
        await cb.message.answer(
            f"ℹ️ در <b>{label}</b> نام کاربری همان شناسهٔ یکتای اکانت است و روی "
            "اکانت‌های موجود قابل تغییر نیست. نام جدید فقط روی کانفیگ‌های تازه "
            "اعمال می‌شود.")
        return

    await cb.answer("⏳ در حال اعمال نام‌ها…")
    await cb.message.answer(f"⏳ در حال اعمال نام سرور روی اکانت‌های <b>{label}</b>…")

    from tenant.bots.vpn.vpn_user import build_srv_name
    from tenant.shared.panel_clients.vpn_panel import gen_sub_id
    from sqlalchemy import select as _sel
    from models import PanelAccount as _PA

    cfg = await get_srvname_cfg(row.id, panel_key)
    bot_username = None
    try:
        me = await bot.get_me()
        bot_username = me.username
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    async with Session() as s:
        accts = (await s.scalars(
            _sel(_PA).where(
                _PA.bot_id == row.id, _PA.panel_key == panel_key,
                _PA.status == "active",
            )
        )).all()
        acct_list = [(a.id, a.email, a.user_telegram_id) for a in accts]

    if not acct_list:
        await cb.message.answer("ℹ️ هیچ اکانت فعالی برای این پنل پیدا نشد.")
        return

    client = make_panel_client(panel)
    renamed = 0
    skipped = 0
    failed = []
    try:
        await client.login()
        for aid, old_email, tg_id in acct_list:
            try:
                base = build_srv_name(cfg["prefix"], cfg["idtype"],
                                      bot_username, tg_id, None)
                new_email = f"{base}{gen_sub_id(3)}"
                if new_email == old_email:
                    skipped += 1
                    continue
                ok = await client.rename_account(old_email, new_email)
                if ok:
                    async with Session() as s:
                        a2 = await s.get(_PA, aid)
                        if a2:
                            a2.email = new_email
                            await s.commit()
                    renamed += 1
                else:
                    skipped += 1
            except PanelError as e:
                failed.append(f"{old_email}: {e}")
            except Exception as e:
                failed.append(f"{old_email}: {e}")
    except PanelError as e:
        await cb.message.answer(f"❌ خطا در اتصال به <b>{label}</b>:\n{e}")
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    lines = [
        f"✅ <b>اعمال نام سرور روی {label} تمام شد:</b>",
        f"• تغییر نام‌یافته: {renamed} اکانت از {len(acct_list)}",
    ]
    if skipped:
        lines.append(f"• بدون تغییر: {skipped}")
    if failed:
        lines.append(f"• ناموفق: {len(failed)}")
        for e in failed[:5]:
            lines.append(f"  — {e}")
        if len(failed) > 5:
            lines.append(f"  … و {len(failed) - 5} مورد دیگر")
    lines.append("\n💡 لینک ساب کاربران تغییری نکرده؛ فقط نام نمایشیِ سرور بروز شد.")
    await cb.message.answer("\n".join(lines))


# ─────────────── یادداشت فاکتور / تحویل کانفیگ (مخصوص هر پنل) ───────────────
# پیکربندی مشترک دو نوع یادداشت تا کد تکرار نشود.
_NOTE_META = {
    "invoice": {
        "cb": "noteinv",
        "fsm": "invoice",
        "title": "🧾 یادداشت در فاکتور محصول",
        "where": "این متن در «پیش‌فاکتور»، زیرِ قیمت، به خریدارِ این پنل نمایش داده می‌شود.",
    },
    "delivery": {
        "cb": "notedel",
        "fsm": "delivery",
        "title": "📦 یادداشت در توضیحات تحویل کانفیگ",
        "where": "این متن هنگام «تحویل کانفیگ» به کاربرِ این پنل نمایش داده می‌شود.",
    },
}


def _note_text(kind: str, panel_key: str, current: str) -> str:
    meta = _NOTE_META[kind]
    label = PANEL_LABELS.get(panel_key, panel_key)
    cur_disp = current if current else "— (تنظیم نشده)"
    return (
        f"{meta['title']} — {label}\n\n"
        f"{meta['where']}\n\n"
        f"متن فعلی:\n<blockquote>{cur_disp}</blockquote>"
    )


async def _note_kb(kind: str, panel_key: str, current: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    meta = _NOTE_META[kind]
    label = PANEL_LABELS.get(panel_key, panel_key)
    edit_label = "✏️ ویرایش متن" if current else "➕ افزودن متن"
    rows = [[InlineKeyboardButton(
        text=edit_label, callback_data=f"tvpn:{meta['cb']}_edit:{panel_key}",
        style=await _ibs(bot_id, "ibtn:vpn_custom"))]]
    if current:
        rows.append([InlineKeyboardButton(
            text="🗑 حذف متن", callback_data=f"tvpn:{meta['cb']}_del:{panel_key}",
            style=await _ibs(bot_id, "ibtn:vpn_delete"))])
    rows.append([InlineKeyboardButton(
        text=f"⬅️ بازگشت به {label}", callback_data=f"tvpn:panel:{panel_key}",
        style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _note_open(cb: CallbackQuery, bot: Bot, kind: str):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    if not is_panel_key(panel_key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    current = await get_panel_note(row.id, panel_key, kind)
    text = _note_text(kind, panel_key, current)
    kb = await _note_kb(kind, panel_key, current, row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:noteinv:"))
async def vpn_note_invoice(cb: CallbackQuery, bot: Bot):
    await _note_open(cb, bot, "invoice")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notedel:"))
async def vpn_note_delivery(cb: CallbackQuery, bot: Bot):
    await _note_open(cb, bot, "delivery")


async def _note_del(cb: CallbackQuery, bot: Bot, kind: str):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    await set_panel_note(row.id, panel_key, kind, "")
    text = _note_text(kind, panel_key, "")
    kb = await _note_kb(kind, panel_key, "", row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("✅ حذف شد.")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:noteinv_del:"))
async def vpn_note_invoice_del(cb: CallbackQuery, bot: Bot):
    await _note_del(cb, bot, "invoice")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notedel_del:"))
async def vpn_note_delivery_del(cb: CallbackQuery, bot: Bot):
    await _note_del(cb, bot, "delivery")


async def _note_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext, kind: str):
    panel_key = cb.data.split(":")[2]
    row = await _owner(cb, bot)
    if not row:
        return
    if not is_panel_key(panel_key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    fsm_state = VpnNoteFSM.invoice if kind == "invoice" else VpnNoteFSM.delivery
    await state.set_state(fsm_state)
    await state.update_data(note_panel_key=panel_key)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    meta = _NOTE_META[kind]
    await cb.message.answer(
        f"✏️ متنِ «{meta['title']}» را بفرست:\n"
        "(می‌توانی چند خط بنویسی. برای انصراف /cancel یا دکمهٔ لغو.)",
        reply_markup=cancel_kb())


@vpn_admin_router.callback_query(F.data.startswith("tvpn:noteinv_edit:"))
async def vpn_note_invoice_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _note_edit_start(cb, bot, state, "invoice")


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notedel_edit:"))
async def vpn_note_delivery_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _note_edit_start(cb, bot, state, "delivery")


async def _note_save(msg: Message, bot: Bot, state: FSMContext, kind: str):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    panel_key = data.get("note_panel_key", "")
    await state.clear()
    if not is_panel_key(panel_key):  # PATCH_MULTI_PANEL_INSTANCES_V1
        await msg.answer("پنل نامعتبر بود؛ دوباره از منوی پنل امتحان کن.")
        return
    value = (msg.text or "").strip()
    # محدودیت طول معقول برای جلوگیری از پیام‌های بیش‌ازحد بلند
    if len(value) > 700:
        value = value[:700]
    await set_panel_note(row.id, panel_key, kind, value)
    current = await get_panel_note(row.id, panel_key, kind)
    text = "✅ ذخیره شد.\n\n" + _note_text(kind, panel_key, current)
    kb = await _note_kb(kind, panel_key, current, row.id)
    await msg.answer(text, reply_markup=kb)


@vpn_admin_router.message(VpnNoteFSM.invoice, F.text)
async def vpn_note_invoice_save(msg: Message, bot: Bot, state: FSMContext):
    await _note_save(msg, bot, state, "invoice")


@vpn_admin_router.message(VpnNoteFSM.delivery, F.text)
async def vpn_note_delivery_save(msg: Message, bot: Bot, state: FSMContext):
    await _note_save(msg, bot, state, "delivery")


# ─────────────── تنظیمات تمدید (مخصوص هر پنل) ───────────────
async def _renew_kb(cfg: dict, key: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    enabled = cfg.get("renew_enabled", "1") == "1"
    toggle = "🟢 دکمه‌های تمدید: فعال" if enabled else "🔴 دکمه‌های تمدید: غیرفعال"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"tvpn:renew_toggle:{key}",
                              style=await _ibs(bot_id, "ibtn:vpn_toggle"))],
        [InlineKeyboardButton(text="📦 بازهٔ افزایش حجم (گیگ)", callback_data=f"tvpn:renew_gb:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📅 بازهٔ تمدید (روز)", callback_data=f"tvpn:renew_days:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💵 قیمت هر گیگ/روز تمدید", callback_data=f"tvpn:renew_price:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:panel:{key}",
                              style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)],
    ])


def _renew_text(cfg: dict, label: str = "") -> str:
    en = "✅ فعال" if cfg.get("renew_enabled", "1") == "1" else "❌ غیرفعال"
    title = f"♻️ <b>تنظیمات تمدید / افزایش حجم — {label}</b>\n\n" if label else "♻️ <b>تنظیمات تمدید / افزایش حجم</b>\n\n"
    return (
        title +
        f"وضعیت دکمه‌های تمدید: {en}\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 افزایش حجم: از {cfg['renew_min_gb']} تا {cfg['renew_max_gb']} گیگ (گام {cfg['renew_step_gb']})\n"
        f"📅 تمدید مدت: از {cfg['renew_min_days']} تا {cfg['renew_max_days']} روز (گام {cfg['renew_step_days']})\n"
        f"💵 قیمت هر گیگ: {int(cfg['renew_price_gb']):,} تومان\n"
        f"💵 قیمت هر روز: {int(cfg['renew_price_day']):,} تومان\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"🔔 اطلاع اتمام حجم: وقتی حجم باقی‌مانده به {cfg['notify_gb_threshold']} گیگ برسد\n"
        f"🔔 اطلاع اتمام مدت: وقتی {cfg['notify_days_threshold']} روز به پایان مانده باشد\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💡 این تنظیمات فقط برای همین پنل اعمال می‌شوند؛ هر پنل قیمت/بازهٔ مستقل خودش را دارد.\n"
        "برای تغییر هر مورد، روی دکمه‌اش بزن."
    )


@vpn_admin_router.callback_query(F.data.startswith("tvpn:renew:"))
async def vpn_renew(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    try:
        await cb.message.edit_text(_renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))
    except Exception:
        await cb.message.answer(_renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:renew_toggle:"))
async def vpn_renew_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    cur = await _vc_get(row.id, "renew_enabled", key)
    await _vc_set(row.id, "renew_enabled", "0" if cur == "1" else "1", key)
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    try:
        await cb.message.edit_text(_renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:renew_gb:"))
async def vpn_renew_gb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnRenewFSM.gb_range)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        f"بازهٔ افزایش حجم: «حداقل حداکثر گام»\n"
        f"مثال: <code>10 100 10</code>\n"
        f"فعلی: {cfg['renew_min_gb']} تا {cfg['renew_max_gb']} (گام {cfg['renew_step_gb']})",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnRenewFSM.gb_range, F.text)
async def vpn_renew_gb_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 3 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("سه عدد با فاصله بفرست:", reply_markup=cancel_kb())
        return
    mn, mx, st = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "renew_min_gb", str(mn), key)
    await _vc_set(row.id, "renew_max_gb", str(mx), key)
    await _vc_set(row.id, "renew_step_gb", str(st), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:renew_days:"))
async def vpn_renew_days(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnRenewFSM.days_range)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        f"بازهٔ تمدید روز: «حداقل حداکثر گام»\n"
        f"مثال: <code>7 90 7</code>\n"
        f"فعلی: {cfg['renew_min_days']} تا {cfg['renew_max_days']} (گام {cfg['renew_step_days']})",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnRenewFSM.days_range, F.text)
async def vpn_renew_days_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 3 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("سه عدد با فاصله بفرست:", reply_markup=cancel_kb())
        return
    mn, mx, st = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "renew_min_days", str(mn), key)
    await _vc_set(row.id, "renew_max_days", str(mx), key)
    await _vc_set(row.id, "renew_step_days", str(st), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:renew_price:"))
async def vpn_renew_price(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnRenewFSM.prices)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        f"قیمت تمدید (تومان): «قیمت‌گیگ قیمت‌روز»\n"
        f"مثال: <code>1000 500</code>\n"
        f"فعلی: {int(cfg['renew_price_gb']):,}/گیگ، {int(cfg['renew_price_day']):,}/روز",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnRenewFSM.prices, F.text)
async def vpn_renew_price_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    parts = msg.text.strip().split()
    if len(parts) != 2 or any(fa_to_int(p) is None for p in parts):
        await msg.answer("دو عدد با فاصله بفرست: قیمت‌گیگ قیمت‌روز", reply_markup=cancel_kb())
        return
    pg, pd = [fa_to_int(p) for p in parts]
    await _vc_set(row.id, "renew_price_gb", str(pg), key)
    await _vc_set(row.id, "renew_price_day", str(pd), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _renew_text(cfg, label), reply_markup=await _renew_kb(cfg, key, row.id))


# ─────────────── تنظیمات اطلاع از اتمام حجم/مدت (مخصوص هر پنل) ───────────────
async def _notify_kb(key: str, bot_id: int = 0) -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 آستانهٔ حجم باقی‌مانده (گیگ)", callback_data=f"tvpn:notify_gb:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📅 آستانهٔ روزهای باقی‌مانده", callback_data=f"tvpn:notify_days:{key}", style=ButtonStyle.PRIMARY)],
        # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
        [InlineKeyboardButton(text="🗑 مهلتِ حذف پس از اتمامِ حجم (ساعت)", callback_data=f"tvpn:notify_delhrs:{key}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"tvpn:panel:{key}",
                              style=await _ibs(bot_id, "ibtn:back") or ButtonStyle.DANGER)],
    ])


def _notify_text(cfg: dict, label: str = "") -> str:
    title = (f"🔔 <b>تنظیمات اطلاع از اتمام حجم — {label}</b>\n\n" if label
             else "🔔 <b>تنظیمات اطلاع از اتمام حجم</b>\n\n")
    return (
        title +
        "وقتی حجم یا مدت سرویس کاربر به این آستانه‌ها برسد، به او پیام هشدار "
        "ارسال می‌شود.\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 حجم باقی‌مانده: {cfg['notify_gb_threshold']} گیگ\n"
        f"📅 روزهای باقی‌مانده: {cfg['notify_days_threshold']} روز\n"
        # === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
        f"🗑 مهلتِ حذف پس از اتمامِ حجم: {cfg['traffic_delete_hours']} ساعت\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💡 این آستانه‌ها فقط برای همین پنل اعمال می‌شوند.\n"
        "برای تغییر هر مورد، روی دکمه‌اش بزن."
    )


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notify:"))
async def vpn_notify_settings(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    try:
        await cb.message.edit_text(_notify_text(cfg, label), reply_markup=await _notify_kb(key, row.id))
    except Exception:
        await cb.message.answer(_notify_text(cfg, label), reply_markup=await _notify_kb(key, row.id))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notify_gb:"))
async def vpn_notify_gb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnNotifyFSM.gb_threshold)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        "حجم باقی‌مانده (گیگ) که در آن به کاربر اطلاع داده شود بسته در حال اتمام است:\n"
        "یک عدد بفرست، مثال: <code>2</code>\n"
        f"فعلی: {cfg['notify_gb_threshold']} گیگ",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnNotifyFSM.gb_threshold, F.text)
async def vpn_notify_gb_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 2):", reply_markup=cancel_kb())
        return
    await _vc_set(row.id, "notify_gb_threshold", str(n), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _notify_text(cfg, label), reply_markup=await _notify_kb(key, row.id))


# === PATCH_TRAFFIC_DELETE_HOURS_BTN_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:notify_delhrs:"))
async def vpn_notify_delhrs(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnNotifyFSM.delete_hours)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        "چند ساعت بعدِ اتمامِ حجم (اگه کاربر تمدید/شارژ نکنه)، سرویس هم از "
        "پنل هم از «کانفیگ‌های من» حذف بشه؟\n"
        "یک عدد بفرست، مثال: <code>24</code>\n"
        f"فعلی: {cfg['traffic_delete_hours']} ساعت\n\n"
        "⚠️ حداقل ۰.۵ و حداکثر ۷۲۰ ساعت (۳۰ روز) قابلِ ثبته.",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnNotifyFSM.delete_hours, F.text)
async def vpn_notify_delhrs_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    raw = (msg.text or "").strip()
    _trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    normalized = raw.translate(_trans).replace(",", "").replace("،", "").strip()
    try:
        n = float(normalized)
    except (TypeError, ValueError):
        n = None
    if n is None or n < 0.5 or n > 720:
        await msg.answer("یک عددِ معتبر بینِ ۰.۵ تا ۷۲۰ بفرست (مثلاً 24):", reply_markup=cancel_kb())
        return
    await _vc_set(row.id, "traffic_delete_hours", (str(int(n)) if n == int(n) else str(n)), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _notify_text(cfg, label), reply_markup=await _notify_kb(key, row.id))


@vpn_admin_router.callback_query(F.data.startswith("tvpn:notify_days:"))
async def vpn_notify_days(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.set_state(VpnNotifyFSM.days_threshold)
    await state.update_data(panel_key=key)
    cfg = await _vc_all(row.id, key)
    await cb.message.answer(
        "چند روز مانده به پایان بسته، به کاربر اطلاع داده شود بسته در حال اتمام است:\n"
        "یک عدد بفرست، مثال: <code>5</code>\n"
        f"فعلی: {cfg['notify_days_threshold']} روز",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnNotifyFSM.days_threshold, F.text)
async def vpn_notify_days_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    key = data.get("panel_key", "")
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("یک عدد معتبر بفرست (مثلاً 5):", reply_markup=cancel_kb())
        return
    await _vc_set(row.id, "notify_days_threshold", str(n), key)
    await state.clear()
    cfg = await _vc_all(row.id, key)
    label = PANEL_LABELS.get(key, key)
    await msg.answer("✅ ذخیره شد.\n\n" + _notify_text(cfg, label), reply_markup=await _notify_kb(key, row.id))


# ─────────────── پلن‌های فروش (مخصوص هر پنل) ───────────────
@vpn_admin_router.callback_query(F.data.startswith("tvpn:plans:"))
async def vpn_plans(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    label = PANEL_LABELS.get(key, key)
    from tenant import ibtn_style as _ibs
    async with Session() as s:
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == row.id, VpnPlan.panel_key == key))).all()
    plans = _manual_sorted(plans)      # همان ترتیبی که مشتری می‌بیند
    rows_kb = []
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    _groups: dict[str | None, list] = {}
    _order: list[str | None] = []
    for p in plans:
        cat = (p.category or "").strip() or None
        if cat not in _groups:
            _groups[cat] = []
            _order.append(cat)
        _groups[cat].append(p)
    for cat in _order:
        if cat:
            # === PATCH_VPN_PLAN_CATEGORY_RENAME_V1 ===
            # قبلاً این ردیف فقط یک متنِ تزئینی بود (callback_data="noop")؛
            # حالا با زدنش می‌شود اسمِ دسته را عوض کرد. اگر اسمِ دسته آن‌قدر
            # بلند باشد که callback_data از سقفِ ۶۴بایتیِ تلگرام رد شود، به‌جایِ
            # خودِ اسم از ایندیکسِ آن در همین لیست استفاده می‌کنیم — امن و
            # بدونِ خطر برای دسته‌هایِ فارسیِ طولانی.
            _cat_idx = [c for c in _order if c].index(cat)
            _cb = f"tvpn:catmenu:{key}:{_cat_idx}"
            rows_kb.append([InlineKeyboardButton(
                text=f"🏷 ── {cat} ── (ویرایش / ترتیب)", callback_data=_cb)])
        for p in _groups[cat]:
            vol = "نامحدود" if p.volume_gb == 0 else f"{p.volume_gb}G"
            dur = "بی‌انقضا" if p.days == 0 else f"{p.days}d"
            status = "🟢" if p.active else "🔴"
            rows_kb.append([InlineKeyboardButton(
                text=f"{status} {p.title} ({vol}/{dur}/{rial(p.price)})",
                callback_data=f"tvpn:planmenu:{key}:{p.id}")])
    rows_kb.append([InlineKeyboardButton(text="➕ افزودن پلن جدید", callback_data=f"tvpn:planadd:{key}",
                                         style=await _ibs(row.id, "ibtn:vpn_setup"))])
    # === PATCH_PLAN_TABLE_VIEW_V1 === حالتِ نمایشِ پلن‌ها به مشتری (فقط پلنِ حرفه‌ای)
    from plans import has_feature as _hf, Feature as _Ft
    from tenant import get_setting as _gset
    if _hf(row.plan, _Ft.PLAN_TABLE_VIEW):
        _pv = (await _gset(row.id, f"plan_view_{key}", "list")) == "table"
        rows_kb.append([InlineKeyboardButton(
            text="🖼 حالتِ نمایشِ پلن‌ها: " + ("📊 جدولی (حجم | زمان | قیمت)" if _pv else "📋 لیستی"),
            callback_data=f"tvpn:planview:{key}", style=ButtonStyle.PRIMARY)])
        if _pv:
            # === PATCH_PLAN_TABLE_USERS_TOGGLE_V1 === فقط وقتی حالتِ جدولی فعال است
            _pu = (await _gset(row.id, f"plan_users_{key}", "1")) != "0"
            rows_kb.append([InlineKeyboardButton(
                text="👥 نمایشِ تعدادِ کاربر در جدول: " + ("✅ روشن" if _pu else "⛔️ خاموش"),
                callback_data=f"tvpn:planusers:{key}",
                style=ButtonStyle.SUCCESS if _pu else ButtonStyle.DANGER)])
    else:
        rows_kb.append([InlineKeyboardButton(text="🔒 نمایشِ جدولیِ پلن‌ها (ویژهٔ حرفه‌ای)",
                                             callback_data=f"tvpn:planview:{key}")])
    rows_kb.append([InlineKeyboardButton(text=f"⬅️ بازگشت به {label}", callback_data=f"tvpn:panel:{key}", style=ButtonStyle.DANGER)])
    text = (
        f"📦 <b>پلن‌های فروش — {label}</b>\n\n"
        "روی هر پلن بزن تا منوی کاملِ ویرایش/حذف همان پلن باز شود:"
        if plans else
        f"📦 <b>پلن‌های فروش — {label}</b>\n\nهنوز پلنی برای این پنل ثبت نشده."
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))
    await cb.answer()


# === PATCH_PLAN_TABLE_VIEW_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:planview:"))
async def vpn_plan_view_toggle(cb: CallbackQuery, bot: Bot):
    """جابه‌جایی بینِ نمایشِ «لیستی» و «جدولیِ» پلن‌ها برایِ مشتری (هر پنل جدا). فقط پلنِ حرفه‌ای."""
    row = await _owner(cb, bot)
    if not row:
        return
    from plans import has_feature as _hf, Feature as _Ft
    if not _hf(row.plan, _Ft.PLAN_TABLE_VIEW):
        await cb.answer("🔒 نمایشِ جدولیِ پلن‌ها فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    from tenant import get_setting as _gset, set_setting as _sset
    cur = (await _gset(row.id, f"plan_view_{key}", "list")) == "table"
    await _sset(row.id, f"plan_view_{key}", "list" if cur else "table")
    await vpn_plans(cb.model_copy(update={"data": f"tvpn:plans:{key}"}), bot)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:planusers:"))
async def vpn_plan_users_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش‌کردنِ نمایشِ «تک‌کاربره/۲ کاربره» در حالتِ جدولی (هر پنل جدا)."""
    row = await _owner(cb, bot)
    if not row:
        return
    from plans import has_feature as _hf, Feature as _Ft
    if not _hf(row.plan, _Ft.PLAN_TABLE_VIEW):
        await cb.answer("🔒 فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    from tenant import get_setting as _gset, set_setting as _sset
    cur = (await _gset(row.id, f"plan_users_{key}", "1")) != "0"
    await _sset(row.id, f"plan_users_{key}", "0" if cur else "1")
    await vpn_plans(cb.model_copy(update={"data": f"tvpn:plans:{key}"}), bot)


# ═══════════════════════════════════════════
#  تغییرِ نامِ دستهٔ پلن — PATCH_VPN_PLAN_CATEGORY_RENAME_V1
# ═══════════════════════════════════════════

async def _panel_categories(bot_id: int, key: str) -> list[str]:
    """فهرستِ مرتب‌شدهٔ دسته‌هایِ همین پنل — دقیقاً همان ترتیبی که در
    صفحهٔ «پلن‌های فروش» نمایش داده می‌شود، برایِ حلِ ایندیکس‌هایِ
    callback_dataیِ کوتاه‌شده."""
    async with Session() as s:
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.panel_key == key).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    order: list[str] = []
    for p in plans:
        cat = (p.category or "").strip()
        if cat and cat not in order:
            order.append(cat)
    return order


async def _ask_cat_rename(cb: CallbackQuery, bot: Bot, state: FSMContext, key: str, cat: str) -> None:
    from tenant import smart_cancel_kb  # lazy: جلوگیری از ایمپورتِ حلقوی
    await state.set_state(VpnCatRenameFSM.name)
    await state.update_data(cat_panel_key=key, cat_old_name=cat)
    await cb.message.answer(
        f"✏️ اسمِ تازه برایِ دستهٔ «{cat}» را بفرست.\n\n"
        "همهٔ پلن‌هایِ همین پنل که زیرِ این دسته هستند، با اسمِ تازه به‌روز "
        "می‌شوند.", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:catrenameidx:"))
async def vpn_cat_rename_idx(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """نسخهٔ ایندیکس‌محور — برایِ دسته‌هایِ فارسیِ طولانی که در خودِ
    callback_data جا نمی‌شدند."""
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, idx = cb.data.split(":")
    names = await _panel_categories(row.id, key)
    idx = int(idx)
    if idx >= len(names):
        await cb.answer("این دسته دیگر وجود ندارد.", show_alert=True)
        return
    await _ask_cat_rename(cb, bot, state, key, names[idx])


@vpn_admin_router.callback_query(F.data.startswith("tvpn:catrename:"))
async def vpn_cat_rename_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, cat = cb.data.split(":", 3)
    await _ask_cat_rename(cb, bot, state, key, cat)


@vpn_admin_router.message(VpnCatRenameFSM.name, F.text)
async def vpn_cat_rename_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    data = await state.get_data()
    key = data.get("cat_panel_key")
    old_name = data.get("cat_old_name")
    if not row or not key or not old_name:
        await state.clear()
        await msg.answer("مشکلی پیش آمد؛ دوباره از منویِ پلن‌ها امتحان کن.")
        return
    new_name = (msg.text or "").strip()
    if not new_name:
        await msg.answer("❌ اسمِ خالی قابلِ‌قبول نیست؛ یک اسمِ دیگر بفرست:")
        return
    # ستونِ VpnPlan.category در دیتابیس VARCHAR(30) است و بقیهٔ مسیرهایِ
    # ساختِ دسته هم حداکثر ۲۰ کاراکتر می‌پذیرند. قبلاً این‌جا تا ۶۰ کاراکتر
    # بریده می‌شد و commit با «value too long for type character varying(30)»
    # می‌ترکید (در error.log دیده شد) — ادمین هم هیچ پیامی نمی‌گرفت.
    if len(new_name) > 20:
        await msg.answer("⚠️ نامِ دسته بیش از حد بلنده؛ حداکثر ۲۰ کاراکتر بفرست:")
        return
    await state.clear()
    if new_name == old_name:
        await msg.answer("همان اسمِ قبلی است؛ چیزی تغییر نکرد.")
    else:
        async with Session() as s:
            plans = (await s.scalars(select(VpnPlan).where(
                VpnPlan.bot_id == row.id, VpnPlan.panel_key == key,
                VpnPlan.category == old_name))).all()
            n = len(plans)
            for p in plans:
                p.category = new_name
            await s.commit()
        await msg.answer(f"✅ اسمِ دسته از «{old_name}» به «{new_name}» تغییر کرد "
                         f"({n} پلن به‌روز شد).")
    fake_cb = type("CB", (), {
        "data": f"tvpn:plans:{key}", "answer": (lambda *a, **k: _noop_answer()),
        "message": msg, "from_user": msg.from_user,
    })()
    await vpn_plans(fake_cb, bot)


async def _noop_answer(*a, **k):
    return None


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plantoggle:"))
async def vpn_plan_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, plan_id = cb.data.split(":")
    new_active = False
    async with Session() as s:
        p = await s.get(VpnPlan, int(plan_id))
        if p and p.bot_id == row.id:
            p.active = not p.active
            new_active = p.active
            await s.commit()
    await cb.answer("✅ فعال شد" if new_active else "غیرفعال شد")
    fake_cb = type("CB", (), {
        "data": f"tvpn:plans:{key}", "answer": cb.answer,
        "message": cb.message, "from_user": cb.from_user
    })()
    await vpn_plans(fake_cb, bot)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plandel:"))
async def vpn_plan_del(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, plan_id = cb.data.split(":")
    async with Session() as s:
        p = await s.get(VpnPlan, int(plan_id))
        if p and p.bot_id == row.id:
            await s.delete(p)
            await s.commit()
    await cb.answer("پلن حذف شد")
    fake_cb = type("CB", (), {
        "data": f"tvpn:plans:{key}", "answer": cb.answer,
        "message": cb.message, "from_user": cb.from_user
    })()
    await vpn_plans(fake_cb, bot)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:planadd:"))
async def vpn_plan_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":")[2]
    await state.update_data(plan_panel_key=key)
    await state.set_state(VpnPlanFSM.title)
    await cb.message.answer(
        "عنوان پلن را بفرست (مثل «پلن اقتصادی» یا «ویژه»):\n\n"
        "💡 حجم/مدت/قیمت را خودت جدا وارد می‌کنی و بات به‌صورت خودکار به عنوان اضافه می‌کند؛ "
        "پس لازم نیست تو همین عنوان تکرارشون کنی. عنوان را کوتاه (حداکثر ۲۰ کاراکتر) بفرست "
        "تا دکمهٔ فروشگاه فشرده/بریده نشود.",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnPlanFSM.title, F.text)
async def vpn_plan_title(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    title = msg.text.strip()
    if len(title) > 20:
        await msg.answer(
            "⚠️ عنوان بیش از حد بلند است و روی دکمهٔ فروشگاه فشرده/بریده می‌شود.\n"
            "لطفاً عنوانِ کوتاه‌تری (حداکثر ۲۰ کاراکتر) بفرست:",
            reply_markup=cancel_kb())
        return
    await state.update_data(title=title)
    await state.set_state(VpnPlanFSM.gb)
    await msg.answer("حجم (گیگ) را بفرست (0 = نامحدود):", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPlanFSM.gb, F.text)
async def vpn_plan_gb(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("عدد معتبر بفرست:", reply_markup=cancel_kb())
        return
    await state.update_data(gb=n)
    await state.set_state(VpnPlanFSM.days)
    await msg.answer("مدت (روز) را بفرست (0 = بی‌انقضا):", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPlanFSM.days, F.text)
async def vpn_plan_days(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("عدد معتبر بفرست:", reply_markup=cancel_kb())
        return
    await state.update_data(days=n)
    await state.set_state(VpnPlanFSM.price)
    await msg.answer("قیمت (تومان) را بفرست:", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPlanFSM.price, F.text)
async def vpn_plan_price(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("عدد معتبر بفرست:", reply_markup=cancel_kb())
        return
    await state.update_data(price=n)
    await state.set_state(VpnPlanFSM.limit_ip)
    await msg.answer("محدودیت IP همزمان را بفرست (0 = بدون محدودیت):", reply_markup=cancel_kb())


@vpn_admin_router.message(VpnPlanFSM.limit_ip, F.text)
async def vpn_plan_limit_ip(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n < 0:
        await msg.answer("عدد معتبر بفرست:", reply_markup=cancel_kb())
        return
    await state.update_data(limit_ip=n)
    await state.set_state(VpnPlanFSM.category)
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    # دسته‌بندی‌هایِ موجودِ این ربات رو به‌صورتِ دکمه نشون می‌دیم تا با یه تپ
    # انتخاب بشه (دقیقاً همون الگویِ «انبار کانفیگ» که قبلاً درست شد) —
    # پلن‌هایی که دسته‌بندیِ یکسان دارن، تو لیستِ خرید زیرِ یک تیترِ مشترک
    # کنارِ هم می‌شینن (مثلاً «پلن حرفه‌ای» / «پلن اقتصادی»).
    async with Session() as s:
        existing = (await s.scalars(
            select(VpnPlan.category).where(
                VpnPlan.bot_id == row.id, VpnPlan.category.is_not(None))
            .distinct().order_by(VpnPlan.category)
        )).all()
# === PATCH_PLANCAT_CBDATA_LEN_V1 ===
    # همان مشکلِ طولِ callback_data در مرحلهٔ ساخت هم بود.
    _cats = [c for c in existing if c]
    kb_rows = [[InlineKeyboardButton(text=f"🏷 {cat}",
                                     callback_data=f"tvpn:plancatpick:i{idx}",
                                     style=ButtonStyle.PRIMARY)]
               for idx, cat in enumerate(_cats)]
    kb_rows.append([InlineKeyboardButton(text="🚫 بدون دسته‌بندی", callback_data="tvpn:plancatpick:-", style=ButtonStyle.PRIMARY)])
    await msg.answer(
        "🏷 این پلن تویِ کدوم دسته‌بندی باشه؟ یکی از دسته‌های موجود رو انتخاب کن، یا "
        "نامِ دستهٔ جدید رو بفرست (حداکثر ۲۰ کاراکتر؛ مثلاً «پلن حرفه‌ای»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


async def _finalize_vpn_plan(bot: Bot, chat_id: int, state: FSMContext, row, category: str | None) -> None:
    data = await state.get_data()
    key = data.get("plan_panel_key", "sanai")
    # === PATCH_PLAN_FINALIZE_DOUBLETAP_V1 === لاگِ سرور: KeyError 'title' — تپِ دوباره/هم‌زمانِ دکمهٔ دسته: اولی state را پاک
    # کرده و دومی داده‌ای ندارد. پلنِ تکراری/ناقص ساخته نمی‌شود و خطا هم نمی‌افتد.
    if not all(k in data for k in ("title", "gb", "days", "price")):
        logger.info("vpn plan finalize: state خالی/تکراری (تپِ دوباره) — نادیده گرفته شد")
        return
    await state.clear()
    async with Session() as s:
        s.add(VpnPlan(
            bot_id=row.id, panel_key=key, title=data["title"],
            volume_gb=data["gb"], days=data["days"], price=data["price"],
            limit_ip=data.get("limit_ip", 0), active=True, category=category))
        await s.commit()
    label = PANEL_LABELS.get(key, key)
    cat_line = f"🏷 دسته‌بندی: {category}\n" if category else ""
    await bot.send_message(
        chat_id,
        f"✅ پلن «{data['title']}» برای {label} ذخیره شد.\n\n"
        f"حجم: {data['gb']} گیگ | مدت: {data['days']} روز | قیمت: {rial(data['price'])}\n{cat_line}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 پلن‌های فروش", callback_data=f"tvpn:plans:{key}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⬅️ منوی VPN", callback_data="tvpn:menu", style=ButtonStyle.DANGER)],
        ]))


@vpn_admin_router.callback_query(VpnPlanFSM.category, F.data.startswith("tvpn:plancatpick:"))
async def vpn_plan_category_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    raw = cb.data.split(":", 2)[2]
    # === PATCH_PLANCAT_CBDATA_LEN_V1 ===
    if raw == "-":
        category = None
    elif raw.startswith("i"):
        async with Session() as s:
            _existing = (await s.scalars(
                select(VpnPlan.category).where(
                    VpnPlan.bot_id == row.id, VpnPlan.category.is_not(None))
                .distinct().order_by(VpnPlan.category)
            )).all()
        _cats = [c for c in _existing if c]
        try:
            category = _cats[int(raw[1:])]
        except (ValueError, IndexError):
            category = None
    else:
        category = raw
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _finalize_vpn_plan(bot, cb.message.chat.id, state, row, category)
    await cb.answer()


@vpn_admin_router.message(VpnPlanFSM.category, F.text)
async def vpn_plan_category_text(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    if raw in ("-", "بدون دسته", "حذف"):
        category = None
    else:
        if len(raw) > 20:
            await msg.answer("⚠️ نامِ دسته بیش از حد بلنده؛ حداکثر ۲۰ کاراکتر بفرست:")
            return
        category = raw
    await _finalize_vpn_plan(bot, msg.chat.id, state, row, category)


# ─────────────── جستجوی کانفیگ با کد سرویس (مدیریت) ───────────────
async def _code_acct_text_kb(a) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.vpn.vpn_user import _svc_label, _jalali
    title = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)
    # === PATCH_ADMIN_GB_MB_SERVICE_V1 === سرویسِ زیرِ ۱ گیگ نامحدود نیست
    if a.volume_mb and not (a.volume_gb or 0):
        vol = f"{a.volume_mb} مگابایت"
    else:
        vol = "نامحدود" if not a.volume_gb else f"{a.volume_gb} گیگ"
    dur = "بی‌انقضا" if not a.days else f"{a.days} روز"
    status = "🟢 فعال" if (a.status or "active") == "active" else "🔴 غیرفعال"
    text = (
        "🔍 <b>نتیجهٔ جستجوی کانفیگ</b>\n\n"
        f"💎 نام سرویس: <b>{title}</b>\n"
        f"🗂 کد سرویس: <code>{a.svc_code}</code>\n"
        f"👤 آیدی کاربر: <code>{a.user_telegram_id}</code>\n"
        f"📧 شناسه در پنل: <code>{a.email}</code>\n"
        f"🛰 پنل: {a.panel_key or '—'}\n"
        f"📦 حجم خریداری‌شده: {vol} | 📅 مدت: {dur}\n"
        f"⏳ انقضا (ثبت محلی): {_jalali(a.expires_at)}\n"
        f"وضعیت: {status}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 افزودن حجم", callback_data=f"tvpn:cf:addgb:{a.id}", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="⏳ افزودن روز", callback_data=f"tvpn:cf:adddays:{a.id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ تغییر نام سرویس", callback_data=f"tvpn:cf:rename:{a.id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"tvpn:cf:del:{a.id}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="🔍 جستجوی کد دیگر", callback_data="tvpn:codesearch", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="⬅️ منوی VPN", callback_data="tvpn:menu", style=ButtonStyle.DANGER)],
    ])
    return text, kb


async def _cf_get(cb: CallbackQuery, bot: Bot, aid: int):
    """گرفتن (row, account) با بررسی مالکیت — یا None."""
    row = await _owner(cb, bot)
    if not row:
        return None, None
    from models import PanelAccount as _PA
    async with Session() as s:
        a = await s.get(_PA, aid)
    if not a or a.bot_id != row.id:
        await cb.answer("سرویس یافت نشد (شاید حذف شده).", show_alert=True)
        return row, None
    return row, a


@vpn_admin_router.callback_query(F.data == "tvpn:codesearch")
async def vpn_code_search_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnCodeSearchFSM.code)
    await cb.message.answer(
        "🔍 <b>جستجوی کانفیگ با کد سرویس</b>\n\n"
        "کد ۹ رقمی سرویس را بفرست (همان کدی که در «کانفیگ‌های من» کاربر و "
        "پیام‌های هشدار نمایش داده می‌شود):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ انصراف", callback_data="tvpn:cancel", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@vpn_admin_router.message(VpnCodeSearchFSM.code, F.text)
async def vpn_code_search_do(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    code = _fa_num(msg.text.strip())
    code = re.sub(r"\D", "", code)
    if not code:
        await msg.answer("❗️ فقط رقم‌های کد را بفرست.")
        return
    from models import PanelAccount as _PA
    async with Session() as s:
        a = await s.scalar(select(_PA).where(
            _PA.bot_id == row.id, _PA.svc_code == code))
    if not a:
        await msg.answer(
            f"❌ سرویسی با کد <code>{code}</code> پیدا نشد.\n"
            "کد را دوباره بفرست یا انصراف بده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ انصراف", callback_data="tvpn:cancel", style=ButtonStyle.DANGER)]]))
        return
    await state.clear()
    text, kb = await _code_acct_text_kb(a)
    await msg.answer(text, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:cf:addgb:"))
async def vpn_cf_addgb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row, a = await _cf_get(cb, bot, int(cb.data.split(":")[3]))
    if not a:
        return
    await state.update_data(cf_aid=a.id)
    await state.set_state(VpnCodeSearchFSM.add_gb)
    await cb.message.answer("📈 چند <b>گیگ</b> به این سرویس اضافه شود؟ (عدد بفرست)")
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:cf:adddays:"))
async def vpn_cf_adddays(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row, a = await _cf_get(cb, bot, int(cb.data.split(":")[3]))
    if not a:
        return
    await state.update_data(cf_aid=a.id)
    await state.set_state(VpnCodeSearchFSM.add_days)
    await cb.message.answer("⏳ چند <b>روز</b> به این سرویس اضافه شود؟ (عدد بفرست)")
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:cf:rename:"))
async def vpn_cf_rename(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row, a = await _cf_get(cb, bot, int(cb.data.split(":")[3]))
    if not a:
        return
    await state.update_data(cf_aid=a.id)
    await state.set_state(VpnCodeSearchFSM.rename)
    await cb.message.answer("✏️ نام جدید سرویس را بفرست (فقط نام نمایشی عوض می‌شود):")
    await cb.answer()


async def _cf_apply_panel(row, a, *, add_gb: float = 0, add_days: int = 0) -> str | None:
    """اعمال تغییر روی پنل؛ در صورت خطا پیام خطا برمی‌گرداند."""
    from tenant.bots.vpn.vpn_user import _panel_for_account, _panel_client
    async with Session() as s:
        panel = await _panel_for_account(s, row.id, a)
    if not panel:
        return "پنل این سرویس یافت نشد یا غیرفعال است."
    client = await _panel_client(panel)
    try:
        if add_gb:
            await client.add_traffic(a.email, add_gb)
        if add_days:
            await client.renew(a.email, add_days)
        return None
    except PanelError as e:
        return str(e)
    except Exception as e:
        return str(e)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@vpn_admin_router.message(VpnCodeSearchFSM.add_gb, F.text)
async def vpn_cf_addgb_do(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    try:
        gb = float(_fa_num(msg.text.strip()))
        assert gb > 0
    except Exception:
        await msg.answer("❗️ یک عدد مثبت بفرست.")
        return
    data = await state.get_data()
    aid = int(data.get("cf_aid", 0))
    from models import PanelAccount as _PA
    async with Session() as s:
        a = await s.get(_PA, aid)
    if not a or a.bot_id != row.id:
        await state.clear()
        await msg.answer("❌ سرویس یافت نشد.")
        return
    err = await _cf_apply_panel(row, a, add_gb=gb)
    if err:
        await msg.answer(f"❌ خطا در اعمال روی پنل:\n<code>{_esc_html(str(err))}</code>")
        return
    async with Session() as s:
        a2 = await s.get(_PA, aid)
        if a2:
            a2.volume_gb = int((a2.volume_gb or 0) + gb)
            a2.traffic_out_at = None  # PATCH_TRAFFIC_RENEW_GUARD_V1 — حجم اضافه شد ⇒ چرخهٔ حذفِ «اتمامِ حجم» لغو
            a2.expiry_notice = None  # چرخهٔ هشدار از نو
            # === PATCH_EXPIRY_WARN_QUOTA_V1 === با تمدید، چرخهٔ اخطار از نو
            a2.expiry_warn_count = 0
            await s.commit()
            a = a2
    await state.clear()
    text, kb = await _code_acct_text_kb(a)
    await msg.answer(f"✅ {gb:g} گیگ اضافه شد.\n\n" + text, reply_markup=kb)


@vpn_admin_router.message(VpnCodeSearchFSM.add_days, F.text)
async def vpn_cf_adddays_do(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    try:
        d = int(_fa_num(msg.text.strip()))
        assert d > 0
    except Exception:
        await msg.answer("❗️ یک عدد صحیح مثبت بفرست.")
        return
    data = await state.get_data()
    aid = int(data.get("cf_aid", 0))
    from models import PanelAccount as _PA
    import datetime as _dt
    async with Session() as s:
        a = await s.get(_PA, aid)
    if not a or a.bot_id != row.id:
        await state.clear()
        await msg.answer("❌ سرویس یافت نشد.")
        return
    err = await _cf_apply_panel(row, a, add_days=d)
    if err:
        await msg.answer(f"❌ خطا در اعمال روی پنل:\n<code>{_esc_html(str(err))}</code>")
        return
    async with Session() as s:
        a2 = await s.get(_PA, aid)
        if a2:
            a2.days = int((a2.days or 0) + d)
            base = (a2.expires_at if a2.expires_at and a2.expires_at > _dt.datetime.utcnow()
                    else _dt.datetime.utcnow())
            a2.expires_at = base + _dt.timedelta(days=d)
            a2.expiry_notice = None
            # === PATCH_EXPIRY_WARN_QUOTA_V1 === با تمدید، چرخهٔ اخطار از نو
            a2.expiry_warn_count = 0
            await s.commit()
            a = a2
    await state.clear()
    text, kb = await _code_acct_text_kb(a)
    await msg.answer(f"✅ {d} روز اضافه شد.\n\n" + text, reply_markup=kb)


@vpn_admin_router.message(VpnCodeSearchFSM.rename, F.text)
async def vpn_cf_rename_do(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    name = msg.text.strip()[:80]
    if not name:
        await msg.answer("❗️ نام معتبر بفرست.")
        return
    data = await state.get_data()
    aid = int(data.get("cf_aid", 0))
    from models import PanelAccount as _PA
    async with Session() as s:
        a = await s.get(_PA, aid)
        if not a or a.bot_id != row.id:
            await state.clear()
            await msg.answer("❌ سرویس یافت نشد.")
            return
        a.plan_title = name
        await s.commit()
    await state.clear()
    text, kb = await _code_acct_text_kb(a)
    await msg.answer("✅ نام سرویس عوض شد.\n\n" + text, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:cf:del:"))
async def vpn_cf_del(cb: CallbackQuery, bot: Bot):
    row, a = await _cf_get(cb, bot, int(cb.data.split(":")[3]))
    if not a:
        return
    await cb.message.answer(
        f"🗑 حذف سرویس با کد <code>{a.svc_code}</code>؟\n"
        "این کار سرویس را از پنل و ربات <b>برای همیشه</b> حذف می‌کند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"tvpn:cf:delok:{a.id}",
                                  style=ButtonStyle.DANGER),
             InlineKeyboardButton(text="❌ انصراف", callback_data="tvpn:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:cf:delok:"))
async def vpn_cf_delok(cb: CallbackQuery, bot: Bot):
    row, a = await _cf_get(cb, bot, int(cb.data.split(":")[3]))
    if not a:
        return
    from tenant.bots.vpn.vpn_user import _panel_for_account, _panel_client
    async with Session() as s:
        panel = await _panel_for_account(s, row.id, a)
    if panel:
        client = await _panel_client(panel)
        try:
            await client.delete_account(a.email)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    from models import PanelAccount as _PA
    async with Session() as s:
        a2 = await s.get(_PA, a.id)
        if a2:
            await s.delete(a2)
            await s.commit()
    await cb.message.answer(f"✅ سرویس با کد <code>{a.svc_code}</code> حذف شد.")
    await cb.answer()


# ─────────────── منوی کامل هر پلن فروش (ویرایش/حذف) ───────────────
_PLAN_COLOR_LABEL = {"": "🟢 پیش‌فرض", "danger": "🔴 قرمز", "success": "🟢 سبز", "primary": "🔵 آبی"}


# === PATCH_RESELLER_PLANS_V1 ===
_AUDIENCE_LABEL = {"all": "همه", "users": "فقط مشتری", "resellers": "فقط نماینده"}
_AUDIENCE_NEXT = {"all": "users", "users": "resellers", "resellers": "all"}


def _plan_menu_view(p: VpnPlan, key: str) -> tuple[str, InlineKeyboardMarkup]:
    vol = "♾️ نامحدود" if p.volume_gb == 0 else f"{p.volume_gb} گیگ"
    dur = "بی‌انقضا" if p.days == 0 else f"{p.days} روز"
    users = "نامحدود" if not p.limit_ip else str(p.limit_ip)
    status = "🟢 فعال" if p.active else "🔴 غیرفعال"
    color_label = _PLAN_COLOR_LABEL.get((p.color or "").strip(), "🟢 پیش‌فرض")
    cat_label = p.category or "—"  # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    text = (
        f"📦 <b>پلن «{p.title}»</b>\n\n"
        f"📏 حجم: <b>{vol}</b>\n"
        f"📅 مدت: <b>{dur}</b>\n"
        f"💰 قیمت: <b>{rial(p.price)}</b>\n"
        f"👥 تعداد کاربر: <b>{users}</b>\n"
        f"🎨 رنگ دکمه: <b>{color_label}</b>\n"
        f"🏷 دسته‌بندی: <b>{cat_label}</b>\n"
        # === PATCH_RESELLER_PLANS_V1 ===
        f"👤 مخاطب: <b>{_AUDIENCE_LABEL.get(p.audience or 'all', 'همه')}</b>\n"
        f"وضعیت فروش: <b>{status}</b>\n\n"
        "روی هر فیلد بزن و مقدار جدید را بفرست:"
    )
    def _b(t, d, **kw):
        # === PATCH_PLANMENU_BUTTON_STYLE_DUPKW_FIX_V1 ===
        # قبلاً style همیشه هاردکد به PRIMARY پاس داده می‌شد؛ اگر خودِ فراخوان
        # هم style (مثلاً DANGER برای دکمهٔ حذف) می‌داد، پایتون با
        # "multiple values for keyword argument 'style'" کرش می‌کرد
        # (دقیقاً همون ERR-221257).
        kw.setdefault("style", ButtonStyle.PRIMARY)
        return InlineKeyboardButton(text=t, callback_data=d, **kw)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_b("✏️ عنوان", f"tvpn:planedit:{key}:title:{p.id}")],
        [_b(f"📏 حجم: {vol}", f"tvpn:planedit:{key}:gb:{p.id}"),
         _b(f"📅 مدت: {dur}", f"tvpn:planedit:{key}:days:{p.id}")],
        [_b(f"💰 قیمت: {rial(p.price)}", f"tvpn:planedit:{key}:price:{p.id}"),
         _b(f"👥 کاربر: {users}", f"tvpn:planedit:{key}:users:{p.id}")],
        [_b(f"🎨 رنگ: {color_label}", f"tvpn:plancolor:{key}:{p.id}"),
         _b(f"🏷 دسته: {cat_label}", f"tvpn:plancat:{key}:{p.id}")],
        # === PATCH_RESELLER_PLANS_V1 === چرخشی بینِ همه / مشتری / نماینده
        [_b(f"👤 مخاطب: {_AUDIENCE_LABEL.get(p.audience or 'all', 'همه')}",
            f"tvpn:planaud:{key}:{p.id}")],
        # === PATCH_PLANMENU_TOGGLE_STYLE_V1 ===
        # لیبل که عوض می‌شود، رنگ هم باید عوض شود: غیرفعال‌کردن = DANGER،
        # فعال‌کردن = SUCCESS. قبلاً هر دو حالت PRIMARY بود.
        [_b("🔴 غیرفعال‌کردن فروش" if p.active else "🟢 فعال‌کردن فروش",
            f"tvpn:plantg2:{key}:{p.id}",
            style=ButtonStyle.DANGER if p.active else ButtonStyle.SUCCESS)],
        # ترتیبِ نمایش به مشتری (بالا/پایین بردنِ پلن)
        [_b("🔼 بالا بردن", f"tvpn:planmv:{key}:{p.id}:up"),
         _b("🔽 پایین بردن", f"tvpn:planmv:{key}:{p.id}:dn")],
        [_b("🗑 حذف این پلن", f"tvpn:plandel2:{key}:{p.id}", style=ButtonStyle.DANGER)],
        [_b("⬅️ بازگشت به پلن‌ها", f"tvpn:plans:{key}", style=ButtonStyle.DANGER)],
    ])
    return text, kb


# === PATCH_VPN_PLAN_CATEGORY_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:plancat:"))
async def vpn_plan_category_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    async with Session() as s:
        existing = (await s.scalars(
            select(VpnPlan.category).where(
                VpnPlan.bot_id == row.id, VpnPlan.category.is_not(None))
            .distinct().order_by(VpnPlan.category)
        )).all()
    def _b(t, d, **kw):
        # === PATCH_PLANMENU_BUTTON_STYLE_DUPKW_FIX_V1 ===
        # قبلاً style همیشه هاردکد به PRIMARY پاس داده می‌شد؛ اگر خودِ فراخوان
        # هم style (مثلاً DANGER برای دکمهٔ حذف) می‌داد، پایتون با
        # "multiple values for keyword argument 'style'" کرش می‌کرد
        # (دقیقاً همون ERR-221257).
        kw.setdefault("style", ButtonStyle.PRIMARY)
        return InlineKeyboardButton(text=t, callback_data=d, **kw)
    cur = (p.category or "").strip()
    rows = []
    # === PATCH_PLANCAT_CBDATA_LEN_V1 ===
    _cats = [c for c in existing if c]
    for idx, cat in enumerate(_cats):
        mark = "✅ " if cur == cat else ""
# === PATCH_PLANCAT_CBDATA_LEN_V1 ===
        # نامِ دسته دیگر داخلِ callback_data نمی‌رود: تلگرام سقفِ ۶۴ بایت
        # دارد و هر حرفِ فارسی ۲ بایت است، پس نامِ ۲۰ کاراکتری با پیشوند
        # از سقف رد می‌شد و تلگرام BUTTON_DATA_INVALID می‌داد (ERR-142705).
        # حالا فقط «اندیس» می‌رود و سمتِ سرور از همان کوئریِ مرتب ترجمه
        # می‌شود.
        rows.append([_b(f"{mark}🏷 {cat}", f"tvpn:plancatset:{key}:{p.id}:i{idx}")])
    rows.append([_b(("✅ " if not cur else "") + "🚫 بدون دسته‌بندی", f"tvpn:plancatset:{key}:{p.id}:-")])
    rows.append([_b("⬅️ بازگشت", f"tvpn:planmenu:{key}:{p.id}")])
    await state.set_state(VpnPlanEditFSM.value)
    await state.update_data(pe_field="category", pe_plan_id=p.id, pe_key=key)
    await cb.message.edit_text(
        f"🏷 دسته‌بندیِ پلن «{p.title}» را انتخاب کن، یا نامِ دستهٔ جدید را مستقیم بفرست "
        "(حداکثر ۲۰ کاراکتر):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plancatset:"))
async def vpn_plan_category_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":", 4)
    key, plan_id, cat_raw = parts[2], parts[3], parts[4]
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    await state.clear()
    # === PATCH_PLANCAT_CBDATA_LEN_V1 ===
    if cat_raw == "-":
        category = None
    elif cat_raw.startswith("i"):
        async with Session() as s:
            _existing = (await s.scalars(
                select(VpnPlan.category).where(
                    VpnPlan.bot_id == row.id, VpnPlan.category.is_not(None))
                .distinct().order_by(VpnPlan.category)
            )).all()
        _cats = [c for c in _existing if c]
        try:
            category = _cats[int(cat_raw[1:])]
        except (ValueError, IndexError):
            await cb.answer("این دسته دیگر موجود نیست؛ دوباره انتخاب کن.",
                            show_alert=True)
            return
    else:
        category = cat_raw
    async with Session() as s:
        p2 = await s.get(VpnPlan, p.id)
        if p2:
            p2.category = category
            await s.commit()
            p = p2
    await cb.answer("✅ دسته‌بندی ذخیره شد")
    text, kb = _plan_menu_view(p, key)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plancolor:"))
async def vpn_plan_color_menu(cb: CallbackQuery, bot: Bot):
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    cur = (p.color or "").strip()
    def _b(t, d, **kw):
        # === PATCH_PLANMENU_BUTTON_STYLE_DUPKW_FIX_V1 ===
        # قبلاً style همیشه هاردکد به PRIMARY پاس داده می‌شد؛ اگر خودِ فراخوان
        # هم style (مثلاً DANGER برای دکمهٔ حذف) می‌داد، پایتون با
        # "multiple values for keyword argument 'style'" کرش می‌کرد
        # (دقیقاً همون ERR-221257).
        kw.setdefault("style", ButtonStyle.PRIMARY)
        return InlineKeyboardButton(text=t, callback_data=d, **kw)
    rows = []
    for cval, clabel in _PLAN_COLOR_LABEL.items():
        mark = "✅ " if cur == cval else ""
        rows.append([_b(f"{mark}{clabel}", f"tvpn:plancolorset:{key}:{p.id}:{cval or 'default'}")])
    rows.append([_b("⬅️ بازگشت", f"tvpn:planmenu:{key}:{p.id}")])
    await cb.message.edit_text(
        f"🎨 رنگِ دکمهٔ خرید برای پلن «{p.title}» را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plancolorset:"))
async def vpn_plan_color_set(cb: CallbackQuery, bot: Bot):
    _, _, key, plan_id, cval = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    cval = "" if cval == "default" else cval
    if cval not in _PLAN_COLOR_LABEL:
        await cb.answer("نامعتبر", show_alert=True)
        return
    async with Session() as s:
        p2 = await s.get(VpnPlan, p.id)
        if p2:
            p2.color = cval
            await s.commit()
            p = p2
    await cb.answer("✅ رنگ ذخیره شد")
    text, kb = _plan_menu_view(p, key)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


async def _plan_get(cb: CallbackQuery, bot: Bot, plan_id: int) -> "tuple[BotRow | None, VpnPlan | None]":
    row = await _owner(cb, bot)
    if not row:
        return None, None
    async with Session() as s:
        p = await s.get(VpnPlan, plan_id)
    if not p or p.bot_id != row.id:
        await cb.answer("پلن یافت نشد (شاید حذف شده).", show_alert=True)
        return row, None
    return row, p


# === PATCH_RESELLER_PLANS_V1 ===
@vpn_admin_router.callback_query(F.data.startswith("tvpn:planaud:"))
async def vpn_plan_audience(cb: CallbackQuery, bot: Bot):
    """چرخشِ مخاطبِ پلن: همه ← فقط مشتری ← فقط نماینده.

    «فقط نماینده» یعنی این پلن را در فهرستِ خرید فقط نمایندگانِ فعال
    می‌بینند؛ اینطور می‌شود برایشان منو و قیمتِ جدا طراحی کرد، به‌جایِ
    اینکه فقط درصدِ تخفیف رویِ پلن‌هایِ مشتری بخورد.
    """
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    nxt = _AUDIENCE_NEXT.get(p.audience or "all", "users")
    async with Session() as s:
        p2 = await s.get(VpnPlan, p.id)
        if p2:
            p2.audience = nxt
            await s.commit()
            p = p2
    await cb.answer(f"مخاطب: {_AUDIENCE_LABEL.get(nxt, nxt)}")
    text, kb = _plan_menu_view(p, key)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:planmenu:"))
async def vpn_plan_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    await state.clear()
    text, kb = _plan_menu_view(p, key)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plantg2:"))
async def vpn_plan_toggle2(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    async with Session() as s:
        p2 = await s.get(VpnPlan, p.id)
        if p2:
            p2.active = not p2.active
            await s.commit()
            p = p2
    await cb.answer("✅ فعال شد" if p.active else "غیرفعال شد")
    text, kb = _plan_menu_view(p, key)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


def _manual_sorted(plans):
    """ترتیبِ دستیِ فعلی: sort_order>0 به ترتیب؛ پلن‌هایِ بدونِ جا (0) آخر؛ در حالتِ خودکار همان مرتب‌سازیِ حجم/مدت/قیمت."""
    from tenant.bots.vpn.vpn_user import plans_ordered
    return plans_ordered(plans)


async def move_plan(bot_id: int, panel_key: str, plan_id: int, direction: str) -> bool:
    """پلن را در لیستِ همین پنل یک پله بالا/پایین می‌برد (جابه‌جایی با همسایه‌ی همان دسته).
    ترتیبِ فعلی (همان چیزی که مشتری می‌بیند) اول به شمارهٔ ۱..n تثبیت می‌شود، پس بار اول هم
    دقیقاً از همان ترتیبِ دیده‌شده جابه‌جا می‌شود. True = جابه‌جا شد."""
    async with Session() as s:
        plans = _manual_sorted((await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id))).all())
        for i, x in enumerate(plans, 1):
            x.sort_order = i
        mine = [x for x in plans if x.panel_key == panel_key]
        idx = next((i for i, x in enumerate(mine) if x.id == plan_id), None)
        if idx is None:
            await s.commit()
            return False
        cat = (mine[idx].category or "").strip()
        j = idx - 1 if direction == "up" else idx + 1
        while 0 <= j < len(mine) and (mine[j].category or "").strip() != cat:
            j = j - 1 if direction == "up" else j + 1
        moved = 0 <= j < len(mine)
        if moved:
            mine[idx].sort_order, mine[j].sort_order = mine[j].sort_order, mine[idx].sort_order
        await s.commit()
    return moved


async def panel_category_order(bot_id: int, panel_key: str) -> list[str]:
    """اسمِ دسته‌هایِ این پنل به ترتیبِ نمایش به مشتری."""
    async with Session() as s:
        plans = _manual_sorted((await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.panel_key == panel_key))).all())
    out: list[str] = []
    for p in plans:
        c = (p.category or "").strip()
        if c and c not in out:
            out.append(c)
    return out


async def move_plan_category(bot_id: int, panel_key: str, category: str, direction: str) -> bool:
    """کلِ دسته (همهٔ پلن‌هایش) را یک پله بالا/پایین می‌برد؛ ترتیبِ داخلِ هر دسته حفظ می‌شود."""
    async with Session() as s:
        allp = _manual_sorted((await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id))).all())
        for i, x in enumerate(allp, 1):
            x.sort_order = i
        mine = [x for x in allp if x.panel_key == panel_key]
        groups: list[tuple[str, list]] = []
        for p in mine:
            c = (p.category or "").strip()
            g = next((g for g in groups if g[0] == c), None)
            if g is None:
                groups.append((c, [p]))
            else:
                g[1].append(p)
        names = [g[0] for g in groups]
        if category not in names:
            await s.commit()
            return False
        i = names.index(category)
        # «بدونِ دسته» ("") همیشه جدا از دسته‌هایِ نام‌دار حساب می‌شود و جابه‌جا نمی‌شود
        named = [k for k, n in enumerate(names) if n]
        pos = named.index(i)
        npos = pos - 1 if direction == "up" else pos + 1
        if not (0 <= npos < len(named)):
            await s.commit()
            return False
        j = named[npos]
        groups[i], groups[j] = groups[j], groups[i]
        slots = sorted(x.sort_order for x in mine)          # همان شماره‌ها را به ترتیبِ جدید دوباره پخش می‌کنیم
        k = 0
        for _, ps in groups:
            for p in ps:
                p.sort_order = slots[k]
                k += 1
        await s.commit()
    return True


@vpn_admin_router.callback_query(F.data.startswith("tvpn:catmenu:"))
async def vpn_cat_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, idx = cb.data.split(":")
    names = await panel_category_order(row.id, key)
    try:
        cat = names[int(idx)]
    except (ValueError, IndexError):
        await cb.answer("دسته پیدا نشد (شاید تغییر کرده).", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییرِ نامِ دسته", callback_data=f"tvpn:catrenameidx:{key}:{idx}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔼 بالا بردنِ دسته", callback_data=f"tvpn:catmv:{key}:{idx}:up",
                              style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="🔽 پایین بردنِ دسته", callback_data=f"tvpn:catmv:{key}:{idx}:dn",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت به پلن‌ها", callback_data=f"tvpn:plans:{key}",
                              style=ButtonStyle.DANGER)],
    ])
    pos = f"{int(idx) + 1} از {len(names)}"
    text = (f"🏷 <b>دستهٔ «{cat}»</b>\n\nجایگاه در لیستِ فروش: {pos}\n"
            "با 🔼/🔽 کلِ دسته (با همهٔ پلن‌هایش) را در لیستِ مشتری جابه‌جا کن؛ "
            "ترتیبِ پلن‌ها داخلِ دسته را از منویِ هر پلن عوض کن.")
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:catmv:"))
async def vpn_cat_move(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, key, idx, direction = cb.data.split(":")
    names = await panel_category_order(row.id, key)
    try:
        cat = names[int(idx)]
    except (ValueError, IndexError):
        await cb.answer("دسته پیدا نشد (شاید تغییر کرده).", show_alert=True)
        return
    moved = await move_plan_category(row.id, key, cat, direction)
    if not moved:
        await cb.answer("بالاتر/پایین‌تر از این جایی نیست.")
        return
    await cb.answer("✅ جابه‌جا شد")
    new_idx = (await panel_category_order(row.id, key)).index(cat)
    await vpn_cat_menu(cb.model_copy(update={"data": f"tvpn:catmenu:{key}:{new_idx}"}), bot)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:planmv:"))
async def vpn_plan_move(cb: CallbackQuery, bot: Bot):
    _, _, key, plan_id, direction = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    moved = await move_plan(row.id, key, p.id, direction)
    await cb.answer("✅ جابه‌جا شد" if moved else "بالاتر/پایین‌تر از این جایی نیست.")
    if moved:
        # در همان منویِ پلن بمان تا بشود چندبار پشتِ‌سرِهم جابه‌جا کرد
        async with Session() as s:
            p = await s.get(VpnPlan, p.id)
        text, kb = _plan_menu_view(p, key)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            logger.info("بازسازیِ منویِ پلن بعد از جابه‌جایی ممکن نشد")


_PLAN_EDIT_PROMPTS = {
    "title": "✏️ عنوان جدید پلن را بفرست:",
    "gb": "📏 حجم جدید را به <b>گیگ</b> بفرست (0 = نامحدود):",
    "days": "📅 مدت جدید را به <b>روز</b> بفرست (0 = بی‌انقضا):",
    "price": "💰 قیمت جدید را به <b>تومان</b> بفرست:",
    "users": "👥 تعداد کاربر مجاز (اتصال هم‌زمان) را بفرست (0 = نامحدود):",
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    "category": "🏷 نامِ دستهٔ جدید را بفرست (حداکثر ۲۰ کاراکتر؛ برایِ حذفِ دسته «-» بفرست):",
}


@vpn_admin_router.callback_query(F.data.startswith("tvpn:planedit:"))
async def vpn_plan_edit_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    _, _, key, field, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p or field not in _PLAN_EDIT_PROMPTS:
        return
    prompt = await cb.message.answer(
        _PLAN_EDIT_PROMPTS[field],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت / انصراف",
                                  callback_data=f"tvpn:planmenu:{key}:{p.id}", style=ButtonStyle.DANGER)]]))
    await state.set_state(VpnPlanEditFSM.value)
    await state.update_data(pe_field=field, pe_plan_id=p.id, pe_key=key,
                            pe_prompt_id=prompt.message_id,
                            pe_panel_chat=cb.message.chat.id,
                            pe_panel_id=cb.message.message_id)
    await cb.answer()


@vpn_admin_router.message(VpnPlanEditFSM.value, F.text)
async def vpn_plan_edit_value(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    field, plan_id, key = data.get("pe_field"), data.get("pe_plan_id"), data.get("pe_key")
    raw = (msg.text or "").strip()
    val = None
    if field == "title":
        if raw:
            if len(raw) > 20:
                await msg.answer(
                    "⚠️ عنوان بیش از حد بلند است و روی دکمهٔ فروشگاه فشرده/بریده می‌شود.\n"
                    "لطفاً عنوانِ کوتاه‌تری (حداکثر ۲۰ کاراکتر) بفرست:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ بازگشت / انصراف",
                                              callback_data=f"tvpn:planmenu:{key}:{plan_id}", style=ButtonStyle.DANGER)]]))
                return
            val = raw[:20]
    elif field == "category":
        # === PATCH_VPN_PLAN_CATEGORY_V1 ===
        if raw in ("-", "بدون دسته", "حذف"):
            val = ""  # رشتهٔ خالی = "معتبر ولی یعنی None" (زیرِ چک val is None جا نگیره)
        elif len(raw) > 20:
            await msg.answer("⚠️ نامِ دسته بیش از حد بلنده؛ حداکثر ۲۰ کاراکتر بفرست:")
            return
        else:
            val = raw
    else:
        n = fa_to_int(raw)
        if field in ("gb", "days", "users") and n is not None and n >= 0:
            val = n
        elif field == "price" and n is not None and n > 0:
            val = n
    if val is None:
        await msg.answer("❌ مقدار معتبر بفرست.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ بازگشت / انصراف",
                                                   callback_data=f"tvpn:planmenu:{key}:{plan_id}", style=ButtonStyle.DANGER)]]))
        return
    async with Session() as s:
        p = await s.get(VpnPlan, plan_id)
        if not p or p.bot_id != row.id:
            await state.clear()
            await msg.answer("❌ پلن یافت نشد.")
            return
        if field == "title":
            p.title = val
        elif field == "gb":
            p.volume_gb = val
        elif field == "days":
            p.days = val
        elif field == "price":
            p.price = val
        elif field == "users":
            p.limit_ip = val
        elif field == "category":  # === PATCH_VPN_PLAN_CATEGORY_V1 ===
            p.category = val or None
        await s.commit()
    # پاک‌کردن پرسش و پاسخ تا چت تمیز بماند
    for mid in (data.get("pe_prompt_id"), msg.message_id):
        if mid:
            try:
                await bot.delete_message(msg.chat.id, mid)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await state.clear()
    text, kb = _plan_menu_view(p, key)
    chat_id = data.get("pe_panel_chat") or msg.chat.id
    panel_id = data.get("pe_panel_id")
    try:
        if panel_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=panel_id,
                                        reply_markup=kb)
        else:
            await msg.answer(text, reply_markup=kb)
    except Exception:
        await msg.answer(text, reply_markup=kb)


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plandel2:"))
async def vpn_plan_del2(cb: CallbackQuery, bot: Bot):
    _, _, key, plan_id = cb.data.split(":")
    row, p = await _plan_get(cb, bot, int(plan_id))
    if not p:
        return
    await cb.message.edit_text(
        f"🗑 پلن «<b>{p.title}</b>» برای همیشه حذف شود؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن",
                                  callback_data=f"tvpn:plandelok:{key}:{p.id}",
                                  style=ButtonStyle.DANGER),
             InlineKeyboardButton(text="❌ انصراف",
                                  callback_data=f"tvpn:planmenu:{key}:{p.id}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("tvpn:plandelok:"))
async def vpn_plan_delok(cb: CallbackQuery, bot: Bot):
    _, _, key, plan_id = cb.data.split(":")
    row = await _owner(cb, bot)
    if not row:
        return
    async with Session() as s:
        p = await s.get(VpnPlan, int(plan_id))
        if p and p.bot_id == row.id:
            await s.delete(p)
            await s.commit()
    await cb.answer("پلن حذف شد")
    fake_cb = type("CB", (), {
        "data": f"tvpn:plans:{key}", "answer": cb.answer,
        "message": cb.message, "from_user": cb.from_user
    })()
    await vpn_plans(fake_cb, bot)


# ═══════════════════════════════════════════
# === PATCH_QR_BACKGROUND_V1 ===
# عکسِ دلخواه پشتِ بارکد: مالک یک عکس می‌فرستد و از آن به بعد هر QR ای که
# ربات می‌سازد (کانفیگ، ساب‌لینک، سرویس‌های من، …) رویِ همان عکس می‌نشیند.
# ═══════════════════════════════════════════

class QrBgFSM(StatesGroup):
    photo = State()


async def _qrbg_screen(bot_id: int):
    from tenant.shared.qr_brand import get_bg
    has = await get_bg(bot_id) is not None
    text = (
        "🖼 <b>عکسِ پشتِ بارکد</b>\n\n"
        f"وضعیت: {'✅ عکس ثبت شده' if has else '❌ عکسی ثبت نشده (بارکدِ ساده)'}\n\n"
        "یک عکس بفرست تا از این به بعد همهٔ بارکدهایِ ربات رویِ آن ساخته شوند؛ "
        "بارکد داخلِ یک کارتِ سفیدِ گِرد در وسطِ عکس می‌نشیند تا حتماً اسکن شود.\n\n"
        "💡 عکسِ مربعی بهترین نتیجه را می‌دهد؛ عکسِ غیرمربع از وسط برش می‌خورد."
    )
    rows = [[InlineKeyboardButton(text="🖼 آپلود / تعویضِ عکس",
                                  callback_data="qrbg:set",
                                  style=ButtonStyle.SUCCESS)]]
    if has:
        rows.append([InlineKeyboardButton(text="👁 پیش‌نمایش",
                                          callback_data="qrbg:preview",
                                          style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ عکس",
                                          callback_data="qrbg:del",
                                          style=ButtonStyle.DANGER)])
    # === PATCH_QR_BACKGROUND_V1 ===
    # «tadm:settings» همیشه منویِ شیشه‌ای می‌ساخت، پس در حالتِ کیبوردی بازگشت،
    # منو را اینلاین می‌کرد. «tadm:sec:settings» حالتِ فعلیِ منو را رعایت می‌کند.
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings",
                                      style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@vpn_admin_router.callback_query(F.data == "qrbg:menu")
async def qrbg_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    # === PATCH_PLAN_FEATURE_GATING_V1 ===
    # فقط پنهان‌کردنِ دکمه از منو کافی نیست — کسی که پیامِ قدیمی/کش‌شده
    # دارد یا مستقیم رویِ callback_data تپ می‌زند نباید بتواند دور بزند.
    if not has_feature(row.plan, Feature.QR_BACKGROUND):
        await cb.answer("🔒 این قابلیت در پلن فعلی این ربات روشن نیست.", show_alert=True)
        return
    await state.clear()
    text, kb = await _qrbg_screen(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@vpn_admin_router.callback_query(F.data == "qrbg:set")
async def qrbg_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(QrBgFSM.photo)
    await cb.message.answer(
        "🖼 عکسِ موردِ نظرت را بفرست (به‌صورتِ عکس، نه فایل):",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(QrBgFSM.photo, F.photo)
async def qrbg_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    from tenant.shared.qr_brand import normalize_bg, render, set_bg
    row = await get_bot_row(bot)
    await state.clear()
    if not row:
        return
    # === PATCH_QR_BG_DOWNLOAD_RETRY_V1 ===
    # دانلودِ عکس از تلگرام گاهی با ServerDisconnectedError قطع می‌شود؛
    # یک قطعیِ گذراست، پس به‌جای شکستِ کامل دو بارِ دیگر تلاش می‌کنیم.
    data = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            buf = await bot.download(msg.photo[-1].file_id)
            data = await asyncio.to_thread(normalize_bg, buf.read())
            break
        except Exception as e:
            last_err = e
            logger.exception("دانلود/پردازشِ عکسِ پس‌زمینهٔ QR ناموفق بود (تلاشِ %s)",
                             attempt + 1)
            await asyncio.sleep(1.5 * (attempt + 1))
    if data is None:
        await msg.answer(
            "❌ عکس خوانده نشد؛ ارتباط با تلگرام قطع شد. "
            "چند لحظه بعد دوباره همان عکس را بفرست.\n"
            f"<code>{type(last_err).__name__}</code>")
        return
    await set_bg(row.id, data)
    preview = await asyncio.to_thread(
        render, "https://t.me/" + (row.username or "botsaz"), data)
    await msg.answer_photo(
        BufferedInputFile(preview, filename="qr_preview.png"),
        caption="✅ عکس ثبت شد. از این به بعد بارکدها این‌شکلی ساخته می‌شوند.")
    text, kb = await _qrbg_screen(row.id)
    await msg.answer(text, reply_markup=kb)


@vpn_admin_router.message(QrBgFSM.photo)
async def qrbg_not_photo(msg: Message):
    await msg.answer("لطفاً یک <b>عکس</b> بفرست (نه فایل و نه متن).")


@vpn_admin_router.callback_query(F.data == "qrbg:preview")
async def qrbg_preview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant.shared.qr_brand import get_bg, render
    bg = await get_bg(row.id)
    png = await asyncio.to_thread(
        render, "https://t.me/" + (row.username or "botsaz"), bg)
    await cb.message.answer_photo(
        BufferedInputFile(png, filename="qr_preview.png"),
        caption="👁 نمونهٔ بارکد با عکسِ فعلی")
    await cb.answer()


@vpn_admin_router.callback_query(F.data == "qrbg:del")
async def qrbg_del(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant.shared.qr_brand import set_bg
    await set_bg(row.id, None)
    text, kb = await _qrbg_screen(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("حذف شد")


# ═══════════════════════════════════════════════════════════════════════
#  تیترِ قابل‌ویرایشِ صفحه‌هایِ خرید/سرویس‌ها — PATCH_VPN_EDITABLE_TITLES_V1
# ═══════════════════════════════════════════════════════════════════════
# گزارشِ ادمین (ایلیا): «امکانِ ویرایشِ متنِ بالایِ دکمه‌هایِ هر بخش، مثلاً در بخشِ
# خریدِ سرویس الان نوشته "خرید VPN - حجم/روز دلخواه"». منطق در vpn_titles.py؛
# این‌جا فقط رابطِ ویرایش است. ورود از «تنظیماتِ عمومی ← ✏️ تیترِ صفحه‌ها».
class VpnTitleFSM(StatesGroup):
    waiting = State()
    waiting_tpl = State()   # PATCH_VC_PAGE_TEMPLATE_V1


async def _titles_screen(bot_id: int):
    from tenant.bots.vpn.vpn_titles import TITLES, get_raw, MAX_LEN
    lines = ["✏️ <b>تیترِ صفحه‌هایِ خرید و سرویس‌ها</b>", "",
             "تیترِ هر صفحه (خطِ اولِ پیام، بالایِ دکمه‌ها) را می‌توانی عوض کنی. "
             "هرکدام را که ثبت نکنی همان متنِ پیش‌فرض می‌ماند.", ""]
    rows = []
    for key, (default, desc) in TITLES.items():
        raw = await get_raw(bot_id, key)
        shown = _html_escape_mod.escape(raw or default)
        lines.append(f"• {desc}\n   ← <b>{shown}</b>" + ("" if raw else " <i>(پیش‌فرض)</i>"))
        rows.append([InlineKeyboardButton(text=f"✏️ {desc}", callback_data=f"vtl:e:{key}",
                                          style=ButtonStyle.PRIMARY)])
    # === PATCH_VC_PAGE_TEMPLATE_V1 ===
    rows.append([InlineKeyboardButton(text="📝 متنِ کاملِ صفحهٔ «خرید سرویس جدید»",
                                      callback_data="vtl:tpl", style=ButtonStyle.SUCCESS)])
    lines += ["", f"ℹ️ حداکثر {MAX_LEN} کاراکتر. «متنِ دکمه» (توضیحی که زیرِ تیتر می‌آید) "
                  "جداست و از همان جایِ قبلی‌اش تنظیم می‌شود."]
    # === PATCH_VPN_TITLES_IN_SETTINGS_V1 === رباتِ همکار از هابِ عمومی می‌آید، بقیه از «🎛 دکمه‌ها و تنظیمات»
    _back = "tadm:sec:settings"
    try:
        from tenant import get_bot_row_by_id as _gbr_t
        _br = await _gbr_t(bot_id)
        if _br and _br.plan == "partner":
            _back = "set:hub_general"
    except Exception:
        logger.exception("vpn_titles: خواندنِ پلن برایِ مقصدِ بازگشت ناموفق بود")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back,
                                      style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@vpn_admin_router.callback_query(F.data == "vtl:menu")
async def vpn_titles_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await _titles_screen(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ═══════════ تنظیماتِ برگشت وجه — PATCH_VPN_REFUND_V1 ═══════════
class VpnRefundFSM(StatesGroup):
    fee = State()


async def _refund_cfg_screen(bot_id: int):
    from tenant import get_setting as _gs
    on = await _gs(bot_id, "refund_enabled", "0") == "1"
    days = await _gs(bot_id, "refund_days_aware", "0") == "1"
    fee = await _gs(bot_id, "refund_fee_pct", "10")
    text = ("💸 <b>برگشت وجه سرویس</b>\n\n"
            "کاربر در صفحهٔ هر سرویسِ پولی‌اش دکمهٔ «💸 برگشت وجه» را می‌بیند؛ بعد از زدن، "
            "<b>دلیلش را می‌نویسد و درخواست برایِ شما (مالک/ادمین‌ها) می‌آید</b> — خودکار نیست. "
            "با «✅ تأیید»، مبلغ بر اساسِ <b>حجمِ باقی‌مانده</b> حساب می‌شود، کارمزد کم می‌شود، مبلغ به "
            "کیف پولِ کاربر برمی‌گردد و سرویس از پنل حذف می‌شود؛ با «❌ رد» چیزی برنمی‌گردد.\n\n"
            "• تستِ رایگان، هدیه/امتیاز و کانفیگ‌هایِ نمایندگی مشمول نیستند.\n"
            "• فقط سرویس‌هایی که بعد از فعال‌شدنِ این قابلیت خریده شده‌اند مشمول‌اند (قیمتِ پرداختی "
            "قبلاً ذخیره نمی‌شد).\n\n"
            f"وضعیت: <b>{'🟢 فعال' if on else '🔴 غیرفعال'}</b>\n"
            f"کارمزد: <b>{fee}٪</b>\n"
            f"احتسابِ روزِ باقی‌مانده: <b>{'🟢 روشن (کمینهٔ حجم و زمان)' if days else '⚪️ خاموش (فقط حجم)'}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 غیرفعال کن" if on else "🟢 فعال کن", callback_data="vrf:cfgt:on",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="✏️ تغییر درصد کارمزد", callback_data="vrf:cfgfee", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=("⚪️ خاموش‌کردنِ احتسابِ روز" if days else "🟢 روشن‌کردنِ احتسابِ روز"),
                              callback_data="vrf:cfgt:days", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    return text, kb


@vpn_admin_router.callback_query(F.data == "vrf:cfg")
async def vpn_refund_cfg(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await _refund_cfg_screen(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@vpn_admin_router.callback_query(F.data.startswith("vrf:cfgt:"))
async def vpn_refund_cfg_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import get_setting as _gs, set_setting as _ss
    key = {"on": "refund_enabled", "days": "refund_days_aware"}.get(cb.data.split(":")[2])
    if not key:
        return await cb.answer()
    cur = await _gs(row.id, key, "0")
    await _ss(row.id, key, "0" if cur == "1" else "1")
    text, kb = await _refund_cfg_screen(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("ویرایشِ صفحهٔ تنظیماتِ برگشت وجه")
    await cb.answer("✅ تغییر کرد")


@vpn_admin_router.callback_query(F.data == "vrf:cfgfee")
async def vpn_refund_fee_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnRefundFSM.fee)
    await cb.message.answer("درصدِ کارمزد را بفرست (عددِ ۰ تا ۱۰۰؛ مثلاً 10).", reply_markup=cancel_kb())
    await cb.answer()


@vpn_admin_router.message(VpnRefundFSM.fee, F.text)
async def vpn_refund_fee_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    from tenant import set_setting as _ss
    raw = (msg.text or "").strip().replace("٪", "").replace("%", "")
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if raw in ("/cancel", "لغو", "❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _refund_cfg_screen(row.id)
        return await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
    try:
        v = float(raw)
        assert 0 <= v <= 100
    except Exception:
        return await msg.answer("عددِ معتبر بین ۰ تا ۱۰۰ بفرست:")
    await state.clear()
    await _ss(row.id, "refund_fee_pct", f"{v:g}")
    text, kb = await _refund_cfg_screen(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_VC_PAGE_TEMPLATE_V1 ===
_VC_TPL_HELP = (
    "📝 <b>متنِ کاملِ صفحهٔ «خرید سرویس جدید»</b>\n\n"
    "کلِ متنِ بالایِ دکمه‌هایِ ➕/➖ را خودت بنویس. جاگذاری‌هایِ خودکار:\n"
    "<code>{gb}</code> → حجم\n<code>{days}</code> → مدت (روز)\n<code>{count}</code> → تعداد\n"
    "<code>{price_gb}</code> → قیمت هر گیگ\n<code>{price_day}</code> → قیمت هر روز\n"
    "<code>{total}</code> → مبلغِ نهایی (بعد از تخفیف)\n<code>{discount}</code> → مبلغِ تخفیف\n"
    "<code>{balance}</code> → موجودیِ کیف پول\n<code>{warn}</code> → هشدارِ «موجودی کافی نیست» (فقط وقتی لازم است)\n"
    "<code>{title}</code> → تیترِ صفحه\n\n"
    "مثال:\n<code>⚙️ خرید سرویس جدید\n\n📦 حجم: {gb}\n📅 مدت: {days}\n🔢 تعداد: {count}\n\n"
    "💵 قیمت هر گیگ: {price_gb} تومان\n\n💰 مجموع: {total} تومان\n👛 موجودی کیف پول: {balance} {warn}</code>\n\n"
    "برایِ برگشت به متنِ پیش‌فرض: /default"
)


@vpn_admin_router.callback_query(F.data == "vtl:tpl")
async def vpn_vc_tpl_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import get_setting as _gs
    cur = (await _gs(row.id, "vc_page_template", "") or "").strip()
    await state.set_state(VpnTitleFSM.waiting_tpl)
    extra = ("\n\n📝 متنِ فعلی:\n" + _html_escape_mod.escape(cur)) if cur else "\n\n📝 الان متنِ پیش‌فرض فعال است."
    await cb.message.answer(_VC_TPL_HELP + extra, reply_markup=cancel_kb(), parse_mode="HTML")
    await cb.answer()


@vpn_admin_router.message(VpnTitleFSM.waiting_tpl, F.text)
async def vpn_vc_tpl_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    from tenant import set_setting as _ss
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو", "❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _titles_screen(row.id)
        return await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
    if raw == "/default":
        await state.clear()
        await _ss(row.id, "vc_page_template", "")
        text, kb = await _titles_screen(row.id)
        return await msg.answer("✅ به متنِ پیش‌فرض برگشت.\n\n" + text, reply_markup=kb, parse_mode="HTML")
    tpl = msg.html_text or raw
    if len(tpl) > 3000:
        return await msg.answer("⚠️ متن بیش از حد طولانی است (حداکثر ۳۰۰۰ کاراکتر).")
    prev = tpl
    for k, v in (("gb", "5"), ("days", "30"), ("count", "1"), ("price_gb", "3,000"),
                 ("price_day", "500"), ("total", "30,000"), ("discount", "0"),
                 ("balance", "50,000"), ("warn", ""), ("title", "خرید سرویس")):
        prev = prev.replace("{" + k + "}", v)
    try:
        await msg.answer("👀 <b>پیش‌نمایش:</b>", parse_mode="HTML")
        await msg.answer(prev, parse_mode="HTML")
    except Exception as e:
        return await msg.answer(
            "❌ <b>ذخیره نشد.</b> قالب‌بندیِ HTML مشکل دارد:\n"
            f"<code>{_html_escape_mod.escape(str(e)[:200])}</code>\nاصلاح کن و دوباره بفرست.",
            parse_mode="HTML")
    await state.clear()
    await _ss(row.id, "vc_page_template", tpl)
    text, kb = await _titles_screen(row.id)
    await msg.answer("✅ متنِ صفحه ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@vpn_admin_router.callback_query(F.data.startswith("vtl:e:"))
async def vpn_titles_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant.bots.vpn.vpn_titles import TITLES, get_raw, MAX_LEN
    key = cb.data.split(":", 2)[2]
    if key not in TITLES:
        return await cb.answer("نامعتبر", show_alert=True)
    default, desc = TITLES[key]
    cur = await get_raw(row.id, key)
    await state.set_state(VpnTitleFSM.waiting)
    await state.update_data(vtl_key=key)
    await cb.message.answer(
        f"✏️ <b>{desc}</b>\n\n"
        f"تیترِ فعلی: <b>{_html_escape_mod.escape(cur or default)}</b>"
        + ("" if cur else " <i>(پیش‌فرض)</i>") + "\n\n"
        f"تیترِ تازه را بفرست (حداکثر {MAX_LEN} کاراکتر). بهتر است با حرفِ فارسی شروع شود "
        "تا جهتِ متن راست‌به‌چپ بماند؛ ایموجیِ ابتدایِ تیتر را خودِ ربات می‌گذارد.\n"
        "برایِ برگشت به متنِ پیش‌فرض: /skip",
        reply_markup=cancel_kb(), parse_mode="HTML")
    await cb.answer()


@vpn_admin_router.message(VpnTitleFSM.waiting, F.text)
async def vpn_titles_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    from tenant.bots.vpn.vpn_titles import TITLES, set_title, clean_input, MAX_LEN
    data = await state.get_data()
    key = data.get("vtl_key")
    if key not in TITLES:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو"):
        await state.clear()
        text, kb = await _titles_screen(row.id)
        return await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
    if raw == "/skip":
        await state.clear()
        await set_title(row.id, key, "")
        text, kb = await _titles_screen(row.id)
        return await msg.answer("✅ به متنِ پیش‌فرض برگشت.\n\n" + text, reply_markup=kb,
                                parse_mode="HTML")
    cleaned = clean_input(raw)
    if not cleaned:
        return await msg.answer("متن خالی است. دوباره بفرست (یا /skip):")
    if len(" ".join(raw.split())) > MAX_LEN:
        return await msg.answer(f"بیشتر از {MAX_LEN} کاراکتر است. کوتاه‌ترش کن و دوباره بفرست:")
    await state.clear()
    await set_title(row.id, key, cleaned)
    text, kb = await _titles_screen(row.id)
    await msg.answer("✅ تیتر ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
