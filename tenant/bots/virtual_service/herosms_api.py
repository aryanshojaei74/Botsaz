"""
herosms_api.py — کلاینتِ HeroSMS (پروتکلِ سازگار با SMS-Activate) برایِ «شماره مجازیِ خودکار».

=== PATCH_VNUM_HEROSMS_V1 ===
مستند: بخشِ «SMS-Activate API-compatible» در https://hero-sms.com (آدرس:
https://hero-sms.com/stubs/handler_api.php). احراز با پارامترِ api_key. پاسخ‌ها متنیِ ساده‌اند
(ACCESS_NUMBER:id:phone ، STATUS_OK:code ، ...) به‌جز getCountries/getServicesList که JSON‌اند.

رابطِ این ماژول عمداً عیناً مثلِ smspool_api است (get_balance / validate / order_sms /
check_sms / cancel_sms / get_countries / get_services / search_services) و خروجی‌ها به همان
شکل برمی‌گردند؛ پس کلِ جریانِ فروشِ خودکار بدونِ تغییر کار می‌کند. smspool_api وقتی کلید با
«hero:» شروع شود فراخوان را به این‌جا می‌سپارد.

نکته: sms-activate.ru/.org قدیمی دیگر پایدار نیست؛ کلیدِ باید از حسابِ HeroSMS گرفته شود.
"""
from __future__ import annotations

try:
    import aiohttp
except ImportError:
    aiohttp = None

import json as _json

_URL = "https://hero-sms.com/stubs/handler_api.php"
_TIMEOUT = 25

# کدهایِ وضعیتِ هم‌خانواده با smspool_api: 1 منتظر، 3 کد رسید، 5 لغو
_ERR_FA = {
    "BAD_KEY": "API Key نامعتبر است.",
    "NO_KEY": "API Key ارسال نشد.",
    "NO_BALANCE": "موجودیِ حسابِ HeroSMS کافی نیست.",
    "NO_NUMBERS": "شمارهٔ آزاد برایِ این کشور/سرویس الان موجود نیست.",
    "BAD_SERVICE": "کدِ سرویس نامعتبر است.",
    "BAD_ACTION": "درخواست نامعتبر است.",
    "WRONG_SERVICE": "کدِ سرویس نامعتبر است.",
    "BANNED": "حسابِ HeroSMS مسدود است.",
}


def strip_prefix(api_key: str) -> str:
    k = (api_key or "").strip()
    return k[5:].strip() if k.lower().startswith("hero:") else k


async def _get(params: dict) -> str | None:
    if aiohttp is None:
        return None
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(_URL, params=params, headers={"User-Agent": "Mozilla/5.0"}) as r:
                return (await r.text()).strip()
    except Exception:
        return None


def _err_text(raw: str | None) -> str:
    if raw is None:
        return "اتصال به HeroSMS برقرار نشد."
    head = raw.split(":", 1)[0].strip()
    return _ERR_FA.get(head) or (f"پاسخِ HeroSMS: {raw[:120]}" if raw else "پاسخِ خالی از HeroSMS.")


async def get_balance(api_key: str) -> float | None:
    raw = await _get({"action": "getBalance", "api_key": strip_prefix(api_key)})
    if raw and raw.startswith("ACCESS_BALANCE:"):
        try:
            return float(raw.split(":", 1)[1])
        except ValueError:
            return None
    return None


async def validate(api_key: str) -> tuple[bool, str]:
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp نصب نیست."
    if not strip_prefix(api_key):
        return False, "API Key خالی است."
    raw = await _get({"action": "getBalance", "api_key": strip_prefix(api_key)})
    if raw and raw.startswith("ACCESS_BALANCE:"):
        return True, f"✅ اتصال موفق به HeroSMS. موجودی: {raw.split(':', 1)[1]}"
    return False, "❌ " + _err_text(raw)


async def order_sms(api_key: str, country: str, service: str, pool: str | None = None) -> dict:
    raw = await _get({"action": "getNumber", "api_key": strip_prefix(api_key),
                      "service": str(service), "country": str(country)})
    if raw and raw.startswith("ACCESS_NUMBER:"):
        parts = raw.split(":")
        if len(parts) >= 3:
            _ph = parts[2].strip()
            if _ph.isdigit():   # شمارهٔ HeroSMS با کدِ کشور می‌آید؛ برایِ نمایش «+» اضافه می‌شود
                _ph = "+" + _ph
            return {"success": 1, "order_id": parts[1], "phonenumber": _ph, "cc": "",
                    "expires_in": 1200}
    return {"success": 0, "message": _err_text(raw)}


def _norm_status(raw: str | None) -> dict:
    if not raw:
        return {"status": 0, "sms": "", "full_sms": ""}
    head, _, rest = raw.partition(":")
    if head == "STATUS_OK":
        return {"status": 3, "sms": rest.strip(), "full_sms": rest.strip()}
    if head in ("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND"):
        return {"status": 1, "sms": "", "full_sms": ""}
    if head in ("STATUS_CANCEL", "ACCESS_CANCEL"):
        return {"status": 5, "sms": "", "full_sms": ""}
    if head == "NO_ACTIVATION":
        return {"status": 2, "sms": "", "full_sms": ""}
    return {"status": 0, "sms": "", "full_sms": ""}


async def check_sms(api_key: str, order_id: str) -> dict:
    k = strip_prefix(api_key)
    out = _norm_status(await _get({"action": "getStatus", "api_key": k, "id": str(order_id)}))
    if out["status"] == 3:
        # تکمیلِ فعال‌سازی (ایدمپوتنت): بدونِ آن، فعال‌سازی تا انقضا باز می‌ماند
        await _get({"action": "setStatus", "api_key": k, "id": str(order_id), "status": "6"})
    return out


async def cancel_sms(api_key: str, order_id: str) -> dict:
    raw = await _get({"action": "setStatus", "api_key": strip_prefix(api_key),
                      "id": str(order_id), "status": "8"})
    if raw and raw.startswith("ACCESS_CANCEL"):
        return {"success": 1}
    return {"success": 0, "message": _err_text(raw)}


async def get_countries(api_key: str) -> list[dict]:
    raw = await _get({"action": "getCountries", "api_key": strip_prefix(api_key)})
    try:
        d = _json.loads(raw or "")
    except Exception:
        return []
    items = d.values() if isinstance(d, dict) else d
    out = []
    for c in items:
        if isinstance(c, dict) and c.get("id") is not None:
            out.append({"ID": str(c["id"]), "name": c.get("eng") or c.get("rus") or str(c["id"])})
    return out


async def get_services(api_key: str, country: str | None = None) -> list[dict]:
    params = {"action": "getServicesList", "api_key": strip_prefix(api_key), "lang": "en"}
    if country:
        params["country"] = str(country)
    raw = await _get(params)
    try:
        d = _json.loads(raw or "")
    except Exception:
        return []
    svcs = d.get("services") if isinstance(d, dict) else d
    return [{"ID": str(s.get("code")), "name": s.get("name") or str(s.get("code"))}
            for s in (svcs or []) if isinstance(s, dict) and s.get("code")]


async def search_services(api_key: str, keyword: str, limit: int = 30) -> list[dict]:
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    out = []
    for it in await get_services(api_key):
        if kw in str(it.get("name", "")).lower() or kw == str(it.get("ID", "")).lower():
            out.append(it)
            if len(out) >= limit:
                break
    return out
