"""bridge_alerts.py — PATCH_BRIDGE_ALERT_QUIET_V1

هشدارِ «اتصال به روبیکا/سروش پلاس/پنلِ سروش برقرار نیست» قبلاً با اولین خطا (مثلاً یک ۵۰۲ یا reset گذرا از سمتِ
خودِ آن سرویس‌ها) و هر ساعت می‌آمد. حالا فقط وقتی هشدار می‌دهیم که قطعی *پیوسته* باشد (پیش‌فرض ۴۵ دقیقه بدونِ حتی
یک پاسخِ موفق) و بعد از هر هشدار تا ۱۲ ساعت دوباره نمی‌آید. با اولین پاسخِ موفق شمارنده صفر می‌شود.
"""
from __future__ import annotations

import re
import time

OUTAGE_SECS = 45 * 60
COOLDOWN_SECS = 12 * 3600


class OutageGate:
    def __init__(self, outage: float = OUTAGE_SECS, cooldown: float = COOLDOWN_SECS):
        self.outage, self.cooldown = outage, cooldown
        self.since: float | None = None
        self.last_alert = float("-inf")

    def ok(self) -> None:
        self.since = None

    def failed(self, now: float | None = None) -> bool:
        """True ⇒ همین حالا باید هشدار داد."""
        now = time.monotonic() if now is None else now
        if self.since is None:
            self.since = now
        if now - self.since < self.outage or now - self.last_alert < self.cooldown:
            return False
        self.last_alert = now
        return True


def short_reason(err: str, limit: int = 160) -> str:
    """متنِ خطا را کوتاه و بدونِ HTML می‌کند (صفحهٔ ۵۰۲ی که nginx برمی‌گرداند در پیام نیاید)."""
    e = str(err or "")
    m = re.match(r"\s*(HTTP\s+\d{3})", e)
    if m and re.search(r"<\s*(!doctype|html)", e, re.I):
        return m.group(1) + " (خطای سرورِ آن سرویس)"
    e = re.sub(r"<[^>]+>", " ", e)
    return re.sub(r"\s+", " ", e).strip()[:limit]
