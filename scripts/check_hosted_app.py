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
  ۴) ماژول گم‌شده (No module named …) در لاگ و مقایسه با requirements.txt.
  ۷) --fix-media [--apply] [--restart] [--verify] (PATCH_HOSTED_MEDIA_FIX_V1): اصلاح همان مشکل ۶ — requirements.txt
     (moviepy==1.0.3 + imageio-ffmpeg) و در صورت نیاز یک خط شیم Pillow در کد مشتری؛ بدون --apply فقط پیش‌نمایش.
     با --restart اپ ری‌استارت می‌شود و با --verify داخل کانتینر آزمون واقعی moviepy/ffmpeg اجرا می‌شود.
  ۶) ffmpeg / moviepy (PATCH_HOSTED_MEDIA_CHECK_V1): «این سرویس فعال نیست (ffmpeg یا moviepy نصب نیست)» با وجود
     افزودن moviepy به requirements — علت رایج: moviepy ۲ دیگر moviepy.editor ندارد، یا برنامهٔ ffmpeg در ایمیج نیست.

اجرا از /opt/botsaz (شناسهٔ اپ همان id در «کاربران و ربات‌ها»):

    venv/bin/python scripts/check_hosted_app.py 17
    venv/bin/python scripts/check_hosted_app.py --name pyhdbkm
    venv/bin/python scripts/check_hosted_app.py --owner 8757601463   # نام اپ را نمی‌دانی: تلگرام‌آیدی مالک (از تیکت)
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
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


# === PATCH_HOSTED_MEDIA_CHECK_V1 ===
def req_spec(requirements: str, pip_name: str):
    """(بسته در requirements هست؟، مشخصهٔ نسخه مثل '==1.0.3' یا '' برای بدون پین)."""
    want = _norm_pkg(pip_name)
    for ln in (requirements or "").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln or ln.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", ln)
        if m and _norm_pkg(m.group(1)) == want:
            return True, re.sub(r"\s+", "", m.group(2).split(";", 1)[0])
    return False, ""


def moviepy_is_v2(spec: str) -> bool:
    """با این مشخصه pip کدام نسخهٔ moviepy را نصب می‌کند: ۲.x (که moviepy.editor را ندارد) یا ۱.x؟
    بدون پین یا فقط کف نسخه (>=) ⇒ آخرین نسخه = ۲.x."""
    spec = (spec or "").lower()
    m = re.search(r"(?:==|~=|<=|<)\s*(\d+)(?:\.(\d+))?", spec)
    if m:
        major = int(m.group(1))
        if "<" in spec and "<=" not in spec and "==" not in spec:      # <2 ، <2.0 ⇒ ۱.x
            return major > 2 or (major == 2 and int(m.group(2) or 0) > 0)
        return major >= 2
    return True


