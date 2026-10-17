import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Setting, Order


async def main(bot_id: int):
    async with Session() as s:
        tpl = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "trust_channel_template"))
        print("=== trust_channel_template ===")
        print(repr(tpl.value) if tpl else "(ذخیره نشده — از فرمتِ پیش‌فرض استفاده می‌شه)")

        print("\n=== ۵ سفارشِ اخیر ===")
        orders = (await s.scalars(select(Order).where(
            Order.bot_id == bot_id).order_by(Order.id.desc()).limit(5))).all()
        for o in orders:
            print(f"order_id={o.id} status={o.status} pay_method={o.pay_method} "
                  f"panel_key={o.panel_key!r} plan_id={o.plan_id} amount={o.amount} "
                  f"vol={o.volume_gb} days={o.days}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 936))
