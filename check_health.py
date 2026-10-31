#!/usr/bin/env python3
"""
check_health.py — بررسیِ سلامتِ پروژه پیش از استقرار.

=== PATCH_HEALTH_CHECK_V1 ===
سه الگویِ باگ که در این پروژه بارها تکرار شده‌اند و هر سه *پیش از
ری‌استارت* قابلِ تشخیص‌اند:

  ۱. سایه‌اندازیِ پیشوندِ callback
     F.data.startswith("x:pay:") قبل از F.data == "x:pay:wallet" ثبت
     شود، دومی هرگز اجرا نمی‌شود. یک‌بار پرداخت را کاملاً شکست.

  ۲. دکمه‌ای که هندلر ندارد
     callback_data="wallet" نوشته شد ولی هندلر w:charge بود — دکمه
     بی‌صدا بی‌کار ماند.

  ۳. خطایِ نحوی
     ساده ولی مرگبار: سرویس بالا نمی‌آید و همهٔ ربات‌ها می‌خوابند.

اجرا:
    cd /opt/botsaz && venv/bin/python check_health.py

خروجی صفر یعنی همه‌چیز سالم — می‌توانی ری‌استارت کنی.
"""
from __future__ import annotations

import ast
import glob
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

# فضاهایِ نامی که هندلرشان جایِ دیگری است و نبودشان اینجا خطا نیست
EXTERNAL_PREFIXES = (
    "http", "tg://", "https",
)


def _py_files() -> list[Path]:
    out = []
    for pat in ("*.py", "tenant/*.py"):
        for f in glob.glob(str(BASE / pat)):
            p = Path(f)
            if ".bak" in p.name or p.name.startswith("apply_"):
                continue
            if p.name in ("check_health.py", "test_partner_loop.py"):
                continue
            out.append(p)
    return sorted(out)


def check_syntax(files: list[Path]) -> list[str]:
    errs = []
    for f in files:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            errs.append(f"{f.name}:{e.lineno} — {e.msg}")
    return errs


def _handlers(src: str) -> list[tuple[str, bool]]:
    """(pattern, is_prefix) برایِ هر هندلرِ ثبت‌شده در این فایل."""
    out = []
    pat = (r'callback_query\(\s*(?:[\w.]+,\s*)*F\.data\s*'
           r'(?:\.startswith\(\s*"([^"]+)"|==\s*"([^"]+)"|\.in_\(([^)]*)\))')
    for m in re.finditer(pat, src, re.S):
        if m.group(3):
            for x in re.findall(r'"([^"]+)"', m.group(3)):
                out.append((x, False))
        else:
            out.append((m.group(1) or m.group(2), bool(m.group(1))))
    return out


def check_shadowing(files: list[Path]) -> list[str]:
    """در هر فایل، پیشوندی که هندلرِ بعدی را می‌بلعد.

    فیلترهایی که استثنا دارند (`& ~F.data.startswith(...)`) نادیده
    گرفته می‌شوند — آن‌ها عمداً محدود شده‌اند و سایه نمی‌اندازند.
    """
    probs = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        hs = []
        # کلِ خطِ دکوراتور را بگیر تا استثناها هم دیده شوند
        for m in re.finditer(r'callback_query\((.*?)\)\s*\n\s*async def', src, re.S):
            filt = m.group(1)
            line = src[:m.start()].count("\n") + 1
            has_excl = "~F.data" in filt
            for mm in re.finditer(
                    r'F\.data\s*(?:\.startswith\(\s*"([^"]+)"|==\s*"([^"]+)")',
                    filt):
                # فقط اولین الگو (بقیه معمولاً استثنا هستند)
                hs.append((mm.group(1) or mm.group(2), bool(mm.group(1)),
                           line, has_excl, filt))
                break
        for i, (a, is_pre, la, excl, fa) in enumerate(hs):
            if not is_pre or excl:
                continue
            for b, _p, lb, _e, fb in hs[i + 1:]:
                # اگر فیلترها StateFilterِ متفاوت دارند، همزمان فعال
                # نیستند و سایه‌اندازی رخ نمی‌دهد.
                sa = re.findall(r'(\w+FSM\.\w+|\w+\.\w+_\w+)', fa)
                sb = re.findall(r'(\w+FSM\.\w+|\w+\.\w+_\w+)', fb)
                if sa and sb and set(sa) != set(sb):
                    continue
                if b != a and b.startswith(a):
                    probs.append(
                        f"{f.name}: خط {la} «{a}» جلوی خط {lb} «{b}» را می‌گیرد")
    return probs


