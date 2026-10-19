#!/usr/bin/env python3
"""
scripts/check_hosted_app.py — تشخیصِ فقط‌خواندنیِ «توکن درست است ولی Unauthorized می‌دهد» (PATCH_HOSTED_TOKEN_CHECK_V1).

برایِ یک اپِ میزبانی‌شده (سرور فعال‌سازیِ ربات) این‌ها را چک می‌کند، بدونِ تغییرِ هیچ‌چیز و بدونِ چاپِ توکنِ کامل:

  ۱) هر متغیرِ شبیهِ توکن در .env (BOT_TOKEN, TOKEN, …): طول، فاصله/نقل‌قول/خطِ‌جدیدِ اضافه، قالب،
     و جوابِ واقعیِ تلگرام (getMe).
  ۲) فایل‌هایِ پروژه: توکنِ سخت‌کدشده یا فایلِ .env داخلِ zip که با مقدارِ ثبت‌شده فرق دارد
     (علتِ رایجِ «توکنِ تازه را زدم ولی باز Unauthorized می‌دهد»: برنامه توکنِ قدیمیِ فایل را می‌خواند).
  ۳) لاگ: خطِ Unauthorized/Conflict دقیقاً از کجا می‌آید؛ ممکن است Unauthorized از یک APIِ دیگرِ کدِ مشتری
     باشد (مثلاً کلیدِ پنلِ SMM) و ربطی به توکنِ تلگرام نداشته باشد.

اجرا از /opt/botsaz (شناسهٔ اپ همان id در «کاربران و ربات‌ها»):

    venv/bin/python scripts/check_hosted_app.py 17
    venv/bin/python scripts/check_hosted_app.py --name pyhdbkm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
TOKEN_KEYS = ("BOT_TOKEN", "TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN")
SCAN_EXT = (".py", ".env", ".json", ".txt", ".js", ".ts", ".yml", ".yaml", ".ini", ".cfg", ".toml")
MAX_SCAN_FILES = 40


# نامِ ماژولِ ایمپورت ⇒ نامِ بسته در pip (فقط مواردِ رایجی که فرق دارند)
PIP_NAMES = {
    "telegram": "python-telegram-bot", "yt_dlp": "yt-dlp", "youtube_dl": "youtube-dl", "cv2": "opencv-python",
    "PIL": "Pillow", "bs4": "beautifulsoup4", "yaml": "PyYAML", "dotenv": "python-dotenv", "sklearn": "scikit-learn",
    "telethon": "Telethon", "pyrogram": "Pyrogram", "aiogram": "aiogram", "Crypto": "pycryptodome",
    "dateutil": "python-dateutil", "magic": "python-magic", "jwt": "PyJWT", "serial": "pyserial",
}


def _norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def req_has(requirements: str, pip_name: str) -> bool:
    """بسته (با هر مقایسه‌گر/نسخه) در متنِ requirements.txt هست؟"""
    want = _norm_pkg(pip_name)
    for ln in (requirements or "").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln or ln.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)", ln)
        if m and _norm_pkg(m.group(1)) == want:
            return True
    return False


def mask(tok: str) -> str:
    t = (tok or "").strip()
    return t[:4] + "…" + t[-4:] if len(t) > 8 else "…"


def diagnose_token(value: str) -> list[str]:
    """مشکل‌هایِ ظاهریِ یک مقدار که توکنِ درست را برایِ تلگرام نامعتبر می‌کند."""
    v = value if value is not None else ""
    out = []
    if v != v.strip():
        out.append("فاصله/خطِ‌جدید در ابتدا یا انتهایِ مقدار")
    if "\n" in v or "\r" in v:
        out.append("خطِ‌جدید داخلِ مقدار")
    s = v.strip()
    if s[:1] in "'\"" or s[-1:] in "'\"":
        out.append("نقل‌قول (' یا \") دورِ مقدار")
    if s.lower().startswith("bot") and ":" in s[3:]:
        out.append("پیشوندِ «bot» اولِ توکن (توکن نباید با bot شروع شود)")
    if re.search(r"\s", s):
        out.append("فاصله داخلِ توکن")
    if not TOKEN_RE.fullmatch(s.strip("'\" ")):
        out.append("قالبِ توکن نامعتبر (باید مثلِ 123456789:AAE... باشد)")
    return out


def _load_env_file(path: str) -> dict[str, str]:
    out = {}
    try:
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def _api(base: str, secret: str, method: str, path: str, params: dict | None = None):
    url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, method=method, headers={"X-Admin-Secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def get_me(token: str) -> str:
    """جوابِ واقعیِ تلگرام: «@username» یا دلیلِ ردّ."""
    t = token.strip().strip("'\" ")
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getMe", timeout=15) as r:
            j = json.loads(r.read().decode())
            return "✅ معتبر: @" + ((j.get("result") or {}).get("username") or "?")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            d = ""
        return f"❌ تلگرام رد کرد (HTTP {e.code}) {d}"
    except Exception as e:  # noqa: BLE001
        return f"⚠️ ارتباط با تلگرام ناموفق: {type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id", nargs="?", type=int)
    ap.add_argument("--name")
    a = ap.parse_args()

    envf = _load_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    secret = os.getenv("VIP_ADMIN_SECRET") or envf.get("VIP_ADMIN_SECRET", "")
    base = os.getenv("VIP_API_BASE") or envf.get("VIP_API_BASE") or "http://127.0.0.1:8002"
    if not secret:
        print("VIP_ADMIN_SECRET در .env پیدا نشد.")
        return 2

    app_id = a.app_id
    if not app_id and a.name:
        # نام ⇒ شناسه از رویِ دیتابیسِ مشترک
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import asyncio
        from sqlalchemy import select
        from config import Session
        from models import HostedApp

        async def _find():
            async with Session() as s:
                return await s.scalar(select(HostedApp.id).where(HostedApp.name == a.name))
        app_id = asyncio.run(_find())
    if not app_id:
        print("شناسهٔ اپ لازم است (یا --name).")
        return 2

    problems = 0
    ok, env = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/env")
    if not ok:
        print("env خوانده نشد:", env)
        return 2
    print(f"══ اپ {env.get('name', app_id)} ══")
    reg_tokens: dict[str, str] = {}
    others: list[str] = []
    print("\n۱) متغیرهایِ ثبت‌شده")
    for e in env.get("env") or []:
        k, v = e.get("key", ""), e.get("value", "")
        if k.upper() in TOKEN_KEYS or TOKEN_RE.search(v or ""):
            issues = diagnose_token(v)
            print(f"  • {k}: {mask(v)} (طول {len(v)}) — {get_me(v)}")
            for i in issues:
                print(f"      ❌ {i}")
            problems += len(issues)
            reg_tokens[k] = v.strip().strip("'\" ")
        else:
            others.append(k)
    if others:
        up = [k for k in others if k == k.upper()]
        low = [k for k in others if k != k.upper()]
        print(f"  • سایرِ متغیرها: {', '.join(up) or '—'}")
        if low:
            print(f"  ℹ️ {len(low)} متغیرِ با نامِ کوچک هم ثبت شده (مثلِ {', '.join(low[:4])}) که شبیهِ نامِ "
                  "متغیرهایِ داخلِ کد است نه تنظیماتِ واقعی؛ معمولاً بی‌اثر است، ولی احتمالاً از اسکنِ خودکارِ کد آمده.")
    if env.get("needed"):
        miss = [k for k in env["needed"] if k not in {e.get('key') for e in env.get('env') or []}]
        if miss:
            print("  ⚠️ کدِ ربات این‌ها را می‌خواهد ولی ثبت نشده:", ", ".join(miss))
            problems += 1
    if env.get("only_in_file"):
        print("  ⚠️ فقط در فایلِ .env (نه دیتابیس):", ", ".join(env["only_in_file"]))
    if not reg_tokens:
        print("  ❌ هیچ متغیرِ توکنی ثبت نشده است.")
        problems += 1

    print("\n۲) فایل‌هایِ پروژه (توکنِ سخت‌کدشده / .env داخلِ zip)")
    ok, fl = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/files")
    files = (fl.get("files") if ok else None) or []
    scanned = 0
    texts: dict[str, str] = {}
    reg_set = set(reg_tokens.values())
    for f in files:
        p = f.get("path", "")
        if scanned >= MAX_SCAN_FILES or f.get("size", 0) > 300_000:
            continue
        if not (p.lower().endswith(SCAN_EXT) or os.path.basename(p).startswith(".env")):
            continue
        if any(x in p for x in ("node_modules/", "venv/", ".git/", "site-packages/")):
            continue
        scanned += 1
        ok2, d = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/filetext", {"path": p})
        if not ok2:
            continue
        txt = d.get("text") or ""
        texts[p] = txt
        for tok in set(TOKEN_RE.findall(txt)):
            same = tok in reg_set
            print(f"  • {p}: توکن {mask(tok)} — "
                  + ("همان توکنِ ثبت‌شده" if same else f"❗ با توکنِ ثبت‌شده فرق دارد — {get_me(tok)}"))
            if not same:
                problems += 1
                for ln in txt.splitlines():
                    if "token" in ln.lower() and len(ln) < 200:
                        print("      خط:", TOKEN_RE.sub(lambda m: mask(m.group(0)), ln.strip()))
                        break
    if not scanned:
        print("  (فایلِ متنیِ قابلِ اسکن پیدا نشد)")

    print("\n۳) لاگ")
    ok, lg = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/logs", {"lines": 200})
    lines = ((lg.get("logs") or "") if ok else "").splitlines()
    hits = [(i, ln) for i, ln in enumerate(lines)
            if re.search(r"unauthorized|401|conflict|terminated by other|invalid token|not found", ln, re.I)]
    for i, ln in hits[-6:]:
        print("  >", ln[:200])
        for ctx in lines[max(0, i - 2):i]:
            print("      ", ctx[:160])
    if not hits:
        print("  (خطی با Unauthorized/Conflict در ۲۰۰ خطِ آخر نبود)")
    print("  آخرین خط‌ها:")
    for ln in lines[-8:]:
        print("   ", ln[:180])

    miss_mods = []
    for ln in lines:
        m = re.search(r"No module named ['\"]([\w.]+)['\"]", ln)
        if m and m.group(1).split(".")[0] not in miss_mods:
            miss_mods.append(m.group(1).split(".")[0])
    if miss_mods:
        print("\n۴) ماژولِ گم‌شده (علتِ واقعیِ کرشِ فعلی)")
        req_path = next((p for p in texts if os.path.basename(p).lower() == "requirements.txt"), None)
        if req_path is None:
            ok3, d3 = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/filetext", {"path": "requirements.txt"})
            req_txt = (d3.get("text") or "") if ok3 else None
        else:
            req_txt = texts[req_path]
        for mod in miss_mods:
            pip = PIP_NAMES.get(mod, mod)
            if req_txt is None:
                print(f"  ❌ {mod}: فایلِ requirements.txt در پروژه نیست ⇒ هیچ بسته‌ای نصب نمی‌شود. بسته: {pip}")
            elif not req_has(req_txt, pip):
                print(f"  ❌ {mod}: «{pip}» در requirements.txt نیست ⇒ باید اضافه شود.")
            else:
                print(f"  ⚠️ {mod}: «{pip}» در requirements.txt هست ولی نصب نشده ⇒ ساختِ (build) دوباره را بزن / لاگِ بیلد را ببین.")
            problems += 1
        tg = [ln.strip() for t in texts.values() for ln in t.splitlines()
              if re.match(r"\s*(from|import)\s+telegram", ln)][:6]
        if tg and "telegram" in miss_mods:
            print("  ایمپورت‌هایِ کد (برایِ تعیینِ نسخه):")
            for ln in tg:
                print("    ", ln[:120])
            print("  راهنما: «Updater/Dispatcher» ⇒ python-telegram-bot==13.15 | «Application/ApplicationBuilder» ⇒ python-telegram-bot>=20")

    print("\n══ جمع‌بندی ══")
    print("❌ مشکلِ احتمالی بالا پیدا شد." if problems else "✅ توکنِ ثبت‌شده معتبر و بدونِ مشکلِ ظاهری است؛ علتِ Unauthorized جایِ دیگری است (لاگ را ببین).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
