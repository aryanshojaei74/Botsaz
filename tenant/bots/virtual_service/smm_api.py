"""
smm_api.py — کلاینت پنل SMM (خدمات شبکه‌های اجتماعی) با API استاندارد برای بات‌ساز.

اکثر پنل‌های SMM (ایرانی و خارجی) از یک API یکسان پیروی می‌کنند:
یک endpoint واحد (معمولاً https://panel.example/api/v2) که با POST و پارامترهای
`key` و `action` فراخوانی می‌شود:
    action=balance                    → موجودی حساب
    action=services                   → لیست سرویس‌ها
    action=add & service & link & quantity → ثبت سفارش (خروجی: {"order": 123})
    action=status & order             → وضعیت سفارش

هر فروشنده (ادمین ربات) آدرس پنل و API Key خودش را در تنظیمات وارد می‌کند؛
همهٔ توابع، اعتبارنامه را به‌صورت پارامتر می‌گیرند (چند‌مستأجری).
"""
import asyncio
import re
import time
import uuid
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:
    aiohttp = None

from logging_setup import get_logger as _get_logger
_log = _get_logger(__name__)

# === PATCH_SMM_API_TIMEOUT_REDUCE_V1 ===
# قبلاً ۲۵ ثانیه بود — همون مشکلی که برایِ کلاینت‌هایِ پنلِ VPN با
# PATCH_PANEL_TIMEOUT_REDUCE_V1 حل شد اما اینجا جا مونده بود: این تابع
# مستقیم داخلِ هندلرِ ربات صدا زده می‌شه (مثلاً وقتی ادمین رو «مرور و
# افزودنِ سرویس» می‌زنه)، پس اگه پنل کند/معلق باشه، ادمین تا ۲۵ ثانیه
# معطل می‌مونه. ۱۰ ثانیه برایِ یه پنلِ سالم (حتی با پاسخِ نسبتاً بزرگِ
# لیستِ سرویس‌ها) کافیه؛ برایِ پنلِ واقعاً معلق، خیلی سریع‌تر fail می‌شه.
_TIMEOUT = 10

# === PATCH_SMM_API_SESSION_REUSE_V1 ===
# قبلاً هر تماس (balance/services/add/status/...) یک aiohttp.ClientSession
# تازه می‌ساخت و بعدش می‌بست — یعنی هر بار یه دست‌دهیِ کاملِ TCP+TLS از نو،
# حتی وقتی چند تماسِ پشتِ‌سرِهم به همون پنل می‌زدیم (مثلاً موجودی، بعد
# لیستِ سرویس‌ها). این کندیِ محسوسی داشت، مخصوصاً وقتی ادمین اول رو
# «ارائه‌دهندگان» بروزرسانیِ موجودی می‌زنه و بلافاصله «مرور و افزودنِ
# سرویس» — دو تماسِ جدا به همون هاست، هر کدوم با هندشیکِ TLS از صفر.
# الان یک ClientSession مشترک و تنبل‌ساخته (lazy) برایِ کلِ پروسه نگه
# می‌داریم؛ خودِ aiohttp کانکشن‌هایِ keep-alive رو به‌ازایِ هر هاست پول
# می‌کنه، پس تماسِ دومِ به همون پنل نیازی به هندشیکِ TLS تازه نداره.
_session: "aiohttp.ClientSession | None" = None
_session_lock = asyncio.Lock()


async def _get_session() -> "aiohttp.ClientSession":
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _session_lock:
        if _session is None or _session.closed:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=10, ttl_dns_cache=300)
            _session = aiohttp.ClientSession(connector=connector)
        return _session


