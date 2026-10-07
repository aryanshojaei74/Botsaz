"""
ماژول فروشگاه دیجیتال — برای ربات‌های با زمینه digital_shop

قابلیت‌ها:
  • نمایش محصولات با دسته‌بندی و عکس
  • خرید با کیف پول یا کارت‌به‌کارت
  • ارسال فایل دیجیتال بعد از پرداخت
  • مدیریت انبار (موجودی)
  • پنل ادمین مدیریت محصولات
"""

import html as _html
import json
import random
import string

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler  # PATCH_RECEIPT_STATE_CANCEL_V1
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, WebAppInfo, LabeledPrice, ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, BotUser, Product, ProductOrder, Setting, WalletTxn, order_no

from bot_utils import has_media  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

shop_router = Router()


# ─────────────────── FSM ───────────────────

class ShopFSM(StatesGroup):
    add_title = State()
    add_desc = State()
    add_price = State()
    add_photo = State()
    add_file = State()
    add_category = State()
    add_stock = State()

    edit_field = State()
    edit_value = State()
    shoplink_url = State()   # دریافت آدرس لینک فروشگاه (مرحلهٔ ۱ — قبل از انتخاب حالت بازشدن)
    discount_code = State()  # خرید محصول: انتظار برای کد تخفیف
    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    giftlink_url = State()   # ادمین: دریافت لینک گیفت پرمیوم (t.me/nft/...)
    # === PATCH_GIFT_TO_FRIEND_V1 ===
    gift_to_id = State()     # خریدار: دریافت آیدی عددیِ گیرندهٔ هدیه
    low_stock_threshold = State()  # ادمین: آستانهٔ هشدار اتمام موجودی
    gift_price = State()      # ادمین: قیمتِ فروش (تومان) برای گیفتِ تلگرامِ انتخاب‌شده


