#!/usr/bin/env python3
"""
scripts/diag_trial_button.py — تشخیصِ فقط‌خواندنیِ «دکمهٔ تست رایگان» یک ربات (تیکت #849).

نشان می‌دهد برایِ هر دکمهٔ «تست رایگان»:
  • نوعِ تحویل، پنل(های) انتخاب‌شده، و مقدارهایی که *روی خودِ دکمه* ذخیره شده (اینها تنظیماتِ کلی را می‌پوشانند)،
  • مقدارِ مؤثر بعد از پُرشدن از «تنظیماتِ تستِ رایگان»،
  • پنل‌هایِ فعالِ ربات و اینکه تست‌هایِ ۱۴ روزِ اخیر از کدام پنل ساخته شده،
  • و یک جمع‌بندیِ ساده (چرا دو تنظیم فرق می‌کنند / چرا از چند پنل می‌گیرد).
چیزی را تغییر نمی‌دهد. اجرا از /opt/botsaz:

    venv/bin/python scripts/diag_trial_button.py --owner 344284721
    venv/bin/python scripts/diag_trial_button.py --bot-username ManiBot
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from collections import Counter
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def conclusions(cfg: dict, global_gb: str, global_days: str, panels: list[str], used: Counter) -> list[str]:
    """جمع‌بندیِ خواناییِ یک دکمه؛ تابعِ خالص تا تست‌پذیر باشد."""
    out: list[str] = []
    mode = cfg.get("trial_mode")
    if not mode:
        out.append("⚠️ این دکمه هنوز «نوعِ تحویل» ندارد؛ ربات مسیرِ قدیمیِ سراسری را می‌رود (حجم/مدت از «تنظیماتِ تستِ رایگان» و "
                   "پنل = اولین پنلِ فعال، یعنی انتخابِ پنل در دکمه اثری ندارد).")
        return out
    own_gb, own_days = cfg.get("trial_gb"), cfg.get("trial_days")
    if own_gb is not None and str(own_gb) != str(global_gb):
        out.append(f"⚠️ حجمِ اختصاصیِ دکمه ({own_gb} گیگ) با تنظیماتِ کلی ({global_gb} گیگ) فرق دارد و مقدارِ دکمه اولویت دارد. "
                   "راه‌حل: در تنظیماتِ همین دکمه «♻️ حجم و مدت از تنظیماتِ تستِ رایگان» را بزن.")
    if own_days is not None and str(own_days) != str(global_days):
        out.append(f"⚠️ مدتِ اختصاصیِ دکمه ({own_days} روز) با تنظیماتِ کلی ({global_days} روز) فرق دارد (مقدارِ دکمه اولویت دارد).")
    if mode == "auto":
        keys = [k for k in (cfg.get("trial_panel_keys") or "").split(",") if k] or \
               ([cfg["trial_panel_key"]] if cfg.get("trial_panel_key") else [])
        if not keys:
            out.append("⚠️ هیچ پنلی روی دکمه انتخاب نشده؛ تست از «اولین پنلِ فعال» ساخته می‌شود.")
        elif any(k not in panels for k in keys):
            stale = [k for k in keys if k not in panels]
            out.append(f"❌ کلیدِ پنلِ حذف/غیرفعال‌شده روی دکمه مانده ({', '.join(stale)}) و در صفحهٔ انتخاب دیده نمی‌شود؛ "
                       "نسخه‌هایِ قدیمی هر چند تست یک‌بار به «اولین پنلِ فعال» می‌افتادند (یعنی از پنلِ دیگر هم ساخته می‌شد). "
                       "با نسخهٔ جدید خودکار پاک می‌شود.")
        elif len(keys) > 1:
            out.append(f"ℹ️ {len(keys)} پنل تیک خورده ({', '.join(keys)}) ⇒ هر تست رندوم از یکی از آن‌ها ساخته می‌شود. "
                       "برایِ فقط یک پنل: صفحهٔ انتخابِ پنل را باز کن و فقط همان یکی را ✅ بگذار، سپس «✅ تأیید و ذخیره».")
        else:
            missing = [k for k in keys if k not in panels]
            if missing:
                out.append(f"⚠️ پنلِ انتخاب‌شده ({missing[0]}) در پنل‌هایِ فعالِ ربات نیست؛ تست از اولین پنلِ فعال ساخته می‌شود.")
            else:
                out.append(f"✅ فقط پنلِ {keys[0]} انتخاب شده.")
        other = [k for k in used if keys and k not in keys]
        if other:
            out.append(f"⚠️ در ۱۴ روزِ اخیر تست‌هایی از پنلِ دیگر هم ساخته شده: {', '.join(f'{k}×{used[k]}' for k in other)} "
                       "(ممکن است قبل از انتخابِ پنل بوده یا از مسیرِ دیگری مثل تستِ سراسری).")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--owner", type=int, help="تلگرام‌آیدیِ مالکِ ربات")
    g.add_argument("--bot-username")
    g.add_argument("--bot-id", type=int)
    a = ap.parse_args()
    from sqlalchemy import func, select
    from config import Session
    from models import Bot, MainButton, PanelAccount, VpnPanel
    from tenant import _trial_cfg_of, get_setting
    async with Session() as s:
        q = select(Bot).where(Bot.archived_at.is_(None))
        if a.owner:
            q = q.where(Bot.owner_id == a.owner)
        elif a.bot_username:
            q = q.where(func.lower(Bot.username) == a.bot_username.lstrip("@").lower())
        else:
            q = q.where(Bot.id == a.bot_id)
        bots = (await s.scalars(q)).all()
    if not bots:
        print("رباتی پیدا نشد.")
        return 1
    for b in bots:
        print(f"\n══ ربات #{b.id} @{b.username} پلن={b.plan} نوع={b.field} وضعیت={b.status}")
        async with Session() as s:
            panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == b.id))).all()
            btns = (await s.scalars(select(MainButton).where(
                MainButton.bot_id == b.id, MainButton.action == "free_trial"))).all()
            since = dt.datetime.utcnow() - dt.timedelta(days=14)
            rows = (await s.execute(select(PanelAccount.panel_key, func.count()).where(
                PanelAccount.bot_id == b.id, PanelAccount.plan_title == "تست رایگان",
                PanelAccount.created_at >= since).group_by(PanelAccount.panel_key))).all() \
                if hasattr(PanelAccount, "created_at") else []
        used = Counter({k or "—": n for k, n in rows})
        print("پنل‌ها:")
        for p in panels:
            host = urlparse(p.base_url).hostname if getattr(p, "base_url", None) else "—"
            print(f"   #{p.id} کلید={p.panel_key} {'فعال' if p.enabled else 'غیرفعال'} میزبان={host}")
        g_gb = str(await get_setting(b.id, "trial_gb", "2"))
        g_days = str(await get_setting(b.id, "trial_days", "3"))
        g_hours = str(await get_setting(b.id, "trial_hours", "0"))
        g_src = str(await get_setting(b.id, "trial_source", "panel"))
        print(f"«تنظیماتِ تستِ رایگان» (کلی): حجم={g_gb} گیگ، مدت={g_days} روز + {g_hours} ساعت، منبع={g_src}")
        print(f"تست‌هایِ ۱۴ روزِ اخیر به تفکیکِ پنل: {dict(used) or 'هیچ'}")
        if not btns:
            print("⚠️ هیچ دکمه‌ای با عملکردِ «تست رایگان» در منویِ اصلی نیست (تست از مسیرِ سراسری/دستور است).")
        enabled_keys = [p.panel_key for p in panels if p.enabled]
        for mb in btns:
            cfg = _trial_cfg_of(mb)
            show = {k: v for k, v in cfg.items() if k.startswith("trial_")}
            print(f"\n   🔘 دکمه #{mb.id} «{mb.label}» — تنظیماتِ ذخیره‌شده روی دکمه: {show or '— هیچ —'}")
            for line in conclusions(cfg, g_gb, g_days, enabled_keys, used):
                print("      " + line)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
