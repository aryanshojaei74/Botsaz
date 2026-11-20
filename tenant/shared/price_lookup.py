"""price_lookup.py — «🔎 قیمت‌یاب» (PATCH_PRICE_LOOKUP_V1، درخواست سینا؛ نمونه: @PriceifyBot).

کاربر در پیوی یا گروه می‌نویسد «تون»، «5 تون»، «2 گرم طلا»، «5 دلار»، «سکه امامی»، «بیت کوین» و ربات قیمت دلاری و تومانی
(و برای کریپتو تغییر روزانه) را می‌فرستد؛ زیرش دکمه‌های شیشه‌ای که مالک تنظیم کرده.

قابلیت (نه نوع ربات): برای هر ربات جدا روشن/خاموش می‌شود (پیش‌فرض خاموش)؛ مسیر: پنل ← 🎛 دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب.

منابع:
  • دلار به تومان: نرخ زندهٔ تتر در والکس ← نوبیتکس (کش ۶۰ ثانیه)؛ اگر هیچ‌کدام نبود نرخ ذخیره‌شدهٔ سیستم.
  • کریپتو: CoinGecko (عمومی، بدون کلید) — قیمت دلاری + درصد تغییر ۲۴ ساعته.
  • طلا، سکه و ارزهای دیگر: BrsApi (با کلید «brsapi_key» ربات یا پلتفرم؛ بدون کلید، نسخهٔ رایگانش). اگر در دسترس نبود
    پیام «فعلاً در دسترس نیست» می‌رود و هیچ عدد ساختگی ساخته نمی‌شود.
فقط پیام‌هایی جواب می‌گیرند که دقیقاً «عدد + نام» یا «نام» یکی از دارایی‌ها باشند؛ گفتگوی عادی دست نمی‌خورد.
"""
from __future__ import annotations

import asyncio
import contextvars
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
price_router = Router(name="price_lookup")

ON_KEY = "pq_on"
BTN_KEY = "pq_buttons"
CACHE_SEC = 60

# key → (نوع، شناسه در منبع، عنوان نمایشی، ایموجی، نام‌ها)
ASSETS: dict[str, tuple[str, str, str, str, tuple[str, ...]]] = {
    "ton": ("crypto", "the-open-network", "GRAM (ton)", "📈", ("تون", "ton", "گرم تون", "gram", "تونکوین", "toncoin")),
    "btc": ("crypto", "bitcoin", "Bitcoin", "📈", ("بیت کوین", "بیتکوین", "btc", "bitcoin")),
    "eth": ("crypto", "ethereum", "Ethereum", "📈", ("اتریوم", "eth", "ethereum")),
    "usdt": ("crypto", "tether", "Tether (USDT)", "📈", ("تتر", "usdt", "tether")),
    "trx": ("crypto", "tron", "Tron (TRX)", "📈", ("ترون", "trx", "tron")),
    "not": ("crypto", "notcoin", "Notcoin", "📈", ("نات", "نات کوین", "notcoin", "not")),
    "sol": ("crypto", "solana", "Solana", "📈", ("سولانا", "sol", "solana")),
    "doge": ("crypto", "dogecoin", "Dogecoin", "📈", ("دوج", "دوج کوین", "doge", "dogecoin")),
    "bnb": ("crypto", "binancecoin", "BNB", "📈", ("بی ان بی", "bnb")),
    "usd": ("fiat", "دلار", "دلار آمریکا", "🇺🇸", ("دلار", "دلار آمریکا", "usd")),
    "eur": ("fiat", "یورو", "یورو", "🇪🇺", ("یورو", "eur", "euro")),
    "gbp": ("fiat", "پوند", "پوند انگلیس", "🇬🇧", ("پوند", "gbp")),
    "aed": ("fiat", "درهم", "درهم امارات", "🇦🇪", ("درهم", "aed")),
    "try": ("fiat", "لیر", "لیر ترکیه", "🇹🇷", ("لیر", "لیر ترکیه", "try")),
    "gold18": ("gold", "18", "طلای 18 عیار", "⚜️", ("طلا", "گرم طلا", "طلای 18", "طلای 18 عیار", "طلا 18")),
    "gold24": ("gold", "24", "طلای 24 عیار", "⚜️", ("طلای 24", "طلای 24 عیار", "طلا 24")),
    "emami": ("gold", "امامی", "سکه امامی", "🪙", ("سکه", "سکه امامی", "سکه تمام")),
    "nim": ("gold", "نیم", "نیم سکه", "🪙", ("نیم سکه",)),
    "rob": ("gold", "ربع", "ربع سکه", "🪙", ("ربع سکه",)),
    "ons": ("gold", "انس", "انس طلا", "🌐", ("انس", "انس طلا", "اونس", "اونس طلا")),
}
_UNIT_WORDS = ("عدد", "تا", "واحد", "گرم")
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm(t: str) -> str:
    t = (t or "").translate(_FA).replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("ِ", "")
    return " ".join(t.lower().split())


_NAME_TO_KEY: dict[str, str] = {}
for _k, (_t, _id, _title, _em, _names) in ASSETS.items():
    for _n in _names:
        _NAME_TO_KEY[_norm(_n)] = _k
_NUM = r"(\d+(?:[.,/٫]\d+)?)"


