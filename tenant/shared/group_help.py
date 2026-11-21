"""group_help.py — «راهنما گروه»: هر عضوِ گروه این را بنویسد، راهنمای کاملِ کیف پول/انتقال/بازی/جایزه‌هایِ روزانه را می‌گیرد.

متن پویاست: فقط بخش‌هایی که در همین ربات فعال‌اند نشان داده می‌شوند و عددها (حداقل/حداکثر/کارمزد/مهلت) از تنظیماتِ
همان ربات می‌آیند. (PATCH_GROUP_HELP_V1)
"""
from __future__ import annotations

import html
import json
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from config import Session
from models import DiceGame
from logging_setup import get_logger as _get_logger
from tenant.shared.group_wallet import _group_feature_allowed

logger = _get_logger(__name__)
group_help_router = Router()

_TRIGGER_RE = re.compile(r"^(?:📖\s*)?راهنما(?:ی)?\s*گروه\s*[📖]?$")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _fa(n) -> str:
    return f"{int(n):,}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _t(n) -> str:
    return f"{_fa(n)} تومان"


def _to_int(v, default=0) -> int:
    try:
        return int(str(v or "").translate(_DIGITS).replace(",", "").replace("،", "").strip() or default)
    except ValueError:
        return default


_PERIOD_FA = {"day": "در هر روز", "week": "در هر هفته", "total": "در کل"}


def _top_prize(g: DiceGame) -> int:
    try:
        d = json.loads(g.prizes or "{}") or {}
        return max((int(v or 0) for v in d.values()), default=0)
    except Exception:      # noqa: BLE001
        return 0


