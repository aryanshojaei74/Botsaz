"""
platform_admin_menu.py — بخشِ «چیدمانِ پنل مدیریت» جدا از platform_bot.py.

=== PATCH_ADMIN_MENU_GROUPS_V1 ===
پنلِ مدیریت به ۲۰+ دکمهٔ تخت رسیده بود و پیدا کردنِ هر چیزی سخت شده بود.
این‌جا همان دکمه‌ها — با «دقیقاً همان callback_data»های قبلی — در شش
دستهٔ موضوعی گروه‌بندی می‌شوند:

    📊 آمار و مالی · 👥 کاربران و ربات‌ها · 💬 پشتیبانی و اطلاع‌رسانی
    🎉 سرگرمی و جامعه · ⚙️ تنظیمات و پلن‌ها · 🩺 سیستم و نگهداری

هیچ callback_data ای عوض نشده؛ فقط یک لایهٔ «adm:cat:<key>» بینِ صفحهٔ
اصلیِ پنل و خودِ دکمه‌ها اضافه شده. پس کیبوردهایِ قدیمی که همین حالا در
چتِ ادمین‌ها نشسته‌اند هم کار می‌کنند.

دسترسی: آیتم‌هایِ super_only فقط برایِ PLATFORM_ADMIN_ID دیده می‌شوند؛
بقیه برایِ ادمین‌های فرعی هم.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import PLATFORM_ADMIN_ID, Session
from platform_selfsaz import selfsaz_api
from premium_emoji import hx_button
from sqlalchemy import select
from models import PlatformTxn, PlatformUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

admin_menu_router = Router()


class _SelfSazAdminFSM(StatesGroup):
    price = State()


# ======================= نقشهٔ دسته‌ها =======================
# هر آیتم: (برچسب, callback_data, استایل, فقط‌مدیرِ‌اصلی)
# ترتیبِ این جدول = ترتیبِ نمایش. برایِ افزودنِ دکمهٔ تازه فقط یک سطر
# این‌جا اضافه کن — دیگر لازم نیست به admin_panel_kb دست بزنی.

ADMIN_SECTIONS: list[dict] = [
    {
        "key": "stats",
        "title": "📊 آمار و مالی",
        "desc": "آمار کلی، رسیدها، سفارش‌ها و نفراتِ برتر",
        "items": [
            ("📊 آمار کلی",            "adm:stats",   ButtonStyle.PRIMARY, False),
            ("💳 مالی و رسیدها",       "adm:fin",     ButtonStyle.PRIMARY, False),
            ("🛒 سفارش‌ها",            "adm:orders",  ButtonStyle.PRIMARY, False),
            ("🏆 نفرات برتر بات‌ساز",   "adm:ptc",     ButtonStyle.PRIMARY, True),
            # === PATCH_MENU_MOVE_V1 ===
            # «پرداخت و کیف پول» از دلِ «تنظیمات» به این‌جا منتقل شد؛
            # کنارِ رسیدها و سفارش‌ها جایِ طبیعی‌ترش است.
            ("💳 پرداخت و کیف پول",     "set:hub_pay", ButtonStyle.PRIMARY, True),
        ],
    },
    {
        "key": "users",
        "title": "👥 کاربران و ربات‌ها",
        "desc": "کاربران، ربات‌های فعال، بایگانی و بررسیِ دعوت‌ها",
        "items": [
            ("👥 کاربران",              "adm:users",    ButtonStyle.PRIMARY, False),
            ("🤖 ربات‌های فعال",         "adm:bots",     ButtonStyle.PRIMARY, False),
            ("🗄 بایگانی",              "adm:archive",  ButtonStyle.PRIMARY, False),
            ("🔎 بررسی دعوت‌های کاربر",  "adm:refcheck", ButtonStyle.PRIMARY, True),
            ("👑 ادمین‌های فرعی",        "adm:sub_admins", ButtonStyle.PRIMARY, True),
        ],
    },
    {
        "key": "support",
        "title": "💬 پشتیبانی و اطلاع‌رسانی",
        "desc": "تیکت‌ها، نظرات، پیام همگانی و تبلیغات",
        "items": [
            ("🎫 تیکت‌های کاربران",  "adm:tickets",   ButtonStyle.PRIMARY, False),
            ("📩 نظرات کاربران",     "adm:feedbacks", ButtonStyle.PRIMARY, False),
            ("📢 پیام همگانی",       "adm:broadcast", ButtonStyle.PRIMARY, True),
            ("📣 تبلیغ در ربات‌های رایگان", "adm:ad_free", ButtonStyle.PRIMARY, True),
            ("📢 اطلاع به صاحبانِ ربات‌هایِ «تبدیلِ فایل»",
             "adm:notify_filelink", ButtonStyle.PRIMARY, True),
        ],
    },
    {
        "key": "fun",
        "title": "🎉 سرگرمی و جامعه",
        "desc": "قرعه‌کشی، بازی و انجمن بات‌ساز",
        "items": [
            ("🎉 قرعه‌کشی کانال بات‌ساز", "raffle:menu",   ButtonStyle.PRIMARY, False),
            ("🎮 بازی‌ها و شرط‌بندی",   "pgw:hub",       ButtonStyle.SUCCESS, True),   # PATCH_PLATFORM_WAGER_V1
            ("🎲 بازی کن پول بگیر",      "padg:menu",     ButtonStyle.PRIMARY, False),
            ("🎃 تم هالوین (همهٔ ربات‌ها)", "adm:halloween", ButtonStyle.SUCCESS, True),   # PATCH_HALLOWEEN_V1
            ("🏛 انجمن بات‌ساز",         "adm:community", ButtonStyle.PRIMARY, True),
            ("🔎 Fun Stat (تست)",       "fst:adm",       ButtonStyle.PRIMARY, True),  # PATCH_FUNSTAT_V1
        ],
    },
    {
        "key": "settings",
        "title": "⚙️ تنظیمات و پلن‌ها",
        "desc": "تنظیمات پلتفرم، قابلیت‌های پلن‌ها و آموزش",
        "items": [
            ("⚙️ تنظیمات پلتفرم",   "adm:settings",      ButtonStyle.PRIMARY, True),
            ("🔧 قابلیت‌های پلن‌ها",  "adm:plan_features", ButtonStyle.PRIMARY, True),
            ("✏️ ویرایش آموزش",     "adm:tutorial",      ButtonStyle.SUCCESS, True),
            ("🧩 مدیریت سلف‌ساز",     "adm:selfsaz",       ButtonStyle.PRIMARY, True),  # SELFSaz_ADMIN_ITEM
            # === PATCH_PLATFORM_PREMIUM_EMOJI_V1 ===
            ("⭐ افزودن ایموجی پرمیوم (ربات اصلی)", "pemj:menu:0", ButtonStyle.PRIMARY, True),
            ("➕ افزودن استیکر", "pst:menu", ButtonStyle.SUCCESS, True),
        ],
    },
    {
        "key": "system",
        "title": "🩺 سیستم و نگهداری",
        "desc": "سلامتِ ربات‌ها، حالتِ آپدیت و محافظ گروه",
        "items": [
            ("🩺 بروزرسانی و سلامت ربات‌ها", "adm:health",      ButtonStyle.PRIMARY, True),
            ("🛡 محافظ گروه",               "adm:group_guard", ButtonStyle.PRIMARY, True),
            # «حالت آپدیت» برچسب/رنگش پویاست؛ در _section_items ساخته می‌شود.
        ],
    },
]

_SECTION_BY_KEY = {sec["key"]: sec for sec in ADMIN_SECTIONS}

# === PATCH_ADMIN_BACKNAV_V1 ===
# آخرین دسته‌ای که هر ادمین بازش کرده. چرا لازم است: صدها دکمهٔ «بازگشت»
# در کلِ پروژه به adm:back اشاره می‌کنند؛ با اضافه شدنِ لایهٔ دسته‌بندی،
# آن‌ها یک‌باره دو پله می‌پریدند (صفحه → ریشهٔ پنل). به‌جایِ دست‌زدن به
# همهٔ آن‌ها، خودِ adm:back یاد می‌گیرد که «یک پله عقب» یعنی برگشت به
# دسته‌ای که کاربر از آن آمده. حافظه‌ای درون‌پروسه‌ای و بی‌ضرر است: اگر
# خالی باشد (ری‌استارت)، دقیقاً همان رفتارِ قبلی یعنی ریشهٔ پنل.
_LAST_CAT: dict[int, str] = {}


def _update_mode_on() -> bool:
    """حالتِ آپدیت در platform_bot زندگی می‌کند؛ عمداً lazy خوانده می‌شود
    تا این ماژول به آن فایل وابستهٔ import-time نشود."""
    try:
        import platform_bot
        return bool(getattr(platform_bot, "_UPDATE_MODE", False))
    except Exception:
        logger.exception("خواندنِ حالتِ آپدیت ناموفق بود")
        return False


def _section_items(sec: dict, is_super: bool) -> list[tuple]:
    """آیتم‌هایِ قابلِ نمایشِ یک دسته برایِ این کاربر (+ آیتم‌هایِ پویا)."""
    items = [it for it in sec["items"] if is_super or not it[3]]
    if sec["key"] == "system" and is_super:
        on = _update_mode_on()
        items.append((
            ("🔴 حالت آپدیت: روشن" if on else "🟢 حالت آپدیت: خاموش"),
            "adm:update_mode",
            (ButtonStyle.DANGER if on else ButtonStyle.SUCCESS),
            True,
        ))
    return items


def admin_panel_kb(uid: int | None = None) -> InlineKeyboardMarkup:
    """صفحهٔ اولِ پنل مدیریت: فقط دسته‌ها + میان‌برهایِ پرکاربرد."""
    is_super = (uid == PLATFORM_ADMIN_ID)
    rows: list[list] = []

    for sec in ADMIN_SECTIONS:
        if not _section_items(sec, is_super):
            continue  # دسته‌ای که برایِ این کاربر خالی است اصلاً نشان داده نمی‌شود
        rows.append([hx_button(sec["title"], callback_data=f"adm:cat:{sec['key']}",
                               style=ButtonStyle.PRIMARY)])

    if is_super:
        # === PATCH_PARTNER_V1 === عمداً بیرونِ دسته‌ها و برجسته می‌ماند.
        rows.append([hx_button("🤝 ربات‌های همکار", callback_data="pa:home",
                               style=ButtonStyle.SUCCESS)])
        # === PATCH_VIP_IN_ROOT_PANEL_V1 ===
        # مثلِ «ربات‌های همکار» یک میان‌بُرِ ریشه‌ای است، نه زیرمجموعهٔ
        # دسته‌ها — چون روزانه استفاده می‌شود و باید یک تپ فاصله داشته باشد.
        rows.append([hx_button("⚡️ سرور فعال‌سازی ربات", callback_data="vip:menu",
                               style=ButtonStyle.PRIMARY)])
        rows.append([hx_button("🧩 مدیریت سلف‌ساز", callback_data="adm:selfsaz",
                               style=ButtonStyle.SUCCESS)])
        # === PATCH_HOMEBTNS_IN_ROOT_PANEL_V1 ===
        # مثلِ «ربات‌های همکار» و «سرور فعال‌سازی» یک میان‌بُرِ ریشه‌ای
        # است: تقریباً روزانه استفاده می‌شود و نباید دو تپ داخلِ دسته‌ها
        # پنهان باشد. نسخهٔ داخلِ دسته هم سرِ جایش می‌ماند.
        rows.append([hx_button("🏠 مدیریت دکمه‌های صفحه اصلی",
                               callback_data="adm:homebtns",
                               style=ButtonStyle.PRIMARY)])
        # === PATCH_MENU_MOVE_V1 ===
        rows.append([hx_button("🛍 فروشگاهِ پروژه‌ها (ربات آماده)",
                               callback_data="mka:hub",
                               style=ButtonStyle.SUCCESS)])
        # === PATCH_SFTP_IN_ROOT_PANEL_V1 ===
        rows.append([hx_button("🗂 مدیریت SFTP حرفه‌ای",
                               callback_data="adm:sftpvip",
                               style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def section_kb(key: str, uid: int | None = None) -> InlineKeyboardMarkup | None:
    sec = _SECTION_BY_KEY.get(key)
    if not sec:
        return None
    items = _section_items(sec, uid == PLATFORM_ADMIN_ID)
    if not items:
        return None
    rows = [[hx_button(label, callback_data=cbd, style=style)]
            for label, cbd, style, _sup in items]
    # ریشهٔ پنل — adm:root (نه adm:back، وگرنه همین‌جا حلقه می‌شد)
    rows.append([hx_button("⬅️ بازگشت به پنل", callback_data="adm:root",
                           style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def section_text(key: str) -> str:
    sec = _SECTION_BY_KEY.get(key) or {}
    return (f"{sec.get('title', '—')}\n\n"
            f"<i>{sec.get('desc', '')}</i>\n\n"
            "یکی را انتخاب کن:")


@admin_menu_router.callback_query(F.data.startswith("adm:cat:"))
async def adm_open_category(cb: CallbackQuery):
    """بازکردنِ یک دستهٔ پنل مدیریت."""
    from platform_bot import is_admin, has_perm  # lazy — جلوگیری از importِ حلقوی

    uid = cb.from_user.id
    # همان شرطی که خودِ ورودیِ پنل مدیریت دارد.
    if not (is_admin(uid) or await has_perm(uid, "adm:stats")):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    _LAST_CAT[uid] = key  # PATCH_ADMIN_BACKNAV_V1
    kb = section_kb(key, uid)
    if kb is None:
        await cb.answer("این بخش برایِ شما در دسترس نیست.", show_alert=True)
        return
    try:
        await cb.message.edit_text(section_text(key), reply_markup=kb)
    except Exception:
        await cb.message.answer(section_text(key), reply_markup=kb)
    await cb.answer()


@admin_menu_router.callback_query(F.data == "adm:root")
async def adm_root(cb: CallbackQuery):
    """=== PATCH_ADMIN_BACKNAV_V1 === ریشهٔ پنل مدیریت (لایهٔ دسته‌ها)."""
    from platform_bot import is_admin, has_perm

    uid = cb.from_user.id
    if not (is_admin(uid) or await has_perm(uid, "adm:stats")):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _LAST_CAT.pop(uid, None)
    txt = "🛠 <b>پنل مدیریت بات‌ساز</b>"
    kb = admin_panel_kb(uid)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


def back_target(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_ADMIN_BACKNAV_V1 === «یک پله عقب» برایِ adm:back:
    دسته‌ای که کاربر از آن آمده، وگرنه ریشهٔ پنل."""
    key = _LAST_CAT.get(uid)
    if key:
        kb = section_kb(key, uid)
        if kb is not None:
            return section_text(key), kb
    return "🛠 <b>پنل مدیریت بات‌ساز</b>", admin_panel_kb(uid)


