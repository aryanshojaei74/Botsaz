"""
tutorial_page.py — صفحهٔ آموزش بات‌ساز به‌صورت مینی‌اپ با ذخیره‌سازی سمت سرور.

ساختار داده: هر «بخش» شامل چند «مرحله» است و هر مرحله تا ۳ عکس + توضیح دارد.
sections = [{id, title, steps: [{id, images: [..], text}]}]
(فرمت‌های قدیمی {desc,image} و {image} به‌صورت خودکار مهاجرت می‌شوند)

- GET  /tutorial            → خود صفحه (برای دکمهٔ مینی‌اپ ربات)
- GET  /tutorial/data       → داده‌های بخش‌ها (JSON) — عمومی
- POST /tutorial/data       → ذخیرهٔ بخش‌ها — فقط با توکن ویرایش

حالت ویرایش فقط وقتی فعال می‌شود که صفحه با ?edit=<TOKEN> باز شود.
توکن از env با نام TUTORIAL_EDIT_TOKEN خوانده می‌شود؛ اگر ست نشده باشد،
بار اول یک توکن تصادفی ساخته و در فایل tutorial_token.txt کنار دیتابیس
ذخیره می‌شود (توی لاگ هم چاپ می‌شود).
"""
# === PATCH_TUTORIAL_PERF_V1 ===
from __future__ import annotations

import hashlib
import base64
import re as _re
import json
import os
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

tutorial_router = APIRouter()

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_FILE = os.path.join(_BASE_DIR, "tutorial_data.json")
_TOKEN_FILE = os.path.join(_BASE_DIR, "tutorial_token.txt")

# حداکثر حجم داده (عکس‌ها به‌صورت base64 داخل JSON ذخیره می‌شوند)
_MAX_BYTES = 20 * 1024 * 1024  # 20MB


def _edit_token() -> str:
    tok = (os.getenv("TUTORIAL_EDIT_TOKEN") or "").strip()
    if tok:
        return tok
    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    tok = secrets.token_urlsafe(24)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    print(f"[tutorial] توکن ویرایش ساخته شد: {tok}  (فایل: {_TOKEN_FILE})")
    return tok


# کشِ سبکِ سمتِ سرور: تا وقتی فایل عوض نشده (بر اساسِ mtime)، دوباره از
# دیسک خونده و JSON-parse نمی‌شه — سرعتِ پاسخ‌دهی رو خیلی بالا می‌بره.
_cache_mtime: float | None = None
_cache_sections: list = []
_cache_etag: str = ""

# === PERF V2: عکس‌ها دیگر داخل JSON نیستند ===
# base64 → فایل در tutorial_imgs/ → آدرس /tutorial/img/<name>
# نتیجه: JSON چند کیلوبایتی می‌شود و متن‌ها فوری بالا می‌آیند؛ عکس‌ها lazy لود می‌شوند.
_IMG_DIR = os.path.join(_BASE_DIR, "tutorial_imgs")
_IMG_NAME_RE = _re.compile(r"^[a-f0-9]{16}\.(jpg|png|webp)$")
_IMG_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _extract_images(sections: list) -> bool:
    """عکس‌های base64 داخل JSON را به فایل تبدیل و با URL جایگزین می‌کند."""
    changed = False
    os.makedirs(_IMG_DIR, exist_ok=True)
    for s in sections:
        for st in (s.get("steps") or []):
            imgs = st.get("images")
            if not isinstance(imgs, list):
                continue
            out = []
            for im in imgs:
                if isinstance(im, str) and im.startswith("data:image"):
                    try:
                        head, b64 = im.split(",", 1)
                        ext = "png" if "png" in head else ("webp" if "webp" in head else "jpg")
                        raw = base64.b64decode(b64)
                        name = secrets.token_hex(8) + "." + ext
                        with open(os.path.join(_IMG_DIR, name), "wb") as f:
                            f.write(raw)
                        out.append(f"/tutorial/img/{name}")
                        changed = True
                    except Exception:
                        logger.exception("ذخیرهٔ تصویرِ آموزش ناموفق بود")  # عکس خراب — حذف می‌شود
                else:
                    out.append(im)
            st["images"] = out
    return changed


