"""
vpn_partners.py — قابلیت «حساب‌های همکار» برای بخشِ «VPN خودکار».

ادمینِ ربات می‌تواند آیدیِ عددیِ تلگرامِ یک همکار/شخص خاص را ثبت کند و برای
او یک درصدِ تخفیفِ دلخواه تعریف کند (مثلاً ۳۰٪). این تخفیف بدونِ نیاز به
واردکردنِ کدِ تخفیف، خودکار روی خریدهایِ همان شخص اعمال می‌شود. برای هر
همکار به‌صورت مجزا مشخص می‌شود:
  • رویِ کدام پنل‌های متصل اعمال شود (چک‌لیست)
  • رویِ کدام نوعِ خرید اعمال شود: پلن‌های آمادهٔ فروش و/یا فروشِ آنلاینِ
    دلخواه (حجم/روزِ آزاد)

لیستِ همکارها مشترکِ کلِ ربات است (نه مخصوصِ یک پنلِ خاص).

اولویتِ تخفیف‌ها (وقتی چندتا هم‌زمان قابل‌اعمال باشند):
  کدِ تخفیفِ دستی  >  تخفیفِ همکار  >  تخفیفِ خودکارِ زیرمجموعه‌گیری/نفراتِ برتر
(یعنی تخفیفِ همکار با کدِ تخفیف ترکیب نمی‌شود، ولی روی تخفیف‌هایِ خودکارِ
عمومی اولویت دارد — چون مشخصاً برای همان شخص تنظیم شده.)

callbackها با پیشوند: vprt:
  vprt:list            — لیستِ همکارها + دکمهٔ افزودن
  vprt:add             — شروعِ افزودنِ همکارِ جدید (FSM: آیدی → درصد)
  vprt:view:{id}        — نمایش/ویرایشِ یک همکار
  vprt:toggle:{id}      — فعال/غیرفعالِ کلِ همکاری
  vprt:editpct:{id}     — شروعِ ویرایشِ درصدِ تخفیف
  vprt:scope:{id}:{s}   — تغییرِ محدودهٔ اعمال (all/plans/custom)
  vprt:panel:{id}:{key} — تیک‌زدن/برداشتنِ یک پنلِ خاص
  vprt:del:{id}         — درخواستِ حذف
  vprt:del_ok:{id}      — تأییدِ حذف
"""
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)  # PATCH_PANEL_MINIAPP_V1
from sqlalchemy import select

from config import Session
from models import VpnPartner
import secrets  # === PATCH_RESELLER_WEB_V1 ===

from config import WEBHOOK_BASE  # === PATCH_RESELLER_WEB_V1 ===
from tenant.bots.vpn.vpn_admin import (PANEL_LABELS, _owner, _owner_msg, cancel_kb,
                              fa_to_int, panel_label, rial)  # PATCH_RSME_PANEL_LABEL_FIX_V1
from tenant.gateways.discount_engine import DiscountResult, _calc_discount

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

vpn_partners_router = Router()


class VpnPartnerFSM(StatesGroup):
    telegram_id = State()
    percent = State()
    edit_percent = State()
    web_username = State()   # === PATCH_RESELLER_WEB_V1 ===
    trial_quota = State()    # === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
    panel_pct = State()      # === PATCH_PARTNER_PER_PANEL_DISCOUNT_V1 ===


# ─────────────── کمک‌توابعِ محاسبهٔ تخفیف (برایِ استفاده در vpn_user.py) ───────────────

async def get_partner(bot_id: int, telegram_id: int) -> VpnPartner | None:
    async with Session() as s:
        return await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.telegram_id == telegram_id,
            VpnPartner.active == True))


# === PATCH_PARTNER_NAV_SEND_DELETE_V1 ===
# ناوبریِ این بخش از «ویرایشِ پیام» به «حذف و ارسالِ تازه» عوض شد. دلیلش:
# ویرایش وقتی پیامِ قبلی مدیا/کپشن داشته باشد یا بیشتر از ۴۸ ساعت گذشته
# باشد شکست می‌خورد و کاربر گیر می‌کرد؛ ضمناً صفحهٔ تازه همیشه پایینِ چت
# می‌آید و لازم نیست کاربر اسکرول کند تا ببیند چه عوض شده.
async def _nav(cb: CallbackQuery, text: str, kb=None):
    """=== PATCH_GLASS_EDIT_IN_PLACE_V1 === صفحه رویِ همان پیام ویرایش می‌شود؛
    اگر ویرایش ممکن نبود (مدیا/قدیمی) مثلِ قبل حذف+ارسالِ تازه."""
    from tenant import edit_or_replace
    await edit_or_replace(cb.message, text, reply_markup=kb)


# === PATCH_PARTNER_PER_PANEL_DISCOUNT_V1 ===
# درخواستِ ادمین‌ها: «برایِ پاسارگاد ۱۰٪ و برایِ یوپنل ۲۵٪». درصدِ کلیِ
# نماینده سرِ جایش می‌ماند و فقط پنل‌هایی که عددِ جدا خورده‌اند از آن
# پیروی نمی‌کنند.
def _panel_discounts(p: VpnPartner) -> dict:
    raw = getattr(p, "panel_discounts", None)
    if not raw:
        return {}
    try:
        import json as _json
        d = _json.loads(raw) or {}
        return {str(k): int(v) for k, v in d.items()}
    except Exception:
        logger.exception("خواندنِ تخفیفِ پنلیِ نماینده ناموفق بود؛ درصدِ کلی اعمال شد")
        return {}


async def _set_panel_discount(partner_id: int, panel_key: str, pct: int | None) -> None:
    """درصدِ یک پنل را ثبت یا (با None) حذف می‌کند."""
    import json as _json
    async with Session() as s:
        p = await s.get(VpnPartner, partner_id)
        if not p:
            return
        d = _panel_discounts(p)
        if pct is None:
            d.pop(panel_key, None)
        else:
            d[panel_key] = max(0, min(100, int(pct)))
        p.panel_discounts = _json.dumps(d, ensure_ascii=False) if d else None
        await s.commit()


# === PATCH_PARTNER_PANEL_GB_PRICE_V1 ===
def _panel_gb_prices(p: VpnPartner) -> dict:
    raw = getattr(p, "panel_gb_prices", None)
    if not raw:
        return {}
    try:
        import json as _json
        return {str(k): int(v) for k, v in (_json.loads(raw) or {}).items() if int(v) > 0}
    except Exception:
        logger.exception("خواندنِ قیمتِ گیگیِ همکار ناموفق بود؛ درصد اعمال شد")
        return {}


async def _set_panel_gb_price(partner_id: int, panel_key: str, price: int | None) -> None:
    """قیمتِ هر گیگِ یک پنل را ثبت یا (با None) حذف می‌کند."""
    import json as _json
    async with Session() as s:
        p = await s.get(VpnPartner, partner_id)
        if not p:
            return
        d = _panel_gb_prices(p)
        if price is None or price <= 0:
            d.pop(panel_key, None)
        else:
            d[panel_key] = int(price)
        p.panel_gb_prices = _json.dumps(d, ensure_ascii=False) if d else None
        await s.commit()


