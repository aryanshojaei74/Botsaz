"""
panelyar.py — PATCH_PANEL_YAR_V1

«پنل‌یار VPN»: ابزارِ مدیریتِ پنل‌هایِ VPN برایِ خودِ مالکِ ربات — نه یک
رباتِ فروش. این ماژول مخصوصِ رباتِ مستأجری با field == "panel_yar" است
(نگاه کن به platform_bot.py: FIELDS / ACTIVE_FIELDS / FIELD_PITCH و
action_kb برایِ اکشنِ "pyar_panels"، و tenant/__init__.py برایِ نقطهٔ
ورودش در _run_button_action).

طرحِ کلی (سه مرحله — هر سه کامل است، PATCH_PANEL_YAR_BACKUP_MIGRATE_V1):
  ۱) 👥 نمایشِ کاربرانِ هر پنل با حجم/انقضایِ باقی‌مانده
  ۲) 💾 بکاپِ JSON از کاربرانِ یک پنل (فایل داخلِ چت ارسال می‌شود)
  ۳) 🔁 انتقال/کپیِ کاربران بینِ دو پنل (حتی دو نوعِ متفاوت)

هیچ کدِ کلاینتِ پنلی اینجا کپی نشده: از همون جدولِ VpnPanel و همون
tenant.shared.panel_clients.vpn_panel_factory.make_panel_client استفاده می‌شود که رباتِ
فروشِ VPN هم استفاده می‌کند — این دو تا کاملاً مستقل از aiogram/Session
هستند (فقط aiohttp)، پس import‌شان هیچ کدِ اجراییِ رباتِ فروش را لود
نمی‌کند و تغییرِ این فایل به آن ربات آسیبی نمی‌رساند.

اتصالِ حساب‌ها: هر پنل یک ردیفِ VpnPanel با bot_id == این رباتِ پنل‌یار
است — کاملاً مجزا از پنل‌هایی که یک رباتِ فروشِ VPن ممکن است ثبت کرده
باشد (چون bot_id فرق دارد)، حتی اگر هر دو رباتِ روی یک اکانتِ همان
مالک ساخته شده باشند.

callbackها: همه با پیشوند pyar: (نگاه کن به references/callback-map.md)
  pyar:menu                 — لیستِ پنل‌های ثبت‌شده + دکمهٔ افزودن
  pyar:add                  — شروعِ افزودنِ پنلِ جدید (انتخابِ نوع)
  pyar:addtype:{key}        — نوعِ پنل انتخاب شد → پرسیدنِ آدرس
  pyar:cancel                — لغوِ ویزاردِ افزودن
  pyar:panel:{id}            — منویِ مدیریتِ یک پنل
  pyar:del:{id} / pyar:del_yes:{id} — حذفِ پنل
  pyar:users:{id}:{page}     — لیستِ کاربرانِ پنل (حجم/انقضایِ باقی‌مانده)
  pyar:idle:{id}:{days}      — کاربرانِ فعالِ بدونِ اتصال (۲/۷/۳۰ روز)
  pyar:backup:{id}           — بکاپِ JSON از کاربرانِ پنل
  pyar:migrate:{id}          — انتخابِ پنلِ مقصد
  pyar:migto:{src}:{dst}     — صفحهٔ پیش‌نمایش/تأیید
  pyar:migmode:{src}:{dst}:{r|t} — انتخابِ حجمِ منتقل‌شونده (باقی‌مانده/کل)
  pyar:miggo:{src}:{dst}:{r|t}   — شروعِ انتقال
"""
from __future__ import annotations

import contextvars
from html import escape as _esc_html

import asyncio
import datetime as _dt
import json
import math
import time

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from config import Session
from models import VpnPanel
from tenant.shared.panel_clients.vpn_panel import PanelError
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client, make_panel_client_from_fields
from tenant.bots.vpn.vpn_admin import (
    PANEL_LABELS, PANEL_KEYS, get_all_panels,
    panel_base_key, is_panel_key, next_panel_instance_key,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panelyar_router = Router()

# === PATCH_PANELYAR_AUTHZ_V1 ===
# قبلاً هیچ هندلری در این روتر چک نمی‌کرد کاربر مالک/ادمینِ ربات است، و
# پنل‌ها فقط با id (بدونِ bot_id) خوانده می‌شدند. یعنی هر کاربرِ هر رباتی با
# یک callback_dataِ دست‌ساز (مثلاً pyar:del_yes:<id> یا pyar:users:<id>:0)
# می‌توانست پنلِ رباتِ دیگری را حذف کند یا کاربران/کانفیگ‌هایش را ببیند.
# حالا: (۱) کلِ روتر فقط برایِ مالک/ادمینِ همین ربات است؛ (۲) هر جستجویِ پنل
# به رباتِ جاری محدود است.
_CUR_BOT_ID: contextvars.ContextVar = contextvars.ContextVar("pyar_cur_bot_id", default=None)


async def _pyar_admin_only(handler, event, data):
    from tenant import get_bot_row, is_owner_or_admin
    bot = data.get("bot")
    user = getattr(event, "from_user", None)
    row = await get_bot_row(bot) if bot is not None else None
    if not row or not user or not await is_owner_or_admin(row.id, user.id, row.owner_id):
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:
                logger.debug("pyar: پاسخ به callbackِ غیرمجاز ممکن نشد", exc_info=True)
        return None
    token = _CUR_BOT_ID.set(row.id)
    try:
        return await handler(event, data)
    finally:
        _CUR_BOT_ID.reset(token)


panelyar_router.callback_query.middleware(_pyar_admin_only)
panelyar_router.message.middleware(_pyar_admin_only)

_USERS_PAGE_SIZE = 10


class PanelYarFSM(StatesGroup):
    base_url = State()
    username = State()
    password = State()
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    admin_add_username = State()
    admin_add_password = State()
    admin_pw_new = State()
    sub_edit = State()
    restore_file = State()   # PATCH_PANEL_YAR_RESTORE_FILE_V1


# ═══════════════════════ کمک‌تابع‌های عمومی ═══════════════════════

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ لغو", callback_data="pyar:menu", style=ButtonStyle.DANGER)]
    ])


def _fmt_gb(bytes_val: float) -> str:
    gb = bytes_val / (1024 ** 3)
    return f"{gb:,.1f} گیگ"