def parse(text: str | None) -> tuple[float, str] | None:
    """«5 تون» ⇒ (5, "ton")؛ «تون» ⇒ (1, "ton")؛ «2 گرم طلا» ⇒ (2, "gold18»)؛ متن عادی ⇒ None."""
    t = _norm(text or "")
    if not t or len(t) > 40:
        return None
    qty = 1.0
    m = re.fullmatch(_NUM + r"\s*(.+)", t) or None
    name = t
    if m:
        qty, name = float(m.group(1).replace(",", ".").replace("/", ".").replace("٫", ".")), m.group(2)
    else:
        m2 = re.fullmatch(r"(.+?)\s+" + _NUM, t)
        if m2:
            name, qty = m2.group(1), float(m2.group(2).replace(",", ".").replace("/", ".").replace("٫", "."))
    name = name.strip()
    key = _NAME_TO_KEY.get(name)
    if key is None:
        for w in _UNIT_WORDS:                        # «2 گرم طلا» / «3 عدد سکه»
            if name.startswith(w + " ") and name[len(w) + 1:] in _NAME_TO_KEY:
                key = _NAME_TO_KEY[name[len(w) + 1:]]
                break
    if key is None or qty <= 0 or qty > 10_000_000:
        return None
    return qty, key


# کلمه‌های رایج گفتگو که نباید به‌عنوان نماد ارز جواب بگیرند
_STOP = {"hi", "hello", "ok", "okay", "no", "yes", "the", "lol", "gm", "gn", "me", "you", "it", "is", "a", "an", "and", "or",
         "so", "go", "up", "hey", "bye", "thanks", "thx", "wow", "good", "nice", "love", "one", "all", "get", "any", "new",
         "now", "win", "test", "bot", "start", "help", "admin", "free", "pls", "plz", "my", "we", "in", "on", "of", "to"}
_PREFIX = re.compile(r"^(?:قیمت|price|/p|/price|\$)\s*(.+)$", re.I)


def parse_query(text: str | None):
    """None | ("asset", qty, key) | ("coin", qty, query, strict).
    strict=True: کلمهٔ لاتین بی‌پیشوند (مثلاً «not» در گفتگو) — فقط اگر نماد یکی از ۱۰۰۰ ارز برتر باشد جواب می‌گیرد."""
    from tenant.shared.coin_data import ADDR_RE
    p = parse(text)
    if p:
        return ("asset", p[0], p[1])
    raw = (text or "").strip()
    if not raw or len(raw) > 80 or "\n" in raw:
        return None
    strict = True
    m = _PREFIX.match(raw)
    if m:
        raw, strict = m.group(1).strip(), False
        p = parse(raw)                                  # «قیمت تون»
        if p:
            return ("asset", p[0], p[1])
    if ADDR_RE.match(raw) and (not strict or not raw.isalpha()):
        return ("coin", 1.0, raw, False)
    t = _norm(raw)
    qty = 1.0
    m = re.fullmatch(_NUM + r"\s*([a-z][a-z0-9._-]{0,29})", t) or re.fullmatch(r"([a-z][a-z0-9._-]{0,29})\s+" + _NUM, t)
    if m:
        a, b = m.group(1), m.group(2)
        num, name = (a, b) if a[0].isdigit() else (b, a)
        qty = float(num.replace(",", ".").replace("/", ".").replace("٫", "."))
    elif re.fullmatch(r"[a-z][a-z0-9._ -]{0,29}", t):
        name = t
    else:
        return None
    if qty <= 0 or qty > 1e12:
        return None
    if strict and (name in _STOP or " " in name or len(name) < 2 or len(name) > 12):
        return None
    return ("coin", qty, name, strict)


# ───────────────────────── منابع داده (با کش) ─────────────────────────
_CACHE: dict[str, tuple[float, object]] = {}


async def _cached(key: str, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    val = await fn()
    if val:
        _CACHE[key] = (time.time(), val)
    return val


async def _http_json(url: str):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)


async def _unused_crypto_quotes() -> dict:
    """{coingecko_id: (usd, change_pct_24h)}"""
    ids = ",".join(sorted({a[1] for a in ASSETS.values() if a[0] == "crypto"}))

    async def go():
        d = await _http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true")
        if not isinstance(d, dict):
            return None
        return {k: (float(v.get("usd") or 0), float(v.get("usd_24h_change") or 0)) for k, v in d.items() if isinstance(v, dict)}
    try:
        return await _cached("cg", go) or {}
    except Exception:  # noqa: BLE001
        logger.info("price_lookup: CoinGecko در دسترس نیست", exc_info=True)
        return {}


def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", "").replace("٬", "").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def parse_brs(data) -> list[dict]:
    """پاسخ BrsApi (طلا/ارز/…) ⇒ [{name, price, change}] با قیمت تومانی. ریال (عدد خیلی بزرگ) ÷ ۱۰ نمی‌شود چون BrsApi تومان می‌دهد؛
    فقط اگر فیلد unit «ریال» بود تقسیم می‌شود."""
    items = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                items += [x for x in v if isinstance(x, dict)]
    elif isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    out = []
    for it in items:
        name = str(it.get("name") or it.get("title") or "")
        price = _num(it.get("price") if it.get("price") is not None else it.get("value"))
        if not name or price is None:
            continue
        if "ریال" in str(it.get("unit") or ""):
            price /= 10
        out.append({"name": name, "price": price, "change": _num(it.get("change_percent")) if it.get("change_percent") is not None else None,
                    "unit": str(it.get("unit") or "")})
    return out


