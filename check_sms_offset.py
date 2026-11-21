import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Setting


async def main(fix: bool):
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(
            Setting.bot_id == 0, Setting.key.like("platform_sms%")))).all()
        print("=== تنظیمات فعلی ===")
        for r in rows:
            print(f"  {r.key} = {r.value!r}")

        if fix:
            for key, val in [("platform_sms_offset_min", "100"), ("platform_sms_offset_max", "4999")]:
                row = next((r for r in rows if r.key == key), None)
                if row:
                    row.value = val
                else:
                    s.add(Setting(bot_id=0, key=key, value=val))
            await s.commit()
            print("\n✅ بازه به مقدارِ پیش‌فرض (۱۰۰ تا ۴۹۹۹) برگشت.")


if __name__ == "__main__":
    asyncio.run(main(fix="--fix" in sys.argv))