def _fmt_remaining(client: dict) -> tuple[str, str, bool]:
    """از خروجیِ list_clients() سه‌تایی برمی‌گرداند: (حجمِ باقی‌مانده،
    روزِ باقی‌مانده، آیا فعال است). قراردادِ کلیدها (email/traffic{up,down,
    total}/totalGB/expiryTime/enable) در همهٔ کلاینت‌های پنل یکسان است —
    نگاه کن به main.py:vpn_expiry_loop که همین فرمول را برای همهٔ انواعِ
    پنل به‌کار می‌برد."""
    tr = client.get("traffic") or {}
    used = int(tr.get("up", 0) or 0) + int(tr.get("down", 0) or 0)
    total = int(client.get("totalGB") or tr.get("total") or 0)
    if total <= 0:
        vol_txt = "♾️ نامحدود"
    else:
        remain = max(0, total - used)
        vol_txt = f"{_fmt_gb(remain)} / {_fmt_gb(total)}"

    expiry_ms = int(client.get("expiryTime") or 0)
    if expiry_ms <= 0:
        day_txt = "♾️ بی‌انقضا"
    else:
        now_ms = int(time.time() * 1000)
        remain_days = max(0, (expiry_ms - now_ms) // 86_400_000)
        day_txt = f"{remain_days} روز" if expiry_ms > now_ms else "⛔️ منقضی‌شده"

    enabled = bool(client.get("enable", True))
    return vol_txt, day_txt, enabled


# ═══════════════════════ صفحهٔ اصلی: لیستِ پنل‌ها ═══════════════════════

async def _panels_menu_content(bot_id: int, *, show_back: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    panels = await get_all_panels(bot_id)
    rows = []
    if not panels:
        txt = (
            "🗂 <b>پنل‌یار VPN</b>\n\n"
            "هنوز هیچ پنلی وصل نکرده‌ای. با دکمهٔ زیر اولین پنلت رو اضافه کن:"
        )
    else:
        lines = ["🗂 <b>پنل‌یار VPN</b>\n", "پنل‌هایِ ثبت‌شده:"]
        for p in panels:
            label = PANEL_LABELS.get(p.panel_key, p.panel_key)
            lines.append(f"• {label}")
            rows.append([InlineKeyboardButton(
                text=label, callback_data=f"pyar:panel:{p.id}", style=ButtonStyle.PRIMARY)])
        txt = "\n".join(lines)
    rows.append([InlineKeyboardButton(text="➕ افزودنِ پنلِ جدید", callback_data="pyar:add",
                                      style=ButtonStyle.SUCCESS)])
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    # حالا که پنل‌یار زیرمنویِ «📦 سرویس‌ها و محصولات» ربات‌هایِ فروشِ VPN
    # شده (نه یه نوعِ کاملاً جداگانه‌ی ربات)، این صفحه باید بتونه برگرده.
    # برایِ ربات‌هایِ قدیمیِ field=="panel_yar" (که این صفحه براشون خونهٔ
    # اصلیه، نه یه زیرمنو) show_back=False می‌مونه و رفتارِ قبلی حفظ می‌شه.
    if show_back:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products",
                                          style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_panels_menu(msg: Message, bot_id: int, *, show_back: bool = False) -> None:
    """نقطهٔ ورودِ اکشنِ دکمهٔ اصلی («📡 پنل‌های من») — از
    tenant/__init__.py:_run_button_action صدا زده می‌شود."""
    txt, kb = await _panels_menu_content(bot_id, show_back=show_back)
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


@panelyar_router.callback_query(F.data == "pyar:menu")
async def pyar_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    from tenant import get_bot_row
    brow = await get_bot_row(cb.bot)
    if not brow:
        await cb.answer()
        return
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    show_back = (brow.field == "vpn_sales")
    txt, kb = await _panels_menu_content(brow.id, show_back=show_back)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ═══════════════════════ افزودنِ پنلِ جدید ═══════════════════════

def _add_type_kb() -> InlineKeyboardMarkup:
    rows = []
    row_buf = []
    for key in PANEL_KEYS:
        row_buf.append(InlineKeyboardButton(
            text=PANEL_LABELS[key], callback_data=f"pyar:addtype:{key}", style=ButtonStyle.PRIMARY))
        if len(row_buf) == 2:
            rows.append(row_buf)
            row_buf = []
    if row_buf:
        rows.append(row_buf)
    rows.append([InlineKeyboardButton(text="✖️ لغو", callback_data="pyar:menu", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@panelyar_router.callback_query(F.data == "pyar:add")
async def pyar_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🗂 <b>افزودنِ پنلِ جدید</b>\n\nنوعِ پنل را انتخاب کن:",
        reply_markup=_add_type_kb(), parse_mode="HTML")
    await cb.answer()


def _base_url_hint(key: str) -> str:
    base = panel_base_key(key)
    if base in ("pasargad", "pasarguard"):
        return ("۱/۳ — آدرسِ کاملِ پنل را با https و همراهِ پورت بفرست، مثل:\n"
                "<code>https://panel.example.com:8000</code>")
    if base in ("marzban", "rebecca"):
        return ("۱/۳ — آدرسِ کاملِ پنل را با https و همراهِ پورت بفرست "
                "(بدونِ مسیرِ /dashboard یا /sub در انتها)، مثل:\n"
                "<code>https://panel.example.com:8000</code>")
    if base == "marzneshin":
        return ("۱/۳ — آدرسِ کاملِ پنلِ مرزنشین را با https و همراهِ پورت بفرست، مثل:\n"
                "<code>https://panel.example.com:8000</code>")
    if base == "hiddify":
        return ("۱/۳ — آدرسِ پایهٔ پنلِ هیدیفای را بفرست، <b>بدونِ</b> بخشِ مخفیِ "
                "وسطِ آدرس (اونو مرحلهٔ بعد جدا می‌پرسم)، مثل:\n"
                "<code>https://panel.example.com</code>")
    if base == "upanel_mizban":
        return ("۱/۳ — آدرسِ کاملِ یوپنل (مرزبان) را با https و همراهِ پورت بفرست، مثل:\n"
                "<code>https://panel.example.com:2053</code>")
    if base == "shahan":
        return ("۱/۳ — آدرسِ پایهٔ پنلِ شاهان را بفرست (بدونِ /apiV1/api.php در انتها)، مثل:\n"
                "<code>https://panel.example.com</code>\n\n"
                "⚠️ این پنل با «توکن» کار می‌کند نه یوزرنیم/رمز؛ مرحلهٔ بعد توضیح می‌دم.")
    return ("۱/۳ — آدرسِ کاملِ پنل را با http یا https و همراهِ پورت بفرست، مثل:\n"
            "<code>https://panel.example.com:54321/dhdhdhds</code>")


def _username_prompt(key: str) -> str:
    base = panel_base_key(key)
    if base == "shahan":
        return "۲/۳ — این پنل یوزرنیم ندارد؛ فقط «-» بفرست تا رد شود:"
    if base == "hiddify":
        return ("۲/۳ — بخشِ مخفیِ وسطِ آدرسِ پنل (proxy_path) را بفرست. توی آدرسِ "
                "پنل، بعد از دامنه می‌بینیش؛ مثلاً اگه آدرس اینه:\n"
                "<code>https://panel.example.com/AbCd1234/admin/</code>\n"
                "همون <code>AbCd1234</code> رو بفرست:")
    return "۲/۳ — نامِ کاربریِ ورود به پنل را بفرست:"


def _password_prompt(key: str) -> str:
    base = panel_base_key(key)
    if base == "shahan":
        return "۳/۳ — توکنِ API پنلِ شاهان را بفرست:"
    if base == "hiddify":
        return ("۳/۳ — «Hiddify-API-Key» را بفرست — همون UUIDِ ادمینه؛ از پنل، "
                "بخشِ Admin → Profile قابلِ کپی‌کردنه:")
    return "۳/۳ — رمزِ عبورِ پنل را بفرست:"


@panelyar_router.callback_query(F.data.startswith("pyar:addtype:"))
async def pyar_addtype(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 2)[2]
    if panel_base_key(key) not in PANEL_LABELS:
        await cb.answer("نوعِ پنل نامعتبر", show_alert=True)
        return
    from tenant import get_bot_row
    brow = await get_bot_row(cb.bot)
    if not brow:
        await cb.answer()
        return
    # اگه از این نوع پنل قبلاً ثبت شده، خودکار روی نمونهٔ بعدی (#۲، #۳...) می‌ره
    real_key = await next_panel_instance_key(brow.id, key)
    await state.update_data(panel_key=real_key)
    await state.set_state(PanelYarFSM.base_url)
    label = PANEL_LABELS.get(key, key)
    await cb.message.edit_text(
        f"⚙️ افزودنِ <b>{label}</b>\n\n{_base_url_hint(key)}",
        reply_markup=_cancel_kb(), parse_mode="HTML")
    await cb.answer()


@panelyar_router.message(PanelYarFSM.base_url, F.text)
async def pyar_set_base(msg: Message, state: FSMContext):
    url = (msg.text or "").strip()
    if not url.startswith("http"):
        await msg.answer("آدرس باید با http یا https شروع شود. دوباره بفرست:", reply_markup=_cancel_kb())
        return
    await state.update_data(base_url=url.rstrip("/"))
    await state.set_state(PanelYarFSM.username)
    data = await state.get_data()
    key = data["panel_key"]
    await msg.answer(_username_prompt(key), reply_markup=_cancel_kb(), parse_mode="HTML")


@panelyar_router.message(PanelYarFSM.username, F.text)
async def pyar_set_username(msg: Message, state: FSMContext):
    await state.update_data(username=(msg.text or "").strip())
    await state.set_state(PanelYarFSM.password)
    data = await state.get_data()
    key = data["panel_key"]
    await msg.answer(_password_prompt(key), reply_markup=_cancel_kb(), parse_mode="HTML")


@panelyar_router.message(PanelYarFSM.password, F.text)
async def pyar_set_password(msg: Message, state: FSMContext, bot):
    from tenant import get_bot_row
    data = await state.get_data()
    key = data["panel_key"]
    base_url = data["base_url"]
    username = data.get("username", "")
    password = (msg.text or "").strip()
    await state.clear()

    brow = await get_bot_row(bot)
    if not brow:
        return

    testing = await msg.answer("🔄 در حالِ تستِ اتصال به پنل…")
    try:
        client = make_panel_client_from_fields(
            panel_base_key(key), base_url, username, password)
        ok = await client.test_connection()
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except PanelError as e:
        ok = False
        logger.warning("[panelyar] تستِ اتصال ناموفق: %s", e)
    except Exception:
        ok = False
        logger.exception("[panelyar] خطایِ غیرمنتظره در تستِ اتصال")

    if not ok:
        await testing.edit_text(
            "❌ اتصال به پنل ناموفق بود. آدرس/نام‌کاربری/رمز را دوباره بررسی کن "
            "و از منو دوباره امتحان کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 تلاشِ دوباره", callback_data="pyar:add",
                                     style=ButtonStyle.PRIMARY),
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pyar:menu",
                                     style=ButtonStyle.DANGER),
            ]]))
        return

    async with Session() as s:
        existing = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == brow.id, VpnPanel.panel_key == key))
        if existing:
            existing.base_url = base_url
            existing.username = username
            existing.password = password
            existing.enabled = True
        else:
            s.add(VpnPanel(bot_id=brow.id, panel_key=key, base_url=base_url,
                           username=username, password=password, enabled=True))
        await s.commit()

    label = PANEL_LABELS.get(panel_base_key(key), key)
    txt, kb = await _panels_menu_content(brow.id)
    await testing.edit_text(f"✅ پنلِ <b>{label}</b> با موفقیت وصل شد.\n\n{txt}",
                            reply_markup=kb, parse_mode="HTML")


