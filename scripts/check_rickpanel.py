#!/usr/bin/env python3
"""
scripts/check_rickpanel.py — تستِ فقط‌خواندنیِ اتصال به RickPanel (PATCH_RICKPANEL_PANEL_V1).

چیزی نمی‌سازد/تغییر نمی‌دهد. لاگین + رکوردِ ادمین (قوانینِ سرویس) + تعدادِ کاربرها (+ اختیاری: یک کاربرِ موجود).

    RICKPANEL_PASS='...' venv/bin/python scripts/check_rickpanel.py --url https://rickpanel.io --user NAME
    RICKPANEL_PASS='...' venv/bin/python scripts/check_rickpanel.py --url https://rickpanel.io --user NAME --email EXISTING_USER

⚠️ رمز را در چت نفرست؛ فقط از env بده و خروجی را (بدونِ توکن/ساب‌لینک) بفرست.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tenant.shared.panel_clients.rickpanel_panel import RickPanelClient  # noqa: E402
from tenant.shared.panel_clients.vpn_panel import PanelError  # noqa: E402


async def main(url: str, user: str, email: str | None) -> int:
    pw = os.environ.get("RICKPANEL_PASS", "")
    if not pw:
        print("RICKPANEL_PASS را در env بده.")
        return 2
    c = RickPanelClient(url.rstrip("/"), user, pw)
    try:
        res = await c.test_connection()
        print("✅ لاگین و رکوردِ ادمین OK")
        print("قوانینِ سرویس:", json.dumps(res.get("service"), ensure_ascii=False))
        users = await c.list_clients()
        print(f"تعدادِ کاربرهایِ این ادمین: {len(users)}")
        if email:
            cur = await c.get_usage(email)
            print("مصرفِ کاربر:", json.dumps(cur, ensure_ascii=False) if cur else "پیدا نشد")
        return 0
    except PanelError as e:
        print("❌", e)
        return 1
    finally:
        await c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--email")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, a.user, a.email)))
