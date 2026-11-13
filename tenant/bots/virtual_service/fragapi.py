"""
fragapi.py — اتصال به «Fragment API» (https://fragment-api.com، سرویسِ غیررسمی) برایِ تحویلِ خودکارِ
استارز و پرمیومِ تلگرام. PATCH_FRAGAPI_V1

مرجع: OpenAPI رسمیِ سایت (api.fragment-api.com، نسخهٔ v1).
  • احراز: هدرِ  Authorization: JWT <token>  — توکنِ «Fragment Connection» از fragment-api.com/dashboard
  • POST /v1/order/stars/    {username, quantity≥50, show_sender, currency, response_url?}
  • POST /v1/order/premium/  {username, months∈{3,6,12}, show_sender, currency, response_url?}
  • GET  /v1/order/{id}/     وضعیتِ سفارش: CREATED|PENDING|BLOCKCHAIN_SENT|COMPLETED|FAILED
  • GET  /v1/misc/wallet/ ، /v1/misc/prices/ ، /v1/misc/user/{username}/

اصولِ ایمنی (پولِ واقعی خرج می‌شود) — همان قراردادِ TGTools/ولتِ فرگمنت:
  • خاموش به‌صورتِ پیش‌فرض؛ فقط «مالکِ ربات» توکن را می‌گذارد و روشن می‌کند؛ توکن رمزنگاری‌شده ذخیره می‌شود.
  • ادعایِ اتمیِ سفارش (awaiting_admin → auto_processing) و قفلِ per-bot ⇒ تحویلِ دوبار ممکن نیست.
  • این API کدِ پیگیری/idempotency ندارد؛ پس هیچ retryِ خودکارِ «خرید» وجود ندارد. پاسخِ نامشخص (timeout/قطعِ شبکه)
    ⇒ برگشت به دستی با هشدارِ «اول تراکنش‌هایِ داشبورد را ببین».
  • خطایِ قطعیِ قبل از پرداخت (یوزر پیدا نشد/KYC/ردِ فرگمنت/۴۰۰) ⇒ برگشت به دستی.
  • سقفِ هر سفارشِ خودکارِ استارز ۵٬۰۰۰ ⭐ (بالاتر دستی می‌ماند).
"""
from __future__ import annotations

from tenant.shared.owner_access import is_owner_uid as _is_owner_uid
import asyncio
import base64
import json as _json
import logging
import re
import time

from aiogram import Bot, F
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

logger = logging.getLogger(__name__)

BASE_URL = "https://api.fragment-api.com"
_TIMEOUT_READ = 20
_TIMEOUT_BUY = 150          # سفارشِ همگام (بدونِ webhook) تا تأییدِ بلاک‌چین طول می‌کشد
MAX_AUTO_STARS = 5000
MIN_STARS = 50
_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{2,31})")

# کدهایِ خطایِ مستند: این‌ها یعنی «قبل از پرداخت رد شد» (پولی خرج نشده)
_CLEAN_CODES = {"11", "20", "21"}
_CODE_FA = {
    "0": "خطایِ عمومیِ Fragment API",
    "10": "خطایِ شبکهٔ TON",
    "11": "فرگمنت برایِ این حساب احرازِ هویت (KYC) می‌خواهد",
    "12": "قطعیِ اتصال به شبکهٔ TON",
    "13": "خطایِ عمومیِ TON/تلگرام",
    "20": "این یوزرنیم در فرگمنت پیدا نشد",
    "21": "فرگمنت سفارش را نپذیرفت (مثلاً کاربر قبلاً پرمیوم دارد یا موجودی کافی نیست)",
}