# ======================= SELFSaz platform admin =======================
async def _render_selfsaz_admin(cb: CallbackQuery):
    cfg = await selfsaz_api("GET", "/v1/config")
    from platform_bot import get_pset
    _admin_only = (await get_pset("hbtn:self_saz:admin_only", "off")) == "on"
    if not cfg:
        text = "🧩 <b>مدیریت سلف‌ساز</b>\n\n⚠️ سرویس مستقل سلف‌ساز در دسترس نیست."
        kb = InlineKeyboardMarkup(inline_keyboard=[[hx_button("⬅️ بازگشت", callback_data="adm:cat:settings", style=ButtonStyle.DANGER)]])
    else:
        text = ("🧩 <b>مدیریت سلف‌ساز</b>\n\n"
                f"وضعیت سرویس: {'🟢 روشن' if cfg.get('enabled') else '🔴 خاموش'}\n"
                f"قیمت پیش‌فرض: {int(cfg.get('default_price', 0)):,} تومان")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("🔄 روشن/خاموش کردن", callback_data="sszg2:toggle", style=ButtonStyle.PRIMARY)],
            [hx_button("💰 تعیین قیمت پیش‌فرض", callback_data="sszg2:price", style=ButtonStyle.SUCCESS)],
            [hx_button("👥 مدیریت کاربران سلف‌ساز", callback_data="sszg3:users", style=ButtonStyle.PRIMARY)],
            [hx_button("📊 آمار و فروش", callback_data="sszg2:stats", style=ButtonStyle.PRIMARY),
             hx_button("🏷 تخفیف پلن‌ها", callback_data="sszg2:disc", style=ButtonStyle.PRIMARY)],
            [hx_button("🖼 افزودن/تغییر تصویر پنل", callback_data="sszg2:image", style=ButtonStyle.SUCCESS)],
            [hx_button(("🔒 SelfSaz: فقط من (حالت تست)" if _admin_only else "🌐 SelfSaz: نمایش برای کاربران"),
                       callback_data="sszg2:visibility",
                       style=(ButtonStyle.DANGER if _admin_only else ButtonStyle.SUCCESS))],
            [hx_button("⬅️ بازگشت", callback_data="adm:cat:settings", style=ButtonStyle.DANGER)],
        ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@admin_menu_router.callback_query(F.data == "adm:selfsaz")