async def brs_items(bot_id: int) -> list[dict]:
    key = ""
    if bot_id:
        from tenant import get_setting
        key = (await get_setting(bot_id, "brsapi_key", "") or "").strip()
    if not key:
        try:
            from platform_bot import get_pset
            key = (await get_pset("brsapi_key", "") or "").strip()
        except Exception:  # noqa: BLE001
            key = ""
    urls = ([f"https://brsapi.ir/Api/Market/Gold_Currency.php?key={key}"] if key else []) + [
        "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency_v2.json"]

    async def go():
        for u in urls:
            try:
                rows = parse_brs(await _http_json(u))
                if rows:
                    return rows
            except Exception:  # noqa: BLE001
                logger.info("price_lookup: BrsApi پاسخ نداد (%s)", u.split("?")[0], exc_info=True)
        return None
    return await _cached(f"brs:{key[:6]}", go) or []


def find_brs(rows: list[dict], kind: str, ident: str) -> dict | None:
    """یافتن ردیف طلا/سکه/ارز بر اساس نام فارسی."""
    def has(n, *ws):
        return all(w in n for w in ws)
    for r in rows:
        n = _norm(r["name"])
        if kind == "fiat":
            if ident in n and "مبادله" not in n:
                return r
        elif ident == "18" and has(n, "طلا", "18"):
            return r
        elif ident == "24" and has(n, "طلا", "24"):
            return r
        elif ident == "امامی" and has(n, "سکه") and ("امامی" in n or "تمام" in n) and "نیم" not in n and "ربع" not in n:
            return r
        elif ident in ("نیم", "ربع") and has(n, ident, "سکه"):
            return r
        elif ident == "انس" and ("انس" in n or "اونس" in n):
            return r
    return None


# ───────────────────────── ساخت پیام ─────────────────────────
_RIAL = contextvars.ContextVar("pq_rial", default=False)   # «… به ریال» ⇒ مبلغ‌های ایرانی به ریال


def _tm(v: float) -> str:
    return f"{v * (10 if _RIAL.get() else 1):,.0f} {'ریال' if _RIAL.get() else 'تومان'}"


def _tm_label() -> str:
    return "قیمت ریالی" if _RIAL.get() else "قیمت تومانی"


_UNIT_RE = re.compile(r"\s*(?:به|بر حسب|برحسب)\s*(ریال|تومان|تومن)\s*[؟?]?\s*$")


def split_unit(text: str | None) -> tuple[str, bool]:
    """«قیمت 5 تون به ریال» ⇒ («قیمت 5 تون»، True)."""
    t = (text or "").strip()
    m = _UNIT_RE.search(t)
    if not m:
        return t, False
    return t[:m.start()].strip(), m.group(1) == "ریال"


HELP_TRIGGERS = {"راهنما قیمت", "راهنمای قیمت", "راهنمای قیمت یاب", "راهنما قیمت یاب", "قیمت", "/price", "/p",
                 "price", "help price", "راهنمای قیمتیاب", "قیمت یاب", "قیمتیاب"}
HELP_TEXT = ("🔎 <b>راهنمای قیمت‌یاب</b>\n\n"
             "🪙 <b>ارز دیجیتال</b>\n"
             "┤ <code>تون</code> · <code>5 تون</code> · <code>btc</code> · <code>2.5 eth</code> · <code>not</code>\n"
             "┤ هر ارز دیگر: <code>قیمت pepe</code> یا <code>$pepe</code>\n"
             "┙ توکن تازه: آدرس قرارداد (TON، اتریوم، BSC، سولانا، ترون، بیس)\n\n"
             "🏅 <b>طلا، سکه و ارز</b>\n"
             "┤ <code>2 گرم طلا</code> · <code>طلای 24</code> · <code>انس طلا</code>\n"
             "┤ <code>سکه امامی</code> · <code>نیم سکه</code> · <code>ربع سکه</code>\n"
             "┙ <code>5 دلار</code> · <code>100 یورو</code> · <code>درهم</code> · <code>لیر</code> · <code>پوند</code>\n\n"
             "💱 <b>به ریال یا تومان</b>\n"
             "┙ آخرش بنویس «به ریال»: <code>قیمت 5 تون به ریال</code> (پیش‌فرض تومان)\n\n"
             "📊 جواب: قیمت دلاری و تومانی، تغییر و سود و ضرر 24 ساعته، رتبه، سقف و کف، حجم، ساپلای، کپ و نمودار کندلی.")


def is_help(text: str | None) -> bool:
    return _norm(text or "").replace("‌", " ") in {_norm(x) for x in HELP_TRIGGERS}
def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:,.6f}".rstrip("0").rstrip(".")


def _fmt_q(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def _now_fa() -> str:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=3, minutes=30)
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=now).strftime("%Y/%m/%d | %H:%M:%S")
    except Exception:  # noqa: BLE001
        return now.strftime("%Y/%m/%d | %H:%M:%S")


