#!/usr/bin/env python3
"""
cleanup_extras.py — پاک‌سازیِ فایل‌هایِ اضافه از ریشهٔ پروژه.

فقط فایل‌هایِ «زائد» را هدف می‌گیرد: نسخه‌هایِ پشتیبانِ دستی (.bak*)، پسماندِ
patch/reject (.rej، .orig)، اسکریپت‌هایِ یک‌بارمصرفِ patch_*.py و
fix_*.patch، لاگ‌هایِ چرخیده (error.log.N / *.gz)، __pycache__ و پوشهٔ
__MACOSX. هیچ ماژولِ زنده‌ای در فهرست نیست.

پیش‌فرض «نمایشی» است و چیزی پاک نمی‌کند:

    python3 scripts/cleanup_extras.py            # فقط گزارش
    python3 scripts/cleanup_extras.py --apply    # پاک‌سازیِ واقعی
    python3 scripts/cleanup_extras.py --apply --keep-logs   # لاگ‌ها بمانند

⚠️ قبل از --apply مطمئن شو تغییراتِ کاری‌ات commit شده — این اسکریپت به
سطلِ بازیافت چیزی نمی‌فرستد.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# فایل‌هایی که هرگز نباید پاک شوند، حتی اگر با الگویی جور دربیایند.
PROTECTED = {
    "main.py", "main_platform.py", "main_tenant.py", "config.py", "models.py",
    "plans.py", "requirements.txt", "requirements-dev.txt", "pytest.ini",
}

FILE_PATTERNS = [
    ("پشتیبانِ دستی", ["*.bak", "*.bak_*", "*.py.bak*", "*.orig", "*.rej", "*~"]),
    ("اسکریپتِ یک‌بارمصرفِ پچ", ["patch_*.py", "fix_*.patch", "*.patch"]),
]
LOG_PATTERNS = ["error.log.*", "*.log.gz", "*.log.[0-9]", "*.log.[0-9].gz"]
DIR_NAMES = ["__pycache__", "__MACOSX", ".pytest_cache"]


def _match(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def collect(root: str, keep_logs: bool) -> tuple[list[tuple[str, str]], list[str]]:
    files: list[tuple[str, str]] = []   # (مسیر, دلیل)
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirpath.split(os.sep):
            continue
        for d in list(dirnames):
            if d in DIR_NAMES:
                dirs.append(os.path.join(dirpath, d))
                dirnames.remove(d)
        for fn in filenames:
            if fn in PROTECTED:
                continue
            path = os.path.join(dirpath, fn)
            reason = None
            for label, pats in FILE_PATTERNS:
                if _match(fn, pats):
                    reason = label
                    break
            if reason is None and not keep_logs and _match(fn, LOG_PATTERNS):
                reason = "لاگِ چرخیده"
            if reason:
                files.append((path, reason))
    return files, dirs


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="واقعاً پاک کن")
    ap.add_argument("--keep-logs", action="store_true", help="لاگ‌هایِ چرخیده را نگه دار")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()

    files, dirs = collect(args.root, args.keep_logs)
    if not files and not dirs:
        print("✅ چیزی برای پاک‌کردن پیدا نشد.")
        return 0

    total = 0
    for path, reason in sorted(files):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        total += size
        print(f"  [{reason}] {os.path.relpath(path, args.root)}  ({human(size)})")
    for d in sorted(dirs):
        print(f"  [پوشهٔ موقت] {os.path.relpath(d, args.root)}/")

    print(f"\nجمعاً {len(files)} فایل و {len(dirs)} پوشه — حدود {human(total)}")
    if not args.apply:
        print("\nℹ️ حالتِ نمایشی. برایِ پاک‌سازیِ واقعی: --apply")
        return 0

    removed = 0
    for path, _ in files:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            print(f"❌ {path}: {e}", file=sys.stderr)
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n🧹 {removed} فایل و {len(dirs)} پوشه پاک شد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