async def partner_gb_price(bot_id: int, telegram_id: int, panel_key: str, scope: str) -> int:
    """قیمتِ هر گیگِ اختصاصیِ همکارِ دستی برایِ این پنل/نوعِ خرید؛ ۰ یعنی ندارد
    (همان قواعدِ پنل/محدودهٔ درصدِ دستی)."""
    if not panel_key:
        return 0
    p = await get_partner(bot_id, telegram_id)
    if not p:
        return 0
    if not getattr(p, "apply_all", False):
        if scope == "plans" and not p.apply_plans:
            return 0
        if scope == "custom" and not p.apply_custom:
            return 0
        if panel_key not in _partner_panels(p):
            return 0
    return int(_panel_gb_prices(p).get(panel_key) or 0)


def _partner_panels(partner: VpnPartner) -> set[str]:
    if not partner.panel_keys:
        return set()
    return {k.strip() for k in partner.panel_keys.split(",") if k.strip()}


def _percent_for_panel(p: VpnPartner, panel_key: str) -> int:
    """درصدِ این پنل: اگر عددِ اختصاصی داشته باشد همان، وگرنه درصدِ کلی."""
    per = _panel_discounts(p)
    if panel_key and panel_key in per:
        return max(0, min(100, per[panel_key]))
    return max(0, min(100, int(p.discount_percent or 0)))


async def _manual_partner_percent(bot_id: int, telegram_id: int, panel_key: str, scope: str) -> int:
    """درصدِ همکاریِ *دستی* (رکوردِ VpnPartner که ادمین ساخته)."""
    p = await get_partner(bot_id, telegram_id)
    if not p:
        return 0
    # === PATCH_RESELLER_MERGE_V1 ===
    # «همهٔ محصولات» یعنی هیچ محدودیتِ پنل/نوعِ خرید چک نشود.
    if getattr(p, "apply_all", False):
        return _percent_for_panel(p, panel_key)
    if scope == "plans" and not p.apply_plans:
        return 0
    if scope == "custom" and not p.apply_custom:
        return 0
    if panel_key and panel_key not in _partner_panels(p):
        return 0
    return _percent_for_panel(p, panel_key)


async def partner_discount_percent(bot_id: int, telegram_id: int, panel_key: str, scope: str,
                                   include_auto_reseller: bool = True) -> int:
    """درصدِ تخفیفِ خودکارِ این کاربر برای این پنل/نوعِ خرید؛ اگر هیچ‌کدام
    منطبق نبود، ۰. scope یکی از 'plans' یا 'custom' است.

    === PATCH_RESELLER_TIERS_V1 ===
    دو منبع دارد: همکاریِ *دستی* (ادمین آیدیِ شخص را ثبت کرده) و نمایندگیِ
    *خودکار* (کاربر با شارژ/خریدش به یک پله رسیده). بزرگ‌ترشان اعمال می‌شود
    و با هم جمع نمی‌شوند — وگرنه یک نمایندهٔ دستی که اتفاقاً پله را هم رد
    کرده بود، تخفیفِ دوبرابر می‌گرفت.

    عمداً همین‌جا ادغام شد، نه در تک‌تکِ نقاطِ محاسبهٔ قیمت: هر جایی که از
    قبل تخفیفِ همکار را می‌فهمید، حالا بدونِ تغییر نمایندگی را هم می‌فهمد.

    === PATCH_RESELLER_PANEL_ONLY_V1 ===
    درخواستِ ادمین: تخفیفِ خودکارِ نمایندگی فقط داخلِ خودِ «پنلِ نمایندگی»
    اعمال شود، نه وقتی همان نماینده از دکمه‌هایِ عادیِ خریدِ خودِ ربات
    استفاده می‌کند. برایِ همین include_auto_reseller اضافه شد — نقاطِ
    خریدِ عادیِ ربات (tenant/vpn_user.py) این را False می‌فرستند تا فقط
    تخفیفِ *دستیِ* همکاری (اگر ادمین جداگانه ثبت کرده باشد) اثر بگذارد،
    نه پلهٔ خودکارِ نمایندگی. خودِ پنلِ نمایندگی (web_panel.py،
    vpn_partners.py) هیچ تغییری نمی‌کند چون پیش‌فرض همچنان True است.
    """
    manual = await _manual_partner_percent(bot_id, telegram_id, panel_key, scope)
    if not include_auto_reseller:
        return manual
    try:
        from tenant.shared.reseller import reseller_percent
        # PATCH_RESELLER_AUTO_PER_PANEL_V1 — درصدِ همین پنل
        auto = await reseller_percent(bot_id, telegram_id, panel_key)
    except Exception:
        logger.exception("محاسبهٔ نمایندگیِ خودکار ناموفق بود؛ فقط همکاریِ دستی اعمال شد.")
        auto = 0
    return max(manual, auto)


async def partner_discount(*, bot_id: int, telegram_id: int, price, panel_key: str,
                            scope: str = "plans", include_auto_reseller: bool = True,
                            gb: float | None = None) -> DiscountResult:
    """مثلِ referral_discount در discount_engine ولی مبنایش تخفیفِ همکاری است.
    چیزی در دیتابیس ثبت نمی‌کند؛ فقط محاسبه می‌کند.

    === PATCH_RESELLER_PANEL_ONLY_V1 ===
    include_auto_reseller=False یعنی فقط تخفیفِ دستیِ همکاری حساب شود، نه
    پلهٔ خودکارِ نمایندگی — برایِ نقاطِ خریدِ عادیِ ربات."""
    price = int(round(float(price)))
    # === PATCH_PARTNER_PANEL_GB_PRICE_V1 === قیمتِ گیگیِ اختصاصی به‌جایِ درصد (پلن‌هایِ حجمی)
    try:
        _gbp = await partner_gb_price(bot_id, telegram_id, panel_key, scope)
        _gb = float(gb or 0)
    except Exception:
        logger.exception("قیمتِ گیگیِ همکار خوانده نشد؛ درصد اعمال شد")
        _gbp, _gb = 0, 0.0
    if _gbp > 0 and _gb > 0:
        final = min(price, int(round(_gb * _gbp)))
        if final >= price:
            return DiscountResult(ok=False, final_price=price,
                                  reason="قیمتِ گیگیِ همکار از قیمتِ عادی کمتر نیست")
        return DiscountResult(ok=True, discount=price - final, final_price=final,
                              code=None, disc_type="amount")
    pct = await partner_discount_percent(bot_id, telegram_id, panel_key, scope,
                                         include_auto_reseller=include_auto_reseller)
    if pct <= 0:
        return DiscountResult(ok=False, final_price=price, reason="همکاری/نمایندگیِ فعالی برای این کاربر نیست")
    discount = _calc_discount("percent", pct, 0, price)
    return DiscountResult(ok=True, discount=discount, final_price=price - discount,
                          code=None, disc_type="percent")


# ─────────────── نمایشِ متن/کیبوردِ یک همکار ───────────────

