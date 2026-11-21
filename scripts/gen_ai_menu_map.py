#!/usr/bin/env python3
"""
scripts/gen_ai_menu_map.py — «نقشهٔ دقیقِ پنلِ مدیریت» را از خودِ کد می‌سازد (نه از روی حافظهٔ ما) برایِ هوش مصنوعیِ گروه.

چرا: مسیرهایی که هوش مصنوعی می‌داد غلط یا ناقص بود چون دانشش دستی نوشته شده و با تغییرِ منوها عقب می‌ماند. این اسکریپت
کیبوردِ واقعیِ پنج بخشِ «پنل مدیریت» (محصولات، مالی، آمار، ادمین‌ها، تنظیمات) را برایِ هر نوعِ ربات و هر پلن می‌سازد و برچسبِ
دکمه‌ها را در platform_ai_menu_map.py ذخیره می‌کند.

    python3 scripts/gen_ai_menu_map.py            # platform_ai_menu_map.py را بازنویسی می‌کند
    python3 scripts/gen_ai_menu_map.py --check    # اگر فایل کهنه باشد با کدِ خروج ۱ تمام می‌شود (برایِ تست/CI)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "platform_ai_menu_map.py")

PLANS = ("free", "growth", "pro")
SECTIONS = ("products", "finance", "stats", "admins", "settings")
_STATE_TAIL = re.compile(r"\s*[:：]\s*[^:]*$")        # «: ✅ فعال» / «: خاموش» ... (وضعیتِ لحظه‌ای)
_SKIP = ("بازگشت", "رفرش", "بستن")


def _base(label: str) -> str:
    t = (label or "").strip()
    if ":" in t and any(w in t.split(":", 1)[1] for w in ("فعال", "غیرفعال", "روشن", "خاموش", "✅", "❌", "🟢", "🔴", "⚪", "⌨", "💠", "شیشه")):
        t = _STATE_TAIL.sub("", t).strip()
    t = re.sub(r"\s*[⚪️🔴🟢✅❌]+\s*$", "", t).strip()
    return t


async def collect() -> list[tuple[str, str, str, str]]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import config
    import models
    import tenant
    from platform_bot import FIELDS

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for mod in list(sys.modules.values()):
        if getattr(mod, "Session", None) is orig:
            mod.Session = mk
    found: dict[tuple[str, str, str], set[str]] = {}
    sec_title = dict(tenant._ADMIN_SECTIONS)
    bot_id = 0
    for field in sorted(FIELDS):
        for plan in PLANS:
            bot_id += 1
            use_plan = "partner" if field == "partner" else plan
            if field == "partner" and plan != "pro":
                continue
            async with mk() as s:
                s.add(models.Bot(id=bot_id, owner_id=1, token=f"{bot_id}:T", username=f"b{bot_id}", field=field,
                                 plan=use_plan, status="active"))
                await s.commit()
            calls = {
                "products": (tenant._sec_products_kb, (bot_id, field, use_plan)),
                "finance": (tenant._sec_finance_kb, (bot_id, use_plan)),
                "stats": (tenant._sec_stats_kb, (bot_id, use_plan)),
                "admins": (tenant._sec_admins_kb, (bot_id, use_plan)),
                "settings": (tenant._sec_settings_kb, (bot_id, use_plan, "inline", False)),
            }
            for sec, (fn, args) in calls.items():
                try:
                    kb = await fn(*args)
                except Exception as e:  # noqa: BLE001
                    print(f"[skip] {field}/{use_plan}/{sec}: {type(e).__name__}", file=sys.stderr)
                    continue
                rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", None) or []
                for r in rows:
                    for b in r:
                        lab = _base(getattr(b, "text", None) or (b if isinstance(b, str) else ""))
                        if not lab or any(w in lab for w in _SKIP):
                            continue
                        found.setdefault((sec, lab, field), set()).add(use_plan)
    await eng.dispose()
    out = []
    order = {"free": 0, "growth": 1, "pro": 2, "partner": 3}
    for (sec, lab, field), plans in found.items():
        out.append((field, min(plans, key=lambda p: order[p]), sec_title.get(sec, sec), lab))
    out.sort()
    return out


def render(entries) -> str:
    lines = ['"""تولیدشده با scripts/gen_ai_menu_map.py — دستی ویرایش نکن؛ بعد از تغییرِ منوهایِ پنل دوباره بساز."""',
             "# (نوعِ ربات, کمترین پلنی که دکمه را دارد, بخشِ پنل, برچسبِ دکمه)",
             "MENU_ENTRIES: tuple[tuple[str, str, str, str], ...] = ("]
    for e in entries:
        lines.append("    " + repr(tuple(e)) + ",")
    lines.append(")")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = render(asyncio.run(collect()))
    if a.check:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        sys.exit(0 if cur == text else 1)
    open(OUT, "w", encoding="utf-8").write(text)
    print("✅", OUT, text.count("\n") - 3, "ورودی")
