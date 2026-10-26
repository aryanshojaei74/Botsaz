"""
smm.py — فیچر پنل SMM با API استاندارد برای ربات‌های «خدمات مجازی».

قابلیت‌ها:
  • اتصال به پنل SMM با آدرس + API Key (چند‌مستأجری؛ هر ربات پنل خودش).
  • نمایش موجودی حساب پنل به ادمین.
  • دریافت سرویس‌های پنل و انتخاب/فعال‌سازی آن‌ها برای فروش.
  • دو حالت قیمت‌گذاری (در تنظیمات، انتخابی):
        margin → قیمت فروش = قیمت پنل × (۱ + درصد سود)
        manual → قیمت هر سرویس دستی وارد می‌شود.
  • دو حالت ثبت سفارش (در تنظیمات، انتخابی):
        auto   → لحظهٔ پرداختِ کاربر، سفارش مستقیم روی پنل ثبت می‌شود.
        manual → سفارش برای ادمین می‌آید و با تأیید او روی پنل ثبت می‌شود.
  • کسر هزینه از کیف پول کاربر و بازگشت وجه در صورت خطا/رد.
  • پیگیری خودکار وضعیت سفارش‌ها از پنل (poller) و اطلاع به کاربر.

تنظیمات ذخیره‌شده (Setting) با پیشوند smm_:
    smm_api_url, smm_api_key
    smm_price_mode  = margin | manual   (پیش‌فرض margin)
    smm_margin_pct  = عدد (درصد سود، پیش‌فرض 30)
    smm_toman_per_unit = نرخ تبدیل واحدِ پولِ پنل به تومان (برای حالت margin)
    smm_order_mode  = auto | manual     (پیش‌فرض manual)
"""

from datetime import datetime  # PATCH_VS_ONBOARDING_V1
import json  # === PATCH_SMM_PLATFORM_RENAME_V1 ===
import re  # === PATCH_STAR_REACTION_V1 ===
import asyncio  # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===
import types  # === PATCH_VS_MINIAPP_ORDER_V1 ===
# === PATCH_SMM_PROVIDER_DESC_V1 ===
# توضیحاتِ محصول از سرورِ ارائه‌دهنده می‌آید (متنِ کنترل‌نشده) و داخلِ پیامی
# با parse_mode="HTML" نمایش داده می‌شود — پس باید escape شود، وگرنه یک
# علامتِ < در متنِ پنل کلِ پیامِ محصول را برایِ مشتری خراب می‌کند.
from html import escape as _html_escape
from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler  # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select, update

from config import Session
from models import Bot as BotRow, BotUser, SmmService, SmmOrder, SmmPanel, SmmCategory, WalletTxn, TelegramGiftConfig, GiftOrder, VnumNumber, MainButton
from models import order_no  # شناسهٔ واحدِ سفارش
from models import NftOrder, NftItem  # === PATCH_VS_UNIFIED_ORDERS_V1 ===
from tenant.bots.virtual_service import smm_api
from tenant.bots.virtual_service import smm_link_types   # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
from tenant.bots.virtual_service import smspool_api  # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
from plans import has_feature, Feature  # === PATCH_SMM_ONE_ENTRY_V1 ===
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

smm_router = Router()
from aiogram.types import LinkPreviewOptions as _LPO
_LPO_ON = _LPO(is_disabled=False)   # PATCH_NO_LINK_PREVIEW_V1: پیش‌نمایشِ لینک فقط برایِ گیفتِ NFT/سفارشی
# === PATCH_VSO_ALL_KINDS_V1 === روترِ «راه‌اندازیِ حرفه‌ایِ ورود» برایِ همهٔ انواعِ ربات (VPN هم) ثبت می‌شود
vso_router = Router()


# === PATCH_SMM_API_ACCESS_V1 ===
async def _require_smm_api_access(cb: CallbackQuery, row) -> bool:
    """گیتِ سمتِ سرور برایِ Feature.SMM_API_ACCESS (پلنِ رشد به بالا).
    گذاشتنِ دکمه پشتِ ok() تو منو کافی نیست — چون callback_data می‌تونه
    از یه کیبوردِ قدیمی/کش‌شده (قبلِ داون‌گرید شدنِ پلن) دوباره کلیک بشه؛
    این تابع همون چک رو رویِ خودِ هندلر هم تکرار می‌کنه. خروجی False یعنی
    قفله و پیامِ مناسب هم فرستاده شده — فراخوان باید فوراً return کنه."""
    if has_feature(row.plan, Feature.SMM_API_ACCESS):
        return True
    await cb.answer(
        "🔒 اتصال به ارائه‌دهنده/پنل فقط از پلنِ رشد به بالا فعاله.\n"
        "پلنِ رایگان فقط «سرویس‌های دستی» رو داره. برایِ ارتقا، از داخلِ "
        "بات‌ساز به «🗂 ربات‌های من» برو.", show_alert=True)
    return False


async def _delete_shop_panel(cb: CallbackQuery) -> None:
    """حذف امن پیام‌های میانی خرید؛ رسید نهایی دست‌نخورده می‌ماند."""
    try:
        await cb.message.delete()
    except Exception:
        logger.debug("shop panel delete skipped", exc_info=True)


# === PATCH_VS_EDIT_IN_PLACE_V1 ===
class _EditFirstV:
    """رپرِ cb.message: اولین `.answer(text, ...)` به‌جایِ حذفِ پیام و ارسالِ پیامِ تازه،
    *همان پیام* را ویرایش می‌کند (سریع‌تر، بدونِ پرشِ چت). پیامِ دوم به بعد و هر ویژگیِ
    دیگر عادی به پیامِ اصلی می‌رود. اگر ویرایش ممکن نبود (پیامِ عکس‌دار، قدیمی، کیبوردِ
    غیرِ اینلاین) همان رفتارِ قبلی: حذفِ پیامِ قدیمی + ارسالِ جدید."""
    __slots__ = ("_m", "_used")

    def __init__(self, msg):
        self._m = msg
        self._used = False

    def __getattr__(self, name):
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
                logger.debug("vs: حذفِ پیامِ قدیمی ممکن نشد (بی‌اهمیت)")
        return await self._m.answer(text, **kw)


# پرچمِ اجرای یک‌بارهٔ migration در طول عمر پروسه
_smm_migrated = {"done": False}


async def ensure_smm_columns(force: bool = False):
    """اطمینان از وجود ستون‌های جدیدِ جدول‌های SMM در دیتابیس (migration مطمئن).

    این تابع هم در startup و هم در ابتدای هندلرهای SMM صدا زده می‌شود تا حتی اگر
    ربات ری‌استارت هم نشده باشد، اولین کلیک روی هر دکمهٔ SMM ستون‌های لازم را
    بسازد و از خطای «no such column: is_manual» جلوگیری کند. با پرچم داخلی فقط
    یک‌بار در هر اجرای پروسه واقعاً کار می‌کند.

    دیتابیس‌مستقل است: برای تشخیص ستون‌های موجود از inspect استفاده می‌کند (نه
    PRAGMA که فقط SQLite است)، پس روی PostgreSQL/MySQL هم درست کار می‌کند.
    """
    if _smm_migrated["done"] and not force:
        return
    from sqlalchemy import text, inspect as _sa_inspect
    from models import Base

    # ۱) مطمئن شو جدول‌ها وجود دارند
    try:
        async with Session() as s:
            conn = await s.connection()
            await conn.run_sync(Base.metadata.create_all)
            await s.commit()
    except Exception as e:
        print("ensure_smm_columns create_all:", e)

    # ستون‌هایی که ممکن است در دیتابیس‌های قدیمی نباشند (نوع سازگار با SQLite/PG/MySQL)
    wanted = {
        "smm_services": [
            ("is_manual", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
            # === PATCH_SMM_MANUAL_SORT_ORDER_V1 ===
            ("sort_order", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
            # === PATCH_SMM_STORED_CLASSIFICATION_V1 ===
            ("platform_key", "VARCHAR(30)", "VARCHAR(30)"),
            ("type_key", "VARCHAR(30)", "VARCHAR(30)"),
            # === PATCH_SMM_PROVIDER_DESC_V1 ===
            ("provider_desc", "TEXT", "TEXT"),
            # === PATCH_SMM_DESC_TRANSLATE_V1 ===
            ("provider_desc_fa", "TEXT", "TEXT"),
            ("provider_desc_fa_hash", "VARCHAR(16)", "VARCHAR(16)"),
            # === PATCH_SMM_INPUT_KIND_V1 ===
            ("input_kind", "VARCHAR(10) DEFAULT ''", "VARCHAR(10) DEFAULT ''"),
        ],
        "smm_orders": [("panel_status", "VARCHAR(30)", "VARCHAR(30)")],
        # === PATCH_SMM_CATEGORY_DESC_V1 ===
        "smm_categories": [("description", "TEXT", "TEXT")],
        # === PATCH_SMM_PROVIDER_DETAIL_V1 ===
        "smm_panels": [
            ("auto_check_enabled", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
            ("last_check_ok", "BOOLEAN", "BOOLEAN"),
            ("last_check_at", "DATETIME", "TIMESTAMP"),
            ("last_balance", "NUMERIC(14, 2)", "NUMERIC(14, 2)"),
            ("last_currency", "VARCHAR(20) DEFAULT ''", "VARCHAR(20) DEFAULT ''"),
            # === PATCH_SMM_PANEL_CURRENCY_V1 ===
            ("currency", "VARCHAR(10) DEFAULT 'usd'", "VARCHAR(10) DEFAULT 'usd'"),
        ],
    }

    def _get_existing(sync_conn, table):
        try:
            insp = _sa_inspect(sync_conn)
            if table not in insp.get_table_names():
                return None
            return {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return set()

    try:
        async with Session() as s:
            conn = await s.connection()
            is_sqlite = conn.dialect.name == "sqlite"
            for table, cols in wanted.items():
                existing = await conn.run_sync(_get_existing, table)
                if existing is None:
                    continue  # جدول وجود ندارد (create_all باید ساخته باشد)
                for col, ddl_sqlite, ddl_other in cols:
                    if col in existing:
                        continue
                    ddl = ddl_sqlite if is_sqlite else ddl_other
                    try:
                        await conn.execute(text(
                            f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'))
                        print(f"[SMM] migrated: {table}.{col}")
                    except Exception as e:
                        print(f"[SMM] migrate skip {table}.{col}:", e)
            await s.commit()
    except Exception as e:
        print("ensure_smm_columns alter phase:", e)

    _smm_migrated["done"] = True


class SmmFSM(StatesGroup):
    premium_frag_usd = State()   # PATCH_PREMIUM_FRAGMENT_PRICE_V1
    api_url = State()
    api_key = State()
    margin_pct = State()
    toman_rate = State()
    manual_price = State()
    minmax = State()                # PATCH_SMM_MINMAX_V1 — حداقل/حداکثرِ سفارش      # قیمت دستیِ یک سرویس
    # === PATCH_SMM_RENAME_SERVICE_V1 ===
    rename_service = State()    # ادمین اسمِ نمایشیِ فارسیِ یک سرویس را می‌نویسد
    # === PATCH_SMM_SERVICE_DESCRIPTION_V1 ===
    desc_service = State()      # ادمین توضیحاتِ یک سرویس را می‌نویسد
    add_service_id = State()    # ادمین شناسهٔ سرویس پنل را وارد می‌کند
    search_query = State()      # ادمین عبارت جستجوی سرویس را وارد می‌کند
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    panel_name = State()        # ادمین اسمِ دلخواهِ پنلِ جدید را می‌نویسد
    panel_url = State()
    panel_key = State()
    # === PATCH_SMM_PROVIDER_DETAIL_V1 ===
    panel_edit_name = State()   # ویرایشِ نامِ یک ارائه‌دهندهٔ موجود
    panel_edit_url = State()    # ویرایشِ لینکِ (URL) یک ارائه‌دهندهٔ موجود
    panel_edit_key = State()    # ویرایشِ کلید (Api Key) یک ارائه‌دهندهٔ موجود
    # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
    cat_name = State()          # ادمین اسمِ دستهٔ جدید را می‌نویسد (بعدِ انتخابِ پلتفرم)
    cat_margin = State()        # ادمین درصدِ سودِ مخصوصِ یک دسته را می‌نویسد
    cat_rename = State()        # ادمین اسمِ جدیدِ یک دستهٔ موجود را می‌نویسد
    cat_desc = State()          # PATCH_SMM_CATEGORY_DESC_V1: ادمین توضیحات مشترک دسته را می‌نویسد
    # === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
    cat_linkpattern = State()  # ادمین نمونه‌لینکِ این دسته را می‌نویسد
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    plat_rename = State()       # ادمین اسمِ دلخواهِ جدیدِ یک پلتفرم را می‌نویسد
    man_name = State()
    man_price = State()
    man_min = State()
    man_max = State()
    user_link = State()         # کاربر لینک می‌فرستد
    user_link_confirm = State() # پیش‌نمایش و تأیید لینک
    user_qty = State()          # کاربر تعداد می‌فرستد
    user_qty_confirm = State()  # پیش‌نمایش و تأیید تعداد
    # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
    user_disc_code = State()    # کاربر کدِ تخفیف را برایِ سفارشِ تکی وارد می‌کند
    # === PATCH_SMM_BULK_ORDER_V1 ===
    bulk_links = State()    # کاربر چند لینک (هر خط یکی) می‌فرستد
    bulk_qty = State()      # تعداد مشترک برایِ همهٔ لینک‌ها
    # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
    bulk_disc_code = State()  # کاربر کدِ تخفیف را برایِ سفارشِ انبوه وارد می‌کند
    # === PATCH_VS_ONBOARDING_V1 ===
    rules_text = State()        # ادمین متنِ قوانین را می‌نویسد
    welcome_text = State()      # ادمین متنِ خوش‌آمدِ تایپ‌شونده را می‌نویسد
    # === PATCH_TELEGRAM_GIFTS_V1 ===
    gift_rate = State()         # ادمین نرخِ تومان به‌ازایِ هر استارز را می‌نویسد
    gift_margin = State()       # ادمین درصدِ سود را می‌نویسد
    gift_dest_username = State()  # مشتری یوزرنیمِ مقصد را می‌نویسد
    gift_comment = State()        # PATCH_GIFT_COMMENT_V1 مشتری کامنتِ گیفت را می‌نویسد
    sfirst_text = State()         # PATCH_SUPPORT_FIRST_V1 متنِ «اول به پشتیبانی پیام بده»
    # === PATCH_VNUM_TOGGLE_V1 ===
    vnum_add_name = State()     # ادمین اسمِ کشور/نوعِ شماره را می‌نویسد
    vnum_add_price = State()    # ادمین قیمتِ آن را می‌نویسد
    # === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
    vnum_manual_number = State()  # ادمین شمارهٔ واقعی را برایِ سفارشِ دستی می‌نویسد
    # === PATCH_TELEGRAM_PREMIUM_V1 ===
    premium_rate = State()      # ادمین نرخِ تومان به‌ازایِ هر استارز (پرمیوم) را می‌نویسد
    premium_margin = State()    # ادمین درصدِ سود (پرمیوم) را می‌نویسد
    premium_dest_username = State()  # مشتری یوزرنیمِ مقصد را می‌نویسد
    # === PATCH_PREMIUM_CUSTOM_PRICE_V1 ===
    premium_custom_price = State()   # ادمین قیمتِ شخصیِ تومانیِ یک پلن را می‌نویسد
    # === PATCH_GIFT_CARD_PAYMENT_V1 ===
    gift_receipt_photo = State()  # مشتری عکسِ رسیدِ کارت‌به‌کارت را می‌فرستد
    # === PATCH_STARS_SELL_V1 ===
    stars_qty_in = State()          # مشتری تعدادِ استارز را می‌نویسد
    stars_dest_username = State()   # مشتری یوزرنیمِ مقصد را می‌نویسد
    stars_receipt_photo = State()   # مشتری عکسِ رسیدِ کارت‌به‌کارت را می‌فرستد
    stars_rate_in = State()         # ادمین نرخِ هر استارز را می‌نویسد
    stars_margin_in = State()       # ادمین درصدِ سودِ نرخِ خودکار را می‌نویسد
    stars_min_in = State()          # ادمین حداقلِ تعداد را می‌نویسد
    stars_max_in = State()          # ادمین حداکثرِ تعداد را می‌نویسد
    tgtools_key_in = State()        # === PATCH_TGTOOLS_STARS_V1 === مالک کلیدِ API سایتِ TGTools را می‌فرستد
    # === PATCH_STAR_REACTION_V1 ===
    srx_qty_in = State()            # مشتری تعدادِ ری‌اکشنِ استارزی را می‌نویسد
    srx_link_in = State()           # مشتری لینکِ پست را می‌نویسد
    srx_rate_in = State()           # ادمین نرخِ هر ری‌اکشن‌استارز را می‌نویسد
    stars_base_in = State()         # قیمتِ پایهٔ ⭐ به دلار / نرخِ دستیِ دلار
    srx_margin_in = State()
    srx_min_in = State()            # ادمین حداقلِ تعداد را می‌نویسد
    # === PATCH_VNUM_REAL_NUMBERS_V1 ===
    vnum_number_add = State()       # ادمین شمارهٔ واقعی را می‌نویسد (می‌تواند چندتا با خط جدید باشد)
    vnum_code_reply = State()       # ادمین کدِ SMS را برایِ مشتری تایپ می‌کند
    # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===
    vnum_smspool_key = State()      # ادمین API Key سرویسِ SMSPool را می‌نویسد
    vnum_auto_add_name = State()    # ادمین اسمِ نمایشیِ کشور را می‌نویسد
    vnum_auto_add_country = State()  # ادمین شناسهٔ کشور در SMSPool را می‌نویسد
    vnum_auto_add_service = State()  # ادمین شناسهٔ سرویس در SMSPool را می‌نویسد
    vnum_auto_add_price = State()   # ادمین قیمتِ فروش را می‌نویسد
    vnum_auto_svc_search = State()  # ادمین کلیدواژهٔ جستجویِ سرویس را می‌نویسد
    # === PATCH_MANUAL_GIFT_V1 ===
    manual_gift_link = State()   # ادمین لینکِ گیفتِ سفارشی را می‌فرستد
    manual_gift_name = State()   # ادمین اسمِ گیفتِ سفارشی را می‌فرستد
    manual_gift_price = State()  # ادمین قیمتِ گیفتِ سفارشی را می‌فرستد
    # === PATCH_TON_SELL_V1 === فروشِ تون (TON) — همیشه دستی، مثلِ فروشِ استارز
    ton_amount_in = State()       # مشتری مقدارِ تون را می‌نویسد (اعشاری مجاز)
    ton_wallet_in = State()       # مشتری آدرسِ کیف‌پولِ TON مقصد را می‌فرستد
    ton_memo_in = State()         # PATCH_TON_MEMO_V1 — کامنت/ممو (اختیاری)
    ton_rate_in = State()         # ادمین نرخِ هر ۱ تون به تومان را می‌نویسد
    ton_margin_in = State()       # PATCH_AUTO_RATE_TON_V1 — درصدِ سودِ نرخِ خودکار
    ton_min_in = State()          # ادمین حداقلِ مقدار را می‌نویسد
    ton_max_in = State()          # ادمین حداکثرِ مقدار را می‌نویسد


# ═══════════════════════ کمکی‌ها ═══════════════════════

async def _get_setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set_setting(bot_id: int, key: str, value: str):
    from tenant import set_setting
    await set_setting(bot_id, key, value)


# === PATCH_SMM_MULTI_PANEL_V1 ===
async def _panel_creds(bot_id: int, panel_pk: int | None) -> tuple[str, str, str]:
    """اطلاعاتِ اتصالِ یک پنل را برمی‌گرداند: (api_url, api_key, نام).

    panel_pk=None یعنی همان پنلِ پیش‌فرضِ قدیمی (Setting.smm_api_url/key) —
    برایِ سازگاری با ربات‌هایی که این فیچر رو نداشتن. اگر panel_pk مقدار
    داشته باشد ولی پنل پیدا نشود یا غیرفعال باشد، خالی برمی‌گردد (تا
    فراخوان به‌جایِ کرش، پیامِ «اتصال ناموفق» بدهد)."""
    if panel_pk is None:
        api_url = await _get_setting(bot_id, "smm_api_url", "")
        api_key = await _get_setting(bot_id, "smm_api_key", "")
        return api_url, api_key, "پنل پیش‌فرض"
    async with Session() as s:
        p = await s.get(SmmPanel, panel_pk)
    if not p or p.bot_id != bot_id:
        return "", "", "—"
    return p.api_url, p.api_key, p.name


# === PATCH_SMM_BULK_STATUS_V1 ===
async def _panel_bulk_enabled(bot_id: int, panel_pk: int | None) -> bool:
    """آیا برای این پنل استعلامِ گروهیِ وضعیت فعاله؟"""
    if panel_pk is None:
        return (await _get_setting(bot_id, "smm_default_panel_bulk_status", "0")) == "1"
    async with Session() as s:
        p = await s.get(SmmPanel, panel_pk)
    return bool(p and p.bot_id == bot_id and p.bulk_status)


async def _panel_set_bulk_enabled(bot_id: int, panel_pk: int | None, value: bool) -> None:
    if panel_pk is None:
        await _set_setting(bot_id, "smm_default_panel_bulk_status", "1" if value else "0")
        return
    async with Session() as s:
        p = await s.get(SmmPanel, panel_pk)
        if p and p.bot_id == bot_id:
            p.bulk_status = value
            await s.commit()


# === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
# نگاشتِ هر تنظیمِ per-panel به کلیدِ Settingِ قدیمی (برایِ پنلِ پیش‌فرض،
# panel_pk=None) و ستونِ متناظرش رویِ SmmPanel (برایِ پنل‌هایِ اضافه).
_PANEL_SETTING_DEFAULTS: dict[str, tuple[str, str]] = {
    "price_mode":     ("smm_price_mode", "margin"),
    "margin_pct":     ("smm_margin_pct", "30"),
    "rate_mode":      ("smm_rate_mode", "manual"),
    "toman_per_unit": ("smm_toman_per_unit", "0"),
    "order_mode":     ("smm_order_mode", "manual"),
    "fail_policy":    ("smm_fail_policy", "auto_refund"),
    # === PATCH_SMM_PRICE_ROUND_V1 ===
    "price_round":    ("smm_price_round", "0"),
    # === PATCH_SMM_PANEL_CURRENCY_V1 ===
    # واحدِ پولی که panel_rate در آن گزارش می‌شود. بدونِ این فیلد، وقتی
    # rate_mode روی «خودکار» بود، نرخِ زندهٔ دلار همیشه اعمال می‌شد —
    # حتی برایِ پنل‌هایی که خودشان از ابتدا قیمت را به تومان می‌دادند؛
    # نتیجه یک تورمِ چند صدهزار برابری در قیمتِ نهایی بود (مثلاً
    # panel_rate=3,150,000 تومان × نرخِ زندهٔ دلار به‌اشتباه). این فیلد
    # صریحاً مشخص می‌کند پنل دلاری است یا تومانی، مستقل از اینکه
    # rate_mode دستی است یا خودکار.
    "currency":       ("smm_panel_currency", "usd"),
}


async def _panel_setting_str(bot_id: int, panel_pk: int | None, attr: str) -> str:
    legacy_key, default = _PANEL_SETTING_DEFAULTS[attr]
    if panel_pk is None:
        return await _get_setting(bot_id, legacy_key, default)
    async with Session() as s:
        p = await s.get(SmmPanel, panel_pk)
    if not p or p.bot_id != bot_id:
        return default
    val = getattr(p, attr, None)
    return str(val) if val is not None else default


async def _panel_setting_float(bot_id: int, panel_pk: int | None, attr: str) -> float:
    try:
        return float(await _panel_setting_str(bot_id, panel_pk, attr))
    except (TypeError, ValueError):
        legacy_key, default = _PANEL_SETTING_DEFAULTS[attr]
        return float(default)


async def _panel_set_setting(bot_id: int, panel_pk: int | None, attr: str, value) -> None:
    legacy_key, _default = _PANEL_SETTING_DEFAULTS[attr]
    if panel_pk is None:
        await _set_setting(bot_id, legacy_key, str(value))
        return
    async with Session() as s:
        p = await s.get(SmmPanel, panel_pk)
        if p and p.bot_id == bot_id:
            setattr(p, attr, value)
            await s.commit()


async def _panel_price_mode(bot_id: int, panel_pk: int | None) -> str:
    return await _panel_setting_str(bot_id, panel_pk, "price_mode")


async def _panel_margin_pct(bot_id: int, panel_pk: int | None) -> float:
    return await _panel_setting_float(bot_id, panel_pk, "margin_pct")


async def _panel_rate_mode(bot_id: int, panel_pk: int | None) -> str:
    return await _panel_setting_str(bot_id, panel_pk, "rate_mode")


# === PATCH_SMM_PANEL_CURRENCY_V1 ===
async def _panel_currency(bot_id: int, panel_pk: int | None) -> str:
    """واحدِ پولی که panel_rate در آن گزارش می‌شود: 'usd' یا 'toman'.
    این مستقل از rate_mode است — حتی در حالتِ «خودکار»، اگر پنل تومانی
    باشد، نرخِ زندهٔ دلار نباید رویِ قیمتش اعمال شود."""
    val = (await _panel_setting_str(bot_id, panel_pk, "currency") or "usd").strip().lower()
    return "toman" if val == "toman" else "usd"


async def _panel_toman_per_unit(bot_id: int, panel_pk: int | None) -> float:
    return await _panel_setting_float(bot_id, panel_pk, "toman_per_unit")


async def _panel_order_mode(bot_id: int, panel_pk: int | None) -> str:
    return await _panel_setting_str(bot_id, panel_pk, "order_mode")


async def _panel_fail_policy(bot_id: int, panel_pk: int | None) -> str:
    return await _panel_setting_str(bot_id, panel_pk, "fail_policy")


# === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
async def _browse_by_provider_category(bot_id: int) -> bool:
    """گزارشِ ادمین (فدایی): موقعِ افزودنِ محصول، دسته‌بندیِ خودِ ارائه‌دهنده
    نشان داده نمی‌شد، فقط حدسِ خودکار. این تنظیمِ سراسریِ رباتی است (نه
    مخصوصِ یک پنل)، چون هدفش فقط تغییرِ ظاهرِ فهرست است، نه رفتارِ خودِ پنل."""
    return await _get_setting(bot_id, "smm_browse_provider_cat", "0") == "1"


async def _group_for_admin(bot_id: int, services: list) -> dict:
    """پوششِ نازک روی smm_api.group_services برایِ رعایتِ تنظیمِ بالا در
    همهٔ نقاطِ ورودیِ «افزودنِ محصول» با یک تغییر، بدونِ نیاز به این‌که هر
    فراخوانی جدا تنظیم را بخواند."""
    return smm_api.group_services(services or [], by_category=await _browse_by_provider_category(bot_id))


# === PATCH_SMM_PRICE_ROUND_V1 ===
async def _panel_price_round(bot_id: int, panel_pk: int | None) -> int:
    try:
        return int(await _panel_setting_str(bot_id, panel_pk, "price_round"))
    except (TypeError, ValueError):
        return 0


def _round_price(price: float, unit: int) -> int:
    """قیمت را به نزدیک‌ترین مضربِ unit گرد می‌کند (اگر unit<=0، بدون تغییر).

    === PATCH_SMM_TINY_PRICE_FLOOR_V1 ===
    گزارشِ ادمین (فدایی): سفارش‌هایِ زیرِ ۱۰۰ تومان خطایِ «قیمتِ این محصول تنظیم نشده»
    می‌دادند و «حداقلِ سفارش» صفر تومان نشان داده می‌شد. علت: گردکردن به نزدیک‌ترین
    مضربِ گام (مثلاً ۱۰۰) قیمتِ ۳۵ تومان را به صفر می‌رساند، و قیمتِ صفر یعنی
    «قیمت تنظیم نشده». حالا اگر قیمتِ واقعی مثبت بود ولی گردکردن آن را صفر کرد،
    همان قیمتِ دقیق (حداقل ۱ تومان) گرفته می‌شود. قیمتِ صفرِ واقعی (سرویسِ بی‌قیمت)
    عمداً صفر می‌ماند تا هشدارِ «تنظیم نشده» برایِ آن‌ها بماند."""
    raw = float(price or 0)
    price = int(round(raw))
    if unit <= 0:
        return _positive_floor(raw, price)
    out = int(round(price / unit)) * unit
    if out <= 0:
        return _positive_floor(raw, price)
    return out


def _positive_floor(raw: float, rounded: int) -> int:
    """اگر قیمتِ خام مثبت است ولی عددِ گردشده صفر شد، حداقل ۱ تومان؛ وگرنه همان عدد."""
    if raw > 0 and rounded <= 0:
        return 1
    return rounded


# === PATCH_SMM_NAV_FIX_V1 ===
# ادمین‌ها گزارش کردن که تویِ زیرمنویِ سرویس‌ها، دکمهٔ «بازگشت» گاهی یه
# صفحهٔ نامرتبط باز می‌کنه. علتش این بود: مرورِ سرویس‌ها (services→plat→
# typ→add→confirm) از دو تا هابِ متفاوت قابلِ ورود شده (smm:menu، یا
# smm:panels) ولی دکمه‌ی «بازگشتِ» بالاترین سطح (smm_services) همیشه یه
# مقصدِ ثابت داشت. چون این چند لایه از طریقِ کلیک‌هایِ جدا روی
# callback_dataهایِ ثابت طی می‌شن، نمی‌شه فقط با پارامتر «از کجا اومدم» رو
# رد کرد — باید یه‌جا نگهش داشت. اینجا با یه Setting سطحِ ربات نگهش
# می‌داریم و هروقت ادمین وارد یکی از این دو هاب می‌شه، به‌روزش می‌کنیم؛
# همیشه منعکس‌کنندهٔ آخرین هابیه که ازش اومده، در هر عمقی از زیرمنو.
async def _set_browse_origin(bot_id: int, origin: str) -> None:
    await _set_setting(bot_id, "smm_browse_origin", origin)


async def _get_browse_origin(bot_id: int) -> str:
    return await _get_setting(bot_id, "smm_browse_origin", "menu")


async def _active_panels(bot_id: int) -> list[tuple[int | None, str]]:
    """لیستِ همهٔ پنل‌هایِ فعالِ این ربات: [(panel_pk, name), ...].
    اگر پنلِ پیش‌فرضِ قدیمی هم وصل باشد، به‌عنوانِ (None, 'پنل پیش‌فرض')
    اولِ لیست می‌آید."""
    out: list[tuple[int | None, str]] = []
    legacy_url = await _get_setting(bot_id, "smm_api_url", "")
    legacy_key = await _get_setting(bot_id, "smm_api_key", "")
    if legacy_url and legacy_key:
        out.append((None, "پنل پیش‌فرض"))
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(
            SmmPanel.bot_id == bot_id, SmmPanel.is_active == True).order_by(SmmPanel.id))).all()
    for p in panels:
        out.append((p.id, p.name))
    return out


async def _get_browsing_panel(bot_id: int) -> int | None:
    """پنلی که ادمین آخرین‌بار برای مرور/افزودنِ سرویس انتخاب کرده — یک
    Setting سطحِ ربات (نه FSM state، چون این فایل جاهایِ زیادی state.clear()
    می‌زند و اگر این انتخاب توی state ذخیره می‌شد، با اولین state.clear()ِ
    نامرتبط بی‌صدا به پنلِ پیش‌فرض برمی‌گشت)."""
    raw = await _get_setting(bot_id, "smm_browse_panel_pk", "")
    return int(raw) if raw.strip().isdigit() else None


async def _owner(cb_or_msg, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        return None
    uid = cb_or_msg.from_user.id
    if not await is_owner_or_admin(row.id, uid, row.owner_id):
        # اگر callback بود، اسپینر را ببند تا کاربر حس بی‌واکنشی نگیرد
        try:
            if isinstance(cb_or_msg, CallbackQuery):
                await cb_or_msg.answer("⛔️ فقط ادمین به این بخش دسترسی دارد.", show_alert=True)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return None
    return row


_FA_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm_num(raw: str) -> str:
    """اعدادِ فارسی/عربی و جداکننده‌های هزارگان (٬ ، ,) را به عددِ لاتینِ
    قابلِ‌پارس تبدیل می‌کند. بدونِ این تبدیل، اگر ادمین با کیبوردِ فارسی
    عدد بفرستد (مثلاً «۵۰»)، float() خطا می‌دهد و مقدارِ قبلی (پیش‌فرض)
    بی‌صدا باقی می‌ماند — همان چیزی که باعثِ «ربات کم‌تر از درصدِ
    واقعی سود اضافه می‌کند» می‌شود چون درصد/نرخِ جدید اصلاً ذخیره نشده."""
    return (raw or "").strip().translate(_FA_DIGIT_TRANS).replace(",", "").replace("٬", "").replace("،", "")


def _rial(n) -> str:
    try:
        return f"{int(float(n)):,} تومان"
    except Exception:
        return f"{n} تومان"


async def _get_toman_unit_rate(bot_id: int, panel_pk: int | None = None) -> float:
    """نرخ تبدیلِ واحدِ پولِ پنل (معمولاً دلار) به تومان.

    اگر حالتِ نرخ روی «خودکار» باشد، ابتدا نرخِ زنده (همان چِینِ
    BrsApi/بایننس‌P2P/navasan که در crypto_pay.get_usd_to_toman_rate پیاده
    شده) گرفته می‌شود. اگر نرخِ زنده در دسترس نبود (یا حالت روی «دستی»
    بود)، به نرخِ دستیِ ثبت‌شده توسط ادمین سقوط می‌کنیم — یعنی خودکار و
    دستی هر دو همزمان پشتیبانی می‌شوند.

    === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    panel_pk: اگر داده بشه، این تنظیم (حالتِ نرخ + نرخِ دستی) مالِ همون
    پنلِ خاصه، نه کلِ ربات. پیش‌فرض None یعنی پنلِ پیش‌فرضِ قدیمی —
    برایِ فراخوان‌هایی که این پارامتر رو نمی‌دن، دقیقاً همون رفتارِ قبلی.

    === PATCH_SMM_PANEL_CURRENCY_V1 ===
    اگر ادمین صراحتاً واحدِ پنل را «تومان» ثبت کرده باشد، نرخِ زندهٔ
    دلار اصلاً نباید اعمال شود — حتی اگر rate_mode روی «خودکار» باشد.
    قبلاً این حالت وجود نداشت و باعث می‌شد پنل‌هایِ تومانی که به‌اشتباه
    (یا از قبل) رویِ حالتِ خودکار بودند، قیمتشان با نرخِ دلار ضرب شود —
    یعنی چند صدهزار برابر بیشتر از قیمتِ واقعی."""
    currency = await _panel_currency(bot_id, panel_pk)
    if currency == "toman":
        return 1.0
    rate_mode = await _panel_rate_mode(bot_id, panel_pk)
    if rate_mode == "auto":
        try:
            from tenant.gateways.crypto_pay import get_usd_to_toman_rate
            live = await get_usd_to_toman_rate()
        except Exception:
            logger.exception("خطا در دریافت نرخ زندهٔ دلار برای SMM")
            live = 0.0
        if live and live > 0:
            return float(live)
    manual = await _panel_toman_per_unit(bot_id, panel_pk)
    if manual > 0:
        return manual
    # === PATCH_SMM_USD_RATE_FALLBACK_V1 ===
    # تیکت #856: پنلِ دلاری با حالتِ «دستی» و نرخِ ثبت‌نشده (۰) باعث می‌شد
    # فراخوان‌ها نرخ را ۱ بگیرند و ۸ دلار به‌صورتِ ۸ تومان فروخته/نشان داده شود.
    # برایِ پنلِ ارزی، در نبودِ نرخِ دستی از نرخِ زندهٔ دلار استفاده می‌کنیم.
    if rate_mode != "auto":
        try:
            from tenant.gateways.crypto_pay import get_usd_to_toman_rate
            live = await get_usd_to_toman_rate()
        except Exception:
            logger.exception("خطا در دریافت نرخ زندهٔ دلار برای SMM (fallback)")
            live = 0.0
        if live and live > 0:
            return float(live)
    return manual


async def _effective_margin_pct(bot_id: int, svc: SmmService) -> float:
    """درصدِ سودِ مؤثر برایِ یک سرویس: اگر سرویس به یک دستهٔ دستی وصل باشه
    و اون دسته سودِ مخصوصِ خودشو تنظیم کرده باشه، همون اولویت داره؛
    وگرنه از سودِ سطحِ پنل استفاده می‌شه.
    === PATCH_SMM_MANUAL_CATEGORY_V1 ==="""
    if svc.category_pk:
        async with Session() as s:
            c = await s.get(SmmCategory, svc.category_pk)
        if c and c.bot_id == bot_id and c.margin_pct is not None:
            return float(c.margin_pct)
    return await _panel_margin_pct(bot_id, svc.panel_pk)


# === PATCH_SMM_UNIT_MODE_V1 ===
def _qty_factor(svc) -> float:
    """ضریبی که قیمتِ پایه در آن ضرب می‌شود.

    استانداردِ SMM قیمت را «هر ۱۰۰۰» می‌دهد، ولی سرویسهایِ تکی (گیفت،
    اکانت و …) قیمتشان قیمت *یک عدد* است. تا قبل از این، همه هزارتایی
    فرض می‌شدند و سرویسِ تکی به یک‌هزارمِ قیمت فروخته می‌شد.
    """
    mode = (getattr(svc, "unit_mode", "auto") or "auto").lower()
    if mode == "per_unit":
        return 1.0
    if mode == "per_1000":
        return 1 / 1000.0
    # auto → اگر سقفِ سفارش ۱ باشد قطعاً سرویسِ تکی است
    try:
        if int(getattr(svc, "max_qty", 0) or 0) == 1:
            return 1.0
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return 1 / 1000.0


def unit_label(svc) -> str:
    """برچسب واحد برایِ نمایش به کاربر."""
    return "هر عدد" if _qty_factor(svc) == 1.0 else "هر ۱۰۰۰ عدد"


# === PATCH_SMM_AUTO_DESC_V1 ===
def _fa_int(n) -> str:
    try:
        return f"{int(float(n or 0)):,}"
    except Exception:
        return str(n)


# === PATCH_SMM_DESC_BLOCKQUOTE_V1 ===
_DESC_TITLE = "\U0001f4f2 <b>توضیحاتِ محصول:</b>"


def _desc_block(body: str) -> str:
    """عنوانِ «توضیحاتِ محصول» + بدنه داخلِ نقل‌قولِ جمع‌شوندهٔ تلگرام.

    توضیحاتِ ارائه‌دهنده معمولاً چند خطِ بلند است (شرایطِ ریزش، جبران،
    مشخصاتِ اکانت و...) و وسطِ پیامِ محصول قیمت و دکمه‌ها را گم می‌کرد.
    با <blockquote expandable> متن تا زدنِ «بیشتر» جمع می‌ماند و پیام
    مرتب می‌شود. اگر کلاینتِ کاربر قدیمی باشد تلگرام خودش به نقل‌قولِ
    ساده تنزل می‌دهد — چیزی نمی‌شکند."""
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n\n{_DESC_TITLE}\n<blockquote expandable>{body}</blockquote>"


# === PATCH_SMM_STRIP_PROVIDER_COMMANDS_V1 ===
# توضیحاتِ ارائه‌دهنده گاهی دستورهایِ رباتِ *خودِ آن‌ها* را دارد (مثلاً «میانبر: /372» و
# «ثبت سفارش انبوه: /multi_372»). در چتِ ما این متن‌ها تپ‌شدنی‌اند و از وقتی /<شماره> دستورِ
# میانبرِ خودِ ما شده، «/372»یِ ارائه‌دهنده محصولِ شمارهٔ ۳۷۲ِ *ما* را باز می‌کرد که هیچ
# ربطی به آن ندارد. پس خط‌هایی که چنین دستوری دارند از توضیحاتِ وارداتی حذف می‌شوند.
# مرزِ «نه بعد از حرف/رقم» (?<!\w) مانعِ خرابیِ متن‌هایی مثلِ «1000/50» یا آدرسِ
# t.me/x/123 می‌شود.
_PROVIDER_CMD_RE = re.compile(r"(?<!\w)/(?:multi_)?\d{2,}(?!\w)")


def _strip_provider_commands(text: str) -> str:
    kept = [ln for ln in (text or "").splitlines() if not _PROVIDER_CMD_RE.search(ln)]
    return "\n".join(kept).strip()


_TR_LOCKS: dict[str, "asyncio.Lock"] = {}


async def _provider_text_fa(svc, text: str) -> str:
    """=== PATCH_SMM_DESC_TRANSLATE_V1 === ترجمهٔ فارسی توضیحات ارائه‌دهنده (کش‌شده در ردیف سرویس)؛ در هر خطا متن اصلی."""
    from tenant.shared.fa_translate import needs_translation, text_hash, translate_fa
    if not needs_translation(text):
        return text
    h = text_hash(text)
    if getattr(svc, "provider_desc_fa_hash", None) == h and getattr(svc, "provider_desc_fa", None):
        return svc.provider_desc_fa
    lock = _TR_LOCKS.setdefault(h, asyncio.Lock())
    async with lock:
        if getattr(svc, "provider_desc_fa_hash", None) == h and getattr(svc, "provider_desc_fa", None):
            return svc.provider_desc_fa              # همزمان یکی دیگر ترجمه‌اش کرد
        tr = await translate_fa(text)
        if len(_TR_LOCKS) > 500:
            _TR_LOCKS.clear()
    if not tr:
        return text
    try:
        svc.provider_desc_fa, svc.provider_desc_fa_hash = tr, h
    except Exception:
        logger.debug("provider_desc_fa روی شیء ست نشد (بی‌اهمیت)")
    sid = getattr(svc, "id", None)
    if sid:
        try:
            async with Session() as s:
                await s.execute(update(SmmService).where(SmmService.id == sid).values(
                    provider_desc_fa=tr, provider_desc_fa_hash=h))
                await s.commit()
        except Exception:
            logger.exception("ذخیرهٔ ترجمهٔ توضیحات ناموفق بود")
    return tr


async def _category_description(svc) -> str:
    """=== PATCH_SMM_CATEGORY_DESC_V1 === توضیحات مشترک دستهٔ این سرویس ('' اگر دسته/توضیحی نیست)."""
    cid = getattr(svc, "category_pk", None)
    if not cid:
        return ""
    try:
        async with Session() as s:
            c = await s.get(SmmCategory, cid)
        return ((c.description if c else "") or "").strip()[:1500]
    except Exception:
        logger.exception("خواندن توضیحات دسته ناموفق بود")
        return ""


async def build_description(bot_id: int, svc) -> str:
    """متنِ توضیحاتِ محصول برایِ نمایش به مشتری.

    === PATCH_SMM_PROVIDER_DESC_V1 ===
    اولویت با توضیحاتِ خودِ ارائه‌دهنده است (اگر پنل فرستاده باشد) — این
    دقیقاً همان چیزی است که مشتری انتظار دارد ببیند: کیفیت، سرعتِ تحویل،
    جبرانِ ریزش و شرایطِ سرویس. قبلاً این فیلد اصلاً ذخیره نمی‌شد و ربات
    به‌جایش یک متنِ عمومی از رویِ دسته/بازهٔ سفارش می‌ساخت که هیچ اطلاعاتِ
    واقعی‌ای نداشت.

    ترتیب: توضیحاتِ ارائه‌دهنده → متنِ دستیِ ادمین → (اگر هیچ‌کدام نبود)
    همان خلاصهٔ ساختگیِ قبلی به‌عنوانِ آخرین راهِ چاره.
    """
    mode = await _get_setting(bot_id, "smm_desc_mode", "auto")
    if mode == "off":
        return ""

    manual = (getattr(svc, "description", "") or "").strip()
    if mode == "manual":
        if not manual:
            _cd = await _category_description(svc)       # PATCH_SMM_CATEGORY_DESC_V1
            if _cd:
                return _desc_block(_html_escape(_cd))
        return _desc_block(manual)  # PATCH_SMM_DESC_BLOCKQUOTE_V1

    provider = _strip_provider_commands((getattr(svc, "provider_desc", "") or "").strip())
    if provider and await _get_setting(bot_id, "smm_desc_translate", "1") != "0":
        provider = await _provider_text_fa(svc, provider)    # PATCH_SMM_DESC_TRANSLATE_V1
    if provider:
        body = _html_escape(provider)
        if manual:
            body += f"\n{_html_escape(manual)}"
        return _desc_block(body)  # PATCH_SMM_DESC_BLOCKQUOTE_V1

    if not manual:
        # === PATCH_SMM_CATEGORY_DESC_V1 === نه توضیح پنل هست نه دستی خود سرویس ⇒ توضیحات مشترک دسته (اگر ادمین نوشته)
        _cd = await _category_description(svc)
        if _cd:
            return _desc_block(_html_escape(_cd))

    lines = []
    try:
        _p = getattr(svc, "platform_key", "") or ""
        _t = getattr(svc, "type_key", "") or ""
        if _p or _t:
            _lbl = " \u2014 ".join(x for x in (
                smm_api.platform_label(_p) if _p else "",
                smm_api.service_type_label(_t) if _t else "") if x)
            if _lbl:
                lines.append(f"\u2022 نوعِ سرویس: {_lbl}")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    cat = (getattr(svc, "category", "") or "").strip()
    if cat:
        lines.append(f"\u2022 دسته: {cat}")

    mn = int(getattr(svc, "min_qty", 0) or 0)
    mx = int(getattr(svc, "max_qty", 0) or 0)
    try:
        _unit = "عدد" if _qty_factor(svc) == 1.0 else "عدد"
    except Exception:
        _unit = "عدد"
    if mn and mx:
        if mn == mx:
            lines.append(f"\u2022 تعدادِ ثابت: {_fa_int(mn)} {_unit}")
        else:
            lines.append(f"\u2022 بازهٔ سفارش: {_fa_int(mn)} تا {_fa_int(mx)} {_unit}")
    elif mn:
        lines.append(f"\u2022 حداقلِ سفارش: {_fa_int(mn)} {_unit}")

    if not lines and not manual:
        return ""

    # === PATCH_SMM_DESC_BLOCKQUOTE_V1 ===
    body = "\n".join(lines)
    if manual:
        body = (body + "\n" + manual) if body else manual
    return _desc_block(body)


# === PATCH_SMM_PRICE_MEMO_V1 ===
# گزارشِ Omid: «وقتی مشتری دکمه را می‌زند محصولات دیر می‌آیند». وقتی مرتب‌سازیِ قیمتی روشن است
# (smm_sort_order=price_asc/desc)، _sort_svc_objs برایِ *هر* سرویس compute_sell_price را صدا می‌زد و هر بار چند کوئریِ
# جدایِ دیتابیس (سودِ دسته/پنل، واحدِ پول، حالتِ نرخ، نرخِ زنده، گِرد‌کردن) اجرا می‌شد ⇒ برایِ ۵۰ سرویس صدها کوئریِ
# پشتِ‌هم. تنظیماتِ هر (ربات/پنل/دسته) حالا ۴ ثانیه کش می‌شود؛ یعنی یک‌بار حساب و بقیه از حافظه (تغییرِ تنظیماتِ ادمین
# حداکثر ۴ ثانیه دیرتر اعمال می‌شود).
_PRICE_MEMO: dict = {}
_PRICE_MEMO_TTL = 4.0


async def _memo(key, factory):
    import time as _t
    now = _t.monotonic()
    hit = _PRICE_MEMO.get(key)
    if hit and now - hit[0] < _PRICE_MEMO_TTL:
        return hit[1]
    val = await factory()
    if len(_PRICE_MEMO) > 2000:
        _PRICE_MEMO.clear()
    _PRICE_MEMO[key] = (now, val)
    return val


async def compute_sell_price(bot_id: int, svc: SmmService, quantity: int) -> int:
    """قیمت فروشِ نهایی (تومان) برای یک سرویس و تعداد مشخص را حساب می‌کند.
    خروجی گِرد‌شده به تومان.

    === PATCH_SMM_PER_SERVICE_PRICE_OVERRIDE_V1 ===
    قبلاً قیمتِ دستیِ هر سرویس فقط وقتی اثر داشت که کلِ ربات رویِ حالتِ
    «دستی» بود — یعنی ادمین برایِ اینکه فقط قیمتِ یک محصولِ متصل به پنل
    را جدا تنظیم کند، مجبور بود کلِ حالتِ قیمت‌گذاریِ ربات را عوض کند
    (که رویِ همهٔ سرویس‌هایِ دیگر هم اثر می‌گذاشت). حالا هر سرویس —
    چه متصل به پنل، چه دستی — اگر خودش یک قیمتِ دستیِ ثبت‌شده داشته
    باشد (sell_rate > 0)، همان اولویت دارد؛ در غیرِ این صورت (سرویس
    هیچ قیمتِ دستی‌ای نداشته) به‌طورِ خودکار از فرمولِ سود محاسبه
    می‌شود. یعنی الان می‌شود بعضی سرویس‌ها را دستی قیمت گذاشت و بقیه
    را رویِ محاسبهٔ خودکار گذاشت، بدونِ سویچِ سراسری."""
    per_1000 = 0.0
    if svc.sell_rate and float(svc.sell_rate) > 0:
        per_1000 = float(svc.sell_rate)  # تومان به‌ازای هر ۱۰۰۰ — قیمتِ دستیِ همین سرویس
    elif getattr(svc, "is_manual", False):
        # سرویسِ دستی که هنوز قیمتی برایش ثبت نشده — به پنل وصل نیست که
        # از رویش محاسبه کنیم، پس قیمتش صفر می‌ماند تا ادمین ثبتش کند.
        per_1000 = 0.0
    else:
        # margin: قیمت پنل × (۱+سود) × نرخ تبدیل به تومان
        # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
        margin = await _memo(("m", bot_id, svc.category_pk, svc.panel_pk),
                             lambda: _effective_margin_pct(bot_id, svc))
        toman_unit = await _memo(("u", bot_id, svc.panel_pk),
                                 lambda: _get_toman_unit_rate(bot_id, svc.panel_pk))
        panel_per_1000 = float(svc.panel_rate or 0)
        if toman_unit <= 0:
            # اگر نرخ تبدیل تنظیم نشده، فرض می‌کنیم قیمت پنل خودش تومان است
            toman_unit = 1.0
        per_1000 = panel_per_1000 * toman_unit * (1 + margin / 100.0)
        price = per_1000 * quantity * _qty_factor(svc)  # PATCH_SMM_UNIT_MODE_V1
        # === PATCH_SMM_PRICE_ROUND_V1 ===
        # رند فقط رویِ قیمتِ خودکار‌محاسبه‌شده اعمال می‌شه، نه رویِ قیمتِ
        # دستی‌ای که خودِ ادمین عینِ همون عدد رو خواسته.
        round_unit = await _memo(("r", bot_id, svc.panel_pk),
                                 lambda: _panel_price_round(bot_id, svc.panel_pk))
        return _round_price(price, round_unit)
    price = per_1000 * quantity * _qty_factor(svc)  # PATCH_SMM_UNIT_MODE_V1
    return _positive_floor(price, int(round(price)))  # === PATCH_SMM_TINY_PRICE_FLOOR_V1 ===


# === PATCH_VS_ORDER_DETAIL_V1 ===
async def compute_cost_price(bot_id: int, svc: SmmService, quantity: int) -> float | None:
    """هزینهٔ خامِ پنل (بدونِ سود) برای یک سرویس/تعداد — تومان.

    فقط برایِ سرویس‌هایِ متصل به پنل با قیمت‌گذاریِ خودکار (margin) معنا
    دارد؛ برایِ سرویس‌هایِ دستی یا قیمتِ دستیِ هر‌سرویس (sell_rate ثبت‌شده)
    هیچ «هزینهٔ پنل»ی برای مقایسه وجود ندارد، پس None برمی‌گردد (یعنی تویِ
    جزئیاتِ سفارش خطِ سود/هزینه اصلاً نشون داده نمی‌شود، به‌جایِ صفرِ
    گمراه‌کننده)."""
    if getattr(svc, "is_manual", False) or (svc.sell_rate and float(svc.sell_rate) > 0):
        return None
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    toman_unit = await _get_toman_unit_rate(bot_id, svc.panel_pk)
    if toman_unit <= 0:
        toman_unit = 1.0
    panel_per_1000 = float(svc.panel_rate or 0)
    # PATCH_SMM_UNIT_MODE_V1 — همان اصلاحِ قیمتِ فروش، این‌بار رویِ هزینه
    cost = panel_per_1000 * toman_unit * quantity * _qty_factor(svc)
    return round(cost)


# ═══════════════════════ منوی ادمین ═══════════════════════

# === PATCH_SMM_PLATFORM_RENAME_V1 ===
# درخواستِ ادمین: بشه اسمِ خودِ پلتفرم‌ها (نه فقط اسمِ دسته) رو کامل عوض
# کرد — مثلاً «🔍 گوگل» رو به هرچی که خودش می‌خواد. چون این مالِ هر
# رباته (نه یه چیزِ سراسری برایِ همه‌ی ربات‌ها)، تویِ یه Setting به‌صورتِ
# JSON نگه‌داری می‌شه: {"google": "برچسبِ دلخواه", ...}.
async def _platform_label_overrides(bot_id: int) -> dict:
    raw = await _get_setting(bot_id, "smm_platform_labels", "")
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


async def platform_label_for_bot(bot_id: int, pkey: str) -> str:
    """برچسبِ پلتفرم مخصوصِ همین ربات — اگه ادمین رنامش کرده باشه همون رو
    برمی‌گردونه، وگرنه برچسبِ پیش‌فرض."""
    overrides = await _platform_label_overrides(bot_id)
    if pkey in overrides:
        return overrides[pkey]
    return smm_api.platform_label(pkey)


async def _set_platform_label(bot_id: int, pkey: str, label: str | None) -> None:
    overrides = await _platform_label_overrides(bot_id)
    if label:
        overrides[pkey] = label[:60]
    else:
        overrides.pop(pkey, None)
    await _set_setting(bot_id, "smm_platform_labels", json.dumps(overrides, ensure_ascii=False))


_SORT_ORDER_LABELS = {
    "none": "پیش‌فرض (بدونِ ترتیب)",
    "price_asc": "ارزان به گران",
    "price_desc": "گران به ارزان",
    "new_first": "جدید به قدیمی",
    "old_first": "قدیمی به جدید",
    # === PATCH_SMM_MANUAL_SORT_ORDER_V1 ===
    # طبقِ references/smm-menu-redesign.md (تصمیمِ ۵): علاوه بر پریست‌های
    # بالا، ادمین می‌تونه خودش با دکمه‌هایِ 🔼/🔽 روی صفحهٔ هر سرویس (زیرِ
    # «🗂 سرویس‌های فعال من») ترتیبِ دلخواه بچینه — این حالت همون ترتیب رو
    # به مشتری نشون می‌ده.
    "manual": "ترتیبِ دستی (خودم می‌چینم)",
}
_SORT_ORDER_CYCLE = ["none", "price_asc", "price_desc", "new_first", "old_first", "manual"]

# === PATCH_SMM_PRICE_VIEW_MODE_V1 ===
# ادمین خواست بتواند قیمت را به‌ازای «حداقلِ سفارشِ همان سرویس» هم نشان
# دهد، نه فقط واحدِ خامِ هر ۱۰۰۰ تا — چون برایِ سرویسی که حداقلش ۱۰ تاست،
# «قیمت هر ۱۰۰۰ عدد» عددی است که مشتری اصلاً نمی‌تواند بخرد.
_PRICE_VIEW_LABELS = {
    "unit": "فقط واحد (هر ۱۰۰۰ / هر عدد)",
    "min": "فقط به‌ازای حداقلِ سفارش",
    "both": "هر دو (واحد + حداقلِ سفارش)",
}
_PRICE_VIEW_CYCLE = ["unit", "min", "both"]


async def smm_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    await ensure_smm_columns()
    api_url = await _get_setting(bot_id, "smm_api_url", "")
    api_key = await _get_setting(bot_id, "smm_api_key", "")
    price_mode = await _get_setting(bot_id, "smm_price_mode", "margin")
    order_mode = await _get_setting(bot_id, "smm_order_mode", "manual")
    margin = await _get_setting(bot_id, "smm_margin_pct", "30")

    # === PATCH_SMM_MULTI_PANEL_V1 ===
    panels = await _active_panels(bot_id)
    connected = bool(panels)
    conn_txt = f"✅ {len(panels)} پنلِ فعال" if connected else "❌ هیچ پنلی وصل نیست"
    price_txt = "درصد سود (خودکار)" if price_mode == "margin" else "دستی"
    order_txt = "خودکار" if order_mode == "auto" else "تأیید ادمین"
    # === PATCH_SMM_FAIL_RETRY_V1 ===
    fail_policy = await _get_setting(bot_id, "smm_fail_policy", "auto_refund")
    fail_policy_txt = "بازگشتِ خودکارِ وجه" if fail_policy == "auto_refund" else "نگه‌داری برایِ بررسیِ دستی"
    rate_mode = await _get_setting(bot_id, "smm_rate_mode", "manual")
    rate_txt = "خودکار (زنده)" if rate_mode == "auto" else "دستی"

    async with Session() as s:
        n_active = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True))

    lines = [
        "🛰 <b>پنل SMM (خدمات شبکه‌های اجتماعی)</b>\n",
        f"وضعیت اتصال: <b>{conn_txt}</b>",
        f"حالت قیمت‌گذاری: <b>{price_txt}</b>" + (f" ({margin}٪)" if price_mode == "margin" else ""),
        f"حالت ثبت سفارش: <b>{order_txt}</b>",
        f"سرویس‌های فعال برای فروش: <b>{n_active or 0}</b>",
    ]
    rows = [
        [InlineKeyboardButton(text=("🔁 تغییر پنل" if connected else "🔗 اتصال به پنل"),
                              callback_data="smm:setpanel")],
    ]
    # === PATCH_MANUAL_HUB_MERGE_V1 ===
    # گیفت/شماره‌ی مجازی/پرمیوم/استارز/ری‌اکشنِ استارزی/TON از اینجا به
    # «✍️ سرویس‌های دستی» منتقل شدن — چون هیچ‌کدوم به پنلِ API وصل نیستن
    # و ادمین‌ها گیج می‌شدن که چرا زیرِ «اتصال به پنل»ان.
    if connected:
        # === PATCH_SMM_PROVIDERS_LIST_COMPACT_V1 ===
        # این دو تا («موجودی» و «استعلامِ گروهی») قبلاً مستقیم تو لیستِ
        # ارائه‌دهندگان بودن؛ چون لیست جمع‌وجور شد، برایِ پنلِ پیش‌فرض
        # (که صفحهٔ عملیاتِ جداگانه‌ای مثلِ پنل‌هایِ اضافه نداره) اینجا موند.
        bulk_on_legacy = await _panel_bulk_enabled(bot_id, None)
        rows += [
            [InlineKeyboardButton(text="💰 موجودی حساب پنل", callback_data="smm:balance")],
            [InlineKeyboardButton(
                text=("📡 استعلامِ گروهیِ سفارش‌ها: خاموش کن" if bulk_on_legacy else "📡 استعلامِ گروهیِ سفارش‌ها: روشن کن"),
                callback_data="smm:panel:bulk:0")],
            [InlineKeyboardButton(text="📥 دریافت و انتخاب سرویس‌ها", callback_data="smm:services:0")],
            # === PATCH_SMM_PRODUCTS_HUB_V1 ===
            # «🗂 سرویس‌های فعال من» از اینجا برداشته شد — چون همون لیست
            # الان زیرِ «📦 مدیریتِ محصولات» جمع شده و بازگشتش هم به همون‌جا
            # اشاره می‌کنه؛ دو تا ورودیِ موازی با یه بازگشتِ ثابت باعثِ
            # ناوبریِ اشتباه می‌شد (از اینجا وارد بشی، برگردی جایِ دیگه).
            [InlineKeyboardButton(
                text=f"⚙️ حالت قیمت: {price_txt}", callback_data="smm:togpricemode")],
        ]
        # === PATCH_SMM_PANEL_ONLY_SETTINGS_V1 ===
        # این دکمه‌ها قبلاً این‌جا بودند ولی مقادیرِ *عمومی* را عوض می‌کردند،
        # در حالی که موقعِ ثبتِ سفارش تنظیمِ خودِ پنل خوانده می‌شود. یعنی
        # ادمین «خودکار» می‌دید و سفارش‌ها به تأییدِ دستی می‌رفتند. حالا
        # تنظیمات فقط از داخلِ هر پنل انجام می‌شود تا تناقض نباشد.
        # === PATCH_SMM_DEFAULT_PANEL_SETTINGS_DEADEND_V1 ===
        # باگِ گزارش‌شده («این چیه می‌زنم روش کار نمی‌کنه»): این دکمه
        # callback_dataاش smm:panels بود — دقیقاً همون مقصدِ دکمهٔ «⬅️ بازگشت»
        # چند ردیف پایین‌تر. یعنی مدیر «تنظیماتِ قیمت و سفارش» را می‌زد و
        # سر از لیستِ ارائه‌دهندگان درمی‌آورد، و ردیفِ «پنلِ پیش‌فرض»یِ همون
        # لیست (smm:menu) دوباره برش می‌گردوند همین‌جا — یک حلقهٔ بسته که
        # هیچ‌وقت به صفحهٔ تنظیمات نمی‌رسید. خودِ صفحه از قبل وجود داشت و
        # پنلِ پیش‌فرض را هم با شناسهٔ 0 قبول می‌کند، فقط کسی به آن لینک
        # نمی‌داد.
        rows.append([InlineKeyboardButton(
            text="\u2699\ufe0f تنظیماتِ قیمت و سفارشِ این پنل",
            callback_data="smm:panel:settings:0")])
    # همیشه در دسترس (حتی بدون پنل): سفارش‌ها
    rows.append([InlineKeyboardButton(text="📋 سفارش‌های SMM", callback_data="smm:orders")])
    # === PATCH_SMM_CUSTOM_TEXTS_V1 ===
    rows.append([InlineKeyboardButton(
        text="✍️ متن‌هایِ اطلاع‌رسانیِ سفارش", callback_data="smm:texts")])
    # === PATCH_SMM_QLC_DEPTH_V1 ===
    _qd = await _get_setting(bot_id, "qlc_depth", "3")
    _qd_txt = ("دسته ← سرویس" if _qd == "2" else "دسته ← نوع ← سرویس")
    rows.append([InlineKeyboardButton(
        text=f"🧭 مسیرِ خرید: {_qd_txt}", callback_data="smm:qlcdepth")])
    _dm = await _get_setting(bot_id, "smm_desc_mode", "auto")
    _dm_txt = {"off": "خاموش", "manual": "فقط دستی",
               "auto": "خودکار + دستی"}.get(_dm, _dm)
    rows.append([InlineKeyboardButton(
        text=f"\U0001f4f2 توضیحاتِ محصول: {_dm_txt}",
        callback_data="smm:descmode")])
    # === PATCH_SMM_DESC_TRANSLATE_V1 ===
    _dtr = await _get_setting(bot_id, "smm_desc_translate", "1") != "0"
    rows.append([InlineKeyboardButton(
        text=("\U0001f310 ترجمهٔ خودکار توضیحات به فارسی: " + ("روشن" if _dtr else "خاموش")),
        callback_data="smm:desctr")])
    _bc = await _get_setting(bot_id, "smm_btn_cols", "1")
    _bn = await _get_setting(bot_id, "smm_btn_count", "1")
    # === PATCH_SMM_PRICE_VIEW_MODE_V1 ===
    _pv = await _get_setting(bot_id, "smm_price_view", "unit")
    rows.append([InlineKeyboardButton(
        text=f"\U0001f4b5 نمایشِ قیمت: {_PRICE_VIEW_LABELS.get(_pv, _pv)}",
        callback_data="smm:pricview")])
    rows.append([InlineKeyboardButton(
        text=("\u25ab\ufe0f چیدمان: دوتایی" if _bc == "2" else "\u25ab\ufe0f چیدمان: تکی"),
        callback_data="smm:btncols")])
    rows.append([InlineKeyboardButton(
        text=("\U0001f522 نمایشِ تعداد: روشن" if _bn != "0" else "\U0001f522 نمایشِ تعداد: خاموش"),
        callback_data="smm:btncount")])
    rows.append([InlineKeyboardButton(
        text="\u267b\ufe0f ریستِ محصولات", callback_data="smm:reset",
        style=ButtonStyle.DANGER)])
    # === PATCH_SMM_AI_ASSISTANT_V1 ===
    rows.append([InlineKeyboardButton(text="🤖 دستیارِ هوشِ مصنوعیِ مشتری", callback_data="smmai:menu", style=ButtonStyle.PRIMARY)])
    # === PATCH_SMM_PRODUCTS_HUB_V1 ===
    # «↕️ ترتیبِ نمایشِ سرویس‌ها» از اینجا به «📦 مدیریتِ محصولات» منتقل شد —
    # این یه تنظیمِ سراسریه (رویِ همه‌ی پنل‌ها اثر می‌ذاره)، نه مخصوصِ پنلِ
    # پیش‌فرض، پس اینجا (که فقط برایِ پنلِ پیش‌فرضه) جایِ درستی نبود.
    # === PATCH_SMM_ONE_ENTRY_V1 ===
    # این صفحه دیگه مستقیم از «سرویس‌ها و محصولات» قابلِ‌ورود نیست — فقط
    # از داخلِ «🌐 پنل‌های API» (که خودش الان برایِ همهٔ پلن‌ها بازه) —
    # پس برگشتش هم باید همونجا بره.
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════ مدیریتِ محصولات (هاب) ═══════════════════
# === PATCH_SMM_PRODUCTS_HUB_V1 ===
# طبقِ چیدمانِ نهاییِ ادمین: «ارائه‌دهندگان» تنها زیرِ «مشاهده سفارشات»
# می‌شینه، و جایِ قبلیِ آن (کنارِ «مدیریتِ دسته‌ها») را این دکمه می‌گیرد.
# محتوایِ این هاب دقیقاً همون چیزیه که از اول برایِ «مدیریت محصولات» ربات
# مرجع توضیح داده شد: افزودنِ محصول (با انتخابِ ارائه‌دهنده)، مشاهده/مدیریتِ
# محصولاتِ فعلی، مدیریتِ دسته‌ها، و ترتیبِ نمایش — هیچ‌کدوم منطقِ تازه
# نیست، فقط میان‌برهایی به چیزهایی که از قبل ساخته شده بودن.
async def smm_products_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.virtual_service import smm_price_sync as _PS   # PATCH_SMM_PRICE_AUTOSYNC_V1
    _ps_on = (await _get_setting(bot_id, "smm_price_autosync", "1")) == "1"
    _ps_mode = await _PS.notify_mode(bot_id)
    sort_order = await _get_setting(bot_id, "smm_sort_order", "none")
    async with Session() as s:
        n_active = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True))
    lines = [
        "📦 <b>مدیریتِ محصولات</b>\n",
        f"سرویس‌های فعالِ فروش: <b>{n_active or 0}</b>",
        f"ترتیبِ نمایش به مشتری: <b>{_SORT_ORDER_LABELS.get(sort_order, sort_order)}</b>",
    ]
    rows = [
        [InlineKeyboardButton(text="➕ افزودنِ محصول (انتخابِ ارائه‌دهنده)",
                              callback_data="smm:products:addpick", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🗂 مدیریتِ محصولات (سرویس‌هایِ من)", callback_data="smm:myservices")],
        [InlineKeyboardButton(text="📁 مدیریتِ دسته‌ها", callback_data="smm:cat:menu:ph")],
        [InlineKeyboardButton(text=f"↕️ ترتیبِ نمایشِ سرویس‌ها: {_SORT_ORDER_LABELS.get(sort_order, sort_order)}",
                              callback_data="smm:togsortorder")],
        # === PATCH_SMM_QUICKLINK_V1 ===
        [InlineKeyboardButton(text="⚡️ اتصالِ سریعِ محصولات به دکمه‌ها",
                              callback_data="smm:quicklink:menu", style=ButtonStyle.PRIMARY)],
        # === PATCH_SMM_PROVIDER_DESC_V1 ===
        # سرویس‌هایی که *قبل از* این قابلیت اضافه شده‌اند توضیحاتِ
        # ارائه‌دهنده را ذخیره نکرده‌اند. این دکمه یک‌بار لیستِ سرویس‌هایِ هر
        # پنل را می‌گیرد و توضیحات را برایِ محصولاتِ موجود پر می‌کند.
        [InlineKeyboardButton(text="🔄 بروزرسانیِ توضیحات از ارائه‌دهنده",
                              callback_data="smm:descsync")],
        # === PATCH_SMM_PRICE_AUTOSYNC_V1 ===
        [InlineKeyboardButton(
            text=("🔁 بروزرسانیِ خودکارِ قیمت (هر ۳۰ دقیقه): " + ("✅ روشن" if _ps_on else "❌ خاموش")),
            callback_data="smm:psync:auto",
            style=(ButtonStyle.SUCCESS if _ps_on else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"📨 گزارشِ بروزرسانیِ قیمت: {_PS.NOTIFY_LABELS[_ps_mode]}",
                              callback_data="smm:psync:mode")],
        [InlineKeyboardButton(text="💲 بروزرسانیِ قیمت‌ها از ارائه‌دهنده (همین الان)",
                              callback_data="smm:psync:now", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_SMM_PRICE_AUTOSYNC_V1 ===
@smm_router.callback_query(F.data.in_({"smm:psync:auto", "smm:psync:mode"}))
async def smm_psync_settings(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm_price_sync as _PS
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if cb.data == "smm:psync:auto":
        on = (await _get_setting(row.id, "smm_price_autosync", "1")) == "1"
        await _set_setting(row.id, "smm_price_autosync", "0" if on else "1")
        await cb.answer("خاموش شد" if on else "روشن شد؛ هر ۳۰ دقیقه قیمت‌ها از ارائه‌دهنده به‌روز می‌شوند")
    else:
        cur = await _PS.notify_mode(row.id)
        nxt = _PS.NOTIFY_MODES[(_PS.NOTIFY_MODES.index(cur) + 1) % len(_PS.NOTIFY_MODES)]
        await _set_setting(row.id, "smm_price_sync_notify", nxt)
        await cb.answer("گزارش: " + _PS.NOTIFY_LABELS[nxt])
    text, kb = await smm_products_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:psync:p:"))
async def smm_psync_panel_toggle(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm_price_sync as _PS
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    on = await _PS.panel_auto_on(row.id, pid)
    await _PS.set_panel_auto(row.id, pid, not on)
    await cb.answer("استعلامِ خودکارِ قیمت " + ("خاموش شد" if on else "روشن شد"))
    await _render_panel_detail(cb.message, row.id, pid)


@smm_router.callback_query(F.data == "smm:psync:now")
@smm_router.callback_query(F.data.startswith("smm:psync:now:"))
async def smm_psync_now(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import smm_price_sync as _PS
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await cb.answer("⏳ در حالِ گرفتنِ قیمت‌ها از ارائه‌دهنده…")
    only = int(cb.data.rsplit(":", 1)[1]) if cb.data.count(":") == 3 else "all"
    reports = await _PS.sync_bot(row.id, only_pk=only)
    texts = [t for t in (_PS.report_text(r) for r in reports) if t]
    for t in texts or ["ℹ️ هیچ محصولِ متصل به ارائه‌دهنده‌ای برایِ بروزرسانیِ قیمت نداری."]:
        await cb.message.answer(t, parse_mode="HTML")


@smm_router.callback_query(F.data == "smm:products:menu")
async def smm_products_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    text, kb = await smm_products_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_SMM_PROVIDER_DESC_V1 ===
@smm_router.callback_query(F.data == "smm:descsync")
async def smm_desc_sync(cb: CallbackQuery, bot: Bot):
    """توضیحاتِ ارائه‌دهنده را برایِ محصولاتی که از قبل اضافه شده‌اند پر می‌کند.

    برایِ هر پنلی که سرویسِ فعالی از آن داریم یک‌بار لیستِ سرویس‌ها گرفته
    می‌شود (نه یک تماس به‌ازای هر محصول) و توضیحاتِ هر سرویس رویِ ردیفِ
    متناظر نوشته می‌شود. متنِ دستیِ ادمین دست‌نخورده می‌ماند.
    """
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    await ensure_smm_columns()
    await cb.answer("⏳ در حالِ گرفتن توضیحات از ارائه‌دهنده‌ها…")

    async with Session() as s:
        panels = (await s.scalars(select(SmmService.panel_pk).where(
            SmmService.bot_id == row.id, SmmService.is_manual == False))).all()  # noqa: E712
    panel_pks = sorted(set(panels), key=lambda x: (x is not None, x))

    updated = 0
    checked = 0
    for panel_pk in panel_pks:
        services = await _fetch_services_cached(row.id, panel_pk, force=True)
        if not services:
            continue
        by_id = {str(x.get("service")): (x.get("desc") or "").strip() for x in services}
        async with Session() as s:
            rows_ = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == row.id,
                SmmService.panel_pk == panel_pk,
                SmmService.is_manual == False))).all()  # noqa: E712
            for v in rows_:
                checked += 1
                new_desc = by_id.get(str(v.service_id), "")
                if new_desc and (v.provider_desc or "") != new_desc:
                    v.provider_desc = new_desc
                    updated += 1
            if updated:
                await s.commit()

    text, kb = await smm_products_content(row.id)
    if not panel_pks:
        note = "ℹ️ هیچ محصولِ متصل به پنلی نداری که توضیحاتش را بگیرم.\n\n"
    elif updated:
        note = (f"✅ توضیحاتِ {updated} محصول از ارائه‌دهنده گرفته شد "
                f"(از {checked} محصولِ بررسی‌شده).\n\n")
    else:
        note = ("ℹ️ چیزی برایِ بروزرسانی نبود — یا ارائه‌دهنده برایِ این "
                "سرویس‌ها توضیحاتی نفرستاده، یا از قبل ذخیره شده بود.\n\n")
    try:
        await cb.message.edit_text(note + text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(note + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
async def _smm_panel_choices(bot_id: int) -> list[tuple[str, str]]:
    """[(callback_value, label)] — «0» یعنی پنلِ پیش‌فرض (قدیمی)، بقیه
    شناسهٔ واقعیِ SmmPanel. اگه فقط یه گزینه باشه (یا هیچی)، لیست کوتاهه
    و فراخوان باید تصمیم بگیره پرسیدن لازم نیست."""
    legacy_url = await _get_setting(bot_id, "smm_api_url", "")
    legacy_key = await _get_setting(bot_id, "smm_api_key", "")
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(
            SmmPanel.bot_id == bot_id, SmmPanel.is_active == True).order_by(SmmPanel.id))).all()
    out = []
    if legacy_url and legacy_key:
        out.append(("0", "⚪️ پنل پیش‌فرض"))
    for p in panels:
        out.append((str(p.id), f"🏢 {p.name}"))
    return out


def _quicklink_panel_where(filter_raw: str) -> tuple[bool, int | None]:
    """(آیا فیلتر فعاله, panel_pk موردنظر). panel_pk=None یعنی «پنلِ
    پیش‌فرض» (قدیمی)؛ وقتی فیلتر فعال نیست یعنی همهٔ پنل‌ها با هم (رفتارِ
    قدیمی، برایِ ربات‌هایی که هنوز فقط یه پنل دارن)."""
    if filter_raw == "":
        return False, None
    return True, (None if filter_raw == "0" else int(filter_raw))


@smm_router.callback_query(F.data == "smm:products:addpick")
async def smm_products_addpick(cb: CallbackQuery, bot: Bot):
    """قدمِ اولِ «افزودنِ محصول»: انتخابِ اینکه از کدوم ارائه‌دهنده سرویس
    بیاد. همون smm:panel:pick قبلی رو صدا می‌زنه (منطقِ مرور/انتخابِ
    سرویس از پنل از قبل کامل بود، اینجا فقط یه لیستِ سبکِ انتخابه)."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    legacy_url = await _get_setting(row.id, "smm_api_url", "")
    legacy_key = await _get_setting(row.id, "smm_api_key", "")
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(
            SmmPanel.bot_id == row.id, SmmPanel.is_active == True).order_by(SmmPanel.id))).all()
    rows = []
    if legacy_url and legacy_key:
        rows.append([InlineKeyboardButton(text="⚪️ پنل پیش‌فرض", callback_data="smm:panel:pick:0")])
    for p in panels:
        rows.append([InlineKeyboardButton(text=f"🏢 {p.name}", callback_data=f"smm:panel:pick:{p.id}")])
    if not rows:
        await cb.answer("هنوز هیچ ارائه‌دهنده‌ای وصل نکردی — اول از «ارائه‌دهندگان» یکی اضافه کن.", show_alert=True)
        return
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "➕ <b>افزودنِ محصول</b>\n\nاول انتخاب کن از کدوم ارائه‌دهنده سرویس بیاد:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


# ═══════════════════ اتصالِ سریعِ محصولات به دکمه‌ها ═══════════════════

# === PATCH_QUICKLINK_ALERT_FIX_V1 ===
# علتِ باگ‌هایِ گزارش‌شده («دکمه رو می‌زنم هیچ اتفاقی نمی‌افته» و «موقعِ وصل‌کردنِ
# دسته‌ها گیر می‌کنه»): تلگرام برایِ متنِ answerCallbackQuery سقفِ ۲۰۰ کاراکتر داره
# و بیشترش رو با خطا رد می‌کنه. پیامِ قفل (با اسمِ دکمه + برچسبِ عملکرد) و پیامِ
# «✅ n دسته (نام‌ها…) وصل شد» راحت از ۲۰۰ رد می‌شدن؛ استثنا بالا می‌رفت، دکمه هیچ
# واکنشی نشون نمی‌داد و در ثبتِ دسته‌ها، ثبت انجام می‌شد ولی صفحه هیچ‌وقت تازه نمی‌شد.
def _ql_clip(text: str, limit: int = 200) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


async def _ql_alert(cb, text: str = "", show_alert: bool = False) -> None:
    """cb.answer امن: متن به ۲۰۰ کاراکتر کوتاه می‌شه و هیچ خطایی بالا نمی‌ره."""
    try:
        await cb.answer(_ql_clip(text), show_alert=show_alert)
    except Exception:
        logger.warning("quicklink: پاسخ به callback ناموفق بود", exc_info=True)
        try:
            await cb.answer()
        except Exception:
            pass


async def _ql_edit(cb, text: str, kb) -> None:
    """ویرایشِ پیام؛ «message is not modified» نادیده گرفته می‌شه (نه ارسالِ پیامِ تکراری)."""
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logger.exception("quicklink: نمایشِ صفحه ناموفق بود")


# === PATCH_QUICKLINK_SHORT_TOKEN_V1 ===
# علتِ اصلیِ «دکمه رو می‌زنم هیچ اتفاقی نمی‌افته»: کلیدِ پلتفرم/نوع (pkey/tkey)
# وقتی حالتِ «دسته‌بندیِ خودِ ارائه‌دهنده» فعاله یه رشتهٔ خامِ بلند و فارسی/انگلیسیه
# (مثلاً «Instagram Followers [Fast]»). این رشته مستقیم تو callback_data می‌رفت و
# تلگرام بیشتر از ۶۴ بایت رو با BUTTON_DATA_INVALID رد می‌کنه؛ یعنی کلِ edit_text
# شکست می‌خورد و هیچ صفحه‌ای نشون داده نمی‌شد. حالا فقط یه توکنِ کوتاهِ ۸ حرفی
# می‌رود تو callback_data و موقعِ کلیک از رویِ سرویس‌هایِ ربات دوباره به کلیدِ
# اصلی برگردونده می‌شه (بدونِ ذخیرهٔ اضافه). لینک‌هایِ قدیمی (کلیدِ خام) هم کار می‌کنن.
import hashlib as _hashlib


def _ql_tok(key: str) -> str:
    return _hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:8]


async def _ql_resolve(bot_id: int, tok: str, idx: int) -> str:
    """idx=0 → پلتفرم، idx=1 → نوعِ سرویس. اگه توکن پیدا نشد همون ورودی برمی‌گرده."""
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(SmmService.bot_id == bot_id))).all()
    for v in svcs:
        k = _svc_platform_type(v)[idx]
        if _ql_tok(k) == tok:
            return k
    return tok


async def _ql_locked_alert(cb, row, mb_id: int) -> None:
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
    if not mb or mb.bot_id != row.id:
        await _ql_alert(cb, "پیدا نشد", True)
        return
    from tenant import _thb_action_label
    try:
        act = _thb_action_label(mb)
    except Exception:
        act = mb.action or "؟"
    await _ql_alert(
        cb,
        f"🔒 «{mb.label[:25]}» از قبل عملکردِ «{act[:40]}» داره.\n"
        "اول از «تنظیماتِ دکمه ← عملکرد» پاکش کن، بعد اینجا وصلش کن.",
        True)

# === PATCH_SMM_QUICKLINK_V1 ===
# درخواستِ ادمین: به‌جایِ اینکه برایِ وصل‌کردنِ یه سرویس به یه دکمهٔ
# صفحهٔ اصلی بره تو تنظیماتِ همون دکمه (که از قبل با thb:setsmmproduct
# وجود داشت، ولی فقط تک‌سرویس بود)، از همین‌جا (مدیریتِ محصولات) یه
# میان‌بر داشته باشه که هم می‌تونه یه دسته (پلتفرم+نوع) رو کامل وصل کنه،
# هم چندتا سرویسِ مشخص رو با تیک‌زدن. مسیرِ انتخاب همون پلتفرم→نوع→آیتم
# ه، دقیقاً مثلِ چیزی که خودِ ادمین موقعِ افزودن طی می‌کنه، و از همون
# طبقه‌بندیِ تأییدشده (نه حدسی) استفاده می‌کنه (_svc_platform_type).
#
# نکتهٔ مهم: MainButton.action فقط یک مقدار می‌تونه داشته باشه. اگه از
# قبل رویِ دکمه یه عملکردِ دیگه (غیر از smm_direct_product) ست شده،
# این‌جا نمی‌ذاریم انتخابش کنه — باید اول از «تنظیماتِ دکمه» پاکش کنه.

async def _quicklink_buttons_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import _thb_action_label
    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id).order_by(MainButton.parent_id.is_(None).desc(),
                                                   MainButton.row, MainButton.col))).all()
    if not btns:
        return ("هنوز هیچ دکمه‌ای تویِ صفحهٔ اصلیِ ربات نساختی.\n\n"
                "اول از بخشِ «دکمه‌های صفحهٔ اصلی» چند دکمه بساز، بعد بیا اینجا وصلشون کن."), InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu", style=ButtonStyle.DANGER)]])
    top = [b for b in btns if b.parent_id is None]
    children_of = {}
    for b in btns:
        if b.parent_id is not None:
            children_of.setdefault(b.parent_id, []).append(b)
    lines = [
        "⚡️ <b>اتصالِ سریعِ محصولات به دکمه‌ها</b>\n",
        "یه دکمه‌ی صفحهٔ اصلی رو انتخاب کن تا یه سرویس یا یه دستهٔ کامل رو مستقیم بهش وصل کنی.\n",
        "🔒 دکمه‌هایی که از قبل عملکردِ دیگه‌ای دارن قفلن — اول از «تنظیماتِ دکمه» عملکردشون رو پاک کن، بعد اینجا وصل‌شون کن.\n",
    ]
    rows = []

    def _btn_row(b, indent: str):
        locked = b.action is not None and b.action != "smm_direct_product"
        if locked:
            label = f"{indent}🔒 {b.label}"
            cb_data = f"smm:quicklink:locked:{b.id}"
        elif b.action == "smm_direct_product":
            label = f"{indent}✅ {b.label}"
            cb_data = f"smm:quicklink:pick:{b.id}"
        else:
            label = f"{indent}⚪️ {b.label}"
            cb_data = f"smm:quicklink:pick:{b.id}"
        rows.append([InlineKeyboardButton(text=label, callback_data=cb_data)])

    for b in top:
        _btn_row(b, "")
        for c in children_of.get(b.id, []):
            rows.append([InlineKeyboardButton(text=f"↳ زیرمنو: {c.label}", callback_data="noop")])
            _btn_row(c, "    ")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:quicklink:menu")
async def smm_quicklink_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    text, kb = await _quicklink_buttons_content(row.id)
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:locked:"))
async def smm_quicklink_locked(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await _ql_locked_alert(cb, row, int(cb.data.split(":")[3]))


@smm_router.callback_query(F.data.startswith("smm:quicklink:pick:"))
async def smm_quicklink_pick_button(cb: CallbackQuery, bot: Bot):
    """قدمِ ۱: دکمه انتخاب شد — حالا مبدأِ انتخاب رو ریست کن و برو مرحلهٔ پلتفرم."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    mb_id = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
    if not mb or mb.bot_id != row.id:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    if mb.action is not None and mb.action != "smm_direct_product":
        # دفاعِ دوم — حتی اگه یه لینکِ کهنه/کش‌شده باشه
        await _ql_locked_alert(cb, row, mb_id)
        return
    await _set_setting(row.id, f"quicklink_sel_{mb_id}", "[]")
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    # گزارشِ ادمین: وقتی چند پنل/ارائه‌دهنده وصل می‌کنی، محصولاتِ همه‌شون
    # این‌جا با هم قاطی نشون داده می‌شد. اگه بیشتر از یک گزینه هست (پنلِ
    # پیش‌فرض + حداقل یه ارائه‌دهندهٔ دیگه، یا چندتا ارائه‌دهنده)، اول
    # می‌پرسیم از کدوم پنل — دقیقاً همون قدمی که «➕ افزودنِ محصول» هم داره.
    choices = await _smm_panel_choices(row.id)
    if len(choices) > 1:
        rows = [[InlineKeyboardButton(text=label, callback_data=f"smm:quicklink:panelpick:{mb_id}:{val}")]
                for val, label in choices]
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:quicklink:menu", style=ButtonStyle.DANGER)])
        await cb.message.edit_text(
            "⚡️ <b>اتصالِ سریعِ محصولات</b>\n\nاول انتخاب کن از کدوم پنل می‌خوای محصول به این دکمه اضافه کنی:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
        await cb.answer()
        return
    # === PATCH_SMM_QUICKLINK_V1 ===
    try:
        text, kb = await _quicklink_platforms_content(row.id, mb_id)
    except Exception:
        logger.exception("quicklink: ساختِ صفحهٔ پلتفرم‌ها ناموفق بود")
        await _ql_alert(cb, "⚠️ خطا در ساختِ صفحه؛ لاگِ ربات رو ببین.", True)
        return
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:panelpick:"))
async def smm_quicklink_panelpick(cb: CallbackQuery, bot: Bot):
    """ذخیرهٔ انتخابِ پنل برایِ این دکمه، بعد رفتن به مرحلهٔ پلتفرم‌ها."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, val = int(parts[3]), parts[4]
    await _set_setting(row.id, f"quicklink_panel_{mb_id}", val)
    text, kb = await _quicklink_platforms_content(row.id, mb_id)
    await _ql_edit(cb, text, kb)
    await cb.answer()


async def _quicklink_cart_rows(bot_id: int, mb_id: int) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """=== PATCH_SMM_QUICKLINK_CART_V1 ===
    باگِ گزارش‌شده نبود، فقط دیده نمی‌شد: بافرِ انتخاب (quicklink_sel_*)
    از اول هم بینِ دسته‌های مختلف مشترک بود — یعنی از «لایک» یکی تیک بزنی
    بعد بری تو «فالوور» یکی دیگه تیک بزنی، هر دو با هم می‌موندن. مشکل
    این بود که این «سبد» فقط تویِ صفحهٔ خودِ یه دسته دیده می‌شد، نه تو
    صفحه‌ی پلتفرم‌ها/دسته‌ها — پس ادمین حسِ گم‌شدنِ انتخاب‌هاش رو می‌کرد.
    این تابع یه خلاصه‌ی همیشه‌-قابل‌مشاهده می‌سازه که رو هر سه صفحه
    (پلتفرم‌ها، دسته‌ها، آیتم‌ها) نشون داده می‌شه."""
    raw = json.loads(await _get_setting(bot_id, f"quicklink_sel_{mb_id}", "[]"))
    if not raw:
        return "", []
    async with Session() as s:
        names = (await s.scalars(select(SmmService.name).where(
            SmmService.id.in_(raw), SmmService.bot_id == bot_id))).all()
    preview = "، ".join(n[:20] for n in names[:3]) + (" ..." if len(names) > 3 else "")
    text = f"\n🛒 <b>سبدِ انتخاب‌شده تا الان: {len(raw)} مورد</b> ({preview})\n"
    rows = [
        [InlineKeyboardButton(text=f"✅ ثبتِ نهاییِ سبد ({len(raw)} مورد)",
                              callback_data=f"smm:quicklink:save:{mb_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🗑 خالی‌کردنِ سبد", callback_data=f"smm:quicklink:clearsel:{mb_id}", style=ButtonStyle.DANGER)],
    ]
    return text, rows


async def _quicklink_platforms_content(bot_id: int, mb_id: int) -> tuple[str, InlineKeyboardMarkup]:
    await sync_category_platforms(bot_id)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    filter_raw = await _get_setting(bot_id, f"quicklink_panel_{mb_id}", "")
    active, panel_pk = _quicklink_panel_where(filter_raw)
    async with Session() as s:
        q = select(SmmService).where(SmmService.bot_id == bot_id, SmmService.is_active == True)
        if active:
            q = q.where(SmmService.panel_pk == panel_pk)
        svcs = (await s.scalars(q)).all()
    if not svcs:
        msg = "هنوز سرویسی فعال نکردی — اول از «➕ افزودنِ محصول» چندتا اضافه کن." if not active \
            else "این پنل هنوز سرویسِ فعالی نداره. یه پنلِ دیگه انتخاب کن یا از «➕ افزودنِ محصول» چندتا اضافه کن."
        rows0 = []
        choices0 = await _smm_panel_choices(bot_id)
        if len(choices0) > 1:
            rows0.append([InlineKeyboardButton(text="🔀 تغییرِ پنل", callback_data=f"smm:quicklink:pick:{mb_id}")])
        rows0.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:quicklink:menu", style=ButtonStyle.DANGER)])
        return msg, InlineKeyboardMarkup(inline_keyboard=rows0)
    tree: dict[str, int] = {}
    for v in svcs:
        p, _t = _svc_platform_type(v)
        tree[p] = tree.get(p, 0) + 1
    # === PATCH_SMM_QUICKLINK_WHOLE_PLATFORM_V1 ===
    # درخواستِ ادمین: بعضی وقت‌ها نمی‌خوای تک‌تکِ سرویس‌هایِ یه پلتفرم رو
    # تیک بزنی — می‌خوای کلِ «اینستاگرام» و کلِ «تلگرام» بیفته رویِ دکمه و
    # مشتری خودش بین همون‌ها جلو بره. پس کنارِ هر پلتفرم یه دکمهٔ تیک هست
    # که همهٔ سرویس‌هایِ فعالِ اون پلتفرم رو یک‌جا به سبد اضافه/حذف می‌کنه.
    # مسیرِ مشتری دست‌نخورده می‌مونه: پلتفرم → نوعِ سرویس → خودِ سرویس.
    selected = set(json.loads(await _get_setting(bot_id, f"quicklink_sel_{mb_id}", "[]")))
    by_platform: dict[str, list[int]] = {}
    for v in svcs:
        by_platform.setdefault(_svc_platform_type(v)[0], []).append(v.id)
    rows = []
    for pkey in sorted(tree.keys(), key=lambda k: -tree[k]):
        ids = by_platform.get(pkey, [])
        all_in = bool(ids) and all(i in selected for i in ids)
        rows.append([
            InlineKeyboardButton(
                text=f"{'☑️' if all_in else '⬜️'} همه",
                callback_data=f"smm:quicklink:allplat:{mb_id}:{_ql_tok(pkey)}"),
            InlineKeyboardButton(
                text=f"{smm_api.platform_label(pkey)} ({tree[pkey]})",
                callback_data=f"smm:quicklink:typ:{mb_id}:{_ql_tok(pkey)}"),
        ])
    # === PATCH_SMM_QUICKLINK_CART_V1 ===
    cart_text, cart_rows = await _quicklink_cart_rows(bot_id, mb_id)
    rows = cart_rows + rows
    # === PATCH_SMM_QUICKLINK_MANUALCAT_V1 ===
    # میان‌بر: به‌جایِ پلتفرم→نوع→آیتم، مستقیم برو سراغِ دسته‌هایِ دستی‌ای
    # که تو «📦 مدیریتِ دسته‌ها» ساخته شده — چندتاشونو با هم تیک بزن.
    rows.append([InlineKeyboardButton(text="📦 اتصالِ مستقیم به دسته‌ها", callback_data=f"smm:quicklink:cats:{mb_id}")])
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    choices = await _smm_panel_choices(bot_id)
    if len(choices) > 1:
        rows.append([InlineKeyboardButton(text="🔀 تغییرِ پنل", callback_data=f"smm:quicklink:pick:{mb_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:quicklink:menu", style=ButtonStyle.DANGER)])
    panel_line = ""
    if active:
        _lbl = dict(choices).get(filter_raw, "")
        panel_line = f"پنل: {_lbl}\n" if _lbl else ""
    text = f"⚡️ <b>اتصالِ سریع</b>\n{panel_line}{cart_text}\nاول پلتفرم رو انتخاب کن — می‌تونی از چند دسته/پلتفرمِ مختلف هم تیک بزنی، همه‌شون تو همون سبد جمع می‌مونن:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_SMM_QUICKLINK_WHOLE_PLATFORM_V1 ===
@smm_router.callback_query(F.data.startswith("smm:quicklink:allplat:"))
async def smm_quicklink_toggle_platform(cb: CallbackQuery, bot: Bot):
    """تیک/حذفِ همهٔ سرویس‌هایِ یک پلتفرم به‌صورتِ یک‌جا."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, pkey = int(parts[3]), await _ql_resolve(row.id, parts[4], 0)
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    filter_raw = await _get_setting(row.id, f"quicklink_panel_{mb_id}", "")
    active, panel_pk = _quicklink_panel_where(filter_raw)
    async with Session() as s:
        q = select(SmmService).where(SmmService.bot_id == row.id, SmmService.is_active == True)
        if active:
            q = q.where(SmmService.panel_pk == panel_pk)
        svcs = (await s.scalars(q)).all()
    ids = [v.id for v in svcs if _svc_platform_type(v)[0] == pkey]
    if not ids:
        await cb.answer("سرویسی تو این پلتفرم نیست.", show_alert=True)
        return
    key = f"quicklink_sel_{mb_id}"
    selected = set(json.loads(await _get_setting(row.id, key, "[]")))
    if all(i in selected for i in ids):
        selected -= set(ids)
        note = f"➖ {len(ids)} سرویسِ {smm_api.platform_label(pkey)} از سبد حذف شد"
    else:
        selected |= set(ids)
        note = f"➕ کلِ {smm_api.platform_label(pkey)} ({len(ids)} سرویس) به سبد اضافه شد"
    await _set_setting(row.id, key, json.dumps(sorted(selected)))
    text, kb = await _quicklink_platforms_content(row.id, mb_id)
    await _ql_edit(cb, text, kb)
    await _ql_alert(cb, note, False)


@smm_router.callback_query(F.data.startswith("smm:quicklink:typ:"))
async def smm_quicklink_pick_platform(cb: CallbackQuery, bot: Bot):
    """قدمِ ۲: پلتفرم انتخاب شد — انواعِ سرویس (دسته‌ها) رو نشون بده."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, pkey = int(parts[3]), await _ql_resolve(row.id, parts[4], 0)
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    filter_raw = await _get_setting(row.id, f"quicklink_panel_{mb_id}", "")
    active, panel_pk = _quicklink_panel_where(filter_raw)
    async with Session() as s:
        q = select(SmmService).where(SmmService.bot_id == row.id, SmmService.is_active == True)
        if active:
            q = q.where(SmmService.panel_pk == panel_pk)
        svcs = (await s.scalars(q)).all()
    types: dict[str, int] = {}
    for v in svcs:
        p, t = _svc_platform_type(v)
        if p == pkey:
            types[t] = types.get(t, 0) + 1
    if not types:
        await cb.answer("موردی تو این پلتفرم پیدا نشد.", show_alert=True)
        return
    rows = []
    for tkey in sorted(types.keys(), key=lambda k: -types[k]):
        rows.append([InlineKeyboardButton(
            text=f"{smm_api.service_type_label(tkey)} ({types[tkey]})",
            callback_data=f"smm:quicklink:items:{mb_id}:{_ql_tok(pkey)}:{_ql_tok(tkey)}")])
    # === PATCH_SMM_QUICKLINK_CART_V1 ===
    cart_text, cart_rows = await _quicklink_cart_rows(row.id, mb_id)
    rows = cart_rows + rows
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:quicklink:pick:{mb_id}", style=ButtonStyle.DANGER)])
    text = f"⚡️ <b>اتصالِ سریع</b>\n{cart_text}\n{smm_api.platform_label(pkey)}\n\nحالا دسته (نوعِ سرویس) رو انتخاب کن:"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


# === PATCH_SMM_QUICKLINK_MANUALCAT_V1 ===
async def _quicklink_cats_content(bot_id: int, mb_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """صفحه‌ی تیک‌زدنِ دسته‌هایِ دستی برایِ اتصالِ مستقیم به یه دکمه."""
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == bot_id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    if not cats:
        return ("هنوز هیچ دسته‌ی دستی‌ای نساختی.\n\n"
                "اول از «📦 مدیریتِ دسته‌ها» چند دسته بساز، بعد بیا اینجا وصلشون کن."), InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:quicklink:pick:{mb_id}", style=ButtonStyle.DANGER)]])
    overrides = await _platform_label_overrides(bot_id)
    selected = set(json.loads(await _get_setting(bot_id, f"quicklink_catsel_{mb_id}", "[]")))
    rows = []
    _hidden = max(0, len(cats) - 85)  # سقفِ ۱۰۰ دکمه در هر پیام
    if _hidden:
        cats = sorted(cats, key=lambda c: (c.id not in selected, c.id))[:85]
    for c in cats:
        plat_label = overrides.get(c.platform, smm_api.platform_label(c.platform))
        mark = "☑️" if c.id in selected else "⬜️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {plat_label} — {c.name}"[:64],
            callback_data=f"smm:quicklink:cattoggle:{mb_id}:{c.id}")])
    if selected:
        rows.append([InlineKeyboardButton(
            text=f"✅ ثبتِ دسته‌هایِ انتخاب‌شده ({len(selected)} مورد)",
            callback_data=f"smm:quicklink:catsave:{mb_id}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:quicklink:pick:{mb_id}", style=ButtonStyle.DANGER)])
    text = ("📦 <b>اتصالِ مستقیم به دسته‌ها</b>\n\n"
            "یک یا چند دسته رو تیک بزن — هر سرویسِ جدیدی که بعداً به این دسته‌ها اضافه بشه، "
            "خودکار زیرِ همین دکمه هم می‌آد:")
    if _hidden:
        text += f"\n\n(فقط ۸۵ دسته نشان داده شد؛ {_hidden} دستهٔ دیگر مخفی است.)"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smm:quicklink:cats:"))
async def smm_quicklink_show_cats(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    mb_id = int(cb.data.rsplit(":", 1)[1])
    text, kb = await _quicklink_cats_content(row.id, mb_id)
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:cattoggle:"))
async def smm_quicklink_toggle_cat(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, cid = int(parts[3]), int(parts[4])
    key = f"quicklink_catsel_{mb_id}"
    selected = set(json.loads(await _get_setting(row.id, key, "[]")))
    if cid in selected:
        selected.discard(cid)
    else:
        selected.add(cid)
    await _set_setting(row.id, key, json.dumps(sorted(selected)))
    text, kb = await _quicklink_cats_content(row.id, mb_id)
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:catsave:"))
async def smm_quicklink_save_cats(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    mb_id = int(cb.data.rsplit(":", 1)[1])
    key = f"quicklink_catsel_{mb_id}"
    selected = json.loads(await _get_setting(row.id, key, "[]"))
    if not selected:
        await cb.answer("هنوز دسته‌ای تیک نزدی.", show_alert=True)
        return
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb or mb.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if mb.action is not None and mb.action != "smm_direct_product":
            await _ql_locked_alert(cb, row, mb_id)
            return
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.id.in_(selected), SmmCategory.bot_id == row.id))).all()
        mb.action = "smm_direct_product"
        mb.config = json.dumps({"smm_category_ids": [c.id for c in cats]}, ensure_ascii=False)
        await s.commit()
        cat_names = "، ".join(c.name for c in cats[:3]) + (" ..." if len(cats) > 3 else "")
    await _set_setting(row.id, key, "[]")
    await _ql_alert(cb, f"✅ {len(cats)} دسته ({cat_names}) به دکمه‌ی «{mb.label}» وصل شد.", show_alert=True)
    text, kb = await _quicklink_buttons_content(row.id)
    await _ql_edit(cb, text, kb)


@smm_router.callback_query(F.data.startswith("smm:quicklink:clearsel:"))
async def smm_quicklink_clear_selection(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    mb_id = int(cb.data.rsplit(":", 1)[1])
    await _set_setting(row.id, f"quicklink_sel_{mb_id}", "[]")
    await cb.answer("🗑 سبد خالی شد.")
    text, kb = await _quicklink_platforms_content(row.id, mb_id)
    await _ql_edit(cb, text, kb)


# === PATCH_SMM_QUICKLINK_ITEMS_CACHE_V1 ===
# اندازه‌گیریِ زنده: هر «تیک‌زدن» (smm:quicklink:toggle) ≈۴٫۴ثانیه طول می‌کشید با ۶ کوئریِ ۰٫۰ثانیه‌ای ⇒ زمان در پایتون
# خرج می‌شد: همهٔ سرویس‌هایِ فعالِ ربات (هزاران ردیف) با ORM لود و تک‌تک طبقه‌بندی می‌شدند، در هر کلیک، و لوپِ همان
# پروسه را می‌بستند. فهرستِ هر دسته حالا کش می‌شود؛ با تغییرِ «تعداد/بیشینهٔ id»ِ سرویس‌ها خودکار تازه می‌شود (+ حداکثر ۲دقیقه).
_QL_ITEMS_CACHE: dict = {}
_QL_ITEMS_TTL = 120.0


async def _ql_items_for(bot_id: int, filter_raw: str, pkey: str, tkey: str) -> list[tuple[int, str]]:
    import time as _t
    active, panel_pk = _quicklink_panel_where(filter_raw)
    async with Session() as s:
        qf = select(func.count(SmmService.id), func.max(SmmService.id)).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True)  # noqa: E712
        if active:
            qf = qf.where(SmmService.panel_pk == panel_pk)
        fp = tuple((await s.execute(qf)).one())
    ck = (bot_id, filter_raw, pkey, tkey)
    hit = _QL_ITEMS_CACHE.get(ck)
    if hit and hit[0] == fp and _t.monotonic() - hit[1] < _QL_ITEMS_TTL:
        return hit[2]
    async with Session() as s:
        q = select(SmmService).where(SmmService.bot_id == bot_id, SmmService.is_active == True)  # noqa: E712
        if active:
            q = q.where(SmmService.panel_pk == panel_pk)
        svcs = (await s.scalars(q)).all()
    items = [(v.id, v.name or "") for v in svcs if _svc_platform_type(v) == (pkey, tkey)]
    _QL_ITEMS_CACHE[ck] = (fp, _t.monotonic(), items)
    return items


async def _quicklink_items_content(bot_id: int, mb_id: int, pkey: str, tkey: str) -> tuple[str, InlineKeyboardMarkup]:
    # === PATCH_SMM_QUICKLINK_PANEL_FILTER_V1 ===
    filter_raw = await _get_setting(bot_id, f"quicklink_panel_{mb_id}", "")
    items = await _ql_items_for(bot_id, filter_raw, pkey, tkey)
    if not items:
        return "موردی تو این دسته نیست.", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:quicklink:typ:{mb_id}:{_ql_tok(pkey)}", style=ButtonStyle.DANGER)]])
    selected = set(json.loads(await _get_setting(bot_id, f"quicklink_sel_{mb_id}", "[]")))
    rows = [[InlineKeyboardButton(
        text="✅ اتصالِ کلِ این دسته (بدونِ نیاز به تیک‌زدنِ تک‌تک)",
        callback_data=f"smm:quicklink:savewhole:{mb_id}:{_ql_tok(pkey)}:{_ql_tok(tkey)}", style=ButtonStyle.SUCCESS)]]
    for v_id, v_name in items[:60]:
        mark = "☑️" if v_id in selected else "⬜️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {(v_name or '')[:40]}",
            callback_data=f"smm:quicklink:toggle:{mb_id}:{_ql_tok(pkey)}:{_ql_tok(tkey)}:{v_id}")])
    # === PATCH_SMM_QUICKLINK_CART_V1 ===
    # دکمه‌ی ثبت دیگه فقط برایِ همین دسته نیست — کلِ سبد رو (حتی اگه از
    # دسته‌هایِ دیگه هم چیزی توش باشه) با هم ثبت می‌کنه؛ برایِ همین دیگه
    # نیازی به pkey/tkey تو callback_dataش نیست.
    cart_text, cart_rows = await _quicklink_cart_rows(bot_id, mb_id)
    rows += cart_rows
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:quicklink:typ:{mb_id}:{_ql_tok(pkey)}", style=ButtonStyle.DANGER)])
    header = f"{smm_api.platform_label(pkey)} — {smm_api.service_type_label(tkey)}"
    text = (f"⚡️ <b>اتصالِ سریع</b>\n\n{header}\n{cart_text}\n"
            "یا کلِ این دسته رو یک‌جا وصل کن، یا با تیک‌زدن یک یا چند موردِ مشخص انتخاب کن — "
            "می‌تونی از دسته‌های دیگه هم بری تیک بزنی، همه با هم تویِ یک دکمه ثبت می‌شن:")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smm:quicklink:items:"))
async def smm_quicklink_show_items(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id = int(parts[3])
    pkey, tkey = await _ql_resolve(row.id, parts[4], 0), await _ql_resolve(row.id, parts[5], 1)
    text, kb = await _quicklink_items_content(row.id, mb_id, pkey, tkey)
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:toggle:"))
async def smm_quicklink_toggle_item(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, svc_id = int(parts[3]), int(parts[6])
    pkey, tkey = await _ql_resolve(row.id, parts[4], 0), await _ql_resolve(row.id, parts[5], 1)
    key = f"quicklink_sel_{mb_id}"
    selected = set(json.loads(await _get_setting(row.id, key, "[]")))
    if svc_id in selected:
        selected.discard(svc_id)
    else:
        selected.add(svc_id)
    await _set_setting(row.id, key, json.dumps(sorted(selected)))
    text, kb = await _quicklink_items_content(row.id, mb_id, pkey, tkey)
    await _ql_edit(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:quicklink:savewhole:"))
async def smm_quicklink_save_whole(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id = int(parts[3])
    pkey, tkey = await _ql_resolve(row.id, parts[4], 0), await _ql_resolve(row.id, parts[5], 1)
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb or mb.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if mb.action is not None and mb.action != "smm_direct_product":
            await _ql_locked_alert(cb, row, mb_id)
            return
        mb.action = "smm_direct_product"
        mb.config = json.dumps({"smm_platform_key": pkey, "smm_type_key": tkey}, ensure_ascii=False)
        await s.commit()
    await _set_setting(row.id, f"quicklink_sel_{mb_id}", "[]")
    await _ql_alert(
        cb,
        f"✅ کلِ دستهٔ «{smm_api.service_type_label(tkey)}» به «{mb.label[:25]}» وصل شد.\n"
        "سرویسِ جدیدِ همین دسته هم خودکار اضافه می‌شه.",
        True)
    text, kb = await _quicklink_buttons_content(row.id)
    await _ql_edit(cb, text, kb)


@smm_router.callback_query(F.data.startswith("smm:quicklink:save:"))
async def smm_quicklink_save_selected(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id = int(parts[3])
    key = f"quicklink_sel_{mb_id}"
    selected = json.loads(await _get_setting(row.id, key, "[]"))
    if not selected:
        await cb.answer("هنوز چیزی تیک نزدی.", show_alert=True)
        return
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
        if not mb or mb.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if mb.action is not None and mb.action != "smm_direct_product":
            await _ql_locked_alert(cb, row, mb_id)
            return
        mb.action = "smm_direct_product"
        if len(selected) == 1:
            mb.config = json.dumps({"smm_service_id": selected[0]}, ensure_ascii=False)
        else:
            mb.config = json.dumps({"smm_service_ids": selected}, ensure_ascii=False)
        await s.commit()
    await _set_setting(row.id, key, "[]")
    await _ql_alert(cb, f"✅ {len(selected)} سرویس به دکمه‌ی «{mb.label}» وصل شد.", show_alert=True)
    text, kb = await _quicklink_buttons_content(row.id)
    await _ql_edit(cb, text, kb)


# === PATCH_SMM_SORT_EVERYWHERE_V1 ===
# ترتیبِ نمایشِ ادمین (ارزان→گران، گران→ارزان، جدید، قدیمی، دستی) فقط در یک مسیرِ
# نمایش اعمال می‌شد؛ دکمه‌هایِ «اتصالِ سریع»، دسته‌هایِ دستی و میان‌بُرِ vs_* ترتیب نداشتند.
# باگِ اصلی: قیمت‌ها با هم قیاس می‌شدند ولی sell_rate «تومان» است و panel_rate «واحدِ پنل»
# (مثلاً دلار) — مخلوط‌کردنشان ترتیبِ ارزان→گران را به‌هم می‌ریخت. حالا با قیمتِ واقعیِ
# فروشِ هر هزار (compute_sell_price: سود، نرخِ تبدیل، قیمتِ دستی، واحد) مرتب می‌شود.
async def _sort_svc_objs(bot_id: int, svcs: list, sort_order: str) -> list:
    svcs = list(svcs)
    if sort_order in ("price_asc", "price_desc"):
        prices: dict[int, float] = {}
        for v in svcs:
            try:
                prices[v.id] = float(await compute_sell_price(bot_id, v, 1000))
            except Exception:
                logger.exception("مرتب‌سازیِ قیمت: محاسبهٔ قیمتِ سرویس %s ناموفق بود", getattr(v, "id", "?"))
                prices[v.id] = float("inf")
        if sort_order == "price_asc":
            svcs.sort(key=lambda v: (prices[v.id], v.id))
        else:
            svcs.sort(key=lambda v: (-prices[v.id] if prices[v.id] != float("inf") else 1, v.id))
    elif sort_order == "new_first":
        svcs.sort(key=lambda v: (v.created_at or datetime.min, v.id), reverse=True)
    elif sort_order == "old_first":
        svcs.sort(key=lambda v: (v.created_at or datetime.min, v.id))
    elif sort_order == "manual":
        svcs.sort(key=lambda v: (v.sort_order or 0, v.id))
    return svcs


async def _resolve_direct_product_services(bot_id: int, mb: "MainButton") -> list:
    return await _sort_svc_objs(bot_id, await _resolve_direct_product_services_raw(bot_id, mb),
                                await _get_setting(bot_id, "smm_sort_order", "none"))


async def _resolve_direct_product_services_raw(bot_id: int, mb: "MainButton") -> list:
    """=== PATCH_SMM_QUICKLINK_PATH_V1 ===
    از mb.config، لیستِ واقعیِ سرویس‌های فعالِ متصل به این دکمه رو
    برمی‌گردونه (چه تک‌سرویس، چه چندتاییِ تیک‌خورده، چه یه دستهٔ کامل)."""
    cfg = {}
    if mb.config:
        try:
            cfg = json.loads(mb.config)
        except Exception:
            cfg = {}
    async with Session() as s:
        if cfg.get("smm_service_id"):
            v = await s.get(SmmService, int(cfg["smm_service_id"]))
            return [v] if v and v.bot_id == bot_id and v.is_active else []
        if cfg.get("smm_service_ids"):
            ids = [int(x) for x in cfg["smm_service_ids"]]
            svcs = (await s.scalars(select(SmmService).where(
                SmmService.id.in_(ids), SmmService.bot_id == bot_id, SmmService.is_active == True))).all()
            return list(svcs)
        if cfg.get("smm_platform_key") and cfg.get("smm_type_key"):
            all_svcs = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == bot_id, SmmService.is_active == True))).all()
            await sync_category_platforms(bot_id, all_svcs)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1
            return [v for v in all_svcs if _svc_platform_type(v) == (cfg["smm_platform_key"], cfg["smm_type_key"])]
        # === PATCH_SMM_QUICKLINK_MANUALCAT_V1 ===
        if cfg.get("smm_category_ids"):
            ids = [int(x) for x in cfg["smm_category_ids"]]
            svcs = (await s.scalars(select(SmmService).where(
                SmmService.category_pk.in_(ids), SmmService.bot_id == bot_id, SmmService.is_active == True))).all()
            return list(svcs)
    return []


def _qlu_root_content(mb_id: int, svcs: list) -> tuple[str, InlineKeyboardMarkup]:
    tree: dict[str, int] = {}
    for v in svcs:
        p, _t = _svc_platform_type(v)
        tree[p] = tree.get(p, 0) + 1
    rows = [[InlineKeyboardButton(text=f"{smm_api.platform_label(p)} ({n})",
                                  callback_data=f"smm:qlu:{mb_id}:plat:{p}")]
            for p, n in sorted(tree.items(), key=lambda kv: -kv[1])]
    # === PATCH_SMM_QUICKLINK_BACKNAV_V1 ===
    # این صفحه (انتخابِ پلتفرم) بالاترین سطحِ این مسیره — پس اینجا واقعاً
    # جایی برایِ «یک قدم عقب» نیست، فقط منویِ اصلی معنی داره.
    rows.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
    return "🛰 پلتفرم موردنظر رو انتخاب کن:", InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_SMM_QUICKLINK_CATNAV_V1 ===
def _mb_direct_cfg(mb: "MainButton") -> dict:
    """کانفیگِ خامِ دکمه‌یِ smm_direct_product را برمی‌گرداند (یا {} اگر نامعتبر بود)."""
    if not mb.config:
        return {}
    try:
        return json.loads(mb.config)
    except Exception:
        return {}


async def _qlc_category_names(bot_id: int, cat_ids: list[int]) -> dict[int, str]:
    """نگاشتِ category_pk → «پلتفرم — نامِ دسته» برایِ دسته‌هایِ دستیِ ادمین."""
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.id.in_(cat_ids), SmmCategory.bot_id == bot_id))).all()
    overrides = await _platform_label_overrides(bot_id)
    return {c.id: f"{overrides.get(c.platform, smm_api.platform_label(c.platform))} — {c.name}" for c in cats}


# === PATCH_SMM_BTN_LAYOUT_V1 ===
async def _btn_cols(bot_id: int) -> int:
    """تعدادِ ستونِ دکمه‌هایِ فهرست (۱ یا ۲)."""
    try:
        return 2 if (await _get_setting(bot_id, "smm_btn_cols", "1")) == "2" else 1
    except Exception:
        return 1


async def _show_count(bot_id: int) -> bool:
    """آیا تعداد داخلِ پرانتز نشان داده شود؟"""
    try:
        return (await _get_setting(bot_id, "smm_btn_count", "1")) != "0"
    except Exception:
        return True


# === PATCH_SMM_LIST_PAGINATION_V1 ===
# لاگِ سرور: «Bad Request: reply markup is too long» — نوعی مثلِ «ممبر» با ده‌ها سرویس (نامِ فارسیِ بلند)
# از سقفِ حجمِ کیبوردِ تلگرام رد می‌شد و مشتری با زدنِ دکمه هیچ چیزی نمی‌دید. سقفِ ۸۰ تایی هم کافی نبود
# (سقفِ تلگرام حجمِ کل است نه تعداد). حالا همهٔ فهرست‌هایِ سرویس ۳۰تایی صفحه‌بندی می‌شوند.
_SVC_LIST_PAGE = 30


def _page_slice(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + _SVC_LIST_PAGE - 1) // _SVC_LIST_PAGE)
    page = max(1, min(int(page or 1), pages))
    return items[(page - 1) * _SVC_LIST_PAGE: page * _SVC_LIST_PAGE], page, pages


def _page_nav(cb_prefix: str, page: int, pages: int) -> list | None:
    """ردیفِ «◀️ قبلی | n/m | بعدی ▶️»؛ None اگر فقط یک صفحه است. callback = f"{cb_prefix}:{n}"."""
    if pages <= 1:
        return None
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"{cb_prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        row.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"{cb_prefix}:{page + 1}"))
    return row


def _chunk_rows(buttons: list, cols: int) -> list:
    """فهرستِ تختِ دکمه‌ها را به ردیف‌هایِ cols‌تایی تبدیل می‌کند."""
    if cols <= 1:
        return [[b] for b in buttons]
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


async def _qlc_root_content(bot_id: int, mb_id: int, svcs: list) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_SMM_QUICKLINK_CATNAV_V1 ===
    سطحِ اولِ مسیر برایِ دکمه‌یِ متصل‌شده به «دسته‌هایِ دستی»: بر خلافِ
    _qlu_root_content که بر اساسِ پلتفرمِ حدسی/تشخیصی گروه‌بندی می‌کرد،
    این یکی از رویِ همون نامِ دسته‌ای که خودِ ادمین ساخته (category_pk)
    گروه‌بندی می‌کنه — چون درخواستِ ادمین این بود که اول اسمِ دسته دیده
    بشه، نه اسمِ پلتفرمِ حدسی."""
    by_cat: dict[int, int] = {}
    for v in svcs:
        cid = getattr(v, "category_pk", None)
        if cid:
            by_cat[cid] = by_cat.get(cid, 0) + 1
    names = await _qlc_category_names(bot_id, list(by_cat.keys()))
    # PATCH_SMM_BTN_LAYOUT_V1
    _cols = await _btn_cols(bot_id)
    _cnt = await _show_count(bot_id)
    _btns = [InlineKeyboardButton(
        text=(f"{names.get(cid, '؟')} ({n})" if _cnt else f"{names.get(cid, '؟')}"),
        callback_data=f"smm:qlc:{mb_id}:cat:{cid}")
        for cid, n in sorted(by_cat.items(), key=lambda kv: -kv[1])]
    rows = _chunk_rows(_btns, _cols)
    rows.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
    return "📦 دستهٔ موردنظر رو انتخاب کن:", InlineKeyboardMarkup(inline_keyboard=rows)


async def show_smm_direct_product_entry(msg, bot: Bot, bot_id: int, mb_id: int, state: FSMContext) -> None:
    """=== PATCH_SMM_QUICKLINK_PATH_V1 (+ PATCH_SMM_QUICKLINK_CATNAV_V1) ===
    نقطهٔ ورودِ دکمه‌ای که با «اتصالِ سریع» ساخته شده. درخواستِ صریحِ ادمین:
    وقتی بیش از یک سرویس به دکمه وصله، به‌جایِ یه لیستِ تختِ همه‌چی‌باهم،
    دقیقاً همون مسیری که خودِ ادمین موقعِ وصل‌کردن طی کرد رو به مشتری هم
    نشون بده. برایِ دکمه‌هایی که مستقیم به «دسته‌هایِ دستی» وصل شدن، این
    مسیر: نامِ دسته (مثلاً «اینستاگرام») → نوعِ سرویس (مثلاً «ممبر») →
    خودِ سرویس‌ها. برایِ بقیهٔ حالت‌ها (پلتفرم/نوع یا چند سرویسِ تیک‌خورده)
    همون مسیرِ قبلیِ پلتفرم → نوع → سرویس. اگه دقیقاً یک سرویس وصل باشه،
    بدونِ این مسیرِ اضافه، مستقیم می‌ره صفحه‌ی خرید."""
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
    if not mb or mb.bot_id != bot_id:
        await msg.answer("⚠️ این دکمه دیگر در دسترس نیست.")
        return
    svcs = await _resolve_direct_product_services(bot_id, mb)
    if not svcs:
        await msg.answer("⚠️ هنوز هیچ سرویسی به این دکمه وصل نشده یا دیگر در دسترس نیست.")
        return
    # === PATCH_QLC_ALWAYS_ROOT_V1 ===
    # درخواستِ ادمین: برایِ دکمههایِ متصل به «دسته‌هایِ دستی» همیشه اول
    # نامِ دسته دیده شود — حتی وقتی فقط یک دسته یا یک سرویس وصل است.
    # مسیرِ کامل: دسته → نوع سرویس → خودِ سرویس.
    cfg = _mb_direct_cfg(mb)
    if len(svcs) == 1 and not cfg.get("smm_category_ids"):
        ok = await send_smm_product_page(msg, bot, svcs[0].id, state)
        if not ok:
            await msg.answer("⚠️ این سرویس دیگر در دسترس نیست.")
        return
    if cfg.get("smm_category_ids"):
        text, kb = await _qlc_root_content(bot_id, mb_id, svcs)
    else:
        text, kb = _qlu_root_content(mb_id, svcs)
    await msg.answer(text, reply_markup=kb)


@smm_router.callback_query(F.data.startswith("smm:qlc:"))
async def smm_qlc_nav(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_QUICKLINK_CATNAV_V1 ===
    نسخه‌یِ smm_qlu_nav ولی برایِ دکمه‌هایِ متصل به «دسته‌هایِ دستی»:
    نامِ دسته → نوعِ سرویس → خودِ سرویس (با breadcrumb)."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, kind = int(parts[2]), parts[3]
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
    if not mb or mb.bot_id != row.id:
        await cb.answer("این دکمه دیگر در دسترس نیست", show_alert=True)
        return
    svcs = await _resolve_direct_product_services(row.id, mb)
    if not svcs:
        await cb.answer("سرویسی در دسترس نیست", show_alert=True)
        return
    if kind == "root":
        text, kb = await _qlc_root_content(row.id, mb_id, svcs)
    elif kind == "cat":
        cat_pk = int(parts[4])
        cat_svcs = [v for v in svcs if getattr(v, "category_pk", None) == cat_pk]
        names = await _qlc_category_names(row.id, [cat_pk])
        cat_name = names.get(cat_pk, "؟")

        # === PATCH_SMM_QLC_DEPTH_V1 ===
        # عمقِ ۲: مرحلهٔ «نوعِ سرویس» رد می‌شود و خودِ سرویس‌ها می‌آیند.
        _depth = await _get_setting(row.id, "qlc_depth", "3")
        if _depth == "2":
            _pg = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 1
            _page_items, _pg, _pages = _page_slice(cat_svcs, _pg)   # PATCH_SMM_LIST_PAGINATION_V1
            rows = [[InlineKeyboardButton(
                text=v.name[:55],
                callback_data=f"smmu:svcbraw:{v.id}:{mb_id}")]
                for v in _page_items]
            _nav = _page_nav(f"smm:qlc:{mb_id}:cat:{cat_pk}", _pg, _pages)
            if _nav:
                rows.append(_nav)
            rows.append([InlineKeyboardButton(
                text="⬅️ بازگشت", callback_data=f"smm:qlc:{mb_id}:root",
                style=ButtonStyle.DANGER)])
            text = f"📦 {cat_name}\n\nسرویسِ موردنظر را انتخاب کن:"
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            try:
                await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
            await cb.answer()
            return

        types: dict[str, int] = {}
        for v in cat_svcs:
            _p, t = _svc_platform_type(v)
            types[t] = types.get(t, 0) + 1
        # PATCH_SMM_BTN_LAYOUT_V1
        _cols = await _btn_cols(row.id)
        _cnt = await _show_count(row.id)
        _btns = [InlineKeyboardButton(
            text=(f"{smm_api.service_type_label(t)} ({n})" if _cnt
                  else smm_api.service_type_label(t)),
            callback_data=f"smm:qlc:{mb_id}:typ:{cat_pk}:{t}")
            for t, n in sorted(types.items(), key=lambda kv: -kv[1])]
        rows = _chunk_rows(_btns, _cols)
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:qlc:{mb_id}:root", style=ButtonStyle.DANGER)])
        text = f"📦 {cat_name}\n\nنوع سرویس را انتخاب کن:"
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:  # kind == "typ"
        cat_pk, tkey = int(parts[4]), parts[5]
        _pg = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 1
        names = await _qlc_category_names(row.id, [cat_pk])
        cat_name = names.get(cat_pk, "؟")
        items = [v for v in svcs if getattr(v, "category_pk", None) == cat_pk and _svc_platform_type(v)[1] == tkey]
        items, _pg, _pages = _page_slice(items, _pg)   # PATCH_SMM_LIST_PAGINATION_V1
        breadcrumb = f"{cat_name} › {smm_api.service_type_label(tkey)}"
        rows = [[InlineKeyboardButton(text=v.name[:55], callback_data=f"smmu:svcbraw:{v.id}:{mb_id}")]
                for v in items]
        _nav = _page_nav(f"smm:qlc:{mb_id}:typ:{cat_pk}:{tkey}", _pg, _pages)
        if _nav:
            rows.append(_nav)
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:qlc:{mb_id}:cat:{cat_pk}", style=ButtonStyle.DANGER)])
        text = f"📦 {breadcrumb}\n\nسرویس موردنظر را انتخاب کن:"
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        # === PATCH_SMM_QUICKLINK_CATNAV_V1 ===
        # چون breadcrumb اینجا از رویِ نامِ دستهٔ دستی ساخته می‌شه (نه
        # پلتفرمِ حدسی)، مستقیم تویِ callback_data جا نمی‌شه — بجاش تو
        # یه نگاشتِ موقتِ per-button ذخیره‌ش می‌کنیم (سطرِ پایین).
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smmu:svcbraw:"))
async def smmu_pick_service_breadcrumb_raw(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_SMM_QUICKLINK_CATNAV_V1 ===
    مثلِ smmu_pick_service_breadcrumb، فقط breadcrumb (که بر اساسِ نامِ
    دستهٔ دستی ساخته شده، نه پلتفرمِ حدسی) رو از تنظیماتِ موقتِ همون
    دکمه می‌خونه، چون طولِ متنش تو callback_data جا نمی‌شه."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, mb_id = int(parts[2]), int(parts[3])
    # === PATCH_SMM_CRUMB_PER_SERVICE_V1 ===
    # باگ (فدایی): مسیرِ بالایِ صفحهٔ محصول («تلگرام — ممبر گروه › … › محصول») اشتباه بود. علت: مسیر در یک
    # تنظیمِ مشترکِ هر دکمه (quicklink_crumb_{mb_id}) ذخیره می‌شد؛ پس مسیرِ آخرین کسی که آن دکمه را
    # باز کرده (یا ادمین) برایِ همه می‌آمد، و در حالتِ عمقِ ۲ اصلاً ست نمی‌شد. حالا مسیر همان لحظه از رویِ
    # خودِ محصول ساخته می‌شود.
    breadcrumb = None
    try:
        async with Session() as s_:
            sv = await s_.get(SmmService, vid)
            if sv is not None and await _is_foreign(sv, bot):   # PATCH_TENANT_SCOPE_V1
                sv = None
        if sv is not None and getattr(sv, "category_pk", None):
            _nm = (await _qlc_category_names(row.id, [sv.category_pk])).get(sv.category_pk)
            if _nm:
                if (await _get_setting(row.id, "qlc_depth", "3")) == "2":
                    breadcrumb = _nm
                else:
                    breadcrumb = f"{_nm} › {smm_api.service_type_label(_svc_platform_type(sv)[1])}"
    except Exception:
        logger.exception("[smm] ساختِ مسیرِ محصول ناموفق بود")
    ok = await send_smm_product_page(cb, bot, vid, state, breadcrumb=breadcrumb)
    if not ok:
        await cb.answer("این سرویس دیگر موجود نیست", show_alert=True)
        return
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:qlu:"))
async def smm_qlu_nav(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_QUICKLINK_PATH_V1 ===
    مرحله‌های ۲ و ۳ برایِ مشتری: انتخابِ نوعِ سرویس، بعد خودِ سرویس —
    هر دو از callback_dataیِ یکسان (smm:qlu:{mb_id}:plat/typ:...) رد
    می‌شن و بسته به بخشِ سوم منشعب می‌شن.
    === PATCH_SMM_QUICKLINK_BACKNAV_V1 ===
    گزارشِ ادمین: «بازگشت» تو این مسیر می‌پروند به منویِ اصلی، نه یک
    قدم عقب. الان هر سطح دقیقاً یک قدم برمی‌گرده: لیستِ سرویس‌ها → لیستِ
    نوع‌ها، لیستِ نوع‌ها → لیستِ پلتفرم‌ها؛ فقط بالاترین سطح (لیستِ
    پلتفرم‌ها) به منویِ اصلی می‌ره، چون واقعاً جایِ دیگه‌ای نداره بره."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    mb_id, kind = int(parts[2]), parts[3]
    async with Session() as s:
        mb = await s.get(MainButton, mb_id)
    if not mb or mb.bot_id != row.id:
        await cb.answer("این دکمه دیگر در دسترس نیست", show_alert=True)
        return
    svcs = await _resolve_direct_product_services(row.id, mb)
    if not svcs:
        await cb.answer("سرویسی در دسترس نیست", show_alert=True)
        return
    if kind == "root":
        text, kb = _qlu_root_content(mb_id, svcs)
    elif kind == "plat":
        pkey = parts[4]
        types: dict[str, int] = {}
        for v in svcs:
            p, t = _svc_platform_type(v)
            if p == pkey:
                types[t] = types.get(t, 0) + 1
        rows = [[InlineKeyboardButton(text=f"{smm_api.service_type_label(t)} ({n})",
                                      callback_data=f"smm:qlu:{mb_id}:typ:{pkey}:{t}")]
                for t, n in sorted(types.items(), key=lambda kv: -kv[1])]
        # === PATCH_SMM_QUICKLINK_BACKNAV_V1 ===
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:qlu:{mb_id}:root", style=ButtonStyle.DANGER)])
        text = f"{smm_api.platform_label(pkey)}\n\nنوع سرویس را انتخاب کن:"
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:  # kind == "typ"
        pkey, tkey = parts[4], parts[5]
        _pg = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 1
        items = [v for v in svcs if _svc_platform_type(v) == (pkey, tkey)]
        items, _pg, _pages = _page_slice(items, _pg)   # PATCH_SMM_LIST_PAGINATION_V1
        # === PATCH_SMM_QUICKLINK_BREADCRUMB_V1 ===
        # درخواستِ ادمین: توی مسیرِ «اتصالِ سریع»، وقتی مشتری تا اینجا
        # (پلتفرم → نوع → سرویس) اومده، صفحهٔ نهاییِ خرید هم مسیرِ کامل
        # (مثلاً «اینستاگرام › ممبر › نامِ سرویس») رو نشون بده، نه فقط
        # اسمِ سرویس تنها.
        rows = [[InlineKeyboardButton(text=v.name[:55], callback_data=f"smmu:svcb:{v.id}:{pkey}:{tkey}")] for v in items]
        _nav = _page_nav(f"smm:qlu:{mb_id}:typ:{pkey}:{tkey}", _pg, _pages)
        if _nav:
            rows.append(_nav)
        # === PATCH_SMM_QUICKLINK_BACKNAV_V1 ===
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:qlu:{mb_id}:plat:{pkey}", style=ButtonStyle.DANGER)])
        text = f"{smm_api.platform_label(pkey)} — {smm_api.service_type_label(tkey)}\n\nسرویس موردنظر را انتخاب کن:"
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "smm:menu")
async def smm_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    # === PATCH_SMM_NAV_FIX_V1 ===
    await _set_browse_origin(row.id, "menu")
    text, kb = await smm_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ── اتصال به پنل ──

@smm_router.callback_query(F.data == "smm:setpanel")
async def smm_setpanel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    await state.set_state(SmmFSM.api_url)
    await cb.message.answer(
        "🔗 <b>اتصال به پنل SMM</b>\n\n"
        "۱/۲ — آدرس پنل را بفرست.\n"
        "می‌تونی فقط دامنه رو بدی (مثل <code>https://panel.example.com</code>) "
        "یا آدرس کامل API (مثل <code>https://panel.example.com/api/v2</code>).",
        parse_mode="HTML")
    await cb.answer()


@smm_router.message(SmmFSM.api_url, F.text)
async def smm_api_url_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.update_data(smm_api_url=msg.text.strip())
    await state.set_state(SmmFSM.api_key)
    await msg.answer("۲/۲ — حالا <b>API Key</b> پنل را بفرست:", parse_mode="HTML")


@smm_router.message(SmmFSM.api_key, F.text)
async def smm_api_key_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    data = await state.get_data()
    api_url = data.get("smm_api_url", "")
    api_key = msg.text.strip()
    await msg.answer("⏳ در حال بررسی اتصال...")
    ok, message = await smm_api.validate(api_url, api_key)
    if not ok:
        await state.clear()
        await msg.answer(
            f"{message}\n\nدوباره از «🔗 اتصال به پنل» شروع کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:menu")]]))
        return
    await _set_setting(row.id, "smm_api_url", api_url)
    await _set_setting(row.id, "smm_api_key", api_key)
    await state.clear()
    text, kb = await smm_admin_content(row.id)
    await msg.answer(f"{message}\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ چند پنلی ═══════════════════════
# === PATCH_SMM_MULTI_PANEL_V1 ===
# قبلاً هر ربات فقط یک پنلِ SMM می‌توانست وصل کند (یک api_url/api_key
# سطحِ ربات). این بخش امکانِ اضافه‌کردنِ چند پنل را می‌دهد — مثلاً یک پنل
# برای ممبر/ویو و یک پنلِ دیگر با قیمتِ بهتر برای لایک، یا یک پنلِ
# پشتیبان اگر اولی قطع بود. پنلِ قدیمی (تکی) دست‌نخورده به‌عنوانِ
# «پنل پیش‌فرض» باقی می‌ماند؛ این‌ها فقط پنل‌هایِ *اضافه* هستند.

async def smm_panels_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    # === PATCH_SMM_PROVIDERS_LIST_COMPACT_V1 ===
    # قبلاً هر ارائه‌دهنده ۵-۶ تا دکمه (مرور/تست/فعال‌غیرفعال/حذف/استعلامِ
    # گروهی/تنظیمات/عملیات) پشتِ‌سرِ‌هم تو همین لیست داشت — طبقِ نظرِ ادمین
    # («خیلی شلوغه») جمع شد: هر ارائه‌دهنده فقط یک دکمه با اسمِ خودشه؛ زدنش
    # مستقیم می‌بره به همون صفحهٔ «عملیاتِ ارائه‌دهنده» (_panel_detail_content)
    # که از قبل همه‌ی این کارها رو داره (نه چیزی حذف شده، فقط یک لایه عمیق‌تر
    # رفته). «انتخاب برای مرور» هم از این لیست به همون صفحهٔ عملیات منتقل شد.
    legacy_url = await _get_setting(bot_id, "smm_api_url", "")
    legacy_key = await _get_setting(bot_id, "smm_api_key", "")
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(
            SmmPanel.bot_id == bot_id).order_by(SmmPanel.id))).all()
        # === PATCH_SMM_ONE_ENTRY_V1 ===
        brow = await s.get(BotRow, bot_id)
    can_add_more = has_feature(brow.plan if brow else "free", Feature.SMM_MULTI_PANEL)

    lines = ["🏢 <b>ارائه‌دهندگانِ من</b>\n", "روی هر ارائه‌دهنده بزن تا مشخصات و عملیاتش را ببینی:\n"]
    rows = []
    # پنلِ پیش‌فرض هم مثلِ بقیه یک ردیفِ خودش را دارد — چه وصل باشد چه نه،
    # چون تنها راهِ رسیدن به «اتصال به پنل» هم همین دکمه است.
    legacy_connected = bool(legacy_url and legacy_key)
    lines.append(f"{'✅' if legacy_connected else '❌'} <b>پنل پیش‌فرض</b> — {'متصل' if legacy_connected else 'متصل نشده'}")
    rows.append([InlineKeyboardButton(text="⚪️ پنل پیش‌فرض", callback_data="smm:menu")])
    for p in panels:
        conn_dot = "—" if p.last_check_ok is None else ("🟢" if p.last_check_ok else "🔴")
        active_mark = "" if p.is_active else " (غیرفعال)"
        lines.append(f"{conn_dot} <b>{p.name}</b>{active_mark}")
        rows.append([InlineKeyboardButton(text=f"{conn_dot} {p.name}{active_mark}",
                                          callback_data=f"smm:panel:detail:{p.id}")])
    # === PATCH_SMM_ONE_ENTRY_V1 ===
    if can_add_more:
        rows.append([InlineKeyboardButton(text="➕ افزودنِ پنلِ جدید", callback_data="smm:panel:add", style=ButtonStyle.SUCCESS)])
    elif not panels:
        # فقط وقتی هنوز هیچ پنلِ اضافه‌ای نساخته، هینت رو نشون بده — اگه
        # قبلاً (وقتی پلنِ بالاتر داشته) پنل اضافه کرده و حالا پلنش پایین
        # اومده، لازم نیست دوباره تبلیغِ ارتقا ببینه، پنل‌هاش دست‌نخورده می‌مونن.
        rows.append([InlineKeyboardButton(text="🔒 افزودنِ پنلِ بیشتر (فقط پلنِ حرفه‌ای)", callback_data="smm:panel:add")])
    # === PATCH_FRAGMENT_WALLET_V1 ===
    rows.append([InlineKeyboardButton(text="🔗 اتصال به فرگمنت (استارزِ خودکار)",
                                      callback_data="smm:fragment:menu")])
    # === PATCH_TGTOOLS_AUTO_SECTION_V1 === TGTools هم «اتصالِ خودکار» است، نه دستی
    rows.append([InlineKeyboardButton(text="🤖 اتصال به TGTools (استارزِ خودکار)",
                                      callback_data="ssl:tgt")])
    # === PATCH_ACC_GIFT_V1 ===
    rows.append([InlineKeyboardButton(text="👤 گیفتِ خودکار با اکانتِ پشتیبانی",
                                      callback_data="ssl:acc")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:panels")
async def smm_panels(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    # === PATCH_SMM_NAV_FIX_V1 ===
    await _set_browse_origin(row.id, "panels")
    text, kb = await smm_panels_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ═══════════════════ عملیاتِ ارائه‌دهنده (تک‌پنل) ═══════════════════
# === PATCH_SMM_PROVIDER_DETAIL_V1 ===
# صفحهٔ متمرکزِ یک ارائه‌دهنده، طبقِ references/smm-menu-redesign.md در
# اسکیلِ botsaz-dev: نام/لینک/کلید قابلِ ویرایش، وضعیتِ اتصال و موجودیِ
# کش‌شده (با دکمهٔ بروزرسانیِ دستی یا حلقهٔ پس‌زمینه تازه می‌شود)، سوییچِ
# روشن/خاموش (ستونِ is_active که از قبل بود)، سوییچِ بررسیِ خودکار (ستونِ
# جدیدِ auto_check_enabled — main_tenant.smm_panel_health_loop از رویِ همین
# ستون فیلتر می‌کند)، و لینک به تنظیماتِ قیمت/سود/سفارشِ موجود. فقط برایِ
# پنل‌هایِ اضافه (SmmPanel) است، نه «پنلِ پیش‌فرض» — آن پنل مفهومِ جداگانه‌ای
# دارد (Setting سطحِ ربات، نه ردیفِ این جدول) و جای دیگری (smm:menu) مدیریت
# می‌شود.

def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


async def _panel_refresh_status(bot_id: int, panel_id: int) -> tuple[bool, str]:
    """موجودی/وضعیتِ اتصالِ یک ارائه‌دهنده را زنده از API می‌گیرد و در
    ستون‌هایِ last_check_* کش می‌کند. خروجی: (موفق؟, پیامِ خلاصه)."""
    async with Session() as s:
        p = await s.get(SmmPanel, panel_id)
        if not p or p.bot_id != bot_id:
            return False, "ارائه‌دهنده پیدا نشد."
        res = await smm_api.get_balance(p.api_url, p.api_key)
        p.last_check_at = datetime.utcnow()
        if res is None:
            p.last_check_ok = False
            await s.commit()
            return False, "❌ اتصال برقرار نشد یا کلید/آدرس نامعتبر است."
        bal, cur = res
        p.last_check_ok = True
        p.last_balance = bal
        p.last_currency = cur
        await s.commit()
        return True, f"✅ متصل — موجودی: {bal:,.2f} {cur}".strip()


async def _panel_detail_content(bot_id: int, panel_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """صفحهٔ «عملیاتِ ارائه‌دهنده» — None اگر پیدا نشود (حذف‌شده/متعلق به
    رباتِ دیگر) تا فراخوان پیامِ مناسب بدهد.
    === PATCH_SMM_PROVIDERS_LIST_COMPACT_V1 ===
    این صفحه حالا تنها مقصدِ هر ردیفِ لیستِ ارائه‌دهندگانه، پس هر عملیاتی
    که قبلاً مستقیم تو لیست بود (مرور/افزودنِ سرویس، تستِ اتصال، استعلامِ
    گروهی) هم به همین‌جا منتقل شد — چیزی حذف نشده."""
    async with Session() as s:
        p = await s.get(SmmPanel, panel_id)
    if not p or p.bot_id != bot_id:
        return None
    margin = await _panel_margin_pct(bot_id, p.id)
    price_mode = await _panel_price_mode(bot_id, p.id)
    browse_pk = await _get_browsing_panel(bot_id)
    bulk_on = await _panel_bulk_enabled(bot_id, p.id)
    from tenant.bots.virtual_service import smm_price_sync as _PS   # PATCH_SMM_PRICE_AUTOSYNC_V1
    price_auto = await _PS.panel_auto_on(bot_id, p.id)
    balance_txt = "—" if p.last_balance is None else f"{float(p.last_balance):,.2f} {p.last_currency}".strip()
    masked_key = (p.api_key[:4] + "…" + p.api_key[-4:]) if p.api_key and len(p.api_key) > 10 else (p.api_key or "—")
    lines = [
        f"🏢 <b>مشخصاتِ ثبت‌شده — {p.name}</b>\n",
        f"🔗 لینک: <code>{p.api_url}</code>",
        f"🔑 کلید: <code>{masked_key}</code>",
        f"💰 موجودیِ کش‌شده: <b>{balance_txt}</b>"
        + (f" (آخرین بررسی: {_fmt_dt(p.last_check_at)})" if p.last_check_at else ""),
    ]
    rows = [
        # === PATCH_SMM_PROVIDER_DETAIL_SHIK_V1 ===
        # طبقِ خواستِ ادمین («شیک و حرفه‌ای مثلِ ربات مرجع»): هر مشخصه یک
        # ردیفِ دوستونیِ «برچسب | مقدار» شد، دقیقاً همون چیدمانی که تو
        # اسکرین‌شاتِ ربات مرجع بود. مقدارهایِ صرفاً نمایشی (وضعیتِ اتصال،
        # نوعِ ارز) با callback_data="noop" غیرِفعالن؛ مقدارهایی که خودشون
        # اکشن‌ان (روشن/خاموش، بررسیِ خودکار) هنوز کلیک‌پذیرن.
        [InlineKeyboardButton(text="✏️ ویرایشِ نامِ ارائه‌دهنده", callback_data=f"smm:panel:editname:{p.id}")],
        [InlineKeyboardButton(text="🔑 ویرایشِ کلید (Api Key)", callback_data=f"smm:panel:editkey:{p.id}"),
         InlineKeyboardButton(text="🔗 ویرایشِ لینک (URL)", callback_data=f"smm:panel:editurl:{p.id}")],
        [InlineKeyboardButton(text="📶 وضعیتِ اتصال", callback_data="noop"),
         InlineKeyboardButton(text=("🟢 متصل" if p.last_check_ok else ("🔴 قطع" if p.last_check_ok is False else "— نامشخص")),
                              callback_data="noop")],
        [InlineKeyboardButton(text="⚡️ وضعیتِ پنل", callback_data="noop"),
         InlineKeyboardButton(text=("🟢 روشن" if p.is_active else "🔴 خاموش"),
                              callback_data=f"smm:panel:toggle:{p.id}")],
        [InlineKeyboardButton(text="🔁 بررسیِ وضعیت‌گرِ خودکار", callback_data="noop"),
         InlineKeyboardButton(text=("✅ فعال" if p.auto_check_enabled else "❌ غیرفعال"),
                              callback_data=f"smm:panel:autochk:{p.id}")],
        # === PATCH_SMM_PRICE_AUTOSYNC_V1 === استعلامِ خودکارِ قیمت برایِ همین ارائه‌دهنده
        [InlineKeyboardButton(text="💲 استعلامِ خودکارِ قیمت (هر ۳۰ دقیقه)", callback_data="noop"),
         InlineKeyboardButton(text=("✅ فعال" if price_auto else "❌ غیرفعال"),
                              callback_data=f"smm:psync:p:{p.id}")],
        [InlineKeyboardButton(text="💲 بروزرسانیِ قیمتِ محصولاتِ این ارائه‌دهنده (همین الان)",
                              callback_data=f"smm:psync:now:{p.id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📈 درصدِ سودِ ارائه‌دهنده", callback_data="noop"),
         InlineKeyboardButton(text=(f"{margin:g}٪" if price_mode == "margin" else "تنظیم نشده"),
                              callback_data=f"smm:panel:settings:{p.id}")],
        [InlineKeyboardButton(text="💰 نوعِ ارز", callback_data="noop"),
         InlineKeyboardButton(text=(p.last_currency or "—"), callback_data="noop")],
        [InlineKeyboardButton(text="📡 استعلامِ گروهیِ سفارش‌ها", callback_data="noop"),
         InlineKeyboardButton(text=("✅ فعال" if bulk_on else "❌ غیرفعال"),
                              callback_data=f"smm:panel:bulk:{p.id}")],
        [InlineKeyboardButton(text="🔄 بروزرسانیِ موجودی", callback_data=f"smm:panel:refresh:{p.id}"),
         InlineKeyboardButton(text="🧪 تستِ سریع", callback_data=f"smm:panel:test:{p.id}")],
        [InlineKeyboardButton(
            text=("✅ در حالِ مرور از این پنل" if browse_pk == p.id else "🛒 مرور و افزودنِ سرویس از این پنل"),
            callback_data=f"smm:panel:pick:{p.id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ حذفِ ارائه‌دهنده", callback_data=f"smm:panel:del:{p.id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_panel_detail(target, bot_id: int, panel_id: int, prefix: str = ""):
    """رندرِ صفحهٔ عملیاتِ ارائه‌دهنده رویِ یک Message یا CallbackQuery.message.
    اگر ارائه‌دهنده پیدا نشود (مثلاً هم‌زمان حذف شده)، به لیست برمی‌گرداند."""
    content = await _panel_detail_content(bot_id, panel_id)
    if content is None:
        text, kb = await smm_panels_content(bot_id)
        text = "❌ این ارائه‌دهنده پیدا نشد (شاید حذف شده).\n\n" + text
    else:
        text, kb = content
        text = prefix + text
    try:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:panel:detail:"))
async def smm_panel_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    await _render_panel_detail(cb.message, row.id, pid)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:panel:refresh:"))
async def smm_panel_refresh(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    await cb.answer("در حال بررسی...")
    ok, message = await _panel_refresh_status(row.id, pid)
    await _render_panel_detail(cb.message, row.id, pid, prefix=f"{message}\n\n")


@smm_router.callback_query(F.data.startswith("smm:panel:autochk:"))
async def smm_panel_autochk_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        p.auto_check_enabled = not p.auto_check_enabled
        await s.commit()
    await cb.answer("✅ ذخیره شد")
    await _render_panel_detail(cb.message, row.id, pid)


@smm_router.callback_query(F.data.startswith("smm:panel:editname:"))
async def smm_panel_editname_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.panel_edit_name)
    await state.update_data(edit_panel_id=pid)
    await cb.message.answer(
        "✏️ اسمِ جدیدِ این ارائه‌دهنده را بفرست:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:panel:detail:{pid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.panel_edit_name, F.text)
async def smm_panel_editname_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("edit_panel_id")
    name = msg.text.strip()[:60]
    if not name or not pid:
        await msg.answer("❌ اسمِ خالی مجاز نیست.")
        return
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await state.clear()
            await msg.answer("❌ ارائه‌دهنده پیدا نشد.")
            return
        p.name = name
        await s.commit()
    await state.clear()
    await _render_panel_detail(msg, row.id, pid, prefix="✅ نام ذخیره شد.\n\n")


@smm_router.callback_query(F.data.startswith("smm:panel:editurl:"))
async def smm_panel_editurl_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.panel_edit_url)
    await state.update_data(edit_panel_id=pid)
    await cb.message.answer(
        "🔗 لینکِ (URL) جدیدِ این ارائه‌دهنده را بفرست:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:panel:detail:{pid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.panel_edit_url, F.text)
async def smm_panel_editurl_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("edit_panel_id")
    new_url = msg.text.strip()
    if not new_url or not pid:
        await msg.answer("❌ لینکِ خالی مجاز نیست.")
        return
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await state.clear()
            await msg.answer("❌ ارائه‌دهنده پیدا نشد.")
            return
        p.api_url = new_url
        await s.commit()
    await state.clear()
    await msg.answer("⏳ در حال بررسیِ اتصال با لینکِ جدید...")
    ok, message = await _panel_refresh_status(row.id, pid)
    await _render_panel_detail(msg, row.id, pid, prefix=f"✅ لینک ذخیره شد. {message}\n\n")


@smm_router.callback_query(F.data.startswith("smm:panel:editkey:"))
async def smm_panel_editkey_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.panel_edit_key)
    await state.update_data(edit_panel_id=pid)
    await cb.message.answer(
        "🔑 کلید (Api Key) جدیدِ این ارائه‌دهنده را بفرست:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:panel:detail:{pid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.panel_edit_key, F.text)
async def smm_panel_editkey_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("edit_panel_id")
    new_key = msg.text.strip()
    if not new_key or not pid:
        await msg.answer("❌ کلیدِ خالی مجاز نیست.")
        return
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await state.clear()
            await msg.answer("❌ ارائه‌دهنده پیدا نشد.")
            return
        p.api_key = new_key
        await s.commit()
    await state.clear()
    await msg.answer("⏳ در حال بررسیِ اتصال با کلیدِ جدید...")
    ok, message = await _panel_refresh_status(row.id, pid)
    await _render_panel_detail(msg, row.id, pid, prefix=f"✅ کلید ذخیره شد. {message}\n\n")


# === PATCH_SMM_PROVIDER_DETAIL_V1 ===
async def smm_panel_health_check_once() -> None:
    """یک دورِ بررسیِ خودکارِ همهٔ ارائه‌دهندگانی که «بررسیِ خودکار» رویشان
    روشن است — موجودی/وضعیتِ اتصال را زنده می‌گیرد و کش می‌کند. توسطِ
    main_tenant.smm_panel_health_loop (اجرا در worker_tenant.py) صدا زده
    می‌شود، هر چند دقیقه یک‌بار."""
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(
            SmmPanel.auto_check_enabled == True))).all()
        ids = [(p.id, p.bot_id) for p in panels]
    for pid, bid in ids:
        try:
            await _panel_refresh_status(bid, pid)
        except Exception:
            import traceback
            print(f"smm_panel_health_check_once error (panel {pid}):\n" + traceback.format_exc())


@smm_router.callback_query(F.data.startswith("smm:panel:pick:"))
async def smm_panel_pick(cb: CallbackQuery, bot: Bot):
    """کدام پنل را انتخاب و بلافاصله سرویس‌هایش را برای انتخاب/فروش نشان بده.

    === PATCH_SMM_MULTI_PANEL_V1 (بازطراحی) ===
    قبلاً فقط پنلِ فعال را عوض می‌کرد و همون صفحهٔ لیستِ پنل‌ها را دوباره
    نشون می‌داد — ادمین باید یه قدمِ اضافه می‌زد (برمی‌گشت به «پنل SMM» و
    «دریافت سرویس‌ها» رو می‌زد). طبقِ درخواست («بزنه رو پنل، مشخصات و
    قابلیت‌هاش بیاد و بتونه بذاره برای فروش») حالا یک تیک مستقیم می‌ره تو
    لیستِ سرویس‌هایِ همون پنل."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    raw = cb.data.rsplit(":", 1)[1]
    await _set_setting(row.id, "smm_browse_panel_pk", "" if raw == "0" else raw)
    # توجه: خودِ cb.answer() اینجا زده نمی‌شود — smm_services پایین‌تر
    # خودش cb.answer() می‌زند؛ زدنِ دوباره‌اش رو یک callback_query همون
    # (که تلگرام فقط یک‌بار answerCallbackQuery قبول می‌کند) خطا می‌دهد.
    # === PATCH_SMM_NAV_FIX_V1 ===
    # این‌جا از «پنل‌های API» اومدیم، نه از «پنل SMM» — پس دکمهٔ برگشتِ
    # صفحهٔ سرویس‌ها هم باید برگرده «پنل‌های API»، نه smm:menu.
    await smm_services(cb, bot, back_target="smm:panels")


@smm_router.callback_query(F.data.startswith("smm:panel:toggle:"))
async def smm_panel_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await cb.answer("پنل پیدا نشد", show_alert=True)
            return
        p.is_active = not p.is_active
        await s.commit()
    await cb.answer("✅ ذخیره شد")
    # === PATCH_SMM_PROVIDERS_LIST_COMPACT_V1 ===
    # این دکمه دیگه تو خودِ لیست نیست، فقط رویِ صفحهٔ عملیاتِ همون
    # ارائه‌دهندهٔ خاصه — پس باید همونجا برگردیم، نه لیست.
    await _render_panel_detail(cb.message, row.id, pid)


# === PATCH_SMM_BULK_STATUS_V1 ===
@smm_router.callback_query(F.data.startswith("smm:panel:bulk:"))
async def smm_panel_bulk_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    raw = cb.data.rsplit(":", 1)[1]
    panel_pk = None if raw == "0" else int(raw)
    cur = await _panel_bulk_enabled(row.id, panel_pk)
    await _panel_set_bulk_enabled(row.id, panel_pk, not cur)
    await cb.answer("✅ ذخیره شد")
    # === PATCH_SMM_PROVIDERS_LIST_COMPACT_V1 ===
    # این دکمه دیگه تو لیست نیست: برایِ پنلِ پیش‌فرض (panel_pk=None) تو
    # صفحهٔ smm:menu نشسته، برایِ پنل‌هایِ اضافه تو صفحهٔ عملیاتِ خودشون.
    if panel_pk is None:
        text, kb = await smm_admin_content(row.id)
    else:
        content = await _panel_detail_content(row.id, panel_pk)
        if content is None:
            text, kb = await smm_panels_content(row.id)
        else:
            text, kb = content
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_DEL_HANDLER_DECORATOR_FIX_V1 ===
# این تابع دکوریتورش رو نداشت — یعنی «🗑 حذف» و «❌ حذفِ ارائه‌دهنده» تو
# aiogram اصلاً به این هندلر route نمی‌شدن (دکمه کاملاً بی‌واکنش بود، نه
# خطا می‌داد نه چیزی). چون این فقط با گشتنِ دستیِ کد پیدا شد، نه با
# audit.py (که فقط سطحِ include_router رو چک می‌کنه، نه هر تک‌هندلر رو).
@smm_router.callback_query(F.data.startswith("smm:panel:del:"))
async def smm_panel_del(cb: CallbackQuery, bot: Bot):
    """حذفِ یک پنل. سرویس‌هایی که از این پنل اضافه شده بودن حذف نمی‌شوند
    (تا سفارش‌های تاریخی/کاربرانی که قبلاً خریدن بی‌معنا نشن)، فقط دیگر
    از این پنل استعلامِ وضعیت نمی‌شوند — بهتره ادمین قبل از حذفِ پنل،
    سرویس‌هایِ متصل بهش رو دستی غیرفعال کنه."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        p = await s.get(SmmPanel, pid)
        if not p or p.bot_id != row.id:
            await cb.answer("پنل پیدا نشد", show_alert=True)
            return
        await s.delete(p)
        await s.commit()
    if await _get_browsing_panel(row.id) == pid:
        await _set_setting(row.id, "smm_browse_panel_pk", "")
    # === PATCH_SMM_PROVIDER_DETAIL_V1 (راهنمای حذف) ===
    # طبقِ تصمیمِ ادمین: حذفِ ارائه‌دهنده چیزی رو پاک نمی‌کنه، پس به‌جایِ
    # توستِ کوتاهِ معمولی (که خودش رفع‌می‌شه)، یه alert می‌ذاریم که ادمین
    # واقعاً بخونه چه اتفاقی افتاد.
    await cb.answer(
        "🗑 ارائه‌دهنده حذف شد.\nمحصولاتی که ازش اضافه کرده بودی پاک نشدن؛ "
        "فقط اتصالِ زنده‌شون به این پنل قطع شد و از این به بعد مثلِ یک "
        "سرویسِ دستی رفتار می‌کنن.", show_alert=True)
    text, kb = await smm_panels_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:panel:test:"))
async def smm_panel_test(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    raw = cb.data.rsplit(":", 1)[1]
    pid = None if raw == "0" else int(raw)
    api_url, api_key, name = await _panel_creds(row.id, pid)
    await cb.answer("در حال بررسی...")
    ok, message = await smm_api.validate(api_url, api_key)
    await cb.message.answer(f"🧪 <b>تستِ پنل «{name}»</b>\n\n{message}", parse_mode="HTML")


@smm_router.callback_query(F.data == "smm:panel:add")
async def smm_panel_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_SMM_ONE_ENTRY_V1 ===
    # الان که صفحه‌ی «پنل‌های API» برایِ همه‌ی پلن‌ها بازه (چون گیفت/
    # پرمیوم/استارز/TON هم اونجاست)، خودِ «افزودنِ پنلِ دومِ به‌بعد» باید
    # گیت بشه، نه کلِ صفحه. پنلِ پیش‌فرضِ تکی برایِ همه‌ی پلن‌ها همیشه آزاده.
    if not has_feature(row.plan, Feature.SMM_MULTI_PANEL):
        await cb.answer()
        await cb.message.answer(
            "🔒 اتصالِ بیش از یک پنلِ SMM فقط تو پلنِ حرفه‌ای فعاله.\n\n"
            "پنلِ پیش‌فرض همچنان برایِ همه‌ی پلن‌ها آزاده — برایِ افزودنِ "
            "پنل‌هایِ اضافه، پلنِ ربات رو ارتقا بده.")
        return
    await state.set_state(SmmFSM.panel_name)
    await cb.message.answer(
        "➕ <b>افزودنِ پنلِ جدید</b>\n\n"
        "۱/۳ — یک اسمِ دلخواه برایِ این پنل بفرست (فقط برایِ خودت، مثلاً «پنل ارزان» یا «پنل ویژه‌ی ممبر»):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="smm:panels", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.panel_name, F.text)
async def smm_panel_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    name = msg.text.strip()[:60]
    if not name:
        await msg.answer("❌ اسمِ خالی مجاز نیست.")
        return
    await state.update_data(panel_name=name)
    await state.set_state(SmmFSM.panel_url)
    await msg.answer("۲/۳ — آدرسِ API این پنل را بفرست:")


@smm_router.message(SmmFSM.panel_url, F.text)
async def smm_panel_url_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    await state.update_data(panel_url=msg.text.strip())
    await state.set_state(SmmFSM.panel_key)
    await msg.answer("۳/۳ — حالا API Key این پنل را بفرست:")


@smm_router.message(SmmFSM.panel_key, F.text)
async def smm_panel_key_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    name = data.get("panel_name", "پنل")
    api_url = data.get("panel_url", "")
    api_key = msg.text.strip()
    await msg.answer("⏳ در حال بررسی اتصال...")
    ok, message = await smm_api.validate(api_url, api_key)
    if not ok:
        await state.clear()
        await msg.answer(
            f"{message}\n\nدوباره از «➕ افزودنِ پنلِ جدید» شروع کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels")]]))
        return
    async with Session() as s:
        s.add(SmmPanel(bot_id=row.id, name=name, api_url=api_url, api_key=api_key, is_active=True))
        await s.commit()
    await state.clear()
    text, kb = await smm_panels_content(row.id)
    await msg.answer(f"{message}\n\n✅ پنل «{name}» اضافه شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ── موجودی ──

@smm_router.callback_query(F.data == "smm:balance")
async def smm_balance(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await cb.answer("در حال دریافت...")
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    panels = await _active_panels(row.id)
    if not panels:
        kb0 = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:menu")]])
        try:
            await cb.message.edit_text("❌ هیچ پنلی وصل نیست.", reply_markup=kb0)
        except Exception:
            await cb.message.answer("❌ هیچ پنلی وصل نیست.", reply_markup=kb0)
        return
    lines = ["💰 <b>موجودی حساب پنل‌ها</b>\n"]
    for panel_pk, name in panels:
        api_url, api_key, _ = await _panel_creds(row.id, panel_pk)
        res = await smm_api.get_balance(api_url, api_key)
        if res is None:
            lines.append(f"⚪️ {name}: ❌ دریافت ناموفق بود")
        else:
            bal, cur = res
            lines.append(f"🟢 {name}: <b>{bal:,.2f} {cur}</b>".strip())
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:menu")]])
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ── دریافت و انتخاب سرویس‌ها (صفحه‌بندی) ──

_PAGE = 10

# کش موقتِ سرویس‌های پنل برای هر ربات تا از دریافت مکررِ کند از API جلوگیری شود.
# ساختار: { bot_id: (timestamp, [services]) } — اعتبار ۱۰ دقیقه.
# کش موقتِ سرویس‌های پنل برای هر (ربات، پنل) تا از دریافت مکررِ کند از API
# جلوگیری شود. ساختار: { (bot_id, panel_pk): (timestamp, [services]) } — اعتبار ۱۰ دقیقه.
_SVC_CACHE: dict[tuple[int, int | None], tuple[float, list]] = {}
_SVC_TTL = 600


async def _fetch_services_cached(bot_id: int, panel_pk: int | None = None, force: bool = False) -> list:
    """=== PATCH_SMM_SVC_CACHE_CROSS_PROCESS_V1 ===
    گزارشِ ادمین: «خیلی داره ارتباط می‌گیره با سرویسِ ارائه‌دهنده». علتش
    همون کلاسِ باگی بود که تویِ کشِ نرخِ ارز پیدا شد: _SVC_CACHE فقط یه
    دیکشنریِ تویِ حافظه‌ست — اگه بیش از یک نمونه/پروسه از رباتِ مستأجر
    اجرا بشه (یا پروسه ری‌استارت بشه)، هر کدوم کشِ خودشو جدا می‌سازه و
    دوباره از پنل می‌گیره. الان یه لایهٔ کشِ دومِ دیتابیسی (Settingِ
    خودِ همین بات، نه سراسری، چون لیستِ سرویس‌ها مالِ همین باته) اضافه شد
    که بینِ همه‌ی پروسه‌ها مشترکه."""
    import time as _t
    now = _t.time()
    cache_key = (bot_id, panel_pk)
    if not force and cache_key in _SVC_CACHE:
        ts, data = _SVC_CACHE[cache_key]
        if now - ts < _SVC_TTL:
            return data
    # === PATCH_SMM_SVC_CACHE_CROSS_PROCESS_V1 ===
    db_key = f"svc_cache_{panel_pk if panel_pk is not None else 0}"
    if not force:
        try:
            raw = await _get_setting(bot_id, db_key, "")
            if raw:
                payload = json.loads(raw)
                if (now - payload["ts"]) < _SVC_TTL:
                    _SVC_CACHE[cache_key] = (now, payload["data"])  # کشِ محلی هم گرم شه
                    return payload["data"]
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (خواندنِ کشِ دیتابیسیِ سرویس‌هایِ پنل)")
    api_url, api_key, _name = await _panel_creds(bot_id, panel_pk)
    data = await smm_api.get_services(api_url, api_key)
    if data:
        _SVC_CACHE[cache_key] = (now, data)
        try:
            await _set_setting(bot_id, db_key, json.dumps({"ts": now, "data": data}, ensure_ascii=False))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (نوشتنِ کشِ دیتابیسیِ سرویس‌هایِ پنل)")
    return data


@smm_router.callback_query(F.data.startswith("smm:services:"))
async def smm_services(cb: CallbackQuery, bot: Bot, back_target: str | None = None):
    """مرحلهٔ ۱ (ادمین): نمایش پلتفرم‌های موجود در پنل + دکمهٔ جستجو.

    === PATCH_SMM_NAV_FIX_V1 ===
    back_target=None (پیشِ‌فرض، همونی که خودِ تلگرام موقعِ زدنِ دکمه صدا
    می‌زنه) یعنی «از رویِ همون هابی که آخرین‌بار ازش اومدی» تصمیم بگیر
    (_get_browse_origin) — چون این تابع هم مستقیم از smm:menu می‌آد،
    هم از پنل‌ها، هم به‌عنوانِ «بازگشتِ» عمیق‌ترِ زیرمنوهایِ پایین‌تر."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if back_target is None:
        origin = await _get_browse_origin(row.id)
        back_target = "smm:panels" if origin == "panels" else "smm:menu"
    force = cb.data.endswith(":refresh")
    # === PATCH_SMM_SERVICES_PAGING_V1 ===
    # تلگرام حداکثر ۱۰۰ دکمه در یک پیام می‌پذیرد؛ پنل‌هایِ پرمحصول در حالتِ
    # «دسته‌بندیِ ارائه‌دهنده» صدها دسته دارند => REPLY_MARKUP_TOO_LONG.
    import re as _re
    _m = _re.search(r":p(\d+)$", cb.data or "")
    if _m:
        _page = int(_m.group(1))
        _SVC_PAGE[row.id] = _page
    elif (cb.data or "").startswith("smm:services:"):
        _page = 0
        _SVC_PAGE[row.id] = 0
    else:  # فراخوانیِ داخلی (تیک‌زدن/حالتِ انتخاب) => همان صفحه بماند
        _page = _SVC_PAGE.get(row.id, 0)
    try:
        await cb.answer("در حال دریافت...")
    except Exception:
        pass
    try:
        await _smm_services_body(cb, row, back_target, force, _page)
    except Exception as e:
        # === PATCH_SMM_SERVICES_NEVER_STUCK_V1 ===
        # هر خطایی (تلگرام/دیتابیس/پنل) قبلاً پیامِ «⏳ صبر کن» را برایِ همیشه
        # باقی می‌گذاشت. حالا همیشه یک پیامِ خطایِ واضح + دکمهٔ تلاشِ دوباره.
        logger.exception("smm_services failed (bot_id=%s)", row.id)
        _kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تلاش دوباره", callback_data="smm:services:refresh")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target or "smm:menu")]])
        _txt = ("❌ نمایشِ لیستِ سرویس‌ها با خطا مواجه شد.\n\n"
                f"<code>{_html_escape(str(e)[:200])}</code>")
        try:
            await cb.message.edit_text(_txt, reply_markup=_kb, parse_mode="HTML")
        except Exception:
            try:
                await cb.message.answer(_txt, reply_markup=_kb, parse_mode="HTML")
            except Exception:
                logger.exception("smm_services: حتی پیامِ خطا هم ارسال نشد")


_SVC_PAGE: dict[int, int] = {}
_SVC_BTN_PER_PAGE = 40


async def _smm_services_body(cb: CallbackQuery, row, back_target: str, force: bool, page: int):
    panel_pk = await _get_browsing_panel(row.id)
    # === PATCH_SMM_LOADING_FEEDBACK_V1 ===
    # اگه کشِ سرویس‌ها منقضی شده باشه (یا force=True با «به‌روزرسانی
    # لیست»)، این خط یه تماسِ شبکه‌ایِ واقعی به پنله و می‌تونه چند ثانیه
    # طول بکشه. توستِ بالا خیلی زود ناپدید می‌شه؛ این ادیت یه وضعیتِ
    # واضح‌تر و پایدارتر نشون می‌ده تا ادمین حسِ «هنگ‌کردن» نگیره.
    try:
        await cb.message.edit_text("⏳ در حالِ دریافتِ سرویس‌ها از پنل... چند لحظه صبر کن.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    services = await _fetch_services_cached(row.id, panel_pk, force=force)
    if not services:
        # === PATCH_SMM_API_REDIRECT_RETRY_V1 === علتِ دقیقِ خطایِ پنل را هم نشان بده
        try:
            _au, _ak, _ = await _panel_creds(row.id, panel_pk)
            _why = smm_api.last_error(_au)
        except Exception:
            _why = ""
        await cb.message.answer("❌ سرویسی دریافت نشد. اتصال پنل را بررسی کن."
                                + (f"\n\nعلتِ دقیق: <code>{_html_escape(_why[:300])}</code>" if _why else ""),
                                parse_mode="HTML")
        return
    tree = await _group_for_admin(row.id, services)
    _, _, panel_name = await _panel_creds(row.id, panel_pk)
    async with Session() as s:
        added = set((await s.scalars(select(SmmService.service_id).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk))).all())
    rows = [[InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="smm:search")]]
    # === PATCH_SMM_PLATFORM_BULK_ADD_V1 ===
    # درخواستِ ادمین: تک‌تک واردِ هر پلتفرم شدن و بعد «افزودنِ همه» را زدن
    # خیلی طول می‌کشید. حالا می‌شود چند پلتفرم را همین‌جا تیک زد و همه را
    # یکجا اضافه کرد؛ برایِ هر پلتفرم یک دستهٔ هم‌نام ساخته/استفاده می‌شود.
    _picked = await _bulk_picked(row.id)
    _selmode = (await _get_setting(row.id, "smm_bulk_selmode", "0")) == "1"
    # پلتفرم‌ها به‌ترتیب تعداد
    _all_keys = sorted(tree.keys(), key=lambda k: -sum(len(x) for x in tree[k].values()))
    _npages = max(1, -(-len(_all_keys) // _SVC_BTN_PER_PAGE))
    page = max(0, min(page, _npages - 1))
    _page_keys = _all_keys[page * _SVC_BTN_PER_PAGE:(page + 1) * _SVC_BTN_PER_PAGE]
    for pkey in _page_keys:
        types = tree[pkey]
        cnt = sum(len(x) for x in types.values())
        act = sum(1 for t in types.values() for svc in t if svc["service"] in added)
        badge = f" — {act}✅" if act else ""
        # === PATCH_SMM_BULK_SELMODE_V1 ===
        # دکمهٔ جداگانهٔ تیک، نصفِ عرضِ ردیف را می‌گرفت و متن/عددِ پلتفرم
        # بریده می‌شد (تلگرام عرضِ دکمه‌هایِ یک ردیف را مساوی می‌کند).
        # حالا هر پلتفرم یک دکمه تمام‌عرض است و «حالتِ انتخاب» تعیین
        # می‌کند که زدنِ آن یعنی ورود یا تیک‌زدن.
        if _selmode:
            mark = "☑️ " if pkey in _picked else "⬜️ "
            _cbd = f"smm:bulktog:{pkey}"
        else:
            mark = "☑️ " if pkey in _picked else ""
            _cbd = f"smm:plat:{pkey}:0"
        rows.append([InlineKeyboardButton(
            text=f"{mark}{smm_api.platform_label(pkey)} ({cnt}){badge}",
            callback_data=_cbd)])

    if _npages > 1:
        _nav = []
        if page > 0:
            _nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"smm:services:p{page-1}"))
        _nav.append(InlineKeyboardButton(text=f"{page+1}/{_npages}", callback_data="noop"))
        if page < _npages - 1:
            _nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"smm:services:p{page+1}"))
        rows.append(_nav)
    # حالت انتخاب پلتفرم فقط در صفحهٔ فهرست پلتفرم‌ها باقی می‌ماند.
    # حالت انتخاب سرویس از داخل خود پلتفرم، مثل اینستاگرام، کنترل می‌شود.
    rows.append([InlineKeyboardButton(
        text=("✔️ حالتِ انتخاب پلتفرم: روشن" if _selmode else "☑️ حالتِ انتخاب پلتفرم: خاموش"),
        callback_data="smm:bulkmode",
        style=(ButtonStyle.SUCCESS if _selmode else ButtonStyle.PRIMARY))])

    if _picked:
        _nsel = sum(sum(len(x) for x in tree.get(pk, {}).values())
                    for pk in _picked)
        rows.append([InlineKeyboardButton(
            text=f"➕ افزودنِ پلتفرم‌هایِ تیک‌خورده ({_nsel} سرویس)",
            callback_data="smm:bulkaddask", style=ButtonStyle.SUCCESS)])
        # === PATCH_SMM_BULK_REMOVE_V1 ===
        # تا امروز فقط «افزودن» بود؛ اگر ادمین پلتفرمِ اشتباهی اضافه می‌کرد،
        # باید تک‌تکِ ده‌ها سرویس را دستی غیرفعال می‌کرد. این دکمه دقیقاً
        # عکسِ همان عملیات است تا برگرداندنش هم یک‌کلیکی باشد.
        rows.append([InlineKeyboardButton(
            text="➖ حذفِ سرویس‌هایِ پلتفرم‌هایِ تیک‌خورده",
            callback_data="smm:bulkdelask", style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(
            text="🗑 برداشتنِ تیک‌ها", callback_data="smm:bulkclear")])
    rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانی لیست", callback_data="smm:services:refresh")])
    # === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
    _by_cat = await _browse_by_provider_category(row.id)
    rows.append([InlineKeyboardButton(
        text=("🏷 نمایش: دسته‌بندیِ ارائه‌دهنده" if _by_cat else "🤖 نمایش: پلتفرمِ خودکار"),
        callback_data="smm:togbrowsemode")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target)])
    txt = (f"📥 <b>سرویس‌های پنل «{panel_name}»</b> (کل: {len(services)})\n\n"
           + ("دسته‌بندی: همان چیزی که ارائه‌دهنده فرستاده.\n"
              if _by_cat else
              "اول پلتفرم را انتخاب کن، بعد نوع سرویس (ممبر/ویو/لایک/...).\n")
           + "✅ = فعال برای فروش")
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                   parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                parse_mode="HTML")


# === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
@smm_router.callback_query(F.data == "smm:togbrowsemode")
async def smm_toggle_browse_mode(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ نمایشِ «➕ افزودنِ محصول» بینِ پلتفرمِ خودکار (حدسِ ما
    از رویِ اسمِ سرویس) و دسته‌بندیِ خامِ ارائه‌دهنده. فقط ظاهرِ فهرست را
    عوض می‌کند؛ محصولاتی که قبلاً اضافه شده‌اند و دسته‌بندیِ دستی‌شان را
    دست‌نمی‌زند."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _browse_by_provider_category(row.id)
    await _set_setting(row.id, "smm_browse_provider_cat", "0" if cur else "1")
    await cb.answer(
        "نمایش رفت رویِ دسته‌بندیِ ارائه‌دهنده."
        if not cur else "نمایش برگشت به پلتفرمِ خودکار.")
    await smm_services(cb, bot)


# ═════════ PATCH_SMM_PLATFORM_BULK_ADD_V1 ═════════
_BULK_KEY = "smm_bulk_platforms"


async def _bulk_picked(bot_id: int) -> set:
    """پلتفرم‌هایِ تیک‌خورده در صفحهٔ سرویس‌ها."""
    try:
        raw = await _get_setting(bot_id, _BULK_KEY, "[]")
        return set(json.loads(raw or "[]"))
    except Exception:
        return set()


async def _bulk_set(bot_id: int, vals) -> None:
    await _set_setting(bot_id, _BULK_KEY,
                       json.dumps(sorted(vals), ensure_ascii=False))


async def _category_for_platform(bot_id: int, pkey: str) -> int | None:
    """دستهٔ هم‌نامِ پلتفرم را برمی‌گرداند؛ اگر نبود می‌سازد."""
    label = smm_api.platform_label(pkey)
    async with Session() as s:
        # === PATCH_SMM_MULTI_CATEGORY_PER_PLATFORM_V1 === چند دسته برایِ یک پلتفرم مجاز است؛
        # «دستهٔ هم‌نام» همیشه قدیمی‌ترین است (نه یک ردیفِ دلخواه) تا نتیجه قابلِ پیش‌بینی باشد.
        cat = await s.scalar(select(SmmCategory).where(
            SmmCategory.bot_id == bot_id, SmmCategory.platform == pkey)
            .order_by(SmmCategory.id).limit(1))
        if cat:
            return cat.id
        cat = SmmCategory(bot_id=bot_id, platform=pkey,
                          name=label[:60], is_active=True)
        s.add(cat)
        await s.commit()
        return cat.id


@smm_router.callback_query(F.data == "smm:bulkmode")
async def smm_bulk_mode(cb: CallbackQuery, bot: Bot):
    """روشن/خاموشِ حالتِ انتخاب. وقتی روشن است، زدنِ هر پلتفرم آن را تیک
    می‌زند به‌جای واردشدن؛ وقتی خاموش است، رفتارِ عادی برمی‌گردد."""
    row = await _owner(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "smm_bulk_selmode", "0")) == "1"
    await _set_setting(row.id, "smm_bulk_selmode", "0" if cur else "1")
    if cur:
        await _bulk_set(row.id, set())
    await smm_services(cb, bot)


@smm_router.callback_query(F.data.startswith("smm:bulktog:"))
async def smm_bulk_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    pkey = cb.data.split(":", 2)[2]
    cur = await _bulk_picked(row.id)
    cur.discard(pkey) if pkey in cur else cur.add(pkey)
    await _bulk_set(row.id, cur)
    await smm_services(cb, bot)


@smm_router.callback_query(F.data == "smm:bulkclear")
async def smm_bulk_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await _bulk_set(row.id, set())
    await smm_services(cb, bot)


# === PATCH_SMM_BULK_REMOVE_V1 ===
@smm_router.callback_query(F.data == "smm:bulkdelask")
async def smm_bulk_del_ask(cb: CallbackQuery, bot: Bot):
    """تأییدِ حذفِ گروهی. چون عملیات برگشت‌ناپذیر است و ممکن است ده‌ها
    سرویس را بردارد، اول تعدادِ دقیق را نشان می‌دهیم."""
    row = await _owner(cb, bot)
    if not row:
        return
    picked = await _bulk_picked(row.id)
    if not picked:
        return await cb.answer("هنوز پلتفرمی تیک نزدی.", show_alert=True)

    panel_pk = await _get_browsing_panel(row.id)
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == row.id,
            SmmService.panel_pk == panel_pk,
            SmmService.platform_key.in_(list(picked))))
    if not n:
        return await cb.answer("از این پلتفرم‌ها سرویسی اضافه نشده.", show_alert=True)

    labels = "، ".join(smm_api.platform_label(p) for p in list(picked)[:6])
    if len(picked) > 6:
        labels += f" و {len(picked) - 6} تایِ دیگر"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 بله، هر {n} سرویس حذف شود",
                              callback_data="smm:bulkdel", style=ButtonStyle.DANGER)],
        # === PATCH_SMM_CANCEL_DEAD_BUTTON_V1 ===
        # مقصدِ «smm:services» هیچ هندلری ندارد — هندلرِ واقعی
        # `startswith("smm:services:")` است و شمارهٔ صفحه می‌خواهد. پس این
        # دکمه کاملاً بی‌اثر بود و ادمین در صفحهٔ انتخابِ دسته گیر می‌کرد.
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data="smm:services:0")],
    ])
    await cb.message.edit_text(
        f"⚠️ <b>حذفِ گروهیِ سرویس‌ها</b>\n\n"
        f"پلتفرم‌ها: {labels}\n"
        f"تعدادِ سرویسی که حذف می‌شود: <b>{n}</b>\n\n"
        "<i>سفارش‌هایِ ثبت‌شدهٔ قبلی دست‌نخورده می‌مانند؛ فقط این سرویس‌ها از "
        "فهرستِ فروش برداشته می‌شوند و هر وقت خواستی می‌توانی دوباره "
        "اضافه‌شان کنی.</i>",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "smm:bulkdel")
async def smm_bulk_del(cb: CallbackQuery, bot: Bot):
    """عکسِ دقیقِ smm_bulk_add: سرویس‌هایِ پلتفرم‌هایِ تیک‌خورده را برمی‌دارد."""
    row = await _owner(cb, bot)
    if not row:
        return
    picked = await _bulk_picked(row.id)
    if not picked:
        return await cb.answer("هنوز پلتفرمی تیک نزدی.", show_alert=True)

    try:
        await cb.message.edit_text("⏳ در حالِ حذف… چند لحظه صبر کن.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (پیامِ در حالِ حذف)")

    panel_pk = await _get_browsing_panel(row.id)
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id,
            SmmService.panel_pk == panel_pk,
            SmmService.platform_key.in_(list(picked))))).all()
        removed = len(svcs)
        for v in svcs:
            await s.delete(v)
        await s.commit()

    await _bulk_set(row.id, set())
    await cb.answer(f"{removed} سرویس حذف شد.", show_alert=True)
    await smm_services(cb, bot)


# === PATCH_SMM_BULKADD_PICK_CATEGORY_V1 ===
# درخواستِ ادمین: «موقعِ افزودنِ پلتفرم‌هایِ تیک‌خورده ازمون بپرسه به کدوم
# دسته اضافه کنه».
#
# رفتارِ قبلی: هر سرویس خودکار در دستهٔ هم‌نامِ پلتفرمِ خودش می‌رفت
# (_category_for_platform) و ادمین هیچ انتخابی نداشت. اگر می‌خواست همه را
# در یک دستهٔ دلخواه بگذارد، باید بعدش تک‌تک جابه‌جا می‌کرد.
#
# حالا یک مرحلهٔ انتخاب اضافه شده. گزینهٔ پیش‌فرض (دستهٔ هم‌نامِ پلتفرم)
# سرِ جایش است تا رفتارِ قبلی با یک کلیک در دسترس بماند.
@smm_router.callback_query(F.data == "smm:bulkaddask")
async def smm_bulk_add_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    picked = await _bulk_picked(row.id)
    if not picked:
        return await cb.answer("هنوز پلتفرمی تیک نزدی.", show_alert=True)

    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id).order_by(SmmCategory.name))).all()
        cat_rows = [(c.id, c.name) for c in cats]

    labels = "، ".join(smm_api.platform_label(p) for p in list(picked)[:5])
    if len(picked) > 5:
        labels += f" و {len(picked) - 5} تایِ دیگر"

    rows = [[InlineKeyboardButton(
        text="🏷 دستهٔ هم‌نامِ هر پلتفرم (پیش‌فرض)",
        callback_data="smm:bulkadd:auto", style=ButtonStyle.SUCCESS)]]
    for cid, cname in cat_rows[:20]:
        rows.append([InlineKeyboardButton(text=f"📂 {cname[:40]}",
                                          callback_data=f"smm:bulkadd:{cid}")])
    # PATCH_SMM_CANCEL_DEAD_BUTTON_V1 — همان باگ، در صفحهٔ انتخابِ دستهٔ افزودن
    rows.append([InlineKeyboardButton(text="⬅️ انصراف", callback_data="smm:services:0",
                                      style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"📂 <b>سرویس‌ها در کدام دسته قرار بگیرند؟</b>\n\n"
        f"پلتفرم‌هایِ انتخابی: {labels}\n\n"
        "• «دستهٔ هم‌نامِ هر پلتفرم» یعنی هر سرویس در دستهٔ خودش "
        "(اینستاگرام در «اینستاگرام»، تلگرام در «تلگرام»…) — اگر دسته "
        "نباشد ساخته می‌شود.\n"
        "• یا یکی از دسته‌هایِ زیر را بزن تا <b>همه</b> در همان قرار بگیرند."
        + ("" if cat_rows else "\n\n<i>هنوز دستهٔ دستی نساخته‌ای.</i>"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:bulkadd:"))
async def smm_bulk_add_to_cat(cb: CallbackQuery, bot: Bot):
    """افزودن با دستهٔ انتخاب‌شده — «auto» یعنی همان رفتارِ قبلی."""
    arg = cb.data.split(":")[2]
    await _do_bulk_add(cb, bot, forced_cat=(None if arg == "auto" else int(arg)))


@smm_router.callback_query(F.data == "smm:bulkadd")
async def smm_bulk_add_legacy(cb: CallbackQuery, bot: Bot):
    """سازگاری با کیبوردهایِ قدیمی که هنوز در چتِ ادمین‌ها هستند."""
    await _do_bulk_add(cb, bot, forced_cat=None)


# === PATCH_SMM_ADD_DUP_FIX_V1 ===
# گزارشِ کاربر (OWNERZONIX / ربات زونیکس): «افزودنِ محصولات با خطا مواجه شد و متوقف شد:
# duplicate key ... uq_smm_services_bot_service, Key (bot_id, service_id)=(817, 223)».
# علت: محدودیتِ یکتاییِ دیتابیس رویِ (bot_id, service_id) است — یعنی یک شناسهٔ سرویس در یک ربات فقط
# یک‌بار، *بدونِ توجه به پنل* — ولی همهٔ مسیرهایِ افزودن فقط «هم‌پنل» بودنِ تکراری را چک می‌کردند.
# پس اگر ربات چند پنل داشت و پنلِ دوم هم سرویسی با همان شناسه (مثلاً ۲۲۳) داشت، INSERT می‌ترکید و کلِ
# افزودن (حتی سرویس‌هایِ سالم) متوقف می‌شد. حالا چکِ تکراری روی کلِ ربات است؛ سرویسی که شناسه‌اش قبلاً
# از پنلِ دیگری اضافه شده رد می‌شود و ادمین با تعدادش باخبر می‌شود.
async def _existing_service_ids(s, bot_id: int, panel_pk, only=None) -> tuple[set, set]:
    """(همهٔ شناسه‌هایِ ربات، فقط شناسه‌هایِ همین پنل) — هر دو به‌صورت str."""
    q = select(SmmService.service_id, SmmService.panel_pk).where(SmmService.bot_id == bot_id)
    if only:
        q = q.where(SmmService.service_id.in_([str(x) for x in only]))
    rows = (await s.execute(q)).all()
    same_panel = {str(r[0]) for r in rows if r[1] == panel_pk}
    # === PATCH_SMM_SAME_ID_OTHER_PANEL_V1 === شناسهٔ سرویس فقط «در همان پنل» یکتاست؛ دو ارائه‌دهندهٔ مختلف
    # کاملاً عادی است که شناسهٔ یکسان (مثلاً ۲۲۳) داشته باشند. (قبلاً رد می‌شد و به ادمین می‌گفت «قبلاً از
    # پنلِ دیگری اضافه شده»؛ قیدِ یکتاییِ دیتابیس هم در init_db به (bot_id, panel_pk, service_id) تغییر کرد.)
    return same_panel, same_panel


async def _do_bulk_add(cb: CallbackQuery, bot: Bot, forced_cat: int | None = None):
    """همهٔ سرویس‌هایِ پلتفرم‌هایِ تیک‌خورده را یکجا فعالِ فروش می‌کند.

    forced_cat=None → هر سرویس در دستهٔ هم‌نامِ پلتفرمِ خودش (رفتارِ قبلی)
    forced_cat=<id> → همه در همان دستهٔ انتخاب‌شده
    """
    row = await _owner(cb, bot)
    if not row:
        return
    picked = await _bulk_picked(row.id)
    if not picked:
        return await cb.answer("هنوز پلتفرمی تیک نزدی.", show_alert=True)

    try:
        await cb.message.edit_text("⏳ در حالِ افزودن… چند لحظه صبر کن.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # === PATCH_SMM_BULKADD_STUCK_LOADING_FIX_V1 ===
    # گزارشِ ادمین (OWNERZONIX): بعدِ همین پیامِ «⏳ در حالِ افزودن…» ربات
    # برایِ همیشه همان‌جا می‌ماند — هیچ خطا، هیچ نتیجه. علت: هرچیزی که در
    # ادامهٔ این تابع (دریافتِ سرویس‌ها، ساختِ ردیف‌هایِ SmmService، commit)
    # exception بدهد، بدونِ هیچ try/except به بیرون درز می‌کرد — یعنی خودِ
    # پیامِ لودینگ هیچ‌وقت جایگزین نمی‌شد و ادمین هیچ اطلاعی از خطا نداشت
    # (فقط یک Traceback در لاگِ سرور که خودِ ادمین نمی‌بیند). حالا کلِ کار
    # را داخلِ یک try می‌گذاریم: اگر جایی خطا داد، پیامِ لودینگ را با یک
    # پیامِ خطایِ روشن جایگزین می‌کنیم و کاملِ Traceback را لاگ می‌کنیم.
    try:
        panel_pk = await _get_browsing_panel(row.id)
        services = await _fetch_services_cached(row.id, panel_pk)
        tree = await _group_for_admin(row.id, services or [])

        total_new = 0
        skipped_other_panel = 0  # PATCH_SMM_ADD_DUP_FIX_V1
        async with Session() as s:
            existing, _same_panel = await _existing_service_ids(s, row.id, panel_pk)

            for pkey in picked:
                # PATCH_SMM_BULKADD_PICK_CATEGORY_V1
                cat_pk = (forced_cat if forced_cat is not None
                          else await _category_for_platform(row.id, pkey))
                for tkey, items in (tree.get(pkey) or {}).items():
                    for svc in items:
                        sid = svc["service"]
                        if str(sid) in existing:
                            if str(sid) not in _same_panel:
                                skipped_other_panel += 1
                            continue
                        s.add(SmmService(
                            bot_id=row.id, service_id=sid,
                            name=smm_api.persianize_name(svc["name"]),
                            category=svc.get("category", ""),
                            panel_rate=svc["rate"], sell_rate=0,
                            min_qty=svc["min"], max_qty=svc["max"],
                            is_active=True, panel_pk=panel_pk, category_pk=cat_pk,
                            platform_key=pkey, type_key=tkey,
                            # === PATCH_SMM_PROVIDER_DESC_V1 ===
                            provider_desc=(svc.get("desc") or "")))
                        existing.add(str(sid))
                        total_new += 1
            if total_new:
                await s.commit()

        if not services:
            # دریافتِ لیستِ سرویس‌ها از پنل ناموفق بود (تایم‌اوت/قطعی) —
            # این را جدا از «چیزِ تازه‌ای نبود» بگو، وگرنه ادمین فکر می‌کند
            # همه‌چیز از قبل اضافه بوده در حالی که اصلاً چیزی دریافت نشده.
            await _bulk_set(row.id, set())
            await _set_setting(row.id, "smm_bulk_selmode", "0")
            await cb.answer(
                "❌ اتصال به پنل ناموفق بود (تایم‌اوت یا قطعی) — چیزی دریافت نشد. "
                "چند لحظه دیگر دوباره امتحان کن.", show_alert=True)
            await smm_services(cb, bot)
            return

        await _bulk_set(row.id, set())
        await _set_setting(row.id, "smm_bulk_selmode", "0")
        _skip_note = (f"\n\nℹ️ {skipped_other_panel} سرویس هم رد شد چون شناسه‌شان قبلاً از "
                      "پنلِ دیگری اضافه شده بود." if skipped_other_panel else "")
        await cb.answer(
            (f"✅ {total_new} سرویسِ جدید اضافه شد و در دسته‌هایِ هم‌نامِ "
             "پلتفرم قرار گرفت." if total_new else
             "چیزِ تازه‌ای نبود — همه‌شون از قبل اضافه بودن.") + _skip_note, show_alert=True)
        await smm_services(cb, bot)
    except Exception as e:
        logger.exception("افزودنِ انبوهِ محصولات ناموفق بود (bot_id=%s)", row.id)
        try:
            await cb.message.edit_text(
                "❌ افزودنِ محصولات با خطا مواجه شد و متوقف شد.\n\n"
                f"<code>{_html_escape(str(e)[:300])}</code>\n\n"
                "این خطا در لاگِ سرور هم ثبت شد. دوباره امتحان کن؛ اگر تکرار شد، "
                "به پشتیبانی بگو.",
                parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (نمایشِ خطایِ افزودنِ انبوه)")


async def smm_plat_view(row, cb: CallbackQuery, bot: Bot, pkey: str):
    """بازنمایی صفحهٔ پلتفرم برای callback تغییر حالت انتخاب سرویس."""
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    tree = await _group_for_admin(row.id, services)
    types = tree.get(pkey, {})
    if not types:
        await cb.answer("موردی نیست", show_alert=True)
        return
    async with Session() as s:
        added = set((await s.scalars(select(SmmService.service_id).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk))).all())
    rows = []
    type_select_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    type_picked = await _smm_type_picked(row.id, pkey) if type_select_mode else set()
    for tkey in sorted(types.keys(), key=lambda k: -len(types[k])):
        items = types[tkey]
        act = sum(1 for svc in items if svc["service"] in added)
        badge = f" — {act}✅" if act else ""
        if type_select_mode:
            mark = "☑️" if tkey in type_picked else "⬜️"
            callback = f"smm:typetog:{pkey}:{tkey}"
        else:
            mark = ""
            callback = f"smm:typ:{pkey}:{tkey}:0"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {smm_api.service_type_label(tkey)} ({len(items)}){badge}",
            callback_data=callback)])
    svc_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    if svc_mode:
        rows.append([InlineKeyboardButton(
            text=f"➕ افزودن نوع‌های انتخاب‌شده ({len(type_picked)})",
            callback_data=f"smm:typeaddask:{pkey}", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(
            text=f"➖ حذف نوع‌های انتخاب‌شده ({len(type_picked)})",
            callback_data=f"smm:typedelask:{pkey}", style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(
            text="🗑 برداشتن تیک نوع‌ها", callback_data=f"smm:typeclear:{pkey}")])
    rows.append([InlineKeyboardButton(
        text=("✅ حالتِ انتخاب سرویس: روشن" if svc_mode else "☑️ حالتِ انتخاب سرویس: خاموش"),
        callback_data=f"smm:servmode:plat:{pkey}",
        style=(ButtonStyle.SUCCESS if svc_mode else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:services:0")])
    text = f"{smm_api.platform_label(pkey)}\n\nنوع سرویس را انتخاب کن:"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:plat:"))
async def smm_plat(cb: CallbackQuery, bot: Bot):
    """مرحلهٔ ۲ (ادمین): نمایش انواع سرویسِ یک پلتفرم."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey = parts[2]
    await cb.answer()
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    tree = await _group_for_admin(row.id, services)
    types = tree.get(pkey, {})
    if not types:
        await cb.answer("موردی نیست", show_alert=True)
        return
    async with Session() as s:
        added = set((await s.scalars(select(SmmService.service_id).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk))).all())
    rows = []
    type_select_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    type_picked = await _smm_type_picked(row.id, pkey) if type_select_mode else set()
    for tkey in sorted(types.keys(), key=lambda k: -len(types[k])):
        items = types[tkey]
        act = sum(1 for svc in items if svc["service"] in added)
        badge = f" — {act}✅" if act else ""
        if type_select_mode:
            mark = "☑️" if tkey in type_picked else "⬜️"
            callback = f"smm:typetog:{pkey}:{tkey}"
        else:
            mark = ""
            callback = f"smm:typ:{pkey}:{tkey}:0"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {smm_api.service_type_label(tkey)} ({len(items)}){badge}",
            callback_data=callback)])
    svc_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    if svc_mode:
        rows.append([InlineKeyboardButton(
            text=f"➕ افزودن نوع‌های انتخاب‌شده ({len(type_picked)})",
            callback_data=f"smm:typeaddask:{pkey}", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(
            text=f"➖ حذف نوع‌های انتخاب‌شده ({len(type_picked)})",
            callback_data=f"smm:typedelask:{pkey}", style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(
            text="🗑 برداشتن تیک نوع‌ها", callback_data=f"smm:typeclear:{pkey}")])
    rows.append([InlineKeyboardButton(
        text=("✅ حالتِ انتخاب سرویس: روشن" if svc_mode else "☑️ حالتِ انتخاب سرویس: خاموش"),
        callback_data=f"smm:servmode:plat:{pkey}",
        style=(ButtonStyle.SUCCESS if svc_mode else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:services:0")])
    try:
        await cb.message.edit_text(
            f"{smm_api.platform_label(pkey)}\n\nنوع سرویس را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(
            f"{smm_api.platform_label(pkey)}\n\nنوع سرویس را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:typ:"))
async def smm_typ(cb: CallbackQuery, bot: Bot, _override: tuple[str, str, int] | None = None):
    """مرحلهٔ ۳ (ادمین): لیست سرویس‌های پلتفرم+نوع با صفحه‌بندی و افزودن/حذف.

    === PATCH_SMM_FROZEN_CB_DATA_V1 ===
    _override: به‌جایِ خواندن از cb.data. لازم شد چون فراخوان‌هایِ داخلی
    (مثلِ smm_confirm_add) قبلاً `cb.data = ...` می‌کردن تا این تابع رو با
    پارامترهایِ دلخواه صدا بزنن — ولی CallbackQuery تویِ pydantic v2 فریزه
    و این انتساب ValidationError می‌داد (تویِ error.log ده‌ها بار تکرار
    شده). حالا مقدار مستقیم پاس داده می‌شه، بدونِ دست‌زدن به آبجکتِ فریز."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if _override is not None:
        pkey, tkey, page = _override
    else:
        parts = cb.data.split(":")
        pkey, tkey, page = parts[2], parts[3], int(parts[4])
    await cb.answer()
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    tree = await _group_for_admin(row.id, services)
    items = tree.get(pkey, {}).get(tkey, [])
    if not items:
        await cb.answer("موردی نیست", show_alert=True)
        return
    async with Session() as s:
        added = set((await s.scalars(select(SmmService.service_id).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk))).all())
    total = len(items)
    start = page * _PAGE
    chunk = items[start:start + _PAGE]
    mode_key = f"smm_cat_selmode_{pkey}_{tkey}"
    pick_key = f"smm_cat_selected_{pkey}_{tkey}"
    local_select_mode = (await _get_setting(row.id, mode_key, "0")) == "1"
    global_select_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    # حالت روشن‌شده در صفحهٔ ارائه‌دهنده/پلتفرم باید در صفحهٔ نوع سرویس
    # هم اعمال شود؛ حالت محلی همچنان برای کنترل همان نوع حفظ می‌شود.
    select_mode = local_select_mode or global_select_mode
    try:
        picked = set(json.loads(await _get_setting(row.id, pick_key, "[]")) or [])
    except Exception:
        picked = set()
    rows = []
    for svc in chunk:
        sid = str(svc["service"])
        if select_mode:
            mark = "☑️" if sid in picked else "⬜️"
            callback = f"smm:catpick:{pkey}:{tkey}:{page}:{sid}"
        else:
            mark = "✅" if sid in added else "➕"
            callback = f"smm:add:{sid}:{pkey}:{tkey}:{page}"
        # === PATCH_SMM_NAME_PERSIANIZE_V1 ===
        disp_name = smm_api.persianize_name(svc["name"])
        rows.append([InlineKeyboardButton(
            text=f"{mark} [{sid}] {disp_name[:38]}", callback_data=callback)])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"smm:typ:{pkey}:{tkey}:{page-1}"))
    if start + _PAGE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"smm:typ:{pkey}:{tkey}:{page+1}"))
    if nav:
        rows.append(nav)
    if select_mode:
        rows.append([InlineKeyboardButton(
            text=f"➕ افزودنِ انتخاب‌شده‌ها ({len(picked)})",
            callback_data=f"smm:cataddask:{pkey}:{tkey}:{page}", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(
            text=f"➖ حذفِ انتخاب‌شده‌ها ({len(picked)})",
            callback_data=f"smm:catdelask:{pkey}:{tkey}:{page}", style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(text="🗑 پاک‌کردن انتخاب‌ها", callback_data=f"smm:catclear:{pkey}:{tkey}:{page}")])
    else:
        # === PATCH_SMM_ADD_ALL_V1 ===
        # درخواستِ ادمین با ارجاع به ربات مرجع: «اضافه کردن کلیه سرویس‌ها».
        rows.append([InlineKeyboardButton(text="➕ افزودنِ همه‌یِ این سرویس‌ها",
                                          callback_data=f"smm:addall:{pkey}:{tkey}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(
        text=("✅ حالتِ انتخاب: روشن" if select_mode else "☑️ حالتِ انتخاب: خاموش"),
        callback_data=f"smm:catmode:{pkey}:{tkey}:{page}",
        style=(ButtonStyle.SUCCESS if select_mode else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:plat:{pkey}:0")])
    txt = (f"{smm_api.platform_label(pkey)} — {smm_api.service_type_label(tkey)}\n"
           f"صفحهٔ {page+1} از {((total-1)//_PAGE)+1}\n\n"
           + (f"حالت انتخاب روشن است؛ {len(picked)} سرویس انتخاب شده.\n"
              "با تیک‌زدن سرویس‌ها، سپس افزودن یا حذف دسته‌ای را اجرا کن."
              if select_mode else
              "روی هر سرویس بزن تا جزئیات افزودن یا حذف آن باز شود؛ برای چندتایی، حالت انتخاب را روشن کن."))
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                   parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:addall:"))
async def smm_add_all(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_ADD_ALL_V1 (بازنویسی: PATCH_SMM_ADD_ALL_PICK_CATEGORY_V1) ===
    قبلاً بلافاصله همه‌یِ سرویس‌هایِ این دسته رو بدونِ پرسیدنِ دسته‌بندیِ
    ادمین اضافه می‌کرد. حالا — دقیقاً هم‌راستا با مسیرِ افزودنِ تکی —
    اول می‌پرسه این‌ها تویِ کدوم دستهٔ ادمین (مثلاً «اینستاگرام») اضافه
    بشن؛ یک گزینهٔ «بدونِ دسته / افزودن به محصولات» هم هست که رفتارِ
    قبلی (بدونِ دسته‌بندی) رو حفظ می‌کنه."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey = parts[2], parts[3]
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    tree = await _group_for_admin(row.id, services)
    items = tree.get(pkey, {}).get(tkey, [])
    if not items:
        await cb.answer("موردی نیست", show_alert=True)
        return
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    if not cats:
        # هیچ دسته‌ای نساخته — سؤال‌کردن بی‌معنیه، مستقیم برو مرحلهٔ افزودن
        await _smm_finalize_add_all(cb, bot, row.id, pkey, tkey, cat_pk=None)
        return
    await cb.answer()
    rows = [[InlineKeyboardButton(text="🚫 بدونِ دسته (افزودن به محصولات)",
                                  callback_data=f"smm:addallcat:{pkey}:{tkey}:0")]]
    for c in cats:
        plat = await platform_label_for_bot(row.id, c.platform)
        rows.append([InlineKeyboardButton(text=f"{plat} {c.name}",
                                          callback_data=f"smm:addallcat:{pkey}:{tkey}:{c.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:typ:{pkey}:{tkey}:0")])
    txt = (f"📦 همه‌یِ سرویس‌هایِ «{smm_api.platform_label(pkey)} — "
           f"{smm_api.service_type_label(tkey)}» رو تویِ کدوم دسته اضافه کنم؟")
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@smm_router.callback_query(F.data.startswith("smm:addallcat:"))
async def smm_add_all_with_category(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_ADD_ALL_PICK_CATEGORY_V1 ===
    ادامهٔ smm_add_all پس از انتخابِ دستهٔ ادمین (یا «بدونِ دسته»)."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, raw_cat = parts[2], parts[3], parts[4]
    cat_pk = None if raw_cat == "0" else int(raw_cat)
    await _smm_finalize_add_all(cb, bot, row.id, pkey, tkey, cat_pk)


async def _smm_finalize_add_all(cb: CallbackQuery, bot: Bot, bot_id: int, pkey: str, tkey: str,
                                 cat_pk: int | None) -> None:
    """=== PATCH_SMM_ADD_ALL_PICK_CATEGORY_V1 ===
    افزودنِ نهاییِ همه‌یِ سرویس‌هایِ این دسته (نه فقط صفحه‌یِ فعلی) با
    تنظیماتِ پیش‌فرض (قیمتِ دستی=۰) و دستهٔ ادمینِ انتخاب‌شده؛ هرکدوم که
    از قبل اضافه بوده رد می‌شه (idempotent)."""
    panel_pk = await _get_browsing_panel(bot_id)
    services = await _fetch_services_cached(bot_id, panel_pk)
    tree = await _group_for_admin(bot_id, services)
    items = tree.get(pkey, {}).get(tkey, [])
    if not items:
        await cb.answer("موردی نیست", show_alert=True)
        return
    async with Session() as s:
        existing_ids, _same_panel = await _existing_service_ids(s, bot_id, panel_pk)  # PATCH_SMM_ADD_DUP_FIX_V1
        added_count = 0
        for svc in items:
            sid = svc["service"]
            if str(sid) in existing_ids:
                continue
            s.add(SmmService(
                bot_id=bot_id, service_id=sid, name=smm_api.persianize_name(svc["name"]),
                category=svc.get("category", ""), panel_rate=svc["rate"],
                sell_rate=0, min_qty=svc["min"], max_qty=svc["max"],
                is_active=True, panel_pk=panel_pk, category_pk=cat_pk,
                # === PATCH_SMM_PROVIDER_DESC_V1 ===
                provider_desc=(svc.get("desc") or ""),
                # === PATCH_SMM_STORED_CLASSIFICATION_V1 ===
                platform_key=pkey, type_key=tkey))
            existing_ids.add(str(sid))
            added_count += 1
        if added_count:
            await s.commit()
    await cb.answer(
        f"✅ {added_count} سرویسِ جدید اضافه شد." if added_count else
        "چیزِ تازه‌ای نبود — همه‌شون از قبل اضافه بودن.", show_alert=True)
    await smm_typ(cb, bot, _override=(pkey, tkey, 0))


@smm_router.callback_query(F.data == "smm:search")
async def smm_search_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmFSM.search_query)
    await cb.message.answer(
        "🔎 عبارت جستجو را بفرست (مثلاً نام سرویس، پلتفرم یا شناسه):")
    await cb.answer()


@smm_router.message(SmmFSM.search_query, F.text)
async def smm_search_run(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.clear()
    q = msg.text.strip().lower()
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    if not services:
        await msg.answer("❌ سرویسی دریافت نشد.")
        return
    matched = [s for s in services
               if q in s["name"].lower() or q in str(s["service"]).lower()
               or q in (s.get("category", "") or "").lower()]
    if not matched:
        await msg.answer("موردی برای این جستجو یافت نشد.")
        return
    # نتایج را بر اساس نوع سرویس دسته‌بندی کن
    tree = await _group_for_admin(row.id, matched)
    async with Session() as s:
        added = set((await s.scalars(select(SmmService.service_id).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk))).all())
    rows = []
    for pkey, types in tree.items():
        for tkey, items in types.items():
            act = sum(1 for svc in items if svc["service"] in added)
            badge = f" — {act}✅" if act else ""
            rows.append([InlineKeyboardButton(
                text=f"{smm_api.platform_label(pkey)} · {smm_api.service_type_label(tkey)} ({len(items)}){badge}",
                callback_data=f"smm:typ:{pkey}:{tkey}:0")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:services:0")])
    await msg.answer(
        f"🔎 نتایج «{msg.text.strip()}» ({len(matched)} سرویس)\n\n"
        "دسته را انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


# ═════════ PATCH_SMM_TYPE_SELECTION_V1 ═════════
async def _smm_type_picked(row_id: int, pkey: str) -> set[str]:
    try:
        values = json.loads(await _get_setting(row_id, f"smm_type_selected_{pkey}", "[]")) or []
        return {str(x) for x in values}
    except Exception:
        return set()


async def _smm_type_reset(row_id: int, pkey: str) -> None:
    await _set_setting(row_id, f"smm_type_selected_{pkey}", "[]")


@smm_router.callback_query(F.data.startswith("smm:typetog:"))
async def smm_type_selection_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey = parts[2], parts[3]
    picked = await _smm_type_picked(row.id, pkey)
    if tkey in picked:
        picked.remove(tkey)
    else:
        picked.add(tkey)
    await _set_setting(row.id, f"smm_type_selected_{pkey}", json.dumps(sorted(picked), ensure_ascii=False))
    await cb.answer("انتخاب شد." if tkey in picked else "از انتخاب خارج شد.")
    await smm_plat_view(row, cb, bot, pkey)


@smm_router.callback_query(F.data.startswith("smm:typeclear:"))
async def smm_type_selection_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":")[2]
    await _smm_type_reset(row.id, pkey)
    await cb.answer("تیک نوع سرویس‌ها پاک شد.")
    await smm_plat_view(row, cb, bot, pkey)


@smm_router.callback_query(F.data.startswith("smm:typeaddask:"))
async def smm_type_selection_add_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":")[2]
    picked = await _smm_type_picked(row.id, pkey)
    if not picked:
        await cb.answer("ابتدا حداقل یک نوع سرویس را تیک بزن.", show_alert=True)
        return
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    rows = [[InlineKeyboardButton(
        text="🚫 بدون دسته (افزودن به محصولات)", callback_data=f"smm:typeadd:{pkey}:0")]]
    for c in cats:
        plat = await platform_label_for_bot(row.id, c.platform)
        rows.append([InlineKeyboardButton(
            text=f"{plat} {c.name}", callback_data=f"smm:typeadd:{pkey}:{c.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"smm:typeaddcancel:{pkey}")])
    labels = "، ".join(smm_api.service_type_label(t) for t in sorted(picked))
    await cb.answer()
    await cb.message.edit_text(
        f"📂 نوع‌های انتخاب‌شده: {labels}\n\nسرویس‌های این نوع‌ها در کدام دسته قرار بگیرند؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@smm_router.callback_query(F.data.startswith("smm:typeadd:"))
async def smm_type_selection_add(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, raw_cat = parts[2], parts[3]
    cat_pk = None if raw_cat == "0" else int(raw_cat)
    picked = await _smm_type_picked(row.id, pkey)
    if not picked:
        await cb.answer("انتخابی برای افزودن نیست.", show_alert=True)
        return
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    tree = await _group_for_admin(row.id, services or [])
    existing = set()
    added_count = 0
    async with Session() as s:
        existing, _same_panel = await _existing_service_ids(s, row.id, panel_pk)  # PATCH_SMM_ADD_DUP_FIX_V1
        for tkey in picked:
            for svc in (tree.get(pkey, {}).get(tkey, []) or []):
                sid = svc["service"]
                if str(sid) in existing:
                    continue
                s.add(SmmService(
                    bot_id=row.id, service_id=sid,
                    name=smm_api.persianize_name(svc["name"]), category=svc.get("category", ""),
                    panel_rate=svc["rate"], sell_rate=0, min_qty=svc["min"], max_qty=svc["max"],
                    is_active=True, panel_pk=panel_pk, category_pk=cat_pk,
                    platform_key=pkey, type_key=tkey,
                    provider_desc=(svc.get("desc") or "")))
                existing.add(str(sid))
                added_count += 1
        if added_count:
            await s.commit()
    await _smm_type_reset(row.id, pkey)
    await _set_setting(row.id, "smm_category_service_mode", "0")
    await cb.answer(f"✅ {added_count} سرویس اضافه شد.", show_alert=True)
    await smm_plat_view(row, cb, bot, pkey)


@smm_router.callback_query(F.data.startswith("smm:typedelask:"))
async def smm_type_selection_delete_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":")[2]
    picked = await _smm_type_picked(row.id, pkey)
    if not picked:
        await cb.answer("ابتدا نوع سرویس را تیک بزن.", show_alert=True)
        return
    panel_pk = await _get_browsing_panel(row.id)
    async with Session() as s:
        count = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk,
            SmmService.platform_key == pkey, SmmService.type_key.in_(list(picked))))
    if not count:
        await cb.answer("از این نوع‌ها سرویس فعالی در فروش نیست.", show_alert=True)
        return
    labels = "، ".join(smm_api.service_type_label(t) for t in sorted(picked))
    await cb.answer()
    await cb.message.edit_text(
        f"⚠️ حذف {count} سرویس از نوع‌های «{labels}» را تأیید می‌کنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ بله، {count} سرویس حذف شود",
                                  callback_data=f"smm:typedel:{pkey}", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"smm:typeaddcancel:{pkey}")],
        ]))


@smm_router.callback_query(F.data.startswith("smm:typeaddcancel:"))
async def smm_type_selection_cancel(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":")[2]
    await cb.answer("لغو شد.")
    await smm_plat_view(row, cb, bot, pkey)


@smm_router.callback_query(F.data.startswith("smm:typedel:"))
async def smm_type_selection_delete(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":")[2]
    picked = await _smm_type_picked(row.id, pkey)
    if not picked:
        await cb.answer("انتخابی برای حذف نیست.", show_alert=True)
        return
    panel_pk = await _get_browsing_panel(row.id)
    async with Session() as s:
        rows = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk,
            SmmService.platform_key == pkey, SmmService.type_key.in_(list(picked))))).all()
        removed = len(rows)
        for item in rows:
            await s.delete(item)
        if removed:
            await s.commit()
    await _smm_type_reset(row.id, pkey)
    await _set_setting(row.id, "smm_category_service_mode", "0")
    await cb.answer(f"➖ {removed} سرویس حذف شد.", show_alert=True)
    await smm_plat_view(row, cb, bot, pkey)


# ═════════ PATCH_SMM_GLOBAL_SERVICE_MODE_V1 ═════════
@smm_router.callback_query(F.data.startswith("smm:servmode:"))
async def smm_global_service_mode(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    target = parts[2] if len(parts) > 2 else "root"
    pkey = parts[3] if target == "plat" and len(parts) > 3 else None
    key = "smm_category_service_mode"
    enabled = (await _get_setting(row.id, key, "0")) == "1"
    new_value = "0" if enabled else "1"
    await _set_setting(row.id, key, new_value)
    if target == "plat" and pkey and new_value == "0":
        await _smm_type_reset(row.id, pkey)
    await cb.answer("✅ حالت انتخاب سرویس روشن شد." if new_value == "1" else "حالت انتخاب سرویس خاموش شد.")
    if target == "plat" and pkey:
        # callback ساختگی لازم نیست؛ همان صفحهٔ پلتفرم با دادهٔ موجود بازسازی می‌شود.
        await smm_plat_view(row, cb, bot, pkey)
    else:
        await smm_services(cb, bot)


# ═════════ PATCH_SMM_CATEGORY_SELECTION_V1 ═════════
async def _smm_category_selection_state(row_id: int, pkey: str, tkey: str):
    mode_key = f"smm_cat_selmode_{pkey}_{tkey}"
    pick_key = f"smm_cat_selected_{pkey}_{tkey}"
    mode = (await _get_setting(row_id, mode_key, "0")) == "1"
    try:
        picked = set(str(x) for x in (json.loads(await _get_setting(row_id, pick_key, "[]")) or []))
    except Exception:
        picked = set()
    return mode_key, pick_key, mode, picked


@smm_router.callback_query(F.data.startswith("smm:catmode:"))
async def smm_category_selection_mode(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page = parts[2], parts[3], int(parts[4])
    mode_key, pick_key, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    global_mode = (await _get_setting(row.id, "smm_category_service_mode", "0")) == "1"
    # اگر حالت سراسری روشن باشد، دکمهٔ همان نوع آن را خاموش می‌کند؛
    # در غیر این صورت state محلی همان نوع تغییر می‌کند.
    if global_mode:
        await _set_setting(row.id, "smm_category_service_mode", "0")
        new_mode = False
    else:
        new_mode = not mode
        await _set_setting(row.id, mode_key, "1" if new_mode else "0")
    if not new_mode:
        await _set_setting(row.id, pick_key, "[]")
    await cb.answer("✅ حالت انتخاب روشن شد." if new_mode else "حالت انتخاب خاموش شد.")
    await smm_typ(cb, bot, _override=(pkey, tkey, page))


@smm_router.callback_query(F.data.startswith("smm:catpick:"))
async def smm_category_selection_pick(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page, sid = parts[2], parts[3], int(parts[4]), str(parts[5])
    mode_key, pick_key, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    if not mode:
        await cb.answer("ابتدا حالت انتخاب را روشن کن.", show_alert=True)
        return
    if sid in picked:
        picked.discard(sid)
    else:
        picked.add(sid)
    await _set_setting(row.id, pick_key, json.dumps(sorted(picked), ensure_ascii=False))
    await cb.answer("انتخاب شد." if sid in picked else "از انتخاب خارج شد.")
    await smm_typ(cb, bot, _override=(pkey, tkey, page))


@smm_router.callback_query(F.data.startswith("smm:catclear:"))
async def smm_category_selection_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page = parts[2], parts[3], int(parts[4])
    _, pick_key, _, _ = await _smm_category_selection_state(row.id, pkey, tkey)
    await _set_setting(row.id, pick_key, "[]")
    await cb.answer("انتخاب‌ها پاک شد.")
    await smm_typ(cb, bot, _override=(pkey, tkey, page))


@smm_router.callback_query(F.data.startswith("smm:cataddask:"))
async def smm_category_selection_add_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page = parts[2], parts[3], int(parts[4])
    _, _, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    if not mode or not picked:
        await cb.answer("حالت انتخاب را روشن کن و حداقل یک سرویس را تیک بزن.", show_alert=True)
        return
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    rows = [[InlineKeyboardButton(
        text="🚫 بدون دسته (افزودن به محصولات)",
        callback_data=f"smm:catadd:{pkey}:{tkey}:{page}:0")]]
    for c in cats:
        plat = await platform_label_for_bot(row.id, c.platform)
        rows.append([InlineKeyboardButton(
            text=f"{plat} {c.name}", callback_data=f"smm:catadd:{pkey}:{tkey}:{page}:{c.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:typ:{pkey}:{tkey}:{page}")])
    await cb.answer()
    await cb.message.edit_text(
        f"📦 {len(picked)} سرویس انتخاب شده است. در کدام دسته اضافه شود؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@smm_router.callback_query(F.data.startswith("smm:catadd:"))
async def smm_category_selection_add(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page, raw_cat = parts[2], parts[3], int(parts[4]), parts[5]
    cat_pk = None if raw_cat == "0" else int(raw_cat)
    _, pick_key, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    if not mode or not picked:
        await cb.answer("انتخابی برای افزودن باقی نمانده است.", show_alert=True)
        return
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    wanted = {str(x) for x in picked}
    items = [svc for svc in services if str(svc.get("service")) in wanted]
    async with Session() as s:
        existing, _same_panel = await _existing_service_ids(s, row.id, panel_pk, only=wanted)  # PATCH_SMM_ADD_DUP_FIX_V1
        added_count = 0
        for svc in items:
            sid = svc["service"]
            if str(sid) in existing:
                continue
            s.add(SmmService(
                bot_id=row.id, service_id=sid, name=smm_api.persianize_name(svc["name"]),
                category=svc.get("category", ""), panel_rate=svc["rate"], sell_rate=0,
                min_qty=svc["min"], max_qty=svc["max"], is_active=True,
                panel_pk=panel_pk, category_pk=cat_pk,
                provider_desc=(svc.get("desc") or ""), platform_key=pkey, type_key=tkey))
            existing.add(str(sid))
            added_count += 1
        if added_count:
            await s.commit()
    await _set_setting(row.id, pick_key, "[]")
    await _set_setting(row.id, f"smm_cat_selmode_{pkey}_{tkey}", "0")
    await cb.answer(f"✅ {added_count} سرویس اضافه شد.", show_alert=True)
    await smm_typ(cb, bot, _override=(pkey, tkey, page))


@smm_router.callback_query(F.data.startswith("smm:catdelask:"))
async def smm_category_selection_delete_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page = parts[2], parts[3], int(parts[4])
    _, _, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    if not mode or not picked:
        await cb.answer("حالت انتخاب را روشن کن و سرویس‌ها را تیک بزن.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"⚠️ حذف {len(picked)} سرویس از فروش را تأیید می‌کنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"smm:catdel:{pkey}:{tkey}:{page}", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ لغو", callback_data=f"smm:typ:{pkey}:{tkey}:{page}")],
        ]))


@smm_router.callback_query(F.data.startswith("smm:catdel:"))
async def smm_category_selection_delete(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey, page = parts[2], parts[3], int(parts[4])
    _, pick_key, mode, picked = await _smm_category_selection_state(row.id, pkey, tkey)
    if not picked:
        await cb.answer("انتخابی برای حذف نیست.", show_alert=True)
        return
    panel_pk = await _get_browsing_panel(row.id)
    async with Session() as s:
        rows = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.panel_pk == panel_pk,
            SmmService.service_id.in_(list(picked))))).all()
        removed = len(rows)
        for item in rows:
            await s.delete(item)
        if removed:
            await s.commit()
    await _set_setting(row.id, pick_key, "[]")
    await _set_setting(row.id, f"smm_cat_selmode_{pkey}_{tkey}", "0")
    await cb.answer(f"➖ {removed} سرویس حذف شد.", show_alert=True)
    await smm_typ(cb, bot, _override=(pkey, tkey, page))


@smm_router.callback_query(F.data.startswith("smm:add:"))
async def smm_add_service(cb: CallbackQuery, bot: Bot):
    """با کلیک روی یک سرویس: اگر قبلاً فعال شده باشد آن را حذف می‌کند؛
    اگر جدید باشد، به‌جای افزودنِ فوری، صفحهٔ جزئیات را نشان می‌دهد تا ادمین
    قبل از تأیید، مشخصات کامل سرویس را ببیند."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    sid = parts[2]
    back = None
    if len(parts) >= 6:
        back = (parts[3], parts[4], parts[5])  # pkey, tkey, page
    panel_pk = await _get_browsing_panel(row.id)
    services = await _fetch_services_cached(row.id, panel_pk)
    svc = next((x for x in services if x["service"] == sid), None)
    if not svc:
        await cb.answer("سرویس یافت نشد", show_alert=True)
        return
    async with Session() as s:
        existing = await s.scalar(select(SmmService).where(
            SmmService.bot_id == row.id, SmmService.service_id == sid,
            SmmService.panel_pk == panel_pk))

    if existing:
        # قبلاً فعال بوده → حذف (toggle)
        async with Session() as s:
            ex = await s.scalar(select(SmmService).where(
                SmmService.bot_id == row.id, SmmService.service_id == sid,
                SmmService.panel_pk == panel_pk))
            if ex:
                await s.delete(ex)
                await s.commit()
        await cb.answer("از فروش حذف شد ➖")
        if back:
            # === PATCH_SMM_FROZEN_CB_DATA_V1 ===
            await smm_typ(cb, bot, _override=(back[0], back[1], int(back[2])))
        return

    # جدید → نمایش صفحهٔ جزئیات قبل از افزودن
    await cb.answer()
    pkey = back[0] if back else smm_api.detect_platform(svc["name"], svc.get("category", ""))
    tkey = back[1] if back else smm_api.detect_service_type(svc["name"], svc.get("category", ""))
    page = back[2] if back else "0"

    # قیمت فروش نمونه بر اساس حالت قیمت‌گذاریِ همین پنل
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    price_mode = await _panel_price_mode(row.id, panel_pk)
    price_hint = ""
    if price_mode == "margin":
        margin = await _panel_margin_pct(row.id, panel_pk)
        toman_unit = await _get_toman_unit_rate(row.id, panel_pk)
        if toman_unit <= 0:
            toman_unit = 1.0
        sell_1000 = float(svc["rate"]) * toman_unit * (1 + margin / 100.0)
        price_hint = (f"💵 قیمت فروش (خودکار با {int(margin)}٪ سود): "
                      f"<b>{int(round(sell_1000)):,} تومان / هزار</b>")
    else:
        price_hint = "💵 قیمت فروش: <b>پس از افزودن، به‌صورت دستی تعیین کن</b>"

    dripfeed = "✅ دارد" if svc.get("dripfeed") else "—"
    refill = "✅ دارد" if svc.get("refill") else "—"
    # === PATCH_SMM_NAME_PERSIANIZE_V1 ===
    fa_name = smm_api.persianize_name(svc["name"])
    name_lines = f"🏷 نام (پنل): {svc['name']}\n"
    if fa_name != svc["name"]:
        name_lines += f"🇮🇷 نام پیشنهادیِ فارسی (به‌عنوانِ نامِ نمایشی ذخیره می‌شه): <b>{fa_name}</b>\n"
    txt = (
        f"🛰 <b>جزئیات سرویس</b>\n\n"
        f"{name_lines}"
        f"🆔 شناسه: <code>{svc['service']}</code>\n"
        f"📂 دسته‌بندی: {smm_api.platform_label(pkey)} · {smm_api.service_type_label(tkey)}\n"
        + (f"🗂 دستهٔ پنل: {svc.get('category','')}\n" if svc.get('category') else "")
        + f"💰 قیمت پنل: <b>{float(svc['rate']):g}</b> (هر ۱۰۰۰)\n"
        f"🔢 حداقل: <b>{svc['min']:,}</b> | حداکثر: <b>{svc['max']:,}</b>\n"
        f"♻️ ریفیل: {refill} | 💧 دریپ‌فید: {dripfeed}\n"
        + (f"🧩 نوع: {svc.get('type','')}\n" if svc.get('type') else "")
        + f"\n{price_hint}\n\n"
        f"آیا این سرویس برای فروش اضافه شود؟"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و افزودن",
                              callback_data=f"smm:confirmcat:{sid}:{pkey}:{tkey}:{page}")],
        [InlineKeyboardButton(text="⬅️ بازگشت به لیست",
                              callback_data=f"smm:typ:{pkey}:{tkey}:{page}")],
    ])
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_ADD_PICK_CATEGORY_V1 ===
# درخواستِ ادمین: موقعِ تأییدِ افزودنِ سرویس، همون‌جا ازش دسته رو هم
# بپرسه — به‌جایِ اینکه بعداً جدا بره از «🗂 سرویس‌های فعالِ من» دسته رو
# ست کنه.
@smm_router.callback_query(F.data.startswith("smm:confirmcat:"))
async def smm_confirm_pick_category(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    sid, pkey, tkey, page = parts[2], parts[3], parts[4], parts[5]
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    if not cats:
        # هیچ دسته‌ای نساخته — سؤال‌کردن بی‌معنیه، مستقیم برو مرحلهٔ ثبت
        await _smm_finalize_add(cb, bot, row.id, sid, pkey, tkey, page, cat_pk=None)
        return
    rows = [[InlineKeyboardButton(text="🚫 بدونِ دسته (رد کردن)", callback_data=f"smm:confirm2:{sid}:{pkey}:{tkey}:{page}:0")]]
    for c in cats:
        plat = await platform_label_for_bot(row.id, c.platform)
        rows.append([InlineKeyboardButton(text=f"{plat} {c.name}", callback_data=f"smm:confirm2:{sid}:{pkey}:{tkey}:{page}:{c.id}")])
    await cb.answer()
    try:
        await cb.message.edit_text(
            "📦 این سرویس رو تویِ کدوم دسته اضافه کنم؟\n"
            "(می‌تونی بعداً هم از «🗂 سرویس‌هایِ فعالِ من» عوضش کنی)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(
            "📦 این سرویس رو تویِ کدوم دسته اضافه کنم؟\n"
            "(می‌تونی بعداً هم از «🗂 سرویس‌هایِ فعالِ من» عوضش کنی)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@smm_router.callback_query(F.data.startswith("smm:confirm2:"))
async def smm_confirm_add_with_category(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    sid, pkey, tkey, page, raw_cat = parts[2], parts[3], parts[4], parts[5], parts[6]
    cat_pk = None if raw_cat == "0" else int(raw_cat)
    await _smm_finalize_add(cb, bot, row.id, sid, pkey, tkey, page, cat_pk)


async def _smm_finalize_add(cb: CallbackQuery, bot: Bot, bot_id: int, sid: str, pkey: str, tkey: str,
                            page: str, cat_pk: int | None) -> None:
    """افزودن نهایی سرویس پس از تأیید ادمین (و انتخابِ دسته، اگه دسته‌ای بود)."""
    panel_pk = await _get_browsing_panel(bot_id)
    services = await _fetch_services_cached(bot_id, panel_pk)
    svc = next((x for x in services if x["service"] == sid), None)
    if not svc:
        await cb.answer("سرویس یافت نشد", show_alert=True)
        return
    async with Session() as s:
        # === PATCH_SMM_SAME_ID_OTHER_PANEL_V1 === یکتایی فقط درونِ همان پنل
        existing = await s.scalar(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.service_id == sid,
            SmmService.panel_pk == panel_pk))
        if not existing:
            s.add(SmmService(
                bot_id=bot_id, service_id=sid, name=smm_api.persianize_name(svc["name"]),
                category=svc.get("category", ""), panel_rate=svc["rate"],
                sell_rate=0, min_qty=svc["min"], max_qty=svc["max"],
                is_active=True, panel_pk=panel_pk, category_pk=cat_pk,
                # === PATCH_SMM_PROVIDER_DESC_V1 ===
                provider_desc=(svc.get("desc") or ""),
                # === PATCH_SMM_STORED_CLASSIFICATION_V1 ===
                # همون pkey/tkey ای که ادمین طیِ کلِ این مسیر (پلتفرم →
                # نوع → سرویس) عملاً تأیید کرده — دیگه لازم نیست جایِ
                # دیگه از رویِ اسم حدس زده بشه.
                platform_key=pkey, type_key=tkey))
            await s.commit()
        else:
            # === PATCH_SMM_PROVIDER_DESC_V1 ===
            # سرویس از قبل بوده: توضیحاتِ ارائه‌دهنده را تازه کن (ممکن است
            # پنل بعداً توضیحات اضافه/عوض کرده باشد). متنِ دستیِ ادمین
            # (description) دست‌نخورده می‌ماند.
            _pd = (svc.get("desc") or "").strip()
            if _pd and (existing.provider_desc or "") != _pd:
                existing.provider_desc = _pd
            if cat_pk is not None:
                existing.category_pk = cat_pk
            await s.commit()
    await cb.answer("برای فروش فعال شد ✅")
    # برگرد به لیست همان دسته
    await smm_typ(cb, bot, _override=(pkey, tkey, int(page)))


# ── سرویس‌های فعال من (قیمت‌گذاری دستی) ──

_MYSVC_PAGE_SIZE = 10


def _svc_platform_type(v: "SmmService") -> tuple[str, str]:
    """=== PATCH_SMM_STORED_CLASSIFICATION_V1 ===
    اولویت با چیزیه که موقعِ افزودن ذخیره شده (platform_key/type_key —
    همون چیزی که ادمین طیِ مسیرِ افزودن واقعاً انتخاب/تأیید کرده)؛ فقط
    اگه سرویس قدیمی‌تر از این فیچره و این دو تا خالیه، از رویِ اسم حدس
    می‌زنه (سازگاریِ عقب‌رو، نه رفتارِ پیش‌فرض)."""
    if v.platform_key and v.type_key:
        return v.platform_key, v.type_key
    return (smm_api.detect_platform(v.name or "", v.category or ""),
            smm_api.detect_service_type(v.name or "", v.category or ""))


async def _smm_myservices_groups_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_SMM_MYSERVICES_GROUPED_V1 ===
    درخواستِ ادمین: «مدیریتِ محصولات (سرویس‌هایِ من)» دیگه یه فهرستِ خامِ
    به‌هم‌ریخته نباشه — دقیقاً مثلِ ربات مرجع، اول پلتفرم رو نشون بده،
    بعد نوع (بوست/ممبر/ویو/...)، تا هر سرویس زیرِ دسته‌ی خودش جمع بشه.
    گروه‌بندی از رویِ همون پلتفرم/نوعی انجام می‌شه که ادمین موقعِ افزودن
    از پنل واقعاً تأیید کرده (_svc_platform_type) — نه یه حدسِ تازه."""
    await sync_category_platforms(bot_id)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(SmmService.bot_id == bot_id))).all()
    if not svcs:
        return ("🗂 هنوز سرویسی برای فروش فعال نکرده‌ای.\n\n"
                "از «➕ افزودنِ محصول» (زیرِ «مدیریتِ محصولات») یا «📥 دریافت "
                "و انتخاب سرویس‌ها» (زیرِ پنل پیش‌فرض) چند سرویس اضافه کن."), InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu")]])
    tree: dict[str, int] = {}
    for v in svcs:
        p, _t = _svc_platform_type(v)
        tree[p] = tree.get(p, 0) + 1
    rows = []
    for pkey in sorted(tree.keys(), key=lambda k: -tree[k]):
        rows.append([InlineKeyboardButton(
            text=f"{smm_api.platform_label(pkey)} ({tree[pkey]})", callback_data=f"smm:mysvc:plat:{pkey}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu", style=ButtonStyle.DANGER)])
    text = f"🗂 <b>مدیریتِ محصولات (سرویس‌هایِ من)</b>\n\nمجموعاً {len(svcs)} سرویس — اول پلتفرم را انتخاب کن:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _smm_myservices_types_content(bot_id: int, pkey: str) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(SmmService.bot_id == bot_id))).all()
    types: dict[str, int] = {}
    for v in svcs:
        p, t = _svc_platform_type(v)
        if p != pkey:
            continue
        types[t] = types.get(t, 0) + 1
    if not types:
        return "موردی تو این پلتفرم پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:myservices", style=ButtonStyle.DANGER)]])
    rows = []
    for tkey in sorted(types.keys(), key=lambda k: -types[k]):
        rows.append([InlineKeyboardButton(
            text=f"{smm_api.service_type_label(tkey)} ({types[tkey]})", callback_data=f"smm:mysvc:typ:{pkey}:{tkey}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:myservices", style=ButtonStyle.DANGER)])
    text = f"{smm_api.platform_label(pkey)}\n\nنوع سرویس را انتخاب کن:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smm:mysvc:plat:"))
async def smm_mysvc_plat(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.rsplit(":", 1)[1]
    text, kb = await _smm_myservices_types_content(row.id, pkey)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:mysvc:typ:"))
async def smm_mysvc_typ(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    pkey, tkey = parts[3], parts[4]
    # === PATCH_SMM_MYSERVICES_GROUPED_V1 ===
    # فیلتر تو Setting ذخیره می‌شه، نه تو خودِ callback_data — چون
    # back_page (که این فیلتر رو باید از پشتِ سرش رد کنه) تو ده‌ها جایِ
    # دیگه (ویرایشِ اسم/قیمت/توضیحات/دسته/حذف و غیره) به‌عنوانِ عددِ خامِ
    # صفحه پاس داده می‌شه؛ عوض‌کردنِ نوعش به یه رشتهٔ ترکیبی یعنی دست‌زدن
    # به همه‌ی اون‌ها. این‌جوری با صفر تغییر تو بقیه، همون «صفحهٔ N از
    # لیست» معنیش می‌مونه، فقط خودِ لیست از قبل فیلتر شده.
    await _set_setting(row.id, "smm_mysvc_filter_plat", pkey)
    await _set_setting(row.id, "smm_mysvc_filter_type", tkey)
    text, kb = await _smm_myservices_list_content(row.id, 1)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


async def _smm_myservices_list_content(bot_id: int, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    قبلاً این صفحه یه‌جا هم یه دیوارِ متنِ همه‌ی سرویس‌ها رو نشون می‌داد هم
    زیرش، جدا از اون متن، دکمه‌های عمومیِ هر سرویس رو — یعنی اسمِ سرویس
    از دکمه‌هاش بصری جدا بود و پیدا کردنِ اینکه کدوم دکمه مالِ کدوم
    سرویسه سخت بود. مهم‌تر، چون هر بار *همه‌ی* سرویس‌ها با هم رندر
    می‌شدن (و بعدِ هر ویرایش دوباره کل لیست ساخته می‌شد)، هرچی سرویس
    بیشتر می‌شد کندتر می‌شد. الان: لیست فقط اسمه (صفحه‌بندی‌شده)، هر
    سرویس رو که بزنی صفحه‌ی مخصوصِ خودش با همه‌ی جزئیات و دکمه‌هاش باز
    می‌شه — و بعدِ هر ویرایش فقط همون یه صفحه دوباره ساخته می‌شه، نه کل
    لیست.

    === PATCH_SMM_MYSERVICES_GROUPED_V1 ===
    این تابع دیگه ورودیِ اصلیِ صفحه نیست — smm:myservices (بدونِ شماره‌ی
    صفحه) اول گروه‌های پلتفرم/نوع رو نشون می‌ده (_smm_myservices_groups_
    content) و ادمین از اونجا با انتخابِ نوع، فیلترِ این تابع رو ست
    می‌کنه. اگه فیلتری ست نشده باشه (مثلاً یه لینکِ خیلی قدیمی)، رفتارِ
    قدیمی (نمایشِ همه) رو حفظ می‌کنه تا چیزی نشکنه."""
    page = max(1, page)
    filt_plat = await _get_setting(bot_id, "smm_mysvc_filter_plat", "")
    filt_type = await _get_setting(bot_id, "smm_mysvc_filter_type", "")
    async with Session() as s:
        all_svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id).order_by(SmmService.id))).all()
    if filt_plat:
        svcs_all = [v for v in all_svcs if _svc_platform_type(v) == (filt_plat, filt_type)]
        back_target = f"smm:mysvc:plat:{filt_plat}"
        header = f"{smm_api.platform_label(filt_plat)} — {smm_api.service_type_label(filt_type)}"
    else:
        svcs_all = all_svcs
        back_target = "smm:myservices"
        header = "🗂 <b>سرویس‌های فعال برای فروش</b>"
    total = len(svcs_all)
    if not all_svcs:
        # === PATCH_SMM_PRODUCTS_HUB_V1 ===
        return ("🗂 هنوز سرویسی برای فروش فعال نکرده‌ای.\n\n"
                "از «➕ افزودنِ محصول» (زیرِ «مدیریتِ محصولات») یا «📥 دریافت "
                "و انتخاب سرویس‌ها» (زیرِ پنل پیش‌فرض) چند سرویس اضافه کن."), InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:products:menu")]])
    if not total:
        return f"{header}\n\nسرویسی تو این دسته پیدا نشد (شاید حذف/جابه‌جا شده).", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target, style=ButtonStyle.DANGER)]])
    total_pages = max(1, (total + _MYSVC_PAGE_SIZE - 1) // _MYSVC_PAGE_SIZE)
    page = min(page, total_pages)
    svcs = svcs_all[(page - 1) * _MYSVC_PAGE_SIZE: page * _MYSVC_PAGE_SIZE]
    rows = []
    for v in svcs:
        if v.sell_rate and float(v.sell_rate) > 0:
            pr = f"🔒{int(v.sell_rate):,}"
        elif v.is_manual:
            pr = "⚠️بدونِ قیمت"
        else:
            pr = f"⚙️{float(v.panel_rate):g}"
        rows.append([InlineKeyboardButton(text=f"{v.name[:38]} — {pr}", callback_data=f"smm:svcview:{v.id}:{page}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"smm:myservices:{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"smm:myservices:{page+1}"))
    if nav:
        rows.append(nav)
    # === PATCH_SMM_PRODUCTS_HUB_V1 / PATCH_SMM_MYSERVICES_GROUPED_V1 ===
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target, style=ButtonStyle.DANGER)])
    text = f"{header} (صفحهٔ {page} از {total_pages}، مجموعاً {total})\n\nروی هرکدوم بزن تا جزئیات و دکمه‌هاش رو ببینی:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _smm_myservice_detail_content(bot_id: int, svc_id: int, back_page: int) -> tuple[str, InlineKeyboardMarkup]:
    """صفحه‌ی جزئیاتِ یک سرویس — همه‌ی دکمه‌های ویرایش زیرِ همینه، نه تو
    یه دیوارِ جدا."""
    async with Session() as s:
        v = await s.get(SmmService, svc_id)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=f"smm:myservices:{back_page}")]])
    if not v or v.bot_id != bot_id:
        return "❌ این سرویس دیگر پیدا نشد.", back_kb
    if v.sell_rate and float(v.sell_rate) > 0:
        pr = f"🔒 دستی: {int(v.sell_rate):,} تومان/هزار"
    elif v.is_manual:
        pr = "⚠️ هنوز قیمتی ثبت نشده"
    else:
        pr = f"⚙️ خودکار (قیمتِ پنل: {float(v.panel_rate):g})"
    cat_name = "بدونِ دسته (خودکار)"
    if v.category_pk:
        async with Session() as s:
            c = await s.get(SmmCategory, v.category_pk)
        if c:
            cat_name = f"{await platform_label_for_bot(bot_id, c.platform)} {c.name}"
    # === PATCH_SMM_PRODUCT_NUMBER_SHOWN_V1 ===
    # گزارشِ ادمین (فدایی): لینکِ سریعِ svc_47 «کار نمی‌کند». علت: عددی که ادمین این‌جا
    # می‌دید «شناسهٔ سرویسِ پنل» (شناسهٔ ارائه‌دهنده) بود، ولی لینک و دستورِ /<عدد> با
    # «شمارهٔ محصول» در خودِ ربات (کلیدِ داخلی) کار می‌کنند و آن هیچ‌جا نمایش داده
    # نمی‌شد. شناسهٔ پنل حتی یکتا هم نیست (دو پنل می‌توانند هر دو سرویسِ ۴۷ داشته
    # باشند). حالا شمارهٔ محصول، دستور و لینکِ آماده‌یِ کپی همین‌جا نشان داده می‌شود.
    _uname = ""
    try:
        async with Session() as s:
            _b = await s.get(BotRow, bot_id)
        _uname = (_b.username or "") if _b else ""
    except Exception:
        logger.exception("[SMM] خواندنِ یوزرنیمِ ربات برایِ لینکِ محصول ناموفق بود")
    lines = [
        f"🛰 <b>{v.name}</b>\n",
        f"🔢 شمارهٔ محصول: <code>{v.id}</code>",
        f"⌨️ دستورِ سریع: <code>/{v.id}</code>",
    ] + ([f"🔗 لینکِ سریع: <code>https://t.me/{_uname}?start=svc_{v.id}</code>"] if _uname else []) + [
        f"🆔 شناسهٔ سرویسِ پنل: <code>{v.service_id}</code>",
        f"📦 دسته: {cat_name}",
        f"📝 توضیحات: {'ثبت شده ✅' if v.description else 'ثبت نشده'}",
        f"💵 قیمت: {pr}",
        f"🔢 حداقل/حداکثر: {v.min_qty} / {v.max_qty}",
    ]
    rows = [
        [InlineKeyboardButton(text="✏️ تغییرِ اسم", callback_data=f"smm:rename:{v.id}:{back_page}")],
        [InlineKeyboardButton(text=("📝 ویرایشِ توضیحات" if v.description else "📄 افزودنِ توضیحات"),
                              callback_data=f"smm:setdesc:{v.id}:{back_page}")],
        [InlineKeyboardButton(text="📦 تغییرِ دسته", callback_data=f"smm:svc:cat:{v.id}:myd:{back_page}")],
    ]
    price_row = [InlineKeyboardButton(
        text=("💵 ویرایشِ قیمتِ دستی" if (v.sell_rate and float(v.sell_rate) > 0) else "💵 ثبتِ قیمتِ دستی"),
        callback_data=f"smm:setprice:{v.id}:{back_page}")]
    if v.sell_rate and float(v.sell_rate) > 0 and not v.is_manual:
        price_row.append(InlineKeyboardButton(text="↩️ خودکار کن", callback_data=f"smm:clearprice:{v.id}:{back_page}"))
    rows.append(price_row)
    rows.append([InlineKeyboardButton(text="🔢 حداقل/حداکثرِ سفارش", callback_data=f"smm:setminmax:{v.id}:{back_page}")])
    # === PATCH_SMM_MANUAL_SORT_ORDER_V1 ===
    # این دو دکمه فقط وقتی نشون داده می‌شن که ادمین حالتِ «ترتیبِ نمایشِ
    # سرویس‌ها» رو رویِ «دستی» گذاشته باشه — وگرنه بی‌فایده و گیج‌کننده‌ست
    # (چون تو حالت‌هایِ دیگه، این ترتیب رویِ چیزی که مشتری می‌بینه اثر نداره).
    if await _get_setting(bot_id, "smm_sort_order", "none") == "manual":
        rows.append([
            InlineKeyboardButton(text="🔼 بالاتر", callback_data=f"smm:svc:moveup:{v.id}:{back_page}"),
            InlineKeyboardButton(text="🔽 پایین‌تر", callback_data=f"smm:svc:movedown:{v.id}:{back_page}"),
        ])
    rows.append([InlineKeyboardButton(text="🗑 حذفِ سرویس", callback_data=f"smm:del:{v.id}:{back_page}", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=f"smm:myservices:{back_page}", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smm:myservices"))
async def smm_myservices(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    # === PATCH_SMM_MYSERVICES_GROUPED_V1 ===
    # smm:myservices خام (بدونِ شماره‌ی صفحه) یعنی «از اول» — یعنی
    # گروه‌هایِ پلتفرم، نه لیستِ خام. smm:myservices:{page} یعنی «همون
    # صفحه‌ای که بودی، تو همون فیلترِ فعلی».
    if len(parts) <= 2:
        # ورودیِ تازه به این بخش، یعنی فیلترِ قبلی (اگه بود) دیگه معنی نداره
        await _set_setting(row.id, "smm_mysvc_filter_plat", "")
        await _set_setting(row.id, "smm_mysvc_filter_type", "")
        text, kb = await _smm_myservices_groups_content(row.id)
    else:
        # === PATCH_SMM_MYSERVICES_PAGE_GUARD_V1 ===
        # قبلاً int(parts[2]) لخت بود و هر callbackِ ناسازگاری که به این
        # هندلر می‌رسید با «invalid literal for int() with base 10: 'setcat'»
        # می‌ترکید (در error.log دیده شده). حالا به‌جایِ کرش، صفحهٔ اول.
        try:
            page = int(parts[2])
        except (ValueError, IndexError):
            logger.warning("smm_myservices: بخشِ صفحهٔ نامعتبر در callback: %r", cb.data)
            page = 0
        text, kb = await _smm_myservices_list_content(row.id, page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:svcview:"))
async def smm_svcview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    svc_id, back_page = int(parts[2]), int(parts[3])
    text, kb = await _smm_myservice_detail_content(row.id, svc_id, back_page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_SMM_MANUAL_SORT_ORDER_V1 ===
async def _smm_svc_move(cb: CallbackQuery, bot: Bot, direction: int):
    """جابه‌جاییِ یک سرویس با همسایه‌اش در ترتیبِ نمایش (سوآپِ sort_order).
    direction=-1 یعنی بالاتر (نزدیک‌ترِ به اولِ لیست)، +1 یعنی پایین‌تر.
    اگه هر دو مقدارِ sort_order برابر باشن (مثلاً هر دو هنوز پیش‌فرضِ ۰ان،
    چون هیچ‌کس قبلاً جابه‌جا نشده)، فقط سوآپِ خام هیچ اثری نداره؛ برایِ
    همین تویِ اون حالت، به‌جایِ سوآپ، یکی از دو مقدار رو یکی کمتر/بیشتر
    می‌کنیم تا جابه‌جایی واقعاً دیده بشه."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[3]), int(parts[4])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        all_svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == row.id).order_by(SmmService.sort_order, SmmService.id))).all()
        idx = next((i for i, x in enumerate(all_svcs) if x.id == v.id), None)
        if idx is None:
            await cb.answer()
            return
        nidx = idx + direction
        if nidx < 0 or nidx >= len(all_svcs):
            await cb.answer("در ابتدایِ لیسته" if direction < 0 else "در انتهایِ لیسته")
            return
        other = all_svcs[nidx]
        orig_v, orig_other = v.sort_order, other.sort_order
        if orig_v != orig_other:
            v.sort_order, other.sort_order = orig_other, orig_v
        elif direction < 0:
            v.sort_order = orig_other - 1
        else:
            v.sort_order = orig_other + 1
        await s.commit()
    await cb.answer("✅ جابه‌جا شد")
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:svc:moveup:"))
async def smm_svc_moveup(cb: CallbackQuery, bot: Bot):
    await _smm_svc_move(cb, bot, direction=-1)


@smm_router.callback_query(F.data.startswith("smm:svc:movedown:"))
async def smm_svc_movedown(cb: CallbackQuery, bot: Bot):
    await _smm_svc_move(cb, bot, direction=1)


@smm_router.callback_query(F.data.startswith("smm:del:"))
async def smm_del_service(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[2]), int(parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if v and v.bot_id == row.id:
            await s.delete(v)
            await s.commit()
    await cb.answer("حذف شد")
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    # چون خودِ سرویس حذف شد، برگردیم به لیست، نه صفحه‌ی جزئیاتِ خودش
    text, kb = await _smm_myservices_list_content(row.id, back_page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_PER_SERVICE_PRICE_OVERRIDE_V1 ===
@smm_router.callback_query(F.data.startswith("smm:clearprice:"))
async def smm_clear_price(cb: CallbackQuery, bot: Bot):
    """قیمتِ دستیِ همین یک سرویس را پاک می‌کند تا دوباره از فرمولِ سود
    (خودکار) محاسبه شود — فقط برایِ سرویسِ متصل به پنل معنا دارد."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[2]), int(parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id:
            await cb.answer("سرویس پیدا نشد", show_alert=True)
            return
        if v.is_manual:
            await cb.answer("سرویسِ دستی همیشه باید قیمتِ خودش را داشته باشد.", show_alert=True)
            return
        v.sell_rate = 0
        await s.commit()
    await cb.answer("✅ به حالتِ خودکار برگشت")
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:rename:"))
async def smm_rename_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[2]), int(parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id:
        await cb.answer("سرویس یافت نشد", show_alert=True)
        return
    await state.update_data(smm_rename_svc_id=vid, smm_rename_back_page=back_page)
    await state.set_state(SmmFSM.rename_service)
    await cb.message.answer(
        f"✏️ اسمِ فعلی: <code>{v.name}</code>\n\n"
        "اسمِ تازه (فارسی یا هرچی دوست داری) را بفرست — این فقط چیزی است "
        "که مشتری می‌بیند، اسمِ داخلیِ سرویس در پنل دست‌نخورده می‌ماند:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:svcview:{vid}:{back_page}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.rename_service, F.text)
async def smm_rename_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    vid = data.get("smm_rename_svc_id")
    back_page = data.get("smm_rename_back_page", 1)
    new_name = (msg.text or "").strip()[:255]
    if not new_name:
        await msg.answer("❌ اسمِ خالی قابلِ‌قبول نیست.")
        return
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id:
            await msg.answer("سرویس یافت نشد.")
            return
        v.name = new_name
        await s.commit()
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    await msg.answer(f"✅ اسم به «{new_name}» تغییر کرد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_SERVICE_DESCRIPTION_V1 ===
@smm_router.callback_query(F.data.startswith("smm:setdesc:"))
async def smm_desc_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[2]), int(parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id:
        await cb.answer("سرویس یافت نشد", show_alert=True)
        return
    await state.update_data(smm_desc_svc_id=vid, smm_desc_back_page=back_page)
    await state.set_state(SmmFSM.desc_service)
    cur = f"\n\nتوضیحاتِ فعلی:\n<code>{v.description}</code>" if v.description else ""
    await cb.message.answer(
        f"📝 توضیحاتِ سرویسِ «{v.name}» را بفرست — همین متن قبل از خرید به "
        f"مشتری نشان داده می‌شود (مثلاً کیفیت، سرعتِ تحویل، شرایط و غیره).\n"
        f"برایِ پاک‌کردنِ توضیحاتِ فعلی، فقط یک خط تیره (-) بفرست."
        f"{cur}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:svcview:{vid}:{back_page}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.desc_service, F.text)
async def smm_desc_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    vid = data.get("smm_desc_svc_id")
    back_page = data.get("smm_desc_back_page", 1)
    raw = (msg.text or "").strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        if row and vid:
            # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
            text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
            await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer("لغو شد.")
        return
    await state.clear()
    if not row:
        return
    new_desc = None if raw == "-" else raw[:1000]
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id:
            await msg.answer("سرویس یافت نشد.")
            return
        v.description = new_desc
        svc_name = v.name
        await s.commit()
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    msg_txt = "✅ توضیحات پاک شد." if new_desc is None else f"✅ توضیحاتِ «{svc_name}» ثبت شد."
    await msg.answer(msg_txt + "\n\n" + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_MINMAX_V1 ===
@smm_router.callback_query(F.data.startswith("smm:setminmax:"))
async def smm_setminmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """تغییرِ حداقل و حداکثرِ تعدادِ سفارشِ یک سرویس.

    مقدارِ پیش‌فرض از پنلِ SMM می‌آید، ولی مالکِ ربات ممکن است بخواهد
    سخت‌گیرانه‌تر بفروشد (مثلاً حداقل ۵۰۰ ممبر به‌جایِ ۱۰۰).
    """
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cb_parts = cb.data.split(":")
    vid, back_page = int(cb_parts[2]), int(cb_parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id:
        await cb.answer("سرویس پیدا نشد", show_alert=True)
        return
    await state.update_data(smm_minmax_svc_id=vid, smm_minmax_back_page=back_page)
    await state.set_state(SmmFSM.minmax)
    await cb.message.answer(
        f"🔢 <b>{v.name[:60]}</b>\n\n"
        f"مقدارِ فعلی: حداقل <b>{v.min_qty:,}</b> | حداکثر <b>{v.max_qty:,}</b>\n\n"
        "دو عدد با فاصله بفرست: <b>حداقل حداکثر</b>\n"
        "مثال: <code>500 100000</code>\n\n"
        "ℹ️ اگر فقط یک عدد بفرستی، همان حداقل عوض می‌شود و حداکثر سرِ جایش "
        "می‌ماند.\n"
        "⚠️ اگر حداقلی که می‌گذاری از حداقلِ خودِ پنل کمتر باشد، پنل سفارش "
        "را رد می‌کند؛ پس فقط بالاتر ببرش.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:svcview:{vid}:{back_page}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.minmax, F.text)
async def smm_minmax_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    data0 = await state.get_data()
    vid0 = data0.get("smm_minmax_svc_id")
    back_page0 = data0.get("smm_minmax_back_page", 1)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        if vid0:
            # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
            text, kb = await _smm_myservice_detail_content(row.id, vid0, back_page0)
            await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer("لغو شد.")
        return
    parts = raw.replace(",", " ").replace("،", " ").split()
    nums = []
    for x in parts:
        x = x.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        if not x.isdigit():
            # همان الگویِ بقیهٔ ورودی‌ها: متنِ نامربوط یعنی کاربر منصرف شده
            await state.clear()
            raise SkipHandler
        nums.append(int(x))
    if not nums:
        await state.clear()
        raise SkipHandler

    data = await state.get_data()
    vid = data.get("smm_minmax_svc_id")
    back_page = data.get("smm_minmax_back_page", 1)
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id:
            await state.clear()
            await msg.answer("سرویس پیدا نشد.")
            return
        mn = max(1, nums[0])
        mx = nums[1] if len(nums) > 1 else int(v.max_qty or 0)
        if mx and mx < mn:
            await msg.answer("حداکثر نباید کمتر از حداقل باشد. دوباره بفرست:")
            return
        v.min_qty, v.max_qty = mn, mx
        await s.commit()
        name = v.name
    await state.clear()
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    await msg.answer(f"✅ ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:setprice:"))
async def smm_setprice(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, back_page = int(parts[2]), int(parts[3])
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id:
        await cb.answer("سرویس پیدا نشد", show_alert=True)
        return
    await state.update_data(smm_price_svc_id=vid, smm_price_back_page=back_page)
    await state.set_state(SmmFSM.manual_price)
    cur_line = (f"قیمتِ دستیِ فعلی: <b>{int(v.sell_rate):,} ت/هزار</b>\n\n"
               if v.sell_rate and float(v.sell_rate) > 0 else "")
    await cb.message.answer(
        f"💵 <b>{v.name[:60]}</b>\n\n{cur_line}"
        "قیمت فروش این سرویس را به <b>تومان به‌ازای هر ۱۰۰۰ عدد</b> بفرست (فقط عدد):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:svcview:{vid}:{back_page}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.manual_price, F.text)
async def smm_manual_price_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    data0 = await state.get_data()
    vid0 = data0.get("smm_price_svc_id")
    back_page0 = data0.get("smm_price_back_page", 1)
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    # باگِ گزارش‌شده (تیکت #400): این هندلر هر متنِ غیرِعددی را برایِ همیشه
    # رد می‌کرد — یعنی اگر کاربر/ادمین منصرف می‌شد و دکمه‌ای از منویِ اصلی
    # می‌زد، همان‌جا برایِ همیشه گیر می‌کرد. حالا «❌ لغو» صریحاً مدیریت
    # می‌شود و هر متنِ غیرِعددیِ دیگر با SkipHandler به هندلرِ منویِ اصلی
    # واگذار می‌شود (همان الگویِ tenant_buttons_stuck_state_rescue).
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        if vid0:
            # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
            text, kb = await _smm_myservice_detail_content(row.id, vid0, back_page0)
            await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer("لغو شد.")
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit():
        await state.clear()
        raise SkipHandler
    data = await state.get_data()
    vid = data.get("smm_price_svc_id")
    back_page = data.get("smm_price_back_page", 1)
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if v and v.bot_id == row.id:
            v.sell_rate = int(raw)
            await s.commit()
    await state.clear()
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    text, kb = await _smm_myservice_detail_content(row.id, vid, back_page)
    await msg.answer("✅ قیمت ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ── تغییر حالت‌ها و اعداد ──

async def _panel_settings_content(bot_id: int, panel_pk: int | None) -> tuple[str, InlineKeyboardMarkup]:
    """تنظیماتِ قیمت‌گذاری/نرخ/حالتِ سفارش/سیاستِ خطا — مخصوصِ یک پنلِ
    خاص (یا پنلِ پیش‌فرض اگر panel_pk=None).
    === PATCH_SMM_PER_PANEL_SETTINGS_V1 ==="""
    _, _, panel_name = await _panel_creds(bot_id, panel_pk)
    price_mode = await _panel_price_mode(bot_id, panel_pk)
    margin = await _panel_margin_pct(bot_id, panel_pk)
    rate_mode = await _panel_rate_mode(bot_id, panel_pk)
    toman_rate = await _panel_toman_per_unit(bot_id, panel_pk)
    order_mode = await _panel_order_mode(bot_id, panel_pk)
    fail_policy = await _panel_fail_policy(bot_id, panel_pk)
    # === PATCH_SMM_PRICE_ROUND_V1 ===
    price_round = await _panel_price_round(bot_id, panel_pk)
    # === PATCH_SMM_PANEL_CURRENCY_V1 ===
    currency = await _panel_currency(bot_id, panel_pk)

    price_txt = "درصد سود (خودکار)" if price_mode == "margin" else "دستی"
    rate_txt = "خودکار (زنده)" if rate_mode == "auto" else "دستی"
    order_txt = "خودکار" if order_mode == "auto" else "تأیید ادمین"
    fail_txt = "بازگشتِ خودکارِ وجه" if fail_policy == "auto_refund" else "نگه‌داری برایِ بررسیِ دستی"
    round_txt = "بدونِ رند" if price_round <= 0 else f"رندِ {price_round:,} تومانی"
    currency_txt = "تومان (پنل خودش تومان می‌ده)" if currency == "toman" else "ارزی/دلار (با نرخِ تبدیل)"

    pid = "0" if panel_pk is None else str(panel_pk)
    lines = [
        f"⚙️ <b>تنظیماتِ پنلِ «{panel_name}»</b>\n",
        f"واحدِ قیمتِ پنل: <b>{currency_txt}</b>",
        f"حالتِ قیمت‌گذاری: <b>{price_txt}</b>" + (f" ({margin:g}٪)" if price_mode == "margin" else ""),
        f"نرخِ تبدیل: <b>{rate_txt}</b>" + (f" ({toman_rate:g} تومان)" if toman_rate else ""),
    ]
    if currency == "usd" and rate_mode != "auto" and not toman_rate:
        lines.append("⚠️ نرخِ دستی ثبت نشده؛ از نرخِ زندهٔ دلار استفاده می‌شود. برایِ ارزِ دیگر "
                     "(یورو و …) «نرخِ دستی» را به تومان ثبت کن.")
    lines += [
        f"حالتِ ثبتِ سفارش: <b>{order_txt}</b>",
        f"سیاستِ خطا: <b>{fail_txt}</b>",
        f"رندِ قیمت: <b>{round_txt}</b>",
    ]
    rows = [
        [InlineKeyboardButton(text=f"💰 واحدِ پنل: {currency_txt}", callback_data=f"smm:panel:set:{pid}:togcurrency")],
        [InlineKeyboardButton(text=f"⚙️ حالتِ قیمت: {price_txt}", callback_data=f"smm:panel:set:{pid}:togprice")],
    ]
    if price_mode == "margin":
        if currency == "usd":
            rows.append([InlineKeyboardButton(text=f"💱 نرخِ تبدیل: {rate_txt}", callback_data=f"smm:panel:set:{pid}:tograte")])
            rows.append([InlineKeyboardButton(text="✏️ نرخِ دستی (پشتیبان/جایگزین)", callback_data=f"smm:panel:set:{pid}:rate")])
        rows.append([InlineKeyboardButton(text=f"📈 درصدِ سود ({margin:g}٪)", callback_data=f"smm:panel:set:{pid}:margin")])
        # === PATCH_SMM_PRICE_ROUND_V1 ===
        rows.append([InlineKeyboardButton(text=f"🔘 رندِ قیمت: {round_txt}", callback_data=f"smm:panel:set:{pid}:toground")])
    rows.append([InlineKeyboardButton(text=f"🧾 حالتِ ثبتِ سفارش: {order_txt}", callback_data=f"smm:panel:set:{pid}:togorder")])
    rows.append([InlineKeyboardButton(text=f"🩹 سیاستِ خطا: {fail_txt}", callback_data=f"smm:panel:set:{pid}:togfail")])
    # === PATCH_SMM_PROVIDER_DETAIL_V1 ===
    # طبقِ قاعدهٔ ناوبری («بازگشت» یعنی دقیقاً یک سطح بالاتر): برایِ
    # پنل‌هایِ اضافه، یک سطح بالاتر الان صفحهٔ «عملیاتِ ارائه‌دهنده»ست، نه
    # لیستِ خامِ پنل‌ها.
    # === PATCH_SMM_DEFAULT_PANEL_SETTINGS_DEADEND_V1 ===
    # پنلِ پیش‌فرض (pid == "0") صفحهٔ عملیات ندارد و این تنظیمات حالا از
    # خودِ صفحهٔ «پنل SMM» باز می‌شود، پس یک سطح بالاترش همان است — قبلاً
    # به لیستِ ارائه‌دهندگان می‌رفت، یعنی بازگشت یک سطح اضافه می‌پرید.
    back_target = "smm:menu" if pid == "0" else f"smm:panel:detail:{pid}"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target, style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smm:panel:settings:"))
async def smm_panel_settings(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    raw = cb.data.rsplit(":", 1)[1]
    panel_pk = None if raw == "0" else int(raw)
    text, kb = await _panel_settings_content(row.id, panel_pk)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:panel:set:"))
async def smm_panel_set_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    raw_pid, action = parts[3], parts[4]
    panel_pk = None if raw_pid == "0" else int(raw_pid)

    if action == "togcurrency":
        # === PATCH_SMM_PANEL_CURRENCY_V1 ===
        cur = await _panel_currency(row.id, panel_pk)
        new = "usd" if cur == "toman" else "toman"
        await _panel_set_setting(row.id, panel_pk, "currency", new)
        await cb.answer("واحدِ پنل «تومان» شد — دیگه نرخِ دلار روش اعمال نمی‌شه" if new == "toman"
                         else "واحدِ پنل «دلار» شد")
    elif action == "togprice":
        cur = await _panel_price_mode(row.id, panel_pk)
        await _panel_set_setting(row.id, panel_pk, "price_mode", "manual" if cur == "margin" else "margin")
        await cb.answer("حالتِ قیمت‌گذاری عوض شد")
    elif action == "tograte":
        cur = await _panel_rate_mode(row.id, panel_pk)
        new = "manual" if cur == "auto" else "auto"
        await _panel_set_setting(row.id, panel_pk, "rate_mode", new)
        await cb.answer("نرخِ تبدیل روی خودکار (زنده) رفت" if new == "auto" else "نرخِ تبدیل روی دستی رفت")
    elif action == "togorder":
        cur = await _panel_order_mode(row.id, panel_pk)
        await _panel_set_setting(row.id, panel_pk, "order_mode", "auto" if cur == "manual" else "manual")
        await cb.answer("حالتِ ثبتِ سفارش عوض شد")
    elif action == "togfail":
        cur = await _panel_fail_policy(row.id, panel_pk)
        new = "manual_review" if cur == "auto_refund" else "auto_refund"
        await _panel_set_setting(row.id, panel_pk, "fail_policy", new)
        await cb.answer("✅ ذخیره شد")
    elif action == "toground":
        # === PATCH_SMM_PRICE_ROUND_V1 ===
        cur = await _panel_price_round(row.id, panel_pk)
        cycle = [0, 10, 100, 1000]
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else 0
        await _panel_set_setting(row.id, panel_pk, "price_round", nxt)
        await cb.answer("✅ ذخیره شد")
    elif action == "margin":
        await state.set_state(SmmFSM.margin_pct)
        await state.update_data(smm_settings_panel_pk=raw_pid)
        await cb.message.answer(
            "📈 درصدِ سود را بفرست (فقط عدد، مثلاً 30):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:panel:settings:{raw_pid}", style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    elif action == "rate":
        await state.set_state(SmmFSM.toman_rate)
        await state.update_data(smm_settings_panel_pk=raw_pid)
        await cb.message.answer(
            "💱 نرخ تبدیل هر واحدِ پولِ پنل به تومان را بفرست (فقط عدد).\n\n"
            "مثال: اگر قیمت‌های پنل به دلار است و هر دلار ۷۰٬۰۰۰ تومان، عدد "
            "<code>70000</code> را بفرست.\nاگر قیمت‌های پنل خودش به تومان است، عدد <code>1</code> را بفرست.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"smm:panel:settings:{raw_pid}", style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    else:
        await cb.answer()
        return

    text, kb = await _panel_settings_content(row.id, panel_pk)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_SORT_ORDER_V1 ===
@smm_router.callback_query(F.data == "smm:togsortorder")
async def smm_togsortorder(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_sort_order", "none")
    idx = _SORT_ORDER_CYCLE.index(cur) if cur in _SORT_ORDER_CYCLE else 0
    new = _SORT_ORDER_CYCLE[(idx + 1) % len(_SORT_ORDER_CYCLE)]
    await _set_setting(row.id, "smm_sort_order", new)
    await cb.answer(f"✅ ترتیب: {_SORT_ORDER_LABELS[new]}")
    # === PATCH_SMM_PRODUCTS_HUB_V1 ===
    await smm_products_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:togpricemode")
async def smm_togpricemode(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_price_mode", "margin")
    new = "manual" if cur == "margin" else "margin"
    await _set_setting(row.id, "smm_price_mode", new)
    await cb.answer("حالت قیمت‌گذاری عوض شد")
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:togratemode")
async def smm_tograte_mode(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_rate_mode", "manual")
    new = "manual" if cur == "auto" else "auto"
    await _set_setting(row.id, "smm_rate_mode", new)
    await cb.answer(
        "نرخ تبدیل روی خودکار (زنده) رفت" if new == "auto" else "نرخ تبدیل روی دستی رفت")
    await smm_menu(cb, bot)


# === PATCH_SMM_QLC_DEPTH_V1 ===
# === PATCH_SMM_RESET_SERVICES_V1 ===
# === PATCH_SMM_AUTO_DESC_V1 ===
# === PATCH_SMM_BTN_LAYOUT_V1 ===
@smm_router.callback_query(F.data == "smm:btncols")
async def smm_btn_cols_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_btn_cols", "1")
    new = "2" if cur != "2" else "1"
    await _set_setting(row.id, "smm_btn_cols", new)
    await cb.answer("چیدمان: " + ("دوتایی" if new == "2" else "تکی"))
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:btncount")
async def smm_btn_count_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_btn_count", "1")
    new = "0" if cur != "0" else "1"
    await _set_setting(row.id, "smm_btn_count", new)
    await cb.answer("نمایشِ تعداد: " + ("خاموش" if new == "0" else "روشن"))
    await smm_menu(cb, bot)


# === PATCH_SMM_PRICE_VIEW_MODE_V1 ===
@smm_router.callback_query(F.data == "smm:pricview")
async def smm_price_view_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_price_view", "unit")
    idx = _PRICE_VIEW_CYCLE.index(cur) if cur in _PRICE_VIEW_CYCLE else 0
    new = _PRICE_VIEW_CYCLE[(idx + 1) % len(_PRICE_VIEW_CYCLE)]
    await _set_setting(row.id, "smm_price_view", new)
    await cb.answer(f"نمایشِ قیمت: {_PRICE_VIEW_LABELS[new]}")
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:descmode")
async def smm_desc_mode(cb: CallbackQuery, bot: Bot):
    """چرخشِ حالتِ توضیحاتِ محصول: خودکار+دستی \u2192 فقط دستی \u2192 خاموش."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    order = ["auto", "manual", "off"]
    cur = await _get_setting(row.id, "smm_desc_mode", "auto")
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
    await _set_setting(row.id, "smm_desc_mode", nxt)
    await cb.answer("توضیحاتِ محصول: " + {"off": "خاموش", "manual": "فقط دستی",
                                          "auto": "خودکار + دستی"}[nxt])
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:desctr")
async def smm_desc_translate_toggle(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_DESC_TRANSLATE_V1 === روشن/خاموش ترجمهٔ خودکار توضیحات انگلیسی ارائه‌دهنده به فارسی."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_desc_translate", "1") != "0"
    await _set_setting(row.id, "smm_desc_translate", "0" if cur else "1")
    await cb.answer("ترجمهٔ خودکار توضیحات: " + ("خاموش" if cur else "روشن"))
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:reset")
async def smm_reset_ask(cb: CallbackQuery, bot: Bot):
    """صفحهٔ تأیید برایِ پاک‌کردنِ سرویس‌هایِ اضافه‌شده."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        n_all = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == row.id))
        n_on = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == row.id, SmmService.is_active == True))  # noqa: E712
    if not n_all:
        await cb.answer("سرویسی برایِ پاک‌کردن نیست.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\U0001f6d1 غیرفعال\u200cکردنِ همه ({n_on})",
                              callback_data="smm:reset:off",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"\U0001f5d1 پاک\u200cکردنِ کاملِ همه ({n_all})",
                              callback_data="smm:reset:del",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="\u2b05\ufe0f انصراف",
                              callback_data="smm:menu")],
    ])
    await cb.message.edit_text(
        "\u267b\ufe0f <b>ریستِ محصولات</b>\n\n"
        f"سرویس\u200cهایِ ثبت\u200cشده: <b>{n_all}</b> (فعال: {n_on})\n\n"
        "\u2022 <b>غیرفعال\u200cکردن</b>: سرویس\u200cها می\u200cمانند ولی از "
        "فروش خارج می\u200cشوند \u2014 هر وقت خواستی دوباره فعالشان کن.\n"
        "\u2022 <b>پاک\u200cکردنِ کامل</b>: ردیف\u200cها حذف می\u200cشوند و باید "
        "از نو از پنل اضافه کنی. سفارش\u200cهایِ قبلی سالم می\u200cمانند.\n\n"
        "\u26a0\ufe0f پاک\u200cکردن برگشت\u200cپذیر نیست.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.in_({"smm:reset:off", "smm:reset:del"}))
async def smm_reset_do(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    hard = cb.data.endswith(":del")
    # === PATCH_SMM_RESET_FK_FIX_V1 ===
    # گزارش: «ریستِ محصولات هیچ کاری نمی‌کند». علت: پاک‌کردنِ کامل، ردیف‌هایِ SmmService را
    # مستقیم حذف می‌کرد، ولی شماره‌هایِ مجازی (VnumNumber) با کلیدِ خارجی به همین
    # ردیف‌ها وصل‌اند → روی PostgreSQL خطایِ foreign key می‌داد، استثنا بالا می‌رفت و کاربر
    # هیچ واکنشی نمی‌دید. حالا: شماره‌هایِ فروش‌نرفتهٔ وابسته اول پاک می‌شوند؛ سرویسی که
    # شمارهٔ فروخته‌شده دارد (سابقهٔ مشتری) پاک نمی‌شود و فقط غیرفعال می‌شود؛ و هر خطایِ
    # دیگری به کاربر گفته می‌شود نه سکوت.
    n_del = n_off = 0
    try:
        from models import VnumNumber
        from sqlalchemy import delete
        async with Session() as s:
            rows_ = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == row.id))).all()
            for v in rows_:
                if not hard:
                    v.is_active = False
                    n_off += 1
                    continue
                sold = await s.scalar(select(func.count()).select_from(VnumNumber).where(
                    VnumNumber.country_service_pk == v.id, VnumNumber.status == "sold"))
                if sold:
                    v.is_active = False
                    n_off += 1
                    continue
                await s.execute(delete(VnumNumber).where(VnumNumber.country_service_pk == v.id))
                await s.delete(v)
                n_del += 1
            await s.commit()
    except Exception as e:                                   # noqa: BLE001
        logger.exception("[SMM] reset services failed | bot=%s hard=%s", row.id, hard)
        await cb.answer(f"❌ ریست انجام نشد ({type(e).__name__}). دوباره امتحان کن؛ اگر تکرار شد «غیرفعال‌کردن» را بزن.",
                        show_alert=True)
        return
    logger.info("[SMM] reset services | bot=%s hard=%s deleted=%s deactivated=%s", row.id, hard, n_del, n_off)
    if hard:
        msg_txt = f"\u2705 {n_del} سرویس حذف شد." + (
            f"\n{n_off} سرویس چون شمارهٔ فروخته‌شده داشت فقط غیرفعال شد." if n_off else "")
    else:
        msg_txt = f"\u2705 {n_off} سرویس غیرفعال شد."
    await cb.answer(msg_txt, show_alert=True)
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:qlcdepth")
async def smm_qlc_depth_toggle(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ عمقِ مسیرِ خرید بینِ دو و سه مرحله."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "qlc_depth", "3")
    new = "2" if cur == "3" else "3"
    await _set_setting(row.id, "qlc_depth", new)
    await cb.answer("مسیرِ خرید: " +
                    ("دسته ← سرویس" if new == "2" else "دسته ← نوع ← سرویس"))
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:togordermode")
async def smm_togordermode(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_SMM_ORDERMODE_SYNC_V1 ===
    # باگِ گزارششده: این دکمه فقط کلیدِ عمومیِ smm_order_mode را عوض
    # می‌کرد، ولی موقعِ ثبتِ سفارش _panel_order_mode ستونِ خودِ پنل را
    # می‌خواند. نتیجه: ادمین «خودکار» می‌دید ولی سفارش‌ها به صفِ تأییدِ
    # دستی می‌رفتند. حالا هر دو با هم عوض می‌شوند.
    cur = await _get_setting(row.id, "smm_order_mode", "manual")
    new = "auto" if cur == "manual" else "manual"
    await _set_setting(row.id, "smm_order_mode", new)
    try:
        async with Session() as _s:
            _panels = (await _s.scalars(select(SmmPanel).where(
                SmmPanel.bot_id == row.id))).all()
            for _p in _panels:
                _p.order_mode = new
            if _panels:
                await _s.commit()
    except Exception:
        logger.exception("[SMM] همگام‌سازیِ order_mode روی پنل‌ها ناموفق بود")
    await cb.answer("حالت ثبت سفارش عوض شد")
    await smm_menu(cb, bot)


# === PATCH_SMM_FAIL_RETRY_V1 ===
@smm_router.callback_query(F.data == "smm:togfailpolicy")
async def smm_togfailpolicy(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = await _get_setting(row.id, "smm_fail_policy", "auto_refund")
    new = "manual_review" if cur == "auto_refund" else "auto_refund"
    await _set_setting(row.id, "smm_fail_policy", new)
    msg = ("از این به بعد وقتی پنل خطا بده، وجه فوراً برنمی‌گرده — سفارش تویِ "
           "«⚠️ سفارشاتِ خطا خورده» می‌مونه تا دستی دوباره بفرستی یا وجهش رو برگردونی."
           if new == "manual_review" else
           "برگشت به حالتِ قبلی: وقتی پنل خطا بده، وجه فوراً و خودکار برمی‌گرده.")
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    # قبلاً اینجا هم یه toast می‌زد هم یه پیامِ جداگانه می‌فرستاد (msg) و
    # بعدش صفحه رو ادیت می‌کرد — یعنی هر بار زدنِ این دکمه یه پیامِ اضافه‌ی
    # ولگرد تویِ چت باقی می‌ذاشت. الان توضیح می‌ره تو خودِ popup (alert)،
    # نه یه پیامِ جدا.
    await cb.answer(msg, show_alert=True)
    await smm_menu(cb, bot)


@smm_router.callback_query(F.data == "smm:setmargin")
async def smm_setmargin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmFSM.margin_pct)
    await cb.message.answer(
        "📈 درصد سود را بفرست (فقط عدد، مثلاً 30):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data="smm:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.margin_pct, F.text)
async def smm_margin_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    raw = _norm_num(msg.text.replace("٪", "").replace("%", ""))
    try:
        val = float(raw)
    except ValueError:
        await msg.answer("فقط عدد بفرست. مثال: 30")
        return
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    data = await state.get_data()
    raw_pid = data.get("smm_settings_panel_pk")
    await state.clear()
    if raw_pid is not None:
        panel_pk = None if raw_pid == "0" else int(raw_pid)
        await _panel_set_setting(row.id, panel_pk, "margin_pct", val)
        text, kb = await _panel_settings_content(row.id, panel_pk)
    else:
        await _set_setting(row.id, "smm_margin_pct", str(val))
        text, kb = await smm_admin_content(row.id)
    await msg.answer("✅ درصد سود ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "smm:setrate")
async def smm_setrate(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmFSM.toman_rate)
    await cb.message.answer(
        "💱 نرخ تبدیل هر واحدِ پولِ پنل به تومان را بفرست (فقط عدد).\n\n"
        "مثال: اگر قیمت‌های پنل به دلار است و هر دلار ۷۰٬۰۰۰ تومان، عدد "
        "<code>70000</code> را بفرست.\n"
        "اگر قیمت‌های پنل خودش به تومان است، عدد <code>1</code> را بفرست.\n\n"
        "⚠️ اگر «نرخِ تبدیل» را روی خودکار گذاشته‌ای، همین عددِ دستی فقط "
        "زمانی استفاده می‌شود که دریافتِ نرخِ زنده ناموفق باشد (پشتیبان).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data="smm:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.toman_rate, F.text)
async def smm_rate_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    raw = _norm_num(msg.text)
    try:
        val = float(raw)
    except ValueError:
        await msg.answer("فقط عدد بفرست. مثال: 70000")
        return
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    data = await state.get_data()
    raw_pid = data.get("smm_settings_panel_pk")
    await state.clear()
    if raw_pid is not None:
        panel_pk = None if raw_pid == "0" else int(raw_pid)
        await _panel_set_setting(row.id, panel_pk, "toman_per_unit", val)
        text, kb = await _panel_settings_content(row.id, panel_pk)
    else:
        await _set_setting(row.id, "smm_toman_per_unit", str(val))
        text, kb = await smm_admin_content(row.id)
    await msg.answer("✅ نرخ تبدیل ثبت شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ سفارش‌های ادمین ═══════════════════════

async def smm_orders_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    await ensure_smm_columns()
    async with Session() as s:
        pending = (await s.scalars(select(SmmOrder).where(
            SmmOrder.bot_id == bot_id, SmmOrder.status == "awaiting_admin")
            .order_by(SmmOrder.id.desc()).limit(20))).all()
        recent = (await s.scalars(select(SmmOrder).where(
            SmmOrder.bot_id == bot_id,
            SmmOrder.status.in_(["sent", "completed", "failed", "refunded", "canceled", "partial"]))  # PATCH_SMM_PARTIAL_V1
            .order_by(SmmOrder.id.desc()).limit(10))).all()
    lines = ["📋 <b>سفارش‌های SMM</b>\n"]
    rows = []
    if pending:
        lines.append(f"⏳ <b>منتظر تأیید ({len(pending)}):</b>")
        for o in pending:
            lines.append(
                f"• #{o.tracking_code or o.id} — {o.service_name or o.service_id} × {o.quantity}\n"
                f"  🔗 {o.link[:60]}\n  💰 {_rial(o.price_toman)} — کاربر <code>{o.user_telegram_id}</code>")
            rows.append([
                InlineKeyboardButton(text=f"✅ ثبت #{o.tracking_code or o.id}", callback_data=f"smm:approve:{o.id}"),
                InlineKeyboardButton(text=f"❌ رد #{o.tracking_code or o.id}", callback_data=f"smm:reject:{o.id}"),
            ])
    if recent:
        lines.append("\n📌 <b>اخیر:</b>")
        _st = {"sent": "🚀 ثبت‌شده", "completed": "✅ تکمیل", "failed": "⚠️ خطا",
               "refunded": "↩️ بازگشت وجه", "canceled": "❌ لغو"}
        for o in recent:
            lines.append(f"• #{o.tracking_code or o.id} — {o.service_name or o.service_id} × {o.quantity} — "
                         f"{_st.get(o.status, o.status)}")
    if not pending and not recent:
        lines.append("هنوز سفارشی ثبت نشده است.")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:orders")
async def smm_orders(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await cb.answer()
    text, kb = await smm_orders_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_VS_ORDERS_SUBMENU_V1 ===
# قبلاً دکمهٔ «مشاهده سفارشات» مستقیم لیستِ خامِ سفارش‌ها (منتظرِ تأیید +
# ۱۰ تای اخیر، همه قاطی) رو نشون می‌داد. حالا طبقِ خواستهٔ کاربر یه زیرمنوی
# سه‌گزینه‌ای می‌سازیم: «لیست سفارشات» (همه، صفحه‌بندی‌شده)، «سفارشات خطا
# خورده» (فقط status=failed)، و «جستجوی سفارش» (پیدا کردن با شناسهٔ عددی).

_VS_PAGE_SIZE = 15

# === PATCH_SMM_CUSTOM_TEXTS_WIRE_V1 ===
async def _smm_txt(bot_id: int, key: str) -> str:
    """متنِ اطلاع‌رسانیِ وضعیتِ سفارش — دلخواهِ ادمین یا پیش‌فرض."""
    try:
        from tenant.bots.virtual_service.smm_texts import get_text
        return await get_text(bot_id, key)
    except Exception:
        logger.exception("[SMM] خواندنِ متنِ سفارش ناموفق بود")
        return ""


# === PATCH_SMM_ORDER_PLACED_TEXT_V1 ===
async def _smm_custom_txt(bot_id: int, key: str) -> str:
    """متنِ دلخواهِ ادمین (بدونِ پیش‌فرض)؛ خالی یعنی تنظیم نشده."""
    try:
        from tenant.bots.virtual_service.smm_texts import get_custom
        return await get_custom(bot_id, key)
    except Exception:
        logger.exception("[SMM] خواندنِ متنِ دلخواه ناموفق بود")
        return ""


async def _render_order_text(bot_id: int, order_pk: int, text: str) -> str:
    """متغیرهایِ {{product_name}} ... را برایِ یک سفارش جایگزین می‌کند؛ در خطا متنِ خام برمی‌گردد."""
    try:
        from tenant.shared.text_vars import render_text
        async with Session() as s:
            o = await s.get(SmmOrder, order_pk)
            bu = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == o.user_telegram_id)) if o else None
        return await render_text(text, bot_id=bot_id, user=bu, order=o)
    except Exception:
        logger.exception("[SMM] رندرِ متغیرهایِ متنِ ثبتِ سفارش ناموفق بود (متنِ خام)")
        return text


async def _answer_html_safe(msg, text: str, **kw) -> None:
    """پیام را با HTML می‌فرستد؛ اگر تلگرام به‌خاطرِ تگِ ناقصِ متنِ دلخواه رد کرد، ساده می‌فرستد
    (خبرِ ثبتِ سفارش به مشتری نباید به‌خاطرِ یک تگِ اشتباه گم شود)."""
    try:
        await msg.answer(text, parse_mode="HTML", **kw)
    except Exception as e:  # noqa: BLE001
        if "parse" not in str(e).lower() and "entit" not in str(e).lower() and "tag" not in str(e).lower():
            raise
        logger.warning("[SMM] متنِ ثبتِ سفارش HTML معتبر نبود؛ بدونِ قالب فرستاده شد (%s)", e)
        await msg.answer(text, parse_mode=None, **kw)


def _customer_fail_reason(note: str) -> str:
    """علتی که به *مشتری* نشان داده می‌شود. خطاهایِ فنی/شبکه‌ایِ پنل (HTTP 500، قطعیِ ارتباط، ...) مالِ
    ادمین است، نه مشتری؛ متنی که خودِ پنل دربارهٔ سفارش نوشته (مثل «لینک نامعتبر») همان‌طور می‌ماند."""
    n = (note or "").strip()
    if not n or n.startswith(smm_api.TRANSPORT_ERR_PREFIXES):
        return "مشکلِ موقت"
    # === PATCH_USER_ERRORS_GENERIC_V1 === خطاهایی که از اطلاعاتِ داخلیِ پنل/ارائه‌دهنده می‌گویند
    # (موجودی/کلید/API/آدرس) به مشتری نشان داده نمی‌شود؛ ادمین جدا خبر می‌گیرد.
    _l = n.lower()
    if any(t in _l for t in ("balance", "fund", "credit", "api", "key", "token", "http", "host", "ssl",
                             "موجودی", "اعتبار", "کلید", "توکن", "آدرس")) or len(n) > 120:
        return "مشکلِ موقت"
    return n


_VS_STATUS_LABELS = {
    "sending": "📤 در حالِ ارسال به پنل",   # PATCH_SMM_PLACE_CLAIM_V1 (وضعیتِ گذرا)
    "awaiting_admin": "⏳ منتظر تأیید",
    "sent": "🚀 ثبت‌شده",
    "completed": "✅ تکمیل",
    "failed": "⚠️ خطا",
    "refunded": "↩️ بازگشت وجه",
    "canceled": "❌ لغو شده",
    "partial": "🟠 نیمه‌تمام",   # === PATCH_SMM_PARTIAL_V1 ===
}


async def smm_vs_menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """زیرمنوی «مشاهده سفارشات»."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 لیست سفارشات", callback_data="smm:vs:list:1", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⚠️ سفارشات خطا خورده", callback_data="smm:vs:failed:1", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔍 جستجوی سفارش", callback_data="smm:vs:search", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)],
    ])
    return "🗂 <b>مشاهده سفارشات</b>\n\nیکی از گزینه‌های زیر را انتخاب کن:", kb


# === PATCH_VS_UNIFIED_ORDERS_V1 ===
# گزارشِ ادمین (فدایی): «لیست سفارشات» فقط سفارش‌هایِ SMM را نشان می‌داد و سفارش‌هایِ
# استارز/گیفت/پریمیوم/TON/ری‌اکشن (GiftOrder) و NFT اصلاً در آن نبود؛ باید جایِ دیگری
# (منویِ گیفت) دنبالشان می‌گشت. حالا همهٔ انواعِ سفارش با هم و به ترتیبِ زمان در یک
# لیست می‌آیند. کدِ پیگیری بینِ انواع مشترک است، پس جستجو هم روی همه کار می‌کند.

_VS_GIFT_STATUS_LABELS = {
    "pending": "⏳ در انتظار", "sent": "✅ ارسال شد", "sending": "📤 در حالِ ارسال",
    "awaiting_stars": "⏳ منتظرِ موجودی", "auto_processing": "🤖 خریدِ خودکار",
    "failed": "⚠️ خطا", "refunded": "↩️ بازگشت وجه", "awaiting_receipt": "🧾 منتظرِ رسید",
    "awaiting_admin": "⏳ منتظر تأیید", "rejected": "❌ رد شده",
    "completed": "✅ تکمیل", "done": "✅ تکمیل", "canceled": "❌ لغو شده",
}
_VS_NFT_STATUS_LABELS = {
    "pending": "⏳ در انتظار", "paid": "💳 پرداخت‌شده", "delivered": "✅ تحویل شد",
    "returned": "↩️ برگشت", "rejected": "❌ رد شده", "expired": "⌛ منقضی",
}
_VS_DONE_STATUSES = {"completed", "sent", "done", "delivered"}
_VS_OPEN_STATUSES = {"sent", "awaiting_admin", "pending", "paid", "sending", "auto_processing",
                     "awaiting_stars", "awaiting_receipt", "partial"}


def _vs_gift_item_label(o) -> str:
    """برچسبِ کوتاهِ نوعِ سفارشِ GiftOrder برایِ ستونِ وسط."""
    gid = (o.gift_id or "")
    if gid.startswith("premium_"):
        item = f"🌟 پریمیوم {gid.split('_', 1)[1]} ماهه"
    elif gid.startswith("starsell_"):
        item = f"⭐ {o.star_count or 0:,} استارز"
    elif gid.startswith("srx_"):
        item = f"⭐ ری‌اکشن {o.star_count or 0:,}"
    elif gid.startswith("tonsell_"):
        item = f"💎 {(o.star_count or 0) / 100:g} TON"
    elif gid.startswith("manual"):
        item = "🔗 گیفتِ سفارشی"
    else:
        item = f"{o.gift_emoji or '🎁'} گیفت {o.star_count or 0}⭐"
    if (o.quantity or 1) > 1:
        item += f" ×{o.quantity}"
    return item


_VS_ROW_DONE = {"completed", "done", "delivered"}
_VS_ROW_BAD = {"failed", "canceled", "cancelled", "refunded", "rejected", "expired", "returned"}


def _vs_row_style(status: str, typ: str = "s"):
    """رنگِ ردیفِ لیستِ سفارشات (PATCH_ORDER_LIST_COLORS_V2): تکمیل سبز، نیمه‌تمام آبی،
    لغو/خطا/برگشت قرمز، و «در حالِ انجام» (ثبت‌شده، منتظر تأیید، در حالِ ارسال، …) بدونِ رنگ.
    «sent» برایِ سفارشِ SMM یعنی در حالِ انجام، ولی برایِ گیفت/استارز یعنی ارسال‌شده (تکمیل)."""
    st = (status or "").lower()
    if st in _VS_ROW_DONE or (st == "sent" and typ == "g"):
        return ButtonStyle.SUCCESS
    if st == "partial":
        return ButtonStyle.PRIMARY
    if st in _VS_ROW_BAD:
        return ButtonStyle.DANGER
    return None


async def _vs_unified_page(bot_id: int, page: int, only_failed: bool):
    """(total, page, total_pages, items) — items لیستی از (نوع، شیء) با نوع‌هایِ s/g/n."""
    need = max(1, page) * _VS_PAGE_SIZE
    async with Session() as s:
        smm_q = select(SmmOrder).where(SmmOrder.bot_id == bot_id)
        gift_q = select(GiftOrder).where(GiftOrder.bot_id == bot_id)
        nft_q = select(NftOrder).where(NftOrder.bot_id == bot_id)
        if only_failed:
            smm_q = smm_q.where(SmmOrder.status == "failed")
            gift_q = gift_q.where(GiftOrder.status == "failed")
            nft_q = nft_q.where(NftOrder.status == "rejected")
        total = 0
        for q in (smm_q, gift_q, nft_q):
            total += int(await s.scalar(select(func.count()).select_from(q.subquery())) or 0)
        smm_rows = (await s.scalars(smm_q.order_by(SmmOrder.created_at.desc(), SmmOrder.id.desc()).limit(need))).all()
        gift_rows = (await s.scalars(gift_q.order_by(GiftOrder.created_at.desc(), GiftOrder.id.desc()).limit(need))).all()
        nft_rows = (await s.scalars(nft_q.order_by(NftOrder.created_at.desc(), NftOrder.id.desc()).limit(need))).all()
    merged = ([("s", o) for o in smm_rows] + [("g", o) for o in gift_rows] + [("n", o) for o in nft_rows])
    merged.sort(key=lambda t: (t[1].created_at or datetime.min, t[1].id), reverse=True)
    total_pages = max(1, (total + _VS_PAGE_SIZE - 1) // _VS_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    items = merged[(page - 1) * _VS_PAGE_SIZE: page * _VS_PAGE_SIZE]
    return total, page, total_pages, items


async def _smm_vs_paginated_content(bot_id: int, page: int, *, only_failed: bool,
                                    kind: str) -> tuple[str, InlineKeyboardMarkup]:
    """محتوایِ صفحه‌بندی‌شدهٔ همهٔ سفارش‌ها (SMM + استارز/گیفت/پریمیوم/TON + NFT).
    kind فقط برایِ ساختِ callback_dataهای صفحه‌بندی/جزئیات است (\"list\" یا \"failed\")."""
    total, page, total_pages, items = await _vs_unified_page(bot_id, page, only_failed)

    title = "⚠️ سفارشاتِ خطا خورده" if only_failed else "🗂 لیستِ سفارشاتِ فروشگاه"
    lines = [f"{title} (صفحه {page} از {total_pages}):", "هر ردیف: کد پیگیری | نوع/لینک | وضعیت"]
    kb_rows = [[
        InlineKeyboardButton(text="≡ کد پیگیری ≡", callback_data="noop"),
        InlineKeyboardButton(text="≡ لینک ≡", callback_data="noop"),
        InlineKeyboardButton(text="≡ وضعیت ≡", callback_data="noop"),
    ]]
    if not items:
        lines.append("\nسفارشی پیدا نشد.")
    for typ, o in items:
        if typ == "s":
            status_label = _VS_STATUS_LABELS.get(o.status, o.status)
            mid = (o.link or "—")
            code = str(o.tracking_code or o.id)
            cb_view = f"smm:vs:view:{kind}:{page}:{o.id}"
        elif typ == "g":
            status_label = _VS_GIFT_STATUS_LABELS.get(o.status, o.status)
            mid = _vs_gift_item_label(o)
            code = str(o.tracking_code or f"G{o.id}")
            cb_view = f"smm:vs:gview:{kind}:{page}:{o.id}"
        else:
            status_label = _VS_NFT_STATUS_LABELS.get(o.status, o.status)
            mid = "🖼 NFT " + ("اجاره" if o.kind == "rent" else "خرید")
            code = f"N{o.id}"
            cb_view = f"smm:vs:nview:{kind}:{page}:{o.id}"
        if len(mid) > 22:
            mid = mid[:19] + "…"
        row_style = _vs_row_style(o.status, typ)
        kb_rows.append([
            InlineKeyboardButton(text=code, callback_data=cb_view, style=row_style),
            InlineKeyboardButton(text=mid, callback_data=cb_view, style=row_style),
            InlineKeyboardButton(text=status_label, callback_data=cb_view, style=row_style),
        ])
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"smm:vs:{kind}:{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"smm:vs:{kind}:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)
    # === PATCH_SMM_BULK_ACTIONS_V1 === (عملیاتِ گروهی فقط رویِ خطاهایِ SMM است)
    if only_failed and any(t == "s" for t, _ in items):
        kb_rows.append([InlineKeyboardButton(text="⚙️ عملیاتِ گروهی رویِ همهٔ خطاخورده‌ها",
                                             callback_data="smm:vs:bulkmenu", style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:vs:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _vs_gift_detail_content(bot_id: int, order_id: int,
                                  back_cb: str) -> tuple[str, InlineKeyboardMarkup]:
    """جزئیاتِ سفارشِ استارز/گیفت/پریمیوم/TON/ری‌اکشن + دکمه‌هایِ همان اقدام‌هایی که ادمین از پیامِ سفارش داشت."""
    from tenant import _jalali_date_time
    back_btn = [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)]
    async with Session() as s:
        o = await s.scalar(select(GiftOrder).where(GiftOrder.id == order_id, GiftOrder.bot_id == bot_id))
        buyer = await s.get(BotUser, o.buyer_user_id) if o else None
    if not o:
        return "❌ سفارشی با این شناسه برای این ربات پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[back_btn])
    d, t = _jalali_date_time(o.created_at)
    qty = int(o.quantity or 1)
    total_price = float(o.price_toman or 0) * qty
    uname = f"@{buyer.username}" if (buyer and buyer.username) else "—"
    lines = [
        f"🔴 <b>وضعیت سفارش:</b> {_VS_GIFT_STATUS_LABELS.get(o.status, o.status)}",
        "", "📋 <b>مشخصات سفارش:</b>", "<code>",
        f"≡ شناسه سفارش : {o.tracking_code or o.id}",
        f"≡ نوع : {_vs_gift_item_label(o)}",
        f"≡ شناسه کاربر : {buyer.telegram_id if buyer else '—'}",
        f"≡ یوزرنیم : {uname}",
        f"≡ گیرنده : {o.dest_label or '—'}",
        f"≡ تعداد : {qty}",
        f"≡ قیمت سفارش : {_rial(total_price)}",
        f"≡ روش پرداخت : {o.pay_method or 'wallet'}",
    ]
    if getattr(o, "sent_qty", 0):
        lines.append(f"≡ ارسال‌شده : {o.sent_qty} از {qty}")
    if getattr(o, "discount_amount", 0):
        lines.append(f"≡ تخفیف : {_rial(o.discount_amount)}")
    lines.append("</code>")
    if o.fail_reason:
        lines.append(f"\n📝 یادداشت: {_html_escape(str(o.fail_reason))[:300]}")
    lines.append(f"\n🗓 تاریخ ثبت: {d} {t}")

    gid = o.gift_id or ""
    rows = []
    st = o.status
    if gid.startswith("premium_") and st in ("awaiting_admin", "pending"):
        rows.append([InlineKeyboardButton(text="✅ انجام شد", callback_data=f"prm:done:{o.id}", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="❌ رد", callback_data=f"prm:reject:{o.id}", style=ButtonStyle.DANGER)])
    elif gid.startswith("starsell_") and st in ("awaiting_admin", "pending"):
        rows.append([InlineKeyboardButton(text="✅ انجام شد", callback_data=f"star:done:{o.id}", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="❌ رد", callback_data=f"star:reject:{o.id}", style=ButtonStyle.DANGER)])
    elif gid.startswith("srx_") and st in ("awaiting_admin", "pending"):
        rows.append([InlineKeyboardButton(text="✅ انجام شد", callback_data=f"srx:ok:{o.id}", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="❌ رد", callback_data=f"srx:no:{o.id}", style=ButtonStyle.DANGER)])
    elif gid.startswith("tonsell_") and st in ("awaiting_admin", "pending"):
        rows.append([InlineKeyboardButton(text="✅ ارسال شد", callback_data=f"ton:done:{o.id}", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="↩️ رد و بازگشتِ وجه", callback_data=f"ton:refund:{o.id}", style=ButtonStyle.DANGER)])
    elif st == "awaiting_stars":
        rows.append([InlineKeyboardButton(text="🔄 ارسال (بعد از شارژ)", callback_data=f"gift:retry:{o.id}", style=ButtonStyle.PRIMARY),
                     InlineKeyboardButton(text="❌ رد و بازگشتِ وجه", callback_data=f"gift:cardno:{o.id}", style=ButtonStyle.DANGER)])
    elif st in ("awaiting_admin",) and not gid.startswith(("premium_", "starsell_", "srx_", "tonsell_")):
        rows.append([InlineKeyboardButton(text="✅ انجام شد", callback_data=f"gift:cardok:{o.id}", style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text="❌ رد", callback_data=f"gift:cardno:{o.id}", style=ButtonStyle.DANGER)])
    rows.append(back_btn)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _vs_nft_detail_content(bot_id: int, order_id: int,
                                 back_cb: str) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import _jalali_date_time
    back_btn = [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)]
    async with Session() as s:
        o = await s.scalar(select(NftOrder).where(NftOrder.id == order_id, NftOrder.bot_id == bot_id))
        item = await s.get(NftItem, o.item_id) if o else None
        buyer = await s.get(BotUser, o.user_id) if o else None
    if not o:
        return "❌ سفارشی با این شناسه برای این ربات پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[back_btn])
    d, t = _jalali_date_time(o.created_at)
    lines = [
        f"🔴 <b>وضعیت سفارش:</b> {_VS_NFT_STATUS_LABELS.get(o.status, o.status)}",
        "", "📋 <b>مشخصات سفارش:</b>", "<code>",
        f"≡ شناسه سفارش : N{o.id}",
        f"≡ نوع : NFT {'اجاره' if o.kind == 'rent' else 'خرید'}",
        f"≡ آیتم : {_html_escape(item.title) if item else '—'}",
        f"≡ شناسه کاربر : {buyer.telegram_id if buyer else '—'}",
        f"≡ یوزرنیم : @{buyer.username}" if (buyer and buyer.username) else "≡ یوزرنیم : —",
    ]
    if o.kind == "rent":
        lines.append(f"≡ مدت : {o.days} روز")
    lines += [f"≡ مبلغ : {_rial(o.amount)}", f"≡ تحویل : {'خودکار' if o.transfer_mode == 'auto' else 'دستی'}", "</code>"]
    if o.transfer_note:
        lines.append(f"\n📝 یادداشت: {_html_escape(str(o.transfer_note))[:300]}")
    lines.append(f"\n🗓 تاریخ ثبت: {d} {t}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[back_btn])


async def _smm_vs_order_detail_content(bot_id: int, order_id: int,
                                       back_cb: str) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import _jalali_date_time
    async with Session() as s:
        o = await s.scalar(select(SmmOrder).where(SmmOrder.id == order_id, SmmOrder.bot_id == bot_id))
        u = None
        svc = None
        if o:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == o.user_telegram_id))
            if o.smm_service_pk:
                svc = await s.get(SmmService, o.smm_service_pk)
    if not o:
        text = "❌ سفارشی با این شناسه برای این ربات پیدا نشد."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)],
        ])
        return text, kb

    status_label = _VS_STATUS_LABELS.get(o.status, o.status)
    is_manual = bool(svc and getattr(svc, "is_manual", False)) or str(o.service_id).startswith("man")
    if is_manual:
        kind_label = "✍️ دستی"
    else:
        # === PATCH_SMM_MULTI_PANEL_V1 ===
        _, _, panel_name = await _panel_creds(bot_id, o.panel_pk)
        kind_label = f"🛰 API ({panel_name})"
    username_line = f"@{u.username}" if (u and u.username) else "—"
    created_date, created_time = _jalali_date_time(o.created_at)
    updated_date, updated_time = _jalali_date_time(o.updated_at) if o.updated_at else (created_date, created_time)

    lines = [
        f"🔴 <b>وضعیت سفارش:</b> {status_label}",
        "",
        "📋 <b>مشخصات سفارش:</b>",
        "<code>",
        f"≡ شناسه سفارش : {o.tracking_code or o.id}",
        f"≡ شناسه کاربر : {o.user_telegram_id}",
        f"≡ یوزرنیم : {username_line}",
        f"≡ شناسه سرویس : {o.service_id}",
        f"≡ شناسه در ارائه‌دهنده : {o.panel_order_id or '—'}",
        f"≡ نوع سرویس : {kind_label}",
        f"≡ سرویس : {o.service_name or '—'}",
        f"≡ تعداد : {o.quantity}",
    ]
    if o.remaining is not None:
        lines.append(f"≡ باقیمانده : {o.remaining}")
    lines.append(f"≡ قیمت سفارش : {_rial(o.price_toman)}")
    # === PATCH_SMM_REFILL_V1 ===
    try:
        from tenant.bots.virtual_service import smm_refill as _rfa
        _lr = await _rfa.latest(o.id)
        if _lr is not None and _lr.status != "requesting":
            lines.append(f"≡ ریفیل : {_rfa.STATUS_LABEL.get(_lr.status, _lr.status)}"
                         + (f" (کد {_lr.panel_refill_id})" if _lr.panel_refill_id else ""))
    except Exception:
        logger.exception("[refill] خطِ وضعیتِ ریفیل در جزئیاتِ ادمین ناموفق بود")
    if getattr(o, "refunded_toman", None):   # === PATCH_SMM_PARTIAL_V1 ===
        lines.append(f"≡ مبلغ بازگشتی : {_rial(o.refunded_toman)}")
        lines.append(f"≡ مبلغ نهایی : {_rial(max(0.0, float(o.price_toman or 0) - float(o.refunded_toman or 0)))}")
    if o.cost_toman is not None:
        profit = float(o.price_toman or 0) - float(o.cost_toman or 0)
        lines.append(f"≡ هزینه : {_rial(o.cost_toman)}")
        lines.append(f"≡ سود : {_rial(profit)}")
    lines.append(f"≡ لینک : {o.link or '—'}")
    lines.append("</code>")
    if o.note:
        lines.append(f"\n📝 یادداشت: {o.note}")
    lines.append(f"\n🗓 تاریخ ثبت: {created_date} {created_time}")
    lines.append(f"📊 آخرین بروزرسانی: {updated_date} {updated_time}")
    text = "\n".join(lines)

    kb_rows = []
    # === PATCH_SMM_FAIL_RETRY_V1 ===
    if o.status == "failed":
        # === PATCH_SMM_NAV_FIX_V1 ===
        # back_cb می‌تونه «smm:vs:failed:2» یا «smm:vs:menu» و غیره باشه؛
        # چون کاراکترِ «:» تو خودِ callback_data برایِ جداکردنِ بخش‌ها
        # استفاده می‌شه، این‌جا با «~» انکود می‌کنیم تا بعدِ ارسالِ دوباره/
        # بازگشتِ وجه بشه دقیقاً به همون صفحه‌ای که ادمین ازش اومده برگشت
        # (نه همیشه به منویِ کلی).
        back_enc = back_cb.replace(":", "~")
        kb_rows.append([InlineKeyboardButton(text="🔁 ارسالِ دوباره به پنل", callback_data=f"smm:vs:retry:{o.id}:{back_enc}", style=ButtonStyle.SUCCESS)])
        # === PATCH_SMM_MANUAL_COMPLETE_V1 ===
        # گزارشِ ادمین: وقتی سفارش دستی (خارج از API) توسطِ خودِ ادمین انجام
        # می‌شه، باید بشه بدونِ برگردوندنِ وجه به کاربر، سفارش رو «تکمیل»
        # علامت زد — قبلاً فقط «ارسالِ دوباره» و «بازگشتِ وجه» بود، هیچ راهی
        # برایِ «انجام دادم، وجه رو نگه دار» نبود.
        kb_rows.append([InlineKeyboardButton(text="✅ تکمیلِ سفارش (بدونِ بازگشتِ وجه)", callback_data=f"smm:vs:complete:{o.id}:{back_enc}", style=ButtonStyle.SUCCESS)])
        kb_rows.append([InlineKeyboardButton(text="↩️ بازگشتِ دستیِ وجه به کاربر", callback_data=f"smm:vs:refund:{o.id}:{back_enc}", style=ButtonStyle.DANGER)])
    # === PATCH_SMM_PARTIAL_V1 ===
    # درخواستِ ادمین (فدایی): «سفارشا رو دستی بشه تغییر وضعیت داد». برایِ سفارش‌هایِ
    # در جریان/خطاخورده (sent / awaiting_admin / failed) — هر تغییرِ مالی تأییدِ دوباره دارد.
    if o.status in ("sent", "awaiting_admin", "failed"):
        _enc = back_cb.replace(":", "~")
        if o.status == "awaiting_admin" and is_manual:
            kb_rows.append([InlineKeyboardButton(
                text="🚀 در حال انجام", callback_data=f"smm:vs:st:run:{o.id}:{_enc}", style=ButtonStyle.PRIMARY)])
        if o.status in ("sent", "awaiting_admin"):
            kb_rows.append([
                InlineKeyboardButton(text="✅ تکمیل سفارش", callback_data=f"smm:vs:st:done:{o.id}:{_enc}",
                                     style=ButtonStyle.SUCCESS),
                InlineKeyboardButton(text="❌ لغو و بازگشت وجه", callback_data=f"smm:vs:st:cancel:{o.id}:{_enc}",
                                     style=ButtonStyle.DANGER),
            ])
        kb_rows.append([InlineKeyboardButton(
            text="🟠 نیمه‌تمام (ثبتِ مقدارِ تحویل‌نشده)", callback_data=f"smm:vs:st:part:{o.id}:{_enc}",
            style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    return text, kb


async def _smm_vs_edit_or_send(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_FAIL_RETRY_V1 ===
@smm_router.callback_query(F.data.startswith("smm:vs:retry:"))
async def smm_vs_retry(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_SMM_NAV_FIX_V1 ===
    parts = cb.data.split(":")
    oid = int(parts[3])
    back_cb = parts[4].replace("~", ":") if len(parts) > 4 else "smm:vs:menu"
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        if not o or o.bot_id != row.id or o.status != "failed":
            await cb.answer("این سفارش دیگه قابلِ ارسالِ دوباره نیست.", show_alert=True)
            return
        o.status = "awaiting_admin"
        await s.commit()
    await cb.answer("در حالِ ارسالِ دوباره به پنل...")
    ok, note = await _place_on_panel(row.id, oid)
    await cb.message.answer("✅ این‌بار ثبت شد." if ok else f"❌ بازم ناموفق بود: {note}")
    # === PATCH_SMM_NOTIFY_GAPS_V1 ===
    if ok:
        await _notify_user(row.id, oid, (await _smm_txt(row.id, "sent")), bot=bot)
    text, kb = await _smm_vs_order_detail_content(row.id, oid, back_cb)
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    # قبلاً همیشه پیامِ تازه می‌فرستاد؛ هم‌خانواده‌اش (smm_vs_refund، چند
    # خط پایین‌تر) درست ادیت می‌کرد. الان همون الگو رو داره — سریع‌تره و
    # چت رو با پیام‌های تکراری شلوغ نمی‌کنه.
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:vs:refund:"))
async def smm_vs_refund(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_SMM_NAV_FIX_V1 ===
    parts = cb.data.split(":")
    oid = int(parts[3])
    back_cb = parts[4].replace("~", ":") if len(parts) > 4 else "smm:vs:menu"
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        if not o or o.bot_id != row.id or o.status != "failed":
            await cb.answer("این سفارش دیگه قابلِ بازگشتِ وجه نیست.", show_alert=True)
            return
    ok = await _refund_order(row.id, oid, reason="بازگشتِ دستیِ وجه توسطِ ادمین",
                             notify_text=(await _smm_txt(row.id, "refunded")), bot=bot)
    await cb.answer("✅ وجه برگشت." if ok else "❌ ناموفق بود.", show_alert=True)
    text, kb = await _smm_vs_order_detail_content(row.id, oid, back_cb)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_MANUAL_COMPLETE_V1 ===
@smm_router.callback_query(F.data.startswith("smm:vs:complete:"))
async def smm_vs_complete(cb: CallbackQuery, bot: Bot):
    """تکمیلِ دستیِ یک سفارشِ خطاخورده — بدونِ بازگشتِ وجه (ادمین خودش خارج
    از API انجامش داده)."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    oid = int(parts[3])
    back_cb = parts[4].replace("~", ":") if len(parts) > 4 else "smm:vs:menu"
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        if not o or o.bot_id != row.id or o.status != "failed":
            await cb.answer("این سفارش دیگه قابلِ تکمیل‌کردن نیست.", show_alert=True)
            return
        o.status = "completed"
        o.updated_at = datetime.utcnow()
        o.note = (o.note or "") + " | تکمیلِ دستی توسطِ ادمین (بدونِ بازگشتِ وجه)"
        await s.commit()
    await cb.answer("✅ سفارش تکمیل شد.", show_alert=True)
    await _notify_user(row.id, oid, (await _smm_txt(row.id, "completed")), bot=bot)
    text, kb = await _smm_vs_order_detail_content(row.id, oid, back_cb)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════ تغییرِ وضعیتِ دستیِ سفارش (ادمین) ═══════════════
# === PATCH_SMM_PARTIAL_V1 ===
# callback: smm:vs:st:<code>:<oid>:<back~encoded>[:<remains>]
#   run     → (فقط سفارشِ دستیِ در انتظار) «در حال انجام»
#   done    → تأیید ← doneok  : تکمیل، بدونِ بازگشتِ وجه
#   cancel  → تأیید ← cancelok: لغو و بازگشتِ کاملِ وجه
#   part    → پرسیدنِ «چند عدد تحویل نشد» ← تأیید ← partok:<remains>
#   back    → برگشت به جزئیاتِ سفارش (و پاک‌کردنِ حالتِ انتظار)

class VsPartialFSM(StatesGroup):
    qty = State()


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _st_confirm_kb(yes_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله، انجام شود", callback_data=yes_cb, style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ انصراف", callback_data=back_cb, style=ButtonStyle.DANGER),
    ]])


async def _vs_show_detail(target, bot_id: int, oid: int, back_cb: str) -> None:
    text, kb = await _smm_vs_order_detail_content(bot_id, oid, back_cb)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:vs:st:"))
async def smm_vs_setstatus(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    try:
        code, oid = parts[3], int(parts[4])
    except (IndexError, ValueError):
        await cb.answer()
        return
    back_enc = parts[5] if len(parts) > 5 else "smm~vs~menu"
    back_cb = back_enc.replace("~", ":")
    back_here = f"smm:vs:st:back:{oid}:{back_enc}"

    if code == "back":
        await state.clear()
        await cb.answer()
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return

    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        svc = await s.get(SmmService, o.smm_service_pk) if (o and o.smm_service_pk) else None
    if not o or o.bot_id != row.id:
        await cb.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    if o.status not in _PARTIAL_FROM_ADMIN:
        await cb.answer("وضعیتِ این سفارش دیگر قابلِ تغییر نیست.", show_alert=True)
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return
    is_manual = bool(svc and getattr(svc, "is_manual", False)) or str(o.service_id).startswith("man")
    qty = int(o.quantity or 0)

    if code == "run":
        if o.status != "awaiting_admin" or not is_manual:
            await cb.answer("این گزینه فقط برایِ سفارشِ دستیِ در انتظار است.", show_alert=True)
            return
        async with Session() as s:
            oo = await s.get(SmmOrder, oid, with_for_update=True)
            if oo is not None and await _is_foreign(oo, bot):   # PATCH_TENANT_SCOPE_V1
                oo = None
            if not oo or oo.status != "awaiting_admin":
                await cb.answer("وضعیتِ سفارش عوض شده.", show_alert=True)
                return
            oo.status = "sent"
            oo.updated_at = datetime.utcnow()
            oo.note = (oo.note or "") + " | در حال انجام (دستی، توسطِ ادمین)"
            await s.commit()
        await cb.answer("🚀 وضعیت: در حال انجام")
        await _notify_user(row.id, oid, (await _smm_txt(row.id, "sent")), bot=bot)
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return

    if code in ("done", "cancel"):
        if code == "done":
            q = (f"✅ سفارشِ <b>#{o.tracking_code or o.id}</b> «تکمیل‌شده» علامت بخورد؟\n\n"
                 "پولِ مشتری برنمی‌گردد و پیامِ «تکمیل» برایش می‌رود.")
        else:
            q = (f"❌ سفارشِ <b>#{o.tracking_code or o.id}</b> لغو و <b>{_rial(o.price_toman)}</b> به کیف‌پولِ مشتری برگردد؟")
            if o.panel_order_id and o.status == "sent":
                q += ("\n\n⚠️ این کار سفارش را <b>روی پنلِ ارائه‌دهنده لغو نمی‌کند</b>؛ "
                      "اگر هنوز آن‌جا در حال اجراست، جدا از پنل لغوش کن.")
        await cb.answer()
        await _smm_vs_edit_or_send(cb, q, _st_confirm_kb(f"smm:vs:st:{code}ok:{oid}:{back_enc}", back_here))
        return

    if code == "doneok":
        async with Session() as s:
            oo = await s.get(SmmOrder, oid, with_for_update=True)
            if oo is not None and await _is_foreign(oo, bot):   # PATCH_TENANT_SCOPE_V1
                oo = None
            if not oo or oo.status not in _PARTIAL_FROM_ADMIN:
                await cb.answer("وضعیتِ سفارش عوض شده.", show_alert=True)
                await _vs_show_detail(cb, row.id, oid, back_cb)
                return
            oo.status = "completed"
            oo.updated_at = datetime.utcnow()
            oo.note = (oo.note or "") + " | تکمیلِ دستی توسطِ ادمین (بدونِ بازگشتِ وجه)"
            await s.commit()
        await cb.answer("✅ سفارش تکمیل شد.", show_alert=True)
        await _notify_user(row.id, oid, (await _smm_txt(row.id, "completed")), bot=bot)
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return

    if code == "cancelok":
        ok = await _refund_order(row.id, oid, reason="لغو و بازگشتِ وجه توسطِ ادمین",
                                 notify_text=(await _smm_txt(row.id, "refunded")), bot=bot)
        await cb.answer("✅ لغو شد و وجه برگشت." if ok else "❌ انجام نشد (وضعیت عوض شده).", show_alert=True)
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return

    if code == "part":
        if qty < 2:
            await cb.answer("تعدادِ این سفارش برایِ نیمه‌تمام‌کردن کم است.", show_alert=True)
            return
        await state.set_state(VsPartialFSM.qty)
        await state.update_data(vsp_oid=oid, vsp_back=back_enc)
        await cb.answer()
        await cb.message.answer(
            f"🟠 <b>نیمه‌تمام‌کردنِ سفارش #{o.tracking_code or o.id}</b>\n\n"
            f"تعدادِ کل: <b>{qty:,}</b> — مبلغ: <b>{_rial(o.price_toman)}</b>\n\n"
            f"چند عدد <b>تحویل داده نشد</b>؟ عددی بین <b>۱</b> و <b>{qty - 1:,}</b> بفرست.\n"
            "سهمِ همان مقدار از مبلغ به کیف‌پولِ مشتری برمی‌گردد.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ انصراف", callback_data=back_here, style=ButtonStyle.DANGER)]]))
        return

    if code == "partok":
        try:
            remains = int(parts[6])
        except (IndexError, ValueError):
            await cb.answer()
            return
        res = await _apply_partial(row.id, oid, remains, source="admin", bot=bot)
        msg = {"partial": "🟠 سفارش نیمه‌تمام شد و مابقیِ مبلغ برگشت.",
               "completed": "✅ چیزی باقی نمانده بود؛ سفارش تکمیل شد.",
               "refunded": "↩️ چیزی تحویل نشده بود؛ کلِ مبلغ برگشت.",
               }.get(res, "❌ انجام نشد (وضعیتِ سفارش عوض شده).")
        await cb.answer(msg, show_alert=True)
        await _vs_show_detail(cb, row.id, oid, back_cb)
        return

    await cb.answer()


@smm_router.message(VsPartialFSM.qty, F.text)
async def smm_vs_partial_qty(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    oid, back_enc = data.get("vsp_oid"), data.get("vsp_back") or "smm~vs~menu"
    if not row or not oid:
        await state.clear()
        return
    raw = (msg.text or "").strip().translate(_FA_DIGITS).replace(",", "").replace("٬", "")
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
    if not o or o.bot_id != row.id or o.status not in _PARTIAL_FROM_ADMIN:
        await state.clear()
        await msg.answer("وضعیتِ این سفارش عوض شده؛ دوباره از جزئیاتِ سفارش شروع کن.")
        return
    qty = int(o.quantity or 0)
    if not raw.isdigit() or not (1 <= int(raw) <= qty - 1):
        await msg.answer(f"❌ عددِ معتبر بفرست: بین ۱ و {qty - 1:,}.\n"
                         "(اگر چیزی باقی نمانده، «تکمیل سفارش»؛ اگر هیچ‌چیز تحویل نشده، «لغو و بازگشت وجه» را بزن.)")
        return
    remains = int(raw)
    await state.clear()
    refund = _partial_refund_amount(o.price_toman, qty, remains)
    await msg.answer(
        f"🟠 <b>تأییدِ نیمه‌تمام — سفارش #{o.tracking_code or o.id}</b>\n\n"
        f"🟢 تحویل‌شده: <b>{qty - remains:,}</b> از {qty:,}\n"
        f"🅱️ تحویل‌نشده: <b>{remains:,}</b>\n"
        f"💰 پرداختی: {_rial(o.price_toman)}\n"
        f"🆕 بازگشتی به کیف‌پولِ مشتری: <b>{_rial(refund)}</b>\n"
        f"🧾 مبلغِ نهایی: <b>{_rial(max(0, int(float(o.price_toman or 0)) - refund))}</b>\n\n"
        "ثبت شود؟", parse_mode="HTML",
        reply_markup=_st_confirm_kb(f"smm:vs:st:partok:{oid}:{back_enc}:{remains}",
                                    f"smm:vs:st:back:{oid}:{back_enc}"))


@smm_router.callback_query(F.data == "smm:vs:menu")
async def smm_vs_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    text, kb = await smm_vs_menu_content(row.id)
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:vs:list:"))
async def smm_vs_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    page = int(cb.data.rsplit(":", 1)[1])
    text, kb = await _smm_vs_paginated_content(row.id, page, only_failed=False, kind="list")
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:vs:failed:"))
async def smm_vs_failed(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    page = int(cb.data.rsplit(":", 1)[1])
    text, kb = await _smm_vs_paginated_content(row.id, page, only_failed=True, kind="failed")
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


# === PATCH_SMM_BULK_ACTIONS_V1 ===
_BULK_LABELS = {
    "complete": "✅ تکمیلِ همه (بدونِ بازگشتِ وجه)",
    "refund":   "↩️ لغوِ همه (با بازگشتِ وجه)",
    "retry":    "🔁 ارسالِ همه به API",
}
_BULK_CONFIRM_TXT = {
    "complete": "همهٔ سفارش‌هایِ خطاخورده بدونِ بازگشتِ وجه «تکمیل» علامت بخورن؟",
    "refund":   "همهٔ سفارش‌هایِ خطاخورده لغو و وجهشون به کیف‌پولِ کاربرها برگرده؟",
    "retry":    "همهٔ سفارش‌هایِ خطاخورده دوباره به پنل ارسال بشن؟",
}


async def _smm_vs_bulkmenu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        cnt = await s.scalar(select(func.count()).select_from(
            select(SmmOrder).where(SmmOrder.bot_id == bot_id, SmmOrder.status == "failed").subquery()))
    cnt = int(cnt or 0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_BULK_LABELS["complete"], callback_data="smm:vs:bulkconfirm:complete", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=_BULK_LABELS["refund"], callback_data="smm:vs:bulkconfirm:refund", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text=_BULK_LABELS["retry"], callback_data="smm:vs:bulkconfirm:retry", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:vs:failed:1", style=ButtonStyle.DANGER)],
    ])
    return (f"⚙️ <b>عملیاتِ گروهی رویِ سفارشاتِ خطاخورده</b>\n\n"
            f"تعدادِ سفارشاتِ خطاخوردهٔ فعلی: <b>{cnt}</b>\n\n"
            "یکی از عملیاتِ زیر رو رویِ <b>همهٔ</b> این سفارش‌ها انجام بده:"), kb


@smm_router.callback_query(F.data == "smm:vs:bulkmenu")
async def smm_vs_bulkmenu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    text, kb = await _smm_vs_bulkmenu_content(row.id)
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:vs:bulkconfirm:"))
async def smm_vs_bulkconfirm(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    action = cb.data.rsplit(":", 1)[1]
    if action not in _BULK_LABELS:
        await cb.answer()
        return
    async with Session() as s:
        cnt = await s.scalar(select(func.count()).select_from(
            select(SmmOrder).where(SmmOrder.bot_id == row.id, SmmOrder.status == "failed").subquery()))
    cnt = int(cnt or 0)
    if cnt == 0:
        await cb.answer("دیگه سفارشِ خطاخورده‌ای نمونده.", show_alert=True)
        text, kb = await _smm_vs_bulkmenu_content(row.id)
        await _smm_vs_edit_or_send(cb, text, kb)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، مطمئنم", callback_data=f"smm:vs:bulkdo:{action}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ بازگشت", callback_data="smm:vs:bulkmenu", style=ButtonStyle.PRIMARY)],
    ])
    await _smm_vs_edit_or_send(
        cb, f"⚠️ <b>تأییدِ نهایی</b>\n\n{_BULK_CONFIRM_TXT[action]}\n\n"
            f"این کار رویِ <b>{cnt}</b> سفارش انجام می‌شه و قابلِ بازگشت نیست.",
        kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:vs:bulkdo:"))
async def smm_vs_bulkdo(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    action = cb.data.rsplit(":", 1)[1]
    if action not in _BULK_LABELS:
        await cb.answer()
        return
    await cb.answer()
    try:
        await cb.message.edit_text("🔄 در حالِ پردازش… ممکنه چند ثانیه طول بکشه.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    async with Session() as s:
        ids = (await s.scalars(select(SmmOrder.id).where(
            SmmOrder.bot_id == row.id, SmmOrder.status == "failed"))).all()

    ok_count = 0
    fail_count = 0
    if action == "complete":
        for oid in ids:
            async with Session() as s:
                o = await s.get(SmmOrder, oid)
                if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
                    o = None
                if not o or o.status != "failed":
                    continue
                o.status = "completed"
                o.updated_at = datetime.utcnow()
                o.note = (o.note or "") + " | تکمیلِ گروهیِ دستی توسطِ ادمین (بدونِ بازگشتِ وجه)"
                await s.commit()
            ok_count += 1
            await _notify_user(row.id, oid, (await _smm_txt(row.id, "completed")), bot=bot)
            await asyncio.sleep(0.05)  # از فلادِ ارسالِ پیام به تلگرام جلوگیری می‌کنه
    elif action == "refund":
        refund_txt = await _smm_txt(row.id, "refunded")
        for oid in ids:
            ok = await _refund_order(row.id, oid, reason="بازگشتِ گروهیِ دستیِ وجه توسطِ ادمین",
                                     notify_text=refund_txt, bot=bot)
            ok_count += 1 if ok else 0
            fail_count += 0 if ok else 1
            await asyncio.sleep(0.05)
    elif action == "retry":
        for oid in ids:
            ok, _ = await _place_on_panel(row.id, oid)
            ok_count += 1 if ok else 0
            fail_count += 0 if ok else 1
            await asyncio.sleep(0.1)  # پنل‌هایِ SMM معمولاً ریت‌لیمیتِ سخت‌گیرانه‌تری دارن

    label = _BULK_LABELS[action]
    summary = f"✅ <b>{label}</b> تمام شد.\n\nموفق: {ok_count}"
    if action != "complete":
        summary += f"\nناموفق: {fail_count}"
    text, kb = await _smm_vs_bulkmenu_content(row.id)
    try:
        await cb.message.edit_text(summary + "\n\n" + text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(summary, parse_mode="HTML")
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:vs:view:"))
async def smm_vs_view(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    try:
        _, _, _, kind, page, oid = cb.data.split(":")
    except ValueError:
        await cb.answer("داده نامعتبر است.", show_alert=True)
        return
    back_cb = f"smm:vs:{kind}:{page}"
    text, kb = await _smm_vs_order_detail_content(row.id, int(oid), back_cb)
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


# === PATCH_VS_UNIFIED_ORDERS_V1 ===
@smm_router.callback_query(F.data.startswith("smm:vs:gview:"))
async def smm_vs_gview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    try:
        _, _, _, kind, page, oid = cb.data.split(":")
        text, kb = await _vs_gift_detail_content(row.id, int(oid), f"smm:vs:{kind}:{page}")
    except ValueError:
        await cb.answer("داده نامعتبر است.", show_alert=True)
        return
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:vs:nview:"))
async def smm_vs_nview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    try:
        _, _, _, kind, page, oid = cb.data.split(":")
        text, kb = await _vs_nft_detail_content(row.id, int(oid), f"smm:vs:{kind}:{page}")
    except ValueError:
        await cb.answer("داده نامعتبر است.", show_alert=True)
        return
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer()


class SmmVsSearchFSM(StatesGroup):
    order_id = State()


@smm_router.callback_query(F.data == "smm:vs:search")
async def smm_vs_search_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmVsSearchFSM.order_id)
    await cb.message.answer(
        "✨ لطفاً شناسهٔ سفارش را با اعداد انگلیسی ارسال کنید.\n"
        "مثال: 123456\n\n"
        "❌ برای لغو /cancel را ارسال کنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="smm:vs:search:cancel", style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


@smm_router.callback_query(F.data == "smm:vs:search:cancel")
async def smm_vs_search_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.clear()
    text, kb = await smm_vs_menu_content(row.id)
    await _smm_vs_edit_or_send(cb, text, kb)
    await cb.answer("لغو شد")


_SMM_VS_NAV_TEXTS = {"🏠 بازگشت به خانه", "⬅️ بازگشت", "لغو", "/cancel"}


@smm_router.message(SmmVsSearchFSM.order_id, F.text)
async def smm_vs_search_result(msg: Message, state: FSMContext, bot: Bot):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in _SMM_VS_NAV_TEXTS:
        await state.clear()
        text, kb = await smm_vs_menu_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    # === PATCH_UNIFIED_TRACKING_V1 === اول با کدِ پیگیری (یکسان در همهٔ بخش‌ها) جست‌وجو کن
    _code = raw.strip().upper()
    if _code:
        async with Session() as s:
            _hit = await s.scalar(select(SmmOrder.id).where(
                SmmOrder.bot_id == row.id, SmmOrder.tracking_code == _code))
            # === PATCH_VS_UNIFIED_ORDERS_V1 === استارز/گیفت/پریمیوم/TON هم همین کدِ پیگیری را دارند
            _ghit = None if _hit else await s.scalar(select(GiftOrder.id).where(
                GiftOrder.bot_id == row.id, GiftOrder.tracking_code == _code))
        if _hit:
            await state.clear()
            text, kb = await _smm_vs_order_detail_content(row.id, int(_hit), "smm:vs:menu")
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
            return
        if _ghit:
            await state.clear()
            text, kb = await _vs_gift_detail_content(row.id, int(_ghit), "smm:vs:menu")
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
            return
        if re.fullmatch(r"N\d+", _code):   # NFT: «N12»
            await state.clear()
            text, kb = await _vs_nft_detail_content(row.id, int(_code[1:]), "smm:vs:menu")
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
            return
    oid_s = _norm_num(raw)
    # === PATCH_SMM_TRACKING_IS_ORDER_ID_V1 ===
    # کدی که مشتری می‌فرستد حالا «SMM-123» است؛ ادمین معمولاً همان را کپی
    # می‌کند، پس پیشوند/هشتگ را هم قبول کن به‌جای «فقط عدد بفرست».
    oid_s = oid_s.upper().replace("SMM-", "").replace("SMM", "").replace("#", "").strip()
    if not oid_s.isdigit():
        await msg.answer("❌ سفارشی با این کد پیدا نشد. کدِ پیگیری (مثلاً AB12CD34) "
                         "یا شناسهٔ عددیِ سفارش را بفرست.")
        return
    await state.clear()
    text, kb = await _smm_vs_order_detail_content(row.id, int(oid_s), "smm:vs:menu")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:approve:"))
async def smm_approve(cb: CallbackQuery, bot: Bot):
    await ensure_smm_columns()
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    oid = int(cb.data.split(":")[2])
    # آیا این سفارش برای یک سرویس دستی است؟
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        if o is not None and o.bot_id != row.id:   # PATCH_TENANT_SCOPE_V1: رکوردِ رباتِ دیگر
            o = None
        is_manual = False
        if o and o.smm_service_pk:
            v = await s.get(SmmService, o.smm_service_pk)
            is_manual = bool(v and v.is_manual)
        # اگر service_pk نبود، از روی service_id تشخیص بده
        if o and not is_manual and str(o.service_id).startswith("man"):
            is_manual = True

    if is_manual:
        # سرویس دستی: به پنل ارسال نمی‌شود؛ ادمین خودش انجام داده و اینجا فقط تأیید می‌کند
        await cb.answer("سفارش دستی تأیید شد")
        async with Session() as s:
            o = await s.get(SmmOrder, oid)
            if o and o.status == "awaiting_admin":
                o.status = "completed"
                o.updated_at = datetime.utcnow()
                o.note = (o.note or "") + " | تأیید دستی ادمین"
                await s.commit()
        await _notify_user(row.id, oid,
                           (await _smm_txt(row.id, "completed")), bot=bot)
        await cb.message.answer(f"✅ سفارش دستی #{await order_no(SmmOrder, oid)} تأیید و به‌عنوان انجام‌شده ثبت شد.")
        # === PATCH_SMM_TRUST_CHANNEL_V1 ===
        await _notify_smm_purchase_success_by_order(bot, row, oid)
        # === PATCH_SMM_ORDER_CHANNEL_V1 ===
        await _post_order_status_to_channel(
            bot, row, oid, manual=True, status="✅ انجام شد",
            note=f"تاییدِ ادمین: {cb.from_user.id}")
    else:
        await cb.answer("در حال ثبت روی پنل...")
        ok, note = await _place_on_panel(row.id, oid)
        if ok:
            await cb.message.answer(f"✅ سفارش #{await order_no(SmmOrder, oid)} روی پنل ثبت شد.")
            await _notify_user(row.id, oid,
                               (await _smm_txt(row.id, "sent")), bot=bot)
            # === PATCH_SMM_TRUST_CHANNEL_V1 ===
            await _notify_smm_purchase_success_by_order(bot, row, oid)
        else:
            await cb.message.answer(f"⚠️ ثبت سفارش #{await order_no(SmmOrder, oid)} ناموفق بود: {note}")
    # === PATCH_SMM_CHANNEL_APPROVE_BUTTONS_V1 ===
    # اگه این دکمه رو از خودِ پستِ کانالِ سفارشاتِ دستی زده (نه از داخلِ
    # لیستِ ادمین در ربات)، نباید کلِ لیستِ سفارش‌ها رو جایِ اون پیام
    # بذاریم — فقط دکمه‌ها رو برمی‌داریم و یه خط وضعیت اضافه می‌کنیم تا
    # بقیهٔ تیم بدونن رسیدگی شده.
    if getattr(cb.message.chat, "type", None) == "channel":
        try:
            await cb.message.edit_text(
                (cb.message.html_text or cb.message.text or "") + "\n\n✅ تأیید شد.",
                reply_markup=None, parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (به‌روزرسانیِ پستِ کانال)")
        return
    text, kb = await smm_orders_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# === PATCH_MANUAL_ORDER_CHANNEL_V1 ===
async def announce_manual_smm_order(bot, row, oid: int) -> None:
    """سفارشِ دستیِ SMM که از مسیری غیر از تک‌سفارش ثبت شده (انبوه، API):
    همان رفتارِ تک‌سفارش — پست در کانالِ سفارشاتِ دستی با دکمهٔ تأیید/رد،
    وگرنه پیامِ خصوصی به مالک. قبلاً این سفارش‌ها به هیچ‌جا اعلام نمی‌شدند."""
    try:
        from tenant.gateways.receipt_channel import post_smm_order
        async with Session() as s:
            o = await s.get(SmmOrder, oid)
            if not o or o.bot_id != row.id:
                return
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == o.user_telegram_id))
        posted = await post_smm_order(
            bot, row.id, manual=True, service_name=o.service_name or "—", link=o.link,
            quantity=o.quantity, price=o.price_toman, user=u,
            order_id=o.tracking_code or f"#{oid}", status="در انتظارِ انجام و تایید",
            note="از دکمه‌هایِ پایین همین پیام تأیید یا رد کن.", pk=oid)   # PATCH_ORDER_CODE_UNIFIED_V1
        if not posted and (await _get_setting(row.id, "smm_alert_manual_order", "1")) != "0":
            await bot.send_message(
                row.owner_id,
                f"🔔 سفارش جدید SMM (#{o.tracking_code or oid})\n"
                f"🛰 {o.service_name} × {int(o.quantity or 0):,}\n🔗 {o.link}\n"
                f"💰 {int(float(o.price_toman or 0)):,} تومان\n\n"
                f"از «📋 سفارش‌های SMM» تأیید یا رد کن.")
    except Exception:
        logger.exception("اعلامِ سفارشِ دستیِ SMM ناموفق بود (order=%s)", oid)


# === PATCH_SMM_ORDER_CHANNEL_V1 ===
async def _post_order_status_to_channel(bot, row, oid: int, manual: bool,
                                        status: str, note: str = "") -> None:
    """پستِ به‌روزرسانیِ وضعیتِ یک سفارش در کانالِ مربوطه.

    برایِ اینکه تیم بداند کارِ دستی انجام شده و دوباره سراغش نرود.
    """
    try:
        from tenant.gateways.receipt_channel import post_smm_order
        async with Session() as s:
            o = await s.get(SmmOrder, oid)
            if not o:
                return
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id,
                BotUser.telegram_id == o.user_telegram_id))
        await post_smm_order(bot, row.id, manual=manual,
                             service_name=o.service_name or "—", link=o.link,
                             quantity=o.quantity, price=o.price_toman, user=u,
                             order_id=o.tracking_code or f"#{oid}",
                             status=status, note=note)
    except Exception:
        logger.exception("پستِ وضعیتِ سفارش در کانال ناموفق بود (oid=%s)", oid)


@smm_router.callback_query(F.data.startswith("smm:reject:"))
async def smm_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    oid = int(cb.data.split(":")[2])
    # === PATCH_SMM_NOTIFY_GAPS_V1 ===
    refunded = await _refund_order(row.id, oid, reason="رد توسط ادمین",
                                   notify_text=(await _smm_txt(row.id, "rejected")), bot=bot)
    await cb.answer("سفارش رد و وجه بازگردانده شد" if refunded else "انجام شد")
    # === PATCH_SMM_CHANNEL_APPROVE_BUTTONS_V1 ===
    if getattr(cb.message.chat, "type", None) == "channel":
        try:
            await cb.message.edit_text(
                (cb.message.html_text or cb.message.text or "") + "\n\n❌ رد شد.",
                reply_markup=None, parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (به‌روزرسانیِ پستِ کانال)")
        return
    text, kb = await smm_orders_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════ ثبت روی پنل و بازگشت وجه ═══════════════════════

async def _place_on_panel(bot_id: int, order_pk: int) -> tuple[bool, str]:
    """سفارش داخلی را روی پنل ثبت می‌کند.

    === PATCH_SMM_FAIL_RETRY_V1 ===
    قبلاً وقتی پنل خطا می‌داد، همیشه بی‌چون‌وچرا وجه برمی‌گشت — یعنی
    سفارشِ «خطا خورده» هیچ‌وقت واقعاً باقی نمی‌موند که ادمین بتونه دوباره
    امتحانش کنه (مثلاً وقتی مشکل موقتیه: پنل کمبودِ موجودی داشته یا نتِ
    لحظه‌ای قطع بوده). حالا با تنظیمِ smm_fail_policy قابلِ انتخابه:
      - "auto_refund" (پیش‌فرض، دقیقاً رفتارِ قبلی) → بلافاصله بازگشتِ وجه.
      - "manual_review" → وضعیت می‌شه failed و منتظرِ تصمیمِ ادمین می‌مونه
        (از صفحهٔ «⚠️ سفارشاتِ خطا خورده» می‌تونه دوباره بفرسته یا دستی
        وجه برگردونه). پیش‌فرض دست‌نخورده مونده تا هیچ رباتِ فعلی‌ای رفتارش
        عوض نشه؛ فقط کسایی که خودشون فعالش کنن این حالتِ جدید رو می‌گیرن.
    """
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
        if not o or o.bot_id != bot_id:
            return False, "سفارش یافت نشد"
        if o.status not in ("awaiting_admin", "failed"):
            return False, "این سفارش قبلاً پردازش شده"
        link, qty, sid, panel_pk = o.link, o.quantity, o.service_id, o.panel_pk
        prev_status = o.status
        # === PATCH_SMM_PLACE_CLAIM_V1 === تصاحبِ اتمیک: دو کلیکِ هم‌زمانِ «ارسال به پنل»
        # (یا دو ادمین) قبلاً هر دو از چکِ وضعیت رد می‌شدند و سفارش دوبار در پنل ثبت می‌شد.
        res_claim = await s.execute(
            update(SmmOrder)
            .where(SmmOrder.id == order_pk, SmmOrder.status == prev_status)
            .values(status="sending", updated_at=datetime.utcnow()))
        await s.commit()
        if (res_claim.rowcount or 0) != 1:
            return False, "این سفارش قبلاً پردازش شده"
    # === PATCH_SMM_MULTI_PANEL_V1 ===
    api_url, api_key, _ = await _panel_creds(bot_id, panel_pk)
    try:
        res = await smm_api.add_order(api_url, api_key, sid, link, qty)
    except Exception as e:   # نباید سفارش در وضعیتِ «sending» گیر کند
        logger.exception("[SMM] add_order برای سفارش #%s استثنا داد", order_pk)
        res = {"error": f"ارتباط با پنل برقرار نشد ({type(e).__name__})"}
    if not res or res.get("error") or not res.get("order"):
        err = (res or {}).get("error", "پاسخ نامعتبر از پنل")
        # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
        policy = await _panel_fail_policy(bot_id, panel_pk)
        if policy == "manual_review":
            async with Session() as s:
                o = await s.get(SmmOrder, order_pk)
                if o:
                    o.status = "failed"
                    o.panel_status = str(err)[:30]
                    o.updated_at = datetime.utcnow()
                    o.note = (o.note or "") + f" | خطای پنل: {err}"
                    await s.commit()
            # === PATCH_SMM_MANUAL_REVIEW_NOTIFY_V1 ===
            # قبلاً هیچ پیامی به مشتری نمی‌رفت؛ الان بهش میگیم که منتظرِ
            # بررسیِ ادمینه، نه اینکه فکر کنه سفارشش گم شده.
            await _notify_user(bot_id, order_pk, (await _smm_txt(bot_id, "manual_review")))
            return False, str(err)
        await _refund_order(bot_id, order_pk, reason=f"خطای پنل: {err}",
                            notify_text=(await _smm_txt(bot_id, "failed")))
        return False, str(err)
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
        if o:
            o.panel_order_id = str(res.get("order"))
            o.status = "sent"
            o.updated_at = datetime.utcnow()
            await s.commit()
    return True, "ثبت شد"


async def _refund_order(bot_id: int, order_pk: int, reason: str = "",
                        notify_text: str | None = None, bot: "Bot | None" = None) -> bool:
    """وجه سفارش را به کیف پول کاربر برمی‌گرداند و وضعیت را refunded می‌کند.

    === PATCH_SMM_NOTIFY_GAPS_V1 ===
    notify_text: اگه داده بشه، بعدِ موفقیتِ بازگشتِ وجه به همون کاربر
    فرستاده می‌شه. قبلاً فقط یک‌جا (ردِ دستیِ ادمین) این اطلاع‌رسانی رو
    جدا صدا می‌زد؛ بقیهٔ مسیرها (خطایِ پنل، لغوِ پنل در حلقهٔ پس‌زمینه،
    بازگشتِ دستیِ وجه) اصلاً به کاربر خبر نمی‌دادن."""
    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        o = await s.get(SmmOrder, order_pk, with_for_update=True)
        if not o or o.bot_id != bot_id:
            return False
        if o.status in ("refunded", "canceled", "completed"):
            return False
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == o.user_telegram_id).with_for_update())
        # سفارشِ «partial» قبلاً بخشی از مبلغ را پس گرفته (refunded_toman)؛ بدونِ کسرِ
        # آن، لغو/بازگشتِ بعدیِ ادمین کلِ مبلغ را دوباره برمی‌گرداند (بیش از پرداختی).
        credit = max(0.0, float(o.price_toman or 0) - float(o.refunded_toman or 0))
        if u and credit > 0:
            u.balance = float(u.balance) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit,
                            kind="smm_refund", note=f"بازگشت وجه SMM #{o.tracking_code or o.id} — {reason}"[:200]))
        o.status = "refunded"
        o.refunded_toman = float(o.refunded_toman or 0) + (credit if u else 0)
        o.updated_at = datetime.utcnow()
        if reason:
            o.note = (o.note or "") + f" | {reason}"
        await s.commit()
    if notify_text:
        await _notify_user(bot_id, order_pk, notify_text, bot=bot)
    return True


# ═══════════════════ سفارشِ نیمه‌تمام (Partial) ═══════════════════
# === PATCH_SMM_PARTIAL_V1 ===
# گزارشِ ادمین (فدایی): «وضعیتِ نیمه‌تمامو چرا نداره رباتت؟». پنل‌هایِ SMM وقتی فقط بخشی از
# تعداد را تحویل بدهند وضعیتِ "Partial" و مقدارِ remains را برمی‌گردانند و مابقیِ پول را خودشان
# برمی‌گردانند. قبلاً این وضعیت اصلاً شناخته نمی‌شد: سفارش برایِ همیشه «ثبت‌شده/در حال انجام»
# می‌ماند (هر دقیقه دوباره استعلام می‌شد)، مشتری هیچ خبری نمی‌گرفت و مبلغِ مابقی به کیف‌پولش
# برنمی‌گشت.

# وضعیت‌هایی که از آن‌ها می‌شود به «نیمه‌تمام» رفت (پنل فقط از «sent»؛ ادمینِ دستی از هرسه).
_PARTIAL_FROM_PANEL = ("sent",)
_PARTIAL_FROM_ADMIN = ("sent", "awaiting_admin", "failed")


def _partial_refund_amount(price, quantity, remains) -> int:
    """سهمِ مبلغِ مربوط به «مقدارِ باقی‌مانده» (تومانِ صحیح، گردکردنِ معمولی). هرگز از کلِ مبلغ بیشتر نمی‌شود.
    مثال: پرداختی ۱۷۲، تعداد ۳۵۰، باقی‌مانده ۳۲ ⇒ ۱۶."""
    from decimal import Decimal, ROUND_HALF_UP
    q = int(quantity or 0)
    r = int(remains or 0)
    total = Decimal(str(price or 0))
    if q <= 0 or r <= 0 or total <= 0:
        return 0
    if r >= q:
        return int(total.to_integral_value(rounding=ROUND_HALF_UP))
    amt = (total * Decimal(r) / Decimal(q)).to_integral_value(rounding=ROUND_HALF_UP)
    return max(0, min(int(amt), int(total.to_integral_value(rounding=ROUND_HALF_UP))))


async def _apply_partial(bot_id: int, order_pk: int, remains, *, source: str = "panel",
                         bot: "Bot | None" = None) -> str:
    """سفارش را «نیمه‌تمام» می‌کند: مبلغِ سهمِ مقدارِ باقی‌مانده به کیف‌پول برمی‌گردد،
    وضعیت partial می‌شود و پیامِ قابل‌تنظیمِ «partial» برایِ مشتری می‌رود.

    خروجی: "partial" | "completed" (چیزی باقی نمانده بود) | "refunded" (هیچ‌چیز تحویل نشده بود)
           | "skip" (سفارش در وضعیتی نیست که بشود تغییرش داد / ورودیِ نامعتبر).
    ایمن در برابرِ دوبار اجرا: بعد از اولین بار وضعیت partial است و دفعهٔ دوم «skip» می‌شود؛ پول
    فقط یک‌بار و زیرِ قفلِ ردیفِ سفارش و کاربر جابه‌جا می‌شود."""
    try:
        remains = int(remains)
    except (TypeError, ValueError):
        return "skip"
    allowed = _PARTIAL_FROM_PANEL if source == "panel" else _PARTIAL_FROM_ADMIN

    async with Session() as s:
        o = await s.get(SmmOrder, order_pk, with_for_update=True)
        if not o or o.bot_id != bot_id or o.status not in allowed:
            return "skip"
        qty = int(o.quantity or 0)
        if qty <= 0 or remains < 0:
            return "skip"
        if remains == 0:
            # چیزی باقی نمانده = عملاً تکمیل (بدونِ بازگشتِ وجه)
            o.status = "completed"
            o.remaining = 0
            o.updated_at = datetime.utcnow()
            o.note = (o.note or "") + f" | تکمیل ({source}؛ باقی‌مانده ۰)"
            await s.commit()
            outcome = "completed"
        elif remains >= qty:
            outcome = "refunded"
        else:
            refund = _partial_refund_amount(o.price_toman, qty, remains)
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id,
                BotUser.telegram_id == o.user_telegram_id).with_for_update())
            if u is None:
                logger.error("[SMM] نیمه‌تمام #%s: کاربر پیدا نشد؛ وجه برگردانده نشد", order_pk)
            elif refund > 0:
                u.balance = float(u.balance) + refund
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=refund, kind="smm_refund",
                                note=f"بازگشتِ بخشیِ سفارشِ نیمه‌تمام SMM #{o.tracking_code or o.id} "
                                     f"({qty - remains} از {qty} تحویل شد)"[:200]))
            o.status = "partial"
            o.remaining = remains
            o.refunded_toman = refund
            o.updated_at = datetime.utcnow()
            o.note = (o.note or "") + (
                f" | نیمه‌تمام ({source}): تحویل {qty - remains} از {qty}، برگشت {refund} تومان")
            await s.commit()
            outcome = "partial"

    if outcome == "refunded":
        ok = await _refund_order(bot_id, order_pk, reason=f"هیچ‌چیز تحویل نشد ({source})",
                                 notify_text=(await _smm_txt(bot_id, "refunded")), bot=bot)
        return "refunded" if ok else "skip"
    if outcome == "completed":
        await _notify_user(bot_id, order_pk, (await _smm_txt(bot_id, "completed")), bot=bot)
    else:
        await _notify_user(bot_id, order_pk, (await _smm_txt(bot_id, "partial")), bot=bot)
    return outcome


def _api_notify_key(tg_id: int) -> str:
    return f"api_notify_{int(tg_id)}"


async def api_notify_enabled(bot_id: int, tg_id: int) -> bool:
    """PATCH_API_ORDER_NOTIFY_V1 — آیا مشتری اعلانِ وضعیتِ سفارش‌هایِ وب‌سرویس را روشن کرده؟"""
    return (await _get_setting(bot_id, _api_notify_key(tg_id), "0")) == "1"


async def set_api_notify(bot_id: int, tg_id: int, on: bool) -> None:
    await _set_setting(bot_id, _api_notify_key(tg_id), "1" if on else "0")


async def _notify_user(bot_id: int, order_pk: int, text: str, bot: "Bot | None" = None):
    """پیام را به کاربرِ صاحبِ سفارش می‌فرستد.

    === PATCH_SMM_NOTIFY_GAPS_V1 ===
    قبلاً این تابع فقط از هندلرهایِ زنده (که خودشون یه Bot دارن) صدا زده
    می‌شد؛ حلقهٔ پس‌زمینه (poll loop) و _refund_order هیچ Bot ای در دسترس
    نداشتن، پس اصلاً به کاربر خبر نمی‌دادن — دقیقاً همون باگِ گزارش‌شده که
    «پیامِ وضعیتِ سفارش اصلاً برایِ کاربر نمی‌ره». حالا اگه bot داده نشه،
    خودش یکی از رویِ توکنِ ربات می‌سازه."""
    try:
        async with Session() as s:
            o = await s.get(SmmOrder, order_pk)
            if not o:
                return
            tg_id = o.user_telegram_id
            _is_api = (getattr(o, "source", None) or "") == "api"
            # === PATCH_TEXT_VARS_V1 ===
            _bu = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        # === PATCH_API_ORDER_NOTIFY_V1 ===
        # درخواستِ فدایی: پیامِ وضعیتِ سفارش‌هایِ وب‌سرویس فقط وقتی برایِ مشتری بیاید که خودش
        # «🟢 اعلان سفارشات وبسرویس» را در صفحهٔ API روشن کرده باشد (پیش‌فرض خاموش — نماینده‌ها
        # صدها سفارش دارند)، و با برچسبِ #وبسرویس تا از سفارش‌هایِ داخلِ ربات جدا شود.
        if _is_api:
            if not await api_notify_enabled(bot_id, tg_id):
                return
            text = "#وبسرویس\n" + text
        # متغیرهایِ متن ({{product_name}}، {{quantity}}، …): یک نقطهٔ رندر برایِ همهٔ
        # متن‌هایِ وضعیتِ سفارش. اگر رندر شکست بخورد، متنِ خام فرستاده می‌شود (خبرِ
        # سفارش به مشتری هیچ‌وقت نباید به‌خاطرِ یک متغیر گم شود).
        try:
            from tenant.shared.text_vars import render_text
            text = await render_text(text, bot_id=bot_id, user=_bu, order=o)
        except Exception:
            logger.exception("[SMM] رندرِ متغیرهایِ متنِ سفارش ناموفق بود (متنِ خام ارسال شد)")
        tbot = bot
        own_bot = False
        if tbot is None:
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if not brow:
                return
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            own_bot = True
        try:
            await tbot.send_message(tg_id, text)
        finally:
            if own_bot:
                await tbot.session.close()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# === PATCH_SMM_TRUST_CHANNEL_V1 ===
# اطلاعِ خرید (به ادمین‌ها + کانالِ اعتمادسازی) برایِ بات‌هایِ خدماتِ مجازی.
# قبل از این پچ، بات‌هایِ virtual_service هرگز _purchase_alert رو صدا نمی‌زدن،
# پس هم اطلاع‌رسانیِ خرید کار نمی‌کرد هم کانالِ اعتمادسازی.

async def _notify_smm_purchase_success(bot: Bot, row, buyer_user,
                                         service, quantity: int, price: int,
                                         order_pk: int) -> None:
    """صدازدنِ _purchase_alertِ کلانِ tenant. بی‌صدا از هر خطایی رد می‌شه —
    این جانبی‌ست، نباید خودِ خریدِ موفق رو مختل کنه."""
    # === PATCH_SMM_ADMIN_ALERT_TOGGLES_V1 ===
    # درخواستِ ادمین (فدایی): «پیامِ ثبت سفارش/جزئیاتِ ارائه‌دهنده رو خاموش کنم؛ پیوی ربات شلوغ شده».
    # دو کلیدِ جدا (پیش‌فرض روشن = رفتارِ قبلی؛ "0" = خاموش) از منویِ «✍️ متن‌هایِ سفارش»:
    #   smm_alert_purchase       → پیامِ «🛍 خرید جدید ثبت شد» به ادمین‌ها
    #   smm_alert_provider_info  → پیامِ «🧾 جزئیاتِ ارائه‌دهنده برایِ همین سفارش» به مالک
    # خاموش‌کردنِ این‌ها *پستِ کانالِ اعتمادسازی* را قطع نمی‌کند (آن مستقل است).
    try:
        from tenant import _purchase_alert
        detail = f"{getattr(service, 'name', 'سرویس مجازی')} × {quantity:,}"
        if (await _get_setting(row.id, "smm_alert_purchase", "1")) == "0":
            # فقط کانالِ اعتمادسازی؛ بدونِ DM به ادمین‌ها
            from tenant.shared.trust_report import send_trust_channel_post
            await send_trust_channel_post(
                bot, row.id,
                buyer_telegram_id=(buyer_user.telegram_id if buyer_user else None),
                product_name=detail, amount=price,
                order_id=order_pk, product_id=getattr(service, "id", None) or order_pk,
                tracking_code=None, count=quantity,
                bot_username=row.username,
                panel_name=None, button_name=None,
            )
        else:
            await _purchase_alert(
                bot, row, buyer=buyer_user,
                section="خدمات مجازی تلگرام",
                detail=detail, amount=price,
                order_id=order_pk,
                product_id=getattr(service, "id", None),
                count=quantity,
            )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده در _notify_smm_purchase_success")
    if (await _get_setting(row.id, "smm_alert_provider_info", "1")) == "0":
        return
    # === PATCH_SMM_ADMIN_PROVIDER_INFO_V1 ===
    # درخواستِ ادمین: پیامِ «خریدِ جدید» باید کدِ سفارش در ارائه‌دهنده رو هم
    # نشون بده. _purchase_alert یه تابعِ عمومیِ مشترکِ بینِ همه‌ی نوع محصولاته
    # (فروشگاه/VPN/گیفت و...)، پس قالبش رو دست‌نزدیم؛ به‌جاش این‌جا یه پیامِ
    # کوتاهِ الحاقیِ مخصوصِ SMM می‌فرستیم که فقط شاملِ اطلاعاتِ مربوط به
    # ارائه‌دهنده/کدِ پیگیریه.
    try:
        async with Session() as s:
            o = await s.get(SmmOrder, order_pk)
        if o:
            _, _, panel_name = await _panel_creds(row.id, o.panel_pk)
            lines = [
                "🧾 <b>جزئیاتِ ارائه‌دهنده برایِ همین سفارش</b>",
                f"🔖 کدِ پیگیریِ ما: <code>{o.tracking_code or o.id}</code>",
            ]
            if o.panel_order_id:
                lines.append(f"🛰 پنل: {panel_name}")
                lines.append(f"🆔 کدِ سفارش در پنل: <code>{o.panel_order_id}</code>")
            else:
                lines.append("⏳ هنوز روی پنل ثبت نشده (سفارشِ دستی یا در انتظارِ تأیید).")
            if o.link:
                lines.append(f"🔗 لینک: {o.link}")
            from aiogram.types import LinkPreviewOptions
            await bot.send_message(row.owner_id, "\n".join(lines), parse_mode="HTML",
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده در ارسالِ جزئیاتِ ارائه‌دهنده به ادمین")


# === PATCH_SMM_ADMIN_FAILURE_ALERT_V1 ===
_LOW_BALANCE_HINTS = ("insufficient", "balance", "not enough", "موجودی", "اعتبار کافی")


async def _notify_smm_order_failed(bot: Bot, row, buyer_user, service,
                                    quantity: int, price: int, panel_error: str) -> None:
    """وقتی سفارشِ خودکار روی پنل رد می‌شه، مستقیم به مالکِ ربات پیام می‌ده —
    قبلاً این مسیر اصلاً وجود نداشت و ادمین فقط از طریقِ شکایتِ مشتری (تیکت)
    می‌فهمید. بی‌صدا از خطا رد می‌شه، نباید جریانِ اصلیِ بازپرداخت رو مختل کنه."""
    try:
        is_balance_issue = any(h.lower() in (panel_error or "").lower() for h in _LOW_BALANCE_HINTS)
        # === PATCH_SMM_ALERT_TOGGLE_V1 ===
        # درخواستِ ادمین (فدایی): «چطوری غیرفعال کنم این پیاما نیاد؛ سفارش‌ها تو لیست هست دیگه».
        # کلید: smm_alert_panel_reject ("0" = خاموش). وقتی خاموش است هیچ پیامِ تک‌تک‌ه‌ای نمی‌آید، *به‌جز*
        # هشدارِ «موجودیِ پنل تمام شده» که حداکثر هر ۶ ساعت یک‌بار می‌آید — چون بی‌خبری از آن یعنی
        # همهٔ سفارش‌هایِ بعدی هم بی‌صدا رد شوند.
        if (await _get_setting(row.id, "smm_alert_panel_reject", "1")) == "0":
            if not is_balance_issue:
                return
            import time as _time
            _now = _time.time()
            try:
                _last = float(await _get_setting(row.id, "smm_lowbal_alert_ts", "0") or 0)
            except ValueError:
                _last = 0.0
            if _now - _last < 6 * 3600:
                return
            await _set_setting(row.id, "smm_lowbal_alert_ts", str(_now))
        buyer_label = f"@{buyer_user.username}" if getattr(buyer_user, "username", None) else \
            str(getattr(buyer_user, "telegram_id", "—"))
        svc_name = getattr(service, "name", "سرویس مجازی")
        text = (
            f"❌ <b>سفارشِ خودکارِ SMM رویِ پنل رد شد</b>\n\n"
            f"🛰 {svc_name} × {quantity:,}\n"
            f"👤 مشتری: {buyer_label}\n"
            f"💰 مبلغ (بازگردانده‌شده به کیفِ‌پولِ مشتری): {price:,} تومان\n"
            f"📋 پیامِ پنل: <code>{panel_error}</code>"
        )
        if is_balance_issue:
            text += (
                "\n\n⚠️ <b>احتمالاً موجودیِ حسابت رویِ پنلِ SMM تموم شده</b> — "
                "برو تو سایتِ همون پنل (نه بات‌ساز) شارژش کن، وگرنه سفارش‌هایِ "
                "بعدی هم رد می‌شن و مشتری‌هات پولشون هربار برمی‌گرده بدونِ اینکه "
                "سرویسی بگیرن."
            )
        await bot.send_message(row.owner_id, text)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده در _notify_smm_order_failed")


async def _notify_smm_purchase_success_by_order(bot: Bot, row, order_pk: int) -> None:
    """نسخهٔ راحت‌ترِ بالا وقتی فقط order_pk داری (توی مسیرِ تأییدِ ادمین).
    خودش SmmOrder و کاربر رو می‌خونه."""
    try:
        async with Session() as s:
            o = await s.get(SmmOrder, order_pk)
            if not o:
                return
            buyer_user = await s.scalar(select(BotUser).where(
                BotUser.bot_id == o.bot_id,
                BotUser.telegram_id == o.user_telegram_id))
            svc = None
            if o.smm_service_pk:
                svc = await s.get(SmmService, o.smm_service_pk)
        # شیءِ سبک اگه سرویس تو دیتابیس نبود
        if svc is None:
            class _S:
                name = o.service_name or "سرویس مجازی"
                id = None
            svc = _S()
        await _notify_smm_purchase_success(
            bot, row, buyer_user, svc, o.quantity or 1,
            int(o.price_toman or 0), order_pk)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده در _notify_smm_purchase_success_by_order")


async def _notify_gift_order_trust_success(bot: Bot, row, order_id: int) -> None:
    """ارسال اعلان کانال اعتمادسازی برای سفارش‌های GiftOrder پس از موفقیت قطعی."""
    try:
        async with Session() as s:
            o = await s.get(GiftOrder, order_id)
            if not o or o.bot_id != row.id:
                return
            # === PATCH_TRUST_ONLY_AFTER_DELIVERY_V1 ===
            # گزارشِ MD04: خریدِ استارز/تون/ری‌اکشن/پرمیومِ دستی همان لحظهٔ ثبتِ سفارش (قبل از اینکه ادمین انجامش بدهد)
            # در کانالِ اعتمادسازی پست می‌شد. حالا فقط وقتی سفارش واقعاً تحویل شده (status=sent) پست می‌شود؛
            # مسیرهایِ «ثبتِ سفارش» که هنوز awaiting_admin هستند خودکار رد می‌شوند و بعد از تأییدِ ادمین
            # (که status را sent می‌کند) همین تابع دوباره صدا زده و پست انجام می‌شود.
            if (o.status or "") not in ("sent", "completed", "done"):
                return
            buyer = await s.get(BotUser, o.buyer_user_id)
        from tenant import _purchase_alert
        # === PATCH_TRUST_COUNT_V1 ===
        # گزارشِ MD04: در متنِ اعتمادسازی {count} همیشه ۱ بود و عددِ استارز داخلِ نامِ محصول می‌آمد.
        # حالا نامِ محصول بدونِ عدد است و {count} = تعدادِ واقعی (استارز/ری‌اکشن/تون).
        _pub_detail = None
        _count = o.quantity or 1
        if (o.gift_id or '').startswith('premium_'):
            section = 'پرمیوم تلگرام'
            detail = f'🌟 پرمیوم تلگرام — {o.gift_id.split("_")[-1]} ماهه'
        elif (o.gift_id or '').startswith('starsell_'):
            section = 'استارز تلگرام'
            detail = f'⭐ استارز تلگرام — {o.star_count or 0:,} عدد'
            _pub_detail, _count = '⭐ استارز تلگرام', (o.star_count or 0)
        elif (o.gift_id or '').startswith('srx_'):
            section = 'ری‌اکشن استارزی'
            detail = f'⭐️ ری‌اکشن استارزی — {o.star_count or 0:,} عدد'
            _pub_detail, _count = '⭐️ ری‌اکشن استارزی', (o.star_count or 0)
        elif (o.gift_id or '').startswith('tonsell_'):
            # === PATCH_TON_SELL_V1 === مقدارِ تون در star_count به‌صورتِ صدم‌تون ذخیره شده
            section = 'تون (TON)'
            detail = f'💎 تون (TON) — {(o.star_count or 0) / 100:g} TON'
            _pub_detail, _count = '💎 تون (TON)', f'{(o.star_count or 0) / 100:g}'
        elif (o.gift_id or '').startswith('manual'):
            section = 'گیفت تلگرام'
            detail = f'🎁 گیفت سفارشی — {o.gift_emoji or "🎁"}'
        else:
            section = 'گیفت تلگرام'
            detail = f'🎁 گیفت تلگرام — {o.gift_emoji or "🎁"}'
        # === PATCH_TRUST_GIFT_COUNT_SUFFIX_V1 ===
        # گزارشِ فدایی: در کانالِ اعتمادسازی سفارش‌هایِ SMM «نام × تعداد» دارند ولی استارز/TON/گیفت فقط
        # نام (چون عدد فقط در {count} بود و قالبِ او {count} ندارد). اگر قالبِ سفارشیِ کانال {count} ندارد،
        # «× تعداد» به نامِ محصول اضافه می‌شود؛ اگر دارد (مثلاً «{product} × {count}») تکرار نمی‌شود.
        try:
            from tenant import get_setting as _gs
            _tpl = await _gs(row.id, "trust_channel_template", "")
        except Exception:
            _tpl = ""
        if "{count}" not in (_tpl or "") and not (o.gift_id or "").startswith("premium_"):
            _pub_detail = f"{_pub_detail or detail} × {_count}"
        await _purchase_alert(
            bot, row, buyer=buyer, section=section, detail=detail,
            # PATCH_TRUST_GIFT_TOTAL_V1 — price_toman برایِ گیفت قیمتِ «هر عدد» است؛ گزارش باید
            # مبلغِ کلِ پرداختی را نشان دهد (برایِ بقیهٔ انواع quantity همیشه ۱ است)
            amount=int(o.price_toman or 0) * max(1, int(o.quantity or 1)), order_id=o.id,
            tracking_code=o.tracking_code, count=_count, public_detail=_pub_detail)
    except Exception:
        logger.exception("ارسال اعلان موفقیت محصول به کانال اعتمادسازی ناموفق بود")


# ═══════════════════════ دسته‌بندیِ دستی ═══════════════════════
# === PATCH_SMM_MANUAL_CATEGORY_V1 ===
# برخلافِ دسته‌بندیِ خودکار (که از رویِ اسمِ سرویس حدس می‌زنه)، این‌جا
# ادمین خودش یه پلتفرم انتخاب می‌کنه و یه اسمِ دلخواه براش می‌ذاره —
# مثلاً «تلگرام» → «خدماتِ گروهِ تلگرام». هر دسته می‌تونه سودِ مخصوصِ
# خودشو داشته باشه (اولویت روی سودِ پنل).
#
# قدمِ فعلی فقط مدیریتِ خودِ دسته‌ها (ساخت/ویرایش/حذف) است. وصل‌کردنِ
# سرویس‌ها به یک دسته، قدمِ بعدیه.

# === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
def _link_domain(url: str) -> str:
    """دامنهٔ یک لینک را استخراج می‌کند (بدونِ www.، حروفِ کوچک).
    برایِ ورودی‌هایی که http/https ندارن (مثلِ @username یا t.me/x بدونِ
    اسکیم) هم کار می‌کنه — چیزی که این تابع می‌خواد فقط «کدوم پلتفرم»ه،
    نه اعتبارسنجیِ کاملِ URL."""
    u = (url or "").strip()
    if u.startswith("@"):
        return "t.me"  # یوزرنیمِ تلگرام
    m = re.search(r'(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+)', u)
    return m.group(1).lower() if m else u.lower()


# === PATCH_SMM_LINK_KIND_CHECK_V1 ===
# گزارشِ ادمین (فدایی): «نمونه‌لینکِ هر دسته کار نمی‌کند؛ وقتی مشتری لینکِ اشتباه می‌فرستد،
# نمونه‌ای که تنظیم کرده‌ام را برایش بفرست». پیامِ خطا از قبل نمونه را نشان می‌داد، ولی چک
# فقط *دامنه* را مقایسه می‌کرد؛ و چون همهٔ محصولاتِ او تلگرامی‌اند (همه t.me)، هیچ لینکی
# هرگز رد نمی‌شد — حتی لینکِ کانال به‌جایِ لینکِ پست برایِ سرویسِ «بازدید». حالا علاوه بر
# دامنه، «نوعِ لینک» هم مقایسه می‌شود.
_POST_KEYWORDS = {"p", "reel", "reels", "tv", "status", "watch", "shorts", "video", "videos",
                  "post", "posts", "stories", "story", "live"}
_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def _link_kind(url: str) -> str:
    """نوعِ لینک: "post" (پست/ویدیو/استوری)، "profile" (کانال/گروه/صفحه/یوزرنیم) یا "other" (نامعلوم).

    عمداً محافظه‌کار است: هرچه با اطمینان تشخیص داده نشود «other» می‌شود و «other» هیچ‌وقت
    باعثِ ردِ لینک نمی‌شود، تا فروشِ قانونی به‌خاطرِ یک چکِ سخت‌گیرانه از دست نرود.
    برایِ تلگرام، لینکِ دعوت (t.me/+xxx یا joinchat) هم «profile» حساب می‌شود، چون گروه/کانالِ
    خصوصی فقط لینکِ دعوت دارد و باید مثلِ یوزرنیم پذیرفته شود."""
    u = (url or "").strip()
    if not u:
        return "other"
    if u.startswith("@"):
        return "profile"
    m = re.match(r'(?i)^(?:https?://)?(?:www\.)?([^/\s?#]+)(/[^?#\s]*)?', u)
    if not m:
        return "other"
    host = m.group(1).lower()
    segs = [s for s in (m.group(2) or "").split("/") if s]
    if host in _TG_HOSTS:
        if not segs:
            return "other"
        first = segs[0]
        if first.lower() == "c" and len(segs) >= 3 and segs[2].isdigit():
            return "post"                                  # پستِ کانالِ خصوصی: t.me/c/<id>/<n>
        if first.startswith("+") or first.lower() == "joinchat":
            return "profile"                               # لینکِ دعوت
        if len(segs) >= 2 and segs[1].isdigit():
            return "post"                                  # t.me/<name>/<n>
        if len(segs) == 1:
            return "profile"                               # t.me/<name>
        return "other"                                     # t.me/s/<name> و مانندِ آن
    lowered = [s.lower() for s in segs]
    if host in ("youtu.be", "vm.tiktok.com", "vt.tiktok.com") and len(segs) == 1:
        return "post"
    if any(s in _POST_KEYWORDS for s in lowered) or any(s.isdigit() and len(s) >= 6 for s in segs):
        return "post"
    if len(segs) == 1:
        return "profile"                                   # instagram.com/<user>، youtube.com/@x
    return "other"


def _link_matches_sample(link: str, sample: str) -> bool:
    """آیا لینکِ مشتری با نمونهٔ دسته هم‌دامنه و هم‌نوع است؟ (نوعِ نامعلوم هیچ‌وقت ردِ لینک نمی‌کند.)"""
    if _link_domain(link) != _link_domain(sample):
        return False
    want, got = _link_kind(sample), _link_kind(link)
    if want in ("post", "profile") and got in ("post", "profile") and want != got:
        return False
    return True


# === PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
# «انواعِ لینکِ مجاز» برایِ هر دسته (تلگرام/اینستاگرام). منطقِ تشخیص در smm_link_types.py است؛
# این‌جا فقط ذخیره/خواندن (Setting با کلیدِ smm_cat_links:<id>، بدونِ نیاز به مایگریشنِ دیتابیس)
# و اعمال روی سفارش. اگر هیچ نوعی تیک نخورده باشد، رفتارِ قبلی (نمونه‌لینک) دست‌نخورده می‌ماند.
def _cat_links_key(cat_id: int) -> str:
    return f"smm_cat_links:{cat_id}"


async def _cat_link_types(cat) -> set[str]:
    if not cat or not smm_link_types.supported(cat.platform):
        return set()
    return smm_link_types.parse_keys(await _get_setting(cat.bot_id, _cat_links_key(cat.id), ""), cat.platform)


async def _check_links_for_category(cat, links: list[str]) -> tuple[list[str], str]:
    """(لینک‌هایِ نامعتبر، متنِ راهنما). اگر برایِ دسته قاعده‌ای نیست: ([], "").
    اولویت: نوعِ لینکِ مجاز ← نمونه‌لینک (الگویِ قدیمی) ← بدونِ چک."""
    if not cat:
        return [], ""
    types = await _cat_link_types(cat)
    if types:
        wrong = [ln for ln in links if not (smm_link_types.classify(cat.platform, ln) & types)]
        return wrong, smm_link_types.hint_html(cat.platform, types)
    if cat.link_pattern:
        wrong = [ln for ln in links if not _link_matches_sample(ln, cat.link_pattern)]
        return wrong, f"نمونهٔ لینکِ درست: <code>{_html_escape(cat.link_pattern)}</code>"
    return [], ""


async def smm_categories_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == bot_id).order_by(SmmCategory.id))).all()
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    overrides = await _platform_label_overrides(bot_id)
    lines = ["📦 <b>مدیریتِ دسته‌ها</b>\n",
             "دسته‌هایِ دستی که ادمین می‌سازه — هر کدوم می‌تونه سودِ مخصوصِ خودشو داشته باشه.\n"]
    rows = [[InlineKeyboardButton(text="➕ افزودنِ دسته", callback_data="smm:cat:add", style=ButtonStyle.SUCCESS)],
           [InlineKeyboardButton(text="🏷 تغییرِ نامِ پلتفرم‌ها", callback_data="smm:platrename:menu")]]
    if not cats:
        lines.append("هنوز دسته‌ای نساختی.")
    for c in cats:
        plat_label = overrides.get(c.platform, smm_api.platform_label(c.platform))
        mark = "✅" if c.is_active else "🔴"
        margin_txt = f"{float(c.margin_pct):g}٪" if c.margin_pct is not None else "پیش‌فرضِ پنل"
        lines.append(f"{mark} {plat_label} — <b>{c.name}</b> (سود: {margin_txt})")
        rows.append([InlineKeyboardButton(text=f"{plat_label} {c.name}", callback_data=f"smm:cat:view:{c.id}")])
    # === PATCH_SMM_CAT_ORIGIN_V1 ===
    origin = await _get_setting(bot_id, "smm_cat_origin", "top")
    back_target = "smm:products:menu" if origin == "products" else "tadm:sec:products"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target, style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_SMM_PLATFORM_RENAME_V1 ===
async def smm_platrename_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    overrides = await _platform_label_overrides(bot_id)
    lines = ["🏷 <b>تغییرِ نامِ پلتفرم‌ها</b>\n",
             "برچسبِ نمایشیِ هر پلتفرم رو می‌تونی کامل به دلخواهِ خودت عوض کنی — هم تو دسته‌بندی هم تو مرورِ سرویس‌ها.\n"]
    rows = []
    for key, label, _kws in smm_api.PLATFORMS:
        cur = overrides.get(key, label)
        mark = " ✏️" if key in overrides else ""
        rows.append([InlineKeyboardButton(text=f"{cur}{mark}", callback_data=f"smm:platrename:pick:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:cat:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:platrename:menu")
async def smm_platrename_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    text, kb = await smm_platrename_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:platrename:pick:"))
async def smm_platrename_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.rsplit(":", 1)[1]
    default_label = smm_api.platform_label(pkey)
    overrides = await _platform_label_overrides(row.id)
    cur = overrides.get(pkey, default_label)
    await state.set_state(SmmFSM.plat_rename)
    await state.update_data(plat_rename_key=pkey)
    rows = [[InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data="smm:platrename:menu", style=ButtonStyle.DANGER)]]
    if pkey in overrides:
        rows.insert(0, [InlineKeyboardButton(text="↩️ برگشت به نامِ پیش‌فرض", callback_data=f"smm:platrename:reset:{pkey}", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        f"✏️ اسمِ دلخواهِ جدید برایِ این پلتفرم رو بفرست.\n"
        f"الان: <b>{cur}</b> — پیش‌فرض: {default_label}",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:platrename:reset:"))
async def smm_platrename_reset(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.rsplit(":", 1)[1]
    await _set_platform_label(row.id, pkey, None)
    await cb.answer("✅ برگشت به پیش‌فرض")
    text, kb = await smm_platrename_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.message(SmmFSM.plat_rename, F.text)
async def smm_platrename_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    pkey = data.get("plat_rename_key")
    name = (msg.text or "").strip()
    await state.clear()
    if not pkey or not name:
        return
    await _set_platform_label(row.id, pkey, name)
    text, kb = await smm_platrename_content(row.id)
    await msg.answer(f"✅ نامِ پلتفرم به «{name[:60]}» تغییر کرد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:cat:menu"))
async def smm_cat_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if not await _require_smm_api_access(cb, row):
        return
    # === PATCH_SMM_CAT_ORIGIN_V1 ===
    # این صفحه دو تا ورودیِ *بیرونی* جدا داره: مستقیم از منویِ اصلی و از
    # داخلِ «📦 مدیریتِ محصولات» — هر دو با یه پسوندِ صریح مشخص می‌شن
    # (:top / :ph) تا مبدأ رو ست کنن. اما کلیِ زیرصفحه‌هایِ داخلیِ همین
    # بخش (تغییرِ نامِ پلتفرم‌ها، لغوِ افزودنِ دسته، و غیره) هم برایِ
    # برگشتن به این منو از همون callback_data خامِ "smm:cat:menu" (بدونِ
    # پسوند) استفاده می‌کنن — اون‌ها نباید مبدأ رو دست بزنن، وگرنه اگه از
    # هابِ محصولات وارد شده باشی و بعد یکی از این زیرصفحه‌ها رو باز/بسته
    # کنی، مبدأ خطا به "top" ریست می‌شد و بازگشتِ نهایی اشتباه می‌رفت.
    if cb.data == "smm:cat:menu:ph":
        await _set_setting(row.id, "smm_cat_origin", "products")
    elif cb.data == "smm:cat:menu:top":
        await _set_setting(row.id, "smm_cat_origin", "top")
    # اگه cb.data دقیقاً "smm:cat:menu" (بدونِ پسوند) بود، مبدأِ ذخیره‌شده
    # دست‌نخورده می‌مونه — همینه که باگ رو حل می‌کنه.
    text, kb = await smm_categories_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "smm:cat:add")
async def smm_cat_add_start(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    rows = []
    chunk = []
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    _overrides = await _platform_label_overrides(row.id)
    for key, label, _kws in smm_api.PLATFORMS:
        chunk.append(InlineKeyboardButton(text=_overrides.get(key, label), callback_data=f"smm:cat:addp:{key}"))
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append([InlineKeyboardButton(text="📦 سایر", callback_data="smm:cat:addp:other")])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="smm:cat:menu", style=ButtonStyle.DANGER)])
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    # این یه صفحهٔ انتخابِ پلتفرم از لیسته (نه پرسیدنِ متن) — قدمِ بعدی
    # (smm_cat_add_platform) که واقعاً متن می‌پرسه، درست پیامِ تازه می‌فرسته.
    txt = "لطفاً پلتفرمِ مربوط به دستهٔ جدید را انتخاب کن:"
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:cat:addp:"))
async def smm_cat_add_platform(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    platform = cb.data.rsplit(":", 1)[1]
    await state.set_state(SmmFSM.cat_name)
    await state.update_data(cat_platform=platform)
    await cb.message.answer(
        "✨ لطفاً اسمِ دلخواه برایِ این دسته را بفرست.\n"
        "مثال: <code>خدماتِ گروهِ تلگرام</code>\n\n"
        "❌ برایِ لغو /cancel را بفرست.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="smm:cat:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.cat_name, F.text)
async def smm_cat_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    name = msg.text.strip()
    if name in ("لغو", "/cancel", "🏠 بازگشت به خانه", "⬅️ بازگشت"):
        await state.clear()
        text, kb = await smm_categories_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    if not name:
        await msg.answer("❌ اسمِ خالی مجاز نیست.")
        return
    data = await state.get_data()
    platform = data.get("cat_platform", "other")
    # === PATCH_SMM_MULTI_CATEGORY_PER_PLATFORM_V1 ===
    # گزارشِ ادمین (فدایی): «چرا نمی‌شه بیشتر از یکی دسته برایِ هر پلتفرم آورد؟»
    # نسخهٔ قبلی (PATCH_SMM_CATEGORY_DUP_PLATFORM_V1) عمداً از ساختنِ دستهٔ دوم برایِ
    # همان پلتفرم جلوگیری می‌کرد، ولی این قفل اشتباه بود: چنددسته‌ای‌بودنِ یک پلتفرم
    # (مثلاً تلگرام ← «بازدید»، «ممبر گروه»، …) کاملاً معتبر است، و انتخابِ دسته موقعِ
    # افزودن (smm:bulkaddask) و جابه‌جاییِ تک‌تکِ محصول (smm:svc:setcat) از قبل برایِ
    # همین ساخته شده بودند. حالا ساختن آزاد است. مشکلِ اصلیِ آن روز (نمونه‌لینک رویِ
    # دستهٔ خالی و بی‌مصرف) با راهنمایِ پایین‌ترِ همین پیام پوشش داده می‌شود.
    async with Session() as s:
        s.add(SmmCategory(bot_id=row.id, platform=platform, name=name[:60], is_active=True))
        await s.commit()
    await state.clear()
    text, kb = await smm_categories_content(row.id)
    await msg.answer(
        f"✅ دستهٔ «{name}» ساخته شد و فعلاً خالی است.\n"
        "برایِ قرار‌دادنِ محصول در آن: موقعِ «➕ افزودنِ محصول» همین دسته را انتخاب کن، "
        "یا از صفحهٔ هر محصول «تغییرِ دسته» را بزن. نمونه‌لینک و سودِ این دسته فقط رویِ "
        "محصولاتِ داخلِ همین دسته اعمال می‌شود.\n\n" + text,
        reply_markup=kb, parse_mode="HTML")


async def smm_cat_detail_content(bot_id: int, cid: int) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
    if not c or c.bot_id != bot_id:
        return "❌ دسته پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:cat:menu", style=ButtonStyle.DANGER)]])
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    plat_label = await platform_label_for_bot(bot_id, c.platform)
    margin_txt = f"{float(c.margin_pct):g}٪" if c.margin_pct is not None else "پیش‌فرضِ پنل (تنظیم نشده)"
    async with Session() as s:
        n_svc = await s.scalar(select(func.count()).select_from(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.category_pk == cid))
    # === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
    lp_txt = f"<code>{_html_escape(c.link_pattern)}</code>" if c.link_pattern else "تنظیم نشده (چک نمی‌شود)"
    # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
    _lt_supported = smm_link_types.supported(c.platform)
    _lt_keys = await _cat_link_types(c)
    _lt_line = ""
    if _lt_supported:
        _lt_line = ("انواعِ لینکِ مجاز: "
                    + (smm_link_types.short_labels(c.platform, _lt_keys) if _lt_keys
                       else "تنظیم نشده (چک نمی‌شود)") + "\n")
    text = (
        f"📦 <b>دستهٔ «{c.name}»</b>\n\n"
        f"پلتفرم: {plat_label}\n"
        f"وضعیت: {'✅ فعال' if c.is_active else '🔴 غیرفعال'}\n"
        f"سودِ مخصوصِ این دسته: <b>{margin_txt}</b>\n"
        f"{_lt_line}"
        f"نمونه‌لینکِ این دسته: {lp_txt}\n"
        f"توضیحات مشترک دسته: {'✅ ثبت شده' if (c.description or '').strip() else 'ثبت نشده'}\n"
        f"تعدادِ سرویس‌هایِ این دسته: <b>{n_svc or 0}</b>"
    )
    kb_rows = [
        [InlineKeyboardButton(text="✏️ تغییرِ نام", callback_data=f"smm:cat:rename:{cid}")],
        [InlineKeyboardButton(text="📈 تنظیمِ سودِ این دسته", callback_data=f"smm:cat:margin:{cid}")],
        [InlineKeyboardButton(text=("📝 ویرایش توضیحات دسته" if (c.description or "").strip() else "📝 افزودن توضیحات دسته"),
                              callback_data=f"smm:cat:desc:{cid}", style=ButtonStyle.PRIMARY)],
    ]
    if (c.description or "").strip():
        kb_rows.append([InlineKeyboardButton(text="🗑 حذف توضیحات دسته", callback_data=f"smm:cat:descclear:{cid}",
                                             style=ButtonStyle.DANGER)])
    if _lt_supported:   # فعلاً فقط تلگرام و اینستاگرام
        kb_rows.append([InlineKeyboardButton(text="🔗 تنظیمِ نوعِ لینک", callback_data=f"smm:cat:ltypes:{cid}",
                                             style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(text="🔗 الگویِ لینک (نمونه)", callback_data=f"smm:cat:linkpattern:{cid}")])
    if c.link_pattern:
        kb_rows.append([InlineKeyboardButton(
            text="🗑 حذفِ الگویِ لینک", callback_data=f"smm:cat:linkpattern:clear:{cid}", style=ButtonStyle.DANGER)])
    kb_rows += [
        [InlineKeyboardButton(text=("🔴 غیرفعال کن" if c.is_active else "🟢 فعال کن"),
                              callback_data=f"smm:cat:toggle:{cid}")],
        [InlineKeyboardButton(text="🗑 حذفِ دسته", callback_data=f"smm:cat:del:{cid}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:cat:menu", style=ButtonStyle.DANGER)],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    return text, kb


@smm_router.callback_query(F.data.startswith("smm:cat:view:"))
async def smm_cat_view(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    text, kb = await smm_cat_detail_content(row.id, cid)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:cat:rename:"))
async def smm_cat_rename_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.cat_rename)
    await state.update_data(cat_id=cid)
    await cb.message.answer("✏️ اسمِ جدیدِ دسته را بفرست:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:cat:view:{cid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.cat_rename, F.text)
async def smm_cat_rename_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    cid = data.get("cat_id")
    name = msg.text.strip()
    await state.clear()
    if not cid or not name:
        return
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.name = name[:60]
            await s.commit()
    text, kb = await smm_cat_detail_content(row.id, cid)
    await msg.answer("✅ اسم تغییر کرد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_CATEGORY_DESC_V1 === گزارش فدایی: پنل WhaleSMM اصلا توضیحات نمی‌فرستد؛ یک متن فارسی برای کل دسته
# زیر همهٔ محصولات آن دسته نشان داده می‌شود (مگر محصول توضیح پنل/دستی خودش را داشته باشد).
@smm_router.callback_query(F.data.regexp(r"^smm:cat:desc:\d+$"))
async def smm_cat_desc_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
    if not c or c.bot_id != row.id:
        return await cb.answer("دسته پیدا نشد.", show_alert=True)
    await state.set_state(SmmFSM.cat_desc)
    await state.update_data(cat_id=cid)
    cur = f"\n\nمتن فعلی:\n<code>{_html_escape((c.description or '')[:600])}</code>" if (c.description or "").strip() else ""
    await cb.message.answer(
        "📝 <b>توضیحات مشترک دسته</b>\n\nمتنی که می‌خواهی زیر <b>همهٔ محصولات این دسته</b> نشان داده شود را بفرست "
        "(مثلا سرعت، ضمانت، نکات سفارش؛ حداکثر ۱۵۰۰ حرف). اگر محصولی توضیح خودش را داشته باشد، همان نشان داده می‌شود."
        + cur, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:cat:view:{cid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.cat_desc, F.text)
async def smm_cat_desc_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    cid = data.get("cat_id")
    text = (msg.text or "").strip()
    await state.clear()
    if not cid or not text:
        return
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.description = text[:1500]
            await s.commit()
    t, kb = await smm_cat_detail_content(row.id, cid)
    await msg.answer("✅ توضیحات دسته ذخیره شد.\n\n" + t, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.regexp(r"^smm:cat:descclear:\d+$"))
async def smm_cat_desc_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.description = None
            await s.commit()
    t, kb = await smm_cat_detail_content(row.id, cid)
    await cb.answer("🗑 حذف شد")
    try:
        await cb.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(t, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:cat:margin:"))
async def smm_cat_margin_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.cat_margin)
    await state.update_data(cat_id=cid)
    await cb.message.answer(
        "📈 درصدِ سودِ این دسته را بفرست (فقط عدد، مثلاً 30).\n"
        "برایِ برگشتن به سودِ پیش‌فرضِ پنل، عددِ <code>-1</code> را بفرست.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:cat:view:{cid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.cat_margin, F.text)
async def smm_cat_margin_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    cid = data.get("cat_id")
    raw = _norm_num(msg.text.replace("٪", "").replace("%", ""))
    try:
        val = float(raw)
    except ValueError:
        await msg.answer("فقط عدد بفرست. مثال: 30")
        return
    await state.clear()
    if not cid:
        return
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.margin_pct = None if val < 0 else val
            await s.commit()
    text, kb = await smm_cat_detail_content(row.id, cid)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
@smm_router.callback_query(F.data.startswith("smm:cat:linkpattern:clear:"))
async def smm_cat_linkpattern_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.link_pattern = None
            await s.commit()
    await cb.answer("✅ حذف شد؛ دیگه لینک برایِ این دسته چک نمی‌شود.")
    text, kb = await smm_cat_detail_content(row.id, cid)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:cat:linkpattern:"))
async def smm_cat_linkpattern_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    await state.set_state(SmmFSM.cat_linkpattern)
    await state.update_data(cat_id=cid)
    await cb.message.answer(
        "🔗 <b>الگویِ لینکِ این دسته</b>\n\n"
        "یه نمونه لینکِ درست از همین دسته بفرست (مثلاً برایِ اینستاگرام: "
        "<code>https://instagram.com/p/xxxx</code>).\n\n"
        "از این به بعد، وقتی کاربر برایِ سرویس‌هایِ این دسته لینک می‌فرسته، "
        "فقط لینک‌هایی که دامنه‌شون با همینِ نمونه یکی باشه قبول می‌شه — "
        "تا لینکِ پلتفرمِ اشتباه نفرسته.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"smm:cat:view:{cid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@smm_router.message(SmmFSM.cat_linkpattern, F.text)
async def smm_cat_linkpattern_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    cid = data.get("cat_id")
    sample = msg.text.strip()
    await state.clear()
    if not cid or not sample:
        return
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if c and c.bot_id == row.id:
            c.link_pattern = sample[:300]
            await s.commit()
    text, kb = await smm_cat_detail_content(row.id, cid)
    await msg.answer(f"✅ ذخیره شد؛ دامنهٔ موردِقبول: <code>{_html_escape(_link_domain(sample))}</code>\n\n" + text,
                     reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
async def _cat_link_types_screen(bot_id: int, cid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
    if not c or c.bot_id != bot_id or not smm_link_types.supported(c.platform):
        return None
    keys = await _cat_link_types(c)
    plat_label = await platform_label_for_bot(bot_id, c.platform)
    lines = [f"🔗 <b>نوعِ لینکِ دستهٔ «{_html_escape(c.name)}»</b>  ({plat_label})", "",
             "انواعِ لینک‌هایِ قابلِ پشتیبانی:"]
    rows = []
    for key, label, desc, _ex in smm_link_types.type_defs(c.platform):
        on = key in keys
        lines.append(f"• {label}: {'✅ فعال' if on else '❌ غیرفعال'}\n  <i>{desc}</i>")
        rows.append([InlineKeyboardButton(
            text=f"{label} {'✅' if on else '❌'}", callback_data=f"smm:cat:ltog:{cid}:{key}",
            style=ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY)])
    lines += ["", "💡 می‌توانی چندین نوع لینک را هم‌زمان فعال کنی.",
              "اگر هیچ‌کدام فعال نباشد، لینک چک نمی‌شود (یا اگر «الگویِ لینک (نمونه)» ثبت کرده‌ای، "
              "همان چک می‌شود). به‌محضِ فعال‌شدنِ یک نوع، همان جایگزینِ نمونه‌لینک می‌شود و اگر "
              "مشتری نوعِ دیگری بفرستد، ربات لینک‌هایِ مجاز را با نمونه برایش می‌فرستد."]
    if keys:
        rows.append([InlineKeyboardButton(text="♻️ غیرفعال‌کردنِ همه (بدونِ چک)",
                                          callback_data=f"smm:cat:ltog:{cid}:none", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smm:cat:view:{cid}",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_cat_link_types(cb: CallbackQuery, bot_id: int, cid: int) -> None:
    content = await _cat_link_types_screen(bot_id, cid)
    if not content:
        await cb.answer("این دسته پیدا نشد یا پلتفرمش پشتیبانی نمی‌شود.", show_alert=True)
        return
    try:
        await cb.message.edit_text(content[0], reply_markup=content[1], parse_mode="HTML")
    except Exception:
        await cb.message.answer(content[0], reply_markup=content[1], parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:cat:ltypes:"))
async def smm_cat_ltypes(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    try:
        cid = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("نامعتبر", show_alert=True)
    await _show_cat_link_types(cb, row.id, cid)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:cat:ltog:"))
async def smm_cat_ltog(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    try:
        cid, key = int(parts[3]), parts[4]
    except (ValueError, IndexError):
        return await cb.answer("نامعتبر", show_alert=True)
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
    if not c or c.bot_id != row.id or not smm_link_types.supported(c.platform):
        return await cb.answer("این دسته پیدا نشد.", show_alert=True)
    keys = await _cat_link_types(c)
    if key == "none":
        keys = set()
    elif key in smm_link_types.valid_keys(c.platform):
        keys = (keys - {key}) if key in keys else (keys | {key})
    else:
        return await cb.answer("نوعِ نامعتبر", show_alert=True)
    await _set_setting(row.id, _cat_links_key(cid), smm_link_types.dump_keys(keys, c.platform))
    await _show_cat_link_types(cb, row.id, cid)
    await cb.answer("✅ ذخیره شد" if keys else "بدونِ چکِ نوعِ لینک")


@smm_router.callback_query(F.data.startswith("smm:cat:toggle:"))
async def smm_cat_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if not c or c.bot_id != row.id:
            await cb.answer("دسته پیدا نشد", show_alert=True)
            return
        c.is_active = not c.is_active
        await s.commit()
    await cb.answer("✅ ذخیره شد")
    text, kb = await smm_cat_detail_content(row.id, cid)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smm:cat:del:"))
async def smm_cat_del(cb: CallbackQuery, bot: Bot):
    """حذفِ دسته. سرویس‌هایِ این دسته حذف نمی‌شن، فقط category_pkشون خالی
    می‌شه (برمی‌گردن به دسته‌بندیِ خودکار)."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    cid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        c = await s.get(SmmCategory, cid)
        if not c or c.bot_id != row.id:
            await cb.answer("دسته پیدا نشد", show_alert=True)
            return
        await s.execute(update(SmmService).where(SmmService.category_pk == cid).values(category_pk=None))
        await s.delete(c)
        await s.commit()
    await _set_setting(row.id, _cat_links_key(cid), "")   # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
    await cb.answer("🗑 حذف شد")
    text, kb = await smm_categories_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_MANUAL_CATEGORY_V1 (وصل‌کردنِ سرویس به دسته) ===
@smm_router.callback_query(F.data.startswith("smm:svc:cat:"))
async def smm_svc_cat_pick(cb: CallbackQuery, bot: Bot):
    """انتخابِ اینکه این سرویس زیرِ کدوم دستهٔ دستی باشه."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    # === PATCH_SMM_MANUAL_CAT_BTN_ID_V1 ===
    # محافظ: کیبوردهایِ قدیمی که هنوز در چتِ مالک‌ها هستند شناسهٔ متنی
    # دارند و بدونِ این چک، تپ رویشان کلِ هندلر را می‌ترکاند.
    try:
        svc_id = int(parts[3])
    except (ValueError, IndexError):
        logger.warning("smm_svc_cat_pick: شناسهٔ نامعتبر در callback: %r", cb.data)
        await cb.answer("این دکمه قدیمی است. از «✍️ سرویس‌های دستی» دوباره واردِ سرویس شو.",
                        show_alert=True)
        return
    origin = parts[4]
    # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
    # origin=="myd" یعنی از صفحه‌ی جزئیاتِ سرویس (لیستِ صفحه‌بندی‌شده)
    # اومدیم، که یه بخشِ back_page هم بعدش داره.
    back_page = int(parts[5]) if origin == "myd" and len(parts) > 5 else None
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    if not svc or svc.bot_id != row.id:
        await cb.answer("سرویس پیدا نشد", show_alert=True)
        return
    cur_name = None
    if svc.category_pk:
        async with Session() as s:
            c = await s.get(SmmCategory, svc.category_pk)
        cur_name = c.name if c else None
    origin_suffix = f":{back_page}" if back_page is not None else ""
    rows = [[InlineKeyboardButton(text="🚫 بدون دسته (خودکار)", callback_data=f"smm:svc:setcat:{svc_id}:0:{origin}{origin_suffix}")]]
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    _overrides = await _platform_label_overrides(row.id)
    for c in cats:
        mark = "✅ " if svc.category_pk == c.id else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{_overrides.get(c.platform, smm_api.platform_label(c.platform))} {c.name}",
            callback_data=f"smm:svc:setcat:{svc_id}:{c.id}:{origin}{origin_suffix}")])
    if not cats:
        rows.append([InlineKeyboardButton(text="➕ اول یه دسته بساز", callback_data="smm:cat:menu")])
    if origin == "myd":
        back_target = f"smm:svcview:{svc_id}:{back_page}"
    elif origin == "my":
        back_target = "smm:myservices"
    else:
        back_target = "smm:manlist"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_target, style=ButtonStyle.DANGER)])
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    # این یه صفحهٔ انتخاب از لیسته (نه پرسیدنِ متن)، پس باید مثلِ بقیهٔ
    # صفحه‌هایِ ناوبری اول ادیت رو امتحان کنه، نه همیشه پیامِ تازه بفرسته.
    txt = (f"📦 دستهٔ سرویسِ «{svc.name[:40]}» را انتخاب کن"
           + (f"\n(الان: {cur_name})" if cur_name else "\n(الان: بدون دسته — خودکار)"))
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:svc:setcat:"))
async def smm_svc_setcat(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    svc_id, raw_cid, origin = int(parts[3]), parts[4], parts[5]
    back_page = int(parts[6]) if origin == "myd" and len(parts) > 6 else None
    cat_pk = None if raw_cid == "0" else int(raw_cid)
    async with Session() as s:
        svc = await s.get(SmmService, svc_id)
        if not svc or svc.bot_id != row.id:
            await cb.answer("سرویس پیدا نشد", show_alert=True)
            return
        if cat_pk is not None:
            c = await s.get(SmmCategory, cat_pk)
            if not c or c.bot_id != row.id:
                await cb.answer("دسته پیدا نشد", show_alert=True)
                return
        svc.category_pk = cat_pk
        await s.commit()
    await sync_category_platforms(row.id)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1
    if origin == "myd" and back_page is not None:
        # === PATCH_SMM_MYSERVICES_REDESIGN_V1 ===
        await cb.answer("✅ ذخیره شد")
        text, kb = await _smm_myservice_detail_content(row.id, svc_id, back_page)
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    elif origin == "my":
        # cb.data همچنان smm:svc:setcat است و نباید به smm_myservices
        # داده شود؛ آن handler بخش سوم را به‌عنوان شماره صفحه parse می‌کند.
        # بعد از ذخیره، همان صفحهٔ گروه‌های سرویس‌ها را مستقیم بساز.
        await cb.answer("✅ ذخیره شد")
        text, kb = await _smm_myservices_groups_content(row.id)
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await cb.answer("✅ ذخیره شد")
        text, kb = await manual_services_content(row.id)
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ دسته‌بندی سرویس‌های مستقلِ دستی ═══════════════════════
# این سرویس‌ها (گیفت، شمارهٔ مجازی، پرمیوم، استارز، ری‌اکشن و TON)
# مدل SmmService ندارند؛ نگاشت دسته‌شان در Setting ذخیره می‌شود تا migration
# و تغییر در روند فروش/تسویه لازم نباشد.
_SPECIAL_MANUAL_PRODUCTS = (
    ("gift", "🎁 فروشِ گیفتِ تلگرام"),
    ("vnum", "📱 مدیریتِ شمارهٔ مجازی (کشورها)"),
    ("premium", "🌟 فروشِ پرمیومِ تلگرام"),
    ("stars", "⭐ فروشِ استارزِ تلگرام"),
    ("reaction", "⭐️ ری‌اکشنِ استارزیِ پست"),
    ("ton", "💎 فروشِ تون (TON)"),
    # === PATCH_SMM_BOOST_MANUAL_CATEGORY_V1 ===
    # گزارشِ ادمین‌هایِ خدماتِ مجازی: راهنمایِ «اتصالِ محصولات» (بالاترِ همین
    # فایل) از قبل می‌گفت «محصول می‌تواند شامل … بوست … باشد» ولی خودِ
    # بوست تویِ همین لیست نبود — یعنی چیزی که به ادمین وعده داده شده بود،
    # تویِ عمل جا افتاده بود.
    ("boost", "🚀 فروشِ بوستِ تلگرام"),
)


def _special_manual_product_label(key: str) -> str:
    return dict(_SPECIAL_MANUAL_PRODUCTS).get(key, key)


async def _special_manual_category_map(bot_id: int) -> dict[str, int]:
    raw = await _get_setting(bot_id, "smm_special_manual_categories", "{}")
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    out = {}
    for key, value in data.items() if isinstance(data, dict) else []:
        try:
            if key in dict(_SPECIAL_MANUAL_PRODUCTS) and int(value) > 0:
                out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


async def _save_special_manual_category_map(bot_id: int, mapping: dict[str, int]):
    await _set_setting(bot_id, "smm_special_manual_categories", json.dumps(mapping, ensure_ascii=False))


async def _special_manual_categories_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mapping = await _special_manual_category_map(bot_id)
    ids = [int(v) for v in mapping.values()]
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == bot_id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    cat_names = {c.id: c.name for c in cats}
    lines = [
        "📂 <b>دسته‌بندی سرویس‌های مستقل</b>\n",
        "برای هر سرویس دستیِ مستقل یک دسته انتخاب کنید. این تنظیم فقط برای نظم‌دهی است و روند فروش و تأیید سفارش را تغییر نمی‌دهد.\n",
    ]
    rows = []
    for key, label in _SPECIAL_MANUAL_PRODUCTS:
        cid = mapping.get(key)
        current = cat_names.get(cid, "بدون دسته")
        lines.append(f"{label}\n  📂 دسته: <b>{current}</b>")
        rows.append([InlineKeyboardButton(
            text=f"📂 انتخاب دسته برای {label.split(' ', 1)[-1][:28]}",
            callback_data=f"smm:manualcat:pick:{key}")])
    if not cats:
        lines.append("\n⚠️ هنوز دسته‌ای نساخته‌اید. ابتدا از بخش «مدیریت دسته‌ها» یک دسته بسازید.")
    rows.append([InlineKeyboardButton(text="📦 مدیریت دسته‌ها", callback_data="smm:cat:menu")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:manualcat:menu")
async def smm_special_manual_categories_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    text, kb = await _special_manual_categories_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:manualcat:pick:"))
async def smm_special_manual_category_pick(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    key = cb.data.rsplit(":", 1)[-1]
    if key not in dict(_SPECIAL_MANUAL_PRODUCTS):
        await cb.answer("سرویس پیدا نشد", show_alert=True)
        return
    mapping = await _special_manual_category_map(row.id)
    current_id = mapping.get(key)
    async with Session() as s:
        cats = (await s.scalars(select(SmmCategory).where(
            SmmCategory.bot_id == row.id, SmmCategory.is_active == True).order_by(SmmCategory.id))).all()
    rows = [[InlineKeyboardButton(
        text=("✅ " if current_id is None else "") + "🚫 بدون دسته",
        callback_data=f"smm:manualcat:set:{key}:0")]]
    overrides = await _platform_label_overrides(row.id)
    for c in cats:
        mark = "✅ " if current_id == c.id else ""
        plat = overrides.get(c.platform, smm_api.platform_label(c.platform))
        rows.append([InlineKeyboardButton(
            text=f"{mark}{plat} — {c.name}",
            callback_data=f"smm:manualcat:set:{key}:{c.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manualcat:menu", style=ButtonStyle.DANGER)])
    text = f"📂 دستهٔ «{_special_manual_product_label(key)}» را انتخاب کنید:"
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smm:manualcat:set:"))
async def smm_special_manual_category_set(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    key = parts[3] if len(parts) > 3 else ""
    raw_cid = parts[4] if len(parts) > 4 else "0"
    if key not in dict(_SPECIAL_MANUAL_PRODUCTS):
        await cb.answer("سرویس پیدا نشد", show_alert=True)
        return
    try:
        cid = int(raw_cid)
    except ValueError:
        await cb.answer("دسته نامعتبر است", show_alert=True)
        return
    mapping = await _special_manual_category_map(row.id)
    if cid == 0:
        mapping.pop(key, None)
    else:
        async with Session() as s:
            cat = await s.get(SmmCategory, cid)
        if not cat or cat.bot_id != row.id or not cat.is_active:
            await cb.answer("دسته پیدا نشد", show_alert=True)
            return
        mapping[key] = cid
    await _save_special_manual_category_map(row.id, mapping)
    await cb.answer("✅ دسته ذخیره شد")
    text, kb = await _special_manual_categories_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ سرویس‌های دستی ═══════════════════════

# === PATCH_SMM_INPUT_KIND_V1 ===
# درخواستِ فدایی/سهیل: «محصولِ دستی (میو پوینت) آدرسِ سفارش را به‌صورتِ عدد بگیرد، نه لینک.» هر سرویس یک «نوعِ ورودیِ سفارش» دارد:
#   link (پیش‌فرض) · number (فقط عدد: آیدی/شماره) · text (متنِ آزاد)
_INPUT_KINDS = ("link", "number", "text")
_INPUT_KIND_FA = {"link": "🔗 لینک", "number": "🔢 عدد (آیدی)", "text": "✏️ متنِ آزاد"}
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _input_kind(v) -> str:
    k = (getattr(v, "input_kind", "") or "link").lower()
    return k if k in _INPUT_KINDS else "link"


def _norm_number_input(raw: str) -> str | None:
    """«۱۲۳٬۴۵۶» / «123 456» ← «123456»؛ هر چیزِ غیرعددی ⇒ None."""
    t = (raw or "").translate(_FA_DIGITS)
    t = "".join(ch for ch in t if ch not in " ,،٬\u200c\u200f\u200e")
    return t if t.isdigit() and 1 <= len(t) <= 40 else None


def _input_prompt(kind: str, link_prompt: str) -> str:
    if kind == "number":
        return "آیدی/شماره‌ی موردنظرت را بفرست (فقط عدد):"
    if kind == "text":
        return "اطلاعاتِ موردنیاز را به‌صورتِ متن بفرست:"
    return link_prompt


async def manual_services_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبوردِ «سرویس‌های دستی» — مشترک بین callback و action مستقل."""
    await ensure_smm_columns()
    try:
        async with Session() as s:
            svcs = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == bot_id, SmmService.is_manual == True)
                .order_by(SmmService.id))).all()
    except Exception as e:
        print("manual_services_content query error:", e)
        # تلاش دوباره برای migration و سپس کوئری امن‌تر
        await ensure_smm_columns(force=True)
        try:
            async with Session() as s:
                svcs = (await s.scalars(select(SmmService).where(
                    SmmService.bot_id == bot_id, SmmService.is_manual == True)
                    .order_by(SmmService.id))).all()
        except Exception as e2:
            print("manual_services_content retry failed:", e2)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)]])
            return ("✍️ <b>سرویس‌های دستی</b>\n\n"
                    "⚠️ برای فعال‌سازی این بخش، یک‌بار ربات را ری‌استارت کن.\n"
                    "اگر باز هم خطا دیدی به پشتیبانی اطلاع بده."), kb
    lines = ["✍️ <b>سرویس‌های دستی</b>\n",
             "این سرویس‌ها به پنل وصل نیستند؛ سفارششان همیشه برای تأیید ادمین می‌آید.\n"]
    # دستهٔ انتخاب‌شدهٔ هر سرویس دستی را برای نمایش و مدیریت شفاف می‌خوانیم.
    _manual_cat_ids = {v.category_pk for v in svcs if getattr(v, "category_pk", None)}
    _manual_cat_names = {}
    if _manual_cat_ids:
        async with Session() as _cs:
            _manual_cats = (await _cs.scalars(select(SmmCategory).where(
                SmmCategory.bot_id == bot_id,
                SmmCategory.id.in_(_manual_cat_ids)))).all()
        _manual_cat_names = {c.id: c.name for c in _manual_cats}
    rows = [[InlineKeyboardButton(text="➕ ساخت سرویس دستی", callback_data="smm:mannew")]]
    rows.append([InlineKeyboardButton(
        text="📂 دسته‌بندی سرویس‌های مستقل",
        callback_data="smm:manualcat:menu")])
    # === PATCH_MANUAL_HUB_MERGE_V1 ===
    # این‌ها هم به هیچ پنلِ APIای وصل نیستن (گیفت/پرمیوم/استارز از طریقِ
    # خودِ تلگرام کار می‌کنن، نه از طریقِ پنل SMM) — قبلاً زیرِ «اتصال به
    # پنل» بودن که گیج‌کننده بود؛ الان همه‌ی محصولاتِ «بدونِ پنل» یک‌جان.
    rows.append([InlineKeyboardButton(text="🎁 فروشِ گیفتِ تلگرام", callback_data="gift:menu", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="📱 مدیریتِ شمارهٔ مجازی (کشورها)", callback_data="vnum:menu:0", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🌟 فروشِ پرمیومِ تلگرام", callback_data="prm:menu", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⭐ فروشِ استارزِ تلگرام", callback_data="ssl:menu", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⭐️ ری‌اکشنِ استارزیِ پست", callback_data="srx:menu", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="💎 فروشِ تون (TON)", callback_data="ton:menu", style=ButtonStyle.PRIMARY)])
    # === PATCH_SMM_BOOST_HUB_V1 ===
    # درخواستِ ادمین: بوست هم دقیقاً همین‌جا یه دکمهٔ اختصاصی داشته باشه،
    # کنارِ گیفت/پرمیوم/استارز/تون. برخلافِ اون‌ها (که موتورِ کاملاً
    # جداگونه دارن چون از API واقعیِ تلگرام تحویل داده می‌شن)، بوست از
    # همون موتورِ SmmService استفاده می‌کنه — چون هم می‌تونه به پنل وصل
    # باشه هم دستی، دقیقاً مثلِ بقیه‌ی پلتفرم‌ها؛ فقط یه صفحهٔ هابِ
    # اختصاصی روش گذاشته شده که هر دو مسیر رو یک‌جا در دسترس می‌ذاره.
    rows.append([InlineKeyboardButton(text="🚀 فروشِ بوستِ تلگرام", callback_data="smm:boostmenu", style=ButtonStyle.PRIMARY)])
    if not svcs:
        lines.append("هنوز سرویس دستی‌ای نساخته‌ای.")
    for v in svcs:
        pr = f"{int(v.sell_rate):,} ت/هزار" if v.sell_rate else "بدون قیمت"
        _cat_name = _manual_cat_names.get(getattr(v, "category_pk", None), "بدون دسته")
        lines.append(f"• [{smm_api.service_type_label(smm_api.detect_service_type(v.name, v.category or ''))}] "
                     f"{v.name[:38]} — {pr}\n  📂 دسته‌بندی: {_cat_name} · ورودیِ سفارش: {_INPUT_KIND_FA[_input_kind(v)]}")
        rows.append([
            InlineKeyboardButton(text=f"🗑 حذف: {v.name[:22]}", callback_data=f"smm:mandel:{v.id}"),
            # === PATCH_SMM_MANUAL_CATEGORY_FLOW_V2 ===
            InlineKeyboardButton(text="📂 دسته‌بندی", callback_data=f"smm:svc:cat:{v.id}:man"),
        ])
        # === PATCH_SMM_INPUT_KIND_V1 ===
        rows.append([InlineKeyboardButton(
            text=f"✏️ ورودیِ سفارش: {_INPUT_KIND_FA[_input_kind(v)]} (بزن تا عوض شود)",
            callback_data=f"smm:inpk:{v.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:manlist")
async def smm_manlist(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await cb.answer()
    text, kb = await manual_services_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ هابِ اختصاصیِ «فروشِ بوستِ تلگرام» ═══════════════════════
# === PATCH_SMM_BOOST_HUB_V1 ===
# ظاهرش مثلِ گیفت/پرمیوم/استارز/تون یه دکمهٔ اختصاصیِ خودشه، ولی زیرش
# همون موتورِ SmmService (پنل یا دستی) کار می‌کنه — چون بوست، برخلافِ
# اون‌ها، هیچ APIِ رسمیِ تحویلِ خودکارِ تلگرامی نداره و همیشه یا از
# طریقِ پنلِ SMMِ بیرونی یا کاملاً دستی انجام می‌شه؛ دقیقاً همون دو
# حالتی که SmmService از اول ساپورت می‌کنه.
async def _boost_hub_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        boost_svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id))).all()
    boost_svcs = [v for v in boost_svcs if _svc_platform_type(v)[0] == "boost"]
    n_panel = sum(1 for v in boost_svcs if not v.is_manual)
    n_manual = sum(1 for v in boost_svcs if v.is_manual)
    lines = [
        "🚀 <b>فروشِ بوستِ تلگرام</b>\n",
        "بوست برخلافِ گیفت/پرمیوم/استارز، APIِ رسمیِ تحویلِ خودکار از "
        "طریقِ خودِ تلگرام نداره؛ پس همیشه یا از طریقِ یک پنلِ SMMِ "
        "بیرونی، یا کاملاً دستی (تأییدِ ادمین) انجام می‌شه.\n",
        f"📊 در حالِ حاضر: <b>{n_panel}</b> سرویسِ وصل‌به‌پنل، <b>{n_manual}</b> سرویسِ دستی.",
    ]
    rows = [
        [InlineKeyboardButton(text="➕ ساختِ سرویسِ دستیِ بوست", callback_data="smm:mannew:boost")],
        [InlineKeyboardButton(text="🔌 اتصال/دریافتِ بوست از یک پنلِ SMM", callback_data="smm:panels")],
    ]
    if boost_svcs:
        rows.append([InlineKeyboardButton(
            text=f"📂 مدیریتِ سرویس‌هایِ بوستِ موجود ({len(boost_svcs)})",
            callback_data="smm:mysvc:plat:boost")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "smm:boostmenu")
async def smm_boost_hub(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    text, kb = await _boost_hub_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "smm:mannew:boost")
async def smm_mannew_boost(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_SMM_BOOST_HUB_V1 ===
    دقیقاً همون فلوِ چهارمرحله‌ایِ smm_mannew، با این فرق که پلتفرم/نوع
    رو از رویِ اسم حدس نمی‌زنیم — چون از همینجا اومده، صراحتاً «بوست»
    ثبت می‌شه، حتی اگه ادمین اسمی بدون کلمهٔ «بوست» انتخاب کنه."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmFSM.man_name)
    await state.update_data(force_platform="boost", force_type="boost")
    from tenant import smart_cancel_kb
    await cb.message.answer(
        "🚀 <b>ساختِ سرویسِ دستیِ بوست</b> — گام ۱ از ۴\n\n"
        "نام سرویس را بفرست (مثلاً: <code>بوست کانال ۱ الی ۵ روزه</code>).",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()



@smm_router.callback_query(F.data.startswith("smm:mandel:"))
async def smm_mandel(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    vid = int(cb.data.split(":")[2])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if v and v.bot_id == row.id and v.is_manual:
            await s.delete(v)
            await s.commit()
    await cb.answer("حذف شد")
    await smm_manlist(cb, bot)


@smm_router.callback_query(F.data.startswith("smm:inpk:"))
async def smm_input_kind_cycle(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_INPUT_KIND_V1 === چرخش: لینک ← عدد ← متنِ آزاد (فقط سرویس‌هایِ دستیِ همین ربات)."""
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await ensure_smm_columns()
    vid = int(cb.data.split(":")[2])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if not v or v.bot_id != row.id or not v.is_manual:
            await cb.answer("سرویس پیدا نشد.", show_alert=True)
            return
        nxt = _INPUT_KINDS[(_INPUT_KINDS.index(_input_kind(v)) + 1) % len(_INPUT_KINDS)]
        v.input_kind = nxt
        await s.commit()
    await cb.answer(f"ورودیِ سفارش: {_INPUT_KIND_FA[nxt]}")
    text, kb = await manual_services_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "smm:mannew")
async def smm_mannew(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(SmmFSM.man_name)
    from tenant import smart_cancel_kb
    await cb.message.answer(
        "✍️ <b>ساخت سرویس دستی</b> — گام ۱ از ۴\n\n"
        "نام سرویس را بفرست.\n"
        "💡 برای دسته‌بندی خودکار، بهتر است نام شامل پلتفرم و نوع باشد؛ "
        "مثال: <code>افزایش ممبر تلگرام کیفیت بالا</code> یا "
        "<code>Instagram Followers Real</code>.",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.man_name, F.text)
async def smm_man_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    name = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if name in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if len(name) < 2:
        await msg.answer("نام خیلی کوتاه است. دوباره بفرست.")
        return
    await state.update_data(man_name=name)
    await state.set_state(SmmFSM.man_price)
    from tenant import smart_cancel_kb
    await msg.answer("گام ۲ از ۴ — قیمت فروش را به <b>تومان به‌ازای هر ۱۰۰۰ عدد</b> بفرست (فقط عدد):",
                     parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.man_price, F.text)
async def smm_man_price_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit() or int(raw) <= 0:
        await state.clear()
        raise SkipHandler
    await state.update_data(man_price=int(raw))
    await state.set_state(SmmFSM.man_min)
    from tenant import smart_cancel_kb
    await msg.answer("گام ۳ از ۴ — حداقل تعداد سفارش را بفرست (فقط عدد، مثلاً 1000):",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.man_min, F.text)
async def smm_man_min_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit():
        await state.clear()
        raise SkipHandler
    await state.update_data(man_min=int(raw))
    await state.set_state(SmmFSM.man_max)
    from tenant import smart_cancel_kb
    await msg.answer("گام ۴ از ۴ — حداکثر تعداد سفارش را بفرست (فقط عدد، مثلاً 100000):",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.man_max, F.text)
async def smm_man_max_in(msg: Message, bot: Bot, state: FSMContext):
    await ensure_smm_columns()
    row = await _owner(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit():
        await state.clear()
        raise SkipHandler
    mx = int(raw)
    data = await state.get_data()
    name = data.get("man_name", "")
    price = int(data.get("man_price", 0))
    mn = int(data.get("man_min", 0))
    if mx and mn and mx < mn:
        await msg.answer("حداکثر نباید کمتر از حداقل باشد. دوباره حداکثر را بفرست.")
        return
    # شناسهٔ یکتا برای سرویس دستی
    import time as _t
    sid = f"man{int(_t.time())}"
    cat = ""  # دسته‌بندی از روی نام تشخیص داده می‌شود
    # === PATCH_SMM_STORED_CLASSIFICATION_V1 ===
    # برایِ سرویسِ دستی، چون مرحله‌ی «مرورِ پنل» وجود نداره، همچنان با
    # حدسِ نام تعیین می‌شه — ولی حداقل یک‌بار موقعِ ساخت ذخیره می‌شه، نه
    # هر بار تویِ صفحه‌ی «سرویس‌هایِ من».
    _plat_guess = smm_api.detect_platform(name)
    _type_guess = smm_api.detect_service_type(name)
    # === PATCH_SMM_BOOST_HUB_V1 ===
    # اگه از هابِ اختصاصیِ بوست (یا هر مسیرِ مشابهِ آینده) اومده باشیم،
    # پلتفرم/نوع صراحتاً مشخص شده و نباید حدسِ رویِ اسم جایگزینش بشه —
    # حدس فقط برایِ مسیرِ عمومیِ «➕ ساختِ سرویسِ دستی» لازم است.
    if data.get("force_platform"):
        _plat_guess = data["force_platform"]
    if data.get("force_type"):
        _type_guess = data["force_type"]
    # === PATCH_SMM_MANUAL_CAT_BTN_ID_V1 ===
    # باگ: دکمهٔ «📂 انتخاب دسته‌بندی» با `sid` ساخته می‌شد — یعنی
    # `service_id` متنی (مثلِ man1788816335) نه کلیدِ اصلیِ عددیِ ردیف.
    # هندلرِ smm_svc_cat_pick اولین کارش `int(parts[3])` است، پس تپ رویِ
    # دکمه با `invalid literal for int(): 'man1788816335'` می‌ترکید. در
    # error.log چهار بار دیده شد. جایِ دیگری که همین دکمه ساخته می‌شود
    # (خطِ ~۵۸۴۶) درست از `v.id` استفاده می‌کند.
    async with Session() as s:
        _svc = SmmService(
            bot_id=row.id, service_id=sid, name=name, category=cat,
            panel_rate=0, sell_rate=price, min_qty=mn, max_qty=mx,
            is_active=True, is_manual=True,
            platform_key=_plat_guess, type_key=_type_guess)
        s.add(_svc)
        await s.commit()
        new_id = _svc.id          # کلیدِ اصلیِ عددی، بعد از commit
    await state.clear()
    p = smm_api.platform_label(_plat_guess)
    t = smm_api.service_type_label(_type_guess)
    await msg.answer(
        f"✅ سرویس دستی ساخته شد.\n\n"
        f"🛰 {name}\n📂 دسته‌بندی: بدون دسته\n"
        f"🧭 تشخیص خودکار: {p} · {t}\n💵 {price:,} ت/هزار\n🔢 {mn} تا {mx}\n\n"
        f"اکنون می‌توانی آن را به یکی از دسته‌های ساخته‌شده اضافه کنی؛ "
        f"در غیر این صورت بدون دسته باقی می‌ماند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 انتخاب دسته‌بندی", callback_data=f"smm:svc:cat:{new_id}:man")],
            [InlineKeyboardButton(text="🔢 ورودیِ سفارش: لینک (برایِ عدد/آیدی بزن)", callback_data=f"smm:inpk:{new_id}")],
            [InlineKeyboardButton(text="✍️ سرویس‌های دستی", callback_data="smm:manlist")]]))




async def show_smm_services_to_user(bot: Bot, msg_or_cb, bot_id: int):
    """مرحلهٔ ۱: نمایش پلتفرم‌های موجود (تلگرام، اینستا، یوتیوب، ...) به کاربر."""
    await ensure_smm_columns()
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True)
            .order_by(SmmService.id))).all()
    is_cb = isinstance(msg_or_cb, CallbackQuery)
    target = msg_or_cb.message if is_cb else msg_or_cb
    if not svcs:
        await target.answer("🛒 در حال حاضر سرویسی برای فروش موجود نیست.")
        return
    tree = await classified_tree(bot_id, svcs)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    _overrides = await _platform_label_overrides(bot_id)
    # تعداد سرویس هر پلتفرم برای نمایش
    rows = []
    for pkey, types in tree.items():
        cnt = sum(len(x) for x in types.values())
        rows.append([InlineKeyboardButton(
            text=f"{_overrides.get(pkey, smm_api.platform_label(pkey))} ({cnt})",
            callback_data=f"smmu:plat:{pkey}")])
    rows.append([InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="smmu:myorders")])
    # === PATCH_SMM_AI_ASSISTANT_V1 ===
    from tenant.bots.virtual_service.smm_ai import _get_setting as _ai_setting
    if (await _ai_setting(bot_id, "smm_ai_enabled", "0")) == "1":
        rows.append([InlineKeyboardButton(text="🤖 پرسش از دستیارِ هوشمند", callback_data="smmai:ask")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:home", style=ButtonStyle.DANGER)])
    text = "🌐 <b>خدمات شبکه‌های اجتماعی</b>\n\nاول پلتفرم را انتخاب کن:"
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    # === PATCH_SMM_EDIT_NOT_NEW_V1 ===
    # قبلاً این تابع همیشه پیامِ جدید می‌فرستاد، حتی وقتی از دکمهٔ «بازگشت»
    # صدا زده می‌شد — دقیقاً همون باگی که گزارش شد: «هر بخشی رو می‌ری همون
    # دکمه ادیت شه، نه پیامِ جدید». الان اگه از یه دکمه اومده باشیم، اول
    # سعی می‌کنیم همون پیام رو ادیت کنیم.
    if is_cb:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_MYORDERS_BUTTON_ACTION_V1 ===
# درخواستِ ادمین: امکانِ وصل‌کردنِ یه دکمهٔ صفحهٔ اصلی مستقیم به «سفارش‌های
# من» — قبلاً این صفحه فقط از داخلِ منویِ SMM (بعد از انتخابِ پلتفرم)
# قابل‌دسترس بود. منطقش تکرار نشده، همون _smm_myorders_paginated_content
# که خودِ دکمهٔ اینلاینِ smmu:myorders هم استفاده می‌کنه.
async def show_smm_myorders_to_user(bot: Bot, msg_or_cb, bot_id: int):
    is_cb = isinstance(msg_or_cb, CallbackQuery)
    target = msg_or_cb.message if is_cb else msg_or_cb
    tg_id = msg_or_cb.from_user.id
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    # این تابع نقطهٔ ورودِ مستقیم است (دکمهٔ صفحهٔ اصلی)، نه ناوبریِ داخلیِ
    # فروشگاه — پس «بازگشت» باید به خانهٔ واقعیِ ربات برود، نه خانهٔ فروشگاه.
    text, kb = await _smm_myorders_paginated_content(bot_id, tg_id, 1, home_cb="mbtn:home")
    if is_cb:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


_SMM_STATUS_LABEL = {
    "awaiting_admin": "⏳ در انتظار تأیید ادمین",
    "sent":           "🚀 ثبت‌شده روی پنل / در حال انجام",
    "completed":      "✅ تکمیل‌شده",
    "failed":         "❌ ناموفق",
    "canceled":       "❌ لغوشده",
    "cancelled":      "❌ لغوشده",
    "refunded":       "↩️ لغو و وجه برگشت داده‌شد",
    "partial":        "🟠 نیمه‌تمام (بخشی تحویل شد، مابقی برگشت)",   # === PATCH_SMM_PARTIAL_V1 ===
}

# === PATCH_SMM_CUSTOMER_ORDERS_TABLE_V1 ===
# درخواستِ ادمین: فرمتِ قبلی (یک دکمهٔ فشرده به‌ازایِ هر سفارش) به‌هم‌ریخته
# به‌نظر می‌رسید. الان دقیقاً مثلِ جدولِ سفارشاتِ ادمین (شناسه/لینک/وضعیت)
# ولی با ترتیبِ خواسته‌شده «وضعیت | لینک | قیمت | شناسه» و رنگِ متناسب با
# وضعیت — مشتری هنوزم با زدنِ هر ردیف جزئیاتِ کامل و کدِ پیگیری رو می‌بینه.
# === PATCH_SMM_MYORDERS_PAGE_SIZE_MATCH_V1 ===
# درخواستِ ادمین: صفحه‌بندیِ «سفارش‌های من» (سمتِ مشتری) هم مثلِ لیستِ
# سفارشاتِ ادمین (که از قبل ۱۵تایی بود) هر صفحه ۱۵ ردیف نشان بدهد، نه ۱۰.
_SMM_MYORDERS_PAGE_SIZE = 15


async def _smm_myorders_paginated_content(bot_id: int, user_id: int, page: int,
                                          home_cb: str = "smmu:home",
                                          warranty: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    گزارشِ ادمین (فدایی): وقتی «سفارش‌هایِ من» مستقیم به یک دکمهٔ صفحهٔ
    اصلی وصل می‌شود (نه از دلِ خودِ فروشگاه)، دکمهٔ «بازگشت» کاربر را به
    «smmu:home» (صفحهٔ اصلیِ *فروشگاه*، یعنی لیستِ همهٔ پلتفرم‌ها) می‌برد —
    که از دیدِ کاربر «یه جایِ دیگه»‌ست، نه خانهٔ واقعیِ ربات. home_cb
    مقصدِ واقعیِ دکمه را مشخص می‌کند؛ پیش‌فرض همان رفتارِ قبلی (برایِ
    ناوبریِ داخلیِ فروشگاه) است، فقط نقطهٔ ورودِ مستقیم (
    show_smm_myorders_to_user) آن را به mbtn:home (خانهٔ واقعی) عوض می‌کند.
    """
    page = max(1, page)
    # === PATCH_SMM_REFILL_V1 === «سفارشاتِ گارانتی‌دار»: فقط سفارش‌هایی که ریفیل‌پذیرند و ارائه‌دهنده
    # برایِ سرویسشان ریفیل دارد (یا نامعلوم است). فهرستِ شناسه‌ها اول ساخته می‌شود، بعد صفحه‌بندی.
    _warr_ids = None
    if warranty:
        _warr_ids = await _warranty_order_ids(bot_id, user_id)
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    _suffix = ":mbtn" if home_cb == "mbtn:home" else ""
    if _warr_ids is None:
        # === PATCH_CUSTOMER_UNIFIED_ORDERS_V1 === سفارش‌های استارز/تون/گیفت/ری‌اکشن/پریمیوم هم در «سفارش‌های من» می‌آیند
        total, page, total_pages, items = await _my_unified_page(bot_id, user_id, page)
    else:
        async with Session() as s:
            base_q = select(SmmOrder).where(
                SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == user_id,
                SmmOrder.id.in_(_warr_ids or [-1]))
            total = await s.scalar(select(func.count()).select_from(base_q.subquery()))
            total = int(total or 0)
            total_pages = max(1, (total + _SMM_MYORDERS_PAGE_SIZE - 1) // _SMM_MYORDERS_PAGE_SIZE)
            page = min(page, total_pages)
            items = [("s", o) for o in (await s.scalars(
                base_q.order_by(SmmOrder.id.desc())
                .offset((page - 1) * _SMM_MYORDERS_PAGE_SIZE).limit(_SMM_MYORDERS_PAGE_SIZE)
            )).all()]

    _title = "🛡 <b>سفارشاتِ گارانتی‌دار</b>" if warranty else "📦 <b>سفارش‌های من</b>"
    if not items and warranty:
        text = (f"{_title}\n\nسفارشی که ارائه‌دهنده برایش ریفیل (جبرانی) داشته باشد پیدا نشد.\n"
                "(فقط سفارش‌هایِ ثبت‌شده رویِ پنل و تکمیل‌شده/در حال انجام بررسی می‌شوند.)")
        rows = []
    elif not items:
        text = "📦 <b>سفارش‌های من</b>\n\nهنوز هیچ سفارشی ثبت نکرده‌ای."
        rows = []
    else:
        text = (f"{_title} (صفحهٔ {page} از {total_pages})\n"
                + ("این سفارش‌ها ریفیل (جبرانی) دارند؛ روی هر ردیف بزن و «🔁 درخواستِ ریفیل» را انتخاب کن:"
                   if warranty else
                   "هر ردیف: وضعیت | لینک | قیمت | شناسه — روی هر ردیف بزن تا جزئیات و کدِ پیگیری‌اش رو ببینی:"))
        rows = [[
            InlineKeyboardButton(text="≡ وضعیت ≡", callback_data="noop"),
            InlineKeyboardButton(text="≡ لینک ≡", callback_data="noop"),
            InlineKeyboardButton(text="≡ قیمت ≡", callback_data="noop"),
            InlineKeyboardButton(text="≡ شناسه ≡", callback_data="noop"),
        ]]
        for _typ, o in items:
            if _typ == "g":      # === PATCH_CUSTOMER_UNIFIED_ORDERS_V1 === استارز/تون/گیفت/ری‌اکشن/پریمیوم
                _gl = _VS_GIFT_STATUS_LABELS.get((o.status or "").lower(), o.status or "نامشخص")
                _gi = _vs_gift_item_label(o)
                if len(_gi) > 16:
                    _gi = _gi[:13] + "…"
                _gcb = f"smmu:gview:{o.id}:{page}{_suffix}"
                _gs = _vs_row_style(o.status, "g")
                rows.append([
                    InlineKeyboardButton(text=_gl.split()[0], callback_data=_gcb, style=_gs),
                    InlineKeyboardButton(text=_gi, callback_data=_gcb, style=_gs),
                    InlineKeyboardButton(text=f"{int(o.price_toman or 0) * max(1, int(o.quantity or 1)):,}ت",
                                         callback_data=_gcb, style=_gs),
                    InlineKeyboardButton(text=str(o.tracking_code or o.id), callback_data=_gcb, style=_gs),
                ])
                continue
            label = _SMM_STATUS_LABEL.get((o.status or "").lower(), o.status or "نامشخص")
            link_short = (o.link or "—")
            if len(link_short) > 16:
                link_short = link_short[:13] + "…"
            row_style = _vs_row_style(o.status, "s")   # === PATCH_ORDER_LIST_COLORS_V2 ===
            # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
            cb = f"smmu:oview:{o.id}:{page}{_suffix}{':w' if warranty else ''}"
            rows.append([
                InlineKeyboardButton(text=label.split()[0], callback_data=cb, style=row_style),
                InlineKeyboardButton(text=link_short, callback_data=cb, style=row_style),
                InlineKeyboardButton(text=f"{int(o.price_toman):,}ت", callback_data=cb, style=row_style),
                # PATCH_ORDER_CODE_UNIFIED_V1 — همان کدِ پیگیریِ لیستِ ادمین (نه شناسهٔ داخلیِ دیتابیس)
                InlineKeyboardButton(text=str(o.tracking_code or o.id), callback_data=cb, style=row_style),
            ])
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    # home_cb باید در ورق‌زدنِ صفحه هم حفظ شود، وگرنه صفحهٔ ۲ به بعد
    # دوباره به «خانهٔ فروشگاه» برمی‌گردد حتی اگر ورودی مستقیم بوده.
    _lc = "smmu:warr" if warranty else "smmu:myorders"
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"{_lc}:{page-1}{_suffix}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"{_lc}:{page+1}{_suffix}"))
    if nav:
        rows.append(nav)
    if not warranty:
        # === PATCH_SMM_REFILL_V1 === میان‌برِ «سفارشاتِ گارانتی‌دار» (فقط اگر سفارشی ریفیل‌پذیر داشته باشد)
        try:
            if total and await _has_refillable_orders(bot_id, user_id):
                rows.append([InlineKeyboardButton(text="🛡 سفارشاتِ گارانتی‌دار",
                                                  callback_data=f"smmu:warr:1{_suffix}", style=ButtonStyle.SUCCESS)])
        except Exception:
            logger.exception("[refill] ساختِ دکمهٔ سفارشاتِ گارانتی‌دار ناموفق بود")
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=home_cb, style=ButtonStyle.DANGER)])
    else:
        rows.append([InlineKeyboardButton(text="📦 همهٔ سفارش‌ها", callback_data=f"smmu:myorders:1{_suffix}",
                                          style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=home_cb, style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _my_unified_page(bot_id: int, user_id: int, page: int):
    """(total, page, total_pages, items) — فهرست ادغام‌شدهٔ «سفارش‌های من»: SMM ('s') + GiftOrder ('g').
    === PATCH_CUSTOMER_UNIFIED_ORDERS_V1 ===
    گزارش فدایی: «سفارشات استارز و تون و گیفت و ری‌اکشن استارزی توی لیست سفارشات من کاربر نمی‌آید».
    «سفارش‌های من» فقط SmmOrder را می‌خواند، ولی این انواع در GiftOrder ذخیره می‌شوند (لیست ادمین از قبل
    ادغام بود). همان روش ادغام ادمین: از هر نوع تا انتهای صفحهٔ خواسته‌شده می‌خوانیم، با زمان ثبت مرتب
    می‌کنیم و صفحه را می‌بریم."""
    size = _SMM_MYORDERS_PAGE_SIZE
    need = max(1, page) * size
    async with Session() as s:
        buyer_pk = await s.scalar(select(BotUser.id).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == user_id))
        qs = [("s", select(SmmOrder).where(SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == user_id), SmmOrder)]
        if buyer_pk is not None:
            qs.append(("g", select(GiftOrder).where(
                GiftOrder.bot_id == bot_id, GiftOrder.buyer_user_id == buyer_pk), GiftOrder))
        total = 0
        merged = []
        for typ, q, model in qs:
            total += int(await s.scalar(select(func.count()).select_from(q.subquery())) or 0)
            rows = (await s.scalars(q.order_by(model.created_at.desc(), model.id.desc()).limit(need))).all()
            merged += [(typ, o) for o in rows]
    merged.sort(key=lambda t: (t[1].created_at or datetime.min, t[1].id), reverse=True)
    total_pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), total_pages)
    return total, page, total_pages, merged[(page - 1) * size: page * size]


async def _smm_my_gift_detail_content(bot_id: int, user_id: int, oid: int, back_page: int,
                                      home_cb: str = "smmu:home") -> tuple[str, InlineKeyboardMarkup]:
    """جزئیات یک سفارش استارز/تون/گیفت/ری‌اکشن/پریمیوم برای خود مشتری (بدون اطلاعات داخلی).
    === PATCH_CUSTOMER_UNIFIED_ORDERS_V1 ==="""
    async with Session() as s:
        o = await s.get(GiftOrder, oid)
        buyer = await s.get(BotUser, o.buyer_user_id) if o else None
    _suffix = ":mbtn" if home_cb == "mbtn:home" else ""
    back_btn = InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=f"smmu:myorders:{back_page}{_suffix}",
                                    style=ButtonStyle.DANGER)
    if not o or o.bot_id != bot_id or buyer is None or buyer.telegram_id != user_id:
        return "❌ این سفارش پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[[back_btn]])
    from tenant import _jalali_date_time
    date_str, time_str = _jalali_date_time(o.created_at)
    qty = max(1, int(o.quantity or 1))
    label = _VS_GIFT_STATUS_LABELS.get((o.status or "").lower(), o.status or "نامشخص")
    lines = [
        "📦 <b>جزئیات سفارش</b>\n",
        f"🛍 محصول: {_html_escape(_vs_gift_item_label(o))}",
        f"🎯 مقصد: {_html_escape(o.dest_label or '—')}",
        f"💰 مبلغ پرداختی: {int(o.price_toman or 0) * qty:,} تومان",
        f"📊 وضعیت: {label}",
        f"🔖 کد پیگیری: <code>{o.tracking_code or o.id}</code>",
        f"🗓 تاریخ ثبت: {date_str} {time_str}",
    ]
    if qty > 1 and int(o.sent_qty or 0) and (o.status or "") not in ("sent", "completed", "done"):
        lines.insert(4, f"📦 ارسال‌شده: {int(o.sent_qty or 0)} از {qty}")
    rows = []
    if o.tracking_code:
        rows.append([InlineKeyboardButton(text="📞 پشتیبانی (پیگیری این سفارش)",
                                          callback_data=f"gift:support:{o.tracking_code}")])
    rows.append([back_btn])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smmu:gview:"))
async def smmu_gift_order_view(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    try:
        oid, back_page = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await cb.answer()
        return
    home_cb = "mbtn:home" if "mbtn" in parts[4:] else "smmu:home"
    text, kb = await _smm_my_gift_detail_content(row.id, cb.from_user.id, oid, back_page, home_cb=home_cb)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


async def _refillable_orders(bot_id: int, user_id: int):
    """سفارش‌هایِ کاربر که اصلاً ریفیل‌پذیرند (رویِ پنل ثبت شده و در وضعیتِ مناسب)."""
    from tenant.bots.virtual_service import smm_refill as _rf
    async with Session() as s:
        return (await s.scalars(select(SmmOrder).where(
            SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == user_id,
            SmmOrder.status.in_(_rf.REFILLABLE_ORDER_STATUSES),
            SmmOrder.panel_order_id.isnot(None)).order_by(SmmOrder.id.desc()).limit(500))).all()


async def _warranty_order_ids(bot_id: int, user_id: int) -> list[int]:
    """شناسهٔ سفارش‌هایی که ارائه‌دهنده برایِ سرویسشان ریفیل دارد. برایِ هر پنل حداکثر یک‌بار فهرستِ
    سرویس‌ها گرفته می‌شود (کش‌شده)؛ نامعلوم (پنل پرچم نمی‌دهد/قطع است) هم شامل می‌شود."""
    from tenant.bots.virtual_service import smm_refill as _rf
    orders = await _refillable_orders(bot_id, user_id)
    out: list[int] = []
    creds: dict = {}
    for o in orders:
        if o.panel_pk not in creds:
            creds[o.panel_pk] = await _panel_creds(bot_id, o.panel_pk)
        api_url, api_key, _ = creds[o.panel_pk]
        sup = await _rf.support(api_url, api_key, o.service_id)
        if sup is not False:
            out.append(o.id)
    return out


async def _has_refillable_orders(bot_id: int, user_id: int) -> bool:
    """بدونِ شبکه: آیا کاربر سفارشِ ریفیل‌پذیری دارد که *معلوم نیست ریفیل نداشته باشد*؟ (فقط از کش)."""
    from tenant.bots.virtual_service import smm_refill as _rf
    orders = await _refillable_orders(bot_id, user_id)
    creds: dict = {}
    for o in orders:
        if o.panel_pk not in creds:
            creds[o.panel_pk] = await _panel_creds(bot_id, o.panel_pk)
        api_url, api_key, _ = creds[o.panel_pk]
        _rf.warm(api_url, api_key)
        if _rf.cached_support(api_url, api_key, o.service_id) is not False:
            return True
    return False


async def _smm_myorder_detail_content(bot_id: int, user_id: int, oid: int, back_page: int,
                                      home_cb: str = "smmu:home",
                                      warranty: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """جزئیاتِ یک سفارش — نسخهٔ مخصوصِ مشتری (بدونِ اطلاعاتِ داخلیِ ادمین
    مثلِ هزینه/سود/پنل).

    === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    home_cb همان انتخابِ صفحهٔ لیست است؛ باید در «بازگشت به لیست» هم حفظ
    شود، وگرنه رفتنِ به جزئیاتِ یک سفارش و برگشتن، زمینهٔ ورودِ مستقیم را
    گم می‌کند."""
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
    _suffix = ":mbtn" if home_cb == "mbtn:home" else ""
    # === PATCH_SMM_REFILL_V1 === از «سفارشاتِ گارانتی‌دار» آمده باشد، برگشت هم به همان لیست است
    _list_cb = "smmu:warr" if warranty else "smmu:myorders"
    back_btn = InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=f"{_list_cb}:{back_page}{_suffix}", style=ButtonStyle.DANGER)
    if not o or o.bot_id != bot_id or o.user_telegram_id != user_id:
        return "❌ این سفارش پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[[back_btn]])
    label = _SMM_STATUS_LABEL.get((o.status or "").lower(), o.status or "نامشخص")
    from tenant import _jalali_date_time
    date_str, time_str = _jalali_date_time(o.created_at)
    lines = [
        f"📦 <b>جزئیاتِ سفارش</b>\n",
        f"📦 سرویس: {o.service_name or '—'}",
        f"🔢 تعداد: {o.quantity:,}",
        f"🔗 لینک: {o.link or '—'}",
        f"💰 مبلغِ پرداختی: {int(o.price_toman):,} تومان",
        f"📊 وضعیت: {label}",
        f"🔖 کدِ پیگیری: <code>{o.tracking_code or o.id}</code>",
        f"🗓 تاریخِ ثبت: {date_str} {time_str}",
    ]
    if o.status == "sent" and o.remaining is not None:
        lines.append(f"⏳ باقی‌مانده تا تکمیل: {o.remaining:,}")
    if o.status == "partial":   # === PATCH_SMM_PARTIAL_V1 ===
        _ref = float(getattr(o, "refunded_toman", None) or 0)
        lines.append(f"🟢 تحویل‌شده: {max(0, int(o.quantity or 0) - int(o.remaining or 0)):,}")
        lines.append(f"🅱️ تحویل‌نشده: {int(o.remaining or 0):,}")
        lines.append(f"↩️ مبلغِ بازگشتی به کیف پول: {int(_ref):,} تومان")
        lines.append(f"🧾 مبلغِ نهاییِ سفارش: {int(max(0.0, float(o.price_toman or 0) - _ref)):,} تومان")
    # === PATCH_SMM_REFILL_CANCEL_V1 ===
    # این دکمه‌ها فقط برایِ سفارش‌هایی معنا دارن که واقعاً رویِ یک پنلِ
    # API ثبت شدن (panel_order_id دارن) — سفارشِ دستی چیزی برایِ ارسال به
    # API نداره، باید با پشتیبانی هماهنگ بشه.
    rows = []
    if o.panel_order_id:
        # === PATCH_SMM_REFILL_V1 ===
        # قبلاً برایِ *هر* سفارشِ ثبت‌شده رویِ پنل دکمهٔ ریفیل بود. حالا: فقط اگر ارائه‌دهنده برایِ این سرویس
        # ریفیل دارد (یا هنوز نمی‌دانیم؛ همان لحظهٔ کلیک دقیق چک می‌شود) و ریفیلِ فعالی در جریان نیست.
        # وضعیتِ آخرین ریفیل هم زیرِ سفارش دیده می‌شود.
        # هر خطایِ بخشِ ریفیل فقط همین بخش را حذف می‌کند؛ جزئیاتِ سفارش هرگز به‌خاطرش نمی‌شکند.
        try:
            from tenant.bots.virtual_service import smm_refill as _rf
            _last = await _rf.latest(o.id)
            if _last is not None and _last.status != "requesting":
                _lbl = _rf.STATUS_LABEL.get(_last.status, _last.status)
                lines.append(f"♻️ ریفیل: {_lbl}" + (f" — کد <code>{_last.panel_refill_id}</code>" if _last.panel_refill_id else ""))
            if o.status in _rf.REFILLABLE_ORDER_STATUSES:
                _api_url, _api_key, _ = await _panel_creds(bot_id, o.panel_pk)
                _sup = _rf.cached_support(_api_url, _api_key, o.service_id)
                _rf.warm(_api_url, _api_key)                       # پس‌زمینه؛ صفحه معطل نمی‌شود
                if _sup is not False and not _rf.is_active(_last):
                    rows.append([InlineKeyboardButton(
                        text="🔁 درخواستِ ریفیل (جبرانی)", callback_data=f"smmu:refill:{o.id}:{back_page}{':w' if warranty else ''}",
                        style=ButtonStyle.PRIMARY)])
        except Exception:
            logger.exception("[refill] بخشِ ریفیلِ صفحهٔ جزئیاتِ سفارش خطا داد (بدونِ آن نمایش داده شد)")
        # === PATCH_SMM_CANCEL_FLAG_V1 === اگر پنل برایِ این سرویس صراحتاً cancel=false گفته، دکمه نشان داده نمی‌شود
        # === PATCH_SMM_CANCEL_ALWAYS_V1 === (فدایی) «برایِ همهٔ محصولات می‌شود درخواستِ لغو ثبت کرد» ولی برایِ بعضی
        # سرویس‌ها دکمه نبود: چون پرچمِ cancelِ فهرستِ ارائه‌دهنده false بود و دکمه را پنهان می‌کردیم. پرچم در عمل قابل‌اعتماد
        # نیست؛ دکمه همیشه هست و اگر پنل لغو را نپذیرد، مشتری پیامِ روشنِ «پنل اجازه نمی‌دهد» می‌بیند (smmu:reqcancel).
        _cancel_ok = True
        if o.status in ("sent", "awaiting_admin") and _cancel_ok:
            rows.append([InlineKeyboardButton(text="🚫 درخواستِ لغوِ سفارش", callback_data=f"smmu:reqcancel:{o.id}:{back_page}", style=ButtonStyle.DANGER)])
    rows.append([back_btn])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_SMM_PANEL_ERR_FRIENDLY_V1 ===
def panel_err_friendly(raw: str, what: str = "درخواست") -> str:
    """پاسخِ خامِ پنل را به یک جملهٔ کوتاه و قابل‌فهم برای مشتری تبدیل می‌کند (نه متنِ فنیِ دقیق)."""
    t = (raw or "").lower()
    # === PATCH_SMM_I18N_ERR_KEYS_V1 === بعضی پنل‌ها (مثل WhaleSMM) به‌جایِ متن، کلیدِ ترجمه می‌دهند:
    # «error.incorrect_order_id» / «error.refill_not_found». کلید را به کلماتِ ساده تبدیل می‌کنیم
    # تا همان شاخه‌هایِ پایین تشخیصش بدهند، و چیزِ خام هرگز به مشتری نمایش داده نشود.
    _m = re.search(r"\berror[._]([a-z0-9_.]+)", t)
    if _m:
        t = _m.group(1).replace("_", " ").replace(".", " ")
    if "refill" in t and any(k in t for k in ("not found", "no refill", "incorrect", "invalid")):
        return "ریفیلی با این شناسه روی پنل پیدا نشد."
    if "incorrect order" in t or "invalid order" in t or "order id" in t and "not" in t:
        return "این سفارش روی پنل پیدا نشد؛ با پشتیبانی هماهنگ کن."
    # محدودیت‌هایِ زمانیِ رایجِ پنل‌ها — قبلاً به پیامِ کلیِ «بعداً امتحان کن» می‌افتادند
    # و مشتری نمی‌فهمید چرا ریفیل رد شد.
    if any(k in t for k in ("expired", "period has", "منقضی")):
        return f"مهلتِ {what} برای این سفارش تمام شده است."
    if any(k in t for k in ("24 hour", "once every", "per day", "try again after")):
        return f"{what} برای هر سفارش فقط یک‌بار در هر ۲۴ ساعت ممکن است؛ کمی بعد دوباره امتحان کن."
    if any(k in t for k in ("already", "in progress", "pending", "duplicate")):
        return f"برای این سفارش قبلاً {what} ثبت شده؛ منتظر پاسخِ پنل بمان."
    if any(k in t for k in ("incorrect order", "not found", "no order", "invalid order", "wrong order")):
        return "این سفارش روی پنل پیدا نشد؛ با پشتیبانی هماهنگ کن."
    if any(k in t for k in ("not available", "cannot", "can't", "not allowed", "not eligible", "completed",
                            "canceled", "cancelled", "partial", "refill not", "unsupported", "not support")):
        return f"پنل اجازهٔ این {what} را برای این سفارش نمی‌دهد (شاید سفارش در مرحله‌ای است که قابلِ انجام نیست)."
    if any(k in t for k in ("timeout", "timed out", "connect", "http 5", "502", "503", "504", "نامعتبر", "دریافت نشد")):
        return f"پنل الان پاسخ نداد؛ کمی بعد دوباره امتحان کن."
    return f"{what} ثبت نشد؛ بعداً دوباره امتحان کن."


def _parse_cancel_response(d, order_id) -> tuple[bool, str]:
    """پاسخِ اکشنِ cancel رو پارس می‌کنه — چون شکلش بینِ پنل‌ها فرق داره
    (بعضی dict تکی، بعضی آرایه‌یِ چند‌سفارشی).

    === PATCH_SMM_CANCEL_CONFIRM_FIX_V1 ===
    قبلاً اگه پاسخِ پنل هیچ فیلدِ status ای نداشت (مثلاً یک dict خالیِ
    {} یا جوابِ بی‌ربط، بدونِ error)، به‌طورِ پیش‌فرض «Completed» فرض
    می‌شد و سفارش با موفقیت لغو می‌شد — یعنی بدونِ تأییدِ واقعیِ پنل،
    فقط چون جواب error نداشت، سفارش لغو حساب می‌شد. حالا نبودِ status
    صریح به‌معنیِ عدمِ تأییدِ لغوه، نه موفقیتِ خودکار."""
    if d is None:
        return False, "پاسخی از پنل دریافت نشد."
    # === PATCH_SMM_CANCEL_REQUESTED_V1 ===
    # بیشترِ پنل‌ها (استانداردِ API) برایِ cancel فقط «درخواست ثبت شد» می‌دهند: [{"order":1,"cancel":1}]
    # و لغوِ واقعی بعداً در وضعیتِ سفارش (Canceled) می‌آید. قبلاً چون فیلدِ status نبود «ناموفق» نشان
    # داده می‌شد و دوباره‌زدن «Cancel already requested» می‌داد. این‌ها حالتِ «در انتظارِ پنل» حساب می‌شوند.
    _txt_err = ""
    if isinstance(d, dict) and d.get("error"):
        _txt_err = str(d["error"])
    if _txt_err and "already" in _txt_err.lower() and "cancel" in _txt_err.lower():
        return True, "already"
    if _txt_err:
        return False, _txt_err
    items = d if isinstance(d, list) else [d]
    if not items:
        return False, "پاسخِ نامعتبر از پنل."
    # === PATCH_SMM_CANCEL_MATCH_ORDER_V1 === پاسخِ چندسفارشی: ردیفِ همین سفارش اول بررسی شود
    items = sorted(items, key=lambda x: 0 if isinstance(x, dict) and str(x.get("order")) == str(order_id) else 1)
    for it in items:
        if not isinstance(it, dict):
            continue
        c = it.get("cancel") if isinstance(it.get("cancel"), dict) else it
        if not isinstance(c, dict):
            continue
        if c.get("error"):
            _e = str(c["error"])
            if "already" in _e.lower() and "cancel" in _e.lower():
                return True, "already"
            return False, _e
        status = c.get("status")
        _cv = it.get("cancel")
        if not status and (_cv is True or str(_cv).strip().lower() in ("1", "true", "ok", "success", "requested")):
            return True, "requested"
        if not status:
            # پنل هیچ وضعیتِ صریحی برای لغو اعلام نکرده — این را تأییدِ
            # لغو حساب نمی‌کنیم، تا سفارش بدونِ تأییدِ واقعیِ پنل لغو نشه.
            return False, "پنل وضعیتِ لغو را تأیید نکرد (پاسخی بدونِ status دریافت شد)."
        return True, str(status)
    return False, "پاسخِ نامعتبر از پنل."


# === PATCH_SMM_ORDER_DONE_KB_V1 ===
# درخواستِ ادمین (فدایی): زیرِ پیامِ «سفارش ثبت شد» دکمهٔ «بازگشت به منو» نباشد (چون پیامِ فاکتور
# را از چت می‌برد)؛ به‌جایش «🏠 استارت» (ربات دوباره /start می‌شود و منوی اصلی می‌آید، فاکتور
# سرِ جایش می‌ماند) و «🔁 سفارش مجدد» (همان محصول؛ لینک و تعدادِ تازه را می‌خواهد).
def _order_done_kb(order_pk: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 سفارش مجدد", callback_data=f"smmu:reorder:{order_pk}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🏠 استارت", callback_data="smmu:restart",
                              style=ButtonStyle.SUCCESS)],
    ])


def _no_glass_edit() -> None:
    """=== PATCH_SMM_REORDER_NO_EDIT_V1 === میان‌افزارِ «ویرایشِ درجا» (منویِ شیشه‌ای) اولین پیامِ
    ارسالیِ هندلر را به‌جایِ پیامِ جدید رویِ پیامِ دکمه ادیت می‌کند؛ برایِ پیامِ «ثبت سفارش»
    که باید برایِ همیشه بماند، این را خاموش می‌کنیم تا حتماً پیامِ تازه برود."""
    try:
        from tenant import _GLASS_EDIT
        st = _GLASS_EDIT.get()
        if st is not None:
            st["done"] = True
    except Exception:
        logger.debug("خاموش‌کردنِ glass-edit ممکن نشد (بی‌اهمیت)")


@smm_router.callback_query(F.data == "smmu:restart")
async def smmu_restart(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """مثلِ زدنِ /start: پیامِ فعلی (فاکتور) دست‌نخورده می‌ماند و منوی اصلی تازه می‌آید."""
    from aiogram.filters import CommandObject
    from tenant import tenant_start
    await cb.answer()
    _no_glass_edit()
    try:
        fake = cb.message.model_copy(update={"text": "/start", "from_user": cb.from_user}).as_(bot)
        await tenant_start(fake, CommandObject(prefix="/", command="start", args=None), bot, state)
    except Exception:
        logger.exception("smmu:restart ناموفق بود")
        await cb.message.answer("برای رفتن به منوی اصلی /start را بزن.")


@smm_router.callback_query(F.data.startswith("smmu:reorder:"))
async def smmu_reorder(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """سفارشِ مجدد برایِ همان سرویس: صفحهٔ خریدِ همان محصول باز می‌شود تا لینک و تعدادِ جدید بگیرد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    try:
        oid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        return await cb.answer()
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
        v = None
        if o and o.bot_id == row.id and o.user_telegram_id == cb.from_user.id:
            # === PATCH_SMM_SAME_ID_OTHER_PANEL_V1 === شناسهٔ سرویس ممکن است در چند پنل تکرار شود؛ اول ردیفِ دقیقِ سفارش
            v = (await s.get(SmmService, o.smm_service_pk)) if o.smm_service_pk else None
            if not (v and v.bot_id == row.id and v.is_active):
                v = await s.scalar(select(SmmService).where(
                    SmmService.bot_id == row.id, SmmService.service_id == o.service_id,
                    SmmService.is_active == True).order_by(SmmService.id).limit(1))
    if not v:
        return await cb.answer("این سرویس دیگر در دسترس نیست.", show_alert=True)
    await cb.answer()
    _no_glass_edit()
    # === PATCH_SMM_REORDER_NEWMSG_V1 ===
    # پیامِ «ثبت سفارش» ادیت نمی‌شود و برایِ همیشه می‌ماند؛ لینک/تعدادِ تازه با یک پیامِ جدید پرسیده می‌شود
    # و دکمهٔ «سفارش مجدد» از همان پیام برداشته می‌شود (فقط «🏠 استارت» می‌ماند).
    ok = await send_smm_product_page(cb.message, bot, v.id, state)
    if not ok:
        await cb.message.answer("⚠️ این سرویس دیگر در دسترس نیست.")
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 استارت", callback_data="smmu:restart", style=ButtonStyle.SUCCESS)]]))
    except Exception:
        logger.info("برداشتنِ دکمهٔ «سفارش مجدد» ممکن نشد")


@smm_router.callback_query(F.data.startswith("smmu:refill:"))
async def smmu_request_refill(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_REFILL_V1 === منطقِ اصلی در smm_refill.request_refill است (چکِ پشتیبانیِ ارائه‌دهنده،
    قفل، شناسهٔ معتبر). این‌جا فقط پیام‌رسانی."""
    from tenant import get_bot_row
    from tenant.bots.virtual_service import smm_refill as _rf
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    try:
        oid = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        await cb.answer()
        return
    await cb.answer("در حالِ بررسی و ارسالِ درخواستِ ریفیل…")
    # === PATCH_SMM_REFILL_ALWAYS_REPLY_V1 ===
    # گزارش: «ریفیل ثبت می‌کنم هیچ پاسخی نمیده، فقط همون بالا میگه در حالِ بررسی». هر خطا/تایم‌اوتِ
    # پنل بی‌صدا می‌ماند و کاربر نتیجه‌ای نمی‌دید. حالا همیشه یک پاسخ می‌رود.
    try:
        res = await asyncio.wait_for(_rf.request_refill(row.id, oid, cb.from_user.id), timeout=45)
    except asyncio.TimeoutError:
        logger.warning("[refill] پنل در ۴۵ ثانیه پاسخ نداد (order=%s)", oid)
        await cb.message.answer("⏳ پنل دیر جواب داد. درخواستت ممکن است ثبت شده باشد؛ چند دقیقه بعد "
                                "وضعیتِ ریفیل را در «جزئیاتِ سفارش» ببین و اگر نبود دوباره امتحان کن.")
        return
    except Exception:
        logger.exception("[refill] درخواستِ ریفیل خطا داد (order=%s)", oid)
        await cb.message.answer("❌ ثبتِ ریفیل با خطا مواجه شد. کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.")
        return
    if not res.ok:
        await cb.message.answer(res.text or "❌ ریفیل ثبت نشد.", parse_mode="HTML")
        return
    try:
        txt = await _smm_txt(row.id, "refill_registered")
        txt = await _render_order_text(row.id, oid, txt)
        if not (txt or "").strip():
            raise ValueError("empty")
    except Exception:
        logger.exception("[refill] ساختِ متنِ تأیید ناموفق؛ متنِ پیش‌فرض")
        txt = "✅ درخواستِ ریفیلِ تو ثبت شد. نتیجه که از پنل رسید همین‌جا خبرت می‌کنم."
    await _answer_html_safe(cb.message, txt)


@smm_router.callback_query(F.data.startswith("smmu:reqcancel:"))
async def smmu_request_cancel(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    _, _, oid_s, back_page_s = cb.data.split(":")
    oid, back_page = int(oid_s), int(back_page_s)
    async with Session() as s:
        o = await s.get(SmmOrder, oid)
    if not o or o.bot_id != row.id or o.user_telegram_id != cb.from_user.id:
        await cb.answer("این سفارش پیدا نشد.", show_alert=True)
        return
    if o.status not in ("sent", "awaiting_admin") or not o.panel_order_id:
        await cb.answer("این سفارش دیگر قابلِ لغو نیست.", show_alert=True)
        return
    await cb.answer("در حالِ ارسالِ درخواستِ لغو به پنل…")
    api_url, api_key, _ = await _panel_creds(row.id, o.panel_pk)
    # === PATCH_SMM_CANCEL_ALWAYS_REPLY_V1 === گزارش فدایی: «وقتی پنل خطا می‌دهد ربات به کاربر نمی‌گوید و گیر می‌کند».
    # تایم‌اوت/استثنا هم باید پیام بدهد، و خطای واقعی پنل (غیر ۲۰۰) به‌جای «پاسخی نیامد» نشان داده شود.
    try:
        d = await asyncio.wait_for(smm_api.cancel_order(api_url, api_key, o.panel_order_id), timeout=45)
    except asyncio.TimeoutError:
        logger.warning("[cancel] پنل در ۴۵ ثانیه پاسخ نداد (order=%s)", oid)
        await cb.message.answer("⏳ پنل دیر جواب داد. درخواست لغوت ممکن است ثبت شده باشد؛ چند دقیقه بعد وضعیت سفارش را "
                                "ببین و اگر تغییری نکرد دوباره امتحان کن.")
        return
    except Exception:
        logger.exception("[cancel] درخواست لغو خطا داد (order=%s)", oid)
        await cb.message.answer("❌ ارسال درخواست لغو به پنل با خطا مواجه شد. کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.")
        return
    if d is None:
        ok, note = False, (smm_api.last_error(api_url) or "پاسخی از پنل دریافت نشد.")
    else:
        ok, note = _parse_cancel_response(d, o.panel_order_id)
    if not ok:
        logger.info("[cancel] پاسخِ پنل (order=%s): %s", oid, note)
        await cb.message.answer("❌ " + panel_err_friendly(note, "درخواستِ لغو"))
        return
    if note == "already":   # PATCH_SMM_PANEL_ERR_FRIENDLY_V1
        await cb.message.answer(
            "⏳ برای این سفارش <b>قبلاً درخواستِ لغو ثبت شده</b> و منتظرِ پاسخِ پنل هستیم.\n"
            "لازم نیست دوباره بزنی؛ نتیجه که مشخص شد همین‌جا خبرت می‌کنم.", parse_mode="HTML")
        return
    if note == "requested":   # PATCH_SMM_CANCEL_REQUESTED_V1
        async with Session() as s:
            oo = await s.get(SmmOrder, oid, with_for_update=True)
            if oo is not None and await _is_foreign(oo, bot):   # PATCH_TENANT_SCOPE_V1
                oo = None
            if oo and "درخواستِ لغو ثبت شد" not in (oo.note or ""):
                oo.note = (oo.note or "") + " | درخواستِ لغو ثبت شد (منتظرِ تأییدِ پنل)"
                await s.commit()
        await cb.message.answer(
            "⏳ درخواستِ لغوِ سفارشت به پنل رسید و <b>ثبت شد</b>.\n"
            "اگر پنل قبول کند، سفارش لغو و مبلغِ تحویل‌نشده خودکار به کیف پولت برمی‌گردد؛ "
            "لازم نیست دوباره درخواست بدهی.", parse_mode="HTML")
        return
    # === PATCH_SMM_CANCEL_REAL_STATUS_V1 ===
    # قبلاً هر پاسخِ معتبری (یعنی هر status ای، حتی «Completed» یا «In
    # progress») بدونِ چک‌کردنِ خودِ متنش، بی‌قیدوشرط سفارش رو «لغوشده»
    # می‌کرد — یعنی اگه پنل می‌گفت سفارش قبلاً کامل شده یا هنوز در حالِ
    # انجامه، ما همچنان به دروغ به مشتری می‌گفتیم «لغو شد». الان متنِ
    # واقعیِ status رو چک می‌کنیم.
    note_low = note.lower()
    is_actually_canceled = "cancel" in note_low
    is_completed = "complet" in note_low
    if not is_actually_canceled:
        if is_completed:
            async with Session() as s:
                oo = await s.get(SmmOrder, oid, with_for_update=True)
                if oo is not None and await _is_foreign(oo, bot):   # PATCH_TENANT_SCOPE_V1
                    oo = None
                if oo and oo.status not in ("refunded", "canceled", "completed"):
                    oo.status = "completed"
                    oo.updated_at = datetime.utcnow()
                    oo.note = (oo.note or "") + f" | درخواستِ لغو رد شد — پنل می‌گوید تکمیل شده ({note})"
                    await s.commit()
            await cb.message.answer(
                "ℹ️ این سفارش قبل از رسیدنِ درخواستِ لغو، رویِ پنل تکمیل شده بود؛ "
                "وضعیتش به «✅ تکمیل‌شده» به‌روزرسانی شد و وجهی برگردانده نمی‌شود.")
        else:
            await cb.message.answer(
                f"ℹ️ پنل هنوز اجازهٔ لغو نداد — وضعیتِ فعلیِ سفارش رویِ پنل: «{note}». "
                "چیزی تغییر نکرد؛ اگه هنوز در حالِ انجامه، بعداً دوباره امتحان کن.")
        return
    # === PATCH_SMM_REFILL_CANCEL_V1 ===
    # طبقِ درخواستِ ادمین: «هر مقداری که پنل قبول کنه، همونو برگردون» —
    # یعنی بعدِ لغوِ موفق، وضعیتِ تازهٔ سفارش رو از پنل می‌گیریم تا ببینیم
    # چقدرش واقعاً تحویل داده نشده (remains)، و فقط همون بخش رو نسبتی
    # برمی‌گردونیم — نه کلِ مبلغ، چون بخشی ممکنه از قبل تحویل شده باشه.
    status_data = await smm_api.get_order_status(api_url, api_key, o.panel_order_id)
    remains = None
    if isinstance(status_data, dict):
        try:
            remains = int(status_data.get("remains"))
        except (TypeError, ValueError):
            remains = None
    refund = 0.0
    if remains is not None and o.quantity:
        refund = round(float(o.price_toman) * (min(remains, o.quantity) / o.quantity))
    async with Session() as s:
        oo = await s.get(SmmOrder, oid, with_for_update=True)
        if oo is not None and await _is_foreign(oo, bot):   # PATCH_TENANT_SCOPE_V1
            oo = None
        if not oo or oo.status in ("refunded", "canceled", "completed"):
            await cb.message.answer("این سفارش قبلاً پردازش شده بود؛ چیزی تغییر نکرد.")
            return
        oo.status = "canceled"
        oo.updated_at = datetime.utcnow()
        oo.note = (oo.note or "") + f" | لغو توسطِ مشتری (تأییدِ پنل: {note}, باقیمانده={remains})"
        if refund > 0:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id).with_for_update())
            if u:
                u.balance = float(u.balance) + refund
                s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=refund,
                                kind="smm_refund", note=f"بازگشتِ وجهِ نسبیِ سفارش #{oo.tracking_code or oo.id} (لغوِ مشتری)"))
        await s.commit()
    txt = "✅ سفارش لغو شد."
    if refund > 0:
        txt += f"\n💰 <b>{int(refund):,} تومان</b> (بابتِ بخشِ تحویل‌نشده) به کیف‌پولت برگشت."
    else:
        txt += "\nطبقِ اعلامِ پنل، بخشی برایِ بازگشتِ وجه باقی نمانده بود."
    await cb.message.answer(txt, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smmu:myorders"))
async def smmu_my_orders(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 1
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    home_cb = "mbtn:home" if len(parts) > 3 and parts[3] == "mbtn" else "smmu:home"
    text, kb = await _smm_myorders_paginated_content(row.id, cb.from_user.id, page, home_cb=home_cb)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("smmu:warr"))
async def smmu_warranty_orders(cb: CallbackQuery, bot: Bot):
    """=== PATCH_SMM_REFILL_V1 === «🛡 سفارشاتِ گارانتی‌دار» — فقط سفارش‌هایی که ریفیل دارند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        page = 1
    home_cb = "mbtn:home" if len(parts) > 3 and parts[3] == "mbtn" else "smmu:home"
    await cb.answer("در حالِ بررسیِ ریفیل‌دار بودنِ سفارش‌ها…")
    text, kb = await _smm_myorders_paginated_content(row.id, cb.from_user.id, page, home_cb=home_cb, warranty=True)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smmu:oview:"))
async def smmu_order_view(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    oid, back_page = int(parts[2]), int(parts[3])
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    flags = set(parts[4:])                                   # PATCH_SMM_REFILL_V1: mbtn و/یا w
    back_home_cb = "mbtn:home" if "mbtn" in flags else "smmu:home"
    text, kb = await _smm_myorder_detail_content(row.id, cb.from_user.id, oid, back_page,
                                                 home_cb=back_home_cb, warranty=("w" in flags))
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1 ===
# گزارشِ فدایی: «دسته‌هایِ روبینو را به دکمه وصل می‌کنم، مشتری می‌زند چیزی وصل نیست.»
# علت: فروشگاهِ مشتری (و صفحهٔ «اتصال به پلتفرم» و دکمهٔ پلتفرم) پلتفرم را فقط از رویِ «اسمِ»
# سرویس حدس می‌زد؛ نه دسته‌ای که ادمین سرویس را داخلش گذاشته، نه پلتفرم/نوعِ ذخیره‌شده. سرویسی مثلِ
# «فالوور ایرانی» داخلِ دستهٔ روبینو زیرِ «سایر»/اینستاگرام می‌رفت و دکمهٔ روبینو خالی می‌ماند.
# حالا ترتیب: پلتفرمِ دستهٔ ادمین ← پلتفرم/نوعِ ذخیره‌شده ← حدس از اسم (همان _svc_platform_type).
# اقلام با شناسهٔ خودِ سرویس (pk) نگاشت می‌شوند، نه service_idِ پنل که بینِ دو پنل/سرویسِ دستی تکراری است.
async def _cat_platform_map(bot_id: int) -> dict:
    async with Session() as s:
        rows = (await s.execute(select(SmmCategory.id, SmmCategory.platform).where(
            SmmCategory.bot_id == bot_id))).all()
    return {r[0]: r[1] for r in rows}


async def sync_category_platforms(bot_id: int, svcs: list | None = None) -> int:
    """پلتفرمِ هر سرویسِ داخلِ یک دسته = پلتفرمِ همان دسته (انتخابِ صریحِ ادمین)؛ نوعِ خالی یک‌بار
    از رویِ اسم پر می‌شود. فقط ردیف‌هایِ ناهماهنگ نوشته می‌شوند؛ اشیایِ svcs هم در حافظه به‌روز می‌شوند
    تا همهٔ مصرف‌کننده‌هایِ _svc_platform_type (فروشگاه، اتصالِ سریع، مینی‌اپ، مدیریتِ محصولات) یکی ببینند."""
    cat_plats = await _cat_platform_map(bot_id)
    if not cat_plats:
        return 0
    if svcs is None:
        async with Session() as s:
            svcs = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == bot_id, SmmService.category_pk.is_not(None)))).all()
    changed = []
    for v in svcs:
        cp = cat_plats.get(getattr(v, "category_pk", None))
        if not cp or cp == "other":
            continue
        t = v.type_key or smm_api.detect_service_type(v.name or "", v.category or "")
        if v.platform_key != cp or v.type_key != t:
            v.platform_key, v.type_key = cp, t
            changed.append((v.id, cp, t))
    if changed:
        from sqlalchemy import update as _upd
        async with Session() as s:
            for vid, cp, t in changed:
                await s.execute(_upd(SmmService).where(SmmService.id == vid).values(
                    platform_key=cp, type_key=t))
            await s.commit()
    return len(changed)


async def classified_tree(bot_id: int, svcs: list) -> dict:
    """{platform: {type: [ {service, pk, name, category, platform, stype} ]}} — ترتیبِ ورودی حفظ می‌شود."""
    try:
        await sync_category_platforms(bot_id, svcs)
    except Exception:
        logger.exception("[smm] هماهنگ‌سازیِ پلتفرمِ دسته‌ها ناموفق بود")
    tree: dict[str, dict[str, list]] = {}
    for v in svcs:
        p, t = _svc_platform_type(v)
        tree.setdefault(p, {}).setdefault(t, []).append({
            "service": v.service_id, "pk": v.id, "name": v.name or "",
            "category": v.category or "", "platform": p, "stype": t})
    return tree


async def _user_active_tree(bot_id: int):
    """درخت دسته‌بندی‌شدهٔ سرویس‌های فعال + نگاشت id→SmmService."""
    await ensure_smm_columns()
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True)
            .order_by(SmmService.id))).all()
    by_pk = {v.id: v for v in svcs}
    tree = await classified_tree(bot_id, svcs)
    return tree, by_pk


async def _smm_platform_content(bot_id: int, pkey: str,
                                home_cb: str = "smmu:home") -> tuple[str, InlineKeyboardMarkup] | None:
    """محتوایِ صفحهٔ «نوعِ سرویس» برایِ یک پلتفرمِ مشخص — هم دکمهٔ عادیِ
    smmu:plat: و هم دکمهٔ صفحهٔ اصلیِ متصل‌شده به یک پلتفرم (smm_platform)
    از همین یک تابع استفاده می‌کنند.

    === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    home_cb: مقصدِ دکمهٔ «بازگشت». وقتی این صفحه از دلِ خودِ فروشگاه باز
    می‌شود (smmu:plat:)، بازگشت باید به خانهٔ فروشگاه برود (پیش‌فرض).
    وقتی مستقیم از یک دکمهٔ صفحهٔ اصلی باز می‌شود (show_smm_platform_to_user)،
    باید به خانهٔ واقعیِ ربات برگردد، نه دوباره وسطِ فروشگاه."""
    tree, _ = await _user_active_tree(bot_id)
    types = tree.get(pkey, {})
    if not types:
        return None
    rows = []
    for tkey, items in types.items():
        rows.append([InlineKeyboardButton(
            text=f"{smm_api.service_type_label(tkey)} ({len(items)})",
            callback_data=f"smmu:type:{pkey}:{tkey}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=home_cb)])
    plat_txt = await platform_label_for_bot(bot_id, pkey)
    return f"{plat_txt}\n\nنوع سرویس را انتخاب کن:", InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("smmu:plat:"))
async def smmu_pick_platform(cb: CallbackQuery, bot: Bot):
    """مرحلهٔ ۲: نمایش انواع سرویسِ یک پلتفرم (ممبر، ویو، لایک، ...)."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    pkey = cb.data.split(":", 2)[2]
    content = await _smm_platform_content(row.id, pkey)
    if not content:
        await cb.answer("موردی نیست", show_alert=True)
        return
    text, kb = content
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_SMM_PLATFORM_BUTTON_ACTION_V1 ===
# درخواستِ ادمین: امکانِ وصل‌کردنِ یه دکمهٔ صفحهٔ اصلی مستقیم به یک پلتفرمِ
# مشخص (مثلاً «اینستاگرام») — با زدنش، کاربر مستقیم می‌ره سراغِ نوع‌سرویس‌های
# همون پلتفرم، بدونِ اینکه اول از بینِ همهٔ پلتفرم‌ها انتخاب کنه.
async def show_smm_platform_to_user(bot: Bot, msg: Message, bot_id: int, pkey: str):
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    content = await _smm_platform_content(bot_id, pkey, home_cb="mbtn:home")
    if not content:
        await msg.answer("🛒 سرویسی برایِ این پلتفرم فعال نیست.")
        return
    text, kb = content
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_SMM_PLATFORM_MULTI_V1 ===
# درخواستِ ادمین: چند پلتفرم هم‌زمان به یک دکمه وصل بشه، نه فقط یکی.
async def _smm_platforms_subset_content(bot_id: int, pkeys: list[str]) -> tuple[str, InlineKeyboardMarkup] | None:
    """دقیقاً همون صفحهٔ اولِ SMM (لیستِ پلتفرم‌ها)، ولی فقط محدود به
    همون پلتفرم‌هایی که ادمین به این دکمه وصل کرده."""
    tree, _ = await _user_active_tree(bot_id)
    overrides = await _platform_label_overrides(bot_id)
    rows = []
    for pkey in pkeys:
        types = tree.get(pkey)
        if not types:
            continue
        cnt = sum(len(x) for x in types.values())
        rows.append([InlineKeyboardButton(
            text=f"{overrides.get(pkey, smm_api.platform_label(pkey))} ({cnt})",
            callback_data=f"smmu:plat:{pkey}")])
    if not rows:
        return None
    rows.append([InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="smmu:myorders:1:mbtn")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
    return "🌐 <b>خدمات شبکه‌های اجتماعی</b>\n\nپلتفرم را انتخاب کن:", InlineKeyboardMarkup(inline_keyboard=rows)


async def show_smm_platforms_subset_to_user(bot: Bot, msg: Message, bot_id: int, pkeys: list[str]):
    # === PATCH_SMM_DIRECT_ENTRY_HOME_BACK_V1 ===
    content = await _smm_platforms_subset_content(bot_id, pkeys)
    if not content:
        await msg.answer("🛒 سرویسی برایِ این پلتفرم‌ها فعال نیست.")
        return
    text, kb = content
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smmu:type:"))
async def smmu_pick_type(cb: CallbackQuery, bot: Bot):
    """مرحلهٔ ۳: نمایش سرویس‌های همان پلتفرم+نوع برای انتخاب نهایی."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    # فرمت: smmu:type:<plat>:<type>[:<page>]
    parts = cb.data.split(":")
    pkey, tkey = parts[2], parts[3]
    _pg = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1   # PATCH_SMM_LIST_PAGINATION_V1
    tree, sid_map = await _user_active_tree(row.id)   # PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1: نگاشت با pk
    items = tree.get(pkey, {}).get(tkey, [])
    if not items:
        await cb.answer("موردی نیست", show_alert=True)
        return
    # === PATCH_SMM_SORT_ORDER_V1 ===
    # درخواستِ ادمین: ترتیبِ نمایشِ سرویس‌ها قابلِ‌انتخاب باشه (ارزان/گران/
    # جدید/قدیمی) — دقیقاً چیزی که تو ویدیویِ رقیب دیده بودیم. برایِ سرعت،
    # به‌جایِ محاسبهٔ قیمتِ دقیقِ هرکدوم (که یعنی N بار کوئریِ async رویِ
    # تنظیماتِ پنل)، از panel_rate/sell_rate — که همین‌جوری هم لود شده —
    # به‌عنوانِ معیارِ نسبیِ قیمت استفاده می‌کنیم؛ چون سودِ همون پنل معمولاً
    # یکسانه، ترتیبِ نسبی همون می‌مونه.
    sort_order = await _get_setting(row.id, "smm_sort_order", "none")
    rows = []
    entries = []
    for it in items:
        v = sid_map.get(it["pk"])
        if not v:
            continue
        entries.append((it, v))
    # === PATCH_SMM_SORT_EVERYWHERE_V1 === همان مرتب‌سازِ مشترک (با قیمتِ واقعی)
    _ord = {v.id: i for i, v in enumerate(await _sort_svc_objs(row.id, [e[1] for e in entries], sort_order))}
    entries.sort(key=lambda x: _ord[x[1].id])
    entries, _pg, _pages = _page_slice(entries, _pg)
    for it, v in entries:
        _cbd = f"smmu:svc:{v.id}:{pkey}:{tkey}:{_pg}"          # PATCH_SMM_PRODUCT_BACK_TO_LIST_V1: صفحهٔ محصول بداند از کدام لیست آمده
        rows.append([InlineKeyboardButton(
            text=it["name"][:55], callback_data=_cbd if len(_cbd.encode()) <= 64 else f"smmu:svc:{v.id}")])
    _nav = _page_nav(f"smmu:type:{pkey}:{tkey}", _pg, _pages)
    if _nav:
        rows.append(_nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"smmu:plat:{pkey}")])
    # === PATCH_SMM_PLATFORM_RENAME_V1 ===
    header = f"{await platform_label_for_bot(row.id, pkey)} — {smm_api.service_type_label(tkey)}"
    try:
        await cb.message.edit_text(
            f"{header}\n\nسرویس موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(
            f"{header}\n\nسرویس موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "smmu:home")
async def smmu_home(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await cb.answer()
    await show_smm_services_to_user(bot, cb, row.id)


@smm_router.callback_query(F.data.startswith("smmu:svc:"))
async def smmu_pick_service(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    _p = cb.data.split(":")
    vid = int(_p[2])
    _back = None                                            # PATCH_SMM_PRODUCT_BACK_TO_LIST_V1
    if len(_p) >= 6 and _p[5].isdigit():
        _back = f"smmu:type:{_p[3]}:{_p[4]}:{_p[5]}"
    ok = await send_smm_product_page(cb, bot, vid, state, back_cb=_back)
    if not ok:
        await cb.answer("این سرویس دیگر موجود نیست", show_alert=True)
        return
    await cb.answer()


# === PATCH_SMM_QUICKLINK_BREADCRUMB_V1 ===
@smm_router.callback_query(F.data.startswith("smmu:svcb:"))
async def smmu_pick_service_breadcrumb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """مثلِ smmu_pick_service، فقط با نگه‌داشتنِ مسیرِ پلتفرم/نوعی که
    مشتری تویِ «اتصالِ سریع» طی کرده، تا صفحهٔ نهایی بتونه اون رو
    به‌صورتِ breadcrumb (مثلاً «اینستاگرام › ممبر») نشون بده."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    vid, pkey, tkey = int(parts[2]), parts[3], parts[4]
    breadcrumb = f"{smm_api.platform_label(pkey)} › {smm_api.service_type_label(tkey)}"
    ok = await send_smm_product_page(cb, bot, vid, state, breadcrumb=breadcrumb)
    if not ok:
        await cb.answer("این سرویس دیگر موجود نیست", show_alert=True)
        return
    await cb.answer()


# === PATCH_SMM_VNUM_ROUTE_V1 ===
# شمارهٔ مجازی ذاتاً نه «لینک هدف» دارد و نه تعدادش فرق می‌کند (همیشه ۱).
# مسیرِ خریدِ اختصاصیِ خودش (show_vnum_shop_to_user → vnum:buy / vnum:ozbuy)
# از اول وجود داشته، ولی وقتی ادمین همین سرویس را به یک «دکمهٔ سفارشی» یا
# «اتصالِ سریع» وصل می‌کرد، مسیرِ عمومیِ SMM اجرا می‌شد و دو چیز خراب می‌شد:
#
#   ۱) قیمت ۱۰۰۰ برابر نشان داده می‌شد. سرویس‌های OzVinoo قیمتشان در
#      SmmService.sell_rate به‌صورتِ «هر ۱۰۰۰» ذخیره می‌شود (×۱۰۰۰)، ولی
#      _qty_factor چون max_qty==1 است آن را «تکی» تشخیص می‌دهد و ضریبِ
#      ۱/۱۰۰۰ را اعمال نمی‌کند. نتیجه: ۱۱۵٬۰۵۰ تومان → ۱۱۵٬۰۵۰٬۰۰۰ تومان.
#   ۲) از کاربر «لینک کانال/پست/پروفایل» خواسته می‌شد که برایِ شماره
#      بی‌معنی است و کاربر گیر می‌کرد.
#
# راه‌حل: قبل از ورود به مسیرِ عمومی، سرویسِ شمارهٔ مجازی را تشخیص بده و
# کارتِ خریدِ خودش را بفرست. قیمت از همان فرمولِ فروشگاهِ شمارهٔ مجازی
# (sell_rate // 1000) می‌آید تا هر دو مسیر یک عدد نشان دهند.

def _is_vnum_service(svc) -> bool:
    """آیا این سرویس، شمارهٔ مجازی است (دستی یا خودکارِ OzVinoo)؟"""
    if str(getattr(svc, "service_id", "") or "").startswith("ozv:"):
        return True
    if (getattr(svc, "category", "") or "") == _VNUM_CATEGORY:
        return True
    return (getattr(svc, "type_key", "") or "") == "virtual_number"


def _vnum_price(svc) -> int:
    """قیمتِ واقعیِ یک شماره — دقیقاً مثلِ show_vnum_shop_to_user."""
    try:
        return int(svc.sell_rate) // 1000
    except (TypeError, ValueError):
        return 0


async def _send_vnum_service_card(target, bot_id: int, svc,
                                  breadcrumb: str | None = None) -> None:
    """کارتِ خریدِ یک شمارهٔ مجازی: قیمتِ درست + دکمهٔ «دریافت شماره»."""
    price = _vnum_price(svc)
    is_oz = str(svc.service_id or "").startswith("ozv:")
    available = bool(svc.is_active)
    if not available:
        cb_data = f"vnum:unavail:{svc.id}"
        btn_text = "❌ ناموجود"
        btn_style = ButtonStyle.DANGER
    else:
        cb_data = f"vnum:ozbuy:{svc.id}" if is_oz else f"vnum:buy:{svc.id}"
        btn_text = "📱 دریافت شماره"
        btn_style = ButtonStyle.SUCCESS

    _rate = ""
    try:
        from tenant.shared.product_rating import rating_line, smm_key
        _rate = await rating_line(bot_id, smm_key(svc.id))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (نمایشِ امتیازِ شمارهٔ مجازی)")

    crumb_line = f"📂 {breadcrumb} › {svc.name}\n\n" if breadcrumb else ""
    stock_line = "🟢 موجود" if available else "🔴 ناموجود"
    text = (
        f"{crumb_line}"
        f"📱 <b>{svc.name}</b>\n\n"
        f"💵 قیمت: <b>{price:,} تومان</b>\n"
        f"📦 وضعیت: {stock_line}"
        f"{_rate}\n\n"
        f"پس از زدنِ دکمهٔ زیر، شماره برایت رزرو و ارسال می‌شود؛ "
        f"سپس با دکمهٔ «دریافت کد پیامک» کدِ تأیید را می‌گیری."
    )
    rows = [[InlineKeyboardButton(text=btn_text, callback_data=cb_data, style=btn_style)]]
    try:
        from tenant import inline_back_row
        br = await inline_back_row(bot_id)
        if br:
            rows.append(br)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (دکمهٔ بازگشتِ کارتِ شمارهٔ مجازی)")
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await target.answer(text, parse_mode="HTML", reply_markup=kb)


# === PATCH_SMM_DIRECT_PRODUCT_BUTTON_V1 ===
# === PATCH_SMM_DEEPLINK_RESUME_V1 ===
# گزارشِ ادمین (فدایی): لینکِ محصول برایِ کاربرِ تازه کار نمی‌کرد. /start در دو مسیرِ
# زودهنگام (گیتِ قوانین، عضویتِ اجباری) برمی‌گردد و پارامترِ svc_ را دور می‌ریخت، پس
# کاربری که از یک لینکِ اشتراکی می‌آمد بعدِ تأییدِ قوانین/عضویت به خانه می‌رسید، نه به محصول.
# حالا شمارهٔ محصول در FSM (pending_svc_id) نگه داشته می‌شود و بعدِ عبور از گیت نمایش
# داده می‌شود؛ درست مثلِ pending_gate_code.
def _svc_id_from_start_payload(payload: str | None) -> int | None:
    p = (payload or "").strip()
    if p.startswith("svc_"):
        try:
            return int(p[4:])
        except ValueError:
            return None
    return None


async def _resume_pending_svc(target, bot: Bot, state: FSMContext) -> None:
    """اگر لینکِ محصولی پیش از یک گیت نگه داشته شده بود، همین حالا صفحهٔ آن را نشان بده.
    هرگز جریانِ گیت را نمی‌شکند: هر خطا فقط لاگ می‌شود."""
    try:
        data = await state.get_data()
        sid = data.get("pending_svc_id")
        if not sid:
            return
        await state.update_data(pending_svc_id=None)
        await send_smm_product_page(target, bot, int(sid), state)
    except Exception:
        logger.exception("[SMM] ادامهٔ لینکِ محصول بعد از گیت ناموفق بود")


# === PATCH_SMM_PRODUCT_SHORTCUT_ROUTING_V1 ===
# گزارشِ ادمین (فدایی): «لینک/دستورِ سریعِ محصول هنوز وقتی می‌فرستی کار نمی‌کند».
# علت (با اجرایِ واقعیِ روتر ثابت شد): هندلرِ قبلیِ من رویِ smm_router بود و
#   • برایِ کاربرِ بی‌کار، tenant_buttons (catch-allِ دکمه‌ها که برایِ متنِ ناشناس بی‌صدا و
#     بدونِ SkipHandler برمی‌گردد) پیش از آن متن را مصرف می‌کرد؛ چون هندلرهایِ مستقیمِ
#     tenant_router همیشه پیش از زیرروترها امتحان می‌شوند؛
#   • برایِ مشتری‌ای که روی صفحهٔ یک محصول است (state=user_link)، smmu_link_in عدد را
#     به‌عنوانِ «لینکِ سفارش» می‌خواند، و StateFilter(None) هندلرِ من را کنار می‌گذاشت.
# حالا منطقِ باز کردنِ محصول این‌جاست و از هندلرِ اولویت‌دارِ tenant_router
# (tenant_svc_shortcut در tenant/__init__.py) صدا زده می‌شود.
async def open_product_shortcut(msg: Message, bot: Bot, state: FSMContext, vid: int,
                                bulk: bool = False) -> bool:
    """/<شماره> یا /multi_<شماره>: صفحهٔ محصول (یا شروعِ سفارشِ انبوه) را باز می‌کند.
    False یعنی چنین محصولِ فعالی در همین ربات نیست (تا فراخوان بتواند متن را به بقیهٔ
    روال‌ها بسپارد)."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return False
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id or not v.is_active:
        return False
    await state.clear()          # هر جریانِ نیمه‌کارهٔ سفارشِ قبلی کنار گذاشته می‌شود
    if bulk and not _is_vnum_service(v):
        await _bulk_start_prompt(msg, state, v)
        return True
    return await send_smm_product_page(msg, bot, vid, state)


async def send_smm_product_page(msg_or_cb, bot: Bot, vid: int, state: FSMContext,
                                breadcrumb: str | None = None, back_cb: str | None = None) -> bool:
    """صفحهٔ خریدِ یک سرویسِ SMM مشخص را نشون می‌ده — چه از وسطِ مرورِ عادی
    صدا زده بشه (cb از smmu_pick_service)، چه مستقیم از یه دکمهٔ سفارشی
    که ادمین این سرویس رو بهش وصل کرده (بدونِ نیازِ به مرورِ پلتفرم/نوع).
    خروجی False یعنی سرویس پیدا نشد/غیرفعاله.
    === PATCH_SMM_QUICKLINK_BREADCRUMB_V1 ===
    breadcrumb اختیاریه: وقتی از مسیرِ «اتصالِ سریع» صدا زده بشه (مثلاً
    «اینستاگرام › ممبر»)، بالایِ صفحه نشون داده می‌شه."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return False
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id or not v.is_active:
        return False
    is_cb = isinstance(msg_or_cb, CallbackQuery)
    target = msg_or_cb.message if is_cb else msg_or_cb
    # === PATCH_SMM_VNUM_ROUTE_V1 ===
    # شمارهٔ مجازی نه لینک می‌خواهد نه تعداد؛ کارتِ خودش را بفرست.
    if _is_vnum_service(v):
        await state.clear()
        if is_cb:
            await msg_or_cb.answer()
        await _send_vnum_service_card(target, row.id, v, breadcrumb)
        return True
    # PATCH_SMM_UNIT_SAMPLE_V1 — برای سرویسِ تکی، نمونه با تعداد ۱
    _sample_qty = 1 if _qty_factor(v) == 1.0 else 1000
    sample = await compute_sell_price(row.id, v, _sample_qty)
    await state.set_state(SmmFSM.user_link)
    await state.update_data(smmu_svc_id=vid)
    _ik = _input_kind(v)       # PATCH_SMM_INPUT_KIND_V1 — سفارشِ انبوه فقط برایِ ورودیِ لینک
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        *([[InlineKeyboardButton(text="📦 ثبتِ سفارشِ انبوه (چندلینکی)", callback_data=f"smmu:bulk:{vid}",
                                 style=ButtonStyle.PRIMARY)]] if _ik == "link" else []),
        ([InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=back_cb, style=ButtonStyle.DANGER),
          InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)]
         if back_cb else
         [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)])])
    _rate = ""
    try:
        from tenant.shared.product_rating import rating_line, smm_key
        _rate = await rating_line(row.id, smm_key(vid))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (نمایشِ امتیازِ سرویسِ مجازی)")
    # PATCH_SMM_AUTO_DESC_V1
    desc_line = await build_description(row.id if row else 0, v)
    # === PATCH_SMM_QUICKLINK_BREADCRUMB_V1 ===
    crumb_line = f"📂 {breadcrumb} › {v.name}\n\n" if breadcrumb else ""
    # === PATCH_SMM_PRICE_VIEW_MODE_V1 ===
    # علاوه بر قیمتِ واحد، قیمتِ «حداقلِ سفارشِ همین سرویس» هم قابلِ نمایش
    # است — چون واحدِ خام (هر ۱۰۰۰) برایِ سرویسی با حداقلِ ۱۰ تا عددی است
    # که مشتری اصلاً نمی‌تواند بخرد و فقط گیجش می‌کند.
    _pview = await _get_setting(row.id if row else 0, "smm_price_view", "unit")
    _price_lines = []
    if _pview in ("unit", "both"):
        _price_lines.append(
            f"💵 قیمت: حدود <b>{sample:,} تومان</b> به‌ازای {unit_label(v)}")
    if _pview in ("min", "both") and (v.min_qty or 0) > 0:
        _min_price = await compute_sell_price(row.id, v, int(v.min_qty))
        _price_lines.append(
            f"🧾 حداقلِ سفارش ({int(v.min_qty):,} عدد): <b>{_min_price:,} تومان</b>")
    # === PATCH_SMM_PRICE_VIEW_MIN_FALLBACK_HINT_V1 ===
    # گزارشِ ادمین: «هرچی تنظیم می‌کنم فقط قیمتِ هر ۱۰۰۰ تا رو نشون می‌ده».
    # علتش این نبود که خودِ تنظیم (smm_price_view) کار نمی‌کرد — این سرویسِ
    # خاص min_qty=0 داشت (حداقلِ سفارش تعریف نشده)، پس با انتخابِ حالتِ
    # «min»، هیچ خطی اضافه نمی‌شد و همین fallback (که از قبل هم بود، فقط
    # بی‌صدا) دوباره برمی‌گشت به نمایشِ واحدِ خام — دقیقاً همون چیزی که به
    # ادمین «باگ» نشون می‌داد، چون هیچ توضیحی همراهش نبود. حالا وقتی
    # fallback فعال می‌شه، صریح می‌گه چرا.
    if not _price_lines and _pview == "min":
        _price_lines.append(
            f"💵 قیمت: حدود <b>{sample:,} تومان</b> به‌ازای {unit_label(v)}\n"
            "<i>(این سرویس حداقلِ سفارشِ تعریف‌شده‌ای ندارد، پس قیمتِ واحد نشان داده شد)</i>")
    if not _price_lines:   # حالتِ «min» ولی سرویس حداقل ندارد — بی‌قیمت نماند
        _price_lines.append(
            f"💵 قیمت: حدود <b>{sample:,} تومان</b> به‌ازای {unit_label(v)}")
    # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 === اگر ادمین نوعِ لینک را تنظیم کرده، دقیقاً همان را بگو
    _link_prompt = "لینک هدف (کانال/پست/پروفایل) را بفرست:"
    try:
        if v.category_pk:
            async with Session() as s:
                _cat = await s.get(SmmCategory, v.category_pk)
            _lk = await _cat_link_types(_cat)
            if _lk:
                _link_prompt = (f"لینک هدف را بفرست ({smm_link_types.short_labels(_cat.platform, _lk)}):")
    except Exception:
        logger.exception("[SMM] خواندنِ نوعِ لینکِ مجازِ دسته برایِ صفحهٔ محصول ناموفق بود")
    text = (
        f"{crumb_line}"
        f"🛍 <b>{v.name}</b>\n\n"
        + "\n".join(_price_lines) + "\n"
        f"🔢 حداقل: {v.min_qty} | حداکثر: {v.max_qty}\n"
        # === PATCH_SMM_PRODUCT_SHORTCUT_ROUTING_V1 === دستورهایِ میانبر (تپ‌شدنی)
        f"⌨️ میانبر: /{v.id}\n"
        + (f"📦 ثبت سفارش انبوه: /multi_{v.id}" if _ik == "link" else "")
        + f"{_rate}"
        f"{desc_line}\n\n"
        f"۱/۲ — {_input_prompt(_ik, _link_prompt)}"
    )
    if is_cb:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
        except Exception:
            await target.answer(text, parse_mode="HTML", reply_markup=cancel_kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=cancel_kb)
    return True


def _smm_link_confirm_kb(kind: str = "link") -> InlineKeyboardMarkup:
    _w = "لینک" if kind == "link" else ("آیدی" if kind == "number" else "اطلاعات")   # PATCH_SMM_INPUT_KIND_V1
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ تأیید {_w}", callback_data="smmu:link_confirm", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text=f"✏️ تغییر {_w}", callback_data="smmu:link_change", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو سفارش", callback_data="smmu:order_cancel", style=ButtonStyle.DANGER)],
    ])


# === PATCH_SMM_ORDER_DISCOUNT_V1 ===
# گزارش: کدِ تخفیف (دستی + خودکارِ اولین‌خرید) فقط تویِ خریدِ گیفت/پرمیوم/
# استارز کار می‌کرد، نه تویِ سفارشِ اصلیِ سرویس‌ها (فالوور/ویو/...) —
# دقیقاً همون چیزی که مشتری تویِ تیکت پرسید. این بخش همون الگویِ
# discount_engine که تویِ shop.py هست رو برایِ سفارشِ تکی و انبوهِ SMM
# هم پیاده می‌کنه.
async def _smm_discount_row(bot_id: int, disc: dict | None,
                            del_cb: str, add_cb: str) -> list[list[InlineKeyboardButton]]:
    if disc:
        return [[InlineKeyboardButton(text="🗑 حذف کد تخفیف", callback_data=del_cb,
                                      style=ButtonStyle.DANGER)]]
    from tenant import get_setting as _gs
    if await _gs(bot_id, "discount_purchase_enabled", "1") == "1":
        return [[InlineKeyboardButton(text="🏷 وارد کردن کد تخفیف", callback_data=add_cb)]]
    return []


def _smm_price_line(price: int, disc: dict | None, label: str = "مبلغ") -> str:
    if disc:
        return (f"💰 {label}: <s>{disc['orig_price']:,}</s> ➜ <b>{disc['final_price']:,} تومان</b>\n"
                f"🏷 کد <code>{_html_escape(disc['code'])}</code> اعمال شد "
                f"(−{disc['discount']:,} تومان)")
    return f"💰 {label}: <b>{price:,} تومان</b>"


# === PATCH_SMM_AUTO_DISCOUNT_PREVIEW_V1 ===
# تخفیف‌هایِ خودکار (اولین خرید + تخفیفِ رتبه‌ایِ ۵ نفرِ برتر) هم در خودِ کسرِ مبلغ و هم
# در پیش‌نمایش/فاکتور از *همین یک تابع* می‌آیند، تا عددی که مشتری در پیش‌نمایش می‌بیند
# دقیقاً همان عددی باشد که کسر می‌شود. اگر کدِ تخفیفِ دستی اعمال شده باشد، این‌ها
# اعمال نمی‌شوند (مثلِ قبل). اگر هر دو باشند، تخفیفِ بزرگ‌تر اعمال می‌شود (جمع نمی‌شوند).
async def _smm_auto_discount(bot_id: int, tg_id: int | None, price: int, session=None) -> dict | None:
    price = int(price)
    if not tg_id or price <= 0:
        return None
    best = None
    try:
        from tenant.gateways.discount_engine import first_purchase_discount
        fd = await first_purchase_discount(bot_id=bot_id, telegram_id=tg_id, price=price,
                                           session=session)
        if fd.ok and fd.discount > 0:
            best = {"kind": "first", "discount": int(fd.discount)}
    except Exception:
        logger.exception("[SMM] محاسبهٔ تخفیفِ اولین خرید ناموفق بود")
    try:
        from tenant.shared.top_customers import tc_rank_enabled, get_user_rank, tc_rank_percent
        if await tc_rank_enabled(bot_id):
            rank = await get_user_rank(bot_id, tg_id)
            if rank is not None:
                pct = await tc_rank_percent(bot_id, rank)
                if pct > 0:
                    d = round(price * min(100, pct) / 100)
                    if d > 0 and (best is None or d > best["discount"]):
                        best = {"kind": "rank", "discount": int(d), "rank": rank, "pct": pct}
    except Exception:
        logger.exception("[SMM] محاسبهٔ تخفیفِ رتبه‌ایِ نفراتِ برتر ناموفق بود")
    if not best:
        return None
    best["discount"] = max(0, min(best["discount"], price))
    best["orig"] = price
    best["final"] = price - best["discount"]
    return best


def _smm_auto_discount_text(ad: dict, label: str = "مبلغ") -> str:
    lines = []
    if ad["kind"] == "rank":
        lines.append(f"🏆 شما رتبهٔ <b>{ad['rank']}</b> نفراتِ برتر هستید — "
                     f"<b>{ad['pct']}٪</b> تخفیف روی هر سفارش دارید.")
    else:
        lines.append("🎁 چون این <b>اولین سفارشِ</b> شماست، تخفیفِ اولین خرید اعمال می‌شود.")
    lines.append(f"💸 مبلغِ تخفیفِ این سفارش: <b>−{ad['discount']:,} تومان</b>")
    lines.append(f"💰 {label}: <s>{ad['orig']:,}</s> ➜ <b>{ad['final']:,} تومان</b> (مبلغِ نهاییِ فاکتور)")
    return "\n".join(lines)


async def _smm_qty_preview_text_kb(bot_id: int, v, link: str, qty: int, price: int,
                                   disc: dict | None, tg_id: int | None = None):
    _ad = None if disc else await _smm_auto_discount(bot_id, tg_id, price)
    _price_txt = _smm_auto_discount_text(_ad) if _ad else _smm_price_line(price, disc)
    text = (
        "🧾 <b>پیش‌نمایش سفارش</b>\n\n"
        f"🛍 سرویس: {_html_escape(v.name)}\n"
        f"{'🔢 آیدی/شماره' if str(link).isdigit() and _input_kind(v) == 'number' else '🔗 لینک'}: "
        f"<code>{_html_escape(link)}</code>\n"
        f"🔢 تعداد: <b>{qty:,}</b>\n"
        f"{_price_txt}\n\n"
        "اطلاعات سفارش درست است؟"
    )
    kb_rows = [
        [InlineKeyboardButton(text="✅ تأیید سفارش", callback_data="smmu:qty_confirm", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="✏️ تغییر تعداد", callback_data="smmu:qty_change", style=ButtonStyle.PRIMARY)],
    ]
    kb_rows += await _smm_discount_row(bot_id, disc, "smmu:disc:del", "smmu:disc:add")
    kb_rows.append([InlineKeyboardButton(text="❌ لغو سفارش", callback_data="smmu:order_cancel",
                                         style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _smm_bulk_preview_text_kb(bot_id: int, v, links: list[str], qty: int,
                                    unit_price: int, disc: dict | None, tg_id: int | None = None):
    total = unit_price * len(links)
    price_disp = disc["orig_price"] if disc else total
    _ad = None if disc else await _smm_auto_discount(bot_id, tg_id, total)
    links_preview = "\n".join(f"{i+1}. <code>{_html_escape(l)}</code>" for i, l in enumerate(links[:10]))
    more = f"\n… و {len(links) - 10} موردِ دیگر" if len(links) > 10 else ""
    text = (
        "🧾 <b>پیش‌نمایشِ سفارشِ انبوه</b>\n\n"
        f"🛍 سرویس: {_html_escape(v.name)}\n"
        f"📦 تعدادِ لینک‌ها: <b>{len(links)}</b>\n"
        f"🔢 تعداد برایِ هرکدوم: <b>{qty:,}</b>\n"
        f"💵 قیمتِ هرکدوم: {unit_price:,} تومان\n"
        f"{_smm_auto_discount_text(_ad, label='مجموعِ قابلِ پرداخت') if _ad else _smm_price_line(price_disp, disc, label='مجموعِ قابلِ پرداخت')}\n\n"
        f"{links_preview}{more}\n\n"
        "همه‌چیز درست است؟"
    )
    kb_rows = [[InlineKeyboardButton(text="✅ تأیید و پرداخت", callback_data="smmu:bulkconfirm",
                                     style=ButtonStyle.SUCCESS)]]
    kb_rows += await _smm_discount_row(bot_id, disc, "smmu:bulkdisc:del", "smmu:bulkdisc:add")
    kb_rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="smmu:order_cancel",
                                         style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _smm_qty_confirm_kb() -> InlineKeyboardMarkup:
    # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
    # این تابع دیگه مستقیم استفاده نمی‌شه (جایگزینش _smm_qty_preview_text_kb
    # هست که دکمه‌ی تخفیف رو هم مشروط اضافه می‌کنه)؛ فقط برایِ سازگاریِ
    # احتمالیِ کدِ خارجی/تست نگه داشته شده.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید سفارش", callback_data="smmu:qty_confirm", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="✏️ تغییر تعداد", callback_data="smmu:qty_change", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو سفارش", callback_data="smmu:order_cancel", style=ButtonStyle.DANGER)],
    ])


# === PATCH_SMM_BULK_ORDER_V1 ===
# ثبتِ سفارشِ انبوه: مشتری چند لینک می‌فرستد (هر خط یکی)، یک تعداد برایِ
# همه‌شان انتخاب می‌کند، و هرکدام به‌صورتِ یک SmmOrder کاملاً مستقل ثبت
# می‌شود — دقیقاً مثلِ اینکه همون سفارش رو N بار جدا زده باشه، فقط با
# یه ویزاردِ کوتاه‌تر. کسرِ کیف‌پول یک‌جا (اتمیک، برایِ جمعِ کل) انجام
# می‌شود تا در وسطِ کار موجودی تمام نشود؛ ثبتِ رویِ پنل ولی یکی‌یکی و در
# پس‌زمینه انجام می‌شود (چون هرکدوم یه تماسِ شبکه‌ایِ واقعیه) و برایِ هر
# کدوم یه رسیدِ جدا فرستاده می‌شود.
_MAX_BULK_LINKS = 100


@smm_router.callback_query(F.data.startswith("smmu:bulk:"))
async def smmu_bulk_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    vid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        v = await s.get(SmmService, vid)
        if v is not None and await _is_foreign(v, bot):   # PATCH_TENANT_SCOPE_V1
            v = None
    if not v or not v.is_active:
        await cb.answer("این سرویس دیگر موجود نیست.", show_alert=True)
        return
    await _bulk_start_prompt(cb.message, state, v)
    await cb.answer()


async def _bulk_start_prompt(target, state: FSMContext, v: "SmmService") -> None:
    """شروعِ سفارشِ انبوه برایِ سرویسِ v — هم از دکمه (smmu:bulk:) و هم از دستورِ /multi_<شماره>."""
    if _input_kind(v) != "link":      # PATCH_SMM_INPUT_KIND_V1
        await target.answer("برایِ این محصول سفارشِ انبوه وجود ندارد؛ همان صفحهٔ محصول را باز کن و آیدی/شماره را بفرست.")
        return
    await state.set_state(SmmFSM.bulk_links)
    await state.update_data(smmu_svc_id=v.id)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)]])
    await target.answer(
        f"📦 <b>ثبتِ سفارشِ انبوه — {_html_escape(v.name)}</b>\n\n"
        f"لینک‌ها رو بفرست، هر خط یک لینک (حداکثر {_MAX_BULK_LINKS} تا):\n\n"
        "<code>@username1\n@username2\nhttps://t.me/username3</code>",
        parse_mode="HTML", reply_markup=cancel_kb)


def _looks_like_link(s: str) -> bool:
    s = s.strip()
    return bool(s) and (s.startswith("http://") or s.startswith("https://")
                        or s.startswith("@") or "t.me/" in s)


@smm_router.message(SmmFSM.bulk_links, F.text)
async def smmu_bulk_links_in(msg: Message, bot: Bot, state: FSMContext):
    lines = [ln.strip() for ln in (msg.text or "").splitlines() if ln.strip()]
    if not lines:
        await msg.answer("هیچ لینکی پیدا نشد. حداقل یک خط لینک بفرست:")
        return
    if len(lines) > _MAX_BULK_LINKS:
        await msg.answer(f"حداکثر {_MAX_BULK_LINKS} لینک در هر بار مجاز است "
                         f"(الان {len(lines)} خط فرستادی). دوباره بفرست:")
        return
    bad = [ln for ln in lines if not _looks_like_link(ln)]
    if bad:
        preview = "\n".join(f"• {_html_escape(b)}" for b in bad[:10])
        await msg.answer(
            f"❌ این خط‌ها لینکِ معتبر به‌نظر نمی‌رسن:\n{preview}\n\n"
            "همهٔ خط‌ها باید با http/https شروع بشن، یا @یوزرنیم/لینکِ t.me باشن. "
            "دوباره کاملِ لیست رو بفرست:", parse_mode="HTML")
        return
    # === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
    _data0 = await state.get_data()
    async with Session() as s:
        _v0 = await s.get(SmmService, _data0.get("smmu_svc_id"))
        _cat0 = await s.get(SmmCategory, _v0.category_pk) if _v0 and _v0.category_pk else None
    # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 === (نوعِ لینکِ مجازِ دسته، وگرنه نمونه‌لینک)
    _wrong, _hint = await _check_links_for_category(_cat0, lines)
    if _wrong:
        preview = "\n".join(f"• {_html_escape(w)}" for w in _wrong[:10])
        await msg.answer(
            f"❌ این خط‌ها برایِ این سرویس درست به‌نظر نمی‌رسن:\n{preview}\n\n"
            f"{_hint}\n\n"
            "دوباره کاملِ لیست رو بفرست:", parse_mode="HTML")
        return
    await state.update_data(smmu_bulk_links=lines)
    await state.set_state(SmmFSM.bulk_qty)
    data = await state.get_data()
    async with Session() as s:
        v = await s.get(SmmService, data.get("smmu_svc_id"))
    rng = f"({v.min_qty} تا {v.max_qty})" if v else ""
    await msg.answer(
        f"✅ {len(lines)} لینک دریافت شد.\n\n"
        f"۲/۲ — تعداد برایِ <b>هرکدوم</b> از این لینک‌ها را بفرست {rng} (فقط عدد):",
        parse_mode="HTML")


@smm_router.message(SmmFSM.bulk_qty, F.text)
async def smmu_bulk_qty_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("سفارشِ انبوه لغو شد.", reply_markup=await inline_back_kb(row.id))
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit() or int(raw) <= 0:
        await msg.answer("فقط عدد بفرست:")
        return
    qty = int(raw)
    data = await state.get_data()
    vid = data.get("smmu_svc_id")
    links = data.get("smmu_bulk_links") or []
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id or not v.is_active:
        await state.clear()
        await msg.answer("این سرویس دیگر در دسترس نیست.")
        return
    if v.min_qty and qty < v.min_qty:
        await msg.answer(f"حداقل تعداد برای این سرویس {v.min_qty} است.")
        return
    if v.max_qty and qty > v.max_qty:
        await msg.answer(f"حداکثر تعداد برای این سرویس {v.max_qty} است.")
        return
    unit_price = await compute_sell_price(row.id, v, qty)
    if unit_price <= 0:
        await state.clear()
        await msg.answer("قیمت این سرویس تنظیم نشده. لطفاً بعداً تلاش کن.")
        return
    total = unit_price * len(links)
    # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
    # تعداد/تعدادِ لینک‌ها عوض شده → مجموع فرق می‌کنه، کدِ قبلی نامعتبر می‌شه.
    await state.update_data(smmu_bulk_qty=qty, smmu_bulk_unit_price=unit_price, smmu_bulk_discount=None)
    text, kb = await _smm_bulk_preview_text_kb(row.id, v, links, qty, unit_price, None,
                                               tg_id=msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


async def _smm_bulk_process_bg(bot: Bot, row, order_ids: list[int], chat_id: int) -> None:
    """پردازشِ پس‌زمینهٔ سفارش‌هایِ انبوه — یکی‌یکی رویِ پنل ثبت می‌کند و
    برایِ هرکدام یه رسیدِ جدا می‌فرستد. جدا از هندلرِ اصلی اجرا می‌شود
    (asyncio.create_task) چون ممکن است ده‌ها تماسِ شبکه‌ایِ واقعی پشتِ‌سرِ
    هم طول بکشد و نباید جلویِ پاسخ‌گوییِ فوریِ ربات را بگیرد."""
    ok_count = 0
    for oid in order_ids:
        async with Session() as s:
            o = await s.get(SmmOrder, oid)
        if not o:
            continue
        # === PATCH_SMM_TRUST_CHANNEL_V1 ===
        # همون اطلاع‌رسانیِ خرید (ادمین‌ها + کانالِ اعتمادسازی) که مسیرِ
        # تک‌سفارشی از قبل داره، برایِ هر سفارشِ انبوه هم باید بره — وگرنه
        # این مسیر «فروشِ بی‌گزارش» می‌شد.
        await _notify_smm_purchase_success_by_order(bot, row, oid)
        async with Session() as s:
            v = await s.get(SmmService, o.smm_service_pk) if o.smm_service_pk else None
        order_mode = await _panel_order_mode(row.id, o.panel_pk)
        if order_mode == "auto" and not (v and v.is_manual):
            ok, note = await _place_on_panel(row.id, oid)
        else:
            ok, note = True, "در انتظارِ تأییدِ ادمین"
            await announce_manual_smm_order(bot, row, oid)
        ok_count += 1 if ok else 0
        try:
            status_txt = "✅ ثبت شد" if ok else f"❌ ناموفق: {_customer_fail_reason(note)}"   # PATCH_SMM_PANEL_ERROR_TEXT_V1
            await bot.send_message(
                chat_id,
                f"📦 سفارشِ <code>{o.tracking_code or o.id}</code>\n"
                f"🔗 {o.link}\n{status_txt}", parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (ارسالِ رسیدِ سفارشِ انبوه)")
        await asyncio.sleep(0.3)  # از فلادِ ارسالِ پیام/درخواستِ پیاپی به پنل جلوگیری می‌کند
    try:
        await bot.send_message(
            chat_id, f"🏁 پردازشِ سفارشِ انبوه تمام شد — {ok_count} از {len(order_ids)} با موفقیت ثبت شد.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (پیامِ پایانیِ سفارشِ انبوه)")


@smm_router.callback_query(F.data == "smmu:bulkconfirm")
async def smmu_bulk_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    data = await state.get_data()
    vid = data.get("smmu_svc_id")
    links = data.get("smmu_bulk_links") or []
    qty = data.get("smmu_bulk_qty")
    unit_price = data.get("smmu_bulk_unit_price")
    if not (vid and links and qty and unit_price):
        await cb.answer("اطلاعاتِ سفارشِ انبوه پیدا نشد؛ دوباره از انتخابِ سرویس شروع کن.", show_alert=True)
        return
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id or not v.is_active:
        await state.clear()
        await cb.answer("این سرویس دیگر در دسترس نیست.", show_alert=True)
        return
    # قیمت در لحظهٔ پرداخت دوباره حساب می‌شود (نه از state): اگر ادمین بینِ
    # پیش‌نمایش و تأیید قیمت/حداقل را عوض کرده باشد، نباید با عددِ کهنه فروخت.
    if ((v.min_qty and qty < v.min_qty) or (v.max_qty and qty > v.max_qty)
            or not isinstance(qty, int) or qty <= 0):
        await cb.answer("تعدادِ سفارش دیگر معتبر نیست؛ دوباره تعداد را وارد کن.", show_alert=True)
        return
    unit_price = await compute_sell_price(row.id, v, qty)
    if unit_price <= 0:
        await cb.answer("قیمت این سرویس تنظیم نشده. لطفاً بعداً تلاش کن.", show_alert=True)
        return
    total = float(unit_price) * len(links)
    disc = data.get("smmu_bulk_discount") or None
    u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name,
                                 cb.from_user.username or "")

    # ── کسرِ اتمیکِ مبلغِ کل + ساختِ همهٔ سفارش‌ها در یک تراکنش ──
    async with Session() as s:
        uu = await s.get(BotUser, u.id, with_for_update=True)
        # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
        # همون الگویِ اعتبارسنجیِ اتمیکِ shop.py — دوباره چک می‌کنیم چون
        # ممکنه بینِ پیش‌نمایش و همین لحظه کد منقضی/پر شده باشه.
        final_total = total
        applied_code_id = None
        applied_discount_amt = 0
        if disc and disc.get("code"):
            from tenant.gateways.discount_engine import validate_discount
            res = await validate_discount(
                bot_id=row.id, code=disc["code"], telegram_id=cb.from_user.id,
                price=int(total), product_key="smm", session=s)   # PATCH_VS_DISCOUNT_V1
            if res.ok:
                final_total = float(res.final_price)
                applied_code_id = res.code_id
                applied_discount_amt = res.discount
        if not applied_code_id:
            # === PATCH_SMM_AUTO_DISCOUNT_PREVIEW_V1 === همان تابعِ پیش‌نمایش ⇒ عددِ یکسان
            _ad = await _smm_auto_discount(row.id, cb.from_user.id, int(total), session=s)
            if _ad:
                final_total = float(_ad["final"])
        if not uu or float(uu.balance) < final_total:
            bal = float(uu.balance) if uu else 0
            await cb.answer()
            from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — دکمهٔ «شارژ حساب»
            _bk = await inline_back_kb(row.id)
            await cb.message.answer(
                f"❌ موجودی کیف پولت کافی نیست.\n"
                f"مبلغِ کل: {int(final_total):,} تومان\nموجودیِ تو: {int(bal):,} تومان\n\n"
                f"ابتدا کیف پولت را شارژ کن.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    *await _vp.charge_row(row.id, cb.from_user.id, int(final_total)), *(_bk.inline_keyboard if _bk else [])]))
            return
        uu.balance = float(uu.balance) - final_total
        cost_unit = await compute_cost_price(row.id, v, qty)
        now = datetime.utcnow()
        # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
        # تخفیفِ کل رو مساوی بینِ سفارش‌هایِ تک‌تکِ لینک‌ها پخش می‌کنیم تا
        # جمعِ price_toman هر سفارش با مبلغِ واقعاً کسرشده جور دربیاد.
        final_unit_price = final_total / len(links)
        order_ids: list[int] = []
        for link in links:
            o = SmmOrder(
                bot_id=row.id, user_telegram_id=cb.from_user.id,
                smm_service_pk=v.id, service_id=v.service_id, service_name=v.name,
                link=link, quantity=qty, price_toman=final_unit_price,
                status="awaiting_admin", cost_toman=cost_unit, updated_at=now,
                panel_pk=v.panel_pk)
            s.add(o)
            await s.flush()   # before_insert خودش شمارهٔ ترتیبیِ سفارش را می‌دهد؛ دوباره نگیر (قبلاً «دوتا دوتا» بالا می‌رفت)
            order_ids.append(o.id)
        _note_extra = f" (تخفیف −{int(total - final_total):,})" if final_total < total else ""
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-final_total,
                        kind="smm_order",
                        note=f"سفارشِ انبوه: {v.name[:40]} × {qty} × {len(links)} لینک{_note_extra}"))
        if applied_code_id:
            from tenant.gateways.discount_engine import record_usage
            await record_usage(s, code_id=applied_code_id, bot_id=row.id,
                               telegram_id=cb.from_user.id, saved_amount=applied_discount_amt)
        await s.commit()
    await state.update_data(smmu_bulk_discount=None)
    await state.clear()

    await cb.answer()
    await cb.message.answer(
        f"✅ {len(order_ids)} سفارش در صف قرار گرفت و به‌تدریج پردازش می‌شود. "
        "وضعیتِ هرکدام جداگانه براتون ارسال می‌شود.")
    asyncio.create_task(_smm_bulk_process_bg(bot, row, order_ids, cb.message.chat.id))


# === PATCH_SMM_ORDER_DISCOUNT_V1 ===
@smm_router.callback_query(F.data == "smmu:bulkdisc:add")
async def smmu_bulkdisc_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف غیرفعال است.", show_alert=True)
        return
    await state.set_state(SmmFSM.bulk_disc_code)
    await cb.message.answer("🏷 کد تخفیف را بفرست:\n(یا /cancel برای انصراف)")
    await cb.answer()


@smm_router.message(SmmFSM.bulk_disc_code, F.text)
async def smmu_bulkdisc_enter(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    vid = data.get("smmu_svc_id")
    links = data.get("smmu_bulk_links") or []
    qty = data.get("smmu_bulk_qty")
    unit_price = data.get("smmu_bulk_unit_price")
    async with Session() as s:
        v = await s.get(SmmService, vid) if vid else None
    if not v or not links or not qty or not unit_price:
        await state.clear()
        await msg.answer("اطلاعاتِ سفارشِ انبوه پیدا نشد؛ دوباره از انتخابِ سرویس شروع کن.")
        return
    raw = (msg.text or "").strip()
    if raw.lower() in ("/cancel", "❌ لغو", "❌ لغو / بازگشت", "لغو", "انصراف"):
        text, kb = await _smm_bulk_preview_text_kb(row.id, v, links, qty, unit_price,
                                                    data.get("smmu_bulk_discount"),
                                                    tg_id=msg.from_user.id)
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    total = int(unit_price) * len(links)
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(
        bot_id=row.id, code=raw, telegram_id=msg.from_user.id, price=total, product_key="smm")   # PATCH_VS_DISCOUNT_V1
    if not res.ok:
        await msg.answer(f"❌ {res.reason or 'کد تخفیف نامعتبر است.'}\nدوباره بفرست یا /cancel بزن:")
        return
    disc = {"code": res.code, "code_id": res.code_id, "discount": res.discount,
            "final_price": res.final_price, "orig_price": total}
    await state.update_data(smmu_bulk_discount=disc)
    text, kb = await _smm_bulk_preview_text_kb(row.id, v, links, qty, unit_price, disc,
                                               tg_id=msg.from_user.id)
    await msg.answer(f"✅ کد تخفیف اعمال شد.\n\n{text}", parse_mode="HTML", reply_markup=kb)


@smm_router.callback_query(F.data == "smmu:bulkdisc:del")
async def smmu_bulkdisc_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await state.update_data(smmu_bulk_discount=None)
    data = await state.get_data()
    vid = data.get("smmu_svc_id")
    links = data.get("smmu_bulk_links") or []
    qty = data.get("smmu_bulk_qty")
    unit_price = data.get("smmu_bulk_unit_price")
    async with Session() as s:
        v = await s.get(SmmService, vid) if vid else None
    if not v or not links or not qty or not unit_price:
        await cb.answer()
        return
    text, kb = await _smm_bulk_preview_text_kb(row.id, v, links, qty, unit_price, None,
                                               tg_id=cb.from_user.id)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer("حذف شد")


@smm_router.message(SmmFSM.user_link, F.text)
async def smmu_link_in(msg: Message, bot: Bot, state: FSMContext):
    link = msg.text.strip()
    # === PATCH_SMM_INPUT_KIND_V1 === محصولی که «عدد/متن» می‌خواهد: بدونِ چکِ لینک و بدونِ نوعِ لینکِ دسته
    _d0 = await state.get_data()
    _kind = "link"
    if _d0.get("smmu_svc_id"):
        async with Session() as s:
            _v0 = await s.get(SmmService, int(_d0["smmu_svc_id"]))
        if _v0 is not None and not await _is_foreign(_v0, bot):
            _kind = _input_kind(_v0)
    if _kind != "link":
        if _kind == "number":
            val = _norm_number_input(link)
            if val is None:
                await msg.answer("❌ فقط عدد بفرست (بدونِ حرف و فاصله)، مثلاً 123456:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                     text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)]]))
                return
        else:
            val = link[:300]
            if not val:
                await msg.answer("❌ متنی فرستاده نشد. دوباره بفرست:")
                return
        await state.update_data(smmu_link=val)
        await state.set_state(SmmFSM.user_link_confirm)
        await msg.answer(
            ("🔢 <b>آیدی/شماره دریافت شد</b>" if _kind == "number" else "✏️ <b>اطلاعات دریافت شد</b>")
            + f"\n\n<code>{_html_escape(val)}</code>\n\nآیا درست است؟",
            parse_mode="HTML", reply_markup=_smm_link_confirm_kb(_kind))
        return
    if not (link.startswith("http://") or link.startswith("https://") or link.startswith("@")

            or "t.me/" in link):
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)]])
        await msg.answer("لطفاً یک لینک معتبر بفرست (مثلاً https://t.me/...):",
                          reply_markup=cancel_kb)
        return
    # === PATCH_SMM_CATEGORY_LINK_PATTERN_V1 ===
    # درخواستِ ادمین: جلوگیری از فرستادنِ لینکِ پلتفرمِ اشتباه (مثلاً لینکِ
    # اینستاگرام برایِ سرویسِ تلگرام). فقط اگه خودِ ادمین برایِ دستهٔ این
    # سرویس یه نمونه‌لینک ثبت کرده باشه چک می‌شود؛ وگرنه رفتارِ قبلی (فقط
    # همون چکِ عمومیِ بالا) دست‌نخورده می‌ماند.
    data = await state.get_data()
    svc_id = data.get("smmu_svc_id")
    if svc_id:
        async with Session() as s:
            v = await s.get(SmmService, int(svc_id))
            if v is not None and await _is_foreign(v, bot):   # PATCH_TENANT_SCOPE_V1
                v = None
            cat = await s.get(SmmCategory, v.category_pk) if v and v.category_pk else None
        # === PATCH_SMM_CATEGORY_LINK_TYPES_V1 === (نوعِ لینکِ مجازِ دسته، وگرنه نمونه‌لینک)
        _wrong1, _hint1 = await _check_links_for_category(cat, [link])
        if _wrong1:
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)]])
            await msg.answer(
                f"❌ این لینک برایِ این سرویس درست به‌نظر نمی‌رسه.\n\n"
                f"{_hint1}\n\n"
                "یه لینکِ دیگه بفرست:",
                parse_mode="HTML", reply_markup=cancel_kb)
            return
    await state.update_data(smmu_link=link)
    await state.set_state(SmmFSM.user_link_confirm)
    await msg.answer(
        "🔗 <b>لینک دریافت شد</b>\n\n"
        f"<code>{_html_escape(link)}</code>\n\n"
        "آیا لینک درست است؟",
        parse_mode="HTML", reply_markup=_smm_link_confirm_kb())


async def _smm_prompt_quantity(target, bot: Bot, state: FSMContext, svc_id: int):
    await state.set_state(SmmFSM.user_qty)
    async with Session() as s:
        v = await s.get(SmmService, svc_id)
    rng = f"({v.min_qty} تا {v.max_qty})" if v else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🧮 محاسبه با موجودیِ من", callback_data=f"smmu:calc:{v.id}")]]) if v else None
    await target.answer(f"۲/۲ — تعداد موردنظر را بفرست {rng} (فقط عدد):", reply_markup=kb)


# === PATCH_SMM_ORDER_FLOW_CLEANUP_V1 ===
# گزارشِ ادمین (فدایی): «لینک را تأیید می‌کند ولی آن پیام ادیت/حذف نمی‌شود و پیامِ جدید
# می‌آید؛ پیویِ ربات شلوغ می‌ماند». در مسیرِ سفارش (لینک ← تعداد ← پیش‌نمایش ← تأیید)
# هر مرحله پیامِ تازه می‌فرستاد و پیامِ مرحلهٔ قبل با دکمه‌هایِ فعال می‌ماند. این
# تابع پیامِ منسوخ‌شده را پاک می‌کند. شکستش (مثلاً پیامِ قدیمی‌تر از ۴۸ ساعت یا از
# قبل پاک‌شده) هیچ‌وقت نباید جریانِ سفارش را بشکند.
async def _smm_del(message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("پاک‌کردنِ پیامِ مرحلهٔ قبلِ سفارش ممکن نشد (نادیده گرفته شد)", exc_info=True)


@smm_router.callback_query(F.data == "smmu:link_confirm")
async def smmu_link_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    data = await state.get_data()
    svc_id = data.get("smmu_svc_id")
    link = data.get("smmu_link")
    if not svc_id or not link:
        await cb.answer("لینک سفارش پیدا نشد؛ دوباره از انتخاب محصول شروع کن.", show_alert=True)
        return
    # === PATCH_VS_EDIT_IN_PLACE_V1 === همان پیامِ «تأیید/تغییرِ لینک» به مرحلهٔ تعداد ویرایش می‌شود
    await _smm_prompt_quantity(_EditFirstV(cb.message), bot, state, int(svc_id))
    await cb.answer("لینک تأیید شد")


@smm_router.callback_query(F.data == "smmu:link_change")
async def smmu_link_change(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.set_state(SmmFSM.user_link)
    _kind = "link"      # PATCH_SMM_INPUT_KIND_V1
    _sid = (await state.get_data()).get("smmu_svc_id")
    if _sid:
        async with Session() as s:
            _kind = _input_kind(await s.get(SmmService, int(_sid)))
    await _EditFirstV(cb.message).answer(   # PATCH_VS_EDIT_IN_PLACE_V1
        {"link": "✏️ لینک جدید را بفرست:", "number": "✏️ آیدی/شماره‌ی جدید را بفرست (فقط عدد):",
         "text": "✏️ اطلاعاتِ جدید را بفرست:"}[_kind],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو سفارش", callback_data="smmu:order_cancel", style=ButtonStyle.DANGER)
        ]]))
    await cb.answer()


@smm_router.callback_query(F.data == "smmu:order_cancel")
async def smmu_order_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    # === PATCH_SMM_ORDER_FLOW_CLEANUP_V1 === همان پیامی که دکمهٔ لغو رویش بود ادیت می‌شود
    # (نه یک پیامِ تازه)، تا پیش‌نمایشِ قدیمی با دکمه‌هایِ فعال در چت نماند.
    _cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home", style=ButtonStyle.DANGER)
    ]])
    try:
        await cb.message.edit_text("❌ سفارش لغو شد.", reply_markup=_cancel_kb)
    except Exception:
        await cb.message.answer("❌ سفارش لغو شد.", reply_markup=_cancel_kb)
        await _smm_del(cb.message)
    await cb.answer("لغو شد")


@smm_router.callback_query(F.data.startswith("smmu:calc:"))
async def smmu_calc_balance(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    vid = int(cb.data.split(":")[2])
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id:
        await cb.answer("این سرویس دیگر در دسترس نیست.", show_alert=True)
        return
    u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name,
                                 cb.from_user.username or "")
    async with Session() as s:
        uu = await s.get(BotUser, u.id)
        balance = float(uu.balance) if uu else 0.0

    # === PATCH_STARS_BALANCE_CALC_V1 ===
    # قیمت‌گذاری خطی است (هر واحد یک نرخِ ثابت دارد)، پس با گرفتنِ قیمتِ
    # نمونه برایِ ۱۰۰۰ واحد، نرخِ هر واحد را حساب می‌کنیم، سپس حداکثرِ
    # تعدادِ خریدنی را از رویِ موجودیِ کاربر به‌دست می‌آوریم — و برایِ
    # اطمینانِ کامل (اگر گردکردن باعثِ کمی رد شدن از موجودی بشود)، یک
    # واحد واحد عقب می‌رویم تا قیمتِ واقعی حتماً درونِ موجودی جا بگیرد.
    # PATCH_SMM_UNIT_SAMPLE_V1 — برای سرویسِ تکی، نمونه با تعداد ۱
    _sample_qty = 1 if _qty_factor(v) == 1.0 else 1000
    sample_price = await compute_sell_price(row.id, v, _sample_qty)
    if sample_price <= 0:
        await cb.answer("قیمتِ این سرویس هنوز تنظیم نشده.", show_alert=True)
        return
    per_unit = sample_price / 1000
    est_qty = int(balance // per_unit) if per_unit > 0 else 0
    est_qty = max(0, min(est_qty, v.max_qty or est_qty))
    while est_qty > 0:
        real_price = await compute_sell_price(row.id, v, est_qty)
        if real_price <= balance:
            break
        est_qty -= 1

    if est_qty < (v.min_qty or 1):
        await cb.answer(
            f"❌ با موجودیِ {int(balance):,} تومانت، حتی به حداقلِ تعداد "
            f"({v.min_qty}) هم نمی‌رسی. اول کیف‌پولت را شارژ کن.",
            show_alert=True)
        return
    final_price = await compute_sell_price(row.id, v, est_qty)
    await cb.answer(
        f"🧮 با موجودیِ {int(balance):,} تومانت، حداکثر {est_qty:,} عدد "
        f"می‌تونی بخری (قیمت: {int(final_price):,} تومان).",
        show_alert=True)


@smm_router.message(SmmFSM.user_qty, F.text)
async def smmu_qty_in(msg: Message, bot: Bot, state: FSMContext):
    await ensure_smm_columns()
    from tenant import get_bot_row, get_or_create_user, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    # باگِ گزارش‌شده (تیکت #400): این هندلر مسیرِ خریدِ مشتری است — قبلاً
    # هر متنِ غیرِعددی (مثلاً زدنِ دکمه‌ای از منویِ اصلی برایِ منصرف‌شدن)
    # را «فقط عدد بفرست» رد می‌کرد و مشتری برایِ همیشه همان‌جا گیر می‌کرد.
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("خرید لغو شد.", reply_markup=await inline_back_kb(row.id))
        return
    raw = raw.replace(",", "").replace("،", "")
    if not raw.isdigit() or int(raw) <= 0:
        await state.clear()
        raise SkipHandler
    qty = int(raw)
    data = await state.get_data()
    vid = data.get("smmu_svc_id")
    link = data.get("smmu_link", "")
    async with Session() as s:
        v = await s.get(SmmService, vid)
    if not v or v.bot_id != row.id or not v.is_active:
        await state.clear()
        await msg.answer("این سرویس دیگر در دسترس نیست.")
        return
    if v.min_qty and qty < v.min_qty:
        await msg.answer(f"حداقل تعداد برای این سرویس {v.min_qty} است.")
        return
    if v.max_qty and qty > v.max_qty:
        await msg.answer(f"حداکثر تعداد برای این سرویس {v.max_qty} است.")
        return

    price = await compute_sell_price(row.id, v, qty)
    if price <= 0:
        await state.clear()
        await msg.answer("قیمت این سرویس تنظیم نشده. لطفاً بعداً تلاش کن.")
        return

    if not bool(data.get("smmu_final_confirmed")):
        # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
        # تعداد عوض شده → قیمتِ پایه فرق می‌کنه، پس کدِ تخفیفِ قبلی (اگه بود)
        # دیگه معتبر نیست؛ کاربر باید دوباره واردش کنه.
        await state.update_data(smmu_qty=qty, smmu_price=price, smmu_final_confirmed=False,
                                smmu_discount=None)
        await state.set_state(SmmFSM.user_qty_confirm)
        text, kb = await _smm_qty_preview_text_kb(row.id, v, link, qty, price, None,
                                                  tg_id=msg.from_user.id)
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    await state.update_data(smmu_final_confirmed=False)
    disc = data.get("smmu_discount") or None

    res = await smm_place_order(bot, row, msg, v, link, qty, price, disc)
    await state.update_data(smmu_discount=None)
    await state.clear()
    return res


# === PATCH_SMM_PLACE_ORDER_CORE_V1 ===
# هستهٔ ثبتِ سفارشِ SMM — از دلِ smmu_qty_in بیرون کشیده شد تا مینی‌اپ هم دقیقاً همین
# مسیر را صدا بزند (تخفیف، کسرِ اتمیکِ کیف‌پول، ثبت روی پنل/دستی، اطلاع به ادمین،
# کانالِ اعتماد، کانالِ سفارش‌ها) و دو مسیر هرگز از هم واگرا نشوند.
# msg: هر شیئی که from_user (id/full_name/username) و answer(text, **kw) داشته باشد.
# خروجی: dict با status = ok | insufficient | panel_failed
# === PATCH_SMM_PLACE_ORDER_BG_NOTIFY_V1 ===
# گزارشِ مجددِ Omid: «سفارش‌هام خیلی دیر ثبت میشه». بعد از ثبتِ موفقِ سفارش، چند کارِ جانبیِ *شبکه‌ای* (پیام به ادمین‌ها،
# پستِ کانالِ اعتماد، پستِ کانالِ سفارش‌ها) هنوز پشتِ‌سرِ‌هم await می‌شد و تا تمامشان handler برنمی‌گشت؛ در نتیجه
# پیش‌نمایشِ قدیمیِ سفارش (با دکمهٔ تأیید) ثانیه‌ها روی صفحه می‌ماند و مشتری فکر می‌کرد ثبت نشده. حالا این‌ها
# بعد از پاسخِ مشتری در پس‌زمینه اجرا می‌شوند؛ نتیجهٔ سفارش و پیامِ مشتری عوض نشده.
_SMM_BG_TASKS: set = set()


def _smm_bg(coro) -> None:
    async def _runner():
        try:
            await coro
        except Exception:
            logger.exception("smm: کارِ پس‌زمینهٔ بعد از ثبتِ سفارش ناموفق بود (نادیده گرفته شد)")
    t = asyncio.create_task(_runner())
    _SMM_BG_TASKS.add(t)
    t.add_done_callback(_SMM_BG_TASKS.discard)


async def smm_place_order(bot: Bot, row, msg, v, link: str, qty: int, price: int, disc) -> dict:
    from tenant import get_or_create_user, inline_back_kb
    u = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name,
                                 msg.from_user.username or "")
    # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
    order_mode = await _panel_order_mode(row.id, v.panel_pk)

    # ── کسر اتمیک از کیف پول و ثبت سفارش ──
    async with Session() as s:
        uu = await s.get(BotUser, u.id, with_for_update=True)
        # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
        # اعتبارسنجیِ مجدد و اتمیکِ کدِ تخفیف — دقیقاً همون الگویی که تو
        # shop.py هست: بینِ پیش‌نمایش و همین لحظه ممکنه کد منقضی/پر شده
        # باشه، پس دوباره چک می‌شه. اگه کدی اعمال نشده، تخفیفِ خودکارِ
        # اولین‌خرید رو امتحان می‌کنه (دقیقاً مثلِ گیفت/پرمیوم/استارز).
        final_price = float(price)
        applied_code_id = None
        applied_discount_amt = 0
        if disc and disc.get("code"):
            from tenant.gateways.discount_engine import validate_discount
            res = await validate_discount(
                bot_id=row.id, code=disc["code"], telegram_id=msg.from_user.id,
                price=int(price), product_key="smm", session=s)   # PATCH_VS_DISCOUNT_V1
            if res.ok:
                final_price = float(res.final_price)
                applied_code_id = res.code_id
                applied_discount_amt = res.discount
        if not applied_code_id:
            # === PATCH_SMM_AUTO_DISCOUNT_PREVIEW_V1 === همان تابعِ پیش‌نمایش ⇒ عددِ یکسان
            _ad = await _smm_auto_discount(row.id, msg.from_user.id, int(price), session=s)
            if _ad:
                final_price = float(_ad["final"])
        if not uu or float(uu.balance) < final_price:
            bal = float(uu.balance) if uu else 0
            from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — دکمهٔ «شارژ حساب»
            _bk = await inline_back_kb(row.id)
            await msg.answer(
                f"❌ موجودی کیف پولت کافی نیست.\n"
                f"مبلغ سفارش: {int(final_price):,} تومان\nموجودی تو: {int(bal):,} تومان\n\n"
                f"ابتدا کیف پولت را شارژ کن.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    *await _vp.charge_row(row.id, msg.from_user.id, int(final_price)), *(_bk.inline_keyboard if _bk else [])]))
            return {"status": "insufficient", "need": int(final_price), "balance": int(bal)}
        uu.balance = float(uu.balance) - final_price
        # === PATCH_SMM_TRACKING_IS_ORDER_ID_V1 ===
        # قبلاً کدِ پیگیری ۸ کاراکترِ تصادفیِ لاتین بود («یه چیزِ انگلیسی و
        # چرت» به قولِ ادمین) که هیچ ربطی به لیستِ سفارشاتِ پنلِ مدیریت
        # نداشت: آن‌جا جست‌وجو با شناسهٔ عددیِ سفارش انجام می‌شود، پس وقتی
        # مشتری کدش را می‌فرستاد، ادمین نمی‌توانست پیدایش کند. حالا کد
        # خودِ شناسهٔ سفارش است. چون id قبل از درج مشخص نیست، اول flush
        # می‌کنیم تا id ساخته شود، بعد کد را می‌نویسیم — همه در یک تراکنش.
        # === PATCH_VS_ORDER_DETAIL_V1 ===
        cost = await compute_cost_price(row.id, v, qty)
        now = datetime.utcnow()
        o = SmmOrder(
            bot_id=row.id, user_telegram_id=msg.from_user.id,
            smm_service_pk=v.id, service_id=v.service_id, service_name=v.name,
            link=link, quantity=qty, price_toman=final_price,
            status="awaiting_admin",
            cost_toman=cost, updated_at=now,
            panel_pk=v.panel_pk)  # === PATCH_SMM_MULTI_PANEL_V1 ===
        s.add(o)
        await s.flush()          # تا o.id ساخته شود؛ before_insert همان‌جا شمارهٔ ترتیبی را می‌دهد
        trk = o.tracking_code    # دوباره شماره نگیر (قبلاً هر سفارش دو شماره مصرف می‌کرد)
        # === PATCH_SMM_ORDER_DISCOUNT_V1 ===
        _note_extra = f" (تخفیف −{int(price - final_price):,})" if final_price < price else ""
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-final_price,
                        kind="smm_order", note=f"سفارش SMM: {v.name[:40]} × {qty}{_note_extra}"))
        if applied_code_id:
            from tenant.gateways.discount_engine import record_usage
            await record_usage(s, code_id=applied_code_id, bot_id=row.id,
                               telegram_id=msg.from_user.id, saved_amount=applied_discount_amt)
        await s.commit()
        order_pk = o.id
        price = final_price  # از این‌جا به بعد، همه‌ی متن‌ها مبلغِ نهایی را نشان می‌دهند
    _status = "ok"

    if order_mode == "auto" and not v.is_manual:
        # === PATCH_SMM_LOADING_FEEDBACK_V1 ===
        # ثبتِ سفارش رویِ پنل یه تماسِ شبکه‌ایِ واقعی به یه سرورِ بیرونیه
        # که ما کنترلش نمی‌کنیم — گاهی چند ثانیه طول می‌کشه. قبلاً تویِ این
        # چند ثانیه هیچ واکنشی به مشتری نشون داده نمی‌شد و حسِ «کند/هنگ»
        # می‌داد. الان اول یه پیامِ «در حالِ ثبت...» می‌فرستیم، بعد همونو
        # با نتیجه‌ی نهایی ادیت می‌کنیم.
        pending_msg = await msg.answer("⏳ در حالِ ثبتِ سفارش رویِ پنل... چند لحظه صبر کن.")
        ok, note = await _place_on_panel(row.id, order_pk)
        try:
            await pending_msg.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if ok:
            # === PATCH_SMM_ORDER_PLACED_TEXT_V1 ===
            # اگر ادمین متنِ «🚀 ثبت روی پنل» را تنظیم کرده باشد، همان (با متغیرها) فرستاده می‌شود؛
            # وگرنه پیامِ پیش‌فرضِ قبلی بدونِ هیچ تغییری.
            _custom_sent = await _smm_custom_txt(row.id, "sent")
            if _custom_sent:
                _placed_txt = await _render_order_text(row.id, order_pk, _custom_sent)
            else:
                _placed_txt = (
                    f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n"
                    f"🛍 {v.name}\n🔢 تعداد: {qty:,}\n💰 مبلغ: {price:,} تومان\n"
                    f"🔖 کد پیگیری: <code>{trk}</code>\n\n"
                    f"وضعیت سفارش به‌صورت خودکار به‌روزرسانی می‌شود.")
            await _answer_html_safe(msg, _placed_txt, reply_markup=_order_done_kb(order_pk))   # PATCH_SMM_ORDER_DONE_KB_V1
            # === PATCH_SMM_TRUST_CHANNEL_V1 ===
            # اطلاعِ خرید به ادمین‌ها + پستِ کانالِ اعتمادسازی (تا الان
            # این مسیر _purchase_alert رو صدا نمی‌زد و کانالِ اعتمادسازیِ
            # ربات‌هایِ خدماتِ مجازی هرگز کار نمی‌کرد).
            async def _after_ok():
                await _notify_smm_purchase_success(bot, row, u, v, qty, price, order_pk)
                # === PATCH_SMM_ORDER_CHANNEL_V1 === پستِ سفارش در کانالِ خودکار
                from tenant.gateways.receipt_channel import post_smm_order
                await post_smm_order(bot, row.id, manual=False, service_name=v.name,
                                     link=link, quantity=qty, price=price, user=u,
                                     order_id=trk, status="ثبت‌شده روی پنل")
            _smm_bg(_after_ok())
        else:
            await msg.answer(
                f"⚠️ ثبت سفارش روی پنل با مشکل مواجه شد و مبلغ به کیف پولت بازگردانده شد.\n"
                f"علت: {_customer_fail_reason(note)}\n\nلطفاً بعداً دوباره تلاش کن.",
                reply_markup=await inline_back_kb(row.id))
            # === PATCH_SMM_ADMIN_FAILURE_ALERT_V1 ===
            # قبلاً وقتی سفارشِ خودکار روی پنل رد می‌شد (مثلاً موجودیِ پنل کم
            # بود)، فقط خودِ مشتری باخبر می‌شد و پولش برمی‌گشت — ادمین هیچ‌وقت
            # نمی‌فهمید، مگه اینکه مشتری بیاد تیکت بزنه (دقیقاً همون چیزی که
            # اتفاق افتاد). حالا ادمین هم فوراً باخبر می‌شه، با یه یادآوریِ
            # روشن اگه علتش «موجودیِ کم» بوده باشه.
            _smm_bg(_notify_smm_order_failed(bot, row, u, v, qty, price, note))
            _status = "panel_failed"
    else:
        # === PATCH_SMM_ORDER_PLACED_TEXT_V1 === متنِ دلخواهِ ادمین (اگر تنظیم شده) وگرنه پیش‌فرضِ قبلی
        _custom_manual = await _smm_custom_txt(row.id, "order_placed_manual")
        if _custom_manual:
            _manual_txt = await _render_order_text(row.id, order_pk, _custom_manual)
        else:
            _manual_txt = (
                f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n"
                f"🛍 {v.name}\n🔢 تعداد: {qty:,}\n💰 مبلغ: {price:,} تومان\n"
                f"🔖 کد پیگیری: <code>{trk}</code>\n\n"
                # === PATCH_SMM_CUSTOMER_ORDERS_REDESIGN_V1 ===
                # قبلاً به یه دکمه‌ی «🔎 پیگیریِ کد» اشاره می‌کرد که اصلاً وجود
                # نداشت — مشتری دنبالش می‌گشت و پیدا نمی‌کرد. الان به همون
                # چیزی اشاره می‌کنه که واقعاً هست و کدِ پیگیری رو نگه می‌داره.
                f"پس از تأیید و تکمیل، بهت اطلاع داده می‌شود. هر وقت خواستی از "
                f"«📦 سفارش‌های من» می‌تونی وضعیت و کدِ پیگیری‌شو دوباره ببینی.")
        await _answer_html_safe(msg, _manual_txt, reply_markup=_order_done_kb(order_pk))   # PATCH_SMM_ORDER_DONE_KB_V1
        async def _after_manual():
            # === PATCH_SMM_ORDER_CHANNEL_V1 ===
            # اول کانالِ سفارشاتِ دستی؛ اگر وصل نبود، همان دی‌امِ قبلی به مالک.
            from tenant.gateways.receipt_channel import post_smm_order
            posted = await post_smm_order(
                bot, row.id, manual=True, service_name=v.name, link=link,
                quantity=qty, price=price, user=u, order_id=trk,
                status="در انتظارِ انجام و تایید",
                note="از دکمه‌هایِ پایین همین پیام تأیید یا رد کن.",   # PATCH_ORDER_CODE_UNIFIED_V1
                pk=order_pk)
            # === PATCH_SMM_ADMIN_ALERT_TOGGLES_V1 === کلید: smm_alert_manual_order ("0" = خاموش)
            if not posted and (await _get_setting(row.id, "smm_alert_manual_order", "1")) != "0":
                try:
                    await bot.send_message(
                        row.owner_id,
                        f"🔔 سفارش جدید SMM (#{trk or order_pk})\n"
                        f"🛰 {v.name} × {qty:,}\n🔗 {link}\n💰 {price:,} تومان\n\n"
                        f"از «📋 سفارش‌های SMM» تأیید یا رد کن.")
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        _smm_bg(_after_manual())
    return {"status": _status, "order_pk": order_pk, "tracking": trk, "price": int(price)}


class _SmmConfirmMessage:
    def __init__(self, cb: CallbackQuery, quantity: int, bot: Bot):
        self.text = str(quantity)
        self.from_user = cb.from_user
        self._bot = bot
        self._chat_id = cb.message.chat.id

    async def answer(self, text, **kwargs):
        return await self._bot.send_message(self._chat_id, text, **kwargs)


@smm_router.callback_query(F.data == "smmu:qty_confirm")
async def smmu_qty_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    data = await state.get_data()
    qty = data.get("smmu_qty")
    if not qty:
        await cb.answer("تعداد سفارش پیدا نشد؛ دوباره وارد کن.", show_alert=True)
        return
    # === PATCH_SMM_CONFIRM_ANSWER_FIRST_V1 ===
    # گزارش (Omid): «ثبتِ سفارش خیلی دیر می‌آورد». پاسخ به کلیکِ دکمه (answerCallbackQuery) تا آخرِ ثبتِ سفارش
    # (شاملِ تماس با پنلِ بالادستی) عقب می‌افتاد؛ پس مشتری ثانیه‌ها چرخانکِ دکمه را می‌دید و فکر می‌کرد گیر کرده.
    # حالا همان لحظه جواب داده می‌شود.
    try:
        await cb.answer("⏳ در حال ثبت سفارش…")
    except Exception:
        logger.debug("پاسخِ زودهنگام به کلیکِ تأیید ممکن نشد (بی‌اهمیت)")
    await state.update_data(smmu_final_confirmed=True)
    await state.set_state(SmmFSM.user_qty)
    proxy = _SmmConfirmMessage(cb, int(qty), bot)
    await smmu_qty_in(proxy, bot, state)
    # === PATCH_SMM_ORDER_FLOW_CLEANUP_V1 ===
    # smmu_qty_in در دو پایانِ قطعی state را پاک می‌کند: سفارش ثبت شد، یا موجودی کم بود.
    # فقط در این دو حالت پیش‌نمایشِ قدیمی (با دکمه‌هایِ تأیید/لغو) منسوخ است و پاک می‌شود؛
    # اگر خطایِ «قابلِ تلاشِ دوباره» بود (state هنوز هست)، پیش‌نمایش برایِ ادامه می‌ماند.
    if await state.get_state() is None:
        await _smm_del(cb.message)


@smm_router.callback_query(F.data == "smmu:qty_change")
async def smmu_qty_change(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.update_data(smmu_final_confirmed=False)
    await state.set_state(SmmFSM.user_qty)
    await _EditFirstV(cb.message).answer(   # PATCH_VS_EDIT_IN_PLACE_V1
        "✏️ تعداد جدید را بفرست:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو سفارش", callback_data="smmu:order_cancel", style=ButtonStyle.DANGER)
        ]]))
    await cb.answer()


# === PATCH_SMM_ORDER_DISCOUNT_V1 ===
@smm_router.callback_query(F.data == "smmu:disc:add")
async def smmu_disc_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف غیرفعال است.", show_alert=True)
        return
    await state.set_state(SmmFSM.user_disc_code)
    await cb.message.answer("🏷 کد تخفیف را بفرست:\n(یا /cancel برای انصراف)")
    await cb.answer()


@smm_router.message(SmmFSM.user_disc_code, F.text)
async def smmu_disc_enter(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    vid, qty, price = data.get("smmu_svc_id"), data.get("smmu_qty"), data.get("smmu_price")
    async with Session() as s:
        v = await s.get(SmmService, vid) if vid else None
    if not v or not qty or not price:
        await state.clear()
        await msg.answer("اطلاعاتِ سفارش پیدا نشد؛ دوباره از انتخابِ سرویس شروع کن.")
        return
    raw = (msg.text or "").strip()
    if raw.lower() in ("/cancel", "❌ لغو", "❌ لغو / بازگشت", "لغو", "انصراف"):
        await state.set_state(SmmFSM.user_qty_confirm)
        text, kb = await _smm_qty_preview_text_kb(row.id, v, data.get("smmu_link", ""),
                                                  qty, price, data.get("smmu_discount"),
                                                  tg_id=msg.from_user.id)
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(
        bot_id=row.id, code=raw, telegram_id=msg.from_user.id, price=int(price), product_key="smm")   # PATCH_VS_DISCOUNT_V1
    if not res.ok:
        await msg.answer(f"❌ {res.reason or 'کد تخفیف نامعتبر است.'}\nدوباره بفرست یا /cancel بزن:")
        return
    disc = {"code": res.code, "code_id": res.code_id, "discount": res.discount,
            "final_price": res.final_price, "orig_price": int(price)}
    await state.update_data(smmu_discount=disc)
    await state.set_state(SmmFSM.user_qty_confirm)
    text, kb = await _smm_qty_preview_text_kb(row.id, v, data.get("smmu_link", ""), qty, price, disc,
                                              tg_id=msg.from_user.id)
    await msg.answer(f"✅ کد تخفیف اعمال شد.\n\n{text}", parse_mode="HTML", reply_markup=kb)


@smm_router.callback_query(F.data == "smmu:disc:del")
async def smmu_disc_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await state.update_data(smmu_discount=None)
    data = await state.get_data()
    vid, qty, price = data.get("smmu_svc_id"), data.get("smmu_qty"), data.get("smmu_price")
    async with Session() as s:
        v = await s.get(SmmService, vid) if vid else None
    if not v or not qty or not price:
        await cb.answer()
        return
    text, kb = await _smm_qty_preview_text_kb(row.id, v, data.get("smmu_link", ""), qty, price, None,
                                              tg_id=cb.from_user.id)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer("حذف شد")


# ═══════════════════════ Poller وضعیت سفارش‌ها ═══════════════════════

async def _apply_order_status(bot_id: int, order_pk: int, data: dict) -> None:
    """یک نتیجهٔ استعلامِ وضعیت (از تکی یا گروهی) را روی سفارش اعمال می‌کند.
    === PATCH_SMM_BULK_STATUS_V1 ===
    این منطق قبلاً مستقیم توی smm_status_poll_once بود؛ حالا یک تابعِ
    مشترکه چون هم مسیرِ تک‌تک‌استعلام‌گیری هم مسیرِ گروهی باید دقیقاً
    همین رفتار رو داشته باشن، بدونِ کپی‌شدنِ منطق تو دو جا."""
    # === PATCH_SMM_STATUS_ERROR_ROW_V1 === مستنداتِ استعلامِ گروهی: ردیفِ بدشناسه {"error": "Incorrect order ID"}
    # است؛ بدونِ این گارد، panel_status خالی و remaining صفر/None روی سفارشِ سالم نوشته می‌شد.
    if data.get("error") and not data.get("status"):
        return
    st = str(data.get("status", "") or "").lower()
    remains = data.get("remains")
    try:
        remains = int(remains) if remains is not None else None
    except (TypeError, ValueError):
        remains = None
    newly_completed = False
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
        if not o:
            return
        o.panel_status = st[:30]
        o.remaining = remains
        o.updated_at = datetime.utcnow()
        if st in ("completed", "complete"):
            if o.status != "completed":
                newly_completed = True
            o.status = "completed"
        elif st == "partial":
            # === PATCH_SMM_PARTIAL_V1 ===
            # پنل فقط بخشی را تحویل داد. remains را ذخیره کن، بعد بازگشتِ مابقیِ مبلغ + پیامِ مشتری.
            await s.commit()
            if remains is not None:
                await _apply_partial(bot_id, order_pk, remains, source="panel")
            else:
                logger.warning("[SMM] سفارش #%s: پنل Partial داد ولی remains نداد؛ دستی بررسی شود", order_pk)
            return
        elif st in ("canceled", "cancelled", "refunded", "fail", "failed", "error"):
            # === PATCH_SMM_PER_PANEL_SETTINGS_V1 ===
            policy = await _panel_fail_policy(bot_id, o.panel_pk)
            if policy == "manual_review":
                o.status = "failed"
                await s.commit()
                # === PATCH_SMM_MANUAL_REVIEW_NOTIFY_V1 ===
                await _notify_user(bot_id, order_pk, (await _smm_txt(bot_id, "manual_review")))
                return
            # پیش‌فرض (رفتارِ قدیمی): پنل سفارش را لغو کرد → بازگشت وجه
            await s.commit()
            # === PATCH_SMM_NOTIFY_GAPS_V1 ===
            await _refund_order(bot_id, order_pk, reason=f"لغو در پنل ({st})",
                                notify_text=(await _smm_txt(bot_id, "canceled")))
            return
        await s.commit()
    # === PATCH_SMM_NOTIFY_GAPS_V1 ===
    if newly_completed:
        await _notify_user(bot_id, order_pk, (await _smm_txt(bot_id, "completed")))


_VNUM_SERVICE_PREFIXES = ("ozv:", "sp:")


def _is_vnum_service_id(service_id) -> bool:
    return str(service_id or "").startswith(_VNUM_SERVICE_PREFIXES)


async def smm_status_poll_once():
    """یک‌بار همهٔ سفارش‌های sent را از پنل استعلام و در صورت تکمیل/خطا
    وضعیت را به‌روزرسانی می‌کند (و در خطا، طبقِ سیاستِ ربات، وجه را
    برمی‌گرداند یا failed می‌گذارد تا ادمین دستی تصمیم بگیرد).

    === PATCH_SMM_BULK_STATUS_V1 ===
    اگر پنلِ یک سفارش «استعلامِ گروهی» فعال داشته باشد، به‌جایِ یک
    درخواستِ HTTP به‌ازایِ هر سفارش، سفارش‌هایِ آن پنل را دسته‌هایِ ۵۰تایی
    می‌کند و هر دسته را با یک درخواست استعلام می‌گیرد."""
    async with Session() as s:
        orders = (await s.scalars(select(SmmOrder).where(
            SmmOrder.status == "sent").limit(300))).all()
        # === PATCH_SMM_POLL_SKIP_VNUM_V1 ===
        # سفارش‌هایِ شمارهٔ مجازیِ خودکار (OzVinoo «ozv:»، SMSPool «sp:») هم در جدولِ
        # SmmOrder با status=sent و panel_order_id ذخیره می‌شوند ولی panel_pk ندارند؛
        # قبلاً همین‌جا با پنلِ پیش‌فرضِ SMMِ ربات استعلام می‌شدند — یعنی شمارهٔ سفارشِ
        # OzVinoo از یک پنلِ بی‌ربط پرسیده می‌شد و جوابِ «سفارشِ دیگری» رویشان اعمال
        # می‌شد (completed/failed/لغو و بازگشتِ وجه). نتیجه: دکمهٔ «لغو و بازگشتِ وجه»ِ
        # مشتری می‌گفت «دیگر قابلِ لغو نیست». این سفارش‌ها پیگیرِ اختصاصیِ خودشان را دارند.
        rows = [(o.id, o.bot_id, o.panel_order_id, o.panel_pk) for o in orders
                if o.panel_order_id and not _is_vnum_service_id(o.service_id)]

    # گروه‌بندی بر اساسِ (bot_id, panel_pk) — چون اعتبارنامه و تنظیمِ bulk
    # مالِ همون پنل مشخصه، نه کلِ سفارش‌ها.
    groups: dict[tuple[int, int | None], list[tuple[int, str]]] = {}
    for order_pk, bot_id, panel_oid, panel_pk in rows:
        groups.setdefault((bot_id, panel_pk), []).append((order_pk, str(panel_oid)))

    for (bot_id, panel_pk), items in groups.items():
        api_url, api_key, _ = await _panel_creds(bot_id, panel_pk)
        if not api_url or not api_key:
            continue
        bulk_on = await _panel_bulk_enabled(bot_id, panel_pk)
        if not bulk_on:
            for order_pk, panel_oid in items:
                data = await smm_api.get_order_status(api_url, api_key, panel_oid)
                if data:
                    await _apply_order_status(bot_id, order_pk, data)
            continue
        # === PATCH_SMM_BULK_STATUS_V1 === دسته‌هایِ حداکثر ۵۰تایی
        for i in range(0, len(items), 50):
            chunk = items[i:i + 50]
            id_map = {panel_oid: order_pk for order_pk, panel_oid in chunk}
            result = await smm_api.get_orders_status_bulk(api_url, api_key, [pid for _, pid in chunk])
            if not result:
                # پنل جوابِ گروهی نداد (شاید موقتاً قطع بوده) — برایِ این
                # دسته با روشِ تک‌تک ادامه بده تا سفارش‌ها بی‌جواب نمونن.
                for order_pk, panel_oid in chunk:
                    data = await smm_api.get_order_status(api_url, api_key, panel_oid)
                    if data:
                        await _apply_order_status(bot_id, order_pk, data)
                continue
            for panel_oid_key, data in result.items():
                order_pk = id_map.get(str(panel_oid_key))
                if order_pk and isinstance(data, dict):
                    await _apply_order_status(bot_id, order_pk, data)

    # === PATCH_SMM_REFILL_V1 === پیگیریِ وضعیتِ ریفیل‌ها؛ خطایش هرگز پیگیریِ سفارش‌ها را نمی‌اندازد
    try:
        from tenant.bots.virtual_service import smm_refill
        await smm_refill.poll_once()
    except Exception:
        logger.exception("[refill] پیگیریِ ریفیل‌ها خطا داد")


# ═══════════ میان‌بر: نمایش مستقیم یک نوع سرویس (برای دکمه‌های vs_*) ═══════════

# نگاشت اکشن‌های قدیمیِ خدمات مجازی به (پلتفرم، نوعِ سرویس) در سیستم SMM
VS_ACTION_MAP = {
    "vs_member":   ("telegram", "member"),
    "vs_reaction": ("telegram", "reaction"),
    "vs_view":     ("telegram", "view"),
    "vs_like":     ("telegram", "like"),
    "vs_vote":     ("telegram", "vote"),
    "vs_comment":  ("telegram", "comment"),
    # === PATCH_VS_INSTAGRAM_ACTIONS_V1 ===
    # درخواستِ ادمینِ رباتِ خدماتِ مجازی: فالوور/ویو/لایکِ اینستاگرام هم
    # مثلِ سرویس‌هایِ تلگرامی دکمهٔ مستقیمِ خودشان را داشته باشند. نوعِ
    # "follower" از قبل در SERVICE_TYPESِ smm_api تعریف شده بود و فقط راهی
    # برایِ رسیدن به آن از منویِ دکمه‌ها وجود نداشت.
    "vs_ig_follower": ("instagram", "follower"),
    "vs_ig_view":     ("instagram", "view"),
    "vs_ig_like":     ("instagram", "like"),
}


async def show_smm_type_to_user(bot: Bot, msg_or_cb, bot_id: int, action: str, page: int = 1):
    """با زدن دکمه‌ای مثل «افزایش ممبر»، مستقیم سرویس‌های همان نوع را نشان می‌دهد.

    از سرویس‌های فعالِ ادمین استفاده می‌کند و آن‌ها را با تشخیص خودکار فیلتر
    می‌کند. اگر برای آن نوع سرویسی فعال نباشد، پیام مناسب می‌دهد.
    """
    target = msg_or_cb.message if isinstance(msg_or_cb, CallbackQuery) else msg_or_cb
    pkey, tkey = VS_ACTION_MAP.get(action, ("telegram", "other"))
    tree, _ = await _user_active_tree(bot_id)
    items = tree.get(pkey, {}).get(tkey, [])

    # اگر با تشخیصِ خودکار چیزی پیدا نشد، همهٔ سرویس‌های آن پلتفرم را نشان بده
    fallback = False
    if not items:
        merged = []
        for t, lst in tree.get(pkey, {}).items():
            merged.extend(lst)
        items = merged
        fallback = True

    if not items:
        await target.answer(
            "🛒 در حال حاضر سرویسی برای این بخش فعال نیست.\n"
            "لطفاً بعداً دوباره امتحان کن یا با پشتیبانی در تماس باش.")
        return

    async with Session() as s:
        _objs = {v.id: v for v in (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True))).all()}
    # === PATCH_SMM_SORT_EVERYWHERE_V1 ===
    _so = await _get_setting(bot_id, "smm_sort_order", "none")
    _pairs = [(it, _objs[it["pk"]]) for it in items if it["pk"] in _objs]
    _order = {v.id: i for i, v in enumerate(await _sort_svc_objs(bot_id, [v for _, v in _pairs], _so))}
    _pairs.sort(key=lambda p: _order[p[1].id])
    _pairs, page, _pages = _page_slice(_pairs, page)   # PATCH_SMM_LIST_PAGINATION_V1
    rows = []
    for it, _v in _pairs:
        rows.append([InlineKeyboardButton(
            text=it["name"][:55], callback_data=f"smmu:svc:{_v.id}")])
    _nav = _page_nav(f"smmu:vst:{action}", page, _pages)
    if _nav:
        rows.append(_nav)
    from tenant import inline_back_row
    br = await inline_back_row(bot_id)
    if br:
        rows.append(br)
    label = smm_api.service_type_label(tkey) if not fallback else smm_api.platform_label(pkey)
    _txt = f"{label}\n\nسرویس موردنظر را انتخاب کن:"
    _kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(msg_or_cb, CallbackQuery):   # ورق‌زدن: همان پیام عوض می‌شود
        try:
            await msg_or_cb.message.edit_text(_txt, reply_markup=_kb, parse_mode="HTML")
            return
        except Exception:
            logger.debug("[smm] ویرایشِ صفحهٔ سرویس‌ها ممکن نشد؛ پیامِ تازه", exc_info=True)
    await target.answer(_txt, reply_markup=_kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("smmu:vst:"))
async def smmu_vs_type_page(cb: CallbackQuery, bot: Bot):
    """PATCH_SMM_LIST_PAGINATION_V1 — صفحهٔ بعد/قبلِ فهرستِ دکمه‌هایی مثلِ «افزایشِ ممبر»."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    _, _, action, page = cb.data.split(":", 3)
    if action not in VS_ACTION_MAP or not page.isdigit():
        return await cb.answer()
    await cb.answer()
    await show_smm_type_to_user(bot, cb, row.id, action, int(page))


# ═══════════════════════════════════════════
#  تجربهٔ ورودِ حرفه‌ای — قوانین + خوش‌آمدگوییِ تایپ‌شونده
#  === PATCH_VS_ONBOARDING_V1 ===
# ═══════════════════════════════════════════
# فقط برایِ ربات‌هایِ «خدمات مجازی» (virtual_service) — کنارِ پنلِ SMM، نه
# جایگزینِ آن. سه بخشِ کلیدی:
#   ۱) قوانین: یک متن که می‌شود پینش کرد و کاربر باید قبل از استفاده از
#      ربات تاییدش کند (اختیاری — ادمین روشن/خاموشش می‌کند)
#   ۲) خوش‌آمدگوییِ تایپ‌شونده: بعدِ تاییدِ قوانین، یک پیام که کلمه‌به‌کلمه
#      روی صفحه «نوشته می‌شود» به‌جایِ این‌که یک‌جا ظاهر شود
#   ۳) قلاب‌شدن به tenant_start: قبل از هر چیزِ دیگر (حتی دیپ‌لینک‌ها) چک
#      می‌شود، و تا تایید نشود، ربات کارِ دیگری برایِ آن کاربر انجام نمی‌دهد

async def _vs_onboarding_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rules_required = (await _get_setting(bot_id, "vs_rules_required", "0")) == "1"
    rules_text = await _get_setting(bot_id, "vs_rules_text", "")
    welcome_text = await _get_setting(bot_id, "vs_welcome_text", "")
    typewriter_on = False   # === PATCH_VSO_NO_TYPEWRITER_V1 === پیامِ خوش‌آمد همیشه یک‌جا و سریع می‌رود

    lines = [
        "🎓 <b>راه‌اندازی حرفه‌ای ورود</b>\n",
        "در این بخش می‌توانید قوانین استفاده و پیام خوش‌آمد "
        "ربات را تنظیم کنید. پس از فعال‌سازی قوانین، کاربر باید "
        "پیش از استفاده آن‌ها را تأیید کند.\n",
        f"وضعیت تأیید قوانین: {'✅ اجباری' if rules_required else '❌ غیرفعال'}",
        f"متن قوانین: {'✅ تنظیم شده' if rules_text else '❌ هنوز تنظیم نشده'}",
        f"متن پیام خوش‌آمد: {'✅ تنظیم شده' if welcome_text else 'ℹ️ استفاده از متن پیش‌فرض ربات'}",
    ]
    rows = [
        [InlineKeyboardButton(
            text=("🔴 خاموش‌کردنِ اجباریِ قوانین" if rules_required else "🟢 اجباری‌کردنِ تاییدِ قوانین"),
            callback_data="vso:togrules",
            style=(ButtonStyle.DANGER if rules_required else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="✏️ ویرایشِ متنِ قوانین", callback_data="vso:setrules")],
        [InlineKeyboardButton(text="✏️ ویرایشِ متنِ خوش‌آمد", callback_data="vso:setwelcome")],
        [InlineKeyboardButton(text="🔍 پیش‌نمایشِ تجربهٔ ورود", callback_data="vso:preview")],
        # === PATCH_ONBOARDING_ALL_FIELDS_V1 ===
        # قبلاً همیشه به «smm:menu» برمی‌گشت چون این پنل فقط از دلِ پنلِ
        # SMM در دسترس بود. حالا که از «دکمه‌ها و تنظیمات» عمومی هم قابلِ
        # دسترسی است (برایِ هر نوع ربات، نه فقط خدماتِ مجازی)، مقصدِ
        # برگشت هم باید همان منویِ عمومی باشد که برایِ همه کار می‌کند.
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@vso_router.callback_query(F.data == "vso:menu")
async def vso_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await _vs_onboarding_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@vso_router.callback_query(F.data == "vso:togrules")
async def vso_toggle_rules(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "vs_rules_required", "0")) == "1"
    if not cur:
        rules_text = await _get_setting(row.id, "vs_rules_text", "")
        if not rules_text:
            await cb.answer("❌ اول باید متنِ قوانین را بنویسی.", show_alert=True)
            return
    await _set_setting(row.id, "vs_rules_required", "0" if cur else "1")
    text, kb = await _vs_onboarding_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")   # PATCH_VSO_SAFE_HTML_V1
    await cb.answer("✅ روشن شد" if not cur else "خاموش شد")


@vso_router.callback_query(F.data == "vso:togtype")
async def vso_toggle_typewriter(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "vs_typewriter_enabled", "1")) == "1"
    await _set_setting(row.id, "vs_typewriter_enabled", "0" if cur else "1")
    text, kb = await _vs_onboarding_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")   # PATCH_VSO_SAFE_HTML_V1
    await cb.answer("✅ روشن شد" if not cur else "خاموش شد")


@vso_router.callback_query(F.data == "vso:setrules")
async def vso_set_rules_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.rules_text)
    await cb.message.answer(
        "✏️ متنِ قوانینِ ربات را بفرست (HTML ساده مثلِ <b>پررنگ</b> مجاز است).\n\n"
        "کاربر باید قبل از استفاده از ربات تاییدش کند.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@vso_router.message(SmmFSM.rules_text, F.text)
async def vso_set_rules_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    await _set_setting(row.id, "vs_rules_text", msg.html_text or msg.text)
    text, kb = await _vs_onboarding_content(row.id)
    await msg.answer("✅ متنِ قوانین ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@vso_router.callback_query(F.data == "vso:setwelcome")
async def vso_set_welcome_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.welcome_text)
    await cb.message.answer(
        "✏️ متنِ خوش‌آمدگویی را بفرست.\n\n"
        "این متن بعدِ تاییدِ قوانین، فوراً برایِ کاربر فرستاده می‌شود. اگر خالی بگذاری، پیامِ خوش‌آمدِ معمولیِ ربات استفاده می‌شود.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@vso_router.message(SmmFSM.welcome_text, F.text)
async def vso_set_welcome_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    await _set_setting(row.id, "vs_welcome_text", msg.html_text or msg.text)
    text, kb = await _vs_onboarding_content(row.id)
    await msg.answer("✅ متنِ خوش‌آمد ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@vso_router.callback_query(F.data == "vso:preview")
async def vso_preview(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await cb.answer("در حالِ نمایشِ پیش‌نمایش…")
    rules_text = await _get_setting(row.id, "vs_rules_text", "")
    if rules_text:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✅ تایید قوانین", callback_data="vso:noop_preview")]])
        preview_msg = await cb.message.answer(
            f"📖 <b>پیش‌نمایشِ قوانین</b>\n\n{rules_text}", reply_markup=kb, parse_mode="HTML")
    welcome_text = await _get_setting(row.id, "vs_welcome_text", "") or "🎉 به ربات خوش اومدی!"
    typewriter_on = False   # === PATCH_VSO_NO_TYPEWRITER_V1 === پیامِ خوش‌آمد همیشه یک‌جا و سریع می‌رود
    await _play_typewriter(cb.message, welcome_text, typewriter_on)


@vso_router.callback_query(F.data == "vso:noop_preview")
async def vso_noop_preview(cb: CallbackQuery):
    await cb.answer("این فقط پیش‌نمایش است — مشتریِ واقعی همین دکمه را می‌بیند.", show_alert=True)


# ─────────────── دروازهٔ قوانین برایِ مشتری — از tenant_start صدا زده می‌شود ───────────────

async def _vs_show_rules_gate(msg: Message, bot: Bot, bot_id: int) -> bool:
    """اگر قوانین برایِ این ربات اجباری باشد، پیامِ قوانین را نشان (و پین)
    می‌کند و True برمی‌گرداند (یعنی tenant_start باید همین‌جا return کند).
    اگر اجباری نباشد، False برمی‌گرداند (ادامهٔ روالِ عادی)."""
    rules_required = (await _get_setting(bot_id, "vs_rules_required", "0")) == "1"
    if not rules_required:
        return False
    rules_text = await _get_setting(bot_id, "vs_rules_text", "")
    if not rules_text:
        return False
    # === PATCH_VSO_NO_MENU_NO_PIN_V1 ===
    # تا تاییدِ قوانین هیچ منویی نباید دیده شود: کیبوردِ پایینیِ قبلی را برمی‌داریم
    # (پیامِ کوتاهی که بلافاصله پاک می‌شود)، و قوانین را دیگر پین نمی‌کنیم.
    try:
        from tenant import clear_reply_kb_once
        await clear_reply_kb_once(msg, bot_id, msg.from_user.id)
    except Exception:
        logger.exception("[vso] برداشتنِ کیبوردِ قبلی ممکن نشد")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="✅ تایید قوانین", callback_data="vso:accept", style=ButtonStyle.SUCCESS)]])
    # === PATCH_RULES_VERSION_V1 === اگر قبلاً قوانینِ قبلی را تأیید کرده بود، یعنی متن عوض شده
    _upd_note = ""
    try:
        async with Session() as _s:
            from models import BotUser as _BU
            _bu = await _s.scalar(select(_BU).where(_BU.bot_id == bot_id, _BU.telegram_id == msg.from_user.id))
        if _bu is not None and _bu.rules_accepted:
            _upd_note = "🔄 <b>قوانین به‌روزرسانی شده است.</b> لطفاً نسخهٔ جدید را بخوان و دوباره تأیید کن.\n\n"
    except Exception:
        logger.exception("[vso] تشخیصِ «قوانینِ به‌روزشده» ممکن نشد")
    try:
        sent = await msg.answer(f"{_upd_note}📖 <b>قوانینِ استفاده از ربات</b>\n\n{rules_text}",
                                reply_markup=kb, parse_mode="HTML")
    except Exception:
        # === PATCH_VSO_SAFE_HTML_V1 === تگِ ناقصِ متنِ قوانین نباید /start را برایِ همه بشکند
        logger.warning("[vso] متنِ قوانین HTML معتبر نبود؛ ساده فرستاده شد")
        sent = await msg.answer(f"📖 قوانینِ استفاده از ربات\n\n{rules_text}", reply_markup=kb, parse_mode=None)
    return True


@vso_router.callback_query(F.data == "vso:accept")
async def vso_accept_rules(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import (get_bot_row, get_or_create_user, main_kb_or_remove,
                        HOME_MENU_TEXT_DEFAULT, send_home_menu)  # PATCH_CUSTOM_HOME_MENU_TEXT_HIDE_V2
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    u, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        from models import BotUser
        u2 = await s.get(BotUser, u.id)
        u2.rules_accepted = True
        u2.rules_accepted_at = datetime.now()
        from tenant import _rules_ver   # PATCH_RULES_VERSION_V1
        u2.rules_version = _rules_ver(await _get_setting(row.id, "vs_rules_text", ""))
        await s.commit()
    await cb.answer("✅ قوانین تایید شد")
    try:
        # === PATCH_UNPIN_CHAT_MESSAGE_KWARG_FIX_V1 ===
        # aiogram 3.29.1 ترتیبِ پارامترهای unpin_chat_message رو عوض کرده
        # (پارامترِ دوم الان business_connection_id هست، نه message_id)؛
        # با کیورد صدا می‌زنیم تا مستقل از ترتیب درست کار کنه.
        await bot.unpin_chat_message(cb.message.chat.id, message_id=cb.message.message_id)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگامِ آنپین‌کردنِ قوانین")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگامِ پاک‌کردنِ دکمهٔ «تایید قوانین»")

    welcome_text = await _get_setting(row.id, "vs_welcome_text", "")
    if not welcome_text:
        welcome_text = await _get_setting(row.id, "welcome_text", "") or HOME_MENU_TEXT_DEFAULT
    typewriter_on = False   # === PATCH_VSO_NO_TYPEWRITER_V1 === پیامِ خوش‌آمد همیشه یک‌جا و سریع می‌رود
    tw_msg = None
    try:
        tw_msg = await _play_typewriter(cb.message, welcome_text, typewriter_on)
    except Exception:
        logger.exception("[vso] جلوهٔ تایپ‌شونده خطا داد؛ ادامه با نمایشِ منو")

    # === PATCH_VSO_GLASS_MENU_FIX_V1 ===
    # باگِ گزارش‌شده: بعد از «✅ تایید قوانین» در رباتِ شیشه‌ای هیچ منویی نمی‌آمد (انگار ربات از کار
    # افتاده)، چون main_kb_or_remove در حالتِ شیشه‌ای فقط ReplyKeyboardRemove می‌دهد و
    # منویِ اینلاین هرگز ساخته نمی‌شد. حالا در حالتِ شیشه‌ای منویِ اینلاین رویِ پیامِ خوش‌آمد می‌نشیند.
    from tenant import main_inline_kb
    if (await _get_setting(row.id, "menu_mode", "reply")) == "inline":
        ikb = await main_inline_kb(row.id, row.owner_id, cb.from_user.id)
        if ikb:
            try:
                if tw_msg is None:
                    raise ValueError("no msg")
                await tw_msg.edit_reply_markup(reply_markup=ikb)
            except Exception:
                logger.info("[vso] سوارکردنِ منو رویِ پیامِ خوش‌آمد ممکن نشد؛ پیامِ جدا")
                await send_home_menu(cb.message, row.id, ikb)
        else:
            await send_home_menu(cb.message, row.id)
    else:
        kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
        await send_home_menu(cb, row.id, kb)
    await _resume_pending_svc(cb.message, bot, state)   # === PATCH_SMM_DEEPLINK_RESUME_V1 ===


# ─────────────── جلوهٔ تایپ‌شونده ───────────────

def _plain_for_typing(t: str) -> str:
    """فریم‌هایِ میانیِ تایپ بدونِ parse_mode می‌روند؛ تگ‌هایِ HTML را برمی‌دارد تا «<b>» خام دیده نشود."""
    import re as _re
    import html as _h
    t = _re.sub(r"<[^>]*$", "", t)            # تگِ نیمه‌بریده در انتها
    t = _re.sub(r"<[^>]+>", "", t)
    return _h.unescape(t)


async def _play_typewriter(target: Message, text: str, enabled: bool = True,
                           chunk: int = 14, delay: float = 0.4) -> Message:
    """متن را کلمه‌به‌کلمه/چندحرف‌چندحرف روی یک پیام «تایپ می‌کند» — یعنی
    پیام را مرتب edit می‌کند تا هربار چند کاراکترِ بیشتر نشان بدهد.

    اگر enabled=False باشد (ادمین خاموشش کرده)، فقط یک‌جا می‌فرستد — بدونِ
    هیچ‌گونه تاخیر یا ادیتِ اضافه، تا در تلگرام محدودیتِ نرخِ درخواست‌ها را
    رد نکنیم.
    """
    import asyncio as _asyncio
    if not enabled or len(text) <= chunk:
        # === PATCH_VSO_SAFE_HTML_V1 === متنِ دلخواهِ ادمین ممکن است تگِ ناقص داشته باشد
        try:
            return await target.answer(text, parse_mode="HTML")
        except Exception:
            logger.warning("[vso] متنِ خوش‌آمد HTML معتبر نبود؛ ساده فرستاده شد")
            return await target.answer(text, parse_mode=None)
    # === PATCH_VS_ONBOARDING_V1 ===
    # نکتهٔ مهم: چون parse_mode="HTML" است و متن ممکن است تگ داشته باشد
    # (مثلِ <b>...</b>)، نمی‌شود وسطِ یک تگ برش زد — تلگرام خطای پارس
    # می‌دهد. برایِ همین برشِ تدریجی را رویِ متنِ خام (بدونِ نگرانیِ تگ)
    # انجام می‌دهیم و در پایان، متنِ کاملِ HTML را می‌فرستیم؛ گام‌های میانی
    # را با parse_mode خاموش می‌فرستیم تا برشِ ناقصِ تگ خطا ندهد.
    msg = await target.answer("⌨️", parse_mode="HTML")
    shown = ""
    try:
        for i in range(chunk, len(text), chunk):
            shown = _plain_for_typing(text[:i]) + "▌"   # PATCH_VSO_SAFE_HTML_V1: بدونِ تگِ خام
            try:
                await msg.edit_text(shown)
            except Exception as e:
                # «message is not modified» یا نرخِ درخواست — انتظارِ می‌رود و
                # بی‌اهمیت است (این تابع ممکن است ده‌ها بار در یک انیمیشن
                # ادیت کند)، ولی برایِ دیباگِ آینده حداقل یک ردِ سبک بگذاریم.
                logger.debug("ادیتِ فریمِ جلوهٔ تایپ‌شونده رد شد (بی‌اهمیت): %s", e)
            await _asyncio.sleep(delay)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده در جلوهٔ تایپ‌شونده")
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده در نمایشِ نهاییِ خوش‌آمدگویی")
    return msg


# ═══════════════════════════════════════════
#  فروشِ گیفتِ رسمیِ تلگرام — PATCH_TELEGRAM_GIFTS_V1
# ═══════════════════════════════════════════
# نکتهٔ فنی: sendGift از موجودیِ استارزِ خودِ ربات هزینه می‌کند — همان
# استارزی که ربات از فاکتورهایِ XTR (مثلاً شارژِ کیف‌پول با استارز، در
# stars_admin.py) واقعاً دریافت کرده. اگر موجودیِ ربات کم بیاورد، ارسال
# شکست می‌خورد و مبلغ به کیف‌پولِ مشتری برمی‌گردد.

def _gift_price(star_count: int, rate: float, margin_pct: float) -> int:
    base = star_count * rate
    return max(1, round(base * (1 + margin_pct / 100)))


# === PATCH_MANUAL_GIFT_V1 ===
async def _gift_price_and_label(g: "TelegramGiftConfig", bot_id: int) -> tuple[int, str]:
    """(قیمتِ نهایی، برچسبِ کوتاهِ کالا) — هم برایِ گیفتِ کاتالوگی هم سفارشی؛
    یک‌جا نگهش می‌داریم تا فروشگاه/فاکتور/لیستِ ادمین همیشه هماهنگ بمانند."""
    if g.is_manual:
        price = g.manual_price_toman or 0
        label = f"🔗 {g.manual_name or 'گیفتِ سفارشی'}"
        return price, label
    rate, margin = await _gift_settings(bot_id)
    price = _gift_price(g.star_count, rate, margin)
    label = f"گیفتِ {g.emoji or '🎁'} ({g.star_count} استارز)"
    return price, label


# === PATCH_GIFT_QUANTITY_V1 ===
# مشتری قبل از انتخابِ مقصد، تعدادِ موردنظر را با دکمه‌های -/+ انتخاب
# می‌کند (دقیقاً مثلِ نمونه‌ای که رقبا استفاده می‌کنند) — فقط برایِ
# گیفتِ کاتالوگی/استارزی معنا دارد؛ گیفتِ سفارشی/NFT چون یک لینکِ
# منحصربه‌فرد است، همیشه تعدادش ۱ می‌ماند.
_GIFT_MAX_QTY = 20


def _gift_qty_kb(gift_id: str, qty: int) -> InlineKeyboardMarkup:
    qty = max(1, min(qty, _GIFT_MAX_QTY))
    dec = max(1, qty - 1)
    inc = min(_GIFT_MAX_QTY, qty + 1)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏬️", callback_data=f"gift:qty:{gift_id}:{dec}",
                                 style=ButtonStyle.DANGER),
            InlineKeyboardButton(text=str(qty), callback_data="noop"),
            InlineKeyboardButton(text="⏫️", callback_data=f"gift:qty:{gift_id}:{inc}",
                                 style=ButtonStyle.SUCCESS),
        ],
        [InlineKeyboardButton(text="◀️ بازگشت", callback_data="gift:back_shop",
                              style=ButtonStyle.DANGER),
         InlineKeyboardButton(text="ادامه ▶️", callback_data=f"gift:qcontinue:{gift_id}:{qty}",
                              style=ButtonStyle.SUCCESS)],
    ])


async def _gift_qty_text(g: "TelegramGiftConfig", bot_id: int, qty: int) -> str:
    price, item_label = await _gift_price_and_label(g, bot_id)
    return (f"{item_label}\n💰 قیمتِ هر واحد: {price:,} تومان\n\n"
           f"تعدادِ موردنظر را انتخاب کن:\n💵 جمع: {price * qty:,} تومان")


# === PATCH_BOT_STAR_BALANCE_FILTER_V1 ===
# مشکلِ گزارش‌شده: مشتری‌ها گیفت می‌خریدند، پول از کیف‌پولشان کم می‌شد،
# بعد ارسالِ واقعی با «BALANCE_TOO_LOW» شکست می‌خورد (چون خودِ ربات
# استارزِ کافی برایِ فرستادن نداشت) — و ادمین‌ها نمی‌دانستند این را
# چطور رفع کنند. راهِ درست این نیست که خطا را بهتر نشان بدهیم؛ راهِ
# درست این است که از اول گیفتی که ربات نمی‌تواند بفرستد، اصلاً به
# مشتری نشان داده نشود.
_star_balance_cache: dict[int, tuple[float, int]] = {}  # bot_id -> (زمان، موجودی)
_STAR_BALANCE_TTL = 60  # ثانیه — تعادلِ بینِ تازگیِ داده و تعدادِ فراخوانیِ API


async def _bot_star_balance(bot: "Bot") -> int:
    """موجودیِ واقعیِ استارزِ خودِ ربات — با کش برایِ جلوگیری از فراخوانیِ
    زیادِ API در هر بازِکردنِ فروشگاه."""
    import time as _time
    bid = id(bot)
    now = _time.time()
    cached = _star_balance_cache.get(bid)
    if cached and now - cached[0] < _STAR_BALANCE_TTL:
        return cached[1]
    try:
        result = await bot.get_my_star_balance()
        balance = int(result.amount)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگامِ خواندنِ موجودیِ استارزِ ربات")
        # اگر نتوانستیم بخوانیم، محافظه‌کارانه صفر فرض می‌کنیم — بهتر است
        # گیفت موقتاً نمایش داده نشود تا این‌که مشتری خرید کند و شکست بخورد.
        balance = 0
    _star_balance_cache[bid] = (now, balance)
    return balance


async def _gift_settings(bot_id: int) -> tuple[float, float]:
    rate_s = await _get_setting(bot_id, "gift_toman_rate", "1000")
    if (await _get_setting(bot_id, "gift_auto", "0")) == "1":
        try:
            from tenant.gateways.stars_rate import star_toman
            _a = await star_toman(0, bot_id)
            if _a > 0:
                rate_s = str(_a)
        except Exception:
            logger.exception("نرخِ خودکارِ گیفت")
    margin_s = await _get_setting(bot_id, "gift_margin_pct", "20")
    try:
        rate = float(rate_s)
    except Exception:
        rate = 1000.0
    try:
        margin = float(margin_s)
    except Exception:
        margin = 20.0
    return rate, margin


async def gift_admin_content(bot_id: int, bot: "Bot | None" = None) -> tuple[str, InlineKeyboardMarkup]:
    rate, margin = await _gift_settings(bot_id)
    _gauto = (await _get_setting(bot_id, "gift_auto", "0")) == "1"
    _gchoice = (await _get_setting(bot_id, "gift_user_choice", "0")) == "1"   # PATCH_GIFT_USER_CHOICE_V1
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id))
        active = await s.scalar(select(func.count()).select_from(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id, TelegramGiftConfig.active == True))  # noqa: E712
    # === PATCH_BOT_STAR_BALANCE_FILTER_V1 ===
    # قبلاً ادمین فقط بعدِ شکایتِ مشتری می‌فهمید موجودیِ استارزِ ربات کم
    # است. حالا همین‌جا، همیشه، موجودیِ واقعی نشان داده می‌شود.
    balance_line = ""
    if bot is not None:
        bal = await _bot_star_balance(bot)
        balance_line = f"⭐ موجودیِ فعلیِ استارزِ ربات: <b>{bal:,}</b>\n"
    # === PATCH_GLOBAL_MANUAL_DELIVERY_V1 ===
    # قبلاً فقط گیفتِ سفارشی/لینک‌دار (NFT) به‌صورتِ دستی تحویل داده
    # می‌شد؛ گیفتِ رسمی/استارزی همیشه با sendGiftِ خودکار می‌رفت. این
    # سویچِ کلی، همهٔ گیفت‌ها (حتی رسمی‌هایِ استارزی) را به همان مسیرِ
    # دستی می‌فرستد: مشتری می‌خرد، سفارش برایِ ادمین ثبت می‌شود، ادمین
    # با اکانتِ شخصیِ خودش می‌فرستد، بعد در پنل تاییدش می‌کند و همان‌جا
    # به مشتری اطلاع داده می‌شود — بدونِ نیازِ به موجودیِ استارزِ ربات.
    manual_mode = await _get_setting(bot_id, "gift_manual_delivery", "0") == "1"
    manual_toggle_text = ("🟢 حالتِ تحویلِ دستیِ همه: روشن" if manual_mode
                          else "⚪️ حالتِ تحویلِ دستیِ همه: خاموش")
    manual_mode_line = ""
    if manual_mode:
        manual_mode_line = (
            "\n\n🖐 <b>حالتِ تحویلِ دستی روشن است</b> — همهٔ گیفت‌ها (حتی رسمی/استارزی) "
            "بعدِ خرید مستقیم می‌روند برایِ تو؛ خودت با اکانتِ شخصی‌ات می‌فرستی و در "
            "«🧾 سفارش‌های گیفت» تاییدش می‌کنی. دیگر نیازی به موجودیِ استارزِ ربات نیست.")
    # === PATCH_GIFT_COLORED_BUTTONS_V1 ===
    # درخواستِ ادمین: دکمه‌هایِ فروشگاهِ گیفت (که مشتری می‌بیند) پیش‌فرض
    # بدونِ رنگ‌اند. این سویچ، آن‌ها را با یک استایلِ رنگیِ (سبز) یک‌دست
    # نشان می‌دهد — دقیقاً مثلِ نمونه‌هایِ رقبا.
    colored_gift_buttons = await _get_setting(bot_id, "gift_colored_buttons", "0") == "1"
    colored_toggle_text = ("🟢 دکمه‌هایِ رنگی: روشن" if colored_gift_buttons
                           else "⚪️ دکمه‌هایِ رنگی: خاموش")
    lines = [
        "🎁 <b>فروشِ گیفتِ رسمیِ تلگرام</b>\n",
        "کاتالوگ مستقیماً از خودِ تلگرام خوانده می‌شود — نیازی به اتصالِ "
        "پنلِ جداگانه نیست. فقط قیمت‌گذاری و فعال‌کردنِ هرکدام دستِ توست.\n",
        f"تعدادِ گیفتِ دریافت‌شده: <b>{total or 0}</b>",
        f"فعال برایِ فروش: <b>{active or 0}</b>",
        f"نرخِ پایه: هر ⭐ = <b>{rate:,.0f} تومان</b>  •  سود: <b>{margin:.0f}٪</b>\n"
        f"💵 <b>قیمتِ نهاییِ فروش (با سود): هر ⭐ = {rate * (1 + margin / 100):,.0f} تومان</b>\n",
        balance_line +
        "⚠️ برایِ ارسالِ واقعیِ گیفت، خودِ ربات باید موجودیِ استارزِ کافی "
        "داشته باشد — همان استارزی که از فاکتورهایِ استارزی (مثلاً شارژِ "
        "کیف‌پول با استارز) وارد حسابِ ربات می‌شود. حالا فروشگاهِ مشتری هم "
        "خودکار فقط گیفت‌هایی را نشان می‌دهد که با همین موجودی قابلِ‌ارسال‌اند.\n\n"
        "💰 برایِ افزایشِ موجودی، دکمهٔ «افزایشِ موجودیِ استارزِ ربات» را بزن "
        "و با استارزِ خودت پرداخت کن — همان استارز مستقیم وارد حسابِ ربات "
        "می‌شود. اگر آن دکمه پیامِ «هنوز فعال نشده» داد، اول از "
        "💰 مالی‌وکیف‌پول → ⭐ پرداخت با استارزِ تلگرام، این قابلیت را روشن کن."
        + manual_mode_line,
    ]
    rows = [
        [InlineKeyboardButton(text="🔄 به‌روزرسانیِ فهرست از تلگرام", callback_data="gift:refresh")],
        [InlineKeyboardButton(text="🗂 مدیریتِ گیفت‌ها (فعال/غیرفعال)", callback_data="gift:list:0")],
        # === PATCH_GLOBAL_MANUAL_DELIVERY_V1 ===
        [InlineKeyboardButton(text=manual_toggle_text, callback_data="gift:manualdelivery:toggle",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_GIFT_COLORED_BUTTONS_V1 ===
        [InlineKeyboardButton(text=colored_toggle_text, callback_data="gift:coloredbuttons:toggle",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_MANUAL_GIFT_V1 ===
        [InlineKeyboardButton(text="➕ افزودنِ گیفتِ سفارشی (با لینک)", callback_data="gift:manualadd",
                              style=ButtonStyle.SUCCESS)],
        # === PATCH_BOT_STAR_CHARGE_BUTTON_V1 ===
        # تنها راهِ واقعیِ افزایشِ موجودیِ استارزِ خودِ ربات، پرداختِ
        # واقعیِ استارز است — همان چیزی که «شارژِ کیف‌پول با استارز» از
        # قبل انجام می‌دهد (مشتری/ادمین استارز می‌دهد، استارزِ واقعی وارد
        # حسابِ ربات می‌شود، در ازایش کیف‌پولِ تومانی شارژ می‌شود). به‌جایِ
        # ساختنِ یک راهِ تازه، همان دکمه را مستقیم اینجا هم می‌گذاریم.
        [InlineKeyboardButton(text="💰 افزایشِ موجودیِ استارزِ ربات", callback_data="w:botstars",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(
            text=("🙋 انتخابِ روشِ ارسال توسطِ مشتری: روشن ✅" if _gchoice else "🙋 انتخابِ روشِ ارسال توسطِ مشتری: خاموش"),
            callback_data="gift:userchoice:toggle",
            style=(ButtonStyle.SUCCESS if _gchoice else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=f"💱 نرخِ پایه (هر ⭐ = {rate:,.0f} تومان)", callback_data="gift:setrate")],
        [InlineKeyboardButton(
            text=("🔄 نرخ خودکار: روشن ✅" if _gauto else "🔄 نرخ خودکار: خاموش"),
            callback_data="sauto:gift",
            style=(ButtonStyle.SUCCESS if _gauto else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({margin:.0f}٪)", callback_data="gift:setmargin")],
        [InlineKeyboardButton(text="⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار", callback_data="sauto:base")],
        _sfirst_button_row((await _get_setting(bot_id, "support_first_on", "0")) == "1", "gift"),
        [InlineKeyboardButton(text="✏️ متنِ پیامِ «اول به پشتیبانی پیام بده»", callback_data="sfirst:text:gift")],
        [InlineKeyboardButton(text="🧾 سفارش‌هایِ گیفت", callback_data="gift:orders:0")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "gift:menu")
async def gift_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await gift_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "gift:refresh")
async def gift_refresh(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await cb.answer("در حالِ دریافتِ فهرست از تلگرام…")
    try:
        result = await bot.get_available_gifts()
    except Exception as e:
        await cb.message.answer(f"❌ دریافتِ فهرستِ گیفت‌ها از تلگرام ناموفق بود:\n{e}")
        return
    n_new = 0
    async with Session() as s:
        existing = {g.gift_id: g for g in (await s.scalars(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id))).all()}
        for g in result.gifts:
            emoji = getattr(getattr(g, "sticker", None), "emoji", None)
            if g.id in existing:
                existing[g.id].star_count = g.star_count
                existing[g.id].emoji = emoji
            else:
                s.add(TelegramGiftConfig(bot_id=row.id, gift_id=g.id, star_count=g.star_count,
                                         emoji=emoji, active=False))
                n_new += 1
        await s.commit()
    text, kb = await gift_admin_content(row.id, bot)
    await cb.message.answer(f"✅ فهرست به‌روز شد — {len(result.gifts)} گیفت خوانده شد "
                            f"({n_new} موردِ تازه، غیرفعال).")
    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _gift_list_render(cb: CallbackQuery, bot_id: int, page: int) -> None:
    """رندرِ صفحهٔ مدیریتِ گیفت‌ها — بدونِ خواندن از cb.data، تا هم از
    خودِ هندلر (gift_list) هم از رفرش‌کردن‌هایِ دیگر (gift_toggle) با
    شمارهٔ صفحهٔ مشخص صدا زده شود."""
    per_page = 8
    async with Session() as s:
        all_gifts = (await s.scalars(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id).order_by(TelegramGiftConfig.star_count))).all()
    if not all_gifts:
        await cb.answer("هنوز فهرستی دریافت نشده — اول «به‌روزرسانی از تلگرام» را بزن.", show_alert=True)
        return
    start, end = page * per_page, page * per_page + per_page
    rows = []
    for g in all_gifts[start:end]:
        mark = "✅" if g.active else "▫️"
        price, item_label = await _gift_price_and_label(g, bot_id)
        rows.append([InlineKeyboardButton(text=f"{mark} {item_label} — {price:,} تومان",
                                          callback_data=f"gift:tog:{g.gift_id}:{page}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"gift:list:{page-1}"))
    if end < len(all_gifts):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"gift:list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:menu", style=ButtonStyle.DANGER)])
    text = (f"🗂 <b>مدیریتِ گیفت‌ها</b> ({start+1}-{min(end,len(all_gifts))} از {len(all_gifts)})\n\n"
           "روی هرکدام بزن تا فعال/غیرفعالش کنی:")
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("gift:list:"))
async def gift_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    await _gift_list_render(cb, row.id, page)


@smm_router.callback_query(F.data.startswith("gift:tog:"))
async def gift_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, gift_id, page = cb.data.split(":")
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id))
        if not g:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        g.active = not g.active
        await s.commit()
    # === PATCH_CB_DATA_FROZEN_FIX_V1 ===
    # باگِ واقعیِ تولید: cb روی سرورِ واقعی یک آبجکتِ pydanticِ frozen است؛
    # نوشتنِ مستقیم رویِ cb.data (که قبلاً اینجا بود) با
    # "ValidationError: Instance is frozen" کرش می‌کرد. تستِ محلی این را
    # نگرفت چون آبجکتِ ساختگیِ تست اجازهٔ تغییرِ آزادِ اتریبیوت می‌داد —
    # رفتاری که با نمونهٔ واقعیِ aiogram فرق داشت. حالا به‌جایِ تغییرِ
    # cb.data، مستقیم تابعِ رندرِ صفحه با شمارهٔ صفحهٔ درست صدا زده می‌شود.
    await _gift_list_render(cb, row.id, int(page))


# ─────────────── گیفتِ سفارشی/لینک‌دار — طرفِ ادمین — PATCH_MANUAL_GIFT_V1 ───────────────

# ─────────────── حالتِ تحویلِ دستیِ همگانی — PATCH_GLOBAL_MANUAL_DELIVERY_V1 ───────────────

@smm_router.callback_query(F.data == "gift:manualdelivery:toggle")
async def gift_manual_delivery_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _get_setting(row.id, "gift_manual_delivery", "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting(row.id, "gift_manual_delivery", new_val)
    await cb.answer("✅ حالتِ تحویلِ دستی روشن شد" if new_val == "1" else "✅ حالتِ تحویلِ دستی خاموش شد")
    text, kb = await gift_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_GIFT_USER_CHOICE_V1 ===
@smm_router.callback_query(F.data == "gift:userchoice:toggle")
async def gift_user_choice_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _get_setting(row.id, "gift_user_choice", "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting(row.id, "gift_user_choice", new_val)
    await cb.answer("✅ مشتری حالا روشِ ارسال را خودش انتخاب می‌کند (ربات یا اکانتِ پشتیبانی)"
                    if new_val == "1" else "✅ انتخابِ مشتری خاموش شد", show_alert=(new_val == "1"))
    text, kb = await gift_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_GIFT_COLORED_BUTTONS_V1 ===
@smm_router.callback_query(F.data == "gift:coloredbuttons:toggle")
async def gift_colored_buttons_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _get_setting(row.id, "gift_colored_buttons", "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting(row.id, "gift_colored_buttons", new_val)
    await cb.answer("✅ دکمه‌هایِ رنگی روشن شد" if new_val == "1" else "✅ دکمه‌هایِ رنگی خاموش شد")
    text, kb = await gift_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "gift:manualadd")
async def gift_manual_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.manual_gift_link)
    await cb.message.answer(
        "🔗 <b>افزودنِ گیفتِ سفارشی</b>\n\n"
        "این برایِ گیفت‌هایی است که در کاتالوگِ عادیِ تلگرام نیستند (مثلاً "
        "گیفتِ منحصربه‌فرد/NFT) — چون ربات نمی‌تواند این‌ها را خودکار "
        "بفرستد، تو خودت بعدِ فروش، دستی برایِ خریدار می‌فرستی.\n\n"
        "اول لینکِ گیفت را بفرست (همان چیزی که مشتری قبل از خرید می‌بیند):",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.manual_gift_link, F.text)
async def gift_manual_link_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.update_data(manual_gift_link=msg.text.strip())
    await state.set_state(SmmFSM.manual_gift_name)
    from tenant import smart_cancel_kb
    await msg.answer("✏️ اسمِ این گیفت را بفرست (چیزی که مشتری می‌بیند):",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.manual_gift_name, F.text)
async def gift_manual_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.update_data(manual_gift_name=msg.text.strip())
    await state.set_state(SmmFSM.manual_gift_price)
    from tenant import smart_cancel_kb
    await msg.answer("💵 قیمتِ این گیفت چند تومان است؟ (فقط عدد)",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.manual_gift_price, F.text)
async def gift_manual_price_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    price = fa_to_int(msg.text)
    if not price or price <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    link = data.get("manual_gift_link", "")
    name = data.get("manual_gift_name", "گیفتِ سفارشی")
    import time as _t
    gid = f"manual{int(_t.time()*1000)}"
    async with Session() as s:
        s.add(TelegramGiftConfig(
            bot_id=row.id, gift_id=gid, star_count=0, emoji="🔗",
            active=True, is_manual=True, manual_link=link,
            manual_name=name, manual_price_toman=price))
        await s.commit()
    await msg.answer(f"✅ گیفتِ سفارشیِ «{name}» با قیمتِ {price:,} تومان اضافه شد و فعال است.")
    text, kb = await gift_admin_content(row.id, bot)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "gift:setrate")
async def gift_setrate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.gift_rate)
    await cb.message.answer("💱 هر یک استارز، چند تومان حساب شود؟ (فقط عدد، مثلاً 1000)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.gift_rate, F.text)
async def gift_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "gift_toman_rate", str(val))
    text, kb = await gift_admin_content(row.id, bot)
    await msg.answer("✅ نرخ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "gift:setmargin")
async def gift_setmargin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.gift_margin)
    await cb.message.answer("📈 چند درصد سود روی قیمتِ استارزی اضافه شود؟ (فقط عدد، مثلاً 20)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.gift_margin, F.text)
async def gift_setmargin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "gift_margin_pct", str(val))
    text, kb = await gift_admin_content(row.id, bot)
    await msg.answer("✅ درصدِ سود ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("gift:orders:"))
async def gift_orders(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    per_page = 10
    async with Session() as s:
        orders = (await s.scalars(select(GiftOrder).where(
            GiftOrder.bot_id == row.id).order_by(GiftOrder.id.desc())
            .offset(page * per_page).limit(per_page))).all()
        # === PATCH_GIFT_WAITING_STARS_LIST_V1 ===
        # گزارشِ فدایی: وقتی استارزِ ربات کم است پیامِ ادمین می‌آید، با «افزایشِ موجودی» پیام می‌رود و سفارش
        # دیگر جایی برایِ «ارسال دوباره/لغو» ندارد. حالا همیشه بالایِ همین لیست، با دکمه‌هایِ ارسال و لغو.
        waiting = (await s.scalars(select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.status == "awaiting_stars")
            .order_by(GiftOrder.id).limit(8))).all()
    async with Session() as s_pa:        # === PATCH_PENDING_LISTS_FIX_V1 === سفارش‌هایِ منتظرِ تأییدِ ادمین (هر نوع) با دکمهٔ تأیید/رد
        pending_admin = (await s_pa.scalars(select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.status == "awaiting_admin")
            .order_by(GiftOrder.id).limit(8))).all()
    lines = ["🧾 <b>سفارش‌هایِ گیفت</b>\n"]
    if pending_admin:
        lines.append(f"⏳ <b>در انتظارِ تأیید شما: {len(pending_admin)} سفارش</b>\n"
                     + "\n".join(f"• {_gift_item_label(o)} → {_html_escape(str(o.dest_label or ''))} — "
                                 f"{float(o.price_toman or 0):,.0f} تومان — 🔖 {o.tracking_code or '#' + str(o.id)}"
                                 for o in pending_admin) + "\n")
    if waiting:
        lines.append(f"⏳ <b>منتظرِ موجودیِ استارزِ ربات: {len(waiting)} سفارش</b>\n"
                     "موجودیِ ربات را شارژ کن و برایِ هر سفارش «🔄 ارسال» را بزن؛ یا «❌ لغو» تا وجهِ مشتری برگردد.\n")
    status_label = {"pending": "⏳ در انتظار", "sent": "✅ ارسال شد",
                    "awaiting_stars": "⏳ منتظرِ موجودیِ استارز", "auto_processing": "🤖 در حالِ خریدِ خودکار",
                    "failed": "❌ ناموفق", "refunded": "↩️ بازگشتِ وجه",
                    # === PATCH_GIFT_CARD_PAYMENT_V1 / PATCH_MANUAL_GIFT_V1 ===
                    "awaiting_receipt": "🧾 در انتظارِ رسید", "awaiting_admin": "⏳ در انتظارِ تایید",
                    "rejected": "❌ ردشده"}
    if not orders:
        lines.append("هنوز سفارشی ثبت نشده.")
    for o in orders:
        # === PATCH_MANUAL_GIFT_V1 / PATCH_TON_SELL_V1 ===
        item = _gift_item_label(o)       # PATCH_PENDING_LISTS_FIX_V1
        # === PATCH_GIFT_QUANTITY_V1 ===
        qty_suffix = f" ×{o.quantity}" if (o.quantity or 1) > 1 else ""
        lines.append(f"🔖 {o.tracking_code or '#' + str(o.id)} — {item}{qty_suffix} → {_html_escape(str(o.dest_label or ''))} "
                    f"— {status_label.get(o.status, o.status)}")
    rows = []
    for _po in pending_admin:     # PATCH_PENDING_LISTS_FIX_V1
        _pc = _po.tracking_code or f"#{_po.id}"
        rows.append([InlineKeyboardButton(text=f"✅ تأیید {_pc}", callback_data=f"gift:lp:ok:{_po.id}:{page}",
                                          style=ButtonStyle.SUCCESS),
                     InlineKeyboardButton(text=f"❌ رد {_pc}", callback_data=f"gift:lp:no:{_po.id}:{page}",
                                          style=ButtonStyle.DANGER)])
    for _w in waiting:   # PATCH_GIFT_WAITING_STARS_LIST_V1
        _wc = _w.tracking_code or f"#{_w.id}"
        rows.append([InlineKeyboardButton(text=f"🔄 ارسال {_wc}", callback_data=f"gift:lr:{_w.id}:{page}",
                                          style=ButtonStyle.PRIMARY),
                     InlineKeyboardButton(text=f"❌ لغو {_wc}", callback_data=f"gift:lc:{_w.id}:{page}",
                                          style=ButtonStyle.DANGER)])
    if waiting:
        rows.append([InlineKeyboardButton(text="💰 افزایشِ موجودیِ استارزِ ربات", callback_data="w:botstars",
                                          style=ButtonStyle.SUCCESS)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"gift:orders:{page-1}"))
    if len(orders) == per_page:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"gift:orders:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:menu", style=ButtonStyle.DANGER)])
    text = "\n".join(lines)
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


async def _gift_list_action(cb: CallbackQuery, bot: Bot, action_cb: str, handler) -> None:
    """اجرایِ «ارسال/لغو» از داخلِ لیستِ سفارش‌ها و تازه‌کردنِ لیست؛ از همان هندلرهایِ موجود استفاده می‌شود (منطقِ پول دست‌نخورده)."""
    _, _, oid, page = cb.data.split(":")
    # CallbackQuery در aiogram غیرقابلِ‌تغییر (frozen) است؛ نسخهٔ کپی‌شده با data تازه می‌سازیم
    await handler(cb.model_copy(update={"data": f"{action_cb}:{oid}"}), bot)
    async with Session() as s:
        o = await s.get(GiftOrder, int(oid))
        changed = bool(o) and o.status != "awaiting_stars"
    if changed:       # سفارش از «منتظرِ موجودی» خارج شد ⇒ لیست را تازه کن
        try:
            await gift_orders(cb.model_copy(update={"data": f"gift:orders:{page}"}), bot)
        except Exception:
            logger.info("تازه‌کردنِ لیستِ سفارش‌هایِ گیفت پس از اقدام ممکن نشد", exc_info=True)


def _gift_item_label(o) -> str:
    """PATCH_PENDING_LISTS_FIX_V1 — برچسبِ کالایِ هر نوعِ سفارشِ گیفت/استارز/تون/ری‌اکشن/پریمیوم (قبلاً همه «🎁 N⭐» بود)."""
    gid = (o.gift_id or "")
    n = int(o.star_count or 0)
    if gid.startswith("manual"):
        return "🔗 گیفتِ سفارشی"
    if gid.startswith("tonsell_"):
        return f"💎 {n / 100:g} TON"
    if gid.startswith("starsell_"):
        return f"⭐ {n:,} استارز"
    if gid.startswith("srx_"):
        return f"⭐️ ری‌اکشنِ استارزی ×{n:,}"
    if gid.startswith("premium_"):
        return "👑 پریمیومِ تلگرام"
    return f"{o.gift_emoji or '🎁'} {n}⭐"


# تأیید/ردِ سفارشِ منتظرِ تأیید از داخلِ لیستِ سفارش‌ها؛ هر نوع به هندلرِ خودش وصل است و بعد لیست تازه می‌شود.
_LP_ROUTES = {
    ("starsell_", "ok"): ("star:done", "star_admin_done"), ("starsell_", "no"): ("star:reject", "star_admin_reject"),
    ("tonsell_", "ok"): ("ton:done", "ton_order_done"), ("tonsell_", "no"): ("ton:refund", "ton_order_refund"),
    ("srx_", "ok"): ("srx:ok", "srx_admin_done"), ("srx_", "no"): ("srx:no", "srx_admin_reject"),
    ("", "ok"): ("gift:cardok", "gift_cardpay_approve"), ("", "no"): ("gift:cardno", "gift_cardpay_reject"),
}


@smm_router.callback_query(F.data.startswith("gift:lp:"))
async def gift_list_pending_action(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    try:
        _, _, act, oid_s, page_s = cb.data.split(":")
        oid, page = int(oid_s), int(page_s)
    except ValueError:
        return await cb.answer()
    async with Session() as s:
        o = await s.get(GiftOrder, oid)
    if not o or o.bot_id != row.id:
        return await cb.answer("سفارش پیدا نشد.", show_alert=True)
    gid = o.gift_id or ""
    key = next((p for p in ("starsell_", "tonsell_", "srx_") if gid.startswith(p)), "")
    cb_prefix, fn_name = _LP_ROUTES[(key, act)]
    await globals()[fn_name](cb.model_copy(update={"data": f"{cb_prefix}:{oid}"}), bot)
    try:
        await gift_orders(cb.model_copy(update={"data": f"gift:orders:{page}"}), bot)
    except Exception:
        logger.info("تازه‌کردنِ لیستِ سفارش‌هایِ گیفت پس از تأیید/ردِ سفارش ممکن نشد", exc_info=True)


@smm_router.callback_query(F.data.startswith("gift:lr:"))
async def gift_list_retry(cb: CallbackQuery, bot: Bot):
    await _gift_list_action(cb, bot, "gift:retry", gift_retry_send)


@smm_router.callback_query(F.data.startswith("gift:lc:"))
async def gift_list_cancel(cb: CallbackQuery, bot: Bot):
    await _gift_list_action(cb, bot, "gift:cardno", gift_cardpay_reject)


# ─────────────── فروشگاهِ گیفت — طرفِ مشتری ───────────────

async def show_gift_shop_to_user(bot: Bot, target: Message, bot_id: int) -> None:
    async with Session() as s:
        gifts = (await s.scalars(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id, TelegramGiftConfig.active == True  # noqa: E712
        ).order_by(TelegramGiftConfig.star_count))).all()
    if not gifts:
        await target.answer("🎁 فعلاً هیچ گیفتی برایِ فروش فعال نیست.")
        return
    # === PATCH_BOT_STAR_BALANCE_FILTER_V1 ===
    # گیفتِ سفارشی (is_manual) از موجودیِ استارزِ ربات استفاده نمی‌کند
    # (تحویلش دستی است)، پس همیشه نشان داده می‌شود. گیفتِ کاتالوگیِ
    # واقعی را فقط اگر موجودیِ فعلیِ ربات کافی باشد نشان می‌دهیم — تا
    # مشتری هرگز پولی ندهد که بعداً به‌خاطرِ کمبودِ استارزِ ربات برگردد.
    #
    # === PATCH_GLOBAL_MANUAL_DELIVERY_BALANCE_FIX_V1 ===
    # باگ: وقتی «حالتِ تحویلِ دستیِ همگانی» روشن است، دیگر هیچ گیفتی از
    # طریقِ APIِ ربات فرستاده نمی‌شود — پس موجودیِ استارزِ ربات کاملاً
    # بی‌ربط است. ولی این فیلتر همچنان طبقِ موجودی فیلتر می‌کرد، یعنی
    # درست همان ادمین‌هایی که به‌خاطرِ کمِ‌بودنِ استارز حالتِ دستی را روشن
    # کرده بودند، می‌دیدند فروشگاه هنوز خالی است — دقیقاً همان مشکلی که
    # حالتِ دستی قرار بود حلش کند! حالا وقتی حالتِ دستی روشن است، این
    # فیلتر کاملاً نادیده گرفته می‌شود.
    manual_mode = await _get_setting(bot_id, "gift_manual_delivery", "0") == "1"
    # === PATCH_GIFT_USER_CHOICE_V1 === با «انتخابِ مشتری» همهٔ گیفت‌ها نشان داده می‌شوند
    # (اکانتِ پشتیبانی به موجودیِ ربات نیاز ندارد؛ «ربات» هم در کمبود منتظرِ شارژ می‌ماند).
    _choice_on = (await _get_setting(bot_id, "gift_user_choice", "0")) == "1"
    balance = await _bot_star_balance(bot) if not (manual_mode or _choice_on) else float("inf")
    # === PATCH_MANUAL_GIFT_SLIDER_V1 ===
    # قبلاً گیفتِ سفارشی/NFT هم مثلِ بقیه یک ردیفِ ساده در لیست بود —
    # درحالی‌که این‌جور گیفت‌ها (منحصربه‌فرد، معمولاً چندتا نه صدتا) با
    # یک اسلایدر که خودِ پیش‌نمایشِ لینک (عکس/اسمِ واقعیِ NFT) را هم نشان
    # می‌دهد، خیلی جذاب‌تر دیده می‌شوند — دقیقاً همان الگویی که رقبا هم
    # استفاده می‌کنند. حالا گیفتِ کاتالوگی (استارزی) هنوز ردیفی است، ولی
    # گیفتِ سفارشی یک دکمهٔ ورودی به اسلایدر می‌شود.
    catalog_gifts = [g for g in gifts if not g.is_manual]
    manual_gifts = [g for g in gifts if g.is_manual]
    # === PATCH_GIFT_COLORED_BUTTONS_V1 ===
    colored = await _get_setting(bot_id, "gift_colored_buttons", "0") == "1"
    item_style = ButtonStyle.SUCCESS if colored else None
    rows = []
    for g in catalog_gifts:
        if g.star_count > balance:
            continue
        price, item_label = await _gift_price_and_label(g, bot_id)
        rows.append([InlineKeyboardButton(text=f"{item_label} — {price:,} تومان",
                                          callback_data=f"gift:pick:{g.gift_id}",
                                          style=item_style)])
    if manual_gifts:
        rows.append([InlineKeyboardButton(
            text=f"🔗 گیفت‌هایِ سفارشی/NFT ({len(manual_gifts)} مورد)",
            callback_data="gift:mslide:0", style=ButtonStyle.SUCCESS)])
    if not rows:
        await target.answer(
            "🎁 فعلاً هیچ گیفتی موجود نیست — به‌زودی دوباره سر بزن.")
        return
    from tenant import inline_back_row
    br = await inline_back_row(bot_id)
    if br:
        rows.append(br)
    await target.answer("🎁 <b>گیفتِ تلگرام</b>\n\nیکی را انتخاب کن:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


# ─────────────── اسلایدرِ گیفتِ سفارشی/NFT — PATCH_MANUAL_GIFT_SLIDER_V1 ───────────────

async def _manual_gifts_list(bot_id: int) -> list["TelegramGiftConfig"]:
    async with Session() as s:
        return (await s.scalars(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id, TelegramGiftConfig.active == True,  # noqa: E712
            TelegramGiftConfig.is_manual == True  # noqa: E712
        ).order_by(TelegramGiftConfig.id))).all()


async def _manual_slider_render(cb: CallbackQuery, bot_id: int, index: int) -> bool:
    """یک گیفتِ سفارشی را در قالبِ اسلایدر نشان می‌دهد — با دکمهٔ قبلی/بعدی
    برایِ ورق‌زدنِ بینِ چندتا، و خودِ لینک زیرِ متن تا تلگرام پیش‌نمایشِ
    واقعیِ NFT (عکس + اسم) را خودکار نشان بدهد. برایِ حسِ واقعیِ اسلایدر
    (نه انباشتنِ پیامِ تازه به‌ازایِ هر تپ)، پیامِ موجود ادیت می‌شود.
    خروجی: True اگر موفق بود، False اگر لیست خالی بود (تا فراخوان‌کننده
    خودش یک‌بار cb.answer را با پیامِ مناسب صدا بزند — نه اینجا هم)."""
    items = await _manual_gifts_list(bot_id)
    if not items:
        return False
    index = max(0, min(index, len(items) - 1))
    g = items[index]
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"gift:mslide:{index-1}",
                                        style=ButtonStyle.PRIMARY))
    if index < len(items) - 1:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"gift:mslide:{index+1}",
                                        style=ButtonStyle.PRIMARY))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🛒 خریدِ همین گیفت", callback_data=f"gift:pick:{g.gift_id}",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop",
                                      style=ButtonStyle.DANGER)])
    price = g.manual_price_toman or 0
    text = (
        f"🔗 <b>{g.manual_name or 'گیفتِ سفارشی'}</b>\n"
        f"💰 قیمت: {price:,} تومان\n"
        f"📦 موردِ {index + 1} از {len(items)}\n\n"
        f"{g.manual_link or ''}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML", link_preview_options=_LPO_ON)
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML", link_preview_options=_LPO_ON)
    return True


@smm_router.callback_query(F.data.startswith("gift:mslide:"))
async def gift_manual_slide(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    index = int(cb.data.split(":")[2])
    ok = await _manual_slider_render(cb, row.id, index)
    if not ok:
        await cb.answer("🔗 فعلاً هیچ گیفتِ سفارشی‌ای موجود نیست.", show_alert=True)
        return
    await cb.answer()


@smm_router.callback_query(F.data.startswith("gift:pick:"))
async def gift_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    gift_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id,
            TelegramGiftConfig.active == True))  # noqa: E712
    if not g:
        await cb.answer("این گیفت دیگر در دسترس نیست.", show_alert=True)
        return
    # === PATCH_GIFT_QUANTITY_V1 ===
    # گیفتِ سفارشی/NFT (لینک‌دار) یک موردِ منحصربه‌فرد است — تعدادش همیشه
    # ۱ می‌ماند و مستقیم می‌رود سراغِ انتخابِ مقصد (رفتارِ قبلی). فقط
    # گیفتِ کاتالوگی/استارزی مرحلهٔ انتخابِ تعداد را می‌بیند.
    if g.is_manual:
        await cb.answer()
        await _gift_ask_dest(_EditFirstV(cb.message), row.id, gift_id, 1)
        return
    await cb.answer()
    await _EditFirstV(cb.message).answer(
        await _gift_qty_text(g, row.id, 1),
        reply_markup=_gift_qty_kb(gift_id, 1), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("gift:qty:"))
async def gift_qty_adjust(cb: CallbackQuery, bot: Bot):
    """دکمه‌هایِ 🔻/🔺 — فقط عددِ نمایش‌داده‌شده و جمع‌کل را به‌روز می‌کند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    _, _, gift_id, qty_s = cb.data.split(":")
    qty = max(1, min(int(qty_s), _GIFT_MAX_QTY))
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id,
            TelegramGiftConfig.active == True))  # noqa: E712
    if not g:
        await cb.answer("این گیفت دیگر در دسترس نیست.", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            await _gift_qty_text(g, row.id, qty),
            reply_markup=_gift_qty_kb(gift_id, qty), parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (ادیتِ پیامِ تعدادِ گیفت)")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("gift:qcontinue:"))
async def gift_qty_continue(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    _, _, gift_id, qty_s = cb.data.split(":")
    qty = max(1, min(int(qty_s), _GIFT_MAX_QTY))
    await cb.answer()
    # === PATCH_GIFT_VIA_FIRST_V1 ===
    # درخواستِ فدایی: اول روشِ ارسال (ربات/پشتیبانی) پرسیده شود، بعد مقصد — چون «پشتیبانی» برایِ
    # هر آیدی/یوزرنیمی ارسال می‌کند ولی «ربات» فقط برایِ کسانی که ربات را استارت کرده‌اند.
    if (await _get_setting(row.id, "gift_user_choice", "0")) == "1" \
            and (await _get_setting(row.id, "gift_manual_delivery", "0")) != "1":
        async with Session() as s:
            g = await s.scalar(select(TelegramGiftConfig).where(
                TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id))
        if g and not g.is_manual:
            unit_price, item_label = await _gift_price_and_label(g, row.id)
            ckb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 ارسال خودکار توسطِ ربات",
                                      callback_data=f"gift:via0:b:{gift_id}:{qty}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="👤 ارسال با اکانتِ پشتیبانی (دستی)",
                                      callback_data=f"gift:via0:s:{gift_id}:{qty}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop", style=ButtonStyle.DANGER)],
            ])
            await _EditFirstV(cb.message).answer(
                f"🎁 <b>{item_label}</b>" + (f" × {qty}" if qty > 1 else "") + f"\n💰 {unit_price * qty:,} تومان\n\n"
                "گیفت چطور برایِ مقصد فرستاده شود؟\n"
                "🤖 <b>ربات:</b> بعد از پرداخت فوراً و خودکار ارسال می‌شود (فقط برایِ کسی که ربات را استارت کرده).\n"
                "👤 <b>اکانتِ پشتیبانی:</b> دستی ارسال می‌شود و برایِ هر آیدی/یوزرنیمی ممکن است.",
                reply_markup=ckb, parse_mode="HTML")
            return
    await _gift_ask_dest(_EditFirstV(cb.message), row.id, gift_id, qty)


@smm_router.callback_query(F.data.startswith("gift:via0:"))
async def gift_via_first(cb: CallbackQuery, bot: Bot):
    """=== PATCH_GIFT_VIA_FIRST_V1 === بعد از انتخابِ روشِ ارسال، مقصد پرسیده می‌شود."""
    parts = cb.data.split(":")
    via, gift_id, qty = parts[2], parts[3], max(1, min(int(parts[4]), _GIFT_MAX_QTY))
    if via not in ("b", "s"):
        return await cb.answer()
    await cb.answer()
    await _gift_ask_dest(_EditFirstV(cb.message), await _bot_id(bot), gift_id, qty, via=via)


async def _gift_ask_dest(target: Message, bot_id: int, gift_id: str, qty: int, via: str = "") -> None:
    """صفحهٔ انتخابِ مقصد (خودم/کاربرِ دیگر) — بعدِ انتخابِ تعداد."""
    async with Session() as s:
        # === PATCH_GIFT_QUANTITY_V1 ===
        # باگ: قبلاً این کوئری bot_id را فیلتر نمی‌کرد — چون gift_id
        # (به‌خصوص برایِ گیفتِ کاتالوگیِ رسمی) بینِ ربات‌هایِ مختلف
        # می‌تواند یکسان باشد، ممکن بود اطلاعاتِ گیفتِ یک ربات (اسم/ایموجی)
        # در این صفحه‌یِ میانی برایِ ربات‌بازِ دیگری نشان داده شود.
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id, TelegramGiftConfig.gift_id == gift_id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 خرید برای خودم", callback_data=f"gift:dself:{gift_id}:{qty}:{via or 'n'}")],
        [InlineKeyboardButton(text=("👤 برایِ شخصِ دیگر (آیدی یا یوزرنیم)" if via == "s"
                                     else "👤 برایِ کاربرِ دیگر (یوزرنیم)"),
                              callback_data=f"gift:dask:{gift_id}:{qty}:{via or 'n'}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop", style=ButtonStyle.DANGER)],
    ])
    # === PATCH_MANUAL_GIFT_V1 ===
    # برایِ گیفتِ سفارشی، همین‌جا خودِ لینک را هم نشان می‌دهیم — تلگرام
    # معمولاً پیش‌نمایشِ لینک را هم خودکار زیرِ پیام می‌آورد.
    if g and g.is_manual:
        intro = (f"🔗 <b>{g.manual_name or 'گیفتِ سفارشی'}</b>\n\n"
                f"{g.manual_link or ''}\n\n"
                f"این گیفت را برایِ چه کسی می‌خری؟\n\n")
    else:
        qty_line = f" — تعداد: {qty}" if qty > 1 else ""
        intro = f"{(g.emoji if g else '🎁') or '🎁'} این گیفت را برایِ چه کسی می‌خری؟{qty_line}\n\n"
    if via == "s":   # === PATCH_GIFT_VIA_FIRST_V1 === پشتیبانی دستی می‌فرستد؛ محدودیتِ استارت ندارد
        _warn = "ℹ️ ارسال با اکانتِ پشتیبانی است؛ برایِ «شخصِ دیگر» هر آیدیِ عددی یا یوزرنیمی که ربات را استارت کرده، قبول می‌شود."
    else:
        _warn = ("ℹ️ اگر می‌خواهی خودِ ربات گیفت را برایِ آن شخص بفرستد، او باید یک‌بار رباتِ ما را استارت کرده باشد؛ "
                 "وگرنه گیفت توسطِ پشتیبانی به‌صورتِ دستی ارسال می‌شود.")
    await target.answer(
        intro + _warn,
        reply_markup=kb, parse_mode="HTML", link_preview_options=_LPO_ON)


@smm_router.callback_query(F.data == "gift:back_shop")
async def gift_back_shop(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await cb.answer()
    await show_gift_shop_to_user(bot, _EditFirstV(cb.message), row.id)


async def _vsd_rows_lazy(bot_id: int, kind: str, price: int):
    """PATCH_VS_DISCOUNT_V1 — ردیفِ دکمهٔ «کد تخفیف» (خالی اگر قابلیت/کدی نیست)."""
    from tenant.bots.virtual_service.vs_discount import vsd_rows
    return await vsd_rows(bot_id, kind, price)


async def _gift_show_invoice(target: Message, bot_id: int, gift_id: str,
                             dest_id: int, dest_label: str, qty: int = 1, via: str = "",
                             comment: str = "", state: "FSMContext | None" = None) -> None:
    anon, scope = False, 0
    if state is not None:   # PATCH_GIFT_ANON_SCOPE_V1
        comment, anon, scope = _gift_opts_from(await state.get_data(), gift_id, dest_id, qty)
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id, TelegramGiftConfig.gift_id == gift_id))
    if not g:
        await target.answer("این گیفت دیگر در دسترس نیست.")
        return
    # === PATCH_GIFT_QUANTITY_V1 ===
    qty = 1 if g.is_manual else max(1, min(qty, _GIFT_MAX_QTY))
    unit_price, item_label = await _gift_price_and_label(g, bot_id)
    total_price = unit_price * qty
    # === PATCH_GIFT_USER_CHOICE_V1 ===
    # اگر ادمین «انتخابِ روشِ ارسال توسطِ مشتری» را روشن کرده باشد، قبل از فاکتور می‌پرسیم
    # ربات خودکار بفرستد یا اکانتِ پشتیبانی (دستی). گیفتِ سفارشی و حالتِ «تحویلِ دستیِ همه» مستثنی‌اند.
    if (not via and not g.is_manual
            and (await _get_setting(bot_id, "gift_user_choice", "0")) == "1"
            and (await _get_setting(bot_id, "gift_manual_delivery", "0")) != "1"):
        ckb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 ارسال خودکار توسطِ ربات",
                                  callback_data=f"gift:via:b:{gift_id}:{dest_id}:{qty}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="👤 ارسال با اکانتِ پشتیبانی (دستی)",
                                  callback_data=f"gift:via:s:{gift_id}:{dest_id}:{qty}",
                                  style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop", style=ButtonStyle.DANGER)],
        ])
        await target.answer(
            f"🎁 <b>{item_label}</b>" + (f" × {qty}" if qty > 1 else "") + f"\n💰 {total_price:,} تومان\n\n"
            "گیفت چطور برایت فرستاده شود؟\n"
            "🤖 <b>ربات:</b> بعد از پرداخت فوراً و خودکار ارسال می‌شود.\n"
            "👤 <b>اکانتِ پشتیبانی:</b> سفارشت برایِ پشتیبانی می‌رود و دستی برایت می‌فرستد.",
            reply_markup=ckb, parse_mode="HTML")
        return
    _sfx = f":{via}" if via else ""
    # === PATCH_GIFT_WIZARD_V1 === (درخواستِ MoBiN) مراحلِ کامنت ← شناس/ناشناس ← پرداخت، همه در یک پیام (مثلِ ری‌اکشن)
    _wz_manual = bool(g.is_manual or via == "s"
                      or (await _get_setting(bot_id, "gift_manual_delivery", "0")) == "1")
    _wz_done = False
    if state is not None and (not g.is_manual or _wz_manual):
        _need_cmt, _need_anon = (not g.is_manual), _wz_manual
        _gc = dict((await state.get_data()).get("gift_comment") or {})
        _key = f"{gift_id}:{dest_id}:{qty}"
        if _gc.get("k") != _key:
            _gc = {"k": _key, "t": "", "anon": False, "scope": 0}
        _wz = _gc.get("wz") or ("c" if _need_cmt else "a")
        if _wz == "c" and not _need_cmt:
            _wz = "a"
        if _wz == "a" and not _need_anon:
            _wz = "d"
        _gc["wz"] = _wz
        await state.update_data(gift_comment=_gc)
        if _wz in ("c", "a"):
            await _gift_wizard_step(target, state, _wz, gift_id, dest_id, qty, via, _gc, dest_label)
            return
        _wz_done = True
    # === PATCH_GIFT_CARD_PAYMENT_V1 ===
    # قبلاً فقط یک دکمهٔ «پرداخت» بود (فقط کیف‌پول). حالا روشِ پرداخت هم
    # پرسیده می‌شود — کیف‌پول (فوری) یا کارت‌به‌کارت (نیازِ به تاییدِ ادمین).
    _cmt_row = []
    if not g.is_manual:   # PATCH_GIFT_COMMENT_V1 — کامنتِ اختیاریِ همراهِ گیفت
        _cmt_row = [[InlineKeyboardButton(
            text=("✏️ ویرایشِ کامنت" if comment else "💬 افزودنِ کامنت (اختیاری)"),
            callback_data=f"gift:cmt:{gift_id}:{dest_id}:{qty}:{via or 'n'}", style=ButtonStyle.PRIMARY)]]
    # === PATCH_GIFT_ANON_SCOPE_V1 === ناشناس/با نام
    # گزارش (Fadayi): ناشناس/با نام فقط برایِ ارسالِ دستی (اکانتِ پشتیبانی) معنا دارد؛ برایِ ارسالِ
    # خودکارِ ربات (Bot API) امکانش نیست، پس آن‌جا اصلاً نشان داده نمی‌شود.
    _manual_path = bool(g.is_manual or via == "s"
                        or (await _get_setting(bot_id, "gift_manual_delivery", "0")) == "1")
    if not _manual_path:
        anon = False
    _anon_row = [[InlineKeyboardButton(
        text=("🕶 ارسال: ناشناس (برای تغییر بزن)" if anon else "👤 ارسال: با نامِ من (برای ناشناس‌شدن بزن)"),
        callback_data=f"gift:an:{gift_id}:{dest_id}:{qty}:{via or 'n'}", style=ButtonStyle.PRIMARY)]] if _manual_path else []
    if _wz_done:   # PATCH_GIFT_WIZARD_V1 — کامنت/ناشناس در مراحلِ قبل گرفته شده؛ فاکتور فقط پرداخت است
        _cmt_row, _anon_row = [], []
    if comment and qty > 1:
        _cmt_row.append([InlineKeyboardButton(
            text=("📝 کامنت برایِ: " + ("همهٔ گیفت‌ها" if not scope else f"گیفتِ شمارهٔ {scope}") + " (تغییر)"),
            callback_data=f"gift:csc:{gift_id}:{dest_id}:{qty}:{via or 'n'}")])
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — تیک‌هایِ مالک + شارژ حساب
    _pay_rows = await _vp.pay_rows(bot_id, _vp.tg_of(target), int(total_price),
                                   f"gift:pay:{gift_id}:{dest_id}:{qty}{_sfx}",
                                   f"gift:cardpay:{gift_id}:{dest_id}:{qty}{_sfx}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *_cmt_row,
        *_anon_row,
        *_pay_rows,
        *await _vsd_rows_lazy(bot_id, "gift", total_price),   # PATCH_VS_DISCOUNT_V1
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop", style=ButtonStyle.DANGER)],
    ])
    qty_line = f"تعداد: {qty}\n" if qty > 1 else ""
    price_line = (f"قیمتِ واحد: {unit_price:,} تومان\nجمعِ کل: {total_price:,} تومان"
                 if qty > 1 else f"قیمت: {total_price:,} تومان")
    await target.answer(
        f"🧾 <b>فاکتورِ خرید</b>\n\n"
        f"کالا: {item_label}\n"
        f"{qty_line}"
        f"مقصد: {dest_label}\n"
        + (f"💬 کامنت: {_html_escape(comment)}"
           + ((" (فقط گیفتِ شمارهٔ %d)" % scope) if (scope and qty > 1) else (" (برایِ همهٔ گیفت‌ها)" if qty > 1 else ""))
           + "\n" if comment else "")
        + ("🕶 فرستنده: ناشناس (گیفت توسطِ پشتیبانی و به‌صورتِ دستی ارسال می‌شود)\n" if anon else "")
        + f"{price_line}\n\n"
        f"روشِ پرداخت را انتخاب کن:" + ("" if _pay_rows else _vp.NO_METHOD_NOTE),
        reply_markup=kb, parse_mode="HTML")


async def _gift_wizard_step(target, state, step: str, gift_id, dest_id, qty, via, gc: dict, dest_label: str) -> None:
    """=== PATCH_GIFT_WIZARD_V1 === مرحلهٔ کامنت ('c') یا شناس/ناشناس ('a') — یک پیام که ویرایش می‌شود."""
    sfx = f"{gift_id}:{dest_id}:{qty}:{via or 'n'}"
    back = [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="gift:back_shop", style=ButtonStyle.DANGER)]
    if step == "c":
        await state.set_state(SmmFSM.gift_comment)
        await state.update_data(gc_gift=gift_id, gc_dest=dest_id, gc_qty=qty, gc_via=via or "")
        await target.answer(
            "💬 <b>کامنتِ همراهِ گیفت</b> (اختیاری)\n\n"
            f"مقصد: {dest_label}\n\n"
            "اگر می‌خواهی همراهِ گیفت کامنت باشد، <b>متنت را همین‌جا بفرست</b> (حداکثر ۱۲۸ حرف).\n"
            "اگر کامنت نمی‌خواهی، دکمهٔ «⏭ رد کردن» را بزن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ رد کردن", callback_data=f"gift:wz:skip:{sfx}", style=ButtonStyle.PRIMARY)],
                back]),
            parse_mode="HTML")
        return
    await state.set_state(None)
    sel, anon = bool(gc.get("anon_set")), bool(gc.get("anon"))
    cmt = gc.get("t") or ""
    await target.answer(
        "🕶 <b>فرستنده: شناس یا ناشناس؟</b>\n\n"
        + (f"💬 کامنت: {_html_escape(cmt)}\n\n" if cmt else "")
        + "👤 <b>شناس:</b> گیرنده می‌بیند گیفت از طرفِ تو بوده.\n"
        "🕶 <b>ناشناس:</b> نامِ فرستنده مخفی می‌شود (گیفت توسطِ پشتیبانی دستی ارسال می‌شود).\n\n"
        "یکی را انتخاب کن، بعد «ثبت کردن» را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("✅ 👤 شناس" if sel and not anon else "👤 شناس"),
                                  callback_data=f"gift:wz:an:0:{sfx}", style=ButtonStyle.PRIMARY),
             InlineKeyboardButton(text=("✅ 🕶 ناشناس" if sel and anon else "🕶 ناشناس"),
                                  callback_data=f"gift:wz:an:1:{sfx}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="✅ ثبت کردن", callback_data=f"gift:wz:ok:{sfx}", style=ButtonStyle.SUCCESS)],
            back]),
        parse_mode="HTML")


async def _gift_wz_apply(cb: CallbackQuery, bot: Bot, state: FSMContext, parts: list, mutate) -> None:
    gift_id, dest_id, qty, via = parts[0], int(parts[1]), int(parts[2]), parts[3]
    gc = dict((await state.get_data()).get("gift_comment") or {})
    if gc.get("k") != f"{gift_id}:{dest_id}:{qty}":
        return await cb.answer("منقضی شد؛ دوباره از اول شروع کن.", show_alert=True)
    mutate(gc)
    await state.update_data(gift_comment=gc)
    await cb.answer()
    bot_id = await _bot_id(bot)
    label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
             else await _resolve_dest_label(bot_id, dest_id))
    await _gift_show_invoice(_EditFirstV(cb.message), bot_id, gift_id, dest_id, label, qty,
                             via="" if via == "n" else via, state=state)


@smm_router.callback_query(F.data.startswith("gift:wz:skip:"))
async def gift_wz_skip(cb: CallbackQuery, bot: Bot, state: FSMContext):
    def _m(gc):
        gc["t"], gc["wz"] = "", "a"
    await _gift_wz_apply(cb, bot, state, cb.data.split(":")[3:], _m)


@smm_router.callback_query(F.data.startswith("gift:wz:an:"))
async def gift_wz_anon(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    val = parts[3] == "1"

    def _m(gc):
        gc["anon"], gc["anon_set"], gc["wz"] = val, True, "a"
    await _gift_wz_apply(cb, bot, state, parts[4:], _m)


@smm_router.callback_query(F.data.startswith("gift:wz:ok:"))
async def gift_wz_ok(cb: CallbackQuery, bot: Bot, state: FSMContext):
    def _m(gc):
        gc["wz"] = "d"
    await _gift_wz_apply(cb, bot, state, cb.data.split(":")[3:], _m)


@smm_router.callback_query(F.data.startswith("gift:cmt:"))
async def gift_comment_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_GIFT_COMMENT_V1 === کامنتِ اختیاریِ گیفت. (حالتِ «مخفی‌کردنِ نامِ فرستنده» در Bot APIِ تلگرام
    برایِ ربات‌ها وجود ندارد؛ فقط متنِ کامنت پشتیبانی می‌شود.)"""
    from tenant import smart_cancel_kb
    parts = cb.data.split(":")
    gift_id, dest_id, qty, via = parts[2], int(parts[3]), int(parts[4]), parts[5]
    await state.set_state(SmmFSM.gift_comment)
    await state.update_data(gc_gift=gift_id, gc_dest=dest_id, gc_qty=qty, gc_via=("" if via == "n" else via))
    await cb.answer()
    await cb.message.answer("💬 متنِ کامنتِ همراهِ گیفت را بفرست (حداکثر ۱۲۸ حرف).\nبرایِ حذفِ کامنت «-» بفرست:",
                            reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.gift_comment, F.text)
async def gift_comment_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    gift_id, dest_id = data.get("gc_gift"), data.get("gc_dest")
    qty, via = int(data.get("gc_qty") or 1), data.get("gc_via") or ""
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    comment = "" if raw in ("-", "") else raw[:128]
    key = f"{gift_id}:{dest_id}:{qty}"
    await state.set_state(None)
    _old = (data.get("gift_comment") or {})
    _same = _old.get("k") == key
    _wz = (_old.get("wz") if _same else None)
    if _wz == "c":   # PATCH_GIFT_WIZARD_V1 — کامنت ثبت شد، مرحلهٔ بعد
        _wz = "a"
    await state.update_data(gift_comment={"k": key, "t": comment,
                                          "anon": bool(_old.get("anon")) if _same else False,
                                          "scope": int(_old.get("scope") or 0) if _same else 0,
                                          "wz": _wz, "anon_set": bool(_old.get("anon_set")) if _same else False})
    bot_id = await _bot_id(bot)
    label = (_self_dest_label(msg.from_user) if dest_id == msg.from_user.id
             else await _resolve_dest_label(bot_id, dest_id))
    if comment and qty > 1:   # PATCH_GIFT_ANON_SCOPE_V1 — کامنت برایِ کدام گیفت؟
        await _gift_ask_scope(msg, gift_id, dest_id, qty, via)
        return
    await _gift_show_invoice(msg, bot_id, gift_id, dest_id, label, qty, via=via, state=state)


def _gift_comment_from(data: dict, gift_id, dest_id, qty) -> str:
    gc = (data or {}).get("gift_comment") or {}
    return (gc.get("t") or "") if gc.get("k") == f"{gift_id}:{dest_id}:{qty}" else ""


def _gift_opts_from(data: dict, gift_id, dest_id, qty) -> tuple[str, bool, int]:
    """=== PATCH_GIFT_ANON_SCOPE_V1 === (کامنت، ناشناس؟، شمارهٔ گیفتِ کامنت‌دار؛ ۰=همه)"""
    gc = (data or {}).get("gift_comment") or {}
    if gc.get("k") != f"{gift_id}:{dest_id}:{qty}":
        return "", False, 0
    scope = int(gc.get("scope") or 0)
    return (gc.get("t") or ""), bool(gc.get("anon")), (scope if 0 < scope <= qty else 0)


def _gift_comment_for(comment: str, scope: int, idx: int) -> str:
    """متنِ کامنت برایِ گیفتِ شمارهٔ idx (از ۰). scope=0 → همه، وگرنه فقط گیفتِ شمارهٔ scope."""
    if not comment:
        return ""
    return comment if (not scope or idx + 1 == scope) else ""


async def _gift_ask_scope(target, gift_id, dest_id, qty, via) -> None:
    sfx = f"{gift_id}:{dest_id}:{qty}:{via or 'n'}"
    nums = [InlineKeyboardButton(text=str(i), callback_data=f"gift:cs:{sfx}:{i}") for i in range(1, qty + 1)]
    rows = [[InlineKeyboardButton(text="🎁 همهٔ گیفت‌ها", callback_data=f"gift:cs:{sfx}:0",
                                  style=ButtonStyle.SUCCESS)]]
    for j in range(0, len(nums), 5):
        rows.append(nums[j:j + 5])
    await target.answer(
        f"💬 تعدادِ گیفت‌ها {qty} است. این کامنت را برایِ <b>کدام گیفت</b> می‌خواهی؟\n"
        "«همهٔ گیفت‌ها» = همین کامنت همراهِ هر گیفت می‌رود.\n"
        "یا شمارهٔ یک گیفت را بزن = فقط همان گیفت کامنت دارد و بقیه بدونِ کامنت می‌روند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("gift:csc:"))
async def gift_scope_change(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    gift_id, dest_id, qty, via = parts[2], int(parts[3]), int(parts[4]), parts[5]
    await cb.answer()
    await _gift_ask_scope(_EditFirstV(cb.message), gift_id, dest_id, qty, "" if via == "n" else via)


@smm_router.callback_query(F.data.startswith("gift:cs:"))
async def gift_scope_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    gift_id, dest_id, qty, via, scope = parts[2], int(parts[3]), int(parts[4]), parts[5], int(parts[6])
    data = await state.get_data()
    gc = dict(data.get("gift_comment") or {})
    if gc.get("k") != f"{gift_id}:{dest_id}:{qty}":
        return await cb.answer("منقضی شد؛ دوباره کامنت را بنویس.", show_alert=True)
    gc["scope"] = scope if 0 <= scope <= qty else 0
    await state.update_data(gift_comment=gc)
    await cb.answer()
    bot_id = await _bot_id(bot)
    label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
             else await _resolve_dest_label(bot_id, dest_id))
    await _gift_show_invoice(_EditFirstV(cb.message), bot_id, gift_id, dest_id, label, qty,
                             via="" if via == "n" else via, state=state)


@smm_router.callback_query(F.data.startswith("gift:an:"))
async def gift_anon_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    gift_id, dest_id, qty, via = parts[2], int(parts[3]), int(parts[4]), parts[5]
    data = await state.get_data()
    key = f"{gift_id}:{dest_id}:{qty}"
    gc = dict(data.get("gift_comment") or {})
    if gc.get("k") != key:
        gc = {"k": key, "t": "", "anon": False, "scope": 0}
    gc["anon"] = not bool(gc.get("anon"))
    await state.update_data(gift_comment=gc)
    await cb.answer("🕶 ناشناس" if gc["anon"] else "👤 با نامِ شما")
    bot_id = await _bot_id(bot)
    label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
             else await _resolve_dest_label(bot_id, dest_id))
    await _gift_show_invoice(_EditFirstV(cb.message), bot_id, gift_id, dest_id, label, qty,
                             via="" if via == "n" else via, state=state)


@smm_router.callback_query(F.data.startswith("gift:via:"))
async def gift_choose_via(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_GIFT_USER_CHOICE_V1 === انتخابِ مشتری: b = ربات، s = اکانتِ پشتیبانی."""
    parts = cb.data.split(":")
    via, gift_id, dest_id, qty = parts[2], parts[3], int(parts[4]), int(parts[5])
    if via not in ("b", "s"):
        return await cb.answer()
    await cb.answer()
    await _gift_show_invoice(_EditFirstV(cb.message), (await _bot_id(bot)), gift_id, dest_id,
                             _self_dest_label(cb.from_user) if dest_id == cb.from_user.id
                             else await _resolve_dest_label(await _bot_id(bot), dest_id), qty, via=via, state=state)


@smm_router.callback_query(F.data.startswith("gift:dself:"))
async def gift_dest_self(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    gift_id = parts[2]
    qty = int(parts[3]) if len(parts) > 3 else 1
    via = parts[4] if len(parts) > 4 and parts[4] in ("b", "s") else ""   # PATCH_GIFT_VIA_FIRST_V1
    await cb.answer()
    await state.update_data(gift_comment=None)   # PATCH_GIFT_WIZARD_V1 — هر خریدِ تازه از مرحلهٔ کامنت شروع شود
    await _gift_show_invoice(_EditFirstV(cb.message), (await _bot_id(bot)), gift_id, cb.from_user.id, "خودم", qty,
                             via=via, state=state)


@smm_router.callback_query(F.data.startswith("gift:dask:"))
async def gift_dest_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = cb.data.split(":")
    gift_id = parts[2]
    qty = int(parts[3]) if len(parts) > 3 else 1
    via = parts[4] if len(parts) > 4 and parts[4] in ("b", "s") else ""   # PATCH_GIFT_VIA_FIRST_V1
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.gift_dest_username)
    await state.update_data(gift_dest_gift_id=gift_id, gift_dest_qty=qty, gift_dest_via=via)
    await _EditFirstV(cb.message).answer(
        ("👤 آیدیِ عددی یا یوزرنیمِ مقصد را بفرست (بدونِ @):" if via == "s"
         else "👤 لطفاً یوزرنیمِ مقصد را بفرست (بدونِ @):"),
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.gift_dest_username, F.text)
async def gift_dest_username_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    data = await state.get_data()
    gift_id = data.get("gift_dest_gift_id")
    qty = data.get("gift_dest_qty", 1)
    via = data.get("gift_dest_via") or ""   # PATCH_GIFT_VIA_FIRST_V1
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not gift_id:
        return
    _raw = (msg.text or "").strip()
    _nn = _norm_num(_raw).lstrip("@")
    uname = _nn if _nn.isdigit() else _raw.lstrip("@")
    # === PATCH_GIFT_VIA_FIRST_V1 === آیدیِ عددی: با «پشتیبانی» هر آیدی قبول است (حتی اگر ربات را
    # استارت نکرده)؛ با «ربات» فقط اگر قبلاً استارت کرده باشد.
    if uname.isdigit():
        _did = int(uname)
        async with Session() as s:
            _known = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == _did))
        if _known or via == "s":
            await _gift_show_invoice(msg, row.id, gift_id, _did,
                                     await _resolve_dest_label(row.id, _did), qty, via=via, state=state)
            return
        # === PATCH_GIFT_ANY_DEST_V1 === (درخواستِ MoBiN) گیرنده لازم نیست عضوِ ربات باشد (کانال/سورپرایز):
        # ربات‌ها نمی‌توانند به کسی که استارت نکرده گیفت بفرستند، پس چنین سفارشی خودکار به «اکانتِ پشتیبانی/دستی» می‌رود.
        await _gift_show_invoice(msg, row.id, gift_id, _did,
                                 await _resolve_dest_label(row.id, _did), qty, via="s", state=state)
        return
    async with Session() as s:
        target_user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.username == uname))
    if not target_user:
        # === PATCH_GIFT_ANY_DEST_V1 === یوزرنیمِ ناشناخته (کانال/کسی که استارت نکرده): سفارش با اکانتِ پشتیبانی/دستی
        if not _valid_tg_username(uname):
            await msg.answer("❌ یوزرنیم نامعتبر است (۵ تا ۳۲ حرفِ انگلیسی/عدد/زیرخط، بدونِ @). دوباره بفرست.")
            return
        dest_id = await _ensure_placeholder_dest(row.id, uname)
        await _gift_show_invoice(msg, row.id, gift_id, dest_id, f"@{uname}", qty, via="s", state=state)
        return
    await _gift_show_invoice(msg, row.id, gift_id, target_user.telegram_id, f"@{uname}", qty, via=via, state=state)


def _valid_tg_username(u: str) -> bool:
    import re as _r
    return bool(_r.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", u or ""))


async def _ensure_placeholder_dest(bot_id: int, uname: str) -> int:
    """=== PATCH_GIFT_ANY_DEST_V1 === برایِ گیرنده‌ایِ ناشناخته (کانال/نااستارت) یک ردیفِ «جانشین» با آیدیِ منفیِ ثابت می‌سازد تا
    کلِ مسیرِ گیفت (callbackها که آیدیِ عددی می‌خواهند) بدونِ تغییر کار کند. مسدود علامت می‌خورد تا پیامِ همگانی/آمار را خراب نکند.
    آیدیِ منفی فقط به مسیرِ دستی/پشتیبانی می‌رسد (via=s) و هرگز به bot.send_gift."""
    import zlib as _z
    tid = -((_z.crc32(f"ph:{bot_id}:{uname.lower()}".encode()) % 2_000_000_000) + 1)
    from sqlalchemy.exc import IntegrityError
    for _ in range(2):
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tid))
            if u is not None:
                if (u.username or "").lower() != uname.lower():
                    u.username = uname
                    await s.commit()
                return tid
            s.add(BotUser(bot_id=bot_id, telegram_id=tid, username=uname,
                          full_name=f"@{uname} (گیرندهٔ بدونِ استارت)", is_blocked=True))
            try:
                await s.commit()
                return tid
            except IntegrityError:
                await s.rollback()
    return tid


async def _bot_id(bot: Bot) -> int:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    return row.id if row else 0


# === PATCH_SUPPORT_FIRST_V1 ===
# درخواستِ MD04: گیفت/استارزِ دستی با اکانتِ شخصیِ ادمین ارسال می‌شود؛ اگر مشتری قبلاً به
# آن اکانت پیام نداده باشد تلگرام ارسال را محدود/ریپورت می‌کند. پس بعد از ثبتِ سفارش از مشتری
# می‌خواهیم اول یک پیام به پشتیبانی بفرستد. مالک می‌تواند روشن/خاموش و متن را عوض کند.
_SFIRST_DEFAULT = (
    "📩 <b>مهم:</b> برایِ اینکه سفارشت بدونِ مشکل تحویل داده بشه، همین الان یک پیامِ کوتاه "
    "(مثلاً «سلام، سفارش دادم») برایِ پشتیبانی بفرست.\n"
    "بدونِ این پیامِ اولیه، ارسال از اکانتِ ما ممکنه با محدودیتِ تلگرام روبه‌رو بشه و دیر برسه.")


async def _with_sfirst(bot_id: int, text: str, kb: InlineKeyboardMarkup | None):
    """اگر قابلیت روشن است، متنِ هشدار را به پیام اضافه و دکمهٔ پشتیبانی را بالایِ کیبورد می‌گذارد."""
    try:
        if (await _get_setting(bot_id, "support_first_on", "0")) != "1":
            return text, kb
        note = (await _get_setting(bot_id, "support_first_text", "")).strip() or _SFIRST_DEFAULT
        from tenant import support_button
        sb = await support_button(bot_id, text="💬 ارسال پیام به پشتیبانی")
        rows = list(kb.inline_keyboard) if kb else []
        if sb:
            rows.insert(0, [sb])
        return text + "\n\n" + note, InlineKeyboardMarkup(inline_keyboard=rows)
    except Exception:
        logger.exception("support_first note")
        return text, kb


def _sfirst_button_row(on: bool, origin: str) -> list:
    return [InlineKeyboardButton(
        text=("📩 «اول به پشتیبانی پیام بده»: روشن ✅" if on else "📩 «اول به پشتیبانی پیام بده»: خاموش"),
        callback_data=f"sfirst:toggle:{origin}",
        style=(ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY))]


@smm_router.callback_query(F.data.startswith("sfirst:toggle:"))
async def sfirst_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    origin = cb.data.split(":")[2]
    cur = await _get_setting(row.id, "support_first_on", "0")
    await _set_setting(row.id, "support_first_on", "0" if cur == "1" else "1")
    if cur != "1":
        await cb.message.answer(
            "✅ روشن شد. بعد از ثبتِ سفارشِ گیفت/استارزِ دستی، به مشتری گفته می‌شود اول به پشتیبانی پیام بدهد.\n\n"
            f"متنِ فعلی:\n{(await _get_setting(row.id, 'support_first_text', '')).strip() or _SFIRST_DEFAULT}\n\n"
            "برایِ تغییرِ متن دکمهٔ «✏️ متنِ پیامِ پشتیبانی» را بزن. دکمهٔ «💬 ارسال پیام به پشتیبانی» "
            "هم فقط وقتی می‌آید که «پشتیبانی» در تنظیماتِ ربات ثبت شده باشد.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="✏️ متنِ پیامِ پشتیبانی", callback_data=f"sfirst:text:{origin}")]]))
    await cb.answer("روشن شد" if cur != "1" else "خاموش شد")
    await _sfirst_back(cb, bot, origin, row.id)


async def _sfirst_back(cb, bot, origin, bot_id):
    try:
        if origin == "stars":
            text, kb = await stars_sell_admin_content(bot_id)
        else:
            text, kb = await gift_admin_content(bot_id, bot)
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("sfirst back")


@smm_router.callback_query(F.data.startswith("sfirst:text:"))
async def sfirst_text_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.sfirst_text)
    await cb.message.answer(
        "✏️ متنِ جدید را بفرست (HTML ساده مجاز است). برایِ برگشت به متنِ پیش‌فرض «-» بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.sfirst_text, F.text)
async def sfirst_text_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    r = await get_bot_row(bot)
    if not r or not await is_owner_or_admin(r.id, msg.from_user.id, r.owner_id):
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        return await msg.answer("لغو شد.")
    await _set_setting(r.id, "support_first_text", "" if raw == "-" else (msg.html_text or raw)[:900])
    await state.clear()
    await msg.answer("✅ ذخیره شد.")


async def _gift_support_kb(tracking_code: str) -> InlineKeyboardMarkup:
    """دکمهٔ «📞 پشتیبانی» که با یک تپ، تیکتِ تازه با کدِ پیگیریِ همین
    سفارش را می‌سازد — کاربر لازم نیست چیزی تایپ کند."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📞 پشتیبانی (پیگیریِ این سفارش)",
        callback_data=f"gift:support:{tracking_code}")]])


# === PATCH_CARD_LAST4_V1 ===
def _extract_card_last4(caption: str | None) -> str | None:
    """۴ رقمِ آخرِ کارت را از کپشنِ عکسِ رسید استخراج می‌کند — فقط وقتی
    کپشن دقیقاً شاملِ یک عددِ ۴ رقمی باشد (رقم‌هایِ فارسی هم پشتیبانی
    می‌شوند)؛ در غیرِ این صورت None برمی‌گرداند تا کاربر دوباره بفرستد."""
    import re
    if not caption:
        return None
    fa_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    text = caption.translate(fa_map)
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    return m.group(1) if m else None


# === PATCH_ORDER_TICKET_FLOW_V1 === (فدایی)
# قبلاً با یک تپ فوراً تیکتی «پیگیریِ سفارش X» (بدونِ هیچ توضیحِ مشتری و بدونِ مشخصاتِ سفارش) ساخته می‌شد، و
# برایِ رباتِ پرتردد تیکتِ بی‌محتوا می‌ریخت. حالا: کد پیگیری از دکمه می‌آید، متنِ مشتری پرسیده می‌شود، و تیکت
# هم‌راهِ «مشخصاتِ سفارش» (همان صفحهٔ جزئیاتِ لیستِ سفارش‌ها) برایِ ادمین می‌رود.
class OrderTicketFSM(StatesGroup):
    text = State()


async def _order_details_html(bot_id: int, trk: str) -> str:
    """مشخصاتِ سفارش با همان رندرِ جزئیاتِ لیستِ سفارش‌هایِ ادمین؛ خالی اگر کد پیدا نشد."""
    code = (trk or "").strip().upper()
    if not code:
        return ""
    try:
        async with Session() as s:
            sid = await s.scalar(select(SmmOrder.id).where(SmmOrder.bot_id == bot_id, SmmOrder.tracking_code == code))
            gid = None if sid else await s.scalar(select(GiftOrder.id).where(
                GiftOrder.bot_id == bot_id, GiftOrder.tracking_code == code))
        if sid:
            text, _ = await _smm_vs_order_detail_content(bot_id, int(sid), "noop")
        elif gid:
            text, _ = await _vs_gift_detail_content(bot_id, int(gid), "noop")
        elif re.fullmatch(r"N\d+", code):
            text, _ = await _vs_nft_detail_content(bot_id, int(code[1:]), "noop")
        else:
            return ""
        return text or ""
    except Exception:
        logger.exception("مشخصاتِ سفارش برایِ تیکت خوانده نشد (trk=%s)", trk)
        return ""


def compact_order_details(det: str) -> str:
    """=== PATCH_ORDER_TICKET_FORMAT_V1 === (Fadayi) مشخصاتِ سفارش برایِ تیکت: فقط «مقدار»ِ هر خط قابلِ‌کپی (<code>)،
    نه کلِ بلوک؛ و کلِ مشخصات در نقل‌قولِ جمع‌شونده (expandable) تا تیکت کوتاه بماند."""
    import html as _h
    import re as _re
    if not det:
        return ""

    def _fix(m):
        out = []
        for line in m.group(1).strip("\n").split("\n"):
            mm = _re.match(r"\s*≡\s*(.+?)\s*:\s*(.*)$", line)
            if not mm:
                out.append(line.strip())
                continue
            key, val = mm.group(1), _h.unescape(mm.group(2)).strip()
            out.append(f"{key}: " + (f"<code>{_h.escape(val)}</code>" if val and val != "—" else "—"))
        return "\n".join(out)
    body = _re.sub(r"<code>(.*?)</code>", _fix, det, flags=_re.S)
    return f"<blockquote expandable>{body.strip()}</blockquote>"


async def build_order_ticket_text(bot_id: int, trk: str, user_text: str = "") -> str:
    import html as _h
    parts = [f"📞 <b>پیگیریِ سفارش</b>\nکدِ پیگیری: <code>{_h.escape(trk)}</code>"]
    if user_text.strip():
        parts.append("💬 <b>پیامِ مشتری:</b>\n" + _h.escape(user_text.strip()))
    det = compact_order_details(await _order_details_html(bot_id, trk))
    if det:
        parts.append("📋 <b>مشخصاتِ سفارش:</b>\n" + det)
    else:
        parts.append("⚠️ مشخصاتِ این کدِ پیگیری پیدا نشد.")
    return "\n\n".join(parts)


async def recent_orders_ticket_block(bot_id: int, telegram_id: int, limit: int = 3) -> str:
    """=== PATCH_ORDER_TICKET_FLOW_V2 === (Fadayi، تیکتِ #2413) تیکتی که مشتری از منوی «تیکت ← پیگیریِ سفارش» می‌سازد
    کدِ پیگیری ندارد؛ آخرین سفارش‌هایِ همان مشتری (با همان رندرِ جزئیاتِ ادمین) به تیکت اضافه می‌شود."""
    import html as _h
    try:
        async with Session() as s:
            uid = await s.scalar(select(BotUser.id).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
            rows = []
            if uid:
                rows += (await s.execute(select(GiftOrder.tracking_code, GiftOrder.created_at).where(
                    GiftOrder.bot_id == bot_id, GiftOrder.buyer_user_id == uid,
                    GiftOrder.tracking_code.is_not(None)).order_by(GiftOrder.id.desc()).limit(limit))).all()
            rows += (await s.execute(select(SmmOrder.tracking_code, SmmOrder.created_at).where(
                SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == telegram_id,
                SmmOrder.tracking_code.is_not(None)).order_by(SmmOrder.id.desc()).limit(limit))).all()
        rows.sort(key=lambda r: r[1] or 0, reverse=True)
        parts = []
        for trk, _ in rows[:limit]:
            det = compact_order_details(await _order_details_html(bot_id, trk))
            if det:
                parts.append(f"🔖 کدِ پیگیری: <code>{_h.escape(trk)}</code>\n{det}")
        if not parts:
            return "⚠️ این مشتری سفارشِ ثبت‌شده‌ای با کدِ پیگیری ندارد."
        out = "📋 <b>آخرین سفارش‌هایِ این مشتری:</b>\n\n" + "\n\n".join(parts)
        return out if len(out) <= 3000 else "📋 <b>آخرین سفارش‌هایِ این مشتری:</b>\n\n" + "\n\n".join(parts[:2])
    except Exception:
        logger.exception("آخرین سفارش‌هایِ مشتری برایِ تیکت خوانده نشد")
        return ""


@smm_router.callback_query(F.data.startswith("gift:support:"))
async def gift_support_quick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """دکمهٔ «📞 پشتیبانی (پیگیریِ این سفارش)»: متنِ مشتری را می‌گیرد و تیکتِ کامل (با مشخصاتِ سفارش) می‌سازد."""
    from tenant import get_bot_row
    from tenant.shared.support_ticket import _save_new_ticket
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    trk = cb.data.split(":", 2)[2]
    await cb.answer()
    if (await _get_setting(row.id, "order_ticket_ask_text", "1")) != "0":
        await state.set_state(OrderTicketFSM.text)
        await state.update_data(ordtk_trk=trk)
        await cb.message.answer(
            f"📞 <b>پیگیریِ سفارش</b> <code>{trk}</code>\n\n"
            "پیام یا سؤالت درباره‌یِ این سفارش را بنویس و بفرست تا همراهِ مشخصاتِ سفارش برایِ پشتیبانی برود:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="❌ انصراف", callback_data="ordtk:cancel", style=ButtonStyle.DANGER)]]))
        return
    fake_msg = type("M", (), {"from_user": cb.from_user, "answer": cb.message.answer})()
    await state.update_data(ticket_category="order", ticket_subject=f"پیگیریِ سفارش {trk}")
    await _save_new_ticket(fake_msg, state, bot, text=await build_order_ticket_text(row.id, trk))


@smm_router.callback_query(F.data == "ordtk:cancel")
async def order_ticket_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("انصراف شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("ordtk: پاک‌کردنِ دکمهٔ انصراف ممکن نشد (بی‌اهمیت)")


@smm_router.message(OrderTicketFSM.text, F.text)
async def order_ticket_text_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    from tenant.shared.support_ticket import _save_new_ticket
    row = await get_bot_row(bot)
    data = await state.get_data()
    trk = data.get("ordtk_trk") or ""
    if not row or not trk:
        await state.clear()
        return
    user_text = (msg.text or "").strip()
    if len(user_text) > 1500:
        await msg.answer("پیامت خیلی بلند است (حداکثر ۱۵۰۰ کاراکتر). کوتاه‌ترش کن:")
        return
    await state.update_data(ticket_category="order", ticket_subject=f"پیگیریِ سفارش {trk}")
    await _save_new_ticket(msg, state, bot, text=await build_order_ticket_text(row.id, trk, user_text))


@smm_router.callback_query(F.data.startswith("gift:pay:"))
async def gift_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, vsd_clear, VsdError   # PATCH_VS_DISCOUNT_V1
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await _vp.wallet_pay_allowed(row.id):
        await cb.answer("❌ پرداخت از کیف‌پول در این ربات غیرفعال است.", show_alert=True)
        return
    parts = cb.data.split(":")
    gift_id, dest_id_str = parts[2], parts[3]
    qty = max(1, min(int(parts[4]), _GIFT_MAX_QTY)) if len(parts) > 4 else 1
    dest_id = int(dest_id_str)
    via = parts[5] if len(parts) > 5 and parts[5] in ("b", "s") else ""   # PATCH_GIFT_USER_CHOICE_V1
    comment, anon, scope = _gift_opts_from(await state.get_data(), gift_id, dest_id, qty)   # PATCH_GIFT_COMMENT_V1
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id,
            TelegramGiftConfig.active == True))  # noqa: E712
    if not g:
        await cb.answer("این گیفت دیگر در دسترس نیست.", show_alert=True)
        return
    # PATCH_GIFT_ANON_SCOPE_V1 — ناشناس فقط در مسیرِ دستی (اکانتِ پشتیبانی)؛ در ارسالِ خودکار نادیده گرفته می‌شود
    if anon and not (g.is_manual or via == "s"
                     or (await _get_setting(row.id, "gift_manual_delivery", "0")) == "1"):
        anon = False
    if g.is_manual:
        qty = 1  # === PATCH_GIFT_QUANTITY_V1 === گیفتِ سفارشی همیشه ۱ است
        comment = ""
        scope = 0
    unit_price, item_label = await _gift_price_and_label(g, row.id)
    price = unit_price * qty
    qty_label = f"{item_label} × {qty}" if qty > 1 else item_label
    # === PATCH_GLOBAL_MANUAL_DELIVERY_V1 ===
    manual_mode = await _get_setting(row.id, "gift_manual_delivery", "0") == "1"
    if via == "s":
        manual_mode = True   # مشتری «اکانتِ پشتیبانی» را انتخاب کرده → تحویلِ دستی

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        _note_extra = ""
        # === PATCH_VS_DISCOUNT_V1 === کدِ تخفیفِ واردشده بر تخفیفِ اولین خرید اولویت دارد (ترکیب نمی‌شوند)
        _dc_id, _dc_amt = None, 0
        _dc_code = vsd_pick(await state.get_data(), cb.message.message_id, "gift")
        if _dc_code:
            try:
                price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_dc_code, kind="gift", price=int(price))
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
            _note_extra = f" (کد تخفیف −{_dc_amt:,})"
        else:
            from tenant.gateways.discount_engine import first_purchase_discount
            _fd = await first_purchase_discount(
                bot_id=row.id, telegram_id=cb.from_user.id, price=int(price), session=s)
            if _fd.ok and _fd.discount > 0:
                _note_extra = f" (تخفیف اولین خرید −{_fd.discount:,})"
                price = _fd.final_price
        if float(b.balance) < price:
            await s.rollback()        # قفلِ ردیف آزاد شود، بعد پیام برود
            await cb.answer("❌ موجودی کافی نیست.")
            await _vp.insufficient_notice(cb.message, row.id, cb.from_user.id, int(price), cb.data)
            return
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ {qty_label}{_note_extra}"))
        # === PATCH_ORDER_TRACKING_V1 ===
        from tenant import _gen_tracking
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        # === PATCH_GIFT_DEST_LABEL_FIX_V1 ===
        # باگ: قبلاً برایِ «کاربرِ دیگر» فقط آیدیِ عددیِ خامِ dest_id ذخیره
        # می‌شد (نه یوزرنیمی که مشتری واقعاً تایپ کرده بود) — یعنی ادمین
        # برایِ گیفتِ سفارشی/NFT (که تحویلش دستی است) هیچ راهِ عملی‌ای
        # نداشت بفهمد این آیدیِ خام واقعاً متعلق به کیست. حالا یوزرنیمِ
        # واقعی از رویِ رکوردِ BotUser خوانده می‌شود.
        # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
        # درخواستِ ادمین: زیرِ آیدیِ عددی، یوزرنیم هم به‌صورتِ خط جدا نشان
        # داده شود — به‌جایِ تکرارِ این منطق، از همان هلپرِ مشترک استفاده
        # می‌کنیم تا همه‌جایِ فروش یک‌دست باشد.
        dest_label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
                     else await _resolve_dest_label(row.id, dest_id))
        # با تخفیفِ اولین خرید، «هر عدد» واقعاً price/qty پرداخت شده؛ همان ذخیره می‌شود تا
        # بازگشتِ وجه (price_toman × عددهای ارسال‌نشده) و گزارشِ خرید از پرداختِ واقعی بیشتر نشود
        _paid_unit = unit_price if price >= unit_price * qty else int(price) // max(1, qty)
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=gift_id,
                          gift_emoji=g.emoji, star_count=g.star_count, price_toman=_paid_unit,
                          quantity=qty, dest_telegram_id=dest_id, dest_label=dest_label,
                          status="pending", pay_method="wallet", tracking_code=trk,
                          deliver_via={"b": "bot", "s": "support"}.get(via, ""),
                          comment=(comment or None), anonymous=bool(anon),
                          comment_scope=int(scope or 0),
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(order)
        await s.commit()
        order_id = order.id
    await vsd_clear(state)   # PATCH_VS_DISCOUNT_V1

    # === PATCH_MANUAL_GIFT_V1 / PATCH_GLOBAL_MANUAL_DELIVERY_V1 ===
    # برایِ گیفتِ سفارشی، gift_id یک شناسهٔ ساختگیِ ماست، نه یک گیفتِ
    # واقعیِ تلگرام — پس هیچ فراخوانیِ sendGiftای معنا ندارد. یا اگر
    # حالتِ «تحویلِ دستیِ همگانی» روشن باشد، حتی گیفتِ رسمی/استارزی هم
    # همین مسیر را می‌رود. پرداخت که انجام شد، سفارش مستقیم می‌رود برایِ
    # تحویلِ دستیِ ادمین (که با اکانتِ شخصیِ خودش گیفت را واقعاً منتقل
    # می‌کند).
    if g.is_manual or manual_mode:
        async with Session() as s:
            o = await s.get(GiftOrder, order_id)
            if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
                o = None
            o.status = "awaiting_admin"
            await s.commit()
        await cb.answer("✅ پرداخت شد")
        _t1, _k1 = await _with_sfirst(
            row.id,
            f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nبرایِ تحویلِ دستیِ ادمین ارسال شد.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>",
            await _gift_support_kb(trk))
        await cb.message.answer(_t1, parse_mode="HTML", reply_markup=_k1)
        from tenant import _notify_admins
        # === PATCH_NFT_LINK_IN_NOTIFY_V1 ===
        # قبلاً فقط اسمِ گیفت در پیامِ اطلاع بود، نه خودِ لینک — اگر ادمین
        # چند گیفتِ سفارشیِ مختلف داشت، باید جدا می‌رفت پیدا می‌کرد کدام
        # لینک مالِ کدام سفارش است. حالا لینک مستقیم همین‌جا هست.
        link_line = f"\n🔗 لینک: {g.manual_link}" if g.manual_link else ""
        qty_line = f"\n🔢 تعداد: {qty}" if qty > 1 else ""
        # === PATCH_WALLET_GIFT_ADMIN_BUTTONS_V1 ===
        # باگ: سفارشِ گیفتِ پرداخت‌شده با کیف‌پول هیچ دکمهٔ «✅ انجام شد /
        # ❌ رد» در پیامِ اطلاعِ ادمین نداشت — ادمین مجبور بود جدا برود توی
        # «🧾 سفارش‌های گیفت» بگردد. حالا از همان دکمه‌هایِ کارت‌به‌کارت
        # (gift:cardok / gift:cardno) استفاده می‌کنیم؛ آن هندلرها مستقل از
        # روشِ پرداخت کار می‌کنند و gift_cardpay_reject حالا برایِ سفارش‌هایِ
        # کیف‌پولی وجه را هم برمی‌گرداند.
        admin_kb = await _gift_admin_kb(row.id, order_id, gift_id)   # PATCH_TGTOOLS_GIFT_V1
        await _notify_manual_order(
            bot, row,
            f"💰 سفارشِ گیفت (تحویلِ دستی{' — انتخابِ مشتری: اکانتِ پشتیبانی' if via == 's' else ''}، پرداختِ کیف‌پول) — #{await order_no(GiftOrder, order_id)}\n"
            f"{item_label}{qty_line}\n💰 {price:,} تومان\n🎯 مقصد: {order.dest_label}{link_line}\n"
            + (f"💬 کامنتِ مشتری: {_html_escape(comment)}"
               + (f" (فقط گیفتِ شمارهٔ {scope})" if (scope and qty > 1) else "") + "\n" if comment else "")
            + ("🕶 <b>ناشناس:</b> هنگامِ ارسال، گزینهٔ مخفی‌کردنِ نامِ فرستنده را بزن!\n" if anon else "") +
            f"🔖 کد: {trk}\n\nاین گیفت را باید خودت دستی برایِ خریدار بفرستی، "
            f"بعد «✅ انجام شد» را بزن.", admin_kb)
        # === PATCH_TGTOOLS_GIFT_V1 === گیفتِ کاتالوگی + «گیفتِ خودکارِ TGTools» روشن ⇒ همین حالا خودکار می‌خرد
        if not g.is_manual and await _auto_gift_on(row.id):     # PATCH_ACC_GIFT_V1
            _gift_auto_spawn(bot, row, order_id)
        return

    # === PATCH_GIFT_QUANTITY_V1 ===
    # ارسالِ خودکار: هیچ APIای برایِ فرستادنِ «N عدد یک‌جا» وجود ندارد، پس
    # به تعدادِ qty، sendGift را پشتِ‌سرِهم صدا می‌زنیم. اگر وسطِ کار
    # موجودیِ استارزِ ربات تمام شود، همان تعدادی که موفق شده واقعاً
    # فرستاده شده و فقط بابتِ باقی‌مانده وجه برمی‌گردد — نه کلِ مبلغ.
    await _delete_shop_panel(cb)
    await cb.answer("در حالِ ارسالِ گیفت…")
    sent_count = 0
    last_err: Exception | None = None
    for _i in range(qty):
        try:
            _t = _gift_comment_for(comment, scope, _i)
            await bot.send_gift(gift_id=gift_id, user_id=dest_id, **({"text": _t} if _t else {}))
            sent_count += 1
        except Exception as e:
            last_err = e
            break
    # === PATCH_GIFT_USER_CHOICE_V1 ===
    # مشتری «ربات» را انتخاب کرده و موجودیِ استارزِ ربات کم است: وجه برنمی‌گردد؛ سفارش منتظرِ
    # شارژِ موجودی می‌ماند و ادمین باخبر می‌شود (بعد از شارژ «🔄 ارسال» را می‌زند).
    if sent_count < qty and via == "b" and _is_low_stars_err(last_err):
        async with Session() as s:
            oh = await s.get(GiftOrder, order_id)
            if oh is not None and await _is_foreign(oh, bot):   # PATCH_TENANT_SCOPE_V1
                oh = None
            oh.status = "awaiting_stars"
            oh.sent_qty = sent_count
            oh.fail_reason = str(last_err)
            await s.commit()
        await _gift_hold_notify(bot, row, order_id, cb.message, sent_count, qty, trk)
        return
    if sent_count < qty:
        refund = _paid_unit * (qty - sent_count)   # PATCH_VS_DISCOUNT_V1 — مبلغِ واقعاً پرداخت‌شده (با کد/تخفیف)
        async with Session() as s:
            b2 = await s.get(BotUser, buyer.id)
            b2.balance = float(b2.balance) + refund
            s.add(WalletTxn(bot_id=row.id, user_id=b2.id, amount=refund, kind="charge",
                            note="بازگشتِ وجهِ گیفتِ ناموفق"))
            o2 = await s.get(GiftOrder, order_id)
            if o2 is not None and await _is_foreign(o2, bot):   # PATCH_TENANT_SCOPE_V1
                o2 = None
            o2.status = "failed" if sent_count == 0 else "sent"
            o2.fail_reason = str(last_err)
            o2.quantity = sent_count
            await s.commit()
        sent_line = f"از {qty} عدد، {sent_count} عدد فرستاده شد.\n" if sent_count else ""
        await cb.message.answer(
            f"❌ متاسفانه ارسالِ گیفت ناموفق بود (احتمالاً موجودیِ استارزِ ربات کافی نیست).\n"
            f"{sent_line}مبلغِ {refund:,} تومانِ باقی‌مانده به کیف‌پولت برگشت.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>\n\n🙏 لطفاً کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", parse_mode="HTML",
            reply_markup=await _gift_support_kb(trk))
        from tenant import report_user_error
        await report_user_error(bot, row.id, "ارسالِ گیفت", last_err, cb.from_user.id)
        logger.exception("ارسالِ گیفتِ تلگرام ناموفق بود")
        return

    async with Session() as s:
        o3 = await s.get(GiftOrder, order_id)
        if o3 is not None and await _is_foreign(o3, bot):   # PATCH_TENANT_SCOPE_V1
            o3 = None
        o3.status = "sent"
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await cb.message.answer(f"✅ گیفت با موفقیت ارسال شد!\n{price:,} تومان از کیف‌پولت کسر شد.\n"
                            f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                            reply_markup=await _gift_support_kb(trk))


# === PATCH_GIFT_USER_CHOICE_V1 ===
def _is_low_stars_err(e) -> bool:
    t = str(e or "").upper()
    return "BALANCE_TOO_LOW" in t or "NOT ENOUGH" in t


async def _gift_hold_notify(bot: Bot, row, order_id: int, user_msg, sent_count: int, qty: int, trk: str,
                            to_admin_only: bool = False) -> None:
    """سفارش منتظرِ موجودیِ استارزِ ربات: به مشتری و ادمین خبر می‌دهد."""
    from tenant import _notify_admins
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        item = f"{o.gift_emoji or '🎁'} {o.star_count}⭐ × {qty}" if o else "گیفت"
        dest = o.dest_label if o else ""
        need = (o.star_count * (qty - sent_count)) if o else 0
    if not to_admin_only and user_msg is not None:
        part = f"از {qty} عدد، {sent_count} عدد فرستاده شد. " if sent_count else ""
        try:
            await user_msg.answer(
                f"⏳ سفارشت ثبت شد؛ {part}به‌محضِ آماده‌شدنِ موجودیِ ربات، گیفت ارسال می‌شود.\n"
                f"مبلغ کم نشده‌ای برنمی‌گردد و لازم نیست دوباره سفارش بدهی.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("[gift] اطلاعِ مشتری از انتظارِ موجودی ناموفق")
    # PATCH_MANUAL_ORDER_CHANNEL_V1 — در کانال، دکمهٔ «افزایشِ موجودی» (صفحهٔ مدیریتی) نمی‌آید
    from tenant import manual_order_channel_active
    _in_channel = await manual_order_channel_active(row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=([] if _in_channel else [
        [InlineKeyboardButton(text="💰 افزایشِ موجودیِ استارزِ ربات", callback_data="w:botstars",
                              style=ButtonStyle.SUCCESS)]]) + [
        [InlineKeyboardButton(text="🔄 ارسال (بعد از شارژ)", callback_data=f"gift:retry:{order_id}",
                              style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="❌ رد و بازگشتِ وجه", callback_data=f"gift:cardno:{order_id}",
                              style=ButtonStyle.DANGER)],
    ])
    await _notify_manual_order(
        bot, row,
        f"⚠️ موجودیِ استارزِ ربات برایِ این سفارش کافی نیست — #{await order_no(GiftOrder, order_id)}\n"
        f"{item}\n🎯 مقصد: {dest}\n🔖 کد: {trk}\n"
        f"⭐ حدودِ استارزِ لازم: {need:,}\n\n"
        "مشتری «ارسال توسطِ ربات» را انتخاب کرده. موجودیِ ربات را زیاد کن و «🔄 ارسال» را بزن؛ "
        "یا سفارش را رد کن تا وجه برگردد.\n\n"
        "💡 اگر این پیام را گم کردی: «تنظیماتِ گیفت ← 🧾 سفارش‌هایِ گیفت» (بالایِ لیست) همین دکمه‌ها را دارد.", kb)


@smm_router.callback_query(F.data.startswith("gift:retry:"))
async def gift_retry_send(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        # قفل + تصاحب: دو کلیکِ هم‌زمان (مثلاً دو ادمین در کانالِ سفارشات) گیفت را دو بار نفرستند
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            return await cb.answer("پیدا نشد", show_alert=True)
        if o.status != "awaiting_stars":
            return await cb.answer("این سفارش دیگر منتظرِ موجودی نیست.", show_alert=True)
        o.status = "sending"
        gift_id, dest, qty, done = o.gift_id, o.dest_telegram_id, int(o.quantity or 1), int(o.sent_qty or 0)
        _cm = (o.comment or "")[:128]   # PATCH_GIFT_COMMENT_V1
        _sc = int(o.comment_scope or 0)
        buyer = await s.get(BotUser, o.buyer_user_id)
        buyer_tg = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        await s.commit()
    await cb.answer("در حالِ ارسال…")
    last_err = None
    sent = done
    while sent < qty:
        try:
            _t = _gift_comment_for(_cm, _sc, sent)
            await bot.send_gift(gift_id=gift_id, user_id=dest, **({"text": _t} if _t else {}))
            sent += 1
        except Exception as e:
            last_err = e
            break
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        o.sent_qty = sent
        if sent >= qty:
            o.status = "sent"
        else:
            o.status = "awaiting_stars"   # تصاحب آزاد می‌شود تا بعد از شارژ دوباره بشود زد
            o.fail_reason = str(last_err)
        await s.commit()
    if sent >= qty:
        await _notify_gift_order_trust_success(bot, row, order_id)
        await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} ارسال شد.")
        if buyer_tg:
            try:
                await bot.send_message(
                    buyer_tg, f"✅ گیفتت با موفقیت ارسال شد! 🎉\n🔖 کد پیگیری: <code>{trk}</code>",
                    parse_mode="HTML", reply_markup=await _gift_support_kb(trk))
            except Exception:
                logger.exception("[gift] اطلاعِ مشتری پس از ارسال ناموفق")
        return
    if _is_low_stars_err(last_err):
        await cb.message.answer("❌ هنوز موجودیِ استارزِ ربات کافی نیست؛ اول موجودی را زیاد کن، بعد دوباره «🔄 ارسال» را بزن.")
    else:
        await cb.message.answer(f"❌ ارسال ناموفق بود: {last_err}")


# ─────────────── پرداختِ کارت‌به‌کارتِ گیفت — PATCH_GIFT_CARD_PAYMENT_V1 ───────────────

@smm_router.callback_query(F.data.startswith("gift:cardpay:"))
async def gift_cardpay_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, get_setting, _gen_tracking, smart_cancel_kb
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, VsdError   # PATCH_VS_DISCOUNT_V1
    _disc_code = vsd_pick(await state.get_data(), cb.message.message_id, "gift")
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    gift_id, dest_id_str = parts[2], parts[3]
    qty = max(1, min(int(parts[4]), _GIFT_MAX_QTY)) if len(parts) > 4 else 1
    dest_id = int(dest_id_str)
    via = parts[5] if len(parts) > 5 and parts[5] in ("b", "s") else ""   # PATCH_GIFT_USER_CHOICE_V1
    comment, anon, scope = _gift_opts_from(await state.get_data(), gift_id, dest_id, qty)   # PATCH_GIFT_COMMENT_V1
    async with Session() as s:
        g = await s.scalar(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id,
            TelegramGiftConfig.active == True))  # noqa: E712
    if not g:
        await cb.answer("این گیفت دیگر در دسترس نیست.", show_alert=True)
        return
    if anon and not (g.is_manual or via == "s"
                     or (await _get_setting(row.id, "gift_manual_delivery", "0")) == "1"):
        anon = False   # PATCH_GIFT_ANON_SCOPE_V1
    if g.is_manual:
        comment = ""
        scope = 0
        qty = 1  # === PATCH_GIFT_QUANTITY_V1 ===
    unit_price, item_label = await _gift_price_and_label(g, row.id)
    total_price = unit_price * qty
    card_num = await get_setting(row.id, "card_number", "")
    card_holder = await get_setting(row.id, "card_holder", "")
    if not card_num:
        await cb.answer("❌ پرداختِ کارت‌به‌کارت هنوز تنظیم نشده — از کیف‌پول استفاده کن.", show_alert=True)
        return
    # === PATCH_VS_PAY_V1 === تیکِ کارت‌به‌کارتِ خرید + سقفِ احرازِ هویت *قبل* از نمایشِ شماره‌کارت
    from tenant.shared import vs_pay as _vp
    if not await _vp.card_pay_allowed(row.id):
        await cb.answer("❌ پرداختِ کارت‌به‌کارت در این ربات غیرفعال است.", show_alert=True)
        return
    _blk = await _vp.kyc_block(row.id, cb.from_user.id, int(total_price), "gift:back_shop")
    if _blk:
        await cb.answer()
        await cb.message.answer(_blk[0], reply_markup=_blk[1])
        return

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    from models import next_tracking as _next_tracking  # PATCH_SEQ_TRACKING_V1
    trk = await _next_tracking(row.id)
    async with Session() as s:
        # === PATCH_VS_DISCOUNT_V1 === قیمتِ کارت‌به‌کارت = بعد از کد؛ مصرفِ کد همین‌جا (رد → برمی‌گردد)
        _dc_id, _dc_amt = None, 0
        if _disc_code:
            try:
                total_price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_disc_code, kind="gift", price=int(total_price))
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
            unit_price = max(1, int(total_price) // max(1, qty))
        # === PATCH_GIFT_DEST_LABEL_FIX_V1 ===
        # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
        # درخواستِ ادمین: زیرِ آیدیِ عددی، یوزرنیم هم به‌صورتِ خط جدا نشان
        # داده شود — به‌جایِ تکرارِ این منطق، از همان هلپرِ مشترک استفاده
        # می‌کنیم تا همه‌جایِ فروش یک‌دست باشد.
        dest_label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
                     else await _resolve_dest_label(row.id, dest_id))
        order = GiftOrder(bot_id=row.id, buyer_user_id=buyer.id, gift_id=gift_id,
                          gift_emoji=g.emoji, star_count=g.star_count, price_toman=unit_price,
                          quantity=qty, dest_telegram_id=dest_id, dest_label=dest_label,
                          status="awaiting_receipt", pay_method="card", tracking_code=trk,
                          deliver_via={"b": "bot", "s": "support"}.get(via, ""),
                          comment=(comment or None), anonymous=bool(anon),
                          comment_scope=int(scope or 0),
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(order)
        await s.commit()
        order_id = order.id

    await state.set_state(SmmFSM.gift_receipt_photo)
    await state.update_data(gift_order_id=order_id, vsd=None, vsd_pending=None)   # PATCH_VS_DISCOUNT_V1
    await _delete_shop_panel(cb)
    await cb.answer()
    holder_line = f"\n👤 به‌نامِ: {card_holder}" if card_holder else ""
    from tenant.gateways.card_display import extra_card_block   # PATCH_CARD_BOTH_V1
    _xb = await extra_card_block(row.id)
    if _xb:
        holder_line += "\n" + _xb.rstrip("\n")
    qty_line = f"تعداد: {qty}\n" if qty > 1 else ""
    await cb.message.answer(
        f"💳 <b>پرداختِ کارت‌به‌کارت</b>\n\n"
        f"{qty_line}"
        f"مبلغ: {total_price:,} تومان\n"
        f"شمارهٔ کارت: <code>{card_num}</code>{holder_line}\n\n"
        f"🔖 کدِ پیگیریِ سفارشت: <code>{trk}</code>\n\n"
        # === PATCH_CARD_LAST4_V1 ===
        f"بعدِ واریز، عکسِ رسید را همین‌جا بفرست و در کپشنِ همان عکس، "
        f"۴ رقمِ آخرِ کارتی که ازش پرداخت کردی را بنویس (مثلاً: 1234):",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.gift_receipt_photo, F.photo)
async def gift_receipt_photo_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, _notify_admins
    data = await state.get_data()
    order_id = data.get("gift_order_id")
    row = await get_bot_row(bot)
    if not row or not order_id:
        await state.clear()
        return
    # === PATCH_CARD_LAST4_V1 ===
    # درخواستِ ادمین: برایِ پرداختِ کارت‌به‌کارت، ۴ رقمِ آخرِ کارتِ مبدأ هم
    # لازم است — از کپشنِ همان عکسِ رسید خوانده می‌شود. اگر کپشن نبود یا
    # عددِ ۴ رقمی توش پیدا نشد، سفارش را رد نمی‌کنیم؛ فقط دوباره می‌خواهیم
    # (استیت پاک نمی‌شود تا کاربر مجبور نشود از اول شروع کند).
    last4 = _extract_card_last4(msg.caption)
    if not last4:
        await msg.answer(
            "❌ در کپشنِ عکس، ۴ رقمِ آخرِ کارتی که ازش پرداخت کردی پیدا نشد.\n\n"
            "لطفاً همین عکسِ رسید را دوباره بفرست و این‌بار در کپشنِ آن، "
            "۴ رقمِ آخرِ کارت را بنویس (مثلاً: 1234).")
        return
    await state.clear()
    photo_id = msg.photo[-1].file_id
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await msg.answer("سفارش پیدا نشد.")
            return
        o.receipt_photo_id = photo_id
        o.payer_card_last4 = last4
        o.status = "awaiting_admin"
        trk = o.tracking_code
        unit_price = o.price_toman
        qty = o.quantity or 1
        total_price = unit_price * qty
        _buyer_uid = o.buyer_user_id   # PATCH_VS_PAY_V1 — برایِ مسدودسازیِ خودکارِ سطح ۱ بعد از پرداختِ واقعی
        emoji = o.gift_emoji
        stars = o.star_count
        dest_label = o.dest_label
        gift_id = o.gift_id
        _c_comment, _c_scope, _c_anon = (o.comment or ""), int(o.comment_scope or 0), bool(o.anonymous)
        _c_via = o.deliver_via or ""
        # === PATCH_NFT_LINK_IN_NOTIFY_V1 ===
        # قبلاً اینجا فرض می‌شد همیشه یک گیفتِ کاتالوگی/استارزی است — برایِ
        # گیفتِ سفارشی (لینک‌دار/NFT) نه اسمِ درست نشان داده می‌شد نه لینکش.
        item_line = f"🎁 {emoji or '🎁'} ({stars} استارز)"
        link_line = ""
        if (gift_id or "").startswith("manual"):
            g = await s.scalar(select(TelegramGiftConfig).where(
                TelegramGiftConfig.bot_id == row.id, TelegramGiftConfig.gift_id == gift_id))
            if g:
                item_line = f"🔗 {g.manual_name or 'گیفتِ سفارشی'}"
                if g.manual_link:
                    link_line = f"\n🔗 لینک: {g.manual_link}"
        await s.commit()

    # === PATCH_SUPPORT_FIRST_V1 === فقط وقتی تحویل دستی است (اکانتِ ادمین می‌فرستد)
    _manual_deliv = (str(gift_id or "").startswith("manual") or _c_anon or _c_via == "support"
                     or (await _get_setting(row.id, "gift_manual_delivery", "0")) == "1")
    _rt, _rk = (f"✅ رسید دریافت شد و برایِ تاییدِ ادمین فرستاده شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", await _gift_support_kb(trk))
    if _manual_deliv:
        _rt, _rk = await _with_sfirst(row.id, _rt, _rk)
    await msg.answer(_rt, parse_mode="HTML", reply_markup=_rk)
    from tenant.shared import kyc as _K   # PATCH_VS_PAY_V1
    await _K.after_charge(row.id, _buyer_uid)

    admin_kb = await _gift_admin_kb(row.id, order_id, gift_id)   # PATCH_TGTOOLS_GIFT_V1
    qty_line = f"🔢 تعداد: {qty}\n" if qty > 1 else ""
    await _notify_manual_order(
        bot, row,
        f"💳 رسیدِ کارت‌به‌کارتِ گیفت — #{await order_no(GiftOrder, order_id)}\n"
        f"{item_line}\n"
        f"{qty_line}"
        f"💰 {total_price:,} تومان\n"
        f"💳 ۴ رقمِ آخرِ کارتِ پرداخت‌کننده: <code>{last4}</code>\n"
        f"🎯 مقصد: {dest_label}{link_line}\n"
        + (f"💬 کامنتِ مشتری: {_html_escape(_c_comment)}"
           + (f" (فقط گیفتِ شمارهٔ {_c_scope})" if (_c_scope and qty > 1) else "") + "\n" if _c_comment else "")
        + ("🕶 <b>ناشناس:</b> هنگامِ ارسال، گزینهٔ مخفی‌کردنِ نامِ فرستنده را بزن!\n" if _c_anon else "")
        + f"🔖 کد: {trk}",
        admin_kb, photo_file_id=photo_id)


@smm_router.callback_query(F.data.startswith("gift:cardok:"))
async def gift_cardpay_approve(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
    # === PATCH_MANUAL_GIFT_V1 / PATCH_GLOBAL_MANUAL_DELIVERY_V1 ===
    # gift_id هایِ گیفتِ سفارشی همیشه با «manual» شروع می‌شوند (چون یک
    # شناسهٔ ساختگیِ داخلیِ ماست، نه گیفتِ واقعیِ تلگرام) — برایِ این‌ها
    # هیچ فراخوانیِ sendGiftای معنا ندارد. یا اگر حالتِ «تحویلِ دستیِ
    # همگانی» روشن باشد، حتی گیفتِ رسمی/استارزی هم همین مسیر را می‌رود؛
    # تاییدِ ادمین یعنی «خودم دستی فرستادم».
    manual_mode = await _get_setting(row.id, "gift_manual_delivery", "0") == "1"
    # === PATCH_TGTOOLS_GIFT_V1 === رسید تأیید شد؛ گیفتِ کاتالوگی و گیفتِ خودکارِ TGTools روشن ⇒ خودکار بخر (نه «دستی فرستادم»)
    if (not (o.gift_id or "").startswith(("manual", "premium_", "starsell_", "srx_", "tonsell_"))
            and o.deliver_via != "bot" and await _auto_gift_on(row.id)):     # PATCH_ACC_GIFT_V1
        _via_lbl = "اکانتِ پشتیبانی" if await _acc_gift_on(row.id) else "TGTools"
        await cb.answer(f"🤖 ارسالِ خودکار با {_via_lbl} شروع شد…")
        await _admin_msg_close(cb, f"🤖 سفارش #{await order_no(GiftOrder, order_id)}: ارسالِ خودکار با {_via_lbl} شروع شد؛ نتیجه اینجا اعلام می‌شود.")
        _gift_auto_spawn(bot, row, order_id)
        return
    if (o.gift_id or "").startswith("manual") or manual_mode or o.deliver_via == "support":
        async with Session() as s:
            o3 = await s.get(GiftOrder, order_id)
            if o3 is not None and await _is_foreign(o3, bot):   # PATCH_TENANT_SCOPE_V1
                o3 = None
            o3.status = "sent"
            buyer = await s.get(BotUser, o3.buyer_user_id)
            buyer_tg_id = buyer.telegram_id if buyer else None
            trk = o3.tracking_code
            await s.commit()
        await _notify_gift_order_trust_success(bot, row, order_id)
        await cb.answer("✅ ثبت شد")
        await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} انجام‌شده ثبت شد.")
        if buyer_tg_id:
            try:
                await bot.send_message(
                    buyer_tg_id,
                    f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n🎁 گیفت تلگرام برای تحویل ارسال شد.\n"
                    f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                    reply_markup=await _gift_support_kb(trk))
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return

    await cb.answer("در حالِ ارسالِ گیفت…")
    qty = o.quantity or 1
    sent_count = 0
    last_err: Exception | None = None
    for _i in range(qty):
        try:
            _t = _gift_comment_for((o.comment or "")[:128], int(o.comment_scope or 0), _i)
            await bot.send_gift(gift_id=o.gift_id, user_id=o.dest_telegram_id,
                                **({"text": _t} if _t else {}))
            sent_count += 1
        except Exception as e:
            last_err = e
            break
    # === PATCH_GIFT_USER_CHOICE_V1 === انتخابِ «ربات» + کمبودِ استارز → منتظرِ شارژ بماند
    if sent_count < qty and o.deliver_via == "bot" and _is_low_stars_err(last_err):
        async with Session() as s:
            oh = await s.get(GiftOrder, order_id)
            if oh is not None and await _is_foreign(oh, bot):   # PATCH_TENANT_SCOPE_V1
                oh = None
            oh.status = "awaiting_stars"
            oh.sent_qty = sent_count
            oh.fail_reason = str(last_err)
            trk_h = oh.tracking_code
            buyer_h = await s.get(BotUser, oh.buyer_user_id)
            buyer_h_tg = buyer_h.telegram_id if buyer_h else None
            await s.commit()
        await _admin_msg_close(cb, f"⏳ سفارش #{await order_no(GiftOrder, order_id)}: موجودیِ استارزِ ربات کم است؛ بعد از شارژ «🔄 ارسال» را بزن.")
        await _gift_hold_notify(bot, row, order_id, None, sent_count, qty, trk_h, to_admin_only=True)
        if buyer_h_tg:
            try:
                await bot.send_message(
                    buyer_h_tg,
                    f"⏳ پرداختت تأیید شد؛ گیفت به‌محضِ آماده‌شدنِ موجودیِ ربات ارسال می‌شود.\n"
                    f"🔖 کد پیگیری: <code>{trk_h}</code>", parse_mode="HTML",
                    reply_markup=await _gift_support_kb(trk_h))
            except Exception:
                logger.exception("[gift] اطلاعِ مشتری ناموفق")
        return
    if sent_count < qty:
        async with Session() as s:
            o2 = await s.get(GiftOrder, order_id)
            if o2 is not None and await _is_foreign(o2, bot):   # PATCH_TENANT_SCOPE_V1
                o2 = None
            o2.status = "failed" if sent_count == 0 else "sent"
            o2.fail_reason = str(last_err)
            o2.quantity = sent_count
            buyer2 = await s.get(BotUser, o2.buyer_user_id)
            buyer2_tg_id = buyer2.telegram_id if buyer2 else None
            await s.commit()
        sent_line = f"از {qty} عدد، {sent_count} عدد فرستاده شد. " if sent_count else ""
        await cb.message.answer(f"❌ ارسالِ گیفت ناموفق بود: {sent_line}{last_err}\nبا خریدار هماهنگ کن.")
        logger.exception("ارسالِ گیفتِ کارت‌به‌کارت ناموفق بود")
        if buyer2_tg_id:
            try:
                await bot.send_message(
                    buyer2_tg_id,
                    f"❌ متاسفانه ارسالِ گیفتت به‌طورِ کامل ناموفق بود ({sent_line}باقی‌مانده). "
                    f"پشتیبانی به‌زودی باهات هماهنگ می‌کند.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    async with Session() as s:
        o3 = await s.get(GiftOrder, order_id)
        if o3 is not None and await _is_foreign(o3, bot):   # PATCH_TENANT_SCOPE_V1
            o3 = None
        o3.status = "sent"
        buyer = await s.get(BotUser, o3.buyer_user_id)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o3.tracking_code
        price = o3.price_toman
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} انجام‌شده ثبت شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n🎁 گیفت تلگرام با موفقیت ارسال شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@smm_router.callback_query(F.data.startswith("gift:cardno:"))
async def gift_cardpay_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status not in ("awaiting_admin", "awaiting_stars"):   # PATCH_GIFT_USER_CHOICE_V1
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        _was_stars = o.status == "awaiting_stars"
        o.status = "rejected"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
        await vsd_revert_order(s, o, buyer_tg_id)
        # === PATCH_WALLET_GIFT_REJECT_REFUND_V1 ===
        # باگ: این دکمه از سفارش‌هایِ کارت‌به‌کارت می‌آمد که هیچ‌وقت کیف‌پول
        # کم نشده بود، پس رد کردن بدونِ بازگشتِ وجه درست بود. اما حالا همین
        # دکمه برایِ سفارش‌هایِ پرداخت‌شده با کیف‌پول هم استفاده می‌شود که
        # مبلغ همان لحظهٔ خرید کسر شده — رد کردنِ آن‌ها بدونِ بازگشتِ وجه
        # یعنی پول مشتری بدونِ گیفت گم می‌شود.
        refunded = o.pay_method == "wallet" or _was_stars
        if refunded and buyer:
            # === PATCH_GIFT_QUANTITY_V1 ===
            # price_toman قیمتِ «هر واحد» است؛ مبلغِ واقعاً کسرشده از
            # کیف‌پول price_toman × quantity بوده، پس بازگشتِ وجه هم باید
            # همین جمعِ کل باشد، نه فقط قیمتِ یک واحد.
            refund_amount = int(o.price_toman) * max(0, int(o.quantity or 1) - int(o.sent_qty or 0))
            buyer.balance = float(buyer.balance) + float(refund_amount)
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=refund_amount, kind="charge",
                            note="بازگشتِ وجهِ سفارشِ گیفتِ ردشده"))
        await s.commit()
    await cb.answer("سفارش رد و وجه بازگردانده شد" if refunded else "رد شد")
    await _admin_msg_close(cb, f"❌ سفارش #{await order_no(GiftOrder, order_id)} رد شد.")
    if buyer_tg_id:
        try:
            refund_line = "مبلغِ سفارش به کیف‌پولت بازگردانده شد.\n" if refunded else ""
            reason_line = "متاسفانه رسیدت تایید نشد." if not refunded else "متاسفانه سفارشت رد شد."
            await bot.send_message(
                buyer_tg_id,
                f"❌ {reason_line}\n{refund_line}🔖 کد پیگیری: <code>{trk}</code>\n\n"
                f"اگر فکر می‌کنی اشتباهی رخ داده، از دکمهٔ پشتیبانی پیام بده.",
                parse_mode="HTML", reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════════════════════════
#  مدیریتِ سریعِ شمارهٔ مجازی — PATCH_VNUM_TOGGLE_V1
# ═══════════════════════════════════════════
# دقیقاً همان جدولی که در اسکرین‌شات دیدیم: هر کشور یک ردیف، با دکمهٔ
# روشن/خاموشِ سریع — بدونِ نیاز به طیِ جادویِ کاملِ «ساختِ سرویسِ دستی»
# برایِ هرکدام. رویِ همان زیرساختِ SmmService (is_manual=True) سوار است؛
# فقط category را با یک برچسبِ ثابت مشخص می‌کنیم تا اینجا فیلترش کنیم.

_VNUM_CATEGORY = "شمارهٔ مجازیِ تلگرام"


async def _vnum_list(bot_id: int) -> list[SmmService]:
    async with Session() as s:
        return list((await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.category == _VNUM_CATEGORY
        ).order_by(SmmService.sell_rate))).all())


async def _vnum_menu_content(bot_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    per_page = 10
    items = await _vnum_list(bot_id)
    start, end = page * per_page, page * per_page + per_page
    rows = [
        [InlineKeyboardButton(text="➕ افزودنِ کشورِ تازه", callback_data="vnum:add")],
        # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===
        [InlineKeyboardButton(text="🤖 اتصالِ خودکار (SMSPool)", callback_data="vnum:auto:menu")],
    ]
    # === PATCH_VNUM_BULK_TOGGLE_V1 ===
    # گزارشِ ادمین (فدایی): «غیرفعال‌سازیِ همه و فعال‌سازیِ همه برایِ فروش» تا دونه‌دونه لازم نباشد.
    # کشورهایِ OzVinoo (پیشوندِ ozv:) از صفحهٔ خودشان مدیریت می‌شوند (فهرستِ پایدارِ غیرفعال‌شده‌ها دارند).
    if any(not (i.service_id or "").startswith("ozv:") for i in items):
        rows.append([
            InlineKeyboardButton(text="🟢 فعال‌کردنِ همه", callback_data="vnum:allask:on", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="🔴 غیرفعال‌کردنِ همه", callback_data="vnum:allask:off", style=ButtonStyle.DANGER),
        ])
    for it in items[start:end]:
        mark = "✅" if it.is_active else "❌"
        # === PATCH_VNUM_TOGGLE_V1 ===
        # sell_rate داخلاً «هر ۱۰۰۰ واحد» ذخیره می‌شود (همان قراردادِ
        # قیمت‌گذاریِ SmmService)؛ چون شماره‌هایِ مجازی همیشه تعدادشان ۱
        # است، قیمتِ واقعی = sell_rate ÷ ۱۰۰۰. قبلاً اینجا rawِ sell_rate
        # نشان داده می‌شد که هزار برابرِ قیمتِ واقعی به‌نظر می‌رسید.
        real_price = int(it.sell_rate) // 1000
        # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===
        # کشورهایِ متصل‌به‌SMSPool (خودکار) شمارهٔ واقعی/انبارِ دستی ندارند —
        # به‌جایِ ردیفِ «🔢 شماره‌ها» فقط یک نشانگرِ 🤖 نشان داده می‌شود.
        is_auto = (it.service_id or "").startswith("sp:")
        if is_auto:
            rows.append([
                InlineKeyboardButton(text=f"{mark} 🤖 {it.name} — {real_price:,} تومان (خودکار)",
                                     callback_data=f"vnum:tog:{it.id}:{page}"),
            ])
            continue
        # === PATCH_VNUM_REAL_NUMBERS_V1 ===
        # قبلاً هر کشور فقط یک ردیفِ قیمت بود، بدونِ خودِ شماره‌هایِ واقعی.
        # حالا کنارِ همان دکمهٔ موجود/ناموجودکردنِ کلیِ کشور، یک دکمهٔ
        # «🔢 شماره‌ها» هم هست که به فهرستِ خودِ شماره‌هایِ واقعیِ آن کشور
        # می‌رود — همان‌جا اضافه/حذف و تعدادِ موجود/فروخته‌شده دیده می‌شود.
        async with Session() as s:
            n_avail = await s.scalar(select(func.count()).select_from(VnumNumber).where(
                VnumNumber.country_service_pk == it.id, VnumNumber.status == "available"))
            n_sold = await s.scalar(select(func.count()).select_from(VnumNumber).where(
                VnumNumber.country_service_pk == it.id, VnumNumber.status == "sold"))
        rows.append([
            InlineKeyboardButton(text=f"{mark} {it.name} — {real_price:,} تومان",
                                 callback_data=f"vnum:tog:{it.id}:{page}"),
        ])
        rows.append([
            InlineKeyboardButton(text=f"🔢 شماره‌ها ({n_avail or 0} موجود، {n_sold or 0} فروخته)",
                                 callback_data=f"vnum:nums:{it.id}:0"),
        ])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"vnum:menu:{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"vnum:menu:{page+1}"))
    if nav:
        rows.append(nav)
    # === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
    # درخواستِ ادمین: مثلِ گیفت/پرمیوم، یک حالتِ تحویلِ دستی برایِ شماره‌
    # مجازی هم لازم بود — تا وقتی برایِ یک کشور هیچ شماره‌ای از قبل ذخیره
    # نکرده (انبار خالی است)، مشتری همچنان بتواند بخرد؛ سفارش می‌رود پیشِ
    # ادمین و خودش دستی شماره را (مثلاً از یک سیم‌کارت/اپِ واقعی) به مشتری
    # می‌فرستد، به‌جایِ اینکه فروشگاه بگوید «موجود نیست».
    manual_mode = await _get_setting(bot_id, "vnum_manual_delivery", "0") == "1"
    manual_toggle_text = ("🟢 تحویلِ دستی وقتِ نبودِ انبار: روشن" if manual_mode
                          else "⚪️ تحویلِ دستی وقتِ نبودِ انبار: خاموش")
    rows.append([InlineKeyboardButton(text=manual_toggle_text, callback_data="vnum:manualdelivery:toggle",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)])
    text = (
        "📱 <b>مدیریتِ شمارهٔ مجازیِ تلگرام</b>\n\n"
        + (f"{len(items)} کشور ثبت شده — روی هرکدام بزن تا موجود/ناموجودش کنی:\n"
           "✅ یعنی موجود و قابلِ‌خرید، ❌ یعنی فعلاً ناموجود (نمایش داده "
           "می‌شود ولی خریدش بسته است)."
           if items else
           "هنوز هیچ کشوری اضافه نکرده‌ای. با «➕ افزودنِ کشورِ تازه» شروع کن.")
    )
    if manual_mode:
        text += ("\n\n🖐 <b>تحویلِ دستیِ وقتِ نبودِ انبار روشن است</b> — اگر برایِ یک "
                "کشور هیچ شماره‌ای در انبار نباشد، مشتری بازهم می‌تواند بخرد؛ "
                "سفارش می‌آید پیشِ تو و باید خودت دستی شماره را برایِ او بفرستی.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("vnum:menu:"))
async def vnum_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    text, kb = await _vnum_menu_content(row.id, page)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
@smm_router.callback_query(F.data == "vnum:manualdelivery:toggle")
async def vnum_manual_delivery_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _get_setting(row.id, "vnum_manual_delivery", "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting(row.id, "vnum_manual_delivery", new_val)
    await cb.answer("✅ روشن شد" if new_val == "1" else "✅ خاموش شد")
    text, kb = await _vnum_menu_content(row.id, 0)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_VNUM_BULK_TOGGLE_V1 ===
def _vnum_bulk_targets(items):
    return [i for i in items if not (i.service_id or "").startswith("ozv:")]


@smm_router.callback_query(F.data.startswith("vnum:allask:"))
async def vnum_all_ask(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    mode = cb.data.split(":")[2]
    if mode not in ("on", "off"):
        return await cb.answer("نامعتبر", show_alert=True)
    targets = _vnum_bulk_targets(await _vnum_list(row.id))
    if not targets:
        return await cb.answer("کشوری برایِ تغییر نیست.", show_alert=True)
    n_on = sum(1 for i in targets if i.is_active)
    if mode == "off":
        text = (f"🔴 <b>غیرفعال‌کردنِ همهٔ {len(targets)} کشور</b>\n\n"
                f"الان {n_on} کشور فعال است؛ همه «ناموجود» می‌شوند (نمایش داده می‌شوند ولی خریدشان بسته است). "
                "بعد می‌توانی فقط کشورِ دلخواه را دوباره روشن کنی.")
    else:
        text = (f"🟢 <b>فعال‌کردنِ همهٔ {len(targets)} کشور</b>\n\n"
                f"الان {len(targets) - n_on} کشور غیرفعال است؛ همه «موجود و قابلِ‌خرید» می‌شوند. "
                "اگر کشورِ دستی‌ای انبارِ خالی دارد، مشتری در خریدِ آن به‌جایِ شماره با «ناموجود» یا "
                "(اگر «تحویلِ دستی» روشن باشد) سفارشِ دستی روبه‌رو می‌شود.")
    text += "\n\nℹ️ کشورهایِ OzVinoo (اگر داری) جدا و از صفحهٔ خودشان مدیریت می‌شوند."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، انجام بده", callback_data=f"vnum:allgo:{mode}",
                              style=ButtonStyle.SUCCESS if mode == "on" else ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ انصراف", callback_data="vnum:menu:0", style=ButtonStyle.PRIMARY)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("vnum:allgo:"))
async def vnum_all_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    mode = cb.data.split(":")[2]
    if mode not in ("on", "off"):
        return await cb.answer("نامعتبر", show_alert=True)
    ids = [i.id for i in _vnum_bulk_targets(await _vnum_list(row.id))]
    if ids:
        async with Session() as s:
            await s.execute(update(SmmService).where(
                SmmService.bot_id == row.id, SmmService.id.in_(ids)
            ).values(is_active=(mode == "on")))
            await s.commit()
    text, kb = await _vnum_menu_content(row.id, 0)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer(f"{'🟢 فعال' if mode == 'on' else '🔴 غیرفعال'} شد: {len(ids)} کشور")


@smm_router.callback_query(F.data.startswith("vnum:tog:"))
async def vnum_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, item_id, page = cb.data.split(":")
    async with Session() as s:
        it = await s.get(SmmService, int(item_id))
        if not it or it.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        it.is_active = not it.is_active
        new_state = it.is_active
        await s.commit()
    text, kb = await _vnum_menu_content(row.id, int(page))
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer("✅ موجود شد" if new_state else "❌ ناموجود شد")


@smm_router.callback_query(F.data == "vnum:add")
async def vnum_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_add_name)
    await cb.message.answer(
        "📱 اسمِ کشور را بفرست (با پرچم اگر خواستی، مثلاً «🇺🇸 آمریکا»):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_add_name, F.text)
async def vnum_add_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.update_data(vnum_name=msg.text.strip())
    await state.set_state(SmmFSM.vnum_add_price)
    from tenant import smart_cancel_kb
    await msg.answer("💵 قیمتِ این شماره چند تومان است؟ (فقط عدد)",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.vnum_add_price, F.text)
async def vnum_add_price_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    price = fa_to_int(msg.text)
    if not price or price <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    name = data.get("vnum_name", "").strip()
    if not name:
        await msg.answer("مشکلی پیش آمد؛ دوباره از منو امتحان کن.")
        return
    import time as _t
    sid = f"man{int(_t.time()*1000)}"
    async with Session() as s:
        s.add(SmmService(
            bot_id=row.id, service_id=sid, name=f"📱 {name}", category=_VNUM_CATEGORY,
            panel_rate=0, sell_rate=price * 1000,  # قیمت‌گذاریِ داخلی هر ۱۰۰۰ است؛ تعداد همیشه ۱
            min_qty=1, max_qty=1, is_active=True, is_manual=True))
        await s.commit()
    await msg.answer(f"✅ «{name}» با قیمتِ {price:,} تومان اضافه شد و موجود است.")
    cb_fake = type("CB", (), {
        "data": "vnum:menu:0", "from_user": msg.from_user,
        "message": msg, "answer": (lambda *a, **k: _vnum_noop()),
    })()
    await vnum_menu(cb_fake, bot)


# ─────────────── مدیریتِ خودِ شماره‌هایِ واقعی — PATCH_VNUM_REAL_NUMBERS_V1 ───────────────

async def _vnum_numbers_content(bot_id: int, country_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    per_page = 15
    async with Session() as s:
        country = await s.get(SmmService, country_id)
        if not country or country.bot_id != bot_id:
            return "کشور پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vnum:menu:0", style=ButtonStyle.DANGER)]])
        nums = (await s.scalars(select(VnumNumber).where(
            VnumNumber.country_service_pk == country_id).order_by(VnumNumber.id))).all()
    start, end = page * per_page, page * per_page + per_page
    rows = [[InlineKeyboardButton(text="➕ افزودنِ شماره (چندتا هم می‌شود، هرکدام یک خط)",
                                  callback_data=f"vnum:numadd:{country_id}")]]
    for n in nums[start:end]:
        mark = "🟢" if n.status == "available" else "🔴"
        label = f"{mark} {n.phone_number}" + ("" if n.status == "available" else " (فروخته‌شده)")
        row_btns = [InlineKeyboardButton(text=label, callback_data="noop")]
        if n.status == "available":
            row_btns.append(InlineKeyboardButton(text="🗑", callback_data=f"vnum:numdel:{n.id}:{country_id}:{page}",
                                                 style=ButtonStyle.DANGER))
        rows.append(row_btns)
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"vnum:nums:{country_id}:{page-1}"))
    if end < len(nums):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"vnum:nums:{country_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به فهرستِ کشورها", callback_data="vnum:menu:0",
                                      style=ButtonStyle.DANGER)])
    n_avail = sum(1 for n in nums if n.status == "available")
    n_sold = len(nums) - n_avail
    text = (f"🔢 <b>شماره‌هایِ {country.name}</b>\n\n"
           f"🟢 موجود: {n_avail}  •  🔴 فروخته‌شده: {n_sold}\n\n"
           + ("روی 🗑 بزن تا یک شمارهٔ اشتباه را حذف کنی (فقط شماره‌هایِ هنوز موجود قابلِ‌حذف‌اند)."
              if nums else "هنوز هیچ شماره‌ای برایِ این کشور اضافه نکرده‌ای."))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data.startswith("vnum:nums:"))
async def vnum_numbers_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, country_id, page = cb.data.split(":")
    text, kb = await _vnum_numbers_content(row.id, int(country_id), int(page))
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("vnum:numadd:"))
async def vnum_number_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    country_id = int(cb.data.split(":")[2])
    async with Session() as s:
        country = await s.get(SmmService, country_id)
    if not country or country.bot_id != row.id:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_number_add)
    await state.update_data(vnum_country_id=country_id)
    # === PATCH_VNUM_DOUBLE_MOBILE_EMOJI_FIX_V1 ===
    # country.name از قبل «📱 » دارد (رجوع کن به vnum_add_price_save) —
    # پیشوندِ سختِ اینجا حذف شد که دو تا 📱 پشتِ‌سرِهم نشه.
    await cb.message.answer(
        f"شمارهٔ واقعی برایِ «{country.name}» را بفرست.\n"
        "اگر چندتا داری، هرکدام را در یک خطِ جدا بفرست — همه یک‌جا اضافه می‌شوند.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_number_add, F.text)
async def vnum_number_add_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    country_id = data.get("vnum_country_id")
    async with Session() as s:
        country = await s.get(SmmService, country_id) if country_id else None
    if not country or country.bot_id != row.id:
        await msg.answer("مشکلی پیش آمد؛ دوباره از منو امتحان کن.")
        return
    lines = [ln.strip() for ln in msg.text.splitlines() if ln.strip()]
    if not lines:
        await msg.answer("چیزی دریافت نشد. دوباره امتحان کن.")
        return
    async with Session() as s:
        for ln in lines:
            s.add(VnumNumber(bot_id=row.id, country_service_pk=country_id, phone_number=ln,
                             status="available"))
        await s.commit()
    await msg.answer(f"✅ {len(lines)} شماره برایِ «{country.name}» اضافه شد.")
    text, kb = await _vnum_numbers_content(row.id, country_id, 0)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("vnum:numdel:"))
async def vnum_number_delete(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, num_id, country_id, page = cb.data.split(":")
    async with Session() as s:
        n = await s.get(VnumNumber, int(num_id))
        if not n or n.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if n.status != "available":
            await cb.answer("این شماره فروخته شده — قابلِ‌حذف نیست.", show_alert=True)
            return
        await s.delete(n)
        await s.commit()
    await cb.answer("🗑 حذف شد")
    text, kb = await _vnum_numbers_content(row.id, int(country_id), int(page))
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _vnum_noop(*a, **k):
    return None


# ═══════════════════════════════════════════
#  اتصالِ خودکار به SMSPool — PATCH_VNUM_SMSPOOL_AUTO_V1
#  فروشِ خودکارِ شمارهٔ مجازی از طریقِ وب‌سرویسِ SMSPool.net: خرید و
#  دریافتِ کدِ پیامک بدونِ دخالتِ ادمین انجام می‌شود. این قابلیت کاملاً
#  جدا از انبارِ دستیِ شماره‌ها (VnumNumber) کار می‌کند — کشورهایِ
#  «خودکار» با پیشوندِ service_id == "sp:<country>:<service>" مشخص
#  می‌شوند (رجوع کن به vnum_auto_add_price_save).
# ═══════════════════════════════════════════

_VNUM_AUTO_POLL_INTERVAL = 8      # ثانیه بینِ هر استعلامِ خودکار
_VNUM_AUTO_POLL_MAX_MIN = 10      # حداکثر چند دقیقه صبر کند


async def _vnum_auto_menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    api_key = await _get_setting(bot_id, "smspool_api_key", "")
    key_status = f"✅ متصل (…{api_key[-4:]})" if api_key else "❌ هنوز تنظیم نشده"
    rows = [
        [InlineKeyboardButton(text=f"🔑 API Key: {key_status}", callback_data="vnum:auto:setkey")],
    ]
    if api_key:
        rows.append([InlineKeyboardButton(text="➕ افزودنِ کشورِ خودکار", callback_data="vnum:auto:add")])
        rows.append([InlineKeyboardButton(text="🌍 فهرستِ کدهایِ کشور", callback_data="vnum:auto:countries")])
        rows.append([InlineKeyboardButton(text="🔎 جستجویِ کدِ سرویس", callback_data="vnum:auto:svcsearch")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vnum:menu:0", style=ButtonStyle.DANGER)])
    text = (
        "🤖 <b>اتصالِ خودکار به SMSPool / HeroSMS</b>\n\n"
        "با این قابلیت، شمارهٔ مجازی به‌جایِ انبارِ دستی از سرویسِ SMSPool.net یا HeroSMS "
        "خریداری می‌شود — مشتری بلافاصله شماره را می‌گیرد و کدِ پیامک هم "
        "خودکار برایش ارسال می‌شود.\n\n"
        "۱) اول API Key حسابِ SMSPool یا HeroSMS خودت را وارد کن.\n"
        "۲) بعد با «➕ افزودنِ کشورِ خودکار» یک کشور/سرویس اضافه کن.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "vnum:auto:menu")
async def vnum_auto_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await _vnum_auto_menu_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "vnum:auto:setkey")
async def vnum_auto_setkey_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_smspool_key)
    await cb.message.answer(
        "🔑 API Key را بفرست. دو سرویس پشتیبانی می‌شود و خودکار تشخیص داده می‌شود:\n"
        "• SMSPool (smspool.net — از تنظیماتِ حساب)\n"
        "• HeroSMS (hero-sms.com — جانشینِ sms-activate؛ کلیدِ حسابِ HeroSMS)\n\n"
        "⚠️ کلیدِ سایتِ قدیمیِ sms-activate.ru کار نمی‌کند؛ باید در HeroSMS حساب بسازی.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_smspool_key, F.text)
async def vnum_auto_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    key = msg.text.strip()
    wait_msg = await msg.answer("⏳ در حالِ اعتبارسنجیِ کلید (SMSPool / HeroSMS)…")
    # === PATCH_VNUM_HEROSMS_V1 === اگر کلید با «hero:» شروع شود مستقیم HeroSMS؛ وگرنه اول SMSPool و
    # در صورتِ شکست HeroSMS (پروتکلِ SMS-Activate) امتحان می‌شود و با پیشوندِ hero: ذخیره می‌شود.
    ok, note = await smspool_api.validate(key)
    if not ok and not key.lower().startswith("hero:"):
        ok_h, note_h = await smspool_api.validate("hero:" + key)
        if ok_h:
            ok, note, key = True, note_h, "hero:" + key
    try:
        await wait_msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not ok:
        await msg.answer(f"{note}\n\nکلید ذخیره نشد — دوباره امتحان کن یا از منو خارج شو.")
        return
    await _set_setting(row.id, "smspool_api_key", key)
    await msg.answer(f"✅ کلید ذخیره شد.\n{note}")
    text, kb = await _vnum_auto_menu_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "vnum:auto:countries")
async def vnum_auto_countries(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    api_key = await _get_setting(row.id, "smspool_api_key", "")
    if not api_key:
        await cb.answer("اول API Key را تنظیم کن.", show_alert=True)
        return
    await cb.answer("⏳ در حالِ دریافتِ فهرست…")
    countries = await smspool_api.get_countries(api_key)
    if not countries:
        await cb.message.answer(
            "❌ فهرستِ کشورها از SMSPool دریافت نشد. می‌توانی همچنان با "
            "کدِ کشوریِ که از سایتِ خودِ SMSPool (صفحهٔ خرید) برداشتی، "
            "کشور را دستی اضافه کنی.")
        return
    lines = [f"{c.get('ID')} — {c.get('name', '')}" for c in countries[:80]]
    text = "🌍 <b>کدهایِ کشور در SMSPool</b> (۸۰ موردِ اول):\n\n<pre>" + "\n".join(lines) + "</pre>"
    if len(text) > 4000:
        text = text[:3990] + "\n…</pre>"
    await cb.message.answer(text, parse_mode="HTML")


@smm_router.callback_query(F.data == "vnum:auto:svcsearch")
async def vnum_auto_svcsearch_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    api_key = await _get_setting(row.id, "smspool_api_key", "")
    if not api_key:
        await cb.answer("اول API Key را تنظیم کن.", show_alert=True)
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_auto_svc_search)
    await cb.message.answer(
        "🔎 بخشی از اسمِ سرویس را بفرست (مثلاً «Telegram» یا «Discord») "
        "تا کدِ آن را از SMSPool پیدا کنم:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_auto_svc_search, F.text)
async def vnum_auto_svcsearch_run(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    api_key = await _get_setting(row.id, "smspool_api_key", "")
    if not api_key:
        await msg.answer("اول API Key را تنظیم کن.")
        return
    wait_msg = await msg.answer("⏳ در حالِ جستجو…")
    results = await smspool_api.search_services(api_key, msg.text.strip())
    try:
        await wait_msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not results:
        await msg.answer("چیزی پیدا نشد — کلیدواژهٔ دیگری امتحان کن.")
        return
    lines = [f"{r.get('ID')} — {r.get('name', '')}" for r in results]
    await msg.answer("🔎 <b>نتایج:</b>\n\n<pre>" + "\n".join(lines) + "</pre>", parse_mode="HTML")


@smm_router.callback_query(F.data == "vnum:auto:add")
async def vnum_auto_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    api_key = await _get_setting(row.id, "smspool_api_key", "")
    if not api_key:
        await cb.answer("اول API Key را تنظیم کن.", show_alert=True)
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_auto_add_name)
    await cb.message.answer(
        "📱 اسمِ نمایشیِ این کشور را بفرست (با پرچم اگر خواستی، مثلاً "
        "«🇺🇸 آمریکا (خودکار)»):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_auto_add_name, F.text)
async def vnum_auto_add_name_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    await state.update_data(vnum_auto_name=msg.text.strip())
    await state.set_state(SmmFSM.vnum_auto_add_country)
    from tenant import smart_cancel_kb
    await msg.answer(
        "🌍 کدِ کشورِ SMSPool را بفرست (عدد یا کدِ کوتاه، مثلاً 1 یا US — "
        "با «🌍 فهرستِ کدهایِ کشور» در منو می‌توانی پیدایش کنی):",
        reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.vnum_auto_add_country, F.text)
async def vnum_auto_add_country_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    country = msg.text.strip()
    if not country:
        await msg.answer("مقدارِ نامعتبر. دوباره بفرست.")
        return
    await state.update_data(vnum_auto_country=country)
    await state.set_state(SmmFSM.vnum_auto_add_service)
    from tenant import smart_cancel_kb
    await msg.answer(
        "🧩 کدِ سرویسِ SMSPool را بفرست (مثلاً نامِ سرویس مثلِ «telegram» یا "
        "شناسهٔ عددی — با «🔎 جستجویِ کدِ سرویس» در منو می‌توانی پیدایش کنی):",
        reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.vnum_auto_add_service, F.text)
async def vnum_auto_add_service_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    service = msg.text.strip()
    if not service:
        await msg.answer("مقدارِ نامعتبر. دوباره بفرست.")
        return
    await state.update_data(vnum_auto_service=service)
    await state.set_state(SmmFSM.vnum_auto_add_price)
    from tenant import smart_cancel_kb
    await msg.answer("💵 قیمتِ فروش به مشتری چند تومان باشد؟ (فقط عدد)",
                     reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.vnum_auto_add_price, F.text)
async def vnum_auto_add_price_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    price = fa_to_int(msg.text)
    if not price or price <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    name = data.get("vnum_auto_name", "").strip()
    country = data.get("vnum_auto_country", "").strip()
    service = data.get("vnum_auto_service", "").strip()
    if not (name and country and service):
        await msg.answer("مشکلی پیش آمد؛ دوباره از منو امتحان کن.")
        return
    sid = f"sp:{country}:{service}"
    async with Session() as s:
        s.add(SmmService(
            bot_id=row.id, service_id=sid, name=f"📱 {name}", category=_VNUM_CATEGORY,
            panel_rate=0, sell_rate=price * 1000,
            min_qty=1, max_qty=1, is_active=True, is_manual=False))
        await s.commit()
    await msg.answer(
        f"✅ «{name}» به‌صورتِ خودکار (SMSPool) با قیمتِ {price:,} تومان اضافه شد.")
    text, kb = await _vnum_auto_menu_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("vnum:autobuy:"))
async def vnum_auto_buy(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """خریدِ خودکارِ یک شمارهٔ مجازی از SMSPool — بلافاصله شماره تحویل
    داده می‌شود و یک تسکِ پس‌زمینه منتظرِ رسیدنِ کدِ پیامک می‌ماند."""
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _notify_admins, _purchase_alert
    from tenant.bots.virtual_service.vs_discount import vsd_pick   # PATCH_VS_DISCOUNT_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    country_id = int(cb.data.split(":")[2])
    async with Session() as s:
        country = await s.get(SmmService, country_id)
    if not country or country.bot_id != row.id or country.category != _VNUM_CATEGORY \
            or not (country.service_id or "").startswith("sp:") or not country.is_active:
        await cb.answer("این آیتم دیگر موجود نیست.", show_alert=True)
        return
    api_key = await _get_setting(row.id, "smspool_api_key", "")
    if not api_key:
        await cb.answer("این سرویس فعلاً در دسترس نیست.", show_alert=True)
        return
    _, sp_country, sp_service = country.service_id.split(":", 2)
    real_price = int(country.sell_rate) // 1000

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    # === PATCH_VS_DISCOUNT_V1 === کدِ تخفیف: اینجا فقط پیش‌چکِ مبلغ؛ مصرفِ واقعی داخلِ تراکنشِ کسرِ پول
    _dc_code = vsd_pick(await state.get_data(), cb.message.message_id, "vnum")
    _chk_price = real_price
    if _dc_code:
        from tenant.gateways.discount_engine import validate_discount
        _r = await validate_discount(bot_id=row.id, code=_dc_code, telegram_id=cb.from_user.id,
                                     price=int(real_price), product_key="vnum")
        if not _r.ok:
            await cb.answer(f"❌ {_r.reason or 'کد تخفیف دیگر معتبر نیست.'}", show_alert=True)
            return
        _chk_price = int(_r.final_price)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id)
        if float(b.balance) < _chk_price:
            await cb.answer(
                f"❌ موجودیِ کیف‌پول کافی نیست. کمبود: {_chk_price - int(b.balance):,} تومان",
                show_alert=True)
            return

    # === PATCH_VNUM_AUDIT_V1 === دو تپِ پشتِ‌سرهم = دو شماره؛ همزمان فقط یک خرید برایِ هر کاربر.
    _guard = (row.id, cb.from_user.id)
    if _guard in _VNUM_BUY_INFLIGHT:
        await cb.answer("⏳ خریدِ قبلی شما در حال انجام است.", show_alert=True)
        return
    _VNUM_BUY_INFLIGHT.add(_guard)
    try:
        await _vnum_auto_buy_locked(cb, bot, row, country, api_key, sp_country, sp_service,
                                    real_price, buyer, disc_code=_dc_code, state=state)
    finally:
        _VNUM_BUY_INFLIGHT.discard(_guard)


_VNUM_BUY_INFLIGHT: set = set()


async def _vnum_auto_buy_locked(cb, bot, row, country, api_key, sp_country, sp_service,
                                real_price, buyer, disc_code=None, state=None):
    from tenant import _purchase_alert
    from tenant.bots.virtual_service.vs_discount import vsd_charge, vsd_record, vsd_clear, VsdError   # PATCH_VS_DISCOUNT_V1
    await cb.answer("⏳ در حالِ خریدِ شماره از SMSPool…")
    result = await smspool_api.order_sms(api_key, sp_country, sp_service)
    if not result or str(result.get("success")) not in ("1", "True", "true"):
        err = str(result.get("message") or "موجودیِ این کشور/سرویس در SMSPool تمام شده.") \
            if isinstance(result, dict) else "خطا در ارتباط با SMSPool."
        await cb.message.answer(f"❌ خرید انجام نشد: {err}\nوجهی از کیف‌پولت کسر نشد — دوباره امتحان کن.")
        return

    sp_order_id = str(result.get("order_id") or "")
    phone_cc = str(result.get("cc") or "")
    phone_num = str(result.get("phonenumber") or "")
    full_phone = f"+{phone_cc}{phone_num}" if phone_cc else phone_num

    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)   # PATCH_VNUM_AUDIT_V1: قفلِ ردیف (جلوگیری از کسرِ هم‌زمان)
        # === PATCH_VS_DISCOUNT_V1 === قیمتِ واقعاً پرداخت‌شده (بعد از کد) در کسر، سفارش، پیام و بازگشتِ وجهِ حلقهٔ استعلام
        _dc_id, _dc_amt = None, 0
        if disc_code:
            try:
                real_price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=disc_code, kind="vnum", price=int(real_price))
            except VsdError as _e:
                try:
                    await smspool_api.cancel_sms(api_key, sp_order_id)
                except Exception:
                    logger.exception("لغوِ سفارشِ SMSPool بعد از ردِ کدِ تخفیف ناموفق بود")
                await cb.message.answer(f"❌ {_e}")
                return
        if float(b.balance) < real_price:
            # موجودی بینِ چک اول و رسیدنِ پاسخِ SMSPool ممکن است تغییر کرده
            # باشد — احتیاطاً همینجا سفارشِ SMSPool را لغو کن تا هدر نرود.
            try:
                await smspool_api.cancel_sms(api_key, sp_order_id)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await cb.message.answer("❌ موجودیِ کیف‌پول در لحظهٔ پرداخت کافی نبود.")
            return
        b.balance = float(b.balance) - real_price
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        o = SmmOrder(
            bot_id=row.id, user_telegram_id=cb.from_user.id,
            smm_service_pk=country.id, service_id=country.service_id, service_name=country.name,
            link=full_phone, quantity=1, price_toman=real_price,
            status="sent", panel_order_id=sp_order_id, panel_status="pending",
            tracking_code=trk,
            note=f"خریدِ خودکارِ شمارهٔ مجازی از SMSPool — {country.name}",
            discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(o)
        if _dc_id:
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-real_price,
                        kind="smm_order", note=f"خریدِ شمارهٔ مجازی (خودکار): {country.name}"))
        await s.commit()
        order_id = o.id
    if state is not None:
        await vsd_clear(state)   # PATCH_VS_DISCOUNT_V1

    check_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 دریافتِ کدِ پیامک", callback_data=f"vnum:autocheck:{order_id}",
                             style=ButtonStyle.PRIMARY),
    ]])
    await cb.message.answer(
        f"✅ <b>شماره‌ات آماده شد!</b> 🎉\n\n"
        f"🌍 کشور: {country.name}\n"
        f"📱 شماره: <code>{full_phone}</code>\n"
        f"💳 مبلغ پرداخت‌شده: {real_price:,} تومان\n"
        f"🔑 کد پیگیری: <code>{trk}</code>\n\n"
        f"⏳ حالا با همین شماره در سرویسِ موردنظرت ثبت‌نام/تأیید کن — کدِ "
        f"پیامک به‌محضِ رسیدن خودکار برایت فرستاده می‌شود؛ اگر خواستی "
        f"زودتر چک کنی، دکمهٔ زیر را بزن.",
        reply_markup=check_kb, parse_mode="HTML")

    try:
        await _purchase_alert(
            bot, row, buyer=buyer, section="خدمات مجازی تلگرام",
            detail=f"📱 شمارهٔ مجازی (خودکار) — {country.name}",
            amount=real_price, order_id=order_id, product_id=country.id,
            tracking_code=trk, count=1)
    except Exception:
        logger.exception("ارسال اعلان خریدِ خودکارِ شمارهٔ مجازی به کانال اعتمادسازی ناموفق بود")

    asyncio.create_task(_vnum_auto_poll_loop(
        bot, row.id, order_id, api_key, sp_order_id, cb.from_user.id, real_price, buyer.id, country.name))


async def _vnum_auto_deliver_code(bot: Bot, chat_id: int, sms_code: str, full_sms: str) -> None:
    text = f"📩 <b>کدِ پیامکت رسید!</b>\n\n🔑 کد: <code>{sms_code}</code>"
    if full_sms and full_sms != sms_code:
        text += f"\n\n📄 متنِ کامل: {full_sms}"
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("ارسالِ کدِ خودکارِ شمارهٔ مجازی به مشتری ناموفق بود")


async def _vnum_auto_apply_result(bot: Bot, bot_id: int, order_id: int, api_key: str,
                                  sp_order_id: str, buyer_chat_id: int, real_price: float,
                                  buyer_user_id: int, country_name: str, result: dict) -> str:
    """یک‌بار پاسخِ SMSPool را روی سفارش اعمال می‌کند. خروجی: وضعیتِ
    نهایی («completed»/«refunded»/«pending»/...) برایِ تصمیمِ حلقهٔ استعلام."""
    status = int(result.get("status") or 0)
    sms_code = str(result.get("sms") or "").strip()
    full_sms = str(result.get("full_sms") or "").strip()
    async with Session() as s:
        o = await s.get(SmmOrder, order_id, with_for_update=True)   # PATCH_VNUM_AUDIT_V1: جلوگیری از دو بار بازگشتِ وجه
        if not o or o.bot_id != bot_id:
            return "gone"
        if o.status == "completed":
            return "completed"  # قبلاً تحویل داده شده — کاری نکن
        if status == 3 and sms_code:
            o.status = "completed"
            o.panel_status = "completed"
            o.note = (o.note or "") + f" — کد: {sms_code}"
            await s.commit()
            await _vnum_auto_deliver_code(bot, buyer_chat_id, sms_code, full_sms)
            return "completed"
        if status in (2, 5, 6):  # expired / cancelled / refunded → بازگشتِ وجه
            if o.status not in ("refunded", "canceled"):
                b = await s.get(BotUser, buyer_user_id, with_for_update=True)
                if b:
                    b.balance = float(b.balance) + real_price
                    s.add(WalletTxn(bot_id=bot_id, user_id=buyer_user_id, amount=real_price,
                                    kind="smm_refund",
                                    note=f"بازگشتِ وجهِ شمارهٔ مجازی (SMSPool) — {country_name}"))
                o.status = "refunded"
                o.panel_status = smspool_api.STATUS_LABELS.get(status, str(status))
                from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
                await vsd_revert_order(s, o, buyer_chat_id)
                await s.commit()
                try:
                    await bot.send_message(
                        buyer_chat_id,
                        f"↩️ سفارشِ شمارهٔ «{country_name}» بدونِ دریافتِ کد لغو/منقضی شد و "
                        f"{int(real_price):,} تومان به کیف‌پولت بازگشت داده شد.")
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            return "refunded"
        o.panel_status = smspool_api.STATUS_LABELS.get(status, str(status))
        await s.commit()
        return "pending"


async def _vnum_auto_poll_loop(bot: Bot, bot_id: int, order_id: int, api_key: str,
                               sp_order_id: str, buyer_chat_id: int, real_price: float,
                               buyer_user_id: int, country_name: str) -> None:
    """در پس‌زمینه هر چند ثانیه از SMSPool می‌پرسد تا کد برسد یا زمان
    تمام شود. اگر پروسه ری‌استارت شود این تسک از دست می‌رود — به همین
    خاطر دکمهٔ «📩 دریافتِ کدِ پیامک» هم به‌صورتِ دستی همین منطق را صدا
    می‌زند تا کاربر همیشه بتواند خودش استعلام بگیرد."""
    max_tries = max(1, (_VNUM_AUTO_POLL_MAX_MIN * 60) // _VNUM_AUTO_POLL_INTERVAL)
    for _ in range(max_tries):
        await asyncio.sleep(_VNUM_AUTO_POLL_INTERVAL)
        try:
            result = await smspool_api.check_sms(api_key, sp_order_id)
            outcome = await _vnum_auto_apply_result(
                bot, bot_id, order_id, api_key, sp_order_id, buyer_chat_id,
                real_price, buyer_user_id, country_name, result)
            if outcome in ("completed", "refunded", "gone"):
                return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده در حلقهٔ استعلامِ خودکارِ شمارهٔ مجازی")
    # === PATCH_VNUM_AUTOCANCEL_V1 ===
    # ادمین: «اگر ۱۰ دقیقه کد نیامد خودکار لغو شود». قبلاً حلقه همین‌جا تمام می‌شد و سفارش
    # «در انتظار» می‌ماند. حالا یک استعلامِ آخر، بعد لغو در پنل؛ فقط اگر پنل لغو را بپذیرد
    # وجه برمی‌گردد (همان مسیرِ وضعیتِ «لغو‌شده»).
    try:
        result = await smspool_api.check_sms(api_key, sp_order_id)
        outcome = await _vnum_auto_apply_result(
            bot, bot_id, order_id, api_key, sp_order_id, buyer_chat_id,
            real_price, buyer_user_id, country_name, result)
        if outcome != "pending":
            return
        cres = await smspool_api.cancel_sms(api_key, sp_order_id)
        logger.info("[vnum-autocancel] order=%s result=%s", order_id, str(cres)[:200])
        if isinstance(cres, dict) and str(cres.get("success")) in ("1", "True", "true"):
            await _vnum_auto_apply_result(
                bot, bot_id, order_id, api_key, sp_order_id, buyer_chat_id,
                real_price, buyer_user_id, country_name, {"status": 5})
    except Exception:
        logger.exception("لغوِ خودکارِ شمارهٔ مجازی بعد از پایانِ مهلت ناموفق بود")


# === PATCH_VNUM_RECOVER_V1 ===
# تسکِ پس‌زمینهٔ هر خرید فقط در حافظهٔ همان پروسه است؛ با ری‌استارتِ سرویس (که زیاد پیش می‌آید)
# سفارش «در انتظار» می‌ماند و نه کد می‌رسید نه لغو/بازگشتِ وجه اتفاق می‌افتاد. این حلقه (در worker)
# سفارش‌هایِ SMSPool/HeroSMSِ قدیمی‌تر از مهلت را پیدا می‌کند: اگر کد رسیده تحویل می‌دهد، اگر
# منقضی/لغو شده وجه را برمی‌گرداند، و اگر هنوز بی‌کد است لغو می‌کند (فقط با تأییدِ پنل وجه برمی‌گردد).
async def vnum_recover_once() -> int:
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=_VNUM_AUTO_POLL_MAX_MIN + 2)
    async with Session() as s:
        orders = (await s.scalars(select(SmmOrder).where(
            SmmOrder.status == "sent", SmmOrder.service_id.like("sp:%"),
            SmmOrder.created_at < cutoff).order_by(SmmOrder.id.desc()).limit(100))).all()
    if not orders:
        return 0
    done = 0
    live: dict[int, Bot] = {}
    try:
        for o in orders:
            try:
                async with Session() as s:
                    brow = await s.get(BotRow, o.bot_id)
                    buyer = await s.scalar(select(BotUser).where(
                        BotUser.bot_id == o.bot_id, BotUser.telegram_id == o.user_telegram_id))
                if not brow or brow.status != "active" or not brow.token or not buyer or not o.panel_order_id:
                    continue
                api_key = await _get_setting(o.bot_id, "smspool_api_key", "")
                if not api_key:
                    continue
                bot = live.get(o.bot_id)
                if bot is None:
                    bot = live[o.bot_id] = Bot(token=brow.token)
                args = (bot, o.bot_id, o.id, api_key, o.panel_order_id, o.user_telegram_id,
                        float(o.price_toman), buyer.id, o.service_name or "")
                result = await smspool_api.check_sms(api_key, o.panel_order_id)
                outcome = await _vnum_auto_apply_result(*args, result)
                if outcome == "pending":
                    cres = await smspool_api.cancel_sms(api_key, o.panel_order_id)
                    logger.info("[vnum-recover] order=%s cancel=%s", o.id, str(cres)[:200])
                    if isinstance(cres, dict) and str(cres.get("success")) in ("1", "True", "true"):
                        await _vnum_auto_apply_result(*args, {"status": 5})
                done += 1
            except Exception:
                logger.exception("vnum_recover: خطا برایِ سفارش %s", o.id)
    finally:
        for b in live.values():
            try:
                await b.session.close()
            except Exception:
                logger.exception("vnum_recover: بستنِ session")
    return done


async def vnum_recover_loop(interval: int = 300):
    await asyncio.sleep(90)
    while True:
        try:
            await vnum_recover_once()
        except Exception:
            logger.exception("vnum_recover_loop error")
        await asyncio.sleep(interval)


@smm_router.callback_query(F.data.startswith("vnum:autocheck:"))
async def vnum_auto_check_now(cb: CallbackQuery, bot: Bot):
    """استعلامِ دستیِ کدِ پیامک — همیشه در دسترس، حتی اگر تسکِ پس‌زمینه
    (مثلاً بعدِ ری‌استارتِ سرویس) از دست رفته باشد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(SmmOrder, order_id)
        if not o or o.bot_id != row.id or o.user_telegram_id != cb.from_user.id:
            await cb.answer("این سفارش پیدا نشد.", show_alert=True)
            return
        if o.status == "completed":
            await cb.answer("✅ کد قبلاً همینجا برایت فرستاده شده بود.", show_alert=True)
            return
        if o.status in ("refunded", "canceled"):
            await cb.answer("این سفارش لغو/بازگشت‌داده شده است.", show_alert=True)
            return
        api_key = await _get_setting(row.id, "smspool_api_key", "")
        sp_order_id = o.panel_order_id
        smm_pk, price = o.smm_service_pk, o.price_toman
        buyer = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        country_name = o.service_name or ""
    if not api_key or not sp_order_id or not buyer:
        await cb.answer("در حالِ حاضر امکانِ استعلام نیست.", show_alert=True)
        return
    await cb.answer("⏳ در حالِ استعلام از SMSPool…")
    result = await smspool_api.check_sms(api_key, sp_order_id)
    outcome = await _vnum_auto_apply_result(
        bot, row.id, order_id, api_key, sp_order_id, cb.from_user.id,
        float(price), buyer.id, country_name, result)
    if outcome == "pending":
        status_int = int(result.get("status") or 0)
        label = smspool_api.STATUS_LABELS.get(status_int, "نامشخص")
        await cb.message.answer(f"⏳ هنوز کدی نرسیده — وضعیتِ فعلی: {label}\nکمی بعد دوباره امتحان کن.")


# ═══════════════════════════════════════════
#  فروشِ پرمیومِ رسمیِ تلگرام — PATCH_TELEGRAM_PREMIUM_V1
# ═══════════════════════════════════════════
# نکتهٔ فنی: برخلافِ گیفتِ معمولی که کاتالوگش پویاست، پرمیوم فقط سه حالتِ
# ثابت دارد — تلگرام خودش تعیین کرده: ۳ ماهه=۱۰۰۰ استارز، ۶ ماهه=۱۵۰۰،
# ۱۲ ماهه=۲۵۰۰ (giftPremiumSubscription این سه عدد را دقیقاً همین‌طور
# می‌خواهد، هیچ عددِ دیگری قبول نمی‌کند). عمداً از همان جدولِ GiftOrder
# استفاده می‌کنیم (gift_id="premium_3/6/12") چون ساختارش کاملاً همین
# سناریو را پوشش می‌دهد — نیازی به جدولِ جداگانه نبود.

_PREMIUM_TIERS = [(3, 1000), (6, 1500), (12, 2500)]  # (ماه, استارز)


async def _premium_settings(bot_id: int) -> tuple[float, float]:
    rate_s = await _get_setting(bot_id, "premium_toman_rate", "1000")
    # === PATCH_PREMIUM_AUTO_RATE_V1 === (گزارشِ Mike: پرمیوم برخلافِ گیفت/استارز «نرخِ خودکار» نداشت)
    # مثلِ گیفت: نرخِ بازارِ هر ⭐ بدونِ سود؛ سود همان «درصدِ سود»ِ پرمیوم است.
    if (await _premium_auto_mode(bot_id)) == "1":
        try:
            from tenant.gateways.stars_rate import star_toman
            _a = await star_toman(0, bot_id)
            if _a > 0:
                rate_s = str(_a)
        except Exception:
            logger.exception("نرخِ خودکارِ پرمیوم")
    margin_s = await _get_setting(bot_id, "premium_margin_pct", "20")
    try:
        rate = float(rate_s)
    except Exception:
        rate = 1000.0
    try:
        margin = float(margin_s)
    except Exception:
        margin = 20.0
    return rate, margin


def _premium_price(star_count: int, rate: float, margin_pct: float) -> int:
    return max(1, round(star_count * rate * (1 + margin_pct / 100)))


# === PATCH_PREMIUM_CUSTOM_PRICE_V1 ===
# قیمتِ شخصیِ تومانیِ اختیاری برایِ هر پلن — اگر تنظیم شود، به‌جایِ
# فرمولِ نرخِ استارزی استفاده می‌شود (چون فرمولِ سراسری نمی‌تواند
# هم‌زمان برایِ هر سه پلن قیمتِ «معقول» بدهد).
def _premium_custom_setting_key(months: int) -> str:
    return f"premium_custom_price_{months}"


async def _premium_get_custom_price(bot_id: int, months: int) -> int | None:
    raw = await _get_setting(bot_id, _premium_custom_setting_key(months), "0")
    try:
        val = int(float(raw))
    except Exception:
        val = 0
    return val if val > 0 else None


# === PATCH_PREMIUM_FRAGMENT_PRICE_V1 ===
# درخواستِ Mike: «قیمتِ خودکارِ پرمیوم رو بر اساسِ فرگمنت بذار». نرخِ خودکارِ قبلی پرمیوم را به‌اندازهٔ
# «استارزِ هدیهٔ پرمیوم» (۱۰۰۰/۱۵۰۰/۲۵۰۰ ⭐) حساب می‌کرد که از قیمتِ واقعیِ فرگمنت گران‌تر است. حالتِ
# «💎 فرگمنت»: قیمتِ دلاریِ هر پلن در فرگمنت × نرخِ دلار (خودکار یا دستیِ «قیمتِ پایه») × (۱+سود).
# قیمت‌هایِ دلاریِ فرگمنت قابلِ ویرایش‌اند (اگر فرگمنت تغییرشان داد).
_PREMIUM_FRAG_USD_DEFAULT = {3: 11.99, 6: 15.99, 12: 28.99}


async def _premium_auto_mode(bot_id: int) -> str:
    """"0" خاموش · "1" بر اساسِ استارز · "fragment" بر اساسِ قیمتِ فرگمنت."""
    v = (await _get_setting(bot_id, "premium_auto", "0") or "0").strip()
    return v if v in ("0", "1", "fragment") else "0"


async def _premium_frag_usd(bot_id: int) -> dict[int, float]:
    out = dict(_PREMIUM_FRAG_USD_DEFAULT)
    raw = (await _get_setting(bot_id, "premium_frag_usd", "") or "").strip()
    if raw:
        try:
            for k, v in json.loads(raw).items():
                if int(k) in out and float(v) > 0:
                    out[int(k)] = float(v)
        except Exception:
            logger.warning("premium_frag_usd نامعتبر است؛ قیمت‌هایِ پیش‌فرضِ فرگمنت", exc_info=True)
    return out


async def _premium_usd_toman(bot_id: int) -> float:
    try:
        manual = float(await _get_setting(bot_id, "stars_usd_toman", "0") or 0)
    except (TypeError, ValueError):
        manual = 0.0
    if manual > 0:
        return manual
    from tenant.gateways.stars_rate import usd_toman
    return float(await usd_toman() or 0)


async def _premium_frag_price(bot_id: int, months: int, margin_pct: float) -> int | None:
    usd_price = (await _premium_frag_usd(bot_id)).get(months)
    if not usd_price:
        return None
    usd = await _premium_usd_toman(bot_id)
    if usd <= 0:
        return None
    return max(1, int(round(usd_price * usd * (1 + float(margin_pct) / 100.0))))


async def _premium_effective_price(bot_id: int, months: int, stars: int,
                                    rate: float, margin_pct: float) -> tuple[int, bool]:
    """برمی‌گرداند: (قیمتِ نهایی، آیا قیمتِ شخصی است؟)"""
    custom = await _premium_get_custom_price(bot_id, months)
    if custom is not None:
        return custom, True
    if await _premium_auto_mode(bot_id) == "fragment":   # PATCH_PREMIUM_FRAGMENT_PRICE_V1
        _fp = await _premium_frag_price(bot_id, months, margin_pct)
        if _fp:
            return _fp, False
    # === PATCH_TGTOOLS_PREMIUM_V1 === با روشنِ‌بودنِ «پرمیومِ TGTools»، قیمت از نرخِ TONِ TGTools × نرخِ تومانیِ TON × سود
    _tp = await _tgt_premium_price(bot_id, months, margin_pct)
    if _tp:
        return _tp, False
    return _premium_price(stars, rate, margin_pct), False


async def premium_admin_content(bot_id: int, bot: "Bot | None" = None) -> tuple[str, InlineKeyboardMarkup]:
    rate, margin = await _premium_settings(bot_id)
    _pmode = await _premium_auto_mode(bot_id)   # PATCH_PREMIUM_FRAGMENT_PRICE_V1
    _pauto = _pmode != "0"
    lines = [
        "🌟 <b>فروشِ پرمیومِ رسمیِ تلگرام</b>\n",
        "همیشه فقط همین سه بسته وجود دارد (خودِ تلگرام همین سه عدد را "
        "تعیین کرده، قابلِ‌تغییر نیست):\n",
    ]
    # === PATCH_PREMIUM_MANUAL_DELIVERY_V1 ===
    manual_mode = await _get_setting(bot_id, "premium_manual_delivery", "0") == "1"
    # === PATCH_BOT_STAR_BALANCE_FILTER_V1 ===
    # باگ: در حالتِ تحویلِ دستی، اصلاً از موجودیِ استارزِ خودِ ربات استفاده
    # نمی‌شود (ادمین با اکانتِ شخصی‌اش می‌فرستد) — پس این چک/علامتِ ❌
    # نباید در آن حالت اعمال شود، وگرنه با اینکه حالتِ دستی روشن است،
    # همه‌چیز همچنان به‌خاطرِ کمبودِ استارزِ ربات ❌ نشان داده می‌شود.
    balance = await _bot_star_balance(bot) if (bot is not None and not manual_mode) else None
    for months, stars in _PREMIUM_TIERS:
        price, is_custom = await _premium_effective_price(bot_id, months, stars, rate, margin)  # PATCH_PREMIUM_CUSTOM_PRICE_V1
        mark = ""
        if balance is not None:
            mark = " ✅" if stars <= balance else " ❌ (موجودیِ ربات کافی نیست)"
        custom_tag = " 🖊️(شخصی)" if is_custom else ""  # PATCH_PREMIUM_CUSTOM_PRICE_V1
        lines.append(f"• {months} ماهه ({stars:,} استارز) — {price:,} تومان{mark}{custom_tag}")
    if balance is not None:
        lines.append(f"\n⭐ موجودیِ فعلیِ استارزِ ربات: <b>{balance:,}</b>")
    if _pmode == "fragment":   # PATCH_PREMIUM_FRAGMENT_PRICE_V1
        lines.append(f"\n💎 قیمت بر اساسِ <b>فرگمنت</b> (دلار ≈ {int(await _premium_usd_toman(bot_id)):,} تومان)"
                     f"  •  سود: <b>{margin:.0f}٪</b>\n")
    else:
        lines.append(f"\nنرخ: هر ⭐ = <b>{rate:,.0f} تومان</b>  •  سود: <b>{margin:.0f}٪</b>\n")
    lines.append(
        "🖊️ برایِ هر پلن می‌تونی یه «قیمتِ شخصی» ثابت هم بذاری که جایِ "
        "محاسبه‌یِ خودکار استفاده بشه (برایِ حذفش، عدد 0 بفرست).\n")  # PATCH_PREMIUM_CUSTOM_PRICE_V1
    manual_toggle_text = ("🟢 حالتِ تحویلِ دستی: روشن" if manual_mode
                          else "⚪️ حالتِ تحویلِ دستی: خاموش")
    if manual_mode:
        lines.append(
            "\n🖐 <b>حالتِ تحویلِ دستی روشن است</b> — سفارش‌ها بعدِ خرید مستقیم "
            "می‌روند برایِ تو؛ خودت با اکانتِ شخصی‌ات پرمیوم را هدیه می‌دهی و در "
            "پیامِ سفارش «✅ انجام شد» را می‌زنی. دیگر نیازی به موجودیِ استارزِ "
            "ربات نیست و همهٔ پلن‌ها همیشه در فروشگاه فعال‌اند.")
    else:
        lines.append("\n⚠️ این هم مثلِ گیفت از موجودیِ استارزِ خودِ ربات کم می‌شود. "
                    "پلن‌هایی که موجودیِ کافی ندارند، خودکار در فروشگاهِ مشتری هم پنهان می‌شوند.")
    custom_rows = []  # === PATCH_PREMIUM_CUSTOM_PRICE_V1 ===
    for _cm, _cs in _PREMIUM_TIERS:
        _cval = await _premium_get_custom_price(bot_id, _cm)
        if _cval is not None:
            custom_rows.append([InlineKeyboardButton(
                text=f"🖊️ قیمتِ شخصیِ {_cm} ماهه: {_cval:,} تومان (تغییر/حذف)",
                callback_data=f"prm:setcustom:{_cm}", style=ButtonStyle.PRIMARY)])
        else:
            custom_rows.append([InlineKeyboardButton(
                text=f"➕ قیمتِ شخصی برایِ {_cm} ماهه",
                callback_data=f"prm:setcustom:{_cm}")])
    rows = [
        # === PATCH_BOT_STAR_CHARGE_BUTTON_V1 ===
        [InlineKeyboardButton(text="💰 افزایشِ موجودیِ استارزِ ربات", callback_data="w:botstars",
                              style=ButtonStyle.SUCCESS)],
        # === PATCH_PREMIUM_MANUAL_DELIVERY_V1 ===
        [InlineKeyboardButton(text=manual_toggle_text, callback_data="prm:manualdelivery:toggle",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"💱 نرخ (هر ⭐ = {rate:,.0f} تومان)", callback_data="prm:setrate")],
        [InlineKeyboardButton(   # PATCH_PREMIUM_AUTO_RATE_V1 + PATCH_PREMIUM_FRAGMENT_PRICE_V1
            text={"0": "🔄 نرخ خودکار: خاموش", "1": "🔄 نرخ خودکار: ⭐ استارز ✅",
                  "fragment": "🔄 نرخ خودکار: 💎 فرگمنت ✅"}[_pmode],
            callback_data="sauto:premium",
            style=(ButtonStyle.SUCCESS if _pauto else ButtonStyle.PRIMARY))],
        *([[InlineKeyboardButton(
            text="💵 قیمتِ دلاریِ فرگمنت (" + " / ".join(
                f"{_m}م={_u:g}$" for _m, _u in (await _premium_frag_usd(bot_id)).items()) + ")",
            callback_data="prm:fragusd")]] if _pmode == "fragment" else []),
        [InlineKeyboardButton(text="⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار", callback_data="sauto:base")],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({margin:.0f}٪)", callback_data="prm:setmargin")],
        *custom_rows,  # === PATCH_PREMIUM_CUSTOM_PRICE_V1 ===
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "prm:menu")
async def prm_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await premium_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_PREMIUM_CUSTOM_PRICE_V1 ===
@smm_router.callback_query(F.data.startswith("prm:setcustom:"))
async def prm_setcustom_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    months = int(cb.data.split(":", 2)[2])
    if months not in dict(_PREMIUM_TIERS):
        await cb.answer("این پلن نامعتبر است.", show_alert=True)
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.premium_custom_price)
    await state.update_data(premium_custom_months=months)
    cur = await _premium_get_custom_price(row.id, months)
    cur_txt = f"{cur:,} تومان" if cur is not None else "تنظیم‌نشده (محاسبه‌یِ خودکار)"
    await cb.message.answer(
        f"💰 قیمتِ شخصیِ فعلیِ پلنِ {months} ماهه: {cur_txt}\n\n"
        "قیمتِ تومانیِ جدید را بفرست (فقط عدد).\n"
        "برایِ حذفِ قیمتِ شخصی و بازگشت به محاسبه‌یِ خودکار (نرخ × استارز × سود)، عدد 0 بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.premium_custom_price, F.text)
async def prm_setcustom_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    months = data.get("premium_custom_months")
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await state.clear()
    if not row or months not in dict(_PREMIUM_TIERS):
        return
    from tenant import fa_to_int
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    if val == 0:
        await _set_setting(row.id, _premium_custom_setting_key(months), "0")
        await msg.answer(f"✅ قیمتِ شخصیِ پلنِ {months} ماهه حذف شد — دوباره خودکار محاسبه می‌شود.")
    else:
        await _set_setting(row.id, _premium_custom_setting_key(months), str(val))
        await msg.answer(f"✅ قیمتِ پلنِ {months} ماهه روی {val:,} تومان ثابت شد.")
    text, kb = await premium_admin_content(row.id, bot)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_PREMIUM_MANUAL_DELIVERY_V1 ===
@smm_router.callback_query(F.data == "prm:manualdelivery:toggle")
async def prm_manual_delivery_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = await _get_setting(row.id, "premium_manual_delivery", "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting(row.id, "premium_manual_delivery", new_val)
    await cb.answer("✅ حالتِ تحویلِ دستی روشن شد" if new_val == "1" else "✅ حالتِ تحویلِ دستی خاموش شد")
    text, kb = await premium_admin_content(row.id, bot)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "prm:fragusd")
async def prm_fragusd_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """PATCH_PREMIUM_FRAGMENT_PRICE_V1 — ویرایشِ قیمتِ دلاریِ پلن‌ها در فرگمنت."""
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    cur = await _premium_frag_usd(row.id)
    await state.set_state(SmmFSM.premium_frag_usd)
    await cb.message.answer(
        "💵 قیمتِ دلاریِ پرمیوم در فرگمنت را برایِ ۳، ۶ و ۱۲ ماهه (به همین ترتیب) بفرست، با فاصله:\n"
        f"مثال: <code>{cur[3]:g} {cur[6]:g} {cur[12]:g}</code>\n\n"
        "(برایِ برگشت به پیش‌فرض: <code>0</code>)", parse_mode="HTML",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.premium_frag_usd, F.text)
async def prm_fragusd_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    raw = (msg.text or "").strip()
    t = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫,", "0123456789.."))
    if not row:
        await state.clear()
        return
    if t == "0":
        await state.clear()
        await _set_setting(row.id, "premium_frag_usd", "")
    else:
        parts = t.replace("/", " ").split()
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            vals = []
        if len(vals) != 3 or any(v <= 0 or v > 1000 for v in vals):
            if not any(ch.isdigit() for ch in t):
                await state.clear()
                raise SkipHandler
            await msg.answer("سه عدد بفرست، مثلاً: <code>11.99 15.99 28.99</code>", parse_mode="HTML")
            return
        await state.clear()
        await _set_setting(row.id, "premium_frag_usd", json.dumps({"3": vals[0], "6": vals[1], "12": vals[2]}))
    text, kb = await premium_admin_content(row.id, bot)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "prm:setrate")
async def prm_setrate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.premium_rate)
    await cb.message.answer("💱 هر یک استارز، چند تومان حساب شود؟ (فقط عدد، مثلاً 1000)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.premium_rate, F.text)
async def prm_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "premium_toman_rate", str(val))
    text, kb = await premium_admin_content(row.id, bot)
    await msg.answer("✅ نرخ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "prm:setmargin")
async def prm_setmargin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.premium_margin)
    await cb.message.answer("📈 چند درصد سود روی قیمتِ استارزی اضافه شود؟ (فقط عدد، مثلاً 20)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.premium_margin, F.text)
async def prm_setmargin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "premium_margin_pct", str(val))
    text, kb = await premium_admin_content(row.id, bot)
    await msg.answer("✅ درصدِ سود ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ─────────────── فروشگاهِ پرمیوم — طرفِ مشتری ───────────────

async def show_premium_shop_to_user(bot: Bot, target: Message, bot_id: int) -> None:
    rate, margin = await _premium_settings(bot_id)
    # === PATCH_PREMIUM_MANUAL_DELIVERY_V1 ===
    # باگ: این فیلتر همیشه بر اساسِ موجودیِ استارزِ خودِ ربات پلن‌ها را
    # پنهان می‌کرد — حتی وقتی حالتِ تحویلِ دستی روشن بود و اصلاً نیازی به
    # این موجودی نبود. نتیجه: با اینکه ادمین حالتِ دستی را روشن کرده بود،
    # فروشگاه هنوز خالی نشان داده می‌شد.
    manual_mode = await _get_setting(bot_id, "premium_manual_delivery", "0") == "1"
    balance = await _bot_star_balance(bot) if not manual_mode else None
    rows = []
    for months, stars in _PREMIUM_TIERS:
        if balance is not None and stars > balance:
            continue
        price, _is_custom = await _premium_effective_price(bot_id, months, stars, rate, margin)  # PATCH_PREMIUM_CUSTOM_PRICE_V1
        # === PATCH_PREMIUM_BUTTON_COLOR_V1 ===
        # درخواستِ ادمین: دکمه‌هایِ پلن‌هایِ پرمیوم (۳/۶/۱۲ ماهه) رنگی
        # باشند، نه خاکستریِ پیش‌فرض.
        rows.append([InlineKeyboardButton(
            text=f"🌟 {months} ماهه — {price:,} تومان",
            callback_data=f"prm:pick:{months}", style=ButtonStyle.PRIMARY)])
    from tenant import inline_back_row
    if not rows:
        br = await inline_back_row(bot_id)
        text = "🌟 فعلاً هیچ پلنِ پرمیومی موجود نیست — به‌زودی دوباره سر بزن."
        await target.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[br] if br else []))
        return
    br = await inline_back_row(bot_id)
    if br:
        rows.append(br)
    await target.answer(
        "🌟 <b>پرمیومِ تلگرام</b>\n\n"
        "یکی از پلن‌های پرمیوم را انتخاب کن — از طریقِ سایتِ رسمیِ فرگمنت "
        "و بدون نیاز به ورود به اکانتِ فرگمنت، مستقیم روی حسابِ تلگرامت "
        "فعال می‌شود:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("prm:pick:"))
async def prm_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    months = int(cb.data.split(":", 2)[2])
    if months not in dict(_PREMIUM_TIERS):
        await cb.answer("این پلن نامعتبر است.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌟 خرید برای خودم", callback_data=f"prm:dself:{months}")],
        [InlineKeyboardButton(text="👤 برایِ کاربرِ دیگر (یوزرنیم)", callback_data=f"prm:dask:{months}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="prm:back_shop", style=ButtonStyle.DANGER)],
    ])
    await cb.message.answer(
        f"🌟 پرمیومِ {months} ماهه را برایِ چه کسی می‌خری؟\n\n"
        "⚠️ برایِ «کاربرِ دیگر»، آن شخص باید حداقل یک‌بار همین ربات را "
        "استارت کرده باشد.",
        reply_markup=kb)
    await cb.answer()


@smm_router.callback_query(F.data == "prm:back_shop")
async def prm_back_shop(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await cb.answer()
    await show_premium_shop_to_user(bot, cb.message, row.id)


async def _premium_show_invoice(target: Message, bot_id: int, months: int,
                                dest_id: int, dest_label: str) -> None:
    stars = dict(_PREMIUM_TIERS)[months]
    rate, margin = await _premium_settings(bot_id)
    price, _is_custom = await _premium_effective_price(bot_id, months, stars, rate, margin)  # PATCH_PREMIUM_CUSTOM_PRICE_V1
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — تیک‌هایِ مالک + شارژ حساب
    _pay_rows = await _vp.pay_rows(bot_id, _vp.tg_of(target), int(price), f"prm:pay:{months}:{dest_id}",
                                   wallet_label="💳 پرداخت")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *_pay_rows,
        *await _vsd_rows_lazy(bot_id, "premium", price),   # PATCH_VS_DISCOUNT_V1
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="prm:back_shop", style=ButtonStyle.DANGER)],
    ])
    await target.answer(
        f"🧾 <b>فاکتورِ خرید</b>\n\n"
        f"کالا: پرمیومِ تلگرام {months} ماهه ({stars:,} استارز)\n"
        f"مقصد: {dest_label}\n"
        f"قیمت: {price:,} تومان\n\n"
        f"مبلغِ قابل‌پرداخت: {price:,} تومان" + ("" if _pay_rows else _vp.NO_METHOD_NOTE),
        reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("prm:dself:"))
async def prm_dest_self(cb: CallbackQuery, bot: Bot):
    months = int(cb.data.split(":", 2)[2])
    await cb.answer()
    await _premium_show_invoice(cb.message, (await _bot_id(bot)), months, cb.from_user.id, "خودم")


@smm_router.callback_query(F.data.startswith("prm:dask:"))
async def prm_dest_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    months = int(cb.data.split(":", 2)[2])
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.premium_dest_username)
    await state.update_data(premium_dest_months=months)
    await cb.message.answer("👤 لطفاً یوزرنیمِ مقصد را بفرست (بدونِ @):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.premium_dest_username, F.text)
async def prm_dest_username_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    data = await state.get_data()
    months = data.get("premium_dest_months")
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not months:
        return
    uname = (msg.text or "").strip().lstrip("@")
    async with Session() as s:
        target_user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.username == uname))
    if not target_user:
        await msg.answer(
            f"❌ کاربری با یوزرنیمِ @{uname} پیدا نشد.\n\n"
            "این شخص باید حداقل یک‌بار همین ربات را استارت کرده باشد.")
        return
    await _premium_show_invoice(msg, row.id, months, target_user.telegram_id, f"@{uname}")


@smm_router.callback_query(F.data.startswith("prm:pay:"))
async def prm_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, _gen_tracking
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, vsd_clear, VsdError   # PATCH_VS_DISCOUNT_V1
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if not await _vp.wallet_pay_allowed(row.id):
        await cb.answer("❌ پرداخت از کیف‌پول در این ربات غیرفعال است.", show_alert=True)
        return
    _, _, months_s, dest_id_str = cb.data.split(":")
    months = int(months_s)
    dest_id = int(dest_id_str)
    if months not in dict(_PREMIUM_TIERS):
        await cb.answer("این پلن نامعتبر است.", show_alert=True)
        return
    stars = dict(_PREMIUM_TIERS)[months]
    rate, margin = await _premium_settings(row.id)
    price, _is_custom = await _premium_effective_price(row.id, months, stars, rate, margin)  # PATCH_PREMIUM_CUSTOM_PRICE_V1

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        _note_extra = ""
        # === PATCH_VS_DISCOUNT_V1 === کدِ واردشده بر تخفیفِ اولین خرید اولویت دارد
        _dc_id, _dc_amt = None, 0
        _dc_code = vsd_pick(await state.get_data(), cb.message.message_id, "premium")
        if _dc_code:
            try:
                price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_dc_code, kind="premium", price=int(price))
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
            _note_extra = f" (کد تخفیف −{_dc_amt:,})"
        else:
            from tenant.gateways.discount_engine import first_purchase_discount
            _fd = await first_purchase_discount(
                bot_id=row.id, telegram_id=cb.from_user.id, price=int(price), session=s)
            if _fd.ok and _fd.discount > 0:
                _note_extra = f" (تخفیف اولین خرید −{_fd.discount:,})"
                price = _fd.final_price
        if float(b.balance) < price:
            await s.rollback()        # قفلِ ردیف آزاد شود، بعد پیام برود
            await cb.answer("❌ موجودی کافی نیست.")
            await _vp.insufficient_notice(cb.message, row.id, cb.from_user.id, int(price), cb.data)
            return
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ پرمیومِ تلگرام ({months} ماهه){_note_extra}"))
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        # === PATCH_GIFT_DEST_LABEL_FIX_V1 ===
        # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
        # درخواستِ ادمین: زیرِ آیدیِ عددی، یوزرنیم هم به‌صورتِ خط جدا نشان
        # داده شود — به‌جایِ تکرارِ این منطق، از همان هلپرِ مشترک استفاده
        # می‌کنیم تا همه‌جایِ فروش یک‌دست باشد.
        dest_label = (_self_dest_label(cb.from_user) if dest_id == cb.from_user.id
                     else await _resolve_dest_label(row.id, dest_id))
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=f"premium_{months}",
                          gift_emoji="🌟", star_count=stars, price_toman=price,
                          dest_telegram_id=dest_id, dest_label=dest_label,
                          status="pending", tracking_code=trk,
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(order)
        await s.commit()
        order_id = order.id
    await vsd_clear(state)   # PATCH_VS_DISCOUNT_V1

    # === PATCH_TGTOOLS_PREMIUM_V1 === خریدِ خودکار با TGTools (اگر روشن و کلید ثبت باشد)
    if await _tgt_premium_on(row.id):
        async with Session() as s:
            o = await s.get(GiftOrder, order_id)
            if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
                o = None
            o.status = "awaiting_admin"
            await s.commit()
        await cb.answer("✅ پرداخت شد")
        await cb.message.answer(
            f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nپرمیوم در حالِ فعال‌سازی است؛ تا چند دقیقهٔ دیگر خبرت می‌کنم.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
        from tenant import _notify_admins
        await _notify_admins(
            bot, row,
            f"🌟 سفارشِ پرمیوم (خریدِ خودکار با TGTools) — #{await order_no(GiftOrder, order_id)}\n"
            f"پلن: {months} ماهه\n💰 {price:,} تومان\n🎯 مقصد: {dest_label}\n🔖 کد: {trk}\n\n"
            "خریدِ خودکار شروع شد؛ نتیجه با پیامِ جداگانه می‌آید. اگر خودت می‌خواهی دستی هدیه بدهی، "
            "اول در سایتِ TGTools مطمئن شو سفارش ثبت نشده.", await _prm_admin_kb(order_id))
        _tgt_spawn_prm(bot, row, order_id)
        return

    # === PATCH_PREMIUM_MANUAL_DELIVERY_V1 ===
    manual_mode = await _get_setting(row.id, "premium_manual_delivery", "0") == "1"
    if manual_mode:
        async with Session() as s:
            o = await s.get(GiftOrder, order_id)
            if o is not None and await _is_foreign(o, bot):   # PATCH_TENANT_SCOPE_V1
                o = None
            o.status = "awaiting_admin"
            await s.commit()
        await cb.answer("✅ پرداخت شد")
        await cb.message.answer(
            f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\nبرایِ تحویلِ دستیِ ادمین ارسال شد.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
        from tenant import _notify_admins
        admin_kb = await _prm_admin_kb(order_id)   # PATCH_TGTOOLS_PREMIUM_V1 (+ دکمهٔ 🤖 TGTools اگر کلید ثبت است)
        await _notify_manual_order(
            bot, row,
            f"🌟 سفارشِ پرمیوم (تحویلِ دستی) — #{await order_no(GiftOrder, order_id)}\n"
            f"پلن: {months} ماهه ({stars:,} استارز)\n💰 {price:,} تومان\n"
            f"🎯 مقصد: {dest_label}\n🔖 کد: {trk}\n\n"
            f"این پرمیوم را باید خودت با اکانتِ شخصی‌ات هدیه بدهی، "
            f"بعد «✅ انجام شد» را بزن.", admin_kb)
        return

    await cb.answer("در حالِ فعال‌سازیِ پرمیوم…")
    try:
        await bot.gift_premium_subscription(user_id=dest_id, month_count=months, star_count=stars)
    except Exception as e:
        async with Session() as s:
            b2 = await s.get(BotUser, buyer.id)
            b2.balance = float(b2.balance) + price
            s.add(WalletTxn(bot_id=row.id, user_id=b2.id, amount=price, kind="charge",
                            note="بازگشتِ وجهِ پرمیومِ ناموفق"))
            o2 = await s.get(GiftOrder, order_id)
            if o2 is not None and await _is_foreign(o2, bot):   # PATCH_TENANT_SCOPE_V1
                o2 = None
            o2.status = "failed"
            o2.fail_reason = str(e)
            await s.commit()
        await cb.message.answer(
            f"❌ متاسفانه فعال‌سازیِ پرمیوم ناموفق بود (احتمالاً موجودیِ استارزِ ربات کافی نیست).\n"
            f"مبلغِ {price:,} تومان به کیف‌پولت برگشت.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>\n\n🙏 لطفاً کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", parse_mode="HTML")
        from tenant import report_user_error
        await report_user_error(bot, row.id, "فعال‌سازیِ پرمیوم", e, cb.from_user.id)
        logger.exception("فعال‌سازیِ پرمیومِ تلگرام ناموفق بود")
        return

    async with Session() as s:
        o3 = await s.get(GiftOrder, order_id)
        if o3 is not None and await _is_foreign(o3, bot):   # PATCH_TENANT_SCOPE_V1
            o3 = None
        o3.status = "sent"
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await cb.message.answer(
        f"✅ پرمیومِ {months} ماهه با موفقیت فعال شد!\n{price:,} تومان از کیف‌پولت کسر شد.\n"
        f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("prm:done:"))
async def prm_admin_done(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "sent"
        buyer = await s.get(BotUser, o.buyer_user_id)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        months = o.gift_id.split("_")[-1] if o.gift_id else "?"
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await cb.answer("✅ ثبت شد")
    await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} انجام‌شده ثبت شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n🌟 تلگرام پرمیومِ {months} ماهه فعال شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@smm_router.callback_query(F.data.startswith("prm:reject:"))
async def prm_admin_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "rejected"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
        await vsd_revert_order(s, o, buyer_tg_id)
        if buyer:
            buyer.balance = float(buyer.balance) + float(o.price_toman)
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=o.price_toman, kind="charge",
                            note="بازگشتِ وجهِ سفارشِ پرمیومِ ردشده"))
        await s.commit()
    await cb.answer("سفارش رد و وجه بازگردانده شد")
    await _admin_msg_close(cb, f"❌ سفارش #{await order_no(GiftOrder, order_id)} رد شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"❌ متاسفانه سفارشت رد شد.\nمبلغِ سفارش به کیف‌پولت بازگردانده شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>",
                parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════════════════════════
#  پرمیومِ خودکار با TGTools — PATCH_TGTOOLS_PREMIUM_V1
# ═══════════════════════════════════════════
# مثلِ استارز: بعد از پرداختِ کیف‌پولِ مشتری، پرمیوم با کلیدِ TGTools خریده و به یوزرنیمِ مقصد هدیه می‌شود
# (paymentMethod=ton از کیف‌پولِ TGTools). قیمتِ فروش از نرخِ TONِ TGTools × نرخِ تومانیِ TON (nft_ton_rate) × سود.
# هر خطا/نامشخص ⇒ سفارش به تحویلِ دستی برمی‌گردد و ادمین خبردار می‌شود؛ هیچ retryِ خودکارِ «خرید» نیست.
_TGT_PRICES_CACHE: dict = {"t": -1e9, "v": {}}


async def _tgt_premium_on(bot_id: int) -> bool:
    return ((await _get_setting(bot_id, "tgtools_premium", "0")) == "1"
            and bool(await _get_setting(bot_id, "tgtools_api_key", "")))


async def _tgt_premium_price(bot_id: int, months: int, margin_pct: float) -> int | None:
    """قیمتِ تومانیِ پرمیوم با نرخِ TGTools؛ None یعنی (خاموش/نرخِ TON تنظیم نشده/TGTools پاسخ نداد) ⇒ فرمولِ قبلی."""
    if not await _tgt_premium_on(bot_id):
        return None
    try:
        ton_rate = int(float(await _get_setting(bot_id, "nft_ton_rate", "0") or 0))
    except (TypeError, ValueError):
        ton_rate = 0
    if ton_rate <= 0:
        return None
    import time as _t
    from tenant.bots.virtual_service import tgtools_api as _tg
    if _t.monotonic() - _TGT_PRICES_CACHE["t"] > 60:
        try:
            _TGT_PRICES_CACHE["v"] = await _tg.get_prices()
            _TGT_PRICES_CACHE["t"] = _t.monotonic()
        except _tg.TgToolsError:
            logger.warning("tgtools: نرخِ پرمیوم گرفته نشد؛ فرمولِ قبلی استفاده شد", exc_info=True)
            if not _TGT_PRICES_CACHE["v"]:
                return None
    ton = _tg.premium_price_ton(_TGT_PRICES_CACHE["v"], months)
    if not ton:
        return None
    return max(1, int(round(ton * ton_rate * (1 + float(margin_pct) / 100.0))))


async def _prm_admin_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"prm:done:{order_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"prm:reject:{order_id}", style=ButtonStyle.DANGER),
    ]]
    try:
        async with Session() as s:
            _o = await s.get(GiftOrder, order_id)
        if _o and await _get_setting(_o.bot_id, "tgtools_api_key", ""):
            rows.append([InlineKeyboardButton(text="🤖 خرید با TGTools", callback_data=f"prm:tgt:{order_id}",
                                              style=ButtonStyle.PRIMARY)])
    except Exception:
        logger.exception("tgtools: ساختِ دکمهٔ پرمیوم ناموفق")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tgt_spawn_prm(bot: Bot, row, order_id: int, by_admin: bool = False) -> None:
    t = asyncio.create_task(tgtools_fulfil_premium(bot, row, order_id, by_admin=by_admin))
    _tgt_tasks.add(t)
    t.add_done_callback(_tgt_tasks.discard)


async def _tgt_revert_manual_prm(bot: Bot, row, order_id: int, reason: str, *, warn: bool = False) -> None:
    from tenant import _notify_admins
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            return
        if o.status == "auto_processing":
            o.status = "awaiting_admin"
        gift_id, dest, trk = o.gift_id, o.dest_label, o.tracking_code
        await s.commit()
    months = str(gift_id or "").split("_")[-1]
    await _notify_manual_order(
        bot, row,
        f"{'⚠️' if warn else '🤖'} خریدِ خودکارِ پرمیوم — سفارش #{await order_no(GiftOrder, order_id)}\n\n{reason}\n\n"
        f"پلن: {months} ماهه\n🎯 مقصد: {dest}\n🔖 کد: {trk}\n\n"
        "این سفارش به «در انتظارِ ادمین» برگشت؛ دستی هدیه بده و «✅ انجام شد» را بزن.",
        await _prm_admin_kb(order_id))


async def _tgt_finalize_prm(bot: Bot, row, order_id: int, order: dict) -> str:
    from tenant import _notify_admins
    from tenant.bots.virtual_service import tgtools_api as _tg
    st = _tg.norm_status(order)
    if st == "pending":
        return "pending"
    if st == "completed":
        async with Session() as s:
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            if not o or o.bot_id != row.id or o.status != "auto_processing":
                return "completed"
            o.status = "sent"
            buyer = await s.get(BotUser, o.buyer_user_id)
            buyer_tg_id = buyer.telegram_id if buyer else None
            trk = o.tracking_code
            months = o.gift_id.split("_")[-1] if o.gift_id else "?"
            await s.commit()
        await _notify_gift_order_trust_success(bot, row, order_id)
        await _notify_manual_order(bot, row, f"✅ پرمیومِ سفارش #{await order_no(GiftOrder, order_id)} با TGTools تحویل داده شد.")
        if buyer_tg_id:
            try:
                await bot.send_message(
                    buyer_tg_id,
                    f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n🌟 تلگرام پرمیومِ {months} ماهه فعال شد.\n"
                    f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
            except Exception:
                logger.exception("tgtools: خبر به مشتریِ پرمیوم ناموفق بود")
        return "completed"
    reason = str(order.get("message") or order.get("errorMessage") or "").strip()
    if await _tgt_refund_cancelled(bot, row, order_id, reason, "سفارش پرمیوم"):   # PATCH_TGTOOLS_CANCEL_REFUND_V1
        return "failed"
    await _tgt_revert_manual_prm(
        bot, row, order_id,
        "❌ TGTools سفارش را ناموفق اعلام کرد (طبقِ مستند، مبلغ به کیف‌پولِ TGTools برمی‌گردد)."
        + (f"\nعلت: {reason}" if reason else ""))
    return "failed"


async def tgtools_fulfil_premium(bot: Bot, row, order_id: int, *, by_admin: bool = False) -> None:
    from tenant.bots.virtual_service import tgtools_api as _tg
    if order_id in _tgt_running:
        return
    _tgt_running.add(order_id)
    try:
        key = await _tgt_key(row.id)
        if not key:
            if by_admin:
                from tenant import _notify_admins
                await _notify_admins(bot, row, "⚠️ کلیدِ API سایتِ TGTools ثبت نشده — از «ارائه‌دهندگانِ من ← 🤖 اتصال به TGTools» ثبتش کن.")
            return
        # ادعایِ اتمیِ سفارش (جلوگیریِ تحویلِ دوبار: دستی + خودکار)
        async with Session() as s:
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            if (not o or o.bot_id != row.id or not str(o.gift_id).startswith("premium_")
                    or o.status != "awaiting_admin"):
                return
            o.status = "auto_processing"
            try:
                months = int(str(o.gift_id).split("_")[-1])
            except ValueError:
                months = 0
            dest_label = o.dest_label
            await s.commit()
        if months not in (3, 6, 12):
            await _tgt_revert_manual_prm(bot, row, order_id, "مدتِ پرمیوم نامعتبر است (فقط ۳/۶/۱۲ ماه).")
            return
        username = _tg.extract_username(dest_label)
        if not username:
            await _tgt_revert_manual_prm(bot, row, order_id,
                                         "مقصدِ این سفارش یوزرنیم ندارد و TGTools فقط با یوزرنیم هدیه می‌دهد.")
            return
        try:
            res = await _tg.buy_premium(key, username, months, _tgt_code(row.id, order_id))
        except _tg.TgToolsAmbiguous as e:
            await _tgt_revert_manual_prm(
                bot, row, order_id,
                f"پاسخِ TGTools نامشخص بود ({e}). ممکن است سفارش ثبت شده باشد!\n"
                f"⛔ قبل از هر کاری در tg-tools.shop بخشِ تراکنش‌ها را با کدِ "
                f"<code>{_tgt_code(row.id, order_id)}</code> چک کن؛ اگر آنجا ثبت شده، دستی نفرست.", warn=True)
            return
        except _tg.TgToolsError as e:
            await _tgt_revert_manual_prm(bot, row, order_id, f"TGTools خرید را رد کرد: {e.message}")
            return
        tx = res.get("transactionId")
        if not tx:
            await _tgt_revert_manual_prm(
                bot, row, order_id,
                "TGTools شناسهٔ تراکنش برنگرداند؛ ممکن است سفارش ثبت شده باشد. "
                f"در سایت با کدِ <code>{_tgt_code(row.id, order_id)}</code> چک کن.", warn=True)
            return
        await _set_setting(row.id, f"tgtools_tx_{order_id}", str(tx))
        if _tg.norm_status(res) != "pending":
            await _tgt_finalize_prm(bot, row, order_id, res)
            return
        for d in _TGT_POLL_DELAYS:
            await asyncio.sleep(d)
            try:
                cur = await _tg.get_order(key, tx)
            except _tg.TgToolsError:
                continue
            if await _tgt_finalize_prm(bot, row, order_id, cur) != "pending":
                return
        from tenant import _notify_admins
        await _notify_manual_order(
            bot, row,
            f"⏳ پرمیومِ سفارش #{await order_no(GiftOrder, order_id)} هنوز در صفِ TGTools است (تراکنش {tx}). "
            "وقتی تحویل شد با دکمهٔ زیر وضعیت را بررسی کن — تا آن موقع دستی نفرست.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔄 بررسیِ وضعیت", callback_data=f"prm:tgtchk:{order_id}")]]))
    except Exception:
        logger.exception("tgtools: خطایِ پیش‌بینی‌نشده در پرمیومِ سفارش %s", order_id)
        try:
            await _tgt_revert_manual_prm(
                bot, row, order_id,
                "خطایِ داخلی رخ داد. اگر مطمئن نیستی خرید انجام شده یا نه، اول در tg-tools.shop چک کن.", warn=True)
        except Exception:
            logger.exception("tgtools: برگرداندنِ پرمیوم به دستی هم ناموفق بود")
    finally:
        _tgt_running.discard(order_id)


@smm_router.callback_query(F.data.startswith("prm:tgt:"))
async def prm_tgt_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ TGTools را ثبت کن (🤖 TGTools).", show_alert=True)
        return
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not o or o.bot_id != row.id or o.status != "awaiting_admin" or not str(o.gift_id).startswith("premium_"):
        await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        return
    await cb.answer("🤖 در حالِ خرید با TGTools… نتیجه اینجا اعلام می‌شود.", show_alert=True)
    _tgt_spawn_prm(bot, row, order_id, by_admin=True)
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ سفارشِ پرمیوم پس از خریدِ خودکار ممکن نشد")


@smm_router.callback_query(F.data.startswith("prm:tgtchk:"))
async def prm_tgt_check(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import tgtools_api as _tg
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    key = await _tgt_key(row.id)
    tx = await _get_setting(row.id, f"tgtools_tx_{order_id}", "")
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not key or not tx or not o or o.bot_id != row.id:
        await cb.answer("اطلاعاتِ این سفارش پیدا نشد.", show_alert=True)
        return
    if o.status != "auto_processing":
        await cb.answer("این سفارش قبلاً نهایی شده.", show_alert=True)
        return
    try:
        cur = await _tg.get_order(key, tx)
    except _tg.TgToolsError as e:
        await cb.answer(f"خطا در استعلام: {e.message}"[:190], show_alert=True)
        return
    res = await _tgt_finalize_prm(bot, row, order_id, cur)
    await cb.answer({"completed": "✅ تحویل داده شده", "failed": "❌ TGTools لغو/ناموفق اعلام کرد — وضعیت سفارش و پیام ادمین را ببین",
                     "pending": "⏳ هنوز در صف است"}[res], show_alert=True)
    if res != "pending":
        try:
            await cb.message.delete()
        except Exception:
            logger.info("حذفِ پیامِ بررسیِ وضعیتِ پرمیوم ممکن نشد")


# ═══════════════════════════════════════════
#  فروشِ استارزِ خام — PATCH_STARS_SELL_V1
# ═══════════════════════════════════════════
# برخلافِ گیفت و پرمیوم، هیچ APIای برایِ «فرستادنِ مستقیمِ N عدد استارزِ
# خام» به یک کاربر وجود ندارد — نه sendGift، نه هیچ‌چیزِ دیگری. پس این
# ویژگی از اول و همیشه دستی است: مشتری تعداد را می‌نویسد، مقصد را
# انتخاب می‌کند، پول می‌دهد؛ سفارش برایِ ادمین می‌رود؛ ادمین با روشِ
# خودش (مثلاً فرگمنت یا موجودیِ شخصی) استارز را واقعاً می‌فرستد، بعد در
# پنل «✅ انجام شد» می‌زند و مشتری باخبر می‌شود. عمداً به‌جایِ استفاده‌ی
# مجددِ توابعِ خودِ گیفت پیاده‌سازی شد — چون آن‌ها در تاییدِ ادمین سعی
# می‌کنند gift_id را به send_gift واقعی بدهند، و اینجا gift_id ساختگی
# (نه یک گیفتِ واقعی) است؛ استفاده‌ی مجدد ریسکِ فراخوانیِ اشتباهِ API را
# داشت.

async def _stars_sell_settings(bot_id: int) -> tuple[float, int, int]:
    """(نرخِ هر استارز به تومان، حداقلِ تعداد، حداکثرِ تعداد)"""
    # === PATCH_AUTO_RATE_SMM_V1 ===
    # اگر «نرخ خودکار» روشن باشد، نرخ از بازار (والکس) + درصدِ سود می‌آید.
    if (await _get_setting(bot_id, "stars_sell_auto", "0")) == "1":
        try:
            from tenant.gateways.stars_rate import star_toman
            _m = float(await _get_setting(bot_id, "stars_sell_margin", "60") or 60)
            _auto = await star_toman(_m, bot_id)
            if _auto > 0:
                rate_s = str(int(round(_auto)))
            else:
                rate_s = await _get_setting(bot_id, "stars_sell_rate", "1200")
        except Exception:
            rate_s = await _get_setting(bot_id, "stars_sell_rate", "1200")
    else:
        rate_s = await _get_setting(bot_id, "stars_sell_rate", "1200")
    min_s = await _get_setting(bot_id, "stars_sell_min", "50")
    max_s = await _get_setting(bot_id, "stars_sell_max", "10000")
    try:
        rate = float(rate_s)
    except Exception:
        rate = 1200.0
    try:
        qmin = int(min_s)
    except Exception:
        qmin = 50
    try:
        qmax = int(max_s)
    except Exception:
        qmax = 10000
    return rate, qmin, qmax


async def stars_sell_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rate, qmin, qmax = await _stars_sell_settings(bot_id)
    _auto_on = (await _get_setting(bot_id, "stars_sell_auto", "0")) == "1"
    _margin = float(await _get_setting(bot_id, "stars_sell_margin", "60") or 60)
    async with Session() as s:
        pending = await s.scalar(select(func.count()).select_from(GiftOrder).where(
            GiftOrder.bot_id == bot_id, GiftOrder.gift_id.like("starsell_%"),
            GiftOrder.status == "awaiting_admin"))
    text = (
        "⭐ <b>فروشِ استارزِ تلگرام</b>\n\n"
        "ربات‌ها نمی‌توانند استارزِ خام را مستقیم بفرستند؛ پس یا دستی می‌فرستی "
        "(مثلاً فرگمنت) و در پنل تایید می‌کنی، یا با «🤖 TGTools» (از بخشِ اتصالِ خودکار) خودکار "
        "ارسال می‌شود.\n\n"
        f"نرخ: هر ⭐ = <b>{rate:,.0f} تومان</b>\n"
        f"محدودهٔ سفارش: <b>{qmin:,}</b> تا <b>{qmax:,}</b> استارز\n"
        f"سفارش‌هایِ در انتظار: <b>{pending or 0}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💱 نرخ (هر ⭐ = {rate:,.0f} تومان)", callback_data="ssl:setrate")],
        [InlineKeyboardButton(
            text=("🔄 نرخ خودکار: روشن ✅" if _auto_on else "🔄 نرخ خودکار: خاموش"),
            callback_data="ssl:autotoggle",
            style=(ButtonStyle.SUCCESS if _auto_on else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({_margin:g}٪)",
                              callback_data="ssl:setmargin")],
        [InlineKeyboardButton(text="⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار", callback_data="sauto:base")],
        [InlineKeyboardButton(text=f"🔽 حداقلِ تعداد ({qmin:,})", callback_data="ssl:setmin")],
        [InlineKeyboardButton(text=f"🔼 حداکثرِ تعداد ({qmax:,})", callback_data="ssl:setmax")],
        _sfirst_button_row((await _get_setting(bot_id, "support_first_on", "0")) == "1", "stars"),
        [InlineKeyboardButton(text="✏️ متنِ پیامِ «اول به پشتیبانی پیام بده»", callback_data="sfirst:text:stars")],
        [InlineKeyboardButton(text="🧾 سفارش‌هایِ در انتظار", callback_data="ssl:orders:0")],
        # === PATCH_TGTOOLS_AUTO_SECTION_V1 === دکمهٔ TGTools به «ارائه‌دهندگانِ من» (اتصالِ خودکار) منتقل شد
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)],
    ])
    return text, kb


# === PATCH_SSL_ORDERS_LIST_V1 ===
# دکمهٔ «🧾 سفارش‌هایِ در انتظار» بالاتر (ssl:orders:0) هیچ‌وقت هندلر
# نداشت — پیدا شد حینِ اسکنِ استاتیکِ همه‌ی callback_dataهایِ این فایل.
# دقیقاً هم‌شکلِ ton_orders (چند خط پایین‌تر) ولی برایِ استارز؛ به‌جایِ
# دکمه‌هایِ تازه، از هندلرهایِ ازقبل‌موجودِ star:done:/star:reject:
# استفاده می‌کنه (که خودشون از پیامِ اطلاع‌رسانیِ تک‌سفارشی هم صدا زده
# می‌شن، پس اینجا فقط لیستِ چندتاییش رو اضافه می‌کنه).
@smm_router.callback_query(F.data.startswith("ssl:orders:"))
async def ssl_orders(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer()
        return
    try:
        page = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    per = 10
    async with Session() as s:
        q = select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.gift_id.like("starsell_%"),
            GiftOrder.status == "awaiting_admin").order_by(GiftOrder.id.desc())
        orders = (await s.scalars(q.limit(per).offset(page * per))).all()
        rows_data = [(o.id, o.star_count, o.price_toman, o.dest_label, o.tracking_code, o.dest_memo)
                     for o in orders]
    if not rows_data:
        await cb.answer("سفارشِ در انتظاری نیست.", show_alert=True)
        return
    rows = []
    lines_ = ["⭐ <b>سفارش‌هایِ در انتظارِ استارز</b>\n"]
    for oid, qty, price, dest, trk, memo in rows_data:       # PATCH_PENDING_LISTS_FIX_V1: tuple شش‌تایی بود و پنج‌تایی باز می‌شد ⇒ هندلر می‌مرد
        lines_.append(f"#{await order_no(GiftOrder, oid)} — {(qty or 0):,} ⭐ — {float(price or 0):,.0f} تومان\n🎯 {_html_escape(str(dest or ''))}  🔖 {trk}"
                      + (f"\n📝 ممو: <code>{_html_escape(memo)}</code>" if memo else ""))
        rows.append([
            InlineKeyboardButton(text=f"✅ انجام شد #{await order_no(GiftOrder, oid)}", callback_data=f"star:done:{oid}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text=f"❌ رد #{await order_no(GiftOrder, oid)}", callback_data=f"star:reject:{oid}", style=ButtonStyle.DANGER),
        ])
    if len(rows_data) == per:
        rows.append([InlineKeyboardButton(text="بعدی ▶️", callback_data=f"ssl:orders:{page+1}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ssl:menu", style=ButtonStyle.DANGER)])
    await cb.message.answer("\n\n".join(lines_), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@smm_router.callback_query(F.data == "ssl:menu")
async def ssl_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await stars_sell_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_AUTO_RATE_SMM_V1 — هندلرهایِ نرخِ خودکار ===
@smm_router.callback_query(F.data == "ssl:autotoggle")
async def ssl_auto_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "stars_sell_auto", "0")) == "1"
    await _set_setting(row.id, "stars_sell_auto", "0" if cur else "1")
    if not cur:
        from tenant.gateways.stars_rate import star_toman
        m = float(await _get_setting(row.id, "stars_sell_margin", "60") or 60)
        v = await star_toman(m, row.id)
        if v <= 0:
            await _set_setting(row.id, "stars_sell_auto", "0")
            await cb.answer("نرخِ بازار در دسترس نیست؛ فعلاً دستی بماند.",
                            show_alert=True)
        else:
            await cb.answer(f"روشن شد — هر ⭐ ≈ {int(v):,} تومان", show_alert=True)
    else:
        await cb.answer("خاموش شد؛ نرخِ دستی استفاده می‌شود.")
    text, kb = await stars_sell_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ssl:setmargin")
async def ssl_setmargin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    from tenant.gateways.stars_rate import star_toman
    base = await star_toman(0, row.id)
    hint = f"\n\nقیمتِ تمام‌شدهٔ هر ⭐ الان ≈ {int(base):,} تومان است." if base > 0 else ""
    await state.set_state(SmmFSM.stars_margin_in)
    await cb.message.answer(
        "📈 چند درصد سود رویِ نرخِ بازار گذاشته شود؟ (فقط عدد، مثال: 60)" + hint,
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.stars_margin_in, F.text)
async def ssl_setmargin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > 1000:
        await msg.answer("عددِ نامعتبر (۰ تا ۱۰۰۰). دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "stars_sell_margin", str(val))
    text, kb = await stars_sell_admin_content(row.id)
    await msg.answer("✅ درصدِ سود ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ssl:setrate")
async def ssl_setrate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.stars_rate_in)
    await cb.message.answer("💱 هر یک استارز چند تومان فروخته شود؟ (فقط عدد، مثال: 1200)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.stars_rate_in, F.text)
async def ssl_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "stars_sell_rate", str(val))
    text, kb = await stars_sell_admin_content(row.id)
    await msg.answer("✅ نرخ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ssl:setmin")
async def ssl_setmin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.stars_min_in)
    await cb.message.answer("🔽 حداقلِ تعدادِ استارز در هر سفارش چند باشد؟", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.stars_min_in, F.text)
async def ssl_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "stars_sell_min", str(val))
    text, kb = await stars_sell_admin_content(row.id)
    await msg.answer("✅ حداقل ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ssl:setmax")
async def ssl_setmax_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.stars_max_in)
    await cb.message.answer("🔼 حداکثرِ تعدادِ استارز در هر سفارش چند باشد؟", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.stars_max_in, F.text)
async def ssl_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "stars_sell_max", str(val))
    text, kb = await stars_sell_admin_content(row.id)
    await msg.answer("✅ حداکثر ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════════════════════════
#  فروشِ تون (TON) — PATCH_TON_SELL_V1
# ═══════════════════════════════════════════
# مثلِ فروشِ استارز، این هم *همیشه دستی* است: ربات هرگز به‌صورتِ خودکار روی
# بلاک‌چینِ TON چیزی نمی‌فرستد (چون آن مستلزمِ نگه‌داریِ کلیدِ خصوصیِ کیف‌پولِ
# ادمین داخلِ ربات است — ریسکِ امنیتیِ بزرگ). مشتری مقدارِ تون و آدرسِ
# کیف‌پولش را می‌دهد و پول را از کیف‌پول می‌پردازد؛ سفارش «awaiting_admin»
# می‌شود و ادمین با کیف‌پولِ خودش واقعاً تون را می‌فرستد و تأیید می‌کند.
#
# مقدارِ تون اعشاری است (تا ۲ رقم)، ولی چون ستونِ star_count عدد صحیح است،
# مقدار را به «صدم‌تون» (amount × 100) ذخیره می‌کنیم و موقعِ نمایش /۱۰۰ می‌کنیم.

_TON_CENTI = 100  # هر تون = ۱۰۰ صدم‌تون


async def _ton_sell_settings(bot_id: int) -> tuple[float, float, float]:
    """(نرخِ هر ۱ تون به تومان، حداقلِ مقدار تون، حداکثرِ مقدار تون)"""
    # === PATCH_AUTO_RATE_TON_V1 ===
    # درخواستِ ادمین: «استارز نرخِ خودکار داره، تون هم داشته باشه».
    # عیناً همان منطقِ استارز: قیمتِ لحظه‌ایِ تون × نرخِ دلار × (۱ + سود).
    # اگر گرفتنِ نرخ شکست بخورد، به نرخِ دستیِ ذخیره‌شده برمی‌گردیم تا
    # فروش هیچ‌وقت به‌خاطرِ قطعیِ API متوقف نشود.
    if (await _get_setting(bot_id, "ton_sell_auto", "0")) == "1":
        try:
            from tenant.gateways.stars_rate import ton_toman
            _m = float(await _get_setting(bot_id, "ton_sell_margin", "10") or 10)
            _auto = await ton_toman(_m)
            rate_s = (str(int(round(_auto))) if _auto > 0
                      else await _get_setting(bot_id, "ton_sell_rate", "0"))
        except Exception:
            logger.exception("نرخِ خودکارِ تون گرفته نشد؛ نرخِ دستی استفاده شد")
            rate_s = await _get_setting(bot_id, "ton_sell_rate", "0")
    else:
        rate_s = await _get_setting(bot_id, "ton_sell_rate", "0")
    min_s = await _get_setting(bot_id, "ton_sell_min", "1")
    max_s = await _get_setting(bot_id, "ton_sell_max", "100")
    def _f(x, d):
        try:
            return float(x)
        except Exception:
            return d
    return _f(rate_s, 0.0), _f(min_s, 1.0), _f(max_s, 100.0)


def _parse_ton_amount(raw: str) -> float | None:
    """مقدارِ تونِ واردشده را به عدد اعشاری (با دقتِ ۲ رقم) تبدیل می‌کند.
    ارقامِ فارسی را هم می‌پذیرد. اگر نامعتبر بود None."""
    if not raw:
        return None
    t = raw.strip().replace("٫", ".").replace("،", ".")
    # تبدیلِ ارقامِ فارسی/عربی به لاتین
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    t = t.translate(trans)
    try:
        val = float(t)
    except ValueError:
        return None
    if val <= 0:
        return None
    return round(val, 2)


def _looks_like_ton_address(addr: str) -> bool:
    """اعتبارسنجیِ سبکِ آدرسِ کیف‌پولِ TON: آدرس‌های TON یا با UQ/EQ شروع
    می‌شوند (فرمتِ user-friendly، ۴۸ کاراکتر) یا به‌صورتِ raw «0:hex»‌اند.
    این فقط یک چکِ ظاهری است تا اشتباهاتِ واضح جلو گرفته شود؛ صحتِ نهایی را
    ادمین موقعِ ارسال می‌بیند."""
    a = (addr or "").strip()
    if not a:
        return False
    if len(a) == 48 and (a.startswith("UQ") or a.startswith("EQ") or a.startswith("kQ") or a.startswith("0Q")):
        return True
    if a.startswith("0:") and len(a) > 10:
        return True
    # دامنه‌های TON DNS مثل name.ton هم مجازند
    if a.lower().endswith(".ton") and len(a) >= 5:
        return True
    return False


async def ton_sell_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rate, qmin, qmax = await _ton_sell_settings(bot_id)
    # === PATCH_AUTO_RATE_TON_V1 ===
    _ton_auto_on = (await _get_setting(bot_id, "ton_sell_auto", "0")) == "1"
    try:
        _ton_margin = float(await _get_setting(bot_id, "ton_sell_margin", "10") or 10)
    except (TypeError, ValueError):
        _ton_margin = 10.0
    async with Session() as s:
        pending = await s.scalar(select(func.count()).select_from(GiftOrder).where(
            GiftOrder.bot_id == bot_id, GiftOrder.gift_id.like("tonsell_%"),
            GiftOrder.status == "awaiting_admin"))
    rate_txt = f"{rate:,.0f}" if rate else "تنظیم‌نشده"
    text = (
        "💎 <b>فروشِ تون (TON)</b>\n\n"
        "این ویژگی همیشه دستی است — ربات به‌صورتِ خودکار روی بلاک‌چین چیزی "
        "نمی‌فرستد. مشتری مقدار و آدرسِ کیف‌پولش را می‌دهد و پول می‌پردازد؛ "
        "تو با کیف‌پولِ خودت واقعاً تون را می‌فرستی و بعد در همین‌جا تأییدش "
        "می‌کنی.\n\n"
        f"نرخ: هر ۱ TON = <b>{rate_txt} تومان</b>\n"
        f"محدودهٔ سفارش: <b>{qmin:g}</b> تا <b>{qmax:g}</b> TON\n"
        f"سفارش‌هایِ در انتظار: <b>{pending or 0}</b>"
    )
    if not rate:
        text += "\n\n⚠️ تا وقتی نرخ را تنظیم نکنی، این بخش برای مشتری فعال نمی‌شود."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💱 نرخ (هر ۱ TON = {rate_txt} تومان)", callback_data="ton:setrate")],
        # === PATCH_AUTO_RATE_TON_V1 ===
        [InlineKeyboardButton(
            text=("🔄 نرخ خودکار: روشن ✅" if _ton_auto_on else "🔄 نرخ خودکار: خاموش"),
            callback_data="ton:autotoggle",
            style=(ButtonStyle.SUCCESS if _ton_auto_on else None))],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({_ton_margin:g}٪)",
                              callback_data="ton:setmargin")],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مقدار ({qmin:g} TON)", callback_data="ton:setmin")],
        [InlineKeyboardButton(text=f"🔼 حداکثرِ مقدار ({qmax:g} TON)", callback_data="ton:setmax")],
        [InlineKeyboardButton(text="🧾 سفارش‌هایِ در انتظار", callback_data="ton:orders:0")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@smm_router.callback_query(F.data == "ton:menu")
async def ton_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await ton_sell_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# === PATCH_AUTO_RATE_TON_V1 — هندلرهایِ نرخِ خودکارِ تون ===
# عیناً هم‌ساختارِ ssl:autotoggle / ssl:setmargin برایِ استارز، تا رفتارِ
# دو بخش برایِ ادمین یکسان باشد.
@smm_router.callback_query(F.data == "ton:autotoggle")
async def ton_auto_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "ton_sell_auto", "0")) == "1"
    await _set_setting(row.id, "ton_sell_auto", "0" if cur else "1")
    if not cur:
        from tenant.gateways.stars_rate import ton_toman
        m = float(await _get_setting(row.id, "ton_sell_margin", "10") or 10)
        v = await ton_toman(m)
        if v <= 0:
            # نرخِ بازار در دسترس نیست — روشن‌کردنش یعنی فروش با نرخِ صفر،
            # پس برش می‌گردانیم به دستی به‌جایِ اینکه ضرر بدهد.
            await _set_setting(row.id, "ton_sell_auto", "0")
            await cb.answer("نرخِ بازار در دسترس نیست؛ فعلاً دستی بماند.",
                            show_alert=True)
        else:
            await cb.answer(f"روشن شد — هر ۱ TON ≈ {int(v):,} تومان", show_alert=True)
    else:
        await cb.answer("خاموش شد؛ نرخِ دستی استفاده می‌شود.")
    text, kb = await ton_sell_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (رفرشِ صفحهٔ فروشِ تون)")


@smm_router.callback_query(F.data == "ton:setmargin")
async def ton_setmargin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.ton_margin_in)
    await cb.message.answer(
        "📈 درصدِ سود رویِ نرخِ بازارِ تون را بفرست (فقط عدد، مثال: <code>10</code>).\n\n"
        "<i>نرخِ نهایی = قیمتِ لحظه‌ایِ تون × نرخِ دلار × (۱ + درصدِ سود).</i>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@smm_router.message(SmmFSM.ton_margin_in, F.text)
async def ton_setmargin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    try:
        val = float((msg.text or "").strip().translate(
            str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
    except (TypeError, ValueError):
        await msg.answer("❌ عددِ معتبر بفرست (مثلاً 10).")
        return
    if not (0 <= val <= 500):
        await msg.answer("❌ درصد باید بینِ ۰ تا ۵۰۰ باشد.")
        return
    await _set_setting(row.id, "ton_sell_margin", str(val))
    from tenant.gateways.stars_rate import ton_toman
    v = await ton_toman(val)
    await msg.answer(
        f"✅ درصدِ سود رویِ {val:g}٪ تنظیم شد."
        + (f"\nنرخِ فعلی: هر ۱ TON ≈ <b>{int(v):,}</b> تومان" if v > 0 else
           "\n<i>نرخِ بازار الان در دسترس نیست؛ به‌محضِ برگشتن اعمال می‌شود.</i>"),
        parse_mode="HTML")
    text, kb = await ton_sell_admin_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ton:setrate")
async def ton_setrate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.ton_rate_in)
    await cb.message.answer("💱 هر ۱ تون (TON) چند تومان فروخته شود؟ (فقط عدد، مثال: 350000)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.ton_rate_in, F.text)
async def ton_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if not val or val <= 0:
        await msg.answer("عددِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "ton_sell_rate", str(val))
    text, kb = await ton_sell_admin_content(row.id)
    await msg.answer("✅ نرخ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ton:setmin")
async def ton_setmin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.ton_min_in)
    await cb.message.answer("🔽 حداقلِ مقدارِ تون در هر سفارش چند باشد؟ (اعشاری مجاز، مثال: 0.5)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.ton_min_in, F.text)
async def ton_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    val = _parse_ton_amount(msg.text)
    if val is None:
        await msg.answer("مقدارِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "ton_sell_min", str(val))
    text, kb = await ton_sell_admin_content(row.id)
    await msg.answer("✅ حداقل ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "ton:setmax")
async def ton_setmax_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.ton_max_in)
    await cb.message.answer("🔼 حداکثرِ مقدارِ تون در هر سفارش چند باشد؟ (اعشاری مجاز، مثال: 100)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.ton_max_in, F.text)
async def ton_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    val = _parse_ton_amount(msg.text)
    if val is None:
        await msg.answer("مقدارِ نامعتبر. دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "ton_sell_max", str(val))
    text, kb = await ton_sell_admin_content(row.id)
    await msg.answer("✅ حداکثر ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ───────── سمتِ مشتری ─────────

async def show_ton_shop_to_user(bot: Bot, target: Message, bot_id: int,
                                state: "FSMContext | None" = None) -> None:
    rate, qmin, qmax = await _ton_sell_settings(bot_id)
    from tenant import smart_cancel_kb
    if not rate:
        await target.answer("💎 فروشِ تون فعلاً در دسترس نیست. بعداً دوباره سر بزن.")
        return
    if state is not None:
        await state.set_state(SmmFSM.ton_amount_in)
    await target.answer(
        "💎 <b>خریدِ تون (TON)</b>\n\n"
        f"نرخ: هر ۱ TON = <b>{rate:,.0f} تومان</b>\n"
        f"چه مقدار تون می‌خواهی؟ (بینِ {qmin:g} تا {qmax:g} — اعشاری مجاز، مثال: 1.5)\n"
        "مقدارِ موردنظرت را بفرست:",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.ton_amount_in, F.text)
async def ton_amount_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = _parse_ton_amount(msg.text)
    rate, qmin, qmax = await _ton_sell_settings(row.id)
    if amount is None or amount < qmin or amount > qmax:
        await msg.answer(f"❌ مقدار باید بینِ {qmin:g} تا {qmax:g} تون باشد. دوباره بفرست:")
        return
    await state.update_data(ton_amount=amount, ton_disc=None)   # PATCH_VS_DISCOUNT_V1
    await state.set_state(SmmFSM.ton_wallet_in)
    from tenant import smart_cancel_kb
    price = round(amount * rate)
    from tenant.bots.virtual_service.vs_discount import vsd_enabled   # PATCH_VS_DISCOUNT_V1
    _disc_hint = ("\n\n🏷 کد تخفیف داری؟ <b>قبل از آدرس</b>، همان کد را بفرست."
                  if await vsd_enabled(row.id) else "")
    await msg.answer(
        f"💎 {amount:g} TON — {price:,} تومان\n\n"
        "حالا <b>آدرسِ کیف‌پولِ TON</b> مقصد را بفرست (همان‌جایی که می‌خواهی تون‌ها "
        "واریز شود).\n\n"
        "⚠️ آدرس را با دقت بفرست؛ واریزِ اشتباه روی بلاک‌چین قابلِ برگشت نیست."
        + _disc_hint,
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.ton_wallet_in, F.text)
async def ton_wallet_in(msg: Message, bot: Bot, state: FSMContext, _wallet: str | None = None,
                        _memo: str | None = None):
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        return
    wallet = _wallet or (msg.text or "").strip()
    # === PATCH_VS_DISCOUNT_V1 === متنی که آدرسِ TON نیست ولی کدِ تخفیفِ معتبر است ⇒ کد اعمال می‌شود
    if not _looks_like_ton_address(wallet):
        from tenant.bots.virtual_service.vs_discount import vsd_enabled
        if await vsd_enabled(row.id):
            _d = await state.get_data()
            _amt = _d.get("ton_amount")
            if _amt:
                from tenant.gateways.discount_engine import validate_discount
                _rate, _, _ = await _ton_sell_settings(row.id)
                _res = await validate_discount(bot_id=row.id, code=wallet, telegram_id=msg.from_user.id,
                                               price=round(_amt * _rate), product_key="ton")
                if _res.ok:
                    await state.update_data(ton_disc=_res.code or wallet.upper())
                    await msg.answer(
                        f"✅ کد تخفیف اعمال شد (−{_res.discount:,} تومان)\n"
                        f"💰 مبلغِ نهایی: <b>{_res.final_price:,} تومان</b>\n\n"
                        "حالا <b>آدرسِ کیف‌پولِ TON</b> را بفرست:", parse_mode="HTML")
                    return
                if len(wallet) <= 40 and " " not in wallet and not wallet.lower().endswith(".ton"):
                    await msg.answer(f"❌ {_res.reason or 'کد تخفیف نامعتبر است.'}\n"
                                     "کدِ دیگری بفرست یا آدرسِ کیف‌پولِ TON را بفرست:")
                    return
        await msg.answer(
            "❌ این آدرس معتبر به‌نظر نمی‌رسد. آدرسِ کیف‌پولِ TON معمولاً با "
            "<code>UQ</code> یا <code>EQ</code> شروع می‌شود (۴۸ کاراکتر) یا یک "
            "دامنهٔ <code>.ton</code> است. دوباره بفرست:", parse_mode="HTML")
        return
    data = await state.get_data()
    amount = data.get("ton_amount")
    if not amount:
        await state.clear()
        await msg.answer("مقدار یافت نشد؛ لطفاً دوباره از فروشگاه شروع کن.")
        return
    rate, qmin, qmax = await _ton_sell_settings(row.id)
    if amount < qmin or amount > qmax:
        await state.clear()
        await msg.answer("این سفارش دیگر معتبر نیست؛ دوباره از فروشگاه شروع کن.")
        return
    price = round(amount * rate)

    # === PATCH_TON_MEMO_V1 === آدرس‌هایِ صرافی/کیف‌پولِ امانی برایِ واریز «کامنت/ممو» می‌خواهند؛ بدونِ آن تون گم می‌شود
    if _memo is None and (await _get_setting(row.id, "ton_memo_ask", "1")) == "1":
        await state.update_data(ton_wallet_pending=wallet)
        await state.set_state(SmmFSM.ton_memo_in)
        from tenant import smart_cancel_kb
        await msg.answer(
            f"✅ آدرس ثبت شد:\n<code>{_html_escape(wallet)}</code>\n\n"
            "📝 اگر این آدرس (مثلاً صرافی) برایِ واریز <b>کامنت / ممو (Memo/Tag)</b> می‌خواهد، همین‌جا بفرست.\n"
            "اگر لازم نیست، بنویس: <b>ندارم</b>",
            parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
        return

    buyer, _ = await get_or_create_user(
        row.id, msg.from_user.id, msg.from_user.full_name, msg.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        # *قبل* از بررسیِ موجودی، وگرنه کاربری که با تخفیف پولش می‌رسید
        # اشتباهاً «موجودی ناکافی» می‌گیرد.
        _note_extra = ""
        # === PATCH_VS_DISCOUNT_V1 === کدِ واردشده بر تخفیفِ اولین خرید اولویت دارد
        _dc_id, _dc_amt = None, 0
        _dc_code = data.get("ton_disc")
        if _dc_code:
            from tenant.bots.virtual_service.vs_discount import vsd_charge, vsd_record, VsdError
            try:
                price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=msg.from_user.id, code=_dc_code, kind="ton", price=int(price))
            except VsdError as _e:
                await state.update_data(ton_disc=None)
                await msg.answer(f"❌ {_e}\nدوباره آدرس را بفرست تا بدونِ کد ادامه بدهیم.")
                return
            await vsd_record(s, bot_id=row.id, tg_id=msg.from_user.id, code_id=_dc_id, discount=_dc_amt)
            _note_extra = f" (کد تخفیف −{_dc_amt:,})"
        else:
            from tenant.gateways.discount_engine import first_purchase_discount
            _fd = await first_purchase_discount(
                bot_id=row.id, telegram_id=msg.from_user.id, price=int(price), session=s)
            if _fd.ok and _fd.discount > 0:
                _note_extra = f" (تخفیف اولین خرید −{_fd.discount:,})"
                price = _fd.final_price
        if float(b.balance) < price:
            await state.clear()
            from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — دکمهٔ «شارژ حساب»
            await msg.answer(
                f"❌ موجودیِ کیف‌پول کافی نیست. کمبود: {price - int(b.balance):,} تومان\n"
                "اول کیف‌پولت را شارژ کن و دوباره اقدام کن.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=await _vp.charge_row(row.id, msg.from_user.id, int(price))))
            return
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ {amount:g} تون (TON){_note_extra}"))
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        centi = int(round(amount * _TON_CENTI))  # مقدارِ تون به‌صورتِ صدم‌تون
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=f"tonsell_{centi}",
                          gift_emoji="💎", star_count=centi, price_toman=price,
                          dest_telegram_id=msg.from_user.id, dest_label=wallet[:120],
                          dest_memo=(_memo or None),   # PATCH_TON_MEMO_V1
                          status="awaiting_admin", pay_method="wallet", tracking_code=trk,
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(order)
        await s.commit()
        order_id = order.id

    await state.clear()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await msg.answer(
        "✅ پرداختت ثبت شد — سفارشت برایِ ادمین فرستاده شد و به‌زودی تون‌ها به "
        f"آدرست واریز می‌شود.\n🔖 کد پیگیری: <code>{trk}</code>",
        parse_mode="HTML", reply_markup=await _gift_support_kb(trk))
    await _notify_manual_order(
        bot, row,
        f"💎 سفارشِ تون (TON) — #{await order_no(GiftOrder, order_id)}\n"
        f"مقدار: {amount:g} TON\n💰 {price:,} تومان\n"
        f"🎯 آدرسِ کیف‌پول: <code>{wallet}</code>\n"
        + (f"📝 کامنت/ممو: <code>{_html_escape(_memo)}</code>\n" if _memo else "📝 کامنت/ممو: ندارد\n")
        + _ton_buyer_block(msg.from_user)   # PATCH_TON_MEMO_V1 — مشخصاتِ خریدار
        + f"🔖 کد: {trk}\n\n"
        "این تون را باید خودت واقعاً به آدرسِ بالا بفرستی، بعد در «💎 فروشِ تون ← "
        "سفارش‌هایِ در انتظار» (یا با دکمه‌هایِ زیر) تأییدش کنی.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ ارسال شد", callback_data=f"ton:done:{order_id}",
                                 style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="↩️ رد و بازگشتِ وجه", callback_data=f"ton:refund:{order_id}",
                                 style=ButtonStyle.DANGER)]]))


def _ton_buyer_block(user) -> str:
    """=== PATCH_TON_MEMO_V1 === مشخصاتِ خریدار در پیامِ ادمین (قبلاً اصلاً نبود)."""
    name = _html_escape((getattr(user, "full_name", "") or "—")[:60])
    lines = [f"👤 خریدار: {name}", f"🆔 آیدی: <code>{user.id}</code>"]
    lines.append(f"🔗 یوزرنیم: @{user.username}" if getattr(user, "username", None) else "🔗 یوزرنیم: ندارد")
    return "\n".join(lines) + "\n"


@smm_router.message(SmmFSM.ton_memo_in, F.text)
async def ton_memo_in(msg: Message, bot: Bot, state: FSMContext):
    """=== PATCH_TON_MEMO_V1 === کامنت/ممو را می‌گیرد و سفارش را نهایی می‌کند."""
    data = await state.get_data()
    wallet = data.get("ton_wallet_pending")
    if not wallet:
        await state.clear()
        await msg.answer("جلسه منقضی شد؛ دوباره از فروشگاهِ تون شروع کن.")
        return
    raw = (msg.text or "").strip()
    memo = "" if raw in ("ندارم", "ندارد", "نه", "-", "ندارم.") else raw
    if len(memo) > 120:
        await msg.answer("❌ کامنت/ممو خیلی بلند است (حداکثر ۱۲۰ کاراکتر). دوباره بفرست یا بنویس «ندارم».")
        return
    await state.set_state(SmmFSM.ton_wallet_in)
    await ton_wallet_in(msg, bot, state, _wallet=wallet, _memo=memo)


# ───────── سفارش‌هایِ در انتظارِ تون (ادمین) ─────────

@smm_router.callback_query(F.data.startswith("ton:orders:"))
async def ton_orders(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    try:
        page = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    per = 10
    async with Session() as s:
        q = select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.gift_id.like("tonsell_%"),
            GiftOrder.status == "awaiting_admin").order_by(GiftOrder.id.desc())
        orders = (await s.scalars(q.limit(per).offset(page * per))).all()
        rows_data = [(o.id, o.star_count, o.price_toman, o.dest_label, o.tracking_code, o.dest_memo)   # PATCH_PENDING_LISTS_FIX_V1
                     for o in orders]
    if not rows_data:
        await cb.answer("سفارشِ در انتظاری نیست.", show_alert=True)
        return
    rows = []
    lines = ["💎 <b>سفارش‌هایِ در انتظارِ تون</b>\n"]
    for oid, centi, price, wallet, trk, memo in rows_data:
        amount = (centi or 0) / _TON_CENTI
        lines.append(f"#{await order_no(GiftOrder, oid)} — {amount:g} TON — {price:,} تومان\n🎯 <code>{wallet}</code>  🔖 {trk}"
                     + (f"\n📝 ممو: <code>{_html_escape(memo)}</code>" if memo else ""))
        rows.append([InlineKeyboardButton(text=f"✅ تأیید ارسال #{await order_no(GiftOrder, oid)}",
                                          callback_data=f"ton:done:{oid}")])
        rows.append([InlineKeyboardButton(text=f"❌ لغو و بازپرداخت #{await order_no(GiftOrder, oid)}",
                                          callback_data=f"ton:refund:{oid}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ton:menu", style=ButtonStyle.DANGER)])
    await cb.message.answer("\n\n".join(lines), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@smm_router.callback_query(F.data.startswith("ton:done:"))
async def ton_order_done(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    oid = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, oid)
        if not o or o.bot_id != row.id or not (o.gift_id or "").startswith("tonsell_"):
            await cb.answer("سفارش یافت نشد.", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "sent"
        buyer = await s.get(BotUser, o.buyer_user_id)
        amount = (o.star_count or 0) / _TON_CENTI
        buyer_tg = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        await s.commit()
    # === PATCH_TON_TRUST_REPORT_V1 === گزارشِ فدایی: خریدِ تون در کانالِ اعتمادسازی پست نمی‌شد.
    # هنگامِ ثبت، سفارش awaiting_admin است و پست عمداً رد می‌شود؛ اینجا (بعد از تحویل) باید پست شود.
    await _notify_gift_order_trust_success(bot, row, oid)
    await cb.answer("✅ به‌عنوان ارسال‌شده ثبت شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if buyer_tg:
        try:
            tbot = bot
            await tbot.send_message(
                buyer_tg,
                f"✅ سفارشِ تون تو (#{await order_no(GiftOrder, oid)}) ارسال شد!\n"
                f"💎 {amount:g} TON به آدرست واریز شد.\n🔖 کد پیگیری: {trk}")
        except Exception:
            logger.exception("اطلاع‌رسانیِ تأییدِ تون به کاربر ناموفق بود")


@smm_router.callback_query(F.data.startswith("ton:refund:"))
async def ton_order_refund(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    oid = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, oid, with_for_update=True)
        if not o or o.bot_id != row.id or not (o.gift_id or "").startswith("tonsell_"):
            await cb.answer("سفارش یافت نشد.", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "refunded"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        refunded = int(o.price_toman or 0)
        from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
        await vsd_revert_order(s, o, buyer.telegram_id if buyer else None)
        if buyer:
            buyer.balance = float(buyer.balance or 0) + refunded
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=refunded,
                            kind="charge", note=f"بازپرداختِ سفارشِ تون #{await order_no(GiftOrder, oid)}"))
        buyer_tg = buyer.telegram_id if buyer else None
        await s.commit()
    await cb.answer("↩️ بازپرداخت شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if buyer_tg:
        try:
            await bot.send_message(
                buyer_tg,
                f"↩️ سفارشِ تونِ تو (#{await order_no(GiftOrder, oid)}) لغو شد و مبلغِ {refunded:,} تومان به "
                "کیف‌پولت بازگشت.")
        except Exception:
            logger.exception("اطلاع‌رسانیِ بازپرداختِ تون به کاربر ناموفق بود")


# ═══════════════════════════════════════════
#  ری‌اکشنِ استارزیِ پست — PATCH_STAR_REACTION_V1
# ═══════════════════════════════════════════
# مثلِ «فروشِ استارزِ خام»، این هم همیشه دستی است: هیچ APIای برایِ
# ثبتِ خودکارِ N ری‌اکشنِ استارزی رویِ یک پست وجود ندارد. مشتری تعداد و
# لینکِ پست را می‌دهد، پول را از کیف‌پول کم می‌کنیم، ادمین با روشِ خودش
# (مثلاً فرگمنت/چند اکانت) واقعاً ری‌اکشن‌ها را ثبت می‌کند و بعد در پنل
# تاییدش می‌کند — دقیقاً همان الگویِ فروشِ استارزِ خام، فقط با یک لینکِ
# پست به‌جایِ یک شخص به‌عنوانِ مقصد.

async def _srx_settings(bot_id: int) -> tuple[float, int]:
    """(نرخِ هر ری‌اکشن‌استارز به تومان، حداقلِ تعداد)"""
    rate_s = await _get_setting(bot_id, "srx_rate", "1200")
    if (await _get_setting(bot_id, "srx_auto", "0")) == "1":
        try:
            from tenant.gateways.stars_rate import star_toman
            _m = float(await _get_setting(bot_id, "srx_margin", "60") or 60)
            _a = await star_toman(_m, bot_id)
            if _a > 0:
                rate_s = str(int(round(_a)))
        except Exception:
            logger.exception("نرخِ خودکارِ ری‌اکشنِ استارزی")
    min_s = await _get_setting(bot_id, "srx_min", "5")
    try:
        rate = float(rate_s)
    except Exception:
        rate = 1200.0
    try:
        qmin = int(min_s)
    except Exception:
        qmin = 5
    return rate, qmin


async def srx_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rate, qmin = await _srx_settings(bot_id)
    _sauto = (await _get_setting(bot_id, "srx_auto", "0")) == "1"
    _smargin = float(await _get_setting(bot_id, "srx_margin", "60") or 60)
    async with Session() as s:
        pending = await s.scalar(select(func.count()).select_from(GiftOrder).where(
            GiftOrder.bot_id == bot_id, GiftOrder.gift_id.like("srx_%"),
            GiftOrder.status == "awaiting_admin"))
    text = (
        "⭐️ <b>ری‌اکشنِ استارزیِ پست</b>\n\n"
        "این ویژگی همیشه دستی است — مشتری تعداد و لینکِ پست را می‌دهد و "
        "پول می‌دهد؛ تو با روشِ خودت واقعاً ری‌اکشن‌ها را رویِ آن پست ثبت "
        "می‌کنی، بعد در پنل تاییدش می‌کنی.\n\n"
        f"نرخ: هر ⭐️ = <b>{rate:,.0f} تومان</b>\n"
        f"حداقلِ تعداد: <b>{qmin:,}</b> ری‌اکشن\n"
        f"سفارش‌هایِ در انتظار: <b>{pending or 0}</b>\n\n"
        "⚠️ شرایطی که مشتری باید رعایت کند: کانال باید عمومی (Public) "
        "باشد و قابلیتِ دریافتِ ری‌اکشنِ ⭐️ رویِ آن پست فعال باشد."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💱 نرخ (هر ⭐️ = {rate:,.0f} تومان)", callback_data="srx:setrate")],
        [InlineKeyboardButton(
            text=("🔄 نرخ خودکار: روشن ✅" if _sauto else "🔄 نرخ خودکار: خاموش"),
            callback_data="sauto:srx",
            style=(ButtonStyle.SUCCESS if _sauto else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=f"📈 درصدِ سود ({_smargin:g}٪)", callback_data="srx:setmargin")],
        [InlineKeyboardButton(text="⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار", callback_data="sauto:base")],
        [InlineKeyboardButton(text=f"🔽 حداقلِ تعداد ({qmin:,})", callback_data="srx:setmin")],
        [InlineKeyboardButton(text="🧾 سفارش‌هایِ در انتظار", callback_data="srx:orders:0")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:manlist", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@smm_router.callback_query(F.data == "srx:menu")
async def srx_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await srx_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "srx:setrate")
async def srx_setrate_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.srx_rate_in)
    await cb.message.answer("💱 هر یک ری‌اکشن‌استارز چند تومان فروخته شود؟ (فقط عدد، مثال: 1200)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.srx_rate_in, F.text)
async def srx_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    from tenant import fa_to_int
    val = fa_to_int(raw)
    if not val or val <= 0:
        await state.clear()
        raise SkipHandler
    await state.clear()
    if not row:
        return
    await _set_setting(row.id, "srx_rate", str(val))
    text, kb = await srx_admin_content(row.id)
    await msg.answer("✅ نرخ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_AUTO_RATE_V2 — نرخِ خودکارِ گیفت و ری‌اکشنِ استارزی + قیمتِ پایه ===
@smm_router.callback_query(F.data.in_({"sauto:gift", "sauto:srx", "sauto:premium"}))
async def sauto_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    kind = cb.data.split(":")[1]
    key = {"gift": "gift_auto", "premium": "premium_auto"}.get(kind, "srx_auto")
    if kind == "premium":   # PATCH_PREMIUM_FRAGMENT_PRICE_V1 — خاموش → ⭐ استارز → 💎 فرگمنت → خاموش
        nxt = {"0": "1", "1": "fragment", "fragment": "0"}[await _premium_auto_mode(row.id)]
        if nxt == "1":
            from tenant.gateways.stars_rate import star_toman
            v = await star_toman(0, row.id)
            if v <= 0:
                nxt = "fragment"
        if nxt == "fragment":
            _u = await _premium_usd_toman(row.id)
            if _u <= 0:
                await cb.answer("نرخِ دلار در دسترس نیست؛ از «⚙️ قیمتِ پایه» نرخِ دلار را دستی بده.",
                                show_alert=True)
                nxt = "0"
            else:
                await cb.answer(f"💎 بر اساسِ فرگمنت — دلار ≈ {int(_u):,} تومان (+ درصدِ سود)", show_alert=True)
        elif nxt == "1":
            await cb.answer(f"⭐ بر اساسِ استارز — هر ⭐ ≈ {int(v):,} تومان (بدونِ سود)", show_alert=True)
        else:
            await cb.answer("خاموش شد؛ نرخِ دستی استفاده می‌شود.")
        await _set_setting(row.id, key, nxt)
        text, kb = await premium_admin_content(row.id, bot)
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    cur = (await _get_setting(row.id, key, "0")) == "1"
    if not cur:
        from tenant.gateways.stars_rate import star_toman
        v = await star_toman(0, row.id)
        if v <= 0:
            await cb.answer("نرخِ بازار در دسترس نیست؛ دستی بماند (یا نرخِ دلار را در «قیمتِ پایه» بنویس).",
                            show_alert=True)
            return
        await _set_setting(row.id, key, "1")
        await cb.answer(f"روشن شد — هر ⭐ ≈ {int(v):,} تومان (بدونِ سود)", show_alert=True)
    else:
        await _set_setting(row.id, key, "0")
        await cb.answer("خاموش شد؛ نرخِ دستی استفاده می‌شود.")
    if kind == "gift":
        text, kb = await gift_admin_content(row.id, bot)
    elif kind == "premium":
        text, kb = await premium_admin_content(row.id, bot)
    else:
        text, kb = await srx_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "sauto:base")
async def sauto_base_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    from tenant.gateways.stars_rate import usd_toman, STAR_USD
    b = float(await _get_setting(row.id, "stars_base_usd", "0") or 0) or STAR_USD
    u = float(await _get_setting(row.id, "stars_usd_toman", "0") or 0)
    live = await usd_toman()
    await state.set_state(SmmFSM.stars_base_in)
    await cb.message.answer(
        "⚙️ <b>قیمتِ پایهٔ نرخِ خودکار</b> (برایِ فروشِ استارز، گیفت و ری‌اکشن)\n\n"
        f"• قیمتِ پایهٔ هر ⭐ به دلار: <b>{b:g}</b>\n"
        f"• نرخِ دلار: <b>{(f'{u:,.0f} (دستی)' if u > 0 else f'{live:,.0f} (خودکار)' if live > 0 else 'نامشخص')}</b>\n\n"
        "یکی از این دو را بفرست:\n"
        "<code>قیمت 0.0155</code> ← قیمتِ هر ⭐ به دلار\n"
        "<code>دلار 165000</code> ← نرخِ دستیِ دلار (تومان)\n"
        "<code>دلار 0</code> ← برگشت به نرخِ خودکار",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.stars_base_in, F.text)
async def sauto_base_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    from tenant import fa_to_int
    t = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬,", "0123456789.. "))
    parts = t.replace("،", " ").split()
    try:
        word, val = parts[0], float(parts[1].replace(" ", ""))
    except Exception:
        await msg.answer("فرمت نامعتبر. مثال: قیمت 0.0155  یا  دلار 165000")
        return
    await state.clear()
    if not row:
        return
    if word.startswith("قیمت") and val > 0:
        await _set_setting(row.id, "stars_base_usd", str(val))
    elif word.startswith("دلار") and val >= 0:
        await _set_setting(row.id, "stars_usd_toman", str(val))
    else:
        await msg.answer("فرمت نامعتبر. مثال: قیمت 0.0155  یا  دلار 165000")
        return
    await msg.answer("✅ ذخیره شد.")
    text, kb = await stars_sell_admin_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "srx:setmargin")
async def srx_setmargin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.srx_margin_in)
    await cb.message.answer("📈 چند درصد سود رویِ نرخِ بازار؟ (فقط عدد، مثال: 60)",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.srx_margin_in, F.text)
async def srx_setmargin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    await state.clear()
    if not row:
        return
    from tenant import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > 1000:
        await msg.answer("عددِ نامعتبر (۰ تا ۱۰۰۰). دوباره از منو امتحان کن.")
        return
    await _set_setting(row.id, "srx_margin", str(val))
    text, kb = await srx_admin_content(row.id)
    await msg.answer("✅ درصدِ سود ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data == "srx:setmin")
async def srx_setmin_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.srx_min_in)
    await cb.message.answer("🔽 حداقلِ تعدادِ ری‌اکشن در هر سفارش چند باشد؟",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.srx_min_in, F.text)
async def srx_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    from tenant import fa_to_int
    val = fa_to_int(raw)
    if not val or val <= 0:
        await state.clear()
        raise SkipHandler
    await state.clear()
    if not row:
        return
    await _set_setting(row.id, "srx_min", str(val))
    text, kb = await srx_admin_content(row.id)
    await msg.answer("✅ حداقل ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("srx:orders:"))
async def srx_orders_list(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    per_page = 10
    async with Session() as s:
        orders = (await s.scalars(select(GiftOrder).where(
            GiftOrder.bot_id == row.id, GiftOrder.gift_id.like("srx_%"),
            GiftOrder.status == "awaiting_admin").order_by(GiftOrder.id.desc()))).all()
    start, end = page * per_page, page * per_page + per_page
    lines = ["🧾 <b>سفارش‌هایِ ری‌اکشنِ استارزیِ در انتظار</b>\n"]
    if not orders:
        lines.append("سفارشِ در انتظاری نیست.")
    for o in orders[start:end]:
        lines.append(f"🔖 {o.tracking_code or '#' + str(o.id)} — {(o.star_count or 0):,}⭐️ — {_html_escape(str(o.dest_label or ''))} — {float(o.price_toman or 0):,.0f} تومان"
                     + (" — 🕶 ناشناس" if o.anonymous else " — 👤 با نام"))      # PATCH_PENDING_LISTS_FIX_V1
    rows = []
    for o in orders[start:end]:
        rows.append([InlineKeyboardButton(text=f"{o.tracking_code or '#' + str(o.id)} — {o.star_count:,}⭐️",
                                          callback_data=f"srx:detail:{o.id}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"srx:orders:{page-1}"))
    if end < len(orders):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"srx:orders:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="srx:menu", style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                   parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("srx:detail:"))
async def srx_order_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not o or o.bot_id != row.id:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"srx:ok:{order_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"srx:no:{order_id}", style=ButtonStyle.DANGER),
    ], [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="srx:orders:0", style=ButtonStyle.DANGER)]])
    await cb.message.edit_text(
        f"#{await order_no(GiftOrder, order_id)} — {(o.star_count or 0):,}⭐️ ری‌اکشن\n"
        f"🔗 پست: {_html_escape(str(o.dest_label or ''))}\n💰 {float(o.price_toman or 0):,.0f} تومان\n"
        + ("🕶 <b>ثبت: ناشناس</b> — نامِ فرستنده را مخفی کن\n" if o.anonymous
           else "👤 <b>ثبت: با نامِ مشتری</b> (ناشناس نیست)\n")
        + f"🔖 کد: {o.tracking_code}",
        reply_markup=kb, parse_mode="HTML")      # PATCH_PENDING_LISTS_FIX_V1: بدونِ HTML تگ‌هایِ <b> خام دیده می‌شد
    await cb.answer()


# ─────────────── فروشگاهِ ری‌اکشنِ استارزی — طرفِ مشتری ───────────────

async def srx_show_shop(bot: Bot, target: Message, bot_id: int, state: "FSMContext | None" = None) -> None:
    rate, qmin = await _srx_settings(bot_id)
    from tenant import smart_cancel_kb
    if state is not None:
        await state.set_state(SmmFSM.srx_qty_in)
    calc_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="💰 محاسبه با موجودی من", callback_data="srx:calc")]])
    await target.answer(
        f"⭐️ <b>ری‌اکشنِ استارزیِ پست</b>\n\n"
        "وقتی پست‌هات با ری‌اکشنِ استارزی بدرخشن!\n\n"
        f"نرخ: هر ⭐️ = <b>{rate:,.0f} تومان</b>\n"
        f"حداقلِ خرید: <b>{qmin:,}</b> ری‌اکشن\n\n"
        "⚠️ کانالت باید عمومی (Public) باشد و قابلیتِ دریافتِ ری‌اکشنِ ⭐️ "
        "رویِ آن پست فعال باشد.\n\n"
        f"چه تعداد ری‌اکشن می‌خواهی؟ (حداقل {qmin:,})",
        parse_mode="HTML", reply_markup=calc_kb)
    # === PATCH_STAR_QTY_CANCEL_UX_FIX_V1 ===
    # باگِ گزارش‌شده: قبلاً این پیامِ دوم فقط یک «👇» تنها بود — چون
    # تلگرام پیامِ تک‌ایموجی را بزرگ نشان می‌دهد، یک شکلِ دستِ درشت و
    # بی‌ربط وسطِ صفحه ظاهر می‌شد که هیچ کاربردی نداشت (فقط بهانه‌ای
    # بود برای چسباندنِ کیبوردِ لغو، چون کیبوردِ ریپلای و دکمهٔ اینلاینِ
    # «محاسبه» نمی‌توانند رویِ یک پیام با هم بیایند). حالا یک متنِ واقعاً
    # مفید («عدد رو همینجا بفرست») اونجاست، نه یک ایموجیِ تنهایِ بی‌معنی.
    await target.answer("🔢 عددِ موردنظر رو همینجا بفرست:", reply_markup=await smart_cancel_kb(bot))


@smm_router.callback_query(F.data == "srx:calc")
async def srx_balance_calc(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    rate, qmin = await _srx_settings(row.id)
    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id)
        balance = float(b.balance) if b else 0.0
    afford = int(balance // rate) if rate > 0 else 0
    if afford < qmin:
        msg = (f"💰 موجودیِ فعلیِ تو: {int(balance):,} تومان\n\n"
              f"با این موجودی هنوز به حداقلِ سفارش ({qmin:,} ری‌اکشن) نمی‌رسی.\n"
              f"برایِ {qmin:,} ری‌اکشن، حدودِ {int(qmin * rate):,} تومان لازم داری.")
    else:
        msg = (f"💰 موجودیِ فعلیِ تو: {int(balance):,} تومان\n\n"
              f"با این موجودی تقریباً می‌تونی <b>{afford:,} ری‌اکشنِ استارزی</b> بخری "
              f"(نرخِ هر ⭐️ = {rate:,.0f} تومان).")
    await cb.answer()
    await cb.message.answer(msg, parse_mode="HTML")


@smm_router.message(SmmFSM.srx_qty_in, F.text)
async def srx_qty_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int
    row = await get_bot_row(bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    qty = fa_to_int(raw)
    rate, qmin = await _srx_settings(row.id)
    if not qty or qty < qmin:
        if not qty:
            await state.clear()
            raise SkipHandler
        await msg.answer(f"❌ حداقل {qmin:,} ری‌اکشن. دوباره بفرست:")
        return
    await state.update_data(srx_qty=qty)
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.srx_link_in)
    await msg.answer(
        "🔗 <b>ارسالِ لینکِ پست</b>\n\n"
        "لطفاً لینکِ پستی که می‌خواهید ری‌اکشن‌هایِ استارزی رویِ آن ثبت شود را ارسال کنید\n\n"
        "⚠️ قبل از ارسالِ لینک، موارد زیر را بررسی کنید:\n"
        "✅ کانالِ شما باید عمومی (Public) باشد.\n"
        "✅ قابلیتِ دریافتِ ری‌اکشنِ ⭐️ رویِ پستِ موردنظر فعال باشد.\n\n"
        "نمونه لینکِ صحیح:\n<code>https://t.me/yourchannel/123</code>",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.srx_link_in, F.text)
async def srx_link_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    data = await state.get_data()
    qty = data.get("srx_qty")
    if not qty:
        await state.clear()
        raise SkipHandler
    # === PATCH_STAR_REACTION_V1 ===
    # اعتبارسنجیِ سبک: باید شکلِ یک لینکِ پستِ تلگرام را داشته باشد
    # (t.me/username/messageid) — اعتبارسنجیِ کاملِ «عمومی‌بودن/فعال‌بودنِ
    # ری‌اکشن» نیاز به فراخوانیِ API دارد که در این پروژه در دسترس نیست؛
    # آن دو شرط را متنی به مشتری هشدار دادیم، تاییدِ نهایی با ادمین است.
    m = re.match(r"^https?://t\.me/([A-Za-z0-9_]{4,32})/(\d+)/?$", raw)
    if not m:
        await msg.answer(
            "❌ این یک لینکِ معتبرِ پستِ تلگرام نیست.\n"
            "نمونه: <code>https://t.me/yourchannel/123</code>\nدوباره بفرست:",
            parse_mode="HTML")
        return
    # === PATCH_SRX_ANON_V1 === قبل از پرداخت می‌پرسیم ناشناس یا با نام
    await state.update_data(srx_link=raw)
    await state.set_state(None)
    await msg.answer(
        "🕶 ری‌اکشن‌ها <b>با نامِ تو</b> ثبت شوند یا <b>ناشناس</b>؟\n"
        "(ناشناس: ادمین هنگامِ ثبت، گزینهٔ مخفی‌بودنِ نام را می‌زند)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 با نامِ من", callback_data="srx:an:0", style=ButtonStyle.PRIMARY),
             InlineKeyboardButton(text="🕶 ناشناس", callback_data="srx:an:1", style=ButtonStyle.SUCCESS)]]))


@smm_router.callback_query(F.data.startswith("srx:an:"))
async def srx_anon_choice(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    anon = cb.data.endswith(":1")
    data = await state.get_data()
    qty, raw = data.get("srx_qty"), data.get("srx_link")
    if not qty or not raw:
        await state.clear()
        return await cb.answer("منقضی شد؛ دوباره از اول شروع کن.", show_alert=True)
    await state.clear()
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    msg = cb.message
    _fu = cb.from_user
    rate, qmin = await _srx_settings(row.id)
    price = int(round(qty * rate))
    buyer, _ = await get_or_create_user(
        row.id, _fu.id, _fu.full_name, _fu.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
        # *قبل* از بررسیِ موجودی، وگرنه کاربری که با تخفیف پولش می‌رسید
        # اشتباهاً «موجودی ناکافی» می‌گیرد.
        _note_extra = ""
        from tenant.gateways.discount_engine import first_purchase_discount
        _fd = await first_purchase_discount(
            bot_id=row.id, telegram_id=_fu.id, price=int(price), session=s)
        if _fd.ok and _fd.discount > 0:
            _note_extra = f" (تخفیف اولین خرید −{_fd.discount:,})"
            price = _fd.final_price
        if float(b.balance) < price:
            await msg.answer(f"❌ موجودیِ کیف‌پول کافی نیست. کمبود: {price - int(b.balance):,} تومان")
            return
        b.balance = float(b.balance) - price
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ {qty:,} ری‌اکشنِ استارزی{_note_extra}"))
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=f"srx_{trk}",
                          gift_emoji="⭐️", star_count=qty, price_toman=price, quantity=1,
                          dest_telegram_id=0, dest_label=raw,
                          status="awaiting_admin", pay_method="wallet", tracking_code=trk,
                          anonymous=anon)
        s.add(order)
        await s.commit()
        order_id = order.id

    await _notify_gift_order_trust_success(bot, row, order_id)
    await msg.answer(
        f"✅ پرداختت ثبت شد — سفارشت برایِ ثبتِ دستیِ ری‌اکشن فرستاده شد.\n"
        f"⭐️ تعداد: {qty:,}\n🔗 پست: {raw}\n" + ("🕶 ثبت: ناشناس\n" if anon else "👤 ثبت: با نامِ تو\n") +
        f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"srx:ok:{order_id}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"srx:no:{order_id}",
                             style=ButtonStyle.DANGER),
    ]])
    await _notify_manual_order(
        bot, row,
        f"⭐️ سفارشِ ری‌اکشنِ استارزی — #{await order_no(GiftOrder, order_id)}\n"
        f"تعداد: {qty:,} ری‌اکشن\n💰 {price:,} تومان\n"
        f"🔗 پست: {raw}\n"
        + ("🕶 <b>ثبت: ناشناس</b> — هنگامِ ثبت، گزینهٔ مخفی‌کردنِ نام را بزن\n" if anon
           else "👤 <b>ثبت: با نامِ مشتری</b> (ناشناس نیست)\n")
        + f"🔖 کد: {trk}\n\n"
        f"باید خودت دستی ری‌اکشن‌ها را رویِ این پست ثبت کنی، بعد «✅ انجام شد» را بزن.",
        admin_kb)


@smm_router.callback_query(F.data.startswith("srx:ok:"))
async def srx_admin_done(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "sent"
        buyer = await s.get(BotUser, o.buyer_user_id)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        qty = o.star_count
        link = o.dest_label
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await cb.answer("✅ ثبت شد")
    await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} انجام‌شده ثبت شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n⭐ تعداد {qty:,} ری‌اکشنِ استارزی رویِ پست ثبت شد.\n"
                f"🔗 {link}\n🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@smm_router.callback_query(F.data.startswith("srx:no:"))
async def srx_admin_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "rejected"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        if buyer:
            buyer.balance = float(buyer.balance) + float(o.price_toman)
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=o.price_toman, kind="charge",
                            note="بازگشتِ وجهِ سفارشِ ری‌اکشنِ استارزیِ ردشده"))
        await s.commit()
    await cb.answer("سفارش رد و وجه بازگردانده شد")
    await _admin_msg_close(cb, f"❌ سفارش #{await order_no(GiftOrder, order_id)} رد شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"❌ متاسفانه سفارشِ ری‌اکشنِ استارزی‌ات رد شد (احتمالاً پست/کانال شرایطِ لازم "
                f"را نداشت).\nمبلغِ سفارش به کیف‌پولت بازگردانده شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ─────────────── فروشگاهِ استارز — طرفِ مشتری ───────────────

async def show_stars_shop_to_user(bot: Bot, target: Message, bot_id: int, state: "FSMContext | None" = None) -> None:
    rate, qmin, qmax = await _stars_sell_settings(bot_id)
    from tenant import smart_cancel_kb
    if state is not None:
        await state.set_state(SmmFSM.stars_qty_in)
    # یک پیامِ واحد برای نرخ، بازه و دریافتِ تعداد؛ فقط لغو و بازگشت به خانه
    # زیرِ همین پیام نمایش داده می‌شوند تا چت با پیامِ دوم شلوغ نشود.
    await target.answer(
        f"⭐ <b>خریدِ استارزِ تلگرام</b>\n\n"
        f"نرخ: هر ⭐ = <b>{rate:,.0f} تومان</b>\n"
        f"چه تعداد استارز می‌خواهی؟ (بینِ {qmin:,} تا {qmax:,})\n"
        f"عدد مورد نظرت رو بفرست:",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.callback_query(F.data == "ssl:calc")
async def ssl_balance_calc(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    rate, qmin, qmax = await _stars_sell_settings(row.id)
    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id)
        balance = float(b.balance) if b else 0.0
    afford = int(balance // rate) if rate > 0 else 0
    if afford < qmin:
        msg = (f"💰 موجودیِ فعلیِ تو: {int(balance):,} تومان\n\n"
              f"با این موجودی هنوز به حداقلِ سفارش ({qmin:,} استارز) نمی‌رسی.\n"
              f"برایِ {qmin:,} استارز، حدودِ {int(qmin * rate):,} تومان لازم داری.")
    else:
        msg = (f"💰 موجودیِ فعلیِ تو: {int(balance):,} تومان\n\n"
              f"با این موجودی تقریباً می‌تونی <b>{afford:,} استارز</b> بخری "
              f"(نرخِ هر ⭐ = {rate:,.0f} تومان).")
    await cb.answer()
    await cb.message.answer(msg, parse_mode="HTML")


@smm_router.message(SmmFSM.stars_qty_in, F.text)
async def stars_qty_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int
    row = await get_bot_row(bot)
    if not row:
        return
    qty = fa_to_int(msg.text)
    rate, qmin, qmax = await _stars_sell_settings(row.id)
    if not qty or qty < qmin or qty > qmax:
        await msg.answer(f"❌ عدد باید بینِ {qmin:,} تا {qmax:,} باشد. دوباره بفرست:")
        return
    await state.update_data(stars_qty=qty)
    # === PATCH_STARS_SELL_V1 ===
    # بعدِ اینجا منتظرِ زدنِ دکمه‌ایم (callback)، نه پیامِ متنیِ دیگر —
    # پس state را پاک می‌کنیم (فقط نشانگرِ حالت، نه دیتا) تا اگر مشتری
    # به‌جایِ زدنِ دکمه دوباره پیام بفرستد، دوباره در همین هندلر گیر
    # نکند.
    await state.set_state(None)
    price = round(qty * rate)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 خرید برای خودم", callback_data="star:dself")],
        [InlineKeyboardButton(text="👤 برایِ کاربرِ دیگر (یوزرنیم)", callback_data="star:dask")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="star:back_shop", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"⭐ {qty:,} استارز — {price:,} تومان\n\n"
        f"این استارز‌ها را برایِ چه کسی می‌خری؟\n\n"
        "⚠️ توجه: برایِ «کاربرِ دیگر»، آن شخص باید یوزرنیمِ عمومی داشته باشد "
        "(چون این خرید با فرگمنت انجام می‌شود، نه از طریقِ ربات).",
        reply_markup=kb)


@smm_router.callback_query(F.data == "star:back_shop")
async def stars_back_shop(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await state.clear()
    await cb.answer()
    await show_stars_shop_to_user(bot, _EditFirstV(cb.message), row.id, state)


@smm_router.callback_query(F.data == "star:dself")
async def stars_dest_self(cb: CallbackQuery, bot: Bot, state: FSMContext):
    data = await state.get_data()
    qty = data.get("stars_qty")
    if not qty:
        await cb.answer("ابتدا تعداد را وارد کن.", show_alert=True)
        return
    await cb.answer()
    await _stars_show_invoice(_EditFirstV(cb.message), bot, cb.from_user.id, "خودم", qty)


@smm_router.callback_query(F.data == "star:dask")
async def stars_dest_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.stars_dest_username)
    await cb.answer()
    await _EditFirstV(cb.message).answer("👤 یوزرنیمِ مقصد را بفرست (با یا بدونِ @):",
                            reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.stars_dest_username, F.text)
async def stars_dest_username_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    data = await state.get_data()
    qty = data.get("stars_qty")
    if not qty:
        await state.clear()
        await msg.answer("سفارش پیدا نشد. دوباره از منو شروع کن.")
        return
    uname = msg.text.strip().lstrip("@")
    async with Session() as s:
        target_user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.username.ilike(uname)))
    await state.clear()
    # === PATCH_STARS_UNSTARTED_DEST_V1 ===
    # قبلاً اگر مقصد توی BotUser پیدا نمی‌شد (یعنی این ربات را استارت
    # نکرده)، سفارش رد می‌شد. این محدودیت این‌جا لازم نبود: تحویلِ این
    # بخش از اول دستیه (ادمین خودش از فرگمنت می‌فرسته)، هیچ‌جا از
    # user_id برای صداکردنِ API تلگرام استفاده نمی‌شود — فقط برای
    # نمایش لازم بود. حالا اگر پیدا نشد، با dest_id=0 و خودِ یوزرنیم
    # جلو می‌رویم؛ ادمین یوزرنیم را می‌بیند و می‌تواند دستی بفرستد.
    if target_user:
        await _stars_show_invoice(msg, bot, target_user.telegram_id, f"@{uname}", qty)
    else:
        await _stars_show_invoice(msg, bot, 0, f"@{uname}", qty, dest_username=uname)


async def _stars_show_invoice(target: Message, bot: Bot, dest_id: int, dest_label: str, qty: int,
                              dest_username: str = "") -> None:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    rate, _, _ = await _stars_sell_settings(row.id)
    price = round(qty * rate)
    # === PATCH_STARS_UNSTARTED_DEST_V1 ===
    # وقتی مقصد شناخته‌شده نیست (dest_id=0)، یوزرنیم را هم توی
    # callback_data می‌بریم تا مرحلهٔ پرداخت بدونِ نیاز به state بتونه
    # دوباره بسازدش.
    uname_part = f":{dest_username}" if dest_id == 0 and dest_username else ""
    note = ("\n\n⚠️ این شخص ربات را استارت نکرده — سفارش ثبت می‌شود ولی "
            "ارسال کاملاً دستی خواهد بود." if dest_id == 0 else "")
    from tenant.bots.virtual_service.vs_discount import vsd_rows   # PATCH_VS_DISCOUNT_V1
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1 — تیک‌هایِ مالک + شارژ حساب
    _pay_rows = await _vp.pay_rows(row.id, _vp.tg_of(target), price, f"star:pay:{dest_id}:{qty}{uname_part}",
                                   f"star:cardpay:{dest_id}:{qty}{uname_part}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *_pay_rows,
        *await vsd_rows(row.id, "stars", price),
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="star:back_shop", style=ButtonStyle.DANGER)],
    ])
    await target.answer(
        f"🧾 <b>فاکتورِ خرید</b>\n\n"
        f"کالا: ⭐ {qty:,} استارز\n"
        f"مقصد: {dest_label}\n"
        f"قیمت: {price:,} تومان{note}\n\n"
        f"روشِ پرداخت را انتخاب کن:" + ("" if _pay_rows else _vp.NO_METHOD_NOTE),
        reply_markup=kb, parse_mode="HTML")


async def _stars_admin_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"star:done:{order_id}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"star:reject:{order_id}",
                             style=ButtonStyle.DANGER),
    ]]
    # === PATCH_TGTOOLS_STARS_V1 === دکمهٔ ارسالِ خودکار وقتی کلیدِ TGTools ثبت شده
    _o = None   # PATCH_FRAGMENT_AUTO_STARS_V1
    try:
        async with Session() as s:
            _o = await s.get(GiftOrder, order_id)
        if _o and await _get_setting(_o.bot_id, "tgtools_api_key", ""):
            rows.append([InlineKeyboardButton(text="🤖 ارسال خودکار با TGTools",
                                              callback_data=f"star:tgt:{order_id}",
                                              style=ButtonStyle.PRIMARY)])
    except Exception:
        logger.exception("tgtools: ساختِ دکمهٔ ارسال خودکار ناموفق")
    # === PATCH_FRAGMENT_AUTO_STARS_V1 === دکمهٔ ارسال با ولتِ فرگمنت وقتی کاملاً وصل است
    try:
        if _o and await _fg_wallet(_o.bot_id) is not None:
            rows.append([InlineKeyboardButton(text="⭐ ارسال خودکار با ولتِ فرگمنت",
                                              callback_data=f"star:fgt:{order_id}",
                                              style=ButtonStyle.PRIMARY)])
    except Exception:
        logger.exception("fragment-auto: ساختِ دکمهٔ ارسال ناموفق")
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_VS_MINIAPP_ORDER_V1 ===
# منطقِ ثبتِ سفارشِ استارز از داخلِ هندلرِ کال‌بک بیرون کشیده شد تا مینی‌اپ
# هم بتواند دقیقاً همین را صدا بزند. عمداً بازنویسی نشد: اگر دو نسخهٔ جدا
# از «کسرِ پول + ثبتِ سفارش + خبر به ادمین» داشته باشیم، روزی یکی‌شان وصله
# می‌شود و آن یکی نه — و چون پایِ پولِ مشتری وسط است، آن باگ گران تمام
# می‌شود. هندلرِ ربات هم از همین تابع استفاده می‌کند.
class StarsOrderError(Exception):
    """خطایی که پیامش مستقیم به کاربر نشان داده می‌شود.

    short/price (PATCH_VS_PAY_V1): فقط وقتی «موجودی کافی نیست» است پر می‌شود تا ربات دکمهٔ «شارژ حساب و ثبت سفارش» بدهد."""

    def __init__(self, msg: str = "", short: int = 0, price: int = 0):
        super().__init__(msg)
        self.short = int(short or 0)
        self.price = int(price or 0)


async def place_stars_order(bot: Bot, row, *, buyer_tg_id: int, buyer_name: str,
                            buyer_username: str, dest_id: int, qty: int,
                            self_user=None, dest_username: str = "",
                            price_override: int | None = None,
                            disc_code: str | None = None) -> dict:   # PATCH_VS_DISCOUNT_V1
    """کسرِ پول، ثبتِ سفارش و اطلاع‌رسانی. هم از ربات و هم از مینی‌اپ.

    price_override (PATCH_API_SPECIAL_PRODUCTS_V1): قیمتِ نهایی برایِ وب‌سرویس
    (با تخفیفِ اختصاصیِ کلیدِ API)؛ بقیهٔ مسیر دقیقاً همین است.

    خروجی: {order_id, tracking, price, dest_label}
    خطا: StarsOrderError با پیامِ فارسیِ قابلِ نمایش.
    """
    from tenant import get_or_create_user, _gen_tracking, _notify_admins

    rate, qmin, qmax = await _stars_sell_settings(row.id)
    if qty < qmin or qty > qmax:
        raise StarsOrderError(f"تعداد باید بینِ {qmin:,} تا {qmax:,} باشد.")
    price = round(qty * rate) if price_override is None else int(price_override)

    buyer, _ = await get_or_create_user(
        row.id, buyer_tg_id, buyer_name, buyer_username or "", return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_VS_DISCOUNT_V1 === کدِ تخفیف: اعتبارسنجیِ دوباره + ثبتِ مصرف در همین تراکنش
        _dc_id, _dc_amt = None, 0
        if disc_code:
            from tenant.bots.virtual_service.vs_discount import vsd_charge, vsd_record, VsdError
            try:
                price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=buyer_tg_id, code=disc_code, kind="stars", price=price)
            except VsdError as _e:
                raise StarsOrderError(str(_e))
            await vsd_record(s, bot_id=row.id, tg_id=buyer_tg_id, code_id=_dc_id, discount=_dc_amt)
        if float(b.balance) < price:
            raise StarsOrderError(
                f"موجودیِ کیف‌پول کافی نیست. کمبود: {price - int(b.balance):,} تومان",
                short=price - int(b.balance), price=price)
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"خریدِ {qty:,} استارز" + (f" (کد تخفیف −{_dc_amt:,})" if _dc_amt else "")))
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        # PATCH_SELF_DEST_LABEL_V1
        if dest_id == buyer_tg_id and self_user is not None:
            dest_label = _self_dest_label(self_user)
        elif dest_id == buyer_tg_id:
            dest_label = _self_dest_label(types.SimpleNamespace(
                id=buyer_tg_id, username=buyer_username or None))
        elif dest_id == 0 and dest_username:
            # === PATCH_STARS_UNSTARTED_DEST_V1 ===
            dest_label = (f"👤 یوزرنیم: @{dest_username}\n"
                          "⚠️ این شخص ربات را استارت نکرده — ارسال کاملاً دستی است.")
        else:
            dest_label = await _resolve_dest_label(row.id, dest_id)
        order = GiftOrder(bot_id=row.id, buyer_user_id=b.id, gift_id=f"starsell_{qty}",
                          gift_emoji="⭐", star_count=qty, price_toman=price,
                          dest_telegram_id=dest_id, dest_label=dest_label,
                          status="awaiting_admin", pay_method="wallet", tracking_code=trk,
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(order)
        await s.commit()
        order_id = order.id

    await _notify_gift_order_trust_success(bot, row, order_id)
    await _notify_manual_order(
        bot, row,
        f"⭐ سفارشِ استارز (پرداختِ کیف‌پول) — #{await order_no(GiftOrder, order_id)}\n"
        f"تعداد: {qty:,} استارز\n💰 {price:,} تومان\n🎯 مقصد: {dest_label}\n"
        f"🔖 کد: {trk}\n\nاین استارز را باید خودت واقعاً بفرستی (مثلاً از فرگمنت)، "
        f"بعد «✅ انجام شد» را بزن.", await _stars_admin_kb(order_id))
    # === PATCH_TGTOOLS_STARS_V1 === ارسالِ خودکار (اگر روشن باشد)
    try:
        if ((await _get_setting(row.id, "tgtools_auto", "0")) == "1"
                and await _get_setting(row.id, "tgtools_api_key", "")):
            _tgt_spawn(bot, row, order_id)
        elif await _fg_auto_on(row.id):   # PATCH_FRAGMENT_AUTO_STARS_V1 — اگر TGTools خودکار نیست، ولتِ فرگمنت
            _fg_spawn(bot, row, order_id)
    except Exception:
        logger.exception("tgtools: شروعِ ارسالِ خودکار ناموفق")
    return {"order_id": order_id, "tracking": trk, "price": price,
            "dest_label": dest_label}


@smm_router.callback_query(F.data.startswith("star:pay:"))
async def star_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_clear   # PATCH_VS_DISCOUNT_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    dest_id, qty = int(parts[2]), int(parts[3])
    dest_username = parts[4] if len(parts) > 4 else ""  # PATCH_STARS_UNSTARTED_DEST_V1
    from tenant.shared import vs_pay as _vp   # PATCH_VS_PAY_V1
    if not await _vp.wallet_pay_allowed(row.id):
        await cb.answer("❌ پرداخت از کیف‌پول در این ربات غیرفعال است.", show_alert=True)
        return
    try:
        res = await place_stars_order(
            bot, row, buyer_tg_id=cb.from_user.id, buyer_name=cb.from_user.full_name,
            buyer_username=cb.from_user.username or "", dest_id=dest_id, qty=qty,
            self_user=cb.from_user, dest_username=dest_username,
            disc_code=vsd_pick(await state.get_data(), cb.message.message_id, "stars"))   # PATCH_VS_DISCOUNT_V1
    except StarsOrderError as e:
        if e.short:      # PATCH_VS_PAY_V1 — «شارژ حساب و ثبت سفارش»
            await cb.answer("❌ موجودی کافی نیست.")
            await _vp.insufficient_notice(cb.message, row.id, cb.from_user.id, e.price, cb.data)
            return
        await cb.answer(f"❌ {e}", show_alert=True)
        return
    await vsd_clear(state)
    await _delete_shop_panel(cb)
    await cb.answer("✅ پرداخت شد")
    _t2, _k2 = await _with_sfirst(
        row.id,
        f"✅ پرداختت ثبت شد — سفارشت برایِ ادمین فرستاده شد.\n"
        f"🔖 کد پیگیری: <code>{res['tracking']}</code>",
        await _gift_support_kb(res["tracking"]))
    await cb.message.answer(_t2, parse_mode="HTML", reply_markup=_k2)


# === PATCH_SELF_DEST_LABEL_V1 ===
# باگ: قبلاً وقتی خریدار برایِ خودش سفارش می‌داد، dest_label فقط
# "خودم" بود — بدونِ آیدی/یوزرنیم — و ادمین هیچ راهی برایِ شناساییِ
# مقصدِ واقعی نداشت. حالا آیدی/یوزرنیمِ واقعیِ خریدار هم اضافه می‌شود.
def _self_dest_label(user) -> str:
    # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
    # درخواستِ ادمین: یوزرنیم زیرِ آیدیِ عددی، به‌صورتِ یک خطِ جدا نشان
    # داده شود (نه توی پرانتزِ همون خط) — خواناتر است، مخصوصاً وقتی چند
    # سفارش پشتِ‌سرِ‌هم داخلِ کانالِ اطلاع‌رسانیِ ادمین می‌آید.
    lines = ["خودم", f"🆔 آیدی: {user.id}"]
    if getattr(user, "username", None):
        lines.append(f"👤 یوزرنیم: @{user.username}")
    else:
        lines.append("👤 یوزرنیم: ندارد")
    return "\n".join(lines)


async def _resolve_dest_label(bot_id: int, dest_id: int) -> str:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == dest_id))
    # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
    if dest_id < 0:     # PATCH_GIFT_ANY_DEST_V1 — جانشینِ گیرندهٔ بدونِ استارت: آیدیِ ساختگی را نشان نده
        return (f"@{u.username} (گیرندهٔ بدونِ استارت — با یوزرنیم بفرست)" if u and u.username else "گیرندهٔ ناشناخته")
    lines = [f"🆔 آیدی: {dest_id}"]
    if u and u.full_name:
        lines.insert(0, u.full_name)
    if u and u.username:
        lines.append(f"👤 یوزرنیم: @{u.username}")
    else:
        lines.append("👤 یوزرنیم: ندارد")
    return "\n".join(lines)


@smm_router.callback_query(F.data.startswith("star:cardpay:"))
async def star_cardpay_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_or_create_user, get_setting, _gen_tracking, smart_cancel_kb
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, VsdError   # PATCH_VS_DISCOUNT_V1
    _disc_code = vsd_pick(await state.get_data(), cb.message.message_id, "stars")
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    dest_id, qty = int(parts[2]), int(parts[3])
    dest_username = parts[4] if len(parts) > 4 else ""  # PATCH_STARS_UNSTARTED_DEST_V1
    rate, qmin, qmax = await _stars_sell_settings(row.id)
    if qty < qmin or qty > qmax:
        await cb.answer("این سفارش دیگر معتبر نیست.", show_alert=True)
        return
    price = round(qty * rate)
    card_num = await get_setting(row.id, "card_number", "")
    card_holder = await get_setting(row.id, "card_holder", "")
    if not card_num:
        await cb.answer("❌ پرداختِ کارت‌به‌کارت هنوز تنظیم نشده — از کیف‌پول استفاده کن.", show_alert=True)
        return
    # === PATCH_VS_PAY_V1 === تیکِ کارت‌به‌کارتِ خرید + سقفِ احرازِ هویت *قبل* از نمایشِ شماره‌کارت
    from tenant.shared import vs_pay as _vp
    if not await _vp.card_pay_allowed(row.id):
        await cb.answer("❌ پرداختِ کارت‌به‌کارت در این ربات غیرفعال است.", show_alert=True)
        return
    _blk = await _vp.kyc_block(row.id, cb.from_user.id, price, "star:back_shop")
    if _blk:
        await cb.answer()
        await cb.message.answer(_blk[0], reply_markup=_blk[1])
        return

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    from models import next_tracking as _next_tracking  # PATCH_SEQ_TRACKING_V1
    trk = await _next_tracking(row.id)
    # PATCH_SELF_DEST_LABEL_V1 / PATCH_STARS_UNSTARTED_DEST_V1
    if dest_id == cb.from_user.id:
        dest_label = _self_dest_label(cb.from_user)
    elif dest_id == 0 and dest_username:
        dest_label = (f"👤 یوزرنیم: @{dest_username}\n"
                      "⚠️ این شخص ربات را استارت نکرده — ارسال کاملاً دستی است.")
    else:
        dest_label = await _resolve_dest_label(row.id, dest_id)
    async with Session() as s:
        # === PATCH_VS_DISCOUNT_V1 === قیمتِ کارت‌به‌کارت = قیمتِ بعد از کد؛ مصرفِ کد هم همین‌جا ثبت می‌شود
        # (اگر ادمین رسید را رد کند، همین مصرف در star:reject برگردانده می‌شود)
        _dc_id, _dc_amt = None, 0
        if _disc_code:
            try:
                price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_disc_code, kind="stars", price=price)
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
        order = GiftOrder(bot_id=row.id, buyer_user_id=buyer.id, gift_id=f"starsell_{qty}",
                          gift_emoji="⭐", star_count=qty, price_toman=price,
                          dest_telegram_id=dest_id, dest_label=dest_label,
                          status="awaiting_receipt", pay_method="card", tracking_code=trk,
                          discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))
        s.add(order)
        await s.commit()
        order_id = order.id

    await state.set_state(SmmFSM.stars_receipt_photo)
    await state.update_data(stars_order_id=order_id, vsd=None, vsd_pending=None)
    await _delete_shop_panel(cb)
    await cb.answer()
    holder_line = f"\n👤 به‌نامِ: {card_holder}" if card_holder else ""
    from tenant.gateways.card_display import extra_card_block   # PATCH_CARD_BOTH_V1
    _xb = await extra_card_block(row.id)
    if _xb:
        holder_line += "\n" + _xb.rstrip("\n")
    await cb.message.answer(
        f"💳 <b>پرداختِ کارت‌به‌کارت</b>\n\n"
        f"مبلغ: {price:,} تومان\n"
        f"شمارهٔ کارت: <code>{card_num}</code>{holder_line}\n\n"
        f"🔖 کدِ پیگیریِ سفارشت: <code>{trk}</code>\n\n"
        # === PATCH_CARD_LAST4_V1 ===
        f"بعدِ واریز، عکسِ رسید را همین‌جا بفرست و در کپشنِ همان عکس، "
        f"۴ رقمِ آخرِ کارتی که ازش پرداخت کردی را بنویس (مثلاً: 1234):",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@smm_router.message(SmmFSM.stars_receipt_photo, F.photo)
async def star_receipt_photo_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, _notify_admins
    data = await state.get_data()
    order_id = data.get("stars_order_id")
    row = await get_bot_row(bot)
    if not row or not order_id:
        await state.clear()
        return
    # === PATCH_CARD_LAST4_V1 ===
    last4 = _extract_card_last4(msg.caption)
    if not last4:
        await msg.answer(
            "❌ در کپشنِ عکس، ۴ رقمِ آخرِ کارتی که ازش پرداخت کردی پیدا نشد.\n\n"
            "لطفاً همین عکسِ رسید را دوباره بفرست و این‌بار در کپشنِ آن، "
            "۴ رقمِ آخرِ کارت را بنویس (مثلاً: 1234).")
        return
    await state.clear()
    photo_id = msg.photo[-1].file_id
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await msg.answer("سفارش پیدا نشد.")
            return
        o.receipt_photo_id = photo_id
        o.payer_card_last4 = last4
        o.status = "awaiting_admin"
        trk = o.tracking_code
        price = o.price_toman
        qty = o.star_count
        dest_label = o.dest_label
        _buyer_uid = o.buyer_user_id   # PATCH_VS_PAY_V1
        await s.commit()
    from tenant.shared import kyc as _K   # PATCH_VS_PAY_V1 — مسدودسازیِ خودکارِ سطح ۱ بعد از پرداختِ واقعی
    await _K.after_charge(row.id, _buyer_uid)

    _rt2, _rk2 = await _with_sfirst(   # PATCH_SUPPORT_FIRST_V1 — فروشِ استارز همیشه دستی است
        row.id,
        f"✅ رسید دریافت شد و برایِ تاییدِ ادمین فرستاده شد.\n"
        f"🔖 کد پیگیری: <code>{trk}</code>", await _gift_support_kb(trk))
    await msg.answer(_rt2, parse_mode="HTML", reply_markup=_rk2)
    await _notify_manual_order(
        bot, row,
        f"💳 رسیدِ کارت‌به‌کارتِ استارز — #{await order_no(GiftOrder, order_id)}\n"
        f"تعداد: {qty:,} استارز\n💰 {price:,} تومان\n"
        f"💳 ۴ رقمِ آخرِ کارتِ پرداخت‌کننده: <code>{last4}</code>\n"
        f"🎯 مقصد: {dest_label}\n"
        f"🔖 کد: {trk}", await _stars_admin_kb(order_id), photo_file_id=photo_id)


# === PATCH_ADMIN_MSG_CLOSE_V1 ===
async def _notify_manual_order(bot: Bot, row, text: str, kb=None, photo_file_id=None) -> None:
    """=== PATCH_MANUAL_ORDER_CHANNEL_V1 === سفارشِ دستی → کانالِ سفارشاتِ دستی (یا پیامِ خصوصیِ ادمین)."""
    from tenant import _notify_manual_order as _f
    await _f(bot, row, text, kb, photo_file_id=photo_file_id)


async def _channel_post_mark_done(cb, text: str) -> None:
    import html as _h
    who = cb.from_user.full_name if cb.from_user else ""
    line = f"\n\n{_h.escape(text)}" + (f" — {_h.escape(who)}" if who else "")
    try:
        if cb.message.photo or cb.message.document:
            await cb.message.edit_caption(
                caption=((cb.message.html_text or "") + line)[:1024], reply_markup=None, parse_mode="HTML")
        else:
            await cb.message.edit_text((cb.message.html_text or "") + line,
                                       reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.info("به‌روزرسانیِ پستِ کانالِ سفارش ممکن نشد؛ فقط دکمه‌ها برداشته می‌شود")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.info("برداشتنِ دکمه‌هایِ پستِ کانال ممکن نشد")


async def _admin_msg_close(cb, text: str) -> None:
    """بعد از «انجام شد/رد»، همان پیامِ سفارش در چتِ ادمین پاک می‌شود (نه اینکه پیامِ تازه برسد و قبلی بماند).
    اگر حذف ممکن نبود (پیامِ قدیمی)، دکمه‌ها برداشته و متنِ نتیجه فرستاده می‌شود.
    در کانالِ سفارشات پست پاک نمی‌شود: نتیجه زیرش نوشته و دکمه‌ها برداشته می‌شود
    تا بقیهٔ تیم ببینند رسیدگی شده (PATCH_MANUAL_ORDER_CHANNEL_V1)."""
    if getattr(getattr(cb.message, "chat", None), "type", None) == "channel":
        await _channel_post_mark_done(cb, text)
        return
    try:
        await cb.message.delete()
        return
    except Exception:
        logger.info("حذفِ پیامِ سفارش از چتِ ادمین ممکن نشد؛ فقط دکمه‌ها برداشته شد")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.info("برداشتنِ دکمه‌هایِ پیامِ سفارش ممکن نشد")
    await cb.message.answer(text)


@smm_router.callback_query(F.data.startswith("star:done:"))
async def star_admin_done(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "sent"
        buyer = await s.get(BotUser, o.buyer_user_id)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        qty = o.star_count
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await cb.answer("✅ ثبت شد")
    await _admin_msg_close(cb, f"✅ سفارش #{await order_no(GiftOrder, order_id)} انجام‌شده ثبت شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n⭐ تعداد {qty:,} استارز با موفقیت ارسال شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@smm_router.callback_query(F.data.startswith("star:reject:"))
async def star_admin_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "rejected"
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
        await vsd_revert_order(s, o, buyer_tg_id)
        # === PATCH_STARS_REJECT_REFUND_V1 ===
        # باگ: ردِ سفارشِ استارزِ پرداخت‌شده با کیف‌پول فقط وضعیت را عوض می‌کرد و
        # پولِ کسرشده برنمی‌گشت (پرمیوم و ری‌اکشن این را داشتند، استارز نه).
        # کارت‌به‌کارت از کیف‌پول کم نشده، پس برایش برگشتی نیست.
        refunded = (o.pay_method or "wallet") == "wallet"
        if refunded and buyer:
            refund_amount = int(o.price_toman) * int(o.quantity or 1)
            buyer.balance = float(buyer.balance) + float(refund_amount)
            s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=refund_amount, kind="charge",
                            note="بازگشتِ وجهِ سفارشِ استارزِ ردشده"))
        await s.commit()
    await cb.answer("سفارش رد و وجه بازگردانده شد" if refunded else "رد شد")
    await _admin_msg_close(cb, f"❌ سفارش #{await order_no(GiftOrder, order_id)} رد شد.")
    if buyer_tg_id:
        try:
            refund_line = "مبلغِ سفارش به کیف‌پولت بازگردانده شد.\n" if refunded else ""
            reason_line = "متاسفانه سفارشت رد شد." if refunded else "متاسفانه رسیدت تایید نشد."
            await bot.send_message(
                buyer_tg_id,
                f"❌ {reason_line}\n{refund_line}🔖 کد پیگیری: <code>{trk}</code>\n\n"
                f"اگر فکر می‌کنی اشتباهی رخ داده، از دکمهٔ پشتیبانی پیام بده.",
                parse_mode="HTML", reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════════════════════════
#  ارسالِ خودکارِ استارز با TGTools — PATCH_TGTOOLS_STARS_V1
# ═══════════════════════════════════════════
# جریان: سفارشِ استارز (status=awaiting_admin) → «ادعا» می‌شود
# (auto_processing، تا ادمین همزمان دستی نفرستد) → یوزرنیم از dest_label
# → بررسیِ کاربر در TGTools → POST /api/purchase/stars با trackingCode
# → پولِ وضعیت تا Completed/Failed.
#   Completed → sent + خبر به مشتری.   Failed / هر خطایِ قطعی → برمی‌گردد به
#   awaiting_admin (تحویلِ دستیِ قبلی) و ادمین خبردار می‌شود.
# پاسخِ نامشخص (timeout بعد از ارسال) هم برمی‌گردد به دستی، ولی با هشدارِ
# «اول در سایتِ TGTools چک کن» تا استارز دوبار نرود. هیچ retryِ خودکاریِ
# «خرید» وجود ندارد.

_tgt_running: set[int] = set()
_tgt_tasks: set = set()
_TGT_POLL_DELAYS = [4, 4, 6, 6, 8, 10, 10, 15, 15, 20, 20, 30, 30, 30, 30, 30, 30, 30, 30]  # ≈ ۶ دقیقه


def _tgt_code(bot_id: int, order_id: int) -> str:
    return f"b{bot_id}-o{order_id}"


async def _tgt_key(bot_id: int) -> str:
    enc = await _get_setting(bot_id, "tgtools_api_key", "")
    if not enc:
        return ""
    try:
        from wallet_crypto import decrypt_secret
        return decrypt_secret(enc)
    except Exception:
        logger.exception("tgtools: رمزگشاییِ کلید ناموفق بود")
        return ""


def _tgt_mask(key: str) -> str:
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else "…"


def _tgt_spawn(bot: Bot, row, order_id: int, by_admin: bool = False) -> None:
    t = asyncio.create_task(tgtools_fulfil_order(bot, row, order_id, by_admin=by_admin))
    _tgt_tasks.add(t)
    t.add_done_callback(_tgt_tasks.discard)


async def _tgt_refund_cancelled(bot: Bot, row, order_id: int, reason: str, what: str) -> bool:
    """=== PATCH_TGTOOLS_CANCEL_REFUND_V1 ===
    گزارش فدایی: «وقتی TGTools سفارش استارز را لغو می‌کند پیام لغو برای مشتری نمی‌رود و پولش برنمی‌گردد».
    قبلا هر رد TGTools فقط سفارش را به «در انتظار ادمین» برمی‌گرداند و هیچ خبری به مشتری نمی‌رفت؛ اگر ادمین
    هم متوجه نمی‌شد، مشتری برای همیشه منتظر می‌ماند.
    حالا وقتی TGTools سفارشی را که پذیرفته بود «Failed/لغو» اعلام می‌کند (طبق مستند پول به کیف‌پول TGTools برمی‌گردد،
    پس سفارش قطعا انجام نشده): وجه کیف‌پول مشتری برمی‌گردد، وضعیت «بازگشت وجه» می‌شود، به مشتری خبر می‌رسد
    (متن لغو تنظیم‌شده توسط مالک اعمال می‌شود) و ادمین هم باخبر می‌شود. فقط:
      • پرداخت کیف‌پولی (کارت‌به‌کارت از کیف‌پول کم نشده؛ همان مسیر دستی)،
      • وقتی هنوز چیزی تحویل نشده (sent_qty=0)،
      • و مالک «🤖 TGTools ← بازگشت خودکار» را خاموش نکرده باشد (تنظیم tgtools_cancel_refund).
    خروجی True یعنی رسیدگی شد و نباید به مسیر دستی برگردد."""
    if (await _get_setting(row.id, "tgtools_cancel_refund", "1")) == "0":
        return False
    from tenant.bots.virtual_service.vs_discount import vsd_revert_order
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if (not o or o.bot_id != row.id or o.status != "auto_processing"
                or (o.pay_method or "wallet") != "wallet" or int(o.sent_qty or 0) > 0):
            return False
        buyer = await s.get(BotUser, o.buyer_user_id, with_for_update=True)
        if buyer is None:
            return False
        refund = int(o.price_toman or 0) * max(1, int(o.quantity or 1))
        o.status = "refunded"
        o.fail_reason = (reason or "")[:500] or None
        await vsd_revert_order(s, o, buyer.telegram_id)
        buyer.balance = float(buyer.balance or 0) + refund
        s.add(WalletTxn(bot_id=row.id, user_id=buyer.id, amount=refund, kind="charge",
                        note=f"بازگشت وجه {what} (لغو توسط TGTools)"))
        trk, buyer_tg = o.tracking_code, buyer.telegram_id
        await s.commit()
    try:
        await bot.send_message(
            buyer_tg,
            f"❌ متاسفانه سفارشت توسط ارائه‌دهنده لغو شد و مبلغ {refund:,} تومان به کیف‌پولت برگشت.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>\n\n"
            "اگر فکر می‌کنی اشتباهی رخ داده، از دکمهٔ پشتیبانی پیام بده.",
            parse_mode="HTML", reply_markup=await _gift_support_kb(trk))
    except Exception:
        logger.exception("tgtools: خبر لغو به مشتری ناموفق بود")
    await _notify_manual_order(
        bot, row,
        f"↩️ TGTools {what} #{await order_no(GiftOrder, order_id)} را لغو کرد؛ {refund:,} تومان خودکار به کیف‌پول مشتری "
        f"برگشت و به او خبر داده شد.\n🔖 کد: {trk}" + (f"\nعلت: {_html_escape(reason)}" if reason else "") +
        "\n\nاگر ترجیح می‌دهی در این حالت سفارش به «تحویل دستی» برگردد، در «🤖 TGTools» گزینهٔ «بازگشت خودکار وجه» را خاموش کن.")
    return True


async def _tgt_revert_manual(bot: Bot, row, order_id: int, reason: str, *, warn: bool = False) -> None:
    """برگرداندنِ سفارش به تحویلِ دستی + خبر به ادمین (با دکمه‌هایِ انجام شد/رد)."""
    from tenant import _notify_admins
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            return
        if o.status == "auto_processing":
            o.status = "awaiting_admin"
        qty, dest, trk = o.star_count, o.dest_label, o.tracking_code
        await s.commit()
    head = "⚠️" if warn else "🤖"
    await _notify_manual_order(
        bot, row,
        f"{head} ارسالِ خودکارِ استارز — سفارش #{await order_no(GiftOrder, order_id)}\n\n{reason}\n\n"
        f"تعداد: {qty:,} ⭐\n🎯 مقصد: {dest}\n🔖 کد: {trk}\n\n"
        "این سفارش به حالتِ «در انتظارِ ادمین» برگشت؛ دستی بفرست و «✅ انجام شد» را بزن.",
        await _stars_admin_kb(order_id))


async def _tgt_finalize(bot: Bot, row, order_id: int, order: dict) -> str:
    """رویِ خروجیِ get_order اعمال می‌شود. خروجی: 'completed' | 'failed' | 'pending'."""
    from tenant import _notify_admins
    from tenant.bots.virtual_service import tgtools_api as _tg
    st = _tg.norm_status(order)
    if st == "pending":
        return "pending"
    if st == "completed":
        async with Session() as s:
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            if not o or o.bot_id != row.id or o.status != "auto_processing":
                return "completed"          # قبلاً توسطِ مسیرِ دیگری نهایی شده
            o.status = "sent"
            buyer = await s.get(BotUser, o.buyer_user_id)
            buyer_tg_id = buyer.telegram_id if buyer else None
            trk, qty, dest = o.tracking_code, o.star_count, o.dest_label
            await s.commit()
        await _notify_gift_order_trust_success(bot, row, order_id)
        await _notify_manual_order(bot, row, f"✅ سفارشِ استارز #{await order_no(GiftOrder, order_id)} با TGTools تحویل داده شد ({qty:,} ⭐).")
        if buyer_tg_id:
            try:
                await bot.send_message(
                    buyer_tg_id,
                    f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n⭐ تعداد {qty:,} استارز با موفقیت ارسال شد.\n"
                    f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                    reply_markup=await _gift_support_kb(trk))
            except Exception:
                logger.exception("tgtools: خبر به مشتری ناموفق بود")
        return "completed"
    # failed
    reason = str(order.get("message") or order.get("errorMessage") or "").strip()
    if await _tgt_refund_cancelled(bot, row, order_id, reason, "سفارش استارز"):   # PATCH_TGTOOLS_CANCEL_REFUND_V1
        return "failed"
    await _tgt_revert_manual(
        bot, row, order_id,
        "❌ TGTools سفارش را ناموفق اعلام کرد (طبقِ مستند، مبلغ به کیف‌پولِ TGTools برمی‌گردد)."
        + (f"\nعلت: {reason}" if reason else ""))
    return "failed"


async def tgtools_fulfil_order(bot: Bot, row, order_id: int, *, by_admin: bool = False) -> None:
    from tenant.bots.virtual_service import tgtools_api as _tg
    if order_id in _tgt_running:
        return
    _tgt_running.add(order_id)
    try:
        key = await _tgt_key(row.id)
        if not key:
            if by_admin:
                from tenant import _notify_admins
                await _notify_admins(bot, row, "⚠️ کلیدِ API سایتِ TGTools ثبت نشده — از «ارائه‌دهندگانِ من ← 🤖 اتصال به TGTools» ثبتش کن.")
            return
        # ادعایِ اتمیِ سفارش (جلوگیریِ تحویلِ دوبار: دستی + خودکار)
        async with Session() as s:
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            if (not o or o.bot_id != row.id or not str(o.gift_id).startswith("starsell_")
                    or o.status != "awaiting_admin"):
                return
            o.status = "auto_processing"
            qty, dest_label = int(o.star_count), o.dest_label
            await s.commit()

        username = _tg.extract_username(dest_label)
        if not username:
            await _tgt_revert_manual(bot, row, order_id,
                                     "مقصدِ این سفارش یوزرنیم ندارد و TGTools فقط با یوزرنیم می‌فرستد.")
            return
        # === PATCH_TGTOOLS_LOOKUP_V2 ===
        # گزارشِ فدایی: کاربرِ واقعی (@Ejei09) «نامعتبر» شناخته می‌شد و ارسالِ خودکار هیچ‌وقت انجام نمی‌شد.
        # lookup روی Fragment است و گاهی مقطعی false می‌دهد؛ پس (۱) با کلیدِ API صدا زده می‌شود،
        # (۲) یک‌بار بعد از ۳ ثانیه دوباره تلاش می‌شود، (۳) پاسخِ خامِ TGTools در لاگ و در پیامِ ادمین می‌آید
        # تا علتِ واقعی دیده شود. ردِ قطعی فقط وقتی است که هر دو تلاش ردِ صریح باشند.
        _ok, _why = True, ""
        for _attempt in (1, 2):
            try:
                u = await _tg.lookup_user(username, key)
                logger.info("tgtools lookup @%s (try %d) → %s", username, _attempt, str(u)[:400])
                _ok, _why = _tg.lookup_verdict(u)
            except _tg.TgToolsError:
                logger.warning("tgtools: lookup ناموفق، ادامه با خرید", exc_info=True)
                _ok = True
            if _ok:
                break
            if _attempt == 1:
                await asyncio.sleep(3)
        if not _ok:
            await _tgt_revert_manual(
                bot, row, order_id,
                f"TGTools کاربرِ @{username} را معتبر/قابلِ دریافت نمی‌داند (دو بار چک شد).\n"
                f"پاسخِ TGTools: <code>{_html_escape(_why)}</code>\n"
                "اگر کاربر واقعی است و در Fragment هم جستجو می‌شود، دستی بفرست.")
            return

        try:
            res = await _tg.buy_stars(key, username, qty, _tgt_code(row.id, order_id))
        except _tg.TgToolsAmbiguous as e:
            await _tgt_revert_manual(
                bot, row, order_id,
                f"پاسخِ TGTools نامشخص بود ({e}). ممکن است سفارش ثبت شده باشد!\n"
                f"⛔ قبل از هر کاری در tg-tools.shop بخشِ تراکنش‌ها را با کدِ "
                f"<code>{_tgt_code(row.id, order_id)}</code> چک کن؛ اگر آنجا ثبت شده، دستی نفرست.",
                warn=True)
            return
        except _tg.TgToolsError as e:
            await _tgt_revert_manual(bot, row, order_id, f"TGTools خرید را رد کرد: {e.message}")
            return

        tx = res.get("transactionId")
        if not tx:
            await _tgt_revert_manual(
                bot, row, order_id,
                "TGTools شناسهٔ تراکنش برنگرداند؛ ممکن است سفارش ثبت شده باشد. "
                f"در سایت با کدِ <code>{_tgt_code(row.id, order_id)}</code> چک کن.", warn=True)
            return
        await _set_setting(row.id, f"tgtools_tx_{order_id}", str(tx))
        if _tg.norm_status(res) != "pending":
            await _tgt_finalize(bot, row, order_id, res)
            return
        for d in _TGT_POLL_DELAYS:
            await asyncio.sleep(d)
            try:
                cur = await _tg.get_order(key, tx)
            except _tg.TgToolsError:
                continue
            if await _tgt_finalize(bot, row, order_id, cur) != "pending":
                return
        from tenant import _notify_admins
        await _notify_manual_order(
            bot, row,
            f"⏳ سفارشِ استارز #{await order_no(GiftOrder, order_id)} هنوز در صفِ TGTools است (تراکنش {tx}). "
            "وقتی تحویل شد خودت با دکمهٔ زیر وضعیت را بررسی کن — تا آن موقع دستی نفرست.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔄 بررسیِ وضعیت", callback_data=f"star:tgtchk:{order_id}")]]))
    except Exception:
        logger.exception("tgtools: خطایِ پیش‌بینی‌نشده در سفارش %s", order_id)
        try:
            await _tgt_revert_manual(
                bot, row, order_id,
                "خطایِ داخلی رخ داد. اگر مطمئن نیستی خرید انجام شده یا نه، اول در tg-tools.shop چک کن.",
                warn=True)
        except Exception:
            logger.exception("tgtools: برگرداندن به دستی هم ناموفق بود")
    finally:
        _tgt_running.discard(order_id)


# ═══════════════════════════════════════════
#  ارسالِ خودکارِ استارز با ولتِ فرگمنتِ خودِ ربات — PATCH_FRAGMENT_AUTO_STARS_V1
# ═══════════════════════════════════════════
# گزارشِ ادمین (فدایی): «ولتی که ربات برای فرگمنت داده، فروش‌مون خودکار باشه و برای کاربر بزنه».
# مثلِ TGTools (همان وضعیتِ auto_processing، همان برگشت به تحویلِ دستی)، ولی خریدِ استارز با
# TONِ ولتِ اختصاصیِ همین ربات از طریقِ pyfragment انجام می‌شود.
#
# اصولِ ایمنی (پولِ واقعی خرج می‌شود):
#  • ادعایِ اتمیِ سفارش (awaiting_admin → auto_processing) ⇒ تحویلِ دوبارِ «دستی + خودکار» ممکن نیست.
#  • برایِ هر ربات هم‌زمان فقط یک خرید (قفلِ per-bot) — دو تراکنشِ هم‌زمان از یک ولت seqno را خراب می‌کند.
#  • خطاهایی که قطعاً «قبل از پرداخت» رخ می‌دهند (یوزر پیدا نشد، کوکی خراب، ...) ⇒ برگشت به دستی، بدونِ هشدار.
#  • هر خطایِ نامشخص (تایم‌اوت، خطای تراکنش، پارس...) ⇒ برگشت به دستی *با هشدار*: «اول تراکنش‌هایِ ولت را
#    ببین»؛ هیچ retryِ خودکاریِ خرید وجود ندارد.
#  • سقفِ هر سفارشِ خودکار _FG_AUTO_MAX_STARS؛ بالاتر از آن دستی می‌ماند. کمتر از ۵۰ هم (حدِ فرگمنت).
_FG_AUTO_MAX_STARS = 5000
_FG_MIN_STARS = 50
_fg_running: set[int] = set()
_fg_tasks: set = set()
_fg_locks: dict[int, asyncio.Lock] = {}


async def _fg_wallet(bot_id: int):
    from models import FragmentWallet
    async with Session() as s:
        w = await s.scalar(select(FragmentWallet).where(FragmentWallet.bot_id == bot_id))
    if w and w.enabled and w.encrypted_fragment_cookies and w.encrypted_tonapi_key:
        return w
    return None


async def _fg_auto_on(bot_id: int) -> bool:
    """فروشِ خودکار روشن است و ولتِ فرگمنت کاملاً وصل شده؟"""
    if (await _get_setting(bot_id, "fragment_auto", "0")) != "1":
        return False
    return (await _fg_wallet(bot_id)) is not None


def _fg_spawn(bot: Bot, row, order_id: int, by_admin: bool = False) -> None:
    t = asyncio.create_task(fragment_fulfil_order(bot, row, order_id, by_admin=by_admin))
    _fg_tasks.add(t)
    t.add_done_callback(_fg_tasks.discard)


def _fg_clean_failure(e: Exception) -> str | None:
    """خطاهایی که مطمئنیم *قبل* از ارسالِ تراکنش بوده‌اند ⇒ متنِ فارسی؛ وگرنه None (نامشخص)."""
    try:
        import pyfragment as _pf
    except ImportError:
        return "کتابخانهٔ pyfragment روی سرور نصب نیست (pip install pyfragment)."
    table = (
        (_pf.UserNotFoundError, "فرگمنت این کاربر را پیدا نکرد (یوزرنیم اشتباه یا غیرقابلِ دریافت)."),
        (_pf.CookieError, "کوکی‌هایِ فرگمنت خراب/منقضی است؛ از «⭐ استارزِ خودکار ← ویرایشِ اتصال» دوباره وصل کن."),
        (_pf.ConfigurationError, "تنظیماتِ اتصالِ فرگمنت ناقص است؛ دوباره وصلش کن."),
        (_pf.VerificationError, "فرگمنت برایِ این حساب احرازِ هویت (KYC) می‌خواهد."),
        (_pf.FragmentPageError, "صفحهٔ فرگمنت در دسترس نبود؛ کمی بعد دوباره امتحان کن."),
        (_pf.WalletError, "ولتِ فرگمنت مشکل دارد (احتمالاً موجودیِ TON کافی نیست)؛ شارژش کن."),
    )
    for cls, msg in table:
        if isinstance(e, cls):
            return msg
    return None


async def fragment_fulfil_order(bot: Bot, row, order_id: int, *, by_admin: bool = False) -> None:
    from tenant.bots.virtual_service import tgtools_api as _tg   # فقط extract_username
    if order_id in _fg_running:
        return
    _fg_running.add(order_id)
    try:
        lock = _fg_locks.setdefault(row.id, asyncio.Lock())
        async with lock:
            # ادعایِ اتمیِ سفارش (جلوگیری از تحویلِ دوبار: دستی + TGTools + فرگمنت)
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if (not o or o.bot_id != row.id or not str(o.gift_id).startswith("starsell_")
                        or o.status != "awaiting_admin"):
                    return
                o.status = "auto_processing"
                qty, dest_label = int(o.star_count), o.dest_label
                await s.commit()

            wallet = await _fg_wallet(row.id)
            if wallet is None:
                await _tgt_revert_manual(bot, row, order_id,
                                         "ولتِ فرگمنت وصل/فعال نیست (از «⭐ استارزِ خودکار» وصلش کن).")
                return
            username = _tg.extract_username(dest_label)
            if not username:
                await _tgt_revert_manual(bot, row, order_id,
                                         "مقصدِ این سفارش یوزرنیم ندارد و فرگمنت فقط با یوزرنیم می‌فرستد.")
                return
            if qty < _FG_MIN_STARS or qty > _FG_AUTO_MAX_STARS:
                await _tgt_revert_manual(
                    bot, row, order_id,
                    f"تعدادِ {qty:,} استارز خارج از بازهٔ ارسالِ خودکار ({_FG_MIN_STARS}–{_FG_AUTO_MAX_STARS:,}) است؛ دستی بفرست.")
                return

            from tenant.bots.virtual_service.fragment_stars import _get_fragment_client
            try:
                client = await _get_fragment_client(wallet)
                async with client:
                    res = await asyncio.wait_for(
                        client.purchase_stars(username, amount=qty, show_sender=False), timeout=150)
            except Exception as e:   # noqa: BLE001
                clean = _fg_clean_failure(e)
                if clean:
                    logger.warning("fragment-auto: خرید رد شد (قبل از پرداخت): %s", type(e).__name__)
                    await _tgt_revert_manual(bot, row, order_id, f"خریدِ فرگمنت انجام نشد: {clean}")
                else:
                    logger.exception("fragment-auto: خطایِ نامشخص در سفارش %s", order_id)
                    await _tgt_revert_manual(
                        bot, row, order_id,
                        f"پاسخِ فرگمنت نامشخص بود ({type(e).__name__}). ممکن است TON از ولت خرج و استارز ارسال شده باشد!\n"
                        "⛔ قبل از هر کاری تراکنش‌هایِ ولتِ فرگمنت (و پروفایلِ گیرنده) را چک کن؛ "
                        "اگر خرج شده، دستی نفرست.", warn=True)
                return

            tx = getattr(res, "transaction_id", None)
            if not tx or not getattr(res, "confirmed", True):
                await _tgt_revert_manual(
                    bot, row, order_id,
                    f"تراکنشِ فرگمنت ارسال شد ولی تأیید نشد (شناسه: <code>{tx or '—'}</code>). "
                    "قبل از ارسالِ دستی، وضعیتِ تراکنش را در ولت/فرگمنت چک کن.", warn=True)
                return
            await _set_setting(row.id, f"fragment_tx_{order_id}", str(tx))
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if not o or o.bot_id != row.id:
                    return
                o.status = "sent"
                buyer = await s.get(BotUser, o.buyer_user_id)
                buyer_tg_id = buyer.telegram_id if buyer else None
                trk = o.tracking_code
                await s.commit()
            await _notify_gift_order_trust_success(bot, row, order_id)
            await _notify_manual_order(
                bot, row,
                f"✅ سفارشِ استارز #{await order_no(GiftOrder, order_id)} با ولتِ فرگمنت تحویل داده شد "
                f"({qty:,} ⭐ → @{username}).\nتراکنش: <code>{tx}</code>")
            if buyer_tg_id:
                try:
                    await bot.send_message(
                        buyer_tg_id,
                        f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n⭐ تعداد {qty:,} استارز با موفقیت ارسال شد.\n"
                        f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                        reply_markup=await _gift_support_kb(trk))
                except Exception:
                    logger.exception("fragment-auto: خبر به مشتری ناموفق بود")
    except Exception:
        logger.exception("fragment-auto: خطایِ پیش‌بینی‌نشده در سفارش %s", order_id)
        try:
            await _tgt_revert_manual(
                bot, row, order_id,
                "خطایِ داخلی رخ داد. اگر مطمئن نیستی خرید انجام شده یا نه، اول تراکنش‌هایِ ولتِ فرگمنت را چک کن.",
                warn=True)
        except Exception:
            logger.exception("fragment-auto: برگرداندن به دستی هم ناموفق بود")
    finally:
        _fg_running.discard(order_id)


@smm_router.callback_query(F.data.startswith("star:fgt:"))
async def star_fragment_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    if await _fg_wallet(row.id) is None:
        await cb.answer("اول ولتِ فرگمنت را کامل وصل کن (⭐ استارزِ خودکار).", show_alert=True)
        return
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not o or o.bot_id != row.id or o.status != "awaiting_admin":
        await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        return
    await cb.answer("🤖 در حالِ ارسال با ولتِ فرگمنت… نتیجه اینجا اعلام می‌شود.", show_alert=True)
    _fg_spawn(bot, row, order_id, by_admin=True)
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ سفارشِ استارز پس از ارسالِ خودکارِ فرگمنت ممکن نشد")


@smm_router.callback_query(F.data.startswith("star:tgt:"))
async def star_tgt_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ TGTools را ثبت کن (فروشِ استارز ← 🤖 TGTools).", show_alert=True)
        return
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not o or o.bot_id != row.id or o.status != "awaiting_admin":
        await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        return
    await cb.answer("🤖 در حالِ ارسال با TGTools… نتیجه اینجا اعلام می‌شود.", show_alert=True)
    _tgt_spawn(bot, row, order_id, by_admin=True)
    # === PATCH_ADMIN_MSG_CLOSE_V1 === پیامِ سفارش پاک می‌شود؛ نتیجه با پیامِ تازه می‌آید
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ سفارشِ استارز پس از ارسالِ خودکار ممکن نشد")


@smm_router.callback_query(F.data.startswith("star:tgtchk:"))
async def star_tgt_check(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import tgtools_api as _tg
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    key = await _tgt_key(row.id)
    tx = await _get_setting(row.id, f"tgtools_tx_{order_id}", "")
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not key or not tx or not o or o.bot_id != row.id:
        await cb.answer("اطلاعاتِ این سفارش پیدا نشد.", show_alert=True)
        return
    if o.status != "auto_processing":
        await cb.answer("این سفارش قبلاً نهایی شده.", show_alert=True)
        return
    try:
        cur = await _tg.get_order(key, tx)
    except _tg.TgToolsError as e:
        await cb.answer(f"خطا در استعلام: {e.message}"[:190], show_alert=True)
        return
    res = await _tgt_finalize(bot, row, order_id, cur)
    await cb.answer({"completed": "✅ تحویل داده شده", "failed": "❌ TGTools لغو/ناموفق اعلام کرد — وضعیت سفارش و پیام ادمین را ببین",
                     "pending": "⏳ هنوز در صف است"}[res], show_alert=True)
    if res != "pending":   # PATCH_ADMIN_MSG_CLOSE_V1
        try:
            await cb.message.delete()
        except Exception:
            logger.info("حذفِ پیامِ بررسیِ وضعیتِ TGTools ممکن نشد")


# ═══════════════════════════════════════════
#  گیفتِ تلگرامِ خودکار با TGTools — PATCH_TGTOOLS_GIFT_V1
# ═══════════════════════════════════════════
# سفارشِ گیفتِ کاتالوگی که به تحویلِ دستی می‌رود (حالتِ «اکانتِ پشتیبانی»/تحویلِ دستیِ همگانی/تاییدِ رسیدِ کارت)
# با کلیدِ TGTools خریده و به یوزرنیمِ مقصد هدیه می‌شود (POST /api/Purchase/gift؛ پولِ TON از کیف‌پولِ TGTools).
# • هر واحد یک تماسِ جدا با trackingCodeِ یکتا (b{bot}-o{order}-g{i}) ⇒ تکرارِ سفارش در TGTools رد می‌شود.
# • کامنت/ناشناس (hideSenderName) پشتیبانی می‌شود؛ گیفتِ «سفارشی/NFT» (manual…) هرگز به TGTools نمی‌رود.
# • هر خطا/نامشخص ⇒ برمی‌گردد به «در انتظارِ ادمین» (با تعدادِ ارسال‌شده) و ادمین خبردار می‌شود؛ هیچ retryِ خودکارِ «خرید».

async def _tgt_gift_on(bot_id: int) -> bool:
    return ((await _get_setting(bot_id, "tgtools_gift", "0")) == "1"
            and bool(await _get_setting(bot_id, "tgtools_api_key", "")))


async def _gift_admin_kb(bot_id: int, order_id: int, gift_id: str | None) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"gift:cardok:{order_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"gift:cardno:{order_id}", style=ButtonStyle.DANGER),
    ]]
    try:
        if not str(gift_id or "").startswith("manual") and await _get_setting(bot_id, "tgtools_api_key", ""):
            rows.append([InlineKeyboardButton(text="🤖 خرید با TGTools", callback_data=f"gift:tgt:{order_id}",
                                              style=ButtonStyle.PRIMARY)])
    except Exception:
        logger.exception("tgtools: ساختِ دکمهٔ گیفت ناموفق")
    # === PATCH_ACC_GIFT_V1 === ارسال با اکانتِ پشتیبانی (فقط اگر اکانت وصل است و گیفت کاتالوگی است)
    try:
        if not str(gift_id or "").startswith(("manual", "premium_", "starsell_", "srx_", "tonsell_")):
            from tenant.bots.virtual_service import acc_gift as _acc
            if await _acc.has_account(bot_id):
                rows.append([InlineKeyboardButton(text="👤 ارسال با اکانتِ پشتیبانی", callback_data=f"gift:acc:{order_id}",
                                                  style=ButtonStyle.PRIMARY)])
    except Exception:
        logger.exception("acc_gift: ساختِ دکمهٔ گیفت ناموفق")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tgt_gift_code(bot_id: int, order_id: int, idx: int) -> str:
    return f"{_tgt_code(bot_id, order_id)}-g{idx}"


def _tgt_spawn_gift(bot: Bot, row, order_id: int, by_admin: bool = False, resume: bool = False) -> None:
    t = asyncio.create_task(tgtools_fulfil_gift(bot, row, order_id, by_admin=by_admin, resume=resume))
    _tgt_tasks.add(t)
    t.add_done_callback(_tgt_tasks.discard)


async def _tgt_revert_manual_gift(bot: Bot, row, order_id: int, reason: str, *, warn: bool = False, who: str = "TGTools") -> None:
    from tenant import _notify_admins
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            return
        if o.status == "auto_processing":
            o.status = "awaiting_admin"
        gift_id, dest, trk = o.gift_id, o.dest_label, o.tracking_code
        qty, done = int(o.quantity or 1), int(o.sent_qty or 0)
        await s.commit()
    part = f"\n📦 تا الان با {who} {done} از {qty} عدد فرستاده شده؛ فقط «باقی‌مانده» را دستی بفرست." if done else ""
    await _notify_manual_order(
        bot, row,
        f"{'⚠️' if warn else '🤖'} {'ارسالِ خودکارِ گیفت با ' + who if who != 'TGTools' else 'خریدِ خودکارِ گیفت'} — سفارش #{await order_no(GiftOrder, order_id)}\n\n{reason}{part}\n\n"
        f"🎯 مقصد: {dest}\n🔖 کد: {trk}\n\n"
        "این سفارش به «در انتظارِ ادمین» برگشت؛ دستی بفرست و «✅ انجام شد» را بزن.",
        await _gift_admin_kb(row.id, order_id, gift_id))


async def _tgt_gift_status(key: str, tx, first: dict | None = None) -> tuple[str, dict]:
    """وضعیتِ یک تراکنش را تا نتیجه یا پایانِ پنجرهٔ ≈۶دقیقه پول می‌کند → ('completed'|'failed'|'pending', آخرین پاسخ)."""
    from tenant.bots.virtual_service import tgtools_api as _tg
    cur = first or {}
    if first and _tg.norm_status(first) != "pending":
        return _tg.norm_status(first), first
    for d in _TGT_POLL_DELAYS:
        await asyncio.sleep(d)
        try:
            cur = await _tg.get_order(key, tx)
        except _tg.TgToolsError:
            continue
        st = _tg.norm_status(cur)
        if st != "pending":
            return st, cur
    return "pending", cur


async def _tgt_gift_finish(bot: Bot, row, order_id: int, who: str = "TGTools") -> None:
    from tenant import _notify_admins
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            return
        o.status = "sent"
        o.sent_qty = int(o.quantity or 1)
        buyer = await s.get(BotUser, o.buyer_user_id)
        buyer_tg_id = buyer.telegram_id if buyer else None
        trk = o.tracking_code
        await s.commit()
    await _notify_gift_order_trust_success(bot, row, order_id)
    await _notify_manual_order(bot, row, f"✅ گیفتِ سفارش #{await order_no(GiftOrder, order_id)} با {who} تحویل داده شد.")
    if buyer_tg_id:
        try:
            await bot.send_message(
                buyer_tg_id,
                f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n🎁 گیفت تلگرام با موفقیت ارسال شد.\n"
                f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML",
                reply_markup=await _gift_support_kb(trk))
        except Exception:
            logger.exception("tgtools: خبر به مشتریِ گیفت ناموفق بود")


async def tgtools_fulfil_gift(bot: Bot, row, order_id: int, *, by_admin: bool = False, resume: bool = False) -> None:
    from tenant import _notify_admins
    from tenant.bots.virtual_service import tgtools_api as _tg
    if order_id in _tgt_running:
        return
    _tgt_running.add(order_id)
    try:
        key = await _tgt_key(row.id)
        if not key:
            if by_admin:
                await _notify_admins(bot, row, "⚠️ کلیدِ API سایتِ TGTools ثبت نشده — از «ارائه‌دهندگانِ من ← 🤖 اتصال به TGTools» ثبتش کن.")
            return
        async with Session() as s:     # ادعایِ اتمیِ سفارش (جلوگیری از تحویلِ دوبار: دستی + خودکار)
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            need = "auto_processing" if resume else "awaiting_admin"
            if (not o or o.bot_id != row.id or str(o.gift_id).startswith("manual")
                    or str(o.gift_id).startswith(("premium_", "starsell_", "srx_", "tonsell_")) or o.status != need):
                return
            o.status = "auto_processing"
            gift_id, qty, done = o.gift_id, int(o.quantity or 1), int(o.sent_qty or 0)
            dest_label = o.dest_label
            comment, scope, anon = (o.comment or ""), int(o.comment_scope or 0), bool(o.anonymous)
            await s.commit()
        username = _tg.extract_username(dest_label)
        if not username:
            await _tgt_revert_manual_gift(bot, row, order_id,
                                          "مقصدِ این سفارش یوزرنیم ندارد و TGTools فقط با یوزرنیم هدیه می‌دهد.")
            return
        if not resume:      # گیفت در کاتالوگِ TGTools هست و تمام نشده؟
            try:
                info = _tg.find_gift(await _tg.get_gifts(key), gift_id)
            except _tg.TgToolsError as e:
                await _tgt_revert_manual_gift(bot, row, order_id, f"فهرستِ گیفت‌هایِ TGTools گرفته نشد: {e.message}")
                return
            if not info:
                await _tgt_revert_manual_gift(bot, row, order_id, "این گیفت در کاتالوگِ TGTools وجود ندارد.")
                return
            if info.get("soldOut"):
                await _tgt_revert_manual_gift(bot, row, order_id, "این گیفت در TGTools تمام شده (sold out).")
                return
        for i in range(done, qty):
            code = _tgt_gift_code(row.id, order_id, i)
            msg = _gift_comment_for(comment[:128], scope, i)
            try:
                res = await _tg.buy_gift(key, username, gift_id, code, message=msg, hide_sender=anon)
            except _tg.TgToolsAmbiguous as e:
                await _tgt_revert_manual_gift(
                    bot, row, order_id,
                    f"پاسخِ TGTools نامشخص بود ({e}). ممکن است عددِ {i + 1}ام ثبت شده باشد!\n"
                    f"⛔ قبل از هر کاری در tg-tools.shop تراکنش‌ها را با کدِ <code>{code}</code> چک کن؛ "
                    "اگر آنجا ثبت شده، دستی نفرست.", warn=True)
                return
            except _tg.TgToolsError as e:
                await _tgt_revert_manual_gift(bot, row, order_id, f"TGTools خرید را رد کرد: {e.message}")
                return
            tx = res.get("transactionId")
            if not tx:
                await _tgt_revert_manual_gift(
                    bot, row, order_id,
                    f"TGTools شناسهٔ تراکنش برنگرداند؛ ممکن است عددِ {i + 1}ام ثبت شده باشد. "
                    f"در سایت با کدِ <code>{code}</code> چک کن.", warn=True)
                return
            await _set_setting(row.id, f"tgtools_gtx_{order_id}", f"{i}:{tx}")
            st, cur = await _tgt_gift_status(key, tx, res)
            if st == "pending":
                await _notify_manual_order(
                    bot, row,
                    f"⏳ گیفتِ سفارش #{await order_no(GiftOrder, order_id)} (عددِ {i + 1} از {qty}) هنوز در صفِ TGTools است "
                    f"(تراکنش {tx}). وقتی تحویل شد با دکمهٔ زیر وضعیت را بررسی کن — تا آن موقع دستی نفرست.",
                    InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                        text="🔄 بررسیِ وضعیت", callback_data=f"gift:tgtchk:{order_id}")]]))
                return
            if st == "failed":
                reason = str(cur.get("message") or cur.get("errorMessage") or "").strip()
                if await _tgt_refund_cancelled(bot, row, order_id, reason, "سفارش گیفت"):   # PATCH_TGTOOLS_CANCEL_REFUND_V1
                    return
                await _tgt_revert_manual_gift(
                    bot, row, order_id,
                    "❌ TGTools سفارش را ناموفق اعلام کرد (طبقِ مستند، مبلغ به کیف‌پولِ TGTools برمی‌گردد)."
                    + (f"\nعلت: {reason}" if reason else ""))
                return
            async with Session() as s:      # عددِ i ام تحویل شد
                o2 = await s.get(GiftOrder, order_id, with_for_update=True)
                if o2:
                    o2.sent_qty = i + 1
                    await s.commit()
        await _tgt_gift_finish(bot, row, order_id)
    except Exception:
        logger.exception("tgtools: خطایِ پیش‌بینی‌نشده در گیفتِ سفارش %s", order_id)
        try:
            await _tgt_revert_manual_gift(
                bot, row, order_id,
                "خطایِ داخلی رخ داد. اگر مطمئن نیستی خرید انجام شده یا نه، اول در tg-tools.shop چک کن.", warn=True)
        except Exception:
            logger.exception("tgtools: برگرداندنِ گیفت به دستی هم ناموفق بود")
    finally:
        _tgt_running.discard(order_id)


# ═══════════════════════════════════════════
#  گیفتِ خودکار با اکانتِ پشتیبانی — PATCH_ACC_GIFT_V1
# ═══════════════════════════════════════════
async def _acc_gift_on(bot_id: int) -> bool:
    from tenant.bots.virtual_service import acc_gift as _acc
    return (await _get_setting(bot_id, "acc_gift", "0")) == "1" and await _acc.has_account(bot_id)


async def _auto_gift_on(bot_id: int) -> bool:
    return await _acc_gift_on(bot_id) or await _tgt_gift_on(bot_id)


def _gift_auto_spawn(bot: Bot, row, order_id: int, *, acc_first: bool | None = None, by_admin: bool = False) -> None:
    """اکانتِ پشتیبانی اولویت دارد؛ اگر خاموش بود TGTools. (acc_first=None ⇒ بر اساسِ تنظیمات؛ ارسالِ زمان‌بندی‌شده.)"""
    async def _go():
        use_acc = acc_first if acc_first is not None else await _acc_gift_on(row.id)
        if use_acc:
            await acc_fulfil_gift(bot, row, order_id, by_admin=by_admin)
        else:
            await tgtools_fulfil_gift(bot, row, order_id, by_admin=by_admin)
    t = asyncio.create_task(_go())
    _tgt_tasks.add(t)
    t.add_done_callback(_tgt_tasks.discard)


async def acc_fulfil_gift(bot: Bot, row, order_id: int, *, by_admin: bool = False) -> None:
    from tenant.bots.virtual_service import acc_gift as _acc
    from tenant.bots.virtual_service import tgtools_api as _tg
    if order_id in _tgt_running:
        return
    _tgt_running.add(order_id)
    fallback_tgt = False
    try:
        async with Session() as s:     # ادعایِ اتمیکِ سفارش (جلوگیری از تحویلِ دوبار: دستی + خودکار)
            o = await s.get(GiftOrder, order_id, with_for_update=True)
            if (not o or o.bot_id != row.id or str(o.gift_id).startswith("manual")
                    or str(o.gift_id).startswith(("premium_", "starsell_", "srx_", "tonsell_"))
                    or o.status != "awaiting_admin"):
                return
            o.status = "auto_processing"
            gift_id, qty, done = o.gift_id, int(o.quantity or 1), int(o.sent_qty or 0)
            dest_label, dest_tg = o.dest_label, int(o.dest_telegram_id or 0)
            comment, scope, anon = (o.comment or ""), int(o.comment_scope or 0), bool(o.anonymous)
            await s.commit()
        username = _tg.extract_username(dest_label)
        for i in range(done, qty):
            msg = _gift_comment_for(comment[:128], scope, i)
            try:
                await _acc.send_gift(row.id, username=username, tg_id=(dest_tg if dest_tg > 0 else None), gift_id=gift_id,
                                     text=msg or None, hide_name=anon)
            except _acc.AccGiftError as e:
                if not e.definite:
                    await _tgt_revert_manual_gift(
                        bot, row, order_id,
                        f"{e.message}\n⛔ قبل از هر کاری در خودِ اکانتِ پشتیبانی (گیفت‌هایِ ارسال‌شده) چک کن؛ "
                        f"اگر عددِ {i + 1}ام آنجا ارسال شده، دستی نفرست.", warn=True, who="اکانتِ پشتیبانی")
                    return
                # خطایِ قطعی: ارسال نشده. اگر TGTools هم روشن است، همان‌جا ادامه بده؛ وگرنه دستی.
                if await _tgt_gift_on(row.id):
                    fallback_tgt = True
                    await _notify_manual_order(
                        bot, row, f"⚠️ اکانتِ پشتیبانی گیفتِ سفارش #{await order_no(GiftOrder, order_id)} را نفرستاد "
                                  f"({e.message}) — ادامه با TGTools.")
                else:
                    await _tgt_revert_manual_gift(bot, row, order_id, e.message, who="اکانتِ پشتیبانی")
                break
            async with Session() as s:      # عددِ i ام تحویل شد (بلافاصله ثبت؛ تا ارسالِ تکراری نشود)
                o2 = await s.get(GiftOrder, order_id, with_for_update=True)
                if o2:
                    o2.sent_qty = i + 1
                    await s.commit()
        else:
            await _tgt_gift_finish(bot, row, order_id, who="اکانتِ پشتیبانی")
            return
        if fallback_tgt:
            async with Session() as s:
                o3 = await s.get(GiftOrder, order_id, with_for_update=True)
                if o3 and o3.status == "auto_processing":
                    o3.status = "awaiting_admin"
                    await s.commit()
    except Exception:
        logger.exception("acc_gift: خطایِ پیش‌بینی‌نشده در گیفتِ سفارش %s", order_id)
        try:
            await _tgt_revert_manual_gift(
                bot, row, order_id,
                "خطایِ داخلی رخ داد. اگر مطمئن نیستی گیفت ارسال شده یا نه، اول در اکانتِ پشتیبانی چک کن.",
                warn=True, who="اکانتِ پشتیبانی")
        except Exception:
            logger.exception("acc_gift: برگرداندنِ گیفت به دستی هم ناموفق بود")
    finally:
        _tgt_running.discard(order_id)
    if fallback_tgt:
        _tgt_spawn_gift(bot, row, order_id)


@smm_router.callback_query(F.data.startswith("gift:acc:"))
async def gift_acc_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant.bots.virtual_service import acc_gift as _acc
    order_id = int(cb.data.split(":")[2])
    if not await _acc.has_account(row.id):
        await cb.answer("اکانتِ پشتیبانی وصل نیست (🤝 واسطه‌گری ← اتصالِ اکانت).", show_alert=True)
        return
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if (not o or o.bot_id != row.id or o.status != "awaiting_admin"
            or str(o.gift_id).startswith(("manual", "premium_", "starsell_", "srx_", "tonsell_"))):
        await cb.answer("این سفارش قبلاً رسیدگی شده یا برایِ ارسال با اکانت مناسب نیست.", show_alert=True)
        return
    await cb.answer("👤 در حالِ ارسال با اکانتِ پشتیبانی… نتیجه اینجا اعلام می‌شود.", show_alert=True)
    _gift_auto_spawn(bot, row, order_id, acc_first=True, by_admin=True)
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ سفارشِ گیفت پس از ارسالِ خودکار ممکن نشد")


async def _acc_menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.virtual_service import acc_gift as _acc
    on = (await _get_setting(bot_id, "acc_gift", "0")) == "1"
    has = await _acc.has_account(bot_id)
    lines = ["👤 <b>گیفتِ خودکار با اکانتِ پشتیبانی</b>\n"]
    rows: list = []
    if not has:
        lines.append("❌ اکانتِ پشتیبانی وصل نیست.\nاز «🤝 واسطه‌گری ← 👤 اتصالِ اکانتِ واسط» همان اکانت را وصل کن "
                     "(همان اکانت گیفت‌ها را هم می‌فرستد).")
    else:
        st = await _acc.status(bot_id)
        if st.get("ok"):
            uname = f" @{st['username']}" if st.get("username") else ""
            lines.append(f"✅ اکانت: <b>{_html_escape(st['name'])}</b>{uname}")
            lines.append(f"⭐ موجودیِ استارز: <b>{st['stars']:,}</b>" if st.get("stars") is not None
                         else "⭐ موجودیِ استارز: نامشخص")
        else:
            lines.append(f"⚠️ اتصال: {_html_escape(st.get('error') or '')}")
        lines.append(f"\n🚀 ارسالِ خودکار: <b>{'روشن ✅' if on else 'خاموش'}</b>")
        rows.append([InlineKeyboardButton(text=("🚀 ارسالِ خودکار: روشن ✅" if on else "🚀 ارسالِ خودکار: خاموش"),
                                          callback_data="ssl:acc:toggle",
                                          style=(ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانیِ موجودی", callback_data="ssl:acc")])
    lines.append("\n• گیفت‌هایِ کاتالوگیِ «تحویلِ دستی / اکانتِ پشتیبانی» (و بعد از تاییدِ رسیدِ کارت) درجا با همین اکانت "
                 "فرستاده می‌شوند؛ نامِ فرستنده (ناشناس) و کامنتِ مشتری هم رعایت می‌شود.\n"
                 "• مقصد با یوزرنیم پیدا می‌شود؛ مشتریِ بدونِ یوزرنیم فقط وقتی که اکانت قبلاً با او در تماس بوده.\n"
                 "• موجودیِ استارزِ اکانت کم باشد یا هر خطایی شود، سفارش به «تحویلِ دستی» برمی‌گردد و خبرت می‌کنم "
                 "(اگر TGTools هم روشن باشد، ادامه با آن).\n"
                 "• گیفتِ سفارشی/NFT، پرمیوم و استارز با این مسیر ارسال نمی‌شوند.")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@smm_router.callback_query(F.data == "ssl:acc")
async def ssl_acc_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    text, kb = await _acc_menu_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            await bot.send_message(cb.from_user.id, text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data == "ssl:acc:toggle")
async def ssl_acc_toggle(cb: CallbackQuery, bot: Bot):
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    from tenant.bots.virtual_service import acc_gift as _acc
    if not await _acc.has_account(row.id):
        await cb.answer("اول اکانتِ پشتیبانی را وصل کن.", show_alert=True)
        return
    cur = (await _get_setting(row.id, "acc_gift", "0")) == "1"
    await _set_setting(row.id, "acc_gift", "0" if cur else "1")
    await cb.answer("خاموش شد؛ گیفت‌ها مثلِ قبل دستی هستند (دکمهٔ «👤 ارسال با اکانتِ پشتیبانی» همچنان هست)." if cur
                    else "روشن شد — گیفتِ کاتالوگیِ «تحویلِ دستی» درجا با اکانتِ پشتیبانی فرستاده می‌شود.",
                    show_alert=True)
    text, kb = await _acc_menu_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            await bot.send_message(cb.from_user.id, text, reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("gift:tgt:"))
async def gift_tgt_go(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ TGTools را ثبت کن (🤖 TGTools).", show_alert=True)
        return
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if (not o or o.bot_id != row.id or o.status != "awaiting_admin"
            or str(o.gift_id).startswith(("manual", "premium_", "starsell_", "srx_", "tonsell_"))):
        await cb.answer("این سفارش قبلاً رسیدگی شده یا برایِ TGTools مناسب نیست.", show_alert=True)
        return
    await cb.answer("🤖 در حالِ خرید با TGTools… نتیجه اینجا اعلام می‌شود.", show_alert=True)
    _tgt_spawn_gift(bot, row, order_id, by_admin=True)
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ سفارشِ گیفت پس از خریدِ خودکار ممکن نشد")


@smm_router.callback_query(F.data.startswith("gift:tgtchk:"))
async def gift_tgt_check(cb: CallbackQuery, bot: Bot):
    from tenant.bots.virtual_service import tgtools_api as _tg
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    key = await _tgt_key(row.id)
    raw = await _get_setting(row.id, f"tgtools_gtx_{order_id}", "")
    async with Session() as s:
        o = await s.get(GiftOrder, order_id)
    if not key or ":" not in raw or not o or o.bot_id != row.id:
        await cb.answer("اطلاعاتِ این سفارش پیدا نشد.", show_alert=True)
        return
    if o.status != "auto_processing":
        await cb.answer("این سفارش قبلاً نهایی شده.", show_alert=True)
        return
    idx_s, tx = raw.split(":", 1)
    try:
        cur = await _tg.get_order(key, tx)
    except _tg.TgToolsError as e:
        await cb.answer(f"خطا در استعلام: {e.message}"[:190], show_alert=True)
        return
    st = _tg.norm_status(cur)
    if st == "pending":
        await cb.answer("⏳ هنوز در صف است", show_alert=True)
        return
    if st == "failed":
        await _tgt_revert_manual_gift(bot, row, order_id, "❌ TGTools سفارش را ناموفق اعلام کرد (مبلغ به کیف‌پولِ TGTools برمی‌گردد).")
        await cb.answer("❌ ناموفق — به حالتِ دستی برگشت", show_alert=True)
    else:
        qty = int(o.quantity or 1)
        async with Session() as s:
            o2 = await s.get(GiftOrder, order_id, with_for_update=True)
            if o2 is not None and await _is_foreign(o2, bot):   # PATCH_TENANT_SCOPE_V1
                o2 = None
            o2.sent_qty = int(idx_s) + 1
            await s.commit()
        if int(idx_s) + 1 >= qty:
            await _tgt_gift_finish(bot, row, order_id)
            await cb.answer("✅ تحویل داده شده", show_alert=True)
        else:
            await cb.answer("✅ این عدد تحویل شد؛ ادامهٔ بقیه…", show_alert=True)
            _tgt_spawn_gift(bot, row, order_id, by_admin=True, resume=True)
    try:
        await cb.message.delete()
    except Exception:
        logger.info("حذفِ پیامِ بررسیِ وضعیتِ گیفت ممکن نشد")


# ─────────── پنلِ ادمین: «🤖 TGTools» ───────────

async def _tgt_menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.virtual_service import tgtools_api as _tg
    key = await _tgt_key(bot_id)
    auto_on = (await _get_setting(bot_id, "tgtools_auto", "0")) == "1"
    lines = ["🤖 <b>ارسالِ خودکارِ استارز با TGTools</b>\n"]
    if key:
        lines.append(f"🔑 کلید: <code>{_tgt_mask(key)}</code> ✅")
        try:
            w = await _tg.get_wallet(key)
            lines.append(f"💰 موجودیِ کیف‌پولِ TGTools: <b>{w.get('balance', '—')} TON</b>")
            if w.get("depositAddress"):
                lines.append(f"📥 آدرسِ واریز: <code>{w['depositAddress']}</code>")
            if w.get("depositMemo"):
                lines.append(f"📝 ممو (حتماً وارد کن): <code>{w['depositMemo']}</code>")
        except _tg.TgToolsError as e:
            lines.append(f"⚠️ استعلامِ موجودی ناموفق: {e.message}")
        try:
            p = await _tg.get_prices()
            if p.get("stars_price_ton"):
                lines.append(f"⭐ قیمتِ هر استارز: ≈ {p['stars_price_ton']} TON (با کمیسیونِ حسابِ تو)")
        except _tg.TgToolsError:
            pass
    else:
        lines.append("🔑 کلید: ثبت نشده ❌\n\nاز tg-tools.shop ← Settings ← API Keys یک کلید (tgt_...) بساز و اینجا ثبت کن.")
    lines.append(f"\n🚀 ارسالِ خودکارِ سفارش‌هایِ کیف‌پول: <b>{'روشن ✅' if auto_on else 'خاموش'}</b>")
    prm_on = (await _get_setting(bot_id, "tgtools_premium", "0")) == "1"   # PATCH_TGTOOLS_PREMIUM_V1
    lines.append(f"🌟 پرمیومِ خودکار با TGTools (قیمت از نرخِ TGTools): <b>{'روشن ✅' if prm_on else 'خاموش'}</b>")
    gft_on = (await _get_setting(bot_id, "tgtools_gift", "0")) == "1"   # PATCH_TGTOOLS_GIFT_V1
    lines.append(f"🎁 گیفتِ خودکار با TGTools (سفارش‌هایِ «تحویلِ دستی»): <b>{'روشن ✅' if gft_on else 'خاموش'}</b>")
    if prm_on:
        try:
            _tr = int(float(await _get_setting(bot_id, "nft_ton_rate", "0") or 0))
        except (TypeError, ValueError):
            _tr = 0
        if _tr <= 0:
            lines.append("⚠️ نرخِ تومانیِ هر TON تنظیم نشده (در «مارکتِ NFT ← نرخ‌ها»)؛ تا آن موقع قیمتِ پرمیوم از فرمولِ استارزیِ قبلی می‌آید.")
    _rfd_on = (await _get_setting(bot_id, "tgtools_cancel_refund", "1")) != "0"   # PATCH_TGTOOLS_CANCEL_REFUND_V1
    lines.append("• سفارش‌هایِ کارت‌به‌کارت بعد از تاییدِ رسیدِ خودت، با دکمهٔ «🤖 ارسال خودکار» می‌روند.\n"
                 "• هر خطایی → سفارش به تحویلِ دستی برمی‌گردد و خبرت می‌کنم.\n"
                 f"• اگر TGTools سفارشی را که پذیرفته بود لغو کند: {'پول مشتری خودکار برمی‌گردد و پیام لغو برایش می‌رود ✅' if _rfd_on else 'سفارش به تحویل دستی برمی‌گردد (بازگشت خودکار خاموش است)'}.\n"
                 "• قیمتِ TGTools به TON است؛ نرخِ تومانیِ فروش را طوری بگذار که سود بماند.")
    rows = []
    rows.append([InlineKeyboardButton(text=("🔑 تغییرِ کلید" if key else "🔑 ثبتِ کلیدِ API"),
                                      callback_data="ssl:tgt:key")])
    if key:
        rows.append([InlineKeyboardButton(
            text=("🚀 ارسالِ خودکار: روشن ✅" if auto_on else "🚀 ارسالِ خودکار: خاموش"),
            callback_data="ssl:tgt:toggle",
            style=(ButtonStyle.SUCCESS if auto_on else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(
            text=("🌟 پرمیومِ خودکار: روشن ✅" if prm_on else "🌟 پرمیومِ خودکار: خاموش"),
            callback_data="ssl:tgt:prm",
            style=(ButtonStyle.SUCCESS if prm_on else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(
            text=("🎁 گیفتِ خودکار: روشن ✅" if gft_on else "🎁 گیفتِ خودکار: خاموش"),
            callback_data="ssl:tgt:gift",
            style=(ButtonStyle.SUCCESS if gft_on else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(
            text=("↩️ لغو توسط TGTools ← بازگشت خودکار وجه: روشن ✅" if _rfd_on
                  else "↩️ لغو توسط TGTools ← بازگشت خودکار وجه: خاموش"),
            callback_data="ssl:tgt:rfd",
            style=(ButtonStyle.SUCCESS if _rfd_on else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانیِ موجودی", callback_data="ssl:tgt")])
        async with Session() as s:
            pend = (await s.scalars(select(GiftOrder).where(
                GiftOrder.bot_id == bot_id, GiftOrder.gift_id.like("starsell_%"),
                GiftOrder.status == "auto_processing").order_by(GiftOrder.id.desc()).limit(10))).all()
        for o in pend:
            rows.append([InlineKeyboardButton(text=f"⏳ بررسیِ سفارش #{o.tracking_code or o.id} ({o.star_count:,}⭐)",
                                              callback_data=f"star:tgtchk:{o.id}")])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ کلید", callback_data="ssl:tgt:del",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _tgt_show_menu(cb_or_msg, bot_id: int, edit: bool = True) -> None:
    text, kb = await _tgt_menu_content(bot_id)
    if edit and isinstance(cb_or_msg, CallbackQuery):
        try:
            await cb_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
        await cb_or_msg.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        m = cb_or_msg.message if isinstance(cb_or_msg, CallbackQuery) else cb_or_msg
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


async def _tgt_owner_strict(cb: CallbackQuery, bot: Bot):
    """تنظیمِ کلیدِ پولی فقط با مالکِ ربات (نه ادمینِ فرعی)."""
    row = await _owner(cb, bot)
    if not row:
        return None
    if cb.from_user.id != row.owner_id:
        await cb.answer("⛔️ این بخش فقط برایِ مالکِ ربات است.", show_alert=True)
        return None
    return row


@smm_router.callback_query(F.data == "ssl:tgt")
async def ssl_tgt_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    await _tgt_show_menu(cb, row.id)
    await cb.answer()


@smm_router.callback_query(F.data == "ssl:tgt:key")
async def ssl_tgt_key_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.tgtools_key_in)
    await cb.message.answer(
        "🔑 کلیدِ API سایتِ TGTools را بفرست (شکلِ <code>tgt_...</code>).\n"
        "پیامِ تو بعدِ دریافت پاک می‌شود و کلید رمزنگاری‌شده ذخیره می‌شود.",
        parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.tgtools_key_in, F.text)
async def ssl_tgt_key_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    from tenant.bots.virtual_service import tgtools_api as _tg
    row = await get_bot_row(bot)
    await state.clear()
    if not row or msg.from_user.id != row.owner_id:
        return
    key = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        logger.debug("tgtools: حذفِ پیامِ کلید ممکن نشد", exc_info=True)
    if not key.startswith("tgt_") or len(key) < 12 or " " in key:
        await msg.answer("❌ کلیدِ نامعتبر (باید با tgt_ شروع شود). دوباره از منو امتحان کن.")
        return
    try:
        await _tg.get_wallet(key)
    except _tg.TgToolsError as e:
        await msg.answer(f"❌ TGTools این کلید را قبول نکرد: {e.message}")
        return
    try:
        from wallet_crypto import encrypt_secret
        await _set_setting(row.id, "tgtools_api_key", encrypt_secret(key))
    except Exception:
        logger.exception("tgtools: ذخیرهٔ کلید ناموفق")
        await msg.answer("⛔️ ذخیرهٔ رمزنگاری‌شده ممکن نشد (WALLET_ENC_KEY در .env تنظیم است؟).")
        return
    await msg.answer("✅ کلید ذخیره شد.")
    await _tgt_show_menu(msg, row.id, edit=False)


@smm_router.callback_query(F.data == "ssl:tgt:toggle")
async def ssl_tgt_toggle(cb: CallbackQuery, bot: Bot):
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
        return
    cur = (await _get_setting(row.id, "tgtools_auto", "0")) == "1"
    await _set_setting(row.id, "tgtools_auto", "0" if cur else "1")
    await cb.answer("خاموش شد؛ همه‌چیز دستی است." if cur
                    else "روشن شد — سفارش‌هایِ پرداخت‌شده با کیف‌پول خودکار می‌روند.", show_alert=True)
    await _tgt_show_menu(cb, row.id)


@smm_router.callback_query(F.data == "ssl:tgt:prm")
async def ssl_tgt_prm_toggle(cb: CallbackQuery, bot: Bot):
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
        return
    cur = (await _get_setting(row.id, "tgtools_premium", "0")) == "1"
    await _set_setting(row.id, "tgtools_premium", "0" if cur else "1")
    await cb.answer("خاموش شد؛ پرمیوم مثلِ قبل (استارزِ ربات/تحویلِ دستی) است." if cur
                    else "روشن شد — پرمیوم با TGTools خریده می‌شود و قیمت از نرخِ TGTools می‌آید.", show_alert=True)
    await _tgt_show_menu(cb, row.id)


@smm_router.callback_query(F.data == "ssl:tgt:gift")
async def ssl_tgt_gift_toggle(cb: CallbackQuery, bot: Bot):
    """=== PATCH_TGTOOLS_GIFT_V1 ==="""
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    if not await _tgt_key(row.id):
        await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
        return
    cur = (await _get_setting(row.id, "tgtools_gift", "0")) == "1"
    await _set_setting(row.id, "tgtools_gift", "0" if cur else "1")
    await cb.answer("خاموش شد؛ گیفت‌هایِ «تحویلِ دستی» مثلِ قبل دستی هستند (دکمهٔ «🤖 خرید با TGTools» همچنان هست)." if cur
                    else "روشن شد — گیفتِ کاتالوگیِ «تحویلِ دستی» (و بعد از تاییدِ رسیدِ کارت) خودکار با TGTools خریده می‌شود.",
                    show_alert=True)
    await _tgt_show_menu(cb, row.id)


@smm_router.callback_query(F.data == "ssl:tgt:rfd")
async def ssl_tgt_refund_toggle(cb: CallbackQuery, bot: Bot):
    """=== PATCH_TGTOOLS_CANCEL_REFUND_V1 ==="""
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    cur = (await _get_setting(row.id, "tgtools_cancel_refund", "1")) != "0"
    await _set_setting(row.id, "tgtools_cancel_refund", "0" if cur else "1")
    await cb.answer("خاموش شد؛ اگر TGTools سفارشی را لغو کند، سفارش به تحویل دستی برمی‌گردد و مشتری خبری نمی‌گیرد." if cur
                    else "روشن شد — اگر TGTools سفارشی را لغو کند، پول مشتری برمی‌گردد و پیام لغو برایش می‌رود.",
                    show_alert=True)
    await _tgt_show_menu(cb, row.id)


@smm_router.callback_query(F.data == "ssl:tgt:del")
async def ssl_tgt_del(cb: CallbackQuery, bot: Bot):
    row = await _tgt_owner_strict(cb, bot)
    if not row:
        return
    await _set_setting(row.id, "tgtools_api_key", "")
    await _set_setting(row.id, "tgtools_auto", "0")
    await _set_setting(row.id, "tgtools_premium", "0")   # PATCH_TGTOOLS_PREMIUM_V1
    await _set_setting(row.id, "tgtools_gift", "0")      # PATCH_TGTOOLS_GIFT_V1
    await cb.answer("🗑 کلید حذف و ارسالِ خودکار (استارز/پرمیوم/گیفت) خاموش شد.", show_alert=True)
    await _tgt_show_menu(cb, row.id)


# ═══════════════════════════════════════════
#  فروشگاهِ رنگیِ شمارهٔ مجازی — طرفِ مشتری
#  === PATCH_VNUM_CUSTOMER_SHOP_V1 ===
# ═══════════════════════════════════════════
# دقیقاً همان چیزی که در اسکرین‌شات‌ها بود: فهرستِ کشورها با رنگِ سبز
# (موجود) یا قرمز (ناموجود). برخلافِ مسیرِ عمومیِ SMM (که لینک/تعداد
# می‌پرسد)، شمارهٔ مجازی ذاتاً نه لینک دارد نه تعدادش فرق می‌کند (همیشه
# ۱)، پس یک مسیرِ خریدِ جداگانه و ساده‌تر برایش ساختیم.

# === PATCH_VNUM_SHOP_UNIFIED_V2 ===
# بازطراحیِ کاملِ فروشگاه بعد از گزارشِ ادمین‌ها. چهار ایرادِ واقعی:
#
#  ۱) «چرا دو بار و با فاصله نشون می‌ده؟» — کشورهایِ دستی و کشورهایِ
#     خودکار دو فهرستِ جدا بودند، با یک دکمهٔ جداکننده وسطشان و صفحه‌بندی
#     که فقط رویِ فهرستِ اول کار می‌کرد. حالا هر دو در یک فهرستِ واحد و
#     مرتب‌شده بر اساسِ قیمت ادغام می‌شوند و صفحه‌بندی رویِ کلِ آن است.
#  ۲) «چرا اسمِ ارائه‌دهنده هست؟» — «📡 کشورهای خودکار OzVinoo» نامِ
#     تأمین‌کننده را به مشتریِ نهایی لو می‌داد. مشتری نباید بداند شماره از
#     کجا می‌آید؛ آن دکمه کلاً حذف شد.
#  ۳) «چرا دکمهٔ بازگشت به منویِ اصلی نیست؟» — ردیفِ بازگشت فقط وقتی
#     اضافه می‌شد که inline_back_row چیزی برگرداند و بعد از nav می‌آمد؛
#     حالا همیشه و به‌عنوانِ آخرین ردیف هست.
#  ۴) وضعیتِ کشورهایِ خودکار همیشه ✅ سبز نشان داده می‌شد، حتی وقتی
#     ناموجود بودند — چون برخلافِ کشورهایِ دستی، is_active شان خوانده
#     نمی‌شد و دکمه هم همیشه به خرید وصل بود.
#
# طرحِ نهایی مطابقِ درخواست: یک سرستون (کشور | قیمت | وضعیت) و بعد ردیف‌ها
# با متنِ خوانایِ «موجود ✅ / ناموجود ❌» به‌جایِ فقط یک علامت.

_VNUM_PER_PAGE = 10


def _vnum_shop_price(svc) -> int:
    """قیمتِ واقعیِ یک شماره (sell_rate به‌صورتِ «هر ۱۰۰۰» ذخیره می‌شود)."""
    try:
        return int(svc.sell_rate) // 1000
    except (TypeError, ValueError):
        return 0


def _vnum_shop_callback(svc) -> str:
    """مقصدِ دکمه: خریدِ خودکار، خریدِ دستی، یا پیامِ ناموجود."""
    if not svc.is_active:
        return f"vnum:unavail:{svc.id}"
    if str(svc.service_id or "").startswith("ozv:"):
        return f"vnum:ozbuy:{svc.id}"
    return f"vnum:buy:{svc.id}"


async def show_vnum_shop_to_user(bot: Bot, target: Message, bot_id: int, page: int = 0,
                                 only: str | None = None, tier: str | None = None) -> None:
    """only: None = دستی+خودکار با هم (رفتارِ قدیم) | "manual" | "auto"

    === PATCH_VNUM_SHOP_SPLIT_V1 ===
    قبلاً فقط یک دکمهٔ «شمارهٔ مجازی تلگرام» بود که همیشه هردو نوع (کشورهایِ
    دستیِ ادمین + کشورهایِ خودکارِ OzVinoo) را با هم، مخلوط و مرتب‌شده بر
    اساسِ قیمت، نشان می‌داد. برایِ ادمینی که می‌خواهد فقط یکی را بفروشد
    (یا با دو دکمهٔ جدا بفروشد)، این مخلوط‌شدن گیج‌کننده بود. حالا با
    پارامترِ only می‌شود فقط دستی، فقط خودکار، یا هردو (پیش‌فرض) را نشان داد.
    """
    # === PATCH_VNUM_SHOP_DEDUP_V1 ===
    # باگِ V2: کشورها دوبار نشان داده می‌شدند. علتش این بود که سرویس‌هایِ
    # OzVinoo موقعِ همگام‌سازی با همان `category = _VNUM_CATEGORY` ذخیره
    # می‌شوند، پس `_vnum_list` از قبل شاملشان است — و `ozvinoo_shop_items`
    # دوباره همان ردیف‌ها را برمی‌گرداند. یک منبع کافی است؛ dict برایِ
    # اطمینان، تکراری‌هایِ احتمالیِ آینده را هم بر اساسِ id حذف می‌کند.
    items = list({v.id: v for v in await _vnum_list(bot_id)}.values())
    if only == "manual":
        items = [v for v in items if v.is_manual]
    elif only == "auto":
        items = [v for v in items if not v.is_manual]
    if not items:
        msg = {
            "manual": "📱 فعلاً هیچ کشورِ دستی‌ای برایِ شمارهٔ مجازی ثبت نشده.",
            "auto": "📱 فعلاً هیچ کشورِ خودکاری (OzVinoo) وصل نشده.",
        }.get(only, "📱 فعلاً هیچ کشوری برایِ شمارهٔ مجازی ثبت نشده.")
        await target.answer(msg)
        return

    # === PATCH_VNUM_TIER_CHOICE_V1 ===
    # درخواستِ ادمین (فدایی): وقتی دو ردهٔ «بدونِ ریپورت» و «ارزان» فعال است،
    # قبل از دیدنِ لیست بپرس کدام شماره‌ها را می‌خواهد؛ سپس فقط همان لیست.
    if only != "manual" and any(str(v.service_id or "").endswith(":c") for v in items):
        _cheap = [v for v in items if str(v.service_id or "").endswith(":c")]
        _norm = [v for v in items if not str(v.service_id or "").endswith(":c")]
        if tier not in ("nr", "ch"):
            _o = only or "all"
            _rows = [
                [InlineKeyboardButton(text="🛡 شماره‌های بدونِ ریپورت", callback_data=f"vnum:shop:0:{_o}:nr",
                                      style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="💸 شماره‌های ارزان", callback_data=f"vnum:shop:0:{_o}:ch",
                                      style=ButtonStyle.PRIMARY)],
            ]
            from tenant import inline_back_row
            _br = await inline_back_row(bot_id)
            _rows.append(_br or [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="mbtn:home",
                                                      style=ButtonStyle.DANGER)])
            await target.answer(
                "📱 <b>شمارهٔ مجازیِ تلگرام</b>\n\n"
                "می‌خواهی لیستِ کدام شماره‌ها را ببینی؟\n\n"
                "🛡 <b>بدونِ ریپورت</b>: کیفیتِ بالاتر و مطمئن‌تر، کمی گران‌تر\n"
                "💸 <b>ارزان</b>: قیمتِ پایین‌تر",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=_rows), parse_mode="HTML")
            return
        items = _cheap if tier == "ch" else _norm
        if not items:
            await target.answer("📱 فعلاً شمارهٔ این رده موجود نیست.")
            return

    # فهرستِ واحد: اول موجودها، بعد ناموجودها؛ داخلِ هر گروه ارزان‌ترین بالا.
    items.sort(key=lambda v: (not bool(v.is_active), _vnum_shop_price(v), v.name or ""))

    pages = max(1, -(-len(items) // _VNUM_PER_PAGE))
    page = max(0, min(int(page), pages - 1))
    start = page * _VNUM_PER_PAGE
    chunk = items[start:start + _VNUM_PER_PAGE]

    # سرستون — دکمه نیست، فقط برچسب. === PATCH_VNUM_HEADER_RED_V1 === به‌درخواستِ ادمین قرمز.
    rows = [[
        InlineKeyboardButton(text="🌍 کشور", callback_data="noop", style=ButtonStyle.DANGER),
        InlineKeyboardButton(text="💰 قیمت", callback_data="noop", style=ButtonStyle.DANGER),
        InlineKeyboardButton(text="🔢 وضعیت", callback_data="noop", style=ButtonStyle.DANGER),
    ]]
    for it in chunk:
        available = bool(it.is_active)
        style = ButtonStyle.SUCCESS if available else ButtonStyle.DANGER
        cbd = _vnum_shop_callback(it)
        rows.append([
            InlineKeyboardButton(text=it.name, callback_data=cbd, style=style),
            InlineKeyboardButton(text=f"{_vnum_shop_price(it):,}", callback_data=cbd, style=style),
            InlineKeyboardButton(text="موجود ✅" if available else "ناموجود ❌",
                                 callback_data=cbd, style=style),
        ])

    # === PATCH_VNUM_SHOP_SPLIT_V1 ===
    _suffix = f":{only}" if only else ""
    if tier in ("nr", "ch"):
        _suffix = f":{only or 'all'}:{tier}"
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ صفحهٔ قبل",
                                            callback_data=f"vnum:shop:{page-1}{_suffix}",
                                            style=ButtonStyle.PRIMARY))
        nav.append(InlineKeyboardButton(text=f"{page+1} از {pages}",
                                        callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="صفحهٔ بعد ➡️",
                                            callback_data=f"vnum:shop:{page+1}{_suffix}",
                                            style=ButtonStyle.PRIMARY))
        rows.append(nav)

    # همیشه راهِ بازگشت — آخرین ردیف، بدونِ شرط.
    from tenant import inline_back_row
    br = await inline_back_row(bot_id)
    rows.append(br or [InlineKeyboardButton(text="🏠 منوی اصلی",
                                            callback_data="mbtn:home",
                                            style=ButtonStyle.DANGER)])

    in_stock = sum(1 for v in items if v.is_active)
    title = {
        "manual": "📱 <b>شمارهٔ مجازیِ تلگرام (دستی)</b>",
        "auto": "📱 <b>شمارهٔ مجازیِ تلگرام (خودکار)</b>",
    }.get(only, "📱 <b>شمارهٔ مجازیِ تلگرام</b>")
    await target.answer(
        f"{title}\n\n"
        f"🌍 {len(items):,} کشور · 🟢 {in_stock:,} موجود\n\n"
        "کشورِ موردنظرت را انتخاب کن؛ پس از خرید، شماره و سپس کدِ تأیید "
        "همین‌جا برایت فرستاده می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("vnum:shop:"))
async def vnum_shop(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    # === PATCH_VNUM_SHOP_SPLIT_V1 ===
    # فرمت: vnum:shop:<page> یا vnum:shop:<page>:<only> (فقط برایِ صفحه‌بندیِ
    # حالتِ فیلترشده، تا با ورق‌زدن، فیلتر گم نشود).
    parts = cb.data.split(":")
    page = int(parts[2])
    only = parts[3] if len(parts) > 3 else None
    if only == "all":
        only = None
    tier = parts[4] if len(parts) > 4 else None
    await cb.answer()
    await show_vnum_shop_to_user(bot, _EditFirstV(cb.message), row.id, page, only=only, tier=tier)


@smm_router.callback_query(F.data.startswith("vnum:buy:"))
async def vnum_buy(cb: CallbackQuery, bot: Bot):
    """قبلاً اینجا مستقیم فاکتورِ کلیِ کشور را نشان می‌داد. حالا اول
    فهرستِ خودِ شماره‌هایِ واقعیِ آن کشور (موجود/فروخته‌شده) را نشان
    می‌دهد — PATCH_VNUM_REAL_NUMBERS_V1."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    item_id = int(cb.data.split(":")[2])
    await cb.answer()
    await _vnum_customer_numbers_render(_EditFirstV(cb.message), row.id, item_id, 0)


async def _vnum_customer_numbers_render(target: Message, bot_id: int, country_id: int, page: int) -> None:
    per_page = 12
    async with Session() as s:
        it = await s.get(SmmService, country_id)
        if not it or it.bot_id != bot_id or it.category != _VNUM_CATEGORY:
            await target.answer("این کشور دیگر موجود نیست.")
            return
        if not it.is_active:
            await target.answer("❌ این کشور فعلاً موجود نیست.")
            return
        nums = (await s.scalars(select(VnumNumber).where(
            VnumNumber.country_service_pk == country_id).order_by(VnumNumber.id))).all()
    real_price = int(it.sell_rate) // 1000
    # === PATCH_VNUM_SMSPOOL_AUTO_V1 ===
    # کشورِ متصل‌به‌SMSPool انبارِ دستی ندارد — مستقیم دکمهٔ خریدِ آنی نشان
    # داده می‌شود؛ خریدِ واقعی و دریافتِ کد کاملاً خودکار انجام می‌شود.
    if (it.service_id or "").startswith("sp:"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 خریدِ آنی (خودکار) — {real_price:,} تومان",
                                  callback_data=f"vnum:autobuy:{country_id}", style=ButtonStyle.SUCCESS)],
            *await _vsd_rows_lazy(bot_id, "vnum", real_price),   # PATCH_VS_DISCOUNT_V1
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vnum:shop:0", style=ButtonStyle.DANGER)],
        ])
        await target.answer(
            f"<b>{it.name}</b> — {real_price:,} تومان\n\n"
            "🤖 این شماره به‌صورتِ کاملاً خودکار صادر می‌شود — بلافاصله بعدِ "
            "پرداخت شماره را می‌گیری و کدِ پیامک هم خودش برایت ارسال می‌شود.",
            reply_markup=kb, parse_mode="HTML")
        return
    if not nums:
        # === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
        # باگ/درخواست: قبلاً اگر انبارِ یک کشور خالی بود، مشتری اصلاً امکانِ
        # خرید نداشت. حالا اگر ادمین «تحویلِ دستیِ وقتِ نبودِ انبار» را روشن
        # کرده باشد، همچنان می‌تواند بخرد و ادمین بعداً دستی شماره را
        # می‌فرستد.
        manual_mode = await _get_setting(bot_id, "vnum_manual_delivery", "0") == "1"
        if manual_mode:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🖐 خرید با تحویلِ دستی — {real_price:,} تومان",
                                      callback_data=f"vnum:manualbuy:{country_id}",
                                      style=ButtonStyle.SUCCESS)],
                *await _vsd_rows_lazy(bot_id, "vnum", real_price),   # PATCH_VS_DISCOUNT_V1
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vnum:shop:0", style=ButtonStyle.DANGER)],
            ])
            # === PATCH_VNUM_DOUBLE_MOBILE_EMOJI_FIX_V1 ===
            # باگِ گزارش‌شده: it.name از قبل با «📱 » شروع می‌شود (رجوع کن
            # به vnum_add_price_save که موقعِ ساختِ کشور این پیشوند را
            # همیشه اضافه می‌کند) — این‌جا دوباره «📱 » اضافه می‌شد، یعنی
            # متنِ نهایی واقعاً دو تا کاراکترِ 📱 پشتِ‌سرِهم داشت. اگر ادمین
            # 📱 را به یک ایموجیِ پرمیوم نگاشت کرده باشد، هر دو (درست و
            # طبقِ انتظار) تبدیل می‌شوند — نتیجه‌اش دو آیکونِ پرمیومِ کنارِ
            # هم بود، نه یک باگِ واقعی در خودِ موتورِ جایگزینیِ ایموجی.
            await target.answer(
                f"<b>{it.name}</b> — {real_price:,} تومان\n\n"
                "فعلاً شمارهٔ آماده در انبار نیست، ولی می‌تونی همین حالا بخری — "
                "بعدِ پرداخت، شماره‌ات طیِ چند دقیقه از طرفِ ادمین برات فرستاده می‌شه:",
                reply_markup=kb, parse_mode="HTML")
            return
        # === PATCH_VNUM_DOUBLE_MOBILE_EMOJI_FIX_V1 ===
        await target.answer(f"{it.name} فعلاً هیچ شماره‌ای موجود ندارد — بعداً دوباره سر بزن.")
        return
    start, end = page * per_page, page * per_page + per_page
    rows = []
    for n in nums[start:end]:
        if n.status == "available":
            rows.append([InlineKeyboardButton(text=f"🟢 {n.phone_number}",
                                              callback_data=f"vnum:npick:{n.id}", style=ButtonStyle.SUCCESS)])
        else:
            rows.append([InlineKeyboardButton(text=f"🔴 {n.phone_number} (فروخته‌شده)",
                                              callback_data="vnum:numsold")])
    # === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
    # اگر همهٔ شماره‌هایِ ثبت‌شده فروخته شده باشند (هیچ سبزی نمانده)، همون
    # گزینهٔ خریدِ دستی رو هم نشون بده — دقیقاً مثلِ حالتی که از اول انبار
    # خالی بوده.
    if not any(n.status == "available" for n in nums):
        manual_mode = await _get_setting(bot_id, "vnum_manual_delivery", "0") == "1"
        if manual_mode:
            rows.insert(0, [InlineKeyboardButton(
                text=f"🖐 خرید با تحویلِ دستی — {real_price:,} تومان",
                callback_data=f"vnum:manualbuy:{country_id}", style=ButtonStyle.SUCCESS)])
            for _i, _r in enumerate(await _vsd_rows_lazy(bot_id, "vnum", real_price), start=1):   # PATCH_VS_DISCOUNT_V1
                rows.insert(_i, _r)
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"vnum:numspage:{country_id}:{page-1}"))
    if end < len(nums):
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"vnum:numspage:{country_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="vnum:shop:0", style=ButtonStyle.DANGER)])
    # === PATCH_VNUM_DOUBLE_MOBILE_EMOJI_FIX_V1 ===
    # همان باگِ دوتا-📱 (بالاتر توضیح داده شده) — it.name از قبل «📱 »
    # دارد.
    await target.answer(
        f"<b>{it.name}</b> — {real_price:,} تومان\n\n"
        "🟢 سبز یعنی موجود و قابل‌خرید، 🔴 قرمز یعنی قبلاً فروخته شده.\n"
        "روی یکی از شماره‌هایِ سبز بزن تا بخری:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("vnum:numspage:"))
async def vnum_numspage(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    _, _, country_id, page = cb.data.split(":")
    await cb.answer()
    await _vnum_customer_numbers_render(_EditFirstV(cb.message), row.id, int(country_id), int(page))


@smm_router.callback_query(F.data == "vnum:numsold")
async def vnum_numsold(cb: CallbackQuery):
    await cb.answer("❌ این شماره قبلاً فروخته شده — یکی از شماره‌هایِ سبز را انتخاب کن.", show_alert=True)


@smm_router.callback_query(F.data.startswith("vnum:npick:"))
async def vnum_number_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    num_id = int(cb.data.split(":")[2])
    async with Session() as s:
        n = await s.get(VnumNumber, num_id)
        if not n or n.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if n.status != "available":
            await cb.answer("❌ این شماره قبلاً فروخته شده.", show_alert=True)
            return
        country = await s.get(SmmService, n.country_service_pk)
    real_price = int(country.sell_rate) // 1000
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 از کیف‌پول", callback_data=f"vnum:npay:{num_id}",
                              style=ButtonStyle.SUCCESS)],
        *await _vsd_rows_lazy(row.id, "vnum", real_price),   # PATCH_VS_DISCOUNT_V1
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"vnum:buy:{country.id}", style=ButtonStyle.DANGER)],
    ])
    await cb.answer()
    await _EditFirstV(cb.message).answer(
        f"🧾 <b>فاکتورِ خرید</b>\n\n"
        f"شماره: <code>{n.phone_number}</code>\n"
        f"کشور: {country.name}\n"
        f"قیمت: {real_price:,} تومان",
        reply_markup=kb, parse_mode="HTML")


@smm_router.callback_query(F.data.startswith("vnum:npay:"))
async def vnum_number_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """خریدِ اتمیک: با یک UPDATE شرطی روی status='available'، اگر دو
    مشتری هم‌زمان همین شماره را بزنند، فقط یکی موفق می‌شود — دیگری
    پیامِ «قبلاً فروخته شد» می‌گیرد، نه این‌که دو نفر یک شماره بخرند."""
    from tenant import get_bot_row, get_or_create_user, _gen_tracking
    from sqlalchemy import update as sa_update
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, vsd_clear, VsdError   # PATCH_VS_DISCOUNT_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    num_id = int(cb.data.split(":")[2])
    async with Session() as s:
        n = await s.get(VnumNumber, num_id)
        if not n or n.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if n.status != "available":
            await cb.answer("❌ این شماره قبلاً فروخته شده.", show_alert=True)
            return
        country = await s.get(SmmService, n.country_service_pk)
    real_price = int(country.sell_rate) // 1000

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id)
        # === PATCH_VS_DISCOUNT_V1 === کدِ تخفیف (قبل از چکِ موجودی)
        _dc_id, _dc_amt = None, 0
        _dc_code = vsd_pick(await state.get_data(), cb.message.message_id, "vnum")
        if _dc_code:
            try:
                real_price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_dc_code, kind="vnum", price=real_price)
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
        if float(b.balance) < real_price:
            await cb.answer(
                f"❌ موجودیِ کیف‌پول کافی نیست. کمبود: {real_price - int(b.balance):,} تومان",
                show_alert=True)
            return
        # === PATCH_VNUM_ATOMIC_PURCHASE_V1 ===
        result = await s.execute(
            sa_update(VnumNumber).where(
                VnumNumber.id == num_id, VnumNumber.status == "available"
            ).values(status="sold", sold_to_user_pk=b.id))
        if result.rowcount == 0:
            await s.rollback()
            await cb.answer("❌ متاسفانه یک لحظه پیش همین شماره فروخته شد — یکیِ دیگر را انتخاب کن.",
                            show_alert=True)
            return
        b.balance = float(b.balance) - real_price
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        o = SmmOrder(
            bot_id=row.id, user_telegram_id=cb.from_user.id,
            smm_service_pk=country.id, service_id=country.service_id, service_name=country.name,
            link="", quantity=1, price_toman=real_price,
            status="completed", tracking_code=trk,
            discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(o)
        if _dc_id:
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-real_price,
                        kind="smm_order", note=f"خریدِ شمارهٔ مجازی: {country.name}"))
        await s.commit()
        order_id = o.id
        n2 = await s.get(VnumNumber, num_id)
        if n2 is not None and await _is_foreign(n2, bot):   # PATCH_TENANT_SCOPE_V1
            n2 = None
        n2.sold_order_pk = order_id
        n2.sold_at = func.now()
        await s.commit()

    await vsd_clear(state)   # PATCH_VS_DISCOUNT_V1
    await _delete_shop_panel(cb)
    await cb.answer("✅ خریداری شد")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 درخواستِ کدِ تایید", callback_data=f"vnum:reqcode:{num_id}",
                             style=ButtonStyle.PRIMARY)]])
    from tenant import _jalali_date_time, _purchase_alert
    from datetime import datetime as _datetime
    _jdate, _jtime = _jalali_date_time(_datetime.utcnow())
    await cb.message.answer(
        f"✅ <b>سفارش شما با موفقیت تکمیل شد!</b> 🎉\n\n"
        f"👇🏻 <b>اطلاعات سفارش:</b>\n\n"
        f"⚜️ نوع خدمات: 📱 شمارهٔ مجازی تلگرام\n"
        f"🌍 کشور: {country.name}\n"
        f"📱 شماره: <code>{n.phone_number}</code>\n"
        f"💳 مبلغ نهایی پرداخت‌شده: {real_price:,} تومان\n"
        f"🔑 کد پیگیری سفارش: <code>{trk}</code>\n"
        f"🕔 زمان انجام: {_jdate} {_jtime}\n\n"
        f"🌟 سفارش شما با موفقیت انجام شد و شمارهٔ مجازی به شما تحویل داده شد.\n"
        f"🙏 از اعتماد و انتخاب شما سپاسگزاریم!\n\n"
        f"برای دریافت کد تأیید ارسال‌شده به این شماره، دکمهٔ زیر را بزنید:",
        reply_markup=kb, parse_mode="HTML")
    try:
        await _purchase_alert(
            bot, row, buyer=buyer, section="خدمات مجازی تلگرام",
            detail=f"📱 شمارهٔ مجازی تلگرام — {country.name}",
            amount=real_price, order_id=order_id, product_id=country.id,
            tracking_code=trk, count=1)
    except Exception:
        logger.exception("ارسال اعلان خرید شمارهٔ مجازی به کانال اعتمادسازی ناموفق بود")


# === PATCH_VNUM_MANUAL_DELIVERY_V1 ===
@smm_router.callback_query(F.data.startswith("vnum:manualbuy:"))
async def vnum_manual_buy_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """خریدِ شماره وقتی انبار خالی است — سفارش ثبت و پرداخت می‌شود، ولی
    خودِ شماره را ادمین بعداً دستی می‌فرستد (تابعِ vnum_admin_sendnum)."""
    from tenant import get_bot_row, get_or_create_user, _gen_tracking, _notify_admins
    from tenant.bots.virtual_service.vs_discount import vsd_pick, vsd_charge, vsd_record, vsd_clear, VsdError   # PATCH_VS_DISCOUNT_V1
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    country_id = int(cb.data.split(":")[2])
    manual_mode = await _get_setting(row.id, "vnum_manual_delivery", "0") == "1"
    if not manual_mode:
        await cb.answer("این گزینه دیگر فعال نیست.", show_alert=True)
        return
    async with Session() as s:
        country = await s.get(SmmService, country_id)
        if not country or country.bot_id != row.id or not country.is_active:
            await cb.answer("این کشور دیگر موجود نیست.", show_alert=True)
            return
    real_price = int(country.sell_rate) // 1000

    buyer, _ = await get_or_create_user(
        row.id, cb.from_user.id, cb.from_user.full_name, cb.from_user.username or "",
        return_created=True)
    async with Session() as s:
        b = await s.get(BotUser, buyer.id, with_for_update=True)  # قفلِ ردیف: دابل‌کلیک = دو خرید با یک پول
        # === PATCH_VS_DISCOUNT_V1 ===
        _dc_id, _dc_amt = None, 0
        _dc_code = vsd_pick(await state.get_data(), cb.message.message_id, "vnum")
        if _dc_code:
            try:
                real_price, _dc_id, _dc_amt = await vsd_charge(
                    s, bot_id=row.id, tg_id=cb.from_user.id, code=_dc_code, kind="vnum", price=real_price)
            except VsdError as _e:
                await cb.answer(f"❌ {_e}", show_alert=True)
                return
        if float(b.balance) < real_price:
            await cb.answer(
                f"❌ موجودیِ کیف‌پول کافی نیست. کمبود: {real_price - int(b.balance):,} تومان",
                show_alert=True)
            return
        b.balance = float(b.balance) - real_price
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, row.id)
        o = SmmOrder(
            bot_id=row.id, user_telegram_id=cb.from_user.id,
            smm_service_pk=country.id, service_id=country.service_id, service_name=country.name,
            link="", quantity=1, price_toman=real_price,
            status="awaiting_admin", tracking_code=trk,
            note=f"خریدِ دستیِ شمارهٔ مجازی — انبار خالی بود، شماره را باید ادمین دستی بفرستد.",
            discount_code_id=_dc_id, discount_amount=int(_dc_amt or 0))   # PATCH_VS_DISCOUNT_V1
        s.add(o)
        if _dc_id:
            await vsd_record(s, bot_id=row.id, tg_id=cb.from_user.id, code_id=_dc_id, discount=_dc_amt)
        s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=-real_price,
                        kind="smm_order", note=f"خریدِ شمارهٔ مجازی (دستی): {country.name}"))
        await s.commit()
        order_id = o.id

    await vsd_clear(state)   # PATCH_VS_DISCOUNT_V1
    await _delete_shop_panel(cb)
    await cb.answer("✅ پرداخت شد")
    from tenant import _purchase_alert
    await cb.message.answer(
        f"✅ <b>پرداخت شما با موفقیت ثبت شد!</b> 🎉\n\n"
        f"👇🏻 <b>اطلاعات سفارش:</b>\n\n"
        f"⚜️ نوع خدمات: 📱 شمارهٔ مجازی تلگرام\n"
        f"🌍 کشور: {country.name}\n"
        f"💳 مبلغ نهایی پرداخت‌شده: {real_price:,} تومان\n"
        f"🔑 کد پیگیری سفارش: <code>{trk}</code>\n"
        f"🕔 زمان ثبت سفارش: اکنون\n\n"
        f"⏳ شمارهٔ شما برای تحویل دستی به مدیریت ارسال شد و پس از آماده‌سازی در همین‌جا برایتان فرستاده می‌شود.\n"
        f"🙏 از اعتماد و انتخاب شما سپاسگزاریم!",
        parse_mode="HTML")
    # === PATCH_TRUST_ONLY_AFTER_DELIVERY_V1 ===
    # اعلانِ کانالِ اعتمادسازی برایِ تحویلِ دستی اینجا (قبل از ارسالِ شماره توسطِ ادمین) حذف شد؛
    # همان اعلان بعد از تحویل (تکمیلِ تحویلِ دستی) ارسال می‌شود، پس مشتری گزارشِ زودهنگام نمی‌بیند.

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📮 ارسالِ شماره", callback_data=f"vnum:sendnum:{order_id}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"vnum:manualreject:{order_id}",
                             style=ButtonStyle.DANGER),
    ]])
    # === PATCH_DEST_LABEL_USERNAME_LINE_V1 ===
    # درخواستِ ادمین: هرجا آیدیِ عددیِ خریدار نشان داده می‌شود، یوزرنیم
    # هم زیرش باشد — شماره‌ی مجازی همیشه برایِ خودِ خریدار است، پس از
    # همان هلپرِ «خودم» استفاده می‌کنیم.
    buyer_label = _self_dest_label(cb.from_user)
    await _notify_admins(
        bot, row,
        f"📱 سفارشِ شمارهٔ مجازی (تحویلِ دستی — انبار خالی) — #{await order_no(SmmOrder, order_id)}\n"
        f"🌍 کشور: {country.name}\n💰 {real_price:,} تومان\n"
        f"🎯 خریدار: {buyer_label}\n"
        f"🔖 کد: {trk}\n\n"
        f"باید خودت شماره را دستی برایِ خریدار جور کنی، بعد «📮 ارسالِ شماره» را بزن.",
        admin_kb)


@smm_router.callback_query(F.data.startswith("vnum:sendnum:"))
async def vnum_admin_sendnum_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(SmmOrder, order_id)
        if not o or o.bot_id != row.id:
            await cb.answer("سفارش پیدا نشد.", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
    from tenant import smart_cancel_kb
    await state.update_data(vnum_manual_order_id=order_id)
    await state.set_state(SmmFSM.vnum_manual_number)
    await cb.message.answer(
        f"📮 شمارهٔ سفارش #{await order_no(SmmOrder, order_id)} را بفرست (همان‌طور که به خریدار نشان داده می‌شود؛ "
        f"مثلاً: +989123456789):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_manual_number, F.text)
async def vnum_admin_sendnum_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    data = await state.get_data()
    order_id = data.get("vnum_manual_order_id")
    if not order_id or len(raw) < 5:
        await state.clear()
        raise SkipHandler
    await state.clear()
    async with Session() as s:
        o = await s.get(SmmOrder, order_id)
        if not o or o.bot_id != row.id or o.status != "awaiting_admin":
            await msg.answer("این سفارش پیدا نشد یا قبلاً رسیدگی شده.")
            return
        o.link = raw
        o.status = "completed"
        buyer_tg_id = o.user_telegram_id
        country_name = o.service_name
        trk = o.tracking_code
        order_amount = float(o.price_toman)
        buyer = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == buyer_tg_id))
        await s.commit()
    await msg.answer(f"✅ شماره برایِ سفارش #{await order_no(SmmOrder, order_id)} ثبت و برایِ خریدار فرستاده شد.")
    try:
        from tenant import _jalali_date_time
        from datetime import datetime as _datetime
        _jdate, _jtime = _jalali_date_time(_datetime.utcnow())
        request_code_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📩 درخواست کد تأیید",
                callback_data=f"vnum:reqcodeorder:{order_id}",
                style=ButtonStyle.PRIMARY),
        ]])
        await bot.send_message(
            buyer_tg_id,
            f"✅ <b>سفارش شما با موفقیت تکمیل شد!</b> 🎉\n\n"
            f"👇🏻 <b>اطلاعات سفارش:</b>\n\n"
            f"⚜️ نوع خدمات: 📱 شمارهٔ مجازی تلگرام\n"
            f"🌍 کشور: {country_name}\n"
            f"📱 شماره: <code>{raw}</code>\n"
            f"💳 مبلغ نهایی پرداخت‌شده: {int(order_amount):,} تومان\n"
            f"🔑 کد پیگیری سفارش: <code>{trk}</code>\n"
            f"🕔 زمان انجام: {_jdate} {_jtime}\n\n"
            f"🌟 شمارهٔ مجازی شما با موفقیت آماده و تحویل داده شد.\n"
            f"🙏 از اعتماد و انتخاب شما سپاسگزاریم!\n\n"
            f"برای دریافت کد تأیید، روی دکمهٔ زیر بزنید تا درخواست شما برای ادمین ارسال شود.",
            reply_markup=request_code_kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (اطلاع‌رسانیِ شمارهٔ دستی به خریدار)")


    try:
        from tenant import _purchase_alert
        await _purchase_alert(
            bot, row, buyer=buyer, section="خدمات مجازی تلگرام",
            detail=f"📱 شمارهٔ مجازی تلگرام (تکمیل تحویل دستی) — {country_name}",
            amount=order_amount, order_id=order_id, product_id=None,
            tracking_code=trk, count=1)
    except Exception:
        logger.exception("ارسال اعلان تکمیل شمارهٔ دستی به کانال اعتمادسازی ناموفق بود")

@smm_router.callback_query(F.data.startswith("vnum:manualreject:"))
async def vnum_admin_manual_reject(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(SmmOrder, order_id, with_for_update=True)
        if not o or o.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "refunded"
        buyer_tg_id = o.user_telegram_id
        trk = o.tracking_code
        price = float(o.price_toman)
        from tenant.bots.virtual_service.vs_discount import vsd_revert_order   # PATCH_VS_DISCOUNT_V1
        await vsd_revert_order(s, o, buyer_tg_id)
        b = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == buyer_tg_id).with_for_update())
        if b:
            b.balance = float(b.balance) + price
            s.add(WalletTxn(bot_id=row.id, user_id=b.id, amount=price, kind="charge",
                            note="بازگشتِ وجهِ سفارشِ شمارهٔ مجازیِ ردشده"))
        await s.commit()
    await cb.answer("سفارش رد و وجه بازگردانده شد")
    await _admin_msg_close(cb, f"❌ سفارش #{await order_no(SmmOrder, order_id)} رد شد.")
    try:
        await bot.send_message(
            buyer_tg_id,
            f"❌ متاسفانه سفارشِ شماره‌ات رد شد (شماره موجود نبود).\n"
            f"مبلغِ سفارش به کیف‌پولت بازگردانده شد.\n"
            f"🔖 کد پیگیری: <code>{trk}</code>", parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (اطلاع‌رسانیِ ردِ سفارشِ شمارهٔ دستی)")


@smm_router.callback_query(F.data.startswith("vnum:reqcode:"))
async def vnum_request_code(cb: CallbackQuery, bot: Bot):
    """PATCH_VNUM_CODE_REQUEST_V1 — دکمهٔ درخواستِ کد: به ادمین پیام
    می‌رود که فلان کاربر منتظرِ کدِ فلان شماره است؛ ادمین دستی کد را
    (از جایی که خودش شماره را می‌بیند، مثلاً یک اپ/سیم‌کارتِ واقعی)
    تایپ می‌کند و ربات مستقیم به همان مشتری می‌فرستد."""
    from tenant import get_bot_row, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    num_id = int(cb.data.split(":")[2])
    async with Session() as s:
        n = await s.get(VnumNumber, num_id)
        if not n or n.bot_id != row.id or n.sold_to_user_pk is None:
            await cb.answer("این شماره متعلق به تو نیست یا پیدا نشد.", show_alert=True)
            return
        buyer = await s.get(BotUser, n.sold_to_user_pk)
        if not buyer or buyer.telegram_id != cb.from_user.id:
            await cb.answer("این شماره متعلق به تو نیست.", show_alert=True)
            return
    await cb.answer("✅ درخواست برایِ ادمین فرستاده شد")
    await cb.message.answer(
        f"📩 درخواستت برایِ ادمین فرستاده شد — به‌محضِ آماده‌شدنِ کد، همین‌جا برایت می‌فرستد.")
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 پاسخ با کد", callback_data=f"vnum:sendcode:{num_id}",
                             style=ButtonStyle.PRIMARY)]])
    await _notify_admins(
        bot, row,
        f"🔔 درخواستِ کدِ تایید\n\n"
        f"📱 شماره: {n.phone_number}\n"
        f"👤 کاربر: {cb.from_user.full_name} (@{cb.from_user.username or '—'})\n\n"
        f"کدِ پیامک‌شده به این شماره را بررسی کن و با دکمهٔ زیر برایِ کاربر بفرست:",
        admin_kb)


@smm_router.callback_query(F.data.startswith("vnum:reqcodeorder:"))
async def vnum_request_code_order(cb: CallbackQuery, bot: Bot):
    """درخواست کد برای شماره‌ای که ادمین به‌صورت دستی تحویل داده است."""
    from tenant import get_bot_row, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(SmmOrder, order_id)
        if not o or o.bot_id != row.id or o.status != "completed" or not o.link:
            await cb.answer("این سفارش آمادهٔ درخواست کد نیست.", show_alert=True)
            return
        if o.user_telegram_id != cb.from_user.id:
            await cb.answer("این سفارش متعلق به شما نیست.", show_alert=True)
            return
        buyer = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        buyer_name = buyer.full_name if buyer else (cb.from_user.full_name or "—")
        buyer_username = buyer.username if buyer else (cb.from_user.username or "")
        phone = o.link
        country_name = o.service_name
        tracking = o.tracking_code
    await cb.answer("✅ درخواست کد برای ادمین ارسال شد")
    await cb.message.answer("📩 درخواست شما ثبت شد؛ پس از ارسال کد توسط ادمین، همین‌جا برایتان ارسال می‌شود.")
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 ارسال کد", callback_data=f"vnum:sendcodeorder:{order_id}", style=ButtonStyle.PRIMARY),
    ]])
    await _notify_admins(
        bot, row,
        f"🔔 <b>درخواست کد تأیید شمارهٔ مجازی</b>\n\n"
        f"👤 کاربر: {buyer_name}\n"
        f"🆔 یوزرنیم: @{buyer_username or '—'}\n"
        f"📱 شماره: <code>{phone}</code>\n"
        f"🌍 کشور: {country_name}\n"
        f"🔑 کد پیگیری: <code>{tracking}</code>\n\n"
        f"کاربر درخواست کد تأیید کرده است. پس از دریافت کد، روی دکمهٔ زیر بزنید.",
        admin_kb)


@smm_router.callback_query(F.data.startswith("vnum:sendcodeorder:"))
async def vnum_sendcode_order_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        o = await s.get(SmmOrder, order_id)
        if not o or o.bot_id != row.id or o.status != "completed" or not o.link:
            await cb.answer("سفارش پیدا نشد یا آمادهٔ ارسال کد نیست.", show_alert=True)
            return
        phone = o.link
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_code_reply)
    await state.update_data(vnum_code_order_id=order_id)
    await cb.message.answer(
        f"📤 کد تأیید شمارهٔ <code>{phone}</code> را بفرست تا برای خریدار ارسال شود:",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@smm_router.callback_query(F.data.startswith("vnum:sendcode:"))
async def vnum_sendcode_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    num_id = int(cb.data.split(":")[2])
    async with Session() as s:
        n = await s.get(VnumNumber, num_id)
        if not n or n.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
    from tenant import smart_cancel_kb
    await state.set_state(SmmFSM.vnum_code_reply)
    await state.update_data(vnum_code_num_id=num_id)
    await cb.message.answer(f"📩 کدِ تاییدِ شمارهٔ {n.phone_number} را بفرست تا مستقیم برایِ کاربر ارسال شود:",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@smm_router.message(SmmFSM.vnum_code_reply, F.text)
async def vnum_sendcode_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    num_id = data.get("vnum_code_num_id")
    order_id = data.get("vnum_code_order_id")
    async with Session() as s:
        if order_id:
            o = await s.get(SmmOrder, int(order_id))
            if not o or o.bot_id != row.id or o.status != "completed" or not o.link:
                await msg.answer("مشکلی پیش آمد؛ دوباره از پیامِ درخواست شروع کن.")
                return
            buyer_tg_id = o.user_telegram_id
            phone = o.link
        else:
            n = await s.get(VnumNumber, num_id) if num_id else None
            if not n or n.bot_id != row.id or n.sold_to_user_pk is None:
                await msg.answer("مشکلی پیش آمد؛ دوباره از پیامِ درخواست شروع کن.")
                return
            buyer = await s.get(BotUser, n.sold_to_user_pk)
            buyer_tg_id = buyer.telegram_id if buyer else None
            phone = n.phone_number
    if not buyer_tg_id:
        await msg.answer("کاربرِ خریدار پیدا نشد.")
        return
    code_text = msg.text.strip()
    try:
        await bot.send_message(
            buyer_tg_id,
            f"📩 کدِ تاییدِ شمارهٔ <code>{phone}</code>:\n\n<code>{code_text}</code>",
            parse_mode="HTML")
        await msg.answer("✅ کد برایِ کاربر ارسال شد.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگامِ ارسالِ کدِ SMS به مشتری")
        await msg.answer("❌ ارسال به کاربر ناموفق بود — احتمالاً کاربر ربات را بلاک کرده.")


@smm_router.callback_query(F.data.startswith("vnum:unavail:"))
async def vnum_unavail(cb: CallbackQuery):
    await cb.answer("❌ این کشور فعلاً موجود نیست — بعداً دوباره سر بزن.", show_alert=True)


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
