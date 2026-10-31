#!/usr/bin/env python3
"""scripts/hosted_ai_fix.py — PATCH_HOSTED_AI_FIX_V1

بررسی و اصلاحِ رباتِ میزبانی‌شدهٔ یک کاربر از خطِ فرمان (همان کارِ دکمه‌هایِ «🔍 بررسیِ هوشمند» و «🛠 اصلاح با هوش مصنوعی»
در پنلِ ادمین). اجرا از /opt/botsaz:

    venv/bin/python scripts/hosted_ai_fix.py 17                  # فقط گزارشِ مشکل
    venv/bin/python scripts/hosted_ai_fix.py --name pyhdbkm      # با نامِ اپ
    venv/bin/python scripts/hosted_ai_fix.py --owner 8757601463  # با تلگرام‌آیدیِ مالک
    venv/bin/python scripts/hosted_ai_fix.py 17 --fix            # طرحِ اصلاح (Grok) + diff؛ چیزی نوشته نمی‌شود
    venv/bin/python scripts/hosted_ai_fix.py 17 --fix --apply    # اعمال (پشتیبان) و ری‌استارت و بررسیِ لاگ

نیاز: VIP_ADMIN_SECRET (و در صورتِ لزوم VIP_API_BASE) و برایِ --fix: HOSTED_FIX_AI_ROUTES=xai=xai-... در .env
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_env() -> None:
    p = os.path.join(ROOT, ".env")
    try:
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


def _plain(html_text: str) -> str:
    t = re.sub(r"<[^>]+>", "", html_text)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def _resolve(a) -> int | None:
    if a.app_id:
        return a.app_id
    from sqlalchemy import select
    from config import Session
    from models import HostedApp
    async with Session() as s:
        if a.name:
            return await s.scalar(select(HostedApp.id).where(HostedApp.name == a.name))
        rows = (await s.execute(select(HostedApp.id, HostedApp.name, HostedApp.status)
                                .where(HostedApp.owner_id == a.owner).order_by(HostedApp.id))).all()
    if len(rows) == 1:
        return rows[0][0]
    for r in rows:
        print(f"  • id={r[0]}  نام={r[1]}  وضعیت={r[2]}")
    print("چند اپ دارد؛ یکی را با شناسه انتخاب کن." if rows else "اپی پیدا نشد.")
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id", nargs="?", type=int)
    ap.add_argument("--name")
    ap.add_argument("--owner", type=int)
    ap.add_argument("--fix", action="store_true", help="طرحِ اصلاح با Grok بساز و diff را نشان بده")
    ap.add_argument("--apply", action="store_true", help="با --fix: بنویس و ری‌استارت کن")
    a = ap.parse_args()
    _load_env()
    import hosted_ai_fix as H
    if not (a.app_id or a.name or a.owner):
        print("شناسهٔ اپ (یا --name / --owner) لازم است.")
        return 2
    app_id = await _resolve(a)
    if not app_id:
        return 2
    client = H.VipClient()
    rep, texts = await H.diagnose(client, app_id)
    print(_plain(rep.text()))
    if not a.fix or rep.ok:
        return 0 if rep.ok else 1
    plan = await H.plan_fix(client, app_id, report=rep, texts=texts)
    print("\n──────── طرحِ اصلاح ────────")
    for n in plan.notes:
        print(_plain(n))
    for p in plan.patches:
        print(f"\n### {p.path}  {p.explanation}\n{p.diff(80)}")
    for i in plan.remaining:
        print(f"❌ حل نشد: {i.path}:{i.line} {i.message[:120]}")
    if not plan.patches:
        return 1
    if not a.apply:
        print("\n(پیش‌نمایش؛ برایِ اعمال همین دستور + --apply)")
        return 0
    ok, msgs = await H.apply_fix(client, app_id, plan, rep.name)
    print("\n".join(msgs))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
