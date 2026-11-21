"""apay_log.py — PATCH_APAY_LOG_CHANNEL_V1

لاگِ اختصاصیِ پرداخت‌هایِ درگاهِ آپی در کانالی که مالکِ ربات انتخاب می‌کند: فاکتور ساخته شد (در حال انجام)،
پرداخت شد، لغو شد، منقضی شد. هر رویداد را مالک جدا روشن/خاموش می‌کند. خطایِ ارسال هرگز جلوی خودِ پرداخت را نمی‌گیرد.

کلیدهایِ تنظیمات: apay_log_channel (آیدیِ عددیِ کانال)، apay_log_ev_<created|paid|cancelled|expired> ("1"/"0").
اگر کانال تنظیم نباشد: فقط «پرداخت شد» و فقط وقتی کانالِ لاگِ تراکنش‌هایِ کلی هم نیست، به پیویِ مالک می‌رود
(تا مالک از هر شارژِ آپی باخبر شود و پیامِ تکراری هم نگیرد).
"""
from __future__ import annotations

import html as _html
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

EVENTS = {          # کلید: (برچسب، هشتگ، ایموجی، پیش‌فرضِ روشن بودن)
    "created":   ("در حال انجام", "#آپی_در_حال_انجام", "⏳", "1"),
    "paid":      ("انجام‌شده", "#آپی_پرداخت_شد", "✅", "1"),
    "cancelled": ("لغوشده", "#آپی_لغو", "❌", "1"),
    "expired":   ("منقضی‌شده", "#آپی_منقضی", "⌛️", "0"),
}
_TEHRAN = ZoneInfo("Asia/Tehran")


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def event_enabled(bot_id: int, event: str) -> bool:
    return (await _get(bot_id, f"apay_log_ev_{event}", EVENTS[event][3])) == "1"


def _fa(n) -> str:
    try:
        return f"{int(float(n)):,}".replace(",", "٬")
    except (TypeError, ValueError):
        return str(n)


def build_text(event: str, *, user, user_tg: int, amount_toman: int, pay_rial: int = 0, fee_toman: int = 0,
               order_id: str = "", balance=None, note: str = "", late: bool = False,
               when: datetime | None = None) -> str:
    label, tag, emoji, _d = EVENTS[event]
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    full = getattr(user, "full_name", None) or "—"
    now = (when or datetime.now(_TEHRAN)).strftime("%Y-%m-%d %H:%M")
    lines = [f"{tag} {emoji}", f"<b>پرداختِ آپی — {label}</b>", "",
             f"👤 کاربر: {_html.escape(full)}",
             f"🆔 آیدی عددی: <code>{user_tg}</code>",
             f"🔗 یوزرنیم: {_html.escape(uname)}",
             f"💰 مبلغِ شارژ: {_fa(amount_toman)} تومان"]
    if fee_toman:
        lines.append(f"💸 کارمزد (با خریدار): {_fa(fee_toman)} تومان")
    if pay_rial:
        lines.append(f"🔢 مبلغِ دقیقِ واریز: {_fa(int(pay_rial) // 10)} تومان")
    if event == "paid" and balance is not None:
        lines.append(f"📈 موجودیِ جدید: {_fa(balance)} تومان")
    if late and event == "paid":
        lines.append("⏰ پیامکِ دیرهنگام (بعد از انقضایِ فاکتور تأیید شد)")
    if order_id:
        lines.append(f"🧾 شناسهٔ فاکتور: <code>{_html.escape(order_id)}</code>")
    if note:
        lines.append(f"📝 {_html.escape(note)}")
    lines.append(f"🕒 {now} (تهران)")
    return "\n".join(lines)


async def log_event(bot_id: int, event: str, *, user_tg: int, amount_toman: int, pay_rial: int = 0,
                    fee_toman: int = 0, order_id: str = "", balance=None, note: str = "",
                    late: bool = False) -> bool:
    """True اگر پیامی ارسال شد. هیچ‌وقت استثنا نمی‌دهد."""
    try:
        if event not in EVENTS or not await event_enabled(bot_id, event):
            return False
        ch = ((await _get(bot_id, "apay_log_channel", "")) or "").strip()
        dest, to_owner = None, False
        if ch:
            dest = int(ch) if ch.lstrip("-").isdigit() else ch
        elif event == "paid" and not ((await _get(bot_id, "tx_log_channel", "")) or "").strip():
            to_owner = True
        else:
            return False
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
            user = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == user_tg))
        if not brow:
            return False
        if to_owner:
            dest = brow.owner_id
            if not dest:
                return False
        text = build_text(event, user=user, user_tg=user_tg, amount_toman=amount_toman, pay_rial=pay_rial,
                          fee_toman=fee_toman, order_id=order_id, balance=balance, note=note, late=late)
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await tbot.send_message(dest, text)
        finally:
            await tbot.session.close()
        return True
    except Exception as e:
        logger.warning("apay_log: ارسالِ لاگ ناموفق بود (bot=%s event=%s): %s", bot_id, event, e)
        return False
