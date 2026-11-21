#!/usr/bin/env python3
"""
scripts/diag_panels.py — «پنل‌هایِ وصل‌شدهٔ یک ربات» (فقط خواندن؛ رمز/توکن/کلید چاپ نمی‌شود) — PATCH_PSEL_DIAG_V1

    cd /opt/botsaz && python3 scripts/diag_panels.py whale          # هر رباتی که یوزرنیمش «whale» دارد

برایِ هر ربات: هر پنل با نوع، میزبان (فقط دامنه)، و اینکه «قابلِ‌فروش» حساب می‌شود یا نه. «انتخابِ پنل» در مدیریتِ پنل‌فروشی
فقط وقتی می‌آید که حداقل دو پنلِ قابلِ‌فروش وصل باشد.
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, "/opt/botsaz"):
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)


def host_of(url: str | None) -> str:
    try:
        return urlparse(url or "").hostname or (url or "")[:40]
    except ValueError:
        return "?"


async def main(argv: list[str]) -> int:
    from sqlalchemy import select
    from config import Session
    from models import Bot, Setting, VpnPanel
    from tenant.bots.panel_seller import adapter as AD

    names = [a.lower() for a in argv if not a.startswith("--")]
    if not names:
        print("usage: diag_panels.py <part-of-bot-username>")
        return 1
    async with Session() as s:
        bots = [b for b in (await s.scalars(select(Bot))).all() if any(n in (b.username or "").lower() for n in names)]
        for b in bots:
            panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == b.id).order_by(VpnPanel.id))).all()
            sup = [p for p in panels if AD.supports(p.panel_key)]
            print(f"\n@{b.username}  field={b.field}  plan={b.plan}  status={b.status}")
            print(f"  پنل‌هایِ متصل: {len(panels)} | قابلِ‌فروش: {len(sup)} | "
                  + ("انتخابِ پنل فعال است" if (b.field == 'panel_seller' and len(sup) >= 2)
                     else "انتخابِ پنل نمی‌آید (حداقل ۲ پنلِ قابلِ‌فروش لازم است)" if b.field == "panel_seller" else "این ربات پنل‌فروشی نیست"))
            for p in panels:
                print(f"   - id={p.id:<5} key={p.panel_key:<18} host={host_of(p.base_url):<32} قابلِ‌فروش={'بله' if AD.supports(p.panel_key) else 'خیر'}")
            scopes = (await s.scalars(select(Setting).where(Setting.bot_id == b.id, Setting.key.like("psel_scope_%")))).all()
            for st in scopes:
                print(f"   scope {st.key} = {st.value!r}")
    if not bots:
        print("هیچ رباتی با این نام پیدا نشد")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