# === PATCH_FIX_SHOP_GIFT_PRICE_CRASH_V1 ===
def _shop_add_cancel_kb() -> InlineKeyboardMarkup:
    """کیبوردِ لغوِ ساده برای مراحلِ افزودنِ گیفت — قبلاً این تابع صدا زده
    می‌شد ولی هیچ‌جا تعریف نشده بود (NameError در لاگِ سرور)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ لغو", callback_data="sadm:add:cancel",
                             style=ButtonStyle.DANGER)]])


# === PATCH_PRODUCT_GIFT_LINK_V1 ===
import re as _re
_NFT_LINK_RE = _re.compile(r"https?://t\.me/nft/[\w-]+", _re.IGNORECASE)


# ─────────────────── کمک‌تابع‌ها ───────────────────

def rial(n) -> str:
    return f"{int(n):,} تومان"


def normalize_shop_link(raw: str) -> str | None:
    """آدرس لینک فروشگاه را اعتبارسنجی می‌کند؛ خروجی None یعنی نامعتبر."""
    s = (raw or "").strip()
    if not s:
        return None
    if not (s.startswith("http://") or s.startswith("https://")):
        s = "https://" + s
    # حداقل اعتبارسنجی: باید بعد از scheme یک دامنه داشته باشد
    rest = s.split("://", 1)[1] if "://" in s else ""
    if not rest or "." not in rest.split("/")[0]:
        return None
    return s


async def get_bot_row_by_token(token: str) -> BotRow | None:
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == token))


async def get_setting(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == key))
    return row.value if row else default


# ─────────────────── نمایش فروشگاه ───────────────────

def products_kb(products: list[Product], page: int = 0, page_size: int = 6) -> InlineKeyboardMarkup:
    rows = []
    start = page * page_size
    chunk = products[start:start + page_size]
    for p in chunk:
        stock_txt = "" if p.stock == -1 else f" ({p.stock}✦)"
        rows.append([InlineKeyboardButton(
            text=f"{'🔴' if p.stock == 0 else '🟢'} {p.title} — {rial(p.price)}{stock_txt}",
            callback_data=f"shop:view:{p.id}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"shop:page:{page-1}"))
    if start + page_size < len(products):
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"shop:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📋 سفارش‌های من", callback_data="shop:myorders:0")])
    # دکمه قرمز بازگشت — برای کاربرانی که از دکمه کمه (خارج از منوی اصلی) وارد شده‌اند
    rows.append([InlineKeyboardButton(text="🔴 بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_RECEIPT_STATE_CANCEL_V1 ===
# باگِ گزارش‌شده: کاربر وسطِ ارسالِ رسید دکمهٔ «⬅️ بازگشت به منو» را می‌زد،
# ولی چون آن متن عدد ندارد، ربات می‌گفت «متن رسید باید شامل عدد باشد» و
# کاربر در همان مرحله گیر می‌کرد. این تابع متن‌هایِ خروج را می‌شناسد.
def _is_exit_text(txt: str) -> bool:
    t = (txt or "").strip()
    return t in ("⬅️ بازگشت به منو", "بازگشت به منو", "❌ لغو",
                 "❌ لغو / بازگشت", "لغو", "بازگشت", "/start", "/cancel")


@shop_router.callback_query(F.data.startswith("shop:page:"))
async def shop_page(cb: CallbackQuery, bot: Bot):
    page = int(cb.data.split(":")[2])
    row = await get_bot_row_by_token(bot.token)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        products = (await s.scalars(
            select(Product).where(Product.bot_id == row.id, Product.is_active == True)
            .order_by(Product.sort_order, Product.id)
        )).all()
    if not products:
        await cb.answer("محصولی یافت نشد", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=products_kb(products, page))
    await cb.answer()


async def _render_product_page(pid: int, bot: Bot, state: FSMContext):
    """متن + کیبورد + عکسِ صفحهٔ یه محصول رو می‌سازه. اگه محصول پیدا نشد،
    None برمی‌گردونه.

    === PATCH_PRODUCT_DEEPLINK_V1 ===
    این تابع از دلِ shop_view_product بیرون کشیده شد تا هم دکمهٔ «مشاهدهٔ
    محصول» تو لیستِ فروشگاه، هم دیپ‌لینکِ prod_<id> (که از دکمهٔ آبیِ روی
    پستِ تبلیغِ کانال میاد) از یه منطقِ واحد استفاده کنن — به‌جای کپی‌پیستِ
    همون کد در دو جا.
    """
    async with Session() as s:
        p = await s.get(Product, pid)
    if not p:
        return None
    stock_txt = "✅ موجود" if p.stock != 0 else "❌ ناموجود"
    if p.stock > 0:
        stock_txt = f"✅ موجود ({p.stock} عدد)"
    elif p.stock == -1:
        stock_txt = "✅ موجود (نامحدود)"

    data = await state.get_data()
    disc = data.get("shop_discount") or None
    if disc and disc.get("pid") != pid:
        disc = None
    price_line = f"💰 قیمت: <b>{rial(p.price)}</b>"
    if disc:
        price_line = (
            f"💰 قیمت: <s>{rial(p.price)}</s> ➜ <b>{rial(disc['final_price'])}</b>\n"
            f"🏷 کد <code>{disc['code']}</code> اعمال شد (−{rial(disc['discount'])})"
        )
    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    gift_link = getattr(p, "gift_link_url", None)
    text = (
        (f"{gift_link}\n\n" if gift_link else "")
        + f"🛍 <b>{p.title}</b>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{p.description or ''}\n\n"
        f"{price_line}\n"
        f"📦 موجودی: {stock_txt}"
    )
    # === PATCH_PRODUCT_RATING_V1 ===
    # میانگینِ امتیازِ همین محصول (اگه نظرسنجی روشن باشه و امتیازی ثبت شده باشه)
    try:
        from tenant.shared.product_rating import rating_line, shop_key
        _row = await get_bot_row_by_token(bot.token)
        if _row:
            text += await rating_line(_row.id, shop_key(pid))
    except Exception:
        logger.exception("افزودنِ خطِ امتیازِ محصول ناموفق بود")
    # === PATCH_GIFT_TO_FRIEND_V1 ===
    gift_to = (data.get("gift_recipient") or {})
    gift_to_id = gift_to.get("tg_id") if gift_to.get("pid") == pid else None
    if p.is_gift and gift_to_id:
        text += f"\n🎁 گیرندهٔ هدیه: <code>{gift_to_id}</code>"
    kb_rows = []
    if p.stock != 0:
        row = await get_bot_row_by_token(bot.token)
        buy_row = [InlineKeyboardButton(text="💳 خرید با کیف پول", callback_data=f"shop:buy:wallet:{pid}")]
        card_purchase_enabled = (not row) or (
            await get_setting(row.id, "card_enabled", "1") == "1"
            and await get_setting(row.id, "card_purchase_enabled", "1") == "1"
        )
        if card_purchase_enabled:
            buy_row.append(InlineKeyboardButton(text="💵 کارت‌به‌کارت", callback_data=f"shop:buy:card:{pid}"))
        kb_rows.append(buy_row)
        if row and await get_setting(row.id, "stars_enabled", "0") == "1":
            fp = disc["final_price"] if disc else float(p.price)
            rate = await get_setting(row.id, "stars_toman_per_star", "1000")
            try:
                rate_v = float(rate) or 1000.0
            except Exception:
                rate_v = 1000.0
            stars_n = max(1, round(fp / rate_v))
            kb_rows.append([InlineKeyboardButton(
                text=f"⭐ خرید با استارز ({stars_n} ⭐)", callback_data=f"shop:buy:stars:{pid}")])
        discount_enabled = (not row) or await get_setting(row.id, "discount_purchase_enabled", "1") == "1"
        if disc:
            kb_rows.append([InlineKeyboardButton(text="🗑 حذف کد تخفیف", callback_data=f"shop:disc:del:{pid}")])
        elif discount_enabled:
            kb_rows.append([InlineKeyboardButton(text="🏷 اعمال کد تخفیف", callback_data=f"shop:disc:add:{pid}")])
        # === PATCH_GIFT_TO_FRIEND_V1 ===
        if p.is_gift:
            if gift_to_id:
                kb_rows.append([InlineKeyboardButton(
                    text="🗑 حذف گیرندهٔ هدیه", callback_data=f"shop:gifttodel:{pid}")])
            else:
                kb_rows.append([InlineKeyboardButton(
                    text="🎁 هدیه به دوستان", callback_data=f"shop:giftto:{pid}")])
    if p.shop_link_url:
        if p.shop_link_mode == "webapp":
            kb_rows.append([InlineKeyboardButton(
                text="🛍 ورود به فروشگاه", web_app=WebAppInfo(url=p.shop_link_url))])
        else:
            kb_rows.append([InlineKeyboardButton(
                text="🛍 ورود به فروشگاه", url=p.shop_link_url)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="shop:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows), p.photo_id, gift_link


async def send_product_page(target_msg: Message, bot: Bot, pid: int, state: FSMContext) -> bool:
    """صفحهٔ یه محصول رو به‌عنوانِ پیامِ تازه به target_msg.chat می‌فرسته.
    خروجی: True اگه محصول پیدا شد و فرستاده شد، False اگه محصول وجود
    نداشت (مثلاً حذف شده — از /start prod_<id> صدا زده می‌شه)."""
    rendered = await _render_product_page(pid, bot, state)
    if not rendered:
        return False
    text, kb, photo_id, gift_link = rendered
    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    # اگه لینک گیفت پرمیوم تنظیم شده، به‌جای عکس، پیش‌نمایشِ بزرگِ خودِ
    # لینک نشون داده می‌شه (تصویرِ متحرکِ گیفت — مثلِ StarzX).
    if gift_link:
        from aiogram.types import LinkPreviewOptions
        await target_msg.answer(
            text, reply_markup=kb, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=gift_link, prefer_large_media=True, show_above_text=False))
        return True
    if photo_id:
        await target_msg.answer_photo(photo_id, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        await target_msg.answer(text, reply_markup=kb, parse_mode="HTML")
    return True


@shop_router.callback_query(F.data.startswith("shop:view:"))
async def shop_view_product(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    ok = await send_product_page(cb.message, bot, pid, state)
    if not ok:
        await cb.answer("محصول یافت نشد", show_alert=True)
        return
    await cb.answer()


@shop_router.callback_query(F.data.startswith("shop:disc:add:"))
async def shop_discount_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[3])
    from tenant import smart_cancel_kb
    _row = await get_bot_row_by_token(bot.token)
    if _row and await get_setting(_row.id, "discount_purchase_enabled", "1") != "1":
        await cb.answer("❌ استفاده از کد تخفیف غیرفعال است.", show_alert=True)
        return
    await state.set_state(ShopFSM.discount_code)
    await state.update_data(shop_discount_pid=pid)
    await cb.message.answer("🏷 کد تخفیف را بفرست:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@shop_router.message(ShopFSM.discount_code, F.text)
async def shop_discount_save(msg: Message, state: FSMContext, bot: Bot):
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        from tenant import main_kb_or_remove
        row = await get_bot_row_by_token(bot.token)
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb)
        return
    data = await state.get_data()
    pid = data.get("shop_discount_pid")
    row = await get_bot_row_by_token(bot.token)
    async with Session() as s:
        p = await s.get(Product, pid) if pid else None
    if not row or not p:
        await state.clear()
        await msg.answer("محصول یافت نشد.")
        return
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(
        bot_id=row.id, code=raw, telegram_id=msg.from_user.id, price=int(p.price))
    if not res.ok:
        await msg.answer(f"❌ {res.reason}\n\nدوباره امتحان کن یا «❌ لغو» بزن:")
        return
    await state.update_data(shop_discount={
        "pid": pid, "code_id": res.code_id, "code": res.code,
        "discount": res.discount, "final_price": res.final_price,
    })
    await state.set_state(None)
    await msg.answer(
        f"✅ کد <code>{res.code}</code> اعمال شد! "
        f"قیمتِ جدید: <b>{rial(res.final_price)}</b> (−{rial(res.discount)})")
    fake_cb = _FakeCbForProduct(msg, pid)
    await shop_view_product(fake_cb, bot, state)


@shop_router.callback_query(F.data.startswith("shop:disc:del:"))
async def shop_discount_remove(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[3])
    data = await state.get_data()
    cur = data.get("shop_discount") or {}
    if cur.get("pid") == pid:
        await state.update_data(shop_discount=None)
    await cb.answer("حذف شد")
    await shop_view_product(cb, bot, state)


# ─────────────────── 🎁 هدیه به دوستان (خرید گیفت برای شخص دیگر) ───────────────────
# === PATCH_GIFT_TO_FRIEND_V1 ===

@shop_router.callback_query(F.data.startswith("shop:gifttodel:"))
async def shop_giftto_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    data = await state.get_data()
    cur = data.get("gift_recipient") or {}
    if cur.get("pid") == pid:
        await state.update_data(gift_recipient=None)
    await cb.answer("گیرندهٔ هدیه حذف شد — گیفت برای خودت ارسال می‌شه.")
    await shop_view_product(cb, bot, state)


@shop_router.callback_query(F.data.startswith("shop:giftto:"))
async def shop_giftto_start(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ShopFSM.gift_to_id)
    await state.update_data(giftto_pid=pid)
    await cb.message.answer(
        "🎁 <b>هدیه به دوستان</b>\n\n"
        "آیدی عددیِ تلگرامِ شخصی که می‌خوای گیفت براش خریداری و ارسال بشه رو بفرست:\n"
        "(مثال: <code>123456789</code>)\n\n"
        "⚠️ توجه: اون شخص باید حداقل یک‌بار همین ربات رو استارت زده باشه، "
        "وگرنه تلگرام اجازهٔ ارسال گیفت رو نمی‌ده.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"shop:view:{pid}")]]))
    await cb.answer()


@shop_router.message(ShopFSM.gift_to_id, F.text)
async def shop_giftto_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pid = data.get("giftto_pid")
    if not pid:
        await state.clear()
        return
    raw = (msg.text or "").strip().lstrip("@")
    try:
        tg_id = int(raw)
        if tg_id <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("⛔️ فقط آیدی عددی معتبره (مثال: 123456789). دوباره بفرست:")
        return
    await state.set_state(None)
    await state.update_data(gift_recipient={"pid": pid, "tg_id": tg_id}, giftto_pid=None)
    await msg.answer(
        f"✅ گیرندهٔ هدیه ثبت شد: <code>{tg_id}</code>\n"
        "حالا با هر روشِ پرداختی خرید کنی، گیفت مستقیم برای همین شخص ارسال می‌شه.",
        parse_mode="HTML")
    fake_cb = _FakeCbForProduct(msg, pid)
    await shop_view_product(fake_cb, bot, state)


class _FakeCbMessageForProduct:
    def __init__(self, real_msg: Message):
        self._real = real_msg

    async def answer(self, *a, **kw):
        return await self._real.answer(*a, **kw)

    async def answer_photo(self, *a, **kw):
        return await self._real.answer_photo(*a, **kw)


class _FakeCbForProduct:
    """شبیه‌سازِ سبکِ CallbackQuery برای استفادهٔ دوباره از shop_view_product
    بلافاصله بعد از ثبتِ موفقِ کدِ تخفیف (که از یک Message شروع شده، نه callback)."""
    def __init__(self, msg: Message, pid: int):
        self.from_user = msg.from_user
        self.message = _FakeCbMessageForProduct(msg)
        self.data = f"shop:view:{pid}"

    async def answer(self, *a, **kw):
        return None


async def show_shop(msg_or_cb, bot: Bot):
    """نقطه ورود مشترک — از دکمه اصلی یا callback باز می‌شه."""
    from aiogram.types import Message, CallbackQuery
    is_cb = isinstance(msg_or_cb, CallbackQuery)
    msg = msg_or_cb.message if is_cb else msg_or_cb
    token = bot.token
    from sqlalchemy import select
    from models import Bot as BotRow, Product
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == token))
        if not row:
            if is_cb:
                await msg_or_cb.answer()
            return
        products = (await s.scalars(
            select(Product).where(Product.bot_id == row.id, Product.is_active == True)
            .order_by(Product.sort_order, Product.id)
        )).all()
    if not products:
        if is_cb:
            try:
                await msg.edit_text("📭 هنوز محصولی در فروشگاه ثبت نشده.")
            except Exception:
                await msg.answer("📭 هنوز محصولی در فروشگاه ثبت نشده.")
            await msg_or_cb.answer()
        else:
            await msg.answer("📭 هنوز محصولی در فروشگاه ثبت نشده.")
        return
    text = "🛒 <b>فروشگاه</b>\nمحصول موردنظر را انتخاب کن:"
    kb = products_kb(list(products))
    # === PATCH_SHOP_EDIT_NOT_NEW_V1 ===
    # قبلاً این تابع (نقطهٔ ورودِ اصلیِ فروشگاه) همیشه پیامِ جدید می‌فرستاد،
    # حتی وقتی از یه دکمه صدا زده می‌شد — عکسِ همون باگی که تو خدماتِ مجازی
    # پیدا و رفع شد. اگه پیامِ فعلی عکس/ویدیو/فایل نداشته باشه (که edit_text
    # روش کار نمی‌کنه)، اول ادیت رو امتحان می‌کنیم.
    if is_cb and not (msg.photo or msg.video or msg.document):
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    if is_cb:
        await msg_or_cb.answer()


@shop_router.callback_query(F.data == "shop:list")
async def shop_list(cb: CallbackQuery, bot: Bot):
    row = await get_bot_row_by_token(bot.token)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        products = (await s.scalars(
            select(Product).where(Product.bot_id == row.id, Product.is_active == True)
            .order_by(Product.sort_order, Product.id)
        )).all()
    if not products:
        await cb.message.answer("📭 هنوز محصولی ثبت نشده.")
        await cb.answer()
        return
    # اگه پیام فعلی عکس داره، باید پیام جدید بفرستیم (edit_text روی عکس کار نمی‌کنه)
    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        await cb.message.answer(
            "🛒 <b>فروشگاه</b>\nمحصول موردنظر را انتخاب کن:",
            reply_markup=products_kb(products),
            parse_mode="HTML"
        )
    else:
        await cb.message.edit_text(
            "🛒 <b>فروشگاه</b>\nمحصول موردنظر را انتخاب کن:",
            reply_markup=products_kb(products),
            parse_mode="HTML"
        )
    await cb.answer()


_ORDER_STATUS_LABEL = {
    "paid": "✅ پرداخت‌شده",
    "awaiting_admin": "🟡 در انتظار تأییدِ ادمین",
    "rejected": "❌ ردشده",
}


@shop_router.callback_query(F.data.startswith("shop:myorders:"))
async def shop_my_orders(cb: CallbackQuery, bot: Bot):
    page = int(cb.data.split(":")[2])
    row = await get_bot_row_by_token(bot.token)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        orders = []
        if user:
            orders = (await s.scalars(
                select(ProductOrder).where(ProductOrder.user_id == user.id)
                .order_by(ProductOrder.id.desc())
            )).all()
        product_titles = {}
        if orders:
            pids = {o.product_id for o in orders}
            prods = (await s.scalars(select(Product).where(Product.id.in_(pids)))).all()
            product_titles = {p.id: p.title for p in prods}

    if not orders:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 مشاهدهٔ فروشگاه", callback_data="shop:list")],
            [InlineKeyboardButton(text="🔴 بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)],
        ])
        txt = "📋 <b>سفارش‌های من</b>\n\nهنوز هیچ سفارشی ثبت نکرده‌اید."
    else:
        from tenant import _jalali_date_time
        page_size = 8
        chunk = orders[page * page_size:(page + 1) * page_size]
        lines = ["📋 <b>سفارش‌های من</b>\n"]
        rows = []
        for o in chunk:
            title = product_titles.get(o.product_id, "(محصول حذف‌شده)")
            st = _ORDER_STATUS_LABEL.get(o.status, o.status)
            d, _tm = _jalali_date_time(o.created_at)
            rows.append([InlineKeyboardButton(
                text=f"#{o.tracking_code or o.id} — {title[:20]} — {st}",
                callback_data=f"shop:order:{o.id}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"shop:myorders:{page-1}"))
        if (page + 1) * page_size < len(orders):
            nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"shop:myorders:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🛒 فروشگاه", callback_data="shop:list")])
        rows.append([InlineKeyboardButton(text="🔴 بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        txt = "\n".join(lines) + "روی هر سفارش بزن تا جزئیاتش رو ببینی:"

    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    else:
        try:
            await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@shop_router.callback_query(F.data.startswith("shop:order:"))
async def shop_order_detail(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[2])
    row = await get_bot_row_by_token(bot.token)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        order = await s.get(ProductOrder, oid)
        if order is not None and await _is_foreign(order, bot):   # PATCH_TENANT_SCOPE_V1
            order = None
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        if not order or not user or order.user_id != user.id:
            await cb.answer("سفارش یافت نشد.", show_alert=True)
            return
        product = await s.get(Product, order.product_id)

    title = product.title if product else "(محصول حذف‌شده)"
    st = _ORDER_STATUS_LABEL.get(order.status, order.status)
    from tenant import _jalali_date_time
    d, tm = _jalali_date_time(order.created_at)
    lines = [
        f"📦 <b>سفارش #{order.tracking_code or order.id}</b>",
        f"🛍 محصول: <b>{title}</b>",
        f"💰 مبلغ: <b>{rial(order.amount)}</b>",
        f"💳 روش پرداخت: {'کیف پول' if order.pay_method == 'wallet' else 'کارت‌به‌کارت'}",
        f"📌 وضعیت: {st}",
        f"🗓 تاریخ: {d} {tm}",
    ]
    if order.tracking_code:
        lines.append(f"🔖 کد پیگیری: <code>{order.tracking_code}</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به سفارش‌ها", callback_data="shop:myorders:0")],
        [InlineKeyboardButton(text="🔴 بازگشت", callback_data="mbtn:home", style=ButtonStyle.DANGER)],
    ])
    text = "\n".join(lines)
    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ─────────────────── خرید با استارز تلگرام ───────────────────

@shop_router.callback_query(F.data.startswith("shop:buy:stars:"))
async def shop_buy_stars(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[3])
    row = await get_bot_row_by_token(bot.token)
    async with Session() as s:
        p = await s.get(Product, pid)
    if p is not None and row is not None and p.bot_id != row.id:   # PATCH_SHOP_PRODUCT_SCOPE_V1
        p = None
    if not row or not p:
        await cb.answer()
        return
    if await get_setting(row.id, "stars_enabled", "0") != "1":
        await cb.answer("⭐ پرداخت با استارز فعلاً فعال نیست.", show_alert=True)
        return
    if p.stock == 0:
        await cb.answer("❌ محصول ناموجود است.", show_alert=True)
        return
    data = await state.get_data()
    disc = data.get("shop_discount") or {}
    disc_for_pid = disc if disc.get("pid") == pid else {}
    final_price = disc_for_pid.get("final_price", float(p.price))
    code_id = disc_for_pid.get("code_id") or 0
    code = disc_for_pid.get("code") or ""

    rate = await get_setting(row.id, "stars_toman_per_star", "1000")
    try:
        rate_v = float(rate) or 1000.0
    except Exception:
        rate_v = 1000.0
    stars = max(1, round(final_price / rate_v))
    # === PATCH_GIFT_TO_FRIEND_V1 === گیرندهٔ هدیه (اگه انتخاب شده) داخلِ payload می‌ره
    _gr = data.get("gift_recipient") or {}
    _giftto = _gr.get("tg_id") if _gr.get("pid") == pid else 0
    try:
        await bot.send_invoice(
            chat_id=cb.message.chat.id,
            title=p.title[:32] or "خرید محصول",
            description=f"خرید «{p.title}» از فروشگاه" + (f" (کد {code})" if code_id else ""),
            payload=f"shop_stars2:{row.id}:{pid}:{int(final_price)}:{code_id}:{_giftto or 0}:{code}",
            provider_token="",  # پرداخت با استارز تلگرام، provider_token خالی است
            currency="XTR",
            prices=[LabeledPrice(label=p.title[:32] or "محصول", amount=stars)],
        )
    except Exception as e:
        from tenant import report_user_error, USER_FAIL_TEXT, get_bot_row as _gbr
        _rw = await _gbr(bot)
        if _rw:
            await report_user_error(bot, _rw.id, "فاکتورِ پرداختِ استارز", e, cb.from_user.id)
        await cb.message.answer("❌ ارسالِ فاکتور انجام نشد.\n" + USER_FAIL_TEXT)
    await cb.answer()


@shop_router.callback_query(F.data.startswith("shop:buy:wallet:"))
async def shop_buy_wallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[3])
    async with Session() as s:
        p = await s.get(Product, pid)
        bot_row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == cb.from_user.id))
    if p is not None and bot_row is not None and p.bot_id != bot_row.id:   # PATCH_SHOP_PRODUCT_SCOPE_V1
        p = None
    if not p or not bot_row or not user:
        await cb.answer()
        return
    if p.stock == 0:
        await cb.answer("❌ محصول ناموجود است.", show_alert=True)
        return

    data = await state.get_data()
    disc = data.get("shop_discount") or {}
    disc_code_id = disc.get("code_id") if disc.get("pid") == pid else None

    from tenant.gateways.discount_engine import validate_discount, record_usage
    # ── کسر موجودی، اعمال تخفیف (با اعتبارسنجیِ مجدد اتمیک) و ثبت سفارش در یک تراکنش ──
    delivered = False
    err = None
    final_price = float(p.price)
    order_id = None
    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        u = await s.get(BotUser, user.id, with_for_update=True)
        pr = await s.get(Product, pid, with_for_update=True)   # موجودیِ محدود: آخرین عدد دوبار فروخته نشود
        if pr is not None and await _is_foreign(pr, bot):   # PATCH_TENANT_SCOPE_V1
            pr = None
        if not u or not pr or not pr.is_active:
            err = "❌ محصول در دسترس نیست."
        elif pr.stock == 0:
            err = "❌ محصول ناموجود است."
        else:
            final_price = float(pr.price)
            applied_code_id = None
            if disc_code_id:
                res = await validate_discount(
                    bot_id=bot_row.id, code=disc["code"], telegram_id=cb.from_user.id,
                    price=int(pr.price), session=s)
                if res.ok:
                    final_price = float(res.final_price)
                    applied_code_id = res.code_id
            # === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
            # تخفیفِ خودکارِ اولین خرید — فقط وقتی کدی اعمال نشده، تا با
            # هم جمع نشوند. اینجا داخلِ همان تراکنشِ قفل‌دار حساب می‌شود،
            # پس اگر کاربر هم‌زمان خریدِ دیگری بزند دوبار تخفیف نمی‌گیرد.
            elif not disc_code_id:
                from tenant.gateways.discount_engine import first_purchase_discount
                _fd = await first_purchase_discount(
                    bot_id=bot_row.id, telegram_id=cb.from_user.id,
                    price=int(pr.price), session=s)
                if _fd.ok and _fd.discount > 0:
                    final_price = float(_fd.final_price)
            if float(u.balance) < final_price:
                err = f"❌ موجودی ناکافی! موجودی شما: {rial(u.balance)}"
            else:
                u.balance = float(u.balance) - final_price
                if pr.stock > 0:
                    pr.stock -= 1
                order = ProductOrder(
                    bot_id=bot_row.id, user_id=user.id, product_id=pid,
                    amount=final_price, pay_method="wallet", status="paid",
                    gift_recipient_tg_id=(data.get("gift_recipient") or {}).get("tg_id")
                        if (data.get("gift_recipient") or {}).get("pid") == pid else None,
                )
                s.add(order)
                s.add(WalletTxn(
                    bot_id=bot_row.id, user_id=user.id,
                    amount=-final_price, kind="purchase",
                    note=(f"خرید: {pr.title}"
                          + ("" if final_price >= float(pr.price)
                             else f" (تخفیف −{rial(float(pr.price) - final_price)})"))
                ))
                if applied_code_id:
                    await record_usage(s, code_id=applied_code_id,
                                       bot_id=bot_row.id, telegram_id=cb.from_user.id,
                                       saved_amount=res.discount)
                await s.commit()
                delivered = True
                order_id = order.id
    if err:
        await cb.answer(err, show_alert=True)
        return
    await state.update_data(shop_discount=None)
    from tenant import inline_back_kb
    await cb.message.answer(
        f"✅ <b>خرید موفق!</b>\n محصول: {p.title}\n💰 مبلغ پرداختی: {rial(final_price)}",
        reply_markup=await inline_back_kb(bot_row.id))
    # ارسال فایل دیجیتال / گیفتِ تلگرام
    if delivered and p.file_id:
        await bot.send_document(cb.from_user.id, p.file_id, caption=f"📦 {p.title}")
    if delivered and p.is_gift:
        # === PATCH_GIFT_MANUAL_CONFIRM_V1 ===
        # گیفت‌ها دیگه خودکار ارسال نمی‌شن؛ ادمین دستی می‌فرسته و روی دکمهٔ
        # «انجام شد» می‌زنه، بعدش پیام تاییدیه برای خریدار می‌ره.
        _gr = data.get("gift_recipient") or {}
        _recipient = _gr.get("tg_id") if _gr.get("pid") == pid else None
        await state.update_data(gift_recipient=None)
        await cb.message.answer(
            "🎁 سفارشِ گیفتِ شما ثبت شد؛ به‌محضِ ارسال توسط مدیر، بهت اطلاع می‌دیم.")
        await _notify_admin_gift_manual(bot, bot_row, order_id, p, _recipient or cb.from_user.id)
    await cb.answer("✅ خرید انجام شد!")
    if delivered:
        await _check_low_stock(bot, bot_row, pr)
        # اطلاع خودکار خرید به ادمین‌ها (در صورت روشن‌بودن)
        try:
            from tenant import _purchase_alert
            await _purchase_alert(
                bot, bot_row, buyer=user,
                section="فروشگاه دیجیتال",
                detail=f"محصول: {p.title}", amount=final_price, product_id=p.id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        # پاداش زیرمجموعه (این مسیر قبلاً این فراخوانی را نداشت — برخلاف مسیر کارت‌به‌کارت)
        from tenant.shared.extras import handle_referral_purchase
        await handle_referral_purchase(bot_row.id, cb.from_user.id, bot, amount=final_price)


# ─────────────────── خرید با کارت ───────────────────

class ShopCardFSM(StatesGroup):
    waiting_receipt = State()   # انتظار برای دریافت رسید (عکس یا متن)
    confirm_receipt = State()   # نمایش پیش‌نمایش رسید و تأیید کاربر


def _gen_tracking() -> str:
    """تولید کد پیگیری ۸ کاراکتری تصادفی."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