def render(qty: float, key: str, usd: float | None, toman: float | None, change: float | None,
           usd_toman: float | None) -> str:
    kind, _id, title, em, _n = ASSETS[key]
    q = _fmt_q(qty)
    if kind == "gold":
        head = f"{em} قیمت {q} گرم {title}" if key.startswith("gold") else f"{em} قیمت {q} {title}"
    elif kind == "fiat":
        head = f"{em} {q} {title}"
    else:
        head = f"{em} قیمت {q} {title}" if qty != 1 else f"{em} قیمت {title}"
    lines = [f"<b>{head}</b>", "", "💰 جزئیات قیمت:"]
    lines.append(f"┤💵 قیمت دلاری: {_fmt_usd(usd) if usd else '—'}")
    lines.append(f"┙🇮🇷 {_tm_label()}: {_tm(toman) if toman else '—'}")
    if change is not None:
        up = change >= 0
        lines += ["", "📊 تغییرات روزانه:", f"┤{'🟢' if up else '🔴'} درصد تغییر: {change:+.2f}%"]
        if usd:
            d_usd = abs(usd - usd / (1 + change / 100))
            d_tm = f" / {_tm(d_usd * usd_toman)}" if usd_toman else ""
            lines.append(f"┙{'🔺 مقدار سود' if up else '🔻 مقدار ضرر'}: {_fmt_usd(d_usd)}{d_tm}")
    if _RATE_SRC.get():
        lines += ["", f"💱 نرخ دلار: {_RATE_SRC.get()}"]
    lines += ["", f"⏰ {_now_fa()}"]
    return "\n".join(lines)


def _big(x: float | None, usd: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:,.0f} $" if usd else f"{x:,.0f}"


def _p(x: float | None) -> str:
    from tenant.shared.price_chart import fmt_price
    return "—" if x is None else f"{fmt_price(x)} $"


def render_crypto(qty: float, coin, usd_toman: float | None, title: str = "") -> str:
    """متن کامل (مثل نمونهٔ سینا): قیمت دلاری/تومانی، تغییر و سود/ضرر، رتبه، سقف، کف، حجم، ساپلای، کپ."""
    q = _fmt_q(qty)
    name = title or (f"{coin.name} ({coin.symbol})" if coin.symbol and coin.symbol.lower() != coin.name.lower() else coin.name)
    usd = coin.price * qty
    head = f"🪙 قیمت {q} {name}" if qty != 1 else f"🪙 قیمت {name}"
    lines = [f"<b>{head}</b>", "", "💰 جزئیات قیمت:",
             f"┤💵 قیمت دلاری: {_fmt_usd(usd)}",
             f"┙🇮🇷 {_tm_label()}: {_tm(usd * usd_toman) if usd_toman else '—'}"]
    if qty != 1:
        lines.insert(4, f"┤🔹 هر واحد: {_fmt_usd(coin.price)}")
    stats = []
    if coin.rank:
        stats.append(f"🏆 رتبه: #{coin.rank}")
    if coin.change_pct is not None:
        up = coin.change_pct >= 0
        stats.append(f"{'🟢' if up else '🔴'} تغییرات: {coin.change_pct:+.2f}%")
        d_usd = abs(usd - usd / (1 + coin.change_pct / 100)) if coin.change_pct > -100 else 0
        d_tm = f" / {_tm(d_usd * usd_toman)}" if usd_toman else ""
        stats.append(f"{'🔺 مقدار سود' if up else '🔻 مقدار ضرر'}: {_fmt_usd(d_usd)}{d_tm}")
    if coin.high:
        stats.append(f"📈 سقف: {_p(coin.high)}")
    if coin.low:
        stats.append(f"📉 کف: {_p(coin.low)}")
    if coin.volume:
        stats.append(f"📊 حجم: {_big(coin.volume, True)}")
    if coin.supply:
        stats.append(f"🪙 ساپلای: {_big(coin.supply)}")
    if coin.cap:
        stats.append(f"💎 کپ: {_big(coin.cap, True)}")
    if coin.chain:
        stats.append(f"⛓ شبکه: {coin.chain}")
    if stats:
        lines += ["", "📊 آمار 24 ساعته:"] + [f"┤{x}" for x in stats[:-1]] + [f"┙{stats[-1]}"]
    if _RATE_SRC.get():
        lines += ["", f"💱 نرخ دلار: {_RATE_SRC.get()}"]
    lines += ["", f"⏰ {_now_fa()}"]
    return "\n".join(lines)


# ───────── نرخ زندهٔ دلار (PATCH_PRICE_LIVE_RATE_V1): تتر/تومان صرافی‌های داخلی، تازه هر ۶۰ ثانیه ─────────
# نرخ مشترک فروش (crypto_pay) یک ساعت کش و تا ۴۸ ساعت «کهنه ولی قابل‌استفاده» است؛ برای نمایش قیمت کافی نیست.
RATE_TTL = 60
_RATE_MIN, _RATE_MAX = 50_000, 5_000_000
_LIVE: dict = {"v": 0.0, "ts": 0.0, "src": ""}
_RATE_SRC: contextvars.ContextVar = contextvars.ContextVar("pq_rate_src", default="")


async def _rate_wallex() -> float:
    from tenant.gateways import crypto_pay
    return float(await crypto_pay._rate_from_wallex() or 0)


