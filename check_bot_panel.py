"""
check_bot_panel.py — فقط خواندنی. با یوزرنیمِ ربات، bot_id و پنل(های)ِ
متصل‌شده‌اش رو نشون می‌ده.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_bot_panel.py Next_v2Bot
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Bot as BotRow, VpnPanel


async def main(username: str):
    username = username.lstrip("@")
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.username == username))
        if not row:
            print(f"❌ رباتی با یوزرنیمِ «{username}» پیدا نشد.")
            return
        print(f"✅ bot_id={row.id} status={row.status} plan={row.plan}")
        panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == row.id))).all()
        if not panels:
            print("   هیچ پنلی برایِ این ربات ثبت نشده.")
            return
        for p in panels:
            print(f"   panel_id={p.id} panel_key={p.panel_key!r} base_url={p.base_url} "
                  f"sub_base={p.sub_base} enabled={p.enabled}")


if __name__ == "__main__":
    uname = sys.argv[1] if len(sys.argv) > 1 else "Next_v2Bot"
    asyncio.run(main(uname))