async def _partner_text(p: VpnPartner, connected_keys: list[str]) -> str:
    status = "🟢 فعال" if p.active else "🔴 غیرفعال"
    panels = _partner_panels(p)
    panels_txt = "، ".join(PANEL_LABELS.get(k, k) for k in panels if k in connected_keys) or "— هیچ‌کدام (هنوز تیک نخورده)"
    return (
        f"🤝 <b>حساب همکار</b>\n\n"
        f"آیدی عددی: <code>{p.telegram_id}</code>\n"
        + (f"یادداشت: {p.note}\n" if p.note else "")
        + f"وضعیت: {status}\n"
        f"درصد تخفیف: <b>{p.discount_percent}٪</b>\n\n"
        + ("اعمال روی: <b>همهٔ محصولاتِ ربات</b> (بقیهٔ تیک‌ها نادیده گرفته می‌شوند)\n\n"
           if getattr(p, "apply_all", False) else "")
        + "اعمال روی:\n"
        f"  {'✅' if p.apply_plans else '❌'} پلن‌های آمادهٔ فروش\n"
        f"  {'✅' if p.apply_custom else '❌'} فروش آنلاین دلخواه (حجم/روز آزاد)\n\n"
        f"پنل‌های مشمول: {panels_txt}\n\n"
        "💡 نکته: اگر همکار برای هیچ پنل/محدوده‌ای تیک نخورده باشد، عملاً "
        "هیچ تخفیفی اعمال نمی‌شود؛ از دکمه‌های زیر تنظیم کن."
        + await _plan_coverage_warning(p)
    )


async def _plan_coverage_warning(p: VpnPartner) -> str:
    """=== PATCH_PARTNER_PLAN_COVERAGE_WARNING_V1 ===
    باگِ گزارش‌شده: «تخفیف رویِ فروشِ آنلاین اعمال می‌شود ولی رویِ پلن‌های
    آماده نه». علتش این است که چکِ پنل در partner_discount_percent فقط وقتی
    اجرا می‌شود که panel_key خالی نباشد:

        if panel_key and panel_key not in _partner_panels(p): return 0

    در فروشِ آنلاین، panel_key اغلب رشتهٔ خالی است (کاربر پنلی انتخاب
    نکرده) پس چک رد می‌شود و تخفیف می‌آید؛ ولی پلنِ آماده همیشه panel_key
    دارد، پس اگر آن پنل تیک نخورده باشد تخفیف بی‌صدا صفر می‌شود. ادمین
    هیچ نشانه‌ای نمی‌دید و فکر می‌کرد قابلیت خراب است.

    این تابع دقیقاً می‌گوید کدام پنل‌ها پلنِ فعال دارند ولی تیک نخورده‌اند.
    """
    if getattr(p, "apply_all", False):
        return ""
    if not p.apply_plans:
        return ("\n\n⚠️ «پلن‌های آمادهٔ فروش» برای این همکار خاموش است، پس "
                "رویِ پلن‌ها هیچ تخفیفی اعمال نمی‌شود.")
    from models import VpnPlan
    ticked = _partner_panels(p)
    async with Session() as s:
        keys = list((await s.scalars(select(VpnPlan.panel_key).where(
            VpnPlan.bot_id == p.bot_id, VpnPlan.active == True).distinct())).all())
    missing = sorted({k for k in keys if k and k not in ticked})
    if not missing:
        return ""
    names = "، ".join(PANEL_LABELS.get(k, k) for k in missing)
    return ("\n\n⚠️ <b>این پنل‌ها پلنِ فعال دارند ولی تیک نخورده‌اند:</b> "
            f"{names}\n"
            "یعنی تخفیفِ این همکار رویِ پلن‌های آمادهٔ آن‌ها اعمال <b>نمی‌شود</b> "
            "(هرچند ممکن است رویِ فروشِ آنلاین اعمال شود). از چک‌لیستِ پایین "
            "تیکشان بزن یا «✅ همهٔ پنل‌ها».")


async def _partner_kb(p: VpnPartner, connected: list, origin: str = "vpn") -> InlineKeyboardMarkup:
    from tenant import ibtn_style as _ibs
    connected_keys = [c.panel_key for c in connected]
    panels = _partner_panels(p)
    rows = [
        [InlineKeyboardButton(text="✏️ تغییر درصد تخفیف", callback_data=f"vprt:editpct:{p.id}",
                              style=await _ibs(p.bot_id, "ibtn:vpn_setup"))],
        # === PATCH_RESELLER_WEB_V1 ===
        [InlineKeyboardButton(
            text=("🖥 دسترسیِ پنلِ وب: 🟢 فعال" if getattr(p, "web_enabled", False)
                  else "🖥 دادنِ دسترسیِ پنلِ وب"),
            callback_data=f"vprt:web:{p.id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(
            text=f"{'✅' if getattr(p, 'apply_all', False) else '❌'} رویِ همهٔ محصولاتِ ربات",
            callback_data=f"vprt:scope:{p.id}:all")],  # PATCH_RESELLER_MERGE_V1
        [InlineKeyboardButton(
            text=f"{'✅' if p.apply_plans else '❌'} پلن‌های آمادهٔ فروش",
            callback_data=f"vprt:scope:{p.id}:plans"),
         InlineKeyboardButton(
            text=f"{'✅' if p.apply_custom else '❌'} فروش دلخواه",
            callback_data=f"vprt:scope:{p.id}:custom")],
    ]
    if not connected:
        rows.append([InlineKeyboardButton(text="⚠️ هنوز هیچ پنلی متصل نشده", callback_data="vprt:noop")])
    else:
        # === PATCH_PARTNER_DEFAULT_PANELS_V1 ===
        # اگه فعلاً هیچ پنلی تیک نخورده (مثلاً همکارهایِ قدیمی‌ای که قبل از
        # این پچ ساخته شدن)، یه دکمهٔ رفعِ سریع بذار — تا مجبور نباشه
        # تک‌تکِ پنل‌ها رو دستی تیک بزنه.
        if not panels:
            rows.append([InlineKeyboardButton(
                text="✅ فعال‌کردنِ سریع رویِ همهٔ پنل‌ها", callback_data=f"vprt:allpanels:{p.id}",
                style=ButtonStyle.SUCCESS)])
        # === PATCH_PARTNER_PER_PANEL_DISCOUNT_V1 ===
        per = _panel_discounts(p)
        gbp_map = _panel_gb_prices(p)
        for c in connected:
            key = c.panel_key
            label = await panel_label(p.bot_id, key)
            mark = "☑️" if key in panels else "◻️"
            btns = [InlineKeyboardButton(
                text=f"{mark} {label}", callback_data=f"vprt:panel:{p.id}:{key}")]
            if key in panels:
                _pct = per.get(key)
                _gbp = gbp_map.get(key)
                btns.append(InlineKeyboardButton(
                    text=(f"🏷 {_gbp:,}ت/گیگ" if _gbp else
                          f"🏷 {_pct}٪" if _pct is not None
                          else f"🏷 {int(p.discount_percent or 0)}٪ (کلی)"),
                    callback_data=f"vprt:pctpanel:{p.id}:{key}"))
            rows.append(btns)
    rows.append([InlineKeyboardButton(
        text=("🔴 غیرفعال کردن همکاری" if p.active else "🟢 فعال کردن همکاری"),
        callback_data=f"vprt:toggle:{p.id}",
        style=await _ibs(p.bot_id, "ibtn:vpn_toggle"))])
    rows.append([InlineKeyboardButton(text="🗑 حذف همکار", callback_data=f"vprt:del:{p.id}",
                                      style=await _ibs(p.bot_id, "ibtn:vpn_delete"))])
    # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
    # origin رو حفظ می‌کنیم تا وقتی از اینجا برگردیم به لیست، دکمهٔ
    # «بازگشت»یِ خودِ لیست هم بدونه باید کجا برگرده (VPN خودکار یا نمایندگی).
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به لیست همکارها", callback_data=f"vprt:list:{origin}",
                                      style=await _ibs(p.bot_id, "ibtn:back") or ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _connected_panels(bot_id: int):
    from models import VpnPanel
    async with Session() as s:
        return list((await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id))).all())


