"""
check_webhook.py — فقط خواندنی. وضعیتِ webhook رباتِ taziyan (bot_id=313)
رو مستقیم از خودِ تلگرام می‌پرسه، بدونِ اینکه توکن رو جایی چاپ کنه.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_webhook.py
"""
import asyncio
import aiohttp

from config import Session
from models import Bot as BotRow

BOT_ID = 313


async def main():
    async with Session() as s:
        row = await s.get(BotRow, BOT_ID)
        if not row or not row.token:
            print("❌ ربات یا توکنش پیدا نشد.")
            return
        token = row.token

    async with aiohttp.ClientSession() as http:
        async with http.get(f"https://api.telegram.org/bot{token}/getWebhookInfo") as resp:
            data = await resp.json()

    info = data.get("result", {})
    print("url:", info.get("url"))
    print("pending_update_count:", info.get("pending_update_count"))
    print("last_error_date:", info.get("last_error_date"))
    print("last_error_message:", info.get("last_error_message"))
    print("last_synchronization_error_date:", info.get("last_synchronization_error_date"))
    print("max_connections:", info.get("max_connections"))
    print("ip_address:", info.get("ip_address"))


if __name__ == "__main__":
    asyncio.run(main())
