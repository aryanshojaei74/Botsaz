"""
ad_broadcast.py — پنلِ ادمین و اسکجولرِ ارسالِ زمان‌بندی‌شدهٔ تبلیغ برای
بات‌هایِ نوعِ `ad_bot`.

قابلیت‌ها:
  ۱) ادمین می‌تونه هر تعداد کانال یا گروه رو به بات وصل کنه
     (بعد از ادمین‌کردنِ بات در اونجا).
  ۲) ادمین متنِ تبلیغش رو با تمامِ فرمت‌های تلگرام تنظیم می‌کنه (HTML)،
     همراهِ عکس یا ویدیو (اختیاری).
  ۳) ادمین بازهٔ ارسال رو انتخاب می‌کنه (پیش‌فرض‌ها + سفارشی).
  ۴) با «▶️ فعال‌سازی»، اسکجولر رأسِ هر بازه تبلیغ رو به همهٔ کانال/گروه‌ها
     می‌فرسته و آمار رو نگه می‌داره.
  ۵) اگه ارسال به یه هدفِ خاص شکست بخوره (بات از اونجا اخراج شده، ادمین
     نیست، ...) خطا ذخیره می‌شه و بعد از چندبار خطا، اون هدف auto-disable
     می‌شه تا loop رو خراب نکنه.
"""
from __future__ import annotations

import datetime
import asyncio
import html as _html
import re

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from config import Session
from models import AdBroadcast, AdBroadcastTarget, Bot as BotRow

from logging_setup import get_logger as _get_logger

# === PATCH_AD_GIFT_LINK_V1 ===
# لینک گیفت تلگرام (t.me/nft/...) — اگه مالک به‌جای عکس، لینک گیفتش رو
# داخل متن تبلیغ بذاره، تبلیغ با پیش‌نمایش بزرگِ همین لینک ارسال می‌شه
# تا خودِ گیفت پرمیوم دقیقاً زیرِ لینک نمایش داده بشه.
_GIFT_LINK_RE = re.compile(r"https?://t\.me/nft/[\w-]+", re.IGNORECASE)
logger = _get_logger(__name__)

ad_broadcast_router = Router()

# ═════════════════════════ ثابت‌ها ═════════════════════════

# اگه هدفی این تعداد بار پشت‌سر هم خطا بده، خودکار disable می‌شه.
_MAX_CONSECUTIVE_FAILS_BEFORE_DISABLE = 5

# بازه‌های پیش‌فرض (به دقیقه) که ادمین می‌تونه سریع انتخاب کنه.
_PRESET_INTERVALS = [
    ("هر ساعت", 60),
    ("هر ۳ ساعت", 180),
    ("هر ۶ ساعت", 360),
    ("هر ۱۲ ساعت", 720),
    ("روزی یک بار", 1440),
    ("هر ۲ روز", 2880),
    ("هر ۳ روز", 4320),
    ("هفتگی", 10080),
]


# ═════════════════════════ FSM ═════════════════════════

class AdBroadcastFSM(StatesGroup):
    editing_text = State()      # منتظرِ متنِ تبلیغ (پیام یا عکس با کپشن)
    editing_interval = State()  # منتظرِ عددِ سفارشیِ بازه
    adding_target = State()     # منتظرِ فورواردِ پیام یا chat_id


# ═════════════════════════ کمک‌تابع‌ها ═════════════════════════

async def _get_or_create_config(bot_id: int) -> AdBroadcast:
    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == bot_id))
        if cfg is None:
            cfg = AdBroadcast(bot_id=bot_id)
            s.add(cfg)
            await s.commit()
            await s.refresh(cfg)
        return cfg


async def _get_bot_row_by_token(token: str) -> BotRow | None:
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == token))


def _fmt_interval(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} دقیقه"
    if minutes < 1440:
        h = minutes // 60
        rem = minutes % 60
        return f"{h} ساعت" + (f" و {rem} دقیقه" if rem else "")
    d = minutes // 1440
    rem_h = (minutes % 1440) // 60
    return f"{d} روز" + (f" و {rem_h} ساعت" if rem_h else "")


