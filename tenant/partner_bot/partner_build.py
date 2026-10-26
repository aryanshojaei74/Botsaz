"""
tenant/partner_build.py — ویزاردِ چیدنِ صفحهٔ اصلی، داخلِ خودِ رباتِ همکار.

=== PATCH_PARTNER_V22 ===
بازنویسی‌شده تا دقیقاً همان تجربهٔ بات‌سازِ اصلی را بدهد:

    ۱. شبکهٔ ۵×۳ — کاربر خانه‌به‌خانه انتخاب می‌کند (سبز=دکمه، قرمز=خالی)
    ۲. نام‌گذاریِ شیشه‌ای — روی هر دکمه می‌زند و اسمش را می‌گذارد،
       با پیش‌نمایشِ واقعیِ چیدمان
    ۳. تعیینِ عملکردِ هر دکمه

نسخهٔ اولِ این فایل شش الگویِ ثابت داشت که محدودکننده بود. این نسخه
همان آزادیِ بات‌ساز را می‌دهد ولی مستقل است، چون سرویسِ مستأجر حق
ندارد platform_bot.py را import کند.

تفاوتِ عمدی با نسخهٔ اصلی: فهرستِ عملکردها کوتاه‌تر است — فقط
چیزهایی که در یک رباتِ فروخته‌شده واقعاً هندلر دارند.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, func

from config import Session
from models import Bot as BotRow, MainButton

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_build_router = Router()

GRID_ROWS = 5
GRID_COLS = 3

FIELD_INTRO = (
    "🤖 <b>ربات تو قرار است چه‌کار کند؟</b>\n\n"
    "بر اساس انتخابت، عملکردهای مناسب همان کار به تو پیشنهاد می‌شود.\n"
    "بعداً هم می‌توانی عوضش کنی."
)

GRID_INTRO = (
    "🎨 <b>چیدمان دکمه‌های صفحهٔ اصلی</b>\n\n"
    "روی هر خانه بزن تا سبز شود (دکمه) یا قرمز بماند (خالی).\n"
    "تعداد خانه‌های سبزِ هر ردیف = تعداد دکمه‌های همان ردیف.\n"
    "ردیف‌هایی که هیچ خانهٔ سبزی ندارند نادیده گرفته می‌شوند.\n\n"
    "مثال: ردیف ۱ یک خانهٔ سبز ← یک دکمهٔ تمام‌عرض،\n"
    "ردیف ۲ دو خانهٔ سبز ← دو دکمه کنار هم.\n\n"
    "وقتی تمام شد «✅ تأیید و ادامه» را بزن."
)

# === PATCH_PARTNER_V24 ===
# زمینهٔ ربات — درست مثلِ بات‌سازِ اصلی، کاربر اول انتخاب می‌کند رباتش
# قرار است چه‌کار کند، و همان تعیین می‌کند چه عملکردهایی در دسترس باشد.
FIELDS: list[tuple[str, str]] = [
    ("vpn_sales",       "🛡 فروش VPN / کانفیگ"),
    ("digital_shop",    "🛒 فروشگاه دیجیتال / دوره آموزشی"),
    ("virtual_service", "🤝 خدمات مجازی تلگرام"),
    ("ad_bot",          "📢 ربات تبلیغاتی"),
    # === PATCH_PARTNER_COMMUNITY_MGMT_V1 ===
    # قبلاً این نوعِ ربات فقط در جریانِ ساختِ ربات در خودِ بات‌سازِ اصلی
    # اضافه شده بود؛ ربات‌هایِ همکار فهرستِ زمینه‌هایِ خودشان را از همین
    # لیستِ جداگانه می‌خوانند، پس بدونِ این خط اصلاً برایِ مشتریِ همکار
    # نمایش داده نمی‌شد.
    ("community_mgmt",  "🛡 مدیریت گروه و کانال"),
]
FIELD_LABEL = dict(FIELDS)

# === PATCH_LOCK_TO_THREE_FIELDS_V1 ===
# دقیقاً هم‌راستا با قفلِ خودِ بات‌سازِ اصلی (platform_bot.py): از این به
# بعد ربات‌هایِ *جدیدی* که از داخلِ ربات‌هایِ همکار ساخته می‌شوند هم فقط
# با یکی از این سه زمینه قابلِ‌ساختن‌اند. FIELDS/FIELD_LABEL دست‌نخورده
# می‌مانند (برایِ نمایشِ درستِ نامِ ربات‌هایی که قبلاً با digital_shop یا
# community_mgmt ساخته شده‌اند)؛ فقط allowed_fields() (که هم دکمه‌های
# انتخاب و هم اعتبارسنجیِ سمتِ سرور را کنترل می‌کند) پایین‌تر با همین
# مجموعه فیلتر می‌شود.
ACTIVE_FIELDS = {"vpn_sales", "virtual_service", "ad_bot"}


# === PATCH_PARTNER_SALE_FIELDS_V1 ===
# مالکِ رباتِ همکار می‌تواند انتخاب کند کدام «نوعِ ربات» به مشتری‌هایش
# نمایش داده شود (تنظیماتِ همکار → «🤖 انتخابِ ربات‌ها جهت فروش»).
# مقدار در Setting با کلیدِ زیر ذخیره می‌شود؛ خالی/غایب یعنی «همه» —
# پس رفتارِ ربات‌های موجود بدونِ هیچ مهاجرتی مثلِ قبل می‌ماند.
SALE_FIELDS_KEY = "partner_sale_fields"


async def allowed_fields(bot_id: int) -> list[tuple[str, str]]:
    """نوع‌هایِ رباتِ مجاز برایِ فروش در این رباتِ همکار."""
    from models import Setting
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == SALE_FIELDS_KEY))
    raw = (row.value if row else "") or ""
    picked = {k.strip() for k in raw.split(",") if k.strip()}
    # === PATCH_LOCK_TO_THREE_FIELDS_V1 ===
    # حتی اگه مالکِ رباتِ همکار قبلاً یه زمینهٔ الان‌قفل‌شده رو تو تنظیماتِ
    # خودش انتخاب کرده باشه، اینجا فیلتر می‌شه — قفلِ سراسری همیشه اولویت داره.
    picked &= ACTIVE_FIELDS
    out = [(k, lbl) for k, lbl in FIELDS if k in picked]
    # اگر چیزی انتخاب نشده یا انتخاب‌ها بی‌اعتبار شده‌اند، همهٔ زمینه‌هایِ
    # *فعال* را نشان بده (نه کلِ FIELDS، که شاملِ نوع‌هایِ قفل‌شده هم می‌شود).
    return out or [(k, lbl) for k, lbl in FIELDS if k in ACTIVE_FIELDS]


async def set_allowed_fields(bot_id: int, keys: set[str]) -> None:
    from models import Setting
    value = ",".join(k for k, _ in FIELDS if k in keys)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == SALE_FIELDS_KEY))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=SALE_FIELDS_KEY, value=value))
        await s.commit()


async def field_allowed(bot_id: int, field: str) -> bool:
    return field in dict(await allowed_fields(bot_id))

# عملکردهایِ مشترکِ همهٔ زمینه‌ها.
_COMMON: list[tuple[str, str]] = [
    ("wallet",        "💰 کیف پول"),
    ("profile",       "👤 مشخصات کاربری"),
    ("referral",      "👥 زیرمجموعه‌گیری"),
    ("ticket",        "🎫 ثبت تیکت"),
    ("top_customers", "🏆 نفرات برتر"),
    ("submenu",       "🗂 زیرمنو (دکمه‌های بیشتر)"),
    # === PATCH_PARTNER_V29 ===
    # این دو داده‌ی اضافی می‌خواهند (لینک / متن) که در مرحلهٔ بعد
    # پرسیده می‌شود — مثلِ بات‌سازِ اصلی.
    ("web_app",       "🌐 مینی‌اپ (باز کردن لینک)"),
    ("skip",          "📝 نمایش متن/پیام آزاد"),
]

# عملکردهایِ اختصاصیِ هر زمینه. هرچه این‌جا هست باید در
# tenant/__init__.py هندلر داشته باشد، وگرنه کاربر دکمه‌ای می‌سازد که
# هیچ کاری نمی‌کند.
_BY_FIELD: dict[str, list[tuple[str, str]]] = {
    # === PATCH_PARTNER_V28 ===
    # همهٔ گزینه‌هایِ خریدِ بات‌سازِ اصلی. قبلاً فقط buy_config بود و
    # صاحبِ ربات نمی‌توانست دکمهٔ «خرید از پنلِ X» بسازد.
    "vpn_sales": [
        ("buy_config",            "🛒 خرید کانفیگ (حجم/روز/تعداد)"),
        ("buy_vpn",               "🛰 خرید VPN خودکار (پنل X-UI)"),
        ("buy_vpn_sanai",         "🔵 خرید از پنل سنایی"),
        ("buy_vpn_pasargad",      "🟣 خرید از پنل پاسارگاد"),
        ("buy_vpn_pasarguard",    "🛡️ خرید از پنل PasarGuard"),
        ("buy_vpn_marzban",       "🟠 خرید از پنل مرزبان"),
        ("buy_vpn_marzneshin",    "⚪️ خرید از پنل مرزنشین"),
        ("buy_vpn_hiddify",       "🟢 خرید از پنل هیدیفای"),
        ("buy_vpn_rebecca",       "🔴 خرید از پنل ربکا"),
        ("buy_vpn_alireza",       "🟢 خرید از پنل علیرضا"),
        ("buy_vpn_upanel_mizban", "🟡 خرید از یوپنل (مرزبان)"),
        ("buy_vpn_nodex",         "🔷 خرید از پنل نودکس"),
        ("buy_vpn_degeris",       "🟣 خرید از پنل DEGERIS"),
        ("buy_vpn_eylan",         "🟠 خرید از پنل ایلان"),
        ("buy_vpn_heimdall",      "🛡 خرید از پنل HEIMDALL"),
        ("buy_vpn_rickpanel",     "🟥 خرید از پنل RickPanel"),
        ("buy_vpn_solidlayer",    "🧱 خرید از پنل SolidLayer"),
        # نیازمندِ PATCH_SHAHAN_BUY_BUTTON_V1 در tenant/__init__.py
        ("buy_vpn_shahan",        "🟤 خرید از پنل شاهان"),
        ("my_services",           "📡 سرویس‌های من"),
        ("renew_service",         "♻️ تمدید سرویس"),
        ("free_trial",            "🎁 تست رایگان VPN"),
        ("vpn_help",              "🧩 آموزش اتصال VPN"),
    ],
    "digital_shop": [
        ("show_shop",  "🛒 نمایش فروشگاه"),
        ("slideshow",  "🎞 اسلایدشو چندرسانه‌ای"),
    ],
    "ad_bot": [
        ("show_shop",  "🛒 نمایش فروشگاه"),
        ("slideshow",  "🎞 اسلایدشو چندرسانه‌ای"),
    ],
    "virtual_service": [
        ("vs_member",   "👥 افزایش ممبر کانال/گروه تلگرام"),
        ("vs_reaction", "❤️ افزایش ری‌اکشن پست تلگرام"),
        ("vs_view",     "👁 افزایش ویو پست تلگرام"),
        ("vs_like",     "👍 افزایش لایک تلگرام"),
        ("vs_vote",     "📊 افزایش رأی نظرسنجی تلگرام"),
        ("vs_comment",  "💬 افزایش کامنت تلگرام"),
        # === PATCH_VS_INSTAGRAM_ACTIONS_V1 ===
        ("vs_ig_follower", "🧑‍🤝‍🧑 افزایش فالوور اینستاگرام"),
        ("vs_ig_view",     "👁 افزایش ویو اینستاگرام"),
        ("vs_ig_like",     "👍 افزایش لایک اینستاگرام"),
    ],
}


def actions_for(field: str) -> list[tuple[str, str]]:
    """عملکردهایِ در دسترس برایِ یک زمینه."""
    return _BY_FIELD.get(field, []) + _COMMON


ACTION_LABEL = {k: v for _f, lst in _BY_FIELD.items() for k, v in lst}
ACTION_LABEL.update(dict(_COMMON))


class PBuild(StatesGroup):
    field = State()     # === PATCH_PARTNER_V24 === انتخابِ زمینهٔ ربات
    grid = State()      # انتخابِ خانه‌ها
    naming = State()    # منتظرِ نامِ یک دکمهٔ مشخص
    rename = State()    # === PATCH_PARTNER_V25 === تغییرِ نامِ دکمهٔ موجود
    webapp = State()    # === PATCH_PARTNER_V29 === لینکِ مینی‌اپ
    freetext = State()  # === PATCH_PARTNER_V29 === متنِ پیامِ آزاد
    color = State()     # === PATCH_PARTNER_V29 === رنگ‌بندیِ دکمه‌ها


# ────────── کمکی‌ها ──────────

# === PATCH_PARTNER_DETACH_NO_ACCESS_V1 ===
# گزارش: بعد از «🔓 انتقالِ ربات به بات‌سازِ اصلی»، رباتِ همکار هنوز به رباتِ آن
# شخص دسترسی داشت — چون ویزاردِ دکمه‌ها فقط owner_id را چک می‌کرد و نمی‌پرسید آیا
# این ربات هنوز فروشِ همین همکار است. حالا همهٔ ورودی‌ها فقط وقتی کار می‌کنند که
# ردیفِ PartnerSale (فروشِ همکار) هنوز وجود داشته باشد؛ با جداشدن حذف می‌شود.
async def _still_partner_sold(s, bot_id: int) -> bool:
    from models import PartnerSale
    return bool(await s.scalar(select(PartnerSale.id).where(
        PartnerSale.sold_bot_id == bot_id).limit(1)))


async def _owns(bot_id: int, uid: int) -> BotRow | None:
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != uid:
            return None
        if not await _still_partner_sold(s, bot_id):
            return None
    return row


async def _btn_count(bot_id: int) -> int:
    async with Session() as s:
        return int(await s.scalar(select(func.count(MainButton.id)).where(
            MainButton.bot_id == bot_id)) or 0)


async def _partner_bot_id(bot) -> int:
    """آیدیِ ردیفِ خودِ رباتِ همکار (فروشنده) — نه رباتی که ساخته می‌شود."""
    from tenant import get_bot_row
    try:
        row = await get_bot_row(bot)
        return row.id if row else 0
    except Exception:
        logger.exception("یافتنِ رباتِ همکار برایِ فهرستِ نوع‌ها ناموفق بود")
        return 0


async def field_kb(bot_id: int = 0) -> InlineKeyboardMarkup:
    """=== PATCH_PARTNER_V24 === انتخابِ زمینهٔ ربات.

    === PATCH_PARTNER_SALE_FIELDS_V1 === فقط نوع‌هایی که مالکِ رباتِ همکار
    برایِ فروش فعال کرده (تا در «تغییرِ نوع» و «شخصی‌سازی» هم همان فهرستِ
    محدود دیده شود، نه کلِ نوع‌ها).
    """
    b = InlineKeyboardBuilder()
    src = await allowed_fields(bot_id) if bot_id else [
        (k, lbl) for k, lbl in FIELDS if k in ACTIVE_FIELDS]  # PATCH_LOCK_TO_THREE_FIELDS_V1
    for key, label in src:
        b.button(text=label, callback_data=f"pbf:{key}",
                 style=ButtonStyle.SUCCESS)
    b.button(text="🔙 بستن", callback_data="pbg:close",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    return b.as_markup()


def grid_kb(selected: set[str]) -> InlineKeyboardMarkup:
    """شبکهٔ انتخابِ چیدمان — سبز یعنی دکمه، قرمز یعنی خانهٔ خالی."""
    rows = []
    for r in range(1, GRID_ROWS + 1):
        row = []
        for c in range(1, GRID_COLS + 1):
            on = f"{r},{c}" in selected
            row.append(InlineKeyboardButton(
                text="✓" if on else "·",
                callback_data=f"pbg:{r}:{c}",
                style=ButtonStyle.SUCCESS if on else ButtonStyle.DANGER))
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text="✅ تأیید و ادامه", callback_data="pbg:done",
        style=ButtonStyle.PRIMARY)])
    rows.append([
        InlineKeyboardButton(text="🗑 پاک‌کردن همه", callback_data="pbg:clear",
                             style=ButtonStyle.PRIMARY),
        # === PATCH_PARTNER_FIELD_LOCKED_V1 ===
        # نوعِ ربات فقط یک‌بار، همان لحظهٔ خرید تعیین می‌شود (ps:field:)
        # و دیگر قابلِ تغییر نیست؛ پس این‌جا به‌جایِ «تغییرِ نوعِ ربات»
        # فقط باید یک بازگشتِ سادهٔ یک‌قدمی باشد (بستنِ همین ویزارد).
        InlineKeyboardButton(text="🔙 بازگشت", callback_data="pbg:close",
                             style=ButtonStyle.DANGER),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def naming_kb(cells: list[dict]) -> InlineKeyboardMarkup:
    """پیش‌نمایشِ شیشه‌ای با چیدمانِ واقعی؛ روی هر دکمه می‌شود زد و اسم گذاشت."""
    rows_map: dict[int, list[dict]] = {}
    for c in cells:
        rows_map.setdefault(c["row"], []).append(c)

    kb_rows = []
    for r in sorted(rows_map):
        row = []
        for cell in sorted(rows_map[r], key=lambda x: x["col"]):
            if cell.get("label"):
                text, style = f"✅ {cell['label']}", ButtonStyle.SUCCESS
            else:
                text = f"➕ نام‌گذاری ({cell['row'] + 1}-{cell['col'] + 1})"
                style = ButtonStyle.DANGER
            row.append(InlineKeyboardButton(
                text=text, callback_data=f"pbn:{cell['row']}:{cell['col']}",
                style=style))
        kb_rows.append(row)

    done = all(c.get("label") for c in cells)
    kb_rows.append([
        InlineKeyboardButton(text="🔙 بازگشت به چیدمان", callback_data="pbn:back",
                             style=ButtonStyle.DANGER),
        InlineKeyboardButton(
            text="✅ تایید و ادامه", callback_data="pbn:confirm",
            style=ButtonStyle.SUCCESS if done else ButtonStyle.PRIMARY),
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ────────── ورود ──────────

async def start_build(bot_id: int, msg: Message, state: FSMContext,
                      bot: Bot | None = None) -> None:
    """ورودیِ ویزارد — از partner_sales و partner_user صدا زده می‌شود."""
    # === PATCH_PARTNER_V27 === مالکیت و شمارشِ دکمه‌ها در یک تراکنش
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != msg.from_user.id or not await _still_partner_sold(s, bot_id):
            await msg.answer("این ربات متعلق به شما نیست.")
            return
        n = int(await s.scalar(select(func.count(MainButton.id)).where(
            MainButton.bot_id == bot_id)) or 0)

    if n:
        # === PATCH_PARTNER_V25 ===
        # قبلاً تنها گزینه «بازسازی کامل» بود، یعنی برایِ عوض‌کردنِ یک
        # اسم هم باید همه‌چیز از نو چیده می‌شد. حالا ویرایشِ جزئی هم هست.
        b = InlineKeyboardBuilder()
        b.button(text="✏️ ویرایش دکمه‌های فعلی",
                 callback_data=f"pbe:{bot_id}", style=ButtonStyle.SUCCESS)
        b.button(text="🔄 بازسازی کامل چیدمان",
                 callback_data=f"pbr:{bot_id}", style=ButtonStyle.DANGER)
        b.button(text="🔙 بستن", callback_data="pbg:close",
                 style=ButtonStyle.DANGER)
        b.adjust(1)
        await msg.answer(
            f"🎨 <b>دکمه‌های @{row.username or '—'}</b>\n\n"
            f"این ربات {n} دکمه دارد.\n\n"
            "• <b>ویرایش</b>: اسم یا عملکردِ دکمه‌ها را عوض کن\n"
            "• <b>بازسازی</b>: همه پاک و از نو چیده می‌شوند",
            reply_markup=b.as_markup())
        return

    await state.clear()
    await state.update_data(pb_bot=bot_id, pb_sel=[])
    # === PATCH_PARTNER_BUILD_SKIP_FIELD_V1 ===
    # از PATCH_PARTNER_FIELD_BEFORE_PLAN_V1 به بعد، کاربر نوعِ ربات را
    # *قبل از خرید* در ps:field: انتخاب می‌کند و همان روی ردیفِ Bot
    # ذخیره می‌شود (تنانت/partner_sales.py:ps_token). پس اگر اینجا
    # row.field از قبل معتبر باشد، دیگر نباید دوباره همان سؤال را از
    # کاربر بپرسیم — مستقیم می‌رویم سراغِ چیدمانِ دکمه‌ها.
    if row.field in FIELD_LABEL:
        await state.update_data(pb_field=row.field)
        await state.set_state(PBuild.grid)
        await msg.answer(
            f"✅ نوع ربات: <b>{FIELD_LABEL[row.field]}</b>\n\n" + GRID_INTRO,
            reply_markup=grid_kb(set()))
        return
    await state.set_state(PBuild.field)
    await msg.answer(
        f"🤖 <b>@{row.username or '—'}</b>\n\n" + FIELD_INTRO,
        reply_markup=await field_kb(await _partner_bot_id(bot) if bot else 0))


@partner_build_router.callback_query(F.data.startswith("pbe:"))
async def pbe_edit(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """=== PATCH_PARTNER_V25 ===
    ویرایشِ چیدمانِ موجود بدونِ پاک‌کردن. مستقیم به صفحهٔ دکمه‌ها می‌رود،
    جایی که رویِ هر دکمه می‌شود زد و اسم یا عملکردش را عوض کرد."""
    bot_id = int(cb.data.split(":")[1])
    if not await _owns(bot_id, cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.clear()
    _txt, _kb = await _actions_page(bot_id)
    await cb.message.edit_text(_txt, reply_markup=_kb)
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbr:"))
async def pbg_reset(cb: CallbackQuery, state: FSMContext, bot: Bot):   # PATCH: bot قبلاً تعریف‌نشده بود (NameError)
    bot_id = int(cb.data.split(":")[1])
    row = await _owns(bot_id, cb.from_user.id)
    if not row:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        for o in (await s.scalars(select(MainButton).where(
                MainButton.bot_id == bot_id))).all():
            await s.delete(o)
        await s.commit()

    await state.clear()
    await state.update_data(pb_bot=bot_id, pb_sel=[])
    # === PATCH_PARTNER_FIELD_LOCKED_V1 ===
    # مثلِ start_build: نوعِ ربات فقط یک‌بار (لحظهٔ خرید) انتخاب می‌شود و
    # حتی موقعِ «بازسازیِ کامل» هم نباید دوباره پرسیده شود.
    if row.field in FIELD_LABEL:
        await state.update_data(pb_field=row.field)
        await state.set_state(PBuild.grid)
        await cb.message.edit_text(
            f"🤖 <b>@{row.username or '—'}</b>\n\n"
            f"دکمه‌های قبلی پاک شدند.\n\n"
            f"✅ نوع ربات: <b>{FIELD_LABEL[row.field]}</b>\n\n" + GRID_INTRO,
            reply_markup=grid_kb(set()))
        await cb.answer()
        return
    await state.set_state(PBuild.field)
    await cb.message.edit_text(
        f"🤖 <b>@{row.username or '—'}</b>\n\n"
        "دکمه‌های قبلی پاک شدند.\n\n" + FIELD_INTRO,
        reply_markup=await field_kb(await _partner_bot_id(bot)))
    await cb.answer()


@partner_build_router.callback_query(F.data == "pbg:close")
async def pbg_close(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("بستنِ ویزارد ناموفق بود")
    await cb.answer()


# ────────── مرحلهٔ ۰: زمینه ──────────

@partner_build_router.callback_query(PBuild.field, F.data.startswith("pbf:"))
async def pbf_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """=== PATCH_PARTNER_V24 ===
    زمینه رویِ خودِ ردیفِ Bot ذخیره می‌شود، چون tenant/__init__.py هم
    برایِ رفتارِ ربات به همان نگاه می‌کند (نه فقط این ویزارد)."""
    field = cb.data.split(":")[1]
    # === PATCH_PARTNER_SALE_FIELDS_V1 ===
    if not await field_allowed(await _partner_bot_id(bot), field):
        await cb.answer("این نوع ربات فعلاً ارائه نمی‌شود", show_alert=True)
        return
    data = await state.get_data()
    bot_id = data.get("pb_bot")
    if not bot_id or not await _owns(bot_id, cb.from_user.id):
        await cb.answer("این مرحله منقضی شده", show_alert=True)
        return

    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        if row:
            row.field = field
            await s.commit()

    # === PATCH_PARTNER_V26 ===
    # اگر ربات از قبل دکمه دارد، یعنی کاربر فقط برایِ تعیینِ عملکرد
    # آمده بود و زمینه‌اش خالی بود — نباید چیدمانش پاک شود.
    if await _btn_count(bot_id):
        await state.clear()
        _txt, _kb = await _actions_page(bot_id)
        await cb.message.edit_text(
            f"✅ نوع ربات: <b>{FIELD_LABEL[field]}</b>\n\n" + _txt,
            reply_markup=_kb)
        await cb.answer()
        return

    await state.update_data(pb_field=field, pb_sel=[])
    await state.set_state(PBuild.grid)
    await cb.message.edit_text(
        f"✅ نوع ربات: <b>{FIELD_LABEL[field]}</b>\n\n" + GRID_INTRO,
        reply_markup=grid_kb(set()))
    await cb.answer()


# ────────── مرحلهٔ ۱: شبکه ──────────


@partner_build_router.callback_query(PBuild.grid, F.data == "pbg:clear")
async def pbg_clear(cb: CallbackQuery, state: FSMContext):
    await state.update_data(pb_sel=[])
    try:
        await cb.message.edit_reply_markup(reply_markup=grid_kb(set()))
    except Exception:
        logger.exception("پاک‌کردنِ شبکه ناموفق بود")
    await cb.answer("همه پاک شد")


@partner_build_router.callback_query(PBuild.grid, F.data == "pbg:done")
async def pbg_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = set(data.get("pb_sel") or [])
    if not sel:
        await cb.answer("حداقل یک خانه را انتخاب کن.", show_alert=True)
        return

    # خانه‌هایِ انتخاب‌شده را فشرده کن: ردیف‌هایِ خالی حذف و شماره‌ها
    # از صفر بازچینی می‌شوند، چون MainButton.row/col باید پیوسته باشند.
    by_row: dict[int, list[int]] = {}
    for key in sel:
        r, c = (int(x) for x in key.split(","))
        by_row.setdefault(r, []).append(c)

    cells = []
    for new_r, r in enumerate(sorted(by_row)):
        for new_c, _c in enumerate(sorted(by_row[r])):
            cells.append({"row": new_r, "col": new_c, "label": None})

    await state.update_data(pb_cells=cells)
    await state.set_state(None)
    await cb.message.edit_text(
        f"✏️ <b>نام دکمه‌ها</b> ({len(cells)} دکمه)\n\n"
        "روی هر دکمه بزن و اسمش را بگذار.",
        reply_markup=naming_kb(cells))
    await cb.answer()


@partner_build_router.callback_query(PBuild.grid, F.data.startswith("pbg:"))
async def pbg_toggle(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await cb.answer()
        return
    key = f"{parts[1]},{parts[2]}"

    data = await state.get_data()
    sel = set(data.get("pb_sel") or [])
    sel.symmetric_difference_update({key})
    await state.update_data(pb_sel=sorted(sel))
    try:
        await cb.message.edit_reply_markup(reply_markup=grid_kb(sel))
    except Exception:
        logger.exception("به‌روزرسانیِ شبکه ناموفق بود")
    await cb.answer()


# ────────── مرحلهٔ ۲: نام‌گذاری ──────────

@partner_build_router.callback_query(F.data == "pbn:back")
async def pbn_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = set(data.get("pb_sel") or [])
    await state.set_state(PBuild.grid)
    await cb.message.edit_text(GRID_INTRO, reply_markup=grid_kb(sel))
    await cb.answer()


@partner_build_router.callback_query(F.data == "pbn:confirm")
async def pbn_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cells = list(data.get("pb_cells") or [])
    bot_id = data.get("pb_bot")
    if not cells or not bot_id:
        await cb.answer("این مرحله منقضی شده", show_alert=True)
        return
    if not all(x.get("label") for x in cells):
        await cb.answer("اول اسم همهٔ دکمه‌ها را بگذار.", show_alert=True)
        return
    if not await _owns(bot_id, cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    async with Session() as s:
        for o in (await s.scalars(select(MainButton).where(
                MainButton.bot_id == bot_id))).all():
            await s.delete(o)
        for cell in cells:
            s.add(MainButton(bot_id=bot_id, row=cell["row"], col=cell["col"],
                             label=cell["label"], action=None))
        await s.commit()

    await state.clear()
    _txt, _kb = await _actions_page(bot_id)
    await cb.message.edit_text(
        "✅ <b>چیدمان ذخیره شد</b>\n\n" + _txt, reply_markup=_kb)
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbn:"))
async def pbn_tap(cb: CallbackQuery, state: FSMContext):
    """زدنِ یک خانه برایِ نام‌گذاری. بعد از pbn:back و pbn:confirm ثبت
    شده تا آن دو زودتر گرفته شوند."""
    parts = cb.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await cb.answer()
        return
    r, c = int(parts[1]), int(parts[2])

    data = await state.get_data()
    if not data.get("pb_cells"):
        await cb.answer("این مرحله منقضی شده", show_alert=True)
        return

    await state.update_data(pb_at=[r, c])
    await state.set_state(PBuild.naming)
    await cb.message.edit_text(
        f"✏️ نام دکمهٔ ردیف {r + 1}، ستون {c + 1} را بفرست.\n\n"
        "مثال: <code>🛒 خرید سرویس</code>")
    await cb.answer()


@partner_build_router.message(PBuild.naming)
async def pbn_set_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    cells = list(data.get("pb_cells") or [])
    at = data.get("pb_at")
    if not cells or not at:
        await state.clear()
        return

    label = (msg.text or "").strip()
    if not label:
        await msg.answer("اسم دکمه نمی‌تواند خالی باشد.")
        return
    if len(label) > 60:
        await msg.answer("اسم دکمه خیلی بلند است (حداکثر ۶۰ کاراکتر).")
        return

    r, c = at
    for cell in cells:
        if cell.get("label") == label and not (
                cell["row"] == r and cell["col"] == c):
            await msg.answer("این اسم قبلاً استفاده شده. اسم دیگری بفرست.")
            return
    for cell in cells:
        if cell["row"] == r and cell["col"] == c:
            cell["label"] = label
            break

    await state.update_data(pb_cells=cells, pb_at=None)
    await state.set_state(None)
    named = sum(1 for x in cells if x.get("label"))
    await msg.answer(
        f"✏️ <b>نام دکمه‌ها</b> ({named} از {len(cells)})\n\n"
        "روی دکمهٔ بعدی بزن یا «تایید و ادامه» را انتخاب کن.",
        reply_markup=naming_kb(cells))


# ────────── مرحلهٔ ۳: عملکردها ──────────

async def _actions_page(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """=== PATCH_PARTNER_V27 ===
    متن و کیبوردِ صفحهٔ عملکردها، با **یک** کوئری.

    قبلاً _actions_text و _actions_kb هرکدام جدا همان جدول را می‌خواندند
    و هر کلیک ۵ تا ۷ رفت‌وبرگشت به Postgres می‌زد — همان کندیِ محسوس.
    """
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id)
            .order_by(MainButton.row, MainButton.col))).all()
        items = [(b.id, b.label, b.action, b.row, b.col) for b in btns]
        _msg_map = {b.id: bool(b.message) for b in btns}
        field = (row.field if row else "") or ""

    # ── متن ──
    lines = ["⚙️ <b>تعیین عملکرد دکمه‌ها</b>\n",
             "روی هر دکمه بزن و مشخص کن با زدنش چه اتفاقی بیفتد.\n"]
    for _bid, label, action, _r, _c in items:
        if action:
            lines.append(f"✅ {label} ← {ACTION_LABEL.get(action, action)}")
        elif _msg_map.get(_bid):
            # === PATCH_PARTNER_V29 === دکمهٔ پیامِ آزاد اکشن ندارد
            lines.append(f"✅ {label} ← 📝 نمایش متن")
        else:
            lines.append(f"⚙️ {label} ← <i>تنظیم نشده</i>")
    if field and field in FIELD_LABEL:
        lines.append(f"\n🤖 نوع ربات: {FIELD_LABEL[field]}")

    # ── کیبورد، با همان چیدمانِ واقعیِ صفحهٔ اصلی ──
    rows_map: dict[int, list[tuple]] = {}
    for it in items:
        rows_map.setdefault(it[3], []).append(it)

    kb_rows = []
    pending = 0
    for r in sorted(rows_map):
        row_btns = []
        for bid, label, action, _r, _c in sorted(rows_map[r], key=lambda x: x[4]):
            wide = len(rows_map[r]) == 1
            if action:
                style = ButtonStyle.SUCCESS
                text = (f"✅ {label} → {ACTION_LABEL.get(action, action)}"
                        if wide else f"✅ {label}")
            elif _msg_map.get(bid):   # === PATCH_PARTNER_V29 ===
                style = ButtonStyle.SUCCESS
                text = f"✅ {label} → 📝 نمایش متن" if wide else f"✅ {label}"
            else:
                style = ButtonStyle.PRIMARY
                text = f"⚙️ {label} → تنظیم نشده" if wide else f"⚙️ {label}"
                pending += 1
            row_btns.append(InlineKeyboardButton(
                text=text, callback_data=f"pba:{bid}", style=style))
        kb_rows.append(row_btns)

    # === PATCH_PARTNER_V29 === مرحلهٔ رنگ‌بندی، مثلِ بات‌سازِ اصلی
    kb_rows.append([InlineKeyboardButton(
        text="🎨 رنگ دکمه‌ها", callback_data=f"pbcol:{bot_id}",
        style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(
        text=("🏁 پایان و ذخیره" if not pending
              else f"🏁 پایان ({pending} دکمه بدون عملکرد)"),
        callback_data=f"pbd:{bot_id}",
        style=(ButtonStyle.SUCCESS if not pending else ButtonStyle.PRIMARY))])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@partner_build_router.callback_query(F.data.startswith("pba:"))
async def pba_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    # === PATCH_PARTNER_V27 ===
    # دکمه، ربات و مالکیت در یک تراکنش — قبلاً سه رفت‌وبرگشت جدا بود.
    btn_id = int(cb.data.split(":")[1])
    async with Session() as s:
        btn = await s.get(MainButton, btn_id)
        if not btn:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        bot_id, label = btn.bot_id, btn.label
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != cb.from_user.id or not await _still_partner_sold(s, bot_id):
            await cb.answer("دسترسی ندارید", show_alert=True)
            return
        field = (row.field if row else "") or ""

    # === PATCH_PARTNER_V26 ===
    # فهرستِ عملکردها فقط از رویِ زمینه‌ای که *خودِ کاربر* انتخاب کرده
    # ساخته می‌شود. اگر هنوز انتخاب نکرده، به‌جایِ حدس‌زدن، همان‌جا
    # می‌پرسیم — وگرنه کاربر عملکردهایی می‌بیند که به رباتش ربط ندارند.
    if field not in FIELD_LABEL:
        await state.update_data(pb_bot=bot_id)
        await state.set_state(PBuild.field)
        await cb.message.edit_text(
            "🤖 اول مشخص کن ربات چه‌کار می‌کند، تا عملکردهای مناسبش را "
            "نشانت بدهم.\n\n" + FIELD_INTRO,
            reply_markup=await field_kb(await _partner_bot_id(bot)))
        await cb.answer()
        return

    b = InlineKeyboardBuilder()
    for key, label_fa in actions_for(field):
        b.button(text=label_fa, callback_data=f"pbs:{btn_id}:{key}",
                 style=ButtonStyle.SUCCESS)
    # === PATCH_PARTNER_V25 === تغییرِ نامِ همین دکمه
    b.button(text="✏️ تغییر نام این دکمه", callback_data=f"pbl:{btn_id}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data=f"pbm:{bot_id}",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        f"⚙️ <b>عملکرد دکمهٔ «{label}»</b>\n\n"
        "با زدن این دکمه چه اتفاقی بیفتد؟",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbs:"))
async def pbs_set(cb: CallbackQuery, state: FSMContext):
    _, raw_id, key = cb.data.split(":", 2)
    if key not in ACTION_LABEL:
        await cb.answer()
        return
    # === PATCH_PARTNER_V27 === خواندن، بررسیِ مالکیت و ذخیره در یک تراکنش
    async with Session() as s:
        btn = await s.get(MainButton, int(raw_id))
        if not btn:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        bot_id = btn.bot_id
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != cb.from_user.id or not await _still_partner_sold(s, bot_id):
            await cb.answer("دسترسی ندارید", show_alert=True)
            return
        # === PATCH_PARTNER_V29 ===
        # "skip" اکشن نیست: دکمه فقط یک پیام نشان می‌دهد، پس action
        # باید None بماند — همان کاری که بات‌سازِ اصلی می‌کند.
        btn.action = None if key == "skip" else key
        await s.commit()

    # === PATCH_PARTNER_V29 ===
    # دو عملکرد داده‌ی اضافی می‌خواهند، وگرنه دکمه‌شان بی‌کار می‌ماند.
    if key == "web_app":
        await state.update_data(pb_cfg_btn=int(raw_id), pb_bot=bot_id)
        await state.set_state(PBuild.webapp)
        await cb.message.edit_text(
            "🌐 <b>لینک مینی‌اپ</b>\n\n"
            "آدرس سایتی که می‌خواهی داخل تلگرام باز شود را بفرست.\n"
            "باید با <code>https://</code> شروع شود.\n\n"
            "مثال: <code>https://example.com</code>")
        await cb.answer()
        return
    if key == "skip":
        await state.update_data(pb_cfg_btn=int(raw_id), pb_bot=bot_id)
        await state.set_state(PBuild.freetext)
        await cb.message.edit_text(
            "📝 <b>متن پیام</b>\n\n"
            "با زدن این دکمه چه متنی برای کاربر فرستاده شود؟\n"
            "می‌توانی از <b>bold</b> و <i>italic</i> استفاده کنی.")
        await cb.answer()
        return

    await cb.answer(f"عملکرد: {ACTION_LABEL[key]}")
    _txt, _kb = await _actions_page(bot_id)
    await cb.message.edit_text(_txt, reply_markup=_kb)


@partner_build_router.callback_query(F.data.startswith("pbm:"))
async def pbm_back(cb: CallbackQuery):
    bot_id = int(cb.data.split(":")[1])
    if not await _owns(bot_id, cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _txt, _kb = await _actions_page(bot_id)
    await cb.message.edit_text(_txt, reply_markup=_kb)
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbd:"))
async def pbd_done(cb: CallbackQuery, state: FSMContext):
    bot_id = int(cb.data.split(":")[1])
    row = await _owns(bot_id, cb.from_user.id)
    if not row:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    async with Session() as s:
        n_all = await s.scalar(select(func.count(MainButton.id)).where(
            MainButton.bot_id == bot_id)) or 0
        n_set = await s.scalar(select(func.count(MainButton.id)).where(
            MainButton.bot_id == bot_id,
            MainButton.action.isnot(None))) or 0

    await state.clear()
    note = ("" if n_set == n_all else
            f"\n\n⚠️ {n_all - n_set} دکمه هنوز عملکرد ندارد و با زدنشان "
            "اتفاقی نمی‌افتد. هر وقت خواستی از «🗂 ربات‌های من» تکمیلشان کن.")
    # === PATCH_PARTNER_V35 === راهِ خروج، وگرنه صفحه بن‌بست بود
    _kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 ربات‌های من", callback_data="pu:bots",
                             style=ButtonStyle.SUCCESS)]])
    await cb.message.edit_text(
        f"🎉 <b>ربات @{row.username or '—'} آمادهٔ استفاده است!</b>\n\n"
        f"دکمه‌ها: {n_all} · تنظیم‌شده: {n_set}\n\n"
        "برو داخل ربات خودت و <code>/start</code> بزن تا نتیجه را ببینی."
        + note, reply_markup=_kb)
    await cb.answer("ذخیره شد")


# === PATCH_PARTNER_V25 ===
@partner_build_router.callback_query(F.data.startswith("pbl:"))
async def pbl_rename_ask(cb: CallbackQuery, state: FSMContext):
    # === PATCH_PARTNER_V27 ===
    # دکمه، ربات و مالکیت در یک تراکنش — قبلاً سه رفت‌وبرگشت جدا بود.
    btn_id = int(cb.data.split(":")[1])
    async with Session() as s:
        btn = await s.get(MainButton, btn_id)
        if not btn:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        bot_id, label = btn.bot_id, btn.label
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != cb.from_user.id or not await _still_partner_sold(s, bot_id):
            await cb.answer("دسترسی ندارید", show_alert=True)
            return
        field = (row.field if row else "") or ""

    await state.update_data(pb_rename=btn_id, pb_bot=bot_id)
    await state.set_state(PBuild.rename)
    await cb.message.edit_text(
        f"✏️ نام فعلی: <b>{label}</b>\n\n"
        "نام جدید را بفرست:")
    await cb.answer()


@partner_build_router.message(PBuild.rename)
async def pbl_rename_set(msg: Message, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("pb_rename")
    bot_id = data.get("pb_bot")
    label = (msg.text or "").strip()
    if not btn_id or not bot_id:
        await state.clear()
        return
    if not await _owns(bot_id, msg.from_user.id):   # PATCH_PARTNER_DETACH_NO_ACCESS_V1
        await state.clear()
        await msg.answer("دسترسی ندارید.")
        return
    if not label:
        await msg.answer("اسم دکمه نمی‌تواند خالی باشد.")
        return
    if len(label) > 60:
        await msg.answer("اسم دکمه خیلی بلند است (حداکثر ۶۰ کاراکتر).")
        return

    async with Session() as s:
        dup = await s.scalar(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.label == label,
            MainButton.id != btn_id))
        if dup:
            await msg.answer("این اسم قبلاً استفاده شده. اسم دیگری بفرست.")
            return
        btn = await s.get(MainButton, btn_id)
        if btn:
            btn.label = label
            await s.commit()

    await state.clear()
    _txt, _kb = await _actions_page(bot_id)
    await msg.answer(_txt, reply_markup=_kb)


# ======================= دادهٔ اضافیِ عملکردها =======================
# === PATCH_PARTNER_V29 ===
# دو عملکرد بدونِ دادهٔ اضافی بی‌کارند: web_app لینک می‌خواهد و skip متن.
# در MainButton.config به‌صورت JSON ذخیره می‌شوند — همان قالبی که
# tenant/__init__.py موقعِ اجرا می‌خواند.

async def _save_cfg(btn_id: int, **kw) -> None:
    """کلیدهایِ داده‌شده را در MainButton.config ادغام می‌کند."""
    import json

    async with Session() as s:
        mb = await s.get(MainButton, btn_id)
        if not mb:
            return
        try:
            cfg = json.loads(mb.config) if mb.config else {}
        except Exception:
            cfg = {}
        cfg.update(kw)
        mb.config = json.dumps(cfg, ensure_ascii=False)
        await s.commit()


@partner_build_router.message(PBuild.webapp)
async def pbw_url(msg: Message, state: FSMContext):
    url = (msg.text or "").strip()
    if not url.startswith("https://") or " " in url or len(url) > 500:
        await msg.answer(
            "آدرس معتبر نیست. باید با <code>https://</code> شروع شود، "
            "فاصله نداشته باشد و کوتاه‌تر از ۵۰۰ کاراکتر باشد.")
        return

    data = await state.get_data()
    btn_id, bot_id = data.get("pb_cfg_btn"), data.get("pb_bot")
    if not btn_id or not bot_id:
        await state.clear()
        return
    if not await _owns(bot_id, msg.from_user.id):
        await state.clear()
        return

    await _save_cfg(btn_id, webapp_url=url)
    await state.set_state(None)
    _txt, _kb = await _actions_page(bot_id)
    await msg.answer(f"✅ لینک مینی‌اپ ثبت شد.\n\n{_txt}", reply_markup=_kb)


@partner_build_router.message(PBuild.freetext)
async def pbw_text(msg: Message, state: FSMContext):
    text = (msg.html_text or msg.text or "").strip()
    if not text:
        await msg.answer("متن نمی‌تواند خالی باشد.")
        return
    if len(text) > 3000:
        await msg.answer("متن خیلی بلند است (حداکثر ۳۰۰۰ کاراکتر).")
        return

    data = await state.get_data()
    btn_id, bot_id = data.get("pb_cfg_btn"), data.get("pb_bot")
    if not btn_id or not bot_id:
        await state.clear()
        return
    if not await _owns(bot_id, msg.from_user.id):
        await state.clear()
        return

    # متن در ستونِ MainButton.message است، نه config — همان جایی که
    # tenant/__init__.py موقعِ زدنِ دکمه می‌خواند.
    async with Session() as s:
        mb = await s.get(MainButton, btn_id)
        if mb:
            mb.message = text
            await s.commit()
    await state.set_state(None)
    _txt, _kb = await _actions_page(bot_id)
    await msg.answer(f"✅ متن ثبت شد.\n\n{_txt}", reply_markup=_kb)


# ======================= رنگ‌بندیِ دکمه‌ها =======================
# === PATCH_PARTNER_V29 ===
# مثلِ بات‌سازِ اصلی: با هر زدن، رنگِ دکمه می‌چرخد قرمز ← سبز ← آبی.
# رنگ در MainButton.color ذخیره می‌شود که خودِ tenant موقعِ رندر می‌خواند.

_COLOR_CYCLE = ["danger", "success", "primary"]
_COLOR_STYLE = {
    "danger": ButtonStyle.DANGER,
    "success": ButtonStyle.SUCCESS,
    "primary": ButtonStyle.PRIMARY,
}


async def _color_page(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id)
            .order_by(MainButton.row, MainButton.col))).all()
        items = [(b.id, b.label, b.color, b.row, b.col) for b in btns]

    rows_map: dict[int, list[tuple]] = {}
    for it in items:
        rows_map.setdefault(it[3], []).append(it)

    kb_rows = []
    for r in sorted(rows_map):
        row = []
        for bid, label, color, _r, _c in sorted(rows_map[r], key=lambda x: x[4]):
            row.append(InlineKeyboardButton(
                text=label, callback_data=f"pbc:{bid}",
                style=_COLOR_STYLE.get(color or "primary",
                                       ButtonStyle.PRIMARY)))
        kb_rows.append(row)

    kb_rows.append([InlineKeyboardButton(
        text="✅ تایید رنگ‌بندی", callback_data=f"pbcok:{bot_id}",
        style=ButtonStyle.SUCCESS)])
    return (
        "🎨 <b>رنگ دکمه‌ها</b>\n\n"
        "روی هر دکمه بزن تا رنگش عوض شود:\n"
        "🔴 قرمز ← 🟢 سبز ← 🔵 آبی ← 🔴 …\n\n"
        "این همان چیزی است که کاربرانت می‌بینند.",
        InlineKeyboardMarkup(inline_keyboard=kb_rows))


@partner_build_router.callback_query(F.data.startswith("pbcol:"))
async def pbcol_open(cb: CallbackQuery, state: FSMContext):
    bot_id = int(cb.data.split(":")[1])
    if not await _owns(bot_id, cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.clear()
    _txt, _kb = await _color_page(bot_id)
    await cb.message.edit_text(_txt, reply_markup=_kb)
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbcok:"))
async def pbcol_done(cb: CallbackQuery):
    bot_id = int(cb.data.split(":")[1])
    if not await _owns(bot_id, cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _txt, _kb = await _actions_page(bot_id)
    await cb.message.edit_text("✅ رنگ‌بندی ذخیره شد.\n\n" + _txt,
                               reply_markup=_kb)
    await cb.answer()


@partner_build_router.callback_query(F.data.startswith("pbc:"))
async def pbc_cycle(cb: CallbackQuery):
    btn_id = int(cb.data.split(":")[1])
    async with Session() as s:
        mb = await s.get(MainButton, btn_id)
        if not mb:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        bot_id = mb.bot_id
        row = await s.get(BotRow, bot_id)
        if not row or row.owner_id != cb.from_user.id or not await _still_partner_sold(s, bot_id):
            await cb.answer("دسترسی ندارید", show_alert=True)
            return
        cur = mb.color if mb.color in _COLOR_CYCLE else None
        nxt = (_COLOR_CYCLE[(_COLOR_CYCLE.index(cur) + 1) % len(_COLOR_CYCLE)]
               if cur else _COLOR_CYCLE[0])
        mb.color = nxt
        await s.commit()

    _txt, _kb = await _color_page(bot_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=_kb)
    except Exception:
        logger.exception("به‌روزرسانیِ رنگ ناموفق بود")
    await cb.answer()
