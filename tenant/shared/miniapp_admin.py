"""
miniapp_admin.py — پنلِ مدیریتِ «مینی‌اپ» برای مالک/ادمینِ هر ربات:
  • روشن/خاموش‌کردنِ دکمهٔ مینی‌اپ (Menu Button) رویِ همان ربات
  • ساخت/حذف/تغییرِ ترتیبِ دسته‌بندی‌های خرید (MiniAppCategory)
  • اختصاصِ هر VpnPlan به یکی از دسته‌ها

از همان الگویِ ماژول‌های مستقل پیروی می‌کند (نک: tenant/card_admin.py):
Router و FSM مستقلِ خودش، importهایِ مشترک از tenant/__init__.py به‌صورتِ
لوکال (داخلِ تابع) تا از circular import جلوگیری شود. دستورِ ورودی: /miniapp
"""
from __future__ import annotations

import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, MenuButtonWebApp, MenuButtonDefault, MenuButtonCommands, WebAppInfo,
)
from sqlalchemy import func, select

from config import Session, WEBHOOK_BASE
from models import MiniAppCategory, VpnPlan, Setting, Config, MainButton

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

miniapp_admin_router = Router()


async def _platform_miniapp_feature_enabled() -> bool:
    """سوییچِ سراسریِ قابلیتِ مینی‌اپ که فقط از پنلِ ادمینِ پلتفرم (Setting
    با bot_id=0، کلیدِ miniapp_feature_enabled) قابل‌تغییره. پیش‌فرض روشنه؛
    اگه پلتفرم خاموشش کنه، هیچ مستأجری نمی‌تونه مینی‌اپش رو روشن/ببینه."""
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "miniapp_feature_enabled"))
    return (r.value if (r and r.value is not None) else "on") != "off"


class MiniAppFSM(StatesGroup):
    cat_title = State()
    btn_text = State()  # === PATCH_MINIAPP_PLACEMENT_V1 ===
    accent_color = State()  # === PATCH_MINIAPP_THEME_V1 ===
    bg_color = State()      # === PATCH_MINIAPP_BG_THEME_V1 ===
    cat_rename = State()  # === PATCH_MINIAPP_CAT_RENAME_V1 ===
    card_color = State()  # === PATCH_MINIAPP_SECONDARY_COLOR_V1 ===


def _miniapp_url(bot_id: int) -> str:
    return f"{WEBHOOK_BASE.rstrip('/')}/miniapp/{bot_id}"


async def _apply_menu_button(bot: Bot, row, chat_id: int | None = None) -> str | None:
    """
    === PATCH_MINIAPP_MENU_BUTTON_FIX_V1 ===
    دکمهٔ منویِ رباتِ تنانت را بر اساسِ miniapp_enabled تنظیم می‌کند و در
    صورتِ خطا، متنِ خطا را برمی‌گرداند (قبلاً خطا فقط لاگ می‌شد و کاربر
    فکر می‌کرد کار انجام شده).

    دو نکتهٔ مهمِ خودِ تلگرام که باعثِ «روشن کردم ولی دکمه نیامد» می‌شد:
      ۱) setChatMenuButton بدونِ chat_id فقط *پیش‌فرضِ* ربات را عوض می‌کند و
         کلاینت‌ها آن را کَش می‌کنند؛ در چتی که همین حالا باز است تا ری‌استارتِ
         اپ به‌روز نمی‌شود. پس علاوه بر پیش‌فرض، برای چتِ خودِ کاربر هم
         جداگانه ست می‌شود تا فوراً دیده شود.
      ۲) اگر قبلاً برای یک چت به‌صورتِ اختصاصی دکمه ست شده باشد، مقدارِ
         پیش‌فرض برای آن چت نادیده گرفته می‌شود — بنابراین موقعِ خاموش‌کردن
         هم باید همان چت صراحتاً پاک شود.
    """
    if not row.token:
        return "توکنِ ربات ثبت نشده است."
    if row.miniapp_enabled and not WEBHOOK_BASE.startswith("https://"):
        return "WEBHOOK_BASE (با https) روی سرور تنظیم نشده است."
    # === PATCH_MINIAPP_PLACEMENT_V1 ===
    # در حالتِ «کیبورد»، دکمهٔ Menu اصلاً اشغال نمی‌شود تا منویِ دستوراتِ
    # ربات دست‌نخورده بماند؛ مینی‌اپ به‌جایش داخلِ کیبوردِ اصلی می‌آید.
    _place = await miniapp_place(row.id)
    if row.miniapp_enabled and _place == "menu":
        btn = MenuButtonWebApp(text=await miniapp_btn_text(row.id),
                               web_app=WebAppInfo(url=_miniapp_url(row.id)))
    else:
        # === PATCH_MINIAPP_MENU_BUTTON_OFF_FIX_V1 ===
        # عمداً MenuButtonCommands و نه MenuButtonDefault: «Default» یعنی
        # «به مقدارِ پیش‌فرض برگرد» و کلاینت‌های تلگرام در این حالت اغلب
        # همان دکمهٔ وب‌اپِ کَش‌شده را نگه می‌دارند؛ Commands یک مقدارِ صریح
        # است و دکمه واقعاً به منویِ دستورات برمی‌گردد.
        btn = MenuButtonCommands()
    tenant_bot = Bot(token=row.token)
    err: str | None = None
    try:
        # پیش‌فرضِ ربات (برای همهٔ کاربرانِ آینده)
        await tenant_bot.set_chat_menu_button(menu_button=btn)
        # و همان چتی که کاربر همین حالا داخلش است — تا فوری ببیند
        if chat_id:
            try:
                await tenant_bot.set_chat_menu_button(chat_id=chat_id, menu_button=btn)
            except Exception:
                logger.exception("ستِ دکمهٔ منو برایِ چتِ %s ناموفق بود", chat_id)
    except Exception as e:
        err = str(e) or e.__class__.__name__
        logger.exception("خطا در تنظیمِ دکمهٔ منویِ مینی‌اپ")
    finally:
        try:
            await tenant_bot.session.close()
        except Exception:
            logger.exception("بستنِ سشنِ رباتِ تنانت ناموفق بود")
    return err



# === PATCH_MINIAPP_PLACEMENT_V1 ===
_DEFAULT_BTN_TEXT = "🛍 فروشگاه"


async def miniapp_place(bot_id: int) -> str:
    """«menu» = دکمهٔ Menu تلگرام (پیش‌فرض) | «keyboard» = دکمه داخلِ کیبوردِ ربات."""
    from tenant import get_setting
    v = await get_setting(bot_id, "miniapp_place", "menu")
    return "keyboard" if v == "keyboard" else "menu"


async def miniapp_btn_text(bot_id: int) -> str:
    """متنِ دکمهٔ مینی‌اپ. کوتاه نگهش دار — تلگرام دکمهٔ Menu را به‌اندازهٔ
    متن پهن می‌کند و متنِ بلند کلِ کادرِ تایپ را می‌بلعد."""
    from tenant import get_setting
    return ((await get_setting(bot_id, "miniapp_btn_text", "")).strip()
            or _DEFAULT_BTN_TEXT)[:16]