def diagnose_media_deps(texts: dict, req_txt, dockerfile, log_lines) -> list:
    """وابستگی‌های ویدیو/صدا (moviepy، ffmpeg) در برابر کد پروژه، requirements.txt، Dockerfile و لاگ.
    خروجی: فهرست (نشانه، پیام) با نشانه‌های ❌ مشکل قطعی، ⚠️ احتمال، ℹ️ توضیح."""
    out: list = []
    py = {p: t for p, t in (texts or {}).items() if p.lower().endswith(".py")}
    code = "\n".join(py.values())
    uses_editor = bool(re.search(r"^\s*(?:from|import)\s+moviepy\.editor\b", code, re.M))
    uses_moviepy = bool(re.search(r"^\s*(?:from|import)\s+moviepy\b", code, re.M))
    uses_ffmpeg_bin = bool(re.search(r"(?:subprocess\.\w+\([^)]*['\"]ffmpeg['\"]|shutil\.which\(\s*['\"]ffmpeg|"
                                     r"^\s*(?:from|import)\s+ffmpeg\b|ffmpeg-python|\bffmpeg\s+-i\b)", code, re.M | re.S))
    log_hit = [ln for ln in (log_lines or []) if re.search(r"moviepy|ffmpeg|imageio", ln, re.I)]
    if not (uses_moviepy or uses_ffmpeg_bin or log_hit):
        return out
    has_mp, mp_spec = req_spec(req_txt or "", "moviepy") if req_txt is not None else (False, "")
    has_iio, _ = req_spec(req_txt or "", "imageio-ffmpeg") if req_txt is not None else (False, "")
    docker_ffmpeg = bool(dockerfile and re.search(r"\bffmpeg\b", dockerfile, re.I))
    if uses_moviepy:
        if req_txt is None:
            out.append(("❌", "requirements.txt در پروژه نیست ⇒ moviepy اصلا نصب نمی‌شود."))
        elif not has_mp:
            out.append(("❌", "کد moviepy را ایمپورت می‌کند ولی «moviepy» در requirements.txt نیست."))
        elif uses_editor and moviepy_is_v2(mp_spec):
            out.append(("❌", f"کد «from moviepy.editor import …» دارد ولی requirements «moviepy{mp_spec or ' (بدون پین)'}» است؛ "
                              "moviepy ۲.x ماژول moviepy.editor را حذف کرده ⇒ ImportError ⇒ پیام «moviepy نصب نیست». "
                              "راه‌حل: «moviepy==1.0.3» بگذار، یا ایمپورت را به «from moviepy import VideoFileClip» عوض کن."))
        elif has_mp and not uses_editor and not moviepy_is_v2(mp_spec):
            out.append(("ℹ️", f"moviepy{mp_spec} (نسخهٔ ۱.x) پین شده؛ اگر کد تازه‌تر می‌خواهی ایمپورت‌ها را با ۲.x یکی کن."))
    if uses_ffmpeg_bin and not (docker_ffmpeg or has_iio or has_mp):
        out.append(("❌", "کد برنامهٔ ffmpeg را صدا می‌زند ولی نه در Dockerfile نصب شده (apt-get install -y ffmpeg) و نه "
                          "«imageio-ffmpeg» در requirements است."))
    if (uses_moviepy or uses_ffmpeg_bin) and dockerfile is None:
        out.append(("ℹ️", "Dockerfile در پروژه نیست ⇒ (اپ از نوع python) ایمیج را سرور میزبانی با قالب خودش می‌سازد و فقط "
                          "بسته‌های pip نصب می‌شوند؛ ffmpeg سیستمی در آن نیست مگر از راه «imageio-ffmpeg» (باینری ffmpeg داخل خود بسته است) "
                          "یا اینکه مشتری Dockerfile بگذارد. داخل کانتینر چک کن: "
                          "«docker exec <کانتینر> ffmpeg -version» و «… python -c 'import moviepy; print(moviepy.__version__)'»."))
    elif (uses_moviepy or uses_ffmpeg_bin) and not docker_ffmpeg and not has_iio:
        out.append(("⚠️", "در Dockerfile اسمی از ffmpeg نیست و «imageio-ffmpeg» هم در requirements نیست؛ moviepy ۱.x/۲.x معمولا "
                          "خودش imageio-ffmpeg را (با باینری ffmpeg) نصب می‌کند، ولی اگر بیلد بدون اینترنت/با کش قدیمی بوده "
                          "نصب نشده. «imageio-ffmpeg» را صریح اضافه کن یا ffmpeg را در Dockerfile بنویس."))
    if (uses_moviepy or uses_ffmpeg_bin):
        out.append(("ℹ️", "بعد از تغییر requirements/Dockerfile ایمیج باید دوباره ساخته (rebuild) شود؛ اگر ری‌استارت ایمیج را دوباره "
                          "نسازد بسته‌های تازه نصب نمی‌شوند. با «--verify» بعد از ری‌استارت، نسخهٔ واقعی نصب‌شده داخل کانتینر سنجیده می‌شود."))
    # moviepy 1.0.3 در resize از Image.ANTIALIAS استفاده می‌کند که Pillow>=10 آن را حذف کرده ⇒ AttributeError
    if uses_moviepy and re.search(r"\.resize\(|\bresize\(|vfx\.resize|fx\(\s*resize", code) \
            and not re.search(r"Image\.ANTIALIAS\s*=", code) and (uses_editor or not moviepy_is_v2(mp_spec)):
        out.append(("⚠️", "کد از resize استفاده می‌کند؛ با moviepy 1.0.3 و Pillow>=10 خطای «module PIL.Image has no attribute ANTIALIAS» "
                          "می‌دهد. اگر بعد از پین‌کردن moviepy این خطا آمد، بالای همان فایل بنویس: "
                          "«import PIL.Image; PIL.Image.ANTIALIAS = PIL.Image.LANCZOS»."))
    shown = 0
    for pth, txt in py.items():                                  # خط‌های کد که moviepy/resize/crop/subclip را صدا می‌زنند
        for i, ln in enumerate(txt.splitlines(), 1):
            if shown < 8 and re.search(r"moviepy|\.resize\(|\.crop\(|\.subclip\(|ffmpeg", ln):
                out.append(("ℹ️", f"کد {pth}:{i}: {ln.strip()[:140]}"))
                shown += 1
    for ln in log_hit[-4:]:
        out.append(("ℹ️", "لاگ: " + ln.strip()[:170]))
    return out


