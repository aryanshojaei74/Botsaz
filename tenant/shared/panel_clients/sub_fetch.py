"""sub_fetch.py — خواندنِ یکپارچهٔ محتوایِ ساب‌لینک (PATCH_SUB_UNIFIED_V1)

چرا این فایل هست؟ (نتیجهٔ بررسیِ error.log و کدِ پنل‌ها)
 ۱) ساب‌هایی که «آرایهٔ JSONِ کاملِ Xray» برمی‌گردانند ('[{"log":…') هیچ‌وقت به
    لینک تبدیل نمی‌شدند → «ساب‌لینک باز شد ولی هیچ خطِ کانفیگی نداشت».
    اینجا outboundهایِ vless/vmess/trojan/ss به URI تبدیل می‌شوند.
 ۲) یوپنل با UAیِ v2rayNG پاسخِ ۴۰۳ می‌داد (۶۳ بار در لاگ) و هیچ تلاشِ دوباره
    با UAیِ دیگر نبود. حالا روی ۴۰۳/۴۲۹/۵xx/HTML با چند UA امتحان می‌شود.
 ۳) nodex و marzneshin ساب را با نشستِ احرازشدهٔ پنل (توکنِ پنل!) و بدونِ UA
    می‌گرفتند. حالا همه از نشستِ تازه و UAِ کلاینتِ واقعی استفاده می‌کنند.
 ۴) کلاینتِ 3X-UI/سنایی اصلاً `_fetch_links_from_sub` نداشت، پس فالبکِ
    `_links_with_sub_fallback` در vpn_user.py برایِ آن‌ها همیشه ردّ می‌شد.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)

__all__ = ["parse_sub_body", "xray_json_to_links", "fetch_sub_links"]

_PREFIXES = ("vless://", "vmess://", "trojan://", "ss://", "ssr://", "hysteria",
             "hy2://", "tuic://", "wireguard://")

_UAS = (
    "v2rayNG/1.8.5",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Hiddify/2.0.5",
    "clash-verge/v1.6.6",
    "curl/8.5.0",
)


# ───────────────────────── Xray JSON → URI ─────────────────────────
def _first(x, default=None):
    if isinstance(x, list):
        return x[0] if x else default
    return x if x is not None else default


def _stream_params(ss: dict) -> dict:
    """پارامترهایِ query مشترکِ vless/trojan از streamSettings."""
    ss = ss or {}
    net = ss.get("network") or "tcp"
    sec = ss.get("security") or "none"
    p: dict[str, str] = {"type": net, "security": sec}
    if sec == "tls":
        t = ss.get("tlsSettings") or {}
        if t.get("serverName"):
            p["sni"] = t["serverName"]
        if t.get("fingerprint"):
            p["fp"] = t["fingerprint"]
        if t.get("alpn"):
            p["alpn"] = ",".join(t["alpn"]) if isinstance(t["alpn"], list) else str(t["alpn"])
        if t.get("allowInsecure"):
            p["allowInsecure"] = "1"
    elif sec == "reality":
        r = ss.get("realitySettings") or {}
        if r.get("serverName"):
            p["sni"] = r["serverName"]
        if r.get("fingerprint"):
            p["fp"] = r["fingerprint"]
        if r.get("publicKey"):
            p["pbk"] = r["publicKey"]
        if r.get("shortId"):
            p["sid"] = r["shortId"]
        if r.get("spiderX"):
            p["spx"] = r["spiderX"]
    if net == "ws":
        w = ss.get("wsSettings") or {}
        p["path"] = w.get("path") or "/"
        h = (w.get("headers") or {}).get("Host") or w.get("host")
        if h:
            p["host"] = h
    elif net == "grpc":
        g = ss.get("grpcSettings") or {}
        if g.get("serviceName"):
            p["serviceName"] = g["serviceName"]
        p["mode"] = "multi" if g.get("multiMode") else "gun"
    elif net in ("httpupgrade", "xhttp", "splithttp"):
        w = ss.get(f"{net}Settings") or ss.get("httpupgradeSettings") or ss.get("xhttpSettings") or {}
        p["path"] = w.get("path") or "/"
        if w.get("host"):
            p["host"] = w["host"]
        if w.get("mode"):
            p["mode"] = w["mode"]
    elif net == "kcp":
        k = ss.get("kcpSettings") or {}
        hdr = (k.get("header") or {}).get("type")
        if hdr:
            p["headerType"] = hdr
        if k.get("seed"):
            p["seed"] = k["seed"]
    elif net == "tcp":
        hdr = ((ss.get("tcpSettings") or {}).get("header") or {})
        if hdr.get("type") == "http":
            p["headerType"] = "http"
            req = hdr.get("request") or {}
            path = _first(req.get("path"))
            if path:
                p["path"] = path
            host = _first((req.get("headers") or {}).get("Host"))
            if host:
                p["host"] = host
    return p


def _qs(p: dict) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in p.items() if v not in (None, ""))


def _outbound_to_uri(ob: dict, remark: str) -> str | None:
    proto = (ob.get("protocol") or "").lower()
    st = ob.get("settings") or {}
    ss = ob.get("streamSettings") or {}
    tag = quote(remark or ob.get("tag") or "config", safe="")
    try:
        if proto in ("vless", "vmess"):
            node = _first(st.get("vnext")) or st  # xray جدید: فیلدها مستقیم زیرِ settings
            user = _first(node.get("users")) or node
            addr, port, uid = node.get("address"), node.get("port"), user.get("id")
            if not (addr and port and uid):
                return None
            sp = _stream_params(ss)
            if proto == "vless":
                p = {"encryption": user.get("encryption") or "none"}
                if user.get("flow"):
                    p["flow"] = user["flow"]
                p.update(sp)
                return f"vless://{uid}@{addr}:{port}?{_qs(p)}#{tag}"
            obj = {
                "v": "2", "ps": remark or ob.get("tag") or "config", "add": addr,
                "port": str(port), "id": uid, "aid": str(user.get("alterId", 0)),
                "scy": user.get("security") or "auto", "net": sp.get("type", "tcp"),
                "type": sp.get("headerType", "none"), "host": sp.get("host", ""),
                "path": sp.get("path", sp.get("serviceName", "")),
                "tls": "tls" if sp.get("security") == "tls" else "",
                "sni": sp.get("sni", ""), "alpn": sp.get("alpn", ""), "fp": sp.get("fp", ""),
            }
            raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
            return "vmess://" + base64.b64encode(raw).decode()
        if proto == "trojan":
            node = _first(st.get("servers")) or st
            addr, port, pw = node.get("address"), node.get("port"), node.get("password")
            if not (addr and port and pw):
                return None
            return f"trojan://{quote(str(pw), safe='')}@{addr}:{port}?{_qs(_stream_params(ss))}#{tag}"
        if proto == "shadowsocks":
            node = _first(st.get("servers")) or st
            addr, port = node.get("address"), node.get("port")
            method, pw = node.get("method"), node.get("password")
            if not (addr and port and method and pw):
                return None
            cred = base64.urlsafe_b64encode(f"{method}:{pw}".encode()).decode().rstrip("=")
            return f"ss://{cred}@{addr}:{port}#{tag}"
    except Exception:  # noqa: BLE001
        logger.exception("تبدیلِ outboundِ Xray به لینک ناموفق بود")
    return None


def xray_json_to_links(text: str) -> list[str]:
    """آرایه/آبجکتِ JSONِ کاملِ Xray → لیستِ لینک‌هایِ URI. در هر ابهام [] برمی‌گرداند."""
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return []
    configs = data if isinstance(data, list) else [data]
    links: list[str] = []
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        remark = str(cfg.get("remarks") or cfg.get("remark") or "")
        outs = [o for o in (cfg.get("outbounds") or []) if isinstance(o, dict)]
        # فقط outboundهایِ پروکسی؛ direct/block/dns را رد کن. tag=proxy اول.
        outs.sort(key=lambda o: 0 if o.get("tag") == "proxy" else 1)
        for ob in outs:
            if (ob.get("protocol") or "").lower() in ("vless", "vmess", "trojan", "shadowsocks"):
                uri = _outbound_to_uri(ob, remark)
                if uri:
                    links.append(uri)
                    break  # هر کانفیگ = یک لینک
    return links


# ───────────────────────── تجزیهٔ بدنهٔ ساب ─────────────────────────
def looks_like_xray_json(t: str) -> bool:
    t = (t or "").lstrip()
    return bool(re.match(r"^[\[{]\s*\{?", t)) and '"outbounds"' in t


def split_wg_blob(blob: str) -> list[str]:
    """=== PATCH_WG_SPLIT_SUB_BLOB_V1 === بدنهٔ ساب گاهی چند فایلِ .conf (چند لوکیشن) را پشتِ‌هم می‌دهد.
    اینجا هر بخشِ «[Interface]» (به‌همراهِ کامنت‌هایِ «# Name: …»یِ درست قبلِ آن) یک کانفیگِ جدا می‌شود."""
    lines = (blob or "").replace("\r\n", "\n").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.strip().lower() == "[interface]"]
    if len(starts) <= 1:
        return [(blob or "").strip()] if (blob or "").strip() else []
    cuts = []
    for i in starts:
        j = i
        while j > 0 and lines[j - 1].lstrip().startswith("#"):
            j -= 1
        cuts.append(j)
    cuts[0] = 0 if not "".join(lines[:cuts[0]]).strip() else cuts[0]
    out = []
    for k, c in enumerate(cuts):
        end = cuts[k + 1] if k + 1 < len(cuts) else len(lines)
        chunk = "\n".join(lines[c:end]).strip()
        if chunk:
            out.append(chunk)
    return out