async def _panel_text(row) -> str:
    """متنِ صفحهٔ مدیریتِ مینی‌اپ — یک‌جا تا /miniapp و دکمهٔ تنظیمات یکی باشند."""
    # === PATCH_MINIAPP_HIDE_URL_V1 ===
    # آدرسِ مینی‌اپ عمداً نمایش داده نمی‌شود (کاربردی برای مالک ندارد و
    # صفحه را شلوغ می‌کند)؛ فقط وضعیت.
    status = "فعال ✅" if row.miniapp_enabled else "غیرفعال ❌"
    # === PATCH_MINIAPP_PLACEMENT_V1 ===
    place = await miniapp_place(row.id)
    place_lbl = ("دکمهٔ Menu (کنارِ کادرِ تایپ)" if place == "menu"
                 else "دکمه‌ای داخلِ کیبوردِ ربات")
    label = await miniapp_btn_text(row.id)
    # === PATCH_MINIAPP_THEME_V1 ===
    from tenant.shared.miniapp import _miniapp_accent
    accent = await _miniapp_accent(row.id)
    hint = ("⚠️ در این حالت دکمهٔ Menu اشغال می‌شود و منویِ دستوراتِ ربات "
            "نمایش داده نمی‌شود (تلگرام فقط یک دکمهٔ Menu دارد)."
            if place == "menu" else
            "✅ در این حالت دکمهٔ Menu دست‌نخورده می‌ماند و مینی‌اپ به‌صورتِ یک "
            "دکمه داخلِ کیبوردِ اصلیِ ربات نمایش داده می‌شود.")
    return (f"🛍 <b>مینی‌اپِ فروشگاهیِ ربات</b>\n\n"
            f"وضعیت: {status}\n"
            f"📍 محلِ نمایش: {place_lbl}\n"
            f"🏷 متنِ دکمه: {label}\n"
            f"🎨 رنگِ دکمه‌ها: <code>{accent}</code>\n"
            f"🌗 تمِ اصلی: <code>{await _bg_label(row.id)}</code>\n\n{hint}")


