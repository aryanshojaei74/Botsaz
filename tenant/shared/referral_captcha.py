"""PATCH_REFERRAL_CAPTCHA_V1 — کپچا برای زیرمجموعه‌گیری (Whale Support؛ قابلِ روشن/خاموش توسطِ مالک، پیش‌فرض خاموش).

وقتی روشن باشد، کاربرِ تازه‌ای که با لینکِ دعوت وارد ربات می‌شود قبل از ثبتِ دعوت (و امتیاز/پاداش) باید یک سؤالِ ساده را
پاسخ دهد (جمعِ دو عدد با ۴ دکمهٔ شیشه‌ای). جوابِ درست ⇒ دعوت ثبت می‌شود؛ ۳ پاسخِ غلط یا گذشتنِ مهلت (۱۰ دقیقه) ⇒ دعوت ثبت
نمی‌شود، ولی کاربر ربات را عادی استفاده می‌کند (فقط شمارشِ دعوت نمی‌شود). هدف: جلوگیریِ ربات‌ها/حساب‌هایِ ساختگی از دعوتِ مصنوعی.

وضعیتِ هر چالش در جدولِ Setting (کلیدِ rfcap:<tg>) نگه داشته می‌شود؛ با ریستارت هم گم نمی‌شود.
"""
from __future__ import annotations

import json
import random
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

SETTING_KEY = "referral_captcha"
TTL_SEC = 600
MAX_TRIES = 3
referral_captcha_router = Router(name="referral_captcha")


def _key(tg: int) -> str:
    return f"rfcap:{int(tg)}"


def make_question(rnd=random) -> tuple[str, int, list[int]]:
    """(متنِ سؤال، جوابِ درست، ۴ گزینهٔ مرتب‌شدهٔ یکتا)."""
    a, b = rnd.randint(2, 9), rnd.randint(2, 9)
    ans = a + b
    opts = {ans}
    while len(opts) < 4:
        opts.add(max(1, ans + rnd.choice([-3, -2, -1, 1, 2, 3, 4])))
    lst = list(opts)
    rnd.shuffle(lst)
    return f"{a} + {b} = ؟", ans, lst


def _kb(token: str, opts: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(o), callback_data=f"rfc:{token}:{o}", style=ButtonStyle.PRIMARY) for o in opts]])


def _text(q: str, tries_left: int) -> str:
    return ("🧩 <b>تأییدِ دعوت</b>\n\nبرایِ ثبتِ ورودت از طریقِ لینکِ دعوت، جوابِ درست را بزن:\n\n"
            f"<b>{q}</b>\n\n(فرصتِ باقی‌مانده: {tries_left})")


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


async def enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, SETTING_KEY, "0")) == "1"


async def gate(bot_id: int, new_user, ref_code: str, bot: Bot) -> bool:
    """True ⇒ چالش فرستاده شد (یا در جریان است) و ثبتِ دعوت باید فعلاً انجام نشود."""
    try:
        if not await enabled(bot_id):
            return False
        from sqlalchemy import select
        from config import Session
        from models import ReferralJoin, ReferralLink
        async with Session() as s:
            link = await s.scalar(select(ReferralLink).where(ReferralLink.bot_id == bot_id, ReferralLink.code == ref_code))
            if link is None or link.user_id == new_user.id:
                return False                                   # لینکِ نامعتبر/خودِ دعوت: مسیرِ عادی (که خودش رد می‌کند)
            joined = await s.scalar(select(ReferralJoin.id).where(
                ReferralJoin.bot_id == bot_id, ReferralJoin.referred_telegram_id == new_user.telegram_id))
        if joined:
            return False
        cur = await _get(bot_id, new_user.telegram_id)
        if cur and cur.get("exp", 0) > time.time():
            return True                                        # چالشِ فعالی دارد؛ دوباره نفرست
        q, ans, opts = make_question()
        token = secrets.token_hex(3)
        await _put(bot_id, new_user.telegram_id,
                   {"t": token, "ans": ans, "ref": ref_code, "exp": time.time() + TTL_SEC, "tries": 0})
        await bot.send_message(new_user.telegram_id, _text(q, MAX_TRIES), reply_markup=_kb(token, opts), parse_mode="HTML")
        return True
    except Exception:
        logger.exception("referral captcha: ساختِ چالش ناموفق بود؛ دعوت بدونِ کپچا ثبت می‌شود")
        return False                                           # هرگز نباید کاربر/دعوت را به‌خاطرِ خطا گم کند


@referral_captcha_router.callback_query(F.data.startswith("rfc:"))
async def rfc_answer(cb: CallbackQuery, bot: Bot):
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
    st = await _get(row.id, tg)
    if not st or st.get("t") != token:
        return await cb.answer("این سؤال دیگر معتبر نیست.", show_alert=True)
    if st.get("exp", 0) < time.time():
        await _put(row.id, tg, None)
        try:
            await cb.message.edit_text("⌛ مهلتِ تأییدِ دعوت تمام شد. ربات را عادی استفاده کن؛ ثبتِ دعوت انجام نشد.")
        except Exception:
            logger.debug("rfc: ویرایشِ پیامِ منقضی ممکن نشد")
        return await cb.answer()
    if val == int(st["ans"]):
        await _put(row.id, tg, None)
        from sqlalchemy import select
        from config import Session
        from models import BotUser
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == tg))
        if u is not None:
            from tenant.shared.extras import handle_referral_join
            await handle_referral_join(row.id, u, str(st["ref"]), bot, _captcha_passed=True)
        try:
            await cb.message.edit_text("✅ تأیید شد؛ ورودت از طریقِ لینکِ دعوت ثبت شد.")
        except Exception:
            logger.debug("rfc: ویرایشِ پیامِ موفقیت ممکن نشد")
        return await cb.answer("✅")
    st["tries"] = int(st.get("tries", 0)) + 1
    if st["tries"] >= MAX_TRIES:
        await _put(row.id, tg, None)
        try:
            await cb.message.edit_text("❌ پاسخِ درست داده نشد؛ ثبتِ دعوت انجام نشد. می‌توانی ربات را عادی استفاده کنی.")
        except Exception:
            logger.debug("rfc: ویرایشِ پیامِ رد ممکن نشد")
        return await cb.answer("پاسخ غلط بود.", show_alert=True)
    q, ans, opts = make_question()                             # بعد از هر غلط، سؤالِ تازه (جلوگیری از حدسِ پشت‌سرهم)
    st.update(ans=ans, t=secrets.token_hex(3))
    await _put(row.id, tg, st)
    try:
        await cb.message.edit_text(_text(q, MAX_TRIES - st["tries"]), reply_markup=_kb(st["t"], opts), parse_mode="HTML")
    except Exception:
        logger.debug("rfc: ویرایشِ سؤالِ تازه ممکن نشد")
    await cb.answer("پاسخ غلط بود؛ سؤالِ تازه.")


@referral_captcha_router.callback_query(F.data == "refrw:captcha")
async def rfc_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, SETTING_KEY, "0")) == "1"
    await set_setting(row.id, SETTING_KEY, "0" if cur else "1")
    await cb.answer("🧩 کپچا " + ("خاموش شد" if cur else "روشن شد؛ کاربرانِ تازه با لینکِ دعوت سؤالِ ساده می‌گیرند"), show_alert=not cur)
    from tenant import set_ref_reward_menu
    await set_ref_reward_menu(cb, bot)
