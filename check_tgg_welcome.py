"""
check_tgg_welcome.py — فقط خواندنی. تنظیماتِ محافظِ گروه/خوش‌آمدِ یک
ربات رو نشون می‌ده تا ببینیم چرا پیامِ خوش‌آمد فرستاده نمی‌شه.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_tgg_welcome.py <bot_id>
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Setting


_KEYS = [
    "group_guard_enabled", "group_guard_chat_id", "group_guard_welcome_enabled",
    "group_guard_welcome_text", "group_guard_captcha_enabled",
]


async def main(bot_id: int):
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key.in_(_KEYS)))).all()
    values = {r.key: r.value for r in rows}
    print(f"bot_id={bot_id}")
    for k in _KEYS:
        print(f"  {k} = {values.get(k, '(ست نشده)')!r}")

    enabled = values.get("group_guard_enabled") == "1"
    chat_id = (values.get("group_guard_chat_id") or "").strip()
    welcome_on = values.get("group_guard_welcome_enabled") == "1"

    print()
    if not enabled:
        print("❌ مشکل پیدا شد: «فعال‌سازیِ محافظِ گروه» خاموشه — برایِ همین خوش‌آمد "
              "(و بقیه‌ی قابلیت‌هایِ محافظِ گروه) اصلاً کار نمی‌کنه، مهم نیست خوش‌آمد "
              "خودش روشن باشه یا نه.")
    elif not chat_id:
        print("❌ مشکل پیدا شد: هیچ گروهی وصل نشده (group_guard_chat_id خالیه).")
    elif not welcome_on:
        print("❌ مشکل پیدا شد: خودِ «پیام خوش‌آمد» خاموشه.")
    else:
        print("✅ از نظرِ تنظیمات همه‌چی درسته — اگه بازم نمیاد، احتمالاً ربات "
              "تو گروه ادمین نیست یا Privacy Mode خاموش نشده (BotFather → "
              "Bot Settings → Group Privacy → Turn off).")


if __name__ == "__main__":
    bid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not bid:
        print("Usage: python3 check_tgg_welcome.py <bot_id>")
    else:
        asyncio.run(main(bid))
