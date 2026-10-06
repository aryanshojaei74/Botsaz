"""
nft_sources.py — «منابعِ مارکت» برایِ مینی‌اپ ۲ (واسطه‌گری / دراپ‌شیپینگ).

=== PATCH_NFT_SOURCES_V1 ===

فروشنده لازم نیست گیفت‌ها را از قبل داشته باشد: آیتم‌ها از یک مارکتِ بیرونی
خوانده می‌شوند، با درصدِ سودِ خودش قیمت‌گذاری و در مینی‌اپ عرضه می‌شوند.
سفارش که آمد، خودش از مبدأ می‌خرد و تحویل می‌دهد.

<b>معماریِ چندمنبعی</b>
هر مارکت یک کلاسِ Provider است که در _PROVIDERS ثبت می‌شود. افزودنِ
«مارکت ۲» یعنی فقط یک کلاسِ تازه این‌جا — نه تغییر در مدل، API یا UI.
فعلاً Tonnel پیاده شده و بقیه به‌عنوانِ «به‌زودی» فهرست می‌شوند تا در
رابطِ کاربری جایشان دیده شود.

<b>محدودیت‌هایی که عمداً پذیرفته شده‌اند</b>
  • این API رسمی نیست. مارکت هر وقت بخواهد می‌تواند عوضش کند.
  • authData از مرورگر گرفته می‌شود و هر ۲۴ تا ۷۲ ساعت منقضی می‌شود.
    «تازه‌سازیِ خودکار» به‌معنایِ واقعی ممکن نیست (نیاز به سشنِ تلگرامِ
    خودِ کاربر دارد)، پس به‌جایش: سلامتِ توکن مدام پایش می‌شود، به‌محضِ
    منقضی‌شدن به ادمین هشدار می‌رود و جایِ واردکردنِ توکنِ تازه داخلِ
    خودِ مینی‌اپ است.
  • فقط <b>خواندنی</b>. خریدِ خودکار عمداً پیاده نشده: پایِ پولِ واقعی
    وسط است و یک API غیررسمی که هر لحظه می‌تواند بشکند جایِ ریسک‌کردن
    نیست. تحویل دستی می‌ماند.
  • Tonnel پشتِ Cloudflare است و با کلاینتِ HTTPِ معمولی ۴۰۳ می‌دهد؛
    باید با curl_cffi و جعلِ اثرانگشتِ TLS درخواست زد.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json

from sqlalchemy import select

from config import Session
from models import NftItem, NftMarketSource

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_TIMEOUT = 20


class SourceAuthError(RuntimeError):
    """توکن منقضی یا نامعتبر — با بقیهٔ خطاها فرق دارد چون درمانش
    واردکردنِ توکنِ تازه است، نه تلاشِ دوباره."""


class SourceUnavailable(RuntimeError):
    """مارکت در دسترس نیست (شبکه، Cloudflare، ۵xx)."""


# ───────────────────────── مدلِ داده ─────────────────────────

class RemoteItem:
    """یک آیتمِ خوانده‌شده از مارکتِ مبدأ، مستقل از شکلِ خامِ هر مارکت."""

    __slots__ = ("ref", "title", "collection", "model", "backdrop", "symbol",
                 "price_ton", "image")

    def __init__(self, ref, title, price_ton, collection=None, model=None,
                 backdrop=None, symbol=None, image=None):
        self.ref = str(ref)
        self.title = title
        self.price_ton = float(price_ton or 0)
        self.collection = collection
        self.model = model
        self.backdrop = backdrop
        self.symbol = symbol
        self.image = image


# ───────────────────────── Providerها ─────────────────────────

class BaseProvider:
    key = ""
    label = ""
    ready = False          # False یعنی هنوز پیاده نشده («به‌زودی»)
    auth_help = ""

    async def fetch(self, auth: str, *, limit: int = 60,
                    collection: str | None = None) -> list[RemoteItem]:
        raise NotImplementedError


class TonnelProvider(BaseProvider):
    key = "tonnel"
    label = "Tonnel"
    ready = True
    auth_help = (
        "۱. در مرورگر <code>market.tonnel.network</code> را باز کن و لاگین کن.\n"
        "۲. DevTools → Console.\n"
        "۳. این را اجرا کن:\n"
        "<code>localStorage.getItem(\"web-initData\")</code>\n"
        "۴. خروجی (که با <code>user=</code> شروع می‌شود) را همین‌جا بگذار.\n\n"
        "📱 <b>با گوشی (بدونِ DevTools):</b> در مرورگرِ اندروید (مثلاً Chrome) یک بوکمارکِ دلخواه "
        "بساز و آدرسش را این کن:\n"
        "<code>javascript:prompt('initData',localStorage.getItem('web-initData'))</code>\n"
        "بعد <code>market.tonnel.network</code> را باز و لاگین کن و بوکمارک را بزن؛ "
        "مقدارِ نشان‌داده‌شده را کپی کن و اینجا بگذار. "
        "(اگر مارکت لاگینِ تلگرامیِ مرورگر را پشتیبانی نکرد، این کار را یک‌بار از کامپیوتر انجام بده.)"
    )

    _URL = "https://gifts2.tonnel.network/api/pageGifts"
    _HEADERS = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://market.tonnel.network",
        "referer": "https://market.tonnel.network/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/137.0 Safari/537.36"),
    }

    def _blocking_fetch(self, auth: str, limit: int, collection: str | None):
        # Cloudflare جلویِ کلاینتِ معمولی را می‌گیرد؛ curl_cffi اثرانگشتِ TLSِ
        # کروم را جعل می‌کند. سنکرون است، پس در ترد اجرا می‌شود.
        from curl_cffi import requests as _creq

        flt = {"price": {"$exists": True}, "buyer": {"$exists": False},
               "asset": "TON", "refunded": {"$ne": True},
               "export_at": {"$exists": True}}
        if collection:
            flt["gift_name"] = collection.strip()
        payload = {
            "filter": _json.dumps(flt),
            "limit": max(1, min(int(limit), 30)),   # سقفِ خودِ مارکت ۳۰ است
            "page": 1,
            "sort": '{"price":1,"gift_id":-1}',
            "ref": 0,
            "price_range": 0,
            "user_auth": auth or "",
        }
        r = _creq.post(self._URL, headers=self._HEADERS, json=payload,
                       impersonate="chrome110", timeout=_TIMEOUT)
        if r.status_code in (401, 403):
            # ۴۰۳ هم می‌تواند Cloudflare باشد هم توکنِ باطل؛ چون درمانِ
            # هردو از دیدِ کاربر «توکنت را تازه کن» است، همان را می‌گوییم.
            raise SourceAuthError(
                f"توکن پذیرفته نشد یا مارکت درخواست را رد کرد (HTTP {r.status_code}).")
        if r.status_code == 429:
            raise SourceUnavailable("مارکت فعلاً درخواست‌ها را محدود کرده (429).")
        if r.status_code != 200:
            raise SourceUnavailable(f"پاسخِ غیرمنتظره از مارکت (HTTP {r.status_code}).")
        try:
            return r.json()
        except Exception as e:                               # noqa: BLE001
            raise SourceUnavailable(f"پاسخِ مارکت قابلِ خواندن نبود ({type(e).__name__}).")

    async def fetch(self, auth: str, *, limit: int = 60,
                    collection: str | None = None) -> list[RemoteItem]:
        try:
            raw = await asyncio.to_thread(self._blocking_fetch, auth, limit, collection)
        except (SourceAuthError, SourceUnavailable):
            raise
        except ImportError:
            raise SourceUnavailable(
                "کتابخانهٔ curl_cffi رویِ سرور نصب نیست — بدونِ آن Cloudflare "
                "جلویِ درخواست را می‌گیرد. دستور: pip install curl_cffi")
        except Exception as e:                               # noqa: BLE001
            raise SourceUnavailable(f"ارتباط با مارکت برقرار نشد ({type(e).__name__}).")

        out: list[RemoteItem] = []
        for g in (raw or []):
            if not isinstance(g, dict):
                continue
            price = g.get("price")
            gid = g.get("gift_id")
            if price is None or gid is None:
                continue
            name = g.get("gift_name") or "Gift"
            num = g.get("gift_num")
            out.append(RemoteItem(
                ref=gid,
                title=f"{name} #{num}" if num else str(name),
                price_ton=price,
                collection=name,
                model=_short_trait(g.get("model")),
                backdrop=_short_trait(g.get("backdrop")),
                symbol=_short_trait(g.get("symbol")),
                image=None,
            ))
        return out


def _short_trait(v) -> str | None:
    """«Fist Bump (1.2%)» → «Fist Bump» — درصدِ کمیابی برایِ نمایش لازم نیست."""
    if not v:
        return None
    s = str(v)
    return (s.split("(", 1)[0].strip() or None) if "(" in s else s.strip() or None


class PortalsProvider(BaseProvider):
    key = "portals"
    label = "Portals"
    ready = True  # === PATCH_PORTALS_PROVIDER_V1 ===
    auth_help = (
        "پورتال یک مینی‌اپِ تلگرامه، پس توکنش initDataیِ خودِ تلگرامه (نه چیزی "
        "که تویِ localStorage بمونه):\n"
        "۱. مینی‌اپِ Portals را از داخلِ تلگرام (وب) باز کن: web.telegram.org\n"
        "۲. DevTools → تبِ Network → یکی از درخواست‌ها به "
        "<code>portals-market.com/api/</code> را پیدا کن.\n"
        "۳. تویِ Headersِ همون درخواست، مقدارِ هدرِ <code>Authorization</code> را "
        "کامل کپی کن (با <code>tma </code> اولش).\n"
        "۴. همون رشته را همین‌جا بگذار.\n\n"
        "📱 <b>با گوشی:</b> هر کدام از این‌ها را می‌توانی همین‌جا بگذاری — ربات خودش "
        "جداسازی می‌کند: مقدارِ <code>Authorization</code>، خودِ initData، یا کلِ لینکی که "
        "<code>tgWebAppData=</code> دارد. اگر فقط لینکِ مینی‌اپ را داری: "
        "<code>web.telegram.org/k</code> را در مرورگرِ گوشی باز کن، مینی‌اپ را باز کن، و با "
        "بوکمارکِ <code>javascript:prompt('data',document.querySelector('iframe').src)</code> "
        "لینکِ کامل را بگیر و همین‌جا بفرست."
    )

    # === PATCH_PORTALS_PROVIDER_V1 ===
    # مستندِ رسمی نیست؛ اندپوینت و شکلِ داده از رویِ پکیجِ منبع‌بازِ portalsmp
    # (pypi.org/project/portalsmp) استخراج شده — همون الگویی که Tonnel هم
    # داره: curl_cffi برایِ جعلِ TLS، چون این مارکت هم پشتِ محافظتِ ضدِ ربات است.
    _URL = "https://portals-market.com/api/nfts/search"
    _HEADERS = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://portals-market.com",
        "referer": "https://portals-market.com/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
    }

    def _blocking_fetch(self, auth: str, limit: int, collection: str | None):
        from curl_cffi import requests as _creq
        import urllib.parse as _up

        qs = {
            "offset": "0",
            "limit": str(max(1, min(int(limit), 20))),   # سقفِ خودِ مارکت ۲۰ است
            "sort_by": "listed_at desc",
            "status": "listed",
        }
        if collection:
            qs["filter_by_collections"] = collection.strip()
        url = self._URL + "?" + _up.urlencode(qs)
        headers = dict(self._HEADERS)
        headers["Authorization"] = auth or ""
        r = _creq.get(url, headers=headers, impersonate="chrome110", timeout=_TIMEOUT)
        if r.status_code in (401, 403):
            raise SourceAuthError(
                f"توکن پذیرفته نشد یا مارکت درخواست را رد کرد (HTTP {r.status_code}).")
        if r.status_code == 429:
            raise SourceUnavailable("مارکت فعلاً درخواست‌ها را محدود کرده (429).")
        if r.status_code != 200:
            raise SourceUnavailable(f"پاسخِ غیرمنتظره از مارکت (HTTP {r.status_code}).")
        try:
            data = r.json()
        except Exception as e:                               # noqa: BLE001
            raise SourceUnavailable(f"پاسخِ مارکت قابلِ خواندن نبود ({type(e).__name__}).")
        return data.get("results", data) if isinstance(data, dict) else data

    async def fetch(self, auth: str, *, limit: int = 60,
                    collection: str | None = None) -> list[RemoteItem]:
        try:
            raw = await asyncio.to_thread(self._blocking_fetch, auth, limit, collection)
        except (SourceAuthError, SourceUnavailable):
            raise
        except ImportError:
            raise SourceUnavailable(
                "کتابخانهٔ curl_cffi رویِ سرور نصب نیست — بدونِ آن این مارکت هم "
                "جلویِ درخواست را می‌گیرد. دستور: pip install curl_cffi")
        except Exception as e:                               # noqa: BLE001
            raise SourceUnavailable(f"ارتباط با مارکت برقرار نشد ({type(e).__name__}).")

        out: list[RemoteItem] = []
        for g in (raw or []):
            if not isinstance(g, dict):
                continue
            price = g.get("price")
            gid = g.get("id")
            if price is None or gid is None:
                continue
            attrs = {a.get("type"): a.get("value") for a in (g.get("attributes") or [])
                     if isinstance(a, dict)}
            name = g.get("name") or "Gift"
            num = g.get("external_collection_number")
            out.append(RemoteItem(
                ref=gid,
                title=f"{name} #{num}" if num else str(name),
                price_ton=price,
                collection=name,
                model=attrs.get("model"),
                backdrop=attrs.get("backdrop"),
                symbol=attrs.get("symbol"),
                image=g.get("photo_url"),
            ))
        return out


class _ComingSoon(BaseProvider):
    ready = False

    def __init__(self, key, label):
        self.key, self.label = key, label


# افزودنِ مارکتِ بعدی: یک کلاسِ تازه بساز و همین‌جا ثبتش کن — بقیهٔ
# سیستم (مدل، API، رابطِ کاربری) هیچ تغییری لازم ندارد.
_PROVIDERS: dict[str, BaseProvider] = {
    "tonnel": TonnelProvider(),
    "portals": PortalsProvider(),   # === PATCH_PORTALS_PROVIDER_V1 ===
    "mrkt": _ComingSoon("mrkt", "MRKT"),
}


def normalize_auth(provider_key: str, raw: str) -> str:
    """ورودیِ توکن را از شکل‌هایِ مختلف به شکلِ قابلِ‌استفاده تبدیل می‌کند (PATCH_NFT_AUTH_NORMALIZE_V1).

    کاربرِ گوشی نمی‌تواند DevTools باز کند، پس باید بتواند هر چه را دارد بچسباند:
    خودِ initData، مقدارِ هدرِ Authorization (با «tma »)، رشتهٔ URL-encode‌شده، یا
    کلِ لینکی که tgWebAppData= دارد."""
    import urllib.parse as _up
    s = (raw or "").strip().strip('"').strip("'").strip()
    if "tgWebAppData=" in s:
        s = s.split("tgWebAppData=", 1)[1]
        for stop in ("&tgWebAppVersion", "&tgWebAppPlatform", "&tgWebAppThemeParams"):
            s = s.split(stop, 1)[0]
        s = _up.unquote(s)
    if s.lower().startswith("tma "):
        s = s[4:].strip()
    # هنوز درصد-کدگذاری‌شده است (مثلاً user%3D%7B...)؟
    if "%3D" in s.upper() and "=" not in s[:16]:
        s = _up.unquote(s)
    if provider_key == "portals":
        return "tma " + s if s else ""
    return s


def get_provider(key: str) -> BaseProvider | None:
    return _PROVIDERS.get((key or "").lower())


def provider_list() -> list[dict]:
    return [{"key": p.key, "label": p.label, "ready": p.ready,
             "auth_help": p.auth_help} for p in _PROVIDERS.values()]


# ───────────────────────── همگام‌سازی ─────────────────────────

def _auth_of(src: NftMarketSource) -> str:
    if not src.encrypted_auth:
        return ""
    try:
        from wallet_crypto import decrypt_secret
        return decrypt_secret(src.encrypted_auth)
    except Exception:                                        # noqa: BLE001
        logger.exception("nft_sources: رمزگشاییِ توکنِ منبع #%s نشد", src.id)
        return ""


async def _mark(src_id: int, *, state: str, error: str | None,
                touched_ok: bool) -> None:
    now = _dt.datetime.utcnow()
    async with Session() as s:
        src = await s.get(NftMarketSource, src_id)
        if not src:
            return
        src.token_state = state
        src.last_error = (error or "")[:400] or None
        src.last_sync_at = now
        if touched_ok:
            src.last_ok_at = now
            src.expiry_warned_at = None      # سالم شد، هشدارِ بعدی مجاز است
        await s.commit()


async def sync_source(bot_id: int, source_id: int) -> tuple[bool, str, dict]:
    """قیمت و موجودیِ آیتم‌هایِ این منبع را به‌روز می‌کند و آیتمِ تازه
    وارد می‌کند.

    خروجی: (ok, پیام, آمار). هرگز استثنا به بیرون نمی‌دهد.
    """
    stats = {"added": 0, "updated": 0, "hidden": 0, "seen": 0}
    async with Session() as s:
        src = await s.get(NftMarketSource, source_id)
        if not src or src.bot_id != bot_id:
            return False, "منبع پیدا نشد.", stats
        if not src.is_enabled:
            return False, "این منبع غیرفعال است.", stats
        prov = get_provider(src.provider)
        if not prov or not prov.ready:
            return False, "این مارکت هنوز پشتیبانی نمی‌شود.", stats
        auth = _auth_of(src)
        markup = float(src.markup_percent or 0)
        limit = int(src.max_items or 60)
        coll = (src.collection_filter or "").strip() or None
        auto_hide = bool(src.auto_hide_missing)

    if not auth:
        await _mark(source_id, state="expired", error="توکن ثبت نشده.", touched_ok=False)
        return False, "توکنِ این منبع ثبت نشده.", stats

    # نرخِ TON برایِ تبدیل به تومان
    from tenant.bots.virtual_service.nft_market import _ton_rate
    rate = await _ton_rate(bot_id)
    if rate <= 0:
        return False, ("اول نرخِ هر TON به تومان را در بخشِ «نرخ‌ها» تنظیم کن، "
                       "وگرنه قیمتِ تومانی قابلِ‌محاسبه نیست."), stats

    try:
        remote = await prov.fetch(auth, limit=limit, collection=coll)
    except SourceAuthError as e:
        await _mark(source_id, state="expired", error=str(e), touched_ok=False)
        return False, f"{e} توکن را تازه کن.", stats
    except SourceUnavailable as e:
        await _mark(source_id, state="error", error=str(e), touched_ok=False)
        return False, str(e), stats
    except Exception as e:                                   # noqa: BLE001
        logger.exception("nft_sources: sync ناموفق (source=%s)", source_id)
        await _mark(source_id, state="error", error=f"{type(e).__name__}", touched_ok=False)
        return False, f"خطایِ غیرمنتظره ({type(e).__name__}).", stats

    stats["seen"] = len(remote)
    seen_refs = {r.ref for r in remote}

    async with Session() as s:
        existing = {
            i.source_ref: i for i in (await s.scalars(select(NftItem).where(
                NftItem.bot_id == bot_id, NftItem.source_id == source_id))).all()
            if i.source_ref
        }
        for r in remote:
            cost = int(round(r.price_ton * rate))
            sale = int(round(cost * (1 + markup / 100.0)))
            it = existing.get(r.ref)
            if it:
                # آیتمی که فروخته/رزرو شده دست نمی‌خورد — قیمتش الان
                # بخشی از یک سفارشِ در جریان است.
                if it.status in ("reserved", "sold", "rented"):
                    continue
                it.cost_price = cost
                it.sale_price = sale
                it.title = r.title
                it.collection = r.collection
                it.model = r.model
                it.color = r.backdrop
                it.symbol = r.symbol
                if it.status == "hidden":
                    it.status = "available"
                stats["updated"] += 1
            else:
                s.add(NftItem(
                    bot_id=bot_id, kind="gift", title=r.title, ref=r.ref,
                    collection=r.collection, model=r.model, color=r.backdrop,
                    symbol=r.symbol, image_url=r.image,
                    mode="buy", sale_price=sale, cost_price=cost,
                    rent_daily=0, status="available",
                    source_id=source_id, source_ref=r.ref))
                stats["added"] += 1

        if auto_hide:
            for ref, it in existing.items():
                if ref not in seen_refs and it.status == "available":
                    # در مبدأ نیست؛ مخفی می‌شود تا کسی چیزی را که
                    # نمی‌توانیم تهیه کنیم نخرد.
                    it.status = "hidden"
                    stats["hidden"] += 1
        await s.commit()

    await _mark(source_id, state="ok", error=None, touched_ok=True)
    return True, (f"همگام‌سازی شد — {stats['added']} تازه، "
                  f"{stats['updated']} به‌روز، {stats['hidden']} مخفی."), stats


async def check_auth(bot_id: int, source_id: int) -> tuple[bool, str]:
    """تستِ سریعِ سلامتِ توکن، بدونِ دست‌زدن به آیتم‌ها."""
    async with Session() as s:
        src = await s.get(NftMarketSource, source_id)
        if not src or src.bot_id != bot_id:
            return False, "منبع پیدا نشد."
        prov = get_provider(src.provider)
        if not prov or not prov.ready:
            return False, "این مارکت هنوز پشتیبانی نمی‌شود."
        auth = _auth_of(src)
    if not auth:
        return False, "توکن ثبت نشده."
    try:
        items = await prov.fetch(auth, limit=1)
    except SourceAuthError as e:
        await _mark(source_id, state="expired", error=str(e), touched_ok=False)
        return False, str(e)
    except SourceUnavailable as e:
        await _mark(source_id, state="error", error=str(e), touched_ok=False)
        return False, str(e)
    except Exception as e:                                   # noqa: BLE001
        await _mark(source_id, state="error", error=str(e), touched_ok=False)
        return False, f"خطا ({type(e).__name__})."
    await _mark(source_id, state="ok", error=None, touched_ok=True)
    return True, f"اتصال سالم است ({len(items)} آیتم خوانده شد)."


async def sync_all_due(max_age_minutes: int = 60) -> int:
    """همگام‌سازیِ دوره‌ایِ همهٔ منابعِ فعال — از زمان‌بندِ tenant صدا زده
    می‌شود. منبعی که توکنش منقضی شده تلاشِ بی‌فایده نمی‌کند؛ فقط به
    ادمین هشدار می‌دهد (یک بار، نه هر دور)."""
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(minutes=max_age_minutes)
    async with Session() as s:
        rows = (await s.scalars(select(NftMarketSource).where(
            NftMarketSource.is_enabled.is_(True)))).all()
        due = [(r.bot_id, r.id, r.token_state,
                r.last_sync_at, r.expiry_warned_at) for r in rows]

    done = 0
    for bot_id, sid, state, last_sync, warned in due:
        if state == "expired":
            if not warned:
                try:
                    from tenant.bots.virtual_service.nft_admin import notify_source_expired
                    await notify_source_expired(bot_id, sid)
                except Exception:                            # noqa: BLE001
                    logger.exception("nft_sources: هشدارِ انقضا نرفت (source=%s)", sid)
            continue
        if last_sync and last_sync > cutoff:
            continue
        ok, _note, _st = await sync_source(bot_id, sid)
        done += 1
        if not ok:
            async with Session() as s:
                src = await s.get(NftMarketSource, sid)
                fresh_state = src.token_state if src else ""
            if fresh_state == "expired":
                try:
                    from tenant.bots.virtual_service.nft_admin import notify_source_expired
                    await notify_source_expired(bot_id, sid)
                except Exception:                            # noqa: BLE001
                    logger.exception("nft_sources: هشدارِ انقضا نرفت (source=%s)", sid)
    return done