def parse_sub_body(text: str) -> list[str]:
    """base64 / متنِ خام / فایلِ WireGuard / JSONِ Xray → لیستِ کانفیگ."""
    text = (text or "").strip()
    if not text:
        return []
    if looks_like_xray_json(text):
        links = xray_json_to_links(text)
        if links:
            return links
    decoded = ""
    try:
        pad = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(pad).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        decoded = ""
    wg = decoded if "[Interface]" in decoded else (text if "[Interface]" in text else "")
    if wg:
        return split_wg_blob(wg)
    body = decoded if "://" in decoded else text
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith(_PREFIXES)]


# ───────────────────────── دریافت ─────────────────────────
async def fetch_sub_links(sub_url: str, *, total_timeout: int = 10) -> list[str]:
    """ساب را با نشستِ تازه می‌گیرد؛ روی ۴۰۳/۴۲۹/۵xx/خالی/بدونِ لینک، با UAیِ بعدی تکرار."""
    import aiohttp

    if not sub_url:
        return []
    last_note = ""
    json_links: list[str] = []
    for ua in _UAS:
        try:
            timeout = aiohttp.ClientTimeout(total=total_timeout)
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": ua, "Accept": "*/*"}) as s:
                async with s.get(sub_url, allow_redirects=True) as r:
                    if r.status != 200:
                        last_note = f"HTTP {r.status}"
                        if r.status in (403, 429, 500, 502, 503, 520, 521, 522, 523, 524):
                            continue  # UAیِ بعدی
                        break  # مثلاً ۴۰۴: تکرار فایده ندارد
                    text = (await r.text() or "").strip()
        except Exception as e:  # noqa: BLE001
            last_note = type(e).__name__
            if isinstance(e, (asyncio.TimeoutError, OSError)):
                break  # سرور جواب نمی‌دهد؛ UAی دیگر فایده‌ای ندارد (قبلاً تا ۵×۱۵ثانیه)
            continue
        links = parse_sub_body(text)
        if links and looks_like_xray_json(text):
            # UAی فعلی JSONِ کامل گرفت. لینک‌هایِ تبدیل‌شده را نگه دار ولی با UAیِ بعدی
            # هم امتحان کن؛ اگر پنل با UAِ دیگر لیستِ URIِ اصلی داد، همان بهتر است.
            json_links = json_links or links
            last_note = "JSONِ Xray"
            continue
        if links:
            return links
        last_note = f"بدونِ لینک ({len(text)} بایت، شروع: {text[:40]!r})"
    if json_links:
        return json_links
    logger.warning("ساب‌لینک هیچ کانفیگی نداد [%s]: %s", last_note, sub_url)
    return []
