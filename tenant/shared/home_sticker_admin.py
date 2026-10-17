# -*- coding: utf-8 -*-
"""
home_sticker_admin.py — بخشِ «🎟 افزودن استیکر» در «دکمه‌ها و تنظیماتِ» هر
ربات مستأجر (PATCH_TENANT_HOME_STICKER_V1).

دقیقاً همان قابلیتی که در ربات اصلیِ بات‌ساز هست (نگاه کن به
platform_sticker_admin.py): یک استیکر روی استارت، و یک استیکر جداگانه
برای هر دکمهٔ صفحهٔ اصلی — با این تفاوت که اینجا مخصوصِ هر رباتِ مستأجر
است و فقط پلنِ حرفه‌ای می‌بیندش (نگاه کن Feature.HOME_STICKERS در
plans.py)؛ رایگان و رشد این گزینه را در «دکمه‌ها و تنظیمات» ندارند.

ذخیره‌سازی روی همان جدولِ Setting هر ربات انجام می‌شود:
  • استیکرِ استارت   → کلید "start_sticker"
  • استیکرِ هر دکمه   → کلید f"hbtn:{main_button_id}:sticker"

دو تابعِ کمکیِ get_start_sticker / get_button_sticker برای استفادهٔ
بقیهٔ کد (ارسالِ واقعیِ استیکر پیش از پیامِ استارت/محتویاتِ دکمه) در
انتهای فایل تعریف شده‌اند.

callbackها با پیشوند: thst:
  thst:menu            — فهرستِ استیکرِ استارت + همهٔ دکمه‌های سطحِ اول
  thst:start           — مدیریتِ استیکرِ استارت
  thst:add:start        — شروعِ افزودن/جایگزینیِ استیکرِ استارت
  thst:btn:{mb_id}      — مدیریتِ استیکرِ یک دکمهٔ مشخص
  thst:add:{mb_id}       — شروعِ افزودن/جایگزینیِ استیکرِ همان دکمه
  thst:del:start         — حذفِ استیکرِ استارت
  thst:del:{mb_id}       — حذفِ استیکرِ همان دکمه
  thst:close             — بستنِ صفحه
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import MainButton

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

home_sticker_admin_router = Router()

START_STICKER_KEY = "start_sticker"


class TenantStickerFSM(StatesGroup):
    wait_sticker = State()


# ─────────────── کمک‌توابعِ عمومی (مالک/پلن/تنظیمات) ───────────────

async def _owner(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


async def _owner_msg(msg: Message, bot: Bot):
    from tenant import _owner_ok_msg
    return await _owner_ok_msg(msg, bot)


async def _feature_ok(cb: CallbackQuery, row) -> bool:
    from plans import Feature, has_feature, upgrade_hint
    if has_feature(row.plan, Feature.HOME_STICKERS):
        return True
    await cb.answer(upgrade_hint(Feature.HOME_STICKERS), show_alert=True)
    return False


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def _top_buttons(bot_id: int) -> list[MainButton]:
    async with Session() as s:
        rows = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.parent_id.is_(None))
            .order_by(MainButton.row, MainButton.col))).all()
    return list(rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="thst:menu", style=ButtonStyle.DANGER),
         InlineKeyboardButton(text="❌ بستن", callback_data="thst:close", style=ButtonStyle.DANGER)]
    ])


async def _render_menu(row, target: CallbackQuery | Message, edit: bool = True) -> None:
    bot_id = row.id
    start_has = bool((await _get(bot_id, START_STICKER_KEY, "")).strip())
    start_status = "✅ دارد" if start_has else "— ندارد"
    rows = [[InlineKeyboardButton(text=f"🚀 استیکر روی استارت  {start_status}", callback_data="thst:start")]]

    btns = await _top_buttons(bot_id)
    for mb in btns:
        has = bool((await _get(bot_id, f"hbtn:{mb.id}:sticker", "")).strip())
        shown = mb.label[:45]
        rows.append([InlineKeyboardButton(
            text=f"🎟 {shown}  {'✅' if has else '—'}",
            callback_data=f"thst:btn:{mb.id}")])

    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات", callback_data="tadm:sec:settings",
                                       style=ButtonStyle.DANGER),
                 InlineKeyboardButton(text="❌ بستن", callback_data="thst:close", style=ButtonStyle.DANGER)])
    text = (
        "🎟 <b>مدیریت استیکرهای ربات</b>\n\n"
        "برای استارت، یک استیکر پیش از پیام خوش‌آمد ارسال می‌شود. برای هر دکمهٔ "
        "صفحهٔ اصلی هم استیکر پیش از محتوای همان دکمه ارسال خواهد شد.\n\n"
        "🚀 استیکر استارت: " + start_status + "\n"
        + ("از فهرست زیر بخش موردنظر را انتخاب کن:" if btns else
           "هنوز دکمه‌ای در صفحهٔ اصلی نساخته‌ای؛ فعلاً فقط استیکرِ استارت را می‌توانی تنظیم کنی.")
    )
    # === PATCH_PEMJ_FAKECB_COMPAT_V1-style ===
    # وقتی این تابع از حالتِ منویِ کیبوردی صدا زده می‌شود، به‌جایِ یک
    # CallbackQuery واقعی یک _FakeCb (شبیه‌سازی‌شده، نه زیرکلاسِ واقعی)
    # پاس داده می‌شود؛ isinstance همیشه False برمی‌گرداند، پس با
    # duck-typing (وجودِ ویژگیِ data) هم CallbackQuery واقعی و هم
    # _FakeCb درست تشخیص داده می‌شوند.
    _is_cb_like = hasattr(target, "data")
    msg = target.message if _is_cb_like else target
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        if edit and _is_cb_like:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")

async def _ask(cb: CallbackQuery, state: FSMContext, bot_id: int, target_key: str, title: str) -> None:
    await state.update_data(tenant_sticker_bot_id=bot_id, tenant_sticker_target=target_key)
    await state.set_state(TenantStickerFSM.wait_sticker)
    await cb.message.edit_text(
        f"🎟 <b>{title}</b>\n\nیک استیکر معمولی یا پرمیوم بفرست تا ذخیره شود.",
        reply_markup=_back_kb(), parse_mode="HTML")


@home_sticker_admin_router.callback_query(F.data == "thst:menu")
async def thst_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    await state.clear()
    await _render_menu(row, cb)
    await cb.answer()


@home_sticker_admin_router.callback_query(F.data == "thst:start")
async def thst_start(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    has = bool((await _get(row.id, START_STICKER_KEY, "")).strip())
    rows = [[InlineKeyboardButton(text="➕ افزودن استیکر روی استارت", callback_data="thst:add:start",
                                   style=ButtonStyle.SUCCESS)]]
    if has:
        rows.append([InlineKeyboardButton(text="🗑 حذف استیکر استارت", callback_data="thst:del:start",
                                           style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="thst:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🚀 <b>استیکر روی استارت</b>\n\nاین استیکر قبل از پیام خوش‌آمدگویی ارسال می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@home_sticker_admin_router.callback_query(F.data == "thst:add:start")
async def thst_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    await _ask(cb, state, row.id, "__start__", "افزودن استیکر روی استارت")
    await cb.answer()


@home_sticker_admin_router.callback_query(F.data.startswith("thst:btn:"))
async def thst_btn(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    mb_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        mb = await s.get(MainButton, int(mb_id)) if mb_id.isdigit() else None
    if not mb or mb.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    has = bool((await _get(row.id, f"hbtn:{mb_id}:sticker", "")).strip())
    rows = [[InlineKeyboardButton(text="➕ افزودن استیکر", callback_data=f"thst:add:{mb_id}",
                                   style=ButtonStyle.SUCCESS)]]
    if has:
        rows.append([InlineKeyboardButton(text="🗑 حذف استیکر", callback_data=f"thst:del:{mb_id}",
                                           style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="thst:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"🎟 <b>استیکر دکمه</b>\n\n{mb.label}\n\nاستیکر پیش از محتوای این دکمه ارسال می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@home_sticker_admin_router.callback_query(F.data.startswith("thst:add:"))
async def thst_add_btn(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    mb_id = cb.data.split(":", 2)[2]
    if mb_id == "start":
        await _ask(cb, state, row.id, "__start__", "افزودن استیکر روی استارت")
        await cb.answer()
        return
    async with Session() as s:
        mb = await s.get(MainButton, int(mb_id)) if mb_id.isdigit() else None
    if not mb or mb.bot_id != row.id:
        await cb.answer("یافت نشد", show_alert=True)
        return
    await _ask(cb, state, row.id, mb_id, f"افزودن استیکر برای {mb.label}")
    await cb.answer()


@home_sticker_admin_router.message(TenantStickerFSM.wait_sticker, F.sticker)
async def thst_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    bot_id = int(data.get("tenant_sticker_bot_id") or row.id)
    target = str(data.get("tenant_sticker_target") or "")
    file_id = msg.sticker.file_id
    if target == "__start__":
        await _set(bot_id, START_STICKER_KEY, file_id)
    else:
        await _set(bot_id, f"hbtn:{target}:sticker", file_id)
    await state.clear()
    await msg.answer("✅ استیکر با موفقیت ذخیره شد.")
    await _render_menu(row, msg, edit=False)


# === PATCH_STICKER_WAIT_ESCAPE_V1 ===
# دو مشکلی که ادمین‌ها گزارش کردند:
#
# ۱) این هندلر همه‌چیز را می‌بلعد. تا وقتی ربات منتظرِ استیکر است، حتی
#    زدنِ دکمه‌هایِ کیبوردِ اصلی (منویِ خانه، بازگشت، ...) هم فقط جوابِ
#    «❌ فقط یک استیکر بفرست» می‌گرفت — یعنی ادمین گیر می‌کرد و باید
#    اسکرول می‌کرد بالا تا دکمهٔ شیشه‌ایِ «بازگشت» را پیدا کند. حالا هر
#    متنی که شبیهِ «خروج» باشد حالت را پاک می‌کند و برمی‌گردد.
#
# ۲) پیامِ خطا نمی‌گفت *چه چیزی* فرستاده شده. تلگرام بعضی استیکرها را
#    شبیهِ عکس نشان می‌دهد، برایِ همین ادمین یک عکس می‌فرستاد و فکر
#    می‌کرد استیکر فرستاده و باگ است. حالا دقیقاً می‌گوید چه دیده.
_ESCAPE_WORDS = {
    "بازگشت", "⬅️ بازگشت", "لغو", "❌ لغو", "انصراف", "بستن", "❌ بستن",
    "منوی اصلی", "🏠 منوی اصلی", "خانه", "/cancel", "/start",
}


def _looks_like_escape(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t in _ESCAPE_WORDS:
        return True
    # دکمه‌هایِ کیبورد معمولاً ایموجی + متن‌اند؛ متنِ خالص را هم چک کن
    bare = "".join(ch for ch in t if ch.isalpha() or ch.isspace()).strip()
    return bare in _ESCAPE_WORDS


def _received_kind_fa(msg: Message) -> str:
    """چیزی که کاربر واقعاً فرستاده — برایِ پیامِ خطایِ دقیق."""
    if msg.photo:
        return ("یک <b>عکس</b> فرستادی، نه استیکر. عکس با استیکر فرق دارد؛ "
                "استیکر را باید از پنلِ استیکرهایِ تلگرام انتخاب کنی.")
    if msg.animation:
        return "یک <b>گیف</b> فرستادی، نه استیکر."
    if msg.document:
        return ("یک <b>فایل</b> فرستادی. اگر تصویرِ استیکر را به‌صورتِ فایل "
                "می‌فرستی کار نمی‌کند؛ خودِ استیکر را بفرست.")
    if msg.video or msg.video_note:
        return "یک <b>ویدیو</b> فرستادی، نه استیکر."
    if msg.text:
        return "یک <b>متن</b> فرستادی، نه استیکر."
    return "چیزی که فرستادی استیکر نبود."


@home_sticker_admin_router.message(TenantStickerFSM.wait_sticker)
async def thst_invalid(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner_msg(msg, bot):
        return
    # راهِ خروج: ادمین نباید در این حالت گیر کند
    if _looks_like_escape(msg.text or ""):
        await state.clear()
        await msg.answer("لغو شد؛ استیکری ذخیره نشد.")
        return
    await msg.answer(
        f"❌ {_received_kind_fa(msg)}\n\n"
        "یک استیکر بفرست، یا از دکمهٔ «بازگشت» بالا استفاده کن "
        "(یا کلمهٔ <code>لغو</code> را بفرست).",
        parse_mode="HTML")


@home_sticker_admin_router.callback_query(F.data.startswith("thst:del:"))
async def thst_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    if not await _feature_ok(cb, row):
        return
    await state.clear()
    key = cb.data.split(":", 2)[2]
    if key == "start":
        await _set(row.id, START_STICKER_KEY, "")
    else:
        await _set(row.id, f"hbtn:{key}:sticker", "")
    await cb.answer("✅ حذف شد")
    await _render_menu(row, cb)


@home_sticker_admin_router.callback_query(F.data == "thst:close")
async def thst_close(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_text("✅ بسته شد.")
        except Exception:
            logger.exception("home_sticker_admin: بستنِ پیام (حذف/ویرایش) هر دو ناموفق بودند")
    await cb.answer()


# ─────────────── توابعِ کمکی برای ارسالِ واقعیِ استیکر ───────────────
# اینها را بقیهٔ ماژول‌ها (مثلاً tenant/__init__.py، در /start و در اجرایِ
# محتوایِ دکمه) صدا می‌زنند تا استیکرِ ذخیره‌شده را واقعاً بفرستند. عمداً
# خودشان هیچ چکِ پلنی انجام نمی‌دهند — چون اگر پلن از حرفه‌ای پایین بیاید،
# می‌خواهیم مقدارِ قبلاً ذخیره‌شده نگه داشته شود اما دیگر ارسال نشود؛
# فراخوان (caller) باید has_feature(plan, Feature.HOME_STICKERS) را چک کند.

async def get_start_sticker(bot_id: int) -> str:
    return (await _get(bot_id, START_STICKER_KEY, "")).strip()


async def get_button_sticker(bot_id: int, main_button_id: int) -> str:
    return (await _get(bot_id, f"hbtn:{main_button_id}:sticker", "")).strip()