# ═══════════════════════ منویِ مدیریتِ یک پنل ═══════════════════════

async def _panel_by_id(panel_id: int) -> VpnPanel | None:
    """پنل فقط اگر مالِ رباتِ جاری باشد (PATCH_PANELYAR_AUTHZ_V1)."""
    bot_id = _CUR_BOT_ID.get()
    if bot_id is None:
        return None
    async with Session() as s:
        panel = await s.get(VpnPanel, panel_id)
    return panel if panel is not None and panel.bot_id == bot_id else None


def _panel_menu_kb(panel: VpnPanel) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👥 کاربران (حجم/انقضایِ باقی‌مانده)",
                              callback_data=f"pyar:users:{panel.id}:0", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="😴 کاربرانِ بدونِ اتصال (۲ روز و بیشتر)",
                              callback_data=f"pyar:idle:{panel.id}:2", style=ButtonStyle.PRIMARY)],
    ]
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    # این قابلیت‌ها فقط برایِ خانوادهٔ مرزبان تایید و پیاده‌سازی شدن —
    # چون APIِ رسمیِ مدیریتِ ادمین‌هاش مستند و تست‌شده‌ست. برایِ بقیهٔ
    # پنل‌ها (سنایی/هیدیفای/...) نشون‌داده نمی‌شه تا کاربر گمراه نشه.
    if panel.panel_key == "marzban":
        rows.append([InlineKeyboardButton(text="👤 مدیریتِ ادمین‌های پنل",
                                          callback_data=f"pyar:admins:{panel.id}", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🔗 تغییرِ آدرسِ ساب",
                                          callback_data=f"pyar:subedit:{panel.id}", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🔄 بروزرسانی/نمایشِ اینباندها",
                                          callback_data=f"pyar:inbrefresh:{panel.id}", style=ButtonStyle.PRIMARY)])
    # === PATCH_PANEL_YAR_BACKUP_MIGRATE_V1 ===
    rows.append([InlineKeyboardButton(text="💾 بکاپِ JSON از کاربران",
                                      callback_data=f"pyar:backup:{panel.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🔁 انتقال کاربران به پنلِ دیگر",
                                      callback_data=f"pyar:migrate:{panel.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="📥 ساختِ کاربران از فایلِ بکاپ (روی همین پنل)",
                                      callback_data=f"pyar:fromfile:{panel.id}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🗑 حذفِ این پنل", callback_data=f"pyar:del:{panel.id}",
                                      style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pyar:menu", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@panelyar_router.callback_query(F.data == "pyar:soon")
async def pyar_soon(cb: CallbackQuery):
    await cb.answer("🚧 این قابلیت به‌زودی اضافه می‌شود.", show_alert=True)


@panelyar_router.callback_query(F.data.startswith("pyar:panel:"))
async def pyar_panel_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel:
        await cb.answer("این پنل دیگر وجود ندارد.", show_alert=True)
        return
    label = PANEL_LABELS.get(panel.panel_key, panel.panel_key)
    await cb.message.edit_text(
        f"⚙️ <b>{label}</b>\n\n<code>{panel.base_url}</code>",
        reply_markup=_panel_menu_kb(panel), parse_mode="HTML")
    await cb.answer()


@panelyar_router.callback_query(F.data.startswith("pyar:del:"))
async def pyar_del_confirm(cb: CallbackQuery):
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel:
        await cb.answer("این پنل دیگر وجود ندارد.", show_alert=True)
        return
    label = PANEL_LABELS.get(panel.panel_key, panel.panel_key)
    await cb.message.edit_text(
        f"⚠️ پنلِ <b>{label}</b> حذف شود؟\n\nاعتبارنامهٔ ذخیره‌شده پاک می‌شود؛ "
        "خودِ پنل روی سرورت دست‌نخورده می‌ماند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"pyar:del_yes:{panel_id}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="✖️ نه، بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                  style=ButtonStyle.PRIMARY)],
        ]), parse_mode="HTML")
    await cb.answer()


@panelyar_router.callback_query(F.data.startswith("pyar:del_yes:"))
async def pyar_del_yes(cb: CallbackQuery):
    from tenant import get_bot_row
    panel_id = int(cb.data.split(":")[2])
    async with Session() as s:
        panel = await s.get(VpnPanel, panel_id)
        if panel and panel.bot_id == _CUR_BOT_ID.get():
            await s.delete(panel)
            await s.commit()
    brow = await get_bot_row(cb.bot)
    if not brow:
        await cb.answer()
        return
    txt, kb = await _panels_menu_content(brow.id)
    await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer("حذف شد")


# ═══════════════════════ لیستِ کاربرانِ یک پنل ═══════════════════════

@panelyar_router.callback_query(F.data.startswith("pyar:users:"))
async def pyar_users(cb: CallbackQuery):
    _, _, panel_id_s, page_s = cb.data.split(":")
    panel_id, page = int(panel_id_s), int(page_s)
    panel = await _panel_by_id(panel_id)
    if not panel:
        await cb.answer("این پنل دیگر وجود ندارد.", show_alert=True)
        return
    label = PANEL_LABELS.get(panel.panel_key, panel.panel_key)
    await cb.answer()
    loading = await cb.message.edit_text(f"🔄 در حالِ خواندنِ کاربرانِ <b>{label}</b>…", parse_mode="HTML")

    try:
        client = make_panel_client(panel)
        clients = await client.list_clients()
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except PanelError as e:
        await loading.edit_text(
            f"❌ خواندنِ کاربران ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                     style=ButtonStyle.DANGER)]]), parse_mode="HTML")
        return
    except Exception:
        logger.exception("[panelyar] خواندنِ لیستِ کاربران ناموفق بود (panel_id=%s)", panel_id)
        await loading.edit_text(
            "❌ خطایِ غیرمنتظره در خواندنِ کاربران. دوباره امتحان کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                     style=ButtonStyle.DANGER)]]))
        return

    if not clients:
        await loading.edit_text(
            f"👥 <b>{label}</b>\n\nهیچ کاربری روی این پنل پیدا نشد.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                     style=ButtonStyle.DANGER)]]), parse_mode="HTML")
        return

    total = len(clients)
    start = page * _USERS_PAGE_SIZE
    page_items = clients[start:start + _USERS_PAGE_SIZE]

    lines = [f"👥 <b>{label}</b> — {total} کاربر (صفحهٔ {page + 1})\n"]
    for c in page_items:
        email = c.get("email") or "—"
        vol_txt, day_txt, enabled = _fmt_remaining(c)
        status = "✅" if enabled else "⛔️"
        lines.append(f"{status} <code>{email}</code>\n   📦 {vol_txt} · ⏳ {day_txt}")

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"pyar:users:{panel_id}:{page - 1}"))
    if start + _USERS_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"pyar:users:{panel_id}:{page + 1}"))
    rows = [nav_row] if nav_row else []
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                      style=ButtonStyle.DANGER)])

    await loading.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                            parse_mode="HTML")


# ═══════════════════════ PATCH_PANEL_YAR_MOVE_V1 ═══════════════════════
# مدیریتِ ادمین‌های پنل (فقط خانوادهٔ مرزبان) + تغییرِ آدرسِ ساب + نمایشِ
# اینباندها. ثبت/ویرایشِ خودِ اینباند عمداً اضافه نشده — تویِ مرزبان این
# کار یعنی ویرایشِ مستقیمِ کانفیگِ خامِ Xray Core، که اگه فرمتش کمی
# اشتباه باشه، کلِ سرویسِ پروکسی رویِ اون پنل می‌خوابه. تا وقتی این با
# دقتِ کافی تست نشده، این‌جا اضافه نمی‌شه.