# === PATCH_HOSTED_MEDIA_FIX_V1 ===
SHIM_MARK = "botsaz-moviepy-shim"
SHIM_LINES = (
    f"import PIL.Image as _PILImage  # {SHIM_MARK}: moviepy 1.0.3 + Pillow>=10 (Image.ANTIALIAS)",
    "if not hasattr(_PILImage, 'ANTIALIAS'):",
    "    _PILImage.ANTIALIAS = _PILImage.LANCZOS",
)
_EDITOR_IMPORT = re.compile(r"^([ \t]*)(?:from|import)[ \t]+moviepy\.editor\b", re.M)
_RESIZE_USE = re.compile(r"\.resize\(|\bresize\(|vfx\.resize|fx\(\s*resize")

# آزمون واقعی داخل کانتینر: نسخه‌ها، ایمپورت moviepy.editor، باینری ffmpeg، نوشتن/خواندن یک ویدیوی ۱ ثانیه‌ای،
# و crop + resize (همان کاری که ربات می‌کند)؛ شیم Pillow خود آزمون هم هست تا فقط وابستگی‌ها سنجیده شود.
SMOKE = """\
import os, tempfile
import PIL.Image as _I
if not hasattr(_I, "ANTIALIAS"):
    _I.ANTIALIAS = _I.LANCZOS
import PIL, numpy, moviepy
from moviepy.editor import ColorClip, VideoFileClip
from moviepy.config import get_setting
c = ColorClip((64, 64), color=(255, 0, 0), duration=1)
note = ""
try:
    p = os.path.join(tempfile.mkdtemp(), "t.mp4")
    c.write_videofile(p, fps=5, logger=None)
    c = VideoFileClip(p).subclip(0, 1)
except OSError as e:
    if e.errno not in (13, 30):
        raise
    note = " (write-skipped: %s)" % e
f = c.crop(x_center=32, y_center=32, width=32, height=32).resize(newsize=(24, 24)).get_frame(0)
print("OK moviepy", moviepy.__version__, "numpy", numpy.__version__, "pillow", PIL.__version__,
      "frame", f.shape, "ffmpeg", get_setting("FFMPEG_BINARY") + note)
"""


def media_usage(py_texts: dict) -> tuple:
    """(کد moviepy را ایمپورت می‌کند؟، از moviepy.editor (سبک ۱.x)؟، از resize استفاده می‌کند؟، شیم ANTIALIAS دارد؟)"""
    code = "\n".join(t for pth, t in (py_texts or {}).items() if pth.lower().endswith(".py"))
    uses = bool(re.search(r"^\s*(?:from|import)\s+moviepy\b", code, re.M))
    editor = bool(re.search(r"^\s*(?:from|import)\s+moviepy\.editor\b", code, re.M))
    shim = SHIM_MARK in code or bool(re.search(r"ANTIALIAS\s*=", code))
    return uses, editor, bool(_RESIZE_USE.search(code)), shim


