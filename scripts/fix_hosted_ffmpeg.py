#!/usr/bin/env python3
"""
scripts/fix_hosted_ffmpeg.py — اصلاح اپ‌های میزبانی‌شده‌ای که ffmpeg را با subprocess صدا می‌زنند ولی در ایمیج ffmpeg نیست
(=== PATCH_HOSTED_FFMPEG_SUBPROCESS_V1 ===).

ایمیج اپ‌های python را سرور میزبانی از یک قالب ثابت می‌سازد (Dockerfile مشتری نادیده/بازنویسی می‌شود) و ffmpeg سیستمی ندارد؛
بستهٔ pip با نام «ffmpeg» هم فایل اجرایی نصب نمی‌کند. راه‌حل بدون دست‌زدن به Dockerfile: بستهٔ «imageio-ffmpeg» که باینری ffmpeg را
داخل خودش دارد، و در کد به‌جای "ffmpeg" مسیر آن را بدهیم:

  • requirements.txt: خط «ffmpeg» برداشته می‌شود و «imageio-ffmpeg» اضافه می‌شود.
  • کد پایتون: ["ffmpeg", ...] ⇒ [FFMPEG, ...] و بالای فایل (بعد از import) تعریف FFMPEG (اگر نشد، همان "ffmpeg").

اجرا از /opt/botsaz (بدون --apply فقط پیش‌نمایش؛ نسخهٔ اصلی پشتیبان می‌شود):

    venv/bin/python scripts/fix_hosted_ffmpeg.py 209
    venv/bin/python scripts/fix_hosted_ffmpeg.py 209 --apply --restart --verify
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_hosted_app as C  # noqa: E402

MARK = "BOTSAZ_FFMPEG_FIX"
_CMD_RE = re.compile(r"""(\[\s*)(["'])ffmpeg\2(\s*,)""")
_SHELL_RE = re.compile(r"""(?:run|call|Popen|check_output|check_call|system)\(\s*[fr]?(["'])ffmpeg\s""")
_IMPORT_LINE = re.compile(r"^(?:import|from)\s+\S.*$", re.M)
HEADER = (
    f"try:  # {MARK}: ffmpeg همراه بستهٔ imageio-ffmpeg (در ایمیج میزبانی ffmpeg سیستمی نیست)\n"
    "    import imageio_ffmpeg as _iio_ffmpeg\n"
    "    FFMPEG = _iio_ffmpeg.get_ffmpeg_exe()\n"
    "except Exception:\n"
    '    FFMPEG = "ffmpeg"\n'
)
SMOKE = """\
import subprocess, imageio_ffmpeg
p = imageio_ffmpeg.get_ffmpeg_exe()
out = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=30)
print("OK ffmpeg", out.stdout.splitlines()[0] if out.stdout else out.returncode, p)
"""


def plan_code_fix(text: str):
    """(متن تازه یا None، یادداشت‌ها). None یعنی چیزی برای تغییر نیست یا نتیجه کامپایل نشد."""
    if MARK in text:
        return None, ["این فایل از قبل اصلاح شده است."]
    new, n = _CMD_RE.subn(r"\1FFMPEG\3", text)
    notes = []
    if _SHELL_RE.search(text):
        notes.append("⚠️ فرمان ffmpeg به‌صورت رشته (shell) هم هست؛ آن را دست نمی‌زنم (دستی به FFMPEG عوض شود).")
    if not n:
        return None, notes + ["فرمانی به شکل [\"ffmpeg\", ...] نبود."]
    mi = next((m for m in _IMPORT_LINE.finditer(new) if m.group(0).startswith("import subprocess")), None) \
        or _IMPORT_LINE.search(new)
    if not mi:
        return None, notes + ["جای مناسبی برای تعریف FFMPEG (import) پیدا نشد."]
    pos = mi.end() + 1 if new[mi.end():mi.end() + 1] == "\n" else mi.end()
    new = new[:pos] + HEADER + new[pos:]
    try:
        compile(new, "<patched>", "exec")
    except SyntaxError as e:
        return None, notes + [f"نتیجه کامپایل نشد ({e.msg}، خط {e.lineno})؛ دست نزدم."]
    return new, notes + [f"{n} فرمان ffmpeg ⇒ FFMPEG"]


def plan_requirements(req_txt):
    if req_txt is None:
        return None, ["requirements.txt در پروژه نیست؛ باید ساخته شود (بدون آن بسته‌ای نصب نمی‌شود)."]
    out, notes, seen = [], [], False
    for ln in req_txt.splitlines():
        core = ln.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)", core) if core and not core.startswith("-") else None
        name = C._norm_pkg(m.group(1)) if m else ""
        if name == "ffmpeg":
            notes.append("خط «ffmpeg» برداشته شد (فایل اجرایی نصب نمی‌کند)")
            continue
        if name == "imageio-ffmpeg":
            seen = True
        out.append(ln)
    if not seen:
        out.append("imageio-ffmpeg")
        notes.append("imageio-ffmpeg اضافه شد")
    if not notes:
        return None, ["requirements.txt همین حالا درست است."]
    return "\n".join(out).rstrip("\n") + "\n", notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--wait", type=int, default=240)
    a = ap.parse_args()
    envf = C._load_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    secret = os.getenv("VIP_ADMIN_SECRET") or envf.get("VIP_ADMIN_SECRET", "")
    base = os.getenv("VIP_API_BASE") or envf.get("VIP_API_BASE") or "http://127.0.0.1:8002"
    if not secret:
        print("VIP_ADMIN_SECRET در .env پیدا نشد.")
        return 2
    ok, env = C._api(base, secret, "GET", f"/api/hosting/admin/{a.app_id}/env")
    if not ok:
        print("env خوانده نشد:", env)
        return 2
    app_name = env.get("name") or str(a.app_id)
    print(f"══ اپ {app_name} ══")
    ok, fl = C._api(base, secret, "GET", f"/api/hosting/admin/{a.app_id}/files")
    names = {f.get("path", ""): f for f in ((fl.get("files") if ok else None) or [])}

    def fetch(p):
        okf, d = C._api(base, secret, "GET", f"/api/hosting/admin/{a.app_id}/filetext", {"path": p})
        return (d.get("text") or "") if okf else None

    changes: dict[str, tuple[str, str]] = {}
    problems = 0
    for p, f in names.items():
        if not p.lower().endswith(".py") or f.get("size", 0) > 500_000 \
                or any(x in p for x in ("venv/", ".git/", "site-packages/", "node_modules/")):
            continue
        t = fetch(p)
        if not t or not (_CMD_RE.search(t) or _SHELL_RE.search(t)):
            continue
        if not C.size_matches(t, f.get("size")):
            print(f"⚠️ {p}: متن دریافتی با اندازهٔ فایل نمی‌خواند (ممکن است ناقص باشد)؛ دست نمی‌زنم.")
            problems += 1
            continue
        new, notes = plan_code_fix(t)
        for n in notes:
            print(f"  • {p}: {n}")
        if new is not None:
            changes[p] = (t, new)
    req_p = next((p for p in names if os.path.basename(p).lower() == "requirements.txt"), None)
    req_t = fetch(req_p) if req_p else None
    if req_p and not C.size_matches(req_t or "", names[req_p].get("size")):
        print(f"⚠️ {req_p}: متن دریافتی ناقص به نظر می‌رسد؛ دست نمی‌زنم.")
        problems += 1
    else:
        new_req, notes = plan_requirements(req_t)
        for n in notes:
            print(f"  • requirements.txt: {n}")
        if new_req is not None and req_p:
            changes[req_p] = (req_t, new_req)
    if not any(p.lower().endswith(".py") for p in changes):
        print("چیزی در کد برای اصلاح پیدا نشد.")
    for p, (old, new) in changes.items():
        print(f"\n── {p}")
        for dl in C.short_diff(old, new):
            print("  " + dl)
    restarted = False
    if not changes:
        print("\n✅ چیزی برای نوشتن نیست.")
    elif not a.apply:
        print("\n(پیش‌نمایش؛ چیزی نوشته نشد.) برای اعمال: همین دستور + --apply --restart --verify")
    else:
        bdir = C.backup_originals(app_name, {p: old for p, (old, _n) in changes.items()})
        print(f"\n💾 نسخهٔ اصلی در {bdir} ذخیره شد.")
        all_ok = True
        for p, (old, new) in changes.items():
            ok_w, why = C.write_verified(base, secret, a.app_id, p, old, new)
            print(f"{'✅' if ok_w else '❌'} {p}" + ("" if ok_w else f" — {why}"))
            all_ok = all_ok and ok_w
        if not all_ok:
            problems += 1
        elif a.restart:
            ok_r, res_r = C._post_form(base, secret, f"/api/hosting/admin/{a.app_id}/action", {"action": "restart"})
            print("♻️ ری‌استارت درخواست شد." if ok_r else f"❌ ری‌استارت ناموفق: {res_r}")
            restarted = ok_r
            problems += 0 if ok_r else 1
        else:
            print("♻️ برای اعمال، اپ را ری‌استارت کن (یا همین دستور را با --restart بزن).")
    if a.verify:
        print("\nآزمون واقعی داخل کانتینر:")
        deadline = time.monotonic() + (a.wait if restarted else 0)
        okv, msg = False, "کانتینر پیدا نشد"
        while True:
            name, cands, err = C.find_container(app_name)
            if name:
                code, out, e = C._docker(["exec", "-i", name, "python", "-"], input_text=SMOKE, timeout=120)
                if code == 0 and "OK ffmpeg" in out:
                    okv, msg = True, f"[{name}] " + out.strip().splitlines()[-1]
                    break
                msg = f"[{name}] " + " | ".join([x for x in (e or out).strip().splitlines() if x.strip()][-2:])[:300]
            if time.monotonic() >= deadline:
                break
            time.sleep(10)
        print(("  ✅ " if okv else "  ❌ ") + msg)
        if not okv:
            print("  راهنما: «No module named 'imageio_ffmpeg'» ⇒ بسته هنوز نصب نشده (ایمیج دوباره ساخته نشده؛ build بزن).")
            problems += 1
    print("\n══ جمع‌بندی ══")
    print("❌ مشکل/هشدار بالا را ببین." if problems else "✅ تمام.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
