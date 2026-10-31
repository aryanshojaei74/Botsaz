#!/usr/bin/env python3
"""
scripts/premium_emoji_probe.py — آیا تلگرام برایِ این ربات ایموجیِ پرمیوم را در این چت واقعاً نگه می‌دارد؟

یک پیامِ آزمایشی با entityِ custom_emoji به چتِ داده‌شده می‌فرستد، entityهایِ پیامِ برگشتی را می‌خواند (اگر تلگرام
ایموجی را نپذیرد، entity در پیامِ برگشتی نیست و فقط ایموجیِ معمولی دیده می‌شود)، و پیام را پاک می‌کند.

    venv/bin/python scripts/premium_emoji_probe.py --bot-username <یوزرنیم> --chat <آیدیِ عددی یا @کانال> [--chat ...]

توکن از دیتابیس خوانده می‌شود و چاپ نمی‌شود. برایِ خودِ چتِ خصوصی مالک هم یک --chat <آیدیِ عددیِ مالک> بده تا مقایسه شود.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot  # noqa: E402
from aiogram.types import MessageEntity  # noqa: E402
from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow  # noqa: E402
from premium_emoji import PREMIUM_EMOJI_IDS  # noqa: E402


async def main(username: str, chats: list[str], emoji_id: str) -> None:
    uname = username.lstrip("@")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.username == uname))
    if not row:
        print("ربات پیدا نشد:", uname)
        return
    bot = Bot(row.token)
    try:
        for chat in chats:
            target = int(chat) if chat.lstrip("-").isdigit() else chat
            try:
                m = await bot.send_message(
                    target, "✅ تست ایموجی پرمیوم",
                    entities=[MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=emoji_id)])
            except Exception as e:  # noqa: BLE001
                print(f"{chat}: ارسال ناموفق — {type(e).__name__}: {e}")
                continue
            kept = any(e.type == "custom_emoji" for e in (m.entities or []))
            print(f"{chat}: {'✅ پرمیوم نگه داشته شد' if kept else '❌ تلگرام ایموجیِ پرمیوم را حذف کرد (فقط ایموجیِ ساده)'}")
            try:
                await bot.delete_message(m.chat.id, m.message_id)
            except Exception:  # noqa: BLE001
                print(f"  (پیامِ آزمایشی در {chat} پاک نشد؛ دستی پاکش کن)")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-username", required=True)
    ap.add_argument("--chat", action="append", required=True)
    ap.add_argument("--emoji-id", default=next((v for v in PREMIUM_EMOJI_IDS.values() if v), ""))
    a = ap.parse_args()
    asyncio.run(main(a.bot_username, a.chat, a.emoji_id))