async def _admins_menu_content(panel: VpnPanel) -> tuple[str, InlineKeyboardMarkup]:
    client = make_panel_client(panel)
    try:
        admins = await client.list_admins()
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    rows = [[InlineKeyboardButton(text="➕ افزودنِ ادمینِ جدید",
                                  callback_data=f"pyar:admadd:{panel.id}", style=ButtonStyle.SUCCESS)]]
    lines = ["👤 <b>ادمین‌هایِ پنل</b>\n"]
    for a in admins:
        uname = a.get("username") or "—"
        sudo_txt = "👑 sudo" if a.get("is_sudo") else "عادی"
        lines.append(f"• <code>{uname}</code> ({sudo_txt})")
        rows.append([
            InlineKeyboardButton(text=f"🔑 پسوردِ {uname}", callback_data=f"pyar:admpw:{panel.id}:{uname}",
                                 style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=f"🗑 حذفِ {uname}", callback_data=f"pyar:admdel:{panel.id}:{uname}",
                                 style=ButtonStyle.DANGER),
        ])
    if not admins:
        lines.append("هیچ ادمینی برگردونده نشد.")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel.id}",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@panelyar_router.callback_query(F.data.startswith("pyar:admins:"))
async def pyar_admins_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("این قابلیت فقط برایِ پنلِ مرزبانه.", show_alert=True)
        return
    await cb.answer()
    loading = await cb.message.edit_text("🔄 در حالِ خواندنِ ادمین‌ها…")
    try:
        text, kb = await _admins_menu_content(panel)
    except PanelError as e:
        await loading.edit_text(
            f"❌ خواندنِ ادمین‌ها ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                     style=ButtonStyle.DANGER)]]), parse_mode="HTML")
        return
    except Exception:
        logger.exception("[panelyar] خواندنِ ادمین‌ها ناموفق بود (panel_id=%s)", panel_id)
        await loading.edit_text(
            "❌ خطایِ غیرمنتظره. دوباره امتحان کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                     style=ButtonStyle.DANGER)]]))
        return
    await loading.edit_text(text, reply_markup=kb, parse_mode="HTML")


@panelyar_router.callback_query(F.data.startswith("pyar:admadd:"))
async def pyar_admadd_start(cb: CallbackQuery, state: FSMContext):
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("یافت نشد", show_alert=True)
        return
    await state.set_state(PanelYarFSM.admin_add_username)
    await state.update_data(pyar_panel_id=panel_id, pyar_view_chat=cb.message.chat.id, pyar_view_msg=cb.message.message_id)
    await cb.answer()
    await cb.message.edit_text("👤 یوزرنیمِ ادمینِ جدید رو بفرست:", reply_markup=_cancel_kb())


@panelyar_router.message(PanelYarFSM.admin_add_username, F.text)
async def pyar_admadd_username(msg: Message, state: FSMContext):
    uname = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if not uname:
        return
    await state.update_data(pyar_admin_username=uname)
    await state.set_state(PanelYarFSM.admin_add_password)
    data = await state.get_data()
    await msg.bot.edit_message_text(
        "🔑 پسوردِ این ادمینِ جدید رو بفرست:", chat_id=data["pyar_view_chat"], message_id=data["pyar_view_msg"],
        reply_markup=_cancel_kb())


@panelyar_router.message(PanelYarFSM.admin_add_password, F.text)
async def pyar_admadd_password(msg: Message, state: FSMContext):
    pw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if not pw:
        return
    data = await state.get_data()
    panel_id = data["pyar_panel_id"]
    uname = data["pyar_admin_username"]
    await state.clear()
    panel = await _panel_by_id(panel_id)
    view_chat, view_msg = data["pyar_view_chat"], data["pyar_view_msg"]
    if not panel:
        await msg.bot.edit_message_text("⚠️ این پنل دیگه در دسترس نیست.", chat_id=view_chat, message_id=view_msg)
        return
    client = make_panel_client(panel)
    try:
        await client.create_admin(uname, pw, is_sudo=False)
    except PanelError as e:
        await msg.bot.edit_message_text(
            f"❌ افزودنِ ادمین ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
            chat_id=view_chat, message_id=view_msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:admins:{panel_id}", style=ButtonStyle.DANGER)]]))
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    text, kb = await _admins_menu_content(panel)
    await msg.bot.edit_message_text(f"✅ ادمینِ «{uname}» اضافه شد.\n\n{text}", chat_id=view_chat, message_id=view_msg,
                                    reply_markup=kb, parse_mode="HTML")


@panelyar_router.callback_query(F.data.startswith("pyar:admpw:"))
async def pyar_admpw_start(cb: CallbackQuery, state: FSMContext):
    _, _, panel_id_s, uname = cb.data.split(":")
    panel_id = int(panel_id_s)
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("یافت نشد", show_alert=True)
        return
    await state.set_state(PanelYarFSM.admin_pw_new)
    await state.update_data(pyar_panel_id=panel_id, pyar_admin_username=uname,
                            pyar_view_chat=cb.message.chat.id, pyar_view_msg=cb.message.message_id)
    await cb.answer()
    await cb.message.edit_text(f"🔑 پسوردِ جدیدِ «{uname}» رو بفرست:", reply_markup=_cancel_kb())


@panelyar_router.message(PanelYarFSM.admin_pw_new, F.text)
async def pyar_admpw_save(msg: Message, state: FSMContext):
    pw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if not pw:
        return
    data = await state.get_data()
    panel_id, uname = data["pyar_panel_id"], data["pyar_admin_username"]
    await state.clear()
    panel = await _panel_by_id(panel_id)
    view_chat, view_msg = data["pyar_view_chat"], data["pyar_view_msg"]
    if not panel:
        await msg.bot.edit_message_text("⚠️ این پنل دیگه در دسترس نیست.", chat_id=view_chat, message_id=view_msg)
        return
    client = make_panel_client(panel)
    try:
        await client.modify_admin_password(uname, pw)
    except PanelError as e:
        await msg.bot.edit_message_text(
            f"❌ تغییرِ پسورد ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
            chat_id=view_chat, message_id=view_msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:admins:{panel_id}", style=ButtonStyle.DANGER)]]))
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    text, kb = await _admins_menu_content(panel)
    await msg.bot.edit_message_text(f"✅ پسوردِ «{uname}» تغییر کرد.\n\n{text}", chat_id=view_chat, message_id=view_msg,
                                    reply_markup=kb, parse_mode="HTML")


@panelyar_router.callback_query(F.data.startswith("pyar:admdel:"))
async def pyar_admdel(cb: CallbackQuery):
    _, _, panel_id_s, uname = cb.data.split(":")
    panel_id = int(panel_id_s)
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("یافت نشد", show_alert=True)
        return
    await cb.answer("⏳ در حالِ حذف…")
    client = make_panel_client(panel)
    try:
        await client.delete_admin(uname)
    except PanelError as e:
        await cb.message.edit_text(
            f"❌ حذفِ ادمین ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:admins:{panel_id}", style=ButtonStyle.DANGER)]]))
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    text, kb = await _admins_menu_content(panel)
    await cb.message.edit_text(f"🗑 ادمینِ «{uname}» حذف شد.\n\n{text}", reply_markup=kb, parse_mode="HTML")


@panelyar_router.callback_query(F.data.startswith("pyar:subedit:"))
async def pyar_subedit_start(cb: CallbackQuery, state: FSMContext):
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("یافت نشد", show_alert=True)
        return
    await state.set_state(PanelYarFSM.sub_edit)
    await state.update_data(pyar_panel_id=panel_id, pyar_view_chat=cb.message.chat.id, pyar_view_msg=cb.message.message_id)
    await cb.answer()
    cur = panel.sub_base or "— (خالی، یعنی از خودِ پنل ساخته می‌شه)"
    await cb.message.edit_text(
        f"🔗 آدرسِ سابِ فعلی:\n<code>{cur}</code>\n\nآدرسِ جدید رو بفرست (برایِ برداشتنش «-» بفرست):",
        parse_mode="HTML", reply_markup=_cancel_kb())


