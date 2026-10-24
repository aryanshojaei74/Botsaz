"""
check_account_live.py — فقط خواندنی. یک سرویسِ VPN را هم در دیتابیسِ ربات و هم *زنده روی خودِ پنل* چک می‌کند:
کاربر هنوز روی پنل هست؟ وضعیت/انقضا/مصرف؟ ساب‌لینکِ فعلیِ پنل با ساب‌لینکِ ذخیره‌شده یکی است؟ ساب باز می‌شود؟
چند کانفیگِ تکی دارد؟  (هیچ چیزی نمی‌نویسد/نمی‌سازد/حذف نمی‌کند.)

اجرا:
    cd /opt/botsaz
    source venv/bin/activate
    python3 check_account_live.py BotAtefe6677ch7
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import PanelAccount, VpnPanel


def _mask(u: str) -> str:
    u = u or ""
    return (u[:48] + "…" + u[-8:]) if len(u) > 60 else u


async def _status(url: str) -> str:
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as cs:
            async with cs.get(url, headers={"User-Agent": "v2rayN/6.45"}, ssl=False) as r:
                body = (await r.text())[:120].replace("\n", " ")
                return f"HTTP {r.status} | {body}"
    except Exception as e:  # noqa: BLE001
        return f"خطا: {type(e).__name__}: {e}"


async def main(email_part: str):
    async with Session() as s:
        acc = await s.scalar(select(PanelAccount).where(
            (PanelAccount.email == email_part) | (PanelAccount.plan_title == email_part)
        ).order_by(PanelAccount.id.desc()))
        if not acc:
            acc = await s.scalar(select(PanelAccount).where(
                PanelAccount.email.contains(email_part)).order_by(PanelAccount.id.desc()))
        if not acc:
            print(f"❌ سرویسی با «{email_part}» پیدا نشد.")
            return
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == acc.bot_id, VpnPanel.panel_key == acc.panel_key))
    print(f"✅ در دیتابیسِ ربات: id={acc.id} bot={acc.bot_id} email={acc.email} panel_key={acc.panel_key} "
          f"status={acc.status} حجم={acc.volume_gb} روز={acc.days}")
    print(f"   ساب‌لینکِ ذخیره‌شده: {_mask(acc.sub_link)}")
    if not panel:
        print("⚠️ پنلی با این panel_key برایِ ربات پیدا نشد.")
        return
    print(f"   پنل: id={panel.id} {panel.base_url} فعال={panel.enabled}")
    from tenant.bots.vpn.vpn_user import _client_from_panel
    client = _client_from_panel(panel)
    try:
        cur = await client.get_client(acc.email)
    except Exception as e:  # noqa: BLE001
        print(f"❌ خواندنِ کاربر از پنل شکست خورد: {type(e).__name__}: {e}")
        return
    if not cur:
        print("❌ این کاربر روی پنل *وجود ندارد* (۴۰۴) — احتمالاً از خودِ پنل حذف شده، ولی ربات هنوز آن را «فعال» می‌داند.")
        return
    live_sub = cur.get("sub_link") or client._abs_sub_link(cur.get("subscription_url", ""))
    print("✅ کاربر روی پنل هست:")
    print(f"   status={cur.get('status')} expire={cur.get('expire')} used={cur.get('used_traffic')} "
          f"limit={cur.get('data_limit')}")
    print(f"   ساب‌لینکِ فعلیِ پنل: {_mask(live_sub)}")
    print("   ساب‌لینکِ پنل با ذخیره‌شده " + ("یکی است." if live_sub and live_sub == acc.sub_link
                                           else "*فرق دارد* (لینک عوض شده/ذخیره‌شده کهنه است)."))
    if live_sub:
        print(f"   باز کردنِ ساب‌لینکِ فعلی: {await _status(live_sub)}")
    if acc.sub_link and acc.sub_link != live_sub:
        print(f"   باز کردنِ ساب‌لینکِ ذخیره‌شده: {await _status(acc.sub_link)}")
    try:
        links = await client.get_client_links(acc.email)
        print(f"   کانفیگِ تکی: {len(links)} عدد")
    except Exception as e:  # noqa: BLE001
        print(f"   کانفیگِ تکی: خطا {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
