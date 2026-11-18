"""PATCH_MSG_CLEANUP_V1 — «پاک‌سازیِ خودکارِ پیام‌ها» (ایلیا؛ پلنِ حرفه‌ای، پیش‌فرض خاموش).

بعد از هر کارِ مشتری در چتِ خصوصی، پیام‌هایِ منو/صفحهٔ قبلیِ ربات (همان روز) پاک می‌شود و فقط پیامِ کارِ جدید می‌ماند؛
مثلاً مشتری از «کیف پول» «بازگشت» را بزند ⇒ پیامِ منویِ کیف پول پاک می‌شود و فقط منویِ اصلی می‌ماند.

چه چیزی هرگز پاک نمی‌شود (محافظه‌کارانه):
  • پیام‌هایی که ربات «خودش» می‌فرستد، نه در پاسخ به کارِ مشتری (پیامِ اختصاصی، امتیازدهی به کانفیگ، اخطار، اطلاعیه، …):
    فقط پیام‌هایی ثبت می‌شوند که *درونِ پردازشِ همان آپدیتِ مشتری* و به همان چت ارسال شده باشند.
  • رسانه‌ها (QR، فایل، عکس)، پیام‌هایی که لینک/کانفیگ دارند (۲ علامتِ «://»)، و پیام‌هایی که کیبوردِ پایین (ReplyKeyboard) می‌آورند.
  • چتِ مالک و ادمین‌ها؛ گروه‌ها؛ پیام‌هایِ روزهایِ قبل؛ پیام‌هایِ خودِ مشتری.
ثبتِ پیام‌ها در حافظهٔ همین پروسه است (با ری‌استارتِ ربات، پیام‌هایِ قبلی دیگر پاک نمی‌شوند؛ بی‌خطر).
"""
from __future__ import annotations

