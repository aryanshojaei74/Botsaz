"""ابزارهای کوچکِ امنیتیِ سرویس: محدودکنندهٔ نرخ، پاکسازیِ نشست‌های اتصال، پیام خطای امن."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """پنجرهٔ لغزان در حافظه: allow(key) حداکثر `limit` بار در `window` ثانیه."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def gc(self) -> None:
        now = self.clock()
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
            self._hits.pop(k, None)


SEND_CODE_LIMIT = RateLimiter(limit=5, window=600)      # هر توکن: ۵ درخواست کد در ۱۰ دقیقه
FINISH_LIMIT = RateLimiter(limit=8, window=600)         # هر توکن: ۸ تلاش ورود در ۱۰ دقیقه
MAX_PENDING_SESSIONS = 20                                # کل کلاینت‌های نیمه‌بازِ هم‌زمان
SESSION_TTL = 600


def purge_sessions(sessions: dict, now: float | None = None) -> list:
    """نشست‌های قدیمیِ اتصال را حذف می‌کند و کلاینت‌هایشان را برای disconnect برمی‌گرداند."""
    now = time.time() if now is None else now
    stale = [t for t, s in sessions.items() if now - s.get("created", now) > SESSION_TTL]
    return [sessions.pop(t) for t in stale]


_ERRORS = {
    "PhoneNumberInvalid": "شمارهٔ تلفن نامعتبر است",
    "PhoneNumberBanned": "این شماره توسط تلگرام مسدود شده است",
    "PhoneCodeInvalid": "کد واردشده اشتباه است",
    "PhoneCodeExpired": "کد منقضی شده؛ دوباره کد بگیرید",
    "PasswordHashInvalid": "رمز دومرحله‌ای اشتباه است",
    "FloodWait": "تلگرام موقتاً محدود کرده؛ چند دقیقه بعد دوباره تلاش کنید",
    "SessionPasswordNeeded": "رمز دومرحله‌ای لازم است",
}


def connect_error(exc: BaseException) -> str:
    name = type(exc).__name__
    for key, msg in _ERRORS.items():
        if key in name:
            return msg
    return "اتصال ناموفق بود؛ دوباره تلاش کنید"
