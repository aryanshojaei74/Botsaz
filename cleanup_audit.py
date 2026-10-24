#!/usr/bin/env python3
"""
cleanup_audit.py — گزارشِ فایل‌هایِ بی‌مصرفِ پروژهٔ بات‌ساز.

چرا این ابزار: کنارِ کدِ اصلی ده‌ها فایلِ یک‌بارمصرف جمع شده — اسکریپت‌هایِ
پچ (`apply_*.py` / `patch_*.py`)، بکاپ‌هایِ `.bak-*`، لاگ‌ها و اسکریپت‌هایِ
تشخیصیِ موقت. این‌ها نه import می‌شوند نه اجرا، ولی هر بار خواندنِ پروژه را
سخت‌تر می‌کنند.

روشِ کار: از نقطه‌هایِ ورودِ واقعی (`main.py`, `main_tenant.py`, ...) گرافِ
importها را دنبال می‌کند و هر ماژولی که از آن‌ها قابلِ‌رسیدن باشد را «زنده»
علامت می‌زند — از جمله importهایِ داخلِ تابع (lazy) که در این پروژه زیاد است.

    python3 cleanup_audit.py              # فقط گزارش (هیچ چیزی پاک نمی‌شود)
    python3 cleanup_audit.py --move       # انتقال به پوشهٔ _trash/ به‌جایِ حذف
    python3 cleanup_audit.py --delete     # حذفِ واقعی (با تاییدِ دستی)

پیش‌فرض همیشه «فقط گزارش» است. حتی با --delete، فایل‌هایی که مطمئن نیستیم
دست‌نخورده می‌مانند و در بخشِ «نیازمندِ بررسیِ دستی» فهرست می‌شوند.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# نقطه‌هایِ ورودِ واقعیِ سرویس‌ها + هرچیزی که systemd/cron صدا می‌زند.
ENTRYPOINTS = [
    "main.py", "main_tenant.py", "main_platform.py",
    "check_health.py", "log_report.py", "rewebhook.py",
    "restore_one_bot.py", "backfill_stuck_nowpay.py",
]

# پوشه‌هایی که اصلاً واردشان نمی‌شویم.
SKIP_DIRS = {".git", "__pycache__", "__MACOSX", "node_modules", "_trash",
             ".venv", "venv", "tests"}

# «قطعاً دورریختنی» — الگویِ نامِ فایل‌هایِ یک‌بارمصرف/بکاپ.
JUNK_PATTERNS = [
    (r"\.bak$",                     "بکاپِ قدیمی"),
    (r"\.bak-",                     "بکاپِ خودکارِ پچ"),
    (r"\.bak\d*$",                  "بکاپِ قدیمی"),
    (r"^apply_.*\.py$",             "اسکریپتِ پچِ یک‌بارمصرف"),
    (r"^patch_.*\.py$",             "اسکریپتِ پچِ یک‌بارمصرف"),
    (r"^fix_.*\.py$",               "اسکریپتِ اصلاحِ یک‌بارمصرف"),
    (r"^diag_.*\.py$",              "اسکریپتِ تشخیصیِ موقت"),
    (r"^find_.*\.py$",              "اسکریپتِ بررسیِ موقت"),
    (r"\.orig$|\.rej$",             "باقی‌ماندهٔ merge/patch"),
    (r"^error\.log(\.\d+)?$",       "فایلِ لاگ"),
    (r".*\.log(\.\d+)?$",           "فایلِ لاگ"),
    (r"\.pyc$",                     "بایت‌کدِ کامپایل‌شده"),
    (r"^\.DS_Store$",               "زبالهٔ macOS"),
    (r"~$",                         "فایلِ موقتِ ویرایشگر"),
]


def iter_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            out.append(Path(dirpath) / fn)
    return out


def module_name(p: Path) -> str | None:
    """مسیرِ فایل → نامِ ماژولِ قابلِ import (مثلِ tenant.shop)."""
    if p.suffix != ".py":
        return None
    rel = p.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
        if not parts:
            return None
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def imports_of(p: Path) -> set[str]:
    """همهٔ ماژول‌هایی که این فایل import می‌کند — شاملِ importهایِ داخلِ
    تابع (الگویِ lazy که در این پروژه برایِ شکستنِ حلقهٔ import رایج است)."""
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # importهایِ نسبی
                continue
            if node.module:
                found.add(node.module)
                for a in node.names:
                    found.add(f"{node.module}.{a.name}")
    return found


def build_live_set(py_files: dict[str, Path]) -> set[str]:
    """پیمایشِ گرافِ import از نقطه‌هایِ ورود."""
    live: set[str] = set()
    queue = []
    for e in ENTRYPOINTS:
        m = module_name(ROOT / e)
        if m and m in py_files:
            queue.append(m)
    while queue:
        mod = queue.pop()
        if mod in live:
            continue
        live.add(mod)
        for imp in imports_of(py_files[mod]):
            # هم خودِ ماژول، هم بسته‌ای که ازش چیزی گرفته شده
            for cand in (imp, imp.rsplit(".", 1)[0] if "." in imp else imp):
                if cand in py_files and cand not in live:
                    queue.append(cand)
    return live


def defines_router(p: Path) -> bool:
    """آیا این ماژول یک Router تعریف می‌کند؟ (یعنی یک قابلیتِ کامل است که
    فقط فراموش شده وصل شود — نه یک فایلِ دورریختنی.)"""
    txt = p.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"^\s*\w+\s*=\s*Router\(\)", txt, re.M))


def referenced_by_name(name: str, py_files: dict[str, Path],
                       extra_text: str) -> bool:
    """آیا نامِ این فایل جایی به‌صورتِ رشته ذکر شده؟ (systemd، اسکریپتِ shell،
    importهایِ داینامیک با importlib و ...)"""
    return bool(re.search(rf"\b{re.escape(name)}\b", extra_text))


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--move", action="store_true",
                   help="انتقال به پوشهٔ _trash/ به‌جایِ حذف")
    g.add_argument("--delete", action="store_true", help="حذفِ واقعی")
    ap.add_argument("--include-dead", action="store_true",
                    help="ماژول‌هایِ مردهٔ پایتون را هم شامل شود "
                         "(پیش‌فرض: فقط بکاپ/پچ/لاگ جابه‌جا می‌شود)")
    args = ap.parse_args()

    all_files = iter_files()
    py_files: dict[str, Path] = {}
    for p in all_files:
        m = module_name(p)
        if m:
            py_files[m] = p

    live = build_live_set(py_files)

    # متنِ همهٔ فایل‌هایِ غیر-پایتون (سرویس، shell، conf) برایِ بررسیِ ارجاعِ اسمی
    extra_text = ""
    for p in all_files:
        if p.suffix in (".sh", ".service", ".conf", ".cfg", ".ini", ".txt",
                        ".load", ".json", ".yml", ".yaml"):
            extra_text += p.read_text(encoding="utf-8", errors="ignore")
    for m in live:                      # کدِ زنده هم ممکن است اسمِ فایلی را داشته باشد
        extra_text += py_files[m].read_text(encoding="utf-8", errors="ignore")

    junk: list[tuple[Path, str, int]] = []
    dead_py: list[tuple[Path, str, int]] = []
    review: list[tuple[Path, str, int]] = []

    for p in sorted(all_files):
        rel = p.relative_to(ROOT)
        name = p.name
        size = p.stat().st_size

        reason = None
        for pat, why in JUNK_PATTERNS:
            if re.search(pat, name):
                reason = why
                break
        if reason:
            junk.append((rel, reason, size))
            continue

        m = module_name(p)
        if m is None:
            continue
        if m in live:
            continue
        if m.startswith("tests.") or m == "conftest" or name.startswith("test_"):
            continue
        if referenced_by_name(name, py_files, extra_text):
            review.append((rel, "اسمش جایی ذکر شده — دستی بررسی کن", size))
        elif defines_router(p):
            # ماژولی که Router می‌سازد ولی هیچ‌جا include نشده = قابلیتِ
            # نیمه‌تمام یا وصل‌نشده، نه زباله. هرگز خودکار پاک نمی‌شود.
            review.append((rel, "⚡️ Router دارد ولی include نشده — قابلیتِ وصل‌نشده", size))
        else:
            dead_py.append((rel, "از هیچ نقطهٔ ورودی import نمی‌شود", size))

    def show(title: str, rows: list) -> int:
        if not rows:
            return 0
        tot = sum(r[2] for r in rows)
        print(f"\n{'='*70}\n{title}  —  {len(rows)} فایل، {tot/1024:.0f} کیلوبایت\n{'='*70}")
        for rel, why, size in rows:
            print(f"  {size/1024:8.1f} KB  {str(rel):<55} {why}")
        return tot

    print(f"📁 پروژه: {ROOT}")
    print(f"🔎 ماژول‌هایِ زنده (از {len(ENTRYPOINTS)} نقطهٔ ورود): "
          f"{len(live)} از {len(py_files)}")

    t1 = show("🗑  قطعاً دورریختنی (بکاپ / اسکریپتِ پچ / لاگ)", junk)
    t2 = show("💀 ماژولِ پایتونِ مرده (هیچ‌جا import نمی‌شود)", dead_py)
    t3 = show("⚠️  نیازمندِ بررسیِ دستی (اسمش جایی ذکر شده)", review)

    print(f"\n{'='*70}")
    print(f"💾 آزادسازی با اقدامِ پیش‌فرض: {t1/1024/1024:.2f} مگابایت "
          f"({len(junk)} فایل — فقط دستهٔ «قطعاً دورریختنی»)")
    if t2:
        print(f"   (+ {t2/1024/1024:.2f} مگابایتِ دیگر فقط با --include-dead)")
    print("⚠️  بخشِ «نیازمندِ بررسی» هرگز خودکار پاک نمی‌شود.")

    # === پیش‌فرضِ محافظه‌کارانه ===
    # فقط دستهٔ «قطعاً دورریختنی» (بکاپ/پچ/لاگ) جابه‌جا می‌شود. ماژول‌هایِ
    # مردهٔ پایتون کدِ واقعی‌اند — شاید قابلیتی باشند که هنوز وصل نشده —
    # پس فقط با --include-dead دست‌کاری می‌شوند. دستهٔ «نیازمندِ بررسی»
    # هرگز و تحتِ هیچ شرایطی خودکار جابه‌جا نمی‌شود.
    targets = [r[0] for r in junk]
    if args.include_dead:
        targets += [r[0] for r in dead_py]
    else:
        if dead_py:
            print(f"\n🛡  {len(dead_py)} ماژولِ مردهٔ پایتون دست‌نخورده می‌ماند "
                  "(برایِ شاملشدن: --include-dead)")
        if review:
            print(f"🛡  {len(review)} فایلِ «نیازمندِ بررسی» هم دست‌نخورده می‌ماند.")
    if not (args.move or args.delete):
        print("\n(حالتِ گزارش. برایِ اقدام: --move یا --delete)")
        return 0
    if not targets:
        return 0

    action = "انتقال به _trash/" if args.move else "حذفِ دائمی"
    print(f"\n❗️ {action} برایِ {len(targets)} فایل.")
    if input("مطمئنی؟ بنویس «بله»: ").strip() not in ("بله", "yes", "y"):
        print("لغو شد.")
        return 1

    trash = ROOT / "_trash"
    for rel in targets:
        src = ROOT / rel
        if args.move:
            dst = trash / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        else:
            src.unlink()
    print(f"✅ انجام شد." + (f" فایل‌ها در {trash}" if args.move else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
