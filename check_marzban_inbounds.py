"""
check_marzban_inbounds.py — فقط خواندنی. به پنلِ مرزبانِ یک ربات لاگین
می‌کنه و لیستِ دقیقِ تگِ اینباندها رو (همون‌طور که خودِ پنل ذخیره کرده)
نشون می‌ده، تا با inbound_ids ثبت‌شده تو دیتابیسِ ما مقایسه کنیم.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_marzban_inbounds.py 76
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import VpnPanel


async def main(bot_id: int):
    async with Session() as s:
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == "marzban"))
    if not panel:
        print(f"❌ پنلِ مرزبانی برایِ bot_id={bot_id} پیدا نشد.")
        return

    print(f"inbound_ids ثبت‌شده تو دیتابیسِ ما: {panel.inbound_ids!r}")

    from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient
    client = MarzbanPanelClient(panel.base_url, panel.username, panel.password,
                                sub_base=panel.sub_base or "",
                                api_key=panel.api_key)
    try:
        await client.login()
        inbounds = await client.list_inbounds()
        print("تگ‌هایِ واقعیِ پنل (دقیقاً همون‌طور که مرزبان ذخیره کرده):")
        for ib in inbounds:
            print(f"   tag={ib['tag']!r}  protocol={ib['protocol']!r}")
    finally:
        try:
            await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    bid = int(sys.argv[1]) if len(sys.argv) > 1 else 76
    asyncio.run(main(bid))