async def _shop_notify_admins(bot: Bot, bot_row: BotRow, text: str,
                               kb: InlineKeyboardMarkup,
                               file_id: str | None = None):
    """ارسال اطلاع‌رسانی سفارش به مالک."""
    try:
        if file_id:
            await bot.send_photo(bot_row.owner_id, file_id,
                                 caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(bot_row.owner_id, text,
                                   reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _check_low_stock(bot: Bot, bot_row: BotRow, product: Product):
    """اگر موجودیِ محصول به آستانهٔ هشدار رسید (یا کمتر شد)، به ادمین‌ها اطلاع می‌دهد.
    آستانه از تنظیماتِ فروشگاه خوانده می‌شود؛ آستانهٔ صفر یعنی این هشدار خاموش است.
    موجودیِ نامحدود (-1) هیچ‌وقت هشدار نمی‌دهد."""
    if product.stock < 0:
        return
    threshold = await get_setting(bot_row.id, "shop_low_stock_threshold", "2")
    try:
        threshold_n = int(threshold or "2")
    except Exception:
        threshold_n = 2
    if threshold_n <= 0 or product.stock > threshold_n:
        return
    from tenant import _notify_admins
    level = "❌ موجودی تمام شد!" if product.stock == 0 else "⚠️ موجودی رو به اتمام است"
    text = (
        f"{level}\n\n"
        f"🛍 محصول: <b>{product.title}</b>\n"
        f"📦 موجودی باقی‌مانده: <b>{product.stock}</b>\n\n"
        "برای افزایش موجودی، از «🛒 فروشگاه دیجیتال → مدیریت محصولات» ویرایشش کن."
    )
    await _notify_admins(bot, bot_row, text)


@shop_router.callback_query(F.data.startswith("shop:buy:card:"))
async def shop_buy_card(cb: CallbackQuery, bot: Bot, state: FSMContext):
    pid = int(cb.data.split(":")[3])
    async with Session() as s:
        p = await s.get(Product, pid)
        bot_row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    # PATCH_SHOP_PRODUCT_SCOPE_V1: محصولِ فروشگاهِ دیگری با pidِ دست‌ساز خریداری نشود
    if p is not None and bot_row is not None and p.bot_id != bot_row.id:
        p = None
    if not p or not bot_row:
        await cb.answer()
        return
    if p.stock == 0:
        await cb.answer("❌ محصول ناموجود است.", show_alert=True)
        return
    data = await state.get_data()
    disc = data.get("shop_discount") or {}
    final_price = disc["final_price"] if disc.get("pid") == pid else float(p.price)
    price_line = f"💰 مبلغ قابل پرداخت: <b>{rial(final_price)}</b>"
    if disc.get("pid") == pid:
        price_line = (f"💰 مبلغ قابل پرداخت: <s>{rial(p.price)}</s> ➜ <b>{rial(final_price)}</b> "
                      f"(کد <code>{disc['code']}</code>)")
    card_num = await get_setting(bot_row.id, "card_number", "—")
    card_holder = await get_setting(bot_row.id, "card_holder", "")
    holder_line = f"👤 به نام: <b>{card_holder}</b>\n" if card_holder else ""
    from tenant.gateways.card_display import extra_card_block   # PATCH_CARD_BOTH_V1
    holder_line += await extra_card_block(bot_row.id)
    await state.update_data(shop_product_id=pid)
    await state.set_state(ShopCardFSM.waiting_receipt)
    await cb.message.answer(
        f"🧾 <b>پیش فاکتور</b>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"🛍 محصول: <b>{p.title}</b>\n"
        f"{price_line}\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"💳 <b>اطلاعات واریز</b>\n\n"
        f"<code>{card_num}</code>\n"
        f"{holder_line}\n"
        f"💵 مبلغ: <code>{int(final_price):,} تومان</code>\n\n"
        f"⬆️ روی شماره کارت یا مبلغ بزن تا کپی شود.\n"
        f"📸 پس از واریز، <b>عکس رسید یا متن رسید</b> را اینجا ارسال کن.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="shop:card:cancel"),
        ]])
    )
    await cb.answer()


