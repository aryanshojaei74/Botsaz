"""«کپچا هنگام استارت» (Royal): کاربر چه با لینکِ دعوت چه بدون آن، وقتی ربات را /start می‌کند اول کپچا می‌گیرد؛ منو بعد از حلِ آن می‌آید.

=== PATCH_START_CAPTCHA_V1 ===
• تنظیمِ مالک (هر ربات جدا، پیش‌فرض خاموش): خاموش | «فقط کاربرانِ جدید» | «همهٔ کاربران (یک‌بار)».
  «جدید» = کاربری که همین /start برایش ساخته شده (یا چالشِ حل‌نشده دارد)؛ «همه» = هر کسی که هنوز تأیید نشده، حتی مشتریِ قدیمی (یک‌بار).
• سؤال: جمعِ دو عدد با ۴ دکمهٔ شیشه‌ای. ۳ پاسخِ غلط ⇒ ۱۰ دقیقه محدودیتِ /start. مهلتِ هر سؤال ۱۰ دقیقه.
• تا حل نشود: نه خوش‌آمد، نه منو، نه ثبتِ دعوت، نه هدیهٔ خوش‌آمد. بعد از حلِ درست، «همان /start» (با همان پارامترِ دیپ‌لینک) دوباره اجرا می‌شود،
  یعنی دعوت/هدیه/صفحهٔ دکمهٔ کانال دقیقاً مثلِ حالتِ بدونِ کپچا کار می‌کند (و اگر کپچایِ زیرمجموعه‌گیری هم روشن باشد، دو بار پرسیده نمی‌شود).
• مالک/ادمین‌ها و چتِ گروه معاف‌اند. هنوز «دیوارِ کاملِ ربات» نیست: فقط راهِ ورودِ /start را می‌گیرد (دکمه‌هایِ پیام‌هایِ قدیمی کار می‌کنند).
• وضعیت در جدولِ Setting (scap:<tg> و scap:ok:<tg>) است؛ با ری‌استارت گم نمی‌شود.
"""
from __future__ import annotations

import contextvars
import json
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

SETTING_KEY = "start_captcha"
MODES = ("0", "new", "all")
TTL_SEC = 600
MAX_TRIES = 3
BLOCK_SEC = 600

start_captcha_router = Router(name="start_captcha")

# وقتی /startِ بعد از حلِ کپچا بازپخش می‌شود: "new" = کاربرِ تازه (دعوت/هدیه ثبت شود) | "old" | None = /startِ عادی
SCAP_REPLAY: contextvars.ContextVar = contextvars.ContextVar("botsaz_scap_replay", default=None)


def _key(tg: int) -> str:
    return f"scap:{int(tg)}"


def _okey(tg: int) -> str:
    return f"scap:ok:{int(tg)}"


async def mode(bot_id: int) -> str:
    from tenant import get_setting
    m = (await get_setting(bot_id, SETTING_KEY, "0")).strip().lower()
    return m if m in MODES else "0"


async def _get(bot_id: int, tg: int) -> dict | None:
    from tenant.shared.extras import _eget
    raw = await _eget(bot_id, _key(tg), "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _put(bot_id: int, tg: int, data: dict | None) -> None:
    from tenant.shared.extras import _eset
    await _eset(bot_id, _key(tg), json.dumps(data) if data else "")


async def is_verified(bot_id: int, tg: int) -> bool:
    from tenant.shared.extras import _eget
    return (await _eget(bot_id, _okey(tg), "")) == "1"


async def _mark_verified(bot_id: int, tg: int) -> None:
    from tenant.shared.extras import _eset
    await _eset(bot_id, _okey(tg), "1")


def _kb(token: str, opts: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(o), callback_data=f"scp:{token}:{o}", style=ButtonStyle.PRIMARY) for o in opts]])


def _text(q: str, tries_left: int) -> str:
    return ("🧩 <b>تأییدِ هویت</b>\n\nبرایِ ورود به ربات، جوابِ درست را بزن:\n\n"
            f"<b>{q}</b>\n\n(فرصتِ باقی‌مانده: {tries_left})")


