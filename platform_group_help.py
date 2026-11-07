"""platform_group_help.py — «راهنما گروه» در رباتِ اصلیِ بات‌ساز (PATCH_PLATFORM_GROUP_HELP_V1).

هر عضوِ گروهِ مجاز بنویسد «راهنما گروه» ⇒ راهنمایِ کاملِ کیف پول، انتقال، بازیِ دونفره، بازی‌هایِ رایگانِ شیرینی‌دار و
بازی‌هایِ جایزه‌دارِ تلگرام. فقط بخش‌هایی که همین الان فعال‌اند نشان داده می‌شوند و عددها از تنظیماتِ ربات می‌آیند.
گروهِ غیرِمجاز ⇒ SkipHandler (به بقیهٔ هندلرها می‌رسد).
"""
from __future__ import annotations

import html
import json
import re

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from config import Session
from models import PlatformDiceGame
from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
platform_group_help_router = Router(name="platform_group_help")

_TRIGGER_RE = re.compile(r"^(?:📖\s*)?راهنما(?:ی)?[\s‌]*گروه\s*[📖]?$")
_N = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_PERIOD_FA = {"day": "در هر روز", "week": "در هر هفته", "total": "در کل"}


def _fa(n) -> str:
    return f"{int(n):,}".translate(_N)


def _t(n) -> str:
    return f"{_fa(n)} تومان"


def _top(g: PlatformDiceGame) -> int:
    try:
        return max((int(v or 0) for v in (json.loads(g.prizes or "{}") or {}).values()), default=0)
    except Exception:      # noqa: BLE001
        return 0


async def _allowed(chat_id: int) -> bool:
    from platform_group_game import allowed_chat
    from platform_group_money import _group_allowed
    return bool(await allowed_chat(chat_id) or await _group_allowed(chat_id))