@shop_router.callback_query(F.data == "shop:card:cancel")
async def shop_card_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("لغو شد")


@shop_router.message(ShopCardFSM.waiting_receipt, F.photo)
async def shop_card_receipt_photo(msg: Message, state: FSMContext, bot: Bot):
    """دریافت عکس رسید."""
    file_id = msg.photo[-1].file_id
    await state.update_data(receipt_file_id=file_id, receipt_text=None)
    await state.set_state(ShopCardFSM.confirm_receipt)
    data = await state.get_data()
    pid = data.get("shop_product_id")
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ارسال", callback_data="shop:rc:ok")],
        [InlineKeyboardButton(text="✏️ ارسال رسید دیگر", callback_data="shop:rc:edit")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="shop:card:cancel")],
    ])
    caption = (
        f"📸 <b>پیش‌نمایش رسید</b>\n\n"
        f"🛍 محصول: <b>{p.title if p else '—'}</b>\n"
        f"💰 مبلغ: <b>{rial(p.price) if p else '—'}</b>\n\n"
        f"رسید درست است؟"
    )
    await msg.answer_photo(file_id, caption=caption, reply_markup=kb, parse_mode="HTML")


@shop_router.message(ShopCardFSM.waiting_receipt, F.text & ~F.text.startswith("/"))
async def shop_card_receipt_text(msg: Message, state: FSMContext, bot: Bot):
    """دریافت رسید متنی."""
    # === PATCH_RECEIPT_STATE_CANCEL_V1 === اول خروج، بعد اعتبارسنجی
    if _is_exit_text(msg.text):
        await state.clear()
        raise SkipHandler
    if not msg.text or not msg.text.strip() or not any(ch.isdigit() for ch in msg.text):
        await msg.answer("⚠️ متن رسید باید شامل عدد باشد (مثلاً مبلغ یا کد پیگیری). لطفاً رسید را دوباره ارسال کنید.")
        return
    await state.update_data(receipt_file_id=None, receipt_text=msg.text)
    await state.set_state(ShopCardFSM.confirm_receipt)
    data = await state.get_data()
    pid = data.get("shop_product_id")
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ارسال", callback_data="shop:rc:ok")],
        [InlineKeyboardButton(text="✏️ ارسال رسید دیگر", callback_data="shop:rc:edit")],
        [InlineKeyboardButton(text="❌ لغو", callback_data="shop:card:cancel")],
    ])
    # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
    await msg.answer(
        f"📋 <b>پیش‌نمایش رسید متنی</b>\n\n"
        f"🛍 محصول: <b>{p.title if p else '—'}</b>\n"
        f"💰 مبلغ: <b>{rial(p.price) if p else '—'}</b>\n\n"
        f"<blockquote>{_html.escape(msg.text[:500])}</blockquote>\n\n"
        f"رسید درست است؟",
        reply_markup=kb, parse_mode="HTML"
    )


@shop_router.message(ShopCardFSM.waiting_receipt)
async def shop_card_receipt_wrong(msg: Message):
    await msg.answer("لطفاً عکس رسید یا متن رسید را ارسال کنید.")


@shop_router.callback_query(ShopCardFSM.confirm_receipt, F.data == "shop:rc:edit")
async def shop_rc_edit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopCardFSM.waiting_receipt)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("📸 عکس رسید یا متن رسید جدید را ارسال کنید:")
    await cb.answer()


@shop_router.callback_query(ShopCardFSM.confirm_receipt, F.data == "shop:rc:ok")
async def shop_rc_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """کاربر رسید را تأیید کرد — ثبت سفارش و ارسال به ادمین."""
    data = await state.get_data()
    pid = data.get("shop_product_id")
    file_id = data.get("receipt_file_id")
    receipt_text = data.get("receipt_text")
    disc = data.get("shop_discount") or {}
    disc_for_pid = disc if disc.get("pid") == pid else {}

    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        bot_row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == cb.from_user.id))
        if not p or not bot_row or not user:
            await cb.answer("خطا", show_alert=True)
            await state.clear()
            return
        if p.stock == 0:
            await cb.answer("❌ محصول ناموجود شد!", show_alert=True)
            await state.clear()
            return
        final_price = float(p.price)
        applied_code_id = None
        if disc_for_pid.get("code_id"):
            from tenant.gateways.discount_engine import validate_discount, record_usage
            res = await validate_discount(
                bot_id=bot_row.id, code=disc_for_pid["code"], telegram_id=cb.from_user.id,
                price=int(p.price), session=s)
            if res.ok:
                final_price = float(res.final_price)
                applied_code_id = res.code_id
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        tracking = await _alloc_in(s, bot_row.id)
        order = ProductOrder(
            bot_id=bot_row.id, user_id=user.id, product_id=pid,
            amount=final_price, pay_method="card",
            status="awaiting_admin",
            receipt_file_id=file_id,
            receipt_text=receipt_text,
            tracking_code=tracking,
            discount_code_id=applied_code_id,
            gift_recipient_tg_id=(data.get("gift_recipient") or {}).get("tg_id")
                if (data.get("gift_recipient") or {}).get("pid") == pid else None,
        )
        s.add(order)
        if applied_code_id:
            await record_usage(s, code_id=applied_code_id, bot_id=bot_row.id,
                               telegram_id=cb.from_user.id, saved_amount=res.discount)
        await s.commit()
        order_id = order.id

    await state.update_data(shop_discount=None)
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # پیام تأیید به کاربر
    from tenant import inline_back_kb
    await cb.message.answer(
        f"✅ <b>رسید ثبت شد!</b>\n\n"
        f"🛍 محصول: <b>{p.title}</b>\n"
        f"💰 مبلغ: <b>{rial(final_price)}</b>\n"
        f"🔖 کد پیگیری: <code>{tracking}</code>\n\n"
        f"بعد از تأیید ادمین، محصول ارسال می‌شود.",
        parse_mode="HTML",
        reply_markup=await inline_back_kb(bot_row.id),
    )

    # اطلاع‌رسانی به ادمین
    user_name = cb.from_user.full_name or "—"
    user_uname = f"@{cb.from_user.username}" if cb.from_user.username else "—"
    adm_text = (
        f"🛒 <b>سفارش جدید #{await order_no(ProductOrder, order_id)}</b>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"👤 نام: {user_name}\n"
        f"📇 یوزرنیم: <code>{user_uname}</code>\n"
        f"📱 آیدی: <code>{cb.from_user.id}</code>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"🛍 محصول: <b>{p.title}</b>\n"
        f"💰 مبلغ: <b>{rial(final_price)}</b>"
        + (f" (کد {disc_for_pid.get('code')})" if applied_code_id else "") + "\n"
        f"🔖 کد پیگیری: <code>{tracking}</code>"
    )
    # === PATCH_GIFT_TO_FRIEND_V1 ===
    _gr = data.get("gift_recipient") or {}
    if p.is_gift and _gr.get("pid") == pid and _gr.get("tg_id"):
        adm_text += f"\n🎁 گیرندهٔ هدیه: <code>{_gr['tg_id']}</code>"
    if receipt_text and not file_id:
        # === PATCH_RECEIPT_TEXT_HTML_ESCAPE_V1 ===
        # receipt_text متنِ آزادِ کاربره؛ بدونِ escape، وجودِ < یا > توش کلِ
        # پیامِ HTML-mode رو خراب می‌کنه و اعلانِ رسید به ادمین بی‌صدا گم
        # می‌شه (دقیقاً همون الگویی که با پاسخِ خطای آروان دیده شد).
        adm_text += f"\n\n📋 رسید متنی:\n<blockquote>{_html.escape(receipt_text[:500])}</blockquote>"

    from tenant import _admin_referral_summary
    ref_summary = await _admin_referral_summary(bot_row.id, user.id)
    if ref_summary:
        adm_text += f"\n┈┅━━⊷⊰✨⊱⊶━━┅┈\n{ref_summary}"

    adm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تأیید و ارسال محصول",
                                 callback_data=f"shop:confirm:{order_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"shop:reject:{order_id}"),
        ]
    ])
    await _shop_notify_admins(bot, bot_row, adm_text, adm_kb, file_id)
    await cb.answer("ثبت شد ✅")