def plan_requirements_fix(req_txt, uses_editor: bool):
    """requirements.txt تازه (یا None اگر تغییری لازم نیست) + یادداشت‌ها.
    کد سبک ۱.x (moviepy.editor) ⇒ moviepy==1.0.3؛ کد سبک ۲.x ⇒ نسخهٔ moviepy دست‌نخورده. imageio-ffmpeg همیشه."""
    if req_txt is None:
        return None, ["requirements.txt در پروژه نیست؛ دست نمی‌زنم (بدون آن هیچ بسته‌ای نصب نمی‌شود؛ باید ساخته شود)."]
    out, notes, seen_mp, seen_iio = [], [], False, False
    for ln in req_txt.splitlines():
        core = ln.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)", core) if core and not core.startswith("-") else None
        name = _norm_pkg(m.group(1)) if m else ""
        if name == "moviepy":
            seen_mp = True
            spec = req_spec(ln, "moviepy")[1]
            if uses_editor and moviepy_is_v2(spec):
                out.append("moviepy==1.0.3")
                notes.append(f"moviepy{spec or ' (بدون پین)'} ⇒ moviepy==1.0.3")
                continue
        elif name == "imageio-ffmpeg":
            seen_iio = True
        out.append(ln)
    if not seen_mp:
        out.append("moviepy==1.0.3" if uses_editor else "moviepy")
        notes.append("moviepy اضافه شد")
    if not seen_iio:
        out.append("imageio-ffmpeg")
        notes.append("imageio-ffmpeg اضافه شد (باینری ffmpeg داخل خود بسته است)")
    if not notes:
        return None, ["requirements.txt همین حالا درست است."]
    return "\n".join(out).rstrip("\n") + "\n", notes


def plan_shim_patch(text: str):
    """سه خط شیم Pillow را دقیقا قبل از اولین «from moviepy.editor import» (با همان تورفتگی) می‌گذارد.
    None اگر شیم هست، ایمپورتی نیست، یا نتیجه کامپایل نشود."""
    if SHIM_MARK in text or re.search(r"ANTIALIAS\s*=", text):
        return None
    m = _EDITOR_IMPORT.search(text)
    if not m:
        return None
    new = text[:m.start()] + "".join(m.group(1) + ln + "\n" for ln in SHIM_LINES) + text[m.start():]
    try:
        compile(new, "<patched>", "exec")
    except SyntaxError:
        return None
    return new


def size_matches(text: str, size) -> bool:
    """متنی که API داده کامل فایل است؟ (اختلاف بایت فقط به‌اندازهٔ CRLF→LF مجاز است)؛ جلوی نوشتن فایل نیمه‌کاره."""
    if not isinstance(size, int):
        return False
    return abs(size - len(text.encode("utf-8"))) <= text.count("\n")


def short_diff(old: str, new: str, limit: int = 40) -> list:
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "قبل", "بعد", lineterm="", n=1))
    return d[:limit] + ([f"… ({len(d) - limit} خط دیگر)"] if len(d) > limit else [])


def backup_originals(app_name: str, files: dict) -> str:
    root = os.environ.get("BOTSAZ_BACKUP_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosted_backups")
    d = os.path.join(root, re.sub(r"[^A-Za-z0-9_.-]", "_", app_name or "app"), time.strftime("%Y%m%d-%H%M%S"))
    for pth, txt in files.items():
        parts = [x for x in pth.replace("\\", "/").split("/") if x not in ("", ".", "..")]
        dest = os.path.join(d, *parts) if parts else os.path.join(d, "file")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="") as fh:
            fh.write(txt)
    return d


