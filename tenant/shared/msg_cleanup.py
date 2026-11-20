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
START_KEY = "msg_cleanup_start"       # پاک‌کردنِ خودِ پیامِ «/start» مشتری هم (پیش‌فرض خاموش؛ AR SHOP)
STAFF_KEY = "msg_cleanup_staff"       # اعمال روی چتِ مالک/ادمین‌ها هم (پیش‌فرض خاموش؛ برایِ تستِ خودِ مالک)
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
    text = method.text or ""
    if is_order_message(text):          # PATCH_MSG_CLEANUP_KEEP_ORDERS_V1 (سینا): پیام ثبت/تکمیل/لغو سفارش هرگز پاک نمی‌شود
        return False
    return "://" not in text


_ORDER_MARKERS = ("کد پیگیری", "کدِ پیگیری", "شماره سفارش", "شمارهٔ سفارش")


def is_order_message(text: str) -> bool:
    """پیام سفارش: هر پیامی که سفارش را ثبت/تکمیل/لغو اعلام می‌کند (الگوهای vs_msg_override) یا کد پیگیری دارد."""
    if not text:
        return False
    try:
        from tenant.shared.vs_msg_override import rule_keys
        if rule_keys(text):
            return True
    except Exception:  # noqa: BLE001
        logger.debug("msg_cleanup: تشخیص پیام سفارش ممکن نشد", exc_info=True)
    flat = text.replace("\u0650", "")
    return any(m.replace("\u0650", "") in flat for m in _ORDER_MARKERS)


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
            # PATCH_MSG_CLEANUP_STAFF_V1 (ایلیا: «عملیاتی انجام نمی‌دهد» — خودش با حسابِ مالک تست می‌کرد و مالک/ادمین مستثنی بود)
            if (await T.get_setting(row.id, STAFF_KEY, "0")) != "1":
                return None
        return row.id
    except Exception:
        logger.exception("msg_cleanup: بررسیِ فعال‌بودن ناموفق بود")
        return None


async def _delete_start_cmd(bot, bot_id: int, event) -> None:
    """اگر مالک خواسته باشد، خودِ پیامِ «/start» مشتری (کمتر از ۴۸ ساعت) هم پاک می‌شود."""
    text = getattr(event, "text", None)
    if not text or not text.startswith("/start") or getattr(event, "message_id", None) is None:
        return
    try:
        from tenant import get_setting
        if (await get_setting(bot_id, START_KEY, "0")) != "1":
            return
        await bot.delete_message(event.chat.id, event.message_id)
    except Exception:
        logger.debug("msg_cleanup: پاک‌کردنِ پیامِ /start ممکن نشد")


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
        await _delete_start_cmd(bot, bot_id, event)


# نکته: outer_middleware فقط باید رویِ روترِ «والد» (tenant_router) نصب شود تا کلِ زنجیرهٔ هندلرها داخلش بماند؛
# روی زیرروتر فقط هندلرهایِ خودش را می‌پوشاند. نصب در tenant/__init__.py انجام می‌شود (install_on).
def install_on(router) -> None:
    router.message.outer_middleware(outer_mw)
    router.callback_query.outer_middleware(outer_mw)


# ───────────── پنلِ مالک ─────────────
async def _menu(bot_id: int, plan: str):
    from tenant import get_setting
    on = (await get_setting(bot_id, SETTING_KEY, "0")) == "1"
    staff = (await get_setting(bot_id, STAFF_KEY, "0")) == "1"
    st_cmd = (await get_setting(bot_id, START_KEY, "0")) == "1"
    text = ("🧹 <b>پاک‌سازیِ خودکارِ پیام‌ها</b> · 💎 حرفه‌ای\n\n"
            "بعد از هر کارِ مشتری، پیام‌هایِ منو/صفحهٔ قبلیِ همان روز پاک می‌شود و فقط پیامِ کارِ جدید می‌ماند "
            "(مثلاً از «کیف پول» «بازگشت» را بزند ⇒ منویِ کیف پول پاک می‌شود و فقط منویِ اصلی می‌ماند).\n\n"
            f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n\n"
            "🔒 <b>هرگز پاک نمی‌شود:</b> پیام‌هایی که خودِ ربات می‌فرستد (پیامِ اختصاصی، امتیازدهی به کانفیگ، اخطار، اطلاعیه)، "
            "رسانه/QR/فایل، پیام‌هایِ دارایِ لینک و کانفیگ، و پیام‌هایِ خودِ مشتری.\n"
            "ℹ️ فقط چتِ خصوصی و فقط پیام‌هایِ <b>از لحظهٔ روشن‌شدن به بعد</b> (پیام‌هایِ قدیمی‌تر پاک نمی‌شوند). با ری‌استارتِ ربات، فهرستِ پیام‌هایِ قبلی از نو شروع می‌شود.\n\n"
            f"👮 چتِ مالک و ادمین‌ها: <b>{'✅ هم اعمال می‌شود' if staff else '❌ مستثنی (پیش‌فرض)'}</b>\n"
            "⚠️ اگر خودت (مالک) ربات را تست می‌کنی، این گزینه را روشن کن؛ وگرنه در چتِ خودت چیزی پاک نمی‌شود. "
            "برایِ استفادهٔ عادی خاموشش بگذار تا صفحه‌هایِ پنلِ مدیریت پاک نشوند.\n\n"
            f"🧾 پاک‌کردنِ خودِ پیامِ «/start» کاربر: <b>{'✅ روشن' if st_cmd else '❌ خاموش (پیش‌فرض)'}</b>\n"
            "اگر روشن باشد، هر بار کاربر /start بزند، پیامِ /start خودش هم از چت پاک می‌شود (فقط کمتر از ۴۸ ساعت).")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش‌کردن" if on else "🟢 روشن‌کردن"), callback_data="mcl:tog",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text=("👮 چتِ مالک/ادمین هم: ✅ روشن" if staff else "👮 چتِ مالک/ادمین هم: ❌ خاموش"),
                              callback_data="mcl:staff", style=(ButtonStyle.SUCCESS if staff else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=("🧾 پاک‌کردنِ پیامِ /start: ✅ روشن" if st_cmd else "🧾 پاک‌کردنِ پیامِ /start: ❌ خاموش"),
                              callback_data="mcl:start", style=(ButtonStyle.SUCCESS if st_cmd else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    return text, kb


async def _render(cb, row):
    text, kb = await _menu(row.id, row.plan)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@msg_cleanup_router.callback_query(F.data.in_({"mcl:menu", "mcl:tog", "mcl:staff", "mcl:start"}))
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
    elif cb.data == "mcl:staff":
        cur = (await get_setting(row.id, STAFF_KEY, "0")) == "1"
        await set_setting(row.id, STAFF_KEY, "0" if cur else "1")
        await cb.answer("چتِ مالک/ادمین از این به بعد هم پاک‌سازی می‌شود" if not cur else "چتِ مالک/ادمین مستثنی شد")
    elif cb.data == "mcl:start":
        cur = (await get_setting(row.id, START_KEY, "0")) == "1"
        await set_setting(row.id, START_KEY, "0" if cur else "1")
        await cb.answer("پیامِ /start دیگر پاک نمی‌شود" if cur else "از این به بعد پیامِ /start هم پاک می‌شود")
    else:
        await cb.answer()
    await _render(cb, row)