@shop_router.callback_query(F.data.startswith("shop:confirm:"))
async def shop_confirm_order(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        # FOR UPDATE + رباتِ جاری: دو ادمین هم‌زمان دوبار تحویل/کسرِ موجودی نکنند و
        # سفارشِ رباتِ دیگر تأیید نشود.
        order = await s.get(ProductOrder, order_id, with_for_update=True)
        if order is not None and await _is_foreign(order, bot):
            order = None
        if not order or order.status != "awaiting_admin":
            await cb.answer("سفارش قبلاً پردازش شده", show_alert=True)
            return
        p = await s.get(Product, order.product_id, with_for_update=True)
        user = await s.get(BotUser, order.user_id)
        bot_row = await s.get(BotRow, order.bot_id)
        if p and p.stock > 0:
            p.stock -= 1
        order.status = "paid"
        await s.commit()
        buyer_tg_id = user.telegram_id
        order_bot_id = order.bot_id
        tracking = order.tracking_code or "—"
        _gift_recipient = getattr(order, "gift_recipient_tg_id", None)  # PATCH_GIFT_TO_FRIEND_V1
        _alert_amount = float(order.amount)
        _alert_title = p.title if p else "—"

    # آپدیت پیام ادمین
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n✅ <b>تأیید شد توسط</b> @{cb.from_user.username or cb.from_user.full_name}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None, parse_mode="HTML")
        else:
            await cb.message.edit_text(done_text, reply_markup=None, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # پیام و فایل به خریدار
    await bot.send_message(
        buyer_tg_id,
        f"✅ <b>پرداخت تأیید شد!</b>\n\n"
        f"🛍 محصول: <b>{p.title}</b>\n"
        f"🔖 کد پیگیری: <code>{tracking}</code>",
        parse_mode="HTML"
    )
    if p and p.file_id:
        await bot.send_document(buyer_tg_id, p.file_id,
                                caption=f"📦 <b>{p.title}</b>\n🔖 کد پیگیری: <code>{tracking}</code>",
                                parse_mode="HTML")
    if p and p.is_gift and bot_row:
        # === PATCH_GIFT_MANUAL_CONFIRM_V1 ===
        try:
            await bot.send_message(
                buyer_tg_id,
                "🎁 سفارشِ گیفتِ شما ثبت شد؛ به‌محضِ ارسال توسط مدیر، بهت اطلاع می‌دیم.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ ثبتِ سفارشِ گیفت)")
        await _notify_admin_gift_manual(bot, bot_row, order_id, p, _gift_recipient or buyer_tg_id)
    # پاداش زیرمجموعه
    from tenant.shared.extras import handle_referral_purchase
    await handle_referral_purchase(order_bot_id, buyer_tg_id, bot)
    if p and bot_row:
        await _check_low_stock(bot, bot_row, p)
    # اطلاع خودکار خرید به ادمین‌ها (در صورت روشن‌بودن)
    if bot_row:
        try:
            from tenant import _purchase_alert
            await _purchase_alert(
                bot, bot_row, buyer=user,
                section="فروشگاه دیجیتال (کارت‌به‌کارت)",
                detail=f"محصول: {_alert_title}", amount=_alert_amount,
                product_id=(p.id if p else None), tracking_code=tracking)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("تأیید شد ✅")


@shop_router.callback_query(F.data.startswith("shop:reject:"))
async def shop_reject_order(cb: CallbackQuery, bot: Bot, state: FSMContext):
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        order = await s.get(ProductOrder, order_id, with_for_update=True)
        if order is not None and await _is_foreign(order, bot):   # PATCH_TENANT_SCOPE_V1
            order = None
        if not order or order.status != "awaiting_admin":
            await cb.answer("سفارش قبلاً پردازش شده", show_alert=True)
            return
        user = await s.get(BotUser, order.user_id)
        order.status = "rejected"
        if order.discount_code_id:
            from tenant.gateways.discount_engine import revert_usage
            await revert_usage(s, code_id=order.discount_code_id, telegram_id=user.telegram_id)
        await s.commit()
        buyer_tg_id = user.telegram_id
        tracking = order.tracking_code or "—"

    # درخواست دلیل رد از ادمین
    await cb.answer()
    await state.update_data(shop_reject_oid=order_id, shop_reject_buyer=buyer_tg_id,
                            shop_reject_tracking=tracking)
    await state.set_state(ShopCardFSM.waiting_receipt)  # reuse state for text input
    await state.update_data(shop_reject_mode=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔️ رد بدون دلیل", callback_data=f"shop:reject_nodeny:{order_id}")],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"shop:reject_cancel:{order_id}")],
    ])
    await cb.message.answer(
        f"📝 <b>دلیل رد سفارش #{await order_no(ProductOrder, order_id)} را بنویسید</b> تا به خریدار ارسال شود:\n"
        f"(یا دکمه رد بدون دلیل را بزنید)",
        reply_markup=kb, parse_mode="HTML"
    )