def _post_form(base: str, secret: str, path: str, fields: dict, timeout: int = 60):
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"X-Admin-Secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except ValueError:
                return True, raw
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def write_verified(base: str, secret: str, app_id: int, path: str, old: str, new: str):
    """فایل را می‌نویسد، دوباره می‌خواند و اگر با چیز فرستاده‌شده نمی‌خواند نسخهٔ اصلی را برمی‌گرداند."""
    ep = f"/api/hosting/admin/{app_id}/filetext"
    ok, res = _post_form(base, secret, ep, {"path": path, "text": new})
    if not ok:
        return False, f"نوشتن ناموفق: {res}"
    ok2, d = _api(base, secret, "GET", ep, {"path": path})
    got = d.get("text") if ok2 and isinstance(d, dict) else None
    if got is None or got.replace("\r\n", "\n") != new.replace("\r\n", "\n"):
        ok3, res3 = _post_form(base, secret, ep, {"path": path, "text": old})
        return False, ("بعد از نوشتن، محتوا با چیز فرستاده‌شده نمی‌خواند؛ "
                       + ("نسخهٔ اصلی برگردانده شد." if ok3 else f"⚠️ برگرداندن نسخهٔ اصلی هم ناموفق بود: {res3}"))
    return True, ""


def _docker(args: list, input_text=None, timeout: int = 90):
    try:
        r = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return 127, "", "docker روی این سرور پیدا نشد"
    except subprocess.TimeoutExpired:
        return 124, "", "مهلت اجرا تمام شد"


def find_container(app_name: str):
    """(نام کانتینر در حال اجرا یا None، فهرست نامزدها، خطا)."""
    code, out, err = _docker(["ps", "--format", "{{.Names}}"], timeout=20)
    if code != 0:
        return None, [], (err or out).strip()
    names = [n.strip() for n in out.splitlines() if n.strip()]
    low = (app_name or "").lower()
    exact = [n for n in names if n.lower() == low]
    if len(exact) == 1:
        return exact[0], exact, ""
    cands = [n for n in names if low and low in n.lower()]
    return (cands[0] if len(cands) == 1 else None), cands, ""