async def build_group_help(row, chat_id: int | None = None) -> str:
    from tenant import get_setting
    uname = f"@{row.username}" if row.username else "ربات"
    L: list[str] = [
        "📖 <b>راهنمای کاملِ گروه</b>",
        "━━━━━━━━━━━━━━",
        "",
        "💰 <b>۱) موجودی و شارژ</b>",
        "• بنویس <code>موجودی</code> تا موجودیِ کیف پولت را ببینی.",
        f"• شارژِ کیف پول: وارد {uname} شو ← «💰 کیف پول» ← «شارژ». هر چه شارژ کنی، همین‌جا در گروه هم قابلِ استفاده است.",
        "• موجودیِ گروه و ربات یکی است؛ جداگانه شارژ نمی‌شود.",
        "",
    ]

    # ── انتقال ──
    tmin = _to_int(await get_setting(row.id, "group_transfer_min", "1000"), 1000) or 1000
    tmax = _to_int(await get_setting(row.id, "group_transfer_max", "0"))
    fee_on = (await get_setting(row.id, "group_transfer_fee_enabled", "0")) == "1"
    fee_pct = await get_setting(row.id, "group_transfer_fee_pct", "0")
    by_user = (await get_setting(row.id, "group_transfer_by_username", "0")) == "1"
    L += [
        "🔁 <b>۲) انتقالِ موجودی به دیگران</b>",
        "۱. روی <b>پیامِ همان شخص</b> ریپلای کن.",
        "۲. بنویس <code>انتقال 5000</code> (به‌جایِ ۵۰۰۰ مبلغِ دلخواه به تومان).",
        "۳. ربات نتیجه را با موجودیِ جدیدِ دو طرف نشان می‌دهد.",
        f"• حداقل: <b>{_t(tmin)}</b>" + (f" — حداکثر: <b>{_t(tmax)}</b>" if tmax else " — حداکثر: نامحدود"),
    ]
    if fee_on and str(fee_pct) not in ("", "0", "0.0"):
        L.append(f"• کارمزدِ انتقال: <b>{html.escape(str(fee_pct))}٪</b>")
    else:
        L.append("• کارمزد: ندارد ✅")
    if by_user:
        L.append("• انتقال با یوزرنیم هم ممکن است: یک پیام بفرست که فقط <code>@یوزرنیم</code> در آن باشد، رویِ همان ریپلای کن و بنویس «انتقال مبلغ» (آن شخص باید ربات را استارت کرده باشد).")
    L += ["• به ربات یا به خودت نمی‌شود انتقال داد؛ انتقال برگشت‌پذیر نیست، پس قبل از ارسال مطمئن شو.", ""]

    # ── بازی دونفره ──
    if (await get_setting(row.id, "group_betting_enabled", "0")) == "1":
        bmin = _to_int(await get_setting(row.id, "group_betting_min", "1000"), 1000) or 1000
        bmax = _to_int(await get_setting(row.id, "group_betting_max", "0"))
        bfee_on = (await get_setting(row.id, "group_betting_fee_enabled", "0")) == "1"
        bfee = await get_setting(row.id, "group_betting_fee_pct", "0")
        ttl = max(60, min(_to_int(await get_setting(row.id, "group_betting_ttl", "300"), 300) or 300, 86400))
        L += [
            "🎲 <b>۳) بازیِ دونفره با کیف پول (اختیاری)</b>",
            "• بنویس <code>بازی 10000</code> یا <code>شیر یا خط 10000</code>.",
            "• مبلغ تو همان لحظه از کیف پول کنار گذاشته می‌شود و یک پیام با دکمهٔ «✅ قبول و پیوستن» می‌آید.",
            "• اولین نفری که قبول کند حریفِ تو می‌شود (او هم همان مبلغ را می‌دهد). برایِ بازی با یک نفرِ مشخص، رویِ پیامش ریپلای کن و دستور را بنویس.",
            "• نتیجه تصادفی و منصفانه است؛ برنده کلِ مبلغِ دو نفر" + (f" منهای کارمزد ({html.escape(str(bfee))}٪)" if bfee_on and str(bfee) not in ("", "0") else "") + " را می‌گیرد.",
            f"• حداقل: <b>{_t(bmin)}</b>" + (f" — حداکثر: <b>{_t(bmax)}</b>" if bmax else " — حداکثر: نامحدود"),
            f"• اگر تا <b>{_fa(ttl // 60) if ttl >= 60 else _fa(ttl)} دقیقه</b> کسی قبول نکند یا خودت «❌ لغو» بزنی، مبلغ کامل برمی‌گردد.",
            "• برایِ دیدنِ همهٔ بازی‌هایِ فعال و قوانینِ هرکدام فقط بنویس <code>بازی</code>.",
            "⚠️ فقط با مبلغی بازی کن که از دست‌دادنش برایت مهم نیست؛ بازی راهِ کسب‌درآمدِ تضمینی نیست.",
            "",
        ]

    # ── بازی‌های جایزه‌دار تلگرام ──
    try:
        async with Session() as s:
            q = select(DiceGame).where(DiceGame.bot_id == row.id, DiceGame.active.is_(True), DiceGame.in_groups.is_(True))
            games = [g for g in (await s.scalars(q)).all()
                     if g.group_chat_id is None or chat_id is None or g.group_chat_id == chat_id]
    except Exception:      # noqa: BLE001
        logger.exception("group_help: خواندنِ بازی‌هایِ جایزه‌دار ناموفق بود")
        games = []
    if games:
        L += ["🎁 <b>۴) بازی‌هایِ جایزه‌دارِ روزانه (رایگان)</b>",
              "بازی‌هایِ داخلیِ تلگرام (تاس، دارت، بسکتبال، فوتبال، اسلات…) که <b>هزینه‌ای از تو نمی‌گیرند</b>؛ نتیجه را خودِ تلگرام "
              "تعیین می‌کند (نه ربات و نه مدیر)، و اگر نتیجه جایزه داشته باشد همان مبلغ مستقیم به کیف پولت اضافه می‌شود.",
              "<b>بازی‌هایِ فعالِ این ربات:</b>"]
        for g in games:
            top = _top_prize(g)
            lim = "بدونِ محدودیت" if not g.tries_limit else f"{_fa(g.tries_limit)} بار {_PERIOD_FA.get(g.tries_period, '')}"
            L.append(f"• {g.emoji} <b>{html.escape(g.title)}</b> — سهمیه: {lim}" + (f" — تا <b>{_t(top)}</b> جایزه" if top else ""))
        L += ["• برایِ بازی، فقط نامِ بازی یا ایموجی‌اش را در گروه بفرست؛ مثلاً <code>" + html.escape(games[0].title) + "</code>.",
              "• سهمیه که تمام شود، ربات می‌گوید چه زمانی دوباره می‌توانی بازی کنی؛ هر روز سر بزن تا شانسِ جایزه را از دست ندهی.",
              "ℹ️ مقدارِ جایزه‌ها را مدیرِ ربات تعیین می‌کند و ممکن است هر بار جایزه‌ای نباشد؛ این یک امتیازِ تشویقی است، نه درآمدِ تضمین‌شده.",
              ""]

    L += ["━━━━━━━━━━━━━━",
          "❓ سؤالی داشتی؟ در پیوی ربات به پشتیبانی پیام بده.",
          "این راهنما را هر وقت خواستی با نوشتنِ <code>راهنما گروه</code> دوباره ببین."]
    return "\n".join(L)


@group_help_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                           F.func(lambda m: bool(_TRIGGER_RE.match((m.text or "").strip()))))
async def group_help(msg: Message, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not _group_feature_allowed(row):
        return
    try:
        await msg.reply(await build_group_help(row, msg.chat.id), parse_mode="HTML", disable_web_page_preview=True)
    except Exception:      # noqa: BLE001
        logger.exception("group_help: ارسالِ راهنما ناموفق بود")