class FragApiError(Exception):
    """پاسخِ خطایِ قطعی از سرور (سفارش ثبت نشده، یا طبقِ کد «قبل از پرداخت» رد شده)."""

    def __init__(self, message: str, status: int | None = None, codes: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.codes = codes or []

    @property
    def clean(self) -> bool:
        """مطمئنیم پولی خرج نشده؟ (۴xx با کدِ صریحِ «قبل از پرداخت»، یا ۴۲۹/۴۰۱/۴۰۳)."""
        if self.status in (401, 403, 429):
            return True
        return bool(self.codes) and all(c in _CLEAN_CODES for c in self.codes)


class FragApiAmbiguous(Exception):
    """درخواستِ خرید فرستاده شد ولی پاسخِ قطعی نگرفتیم؛ ممکن است سفارش انجام شده باشد."""


# ───────────────────────── کلاینت ─────────────────────────
_USERNAME_LINE_RE = re.compile(r"یوزرنیم\s*:\s*@([A-Za-z][A-Za-z0-9_]{2,31})")


def extract_username(dest_label: str | None) -> str | None:
    """یوزرنیمِ گیرنده. اول خطِ «یوزرنیم: @x»؛ وگرنه اولین @ در متن
    (قبلاً اگر نامِ نمایشیِ مشتری خودش @ داشت، همان به‌جای یوزرنیمِ واقعی برداشته می‌شد)."""
    txt = dest_label or ""
    m = _USERNAME_LINE_RE.search(txt) or _USERNAME_RE.search(txt)
    return m.group(1) if m else None


def _errors_of(data) -> tuple[str, list[str]]:
    """خروجیِ {errors:[{error,code}]} را به (متن، کدها) تبدیل می‌کند."""
    msgs, codes = [], []
    if isinstance(data, dict):
        errs = data.get("errors")
        if errs is None and isinstance(data.get("error"), dict):
            errs = data["error"].get("errors")
        if isinstance(errs, list):
            for e in errs:
                if isinstance(e, dict):
                    c = str(e.get("code")) if e.get("code") is not None else ""
                    if c:
                        codes.append(c)
                    m = str(e.get("error") or "").strip() or _CODE_FA.get(c, "")
                    if m:
                        msgs.append(m)
        elif data.get("detail"):
            msgs.append(str(data["detail"]))
    return ("؛ ".join(msgs) or "درخواست ناموفق بود"), codes


async def _call(method: str, path: str, token: str | None = None, *, body: dict | None = None,
                timeout: int = _TIMEOUT_READ, buy: bool = False):
    """بدنهٔ JSON را برمی‌گرداند. buy=True ⇒ خطایِ شبکه/timeout = FragApiAmbiguous (نباید retry شود)."""
    if aiohttp is None:  # pragma: no cover
        raise FragApiError("aiohttp نصب نیست")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, BASE_URL + path, headers=headers, json=body) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                    if status < 400 and buy:
                        raise FragApiAmbiguous(f"پاسخِ غیرِ JSON (HTTP {status})")
    except FragApiAmbiguous:
        raise
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        if buy:
            raise FragApiAmbiguous(f"{type(e).__name__}: {e}") from e
        raise FragApiError(f"اتصال به Fragment API برقرار نشد ({type(e).__name__})") from e
    if status >= 400:
        msg, codes = _errors_of(data)
        if status == 401 or status == 403:
            msg = "توکن نامعتبر/منقضی است؛ در fragment-api.com/dashboard اتصالِ تازه بساز."
        elif status == 429:
            msg = "سقفِ تعدادِ درخواست پر شده؛ کمی بعد دوباره امتحان کن."
        elif status >= 500 and buy:
            raise FragApiAmbiguous(f"خطایِ سرور (HTTP {status})")
        raise FragApiError(msg, status=status, codes=codes)
    return data


async def get_wallet(token: str) -> dict:
    return await _call("GET", "/v1/misc/wallet/", token) or {}


async def get_prices(token: str | None = None) -> dict:
    return await _call("GET", "/v1/misc/prices/", token) or {}


async def get_user(token: str, username: str):
    """dict اگر کاربر در فرگمنت هست؛ None اگر ۴۰۴."""
    try:
        return await _call("GET", f"/v1/misc/user/{username}/", token)
    except FragApiError as e:
        if e.status == 404:
            return None
        raise


async def get_order(token: str, order_id: str) -> dict:
    return await _call("GET", f"/v1/order/{order_id}/", token) or {}


async def buy_stars(token: str, username: str, quantity: int) -> dict:
    return await _call("POST", "/v1/order/stars/", token, buy=True, timeout=_TIMEOUT_BUY,
                       body={"username": username, "quantity": int(quantity), "show_sender": False, "currency": "ton"}) or {}


async def gift_premium(token: str, username: str, months: int) -> dict:
    return await _call("POST", "/v1/order/premium/", token, buy=True, timeout=_TIMEOUT_BUY,
                       body={"username": username, "months": int(months), "show_sender": False, "currency": "ton"}) or {}


def norm_status(order: dict) -> str:
    """'completed' | 'failed' | 'pending' (CREATED/PENDING/BLOCKCHAIN_SENT)."""
    o = order or {}
    st = str(o.get("status") or "").upper()
    if st == "COMPLETED":
        return "completed"
    if st == "FAILED":
        return "failed"
    if not st and o.get("success") is True:       # فقط اگر status اصلاً نیامده؛ «success» به‌تنهایی برایِ سفارشِ در جریان گمراه‌کننده است
        return "completed"
    if not st and o.get("success") is False:
        return "failed"
    return "pending"