import contextvars
import datetime as _dt

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.methods import (DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

SETTING_KEY = "msg_cleanup"
msg_cleanup_router = Router(name="msg_cleanup")

# (bot_id, chat_id) -> {"day": "YYYY-MM-DD", "ids": [message_id, ...]}
_TRACK: dict[tuple[int, int], dict] = {}
_MAX_TRACK = 40
_CTX: contextvars.ContextVar = contextvars.ContextVar("botsaz_msg_cleanup_ctx", default=None)


def _today() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def trackable(method) -> bool:
    """فقط پیامِ متنیِ منو/صفحه (نه رسانه، نه لینک/کانفیگ، نه کیبوردِ پایین) قابلِ پاک‌سازی است."""
    if not isinstance(method, SendMessage):
        return False
    if isinstance(getattr(method, "reply_markup", None), (ReplyKeyboardMarkup, ReplyKeyboardRemove)):
        return False
    if getattr(method, "protect_content", None) is True:     # (مقدارِ پیش‌فرضِ aiogram یک شیٔ Default است، نه True)
        return False
    return "://" not in (method.text or "")


def begin(bot_id: int, chat_id: int) -> dict:
    return {"bot_id": bot_id, "chat_id": chat_id, "new": set(), "touched": set()}


async def session_mw(make_request, bot, method):
    """میان‌افزارِ نشستِ aiogram: پیام‌هایِ ارسال/ویرایش‌شده در همین آپدیت را ثبت می‌کند."""
    ctx = _CTX.get()
    result = await make_request(bot, method)
    if ctx is None:
        return result
    try:
        cid = getattr(method, "chat_id", None)
        if cid != ctx["chat_id"]:
            return result
        if isinstance(method, SendMessage):
            mid = getattr(result, "message_id", None)
            if mid:
                if trackable(method):
                    ctx["new"].add(int(mid))
                else:
                    ctx["touched"].add(int(mid))     # پیامِ محافظت‌شده: دیده می‌شود ولی هرگز پاک نمی‌شود
        elif isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            mid = getattr(method, "message_id", None)
            if mid:
                ctx["touched"].add(int(mid))
                if not getattr(method, "text", None) or "://" not in (method.text or ""):
                    ctx["new"].add(int(mid))          # صفحه‌ای که همین‌جا ویرایش شد، «پیامِ جاری» است
        elif isinstance(method, DeleteMessage):
            ctx["new"].discard(int(method.message_id))
    except Exception:
        logger.exception("msg_cleanup: ثبتِ پیام ناموفق بود (نادیده گرفته شد)")
    return result


def install_session_mw(bot) -> None:
    if not getattr(bot.session, "_msg_cleanup_mw", False):
        bot.session.middleware(session_mw)
        bot.session._msg_cleanup_mw = True


async def finish(bot, ctx: dict) -> int:
    """بعد از پردازشِ آپدیت: پیام‌هایِ ثبت‌شدهٔ قبلی (جز پیام‌هایِ این آپدیت) پاک می‌شوند. خروجی: تعدادِ پاک‌شده."""
    cur = ctx["new"] | ctx["touched"]
    if not cur:
        return 0                                       # آپدیتی که پیامی نساخت (مثلاً فقط هشدارِ دکمه) صفحه را خالی نمی‌کند
    key = (ctx["bot_id"], ctx["chat_id"])
    day = _today()
    rec = _TRACK.get(key)
    prev = list(rec["ids"]) if rec and rec["day"] == day else []
    deleted = 0
    for mid in prev:
        if mid in cur:
            continue
        try:
            await bot.delete_message(ctx["chat_id"], mid)
            deleted += 1
        except Exception:
            logger.debug("msg_cleanup: پاک‌کردنِ پیام ممکن نشد (قدیمی/پاک‌شده)")
    keep = sorted(ctx["new"])[-_MAX_TRACK:]
    _TRACK[key] = {"day": day, "ids": keep}
    return deleted


async def enabled_for(bot, event) -> int | None:
    """bot_id اگر برایِ این آپدیت پاک‌سازی باید کار کند، وگرنه None."""
    chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
    user = getattr(event, "from_user", None)
    if chat is None or user is None or getattr(chat, "type", "") != "private":
        return None
    try:
        import tenant as T
        from plans import Feature, has_feature
        row = await T.get_bot_row(bot)
        if not row or not has_feature(row.plan, Feature.MSG_CLEANUP):
            return None
        if (await T.get_setting(row.id, SETTING_KEY, "0")) != "1":
            return None
        if await T.is_owner_or_admin(row.id, user.id, row.owner_id):
            return None
        return row.id
    except Exception:
        logger.exception("msg_cleanup: بررسیِ فعال‌بودن ناموفق بود")
        return None


async def outer_mw(handler, event, data):
    bot = data.get("bot")
    bot_id = await enabled_for(bot, event) if bot is not None else None
    if bot_id is None:
        return await handler(event, data)
    chat = getattr(event, "chat", None) or event.message.chat
    install_session_mw(bot)
    ctx = begin(bot_id, chat.id)
    tok = _CTX.set(ctx)
    try:
        return await handler(event, data)
    finally:
        _CTX.reset(tok)
        try:
            await finish(bot, ctx)
        except Exception:
            logger.exception("msg_cleanup: پاک‌سازی ناموفق بود (نادیده گرفته شد)")


# نکته: outer_middleware فقط باید رویِ روترِ «والد» (tenant_router) نصب شود تا کلِ زنجیرهٔ هندلرها داخلش بماند؛
# روی زیرروتر فقط هندلرهایِ خودش را می‌پوشاند. نصب در tenant/__init__.py انجام می‌شود (install_on).
def install_on(router) -> None:
    router.message.outer_middleware(outer_mw)
    router.callback_query.outer_middleware(outer_mw)


# ───────────── پنلِ مالک ─────────────
async def _menu(bot_id: int, plan: str):
    from tenant import get_setting
    on = (await get_setting(bot_id, SETTING_KEY, "0")) == "1"
    text = ("🧹 <b>پاک‌سازیِ خودکارِ پیام‌ها</b> · 💎 حرفه‌ای\n\n"
            "بعد از هر کارِ مشتری، پیام‌هایِ منو/صفحهٔ قبلیِ همان روز پاک می‌شود و فقط پیامِ کارِ جدید می‌ماند "
            "(مثلاً از «کیف پول» «بازگشت» را بزند ⇒ منویِ کیف پول پاک می‌شود و فقط منویِ اصلی می‌ماند).\n\n"
            f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n\n"
            "🔒 <b>هرگز پاک نمی‌شود:</b> پیام‌هایی که خودِ ربات می‌فرستد (پیامِ اختصاصی، امتیازدهی به کانفیگ، اخطار، اطلاعیه)، "
            "رسانه/QR/فایل، پیام‌هایِ دارایِ لینک و کانفیگ، پیام‌هایِ خودِ مشتری، و چتِ مالک و ادمین‌ها.\n"
            "ℹ️ فقط چتِ خصوصیِ مشتری‌ها و فقط پیام‌هایِ همان روز. با ری‌استارتِ ربات، فهرستِ پیام‌هایِ قبلی از نو شروع می‌شود.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش‌کردن" if on else "🟢 روشن‌کردن"), callback_data="mcl:tog",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    return text, kb


async def _render(cb, row):
    text, kb = await _menu(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@msg_cleanup_router.callback_query(F.data.in_({"mcl:menu", "mcl:tog"}))
async def mcl_menu(cb, bot):
    from tenant import _owner_ok, get_setting, set_setting
    from plans import Feature, has_feature, upgrade_hint
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not has_feature(row.plan, Feature.MSG_CLEANUP):
        return await cb.answer(upgrade_hint(Feature.MSG_CLEANUP), show_alert=True)
    if cb.data == "mcl:tog":
        cur = (await get_setting(row.id, SETTING_KEY, "0")) == "1"
        await set_setting(row.id, SETTING_KEY, "0" if cur else "1")
        await cb.answer("خاموش شد" if cur else "روشن شد")
    else:
        await cb.answer()
    await _render(cb, row)