async def _rate_nobitex() -> float:
    d = await _http_json("https://apiv2.nobitex.ir/market/stats?srcCurrency=usdt&dstCurrency=rls")
    try:
        return float(str(d["stats"]["usdt-rls"]["latest"]).replace(",", "")) / 10     # ریال ⇒ تومان
    except (TypeError, KeyError, ValueError):
        return 0.0


RATE_SOURCES = [("والکس", _rate_wallex), ("نوبیتکس", _rate_nobitex)]


async def live_rate() -> tuple[float, str]:
    """(تومان به‌ازای یک دلار/تتر، نام منبع). ترتیب: والکس ← نوبیتکس ← نرخ ذخیره‌شدهٔ سیستم."""
    if _LIVE["v"] and time.time() - _LIVE["ts"] < RATE_TTL:
        return _LIVE["v"], _LIVE["src"]
    for name, fn in RATE_SOURCES:
        try:
            v = await asyncio.wait_for(fn(), timeout=6)
        except Exception:  # noqa: BLE001
            logger.info("price_lookup: نرخ %s در دسترس نیست", name, exc_info=True)
            continue
        if _RATE_MIN < v < _RATE_MAX:
            _LIVE.update(v=v, ts=time.time(), src=name)
            return v, name
    from tenant.gateways import crypto_pay
    try:
        v = float(await asyncio.wait_for(crypto_pay.get_usd_to_toman_rate(), timeout=10) or 0)
    except Exception:  # noqa: BLE001
        v = 0.0
    if _RATE_MIN < v < _RATE_MAX:
        return v, "نرخ ذخیره‌شده"
    if _LIVE["v"]:                                     # آخرین نرخ زندهٔ موفق، حتی اگر کمی قدیمی است
        return _LIVE["v"], _LIVE["src"]
    return 0.0, ""


async def _usd_toman() -> float | None:
    v, src = await live_rate()
    _RATE_SRC.set(f"{v:,.0f} تومان ({src})" if v else "")
    return v or None


def _card(coin, bot_username: str) -> bytes | None:
    import datetime as dt
    from tenant.shared.price_chart import render_card
    sub = " • ".join(x for x in (f"#{coin.rank}" if coin.rank else "", f"{coin.supply:,.0f} supply" if coin.supply else "",
                                 coin.chain, coin.candle_label) if x)
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3, minutes=30)
    title = f"{coin.name} ({coin.symbol.lower()})" if coin.symbol and len(coin.name) + len(coin.symbol) <= 22 else (coin.symbol or coin.name)
    ch_usd = (coin.price - coin.price / (1 + coin.change_pct / 100)) if coin.change_pct not in (None,) and coin.change_pct > -100 else None
    try:
        return render_card(title=title, subtitle=sub, price=coin.price, change_pct=coin.change_pct, change_usd=ch_usd,
                           candles=coin.candles, bot_username=bot_username, stamp=now.strftime("%d %b %Y • %H:%M"))
    except Exception:  # noqa: BLE001
        logger.warning("price_lookup: ساخت تصویر نمودار ناموفق", exc_info=True)
        return None


async def answer_for(bot_id: int, q, bot_username: str = "") -> tuple[str, bytes | None] | None:
    """q از parse_query. None ⇒ جواب نده (کلمهٔ گفتگو بود)."""
    from tenant.shared import coin_data
    if q[0] == "asset":
        _k, qty, key = q
        kind, ident, title, _em, _n = ASSETS[key]
        if kind != "crypto":
            return await quote(bot_id, qty, key), None
        coin = await coin_data.lookup(key, cg_id=ident)
        if coin is None:
            return f"⚠️ قیمت {title} فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.", None
    else:
        _k, qty, query, strict = q
        coin = await coin_data.lookup(query, strict=strict)
        if coin is None:
            return None if strict else ("⚠️ ارزی با این نام، نماد یا آدرس پیدا نشد (یا منبع قیمت فعلاً جواب نمی‌دهد).", None)
        title = ""
    text = render_crypto(qty, coin, await _usd_toman(), title=title)
    img = await asyncio.to_thread(_card, coin, bot_username) if coin.candles else None
    return text, img


async def quote(bot_id: int, qty: float, key: str) -> str:
    """متن پیام قیمت (هرگز استثنا نمی‌دهد)."""
    kind, ident, title, _em, _n = ASSETS[key]
    usd_toman = await _usd_toman()
    if kind == "crypto":
        from tenant.shared import coin_data
        coin = await coin_data.lookup(key, cg_id=ident, with_candles=False)
        if coin is None:
            return f"⚠️ قیمت {title} فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن."
        return render_crypto(qty, coin, usd_toman, title=title)
    if key == "usd":
        if not usd_toman:
            return "⚠️ نرخ دلار فعلاً در دسترس نیست."
        return render(qty, key, qty, qty * usd_toman, None, usd_toman)
    row = find_brs(await brs_items(bot_id), kind, ident)
    if not row:
        return f"⚠️ قیمت {title} فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن."
    if key == "ons":                                       # انس جهانی به دلار است
        usd = row["price"] * qty
        return render(qty, key, usd, usd * usd_toman if usd_toman else None, row.get("change"), usd_toman)
    toman = row["price"] * qty
    return render(qty, key, toman / usd_toman if usd_toman else None, toman, row.get("change"), usd_toman)


