"""
trial_report.py — PATCH_TRIAL_REPORT_CHANNEL_V1

«گزارش اکانت تست»: هر بار کاربری تستِ رایگان بگیرد، مشخصاتِ آن (آیدی/یوزرنیم کاربر،
لینک، حجم، مدت، پنل) در کانال/گروهی که مالکِ ربات وصل کرده پست می‌شود.

کلیدهایِ تنظیمات (به‌ازایِ هر ربات):
  trial_report_channel   آیدیِ عددیِ کانال/گروه
  trial_report_link      "1" (پیش‌فرض) = لینکِ سرویس هم در پست باشد، "0" = بدونِ لینک
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timedelta

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_MAX_LINK = 900


def _tehran_now() -> str:
    t = datetime.utcnow() + timedelta(hours=3, minutes=30)
    try:
        import jdatetime as _jdt
        d = _jdt.datetime.fromgregorian(datetime=t).strftime("%Y/%m/%d")
    except Exception:
        d = t.strftime("%Y/%m/%d")
    return f"{d} {t.strftime('%H:%M')}"


def build_report(*, user_id, username, full_name, link, volume, duration,
                 panel, source, show_link=True) -> str:
    """متنِ HTMLِ گزارش (تابعِ خالص؛ قابلِ تست)."""
    lines = ["#تست_جدید", ""]
    if show_link and link:
        lk = str(link).strip()
        if len(lk) > _MAX_LINK:
            lk = lk[:_MAX_LINK] + "…"
        lines += ["🔗 لینک:", f"<code>{_html.escape(lk)}</code>", ""]
    lines.append(f"⏱ {_html.escape(str(duration))} | 📦 {_html.escape(str(volume))}")
    if panel:
        lines.append(f"🚀 پنل: {_html.escape(str(panel))}")
    elif source == "stock":
        lines.append("📦 منبع: انبار دستی")
    lines.append(f"🆔 id: <code>{user_id}</code>")
    uname = f"@{username}" if username else "—"
    lines.append(f"👤 {_html.escape(full_name or '—')} | {_html.escape(uname)}")
    lines.append(f"🕒 {_tehran_now()}")
    return "\n".join(lines)


async def report(bot, bot_id: int, u, *, tg_username=None, link="", volume="", duration="",
                 panel_key: str | None = None, panel_name: str | None = None,
                 source: str = "panel") -> None:
    """پستِ گزارش؛ هر خطایی بی‌صدا لاگ می‌شود تا هرگز جلوی تحویلِ تست را نگیرد."""
    try:
        from tenant import get_setting
        ch = (await get_setting(bot_id, "trial_report_channel", "") or "").strip()
        if not ch:
            return
        show_link = (await get_setting(bot_id, "trial_report_link", "1")) != "0"
        name = panel_name
        if not name and panel_key:
            try:
                from tenant.bots.vpn.vpn_admin import panel_label
                name = await panel_label(bot_id, panel_key)
            except Exception:
                logger.exception("trial_report: نامِ پنل خوانده نشد")
                name = panel_key
        text = build_report(
            user_id=getattr(u, "telegram_id", None), username=tg_username or getattr(u, "username", None),
            full_name=getattr(u, "full_name", None), link=link, volume=volume,
            duration=duration, panel=name, source=source, show_link=show_link)
        try:
            chat_id = int(ch)
        except ValueError:
            chat_id = ch
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.warning("trial_report: ارسالِ گزارشِ تست ناموفق بود (bot_id=%s): %s", bot_id, e)