def _normalize_url(api_url: str) -> str:
    """آدرس پنل را تمیز می‌کند و مطمئن می‌شود مسیر api دارد."""
    u = (api_url or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    u = u.rstrip("/")
    # اگر ادمین فقط دامنه را داده، مسیر استاندارد /api/v2 را اضافه کن
    low = u.lower()
    if not (low.endswith("/api/v2") or low.endswith("/api") or "/api" in low):
        u = u + "/api/v2"
    return u


# === PATCH_SMM_PANEL_ERROR_TEXT_V1 ===
# گزارشِ ادمین (فدایی): «به‌جایِ "پاسخ نامعتبر از پنل"، خودِ متنِ خطایی که پنل داده را بفرست؛ مثلاً
# اعتبار کافی نیست یا محدودیت خورده». علت: _post وقتی پنل کدِ HTTP غیر از ۲۰۰ می‌داد (۴۰۰/۴۰۳/۴۲۹/۵۰۰...)
# یا جوابش JSON نبود، *کلِ بدنه را دور می‌ریخت* و فقط None برمی‌گرداند؛ ولی بسیاری از پنل‌ها دلیلِ رد را
# همان‌جا می‌نویسند. حالا وضعیت + بدنه + خطایِ شبکه نگه داشته می‌شود و add_order از آن متنِ دقیق می‌سازد.
_ERR_SNIPPET = 300
# پیشوندهایِ خطاهایِ «فنی/انتقال» (نه متنی که خودِ پنل برایِ مشتری نوشته). مشتری این‌ها را نمی‌بیند.
TRANSPORT_ERR_PREFIXES = ("ارتباط با پنل", "HTTP ", "پاسخ غیرقابل‌فهم", "پاسخ بدون شناسه")


class PanelResponse:
    """نتیجهٔ خامِ یک درخواست به پنل."""
    __slots__ = ("status", "data", "text", "exc")

    def __init__(self, status=None, data=None, text="", exc=None):
        self.status, self.data, self.text, self.exc = status, data, text or "", exc


# === PATCH_SMM_API_REDIRECT_RETRY_V1 ===
# گزارشِ مشتری (پنلِ afgsmm.shop): سرویس‌ها از پنل گرفته نمی‌شد و فقط «اتصال پنل را بررسی کن» می‌آمد.
# دو علتِ محتمل که اینجا بسته شد:
#  ۱) آدرس با http:// ثبت شده بود؛ aiohttp در ریدایرکتِ ۳۰۱/۳۰۲ (http→https) POST را GET می‌کند و بدنه
#     (key/action) را دور می‌ریزد ⇒ پنل «اشتباه» جواب می‌داد. حالا ریدایرکت را دستی و با همان POST دنبال می‌کنیم.
#  ۲) لیستِ سرویس‌هایِ پنل‌هایِ بزرگ در ۱۰ ثانیه نمی‌رسید ⇒ برایِ services تایم‌اوتِ بلندتر + یک تلاشِ مجدد.
# علتِ دقیقِ آخرین خطا هم نگه داشته می‌شود تا به ادمین نشان داده شود (last_error).
_SERVICES_TIMEOUT = 40
_LAST_ERR: dict[str, str] = {}


def last_error(api_url: str) -> str:
    """متنِ دقیقِ آخرین خطایِ ارتباط با این پنل (یا خالی)."""
    return _LAST_ERR.get(_normalize_url(api_url), "")


async def _post_ex(api_url: str, data: dict, timeout: float | None = None) -> PanelResponse:
    if aiohttp is None:
        return PanelResponse(exc="aiohttp نصب نیست")
    url = _normalize_url(api_url)
    if not url:
        return PanelResponse(exc="آدرسِ API پنل خالی است")
    try:
        from urllib.parse import urljoin
        s = await _get_session()
        t = aiohttp.ClientTimeout(total=timeout or _TIMEOUT)
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        cur = url
        _t0 = time.monotonic()
        for _hop in range(4):
            async with s.post(cur, data=data, headers=headers, timeout=t, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    cur = urljoin(cur, r.headers["Location"])
                    continue
                raw = await r.text(errors="replace")
                parsed = None
                try:
                    import json as _json
                    parsed = _json.loads(raw) if raw.strip() else None
                except Exception:
                    parsed = None
                _dt = time.monotonic() - _t0
                if _dt > 3:   # برایِ عیب‌یابیِ «ثبتِ سفارش دیر می‌شود»: کندیِ خودِ پنل در لاگ دیده شود
                    _log.warning("smm پنلِ کند: host=%s action=%s %.1fs hop=%s",
                                 urlparse(cur).netloc, (data or {}).get("action"), _dt, _hop)
                return PanelResponse(status=r.status, data=parsed, text=raw)
        return PanelResponse(exc="ریدایرکتِ بی‌پایان")
    except Exception as e:  # noqa: BLE001
        return PanelResponse(exc=type(e).__name__)


def _compact(value) -> str:
    """مقدارِ خطا (رشته/دیکشنری/لیست) را به یک متنِ کوتاه و خوانا تبدیل می‌کند."""
    if isinstance(value, str):
        return value.strip()
    try:
        import json as _json
        return _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def panel_error_text(resp: PanelResponse) -> str | None:
    """اگر پاسخ *موفق* نیست، متنِ دقیقِ دلیلش را (کوتاه‌شده) برمی‌گرداند؛ وگرنه None."""
    if resp.exc:
        return f"ارتباط با پنل برقرار نشد ({resp.exc})"
    d = resp.data
    if isinstance(d, dict):
        for key in ("error", "errors", "message", "msg", "detail"):
            if d.get(key) not in (None, "", [], {}):
                txt = _compact(d[key])[:_ERR_SNIPPET]
                return txt if resp.status in (200, None) else f"HTTP {resp.status}: {txt}"
        if resp.status != 200:
            return f"HTTP {resp.status}: {_compact(d)[:_ERR_SNIPPET]}"
        return None
    if resp.status != 200:
        body = " ".join((resp.text or "").split())[:_ERR_SNIPPET]
        return f"HTTP {resp.status}" + (f": {body}" if body else "")
    body = " ".join((resp.text or "").split())[:_ERR_SNIPPET]
    return "پاسخ غیرقابل‌فهم از پنل" + (f": {body}" if body else " (خالی)")


async def _post(api_url: str, data: dict, timeout: float | None = None,
                retry: bool = False) -> dict | list | None:
    """درخواست POST به پنل SMM و برگرداندن پاسخ JSON (یا None در خطا؛ علتِ خطا در last_error)."""
    if aiohttp is None:
        return None
    if not _normalize_url(api_url):
        return None
    resp = await _post_ex(api_url, data, timeout)
    if retry and (resp.exc or (resp.status or 0) >= 500):
        resp = await _post_ex(api_url, data, timeout)
    key = _normalize_url(api_url)
    if resp.status == 200 and resp.data is not None:
        # بعضی پنل‌ها با ۲۰۰ هم خطا می‌دهند: {"error": "..."}
        if isinstance(resp.data, dict) and resp.data.get("error") not in (None, "", [], {}) \
                and "balance" not in resp.data and "order" not in resp.data:
            _LAST_ERR[key] = panel_error_text(resp) or ""
        else:
            _LAST_ERR.pop(key, None)
        return resp.data
    _LAST_ERR[key] = panel_error_text(resp) or ""
    return None


# === PATCH_SMM_PAYSTARS_ADAPTER_V1 ===
# پی‌استارز (paystars.uz) استانداردِ key/action معمولِ بالا را دنبال نمی‌کند:
# یک API اختصاصیِ REST دارد (هدرِ X-API-Key، اندپوینت‌هایِ جدا برایِ
# balance/username-check/buy) که فقط برایِ فروشِ استارز و پرمیومِ تلگرام
# طراحی شده، نه سرویسِ عمومیِ ممبر/ویو/... . چون جدولِ پنل ستونِ «نوع»
# ندارد، تشخیص فقط از رویِ دامنه‌یِ api_url انجام می‌شود؛ اگر پی‌استارز
# بود، هرکدام از چهار تابعِ عمومیِ زیر به‌جایِ _post استاندارد، از این
# آداپتور استفاده می‌کنند — بقیه‌یِ سیستم (قیمت‌گذاری، سبد، سفارش، پولینگ)
# دست‌نخورده می‌ماند چون همان قراردادِ ورودی/خروجیِ قبلی را می‌گیرد.
_PAYSTARS_TIMEOUT = 10


def _is_paystars(api_url: str) -> bool:
    return "paystars.uz" in (api_url or "").lower()


def _paystars_base(api_url: str) -> str:
    u = (api_url or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}/api/v1"


async def _paystars_call(method: str, api_url: str, api_key: str, path: str,
                          json_body: dict | None = None) -> tuple[int, dict | None]:
    if aiohttp is None:
        return 0, None
    try:
        s = await _get_session()
        t = aiohttp.ClientTimeout(total=_PAYSTARS_TIMEOUT)
        headers = {"X-API-Key": api_key, "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        async with s.request(method, _paystars_base(api_url) + path, headers=headers,
                              json=json_body, timeout=t) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, data
    except Exception:
        return 0, None


def _paystars_err(res: dict | None, fallback: str) -> str:
    detail = (res or {}).get("detail") if isinstance(res, dict) else None
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    return fallback


async def _paystars_get_balance(api_url: str, api_key: str) -> tuple[float, str] | None:
    status, data = await _paystars_call("GET", api_url, api_key, "/account")
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        return None
    user = data.get("user") or {}
    try:
        bal = float(user.get("balance", 0) or 0)
    except (TypeError, ValueError):
        return None
    currency = str((data.get("pricing") or {}).get("currency", "") or "")
    return bal, currency


async def _paystars_get_services(api_url: str, api_key: str) -> list[dict]:
    """چون پی‌استارز اندپوینتِ «لیستِ سرویس» ندارد، لیستِ محصولات (استارز +
    پرمیومِ ۳/۶/۱۲ ماهه) از رویِ خودِ /account/pricing ساخته می‌شود."""
    status, data = await _paystars_call("GET", api_url, api_key, "/account")
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        return []
    pricing = data.get("pricing") or {}
    try:
        star_price = float(pricing.get("star_price", 0) or 0)
        min_stars = int(float(pricing.get("min_stars", 50) or 50))
        max_stars = int(float(pricing.get("max_stars", 5000) or 5000))
    except (TypeError, ValueError):
        star_price, min_stars, max_stars = 0.0, 50, 5000
    out = []
    if star_price > 0:
        out.append({
            "service": "paystars_stars", "name": "Telegram Stars", "category": "Stars",
            "rate": star_price * 1000, "min": min_stars, "max": max_stars,
            "type": "package", "dripfeed": False, "refill": False,
        })
    for months in (3, 6, 12):
        try:
            price = float(pricing.get(f"premium_{months}_price", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        out.append({
            "service": f"paystars_premium_{months}",
            "name": f"Telegram Premium {months} Months",
            "category": "Premium", "rate": price, "min": 1, "max": 1,
            "type": "package", "dripfeed": False, "refill": False,
        })
    return out


async def _paystars_add_order(api_url: str, api_key: str, service_id: str,
                              link: str, quantity: int) -> dict:
    username = (link or "").strip().lstrip("@")
    if not username:
        return {"error": "یوزرنیمِ گیرنده خالی است."}
    is_premium = service_id.startswith("paystars_premium")
    kind = "premium" if is_premium else "stars"
    status, verify = await _paystars_call(
        "POST", api_url, api_key, "/check-username", {"username": username, "kind": kind})
    if status != 200 or not isinstance(verify, dict) or not verify.get("valid"):
        if isinstance(verify, dict) and verify.get("premium_active"):
            return {"error": "این کاربر از قبل پرمیوم فعال دارد."}
        return {"error": _paystars_err(verify, "یوزرنیم نامعتبر است یا امکانِ خرید برایش وجود ندارد.")}
    token = verify.get("verification_token")
    if is_premium:
        try:
            months = int(service_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return {"error": "سرویسِ پرمیومِ نامعتبر."}
        body = {"username": username, "months": months, "verification_token": token}
        path = "/premium/buy"
    else:
        body = {"username": username, "quantity": int(quantity or 0), "verification_token": token}
        path = "/stars/buy"
    status, res = await _paystars_call("POST", api_url, api_key, path, body)
    if status != 200 or not isinstance(res, dict) or not res.get("ok"):
        return {"error": _paystars_err(res, f"ثبتِ سفارش در پی‌استارز ناموفق بود (کدِ {status}).")}
    order_id = res.get("order_id")
    if order_id is None:
        return {"error": "پاسخِ نامعتبر از پی‌استارز (بدونِ شناسهٔ سفارش)."}
    return {"order": order_id}


async def _paystars_get_order_status(api_url: str, api_key: str, order_id) -> dict | None:
    status, data = await _paystars_call("GET", api_url, api_key, f"/order/{order_id}")
    if status != 200 or not isinstance(data, dict):
        return None
    st = data.get("status")
    if st is None and isinstance(data.get("order"), dict):
        st = data["order"].get("status")
    if st is None:
        return None
    return {"status": str(st)}


async def get_balance(api_url: str, api_key: str) -> tuple[float, str] | None:
    """موجودی حساب. خروجی: (مبلغ, واحد پول) یا None در خطا."""
    if _is_paystars(api_url):
        return await _paystars_get_balance(api_url, api_key)
    d = await _post(api_url, {"key": api_key, "action": "balance"})
    if not isinstance(d, dict):
        return None
    if "balance" not in d:
        return None
    try:
        bal = float(str(d.get("balance")).replace(",", ""))
    except (ValueError, TypeError):
        return None
    currency = str(d.get("currency", "") or "")
    return bal, currency


# === PATCH_SMM_PROVIDER_DESC_V1 ===
# کلیدهایی که پنل‌های مختلف برایِ «توضیحاتِ سرویس» استفاده می‌کنند. ترتیب
# مهم است: اولین کلیدی که مقدارِ غیرخالی داشته باشد برنده است.
_DESC_KEYS = ("desc", "description", "descriptions", "details", "detail",
              "note", "notes", "about", "service_description", "toz", "tozihat")

# سقفِ طولِ توضیحات. بعضی پنل‌ها یک صفحهٔ کاملِ HTML برمی‌گردانند؛ بدونِ
# سقف، پیامِ محصول از حدِ ۴۰۹۶ کاراکترِ تلگرام رد می‌شود و ارسال fail
# می‌خورد — یعنی مشتری اصلاً صفحهٔ محصول را نمی‌بیند.
_DESC_MAX = 600

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")


def _clean_desc(raw) -> str:
    """توضیحاتِ خامِ پنل را برایِ نمایشِ امن در تلگرام تمیز می‌کند.

    پنل‌ها معمولاً HTML می‌فرستند (<br>، <p>، <b> و…). چون پیامِ محصول با
    parse_mode="HTML" فرستاده می‌شود، تگِ ناشناخته یا بازِ نبسته کلِ پیام را
    fail می‌کند؛ پس تگ‌ها را برمی‌داریم و فقط متنِ تمیز نگه می‌داریم.
    """
    if raw is None:
        return ""
    txt = str(raw)
    if not txt.strip():
        return ""
    # <br> و </p> را به خطِ جدید تبدیل کن تا ساختارِ متن حفظ شود
    txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</p\s*>", "\n", txt)
    txt = re.sub(r"(?i)</?li\s*>", "\n", txt)
    txt = _TAG_RE.sub("", txt)
    # موجودیت‌هایِ رایجِ HTML
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        txt = txt.replace(a, b)
    txt = _WS_RE.sub(" ", txt)
    txt = _NL_RE.sub("\n\n", txt)
    txt = "\n".join(line.strip() for line in txt.split("\n")).strip()
    if len(txt) > _DESC_MAX:
        txt = txt[:_DESC_MAX].rstrip() + "…"
    return txt


def _pick_desc(item: dict) -> str:
    """اولین فیلدِ توضیحاتِ غیرخالیِ یک سرویس را برمی‌گرداند (یا رشتهٔ خالی)."""
    for k in _DESC_KEYS:
        if k in item:
            cleaned = _clean_desc(item.get(k))
            if cleaned:
                return cleaned
    return ""


async def get_services(api_url: str, api_key: str) -> list[dict]:
    """لیست سرویس‌های پنل. هر سرویس شامل service/name/rate/min/max/category است.
    خروجی یک لیست از dict یکسان‌سازی‌شده است (رشته‌های عددی به عدد تبدیل می‌شوند)."""
    if _is_paystars(api_url):
        return await _paystars_get_services(api_url, api_key)
    d = await _post(api_url, {"key": api_key, "action": "services"},
                    timeout=_SERVICES_TIMEOUT, retry=True)
    if not isinstance(d, list):
        if isinstance(d, dict) and not last_error(api_url):
            # بعضی پنل‌ها لیست را داخلِ یک کلید می‌گذارند
            for _k in ("services", "data", "result"):
                if isinstance(d.get(_k), list):
                    d = d[_k]
                    break
        if not isinstance(d, list):
            if not last_error(api_url):
                _LAST_ERR[_normalize_url(api_url)] = "پاسخِ پنل لیستِ سرویس نبود: " + str(d)[:150]
            return []
    out = []
    for item in d:
        if not isinstance(item, dict):
            continue
        try:
            sid = str(item.get("service", "") or item.get("id", ""))
            if not sid:
                continue
            rate = float(str(item.get("rate", 0) or 0).replace(",", ""))
            mn = int(float(item.get("min", 0) or 0))
            mx = int(float(item.get("max", 0) or 0))
            out.append({
                "service": sid,
                "name": str(item.get("name", "") or ""),
                "category": str(item.get("category", "") or ""),
                "rate": rate,          # قیمت پنل به‌ازای هر ۱۰۰۰ عدد (به واحد پول پنل)
                "min": mn,
                "max": mx,
                "type": str(item.get("type", "") or ""),
                "dripfeed": bool(item.get("dripfeed", False)),
                "refill": bool(item.get("refill", False)),
                # === PATCH_SMM_REFILL_V1 === بعضی پنل‌ها فیلدِ refill را اصلاً نمی‌فرستند؛ «نبودن» با «False»
                # فرق دارد (اولی یعنی نمی‌دانیم، دومی یعنی ریفیل ندارد).
                "refill_known": "refill" in item,
                # === PATCH_SMM_CANCEL_FLAG_V1 === مستندات: هر سرویس فیلدِ cancel (true/false) دارد
                "cancel": bool(item.get("cancel", False)),
                "cancel_known": "cancel" in item,
                # === PATCH_SMM_PROVIDER_DESC_V1 ===
                # توضیحاتِ خودِ ارائه‌دهنده. استانداردِ رسمیِ SMM این فیلد را
                # ندارد، ولی عملاً بیشترِ پنل‌ها (مخصوصاً ایرانی‌ها) آن را
                # می‌فرستند — فقط با اسم‌های مختلف. تا الان کلاً دور ریخته
                # می‌شد، برای همین ربات مجبور بود یک متنِ عمومی از رویِ
                # دسته/بازهٔ سفارش بسازد (شکایتِ ادمین: «توضیحاتِ ارائه‌دهنده
                # رو نزده، یه چیز دیگه زده کلاً»).
                "desc": _pick_desc(item),
            })
        except (ValueError, TypeError):
            continue
    return out


async def add_order(api_url: str, api_key: str, service_id: str,
                    link: str, quantity: int) -> dict | None:
    """ثبت سفارش روی پنل. خروجی موفق: {"order": <id>}.
    در خطا: {"error": "..."} یا None."""
    if _is_paystars(api_url):
        return await _paystars_add_order(api_url, api_key, service_id, link, quantity)
    # === PATCH_SMM_PANEL_ERROR_TEXT_V1 ===
    resp = await _post_ex(api_url, {
        "key": api_key, "action": "add",
        "service": service_id, "link": link, "quantity": quantity,
    })
    err = panel_error_text(resp)
    d = resp.data
    if resp.status == 200 and isinstance(d, dict) and d.get("order") and not d.get("error"):
        return d                                      # موفق: همان قرارداد {"order": id}
    if err is None:
        # ۲۰۰ و JSON ولی نه شناسهٔ سفارش دارد نه خطا (مثلاً {"status":"ok"}) — ثبت‌شده فرض نمی‌کنیم
        err = f"پاسخ بدون شناسه سفارش: {_compact(d)[:_ERR_SNIPPET]}"
    return {"error": err}


async def get_order_status(api_url: str, api_key: str, order_id) -> dict | None:
    """وضعیت یک سفارش. خروجی شامل status/charge/start_count/remains."""
    if _is_paystars(api_url):
        return await _paystars_get_order_status(api_url, api_key, order_id)
    d = await _post(api_url, {"key": api_key, "action": "status", "order": order_id})
    if not isinstance(d, dict):
        return None
    return d


# === PATCH_SMM_BULK_STATUS_V1 ===
async def get_orders_status_bulk(api_url: str, api_key: str, order_ids: list) -> dict | None:
    """استعلامِ گروهیِ وضعیتِ چند سفارش در یک درخواست (اکشنِ استانداردِ
    status با پارامترِ orders به‌جایِ order، جدا با کاما).

    خروجی: { "order_id_str": {status_fields}, ... } یا None اگر پنل
    این حالت رو پشتیبانی نکنه (پاسخِ نامعتبر/خالی). حداکثر ۵۰ شناسه در
    هر تماس — بیشتر از این معمولاً پنل‌ها رد می‌کنن یا کوتاهش می‌کنن، پس
    فراخوان باید خودش دسته‌بندی (chunk) کنه.

    === PATCH_SMM_PAYSTARS_ADAPTER_V1 ===
    پی‌استارز اکشنِ گروهی ندارد؛ None برمی‌گردانیم تا فراخوان (که در این
    حالت None را «پنل بولک را ساپورت نمی‌کند» تفسیر می‌کند) خودکار به
    استعلامِ تک‌به‌تکِ get_order_status سوییچ کند."""
    if _is_paystars(api_url):
        return None
    ids = ",".join(str(x) for x in order_ids)
    d = await _post(api_url, {"key": api_key, "action": "status", "orders": ids})
    if not isinstance(d, dict) or not d:
        return None
    return d


# === PATCH_SMM_REFILL_CANCEL_V1 ===
def _valid_refill_id(v) -> str | None:
    """شناسهٔ ریفیل را اگر واقعاً معتبر باشد (نه خالی/۰/false) به رشته برمی‌گرداند."""
    if isinstance(v, dict):
        v = v.get("id") or v.get("refill")
    if v is None or isinstance(v, bool):
        return None
    sv = str(v).strip()
    return None if sv.lower() in ("", "0", "false", "none", "null", "no") else sv


async def refill_order(api_url: str, api_key: str, order_id) -> dict:
    """درخواستِ ریفیل برای یک سفارش (اکشنِ استانداردِ refill).

    === PATCH_SMM_REFILL_V1 ===
    همیشه dict برمی‌گرداند: موفق ⇒ {"refill": "<id>"}، شکست ⇒ {"error": "<متنِ دقیقِ پنل>"}.
    قبلاً پاسخِ خالی/بی‌شناسه/قطعِ ارتباط هم «ثبت شد» حساب می‌شد (گزارشِ فدایی: «برایِ همهٔ محصولات
    ثبت میشه»)؛ حالا فقط وقتی شناسهٔ ریفیلِ معتبر برگردد موفق است."""
    if _is_paystars(api_url):
        return {"error": "این پنل درخواستِ ریفیل را از طریقِ API پشتیبانی نمی‌کند."}
    resp = await _post_ex(api_url, {"key": api_key, "action": "refill", "order": order_id})
    err = panel_error_text(resp)
    d = resp.data
    if isinstance(d, list):
        d = next((x for x in d if isinstance(x, dict)), None)
    # === PATCH_SMM_REFILL_ERRDICT_V1 === مستندات: {"refill": {"error": "Incorrect order ID"}}
    if resp.status == 200 and isinstance(d, dict) and isinstance(d.get("refill"), dict) and d["refill"].get("error"):
        return {"error": str(d["refill"]["error"])}
    if resp.status == 200 and isinstance(d, dict) and not d.get("error"):
        rid = _valid_refill_id(d.get("refill"))
        if rid:
            return {"refill": rid}
        return {"error": "پنل شناسهٔ ریفیل نداد (احتمالاً این سفارش/سرویس ریفیل ندارد)."
                + (f" پاسخِ پنل: {_compact(d)[:200]}" if d else "")}
    return {"error": err or "پاسخِ نامعتبر از پنل"}


async def get_refill_status(api_url: str, api_key: str, refill_id) -> dict:
    """وضعیتِ یک ریفیل (اکشنِ استانداردِ refill_status). موفق ⇒ {"status": "..."}؛ شکست ⇒ {"error": "..."}."""
    if _is_paystars(api_url):
        return {"error": "این پنل پیگیریِ ریفیل را پشتیبانی نمی‌کند."}
    resp = await _post_ex(api_url, {"key": api_key, "action": "refill_status", "refill": refill_id})
    err = panel_error_text(resp)
    d = resp.data
    if isinstance(d, list):
        d = next((x for x in d if isinstance(x, dict)), None)
    if resp.status == 200 and isinstance(d, dict) and not d.get("error"):
        st = d.get("status")
        if isinstance(st, dict):
            st = st.get("status") or st.get("name")
        if st not in (None, ""):
            return {"status": str(st)}
        return {"error": "پنل وضعیتِ ریفیل را نداد"}
    return {"error": err or "پاسخِ نامعتبر از پنل"}


def _refill_status_value(st) -> dict:
    """مقدارِ status در پاسخِ گروهی (رشته | {"status"/"name"} | {"error"}) ⇒ {"status": ..} یا {"error": ..}."""
    for _ in range(3):                      # {"status": {"status": "Rejected"}} هم باز می‌شود
        if not isinstance(st, dict):
            break
        if st.get("error"):
            return {"error": str(st["error"])}
        st = st.get("status") or st.get("name")
    if st in (None, "") or isinstance(st, dict):
        return {"error": "پنل وضعیتِ ریفیل را نداد"}
    return {"status": str(st)}


async def get_refills_status_bulk(api_url: str, api_key: str, refill_ids: list) -> dict | None:
    """=== PATCH_SMM_REFILL_BULK_V1 === وضعیتِ چند ریفیل در یک درخواست (مستندات: refill_status با «refills»
    جداشده با کاما؛ پاسخ: [{"refill": 1, "status": "Completed"}, {"refill": 2, "status": {"error": "Refill not found"}}]).

    خروجی: {refill_id_str: {"status": ..} | {"error": ..}} یا None اگر پنل حالتِ گروهی را نپذیرفت (پاسخِ
    نامعتبر/خطایِ کلی) — فراخوان باید به استعلامِ تک‌به‌تک برگردد. شناسه‌هایی که در پاسخ نیامدند در خروجی نیستند."""
    if _is_paystars(api_url) or not refill_ids:
        return None
    resp = await _post_ex(api_url, {"key": api_key, "action": "refill_status",
                                    "refills": ",".join(str(x) for x in refill_ids)})
    d = resp.data
    if resp.status != 200 or d is None or (isinstance(d, dict) and d.get("error")):
        return None
    out: dict[str, dict] = {}
    if isinstance(d, list):
        for it in d:
            if isinstance(it, dict) and it.get("refill") not in (None, ""):
                out[str(it["refill"])] = _refill_status_value(it.get("status", it))
    elif isinstance(d, dict):
        for k, v in d.items():
            if str(k) in {str(x) for x in refill_ids}:
                out[str(k)] = _refill_status_value(v)
    return out or None


async def cancel_order(api_url: str, api_key: str, order_id) -> dict | list | None:
    """درخواستِ لغو برای یک سفارش (اکشنِ استانداردِ cancel). اکثرِ پنل‌ها
    آرایه برمی‌گردونن: [{"order": id, "cancel": {"status": "Completed"}}]
    — بعضی‌ها هم مستقیم dict تکی. پارسِ نتیجه با _parse_cancel_response
    در tenant/smm.py انجام می‌شود، چون شکلِ دقیقِ پاسخ بینِ پنل‌ها فرق
    دارد."""
    if _is_paystars(api_url):
        return {"error": "این پنل لغوِ سفارش را از طریقِ API پشتیبانی نمی‌کند."}
    d = await _post(api_url, {"key": api_key, "action": "cancel", "orders": str(order_id)})
    # بعضی پنل‌ها (مثلِ WhaleSMM) برایِ cancel پارامترِ «order» (مفرد) می‌خواهند و با «orders» خطایِ شناسهٔ سفارش
    # می‌دهند («error.incorrect_order_id»)؛ فقط در همین حالت یک‌بار با نامِ مفرد تکرار می‌کنیم.
    if _cancel_wrong_param(d):
        d2 = await _post(api_url, {"key": api_key, "action": "cancel", "order": str(order_id)})
        if d2 is not None:
            return d2
    return d


def _cancel_wrong_param(d) -> bool:
    items = d if isinstance(d, list) else [d]
    for it in items:
        if not isinstance(it, dict):
            continue
        c = it.get("cancel") if isinstance(it.get("cancel"), dict) else it
        e = str(c.get("error") or it.get("error") or "").lower()
        if e and "already" not in e and any(k in e for k in ("incorrect", "invalid", "not found", "required", "missing", "order_id", "order id")):
            return True
    return False


async def validate(api_url: str, api_key: str) -> tuple[bool, str]:
    """اعتبارسنجی آدرس و کلید پنل. خروجی: (موفق, پیام)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp نصب نیست."
    if not api_url or not api_key:
        return False, "آدرس پنل یا API Key خالی است."
    res = await get_balance(api_url, api_key)
    if res is None:
        why = last_error(api_url)
        return False, "❌ اتصال برقرار نشد یا کلید/آدرس نامعتبر است." + (f"\nعلت: {why}" if why else "")
    bal, cur = res
    return True, f"✅ اتصال موفق. موجودی: {bal:,.2f} {cur}".strip()


# ═══════════════════ دسته‌بندی هوشمند سرویس‌ها ═══════════════════
# سرویس‌های پنل‌های SMM معمولاً نامی مثل «Telegram - Reactions [Positive]» یا
# «Instagram Followers [Real]» دارند. این توابع از روی نام (و در صورت وجود،
# فیلد category) پلتفرم و نوعِ سرویس را تشخیص می‌دهند تا در ربات به‌صورت
# دو مرحله‌ای (اول پلتفرم، بعد نوع) به کاربر نشان داده شوند.

# پلتفرم‌ها: (کلید, برچسبِ نمایشی با ایموجی, کلمات کلیدی)
PLATFORMS = [
    ("telegram",  "✈️ تلگرام",     ["telegram", "tg ", "tg-", "t.me", "تلگرام"]),
    ("instagram", "📸 اینستاگرام", ["instagram", "insta", "ig ", "اینستا"]),
    ("youtube",   "▶️ یوتیوب",      ["youtube", "yt ", "yt-", "یوتیوب"]),
    ("tiktok",    "🎵 تیک‌تاک",     ["tiktok", "tik tok", "تیک"]),
    ("twitter",   "🐦 توییتر/X",    ["twitter", "x.com", " x ", "توییتر"]),
    ("spotify",   "🎧 اسپاتیفای",   ["spotify", "اسپاتیفای"]),
    ("facebook",  "📘 فیسبوک",      ["facebook", "fb ", "فیسبوک"]),
    ("whatsapp",  "💬 واتساپ",      ["whatsapp", "واتساپ"]),
    ("discord",   "🎮 دیسکورد",     ["discord", "دیسکورد"]),
    # === PATCH_SMM_TAXONOMY_EXPAND_V1 ===
    # پلتفرم‌هایِ رایجِ دیگری که تویِ اکثرِ پنل‌هایِ SMM هست ولی قبلاً همه‌شون
    # تویِ سطلِ خنثیِ «سایر» می‌افتادن.
    ("soundcloud", "🎶 ساندکلاود",  ["soundcloud", "ساندکلاود"]),
    ("linkedin",  "💼 لینکدین",     ["linkedin", "لینکدین"]),
    ("pinterest", "📌 پینترست",     ["pinterest", "پینترست"]),
    ("snapchat",  "👻 اسنپ‌چت",     ["snapchat", "اسنپ"]),
    ("threads",   "🧵 تردز",        ["threads", "تردز"]),
    ("reddit",    "👽 ردیت",        ["reddit", "ردیت"]),
    ("twitch",    "🟣 توییچ",       ["twitch", "توییچ"]),
    ("vimeo",     "🎬 ویمیو",       ["vimeo", "ویمیو"]),
    # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
    ("rubika",    "💜 روبیکا",      ["rubika", "روبیکا"]),
    ("rubino",    "🟣 روبینو",      ["rubino", "روبینو"]),   # === PATCH_RUBINO_PLATFORM_V1 ===
    ("eita",      "🟡 ایتا",        ["eitaa", "eita", "ایتا"]),
    ("chatgpt",   "🤖 چت‌جی‌پی‌تی",   ["chatgpt", "chat gpt", "openai", "چت‌جی‌پی‌تی", "چت جی پی تی"]),
    ("google",    "🔍 گوگل",        ["google", "گوگل"]),
    ("gemini",    "♊️ جمینای",      ["gemini", "جمینای", "جمینی"]),
    ("website",   "🌐 وب‌سایت/ترافیک", ["website", "traffic", "seo"]),
    # === PATCH_SMM_MANUAL_CATEGORY_BOOST_V1 ===
    # درخواستِ ادمین: «بوست» هم مثلِ روبیکا/ایتا/گوگل به‌عنوانِ یک
    # پلتفرمِ قابل‌انتخاب موقعِ ساختِ دسته/سرویسِ دستی دربیاد — قبلاً
    # «بوست» فقط تویِ SERVICE_TYPES بود (یعنی فقط برایِ برچسبِ نوعِ
    # سرویس تشخیص داده می‌شد)، نه تویِ همین لیستِ PLATFORMS که دکمه‌هایِ
    # «➕ افزودنِ دسته» و صفحه‌ی «تغییرِ نامِ پلتفرم‌ها» از رویش می‌سازن.
    ("boost",     "🚀 بوست",        ["boost", "بوست"]),
]

# انواع سرویس: (کلید, برچسبِ نمایشی, کلمات کلیدی)
# === PATCH_SMM_TAXONOMY_EXPAND_V1 ===
# ترتیب مهم است: اولین تطابق برنده است، پس دسته‌های خاص‌تر (که ممکنه
# کلمه‌ی مشترک با دسته‌های عمومی‌تر داشته باشن) باید بالاتر بیان.
SERVICE_TYPES = [
    # === PATCH_SMM_BOOST_TYPE_V1 ===
    # درخواستِ ادمین‌ها: «بوستِ» تلگرام دستهٔ خودش را داشته باشد. عمداً
    # *بالاتر* از member و view است، چون نامِ سرویس‌هایِ بوست معمولاً کلمهٔ
    # «channel» یا «premium» هم دارد و اگر پایین‌تر بود، ممکن بود زیرِ
    # دسته‌هایِ دیگر بیفتد — اولین تطابق برنده است.
    ("boost",     "🚀 حمایت بوست",         ["boost", "بوست"]),
    # === PATCH_SMM_TAXONOMY_EXPAND_V1 ===
    ("star_gift", "🎁 گیفت استارزی",       ["star gift", "gift stars", "telegram gift", "nft gift", "گیفت استارز", "گیفت تلگرام"]),
    ("premium_reaction", "💎 ری‌اکشن پرمیوم و استارز",
     ["premium reaction", "star reaction", "sticker reaction", "ری‌اکشن پرمیوم", "ری‌اکشن استارز"]),
    ("bot_start", "🤖 استارت ربات",        ["bot start", "start bot", "استارت ربات", "استارت بات"]),
    ("channel_member", "📢 ممبر کانال",    ["channel member", "channel subscriber", "channel join", "ممبر کانال", "عضو کانال"]),
    ("group_member",   "👥 ممبر گروه",     ["group member", "group subscriber", "group join", "ممبر گروه", "عضو گروه"]),
    # === PATCH_SMM_REACTION_PRIORITY_FIX_V1 ===
    # باگِ واقعی: سرویس‌هایی مثل «ری‌اکشن مثبت [+بازدید گیفت]» هم کلمهٔ
    # «ری‌اکشن» دارن هم «بازدید» (که کلیدواژه‌ی view است). چون قبلاً view
    # زودتر از reaction چک می‌شد، این سرویس‌ها اشتباهی زیرِ دستهٔ «ویو»
    # می‌رفتن. reaction را بالاتر آوردیم تا وقتی اسمِ سرویس صریحاً
    # «ری‌اکشن» دارد، همیشه برنده باشد.
    ("reaction",  "❤️ ری‌اکشن",            ["reaction", "react", "ری‌اکشن", "ریکشن", "واکنش"]),
    ("member",    "👥 ممبر / سابسکرایبر", ["member", "subscriber", "subscribe", "join", "ممبر", "عضو"]),
    ("follower",  "🧑‍🤝‍🧑 فالوور",          ["follower", "follow", "fans", "فالوور", "دنبال"]),
    ("view",      "👁 ویو / بازدید",       ["view", "watch", "impression", "reach", "ویو", "بازدید"]),
    ("like",      "👍 لایک",               ["like", "لایک"]),
    ("comment",   "💬 کامنت",              ["comment", "کامنت", "نظر"]),
    ("share",     "🔁 اشتراک/فوروارد",     ["share", "repost", "forward", "retweet", "اشتراک", "فوروارد"]),
    ("vote",      "📊 رأی / نظرسنجی",      ["vote", "poll", "rating", "رأی", "نظرسنجی"]),
    ("report",    "🚩 ریپورت",             ["report", "ریپورت"]),
    ("saves",     "🔖 سیو / ذخیره",        ["save", "bookmark", "سیو", "ذخیره"]),
    ("live",      "🔴 لایو",               ["live", "لایو"]),
    ("play",      "▶️ پلی / استریم",       ["play", "stream", "listen", "پلی", "استریم"]),
]


def detect_platform(name: str, category: str = "") -> str:
    """کلید پلتفرم را از روی نام/دسته تشخیص می‌دهد. پیش‌فرض: other."""
    text = f"{name} {category}".lower()
    # === PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1 ===
    # روبینو/روبیکا/ایتا اول: اسمِ سرویس‌هایشان معمولاً «فالوور روبینو (اینستاگرامی)» یا
    # «Rubika Rubino …» است و قبلاً به‌خاطرِ ترتیب زیرِ اینستاگرام/روبیکا می‌رفتند.
    for key, _label, kws in _detect_order():
        for kw in kws:
            if kw in text:
                return key
    return "other"


_DETECT_FIRST = ("rubino", "rubika", "eita")
_DETECT_ORDER: list | None = None


def _detect_order() -> list:
    """PLATFORMS با روبینو/روبیکا/ایتا در ابتدا — یک‌بار ساخته می‌شود (detect برایِ هر سرویس صدا زده می‌شود)."""
    global _DETECT_ORDER
    if _DETECT_ORDER is None:
        _DETECT_ORDER = sorted(PLATFORMS, key=lambda p: _DETECT_FIRST.index(p[0]) if p[0] in _DETECT_FIRST else 99)
    return _DETECT_ORDER


def detect_service_type(name: str, category: str = "") -> str:
    """کلید نوع سرویس را از روی نام/دسته تشخیص می‌دهد. پیش‌فرض: other."""
    text = f"{name} {category}".lower()
    for key, _label, kws in SERVICE_TYPES:
        for kw in kws:
            if kw in text:
                return key
    return "other"


# === PATCH_SMM_CATKEY_SHORT_V1 ===
# باگِ «گیر کردن روی ⏳ در حالِ دریافتِ سرویس‌ها»: در حالتِ «دسته‌بندیِ
# ارائه‌دهنده»، نامِ خامِ دستهٔ پنل مستقیم توی callback_data گذاشته می‌شد.
# تلگرام callback_data را حداکثر ۶۴ بایت می‌پذیرد (فارسی/ایموجی ۲ تا ۴ بایت
# برایِ هر حرف) و نبودِ «:» را هم فرض می‌کرد؛ پنل‌هایِ پرمحصول اسمِ دسته‌هایِ
# بلند دارند => BUTTON_DATA_INVALID => هم edit_text و هم answer خطا می‌دادند
# و پیامِ «صبر کن» برایِ همیشه می‌ماند. راه‌حل: کلیدِ کوتاه و ثابتِ ASCII
# (c_ + هشِ ۱۰ کاراکتری) و نگهداریِ نامِ اصلی در یک دیکشنری برایِ نمایش.
_CAT_LABELS: dict[str, str] = {}


def _cat_key(raw: str) -> str:
    import hashlib
    key = "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    _CAT_LABELS[key] = raw
    return key


def platform_label(key: str) -> str:
    for k, label, _ in PLATFORMS:
        if k == key:
            return label
    if key in _CAT_LABELS:
        return f"📦 {_CAT_LABELS[key]}"
    if key.startswith("c_") and len(key) == 12:
        return "📦 دسته"
    # === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
    # وقتی group_services با by_category=True صدا زده شود، pkey دیگر یک
    # کلیدِ ثابت از PLATFORMS نیست — خودِ رشتهٔ دسته‌بندیِ خامِ ارائه‌دهنده
    # است (مثلاً «Instagram Followers»). قبلاً همیشه به «📦 سایر» برمی‌گشت
    # و همهٔ دسته‌هایِ ارائه‌دهنده زیرِ یک برچسبِ یکسان گم می‌شدند.
    if key and key != "other":
        return f"📦 {key}"
    return "📦 سایر"


def service_type_label(key: str) -> str:
    # === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
    if key == "__all__":
        return "📄 نمایشِ محصولات"
    for k, label, _ in SERVICE_TYPES:
        if k == key:
            return label
    return "📦 سایر"


def group_services(services: list[dict], by_category: bool = False) -> dict:
    """سرویس‌ها را به ساختار دو‌سطحی { platform_key: { type_key: [services] } }
    دسته‌بندی می‌کند و فیلدهای platform/stype را به هر سرویس اضافه می‌کند.

    === PATCH_SMM_PROVIDER_CATEGORY_MODE_V1 ===
    گزارشِ ادمین (فدایی): موقعِ افزودنِ محصول، به‌جایِ دسته‌بندیِ خودِ
    ارائه‌دهنده، سیستم با حدسِ خودکار (اسمِ سرویس) دسته‌بندی می‌سازد —
    برایِ ارائه‌دهنده‌هایی که خودشان دسته‌بندیِ منظم دارند، این فقط مانع
    است. با by_category=True، سطحِ اول دقیقاً همان رشتهٔ خامِ category
    است که خودِ پنل برگردانده (نه حدس)؛ سطحِ دوم چون معنایی ندارد یک
    بخشِ ثابت («__all__») می‌شود تا کدِ نمایشِ دوسطحیِ موجود دست‌نخورده
    بماند — فقط یک تپِ اضافه، بدونِ نیاز به بازنویسیِ رابطِ کاربری.
    """
    tree: dict[str, dict[str, list]] = {}
    for svc in services:
        if by_category:
            p = _cat_key((svc.get("category") or "").strip() or "سایر")
            t = "__all__"
        else:
            p = detect_platform(svc.get("name", ""), svc.get("category", ""))
            t = detect_service_type(svc.get("name", ""), svc.get("category", ""))
        svc["platform"] = p
        svc["stype"] = t
        tree.setdefault(p, {}).setdefault(t, []).append(svc)
    return tree


# ═══════════════════ فارسی‌سازیِ خودکارِ نامِ سرویس‌ها ═══════════════════
# === PATCH_SMM_NAME_PERSIANIZE_V1 ===
# اسمِ خامی که پنل برمی‌گردونه معمولاً انگلیسیه (مثلاً
# "Telegram Members [Real][Instant][No Drop]"). این دیکشنری کلمه‌به‌کلمه/
# عبارت‌به‌عبارت جایگزینیِ فارسی می‌زنه تا اسمی که مشتری می‌بینه خواناتر
# باشه. ترجمه‌ی کامل و روان نیست (چون یه سیستمِ ترجمه‌ی هوشمند نیست، فقط
# جایگزینیِ اصطلاحاته)، ولی خروجیش قابلِ‌فهم‌تر از انگلیسیِ خام است. ادمین
# همیشه می‌تونه با «✏️ ویرایشِ نامِ نمایشی» دستی تمیزترش کنه.
#
# ترتیب مهمه: عبارت‌های چندکلمه‌ای باید قبل از تک‌کلمه‌ای‌ها چک بشن،
# وگرنه مثلاً "non drop" قبل از رسیدن به قاعده‌ی خودش با "drop" جایگزین
# نصفه‌ونیمه می‌شه.
_PERSIAN_TERMS: list[tuple[str, str]] = [
    # عبارت‌های چندکلمه‌ای (اول)
    ("non drop", "بدون‌ریزش"),
    ("no drop", "بدون‌ریزش"),
    ("no refill", "بدون‌ریفیل"),
    ("auto refill", "ریفیل خودکار"),
    ("high quality", "کیفیت‌بالا"),
    ("best quality", "بهترین‌کیفیت"),
    ("super fast", "فوق‌سریع"),
    ("life time", "مادام‌العمر"),
    ("lifetime guarantee", "ضمانتِ مادام‌العمر"),
    ("instant start", "شروعِ آنی"),
    ("real active", "واقعی و فعال"),
    ("real & active", "واقعی و فعال"),
    ("old accounts", "اکانتِ قدیمی"),
    ("max speed", "حداکثرِ سرعت"),
    ("world wide", "جهانی"),
    ("worldwide", "جهانی"),
    ("cheapest", "ارزان‌ترین"),
    ("star gift", "گیفتِ استارزی"),
    ("gift stars", "گیفتِ استارزی"),
    # پلتفرم‌ها (تویِ خودِ اسمِ سرویس، چون آیکون/برچسبِ پلتفرم قبلاً جدا نشون داده می‌شه)
    ("telegram", "تلگرام"),
    ("instagram", "اینستاگرام"),
    ("youtube", "یوتیوب"),
    ("tiktok", "تیک‌تاک"),
    ("twitter", "توییتر"),
    ("facebook", "فیسبوک"),
    ("whatsapp", "واتساپ"),
    ("spotify", "اسپاتیفای"),
    # === PATCH_SMM_NAME_PERSIANIZE_V1 (نسخه ۲) ===
    # اسم‌های خامِ پنل معمولاً از این اسم‌های عمومی هم پر بودن که تویِ
    # دور اول جا افتاده بودن (Members/Followers/Views/...).
    ("subscribers", "سابسکرایبر"),
    ("subscriber", "سابسکرایبر"),
    ("members", "ممبر"),
    ("member", "ممبر"),
    ("followers", "فالوور"),
    ("follower", "فالوور"),
    ("views", "ویو"),
    ("view", "ویو"),
    ("likes", "لایک"),
    ("like", "لایک"),
    ("comments", "کامنت"),
    ("comment", "کامنت"),
    ("shares", "اشتراک"),
    ("share", "اشتراک"),
    ("reactions", "ری‌اکشن"),
    ("reaction", "ری‌اکشن"),
    ("channel", "کانال"),
    ("group", "گروه"),
    ("story", "استوری"),
    ("stories", "استوری"),
    ("reels", "ریلز"),
    ("boost", "بوست"),
    ("votes", "رأی"),
    ("vote", "رأی"),
    ("gifts", "گیفت"),
    ("gift", "گیفت"),
    ("star", "استارز"),
    ("package", "بسته"),
    ("bot", "ربات"),
    ("start", "استارت"),
    # تک‌کلمه‌ای‌ها
    ("instant", "آنی"),
    ("fast", "سریع"),
    ("slow", "آهسته"),
    ("real", "واقعی"),
    ("active", "فعال"),
    ("guaranteed", "تضمینی"),
    ("guarantee", "ضمانت"),
    ("refill", "ریفیل"),
    ("drop", "ریزش"),
    ("speed", "سرعت"),
    ("quality", "کیفیت"),
    ("premium", "پرمیوم"),
    ("global", "جهانی"),
    ("europe", "اروپا"),
    ("arabic", "عربی"),
    ("female", "دخترانه"),
    ("male", "پسرانه"),
    ("server", "سرور"),
    ("max", "حداکثر"),
    ("min", "حداقل"),
    ("days", "روزه"),
    ("day", "روزه"),
    ("months", "ماهه"),
    ("month", "ماهه"),
    ("cheap", "ارزان"),
]
_PERSIAN_TERM_RE = [
    (re.compile(r"\b" + re.escape(en) + r"\b", re.IGNORECASE), fa)
    for en, fa in _PERSIAN_TERMS
]


def persianize_name(name: str) -> str:
    """جایگزینیِ اصطلاحاتِ رایجِ انگلیسیِ SMM با معادلِ فارسی. برای اسم‌هایی
    که از قبل کاملاً فارسی‌اند، هیچ تغییری نمی‌دهد (چیزی برای جایگزینی
    پیدا نمی‌شود)."""
    if not name:
        return name
    out = name
    for pattern, fa in _PERSIAN_TERM_RE:
        out = pattern.sub(fa, out)
    return out
