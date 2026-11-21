import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import MainButton


async def main(bot_id: int):
    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id))).all()
    print(f"=== همه‌ی دکمه‌های bot_id={bot_id} با config غیرِخالی ===")
    for b in btns:
        if b.config and ("gate" in b.config or "tier" in b.config or "src" in b.config or "keep_action" in b.config):
            print(f"  id={b.id} label={b.label!r} action={b.action!r}")
            print(f"     config={b.config}")
            print(f"     rules={b.rules!r}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 76))
