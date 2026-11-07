"""نحوهٔ نمایشِ هویتِ خریدار در پیام‌هایِ «کانالِ لاگِ تراکنش» و «کانالِ اعتمادسازی» (پیشنهادِ #187).

یک تنظیمِ مشترکِ هر ربات: report_buyer_mode
  both     — یوزرنیم + آیدیِ عددی (رفتارِ قبلی؛ پیش‌فرض)
  id       — فقط آیدیِ عددی
  username — فقط یوزرنیم (اگر نداشت آیدیِ عددی، تا پیام بی‌هویت نماند)
"""
from __future__ import annotations

import html as _html

KEY = "report_buyer_mode"
MODES = ("both", "id", "username")
LABELS = {"both": "🔗 یوزرنیم + 🆔 آیدی عددی", "id": "🆔 فقط آیدیِ عددی", "username": "🔗 فقط یوزرنیم"}


def normalize(v: str | None) -> str:
    v = (v or "").strip().lower()
    return v if v in MODES else "both"


def next_mode(cur: str | None) -> str:
    cur = normalize(cur)
    return MODES[(MODES.index(cur) + 1) % len(MODES)]


async def get_mode(bot_id: int) -> str:
    from tenant import get_setting
    return normalize(await get_setting(bot_id, KEY, "both"))


async def cycle(bot_id: int) -> str:
    from tenant import set_setting
    new = next_mode(await get_mode(bot_id))
    await set_setting(bot_id, KEY, new)
    return new


def ident_text(mode: str, username: str | None, tg_id) -> str:
    """متنِ تک‌خطیِ هویت برایِ کانالِ اعتمادسازی."""
    mode = normalize(mode)
    uname = f"@{str(username).lstrip('@')}" if username else ""
    code = f"<code>{tg_id}</code>" if tg_id else ""
    if mode == "id":
        return code or "—"
    if mode == "username":
        return _html.escape(uname) or code or "—"
    return " — ".join(p for p in (_html.escape(uname), code) if p) or "—"


def tx_lines(mode: str, full_name: str | None, username: str | None, tg_id) -> list[str]:
    """خط‌هایِ بخشِ «کاربر» در پیامِ لاگِ تراکنش."""
    mode = normalize(mode)
    uname = f"@{username}" if username else "—"
    if mode == "id":
        return [f"🆔 آیدی عددی: <code>{tg_id}</code>"]
    if mode == "username":
        return [f"🔗 یوزرنیم: {_html.escape(uname)}"] if username else [f"🆔 آیدی عددی: <code>{tg_id}</code>"]
    return [f"👤 کاربر: {_html.escape(full_name or '—')}",
            f"🆔 آیدی عددی: <code>{tg_id}</code>",
            f"🔗 یوزرنیم: {_html.escape(uname)}"]
