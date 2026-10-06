#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
getapi — مستنداتِ API هر سایت را خودکار پیدا و در «یک فایلِ متنی» ذخیره می‌کند.

استفاده:
    getapi https://docs.swapwallet.app/
    getapi doc.tonpays.online
    getapi https://example.com/docs --out /opt/botsaz/api --max-pages 60

چه می‌کند (به ترتیب):
  ۱) صفحهٔ اصلی را می‌گیرد و دنبالِ آدرسِ فایلِ spec می‌گردد: Scalar، Swagger UI، Redoc، RapiDoc،
     <link rel="api-definition">، spec/‌url/‌data-url داخلِ HTML، و رشته‌هایِ *.json/*.yaml.
  ۲) فایل‌هایِ JavaScriptِ همان سایت را هم می‌خواند (صفحه‌هایِ SPA آدرس را داخلِ باندل دارند).
  ۳) مسیرهایِ رایج را امتحان می‌کند (openapi.json، swagger.json، v3/api-docs، …).
  ۴) هر کاندیدا را اعتبارسنجی می‌کند (OpenAPI 2/3 یا Postman) و فهرستِ مسیرها را می‌سازد.
  ۵) اگر spec نبود (مستنداتِ HTML ساده)، صفحه‌هایِ همان دامنه را تا حدِ مجاز می‌خزد و متنشان را برمی‌دارد.
  ۶) همه را در یک فایل می‌نویسد:  <out>/<host>/<host>__<تاریخ>.txt   و مسیرش را چاپ می‌کند.

فقط می‌خواند (GET)؛ چیزی را تغییر نمی‌دهد و هیچ کلیدی نمی‌فرستد. فقط کتابخانهٔ استاندارد (PyYAML اختیاری).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import html as _html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque

try:  # اختیاری؛ بدونِ آن YAML به‌صورتِ متنِ خام ذخیره می‌شود و فهرستِ مسیرها با regex ساخته می‌شود
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 getapi/1.0")
TIMEOUT = 25
MAX_BYTES = 30 * 1024 * 1024
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api")

COMMON_SPEC_PATHS = [
    "/openapi.json", "/openapi.yaml", "/openapi.yml", "/openapi/openapi.yaml", "/openapi/openapi.json",
    "/swagger.json", "/swagger.yaml", "/swagger/v1/swagger.json", "/swagger/v2/swagger.json",
    "/v3/api-docs", "/v2/api-docs", "/api-docs", "/api-docs.json", "/api/openapi.json", "/api/swagger.json",
    "/api/docs.json", "/api/v1/openapi.json", "/docs/openapi.json", "/docs/swagger.json", "/docs.json",
    "/spec.json", "/spec.yaml", "/api.json", "/api.yaml", "/openapi/v1.json", "/redoc/openapi.json",
    "/.well-known/openapi.json", "/static/openapi.json", "/assets/openapi.json",
]

_SKIP_JSON = re.compile(r"(package(-lock)?|manifest|tsconfig|composer|asset-manifest|browserconfig|"
                        r"site\.webmanifest|locales?/|i18n|translations?|\.min\.)", re.I)


# ───────────────────────── شبکه ─────────────────────────

def fetch(url: str, timeout: int = TIMEOUT, retries: int = 2) -> tuple[int, dict, bytes]:
    """(status, headers, body). خطایِ شبکه ⇒ (0, {}, b"")."""
    last = (0, {}, b"")
    for _ in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "fa,en;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raw = raw[:MAX_BYTES]
                enc = (r.headers.get("Content-Encoding") or "").lower()
                try:
                    if enc == "gzip":
                        raw = gzip.decompress(raw)
                    elif enc == "deflate":
                        raw = zlib.decompress(raw)
                except Exception:  # noqa: BLE001
                    pass
                return r.status, dict(r.headers), raw
        except urllib.error.HTTPError as e:
            last = (e.code, dict(e.headers or {}), b"")
            if e.code in (404, 403, 401, 410):
                return last
        except Exception as e:  # noqa: BLE001
            last = (0, {"error": f"{type(e).__name__}: {str(e)[:120]}"}, b"")
    return last


def decode(body: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1256", "latin-1"):
        try:
            return body.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return body.decode("utf-8", "replace")


# ───────────────────────── کشفِ آدرسِ spec ─────────────────────────

_STR_SPEC = re.compile(r"""["'`]((?:https?:)?//[^"'`\s<>]+?\.(?:json|ya?ml)(?:\?[^"'`\s<>]*)?|"""
                       r"""/?[A-Za-z0-9_\-./~%@]+\.(?:json|ya?ml)(?:\?[^"'`\s<>]*)?)["'`]""")
_ATTR_SPEC = re.compile(r"""(?:spec-?url|data-url|data-spec-?url|specUrl|openapi-?url|definition-?url|swagger-?url)\s*[=:]\s*["']([^"']+)["']""", re.I)
_URL_KEY = re.compile(r"""(?<![A-Za-z0-9_])["']?url["']?\s*:\s*["']([^"']+)["']""")
_LINK_REL = re.compile(r"""<link[^>]+rel=["'](?:api-definition|service-desc|alternate)["'][^>]*>""", re.I)
_HREF = re.compile(r"""href=["']([^"']+)["']""", re.I)
_WORDY = re.compile(r"""["'`](/?[A-Za-z0-9_\-./~%]*(?:openapi|swagger|api-docs|apidocs|api_docs)[A-Za-z0-9_\-./~%]*)["'`]""", re.I)
_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)


def _norm_cand(base: str, ref: str) -> str | None:
    ref = _html.unescape(ref.strip())
    if not ref or ref.startswith(("data:", "javascript:", "#", "mailto:")):
        return None
    full = urllib.parse.urljoin(base, ref)
    if not full.startswith(("http://", "https://")):
        return None
    # «?v=» + Date.now() و مشابه: کوئریِ ناقص را هم یک‌بار بدونِ کوئری امتحان می‌کنیم (در validate)
    return full


def discover_from_text(text: str, base: str) -> list[str]:
    out: list[str] = []
    for rx in (_ATTR_SPEC, _URL_KEY, _STR_SPEC, _WORDY):
        for m in rx.finditer(text):
            c = _norm_cand(base, m.group(1))
            if c and not _SKIP_JSON.search(c):
                out.append(c)
    for m in _LINK_REL.finditer(text):
        h = _HREF.search(m.group(0))
        if h:
            c = _norm_cand(base, h.group(1))
            if c:
                out.append(c)
    return out


def same_site(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    ra = ".".join(pa.netloc.split(".")[-2:])
    rb = ".".join(pb.netloc.split(".")[-2:])
    return ra == rb


def dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def parse_spec(txt: str):
    """(dict|None, kind). kind: openapi | swagger2 | postman | ''."""
    s = txt.lstrip()
    data = None
    if s[:1] in "{[":
        try:
            data = json.loads(txt)
        except Exception:  # noqa: BLE001
            data = None
    if data is None and yaml is not None and re.match(r"(?m)^(openapi|swagger)\s*:", s):
        try:
            data = yaml.safe_load(txt)
        except Exception:  # noqa: BLE001
            data = None
    if isinstance(data, dict):
        if "openapi" in data and "paths" in data:
            return data, "openapi"
        if "swagger" in data and "paths" in data:
            return data, "swagger2"
        if "paths" in data and isinstance(data["paths"], dict):
            return data, "openapi"
        info = data.get("info") or {}
        if isinstance(info, dict) and "collection" in str(info.get("schema", "")) and "item" in data:
            return data, "postman"
    if data is None and re.match(r"(?m)^(openapi|swagger)\s*:", s) and re.search(r"(?m)^paths\s*:", txt):
        return {"_raw_yaml": True}, "openapi"       # PyYAML نیست؛ متنِ خام معتبر است
    return None, ""


def validate_candidate(url: str, log) -> tuple[str, str, object, str] | None:
    """(url_نهایی, متن, ساختار, نوع) یا None."""
    tries = [url]
    if "?" in url:
        tries.append(url.split("?", 1)[0])
    for u in tries:
        st, _h, body = fetch(u, timeout=40)
        if st != 200 or len(body) < 80:
            continue
        txt = decode(body)
        data, kind = parse_spec(txt)
        if kind:
            log(f"  ✔ spec معتبر ({kind}): {u}  [{len(body):,} بایت]")
            return u, txt, data, kind
    return None


def find_inline_specs(page: str) -> list[tuple[str, object, str]]:
    """specِ JSON که مستقیم داخلِ HTML گذاشته شده (Swagger/Redoc inline)."""
    res = []
    for m in re.finditer(r"""<script[^>]*type=["']application/(?:json|ld\+json)["'][^>]*>(.*?)</script>""", page, re.S | re.I):
        data, kind = parse_spec(m.group(1))
        if kind:
            res.append((m.group(1), data, kind))
    return res


def find_specs(base: str, log) -> tuple[list[tuple[str, str, object, str]], str, list[str]]:
    """(specها، متنِ صفحهٔ اصلی، لیستِ اسکریپت‌ها)."""
    st, _h, body = fetch(base)
    page = decode(body) if st == 200 else ""
    if st != 200:
        log(f"⚠️ صفحهٔ اصلی HTTP {st or 'خطای شبکه'} داد ({_h.get('error', '')})")
    log(f"صفحهٔ اصلی: HTTP {st}، {len(body):,} بایت")
    direct: list[tuple[str, str, object, str]] = []
    if page:
        d0, k0 = parse_spec(page)          # خودِ آدرس یک فایلِ spec است؟
        if k0:
            log(f"  ✔ آدرسِ ورودی خودش spec است ({k0})")
            direct.append((base, page, d0, k0))

    cands: list[str] = discover_from_text(page, base)
    scripts = [urllib.parse.urljoin(base, x) for x in _SCRIPT_SRC.findall(page)]
    scripts = [s for s in dedupe(scripts) if s.startswith("http") and same_site(s, base)]
    # باندل‌هایِ JS (SPA): آدرسِ spec اغلب آن‌جاست
    for s in scripts[:15]:
        sst, _sh, sb = fetch(s, timeout=40, retries=1)
        if sst == 200 and sb and len(sb) < 6 * 1024 * 1024:
            found = discover_from_text(decode(sb), s)
            if found:
                log(f"  اسکریپت {s.rsplit('/', 1)[-1][:40]}: {len(found)} کاندیدا")
            # فقط کاندیداهایی که هم‌سایت‌اند یا واضحاً spec‌ مانند هستند
            cands += [c for c in found if same_site(c, base) or re.search(r"(openapi|swagger|api-docs)", c, re.I)]

    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    dirpath = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
    for p in COMMON_SPEC_PATHS:
        cands.append(root + p)
        if dirpath != root + "/":
            cands.append(urllib.parse.urljoin(dirpath, p.lstrip("/")))

    cands = [c for c in dedupe(cands) if not _SKIP_JSON.search(c)]
    log(f"کاندیدایِ spec: {len(cands)} مورد؛ اعتبارسنجی…")
    specs: list[tuple[str, str, object, str]] = list(direct)
    seen_text: set[int] = {hash(x[1]) for x in direct}
    for c in cands:
        r = validate_candidate(c, log)
        if r and hash(r[1]) not in seen_text:
            seen_text.add(hash(r[1]))
            specs.append(r)
        if len(specs) >= 4:
            break
    for txt, data, kind in find_inline_specs(page):
        if hash(txt) not in seen_text:
            specs.append(("(inline در صفحه)", txt, data, kind))
            log(f"  ✔ spec درونِ صفحه ({kind})")
    return specs, page, scripts


# ───────────────────────── قالب‌بندی ─────────────────────────

def endpoints_of(data, kind: str, raw: str) -> list[str]:
    lines: list[str] = []
    if kind in ("openapi", "swagger2") and isinstance(data, dict) and data.get("paths"):
        for p, v in data["paths"].items():
            if not isinstance(v, dict):
                continue
            for m, o in v.items():
                if m.lower() in ("get", "post", "put", "delete", "patch", "head", "options") and isinstance(o, dict):
                    params = [x.get("name") for x in (o.get("parameters") or []) if isinstance(x, dict) and x.get("name")]
                    summ = (o.get("summary") or o.get("operationId") or (o.get("description") or "").split("\n")[0])[:100]
                    lines.append(f"{m.upper():7} {p}" + (f"  ?{','.join(params[:8])}" if params else "") + (f" — {summ}" if summ else ""))
    elif kind == "postman" and isinstance(data, dict):
        def walk(items, pre=""):
            for it in items or []:
                if "item" in it:
                    walk(it["item"], pre + (it.get("name") or "") + " / ")
                else:
                    rq = it.get("request") or {}
                    url = rq.get("url")
                    raw_url = url.get("raw") if isinstance(url, dict) else url
                    lines.append(f"{(rq.get('method') or 'GET'):7} {raw_url} — {pre}{it.get('name', '')}"[:200])
        walk(data.get("item"))
    elif raw:  # بدونِ PyYAML
        cur = None
        for ln in raw.splitlines():
            m = re.match(r"^  (/[^\s:]*):\s*$", ln)
            if m:
                cur = m.group(1)
                continue
            m2 = re.match(r"^    (get|post|put|delete|patch):\s*$", ln)
            if m2 and cur:
                lines.append(f"{m2.group(1).upper():7} {cur}")
    return lines


def to_text(raw_html: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", "", raw_html)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|pre|section|table)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def crawl_text(base: str, first_page: str, max_pages: int, log) -> list[tuple[str, str]]:
    """مستنداتِ HTML ساده: صفحه‌هایِ همان دامنه (زیرِ همان مسیر) را می‌خزد."""
    pb = urllib.parse.urlparse(base)
    prefix = pb.path.rsplit("/", 1)[0] if "." in pb.path.rsplit("/", 1)[-1] else pb.path.rstrip("/")
    seen = {base.split("#")[0]}
    q = deque([(base, first_page, 0)])
    pages: list[tuple[str, str]] = []
    while q and len(pages) < max_pages:
        url, page, depth = q.popleft()
        if not page:
            st, _h, b = fetch(url)
            page = decode(b) if st == 200 else ""
        if not page:
            continue
        txt = to_text(page)
        if len(txt) > 8:
            pages.append((url, txt))
        if depth >= 3:
            continue
        for h in _HREF.findall(page):
            u = urllib.parse.urljoin(url, _html.unescape(h)).split("#")[0]
            pu = urllib.parse.urlparse(u)
            if (pu.scheme in ("http", "https") and pu.netloc == pb.netloc and u not in seen
                    and pu.path.startswith(prefix)
                    and not re.search(r"\.(png|jpe?g|gif|svg|ico|css|js|woff2?|ttf|pdf|zip|mp4)(\?|$)", pu.path, re.I)):
                seen.add(u)
                q.append((u, "", depth + 1))
    log(f"خزش: {len(pages)} صفحه")
    return pages


# ───────────────────────── اجرا ─────────────────────────

# ───────────────────────── گیت‌هاب ─────────────────────────

_GH_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:/(tree|blob)/([^/\s?#]+)(?:/([^\s?#]*))?)?/?(?:[?#].*)?$", re.I)
_GH_EXT = (".py", ".js", ".ts", ".go", ".rs", ".java", ".php", ".rb", ".json", ".yml", ".yaml", ".md", ".proto", ".graphql")
_GH_SKIP = re.compile(r"(^|/)(node_modules|dist|build|\.github|tests?|__tests__|migrations?|alembic|locales?|i18n|assets|static|dashboard|"
                      r"vendor|\.git)(/|$)|(package-lock|yarn\.lock|pnpm-lock|poetry\.lock)|\.min\.", re.I)
_GH_HOT = re.compile(r"(router|route|api|endpoint|model|schema|view|controller|openapi|swagger)", re.I)
GH_MAX_FILE = 250 * 1024
GH_MAX_TOTAL = 4 * 1024 * 1024


def _gh_get(url: str):
    hdr = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def run_github(url: str, out_dir: str, quiet: bool) -> str | None:
    m = _GH_RE.match(url)
    if not m:
        return None

    def log(*a):
        if not quiet:
            print(*a, flush=True)

    owner, repo, kind, branch, sub = m.group(1), m.group(2), m.group(3), m.group(4), (m.group(5) or "").strip("/")
    log(f"▶ GitHub: {owner}/{repo}")
    if not branch:
        st, b = _gh_get(f"https://api.github.com/repos/{owner}/{repo}")
        if st != 200:
            raise RuntimeError(f"مخزن پیدا نشد (HTTP {st}) {b[:120]!r}")
        branch = json.loads(b).get("default_branch", "main")
    log(f"شاخه: {branch}" + (f" | مسیر: {sub}" if sub else ""))
    picked: list[tuple[str, int]] = []
    if kind == "blob" and sub:
        picked = [(sub, 0)]
    else:
        st, b = _gh_get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
        if st != 200:
            raise RuntimeError(f"فهرستِ فایل‌ها نیامد (HTTP {st}) {b[:160]!r}. اگر rate-limit است: export GITHUB_TOKEN=...")
        tree = json.loads(b).get("tree", [])
        cands = [(t["path"], t.get("size", 0)) for t in tree if t.get("type") == "blob"
                 and t["path"].lower().endswith(_GH_EXT)
                 and (not sub or t["path"] == sub or t["path"].startswith(sub + "/"))
                 and not _GH_SKIP.search(t["path"]) and t.get("size", 0) <= GH_MAX_FILE]
        cands.sort(key=lambda x: (0 if _GH_HOT.search(x[0]) else 1, 0 if x[0].endswith(".py") else 1, x[0]))
        tot = 0
        for pth, sz in cands:
            if tot + sz > GH_MAX_TOTAL:
                continue
            picked.append((pth, sz))
            tot += sz
        log(f"{len(tree)} فایل در مخزن | انتخاب‌شده: {len(picked)}")
    now = _dt.datetime.now()
    lines = ["=" * 70, f"getapi — GitHub: {owner}/{repo} ({branch})", f"آدرسِ ورودی : {url}",
             f"زمان: {now:%Y-%m-%d %H:%M:%S}", f"فایل‌ها: {len(picked)}", "=" * 70, ""]
    ok = 0
    for pth, _sz in picked:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{urllib.parse.quote(branch)}/{urllib.parse.quote(pth)}"
        stt, _h, body = fetch(raw_url)
        if stt != 200:
            log(f"  ✗ {pth} (HTTP {stt})")
            continue
        ok += 1
        lines += ["", "#" * 70, f"# FILE: {pth}", "#" * 70, decode(body)]
    d = out_dir if os.path.isabs(out_dir) else os.path.abspath(out_dir)
    os.makedirs(d, exist_ok=True)
    name = f"github.com__{owner}__{repo}__{now:%Y%m%d-%H%M%S}.txt"
    path = os.path.join(d, name)
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log("")
    log("═" * 60)
    log(f"✅ فایل‌های دریافت‌شده: {ok}/{len(picked)} | حجم: {len(text.encode('utf-8')):,} بایت")
    log(f"📄 فایل: {path}")
    log("═" * 60)
    return path


_GH_LINK = re.compile(r"https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)", re.I)
_GH_PREFER = re.compile(r"(panel|backend|api|server|core)", re.I)
_GH_NOT = re.compile(r"(docs?|website|landing|bot|app|android|ios|windows|mac|telegram|translations?|node|xray|sing|clash|github-pages)", re.I)


def github_repos_in(*texts: str) -> list[str]:
    seen: dict[str, int] = {}
    for t in texts:
        for o, r in _GH_LINK.findall(t or ""):
            r = re.sub(r"\.git$", "", r)
            if r.lower() in ("sponsors", "issues", "pulls", "releases", "discussions") or o.lower() in ("sponsors", "orgs", "topics"):
                continue
            seen[f"{o}/{r}"] = seen.get(f"{o}/{r}", 0) + 1
    def score(k: str):
        r = k.split("/", 1)[1]
        return (0 if r.lower() == "panel" else 1 if _GH_PREFER.search(r) and not _GH_NOT.search(r) else 2, -seen[k])
    return sorted(seen, key=score)


def run(url: str, out_dir: str, max_pages: int, no_crawl: bool, quiet: bool) -> str:
    def log(*a):
        if not quiet:
            print(*a, flush=True)

    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    gp = run_github(url, out_dir, quiet)
    if gp:
        return gp
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "site"
    log(f"▶ {url}")
    specs, page, scripts = find_specs(url, log)

    now = _dt.datetime.now()
    lines: list[str] = [
        "=" * 70,
        f"getapi — مستنداتِ API: {host}",
        f"آدرسِ ورودی : {url}",
        f"زمانِ دریافت: {now:%Y-%m-%d %H:%M:%S}",
        f"specهایِ پیدا‌شده: {len(specs)}",
        "=" * 70, "",
    ]
    if specs:
        for i, (u, txt, data, kind) in enumerate(specs, 1):
            lines.append(f"##### [{i}] SPEC — {kind} — {u}")
            if isinstance(data, dict) and not data.get("_raw_yaml"):
                info = data.get("info") or {}
                lines.append(f"عنوان: {info.get('title', '')} | نسخه: {info.get('version', '')}")
                if data.get("servers"):
                    lines.append("servers: " + json.dumps(data["servers"], ensure_ascii=False))
                if data.get("host"):
                    lines.append(f"host: {data.get('host')} basePath: {data.get('basePath', '')} schemes: {data.get('schemes')}")
                sec = (data.get("components") or {}).get("securitySchemes") or data.get("securityDefinitions")
                if sec:
                    lines.append("احرازِ هویت: " + json.dumps(sec, ensure_ascii=False))
            eps = endpoints_of(data, kind, txt)
            lines += ["", f"--- فهرستِ مسیرها ({len(eps)}) ---"] + eps + ["", f"--- متنِ کاملِ spec [{i}] ---", txt, ""]
    else:
        lines.append("⚠️ فایلِ OpenAPI/Swagger پیدا نشد؛ متنِ صفحه‌ها در ادامه آمده است.\n")

    if specs:
        pages = [(url, to_text(page))] if page and not (len(specs) and specs[0][0] == url) else []
    elif no_crawl:
        pages = [(url, to_text(page))] if page else []
    else:
        pages = crawl_text(url, page, max_pages, log)
    for u, t in pages:
        if t:
            lines += ["", "#" * 70, f"##### صفحه: {u}", "#" * 70, t]

    if scripts and not specs:
        lines += ["", "--- اسکریپت‌هایِ صفحه (برایِ بررسیِ دستی) ---"] + scripts

    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    d = os.path.join(out_dir, safe_host)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{safe_host}__{now:%Y%m%d-%H%M%S}.txt")
    body = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    latest = os.path.join(d, f"{safe_host}__latest.txt")
    try:
        with open(latest, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass
    n_eps = sum(len(endpoints_of(s[2], s[3], s[1])) for s in specs)
    log("")
    log("═" * 60)
    log(f"✅ specها: {len(specs)} | مسیرها: {n_eps} | حجم: {len(body.encode('utf-8')):,} بایت")
    log(f"📄 فایل: {path}")
    log("═" * 60)
    if not specs:
        repos = github_repos_in(page, body)
        if repos:
            log(f"ℹ️ spec روی سایت نیست؛ مخزن‌های گیت‌هابِ داخلِ سایت: {', '.join(repos[:5])}")
            try:
                run_github(f"https://github.com/{repos[0]}", out_dir, quiet)
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ دریافت از گیت‌هاب نشد: {type(e).__name__}: {e}")
        else:
            log("ℹ️ لینکِ گیت‌هاب هم در سایت پیدا نشد؛ آدرسِ گیت‌هاب را مستقیم بده: getapi https://github.com/OWNER/REPO")
    if not specs:
        log("⚠️ spec پیدا نشد؛ اگر مستنداتِ HTML است متنِ صفحه‌ها در فایل هست. اگر سایت با جاوااسکریپت ساخته می‌شود،")
        log("   آدرسِ فایلِ spec را در مرورگر (DevTools ← Network) پیدا کن و مستقیم بده: getapi <آدرسِ spec>")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="getapi", description="مستنداتِ API یک سایت را در یک فایلِ متنی ذخیره می‌کند.")
    ap.add_argument("url", nargs="+", help="آدرسِ سایتِ مستندات (یا مستقیم آدرسِ فایلِ spec)")
    ap.add_argument("--out", default=os.environ.get("GETAPI_OUT", DEFAULT_OUT), help=f"پوشهٔ خروجی (پیش‌فرض: {DEFAULT_OUT})")
    ap.add_argument("--max-pages", type=int, default=40, help="سقفِ صفحه‌ها در حالتِ خزش (پیش‌فرض ۴۰)")
    ap.add_argument("--no-crawl", action="store_true", help="فقط صفحهٔ اصلی؛ خزش نکن")
    ap.add_argument("-q", "--quiet", action="store_true", help="فقط مسیرِ فایل را چاپ کن")
    a = ap.parse_args(argv)
    rc = 0
    for u in a.url:
        try:
            p = run(u, a.out, a.max_pages, a.no_crawl, a.quiet)
            if a.quiet:
                print(p)
        except KeyboardInterrupt:
            return 130
        except Exception as e:  # noqa: BLE001
            print(f"❌ {u}: {type(e).__name__}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
