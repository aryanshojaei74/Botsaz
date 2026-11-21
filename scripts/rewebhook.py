import asyncio, os
from dotenv import load_dotenv

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging
load_dotenv("/opt/botsaz/.env")
import sys; sys.path.insert(0, "/opt/botsaz")
from sqlalchemy import select
from config import Session, WEBHOOK_BASE
from models import Bot as BotRow
from aiogram import Bot

async def main():
    if not WEBHOOK_BASE:
        print("❌ WEBHOOK_BASE تنظیم نشده در .env"); return
    print("WEBHOOK_BASE =", WEBHOOK_BASE)
    async with Session() as s:
        bots = (await s.scalars(select(BotRow).where(BotRow.status == "active"))).all()
    print(f"تعداد ربات فعال: {len(bots)}")
    ok = fail = 0
    for b in bots:
        try:
            tb = Bot(b.token)
            await tb.set_webhook(f"{WEBHOOK_BASE}/webhook/{b.token}", drop_pending_updates=True,
                                 allowed_updates=__import__("webhook_utils").TENANT_ALLOWED_UPDATES)
            await tb.session.close()
            ok += 1
            print(f"  ✅ @{b.username or b.id}")
        except Exception as e:
            fail += 1
            print(f"  ❌ @{b.username or b.id}: {type(e).__name__}: {str(e)[:80]}")
    print(f"\nنتیجه: {ok} موفق، {fail} ناموفق")

asyncio.run(main())
