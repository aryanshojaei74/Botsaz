"""
tgtools_api.py — کلاینتِ REST برایِ «TGTools» (https://tg-tools.shop) جهتِ
ارسالِ خودکارِ استارزِ تلگرام. مرجع: https://tg-tools.shop/api-docs

=== PATCH_TGTOOLS_STARS_V1 ===
نکته‌هایِ مستند که این کلاینت رعایت می‌کند:
  • احرازِ هویت: هدرِ X-Api-Key با کلیدِ tgt_...  |  Base: https://api.tg-tools.shop
  • پاسخِ ممکن است {isSuccess, message, data} باشد یا مستقیم خودِ payload؛
    هر دو حالت پشتیبانی می‌شود.
  • خریدِ استارز صف‌بندی می‌شود: POST /api/purchase/stars → status=Pending،
    بعد باید GET /api/purchase/{id} را پول کرد تا Completed یا Failed شود
    (Failed یعنی پول به کیف‌پولِ TGTools برگشته).
  • trackingCode: اگر همان کد قبلاً کامل شده باشد سفارشِ تکراری رد می‌شود.

عمداً هیچ retry خودکاری برایِ «خرید» وجود ندارد (فقط برایِ خواندنِ وضعیت)،
چون پایِ پولِ واقعی وسط است.
"""
from __future__ import annotations

import logging
import re

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

logger = logging.getLogger(__name__)
BASE_URL = "https://api.tg-tools.shop"
_TIMEOUT = 20

_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")


class TgToolsError(Exception):
    """خطایِ قطعیِ API (پاسخِ خطا از سمتِ سرور) — یعنی سفارش ثبت نشده."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class TgToolsAmbiguous(Exception):
    """درخواست فرستاده شد ولی پاسخِ قطعی نگرفتیم (timeout/قطعِ شبکه).
    سفارش ممکن است ثبت شده باشد — نباید خودکار دوباره تلاش شود."""


def extract_username(dest_label: str | None) -> str | None:
    """یوزرنیمِ مقصد را از برچسبِ مقصدِ سفارش (مثلاً «👤 یوزرنیم: @name») درمی‌آورد."""
    m = _USERNAME_RE.search(dest_label or "")
    return m.group(1) if m else None


def _unwrap(data):
    if isinstance(data, dict) and "isSuccess" in data:
        if not data.get("isSuccess"):
            raise TgToolsError(str(data.get("message") or "درخواست ناموفق بود"))
        return data.get("data")
    return data


def _err_text(data, status: int) -> str:
    if isinstance(data, dict):
        for k in ("message", "error", "detail", "title"):
            if data.get(k):
                return str(data[k])
    return f"HTTP {status}"


async def _request(api_key: str | None, method: str, path: str, *,
                   json_body: dict | None = None, params: dict | None = None,
                   sent_is_ambiguous: bool = False):
    if aiohttp is None:
        raise TgToolsError("کتابخانهٔ aiohttp نصب نیست.")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, BASE_URL + path, headers=headers,
                                 json=json_body, params=params) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
    except Exception as e:  # noqa: BLE001 — timeout / DNS / reset
        if sent_is_ambiguous:
            raise TgToolsAmbiguous(f"{type(e).__name__}") from e
        raise TgToolsError(f"ارتباط با TGTools برقرار نشد ({type(e).__name__})") from e
    if method == "POST":
        # PATCH_TGTOOLS_RAW_LOG_V1: شکلِ واقعیِ پاسخِ خرید در مستند نیست؛ بدنهٔ پاسخ (بدونِ کلید) در لاگ می‌ماند تا قابلِ بررسی باشد
        logger.info("tgtools %s %s → HTTP %s %s", method, path, status, str(data)[:500])
    if status >= 500 and sent_is_ambiguous:
        raise TgToolsAmbiguous(f"HTTP {status}")
    if status >= 400:
        raise TgToolsError(_err_text(data, status), status)
    return _unwrap(data)


# ───────────── عمومی/بدونِ کلید ─────────────

async def get_prices() -> dict:
    return await _request(None, "GET", "/api/purchase/prices") or {}


async def lookup_user(username: str) -> dict:
    return await _request(None, "GET", f"/api/purchase/user/{username.lstrip('@')}") or {}


# ───────────── نیازمندِ کلید ─────────────

async def get_wallet(api_key: str) -> dict:
    return await _request(api_key, "GET", "/api/wallet") or {}


async def buy_stars(api_key: str, username: str, amount: int, tracking_code: str) -> dict:
    body = {"recipientUsername": username.lstrip("@"), "amount": int(amount),
            "paymentMethod": "ton", "trackingCode": tracking_code}
    return await _request(api_key, "POST", "/api/purchase/stars",
                          json_body=body, sent_is_ambiguous=True) or {}


async def buy_premium(api_key: str, username: str, months: int, tracking_code: str) -> dict:
    """=== PATCH_TGTOOLS_PREMIUM_V1 === مستند: months ∈ {3,6,12}، paymentMethod=ton، trackingCode یکتا (ایدمپوتنسی)."""
    body = {"recipientUsername": username.lstrip("@"), "months": int(months),
            "paymentMethod": "ton", "trackingCode": tracking_code}
    return await _request(api_key, "POST", "/api/purchase/premium",
                          json_body=body, sent_is_ambiguous=True) or {}


def premium_price_ton(prices: dict, months: int) -> float | None:
    """قیمتِ TONِ پرمیوم از خروجیِ get_prices (کلیدهایِ premium_3m_ton / 6m / 12m)."""
    try:
        v = float((prices or {}).get(f"premium_{int(months)}m_ton"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


async def get_order(api_key: str, transaction_id: int | str) -> dict:
    return await _request(api_key, "GET", f"/api/purchase/{transaction_id}") or {}


def norm_status(order: dict) -> str:
    """'completed' | 'failed' | 'pending' (هر چیزِ غیرِ نهایی)."""
    st = str((order or {}).get("status") or "").strip().lower()
    if st == "completed":
        return "completed"
    if st == "failed":
        return "failed"
    return "pending"


# ───────────── گیفتِ تلگرام (PATCH_TGTOOLS_GIFT_V1) ─────────────
# مستند: GET /api/Purchase/gifts?lang= → [{giftId, name, emoji, stars, priceTon, soldOut, remainingCount}]
#        POST /api/Purchase/gift {recipientUsername, giftId, message?, hideSenderName?, trackingCode?}
#        (پاسخ و پولِ وضعیت مثلِ استارز/پرمیوم: transactionId + status Pending → GET /api/purchase/{id})

async def get_gifts(api_key: str | None = None, lang: str = "en") -> list:
    res = await _request(api_key, "GET", "/api/Purchase/gifts", params={"lang": lang})
    return res if isinstance(res, list) else []


def find_gift(gifts: list, gift_id) -> dict | None:
    gid = str(gift_id)
    for g in gifts or []:
        if isinstance(g, dict) and str(g.get("giftId")) == gid:
            return g
    return None


async def buy_gift(api_key: str, username: str, gift_id, tracking_code: str, *,
                   message: str = "", hide_sender: bool = False) -> dict:
    body = {"recipientUsername": username.lstrip("@"), "giftId": str(gift_id),
            "hideSenderName": bool(hide_sender), "trackingCode": tracking_code}
    if message:
        body["message"] = message[:128]
    return await _request(api_key, "POST", "/api/Purchase/gift",
                          json_body=body, sent_is_ambiguous=True) or {}
