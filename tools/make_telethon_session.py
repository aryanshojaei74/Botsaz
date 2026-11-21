"""
ساختِ StringSession برایِ «اکانتِ واسط» (واسطه‌گری رباتِ خدماتِ مجازی).

اجرا (روی کامپیوتر یا سرورِ خودت، نه داخلِ تلگرام):
    pip install telethon
    python3 tools/make_telethon_session.py

api_id و api_hash را از https://my.telegram.org ← API development tools بگیر.
شماره و کدی که تلگرام می‌فرستد را همین‌جا در ترمینال وارد کن (کد را در تلگرام فوروارد نکن).
خروجی یک رشتهٔ طولانی است؛ آن را در پنلِ ربات (واسطه‌گری ← اتصالِ اکانتِ واسط) بفرست و هرگز جای دیگری منتشر نکن.
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()          # شماره، کد و (در صورتِ وجود) رمزِ دومرحله‌ای را می‌پرسد
        me = await client.get_me()
        print(f"\nوارد شد: {me.first_name} (@{me.username})\n")
        print("StringSession:\n")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
