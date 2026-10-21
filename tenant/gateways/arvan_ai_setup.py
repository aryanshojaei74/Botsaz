"""arvan_ai_setup.py — راه‌اندازی خودکارِ هوش مصنوعیِ تشخیصِ رسید با «کلیدِ حسابِ آروان» (=== PATCH_ARVAN_AI_AUTOSETUP_V1 ===).

تا امروز مالک باید خودش در پنل آروان endpoint می‌ساخت و «آدرسِ API» و «کلید» را جدا کپی می‌کرد. طبق مستندِ رسمیِ AIaaS آروان
(https://napi.arvancloud.ir/ai/v1، هدرِ Authorization: Apikey <key>) خودِ ربات می‌تواند:
  • GET  /ai/v1/endpoints  ← endpointهایِ موجودِ حساب (هر کدام فیلدِ «endpoint» = آدرسِ گیت‌وی)،
  • GET  /ai/v1/models     ← مدل‌ها (برایِ انتخابِ مدلِ دارایِ پشتیبانیِ عکس)،
  • POST /ai/v1/endpoints  ← ساختِ endpointِ تازه {name (لاتین)، modelID، ratelimit{count,interval,unit}}.
پس مالک فقط کلیدِ حسابش را می‌دهد و یکی را انتخاب (یا یکی بساز) می‌کند. این ماژول فقط منطقِ تماس و تجزیهٔ پاسخ است
(بدونِ تلگرام)؛ هندلرها در receipt_ai_key.py هستند. هیچ‌چیز قبل از موفقیتِ تستِ واقعیِ تماس ذخیره نمی‌شود.
"""
from __future__ import annotations

import re

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

NAPI = "https://napi.arvancloud.ir/ai/v1"
_TIMEOUT = 20
_VISION_TAGS = {"vision", "image", "images", "multimodal", "multi-modal", "image-to-text"}
_VISION_NAME = re.compile(r"gpt-?4\.1|gpt-?4o|gpt-?5|gemini|claude|llava|qwen.*vl|pixtral|llama.*vision", re.I)


def auth_header(key: str) -> dict:
    k = re.sub(r"^(Bearer|Apikey)\s+", "", (key or "").strip(), flags=re.I)
    return {"Authorization": f"Apikey {k}", "Accept": "application/json"}


def _items(resp) -> list:
    data = resp.get("data") if isinstance(resp, dict) else resp
    return [x for x in (data or []) if isinstance(x, dict)] if isinstance(data, list) else []


def parse_endpoints(resp) -> list[dict]:
    """[{id, name, url, model}] از پاسخِ GET /endpoints؛ فقط ردیف‌هایی که آدرسِ https دارند."""
    out = []
    for it in _items(resp):
        url = str(it.get("endpoint") or "").strip()
        if not url.startswith("https://"):
            continue
        model = it.get("model") if isinstance(it.get("model"), dict) else {}
        out.append({"id": str(it.get("id") or ""), "name": str(it.get("name") or "")[:40],
                    "url": url.rstrip("/"), "model": str(model.get("name") or "")[:40]})
    return out


def parse_vision_models(resp, limit: int = 8) -> list[dict]:
    """مدل‌هایِ فعال که احتمالاً عکس را می‌فهمند (تگ یا نام): [{id, name, provider}]."""
    out = []
    for it in _items(resp):
        if it.get("disabled") or not it.get("id"):
            continue
        tags = {str(t).lower() for t in (it.get("tags") or [])}
        name = str(it.get("name") or "")
        if tags & _VISION_TAGS or _VISION_NAME.search(name):
            out.append({"id": str(it["id"]), "name": name[:40], "provider": str(it.get("provider") or "")[:20]})
    # مدل‌هایِ شناخته‌شده اول (GPT-4.1/4o)، بعد بقیه
    out.sort(key=lambda m: (0 if re.search(r"gpt-?4\.1|gpt-?4o", m["name"], re.I) else 1, m["name"]))
    return out[:limit]


def endpoint_from_create(resp) -> str:
    """آدرسِ گیت‌وی از پاسخِ POST /endpoints ('' اگر نبود)."""
    d = resp.get("data") if isinstance(resp, dict) else None
    url = str((d or {}).get("endpoint") or "").strip()
    return url.rstrip("/") if url.startswith("https://") else ""


def slug_name(bot_id: int) -> str:
    return f"botsaz-receipt-{int(bot_id)}"


async def _req(method: str, path: str, key: str, json=None, params=None):
    """(status, data|text). خطای شبکه ⇒ (0, پیام)."""
    try:
        import aiohttp
    except Exception:
        return 0, "کتابخانهٔ aiohttp نصب نیست"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as s:
            async with s.request(method, NAPI + path, headers=auth_header(key), json=json, params=params) as r:
                txt = await r.text()
                try:
                    import json as _json
                    return r.status, _json.loads(txt)
                except ValueError:
                    return r.status, txt[:300]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"[:200]


def _err(status: int, data) -> str:
    if status == 0:
        return f"اتصال به آروان برقرار نشد ({data})"
    if status in (401, 403):
        return "کلید پذیرفته نشد (۴۰۱/۴۰۳). کلیدِ حساب را از «کلیدهای API» آروان بساز و دسترسیِ «هوش مصنوعی» را بده."
    msg = ""
    if isinstance(data, dict):
        msg = str(data.get("message") or (data.get("error") or "") or "")[:150]
    elif isinstance(data, str):
        msg = data[:150]
    return f"آروان کد {status} داد" + (f": {msg}" if msg else "")


async def list_endpoints(key: str):
    """(لیست، خطا)"""
    st, d = await _req("GET", "/endpoints", key)
    if st != 200:
        return [], _err(st, d)
    return parse_endpoints(d), ""


async def list_vision_models(key: str):
    st, d = await _req("GET", "/models", key, params={"limit": 100})
    if st != 200:
        return [], _err(st, d)
    return parse_vision_models(d), ""


async def create_endpoint(key: str, bot_id: int, model_id: str):
    """(آدرس، خطا) — اگر endpointی با همین نام هست همان را برمی‌گرداند (ساختِ تکراری نمی‌شود)."""
    eps, err = await list_endpoints(key)
    if not err:
        for e in eps:
            if e["name"] == slug_name(bot_id):
                return e["url"], ""
    body = {"name": slug_name(bot_id), "modelID": model_id,
            "ratelimit": {"count": 30, "interval": 1, "unit": "minute"}}
    st, d = await _req("POST", "/endpoints", key, json=body)
    if st not in (200, 201):
        return "", _err(st, d)
    url = endpoint_from_create(d)
    return (url, "") if url else ("", "endpoint ساخته شد ولی آدرسش در پاسخ نبود؛ از پنل آروان آدرس را کپی و دستی ثبت کن.")