async def gate(msg: Message, bot: Bot, row, was_created: bool, args: str | None) -> bool:
    """True ⇒ کپچا فرستاده شد (یا کاربر موقتاً محدود است) و /start باید همین‌جا تمام شود. هرگز استثنا نمی‌دهد؛ خطا ⇒ False (کاربر گم نشود)."""
    try:
        if SCAP_REPLAY.get() is not None:
            return False
        if msg.chat.type != "private" or not msg.from_user or msg.from_user.is_bot:
            return False
        m = await mode(row.id)
        if m == "0":
            return False
        tg = msg.from_user.id
        from tenant import is_owner_or_admin
        if await is_owner_or_admin(row.id, tg, row.owner_id):
            return False
        if await is_verified(row.id, tg):
            return False
        rec = await _get(row.id, tg)
        if m == "new" and not (was_created or rec):
            return False                      # مشتریِ قدیمیِ قبل از روشن‌شدنِ کپچا؛ فقط در حالتِ «همه» پرسیده می‌شود
        now = time.time()
        if rec and float(rec.get("until", 0) or 0) > now:
            mins = max(1, int((float(rec["until"]) - now) // 60) + 1)
            await msg.answer(f"⛔️ چند پاسخِ غلط دادی. حدود {mins} دقیقهٔ دیگر دوباره /start بزن.")
            return True
        from tenant.shared.referral_captcha import make_question
        q, ans, opts = make_question()
        token = secrets.token_hex(3)
        tries = int(rec.get("tries", 0)) if rec else 0
        new = bool(was_created or (rec or {}).get("new"))
        keep_args = args if args else (rec or {}).get("args")           # /startِ دوباره بدونِ پارامتر، لینکِ اولیه را گم نکند
        await _put(row.id, tg, {"t": token, "ans": ans, "args": keep_args, "new": new, "tries": tries, "until": 0,
                                "exp": now + TTL_SEC})
        await msg.answer(_text(q, MAX_TRIES - tries), reply_markup=_kb(token, opts), parse_mode="HTML")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("start captcha: gate failed; letting the user in")
        return False


async def _replay_start(cb: CallbackQuery, bot: Bot, row, state: FSMContext, rec: dict) -> None:
    """/startِ اصلی را (با همان پارامتر) برایِ همین کاربر دوباره اجرا می‌کند؛ اگر نشد، دست‌کم خوش‌آمد و منو را می‌فرستد."""
    import datetime as _dt
    from aiogram.filters import CommandObject
    from aiogram.types import Message as _Msg
    args = rec.get("args") or None
    tok = SCAP_REPLAY.set("new" if rec.get("new") else "old")
    try:
        from tenant import tenant_start
        fake = _Msg(message_id=0, date=_dt.datetime.utcnow(), chat=cb.message.chat, from_user=cb.from_user,
                    text="/start" + (f" {args}" if args else "")).as_(bot)
        await tenant_start(fake, CommandObject(prefix="/", command="start", args=args), bot, state)
    except Exception:  # noqa: BLE001
        logger.exception("start captcha: replaying /start failed; sending the welcome menu")
        try:
            from tenant import send_welcome_menu_after_captcha
            await send_welcome_menu_after_captcha(bot, row, cb)
        except Exception:  # noqa: BLE001
            logger.exception("start captcha: welcome menu fallback failed")
    finally:
        SCAP_REPLAY.reset(tok)


@start_captcha_router.callback_query(F.data.startswith("scp:"))
async def scp_answer(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    tg = cb.from_user.id
    try:
        _, token, val = cb.data.split(":")
        val = int(val)
    except ValueError:
        return await cb.answer()
    rec = await _get(row.id, tg)
    if not rec or rec.get("t") != token:
        return await cb.answer("این سؤال دیگر معتبر نیست؛ /start را دوباره بزن.", show_alert=True)
    if float(rec.get("exp", 0)) < time.time():
        keep = {k: rec[k] for k in ("args", "new", "tries") if k in rec}
        await _put(row.id, tg, {**keep, "t": "", "exp": 0, "until": 0})          # سؤالِ منقضی بسته؛ پیشینهٔ دعوت/تلاش می‌ماند
        try:
            await cb.message.edit_text("⌛ مهلتِ این سؤال تمام شد. برایِ سؤالِ تازه /start را بزن.")
        except Exception:  # noqa: BLE001
            logger.debug("scp: expired edit failed")
        return await cb.answer()
    if val == int(rec["ans"]):
        await _mark_verified(row.id, tg)
        await _put(row.id, tg, None)
        try:
            await cb.message.edit_text("✅ تأیید شد.")
        except Exception:  # noqa: BLE001
            logger.debug("scp: success edit failed")
        await cb.answer("✅")
        await _replay_start(cb, bot, row, state, rec)
        return
    tries = int(rec.get("tries", 0)) + 1
    if tries >= MAX_TRIES:
        await _put(row.id, tg, {"args": rec.get("args"), "new": rec.get("new"), "tries": 0, "t": "", "exp": 0,
                                "until": time.time() + BLOCK_SEC})
        try:
            await cb.message.edit_text("⛔️ پاسخِ درست داده نشد. ۱۰ دقیقهٔ دیگر دوباره /start بزن.")
        except Exception:  # noqa: BLE001
            logger.debug("scp: block edit failed")
        return await cb.answer("پاسخ غلط بود.", show_alert=True)
    from tenant.shared.referral_captcha import make_question
    q, ans, opts = make_question()
    rec.update(ans=ans, t=secrets.token_hex(3), tries=tries)
    await _put(row.id, tg, rec)
    try:
        await cb.message.edit_text(_text(q, MAX_TRIES - tries), reply_markup=_kb(rec["t"], opts), parse_mode="HTML")
    except Exception:  # noqa: BLE001
        logger.debug("scp: new question edit failed")
    await cb.answer("پاسخ غلط بود؛ سؤالِ تازه.")


# ───────────── پنلِ مالک ─────────────
_MODE_FA = {"0": "❌ خاموش", "new": "🟢 فقط کاربرانِ جدید", "all": "🟣 همهٔ کاربران (یک‌بار)"}


async def _menu(bot_id: int):
    m = await mode(bot_id)
    text = ("🧩 <b>کپچا هنگامِ استارت</b>\n\n"
            "وقتی کاربری ربات را /start می‌کند (چه با لینکِ دعوت چه بدون آن)، اول یک سؤالِ جمعِ ساده می‌گیرد؛ "
            "خوش‌آمد و منوی اصلی بعد از جوابِ درست می‌آید. برایِ جلوگیری از رباتِ فیک و اسپم.\n\n"
            f"وضعیت: <b>{_MODE_FA[m]}</b>\n\n"
            "• «فقط کاربرانِ جدید»: کسی که همین الان برایِ اولین بار وارد می‌شود.\n"
            "• «همهٔ کاربران»: هر کسی که هنوز تأیید نشده، حتی مشتریِ قدیمی (فقط یک بار).\n"
            "• ۳ پاسخِ غلط ⇒ ۱۰ دقیقه محدودیت. مالک و ادمین‌ها کپچا نمی‌گیرند (برایِ تست با حسابِ دیگری وارد شو).\n"
            "ℹ️ این فقط ورودیِ /start را می‌گیرد؛ دکمه‌هایِ پیام‌هایِ قدیمی همچنان کار می‌کنند.")
    rows = [[InlineKeyboardButton(text=("✅ " if m == k else "") + _MODE_FA[k], callback_data=f"scpm:{k}",
                                  style=ButtonStyle.SUCCESS if m == k else ButtonStyle.PRIMARY)] for k in MODES]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@start_captcha_router.callback_query(F.data.in_({"scpm:menu", "scpm:0", "scpm:new", "scpm:all"}))
async def scp_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if cb.data != "scpm:menu":
        m = cb.data.split(":")[1]
        await set_setting(row.id, SETTING_KEY, m)
        await cb.answer({"0": "خاموش شد", "new": "برایِ کاربرانِ جدید روشن شد", "all": "برایِ همهٔ کاربران روشن شد"}[m])
    else:
        await cb.answer()
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
