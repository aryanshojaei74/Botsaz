"""
tenant/bots/funstat/handlers.py — سمتِ کاربرِ رباتِ Fun Stat (PATCH_FUNSTAT_BOT_V1).

منطقِ تماس با سرویس (توکن، فهرستِ مسدود، قالبِ پیام‌ها) از platform_funstat می‌آید؛ اینجا فقط
دروازه‌ها و سقف‌هایِ هر ربات است:
  • ربات باید field == "funstat" باشد؛
  • سرویس روشن باشد، و دسترسیِ *مالکِ ربات* هنوز معتبر باشد (در مرحلهٔ تست، با پایانِ مدتِ دسترسیِ مالک،
    جست‌وجو برایِ کاربرانِ ربات متوقف می‌شود)؛
  • سقفِ روزانهٔ هر کاربر (تنظیمِ «fsb_user_limit»، پیش‌فرض ۵؛ مالک/ادمین بی‌سقف) و سقفِ روزانهٔ کلِ ربات
    («fsb_bot_limit»، پیش‌فرض ۲۰۰).
هزینهٔ هر جست‌وجو در FunstatLog با پیشوندِ «b<bot_id>:» ثبت می‌شود.
"""
from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, FunstatLog, MainButton

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

funstat_bot_router = Router(name="funstat_bot")

FIELD_KEY = "funstat"
DEFAULT_USER_LIMIT = 5
DEFAULT_BOT_LIMIT = 200


class FunstatBotFSM(StatesGroup):
    query = State()


def _btn(text: str, cb: str, style=None) -> InlineKeyboardButton:
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


_HOME_ROW = [_btn("🏠 منوی اصلی", "mbtn:home", ButtonStyle.DANGER)]

INTRO = (
    "🔎 <b>Fun Stat</b>\n\n"
    "آیدیِ عددیِ یک حساب را از روی @یوزرنیم پیدا کن (یا برعکس) و تاریخچهٔ نام و یوزرنیم و "
    "گروه‌هایِ عمومیِ آن را ببین.\n\n"
    "فقط یک <b>@یوزرنیم</b> یا <b>آیدیِ عددی</b> بفرست."
)


async def _row(bot: Bot) -> BotRow | None:
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _is_privileged(row: BotRow, uid: int) -> bool:
    from tenant import is_owner_or_admin
    return bool(await is_owner_or_admin(row.id, uid, row.owner_id))


async def _count_today(bot_id: int, uid: int | None = None) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count(FunstatLog.id)).where(
        FunstatLog.action.like(f"b{bot_id}:%"), FunstatLog.created_at >= start,
        FunstatLog.ok == True)  # noqa: E712
    if uid is not None:
        q = q.where(FunstatLog.telegram_id == uid)
    async with Session() as s:
        return int(await s.scalar(q) or 0)


async def _limits(bot_id: int) -> tuple[int, int]:
    from tenant import get_setting

    def _i(v, d):
        try:
            return int(float(str(v).strip()))
        except (TypeError, ValueError):
            return d
    return (_i(await get_setting(bot_id, "fsb_user_limit", str(DEFAULT_USER_LIMIT)), DEFAULT_USER_LIMIT),
            _i(await get_setting(bot_id, "fsb_bot_limit", str(DEFAULT_BOT_LIMIT)), DEFAULT_BOT_LIMIT))


async def _gate(row: BotRow, uid: int) -> str | None:
    """None = مجاز؛ وگرنه متنِ رد."""
    from platform_funstat import _access, _cfg
    if row.field != FIELD_KEY:
        return "این بخش در این ربات فعال نیست."
    c = await _cfg()
    if not c["enabled"]:
        return "سرویس موقتاً غیرفعال است."
    ok, _why = await _access(row.owner_id)
    if not ok:
        return "سرویسِ این ربات موقتاً غیرفعال است. به مدیرِ ربات خبر بده."
    if await _is_privileged(row, uid):
        return None
    user_limit, bot_limit = await _limits(row.id)
    if user_limit > 0 and await _count_today(row.id, uid) >= user_limit:
        return f"سقفِ جست‌وجویِ امروزت ({user_limit}) پر شده؛ فردا دوباره."
    if bot_limit > 0 and await _count_today(row.id) >= bot_limit:
        return "ظرفیتِ امروزِ این ربات تکمیل شده؛ فردا دوباره."
    return None


