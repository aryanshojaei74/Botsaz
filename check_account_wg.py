"""
check_account_wg.py — فقط خواندنی. رکوردِ یک PanelAccount خاص رو با ایمیلش
پیدا می‌کنه و پنلِ واقعاً ثبت‌شده روش رو نشون می‌ده.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_account_wg.py nextv27527059770h4o
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import PanelAccount, VpnPanel


async def main(email_part: str):
    async with Session() as s:
        acc = await s.scalar(select(PanelAccount).where(
            PanelAccount.email.contains(email_part)))
        if not acc:
            print(f"❌ هیچ اکانتی با ایمیلِ شاملِ «{email_part}» پیدا نشد.")
            return
        print(f"✅ account_id={acc.id} bot_id={acc.bot_id} email={acc.email}")
        print(f"   panel_key ثبت‌شده رو اکانت: {acc.panel_key!r}")
        print(f"   status={acc.status} volume_gb={acc.volume_gb} days={acc.days}")

        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == acc.bot_id, VpnPanel.panel_key == acc.panel_key))
        if panel:
            print(f"   پنلِ متناظر: panel_id={panel.id} base_url={panel.base_url} "
                  f"inbound_ids={panel.inbound_ids!r} enabled={panel.enabled}")
        else:
            print("   ⚠️ هیچ پنلی با این panel_key برایِ این ربات پیدا نشد!")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "nextv27527059770h4o"
    asyncio.run(main(email))