# ───────────────────────── تنظیمات مالک ─────────────────────────
class PqFSM(StatesGroup):
    buttons = State()


async def purchased(bot_id: int) -> bool:
    """PATCH_PRICE_LOOKUP_SALE_V1: پولی است؛ فقط ربات‌هایی که از ربات اصلی خریده‌اند (و مدیر غیرفعالش نکرده)."""
    try:
        import bot_addons
        return await bot_addons.price_active(bot_id)
    except Exception:  # noqa: BLE001
        logger.warning("price_lookup: بررسی خرید ممکن نشد", exc_info=True)
        return False


async def enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, ON_KEY, "0")) == "1" and await purchased(bot_id)


def parse_button_rows(raw: str) -> list[list[tuple[str, str]]]:
    """هر خط یک ردیف؛ چند دکمه در یک ردیف با «&&»: «متن | لینک [| رنگ] && …» (جزئیات: price_buttons)."""
    from tenant.shared import price_buttons as pb
    return [[(b[0], b[1]) for b in r] for r in pb.parse(raw)]


def parse_buttons(raw: str) -> list[tuple[str, str]]:
    return [b for r in parse_button_rows(raw) for b in r]


def rows_to_raw(rows) -> str:
    return "\n".join(" && ".join(f"{t} | {u}" for t, u in r) for r in rows)


async def buttons_kb(bot_id: int) -> InlineKeyboardMarkup | None:
    from tenant import get_setting
    from tenant.shared import price_buttons as pb
    return pb.keyboard(pb.parse(await get_setting(bot_id, BTN_KEY, "")))


async def _owner(event, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, event.from_user.id, row.owner_id):
        return None
    return row


