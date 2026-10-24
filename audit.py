#!/usr/bin/env python3
"""
audit.py — بررسیِ خودکارِ الگوهایِ باگ‌خیزِ پروژهٔ botsaz.

از ریشهٔ پروژه اجرا کن:

    python3 audit.py                 # همهٔ بررسی‌ها
    python3 audit.py --only hang     # فقط یکی
    python3 audit.py --changed a.py b.py   # فقط فایل‌هایِ مشخص

بررسی‌ها (هرکدام از یک باگِ واقعیِ همین پروژه بیرون آمده):

  hang     هندلرِ callback که هیچ‌وقت answer نمی‌دهد → اسپینرِ تلگرام
           رویِ دکمهٔ کاربر گیر می‌کند و فکر می‌کند ربات هنگ کرده.
  silent   `except: pass` → خطا بی‌سروصدا بلعیده می‌شود و دیباگ غیرممکن.
  router   روتری که include نشده یا دوبار include شده.
  lazy     importِ داخلِ تابع که هدفش وجود ندارد → همان
           ImportError ای که فقط موقعِ کلیکِ کاربر می‌ترکد، نه موقعِ بالا آمدن.
  money    مسیرِ فروش/خرید که `_purchase_alert` را صدا نمی‌زند → کانالِ
           اعتمادسازی و اطلاعِ خودکارِ خرید بی‌صدا از کار می‌افتد.
  noop     دکمهٔ سرتیتر (`callback_data="noop"`) که رنگ گرفته.
  session  استفاده از شیءِ ORM بیرونِ بلاکِ `async with Session()`.

خروجی: کدِ ۱ اگر خطایی باشد (برایِ استفاده در `&&` قبل از دیپلوی).
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
SKIP = {".git", "__pycache__", "venv", ".venv", "_trash", "__MACOSX",
        "node_modules", "tests", "scripts"}
ENTRYPOINTS = {"main", "main_platform", "main_tenant", "check_health",
               "rewebhook", "log_report", "backfill_stuck_nowpay",
               "restore_one_bot"}
ENTRY_FILES = {"main.py", "main_platform.py", "main_tenant.py"}


def py_files(only: list[str] | None = None) -> list[Path]:
    if only:
        return [Path(f) for f in only if Path(f).exists()]
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fn in filenames:
            if fn.endswith(".py") and ".bak" not in fn and fn != "audit.py":
                out.append(Path(dirpath) / fn)
    return sorted(out)


_SRC: dict[Path, str] = {}
_TREE: dict[Path, object] = {}
_LINES: dict[Path, list] = {}


def src_of(p: Path) -> str:
    if p not in _SRC:
        _SRC[p] = p.read_text(encoding="utf-8", errors="ignore")
    return _SRC[p]


def lines_of(p: Path) -> list:
    if p not in _LINES:
        _LINES[p] = src_of(p).split("\n")
    return _LINES[p]


def tree_of(p: Path):
    """درختِ AST — کش‌شده. بدونِ کش، هر بررسی فایل‌هایِ ۲۰هزارخطی را
    دوباره پارس می‌کرد و اجرا چند دقیقه طول می‌کشید."""
    if p not in _TREE:
        try:
            _TREE[p] = ast.parse(src_of(p))
        except SyntaxError:
            _TREE[p] = None
    return _TREE[p]


def seg(p: Path, node) -> str:
    """متنِ یک تابع، با برشِ خطوط.

    جایگزینِ ast.get_source_segment که برایِ *هر* تابع کلِ فایل را از نو
    پردازش می‌کرد — رویِ tenant/__init__.py (۲۰هزار خط، صدها تابع) همین
    یک تفاوت اجرا را از ~۴۰ ثانیه به کمتر از یک ثانیه رساند.
    """
    ls = lines_of(p)
    end = getattr(node, "end_lineno", None) or node.lineno
    return "\n".join(ls[node.lineno - 1:end])


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ─────────────── بررسی‌ها ───────────────

def check_hang(files) -> list[str]:
    """هندلرِ callback بدونِ answer — با دنبال‌کردنِ تابع‌هایِ کمکی."""
    bodies: dict[str, str] = {}
    for p in files:
        t = tree_of(p)
        if not t:
            continue
        for n in ast.walk(t):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies[n.name] = seg(p, n)

    # هر تابعِ داخلیِ پروژه که await می‌شود، نه فقط نام‌هایِ زیرخط‌دار —
    # وگرنه هندلری که کارش را به یک هندلرِ دیگر می‌سپارد اشتباهاً
    # «بدونِ answer» گزارش می‌شود.
    calls: dict[str, set] = {n: set(re.findall(r"await\s+(\w+)\(", b))
                             for n, b in bodies.items()}
    direct = {n: (".answer(" in b) for n, b in bodies.items()}
    memo: dict[str, bool] = {}

    def answers(name: str, depth: int = 0, seen: frozenset = frozenset()) -> bool:
        if depth == 0 and name in memo:
            return memo[name]
        if depth > 3 or name in seen:
            return False
        res = direct.get(name, False) or any(
            answers(c, depth + 1, seen | {name}) for c in calls.get(name, ()))
        if depth == 0:
            memo[name] = res
        return res

    out = []
    for p in files:
        t = tree_of(p)
        if not t:
            continue
        for n in ast.walk(t):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            deco = " ".join(seg(p, d) for d in n.decorator_list)
            if "callback_query" not in deco:
                continue
            if not answers(n.name):
                out.append(f"{rel(p)}:{n.lineno} — «{n.name}» هیچ‌وقت "
                           f"cb.answer() نمی‌زند؛ اسپینرِ کاربر گیر می‌کند.")
    return out


# === PATCH_AUDIT_PRECISION_V1 ===
# عملیاتِ رابطِ کاربریِ تلگرام که شکستشان عادی است (پیام قدیمی/حذف‌شده، callbackِ
# منقضی، ربات بلاک شده...). `except Exception: pass` دورِ *فقط* یکی از این‌ها
# اصطلاحِ درستِ کد است و گزارش‌کردنش هشدارِ الکی می‌شد.
_BEST_EFFORT_UI_CALLS = {
    "delete", "answer", "edit_text", "edit_caption", "edit_reply_markup",
    "delete_message", "answer_callback_query", "unpin", "pin",
    "close",   # بستنِ session/اتصال در finally — شکستش قابلِ چشم‌پوشی است
}
# اسکریپت‌هایِ تشخیصیِ خط‌فرمان در ریشه؛ جزوِ سرویسِ زنده نیستند.
_DIAGNOSTIC_PREFIXES = ("check_", "diag_", "test_")


def _is_bare_swallow(h: ast.ExceptHandler) -> bool:
    """except / except Exception / except BaseException  که بدنه‌اش فقط `pass` است."""
    if len(h.body) != 1 or not isinstance(h.body[0], ast.Pass):
        return False
    t = h.type
    return t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))


def _is_best_effort_ui(try_node: ast.Try) -> bool:
    """بدنهٔ try فقط یک `await x.delete()/answer()/edit_*()` است."""
    if len(try_node.body) != 1:
        return False
    st = try_node.body[0]
    if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Await)):
        return False
    call = st.value.value
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr in _BEST_EFFORT_UI_CALLS)


def _has_explaining_comment(p: Path, h: ast.ExceptHandler) -> bool:
    """بینِ خطِ `except` و `pass` یک خطِ کامنتیِ کامل هست (یعنی نویسنده دلیلش را نوشته)."""
    lines = lines_of(p)
    pass_line = h.body[0].lineno
    return any(lines[i].lstrip().startswith("#") for i in range(h.lineno, pass_line - 1))


def check_silent(files) -> list[str]:
    """`except Exception: pass` — بلعیدنِ هر خطایی بدونِ رد.

    عمداً فقط دنبالِ exceptِ *لخت* می‌گردد. `except OSError: pass` رویِ
    os.remove یا `except TelegramForbiddenError: pass` وقتی کاربر ربات را
    بلاک کرده، اصطلاحِ درستِ پایتون است و نباید گزارش شود — گزارش‌کردنشان
    فقط باعث می‌شود کسی به این بررسی اعتماد نکند.

    PATCH_AUDIT_PRECISION_V1: با AST کار می‌کند (نه regex)، و این دو را گزارش نمی‌کند:
      • try که فقط یک عملیاتِ best-effortِ تلگرام دارد (حذفِ پیامِ کاربر، cb.answer، ...)
        یا بستنِ session (`.close()`)
      • exceptی که بینِ `except` و `pass` یک کامنتِ توضیحی دارد (نویسنده دلیلش را نوشته)
      • اسکریپت‌هایِ تشخیصیِ ریشه (check_*.py / diag_*.py / test_*.py)
    هر `except: pass` دورِ کدِ دیتابیس/پول/منطقِ برنامه همچنان گزارش می‌شود.
    """
    out = []
    for p in files:
        if p.name.startswith(_DIAGNOSTIC_PREFIXES) and p.parent == ROOT:
            continue
        t = tree_of(p)
        if not t:
            continue
        for node in ast.walk(t):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                if (_is_bare_swallow(h) and not _is_best_effort_ui(node)
                        and not _has_explaining_comment(p, h)):
                    out.append(f"{rel(p)}:{h.lineno} — `except Exception: pass` هر خطایی را "
                               f"بی‌سروصدا می‌بلعد؛ حداقل logger.exception بگذار.")
    return out


def check_router(files) -> list[str]:
    defined: dict[str, str] = {}
    dup_names: set[str] = set()
    for p in files:
        for m in re.finditer(r"^(\w+_router)\s*=\s*Router\(\)", src_of(p), re.M):
            if m.group(1) in defined:
                # همین نام در دو ماژولِ متفاوت (مثلاً ticket_router در
                # سمتِ پلتفرم و سمتِ مستأجر) — شمارشِ include بی‌معنا می‌شود.
                dup_names.add(m.group(1))
            defined[m.group(1)] = rel(p)
    # نگاشتِ نامِ مستعار → نامِ اصلی (import ... as _x)
    alias: dict[tuple, str] = {}
    for p in files:
        for m in re.finditer(r"import\s+(\w+_router)\s+as\s+(\w+)", src_of(p)):
            alias[(rel(p), m.group(2))] = m.group(1)

    included: dict[str, int] = {}
    inc_files: dict[str, set] = {}
    for p in files:
        t = tree_of(p)
        if not t:
            continue
        # با AST می‌شماریم، نه regex — وگرنه نمونه‌کدِ داخلِ docstring و
        # کامنت هم به‌عنوانِ include شمرده می‌شود (باگی که واقعاً دیدیم).
        for n in ast.walk(t):
            # === PATCH_AUDIT_INCLUDE_WRAPPER_V1 ===
            # از فازِ ۱۰ (سندِ ریستارتِ ایزوله)، بعضی ثبت‌ها به‌جایِ
            # مستقیمِ tenant_router.include_router(x) از طریقِ کمک‌تابعِ
            # _include(x, "kind") انجام می‌شوند (تا با TENANT_BOT_KIND
            # شرطی باشند). این هم باید همان‌قدر «include شده» حساب شود،
            # وگرنه این چک برایِ همان ۱۸ روترِ اختصاصی همیشه false-positive
            # می‌دهد.
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_include" and n.args
                    and isinstance(n.args[0], ast.Name)):
                name = alias.get((rel(p), n.args[0].id), n.args[0].id)
                included[name] = included.get(name, 0) + 1
                inc_files.setdefault(name, set()).add(rel(p))
                continue
            if not (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "include_router"
                    and n.args and isinstance(n.args[0], ast.Name)):
                continue
            name = alias.get((rel(p), n.args[0].id), n.args[0].id)
            included[name] = included.get(name, 0) + 1
            inc_files.setdefault(name, set()).add(rel(p))

    out = []
    for name, where in sorted(defined.items()):
        if name in dup_names:
            continue
        n = included.get(name, 0)
        if n == 0:
            out.append(f"{where} — روترِ «{name}» ساخته شده ولی هیچ‌جا "
                       f"include نشده؛ هندلرهایش هرگز اجرا نمی‌شوند.")
        elif n > 1 and len(inc_files.get(name, set()) - ENTRY_FILES) > 1:
            # در دو نقطهٔ ورودِ متفاوت (main.py و main_platform.py) طبیعی است؛
            # فقط وقتی *یک* سرویس دوبار include کند مشکل است.
            out.append(f"{where} — روترِ «{name}» {n} بار include شده؛ "
                       f"هندلرها دوبار اجرا می‌شوند.")
    return out


def check_lazy(files) -> list[str]:
    """importِ داخلِ تابع که هدفش وجود ندارد (باگِ دیپلویِ ناقص)."""
    local_mods: dict[str, Path] = {}
    for p in files:
        parts = list(p.relative_to(ROOT).parts) if p.is_absolute() else list(p.parts)
        parts[-1] = parts[-1][:-3]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            local_mods[".".join(parts)] = p

    exported: dict[str, set[str]] = {}
    for mod, p in local_mods.items():
        t = tree_of(p)
        names: set[str] = set()
        if t:
            body = list(t.body)
            for n in list(body):                 # یک لایه داخلِ if/try هم بگرد
                if isinstance(n, (ast.If, ast.Try)):
                    body.extend(n.body)
                    body.extend(getattr(n, "orelse", []))
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(n.name)
                elif isinstance(n, ast.Assign):
                    for tgt in n.targets:
                        if isinstance(tgt, ast.Name):
                            names.add(tgt.id)
                elif isinstance(n, ast.AnnAssign):        # x: dict = {...}
                    if isinstance(n.target, ast.Name):
                        names.add(n.target.id)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        names.add(a.asname or a.name.split(".")[0])
        exported[mod] = names

    out = []
    for p in files:
        t = tree_of(p)
        if not t:
            continue
        for fn in ast.walk(t):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if not (isinstance(n, ast.ImportFrom) and n.module and not n.level):
                    continue
                if n.module not in exported or n.module in ENTRYPOINTS:
                    continue
                for a in n.names:
                    # from tenant import crypto_pay → زیرماژول است، نه صفت
                    if f"{n.module}.{a.name}" in local_mods:
                        continue
                    if a.name != "*" and a.name not in exported[n.module]:
                        out.append(
                            f"{rel(p)}:{n.lineno} — «{a.name}» در «{n.module}» "
                            f"نیست. این خطا فقط لحظه‌ای که کاربر آن دکمه را "
                            f"بزند می‌ترکد، نه موقعِ بالا آمدنِ سرویس.")
    return out


def _records_sale(t: ast.AST) -> bool:
    """آیا این ماژول جایی *واقعاً* فروش ثبت می‌کند؟ یعنی یک فراخوانی با
    `kind="purchase"` یا `kind="sold"` (مثلاً WalletTxn(...)). PATCH_AUDIT_PRECISION_V1:
    قبلاً regex روی کلِ متن بود و ذکرِ `WalletTxn(kind="purchase")` در docstring هم
    (مثلاً توابعی که فقط خرید را *می‌خوانند*) به‌عنوانِ «ثبتِ فروش» گزارش می‌شد."""
    for n in ast.walk(t):
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if (kw.arg == "kind" and isinstance(kw.value, ast.Constant)
                        and kw.value.value in ("purchase", "sold")):
                    return True
    return False


def check_money(files) -> list[str]:
    """مسیرِ فروش که به قلابِ مشترکِ گزارشِ خرید وصل نیست.

    فقط دنبالِ *فروش* می‌گردد (`kind="purchase"` یا `kind="sold"` هنگامِ
    ساختِ WalletTxn/فروش) — شارژِ کیف پول عمداً گزارش نمی‌شود.
    این باگ واقعاً دو بار در این پروژه اتفاق افتاد: یک‌بار برایِ
    ربات‌هایِ SMM و یک‌بار برایِ فروشِ ربات در رباتِ همکار.
    """
    out = []
    for p in files:
        name = rel(p)
        if name in {"models.py"} or name.startswith(("main", "platform_")):
            continue
        t = tree_of(p)
        if not t or not _records_sale(t):
            continue
        s = src_of(p)
        # خریدِ عمدهٔ انبار توسطِ خودِ همکار از پلتفرم، فروش به مشتری
        # نیست و نباید در کانالِ اعتمادسازی پست شود.
        if "PartnerStockOrder(" in s:
            continue
        # _notify_gift_order_trust_success خودش بعد از تحویل _purchase_alert را صدا می‌زند
        if ("_purchase_alert" in s or "send_trust_channel_post" in s
                or "_notify_gift_order_trust_success" in s):
            continue
        out.append(f"{name} — فروش ثبت می‌کند ولی `_purchase_alert` را صدا "
                   f"نمی‌زند؛ کانالِ اعتمادسازی و اطلاعِ خودکارِ خرید برایِ "
                   f"این مسیر کار نمی‌کند.")
    return out


def check_noop(files) -> list[str]:
    pat = re.compile(r"(?:\w*\.?button|InlineKeyboardButton|hx_button)"
                     r"\((?:[^()]|\([^()]*\))*?\)")
    out = []
    for p in files:
        s = src_of(p)
        for m in pat.finditer(s):
            t = " ".join(m.group(0).split())
            if '"noop"' in t and "style=ButtonStyle" in t:
                ln = s[:m.start()].count("\n") + 1
                out.append(f"{rel(p)}:{ln} — دکمهٔ سرتیتر (noop) رنگ گرفته؛ "
                           f"سرتیترها باید بی‌رنگ بمانند.")
    return out


CHECKS = {
    "hang": ("هندلرِ بدونِ answer", check_hang, True),
    "silent": ("except بی‌صدا", check_silent, False),
    "router": ("روترِ گم‌شده/تکراری", check_router, True),
    "lazy": ("importِ تنبلِ شکسته", check_lazy, True),
    "money": ("مسیرِ فروشِ بدونِ گزارش", check_money, False),
    "noop": ("سرتیترِ رنگی", check_noop, False),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(CHECKS))
    ap.add_argument("--changed", nargs="*")
    args = ap.parse_args()

    files = py_files(args.changed)
    names = [args.only] if args.only else list(CHECKS)
    if args.changed:
        # این دو بررسی به دیدِ کلِ پروژه نیاز دارند؛ روی چند فایلِ تکی
        # نتیجهٔ گمراه‌کننده می‌دهند (مثلاً «روتر include نشده»).
        names = [n for n in names if n not in ("router", "lazy")]
        print("ℹ️  حالتِ --changed: بررسی‌هایِ router و lazy رد شدند "
              "(به دیدِ کلِ پروژه نیاز دارند).")
    print(f"📁 {ROOT}  —  {len(files)} فایل", flush=True)
    print("(اولین اجرا چند ثانیه طول می‌کشد؛ فایل‌های بزرگ پارس می‌شوند)\n",
          flush=True)

    fatal = warn = 0
    for key in names:
        label, fn, blocking = CHECKS[key]
        print(f"   … {label}", end="\r", flush=True)   # نشانهٔ زنده‌بودن
        try:
            hits = fn(files)
        except Exception as e:                       # noqa: BLE001
            print(f"⚠️  بررسیِ «{label}» شکست خورد: {e}")
            continue
        icon = "❌" if (hits and blocking) else ("⚠️ " if hits else "✅")
        print(f"{icon} {label}: {len(hits)}" + " " * 20, flush=True)
        for h in hits[:15]:
            print(f"     {h}")
        if len(hits) > 15:
            print(f"     … و {len(hits) - 15} مورد دیگر")
        if hits:
            (globals().__setitem__("_", None))
            if blocking:
                fatal += len(hits)
            else:
                warn += len(hits)

    print("\n" + "=" * 60)
    if fatal:
        print(f"❌ {fatal} مشکلِ بازدارنده + {warn} هشدار.")
        return 1
    print(f"✅ مشکلِ بازدارنده‌ای نیست ({warn} هشدارِ قابلِ بررسی).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
