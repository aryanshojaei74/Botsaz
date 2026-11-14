"""منطقِ «پاسخ عمومی» (پیام خودکار در پی‌وی) — جدا از pyrogram تا تست‌پذیر باشد.

باگ‌های نسخهٔ قبلی:
  • نوع چت با رشتهٔ 'private' مقایسه می‌شد؛ در pyrogram 2 نوع یک enum است و مقایسه همیشه False بود
    ⇒ پاسخ عمومی هرگز ارسال نمی‌شد.
  • اولین دور بررسی، به آخرین پیامِ هر گفتگو (تا ۱۰۰ گفتگو) جواب می‌داد ⇒ با درست شدنِ مقایسه،
    هر ری‌استارت به گفتگوهای قدیمی پیام می‌فرستاد.
  • هم هندلر زنده و هم پولینگ جواب می‌دادند ⇒ دوبار جواب. اینجا یک «دروازه» مشترک دارند.
"""
from __future__ import annotations

import time


class ReplyGate:
    """هر پیام حداکثر یک‌بار جواب می‌گیرد و برای هر گفتگو فاصلهٔ حداقلی بین دو پاسخ هست."""

    def __init__(self, cooldown: float = 15.0, clock=time.time):
        self.cooldown = cooldown
        self.clock = clock
        self.done: dict[int, int] = {}
        self.last: dict[int, float] = {}

    def allow(self, chat_id: int, msg_id: int) -> bool:
        if msg_id <= self.done.get(chat_id, 0):
            return False
        self.done[chat_id] = msg_id                      # چه جواب بدهیم چه نه، این پیام «دیده» شد
        now = self.clock()
        if now - self.last.get(chat_id, -1e12) < self.cooldown:
            return False
        self.last[chat_id] = now
        return True


def decide(prev_seen: int | None, msg_id: int, age_sec: float | None, from_me: bool,
           from_bot: bool, has_text: bool, fresh: float = 90.0) -> tuple[bool, int]:
    """آیا به آخرین پیامِ این گفتگو جواب بدهیم؟ برمی‌گرداند (جواب؟، آخرین id دیده‌شده).

    اولین مشاهدهٔ یک گفتگو فقط وقتی جواب می‌دهد که پیام تازه باشد (≤۹۰ ثانیه)؛
    یعنی بعد از ری‌استارت یا روشن‌کردنِ قابلیت، به پیام‌های قدیمی جواب داده نمی‌شود.
    """
    new_seen = max(prev_seen or 0, msg_id)
    if prev_seen is None:
        if age_sec is None or age_sec > fresh:
            return False, new_seen
    elif msg_id <= prev_seen:
        return False, new_seen
    if from_me or from_bot or not has_text:
        return False, new_seen
    return True, new_seen