async def _show_partner(target, bot_id: int, partner_id: int, *, edit: bool = True, origin: str = "vpn"):
    async with Session() as s:
        p = await s.get(VpnPartner, partner_id)
    if not p or p.bot_id != bot_id:
        return False
    connected = await _connected_panels(bot_id)
    text = await _partner_text(p, [c.panel_key for c in connected])
    kb = await _partner_kb(p, connected, origin=origin)
    # === PATCH_PARTNER_NAV_SEND_DELETE_V1 === حذف و ارسالِ تازه، به‌جای ویرایش
    if isinstance(target, CallbackQuery):
        await _nav(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)
    return True


# ─────────────── لیستِ همکارها ───────────────

async def _partner_list_text_kb(row, origin: str = "vpn") -> tuple[str, InlineKeyboardMarkup]:
    """PATCH_VPN_PARTNER_REPLY_KB_V1 — منطقِ ساختِ متن/کیبوردِ لیستِ همکارها،
    جدا از callback، تا هم دکمه‌ی inline (vprt:list) هم دکمه‌ی کیبوردیِ
    ریپلای («🤝 حساب‌های همکار (تخفیف)») بتونن ازش استفاده کنن.

    origin مشخص می‌کنه از کجا اومدیم («vpn» = منویِ VPN خودکار، «rsl» =
    هابِ نمایندگیِ خودکار) — تا دکمه‌ی «بازگشت» درست بدونه کجا برگرده،
    به‌جایِ حدس‌زدن از رویِ پلنِ ربات (که وقتی هر دو مسیر برایِ یه پلن
    ممکن بودن، همیشه اشتباه می‌رفت)."""
    async with Session() as s:
        partners = (await s.scalars(select(VpnPartner).where(
            VpnPartner.bot_id == row.id).order_by(VpnPartner.id))).all()
    from tenant import ibtn_style as _ibs
    # === PATCH_PARTNER_PLAN_COVERAGE_WARNING_V1 ===
    from models import VpnPlan
    async with Session() as s:
        _plan_keys = {k for k in (await s.scalars(select(VpnPlan.panel_key).where(
            VpnPlan.bot_id == row.id, VpnPlan.active == True).distinct())).all() if k}
    rows = []
    for p in partners:
        icon = "🟢" if p.active else "🔴"
        note = f" ({p.note})" if p.note else ""
        broken = (not getattr(p, "apply_all", False)) and (
            (not _partner_panels(p)) or not (p.apply_plans or p.apply_custom)
            or (p.apply_plans and bool(_plan_keys - _partner_panels(p))))
        warn = "⚠️ " if (p.active and broken) else ""
        rows.append([InlineKeyboardButton(
            text=f"{warn}{icon} {p.telegram_id}{note} — {p.discount_percent}٪",
            callback_data=f"vprt:view:{p.id}:{origin}")])
    rows.append([InlineKeyboardButton(text="➕ افزودن همکار جدید", callback_data="vprt:add",
                                      style=await _ibs(row.id, "ibtn:vpn_setup"))])
    # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
    # قبلاً این‌جا به‌جایِ دنبال‌کردنِ مسیرِ واقعی، از رویِ پلنِ ربات حدس زده
    # می‌شد که آیا کاربر از «نمایندگی» اومده — که برایِ پلن‌هایِ حرفه‌ای
    # همیشه رد می‌شد به rsl:menu، حتی وقتی واقعاً از «VPN خودکار» اومده
    # بودن (باگِ گزارش‌شده: «بازگشت میره به نمایندگیِ خودکار»).
    _back_cb = "rsl:menu" if origin == "rsl" else "tvpn:menu"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back_cb,
                                      style=await _ibs(row.id, "ibtn:back") or ButtonStyle.DANGER)])
    text = (
        "🤝 <b>حساب‌های همکار</b>\n\n"
        "همکارهایی که ثبت می‌کنی، بدونِ نیاز به کدِ تخفیف، با درصدِ دلخواهِ "
        "خودشون خرید می‌کنن.\n\nهمکارهای فعلی:"
        if partners else
        "🤝 <b>حساب‌های همکار</b>\n\n"
        "هنوز همکاری ثبت نکردی. با دکمهٔ زیر آیدیِ عددیِ تلگرامِ همکار رو "
        "اضافه کن و براش درصدِ تخفیف تعیین کن."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@vpn_partners_router.callback_query(F.data.startswith("vprt:list"))
async def vprt_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    origin = parts[2] if len(parts) > 2 else "vpn"  # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
    text, kb = await _partner_list_text_kb(row, origin)
    await _nav(cb, text, kb)
    await cb.answer()


@vpn_partners_router.callback_query(F.data == "vprt:noop")
async def vprt_noop(cb: CallbackQuery):
    await cb.answer("اول از منویِ اصلیِ VPN یک پنل وصل کن.", show_alert=True)


# ─────────────── افزودنِ همکارِ جدید (FSM) ───────────────

@vpn_partners_router.callback_query(F.data == "vprt:add")
async def vprt_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.set_state(VpnPartnerFSM.telegram_id)
    await cb.message.answer(
        "🆔 آیدی عددیِ تلگرامِ همکار رو بفرست.\n\n"
        "💡 اگه آیدی عددی رو نمی‌دونی، همکار می‌تونه با ارسالِ پیام به رباتی "
        "مثل @userinfobot آیدیِ عددیِ خودش رو ببینه.",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_partners_router.message(VpnPartnerFSM.telegram_id, F.text)
async def vprt_add_id(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n <= 0:
        await msg.answer("آیدیِ عددیِ معتبر بفرست (فقط رقم):", reply_markup=cancel_kb())
        return
    async with Session() as s:
        exists = await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == row.id, VpnPartner.telegram_id == n))
    if exists:
        await state.clear()
        await msg.answer("⚠️ این آیدی قبلاً به‌عنوانِ همکار ثبت شده. از لیست ویرایشش کن:")
        await _show_partner(msg, row.id, exists.id, edit=False)
        return
    await state.update_data(prt_telegram_id=n)
    await state.set_state(VpnPartnerFSM.percent)
    await msg.answer("چند درصد تخفیف بگیره؟ (عدد بین ۱ تا ۱۰۰، مثلاً 30):", reply_markup=cancel_kb())


@vpn_partners_router.message(VpnPartnerFSM.percent, F.text)
async def vprt_add_percent(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n <= 0 or n > 100:
        await msg.answer("عددی بینِ ۱ تا ۱۰۰ بفرست:", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    tg_id = data.get("prt_telegram_id")
    await state.clear()
    # === PATCH_PARTNER_DEFAULT_PANELS_V1 ===
    # قبلاً همکارِ تازه‌ساخته‌شده panel_keys=None می‌گرفت (یعنی رویِ هیچ
    # پنلی فعال نبود تا ادمین دستی تیک بزنه) — این باعث می‌شد ادمین فکر
    # کنه همکار رو ساخته و تمومه، ولی چون این قدمِ اضافه رو ندیده بود،
    # مشتری هیچ‌وقت تخفیف نمی‌دید و به نظر می‌رسید یه باگه. الان پیش‌فرض،
    # همه‌ی پنل‌هایِ فعلاً متصل‌شده رو تیک‌خورده می‌ذاریم (رایج‌ترین انتظار:
    # «به این آدم تخفیف بده»، نه یه قدمِ پنهانِ اضافه).
    connected = await _connected_panels(row.id)
    default_keys = ",".join(sorted(c.panel_key for c in connected)) if connected else None
    async with Session() as s:
        p = VpnPartner(bot_id=row.id, telegram_id=tg_id, discount_percent=n,
                       apply_plans=True, apply_custom=False, panel_keys=default_keys, active=True)
        s.add(p)
        await s.commit()
        pid = p.id
    await msg.answer(
        f"✅ همکار <code>{tg_id}</code> با {n}٪ تخفیف اضافه شد و رویِ همه‌ی "
        f"پنل‌هایِ فعلاً متصل فعال شد (برایِ «پلن‌های آمادهٔ فروش»).\n\n"
        "اگه می‌خوای رویِ پنلِ خاصی خاموشش کنی، یا برایِ «فروشِ آنلاینِ دلخواه» "
        "هم فعالش کنی، از دکمه‌های زیر:")
    await _show_partner(msg, row.id, pid, edit=False)


# ─────────────── نمایش/ویرایشِ یک همکار ───────────────

@vpn_partners_router.callback_query(F.data.startswith("vprt:view:"))
async def vprt_view(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    pid = int(parts[2])
    origin = parts[3] if len(parts) > 3 else "vpn"  # === PATCH_VPN_PARTNER_REPLY_KB_V1 ===
    if not await _show_partner(cb, row.id, pid, edit=True, origin=origin):
        await cb.answer("همکار یافت نشد.", show_alert=True)
        return
    await cb.answer()


@vpn_partners_router.callback_query(F.data.startswith("vprt:toggle:"))
async def vprt_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if p and p.bot_id == row.id:
            p.active = not p.active
            await s.commit()
    await cb.answer()
    await _show_partner(cb, row.id, pid, edit=True)


@vpn_partners_router.callback_query(F.data.startswith("vprt:scope:"))
async def vprt_scope(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, pid_s, scope = cb.data.split(":")
    pid = int(pid_s)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if p and p.bot_id == row.id:
            if scope == "plans":
                p.apply_plans = not p.apply_plans
            elif scope == "custom":
                p.apply_custom = not p.apply_custom
            elif scope == "all":  # === PATCH_RESELLER_MERGE_V1 ===
                p.apply_all = not bool(getattr(p, "apply_all", False))
            await s.commit()
    await cb.answer()
    await _show_partner(cb, row.id, pid, edit=True)


@vpn_partners_router.callback_query(F.data.startswith("vprt:panel:"))
async def vprt_panel_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, pid_s, key = cb.data.split(":")
    pid = int(pid_s)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if p and p.bot_id == row.id:
            keys = _partner_panels(p)
            if key in keys:
                keys.discard(key)
            else:
                keys.add(key)
            p.panel_keys = ",".join(sorted(keys)) if keys else None
            await s.commit()
    await cb.answer()
    await _show_partner(cb, row.id, pid, edit=True)


# === PATCH_PARTNER_DEFAULT_PANELS_V1 ===
@vpn_partners_router.callback_query(F.data.startswith("vprt:allpanels:"))
async def vprt_all_panels(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    connected = await _connected_panels(row.id)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if p and p.bot_id == row.id:
            p.panel_keys = ",".join(sorted(c.panel_key for c in connected)) if connected else None
            await s.commit()
    await cb.answer("✅ رویِ همهٔ پنل‌ها فعال شد.")
    await _show_partner(cb, row.id, pid, edit=True)


@vpn_partners_router.callback_query(F.data.startswith("vprt:editpct:"))
async def vprt_editpct_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("همکار یافت نشد.", show_alert=True)
        return
    await state.update_data(prt_edit_id=pid)
    await state.set_state(VpnPartnerFSM.edit_percent)
    await cb.message.answer(
        f"درصدِ تخفیفِ فعلی: {p.discount_percent}٪\nعددِ جدید (۱ تا ۱۰۰) رو بفرست:",
        reply_markup=cancel_kb())
    await cb.answer()


@vpn_partners_router.message(VpnPartnerFSM.edit_percent, F.text)
async def vprt_editpct_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    n = fa_to_int(msg.text.strip())
    if n is None or n <= 0 or n > 100:
        await msg.answer("عددی بینِ ۱ تا ۱۰۰ بفرست:", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    pid = data.get("prt_edit_id")
    await state.clear()
    async with Session() as s:
        p = await s.get(VpnPartner, pid) if pid else None
        if p and p.bot_id == row.id:
            p.discount_percent = n
            await s.commit()
    if pid:
        await msg.answer(f"✅ درصدِ تخفیف به {n}٪ تغییر کرد.")
        await _show_partner(msg, row.id, pid, edit=False)


# ─────────────── حذفِ همکار ───────────────

@vpn_partners_router.callback_query(F.data.startswith("vprt:del:"))
async def vprt_del_confirm(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("همکار یافت نشد.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"vprt:del_ok:{pid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"vprt:view:{pid}")],
    ])
    await _nav(cb, f"مطمئنی می‌خوای همکاریِ <code>{p.telegram_id}</code> حذف بشه؟", kb)
    await cb.answer()


@vpn_partners_router.callback_query(F.data.startswith("vprt:del_ok:"))
async def vprt_del_ok(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if p and p.bot_id == row.id:
            await s.delete(p)
            await s.commit()
    await cb.answer("همکار حذف شد.")
    await vprt_list(cb, bot)


# ═══════════ PATCH_RESELLER_WEB_V1 — دسترسیِ نماینده به پنلِ وب ═══════════
# مالک این‌جا برایِ نماینده یوزرنیم می‌سازد و ربات یک رمزِ تصادفی می‌دهد.
# رمز فقط همان یک‌بار نشان داده می‌شود و بعدش فقط هشش می‌ماند — حتی خودِ
# مالک هم دیگر نمی‌تواند ببیندش، فقط می‌تواند رمزِ تازه بسازد.

@vpn_partners_router.callback_query(F.data.startswith("vprt:web:"))
async def vprt_web_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    from plans import Feature as _F, has_feature as _hf
    if not _hf(row.plan, _F.WEB_PANEL):
        await cb.answer("پنلِ تحت وب فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    on = bool(getattr(p, "web_enabled", False))
    uname = p.web_username or "—"
    # === PATCH_PANEL_CUSTOM_DOMAIN_V1 === دامنهٔ اختصاصیِ ربات، اگر دارد
    from tenant.gateways.custom_domain import get_bot_webhook_base
    base = (await get_bot_webhook_base(row.id) or WEBHOOK_BASE or "").rstrip("/")
    body = (
        f"🖥 <b>پنلِ وبِ نماینده</b> — <code>{p.telegram_id}</code>\n\n"
        f"وضعیت: {'🟢 فعال' if on else '🔴 غیرفعال'}\n"
        f"نام کاربری: <code>{uname}</code>\n"
        f"تخفیفِ او: <b>{p.discount_percent}٪</b>\n\n"
        # === PATCH_PANEL_MINIAPP_V1 ===
        # آدرس دیگر به‌صورتِ متن چاپ نمی‌شود تا در فوروارد/اسکرین‌شات لو
        # نرود؛ به‌جایش دکمهٔ «بازکردنِ پنل» پایینِ همین پیام است که پنل را
        # داخلِ خودِ تلگرام باز می‌کند.
        "آدرسِ ورود: با دکمهٔ پایین باز می‌شود.\n\n"
        "با این دسترسی، نماینده می‌تواند از پنل‌هایی که برایش تیک زده‌ای کانفیگ "
        "بسازد و هزینه‌اش <b>از کیفِ پولِ خودش در همین ربات</b> کم می‌شود "
        "(با همان درصدِ تخفیفِ بالا). به تنظیمات، آدرس یا کلیدِ API پنل "
        "دسترسی ندارد.\n\n"
        "⚠️ اگر کیفِ پولش خالی باشد، چیزی نمی‌تواند بسازد.\n\n"
        # === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
        f"🎁 سهمیهٔ تستِ رایگانِ روزانه: <b>{int(getattr(p, 'trial_daily_limit', 0) or 0)}</b>"
        + ("  (غیرفعال)" if not int(getattr(p, "trial_daily_limit", 0) or 0) else "")
    )
    rows = []
    # === PATCH_PANEL_MINIAPP_V1 ===
    if base.startswith("https://"):
        rows.append([InlineKeyboardButton(
            text="🖥 بازکردنِ پنلِ نماینده",
            web_app=WebAppInfo(url=f"{base}/panel/r/login"))])
    rows.append([InlineKeyboardButton(
        text=("✏️ تغییرِ نام کاربری" if p.web_username else "➕ ساختِ نام کاربری"),
        callback_data=f"vprt:webuser:{pid}", style=ButtonStyle.PRIMARY)])
    if p.web_username:
        rows.append([InlineKeyboardButton(text="🔑 ساختِ رمزِ تازه",
                                          callback_data=f"vprt:webpass:{pid}",
                                          style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(
            text=("🔴 قطعِ دسترسی" if on else "🟢 فعال‌کردنِ دسترسی"),
            callback_data=f"vprt:webtog:{pid}",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))])
    # === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
    rows.append([InlineKeyboardButton(
        text="🎁 سهمیهٔ تستِ روزانه", callback_data=f"vprt:trialq:{pid}",
        style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"vprt:view:{pid}",
                                      style=ButtonStyle.DANGER)])
    await _nav(cb, body, InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# === PATCH_PARTNER_PER_PANEL_DISCOUNT_V1 ===
@vpn_partners_router.callback_query(F.data.startswith("vprt:pctpanel:"))
async def vprt_panel_pct_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """تعیینِ درصدِ تخفیفِ جدا برایِ یک پنلِ خاصِ همین همکار."""
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, pid_s, key = cb.data.split(":", 3)
    pid = int(pid_s)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    cur = _panel_discounts(p).get(key)
    _cur_gbp = _panel_gb_prices(p).get(key)
    label = await panel_label(row.id, key)
    await state.set_state(VpnPartnerFSM.panel_pct)
    await state.update_data(vprt_pct_pid=pid, vprt_pct_key=key)
    await cb.answer()
    await cb.message.answer(
        f"🏷 <b>تخفیفِ {label}</b>\n\n"
        f"درصدِ فعلیِ این پنل: <b>{cur if cur is not None else int(p.discount_percent or 0)}٪</b>"
        + ("" if cur is not None else "  (از درصدِ کلی می‌آید)") + "\n"
        f"درصدِ کلیِ این همکار: <b>{int(p.discount_percent or 0)}٪</b>\n\n"
        + (f"قیمتِ گیگیِ فعلی: <b>{_cur_gbp:,} تومان/گیگ</b> (به‌جایِ درصد)\n" if _cur_gbp else "")
        + "\n"
        "• <b>درصد</b>: عددِ ۰ تا ۱۰۰ بفرست (مثلاً <code>20</code>).\n"
        "• <b>قیمتِ هر گیگ</b>: عدد را با «ت» بفرست (مثلاً <code>1000ت</code>)؛ "
        "عددِ بالایِ ۱۰۰ هم قیمتِ گیگ حساب می‌شود. پلن‌هایِ این پنل برایِ همکار "
        "«حجم × این قیمت» حساب می‌شوند (هیچ‌وقت گران‌تر از قیمتِ عادی نمی‌شود) و در فروشِ دلخواه "
        "همین قیمتِ گیگ است.\n\n"
        "برایِ برداشتنِ عددِ اختصاصی (درصد و قیمتِ گیگ) و برگشت به درصدِ کلی، کلمهٔ "
        "<code>حذف</code> را بفرست.",
        reply_markup=cancel_kb())


@vpn_partners_router.message(VpnPartnerFSM.panel_pct, F.text)
async def vprt_panel_pct_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pid, key = data.get("vprt_pct_pid"), data.get("vprt_pct_key")
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if raw in ("حذف", "-"):
        await state.clear()
        await _set_panel_discount(pid, key, None)
        await _set_panel_gb_price(pid, key, None)
        await msg.answer("✅ عددِ اختصاصیِ این پنل برداشته شد؛ از این پس "
                         "درصدِ کلیِ همکار اعمال می‌شود.")
        return
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    # === PATCH_PARTNER_PANEL_GB_PRICE_V1 === «1000ت» / «گیگی 1000» / عددِ بالایِ ۱۰۰ = قیمتِ هر گیگ
    is_gb = any(t in raw for t in ("ت", "گیگ", "تومان"))
    num = "".join(ch for ch in raw if ch.isdigit())
    if not num:
        await msg.answer("درصد (۰ تا ۱۰۰) یا قیمتِ هر گیگ با «ت» (مثلاً <code>1000ت</code>) بفرست:")
        return
    if is_gb or int(num) > 100:
        await state.clear()
        await _set_panel_gb_price(pid, key, int(num))
        label = await panel_label(row.id, key)
        await msg.answer(f"✅ رویِ <b>{label}</b> برایِ این همکار هر گیگ <b>{int(num):,}</b> تومان حساب می‌شود "
                         "(به‌جایِ درصد). پلن‌هایِ نامحدود همچنان با درصد حساب می‌شوند.")
        return
    raw = num
    await state.clear()
    await _set_panel_gb_price(pid, key, None)
    await _set_panel_discount(pid, key, int(raw))
    label = await panel_label(row.id, key)
    await msg.answer(f"✅ تخفیفِ <b>{label}</b> برایِ این همکار روی "
                     f"<b>{int(raw)}٪</b> تنظیم شد.")


# === PATCH_RESELLER_TRIAL_QUOTA_V1 ===
@vpn_partners_router.callback_query(F.data.startswith("vprt:trialq:"))
async def vprt_trial_quota_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """تعیینِ سهمیهٔ روزانهٔ تستِ رایگانِ این نماینده."""
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
    if not p or p.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    cur = int(getattr(p, "trial_daily_limit", 0) or 0)
    await state.set_state(VpnPartnerFSM.trial_quota)
    await state.update_data(vprt_trialq_pid=pid)
    await cb.answer()
    await cb.message.answer(
        "🎁 <b>سهمیهٔ تستِ روزانه</b>\n\n"
        f"مقدارِ فعلی: <b>{cur}</b> تست در شبانه‌روز\n\n"
        "عددِ تازه را بفرست. مثلاً <code>5</code> یعنی این نماینده هر روز "
        "می‌تواند تا ۵ کانفیگِ تست برای مشتری‌هایش بسازد. سهمیه هر شبانه‌روز "
        "از نو پر می‌شود.\n\n"
        "عددِ <code>0</code> یعنی اجازهٔ تست ندارد.\n\n"
        "ℹ️ مشخصاتِ خودِ تست (حجم و مدت) همان تنظیماتِ تستِ رایگانِ ربات است.",
        reply_markup=cancel_kb())


@vpn_partners_router.message(VpnPartnerFSM.trial_quota, F.text)
async def vprt_trial_quota_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("vprt_trialq_pid")
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if raw in ("❌ لغو", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not raw.isdigit() or int(raw) > 500:
        await msg.answer("عدد نامعتبر است. یک عددِ بین ۰ تا ۵۰۰ بفرست:")
        return
    await state.clear()
    val = int(raw)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if not p or p.bot_id != row.id:
            await msg.answer("نماینده پیدا نشد.")
            return
        p.trial_daily_limit = val
        await s.commit()
    await msg.answer(
        f"✅ سهمیهٔ تستِ روزانهٔ این نماینده روی <b>{val}</b> تنظیم شد."
        + ("\n\nیعنی فعلاً اجازهٔ ساختِ تست ندارد." if val == 0 else
           "\n\nاز تبِ «تست رایگان» در پنلِ وبش می‌تواند استفاده کند."))


@vpn_partners_router.callback_query(F.data.startswith("vprt:webuser:"))
async def vprt_web_user_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    await state.set_state(VpnPartnerFSM.web_username)
    await state.update_data(vprt_web_pid=pid)
    await cb.answer()
    await cb.message.answer(
        "نام کاربریِ نماینده را بفرست (حروفِ انگلیسی، عدد و ـ ، بینِ ۳ تا ۳۲ نویسه):",
        reply_markup=cancel_kb())


@vpn_partners_router.message(VpnPartnerFSM.web_username, F.text)
async def vprt_web_user_save(msg: Message, bot: Bot, state: FSMContext):
    import re as _re
    row = await _owner_msg(msg, bot)
    if not row:
        return
    uname = (msg.text or "").strip()
    if uname in ("لغو", "❌ لغو", "/cancel"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not _re.fullmatch(r"[A-Za-z0-9_]{3,32}", uname):
        await msg.answer("فقط حروفِ انگلیسی، عدد و زیرخط — بینِ ۳ تا ۳۲ نویسه. دوباره بفرست:")
        return
    data = await state.get_data()
    pid = data.get("vprt_web_pid")
    await state.clear()
    if not pid:
        return
    from tenant.shared.web_panel import hash_password
    pw = secrets.token_urlsafe(9)
    async with Session() as s:
        # نام کاربری در کلِ سامانه یکتاست، چون ورود بدونِ انتخابِ ربات است.
        clash = await s.scalar(select(VpnPartner).where(
            VpnPartner.web_username == uname, VpnPartner.id != pid))
        if clash:
            await msg.answer("این نام کاربری قبلاً گرفته شده. یکی دیگر انتخاب کن.")
            return
        p = await s.get(VpnPartner, pid)
        if not p or p.bot_id != row.id:
            return
        p.web_username = uname
        p.web_password_hash = hash_password(pw)
        p.web_enabled = True
        p.web_fail_count = 0
        p.web_locked_until = None
        await s.commit()
        tg = p.telegram_id
    # === PATCH_PANEL_CUSTOM_DOMAIN_V1 ===
    from tenant.gateways.custom_domain import get_bot_webhook_base
    base = (await get_bot_webhook_base(row.id) or WEBHOOK_BASE or "").rstrip("/")
    # === PATCH_PANEL_MINIAPP_V1 === آدرس به‌جای متن، داخلِ دکمه می‌رود
    _kb = None
    if base.startswith("https://"):
        _kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🖥 بازکردنِ پنلِ نماینده",
            web_app=WebAppInfo(url=f"{base}/panel/r/login"))]])
    await msg.answer(
        f"✅ دسترسیِ پنلِ وب برایِ <code>{tg}</code> ساخته و فعال شد.\n\n"
        f"👤 نام کاربری: <code>{uname}</code>\n"
        f"🔑 رمز: <code>{pw}</code>\n\n"
        "پنل با دکمهٔ پایین داخلِ خودِ تلگرام باز می‌شود؛ آدرسی جایی نوشته "
        "نمی‌شود تا با فوروارد یا اسکرین‌شات لو نرود.\n\n"
        "⚠️ این رمز <b>فقط همین یک‌بار</b> نمایش داده می‌شود — بعدش فقط هشش "
        "ذخیره می‌ماند و حتی من هم نمی‌توانم نشانش بدهم. اگر گم شد، از همان "
        "صفحه «ساختِ رمزِ تازه» بزن.", reply_markup=_kb)


@vpn_partners_router.callback_query(F.data.startswith("vprt:webpass:"))
async def vprt_web_newpass(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    from tenant.shared.web_panel import hash_password
    pw = secrets.token_urlsafe(9)
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if not p or p.bot_id != row.id or not p.web_username:
            await cb.answer("اول نام کاربری بساز.", show_alert=True)
            return
        p.web_password_hash = hash_password(pw)
        p.web_fail_count = 0
        p.web_locked_until = None
        await s.commit()
        uname = p.web_username
    await cb.answer("رمزِ تازه ساخته شد")
    await cb.message.answer(
        f"🔑 رمزِ تازه برایِ <code>{uname}</code>:\n\n<code>{pw}</code>\n\n"
        "⚠️ فقط همین یک‌بار نمایش داده می‌شود. رمزِ قبلی از همین لحظه باطل است.")


@vpn_partners_router.callback_query(F.data.startswith("vprt:webtog:"))
async def vprt_web_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(VpnPartner, pid)
        if not p or p.bot_id != row.id:
            return
        p.web_enabled = not bool(getattr(p, "web_enabled", False))
        on = p.web_enabled
        await s.commit()
    await cb.answer("🟢 فعال شد" if on else "🔴 قطع شد")
    # CallbackQuery غیرقابلِ‌تغییر (frozen) است؛ `cb.data = ...` ValidationError می‌داد و منو تازه نمی‌شد
    await vprt_web_menu(cb.model_copy(update={"data": f"vprt:web:{pid}"}), bot)


# ═══════════ PATCH_RESELLER_MENU_BUTTON_V1 — صفحهٔ «پنل نمایندگی من» ═══════════
# این صفحه را خودِ نماینده می‌بیند (نه مالکِ ربات): خلاصهٔ تخفیف، پنل‌هایی
# که اجازه دارد، سهمیهٔ تست، کیفِ پول، و دکمهٔ بازکردنِ مینی‌اپ.

async def _rsme_ctx(bot_id: int, tg_id: int):
    """(نماینده، موجودیِ کیف پول، پنل‌ها) یا None اگر نمایندهٔ فعالی نباشد."""
    from models import BotUser, VpnPanel
    async with Session() as s:
        p = await s.scalar(select(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.telegram_id == tg_id))
        if not p or not p.active:
            return None
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.enabled == True))).all()
    return p, (int(u.balance or 0) if u else 0), panels


async def _rsme_render(bot_id: int, row, tg_id: int):
    """متن و کیبوردِ صفحهٔ نمایندگی."""
    got = await _rsme_ctx(bot_id, tg_id)
    if not got:
        return None, None
    p, balance, panels = got
    allowed = _partner_panels(p)
    if getattr(p, "apply_all", False):
        names = "همهٔ پنل‌ها"
    else:
        # === PATCH_RSME_PANEL_LABEL_FIX_V1 ===
        # باگِ ERR-131402: مدلِ VpnPanel اصلاً ستونِ title ندارد و این خط با
        # AttributeError می‌افتاد. نامِ نمایشی از panel_label می‌آید که نامِ
        # دلخواهِ مالکِ ربات را هم رعایت می‌کند.
        _names = []
        for pl in panels:
            if pl.panel_key in allowed:
                _names.append(await panel_label(bot_id, pl.panel_key))
        names = "، ".join(_names) or "— هنوز پنلی برایت تیک نخورده"
    quota = int(getattr(p, "trial_daily_limit", 0) or 0)
    web_on = bool(p.web_enabled and p.web_username)

    txt = (
        "🏷 <b>پنل نمایندگی من</b>\n\n"
        f"💰 کیف پول: <b>{balance:,}</b> تومان\n"
        f"🏷 تخفیف نمایندگی: <b>{int(p.discount_percent or 0)}٪</b>\n"
        f"🖥 پنل‌های مجاز: {names}\n"
        f"🎁 سهمیهٔ تست روزانه: <b>{quota}</b>"
        + ("  (غیرفعال)" if not quota else "") + "\n\n"
        "تخفیفت روی خریدهای خودت در همین ربات هم به‌صورت خودکار اعمال "
        "می‌شود — نه فقط در پنل تحت وب.\n\n"
    )
    rows = []
    if web_on:
        from tenant.gateways.custom_domain import get_bot_webhook_base
        from config import WEBHOOK_BASE as _WB
        base = (await get_bot_webhook_base(bot_id) or _WB or "").rstrip("/")
        if base.startswith("https://"):
            # === PATCH_PANEL_MINIAPP_V1 === بدونِ چاپِ آدرس در چت
            rows.append([InlineKeyboardButton(
                text="🖥 بازکردن پنل تحت وب",
                web_app=WebAppInfo(url=f"{base}/panel/r/login"))])
            txt += ("در پنل تحت وب می‌توانی پلن آماده بسازی، فروش آنلاین "
                    "(حجم و روز دلخواه) بزنی و تست بگیری.\n"
                    f"👤 نام کاربری‌ات: <code>{p.web_username}</code>")
        else:
            txt += "⚠️ آدرس امنِ پنل تنظیم نشده؛ به مدیر ربات بگو."
    else:
        txt += ("هنوز دسترسی پنل تحت وب برایت ساخته نشده. از مدیر ربات "
                "بخواه نام کاربری و رمز برایت بسازد.")
    # === PATCH_RESELLER_MY_CONFIGS_V1 ===
    # نماینده تا حالا برایِ دیدنِ کانفیگ‌هایی که ساخته باید از منویِ اصلی
    # سراغِ «سرویس‌های من» می‌رفت. همان صفحه از قبل همهٔ کارها را دارد
    # (QR، ساب‌لینک، تمدید، افزایش حجم، تغییرِ لینک، تغییرِ نام، توقف،
    # حذف، نمایشِ سه‌بعدیِ وب، آموزشِ اتصال)، پس به‌جایِ ساختنِ نسخهٔ دومِ
    # آن — که یعنی دو جا برایِ نگهداری و دو رفتارِ متفاوت — از همین‌جا به
    # همان صفحه وصل می‌شویم.
    rows.append([InlineKeyboardButton(text="📱 کانفیگ‌های ساخته‌شده",
                                      callback_data="rsme:cfgs",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🔄 بروزرسانی",
                                      callback_data="rsme:home",
                                      style=ButtonStyle.PRIMARY)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_RESELLER_MENU_BUTTON_V2 ===
# این تابع عمداً هندلر نیست: متنِ دکمه‌هایِ کیبوردی در tenant/__init__.py
# توسطِ یک دیسپچرِ واحد گرفته می‌شود که زودتر از این روتر اجرا می‌شود، پس
# هر @message(F.text == ...) اینجا هیچ‌وقت اجرا نمی‌شد (باگِ نسخهٔ V1:
# حالتِ شیشه‌ای کار می‌کرد و کیبوردی نه). حالا همان دیسپچر این را صدا می‌زند.
async def rsme_show(msg: Message, bot: Bot) -> None:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    txt, kb = await _rsme_render(row.id, row, msg.from_user.id)
    if not txt:
        await msg.answer("نمایندگیِ فعالی برای تو ثبت نیست.")
        return
    await msg.answer(txt, reply_markup=kb)


# === PATCH_RESELLER_MY_CONFIGS_V1 ===
@vpn_partners_router.callback_query(F.data == "rsme:cfgs")
async def rsme_configs(cb: CallbackQuery, bot: Bot):
    """فهرستِ کانفیگ‌هایی که این نماینده ساخته — به‌صورتِ دکمهٔ شیشه‌ای."""
    from tenant import get_bot_row
    from tenant.bots.vpn.vpn_user import show_reseller_configs, mark_svc_from_reseller
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await _rsme_ctx(row.id, cb.from_user.id):
        await cb.answer("نمایندگیِ فعالی برای تو ثبت نیست.", show_alert=True)
        return
    try:
        await cb.message.delete()     # همان حالتِ حذف‌و‌ارسالِ این بخش
    except Exception:
        logger.debug("حذفِ صفحهٔ نمایندگی ممکن نشد (بی‌اهمیت)")
    # === PATCH_RESELLER_SVC_BACK_V1 === تا دکمهٔ بازگشت به همین‌جا برگردد
    mark_svc_from_reseller(row.id, cb.from_user.id, True)
    await show_reseller_configs(bot, cb.message, row.id, cb.from_user.id)
    await cb.answer()


@vpn_partners_router.callback_query(F.data == "rsme:home")
async def rsme_home_cb(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    from tenant.bots.vpn.vpn_user import mark_svc_from_reseller
    row = await get_bot_row(bot)
    if row:
        # === PATCH_RESELLER_SVC_BACK_V1 === برگشتیم؛ پرچم دیگر لازم نیست
        mark_svc_from_reseller(row.id, cb.from_user.id, False)
    if not row:
        await cb.answer()
        return
    txt, kb = await _rsme_render(row.id, row, cb.from_user.id)
    if not txt:
        await cb.answer("نمایندگیِ فعالی برای تو ثبت نیست.", show_alert=True)
        return
    await _nav(cb, txt, kb)
    await cb.answer()
