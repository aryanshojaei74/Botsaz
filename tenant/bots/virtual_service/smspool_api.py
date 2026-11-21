"""
smspool_api.py — کلاینتِ سرویسِ SMSPool.net برایِ فروشِ خودکارِ شمارهٔ مجازی.

مستندِ رسمی: https://www.smspool.net/article/how-to-use-the-smspool-api
endpoint پایه: https://api.smspool.net/

این ماژول فقط یک HTTP wrapper است؛ اعتبارنامه (API Key) به‌صورت پارامتر
به هر تابع داده می‌شود (چندمستأجری — هر ربات کلید خودش را دارد).

کدهایِ وضعیتِ سفارش که SMSPool برمی‌گرداند:
    1 = pending (منتظرِ پیامک)      5 = cancelled (لغوشده)
    2 = expired (منقضی‌شده)         6 = refunded (بازگشت‌داده‌شده)
    3 = completed (کدِ پیامک رسید)   7 = processing
    4 = resend (درخواستِ ارسالِ دوباره) 8 = activating
"""
import asyncio

try:
    import aiohttp
except ImportError:
    aiohttp = None

# === PATCH_VNUM_HEROSMS_V1 === کلیدی که با «hero:» شروع شود = HeroSMS (پروتکلِ SMS-Activate)
def _is_hero(api_key) -> bool:
    return str(api_key or "").strip().lower().startswith("hero:")


def _hero():
    from tenant.bots.virtual_service import herosms_api
    return herosms_api


_BASE = "https://api.smspool.net"
_TIMEOUT = 25

STATUS_LABELS = {
    1: "⏳ منتظرِ پیامک",
    2: "⌛️ منقضی‌شده",
    3: "✅ کدِ پیامک رسید",
    4: "🔁 درخواستِ ارسالِ دوباره",
    5: "❌ لغوشده",
    6: "↩️ بازگشت‌داده‌شده (رفاند)",
    7: "⚙️ در حالِ پردازش",
    8: "🔄 در حالِ فعال‌سازی",
}


async def _post(path: str, data: dict) -> dict | list | None:
    """POST به SMSPool و برگرداندنِ پاسخِ JSON (یا None در خطا)."""
    if aiohttp is None:
        return None
    url = f"{_BASE}{path}"
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(url, data=data, headers=headers) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception:
        return None


def _key_payload(api_key: str, extra: dict) -> dict:
    # مستندِ SMSPool گاهی «key» و گاهی «api_key» را در نمونه‌ها استفاده
    # کرده؛ برایِ اطمینان هر دو را می‌فرستیم — پارامترِ اضافه بی‌ضرر است.
    d = {"key": api_key, "api_key": api_key}
    d.update(extra)
    return d


async def get_balance(api_key: str) -> float | None:
    """موجودیِ حسابِ SMSPool (دلار). None یعنی خطا/کلیدِ نامعتبر."""
    if _is_hero(api_key):
        return await _hero().get_balance(api_key)
    d = await _post("/request/balance", _key_payload(api_key, {}))
    if not isinstance(d, dict):
        return None
    for field in ("balance", "amount"):
        if field in d:
            try:
                return float(str(d[field]).replace(",", ""))
            except (ValueError, TypeError):
                pass
    return None


async def validate(api_key: str) -> tuple[bool, str]:
    """اعتبارسنجیِ API Key. خروجی: (موفق, پیام)."""
    if _is_hero(api_key):
        return await _hero().validate(api_key)
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp نصب نیست."
    if not (api_key or "").strip():
        return False, "API Key خالی است."
    bal = await get_balance(api_key.strip())
    if bal is None:
        return False, "❌ اتصال برقرار نشد یا API Key نامعتبر است."
    return True, f"✅ اتصال موفق. موجودیِ حسابِ SMSPool: ${bal:,.2f}"


async def order_sms(api_key: str, country: str, service: str,
                    pool: str | None = None) -> dict:
    """خریدِ آنیِ یک شمارهٔ مجازی.
    خروجیِ موفق: {"success": 1, "order_id": "...", "phonenumber": "...",
                  "cc": "...", "expires_in": 599}
    خروجیِ ناموفق: {"success": 0, "message": "..."}"""
    if _is_hero(api_key):
        return await _hero().order_sms(api_key, country, service, pool)
    params = {"country": country, "service": service}
    if pool:
        params["pool"] = pool
    d = await _post("/purchase/sms", _key_payload(api_key, params))
    if not isinstance(d, dict):
        return {"success": 0, "message": "پاسخِ نامعتبر از SMSPool (اتصال برقرار نشد)."}
    return d


async def check_sms(api_key: str, order_id: str) -> dict:
    """وضعیتِ سفارش/دریافتِ کد.
    خروجی: {"status": <int>, "sms": "...", "full_sms": "..."}"""
    if _is_hero(api_key):
        return await _hero().check_sms(api_key, order_id)
    d = await _post("/sms/check", _key_payload(api_key, {"orderid": order_id}))
    if not isinstance(d, dict):
        return {"status": 0, "sms": "", "full_sms": ""}
    return d


async def cancel_sms(api_key: str, order_id: str) -> dict:
    """لغوِ سفارش (فقط تا قبلِ رسیدنِ کد ممکن است موفق شود)."""
    if _is_hero(api_key):
        return await _hero().cancel_sms(api_key, order_id)
    d = await _post("/sms/cancel", _key_payload(api_key, {"orderid": order_id}))
    if not isinstance(d, dict):
        return {"success": 0}
    return d


async def get_countries(api_key: str) -> list[dict]:
    """فهرستِ کشورها: [{"ID": "1", "name": "United States", ...}, ...]"""
    if _is_hero(api_key):
        return await _hero().get_countries(api_key)
    d = await _post("/country/retrieve_all", _key_payload(api_key, {}))
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        # بعضی نسخه‌ها dict برمی‌گردانند: {"1": "United States", ...}
        return [{"ID": k, "name": v} for k, v in d.items()]
    return []


async def get_services(api_key: str, country: str | None = None) -> list[dict]:
    """فهرستِ سرویس‌ها (اختیاری: فیلترشده بر اساسِ یک کشور)."""
    if _is_hero(api_key):
        return await _hero().get_services(api_key, country)
    params = {"country": country} if country else {}
    d = await _post("/service/retrieve_all", _key_payload(api_key, params))
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        return [{"ID": k, "name": v} for k, v in d.items()]
    return []


async def search_services(api_key: str, keyword: str, limit: int = 30) -> list[dict]:
    """جستجویِ سرویس‌ها بر اساسِ بخشی از نام (سمتِ کلاینت، چون SMSPool
    endpoint جستجویِ متنی ندارد)."""
    if _is_hero(api_key):
        return await _hero().search_services(api_key, keyword, limit)
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    services = await get_services(api_key)
    out = []
    for it in services:
        name = str(it.get("name", "") or "")
        if kw in name.lower():
            out.append(it)
            if len(out) >= limit:
                break
    return out