async def build_platform_group_help(chat_id: int | None = None, bot_username: str = "") -> str:
    from platform_group_money import _bet_enabled, _int, _pset, _wallet_enabled
    uname = f"@{bot_username}" if bot_username else "ربات"
    L: list[str] = ["📖 <b>راهنمای کاملِ گروه</b>", "━━━━━━━━━━━━━━", ""]

    if await _wallet_enabled():
        mn = max(1, _int(await _pset("platform_group_transfer_min", "1000"), 1000))
        mx = _int(await _pset("platform_group_transfer_max", "0"))
        fee_on = (await _pset("platform_group_transfer_fee_enabled", "off")) == "on"
        fee = await _pset("platform_group_transfer_fee_pct", "0")
        L += ["💰 <b>۱) موجودی و شارژ</b>",
              "• بنویس <code>موجودی</code> تا موجودیِ کیف پولت را ببینی.",
              f"• شارژ: وارد {uname} شو ← «💰 کیف پول» ← «شارژ». همان موجودی در گروه قابلِ استفاده است.",
              "",
              "🔁 <b>۲) انتقالِ موجودی به دیگران</b>",
              "۱. روی <b>پیامِ همان شخص</b> ریپلای کن.",
              "۲. بنویس <code>انتقال 5000</code> (به‌جایِ ۵۰۰۰ مبلغِ دلخواه به تومان).",
              f"• حداقل: <b>{_t(mn)}</b>" + (f" — حداکثر: <b>{_t(mx)}</b>" if mx else " — حداکثر: نامحدود"),
              (f"• کارمزد: <b>{html.escape(str(fee))}٪</b>" if fee_on and str(fee) not in ("", "0", "0.0") else "• کارمزد: ندارد ✅"),
              "• به ربات یا خودت نمی‌شود انتقال داد؛ انتقال برگشت‌پذیر نیست، پس قبل از ارسال مطمئن شو.",
              ""]

    if await _bet_enabled():
        bmn = max(1, _int(await _pset("platform_group_betting_min", "1000"), 1000))
        bmx = _int(await _pset("platform_group_betting_max", "0"))
        ttl = max(60, min(_int(await _pset("platform_group_betting_ttl", "300"), 300), 86400))
        L += ["🎲 <b>۳) بازیِ دونفره با کیف پول (اختیاری)</b>",
              "• بنویس <code>بازی 10000</code> (یا <code>xo 10000</code>، <code>سنگ کاغذ قیچی 10000</code>، <code>تاس 10000</code>).",
              "• مبلغِ تو همان لحظه از کیف پول کنار گذاشته می‌شود؛ اولین نفری که «✅ قبول و پیوستن» را بزند حریفِ تو می‌شود. "
              "برایِ بازی با یک نفرِ مشخص، روی پیامش ریپلای کن.",
              "• برنده کلِ مبلغِ دو نفر (منهایِ کارمزدِ احتمالی) را می‌گیرد؛ مساوی ⇒ مبلغ به هر دو برمی‌گردد.",
              f"• حداقل: <b>{_t(bmn)}</b>" + (f" — حداکثر: <b>{_t(bmx)}</b>" if bmx else " — حداکثر: نامحدود"),
              f"• اگر تا <b>{_fa(ttl // 60)} دقیقه</b> کسی قبول نکند یا خودت لغو کنی، مبلغ کامل برمی‌گردد.",
              "• برایِ دیدنِ قوانینِ هر بازی بنویس <code>بازی</code>.",
              "⚠️ فقط با مبلغی بازی کن که از دست‌دادنش برایت مهم نیست؛ بازی راهِ کسب‌درآمدِ تضمینی نیست.",
              ""]

    # بازی‌هایِ رایگانِ شیرینی‌دار (حدس عدد / سرعت / ریاضی)
    try:
        import platform_group_game as G
        if await G.enabled():
            free = await G.help_text(bet_games=False, compact=True)
            if free:
                L += ["🍬 <b>بازی‌هایِ رایگان</b>", free.replace("🎮 <b>بازی‌هایِ گروه</b>\n", "").strip(), ""]
    except Exception:      # noqa: BLE001
        logger.exception("platform group help: بخشِ بازی‌هایِ رایگان ساخته نشد")

    # بازی‌هایِ جایزه‌دارِ تلگرام (تاس/دارت/…)
    try:
        async with Session() as s:
            games = [g for g in (await s.scalars(select(PlatformDiceGame).where(
                PlatformDiceGame.active.is_(True), PlatformDiceGame.in_groups.is_(True)).order_by(PlatformDiceGame.id))).all()
                if g.group_chat_id is None or chat_id is None or g.group_chat_id == chat_id]
    except Exception:      # noqa: BLE001
        logger.exception("platform group help: خواندنِ بازی‌هایِ جایزه‌دار ناموفق بود")
        games = []
    if games:
        L += ["🎁 <b>بازی‌هایِ جایزه‌دارِ روزانه (رایگان)</b>",
              "بازی‌هایِ داخلیِ تلگرام که هزینه‌ای ندارند؛ نتیجه را خودِ تلگرام تعیین می‌کند (نه ربات و نه مدیر) و اگر نتیجه جایزه داشته باشد "
              "مبلغ مستقیم به کیف پولت اضافه می‌شود.",
              "<b>بازی‌هایِ فعال:</b>"]
        for g in games:
            top = _top(g)
            lim = "بدونِ محدودیت" if not g.tries_limit else f"{_fa(g.tries_limit)} بار {_PERIOD_FA.get(g.tries_period, '')}"
            L.append(f"• {g.emoji} <b>{html.escape(g.title)}</b> — سهمیه: {lim}" + (f" — تا <b>{_t(top)}</b> جایزه" if top else ""))
        L += [f"• برایِ بازی، نامِ بازی یا ایموجی‌اش را بفرست؛ مثلاً <code>{html.escape(games[0].title)}</code>.",
              "• سهمیه که تمام شد، ربات زمانِ بازیِ بعدی را می‌گوید؛ هر روز سر بزن.",
              "ℹ️ مقدارِ جایزه‌ها را مدیر تعیین می‌کند و ممکن است هر بار جایزه‌ای نباشد؛ این امتیازِ تشویقی است، نه درآمدِ تضمین‌شده.",
              ""]

    if len(L) <= 3:
        L += ["هنوز هیچ قابلیتی (کیف پول، بازی) در این گروه فعال نشده است."]
    L += ["━━━━━━━━━━━━━━", "❓ سؤالی داشتی؟ در پیوی ربات به پشتیبانی پیام بده.",
          "این راهنما را هر وقت خواستی با نوشتنِ <code>راهنما گروه</code> دوباره ببین."]
    return "\n".join(L)


@platform_group_help_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                                    F.func(lambda m: bool(_TRIGGER_RE.match((m.text or "").strip()))))
async def platform_group_help(msg: Message):
    if not await _allowed(msg.chat.id):
        raise SkipHandler
    try:
        me = await msg.bot.me()
        text = await build_platform_group_help(msg.chat.id, getattr(me, "username", "") or "")
        if len(text) <= 4000:
            await msg.reply(text, parse_mode="HTML", disable_web_page_preview=True)
        else:                                               # سقفِ تلگرام: دو پیام (بریدن در مرزِ بخش‌ها)
            cut = text.rfind("\n\n", 0, 3900)
            await msg.reply(text[:cut], parse_mode="HTML", disable_web_page_preview=True)
            await msg.answer(text[cut:].strip(), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:      # noqa: BLE001
        logger.exception("platform group help: ارسال ناموفق بود")
