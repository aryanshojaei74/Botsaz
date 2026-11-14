"""کلاینت API سواپ‌ولت (SwapPay) برای شارژ خودکار کیف پول.

پایهٔ کار: هر ادمین مستاجر کلید API حساب سواپ‌ولت خودش را می‌دهد؛ همهٔ
تراکنش‌ها روی همان حساب انجام می‌شود. ما برای شارژ کیف پول کاربرِ ربات:
  ۱) یک «فاکتور/رسید» پرداخت می‌سازیم (create_payment) و لینکش را به کاربر می‌دهیم،
  ۲) با خواندن تراکنش‌ها (verify_payment / poll) پرداخت را تایید می‌کنیم و
     کیف پول را خودکار شارژ می‌کنیم.

نکتهٔ مهم: بخش «ساخت فاکتور/رسید» (create_payment) منتظر اسپک دقیق
گروه‌های «فاکتور» و «رسید» مستندات است (در فایل ذخیره‌شدهٔ مستندات این دو
گروه باز/رندر نشده بودند). بقیهٔ بخش‌ها (احراز هویت، موجودی، خواندن
تراکنش‌ها و تایید) کامل و بر پایهٔ اندپوینت‌های قطعی مستندات پیاده شده‌اند.

مستندات: https://docs.swapwallet.app/   (OAS 3.1.0)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import aiohttp  # از طریق aiogram موجود است

BASE_URL = "https://swapwallet.app/api"
DEFAULT_TIMEOUT = 20

# توکن‌های پشتیبانی‌شده طبق مستندات
TOKENS = ("USDT", "TON", "TRX", "IRT", "ETH", "BNB")
# ارزهای مرجع برای تعیین «ارزش»
QUOTE_UNITS = ("USDT", "IRT")


class SwapPayError(Exception):
    """خطای عمومی SwapPay (status=ERROR یا کد HTTP نامناسب)."""

    def __init__(self, message: str, *, http_status: int | None = None, payload=None):
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload


@dataclass
class Money:
    """مقدار پولی به شکل {number, unit} که در کل API استفاده می‌شود."""
    number: Decimal
    unit: str

    @classmethod
    def parse(cls, obj) -> "Money | None":
        if not obj:
            return None
        try:
            return cls(Decimal(str(obj.get("number", "0"))), obj.get("unit", ""))
        except Exception:
            return None


# === PATCH_SWAP_CLEAN_KEY_V1 ===
# کلیدی که از چت/تلگرام کپی می‌شود اغلب کاراکترِ نامرئی (فاصلهٔ صفر‌پهنا، علامتِ RTL/LTR، BOM)، کوتیشن یا «Bearer »
# دارد؛ هدرِ غیرِ‌ASCII باعثِ ۵۰۰ در سرورِ سواپ‌ولت می‌شد و ربات آن را «کلید نامعتبر» نشان می‌داد.
import re as _re

_INVISIBLE = _re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00a0\s]+")
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def clean_key(raw: str) -> str:
    k = (raw or "").translate(_FA_DIGITS)
    k = _INVISIBLE.sub("", k)
    k = k.strip("\"'`«»“”")
    if k.lower().startswith("bearer"):
        k = k[6:].lstrip(": ").strip()
    return k


def key_problem(key: str) -> str:
    """اگر کلید هنوز نامعتبر به‌نظر می‌رسد، علتِ فارسیِ آن؛ وگرنه ''."""
    if not key:
        return "کلید خالی است."
    if not key.isascii():
        bad = "".join(sorted({c for c in key if not c.isascii()}))[:6]
        return f"کلید کاراکترِ غیرانگلیسی دارد ({bad!r}). کلید را مستقیم از اپِ سواپ‌ولت کپی کن، نه از متنِ فارسی."
    if len(key) < 12:
        return "کلید خیلی کوتاه است؛ کلیدِ کامل (apikey-…) را بفرست."
    return ""


def _auth_headers(api_key: str) -> dict:
    key = clean_key(api_key)
    # کلید معمولاً به شکل apikey-xxx است؛ اگر کاربر فقط بدنه را داد، خودمان پیشوند را اضافه نمی‌کنیم
    # === PATCH_SWAP_BROWSER_UA_V1 ===
    # سایتِ سواپ‌ولت پشتِ لایهٔ محافظ است؛ هویتِ پیش‌فرضِ aiohttp ممکن است ۴۰۳/HTML بگیرد و (قبلاً) «کلید نامعتبر»
    # نمایش داده می‌شد. هویتِ مرورگری + Accept صریح می‌فرستیم؛ خودِ احرازِ هویت همان Bearer است.
    return {"Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


class SwapPayClient:
    """کلاینت سبک و async برای یک حساب سواپ‌ولت (یک کلید API)."""

    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.api_key = clean_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---------- لایهٔ پایه ----------
    async def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        url = self.base_url + path
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, params=params, json=json,
                                      headers=_auth_headers(self.api_key)) as resp:
                    text = await resp.text()
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if resp.status == 401:
                        raise SwapPayError("کلید API نامعتبر است یا منقضی شده.",
                                           http_status=401, payload=data or text)
                    if data is None:
                        _snip = " ".join((text or "").split())[:120]
                        raise SwapPayError(
                            f"پاسخ نامعتبر از سرور (HTTP {resp.status})" + (f": {_snip}" if _snip else "") + ".",
                            http_status=resp.status, payload=text)
                    if isinstance(data, dict) and data.get("status") == "ERROR":
                        msg = data.get("message") or data.get("error") or "خطای ناشناخته از SwapPay"
                        raise SwapPayError(str(msg), http_status=resp.status, payload=data)
                    if resp.status >= 400:
                        raise SwapPayError(f"خطای HTTP {resp.status}", http_status=resp.status, payload=data)
                    return data if isinstance(data, dict) else {"result": data}
        except asyncio.TimeoutError:
            raise SwapPayError("اتصال به سواپ‌ولت زمان‌بر شد (timeout).")
        except aiohttp.ClientError as e:
            raise SwapPayError(f"خطای شبکه در اتصال به سواپ‌ولت: {e}")

    # ---------- احراز هویت / موجودی (قطعی) ----------
    async def check_auth(self) -> bool:
        """صحت کلید API را بررسی می‌کند. GET /v1/utils/authentication/check

        === PATCH_SWAP_AUTH_FALLBACK_V1 ===
        گزارشِ #150: «نمی‌شود API اضافه کرد». کلید فقط با اندپوینتِ v1 سنجیده می‌شد و هر خطایی (حتی نبودنِ آن اندپوینت یا
        قطعیِ لحظه‌ایِ سرویس) «کلید نامعتبر» نشان داده می‌شد. حالا اگر v1 به‌جز ۴۰۱ خطایی داد، با اندپوینتِ
        موجودی (v2) هم امتحان می‌کنیم؛ ۴۰۱ همچنان یعنی کلید نامعتبر."""
        try:
            await self._request("GET", "/v1/utils/authentication/check")
            return True
        except SwapPayError as e1:
            if e1.http_status == 401:
                return False
            try:
                await self._request("GET", "/v2/user/balance")
                return True
            except SwapPayError as e2:
                if e2.http_status == 401:
                    return False
                raise e2 from e1

    async def check_auth_detail(self) -> tuple[bool, str]:
        """(ok, علت). علتِ دقیقِ ناموفق‌بودن (کلید نامعتبر / خطای شبکه / پاسخِ سرور) را برایِ ادمین برمی‌گرداند."""
        _pb = key_problem(self.api_key)
        if _pb:
            return False, _pb
        try:
            ok = await self.check_auth()
        except SwapPayError as e:
            code = f" (HTTP {e.http_status})" if e.http_status else ""
            return False, f"ارتباط با سواپ‌ولت ناموفق بود: {str(e)[:150]}{code}"
        return (True, "") if ok else (False, "کلید از طرفِ سواپ‌ولت رد شد (۴۰۱). کلید را دوباره از پنلِ سواپ‌ولت کپی کن.")

    async def get_balance(self) -> dict:
        """موجودی کل حساب به تفکیک توکن. GET /v2/user/balance"""
        return await self._request("GET", "/v2/user/balance")

    async def get_token_balance(self, token: str) -> dict:
        """موجودی یک توکن خاص. GET /v2/user/balance/{token}"""
        return await self._request("GET", f"/v2/user/balance/{token}")

    # ---------- تراکنش‌ها (قطعی) — مبنای تایید خودکار ----------
    async def get_transaction(self, tx_id: int | str) -> dict:
        """جزئیات یک تراکنش با شناسه. GET /v1/transaction/{id}"""
        return await self._request("GET", f"/v1/transaction/{tx_id}")

    async def list_transactions(self, *, types=None, tokens=None, networks=None,
                                 after_id=None, support_codes=None, query=None,
                                 sort_dir="DESC", limit=20) -> list[dict]:
        """لیست تراکنش‌ها با فیلتر. GET /v2/transaction

        types/tokens/networks/support_codes می‌توانند لیست باشند (چند مقدار).
        برای «Deposit Detector» هر ۱ دقیقه با limit=20 صدا بزن.
        """
        params: list[tuple[str, str]] = []

        def _add(key, val):
            if val is None:
                return
            if isinstance(val, (list, tuple)):
                for v in val:
                    params.append((key, str(v)))
            else:
                params.append((key, str(val)))

        _add("types", types)
        _add("tokens", tokens)
        _add("networks", networks)
        _add("supportCodes", support_codes)
        _add("afterId", after_id)
        _add("query", query)
        _add("sortDir", sort_dir)
        _add("limit", limit)
        data = await self._request("GET", "/v2/transaction", params=params)
        result = data.get("result") or {}
        return result.get("data", []) if isinstance(result, dict) else []

    async def find_successful_payment(self, *, support_code: str | None = None,
                                      tx_id: int | str | None = None,
                                      after_id: int | None = None) -> dict | None:
        """یک پرداخت موفق (type=PAYMENT) را پیدا می‌کند.

        روش تایید خودکار: بعد از ساخت فاکتور، support_code یا شناسهٔ تراکنش را
        نگه می‌داریم و این تابع را (با callback یا polling) صدا می‌زنیم؛ اگر
        تراکنش با وضعیت SUCCEED پیدا شد، پرداخت تایید‌شده است.
        """
        if tx_id is not None:
            tx = (await self.get_transaction(tx_id)).get("result") or {}
            if tx and tx.get("status") == "SUCCEED":
                return tx
            return None
        txs = await self.list_transactions(
            types=["PAYMENT"],
            support_codes=[support_code] if support_code else None,
            after_id=after_id, limit=50,
        )
        for tx in txs:
            if support_code and tx.get("supportCode") != support_code:
                continue
            if tx.get("status") == "SUCCEED":
                return tx
        return None

    # ---------- ساخت فاکتور/رسید پرداخت ----------
    # وضعیت: در مستنداتی که تا الان موجود است (webarchive و PDF) گروه‌های «فاکتور»
    # و «رسید» در نوار کناری دیده می‌شوند ولی جزئیات اندپوینتشان رندر/ثبت نشده بود.
    # هیچ اندپوینت «ساخت فاکتور برای پرداخت شخص ثالث» تایید نشده است. اگر این
    # گروه‌ها چنین قابلیتی داشتند، فقط کافی است مسیر و فیلدها را اینجا پر کنیم.
    INVOICE_CREATE_PATH = ""  # مثلاً "/v1/invoice" یا "/v1/receipt" — منتظر مستندات

    async def create_payment(self, *, value, value_unit: str = "IRT",
                             accepted_coin: str = "USDT", callback_url: str | None = None,
                             order_id: str | None = None, description: str | None = None) -> dict:
        """ساخت فاکتور/رسید پرداخت (در صورت وجود اندپوینت). تا تایید مستندات، خطای واضح می‌دهد."""
        if not self.INVOICE_CREATE_PATH:
            raise SwapPayError(
                "اندپوینت «ساخت فاکتور/رسید» در مستندات فعلی تایید نشده است. "
                "اگر گروه‌های «فاکتور/رسید» چنین قابلیتی دارند، مسیر و فیلدها را بده تا کامل شود. "
                "در غیر این صورت از روش «تشخیص واریز» (detect_incoming) استفاده کن."
            )
        body = {"value": {"number": str(value), "unit": value_unit}, "acceptedCoin": accepted_coin}
        if callback_url:
            body["callbackUrl"] = callback_url
        if order_id:
            body["orderId"] = order_id
        if description:
            body["description"] = description
        return await self._request("POST", self.INVOICE_CREATE_PATH, json=body)

    # ---------- بازار (قطعی) ----------
    async def get_prices(self) -> dict:
        """قیمت لحظه‌ای جفت‌ها به شکل {"TON/IRT": "235000", ...}. GET /v1/market/prices"""
        data = await self._request("GET", "/v1/market/prices")
        return data.get("result", data)

    # ---------- تشخیص واریز (روش شارژ خودکار قابل‌اجرا با همین API) ----------
    async def detect_incoming(self, *, after_id: int | None = None,
                              tokens=("USDT", "TON", "TRX"),
                              include_transfer: bool = True) -> list[dict]:
        """واریزهای موفق جدید به حساب را برمی‌گرداند (برای شارژ خودکار کیف پول).

        - فقط تراکنش‌های SUCCEED برمی‌گرداند.
        - after_id را برابر بزرگ‌ترین id پردازش‌شدهٔ قبلی بده تا فقط جدیدها بیایند.
        خروجی به ترتیب صعودی id است تا پردازش پشت‌سرهم ساده باشد.
        """
        types = ["CRYPTO_DEPOSIT"] + (["TRANSFER"] if include_transfer else [])
        txs = await self.list_transactions(types=types, tokens=list(tokens),
                                           after_id=after_id, sort_dir="ASC", limit=100)
        return [t for t in txs if t.get("status") == "SUCCEED"]


# ---------- کمک‌تابع‌های واحد پول ----------
def irt_value(tx: dict) -> Decimal | None:
    """ارزش ریالی (IRT) یک تراکنش را برمی‌گرداند (از userCurrencyValue هر بخش جزئیات)."""
    for key in ("payment", "cryptoDeposit", "transfer", "fiatDeposit", "credit", "trade"):
        d = tx.get(key)
        if isinstance(d, dict):
            m = Money.parse(d.get("userCurrencyValue"))
            if m and m.unit == "IRT":
                return m.number
    return None
