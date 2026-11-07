"""
find_order.py — فقط خواندنی. سفارش رو با آیدیِ عددیِ خریدار، یوزرنیم، یا
کدِ پیگیریِ رسید پیدا می‌کنه و وضعیتِ دقیقش رو نشون می‌ده.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 find_order.py telegram_id=8033564102
    python3 find_order.py username=Soheilrahro
    python3 find_order.py tracking=1300896331226
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Order, BotUser, Bot as BotRow


async def main(key: str, value: str):
    async with Session() as s:
        if key == "telegram_id":
            users = (await s.scalars(select(BotUser).where(
                BotUser.telegram_id == int(value)))).all()
        elif key == "username":
            users = (await s.scalars(select(BotUser).where(
                BotUser.username.ilike(f"%{value}%")))).all()
        else:
            users = []

        if key == "tracking":
            orders = (await s.scalars(select(Order).where(
                Order.receipt_text.contains(value)).order_by(Order.id.desc()).limit(10))).all()
        else:
            if not users:
                print(f"❌ کاربری با {key}={value} پیدا نشد.")
                return
            uids = [u.id for u in users]
            orders = (await s.scalars(select(Order).where(
                Order.user_id.in_(uids)).order_by(Order.id.desc()).limit(10))).all()

        if not orders:
            print("❌ هیچ سفارشی پیدا نشد.")
            return

        for o in orders:
            u = await s.get(BotUser, o.user_id)
            brow = await s.get(BotRow, o.bot_id)
            print(f"\n📦 order_id={o.id}  bot_id={o.bot_id} ({brow.username if brow else '?'})")
            print(f"   وضعیت: {o.status}  |  روش پرداخت: {o.pay_method}")
            print(f"   مبلغ: {o.amount}  |  حجم/روز: {o.volume_gb}گیگ/{o.days}روز  |  panel_key: {o.panel_key!r}")
            print(f"   خریدار: telegram_id={u.telegram_id if u else '?'} username={u.username if u else '?'}")
            print(f"   receipt_text: {(o.receipt_text or '')[:120]!r}")
            print(f"   created_at: {o.created_at}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or "=" not in sys.argv[1]:
        print("Usage: python3 find_order.py telegram_id=XXXX | username=XXXX | tracking=XXXX")
    else:
        k, v = sys.argv[1].split("=", 1)
        asyncio.run(main(k.strip(), v.strip()))