def check_dead_buttons(files: list[Path]) -> list[str]:
    """callback_dataهایی که هیچ هندلری در کلِ پروژه ندارند."""
    defined: list[tuple[str, bool]] = []
    used: dict[str, set[str]] = {}
    for f in files:
        src = f.read_text(encoding="utf-8")
        defined.extend(_handlers(src))
        # هندلرهایی که با | ترکیب شده‌اند (F.data.startswith("a") | ...)
        for m in re.finditer(r'F\.data\.startswith\(\s*"([^"]+)"', src):
            defined.append((m.group(1), True))
        for m in re.finditer(r'callback_data=(?:f)?"([^"{]*)', src):
            cb = m.group(1)
            if cb and not cb.startswith(EXTERNAL_PREFIXES):
                used.setdefault(cb, set()).add(f.name)

    def handled(cb: str) -> bool:
        for d, pre in defined:
            if cb.startswith(d) if pre else cb == d:
                return True
            # callback_dataیِ ناقص (مثلِ "tadm:cfg:" که ادامه‌اش با
            # f-string ساخته می‌شود) — اگر هندلری با پیشوندِ بلندتر
            # وجود دارد، دکمه مرده نیست.
            if cb.endswith(":") and d.startswith(cb):
                return True
            # callback_dataیِ ساخته‌شده با f-string: "tadm:cfg:link_" که
            # ادامه‌اش normal/unlimited می‌شود
            if cb.endswith("_") and d.startswith(cb):
                return True
        return False

    return [f"{cb}  (در {', '.join(sorted(fs))})"
            for cb, fs in sorted(used.items()) if not handled(cb)]


def check_button_styles(files: list[Path]) -> list[str]:
    """دکمه‌هایِ بدونِ style — فقط در ماژول‌هایِ همکاری که قرارداد داریم."""
    probs = []
    # فقط ماژول‌هایِ سیستمِ «ربات همکار» — نه tenant/vpn_partners.py که
    # همکارانِ فروشِ VPN داخلِ ربات مستأجر است و قراردادِ رنگ ندارد.
    OWNED = {"partner_menu.py", "partner_panel.py", "partner_sales.py",
             "partner_user.py", "partner_build.py",
             "platform_partner.py", "platform_partner_admin.py",
             "platform_feedback_admin.py"}
    for f in files:
        if f.name not in OWNED:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("button", "InlineKeyboardButton"):
                continue
            kws = {k.arg for k in node.keywords}
            if "text" in kws and "style" not in kws:
                probs.append(f"{f.name}: خط {node.lineno}")
    return probs


def main() -> int:
    files = _py_files()
    print(f"بررسیِ {len(files)} فایل...\n")
    failed = False

    checks = [
        ("خطای نحوی", check_syntax, True),
        ("سایه‌اندازیِ callback", check_shadowing, True),
        ("دکمهٔ بدونِ هندلر", check_dead_buttons, True),
        ("دکمهٔ بدونِ رنگ (ماژول‌های همکاری)", check_button_styles, False),
    ]

    for title, fn, fatal in checks:
        probs = fn(files)
        if not probs:
            print(f"✅ {title}")
            continue
        icon = "❌" if fatal else "⚠️ "
        print(f"{icon} {title} — {len(probs)} مورد:")
        for p in probs[:15]:
            print(f"     {p}")
        if len(probs) > 15:
            print(f"     … و {len(probs) - 15} مورد دیگر")
        if fatal:
            failed = True
        print()

    print()
    if failed:
        print("🛑 مشکلِ جدی هست — پیش از ری‌استارت رفعش کن.")
        return 1
    print("🎉 سالم است. می‌توانی ری‌استارت کنی:")
    print("   sudo systemctl restart botsaz-platform botsaz-tenant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