@panelyar_router.message(PanelYarFSM.sub_edit, F.text)
async def pyar_subedit_save(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if not raw:
        return
    data = await state.get_data()
    panel_id = data["pyar_panel_id"]
    view_chat, view_msg = data["pyar_view_chat"], data["pyar_view_msg"]
    await state.clear()
    async with Session() as s:
        panel = await s.get(VpnPanel, panel_id)
        if panel is not None and panel.bot_id != _CUR_BOT_ID.get():
            panel = None
        if not panel:
            await msg.bot.edit_message_text("⚠️ این پنل دیگه در دسترس نیست.", chat_id=view_chat, message_id=view_msg)
            return
        panel.sub_base = None if raw == "-" else raw
        await s.commit()
        await s.refresh(panel)
    label = PANEL_LABELS.get(panel.panel_key, panel.panel_key)
    await msg.bot.edit_message_text(
        f"✅ آدرسِ سابِ «{label}» بروزرسانی شد.", chat_id=view_chat, message_id=view_msg,
        reply_markup=_panel_menu_kb(panel))


@panelyar_router.callback_query(F.data.startswith("pyar:inbrefresh:"))
async def pyar_inbounds_refresh(cb: CallbackQuery):
    panel_id = int(cb.data.split(":")[2])
    panel = await _panel_by_id(panel_id)
    if not panel or panel.panel_key != "marzban":
        await cb.answer("یافت نشد", show_alert=True)
        return
    await cb.answer("🔄 در حالِ خوندن از پنل…")
    client = make_panel_client(panel)
    try:
        inbounds = await client.list_inbounds()
    except PanelError as e:
        await cb.message.edit_text(
            f"❌ خواندنِ اینباندها ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}", style=ButtonStyle.DANGER)]]))
        return
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if not inbounds:
        lines = ["🔄 <b>اینباندهایِ پنل</b>\n\nهیچ اینباندی پیدا نشد."]
    else:
        lines = ["🔄 <b>اینباندهایِ پنل</b> (تازه‌خونده‌شده)\n"]
        for ib in inbounds:
            lines.append(f"• <code>{ib.get('tag')}</code> — {ib.get('protocol')}")
        lines.append(
            "\n💡 این‌جا فقط نمایشه؛ ساختن/ویرایشِ اینباند از همین‌جا هنوز "
            "اضافه نشده، چون تویِ مرزبان یعنی ویرایشِ مستقیمِ کانفیگِ خامِ "
            "Xray و اگه اشتباه بشه کلِ سرویس می‌خوابه — برایِ امنیت فعلاً "
            "این کار رو مستقیم تو خودِ پنل انجام بده.")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                   InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                                                        style=ButtonStyle.DANGER)]]))


# ═════════════════ PATCH_PANEL_YAR_BACKUP_MIGRATE_V1 ═════════════════
# مرحلهٔ ۲ (بکاپِ JSON) و مرحلهٔ ۳ (انتقال بینِ دو پنل). گزارشِ تیکت #807:
# «قسمت بکاپ و انتقال پنل تکمیل نشده، نوشته به‌زودی».
#
# اصولِ طراحی:
#  • فقط مالک/ادمینِ همین ربات و فقط پنل‌هایِ همین ربات (bot_id) — چک در هر هندلر.
#  • رمزِ پنل هرگز در فایلِ بکاپ نمی‌آید.
#  • انتقال «کپی» است، نه جابه‌جایی: کاربرانِ پنلِ مبدأ دست‌نخورده می‌مانند.
#  • قبل از هر انتقال، خودکار یک بکاپِ JSON از مبدأ فرستاده می‌شود.
#  • صفر در پنل‌ها یعنی «نامحدود»؛ پس کاربرِ منقضی‌شده/حجم‌تمام‌شده هرگز با
#    مقدارِ صفر ساخته نمی‌شود (نامحدود می‌شد) — رد می‌شود و در گزارش می‌آید.
#  • کاربری که در مقصد از قبل هست (هم‌نام) رد می‌شود؛ چیزی رونویسی نمی‌شود.

_MB = 1024 * 1024
# پنل‌هایِ مقصدی که پارامترِ hours را نمی‌شناسند ⇒ مدت را به روزِ رو‌به‌بالا گرد می‌کنیم
_NO_HOURS_PANELS = {"hiddify", "shahan"}
_MIG_RUNNING: set[tuple] = set()
_MIG_TASKS: set = set()
_MIG_SLEEP = 0.25          # فاصله بینِ ساختِ دو کاربر، برایِ فشارنیاوردن به پنل
_MIG_PROGRESS_EVERY = 10