@shop_router.callback_query(F.data.startswith("shop:reject_nodeny:"))
async def shop_reject_nodeny(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    order_id = int(cb.data.split(":")[2])
    async with Session() as s:
        order = await s.get(ProductOrder, order_id)
        if order is not None and await _is_foreign(order, bot):   # PATCH_TENANT_SCOPE_V1
            order = None
        if not order:
            await cb.answer()
            return
        user = await s.get(BotUser, order.user_id)
        tracking = order.tracking_code or "—"
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n❌ <b>رد شد توسط</b> @{cb.from_user.username or cb.from_user.full_name}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None, parse_mode="HTML")
        else:
            await cb.message.edit_text(done_text, reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await bot.send_message(
        user.telegram_id,
        f"❌ سفارش شما (کد پیگیری: <code>{tracking}</code>) تأیید نشد.\n"
        f"برای پیگیری با پشتیبانی تماس بگیرید.",
        parse_mode="HTML"
    )
    await cb.answer("رد شد ❌")


@shop_router.callback_query(F.data.startswith("shop:reject_cancel:"))
async def shop_reject_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("لغو شد")


# ─────────────────── پنل ادمین مدیریت محصولات ───────────────────

def shop_admin_kb(bot_field: str = "digital_shop") -> InlineKeyboardMarkup:
    """=== PATCH_ADBOT_HIDE_GIFTS_V1 ===
    گزینهٔ «گیفت‌های کلکسیونی/استارزیِ تلگرام» فقط برای فروشگاهِ دیجیتالِ
    واقعی معنی داره؛ برای بات‌های تبلیغاتی (ad_bot) که فقط محصولِ کنارِ
    تبلیغ می‌فروشن، ربطی نداره — طبقِ درخواستِ صریح حذف شد."""
    rows = [
        [InlineKeyboardButton(text="📋 لیست محصولات", callback_data="sadm:list", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="➕ محصول جدید", callback_data="sadm:add", style=ButtonStyle.SUCCESS)],
    ]
    if bot_field != "ad_bot":
        rows.append([InlineKeyboardButton(text="🎁 گیفت‌های کلکسیونی/استارزیِ تلگرام",
                                          callback_data="sadm:gifts:0", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="📊 آمار فروش", callback_data="sadm:stats", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⚠️ آستانهٔ هشدار اتمام موجودی", callback_data="sadm:lowstock", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER),
                 InlineKeyboardButton(text="🏠 پنل مدیریت", callback_data="tadm:menu", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@shop_router.callback_query(F.data == "tadm:shop")
async def sadm_menu(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    await cb.message.edit_text("🛒 <b>مدیریت فروشگاه</b>", reply_markup=shop_admin_kb(bot_row.field))
    await cb.answer()


@shop_router.callback_query(F.data == "sadm:list")
async def sadm_list(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    async with Session() as s:
        products = (await s.scalars(
            select(Product).where(Product.bot_id == bot_row.id)
            .order_by(Product.sort_order, Product.id)
        )).all()
    if not products:
        await cb.message.edit_text(
            "📭 هیچ محصولی ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ محصول جدید", callback_data="sadm:add", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)],
            ])
        )
        return
    rows = []
    for p in products:
        status = "🟢" if p.is_active and p.stock != 0 else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{status} {p.title} — {rial(p.price)}",
            callback_data=f"sadm:edit:{p.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"📋 <b>محصولات ({len(products)})</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:edit:"))
async def sadm_edit_product(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
    if not p:
        await cb.answer()
        return
    stock_txt = "نامحدود" if p.stock == -1 else str(p.stock)
    if p.shop_link_url:
        _mode_lbl = "📱 مینی‌اپ" if p.shop_link_mode == "webapp" else "🌐 مرورگر"
        link_txt = f"دارد ({_mode_lbl})"
    else:
        link_txt = "—"
    text = (
        f"✏️ <b>{p.title}</b>\n"
        f"قیمت: {rial(p.price)}\n"
        f"دسته: {p.category or '—'}\n"
        f"موجودی: {stock_txt}\n"
        f"وضعیت: {'فعال' if p.is_active else 'غیرفعال'}\n"
        f"لینک فروشگاه: {link_txt}\n"
        f"🎁 لینک گیفت پرمیوم: {'دارد ✅' if getattr(p, 'gift_link_url', None) else '—'}\n"
        f"📌 ارسال موقع /start: {'روشن ✅' if getattr(p, 'pin_on_start', False) else 'خاموش'}"
    )
    link_row = (
        [
            InlineKeyboardButton(text="🔗 تغییر لینک فروشگاه", callback_data=f"sadm:linkset:{pid}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🗑 حذف لینک", callback_data=f"sadm:linkdel:{pid}", style=ButtonStyle.DANGER),
        ] if p.shop_link_url else
        [InlineKeyboardButton(text="🔗 افزودن لینک فروشگاه", callback_data=f"sadm:linkset:{pid}", style=ButtonStyle.SUCCESS)]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ عنوان", callback_data=f"sadm:ef:{pid}:title", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="💰 قیمت", callback_data=f"sadm:ef:{pid}:price", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="📝 توضیحات", callback_data=f"sadm:ef:{pid}:desc", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="📦 موجودی", callback_data=f"sadm:ef:{pid}:stock", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="🖼 عکس", callback_data=f"sadm:ef:{pid}:photo", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="📁 فایل", callback_data=f"sadm:ef:{pid}:file", style=ButtonStyle.PRIMARY),
        ],
        link_row,
        # === PATCH_PRODUCT_GIFT_LINK_V1 ===
        (
            [
                InlineKeyboardButton(text="🎁 تغییر لینک گیفت", callback_data=f"sadm:giftset:{pid}", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton(text="🗑 حذف لینک گیفت", callback_data=f"sadm:giftdel:{pid}", style=ButtonStyle.DANGER),
            ] if getattr(p, "gift_link_url", None) else
            [InlineKeyboardButton(text="🎁 افزودن لینک گیفت پرمیوم", callback_data=f"sadm:giftset:{pid}", style=ButtonStyle.SUCCESS)]
        ),
        # === PATCH_PRODUCT_PIN_ON_START_V1 ===
        # روشن باشه → این محصول بعد از پیامِ خوش‌آمدِ /start خودکار برای کاربر ارسال می‌شه.
        [InlineKeyboardButton(
            text=("📌 ارسال موقع /start: روشن ✅" if getattr(p, "pin_on_start", False)
                  else "📌 چسباندن به /start"),
            callback_data=f"sadm:pinstart:{pid}")],
        [
            InlineKeyboardButton(
                text="🔴 غیرفعال" if p.is_active else "🟢 فعال",
                callback_data=f"sadm:toggle:{pid}", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"sadm:del:{pid}", style=ButtonStyle.DANGER),
        ],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="sadm:list", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:toggle:"))
async def sadm_toggle(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            p.is_active = not p.is_active
            await s.commit()
    await sadm_edit_product(cb, bot)


# === PATCH_PRODUCT_PIN_ON_START_V1 ===
@shop_router.callback_query(F.data.startswith("sadm:pinstart:"))
async def sadm_pinstart_toggle(cb: CallbackQuery, bot: Bot):
    """روشن/خاموش‌کردنِ ارسالِ خودکارِ این محصول موقعِ /start کاربر.
    خاموش (پیش‌فرض) → /start فقط پیامِ خوش‌آمد و دکمه‌ها رو میاره."""
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            p.pin_on_start = not bool(getattr(p, "pin_on_start", False))
            new_state = p.pin_on_start
            await s.commit()
        else:
            await cb.answer("محصول یافت نشد", show_alert=True)
            return
    await cb.answer("📌 این محصول الان موقعِ /start خودکار ارسال می‌شه"
                    if new_state else "📌 ارسال موقعِ /start خاموش شد")
    await sadm_edit_product(cb, bot)


@shop_router.callback_query(F.data.startswith("sadm:del:"))
async def sadm_delete(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            await s.delete(p)
            await s.commit()
    await cb.answer("🗑 محصول حذف شد", show_alert=True)


@shop_router.callback_query(F.data.startswith("sadm:ef:"))
async def sadm_edit_field_start(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    pid, field = int(parts[2]), parts[3]
    prompts = {
        "title": "عنوان جدید محصول را بفرست:",
        "price": "قیمت جدید (به تومان) را بفرست:",
        "desc": "توضیحات جدید را بفرست:",
        "stock": "موجودی جدید را بفرست (-1 = نامحدود):",
        "photo": "عکس جدید محصول را بفرست:",
        "file": "فایل دیجیتال محصول را بفرست:",
    }
    await state.set_state(ShopFSM.edit_value)
    await state.set_data({"edit_pid": pid, "edit_field": field})
    # === PATCH_SADM_EDIT_UX_V1 ===
    # قبلاً هیچ دکمه‌ای نبود — ادمین تو حالتِ ویرایش گیر می‌کرد. الان دکمهٔ
    # بازگشت، و برای فیلدهای اختیاری (توضیحات/عکس/فایل) دکمهٔ حذفِ
    # مقدارِ فعلی هم هست.
    kb_rows = []
    if field in ("desc", "photo", "file", "category"):
        kb_rows.append([InlineKeyboardButton(
            text="🗑 حذف مقدار فعلی", callback_data=f"sadm:efclear:{pid}:{field}", style=ButtonStyle.DANGER)])
    kb_rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data=f"sadm:efback:{pid}", style=ButtonStyle.DANGER)])
    await cb.message.answer(prompts.get(field, "مقدار جدید:"),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


# === PATCH_SADM_EDIT_UX_V1 ===
@shop_router.callback_query(F.data.startswith("sadm:efback:"))
async def sadm_ef_back(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """بازگشت از حالتِ ویرایشِ فیلد به منوی محصول (و پاک‌کردنِ FSM)."""
    await state.clear()
    await sadm_edit_product(cb, bot)


@shop_router.callback_query(F.data.startswith("sadm:efclear:"))
async def sadm_ef_clear(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """حذفِ مقدارِ فعلیِ یک فیلدِ اختیاریِ محصول (توضیحات/عکس/فایل)."""
    parts = cb.data.split(":")
    pid, field = int(parts[2]), parts[3]
    col = {"desc": "description", "photo": "photo_id",
           "file": "file_id", "category": "category"}.get(field)
    if not col:
        await cb.answer()
        return
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            setattr(p, col, None)
            await s.commit()
    await state.clear()
    await cb.answer("🗑 حذف شد")
    await sadm_edit_product(cb, bot)


@shop_router.message(ShopFSM.edit_value)
async def sadm_edit_field_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid, field = data["edit_pid"], data["edit_field"]
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, msg.bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if not p:
            await state.clear()
            return
        if field == "title":
            if not msg.text:
                await msg.answer("⚠️ لطفاً متن بفرست.")
                return
            p.title = msg.text.strip()
        elif field == "price":
            if not msg.text:
                await msg.answer("⚠️ لطفاً عدد بفرست.")
                return
            try:
                p.price = float(msg.text.replace(",", "").replace("،", "").strip())
            except ValueError:
                await msg.answer("عدد نامعتبر.")
                return
        elif field == "desc":
            if not msg.text:
                await msg.answer("⚠️ لطفاً متن بفرست.")
                return
            p.description = msg.html_text
        elif field == "stock":
            if not msg.text:
                await msg.answer("⚠️ لطفاً عدد بفرست.")
                return
            try:
                p.stock = int(msg.text.strip())
            except ValueError:
                await msg.answer("عدد نامعتبر.")
                return
        elif field == "photo":
            if not msg.photo:
                await msg.answer("⚠️ لطفاً یک عکس بفرست. (نه فایل، نه متن)")
                return
            p.photo_id = msg.photo[-1].file_id
        elif field == "file":
            if not msg.document:
                await msg.answer("⚠️ لطفاً فایل را به صورت Document بفرست.")
                return
            p.file_id = msg.document.file_id
        await s.commit()
    await state.clear()
    await msg.answer("✅ ذخیره شد.")


# ─────────────────── لینک فروشگاه (دکمهٔ شیشه‌ای زیر پست محصول) ───────────────────

# === PATCH_PRODUCT_GIFT_LINK_V1 ===
@shop_router.callback_query(F.data.startswith("sadm:giftset:"))
async def sadm_giftset_start(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ShopFSM.giftlink_url)
    await state.update_data(giftlink_pid=pid)
    await cb.message.answer(
        "🎁 <b>لینک گیفت پرمیوم</b>\n\n"
        "لینکِ گیفتِ تلگرامت رو بفرست:\n"
        "(مثال: <code>https://t.me/nft/StellarRocket-92250</code>)\n\n"
        "صفحهٔ محصول با تصویرِ متحرکِ همین گیفت نمایش داده می‌شه.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"sadm:efback:{pid}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@shop_router.message(ShopFSM.giftlink_url, F.text)
async def sadm_giftset_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pid = data.get("giftlink_pid")
    if not pid:
        await state.clear()
        return
    m = _NFT_LINK_RE.search(msg.text or "")
    if not m:
        await msg.answer(
            "⛔️ لینک نامعتبره. باید به شکلِ <code>https://t.me/nft/…</code> باشه. دوباره بفرست:",
            parse_mode="HTML")
        return
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if not p:
            await state.clear()
            await msg.answer("محصول یافت نشد.")
            return
        p.gift_link_url = m.group(0)
        await s.commit()
    await state.clear()
    await msg.answer("✅ لینک گیفت ثبت شد — صفحهٔ محصول الان با تصویرِ متحرکِ گیفت نمایش داده می‌شه.")


@shop_router.callback_query(F.data.startswith("sadm:giftdel:"))
async def sadm_giftdel(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            p.gift_link_url = None
            await s.commit()
    await cb.answer("🗑 لینک گیفت حذف شد")
    await sadm_edit_product(cb, bot)


@shop_router.callback_query(F.data.startswith("sadm:linkset:"))
async def sadm_linkset_start(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.set_state(ShopFSM.shoplink_url)
    await state.update_data(link_pid=pid)
    await cb.message.answer(
        "🔗 <b>لینک فروشگاه</b>\n\n"
        "آدرس سایت/فروشگاهی که با زدنِ دکمهٔ شیشه‌ای باز شود را بفرست:\n"
        "(مثال: <code>https://example.com</code>)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"sadm:efback:{pid}", style=ButtonStyle.DANGER)]]),
    )
    await cb.answer()


@shop_router.message(ShopFSM.shoplink_url, F.text)
async def sadm_linkset_url(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("link_pid")
    url = normalize_shop_link(msg.text)
    if not pid:
        await state.clear()
        return
    if not url:
        await msg.answer("⛔️ آدرس نامعتبر بود. یک لینک درست بفرست (مثلاً https://example.com):")
        return
    await state.update_data(pending_link_url=url)
    await msg.answer(
        "🖼 این لینک به چه‌صورت برای کاربر باز شود؟\n\n"
        "📱 <b>مینی‌اپ تلگرام</b>: سایت داخل خود تلگرام (به‌صورت اپلیکیشن) باز می‌شود — نیاز به آدرس https دارد.\n"
        "🌐 <b>مرورگر گوشی</b>: سایت در مرورگر پیش‌فرض گوشی کاربر باز می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 مینی‌اپ تلگرام", callback_data=f"sadm:linkmode:{pid}:webapp", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="🌐 مرورگر گوشی", callback_data=f"sadm:linkmode:{pid}:browser", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"sadm:linkcancel:{pid}", style=ButtonStyle.DANGER)],
        ]),
    )


@shop_router.callback_query(F.data.startswith("sadm:linkmode:"))
async def sadm_linkset_mode(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    pid, mode = int(parts[2]), parts[3]
    data = await state.get_data()
    url = data.get("pending_link_url")
    if not url or data.get("link_pid") != pid:
        await cb.answer("⛔️ نشست منقضی شد؛ دوباره از «🔗 افزودن لینک فروشگاه» شروع کن.", show_alert=True)
        await state.clear()
        return
    if mode == "webapp" and not url.startswith("https://"):
        await cb.answer("⛔️ برای حالت مینی‌اپ، آدرس باید با https شروع شود.", show_alert=True)
        return
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, cb.bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if not p:
            await cb.answer("محصول یافت نشد.", show_alert=True)
            await state.clear()
            return
        p.shop_link_url = url
        p.shop_link_mode = mode
        await s.commit()
    await state.clear()
    mode_lbl = "📱 مینی‌اپ تلگرام" if mode == "webapp" else "🌐 مرورگر گوشی"
    await cb.message.edit_text(f"✅ لینک فروشگاه ثبت شد. (حالت: {mode_lbl})")
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:linkcancel:"))
async def sadm_linkset_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("لغو شد.")
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:linkdel:"))
async def sadm_linkdel(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
        if p:
            p.shop_link_url = None
            p.shop_link_mode = None
            await s.commit()
    await sadm_edit_product(cb, bot)


# ═══════════════ PATCH_SHOP_ADD_WIZARD_REDESIGN_V1 ═══════════════
# بازطراحیِ کاملِ ویزاردِ «افزودن محصول جدید»:
#   • حالتِ حذف‌و‌ارسال: به‌جای انباشته‌شدنِ پیام‌ها، هر پیامِ کاربر و هر
#     پرامپتِ قبلی حذف می‌شه و فقط یه پیامِ زنده رو صفحه می‌مونه.
#   • دکمهٔ «◀️ مرحلهٔ قبل» — می‌شه هر مرحله رو برگشت و دوباره درست کرد.
#   • دکمه‌های رنگی: رد‌کردن=سبز، برگشت=آبی، لغو=قرمز.
#   • شمارندهٔ مرحله («مرحلهٔ ۳ از ۶») بالای هر پرامپت.
#   • در پایان، دکمهٔ «📦 رفتن به انبار محصولات» + «➕ افزودنِ محصولِ دیگر».

_ADD_STEPS = ["title", "desc", "price", "stock", "photo", "file"]
_ADD_STEP_SKIPPABLE = {"desc", "photo", "file"}
_ADD_STEP_PROMPTS = {
    "title": "📝 عنوانِ محصول را بفرست:",
    "desc":  "📄 توضیحاتِ محصول را بنویس (یا «رد کردن» را بزن):",
    "price": "💰 قیمتِ محصول را به تومان وارد کن (فقط عدد):",
    "stock": "📦 موجودیِ اولیه را وارد کن (برای نامحدود، عددِ <code>-1</code> بفرست):",
    "photo": "🖼 عکسِ محصول را بفرست (یا «رد کردن» را بزن):",
    "file":  "📎 فایلِ دیجیتالِ محصول را بفرست (یا «رد کردن» را بزن):",
}


def _add_step_state(step: str) -> State:
    return {
        "title": ShopFSM.add_title, "desc": ShopFSM.add_desc, "price": ShopFSM.add_price,
        "stock": ShopFSM.add_stock, "photo": ShopFSM.add_photo, "file": ShopFSM.add_file,
    }[step]


def _add_step_kb(step_idx: int) -> InlineKeyboardMarkup:
    step = _ADD_STEPS[step_idx]
    rows = []
    if step in _ADD_STEP_SKIPPABLE:
        rows.append([InlineKeyboardButton(text="⏭ رد کردن", callback_data="shopadd:skip",
                                          style=ButtonStyle.SUCCESS)])
    nav = []
    if step_idx > 0:
        nav.append(InlineKeyboardButton(text="◀️ مرحلهٔ قبل", callback_data="shopadd:back",
                                        style=ButtonStyle.PRIMARY))
    nav.append(InlineKeyboardButton(text="❌ لغو", callback_data="sadm:add:cancel",
                                    style=ButtonStyle.DANGER))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _add_step_text(step_idx: int, data: dict) -> str:
    total = len(_ADD_STEPS)
    step = _ADD_STEPS[step_idx]
    summary = ""
    if data.get("title"):
        summary += f"📝 عنوان: <b>{_html.escape(data['title'])}</b>\n"
    if step_idx >= 2 and data.get("price") is not None:
        summary += f"💰 قیمت: <b>{rial(data['price'])}</b>\n"
    if step_idx >= 3 and data.get("stock") is not None:
        stock_lbl = "نامحدود" if data["stock"] == -1 else str(data["stock"])
        summary += f"📦 موجودی: <b>{stock_lbl}</b>\n"
    header = f"📦 <b>افزودنِ محصولِ جدید</b> — مرحلهٔ {step_idx + 1} از {total}"
    if summary:
        header += f"\n┈┅━━⊷⊰✨⊱⊶━━┅┈\n{summary}┈┅━━⊷⊰✨⊱⊶━━┅┈"
    return f"{header}\n\n{_ADD_STEP_PROMPTS[step]}"


async def _shop_add_goto(target, state: FSMContext, step_idx: int, bot: Bot | None = None):
    """پرامپتِ مرحلهٔ step_idx رو نشون می‌ده؛ اگه پرامپتِ قبلی هنوز رو صفحه
    بود، اول حذفش می‌کنه (حالتِ حذف‌و‌ارسال)."""
    data = await state.get_data()
    is_msg = not hasattr(target, "message")  # CallbackQuery has .message; Message doesn't
    chat_id = target.chat.id if is_msg else target.message.chat.id
    _bot = bot or (target.bot if hasattr(target, "bot") else None)
    prev_id = data.get("_add_prompt_msg_id")
    if prev_id and _bot:
        try:
            await _bot.delete_message(chat_id, prev_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    step = _ADD_STEPS[step_idx]
    await state.set_state(_add_step_state(step))
    await state.update_data(_add_step_idx=step_idx)
    text = _add_step_text(step_idx, data)
    kb = _add_step_kb(step_idx)
    sent = await (target.answer(text, reply_markup=kb, parse_mode="HTML") if is_msg
                 else target.message.answer(text, reply_markup=kb, parse_mode="HTML"))
    await state.update_data(_add_prompt_msg_id=sent.message_id)


@shop_router.callback_query(F.data == "sadm:add:cancel")
async def sadm_add_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    prev_id = data.get("_add_prompt_msg_id")
    await state.clear()
    if prev_id:
        try:
            await bot.delete_message(cb.message.chat.id, prev_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await sadm_menu(cb, bot)


@shop_router.callback_query(F.data == "sadm:add")
async def sadm_add_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await state.update_data(_add_prompt_msg_id=cb.message.message_id)
    await _shop_add_goto(cb, state, 0, bot)
    await cb.answer()


@shop_router.callback_query(F.data == "shopadd:back")
async def shopadd_back(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    idx = max(0, data.get("_add_step_idx", 0) - 1)
    await _shop_add_goto(cb, state, idx, bot)
    await cb.answer()


@shop_router.callback_query(F.data == "shopadd:skip")
async def shopadd_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    idx = data.get("_add_step_idx", 0)
    step = _ADD_STEPS[idx]
    if step not in _ADD_STEP_SKIPPABLE:
        await cb.answer()
        return
    await state.update_data(**{"description" if step == "desc" else
                               ("photo_id" if step == "photo" else "file_id"): None})
    await cb.answer()
    await _shop_add_advance(cb, state, bot)


async def _shop_add_advance(target, state: FSMContext, bot: Bot):
    """می‌ره سراغِ مرحلهٔ بعدی، یا اگه آخرین مرحله بود، محصول رو ذخیره می‌کنه."""
    data = await state.get_data()
    idx = data.get("_add_step_idx", 0)
    if idx + 1 < len(_ADD_STEPS):
        await _shop_add_goto(target, state, idx + 1, bot)
        return
    # === مرحلهٔ آخر بود؛ ذخیرهٔ نهایی ===
    is_msg = not hasattr(target, "message")  # CallbackQuery has .message; Message doesn't
    chat_id = target.chat.id if is_msg else target.message.chat.id
    prev_id = data.get("_add_prompt_msg_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        p = Product(
            bot_id=bot_row.id,
            title=data["title"],
            description=data.get("description", "") or "",
            price=data["price"],
            stock=data.get("stock", -1),
            photo_id=data.get("photo_id"),
            file_id=data.get("file_id"),
        )
        s.add(p)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 رفتن به انبار محصولات", callback_data="sadm:list",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="➕ افزودنِ محصولِ دیگر", callback_data="sadm:add",
                              style=ButtonStyle.SUCCESS)],
    ])
    text = (
        f"✅ <b>محصول با موفقیت اضافه شد!</b>\n\n"
        f"📝 عنوان: <b>{_html.escape(data['title'])}</b>\n"
        f"💰 قیمت: <b>{rial(data['price'])}</b>\n"
        f"📦 موجودی: <b>{'نامحدود' if data.get('stock', -1) == -1 else data.get('stock', -1)}</b>"
    )
    if is_msg:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _shop_add_receive(msg: Message, state: FSMContext, bot: Bot, field: str, value) -> None:
    """مقدارِ یه فیلد رو ذخیره می‌کنه، پیامِ خودِ ادمین رو پاک می‌کنه (حالتِ
    حذف‌و‌ارسال)، و می‌ره سراغِ مرحلهٔ بعد."""
    await state.update_data(**{field: value})
    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _shop_add_advance(msg, state, bot)


async def _shop_add_invalid(msg: Message, state: FSMContext, err: str) -> None:
    """مقدارِ نامعتبر: پیامِ ادمین رو پاک می‌کنه و همون پرامپت رو با پیامِ
    خطا دوباره می‌فرسته (بدونِ پیشروی به مرحلهٔ بعد)."""
    data = await state.get_data()
    idx = data.get("_add_step_idx", 0)
    prev_id = data.get("_add_prompt_msg_id")
    try:
        await msg.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if prev_id:
        try:
            await msg.bot.delete_message(msg.chat.id, prev_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    text = f"⚠️ {err}\n\n" + _add_step_text(idx, data)
    sent = await msg.answer(text, reply_markup=_add_step_kb(idx), parse_mode="HTML")
    await state.update_data(_add_prompt_msg_id=sent.message_id)


@shop_router.message(ShopFSM.add_title, F.text)
async def sadm_add_title(msg: Message, state: FSMContext, bot: Bot):
    title = (msg.text or "").strip()
    if not title:
        await _shop_add_invalid(msg, state, "عنوان نمی‌تونه خالی باشه.")
        return
    await _shop_add_receive(msg, state, bot, "title", title)


@shop_router.message(ShopFSM.add_desc)
async def sadm_add_desc(msg: Message, state: FSMContext, bot: Bot):
    desc = msg.html_text if msg.text else ""
    await _shop_add_receive(msg, state, bot, "description", desc)


@shop_router.message(ShopFSM.add_price, F.text)
async def sadm_add_price(msg: Message, state: FSMContext, bot: Bot):
    try:
        price = float(msg.text.replace(",", "").replace("،", "").strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await _shop_add_invalid(msg, state, "عدد نامعتبره. یه عددِ صحیح یا اعشاری بفرست.")
        return
    await _shop_add_receive(msg, state, bot, "price", price)


@shop_router.message(ShopFSM.add_stock, F.text)
async def sadm_add_stock(msg: Message, state: FSMContext, bot: Bot):
    try:
        stock = int(msg.text.strip())
        if stock < -1:
            raise ValueError
    except ValueError:
        await _shop_add_invalid(msg, state, "عدد نامعتبره (فقط عددِ صحیحِ ۱- یا بیشتر).")
        return
    await _shop_add_receive(msg, state, bot, "stock", stock)


@shop_router.message(ShopFSM.add_photo, F.photo | F.text)
async def sadm_add_photo(msg: Message, state: FSMContext, bot: Bot):
    if msg.text:
        await _shop_add_invalid(msg, state, "لطفاً یه عکس بفرست، یا از دکمهٔ «⏭ رد کردن» استفاده کن.")
        return
    photo_id = msg.photo[-1].file_id if msg.photo else None
    await _shop_add_receive(msg, state, bot, "photo_id", photo_id)


@shop_router.message(ShopFSM.add_file)
async def sadm_add_file(msg: Message, state: FSMContext, bot: Bot):
    if not msg.document:
        await _shop_add_invalid(msg, state, "لطفاً فایل رو به‌صورتِ سند (Document) بفرست، "
                                            "یا از دکمهٔ «⏭ رد کردن» استفاده کن.")
        return
    await _shop_add_receive(msg, state, bot, "file_id", msg.document.file_id)


# ─────────────────── گیفت‌های کلکسیونی/استارزیِ تلگرام ───────────────────

async def deliver_gift_if_needed(bot: Bot, product: Product, buyer_telegram_id: int) -> str | None:
    """اگر محصول از نوع «گیفتِ تلگرام» باشد، گیفت را با استفاده از موجودیِ استارزِ خودِ ربات
    برای خریدار ارسال می‌کند (bot.send_gift). در صورت خطا (مثلاً کمبود استارز ربات)،
    متن خطا را برمی‌گرداند تا به ادمین اطلاع داده شود؛ در غیر این صورت None."""
    if not product.is_gift or not product.gift_id:
        return None
    try:
        await bot.send_gift(user_id=buyer_telegram_id, gift_id=product.gift_id)
        return None
    except Exception as e:
        return str(e)


# === PATCH_GIFT_MANUAL_CONFIRM_V1 ===
async def _notify_admin_gift_manual(bot: Bot, bot_row: BotRow, order_id: int | None,
                                     product: Product, recipient_tg_id: int):
    """به ادمین اطلاع می‌دهد که یک گیفتِ دستی باید ارسال شود، همراه با دکمهٔ
    «✅ انجام شد» که با زدنش، پیامِ تاییدیه برای خریدار/گیرنده ارسال می‌شود."""
    text = (
        "🎁 <b>سفارشِ گیفتِ جدید — نیاز به ارسالِ دستی</b>\n"
        f"🛍 محصول: <b>{product.title}</b>\n"
        f"👤 گیرنده: <code>{recipient_tg_id}</code>\n\n"
        "لطفاً گیفت رو دستی برای آیدیِ بالا ارسال کن، بعد دکمهٔ زیر رو بزن."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ انجام شد", callback_data=f"shop:giftdone:{order_id or 0}:{recipient_tg_id}")
    ]])
    await _shop_notify_admins(bot, bot_row, text, kb)


@shop_router.callback_query(F.data.startswith("shop:giftdone:"))
async def shop_gift_mark_done(cb: CallbackQuery, bot: Bot):
    """ادمین دکمهٔ «✅ انجام شد» رو بعد از ارسالِ دستیِ گیفت می‌زنه — پیامِ تاییدیه
    برای خریدار/گیرندهٔ گیفت ارسال می‌شود و پیامِ ادمین قفل می‌شود."""
    parts = cb.data.split(":")
    order_id = int(parts[2]) if len(parts) > 2 else 0
    recipient_tg_id = int(parts[3]) if len(parts) > 3 else None
    if order_id:
        async with Session() as s:
            order = await s.get(ProductOrder, order_id)
            if order is not None and await _is_foreign(order, bot):   # PATCH_TENANT_SCOPE_V1
                order = None
            if order and order.status != "delivered":
                order.status = "delivered"
                await s.commit()
    if recipient_tg_id:
        try:
            await bot.send_message(
                recipient_tg_id,
                "🎁 گیفت شما با موفقیت ارسال شد!",
                parse_mode="HTML")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (اطلاعِ ارسالِ دستیِ گیفت)")
    try:
        original = cb.message.caption or cb.message.text or ""
        done_text = original + f"\n\n✅ <b>انجام شد توسط</b> @{cb.from_user.username or cb.from_user.full_name}"
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=done_text, reply_markup=None, parse_mode="HTML")
        else:
            await cb.message.edit_text(done_text, reply_markup=None, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("ثبت شد ✅")


async def _fetch_available_gifts(bot: Bot):
    """گیفت‌های تلگرام را می‌گیرد و بر اساس قیمتِ استارز مرتب می‌کند تا ترتیبِ صفحه‌ها ثابت بماند."""
    result = await bot.get_available_gifts()
    gifts = list(result.gifts)
    gifts.sort(key=lambda g: (g.star_count, g.id))
    return gifts


async def _send_gift_visual(bot: Bot, chat_id: int, sticker) -> bool:
    """گیفت‌ها معمولاً استیکرِ نوعِ custom_emoji دارند و تلگرام اجازه نمی‌دهد بات آن‌ها را با
    send_sticker بفرستد (خطای «can't send emoji stickers in messages»). بنابراین اگر استیکر
    از نوعِ عادی نبود، فایلِ استیکر را دانلود کرده و به‌صورتِ عکس/انیمیشن می‌فرستیم."""
    import io

    stype = getattr(sticker, "type", None)
    # اگر استیکرِ معمولی است، مسیرِ سریع را امتحان کن
    if stype not in ("custom_emoji",):
        try:
            await bot.send_sticker(chat_id=chat_id, sticker=sticker.file_id)
            return True
        except Exception:
            logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")  # می‌افتیم روی مسیرِ دانلود پایین

    # دانلودِ خودِ فایلِ استیکر و ارسال به‌عنوان عکس/انیمیشن/داکیومنت
    file_id = sticker.file_id
    # ترجیحاً thumbnail برای انیمیشنی/ویدیویی، چون webp/webm مستقیم به‌عنوان عکس نمی‌رود
    thumb = getattr(sticker, "thumbnail", None) or getattr(sticker, "thumb", None)
    is_anim = getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False)
    try:
        if is_anim and thumb is not None:
            file_id = thumb.file_id
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        buf.seek(0)
        data = buf.read()
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(data, filename="gift.png"))
            return True
        except Exception:
            buf.seek(0)
            await bot.send_document(
                chat_id=chat_id,
                document=BufferedInputFile(data, filename="gift.webp"))
            return True
    except Exception:
        return False


def _gift_caption(g, idx: int, total: int) -> str:
    emoji = getattr(g.sticker, "emoji", None) or "🎁"
    limited = g.total_count is not None
    limit_line = (f"\n📦 محدود: {g.remaining_count}/{g.total_count} مانده"
                  if limited else "\n📦 نامحدود")
    return (
        f"{emoji} <b>گیفتِ تلگرام</b>\n\n"
        f"شناسه: <code>{g.id}</code>\n"
        f"هزینهٔ تلگرام: ⭐ {g.star_count}"
        f"{limit_line}\n\n"
        f"گیفتِ {idx+1} از {total}\n\n"
        "برای افزودنِ همین گیفت به فروشگاه، دکمهٔ «➕ افزودن به فروشگاه» را بزن."
    )


def _gift_kb(g, idx: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"sadm:gifts:{idx-1}", style=ButtonStyle.PRIMARY))
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="sadm:gifts:noop", style=ButtonStyle.PRIMARY))
    if idx < total - 1:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"sadm:gifts:{idx+1}", style=ButtonStyle.PRIMARY))
    rows = [nav]
    rows.append([InlineKeyboardButton(
        text="➕ افزودن به فروشگاه",
        callback_data=f"sadm:giftadd:{g.id}:{g.star_count}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@shop_router.callback_query(F.data == "sadm:gifts:noop")
async def sadm_gifts_noop(cb: CallbackQuery):
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:gifts:"))
async def sadm_gifts_list(cb: CallbackQuery, bot: Bot):
    """نمایشِ گیفت‌های تلگرام یکی‌یکی، همراه با استیکرِ خودِ گیفت، تا ادمین دقیقاً ببینه هرکدوم چیه."""
    try:
        idx = int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        idx = 0
    try:
        gifts = await _fetch_available_gifts(bot)
    except Exception as e:
        try:
            await cb.message.edit_text(
                f"❌ دریافت لیست گیفت‌ها از تلگرام با خطا مواجه شد:\n{e}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)]]))
        except Exception:
            await cb.message.answer(
                f"❌ دریافت لیست گیفت‌ها از تلگرام با خطا مواجه شد:\n{e}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    if not gifts:
        try:
            await cb.message.edit_text(
                "فعلاً هیچ گیفتی از سمت تلگرام در دسترس نیست.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)]]))
        except Exception:
            await cb.message.answer(
                "فعلاً هیچ گیفتی از سمت تلگرام در دسترس نیست.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    total = len(gifts)
    idx = max(0, min(idx, total - 1))
    g = gifts[idx]
    caption = _gift_caption(g, idx, total)
    kb = _gift_kb(g, idx, total)

    sticker_note = ""
    try:
        # پیامِ قبلی (چه متنی چه استیکر) رو حذف می‌کنیم و پیامِ جدید رو دوباره می‌فرستیم،
        # چون استیکر نمی‌تونه کپشن داشته باشه؛ برای همین استیکر و متنِ توضیحات جدا فرستاده می‌شن.
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if getattr(g, "sticker", None):
            ok = await _send_gift_visual(bot, cb.message.chat.id, g.sticker)
            if not ok:
                sticker_note = "\n\n⚠️ این گیفت تصویرِ قابل‌نمایش ندارد."
        else:
            sticker_note = "\n\n⚠️ این گیفت استیکر/تصویر ندارد."
        await bot.send_message(chat_id=cb.message.chat.id, text=caption + sticker_note,
                                reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"خطا: {e}"[:200], show_alert=True)
        return
    await cb.answer()


@shop_router.callback_query(F.data.startswith("sadm:giftadd:"))
async def sadm_gift_add_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    parts = cb.data.split(":")
    gift_id = parts[2]
    star_count = int(parts[3])
    await state.update_data(gift_id=gift_id, gift_star_count=star_count)
    await state.set_state(ShopFSM.gift_price)
    # همون استیکرِ گیفتِ انتخابی رو دوباره می‌فرستیم تا مطمئن باشه داره روی همون گیفتی که دیده قیمت می‌ذاره
    try:
        gifts = await _fetch_available_gifts(bot)
        g = next((x for x in gifts if x.id == gift_id), None)
        if g and getattr(g, "sticker", None):
            await _send_gift_visual(bot, cb.message.chat.id, g.sticker)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"🎁 گیفتِ انتخابی: <code>{gift_id}</code> (هزینهٔ تلگرام: ⭐ {star_count})\n\n"
        "قیمتِ فروش این گیفت را به <b>تومان</b> برای مشتری‌ها وارد کن:",
        reply_markup=_shop_add_cancel_kb(), parse_mode="HTML")
    await cb.answer()


@shop_router.message(ShopFSM.gift_price, F.text)
async def sadm_gift_add_price(msg: Message, state: FSMContext, bot: Bot):
    # === PATCH_FIX_SHOP_GIFT_PRICE_CRASH_V1 ===
    # دو باگِ واقعی از error.log: (۱) اگه ادمین به‌جای عدد، دکمهٔ منو
    # («⬅️ بازگشت»، «🛒 فروشگاه دیجیتال» و...) رو می‌زد، float کرش می‌کرد؛
    # (۲) تابعِ _shop_add_cancel_kb اصلاً تعریف نشده بود و خودِ هندلرِ
    # خطا هم NameError می‌داد (۱۶ بار تو لاگ).
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "⬅️ بازگشت", "/cancel"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    try:
        price = float(raw.replace(",", "").replace("،", ""))
    except ValueError:
        await msg.answer("عدد نامعتبر. قیمت را به تومان و فقط با رقم بفرست (مثلاً 150000).",
                         reply_markup=_shop_add_cancel_kb())
        return
    data = await state.get_data()
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        p = Product(
            bot_id=bot_row.id,
            title=f"🎁 گیفت تلگرام (⭐ {data['gift_star_count']})",
            description="گیفتِ کلکسیونیِ تلگرام — بلافاصله بعد از پرداخت به حساب تلگرامِ شما ارسال می‌شود.",
            price=price,
            stock=-1,
            is_gift=True,
            gift_id=data["gift_id"],
            gift_star_count=data["gift_star_count"],
        )
        s.add(p)
        await s.commit()
    await state.clear()
    from tenant import main_kb_or_remove
    kb = await main_kb_or_remove(bot_row.id, bot_row.owner_id, msg.from_user.id)
    await msg.answer(
        f"✅ گیفت با قیمتِ فروشِ {rial(price)} به فروشگاه اضافه شد.",
        reply_markup=kb or ReplyKeyboardRemove())


# ─────────────────── آمار فروش ───────────────────

@shop_router.callback_query(F.data == "sadm:stats")
async def sadm_stats(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    async with Session() as s:
        total_orders = await s.scalar(
            select(func.count(ProductOrder.id)).where(
                ProductOrder.bot_id == bot_row.id,
                ProductOrder.status == "paid"
            )
        )
        total_revenue = await s.scalar(
            select(func.sum(ProductOrder.amount)).where(
                ProductOrder.bot_id == bot_row.id,
                ProductOrder.status == "paid"
            )
        )
        product_count = await s.scalar(
            select(func.count(Product.id)).where(Product.bot_id == bot_row.id)
        )
    text = (
        f"📊 <b>آمار فروشگاه</b>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"🛍 تعداد محصولات: {product_count}\n"
        f"🛒 خریدهای موفق: {total_orders}\n"
        f"💰 درآمد کل: {rial(total_revenue or 0)}\n"
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)]
    ]))
    await cb.answer()


@shop_router.callback_query(F.data == "sadm:lowstock")
async def sadm_lowstock(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    cur = await get_setting(bot_row.id, "shop_low_stock_threshold", "2")
    text = (
        "⚠️ <b>آستانهٔ هشدار اتمام موجودی</b>\n\n"
        f"مقدار فعلی: <b>{cur}</b>\n\n"
        "وقتی موجودیِ یک محصول به این عدد یا کمتر برسد، بهت پیام هشدار می‌آید.\n"
        "با گذاشتنِ <b>0</b>، این هشدار کاملاً خاموش می‌شود."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییر مقدار", callback_data="sadm:lowstock:edit", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:shop", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@shop_router.callback_query(F.data == "sadm:lowstock:edit")
async def sadm_lowstock_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    from tenant import smart_cancel_kb
    await state.set_state(ShopFSM.low_stock_threshold)
    await cb.message.answer("عددِ جدید را بفرست (مثلاً 2، یا 0 برای خاموش‌کردنِ هشدار):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@shop_router.message(ShopFSM.low_stock_threshold, F.text)
async def sadm_lowstock_edit_save(msg: Message, state: FSMContext, bot: Bot):
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        from tenant import main_kb_or_remove
        row = await get_bot_row_by_token(bot.token)
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb)
        return
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or msg.from_user.id != bot_row.owner_id:
        await state.clear()
        return
    if not raw.isdigit():
        await msg.answer("یک عدد معتبر بفرست (مثلاً 2):")
        return
    n = int(raw)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_row.id, Setting.key == "shop_low_stock_threshold"))
        if row:
            row.value = str(n)
        else:
            s.add(Setting(bot_id=bot_row.id, key="shop_low_stock_threshold", value=str(n)))
        await s.commit()
    await state.clear()
    msg_txt = "✅ هشدار اتمام موجودی خاموش شد." if n == 0 else f"✅ آستانه روی {n} تنظیم شد."
    await msg.answer(msg_txt, reply_markup=shop_admin_kb(bot_row.field))


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
