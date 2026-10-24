"""
vpn_user.py — جریان کاربر نهایی برای «VPN خودکار»:
  • نمایش پلن‌ها و خرید با کیف پول
  • ساخت خودکار کانفیگ روی پنل فروشنده پس از پرداخت
  • تحویل لینک ساب + QR
  • «سرویس‌های من» با مصرف زنده + تمدید

callbackها با پیشوند: vbuy / vpay / vsvc / vrenew
"""
from html import escape as _esc_html
import asyncio
import html
import io
import json  # === PATCH_VPN_PANEL_MARKET_V1 ===
import re

import qrcode
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import func, select, update

from config import Session, WEBHOOK_BASE
from models import Bot as BotRow, BotUser, VpnPanel, VpnPlan, PanelAccount, WalletTxn, Setting, MainButton
from models import Order, order_no, VpnRefundRequest  # شناسهٔ واحدِ سفارش
from tenant.shared.panel_clients.vpn_panel import PanelClient, PanelError, gen_uuid, gen_sub_id
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client, make_panel_client_from_fields
from tenant.gateways.discount_engine import validate_discount, record_usage, revert_usage
from tenant.shared.web_view import ensure_web_token

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot_utils import has_media  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging


# === PATCH_TRUST_REPORT_DUP_TITLE_FIX_V1 ===
def _purchase_detail(title: str, gb, days, count: int = 1) -> str:
    """گزارشِ ادمین (ایلیا): در کانالِ گزارش، وقتی خودِ عنوانِ پلن دقیقاً
    همان مقدارِ حجم/روز است (مثلاً ادمین پلنش را «۲۰ گیگ / ۳۶۵ روز»
    نام‌گذاری کرده — رایج‌ترین روشِ نام‌گذاری)، قبلاً همان اطلاعات دوباره
    با فرمتِ دیگری کنارش چسبانده می‌شد: «۲۰ گیگ / ۳۶۵ روز — ۲۰ گیگ·۳۶۵ روز».
    این تابع اول چک می‌کند آیا عنوان از قبل حاویِ همین دو عدد هست؛ اگر
    بود، همان عنوان را دست‌نخورده برمی‌گرداند. «× N عدد» هم فقط وقتی
    اضافه می‌شود که واقعاً بیش از یکی باشد — برایِ تکی نیازی به نوشتنش
    نیست."""
    title = str(title or "").strip()
    # === PATCH_TRUST_DETAIL_DEDUP_V2 ===
    # مقایسه باید با همان شکلی باشد که عنوانِ خودکار (_plan_title_fa) می‌نویسد:
    # عددِ صحیحِ بدونِ «.۰» و برایِ صفر «نامحدود / بی‌انقضا». قبلاً str(10.0) = «10.0»
    # در «10 گیگ» پیدا نمی‌شد و همان اطلاعات دوباره کنارش چسبانده می‌شد.
    try:
        _g = float(gb)
        gb_s = str(int(_g)) if _g == int(_g) else str(_g)
    except (TypeError, ValueError):
        _g, gb_s = None, str(gb)
    try:
        _d = int(float(days))
        days_s = str(_d)
    except (TypeError, ValueError):
        _d, days_s = None, str(days)
    _has_gb = ("نامحدود" in title) if (_g is not None and _g <= 0) else (gb_s in title)
    _has_days = ("بی‌انقضا" in title) if (_d is not None and _d <= 0) else (days_s in title)
    if title and _has_gb and _has_days:
        base = title
    else:
        # === PATCH_UNLIMITED_DETAIL_V1 === حجمِ ۰ یعنی نامحدود (نه «0 گیگ»)؛ مدتِ ۰ یعنی بی‌انقضا
        _unl = _g is not None and _g <= 0
        _gb_txt = "نامحدود" if _unl else f"{gb} گیگ"
        _d_txt = "بی‌انقضا" if (_d is not None and _d <= 0) else f"{days} روز"
        if title and _unl and _has_gb:
            parts = [] if _has_days else [_d_txt]
            base = f"{title} — {' · '.join(parts)}" if parts else title
        elif title:
            base = f"{title} — {_gb_txt} · {_d_txt}"
        else:
            base = f"{_gb_txt} · {_d_txt}"
    return f"{base} × {count} عدد" if count and count > 1 else base


vpn_user_router = Router()

# === PATCH_VPN_EDITABLE_TITLES_V1 === تیترِ قابل‌ویرایشِ صفحه‌هایِ اصلی (ماژولِ کمکیِ بدونِ router)
from tenant.bots.vpn.vpn_titles import get_title as _get_page_title


class VpnConfiguratorFSM(StatesGroup):
    selecting = State()   # کاربر در حال انتخاب حجم/روز/تعداد
    # === PATCH_VC_MANUAL_ENTRY_V1 ===
    entering_manual = State()   # واردکردنِ دستیِ عددِ حجم/مدت/تعداد


class _VpnDiscountFSM(StatesGroup):
    entering = State()      # وارد کردن کد تخفیف برای خرید پلن ثابت
    entering_vc = State()   # وارد کردن کد تخفیف برای خرید کانفیگ آماده (دلخواه)


# === PATCH_VPN_PAY_DEDUPE_V1 ===
# جلوگیری از اجرای هم‌زمانِ چند کلیکِ متوالی روی دکمهٔ «پرداخت» که باعث می‌شد
# چند کانفیگ تکراری روی پنل‌های خودکار (Marzban/Hiddify/...) ساخته شود.
# چون حذف/تغییرِ دکمه (edit_text) کمی طول می‌کشد، تلگرام کلیک‌های بعدی را هم
# به‌عنوان کال‌بک‌های جدا ارسال می‌کند و aiogram آن‌ها را موازی اجرا می‌کند؛
# این ست، برای هر (bot_token, telegram_id, plan_id) در حال پردازش را نگه می‌دارد
# تا از ورودِ دوباره جلوگیری شود.
_vpn_pay_inflight: set[tuple[str, int, int]] = set()

# === PATCH_VPN_DOUBLE_BUY_GUARD_V1 ===
# همان مشکلِ بالا فقط برایِ «پلنِ آماده» گرفته شده بود. گزارشِ مالکِ ربات
# نشان داد خریدِ دلخواه (vc:pay)، افزایشِ حجم (vadd:pay) و تمدید (vrenp)
# هم دو بار اجرا می‌شوند: پاک‌کردنِ دکمه‌ها (edit_reply_markup) یک رفت‌وبرگشتِ
# شبکه است و تا تمام شود، تپِ دومِ کاربر به‌صورتِ یک آپدیتِ جدا رسیده و
# aiogram آن را موازی اجرا کرده است. این ست همهٔ مسیرهایِ پولی را با یک
# کلیدِ متنی پوشش می‌دهد.
_vpn_action_inflight: set[str] = set()


# === PATCH_VPN_EDIT_IN_PLACE_V1 ===
# === PATCH_FLOW_DROP_REPLY_KB_V1 ===
# گزارشِ ایلیا: در منویِ کیبوردی، وقتی مشتری دکمهٔ خرید (مثلاً «(C) برنزی») را می‌زند و وارد صفحه‌هایِ مرحله‌ای
# (انتخابِ سرور، نامِ دلخواه، حجم/روز/تعدادِ دلخواه) می‌شود، منویِ کیبوردیِ قبلی هنوز زیرِ صفحه می‌ماند و شلوغ/گیج‌کننده است.
# === PATCH_FLOW_BACK_KB_V1 === ولی با برداشتن کامل کیبورد «بازگشت به منو» هم از دست می‌رفت (گزارش بعدی ایلیا): حالا
# کیبورد قبلی با یک کیبورد تک‌دکمه‌ای «⬅️ بازگشت به منو» عوض می‌شود (tenant.flow_back_kb)؛ دکمه به همان زیرمنو برمی‌گردد.
# فقط وقتی فراخوان «پیامِ واقعیِ کاربر» باشد (یعنی از یک دکمهٔ کیبوردی آمده، نه از کلیکِ اینلاین) و ربات در حالتِ کیبوردی باشد.
async def _drop_reply_kb(msg, bot_id: int) -> None:
    try:
        if not (isinstance(msg, Message) and msg.text and getattr(msg, "from_user", None)
                and not msg.from_user.is_bot):
            return
        from tenant import get_setting, flow_back_kb
        if await get_setting(bot_id, "menu_mode", "reply") == "inline":
            return
        await flow_back_kb(msg, bot_id)
    except Exception:
        logger.info("vpn: جایگزینی کیبورد قبلی با کیبورد بازگشت ممکن نشد (بی‌اهمیت)")


async def _with_home_row(kb: InlineKeyboardMarkup, bot_id: int) -> InlineKeyboardMarkup:
    """=== PATCH_FLOW_HOME_ROW_V1 === (ایلیا) در منویِ کیبوردی، کیبوردِ پایین هنگامِ خرید برداشته می‌شود؛ پس هر صفحهٔ خرید
    باید «🏠 بازگشت به منو» داشته باشد تا کیبورد برگردد. اگر قبلاً چنین دکمه‌ای هست، تکرار نمی‌شود."""
    try:
        from tenant import get_setting
        if await get_setting(bot_id, "menu_mode", "reply") == "inline":
            return kb
        rows = list(kb.inline_keyboard)
        # === PATCH_FLOW_BACK_TO_SUBMENU_V1 === (ایلیا) اگر کاربر از یک زیرمنو آمده، «بازگشت به منو» همان زیرمنو را
        # (با کیبوردش) برمی‌گرداند و «🏠 منوی اصلی» جدا کنارش می‌آید؛ وگرنه مثلِ قبل فقط یک دکمهٔ خانه.
        from tenant import _last_menu_for
        pm = _last_menu_for(bot_id)
        has_home = any((b.callback_data or "") == "mbtn:home" for r in rows for b in r)
        has_sub = any((b.callback_data or "").startswith("rsub:") for r in rows for b in r)
        if pm:
            if has_sub:
                return kb
            rows = [[b for b in r if (b.callback_data or "") != "mbtn:home"] for r in rows]
            rows = [r for r in rows if r]
            rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو", callback_data=f"rsub:{pm}",
                                              style=ButtonStyle.DANGER),
                         InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home",
                                              style=ButtonStyle.DANGER)])
            return InlineKeyboardMarkup(inline_keyboard=rows)
        if has_home:
            return kb
        rows.append([InlineKeyboardButton(text="🏠 بازگشت به منو", callback_data="mbtn:home",
                                          style=ButtonStyle.DANGER)])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    except Exception:
        logger.exception("vpn: افزودنِ «بازگشت به منو» ممکن نشد")
        return kb


class _EditFirst:
    """رپرِ cb.message: اولین `.answer(text, ...)` به‌جایِ حذفِ پیام و ارسالِ پیامِ تازه،
    *همان پیام* را ویرایش می‌کند (سریع‌تر و بدونِ پرشِ چت)؛ بقیهٔ فراخوانی‌ها (پیامِ دوم به بعد)
    و هر ویژگیِ دیگری عادی به پیامِ اصلی می‌رود. اگر ویرایش ممکن نبود (پیامِ عکس‌دار/QR،
    قدیمی، کیبوردِ غیرِ اینلاین) همان رفتارِ قبلی: حذفِ پیامِ قدیمی + ارسالِ جدید."""
    __slots__ = ("_m", "_used", "_u")

    def __init__(self, msg, user=None):
        self._m = msg
        self._used = False
        self._u = user      # === PATCH_EDITFIRST_REAL_USER_V1 ===

    def __getattr__(self, name):
        # === PATCH_EDITFIRST_REAL_USER_V1 ===
        # باگِ گزارش‌شده (WHALE SHOP): بعد از «بازگشت»، صفحه «موجودی کیف پول: ۰ تومان» نشان می‌داد درحالی‌که موجودی داشت.
        # علت: from_user رپر همان from_user پیامِ ربات (cb.message) بود، یعنی *خودِ ربات*، و موجودی/تخفیفِ ربات خوانده می‌شد.
        # حالا اگر کاربرِ واقعی (cb.from_user) داده شده باشد، همان برگردانده می‌شود.
        if name == "from_user" and self._u is not None:
            return self._u
        return getattr(self._m, name)

    async def answer(self, text=None, **kw):
        if not self._used and text is not None:
            self._used = True
            rm = kw.get("reply_markup")
            if rm is None or isinstance(rm, InlineKeyboardMarkup):
                try:
                    _kw = {k: v for k, v in kw.items()
                           if k in ("reply_markup", "parse_mode", "link_preview_options",
                                    "disable_web_page_preview")}
                    await self._m.edit_text(text, **_kw)
                    return self._m
                except Exception as e:     # noqa: BLE001
                    if "not modified" in str(e).lower():
                        return self._m
            try:
                await self._m.delete()
            except Exception:
                logger.debug("vpn: حذفِ پیامِ قدیمی ممکن نشد (بی‌اهمیت)")
        return await self._m.answer(text, **kw)


class _SilentCB:
    """CallbackQuery با data/message جایگزین و answer بی‌اثر (قبلاً پاسخ داده شده)."""
    def __init__(self, cb, message, data):
        self._cb, self.message, self.data = cb, message, data

    def __getattr__(self, name):
        return getattr(self._cb, name)

    async def answer(self, *a, **k):
        return True


# === PATCH_PAY_PHONE_GATE_FIX_V1 ===
# «تاییدِ شماره پیش از پرداخت» فقط رویِ شارژِ کیفِ پول اجرا می‌شد و مسیرهایِ
# خریدِ سرویس اصلاً از آن رد نمی‌شدند — برایِ همین مالکِ ربات گزینه را روشن
# می‌کرد ولی موقعِ خرید هیچ شماره‌ای پرسیده نمی‌شد.
async def _pay_gate_block(cb: CallbackQuery, bot: Bot) -> bool:
    """True یعنی شماره لازم است و جلویِ پرداخت گرفته شد."""
    from tenant import pay_phone_gate
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        return False
    if await pay_phone_gate(cb.message, row, cb.from_user.id):
        await cb.answer("📱 اول شماره‌ات را تایید کن.", show_alert=True)
        return True
    return False


async def _claim_or_reject(cb: CallbackQuery, bot: Bot, tag: str) -> bool:
    """قفلِ دیتابیسیِ «یک پیش‌فاکتور = یک تحویل».

    کلید شاملِ شناسهٔ همان پیامِ پیش‌فاکتور است، پس خریدِ بعدیِ کاربر از
    همان پلن آزاد می‌ماند و فقط تپِ دومِ روی همین پیام رد می‌شود.
    """
    from tenant import claim_purchase_once
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        return False
    _mid = getattr(getattr(cb, "message", None), "message_id", 0)
    raw = f"{tag}:{cb.from_user.id}:{_mid}:{cb.data}"
    if await claim_purchase_once(row.id, raw):
        return True
    await cb.answer("این خرید قبلاً ثبت شده — سرویس‌ات را در «سرویس‌های من» ببین.",
                    show_alert=True)
    return False


class _vpn_once:
    """کانتکست‌منیجرِ «فقط یک اجرا در لحظه» برای مسیرهای پولی.

    نمونه:
        async with _vpn_once(key) as ok:
            if not ok:
                return           # کلیکِ تکراری — بی‌صدا رد می‌شود
            ...
    """

    def __init__(self, key: str):
        self.key = key
        self.ok = False

    async def __aenter__(self) -> bool:
        if self.key in _vpn_action_inflight:
            return False
        _vpn_action_inflight.add(self.key)
        self.ok = True
        return True

    async def __aexit__(self, *exc):
        if self.ok:
            _vpn_action_inflight.discard(self.key)
        return False

# === PATCH_VC_RECOVER_PANEL_KEY_V1 ===
async def _vc_recover_panel_key(state: FSMContext, bot_id: int) -> str:
    """کلیدِ پنلِ خریدِ دلخواه را برمی‌گرداند و اگر از state گم شده بازیابی می‌کند.

    لاگِ واقعی: چند مشتری بعد از انتخابِ حجم و نوشتنِ نام، در «پرداخت» با «نشستِ خرید
    منقضی شده» برگشتند (panel_key خالی). دلیل: با /start یا دکمه‌هایِ قدیمیِ صفحه، state
    پاک می‌شد ولی gb/days دوباره از callback ست می‌شد و کلیدِ پنل نه — پیش‌فاکتور هم با
    قیمتِ عمومی/پیش‌فرض ساخته می‌شد. اگر کلید نیست: اول کلیدِ آخرین صفحهٔ پلن‌ها
    (vbuy_panel_key)، وگرنه تنها پنلِ فعالِ دارایِ فروشِ دلخواه. اگر مبهم بود خالی
    می‌ماند (و پرداخت مثلِ قبل رد می‌شود)."""
    data = await state.get_data()
    pk = (data.get("vc_panel_key") or "").strip()
    if pk:
        return pk
    cand = (data.get("vbuy_panel_key") or "").strip()
    if not cand:
        async with Session() as s:
            keys = [r for r in (await s.scalars(select(VpnPanel.panel_key).where(
                VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))).all() if r]  # noqa: E712
        ok = []
        for k in keys:
            try:
                if (await _vc(bot_id, "vc_enabled", k)) == "1":
                    ok.append(k)
            except Exception:
                logger.exception("vc: بررسیِ فعال‌بودنِ فروشِ دلخواهِ پنل %s ناموفق بود", k)
        if len(ok) == 1:
            cand = ok[0]
    if cand:
        await state.update_data(vc_panel_key=cand, vc_bot_id=bot_id)
        logger.info("vc: کلیدِ پنل از state گم شده بود؛ بازیابی شد → %s (bot=%s)", cand, bot_id)
    return cand


class _VpnNameFSM(StatesGroup):
    naming = State()        # پرسیدن اسم دلخواه قبل از پیش‌فاکتورِ پلن آماده
    naming_vc = State()     # پرسیدن اسم دلخواه قبل از پیش‌فاکتورِ خرید دلخواه (vc)


_VC_DEF = {
    "vc_enabled": "0",
    "vc_min_gb": "5", "vc_max_gb": "100", "vc_step_gb": "5",
    "vc_min_days": "30", "vc_max_days": "90", "vc_step_days": "30",
    "vc_max_count": "5", "vc_price_gb": "1000", "vc_price_day": "500",
    # === PATCH_VC_SIMPLE_MODE_V1 === (کلیدها باید با vpn_admin._VC_DEFAULTS یکی باشند؛ تست دارد)
    "vc_simple": "0", "vc_simple_days": "30",
    # === PATCH_RESELLER_GB_PRICING_V1 === خالی یعنی «تنظیم نشده»
    "rs_price_gb": "0", "rs_price_day": "0",
    # حالتِ فروش: "general" (بازهٔ عمومیِ بالا) یا "tiers" (تعرفه‌های مدت‌دار)
    "vc_pricing_mode": "general",
    # تنظیمات تمدید/افزایش حجم
    "renew_enabled": "1",
    "renew_price_gb": "1000", "renew_price_day": "500",
    "renew_min_gb": "10", "renew_max_gb": "100", "renew_step_gb": "10",
    "renew_min_days": "7", "renew_max_days": "90", "renew_step_days": "7",
    # === PATCH_WG_BTN_PANEL_FLAG_V1 === «🛡 ارسال کانفیگ تکی وایرگارد» در صفحهٔ سرویس؛ مالک برایِ پنل‌هایِ غیرِ وایرگارد خاموشش می‌کند
    "wg_cfg_btn": "1",
}

# کلیدهایی که به ازای هر پنل جداگانه ذخیره می‌شوند
_VC_PANEL_KEYS_U = [
    "vc_enabled", "vc_min_gb", "vc_max_gb", "vc_step_gb",
    "vc_min_days", "vc_max_days", "vc_step_days",
    "vc_max_count", "vc_price_gb", "vc_price_day", "vc_pricing_mode",
    "vc_simple", "vc_simple_days",   # === PATCH_VC_SIMPLE_MODE_V1 ===
    "renew_enabled", "renew_price_gb", "renew_price_day",
    "renew_min_gb", "renew_max_gb", "renew_step_gb",
    "renew_min_days", "renew_max_days", "renew_step_days",
    # === PATCH_RESELLER_GB_PRICING_V1 === تعرفهٔ گیگیِ مخصوصِ نماینده
    "rs_price_gb", "rs_price_day",
    "wg_cfg_btn",    # PATCH_WG_BTN_PANEL_FLAG_V1
]


def _pvc_key_u(panel_key: str, key: str) -> str:
    """کلید per-panel. برای vc_* الگوی قدیمی حفظ می‌شود (vc_enabled →
    vc_sanai_enabled)؛ برای renew_* پسوند پنل به انتها اضافه می‌شود
    (renew_price_gb → renew_price_gb_sanai)."""
    if key.startswith("vc_"):
        return f"vc_{panel_key}_{key[3:]}"
    return f"{key}_{panel_key}"


# === PATCH_VC_SETTINGS_CACHE_V1 ===
# اندازه‌گیریِ زنده: هر ضربه روی ➕/➖ِ «خریدِ حجم/روزِ دلخواه» (vc:gb+) ≈۲٫۴ثانیه و ۱۱۳ کوئریِ settings بود؛ چون
# `_vc_all` برایِ هر ~۲۰ کلید یک (یا دو) کوئریِ بدونِ کش می‌زد و هر کلیک چند بار صدایش می‌کرد. حالا هر کلیدِ تنظیم ۸ثانیه
# کش می‌شود (با همان معنایِ دقیقِ قبلی: «ردیف هست/نیست» جدا از «مقدارش خالی است»). تغییرِ ادمین حداکثر ۸ثانیه دیرتر دیده می‌شود.
_VC_CACHE: dict = {}
_VC_CACHE_TTL = 8.0


async def _vc_row(bot_id: int, db_key: str):
    """(ردیف‌هست؟، مقدار) — با کشِ کوتاه."""
    import time as _tm
    now = _tm.monotonic()
    hit = _VC_CACHE.get((bot_id, db_key))
    if hit is not None and hit[2] > now:
        return hit[0], hit[1]
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
    ex, val = row is not None, (row.value if row is not None else None)
    _VC_CACHE[(bot_id, db_key)] = (ex, val, now + _VC_CACHE_TTL)
    return ex, val


async def _vc(bot_id: int, key: str, panel_key: str = "") -> str:
    """مقدار یک تنظیم — اگر panel_key داده شود per-panel می‌خواند؛ اگر هنوز
    برای آن پنل خاص ذخیره نشده بود (سازگاری با قبل)، به مقدارِ عمومیِ
    قدیمیِ همان کلید (بدون پسوند پنل) برمی‌گردد، نه مستقیم مقدار پیش‌فرض."""
    db_key = _pvc_key_u(panel_key, key) if (panel_key and key in _VC_PANEL_KEYS_U) else key
    ex, val = await _vc_row(bot_id, db_key)
    if ex:
        return val
    if panel_key and db_key != key:
        ex2, val2 = await _vc_row(bot_id, key)
        if ex2:
            return val2
    return _VC_DEF.get(key, "0")


async def _vc_all(bot_id: int, panel_key: str = "") -> dict:
    return {k: await _vc(bot_id, k, panel_key) for k in _VC_DEF}


async def _apply_vc_tier(cfg: dict, bot_id: int, panel_key: str, days: int | None,
                         gb: int | None = None) -> dict:
    """اگر برای این پنل «تعرفه‌های مدت‌دار» تعریف شده باشد **و** حالتِ فروش
    روی «تعرفه‌های مدت‌دار» تنظیم شده باشد، بازهٔ حجم/قیمتِ مخصوصِ همان
    بازهٔ روز را جایگزینِ مقادیرِ عمومیِ cfg می‌کند (و بازهٔ روزِ کلی/اسلایدر
    را به اجتماعِ همهٔ تعرفه‌ها تبدیل می‌کند تا کاربر بتواند بینِ تعرفه‌ها
    جابه‌جا شود). اگر حالتِ فروش روی «بازهٔ عمومی» باشد (پیش‌فرض)، این تابع
    cfg را دست‌نخورده برمی‌گرداند — حتی اگر تعرفه‌هایی هم ذخیره شده باشند —
    چون این دو روش قرار نیست هم‌زمان روی خریدِ کاربر اثر بگذارند."""
    if cfg.get("vc_pricing_mode") != "tiers":
        return cfg
    try:
        from tenant.bots.vpn.vpn_admin import vc_tiers_get, vc_tier_for_days
        tiers = await vc_tiers_get(bot_id, panel_key)
    except Exception:
        tiers = []
    if not tiers:
        return cfg
    cfg = dict(cfg)
    union_min = min(int(t.get("days_min", 0)) for t in tiers)
    union_max = max(int(t.get("days_max", 0)) for t in tiers)
    cfg["vc_min_days"] = str(union_min)
    cfg["vc_max_days"] = str(union_max)
    eff_days = days if days is not None else union_min
    tier = vc_tier_for_days(tiers, eff_days, gb)
    if tier:
        # === PATCH_VPN_TIER_GB_BRACKETS_V1 ===
        # اگر برایِ همین مدت چند پلهٔ حجمی تعریف شده باشد، اسلایدرِ حجم
        # باید رویِ *اجتماعِ* همهٔ پله‌ها حرکت کند تا کاربر بتواند از پلهٔ
        # پایین به بالا برود؛ ولی قیمت از پله‌ای خوانده می‌شود که حجمِ
        # فعلی داخلش است. با یک تعرفه، اجتماع = همان یک بازه (رفتارِ قبل).
        from tenant.bots.vpn.vpn_admin import vc_tiers_for_days
        same_days = vc_tiers_for_days(tiers, eff_days) or [tier]
        # === PATCH_VPN_TIER_CUMULATIVE_V1 ===
        # پله‌هایِ حجمیِ همین مدت، برایِ محاسبهٔ تجمعیِ قیمت.
        if len(same_days) > 1:
            cfg["vc_gb_brackets"] = [
                (int(t.get("gb_max", 0)), int(t.get("price_gb", 0)))
                for t in same_days]
        cfg["vc_min_gb"] = str(min(int(t.get("gb_min", 0)) for t in same_days))
        cfg["vc_max_gb"] = str(max(int(t.get("gb_max", 0)) for t in same_days))
        cfg["vc_step_gb"] = str(tier.get("gb_step"))
        cfg["vc_price_gb"] = str(tier.get("price_gb"))
        cfg["vc_price_day"] = str(tier.get("price_day"))
        # گامِ روز مخصوصِ همین تعرفه (اگر تعرفه‌های قدیمی‌تر بدونِ این فیلد
        # ذخیره شده باشند، گامِ ۱ روزه به‌عنوانِ پیش‌فرضِ امن استفاده می‌شود).
        cfg["vc_step_days"] = str(tier.get("days_step") or 1)
    return cfg


async def _vc_all_for_user(bot_id: int, telegram_id: int, panel_key: str = "",
                           days: int | None = None, gb: int | None = None,
                           is_dedicated_panel: bool = False) -> dict:
    """مثل _vc_all ولی اگر کاربر در ۵ نفر برتر «نفرات برتر» باشد، قیمت هر
    گیگ/روز را با تخفیف مربوطه (عمومی یا اختصاصی قسمت) جایگزین می‌کند.

    اگر `days` داده شود و برای این پنل «تعرفه‌های مدت‌دار» تعریف شده باشد،
    ابتدا بازهٔ حجم/قیمتِ مخصوصِ همان بازهٔ روز به‌عنوانِ پایه اعمال می‌شود و
    تخفیف‌های زیر روی همان قیمتِ پایه محاسبه می‌شوند.

    === PATCH_VPN_PARTNER_V1 ===
    اگر کاربر «همکار» باشد و برای همین پنل/محدودهٔ «فروش دلخواه» فعال شده
    باشد، تخفیفِ همکار به‌جای تخفیفِ نفراتِ برتر اعمال می‌شود (روی قیمتِ
    پایه، نه روی قیمتِ از قبل تخفیف‌خورده) — چون مشخصاً برای همین شخص
    تنظیم شده و اولویت دارد.

    `is_dedicated_panel`: فقط وقتی True بده که این فراخوانی از خودِ «پنلِ
    اختصاصیِ نمایندگی» (web_panel.py) است. تعرفهٔ گیگی/روزانهٔ نمایندگی
    (rs_price_gb/rs_price_day) فقط در این حالت اعمال می‌شود؛ فراخوانی‌هایِ
    عادیِ ربات (tenant/vpn_user.py) این را نمی‌دهند تا فعال‌بودنِ نمایندگی
    روی قیمتِ خودِ ربات اثر نگذارد.
    """
    cfg = await _vc_all(bot_id, panel_key)
    cfg = await _apply_vc_tier(cfg, bot_id, panel_key, days, gb)
    base_pg = int(cfg["vc_price_gb"]); base_pd = int(cfg["vc_price_day"])
    eff_pg, eff_pd = base_pg, base_pd
    try:
        from tenant.shared.top_customers import vc_price_with_discount
        tc_pg, tc_pd, discounted = await vc_price_with_discount(
            bot_id, telegram_id, panel_key, base_pg, base_pd)
        if discounted:
            eff_pg, eff_pd = tc_pg, tc_pd
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    pct = 0
    try:
        from tenant.bots.vpn.vpn_partners import partner_discount_percent
        # === PATCH_RESELLER_PANEL_ONLY_V1 ===
        pct = await partner_discount_percent(bot_id, telegram_id, panel_key, "custom",
                                             include_auto_reseller=False)
        # === PATCH_PARTNER_PANEL_GB_PRICE_V1 === قیمتِ گیگیِ اختصاصیِ همکار به‌جایِ درصد
        from tenant.bots.vpn.vpn_partners import partner_gb_price
        _gbp = await partner_gb_price(bot_id, telegram_id, panel_key, "custom")
        if _gbp > 0:
            eff_pg, eff_pd = min(base_pg, _gbp) if base_pg else _gbp, base_pd
            pct = max(pct, 1)    # تعرفهٔ سراسریِ نمایندگی رویِ قیمتِ اختصاصی ننشیند
        elif pct > 0:
            eff_pg = max(0, round(base_pg * (100 - pct) / 100))
            eff_pd = max(0, round(base_pd * (100 - pct) / 100))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # === PATCH_RESELLER_GB_PRICING_V1 ===
    # چند مالکِ ربات گفتند فروششان به نماینده گیگی است نه پلنی، و تخفیفِ
    # درصدی با ساختارِ قیمتشان جور درنمی‌آید. اگر تعرفهٔ جداگانهٔ نمایندگی
    # تنظیم شده باشد، *به‌جایِ* درصدِ تخفیف می‌نشیند (نه روی آن) — وگرنه
    # تخفیف دوبار حساب می‌شد.
    #
    # === PATCH_PARTNER_CUSTOM_PRICE_PRIORITY_V1 ===
    # باگ: این تعرفهٔ سراسری بی‌قیدوشرط رویِ تخفیفِ اختصاصیِ همکار (pct
    # بالا) رونویسی می‌کرد — یعنی اگر ادمین برایِ یک همکارِ خاص قیمتِ
    # ارزان‌تر از تعرفهٔ نمایندگیِ خودکار می‌گذاشت، همان تعرفهٔ سراسری
    # جایگزینش می‌شد و تخفیفِ اختصاصی هیچ‌وقت اثر نداشت. حالا اگر تخفیفِ
    # اختصاصی برایِ این همکار پیدا شده باشد (pct > 0)، اولویت با آن است
    # و تعرفهٔ سراسریِ نمایندگی رویش رونویسی نمی‌کند.
    #
    # === PATCH_RESELLER_GB_PANEL_ONLY_V1 (لغو شد؛ نگاه کن PATCH_RESELLER_GB_EVERYWHERE_V1) ===
    # قبلاً این تعرفه فقط وقتی اعمال می‌شد که is_dedicated_panel=True باشد
    # (یعنی خرید از پنلِ اختصاصیِ نمایندگی/مینی‌اپ، نه از دکمه‌هایِ عادیِ
    # خودِ ربات) — تا فعال‌بودنِ نمایندگی رویِ قیمتِ خودِ ربات اثر نگذارد.
    #
    # === PATCH_RESELLER_GB_EVERYWHERE_V1 ===
    # طبقِ خواستهٔ ادمین: نماینده باید همین تعرفه را از هرجا خرید بزند
    # بگیرد، نه فقط پنلِ اختصاصی/مینی‌اپ. برایِ همین شرطِ
    # `is_dedicated_panel` حذف شد؛ فقط کافی‌ست کاربر نمایندهٔ فعال باشد و
    # تخفیفِ دستیِ اختصاصی نداشته باشد (pct<=0).
    #
    # باگِ دوم: مقدارِ پیش‌فرضِ ذخیره‌شدهٔ rs_price_day یعنی «تنظیم نشده»
    # رشتهٔ "0" است (نگاه کن به _vc_all بالاتر) — درست مثلِ rs_price_gb.
    # چک برایِ روز، برخلافِ چکِ گیگ، فقط isdigit() بود و بررسیِ «> 0»
    # نداشت؛ پس وقتی مالک هیچ تعرفهٔ روزانه‌ای تنظیم نکرده بود، "0" به
    # جایِ «نادیده بگیر» به‌عنوانِ «قیمتِ هر روز صفر تومان» خوانده می‌شد و
    # قیمتِ روزِ عادی را با صفر رونویسی می‌کرد. حالا هر دو چک یکسان‌اند.
    if pct <= 0:
        try:
            _aud, _is_rs = await _plan_audience_filter(bot_id, telegram_id)
            if _is_rs:
                _rs_pg = (await _vc(bot_id, "rs_price_gb", panel_key)).strip()
                _rs_pd = (await _vc(bot_id, "rs_price_day", panel_key)).strip()
                if _rs_pg.isdigit() and int(_rs_pg) > 0:
                    eff_pg = int(_rs_pg)
                if _rs_pd.isdigit() and int(_rs_pd) > 0:
                    eff_pd = int(_rs_pd)
        except Exception:
            logger.exception("تعرفهٔ نمایندگی خوانده نشد؛ قیمتِ عادی اعمال شد")
    if eff_pg != base_pg or eff_pd != base_pd:
        cfg = dict(cfg)
        cfg["vc_price_gb"] = str(eff_pg)
        cfg["vc_price_day"] = str(eff_pd)
        # === PATCH_VPN_TIER_CUMULATIVE_V1 ===
        # تخفیف با همان نسبت رویِ همهٔ پله‌ها اعمال می‌شود، وگرنه کاربرِ
        # تخفیف‌دار رویِ پله‌های بالا تخفیفش را از دست می‌داد.
        if cfg.get("vc_gb_brackets") and base_pg > 0:
            ratio = eff_pg / base_pg
            cfg["vc_gb_brackets"] = [(gmax, max(0, round(pr * ratio)))
                                     for gmax, pr in cfg["vc_gb_brackets"]]
    return cfg


def _vc_gb_cost(cfg: dict, gb: int) -> int:
    """=== PATCH_VPN_FLAT_TIER_PRICING_V1 ===
    هزینهٔ حجم به‌صورتِ تخت (flat): کلِ حجم با قیمتِ همان پله‌ای که کاربر
    داخلش است حساب می‌شود (پله از قبل توسطِ vc_tier_for_days بر اساسِ gb
    انتخاب شده و cfg["vc_price_gb"] را همان مقدار قرار داده است). دیگر
    پله‌های پایین‌تر به‌صورتِ تجمعی/مالیاتی جمع نمی‌شوند."""
    return int(gb) * int(cfg["vc_price_gb"])


# ─────────────── تنظیم «نام سرورها مشتری» (per-panel) ───────────────
# این تنظیمات از منوی مدیریت هر پنل (tenant/vpn_admin.py → tvpn:srvname:{key})
# نوشته می‌شوند؛ این‌جا فقط خوانده می‌شوند (همان قرارداد _vc_/_vc_all بالا).
_SRVNAME_DEFAULTS = {"srvname_prefix": "", "srvname_idtype": "id"}  # idtype: id | username


def _srvname_pkey_u(panel_key: str, key: str) -> str:
    return f"{key}_{panel_key}"  # srvname_prefix_sanai


async def get_srvname_cfg(bot_id: int, panel_key: str) -> dict:
    """تنظیمات نام‌گذاری سرور برای مشتری این پنل: {'prefix':..., 'idtype': 'id'|'username'}."""
    out = {}
    async with Session() as s:
        for full_key, default in _SRVNAME_DEFAULTS.items():
            db_key = _srvname_pkey_u(panel_key, full_key)
            row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == db_key))
            short = full_key.replace("srvname_", "")  # prefix | idtype
            out[short] = row.value if row else default
    return out


def build_srv_name(prefix: str, idtype: str, bot_username: str | None,
                    tg_id: int, tg_username: str | None,
                    custom_label: str | None = None) -> str:  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    """ساخت بخش خوانای نام سرور (بدون پسوند یکتا):
    پیشوند (دلخواه ادمین یا نام ربات) + شناسهٔ کاربر (یوزرنیم، آیدی عددی، یا
    نام دلخواهی که خودِ کاربر موقعِ خرید انتخاب کرده — idtype == "custom").

    اگر idtype == "custom" باشد ولی کاربر در همان خرید اسم دلخواهی وارد
    نکرده باشد (custom_label خالی)، طبقِ درخواستِ ادمین (PATCH_SRVNAME_CUSTOM_SKIP_USERNAME_V1)
    اول به یوزرنیمِ تلگرامِ خودِ کاربر برمی‌گردیم (دقیقاً مثلِ حالتِ idtype
    == "username")، و فقط اگر کاربر اصلاً یوزرنیم نداشت، به آیدیِ عددی.
    """
    base = _re.sub(r"[^a-zA-Z0-9_]", "", prefix or "")[:20]
    if not base:
        raw = (bot_username or "bot").lower()
        base = _re.sub(r"[^a-z0-9]", "", raw)
        if base.endswith("bot") and len(base) > 3:
            base = base[:-3]
        base = base[:12] or "bot"
    if idtype == "custom":
        # === PATCH_SRVNAME_CUSTOM_SKIP_USERNAME_V1 ===
        # نکته: عمداً «@» به ident اضافه نمی‌شود — این رشته همون فیلدِ
        # شناسه/ایمیلِ واقعیِ اکانت رویِ پنله (نگاه کن به _make_config_email)،
        # نه فقط یک برچسبِ نمایشی، و «@» ممکنه رویِ بعضی پنل‌ها (که این
        # فیلد رو به‌عنوانِ بخشی از URLِ ساب‌لینک هم استفاده می‌کنن) مشکل‌ساز
        # بشه. برای همینه که حتیِ حالتِ idtype == "username" (پایین‌تر) هم
        # از اول همیشه بدونِ «@» بوده — این‌جا هم دقیقاً همون قرارداد رو
        # حفظ می‌کنیم؛ فقط ترتیبِ افتادنِ فالبک عوض شده (یوزرنیم قبل از آیدیِ عددی).
        _custom = _re.sub(r"[^a-zA-Z0-9_]", "", custom_label or "")[:20]
        if _custom:
            ident = _custom
        elif tg_username:
            ident = _re.sub(r"[^a-zA-Z0-9_]", "", tg_username)[:20] or str(tg_id)
        else:
            ident = str(tg_id)
    elif idtype == "username" and tg_username:
        ident = _re.sub(r"[^a-zA-Z0-9_]", "", tg_username)[:20] or str(tg_id)
    else:
        ident = str(tg_id)
    return f"{base}{ident}"



def rial(n) -> str:
    return f"{int(n):,} تومان"


async def _panel_client(panel: VpnPanel) -> PanelClient:
    # PATCH_PANEL_CLIENT_POOL_V2 — این هم به همان استخر می‌رود
    return _client_from_panel(panel)


def _qr_bytes(data: str, bg: bytes | None = None) -> bytes | None:
    # === PATCH_QR_BACKGROUND_V1 ===
    # اگر مالکِ ربات عکسِ پس‌زمینه ثبت کرده باشد، بارکد رویِ همان عکس ساخته
    # می‌شود؛ وگرنه دقیقاً همان بارکدِ سادهٔ قبلی.
    from tenant.shared.qr_brand import render
    return render(data, bg)


async def _qr_png(bot_id: int, data: str) -> bytes | None:
    """نسخهٔ async که پس‌زمینهٔ همین ربات را هم می‌خواند."""
    from tenant.shared.qr_brand import render_for
    return await render_for(bot_id, data)


# === PATCH_WG_SINGLE_BUTTON_V1 ===
# بعضی پنل‌ها (مثلاً بعضی نسخه‌های پنلِ متصل به این ربات) دیگر محتوایِ خامِ
# INI (شروع با «[Interface]») برنمی‌گردانند، بلکه یک URI به فرمِ
# wireguard://privkey@host:port/?publickey=...&address=...&... می‌دهند.
# این تابع آن را به فرمتِ استانداردِ .conf تبدیل می‌کند تا کلاینت‌هایِ
# WireGuard (که فقط فایلِ .conf ایمپورت می‌کنند، نه این URI) بتوانند
# مستقیم از آن استفاده کنند.
def _parse_wireguard_uri(uri: str) -> str | None:
    from urllib.parse import urlparse, parse_qs, unquote
    try:
        u = urlparse(uri)
        if u.scheme != "wireguard":
            return None
        private_key = unquote(u.username or "")
        host = u.hostname or ""
        port = u.port
        # === PATCH_HEIMDALL_PANEL_V1 === کلیدهایِ query بدونِ توجه به حروفِ بزرگ/کوچک (HEIMDALL از publicKey/allowedIPs
        # استفاده می‌کند؛ مرزبان از publickey/allowedips) و نام‌هایِ هم‌معنی
        _alias = {"public_key": "publickey", "pbk": "publickey", "preshared_key": "presharedkey", "psk": "presharedkey",
                  "allowed_ips": "allowedips", "persistentkeepalive": "keepalive", "persistent_keepalive": "keepalive"}
        qs: dict = {}
        for _k, _v in parse_qs(u.query).items():
            _kl = _k.lower()
            qs.setdefault(_alias.get(_kl, _kl), _v)
        def _q(key, default=""):
            v = qs.get(key)
            return unquote(v[0]) if v else default
        public_key = _q("publickey")
        address = _q("address")
        mtu = _q("mtu")
        allowed_ips = _q("allowedips", "0.0.0.0/0")
        keepalive = _q("keepalive")
        dns = _q("dns")
        # === PATCH_WG_SUB_FALLBACK_V1 === presharedkey قبلاً دور ریخته می‌شد
        preshared = _q("presharedkey") or _q("preshared_key")
        if not (private_key and public_key and address and host and port):
            return None
        # === MARKER_WG_FRIENDLY_NAME_V1 ===
        # پنل معمولاً اسمِ واقعیِ لوکیشن (همونی که تویِ صفحهٔ ساب دیده
        # می‌شه، مثلِ «Turkey1-Gaming») رو به‌صورتِ فرگمنتِ #name تویِ
        # همین لینکِ wireguard:// می‌ذاره — قبلاً کلاً نادیده گرفته
        # می‌شد. اگه باشه، به‌عنوانِ یه کامنتِ اولِ فایل نگه‌ش می‌داریم
        # تا برایِ اسمِ فایل/کپشن استفاده بشه؛ وایرگارد خطِ # رو
        # نادیده می‌گیره پس ایمپورت شکسته نمی‌شه.
        _name = unquote(u.fragment or "").strip()
        # === PATCH_WG_SUB_FORMAT_PARITY_V1 === قالبِ خروجی دقیقاً مثلِ فایلِ خودِ ساب:
        # «# Name = X»، و فاصله بعد از ویرگول در DNS/AllowedIPs.
        def _sp(v):
            return ", ".join(x.strip() for x in str(v).split(",") if x.strip())
        dns, allowed_ips = _sp(dns), _sp(allowed_ips)
        lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]
        if dns:
            lines.append(f"DNS = {dns}")
        if mtu:
            lines.append(f"MTU = {mtu}")
        lines += ["", "[Peer]", f"PublicKey = {public_key}"]
        if preshared:  # === PATCH_WG_SUB_FALLBACK_V1 ===
            lines.append(f"PresharedKey = {preshared}")
        lines += [f"AllowedIPs = {allowed_ips}", f"Endpoint = {host}:{port}"]
        if keepalive:
            lines.append(f"PersistentKeepalive = {keepalive}")
        text = "\n".join(lines)
        if _name:
            text = f"# Name = {_name}\n" + text
        return text
    except Exception:
        logger.exception("پارسِ wireguard:// ناموفق بود")
        return None


_WG_INI_RE = re.compile(r"^\s*\[Interface\]", re.IGNORECASE | re.MULTILINE)


def _extract_wireguard_configs(links: list[str]) -> list[str]:
    """از لیستِ خامِ لینک‌های یک اکانت، فقط آیتم‌هایِ وایرگارد را (چه INIِ
    خام، چه اسکیمِ wireguard://) استخراج و به فرمتِ .conf تبدیل می‌کند."""
    out = []
    for link in (links or []):
        s = (link or "").strip()
        if not s:
            continue
        if _WG_INI_RE.search(s):          # === PATCH_WG_SPLIT_SUB_BLOB_V1 === «# Name:» قبلِ [Interface] هم مجاز
            out.append(s)
        elif s.startswith("wireguard://"):
            parsed = _parse_wireguard_uri(s)
            if parsed:
                out.append(parsed)
    return out


# === PATCH_WG_ENDPOINT_LABEL_V1 ===
# گزارشِ ادمین: وقتی یک اکانت چند کانفیگِ وایرگارد دارد (چند لوکیشن/سرور)،
# فایل‌ها فقط «wireguard_1.conf»، «wireguard_2.conf»، … نام‌گذاری می‌شدند و
# کپشن هم فقط شماره داشت — مشتری هیچ نشانه‌ای از اینکه کدوم فایل مالِ کدوم
# کشور/سروره نداشت، مگر اینکه بره تو پنلِ وبِ ادمین (که مشتری اصلاً بهش
# دسترسی نداره) و از رویِ Endpoint حدس بزنه. خودِ متنِ کانفیگ همیشه یک خطِ
# «Endpoint = host:port» دارد که همون هاستِ اختصاصیِ همون لوکیشنه (مثلاً
# wiregerman.asan-ps.ir) — همین‌جا از خودِ فایل استخراجش می‌کنیم، بدونِ
# نیاز به هیچ تغییری در پنل یا نگاشتِ دستیِ اسمِ کشورها.
_WG_ENDPOINT_RE = re.compile(r"^\s*Endpoint\s*=\s*([^\s:]+)(?::(\d+))?", re.IGNORECASE | re.MULTILINE)


def _wg_endpoint_label(cfg_text: str) -> str | None:
    """میزبان (و در صورتِ وجود، پورت) رویِ خطِ Endpointِ یک کانفیگِ
    وایرگارد را برمی‌گرداند — مثلاً 'wiregerman.asan-ps.ir:51820'."""
    m = _WG_ENDPOINT_RE.search(cfg_text or "")
    if not m:
        return None
    host = m.group(1)
    port = m.group(2)
    return f"{host}:{port}" if port else host


# === MARKER_WG_FRIENDLY_NAME_V1 ===
_WG_NAME_RE = re.compile(r"^\s*#\s*Name\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _wg_conf_name(cfg_text: str) -> str | None:
    """اسمِ واقعیِ لوکیشن (مثلِ 'Turkey1-Gaming') اگه به‌صورتِ کامنتِ
    «# Name: ...» تویِ متنِ کانفیگ باشه (توسطِ _parse_wireguard_uri از
    فرگمنتِ لینکِ پنل اضافه می‌شه). اگه نبود None — یعنی این کانفیگ از
    یه لینکِ خام/بدونِ اسم اومده و باید مثلِ قبل از رویِ Endpoint حدس زد."""
    m = _WG_NAME_RE.search(cfg_text or "")
    return m.group(1).strip() if m else None


# === PATCH_WG_SHORT_FILENAME_V1 ===
# گزارشِ ادمین: نامِ فایلِ قبلی («wireguard_12_wirearman2_asan-ps_ir.conf»)
# باعثِ خطایِ ایمپورت تویِ اپِ WireGuard می‌شد. کلاینت‌هایِ وایرگارد
# (به‌خصوص wg-quick روی لینوکس) نامِ اینترفیس را از رویِ همین اسمِ فایل
# می‌سازند و به IFNAMSIZ-1 یعنی حداکثر ۱۵ کاراکتر محدودند — قبلاً کلِ
# دامنه (شاملِ پسوندِ مشترکِ «asan-ps.ir» که بینِ همهٔ لوکیشن‌ها یکسان و
# بی‌فایده بود) داخلِ اسمِ فایل می‌رفت. الان فقط زیردامنهٔ متمایزکننده
# (مثلاً «wirearman2») گرفته می‌شود و کلِ اسمِ فایل (بدونِ .conf) به ۱۵
# کاراکتر محدود می‌ماند.
_WG_IFNAME_MAX = 15


def _wg_short_filename(i: int, cfg_text: str) -> str:
    """نامِ فایلِ کوتاهِ امن برایِ آیتمِ iامِ یک اکانتِ چند-لوکیشنه —
    حداکثر ۱۵ کاراکتر (بدونِ .conf)."""
    # === MARKER_WG_FRIENDLY_NAME_V1 ===
    # اگه اسمِ واقعیِ لوکیشن رو داریم (از فرگمنتِ لینکِ پنل، مثلِ
    # «Turkey1-Gaming») همونو ترجیح می‌دیم؛ فقط وقتی نبود برمی‌گردیم
    # به حدسِ قدیمی از رویِ Endpoint (که برایِ آی‌پیِ خام بی‌معنی بود).
    name = _wg_conf_name(cfg_text)
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "", name.replace(" ", "-")) if name else ""
    if not slug:
        label = _wg_endpoint_label(cfg_text)
        if label:
            host = label.split(":", 1)[0]
            first_label = host.split(".", 1)[0]  # فقط زیردامنهٔ متمایزکننده، نه کلِ دامنه
            slug = re.sub(r"[^a-zA-Z0-9\-]+", "", first_label)
    prefix = f"wg{i}"
    if not slug:
        return prefix[:_WG_IFNAME_MAX]
    budget = _WG_IFNAME_MAX - len(prefix) - 1  # −۱ برایِ جداکنندهٔ «-»
    if budget < 2:
        return prefix[:_WG_IFNAME_MAX]
    return f"{prefix}-{slug[:budget]}"


# === PATCH_WG_URL_FETCH_V1 ===
# گزارشِ ادمین: پنل به‌جایِ متنِ خامِ INI یا wireguard://، یک آدرسِ
# HTTP(S) برمی‌گرداند که خودش فایلِ .conf را سرو می‌کند (با بازکردنش در
# مرورگر دانلود می‌شود). دکمهٔ کانفیگِ تکیِ وایرگارد این حالت را
# نمی‌شناخت و «این سرویس کانفیگ وایرگارد ندارد» می‌داد.
async def _fetch_wg_conf_from_url(url: str) -> str | None:
    """اگر url یک آدرسِ HTTP(S) باشد که فایلِ .conf وایرگارد را سرو
    می‌کند، محتوایش را می‌گیرد؛ در غیرِ این‌صورت None."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    import aiohttp as _aio
    try:
        _t = _aio.ClientTimeout(total=15)
        _h = {"User-Agent": "wireguard-client", "Accept": "*/*"}
        async with _aio.ClientSession(timeout=_t, headers=_h) as _s2:
            async with _s2.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning(
                        "دانلودِ فایلِ کانفیگِ وایرگارد از لینک ناموفق بود (%s): %s",
                        resp.status, url)
                    return None
                text = await resp.text(errors="ignore")
    except Exception:
        logger.exception("دانلودِ فایلِ کانفیگِ وایرگارد از لینک ناموفق بود: %s", url)
        return None
    text = (text or "").strip()
    return text if "[Interface]" in text else None


async def _extract_wireguard_configs_async(links: list[str]) -> list[str]:
    """مثلِ _extract_wireguard_configs، ولی وقتی هیچ‌کدام از لینک‌ها
    خودشان INIِ خام یا wireguard:// نبودند، لینک‌هایِ HTTP(S) را دانلود
    می‌کند تا کانفیگِ واقعی را از داخلِ فایل استخراج کند."""
    out = _extract_wireguard_configs(links)
    if out:
        return out
    for link in (links or []):
        s = (link or "").strip()
        if not s:
            continue
        fetched = await _fetch_wg_conf_from_url(s)
        if fetched:
            out.append(fetched)
    return out


# === PATCH_WG_SUB_FALLBACK_V1 ===
async def _links_with_sub_fallback(client, email: str, sub_link: str) -> list[str]:
    """get_client_links از API پنل؛ اگر شکست خورد (مثلاً ۴۰۳ِ کلادفلر روی لاگین)
    یا خالی بود، محتوایِ ساب‌لینکِ ذخیره‌شدهٔ اکانت (بدونِ لاگین) خوانده می‌شود.
    اگر فالبک هم نتیجه نداد، خطای اولیه (در صورتِ وجود) دوباره بالا می‌آید."""
    first_exc = None
    try:
        links = await client.get_client_links(email)
        if links:
            return links
    except Exception as e:  # noqa: BLE001
        first_exc = e
        logger.exception("get_client_links از API پنل شکست خورد؛ فالبک به ساب‌لینک")
    if sub_link and hasattr(client, "_fetch_links_from_sub"):
        try:
            fb = await client._fetch_links_from_sub(sub_link)
            if fb:
                return fb
        except Exception:  # noqa: BLE001
            logger.exception("فالبکِ خواندنِ ساب‌لینک هم شکست خورد")
    if first_exc is not None:
        raise first_exc
    return []


import re as _re


# === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
async def panel_default_limit_ip(bot_id: int, panel_key: str | None) -> int:
    """محدودیتِ پیش‌فرضِ IP که رویِ خودِ پنل تنظیم شده.

    برایِ خریدهایی که پلن ندارند (حجم/روزِ دلخواه) تنها منبعِ محدودیت
    همین است؛ برایِ خریدِ پلن، به‌عنوانِ fallback وقتی پلن عددِ خودش را
    ندارد استفاده می‌شود.
    """
    if not panel_key:
        return 0
    try:
        async with Session() as s:
            row = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.panel_key == panel_key))
        return int(getattr(row, "default_limit_ip", 0) or 0) if row else 0
    except Exception:                                        # noqa: BLE001
        logger.exception("خواندنِ محدودیتِ پیش‌فرضِ IP پنل ناموفق بود")
        return 0


# === PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1 ===
# درخواستِ ادمین: «on hold» (شروعِ زمان از اولین اتصال) تا الان فقط یک
# تنظیمِ سراسری برایِ کلِ ربات بود؛ ادمینی که چند پنل دارد می‌خواست فقط
# رویِ یکی‌شان (مثلاً پاسارگاد ۶) روشن باشد. حالا هر پنل می‌تواند مقدارِ
# خودش را داشته باشد؛ اگر برایِ یک پنل چیزی تنظیم نشده باشد، دقیقاً مثلِ
# قبل از تنظیمِ سراسری پیروی می‌کند — یعنی ربات‌هایِ موجود هیچ تغییرِ
# رفتاری نمی‌بینند تا وقتی که خودِ ادمین دکمهٔ یک پنل را بزند.
ONHOLD_PANELS = frozenset({"marzban", "rebecca", "pasargad", "pasarguard", "rickpanel"})


def purchase_on_hold_key(panel_key: str) -> str:
    return f"purchase_on_hold:{panel_key}"


async def purchase_on_hold_for(bot_id: int, panel_key: str | None) -> bool:
    """آیا کانفیگِ فروشیِ تازه رویِ این پنل باید on_hold ساخته شود؟"""
    from tenant import get_setting as _gs
    if panel_key:
        per = await _gs(bot_id, purchase_on_hold_key(panel_key), "")
        if per in ("0", "1"):
            return per == "1"
    return await _gs(bot_id, "purchase_on_hold", "0") == "1"


async def _card_both_extra(bot_id: int) -> str:   # PATCH_CARD_BOTH_V1
    from tenant.gateways.card_display import extra_card_block
    return await extra_card_block(bot_id)


async def _make_config_email(bot_id: int, panel_key: str, bot_username: str | None,
                              tg_id: int, tg_username: str | None = None,
                              custom_label: str | None = None) -> str:  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    """نام کوتاه برای کانفیگ در پنل: همان چیزی که به‌عنوان «نام سرور» در زمان
    خرید و در ساب‌لینک (داخل اپ کلاینت) به مشتری نمایش داده می‌شود.

    پیشوند و نوع شناسهٔ کاربر (یوزرنیم، آیدی عددی، یا نام دلخواهِ خودِ کاربر)
    از تنظیمات «نام سرورها مشتری» همین پنل خوانده می‌شود (پیش‌فرض: نام ربات
    + آیدی عددی، مثل قبل). وقتی idtype == "custom" است، custom_label (اسمی
    که کاربر همین موقعِ خرید وارد کرده) استفاده می‌شود.
    پنل سنایی نام یکتا می‌خواهد، پس یک پسوند کوتاه برای جلوگیری از تداخل اضافه می‌شود.
    """
    cfg = await get_srvname_cfg(bot_id, panel_key)
    base = build_srv_name(cfg["prefix"], cfg["idtype"], bot_username, tg_id, tg_username,
                          custom_label=custom_label)
    return f"{base}{gen_sub_id(3)}"


def _client_from_panel(panel) -> "PanelClient":
    """کلاینتِ پنل — از استخرِ نشست‌هایِ لاگین‌شده.

    === PATCH_PANEL_CLIENT_POOL_V2 ===
    قبلاً هر بار یک کلاینتِ تازه می‌ساخت، یعنی هر تمدید/حجمِ اضافه/تستِ
    رایگان/بازکردنِ «کانفیگ‌هایِ من» یک TLS + csrf + login از صفر داشت.
    این تابع تنها نقطهٔ ساختِ کلاینت در مسیرهایِ کاربر است (۱۴ فراخوانی)،
    پس عوض‌کردنِ همین یکی همهٔ آن مسیرها را روی استخر می‌برد.

    کلاینتِ برگشتی `close()`اش بی‌اثر است، پس آن ۱۴ نقطه که بعدِ کارشان
    close می‌زنند لازم نیست تغییر کنند و نشستِ مشترک را نمی‌بندند.
    """
    try:
        from tenant.shared.panel_clients import panel_pool as _pool
        return _pool.get_sync(
            panel.panel_key, panel.base_url, panel.username, panel.password,
            sub_base=panel.sub_base or "", inbound_ids_str=panel.inbound_ids,
            api_key=panel.api_key)
    except Exception:
        # استخر هرگز نباید مسیرِ کاربر را بشکند؛ در بدترین حالت رفتارِ قبلی.
        logger.exception("panel_pool در دسترس نبود؛ کلاینتِ تازه ساخته شد")
        return make_panel_client(panel)


async def _web_info_url(s, account: "PanelAccount") -> str | None:
    """لینک صفحهٔ وبِ «مشاهدهٔ زندهٔ کانفیگ» را می‌سازد (فقط برای کانفیگ‌های
    خودکار/پنلی). اگر آدرس پایهٔ وبهوک تنظیم نشده باشد، None برمی‌گرداند
    (و دکمه‌اش اصلاً نمایش داده نمی‌شود)."""
    if not WEBHOOK_BASE:
        return None
    tok = await ensure_web_token(s, account)
    return f"{WEBHOOK_BASE.rstrip('/')}/cfg/{tok}"


async def _panel_for_account(s, bot_id: int, account) -> "VpnPanel | None":
    """پنلِ درستِ یک اکانت را برمی‌گرداند: ابتدا بر اساس panel_key خودِ اکانت،
    و اگر ثبت نشده بود، اولین پنل فعالِ ربات (سازگاری با اکانت‌های قدیمی).

    این از باگِ «استفاده از پنل اشتباه» جلوگیری می‌کند: وقتی ربات چند پنل دارد،
    مدیریت/لینک/کانفیگ تکیِ هر اکانت باید روی همان پنلی انجام شود که اکانت روی
    آن ساخته شده — نه اولین پنل فعال.
    """
    pk = getattr(account, "panel_key", None)
    if pk:
        p = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == pk))
        if p:
            return p
    return await s.scalar(select(VpnPanel).where(
        VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))


async def new_svc_code(s, bot_id: int) -> str:
    """تولید کد اختصاصی ۹ رقمی یکتا برای سرویس (در محدودهٔ همان ربات)."""
    import random
    while True:
        code = "".join(random.choices("123456789", k=1) + random.choices("0123456789", k=8))
        exists = await s.scalar(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.svc_code == code))
        if not exists:
            return code


async def ensure_svc_code(s, a: "PanelAccount") -> str:
    """اگر اکانت (قدیمی) کد ندارد، همین‌جا یکی بساز و ذخیره کن (caller باید commit کند)."""
    if not a.svc_code:
        a.svc_code = await new_svc_code(s, a.bot_id)
    return a.svc_code


# === PATCH_UNLIMITED_LABEL_V1 ===
# باگ: کانفیگ‌های نامحدود (volume_gb = 0) در «سرویس‌های من» و صفحهٔ کانفیگ
# به‌صورت «۰ گیگ» نمایش داده می‌شدند و کاربرها فکر می‌کردند حجمشان تمام شده
# یا سرویس قطع است. _svc_label این را درست مدیریت می‌کرد، ولی کانفیگ‌های
# انبارِ دستی از مسیرِ دیگری رندر می‌شدند و از قلم افتاده بودند.
def fmt_vol(gb, mb=None) -> str:
    """برچسبِ حجم: صفر یعنی نامحدود، نه صفر گیگ."""
    if mb:
        return f"{int(mb)} مگابایت"
    gb = int(gb or 0)
    return "♾️ نامحدود" if gb == 0 else f"{gb} گیگ"


def _svc_label(plan_title: str | None, volume_gb: int, days: int,
               volume_mb: int | None = None) -> str:
    """نمایش خوانا: اگه اسم دلخواه داشت نشون بده، زیرش هم مشخصات.

    === PATCH_PANEL_ACCOUNT_VOLUME_MB_V1 ===
    volume_mb: اگه ست شده باشه (حجم زیرِ ۱ گیگ)، به‌جای «نامحدود»ِ
    نادرست (که از صفربودنِ volume_gb می‌اومد)، همون رقمِ دقیقِ مگابایت
    نشون داده می‌شه.
    """
    if volume_mb:
        vol = f"{volume_mb} مگابایت"
    else:
        vol = "نامحدود" if volume_gb == 0 else f"{volume_gb} گیگ"
    dur  = "بی‌انقضا" if days == 0 else f"{days} روز"
    spec = f"{vol} / {dur}"
    # اگه plan_title یه اسم کاربری باشه (نه فرمت خودکار مثل 30G / 30D)
    if plan_title and not _re.match(r'^\d+G\s*/\s*\d+D$', plan_title.strip()):
        return f"{plan_title}  ({spec})"
    return spec


try:
    import jdatetime as _jdt
except Exception:
    _jdt = None
import datetime as _dt
_GB = 1024 ** 3


def _jalali(dt) -> str:
    """تاریخ شمسی به وقت تهران از datetime (UTC فرض می‌شود)."""
    if not dt:
        return "—"
    try:
        local = dt + _dt.timedelta(hours=3, minutes=30)
        if _jdt:
            return _jdt.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d")
        return local.strftime("%Y/%m/%d")
    except Exception:
        return "—"


def _jalali_from_ms(ms: int) -> str:
    """تاریخ شمسی به وقت تهران از تایم‌استمپ میلی‌ثانیه (expiryTime پنل)."""
    if not ms:
        return "بدون انقضا"
    try:
        utc = _dt.datetime.utcfromtimestamp(int(ms) / 1000)
        return _jalali(utc)
    except Exception:
        return "—"


def _jalali_datetime_from_ms(ms: int) -> str:
    """تاریخ+ساعتِ شمسی به وقتِ تهران از تایم‌استمپِ میلی‌ثانیه — برایِ
    نمایشِ دقیقِ «آخرین اتصال» (بر خلافِ _jalali_from_ms که فقط تاریخه)."""
    if not ms:
        return "—"
    try:
        utc = _dt.datetime.utcfromtimestamp(int(ms) / 1000)
        local = utc + _dt.timedelta(hours=3, minutes=30)
        if _jdt:
            return _jdt.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d — %H:%M")
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _expires_at_from_ms(ms: int) -> _dt.datetime | None:
    """تبدیل expiry_ms برگشتی از create_account به datetime برای ذخیره در DB."""
    if not ms:
        return None
    try:
        return _dt.datetime.utcfromtimestamp(int(ms) / 1000)
    except Exception:
        return None


def _connection_status_line(usage: dict) -> str:
    """بخشِ «آخرین وضعیت اتصال» — فقط وقتی خودِ پنل واقعاً last_online_ms
    برگردانده باشه ساخته می‌شه (نه برایِ پنل‌هایی که این داده رو ندارن،
    مثلِ سنایی/۳X-UI یا مرزنشین)؛ یعنی هیچ‌وقت چیزی حدسی/جعلی نشون نمی‌ده."""
    last_ms = usage.get("last_online_ms")
    if not last_ms:
        return ""
    when = _jalali_datetime_from_ms(int(last_ms))
    app = (usage.get("client_app") or "").strip()
    # «آنلاین» = آخرین اتصال کمتر از ۳ دقیقه پیش (پنل‌ها lastOnline را حدودِ هر دقیقه به‌روز می‌کنند)
    import time as _tm
    _age = _tm.time() - int(last_ms) / 1000
    _live = 0 <= _age <= 180
    lines = ["", "🔌 <b>آخرین وضعیت اتصال</b>",
             "🟢 وضعیت: <b>آنلاین</b>" if _live else "⚪️ وضعیت: <b>آفلاین</b>",
             f"🕒 آخرین اتصال: <b>{when}</b>"]
    if app:
        lines.append(f"📱 برنامه: <b>{app}</b>")
    return "\n".join(lines)


def _usage_block(usage: dict, fallback_expiry: "_dt.datetime | None" = None) -> str:
    """بلوک کامل وضعیت زنده با نوار پیشرفت، مثل ربات سوپرا.

    fallback_expiry: تاریخ انقضای ذخیره‌شده در DB محلی (PanelAccount.expires_at)؛
    وقتی پنل expiryTime معتبر برنگرداند (مثلاً 0 پیش از اولین اتصال یا خطای
    شبکه) از همین استفاده می‌شود تا تاریخ انقضا هیچ‌وقت خالی/گمراه‌کننده نشود.
    """
    fallback_str = _jalali(fallback_expiry) if fallback_expiry else "بدون انقضا"
    if not usage:
        if fallback_expiry:
            return (f"⚠️ <i>اطلاعات زنده در دسترس نیست</i>\n"
                    f"⏳ انقضا (طبق ثبت خرید): <b>{fallback_str}</b>")
        return "⚠️ <i>اطلاعات زنده در دسترس نیست</i>"
    up = int(usage.get("up", 0) or 0)
    down = int(usage.get("down", 0) or 0)
    cap = int(usage.get("total", 0) or 0)
    used = up + down
    used_gb = round(used / _GB, 2)
    raw_expiry_ms = int(usage.get("expiryTime", 0) or 0)
    expiry = _jalali_from_ms(raw_expiry_ms) if raw_expiry_ms else fallback_str
    # === PATCH_ONHOLD_DISPLAY_V1 === سرویسِ «از اولین اتصال»: هنوز انقضایی ثبت نشده، ولی بی‌انقضا هم نیست
    _oh = int(usage.get("on_hold_seconds", 0) or 0)
    if _oh and not raw_expiry_ms:
        _d, _r = divmod(_oh, 86400)
        _h = _r // 3600
        _dur = (f"{_d} روز" if _d else "") + (f" و {_h} ساعت" if (_d and _h) else (f"{_h} ساعت" if _h else ""))
        expiry = f"⏸ از اولین اتصال شروع می‌شود ({_dur.strip() or '—'})"
    conn_line = _connection_status_line(usage)
    if cap:
        cap_gb = round(cap / _GB, 2)
        left_gb = round(max(cap - used, 0) / _GB, 2)
        percent = min(int(used / cap * 100), 100) if cap else 0
        filled = percent // 10
        bar = "🟥" * filled + "⬜️" * (10 - filled)
        return (
            "📊 <b>وضعیت زنده (از سرور)</b>\n"
            f"✅ مصرف‌شده: <b>{used_gb} GB</b>\n"
            f"💾 باقی‌مانده: <b>{left_gb} GB</b>\n"
            f"📦 کل حجم: <b>{cap_gb} GB</b>\n"
            f"{bar}  {percent}٪\n"
            f"⏳ انقضا: <b>{expiry}</b>"
            f"{conn_line}"
        )
    return (
        "📊 <b>وضعیت زنده (از سرور)</b>\n"
        f"✅ مصرف‌شده: <b>{used_gb} GB</b>\n"
        "📦 حجم: <b>نامحدود</b>\n"
        f"⏳ انقضا: <b>{expiry}</b>"
        f"{conn_line}"
    )



async def _safe_edit(cb: CallbackQuery, text: str, reply_markup=None):
    """ویرایش امن پیام — اگه پیام عکس باشه از edit_caption استفاده میکنه."""
    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        # پیام عکس: caption رو ویرایش کن، QR دست نخوره
        try:
            await cb.message.edit_caption(caption=text, reply_markup=reply_markup)
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # پیام متنی معمولی
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(text, reply_markup=reply_markup)


# === PATCH_PROGRESS_EDIT_MEDIA_V1 ===
# لاگ: «there is no text in the message to edit» (۷۶ بار). پیامِ کانفیگ عکسِ QR است
# و edit_text رویِ پیامِ عکس‌دار همیشه خطا می‌دهد => کاربر پیامِ «در حال انجام...» را
# نمی‌دید و دوباره دکمه می‌زد. این تابع رویِ پیامِ عکس‌دار از edit_caption استفاده
# می‌کند و خطاهایِ بی‌ضرر («not modified») را بی‌سروصدا رد می‌کند.
async def _progress_edit(cb: CallbackQuery, text: str) -> None:
    try:
        if has_media(cb.message):
            await cb.message.edit_caption(caption=text, reply_markup=None)
        else:
            await cb.message.edit_text(text)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        logger.warning("نمایشِ پیامِ پیشرفت ناموفق بود: %s", e)


# === PATCH_DELIVERY_FIRST_V1 ===
# گزارش #145 (Sajad): «تحویلِ کانفیگ خیلی دیر می‌رسد». علت (غیر از کندیِ پنل): بعد از ساخته‌شدنِ سرویس،
# اول «پاداشِ دعوت» و «اطلاعِ خریدِ ادمین/کانال» (چند پیامِ تلگرام، هرکدام تا ۱۲ثانیه×۳ تلاش) انجام می‌شد
# و تازه بعدش کانفیگ برایِ مشتری می‌رفت. این‌ها نباید جلویِ تحویل را بگیرند.
def _bg(coro, what: str) -> None:
    async def _run():
        try:
            await coro
        except Exception:
            logger.exception("کارِ پس‌زمینه ناموفق بود: %s", what)
    asyncio.ensure_future(_run())


async def _send_config_delivery(bot: Bot, tg_id: int, aid: int):
    """تحویل کانفیگ: QR ساب لینک + مشخصات و دکمه‌ها در یک پیام."""
    async with Session() as s:
        a = await s.get(PanelAccount, aid)
        if not a:
            return
        vol      = (f"{a.volume_mb} مگابایت" if a.volume_mb
                    else ("نامحدود" if a.volume_gb == 0 else f"{a.volume_gb} گیگ"))
        dur      = "بی‌انقضا" if a.days == 0 else f"{a.days} روز"
        # === PATCH_EXPIRY_FALLBACK_V1 ===
        # در پیامِ تحویل «تاریخ انقضا: —» دیده شد. علتش این است که بعضی
        # مسیرها (مثلِ تستِ رایگان) `expires_at` را پر نمی‌کنند و کاربر
        # خط بی‌معنی می‌بیند. وقتی مدت مشخص است، تاریخ از رویِ زمانِ ساخت
        # قابلِ محاسبه است — پس دلیلی ندارد خالی بماند.
        _exp = a.expires_at
        if _exp is None and a.days and a.created_at:
            _exp = a.created_at + _dt.timedelta(days=a.days)
        exp_line = ("" if a.days == 0 else
                    f"📅 تاریخ انقضا: <b>{_jalali(_exp)}</b>\n")
        title    = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)
        sub_link = a.sub_link or ""
        acc_bot_id = a.bot_id
        acc_panel_key = a.panel_key or ""
        acc_email = a.email
        web_url  = await _web_info_url(s, a)
        await s.commit()

    # یادداشت دلخواه ادمین برای این پنل (در توضیحات تحویل کانفیگ)
    deliver_note = ""
    if acc_panel_key:
        try:
            from tenant.bots.vpn.vpn_admin import get_panel_note
            _dn = await get_panel_note(acc_bot_id, acc_panel_key, "delivery")
            if _dn:
                deliver_note = f"\n📝 {_dn}\n"
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    body = (
        f"✅ <b>کانفیگ آماده شد!</b>\n\n"
        f"🛰 سرویس: <b>{title}</b>\n"
        f"📦 حجم: <b>{vol}</b>\n"
        f"📅 مدت: <b>{dur}</b>\n"
        f"{exp_line}"
        f"👤 نام کاربری پنل: <code>{acc_email}</code>\n"
        + (f"\n🔗 ساب لینک:\n<code>{sub_link}</code>\n" if sub_link else "")
        + deliver_note
        + "\nبرای مدیریت از دکمه‌های زیر استفاده کن 👇"
    )
    kb_rows = [
        [InlineKeyboardButton(text="🔗 ارسال ساب لینک", callback_data=f"vget:sub:{aid}"),
         InlineKeyboardButton(text="📦 کانفیگ‌های تکی", callback_data=f"vget:single:{aid}")],
        [InlineKeyboardButton(text="✏️ نام‌گذاری کانفیگ", callback_data=f"vlabel:{aid}")],
        [InlineKeyboardButton(text="📡 مشاهده در کانفیگ‌های من", callback_data=f"vsvc:{aid}")],
    ]

    # === PATCH_WG_DELIVERY_BUTTON_V1 ===
    # دکمهٔ وایرگارد فقط وقتی نشان داده می‌شود که اکانت واقعاً کانفیگِ
    # وایرگارد داشته باشد (چک زنده از پنل)؛ برای اینباندهایِ غیرِوایرگارد
    # این دکمه اصلاً اضافه نمی‌شود تا کاربر را گیج نکند.
    # === PATCH_DELIVERY_WG_NONBLOCKING_V1 ===
    # گزارش #145 (Sajad): «تحویلِ کانفیگ پاسارگارد و یوپنل خیلی دیر می‌رسد». این چکِ فقط-برایِ-یک-دکمهٔ-جانبی
    # تا ۴ ثانیه (به‌علاوهٔ لاگین/دریافتِ ساب از پنلِ کند) جلویِ ارسالِ خودِ کانفیگ را می‌گرفت.
    # حالا هم‌زمان با ساختنِ پیام اجرا می‌شود؛ اگر تا ۰٫۶ ثانیه نرسید، کانفیگ همان لحظه ارسال می‌شود
    # و دکمهٔ وایرگارد (اگر لازم بود) بعداً به همان پیام اضافه می‌شود.
    async def _wg_check() -> bool:
        try:
            return await _wg_check_inner()
        except (asyncio.TimeoutError, TimeoutError):
            # پنلِ کند؛ فقط دکمهٔ جانبیِ وایرگارد نمایش داده نمی‌شود — خطا نیست
            logger.info("چکِ وایرگارد در پیامِ تحویل به تایم‌اوت خورد (aid=%s)", aid)
            return False
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (چکِ دکمهٔ وایرگارد در پیامِ تحویل)")
            return False

    async def _wg_check_inner() -> bool:
        async with Session() as _s_wg:
            _a_wg = await _s_wg.get(PanelAccount, aid)
            _panel_wg = await _panel_for_account(_s_wg, acc_bot_id, _a_wg) if _a_wg else None
        if not _panel_wg:
            return False
        from tenant.shared.panel_clients import panel_pool as _wpool
        _client_wg = _wpool.get_sync(
            _panel_wg.panel_key, _panel_wg.base_url, _panel_wg.username,
            _panel_wg.password, sub_base=_panel_wg.sub_base or "",
            inbound_ids_str=_panel_wg.inbound_ids, api_key=_panel_wg.api_key)
        try:
            # PATCH_WG_FILES_FIRST_V1: پنل‌هایِ دارایِ مسیرِ بومی ممکن است در لینک‌ها وایرگارد نداشته باشند
            if hasattr(_client_wg, "get_wireguard_endpoints"):
                try:
                    if await asyncio.wait_for(_client_wg.get_wireguard_endpoints(acc_email), timeout=6):
                        return True
                except Exception:
                    logger.debug("چکِ لوکیشن‌هایِ بومیِ وایرگارد در تحویل ممکن نشد", exc_info=True)
            _links_wg = await asyncio.wait_for(
                _links_with_sub_fallback(_client_wg, acc_email, sub_link), timeout=6)
        finally:
            await _client_wg.close()
        return bool(await _extract_wireguard_configs_async(_links_wg))

    _wg_task = asyncio.ensure_future(_wg_check())
    _wg_row = [InlineKeyboardButton(text="🛡 دریافت کانفیگ‌هایِ وایرگارد",
                                    callback_data=f"vwgcfg:{aid}")]

    try:
        from tenant import inline_back_row, _cfg_guide_row
        _gr = await _cfg_guide_row(acc_bot_id, a.panel_key or "sanai")
        if _gr:
            kb_rows.append(_gr)
        _br = await inline_back_row(acc_bot_id)
        if _br:
            kb_rows.append(_br)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_DELIVERY_WG_NONBLOCKING_V1 ===
    _wg_pending = False
    # === PATCH_WG_FILES_FIRST_V1 ===
    # درخواستِ ادمین: هر کس وایرگارد خرید، موقعِ تحویل اول فایل‌هایِ .conf را بگیرد (بعد کارتِ سرویس).
    # تا ۲٫۵ثانیه برایِ تشخیصِ وایرگارد صبر می‌کنیم (سرویس‌هایِ غیرِ وایرگارد معمولاً زیرِ ۱ثانیه جواب می‌گیرند)؛
    # اگر دیرتر شد، کارت همان لحظه می‌رود و فایل‌ها بلافاصله بعدِ آن ارسال می‌شوند.
    _wg_files_sent = False
    try:
        if await asyncio.wait_for(asyncio.shield(_wg_task), timeout=2.5):
            kb_rows.insert(3, _wg_row)
            await _send_wg_files_for_account(bot, tg_id, aid)
            _wg_files_sent = True
    except asyncio.TimeoutError:
        _wg_pending = True
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (چکِ دکمهٔ وایرگارد در پیامِ تحویل)")

    async def _late_wg_button(sent_msg) -> None:
        """چکِ وایرگارد بعد از ارسال تمام شد: اگر وایرگارد داشت، دکمه را به همان پیام اضافه کن و فایل‌ها را بفرست."""
        try:
            has_wg = bool(await _wg_task)
        except Exception:
            has_wg = False
        if not has_wg:
            return
        try:
            _mk = getattr(sent_msg, "reply_markup", None)
            _rows = [list(r) for r in (_mk.inline_keyboard if _mk else kb_rows)]
            if not any(getattr(b, "callback_data", "") == f"vwgcfg:{aid}" for r in _rows for b in r):
                _rows.insert(min(3, len(_rows)), _wg_row)
                await bot.edit_message_reply_markup(
                    chat_id=tg_id, message_id=sent_msg.message_id,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=_rows))
        except Exception:
            logger.debug("افزودنِ دیرهنگامِ دکمهٔ وایرگارد ممکن نشد (بی‌اهمیت)")
        # PATCH_WG_FILES_FIRST_V1: چک دیر تمام شد ⇒ فایل‌ها بلافاصله بعدِ کارت می‌آیند
        await _send_wg_files_for_account(bot, tg_id, aid)

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    # اگه ساب لینک داشت: QR ساب‌لینک + متن در یک پیام (caption)
    if sub_link:
        try:
            png = await _qr_png(acc_bot_id, sub_link)
            _sent = await bot.send_photo(
                tg_id,
                BufferedInputFile(png, filename="vpn_qr.png"),
                caption=body,
                reply_markup=kb)
            if _wg_pending:
                asyncio.ensure_future(_late_wg_button(_sent))
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        # بدون ساب‌لینک (مثلاً چون آدرس ساب برای این پنل تنظیم نشده): برای اینکه
        # کاربر بازهم QR بگیرد، از روی اولین لینکِ کانفیگِ تکی (زندهٔ پنل) QR بساز
        try:
            async with Session() as s:
                a2 = await s.get(PanelAccount, aid)
                panel = await _panel_for_account(s, acc_bot_id, a2) if a2 else None
            if panel and a2:
                client = _client_from_panel(panel)
                try:
                    links = await client.get_client_links(a2.email)
                finally:
                    await client.close()
                if links:
                    png = await _qr_png(acc_bot_id, links[0])
                    _sent = await bot.send_photo(
                        tg_id,
                        BufferedInputFile(png, filename="vpn_qr.png"),
                        caption=body,
                        reply_markup=kb)
                    if _wg_pending:
                        asyncio.ensure_future(_late_wg_button(_sent))
                    return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # بدون ساب لینک/کانفیگ تکی یا اگه send_photo خطا داد
    _sent = await bot.send_message(tg_id, body, reply_markup=kb)
    if _wg_pending:
        asyncio.ensure_future(_late_wg_button(_sent))


# === PATCH_BTN_VPN_CATEGORY_FILTER_V1 ===
_NO_CAT = "-"          # کلیدِ داخلیِ «بدونِ دسته‌بندی»


# === PATCH_BTN_HIDE_ALL_PLANS_V1 ===
NONE_CAT = "__none__"  # مقدارِ ذخیره‌شده برایِ «هیچ پلنی نمایش داده نشود»


async def allowed_categories(bot_id: int, mb_id: int) -> set[str] | None:
    """دسته‌بندی‌هایی که این دکمه اجازهٔ نمایششان را دارد.

    خروجی None یعنی «محدودیتی تنظیم نشده» → همهٔ دسته‌ها نمایش داده شوند.
    مقدارِ ذخیره‌شده یک رشتهٔ جداشده با «|» است؛ «-» یعنی پلن‌هایِ بدونِ دسته.
    """
    from tenant import get_setting
    raw = (await get_setting(bot_id, f"btn_vpncats_{mb_id}", "")).strip()
    if not raw:
        return None
    # === PATCH_BTN_HIDE_ALL_PLANS_V1 ===
    # مقدارِ ویژهٔ NONE_CAT یعنی «هیچ پلنی نشان داده نشود» — دکمه فقط
    # فروشِ آنلاین (حجم/روزِ دلخواه) را نمایش می‌دهد. مجموعهٔ خالی لازم
    # است نه None، چون None یعنی «فیلتری تنظیم نشده».
    if raw == NONE_CAT:
        return set()
    return {c for c in raw.split("|") if c}


async def all_categories(bot_id: int, panel_key: str | None = None) -> list[str]:
    """فهرستِ دسته‌بندی‌هایِ موجود (به‌علاوهٔ «بدونِ دسته» اگر پلنی بدونِ دسته باشد)."""
    async with Session() as s:
        q = select(VpnPlan).where(VpnPlan.bot_id == bot_id)
        if panel_key:
            q = q.where(VpnPlan.panel_key == panel_key)
        plans = (await s.scalars(q.order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    out: list[str] = []
    has_plain = False
    for p in plans:
        cat = (p.category or "").strip()
        if not cat:
            has_plain = True
        elif cat not in out:
            out.append(cat)
    if has_plain:
        out.append(_NO_CAT)
    return out


# ─────────────── نمایش پلن‌ها ───────────────
# === PATCH_RESELLER_PLANS_V1 ===
# پلن‌ها می‌توانند مخصوصِ نماینده یا مخصوصِ مشتریِ عادی باشند. این فیلتر
# در همهٔ جاهایی که فهرستِ پلن ساخته می‌شود اعمال می‌شود تا نماینده منویِ
# خودش را ببیند و مشتریِ عادی پلن‌هایِ نمایندگی را نبیند.
async def _plan_allowed_for(plan, bot_id: int, telegram_id: int | None) -> bool:
    """=== PATCH_PLAN_AUDIENCE_ENFORCE_V1 ===
    فیلترِ مخاطب فقط رویِ *نمایشِ* فهرست اعمال می‌شد؛ مسیرهایِ پرداخت با
    plan_idِ دست‌ساز پلنِ «فقط نماینده» (معمولاً ارزان‌تر) را هم به مشتریِ
    عادی می‌فروختند. همان قاعدهٔ _plan_audience_filter، برایِ یک پلن."""
    aud = (getattr(plan, "audience", None) or "all")
    if aud == "all":
        return True
    _cond, is_rs = await _plan_audience_filter(bot_id, telegram_id)
    return aud == ("resellers" if is_rs else "users")


async def _plan_audience_filter(bot_id: int, telegram_id: int | None):
    """شرطِ SQL برایِ مخاطبِ درست."""
    from models import VpnPlan
    is_rs = False
    if telegram_id:
        try:
            from models import VpnPartner
            async with Session() as s:
                p = await s.scalar(select(VpnPartner).where(
                    VpnPartner.bot_id == bot_id,
                    VpnPartner.telegram_id == telegram_id))
            is_rs = bool(p and p.active)
        except Exception:
            logger.exception("تشخیصِ نمایندگی ناموفق بود؛ پلن‌هایِ عمومی نشان داده می‌شود")
    allowed = ["all", "resellers"] if is_rs else ["all", "users"]
    # === PATCH_PLAN_AUDIENCE_NULL_FIX_V1 ===
    # باگِ فوری: ستونِ audience با مهاجرتِ خودکار اضافه شد ولی ردیف‌هایِ
    # *موجود* مقدارِ NULL گرفتند (DDLِ مهاجرت DEFAULT نمی‌گذارد). چون
    # NULL در IN(...) هیچ‌وقت True نمی‌شود، همهٔ پلن‌هایِ قدیمی از فهرست
    # حذف شدند و دکمهٔ خرید عملاً خالی می‌شد. NULL یعنی «همه».
    from sqlalchemy import or_ as _or
    return _or(VpnPlan.audience.is_(None), VpnPlan.audience.in_(allowed)), is_rs


# === PATCH_RESELLER_TITLE_FA_V1 ===
# عنوانِ کانفیگ همه‌جا دیده می‌شود (سرویس‌های من، کانالِ اعتمادسازی، رسید).
# «5G / 365D» وصله‌ای انگلیسی وسطِ متنِ فارسی بود.
# === PATCH_PLAN_USERS_LABEL_V1 ===
_USERS_FA = {1: "تک‌کاربره", 2: "دو کاربره", 3: "سه کاربره", 4: "چهار کاربره", 5: "پنج کاربره"}


def _users_lbl(n) -> str:
    """برچسبِ تعدادِ کاربرِ هم‌زمانِ یک پلن (limit_ip)؛ برایِ ۰ / نامشخص، رشتهٔ خالی."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return _USERS_FA.get(n, f"{n} کاربره")


def _plan_title_fa(gb, days) -> str:
    try:
        g = float(gb)
    except (TypeError, ValueError):
        g = 0
    vol = "نامحدود" if g <= 0 else (
        f"{int(round(g * 1024))} مگ" if g < 1 else
        f"{int(g) if g == int(g) else g} گیگ")
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 0
    dur = "بی‌انقضا" if d <= 0 else f"{d} روز"
    return f"{vol} / {dur}"


async def show_vpn_plans(bot: Bot, msg: Message, bot_id: int, state=None, panel_key: str | None = None,
                         header: str = "", mb_id: int | None = None,
                         miniapp_category_id: int | None = None):
    """نمایش پلن‌های خرید VPN.
    اگر panel_key داده شده، فقط همان پنل بررسی می‌شود؛ وگرنه اولین پنل فعال.
    header (اختیاری): متنی که به‌عنوان توضیحِ بالای صفحه (از متنِ دکمه) نمایش داده می‌شود.
    mb_id (اختیاری): شناسهٔ دکمه‌ای که این صفحه را باز کرده؛ در state ذخیره می‌شود تا
    کدهای تخفیفِ اختصاصیِ همین دکمه (DiscountCode.button_id) بتوانند در ادامهٔ خرید چک شوند.
    miniapp_category_id (اختیاری) — PATCH_BUNDLE_BUTTON_V1: اگر ست شود، فقط پلن‌هایی
    که به این دستهٔ ترکیبی وصل‌اند نشان داده می‌شوند (برایِ «دکمهٔ خریدِ ترکیبی» که
    چند پنل و چند دستهٔ انبار را زیرِ یک دکمه جمع می‌کند).
    """
    await _drop_reply_kb(msg, bot_id)      # PATCH_FLOW_DROP_REPLY_KB_V1
# === PATCH_FREE_NO_EXPIRY_PANEL_LOCK_NOTIFY_V1 ===
    from tenant import inline_back_row, inline_back_kb, get_bot_row
    from tenant.bots.vpn.vpn_admin import get_panel, get_any_enabled_panel
    from plans import has_feature, Feature
    _row_check = await get_bot_row(bot)
    if not _row_check or not has_feature(_row_check.plan, Feature.PANEL_INTEGRATION):
        # پلنِ فعلیِ ربات (مثلاً بعد از تنزل به رایگان) دیگر دسترسیِ «پنل
        # خودکار» ندارد، حتی اگر پنلی قبلاً برایش متصل/فعال شده باشد.
        await msg.answer("⛔️ فروش VPN خودکار در پلن فعلی این ربات در دسترس نیست.",
                         reply_markup=await inline_back_kb(bot_id))
        return
    if state is not None:
        # PATCH_VBUYBACK_KEEP_CONTEXT_V1 — panel_key هم لازم است تا دکمهٔ
        # «بازگشت»ِ پیش‌فاکتور بتواند دقیقاً همین صفحه را بازسازی کند.
        await state.update_data(vbuy_mb_id=mb_id, vbuy_panel_key=panel_key or "", vbuy_reopen=None)
    if panel_key:
        panel = await get_panel(bot_id, panel_key)
    else:
        panel = await get_any_enabled_panel(bot_id)
        # === PATCH_VPN_PLANS_PANEL_AUTOPICK_V1 ===
        # لاگِ واقعی:
        #   panel_key='' → پنل='pasargad' | پلن‌ها=0 | فروشِ آنلاین=False
        # یعنی ربات چند پنلِ فعال دارد و `get_any_enabled_panel` اولی را
        # برداشته، ولی پلن‌ها رویِ پنلِ دیگری تعریف شده‌اند. نتیجه: کاربر
        # پیامِ «⛔️ فروش VPN خودکار فعال نیست» می‌گرفت درحالی‌که ربات
        # کاملاً سالم بود.
        #
        # به‌جایِ اینکه به اولین پنلِ فعال قناعت کنیم، پنلی را برمی‌داریم
        # که واقعاً پلنِ فعال دارد. این مستقل از state کار می‌کند، پس هر
        # مسیری که panel_key ندهد (بازگشتِ پیش‌فاکتور، دسته‌بندی، مینی‌اپ)
        # را یکجا درست می‌کند.
        #
        # === PATCH_VPN_PLANS_MULTI_PANEL_PICKER_V1 ===
        # گزارشِ ادمین: قیمتِ «حجم/روزِ دلخواه» را رویِ پنلِ X روی ۴۰۰۰
        # تومان می‌گذاشت، ولی صفحهٔ خرید همیشه صفر نشان می‌داد. علتِ واقعی:
        # ربات چند پنلِ فعال داشت که هرکدام هم پلن داشتند هم فروشِ دلخواهِ
        # روشن — منطقِ بالا فقط *اولین* پنلی که هر پلنی داشت را برمی‌داشت
        # (بدونِ توجه به اینکه ادمین دقیقاً کدام پنل را ویرایش کرده)، پس
        # قیمتِ پنلِ *دیگری* (که قیمتش صفر بود) نمایش داده می‌شد.
        #
        # به‌جایِ حدسِ کورِ «اولین پنلِ واجدِ شرایط»، وقتی واقعاً بیش از یک
        # پنل هم‌زمان واجدِ شرایط است (پلنِ فعال یا فروشِ دلخواهِ روشن)،
        # یک صفحهٔ کوچکِ «کدام سرور؟» نشان می‌دهیم تا ابهام از بین برود.
        # برایِ اکثریتِ ربات‌ها (تک‌پنلی) این شاخه اصلاً اجرا نمی‌شود — رفتار
        # کاملاً دست‌نخورده می‌ماند.
        if miniapp_category_id is None:
            async with Session() as _s2:
                _all_panels = (await _s2.scalars(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id,
                    VpnPanel.enabled == True).order_by(VpnPanel.id))).all()  # noqa: E712
                _qualifying = []
                for _p in _all_panels:
                    _n = await _s2.scalar(select(func.count()).select_from(VpnPlan).where(
                        VpnPlan.bot_id == bot_id, VpnPlan.active == True,  # noqa: E712
                        VpnPlan.panel_key == _p.panel_key))
                    _vc_on = (await _vc(bot_id, "vc_enabled", _p.panel_key)) == "1"
                    if _n or _vc_on:
                        _qualifying.append(_p)
            if len(_qualifying) > 1:
                from tenant import inline_back_row, _with_btn_header
                if state is not None:
                    await state.update_data(vbuy_mb_id=mb_id, vbuy_header=header)
                # === PATCH_VPN_PANEL_PICK_LABEL_FIX_V1 ===
                # قبلاً اینجا به `_p.custom_label` دسترسی داشت که اصلاً روی
                # مدلِ VpnPanel وجود نداره (AttributeError) — هر ربات با
                # بیش از یک پنلِ واجدِ شرایط، همین‌جا با «خطایی رخ داد»
                # متوقف می‌شد. نامِ دلخواهِ پنل از قبل با تابعِ panel_label()
                # (که رویِ Setting ذخیره می‌شه) خونده می‌شه، نه یه attributeِ
                # مستقیمِ روی خودِ ردیفِ دیتابیس.
                from tenant.bots.vpn.vpn_admin import panel_label
                _panel_labels = {_p.panel_key: await panel_label(bot_id, _p.panel_key) for _p in _qualifying}
                rows = [[InlineKeyboardButton(
                    text=f"🌐 {_panel_labels[_p.panel_key]}",
                    callback_data=f"vplk:pick:{_p.panel_key}", style=ButtonStyle.PRIMARY)]
                    for _p in _qualifying]
                _br = await inline_back_row(bot_id)
                if _br:
                    rows.append(_br)
                _text = _with_btn_header(
                    "🌐 <b>انتخابِ سرور</b>\n\nاین ربات چند سرور دارد؛ اول انتخاب کن:", header)
                await msg.answer(_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                return
            if len(_qualifying) == 1:
                panel = _qualifying[0]
        async with Session() as _s:
            _has = await _s.scalar(select(func.count()).select_from(VpnPlan).where(
                VpnPlan.bot_id == bot_id, VpnPlan.active == True,  # noqa: E712
                VpnPlan.panel_key == (panel.panel_key if panel else "")))
            if not _has:
                _panels = (await _s.scalars(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id,
                    VpnPanel.enabled == True))).all()  # noqa: E712
                for _p in _panels:
                    _n = await _s.scalar(select(func.count()).select_from(VpnPlan).where(
                        VpnPlan.bot_id == bot_id, VpnPlan.active == True,  # noqa: E712
                        VpnPlan.panel_key == _p.panel_key))
                    if _n:
                        logger.info("پنلِ پیش‌فرض عوض شد: %r → %r (%d پلنِ فعال)",
                                    (panel.panel_key if panel else None), _p.panel_key, _n)
                        panel = _p
                        break
    async with Session() as s:
        # === PATCH_RESELLER_PLANS_V1 ===
        _aud, _is_rs = await _plan_audience_filter(bot_id, getattr(msg.from_user, "id", None))
        q = select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.active == True, _aud,
            VpnPlan.panel_key == (panel.panel_key if panel else ""))
        if miniapp_category_id is not None:  # PATCH_BUNDLE_BUTTON_V1
            q = q.where(VpnPlan.miniapp_category_id == miniapp_category_id)
        plans = (await s.scalars(q.order_by(VpnPlan.sort_order, VpnPlan.id))).all()

    # === PATCH_BTN_VPN_CATEGORY_FILTER_V1 ===
    # هر دکمهٔ خرید می‌تواند فقط دسته‌بندی‌هایِ خاصی را نشان دهد؛ این‌طوری
    # می‌شود برایِ هر دسته یک دکمهٔ جدا ساخت (مثلاً یکی «پلن‌های حجمی» و یکی
    # «پلن‌های نامحدود»). اگر چیزی انتخاب نشده باشد، مثلِ قبل همهٔ دسته‌ها
    # نمایش داده می‌شوند تا رفتارِ ربات‌هایِ موجود عوض نشود.
    if mb_id:
        allowed = await allowed_categories(bot_id, mb_id)
        if allowed is not None:
            plans = [p for p in plans
                     if ((p.category or "").strip() or "-") in allowed]
    cur_panel_key = panel.panel_key if panel else ""
    vc_enabled = (await _vc(bot_id, "vc_enabled", cur_panel_key)) == "1"
    has_panel = panel and panel.enabled
    # === PATCH_VPN_PLANS_DIAGNOSTIC_V1 ===
    # پیامِ «⛔️ فروش VPN خودکار فعال نیست» سه علتِ کاملاً متفاوت دارد و
    # هیچ‌کدام در لاگ نمی‌آمد، پس عیب‌یابی حدسی بود. حالا دقیقاً می‌گوید
    # کدام شرط رد شده و با چه مقادیری.
    if not has_panel or (not plans and not vc_enabled):
        logger.warning(
            "vpn_plans رد شد | bot=%s mb_id=%s panel_key=%r → پنل=%r فعال=%s | "
            "پلن‌ها=%d | فروشِ آنلاین=%s | دسته‌هایِ مجاز=%r",
            bot_id, mb_id, panel_key,
            (panel.panel_key if panel else None),
            (panel.enabled if panel else None),
            len(plans), vc_enabled,
            (await allowed_categories(bot_id, mb_id)) if mb_id else "همه")
        # === PATCH_BTN_HIDE_ALL_PLANS_V1 ===
        # اگر مالک پلن‌ها را برایِ این دکمه پنهان کرده ولی فروشِ آنلاین را
        # روشن نکرده، دکمه عملاً بی‌استفاده است؛ پیامِ عمومی گمراه‌کننده بود.
        if has_panel and mb_id and (await allowed_categories(bot_id, mb_id)) == set():
            await msg.answer(
                "⛔️ فعلاً چیزی برایِ فروش در این بخش تنظیم نشده.",
                reply_markup=await inline_back_kb(bot_id))
            return
        await msg.answer("⛔️ فروش VPN خودکار در حال حاضر فعال نیست.",
                         reply_markup=await inline_back_kb(bot_id))
        return
    # اگه هیچ پلن ثابتی نیست و فقط فروش آنلاین (vc) فعاله → مستقیم صفحه +/−
    if not plans and vc_enabled:
        # === PATCH_VC_SIMPLE_MODE_V1 === حالتِ ساده: مستقیم پرسشِ «حجم را بفرست»
        if state is not None:
            _simple = await _vc_simple_start(bot_id, msg.from_user.id, cur_panel_key, state)
            if _simple:
                from tenant import _with_btn_header
                sent = await msg.answer(_with_btn_header(_simple[0], header), reply_markup=_simple[1])
                await state.update_data(vc_manual_prompt_id=sent.message_id, vc_manual_orig_id=sent.message_id)
                return
        cfg   = await _vc_all_for_user(bot_id, msg.from_user.id, cur_panel_key)
        gb    = int(cfg["vc_min_gb"])
        days  = int(cfg["vc_min_days"])
        count = 1
        if state is not None:
            await state.set_state(VpnConfiguratorFSM.selecting)
            await state.update_data(vc_gb=gb, vc_days=days, vc_count=count,
                                    vc_bot_id=bot_id, vc_panel_key=cur_panel_key,
                                    vc_disc_code=None)
        text, kb = await _vc_render(bot, bot_id, msg.from_user.id, gb, days, count,
                                    cur_panel_key, state, no_plans=True)
        from tenant import _with_btn_header
        await msg.answer(_with_btn_header(text, header), reply_markup=kb)
        return
    from tenant import ibtn_style as _ibs
    from tenant import fa_digits  # === PATCH_PERSIAN_DIGITS_RTL_V1 ===
    rows = []
    _buy_style = await _ibs(bot_id, "ibtn:buy_plan") or ButtonStyle.SUCCESS
    _vc_style  = await _ibs(bot_id, "ibtn:custom_vc") or ButtonStyle.PRIMARY
    _color_map = {"danger": ButtonStyle.DANGER, "success": ButtonStyle.SUCCESS, "primary": ButtonStyle.PRIMARY}
    # === PATCH_VPN_PLAN_CATEGORY_V1 ===
    # پلن‌ها رو بر اساسِ دسته‌بندی گروه‌بندی می‌کنیم و بالایِ هر گروه یه
    # دکمهٔ «تیترِ» غیرقابل‌کلیک با اسمِ همون دسته می‌ذاریم — جایگزینِ
    # ترفندِ رنگ‌کردنِ دستیِ دکمه‌ها برایِ نشون‌دادنِ دسته‌بندی. پلن‌هایِ
    # بدونِ دسته‌بندی، بدونِ تیتر و همون‌طورِ قبل نشون داده می‌شن.
    _groups: dict[str | None, list] = {}
    _order: list[str | None] = []
    # === PATCH_VPN_PLAN_SORT_V1 ===
    for p in plans_ordered(plans):
        cat = (p.category or "").strip() or None
        if cat not in _groups:
            _groups[cat] = []
            _order.append(cat)
        _groups[cat].append(p)
    # === PATCH_PLAN_TABLE_VIEW_V1 ===
    # حالتِ نمایشِ «جدولی» (فقط پلنِ حرفه‌ای، از «پلن‌هایِ فروش» هر پنل): هر پلن یک ردیفِ
    # سه‌ستونه [حجم | زمان | قیمت] است و بالایِ جدول یک ردیفِ عنوان. هر سه خانهٔ یک
    # ردیف به همان پلن می‌روند. پیش‌فرض «لیستی» = رفتارِ قبلی.
    _table_mode = False
    _tbl_users = True   # PATCH_PLAN_TABLE_USERS_TOGGLE_V1 — نمایشِ تعدادِ کاربر در جدول (پیش‌فرض روشن)
    _tbl_vol = True     # PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1
    try:
        from plans import has_feature as _hf, Feature as _Ft
        if _hf(_row_check.plan, _Ft.PLAN_TABLE_VIEW):
            from tenant import get_setting as _gset
            _table_mode = (await _gset(bot_id, f"plan_view_{cur_panel_key}", "list")) == "table"
            _tbl_users = (await _gset(bot_id, f"plan_users_{cur_panel_key}", "1")) != "0"
            # === PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1 === (ایلیا) ستونِ «حجم» را بشود خاموش کرد و فقط «کاربر | زمان | قیمت» دید
            _tbl_vol = (await _gset(bot_id, f"plan_vol_{cur_panel_key}", "1")) != "0" or not _tbl_users
    except Exception:
        logger.exception("vpn: خواندنِ حالتِ نمایشِ پلن‌ها ناموفق بود")
    for cat in _order:
        if cat:
            rows.append([InlineKeyboardButton(text=f"🏷 ── {cat} ──", callback_data="noop")])
        if _table_mode:
            # === PATCH_PLAN_TABLE_VIEW_V2 === ردیفِ عنوان *زیرِ* تیترِ هر دسته (نه بالایِ کلِ صفحه)
            # === PATCH_PLAN_TABLE_VIEW_V3 === ترتیبِ چپ→راست: قیمت | زمان | حجم، و عنوان‌ها سبز
            rows.append([InlineKeyboardButton(text="💲 قیمت", callback_data="noop", style=ButtonStyle.SUCCESS),
                         InlineKeyboardButton(text="🕐 زمان", callback_data="noop", style=ButtonStyle.SUCCESS),
                         InlineKeyboardButton(text=("🔋 حجم" if _tbl_vol else "👥 کاربر"), callback_data="noop",
                                              style=ButtonStyle.SUCCESS)])
        for p in _groups[cat]:
            # === PATCH_PERSIAN_DIGITS_RTL_V1 ===
            # ارقام به فارسی و جداکنندهٔ «/» به «،» تغییر کرد. دلیل: ترکیبِ
            # ارقامِ لاتین با «/» در متنِ راست‌به‌چپ باعثِ جابه‌جاییِ ترتیبِ
            # نمایش می‌شد («2 گیگ/30 روز» بهم‌ریخته دیده می‌شد).
            vol = "نامحدود" if p.volume_gb == 0 else f"{fa_digits(p.volume_gb)} گیگ"
            dur = "بی‌انقضا" if p.days == 0 else f"{fa_digits(p.days)} روز"
            title = (p.title or "").strip()
            # === PATCH_VPN_PLAN_BTN_COMPACT_V1 ===
            # اگه عنوانِ پلن خودش قبلاً حجم/مدت رو ذکر کرده، دوباره تکرارشون نمی‌کنیم
            # تا متنِ دکمه بی‌خودی طولانی/فشرده نشه.
            already_has = ("گیگ" in title or "GB" in title.upper()) and \
                          ("روز" in title or " D" in title.upper())
            spec = "" if already_has else f" — {vol}، {dur}"
            # === PATCH_PLAN_USERS_LABEL_V1 === پلنِ چندکاربره: «تک‌کاربره / دو کاربره» هم نوشته شود
            _ul = _users_lbl(getattr(p, "limit_ip", 0))
            if _ul and "کاربر" not in title:
                spec += f" — {_ul}"
            btn_style = _color_map.get((p.color or "").strip(), _buy_style)
            # === PATCH_PARTNER_PLAN_PRICE_LABEL_V1 ===
            # قیمتِ رویِ دکمه تا حالا همیشه قیمتِ خام بود؛ تخفیفِ نماینده
            # فقط در پیش‌فاکتور و موقعِ کسر از کیف پول اعمال می‌شد. نتیجه:
            # نماینده فهرست را می‌دید، قیمت‌ها عوض نشده بود و فکر می‌کرد
            # تخفیفش کار نمی‌کند. حالا قیمتِ تخفیف‌خورده روی خودِ دکمه
            # می‌آید و قیمتِ قبلی هم کنارش می‌ماند تا تفاوت دیده شود.
            _price_txt = fa_digits(rial(p.price))
            _tbl_price = rial(p.price)   # PATCH_PLAN_TABLE_VIEW_V1 — در جدول فقط قیمتِ نهایی
            try:
                from tenant.bots.vpn.vpn_partners import partner_discount
                # === PATCH_RESELLER_PANEL_ONLY_V1 ===
                # تخفیفِ خودکارِ نمایندگی فقط داخلِ پنلِ نمایندگی اعمال بشه،
                # نه تو خریدِ عادیِ ربات — تخفیفِ دستیِ همکاری (اگه ادمین
                # جدا ثبت کرده) هنوز اعمال می‌شه.
                _pd = await partner_discount(
                    bot_id=bot_id, telegram_id=msg.from_user.id, price=int(p.price or 0),
                    panel_key=(p.panel_key or cur_panel_key), scope="plans",
                    include_auto_reseller=False, gb=p.volume_gb)
                if _pd.ok and _pd.discount > 0:
                    _price_txt = (f"{fa_digits(rial(_pd.final_price))} "
                                  f"(به‌جای {fa_digits(rial(p.price))})")
                    _tbl_price = rial(_pd.final_price)
            except Exception:
                logger.exception("محاسبهٔ تخفیفِ نماینده رویِ برچسبِ پلن ناموفق بود")
            if _table_mode:
                # === PATCH_PLAN_TABLE_VIEW_V1 === ترتیبِ خانه‌ها چپ→راست: حجم | زمان | قیمت
                _v = p.volume_gb
                _vtxt = "♾ نامحدود" if not _v else (f"{int(_v)}GB" if float(_v) == int(float(_v)) else f"{_v}GB")
                # === PATCH_PLAN_TABLE_VIEW_V2 ===
                # متنِ «دو کاربره · ♾ نامحدود» از پهنایِ خانه بیرون می‌زد و بریده می‌شد.
                # برایِ پلنِ نامحدود به‌جایِ حجم فقط تعدادِ کاربر نوشته می‌شود (تیترِ دسته هم
                # «نامحدود» است)؛ برایِ بقیه فرمِ کوتاه: «تک‌کاربره · 30GB» / «۴ کاربره · 50GB».
                try:
                    _n_users = int(getattr(p, "limit_ip", 0) or 0)
                except (TypeError, ValueError):
                    _n_users = 0
                if _n_users > 0 and _tbl_users:
                    _ul = "تک‌کاربره" if _n_users == 1 else f"{_n_users} کاربره"
                    # === PATCH_PLAN_TABLE_TITLE_LABEL_V1 === (ایلیا) اگر مالک اسمِ پلن را عوض کرده («یک کاربر»)، همان در جدول بیاید
                    if "کاربر" in title and len(title) <= 18:
                        _ul = title
                    # === PATCH_PLAN_TABLE_VIEW_V4 === نامحدود: «♾ تک‌کاربره» (نه فقط «تک‌کاربره» که
                    # معلوم نبود حجم چقدر است)؛ هنوز کوتاه و جا می‌شود.
                    if not _tbl_vol:                      # PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1: فقط تعدادِ کاربر
                        _vtxt = _ul
                    else:
                        _vtxt = f"♾ {_ul}" if not _v else f"{_ul} · {_vtxt}"
                _dtxt = "♾ بی‌انقضا" if not p.days else f"{p.days} روز"
                _cbd = f"vbuy:{p.id}"
                rows.append([
                    InlineKeyboardButton(text=_tbl_price, callback_data=_cbd, style=btn_style),
                    InlineKeyboardButton(text=_dtxt, callback_data=_cbd, style=btn_style),
                    InlineKeyboardButton(text=_vtxt, callback_data=_cbd, style=btn_style),
                ])
                continue
            rows.append([InlineKeyboardButton(
                text=f"{title}{spec} — {_price_txt}",
                callback_data=f"vbuy:{p.id}", style=btn_style)])
    if vc_enabled:
        cfg_disc = await _vc_all_for_user(bot_id, msg.from_user.id, cur_panel_key)
        pg = int(cfg_disc["vc_price_gb"])
        pd = int(cfg_disc["vc_price_day"])
        if _table_mode:
            # === PATCH_PLAN_TABLE_VIEW_V1 === ردیفِ «حجم دلخواه» هم هم‌شکلِ جدول
            _vcd = f"vc:open:{cur_panel_key}"
            rows.append([
                InlineKeyboardButton(text=f"{pg:,} ت/گیگ", callback_data=_vcd, style=_vc_style),
                InlineKeyboardButton(text=f"{pd:,} ت/روز", callback_data=_vcd, style=_vc_style),
                InlineKeyboardButton(text="⚙️ حجم دلخواه", callback_data=_vcd, style=_vc_style),
            ])
        else:
            rows.append([InlineKeyboardButton(
                text=f"⚙️ حجم/روز دلخواه — {pg:,}تومان/گیگ + {pd:,}تومان/روز",
                callback_data=f"vc:open:{cur_panel_key}", style=_vc_style)])
    # === PATCH_METERED_PLAN_V1 === دکمهٔ «بستهٔ اعتباری» (فقط پلنِ حرفه‌ای و وقتی ادمین روشنش کرده)
    try:
        from tenant.bots.vpn.vpn_metered import user_entry_row as _mtr_row, feature_ok as _mtr_ok
        if await _mtr_ok(_row_check):
            _mr = await _mtr_row(bot_id)
            if _mr:
                rows.append(_mr)
    except Exception:
        logger.exception("mtr: ساختِ دکمهٔ بستهٔ اعتباری ناموفق")
    _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    from tenant import _with_btn_header
    # === PATCH_RTL_TITLE_ORDER_V1 ===
    # ایموجی از *ابتدای* تیتر به انتهاش منتقل شد. دلیل: تلگرام جهتِ هر خط رو
    # از رویِ اولین کاراکترِ «جهت‌دار»ِ اون خط تعیین می‌کنه. وقتی خط با ایموجی
    # (که بی‌جهته) شروع می‌شد، کلمهٔ لاتینِ «VPN» اولین کاراکترِ جهت‌دار
    # می‌شد و کلِ خط چپ‌چین/بهم‌ریخته رندر می‌شد. علامتِ نامرئیِ RLM هم
    # (که قبلاً امتحان شد) توسطِ کلاینتِ تلگرام نادیده گرفته می‌شه. حالا خط
    # مستقیم با حرفِ فارسی شروع می‌شه و جهتش قطعاً راست‌به‌چپ می‌مونه.
    # === PATCH_RTL_CLEAN_TITLE_V1 ===
    # تیتر از صفر و کاملاً فارسی بازنویسی شد: نه ایموجیِ ابتدای خط،
    # نه کلمهٔ لاتین (VPN → کانفیگ). حالا خط فقط حرفِ فارسی داره، پس
    # هیچ ابهامی در جهتِ متن باقی نمی‌مونه.
    # === PATCH_VPN_EDITABLE_TITLES_V1 === (پیش‌فرض عیناً همان متنِ قبلی است)
    page = f"<b>{await _get_page_title(bot_id, 'plans')}</b>\n\nیک پلن را انتخاب کنید:"
    await msg.answer(_with_btn_header(page, header),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ═══════════════════════════════════════════
#  دکمهٔ خریدِ ترکیبی — PATCH_BUNDLE_BUTTON_V1
# ═══════════════════════════════════════════
# یک دکمه که چند «دستهٔ پنلِ خودکار» (مثلاً یک دسته از یوپنل + یک دسته از
# پاسارگاد) و یک یا چند «دستهٔ انبارِ کانفیگِ آماده» (مثلاً «نامحدود») را
# با هم زیرِ خودش جمع می‌کند. دادهٔ گروه‌بندی همان MiniAppCategory است —
# همان چیزی که برایِ فروشگاهِ مینی‌اپ ساخته شده بود؛ اینجا فقط یک نمایشِ
# چت‌محورِ تازه برایش می‌سازیم، بدونِ دست‌زدن به منطقِ خرید/پرداخت.

async def _bundle_gather(bot_id: int, cat_id: int, viewer_id: int | None = None):
    """پلن‌ها (از هر پنلی) و اسمِ دسته‌هایِ داخلی را برایِ این دستهٔ ترکیبی
    جمع می‌کند. خروجی: (cat, plans, stock_names, bucket_names, has_uncat)

    bucket_names: فهرستِ مرتب‌شدهٔ اسمِ دسته‌هایی که از هر دو منبع دیده
    می‌شوند (VpnPlan.category هر پنل + استاکِ انبار) — این‌ها همان چیزی‌اند
    که به کاربر نشان داده می‌شوند، نه نامِ پنل/منبع.
    """
    from models import MiniAppCategory
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != bot_id or not cat.active:
            return None, [], [], [], False
        plans = list((await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.miniapp_category_id == cat_id,
            VpnPlan.active == True,
            (await _plan_audience_filter(bot_id, viewer_id))[0]))).all())  # noqa: E712

    stock_names = [n.strip() for n in (cat.stock_categories or "").split(",")
                  if n.strip()]
    plan_cat_names = {(p.category or "").strip() for p in plans if (p.category or "").strip()}
    has_uncat = any(not (p.category or "").strip() for p in plans)
    # === PATCH_BUNDLE_MERGED_CATEGORIES_V1 ===
    # اسمِ دستهٔ یکسان از دو پنلِ مختلف (یا از پنل و از انبار) با هم یکی
    # می‌شود چون هر دو در همین یک مجموعه جمع می‌شوند — دقیقاً همان ادغامی
    # که خواسته شده بود.
    bucket_names = bucket_order(plans, plan_cat_names | set(stock_names))
    return cat, plans, stock_names, bucket_names, has_uncat


def _bundle_stock_rows(cfgs, cat_tag: str, mb_id: int | None) -> list:
    """دقیقاً همان منطقِ گروه‌بندی/دکمه‌سازیِ _show_manual_packages، فقط
    این‌جا بدونِ فرستادنِ پیامِ جدا — چون قرار است با دکمه‌هایِ پلن‌هایِ
    پنل در یک صفحهٔ واحد ادغام شود. (ارقامِ فارسی هم اینجا اعمال شده تا با
    دکمه‌هایِ پلن که کنارشان می‌نشینند هم‌شکل باشد.)"""
    from tenant import fa_digits
    groups: dict[tuple, int] = {}
    for c in cfgs:
        key = (c.volume_gb, c.days, c.user_count or 0, int(c.price))
        groups[key] = groups.get(key, 0) + 1
    rows = []
    for (vol, days, users, price), stock in sorted(groups.items()):
        vol_lbl = "♾️ نامحدود" if vol == 0 else f"{fa_digits(vol)} گیگ"
        users_lbl = f" · {fa_digits(users)} کاربر" if users else ""
        # === PATCH_BUNDLE_ZERO_DAYS_LABEL_V1 ===
        # همان باگِ «۰ روز» که قبلاً در fmt_days برایِ بقیهٔ جاها رفع شده بود؛
        # این تابع (bundle/ادغام‌شده با پلن‌هایِ پنل) هنوز مقدارِ خامِ days را
        # چاپ می‌کرد، پس days=0 به‌جایِ «بی‌انقضا» همچنان «۰ روز» نشان داده می‌شد.
        days_lbl = "♾️ بی‌انقضا" if days == 0 else f"{fa_digits(days)} روز"
        label = (f"🛒 {vol_lbl} · {days_lbl}{users_lbl} — "
                f"{fa_digits(rial(price))} (موجودی {fa_digits(stock)})")
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"mcfg:buy:{vol}:{days}:{users}:{price}:{cat_tag}:{mb_id or 0}",
            style=ButtonStyle.PRIMARY)])
    return rows


# === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 ===
# باگِ گزارش‌شده: در فهرستِ پلن‌هایِ یک دسته، «بازگشت به منو» کاربر را تا
# منویِ اصلی برمی‌گرداند و باید کلِ مسیرِ خرید را از اول می‌رفت. بازگشت
# باید یک پله عقب باشد: از فهرستِ پلن‌ها به فهرستِ دسته‌ها.
def _bundle_back_row(back_cb: str | None):
    if not back_cb:
        return None
    return [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb,
                                 style=ButtonStyle.DANGER)]


def _plan_table_row(p, show_users: bool = True, show_volume: bool = True) -> list:
    """یک ردیفِ جدولِ پلن‌ها [قیمت | زمان | حجم] — برایِ صفحهٔ دسته‌هایِ خریدِ ترکیبی
    (PATCH_BUNDLE_TABLE_VIEW_V1). همان قالبِ جدولِ صفحهٔ پنل."""
    v = p.volume_gb
    vtxt = "♾ نامحدود" if not v else (f"{int(v)}GB" if float(v) == int(float(v)) else f"{v}GB")
    try:
        n = int(getattr(p, "limit_ip", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0 and show_users:
        ul = "تک‌کاربره" if n == 1 else f"{n} کاربره"
        _t = (getattr(p, "title", "") or "").strip()      # PATCH_PLAN_TABLE_TITLE_LABEL_V1
        if "کاربر" in _t and len(_t) <= 18:
            ul = _t
        if not show_volume:                         # PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1
            vtxt = ul
        else:
            vtxt = f"♾ {ul}" if not v else f"{ul} · {vtxt}"
    dtxt = "♾ بی‌انقضا" if not p.days else f"{p.days} روز"
    cbd = f"vbuy:{p.id}"
    st = ButtonStyle.SUCCESS
    return [InlineKeyboardButton(text=rial(p.price), callback_data=cbd, style=st),
            InlineKeyboardButton(text=dtxt, callback_data=cbd, style=st),
            InlineKeyboardButton(text=vtxt, callback_data=cbd, style=st)]


async def _render_bundle_bucket(bot: Bot, msg: Message, bot_id: int, plans: list,
                                name: str | None, mb_id: int | None = None,
                                header: str = "", cat=None,
                                stock_ok: bool | None = None,
                                title_override: str | None = None,
                                icon_override: str = "🧩",
                                back_cb: str | None = None,
                                state=None, reopen: str | None = None) -> None:
    """صفحهٔ نهایی: پلن‌هایِ همان دسته (از هر پنلی) + کانفیگ‌هایِ انبارِ همان
    دسته، همه با هم رویِ یک صفحه — بدونِ اشاره به اینکه هرکدام از کدام پنل
    یا از انبار آمده‌اند.

    === PATCH_BUNDLE_ALL_MODES_V1 ===
    قبلاً این تابع همیشه یک شیِ MiniAppCategory واقعی می‌خواست (برایِ
    عنوان و برایِ فهمیدنِ کدام دسته‌هایِ انبار مجازند). حالتِ «همهٔ
    دسته‌ها»/«همهٔ سرویس‌ها» هیچ دستهٔ ثابتی ندارند، پس این پارامترها
    اختیاری شدند: اگر cat داده نشود، از title_override/icon_override
    استفاده می‌شود، و اگر stock_ok صریح داده شود همان را می‌پذیرد (به‌جایِ
    اینکه از stock_categories یک دستهٔ خاص بخواند).
    """
    from tenant import inline_back_row, inline_back_kb, _with_btn_header, fa_digits
    # === PATCH_VBUYBACK_REOPEN_V1 === «بازگشت» از پیش‌فاکتور باید به همین صفحه برگردد
    if state is not None:
        await state.update_data(vbuy_reopen=reopen)
    # === PATCH_VPN_PLAN_SORT_V1 ===
    matched_plans = plans_ordered(
        [p for p in plans if (p.category or "").strip() == (name or "")])
    rows = []
    # === PATCH_BUNDLE_TABLE_VIEW_V1 ===
    # گزارش: ادمین «جدولی» را روشن کرده بود ولی کاربر از مسیرِ «خرید سرویس» (صفحهٔ دسته‌ها)
    # همچنان لیستی می‌دید؛ چون این صفحه به تنظیمِ نمایشِ پنل‌ها توجه نمی‌کرد. حالا اگر
    # *همهٔ* پنل‌هایِ پلن‌هایِ این دسته جدولی باشند (و پلن حرفه‌ای)، جدول نشان داده می‌شود.
    _tbl, _tbl_users, _tbl_vol = False, True, True      # PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1
    if matched_plans:
        try:
            from plans import has_feature as _hf, Feature as _Ft
            from tenant import get_bot_row_by_id as _gbr, get_setting as _gset
            _brow = await _gbr(bot_id)
            if _brow and _hf(_brow.plan, _Ft.PLAN_TABLE_VIEW):
                _pks = {(p.panel_key or "") for p in matched_plans}
                _views = [(await _gset(bot_id, f"plan_view_{k}", "list")) == "table" for k in _pks]
                _tbl = bool(_views) and all(_views)
                _tbl_users = any([(await _gset(bot_id, f"plan_users_{k}", "1")) != "0" for k in _pks])
                # PATCH_PLAN_TABLE_VOLUME_TOGGLE_V1: حجم فقط وقتی پنهان است که همهٔ پنل‌هایِ این دسته خاموشش کرده باشند
                _tbl_vol = (any([(await _gset(bot_id, f"plan_vol_{k}", "1")) != "0" for k in _pks])
                            or not _tbl_users)
        except Exception:
            logger.exception("vpn: خواندنِ حالتِ جدولیِ صفحهٔ دسته ناموفق بود")
    if _tbl:
        rows.append([InlineKeyboardButton(text="💲 قیمت", callback_data="noop", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="🕐 زمان", callback_data="noop", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text=("🔋 حجم" if _tbl_vol else "👥 کاربر"), callback_data="noop",
                                          style=ButtonStyle.SUCCESS)])
    for p in matched_plans:
        if _tbl:
            rows.append(_plan_table_row(p, _tbl_users, _tbl_vol))
            continue
        vol = "نامحدود" if p.volume_gb == 0 else f"{fa_digits(p.volume_gb)} گیگ"
        dur = "بی‌انقضا" if p.days == 0 else f"{fa_digits(p.days)} روز"
        title = (p.title or "").strip()
        already_has = ("گیگ" in title or "GB" in title.upper()) and \
                      ("روز" in title or " D" in title.upper())
        spec = "" if already_has else f" — {vol}، {dur}"
        rows.append([InlineKeyboardButton(
            text=f"{title}{spec} — {fa_digits(rial(p.price))}",
            callback_data=f"vbuy:{p.id}", style=ButtonStyle.SUCCESS)])

    if stock_ok is None:
        stock_ok = bool(name) and cat is not None and name in [
            n.strip() for n in (cat.stock_categories or "").split(",") if n.strip()]
    if stock_ok and name:
        from models import Config
        async with Session() as s:
            cfgs = list((await s.scalars(select(Config).where(
                Config.bot_id == bot_id, Config.status == "in_stock",
                Config.price > 0, Config.category == name))).all())
        rows.extend(_bundle_stock_rows(cfgs, name, mb_id))

    bucket_title = name or (cat.title if cat else title_override) or "این بخش"
    if not rows:
        await msg.answer(
            f"📭 فعلاً چیزی زیرِ «{bucket_title}» موجود نیست.",
            reply_markup=(InlineKeyboardMarkup(inline_keyboard=[_bundle_back_row(back_cb)])
                          if back_cb else await inline_back_kb(bot_id)))
        return

    # === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 ===
    _br = _bundle_back_row(back_cb) or await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    icon = (cat.icon if cat else icon_override) or "🧩"
    title = _with_icon(await _cat_icons_on(bot_id), icon, f"<b>{bucket_title}</b>") + "\n\nیکی را انتخاب کن:"
    await msg.answer(_with_btn_header(title, header),
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# === PATCH_VPN_CAT_ICONS_TOGGLE_V1 ===
# درخواستِ مشتری (#854): آیکنِ کنارِ دکمهٔ دسته‌ها (🛰 / 🏷 / 🧩 …) ظاهرِ دکمه‌ها را خراب می‌کند و در بعضی
# گوشی‌ها شبیهِ پوشه نمایش داده می‌شود. مالک می‌تواند خاموشش کند؛ پیش‌فرض روشن (بدونِ تغییرِ رفتارِ ربات‌هایِ فعلی).
async def _cat_icons_on(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "vpn_cat_icons", "1")) != "0"


def _with_icon(on: bool, icon: str, text: str) -> str:
    return f"{icon} {text}" if on and icon else text


# === PATCH_VPN_PLAN_SORT_V1 ===
def plans_ordered(plans):
    """ترتیبِ نمایشِ پلن‌ها به مشتری.

    اگر مالک حداقل یک‌بار پلن/دسته‌ای را بالا/پایین برده باشد (sort_order > 0 ثبت شده)، ترتیبِ دستیِ او
    اعمال می‌شود (پلنِ تازه‌ساخته که هنوز جایی نگرفته آخر می‌نشیند)؛ وگرنه همان مرتب‌سازیِ خودکار
    (حجم ↑، مدت ↑، قیمت ↑) مثلِ قبل."""
    plans = list(plans)
    if any((getattr(p, "sort_order", 0) or 0) > 0 for p in plans):
        return sorted(plans, key=lambda p: ((getattr(p, "sort_order", 0) or 0) or 10 ** 9, p.id or 0))
    return sorted(plans, key=_plan_sort_key)


def bucket_order(plans, names):
    """ترتیبِ دسته‌ها: در حالتِ دستی به ترتیبِ اولین ظهورِ پلن‌ها (بعدش دسته‌هایِ فقط‌انبار)، وگرنه الفبایی مثلِ قبل."""
    names = set(names)
    if not any((getattr(p, "sort_order", 0) or 0) > 0 for p in plans):
        return sorted(names)
    seen: list[str] = []
    for p in plans_ordered(plans):
        c = (p.category or "").strip()
        if c and c in names and c not in seen:
            seen.append(c)
    return seen + sorted(names - set(seen))


def _plan_sort_key(p):
    """ترتیبِ نمایشِ پلن‌ها: حجم ↑، بعد مدت ↑، بعد قیمت ↑.

    قبلاً هیچ مرتب‌سازی‌ای نبود و پلن‌ها به ترتیبِ ساخته‌شدن (id) می‌آمدند،
    برایِ همین فهرست از دیدِ کاربر «قاطی» به‌نظر می‌رسید (مثلاً ۲۵، ۱۰،
    ۵۰، ۱۰۰). پلنِ «نامحدود» (volume_gb=0) عمداً آخر می‌نشیند، نه اول،
    چون از نظرِ ارزش بالاتر از همه است نه پایین‌تر؛ همین منطق برایِ
    «بی‌انقضا» (days=0) هم اعمال می‌شود.
    """
    vol = p.volume_gb or 0
    days = p.days or 0
    return (
        1 if vol == 0 else 0, vol,
        1 if days == 0 else 0, days,
        int(p.price or 0),
    )


async def show_bundle_hub(bot: Bot, msg: Message, bot_id: int, cat_id: int, state=None,
                          mb_id: int | None = None, header: str = "",
                          support_enabled: bool = False, support_contact: str | None = None,
                          btn=None):
    """صفحهٔ «دکمهٔ خریدِ ترکیبی».

    === PATCH_BUNDLE_MERGED_CATEGORIES_V1 ===
    به‌جایِ نشان‌دادنِ نامِ پنل/منبع (پاسارگاد/یوپنل/کانفیگِ آماده)، فقط
    اسمِ دسته‌هایِ داخلی (مثلاً «اقتصادی») نشان داده می‌شود — پلن‌هایِ همان
    دسته از هر پنلی، به‌علاوهٔ کانفیگ‌هایِ همان دسته از انبار، با هم رویِ یک
    صفحه می‌آیند. اگر فقط یک دستهٔ داخلی وجود داشته باشد، همان مرحلهٔ
    میانی هم حذف می‌شود و مستقیم وارد لیستِ خرید می‌شود.
    """
    await _drop_reply_kb(msg, bot_id)      # PATCH_FLOW_DROP_REPLY_KB_V1
    from tenant import inline_back_kb
    cat, plans, stock_names, bucket_names, has_uncat = await _bundle_gather(bot_id, cat_id, getattr(msg.from_user, 'id', None))
    # === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 ===
    # اگر بیش از یک دسته وجود دارد، «بازگشت» باید به فهرستِ دسته‌ها برود
    # نه منویِ اصلی؛ وگرنه کاربر باید کلِ مسیرِ خرید را از اول برود.
    from models import MiniAppCategory   # PATCH_BUNDLE_BACK_ONE_LEVEL_V1
    async with Session() as _s:
        _multi_cats = ((await _s.scalar(select(func.count()).select_from(MiniAppCategory)
                        .where(MiniAppCategory.bot_id == bot_id,
                               MiniAppCategory.active == True))) or 0) > 1  # noqa: E712
    if not cat:
        await msg.answer("⛔️ این بخش دیگر در دسترس نیست؛ به ادمین اطلاع بده.",
                         reply_markup=await inline_back_kb(bot_id))
        return
    if getattr(cat, "sale_full", False):      # PATCH_CATEGORY_SALE_FULL_V1 — ورودِ مستقیم با دکمهٔ خرید
        await msg.answer(sale_full_text(cat.title), reply_markup=await inline_back_kb(bot_id))
        return

    total_buckets = len(bucket_names) + (1 if has_uncat else 0)
    if total_buckets == 0:
        await msg.answer(f"📭 فعلاً چیزی زیرِ «{cat.title}» موجود نیست.",
                         reply_markup=await inline_back_kb(bot_id))
        return
    if total_buckets == 1:
        # یک دستهٔ داخلی (یا فقط پلن‌هایِ بدونِ‌دسته) — واسطهٔ اضافه لازم نیست
        only_name = bucket_names[0] if bucket_names else None
        # === PATCH_BUNDLE_BACK_SINGLE_BUCKET_V1 === اگر چند دستهٔ ترکیبی هست، «بازگشت» به فهرستِ دسته‌ها برود (نه منویِ اصلی)
        await _render_bundle_bucket(bot, msg, bot_id, plans, only_name,
                                    mb_id=mb_id, header=header, cat=cat,
                                    back_cb=("bndl:cats" if _multi_cats else None),
                                    state=state, reopen=f"bndl:cat:{cat_id}")
        return

    if state is not None:
        await state.update_data(vbuy_mb_id=mb_id, bndl_cat_id=cat_id)
    from tenant import inline_back_row, _with_btn_header
    rows = [[InlineKeyboardButton(text=f"🏷 {n}", callback_data=f"bndl:sub:{cat_id}:{i}",
                                  style=ButtonStyle.PRIMARY)]
            for i, n in enumerate(bucket_names)]
    if has_uncat:
        rows.append([InlineKeyboardButton(
            text="🌐 سایر", callback_data=f"bndl:sub:{cat_id}:{len(bucket_names)}",
            style=ButtonStyle.PRIMARY)])
    # === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 ===
    _br = _bundle_back_row("bndl:cats") if _multi_cats else await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    text = _with_btn_header(
        _with_icon(await _cat_icons_on(bot_id), cat.icon or '🧩', f"<b>{cat.title}</b>") + "\n\nیک دسته را انتخاب کن:", header)
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 ===
@vpn_user_router.callback_query(F.data == "bndl:cats")
async def bundle_back_to_cats(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازگشت به فهرستِ دسته‌ها."""
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    await cb.answer()
    data = await state.get_data()
    await show_bundle_all_categories(bot, _EditFirst(cb.message, cb.from_user), row.id, state=state,
                                     mb_id=data.get("vbuy_mb_id"))


@vpn_user_router.callback_query(F.data == "bndl:allsvc")
async def bundle_back_to_services(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازگشت به فهرستِ «همهٔ سرویس‌ها»."""
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    await cb.answer()
    data = await state.get_data()
    await show_bundle_all_services(bot, _EditFirst(cb.message, cb.from_user), row.id, state=state,
                                   mb_id=data.get("vbuy_mb_id"))


@vpn_user_router.callback_query(F.data.startswith("bndl:sub:"))
async def bundle_pick_bucket(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")  # bndl:sub:{cat_id}:{idx}
    cat_id, idx = int(parts[2]), int(parts[3])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    cat, plans, stock_names, bucket_names, has_uncat = await _bundle_gather(row.id, cat_id, cb.from_user.id)
    if not cat:
        await cb.answer("یافت نشد", show_alert=True)
        return
    name = bucket_names[idx] if idx < len(bucket_names) else None  # آخرین ایندکس = «سایر»
    await cb.answer()
    data = await state.get_data()
    mb_id = data.get("vbuy_mb_id")
    # === PATCH_BUNDLE_BACK_ONE_LEVEL_V1 === بازگشت به همین دسته، نه منوی اصلی
    await _render_bundle_bucket(bot, _EditFirst(cb.message, cb.from_user), row.id, plans, name, mb_id=mb_id,
                                cat=cat, back_cb=f"bndl:cat:{cat_id}",
                                state=state, reopen=f"bndl:sub:{cat_id}:{idx}")


# ═══════════════════════════════════════════
#  «همهٔ دسته‌ها» و «همهٔ سرویس‌ها» — PATCH_BUNDLE_ALL_MODES_V1
# ═══════════════════════════════════════════
# دو حالتِ ویژهٔ دکمهٔ خریدِ ترکیبی که هیچ ربطی به مینی‌اپ ندارند و کاملاً
# داخلِ همان چیزی زندگی می‌کنند که ادمین از «🎯 عملکرد دکمه» می‌سازد:
#   • bundle_mode="all_categories" → به‌جایِ محدودشدن به یک دستهٔ ترکیبیِ
#     مشخص، همهٔ دسته‌هایِ ترکیبیِ ربات را فهرست می‌کند؛ تپ‌کردنِ هرکدام
#     دقیقاً مثلِ همیشه واردِ همان دسته می‌شود.
#   • bundle_mode="all_services" → کلاً بدونِ هیچ دسته‌بندی‌ای: هر پلنِ
#     فعالِ VPN (از هر پنلی) و هر کانفیگِ قیمت‌دارِ انبار، مستقیم زیرِ
#     همین یک دکمه جمع می‌شوند.

# === PATCH_VPN_PLANS_MULTI_PANEL_PICKER_V1 ===
@vpn_user_router.callback_query(F.data.startswith("vplk:pick:"))
async def vpn_plans_pick_panel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """کاربر از صفحهٔ «کدام سرور؟» یکی را انتخاب کرد — دوباره show_vpn_plans
    را این‌بار با panel_key صریح صدا می‌زنیم تا ابهام از بین برود."""
    chosen_key = cb.data.split(":", 2)[2]
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    await cb.answer()
    data = await state.get_data() if state is not None else {}
    mb_id = data.get("vbuy_mb_id")
    header = data.get("vbuy_header", "")
    await show_vpn_plans(bot, _EditFirst(cb.message, cb.from_user), row.id, state=state, panel_key=chosen_key,
                         header=header, mb_id=mb_id)


# === PATCH_CATEGORY_SALE_FULL_V1 ===
def sale_full_text(title: str) -> str:
    return (f"⛔️ <b>ظرفیتِ فروشِ «{title}» پر شده است.</b>\n\n"
            "فعلاً از این دسته خرید انجام نمی‌شود؛ لطفاً از دستهٔ/پنلِ دیگری استفاده کن.")


async def plan_sale_full_message(bot_id: int, plan_id: int) -> str | None:
    """اگر پلن زیرِ دسته‌ای با «ظرفیتِ پر» باشد، متنِ اطلاع؛ وگرنه None."""
    from models import MiniAppCategory
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        cid = getattr(plan, "miniapp_category_id", None) if plan else None
        cat = await s.get(MiniAppCategory, cid) if cid else None
    if cat and cat.bot_id == bot_id and getattr(cat, "sale_full", False):
        return sale_full_text(cat.title)
    return None


async def show_bundle_all_categories(bot: Bot, msg: Message, bot_id: int, state=None,
                                     mb_id: int | None = None, header: str = "") -> None:
    await _drop_reply_kb(msg, bot_id)      # PATCH_FLOW_DROP_REPLY_KB_V1
    from tenant import inline_back_kb, inline_back_row, _with_btn_header
    from models import MiniAppCategory
    async with Session() as s:
        cats = list((await s.scalars(select(MiniAppCategory).where(
            MiniAppCategory.bot_id == bot_id, MiniAppCategory.active == True
        ).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all())  # noqa: E712

    if not cats:
        await msg.answer("📭 فعلاً هیچ دسته‌ای ساخته نشده.", reply_markup=await inline_back_kb(bot_id))
        return
    if len(cats) == 1:
        # یک دسته بیشتر نیست — واسطهٔ اضافه لازم نیست
        await show_bundle_hub(bot, msg, bot_id, cats[0].id, state=state, mb_id=mb_id, header=header)
        return

    if state is not None:
        await state.update_data(vbuy_mb_id=mb_id)
    _ico_on = await _cat_icons_on(bot_id)
    rows = [[InlineKeyboardButton(
                text=_with_icon(_ico_on, c.icon or '🧩', c.title) + (" — 🔴 ظرفیت پر" if getattr(c, "sale_full", False) else ""),
                callback_data=f"bndl:cat:{c.id}",
                style=(ButtonStyle.DANGER if getattr(c, "sale_full", False) else ButtonStyle.PRIMARY))]
            for c in cats]
    _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    text = _with_btn_header(  # === PATCH_VPN_EDITABLE_TITLES_V1 ===
        _with_icon(_ico_on, "🌐", f"<b>{await _get_page_title(bot_id, 'all_cats')}</b>") + "\n\nیک دسته را انتخاب کن:", header)
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@vpn_user_router.callback_query(F.data.startswith("bndl:cat:"))
async def bundle_pick_category(cb: CallbackQuery, bot: Bot, state: FSMContext):
    cat_id = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    from models import MiniAppCategory   # PATCH_CATEGORY_SALE_FULL_V1
    async with Session() as _s:
        _cat = await _s.get(MiniAppCategory, cat_id)
    if _cat and _cat.bot_id == row.id and getattr(_cat, "sale_full", False):
        await cb.answer(f"⛔️ ظرفیتِ فروشِ «{_cat.title}» پر شده؛ لطفاً از دستهٔ دیگری استفاده کن.",
                        show_alert=True)
        return
    await cb.answer()
    data = await state.get_data()
    mb_id = data.get("vbuy_mb_id")
    await show_bundle_hub(bot, _EditFirst(cb.message, cb.from_user), row.id, cat_id, state=state, mb_id=mb_id)


async def _all_services_gather(bot_id: int, viewer_id: int | None = None):
    """معادلِ _bundle_gather ولی بدونِ محدودشدن به یک دستهٔ ترکیبیِ خاص —
    همهٔ پلن‌هایِ فعالِ ربات (هر پنلی، حتی بی‌دسته‌های ترکیبی) + همهٔ
    دسته‌هایِ انبارِ قیمت‌دار."""
    from models import Config
    async with Session() as s:
        plans = list((await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == bot_id, VpnPlan.active == True,
            (await _plan_audience_filter(bot_id, viewer_id))[0]))).all())  # noqa: E712
        stock_names = sorted(set((await s.scalars(select(Config.category).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.price > 0, Config.category.is_not(None)).distinct())).all()))

    plan_cat_names = {(p.category or "").strip() for p in plans if (p.category or "").strip()}
    has_uncat = any(not (p.category or "").strip() for p in plans)
    bucket_names = bucket_order(plans, plan_cat_names | set(stock_names))
    return plans, stock_names, bucket_names, has_uncat


async def show_bundle_all_services(bot: Bot, msg: Message, bot_id: int, state=None,
                                   mb_id: int | None = None, header: str = "") -> None:
    await _drop_reply_kb(msg, bot_id)      # PATCH_FLOW_DROP_REPLY_KB_V1
    from tenant import inline_back_kb
    plans, stock_names, bucket_names, has_uncat = await _all_services_gather(bot_id, getattr(msg.from_user, 'id', None))
    total_buckets = len(bucket_names) + (1 if has_uncat else 0)
    if total_buckets == 0:
        await msg.answer("📭 فعلاً هیچ سرویسی موجود نیست.", reply_markup=await inline_back_kb(bot_id))
        return
    if total_buckets == 1:
        only_name = bucket_names[0] if bucket_names else None
        await _render_bundle_bucket(bot, msg, bot_id, plans, only_name, mb_id=mb_id,
                                    header=header, stock_ok=bool(only_name),
                                    title_override="همهٔ سرویس‌ها", icon_override="📦",
                                    state=state, reopen="bndl:allsvc")
        return

    if state is not None:
        await state.update_data(vbuy_mb_id=mb_id)
    from tenant import inline_back_row, _with_btn_header
    _ico_on = await _cat_icons_on(bot_id)
    rows = [[InlineKeyboardButton(text=_with_icon(_ico_on, "🏷", n), callback_data=f"bndl:svc:{i}",
                                  style=ButtonStyle.PRIMARY)]
            for i, n in enumerate(bucket_names)]
    if has_uncat:
        rows.append([InlineKeyboardButton(
            text=_with_icon(_ico_on, "🌐", "سایر"), callback_data=f"bndl:svc:{len(bucket_names)}",
            style=ButtonStyle.PRIMARY)])
    _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    text = _with_btn_header(  # === PATCH_VPN_EDITABLE_TITLES_V1 ===
        _with_icon(_ico_on, "📦", f"<b>{await _get_page_title(bot_id, 'all_svcs')}</b>") + "\n\nیک دسته را انتخاب کن:", header)
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@vpn_user_router.callback_query(F.data.startswith("bndl:svc:"))
async def bundle_pick_service_bucket(cb: CallbackQuery, bot: Bot, state: FSMContext):
    idx = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    plans, stock_names, bucket_names, has_uncat = await _all_services_gather(row.id, cb.from_user.id)
    name = bucket_names[idx] if idx < len(bucket_names) else None
    await cb.answer()
    data = await state.get_data()
    mb_id = data.get("vbuy_mb_id")
    await _render_bundle_bucket(bot, _EditFirst(cb.message, cb.from_user), row.id, plans, name, mb_id=mb_id,
                                stock_ok=(name in stock_names) if name else False,
                                title_override="همهٔ سرویس‌ها", icon_override="📦",
                                back_cb="bndl:allsvc",   # PATCH_BUNDLE_BACK_ONE_LEVEL_V1
                                state=state, reopen=f"bndl:svc:{idx}")


# ─────────────── خلاصهٔ خرید ───────────────
# === PATCH_PAYMENT_GUIDE_BUTTON_V1 ===
# درخواستِ کاربران: در صفحهٔ پیش‌فاکتور یک دکمهٔ «راهنمای پرداخت» بالایِ
# دکمهٔ بازگشت باشد — چون خیلی‌ها موجودیشان کافی نیست و نمی‌دانند چطور
# شارژ کنند، و همان‌جا رها می‌کنند.
#
# متنش را مالکِ ربات از «دکمه‌ها و تنظیمات» می‌نویسد، با همهٔ قابلیت‌هایِ
# متنیِ تلگرام (بولد، لینک، ایموجی...). برایِ همین `html_text` ذخیره
# می‌شود نه `text`، وگرنه قالب‌بندی از بین می‌رود.
# === PATCH_PAYGUIDE_MOVE_FINANCE_PRO_V1 ===
# فقط پلنِ حرفه‌ای؛ در پلن‌هایِ پایین‌تر حتی اگر مالک متن نوشته و روشن
# کرده باشد، به کاربرِ نهایی نشان داده نمی‌شود.
async def _payguide_visible(bot_id: int, surface: str = "invoice") -> bool:
    """آیا راهنمای پرداخت برای سطحِ مشخص‌شده قابلِ نمایش است؟"""
    from tenant import get_setting, get_bot_row_by_id
    from plans import has_feature, Feature
    if await get_setting(bot_id, "payguide_on", "") != "1":
        return False
    if not (await get_setting(bot_id, "payguide_text", "")).strip():
        return False
    key = "payguide_show_wallet" if surface == "wallet" else "payguide_show_invoice"
    default = "0" if surface == "wallet" else "1"
    if await get_setting(bot_id, key, default) != "1":
        return False
    _row = await get_bot_row_by_id(bot_id)
    return bool(_row and has_feature(_row.plan, Feature.PAYMENT_GUIDE))


async def _pay_guide_row(bot_id: int) -> list | None:
    """ردیفِ راهنما برایِ پیش‌فاکتور، با تیکِ مستقلِ نمایش در فاکتور."""
    if not await _payguide_visible(bot_id, "invoice"):
        return None
    return [InlineKeyboardButton(
        text="💰 راهنمای پرداخت و افزایش موجودی",
        callback_data="payguide:show:invoice")]


@vpn_user_router.callback_query(F.data.startswith("payguide:show"))
async def pay_guide_show(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    surface = parts[2] if len(parts) > 2 and parts[2] in {"invoice", "wallet"} else "invoice"
    if not await _payguide_visible(row.id, surface):
        await cb.answer("این راهنما در این بخش فعال نیست.", show_alert=True)
        return
    txt = (await get_setting(row.id, "payguide_text", "")).strip()
    if not txt:
        await cb.answer("راهنمایی ثبت نشده.", show_alert=True)
        return
    # پیامِ جدا می‌فرستیم و پیش‌فاکتور را دست نمی‌زنیم، تا کاربر بتواند
    # راهنما را بخواند و بعد همان‌جا پرداخت را ادامه دهد.
    await cb.message.answer(txt, parse_mode="HTML",
                            disable_web_page_preview=False)
    await cb.answer()


async def _build_buy_summary(bot: Bot, bot_id: int, plan_id: int,
                             telegram_id: int, state):
    """متن و کیبورد خلاصهٔ خرید را می‌سازد و کد تخفیفِ ذخیره‌شده در state را
    (اگر برای همین پلن باشد) اعمال می‌کند. اگر پلن نامعتبر بود (None, None)
    برمی‌گرداند. کد تخفیف فقط محاسبه می‌شود؛ ثبت مصرف موقع پرداخت انجام می‌شود."""
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != bot_id or not plan.active:
            return None, None
        if not await _plan_allowed_for(plan, bot_id, telegram_id):
            return None, None
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        bal = float(u.balance) if u else 0
        buyer_name = (u.full_name if u and u.full_name else "") or ""
        buyer_uname = (u.username if u and u.username else "") or ""
        card_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_number"))
        card_toggle_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_enabled"))
        card_buy_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_purchase_enabled"))
        # باگ: قبلاً فقط چک می‌شد که شماره‌کارتی ثبت شده یا نه، بدونِ توجه به
        # تاگلِ «کارت‌به‌کارت» که ادمین از پنل خاموش/روشن می‌کنه؛ در نتیجه حتی
        # وقتی ادمین کارت‌به‌کارت رو غیرفعال می‌کرد، دکمه‌اش همچنان نمایش
        # داده می‌شد. الان هر سه شرط چک می‌شه (شمارهٔ کارت + تاگلِ کلی +
        # تاگلِ «وضعیت در خرید» که کارت‌به‌کارت رو فقط در خرید خاموش می‌کنه).
        card_toggle_on = (card_toggle_set.value if card_toggle_set else "1") == "1"
        card_buy_on = (card_buy_set.value if card_buy_set else "1") == "1"
        card_enabled = bool(card_set and card_set.value) and card_toggle_on and card_buy_on
        vol = "نامحدود" if plan.volume_gb == 0 else f"{plan.volume_gb} گیگ"
        dur = "بی‌انقضا" if plan.days == 0 else f"{plan.days} روز"
        base_price = int(float(plan.price))
        p_title, p_panel = plan.title, plan.panel_key
        # === PATCH_VBUYBACK_KEEP_CONTEXT_V2 ===
        # نسخهٔ V1 پنل را از `vbuy_panel_key` می‌خواند که فقط وقتی پر
        # می‌شود که کاربر از مسیرِ show_vpn_plans آمده باشد. ولی وقتی از
        # «🌐 همهٔ دسته‌ها» (مسیرِ bundle) وارد شود، آن تابع اصلاً صدا زده
        # نمی‌شود و کلید خالی می‌ماند — پس دکمهٔ «بازگشت»ِ پیش‌فاکتور باز
        # هم پنلِ اشتباه برمی‌داشت و همان خطا را می‌داد.
        #
        # پنلِ درست همیشه در خودِ پلن هست. همین‌جا — که پیش‌فاکتور ساخته
        # می‌شود، یعنی دقیقاً صفحه‌ای که دکمهٔ بازگشتش مشکل داشت — ذخیره‌اش
        # می‌کنیم تا از هر مسیری که آمده باشد درست باشد.
        if state is not None:
            await state.update_data(vbuy_panel_key=p_panel or "")

    # کد تخفیفِ اعمال‌شده (فقط اگر برای همین پلن ذخیره شده باشد)
    data = await state.get_data() if state else {}
    applied_code = data.get("vdisc_code") if data.get("vdisc_plan_id") == plan_id else None
    disc = None
    final_price = base_price
    ref_disc = None   # تخفیف خودکارِ زیرمجموعه‌ای (فقط وقتی کد نداریم)
    if applied_code:
        disc = await validate_discount(
            bot_id=bot_id, code=applied_code, telegram_id=telegram_id,
            price=base_price, panel_key=p_panel, plan_id=plan_id, button_id=data.get("vbuy_mb_id"))
        if disc.ok and disc.discount > 0:
            final_price = disc.final_price
        else:
            # کد دیگر معتبر نیست → از state پاک کن و نادیده بگیر
            if state:
                await state.update_data(vdisc_code=None, vdisc_plan_id=None)
            applied_code, disc = None, None

    # === PATCH_VPN_PARTNER_V1 ===
    # تخفیفِ همکار / تخفیفِ خودکارِ زیرمجموعه‌ای — فقط وقتی هیچ کدی اعمال
    # نشده (با کد ترکیب نمی‌شوند). اولویت: همکار > زیرمجموعه‌گیری (چون
    # تخفیفِ همکار مشخصاً برای همین شخص تنظیم شده).
    ref_disc_kind = None  # "partner" یا "referral" — برایِ متنِ نمایشی
    if not (disc and disc.ok and disc.discount > 0):
        from tenant.bots.vpn.vpn_partners import partner_discount
        # === PATCH_RESELLER_PANEL_ONLY_V1 ===
        pd_ = await partner_discount(
            bot_id=bot_id, telegram_id=telegram_id, price=base_price,
            panel_key=p_panel, scope="plans", include_auto_reseller=False,
            gb=plan.volume_gb)
        if pd_.ok and pd_.discount > 0:
            ref_disc = pd_
            ref_disc_kind = "partner"
            final_price = pd_.final_price
        else:
            from tenant.gateways.discount_engine import referral_discount
            rd = await referral_discount(
                bot_id=bot_id, telegram_id=telegram_id, price=base_price)
            if rd.ok and rd.discount > 0:
                ref_disc = rd
                ref_disc_kind = "referral"
                final_price = rd.final_price

    # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
    # تخفیفِ خودکارِ «اولین خرید» — آخرین حلقهٔ زنجیره، فقط وقتی هیچ کد،
    # تخفیفِ همکار یا زیرمجموعه‌ای اعمال نشده. با بقیه جمع نمی‌شود تا
    # قیمت زیرِ قیمتِ تمام‌شده نرود.
    first_disc = None
    if not (disc and disc.ok and disc.discount > 0) and ref_disc is None:
        from tenant.gateways.discount_engine import first_purchase_discount
        fd = await first_purchase_discount(
            bot_id=bot_id, telegram_id=telegram_id, price=base_price)
        if fd.ok and fd.discount > 0:
            first_disc = fd
            final_price = fd.final_price

    # === PATCH_WALLET_PAY_TOGGLE_V1 ===
    from tenant import wallet_pay_on as _wpay_on
    kb_rows = ([[InlineKeyboardButton(text="✅ پرداخت از کیف پول",
                callback_data=f"vpay:{plan_id}", style=ButtonStyle.SUCCESS)]]
               if await _wpay_on(bot_id) else [])
    # === PATCH_ABANGATEWAY_REPLACE_CARD_V1 ===
    # اگر ادمین آبان گیت‌وی را «به‌جایِ کارت‌به‌کارتِ دستی» گذاشته، این دکمه پنهان
    # می‌شود و پایین‌تر دکمهٔ آبان گیت‌وی (همیشه، نه فقط با موجودیِ کم) می‌آید.
    from tenant import _ab_replaces_card
    _ab_repl = await _ab_replaces_card(bot_id)
    if card_enabled and not _ab_repl:
        kb_rows.append([InlineKeyboardButton(text="💳 پرداخت کارت‌به‌کارت",
                        callback_data=f"vpay:card:{plan_id}")])
    # === PATCH_WALLET_TOPUP_ROWS_V1 ===
    # اگر موجودی کم است، روش‌هایِ شارژِ فعال (کارت، تون‌پیز، ارز دیجیتال،
    # سواپ‌ولت) همین‌جا پیشنهاد می‌شوند — قبلاً کاربر فقط جملهٔ «اول شارژ کن»
    # را می‌دید بدونِ هیچ راهی، و تون‌پیز هرگز در صفحهٔ خرید دیده نمی‌شد.
    # === PATCH_ABANGATEWAY_VPN_PLAN_RESUME_V1 ===
    # گزارشِ ادمین (کسری): خریدِ VPN فقط «شارژِ کیف‌پول» پیشنهاد می‌داد و بعدش کاربر
    # باید دستی برمی‌گشت و دوباره پلن را می‌خرید. حالا اگر آبان گیت‌وی وصل باشد، دکمهٔ
    # پرداختِ مستقیم برایِ *دقیقاً همین مبلغ* اضافه می‌شود؛ بعدِ تاییدِ پرداخت، دکمهٔ
    # «تکمیلِ خرید» (tenant/gateways/abangateway.py) خودِ همین vpay:{plan_id} را صدا می‌زند.
    # === PATCH_ABANGATEWAY_REPLACE_CARD_V1 === در حالتِ «جایگزینِ کارت»، این دکمه حتی با
    # موجودیِ کافی هم می‌آید (مثلِ خودِ کارت‌به‌کارت)، و چون دکمهٔ مستقیم هست، دکمهٔ عمومیِ
    # «شارژ با آبان» در ردیف‌هایِ شارژ تکرار نمی‌شود.
    _ab_direct_btn = None
    # === PATCH_ABAN_ALWAYS_SHOW_PURCHASE_V1 ===
    # گزارش: «نمایش آبان‌گیت موقعِ پرداختِ محصول فعاله ولی نشون نمی‌ده». علت: دکمه فقط وقتی
    # موجودی کم بود می‌آمد؛ در حالی که کارت‌به‌کارت همیشه نشان داده می‌شود. حالا اگر تیکِ
    # «نمایش در خرید» روشن باشد، با موجودیِ کافی هم دکمهٔ پرداختِ مستقیم دیده می‌شود.
    if True:
        try:
            from tenant import get_setting as _gs_ab, gw_btn
            from tenant.gateways.abangateway import is_available as _ab_ok3
            if (_ab_repl or await _gs_ab(bot_id, "abangateway_show_purchase", "1") == "1") and \
                    await _ab_ok3(bot_id):
                _ab_direct_btn = InlineKeyboardButton(
                    text=await gw_btn(bot_id, 'abangateway', 'پرداخت با', '💳'),
                    callback_data=f"w:abangateway:vpn:{plan_id}:{int(final_price)}")
        except Exception:
            logger.exception("بررسیِ دسترسیِ آبان گیت‌وی برایِ خریدِ VPN ناموفق بود")
    # === PATCH_APAY_DIRECT_PURCHASE_V1 === پرداختِ مستقیمِ آپی برایِ همین پلن (مبلغِ کسری خودکار)
    _ap_direct_btn = None
    try:
        from tenant import get_setting as _gs_ap, gw_btn as _gwb_ap
        from tenant.gateways.apay import is_available as _ap_ok_d
        if bal < final_price and await _gs_ap(bot_id, "apay_show_purchase", "1") == "1" and await _ap_ok_d(bot_id):
            _ap_direct_btn = InlineKeyboardButton(
                text=await _gwb_ap(bot_id, 'apay', 'پرداخت با', '💳'),
                callback_data=f"w:apay:vpn:{plan_id}:{int(final_price)}")
    except Exception:
        logger.exception("بررسیِ آپی برایِ خریدِ پلنِ VPN ناموفق بود")
    # === PATCH_WALLET_TOPUP_ROWS_V1 ===
    # اگر موجودی کم است، روش‌هایِ شارژِ فعال (کارت، تون‌پیز، ارز دیجیتال، سواپ‌ولت) همین‌جا
    # پیشنهاد می‌شوند — قبلاً کاربر فقط جملهٔ «اول شارژ کن» را می‌دید بدونِ هیچ راهی.
    if bal < final_price:
        from tenant import wallet_topup_rows
        # === PATCH_WALLET_TOPUP_SKIP_CARD_V1 ===
        # اگر «پرداخت کارت‌به‌کارت»ِ مستقیم بالا اضافه شده، «شارژ کارت‌به‌کارت» تکرار نمی‌شود.
        kb_rows.extend(await wallet_topup_rows(
            bot_id, skip_card=card_enabled, skip_abangateway=_ab_direct_btn is not None,
            skip_apay=_ap_direct_btn is not None))
    # === PATCH_WALLET_CHARGE_BTN_V1 === تیکِ «شارژ کیف‌پول در صفحهٔ خرید» (مستقل از کمبودِ موجودی)
    from tenant import wallet_charge_btn_rows as _wcb, _has_wallet_charge_btn as _hwc
    if not _hwc(kb_rows):
        kb_rows.extend(await _wcb(bot_id))
    if _ab_direct_btn is not None:
        kb_rows.append([_ab_direct_btn])
    if _ap_direct_btn is not None:
        kb_rows.append([_ap_direct_btn])
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    # باگِ گزارش‌شده: تنظیمِ «استفاده از کد تخفیف در خرید» رو ادمین غیرفعال
    # کرده بود، ولی دکمه‌ی «وارد کردن کد تخفیف» همچنان تو صفحه‌ی خریدِ VPN
    # دیده می‌شد — چون discount_purchase_enabled اصلاً تو این فایل
    # (tenant/vpn_user.py) هیچ‌جا چک نمی‌شد (برخلافِ فروشگاه/انبارِ دستی که
    # قبلاً درست چک می‌کردن).
    from tenant import get_setting as _gs
    if await _gs(bot_id, "discount_purchase_enabled", "1") == "1":
        if applied_code and disc and disc.ok:
            kb_rows.append([InlineKeyboardButton(
                text=f"❌ حذف کد تخفیف ({applied_code})",
                callback_data=f"vdisc:del:{plan_id}")])
        else:
            kb_rows.append([InlineKeyboardButton(
                text="🏷 وارد کردن کد تخفیف", callback_data=f"vdisc:add:{plan_id}")])
    # PATCH_PAYMENT_GUIDE_BUTTON_V1 — بالایِ دکمهٔ بازگشت
    _pg = await _pay_guide_row(bot_id)
    if _pg:
        kb_rows.append(_pg)
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vbuyback",
                    style=ButtonStyle.DANGER)])

    enough = "" if bal >= final_price else "\n\n⚠️ موجودی کیف پول کافی نیست؛ اول شارژ کن."
    if disc and disc.ok and disc.discount > 0:
        price_block = (f"💵 قیمت: <s>{rial(base_price)}</s>\n"
                       f"🏷 تخفیف ({applied_code}): <b>−{rial(disc.discount)}</b>\n"
                       f"💰 مبلغ پرداختی: <b>{rial(final_price)}</b>")
    elif ref_disc and ref_disc.ok and ref_disc.discount > 0:
        _disc_label = "🤝 تخفیف همکار" if ref_disc_kind == "partner" else "🎁 تخفیف زیرمجموعه‌گیری"
        price_block = (f"💵 قیمت: <s>{rial(base_price)}</s>\n"
                       f"{_disc_label}: <b>−{rial(ref_disc.discount)}</b>\n"
                       f"💰 مبلغ پرداختی: <b>{rial(final_price)}</b>")
    elif first_disc and first_disc.ok and first_disc.discount > 0:
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        price_block = (f"💵 قیمت: <s>{rial(base_price)}</s>\n"
                       f"🎁 تخفیفِ اولین خرید: <b>−{rial(first_disc.discount)}</b>\n"
                       f"💰 مبلغ پرداختی: <b>{rial(final_price)}</b>")
    else:
        price_block = f"💵 قیمت: <b>{rial(base_price)}</b>"
    # اطلاعات کاربریِ خریدار (بالای پیش‌فاکتور)
    buyer_lines = f"👤 خریدار: {buyer_name or '—'}"
    if buyer_uname:
        buyer_lines += f" (@{buyer_uname})"
    buyer_lines += f"\n🆔 شناسه: <code>{telegram_id}</code>"
    # اسم دلخواهِ انتخاب‌شده (اگر در همین مرحله ثبت شده باشد)
    custom_label = (data.get("vcustom_label") or "").strip() if data else ""
    label_line = f"\n🏷 نام کانفیگ: <b>{custom_label}</b>" if custom_label else ""

    note_line = ""
    if p_panel:
        try:
            from tenant.bots.vpn.vpn_admin import get_panel_note
            _note = await get_panel_note(bot_id, p_panel, "invoice")
            if _note:
                note_line = f"📝 {_note}\n"
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    text = (f"🧾 <b>پیش فاکتور</b>\n"
            f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            f"{buyer_lines}\n"
            f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            f"🛰 <b>{p_title}</b>{label_line}\n\n"
            f"📦 حجم: {vol}\n📅 مدت: {dur}\n{price_block}\n"
            f"{note_line}"
            f"💳 موجودی کیف پول: {rial(bal)}{enough}")
    # === PATCH_PRODUCT_RATING_V1 ===
    # میانگینِ امتیازِ همین پلن (اگه نظرسنجی روشن باشه و امتیازی ثبت شده باشه)
    # امتیاز بر اساسِ *دستهٔ* پلن ذخیره/نمایش داده می‌شه (نه آیدیِ تکِ پلن)،
    # چون پلن‌ها و کانفیگ‌ها دسته‌بندی می‌شن و دسته شناسهٔ پایدارتریه.
    try:
        from tenant.shared.product_rating import rating_line, cat_key
        _pcat = None
        async with Session() as _s:
            _p = await _s.get(VpnPlan, plan_id)
            _pcat = getattr(_p, "category", None) if _p else None
        _line = await rating_line(bot_id, cat_key(_pcat))
        if _line:
            text += _line
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (نمایشِ امتیازِ دستهٔ پلن)")
    return text, await _with_home_row(InlineKeyboardMarkup(inline_keyboard=kb_rows), bot_id)


async def _ask_custom_name(target, plan_id: int, kind: str = "plan",
                           bot_id: int = 0):  # === PATCH_CFG_NAME_PROMPT_EDIT_V1 ===
    """صفحهٔ پرسیدن اسم دلخواه (اختیاری) قبل از پیش‌فاکتور.
    kind: 'plan' برای پلن آماده | 'vc' برای خرید دلخواه. برای vc، plan_id بی‌اثر است
    و مشخصات از state خوانده می‌شود."""
    skip_cb = f"vname:skip:{plan_id}" if kind == "plan" else "vcname:skip"
    back_cb = "vbuyback" if kind == "plan" else "vc:back"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ بدون نام (ادامه)", callback_data=skip_cb,
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت به منوی قبل", callback_data=back_cb,
                              style=ButtonStyle.DANGER)],
    ])
    if bot_id:      # PATCH_FLOW_DROP_REPLY_KB_V1 — «بازگشت به منو» هم باشد (درخواستِ ایلیا)
        try:
            from tenant import inline_back_row
            _hr = await inline_back_row(bot_id)
            if _hr:
                kb.inline_keyboard.append(_hr)
        except Exception:
            logger.exception("vpn: افزودنِ ردیفِ بازگشت به منو در صفحهٔ نامِ دلخواه ناموفق بود")
# === PATCH_CFG_NAME_PROMPT_EDIT_V1 ===
    # متن از تنظیماتِ ربات خوانده می‌شود تا مالک بتواند عوضش کند؛ اگر
    # چیزی ثبت نکرده باشد، همین متنِ پیش‌فرض می‌ماند.
    txt = await get_cfg_name_prompt(bot_id) if bot_id else CFG_NAME_PROMPT_DEFAULT
    await target.answer(txt, reply_markup=kb)


# === PATCH_CFG_NAME_PROMPT_EDIT_V1 ===
CFG_NAME_PROMPT_DEFAULT = (
    "✏️ <b>یک نام دلخواه برای این کانفیگ بفرست</b>\n\n"
    "(این نام فقط برای نمایش در «کانفیگ‌های من» استفاده می‌شود — "
    "هر چیزی می‌تواند باشد مثل: خانه، مدرسه، موبایل)\n\n"
    "یا دکمهٔ «بدون نام» را بزن تا بدون نام ادامه بدهی."
)


async def get_cfg_name_prompt(bot_id: int) -> str:
    """متنِ مرحلهٔ «نامِ دلخواهِ کانفیگ» — قابلِ ویرایش توسطِ مالک."""
    from tenant import get_setting
    try:
        custom = (await get_setting(bot_id, "cfg_name_prompt", "") or "").strip()
    except Exception:
        logger.exception("خواندنِ متنِ نامِ کانفیگ ناموفق بود")
        custom = ""
    return custom or CFG_NAME_PROMPT_DEFAULT


@vpn_user_router.callback_query(F.data.startswith("vbuy:"))
async def vpn_buy(cb: CallbackQuery, bot: Bot, state: FSMContext):
    plan_id = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        plan = await s.get(VpnPlan, plan_id) if row else None
        plan_ok = bool(plan and row and plan.bot_id == row.id and plan.active)
    if not row:
        await cb.answer()
        return
    if not plan_ok:
        await cb.answer("این پلن دیگر در دسترس نیست.", show_alert=True)
        return
    _full_msg = await plan_sale_full_message(row.id, plan_id)      # PATCH_CATEGORY_SALE_FULL_V1
    if _full_msg:
        await cb.answer("⛔️ ظرفیتِ فروشِ این دسته پر شده؛ لطفاً از دستهٔ دیگری استفاده کن.", show_alert=True)
        return
    # قدم ۱: پرسیدن اسم دلخواه (اختیاری). اسمِ قبلیِ همین پلن را از state پاک کن.
    await state.set_state(_VpnNameFSM.naming)
    await state.update_data(vname_plan_id=plan_id, vcustom_label=None)
    await cb.answer()
    await _ask_custom_name(_EditFirst(cb.message, cb.from_user), plan_id, "plan", row.id)  # === PATCH_CFG_NAME_PROMPT_EDIT_V1 ===


async def _show_plan_preinvoice(target, bot: Bot, bot_id: int, plan_id: int,
                                telegram_id: int, state):
    """پیش‌فاکتور پلن آماده را نمایش می‌دهد (پس از مرحلهٔ نام)."""
    text, kb = await _build_buy_summary(bot, bot_id, plan_id, telegram_id, state)
    if text is None:
        await target.answer("این پلن دیگر در دسترس نیست.")
        return
    await target.answer(text, reply_markup=kb)


@vpn_user_router.callback_query(F.data.startswith("vname:skip:"))
async def vpn_name_skip(cb: CallbackQuery, bot: Bot, state: FSMContext):
    plan_id = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    await state.set_state(None)
    await state.update_data(vcustom_label=None)
    await cb.answer()
    await _show_plan_preinvoice(_EditFirst(cb.message, cb.from_user), bot, row.id, plan_id, cb.from_user.id, state)


# === PATCH_VPN_NAME_STEP_EXIT_TEXT_V1 ===
# باگِ گزارش‌شده: کاربر دقیقاً وسطِ مرحلهٔ «اسمِ دلخواه» (بعد از انتخابِ پلن)
# روی دکمهٔ ریپلای-کیبوردِ «⬅️ بازگشت به سرویس‌ها و محصولات» (یا مشابه‌هایِ
# آن) می‌زد. چون این متن هیچ‌کدام از حالت‌هایِ «/skip»، «/cancel»، «لغو» یا
# «انصراف» نبود، این‌جا به‌عنوانِ خودِ اسمِ دلخواه ذخیره می‌شد و بعداً به‌جایِ
# عنوانِ واقعیِ پلن، همه‌جا (اسمِ سرویس، اطلاعِ ادمین، گزارشِ کانالِ
# اعتمادسازی) نمایش داده می‌شد. همان کلاسِ باگی که قبلاً در shop.py
# (PATCH_RECEIPT_STATE_CANCEL_V1) پیدا و درست شده بود، این‌جا هنوز پوشش
# داده نشده بود.
_VPN_NAME_STEP_EXIT_TEXTS = (
    "⬅️ بازگشت به سرویس‌ها و محصولات", "بازگشت به سرویس‌ها و محصولات",
    "⬅️ بازگشت به منو", "بازگشت به منو", "🔴 بازگشت",
    "❌ لغو", "❌ لغو / بازگشت", "لغو", "بازگشت", "انصراف",
    "/skip", "/cancel", "/start",
)


def _is_vpn_name_step_exit(txt: str) -> bool:
    return (txt or "").strip().lower() in tuple(t.lower() for t in _VPN_NAME_STEP_EXIT_TEXTS)


@vpn_user_router.message(_VpnNameFSM.naming, F.text)
async def vpn_name_enter(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    plan_id = data.get("vname_plan_id")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or not plan_id:
        await state.set_state(None)
        await msg.answer("این پلن دیگر در دسترس نیست.")
        return
    txt = (msg.text or "").strip()
    # === PATCH_VPN_NAME_STEP_EXIT_TEXT_V1 ===
    if _is_vpn_name_step_exit(txt):
        await state.update_data(vcustom_label=None, vname_plan_id=None)
        await state.set_state(None)
        from tenant import tenant_buttons
        await tenant_buttons(msg, bot, state)
        return
    if txt.lower() in ("/skip", "/cancel", "لغو", "انصراف"):
        await state.update_data(vcustom_label=None)
    else:
        await state.update_data(vcustom_label=txt[:60])
    await state.set_state(None)
    await _show_plan_preinvoice(msg, bot, row.id, plan_id, msg.from_user.id, state)


# ─────────────── کد تخفیف در خرید VPN ───────────────
@vpn_user_router.callback_query(F.data.startswith("vdisc:add:"))
async def vpn_disc_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    plan_id = int(cb.data.split(":")[2])
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    from tenant import get_bot_row, get_setting
    _row = await get_bot_row(bot)
    if _row and await get_setting(_row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف در حال حاضر غیرفعال است.", show_alert=True)
        return
    await state.set_state(_VpnDiscountFSM.entering)
    await state.update_data(vdisc_target_plan=plan_id)
    await cb.answer()
    await cb.message.answer("🏷 کد تخفیف را وارد کن:\n(یا /cancel برای انصراف)")


@vpn_user_router.message(_VpnDiscountFSM.entering)
async def vpn_disc_enter(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    plan_id = data.get("vdisc_target_plan")
    txt = (msg.text or "").strip()
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        plan = await s.get(VpnPlan, plan_id) if plan_id else None
        plan_ok = bool(plan and row and plan.bot_id == row.id and plan.active)
        base_price = int(float(plan.price)) if plan_ok else 0
        p_panel = plan.panel_key if plan_ok else None
    if not row or not plan_ok:
        await state.set_state(None)
        await msg.answer("این پلن دیگر در دسترس نیست.")
        return
    if txt.lower() in ("/cancel", "لغو", "انصراف"):
        await state.set_state(None)
        text, kb = await _build_buy_summary(bot, row.id, plan_id, msg.from_user.id, state)
        await msg.answer("لغو شد.")
        if text:
            await msg.answer(text, reply_markup=kb)
        return
    res = await validate_discount(
        bot_id=row.id, code=txt, telegram_id=msg.from_user.id,
        price=base_price, panel_key=p_panel, plan_id=plan_id, button_id=data.get("vbuy_mb_id"))
    if not res.ok:
        await msg.answer(f"❌ {res.reason}\n\nدوباره وارد کن یا /cancel بزن.")
        return
    await state.update_data(vdisc_code=res.code, vdisc_plan_id=plan_id)
    await state.set_state(None)
    text, kb = await _build_buy_summary(bot, row.id, plan_id, msg.from_user.id, state)
    await msg.answer(f"✅ کد «{res.code}» اعمال شد.")
    if text:
        await msg.answer(text, reply_markup=kb)


@vpn_user_router.callback_query(F.data.startswith("vdisc:del:"))
async def vpn_disc_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    plan_id = int(cb.data.split(":")[2])
    await state.update_data(vdisc_code=None, vdisc_plan_id=None)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    text, kb = await _build_buy_summary(bot, row.id, plan_id, cb.from_user.id, state)
    if text:
        await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("کد تخفیف حذف شد.")


@vpn_user_router.callback_query(F.data == "vbuyback")
async def vpn_buy_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.set_state(None)
    await state.update_data(vcustom_label=None)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        bot_id = row.id if row else 0
    if bot_id:
        # === PATCH_VBUYBACK_KEEP_CONTEXT_V1 ===
        # گزارشِ ادمین: در پیش‌فاکتور دکمهٔ «بازگشت» پیامِ «⛔️ فروش VPN
        # خودکار در حال حاضر فعال نیست» می‌داد.
        #
        # علت: این‌جا show_vpn_plans بدونِ `panel_key` و `mb_id` صدا زده
        # می‌شد. بدونِ panel_key تابع سراغِ `get_any_enabled_panel` می‌رود
        # و ممکن است پنلِ *دیگری* را بردارد؛ آن‌وقت کوئریِ پلن‌ها روی آن
        # پنلِ اشتباه چیزی برنمی‌گرداند و شرطِ «نه پلنی هست نه فروشِ
        # آنلاین» فعال می‌شود. یعنی کاربر با یک دکمهٔ سالم، پیامِ
        # «غیرفعال است» می‌گرفت.
        #
        # هر دو مقدار از قبل در state هستند (خودِ show_vpn_plans موقعِ ورود
        # `vbuy_mb_id` را ذخیره می‌کند)، پس فقط باید پس داده شوند.
        _d = await state.get_data()
        # === PATCH_VBUYBACK_REOPEN_V1 === اگر کاربر از صفحهٔ «دسته/سرویس» آمده، به همان‌جا برگردد
        _re = _d.get("vbuy_reopen")
        if _re:
            await cb.answer()
            _scb = _SilentCB(cb, _EditFirst(cb.message, cb.from_user), _re)
            _pp = _re.split(":")
            if _pp[:2] == ["bndl", "sub"]:
                await bundle_pick_bucket(_scb, bot, state)
            elif _pp[:2] == ["bndl", "svc"]:
                await bundle_pick_service_bucket(_scb, bot, state)
            elif _pp[:2] == ["bndl", "cat"]:
                await bundle_pick_category(_scb, bot, state)
            elif _re == "bndl:allsvc":
                await bundle_back_to_services(_scb, bot, state)
            return
        await show_vpn_plans(
            bot, _EditFirst(cb.message, cb.from_user), bot_id, state=state,   # PATCH_VPN_EDIT_IN_PLACE_V1
            panel_key=_d.get("vbuy_panel_key") or "",
            mb_id=_d.get("vbuy_mb_id"))
    await cb.answer()


# ─────────────── پرداخت + ساخت خودکار ───────────────
@vpn_user_router.callback_query(F.data.regexp(r"^vpay:\d+$"))
async def vpn_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    plan_id = int(cb.data.split(":")[1])
    disc_code_id = None      # برای عودت مصرف در صورت شکست ساخت
    applied_code = None
    discount_amt = 0

    # === PATCH_VPN_PAY_DEDUPE_V1 ===
    # اگر همین کاربر برای همین پلن، درخواستِ قبلی هنوز در حال پردازش است
    # (مثلاً چند بار پشت‌سرهم روی دکمه زده)، این تلاش را رد کن.
    _dedupe_key = (bot.token, cb.from_user.id, plan_id)
    if _dedupe_key in _vpn_pay_inflight:
        await cb.answer("⏳ درخواست قبلی‌ات هنوز در حال پردازش است، کمی صبر کن.", show_alert=True)
        return
    # === PATCH_PAY_PHONE_GATE_FIX_V1 ===
    if await _pay_gate_block(cb, bot):
        _vpn_pay_inflight.discard(_dedupe_key)
        return
    _vpn_pay_inflight.add(_dedupe_key)
    # === PATCH_PURCHASE_IDEMPOTENCY_V1 ===
    if not await _claim_or_reject(cb, bot, "vpay"):
        _vpn_pay_inflight.discard(_dedupe_key)
        return
    # دکمه را همین الان غیرفعال/حذف کن تا کلیک‌های بعدی روی همین پیام هم گرافیکی مسدود شوند
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")

    try:
        await _vpn_pay_impl(cb, bot, state, plan_id, disc_code_id, applied_code, discount_amt)
    finally:
        _vpn_pay_inflight.discard(_dedupe_key)


async def _vpn_pay_impl(cb: CallbackQuery, bot: Bot, state: FSMContext, plan_id: int,
                        disc_code_id, applied_code, discount_amt: int):
    # ۱) اعتبارسنجی و کسر موجودی (اتمیک)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        bot_id = row.id
        plan = await s.get(VpnPlan, plan_id)
        panel = None
        if plan and plan.bot_id == bot_id:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.panel_key == plan.panel_key))
        if (not plan or plan.bot_id != bot_id or not plan.active or not panel or not panel.enabled
                or not await _plan_allowed_for(plan, bot_id, cb.from_user.id)):
            await cb.answer("این پلن دیگر در دسترس نیست.", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        base_price = float(plan.price)
        price = base_price
        # ── کد تخفیف: اعتبارسنجی دوباره در لحظهٔ پرداخت (state ممکن است کهنه باشد) ──
        data = await state.get_data()
        stored_code = data.get("vdisc_code") if data.get("vdisc_plan_id") == plan_id else None
        if stored_code:
            res = await validate_discount(
                bot_id=bot_id, code=stored_code, telegram_id=cb.from_user.id,
                price=int(base_price), panel_key=plan.panel_key, plan_id=plan_id,
                button_id=data.get("vbuy_mb_id"), session=s)
            if res.ok and res.discount > 0:
                price = float(res.final_price)
                discount_amt = res.discount
                disc_code_id = res.code_id
                applied_code = res.code
        # === PATCH_VPN_PARTNER_V1 ===
        # تخفیفِ همکار / تخفیفِ خودکارِ زیرمجموعه‌ای — فقط وقتی کدی اعمال
        # نشده (ترکیب نمی‌شوند). اولویت: همکار > زیرمجموعه‌گیری.
        ref_discount_amt = 0
        ref_discount_kind = None
        if not applied_code:
            from tenant.bots.vpn.vpn_partners import partner_discount
            # === PATCH_RESELLER_PANEL_ONLY_V1 ===
            pd_ = await partner_discount(
                bot_id=bot_id, telegram_id=cb.from_user.id, price=int(base_price),
                panel_key=plan.panel_key, scope="plans", include_auto_reseller=False,
                gb=plan.volume_gb)
            if pd_.ok and pd_.discount > 0:
                price = float(pd_.final_price)
                ref_discount_amt = pd_.discount
                ref_discount_kind = "partner"
            else:
                from tenant.gateways.discount_engine import referral_discount
                rd = await referral_discount(
                    bot_id=bot_id, telegram_id=cb.from_user.id, price=int(base_price))
                if rd.ok and rd.discount > 0:
                    price = float(rd.final_price)
                    ref_discount_amt = rd.discount
                    ref_discount_kind = "referral"
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        # لحظهٔ کسرِ واقعیِ پول. دوباره حساب می‌شود (نه خواندن از state)
        # چون بینِ نمایشِ خلاصه و زدنِ «پرداخت» ممکن است کاربر خریدِ دیگری
        # کرده باشد و دیگر «اولین خرید» نباشد — همان الگویی که کدهایِ
        # تخفیف هم دارند.
        first_discount_amt = 0
        if not applied_code and not ref_discount_amt:
            from tenant.gateways.discount_engine import first_purchase_discount
            fd = await first_purchase_discount(
                bot_id=bot_id, telegram_id=cb.from_user.id, price=int(base_price))
            if fd.ok and fd.discount > 0:
                price = float(fd.final_price)
                first_discount_amt = fd.discount
        if not u or float(u.balance) < price:
            await cb.answer("موجودی کیف پول کافی نیست. اول شارژ کن.", show_alert=True)
            return
        u.balance = float(u.balance) - price
        note = f"خرید VPN: {plan.title}"
        if applied_code:
            note += f" (کد {applied_code} −{rial(discount_amt)})"
        elif ref_discount_amt:
            _kind_label = "تخفیف همکار" if ref_discount_kind == "partner" else "تخفیف زیرمجموعه‌گیری"
            note += f" ({_kind_label} −{rial(ref_discount_amt)})"
        elif first_discount_amt:   # PATCH_FIRST_PURCHASE_DISCOUNT_V1
            note += f" (تخفیف اولین خرید −{rial(first_discount_amt)})"
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price,
                        kind="purchase", note=note))
        # ثبت مصرف کد تخفیف در همان تراکنش (اتمیک با کسر کیف‌پول)
        if disc_code_id:
            await record_usage(s, code_id=disc_code_id, bot_id=bot_id,
                               telegram_id=cb.from_user.id, saved_amount=discount_amt)
        await s.commit()
        # نگه‌داشتن مقادیر لازم
        p_gb, p_days, p_title, p_limit = plan.volume_gb, plan.days, plan.title, plan.limit_ip
        p_title_plan = plan.title  # PATCH_TRUST_CHANNEL_NO_CUSTOM_LABEL_V1 — قبل از رونویسیِ زیر
        # اسم دلخواهِ کاربر — اگر انتخاب شده باشد هم جایگزین عنوان پلن (نمایشی) می‌شود
        # و هم (اگر تنظیمِ نام‌گذاریِ پنل روی "custom" باشد) در نام واقعیِ سرور استفاده می‌شود.
        _clabel = (data.get("vcustom_label") or "").strip()
        if _clabel:
            p_title = _clabel[:60]
        _clabel_for_srvname = _clabel[:60] or None  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
        user_db_id = u.id
        bot_username = row.username
        panel_data = (panel.base_url, panel.username, panel.password,
                      panel.sub_base or "", panel.inbound_ids, panel.panel_key, panel.api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1

    # خرید موفق → کد تخفیفِ ذخیره‌شده و اسم دلخواه در state را پاک کن
    await state.update_data(vdisc_code=None, vdisc_plan_id=None, vcustom_label=None)

    await cb.answer("در حال ساخت کانفیگ…")
    await _progress_edit(cb, "⏳ پرداخت موفق بود. در حال ساخت کانفیگ روی سرور…")

    # ۲) ساخت روی پنل
    email = await _make_config_email(
        bot_id, panel_data[5], bot_username, cb.from_user.id, cb.from_user.username,
        custom_label=_clabel_for_srvname)  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
    # === PATCH_PANEL_CLIENT_POOL_V1 ===
    # قبلاً اینجا یک کلاینتِ تازه ساخته می‌شد و بلافاصله بعدِ ساخت بسته
    # می‌شد؛ یعنی هر خرید یک TLS + csrf + login از صفر. حالا نشستِ
    # لاگین‌شده بینِ خریدها مشترک است. (کلاینت را close نمی‌کنیم — مالِ
    # استخر است.)
    from tenant.shared.panel_clients import panel_pool as _pool
    _pkw = dict(sub_base=panel_data[3], inbound_ids_str=panel_data[4],
                api_key=panel_data[6])
    client = await _pool.get_client(
        panel_data[5], panel_data[0], panel_data[1], panel_data[2], **_pkw)
    try:
        # === PATCH_PURCHASE_ON_HOLD_V1 ===
        # همان on_hold تست، این‌بار برایِ کانفیگ‌هایِ فروشی: زمانِ اشتراک از
        # اولین اتصالِ مشتری شروع می‌شود، نه لحظهٔ خرید. کلیدش از تستْ جداست
        # تا فروشنده بتواند فقط یکی را روشن کند.
        _poh = await purchase_on_hold_for(bot_id, panel_data[5])  # PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1
        # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
        # اگر خودِ پلن محدودیت ندارد، از پیش‌فرضِ پنل استفاده شود.
        _lim = int(p_limit or 0) or await panel_default_limit_ip(bot_id, panel_data[5])
        acc = await client.create_account(
            email, gb=p_gb, days=p_days, limit_ip=_lim,
            uuid_str=gen_uuid(), sub_id=gen_sub_id(), on_hold=_poh)
        sub_link = acc.get("sub_link") or ""
    except Exception as e:
        # === PATCH_RENEW_CATCH_ALL_V1 ===
        # همون دلیلِ نسخه‌های تمدید: قبلاً فقط PanelError گرفته می‌شد.
        logger.warning("خطای ساخت کانفیگ روی پنل (bot_id=%s, email=%s): %s", bot_id, email, e)
        # ۳) شکست ساخت → برگشت وجه + برگشت مصرف کد تخفیف
        async with Session() as s:
            u = await s.get(BotUser, user_db_id)
            if u:
                u.balance = float(u.balance) + price
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price,
                                kind="refund", note="عودت به‌دلیل خطای ساخت کانفیگ"))
            if disc_code_id:
                await revert_usage(s, code_id=disc_code_id,
                                   telegram_id=cb.from_user.id)
            await s.commit()
        refund_note = ""
        if applied_code:
            refund_note = f"\n🏷 کد تخفیف «{applied_code}» هم آزاد شد و می‌تونی دوباره استفاده کنی."
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, bot_id, "ساختِ کانفیگ (خرید)", e, cb.from_user.id)
        await cb.message.answer(
            f"❌ ساخت کانفیگ انجام نشد و مبلغ <b>{rial(price)}</b> به کیف پولت بازگشت.{refund_note}\n"
            f"{USER_FAIL_TEXT}")
        # === PATCH_PANEL_CLIENT_POOL_V1 ===
        # نشستِ مشکوک را از استخر بیرون بینداز تا خریدِ بعدی از نو وصل
        # شود؛ وگرنه یک کوکیِ خراب می‌تواند همهٔ خریدهایِ بعدی را بشکند.
        await _pool.drop(panel_data[5], panel_data[0], panel_data[1],
                         panel_data[2], **_pkw)
        return

    # ۴) ذخیرهٔ سرویس + تحویل
    from models import Order
    async with Session() as s:
        new_acc = PanelAccount(
            bot_id=bot_id, user_telegram_id=cb.from_user.id, email=email,
            panel_key=panel_data[5],
            uuid=acc.get("uuid"), sub_id=acc.get("sub_id"), sub_link=sub_link,
            volume_gb=p_gb, days=p_days, plan_title=p_title, status="active",
            paid_toman=int(price or 0) or None,   # PATCH_VPN_REFUND_V1
            svc_code=await new_svc_code(s, bot_id),
            expires_at=_expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False)
        s.add(new_acc)
        # === PATCH_VPN_WALLET_ORDER_RECORD_V1 ===
        # باگِ گزارش‌شده: خریدِ کیف‌پولیِ کانفیگِ زنده (این مسیر) هیچ‌وقت ردیفِ
        # Order نمی‌ساخت — فقط WalletTxn(kind="purchase") ثبت می‌شد. آمارِ ادمین
        # («سفارش‌های موفق»، «خریدارانِ موفق»، «درآمد کل») همه از رویِ جدولِ
        # Order حساب می‌شوند، پس این خریدها (که چون آنی و بدونِ تاییدِ دستی‌ان،
        # اکثریتِ خریدهای واقعی همین مسیرند — مخصوصاً وقتی موجودی از طریقِ
        # TonPays یا شارژِ دستیِ ادمین پر شده بود) اصلاً دیده نمی‌شدند. الان
        # دقیقاً مثلِ مسیرِ مشابهِ «خریدِ کیف‌پولیِ بستهٔ انبار» یک Order با
        # status="paid" ثبت می‌شود.
        s.add(Order(bot_id=bot_id, user_id=user_db_id, volume_gb=p_gb, days=p_days,
                    count=1, amount=price, pay_method="wallet", status="paid",
                    panel_key=panel_data[5], custom_label=_clabel_for_srvname,
                    plan_id=plan_id, button_id=data.get("vbuy_mb_id")))
        await s.commit()
        aid = new_acc.id
    # === PATCH_PANEL_CLIENT_POOL_V1 ===
    # اینجا عمداً close نمی‌کنیم: کلاینت مالِ استخر است و بستنش یعنی
    # خریدِ بعدی دوباره از صفر TLS+login بزند — همان چیزی که می‌خواستیم
    # حذف کنیم. استخر خودش نشستِ بی‌استفاده را بعد از ۱۵ دقیقه می‌بندد.

    # === PATCH_DELIVERY_NEVER_LOST_V1 ===
    # گزارشِ ادمین: «چند بار پیش اومده پول از کیف پول کم شده ولی ساب
    # ارسال نشده — پنل هم اوکیه».
    #
    # ریشه: از این خط تا آخرِ تابع دو فراخوانی بدونِ هیچ محافظی بودند.
    # تا اینجا پول کم شده و حسابِ پنل ساخته و در دیتابیس ذخیره شده —
    # یعنی خرید کاملاً موفق بوده — ولی اگر یکی از این دو استثنا می‌داد،
    # کلِ هندلر می‌ترکید و کاربر *هیچ‌چیز* نمی‌گرفت.
    #
    # چرا حالا بیشتر دیده می‌شود: اندازه‌گیری نشان داد تماس‌هایِ تلگرام سرِ
    # ۱۲ ثانیه تایم‌اوت می‌شوند و تا ۳ بار تکرار (۳۶ ثانیه+). در لاگ
    # آپدیت‌هایِ ۷۶ و ۸۷ ثانیه‌ای هم دیدیم.
    # === PATCH_DELIVERY_NEVER_LOST_V1 ===
    # تحویل با دو لایه محافظ: اگر پیامِ کامل (QR + دکمه‌ها) نرسید، دستِ‌کم
    # ساب‌لینکِ خام برود تا کاربر بی‌کانفیگ نماند. اگر آن هم نرسید، حساب
    # به‌هرحال ساخته شده و از «کانفیگ‌های من» در دسترس است — پس به مالکِ
    # ربات خبر می‌دهیم تا دستی پیگیری کند، نه اینکه بی‌سروصدا گم شود.
    try:
        await _send_config_delivery(bot, cb.from_user.id, aid)
    except Exception:
        logger.exception("تحویلِ کانفیگ ناموفق بود (aid=%s, tg_id=%s)",
                         aid, cb.from_user.id)
        try:
            await bot.send_message(
                cb.from_user.id,
                "✅ سرویست ساخته شد، ولی ارسالِ خودکارش به مشکل خورد.\n\n"
                + (f"🔗 <code>{sub_link}</code>\n\n" if sub_link else "")
                + "از بخشِ «کانفیگ‌های من» هم همیشه در دسترس است. "
                  "اگر مشکلی دیدی به پشتیبانی پیام بده.")
        except Exception:
            logger.exception("پیامِ جایگزینِ تحویل هم نرسید (aid=%s)", aid)
            try:
                async with Session() as s:
                    _r = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
                if _r and _r.owner_id:
                    await bot.send_message(
                        _r.owner_id,
                        f"⚠️ سرویسِ #{aid} برایِ کاربر <code>{cb.from_user.id}</code> "
                        "ساخته و پولش کم شد، ولی هیچ پیامی به او نرسید "
                        "(احتمالاً قطعیِ شبکه به تلگرام). لطفاً دستی پیگیری کن.")
            except Exception:
                logger.exception("هشدارِ تحویلِ ناموفق به مالک هم نرسید (aid=%s)", aid)

    # === PATCH_DELIVERY_FIRST_V1 === بعد از تحویل: پاداشِ دعوت + اطلاعِ خرید
    try:
        from tenant.shared.extras import handle_referral_purchase
        _bg(handle_referral_purchase(bot_id, cb.from_user.id, bot), "پاداش دعوت")
    except Exception:
        logger.exception("پاداشِ دعوت ناموفق بود (تحویلِ کانفیگ ادامه می‌یابد)")

    # === اطلاع خودکار خرید (پیامِ خصوصیِ ادمین + کانالِ اعتمادسازی، در صورتِ فعال‌بودن) ===
    # قبلاً این مسیر (خریدِ اصلیِ VPN از پنلِ متصل) هیچ‌کدوم از این دو اطلاع‌رسانی
    # رو نداشت — فقط مسیرهای «انبارِ دستی» و «فروشگاهِ دیجیتال» پوششش می‌دادن.
    try:
        from tenant import _purchase_alert
        async with Session() as s:
            _row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
            _buyer = await s.get(BotUser, user_db_id)
        if _row:
            # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
            from tenant.bots.vpn.vpn_admin import panel_label
            _pname = await panel_label(bot_id, panel_data[5])
            # === PATCH_ORDER_BUTTON_ID_V1 ===
            _bname = None
            _mb_id = data.get("vbuy_mb_id")
            if _mb_id:
                async with Session() as _s2:
                    _mb = await _s2.get(MainButton, _mb_id)
                    _bname = _mb.label if _mb else None
            await _purchase_alert(
                bot, _row, buyer=_buyer,
                section="سرویس VPN (پنل)",
                detail=_purchase_detail(p_title, p_gb, p_days), amount=price,
                public_detail=_purchase_detail(p_title_plan, p_gb, p_days),  # PATCH_TRUST_CHANNEL_NO_CUSTOM_LABEL_V1
                product_id=aid, panel_name=_pname, button_name=_bname)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@vpn_user_router.callback_query(F.data.startswith("vget:sub:"))
async def vget_sub(cb: CallbackQuery, bot: Bot):
    """ارسال ساب لینک + QR."""
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        sub_link, email = a.sub_link, a.email
        _sub_id = a.sub_id
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال آماده‌سازی…")
    # اگر ساب ذخیره‌شده نبود، از پنل بگیر
    if not sub_link and panel:
        try:
            client = _client_from_panel(panel)
            info = await client.get_client(email)
            sub_link = (info or {}).get("sub_link") or ""
            if sub_link:
                # PATCH_PANEL_RAW_USERNAME_V1 — سرویسِ «افزودنِ دستی» که قبلاً بی‌ساب‌لینک ثبت شده بود
                # حالا یک‌بار از پنل گرفته و ذخیره می‌شود
                async with Session() as _s1:
                    _a1 = await _s1.get(PanelAccount, aid)
                    if _a1 and not _a1.sub_link:
                        _a1.sub_link = sub_link
                        await _s1.commit()
            # === PATCH_SUB_BASE_DISCOVER_V1 === آدرسِ ساب در تنظیماتِ پنل خالی بود → از خودِ پنل پیدا کن
            if not sub_link and hasattr(client, "discover_sub_base"):
                _sid = _sub_id or (info or {}).get("subId") or ""
                _base = await client.discover_sub_base()
                if _base and _sid:
                    sub_link = f"{_base}/{_sid}"
                    async with Session() as _s2:
                        _a2 = await _s2.get(PanelAccount, aid)
                        if _a2 and not _a2.sub_link:
                            _a2.sub_link = sub_link
                            if not _a2.sub_id:
                                _a2.sub_id = _sid
                            await _s2.commit()
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not sub_link:
        await cb.message.answer("⛔️ این سرویس ساب لینک ندارد. از «کانفیگ‌های تکی» استفاده کن.")
        return
    # === PATCH_WG_SUB_NO_HINT_V1 ===
    # ادمین: برایِ وایرگارد توضیحِ «در کلاینت v2rayNG/نکوباکس… اضافه کن» بی‌معنی است
    # (وایرگارد یا با لینک یا با فایل ایمپورت می‌شود). اگر اکانت فایلِ وایرگارد دارد، فقط
    # خودِ لینک با عنوانِ «لینک ساب اشتراک» فرستاده می‌شود. هر خطایی → متنِ قبلی.
    _is_wg_acc = False
    if panel:
        try:
            _c_wg = _client_from_panel(panel)
            if hasattr(_c_wg, "get_wireguard_endpoints"):
                _is_wg_acc = bool(await _c_wg.get_wireguard_endpoints(email))
        except Exception:
            logger.exception("تشخیصِ وایرگارد بودنِ اکانت برایِ متنِ ساب ناموفق بود؛ متنِ عادی")
    if _is_wg_acc:
        body = f"🔗 <b>لینک ساب اشتراک</b>\n<code>{sub_link}</code>"
    else:
        body = (f"🔗 <b>لینک اشتراک (Subscription):</b>\n<code>{sub_link}</code>\n\n"
                "این لینک را در کلاینت (v2rayNG، نکوباکس، استریسند و…) اضافه کن.")
    try:
        png = await _qr_png(row.id, sub_link)
        await bot.send_photo(cb.from_user.id, BufferedInputFile(png, filename="sub_qr.png"),
                             caption=body)
    except Exception:
        await bot.send_message(cb.from_user.id, body)


# === PATCH_WG_FILENAME_ASCII_V1 ===
# وایرگارد نامِ فایل (= نامِ تونل) را فقط با [A-Za-z0-9_=+.-] و حداکثر ۱۵ کاراکتر می‌پذیرد.
# نامِ لوکیشن در پنل گاهی ایموجی/فاصلهٔ فارسی دارد («udvkfuci-🇩🇪 -DE.conf») و ایمپورت خطا می‌داد.
# این تابع آخرین خطِ دفاعی برایِ همهٔ پنل‌هاست؛ نامِ سالم دست‌نخورده می‌ماند.
def _wg_safe_file_name(fname: str, fallback: str, used: set) -> str:
    stem = re.sub(r"\.conf$", "", str(fname or ""), flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9_=+.\-]", "", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip("-.")
    stem = (stem[:15].rstrip("-.")) or fallback[:15]
    base, i = stem, 1
    while stem.lower() in used:
        i += 1
        stem = base[:15 - len(str(i))] + str(i)
    used.add(stem.lower())
    return stem + ".conf"


# === PATCH_WG_SUB_DIRECT_V1 ===
# درخواستِ WhaleSupport: «مستقیم از ساب دانلود کنه، بدونِ هیچ تغییری حتی اسمِ فایل». مشتری‌ها می‌گفتند فایلِ
# رباتِ دیگر/ساب خوب کار می‌کند ولی فایلِ ربات اختلال/پینگِ بالا دارد؛ پس خودِ پاسخِ ساب‌لینک عیناً (بایت‌به‌بایت)
# و با همان اسمِ فایلِ ساب (Content-Disposition) فرستاده می‌شود. اگر ساب وایرگارد نداد، مسیرهایِ قبلی بی‌تغییر.
def wg_files_from_sub_response(body: bytes, disp_name: str | None) -> list[tuple[str, bytes]]:
    """بدنهٔ پاسخِ ساب → [(نامِ فایل، بایت‌هایِ خام)]. فقط وقتی «[Interface]» داشته باشد؛ وگرنه []."""
    from tenant.shared.panel_clients.sub_fetch import split_wg_blob
    import base64 as _b64
    text = (body or b"").decode("utf-8", "ignore")
    if "[Interface]" not in text:
        try:      # بعضی ساب‌ها کلِ فایل را base64 می‌دهند
            dec = _b64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4)).decode("utf-8", "ignore")
            if "[Interface]" in dec:
                body, text = dec.encode("utf-8"), dec
        except Exception:  # noqa: BLE001
            logger.debug("بدنهٔ ساب base64 معتبر نبود (طبیعی است اگر متنِ خام باشد)")
    if "[Interface]" not in text:
        return []
    parts = split_wg_blob(text)
    if len(parts) <= 1:
        name = (disp_name or "").strip() or "wireguard.conf"
        if not name.lower().endswith(".conf"):
            name += ".conf"
        return [(name.replace("/", "_").replace("\\", "_"), body)]        # تک‌فایل: بایت‌هایِ اصلی، دست‌نخورده
    out = []
    for i, part in enumerate(parts, 1):
        m = _re.search(r"(?im)^\s*#\s*name\s*:\s*(.+)$", part)
        nm = (m.group(1).strip() if m else "") or f"wireguard_{i}"
        out.append((nm.replace("/", "_") + ".conf", part.encode("utf-8")))
    return out


async def _fetch_wg_files_from_sub(sub_link: str) -> list[tuple[str, bytes]]:
    if not (sub_link or "").startswith(("http://", "https://")):
        return []
    import aiohttp as _aio
    for ua in ("WireGuard/1.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"):
        try:
            async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=10),
                                          headers={"User-Agent": ua, "Accept": "*/*"}) as sess:
                async with sess.get(sub_link, allow_redirects=True) as r:
                    if r.status != 200:
                        continue
                    body = await r.read()
                    disp = None
                    try:
                        disp = r.content_disposition.filename if r.content_disposition else None
                    except Exception:  # noqa: BLE001
                        disp = None
        except Exception:  # noqa: BLE001
            logger.info("دریافتِ مستقیمِ وایرگارد از ساب ممکن نشد (UA=%s)", ua)
            continue
        files = wg_files_from_sub_response(body, disp)
        if files:
            return files
    return []


async def _send_wg_file_list(bot, chat_id: int, files: list[tuple[str, bytes]]) -> int:
    sent = 0
    for i, (name, data) in enumerate(files, 1):
        try:
            await bot.send_document(
                chat_id, BufferedInputFile(data, filename=name),
                caption=f"🛡 <b>کانفیگِ WireGuard {i} از {len(files)}</b>\n\n"
                        "این فایل مستقیم از ساب‌لینکِ شما گرفته شده و هیچ تغییری نکرده — ایمپورتش کن.",
                parse_mode="HTML")
            sent += 1
        except Exception:
            logger.exception("ارسالِ فایلِ وایرگاردِ ساب ناموفق بود")
    return sent


async def _send_sub_wg_files(bot, chat_id: int, sub_link: str, panel=None, email: str = "") -> int:
    """=== PATCH_WG_SUB_MULTI_FIX_V1 === (تیکت #857) ساب‌لینکِ بعضی پنل‌ها فقط *یک* لوکیشنِ وایرگارد می‌دهد
    در حالی که پنل چند لوکیشن دارد؛ قبلاً همین یک فایل مسیرِ بومیِ چندفایلی را می‌پرید و مشتری فقط یکی می‌گرفت.
    حالا اگر ساب بیش از یک فایل داد همان؛ وگرنه اگر مسیرِ بومیِ پنل لوکیشن‌هایِ بیشتری دارد آن‌ها فرستاده می‌شود،
    و فقط در نبودنِ لوکیشنِ بیشتر همان تک‌فایلِ ساب."""
    files = await _fetch_wg_files_from_sub(sub_link)
    if len(files) > 1:
        return await _send_wg_file_list(bot, chat_id, files)
    if panel is not None and email:
        client = None
        try:
            client = _client_from_panel(panel)
            if hasattr(client, "get_wireguard_endpoints"):
                endpoints = await client.get_wireguard_endpoints(email)
                if endpoints and len(endpoints) > max(1, len(files)):
                    sent = await _send_native_wg_files(bot, chat_id, client, email)
                    if sent:
                        return sent
        except Exception:
            logger.exception("بررسیِ لوکیشن‌هایِ بومیِ وایرگارد برایِ تکمیلِ فایل‌هایِ ساب ناموفق بود")
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    logger.exception("بستنِ کلاینتِ پنل ناموفق بود")
    return await _send_wg_file_list(bot, chat_id, files) if files else 0


# === PATCH_WG_NATIVE_IN_SINGLE_V1 ===
# گزارشِ ادمین: دکمهٔ «ارسال کانفیگِ تکی» برایِ اینباندِ وایرگارد فایل نمی‌فرستاد
# (یا با اسمِ ساختگی «wireguard.conf / wg1-…» می‌فرستاد)، در حالی که مسیرِ بومیِ پنل
# (get_wireguard_endpoints) فایل را با *همان اسمی که در پنل هست* می‌دهد. این مسیر فقط
# در دکمهٔ جداگانهٔ «🛡 دریافت کانفیگ‌هایِ وایرگارد» (vwgcfg) وصل بود، نه اینجا.
# این تابع همان منطق را برایِ «کانفیگِ تکی» هم اجرا می‌کند و تعدادِ فایل‌هایِ
# ارسال‌شده را برمی‌گرداند (۰ = پنل بومی ندارد → مسیرِ قدیمی بی‌تغییر ادامه پیدا می‌کند).
def _is_wg_link(x: str) -> bool:
    x = (x or "").lstrip()
    return ("[Interface]" in x) or x.startswith("wireguard://")


async def _send_native_wg_files(bot, chat_id: int, client, email: str) -> int:
    if not hasattr(client, "get_wireguard_endpoints"):
        return 0
    try:
        endpoints = await client.get_wireguard_endpoints(email)
    except Exception:
        logger.exception("گرفتنِ لیستِ لوکیشن‌هایِ وایرگاردِ بومی ناموفق بود؛ مسیرِ قدیمی")
        return 0
    if not endpoints:
        return 0
    sent = 0
    _used_fn: set = set()
    for ep in endpoints:
        fname = _wg_safe_file_name(ep.get("file"), f"wg{ep.get('id', sent + 1)}", _used_fn)
        try:
            cfg_text = await client.fetch_wireguard_config_text(
                email, ep.get("id"), path=ep.get("config_url"))
        except Exception:
            logger.exception("دریافتِ متنِ کانفیگِ وایرگارد (host=%r) ناموفق بود", ep.get("id"))
            continue
        if not cfg_text:
            continue
        name = ep.get("name") or ""
        _label_line = f"\n🌍 <code>{html.escape(name)}</code>" if name else ""
        caption = (f"🛡 <b>کانفیگِ WireGuard {sent + 1} از {len(endpoints)}</b>{_label_line}\n\n"
                   "این فایل دقیقاً همان چیزی است که پنل داده — بدونِ تغییر ایمپورتش کن.")
        try:
            await bot.send_document(
                chat_id, BufferedInputFile(cfg_text.encode("utf-8"), filename=fname),
                caption=caption, parse_mode="HTML")
            sent += 1
        except Exception:
            logger.exception("ارسالِ فایلِ کانفیگِ وایرگاردِ بومی ناموفق بود")
    return sent


_MISSING_ALERT_TS: dict = {}   # (bot_id, account_id) → زمانِ آخرین اعلان به مالک


@vpn_user_router.callback_query(F.data.startswith("vget:single:"))
async def vget_single(cb: CallbackQuery, bot: Bot):
    """ارسال کانفیگ‌های تکی (لینک‌های مستقیم هر inbound)."""
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        email = a.email
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال دریافت کانفیگ‌ها…")
    # === PATCH_SINGLE_CONFIG_DIAGNOSTIC_V1 ===
    # گزارشِ کاربر: «کانفیگ‌های تکی» همیشه پیامِ «در دسترس نیست» می‌داد.
    # علتش قابلِ تشخیص نبود، چون `except Exception: links = []` هر خطایی را
    # بی‌صدا می‌بلعید و همه‌چیز شبیهِ «پنل این قابلیت را ندارد» به‌نظر
    # می‌رسید — درحالی‌که ممکن است پنل در دسترس نباشد، اکانت روی پنل پاک
    # شده باشد، یا نامِ کاربری فرق کند.
    links = []
    err = None
    native_sent = 0  # === PATCH_WG_NATIVE_IN_SINGLE_V1 ===
    if panel:
        try:
            client = _client_from_panel(panel)
            try:
                native_sent = await _send_native_wg_files(bot, cb.from_user.id, client, email)
                links = await client.get_client_links(email)
                if native_sent:
                    # وایرگارد قبلاً با اسمِ اصلیِ پنل فرستاده شد؛ تکراری نفرست
                    links = [l for l in links if not _is_wg_link(l)]
            finally:
                await client.close()
        except Exception as e:
            err = e
            logger.exception("کانفیگِ تکی برایِ %r از پنلِ %s گرفته نشد",
                             email, getattr(panel, "panel_key", "?"))
        else:
            # === PATCH_SUB_FETCH_UA_V1 ===
            # لیستِ خالی *بدونِ* استثنا هم شکست است — یعنی پنل جواب داد ولی
            # هیچ کانفیگی نداشت. قبلاً این حالت با «پنل پشتیبانی نمی‌کند»
            # یکی گرفته می‌شد و به مالک هم خبر نمی‌رفت.
            if not links and not native_sent:
                err = "پنل پاسخ داد ولی هیچ کانفیگی برنگرداند"
                logger.warning("کانفیگِ تکی خالی بود | پنل=%s | سرویس=%r",
                               getattr(panel, "panel_key", "?"), email)
    else:
        err = "پنلی به این سرویس وصل نیست"
    if not links and native_sent:
        return  # فقط وایرگارد داشت و همه فایل‌ها فرستاده شد
    if not links:
        # === PATCH_SVC_MISSING_ON_PANEL_V1 ===
        # پنل جواب داد ولی کانفیگی نبود: اگر خودِ کاربر روی پنل وجود ندارد (۴۰۴؛ مثلاً از پنل حذف شده)
        # ساب‌لینک هم کار نمی‌کند — پس نباید مشتری را به «ارسال ساب لینک» حواله داد.
        _missing = False
        if panel and isinstance(err, str) and err.startswith("پنل پاسخ داد"):
            try:
                _missing = (await _client_from_panel(panel).get_client(email)) is None
            except Exception:
                logger.exception("بررسیِ وجودِ کاربر روی پنل ناموفق بود (%r)", email)
            if _missing:
                err = "کاربر روی پنل وجود ندارد (۴۰۴) — احتمالاً از خودِ پنل حذف شده"
        _hint = ""
        if err is not None:
            _hint = ("\n\n<i>اگر ادامه داشت به پشتیبانی بگو؛ جزئیاتِ فنی در "
                     "لاگِ ربات ثبت شد.</i>")
            # به مالکِ ربات خبر بده — این خطا معمولاً یعنی مشکلی در پنل هست
            import time as _tm
            _k = (row.id, aid)
            if _tm.monotonic() - _MISSING_ALERT_TS.get(_k, -1e9) > 6 * 3600:
                _MISSING_ALERT_TS[_k] = _tm.monotonic()
                try:
                    async with Session() as s:
                        _b = await s.get(BotRow, row.id)
                    if _b and _b.owner_id:
                        await bot.send_message(
                            _b.owner_id,
                            f"⚠️ «کانفیگِ تکی» برایِ سرویسِ <code>{html.escape(str(email))}</code> "
                            f"(کاربر <code>{cb.from_user.id}</code>) کار نکرد.\n"
                            f"علت: <code>{html.escape(str(err)[:200])}</code>"
                            + ("\n\nاین سرویس در ربات «فعال» است ولی روی پنل نیست؛ سرویسِ جایگزین "
                               "بساز یا برگشت وجه بده." if _missing else ""))
                except Exception:
                    logger.exception("اطلاعِ خطایِ کانفیگِ تکی به مالک نرسید")
        if _missing:
            await cb.message.answer(
                "⛔️ این سرویس روی سرور پیدا نشد (احتمالاً از پنل حذف شده است).\n"
                "لطفاً با پشتیبانی تماس بگیر تا برایت بررسی یا جایگزین کنند." + _hint, parse_mode="HTML")
            return
        await cb.message.answer(
            "⛔️ کانفیگِ تکی برایِ این سرویس در دسترس نیست؛ از «🔗 ارسال ساب لینک» "
            "استفاده کن — همان کانفیگ‌ها را دارد." + _hint, parse_mode="HTML")
        return
    # هر کانفیگ را جدا با QR بفرست
    _cards: list = []
    for i, link in enumerate(links, 1):
        # === PATCH_WIREGUARD_CONF_FILE_V1 ===
        # گزارشِ ادمین: برایِ اینباندِ WireGuard، «لینک» در واقع یه فایلِ
        # کاملِ کانفیگِ INI (بخش‌هایِ [Interface]/[Peer]) است، نه یه رشتهٔ
        # کوتاهِ URI-مانند. نه QR برایِ همچین متنِ بلندی معنی داره، نه
        # فرستادنش داخلِ <code> قابلِ استفاده‌ست — کلاینت‌هایِ وایرگارد
        # همیشه با ایمپورتِ فایلِ .conf کار می‌کنن، نه .txt.
        _wg_norm = _extract_wireguard_configs([link])
        if _wg_norm:
            link = _wg_norm[0]
        if _WG_INI_RE.search(link or ""):
            # === PATCH_WG_ENDPOINT_LABEL_V1 ===
            # گزارشِ ادمین: مشتری از رویِ «wireguard_1.conf، wireguard_2.conf، …»
            # نمی‌فهمید کدوم فایل مالِ کدوم لوکیشنه. حالا هاستِ Endpoint
            # (که همون هاستِ اختصاصیِ همون سرور/کشوره) هم در نامِ فایل و
            # هم در کپشن می‌آید.
            # === PATCH_WG_SHORT_FILENAME_V1 ===
            # گزارشِ ادمین: اسمِ فایلِ قبلی (با کلِ دامنه) بلندتر از حدِ
            # مجازِ نامِ اینترفیسِ وایرگارد بود و اپ ارور می‌داد؛ الان کوتاه
            # و حداکثر ۱۵ کاراکتره.
            # === MARKER_WG_FRIENDLY_NAME_V1 ===
            _wg_label = _wg_conf_name(link) or _wg_endpoint_label(link)
            fname = (f"{_wg_short_filename(i, link)}.conf" if len(links) > 1
                     else "wireguard.conf")
            _label_line = f"\n🌍 <code>{html.escape(_wg_label)}</code>" if _wg_label else ""
            caption = (f"📦 <b>کانفیگِ WireGuard {i} از {len(links)}</b>{_label_line}\n\n"
                       "این فایل رو مستقیم توی اپِ WireGuard ایمپورت کن.")
            try:
                await bot.send_document(
                    cb.from_user.id,
                    BufferedInputFile(link.encode("utf-8"), filename=fname),
                    caption=caption, parse_mode="HTML")
            except Exception:
                logger.exception("ارسالِ فایلِ کانفیگِ وایرگارد ناموفق بود")
                await bot.send_message(cb.from_user.id, f"⚠️ ارسالِ فایلِ کانفیگ ناموفق بود:\n<code>{link}</code>", parse_mode="HTML")
            continue
        body = f"📦 <b>کانفیگ {i} از {len(links)}</b>\n\n<code>{link}</code>"
        _cards.append((i, link, body))
    # === PATCH_SINGLE_CONFIG_ALBUM_V1 ===
    # گزارشِ کندی: «کانفیگ‌هایِ تکی» تا ۲۸ ثانیه طول می‌کشید، چون برایِ هر لینک *پشتِ‌سرِ‌هم* اول QR ساخته و بعد یک
    # عکس جدا آپلود می‌شد. حالا QRها هم‌زمان ساخته می‌شوند و عکس‌ها در آلبوم‌هایِ ۱۰تایی (یک درخواست به‌جایِ ده‌تا)
    # فرستاده می‌شوند؛ ترتیب و متنِ هر کانفیگ دقیقاً مثلِ قبل است.
    if _cards:
        await _send_config_cards(bot, row.id, cb.from_user.id, _cards)


async def _send_config_cards(bot: Bot, bot_id: int, chat_id: int, cards: list) -> None:
    """cards: [(شماره, لینک, متنِ کپشن)]. QRها موازی ساخته می‌شوند؛ هر کارت (عکسِ QR + متنِ کانفیگ در کپشن)
    جدا و به‌ترتیب فرستاده می‌شود.

    ⚠️ آلبوم (send_media_group) عمداً حذف شد: تلگرام در آلبومی که چند کپشن دارد، هیچ کپشنی را زیرِ آلبوم
    نشان نمی‌دهد؛ مشتری فقط یک شبکه از QR می‌دید و متنِ کانفیگ پنهان می‌ماند (گزارشِ nitroteeam)."""
    # === PATCH_CONFIG_CARDS_PIPELINE_V1 ===
    # قبلاً همهٔ QRها هم‌زمان ساخته می‌شدند (CPUِ سنگین در thread ⇒ لوپ می‌گرفت) و فقط بعد از تمامِ همه ارسال شروع
    # می‌شد. حالا حداکثر ۳ QR هم‌زمان؛ و کارتِ اول همین‌که آماده شد ارسال می‌شود (ساخت و ارسال هم‌پوشانی دارند).
    _sem = asyncio.Semaphore(3)

    async def _one_qr(link):
        async with _sem:
            return await _qr_png(bot_id, link)

    _tasks = [asyncio.ensure_future(_one_qr(c[1])) for c in cards]
    try:
        await _send_config_cards_loop(bot, chat_id, cards, _tasks)
    finally:
        for t in _tasks:
            if not t.done():
                t.cancel()


async def _send_config_cards_loop(bot: Bot, chat_id: int, cards: list, tasks: list) -> None:
    for (i, _lnk, body), task in zip(cards, tasks):
        try:
            png = await task
        except Exception as e:  # noqa: BLE001
            png = e
        ok = isinstance(png, (bytes, bytearray)) and bool(png) and len(body) <= 1000
        try:
            if ok:
                await bot.send_photo(chat_id, BufferedInputFile(bytes(png), filename=f"config_{i}.png"), caption=body)
                continue
        except Exception:
            logger.exception("ارسالِ عکسِ کانفیگ %s ناموفق بود", i)
        await bot.send_message(chat_id, body)


async def _send_wg_texts(bot: Bot, chat_id: int, wg_configs: list) -> int:
    """هر متنِ .conf را به‌صورتِ فایل می‌فرستد (نام/کپشنِ لوکیشن‌دار). تعدادِ فایل‌هایِ فرستاده‌شده را برمی‌گرداند."""
    sent = 0
    for i, cfg_text in enumerate(wg_configs, 1):
        # === PATCH_WG_ENDPOINT_LABEL_V1 / PATCH_WG_SHORT_FILENAME_V1 ===
        # === MARKER_WG_FRIENDLY_NAME_V1 ===
        _wg_label = _wg_conf_name(cfg_text) or _wg_endpoint_label(cfg_text)
        fname = (f"{_wg_short_filename(i, cfg_text)}.conf" if len(wg_configs) > 1
                 else "wireguard.conf")
        _label_line = f"\n🌍 <code>{html.escape(_wg_label)}</code>" if _wg_label else ""
        caption = (f"🛡 <b>کانفیگِ WireGuard {i} از {len(wg_configs)}</b>{_label_line}\n\n"
                   "این فایل را مستقیم توی اپِ WireGuard ایمپورت کن.")
        try:
            await bot.send_document(
                chat_id,
                BufferedInputFile(cfg_text.encode("utf-8"), filename=fname),
                caption=caption, parse_mode="HTML")
            sent += 1
        except Exception:
            logger.exception("ارسالِ فایلِ کانفیگِ وایرگارد ناموفق بود")
            await bot.send_message(chat_id, "⚠️ ارسالِ فایلِ کانفیگ ناموفق بود.")
    return sent


async def _send_wg_files_for_account(bot: Bot, chat_id: int, aid: int) -> int:
    """=== PATCH_WG_FILES_FIRST_V1 === فایل‌هایِ وایرگاردِ یک سرویس را (مثلِ دکمهٔ «ارسال کانفیگ تکی وایرگارد») می‌فرستد.
    اول مسیرِ بومیِ پنل (نامِ فایلِ درست)، وگرنه ساخت از لینک‌هایِ ساب. خطا هرگز بالا نمی‌رود."""
    try:
        async with Session() as s:
            a = await s.get(PanelAccount, aid)
            panel = await _panel_for_account(s, a.bot_id, a) if a else None
            if not a or not panel:
                return 0
            email, sub_link = a.email, a.sub_link or ""
        direct = await _send_sub_wg_files(bot, chat_id, sub_link, panel, email)      # PATCH_WG_SUB_DIRECT_V1
        if direct:
            return direct
        client = _client_from_panel(panel)
        try:
            native = await _send_native_wg_files(bot, chat_id, client, email)
            if native:
                return native
            links = await asyncio.wait_for(_links_with_sub_fallback(client, email, sub_link), timeout=15)
        finally:
            await client.close()
        wg_configs = await _extract_wireguard_configs_async(links)
        return await _send_wg_texts(bot, chat_id, wg_configs) if wg_configs else 0
    except Exception:
        logger.exception("ارسالِ خودکارِ فایل‌هایِ وایرگارد بعد از تحویل ناموفق بود (aid=%s)", aid)
        return 0


@vpn_user_router.callback_query(F.data.startswith("vwgcfg:"))
async def vpn_service_wg_single(cb: CallbackQuery, bot: Bot):
    """ارسالِ کانفیگِ تکیِ وایرگارد به‌صورتِ فایلِ .conf — بدونِ QR، چون
    وایرگارد نیازی به QR ندارد و رندرش فقط ارسال را کند می‌کرد."""
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        email = a.email
        _acc_sub_link = a.sub_link or ""  # PATCH_WG_SUB_FALLBACK_V1
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال دریافت کانفیگِ وایرگارد…")
    if not panel:
        await cb.message.answer("⛔️ پنلی به این سرویس وصل نیست.")
        return
    if await _send_sub_wg_files(bot, cb.from_user.id, _acc_sub_link, panel, email):     # PATCH_WG_SUB_DIRECT_V1
        return
    try:
        client = _client_from_panel(panel)
        try:
            # === PATCH_WIREGUARD_NATIVE_DELIVERY_V1 ===
            # اول مسیرِ بومیِ پنل را امتحان کن — این هم نامِ فایلِ صحیح را
            # از خودِ پنل می‌دهد (نه ساختگی)، هم همهٔ لوکیشن‌ها را (نه فقط
            # اولی)، هم متنِ کانفیگ را بدونِ هیچ دست‌کاری. اگر این پنل چنین
            # endpointای نداشت (لیستِ خالی)، بی‌صدا به همان مسیرِ قدیمی
            # برمی‌گردیم — رفتارِ فعلی برایِ پنل‌هایِ دیگر عوض نمی‌شود.
            endpoints = []
            if hasattr(client, "get_wireguard_endpoints"):
                try:
                    endpoints = await client.get_wireguard_endpoints(email)
                except Exception:
                    logger.exception("گرفتنِ لیستِ لوکیشن‌هایِ وایرگاردِ بومی ناموفق بود؛ به مسیرِ قدیمی برمی‌گردیم")
                    endpoints = []
            if endpoints:
                await cb.message.answer(f"📥 در حالِ دریافتِ {len(endpoints)} فایلِ کانفیگ…")
                sent = 0
                _used_fn: set = set()
                for ep in endpoints:
                    fname = _wg_safe_file_name(ep.get("file"), f"wg{ep.get('id', sent + 1)}", _used_fn)
                    host_id = ep.get("id")
                    cfg_path = ep.get("config_url")
                    try:
                        cfg_text = await client.fetch_wireguard_config_text(email, host_id, path=cfg_path)
                    except Exception:
                        logger.exception("دریافتِ متنِ کانفیگِ وایرگارد (host=%r) ناموفق بود", host_id)
                        continue
                    if not cfg_text:
                        continue
                    name = ep.get("name") or ""
                    _label_line = f"\n🌍 <code>{html.escape(name)}</code>" if name else ""
                    caption = (f"🛡 <b>کانفیگِ WireGuard {sent + 1} از {len(endpoints)}</b>{_label_line}\n\n"
                               "این فایل دقیقاً همان چیزی است که پنل داده — بدونِ تغییر ایمپورتش کن.")
                    try:
                        await bot.send_document(
                            cb.from_user.id,
                            BufferedInputFile(cfg_text.encode("utf-8"), filename=fname),
                            caption=caption, parse_mode="HTML")
                        sent += 1
                    except Exception:
                        logger.exception("ارسالِ فایلِ کانفیگِ وایرگاردِ بومی ناموفق بود")
                if sent:
                    return
                # همهٔ لوکیشن‌ها شکست خوردند؛ به مسیرِ قدیمی برو (پایینِ همین تابع)
            links = await _links_with_sub_fallback(client, email, _acc_sub_link)  # PATCH_WG_SUB_FALLBACK_V1
        finally:
            await client.close()
    except Exception:
        logger.exception("کانفیگِ وایرگارد برایِ %r گرفته نشد", email)
        await cb.message.answer("⛔️ گرفتنِ کانفیگِ وایرگارد از پنل ناموفق بود.")
        return
    wg_configs = await _extract_wireguard_configs_async(links)
    if not wg_configs:
        await cb.message.answer("این سرویس کانفیگِ وایرگارد ندارد.")
        return
    await _send_wg_texts(bot, cb.from_user.id, wg_configs)


async def _card_buy_allowed(cb, bot) -> bool:
    """گیتِ سمتِ سرور برای پرداختِ کارت‌به‌کارتِ خریدِ VPN.

    حتی اگر کاربر روی پیامِ قدیمی/کش‌شده‌ای دکمهٔ کارت‌به‌کارت را بزند، وقتی
    مالک کارت‌به‌کارت را (کلاً یا فقط در بخشِ «خرید») خاموش کرده باشد نباید
    مسیرِ پرداخت ادامه پیدا کند.
    """
    from tenant import get_setting
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return False
    if await get_setting(row.id, "card_enabled", "1") != "1" or \
       await get_setting(row.id, "card_purchase_enabled", "1") != "1":
        await cb.answer("❌ پرداخت کارت‌به‌کارت در حال حاضر غیرفعال است.", show_alert=True)
        return False
    # === PATCH_PAY_PHONE_GATE_CARD_BUY_V1 ===
    # «تاییدِ شماره پیش از پرداخت» برایِ خریدِ سرویس با کارت‌به‌کارت اجرا نمی‌شد
    # (فقط شارژِ کیف‌پول شماره می‌گرفت). این تابع اولِ هر دو مسیرِ کارتیِ VPN
    # (vpay:card و vc:cardpay) صدا زده می‌شود، پس همین‌جا گیت می‌شود.
    from tenant import pay_phone_gate
    if await pay_phone_gate(cb.message, row, cb.from_user.id):
        await cb.answer("📱 اول شماره‌ات را تایید کن.", show_alert=True)
        return False
    return True


# ─────────────── پرداخت کارت‌به‌کارت VPN ───────────────
async def _show_card_details(cb: CallbackQuery, bot: Bot, bot_id: int,
                              gb: int, days: int, count: int, price: float,
                              oid: int, panel_key: str = "",
                              state: FSMContext | None = None):
    """نمایش فاکتور حرفه‌ای + شماره کارت برای خرید VPN با کارت."""
    from tenant import get_setting, _attach_invoice, get_or_create_user
    # === PATCH_KYC_GATE_VPN_CARD_V1 ===
    # این مسیر (خریدِ مستقیمِ VPN با کارت) هم مثلِ card_purchase.py سقفِ
    # احرازِ هویت را چک نمی‌کرد. سفارش (Order) با pay_method="vpn_card" *قبل*
    # از این تابع با status="awaiting_receipt" ساخته شده (در vpn_pay_card_plan
    # و vc_pay_card) — پس اگر گیت رد شود، همینجا سفارش را rejected می‌کنیم
    # (نه اینکه معلق در awaiting_receipt بماند) و شماره‌کارت اصلاً نشان
    # داده نمی‌شود.
    try:
        from tenant.shared.kyc import check_charge
        _u = await get_or_create_user(bot_id, cb.from_user.id)
        _ok, _why = await check_charge(bot_id, _u, price)
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (vpn card, پیش از نمایشِ شماره‌کارت)")
        _ok, _why = True, ""
    if not _ok:
        from models import Order
        async with Session() as s:
            o = await s.get(Order, oid)
            if o and o.status == "awaiting_receipt":
                o.status = "rejected"
                await s.commit()
        kyc_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛡 احرازِ هویت", callback_data="kycu:menu", style=ButtonStyle.SUCCESS)]])
        try:
            await cb.message.edit_text(_why, reply_markup=kyc_kb)
        except Exception:
            await cb.message.answer(_why, reply_markup=kyc_kb)
        return
    card        = await get_setting(bot_id, "card_number", "(شماره کارت ثبت نشده)")
    card_holder = await get_setting(bot_id, "card_holder", "")
    # شماره فاکتور و کد پیگیری
    inv_no, trk = await _attach_invoice(oid, bot_id)
    vol  = "نامحدود" if gb == 0 else f"{gb} گیگ"
    dur  = "بی‌انقضا" if days == 0 else f"{days} روز"
    # یادداشت دلخواه ادمین برای این پنل (زیر قیمت در پیش‌فاکتور)
    note_line = ""
    if panel_key:
        try:
            from tenant.bots.vpn.vpn_admin import get_panel_note
            _note = await get_panel_note(bot_id, panel_key, "invoice")
            if _note:
                note_line = f"📝 {_note}\n"
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # اطلاعات کاربریِ خریدار
    _bn = cb.from_user.full_name or "—"
    _bu = cb.from_user.username
    buyer_line = f"👤 خریدار: {_bn}" + (f" (@{_bu})" if _bu else "")
    buyer_line += f"\n🆔 شناسه: <code>{cb.from_user.id}</code>"
    text = (
        "🧾 <b>پیش فاکتور</b>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{buyer_line}\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"📦 سرویس: <b>{vol} / {dur}</b>\n"
        f"🔢 تعداد: <b>{count} عدد</b>\n"
        f"💰 مبلغ قابل پرداخت: <b>{rial(price)}</b>\n"
        f"{note_line}"
        f"🔖 شماره فاکتور: <code>{inv_no}</code>\n"
        f"🆔 کد پیگیری: <code>{trk}</code>\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "💳 <b>اطلاعات واریز</b>\n\n"
        f"<code>{card}</code>\n"
        + (f"👤 به نام: <b>{card_holder}</b>\n" if card_holder else "")
        + await _card_both_extra(bot_id)
        + f"\n💵 مبلغ: <code>{int(price):,} تومان</code>\n\n"
        "⬆️ روی شماره کارت یا مبلغ بزن تا کپی شود.\n"
        "📸 پس از واریز، <b>متن یا عکس رسید</b> را اینجا ارسال کن."
    )
    # === PATCH_AUTO_APPROVE_VPN_CARD_V1 ===
    # وقتی تاییدِ خودکار روشن است، قبل از نشان‌دادنِ شماره‌کارت باید ۴ رقمِ
    # آخرِ کارتِ مبدأ را بپرسیم تا موتورِ تشخیص بتواند کراس‌چک کند — همان
    # کاری که مسیرِ خریدِ کانفیگ انجام می‌دهد. بدونِ این، رسیدها همیشه به
    # دلیلِ نبودِ شناسهٔ فرستنده به بررسیِ دستی ارجاع می‌شدند.
    if state is not None:
        try:
            from tenant.gateways.card_purchase import maybe_gate_card_invoice
            if await maybe_gate_card_invoice(cb, state, bot_id, oid, text,
                                             f"vpaycard:cancel:{oid}"):
                return
        except Exception:
            logger.exception("auto-approve VPN: گیتِ ۴ رقمِ کارتِ مبدأ ناموفق بود؛ پیش‌فاکتور مستقیم نشان داده شد.")
    try:
        await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data=f"vpaycard:cancel:{oid}")]]))
    except Exception:
        await cb.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو", callback_data=f"vpaycard:cancel:{oid}")]]))


@vpn_user_router.callback_query(F.data.startswith("vpay:card:"))
async def vpn_pay_card_plan(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """شروع پرداخت کارت‌به‌کارت برای پلن VPN."""
    if not await _card_buy_allowed(cb, bot):
        return
    plan_id = int(cb.data.split(":")[2])
    _data = await state.get_data()
    _clabel = (_data.get("vcustom_label") or "").strip()[:60] or None
    async with Session() as s:
        row  = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        plan = await s.get(VpnPlan, plan_id)
        if (not plan or plan.bot_id != row.id or not plan.active
                or not await _plan_allowed_for(plan, row.id, cb.from_user.id)):
            await cb.answer("پلن در دسترس نیست.", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u:
            await cb.answer()
            return
        price = float(plan.price)
        # === PATCH_VPN_CARD_DISCOUNT_V1 ===
        # باگِ گزارش‌شده: تویِ پیش‌فاکتور (پرداختِ کیف‌پول) کدِ تخفیف درست
        # اعمال می‌شد، ولی همینجا (شروعِ پرداختِ کارت‌به‌کارت) اصلاً کدِ
        # تخفیف چک نمی‌شد — قیمتِ کارتی همیشه قیمتِ خامِ پلن بود، یعنی
        # مشتری قیمتِ تخفیف‌خورده رو تو پیش‌فاکتور می‌دید ولی وقتی «کارت به
        # کارت» رو می‌زد، مبلغِ کامل (بدونِ تخفیف) نشونش داده می‌شد. الان
        # دقیقاً همون منطقِ پرداختِ کیف‌پول (vpn_pay/_vpn_pay_impl) اینجا هم
        # تکرار شده: کد از state خونده و دوباره اعتبارسنجی می‌شه.
        stored_code = _data.get("vdisc_code") if _data.get("vdisc_plan_id") == plan_id else None
        applied_code = None
        if stored_code:
            res = await validate_discount(
                bot_id=row.id, code=stored_code, telegram_id=cb.from_user.id,
                price=int(price), panel_key=plan.panel_key, plan_id=plan_id,
                button_id=_data.get("vbuy_mb_id"), session=s)
            if res.ok and res.discount > 0:
                price = float(res.final_price)
                applied_code = res.code
        # ثبت سفارش در وضعیت awaiting_receipt
        from models import Order
        _pk = plan.panel_key
        _vol_gb = plan.volume_gb
        _days = plan.days
        o = Order(bot_id=row.id, user_id=u.id,
                  volume_gb=_vol_gb, days=_days, count=1,
                  amount=price, pay_method="vpn_card", status="awaiting_receipt",
                  panel_key=_pk, custom_label=_clabel,
                  discount_code=applied_code,
                  plan_id=plan_id,  # === PATCH_ORDER_PLAN_ID_V1 ===
                  button_id=_data.get("vbuy_mb_id"))  # === PATCH_ORDER_BUTTON_ID_V1 ===
        s.add(o)
        await s.commit()
        oid = o.id
        bot_id = row.id
    await state.update_data(vdisc_code=None, vdisc_plan_id=None)
    await cb.answer()
    await _show_card_details(cb, bot, bot_id, _vol_gb, _days, 1, price, oid, _pk, state=state)  # PATCH_AUTO_APPROVE_VPN_CARD_V1


@vpn_user_router.callback_query(F.data.startswith("vc:cardpay:"))
async def vc_pay_card(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """شروع پرداخت کارت‌به‌کارت برای VPN آنلاین (custom)."""
    if not await _card_buy_allowed(cb, bot):
        return
    parts = cb.data.split(":")
    gb, days, count = int(parts[2]), int(parts[3]), int(parts[4])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        await _vc_recover_panel_key(state, row.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
        _state_data = await state.get_data()
        _pkey = _state_data.get("vc_panel_key", "")
        _clabel = (_state_data.get("vcustom_label") or "").strip()[:60] or None

        # === PATCH_VC_CARDPAY_RANGE_GUARD_V1 ===
        # همون باگِ گزارش‌شده که تویِ پرداختِ کیف‌پول (_vc_pay_impl) با
        # PATCH_VC_PAY_RANGE_GUARD_V1 بسته شده بود، اینجا (پرداختِ
        # کارت‌به‌کارتِ خریدِ دلخواهِ VPN) اصلاً بسته نشده بود: gb/days/count
        # مستقیم از callback_data خونده می‌شدن (که با لغوِ عملیات و زدنِ یک
        # دکمهٔ کارت‌به‌کارتِ قدیمی/دستکاری‌شده هنوز قابلِ ارسال بودن) و هیچ‌جا
        # با بازهٔ مجازِ همون پنل (min/max/step) یا با panel_key خالی تطبیق
        # داده نمی‌شدن. نتیجه: قیمت از فرمولِ عمومی/اشتباه حساب می‌شد و
        # مشتری می‌تونست حجمِ خیلی بیشتری از چیزی که واقعاً پرداخت کرده رو
        # سفارش بده. الان دقیقاً همون اعتبارسنجیِ سمتِ سرورِ مسیرِ کیف‌پول
        # اینجا هم تکرار شده.
        if not _pkey:
            logger.warning(
                "[VC_CARDPAY] panel_key خالی بود؛ خرید رد شد | user=%s data=%s",
                cb.from_user.id, cb.data)
            await cb.answer(
                "نشستِ خرید منقضی شده. لطفاً دوباره از منو حجم و مدت را "
                "انتخاب کن.", show_alert=True)
            return

        cfg   = await _vc_all_for_user(row.id, cb.from_user.id, _pkey,
                                       days=days, gb=gb)  # PATCH_VPN_TIER_GB_BRACKETS_V1
        pg    = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])

        try:
            _mn_gb, _mx_gb = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
            _mn_d,  _mx_d  = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
            _st_gb = max(1, int(cfg.get("vc_step_gb", 1) or 1))
            _st_d  = max(1, int(cfg.get("vc_step_days", 1) or 1))
        except Exception:
            # fail-closed (مثلِ مسیرِ کیف‌پول) — رد کردنِ اعتبارسنجی یعنی دستکاریِ callback
            logger.exception("[VC_CARDPAY] خواندنِ بازهٔ مجاز ناموفق بود؛ خرید رد شد")
            await cb.answer("تنظیماتِ فروشِ این پنل ناقص است؛ به پشتیبانی خبر بده.", show_alert=True)
            return

        if _mn_gb is not None:
            _bad = None
            if not (_mn_gb <= gb <= _mx_gb):
                _bad = f"حجم باید بین {_mn_gb} تا {_mx_gb} گیگ باشد."
            elif not (_mn_d <= days <= _mx_d):
                _bad = f"مدت باید بین {_mn_d} تا {_mx_d} روز باشد."
            elif (gb - _mn_gb) % _st_gb:
                _bad = f"حجم باید مضربی از {_st_gb} گیگ باشد."
            elif (days - _mn_d) % _st_d:
                _bad = f"مدت باید مضربی از {_st_d} روز باشد."
            elif not (1 <= count <= 20):
                _bad = "تعدادِ نامعتبر."
            if _bad:
                logger.warning(
                    "[VC_CARDPAY] درخواستِ خارج از بازه رد شد | user=%s data=%s "
                    "gb=%s days=%s count=%s | مجاز: gb %s-%s، days %s-%s",
                    cb.from_user.id, cb.data, gb, days, count,
                    _mn_gb, _mx_gb, _mn_d, _mx_d)
                await cb.answer("درخواستِ نامعتبر: " + _bad, show_alert=True)
                return

        price = float((_vc_gb_cost(cfg, gb) + days * pd) * count)  # PATCH_VPN_TIER_CUMULATIVE_V1
        # === PATCH_VPN_CARD_DISCOUNT_V1 ===
        # همون باگِ vpn_pay_card_plan، اینجا هم بود: پیش‌فاکتور (_show_vc_
        # preinvoice) کدِ تخفیف رو درست حساب می‌کرد، ولی همین‌جا (شروعِ
        # پرداختِ کارتی) اصلاً چک نمی‌شد — دقیقاً منطقِ _vc_pay_impl
        # (پرداختِ کیف‌پول) تکرار شده.
        stored_code = _state_data.get("vc_disc_code")
        applied_code = None
        if stored_code:
            res = await validate_discount(
                bot_id=row.id, code=stored_code, telegram_id=cb.from_user.id,
                price=int(price), panel_key=_pkey or None, plan_id=None,
                button_id=_state_data.get("vbuy_mb_id"), session=s)
            if res.ok and res.discount > 0:
                price = float(res.final_price)
                applied_code = res.code
        u     = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u:
            await cb.answer()
            return
        from models import Order
        o = Order(bot_id=row.id, user_id=u.id,
                  volume_gb=gb, days=days, count=count,
                  amount=price, pay_method="vpn_card", status="awaiting_receipt",
                  panel_key=_pkey or None, custom_label=_clabel,
                  discount_code=applied_code,
                  button_id=_state_data.get("vbuy_mb_id"))  # === PATCH_ORDER_BUTTON_ID_V1 ===
        s.add(o)
        await s.commit()
        oid = o.id
        bot_id = row.id
    await state.update_data(vc_disc_code=None)
    await state.clear()
    await cb.answer()
    await _show_card_details(cb, bot, bot_id, gb, days, count, price, oid, _pkey, state=state)  # PATCH_AUTO_APPROVE_VPN_CARD_V1


@vpn_user_router.callback_query(F.data.startswith("vpaycard:cancel:"))
async def vpn_card_cancel(cb: CallbackQuery, bot: Bot):
    """لغو سفارش کارتی."""
    oid = int(cb.data.split(":")[2])
    async with Session() as s:
        from models import Order
        o = await s.get(Order, oid)
        bot_id = o.bot_id if o else None
        if o and o.status == "awaiting_receipt":
            o.status = "rejected"
            await s.commit()
        if bot_id is None:
            row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
            bot_id = row.id if row else None
    from tenant import inline_back_kb
    try:
        await cb.message.edit_text("❌ سفارش لغو شد.",
                                    reply_markup=await inline_back_kb(bot_id) if bot_id else None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("لغو شد")


# این handler به tenant/__init__.py منتقل شده تا با card_receipt_upload ادغام شود
# (جلوگیری از تعارض دو handler عکس در یک dispatcher)
# @vpn_user_router.message(StateFilter(None), F.photo)
# async def vpn_card_receipt(msg: Message, bot: Bot):  # DISABLED - merged into card_receipt_upload
async def vpn_card_receipt_DISABLED(msg: Message, bot: Bot):
    """دریافت عکس رسید کارت‌به‌کارت برای سفارش VPN (فقط vpn_card)."""
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        if not u:
            return
        from models import Order
        o = await s.scalar(select(Order).where(
            Order.bot_id == row.id, Order.user_id == u.id,
            Order.pay_method == "vpn_card",
            Order.status == "awaiting_receipt"
        ).order_by(Order.id.desc()))
        if not o:
            return   # رسید برای چیز دیگه‌ایه، نادیده بگیر
        o.status         = "awaiting_admin"
        o.receipt_file_id = msg.photo[-1].file_id
        oid    = o.id
        amount = float(o.amount)
        gb, days, count = o.volume_gb, o.days, o.count
        await s.commit()
    vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
    dur = "بی‌انقضا" if days == 0 else f"{days} روز"
    await msg.answer_photo(
        msg.photo[-1].file_id,
        caption=f"✅ رسید دریافت شد.\n\n"
                f"سفارش: {vol}/{dur} × {count} عدد\n"
                f"مبلغ: {rial(amount)}\n\n"
                f"منتظر تأیید ادمین بمانید.")
    # اطلاع به ادمین
    from tenant import _notify_admins, is_owner_or_admin
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ساخت کانفیگ", callback_data=f"vcadm:ok:{oid}")],
        [InlineKeyboardButton(text="❌ رد پرداخت", callback_data=f"vcadm:no:{oid}")],
    ])
    uname = f"@{msg.from_user.username}" if msg.from_user.username else f"<code>{msg.from_user.id}</code>"
    await _notify_admins(
        bot, row,
        f"💳 <b>رسید خرید VPN</b>\n\n"
        f"کاربر: {uname}\n"
        f"سرویس: {vol}/{dur} × {count} عدد\n"
        f"مبلغ: {rial(amount)}\n"
        f"سفارش: #{await order_no(Order, oid)}",
        kb,
        msg.photo[-1].file_id)


@vpn_user_router.callback_query(F.data.startswith("vcadm:"))
async def vcadm_decide(cb: CallbackQuery, bot: Bot):
    """تأیید/رد پرداخت کارتی VPN توسط ادمین → ساخت کانفیگ روی پنل."""
    from tenant import is_owner_or_admin
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
            await cb.answer("فقط ادمین‌ها.", show_alert=True)
            return
        _, action, oid = cb.data.split(":")
        oid = int(oid)
        from models import Order
        from sqlalchemy import update as sa_update
        new_status = "paid" if action == "ok" else "rejected"
        # قفل اتمیک: فقط اگر سفارش هنوز «awaiting_admin» باشد وضعیتش عوض می‌شود.
        # این از پردازش تکراری در اثر دابل‌کلیک ادمین یا ارسال مجدد callback توسط
        # تلگرام جلوگیری می‌کند (باگ ساخت دو کانفیگ برای یک سفارش).
        # Order.bot_id هم شرط است: بدونِ آن ادمینِ یک ربات با callback_dataِ
        # دست‌ساز می‌توانست سفارشِ رباتِ دیگری را تأیید/رد کند.
        result = await s.execute(
            sa_update(Order)
            .where(Order.id == oid, Order.bot_id == row.id, Order.status == "awaiting_admin")
            .values(status=new_status)
        )
        await s.commit()
        if result.rowcount != 1:
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        o = await s.get(Order, oid)
        if not o:
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        u      = await s.get(BotUser, o.user_id)
        gb     = o.volume_gb
        days   = o.days
        count  = o.count
        o_clabel = (o.custom_label or "").strip()[:60]
        bot_id = row.id
        # === PATCH_VPN_CARD_PANEL_KEY_RESOLVE_V1 ===
        # ترتیبِ تشخیصِ پنلِ درست: ۱) panel_key خودِ سفارش، ۲) panel_key پلنی که
        # سفارش رویش ثبت شده (اگر panel_key سفارش خالی بود ولی plan_id داشت)،
        # ۳) فقط در نبودِ هردو، اولین پنل فعال به‌عنوان آخرین راه‌حل (و با هشدار
        # به ادمین، نه بی‌صدا).
        _panel_guessed = False
        if o.panel_key:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.panel_key == o.panel_key))
        else:
            _plan_pk = None
            if getattr(o, "plan_id", None):
                _plan_row = await s.get(VpnPlan, o.plan_id)
                if _plan_row and _plan_row.bot_id == bot_id:
                    _plan_pk = _plan_row.panel_key
            if _plan_pk:
                panel = await s.scalar(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id, VpnPanel.panel_key == _plan_pk))
            else:
                # سفارش‌های خیلی قدیمی بدون panel_key و بدون plan_id — آخرین راه‌حل
                panel = await s.scalar(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))
                _panel_guessed = True
        # وضعیت سفارش قبلاً به‌صورت اتمیک در بالا تنظیم شده؛ اینجا فقط داده می‌خوانیم.
        buyer_tg_id = u.telegram_id
    admin_name = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    if action != "ok":
        await cb.answer("رد شد ❌")
        try:
            original = cb.message.caption or cb.message.text or ""
            done_text = original + f"\n\n❌ <b>رد شد توسط ادمین</b> {admin_name}"
            if cb.message.caption is not None:
                await cb.message.edit_caption(caption=done_text, reply_markup=None)
            else:
                await cb.message.edit_text(done_text, reply_markup=None)
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        from tenant import inline_back_kb
        await bot.send_message(u.telegram_id,
                               "❌ پرداخت شما تأیید نشد. برای راهنمایی با پشتیبانی تماس بگیرید.",
                               reply_markup=await inline_back_kb(bot_id))
        return
    await cb.answer("تأیید شد — در حال ساخت کانفیگ… ✅")
    # پاداش زیرمجموعه در اولین خرید
    from tenant.shared.extras import handle_referral_purchase
    # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
    try:
        _bg(handle_referral_purchase(bot_id, buyer_tg_id, bot), "پاداش دعوت")
    except Exception:
        logger.exception("پاداشِ دعوت ناموفق بود (خرید ادامه می‌یابد)")
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n✅ <b>تایید شد توسط ادمین</b> {admin_name}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None)
        else:
            await cb.message.edit_text(done_text, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not panel or not panel.enabled:
        from tenant import inline_back_kb
        await bot.send_message(u.telegram_id, "❌ خطا: پنل VPN یافت نشد یا غیرفعال است. با پشتیبانی تماس بگیرید.",
                               reply_markup=await inline_back_kb(bot_id))
        return
    if _panel_guessed:
        # === PATCH_VPN_CARD_PANEL_KEY_RESOLVE_V1 ===
        try:
            await bot.send_message(
                cb.from_user.id,
                f"⚠️ این سفارش (#{await order_no(Order, oid)}) پنلِ ثبت‌شده/پلنِ مشخصی نداشت؛ کانفیگ روی "
                f"«{panel.panel_key}» (اولین پنل فعال) ساخته شد. اگر پنلِ درست چیزِ "
                f"دیگری بود، لطفاً کانفیگ را از «مدیریت سرویس‌های VPN» به پنل درست منتقل/اصلاح کن.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # ساخت کانفیگ‌ها روی پنل
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    client      = _client_from_panel(panel)
    created_ids = []
    failed      = 0
    try:
        await client.login()
        for _ in range(count):
            try:
                email    = await _make_config_email(
                    bot_id, panel.panel_key, row.username if row else None,
                    u.telegram_id, u.username)
                # === PATCH_PURCHASE_ON_HOLD_V1 ===
                _poh = await purchase_on_hold_for(bot_id, panel.panel_key)  # PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1
                # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
                _lim = await panel_default_limit_ip(bot_id, panel.panel_key)
                acc      = await client.create_account(
                    email, gb=gb, days=days, limit_ip=_lim,
                    uuid_str=gen_uuid(), sub_id=gen_sub_id(), on_hold=_poh)
                sub_link = acc.get("sub_link") or ""
                async with Session() as s:
                    new_acc = PanelAccount(
                        bot_id=bot_id, user_telegram_id=u.telegram_id,
                        email=email, panel_key=panel.panel_key,
                        uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                        sub_link=sub_link, volume_gb=gb, days=days,
                        plan_title=(o_clabel or _plan_title_fa(gb, days)), status="active",
                        svc_code=await new_svc_code(s, bot_id),
                        expires_at=_expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False)
                    s.add(new_acc)
                    await s.commit()
                    created_ids.append(new_acc.id)
            except Exception as e:
                # === PATCH_VCADM_CREATE_ACCOUNT_LOGGING_V1 ===
                logger.warning(
                    "خطای ساخت کانفیگ روی پنل (vcadm, bot_id=%s, panel=%s, email=%s): %s",
                    bot_id, panel.panel_key, email, e)
                failed += 1
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not created_ids:
        from tenant import inline_back_kb
        await bot.send_message(u.telegram_id,
                               "❌ متأسفانه ساخت کانفیگ ناموفق بود. با پشتیبانی تماس بگیرید.",
                               reply_markup=await inline_back_kb(bot_id))
        return
    if failed:
        await bot.send_message(u.telegram_id,
                               f"⚠️ {len(created_ids)} کانفیگ ساخته شد، {failed} ناموفق بود.")
    for aid in created_ids:
        # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
        try:
            await _send_config_delivery(bot, u.telegram_id, aid)
        except Exception:
            logger.exception("تحویلِ کانفیگ ناموفق بود (خرید ادامه می‌یابد)")

    if created_ids:
        try:
            from tenant import _purchase_alert
            if row:
                # === PATCH_TRUST_CHANNEL_PLAN_TITLE_V1 ===
                _pt = o_clabel or _plan_title_fa(gb, days)
                # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
                from tenant.bots.vpn.vpn_admin import panel_label
                _pname = await panel_label(bot_id, panel.panel_key) if panel else None
                # === PATCH_ORDER_BUTTON_ID_V1 ===
                _bname = None
                if getattr(o, "button_id", None):
                    async with Session() as _s:
                        _mb = await _s.get(MainButton, o.button_id)
                        _bname = _mb.label if _mb else None
                await _purchase_alert(
                    bot, row, buyer=u,
                    section="سرویس VPN (پنل — تاییدِ دستیِ ادمین)",
                    detail=_purchase_detail(_pt, gb, days, len(created_ids)), amount=float(o.amount),
                    public_detail=_purchase_detail(_plan_title_fa(gb, days), gb, days, len(created_ids)),  # PATCH_TRUST_DETAIL_DEDUP_V2 (قبلاً حجم/مدت دوبار نوشته می‌شد)  # PATCH_TRUST_CHANNEL_NO_CUSTOM_LABEL_V1
                    order_id=o.id, tracking_code=o.tracking_code, count=len(created_ids),
                    panel_name=_pname, button_name=_bname)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ─────────────── خرید آنلاین VPN با حجم/روز دلخواه (+/−) ───────────────
def _vc_cfg_kb(cfg: dict, gb: int, days: int, count: int, no_plans: bool = False,
               applied_code: str | None = None, discount: int = 0,
               discount_enabled: bool = True) -> InlineKeyboardMarkup:
    """کیبورد +/− برای تنظیم حجم/روز/تعداد."""
    mn_gb = int(cfg["vc_min_gb"]); mx_gb = int(cfg["vc_max_gb"])
    mn_d  = int(cfg["vc_min_days"]); mx_d = int(cfg["vc_max_days"])
    mx_c  = int(cfg["vc_max_count"])
    pg    = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])
    total = (_vc_gb_cost(cfg, gb) + days * pd) * count  # PATCH_VPN_TIER_CUMULATIVE_V1
    final_total = max(0, total - int(discount or 0))

    # === PATCH_VC_KB_COLOR_V1 ===
    # رنگی‌کردنِ کیبوردِ +/−: مثبت سبز، منفی قرمز، ستونِ وسط (مقدار) آبی.
    def _btn(t, cb, style=None):
        if style is not None:
            return InlineKeyboardButton(text=t, callback_data=cb, style=style)
        return InlineKeyboardButton(text=t, callback_data=cb)
    locked_gb   = mn_gb == mx_gb
    locked_days = mn_d  == mx_d
    simple = cfg.get("vc_simple") == "1"      # === PATCH_VC_SIMPLE_MODE_V1 ===

    rows = []
    # حجم
    if simple:
        rows.append([_btn(f"✏️ تغییرِ حجم — {gb} گیگ · {days} روز", "vc:gb:manual", ButtonStyle.PRIMARY)])
    elif locked_gb:
        # === PATCH_VC_LOCKED_BTN_COLOR_V1 ===
        # گزارشِ ادمین (ایلیا): وقتی حجم/روز «ثابت» است (min == max)، دکمهٔ قفل‌شده
        # بدونِ رنگ ساخته می‌شد، درحالی‌که همان ردیف در حالتِ عادی آبی (PRIMARY) است؛
        # نتیجه یک دکمهٔ بی‌رنگِ عجیب وسطِ کیبوردِ رنگی بود. حالا هم‌رنگِ ستونِ مقدار.
        rows.append([_btn(f"📦 حجم: {gb} گیگ  🔒", "vc:noop", ButtonStyle.PRIMARY)])
    else:
        # === PATCH_VC_MANUAL_ENTRY_HEADER_V1 ===
        rows.append([_btn("🎯 انتخاب حجم دلخواه", "vc:gb:manual", ButtonStyle.PRIMARY)])
        rows.append([_btn("➖", "vc:gb-", ButtonStyle.DANGER),
                     _btn(f"📦 {gb} گیگ", "vc:gb:manual", ButtonStyle.PRIMARY),
                     _btn("➕", "vc:gb+", ButtonStyle.SUCCESS)])
    # مدت
    if simple:
        pass
    elif locked_days:
        # === PATCH_VC_LOCKED_BTN_COLOR_V1 === (توضیح: بالاتر، ردیفِ حجم)
        rows.append([_btn(f"📅 مدت: {days} روز  🔒", "vc:noop", ButtonStyle.PRIMARY)])
    else:
        # === PATCH_VC_MANUAL_ENTRY_HEADER_V1 ===
        rows.append([_btn("🎯 انتخاب روز دلخواه", "vc:d:manual", ButtonStyle.PRIMARY)])
        rows.append([_btn("➖", "vc:d-", ButtonStyle.DANGER),
                     _btn(f"📅 {days} روز", "vc:d:manual", ButtonStyle.PRIMARY),
                     _btn("➕", "vc:d+", ButtonStyle.SUCCESS)])
    # تعداد
    if mx_c > 1 and not simple:
        # === PATCH_VC_MANUAL_ENTRY_HEADER_V1 ===
        rows.append([_btn("🎯 انتخاب تعداد دلخواه", "vc:c:manual", ButtonStyle.PRIMARY)])
        rows.append([_btn("➖", "vc:c-", ButtonStyle.DANGER),
                     _btn(f"🔢 تعداد: {count}", "vc:c:manual", ButtonStyle.PRIMARY),
                     _btn("➕", "vc:c+", ButtonStyle.SUCCESS)])
    # قیمت
    if applied_code and discount > 0:
        rows.append([_btn(f"💵 مجموع: {final_total:,} تومان (با تخفیف)", "vc:noop")])
    else:
        rows.append([_btn(f"💵 مجموع: {total:,} تومان", "vc:noop")])
    # کد تخفیف
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    # همون باگی که تویِ خریدِ پلن (_build_buy_summary) بود، اینجا هم بود:
    # discount_purchase_enabled اصلاً چک نمی‌شد. چون این تابع sync است
    # (نمی‌تونه await بزنه)، مقدارِ تنظیم رو فراخوان (که async است) از قبل
    # حساب می‌کنه و به‌عنوانِ پارامتر می‌ده.
    # === PATCH_VC_DISCOUNT_ONLY_AT_PAYMENT_V1 ===
    # پیشنهادِ مشتری: کدِ تخفیف فقط در مرحلهٔ پرداخت (پیش‌فاکتور) نشان داده شود، نه در انتخابِ حجم.
    # (دکمه‌هایِ vc:disc:add / vc:disc:del در صفحهٔ پیش‌فاکتور همچنان هست.)
    # خرید — یک دکمهٔ واحد؛ ابتدا اسم دلخواه پرسیده می‌شود سپس پیش‌فاکتور
    rows.append([InlineKeyboardButton(text="💳 پرداخت",
                                      callback_data=f"vc:checkout:{gb}:{days}:{count}",
                                      style=ButtonStyle.SUCCESS)])
    if no_plans:
        rows.append([_btn("🏠 بازگشت به خانه", "vc:home")])
    else:
        rows.append([_btn("⬅️ بازگشت به منوی قبل", "vc:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _vc_cfg_text(cfg: dict, gb: int, days: int, count: int, balance: float = -1,
                 applied_code: str | None = None, discount: int = 0,
                 title: str = "خرید VPN — حجم/روز دلخواه", template: str = "") -> str:
    pg    = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])
    total = (_vc_gb_cost(cfg, gb) + days * pd) * count  # PATCH_VPN_TIER_CUMULATIVE_V1
    discount = int(discount or 0)
    final_total = max(0, total - discount)
    # === PATCH_VC_PAGE_TEMPLATE_V1 === متنِ کاملِ قابل‌ویرایشِ صفحه (مثلِ کانالِ اعتماد)
    if template:
        warn = "⚠️ موجودی کافی نیست" if (balance >= 0 and balance < final_total) else ""
        _vals = {
            "title": title, "gb": gb, "days": days, "count": count,
            "price_gb": f"{pg:,}", "price_day": f"{pd:,}",
            "total": f"{final_total:,}", "balance": (f"{int(balance):,}" if balance >= 0 else "—"),
            "discount": f"{discount:,}", "warn": warn,
        }
        out = template
        for _k, _v in _vals.items():
            out = out.replace("{" + _k + "}", str(_v))
        return out
    lines = [f"⚙️ <b>{title}</b>\n"]   # === PATCH_VPN_EDITABLE_TITLES_V1 === (title از فراخوان: escape‌شده)
    if int(cfg["vc_min_gb"]) == int(cfg["vc_max_gb"]):
        lines.append(f"📦 حجم: <b>{gb} گیگ</b>  (ثابت)")
    else:
        lines.append(f"📦 حجم: <b>{gb} گیگ</b>  (بازه {cfg['vc_min_gb']}–{cfg['vc_max_gb']})")
    if int(cfg["vc_min_days"]) == int(cfg["vc_max_days"]):
        lines.append(f"📅 مدت: <b>{days} روز</b>  (ثابت)")
    else:
        lines.append(f"📅 مدت: <b>{days} روز</b>  (بازه {cfg['vc_min_days']}–{cfg['vc_max_days']})")
    lines.append(f"🔢 تعداد: <b>{count}</b>")
    lines.append(f"\n💵 قیمت هر گیگ: {pg:,} تومان")
    lines.append(f"💵 قیمت هر روز: {pd:,} تومان")
    if applied_code and discount > 0:
        lines.append(f"\n💰 مجموع: <s>{total:,}</s> تومان")
        lines.append(f"🎟 تخفیف ({applied_code}): <b>−{discount:,}</b> تومان")
        lines.append(f"💳 <b>مبلغ پرداختی: {final_total:,} تومان</b>")
    else:
        lines.append(f"\n💰 <b>مجموع: {total:,} تومان</b>")
    if balance >= 0:
        warn = "  ⚠️ موجودی کافی نیست" if balance < final_total else ""
        lines.append(f"👛 موجودی کیف پول: <b>{int(balance):,} تومان</b>{warn}")
    lines.append("\nبا دکمه‌های ➕/➖ تنظیم کن، سپس خرید کن.")
    return "\n".join(lines)


async def _vc_render(bot: Bot, bot_id: int, telegram_id: int, gb: int, days: int,
                     count: int, panel_key: str, state, no_plans: bool = False):
    """متن و کیبورد صفحهٔ کانفیگ آماده را با احتساب کد تخفیفِ ذخیره‌شده در state
    می‌سازد. کد روی «مجموع فعلی» دوباره اعتبارسنجی می‌شود (چون کاربر ممکن است
    بعد از وارد کردن کد، حجم/روز/تعداد را تغییر داده باشد)."""
    # === PATCH_VPN_TIER_GB_BRACKETS_V1 === حجمِ فعلی هم فرستاده می‌شود تا
    # قیمت از پلهٔ حجمیِ درست خوانده شود، نه از اولین تعرفهٔ هم‌مدت.
    cfg = await _vc_all_for_user(bot_id, telegram_id, panel_key, days=days, gb=gb)
    # اگر با تغییرِ روز، حجمِ فعلی از بازهٔ تعرفهٔ جدید بیرون افتاده باشد، آن را
    # داخلِ بازهٔ مجاز گرد می‌کنیم (مثلاً حجمِ ۸۰ گیگ وقتی کاربر از تعرفهٔ ۱ماهه
    # به تعرفهٔ ۲ماهه‌ای با سقفِ ۵۰ گیگ سوییچ می‌کند).
    gb = max(int(cfg["vc_min_gb"]), min(gb, int(cfg["vc_max_gb"])))
    # گِرد کردن ممکن است حجم را به پلهٔ دیگری برده باشد → قیمت را دوباره بخوان
    cfg = await _vc_all_for_user(bot_id, telegram_id, panel_key, days=days, gb=gb)
    pg = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])
    total = (_vc_gb_cost(cfg, gb) + days * pd) * count  # PATCH_VPN_TIER_CUMULATIVE_V1
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
    balance = float(u.balance) if u else 0.0

    data = await state.get_data() if state else {}
    applied_code = data.get("vc_disc_code")
    discount = 0
    if applied_code:
        res = await validate_discount(
            bot_id=bot_id, code=applied_code, telegram_id=telegram_id,
            price=total, panel_key=panel_key or None, plan_id=None, button_id=data.get("vbuy_mb_id"))
        if res.ok and res.discount > 0:
            discount = res.discount
        else:
            if state:
                await state.update_data(vc_disc_code=None)
            applied_code = None
    from tenant import get_setting as _gs0
    text = _vc_cfg_text(cfg, gb, days, count, balance=balance,
                        applied_code=applied_code, discount=discount,
                        title=await _get_page_title(bot_id, "vc"),  # === PATCH_VPN_EDITABLE_TITLES_V1 ===
                        template=((await _gs0(bot_id, "vc_page_template", "")) or "").strip())
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    from tenant import get_setting as _gs
    _disc_enabled = await _gs(bot_id, "discount_purchase_enabled", "1") == "1"
    kb = _vc_cfg_kb(cfg, gb, days, count, no_plans=no_plans,
                    applied_code=applied_code, discount=discount,
                    discount_enabled=_disc_enabled)
    # === PATCH_VC_BACK_PREV_MENU_V1 ===
    # پیشنهادِ مشتری: در صفحهٔ انتخابِ حجم، بالایِ «بازگشت به خانه» یک «بازگشت به منویِ قبل» هم باشد
    # (فقط وقتی صفحه از داخلِ یک زیرمنو/انتخابِ سرور باز شده و منویِ قبلی وجود دارد).
    if no_plans:
        try:
            from tenant import inline_back_row
            _br = await inline_back_row(bot_id)
            if _br and len(_br) == 2 and (_br[0].callback_data or "").startswith("smopen:"):
                _rows = list(kb.inline_keyboard)
                _rows.insert(max(len(_rows) - 1, 0), [_br[0]])
                kb = InlineKeyboardMarkup(inline_keyboard=_rows)
        except Exception:
            logger.exception("vc: دکمهٔ بازگشت به منویِ قبل ساخته نشد")
    return text, await _with_home_row(kb, bot_id)



async def _vc_simple_start(bot_id: int, telegram_id: int, panel_key: str, state):
    """=== PATCH_VC_SIMPLE_MODE_V1 === حالتِ ساده را آماده می‌کند: مدتِ ثابتِ ادمین، state برایِ دریافتِ عددِ حجم.
    خروجی: (متنِ پرسش، کیبورد) یا None اگر حالتِ ساده برایِ این پنل خاموش است."""
    cfg = await _vc_all_for_user(bot_id, telegram_id, panel_key)
    if cfg.get("vc_simple") != "1":
        return None
    try:
        days = max(1, int(cfg.get("vc_simple_days") or 30))
    except (TypeError, ValueError):
        days = 30
    cfg = await _vc_all_for_user(bot_id, telegram_id, panel_key, days=days)
    mn, mx, st = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"]), int(cfg["vc_step_gb"])
    await state.update_data(vc_gb=mn, vc_days=days, vc_count=1, vc_bot_id=bot_id,
                            vc_panel_key=panel_key, vc_disc_code=None, vc_stage=None,
                            vc_manual_field="gb", vc_manual_inplace=True)
    await state.set_state(VpnConfiguratorFSM.entering_manual)
    txt = (f"📌 حجمِ درخواستیِ خود را (به گیگابایت) بفرستید.\n"
           f"🔔 قیمت هر گیگ حجم {int(cfg['vc_price_gb']):,} تومان می‌باشد.\n"
           f"🔔 حداقل حجم {mn} گیگابایت و حداکثر {mx} گیگابایت می‌باشد"
           + (f" (مضرب {st})" if st > 1 else "") + ".\n"
           f"⏳ مدتِ سرویس: {days} روز")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="vc:back")]])
    return txt, kb


@vpn_user_router.callback_query(F.data.startswith("vc:open"))
async def vc_open(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """باز کردن رابط +/− خرید آنلاین — panel_key از callback می‌آید."""
    parts = cb.data.split(":")
    cur_panel_key = parts[2] if len(parts) > 2 else ""
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer(); return
    cfg = await _vc_all_for_user(row.id, cb.from_user.id, cur_panel_key)
    if cfg["vc_enabled"] != "1":
        await cb.answer("این بخش فعال نیست.", show_alert=True); return
    gb    = int(cfg["vc_min_gb"])
    days  = int(cfg["vc_min_days"])
    count = 1
    # === PATCH_VC_SIMPLE_MODE_V1 === حالتِ ساده: مدتِ ثابتِ ادمین؛ کاربر فقط عددِ حجم را می‌فرستد
    _simple = await _vc_simple_start(row.id, cb.from_user.id, cur_panel_key, state)
    if _simple:
        txt, kb = _simple
        await state.update_data(vc_manual_prompt_id=cb.message.message_id,
                                vc_manual_orig_id=cb.message.message_id)
        await cb.answer()
        try:
            await cb.message.edit_text(txt, reply_markup=kb)
        except Exception:
            sent = await bot.send_message(cb.message.chat.id, txt, reply_markup=kb)
            await state.update_data(vc_manual_prompt_id=sent.message_id, vc_manual_orig_id=sent.message_id)
        return
    await state.set_state(VpnConfiguratorFSM.selecting)
    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count,
                            vc_bot_id=row.id, vc_panel_key=cur_panel_key)
    await cb.answer()
    await state.update_data(vc_disc_code=None, vc_stage=None)   # شروع تازه → بدون کد تخفیف/فاز
    text, kb = await _vc_render(bot, row.id, cb.from_user.id, gb, days, count,
                                cur_panel_key, state)
    await cb.message.edit_text(text, reply_markup=kb)


@vpn_user_router.callback_query(F.data == "vc:home")
async def vc_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """برگشت به منوی اصلی از صفحه +/− (وقتی پلن ثابت وجود نداره)."""
    await state.clear()
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if row:
        from tenant import main_kb_or_remove, get_setting
        from aiogram.types import ReplyKeyboardRemove
        kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
        await cb.message.answer("🏠 منوی اصلی:", reply_markup=kb or ReplyKeyboardRemove())


@vpn_user_router.callback_query(F.data.in_({"vc:gb+", "vc:gb-", "vc:d+", "vc:d-",
                                              "vc:c+", "vc:c-", "vc:noop", "vc:back"}))
async def vc_adjust(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """هندلر +/− ."""
    if cb.data == "vc:noop":
        await cb.answer(); return
    if cb.data == "vc:back":
        _data_before_clear = await state.get_data()
        _back_panel_key = _data_before_clear.get("vc_panel_key", "")
        await state.clear()
        await cb.answer()
        # برگشت به لیست پلن‌ها: پیام فعلی را حذف کن و لیست پلن‌ها را ارسال کن
        async with Session() as s:
            row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if row:
            # === PATCH_VC_SIMPLE_BACK_V1 ===
            # باگِ گزارش‌شده (WHALE SHOP): در حالتِ ساده (فقط عددِ حجم) و بدونِ پلنِ ثابت، «بازگشت» به‌جایِ برگشت، صفحهٔ +/−
            # (شبیهِ فاکتور) را باز می‌کرد، چون لیستِ پلنی وجود ندارد که به آن برگردد. حالا به منویِ قبلی (یا خانه) برمی‌گردد.
            try:
                _cfg_b = await _vc_all_for_user(row.id, cb.from_user.id, _back_panel_key)
                async with Session() as _sb:
                    _np = await _sb.scalar(select(func.count()).select_from(VpnPlan).where(
                        VpnPlan.bot_id == row.id, VpnPlan.active == True,  # noqa: E712
                        VpnPlan.panel_key == _back_panel_key))
                if _cfg_b.get("vc_simple") == "1" and not _np:
                    from tenant import _last_menu_for, smopen_handler
                    _pm = _last_menu_for(row.id)
                    if _pm:
                        await smopen_handler(cb.model_copy(update={"data": f"smopen:{_pm}"}), bot, state)
                    else:
                        await vc_home(cb, bot, state)
                    return
            except Exception:
                logger.exception("vc: بازگشتِ حالتِ ساده ناموفق؛ مسیرِ عادی")
            # PATCH_VBUYBACK_KEEP_CONTEXT_V1 — mb_id هم حفظ شود تا فیلترِ
            # دسته‌بندیِ همان دکمه از دست نرود.
            await show_vpn_plans(bot, _EditFirst(cb.message, cb.from_user), row.id, state=None,   # PATCH_VPN_EDIT_IN_PLACE_V1
                                 panel_key=_back_panel_key or None,
                                 mb_id=_data_before_clear.get("vbuy_mb_id"))
        return
    # همان لحظه جواب بده: خواندنِ تنظیمات و ساختِ صفحه ~۲ ثانیه طول می‌کشد و چرخانکِ دکمه نباید تا آن موقع بماند
    try:
        await cb.answer()
    except Exception:
        logger.debug("vc: پاسخِ زودهنگام ممکن نشد")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        return
    await _vc_recover_panel_key(state, row.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
    data  = await state.get_data()
    cur_panel_key = data.get("vc_panel_key", "")
    cfg   = await _vc_all_for_user(row.id, cb.from_user.id, cur_panel_key,
                                   days=data.get("vc_days"), gb=data.get("vc_gb"))
    gb    = data.get("vc_gb",    int(cfg["vc_min_gb"]))
    days  = data.get("vc_days",  int(cfg["vc_min_days"]))
    count = data.get("vc_count", 1)
    mn_gb = int(cfg["vc_min_gb"]); mx_gb = int(cfg["vc_max_gb"]); st_gb = int(cfg["vc_step_gb"])
    mn_d  = int(cfg["vc_min_days"]); mx_d = int(cfg["vc_max_days"]); st_d = int(cfg["vc_step_days"])
    mx_c  = int(cfg["vc_max_count"])
    if   cb.data == "vc:gb+": gb    = min(gb + st_gb, mx_gb)
    elif cb.data == "vc:gb-": gb    = max(gb - st_gb, mn_gb)
    elif cb.data == "vc:d+":  days  = min(days + st_d, mx_d)
    elif cb.data == "vc:d-":  days  = max(days - st_d, mn_d)
    elif cb.data == "vc:c+":  count = min(count + 1, mx_c)
    elif cb.data == "vc:c-":  count = max(count - 1, 1)
    if cb.data in ("vc:d+", "vc:d-"):
        # با تغییرِ روز ممکن است وارد بازهٔ تعرفهٔ دیگری شده باشیم → بازهٔ
        # حجم/قیمت را برای همین روزِ جدید دوباره بخوان و حجمِ فعلی را در
        # صورتِ نیاز داخلِ بازهٔ مجازِ تعرفهٔ جدید گرد کن.
        cfg_new = await _vc_all_for_user(row.id, cb.from_user.id, cur_panel_key,
                                         days=days, gb=gb)
        gb = max(int(cfg_new["vc_min_gb"]), min(gb, int(cfg_new["vc_max_gb"])))
    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count)
    try:
        text, kb = await _vc_render(bot, row.id, cb.from_user.id, gb, days, count,
                                    cur_panel_key, state)
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# === PATCH_VC_MANUAL_ENTRY_V1 ===
# واردکردنِ دستیِ عددِ حجم/مدت/تعداد به‌جایِ زدنِ مکررِ ➕/➖.
_VC_MANUAL_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# === PATCH_VC_MANUAL_INPLACE_FIX_V1 ===
# گزارشِ تازیان‌تیم: «حجم دلخواه رو که وارد می‌کنی مرحلهٔ بعد نمیاد، رویِ همون پیام می‌مونه».
# ریشه: در منویِ شیشه‌ای، میان‌افزارِ glass_generic اولین `cb.message.answer(...)`ی که دکمهٔ اینلاین
# داشته باشد را به *ویرایشِ همان پیام* تبدیل می‌کند. پرسشِ «عددِ حجم را بفرست» هم همین‌طور می‌شد:
# خودِ صفحهٔ +/− به پرسش تبدیل می‌شد و prompt_id با orig_id یکی می‌شد. بعد از وارد‌کردنِ عدد، کد
# «پرسش» را پاک می‌کرد (یعنی خودِ صفحه را!) و بعد سعی می‌کرد همان پیام را ویرایش کند →
# «message to edit not found» که بی‌صدا قورت داده می‌شد؛ کاربر هیچ صفحه‌ای نمی‌دید.
# حالا اگر پرسش رویِ همان پیام نشسته باشد («درجا»)، به‌جایِ حذف، همان پیام دوباره به صفحهٔ +/−
# برمی‌گردد؛ و اگر ویرایش به هر دلیلی نشد، صفحهٔ تازه فرستاده می‌شود تا کاربر هرگز گیر نکند.
async def _vc_show_page(bot: Bot, chat_id: int, orig_id, bot_id: int, telegram_id: int,
                        gb: int, days: int, count: int, panel_key: str, state) -> None:
    try:
        text, kb = await _vc_render(bot, bot_id, telegram_id, gb, days, count, panel_key, state)
    except Exception:
        logger.exception("vc: ساختِ صفحهٔ +/− بعد از ورودیِ دستی ناموفق بود")
        try:
            await bot.send_message(chat_id, "⚠️ نمایشِ صفحه با خطا مواجه شد؛ از منو دوباره شروع کن.")
        except Exception:
            logger.exception("vc: ارسالِ پیامِ خطا هم ناموفق بود")
        return
    if orig_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=orig_id, reply_markup=kb)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logger.warning("vc: ویرایشِ صفحهٔ اصلی ممکن نشد؛ صفحهٔ تازه فرستاده می‌شود: %s", e)
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        logger.exception("vc: ارسالِ صفحهٔ تازه بعد از ورودیِ دستی ناموفق بود")


@vpn_user_router.callback_query(F.data.in_({"vc:gb:manual", "vc:d:manual", "vc:c:manual"}))
async def vc_manual_prompt(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """درخواستِ واردکردنِ دستیِ عدد؛ به‌جایِ زدنِ مکررِ ➕/➖."""
    field = {"vc:gb:manual": "gb", "vc:d:manual": "days", "vc:c:manual": "count"}[cb.data]
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer(); return
    await _vc_recover_panel_key(state, row.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
    data = await state.get_data()
    cur_panel_key = data.get("vc_panel_key", "")
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    if gb is None or days is None:
        await cb.answer(); return
    cfg = await _vc_all_for_user(row.id, cb.from_user.id, cur_panel_key, days=days, gb=gb)
    if field == "gb":
        mn, mx = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
        st = int(cfg["vc_step_gb"])
        prompt = f"📦 عددِ حجم را بین {mn} تا {mx} گیگ بفرست" + (f" (گام {st})" if st > 1 else "") + ":"
    elif field == "days":
        mn, mx = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
        st = int(cfg["vc_step_days"])
        prompt = f"📅 عددِ مدت را بین {mn} تا {mx} روز بفرست" + (f" (گام {st})" if st > 1 else "") + ":"
    else:
        mn, mx = 1, int(cfg["vc_max_count"])
        prompt = f"🔢 عددِ تعداد را بین {mn} تا {mx} بفرست:"
    await cb.answer()
    # === PATCH_VC_MANUAL_ENTRY_HEADER_V1 ===
    # === PATCH_VC_MANUAL_PROMPT_INPLACE_V1 ===
    # پیشنهادِ مشتری: پرسشِ حجم/روز/تعداد رویِ همان صفحه بنشیند (نه پیامِ جدا زیرِ صفحه) تا فقط گزینهٔ «بازگشت» بماند و
    # منوی قبلی باز و قابلِ زدن نباشد (زدنش «عملیات قبلی لغو شد» می‌داد). اگر ویرایش ممکن نبود (پیامِ عکس‌دار) مثلِ قبل.
    _txt = f"{prompt}\n(یا دکمهٔ زیر را برایِ انصراف بزن)"
    _kb_back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="vc:manual:back")],
    ])
    sent = None
    try:
        await cb.message.edit_text(_txt, reply_markup=_kb_back)
        sent = cb.message
    except Exception:
        logger.info("vc: ویرایشِ درجایِ پرسش ممکن نشد؛ پیامِ جدا فرستاده می‌شود")
    if sent is None:
        sent = await bot.send_message(cb.message.chat.id, _txt, reply_markup=_kb_back)
    # === PATCH_VC_MANUAL_INPLACE_FIX_V1 === اگر منویِ شیشه‌ای پرسش را رویِ خودِ صفحه نشانده باشد،
    # شناسهٔ پیامِ پرسش با شناسهٔ صفحه یکی است.
    _inplace = bool(sent is not None and sent.message_id == cb.message.message_id)
    await state.update_data(vc_manual_field=field, vc_manual_prompt_id=sent.message_id,
                            vc_manual_orig_id=cb.message.message_id,
                            vc_manual_inplace=_inplace)
    await state.set_state(VpnConfiguratorFSM.entering_manual)


@vpn_user_router.message(VpnConfiguratorFSM.entering_manual)
async def vc_manual_enter(msg: Message, bot: Bot, state: FSMContext):
    """دریافتِ عددِ دستی؛ اگر در بازه بود بی‌سروصدا ثبت و پیام‌ها پاک می‌شوند."""
    data = await state.get_data()
    field = data.get("vc_manual_field")
    prompt_id = data.get("vc_manual_prompt_id")
    orig_id = data.get("vc_manual_orig_id")
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    panel_key = data.get("vc_panel_key", "")
    txt = (msg.text or "").strip()
    # === PATCH_VC_MANUAL_INPLACE_FIX_V1 === پرسش رویِ همان صفحه نشسته؟ (نگاه کن به _vc_show_page)
    inplace = bool(data.get("vc_manual_inplace")) or bool(prompt_id and prompt_id == orig_id)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or gb is None or days is None or field is None:
        await state.set_state(VpnConfiguratorFSM.selecting)
        await msg.answer("خطا در بازیابی سفارش. دوباره از منو شروع کن.")
        return

    async def _cleanup_prompt():
        try:
            await msg.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        # در حالتِ «درجا» پیامِ پرسش همان صفحه است؛ پاک‌کردنش یعنی پاک‌کردنِ کلِ صفحه.
        if prompt_id and not inplace:
            try:
                await bot.delete_message(msg.chat.id, prompt_id)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    if txt.lower() in ("/cancel", "لغو", "انصراف"):
        await _cleanup_prompt()
        await state.set_state(VpnConfiguratorFSM.selecting)
        await state.update_data(vc_manual_field=None, vc_manual_prompt_id=None,
                                vc_manual_orig_id=None, vc_manual_inplace=False)
        if inplace:
            await _vc_show_page(bot, msg.chat.id, orig_id, row.id, msg.from_user.id,
                                gb, days, count, panel_key, state)
        return

    raw = "".join(ch for ch in txt.translate(_VC_MANUAL_FA_DIGITS) if ch.isdigit())
    if not raw:
        await msg.answer("❌ فقط عدد بفرست. (یا /cancel برای انصراف)")
        return
    value = int(raw)

    cfg = await _vc_all_for_user(row.id, msg.from_user.id, panel_key, days=days, gb=gb)
    if field == "gb":
        mn, mx = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
    elif field == "days":
        mn, mx = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
    else:
        mn, mx = 1, int(cfg["vc_max_count"])
    if value < mn or value > mx:
        await msg.answer(f"❌ باید بینِ {mn} تا {mx} باشد. دوباره بفرست یا /cancel بزن.")
        return

    # === PATCH_VC_MANUAL_STEP_SNAP_V1 ===
    # باگِ واقعی: این مسیر فقط بازه (min/max) رو چک می‌کرد، نه گام
    # (step) رو — یعنی اگه ادمین گامِ ۱۰تایی گذاشته بود، مشتری می‌تونست
    # عددی مثلِ ۲۳ رو وارد کنه که هیچ‌وقت از دکمه‌هایِ ➕/➖ بهش نمی‌رسید.
    # حالا مقدارِ واردشده به نزدیک‌ترین مضربِ گام (نسبت به mn) گرد می‌شه.
    step = int(cfg["vc_step_gb"]) if field == "gb" else (
        int(cfg["vc_step_days"]) if field == "days" else 1)
    if step > 1:
        snapped = mn + round((value - mn) / step) * step
        value = max(mn, min(snapped, mx))

    if field == "gb":
        gb = value
    elif field == "days":
        days = value
        # مثلِ +/−: با تغییرِ روز ممکن است بازهٔ حجم عوض شود
        cfg_new = await _vc_all_for_user(row.id, msg.from_user.id, panel_key, days=days, gb=gb)
        gb = max(int(cfg_new["vc_min_gb"]), min(gb, int(cfg_new["vc_max_gb"])))
    else:
        count = value

    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count,
                            vc_manual_field=None, vc_manual_prompt_id=None,
                            vc_manual_orig_id=None, vc_manual_inplace=False)
    await state.set_state(VpnConfiguratorFSM.selecting)
    await _cleanup_prompt()
    # === PATCH_VC_MANUAL_INPLACE_FIX_V1 === صفحهٔ +/− همیشه نشان داده می‌شود (ویرایشِ همان پیام،
    # و اگر نشد صفحهٔ تازه) — دیگر خطا بی‌صدا قورت داده نمی‌شود.
    await _vc_show_page(bot, msg.chat.id, orig_id, row.id, msg.from_user.id,
                        gb, days, count, panel_key, state)


# === PATCH_VC_MANUAL_ENTRY_HEADER_V1 ===
@vpn_user_router.callback_query(F.data == "vc:manual:back")
async def vc_manual_back(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """دکمهٔ «بازگشت» توی صفحهٔ واردکردنِ دستیِ عدد؛ چیزی تغییر نمی‌کند."""
    data = await state.get_data()
    # === PATCH_VC_MANUAL_INPLACE_FIX_V1 === اگر پرسش رویِ خودِ صفحه نشسته بود، «بازگشت» نباید
    # پیام را پاک کند (کلِ صفحه می‌پرید)؛ همان پیام دوباره به صفحهٔ +/− برمی‌گردد.
    inplace = bool(data.get("vc_manual_inplace")) or (
        data.get("vc_manual_prompt_id") == data.get("vc_manual_orig_id")
        and data.get("vc_manual_prompt_id") == cb.message.message_id)
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    panel_key = data.get("vc_panel_key", "")
    await state.set_state(VpnConfiguratorFSM.selecting)
    await state.update_data(vc_manual_field=None, vc_manual_prompt_id=None,
                            vc_manual_orig_id=None, vc_manual_inplace=False)
    await cb.answer()
    if inplace and gb is not None and days is not None:
        async with Session() as s:
            row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if row:
            await _vc_show_page(bot, cb.message.chat.id, cb.message.message_id, row.id,
                                cb.from_user.id, gb, days, count, panel_key, state)
            return
    if inplace:
        return     # اطلاعاتِ سفارش گم شده؛ پیام را پاک نمی‌کنیم (همان پرسش می‌ماند تا /start)
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ─────────────── کد تخفیف در «کانفیگ آماده» ───────────────
@vpn_user_router.callback_query(F.data == "vc:disc:add")
async def vc_disc_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    from tenant import get_bot_row, get_setting
    _row = await get_bot_row(bot)
    if _row and await get_setting(_row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف در حال حاضر غیرفعال است.", show_alert=True)
        return
    await state.set_state(_VpnDiscountFSM.entering_vc)
    await cb.answer()
    await cb.message.answer("🎟 کد تخفیف را وارد کن:\n(یا /cancel برای انصراف)")


@vpn_user_router.message(_VpnDiscountFSM.entering_vc)
async def vc_disc_enter(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    panel_key = data.get("vc_panel_key", "")
    txt = (msg.text or "").strip()
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or gb is None or days is None:
        await state.set_state(VpnConfiguratorFSM.selecting)
        await msg.answer("خطا در بازیابی سفارش. دوباره از منو شروع کن.")
        return
    cfg = await _vc_all_for_user(row.id, msg.from_user.id, panel_key, days=days, gb=gb)
    total = (_vc_gb_cost(cfg, gb) + days * int(cfg["vc_price_day"])) * count  # PATCH_VPN_TIER_CUMULATIVE_V1
    if txt.lower() in ("/cancel", "لغو", "انصراف"):
        await state.set_state(VpnConfiguratorFSM.selecting)
        text, kb = await _vc_render(bot, row.id, msg.from_user.id, gb, days, count, panel_key, state)
        await msg.answer("لغو شد.")
        await msg.answer(text, reply_markup=kb)
        return
    res = await validate_discount(
        bot_id=row.id, code=txt, telegram_id=msg.from_user.id,
        price=total, panel_key=panel_key or None, plan_id=None, button_id=data.get("vbuy_mb_id"))
    if not res.ok:
        await msg.answer(f"❌ {res.reason}\n\nدوباره وارد کن یا /cancel بزن.")
        return
    await state.update_data(vc_disc_code=res.code)
    await state.set_state(VpnConfiguratorFSM.selecting)
    if data.get("vc_stage") == "preinvoice":
        await msg.answer(f"✅ کد «{res.code}» اعمال شد.")
        await _show_vc_preinvoice(msg, bot, state, msg.from_user.id)
        return
    text, kb = await _vc_render(bot, row.id, msg.from_user.id, gb, days, count, panel_key, state)
    await msg.answer(f"✅ کد «{res.code}» اعمال شد.")
    await msg.answer(text, reply_markup=kb)


@vpn_user_router.callback_query(F.data == "vc:disc:del")
async def vc_disc_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.update_data(vc_disc_code=None)
    data = await state.get_data()
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    panel_key = data.get("vc_panel_key", "")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or gb is None:
        await cb.answer(); return
    if data.get("vc_stage") == "preinvoice":
        text, kb = await _vc_preinvoice(bot, row.id, cb.from_user.id, gb, days, count, panel_key, state)
    else:
        text, kb = await _vc_render(bot, row.id, cb.from_user.id, gb, days, count, panel_key, state)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("کد تخفیف حذف شد.")


async def _vc_preinvoice(bot: Bot, bot_id: int, telegram_id: int, gb: int, days: int,
                         count: int, panel_key: str, state):
    """پیش‌فاکتور خرید دلخواه (vc): اطلاعات خریدار + مشخصات + قیمت + دکمه‌های
    پرداخت (کیف پول/کارت) + کد تخفیف. اسم دلخواه از state خوانده می‌شود."""
    cfg = await _vc_all_for_user(bot_id, telegram_id, panel_key, days=days, gb=gb)  # PATCH_VPN_TIER_GB_BRACKETS_V1
    pg = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])
    total = (_vc_gb_cost(cfg, gb) + days * pd) * count  # PATCH_VPN_TIER_CUMULATIVE_V1
    data = await state.get_data() if state else {}
    applied_code = data.get("vc_disc_code")
    discount = 0
    if applied_code:
        res = await validate_discount(
            bot_id=bot_id, code=applied_code, telegram_id=telegram_id,
            price=total, panel_key=panel_key or None, plan_id=None, button_id=data.get("vbuy_mb_id"))
        if res.ok and res.discount > 0:
            discount = res.discount
        else:
            if state:
                await state.update_data(vc_disc_code=None)
            applied_code = None
    final_total = max(0, total - discount)

    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        card_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_number"))
        card_toggle_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_enabled"))
        card_buy_set = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "card_purchase_enabled"))
    bal = float(u.balance) if u else 0.0
    buyer_name = (u.full_name if u else "") or ""
    buyer_uname = (u.username if u else "") or ""
    # باگ: مثلِ خلاصهٔ خریدِ پلن‌ها، اینجا هم قبلاً تاگلِ «کارت‌به‌کارت» و
    # تاگلِ «وضعیت در خرید» چک نمی‌شد و فقط وجودِ شماره‌کارت کافی بود.
    card_toggle_on = (card_toggle_set.value if card_toggle_set else "1") == "1"
    card_buy_on = (card_buy_set.value if card_buy_set else "1") == "1"
    card_enabled = bool(card_set and card_set.value) and card_toggle_on and card_buy_on

    custom_label = (data.get("vcustom_label") or "").strip()
    label_line = f"\n🏷 نام کانفیگ: <b>{custom_label}</b>" if custom_label else ""
    vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
    dur = "بی‌انقضا" if days == 0 else f"{days} روز"

    buyer_lines = f"👤 خریدار: {buyer_name or '—'}"
    if buyer_uname:
        buyer_lines += f" (@{buyer_uname})"
    buyer_lines += f"\n🆔 شناسه: <code>{telegram_id}</code>"

    if applied_code and discount > 0:
        price_block = (f"💵 مجموع: <s>{total:,}</s> تومان\n"
                       f"🎟 تخفیف ({applied_code}): <b>−{discount:,}</b> تومان\n"
                       f"💳 <b>مبلغ پرداختی: {final_total:,} تومان</b>")
    else:
        price_block = f"💰 <b>مبلغ پرداختی: {total:,} تومان</b>"
    enough = "" if bal >= final_total else "\n\n⚠️ موجودی کیف پول کافی نیست؛ اول شارژ کن."

    note_line = ""
    if panel_key:
        try:
            from tenant.bots.vpn.vpn_admin import get_panel_note
            _note = await get_panel_note(bot_id, panel_key, "invoice")
            if _note:
                note_line = f"📝 {_note}\n"
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    text = (f"🧾 <b>پیش فاکتور</b>\n"
            f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            f"{buyer_lines}\n"
            f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            f"⚙️ <b>{await _get_page_title(bot_id, 'vc')}</b>{label_line}\n\n"   # === PATCH_VPN_EDITABLE_TITLES_V1 ===
            f"📦 حجم: {vol}\n📅 مدت: {dur}\n🔢 تعداد: {count}\n{price_block}\n"
            f"{note_line}"
            f"👛 موجودی کیف پول: {int(bal):,} تومان{enough}")

    # === PATCH_WALLET_PAY_TOGGLE_V1 ===
    from tenant import wallet_pay_on as _wpay_on2
    kb_rows = ([[InlineKeyboardButton(text="✅ پرداخت از کیف پول",
                callback_data=f"vc:pay:{gb}:{days}:{count}", style=ButtonStyle.SUCCESS)]]
               if await _wpay_on2(bot_id) else [])
    # === PATCH_ABANGATEWAY_REPLACE_CARD_V1 === (در خریدِ دلخواه، آبان گیت‌وی از مسیرِ
    # «شارژِ کیف‌پول» زیر می‌آید؛ تکمیلِ خودکارِ خرید فقط برایِ پلن‌هایِ آماده است)
    from tenant import _ab_replaces_card
    if card_enabled and not await _ab_replaces_card(bot_id):
        kb_rows.append([InlineKeyboardButton(text="💳 پرداخت کارت‌به‌کارت",
                        callback_data=f"vc:cardpay:{gb}:{days}:{count}")])
    # === PATCH_ABANGATEWAY_VC_DIRECT_V1 ===
    # گزارش (ایلیا): در خریدِ دلخواه (مثلاً ۱۰ گیگ = ۲۰٬۰۰۰ تومان) بعد از انتخابِ آبان گیت‌وی
    # دوباره مبلغ پرسیده می‌شد. مثلِ پلن‌ها، دکمهٔ مستقیم با مبلغِ همین فاکتور اضافه می‌شود.
    _ab_vc_btn = None
    try:
        from tenant import get_setting as _gs_abv, gw_btn as _gwb_v
        from tenant.gateways.abangateway import is_available as _ab_ok_v
        if (0 <= gb <= 4095 and 0 <= days <= 2047 and 1 <= count <= 31
                and ((await _ab_replaces_card(bot_id)) or await _gs_abv(bot_id, "abangateway_show_purchase", "1") == "1")
                and await _ab_ok_v(bot_id)):
            _ab_vc_btn = InlineKeyboardButton(
                text=await _gwb_v(bot_id, 'abangateway', 'پرداخت با', '💳'),
                callback_data=f"w:abangateway:vc:{gb}:{days}:{count}:{int(final_total)}")
    except Exception:
        logger.exception("بررسیِ آبان گیت‌وی برایِ خریدِ دلخواهِ VPN ناموفق بود")
    # === PATCH_APAY_DIRECT_PURCHASE_V1 === پرداختِ مستقیمِ آپی برایِ همین خریدِ دلخواه
    _ap_vc_btn = None
    try:
        from tenant import get_setting as _gs_apv, gw_btn as _gwb_apv
        from tenant.gateways.apay import is_available as _ap_ok_v
        if (bal < final_total and 0 <= gb <= 4095 and 0 <= days <= 2047 and 1 <= count <= 31
                and await _gs_apv(bot_id, "apay_show_purchase", "1") == "1" and await _ap_ok_v(bot_id)):
            _ap_vc_btn = InlineKeyboardButton(
                text=await _gwb_apv(bot_id, 'apay', 'پرداخت با', '💳'),
                callback_data=f"w:apay:vc:{gb}:{days}:{count}:{int(final_total)}")
    except Exception:
        logger.exception("بررسیِ آپی برایِ خریدِ دلخواهِ VPN ناموفق بود")
    # === PATCH_WALLET_TOPUP_ROWS_V1 ===
    if bal < final_total:
        from tenant import wallet_topup_rows
        # === PATCH_WALLET_TOPUP_SKIP_CARD_V1 ===
        kb_rows.extend(await wallet_topup_rows(bot_id, skip_card=card_enabled,
                                               skip_abangateway=_ab_vc_btn is not None,
                                               skip_apay=_ap_vc_btn is not None))
    elif _ab_vc_btn is None:
        # === PATCH_ABAN_ALWAYS_SHOW_PURCHASE_V1 === با موجودیِ کافی هم آبان‌گیت (اگر تیکِ نمایش در خرید روشن است)
        from tenant import wallet_topup_rows as _wtr_v
        kb_rows.extend([r for r in await _wtr_v(bot_id, skip_card=True)
                        if any((b.callback_data or "").startswith("w:abangateway") for b in r)])
    from tenant import wallet_charge_btn_rows as _wcb2, _has_wallet_charge_btn as _hwc2   # PATCH_WALLET_CHARGE_BTN_V1
    if not _hwc2(kb_rows):
        kb_rows.extend(await _wcb2(bot_id))
    if _ab_vc_btn is not None:
        kb_rows.append([_ab_vc_btn])
    if _ap_vc_btn is not None:
        kb_rows.append([_ap_vc_btn])
    # === PATCH_VPN_DISCOUNT_TOGGLE_V1 ===
    from tenant import get_setting as _gs
    if await _gs(bot_id, "discount_purchase_enabled", "1") == "1":
        if applied_code and discount > 0:
            kb_rows.append([InlineKeyboardButton(
                text=f"❌ حذف کد تخفیف ({applied_code})", callback_data="vc:disc:del")])
        else:
            kb_rows.append([InlineKeyboardButton(
                text="🎟 وارد کردن کد تخفیف", callback_data="vc:disc:add")])
    # PATCH_PAYMENT_GUIDE_BUTTON_V1 — بالایِ دکمهٔ بازگشت
    _pg = await _pay_guide_row(bot_id)
    if _pg:
        kb_rows.append(_pg)
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vc:back",
                    style=ButtonStyle.DANGER)])
    return text, await _with_home_row(InlineKeyboardMarkup(inline_keyboard=kb_rows), bot_id)


@vpn_user_router.callback_query(F.data.startswith("vc:checkout:"))
async def vc_checkout(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """دکمهٔ «پرداخت» در صفحهٔ خرید دلخواه → پرسیدن اسم دلخواه (اختیاری)."""
    parts = cb.data.split(":")
    gb, days, count = int(parts[2]), int(parts[3]), int(parts[4])
    await state.update_data(vc_gb=gb, vc_days=days, vc_count=count, vcustom_label=None)
    await state.set_state(_VpnNameFSM.naming_vc)
    await cb.answer()
    # === PATCH_CFG_NAME_PROMPT_EDIT_V1 ===
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    if _r:
        await _vc_recover_panel_key(state, _r.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
    await _ask_custom_name(_EditFirst(cb.message, cb.from_user), 0, "vc", _r.id if _r else 0)


async def _show_vc_preinvoice(target, bot: Bot, state: FSMContext, telegram_id: int):
    data = await state.get_data()
    gb = data.get("vc_gb"); days = data.get("vc_days"); count = data.get("vc_count", 1)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or gb is None:
        await target.answer("این بخش دیگر در دسترس نیست.")
        return
    panel_key = await _vc_recover_panel_key(state, row.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
    # علامت بگذار که کاربر در فاز پیش‌فاکتور است تا کد تخفیف به پیش‌فاکتور برگردد
    await state.update_data(vc_stage="preinvoice")
    text, kb = await _vc_preinvoice(bot, row.id, telegram_id, gb, days, count, panel_key, state)
    await target.answer(text, reply_markup=kb)


@vpn_user_router.callback_query(F.data == "vcname:skip")
async def vc_name_skip(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.update_data(vcustom_label=None)
    await state.set_state(VpnConfiguratorFSM.selecting)
    await cb.answer()
    await _show_vc_preinvoice(_EditFirst(cb.message, cb.from_user), bot, state, cb.from_user.id)


@vpn_user_router.message(_VpnNameFSM.naming_vc, F.text)
async def vc_name_enter(msg: Message, bot: Bot, state: FSMContext):
    txt = (msg.text or "").strip()
    # === PATCH_VPN_NAME_STEP_EXIT_TEXT_V1 ===
    # همان باگ، در ویزاردِ «کانفیگورِ دلخواه» (vc_*) هم وجود داشت.
    if _is_vpn_name_step_exit(txt):
        await state.update_data(vcustom_label=None)
        await state.set_state(None)
        from tenant import tenant_buttons
        await tenant_buttons(msg, bot, state)
        return
    if txt.lower() in ("/skip", "/cancel", "لغو", "انصراف"):
        await state.update_data(vcustom_label=None)
    else:
        await state.update_data(vcustom_label=txt[:60])
    await state.set_state(VpnConfiguratorFSM.selecting)
    await _show_vc_preinvoice(msg, bot, state, msg.from_user.id)


@vpn_user_router.callback_query(F.data.startswith("vc:pay:"))
async def vc_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پرداخت از کیف پول برای خرید آنلاین VPN."""
    # === PATCH_VPN_DOUBLE_BUY_GUARD_V1 ===
    _key = f"vcpay:{bot.token}:{cb.from_user.id}:{cb.data}"
    async with _vpn_once(_key) as _ok:
        if not _ok:
            await cb.answer("⏳ درخواست قبلی‌ات هنوز در حال پردازش است، کمی صبر کن.",
                            show_alert=True)
            return
        # === PATCH_PAY_PHONE_GATE_FIX_V1 === گیتِ شماره پیش از پرداخت
        if await _pay_gate_block(cb, bot):
            return
        # === PATCH_PURCHASE_IDEMPOTENCY_V1 === قفلِ دیتابیسی (ضدِ چند-ورکری)
        if not await _claim_or_reject(cb, bot, "vcpay"):
            return
        await _vc_pay_impl(cb, bot, state)


async def _vc_pay_impl(cb: CallbackQuery, bot: Bot, state: FSMContext):
    import traceback as _tb
    print(f"[VC_PAY] شروع هندلر | data={cb.data} | user={cb.from_user.id}", flush=True)
    # === PATCH_VC_PAY_INSTANT_BUTTON_CLEAR_V1 ===
    # همون قدمِ اول، قبل از هر کارِ کند/دیتابیسی، دکمه‌های پیش‌فاکتور رو پاک
    # می‌کنیم تا کاربر فیزیکاً دیگه نتونه روی همین پیام دوباره تپ کنه — نه
    # اینکه فقط با یک چکِ نرم‌افزاری رد بشه. اگه کاربر بخواد دوباره بخره، باید
    # از نو حجم/روز رو تنظیم کنه و یک پیش‌فاکتورِ تازه بسازه.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (پاک‌کردنِ فوریِ دکمه‌ها ناموفق بود)")
    parts = cb.data.split(":")
    gb, days, count = int(parts[2]), int(parts[3]), int(parts[4])
    disc_code_id = None      # برای عودت مصرف در صورت شکست کامل
    applied_code = None
    discount = 0
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            print("[VC_PAY] ربات (row) پیدا نشد", flush=True)
            await cb.answer(); return
        await _vc_recover_panel_key(state, row.id)     # PATCH_VC_RECOVER_PANEL_KEY_V1
        _sd = await state.get_data()
        _pk = _sd.get("vc_panel_key", "")

        # === PATCH_VC_PAY_EMPTY_PANELKEY_V1 ===
        # ریشهٔ ضررِ مالیِ گزارش‌شده: اگر stateِ کاربر منقضی/پاک شده باشد
        # (مثلاً پیش‌فاکتور را باز گذاشته و بعداً دکمه را زده)، _pk خالی
        # می‌شود و _vc به تنظیماتِ عمومی/پیش‌فرض سقوط می‌کند — یعنی بازهٔ
        # ۵-۱۰۰ گیگ و ۳۰-۹۰ روز با قیمتِ پیش‌فرض، به‌جای تعرفهٔ همان پنل.
        # انتخابِ پنل چند خط پایین‌تر مسیرِ جداگانهای دارد، پس کانفیگ از
        # پنلِ درست ساخته می‌شد ولی با قیمتِ اشتباه فروخته می‌شد.
        if not _pk:
            logger.warning(
                "[VC_PAY] panel_key خالی بود؛ خرید رد شد | user=%s data=%s",
                cb.from_user.id, cb.data)
            await cb.answer(
                "نشستِ خرید منقضی شده. لطفاً دوباره از منو حجم و مدت را "
                "انتخاب کن.", show_alert=True)
            return
        cfg = await _vc_all_for_user(row.id, cb.from_user.id, _pk,
                                     days=days, gb=gb)  # PATCH_VPN_TIER_GB_BRACKETS_V1
        pg  = int(cfg["vc_price_gb"]); pd = int(cfg["vc_price_day"])

        # === PATCH_VC_PAY_RANGE_GUARD_V1 ===
        # باگِ گزارش‌شده (ضررِ مالیِ واقعی): gb/days/count مستقیم از
        # callback_data خوانده می‌شدند و هیچ‌جا با بازهٔ مجازِ همان پنل
        # تطبیق داده نمی‌شدند. کاربری با دستکاری callback_data توانست
        # «۱۰۰ گیگ / ۹۰ روز» بخرد در حالی که ربات روی ۳۰ روز قفل بود، و
        # قیمت هم از فرمول عمومی حساب شد نه از تعرفهٔ آن پنل.
        # حالا هر سه مقدار سمت سرور اعتبارسنجی می‌شوند.
        try:
            _mn_gb, _mx_gb = int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"])
            _mn_d,  _mx_d  = int(cfg["vc_min_days"]), int(cfg["vc_max_days"])
            _st_gb = max(1, int(cfg.get("vc_step_gb", 1) or 1))
            _st_d  = max(1, int(cfg.get("vc_step_days", 1) or 1))
        except Exception:
            # قبلاً در این حالت اعتبارسنجی کلاً رد می‌شد (fail-open) — یعنی همان
            # سوءاستفادهٔ دستکاریِ callback دوباره ممکن می‌شد. حالا خرید رد می‌شود.
            logger.exception("[VC_PAY] خواندنِ بازهٔ مجاز ناموفق بود؛ خرید رد شد")
            await cb.answer("تنظیماتِ فروشِ این پنل ناقص است؛ به پشتیبانی خبر بده.", show_alert=True)
            return

        if _mn_gb is not None:
            _bad = None
            if not (_mn_gb <= gb <= _mx_gb):
                _bad = f"حجم باید بین {_mn_gb} تا {_mx_gb} گیگ باشد."
            elif not (_mn_d <= days <= _mx_d):
                _bad = f"مدت باید بین {_mn_d} تا {_mx_d} روز باشد."
            elif (gb - _mn_gb) % _st_gb:
                _bad = f"حجم باید مضربی از {_st_gb} گیگ باشد."
            elif (days - _mn_d) % _st_d:
                _bad = f"مدت باید مضربی از {_st_d} روز باشد."
            elif not (1 <= count <= 20):
                _bad = "تعدادِ نامعتبر."
            if _bad:
                logger.warning(
                    "[VC_PAY] درخواستِ خارج از بازه رد شد | user=%s data=%s "
                    "gb=%s days=%s count=%s | مجاز: gb %s-%s، days %s-%s",
                    cb.from_user.id, cb.data, gb, days, count,
                    _mn_gb, _mx_gb, _mn_d, _mx_d)
                await cb.answer("درخواستِ نامعتبر: " + _bad, show_alert=True)
                return

        price_each = _vc_gb_cost(cfg, gb) + days * pd  # PATCH_VPN_TIER_CUMULATIVE_V1
        total      = price_each * count
        # ── کد تخفیف: اعتبارسنجی دوبارهٔ کد روی مجموع، در همان تراکنش ──
        stored_code = _sd.get("vc_disc_code")
        if stored_code:
            res = await validate_discount(
                bot_id=row.id, code=stored_code, telegram_id=cb.from_user.id,
                price=int(total), panel_key=_pk or None, plan_id=None,
                button_id=_sd.get("vbuy_mb_id"), session=s)
            if res.ok and res.discount > 0:
                discount = res.discount
                disc_code_id = res.code_id
                applied_code = res.code
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        # مسیرِ «حجم/روزِ دلخواه». مثلِ بقیه، فقط وقتی کدی اعمال نشده.
        if not discount:
            from tenant.gateways.discount_engine import first_purchase_discount
            _fd = await first_purchase_discount(
                bot_id=row.id, telegram_id=cb.from_user.id,
                price=int(total), session=s)
            if _fd.ok and _fd.discount > 0:
                discount = _fd.discount
                applied_code = "اولین خرید"
        paid_total = total - discount
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        if not u:
            print("[VC_PAY] کاربر در دیتابیس پیدا نشد", flush=True)
            await cb.answer("کاربر یافت نشد.", show_alert=True); return
        # === PATCH_VC_PAY_DEDUPE_V1 ===
        # این دکمه («پرداخت از کیف پول» در خریدِ دلخواهِ VPN) هیچ Order یا
        # شناسهٔ یکتایی نداره — callback_data فقط gb/days/count رو مستقیم حمل
        # می‌کنه، پس هر تپ یک درخواستِ کاملاً معتبر و مستقل به‌حساب می‌اومد.
        # از طرفی ساختِ کانفیگ روی پنل (پایین‌ترِ همین تابع) کند و بیرونِ این
        # قفل انجام می‌شه؛ یعنی قفلِ ردیفِ کاربر فقط جلوی کسرِ هم‌زمان/منفی‌شدنِ
        # موجودی رو می‌گیره، نه جلوی چند تپِ پشتِ‌سرهم (حتی غیرِ کاملاً هم‌زمان)
        # که هر کدوم بعد از commit شدنِ قبلی می‌رسن و یک خریدِ کاملاً «قانونیِ»
        # جدید حساب می‌شن. اینجا زیرِ همون قفل، چک می‌کنیم که آیا چند ثانیهٔ
        # اخیر یک تراکنشِ خرید دقیقاً با همین مبلغ برای همین کاربر ثبت شده؛
        # اگه بله یعنی این تپِ تکراری/دابل‌کلیک است و رد می‌شه.
        _dedupe_cutoff = _dt.datetime.utcnow() - _dt.timedelta(seconds=6)
        _dup = await s.scalar(select(WalletTxn).where(
            WalletTxn.bot_id == row.id, WalletTxn.user_id == u.id,
            WalletTxn.kind == "purchase", WalletTxn.amount == -paid_total,
            WalletTxn.created_at >= _dedupe_cutoff,
        ).order_by(WalletTxn.id.desc()))
        if _dup:
            print(f"[VC_PAY] درخواستِ تکراری (دابل‌تپ) نادیده گرفته شد | user={cb.from_user.id}", flush=True)
            await cb.answer(
                "⏳ همین سفارش چند لحظه پیش ثبت شد؛ لطفاً صبر کن تا کانفیگِ قبلی ارسال بشه "
                "و دوباره روی دکمه نزن.", show_alert=True)
            return
        if u.balance < paid_total:
            print(f"[VC_PAY] موجودی ناکافی | balance={u.balance} total={paid_total}", flush=True)
            await cb.answer(
                f"موجودی ناکافی 💰\nنیاز: {paid_total:,} تومان | موجودی: {int(u.balance):,} تومان",
                show_alert=True)
            return
        # کسر از موجودی (مبلغ پس از تخفیف)
        u.balance -= paid_total
        note = f"خرید {count} کانفیگ {gb}G/{days}D"
        if applied_code:
            note += f" (کد {applied_code} −{discount:,})"
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-paid_total,
                        kind="purchase", note=note))
        # ثبت مصرف کد تخفیف اتمیک با کسر کیف‌پول
        if disc_code_id:
            await record_usage(s, code_id=disc_code_id, bot_id=row.id,
                               telegram_id=cb.from_user.id, saved_amount=discount)
        await s.commit()
        print(f"[VC_PAY] کسر و commit شد | paid={paid_total} | باقی‌مانده={u.balance}", flush=True)
        bot_id       = row.id
        bot_username = row.username
        # === PATCH_VC_PAY_PANEL_KEY_FIX_V1 ===
        # قبلاً این کوئری panel_key رو فیلتر نمی‌کرد و همیشه اولین پنلِ
        # فعال رو برمی‌گردوند (نه پنلی که کاربر از دکمه‌اش وارد شده بود).
        if _pk:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.panel_key == _pk, VpnPanel.enabled == True))
        else:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))

    if not panel:
        print("[VC_PAY] هیچ پنل فعالی پیدا نشد", flush=True)
        await cb.answer("پنل یافت نشد.", show_alert=True); return

    print(f"[VC_PAY] پنل انتخاب شد | panel_id={panel.id} base_url={panel.base_url}", flush=True)
    # اسم دلخواه را پیش از پاک‌کردن state بخوان
    _vc_data = await state.get_data()
    _vc_clabel = (_vc_data.get("vcustom_label") or "").strip()[:60] or None
    await state.clear()
    await cb.answer("در حال ساخت کانفیگ…")
    created_ids = []
    failed      = 0
    last_err    = ""          # آخرین خطای واقعیِ پنل، برای نمایش به کاربر/ادمین
    client      = _client_from_panel(panel)
    try:
        print("[VC_PAY] در حال login به پنل…", flush=True)
        await client.login()
        print("[VC_PAY] login موفق بود", flush=True)
        for i in range(count):
            try:
                email   = await _make_config_email(
                    bot_id, panel.panel_key, bot_username, cb.from_user.id, cb.from_user.username,
                    custom_label=_vc_clabel)  # === PATCH_SRVNAME_CUSTOM_ID_V1 ===
                print(f"[VC_PAY] ساخت اکانت {i+1}/{count} | email={email}", flush=True)
                # === PATCH_PURCHASE_ON_HOLD_V1 ===
                _poh = await purchase_on_hold_for(bot_id, panel.panel_key)  # PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1
                # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
                _lim = await panel_default_limit_ip(bot_id, panel.panel_key)
                acc     = await client.create_account(
                    email, gb=gb, days=days, limit_ip=_lim,
                    uuid_str=gen_uuid(), sub_id=gen_sub_id(), on_hold=_poh)
                print(f"[VC_PAY] اکانت ساخته شد | acc={acc}", flush=True)
                sub_link = acc.get("sub_link") or ""
                async with Session() as s:
                    new_acc = PanelAccount(
                        bot_id=bot_id, user_telegram_id=cb.from_user.id,
                        email=email, panel_key=panel.panel_key,
                        uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                        sub_link=sub_link, volume_gb=gb, days=days,
                        plan_title=(_vc_clabel or _plan_title_fa(gb, days)), status="active",
                        paid_toman=(int(paid_total // max(int(count), 1)) or None),   # PATCH_VPN_REFUND_V1
                        svc_code=await new_svc_code(s, bot_id),
                        expires_at=_expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False)
                    s.add(new_acc)
                    await s.commit()
                    created_ids.append(new_acc.id)
            except Exception as e:
                failed += 1
                last_err = str(e)[:300]
                print(f"[VC_PAY] خطا در ساخت اکانت {i+1}/{count}:\n" + _tb.format_exc(), flush=True)
    except Exception as e:
        # خطای کلی در login یا قبل از حلقه (قبلاً اینجا بی‌صدا بالا می‌رفت)
        failed = count
        last_err = str(e)[:300]
        print("[VC_PAY] خطای کلی (احتمالاً login):\n" + _tb.format_exc(), flush=True)
    finally:
        try:
            await client.close()
        except Exception:
            print("[VC_PAY] خطا در بستن client:\n" + _tb.format_exc(), flush=True)

    print(f"[VC_PAY] نتیجه نهایی | created={len(created_ids)} failed={failed}", flush=True)

    # برگشت وجه برای هر کانفیگ ناموفق (متناسب با مبلغِ واقعاً پرداخت‌شده پس از تخفیف)
    kept_amount = paid_total  # === PATCH_VC_WALLET_ORDER_RECORD_V1 ===
    if failed:
        refund = int(round(paid_total * failed / count)) if count else 0
        kept_amount = paid_total - refund  # === PATCH_VC_WALLET_ORDER_RECORD_V1 ===
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id))
            if u:
                u.balance += refund
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=refund,
                                kind="refund",
                                note=f"برگشت وجه {failed} کانفیگ ناموفق"))
            # اگر همهٔ کانفیگ‌ها شکست خوردند، کد تخفیف را هم آزاد کن
            if disc_code_id and failed == count:
                await revert_usage(s, code_id=disc_code_id,
                                   telegram_id=cb.from_user.id)
            await s.commit()
            print(f"[VC_PAY] وجه برگردانده شد | refund={refund} | موجودی جدید={u.balance if u else '?'}", flush=True)

    try:
        if not created_ids:
            # قبلاً علتِ شکست هیچ‌جا به کاربر/فروشنده نشون داده نمی‌شد و عیب‌یابی
            # فقط از روی لاگِ سرور ممکن بود؛ حالا متنِ خطای پنل هم می‌آید.
            # === PATCH_USER_ERRORS_GENERIC_V1 === علتِ فنی فقط برایِ مالک/ادمین
            from tenant import report_user_error, USER_FAIL_TEXT
            if last_err:
                await report_user_error(bot, bot_id, "ساختِ کانفیگِ حجم/روز", last_err, cb.from_user.id)
            await cb.message.answer(
                "❌ ساخت کانفیگ انجام نشد و وجه برگردانده شد.\n" + USER_FAIL_TEXT)
            print("[VC_PAY] پیام شکست ارسال شد", flush=True)
            return

        if failed:
            await cb.message.answer(
                f"⚠️ {len(created_ids)} کانفیگ ساخته شد، {failed} ناموفق بود و وجه برگشت خورد.")
            print("[VC_PAY] پیام ساخت ناقص ارسال شد", flush=True)

        # === PATCH_DELIVERY_NEVER_LOST_V1 ===
        # همان محافظِ مسیرِ خریدِ پلن، برایِ مسیرِ «حجم/روزِ دلخواه».
        # اینجا حتی حساس‌تر است: چند کانفیگ در یک حلقه تحویل می‌شوند، پس
        # شکستِ اولی قبلاً جلویِ تحویلِ بقیه را هم می‌گرفت.
        try:
            from tenant.shared.extras import handle_referral_purchase
            _bg(handle_referral_purchase(bot_id, cb.from_user.id, bot), "پاداش دعوت")
        except Exception:
            logger.exception("پاداشِ دعوت ناموفق بود (تحویل ادامه می‌یابد)")

        _failed = []
        for aid in created_ids:
            try:
                await _send_config_delivery(bot, cb.from_user.id, aid)
            except Exception:
                logger.exception("تحویلِ کانفیگ ناموفق بود (aid=%s)", aid)
                _failed.append(aid)
        if _failed:
            try:
                await bot.send_message(
                    cb.from_user.id,
                    f"✅ {len(created_ids)} سرویس ساخته شد، ولی ارسالِ "
                    f"{len(_failed)} موردشان به مشکل خورد.\n\n"
                    "همه‌شان از بخشِ «کانفیگ‌های من» در دسترس‌اند.")
            except Exception:
                logger.exception("پیامِ جایگزینِ تحویلِ گروهی هم نرسید")
        print("[VC_PAY] تحویل کانفیگ(ها) با موفقیت انجام شد", flush=True)

        if created_ids:
            try:
                from tenant import _purchase_alert
                from models import Order
                # === PATCH_VC_WALLET_ORDER_RECORD_V1 ===
                # همون باگِ _vpn_pay_impl (خریدِ پلن): این مسیر («حجم/روزِ
                # دلخواه» با کیف‌پول) هم هیچ‌وقت Order نمی‌ساخت، فقط
                # WalletTxn(kind="purchase"). آمارِ «سفارش‌های موفق»/
                # «خریدارانِ موفق»/«درآمد کل» از رویِ Order حساب می‌شوند.
                async with Session() as s:
                    _buyer = await s.scalar(select(BotUser).where(
                        BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id))
                    if _buyer:
                        s.add(Order(bot_id=bot_id, user_id=_buyer.id,
                                    volume_gb=gb, days=days, count=len(created_ids),
                                    amount=kept_amount, pay_method="wallet", status="paid",
                                    panel_key=panel.panel_key if panel else None,
                                    custom_label=_vc_clabel,
                                    button_id=_vc_data.get("vbuy_mb_id")))
                        await s.commit()
                # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
                from tenant.bots.vpn.vpn_admin import panel_label
                _pname = await panel_label(bot_id, panel.panel_key) if panel else None
                # === PATCH_ORDER_BUTTON_ID_V1 ===
                _bname = None
                _mb_id = _vc_data.get("vbuy_mb_id")
                if _mb_id:
                    async with Session() as _s2:
                        _mb = await _s2.get(MainButton, _mb_id)
                        _bname = _mb.label if _mb else None
                await _purchase_alert(
                    bot, row, buyer=_buyer,
                    section="سرویس VPN (پنل — حجم/روزِ دلخواه)",
                    detail=_purchase_detail("", gb, days, len(created_ids)), amount=paid_total,   # PATCH_TRUST_DETAIL_DEDUP_V2 (×۱ فقط وقتی بیش از یکی)
                    count=len(created_ids), panel_name=_pname, button_name=_bname)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except Exception:
        print("[VC_PAY] خطا در ارسال پیام نهایی/تحویل کانفیگ:\n" + _tb.format_exc(), flush=True)



async def show_my_configs(bot: Bot, msg: Message, bot_id: int, tg_id: int):
    from models import Config
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        items = []
        n_panels = 0
        if u:
            # === PATCH_VPN_PANEL_MARKET_V1 ===
            # پنل‌های خریداری‌شده جدا شمرده می‌شن (حجم/روز ندارن، فرمتِ
            # ردیفشون فرق داره) — تویِ همین لیست قاطی نمی‌شن، فقط یه دکمه
            # به بخشِ اختصاصیِ خودشون اضافه می‌شه.
            cfgs = (await s.scalars(select(Config).where(
                Config.bot_id == bot_id, Config.owner_user_id == u.id,
                Config.status == "sold", Config.kind != "panel").order_by(Config.id.desc()))).all()
            items = [(c.id, c.volume_gb, c.days, c.code) for c in cfgs]
            n_panels = await s.scalar(select(func.count()).select_from(Config).where(
                Config.bot_id == bot_id, Config.owner_user_id == u.id,
                Config.status == "sold", Config.kind == "panel")) or 0
    if not items and not n_panels:
        from tenant import inline_back_kb
        await msg.answer("🎫 هنوز کانفیگی نخریدی.",
                         reply_markup=await inline_back_kb(bot_id))
        return
    rows = []
    for cid, vol, days, code in items:
        rows.append([InlineKeyboardButton(
            text=f"📦 {fmt_vol(vol)} / {days}روز (کد {code})", callback_data=f"mcfg:{cid}", style=ButtonStyle.PRIMARY)])
        rows.append([
            InlineKeyboardButton(text="📦 ارسال سرورهای تکی",
                                 callback_data=f"cfgdl:single:{cid}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🔗 ارسال ساب‌لینک",
                                 callback_data=f"cfgdl:sub:{cid}", style=ButtonStyle.PRIMARY),
        ])
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    if n_panels:
        rows.append([InlineKeyboardButton(
            text=f"🏷 پنل‌های خریداری‌شده ({n_panels})", callback_data="mypanels:list", style=ButtonStyle.SUCCESS)])
    # === PATCH_RESELLER_SVC_BACK_V1 ===
    _br = _svc_back_row(bot_id, tg_id)
    if not _br:
        from tenant import inline_back_row
        _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    await msg.answer("🎫 <b>کانفیگ‌های من</b>\n\nبرای دریافت دوبارهٔ هر کانفیگ، انتخاب کن:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@vpn_user_router.callback_query(F.data == "mypanels:list")
async def show_my_panels(cb: CallbackQuery, bot: Bot):
    """PATCH_VPN_PANEL_MARKET_V1 — لیستِ پنل‌هایِ خریداری‌شده‌یِ همین کاربر."""
    from models import Config
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        panels = []
        if u:
            panels = (await s.scalars(select(Config).where(
                Config.bot_id == row.id, Config.owner_user_id == u.id,
                Config.status == "sold", Config.kind == "panel").order_by(Config.id.desc()))).all()
    await cb.answer()
    rows = []
    for c in panels:
        try:
            _d = json.loads(c.payload) or {}
        except Exception:
            _d = {}
        _name = _d.get("name") or getattr(c, "custom_label", None) or f"پنل #{c.code}"
        rows.append([InlineKeyboardButton(
            text=f"🏷 {_name}", callback_data=f"mypanels:view:{c.id}", style=ButtonStyle.PRIMARY)])
    from tenant import inline_back_row
    _br = await inline_back_row(row.id)
    if _br:
        rows.append(_br)
    text = ("🏷 <b>پنل‌های خریداری‌شده</b>\n\nبرای دیدنِ دوبارهٔ مشخصات، انتخاب کن:"
            if panels else "🏷 <b>پنل‌های خریداری‌شده</b>\n\nهنوز پنلی نخریدی.")
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@vpn_user_router.callback_query(F.data.startswith("mypanels:view:"))
async def show_my_panel_detail(cb: CallbackQuery, bot: Bot):
    """PATCH_VPN_PANEL_MARKET_V1 — نمایشِ دوبارهٔ یه پنلِ خریداری‌شده."""
    from models import Config
    from tenant import _send_panel_to_user
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        c = await s.get(Config, cid)
        if not row or not c or c.bot_id != row.id or c.kind != "panel":
            await cb.answer("یافت نشد", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u or c.owner_user_id != u.id:
            await cb.answer("این پنل متعلق به شما نیست.", show_alert=True)
            return
    await cb.answer()
    await _send_panel_to_user(bot, cb.from_user.id, c)


@vpn_user_router.callback_query(F.data.startswith("mcfg:"))
async def show_manual_config(cb: CallbackQuery, bot: Bot):
    from models import Config
    cid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        c = await s.get(Config, cid)
        if not row or not c or c.bot_id != row.id:
            await cb.answer("یافت نشد", show_alert=True)
            return
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not u or c.owner_user_id != u.id:
            await cb.answer("این کانفیگ متعلق به شما نیست.", show_alert=True)
            return
        payload, vol, days, category = c.payload, c.volume_gb, c.days, c.category
    await cb.answer()
    from tenant import _is_sub_link
    is_sub = _is_sub_link(payload)
    body = (f"📦 <b>کانفیگ شما</b>\n📦 حجم: {fmt_vol(vol)} | 📅 مدت: {days} روز\n\n"
            + (f"🔗 ساب‌لینک:\n<code>{payload}</code>" if is_sub
               else f"<code>{payload}</code>"))
    kb_rows = [[
        InlineKeyboardButton(text="📦 ارسال سرورهای تکی",
                             callback_data=f"cfgdl:single:{cid}", style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="🔗 ارسال ساب‌لینک",
                             callback_data=f"cfgdl:sub:{cid}", style=ButtonStyle.PRIMARY),
    ]]
    try:
        from tenant import inline_back_row, _cfg_guide_row
        _cfgsrc = "unlimited" if (vol or 0) == 0 else "manual"
        _gr = await _cfg_guide_row(row.id, _cfgsrc, category)
        if _gr:
            kb_rows.append(_gr)
        _br = await inline_back_row(row.id)
        if _br:
            kb_rows.append(_br)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        png = await _qr_png(row.id, payload)
        await bot.send_photo(cb.from_user.id, BufferedInputFile(png, filename="config_qr.png"),
                             caption=body, reply_markup=kb)
    except Exception:
        await bot.send_message(cb.from_user.id, body, reply_markup=kb)


# ─────────────── سرویس‌های من ───────────────
# === PATCH_RESELLER_SVC_BACK_V1 ===
# وقتی نماینده از «📱 کانفیگ‌های ساخته‌شده» وارد این صفحه می‌شود، دکمهٔ
# بازگشت باید به صفحهٔ نمایندگی برگردد نه منوی اصلی. این ست، همان ورودی‌ها
# را علامت می‌زند. حافظه‌ای است و با ری‌استارت پاک می‌شود؛ بدترین حالتش
# این است که بازگشت به منوی اصلی برود، یعنی همان رفتارِ قبلی.
_svc_from_reseller: set[tuple[int, int]] = set()


def mark_svc_from_reseller(bot_id: int, tg_id: int, on: bool = True) -> None:
    key = (bot_id, tg_id)
    if on:
        _svc_from_reseller.add(key)
    else:
        _svc_from_reseller.discard(key)


def _svc_back_row(bot_id: int, tg_id: int):
    """ردیفِ بازگشت: به پنلِ نمایندگی، یا منوی اصلی."""
    if (bot_id, tg_id) in _svc_from_reseller:
        return [InlineKeyboardButton(text="⬅️ بازگشت به پنل نمایندگی",
                                     callback_data="rsme:home",
                                     style=ButtonStyle.DANGER)]
    return None


# === PATCH_RESELLER_SEPARATE_CONFIGS_V1 ===
async def show_reseller_configs(bot: Bot, msg: Message, bot_id: int, tg_id: int):
    """فهرستِ کانفیگ‌هایی که نماینده از پنلِ نمایندگی ساخته.

    عمداً از همان صفحهٔ جزئیاتِ `vsvc:` استفاده می‌کند تا QR، ساب‌لینک،
    تمدید، توقف و حذف دو پیاده‌سازیِ جدا نداشته باشند؛ فقط *فهرست* جداست.
    """
    async with Session() as s:
        accts = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == tg_id,
            PanelAccount.reseller_made.is_(True))
            .order_by(PanelAccount.id.desc()))).all()
    back = [InlineKeyboardButton(text="⬅️ بازگشت به پنل نمایندگی",
                                 callback_data="rsme:home", style=ButtonStyle.DANGER)]
    if not accts:
        await msg.answer(
            "📱 <b>کانفیگ‌های ساخته‌شده</b>\n\n"
            "هنوز از پنل نمایندگی کانفیگی نساختی. از پنل تحت وب می‌توانی "
            "پلن آماده، فروش آنلاین یا تست بسازی؛ همه‌شان اینجا فهرست می‌شوند.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[back]))
        return
    rows = []
    for a in accts:
        mark = "🟢" if a.status == "active" else "🔴"
        title = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)
        rows.append([InlineKeyboardButton(text=f"{mark} {title}",
                                          callback_data=f"vsvc:{a.id}")])
    rows.append(back)
    await msg.answer(
        f"📱 <b>کانفیگ‌های ساخته‌شده</b> ({len(accts)})\n\n"
        "این‌ها کانفیگ‌هایی هستند که خودت از پنل نمایندگی ساخته‌ای. "
        "برای دیدن جزئیات، QR، تمدید یا حذف، روی هرکدام بزن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_my_services(bot: Bot, msg: Message, bot_id: int, tg_id: int):
    from models import Config
    from tenant import get_setting, set_setting
    # === PATCH_NO_DUPLICATE_LIST_MESSAGES_V1 ===
    # این تابع از دکمهٔ پایدارِ «📡 سرویس‌های من» صدا زده می‌شه — یعنی کاربر
    # می‌تونه هر چندبار که خواست، دوباره بزنه. قبلاً هر بار یه پیامِ کاملاً
    # تازه فرستاده می‌شد و پیامِ لیستِ قبلی همون‌جا تو چت می‌موند؛ با چندبار
    # زدن، چت پر می‌شد از چندین کپیِ همون لیست. الان قبل از فرستادنِ لیستِ
    # تازه، اگه پیامِ لیستِ قبلیِ همین کاربر هنوز موجود بود، حذفش می‌کنیم.
    prev_key = f"last_svc_list_msg:{tg_id}"
    try:
        prev_id = await get_setting(bot_id, prev_key, "")
        if prev_id:
            try:
                await bot.delete_message(msg.chat.id, int(prev_id))
            except Exception:
                logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")  # پیام از قبل حذف شده یا خیلی قدیمی بوده؛ مشکلی نیست
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (پاک‌کردنِ لیستِ قبلیِ سرویس‌ها)")
    async with Session() as s:
        # === PATCH_RESELLER_SEPARATE_CONFIGS_V1 ===
        # ساخته‌هایِ پنلِ نمایندگی اینجا نمی‌آیند؛ جایِ آن‌ها «پنل نمایندگی
        # من → کانفیگ‌های ساخته‌شده» است تا با سرویس‌های شخصی قاطی نشوند.
        accts = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == tg_id,
            PanelAccount.reseller_made.is_not(True),
            # === PATCH_VPN_DELETE_HIDE_ONLY_V1 ===
            PanelAccount.status.notin_(("hidden", "refunded", "refunding")))   # PATCH_VPN_REFUND_V1
            .order_by(PanelAccount.id.desc()))).all()
        items = [(a.id, _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb), a.status) for a in accts]
        # کانفیگ‌های خریداری‌شده از انبار دستی را هم نشان بده
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        manual = []
        if u:
            cfgs = (await s.scalars(select(Config).where(
                Config.bot_id == bot_id, Config.owner_user_id == u.id,
                Config.status == "sold").order_by(Config.id.desc()))).all()
            manual = [(c.id, c.volume_gb, c.days, c.code) for c in cfgs]
    if not items and not manual:
        # === PATCH_RESELLER_SVC_BACK_V1 ===
        _br0 = _svc_back_row(bot_id, tg_id)
        if _br0:
            _kb0 = InlineKeyboardMarkup(inline_keyboard=[_br0])
        else:
            from tenant import inline_back_kb
            _kb0 = await inline_back_kb(bot_id)
        sent = await msg.answer("📡 هنوز سرویس یا کانفیگی نساختی.",
                                reply_markup=_kb0)
        try:
            await set_setting(bot_id, prev_key, str(sent.message_id))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (ذخیرهٔ آیدیِ پیامِ لیستِ سرویس‌ها)")
        return
    rows = []
    for aid, title, status in items:
        mark = "🟢" if status == "active" else "🔴"
        rows.append([InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"vsvc:{aid}")])
    for cid, vol, days, code in manual:
        rows.append([InlineKeyboardButton(
            text=f"📦 کانفیگ {fmt_vol(vol)}/{days}روز (کد {code})", callback_data=f"mcfg:{cid}", style=ButtonStyle.PRIMARY)])
    from tenant import inline_back_row
    _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    sent = await msg.answer(  # === PATCH_VPN_EDITABLE_TITLES_V1 ===
        f"📡 <b>{await _get_page_title(bot_id, 'my_svcs')}</b>\n\nبرای دیدن جزئیات، انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    try:
        await set_setting(bot_id, prev_key, str(sent.message_id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (ذخیرهٔ آیدیِ پیامِ لیستِ سرویس‌ها)")


# === PATCH_RENEW_SERVICE_PICKER_V1 ===
# درخواست Rocket: دکمهٔ صفحهٔ اصلی «♻️ تمدید سرویس» که مشتری را مستقیم به تمدید همان سرویسی ببرد که خریده؛
# بدون اینکه اول «سرویس‌های من» ← سرویس ← تمدید را بزند. هر سرویس قابل‌تمدید یک دکمه است و به همان مسیر
# تمدید موجود (vren:) می‌رود؛ پس قیمت، پلن‌ها، تخفیف نماینده و پرداخت دقیقا مثل قبل است.
async def renewable_services(bot_id: int, tg_id: int) -> list[tuple[int, str, str]]:
    """سرویس‌های پنلی کاربر که ادمین تمدیدشان را روشن کرده و پنلشان فعال است: [(id, عنوان، وضعیت)]."""
    out: list[tuple[int, str, str]] = []
    async with Session() as s:
        accts = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == tg_id,
            PanelAccount.reseller_made.is_not(True),
            PanelAccount.status.notin_(("hidden", "refunded", "refunding")))
            .order_by(PanelAccount.id.desc()))).all()
        for a in accts:
            panel = await _panel_for_account(s, bot_id, a)
            if not panel or not panel.enabled:
                continue
            if (await _vc(bot_id, "renew_enabled", panel.panel_key)) != "1":
                continue
            out.append((a.id, _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb), a.status or "active"))
    return out


async def show_renew_services(bot: Bot, msg: Message, bot_id: int, tg_id: int):
    items = await renewable_services(bot_id, tg_id)
    from tenant import inline_back_row
    back = await inline_back_row(bot_id)
    if not items:
        await msg.answer("♻️ سرویسی که امکان تمدید داشته باشد پیدا نشد.\n\n"
                         "اگر سرویس خریده‌ای، از «📡 سرویس‌های من» وضعیتش را ببین؛ یا از «خرید سرویس جدید» سرویس تازه بگیر.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[back] if back else []))
        return
    rows = [[InlineKeyboardButton(text=f"{'🟢' if st == 'active' else '🔴'} ♻️ {title}", callback_data=f"vren:{aid}")]
            for aid, title, st in items]
    if back:
        rows.append(back)
    await msg.answer("♻️ <b>تمدید سرویس</b>\n\nسرویسی را که می‌خواهی تمدید کنی انتخاب کن:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@vpn_user_router.callback_query(F.data == "vsvc:home")
async def vpn_service_home(cb: CallbackQuery, bot: Bot):
    """بازگشت به منوی اصلی ربات."""
    from tenant import main_kb_or_remove, main_inline_kb, get_setting
    from aiogram.types import ReplyKeyboardRemove
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if row:
        menu_mode = await get_setting(row.id, "menu_mode", "reply")
        if menu_mode == "inline":
            ikb = await main_inline_kb(row.id, row.owner_id, cb.from_user.id)
            await bot.send_message(cb.from_user.id, "🏠 منوی اصلی:", reply_markup=ikb)
        else:
            kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
            await bot.send_message(cb.from_user.id, "🏠 منوی اصلی:",
                                   reply_markup=kb or ReplyKeyboardRemove())
    await cb.answer()


@vpn_user_router.callback_query(F.data.startswith("vsvc:"))
async def vpn_service(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        panel = await _panel_for_account(s, row.id, a)
        email, sub_link = a.email, a.sub_link
        title = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)
        bought = _jalali(a.created_at)
        fallback_expiry = a.expires_at
        # === PATCH_SVC_DISABLE_MARKER_USER ===
        acc_status = (a.status or "active")
        _rf_paid = int(a.paid_toman or 0)   # PATCH_VPN_REFUND_V1
        _rf_reseller = bool(a.reseller_made)
        svc_code = await ensure_svc_code(s, a)
        web_url = await _web_info_url(s, a)
        await s.commit()
    await cb.answer("در حال دریافت مصرف…")
    usage_blk = _usage_block(None, fallback_expiry)
    if panel:
        client = await _panel_client(panel)
        try:
            usage = await client.get_usage(email)
            usage_blk = _usage_block(usage, fallback_expiry)
        except Exception as _ue:
            # PATCH_LOG_THROTTLE_PERSIST_V1 — پنلِ قطع/اکانتِ ادمینِ غیرفعال (PanelError) باگِ ما نیست:
            # یک WARNINGِ یک‌خطی، هر پنل حداکثر ساعتی یک‌بار؛ فقط خطایِ غیرمنتظره traceback می‌گیرد.
            from tenant.shared.panel_clients.vpn_panel import PanelError as _PE
            if isinstance(_ue, _PE):
                from log_throttle import should_log
                if should_log(f"vpnusage:{getattr(panel, 'id', panel)}", 3600):
                    logger.warning("vpn_service: مصرفِ سرویس از پنل گرفته نشد: %s",
                                   (str(_ue) or type(_ue).__name__).splitlines()[0][:200])
            else:
                logger.exception("vpn_service: خواندنِ مصرف از پنل ناموفق بود")
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # بررسی فعال‌بودن دکمه‌های تمدید
    async with Session() as s:
        row2 = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        _bid = row2.id if row2 else 0
    # === PATCH_RENEW_PANEL_INSTANCE_GATE_V1 ===
    # باگ: اینجا panel_key داده نمی‌شد، پس همیشه تنظیمِ سراسری/پیش‌فرضِ
    # renew_enabled خوانده می‌شد (پیش‌فرض «۱» = روشن) — مستقل از این‌که
    # ادمین برایِ همین پنلِ خاص (مثلاً پنلِ دومِ پاسارگاد، «pasargad#2»)
    # چه چیزی تنظیم کرده بود. نتیجه: خاموش‌کردنِ «تمدید/افزایشِ حجم» برایِ
    # پنلِ دوم یا سوم هیچ اثری در صفحهٔ سرویسِ مشتری نداشت.
    renew_on = (await _vc(_bid, "renew_enabled", panel.panel_key if panel else "")) == "1"
    # === PATCH_VPN_REFUND_V1 ===
    from tenant import get_setting as _gs_rf
    _refund_btn = bool(_rf_paid > 0 and not _rf_reseller and acc_status in ("active", "disabled")
                       and await _gs_rf(_bid, "refund_enabled", "0") == "1")
    from tenant import ibtn_style as _ibs
    # === PATCH_SVC_KEYBOARD_PAIRS_V1 ===
    # گزارشِ WHALE SHOP: «دکمه‌ها را مرتب کن» — ۱۳ دکمه زیرِ هم یک ستونِ خیلی بلند می‌ساخت. حالا دکمه‌هایِ کوتاه دوتا-دوتا
    # کنارِ هم می‌نشینند؛ فقط دو دکمهٔ طولانیِ تمدید تمام‌عرض می‌مانند تا معنایشان گم نشود. ترتیب و callback ها همان است.
    kb_rows = [
        [InlineKeyboardButton(text="🔗 ارسال ساب لینک", callback_data=f"vget:sub:{aid}",
                              style=await _ibs(_bid, "ibtn:sub_link")),
         InlineKeyboardButton(text="📦 کانفیگ‌های تکی", callback_data=f"vget:single:{aid}",
                              style=await _ibs(_bid, "ibtn:single_cfg"))],
    ]
    if renew_on:
        kb_rows += [
# === PATCH_RENEW_CLARITY_V1 ===
            # نام‌هایِ قبلی («تمدید / افزایش حجم» و «افزایش حجم (بدون
            # تمدید)») از دیدِ کاربر تفاوتشان معلوم نبود و هر دو شبیهِ هم
            # می‌خواندند. حالا صریح می‌گوییم هرکدام چه چیزی را زیاد می‌کند.
            [InlineKeyboardButton(text="♻️ تمدید مجدد سرویس (زمان + حجم)",
                                  callback_data=f"vren:{aid}",
                                  style=await _ibs(_bid, "ibtn:renew") or ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="📈 فقط افزایش حجم (بدون تغییر زمان)",
                                  callback_data=f"vadd:{aid}",
                                  style=await _ibs(_bid, "ibtn:add_volume"))],
        ]
    # === PATCH_SVC_PAGE_TIDY_V1 === (ایلیا) وایرگارد فقط اگر مالک برایِ این پنل روشن گذاشته؛ «نمایش اطلاعاتِ کانفیگ در وب»
    # (همان چیزی که لینکِ ساب نشان می‌دهد) و «تغییر نام» از صفحهٔ سرویس برداشته شد (هندلرها سرِ جایشان‌اند).
    _small = []
    if (await _vc(_bid, "wg_cfg_btn", panel.panel_key if panel else "")) == "1":
        _small.append(InlineKeyboardButton(text="🛡 ارسال کانفیگ تکی وایرگارد",
                                           callback_data=f"vwgcfg:{aid}",
                                           style=await _ibs(_bid, "ibtn:wg_cfg")))
    if sub_link:
        _small.append(InlineKeyboardButton(text="📱 نمایش QR", callback_data=f"vsvcqr:{aid}"))
    _small += [
        InlineKeyboardButton(text="🔄 تغییر لینک اتصال",
                             callback_data=f"vreset:{aid}",
                             style=await _ibs(_bid, "ibtn:reset_link")),
        InlineKeyboardButton(
            text=("✅ فعال‌سازی سرویس" if acc_status == "disabled" else "⛔️ غیرفعال کردن سرویس"),
            callback_data=(f"vsvcon:{aid}" if acc_status == "disabled" else f"vsvcoff:{aid}"),
            style=(await _ibs(_bid, "ibtn:toggle_svc")
                   or (ButtonStyle.SUCCESS if acc_status == "disabled" else ButtonStyle.DANGER))),
        InlineKeyboardButton(text="🗑 حذف کانفیگ",
                             callback_data=f"vdel:{aid}",
                             style=await _ibs(_bid, "ibtn:del_cfg")),
    ]
    if _refund_btn:
        _small.append(InlineKeyboardButton(text="💸 برگشت وجه", callback_data=f"vrf:{aid}",
                                           style=ButtonStyle.DANGER))
    _small.append(InlineKeyboardButton(text="🧩 آموزش اتصال", callback_data="vhelp:menu",
                                       style=await _ibs(_bid, "ibtn:help_conn")))
    kb_rows += [_small[i:i + 2] for i in range(0, len(_small), 2)]
    # === PATCH_METERED_PLAN_V1 === خطِ توضیحِ بستهٔ اعتباری در صفحهٔ سرویس
    _mtr_line = ""
    try:
        from models import MeteredAccount as _MA
        async with Session() as _s_m:
            _ma = await _s_m.scalar(select(_MA).where(_MA.account_id == aid, _MA.bot_id == _bid))
        if _ma and _ma.status != "ended":
            _mtr_line = (f"\n💳 <b>بستهٔ اعتباری</b>: {int(_ma.price_per_gb):,} تومان/گیگ — کسرشده تاکنون: "
                         f"<b>{int(_ma.billed_toman or 0):,}</b> تومان"
                         + (" — ⛔️ موجودی تمام، با شارژِ کیف پول وصل می‌شود" if _ma.status == "no_credit" else "") + "\n")
    except Exception:
        logger.exception("mtr: خطِ بستهٔ اعتباری ساخته نشد")
    kb_rows.append(
        # === PATCH_RESELLER_SVC_BACK_V1 ===
        # برایِ نماینده، «خانه» یعنی پنلِ نمایندگی‌اش نه منوی اصلیِ ربات.
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vsvcback",
                              style=await _ibs(_bid, "ibtn:back")),
         InlineKeyboardButton(text="🏷 پنل نمایندگی", callback_data="rsme:home",
                              style=await _ibs(_bid, "ibtn:home"))]
        if (_bid, cb.from_user.id) in _svc_from_reseller else
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vsvcback",
                              style=await _ibs(_bid, "ibtn:back")),
         InlineKeyboardButton(text="🏠 خانه", callback_data="vsvc:home",
                              style=await _ibs(_bid, "ibtn:home"))])
    text = (
        f"📦 <b>مشخصات کانفیگ</b>\n\n"
        f"🛰 سرویس: <b>{title}</b>\n"
        f"🗂 کد سرویس: <code>{svc_code}</code>\n"
        f"👤 نام کاربری پنل: <code>{email}</code>\n"
        f"📅 تاریخ خرید: {bought}\n"
        f"{_mtr_line}"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{usage_blk}"
    )
    if sub_link:
        text += f"\n┈┅━━⊷⊰✨⊱⊶━━┅┈\n🔗 <b>ساب‌لینک:</b>\n<code>{sub_link}</code>"

    # اگه ساب لینک داشت: QR با caption و دکمه (یه پیام زیبا)
    if sub_link:
        try:
            png = await _qr_png(row.id, sub_link)
            kb  = InlineKeyboardMarkup(inline_keyboard=kb_rows)
            if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
                # پیام قبلی هم عکس بود — فقط caption و دکمه رو آپدیت کن
                try:
                    await cb.message.edit_caption(caption=text, reply_markup=kb)
                    return
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            # پیام قبلی متنی بود — عکس جدید بفرست و متن قدیمی رو حذف کن
            try:
                # ارسال QR نباید کل callback را در retryهای طولانی نگه دارد؛
                # در خطای شبکه، مسیر متن و دکمه‌ها همچنان قابل‌استفاده می‌ماند.
                await asyncio.wait_for(
                    bot.send_photo(
                        cb.from_user.id,
                        BufferedInputFile(png, filename="vpn_qr.png"),
                        caption=text,
                        reply_markup=kb),
                    timeout=8.0
                )
            except asyncio.TimeoutError:
                logger.warning('ارسال QR به‌دلیل timeout شبکه متوقف شد؛ مسیر متنی ادامه یافت')
            try:
                await cb.message.delete()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # بدون ساب لینک — پیام متنی
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await bot.send_message(
            cb.from_user.id, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@vpn_user_router.callback_query(F.data.startswith("vsvcqr:"))
async def vpn_service_qr(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        sub_link = a.sub_link
    if not sub_link:
        await cb.answer("لینکی موجود نیست.", show_alert=True)
        return
    # === PATCH_QR_TOO_LONG_IS_NOT_AN_ERROR_V1 ===
    # تنها نقطه‌ای که نتیجهٔ QR بدونِ محافظ استفاده می‌شد: اگر ساختِ QR
    # ناموفق می‌شد، send_photo می‌ترکید و کاربر *هیچ‌چیز* نمی‌گرفت — نه
    # بارکد نه لینک. بقیهٔ نقاط از قبل fallbackِ متنی داشتند.
    png = await _qr_png(row.id, sub_link)
    if png:
        await bot.send_photo(cb.from_user.id, BufferedInputFile(png, filename="vpn_qr.png"),
                             caption=f"🔗 <code>{sub_link}</code>")
    else:
        await bot.send_message(cb.from_user.id, f"🔗 <code>{sub_link}</code>")
    await cb.answer()


# ─────────────── غیرفعال/فعال‌سازی سریع سرویس (بدون حذف) ───────────────
@vpn_user_router.callback_query(F.data.startswith("vsvcoff:"))
async def vpn_service_disable_confirm(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
    await _safe_edit(
        cb,
        "⛔️ <b>غیرفعال کردن سرویس</b>\n\n"
        "با تأیید، این سرویس همین الان روی پنل قطع می‌شود و کاربر دیگر "
        "نمی‌تواند از آن وصل شود (کانفیگ حذف نمی‌شود و بعداً می‌توانی دوباره "
        "فعالش کنی).\n\nآیا مطمئنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔️ بله، غیرفعال کن", callback_data=f"vsvcoffok:{aid}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"vsvc:{aid}")],
        ]))
    await cb.answer()


@vpn_user_router.callback_query(F.data.startswith("vsvcoffok:"))
async def vpn_service_disable_ok(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        email = a.email
        panel = await _panel_for_account(s, row.id, a)
    if not panel:
        await cb.answer("پنل این سرویس پیدا نشد.", show_alert=True)
        return
    await cb.answer("در حال غیرفعال‌سازی…")
    try:
        client = _client_from_panel(panel)
        try:
            await client.set_enabled(email, False)
        finally:
            await client.close()
    except Exception as e:
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, row.id, "غیرفعال‌سازیِ سرویس", e, cb.from_user.id)
        await cb.message.answer("❌ غیرفعال‌سازی انجام نشد.\n" + USER_FAIL_TEXT)
        return
    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            a2.status = "disabled"
            await s.commit()
    await cb.message.answer("⛔️ سرویس غیرفعال شد؛ کاربر دیگر نمی‌تواند به آن وصل شود.")


@vpn_user_router.callback_query(F.data.startswith("vsvcon:"))
async def vpn_service_enable(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        email = a.email
        panel = await _panel_for_account(s, row.id, a)
    if not panel:
        await cb.answer("پنل این سرویس پیدا نشد.", show_alert=True)
        return
    await cb.answer("در حال فعال‌سازی…")
    try:
        client = _client_from_panel(panel)
        try:
            await client.set_enabled(email, True)
        finally:
            await client.close()
    except Exception as e:
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, row.id, "فعال‌سازیِ سرویس", e, cb.from_user.id)
        await cb.message.answer("❌ فعال‌سازی انجام نشد.\n" + USER_FAIL_TEXT)
        return
    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            a2.status = "active"
            await s.commit()
    await cb.message.answer("✅ سرویس دوباره فعال شد.")


@vpn_user_router.callback_query(F.data == "vsvcback")
async def vpn_service_back(cb: CallbackQuery, bot: Bot):
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        bot_id = row.id if row else 0
    if bot_id:
        # === PATCH_RESELLER_SEPARATE_CONFIGS_V1 ===
        # اگر از پنلِ نمایندگی آمده، به همان فهرست برگردد نه سرویس‌های شخصی.
        # === PATCH_VPN_EDIT_IN_PLACE_V1 === ویرایشِ همان پیام به‌جایِ حذف+ارسال
        if (bot_id, cb.from_user.id) in _svc_from_reseller:
            await show_reseller_configs(bot, _EditFirst(cb.message, cb.from_user), bot_id, cb.from_user.id)
        else:
            await show_my_services(bot, _EditFirst(cb.message, cb.from_user), bot_id, cb.from_user.id)
    await cb.answer()


# ─────────────── حذف کانفیگ ───────────────
# ─────────────── برگشت وجه — PATCH_VPN_REFUND_V1 ───────────────
# مبلغِ برگشتی = پرداختیِ همین سرویس × سهمِ باقی‌مانده (حجم؛ و اگر ادمین «احتساب روز» را
# روشن کرده، کمینهٔ حجم و روز) × (۱ − کارمزد). فقط برایِ سرویسِ پولی (paid_toman پر باشد):
# تستِ رایگان/هدیه/امتیاز/نماینده مشمول نیستند. سرویس بعد از برگشت از پنل حذف می‌شود.
async def _refund_quote(bot_id: int, a, usage: dict | None) -> dict:
    from tenant import get_setting as _gs
    import time as _t
    paid = int(a.paid_toman or 0)
    try:
        fee = max(0.0, min(100.0, float(await _gs(bot_id, "refund_fee_pct", "10") or 10)))
    except ValueError:
        fee = 10.0
    days_aware = (await _gs(bot_id, "refund_days_aware", "0")) == "1"
    usage = usage or {}
    total = int(usage.get("total") or 0)
    used = int(usage.get("up") or 0) + int(usage.get("down") or 0)
    vol_frac = None
    if total > 0:
        vol_frac = max(0.0, (total - used) / total)
    day_frac = None
    exp_ms = int(usage.get("expiryTime") or 0)
    exp_ts = exp_ms / 1000 if exp_ms > 0 else (a.expires_at.timestamp() if a.expires_at else 0)
    if exp_ts > 0 and int(a.days or 0) > 0:
        day_frac = max(0.0, min(1.0, (exp_ts - _t.time()) / (int(a.days) * 86400)))
    fracs = [f for f in ((vol_frac,) + ((day_frac,) if (days_aware or vol_frac is None) else ())) if f is not None]
    frac = min(fracs) if fracs else 0.0
    gross = int(paid * frac)
    fee_amt = int(gross * fee / 100)
    return {"paid": paid, "fee": fee, "frac": frac, "vol_frac": vol_frac, "day_frac": day_frac,
            "gross": gross, "fee_amt": fee_amt, "net": max(0, gross - fee_amt), "days_aware": days_aware}


async def _refund_load(cb: CallbackQuery, bot: Bot, aid: int):
    """(row, account, panel) اگر کاربر مالکِ سرویس و برگشت‌وجه برایش مجاز باشد؛ وگرنه None + پیامِ خطا."""
    from tenant import get_setting as _gs
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return None
        if await _gs(row.id, "refund_enabled", "0") != "1":
            await cb.answer("برگشت وجه فعال نیست.", show_alert=True)
            return None
        if a.reseller_made or int(a.paid_toman or 0) <= 0 or (a.status or "active") not in ("active", "disabled"):
            await cb.answer("این سرویس مشمولِ برگشت وجه نیست (تست رایگان، هدیه یا قبلاً برگشت‌خورده).",
                            show_alert=True)
            return None
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.panel_key == (a.panel_key or "sanai")))
    return row, a, panel


def _refund_breakdown_lines(q: dict, paid: int) -> list[str]:
    """خطوطِ محاسبهٔ شفاف: درصدِ مصرف‌شده، کارمزد و کلِ مبلغِ تسویه (برایِ کاربر و ادمین)."""
    L = [f"💰 مبلغِ پرداختی: {paid:,} تومان"]
    if q["vol_frac"] is not None:
        L.append(f"📦 حجم: <b>{(1 - q['vol_frac']) * 100:.0f}٪ مصرف‌شده</b> — {q['vol_frac'] * 100:.0f}٪ باقی‌مانده")
    if q["day_frac"] is not None and (q["days_aware"] or q["vol_frac"] is None):
        L.append(f"📅 زمان: <b>{(1 - q['day_frac']) * 100:.0f}٪ گذشته</b> — {q['day_frac'] * 100:.0f}٪ باقی‌مانده")
    L.append(f"🧮 سهمِ قابل‌برگشت: {q['frac'] * 100:.0f}٪ × {paid:,} = {q['gross']:,} تومان")
    L.append(f"➖ کارمزد ({q['fee']:g}٪): {q['fee_amt']:,} تومان")
    L.append(f"✅ <b>کلِ مبلغِ تسویه: {q['net']:,} تومان</b>")
    return L


class _VpnRefundReasonFSM(StatesGroup):
    reason = State()


async def _refund_usage_quote(row, a, panel):
    """(usage, quote) با خواندنِ مصرفِ لحظه‌ای از پنل؛ اگر پنل جواب نداد (None, None)."""
    if not panel:
        return None, None
    usage = None
    client = await _panel_client(panel)
    try:
        usage = await client.get_usage(a.email)
    except Exception:
        logger.exception("برگشت وجه: خواندنِ مصرف از پنل ناموفق بود")
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("بستنِ کلاینتِ پنل")
    if not usage:
        return None, None
    return usage, await _refund_quote(row.id, a, usage)


async def _refund_has_open_request(bot_id: int, aid: int) -> bool:
    async with Session() as s:
        rid = await s.scalar(select(VpnRefundRequest.id).where(
            VpnRefundRequest.bot_id == bot_id, VpnRefundRequest.account_id == aid,
            VpnRefundRequest.status.in_(("pending", "processing"))))
    return rid is not None


# === PATCH_VPN_REFUND_APPROVAL_V1 ===
# برگشت وجه دیگر خودکار نیست (سوءاستفاده): کاربر دلیلش را می‌نویسد، درخواست برایِ ادمین می‌رود و
# فقط با تأییدِ ادمین مبلغ به کیف پول برمی‌گردد و سرویس از پنل حذف می‌شود.
@vpn_user_router.callback_query(F.data.regexp(r"^vrf:\d+$"))
async def vpn_refund_ask(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    loaded = await _refund_load(cb, bot, aid)
    if not loaded:
        return
    row, a, panel = loaded
    if await _refund_has_open_request(row.id, aid):
        await cb.answer("درخواستِ برگشت وجهِ این سرویس قبلاً ثبت شده و در انتظارِ بررسیِ ادمین است.",
                        show_alert=True)
        return
    _usage, q = await _refund_usage_quote(row, a, panel)
    if q is not None and q["net"] <= 0:
        await cb.answer("حجم/زمانِ باقی‌مانده‌ای نیست؛ مبلغی برای برگشت وجود ندارد.", show_alert=True)
        return
    lines = ["💸 <b>درخواستِ برگشت وجه</b>", "",
             f"🏷 سرویس: <b>{_svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)}</b>"]
    if q is not None:
        lines += _refund_breakdown_lines(q, int(a.paid_toman or 0))
    else:
        lines.append(f"💰 مبلغِ پرداختی: {int(a.paid_toman or 0):,} تومان")
    lines += ["",
              "ℹ️ برگشت وجه <b>خودکار نیست</b>. دلیلِ درخواستت را می‌نویسی، ادمین بررسی می‌کند و "
              "اگر تأیید کند مبلغ به کیف پولت برمی‌گردد و سرویس از پنل حذف می‌شود.",
              "مبلغِ نهایی را ادمین بر اساسِ مصرفِ لحظهٔ تأیید حساب می‌کند."]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ثبتِ درخواست", callback_data=f"vrf:req:{aid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"vsvc:{aid}", style=ButtonStyle.PRIMARY)]])
    await _safe_edit(cb, "\n".join(lines), reply_markup=kb)
    await cb.answer()


@vpn_user_router.callback_query(F.data.regexp(r"^vrf:req:\d+$"))
async def vpn_refund_req_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    loaded = await _refund_load(cb, bot, aid)
    if not loaded:
        return
    row, _a, _panel = loaded
    if await _refund_has_open_request(row.id, aid):
        await cb.answer("درخواستِ برگشت وجهِ این سرویس قبلاً ثبت شده.", show_alert=True)
        return
    await state.set_state(_VpnRefundReasonFSM.reason)
    await state.update_data(vrf_aid=aid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"vrf:cancel:{aid}", style=ButtonStyle.DANGER)]])
    await _safe_edit(cb, "📝 <b>دلیلِ درخواستِ برگشت وجه</b> را بنویس و بفرست.\n\n"
                         "مشکلِ سرویس چیست؟ (مثلاً: وصل نمی‌شود، سرعت پایین است، اشتباهی خریده‌ام، ...)\n"
                         "هرچه کامل‌تر بنویسی، بررسیِ ادمین سریع‌تر و دقیق‌تر می‌شود.", reply_markup=kb)
    await cb.answer()


@vpn_user_router.callback_query(F.data.regexp(r"^vrf:cancel:\d+$"))
async def vpn_refund_req_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    aid = int(cb.data.split(":")[2])
    await cb.answer("لغو شد")
    fake = type("FCB", (), {"data": f"vsvc:{aid}", "from_user": cb.from_user,
                             "message": cb.message, "answer": cb.answer})()
    await vpn_service(fake, bot)


@vpn_user_router.message(_VpnRefundReasonFSM.reason)
async def vpn_refund_req_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    aid = data.get("vrf_aid")
    reason = (msg.text or "").strip()
    if len(reason) < 5:
        await msg.answer("لطفاً دلیل را کمی کامل‌تر بنویس (حداقل ۵ حرف) یا «انصراف» را بزن.")
        return
    reason = reason[:500]
    from tenant import get_setting as _gs
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid) if aid else None
        if (not row or not a or a.bot_id != row.id or a.user_telegram_id != msg.from_user.id
                or a.reseller_made or int(a.paid_toman or 0) <= 0
                or (a.status or "active") not in ("active", "disabled")
                or await _gs(row.id, "refund_enabled", "0") != "1"):
            await state.clear()
            await msg.answer("این سرویس مشمولِ برگشت وجه نیست یا قبلاً رسیدگی شده.")
            return
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.panel_key == (a.panel_key or "sanai")))
    if await _refund_has_open_request(row.id, a.id):
        await state.clear()
        await msg.answer("درخواستِ برگشت وجهِ این سرویس قبلاً ثبت شده و در انتظارِ بررسی است.")
        return
    _usage, q = await _refund_usage_quote(row, a, panel)
    async with Session() as s:
        req = VpnRefundRequest(bot_id=row.id, account_id=a.id, user_telegram_id=msg.from_user.id,
                               reason=reason, quoted_net=int(q["net"]) if q else 0, status="pending")
        s.add(req)
        await s.commit()
        rid = req.id
    await state.clear()
    uname = (f"@{msg.from_user.username}" if msg.from_user.username
             else f"<code>{msg.from_user.id}</code>")
    lines = ["💸 <b>درخواستِ برگشت وجهِ سرویس VPN</b>", "",
             f"👤 کاربر: {uname} ({html.escape(msg.from_user.full_name or '')})",
             f"🏷 سرویس: <b>{html.escape(_svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb))}</b>"
             f" — <code>{html.escape(str(a.email))}</code>"]
    if q is not None:
        lines += _refund_breakdown_lines(q, int(a.paid_toman or 0))
    else:
        lines.append(f"💰 مبلغِ پرداختی: {int(a.paid_toman or 0):,} تومان")
        lines.append("⚠️ مصرف از پنل خوانده نشد؛ مبلغ موقعِ تأیید محاسبه می‌شود.")
    lines += ["", "📝 <b>دلیلِ کاربر:</b>", html.escape(reason), "",
              "با «تأیید»، مبلغ به کیف پولِ کاربر برمی‌گردد و سرویس از پنل <b>حذف می‌شود</b>."]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید و برگشت وجه", callback_data=f"vrfa:ok:{rid}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"vrfa:no:{rid}", style=ButtonStyle.DANGER)]])
    from tenant import _notify_admins
    await _notify_admins(bot, row, "\n".join(lines), kb)
    await msg.answer("✅ درخواستِ برگشت وجهت ثبت شد و برای ادمین فرستاده شد.\n"
                     "بعد از بررسی نتیجه را همین‌جا به تو اطلاع می‌دهم.")


async def _refund_finish_admin_msg(cb: CallbackQuery, note: str):
    try:
        base = cb.message.html_text if getattr(cb.message, "html_text", None) else (cb.message.text or "")
        if getattr(cb.message, "caption", None) and not getattr(cb.message, "text", None):
            await cb.message.edit_caption(caption=(cb.message.html_caption or "") + "\n\n" + note,
                                          reply_markup=None, parse_mode="HTML")
        else:
            await cb.message.edit_text(base + "\n\n" + note, reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.exception("ویرایشِ پیامِ ادمینِ برگشت وجه")


@vpn_user_router.callback_query(F.data.regexp(r"^vrfa:(ok|no):\d+$"))
async def vpn_refund_decide(cb: CallbackQuery, bot: Bot):
    """تأیید/ردِ درخواستِ برگشت وجه توسطِ ادمین."""
    from tenant import is_owner_or_admin
    import datetime as _dtm
    _, act, rid_s = cb.data.split(":")
    rid = int(rid_s)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            await cb.answer()
            return
        if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
            await cb.answer("فقط ادمین‌ها.", show_alert=True)
            return
        req = await s.get(VpnRefundRequest, rid)
        if not req or req.bot_id != row.id:
            await cb.answer("درخواست پیدا نشد.", show_alert=True)
            return
        a = await s.get(PanelAccount, req.account_id)
        panel = (await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == row.id, VpnPanel.panel_key == (a.panel_key or "sanai")))) if a else None
    who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
    label = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb) if a else "سرویس"

    async def _set_req(frm: tuple, to: str) -> int:
        async with Session() as s2:
            r = await s2.execute(update(VpnRefundRequest).where(
                VpnRefundRequest.id == rid, VpnRefundRequest.status.in_(frm)).values(
                status=to, decided_at=_dtm.datetime.utcnow(), decided_by=cb.from_user.id))
            await s2.commit()
            return r.rowcount

    if act == "no":
        if not await _set_req(("pending",), "rejected"):
            await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
            return
        await cb.answer("رد شد")
        await _refund_finish_admin_msg(cb, f"❌ <b>رد شد</b> توسط {html.escape(who)}")
        try:
            await bot.send_message(req.user_telegram_id,
                                   f"❌ درخواستِ برگشت وجهِ سرویس «{label}» رد شد.\n"
                                   "اگر سؤالی داری از بخش پشتیبانی پیگیری کن.")
        except Exception:
            logger.exception("اطلاعِ ردِ برگشت وجه به کاربر")
        return

    # ── تأیید ──
    if not a or not panel:
        await cb.answer("سرویس یا پنلِ آن پیدا نشد؛ رد کن یا دستی رسیدگی کن.", show_alert=True)
        return
    if not await _set_req(("pending",), "processing"):
        await cb.answer("این درخواست قبلاً رسیدگی شده یا در حال پردازش است.", show_alert=True)
        return
    usage, q = await _refund_usage_quote(row, a, panel)
    if not usage or q is None:
        await _set_req(("processing",), "pending")
        await cb.answer("پنل پاسخ نداد؛ کمی بعد دوباره «تأیید» را بزن.", show_alert=True)
        return
    if q["net"] <= 0:
        await _set_req(("processing",), "pending")
        await cb.answer("حجم/زمانِ باقی‌مانده‌ای نیست؛ مبلغی برای برگشت نیست. اگر می‌خواهی «رد» کن.",
                        show_alert=True)
        return
    # قفلِ اتمیکِ سرویس: فقط یک بار
    async with Session() as s:
        res = await s.execute(update(PanelAccount).where(
            PanelAccount.id == a.id, PanelAccount.status.in_(("active", "disabled")))
            .values(status="refunding"))
        await s.commit()
    if not res.rowcount:
        await _set_req(("processing",), "rejected")
        await cb.answer("این سرویس دیگر قابلِ برگشت نیست (قبلاً رسیدگی شده).", show_alert=True)
        return
    client = await _panel_client(panel)
    try:
        await client.delete_account(a.email)
    except Exception:
        logger.exception("برگشت وجه: حذفِ سرویس از پنل ناموفق بود (aid=%s)", a.id)
        async with Session() as s:
            await s.execute(update(PanelAccount).where(PanelAccount.id == a.id).values(status=a.status or "active"))
            await s.commit()
        await _set_req(("processing",), "pending")
        await cb.answer("حذفِ سرویس از پنل ناموفق بود؛ چیزی برنگشت. بعداً دوباره تلاش کن.", show_alert=True)
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("بستنِ کلاینتِ پنل")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == req.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance) + q["net"]
            s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=q["net"], kind="refund",
                            note=f"برگشت وجه سرویس {a.email} (کارمزد {q['fee']:g}٪)"))
        await s.execute(update(PanelAccount).where(PanelAccount.id == a.id).values(status="refunded"))
        await s.execute(update(VpnRefundRequest).where(VpnRefundRequest.id == rid).values(
            status="approved", quoted_net=int(q["net"])))
        await s.commit()
    await cb.answer("✅ انجام شد")
    await _refund_finish_admin_msg(
        cb, f"✅ <b>تأیید شد</b> توسط {html.escape(who)} — "
            f"مصرف {(1 - q['frac']) * 100:.0f}٪، کارمزد {q['fee']:g}٪؛ "
            f"<b>{q['net']:,} تومان</b> به کیف پولِ کاربر برگشت.")
    try:
        await bot.send_message(req.user_telegram_id,
                               f"✅ درخواستِ برگشت وجهِ سرویس «{label}» تأیید شد.\n"
                               f"📊 مصرف: {(1 - q['frac']) * 100:.0f}٪ — کارمزد: {q['fee']:g}٪\n"
                               f"💰 <b>{q['net']:,} تومان</b> به کیف پولت اضافه شد. سرویس از پنل حذف شد.")
    except Exception:
        logger.exception("اطلاعِ تأییدِ برگشت وجه به کاربر")


@vpn_user_router.callback_query(F.data.startswith("vdel:ok:"))
async def vpn_del_ok(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
        bot_id = row.id
    # === PATCH_VPN_DELETE_HIDE_ONLY_V1 ===
    # گزارشِ ادمین (ایلیا): «حذف کانفیگ» فقط باید از بخشِ سرویس‌هایِ کاربر
    # حذفش کند، از خودِ پنل حذف نشود — قبلاً `client.delete_account(email)`
    # واقعاً اکانت را رویِ سرورِ ارائه‌دهنده پاک می‌کرد (برگشت‌ناپذیر) و
    # ردیفِ دیتابیس هم کاملاً حذف می‌شد. حالا فقط status روی «hidden»
    # می‌رود — خودِ سرویس رویِ پنل و دیتابیس دست‌نخورده می‌ماند، فقط از
    # لیستِ «📡 سرویس‌های من» (پایین‌تر در show_my_services) فیلتر می‌شود.
    async with Session() as s:
        a = await s.get(PanelAccount, aid)
        if a:
            a.status = "hidden"
            await s.commit()
    await cb.answer("کانفیگ از لیستِ تو پنهان شد")
    # پیامِ کانفیگ معمولاً عکسِ QR است و edit_text رویش «there is no text in
    # the message to edit» می‌داد (در error.log)؛ _progress_edit عکس/متن را
    # جدا می‌کند.
    await _progress_edit(
        cb,
        "✅ کانفیگ از لیستِ سرویس‌هایِ تو حذف شد.\n"
        "(خودِ سرویس هنوز فعال است؛ فقط دیگر در لیستِ تو نشان داده نمی‌شود.)")
    await show_my_services(bot, cb.message, bot_id, cb.from_user.id)


@vpn_user_router.callback_query(F.data.startswith("vdel:"))
async def vpn_del_confirm(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
    await _safe_edit(
        cb,
        # === PATCH_VPN_DELETE_HIDE_ONLY_V1 ===
        "⚠️ <b>حذف کانفیگ</b>\n\n"
        "این فقط کانفیگ را از لیستِ سرویس‌هایِ تو پنهان می‌کند — خودِ سرویس "
        "رویِ سرور دست‌نخورده و فعال می‌ماند. مطمئنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 بله، از لیستم پنهانش کن", callback_data=f"vdel:ok:{aid}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"vsvc:{aid}")],
        ]))
    await cb.answer()


# ─────────────── افزایش حجم (بدون تمدید) ───────────────
@vpn_user_router.callback_query(F.data.startswith("vadd:pay:"))
async def vpn_add_traffic_pay(cb: CallbackQuery, bot: Bot):
    # === PATCH_VPN_DOUBLE_BUY_GUARD_V1 ===
    _key = f"vaddpay:{bot.token}:{cb.from_user.id}:{cb.data}"
    async with _vpn_once(_key) as _ok:
        if not _ok:
            await cb.answer("⏳ درخواست قبلی‌ات هنوز در حال پردازش است، کمی صبر کن.",
                            show_alert=True)
            return
        # === PATCH_PAY_PHONE_GATE_FIX_V1 ===
        if await _pay_gate_block(cb, bot):
            return
        # === PATCH_PURCHASE_IDEMPOTENCY_V1 ===
        if not await _claim_or_reject(cb, bot, "vaddpay"):
            return
        await _vpn_add_traffic_pay_impl(cb, bot)


async def _vpn_add_traffic_pay_impl(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    aid, gb = int(parts[2]), int(parts[3])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
        panel = await _panel_for_account(s, row.id, a)
        _pk = panel.panel_key if panel else ""
        price_gb = int(await _vc(row.id, "renew_price_gb", _pk))
        # === PATCH_RENEW_DAYS_VALIDATE_V1 === فقط همان گزینه‌هایِ منویِ افزایشِ حجم
        _mn = int(await _vc(row.id, "renew_min_gb", _pk))
        _mx = int(await _vc(row.id, "renew_max_gb", _pk))
        _st = max(1, int(await _vc(row.id, "renew_step_gb", _pk)))
        _ok = {_mn + i * _st for i in range(8) if _mn + i * _st <= _mx}
        if gb <= 0 or gb not in _ok:
            await cb.answer("این گزینه معتبر نیست؛ دوباره از منو انتخاب کن.", show_alert=True)
            return
        price    = gb * price_gb
        # باگ‌گارد: اگه قیمت صفر باشه، اجازه نده
        if price <= 0:
            await cb.answer("⛔️ افزایش حجم در حال حاضر امکان‌پذیر نیست.", show_alert=True)
            return
        price = int(await _renew_partner_price(row.id, cb.from_user.id, price, _pk, "custom"))   # PATCH_RENEW_PARTNER_DISCOUNT_V1
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        if not u or float(u.balance) < price:
            await cb.answer(f"موجودی کافی نیست. نیاز: {price:,} تومان", show_alert=True)
            return
        u.balance -= price
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-price,
                        kind="purchase", note=f"افزایش حجم +{gb}G"))
        email = a.email
        await s.commit()
        bot_id = row.id
    await cb.answer("در حال افزایش حجم…")
    # === PATCH_SLOW_PANEL_PROGRESS_V1 ===
    # اندازه‌گیری نشان داد بعضی پنل‌هایِ مشتری‌ها ۱۰ تا ۱۳ ثانیه برایِ یک
    # عملیات طول می‌کشند (مثلاً eantcgsad828…: میانه ۹٫۹s رویِ همهٔ
    # endpointها). این کندی سمتِ سرورِ آن‌هاست و از این‌جا قابلِ رفع نیست،
    # ولی *حسِ* هنگ‌کردن قابلِ رفع است: cb.answer فقط یک toastِ کوچک است
    # که سریع محو می‌شود و صفحه بی‌تغییر می‌ماند. یک edit_text صریح یعنی
    # کاربر می‌بیند کار در جریان است و دوباره دکمه نمی‌زند.
    await _progress_edit(cb, "⏳ در حال افزایشِ حجم روی سرور… این کار ممکن است چند ثانیه طول بکشد.")
    try:
        client = _client_from_panel(panel)
        await client.add_traffic(email, gb)
        await client.close()
        async with Session() as s:
            a2 = await s.get(PanelAccount, aid)
            if a2:
                a2.volume_gb = (a2.volume_gb or 0) + gb
                a2.traffic_out_at = None   # PATCH_TRAFFIC_RENEW_GUARD_V1 — حجم اضافه شد ⇒ چرخهٔ حذفِ «اتمامِ حجم» لغو
                await s.commit()
        await _safe_edit(cb, f"✅ {gb} گیگ به سرویس شما اضافه شد!")
        # === PATCH_RENEW_PURCHASE_ALERT_V1 ===
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                _row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
                _buyer = await s.scalar(select(BotUser).where(
                    BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id))
            if _row:
                # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
                from tenant.bots.vpn.vpn_admin import panel_label
                _pname = await panel_label(bot_id, panel.panel_key) if panel else None
                await _purchase_alert(
                    bot, _row, buyer=_buyer,
                    section="افزایش حجم VPN",
                    detail=f"+{gb} گیگ", amount=price,
                    product_id=aid, panel_name=_pname)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ خریدِ افزایشِ حجمِ VPN)")
        from tenant.shared.extras import handle_referral_purchase
        _bg(handle_referral_purchase(bot_id, cb.from_user.id, bot), "پاداش دعوت")
    except Exception as e:
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id))
            if u:
                u.balance += price
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price,
                                kind="refund", note="برگشت وجه افزایش حجم ناموفق"))
                await s.commit()
        await _safe_edit(cb, f"❌ افزایش حجم ناموفق. وجه برگشت خورد.\n<code>{_esc_html(str(e))}</code>")


@vpn_user_router.callback_query(F.data.startswith("vadd:"))
async def vpn_add_traffic(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
        title = _svc_label(a.plan_title, a.volume_gb, a.days, a.volume_mb)
        panel = await _panel_for_account(s, row.id, a)
    # تنظیمات تمدید از ادمین (مخصوص همین پنل)
    bot_id   = row.id if row else 0
    pk       = panel.panel_key if panel else ""
    price_gb = int(await _vc(bot_id, "renew_price_gb", pk))
    mn_gb    = int(await _vc(bot_id, "renew_min_gb", pk))
    mx_gb    = int(await _vc(bot_id, "renew_max_gb", pk))
    st_gb    = int(await _vc(bot_id, "renew_step_gb", pk))
    # باگ‌گارد: اگه قیمت صفر باشه
    if price_gb <= 0:
        await cb.answer("⛔️ افزایش حجم در حال حاضر فعال نیست.", show_alert=True)
        return
    # ساخت گزینه‌ها بر اساس بازهٔ تنظیم‌شده
    options = []
    gb = mn_gb
    while gb <= mx_gb and len(options) < 8:
        options.append(gb)
        gb += st_gb
    rows = []
    for g in options:
        price = int(await _renew_partner_price(bot_id, cb.from_user.id, g * price_gb, pk, "custom"))   # PATCH_RENEW_PARTNER_DISCOUNT_V1
        rows.append([InlineKeyboardButton(
            text=f"📈 +{g} گیگ — {price:,} تومان",
            callback_data=f"vadd:pay:{aid}:{g}")])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data=f"vsvc:{aid}")])
    await _safe_edit(
        cb,
# === PATCH_RENEW_CLARITY_V1 ===
        f"📈 <b>فقط افزایش حجم</b>\n\n🛰 {title}\n"
        f"💵 هر گیگ: {price_gb:,} تومان\n\n"
        "ℹ️ این گزینه فقط <b>حجم</b> سرویس فعلی‌ات را زیاد می‌کند و "
        "<b>هیچ تأثیری روی تاریخ انقضا ندارد</b>.\n"
        "مناسبِ وقتی است که هنوز زمان داری ولی حجمت تمام شده.\n\n"
        "اگر می‌خواهی <b>هم زمان و هم حجم</b> اضافه شود، از دکمهٔ "
        "«♻️ تمدید مجدد سرویس» استفاده کن.\n\n"
        "یک مقدار انتخاب کن (از کیف پول کسر می‌شود):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# ─────────────── تغییر لینک اتصال (reset sub) ───────────────
@vpn_user_router.callback_query(F.data.startswith("vreset:ok:"))
async def vpn_reset_link_ok(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
        email = a.email
        panel = await _panel_for_account(s, row.id, a)
    await cb.answer("در حال تغییر لینک…")
    try:
        client      = _client_from_panel(panel)
        # === PATCH_RESET_SUB_ROTATE_UUID_V1 ===
        # خروجی حالا سه‌تایی است: لینکِ جدید، uuid جدید (یا None) و subId جدید.
        _res = await client.reset_sub(email)
        if isinstance(_res, tuple):
            new_link, new_uuid, new_sub_id = _res
        else:  # پنل‌هایی که هنوز فقط لینک برمی‌گردانند
            new_link, new_uuid, new_sub_id = _res, None, None
        await client.close()
        # ذخیرهٔ لینک/شناسه‌های جدید
        async with Session() as s:
            a = await s.get(PanelAccount, aid)
            if a:
                a.sub_link = new_link
                if new_uuid:
                    a.uuid = new_uuid
                if new_sub_id:
                    a.sub_id = new_sub_id
                await s.commit()
        _txt = (f"✅ لینک اتصال تغییر کرد.\n\n🔗 <b>لینک جدید:</b>\n<code>{new_link}</code>\n\n"
                "کلاینت‌های متصل باید ساب لینک را دوباره آپدیت کنند.")
        if new_uuid:
            _txt += "\n\n🔒 کانفیگ‌های قبلی از کار افتادند."
        else:
            _txt += ("\n\n⚠️ پنل اجازهٔ تعویضِ شناسهٔ کاربر را نداد، پس کانفیگ‌هایی "
                     "که قبلاً در برنامه ذخیره شده‌اند ممکن است هنوز وصل شوند.")
        try:
            await cb.message.edit_text(_txt)
        except Exception:
            await cb.message.answer(_txt)
    except Exception as e:
        await cb.message.answer(f"❌ تغییر لینک ناموفق.\n<code>{_esc_html(str(e))}</code>")


@vpn_user_router.callback_query(F.data.startswith("vreset:"))
async def vpn_reset_link_confirm(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
    await _safe_edit(
        cb,
        "⚠️ <b>تغییر لینک اتصال</b>\n\n"
        "آیا مطمئنی؟ با این کار لینک/QR فعلی از کار می‌افتد و باید کلاینت‌های "
        "متصل را با ساب لینک جدید دوباره تنظیم کنی.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 بله، تغییر بده", callback_data=f"vreset:ok:{aid}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"vsvc:{aid}")],
        ]))
    await cb.answer()


# ─────────────── تغییر نام کانفیگ ───────────────
class _VpnRenameFSM(StatesGroup):
    waiting = State()


class _VpnLabelFSM(StatesGroup):
    """فقط نام نمایشی — بدون تغییر ایمیل پنل."""
    waiting = State()


@vpn_user_router.callback_query(F.data.startswith("vlabel:"))
async def vpn_label_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """شروع نام‌گذاری اولیه از پیام تحویل."""
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
    await state.set_state(_VpnLabelFSM.waiting)
    await state.update_data(vlabel_aid=aid)
    await cb.answer()
    await _EditFirst(cb.message, cb.from_user).answer(   # PATCH_VPN_EDIT_IN_PLACE_V1
        "✏️ یک نام دلخواه برای این کانفیگ بفرست.\n"
        "(این نام فقط برای نمایش در «کانفیگ‌های من» استفاده می‌شه — "
        "هر چیزی می‌تونه باشه مثل: خانه، مدرسه، موبایل)\n\n"
        "یا /skip بزن تا بدون نام بمونه.")


@vpn_user_router.message(_VpnLabelFSM.waiting)
async def vpn_label_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    aid  = data.get("vlabel_aid")
    if msg.text and msg.text.strip() == "/skip":
        await state.clear()
        await msg.answer("✅ بدون نام ذخیره شد.")
        return
    label = (msg.text or "").strip()[:50]
    if not label:
        await msg.answer("یه نام بفرست یا /skip بزن.")
        return
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != msg.from_user.id:
            await state.clear(); return
        a.plan_title = label
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ نام «{label}» ذخیره شد.\n"
                     "حالا توی «کانفیگ‌های من» با همین نام نمایش داده می‌شه.")


@vpn_user_router.callback_query(F.data.startswith("vrename:cancel:"))
async def vpn_rename_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    aid = int(cb.data.split(":")[2])
    await cb.answer("لغو شد")
    # برگشت به جزئیات
    fake = type("FCB", (), {"data": f"vsvc:{aid}", "from_user": cb.from_user,
                             "message": cb.message, "answer": cb.answer})()
    await vpn_service(fake, bot)


@vpn_user_router.callback_query(F.data.startswith("vrename:"))
async def vpn_rename_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True); return
        email = a.email
    await state.set_state(_VpnRenameFSM.waiting)
    await state.update_data(vrename_aid=aid, vrename_old=email)
    await cb.answer()
    await _EditFirst(cb.message, cb.from_user).answer(   # PATCH_VPN_EDIT_IN_PLACE_V1
        f"✏️ <b>تغییر نام کانفیگ</b>\n\n"
        f"نام فعلی: <code>{email}</code>\n\n"
        "یه نام دلخواه بفرست (فقط حروف/عدد انگلیسی، بدون فاصله — مثال: <code>MyHome</code>)\n"
        "این نام روی پنل هم واقعاً تغییر می‌کنه و توی ساب‌لینک/اپ کلاینت هم دیده می‌شه:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"vrename:cancel:{aid}")]]))


@vpn_user_router.message(_VpnRenameFSM.waiting)
async def vpn_rename_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    aid  = data.get("vrename_aid")
    raw_name = (msg.text or "").strip()[:50]
    if not raw_name:
        await msg.answer("یه نام بفرست یا /skip بزن.")
        return
    # === PATCH_CUSTOMER_RENAME_REAL_V1 ===
    # قبلاً این فقط یه برچسبِ داخلیِ نمایشی (plan_title) رو عوض می‌کرد و نامِ
    # واقعیِ کانفیگ روی پنل/ساب‌لینک (که کاربر توی اپ کلاینتش می‌بینه) دست‌نخورده
    # می‌موند. الان واقعاً روی پنل هم رنیم می‌کنیم — دقیقاً مثلِ چیزی که ادمین از
    # «اعمال نام‌ها روی اکانت‌های موجود» (vpn_admin.py) انجام می‌ده.
    sanitized = _re.sub(r"[^a-zA-Z0-9_]", "", raw_name)[:20]
    if not sanitized:
        await msg.answer(
            "این نام فقط شامل حروف/عددِ انگلیسی می‌تونه باشه (بدون فاصله/فارسی/ایموجی). "
            "دوباره بفرست:")
        return
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a   = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != msg.from_user.id:
            await state.clear(); return
        panel = await _panel_for_account(s, row.id, a)
        old_email = a.email
    await state.clear()
    if not panel or not panel.enabled:
        await msg.answer("❌ پنلِ این کانفیگ در دسترس نیست. با پشتیبانی تماس بگیر.")
        return
    if panel.panel_key in ("marzban", "rebecca", "pasargad", "pasarguard", "marzneshin", "rickpanel"):
        # در این پنل‌ها نام‌کاربری همان شناسهٔ یکتای اکانت است و قابل تغییر نیست؛
        # فقط برچسبِ نمایشیِ داخلِ ربات را ذخیره می‌کنیم.
        async with Session() as s:
            a2 = await s.get(PanelAccount, aid)
            if a2:
                a2.plan_title = raw_name
                await s.commit()
        await msg.answer(
            f"ℹ️ در این نوع پنل، نامِ کانفیگ روی خودِ سرور قابلِ تغییر نیست؛ "
            f"فقط برچسبِ نمایشیِ داخلِ ربات به «{raw_name}» تغییر کرد.")
        return
    new_email = f"{sanitized}{gen_sub_id(3)}"
    client = _client_from_panel(panel)
    try:
        await client.login()
        ok = await client.rename_account(old_email, new_email)
    except PanelError as e:
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, row.id, "تغییرِ نامِ سرویس", e, msg.from_user.id)
        await msg.answer("❌ تغییرِ نام انجام نشد.\n" + USER_FAIL_TEXT)
        return
    except Exception:
        logger.exception("خطای رنیم کانفیگ توسط کاربر")
        await msg.answer("❌ خطای غیرمنتظره در تغییرِ نام. با پشتیبانی تماس بگیر.")
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not ok:
        await msg.answer("❌ تغییرِ نام روی پنل انجام نشد. دوباره امتحان کن یا با پشتیبانی تماس بگیر.")
        return
    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            a2.email = new_email
            a2.plan_title = raw_name
            await s.commit()
    await msg.answer(
        f"✅ نامِ کانفیگ به «{raw_name}» تغییر یافت — این نام الان توی ساب‌لینک/اپ کلاینت هم دیده می‌شه.\n"
        f"نامِ فنیِ جدید: <code>{new_email}</code>")



# === PATCH_RENEW_PARTNER_DISCOUNT_V1 ===
async def _renew_partner_price(bot_id: int, telegram_id: int, price, panel_key: str,
                               scope: str, gb=None) -> float:
    """تخفیفِ «همکار» (VpnPartner) روی تمدید. قبلاً فقط روی خریدِ تازه اعمال می‌شد و
    تمدیدِ همکار به قیمتِ کامل حساب می‌شد. همان قاعدهٔ خرید: فقط همکاریِ دستی
    (include_auto_reseller=False)؛ پلنِ ثابت → scope='plans'، تمدیدِ روز/حجمِ دلخواه → 'custom'."""
    try:
        from tenant.bots.vpn.vpn_partners import partner_discount
        pd_ = await partner_discount(
            bot_id=bot_id, telegram_id=telegram_id, price=price, panel_key=panel_key,
            scope=scope, include_auto_reseller=False, gb=gb)
        if pd_.ok and pd_.discount > 0:
            return float(pd_.final_price)
    except Exception:
        logger.exception("محاسبهٔ تخفیفِ همکار روی تمدید ناموفق بود؛ قیمتِ کامل اعمال شد")
    return float(price)


@vpn_user_router.callback_query(F.data.startswith("vren:"))
async def vpn_renew_menu(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[1])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        panel = await _panel_for_account(s, row.id, a)
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == row.id, VpnPlan.active == True,
            VpnPlan.panel_key == (panel.panel_key if panel else ""),
            (await _plan_audience_filter(row.id, cb.from_user.id))[0],  # PATCH_PLAN_AUDIENCE_ENFORCE_V1
        ).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
# === PATCH_RENEW_PLAN_MATCH_V1 ===
        # تفکیکِ حجمی از نامحدود: سرویسِ حجمی نباید پلنِ «نامحدود» را
        # به‌عنوان گزینهٔ تمدید ببیند و برعکس. مبنا خودِ سرویسِ کاربر است
        # (volume_gb==0 یعنی نامحدود). اگر بعد از فیلتر چیزی نماند، به
        # فهرستِ کامل برمی‌گردیم تا کاربر بی‌گزینه نماند.
        _acct_unlimited = (a.volume_gb or 0) == 0
        _matched = [p for p in plans
                    if ((p.volume_gb or 0) == 0) == _acct_unlimited]
        _use = _matched or plans
        items = [(p.id, p.title, p.volume_gb, p.days, float(p.price)) for p in _use]
    if not panel or not panel.enabled:
        await cb.answer("تمدید در حال حاضر ممکن نیست.", show_alert=True)
        return
    # === PATCH_RENEW_PARTNER_DISCOUNT_V1 === قیمتِ نمایشی = قیمتِ واقعیِ پرداخت
    items = [(pid, title, gb, days, await _renew_partner_price(
                  row.id, cb.from_user.id, price, panel.panel_key, "plans", gb))
             for pid, title, gb, days, price in items]
    rows = []
    from tenant import ibtn_style as _ibs
    _renew_style = await _ibs(row.id, "ibtn:renew") or ButtonStyle.SUCCESS
    _back_style  = await _ibs(row.id, "ibtn:back") or ButtonStyle.DANGER
    for pid, title, gb, days, price in items:
        vol = "نامحدود" if gb == 0 else f"{gb}گیگ"
        dur = "بی‌انقضا" if days == 0 else f"{days}روز"
        rows.append([InlineKeyboardButton(
            text=f"♻️ {title} — +{vol}/+{dur} — {rial(price)}",
            callback_data=f"vrenp:{aid}:{pid}", style=_renew_style)])
    if not items:
        # پلن ثابت نداره — گزینه‌های دینامیک از تنظیمات ادمین (مخصوص همین پنل)
        pk = panel.panel_key if panel else ""
        price_day = int(await _vc(row.id, "renew_price_day", pk))
        mn_d = int(await _vc(row.id, "renew_min_days", pk))
        mx_d = int(await _vc(row.id, "renew_max_days", pk))
        st_d = int(await _vc(row.id, "renew_step_days", pk))
        if price_day <= 0:
            await cb.answer("⛔️ تمدید در حال حاضر فعال نیست.", show_alert=True)
            return
        d = mn_d
        count = 0
        while d <= mx_d and count < 6:
            price = await _renew_partner_price(row.id, cb.from_user.id, d * price_day, pk, "custom")
            rows.append([InlineKeyboardButton(
                text=f"♻️ +{d} روز — {rial(price)}",
                callback_data=f"vrend:{aid}:{d}", style=_renew_style)])
            d += st_d
            count += 1
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"vsvc:{aid}",
                                      style=_back_style)])
    await _safe_edit(
        cb,
# === PATCH_RENEW_CLARITY_V1 ===
        "♻️ <b>تمدید مجدد سرویس</b>\n\n"
        "ℹ️ با این گزینه <b>هم زمان و هم حجم</b> به همین سرویس اضافه می‌شود.\n"
        "• اگر سرویست هنوز اعتبار دارد، مقدارِ جدید <b>روی باقی‌ماندهٔ فعلی</b> "
        "اضافه می‌شود.\n"
        "• اگر تمام شده، سرویس از نو فعال می‌شود.\n\n"
        + ("🛰 سرویس فعلی‌ات <b>نامحدود</b> است، پس فقط پلن‌های نامحدود نشان داده می‌شود.\n\n"
           if _acct_unlimited and _matched else
           "🛰 سرویس فعلی‌ات <b>حجمی</b> است، پس فقط پلن‌های حجمی نشان داده می‌شود.\n\n"
           if _matched else "")
        + "گزینهٔ موردنظر را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@vpn_user_router.callback_query(F.data.startswith("vrend:"))
async def vpn_renew_pay_days(cb: CallbackQuery, bot: Bot):
    """تمدید با مدت دلخواه (بدون پلن ثابت) — قیمت بر اساس renew_price_day."""
    parts = cb.data.split(":")
    aid, add_days = int(parts[1]), int(parts[2])
    add_gb = int(parts[3]) if len(parts) > 3 else None
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        panel = await _panel_for_account(s, row.id, a) if (row and a) else None
        if (not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id
                or not panel or not panel.enabled):
            await cb.answer("امکان تمدید نیست.", show_alert=True)
            return
        bot_id = row.id
        price_day = int(await _vc(bot_id, "renew_price_day", panel.panel_key))
        # باگ‌گارد: اگه قیمت روز صفر باشه، رایگان تمدید نشه
        if price_day <= 0:
            await cb.answer("⛔️ تمدید در حال حاضر فعال نیست.", show_alert=True)
            return
        price_gb = int(await _vc(bot_id, "renew_price_gb", panel.panel_key))
        mn_g = int(await _vc(bot_id, "renew_min_gb", panel.panel_key))
        mx_g = int(await _vc(bot_id, "renew_max_gb", panel.panel_key))
        st_g = max(1, int(await _vc(bot_id, "renew_step_gb", panel.panel_key)))
        # === PATCH_RENEW_DAYS_VALIDATE_V1 ===
        # روز/گیگ مستقیم از callback_data می‌آمد و هیچ اعتبارسنجی‌ای نداشت؛
        # با یک callbackِ دست‌ساز مثلِ «vrend:<aid>:-100:0» قیمت منفی می‌شد و
        # «کسر از کیف پول» در واقع موجودی را *زیاد* می‌کرد. فقط همان گزینه‌هایی
        # که منو می‌سازد (vpn_renew_menu) پذیرفته می‌شوند.
        _mn_d = int(await _vc(bot_id, "renew_min_days", panel.panel_key))
        _mx_d = int(await _vc(bot_id, "renew_max_days", panel.panel_key))
        _st_d = int(await _vc(bot_id, "renew_step_days", panel.panel_key))
        _ok_days = {_mn_d + i * _st_d for i in range(6) if _mn_d + i * _st_d <= _mx_d}
        _ok_gb = {0}
        if price_gb > 0 and mx_g > 0:
            _ok_gb |= {max(mn_g, 1) + i * st_g for i in range(6)
                       if max(mn_g, 1) + i * st_g <= mx_g}
        if (add_days <= 0 or add_days not in _ok_days
                or (add_gb is not None and add_gb not in _ok_gb)):
            await cb.answer("این گزینهٔ تمدید معتبر نیست؛ دوباره از منوی تمدید انتخاب کن.",
                            show_alert=True)
            return
        if add_gb is None and price_gb > 0 and mx_g > 0:
            # مرحلهٔ دوم: انتخابِ حجمِ همراهِ تمدید (یا فقط روز)
            from tenant import ibtn_style as _ibs
            _st = await _ibs(bot_id, "ibtn:renew") or ButtonStyle.SUCCESS
            _pk_r = panel.panel_key
            vrows = [[InlineKeyboardButton(
                text=f"♻️ فقط +{add_days} روز — "
                     f"{rial(await _renew_partner_price(bot_id, cb.from_user.id, add_days * price_day, _pk_r, 'custom'))}",
                callback_data=f"vrend:{aid}:{add_days}:0", style=_st)]]
            g, n = max(mn_g, 1), 0
            while g <= mx_g and n < 6:
                vrows.append([InlineKeyboardButton(
                    text=f"♻️ +{add_days} روز + {g} گیگ — "
                         f"{rial(await _renew_partner_price(bot_id, cb.from_user.id, add_days * price_day + g * price_gb, _pk_r, 'custom'))}",
                    callback_data=f"vrend:{aid}:{add_days}:{g}", style=_st)])
                g += st_g
                n += 1
            vrows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"vren:{aid}")])
            await _safe_edit(
                cb, f"♻️ <b>تمدید {add_days} روزه</b>\n\nحجمِ همراهِ تمدید را انتخاب کن "
                    "(یا فقط زمان):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=vrows))
            await cb.answer()
            return
        add_gb = add_gb or 0
        price = add_days * price_day + add_gb * price_gb
        price = await _renew_partner_price(bot_id, cb.from_user.id, price, panel.panel_key, "custom")   # PATCH_RENEW_PARTNER_DISCOUNT_V1
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        if not u or float(u.balance) < price:
            await cb.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        u.balance = float(u.balance) - price
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price,
                        kind="renew", note=f"تمدید VPN: +{add_days} روز" + (f" +{add_gb}G" if add_gb else "")))
        await s.commit()
        email = a.email
        user_db_id = u.id
        panel_data = (panel.base_url, panel.username, panel.password,
                      panel.sub_base or "", panel.inbound_ids, panel.panel_key, panel.api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1

    await cb.answer("در حال تمدید…")
    # === PATCH_SLOW_PANEL_PROGRESS_V1 ===
    # اندازه‌گیری نشان داد بعضی پنل‌هایِ مشتری‌ها ۱۰ تا ۱۳ ثانیه برایِ یک
    # عملیات طول می‌کشند (مثلاً eantcgsad828…: میانه ۹٫۹s رویِ همهٔ
    # endpointها). این کندی سمتِ سرورِ آن‌هاست و از این‌جا قابلِ رفع نیست،
    # ولی *حسِ* هنگ‌کردن قابلِ رفع است: cb.answer فقط یک toastِ کوچک است
    # که سریع محو می‌شود و صفحه بی‌تغییر می‌ماند. یک edit_text صریح یعنی
    # کاربر می‌بیند کار در جریان است و دوباره دکمه نمی‌زند.
    await _progress_edit(cb, "⏳ در حال تمدیدِ سرویس روی سرور… این کار ممکن است چند ثانیه طول بکشد.")
    # === PATCH_PANEL_CLIENT_POOL_V2 ===  مسیرِ تمدید هم رویِ استخر
    from tenant.shared.panel_clients import panel_pool as _pool
    client = _pool.get_sync(
        panel_data[5], panel_data[0], panel_data[1], panel_data[2],
        sub_base=panel_data[3], inbound_ids_str=panel_data[4], api_key=panel_data[6])
    try:
        await client.renew(email, add_days)
        if add_gb:
            await client.add_traffic(email, add_gb)
    except Exception as e:
        # === PATCH_RENEW_CATCH_ALL_V1 ===
        # قبلاً فقط PanelError گرفته می‌شد؛ اگه پنل یه خطای غیرمنتظره‌ی دیگه
        # می‌داد (قطعیِ عجیب، پاسخِ ناقص/غیرمنتظره و…)، این try/except رد
        # می‌شد و به هندلرِ سراسریِ خطا می‌رسید (همون ERR-XXXXXX) — با این
        # تفاوتِ مهم که پولِ کاربر از قبل کسر شده بود و دیگه هیچ‌وقت
        # برنمی‌گشت، چون منطقِ عودت فقط داخلِ همین except بود. حالا هر خطایی
        # این‌جا گرفته می‌شه، پول برمی‌گرده، و پیامِ روشن نمایش داده می‌شه.
        logger.warning("خطای تمدید کانفیگ روی پنل (bot_id=%s, email=%s): %s", bot_id, email, e)
        async with Session() as s:
            u = await s.get(BotUser, user_db_id)
            if u:
                u.balance = float(u.balance) + price
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price,
                                kind="refund", note="عودت به‌دلیل خطای تمدید"))
                await s.commit()
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, bot_id, "تمدیدِ سرویس", e, cb.from_user.id)
        await cb.message.answer(f"❌ تمدید انجام نشد و مبلغ <b>{rial(price)}</b> بازگشت.\n{USER_FAIL_TEXT}")
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            a2.expiry_notice = None
            # === PATCH_EXPIRY_WARN_QUOTA_V1 === با تمدید، چرخهٔ اخطار از نو
            a2.expiry_warn_count = 0
            a2.status = "active"
            base = a2.expires_at if a2.expires_at and a2.expires_at > _dt.datetime.utcnow() else _dt.datetime.utcnow()
            a2.expires_at = base + _dt.timedelta(days=add_days)
            a2.days = (a2.days or 0) + add_days
            if add_gb:
                a2.volume_gb = (a2.volume_gb or 0) + add_gb
                a2.traffic_out_at = None   # PATCH_TRAFFIC_RENEW_GUARD_V1
            await s.commit()
    try:
        await client.close()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(f"✅ سرویس تمدید شد. (+{add_days} روز" + (f" / +{add_gb} گیگ" if add_gb else "") + ")")
    # === PATCH_RENEW_PURCHASE_ALERT_V1 ===
    try:
        from tenant import _purchase_alert
        async with Session() as s:
            _row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
            _buyer = await s.get(BotUser, user_db_id)
        if _row:
            # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
            from tenant.bots.vpn.vpn_admin import panel_label
            _pname = await panel_label(bot_id, panel.panel_key) if panel else None
            await _purchase_alert(
                bot, _row, buyer=_buyer,
                section="تمدید VPN",
                detail=f"تمدیدِ آزاد — +{add_days} روز" + (f" +{add_gb} گیگ" if add_gb else ""), amount=price,
                product_id=aid, panel_name=_pname)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ خریدِ تمدیدِ VPN)")
    from tenant.shared.extras import handle_referral_purchase
    # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
    try:
        _bg(handle_referral_purchase(bot_id, cb.from_user.id, bot), "پاداش دعوت")
    except Exception:
        logger.exception("پاداشِ دعوت ناموفق بود (خرید ادامه می‌یابد)")


@vpn_user_router.callback_query(F.data.startswith("vrenp:"))
async def vpn_renew_pay(cb: CallbackQuery, bot: Bot):
    # === PATCH_VPN_DOUBLE_BUY_GUARD_V1 ===
    _key = f"vrenp:{bot.token}:{cb.from_user.id}:{cb.data}"
    async with _vpn_once(_key) as _ok:
        if not _ok:
            await cb.answer("⏳ درخواست قبلی‌ات هنوز در حال پردازش است، کمی صبر کن.",
                            show_alert=True)
            return
        # === PATCH_PAY_PHONE_GATE_FIX_V1 ===
        if await _pay_gate_block(cb, bot):
            return
        # === PATCH_PURCHASE_IDEMPOTENCY_V1 ===
        if not await _claim_or_reject(cb, bot, "vrenp"):
            return
        await _vpn_renew_pay_impl(cb, bot)


async def _vpn_renew_pay_impl(cb: CallbackQuery, bot: Bot):
    _, aid_s, pid_s = cb.data.split(":")
    aid, pid = int(aid_s), int(pid_s)
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
        plan = await s.get(VpnPlan, pid)
        panel = await _panel_for_account(s, row.id, a) if (row and a) else None
        if (not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id
                or not plan or plan.bot_id != row.id or not plan.active
                or not panel or not panel.enabled
                or plan.panel_key != panel.panel_key
                or not await _plan_allowed_for(plan, row.id, cb.from_user.id)):
            await cb.answer("امکان تمدید نیست.", show_alert=True)
            return
        bot_id = row.id
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == cb.from_user.id).with_for_update())
        price = await _renew_partner_price(bot_id, cb.from_user.id, float(plan.price),
                                           plan.panel_key, "plans", plan.volume_gb)   # PATCH_RENEW_PARTNER_DISCOUNT_V1
        if not u or float(u.balance) < price:
            await cb.answer("موجودی کیف پول کافی نیست.", show_alert=True)
            return
        u.balance = float(u.balance) - price
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price,
                        kind="renew", note=f"تمدید VPN: {plan.title}"))
        await s.commit()
        email = a.email
        add_gb, add_days, user_db_id = plan.volume_gb, plan.days, u.id
        plan_title = plan.title
        panel_data = (panel.base_url, panel.username, panel.password,
                      panel.sub_base or "", panel.inbound_ids, panel.panel_key, panel.api_key)  # PATCH_PANEL_API_KEY_PROPAGATION_V1

    await cb.answer("در حال تمدید…")
    # === PATCH_SLOW_PANEL_PROGRESS_V1 ===
    # اندازه‌گیری نشان داد بعضی پنل‌هایِ مشتری‌ها ۱۰ تا ۱۳ ثانیه برایِ یک
    # عملیات طول می‌کشند (مثلاً eantcgsad828…: میانه ۹٫۹s رویِ همهٔ
    # endpointها). این کندی سمتِ سرورِ آن‌هاست و از این‌جا قابلِ رفع نیست،
    # ولی *حسِ* هنگ‌کردن قابلِ رفع است: cb.answer فقط یک toastِ کوچک است
    # که سریع محو می‌شود و صفحه بی‌تغییر می‌ماند. یک edit_text صریح یعنی
    # کاربر می‌بیند کار در جریان است و دوباره دکمه نمی‌زند.
    await _progress_edit(cb, "⏳ در حال تمدیدِ سرویس روی سرور… این کار ممکن است چند ثانیه طول بکشد.")
    # === PATCH_PANEL_CLIENT_POOL_V2 ===  مسیرِ تمدید هم رویِ استخر
    from tenant.shared.panel_clients import panel_pool as _pool
    client = _pool.get_sync(
        panel_data[5], panel_data[0], panel_data[1], panel_data[2],
        sub_base=panel_data[3], inbound_ids_str=panel_data[4], api_key=panel_data[6])
    try:
        if add_days:
            await client.renew(email, add_days)
        if add_gb:
            await client.add_traffic(email, add_gb)
    except Exception as e:
        # === PATCH_RENEW_CATCH_ALL_V1 ===
        # همون دلیلِ نسخهٔ بالا: قبلاً فقط PanelError گرفته می‌شد و هر خطای
        # دیگه‌ای از پنل (بخصوص همین‌جا که واقعاً «داره پلن اضافه می‌کنه»)
        # بدونِ عودتِ پول به ERR-XXXXXX می‌رسید.
        logger.warning("خطای تمدید/افزایش حجم روی پنل (bot_id=%s, email=%s): %s", bot_id, email, e)
        async with Session() as s:
            u = await s.get(BotUser, user_db_id)
            if u:
                u.balance = float(u.balance) + price
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price,
                                kind="refund", note="عودت به‌دلیل خطای تمدید"))
                await s.commit()
        from tenant import report_user_error, USER_FAIL_TEXT
        await report_user_error(bot, bot_id, "تمدیدِ سرویس", e, cb.from_user.id)
        await cb.message.answer(f"❌ تمدید انجام نشد و مبلغ <b>{rial(price)}</b> بازگشت.\n{USER_FAIL_TEXT}")
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    # ریست اخطار تا چرخهٔ جدید دوباره هشدار دهد + به‌روزرسانی تاریخ انقضا/حجم محلی
    async with Session() as s:
        a2 = await s.get(PanelAccount, aid)
        if a2:
            a2.expiry_notice = None
            # === PATCH_EXPIRY_WARN_QUOTA_V1 === با تمدید، چرخهٔ اخطار از نو
            a2.expiry_warn_count = 0
            a2.status = "active"
            if add_days:
                base = a2.expires_at if a2.expires_at and a2.expires_at > _dt.datetime.utcnow() else _dt.datetime.utcnow()
                a2.expires_at = base + _dt.timedelta(days=add_days)
                a2.days = (a2.days or 0) + add_days
            if add_gb:
                a2.volume_gb = (a2.volume_gb or 0) + add_gb
                a2.traffic_out_at = None   # PATCH_TRAFFIC_RENEW_GUARD_V1
            await s.commit()
    try:
        await client.close()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    vol = "نامحدود" if add_gb == 0 else f"{add_gb} گیگ"
    dur = "بی‌انقضا" if add_days == 0 else f"{add_days} روز"
    await cb.message.answer(f"✅ سرویس تمدید شد. (+{vol} / +{dur})")
    # === PATCH_RENEW_PURCHASE_ALERT_V1 ===
    # تمدید و افزایشِ حجم، برخلافِ خریدِ تازه، هیچ‌وقت به کانالِ اعتمادسازی/
    # اطلاعِ خودکارِ ادمین گزارش نمی‌شد — انگار اصلاً فروشی اتفاق نیفتاده.
    try:
        from tenant import _purchase_alert
        async with Session() as s:
            _row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
            _buyer = await s.get(BotUser, user_db_id)
        if _row:
            # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
            from tenant.bots.vpn.vpn_admin import panel_label
            _pname = await panel_label(bot_id, panel.panel_key) if panel else None
            await _purchase_alert(
                bot, _row, buyer=_buyer,
                section="تمدید VPN",
                detail=f"{plan_title} — +{vol} / +{dur}", amount=price,
                product_id=aid, panel_name=_pname)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ خریدِ تمدیدِ VPN)")
    from tenant.shared.extras import handle_referral_purchase
    # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
    try:
        _bg(handle_referral_purchase(bot_id, cb.from_user.id, bot), "پاداش دعوت")
    except Exception:
        logger.exception("پاداشِ دعوت ناموفق بود (خرید ادامه می‌یابد)")


# ─────────────── آموزش اتصال ───────────────
_GUIDE = {
    "android": ("🤖 <b>اتصال در اندروید</b>\n\n"
                "۱) برنامهٔ <b>v2rayNG</b> یا <b>Hiddify</b> را از گوگل‌پلی نصب کن.\n"
                "۲) لینک اشتراک (Subscription) را کپی کن.\n"
                "۳) در برنامه: + ← «Import config from Clipboard» یا افزودن Subscription.\n"
                "۴) سرور را انتخاب و وصل شو."),
    "ios": ("🍎 <b>اتصال در آیفون</b>\n\n"
            "۱) برنامهٔ <b>V2Box</b> یا <b>Streisand</b> را از App Store نصب کن.\n"
            "۲) لینک اشتراک را کپی کن.\n"
            "۳) در برنامه: Add ← Import Subscription / from Clipboard.\n"
            "۴) سرور را انتخاب و Connect بزن."),
    "pc": ("💻 <b>اتصال در ویندوز</b>\n\n"
           "۱) برنامهٔ <b>v2rayN</b> یا <b>Hiddify</b> را دانلود کن.\n"
           "۲) لینک اشتراک را کپی کن.\n"
           "۳) v2rayN: Subscriptions ← Import ← Update.\n"
           "۴) سرور را انتخاب و سیستم‌پراکسی را روشن کن."),
}


async def _guide_menu_kb(bot_id: int | None = None) -> InlineKeyboardMarkup:
    """منوی انتخاب سیستم‌عامل — نامِ دکمه‌ها قابلِ ویرایش توسط ادمین است و
    دکمه‌های آموزشِ سفارشی (مثلاً مک) هم اضافه می‌شوند.

    === PATCH_GUIDE_LIST_REORDER_V1 ===
    قبلاً این دکمه‌ها به‌صورتِ فشرده (سه‌تا در هر ردیف) نمایش داده می‌شدند و
    ترتیبشان همیشه ثابت بود (اندروید/آیفون/ویندوز اول، سفارشی‌ها بعد). حالا
    به‌صورتِ لیستِ یک‌ستونه (هرکدوم یک ردیف، خواناتر) و با ترتیبی که ادمین
    از پنلِ «ویرایش متن آموزش اتصال» می‌تونه جابه‌جا کنه، نمایش داده می‌شوند.
    """
    labels = {"android": "🤖 اندروید", "ios": "🍎 آیفون", "pc": "💻 ویندوز"}
    ordered_keys: list[str] = list(labels.keys())
    if bot_id:
        try:
            from tenant import get_setting, _guide_custom_list, _guide_label_of, _guide_ordered_keys
            for k in labels:
                labels[k] = await _guide_label_of(bot_id, k)
            customs = {g["key"]: await _guide_label_of(bot_id, g["key"])
                      for g in await _guide_custom_list(bot_id)}
            labels.update(customs)
            ordered_keys = await _guide_ordered_keys(bot_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
# === PATCH_GUIDE_BTN_COLOR_V1 ===
    # رنگِ دلخواهِ مالک برایِ دکمه‌هایِ داخلِ «آموزش اتصال». همان مکانیزمِ
    # ibtn_style که بقیهٔ دکمه‌ها استفاده می‌کنند؛ اگر رنگی انتخاب نشده
    # باشد None برمی‌گردد و دکمه مثلِ قبل بی‌رنگ می‌ماند.
    # رنگِ هر دکمه جداگانه از Settingِ guide_color:<key> خوانده می‌شود
    # (در پنل: «ویرایش متن آموزش اتصال» ← دکمهٔ «رنگ» کنارِ هر گزینه).
    _colors: dict[str, object] = {}
    if bot_id:
        try:
            from aiogram.enums import ButtonStyle as _BS
# === PATCH_GUIDE_BTN_COLOR_PRO_V1 ===
            from tenant import get_setting as _gs
            from plans import Feature as _F, has_feature as _hf
            from models import Bot as _BR
            from config import Session as _S
            async with _S() as _ss:
                _br = await _ss.get(_BR, bot_id)
            if _br and _hf(_br.plan, _F.GUIDE_BTN_COLOR):
                _map = {"danger": _BS.DANGER, "success": _BS.SUCCESS,
                        "primary": _BS.PRIMARY}
                for k in ordered_keys:
                    _colors[k] = _map.get(await _gs(bot_id, f"guide_color:{k}", ""))
        except Exception:
            logger.exception("خواندنِ رنگِ دکمه‌هایِ آموزش ناموفق بود")
    rows = [[InlineKeyboardButton(text=labels[k], callback_data=f"vhelp:{k}",
                                  style=_colors.get(k))]
            for k in ordered_keys if k in labels]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_vpn_help(bot: Bot, msg: Message):
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    kb = await _guide_menu_kb(row.id if row else None)
    if row:
        from tenant import inline_back_row
        _br = await inline_back_row(row.id)
        if _br:
            kb.inline_keyboard.append(_br)
    await msg.answer("🧩 <b>آموزش اتصال</b>\n\nسیستم‌عاملت را انتخاب کن:",
                     reply_markup=kb)


@vpn_user_router.callback_query(F.data == "vhelp:menu")
async def vpn_help_menu(cb: CallbackQuery, bot: Bot):
    row = None
    try:
        from models import Bot as BotRow
        async with Session() as s:
            row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb = await _guide_menu_kb(row.id if row else None)
    try:
        if row:
            from tenant import inline_back_row
            _br = await inline_back_row(row.id)
            if _br:
                kb.inline_keyboard.append(_br)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        await cb.message.edit_text("🧩 <b>آموزش اتصال</b>\n\nسیستم‌عاملت را انتخاب کن:",
                                   reply_markup=kb)
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer("🧩 <b>آموزش اتصال</b>\n\nسیستم‌عاملت را انتخاب کن:",
                                reply_markup=kb)
    await cb.answer()


@vpn_user_router.callback_query(F.data.startswith("vhelp:"))
async def vpn_help_show(cb: CallbackQuery, bot: Bot):
    key = cb.data.split(":")[1]
    if key == "menu":
        return
    default_text = _GUIDE.get(key)
    if not default_text:
        # دکمهٔ آموزشِ سفارشی (مثلاً مک) — متن از تنظیماتِ ادمین خوانده می‌شود
        # === PATCH_GUIDE_SILENT_FIX_V1 ===
        try:
            from tenant import get_setting, _guide_custom_list
            async with Session() as s:
                bot_row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
            if not bot_row or not any(
                    g["key"] == key for g in await _guide_custom_list(bot_row.id)):
                await cb.answer("⛔️ این دکمهٔ آموزش دیگر وجود ندارد.", show_alert=True)
                return
            text = await get_setting(bot_row.id, f"guide_custom_{key}", "")
        except Exception:
            await cb.answer("⚠️ خطایی رخ داد؛ دوباره امتحان کن.", show_alert=True)
            return
        if not text.strip():
            await cb.answer("هنوز آموزشی برای این بخش تنظیم نشده.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vhelp:menu",
                                  style=ButtonStyle.DANGER)]
        ])
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            await cb.message.answer(text, reply_markup=kb)
        await cb.answer()
        return
    # متن سفارشی‌شده توسط ادمین را از DB بخوان (اگر ست شده باشد)
    _guide_key_map = {"android": "guide_android", "ios": "guide_ios", "pc": "guide_pc"}
    setting_key = _guide_key_map.get(key)
    text = default_text
    if setting_key:
        try:
            # ⚠️ import محلیِ Session این‌جا کل تابع را خراب می‌کرد (UnboundLocalError
            # در شاخهٔ دکمه‌های سفارشی بالاتر). Session/select در سطح ماژول موجودند.
            from models import Setting
            async with Session() as s:
                bot_row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
                if bot_row:
                    st = await s.scalar(
                        select(Setting).where(
                            Setting.bot_id == bot_row.id,
                            Setting.key == setting_key,
                        )
                    )
                    if st and st.value:
                        text = st.value
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vhelp:menu", style=ButtonStyle.DANGER)]
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


# ═══════════ PATCH_AUTO_APPROVE_VPN_CARD_V1 ═══════════
# باگِ گزارش‌شده (تیکت #468): «تایید خودکار روی کیف پول کار می‌کند ولی در
# سرویس‌ها نه». علتش این بود که گیتِ تاییدِ خودکار در card_purchase.py فقط
# pay_method در ("card", "mcfg_card") را می‌پذیرفت — یعنی سفارش‌هایِ خریدِ
# مستقیم از پنل (pay_method="vpn_card") هرگز به موتورِ تشخیصِ رسید نمی‌رسیدند،
# هرچقدر هم ادمین گزینه را روشن می‌کرد. این‌جا معادلِ vcadm_decide (تاییدِ
# دستی) را برایِ مسیرِ خودکار می‌سازیم، با همان قفلِ اتمیک و همان منطقِ
# تشخیصِ پنل — به‌علاوهٔ دکمهٔ «لغو» برایِ ادمین، چون تحویل *قبل* از بررسیِ
# انسانی انجام می‌شود.

async def auto_approve_vpn_card_order(bot: Bot, row, oid: int, check, file_id: str | None,
                                      receipt_text: str | None,
                                      file_unique_id: str | None = None) -> bool:
    """تاییدِ خودکارِ سفارشِ VPN (کارت‌به‌کارت، ساختِ کانفیگ روی پنل).

    True یعنی سفارش تایید و کانفیگ تحویل شد؛ False یعنی نشد و باید مثلِ قبل
    به بررسیِ دستیِ ادمین ارجاع داده شود.
    """
    from models import Order
    from sqlalchemy import update as sa_update
    from tenant import rial, inline_back_kb, _notify_receipt_admins, _admin_user_block, _purchase_alert
    from tenant.gateways.auto_receipt import (receipt_fingerprint, photo_fingerprint,
                                     claim_receipts, release_receipts)

    bot_id = row.id
    _dup_scope = f"tenant:{bot_id}"
    _dup_fp = receipt_fingerprint(check)
    _photo_fp = photo_fingerprint(file_unique_id)
    # PATCH_RECEIPT_CLAIM_ATOMIC_V1: ثبتِ اتمیکِ رسید *پیش* از تحویل (قبلاً بعد از تحویل
    # ثبت می‌شد و دو سفارشِ هم‌زمان با یک رسید هر دو تحویل می‌گرفتند). هر مسیری که
    # تحویل را انجام ندهد (False) رسید را آزاد می‌کند تا تأییدِ دستی ممکن بماند.
    _fps = [_photo_fp, _dup_fp]
    if not await claim_receipts(_dup_scope, _fps):
        return False
    ok = False
    try:
        ok = await _auto_approve_vpn_card_order_body(
            bot, row, oid, check, file_id, receipt_text, bot_id, _dup_scope)
        return ok
    finally:
        if not ok:
            await release_receipts(_dup_scope, _fps)


async def _auto_approve_vpn_card_order_body(bot: Bot, row, oid: int, check, file_id, receipt_text,
                                            bot_id: int, _dup_scope: str) -> bool:
    from models import Order
    from sqlalchemy import update as sa_update
    from tenant import rial, inline_back_kb, _notify_receipt_admins, _admin_user_block, _purchase_alert

    # قفلِ اتمیک — دقیقاً مثلِ vcadm_decide، تا رسیدِ همزمان دو بار کانفیگ نسازد.
    async with Session() as s:
        result = await s.execute(
            sa_update(Order)
            .where(Order.id == oid, Order.bot_id == bot_id, Order.status == "awaiting_admin")
            .values(status="paid")
        )
        await s.commit()
        if result.rowcount != 1:
            return False
        o = await s.get(Order, oid)
        if not o:
            return False
        u = await s.get(BotUser, o.user_id)
        if not u:
            return False
        gb, days, count = o.volume_gb, o.days, o.count
        amount = float(o.amount)
        o_clabel = (o.custom_label or "").strip()[:60]
        tracking_code = o.tracking_code
        buyer_tg_id = u.telegram_id
        # تشخیصِ پنل — همان ترتیبِ PATCH_VPN_CARD_PANEL_KEY_RESOLVE_V1
        _panel_guessed = False
        if o.panel_key:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == bot_id, VpnPanel.panel_key == o.panel_key))
        else:
            _plan_pk = None
            if getattr(o, "plan_id", None):
                _plan_row = await s.get(VpnPlan, o.plan_id)
                if _plan_row and _plan_row.bot_id == bot_id:
                    _plan_pk = _plan_row.panel_key
            if _plan_pk:
                panel = await s.scalar(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id, VpnPanel.panel_key == _plan_pk))
            else:
                panel = await s.scalar(select(VpnPanel).where(
                    VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))
                _panel_guessed = True

    if not panel or not panel.enabled:
        # پنل در دسترس نیست — سفارش را به حالتِ بررسیِ دستی برگردان، وگرنه
        # مشتری پول داده و سفارشش «paid» ولی بدونِ کانفیگ گیر می‌کند.
        async with Session() as s:
            o2 = await s.get(Order, oid)
            if o2 and o2.status == "paid":
                o2.status = "awaiting_admin"
                await s.commit()
        logger.warning("auto-approve VPN: پنلِ فعالی برایِ سفارش #%s پیدا نشد — به بررسیِ دستی برگشت.", oid)
        return False

    # ساختِ کانفیگ‌ها روی پنل
    client = _client_from_panel(panel)
    created_ids: list[int] = []
    failed = 0
    email = None
    # === PATCH_AUTO_APPROVE_VPN_CARD_LOGIN_FAIL_V1 ===
    # قبلاً این try فقط حلقهٔ ساختِ تک‌تکِ اکانت‌ها را try/except می‌کرد و
    # خودِ `client.login()` بیرون از هر except بود (فقط finally برایِ بستنِ
    # کلاینت). چند خط بالاتر سفارش با یک UPDATE اتمیک از قبل «paid» شده
    # بود؛ اگه login به پنل شکست می‌خورد (پنلِ کند/تایم‌اوت/خطای شبکه —
    # چیزی که رویِ همین سرور برایِ پنل‌های پاسارگاد رایج دیده شده)، اکسپشن
    # از این تابع بیرون می‌رفت، هیچ‌کدوم از دو مسیرِ fallback زیر
    # («پنل پیدا نشد» / «not created_ids») اجرا نمی‌شد، سفارش برایِ همیشه
    # paid می‌موند، هیچ کانفیگی ساخته نمی‌شد، و چون تابع قبل از رسیدن به
    # `_notify_receipt_admins` کرش می‌کرد، ادمین هم هیچ گزارشی دریافت
    # نمی‌کرد — پول گرفته می‌شد، نه کانفیگ می‌رفت نه خبر به ادمین می‌رسید.
    login_or_unexpected_failure = False
    try:
        await client.login()
        for _ in range(count):
            try:
                email = await _make_config_email(
                    bot_id, panel.panel_key, row.username if row else None,
                    u.telegram_id, u.username)
                # === PATCH_PURCHASE_ON_HOLD_V1 ===
                _poh = await purchase_on_hold_for(bot_id, panel.panel_key)  # PATCH_PURCHASE_ON_HOLD_PER_PANEL_V1
                # === PATCH_PANEL_DEFAULT_IP_LIMIT_V1 ===
                _lim = await panel_default_limit_ip(bot_id, panel.panel_key)
                acc = await client.create_account(
                    email, gb=gb, days=days, limit_ip=_lim,
                    uuid_str=gen_uuid(), sub_id=gen_sub_id(), on_hold=_poh)
                async with Session() as s:
                    new_acc = PanelAccount(
                        bot_id=bot_id, user_telegram_id=u.telegram_id,
                        email=email, panel_key=panel.panel_key,
                        uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
                        sub_link=acc.get("sub_link") or "", volume_gb=gb, days=days,
                        plan_title=(o_clabel or _plan_title_fa(gb, days)), status="active",
                        paid_toman=(int(amount // max(int(count), 1)) or None),   # PATCH_VPN_REFUND_V1
                        svc_code=await new_svc_code(s, bot_id),
                        expires_at=_expires_at_from_ms(acc.get("expiry_ms")),
                        first_conn_notified=False)
                    s.add(new_acc)
                    await s.commit()
                    created_ids.append(new_acc.id)
            except Exception as e:
                logger.warning(
                    "auto-approve VPN: خطای ساختِ کانفیگ (bot_id=%s, panel=%s, email=%s): %s",
                    bot_id, panel.panel_key, email, e)
                failed += 1
    except Exception:
        login_or_unexpected_failure = True
        logger.exception(
            "auto-approve VPN: login/خطای غیرمنتظره در پنلِ %s برایِ سفارش #%s — "
            "سفارش به بررسیِ دستی برمی‌گردد.", panel.panel_key, oid)
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("auto-approve VPN: بستنِ کلاینتِ پنل ناموفق بود")

    if login_or_unexpected_failure or not created_ids:
        # هیچ کانفیگی ساخته نشد (یا کلاً login/چیزِ دیگه‌ای ترکید) — برگرد
        # به بررسیِ دستی تا ادمین دستی رسیدگی کند و سفارش گم نشود.
        async with Session() as s:
            o2 = await s.get(Order, oid)
            if o2 and o2.status == "paid":
                o2.status = "awaiting_admin"
                await s.commit()
        logger.warning("auto-approve VPN: ساختِ کانفیگ برایِ سفارش #%s کاملاً ناموفق بود.", oid)
        return False


    from tenant.shared.extras import handle_referral_purchase
    # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
    try:
        _bg(handle_referral_purchase(bot_id, buyer_tg_id, bot), "پاداش دعوت")
    except Exception:
        logger.exception("پاداشِ دعوت ناموفق بود (خرید ادامه می‌یابد)")

    try:
        await bot.send_message(buyer_tg_id, "✅ پرداخت شما به‌صورت خودکار تایید شد!",
                               reply_markup=await inline_back_kb(bot_id))
    except Exception:
        logger.exception("auto-approve VPN: ارسالِ پیامِ تایید به کاربر ناموفق بود")
    if failed:
        try:
            await bot.send_message(buyer_tg_id,
                                   f"⚠️ {len(created_ids)} کانفیگ ساخته شد، {failed} ناموفق بود. "
                                   "برای باقی‌مانده با پشتیبانی تماس بگیرید.")
        except Exception:
            logger.exception("auto-approve VPN: اطلاعِ ساختِ ناقص به کاربر ناموفق بود")
    for aid in created_ids:
        # PATCH_DELIVERY_NEVER_LOST_V1 — این هم نباید کلِ خرید را بترکاند
        try:
            await _send_config_delivery(bot, buyer_tg_id, aid)
        except Exception:
            logger.exception("تحویلِ کانفیگ ناموفق بود (خرید ادامه می‌یابد)")

    # گزارشِ کامل برایِ ادمین + دکمهٔ لغو (تحویل قبل از بررسیِ انسانی انجام شد)
    vol = "نامحدود" if gb == 0 else f"{gb} گیگ"
    dur = "بی‌انقضا" if days == 0 else f"{days} روز"
    detail_lines = [f"🔎 مبلغِ خوانده‌شده: {rial(check.amount)}"]
    if check.card_last4:
        detail_lines.append(f"💳 ۴ رقمِ آخرِ کارت روی رسید: <code>{check.card_last4}</code>")
    if check.date_jalali:
        detail_lines.append(f"📅 تاریخِ رسید: {check.date_jalali}")
    report = (
        f"🤖 <b>تایید خودکار رسید — سرویس VPN (پنل)</b>\n\n"
        f"{await _admin_user_block(u, buyer_tg_id, tracking_code, bot_id=bot_id)}\n"
        f"📦 سرویس: {vol}/{dur} × {len(created_ids)} عدد\n"
        f"💵 مبلغ سفارش: {rial(amount)}\n"
        + "\n".join(detail_lines) + "\n"
        f"✅ نتیجهٔ بررسی: {check.reason}\n"
        f"سفارش: #{await order_no(Order, oid)}\n\n"
        f"✅ به‌صورت خودکار تایید و کانفیگ روی «{panel.panel_key}» ساخته و ارسال شد.\n"
        f"⚠️ اگر این رسید فیک/مشکوک است، با دکمهٔ زیر لغو کنید (کانفیگ‌ها از پنل حذف می‌شوند)."
    )
    if _panel_guessed:
        report += ("\n\n⚠️ این سفارش پنلِ ثبت‌شده/پلنِ مشخصی نداشت؛ روی اولین پنلِ فعال ساخته شد.")
    if receipt_text:
        # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
        report += f"\n\n📋 رسید متنی:\n<blockquote>{html.escape(receipt_text[:500])}</blockquote>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 لغو (رسید فیک بود)", callback_data=f"vcadm_revoke:{oid}",
                             style=ButtonStyle.DANGER),
    ]])
    await _notify_receipt_admins(bot, row, report, kb, file_id)

    try:
        # === PATCH_TRUST_CHANNEL_PLAN_TITLE_V1 ===
        # قبلاً اینجا فقط حجم/روز می‌رفت («3 گیگ · 30 روز × 1 عدد») و نامِ
        # خودِ پلن (که چند خط بالاتر برایِ خودِ کانفیگِ تحویلی محاسبه شده
        # بود: o_clabel یا _plan_title_fa) اصلاً استفاده نمی‌شد — برایِ
        # همین تو گزارشِ کانالِ اعتمادسازی معلوم نبود مشتری چه سرویسی
        # خریده، فقط حجم/مدتش معلوم بود.
        _pt = o_clabel or _plan_title_fa(gb, days)
        # === PATCH_TRUST_CHANNEL_PANEL_NAME_V1 ===
        from tenant.bots.vpn.vpn_admin import panel_label
        _pname = await panel_label(bot_id, panel.panel_key) if panel else None
        # === PATCH_ORDER_BUTTON_ID_V1 ===
        _bname = None
        if getattr(o, "button_id", None):
            async with Session() as _s:
                _mb = await _s.get(MainButton, o.button_id)
                _bname = _mb.label if _mb else None
        await _purchase_alert(
            bot, row, buyer=u,
            section="سرویس VPN (پنل — تاییدِ خودکارِ رسید)",
            detail=_purchase_detail(_pt, vol, dur, len(created_ids)), amount=amount,
            public_detail=_purchase_detail(_plan_title_fa(gb, days), gb, days, len(created_ids)),  # PATCH_TRUST_DETAIL_DEDUP_V2
            order_id=oid, tracking_code=tracking_code, count=len(created_ids),
            panel_name=_pname, button_name=_bname)
    except Exception:
        logger.exception("auto-approve VPN: _purchase_alert ناموفق بود")
    return True


# === PATCH_REVOKE_CONFIRM_V1 ===
# گزارش (ایلیا): «دستم خورد رویِ لغو (رسید فیک) و نمی‌دونم سرویسِ کاربر چی شد» — لغو سرویس را از پنل حذف
# می‌کند و برگشت‌ناپذیر است، پس حالا قبل از اجرا تأیید می‌خواهد.
@vpn_user_router.callback_query(F.data.startswith("vcadm_revoke:"))
async def vcadm_revoke_ask(cb: CallbackQuery, bot: Bot):
    from tenant import is_owner_or_admin, get_bot_row
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط ادمین‌ها.", show_alert=True)
        return
    oid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ بله؛ سرویس از پنل حذف و سفارش لغو شود",
                              callback_data=f"vcadm_revoke_ok:{oid}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="↩️ انصراف", callback_data=f"vcadm_revoke_no:{oid}",
                              style=ButtonStyle.SUCCESS)],
    ])
    await cb.answer("این کار سرویسِ کاربر را از پنل حذف می‌کند و برگشت‌ناپذیر است. مطمئنی؟", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("[revoke] نمایشِ تأیید ممکن نشد")


@vpn_user_router.callback_query(F.data.startswith("vcadm_revoke_no:"))
async def vcadm_revoke_no(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🚫 لغو (رسید فیک بود)", callback_data=f"vcadm_revoke:{oid}")]])
    await cb.answer("انصراف داده شد؛ سفارش سرِ جایش است.")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("[revoke] برگرداندنِ دکمه ممکن نشد")


@vpn_user_router.callback_query(F.data.startswith("vcadm_revoke_ok:"))
async def vcadm_revoke(cb: CallbackQuery, bot: Bot):
    """لغوِ یک تاییدِ خودکارِ VPN: کانفیگ‌های ساخته‌شده از پنل و دیتابیس حذف
    و سفارش «rejected» می‌شود. معادلِ rcadm_revoke برایِ مسیرِ پنل."""
    from tenant import is_owner_or_admin, get_bot_row
    from models import Order
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط ادمین‌ها.", show_alert=True)
        return
    oid = int(cb.data.split(":")[1])
    async with Session() as s:
        o = await s.get(Order, oid, with_for_update=True)
        if not o or o.bot_id != row.id or o.status != "paid":
            await cb.answer("قبلاً رسیدگی شده.", show_alert=True)
            return
        u = await s.get(BotUser, o.user_id)
        buyer_tg_id = u.telegram_id if u else None
        created_at = o.created_at
        o.status = "rejected"
        await s.commit()
    # کانفیگ‌هایی که بعدِ ثبتِ این سفارش برایِ همین کاربر ساخته شدند
    removed = 0
    if buyer_tg_id:
        async with Session() as s:
            accs = (await s.scalars(select(PanelAccount).where(
                PanelAccount.bot_id == row.id,
                PanelAccount.user_telegram_id == buyer_tg_id,
                PanelAccount.created_at >= created_at,
                PanelAccount.created_at <= created_at + _dt.timedelta(hours=3)))).all() if created_at else []   # PATCH_REVOKE_CONFIRM_V1: فقط سرویس‌هایِ همین سفارش، نه خریدهایِ بعدی
            acc_list = [(a.id, a.email, a.panel_key) for a in accs]
        for aid, email, pkey in acc_list:
            async with Session() as s:
                panel = await s.scalar(select(VpnPanel).where(
                    VpnPanel.bot_id == row.id, VpnPanel.panel_key == pkey))
            if panel:
                client = _client_from_panel(panel)
                try:
                    await client.login()
                    await client.delete_account(email)
                except Exception:
                    logger.exception("لغوِ تاییدِ خودکار: حذفِ %s از پنل ناموفق بود", email)
                finally:
                    try:
                        await client.close()
                    except Exception:
                        logger.exception("لغوِ تاییدِ خودکار: بستنِ کلاینت ناموفق بود")
            async with Session() as s:
                a2 = await s.get(PanelAccount, aid)
                if a2:
                    await s.delete(a2)
                    await s.commit()
                    removed += 1
    await cb.answer(f"لغو شد — {removed} کانفیگ حذف شد.", show_alert=True)
    try:
        original = cb.message.caption or cb.message.text or ""
        done = original + f"\n\n🚫 <b>لغو شد توسط ادمین</b> ({removed} کانفیگ حذف شد)"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done, reply_markup=None)
        else:
            await cb.message.edit_text(done, reply_markup=None)
    except Exception:
        logger.exception("لغوِ تاییدِ خودکار: ویرایشِ گزارش ناموفق بود")
    if buyer_tg_id:
        try:
            await bot.send_message(buyer_tg_id,
                                   "❌ سفارش شما پس از بررسی لغو شد و سرویس غیرفعال گردید. "
                                   "برای پیگیری با پشتیبانی تماس بگیرید.")
        except Exception:
            logger.exception("لغوِ تاییدِ خودکار: اطلاع به کاربر ناموفق بود")