async def _pyar_guard(cb: CallbackQuery, panel_id: int):
    """(row, panel) یا None. مالک/ادمینِ ربات + تعلقِ پنل به همین ربات."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, cb.bot)
    if not row:
        return None
    panel = await _panel_by_id(panel_id)
    if not panel or panel.bot_id != row.id:
        await cb.answer("این پنل دیگر وجود ندارد.", show_alert=True)
        return None
    return row, panel


async def _read_clients(panel: VpnPanel) -> list[dict]:
    client = make_panel_client(panel)
    try:
        return list(await client.list_clients() or [])
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (بستنِ کلاینتِ پنل)")


def _norm_client(c: dict) -> dict:
    """قراردادِ کلیدهایِ list_clients در پنل‌ها کمی فرق دارد (مرزبان: traffic{...}،
    سنایی: up/down رویِ خودِ آبجکت). همه را به یک شکل درمی‌آوریم."""
    tr = c.get("traffic") if isinstance(c.get("traffic"), dict) else {}

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    used = _i(tr.get("up") or c.get("up")) + _i(tr.get("down") or c.get("down"))
    total = _i(c.get("totalGB") or tr.get("total") or c.get("total"))
    uuid_raw = str(c.get("uuid") or c.get("id") or "")
    return {
        "email": str(c.get("email") or c.get("username") or "").strip(),
        "used": used,
        "total": total,
        "expiry_ms": _i(c.get("expiryTime")),
        "enable": bool(c.get("enable", True)),
        "uuid": uuid_raw if (len(uuid_raw) == 36 and uuid_raw.count("-") == 4) else None,
        "sub_id": str(c.get("subId") or c.get("sub_id") or "") or None,
    }


def _plan_one(n: dict, mode: str, now_ms: int, dst_base_key: str):
    """(کد، kwargs). کد: ok | noname | expired | depleted."""
    if not n["email"]:
        return "noname", None
    exp = n["expiry_ms"]
    if exp > 0 and exp <= now_ms + 60_000:
        return "expired", None
    # سنایی: expiryTime منفی یعنی «مدت از اولین اتصال شروع می‌شود» (مقدارش مدتِ کل است)
    if exp < 0:
        secs = abs(exp) // 1000
    elif exp > 0:
        secs = (exp - now_ms) // 1000
    else:
        secs = 0
    days = hours = 0
    if secs:
        if dst_base_key in _NO_HOURS_PANELS:
            days = max(1, math.ceil(secs / 86400))
        else:
            days = secs // 86400
            hours = math.ceil((secs % 86400) / 3600)
            if hours >= 24:
                days, hours = days + 1, 0
            if days == 0 and hours == 0:
                return "expired", None
    mb = None
    if n["total"] > 0:
        if mode == "r":
            rem = n["total"] - n["used"]
            if rem <= 0:
                return "depleted", None
            vol = rem
        else:
            vol = n["total"]
        mb = max(1, math.ceil(vol / _MB))
    return "ok", {"email": n["email"], "days": days, "hours": hours, "mb": mb,
                  "uuid": n["uuid"], "sub_id": n["sub_id"]}


def _build_backup(panel: VpnPanel, clients: list[dict]) -> bytes:
    label = PANEL_LABELS.get(panel_base_key(panel.panel_key), panel.panel_key)
    data = {
        "format": "botsaz-panelyar-backup",
        "version": 1,
        "created_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "panel": {"type": panel_base_key(panel.panel_key), "label": label,
                  "base_url": panel.base_url},   # عمداً بدونِ username/password
        "count": len(clients),
        "users": [{**_norm_client(c), "raw": c} for c in clients],
    }
    return json.dumps(data, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def _back_kb(panel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}",
                             style=ButtonStyle.DANGER)]])


# ─────────────────── 😴 کاربرانِ بدونِ اتصال (PATCH_PANEL_YAR_IDLE_V1) ───────────────────

_IDLE_DAYS = (2, 7, 30)
_IDLE_SHOW = 15


def _idle_kb(panel_id: int, days: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=(f"✅ {d} روز" if d == days else f"{d} روز"),
                                callback_data=f"pyar:idle:{panel_id}:{d}") for d in _IDLE_DAYS]
    return InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data=f"pyar:panel:{panel_id}", style=ButtonStyle.DANGER)]])


@panelyar_router.callback_query(F.data.regexp(r"^pyar:idle:\d+:\d+$"))
async def pyar_idle(cb: CallbackQuery):
    from tenant.shared.panel_clients import inactive_users as IU
    _, _, pid_s, days_s = cb.data.split(":")
    days = int(days_s) if int(days_s) in _IDLE_DAYS else 2
    g = await _pyar_guard(cb, int(pid_s))
    if not g:
        return
    _row, panel = g
    label = PANEL_LABELS.get(panel_base_key(panel.panel_key), panel.panel_key)
    await cb.answer("🔄 در حالِ بررسی…")
    try:
        await cb.message.edit_text(f"🔄 در حالِ بررسیِ اتصالِ کاربرانِ <b>{label}</b>… (روی پنل‌هایِ بزرگ چند دقیقه طول می‌کشد)",
                                   parse_mode="HTML")
    except Exception:
        logger.debug("[panelyar] edit loading failed")
    skipped, listed = 0, False
    try:
        client = make_panel_client(panel)
        try:
            clients = list(await client.list_clients() or [])
            listed = IU.has_last_online_field(clients)          # خودِ لیست زمانِ اتصال می‌دهد؟
            if clients and not listed:
                _n, skipped = await IU.probe_last_online(client, clients)
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (بستنِ کلاینتِ پنل)")
    except PanelError as e:
        await cb.message.edit_text(f"❌ خواندنِ کاربران ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
                                   reply_markup=_back_kb(panel.id), parse_mode="HTML")
        return
    except Exception:
        logger.exception("[panelyar] بررسیِ کاربرانِ بدونِ اتصال ناموفق (panel_id=%s)", panel.id)
        await cb.message.edit_text("❌ خطایِ غیرمنتظره در بررسی. دوباره امتحان کن.", reply_markup=_back_kb(panel.id))
        return
    if not clients:
        await cb.message.edit_text(f"👥 <b>{label}</b>\n\nهیچ کاربری روی این پنل پیدا نشد.",
                                   reply_markup=_back_kb(panel.id), parse_mode="HTML")
        return
    if not listed and not IU.supported(clients):
        await cb.message.edit_text(
            f"ℹ️ پنلِ <b>{label}</b> زمانِ «آخرین اتصال» کاربران را نمی‌دهد؛ برایِ همین نمی‌شود کاربرانِ بدونِ اتصال را "
            "از روی آن فهرست کرد.", reply_markup=_back_kb(panel.id), parse_mode="HTML")
        return
    now_ms = int(time.time() * 1000)
    res = IU.classify(clients, now_ms, days)
    rows = res["rows"]
    head = (f"😴 <b>{label}</b>\nکاربرانِ فعال که بیش از <b>{days} روز</b> وصل نشده‌اند: <b>{len(rows)}</b> "
            f"از {res['active']} کاربرِ فعال")
    if skipped:
        head += f"\n⚠️ برایِ سبک‌ماندنِ پنل فقط {IU.MAX_PROBE} کاربر بررسی شد ({skipped} کاربر ردشدند)."
    lines = [head, ""]
    for r in rows[:_IDLE_SHOW]:
        lines.append(f"• <code>{_esc_html(r['name'])}</code> · کد: <code>{_esc_html(r['code'])}</code> · "
                     f"{IU.fmt_last(r['last_ms'], now_ms)}")
    if len(rows) > _IDLE_SHOW:
        lines.append(f"… و {len(rows) - _IDLE_SHOW} کاربرِ دیگر (فهرستِ کامل در فایلِ زیر)")
    if not rows:
        lines.append("🎉 همهٔ کاربرانِ فعال در این بازه وصل شده‌اند.")
    await cb.message.edit_text("\n".join(lines)[:4000], reply_markup=_idle_kb(panel.id, days), parse_mode="HTML")
    if rows:
        stamp = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
        await cb.message.answer_document(
            BufferedInputFile(IU.render_txt(label, days, res, now_ms).encode("utf-8"),
                              filename=f"idle_{panel_base_key(panel.panel_key)}_{days}d_{stamp}.txt"),
            caption=f"📄 فهرستِ کاملِ {len(rows)} کاربرِ بدونِ اتصال (بیش از {days} روز)")


# ─────────────────────────── 💾 بکاپ ───────────────────────────

@panelyar_router.callback_query(F.data.startswith("pyar:backup:"))
async def pyar_backup(cb: CallbackQuery):
    g = await _pyar_guard(cb, int(cb.data.split(":")[2]))
    if not g:
        return
    _row, panel = g
    label = PANEL_LABELS.get(panel_base_key(panel.panel_key), panel.panel_key)
    await cb.answer("🔄 در حالِ گرفتنِ بکاپ…")
    try:
        clients = await _read_clients(panel)
    except PanelError as e:
        await cb.message.answer(f"❌ خواندنِ کاربران ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
                                parse_mode="HTML", reply_markup=_back_kb(panel.id))
        return
    except Exception:
        logger.exception("[panelyar] بکاپ ناموفق (panel_id=%s)", panel.id)
        await cb.message.answer("❌ خطایِ غیرمنتظره در گرفتنِ بکاپ. دوباره امتحان کن.",
                                reply_markup=_back_kb(panel.id))
        return
    if not clients:
        await cb.message.answer(f"ℹ️ روی پنلِ <b>{label}</b> کاربری پیدا نشد؛ بکاپی ساخته نشد.",
                                parse_mode="HTML", reply_markup=_back_kb(panel.id))
        return
    stamp = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
    fname = f"panelyar_{panel_base_key(panel.panel_key)}_{stamp}.json"
    payload = _build_backup(panel, clients)
    await cb.message.answer_document(
        BufferedInputFile(payload, filename=fname),
        caption=(f"💾 <b>بکاپِ {label}</b>\n👥 {len(clients)} کاربر\n\n"
                 "🔒 رمزِ پنل داخلِ فایل نیست. فایل را جایِ امنی نگه دار؛ "
                 "شاملِ نام‌کاربری‌ها و شناسه‌هایِ اشتراکِ کاربران است."),
        parse_mode="HTML", reply_markup=_back_kb(panel.id))


# ─────────────────────────── 🔁 انتقال ───────────────────────────

@panelyar_router.callback_query(F.data.startswith("pyar:migrate:"))
async def pyar_migrate_pick(cb: CallbackQuery):
    g = await _pyar_guard(cb, int(cb.data.split(":")[2]))
    if not g:
        return
    row, src = g
    panels = [p for p in await get_all_panels(row.id) if p.id != src.id]
    src_label = PANEL_LABELS.get(panel_base_key(src.panel_key), src.panel_key)
    if not panels:
        await cb.message.edit_text(
            f"🔁 <b>انتقال از {src_label}</b>\n\n"
            "برایِ انتقال باید <b>یک پنلِ دیگر</b> هم در همین ربات ثبت کرده باشی. "
            "اول از منویِ اصلی «➕ افزودنِ پنل» پنلِ مقصد را وصل کن.",
            parse_mode="HTML", reply_markup=_back_kb(src.id))
        await cb.answer()
        return
    rows = []
    for p in panels:
        lbl = PANEL_LABELS.get(panel_base_key(p.panel_key), p.panel_key)
        host = (p.base_url or "").replace("https://", "").replace("http://", "").rstrip("/")[:28]
        rows.append([InlineKeyboardButton(
            text=f"➡️ {lbl} — {host}", callback_data=f"pyar:migto:{src.id}:{p.id}",
            style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{src.id}",
                                      style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"🔁 <b>انتقال کاربران از {src_label}</b>\n\nپنلِ <b>مقصد</b> را انتخاب کن:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


async def _guard_pair(cb: CallbackQuery, src_id: int, dst_id: int):
    g = await _pyar_guard(cb, src_id)
    if not g:
        return None
    row, src = g
    dst = await _panel_by_id(dst_id)
    if not dst or dst.bot_id != row.id or dst.id == src.id:
        await cb.answer("پنلِ مقصد پیدا نشد.", show_alert=True)
        return None
    return row, src, dst


def _dst_names(dst_clients: list[dict]) -> set[str]:
    from tenant.shared.panel_clients.marzban_panel import _safe_username
    out = set()
    for c in dst_clients:
        e = _norm_client(c)["email"]
        if e:
            out.add(e.lower())
            try:
                out.add(_safe_username(e).lower())
            except Exception:
                logger.debug("_safe_username(%r) failed", e)
    return out


def _make_plan(src_clients: list[dict], dst_names: set[str], mode: str, dst_key: str):
    from tenant.shared.panel_clients.marzban_panel import _safe_username
    now_ms = int(time.time() * 1000)
    stats = {"total": len(src_clients), "ok": 0, "exists": 0, "expired": 0,
             "depleted": 0, "noname": 0}
    jobs = []
    for c in src_clients:
        n = _norm_client(c)
        code, kw = _plan_one(n, mode, now_ms, dst_key)
        if code != "ok":
            stats[code] += 1
            continue
        try:
            safe = _safe_username(n["email"]).lower()
        except Exception:
            safe = n["email"].lower()
        if n["email"].lower() in dst_names or safe in dst_names:
            stats["exists"] += 1
            continue
        stats["ok"] += 1
        jobs.append(kw)
    return jobs, stats


def _confirm_text(src_label: str, dst_label: str, mode: str, st: dict) -> str:
    vol_txt = ("حجمِ <b>باقی‌مانده</b> هر کاربر" if mode == "r"
               else "<b>کلِ حجمِ</b> هر کاربر (مصرفِ قبلی صفر می‌شود)")
    return (
        f"🔁 <b>انتقال: {src_label} ⬅️ {dst_label}</b>\n\n"
        f"👥 کاربرانِ مبدأ: <b>{st['total']}</b>\n"
        f"✅ قابلِ انتقال: <b>{st['ok']}</b>\n"
        f"⏭ از قبل در مقصد هست: {st['exists']}\n"
        f"⌛️ منقضی‌شده (رد می‌شود): {st['expired']}\n"
        f"📉 حجم‌تمام‌شده (رد می‌شود): {st['depleted']}\n\n"
        f"📦 حجمِ منتقل‌شونده: {vol_txt}\n"
        "⏳ زمان: به‌اندازهٔ باقی‌ماندهٔ هر کاربر\n\n"
        "ℹ️ این یک <b>کپی</b> است؛ کاربران روی پنلِ مبدأ می‌مانند. لینکِ اشتراکِ "
        "کاربران در مقصد <b>جدید</b> است (آدرسِ پنل عوض شده) و اینباند/گروهِ "
        "پیش‌فرضِ پنلِ مقصد به همه داده می‌شود. قبل از شروع، یک بکاپِ JSON از "
        "مبدأ برایت فرستاده می‌شود.")


async def _render_confirm(cb: CallbackQuery, src, dst, mode: str):
    src_label = PANEL_LABELS.get(panel_base_key(src.panel_key), src.panel_key)
    dst_label = PANEL_LABELS.get(panel_base_key(dst.panel_key), dst.panel_key)
    await cb.message.edit_text("🔄 در حالِ بررسیِ کاربرانِ دو پنل…")
    try:
        src_clients = await _read_clients(src)
        dst_clients = await _read_clients(dst)
    except PanelError as e:
        await cb.message.edit_text(
            f"❌ خواندنِ کاربران ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
            parse_mode="HTML", reply_markup=_back_kb(src.id))
        return
    except Exception:
        logger.exception("[panelyar] پیش‌نمایشِ انتقال ناموفق (src=%s dst=%s)", src.id, dst.id)
        await cb.message.edit_text("❌ خطایِ غیرمنتظره. دوباره امتحان کن.",
                                   reply_markup=_back_kb(src.id))
        return
    _jobs, st = _make_plan(src_clients, _dst_names(dst_clients), mode, panel_base_key(dst.panel_key))
    other = "t" if mode == "r" else "r"
    other_lbl = ("📦 به‌جایش: منتقل‌کردنِ کلِ حجم" if mode == "r"
                 else "📦 به‌جایش: منتقل‌کردنِ حجمِ باقی‌مانده")
    rows = []
    if st["ok"] > 0:
        rows.append([InlineKeyboardButton(
            text=f"🚀 شروعِ انتقالِ {st['ok']} کاربر",
            callback_data=f"pyar:miggo:{src.id}:{dst.id}:{mode}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(
        text=other_lbl, callback_data=f"pyar:migmode:{src.id}:{dst.id}:{other}",
        style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ تغییرِ مقصد", callback_data=f"pyar:migrate:{src.id}",
                                      style=ButtonStyle.DANGER)])
    text = _confirm_text(src_label, dst_label, mode, st)
    if st["ok"] == 0:
        text += "\n\n⚠️ هیچ کاربرِ قابلِ انتقالی نیست."
    await cb.message.edit_text(text, parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@panelyar_router.callback_query(F.data.startswith("pyar:migto:"))
async def pyar_mig_to(cb: CallbackQuery):
    p = cb.data.split(":")
    g = await _guard_pair(cb, int(p[2]), int(p[3]))
    if not g:
        return
    await cb.answer()
    await _render_confirm(cb, g[1], g[2], "r")


@panelyar_router.callback_query(F.data.startswith("pyar:migmode:"))
async def pyar_mig_mode(cb: CallbackQuery):
    p = cb.data.split(":")
    g = await _guard_pair(cb, int(p[2]), int(p[3]))
    if not g:
        return
    await cb.answer()
    await _render_confirm(cb, g[1], g[2], "t" if p[4] == "t" else "r")


@panelyar_router.callback_query(F.data.startswith("pyar:miggo:"))
async def pyar_mig_go(cb: CallbackQuery):
    p = cb.data.split(":")
    g = await _guard_pair(cb, int(p[2]), int(p[3]))
    if not g:
        return
    row, src, dst = g
    mode = "t" if p[4] == "t" else "r"
    key = (row.id, src.id, dst.id)
    if key in _MIG_RUNNING:
        await cb.answer("این انتقال همین الان در حالِ اجراست.", show_alert=True)
        return
    _MIG_RUNNING.add(key)
    await cb.answer("🚀 شروع شد")
    task = asyncio.create_task(_run_migration(cb.bot, cb.message, row, src, dst, mode, key))
    _MIG_TASKS.add(task)
    task.add_done_callback(_MIG_TASKS.discard)


def _progress_text(src_label, dst_label, done, total, okc, failc) -> str:
    return (f"🔁 <b>{src_label} ⬅️ {dst_label}</b>\n\n"
            f"⏳ در حالِ انتقال… {done}/{total}\n"
            f"✅ موفق: {okc}   ❌ ناموفق: {failc}\n\n"
            "تا پایان صبر کن؛ این پیام خودش به‌روز می‌شود.")


async def _run_migration(bot, msg: Message, row, src, dst, mode: str, key: tuple,
                         file_clients: list[dict] | None = None) -> None:
    """file_clients (PATCH_PANEL_YAR_RESTORE_FILE_V1): اگر داده شود، مبدأ یک فایلِ بکاپ است (نه پنلِ زنده)؛
    در این حالت src همان پنلِ مقصد است (فقط برایِ دکمهٔ بازگشت) و بکاپِ خودکارِ مبدأ ارسال نمی‌شود."""
    src_label = ("📄 فایلِ بکاپ" if file_clients is not None
                 else PANEL_LABELS.get(panel_base_key(src.panel_key), src.panel_key))
    dst_label = PANEL_LABELS.get(panel_base_key(dst.panel_key), dst.panel_key)
    chat_id = msg.chat.id
    dst_key = panel_base_key(dst.panel_key)
    try:
        # ۱) خواندن + بکاپِ خودکارِ مبدأ
        await msg.edit_text("🔄 در حالِ خواندنِ کاربران و گرفتنِ بکاپِ مبدأ…")
        src_clients = file_clients if file_clients is not None else await _read_clients(src)
        dst_clients = await _read_clients(dst)
        if src_clients and file_clients is None:
            stamp = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
            try:
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(_build_backup(src, src_clients),
                                      filename=f"panelyar_{panel_base_key(src.panel_key)}_{stamp}_before_migrate.json"),
                    caption="💾 بکاپِ خودکارِ مبدأ (قبل از انتقال)")
            except Exception:
                logger.exception("[panelyar] ارسالِ بکاپِ پیش از انتقال ناموفق بود")
        jobs, st = _make_plan(src_clients, _dst_names(dst_clients), mode, dst_key)
        if not jobs:
            await msg.edit_text("ℹ️ هیچ کاربرِ قابلِ انتقالی پیدا نشد.", reply_markup=_back_kb(src.id))
            return

        # ۲) ساختِ کاربران در مقصد، یکی‌یکی
        client = make_panel_client(dst)
        okc, failed = 0, []
        try:
            for i, j in enumerate(jobs, 1):
                try:
                    kw = {"limit_ip": 0, "mb": j["mb"], "hours": j["hours"]}
                    if j["uuid"]:
                        kw["uuid_str"] = j["uuid"]
                    if j["sub_id"]:
                        kw["sub_id"] = j["sub_id"]
                    await client.create_account(j["email"], 0, j["days"], **kw)
                    okc += 1
                except Exception as e:
                    failed.append({"email": j["email"], "error": str(e)[:200]})
                    logger.warning("[panelyar] انتقالِ %s ناموفق: %s", j["email"], e)
                if i % _MIG_PROGRESS_EVERY == 0 and i != len(jobs):
                    try:
                        await msg.edit_text(
                            _progress_text(src_label, dst_label, i, len(jobs), okc, len(failed)),
                            parse_mode="HTML")
                    except Exception:
                        pass    # «message is not modified» / فلود — مهم نیست
                await asyncio.sleep(_MIG_SLEEP)
        finally:
            try:
                await client.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (بستنِ کلاینتِ مقصد)")

        # ۳) گزارش
        lines = [
            f"✅ <b>انتقال تمام شد: {src_label} ⬅️ {dst_label}</b>\n",
            f"✅ منتقل‌شده: <b>{okc}</b>",
            f"❌ ناموفق: <b>{len(failed)}</b>",
            f"⏭ از قبل در مقصد بود: {st['exists']}",
            f"⌛️ منقضی (ردشده): {st['expired']}",
            f"📉 حجم‌تمام‌شده (ردشده): {st['depleted']}",
        ]
        if failed:
            lines.append("\nنمونهٔ خطاها:")
            for f_ in failed[:5]:
                lines.append(f"• <code>{f_['email'][:40]}</code> — {f_['error'][:80]}")
            lines.append("\nفهرستِ کاملِ ناموفق‌ها در فایلِ زیر است؛ رفعِ علت و اجرایِ دوباره "
                         "امن است (کاربرانِ منتقل‌شده رد می‌شوند).")
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_back_kb(src.id))
        if failed:
            await bot.send_document(
                chat_id,
                BufferedInputFile(json.dumps(failed, ensure_ascii=False, indent=1).encode("utf-8"),
                                  filename="migrate_failed.json"),
                caption="❌ کاربرانِ منتقل‌نشده")
    except PanelError as e:
        await msg.edit_text(f"❌ انتقال متوقف شد:\n<code>{_esc_html(str(e)[:300])}</code>",
                            parse_mode="HTML", reply_markup=_back_kb(src.id))
    except Exception:
        logger.exception("[panelyar] انتقال با خطا متوقف شد (src=%s dst=%s)", src.id, dst.id)
        try:
            await msg.edit_text("❌ انتقال با خطایِ غیرمنتظره متوقف شد. کاربرانِ منتقل‌شده تا این لحظه "
                                "در مقصد مانده‌اند؛ دوباره اجرا کنی، آن‌ها رد می‌شوند.",
                                reply_markup=_back_kb(src.id))
        except Exception:
            pass
    finally:
        _MIG_RUNNING.discard(key)


# ═════════════════ PATCH_PANEL_YAR_RESTORE_FILE_V1 ═════════════════
# گزارشِ مشتری (nitroteeam): «انتقال به پنل از همون فایل بکاپ باشه؛ چون پنل از دست بره این به درد می‌خوره».
# حالا کاربران را مستقیم از فایلِ بکاپِ JSON (همان فایلی که «💾 بکاپِ JSON» می‌دهد) روی یک پنلِ زنده می‌سازیم؛
# پنلِ مبدأ لازم نیست. قوانین مثلِ انتقال است: هم‌نام‌ها رد می‌شوند، منقضی/حجم‌تمام‌شده‌ها ساخته نمی‌شوند،
# مدتِ باقی‌مانده از «حالا» حساب می‌شود (انقضا در فایل مطلق است).

_FILE_CACHE: dict[tuple[int, int], list[dict]] = {}
_FILE_MAX_BYTES = 8 * 1024 * 1024


def _clients_from_backup(raw: bytes) -> list[dict]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("format") != "botsaz-panelyar-backup":
        raise ValueError("این فایل، بکاپِ پنل‌یارِ بات‌ساز نیست.")
    out = []
    for u in data.get("users") or []:
        if not isinstance(u, dict):
            continue
        r = u.get("raw")
        if isinstance(r, dict) and (r.get("email") or r.get("username")):
            out.append(r)
        elif u.get("email"):
            out.append({"email": u["email"], "up": int(u.get("used") or 0), "down": 0,
                        "totalGB": int(u.get("total") or 0), "expiryTime": int(u.get("expiry_ms") or 0),
                        "enable": u.get("enable", True), "uuid": u.get("uuid"), "subId": u.get("sub_id")})
    return out


@panelyar_router.callback_query(F.data.startswith("pyar:fromfile:"))
async def pyar_fromfile_start(cb: CallbackQuery, state: FSMContext):
    g = await _pyar_guard(cb, int(cb.data.split(":")[2]))
    if not g:
        return
    _row, dst = g
    await state.set_state(PanelYarFSM.restore_file)
    await state.update_data(pyar_restore_dst=dst.id)
    label = PANEL_LABELS.get(panel_base_key(dst.panel_key), dst.panel_key)
    await cb.message.edit_text(
        f"📥 <b>ساختِ کاربران از فایلِ بکاپ روی {label}</b>\n\n"
        "فایلِ بکاپِ JSON را (همان که «💾 بکاپِ JSON از کاربران» داده بود) همین‌جا بفرست.\n"
        "پنلِ قبلی لازم نیست زنده باشد. کاربرانِ هم‌نام در این پنل رد می‌شوند و منقضی‌ها ساخته نمی‌شوند.",
        parse_mode="HTML", reply_markup=_back_kb(dst.id))
    await cb.answer()


@panelyar_router.message(PanelYarFSM.restore_file, F.document)
async def pyar_fromfile_got(msg: Message, state: FSMContext):
    data = await state.get_data()
    dst = await _panel_by_id(int(data.get("pyar_restore_dst") or 0))
    row = await _row_for_msg(msg)
    if not row or not dst or dst.bot_id != row.id:
        await state.clear()
        await msg.answer("این مرحله منقضی شده.")
        return
    doc = msg.document
    if (doc.file_size or 0) > _FILE_MAX_BYTES:
        await msg.answer("❌ فایل بزرگ است (حداکثر ۸ مگابایت).")
        return
    buf = await msg.bot.download(doc)
    try:
        clients = _clients_from_backup(buf.read())
    except Exception as e:  # noqa: BLE001
        await msg.answer(f"❌ فایل خوانده نشد: {str(e)[:150]}\nفایلِ درست را بفرست.", reply_markup=_back_kb(dst.id))
        return
    if not clients:
        await msg.answer("ℹ️ در فایل کاربری پیدا نشد.", reply_markup=_back_kb(dst.id))
        return
    await state.clear()
    _FILE_CACHE[(row.id, dst.id)] = clients
    sent = await msg.answer("🔄 در حالِ بررسیِ پنلِ مقصد…")
    await _render_file_confirm(sent, row, dst, "r")


async def _row_for_msg(msg: Message):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(msg.bot)
    if row and await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return row
    return None


async def _render_file_confirm(target: Message, row, dst, mode: str) -> None:
    clients = _FILE_CACHE.get((row.id, dst.id))
    if not clients:
        await target.edit_text("این مرحله منقضی شده؛ دوباره فایل را بفرست.", reply_markup=_back_kb(dst.id))
        return
    try:
        dst_clients = await _read_clients(dst)
    except Exception as e:  # noqa: BLE001
        await target.edit_text(f"❌ خواندنِ کاربرانِ پنلِ مقصد ناموفق بود:\n<code>{_esc_html(str(e)[:300])}</code>",
                               parse_mode="HTML", reply_markup=_back_kb(dst.id))
        return
    _jobs, st = _make_plan(clients, _dst_names(dst_clients), mode, panel_base_key(dst.panel_key))
    dst_label = PANEL_LABELS.get(panel_base_key(dst.panel_key), dst.panel_key)
    other = "t" if mode == "r" else "r"
    rows = []
    if st["ok"] > 0:
        rows.append([InlineKeyboardButton(text=f"🚀 شروعِ ساختِ {st['ok']} کاربر",
                                          callback_data=f"pyar:rfgo:{dst.id}:{mode}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(
        text=("📦 به‌جایش: کلِ حجم" if mode == "r" else "📦 به‌جایش: حجمِ باقی‌مانده"),
        callback_data=f"pyar:rfmode:{dst.id}:{other}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"pyar:panel:{dst.id}",
                                      style=ButtonStyle.DANGER)])
    text = _confirm_text("📄 فایلِ بکاپ", dst_label, mode, st)
    if st["ok"] == 0:
        text += "\n\n⚠️ هیچ کاربرِ قابلِ ساختی نیست."
    await target.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@panelyar_router.callback_query(F.data.startswith("pyar:rfmode:"))
async def pyar_rf_mode(cb: CallbackQuery):
    p = cb.data.split(":")
    g = await _pyar_guard(cb, int(p[2]))
    if not g:
        return
    await cb.answer()
    await _render_file_confirm(cb.message, g[0], g[1], "t" if p[3] == "t" else "r")


@panelyar_router.callback_query(F.data.startswith("pyar:rfgo:"))
async def pyar_rf_go(cb: CallbackQuery):
    p = cb.data.split(":")
    g = await _pyar_guard(cb, int(p[2]))
    if not g:
        return
    row, dst = g
    clients = _FILE_CACHE.get((row.id, dst.id))
    if not clients:
        await cb.answer("منقضی شده؛ دوباره فایل را بفرست.", show_alert=True)
        return
    mode = "t" if p[3] == "t" else "r"
    key = (row.id, -1, dst.id)
    if key in _MIG_RUNNING:
        await cb.answer("این کار همین الان در حالِ اجراست.", show_alert=True)
        return
    _MIG_RUNNING.add(key)
    await cb.answer("🚀 شروع شد")
    task = asyncio.create_task(_run_migration(cb.bot, cb.message, row, dst, dst, mode, key,
                                              file_clients=clients))
    _MIG_TASKS.add(task)
    task.add_done_callback(_MIG_TASKS.discard)