def _fmt_dt(dt: datetime.datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


async def _is_owner(msg_or_cb, bot: Bot) -> BotRow | None:
    """اگه فرستنده مالکِ همین بات باشه، ردیفِ Bot رو برمی‌گردونه."""
    row = await _get_bot_row_by_token(bot.token)
    if not row:
        return None
    uid = msg_or_cb.from_user.id
    if uid != row.owner_id:
        return None
    return row


# ═════════════════════════ منوی اصلی پنل ═════════════════════════

async def _render_panel(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبوردِ پنلِ اصلیِ تبلیغات رو می‌سازه."""
    cfg = await _get_or_create_config(bot_id)
    async with Session() as s:
        targets = (await s.scalars(select(AdBroadcastTarget).where(
            AdBroadcastTarget.bot_id == bot_id))).all()
        # === PATCH_AD_PRODUCT_BUTTON_V1 ===
        product_title = None
        if cfg.linked_product_id:
            from models import Product
            p = await s.get(Product, cfg.linked_product_id)
            product_title = p.title if p else None
    n_total = len(targets)
    n_active = sum(1 for t in targets if not t.disabled)

    text_preview_len = 60
    if cfg.ad_text_html:
        # نمایشِ خامِ کوتاهِ متن — به کاربر می‌گیم چقدر تنظیم شده
        _no_tags = cfg.ad_text_html.replace("<", "&lt;")  # امنِ HTML
        _short = _no_tags[:text_preview_len]
        if len(_no_tags) > text_preview_len:
            _short += "..."
        text_desc = _short
    else:
        text_desc = "<i>هنوز تنظیم نشده</i>"

    media_desc = ""
    if cfg.ad_photo_file_id:
        media_desc = " 📸 (با عکس)"
    elif cfg.ad_video_file_id:
        media_desc = " 🎬 (با ویدیو)"
    elif getattr(cfg, "ad_gift_url", None):
        media_desc = " 🎁 (با گیفت پرمیوم)"

    status_label = "🟢 فعال" if cfg.is_active else "🔴 غیرفعال"
    action_label = "⏸ توقف ارسال" if cfg.is_active else "▶️ شروع ارسال"
    product_line = (f"🔗 دکمهٔ محصول: <b>{product_title}</b>\n" if product_title
                    else "🔗 دکمهٔ محصول: <i>وصل نشده</i>\n")

    text = (
        f"📢 <b>پنلِ تبلیغِ زمان‌بندی‌شده</b>\n\n"
        f"وضعیت: <b>{status_label}</b>\n"
        f"⏱ بازهٔ ارسال: <b>{_fmt_interval(cfg.interval_minutes)}</b>\n"
        f"🎯 کانال/گروه‌های متصل: <b>{n_active}</b>"
        + (f" (از {n_total})" if n_total != n_active else "") + "\n"
        f"📊 مجموع ارسال‌های موفق: <b>{cfg.total_sent}</b>\n"
        f"🕐 آخرین ارسال: <b>{_fmt_dt(cfg.last_sent_at)}</b>\n"
        f"⏰ ارسالِ بعدی: <b>{_fmt_dt(cfg.next_send_at) if cfg.is_active else '—'}</b>\n"
        f"{product_line}\n"
        f"✍️ متنِ فعلی{media_desc}:\n<blockquote>{text_desc}</blockquote>"
    )

    link_btn_label = "🔗 تغییرِ محصولِ متصل" if product_title else "🔗 اتصال به محصول"
    link_row = [InlineKeyboardButton(text=link_btn_label, callback_data="adb:link_product", style=ButtonStyle.PRIMARY)]
    if product_title:
        link_row.append(InlineKeyboardButton(text="🔓 قطع اتصال", callback_data="adb:unlink_product", style=ButtonStyle.PRIMARY))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ ویرایش متن تبلیغ", callback_data="adb:edit_text", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⏱ تنظیم بازه ارسال", callback_data="adb:edit_interval", style=ButtonStyle.PRIMARY)],
        link_row,
        [InlineKeyboardButton(text="➕ افزودن کانال/گروه", callback_data="adb:add_target", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="📋 لیست", callback_data="adb:list_targets", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=action_label,
                              callback_data="adb:toggle", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🗓 پست‌های یک‌بارهٔ زمان‌بندی‌شده", callback_data="adbo:menu", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت به منوی ادمین",
                              callback_data="tadm:menu", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@ad_broadcast_router.callback_query(F.data == "adb:menu")
async def open_panel(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row:
        await cb.answer()
        return
    if row.field not in ("ad_bot", "digital_shop"):
        await cb.answer("این پنل فقط برای ربات‌های تبلیغاتی و فروشگاهی فعاله.", show_alert=True)
        return
    text, kb = await _render_panel(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                    disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)
    await cb.answer()


# ═════════════════════════ اتصال به محصول (دکمهٔ آبیِ روی تبلیغ) ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:link_product")
async def link_product_menu(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    from models import Product
    async with Session() as s:
        products = (await s.scalars(select(Product).where(
            Product.bot_id == row.id, Product.is_active == True
        ).order_by(Product.sort_order, Product.id))).all()
    if not products:
        await cb.answer(
            "هنوز محصولی نساختی. اول از «🛒 فروشگاه دیجیتال» یه محصول اضافه کن.",
            show_alert=True)
        return
    kb_rows = [[InlineKeyboardButton(text=f"🛍 {p.title}", callback_data=f"adb:pickproduct:{p.id}", style=ButtonStyle.PRIMARY)]
              for p in products]
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adb:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🔗 <b>اتصال به محصول</b>\n\n"
        "کدوم محصول رو انتخاب کنی، زیرِ هر پستِ تبلیغ یه دکمهٔ آبی اضافه "
        "می‌شه که مستقیم کاربر رو به صفحهٔ همون محصول تو خودِ بات می‌بره:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await cb.answer()


@ad_broadcast_router.callback_query(F.data.startswith("adb:pickproduct:"))
async def link_product_pick(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == row.id))
        if cfg is None:
            cfg = AdBroadcast(bot_id=row.id)
            s.add(cfg)
        cfg.linked_product_id = pid
        await s.commit()
    text, kb = await _render_panel(row.id)
    await cb.message.edit_text("✅ محصول وصل شد.\n\n" + text, reply_markup=kb, parse_mode="HTML",
                               disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.callback_query(F.data == "adb:unlink_product")
async def link_product_remove(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == row.id))
        if cfg:
            cfg.linked_product_id = None
            await s.commit()
    text, kb = await _render_panel(row.id)
    await cb.message.edit_text("🔓 اتصال قطع شد.\n\n" + text, reply_markup=kb, parse_mode="HTML",
                               disable_web_page_preview=True)
    await cb.answer()


# ═════════════════════════ ویرایش متن ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:edit_text")
async def edit_text_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    await state.set_state(AdBroadcastFSM.editing_text)
    await state.update_data(bot_id=row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adb:menu", style=ButtonStyle.DANGER)]])
    await cb.message.answer(
        "✍️ <b>متنِ تبلیغت رو بفرست.</b>\n\n"
        "می‌تونی از <b>تمامِ فرمت‌های تلگرام</b> استفاده کنی: بولد، ایتالیک، "
        "اسپویلر، لینک، ایموجی سفارشی، منشن، کد، ...\n\n"
        "📸 برای <b>ارسالِ متن با عکس یا ویدیو</b>، عکس/ویدیو رو با کپشنت بفرست.\n\n"
        "🎁 برای <b>تبلیغ با گیفت پرمیوم</b>، لینکِ گیفتِ خودت "
        "(<code>t.me/nft/…</code>) رو داخلِ متن بذار — گیفت زیرِ همون لینک "
        "نمایش داده و پخش می‌شه.\n\n"
        "💡 هر پیامی که بفرستی، مستقیم به‌عنوانِ تبلیغ ذخیره می‌شه — می‌تونی "
        "از هرجا (نوتِ خودت، پیامِ کانالِ دیگه) کپی و پیست کنی و همهٔ "
        "فرمت‌ها حفظ می‌شن.",
        reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.message(AdBroadcastFSM.editing_text)
async def edit_text_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return

    # تشخیصِ اینکه پیام متنی، عکس یا ویدیو داره
    photo_id = None
    video_id = None
    html_text: str | None = None

    if msg.photo:
        photo_id = msg.photo[-1].file_id
        html_text = msg.html_text if msg.caption else ""  # msg.html_text برای caption
    elif msg.video:
        video_id = msg.video.file_id
        html_text = msg.html_text if msg.caption else ""
    elif msg.text:
        html_text = msg.html_text
    else:
        await msg.answer(
            "❌ فقط پیامِ متنی، عکس یا ویدیو (با کپشن اختیاری) قبوله. "
            "دوباره بفرست یا لغو کن.")
        return

    if not html_text and not photo_id and not video_id:
        await msg.answer("❌ متنِ خالی مجاز نیست. دوباره بفرست.")
        return

    # === PATCH_AD_GIFT_LINK_V1 ===
    # اگه پیامِ متنی (بدونِ عکس/ویدیو) حاوی لینک گیفت (t.me/nft/…) باشه،
    # لینک ذخیره می‌شه تا موقعِ پخش، گیفت پرمیوم زیرِ همون لینک نمایش داده بشه.
    gift_url = None
    if html_text and not photo_id and not video_id:
        m = _GIFT_LINK_RE.search(msg.text or "")
        if m:
            gift_url = m.group(0)

    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == bot_id))
        if cfg is None:
            cfg = AdBroadcast(bot_id=bot_id)
            s.add(cfg)
        cfg.ad_text_html = html_text or None
        cfg.ad_photo_file_id = photo_id
        cfg.ad_video_file_id = video_id
        cfg.ad_gift_url = gift_url
        await s.commit()

    await state.clear()
    text, kb = await _render_panel(bot_id)
    _saved_note = ("✅ متنِ تبلیغ ذخیره شد. 🎁 لینکِ گیفت شناسایی شد — گیفت "
                   "پرمیومت زیرِ لینک نمایش داده می‌شه.\n\n" if gift_url
                   else "✅ متنِ تبلیغ ذخیره شد.\n\n")
    await msg.answer(_saved_note + text,
                     reply_markup=kb, parse_mode="HTML",
                     disable_web_page_preview=True)


# ═════════════════════════ تنظیم بازه ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:edit_interval")
async def edit_interval_start(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    kb_rows = []
    row_pair = []
    for label, minutes in _PRESET_INTERVALS:
        row_pair.append(InlineKeyboardButton(
            text=label, callback_data=f"adb:iv:{minutes}", style=ButtonStyle.PRIMARY))
        if len(row_pair) == 2:
            kb_rows.append(row_pair)
            row_pair = []
    if row_pair:
        kb_rows.append(row_pair)
    kb_rows.append([InlineKeyboardButton(
        text="✏️ سفارشی (به دقیقه)", callback_data="adb:iv:custom", style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adb:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "⏱ <b>بازهٔ ارسال رو انتخاب کن:</b>\n\n"
        "بات بعد از هر بازه، تبلیغ رو به همهٔ کانال/گروه‌های متصل می‌فرسته.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML")
    await cb.answer()


@ad_broadcast_router.callback_query(F.data.startswith("adb:iv:"))
async def edit_interval_pick(cb: CallbackQuery, state: FSMContext, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    val = cb.data.split(":")[2]
    if val == "custom":
        await state.set_state(AdBroadcastFSM.editing_interval)
        await state.update_data(bot_id=row.id)
        await cb.message.answer(
            "✏️ عدد رو به <b>دقیقه</b> بفرست (بین ۵ تا ۴۳٬۲۰۰):\n"
            "مثال: <code>90</code> یعنی هر ۱ ساعت و ۳۰ دقیقه.",
            parse_mode="HTML")
        await cb.answer()
        return
    try:
        minutes = int(val)
    except ValueError:
        await cb.answer("مقدارِ نامعتبر", show_alert=True)
        return
    await _apply_interval(row.id, minutes)
    text, kb = await _render_panel(row.id)
    await cb.message.edit_text(
        f"✅ بازه روی <b>{_fmt_interval(minutes)}</b> تنظیم شد.\n\n" + text,
        reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.message(AdBroadcastFSM.editing_interval, F.text)
async def edit_interval_custom(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    # پشتیبانی از ارقامِ فارسی
    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    txt = txt.translate(trans)
    if not txt.isdigit():
        await msg.answer("❌ فقط عدد بفرست (به دقیقه).")
        return
    minutes = int(txt)
    if minutes < 5 or minutes > 43200:
        await msg.answer("❌ عدد باید بینِ ۵ تا ۴۳٬۲۰۰ (۳۰ روز) باشه.")
        return
    await _apply_interval(bot_id, minutes)
    await state.clear()
    text, kb = await _render_panel(bot_id)
    await msg.answer(
        f"✅ بازه روی <b>{_fmt_interval(minutes)}</b> تنظیم شد.\n\n" + text,
        reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


async def _apply_interval(bot_id: int, minutes: int) -> None:
    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == bot_id))
        if cfg is None:
            cfg = AdBroadcast(bot_id=bot_id, interval_minutes=minutes)
            s.add(cfg)
        else:
            cfg.interval_minutes = minutes
            # اگه فعاله، next_send_at رو بر اساسِ آخرینِ ارسال (یا الان)
            # مجدداً محاسبه کن تا تغییرِ بازه فوراً اعمال شه
            if cfg.is_active:
                base = cfg.last_sent_at or datetime.datetime.now()
                cfg.next_send_at = base + datetime.timedelta(minutes=minutes)
        await s.commit()


# ═════════════════════════ افزودن هدف ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:add_target")
async def add_target_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    await state.set_state(AdBroadcastFSM.adding_target)
    await state.update_data(bot_id=row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adb:menu", style=ButtonStyle.DANGER)]])
    await cb.message.answer(
        "➕ <b>افزودنِ کانال یا گروهِ جدید</b>\n\n"
        "۱) اول این باتِ تبلیغاتی رو <b>ادمینِ اون کانال یا گروه کن</b> با "
        "دسترسیِ «ارسال پیام».\n\n"
        "۲) بعد یکی از این کارها رو کن:\n"
        "  • <b>یه پیام از اون کانال/گروه</b> رو اینجا <b>فوروارد کن</b> (ساده‌ترین راه)\n"
        "  • یا <b>Chat ID</b> رو مستقیم بفرست (مثل <code>-100123456789</code>)\n\n"
        "⚠️ اگه بات ادمین نباشه، این افزودن با خطا رد می‌شه.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@ad_broadcast_router.message(AdBroadcastFSM.adding_target)
async def add_target_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return

    chat_id: int | None = None
    # === PATCH_AD_BROADCAST_FORWARD_ORIGIN_V1 ===
    # forward_from_chat فیلدِ قدیمیه (Bot API <7.0) و دیگه همیشه پر نمی‌شه؛
    # اول باید forward_origin (فیلدِ جدید) رو چک کنیم.
    fwd_origin = getattr(msg, "forward_origin", None)
    fwd_origin_type = getattr(fwd_origin, "type", None)
    if fwd_origin_type == "channel":
        chat_id = fwd_origin.chat.id
    elif fwd_origin_type == "chat":
        chat_id = fwd_origin.sender_chat.id
    # اول: فوروارد از کانال؟
    elif getattr(msg, "forward_from_chat", None) is not None:
        chat_id = msg.forward_from_chat.id
    # فوروارد از کاربر: هدفِ معتبر نیست
    elif fwd_origin_type in ("user", "hidden_user") or getattr(msg, "forward_from", None) is not None:
        await msg.answer(
            "❌ این فوروارد از یه <b>کاربر</b>ه، نه کانال/گروه. "
            "لطفاً از خودِ کانال/گروه پیام فوروارد کن.", parse_mode="HTML")
        return
    else:
        # شاید Chat ID مستقیم فرستاده
        raw = (msg.text or "").strip()
        trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        raw = raw.translate(trans).replace(" ", "")
        try:
            chat_id = int(raw)
        except (ValueError, TypeError):
            await msg.answer(
                "❌ نه فورواردی دیدم نه Chat ID معتبر. دوباره تلاش کن یا لغو کن.")
            return

    # تأیید: بات باید بتونه get_chat کنه (یعنی عضو/ادمین اون‌جاست)
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        await msg.answer(
            f"❌ نتونستم به این چت دسترسی داشته باشم: <code>{_html.escape(str(e))}</code>\n\n"
            "مطمئن شو بات رو به اون کانال/گروه اضافه کردی و ادمینش کردی.",
            parse_mode="HTML")
        return

    # بررسی: بات باید ادمین باشه با دسترسی ارسال پیام
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status not in ("administrator", "creator"):
            await msg.answer(
                "❌ بات هنوز ادمینِ این چت نیست. اول ادمینش کن (با دسترسیِ "
                "«ارسال پیام») و بعد دوباره تلاش کن.")
            return
    except Exception:
        # اگه get_chat_member شکست خورد، ادامه بده — get_chat موفق بود پس
        # حداقل عضوه.
        pass

    # کِند: کانال یا گروه؟
    kind = "channel" if chat.type == "channel" else (
        "supergroup" if chat.type == "supergroup" else "group")

    # ذخیره
    async with Session() as s:
        existing = await s.scalar(select(AdBroadcastTarget).where(
            AdBroadcastTarget.bot_id == bot_id,
            AdBroadcastTarget.chat_id == chat_id))
        if existing:
            # از قبل هست — دوباره فعالش کن
            existing.disabled = False
            existing.last_error = None
            existing.title = chat.title
            existing.kind = kind
            already = True
        else:
            s.add(AdBroadcastTarget(
                bot_id=bot_id, chat_id=chat_id, title=chat.title, kind=kind))
            already = False
        await s.commit()

    await state.clear()
    title = _html.escape(chat.title or str(chat_id))
    if already:
        text, kb = await _render_panel(bot_id)
        await msg.answer(
            f"ℹ️ <b>{title}</b> از قبل توی لیست بود — دوباره فعال شد.\n\n" + text,
            reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    else:
        text, kb = await _render_panel(bot_id)
        await msg.answer(
            f"✅ <b>{title}</b> به لیست اضافه شد.\n\n" + text,
            reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


# ═════════════════════════ لیست هدف‌ها ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:list_targets")
async def list_targets(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    async with Session() as s:
        targets = (await s.scalars(select(AdBroadcastTarget).where(
            AdBroadcastTarget.bot_id == row.id).order_by(
            AdBroadcastTarget.added_at.desc()))).all()

    if not targets:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن اولین کانال/گروه",
                                  callback_data="adb:add_target", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adb:menu", style=ButtonStyle.DANGER)],
        ])
        await cb.message.edit_text("📋 هنوز کانال یا گروهی متصل نکردی.",
                                    reply_markup=kb)
        await cb.answer()
        return

    lines = ["📋 <b>لیست کانال/گروه‌های متصل:</b>\n"]
    kb_rows = []
    for t in targets:
        icon = "📢" if t.kind == "channel" else "👥"
        status = "" if not t.disabled else " ⛔️"
        title = _html.escape(t.title or str(t.chat_id))
        lines.append(f"{icon} <b>{title}</b>{status}\n    "
                     f"<code>{t.chat_id}</code>")
        if t.last_error:
            _err = _html.escape(t.last_error[:80])
            lines.append(f"    <i>آخرین خطا: {_err}</i>")
        kb_rows.append([InlineKeyboardButton(
            text=f"🗑 حذف «{(t.title or str(t.chat_id))[:40]}»",
            callback_data=f"adb:rm:{t.id}")])
    kb_rows.append([InlineKeyboardButton(text="➕ افزودن دیگر",
                                          callback_data="adb:add_target", style=ButtonStyle.SUCCESS),
                    InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adb:menu", style=ButtonStyle.DANGER)])

    await cb.message.edit_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.callback_query(F.data.startswith("adb:rm:"))
async def remove_target(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    try:
        target_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer()
        return
    async with Session() as s:
        t = await s.scalar(select(AdBroadcastTarget).where(
            AdBroadcastTarget.id == target_id,
            AdBroadcastTarget.bot_id == row.id))
        if t:
            await s.delete(t)
            await s.commit()
    await cb.answer("حذف شد.")
    # لیست رو دوباره نشون بده
    await list_targets(cb, bot)


# ═════════════════════════ فعال/غیرفعال ═════════════════════════

@ad_broadcast_router.callback_query(F.data == "adb:toggle")
async def toggle_active(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    async with Session() as s:
        cfg = await s.scalar(select(AdBroadcast).where(AdBroadcast.bot_id == row.id))
        if cfg is None:
            cfg = AdBroadcast(bot_id=row.id)
            s.add(cfg)

        if not cfg.is_active:
            # بررسی‌های قبل از فعال‌سازی
            if not cfg.ad_text_html and not cfg.ad_photo_file_id and not cfg.ad_video_file_id:
                await cb.answer(
                    "❌ اول متنِ تبلیغ رو تنظیم کن.", show_alert=True)
                return
            targets = (await s.scalars(select(AdBroadcastTarget).where(
                AdBroadcastTarget.bot_id == row.id,
                AdBroadcastTarget.disabled == False))).all()
            if not targets:
                await cb.answer(
                    "❌ حداقل یک کانال یا گروه باید متصل باشه.", show_alert=True)
                return
            cfg.is_active = True
            # اولین ارسال: همین الان (به‌جای اینکه یه بازهٔ کامل صبر کنه)
            cfg.next_send_at = datetime.datetime.now()
        else:
            cfg.is_active = False
            cfg.next_send_at = None
        await s.commit()

    text, kb = await _render_panel(row.id)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                disable_web_page_preview=True)
    status = "فعال" if cfg.is_active else "غیرفعال"
    await cb.answer(f"وضعیت: {status}")


# ═════════════════════════ اسکجولرِ پس‌زمینه ═════════════════════════

async def _build_product_button(bot_username: str | None, product_id: int | None):
    """اگه محصولی به این تبلیغ وصل باشه، یه دکمهٔ آبیِ اینلاین (URL، نه
    callback_data — چون این پیام تو یه کانال/گروهه و ممکنه بیننده هنوز
    اصلاً با بات چت نکرده باشه) با دیپ‌لینکِ prod_<id> می‌سازه که مستقیم
    کاربر رو به صفحهٔ همون محصول داخلِ بات می‌بره.
    اگه محصول حذف شده باشه یا یوزرنیمِ بات در دسترس نباشه، None برمی‌گردونه
    (تبلیغ بدونِ دکمه فرستاده می‌شه، نه اینکه کرش کنه)."""
    if not product_id or not bot_username:
        return None
    try:
        from models import Product
        async with Session() as s:
            p = await s.get(Product, product_id)
        if not p:
            return None
        label = f"🛍 خرید {p.title}"[:64]
        url = f"https://t.me/{bot_username}?start=prod_{product_id}"
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (ساختِ دکمهٔ محصولِ تبلیغ)")
        return None


async def _send_one_ad(bot: Bot, cfg, target,
                       reply_markup: InlineKeyboardMarkup | None = None) -> tuple[bool, str | None]:
    """یه بار تبلیغ رو به یک هدف می‌فرسته. خروجی: (موفق_بود, پیامِ_خطا)"""
    try:
        if cfg.ad_photo_file_id:
            await bot.send_photo(
                target.chat_id, photo=cfg.ad_photo_file_id,
                caption=cfg.ad_text_html or None, parse_mode="HTML",
                reply_markup=reply_markup)
        elif cfg.ad_video_file_id:
            await bot.send_video(
                target.chat_id, video=cfg.ad_video_file_id,
                caption=cfg.ad_text_html or None, parse_mode="HTML",
                reply_markup=reply_markup)
        else:
            # === PATCH_AD_GIFT_LINK_V1 ===
            # اگه لینک گیفت تنظیم شده، پیش‌نمایشِ بزرگِ همون لینک فعال می‌شه
            # تا گیفت پرمیوم زیرِ متن/لینک نمایش داده بشه.
            _gift = getattr(cfg, "ad_gift_url", None)
            if _gift:
                from aiogram.types import LinkPreviewOptions
                await bot.send_message(
                    target.chat_id, text=cfg.ad_text_html or "",
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(
                        url=_gift, prefer_large_media=True,
                        show_above_text=False),
                    reply_markup=reply_markup)
            else:
                await bot.send_message(
                    target.chat_id, text=cfg.ad_text_html or "",
                    parse_mode="HTML", disable_web_page_preview=False,
                    reply_markup=reply_markup)
        return True, None
    except Exception as e:
        return False, str(e)[:250]


async def _process_due_broadcasts() -> int:
    """یه پاسِ کامل رویِ همهٔ کانفیگ‌های فعالِ سررسیده. تعدادِ ارسال‌های
    انجام‌شده رو برمی‌گردونه.

    === PATCH_AD_BROADCAST_ENTRYPOINT_AGNOSTIC_V1 ===
    قبلاً اینجا صراحتاً `from main import _get_tenant_bot` بود — فرض بر این
    بود که main.py همیشه تنها نقطهٔ ورودِ درحالِ‌اجراست. از وقتی
    main_platform.py/main_tenant.py هم اضافه شدن، این فرض دیگه همیشه درست
    نیست: اگه این حلقه زیرِ main_tenant.py اجرا بشه، ایمپورتِ صریحِ «main»
    باعث می‌شد پایتون سعی کنه main.py رو از نو لود کنه — و چون بعضی
    آبجکت‌ها (مثلِ tenant_router) بینِ ماژول‌ها مشترکن، این با خطای
    «Router is already attached» کرش می‌کرد (دقیقاً چیزی که تو تستِ زنده
    دیده شد). الان به‌جای ایمپورتِ تازه، اول از sys.modules می‌بینیم کدوم
    نقطهٔ ورود همین الان واقعاً درحالِ‌اجراست و از همون _get_tenant_bot رو
    می‌گیریم؛ فقط اگه هیچ‌کدوم از قبل لود نشده بودن (حالتِ غیرمنتظره)،
    به‌عنوانِ fallback ایمپورتِ تازهٔ main.py (رفتارِ قدیمی) انجام می‌شه.
    """
    import sys
    _entry = sys.modules.get("main_tenant") or sys.modules.get("main")
    if _entry is None:
        import main as _entry
    _get_tenant_bot = _entry._get_tenant_bot
    now = datetime.datetime.now()
    async with Session() as s:
        due_cfgs = (await s.scalars(select(AdBroadcast).where(
            AdBroadcast.is_active == True,
            AdBroadcast.next_send_at != None,
            AdBroadcast.next_send_at <= now))).all()
        # snapshot: (cfg_id, bot_id, interval)
        due_snap = [(c.id, c.bot_id, c.interval_minutes) for c in due_cfgs]

    sent_total = 0
    for cfg_id, bot_id, interval in due_snap:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active":
            # ربات پاز/قفل/بایگانی — کانفیگ رو ولش کن، ولی next_send_at رو
            # هم به‌روز نکن تا وقتی active شد بلافاصله بفرسته
            continue
        tbot, tstatus = await _get_tenant_bot(brow.token)
        if not tbot or tstatus != "active":
            continue

        # کانفیگ و هدف‌ها رو تازه بخون (شاید ادمین وسطِ کار عوضشون کرده)
        async with Session() as s:
            cfg = await s.get(AdBroadcast, cfg_id)
            if not cfg or not cfg.is_active:
                continue
            targets = (await s.scalars(select(AdBroadcastTarget).where(
                AdBroadcastTarget.bot_id == bot_id,
                AdBroadcastTarget.disabled == False))).all()
            targets_snap = [(t.id, t.chat_id) for t in targets]
            # کپیِ فیلدها که برای ارسال لازمن (تا session رو زودتر ببندیم)
            _text = cfg.ad_text_html
            _photo = cfg.ad_photo_file_id
            _video = cfg.ad_video_file_id
            _gift = getattr(cfg, "ad_gift_url", None)  # PATCH_AD_GIFT_LINK_V1
            _product_id = cfg.linked_product_id

        if not targets_snap:
            # هدف نداره؛ next_send_at رو جلو ببر تا loop رو خراب نکنه
            async with Session() as s:
                cfg = await s.get(AdBroadcast, cfg_id)
                if cfg:
                    cfg.next_send_at = now + datetime.timedelta(minutes=interval)
                    await s.commit()
            continue

        # آبجکتِ کوچیکِ کانفیگ برای پاس دادن به _send_one_ad
        class _MiniCfg:
            ad_text_html = _text
            ad_photo_file_id = _photo
            ad_video_file_id = _video
            ad_gift_url = _gift  # PATCH_AD_GIFT_LINK_V1
        mcfg = _MiniCfg()
        # === PATCH_AD_PRODUCT_BUTTON_V1 ===
        # اگه محصولی وصل شده، دکمه رو یه‌بار (نه دوباره برای هر هدف) می‌سازیم
        ad_kb = await _build_product_button(brow.username, _product_id)

        # ارسال به هر هدف
        succ = 0
        for target_id, chat_id in targets_snap:
            class _MiniT:
                pass
            mt = _MiniT()
            mt.chat_id = chat_id
            ok, err = await _send_one_ad(tbot, mcfg, mt, reply_markup=ad_kb)
            async with Session() as s:
                t = await s.get(AdBroadcastTarget, target_id)
                if not t:
                    continue
                if ok:
                    t.last_sent_at = now
                    t.last_error = None
                    succ += 1
                else:
                    t.last_error = err
                    # اگه چندبارِ پی‌درپی خطا داد، auto-disable
                    # (نگه‌داشتنِ شمارنده‌ی جدا لازم نیست — از پیامِ خطا استفاده نمی‌کنیم؛
                    # ساده‌سازی: اگه پیامِ خطا شاملِ کلماتِ کلیدیِ «forbidden»،
                    # «chat not found»، «bot was kicked» بود، فوراً disable کن)
                    lower = (err or "").lower()
                    if any(k in lower for k in ("forbidden", "bot was kicked",
                                                  "chat not found", "chat_not_found",
                                                  "bot was blocked", "not enough rights",
                                                  "user_deactivated")):
                        t.disabled = True
                await s.commit()
            # فاصلهٔ کوتاه بین ارسال‌ها تا rate-limit تلگرام رو رد نکنه
            await asyncio.sleep(0.2)

        # به‌روز کردنِ کانفیگ
        async with Session() as s:
            cfg = await s.get(AdBroadcast, cfg_id)
            if cfg:
                cfg.last_sent_at = now
                cfg.total_sent = (cfg.total_sent or 0) + succ
                cfg.next_send_at = now + datetime.timedelta(minutes=interval)
                await s.commit()
        sent_total += succ

    return sent_total


async def ad_broadcast_loop():
    """حلقهٔ پس‌زمینه — از main.py با asyncio.create_task فراخوانی می‌شه."""
    await asyncio.sleep(45)  # صبر کن تا بات‌های مستأجر warm بشن
    while True:
        try:
            n = await _process_due_broadcasts()
            if n:
                logger.info(f"ad_broadcast_loop: {n} ارسالِ موفق")
        except Exception:
            logger.exception("ad_broadcast_loop: خطای نادیده‌گرفته‌شده")
        # هر ۳۰ ثانیه چک کن. برای بازه‌های به‌واحدِ ساعت/روز این کاملاً کافیه.
        await asyncio.sleep(30)


# ═══════════════ PATCH_AD_ONETIME_SCHEDULE_V1 ═══════════════
# پست‌های «یک‌بارهٔ زمان‌بندی‌شده» — دقیقاً یک‌بار در یک تاریخ/ساعتِ مشخص
# به همون کانال/گروه‌هایی که برای تبلیغِ چرخشی وصل شدن فرستاده می‌شن.
# کاملاً مستقل از AdBroadcastِ چرخشی — هردو می‌تونن هم‌زمان فعال باشن.

class AdOnceFSM(StatesGroup):
    editing_text = State()      # منتظرِ متن/عکس/ویدیوی پست
    editing_datetime = State()  # منتظرِ تاریخ/ساعتِ ارسال


def _jalali_dt(dt: datetime.datetime | None) -> str:
    if not dt:
        return "—"
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=dt).strftime("%Y/%m/%d — %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


async def _render_once_list(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from models import AdScheduledPost
    async with Session() as s:
        rows = (await s.scalars(select(AdScheduledPost).where(
            AdScheduledPost.bot_id == bot_id
        ).order_by(AdScheduledPost.scheduled_at.desc()).limit(20))).all()
    kb_rows = []
    lines = ["🗓 <b>پست‌های یک‌بارهٔ زمان‌بندی‌شده</b>\n",
             "این پست‌ها جدا از تبلیغِ چرخشیِ بالا هستن — هرکدوم دقیقاً یک‌بار، "
             "در تاریخ/ساعتی که مشخص می‌کنی، به همون کانال/گروه‌های وصل‌شده فرستاده می‌شه.\n"]
    if not rows:
        lines.append("هنوز پستی زمان‌بندی نشده.")
    for r in rows:
        icon = {"pending": "⏳", "sent": "✅", "failed": "⚠️"}.get(r.status, "❔")
        preview = (r.ad_text_html or "").replace("<", "&lt;")[:30] or "(بدون متن)"
        lines.append(f"{icon} {_jalali_dt(r.scheduled_at)} — {preview}")
        if r.status == "pending":
            kb_rows.append([InlineKeyboardButton(
                text=f"❌ لغوِ پستِ {_jalali_dt(r.scheduled_at)}",
                callback_data=f"adbo:cancel:{r.id}", style=ButtonStyle.DANGER)])
    kb_rows.append([InlineKeyboardButton(text="➕ افزودنِ پستِ جدید", callback_data="adbo:new", style=ButtonStyle.SUCCESS)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adb:menu", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@ad_broadcast_router.callback_query(F.data == "adbo:menu")
async def once_menu(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    text, kb = await _render_once_list(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML",
                                    disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML",
                                 disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.callback_query(F.data == "adbo:new")
async def once_new_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    await state.set_state(AdOnceFSM.editing_text)
    await state.update_data(bot_id=row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)]])
    await cb.message.answer(
        "✍️ <b>متنِ این پستِ یک‌بارهٔ تبلیغاتی رو بفرست.</b>\n\n"
        "می‌تونی از تمامِ فرمت‌های تلگرام استفاده کنی؛ برای همراهیِ عکس/ویدیو، "
        "اون رو با کپشنت بفرست.",
        reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await cb.answer()


@ad_broadcast_router.message(AdOnceFSM.editing_text)
async def once_text_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return
    photo_id = None
    video_id = None
    html_text: str | None = None
    if msg.photo:
        photo_id = msg.photo[-1].file_id
        html_text = msg.html_text if msg.caption else ""
    elif msg.video:
        video_id = msg.video.file_id
        html_text = msg.html_text if msg.caption else ""
    elif msg.text:
        html_text = msg.html_text
    else:
        await msg.answer("❌ فقط پیامِ متنی، عکس یا ویدیو (با کپشن اختیاری) قبوله. دوباره بفرست یا لغو کن.")
        return
    if not html_text and not photo_id and not video_id:
        await msg.answer("❌ متنِ خالی مجاز نیست. دوباره بفرست.")
        return

    # === PATCH_AD_GIFT_LINK_V1 === تشخیص لینک گیفت در پستِ یک‌باره
    once_gift = None
    if html_text and not photo_id and not video_id:
        _m = _GIFT_LINK_RE.search(msg.text or "")
        if _m:
            once_gift = _m.group(0)

    await state.update_data(once_text=html_text, once_photo=photo_id, once_video=video_id,
                            once_gift=once_gift)
    from models import Product
    async with Session() as s:
        has_products = await s.scalar(select(Product.id).where(
            Product.bot_id == bot_id, Product.is_active == True).limit(1))
    if has_products:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 اتصال به محصول", callback_data="adbo:linkprod", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⏭ رد کردن (بدونِ دکمه)", callback_data="adbo:skipprod", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)],
        ])
        await msg.answer(
            "🔗 می‌خوای زیرِ این پست هم یه دکمهٔ آبیِ محصول باشه؟",
            reply_markup=kb)
        return
    await state.set_state(AdOnceFSM.editing_datetime)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)]])
    await msg.answer(
        "📅 <b>تاریخ و ساعتِ ارسال رو بفرست.</b>\n\n"
        "فرمت: <code>1405/05/10 14:30</code> (شمسی) یا <code>2026-08-01 14:30</code> (میلادی)\n"
        "باید در آینده باشه.",
        reply_markup=kb, parse_mode="HTML")


@ad_broadcast_router.callback_query(F.data == "adbo:skipprod")
async def once_skip_product(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdOnceFSM.editing_datetime)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)]])
    await cb.message.edit_text(
        "📅 <b>تاریخ و ساعتِ ارسال رو بفرست.</b>\n\n"
        "فرمت: <code>1405/05/10 14:30</code> (شمسی) یا <code>2026-08-01 14:30</code> (میلادی)\n"
        "باید در آینده باشه.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@ad_broadcast_router.callback_query(F.data == "adbo:linkprod")
async def once_link_product_menu(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await cb.answer()
        return
    from models import Product
    async with Session() as s:
        products = (await s.scalars(select(Product).where(
            Product.bot_id == bot_id, Product.is_active == True
        ).order_by(Product.sort_order, Product.id))).all()
    kb_rows = [[InlineKeyboardButton(text=f"🛍 {p.title}", callback_data=f"adbo:pickprod:{p.id}", style=ButtonStyle.PRIMARY)]
              for p in products]
    kb_rows.append([InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text("کدوم محصول؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@ad_broadcast_router.callback_query(F.data.startswith("adbo:pickprod:"))
async def once_link_product_pick(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.update_data(once_product_id=pid)
    await state.set_state(AdOnceFSM.editing_datetime)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="لغو", callback_data="adbo:menu", style=ButtonStyle.DANGER)]])
    await cb.message.edit_text(
        "📅 <b>تاریخ و ساعتِ ارسال رو بفرست.</b>\n\n"
        "فرمت: <code>1405/05/10 14:30</code> (شمسی) یا <code>2026-08-01 14:30</code> (میلادی)\n"
        "باید در آینده باشه.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@ad_broadcast_router.message(AdOnceFSM.editing_datetime, F.text)
async def once_datetime_save(msg: Message, state: FSMContext, bot: Bot):
    from platform_bot import parse_fa_datetime
    from models import AdScheduledPost
    dt = parse_fa_datetime(msg.text or "")
    if not dt:
        await msg.answer(
            "❌ فرمت درست نیست. دوباره امتحان کن — مثلاً: <code>1405/05/10 14:30</code>",
            parse_mode="HTML")
        return
    if dt <= datetime.datetime.now():
        await msg.answer("❌ این تاریخ گذشته یا همین الانه. یه تاریخِ آینده بفرست.")
        return

    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return
    await state.clear()

    async with Session() as s:
        post = AdScheduledPost(
            bot_id=bot_id,
            ad_text_html=data.get("once_text") or None,
            ad_photo_file_id=data.get("once_photo"),
            ad_video_file_id=data.get("once_video"),
            ad_gift_url=data.get("once_gift"),  # PATCH_AD_GIFT_LINK_V1
            linked_product_id=data.get("once_product_id"),
            scheduled_at=dt,
            status="pending",
        )
        s.add(post)
        await s.commit()

    text, kb = await _render_once_list(bot_id)
    await msg.answer(
        f"✅ پست برای <b>{_jalali_dt(dt)}</b> زمان‌بندی شد.\n\n" + text,
        reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@ad_broadcast_router.callback_query(F.data.startswith("adbo:cancel:"))
async def once_cancel(cb: CallbackQuery, bot: Bot):
    row = await _is_owner(cb, bot)
    if not row or row.field not in ("ad_bot", "digital_shop"):
        await cb.answer()
        return
    from models import AdScheduledPost
    post_id = int(cb.data.split(":")[2])
    async with Session() as s:
        post = await s.get(AdScheduledPost, post_id)
        if post and post.bot_id == row.id and post.status == "pending":
            await s.delete(post)
            await s.commit()
    text, kb = await _render_once_list(row.id)
    await cb.message.edit_text("❌ پست لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML",
                               disable_web_page_preview=True)
    await cb.answer()


async def _process_due_once_posts() -> int:
    """پست‌های یک‌بارهٔ سررسیده رو به همهٔ هدف‌های فعالِ همون بات می‌فرسته."""
    from models import AdScheduledPost
    import sys
    _entry = sys.modules.get("main_tenant") or sys.modules.get("main")
    if _entry is None:
        import main as _entry
    _get_tenant_bot = _entry._get_tenant_bot

    now = datetime.datetime.now()
    async with Session() as s:
        due = (await s.scalars(select(AdScheduledPost).where(
            AdScheduledPost.status == "pending",
            AdScheduledPost.scheduled_at <= now))).all()
        due_snap = [(p.id, p.bot_id) for p in due]

    sent_total = 0
    for post_id, bot_id in due_snap:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow or brow.status != "active":
            continue  # وقتی بات فعال شد، همون لحظه‌ی بعدی سعی می‌کنیم (فرصت رو از دست نمی‌ده)
        tbot, tstatus = await _get_tenant_bot(brow.token)
        if not tbot or tstatus != "active":
            continue

        async with Session() as s:
            post = await s.get(AdScheduledPost, post_id)
            if not post or post.status != "pending":
                continue
            targets = (await s.scalars(select(AdBroadcastTarget).where(
                AdBroadcastTarget.bot_id == bot_id,
                AdBroadcastTarget.disabled == False))).all()
            targets_snap = [(t.id, t.chat_id) for t in targets]
            _text = post.ad_text_html
            _photo = post.ad_photo_file_id
            _video = post.ad_video_file_id
            _gift = getattr(post, "ad_gift_url", None)  # PATCH_AD_GIFT_LINK_V1
            _product_id = post.linked_product_id

        class _MiniCfg:
            ad_text_html = _text
            ad_photo_file_id = _photo
            ad_video_file_id = _video
            ad_gift_url = _gift  # PATCH_AD_GIFT_LINK_V1
        mcfg = _MiniCfg()
        ad_kb = await _build_product_button(brow.username, _product_id)

        succ = 0
        for target_id, chat_id in targets_snap:
            class _MiniT:
                pass
            mt = _MiniT()
            mt.chat_id = chat_id
            ok, err = await _send_one_ad(tbot, mcfg, mt, reply_markup=ad_kb)
            if ok:
                succ += 1
                async with Session() as s:
                    t = await s.get(AdBroadcastTarget, target_id)
                    if t:
                        t.last_sent_at = now
                        await s.commit()
            await asyncio.sleep(0.2)

        async with Session() as s:
            post = await s.get(AdScheduledPost, post_id)
            if post:
                post.status = "sent" if succ else "failed"
                post.sent_at = now
                post.sent_count = succ
                await s.commit()
        sent_total += succ

    return sent_total


async def ad_scheduled_posts_loop():
    """حلقهٔ پس‌زمینهٔ جداگانه برای پست‌های یک‌بارهٔ زمان‌بندی‌شده — هر ۶۰ ثانیه
    چک می‌کنه، چون این‌ها معمولاً برای یه لحظهٔ دقیق (نه یه بازهٔ ساعتی)
    تنظیم می‌شن و تأخیرِ زیاد قابل‌قبول نیست."""
    await asyncio.sleep(50)
    while True:
        try:
            n = await _process_due_once_posts()
            if n:
                logger.info(f"ad_scheduled_posts_loop: {n} ارسالِ موفق")
        except Exception:
            logger.exception("ad_scheduled_posts_loop: خطای نادیده‌گرفته‌شده")
        await asyncio.sleep(60)