async def _error_text(e, row: BotRow, uid: int) -> str:
    """کاربرِ عادی پیامِ کلی می‌بیند؛ مالکِ ربات/ادمین علتِ واقعی را (برایِ عیب‌یابی)."""
    if e.code in ("timeout", "rate"):
        return "⚠️ " + e.message
    if await _is_privileged(row, uid):
        return f"⚠️ [فقط مالک می‌بیند] {e.message} (کد: {e.code})"
    return "⚠️ سرویس موقتاً در دسترس نیست؛ کمی بعد دوباره امتحان کن."


async def _alert_admin(e, bot_id: int) -> None:
    """خطایِ مربوط به توکن/اتصالِ سرویس را (حداکثر ساعتی یک‌بار) با رباتِ اصلی به ادمینِ پلتفرم خبر بده."""
    if e.code in ("timeout", "rate"):
        return
    try:
        import time
        from aiogram import Bot as _B
        from config import PLATFORM_ADMIN_ID, PLATFORM_BOT_TOKEN
        from platform_funstat import _get, _int, _set
        if not (PLATFORM_ADMIN_ID and PLATFORM_BOT_TOKEN):
            return
        if time.time() - _int(await _get("funstat_err_ts", "0")) < 3600:
            return
        await _set("funstat_err_ts", str(int(time.time())))
        pb = _B(token=PLATFORM_BOT_TOKEN)
        try:
            await pb.send_message(
                PLATFORM_ADMIN_ID,
                f"⚠️ <b>Fun Stat</b> (ربات #{bot_id})\n{e.message}\nکد: <code>{e.code}</code>\n"
                "از «پنل مدیریت ← 🔎 Fun Stat ← 💰 بررسیِ موجودی/اتصال» چک کن.")
        finally:
            await pb.session.close()
    except Exception:
        logger.debug("اطلاعِ خطایِ Fun Stat به ادمین ممکن نشد (بی‌اهمیت)")


async def show_search(msg: Message, bot: Bot, state: FSMContext) -> None:
    """ورودیِ دکمهٔ «🔎 جست‌وجویِ Fun Stat» (action=funstat_search)."""
    row = await _row(bot)
    if not row:
        return
    deny = await _gate(row, msg.from_user.id)
    if deny:
        await msg.answer("⛔️ " + deny, reply_markup=_kb(_HOME_ROW))
        return
    await state.set_state(FunstatBotFSM.query)
    await msg.answer(INTRO, reply_markup=_kb(_HOME_ROW))


async def _maybe_menu_button(msg: Message, bot: Bot, state: FSMContext, row: BotRow) -> bool:
    """اگر متن برچسبِ یکی از دکمه‌هایِ منویِ اصلی بود، حالت را ببند و همان دکمه را اجرا کن."""
    async with Session() as s:
        exists = await s.scalar(select(MainButton.id).where(
            MainButton.bot_id == row.id, MainButton.label == msg.text).limit(1))
    if not exists:
        return False
    await state.clear()
    from tenant import tenant_buttons
    await tenant_buttons(msg, bot, state)
    return True


@funstat_bot_router.message(StateFilter(FunstatBotFSM.query), F.text)
async def fsb_query(msg: Message, state: FSMContext, bot: Bot):
    raw = (msg.text or "").strip()
    if raw.startswith("/"):
        await state.clear()
        raise SkipHandler
    row = await _row(bot)
    if not row or row.field != FIELD_KEY:
        await state.clear()
        raise SkipHandler
    if await _maybe_menu_button(msg, bot, state, row):
        return
    uid = msg.from_user.id
    deny = await _gate(row, uid)
    if deny:
        await state.clear()
        await msg.answer("⛔️ " + deny, reply_markup=_kb(_HOME_ROW))
        return
    from platform_funstat import FunstatError, _fmt_user, _log, lookup
    wait = await msg.answer("⏳ در حالِ جست‌وجو…")
    try:
        kind, payload, cost, action = await lookup(raw)
    except FunstatError as e:
        await _log(uid, f"b{row.id}:lookup", raw, 0, False)
        logger.warning("funstat bot=%s: %s (%s)", row.id, e.message, e.code)
        await _alert_admin(e, row.id)
        txt = await _error_text(e, row, uid)
        try:
            await wait.edit_text(txt)
        except Exception:
            await msg.answer(txt)
        return
    try:
        await wait.delete()
    except Exception:
        logger.debug("حذفِ پیامِ انتظار ممکن نشد (بی‌اهمیت)")
    if kind != "ok":
        await msg.answer(str(payload), reply_markup=_kb(_HOME_ROW))
        return
    u = payload
    await _log(uid, f"b{row.id}:{action}", raw, cost, True)
    tid = int(u["id"])
    await state.clear()
    await msg.answer(_fmt_user(u), parse_mode="HTML", reply_markup=_kb(
        [_btn("📜 تاریخچهٔ نام", f"fsb:q:names:{tid}"), _btn("🏷 تاریخچهٔ یوزرنیم", f"fsb:q:unames:{tid}")],
        [_btn("📊 آمارِ کلی", f"fsb:q:stats:{tid}"), _btn("👥 گروه‌هایِ عمومی", f"fsb:q:groups:{tid}")],
        [_btn("🔎 جست‌وجویِ تازه", "fsb:again", ButtonStyle.PRIMARY)],
        _HOME_ROW))


