#!/usr/bin/env python3
"""
preflight.py — بررسیِ سلامتِ کد *قبل از* ری‌استارتِ سرویس.

چرا لازم شد: این پروژه فایل‌به‌فایل دیپلوی می‌شود. اگر فایلی که تابعِ
تازه‌ای را *صدا می‌زند* آپلود شود ولی فایلی که آن تابع را *تعریف می‌کند*
جا بماند، کد بی‌سروصدا سالم به‌نظر می‌رسد (چون importها داخلِ تابع‌اند و
موقعِ بالا آمدنِ سرویس اجرا نمی‌شوند) و خطا تازه وقتی به کاربر می‌خورد که
دستش به آن دکمه برسد. دقیقاً همان اتفاقی که با
`ImportError: cannot import name 'reclaim_token_if_archived'` افتاد.

این اسکریپت دو کار می‌کند:
  ۱) همهٔ ماژول‌های پروژه را واقعاً import می‌کند (خطاهای سطحِ ماژول)
  ۲) importهای «تنبل» (داخلِ تابع) را هم استخراج و بررسی می‌کند —
     همان‌هایی که مرحلهٔ ۱ هرگز اجرایشان نمی‌کند

    python3 preflight.py && systemctl restart botsaz-tenant botsaz-platform

اگر خطایی باشد کدِ خروجیِ غیرِ صفر می‌دهد، پس `&&` جلوی ری‌استارت را
می‌گیرد و کاربرها هیچ‌وقت آن خطا را نمی‌بینند.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# ماژول‌هایی که import کردنشان عوارضِ جانبی دارد (سرویس را بالا می‌آورند)
ENTRYPOINTS = {"main", "main_platform", "main_tenant",
               "backfill_stuck_nowpay", "restore_one_bot",
               "check_health", "rewebhook", "log_report"}

SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "_trash", "tests",
             "__MACOSX", "node_modules", "scripts"}

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(ROOT))


def py_modules() -> list[tuple[str, Path]]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # === PATCH_PREFLIGHT_TRASH_PREFIX_V1 ===
        # باگِ واقعی: SKIP_DIRS شاملِ "_trash" بود ولی مقایسه *دقیق* است،
        # و پوشهٔ قرنطینه نامِ تاریخ‌دار دارد (`_trash_20260908_002104`).
        # پس preflight واردش می‌شد و اسکریپت‌هایِ پچِ یک‌بارمصرفِ داخلش را
        # import می‌کرد. یکی از آن‌ها (fix_selfsaz_admin_button_sszg2.py)
        # موقعِ import مستقیم `raise SystemExit(0)` می‌زند — که کلِ فرایندِ
        # preflight را وسطِ مرحلهٔ ۱ می‌کشت.
        #
        # نتیجه‌اش بدترین حالتِ ممکن بود: preflight با کدِ ۰ و بدونِ هیچ
        # پیامِ خطایی تمام می‌شد، یعنی چند روز «سالم است» گزارش می‌کرد
        # درحالی‌که اصلاً چیزی را چک نکرده بود.
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS
                       and not d.startswith("_trash")
                       and not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".py") or ".bak" in fn or fn == "preflight.py":
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(ROOT)
            parts = list(rel.parts)
            parts[-1] = parts[-1][:-3]
            if parts[-1] == "__init__":
                parts = parts[:-1]
                if not parts:
                    continue
            out.append((".".join(parts), p))
    return out


_SYNTAX_ERRORS: list = []


def lazy_imports(p: Path) -> list[tuple[int, str, list[str]]]:
    """importهایی که داخلِ بدنهٔ تابع‌اند (سطحِ ماژول نیستند)."""
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError as e:
        _SYNTAX_ERRORS.append((p, e))
        return []
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                out.append((node.lineno, node.module,
                            [a.name for a in node.names]))
    return out


def imported_anywhere(mods: list[tuple[str, Path]]) -> set[str]:
    """ماژول‌هایی که دستِ‌کم یک فایلِ دیگر واردشان می‌کند."""
    used: set[str] = set()
    for _n, p in mods:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    used.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                used.add(node.module)
    return used


def main() -> int:
    mods = py_modules()
    used = imported_anywhere(mods)
    errors: list[str] = []
    warns: list[str] = []

    # ── مرحلهٔ ۱: importِ واقعیِ ماژول‌ها ──
    print(f"🔍 مرحلهٔ ۱ — importِ {len(mods)} ماژول…")
    loaded = set()
    for name, _p in mods:
        if name in ENTRYPOINTS:
            continue
        try:
            importlib.import_module(name)
            loaded.add(name)
        except SystemExit as e:
            # === PATCH_PREFLIGHT_TRASH_PREFIX_V1 ===
            # `except Exception` جلویِ SystemExit را نمی‌گیرد (از BaseException
            # ارث می‌برد)، پس ماژولی که موقعِ import خودکشی می‌کند، کلِ
            # preflight را هم با خودش می‌بُرد — بی‌صدا و با کدِ خروجیِ ۰.
            # این‌جا صریح می‌گیریمش تا بررسی ادامه پیدا کند و خودِ آن فایل
            # به‌عنوان مشکل گزارش شود.
            line = (f"ماژولِ {name} موقعِ import فرایند را می‌کشد "
                    f"(SystemExit: {e}) — احتمالاً اسکریپتِ پچِ یک‌بارمصرف "
                    f"است و نباید کنارِ کدِ سرویس بماند.")
            if name in used:
                errors.append(f"❌ {line}")
            else:
                warns.append(f"⚠️  {line}")
        except Exception as e:
            # ماژولی که هیچ‌جا import نمی‌شود نمی‌تواند سرویس را بشکند —
            # خرابیِ آن هشدار است، نه دلیلی برای جلوگیری از ری‌استارت.
            # (فایل‌هایِ رهاشده و ماژول‌هایِ نیمه‌تمام در این دسته‌اند.)
            line = f"import ماژولِ {name}\n   {type(e).__name__}: {e}"
            if name in used:
                errors.append(f"❌ {line}")
            else:
                warns.append(f"⚠️  {line}\n   (هیچ‌جا وصل نشده — سرویس را نمی‌شکند.)")

    # ── مرحلهٔ ۲: importهای داخلِ تابع ──
    print("🔍 مرحلهٔ ۲ — بررسیِ importهایِ داخلِ تابع (تنبل)…")
    checked = 0
    for _name, p in mods:
        for lineno, module, names in lazy_imports(p):
            top = module.split(".")[0]
            # نقطه‌هایِ ورودِ سرویس را import نمی‌کنیم: خودشان Router به
            # Dispatcher می‌چسبانند و importِ دوباره‌شان خطایِ ساختگیِ
            # «Router is already attached» می‌سازد.
            if module in ENTRYPOINTS:
                continue
            # فقط ماژول‌هایِ خودِ پروژه؛ کتابخانه‌هایِ بیرونی کارِ ما نیست
            if not (ROOT / f"{top}.py").exists() and not (ROOT / top).is_dir():
                continue
            checked += 1
            try:
                m = importlib.import_module(module)
            except Exception as e:
                errors.append(
                    f"❌ {p.relative_to(ROOT)}:{lineno} — "
                    f"ماژولِ «{module}» import نشد\n   {type(e).__name__}: {e}")
                continue
            for attr in names:
                if attr == "*":
                    continue
                if not hasattr(m, attr):
                    msg = (f"{p.relative_to(ROOT)}:{lineno} — "
                           f"«{attr}» در «{module}» وجود ندارد.")
                    if _name in used:
                        errors.append(
                            f"❌ {msg}\n"
                            f"   احتمالاً فایلِ {module.replace('.', '/')}.py "
                            f"روی سرور قدیمی مانده و آپلود نشده.")
                    else:
                        warns.append(
                            f"⚠️  {msg}\n"
                            f"   (این ماژول هیچ‌جا وصل نشده، پس سرویس را "
                            f"نمی‌شکند — ولی کدش خراب است.)")
    print(f"   {checked} importِ داخلی بررسی شد.")

    for p, e in _SYNTAX_ERRORS:
        rel = p.relative_to(ROOT)
        mod = str(rel)[:-3].replace("/", ".").replace(".__init__", "")
        line = f"خطای نحوی در {rel}: {e}"
        if mod in used:
            errors.append(f"❌ {line}")
        else:
            warns.append(f"⚠️  {line}\n   (هیچ‌جا وصل نشده — سرویس را نمی‌شکند.)")

    print("\n" + "=" * 62)
    for w in warns:
        print(w)
    if warns:
        print("-" * 62)
    if errors:
        for e in errors:
            print(e)
        print("=" * 62)
        print(f"❌ {len(errors)} مشکل پیدا شد — سرویس را ری‌استارت نکن.")
        return 1
    print(f"✅ همه‌چیز سالم است ({len(loaded)} ماژول). می‌توانی ری‌استارت کنی.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
