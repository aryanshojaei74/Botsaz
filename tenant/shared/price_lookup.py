"""price_lookup.py — «🔎 قیمت‌یاب» (PATCH_PRICE_LOOKUP_V1، درخواست سینا؛ نمونه: @PriceifyBot).

کاربر در پیوی یا گروه می‌نویسد «تون»، «5 تون»، «2 گرم طلا»، «5 دلار»، «سکه امامی»، «بیت کوین» و ربات قیمت دلاری و تومانی
(و برای کریپتو تغییر روزانه) را می‌فرستد؛ زیرش دکمه‌های شیشه‌ای که مالک تنظیم کرده.

قابلیت (نه نوع ربات): برای هر ربات جدا روشن/خاموش می‌شود (پیش‌فرض خاموش)؛ مسیر: پنل ← 🎛 دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب.

منابع:
  • دلار به تومان: همان crypto_pay.get_usd_to_toman_rate (BrsApi / والکس / …) که برای فروش هم استفاده می‌شود.
  • کریپتو: CoinGecko (عمومی، بدون کلید) — قیمت دلاری + درصد تغییر ۲۴ ساعته.
  • طلا، سکه و ارزهای دیگر: BrsApi (با کلید «brsapi_key» ربات یا پلتفرم؛ بدون کلید، نسخهٔ رایگانش). اگر در دسترس نبود
    پیام «فعلاً در دسترس نیست» می‌رود و هیچ عدد ساختگی ساخته نمی‌شود.
فقط پیام‌هایی جواب می‌گیرند که دقیقاً «عدد + نام» یا «نام» یکی از دارایی‌ها باشند؛ گفتگوی عادی دست نمی‌خورد.
"""
from __future__ import annotations

import asyncio
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
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
    lines.append(f"┙🇮🇷 قیمت تومانی: {f'{toman:,.0f} تومان' if toman else '—'}")
    if change is not None:
        up = change >= 0
        lines += ["", "📊 تغییرات روزانه:", f"┤{'🟢' if up else '🔴'} درصد تغییر: {change:+.2f}%"]
        if usd:
            d_usd = abs(usd - usd / (1 + change / 100))
            d_tm = f" / {d_usd * usd_toman:,.0f} تومان" if usd_toman else ""
            lines.append(f"┙{'🔺 مقدار سود' if up else '🔻 مقدار ضرر'}: {_fmt_usd(d_usd)}{d_tm}")
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
             f"┙🇮🇷 قیمت تومانی: {f'{usd * usd_toman:,.0f} تومان' if usd_toman else '—'}"]
    if qty != 1:
        lines.insert(4, f"┤🔹 هر واحد: {_fmt_usd(coin.price)}")
    stats = []
    if coin.rank:
        stats.append(f"🏆 رتبه: #{coin.rank}")
    if coin.change_pct is not None:
        up = coin.change_pct >= 0
        stats.append(f"{'🟢' if up else '🔴'} تغییرات: {coin.change_pct:+.2f}%")
        d_usd = abs(usd - usd / (1 + coin.change_pct / 100)) if coin.change_pct > -100 else 0
        d_tm = f" / {d_usd * usd_toman:,.0f} تومان" if usd_toman else ""
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
    lines += ["", f"⏰ {_now_fa()}"]
    return "\n".join(lines)


async def _usd_toman() -> float | None:
    from tenant.gateways import crypto_pay
    try:
        return await asyncio.wait_for(crypto_pay.get_usd_to_toman_rate(), timeout=10) or None
    except Exception:  # noqa: BLE001
        return None


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
    from tenant.gateways import crypto_pay
    kind, ident, title, _em, _n = ASSETS[key]
    try:
        usd_toman = await asyncio.wait_for(crypto_pay.get_usd_to_toman_rate(), timeout=10)
    except Exception:  # noqa: BLE001
        usd_toman = None
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
    """هر خط یک ردیف؛ چند دکمه در یک ردیف با «&&» جدا می‌شوند: «متن | لینک && متن2 | لینک2». فقط https/http/tg."""
    rows = []
    for line in (raw or "").splitlines():
        row = []
        for part in line.split("&&"):
            if "|" not in part:
                continue
            text, url = (x.strip() for x in part.split("|", 1))
            if text and re.match(r"^(https?://|tg://)", url):
                row.append((text[:40], url[:300]))
        if row:
            rows.append(row[:3])
    return rows[:8]


def parse_buttons(raw: str) -> list[tuple[str, str]]:
    return [b for r in parse_button_rows(raw) for b in r]


def rows_to_raw(rows) -> str:
    return "\n".join(" && ".join(f"{t} | {u}" for t, u in r) for r in rows)


async def buttons_kb(bot_id: int) -> InlineKeyboardMarkup | None:
    from tenant import get_setting
    rows = parse_button_rows(await get_setting(bot_id, BTN_KEY, ""))
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, url=u) for t, u in r] for r in rows])


async def _owner(event, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, event.from_user.id, row.owner_id):
        return None
    return row


async def _menu(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
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
            "قیمت طلا، سکه و ارزهای غیر دلار از BrsApi خوانده می‌شود؛ برای دقیق‌تر بودن کلید BrsApi را در تنظیمات ثبت کنید.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="pq:toggle",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔘 دکمه‌های زیر پیام قیمت", callback_data="pq:btns", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    return text, kb


async def _show(target, bot_id: int):
    text, kb = await _menu(bot_id)
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
        await state.set_state(PqFSM.buttons)
        await cb.message.answer("دکمه‌های زیر پیام قیمت را بفرست. هر خط یک ردیف است و هر دکمه «متن | لینک»؛ برای چند دکمه "
                                "کنار هم در یک ردیف، بینشان <code>&&</code> بگذار (حداکثر 3 دکمه در ردیف، 8 ردیف). مثال:\n"
                                "<code>خرید استارز کف قیمت | https://t.me/mybot?start=stars</code>\n"
                                "<code>کانال ما | https://t.me/mychannel && پشتیبانی | https://t.me/support</code>\n\n"
                                "برای حذف همه: «پاک» · انصراف: /cancel", parse_mode="HTML")
        await cb.answer()
        return
    await _show(cb, row.id)
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
        await _show(msg, row.id)
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
    await _show(msg, row.id)


# ───────────────────────── پاسخ به پیام کاربر ─────────────────────────
async def maybe_answer(msg: Message, bot: Bot, bot_id: int) -> bool:
    """True ⇒ پیام، درخواست قیمت بود و جواب داده شد."""
    q = parse_query(msg.text)
    if q is None or not await enabled(bot_id):
        return False
    uname = ""
    try:
        uname = (await bot.me()).username or "" if bot is not None else ""
    except Exception:  # noqa: BLE001
        uname = ""
    res = await answer_for(bot_id, q, uname)
    if res is None:
        return False
    text, img = res
    kb = await buttons_kb(bot_id)
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
        await _show(msg, row.id)
