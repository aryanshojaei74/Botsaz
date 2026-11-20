"""coin_data.py — دادهٔ ارز دیجیتال برای قیمت‌یاب (PATCH_PRICE_LOOKUP_V2، سینا: «مثل PriceifyBot همهٔ ارزها + نمودار»).

جست‌وجو با نام، نماد یا آدرس قرارداد؛ همه از APIهای رایگان و بدون کلید، با کش و منبع جایگزین:
  1) CoinGecko  — جست‌وجو، قیمت، رتبه، سقف/کف، حجم، ساپلای، مارکت‌کپ، کندل ۳۰ دقیقه‌ای ۲۴ ساعت.
  2) DexScreener — توکن‌های تازه و آدرس قرارداد (TON، اتریوم، BSC، سولانا، ترون، بیس …).
  3) GeckoTerminal — کندل برای توکن‌هایی که از DexScreener آمده‌اند.
  4) CoinMarketCap — فقط اگر «cmc_api_key» در تنظیمات پلتفرم ثبت شده باشد، منبع جایگزین قیمت وقتی CoinGecko جواب نداد.
هیچ عددی ساخته نمی‌شود: اگر هیچ منبعی جواب نداد ⇒ None.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

CG = "https://api.coingecko.com/api/v3"
DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
CMC = "https://pro-api.coinmarketcap.com"

TTL_QUOTE = 60
TTL_SEARCH = 6 * 3600
TTL_CANDLES = 300

# آدرس قرارداد
ADDR_RE = re.compile(r"^(?:(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}|0x[a-fA-F0-9]{40}|T[1-9A-HJ-NP-Za-km-z]{33}|[1-9A-HJ-NP-Za-km-z]{32,44})$")
# شبکه‌های DexScreener ⇒ GeckoTerminal
GT_NET = {"ton": "ton", "ethereum": "eth", "bsc": "bsc", "solana": "solana", "base": "base", "tron": "tron",
          "polygon": "polygon_pos", "arbitrum": "arbitrum", "avalanche": "avax", "optimism": "optimism"}


@dataclass
class Coin:
    name: str
    symbol: str
    price: float
    change_pct: float | None = None
    rank: int | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    supply: float | None = None
    cap: float | None = None
    chain: str = ""
    source: str = ""
    candles: list = field(default_factory=list)        # [(ts_sec, o, h, l, c)]
    candle_label: str = ""


_CACHE: dict[str, tuple[float, object]] = {}


def _get_c(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], True
    return None, False


def _put_c(key: str, val):
    if val:
        _CACHE[key] = (time.time(), val)
        if len(_CACHE) > 3000:                      # سقف حافظه
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:1000]:
                _CACHE.pop(k, None)
    return val


async def _json(url: str, headers: dict | None = None):
    import aiohttp
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    h.update(headers or {})
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url, headers=h) as r:
                if r.status != 200:
                    logger.info("coin_data: %s ⇒ HTTP %s", url.split("?")[0], r.status)
                    return None
                return await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        logger.info("coin_data: درخواست ناموفق %s", url.split("?")[0], exc_info=True)
        return None


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if x == x else None                  # NaN
    except (TypeError, ValueError):
        return None


# ───────────────────────── CoinGecko ─────────────────────────
async def cg_search(query: str) -> list[dict]:
    q = query.strip().lower()
    val, ok = _get_c(f"cgs:{q}", TTL_SEARCH)
    if ok:
        return val
    from urllib.parse import quote
    d = await _json(f"{CG}/search?query={quote(q)}")
    coins = (d or {}).get("coins") if isinstance(d, dict) else None
    return _put_c(f"cgs:{q}", [c for c in (coins or []) if isinstance(c, dict)]) or []


def pick_cg(coins: list[dict], query: str, strict: bool) -> str | None:
    """بهترین id: نماد دقیق با بهترین رتبه؛ در حالت strict (کلمهٔ لاتین بی‌پیشوند در گفتگو) فقط نماد دقیق با رتبهٔ ≤ ۱۰۰۰."""
    q = query.strip().lower()
    exact = [c for c in coins if str(c.get("symbol", "")).lower() == q or str(c.get("name", "")).lower() == q]
    ranked = sorted(exact, key=lambda c: c.get("market_cap_rank") or 10 ** 9)
    if strict:
        ranked = [c for c in ranked if (c.get("market_cap_rank") or 10 ** 9) <= 1000]
        return ranked[0]["id"] if ranked else None
    if ranked:
        return ranked[0]["id"]
    return coins[0]["id"] if coins else None


async def top_map() -> dict[str, str]:
    """نماد/نام ⇒ id برای ۱۰۰۰ ارز برتر (۴ درخواست در هر ۶ ساعت). نمادهای بی‌پیشوند گفتگو فقط با این جواب می‌گیرند،
    پس کلمه‌های عادی گروه به API نمی‌روند."""
    val, ok = _get_c("top", TTL_SEARCH)
    if ok:
        return val
    m: dict[str, str] = {}
    for page in (1, 2, 3, 4):
        d = await _json(f"{CG}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}")
        if not isinstance(d, list):
            break
        for c in d:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            for k in (str(c.get("symbol") or "").lower(), str(c.get("name") or "").lower()):
                if k and k not in m:
                    m[k] = c["id"]
    return _put_c("top", m) or {}


async def cg_coin(cg_id: str) -> Coin | None:
    val, ok = _get_c(f"cgm:{cg_id}", TTL_QUOTE)
    if ok:
        return val
    d = await _json(f"{CG}/coins/markets?vs_currency=usd&ids={cg_id}")
    if not isinstance(d, list) or not d or not isinstance(d[0], dict):
        return None
    m = d[0]
    price = _f(m.get("current_price"))
    if not price:
        return None
    c = Coin(name=str(m.get("name") or cg_id), symbol=str(m.get("symbol") or "").upper(), price=price,
             change_pct=_f(m.get("price_change_percentage_24h")), rank=m.get("market_cap_rank"),
             high=_f(m.get("high_24h")), low=_f(m.get("low_24h")), volume=_f(m.get("total_volume")),
             supply=_f(m.get("circulating_supply")) or _f(m.get("total_supply")), cap=_f(m.get("market_cap")),
             source="coingecko")
    return _put_c(f"cgm:{cg_id}", c)


async def cg_candles(cg_id: str) -> list:
    val, ok = _get_c(f"cgo:{cg_id}", TTL_CANDLES)
    if ok:
        return val
    d = await _json(f"{CG}/coins/{cg_id}/ohlc?vs_currency=usd&days=1")
    rows = [(r[0] / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in (d or []) if isinstance(r, list) and len(r) >= 5 and None not in r[:5]]
    return _put_c(f"cgo:{cg_id}", rows) or []


# ───────────────────────── DexScreener / GeckoTerminal ─────────────────────────
def best_pair(pairs: list[dict], query: str = "") -> dict | None:
    """جفتِ با بیشترین نقدینگی؛ اگر آدرس داده شده، فقط جفت‌هایی که توکن پایه‌شان همان آدرس است."""
    ps = [p for p in pairs or [] if isinstance(p, dict) and _f(p.get("priceUsd"))]
    if query and ADDR_RE.match(query):
        same = [p for p in ps if str((p.get("baseToken") or {}).get("address", "")).lower() == query.lower()]
        ps = same or ps
    elif query:
        q = query.lower()
        same = [p for p in ps if str((p.get("baseToken") or {}).get("symbol", "")).lower() == q]
        ps = same or ps
    if not ps:
        return None
    return max(ps, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0)


def coin_from_pair(p: dict) -> Coin:
    bt = p.get("baseToken") or {}
    return Coin(name=str(bt.get("name") or bt.get("symbol") or "?"), symbol=str(bt.get("symbol") or "").upper(),
                price=_f(p.get("priceUsd")) or 0, change_pct=_f((p.get("priceChange") or {}).get("h24")),
                volume=_f((p.get("volume") or {}).get("h24")), cap=_f(p.get("marketCap")) or _f(p.get("fdv")),
                chain=str(p.get("chainId") or ""), source="dexscreener")


async def dex_lookup(query: str) -> tuple[Coin, dict] | None:
    from urllib.parse import quote
    key = f"dex:{query.lower()}"
    val, ok = _get_c(key, TTL_QUOTE)
    if ok:
        return val
    if ADDR_RE.match(query):
        d = await _json(f"{DEX}/latest/dex/tokens/{quote(query)}")
    else:
        d = await _json(f"{DEX}/latest/dex/search?q={quote(query)}")
    p = best_pair((d or {}).get("pairs") if isinstance(d, dict) else None, query)
    if not p:
        return None
    return _put_c(key, (coin_from_pair(p), p))


async def gt_candles(pair: dict) -> list:
    net = GT_NET.get(str(pair.get("chainId") or ""))
    pool = pair.get("pairAddress")
    if not net or not pool:
        return []
    key = f"gt:{net}:{pool}"
    val, ok = _get_c(key, TTL_CANDLES)
    if ok:
        return val
    d = await _json(f"{GT}/networks/{net}/pools/{pool}/ohlcv/minute?aggregate=15&limit=96&currency=usd",
                    headers={"Accept": "application/json;version=20230302"})
    try:
        lst = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return []
    rows = sorted((float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in lst if len(r) >= 5)
    return _put_c(key, rows) or []


# ───────────────────────── CoinMarketCap (اختیاری) ─────────────────────────
async def cmc_coin(symbol: str) -> Coin | None:
    try:
        from platform_bot import get_pset
        key = (await get_pset("cmc_api_key", "") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    if not key or not re.fullmatch(r"[A-Za-z0-9]{1,15}", symbol or ""):
        return None
    d = await _json(f"{CMC}/v2/cryptocurrency/quotes/latest?symbol={symbol.upper()}", headers={"X-CMC_PRO_API_KEY": key})
    try:
        e = d["data"][symbol.upper()]
        e = e[0] if isinstance(e, list) else e
        q = e["quote"]["USD"]
    except (TypeError, KeyError, IndexError):
        return None
    if not _f(q.get("price")):
        return None
    return Coin(name=str(e.get("name")), symbol=str(e.get("symbol")).upper(), price=_f(q.get("price")),
                change_pct=_f(q.get("percent_change_24h")), rank=e.get("cmc_rank"), volume=_f(q.get("volume_24h")),
                supply=_f(e.get("circulating_supply")), cap=_f(q.get("market_cap")), source="coinmarketcap")


# ───────────────────────── ورودی اصلی ─────────────────────────
async def lookup(query: str, *, cg_id: str | None = None, strict: bool = False, with_candles: bool = True) -> Coin | None:
    """query: نام/نماد/آدرس. cg_id: اگر از قبل معلوم است (دارایی‌های شناخته‌شده). strict: کلمهٔ لاتین بی‌پیشوند در گفتگو."""
    query = (query or "").strip()
    if not cg_id and ADDR_RE.match(query):
        r = await dex_lookup(query)
        if not r:
            return None
        coin, pair = r
        if with_candles:
            coin.candles, coin.candle_label = await gt_candles(pair), "15m"
        return coin
    if not cg_id and strict:
        cg_id = (await top_map()).get(query.lower())
        if not cg_id:
            return None
    if not cg_id:
        coins = await cg_search(query)
        cg_id = pick_cg(coins, query, strict)
    coin = await cg_coin(cg_id) if cg_id else None
    if coin is None and cg_id:
        coin = await cmc_coin(query if re.fullmatch(r"[A-Za-z0-9]{1,15}", query) else "")
    if coin is not None:
        if with_candles and cg_id:
            coin.candles, coin.candle_label = await cg_candles(cg_id), "30m"
        return coin
    if strict:
        return None
    r = await dex_lookup(query)                         # توکن تازه که در CoinGecko نیست
    if not r:
        return None
    coin, pair = r
    if with_candles:
        coin.candles, coin.candle_label = await gt_candles(pair), "15m"
    return coin
