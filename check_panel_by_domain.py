"""
check_panel_by_domain.py — فقط خواندنی. با دامنه‌ی ساب‌لینک، پنل و نوعش رو پیدا می‌کنه.

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_panel_by_domain.py tun.v2teleziloo.ir
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import VpnPanel, PanelAccount


async def main(domain: str):
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.base_url.contains(domain)))).all()
        if not panels:
            # شاید دامنه فقط رو ساب‌بیس باشه نه base_url
            panels = (await s.scalars(select(VpnPanel).where(
                VpnPanel.sub_base.contains(domain)))).all()
        if not panels:
            print(f"❌ هیچ پنلی با دامنه‌ی «{domain}» پیدا نشد.")
            return
        for p in panels:
            print(f"✅ panel_id={p.id} bot_id={p.bot_id} panel_key={p.panel_key!r} "
                  f"base_url={p.base_url} sub_base={p.sub_base} enabled={p.enabled}")


if __name__ == "__main__":
    domain = sys.argv[1] if len(sys.argv) > 1 else "v2teleziloo.ir"
    asyncio.run(main(domain))