def _cleanup_images(sections: list) -> None:
    """فایل‌های عکسی که دیگر در هیچ مرحله‌ای استفاده نمی‌شوند را پاک می‌کند."""
    used = set()
    for s in sections:
        for st in (s.get("steps") or []):
            for im in (st.get("images") or []):
                if isinstance(im, str) and im.startswith("/tutorial/img/"):
                    used.add(im.rsplit("/", 1)[-1])
    try:
        for fn in os.listdir(_IMG_DIR):
            if _IMG_NAME_RE.match(fn) and fn not in used:
                os.remove(os.path.join(_IMG_DIR, fn))
    except FileNotFoundError:
        pass


def _write_sections(sections: list) -> None:
    tmp = _DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sections, f, ensure_ascii=False)
    os.replace(tmp, _DATA_FILE)


def _load_sections() -> list:
    global _cache_mtime, _cache_sections, _cache_etag
    if not os.path.exists(_DATA_FILE):
        _cache_mtime, _cache_sections, _cache_etag = None, [], ""
        return []
    try:
        mtime = os.path.getmtime(_DATA_FILE)
    except OSError:
        mtime = None
    if mtime is not None and mtime == _cache_mtime:
        return _cache_sections
    try:
        with open(_DATA_FILE, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        sections = data if isinstance(data, list) else []
    except Exception:
        sections = []
        raw = ""
    # مهاجرت یک‌بارهٔ دادهٔ قدیمی: اگر عکس base64 داخل JSON باشد → فایل شود
    if sections and _extract_images(sections):
        _write_sections(sections)
        raw = json.dumps(sections, ensure_ascii=False)
        try:
            mtime = os.path.getmtime(_DATA_FILE)
        except OSError:
            mtime = None
    _cache_mtime = mtime
    _cache_sections = sections
    _cache_etag = hashlib.md5(raw.encode("utf-8")).hexdigest() if raw else ""
    return sections


@tutorial_router.get("/tutorial/data")
async def tutorial_data(request: Request):
    sections = _load_sections()
    etag = _cache_etag or "empty"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(
        {"sections": sections},
        headers={"ETag": etag, "Cache-Control": "public, max-age=60, must-revalidate"},
    )


@tutorial_router.post("/tutorial/data")
async def tutorial_save(request: Request):
    body = await request.body()
    if len(body) > _MAX_BYTES:
        return JSONResponse({"ok": False, "error": "حجم داده زیاد است (عکس‌ها را کوچک‌تر کن)"},
                            status_code=413)
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if (payload.get("token") or "") != _edit_token():
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=403)
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return JSONResponse({"ok": False, "error": "bad data"}, status_code=400)
    _extract_images(sections)   # base64های جدید → فایل
    _write_sections(sections)
    _cleanup_images(sections)   # عکس‌های حذف/جایگزین‌شده → پاک
    return JSONResponse({"ok": True})


@tutorial_router.get("/tutorial/img/{name}")
async def tutorial_img(name: str):
    """سرو عکس‌های آموزش با کش طولانی مرورگر (immutable)."""
    if not _IMG_NAME_RE.match(name):
        return Response(status_code=404)
    path = os.path.join(_IMG_DIR, name)
    if not os.path.isfile(path):
        return Response(status_code=404)
    with open(path, "rb") as f:
        data = f.read()
    ext = name.rsplit(".", 1)[-1]
    return Response(
        content=data,
        media_type=_IMG_MIME.get(ext, "image/jpeg"),
        headers={"Cache-Control": "public, max-age=2592000, immutable"})


_PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>آموزش بات‌ساز</title>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link href="https://v1.fontapi.ir/css/BTitr" rel="stylesheet">
<style>
  :root{
    --bg:#faf9f6; --text:#26293a; --subtext:#5c5f70; --card:#fff;
    --border:#e8e8ee; --input-border:#d4d5dd; --muted:#f2f2f6;
    --shadow:0 2px 14px rgba(38,41,58,.07); --accent:#e08a2e;
    --titr:'B Titr','Vazirmatn',sans-serif;
  }
  body.dark{
    --bg:#17181f; --text:#eceff5; --subtext:#9ba0b0; --card:#22242e;
    --border:#33364200; --input-border:#454858; --muted:#2d3040;
    --shadow:0 2px 14px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;font-family:'Vazirmatn',sans-serif;background:var(--bg);color:var(--text);
       min-height:100vh;transition:background .25s,color .25s;font-size:14px;
       padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}

  /* ── موشن‌ها ── */
  @keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
  @keyframes slideIn{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
  @keyframes popIn{0%{opacity:0;transform:scale(.94)}70%{transform:scale(1.015)}100%{opacity:1;transform:scale(1)}}
  @keyframes barGrow{from{width:0}to{width:56px}}
  @media (prefers-reduced-motion: reduce){
    *{animation-duration:.01ms !important;transition-duration:.01ms !important}
  }

  header{max-width:900px;margin:0 auto;padding:60px 22px 26px;text-align:center;
         display:flex;flex-direction:column;align-items:center;gap:12px}
  header h1{margin:0;font-family:var(--titr);font-size:clamp(28px,8vw,38px);font-weight:700;
            letter-spacing:.2px;animation:fadeUp .5s ease both}
  header p{margin:4px 0 0;font-size:13.5px;color:var(--subtext);font-weight:500;
           animation:fadeUp .5s .08s ease both}
  .bar{height:3px;width:56px;border-radius:2px;background:var(--accent);animation:barGrow .6s .15s ease both}

  .topbtns{position:fixed;top:calc(12px + env(safe-area-inset-top));left:14px;z-index:50;display:flex;gap:7px}
  .toplinks{position:fixed;top:calc(12px + env(safe-area-inset-top));right:14px;z-index:50;display:flex;gap:7px}
  .pill{display:flex;align-items:center;gap:5px;padding:8px 13px;border-radius:999px;
        border:1px solid var(--border);background:var(--card);color:var(--text);
        font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;
        box-shadow:var(--shadow);text-decoration:none;
        transition:transform .12s ease, background .2s}
  .pill:active{transform:scale(.92)}
  .pill.active{background:var(--accent);color:#fff}

  main{max-width:720px;margin:0 auto;padding:0 16px 40px;display:flex;flex-direction:column;gap:15px}
  .empty{text-align:center;padding:44px 22px;color:var(--subtext);font-size:14px;
         border:1.5px dashed var(--input-border);border-radius:16px;background:var(--card);
         animation:fadeUp .4s ease both}

  .sec{background:var(--card);border-radius:18px;overflow:hidden;box-shadow:var(--shadow);
       border:1px solid var(--border);animation:slideIn .45s ease both;position:relative}
  .sec .inner{padding:18px 18px 18px}
  .sec .num{font-size:10.5px;font-weight:700;color:var(--accent);margin-bottom:5px}
  .sec .head{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;
             padding:0;border:none;background:none;cursor:pointer;text-align:right;font-family:inherit;
             transition:transform .12s ease}
  .sec .head:active{transform:scale(.98)}
  .sec h2{margin:0;font-family:var(--titr);font-size:clamp(19px,5.4vw,24px);font-weight:700;
          line-height:1.5;color:var(--text)}
  .chev{flex-shrink:0;width:26px;height:26px;border-radius:8px;background:var(--muted);
        display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--subtext);
        transition:transform .25s cubic-bezier(.34,1.56,.64,1)}

  .steps{margin-top:14px;display:flex;flex-direction:column;gap:14px}
  .step{border:1px solid var(--input-border);border-radius:14px;overflow:hidden;background:var(--card);
        animation:slideIn .4s ease both;transition:transform .12s ease}

  /* گالری چندعکسه: اسکرول افقی با اسنپ */
  .gal{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;
       background:var(--muted)}
  .gal::-webkit-scrollbar{display:none}
  .gal img{flex:0 0 100%;width:100%;max-height:400px;object-fit:contain;scroll-snap-align:center;display:block}
  .galwrap{position:relative}
  .galcnt{position:absolute;bottom:8px;left:10px;background:rgba(0,0,0,.55);color:#fff;
          font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:999px;pointer-events:none}

  .step .sbody{padding:12px 14px 14px}
  .step .stag{font-family:var(--titr);font-size:13px;font-weight:700;color:var(--accent);margin-bottom:5px}
  .step p{margin:0;font-size:13.5px;line-height:1.95;color:var(--subtext);white-space:pre-wrap;
          animation:fadeUp .35s .05s ease both}

  .stools{display:flex;flex-wrap:wrap;gap:6px;padding:9px 12px;border-top:1px dashed var(--input-border);
          background:var(--muted)}
  .stools button,.stools label{padding:6px 10px;border-radius:9px;border:1px solid var(--input-border);
        background:var(--card);color:var(--text);cursor:pointer;font-family:inherit;
        font-size:11.5px;font-weight:600;transition:transform .1s ease}
  .stools button:active,.stools label:active{transform:scale(.93)}
  .stools .danger{color:#c0392b}

  .edittools{position:absolute;top:10px;left:10px;display:flex;gap:6px;z-index:2}
  .edittools button,.edittools .grab{width:32px;height:32px;border-radius:10px;
        border:1px solid var(--border);background:var(--card);color:var(--text);
        cursor:pointer;font-size:14px;box-shadow:var(--shadow);
        display:flex;align-items:center;justify-content:center;transition:transform .1s ease}
  .edittools button:active{transform:scale(.9)}
  .edittools .del{color:#c0392b}
  .edittools .grab{cursor:grab;color:var(--subtext)}

  .inlineform{border:1.5px dashed var(--accent);border-radius:14px;padding:14px;margin-top:4px;
              display:flex;flex-direction:column;gap:11px;background:var(--card);animation:popIn .3s ease both}
  .inlineform textarea,.inlineform input[type=text]{padding:10px 13px;border-radius:10px;
        border:1px solid var(--input-border);background:var(--card);color:var(--text);
        font-family:inherit;font-size:13.5px;outline:none;resize:vertical;line-height:1.85;width:100%}
  .inlineform strong{font-family:var(--titr);font-size:15px;font-weight:700}
  .filebtn{padding:8px 13px;border-radius:9px;background:var(--muted);
           border:1px solid var(--border);cursor:pointer;font-size:12px;font-weight:600;
           display:inline-block;width:fit-content;transition:transform .1s ease}
  .filebtn:active{transform:scale(.93)}
  .thumbrow{display:flex;gap:8px;flex-wrap:wrap}
  .thumbbox{position:relative;animation:popIn .25s ease both}
  .thumbbox img{width:86px;height:60px;border-radius:10px;object-fit:cover;
                border:1px solid var(--input-border);display:block}
  .thumbbox button{position:absolute;top:-7px;right:-7px;width:21px;height:21px;border-radius:50%;
                   border:none;background:#c0392b;color:#fff;font-size:11px;cursor:pointer;
                   line-height:1;display:flex;align-items:center;justify-content:center}
  .frow{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
  .primary{padding:10px 17px;border-radius:10px;border:none;background:var(--accent);
           color:#fff;font-family:inherit;font-size:13.5px;font-weight:700;cursor:pointer;
           transition:transform .12s ease}
  .primary:active{transform:scale(.94)}
  .ghost{padding:10px 15px;border-radius:10px;border:1px solid var(--input-border);
         background:var(--card);color:var(--text);font-family:inherit;font-size:13.5px;
         font-weight:600;cursor:pointer;transition:transform .12s ease}
  .ghost:active{transform:scale(.94)}
  .addstep{width:100%;padding:11px;border-radius:12px;border:1.5px dashed var(--input-border);
           background:none;color:var(--subtext);font-family:inherit;font-size:13px;
           font-weight:600;cursor:pointer;margin-top:11px;transition:transform .12s ease}
  .addstep:active{transform:scale(.97)}

  .form{background:var(--card);border-radius:18px;padding:20px;box-shadow:var(--shadow);
        border:1.5px solid rgba(224,138,46,.4);margin-top:6px;animation:fadeUp .4s ease both}
  .form h3{margin:0 0 12px;font-family:var(--titr);font-size:16px;font-weight:700}
  footer{text-align:center;padding:26px 22px 46px;font-size:12px;color:var(--subtext)}
  .toast{position:fixed;bottom:calc(22px + env(safe-area-inset-bottom));right:50%;
         transform:translateX(50%) translateY(8px);background:var(--text);
         color:var(--bg);padding:9px 19px;border-radius:999px;font-size:12.5px;font-weight:600;
         opacity:0;transition:opacity .3s, transform .3s;pointer-events:none;z-index:99}
  .toast.show{opacity:1;transform:translateX(50%) translateY(0)}

  @media (max-width:430px){
    main{padding:0 12px 36px}
    .sec .inner{padding:15px 14px}
    header{padding:54px 18px 22px}
  }
</style>
</head>
<body>
  <div class="topbtns">
    <button id="editToggle" class="pill" style="display:none">ویرایش صفحه</button>
    <button id="darkToggle" class="pill" title="حالت شب/روز">🌙</button>
  </div>
  <div class="toplinks">
    <a class="pill" href="https://t.me/BaBotSazBot" target="_blank" rel="noopener">🤖 ربات</a>
    <a class="pill" href="https://t.me/BaBotSaz" target="_blank" rel="noopener">📢 کانال</a>
  </div>

  <header>
    <h1>بات‌ساز</h1>
    <p>آموزش گام‌به‌گام ساخت و راه‌اندازی ربات تلگرام</p>
    <div class="bar"></div>
  </header>

  <main id="list"></main>
  <footer>ساخته شده توسط بات‌ساز</footer>
  <div id="toast" class="toast"></div>

<script>
(function(){
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg){ tg.ready(); tg.expand(); }
  function haptic(kind){
    try{ tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(kind||'light'); }catch(e){}
  }

  var params = new URLSearchParams(location.search);
  var EDIT_TOKEN = params.get('edit') || '';
  var canEdit = !!EDIT_TOKEN;
  var MAX_IMGS = 3;

  var sections = [];
  var editMode = false, dragIndex = null;
  var expanded = {};
  var ui = {addingStep:null, editStep:null, renaming:null, pendingImgs:[]};

  var listEl = document.getElementById('list');
  var toastEl = document.getElementById('toast');
  var editBtn = document.getElementById('editToggle');
  var darkBtn = document.getElementById('darkToggle');

  try{ if(localStorage.getItem('batsaz_dark')==='1'){document.body.classList.add('dark');darkBtn.textContent='☀️';} }catch(e){}
  darkBtn.onclick = function(){
    haptic('light');
    var d = document.body.classList.toggle('dark');
    darkBtn.textContent = d ? '☀️' : '🌙';
    try{ localStorage.setItem('batsaz_dark', d?'1':'0'); }catch(e){}
  };

  function toast(msg){
    toastEl.textContent = msg; toastEl.classList.add('show');
    setTimeout(function(){ toastEl.classList.remove('show'); }, 2200);
  }
  function fa(n){
    var m=['۰','۱','۲','۳','۴','۵','۶','۷','۸','۹'];
    return String(n).replace(/[0-9]/g,function(c){return m[+c];});
  }
  function esc(s){
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function uid(){ return Date.now()+'-'+Math.random().toString(36).slice(2,8); }

  // مهاجرت فرمت‌های قدیمی به images:[]
  function migrate(list){
    return (list||[]).map(function(s){
      var steps;
      if (!Array.isArray(s.steps)){
        steps = [];
        if ((s.desc||'').trim() || s.image)
          steps.push({id: uid(), images: s.image?[s.image]:[], text: s.desc||''});
      } else {
        steps = s.steps.map(function(st){
          var imgs = Array.isArray(st.images) ? st.images : (st.image ? [st.image] : []);
          return {id: st.id||uid(), images: imgs.slice(0, MAX_IMGS), text: st.text||''};
        });
      }
      return {id: s.id||uid(), title: s.title||'', steps: steps};
    });
  }

  // رندر فوری از آخرین نسخهٔ کش‌شده (JSON سبک است؛ عکس‌ها URL هستند)
  try{
    var snap = localStorage.getItem('batsaz_tut');
    if(snap){ sections = migrate(JSON.parse(snap)); render(); }
  }catch(e){}
  fetch('/tutorial/data').then(function(r){return r.json();}).then(function(d){
    sections = migrate((d && Array.isArray(d.sections)) ? d.sections : []);
    try{ localStorage.setItem('batsaz_tut', JSON.stringify(sections)); }catch(e){}
    render();
  }).catch(function(){ render(); });

  var saveTimer = null;
  function persist(){
    render();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function(){
      fetch('/tutorial/data', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({token: EDIT_TOKEN, sections: sections})
      }).then(function(r){return r.json();}).then(function(d){
        toast(d.ok ? '✅ ذخیره شد' : ('❌ ' + (d.error||'خطا در ذخیره')));
      }).catch(function(){ toast('❌ خطا در اتصال به سرور'); });
    }, 400);
  }

  if (canEdit){
    editBtn.style.display = 'flex';
    editBtn.onclick = function(){
      haptic('medium');
      editMode = !editMode;
      editBtn.textContent = editMode ? 'پایان ویرایش' : 'ویرایش صفحه';
      editBtn.classList.toggle('active', editMode);
      ui = {addingStep:null, editStep:null, renaming:null, pendingImgs:[]};
      render();
    };
  }

  function compressImage(file, cb){
    var reader = new FileReader();
    reader.onload = function(){
      var img = new Image();
      img.onload = function(){
        var MAX = 1100;
        var w = img.width, h = img.height;
        if (w > MAX){ h = h*MAX/w; w = MAX; }
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(img, 0, 0, w, h);
        cb(c.toDataURL('image/jpeg', 0.82));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  }

  function sec(id){ return sections.find(function(x){return x.id===id;}); }
  function step(sid, tid){
    var s = sec(sid); if(!s) return null;
    return (s.steps||[]).find(function(x){return x.id===tid;});
  }

  // ─── سراسری‌ها ───
  window._toggle = function(id){ haptic('light'); expanded[id] = !expanded[id]; render(); };

  window._addSec = function(){
    var t = document.getElementById('newSecTitle').value.trim();
    if(!t) return;
    haptic('medium');
    sections.push({id: uid(), title: t, steps: []});
    persist();
  };
  window._delSec = function(id){
    if(!confirm('کل این بخش با همهٔ مرحله‌هایش حذف شود؟')) return;
    sections = sections.filter(function(x){return x.id!==id;});
    persist();
  };
  window._renSec = function(id){ ui.renaming = id; render();
    var el=document.getElementById('renInp'); if(el){el.focus();} };
  window._renSave = function(id){
    var v = document.getElementById('renInp').value.trim();
    var s = sec(id);
    if(s && v) s.title = v;
    ui.renaming = null;
    persist();
  };
  window._ds = function(i){ dragIndex = i; };
  window._dov = function(e){ e.preventDefault(); };
  window._drop = function(e, i){
    e.preventDefault();
    if(dragIndex===null || dragIndex===i) return;
    var m = sections.splice(dragIndex,1)[0];
    sections.splice(i,0,m);
    dragIndex = null;
    persist();
  };

  // مرحله‌ها
  window._addStepForm = function(sid){
    haptic('light');
    ui.addingStep = sid; ui.editStep=null; ui.pendingImgs=[]; expanded[sid]=true; render();
  };
  window._formImgPick = function(input){
    var files = Array.prototype.slice.call(input.files||[]);
    if(!files.length) return;
    files.slice(0, MAX_IMGS - ui.pendingImgs.length).forEach(function(f){
      compressImage(f, function(d){
        if(ui.pendingImgs.length < MAX_IMGS){ ui.pendingImgs.push(d); render(); }
      });
    });
  };
  window._formImgDel = function(i){ ui.pendingImgs.splice(i,1); render(); };
  window._addStepSave = function(sid){
    var t = document.getElementById('stepText').value.trim();
    if(!t && !ui.pendingImgs.length) return;
    var s = sec(sid); if(!s) return;
    haptic('medium');
    s.steps = s.steps || [];
    s.steps.push({id: uid(), images: ui.pendingImgs.slice(), text: t});
    ui.addingStep = null; ui.pendingImgs=[];
    persist();
  };
  window._editStepForm = function(sid, tid){
    var st = step(sid, tid); if(!st) return;
    haptic('light');
    ui.editStep = {sec:sid, step:tid}; ui.addingStep=null;
    ui.pendingImgs = (st.images||[]).slice();
    render();
    document.getElementById('stepText').value = st.text||'';
  };
  window._editStepSave = function(){
    if(!ui.editStep) return;
    var st = step(ui.editStep.sec, ui.editStep.step); if(!st) return;
    haptic('medium');
    st.text = document.getElementById('stepText').value.trim();
    st.images = ui.pendingImgs.slice(0, MAX_IMGS);
    ui.editStep = null; ui.pendingImgs=[];
    persist();
  };
  window._cancelForm = function(){ ui.addingStep=null; ui.editStep=null; ui.pendingImgs=[]; render(); };
  window._delStep = function(sid, tid){
    if(!confirm('این مرحله حذف شود؟')) return;
    var s = sec(sid); if(!s) return;
    s.steps = (s.steps||[]).filter(function(x){return x.id!==tid;});
    persist();
  };
  // افزودن/حذف عکس مستقیم روی مرحله
  window._stepImgAdd = function(input, sid, tid){
    var st = step(sid, tid); if(!st) return;
    var files = Array.prototype.slice.call(input.files||[]);
    st.images = st.images || [];
    files.slice(0, MAX_IMGS - st.images.length).forEach(function(f){
      compressImage(f, function(d){
        if(st.images.length < MAX_IMGS){ st.images.push(d); persist(); }
      });
    });
  };
  window._stepImgDel = function(sid, tid, idx){
    if(!confirm('این عکس حذف شود؟')) return;
    var st = step(sid, tid); if(!st || !st.images) return;
    st.images.splice(idx,1);
    persist();
  };
  window._stepMove = function(sid, tid, dir){
    var s = sec(sid); if(!s || !s.steps) return;
    var i = s.steps.findIndex(function(x){return x.id===tid;});
    var j = i + dir;
    if(i<0 || j<0 || j>=s.steps.length) return;
    haptic('light');
    var m = s.steps.splice(i,1)[0];
    s.steps.splice(j,0,m);
    persist();
  };

  function galleryHTML(imgs){
    if(!imgs || !imgs.length) return '';
    var lz = ' loading="lazy" decoding="async"';
    if(imgs.length === 1) return '<img class="gal-single" src="'+imgs[0]+'" alt=""'+lz
      + ' style="width:100%;display:block;max-height:400px;object-fit:contain;background:var(--muted)">';
    var h = '<div class="galwrap"><div class="gal">';
    imgs.forEach(function(im){ h += '<img src="'+im+'" alt=""'+lz+'>'; });
    h += '</div><span class="galcnt">'+fa(imgs.length)+' عکس · بکشید ◂</span></div>';
    return h;
  }

  function stepFormHTML(sid, saveFn, title){
    var h = '<div class="inlineform">'
      + '<strong>'+title+'</strong>'
      + '<div class="thumbrow">';
    ui.pendingImgs.forEach(function(im, i){
      h += '<div class="thumbbox"><img src="'+im+'">'
        +  '<button onclick="_formImgDel('+i+')">✕</button></div>';
    });
    h += '</div><div class="frow">';
    if(ui.pendingImgs.length < MAX_IMGS){
      h += '<label class="filebtn">🖼 افزودن عکس ('+fa(ui.pendingImgs.length)+'/'+fa(MAX_IMGS)+')'
        +  '<input type="file" accept="image/*" multiple onchange="_formImgPick(this)" style="display:none"></label>';
    }
    h += '</div>'
      + '<textarea id="stepText" rows="4" placeholder="توضیح این مرحله را بنویس..."></textarea>'
      + '<div class="frow">'
      + '<button class="primary" onclick="'+saveFn+'">ثبت مرحله</button>'
      + '<button class="ghost" onclick="_cancelForm()">انصراف</button>'
      + '</div></div>';
    return h;
  }

  function render(){
    var h = '';
    if(!sections.length){
      h += '<div class="empty">' + (editMode
        ? 'هنوز بخشی اضافه نشده. اولین بخش را از فرم پایین بساز.'
        : 'به‌زودی آموزش‌ها اینجا قرار می‌گیرند…') + '</div>';
    }
    sections.forEach(function(s,i){
      var ex = !!expanded[s.id];
      var steps = s.steps || [];
      h += '<div class="sec" style="animation-delay:'+Math.min(i*60,300)+'ms"' + (editMode
            ? ' draggable="true" ondragstart="_ds('+i+')" ondragover="_dov(event)" ondrop="_drop(event,'+i+')"'
            : '') + '>';
      if(editMode){
        h += '<div class="edittools">'
          +  '<button onclick="_renSec(\''+s.id+'\')" title="تغییر تیتر">✎</button>'
          +  '<button class="del" onclick="_delSec(\''+s.id+'\')" title="حذف بخش">✕</button>'
          +  '<span class="grab" title="جابه‌جایی">⠿</span>'
          +  '</div>';
      }
      h += '<div class="inner">'
        +  '<div class="num">بخش '+fa(i+1)+' · '+fa(steps.length)+' مرحله</div>';
      if(editMode && ui.renaming===s.id){
        h += '<div class="frow"><input type="text" id="renInp" value="'+esc(s.title)+'" '
          +  'style="flex:1;padding:10px 13px;border-radius:10px;border:1px solid var(--input-border);'
          +  'background:var(--card);color:var(--text);font-family:inherit;font-size:14px">'
          +  '<button class="primary" onclick="_renSave(\''+s.id+'\')">ثبت</button></div>';
      } else {
        h += '<button class="head" onclick="_toggle(\''+s.id+'\')">'
          +  '<h2>'+esc(s.title)+'</h2>'
          +  '<span class="chev" style="transform:rotate('+(ex?'-90deg':'0deg')+')">‹</span>'
          +  '</button>';
      }
      if(ex){
        h += '<div class="steps">';
        steps.forEach(function(st, k){
          var isEditing = ui.editStep && ui.editStep.sec===s.id && ui.editStep.step===st.id;
          if(isEditing){
            h += stepFormHTML(s.id, '_editStepSave()', '✏️ ویرایش مرحلهٔ '+fa(k+1));
            return;
          }
          h += '<div class="step" style="animation-delay:'+Math.min(k*70,350)+'ms">';
          h += galleryHTML(st.images);
          h += '<div class="sbody">'
            +  '<div class="stag">مرحله '+fa(k+1)+'</div>'
            +  '<p>'+esc(st.text)+'</p></div>';
          if(editMode){
            var imgs = st.images || [];
            h += '<div class="stools">'
              +  '<button onclick="_editStepForm(\''+s.id+'\',\''+st.id+'\')">✏️ ویرایش</button>';
            if(imgs.length < MAX_IMGS){
              h += '<label>🖼 افزودن عکس ('+fa(imgs.length)+'/'+fa(MAX_IMGS)+')'
                +  '<input type="file" accept="image/*" multiple style="display:none" '
                +  'onchange="_stepImgAdd(this,\''+s.id+'\',\''+st.id+'\')"></label>';
            }
            imgs.forEach(function(_, ix){
              h += '<button class="danger" onclick="_stepImgDel(\''+s.id+'\',\''+st.id+'\','+ix+')">🗑 عکس '+fa(ix+1)+'</button>';
            });
            h += (k>0 ? '<button onclick="_stepMove(\''+s.id+'\',\''+st.id+'\',-1)">⬆️</button>' : '')
              +  (k<steps.length-1 ? '<button onclick="_stepMove(\''+s.id+'\',\''+st.id+'\',1)">⬇️</button>' : '')
              +  '<button class="danger" onclick="_delStep(\''+s.id+'\',\''+st.id+'\')">✕ حذف مرحله</button>'
              +  '</div>';
          }
          h += '</div>';
        });
        if(editMode){
          if(ui.addingStep===s.id){
            h += stepFormHTML(s.id, '_addStepSave(\''+s.id+'\')', '➕ مرحلهٔ جدید');
          } else {
            h += '<button class="addstep" onclick="_addStepForm(\''+s.id+'\')">➕ افزودن مرحله</button>';
          }
        }
        if(!steps.length && !editMode){
          h += '<p style="color:var(--subtext);font-size:13px;margin:6px 0 0">هنوز مرحله‌ای ثبت نشده.</p>';
        }
        h += '</div>';
      }
      h += '</div></div>';
    });
    if(editMode && !ui.addingStep && !ui.editStep){
      h += '<div class="form"><h3>➕ افزودن بخش جدید</h3>'
        +  '<div class="frow">'
        +  '<input type="text" id="newSecTitle" placeholder="تیتر بخش، مثلا: ساخت ربات فروش VPN" '
        +  'style="flex:1;padding:10px 13px;border-radius:10px;border:1px solid var(--input-border);'
        +  'background:var(--card);color:var(--text);font-family:inherit;font-size:13.5px">'
        +  '<button class="primary" onclick="_addSec()">ساخت بخش</button>'
        +  '</div>'
        +  '<p style="font-size:12px;color:var(--subtext);margin:9px 0 0">'
        +  'بعد از ساخت بخش، بازش کن و با «➕ افزودن مرحله» برای هر مرحله تا '+fa(MAX_IMGS)+' عکس و توضیح بگذار.</p>'
        +  '</div>';
    }
    listEl.innerHTML = h;
  }
})();
</script>
</body>
</html>"""


@tutorial_router.get("/tutorial")
async def tutorial_page():
    _edit_token()  # اطمینان از ساخت توکن در اولین اجرا
    return HTMLResponse(_PAGE)
