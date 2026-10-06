import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import MainButton, Setting


async def main(bot_id: int):
    async with Session() as s:
        btns = (await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id))).all()
        print(f"=== دکمه‌های bot_id={bot_id} با قانون (rules) ===")
        for b in btns:
            if b.rules:
                print(f"  id={b.id} label={b.label!r} config={b.config!r}")

        print("\n=== تنظیماتِ refpoint_* ===")
        rows = (await s.scalars(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key.like("refpoint_%")))).all()
        for r in rows:
            print(f"  {r.key} = {r.value!r}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 76))