async def _menu(bot_id: int, bot: Bot | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    if not await purchased(bot_id):
        return ("🔎 <b>قیمت‌یاب</b>\n\n"
                "کاربران ربات در پیوی یا گروه اسم، نماد یا آدرس هر ارز (و طلا، سکه، دلار) را می‌نویسند و ربات قیمت دلاری و "
                "تومانی، آمار 24 ساعته و تصویر نمودار کندلی را می‌فرستد؛ با دکمه‌های دلخواه زیر پیام.\n\n"
                "🔒 این قابلیت برای این ربات خریداری نشده است.\n"
                "خرید: ربات اصلی بات‌ساز ← ربات‌های من ← همین ربات ← «🔎 قیمت‌یاب» ← پرداخت و فعال‌سازی."), \
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    from tenant import get_setting as _gs
    on = (await _gs(bot_id, ON_KEY, "0")) == "1"
    btns = parse_buttons(await get_setting(bot_id, BTN_KEY, ""))
    text = ("🔎 <b>قیمت‌یاب</b>\n\n"
            "کاربر در پیوی یا گروه بنویسد «تون»، «5 تون»، «not»، «2 گرم طلا»، «5 دلار»، «سکه امامی» یا «قیمت pepe» یا آدرس "
            "قرارداد یک توکن، و ربات قیمت دلاری و تومانی، آمار 24 ساعته و نمودار کندلی را می‌فرستد.\n"
            "نماد لاتین تنها (مثل btc) فقط برای 1000 ارز برتر جواب می‌گیرد؛ برای بقیه «قیمت نام‌ارز» یا آدرس قرارداد.\n\n"
            f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n"
            f"دکمه‌های زیر پیام: <b>{len(btns)}</b>\n\n"
            "منبع ارز دیجیتال: CoinGecko و DexScreener (رایگان، با کش و منبع جایگزین).\n"
            "راهنمای کاربران: در گروه یا پیوی بنویسند «راهنما قیمت». «به ریال» در آخر پیام ⇒ مبلغ به ریال.\n"
            "قیمت طلا، سکه و ارزهای غیر دلار از BrsApi خوانده می‌شود؛ برای دقیق‌تر بودن کلید BrsApi را در تنظیمات ثبت کنید.")
    gtext, uname = await group_status(bot_id, bot)
    text += "\n\n" + gtext
    rows = [
        [InlineKeyboardButton(text=("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="pq:toggle",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔘 دکمه‌های زیر پیام (افزودن، رنگ، جابه‌جایی)", callback_data="pq:btns",
                              style=ButtonStyle.PRIMARY)]]
    if uname:
        rows.append([InlineKeyboardButton(text="➕ افزودن ربات به گروه (با دسترسی ادمین)",
                                          url=f"https://t.me/{uname}?startgroup=pq&admin=delete_messages",
                                          style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🔄 بررسی دوبارهٔ گروه‌ها", callback_data="pq:menu")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ───────── گروه‌های ربات (PATCH_PRICE_GROUP_STATUS_V1): ربات خودش گروه‌ها را تشخیص می‌دهد و وضعیت را نشان می‌دهد ─────────
GROUPS_KEY = "pq_groups"
MAX_GROUPS = 10
_SEEN_GROUPS: set[tuple[int, int]] = set()


def _load_groups(raw: str) -> dict[str, str]:
    import json
    try:
        d = json.loads(raw or "{}")
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def _save_group(bot_id: int, chat_id: int, title: str) -> None:
    import json
    from tenant import get_setting, set_setting
    groups = _load_groups(await get_setting(bot_id, GROUPS_KEY, ""))
    if groups.get(str(chat_id)) == title:
        return
    groups.pop(str(chat_id), None)
    groups[str(chat_id)] = title
    while len(groups) > MAX_GROUPS:
        groups.pop(next(iter(groups)))
    await set_setting(bot_id, GROUPS_KEY, json.dumps(groups, ensure_ascii=False))


def remember_group(msg: Message, bot: Bot) -> None:
    """هر بار اولین پیام یک گروه (در این پروسه) دیده شد، اگر قیمت‌یاب خریداری شده، گروه ذخیره شود. بدون انتظار."""
    chat = getattr(msg, "chat", None)
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    key = (getattr(bot, "id", 0), chat.id)
    if key in _SEEN_GROUPS:
        return
    _SEEN_GROUPS.add(key)

    async def go():
        try:
            from tenant import get_bot_row
            row = await get_bot_row(bot)
            if row and await purchased(row.id):
                await _save_group(row.id, chat.id, (chat.title or str(chat.id))[:60])
        except Exception:  # noqa: BLE001
            logger.info("price_lookup: ذخیرهٔ گروه ممکن نشد", exc_info=True)
    try:
        asyncio.get_running_loop().create_task(go())
    except RuntimeError:
        logger.debug("price_lookup: حلقه‌ای برای ذخیرهٔ گروه نیست")


async def group_status(bot_id: int, bot: Bot | None) -> tuple[str, str]:
    """متن وضعیت گروه‌ها + یوزرنیم ربات (برای دکمهٔ افزودن به گروه)."""
    if bot is None:
        return "", ""
    import json
    from tenant import get_setting, set_setting
    try:
        me = await bot.me()
    except Exception:  # noqa: BLE001
        return "", ""
    reads_all = bool(getattr(me, "can_read_all_group_messages", False))
    lines = ["👥 <b>گروه‌ها</b>",
             ("🔓 Group Privacy خاموش است: ربات پیام‌های همهٔ گروه‌هایش را می‌بیند." if reads_all else
              "🔒 Group Privacy روشن است: ربات فقط در گروه‌هایی که ادمین است پیام‌ها را می‌بیند "
              "(یا در @BotFather ← Bot Settings ← Group Privacy ← Turn off).")]
    groups = _load_groups(await get_setting(bot_id, GROUPS_KEY, ""))
    if not groups:
        lines.append("هنوز گروهی دیده نشده؛ ربات را به گروه اضافه کن و یک پیام در گروه بفرست، بعد «🔄 بررسی دوباره» را بزن.")
        return "\n".join(lines), me.username or ""
    changed = False
    for cid, title in list(groups.items())[:MAX_GROUPS]:
        try:
            m = await bot.get_chat_member(int(cid), me.id)
            st = str(getattr(m, "status", ""))
        except Exception:  # noqa: BLE001
            st = "error"
        st = st.split(".")[-1].lower()
        if st in ("administrator", "creator"):
            lines.append(f"• {title} — ✅ ادمین؛ جواب می‌دهد")
        elif st in ("member", "restricted"):
            lines.append(f"• {title} — " + ("✅ عضو؛ جواب می‌دهد" if reads_all else "⚠️ عضو ولی ادمین نیست؛ پیام‌ها را نمی‌بیند"))
        else:
            groups.pop(cid, None)
            changed = True
    if changed:
        await set_setting(bot_id, GROUPS_KEY, json.dumps(groups, ensure_ascii=False))
    if not groups:
        lines.append("ربات دیگر در گروه‌های قبلی نیست.")
    return "\n".join(lines), me.username or ""


async def _show(target, bot_id: int, bot: Bot | None = None):
    text, kb = await _menu(bot_id, bot)
    m = getattr(target, "message", None) or target
    try:
        if getattr(target, "message", None) is not None:
            await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
    except Exception:  # noqa: BLE001
        logger.debug("price_lookup: ویرایش منو ممکن نشد", exc_info=True)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@price_router.callback_query(F.data.in_({"pq:menu", "pq:toggle", "pq:btns"}))
async def pq_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _owner(cb, bot)
    if not row:
        await cb.answer("فقط مالک یا ادمین ربات", show_alert=True)
        return
    if cb.data != "pq:menu" and not await purchased(row.id):
        await cb.answer("🔒 اول باید از ربات اصلی بات‌ساز خریداری شود.", show_alert=True)
        return
    if cb.data == "pq:toggle":
        from tenant import get_setting
        await set_setting(row.id, ON_KEY, "0" if (await get_setting(row.id, ON_KEY, "0")) == "1" else "1")
    elif cb.data == "pq:btns":
        from tenant.shared import price_buttons as pb
        await pb.show_editor(cb.message, TenantStore(row, bot), "pqb", "pq:menu", True)
        await cb.answer()
        return
    elif cb.data == "pq:btns_text":
        await state.set_state(PqFSM.buttons)
        await cb.message.answer("دکمه‌های زیر پیام قیمت را بفرست. هر خط یک ردیف است و هر دکمه «متن | لینک»؛ برای چند دکمه "
                                "کنار هم در یک ردیف، بینشان <code>&&</code> بگذار (حداکثر 3 دکمه در ردیف، 8 ردیف). مثال:\n"
                                "<code>خرید استارز کف قیمت | https://t.me/mybot?start=stars</code>\n"
                                "<code>کانال ما | https://t.me/mychannel && پشتیبانی | https://t.me/support</code>\n\n"
                                "برای حذف همه: «پاک» · انصراف: /cancel", parse_mode="HTML")
        await cb.answer()
        return
    await _show(cb, row.id, bot)
    await cb.answer()


@price_router.message(PqFSM.buttons)
async def pq_buttons_input(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو"):
        await state.clear()
        await _show(msg, row.id, bot)
        return
    if raw == "پاک":
        await set_setting(row.id, BTN_KEY, "")
    else:
        rows = parse_button_rows(raw)
        if not rows:
            await msg.answer("❌ هیچ خط درستی پیدا نشد. هر خط: «متن | https://…». دوباره بفرست یا /cancel.")
            return
        await set_setting(row.id, BTN_KEY, rows_to_raw(rows))
        await msg.answer("پیش‌نمایش دکمه‌ها 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, url=u) for t, u in r] for r in rows]))
    await state.clear()
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id, bot)


# ───────────────────────── پاسخ به پیام کاربر ─────────────────────────
def looks_like(text: str | None) -> bool:
    """بررسی سریع و بدون شبکه/دیتابیس: آیا پیام درخواست قیمت یا راهنماست؟"""
    body, _r = split_unit(text)
    return is_help(body) or parse_query(body) is not None


async def maybe_answer(msg: Message, bot: Bot, bot_id: int) -> bool:
    """True ⇒ پیام، درخواست قیمت بود و جواب داده شد."""
    body, rial = split_unit(msg.text)
    help_req = is_help(body)
    q = None if help_req else parse_query(body)
    if (q is None and not help_req) or not await enabled(bot_id):
        return False
    return await answer_core(msg, bot, bot_id, q, rial, await buttons_kb(bot_id))


async def answer_core(msg: Message, bot, bot_id: int, q, rial: bool, kb) -> bool:
    """پاسخ مشترک ربات مشتری و گروه بات‌ساز اصلی (bot_id=0). q=None ⇒ راهنما."""
    if q is None:
        await msg.reply(HELP_TEXT, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return True
    _RIAL.set(rial)
    uname = ""
    try:
        uname = (await bot.me()).username or "" if bot is not None else ""
    except Exception:  # noqa: BLE001
        uname = ""
    res = await answer_for(bot_id, q, uname)
    if res is None:
        return False
    text, img = res
    if img:
        from aiogram.types import BufferedInputFile
        try:
            await msg.reply_photo(BufferedInputFile(img, "price.jpg"), caption=text, parse_mode="HTML", reply_markup=kb)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("price_lookup: ارسال تصویر ناموفق؛ فقط متن", exc_info=True)
    await msg.reply(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    return True


async def pq_open(msg: Message, bot: Bot):
    """ورودی منو از حالت کیبوردی (_settings_handlers)."""
    row = await _owner(msg, bot)
    if row:
        await _show(msg, row.id, bot)


# ───────────────────────── ویرایشگر دکمه‌ها (PATCH_PRICE_BUTTONS_EDITOR_V1) ─────────────────────────
from tenant.shared import price_buttons as _pb  # noqa: E402


class TenantStore(_pb.Store):
    def __init__(self, row, bot: Bot):
        self.row, self.bot = row, bot

    async def get(self) -> str:
        from tenant import get_setting
        return await get_setting(self.row.id, BTN_KEY, "")

    async def set(self, raw: str) -> None:
        from tenant import set_setting
        await set_setting(self.row.id, BTN_KEY, raw)

    async def main_buttons(self) -> list[tuple[str, str]]:
        """دکمه‌های صفحهٔ اصلی ربات ⇒ لینک t.me/<ربات>?start=mbtn_<id> (با زدن، همان دکمه در ربات باز می‌شود)."""
        from sqlalchemy import select
        from config import Session
        from models import MainButton
        uname = getattr(self.row, "username", "") or ""
        if not uname:
            try:
                uname = (await self.bot.me()).username or ""
            except Exception:  # noqa: BLE001
                uname = ""
        if not uname:
            return []
        async with Session() as s:
            rows = (await s.execute(select(MainButton.id, MainButton.label).where(MainButton.bot_id == self.row.id)
                                    .order_by(MainButton.row, MainButton.col))).all()
        return [(str(lbl)[:40], f"https://t.me/{uname}?start=mbtn_{bid}") for bid, lbl in rows if lbl]


@price_router.callback_query(F.data.startswith("pqb:"))
async def pqb_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        await cb.answer("فقط مالک یا ادمین ربات", show_alert=True)
        return
    if not await purchased(row.id):
        await cb.answer("🔒 اول باید از ربات اصلی بات‌ساز خریداری شود.", show_alert=True)
        return
    await _pb.on_cb(cb, state, TenantStore(row, bot), "pqb", "pq:menu", True)


@price_router.message(StateFilter(_pb.PbFSM.text, _pb.PbFSM.label, _pb.PbFSM.url))
async def pqb_msg(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    await _pb.on_msg(msg, state, TenantStore(row, bot), "pqb", "pq:menu", True)