def order_error(order: dict) -> tuple[str, list[str]]:
    return _errors_of({"errors": ((order or {}).get("error") or {}).get("errors")}) if isinstance(
        (order or {}).get("error"), dict) else ("", [])


def token_expiry(token: str) -> int | None:
    """exp (ثانیهٔ یونیکس) از payloadِ JWT؛ فقط همین فیلد خوانده می‌شود (بقیهٔ payload دست‌نخورده می‌ماند)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(part)).get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def looks_like_jwt(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", tok or ""))


def jwt_diag(tok: str) -> str:
    """خلاصهٔ ساختاریِ توکن برایِ عیب‌یابی (بدونِ نمایشِ خودِ توکن): طولِ بخش‌ها، الگوریتم، انقضا، سالم‌بودنِ امضا."""
    try:
        parts = (tok or "").split(".")
        if len(parts) != 3:
            return f"ساختار: {len(parts)} بخش (باید ۳ بخش باشد)"

        def dec(x):
            return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        hdr = _json.loads(dec(parts[0]))
        alg = str(hdr.get("alg", "?"))
        out = [f"طولِ کل {len(tok):,} نویسه؛ بخش‌ها {len(parts[0]):,}/{len(parts[1]):,}/{len(parts[2]):,}؛ الگوریتم {alg}"]
        try:
            exp = int(_json.loads(dec(parts[1])).get("exp", 0))
            if exp:
                left = (exp - time.time()) / 86400
                out.append(f"انقضا: {int(left)} روزِ دیگر" if left >= 0 else "انقضا: ⚠️ گذشته")
        except Exception:
            out.append("بدنهٔ توکن خوانده نشد (احتمالاً ناقص/به‌هم‌ریخته)")
        sig = len(dec(parts[2]))
        if alg == "RS256" and sig not in (256, 384, 512):
            out.append(f"⚠️ امضا {sig} بایت است؛ برایِ RS256 باید ۲۵۶/۳۸۴/۵۱۲ باشد ⇒ توکن ناقص یا اضافه دارد")
        elif alg == "HS256" and sig != 32:
            out.append(f"⚠️ امضا {sig} بایت است؛ برایِ HS256 باید ۳۲ باشد ⇒ توکن ناقص یا اضافه دارد")
        return "؛ ".join(out)
    except Exception:
        return "ساختارِ توکن قابلِ خواندن نبود (ناقص یا به‌هم‌ریخته)"


def mask(tok: str) -> str:
    return (tok[:8] + "…" + tok[-6:]) if len(tok) > 20 else "…"


# ───────────────────────── تنظیمات/کلید ─────────────────────────
def _S():
    from tenant.bots.virtual_service import smm as S
    return S


async def token_of(bot_id: int) -> str:
    S = _S()
    enc = await S._get_setting(bot_id, "fragapi_jwt", "")
    if not enc:
        return ""
    try:
        from wallet_crypto import decrypt_secret
        return decrypt_secret(enc)
    except Exception:
        logger.exception("fragapi: رمزگشاییِ توکن ناموفق بود")
        return ""


async def auto_on(bot_id: int) -> bool:
    S = _S()
    try:
        return (await S._get_setting(bot_id, "fragapi_auto", "0")) == "1" and bool(await S._get_setting(bot_id, "fragapi_jwt", ""))
    except Exception:
        logger.exception("fragapi: خواندنِ تنظیمات ناموفق (غیرفعال فرض شد)")
        return False


async def premium_on(bot_id: int) -> bool:
    S = _S()
    try:
        if not await S._get_setting(bot_id, "fragapi_jwt", ""):
            return False
        v = await S._get_setting(bot_id, "fragapi_premium", "")
        if v == "":                      # هرگز دست نخورده ⇒ همان وضعیتِ «استارزِ خودکار» (پیش‌فرضِ منطقی)
            return (await S._get_setting(bot_id, "fragapi_auto", "0")) == "1"
        return v == "1"
    except Exception:
        logger.exception("fragapi: خواندنِ تنظیمات ناموفق (غیرفعال فرض شد)")
        return False


# ───────────────────────── تحویل: استارز / پرمیوم ─────────────────────────
_running: set[int] = set()
_tasks: set = set()
_locks: dict[int, asyncio.Lock] = {}
_POLL_DELAYS = [4, 6, 8, 10, 15, 20, 30, 30, 30, 30, 30, 30]      # ≈ ۴ دقیقه


def spawn(bot: Bot, row, order_id: int, kind: str, by_admin: bool = False) -> None:
    t = asyncio.create_task(fulfil(bot, row, order_id, kind, by_admin=by_admin))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _revert(bot: Bot, row, order_id: int, kind: str, reason: str, *, warn: bool = False) -> None:
    S = _S()
    fn = S._tgt_revert_manual if kind == "stars" else S._tgt_revert_manual_prm
    await fn(bot, row, order_id, reason, warn=warn)


async def _finalize(bot: Bot, row, order_id: int, kind: str, order: dict) -> str:
    """'completed' | 'failed' | 'pending'. failed ⇒ سفارش به دستی برمی‌گردد."""
    S = _S()
    st = norm_status(order)
    if st == "pending":
        return "pending"
    if st == "completed":
        async with S.Session() as s:
            o = await s.get(S.GiftOrder, order_id, with_for_update=True)
            if not o or o.bot_id != row.id or o.status != "auto_processing":
                return "completed"
            o.status = "sent"
            buyer = await s.get(S.BotUser, o.buyer_user_id)
            buyer_tg_id = buyer.telegram_id if buyer else None
            trk, qty = o.tracking_code, o.star_count
            months = o.gift_id.split("_")[-1] if o.gift_id else "?"
            await s.commit()
        await S._notify_gift_order_trust_success(bot, row, order_id)
        what = f"استارز ({qty:,} ⭐)" if kind == "stars" else f"پرمیوم ({months} ماهه)"
        ref = str(order.get("ref_id") or "")
        await S._notify_manual_order(
            bot, row, f"✅ سفارشِ {what} #{await S.order_no(S.GiftOrder, order_id)} با Fragment API تحویل داده شد."
            + (f"\nشناسهٔ تراکنش: <code>{S._html_escape(ref)}</code>" if ref else ""))
        if buyer_tg_id:
            try:
                body = (f"⭐ تعداد {qty:,} استارز با موفقیت ارسال شد." if kind == "stars"
                        else f"🌟 تلگرام پرمیومِ {months} ماهه فعال شد.")
                await bot.send_message(
                    buyer_tg_id, f"✅ سفارش شما با موفقیت تکمیل شد! 🎉\n\n{body}\n🔖 کد پیگیری: <code>{trk}</code>",
                    parse_mode="HTML", reply_markup=await S._gift_support_kb(trk))
            except Exception:
                logger.exception("fragapi: خبر به مشتری ناموفق بود")
        return "completed"
    msg, codes = order_error(order)
    await _revert(bot, row, order_id, kind,
                  "❌ Fragment API سفارش را ناموفق اعلام کرد." + (f"\nعلت: {msg}" if msg else ""),
                  warn=not (codes and all(c in _CLEAN_CODES for c in codes)))
    return "failed"


async def fulfil(bot: Bot, row, order_id: int, kind: str, *, by_admin: bool = False) -> None:
    S = _S()
    if order_id in _running:
        return
    _running.add(order_id)
    try:
        lock = _locks.setdefault(row.id, asyncio.Lock())
        async with lock:
            token = await token_of(row.id)
            if not token:
                if by_admin:
                    from tenant import _notify_admins
                    await _notify_admins(bot, row, "⚠️ توکنِ Fragment API ثبت نشده — از «ارائه‌دهندگانِ من ← 🔗 Fragment API» ثبتش کن.")
                return
            prefix = "starsell_" if kind == "stars" else "premium_"
            async with S.Session() as s:                         # ادعایِ اتمیِ سفارش
                o = await s.get(S.GiftOrder, order_id, with_for_update=True)
                if (not o or o.bot_id != row.id or not str(o.gift_id).startswith(prefix)
                        or o.status != "awaiting_admin"):
                    return
                o.status = "auto_processing"
                qty, dest_label, gid = int(o.star_count or 0), o.dest_label, str(o.gift_id)
                await s.commit()
            username = extract_username(dest_label)
            if not username:
                await _revert(bot, row, order_id, kind, "مقصدِ این سفارش یوزرنیم ندارد و Fragment API فقط با یوزرنیم می‌فرستد.")
                return
            if kind == "stars":
                if qty < MIN_STARS or qty > MAX_AUTO_STARS:
                    await _revert(bot, row, order_id, kind,
                                  f"تعدادِ {qty:,} استارز خارج از بازهٔ ارسالِ خودکار ({MIN_STARS}–{MAX_AUTO_STARS:,}) است؛ دستی بفرست.")
                    return
            else:
                try:
                    months = int(gid.split("_")[-1])
                except ValueError:
                    months = 0
                if months not in (3, 6, 12):
                    await _revert(bot, row, order_id, kind, "مدتِ پرمیوم نامعتبر است (فقط ۳/۶/۱۲ ماه).")
                    return
            try:
                res = (await buy_stars(token, username, qty) if kind == "stars"
                       else await gift_premium(token, username, months))
            except FragApiAmbiguous as e:
                await _revert(bot, row, order_id, kind,
                              f"پاسخِ Fragment API نامشخص بود ({e}). ممکن است سفارش انجام و TON خرج شده باشد!\n"
                              "⛔ قبل از هر کاری در fragment-api.com/dashboard بخشِ Transactions و پروفایلِ گیرنده را چک کن؛ "
                              "اگر انجام شده، دستی نفرست.", warn=True)
                return
            except FragApiError as e:
                logger.warning("fragapi: خرید رد شد: %s %s", e.status, e.codes)
                _hint = ""
                if "can't gift" in (e.message or "").lower() or "cannot gift" in (e.message or "").lower():
                    # PATCH_FRAG_RECIPIENT_HINT_V1: خطا از سمتِ گیرنده است، نه قطعیِ API
                    _hint = ("\n\nℹ️ این یعنی فرگمنت برایِ همین گیرنده (حسابِ مقصد) اجازهٔ هدیه‌دادنِ استارز نمی‌دهد؛ "
                             "API قطع نیست. معمولاً حسابِ مقصد محدود/جدید است یا تنظیماتِ حریمِ خصوصی دارد. "
                             "با یک گیرندهٔ دیگر امتحان کنید یا همین سفارش را دستی بفرستید.")
                await _revert(bot, row, order_id, kind, f"Fragment API خرید را رد کرد: {e.message}{_hint}", warn=not e.clean)
                return
            oid = str(res.get("id") or "")
            if oid:
                await S._set_setting(row.id, f"fragapi_tx_{order_id}", oid)
            st = norm_status(res)
            if st != "pending":
                await _finalize(bot, row, order_id, kind, res)
                return
            if not oid:
                await _revert(bot, row, order_id, kind,
                              "پاسخِ Fragment API در حالتِ نامشخص بود و شناسهٔ سفارش نداشت؛ ممکن است انجام شده باشد. "
                              "تراکنش‌هایِ داشبورد را چک کن.", warn=True)
                return
            for d in _POLL_DELAYS:
                await asyncio.sleep(d)
                try:
                    cur = await get_order(token, oid)
                except FragApiError:
                    continue
                if await _finalize(bot, row, order_id, kind, cur) != "pending":
                    return
            await S._notify_manual_order(
                bot, row,
                f"⏳ سفارش #{await S.order_no(S.GiftOrder, order_id)} هنوز در Fragment API نهایی نشده (شناسه {oid}). "
                "با دکمهٔ زیر وضعیت را بررسی کن؛ تا آن موقع دستی نفرست.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="🔄 بررسیِ وضعیت", callback_data=f"fapi:chk:{kind[0]}:{order_id}")]]))
    except Exception:
        logger.exception("fragapi: خطایِ پیش‌بینی‌نشده در سفارش %s", order_id)
        try:
            await _revert(bot, row, order_id, kind,
                          "خطایِ داخلی رخ داد. اگر مطمئن نیستی خرید انجام شده یا نه، اول داشبوردِ fragment-api.com را چک کن.",
                          warn=True)
        except Exception:
            logger.exception("fragapi: برگرداندن به دستی هم ناموفق بود")
    finally:
        _running.discard(order_id)


# ───────────────────────── منویِ ادمین ─────────────────────────
async def menu_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    S = _S()
    token = await token_of(bot_id)
    auto = (await S._get_setting(bot_id, "fragapi_auto", "0")) == "1"
    prem = await premium_on(bot_id)
    lines = ["🔗 <b>Fragment API — تحویلِ خودکارِ استارز و پرمیوم</b>\n"]
    if token:
        lines.append(f"🔑 توکن: <code>{mask(token)}</code> ✅")
        exp = token_expiry(token)
        if exp:
            days = int((exp - time.time()) // 86400)
            lines.append(f"⏳ اعتبارِ توکن: {days} روزِ دیگر" if days >= 0 else "⚠️ توکن منقضی شده؛ اتصالِ تازه بساز.")
        try:
            w = await get_wallet(token)
            bal = ", ".join(f"{b.get('amount')} {str(b.get('currency')).upper()}" for b in (w.get("balances") or []))
            lines.append(f"💰 موجودیِ ولتِ متصل: <b>{bal or '—'}</b>")
        except FragApiError as e:
            lines.append(f"⚠️ استعلامِ موجودی ناموفق: {e.message}")
    else:
        lines.append("🔑 توکن: ثبت نشده ❌\n\nدر fragment-api.com/dashboard ← Fragment Connections یک اتصال بساز "
                     "(ولتِ TON + اکانتِ تلگرام) و توکنِ JWT را اینجا بفرست.")
    lines.append(f"\n🚀 استارزِ خودکار (سفارش‌هایِ پرداخت‌شده با کیف‌پول): <b>{'روشن ✅' if auto else 'خاموش'}</b>")
    lines.append(f"🌟 پرمیومِ خودکار: <b>{'روشن ✅' if prem else 'خاموش'}</b>")
    lines.append("\n• هزینه از ولتِ TONِ همان اتصال کم می‌شود؛ ولت را فقط به‌اندازهٔ نیاز شارژ کن.\n"
                 "• هر خطا ⇒ سفارش به «تحویلِ دستی» برمی‌گردد و خبرت می‌کنم.\n"
                 "• پاسخِ نامشخص (timeout) هرگز خودکار تکرار نمی‌شود؛ اول داشبورد را چک کن.\n"
                 f"• سقفِ هر سفارشِ استارزِ خودکار: {MAX_AUTO_STARS:,} ⭐ (حداقل {MIN_STARS}).\n"
                 "• قیمتِ فروش همان فرمولِ فعلیِ ربات است؛ این بخش فقط تحویل را خودکار می‌کند.")
    rows = [[InlineKeyboardButton(text=("🔑 تغییرِ توکن" if token else "🔑 ثبتِ توکن"), callback_data="fapi:key")]]
    if token:
        rows.append([InlineKeyboardButton(text=("🚀 استارزِ خودکار: روشن ✅" if auto else "🚀 استارزِ خودکار: خاموش"),
                                          callback_data="fapi:auto", style=(ButtonStyle.SUCCESS if auto else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(text=("🌟 پرمیومِ خودکار: روشن ✅" if prem else "🌟 پرمیومِ خودکار: خاموش"),
                                          callback_data="fapi:prem", style=(ButtonStyle.SUCCESS if prem else ButtonStyle.PRIMARY))])
        rows.append([InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data="fapi:menu")])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ توکن", callback_data="fapi:del", style=ButtonStyle.DANGER)])
    if (await S._get_setting(bot_id, "fragment_old_hidden", "0")) == "1":
        rows.append([InlineKeyboardButton(text="👁 نمایشِ دوبارهٔ «اتصال به فرگمنت» قدیمی", callback_data="fapi:fgshow")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="smm:panels", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id: int, edit: bool = True) -> None:
    text, kb = await menu_content(bot_id)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
        await target.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        m = target.message if isinstance(target, CallbackQuery) else target
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


def register(router) -> None:
    """هندلرها را رویِ smm_router می‌نشاند (از انتهایِ smm.py صدا زده می‌شود)."""
    from tenant.bots.virtual_service import smm as S

    @router.callback_query(F.data == "fapi:menu")
    async def fapi_menu(cb: CallbackQuery, bot: Bot):
        row = await S._owner(cb, bot)
        if not row:
            return
        await _show(cb, row.id)
        await cb.answer()

    @router.callback_query(F.data == "fapi:key")
    async def fapi_key_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
        row = await S._tgt_owner_strict(cb, bot)
        if not row:
            return
        from tenant import smart_cancel_kb
        await state.set_state(S.SmmFSM.fragapi_key_in)
        await cb.message.answer(
            "🔑 توکنِ JWTِ «Fragment Connection» را بفرست (از fragment-api.com/dashboard، دکمهٔ کپیِ کنارِ Token).\n"
            "اگر توکن آن‌قدر بلند است که تلگرام تکه‌تکه‌اش می‌کند، همان تکه‌ها را به ترتیب بفرست (جمع می‌شوند)، یا آن را در یک فایلِ <code>.txt</code> بفرست.\n"
            "پیامِ تو بعد از دریافت پاک می‌شود و توکن رمزنگاری‌شده ذخیره می‌شود.\n\n"
            "⚠️ این توکن اجازهٔ خرج‌کردن از ولتِ متصل را می‌دهد؛ جایِ دیگری (گروه/چتِ پشتیبانی) نفرست و ولت را کم‌موجودی نگه دار.",
            parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))
        await cb.answer()

    _UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

    async def _take_token_piece(msg: Message, bot: Bot, state: FSMContext, piece: str) -> None:
        """توکنِ JWT بلند است و تلگرام پیامِ بالای ۴۰۹۶ نویسه را تکه‌تکه می‌فرستد؛ تکه‌ها جمع می‌شوند تا توکنِ کامل شود."""
        from tenant import get_bot_row
        row = await get_bot_row(bot)
        if not row or not _is_owner_uid(msg.from_user.id, row.owner_id):
            await state.clear()
            return
        try:
            await msg.delete()
        except Exception:
            logger.debug("fragapi: حذفِ پیامِ توکن ممکن نشد", exc_info=True)
        data = await state.get_data()
        buf = (data.get("fapi_buf") or "") + re.sub(r"\s+", "", piece or "")
        if buf.upper().startswith("JWT"):
            buf = buf[3:].lstrip(":")
        if _UUID_RE.fullmatch(buf):
            await state.clear()
            await msg.answer(
                "❌ این یک کلیدِ <b>UUID</b> است (مثلاً کلیدِ API سایتِ دیگری مثل TGTools)، نه توکنِ Fragment API.\n\n"
                "• اگر کلیدِ TGTools است: از «ارائه‌دهندگانِ من ← TGTools» واردش کن.\n"
                "• توکنِ Fragment API یک رشتهٔ بلند و سه‌بخشی است که با <code>eyJ</code> شروع می‌شود؛ از "
                "fragment-api.com/dashboard ← Fragment Connections یک اتصال بساز و «JWT Token» را کپی کن.",
                parse_mode="HTML")
            return
        if len(buf) > 30000 or (len(buf) >= 20 and not buf.startswith("eyJ")):
            await state.clear()
            await msg.answer("❌ توکنِ نامعتبر (باید سه بخشِ نقطه‌دار باشد و با eyJ شروع شود). دوباره از منو امتحان کن.")
            return
        if not looks_like_jwt(buf):
            await state.update_data(fapi_buf=buf)
            await msg.answer(f"📥 بخشی از توکن دریافت شد ({len(buf):,} نویسه). اگر توکن چند تکه شده، تکهٔ بعدی را (به ترتیب) بفرست.")
            return
        try:
            await get_wallet(buf)
        except FragApiError as e:
            await state.update_data(fapi_buf=buf)
            await msg.answer(
                f"⚠️ تا اینجا ({len(buf):,} نویسه) Fragment API قبول نکرد: {e.message}\n"
                f"🔎 {jwt_diag(buf)}\n\n"
                "اگر توکن هنوز تمام نشده، تکهٔ بعدی را بفرست؛ وگرنه «لغو» را بزن و از اولِ توکن دوباره شروع کن.")
            return
        await state.clear()
        try:
            from wallet_crypto import encrypt_secret
            await S._set_setting(row.id, "fragapi_jwt", encrypt_secret(buf))
        except Exception:
            logger.exception("fragapi: ذخیرهٔ توکن ناموفق")
            await msg.answer("⛔️ ذخیرهٔ رمزنگاری‌شده ممکن نشد (WALLET_ENC_KEY در .env تنظیم است؟).")
            return
        await msg.answer("✅ توکن ذخیره شد و اتصال تأیید شد.")
        await _show(msg, row.id, edit=False)

    @router.message(S.SmmFSM.fragapi_key_in, F.text)
    async def fapi_key_save(msg: Message, bot: Bot, state: FSMContext):
        await _take_token_piece(msg, bot, state, msg.text or "")

    @router.message(S.SmmFSM.fragapi_key_in, F.document)
    async def fapi_key_file(msg: Message, bot: Bot, state: FSMContext):
        """راهِ ساده‌تر: توکن را در یک فایلِ متنی (.txt) بفرست."""
        doc = msg.document
        if doc.file_size and doc.file_size > 64 * 1024:
            await msg.answer("❌ فایل خیلی بزرگ است (توکن باید یک فایلِ متنیِ کوچک باشد).")
            return
        try:
            buf = await bot.download(doc.file_id)
            txt = buf.read().decode("utf-8", "ignore")
        except Exception:
            logger.exception("fragapi: دانلودِ فایلِ توکن ناموفق")
            await msg.answer("❌ فایل خوانده نشد؛ دوباره بفرست یا توکن را متنی بفرست.")
            return
        await _take_token_piece(msg, bot, state, txt)

    async def _flip(cb: CallbackQuery, bot: Bot, key: str, on_txt: str, off_txt: str):
        row = await S._tgt_owner_strict(cb, bot)
        if not row:
            return
        if not await token_of(row.id):
            await cb.answer("اول توکن را ثبت کن.", show_alert=True)
            return
        cur = (await premium_on(row.id)) if key == "fragapi_premium" else (await S._get_setting(row.id, key, "0")) == "1"
        await S._set_setting(row.id, key, "0" if cur else "1")
        await cb.answer(off_txt if cur else on_txt, show_alert=True)
        await _show(cb, row.id)

    @router.callback_query(F.data == "fapi:fgshow")
    async def fapi_fgshow(cb: CallbackQuery, bot: Bot):
        row = await S._tgt_owner_strict(cb, bot)
        if not row:
            return
        await S._set_setting(row.id, "fragment_old_hidden", "0")
        await cb.answer("دوباره در لیست نمایش داده می‌شود.", show_alert=True)
        await _show(cb, row.id)

    @router.callback_query(F.data == "fapi:auto")
    async def fapi_auto(cb: CallbackQuery, bot: Bot):
        await _flip(cb, bot, "fragapi_auto", "روشن شد — سفارش‌هایِ استارزِ پرداخت‌شده با کیف‌پول خودکار می‌روند.",
                    "خاموش شد؛ استارز دستی/روشِ دیگر است.")

    @router.callback_query(F.data == "fapi:prem")
    async def fapi_prem(cb: CallbackQuery, bot: Bot):
        await _flip(cb, bot, "fragapi_premium", "روشن شد — پرمیومِ پرداخت‌شده با Fragment API خریده می‌شود.",
                    "خاموش شد؛ پرمیوم مثلِ قبل است.")

    @router.callback_query(F.data == "fapi:del")
    async def fapi_del(cb: CallbackQuery, bot: Bot):
        row = await S._tgt_owner_strict(cb, bot)
        if not row:
            return
        for k in ("fragapi_jwt", "fragapi_auto", "fragapi_premium"):
            await S._set_setting(row.id, k, "")
        await cb.answer("توکن حذف و ارسالِ خودکار خاموش شد.", show_alert=True)
        await _show(cb, row.id)

    @router.callback_query(F.data.regexp(r"^fapi:go:(s|p):\d+$"))
    async def fapi_go(cb: CallbackQuery, bot: Bot):
        row = await S._owner(cb, bot)
        if not row:
            return
        _, _, k, oid = cb.data.split(":")
        kind = "stars" if k == "s" else "premium"
        order_id = int(oid)
        if not await token_of(row.id):
            await cb.answer("اول توکنِ Fragment API را ثبت کن.", show_alert=True)
            return
        async with S.Session() as s:
            o = await s.get(S.GiftOrder, order_id)
        if not o or o.bot_id != row.id or o.status != "awaiting_admin":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        await cb.answer("🤖 در حالِ ارسال با Fragment API… نتیجه اینجا اعلام می‌شود.", show_alert=True)
        spawn(bot, row, order_id, kind, by_admin=True)
        try:
            await cb.message.delete()
        except Exception:
            logger.info("fragapi: حذفِ پیامِ سفارش ممکن نشد")

    @router.callback_query(F.data.regexp(r"^fapi:chk:(s|p):\d+$"))
    async def fapi_chk(cb: CallbackQuery, bot: Bot):
        row = await S._owner(cb, bot)
        if not row:
            return
        _, _, k, oid = cb.data.split(":")
        kind = "stars" if k == "s" else "premium"
        order_id = int(oid)
        token = await token_of(row.id)
        tx = await S._get_setting(row.id, f"fragapi_tx_{order_id}", "")
        async with S.Session() as s:
            o = await s.get(S.GiftOrder, order_id)
        if not token or not tx or not o or o.bot_id != row.id:
            await cb.answer("اطلاعاتِ این سفارش پیدا نشد.", show_alert=True)
            return
        if o.status != "auto_processing":
            await cb.answer("این سفارش قبلاً نهایی شده.", show_alert=True)
            return
        try:
            cur = await get_order(token, tx)
        except FragApiError as e:
            await cb.answer(f"خطا در استعلام: {e.message}"[:190], show_alert=True)
            return
        res = await _finalize(bot, row, order_id, kind, cur)
        await cb.answer({"completed": "✅ تحویل داده شده", "failed": "❌ ناموفق اعلام شد — پیامِ ادمین را ببین",
                         "pending": "⏳ هنوز در صف است"}[res], show_alert=True)