@funstat_bot_router.callback_query(F.data == "fsb:again")
async def fsb_again(cb: CallbackQuery, state: FSMContext, bot: Bot):
    row = await _row(bot)
    if not row:
        await cb.answer()
        return
    deny = await _gate(row, cb.from_user.id)
    if deny:
        await cb.answer(deny, show_alert=True)
        return
    await state.set_state(FunstatBotFSM.query)
    await cb.message.answer("@یوزرنیم یا آیدیِ عددیِ بعدی را بفرست:")
    await cb.answer()


@funstat_bot_router.callback_query(F.data.startswith("fsb:q:"))
async def fsb_detail(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[3].lstrip("-").isdigit():
        await cb.answer("نامعتبر", show_alert=True)
        return
    kind, tid = parts[2], int(parts[3])
    row = await _row(bot)
    if not row:
        await cb.answer()
        return
    from platform_funstat import FunstatError, _blocked_ids, _cost_of, _log, detail_text
    if tid in await _blocked_ids():
        await cb.answer("ممکن نیست.", show_alert=True)
        return
    deny = await _gate(row, cb.from_user.id)
    if deny:
        await cb.answer(deny, show_alert=True)
        return
    await cb.answer("⏳")
    try:
        text, d = await detail_text(kind, tid)
    except ValueError:
        return
    except FunstatError as e:
        await _log(cb.from_user.id, f"b{row.id}:detail_{kind}", str(tid), 0, False)
        logger.warning("funstat bot=%s detail: %s (%s)", row.id, e.message, e.code)
        await _alert_admin(e, row.id)
        await cb.message.answer(await _error_text(e, row, cb.from_user.id))
        return
    await _log(cb.from_user.id, f"b{row.id}:detail_{kind}", str(tid), _cost_of(d), True)
    await cb.message.answer(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=_kb(
        [_btn("🔎 جست‌وجویِ تازه", "fsb:again", ButtonStyle.PRIMARY)], _HOME_ROW))


# ───────────── آمار و تنظیمِ مالک ─────────────

async def stats_text(row: BotRow) -> str:
    user_limit, bot_limit = await _limits(row.id)
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with Session() as s:
        total = int(await s.scalar(select(func.count(FunstatLog.id)).where(
            FunstatLog.action.like(f"b{row.id}:%"), FunstatLog.ok == True)) or 0)  # noqa: E712
        users_today = int(await s.scalar(select(func.count(func.distinct(FunstatLog.telegram_id))).where(
            FunstatLog.action.like(f"b{row.id}:%"), FunstatLog.created_at >= start)) or 0)
    today = await _count_today(row.id)
    return ("📊 <b>آمارِ Fun Stat این ربات</b>\n\n"
            f"🔎 جست‌وجوهایِ امروز: <b>{today}</b> (توسطِ {users_today} نفر)\n"
            f"🧮 مجموعِ جست‌وجوها: <b>{total}</b>\n\n"
            f"🔢 سقفِ روزانهٔ هر کاربر: {user_limit or 'بدون سقف'}\n"
            f"🏷 سقفِ روزانهٔ کلِ ربات: {bot_limit or 'بدون سقف'}")


async def _owner_row(cb: CallbackQuery, bot: Bot) -> BotRow | None:
    row = await _row(bot)
    if not row or not await _is_privileged(row, cb.from_user.id):
        await cb.answer("فقط مالکِ ربات.", show_alert=True)
        return None
    return row


@funstat_bot_router.callback_query(F.data == "fsb:stats")
async def fsb_stats(cb: CallbackQuery, bot: Bot):
    row = await _owner_row(cb, bot)
    if not row:
        return
    await cb.message.answer(await stats_text(row), parse_mode="HTML",
                            reply_markup=_kb([_btn("⬅️ بازگشت", "tadm:menu", ButtonStyle.DANGER)]))
    await cb.answer()