async def admin_selfsaz(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await _render_selfsaz_admin(cb)
    await cb.answer()


@admin_menu_router.callback_query(F.data == "psa:toggle")
async def admin_selfsaz_toggle(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    result = await selfsaz_api("POST", "/v1/config/toggle")
    if not result:
        await cb.answer("سرویس مستقل در دسترس نیست", show_alert=True)
        return
    await _render_selfsaz_admin(cb)
    await cb.answer("وضعیت تغییر کرد")


@admin_menu_router.callback_query(F.data == "psa:price")
async def admin_selfsaz_price_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(_SelfSazAdminFSM.price)
    await cb.message.answer("قیمت پیش‌فرض سلف‌ساز را به تومان و فقط به‌صورت عدد بفرستید:")
    await cb.answer()


@admin_menu_router.message(_SelfSazAdminFSM.price, F.text)
async def admin_selfsaz_price_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    value = (msg.text or "").strip()
    if not value.isdigit():
        await msg.answer("فقط عدد معتبر بفرستید.")
        return
    result = await selfsaz_api("PUT", "/v1/config/price", {"price": int(value)})
    await state.clear()
    await msg.answer("✅ قیمت در سرویس مستقل ذخیره شد." if result else "⚠️ سرویس مستقل در دسترس نیست.")


# SELFSaz_ADMIN_FIX_V2 — isolated price flow, independent of older handlers
class _SelfSazPriceFixFSM(StatesGroup):
    value = State()


class _SelfSazImageFSM(StatesGroup):
    photo = State()


@admin_menu_router.callback_query(F.data == 'sszg2:image')
async def selfsaz_admin_image_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer('فقط مدیر اصلی', show_alert=True)
        return
    await state.set_state(_SelfSazImageFSM.photo)
    await cb.message.answer('🖼 لطفاً تصویر هدر پنل سلف‌ساز را به‌صورت عکس ارسال کنید.\n\nاین تصویر برای همهٔ کاربران نمایش داده می‌شود.')
    await cb.answer()


@admin_menu_router.message(_SelfSazImageFSM.photo, F.photo)
async def selfsaz_admin_image_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    file_id = msg.photo[-1].file_id
    from platform_bot import set_pset
    await set_pset('selfsaz:panel_image_file_id', file_id)
    await state.clear()
    await msg.answer('✅ تصویر پنل سلف‌ساز ذخیره شد و از این پس برای همهٔ کاربران نمایش داده می‌شود.')


@admin_menu_router.message(_SelfSazImageFSM.photo)
async def selfsaz_admin_image_invalid(msg: Message):
    if msg.from_user.id == PLATFORM_ADMIN_ID:
        await msg.answer('⚠️ لطفاً فقط یک عکس ارسال کنید.')


@admin_menu_router.callback_query(F.data == "sszg2:toggle")
async def selfsaz_admin_toggle_fix(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    result = await selfsaz_api("POST", "/v1/config/toggle")
    await cb.answer("وضعیت تغییر کرد" if result else "سرویس مستقل در دسترس نیست", show_alert=not bool(result))
    if result:
        await _render_selfsaz_admin(cb)


@admin_menu_router.callback_query(F.data == "sszg2:visibility")
async def selfsaz_admin_visibility(cb: CallbackQuery):
    """دکمهٔ «حالت تست / نمایش برای کاربران» در پنلِ سلف‌ساز هندلر نداشت و
    کاری نمی‌کرد؛ همان کلیدِ hbtn:self_saz:admin_only را جابه‌جا می‌کند."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    from platform_bot import get_pset, set_pset, refresh_home_cfg
    cur = await get_pset("hbtn:self_saz:admin_only", "off")
    new = "off" if cur == "on" else "on"
    await set_pset("hbtn:self_saz:admin_only", new)
    await refresh_home_cfg()
    await cb.answer("✅ حالت تست SelfSaz " + ("فعال شد؛ فقط شما می‌بینید" if new == "on"
                                            else "غیرفعال شد؛ برای کاربران نمایش داده می‌شود"),
                    show_alert=True)
    await _render_selfsaz_admin(cb)


@admin_menu_router.callback_query(F.data == "sszg2:price")
async def selfsaz_admin_price_fix_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.clear()
    await state.set_state(_SelfSazPriceFixFSM.value)
    await cb.message.answer("قیمت پیش‌فرض سلف‌ساز را فقط به‌صورت عدد بفرستید:")
    await cb.answer()


@admin_menu_router.message(_SelfSazPriceFixFSM.value, F.text)
async def selfsaz_admin_price_fix_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    raw = (msg.text or "").strip().replace(",", "").replace("٬", "")
    if not raw.isdigit() or int(raw) < 0:
        await msg.answer("لطفاً فقط یک عدد معتبر به تومان بفرستید.")
        return
    result = await selfsaz_api("PUT", "/v1/config/price", {"price": int(raw)})
    if not result:
        await msg.answer("⚠️ سرویس مستقل در دسترس نیست یا قیمت ذخیره نشد.")
        return
    await state.clear()
    await msg.answer(f"✅ قیمت پیش‌فرض سلف‌ساز روی {int(raw):,} تومان تنظیم شد.")


# ---- SelfSaz pro: آمار و تخفیف پلن‌ها ----
class _SelfSazDiscFSM(StatesGroup):
    value = State()


@admin_menu_router.callback_query(F.data == "sszg2:stats")
async def selfsaz_admin_stats(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    from datetime import datetime, timezone
    from platform_selfsaz_pro import compute_stats, revenue, stats_text
    res = await selfsaz_api("GET", "/v1/admin/accounts")
    if res is None:
        await cb.answer("سرویس مستقل در دسترس نیست", show_alert=True)
        return
    st = compute_stats(res.get("accounts", []), datetime.now(timezone.utc))
    r30, rall, c30 = await revenue()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🔄 بروزرسانی", callback_data="sszg2:stats", style=ButtonStyle.PRIMARY)],
        [hx_button("⬅️ بازگشت", callback_data="adm:selfsaz", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(stats_text(st, r30, rall, c30), reply_markup=kb)
    except Exception:
        await cb.message.answer(stats_text(st, r30, rall, c30), reply_markup=kb)
    await cb.answer()


@admin_menu_router.callback_query(F.data == "sszg2:disc")
async def selfsaz_admin_disc_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    from platform_bot import get_pset
    from platform_selfsaz_pro import DISC_KEY, parse_discounts
    cur = parse_discounts(await get_pset(DISC_KEY, ""))
    await state.clear()
    await state.set_state(_SelfSazDiscFSM.value)
    await cb.message.answer(
        "🏷 <b>تخفیف پلن‌ها</b>\n\n"
        f"فعلی: ۳ ماهه {cur[3]}٪ · ۶ ماهه {cur[6]}٪ · ۱۲ ماهه {cur[12]}٪\n\n"
        "درصدها را به‌صورت «سه‌ماهه ششم‌اهه دوازده‌ماهه» بفرستید، مثلاً: <code>5 10 15</code>\n"
        "(سقف ۹۰٪؛ پلن یک‌ماهه همیشه بدون تخفیف است)")
    await cb.answer()


@admin_menu_router.message(_SelfSazDiscFSM.value, F.text)
async def selfsaz_admin_disc_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    parts = (msg.text or "").replace("٪", " ").replace("%", " ").replace("،", " ").replace(",", " ").split()
    try:
        a, b, c = (int(x) for x in parts)
        if not all(0 <= v <= 90 for v in (a, b, c)):
            raise ValueError
    except ValueError:
        await msg.answer("سه عدد بین ۰ تا ۹۰ بفرستید؛ مثلاً: <code>5 10 15</code>")
        return
    from platform_bot import set_pset
    from platform_selfsaz_pro import DISC_KEY
    await set_pset(DISC_KEY, f"3:{a},6:{b},12:{c}")
    await state.clear()
    await msg.answer(f"✅ تخفیف‌ها ذخیره شد: ۳ ماهه {a}٪ · ۶ ماهه {b}٪ · ۱۲ ماهه {c}٪")


# SELFSaz_ADMIN_USERS_V1 — پنل ایزولهٔ کاربران خریداری‌کرده/متصل‌شده
class _SelfSazPriceV3FSM(StatesGroup):
    value = State()


@admin_menu_router.callback_query(F.data == "sszg3:price")
async def selfsaz_admin_price_v3_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True); return
    await state.clear(); await state.set_state(_SelfSazPriceV3FSM.value)
    await cb.message.answer("قیمت پیش‌فرض سلف‌ساز را فقط به‌صورت عدد به تومان بفرستید:")
    await cb.answer()


@admin_menu_router.message(_SelfSazPriceV3FSM.value, F.text)
async def selfsaz_admin_price_v3_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    raw = (msg.text or "").strip().replace(",", "").replace("٬", "")
    if not raw.isdigit():
        await msg.answer("لطفاً فقط عدد معتبر به تومان بفرستید.")
        return
    result = await selfsaz_api("PUT", "/v1/config/price", {"price": int(raw)})
    if not result:
        await msg.answer("⚠️ سرویس مستقل در دسترس نیست یا قیمت ذخیره نشد.")
        return
    await state.clear(); await msg.answer(f"✅ قیمت پیش‌فرض روی {int(raw):,} تومان ذخیره شد.")


class _SelfSazUsersFSM(StatesGroup):
    balance = State()
    days = State()
    message = State()


def _ss_user_kb(rows):
    keys = []
    for r in rows:
        status = "🟢" if r.get("enabled") else "🔴"
        exp = (r.get("expires_at") or "بدون تاریخ")[:10]
        keys.append([hx_button(f"{status} {r.get('owner_id')} · {r.get('status','—')} · {exp}", callback_data=f"sszg3:user:{r['id']}")])
    keys.append([hx_button("🧾 رسیدهای سلف‌ساز", callback_data="sszg3:receipts")])
    keys.append([hx_button("⬅️ بازگشت", callback_data="adm:selfsaz", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=keys)


@admin_menu_router.callback_query(F.data == "sszg3:users")
async def selfsaz_admin_users(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True); return
    rows = await selfsaz_api("GET", "/v1/admin/accounts")
    data = (rows or {}).get("accounts", [])
    await cb.message.edit_text(f"👥 <b>مدیریت کاربران سلف‌ساز</b>\n\nتعداد حساب‌ها: {len(data)}\nروی هر کاربر بزنید:", reply_markup=_ss_user_kb(data))
    await cb.answer()


@admin_menu_router.callback_query(F.data.startswith("sszg3:user:"))
async def selfsaz_admin_user_detail(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True); return
    aid = int(cb.data.rsplit(":", 1)[1]); data = await selfsaz_api("GET", "/v1/admin/accounts") or {}
    row = next((x for x in data.get("accounts", []) if int(x["id"]) == aid), None)
    if not row: await cb.answer("کاربر پیدا نشد", show_alert=True); return
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == int(row["owner_id"])))
        bal = int(float(u.balance or 0)) if u else 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("⏹ غیرفعال‌کردن" if row.get("enabled") else "▶️ فعال‌کردن"), callback_data=f"sszg3:toggle:{aid}", style=ButtonStyle.PRIMARY)],
        [hx_button("💰 افزایش/کسر موجودی", callback_data=f"sszg3:balance:{aid}", style=ButtonStyle.SUCCESS)],
        [hx_button("📅 افزایش/کاهش روز", callback_data=f"sszg3:days:{aid}", style=ButtonStyle.PRIMARY)],
        [hx_button("✉️ ارسال پیام", callback_data=f"sszg3:message:{row['owner_id']}", style=ButtonStyle.PRIMARY)],
        [hx_button("🗑 حذف کاربر", callback_data=f"sszg3:delete:{aid}", style=ButtonStyle.DANGER)],
        [hx_button("⬅️ فهرست کاربران", callback_data="sszg3:users", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(f"👤 <b>کاربر {row['owner_id']}</b>\n\nوضعیت: {row.get('status')}\nفعال: {'بله' if row.get('enabled') else 'خیر'}\nقیمت: {int(row.get('price',0)):,}\nموجودی کیف پول: {bal:,}\nانقضا: {row.get('expires_at') or '—'}", reply_markup=kb)
    await cb.answer()


@admin_menu_router.callback_query(F.data.startswith("sszg3:delete:"))
async def selfsaz_admin_delete_start(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True); return
    aid=int(cb.data.rsplit(":",1)[1]); data=await selfsaz_api("GET", "/v1/admin/accounts") or {}
    row=next((x for x in data.get("accounts",[]) if int(x["id"])==aid),None)
    if not row: await cb.answer("کاربر پیدا نشد", show_alert=True); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[hx_button("⚠️ بله، حذف کامل کاربر", callback_data=f"sszg3:delete_confirm:{aid}", style=ButtonStyle.DANGER)],[hx_button("↩️ انصراف", callback_data=f"sszg3:user:{aid}", style=ButtonStyle.PRIMARY)]])
    await cb.message.edit_text(f"⚠️ <b>حذف کاربر SelfSaz</b>\n\nشناسه کاربر: <code>{row['owner_id']}</code>\n\nبا تأیید، حساب SelfSaz، نشست فعال و رسیدهای مرتبط حذف می‌شود. کیف پول اصلی کاربر حذف نمی‌شود.\n\nآیا مطمئن هستید؟", reply_markup=kb); await cb.answer()

@admin_menu_router.callback_query(F.data.startswith("sszg3:delete_confirm:"))
async def selfsaz_admin_delete_confirm(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True); return
    aid=int(cb.data.rsplit(":",1)[1]); result=await selfsaz_api("DELETE", f"/v1/admin/accounts/{aid}")
    if not result: await cb.answer("حذف انجام نشد؛ سرویس SelfSaz در دسترس نیست", show_alert=True); return
    await cb.message.edit_text(f"🗑 <b>کاربر SelfSaz حذف شد.</b>\n\nشناسه حساب: <code>{aid}</code>\nنشست و رسیدهای مرتبط حذف شدند و کیف پول اصلی دست‌نخورده باقی ماند.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[hx_button("👥 بازگشت به فهرست کاربران", callback_data="sszg3:users", style=ButtonStyle.PRIMARY)],[hx_button("⬅️ بازگشت", callback_data="adm:selfsaz", style=ButtonStyle.DANGER)]])); await cb.answer("کاربر حذف شد")


@admin_menu_router.callback_query(F.data.startswith("sszg3:toggle:"))
async def selfsaz_admin_toggle_user(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    aid = int(cb.data.rsplit(":",1)[1]); result = await selfsaz_api("POST", f"/v1/admin/accounts/{aid}/toggle")
    await cb.answer("وضعیت تغییر کرد" if result else "سرویس در دسترس نیست", show_alert=not bool(result))
    if result: cb.data = f"sszg3:user:{aid}"; await selfsaz_admin_user_detail(cb)


@admin_menu_router.callback_query(F.data.startswith("sszg3:balance:"))
async def selfsaz_admin_balance_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    await state.update_data(ss_account_id=int(cb.data.rsplit(":",1)[1])); await state.set_state(_SelfSazUsersFSM.balance)
    await cb.message.answer("مبلغ را وارد کنید؛ برای کسر موجودی عدد منفی بفرستید، مثل -50000:"); await cb.answer()


@admin_menu_router.message(_SelfSazUsersFSM.balance, F.text)
async def selfsaz_admin_balance_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    data = await state.get_data(); raw = (msg.text or '').replace(',','').replace('٬','').strip()
    try: delta = int(raw)
    except ValueError: await msg.answer("فقط عدد معتبر بفرستید."); return
    async with Session() as s:
        # resolve exact owner from account list (خطِ قبلیِ [0] رویِ فهرستِ خالی IndexError می‌داد)
        rows = (await selfsaz_api('GET', '/v1/admin/accounts') or {}).get('accounts', [])
        r = next((x for x in rows if int(x['id']) == int(data['ss_account_id'])), None)
        u = await s.scalar(select(PlatformUser).where(
            PlatformUser.telegram_id == int(r['owner_id'])).with_for_update()) if r else None
        if not u: await msg.answer('کاربر کیف پول پیدا نشد.'); return
        u.balance = float(u.balance or 0) + delta
        s.add(PlatformTxn(user_telegram_id=u.telegram_id, amount=delta, kind='admin', note='SelfSaz admin adjustment'))
        await s.commit(); new_bal = int(float(u.balance or 0))
    await state.clear(); await msg.answer(f'✅ موجودی اصلاح شد. موجودی جدید: {new_bal:,}')


@admin_menu_router.callback_query(F.data.startswith("sszg3:days:"))
async def selfsaz_admin_days_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    await state.update_data(ss_account_id=int(cb.data.rsplit(":",1)[1])); await state.set_state(_SelfSazUsersFSM.days)
    await cb.message.answer("تعداد روز را وارد کنید؛ برای کاهش روز عدد منفی بفرستید:"); await cb.answer()


@admin_menu_router.message(_SelfSazUsersFSM.days, F.text)
async def selfsaz_admin_days_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    data = await state.get_data(); raw = (msg.text or '').strip()
    try: days = int(raw)
    except ValueError: await msg.answer('فقط عدد معتبر بفرستید.'); return
    result = await selfsaz_api('PUT', f"/v1/admin/accounts/{int(data['ss_account_id'])}/days", {'days': days, 'admin_id': msg.from_user.id})
    await state.clear(); await msg.answer('✅ روزها اصلاح شد.' if result else '⚠️ انجام نشد.')


@admin_menu_router.callback_query(F.data.startswith("sszg3:message:"))
async def selfsaz_admin_message_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    await state.update_data(ss_owner_id=int(cb.data.rsplit(":",1)[1])); await state.set_state(_SelfSazUsersFSM.message)
    await cb.message.answer('متن پیام را بفرستید:'); await cb.answer()


@admin_menu_router.message(_SelfSazUsersFSM.message, F.text)
async def selfsaz_admin_message_save(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    data = await state.get_data()
    try: await bot.send_message(int(data['ss_owner_id']), msg.text)
    except Exception: await msg.answer('⚠️ ارسال پیام ناموفق بود.'); await state.clear(); return
    await state.clear(); await msg.answer('✅ پیام ارسال شد.')


@admin_menu_router.callback_query(F.data == "sszg3:receipts")
async def selfsaz_admin_receipts(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    data = await selfsaz_api('GET', '/v1/admin/receipts') or {}; rows = data.get('receipts', [])
    text = '🧾 <b>رسیدهای سلف‌ساز</b>\n\n' + ('\n'.join(f"#{r['id']} · کاربر {r['owner_id']} · {r['amount']:,} · {r['status']}" for r in rows) if rows else 'هنوز رسیدی ثبت نشده است.')
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[hx_button('⬅️ کاربران', callback_data='sszg3:users', style=ButtonStyle.DANGER)]])); await cb.answer()


# SELFSaz_ADMIN_UI_FINAL_V1
class _SelfSazFinalPriceFSM(StatesGroup):
    value = State()
class _SelfSazFinalUserFSM(StatesGroup):
    balance = State()
    days = State()
    message = State()

@admin_menu_router.callback_query(F.data == "ssfinal:price")
async def selfsaz_final_price_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer("فقط مدیر اصلی", show_alert=True); return
    await state.clear(); await state.set_state(_SelfSazFinalPriceFSM.value)
    await cb.message.answer("قیمت پیش‌فرض سلف‌ساز را فقط به‌صورت عدد به تومان بفرستید:"); await cb.answer()

@admin_menu_router.message(_SelfSazFinalPriceFSM.value, F.text)
async def selfsaz_final_price_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    raw = (msg.text or '').strip().replace(',', '').replace('٬', '')
    if not raw.isdigit(): await msg.answer('فقط عدد معتبر بفرستید.'); return
    result = await selfsaz_api('PUT', '/v1/config/price', {'price': int(raw)})
    if result:
        await state.clear(); await msg.answer(f'✅ قیمت {int(raw):,} تومان ذخیره شد.')
    else:
        await msg.answer('⚠️ سرویس سلف‌ساز در دسترس نیست؛ قیمت ذخیره نشد.')


def _ssfinal_users_kb(rows):
    rows_kb = []
    for r in rows:
        icon = '🟢' if r.get('enabled') else '🔴'
        rows_kb.append([hx_button(f"{icon} {r.get('owner_id')} · {r.get('status','—')}", callback_data=f"ssfinal:user:{r['id']}")])
    rows_kb.append([hx_button('🧾 رسیدهای سلف‌ساز', callback_data='ssfinal:receipts')])
    rows_kb.append([hx_button('⬅️ بازگشت', callback_data='adm:selfsaz', style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows_kb)

@admin_menu_router.callback_query(F.data == 'ssfinal:users')
async def selfsaz_final_users(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    data = await selfsaz_api('GET', '/v1/admin/accounts') or {}; rows = data.get('accounts', [])
    await cb.message.edit_text(f'👥 <b>مدیریت کاربران سلف‌ساز</b>\n\nتعداد: {len(rows)}\nکاربر را انتخاب کنید:', reply_markup=_ssfinal_users_kb(rows)); await cb.answer()

@admin_menu_router.callback_query(F.data.startswith('ssfinal:user:'))
async def selfsaz_final_user_detail(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    aid = int(cb.data.rsplit(':', 1)[1]); data = await selfsaz_api('GET', '/v1/admin/accounts') or {}
    row = next((x for x in data.get('accounts', []) if int(x['id']) == aid), None)
    if not row: await cb.answer('کاربر پیدا نشد', show_alert=True); return
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == int(row['owner_id'])))
        bal = int(float(u.balance or 0)) if u else 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button('⏹ غیرفعال‌کردن' if row.get('enabled') else '▶️ فعال‌کردن', callback_data=f'ssfinal:toggle:{aid}', style=ButtonStyle.PRIMARY)],
        [hx_button('💰 افزایش/کسر موجودی', callback_data=f'ssfinal:balance:{aid}', style=ButtonStyle.SUCCESS)],
        [hx_button('📅 افزایش/کاهش روز', callback_data=f'ssfinal:days:{aid}', style=ButtonStyle.PRIMARY)],
        [hx_button('✉️ ارسال پیام', callback_data=f"ssfinal:message:{row['owner_id']}", style=ButtonStyle.PRIMARY)],
        [hx_button('⬅️ کاربران', callback_data='ssfinal:users', style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(f"👤 <b>{row['owner_id']}</b>\n\nوضعیت: {row.get('status')}\nفعال: {'بله' if row.get('enabled') else 'خیر'}\nموجودی: {bal:,}\nانقضا: {row.get('expires_at') or '—'}", reply_markup=kb); await cb.answer()

@admin_menu_router.callback_query(F.data.startswith('ssfinal:toggle:'))
async def selfsaz_final_toggle(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    aid = int(cb.data.rsplit(':', 1)[1]); result = await selfsaz_api('POST', f'/v1/admin/accounts/{aid}/toggle')
    await cb.answer('وضعیت تغییر کرد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result: cb.data = f'ssfinal:user:{aid}'; await selfsaz_final_user_detail(cb)

@admin_menu_router.callback_query(F.data.startswith('ssfinal:balance:'))
async def selfsaz_final_balance_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    await state.update_data(ssfinal_account=int(cb.data.rsplit(':', 1)[1])); await state.set_state(_SelfSazFinalUserFSM.balance)
    await cb.message.answer('مبلغ را بفرستید؛ برای کسر موجودی عدد منفی بفرستید:'); await cb.answer()

@admin_menu_router.message(_SelfSazFinalUserFSM.balance, F.text)
async def selfsaz_final_balance_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    d = await state.get_data(); raw = (msg.text or '').replace(',', '').replace('٬', '').strip()
    try: delta = int(raw)
    except ValueError: await msg.answer('فقط عدد معتبر بفرستید.'); return
    rows = (await selfsaz_api('GET', '/v1/admin/accounts') or {}).get('accounts', []); r = next((x for x in rows if int(x['id']) == int(d['ssfinal_account'])), None)
    if not r: await msg.answer('کاربر پیدا نشد.'); return
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == int(r['owner_id'])))
        if not u: await msg.answer('کاربر کیف پول پیدا نشد.'); return
        u.balance = float(u.balance or 0) + delta; s.add(PlatformTxn(user_telegram_id=u.telegram_id, amount=delta, kind='admin', note='SelfSaz admin adjustment')); await s.commit(); bal = int(float(u.balance or 0))
    await state.clear(); await msg.answer(f'✅ موجودی جدید: {bal:,}')

@admin_menu_router.callback_query(F.data.startswith('ssfinal:days:'))
async def selfsaz_final_days_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    await state.update_data(ssfinal_account=int(cb.data.rsplit(':', 1)[1])); await state.set_state(_SelfSazFinalUserFSM.days)
    await cb.message.answer('تعداد روز را بفرستید؛ برای کاهش عدد منفی بفرستید:'); await cb.answer()

@admin_menu_router.message(_SelfSazFinalUserFSM.days, F.text)
async def selfsaz_final_days_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    d = await state.get_data()
    try: days = int((msg.text or '').strip())
    except ValueError: await msg.answer('فقط عدد معتبر بفرستید.'); return
    result = await selfsaz_api('PUT', f"/v1/admin/accounts/{int(d['ssfinal_account'])}/days", {'days': days, 'admin_id': msg.from_user.id})
    await state.clear(); await msg.answer('✅ روزها اصلاح شد.' if result else '⚠️ انجام نشد.')

@admin_menu_router.callback_query(F.data.startswith('ssfinal:message:'))
async def selfsaz_final_message_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    await state.update_data(ssfinal_owner=int(cb.data.rsplit(':', 1)[1])); await state.set_state(_SelfSazFinalUserFSM.message)
    await cb.message.answer('متن پیام را بفرستید:'); await cb.answer()

@admin_menu_router.message(_SelfSazFinalUserFSM.message, F.text)
async def selfsaz_final_message_save(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID: return
    d = await state.get_data()
    try: await bot.send_message(int(d['ssfinal_owner']), msg.text)
    except Exception: await msg.answer('⚠️ ارسال پیام ناموفق بود.'); await state.clear(); return
    await state.clear(); await msg.answer('✅ پیام ارسال شد.')

@admin_menu_router.callback_query(F.data == 'ssfinal:receipts')
async def selfsaz_final_receipts(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID: await cb.answer('فقط مدیر اصلی', show_alert=True); return
    data = await selfsaz_api('GET', '/v1/admin/receipts') or {}; rows = data.get('receipts', [])
    body = '\n'.join(f"#{r['id']} · کاربر {r['owner_id']} · {r['amount']:,} · {r['status']}" for r in rows) or 'هنوز رسیدی ثبت نشده است.'
    await cb.message.edit_text('🧾 <b>رسیدهای سلف‌ساز</b>\n\n' + body, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[hx_button('⬅️ کاربران', callback_data='ssfinal:users', style=ButtonStyle.DANGER)]])); await cb.answer()
