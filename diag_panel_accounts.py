"""diag_panel_accounts.py — اسکریپتِ عیب‌یابیِ اکانت‌های پنل (upanel_mizban / pasargad / pasarguard و...).

اجرا از ریشهٔ پروژه (/opt/botsaz):

    python3 diag_panel_accounts.py                      # نمونهٔ ۲۰ تایی از همهٔ پنل‌های مشکل‌دار
    python3 diag_panel_accounts.py --panel-key upanel_mizban
    python3 diag_panel_accounts.py --panel-key pasarguard --limit 50
    python3 diag_panel_accounts.py --bot-id 899
    python3 diag_panel_accounts.py --email Botnitroteeamvbx
    python3 diag_panel_accounts.py --panel-key upanel_mizban --limit 300 --only-fail

برای هر PanelAccount این کارها را می‌کند:
  ۱) پنلِ مربوطه را (طبق panel_key خودِ اکانت) پیدا می‌کند.
  ۲) کلاینت واقعی پنل را می‌سازد (همان کدِ خودِ بات‌ساز — vpn_panel_factory).
  ۳) get_client(email) را صدا می‌زند و نتیجه/خطا/زمانِ پاسخ را چاپ می‌کند.
  ۴) اگر get_client موفق بود ولی links نداشت، get_client_links را هم تست می‌کند.

خروجی، سه‌دسته است:
  ✅ OK        → پنل کاربر را با موفقیت برگرداند
  ❌ NOT_FOUND → پنل ۴۰۴ داد (یوزرنیم روی خودِ پنل نیست — حذف/رنیم شده)
  ⛔ ERROR     → خطای دیگر (تایم‌اوت، ۴۰۱/۴۰۳، اتصال، ...) — متنِ کامل چاپ می‌شود

هیچ تغییری در دیتابیس یا پنل نمی‌دهد؛ فقط خواندنی (read-only) است.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

sys.path.insert(0, "/opt/botsaz")

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow, PanelAccount, VpnPanel  # noqa: E402
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client  # noqa: E402


def _base(panel_key: str | None) -> str:
    return (panel_key or "").split("#", 1)[0]


async def _load_targets(args):
    async with Session() as s:
        q = select(PanelAccount)
        if args.bot_id:
            q = q.where(PanelAccount.bot_id == args.bot_id)
        if args.email:
            q = q.where(PanelAccount.email.ilike(f"%{args.email}%"))
        rows = (await s.scalars(q)).all()

        panels = (await s.scalars(select(VpnPanel))).all()
        panel_by_key_per_bot = {}
        for p in panels:
            panel_by_key_per_bot[(p.bot_id, p.panel_key)] = p
        # اولین پنلِ فعالِ هر بات هم لازم است (سازگاری با اکانت‌های بدونِ panel_key)
        first_panel_per_bot = {}
        for p in panels:
            first_panel_per_bot.setdefault(p.bot_id, p)

        targets = []
        for a in rows:
            panel = None
            if a.panel_key:
                panel = panel_by_key_per_bot.get((a.bot_id, a.panel_key))
            if panel is None:
                panel = first_panel_per_bot.get(a.bot_id)
            if panel is None:
                continue
            if args.panel_key and _base(panel.panel_key) != args.panel_key:
                continue
            targets.append((a, panel))
        return targets


async def _check_one(account, panel):
    email = account.email
    t0 = time.monotonic()
    try:
        client = make_panel_client(panel)
    except Exception as e:
        return ("⛔ ERROR", f"ساختِ کلاینت شکست خورد: {type(e).__name__}: {e}", 0.0)

    try:
        cur = await client.get_client(email)
        dt = time.monotonic() - t0
    except Exception as e:
        dt = time.monotonic() - t0
        try:
            await client.close()
        except Exception:
            pass
        return ("⛔ ERROR", f"{type(e).__name__}: {e}", dt)

    if not cur:
        try:
            await client.close()
        except Exception:
            pass
        return ("❌ NOT_FOUND", "پنل ۴۰۴/خالی داد — این یوزرنیم روی خودِ پنل نیست", dt)

    # کاربر پیدا شد؛ اگر لینک هم داشت بگو، وگرنه بگو ولی OK بمونه
    links_note = ""
    try:
        links = cur.get("links") or []
        if not links:
            t1 = time.monotonic()
            links = await client.get_client_links(email)
            dt_links = time.monotonic() - t1
            links_note = f" | get_client_links: {len(links)} لینک ({dt_links:.1f}s)"
    except Exception as e:
        links_note = f" | get_client_links خطا داد: {type(e).__name__}: {e}"

    try:
        await client.close()
    except Exception:
        pass
    return ("✅ OK", f"data_limit={cur.get('data_limit')} status={cur.get('status')}{links_note}", dt)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel-key", default=None, help="مثلاً upanel_mizban یا pasarguard یا pasargad")
    ap.add_argument("--bot-id", type=int, default=None)
    ap.add_argument("--email", default=None, help="زیررشتهٔ ایمیل/یوزرنیم")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--only-fail", action="store_true", help="فقط NOT_FOUND/ERROR را نشان بده")
    args = ap.parse_args()

    targets = await _load_targets(args)
    print(f"تعدادِ اکانت‌های مطابق با فیلتر: {len(targets)} — {min(len(targets), args.limit)} تای اول بررسی می‌شود.\n")

    counts = {"✅ OK": 0, "❌ NOT_FOUND": 0, "⛔ ERROR": 0}
    for account, panel in targets[: args.limit]:
        status, detail, dt = await _check_one(account, panel)
        counts[status] += 1
        if args.only_fail and status == "✅ OK":
            continue
        print(f"{status} | bot_id={account.bot_id} panel={panel.panel_key} "
              f"acc_id={account.id} email={account.email!r} ({dt:.1f}s)\n    {detail}\n")

    print("──────── خلاصه ────────")
    for k, v in counts.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