async def _panel_kb(row, back: bool = True) -> InlineKeyboardMarkup:
    # === PATCH_MINIAPP_PANEL_BACK_ALWAYS_V1 ===
    # پیش‌فرض True شد: قبلاً دکمهٔ بازگشت فقط وقتی می‌آمد که کاربر مستقیم
    # از تنظیمات وارد شده بود، پس هر بار که از «دسته‌بندی‌ها» یا بعدِ
    # تاگل به این صفحه برمی‌گشت، صفحه بی‌دکمهٔ بازگشت و بن‌بست می‌شد.
    # === PATCH_MINIAPP_TENANT_SETTINGS_ENTRY_V1 ===
    # برچسبِ تاگل عوض می‌شود، پس رنگش هم باید عوض شود.
    if row.miniapp_enabled:
        toggle_txt, toggle_style = "🔴 خاموش‌کردنِ مینی‌اپ", ButtonStyle.DANGER
    else:
        toggle_txt, toggle_style = "🟢 روشن‌کردنِ مینی‌اپ", ButtonStyle.SUCCESS
    rows = [
        [InlineKeyboardButton(text=toggle_txt, callback_data="miniapp:toggle",
                              style=toggle_style)],
        [InlineKeyboardButton(text="🗂 دسته‌بندی‌های خرید", callback_data="miniapp:cats",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_MINIAPP_PLACEMENT_V1 ===
        [InlineKeyboardButton(
            text=("📍 محلِ نمایش: دکمهٔ Menu" if await miniapp_place(row.id) == "menu"
                  else "📍 محلِ نمایش: داخلِ کیبورد"),
            callback_data="miniapp:place", style=ButtonStyle.PRIMARY)],
        # === PATCH_MINIAPP_KB_POS_V1 === فقط وقتی محل = کیبورد معنا دارد
        *([[InlineKeyboardButton(
            text=("↕️ جایگاهِ دکمه در کیبورد: بالا ← ببر پایین" if await _kb_pos(row.id) == "top"
                  else "↕️ جایگاهِ دکمه در کیبورد: پایین ← ببر بالا"),
            callback_data="miniapp:kbpos", style=ButtonStyle.PRIMARY)]]
          if await miniapp_place(row.id) == "keyboard" else []),
        [InlineKeyboardButton(text="🏷 تغییرِ متنِ دکمه", callback_data="miniapp:btntext",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_MINIAPP_THEME_V1 ===
        [InlineKeyboardButton(text="🎨 رنگِ دکمه‌ها", callback_data="miniapp:color",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_MINIAPP_BG_THEME_V1 ===
        [InlineKeyboardButton(text="🌗 تمِ اصلی (پس‌زمینه)", callback_data="miniapp:bg",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_MINIAPP_SECONDARY_COLOR_V1 ===
        [InlineKeyboardButton(text="🎨 رنگِ ثانویه (کارت‌ها)", callback_data="miniapp:card",
                              style=ButtonStyle.PRIMARY)],        # === PATCH_MINIAPP_WHEEL_V1 ===
        [InlineKeyboardButton(text="🎡 گردونهٔ شانس", callback_data="wheel:menu",
                              style=ButtonStyle.SUCCESS)],
    ]
    # === PATCH_NFT_MARKET_SETTINGS_ENTRY_V2 ===
    # نسخهٔ اول این دکمه را اینجا (داخلِ پنلِ مینی‌اپِ فروشگاهی) هم می‌گذاشت،
    # ولی جایِ اصلی‌اش «دکمه‌ها و تنظیمات» است و داشتنِ دو ورودی برایِ یک
    # چیز گیج‌کننده بود (+ دکمهٔ بازگشتش را هم مبهم می‌کرد). فقط یک ورودی
    # می‌ماند: همان که در _sec_settings_kb ساخته می‌شود.
    _ = back  # همیشه نمایش داده می‌شود؛ پارامتر برایِ سازگاری مانده
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات",
                                      callback_data="miniapp:tosettings",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@miniapp_admin_router.callback_query(F.data == "miniapp:panel")
async def miniapp_panel_entry(cb: CallbackQuery, bot: Bot):
    """
    === PATCH_MINIAPP_TENANT_SETTINGS_ENTRY_V1 ===
    همان صفحهٔ /miniapp، ولی از دکمهٔ «🛍 مینی‌اپ فروشگاهی» در
    «🎛 دکمه‌ها و تنظیمات». دکمهٔ بازگشتش یک سطح بالاتر (همان تنظیمات) است.
    """
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    # پیام‌ها هم alert و هم متنِ عادی فرستاده می‌شوند تا در حالتِ کیبوردی
    # (که alert نداریم) کاربر بی‌پاسخ نماند.
    if not WEBHOOK_BASE.startswith("https://"):
        await cb.answer("⚠️ WEBHOOK_BASE (با https) روی سرور تنظیم نشده است.", show_alert=True)
        await cb.message.answer("⚠️ برای فعال‌سازیِ مینی‌اپ باید WEBHOOK_BASE (با https) "
                                "روی سرور تنظیم شده باشد.")
        return
    if not await _platform_miniapp_feature_enabled():
        await cb.answer("این قابلیت فعلاً از سمتِ بات‌ساز غیرفعال شده است.", show_alert=True)
        await cb.message.answer(
            "🛍 <b>مینی‌اپِ فروشگاهی</b>\n\n"
            "این قابلیت فعلاً از سمتِ بات‌ساز برای همهٔ ربات‌ها غیرفعال شده و در دسترس نیست.")
        return
    try:
        await cb.message.edit_text(await _panel_text(row), reply_markup=await _panel_kb(row, back=True))
    except Exception:
        await cb.message.answer(await _panel_text(row), reply_markup=await _panel_kb(row, back=True))
    await cb.answer()


# === PATCH_NFT_MARKET_V1 ===
@miniapp_admin_router.callback_query(F.data == "miniapp:nft")
async def miniapp_nft_entry(cb: CallbackQuery, bot: Bot):
    """بازکردنِ «مینی‌اپ ۲» (مارکتِ NFT) برایِ مالکِ ربات.

    کلِ مدیریت داخلِ خودِ مینی‌اپ است (تبِ ⚙️ مدیریت)، پس اینجا فقط یک
    دکمهٔ WebApp می‌دهیم — نه یک پنلِ دومِ موازی در چت.
    """
    from plans import Feature, has_feature
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    if (row.field or "") != "virtual_service":
        await cb.answer("این بخش فقط برایِ ربات‌هایِ خدماتِ مجازی است.", show_alert=True)
        return
    if not has_feature(row.plan, Feature.NFT_MARKET):
        await cb.answer("🔒 این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return

    url = f"{WEBHOOK_BASE.rstrip('/')}/miniapp2/{row.id}"
    # === PATCH_NFT_MARKET_SETTINGS_ENTRY_V2 ===
    # بازگشت باید دقیقاً یک سطح بالا برود: این صفحه از «دکمه‌ها و تنظیمات»
    # باز می‌شود، نه از پنلِ مینی‌اپِ فروشگاهی — قبلاً اشتباهاً به آنجا
    # می‌رفت و کاربر تویِ شاخهٔ یک محصولِ دیگر می‌افتاد.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 بازکردنِ مارکت", web_app=WebAppInfo(url=url),
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📘 راهنمایِ تحویلِ خودکار و دستی",
                              callback_data="miniapp:nfthelp", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات",
                              callback_data="miniapp:tosettings", style=ButtonStyle.DANGER)],
    ])
    txt = (
        "🖼 <b>مینی‌اپ ۲ — مارکتِ NFT</b>\n\n"
        "مارکتِ خرید و اجارهٔ گیفتِ NFT و یوزرنیم. موجودی مالِ خودِ توست و "
        "ثبت، قیمت‌گذاری و مدیریتِ سفارش‌ها همه داخلِ خودِ مینی‌اپ انجام می‌شود "
        "(تبِ «⚙️ مدیریت» که فقط برایِ تو نمایش داده می‌شود).\n\n"
        "<b>تحویلِ سفارش‌ها</b> دو حالت دارد: <b>خودکار</b> و <b>دستی</b>. "
        "برایِ راه‌اندازیِ قدم‌به‌قدم، دکمهٔ «📘 راهنما» را بزن."
    )
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:                                        # noqa: BLE001
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


# === PATCH_NFT_MARKET_HELP_V1 ===
@miniapp_admin_router.callback_query(F.data == "miniapp:nfthelp")
async def miniapp_nft_help(cb: CallbackQuery, bot: Bot):
    """راهنمایِ قدم‌به‌قدمِ فعال‌کردنِ تحویلِ خودکار / کارکردِ حالتِ دستی."""
    from plans import Feature, has_feature
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    if (row.field or "") != "virtual_service" or not has_feature(row.plan, Feature.NFT_MARKET):
        await cb.answer("🔒 در دسترس نیست.", show_alert=True)
        return

    txt = (
        "📘 <b>راهنمایِ تحویلِ سفارش‌ها</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "🤖 <b>حالتِ خودکار</b> (فقط گیفتِ NFT)\n"
        "ربات خودش گیفت را به خریدار منتقل می‌کند. برایِ راه‌اندازی:\n\n"
        "<b>۱. اول این قدم، وگرنه قدمِ بعدی خطا می‌دهد.</b>\n"
        "برو به <a href=\"https://t.me/BotFather\">@BotFather</a> ← "
        "<b>/mybots</b> ← همین ربات ← <b>Bot Settings</b> ← "
        "<b>Secretary Mode</b> ← <b>Turn on</b>.\n"
        "<i>⚠️ BotFather این گزینه را «Secretary Mode» (حالتِ منشی) نام‌گذاری "
        "کرده — در بعضی نسخه‌ها «Business Mode» هم دیده می‌شود. هر دو یک چیزند؛ "
        "هرکدام را دیدی همان را روشن کن.</i>\n"
        "<i>تا وقتی این را روشن نکنی، تلگرام موقعِ وصل‌کردن می‌گوید «این ربات "
        "هنوز از حالتِ منشی پشتیبانی نمی‌کند».</i>\n\n"
        "<b>۲.</b> در تلگرامِ <u>خودت</u> (همان اکانتی که گیفت‌ها داخلش است) برو به:\n"
        "<b>Settings ← Telegram Business ← Chatbots</b>\n"
        "<i>(تنظیمات ← کسب‌وکار ← ربات‌ها)</i>\n\n"
        "<b>۳.</b> یوزرنیمِ همین ربات را وارد کن: "
        f"<code>@{row.username or 'username_ربات'}</code>\n\n"
        "<b>۴.</b> در فهرستِ دسترسی‌ها، گزینهٔ "
        "<b>«Transfer and upgrade gifts»</b> <i>(انتقال و ارتقاءِ گیفت)</i> را روشن کن. "
        "بدونِ این تیک، انتقالِ خودکار کار نمی‌کند.\n\n"
        "<b>۵.</b> ربات بلافاصله پیامِ «✅ حسابِ کاری وصل شد» می‌فرستد. "
        "در تبِ «⚙️ مدیریت» مینی‌اپ هم بالایِ صفحه می‌بینی: "
        "«انتقالِ خودکار: ✅ فعال».\n\n"
        "<b>۶.</b> برایِ هر آیتم، فیلدِ <b>«شناسهٔ گیفت»</b> را پر کن. "
        "بدونِ آن، همان آیتم دستی تحویل داده می‌شود.\n\n"
        "━━━━━━━━━━━━━━\n"
        "✋ <b>حالتِ دستی</b>\n"
        "هیچ تنظیمی لازم ندارد و همیشه به‌عنوانِ پشتیبان فعال است:\n\n"
        "• کاربر سفارش می‌دهد و پول از کیف‌پولش کسر می‌شود.\n"
        "• ربات سفارش را با مشخصاتِ خریدار برایت می‌فرستد.\n"
        "• گیفت/یوزرنیم را خودت به او منتقل می‌کنی.\n"
        "• دکمهٔ <b>«✅ تحویل شد»</b> را می‌زنی تا سفارش بسته شود.\n"
        "• اگر نتوانستی، <b>«↩️ لغو و عودت»</b> پول را کامل به کیف‌پولش برمی‌گرداند.\n\n"
        "━━━━━━━━━━━━━━\n"
        "⚠️ <b>نکته‌هایِ مهم</b>\n"
        "• اگر تلگرام گفت <b>«این ربات هنوز از حالتِ منشی پشتیبانی نمی‌کند»</b>، "
        "یعنی قدمِ ۱ انجام نشده. در BotFather دنبالِ <b>Secretary Mode</b> بگرد "
        "(همان Business Mode). بعد از روشن‌کردنش چند ثانیه صبر کن و دوباره "
        "امتحان کن.\n"
        "• <b>یوزرنیم‌ها همیشه دستی‌اند</b> — تلگرام برایِ انتقالشان API ندارد.\n"
        "• اگر انتقالِ خودکار به هر دلیلی شکست بخورد، سفارش خودکار به حالتِ "
        "دستی برمی‌گردد و دلیلش را در همان پیام می‌بینی. هیچ سفارشی گم نمی‌شود.\n"
        "• در پایانِ مدتِ اجاره، ربات یادآوری می‌کند که آیتم را پس بگیری؛ "
        "پس‌گرفتن دستی است."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:nft",
                              style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:                                        # noqa: BLE001
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@miniapp_admin_router.callback_query(F.data == "miniapp:tosettings")
async def miniapp_back_to_settings(cb: CallbackQuery, bot: Bot):
    """بازگشتِ یک‌سطحی به «دکمه‌ها و تنظیمات»."""
    from tenant import get_bot_row, is_owner_or_admin, tadm_sec_settings
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await tadm_sec_settings(cb, bot)


@miniapp_admin_router.message(Command("miniapp"))
async def miniapp_entry(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    if not WEBHOOK_BASE.startswith("https://"):
        await msg.answer("⚠️ برای فعال‌سازیِ مینی‌اپ باید WEBHOOK_BASE (با https) روی سرور تنظیم شده باشد.")
        return
    if not await _platform_miniapp_feature_enabled():
        await msg.answer(
            "🛍 <b>مینی‌اپِ فروشگاهی</b>\n\n"
            "این قابلیت فعلاً از سمتِ بات‌ساز برای همهٔ رِبات‌ها غیرفعال شده و در دسترس نیست.\n"
            "بعداً دوباره امتحان کن یا با پشتیبانیِ بات‌ساز تماس بگیر.")
        return
    await msg.answer(await _panel_text(row), reply_markup=await _panel_kb(row))


@miniapp_admin_router.callback_query(F.data == "miniapp:toggle")
async def miniapp_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    if not row.miniapp_enabled and not await _platform_miniapp_feature_enabled():
        await cb.answer("این قابلیت فعلاً از سمتِ بات‌ساز غیرفعال شده است.", show_alert=True)
        return
    async with Session() as s:
        db_row = await s.get(type(row), row.id)
        db_row.miniapp_enabled = not db_row.miniapp_enabled
        await s.commit()
        await s.refresh(db_row)
    # === PATCH_MINIAPP_MENU_BUTTON_FIX_V1 ===
    _chat_id = getattr(getattr(cb, "message", None), "chat", None)
    _err = await _apply_menu_button(bot, db_row,
                                    chat_id=getattr(_chat_id, "id", None))
    # === PATCH_MINIAPP_TENANT_SETTINGS_ENTRY_V1 ===
    # اگر کاربر از مسیرِ تنظیمات آمده، دکمهٔ بازگشت باید بماند.
    _markup = getattr(cb.message, "reply_markup", None) if cb.message else None
    _back = any(b.callback_data == "miniapp:tosettings"
                for r in (getattr(_markup, "inline_keyboard", None) or [])
                for b in r)
    try:
        await cb.message.edit_text(await _panel_text(db_row), reply_markup=await _panel_kb(db_row, back=_back))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده")
    if _err:
        await cb.answer("وضعیت ذخیره شد، ولی ستِ دکمهٔ منو ناموفق بود.", show_alert=True)
        await cb.message.answer(
            "⚠️ وضعیتِ مینی‌اپ ذخیره شد، اما تلگرام دکمهٔ منو را قبول نکرد:\n"
            f"<code>{_html.escape(_err)[:300]}</code>")
    elif db_row.miniapp_enabled:
        await cb.answer("روشن شد ✅")
        await cb.message.answer(
            "✅ مینی‌اپ روشن شد.\n\n"
            "اگر دکمهٔ «🛍 فروشگاه» را کنارِ کادرِ تایپ نمی‌بینی، یک‌بار از چتِ ربات "
            "بیرون برو و دوباره واردش شو (تلگرام دکمهٔ منو را کَش می‌کند). "
            "برایِ کاربرانِ دیگر خودکار نمایش داده می‌شود.")
    else:
        await cb.answer("خاموش شد ✅")
        await cb.message.answer(
            "✅ مینی‌اپ خاموش شد.\n\n"
            "اگر هنوز دکمهٔ «🛍 فروشگاه» را کنارِ کادرِ تایپ می‌بینی، یک‌بار از چتِ "
            "ربات بیرون برو و دوباره واردش شو — تلگرام دکمهٔ منو را روی گوشی "
            "کَش می‌کند و تا بازکردنِ دوبارهٔ چت، تصویرِ قدیمی را نشان می‌دهد.")
        await cb.message.answer(
            "🔴 مینی‌اپ خاموش شد.\n\n"
            "اگر هنوز دکمهٔ «🛍 فروشگاه» را کنارِ کادرِ تایپ می‌بینی، یک‌بار از چتِ "
            "ربات بیرون برو و دوباره واردش شو — تلگرام دکمهٔ منو را روی گوشی "
            "کَش می‌کند و تا باز‌شدنِ دوبارهٔ چت به‌روز نمی‌شود.")


# === PATCH_MINIAPP_KB_POS_V1 ===
async def _kb_pos(bot_id: int) -> str:
    from tenant import get_setting
    return "top" if (await get_setting(bot_id, "miniapp_kb_pos", "bottom")) == "top" else "bottom"


@miniapp_admin_router.callback_query(F.data == "miniapp:kbpos")
async def miniapp_kbpos_toggle(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ دکمهٔ مینی‌اپ در کیبورد: اولین ردیف ⇄ آخرین ردیف (قبلِ پنلِ مدیریت)."""
    from tenant import get_bot_row, is_owner_or_admin, set_setting
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    new = "bottom" if await _kb_pos(row.id) == "top" else "top"
    await set_setting(row.id, "miniapp_kb_pos", new)
    try:
        await cb.message.edit_text(await _panel_text(row),
                                   reply_markup=await _panel_kb(row, back=_came_from_settings(cb)))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (به‌روزرسانیِ پنلِ مینی‌اپ بعد از جابه‌جایی)")
    await cb.answer("⬆️ دکمه رفت بالا" if new == "top" else "⬇️ دکمه رفت پایین", show_alert=False)
    await cb.message.answer("✅ جایگاهِ دکمه عوض شد. کاربر با /start یا «بازگشت به خانه» کیبوردِ تازه را می‌گیرد.")


# === PATCH_MINIAPP_PLACEMENT_V1 ===
@miniapp_admin_router.callback_query(F.data == "miniapp:place")
async def miniapp_place_toggle(cb: CallbackQuery, bot: Bot):
    """جابه‌جاییِ محلِ نمایشِ مینی‌اپ بینِ دکمهٔ Menu و کیبوردِ ربات."""
    from tenant import get_bot_row, is_owner_or_admin, set_setting
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    new_place = "keyboard" if await miniapp_place(row.id) == "menu" else "menu"
    await set_setting(row.id, "miniapp_place", new_place)
    # دکمهٔ Menu باید فوراً با محلِ جدید هماهنگ شود (در حالتِ کیبورد آزاد شود)
    _chat = getattr(getattr(cb, "message", None), "chat", None)
    await _apply_menu_button(bot, row, chat_id=getattr(_chat, "id", None))
    try:
        await cb.message.edit_text(await _panel_text(row),
                                   reply_markup=await _panel_kb(row, back=_came_from_settings(cb)))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده")
    if new_place == "keyboard":
        await cb.answer("حالا داخلِ کیبورد نمایش داده می‌شود ✅", show_alert=True)
        await cb.message.answer(
            "📍 مینی‌اپ به کیبوردِ ربات منتقل شد.\n\n"
            "دکمهٔ Menu آزاد شد و منویِ دستورات دوباره سرِ جایش برمی‌گردد. "
            "دکمهٔ مینی‌اپ حالا در کیبوردِ اصلیِ ربات دیده می‌شود — کاربر با "
            "زدنِ /start یا «بازگشت به خانه» کیبوردِ تازه را می‌گیرد.")
    else:
        await cb.answer("حالا رویِ دکمهٔ Menu است ✅", show_alert=True)


@miniapp_admin_router.callback_query(F.data == "miniapp:btntext")
async def miniapp_btntext_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(MiniAppFSM.btn_text)
    await cb.message.answer(
        "🏷 متنِ تازهٔ دکمهٔ مینی‌اپ را بفرست.\n\n"
        "کوتاه بنویس (۱ تا ۳ کلمه) — تلگرام دکمهٔ Menu را به‌اندازهٔ متن پهن "
        "می‌کند و متنِ بلند کلِ کادرِ تایپ را می‌گیرد.\n"
        "مثال‌های خوب: «🛍» یا «فروشگاه» یا «🛍 خرید»")
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.btn_text)
async def miniapp_btntext_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, set_setting, _FakeCb
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    text = (msg.text or "").strip()[:16]
    if not text:
        await msg.answer("متنِ دکمه نمی‌تواند خالی باشد. یک متن بفرست:")
        await state.set_state(MiniAppFSM.btn_text)
        return
    # === PATCH_MINIAPP_BTNTEXT_RESERVED_V1 ===
    # اگر متنِ دکمهٔ مینی‌اپ با یکی از دکمه‌های ثابتِ ربات یکی شود، کلیکِ
    # کاربر رویِ آن دکمه به مینی‌اپ می‌رود و ناوبریِ ربات خراب می‌شود.
    from tenant import (BTN_BACK_MENU, BTN_BACK_LEVEL, BTN_TADMIN,
                        BTN_SUPPORT_ROW)
    _reserved = {BTN_BACK_MENU, BTN_BACK_LEVEL, BTN_TADMIN, BTN_SUPPORT_ROW}
    if text in _reserved or text in {"بازگشت", "⬅️ بازگشت"}:
        await msg.answer(
            "این متن برایِ دکمه‌های ثابتِ ربات (بازگشت / پنل مدیریت / پشتیبانی) "
            "رزرو شده و انتخابش ناوبریِ ربات را خراب می‌کند.\n"
            "یک متنِ دیگر بفرست — مثلاً «🛍» یا «فروشگاه».")
        await state.set_state(MiniAppFSM.btn_text)
        return
    async with Session() as _s:
        _clash = await _s.scalar(select(MainButton).where(
            MainButton.bot_id == row.id, MainButton.label == text))
    if _clash:
        await msg.answer(
            f"یکی از دکمه‌های منویِ ربات همین متن («{text}») را دارد؛ اگر این "
            "را انتخاب کنی یکی از آن دو از کار می‌افتد.\n"
            "یک متنِ دیگر بفرست:")
        await state.set_state(MiniAppFSM.btn_text)
        return
    await set_setting(row.id, "miniapp_btn_text", text)
    await _apply_menu_button(bot, row, chat_id=msg.chat.id)
    await msg.answer(f"✅ متنِ دکمه به «{text}» تغییر کرد.")
    await miniapp_panel_entry(_FakeCb(msg), bot)


# === PATCH_MINIAPP_THEME_V1 ===
_COLOR_SWATCHES = [
    ("🔴 قرمز", "#ef4444"),
    ("🟠 نارنجی", "#ff7a1a"),
    ("🟡 زرد", "#eab308"),
    ("🟢 سبز", "#22c55e"),
    ("🔵 آبی", "#3b82f6"),
    ("🟣 بنفش", "#a855f7"),
    ("🌸 صورتی", "#ec4899"),
    ("⚪️ سفید", "#e5e7eb"),
]


@miniapp_admin_router.callback_query(F.data == "miniapp:color")
async def miniapp_color_start(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    from tenant.shared.miniapp import _miniapp_accent
    cur = await _miniapp_accent(row.id)
    rows = []
    line = []
    for label, hexv in _COLOR_SWATCHES:
        mark = "✅ " if hexv.lower() == cur.lower() else ""
        line.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"miniapp:colorpick:{hexv}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(text="✏️ رنگِ دلخواه (کدِ هگز)", callback_data="miniapp:colorcustom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:panel", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        f"🎨 <b>رنگِ مینی‌اپ</b>\n\n"
        f"این رنگ روی دکمه‌ها، برچسب‌ها و تأکیدهایِ مینی‌اپ اعمال می‌شه. الان: <code>{cur}</code>\n\n"
        "یکی از رنگ‌هایِ آماده رو انتخاب کن، یا کدِ هگزِ دلخواهِ خودتو بفرست:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:colorpick:"))
async def miniapp_color_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin, set_setting, _FakeCb
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    hexv = cb.data.rsplit(":", 1)[1]
    await set_setting(row.id, "miniapp_accent_color", hexv)
    await cb.answer(f"✅ رنگ ذخیره شد: {hexv}")
    await miniapp_panel_entry(cb, bot)


@miniapp_admin_router.callback_query(F.data == "miniapp:colorcustom")
async def miniapp_color_custom_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(MiniAppFSM.accent_color)
    await cb.message.answer(
        "✏️ کدِ هگزِ رنگِ دلخواهت رو بفرست (مثلاً <code>#ef4444</code>).\n"
        "از سایت‌هایی مثلِ htmlcolorcodes.com می‌تونی کد بگیری.",
        parse_mode="HTML")
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.accent_color)
async def miniapp_color_custom_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, set_setting, _FakeCb
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if not raw.startswith("#"):
        raw = "#" + raw
    import re as _re
    if not _re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", raw):
        await msg.answer("❌ کدِ هگزِ نامعتبر. باید مثلِ <code>#ef4444</code> یا <code>#f44</code> باشه. دوباره بفرست:",
                         parse_mode="HTML")
        await state.set_state(MiniAppFSM.accent_color)
        return
    await set_setting(row.id, "miniapp_accent_color", raw)
    await msg.answer(f"✅ رنگ ذخیره شد: {raw}")
    await miniapp_panel_entry(_FakeCb(msg), bot)


def _came_from_settings(cb: CallbackQuery) -> bool:
    _m = getattr(cb, "message", None)
    _markup = getattr(_m, "reply_markup", None) if _m else None
    return any(b.callback_data == "miniapp:tosettings"
               for r in (getattr(_markup, "inline_keyboard", None) or [])
               for b in r)


@miniapp_admin_router.callback_query(F.data == "miniapp:cats")
async def miniapp_cats(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cats = (await s.scalars(select(MiniAppCategory).where(
            MiniAppCategory.bot_id == row.id).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all()
    rows = [[InlineKeyboardButton(
        text=f"{'🟢' if c.active else '⚪️'} {c.icon or '🛰'} {c.title}" + (" — 🔴 ظرفیت پر" if c.sale_full else ""),
        callback_data=f"miniapp:cat:{c.id}")] for c in cats]
    rows.append([InlineKeyboardButton(text="➕ دستهٔ جدید", callback_data="miniapp:catnew",
                                      style=ButtonStyle.SUCCESS)])
    # === PATCH_HIDE_ALLSERVERS_V1 ===
    from tenant import get_setting as _gs
    _hidden = (await _gs(row.id, "miniapp_hide_all", "0")) == "1"
    rows.append([InlineKeyboardButton(
        text=("🙈 «همه سرورها» پنهان است — نمایشش بده" if _hidden
              else "🌐 پنهان‌کردنِ دستهٔ «همه سرورها»"),
        callback_data="miniapp:hideall",
        style=(ButtonStyle.SUCCESS if _hidden else ButtonStyle.PRIMARY))])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:back", style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text(
            "🗂 <b>دسته‌بندی‌های خرید در مینی‌اپ</b>\n\nهر پلن VPN فقط زیرِ یکی از این دسته‌ها در فروشگاه دیده می‌شود.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده")
    await cb.answer()


@miniapp_admin_router.callback_query(F.data == "miniapp:back")
async def miniapp_back(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    try:
        await cb.message.edit_text(await _panel_text(row), reply_markup=await _panel_kb(row))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده")
    await cb.answer()


# === PATCH_HIDE_ALLSERVERS_V1 ===
@miniapp_admin_router.callback_query(F.data == "miniapp:hideall")
async def miniapp_hide_all(cb: CallbackQuery, bot: Bot):
    """پنهان/نمایشِ دستهٔ «همه سرورها» در مینی‌اپ."""
    from tenant import get_bot_row, is_owner_or_admin, get_setting, set_setting
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    cur = (await get_setting(row.id, "miniapp_hide_all", "0")) == "1"
    await set_setting(row.id, "miniapp_hide_all", "0" if cur else "1")

    if not cur:
        # هشدارِ لازم: پلن‌هایِ بی‌دسته دیگر هیچ‌جا دیده نمی‌شوند
        async with Session() as s:
            orphan = await s.scalar(select(func.count(VpnPlan.id)).where(
                VpnPlan.bot_id == row.id, VpnPlan.active == True,
                VpnPlan.miniapp_category_id.is_(None))) or 0
        if orphan:
            await cb.answer(
                f"«همه سرورها» پنهان شد.\n\n⚠️ {orphan} پلن هیچ دسته‌ای "
                "ندارند و حالا در مینی‌اپ دیده نمی‌شوند. آن‌ها را داخلِ "
                "یکی از دسته‌ها بگذار.", show_alert=True)
        else:
            await cb.answer("«همه سرورها» پنهان شد ✅")
    else:
        await cb.answer("«همه سرورها» دوباره نمایش داده می‌شود ✅")
    await miniapp_cats(cb, bot)


@miniapp_admin_router.callback_query(F.data == "miniapp:catnew")
async def miniapp_catnew(cb: CallbackQuery, state: FSMContext):
    await state.set_state(MiniAppFSM.cat_title)
    await cb.message.answer("📝 اسمِ دستهٔ جدید رو بفرست (مثلاً «پنل نامحدود»):")
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.cat_title)
async def miniapp_catnew_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    title = (msg.text or "").strip()[:60]
    if not title:
        await msg.answer("اسمِ دسته نمی‌تونه خالی باشه.")
        return
    async with Session() as s:
        max_order = await s.scalar(select(MiniAppCategory.sort_order).where(
            MiniAppCategory.bot_id == row.id).order_by(MiniAppCategory.sort_order.desc()))
        s.add(MiniAppCategory(bot_id=row.id, title=title, sort_order=(max_order or 0) + 1))
        await s.commit()
    # === PATCH_MINIAPP_CATS_NAV_V1 ===
    # قبلاً اینجا فقط یک پیامِ متنی می‌آمد و کاربر بدونِ هیچ دکمه‌ای در
    # بن‌بست می‌ماند؛ حالا مستقیم فهرستِ دسته‌ها دوباره نشان داده می‌شود.
    from tenant import _FakeCb
    await msg.answer(f"✅ دستهٔ «{title}» ساخته شد.")
    await miniapp_cats(_FakeCb(msg), bot)


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:cat:"))
async def miniapp_cat_detail(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        plan_count = await s.scalar(select(VpnPlan.id).where(
            VpnPlan.bot_id == row.id, VpnPlan.miniapp_category_id == cat_id))
    # === PATCH_MINIAPP_STOCK_CATS_V1 ===
    stock_list = _stock_list(cat)
    if cat.active:
        toggle_txt, toggle_style = "⚪️ غیرفعال‌کردن", ButtonStyle.DANGER
    else:
        toggle_txt, toggle_style = "🟢 فعال‌کردن", ButtonStyle.SUCCESS
    rows = [
        [InlineKeyboardButton(text="✏️ تغییرِ نام", callback_data=f"miniapp:catren:{cat_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🤖 پلن‌های VPN خودکار", callback_data=f"miniapp:catplans:{cat_id}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📦 کانفیگ‌های VPN آماده (انبار)",
                              callback_data=f"miniapp:catstock:{cat_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=("🔴 ظرفیتِ فروش: پر (بزن تا آزاد شود)" if cat.sale_full else "🟢 ظرفیتِ فروش: آزاد (بزن تا پر شود)"),
            callback_data=f"miniapp:catfull:{cat_id}",
            style=(ButtonStyle.DANGER if cat.sale_full else ButtonStyle.SUCCESS))],   # PATCH_CATEGORY_SALE_FULL_V1
        [InlineKeyboardButton(text=toggle_txt, callback_data=f"miniapp:cattoggle:{cat_id}",
                              style=toggle_style)],
        # ترتیبِ نمایشِ دسته‌ها در فروشگاهِ ربات و مینی‌اپ
        [InlineKeyboardButton(text="🔼 بالا بردن", callback_data=f"miniapp:catmv:{cat_id}:up", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="🔽 پایین بردن", callback_data=f"miniapp:catmv:{cat_id}:dn", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🗑 حذفِ دسته", callback_data=f"miniapp:catdel:{cat_id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:cats", style=ButtonStyle.DANGER)],
    ]
    stock_txt = "، ".join("بدونِ دسته" if x == "-" else x for x in stock_list) or "ندارد"
    await cb.message.edit_text(
        f"🗂 دستهٔ «{cat.title}»\n\n"
        f"🤖 پلنِ VPN خودکار: {'دارد' if plan_count else 'ندارد'}\n"
        f"📦 انبارِ کانفیگ آماده: {stock_txt}\n\n"
        "هرچه اینجا وصل کنی، در همین دسته از مینی‌اپ به کاربر نشان داده می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def move_category(bot_id: int, cat_id: int, direction: str) -> bool:
    """دسته را یک پله بالا/پایین می‌برد. ترتیبِ فعلی اول به 0..n-1 پیوسته می‌شود
    (دسته‌هایی که sort_order برابر دارند هم درست جابه‌جا شوند). True = جابه‌جا شد."""
    async with Session() as s:
        cats = list((await s.scalars(select(MiniAppCategory).where(
            MiniAppCategory.bot_id == bot_id).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all())
        idx = next((i for i, c in enumerate(cats) if c.id == cat_id), None)
        if idx is None:
            return False
        j = idx - 1 if direction == "up" else idx + 1
        for i, c in enumerate(cats):
            c.sort_order = i
        moved = 0 <= j < len(cats)
        if moved:
            cats[idx].sort_order, cats[j].sort_order = cats[j].sort_order, cats[idx].sort_order
        await s.commit()
    return moved


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catmv:"))
async def miniapp_cat_move(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    _, _, cat_id, direction = cb.data.split(":")
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, int(cat_id))
    if not cat or cat.bot_id != row.id:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    moved = await move_category(row.id, cat.id, direction)
    if not moved:
        await cb.answer("بالاتر/پایین‌تر از این جایی نیست.")
        return
    await cb.answer("✅ جابه‌جا شد")
    await miniapp_cats(cb, bot)        # لیست تازه با ترتیبِ جدید (ترتیب همان ترتیبِ فروشگاه است)


# === PATCH_MINIAPP_CAT_RENAME_V1 ===
@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catren:"))
async def miniapp_cat_rename_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
    if not cat or cat.bot_id != row.id:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await state.set_state(MiniAppFSM.cat_rename)
    await state.update_data(cat_rename_id=cat_id)
    await cb.message.answer(
        f"✏️ اسمِ فعلی: <code>{cat.title}</code>\n\nاسمِ جدیدِ دسته را بفرست:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"miniapp:cat:{cat_id}", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.cat_rename, F.text)
async def miniapp_cat_rename_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, _FakeCb
    row = await get_bot_row(bot)
    data = await state.get_data()
    cat_id = data.get("cat_rename_id")
    await state.clear()
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    new_title = (msg.text or "").strip()[:60]
    if not new_title:
        await msg.answer("❌ اسمِ خالی قابلِ‌قبول نیست.")
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != row.id:
            await msg.answer("دسته پیدا نشد.")
            return
        cat.title = new_title
        await s.commit()
        plan_count = await s.scalar(select(VpnPlan.id).where(
            VpnPlan.bot_id == row.id, VpnPlan.miniapp_category_id == cat_id))
    # === PATCH_MINIAPP_CAT_RENAME_V1 ===
    # miniapp_cat_detail همیشه edit_text می‌زنه (بدونِ فال‌بک) چون طراحی
    # شده برایِ صدازده‌شدن با یه cb که پیامش خودِ ربات فرستاده؛ اینجا پیام
    # پیامِ خودِ کاربره (ورودیِ تایپ‌شده)، پس نمی‌شه edit کرد — به‌جاش
    # مستقیم یه پیامِ تازه با همون محتوا می‌فرستیم.
    stock_list = _stock_list(cat)
    stock_txt = "، ".join("بدونِ دسته" if x == "-" else x for x in stock_list) or "ندارد"
    toggle_txt, toggle_style = (("⚪️ غیرفعال‌کردن", ButtonStyle.DANGER) if cat.active
                                else ("🟢 فعال‌کردن", ButtonStyle.SUCCESS))
    rows = [
        [InlineKeyboardButton(text="✏️ تغییرِ نام", callback_data=f"miniapp:catren:{cat_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🤖 پلن‌های VPN خودکار", callback_data=f"miniapp:catplans:{cat_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📦 کانفیگ‌های VPN آماده (انبار)", callback_data=f"miniapp:catstock:{cat_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=("🔴 ظرفیتِ فروش: پر (بزن تا آزاد شود)" if cat.sale_full else "🟢 ظرفیتِ فروش: آزاد (بزن تا پر شود)"),
            callback_data=f"miniapp:catfull:{cat_id}",
            style=(ButtonStyle.DANGER if cat.sale_full else ButtonStyle.SUCCESS))],   # PATCH_CATEGORY_SALE_FULL_V1
        [InlineKeyboardButton(text=toggle_txt, callback_data=f"miniapp:cattoggle:{cat_id}", style=toggle_style)],
        [InlineKeyboardButton(text="🗑 حذفِ دسته", callback_data=f"miniapp:catdel:{cat_id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:cats", style=ButtonStyle.DANGER)],
    ]
    await msg.answer(
        f"✅ اسمِ دسته به «{new_title}» تغییر کرد.\n\n"
        f"🗂 دستهٔ «{cat.title}»\n\n"
        f"🤖 پلنِ VPN خودکار: {'دارد' if plan_count else 'ندارد'}\n"
        f"📦 انبارِ کانفیگ آماده: {stock_txt}\n\n"
        "هرچه اینجا وصل کنی، در همین دسته از مینی‌اپ به کاربر نشان داده می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
def _stock_list(cat) -> list[str]:
    """دسته‌هایِ انبارِ وصل‌شده به یک دستهٔ مینی‌اپ، به‌صورتِ لیست."""
    raw = (getattr(cat, "stock_categories", None) or "").strip()
    return [x for x in (p.strip() for p in raw.split(",")) if x]


async def _stock_cat_names(bot_id: int) -> list[str]:
    """دسته‌بندی‌هایِ موجود در انبارِ کانفیگ‌های آمادهٔ این ربات.
    مقدارِ ویژهٔ "-" یعنی کانفیگ‌هایِ بدونِ دسته‌بندی."""
    # === PATCH_MINIAPP_STOCK_EMPTY_FIX_V1 ===
    # قبلاً اینجا Config.price > 0 هم شرط بود و برایِ ادمین‌هایی که قیمتِ
    # کانفیگ‌ها را سطحِ رباتی گذاشته‌اند (price = 0 در خودِ رکورد)، فهرست
    # کاملاً خالی درمی‌آمد و پیغامِ «هیچ کانفیگی نداری» می‌آمد — درحالی‌که
    # انبار پر بود. حالا همهٔ کانفیگ‌های موجود شمرده می‌شوند و نبودنِ قیمت
    # جداگانه به مالک هشدار داده می‌شود.
    async with Session() as s:
        names = [x for x in (await s.scalars(select(Config.category).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.category.is_not(None)).distinct())).all() if x]
        has_uncat = await s.scalar(select(Config.id).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.category.is_(None)))
    out = sorted(set(names))
    if has_uncat:
        out.append("-")
    return out


async def _stock_priced_count(bot_id: int) -> tuple[int, int]:
    """(کلِ کانفیگ‌های موجود، تعدادی که قیمتِ اختصاصی دارند)."""
    from sqlalchemy import func as _f
    async with Session() as s:
        total = await s.scalar(select(_f.count(Config.id)).where(
            Config.bot_id == bot_id, Config.status == "in_stock")) or 0
        priced = await s.scalar(select(_f.count(Config.id)).where(
            Config.bot_id == bot_id, Config.status == "in_stock",
            Config.price > 0)) or 0
    return int(total), int(priced)


async def _render_stock_page(cb: CallbackQuery, bot_id: int, cat_id: int) -> None:
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != bot_id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        chosen = set(_stock_list(cat))
        title = cat.title
    names = await _stock_cat_names(bot_id)
    rows = []
    for name in names:
        lbl = "بدونِ دسته‌بندی" if name == "-" else name
        mark = "✅" if name in chosen else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} 📦 {lbl}",
            callback_data=f"miniapp:stockset:{cat_id}:{name}",
            style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"miniapp:cat:{cat_id}",
                                      style=ButtonStyle.DANGER)])
    # === PATCH_MINIAPP_STOCK_EMPTY_FIX_V1 ===
    total, priced = await _stock_priced_count(bot_id)
    head = f"📦 <b>کانفیگ‌های آماده — دستهٔ «{title}»</b>\n\n"
    if not names:
        txt = head + ("انبارِ کانفیگ‌های آماده‌ات خالی است.\n"
                      "اول از پنلِ مدیریت کانفیگ وارد انبار کن.")
    else:
        txt = head + ("روی هر دستهٔ انبار بزن تا زیرِ این دسته در مینی‌اپ نمایش "
                      "داده شود (یا از آن خارج شود):")
        if not priced:
            txt += (f"\n\n⚠️ هر {total} کانفیگِ موجودت قیمتِ اختصاصی ندارد. "
                    "مینی‌اپ فقط کانفیگ‌هایی را می‌فروشد که رویِ خودشان قیمت "
                    "خورده باشد — از پنلِ انبار برایشان قیمت بگذار، وگرنه در "
                    "فروشگاه چیزی نمایش داده نمی‌شود.")
        elif priced < total:
            txt += (f"\n\nℹ️ از {total} کانفیگِ موجود، {priced} تا قیمت دارند و "
                    "فقط همان‌ها در مینی‌اپ نمایش داده می‌شوند.")
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catstock:"))
async def miniapp_cat_stock(cb: CallbackQuery, bot: Bot):
    """انتخابِ اینکه کدام دسته‌هایِ انبارِ کانفیگ آماده زیرِ این دسته بیایند."""
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await _render_stock_page(cb, row.id, cat_id)
    await cb.answer()


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:stockset:"))
async def miniapp_stock_set(cb: CallbackQuery, bot: Bot):
    """تاگلِ یک دستهٔ انبار داخل/خارجِ یک دستهٔ مینی‌اپ."""
    from tenant import get_bot_row, is_owner_or_admin
    parts = cb.data.split(":", 4)  # miniapp:stockset:{cat_id}:{name}
    if len(parts) < 4:
        await cb.answer("درخواستِ نامعتبر", show_alert=True)
        return
    cat_id, name = int(parts[2]), parts[3]
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        cur = _stock_list(cat)
        if name in cur:
            cur.remove(name)
            msg = "از این دسته خارج شد"
        else:
            cur.append(name)
            msg = "به این دسته اضافه شد"
        cat.stock_categories = ",".join(cur)[:200]
        await s.commit()
    await _render_stock_page(cb, row.id, cat_id)
    await cb.answer(msg)


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catplans:"))
async def miniapp_cat_plans(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == row.id, VpnPlan.active == True).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    rows = []
    for p in plans:
        mark = "✅" if p.miniapp_category_id == cat_id else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {p.title} ({_rial(p.price)} تومان)",
            callback_data=f"miniapp:planset:{cat_id}:{p.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"miniapp:cat:{cat_id}",
                                      style=ButtonStyle.DANGER)])
    if not plans:
        await cb.answer("هنوز هیچ پلنِ فعالی برای این ربات تعریف نشده.", show_alert=True)
        return
    await cb.message.edit_text(
        "روی هر پلن بزن تا وارد/خارجِ این دسته بشه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:planset:"))
async def miniapp_plan_set(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    _, _, cat_id, plan_id = cb.data.split(":")
    cat_id, plan_id = int(cat_id), int(plan_id)
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        plan.miniapp_category_id = None if plan.miniapp_category_id == cat_id else cat_id
        await s.commit()
    await cb.answer("ثبت شد ✅")
    await miniapp_cat_plans(cb, bot)


def _rial(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:cattoggle:"))
async def miniapp_cat_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if cat and cat.bot_id == row.id:
            cat.active = not cat.active
            await s.commit()
    await miniapp_cats(cb, bot)


# === PATCH_CATEGORY_SALE_FULL_V1 ===
@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catfull:"))
async def miniapp_cat_full_toggle(cb: CallbackQuery, bot: Bot):
    """«ظرفیتِ فروش پر/آزاد» — رویِ ربات و مینی‌اپ هر دو اثر دارد."""
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if not cat or cat.bot_id != row.id:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        cat.sale_full = not cat.sale_full
        await s.commit()
    await miniapp_cat_detail(cb, bot)      # خودش cb.answer می‌زند؛ وضعیت روی برچسبِ دکمه دیده می‌شود


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:catdel:"))
async def miniapp_cat_delete(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    cat_id = int(cb.data.split(":")[2])
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    async with Session() as s:
        cat = await s.get(MiniAppCategory, cat_id)
        if cat and cat.bot_id == row.id:
            plans = (await s.scalars(select(VpnPlan).where(VpnPlan.miniapp_category_id == cat_id))).all()
            for p in plans:
                p.miniapp_category_id = None
            await s.delete(cat)
            await s.commit()
    await cb.answer("حذف شد ✅")
    await miniapp_cats(cb, bot)


# === PATCH_MINIAPP_BG_THEME_V1 ===
_BG_SWATCHES = [("⚫️ مشکی", "#000000"), ("🌑 زغالی", "#111114"),
                ("🌃 سرمه‌ای", "#0b1220"), ("🟤 قهوه‌ای تیره", "#1a1210"),
                ("⚪️ روشن", "#f2f3f5"), ("🩶 طوسی روشن", "#e6e8eb")]


async def _bg_label(bot_id: int) -> str:
    from tenant.shared.miniapp import _miniapp_bg
    cur = await _miniapp_bg(bot_id)
    if not cur:
        return "خودکار (طبقِ تمِ تلگرامِ مشتری)"
    for label, hexv in _BG_SWATCHES:
        if hexv.lower() == cur.lower():
            return f"{label} {cur}"
    return cur


@miniapp_admin_router.callback_query(F.data == "miniapp:bg")
async def miniapp_bg_start(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    from tenant.shared.miniapp import _miniapp_bg
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    cur = await _miniapp_bg(row.id)
    rows, line = [], []
    for label, hexv in _BG_SWATCHES:
        mark = "✅ " if hexv.lower() == (cur or "").lower() else ""
        line.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"miniapp:bgpick:{hexv}"))
        if len(line) == 2:
            rows.append(line); line = []
    if line: rows.append(line)
    rows.append([InlineKeyboardButton(text="✏️ رنگِ دلخواه (کدِ هگز)", callback_data="miniapp:bgcustom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="♻️ خودکار (طبقِ تمِ تلگرامِ مشتری)", callback_data="miniapp:bgpick:auto", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:panel", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "🌗 <b>تمِ اصلیِ مینی‌اپ</b>\n\nاین رنگ پس‌زمینه و کارت‌هایِ مینی‌اپ رو می‌سازه؛ "
        "رنگِ دکمه‌ها جداگانه از «🎨 رنگِ دکمه‌ها» تنظیم می‌شه. مثلاً پس‌زمینهٔ مشکی + دکمه‌هایِ قرمز.\n\n"
        f"الان: <code>{await _bg_label(row.id)}</code>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:bgpick:"))
async def miniapp_bg_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin, set_setting
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    hexv = cb.data.rsplit(":", 1)[1]
    if hexv == "auto":
        await set_setting(row.id, "miniapp_bg_color", ""); await cb.answer("✅ برگشت به حالتِ خودکار")
    else:
        await set_setting(row.id, "miniapp_bg_color", hexv); await cb.answer(f"✅ تم ذخیره شد: {hexv}")
    await miniapp_panel_entry(cb, bot)


@miniapp_admin_router.callback_query(F.data == "miniapp:bgcustom")
async def miniapp_bg_custom_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    await state.set_state(MiniAppFSM.bg_color)
    await cb.message.answer("✏️ کدِ هگزِ پس‌زمینهٔ دلخواهت رو بفرست (مثلاً <code>#000000</code>).", parse_mode="HTML")
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.bg_color)
async def miniapp_bg_custom_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, set_setting, _FakeCb
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if not raw.startswith("#"): raw = "#" + raw
    import re as _re
    if not _re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", raw):
        await msg.answer("❌ کدِ هگزِ نامعتبر. مثلاً <code>#000000</code>. دوباره بفرست:", parse_mode="HTML")
        await state.set_state(MiniAppFSM.bg_color); return
    await set_setting(row.id, "miniapp_bg_color", raw)
    await msg.answer(f"✅ تمِ اصلی ذخیره شد: <code>{raw}</code>", parse_mode="HTML")
    await miniapp_panel_entry(_FakeCb(msg), bot)


# ═════════ رنگِ ثانویه (کارت‌ها) ═════════
# === PATCH_MINIAPP_SECONDARY_COLOR_V1 ===
# تا امروز رنگِ کارت‌ها همیشه از رویِ پس‌زمینه ساخته می‌شد، پس ترکیب‌هایی
# مثلِ «پس‌زمینهٔ زغالی + کارت‌هایِ مشکیِ خالص» ممکن نبود.

_CARD_SWATCHES = [("⚫️ مشکی", "#000000"), ("🌑 زغالی", "#131316"),
                  ("🌘 خاکستریِ تیره", "#1c1c22"), ("🌃 سرمه‌ای", "#111b2e"),
                  ("🟤 قهوه‌ای تیره", "#1d1512"), ("⚪️ سفید", "#ffffff")]


async def _card_label(bot_id: int) -> str:
    from tenant.shared.miniapp import _miniapp_card
    cur = await _miniapp_card(bot_id)
    if not cur:
        return "خودکار (از رویِ تمِ اصلی)"
    for label, hexv in _CARD_SWATCHES:
        if hexv.lower() == cur.lower():
            return f"{label} {cur}"
    return cur


@miniapp_admin_router.callback_query(F.data == "miniapp:card")
async def miniapp_card_start(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    from tenant.shared.miniapp import _miniapp_card
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    cur = await _miniapp_card(row.id)
    rows, line = [], []
    for label, hexv in _CARD_SWATCHES:
        mark = "✅ " if hexv.lower() == (cur or "").lower() else ""
        line.append(InlineKeyboardButton(text=f"{mark}{label}",
                                         callback_data=f"miniapp:cardpick:{hexv}"))
        if len(line) == 2:
            rows.append(line); line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(text="✏️ رنگِ دلخواه (کدِ هگز)",
                                      callback_data="miniapp:cardcustom",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="♻️ خودکار (از رویِ تمِ اصلی)",
                                      callback_data="miniapp:cardpick:auto",
                                      style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="miniapp:panel",
                                      style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "🎨 <b>رنگِ ثانویه (کارت‌ها)</b>\n\n"
        "پس‌زمینهٔ کارت‌ها و باکس‌هایِ داخلِ مینی‌اپ. با این می‌تونی مثلاً "
        "پس‌زمینه رو زغالی و کارت‌ها رو مشکیِ خالص کنی.\n\n"
        "سه رنگ به‌صورتِ مستقل تنظیم می‌شن:\n"
        "• 🌗 تمِ اصلی → پس‌زمینه\n"
        "• 🎨 رنگِ ثانویه → کارت‌ها (همین‌جا)\n"
        "• 🎨 رنگِ دکمه‌ها → دکمه‌ها و هایلایت‌ها\n\n"
        f"الان: <code>{await _card_label(row.id)}</code>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@miniapp_admin_router.callback_query(F.data.startswith("miniapp:cardpick:"))
async def miniapp_card_pick(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin, set_setting
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    hexv = cb.data.rsplit(":", 1)[1]
    await set_setting(row.id, "miniapp_card_color", "" if hexv == "auto" else hexv)
    await cb.answer("✅ ذخیره شد" if hexv != "auto" else "♻️ برگشت به خودکار")
    await miniapp_card_start(cb, bot)


@miniapp_admin_router.callback_query(F.data == "miniapp:cardcustom")
async def miniapp_card_custom_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer(); return
    await state.set_state(MiniAppFSM.card_color)
    await cb.message.answer("✏️ کدِ هگزِ رنگِ کارت‌ها رو بفرست (مثلاً <code>#000000</code>).",
                            parse_mode="HTML")
    await cb.answer()


@miniapp_admin_router.message(MiniAppFSM.card_color)
async def miniapp_card_custom_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, set_setting, _FakeCb
    await state.clear()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if not raw.startswith("#"):
        raw = "#" + raw
    import re as _re
    if not _re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", raw):
        await msg.answer("❌ کدِ هگزِ نامعتبر. مثلاً <code>#000000</code>. دوباره بفرست:",
                         parse_mode="HTML")
        await state.set_state(MiniAppFSM.card_color)
        return
    await set_setting(row.id, "miniapp_card_color", raw)
    await msg.answer(f"✅ رنگِ ثانویه ذخیره شد: <code>{raw}</code>", parse_mode="HTML")
    await miniapp_panel_entry(_FakeCb(msg), bot)