def verify_in_container(app_name: str, wait: int = 0, poll: int = 10):
    """آزمون SMOKE داخل کانتینر اپ. با wait>0 تا آن‌قدر ثانیه هر poll ثانیه تکرار می‌شود (بعد از ری‌استارت ممکن است
    ایمیج تازه ساخته شود). خروجی: (موفق؟، متن)."""
    deadline = time.monotonic() + max(0, wait)
    last = "کانتینر پیدا نشد"
    while True:
        name, cands, err = find_container(app_name)
        if name:
            for py in ("python", "python3"):
                code, out, e = _docker(["exec", "-i", name, py, "-"], input_text=SMOKE, timeout=180)
                if code in (126, 127) or "executable file not found" in e:
                    continue
                if code == 0 and "OK moviepy" in out:
                    return True, f"[{name}] " + out.strip().splitlines()[-1]
                tail = [ln for ln in (e or out).strip().splitlines() if ln.strip()][-3:]
                last = f"[{name}] " + " | ".join(tail)[:400]
                break
        else:
            last = ("کانتینر اپ پیدا نشد" + (f" (نامزدها: {', '.join(cands)})" if cands else "")
                    + (f" — {err}" if err else ""))
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id", nargs="?", type=int)
    ap.add_argument("--name")
    ap.add_argument("--owner", type=int,
                    help="تلگرام‌آیدی مالک اپ (مثلا از تیکت)؛ اگر نام اپ را نمی‌دانی. چند اپ داشت فهرست می‌دهد")
    ap.add_argument("--fix-media", action="store_true",
                    help="requirements.txt (moviepy==1.0.3 + imageio-ffmpeg) و در صورت نیاز شیم Pillow را درست می‌کند؛ "
                         "بدون --apply فقط پیش‌نمایش")
    ap.add_argument("--apply", action="store_true", help="با --fix-media: واقعا بنویس (نسخهٔ اصلی پشتیبان می‌شود)")
    ap.add_argument("--restart", action="store_true", help="با --apply: بعد از نوشتن اپ را ری‌استارت کن")
    ap.add_argument("--verify", action="store_true",
                    help="داخل کانتینر اپ آزمون واقعی moviepy/ffmpeg را اجرا کن (docker exec)")
    ap.add_argument("--wait", type=int, default=150,
                    help="با --verify بعد از --restart: حداکثر چند ثانیه منتظر آماده‌شدن کانتینر بماند (پیش‌فرض ۱۵۰)")
    ap.add_argument("--fix-token", action="store_true",
                    help="اگر BOT_TOKENِ ثبت‌شده را تلگرام رد می‌کند و در فایل‌هایِ پروژه دقیقاً یک توکنِ دیگرِ "
                         "معتبر هست، همان را در متغیرِ BOT_TOKENِ پنل می‌گذارد (تنها حالتی که چیزی را عوض می‌کند)")
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
    if not app_id and a.owner:
        # === PATCH_HOSTED_MEDIA_CHECK_V1 === مالک ⇒ اپ(ها) از روی دیتابیس مشترک
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import asyncio
        from sqlalchemy import select
        from config import Session
        from models import HostedApp

        async def _by_owner():
            async with Session() as s:
                return (await s.execute(select(HostedApp.id, HostedApp.name, HostedApp.status, HostedApp.runtime)
                                        .where(HostedApp.owner_id == a.owner).order_by(HostedApp.id))).all()
        owned = asyncio.run(_by_owner())
        if not owned:
            print(f"برای تلگرام‌آیدی {a.owner} اپی ثبت نشده.")
            return 2
        if len(owned) > 1:
            print(f"این کاربر {len(owned)} اپ دارد؛ یکی را با شناسه انتخاب کن:")
            for oid, oname, ost, ort in owned:
                print(f"  • id={oid}  نام={oname}  وضعیت={ost}  نوع={ort}")
            print("مثال:  venv/bin/python scripts/check_hosted_app.py", owned[0][0])
            return 2
        app_id = owned[0][0]
        print(f"اپ این کاربر: id={app_id}  نام={owned[0][1]}  وضعیت={owned[0][2]}  نوع={owned[0][3]}")
    if not app_id:
        print("شناسهٔ اپ لازم است (یا --name یا --owner).")
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
    no_env_token = not reg_tokens      # PATCH_HOSTED_MEDIA_FIX_V1 — تصمیمش بعد از اسکن فایل‌ها (توکن پیش‌فرض داخل کد)

    print("\n۲) فایل‌هایِ پروژه (توکنِ سخت‌کدشده / .env داخلِ zip)")
    ok, fl = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/files")
    files = (fl.get("files") if ok else None) or []
    scanned = 0
    texts: dict[str, str] = {}
    alt_tokens: set[str] = set()
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
                  + ("همان توکن ثبت‌شده" if same else
                     (f"❗ با توکن ثبت‌شده فرق دارد — {get_me(tok)}" if reg_set else f"در پنل توکنی ثبت نشده — {get_me(tok)}")))
            if not same:
                alt_tokens.add(tok)
                if reg_set:
                    problems += 1
                for ln in txt.splitlines():
                    if "token" in ln.lower() and len(ln) < 200:
                        print("      خط:", TOKEN_RE.sub(lambda m: mask(m.group(0)), ln.strip()))
                        break
    if not scanned:
        print("  (فایلِ متنیِ قابلِ اسکن پیدا نشد)")
    if no_env_token:
        if any(get_me(t).startswith("✅") for t in alt_tokens):
            print("  ℹ️ در پنل متغیر توکن ثبت نشده، ولی کد خودش توکن معتبری به‌عنوان پیش‌فرض دارد ⇒ مشکل نیست.")
        else:
            print("  ❌ هیچ توکن معتبری نیست (نه در پنل، نه داخل کد).")
            problems += 1

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

    # === PATCH_HOSTED_MEDIA_CHECK_V1 ===
    def _fetch(path: str):
        okf, df = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/filetext", {"path": path})
        return (df.get("text") or "") if okf else None
    names = {f.get("path", ""): f for f in files}
    py_texts = dict(texts)
    for pth, f in names.items():                              # فایل‌های پایتون بزرگ‌تر/بیشتر از سقف اسکن هم برای ایمپورت‌ها
        if pth.lower().endswith(".py") and pth not in py_texts and len(py_texts) < 80 and f.get("size", 0) <= 500_000 \
                and not any(x in pth for x in ("venv/", ".git/", "site-packages/", "node_modules/")):
            t = _fetch(pth)
            if t:
                py_texts[pth] = t
    req_t = next((pth for pth in names if os.path.basename(pth).lower() == "requirements.txt"), None)
    dock_t = next((pth for pth in names if os.path.basename(pth).lower() == "dockerfile"), None)
    req_txt = _fetch(req_t) if req_t else None
    media = diagnose_media_deps(py_texts, req_txt, _fetch(dock_t) if dock_t else None, lines)
    if media:
        print("\n۶) ffmpeg / moviepy (ویدیو ← ویدیو مسیج و …)")
        for mark, msg_ in media:
            print(f"  {mark} {msg_}")
            if mark == "❌":
                problems += 1

    # === PATCH_HOSTED_MEDIA_FIX_V1 ===
    app_name = env.get("name") or str(app_id)
    restarted = False
    if a.fix_media:
        print("\n۷) اصلاح ffmpeg / moviepy (--fix-media" + (" --apply" if a.apply else " — فقط پیش‌نمایش") + ")")
        uses, editor, resize_used, shim_present = media_usage(py_texts)
        changes: dict[str, tuple[str, str]] = {}
        if not uses:
            print("  ℹ️ کد پروژه moviepy را ایمپورت نمی‌کند؛ چیزی برای اصلاح نیست.")
        else:
            if req_t and not size_matches(req_txt or "", names.get(req_t, {}).get("size")):
                print(f"  ⚠️ {req_t}: اندازهٔ متن دریافتی با اندازهٔ فایل نمی‌خواند (ممکن است ناقص باشد)؛ دست نمی‌زنم — "
                      "دستی اصلاح کن: " + ("moviepy==1.0.3 و " if editor else "") + "imageio-ffmpeg")
            elif req_t:
                new_req, notes = plan_requirements_fix(req_txt, editor)
                for n in notes:
                    print("  •", n)
                if new_req is not None:
                    changes[req_t] = (req_txt, new_req)
            else:
                print("  •", plan_requirements_fix(None, editor)[1][0])
            if editor and resize_used and not shim_present:
                for pth, txt in py_texts.items():
                    if len([c for c in changes if c.lower().endswith(".py")]) >= 3:
                        break
                    if not (pth.lower().endswith(".py") and _EDITOR_IMPORT.search(txt)):
                        continue
                    if not size_matches(txt, names.get(pth, {}).get("size")):
                        print(f"  ⚠️ {pth}: اندازهٔ متن دریافتی با اندازهٔ فایل نمی‌خواند (ممکن است ناقص باشد)؛ دست نمی‌زنم — "
                              "شیم را دستی بالای فایل بگذار.")
                        continue
                    new_txt = plan_shim_patch(txt)
                    if new_txt is None:
                        print(f"  ⚠️ {pth}: شیم را نتوانستم با اطمینان اضافه کنم (کامپایل نشد)؛ دستی بگذار.")
                        continue
                    changes[pth] = (txt, new_txt)
                    print(f"  • {pth}: شیم Pillow (۳ خط) قبل از ایمپورت moviepy.editor اضافه می‌شود")
            elif editor and resize_used and shim_present:
                print("  ℹ️ شیم ANTIALIAS از قبل در کد هست.")
        for pth, (old, new) in changes.items():
            print(f"\n  ── {pth}")
            for dl in short_diff(old, new):
                print("    " + dl)
        if not changes:
            print("  ✅ چیزی برای نوشتن نیست.")
        elif not a.apply:
            print("\n  (پیش‌نمایش؛ چیزی نوشته نشد" + (" و --restart بدون --apply اثری ندارد" if a.restart else "")
                  + ".) برای اعمال: همین دستور + --apply --restart --verify")
        else:
            bdir = backup_originals(app_name, {pth: old for pth, (old, _n) in changes.items()})
            print(f"\n  💾 نسخهٔ اصلی در {bdir} ذخیره شد.")
            all_ok = True
            for pth, (old, new) in changes.items():
                ok_w, why = write_verified(base, secret, app_id, pth, old, new)
                print(f"  {'✅' if ok_w else '❌'} {pth}" + ("" if ok_w else f" — {why}"))
                all_ok = all_ok and ok_w
            if not all_ok:
                problems += 1
            elif a.restart:
                ok_r, res_r = _post_form(base, secret, f"/api/hosting/admin/{app_id}/action", {"action": "restart"})
                print("  ♻️ ری‌استارت درخواست شد." if ok_r else f"  ❌ ری‌استارت ناموفق: {res_r}")
                restarted = ok_r
                problems += 0 if ok_r else 1
            else:
                print("  ♻️ برای اعمال، اپ را ری‌استارت کن (یا همین دستور را با --restart بزن).")
    if a.verify:
        print("\n۸) آزمون واقعی داخل کانتینر (--verify)")
        okv, msgv = verify_in_container(app_name, wait=(a.wait if restarted else 0))
        print(("  ✅ " if okv else "  ❌ ") + msgv)
        if not okv:
            print("  راهنما: «No module named 'moviepy.editor'» ⇒ هنوز moviepy 2.x نصب است (requirements اعمال نشده؛ ری‌استارت ایمیج را "
              "دوباره نساخته ⇒ از مینی‌اپ یا آپلود دوباره، build بزن). «No module named 'moviepy'» ⇒ اصلا نصب نیست یا ایمیج قدیمی است.")
            problems += 1

    if a.fix_token:
        print("\n۵) اصلاحِ توکنِ پنل (--fix-token)")
        key = "BOT_TOKEN" if "BOT_TOKEN" in reg_tokens else (next(iter(reg_tokens), None))
        cur = reg_tokens.get(key, "") if key else ""
        if not key:
            print("  ⛔️ متغیرِ توکن در پنل نیست؛ چیزی عوض نشد.")
        elif get_me(cur).startswith("✅"):
            print("  ✅ توکنِ ثبت‌شده معتبر است؛ نیازی به تغییر نیست.")
        else:
            good = [t for t in sorted(alt_tokens) if get_me(t).startswith("✅")]
            if len(good) != 1:
                print(f"  ⛔️ توکنِ جایگزینِ معتبرِ یکتا پیدا نشد ({len(good)} مورد)؛ دستی عوض کن.")
            else:
                new_env = {e["key"]: e["value"] for e in env.get("env") or []}
                new_env[key] = good[0]
                body = urllib.parse.urlencode({"payload": json.dumps(new_env)}).encode()
                req = urllib.request.Request(f"{base}/api/hosting/admin/{app_id}/env", data=body, method="POST",
                                             headers={"X-Admin-Secret": secret})
                try:
                    urllib.request.urlopen(req, timeout=30).read()
                    ok4, chk = _api(base, secret, "GET", f"/api/hosting/admin/{app_id}/env")
                    now_v = next((e["value"] for e in (chk.get("env") or []) if e["key"] == key), "") if ok4 else ""
                    print(f"  ✅ {key}: {mask(cur)} ⟵ {mask(now_v)} "
                          + ("(تأیید شد)" if now_v == good[0] else "⚠️ بعد از ذخیره تأیید نشد!"))
                    print("  ♻️ حالا اپ را از پنل «روشن/راه‌اندازی دوباره» کن.")
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ ذخیره ناموفق: {type(e).__name__}: {e}")
                    problems += 1

    print("\n══ جمع‌بندی ══")
    print("❌ مشکلِ احتمالی بالا پیدا شد." if problems else "✅ توکنِ ثبت‌شده معتبر و بدونِ مشکلِ ظاهری است؛ علتِ Unauthorized جایِ دیگری است (لاگ را ببین).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
