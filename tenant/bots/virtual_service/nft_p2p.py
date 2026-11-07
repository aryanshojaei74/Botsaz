"""
nft_p2p.py — «بازارِ کاربران» داخلِ مینی‌اپ ۲ (مارکتِ NFT).

=== PATCH_P2P_MARKET_V1 ===

کاربرها گیفتِ NFT یا یوزرنیمِ خودشان را برایِ فروش و/یا اجاره می‌گذارند و
ربات واسطه (اسکرو) است:

  ۱) خریدار می‌خرد → مبلغ از کیف‌پولش کم می‌شود و نزدِ ربات می‌ماند (p2p_hold).
  ۲) فروشنده پیام می‌گیرد، با دو دکمه:
       «گیفت را از قبل فروخته‌ام» → معامله لغو و پولِ خریدار برمی‌گردد؛
       «انتقال دادم»              → باید کارمزدِ ربات را بدهد (استارز و/یا کیف‌پول،
                                     به انتخابِ ادمین) و بعد این گزینه قفل می‌شود.
  ۳) خریدار می‌پرسد «فروشنده می‌گوید انتقال داده؛ درست است؟»
       «بله» → مبلغ به کیف‌پولِ فروشنده می‌رود؛
       «مشکلی هست» → معامله به ادمین/پشتیبانی می‌رود (disputed) و ادمین یا
                      پول را آزاد می‌کند یا به خریدار برمی‌گرداند.
  ۴) فروشنده مبلغِ فروش را «برداشت» می‌زند → از موجودی‌اش کم می‌شود، شمارهٔ
     کارتش برایِ ادمین می‌رود؛ ادمین واریز و تأیید می‌کند یا رد می‌کند (برگشت).

مشخصاتِ گیفت (عکس، مدل، رنگ، سمبل) از روی لینکِ t.me/nft/... خودکار گرفته
می‌شود؛ اگر گرفتنش نشد آگهی با اطلاعاتِ استخراج‌شده از خودِ لینک ثبت می‌شود.

هیچ ستونی به جدول‌هایِ قبلی اضافه نشده؛ همه‌چیز در سه جدولِ جدید است
(P2PListing / P2PDeal / P2PWithdrawal).

پیشوندِ callback ربات: p2p:   —   پیشوندِ payloadِ فاکتورِ استارز: p2pfee:
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery,
)
from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func as sa_func, select, update as sa_update

from config import Session
from models import (
    Bot as BotRow, BotUser, P2PDeal, P2PListing, P2PWithdrawal, WalletTxn,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

nft_p2p_api_router = APIRouter()
nft_p2p_bot_router = Router()

MIN_PRICE = 1000          # کمترین قیمتِ آگهی (تومان)
MAX_LISTINGS_PER_USER = 20


# ───────────────────────── کمکی‌ها ─────────────────────────

def _rial(n) -> str:
    try:
        return f"{int(float(n)):,}"
    except (TypeError, ValueError):
        return "0"


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    try:
        return await get_setting(bot_id, key, default)
    except Exception:                                        # noqa: BLE001
        logger.exception("p2p: خواندنِ تنظیم %s ناموفق بود", key)
        return default


async def get_cfg(bot_id: int) -> dict:
    """تنظیماتِ بازارِ کاربران برایِ این ربات."""
    mode = (await _gs(bot_id, "p2p_fee_mode", "both")).strip()
    if mode not in ("stars", "wallet", "both"):
        mode = "both"
    return {
        "enabled": (await _gs(bot_id, "p2p_enabled", "0")) == "1",
        "fee_mode": mode,
        "fee_stars": max(0, _int(await _gs(bot_id, "p2p_fee_stars", "10"), 10)),
        "fee_wallet": max(0, _int(await _gs(bot_id, "p2p_fee_wallet", "0"), 0)),
        "seller_hours": max(1, _int(await _gs(bot_id, "p2p_seller_hours", "24"), 24)),
        "buyer_hours": max(1, _int(await _gs(bot_id, "p2p_buyer_hours", "48"), 48)),
        "min_withdraw": max(0, _int(await _gs(bot_id, "p2p_min_withdraw", "0"), 0)),
    }


def fee_options(cfg: dict) -> list[tuple[str, int]]:
    """روش‌هایِ پرداختِ کارمزد که فروشنده می‌تواند انتخاب کند: [(method, amount)]."""
    out = []
    if cfg["fee_mode"] in ("stars", "both") and cfg["fee_stars"] > 0:
        out.append(("stars", cfg["fee_stars"]))
    if cfg["fee_mode"] in ("wallet", "both") and cfg["fee_wallet"] > 0:
        out.append(("wallet", cfg["fee_wallet"]))
    return out


def _tbot(token: str) -> Bot:
    from tenant.bots.virtual_service.nft_admin import _tenant_bot
    return _tenant_bot(token)


async def _send(bot_id: int, tg_id: int, text: str,
                kb: InlineKeyboardMarkup | None = None) -> bool:
    """ارسالِ پیام از مسیرهایِ غیرِ هندلر (API/زمان‌بند). هرگز استثنا نمی‌دهد."""
    if not tg_id:
        return False
    try:
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
        if not row:
            return False
        tb = _tbot(row.token)
        try:
            await tb.send_message(int(tg_id), text, reply_markup=kb,
                                  disable_web_page_preview=True)
            return True
        finally:
            try:
                await tb.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception as e:                                   # noqa: BLE001
        logger.warning("p2p: پیام به %s نرسید (%s): %s", tg_id, type(e).__name__, e)
        return False


async def _admin_ids(bot_id: int) -> list[int]:
    """مالک + ادمین‌هایِ ربات (برایِ اطلاعِ اختلاف‌ها و برداشت‌ها)."""
    ids: list[int] = []
    try:
        from models import AdminSupport
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
            if row:
                ids.append(int(row.owner_id))
            adm = (await s.scalars(select(AdminSupport.telegram_id).where(
                AdminSupport.bot_id == bot_id))).all()
        for a in adm:
            if a and int(a) not in ids:
                ids.append(int(a))
    except Exception:                                        # noqa: BLE001
        logger.exception("p2p: خواندنِ ادمین‌ها ناموفق بود")
    return ids


async def _notify_admins(bot_id: int, text: str, kb=None) -> None:
    for aid in await _admin_ids(bot_id):
        await _send(bot_id, aid, text, kb)


def _who(u: BotUser | None) -> str:
    if not u:
        return "؟"
    name = _html.escape(u.full_name or str(u.telegram_id))
    link = f'<a href="tg://user?id={u.telegram_id}">{name}</a>'
    return link + (f" (@{_html.escape(u.username)})" if u.username else "")


def _mask(u: BotUser | None) -> str:
    """نامِ عمومیِ فروشنده در کاتالوگ — فقط نامِ کوچک."""
    if not u:
        return "کاربر"
    nm = (u.full_name or "").strip().split(" ")[0]
    return nm[:14] or f"کاربر {u.id}"


async def _lock_user(s, user_id: int) -> BotUser | None:
    return await s.scalar(select(BotUser).where(BotUser.id == user_id).with_for_update())


def _credit(s, bot_id: int, u: BotUser, amount: int, kind: str, note: str) -> None:
    u.balance = _int(u.balance) + amount
    s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=amount, kind=kind[:20], note=note[:200]))


# ───────────────────────── مشخصاتِ گیفت از روی لینک ─────────────────────────

_GIFT_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/nft/([A-Za-z0-9_]+)-(\d+)/?(?:\?.*)?$", re.I)
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_gift_link(link: str) -> tuple[str, str, str] | None:
    """(لینکِ نرمال، slug کالکشن، شماره) یا None."""
    m = _GIFT_LINK_RE.match((link or "").strip())
    if not m:
        return None
    slug, num = m.group(1), m.group(2)
    return f"https://t.me/nft/{slug}-{num}", slug, num


def _split_camel(slug: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", slug).strip()


def _clean_val(raw: str) -> str:
    v = re.sub(r"<[^>]+>", " ", raw or "")
    v = _html.unescape(v)
    v = re.sub(r"\s*[\d.]+\s*%\s*$", "", v.strip())
    return re.sub(r"\s+", " ", v).strip()[:80]


def gift_image_guess(slug: str, num) -> str:
    """تصویرِ استاندارد گیفت روی CDN فرگمنت — وقتی og:image در دسترس نبود."""
    slug = re.sub(r"[^A-Za-z0-9]", "", slug or "").lower()
    if not slug or not str(num or "").isdigit():
        return ""
    return f"https://nft.fragment.com/gift/{slug}-{num}.large.jpg"


async def fetch_gift_meta(link: str) -> dict:
    """مشخصاتِ گیفت را از صفحهٔ عمومیِ t.me/nft/... می‌خواند.

    خروجی همیشه یک dict است؛ اگر شبکه/ساختارِ صفحه خطا داد، فقط فیلدهایی که
    از خودِ لینک قابل‌استخراج‌اند (کالکشن/شماره) پر می‌شوند و ثبتِ آگهی
    متوقف نمی‌شود.
    """
    parsed = parse_gift_link(link)
    if not parsed:
        return {}
    norm, slug, num = parsed
    meta = {"link": norm, "ref": num, "collection": _split_camel(slug),
            "title": f"{_split_camel(slug)} #{num}",
            "image": gift_image_guess(slug, num), "model": "", "color": "", "symbol": "", "fetched": False}
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as cs:
            async with cs.get(norm, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return meta
                html = await r.text()
    except Exception as e:                                   # noqa: BLE001
        logger.info("p2p: گرفتنِ مشخصاتِ گیفت ممکن نشد (%s): %s", type(e).__name__, e)
        return meta

    def _og(prop: str) -> str:
        m = re.search(r'<meta[^>]+property="og:%s"[^>]+content="([^"]*)"' % prop, html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+property="og:%s"' % prop, html, re.I)
        return _html.unescape(m.group(1)).strip() if m else ""

    t = _og("title")
    if t:
        meta["title"] = t[:120]
    img = _og("image") or _og("image:url") or _og("image:secure_url")
    if not img:
        _m = re.search(r'<meta[^>]+(?:name|property)="twitter:image"[^>]+content="([^"]*)"', html, re.I)
        img = _html.unescape(_m.group(1)).strip() if _m else ""
    if not img:
        _m = re.search(r'<(?:img|source)[^>]+(?:src|srcset)="(https?://[^"\s]+\.(?:jpg|jpeg|png|webp))', html, re.I)
        img = _html.unescape(_m.group(1)) if _m else ""
    if img.startswith("//"):
        img = "https:" + img
    if img.startswith("http"):
        meta["image"] = img[:500]
    else:
        meta["image"] = gift_image_guess(slug, num)

    pairs: dict[str, str] = {}
    for m in re.finditer(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", html, re.I | re.S):
        k = _clean_val(m.group(1)).lower()
        if k:
            pairs[k] = _clean_val(m.group(2))
    desc = _og("description")
    for line in re.split(r"[\n\r]+", desc):
        if ":" in line:
            k, v = line.split(":", 1)
            pairs.setdefault(k.strip().lower(), _clean_val(v))
    meta["model"] = pairs.get("model", "")
    meta["color"] = pairs.get("backdrop", "") or pairs.get("background", "")
    meta["symbol"] = pairs.get("symbol", "") or pairs.get("pattern", "")
    meta["fetched"] = bool(meta["image"] or meta["model"] or meta["color"])
    return meta


# ───────────────────────── آگهی‌ها ─────────────────────────

_ACTIVE_LISTING = ("available", "reserved", "rented")


def _guess_from_link(link) -> str:
    pr = parse_gift_link(link or "")
    return gift_image_guess(pr[1], pr[2]) if pr else ""


def _listing_json(l: P2PListing, seller: BotUser | None, me_id: int | None) -> dict:
    return {
        "id": l.id, "p2p": True, "kind": l.kind, "title": l.title,
        "ref": l.ref or "", "link": l.link or "",
        "collection": l.collection or "", "model": l.model or "",
        "color": l.color or "", "symbol": l.symbol or "",
        "image": l.image_url or _guess_from_link(l.link), "mode": l.mode,
        "sale_price": _int(l.sale_price), "rent_daily": _int(l.rent_daily),
        "rent_min_days": l.rent_min_days or 1, "rent_max_days": l.rent_max_days or 30,
        "note": l.note or "", "status": l.status,
        "seller": _mask(seller), "mine": bool(me_id and l.seller_id == me_id),
        "rent_until": l.rent_until.isoformat() if l.rent_until else None,
    }


async def save_listing(bot_id: int, user: BotUser, payload: dict) -> tuple[bool, str, dict | None]:
    cfg = await get_cfg(bot_id)
    if not cfg["enabled"]:
        return False, "p2p_disabled", None
    kind = "username" if payload.get("kind") == "username" else "gift"
    mode = payload.get("mode") if payload.get("mode") in ("buy", "rent", "both") else "buy"
    sale = max(0, _int(payload.get("sale_price")))
    daily = max(0, _int(payload.get("rent_daily")))
    mn = max(1, _int(payload.get("rent_min_days"), 1))
    mx = max(mn, min(365, _int(payload.get("rent_max_days"), 30)))
    note = (payload.get("note") or "").strip()[:300] or None
    if mode in ("buy", "both") and sale < MIN_PRICE:
        return False, "bad_sale_price", None
    if mode in ("rent", "both") and daily < MIN_PRICE:
        return False, "bad_rent_price", None
    if mode == "buy":
        daily = 0
    if mode == "rent":
        sale = 0

    if kind == "gift":
        meta = await fetch_gift_meta(payload.get("link") or "")
        if not meta:
            return False, "bad_link", None
        fields = dict(
            title=(payload.get("title") or meta["title"]).strip()[:120] or meta["title"],
            ref=meta["ref"], link=meta["link"], collection=meta["collection"][:80] or None,
            model=meta["model"] or None, color=meta["color"] or None,
            symbol=meta["symbol"] or None, image_url=meta["image"] or None)
        dup_key = (P2PListing.link == meta["link"])
    else:
        m = _USERNAME_RE.match((payload.get("username") or payload.get("ref") or "").strip())
        if not m:
            return False, "bad_username", None
        uname = m.group(1)
        fields = dict(title=f"@{uname}", ref=uname, link=None, collection=None,
                      model=None, color=None, symbol=None, image_url=None)
        dup_key = (sa_func.lower(P2PListing.ref) == uname.lower()) & (P2PListing.kind == "username")

    async with Session() as s:
        lid = _int(payload.get("id"))
        if lid:
            l = await s.get(P2PListing, lid)
            if not l or l.bot_id != bot_id or l.seller_id != user.id:
                return False, "not_found", None
            if l.status != "available":
                return False, "listing_busy", None
        else:
            cnt = await s.scalar(select(sa_func.count(P2PListing.id)).where(
                P2PListing.bot_id == bot_id, P2PListing.seller_id == user.id,
                P2PListing.status.in_(_ACTIVE_LISTING + ("hidden",)))) or 0
            if cnt >= MAX_LISTINGS_PER_USER:
                return False, "too_many", None
            l = None
        # بررسیِ تکراری *قبل* از add (وگرنه autoflush خودِ آگهیِ جدید را «تکراری» می‌بیند)
        dup = await s.scalar(select(P2PListing.id).where(
            P2PListing.bot_id == bot_id, dup_key, P2PListing.id != lid,
            P2PListing.status.in_(_ACTIVE_LISTING)))
        if dup:
            return False, "duplicate", None
        if l is None:
            l = P2PListing(bot_id=bot_id, seller_id=user.id)
            s.add(l)
        l.kind = kind
        for k, v in fields.items():
            setattr(l, k, v)
        l.mode, l.sale_price, l.rent_daily = mode, sale, daily
        l.rent_min_days, l.rent_max_days, l.note = mn, mx, note
        if not lid:
            l.status = "available"
        await s.commit()
        await s.refresh(l)
        return True, "ok", _listing_json(l, user, user.id)


async def remove_listing(bot_id: int, listing_id: int, user: BotUser | None, *, admin: bool = False) -> tuple[bool, str]:
    async with Session() as s:
        l = await s.get(P2PListing, listing_id)
        if not l or l.bot_id != bot_id or (not admin and (not user or l.seller_id != user.id)):
            return False, "not_found"
        if l.status in ("reserved", "rented"):
            return False, "listing_busy"
        l.status = "removed"
        await s.commit()
    return True, "ok"


# ───────────────────────── معامله (اسکرو) ─────────────────────────

_DEAL_FA_BUYER = {
    "escrow": "منتظرِ انتقال توسطِ فروشنده",
    "await_buyer": "منتظرِ تأییدِ تو",
    "completed": "تکمیل شد",
    "cancelled": "لغو شد — مبلغ برگشت",
    "disputed": "در بررسیِ پشتیبانی",
    "released": "تکمیل شد (تأییدِ ادمین)",
    "refunded": "لغو شد — مبلغ برگشت",
}
_DEAL_FA_SELLER = dict(_DEAL_FA_BUYER, escrow="منتظرِ انتقالِ تو",
                       await_buyer="منتظرِ تأییدِ خریدار")


def deal_actions(d: P2PDeal, role: str) -> list[str]:
    if role == "seller" and d.status == "escrow":
        return ["seller_done", "seller_cancel"]
    if role == "buyer" and d.status == "await_buyer":
        return ["buyer_ok", "buyer_problem"]
    return []


async def create_deal(bot_id: int, buyer: BotUser, listing_id: int, kind: str,
                      days: int) -> tuple[bool, str, dict]:
    cfg = await get_cfg(bot_id)
    if not cfg["enabled"]:
        return False, "p2p_disabled", {}
    kind = "buy" if kind == "buy" else "rent"
    async with Session() as s:
        l = await s.get(P2PListing, listing_id)
        if not l or l.bot_id != bot_id or l.status != "available":
            return False, "item_unavailable", {}
        if l.seller_id == buyer.id:
            return False, "own_listing", {}
        if kind == "buy":
            if l.mode not in ("buy", "both") or _int(l.sale_price) <= 0:
                return False, "not_for_sale", {}
            price, days = _int(l.sale_price), 0
        else:
            if l.mode not in ("rent", "both") or _int(l.rent_daily) <= 0:
                return False, "not_for_rent", {}
            if days < (l.rent_min_days or 1) or days > (l.rent_max_days or 30):
                return False, "bad_days", {}
            price = _int(l.rent_daily) * days
        seller = await s.get(BotUser, l.seller_id)
        if not seller or seller.is_banned:
            return False, "item_unavailable", {}

        b = await _lock_user(s, buyer.id)
        bal = _int(b.balance) if b else 0
        if not b or bal < price:
            return False, "insufficient", {"need": price, "need_fa": _rial(price),
                                           "balance_fa": _rial(bal)}
        res = await s.execute(sa_update(P2PListing).where(
            P2PListing.id == l.id, P2PListing.status == "available").values(status="reserved"))
        if (res.rowcount or 0) == 0:
            return False, "item_unavailable", {}
        _credit(s, bot_id, b, -price, "p2p_hold", f"نگهداریِ مبلغِ معامله: {l.title}")
        d = P2PDeal(bot_id=bot_id, listing_id=l.id, buyer_id=b.id, seller_id=seller.id,
                    kind=kind, days=days, price=price, title_snapshot=l.title[:120],
                    status="escrow", updated_at=_now())
        s.add(d)
        await s.commit()
        await s.refresh(d)
        deal_id, title, seller_tg = d.id, l.title, seller.telegram_id
        buyer_tg = b.telegram_id
        buyer_obj = b

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 انتقال دادم", callback_data=f"p2p:sd:{deal_id}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🚫 گیفت را از قبل فروخته‌ام", callback_data=f"p2p:sc:{deal_id}",
                              style=ButtonStyle.DANGER)],
    ])
    await _send(bot_id, seller_tg, _seller_card_text(deal_id, title, kind, days, price, buyer_obj), kb)
    await _send(bot_id, buyer_tg,
                f"✅ <b>سفارشِ تو ثبت شد</b> — معاملهٔ #{deal_id}\n\n"
                f"<b>{_html.escape(title)}</b>\n"
                f"مبلغِ <b>{_rial(price)}</b> تومان از موجودی‌ات کم شد و نزدِ ربات "
                "نگه داشته می‌شود.\n\n"
                "به فروشنده خبر دادیم. وقتی انتقال را انجام داد، از همین‌جا از تو "
                "تأیید می‌گیریم و بعد از تأییدِ تو پول به فروشنده می‌رسد.\n"
                f"⏳ اگر فروشنده تا {cfg['seller_hours']} ساعت اقدام نکند، معامله خودکار لغو و پولت برمی‌گردد.")
    return True, "ok", {"deal_id": deal_id, "title": title, "amount_fa": _rial(price)}


def _seller_card_text(deal_id, title, kind, days, price, buyer: BotUser | None) -> str:
    return (
        f"🛎 <b>خریدار برایِ آگهی‌ات پیدا شد</b> — معاملهٔ #{deal_id}\n\n"
        f"🎁 <b>{_html.escape(title)}</b>\n"
        + ("نوع: <b>فروش</b>\n" if kind == "buy" else f"نوع: <b>اجاره</b> — {days} روز\n")
        + f"مبلغ: <b>{_rial(price)}</b> تومان (از خریدار کم و نزدِ ربات نگه‌داری می‌شود)\n"
        f"خریدار: {_who(buyer)}\n\n"
        "۱) اگر هنوز داری، آیتم را به همین خریدار منتقل کن و بعد «📤 انتقال دادم» را بزن.\n"
        "۲) اگر قبلاً جایِ دیگری فروخته‌ای، «🚫 از قبل فروخته‌ام» را بزن تا معامله لغو و "
        "پولِ خریدار برگردد.\n\n"
        "<i>با زدنِ «انتقال دادم» باید کارمزدِ ربات را بپردازی و بعد از آن "
        "امکانِ لغو قفل می‌شود.</i>"
    )


async def _get_deal_for(s, bot_id: int, deal_id: int, role: str, tg_id: int):
    d = await s.get(P2PDeal, deal_id)
    if not d or d.bot_id != bot_id:
        return None, None
    uid = d.seller_id if role == "seller" else d.buyer_id
    u = await s.get(BotUser, uid)
    if not u or int(u.telegram_id) != int(tg_id):
        return None, None
    return d, u


async def _cas_status(s, deal_id: int, frm, to: str, **extra) -> bool:
    frm = (frm,) if isinstance(frm, str) else tuple(frm)
    res = await s.execute(sa_update(P2PDeal).where(
        P2PDeal.id == deal_id, P2PDeal.status.in_(frm)).values(
            status=to, updated_at=_now(), **extra))
    return (res.rowcount or 0) > 0


async def cancel_deal(bot_id: int, deal_id: int, *, by: str,
                      to_status: str = "cancelled") -> tuple[bool, str]:
    """لغو و بازگشتِ کاملِ پول به خریدار. by: seller / timeout / admin."""
    async with Session() as s:
        d = await s.get(P2PDeal, deal_id)
        if not d or d.bot_id != bot_id:
            return False, "not_found"
        frm = ("escrow",) if by in ("seller", "timeout") else ("escrow", "await_buyer", "disputed")
        if not await _cas_status(s, deal_id, frm, to_status):
            return False, "bad_status"
        b = await _lock_user(s, d.buyer_id)
        if b:
            _credit(s, bot_id, b, _int(d.price), "p2p_refund",
                    f"بازگشتِ مبلغِ معاملهٔ #{d.id}")
        l = await s.get(P2PListing, d.listing_id)
        if l and l.status == "reserved":
            # فروشنده گفته «از قبل فروخته‌ام» → آگهی برداشته می‌شود؛ تایم‌اوت/ادمین → دوباره آزاد
            l.status = "removed" if by == "seller" else ("hidden" if by == "admin" else "available")
        buyer_tg = b.telegram_id if b else 0
        seller = await s.get(BotUser, d.seller_id)
        title, price = d.title_snapshot, _int(d.price)
        seller_tg = seller.telegram_id if seller else 0
        await s.commit()
    why = {"seller": "فروشنده اعلام کرد آیتم را از قبل فروخته است",
           "timeout": "فروشنده در مهلتِ مقرر اقدام نکرد",
           "admin": "پشتیبانی معامله را لغو کرد"}.get(by, "")
    await _send(bot_id, buyer_tg,
                f"↩️ <b>معاملهٔ #{deal_id} لغو شد</b>\n\n<b>{_html.escape(title)}</b>\n"
                f"{why}. مبلغِ <b>{_rial(price)}</b> تومان به کیف‌پولت برگشت.")
    if by != "seller":
        await _send(bot_id, seller_tg, f"ℹ️ معاملهٔ #{deal_id} ({_html.escape(title)}) لغو شد: {why}.")
    return True, "ok"


async def seller_done(bot_id: int, deal_id: int, seller_tg: int, method: str | None):
    """فروشنده «انتقال دادم» را زده.

    method None → فقط وضعیت/گزینه‌هایِ کارمزد را برمی‌گرداند.
    method wallet → کارمزد از کیف‌پول کم و معامله قفل می‌شود.
    method stars  → فاکتورِ استارز برایِ فروشنده فرستاده می‌شود.
    """
    cfg = await get_cfg(bot_id)
    opts = fee_options(cfg)
    async with Session() as s:
        d, seller = await _get_deal_for(s, bot_id, deal_id, "seller", seller_tg)
        if not d:
            return False, "not_found", {}
        if d.status != "escrow":
            return False, "bad_status", {}
        title = d.title_snapshot
        seller_id = seller.id

    if method is None:
        return True, "need_fee" if opts else "free", {"options": [{"method": m, "amount": a} for m, a in opts]}

    if not opts:
        ok = await _finalize_fee(bot_id, deal_id, "free", 0)
        return ok, ("ok" if ok else "bad_status"), {}

    amounts = dict(opts)
    if method not in amounts:
        return False, "bad_method", {}
    if method == "wallet":
        fee = amounts["wallet"]
        async with Session() as s:
            u = await _lock_user(s, seller_id)
            bal = _int(u.balance) if u else 0
            if not u or bal < fee:
                return False, "insufficient", {"need_fa": _rial(fee), "balance_fa": _rial(bal)}
            if not await _cas_status(s, deal_id, "escrow", "escrow"):
                return False, "bad_status", {}
            _credit(s, bot_id, u, -fee, "p2p_fee", f"کارمزدِ ربات — معاملهٔ #{deal_id}")
            await s.commit()
        ok = await _finalize_fee(bot_id, deal_id, "wallet", fee)
        if not ok:           # معامله هم‌زمان لغو شده بود → کارمزد را برگردان
            async with Session() as s:
                u = await _lock_user(s, seller_id)
                if u:
                    _credit(s, bot_id, u, fee, "p2p_fee_refund", f"بازگشتِ کارمزد — معاملهٔ #{deal_id}")
                    await s.commit()
            return False, "bad_status", {}
        return True, "ok", {}

    # stars
    stars = amounts["stars"]
    try:
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
        tb = _tbot(row.token)
        try:
            await tb.send_invoice(
                chat_id=int(seller_tg), title=f"کارمزدِ معاملهٔ #{deal_id}",
                description=f"کارمزدِ ربات برایِ تکمیلِ معامله: {title[:60]}",
                payload=f"p2pfee:{bot_id}:{deal_id}", provider_token="", currency="XTR",
                prices=[LabeledPrice(label="کارمزدِ ربات", amount=stars)])
        finally:
            try:
                await tb.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception as e:                                   # noqa: BLE001
        logger.warning("p2p: فاکتورِ استارز نرفت (%s): %s", type(e).__name__, e)
        return False, "invoice_failed", {}
    return True, "invoice_sent", {"stars": stars}


async def _finalize_fee(bot_id: int, deal_id: int, method: str, amount: int) -> bool:
    """بعد از پرداختِ کارمزد: معامله قفل و از خریدار تأیید گرفته می‌شود."""
    async with Session() as s:
        if not await _cas_status(s, deal_id, "escrow", "await_buyer",
                                 fee_paid=True, fee_method=method, fee_amount=amount):
            return False
        d = await s.get(P2PDeal, deal_id)
        buyer = await s.get(BotUser, d.buyer_id)
        seller = await s.get(BotUser, d.seller_id)
        title, price, kind, days = d.title_snapshot, _int(d.price), d.kind, d.days
        buyer_tg = buyer.telegram_id if buyer else 0
        seller_tg = seller.telegram_id if seller else 0
        await s.commit()
    cfg = await get_cfg(bot_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، دریافت کردم", callback_data=f"p2p:bok:{deal_id}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⚠️ مشکلی پیش آمده", callback_data=f"p2p:bpr:{deal_id}",
                              style=ButtonStyle.DANGER)],
    ])
    await _send(bot_id, buyer_tg,
                f"📬 <b>فروشنده می‌گوید آیتم را به تو منتقل کرده</b> — معاملهٔ #{deal_id}\n\n"
                f"🎁 <b>{_html.escape(title)}</b>"
                + (f" ({days} روز اجاره)" if kind == "rent" else "") + "\n\n"
                "لطفاً اکانتت را چک کن. آیا آیتم را دریافت کرده‌ای و درست است؟\n\n"
                f"• «بله» → مبلغِ <b>{_rial(price)}</b> تومان که نزدِ ربات است به فروشنده پرداخت می‌شود.\n"
                "• «مشکلی پیش آمده» → پرونده به پشتیبانی می‌رود و پول تا بررسی نزدِ ربات می‌ماند.\n\n"
                f"<i>⏳ اگر تا {cfg['buyer_hours']} ساعت پاسخ ندهی، پرونده برایِ بررسی به پشتیبانی می‌رود.</i>", kb)
    await _send(bot_id, seller_tg,
                f"🔒 کارمزد پرداخت شد و معاملهٔ #{deal_id} قفل شد.\n"
                "منتظرِ تأییدِ خریدار هستیم؛ به‌محضِ تأیید، پیام می‌گیری و مبلغ به موجودی‌ات اضافه می‌شود.")
    return True


async def complete_deal(bot_id: int, deal_id: int, *, by: str = "buyer",
                        buyer_tg: int | None = None) -> tuple[bool, str]:
    """آزادسازیِ پول به فروشنده. by: buyer (تأییدِ خریدار) / admin."""
    async with Session() as s:
        d = await s.get(P2PDeal, deal_id)
        if not d or d.bot_id != bot_id:
            return False, "not_found"
        if by == "buyer":
            b = await s.get(BotUser, d.buyer_id)
            if not b or int(b.telegram_id) != int(buyer_tg or 0):
                return False, "not_found"
            frm = ("await_buyer",)
        else:
            frm = ("await_buyer", "disputed")
        new_status = "completed" if by == "buyer" else "released"
        if not await _cas_status(s, deal_id, frm, new_status, completed_at=_now()):
            return False, "bad_status"
        seller = await _lock_user(s, d.seller_id)
        price = _int(d.price)
        if seller:
            _credit(s, bot_id, seller, price, "p2p_sale", f"فروش در بازارِ کاربران #{d.id}")
        l = await s.get(P2PListing, d.listing_id)
        if l:
            if d.kind == "buy":
                l.status = "sold"
            else:
                l.status = "rented"
                l.rent_until = _now() + _dt.timedelta(days=d.days or 1)
        buyer = await s.get(BotUser, d.buyer_id)
        title = d.title_snapshot
        seller_tg = seller.telegram_id if seller else 0
        buyer_tg2 = buyer.telegram_id if buyer else 0
        new_bal = _int(seller.balance) if seller else 0
        await s.commit()
    await _send(bot_id, seller_tg,
                f"🎉 <b>آیتمت با موفقیت به فروش رسید!</b>\n\n<b>{_html.escape(title)}</b>\n"
                f"مبلغِ <b>{_rial(price)}</b> تومان به حسابِ تو واریز شد.\n"
                f"موجودیِ فعلی: <b>{_rial(new_bal)}</b> تومان\n\n"
                "برایِ دریافتِ پول از مینی‌اپ → «👤 من» → «💸 برداشت» را بزن.")
    if by == "admin":
        await _send(bot_id, buyer_tg2, f"ℹ️ پشتیبانی معاملهٔ #{deal_id} را بررسی و تکمیل کرد.")
    # گزارشِ خرید به «اطلاعِ خودکارِ خرید» و کانالِ اعتماد (همان الگویِ بقیهٔ مسیرهایِ فروش)
    try:
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from tenant import _purchase_alert
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
            buyer = await s.get(BotUser, (await s.get(P2PDeal, deal_id)).buyer_id)
        tb = Bot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await _purchase_alert(tb, row, buyer=buyer, section="بازارِ کاربران",
                                  detail=title, amount=price, order_id=deal_id, count=1)
        finally:
            try:
                await tb.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception as e:                                   # noqa: BLE001
        logger.warning("p2p: گزارشِ خریدِ معاملهٔ #%s نرفت (%s): %s", deal_id, type(e).__name__, e)
    return True, "ok"


async def open_dispute(bot_id: int, deal_id: int, *, buyer_tg: int | None = None,
                       auto: bool = False) -> tuple[bool, str]:
    async with Session() as s:
        d = await s.get(P2PDeal, deal_id)
        if not d or d.bot_id != bot_id:
            return False, "not_found"
        if not auto:
            b = await s.get(BotUser, d.buyer_id)
            if not b or int(b.telegram_id) != int(buyer_tg or 0):
                return False, "not_found"
        if not await _cas_status(s, deal_id, "await_buyer", "disputed",
                                 note=("پاسخ‌ندادنِ خریدار" if auto else "گزارشِ خریدار")):
            return False, "bad_status"
        buyer = await s.get(BotUser, d.buyer_id)
        seller = await s.get(BotUser, d.seller_id)
        l = await s.get(P2PListing, d.listing_id)
        title, price = d.title_snapshot, _int(d.price)
        link = (l.link if l else "") or ""
        await s.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💸 آزاد شود به فروشنده", callback_data=f"p2p:ar:{deal_id}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="↩️ برگشت به خریدار", callback_data=f"p2p:af:{deal_id}",
                             style=ButtonStyle.DANGER)]])
    await _notify_admins(
        bot_id,
        f"⚠️ <b>اختلاف در معاملهٔ #{deal_id}</b>"
        + (" (پاسخ‌ندادنِ خریدار)" if auto else "") + "\n\n"
        f"🎁 <b>{_html.escape(title)}</b>" + (f"\n🔗 {_html.escape(link)}" if link else "") + "\n"
        f"مبلغِ نگه‌داشته‌شده: <b>{_rial(price)}</b> تومان\n"
        f"خریدار: {_who(buyer)}\nفروشنده: {_who(seller)}\n\n"
        "با هر دو نفر صحبت کن و بعد یکی از دکمه‌ها را بزن:", kb)
    await _send(bot_id, buyer.telegram_id if buyer else 0,
                f"📨 پروندهٔ معاملهٔ #{deal_id} به پشتیبانی رفت. تا نتیجه، مبلغ امن نزدِ ربات می‌ماند.")
    await _send(bot_id, seller.telegram_id if seller else 0,
                f"📨 خریدارِ معاملهٔ #{deal_id} مشکل گزارش کرد؛ پشتیبانی بررسی می‌کند و با تو تماس می‌گیرد.")
    return True, "ok"


# ───────────────────────── برداشتِ مبلغِ فروش ─────────────────────────

async def withdrawable(s, bot_id: int, user: BotUser) -> int:
    """سقفِ برداشت = min(موجودی، فروش‌هایِ تکمیل‌شده − برداشت‌هایِ ثبت‌شده).

    فقط پولی که از فروش در همین بازار به‌دست آمده قابل برداشت است؛ شارژهایِ
    معمولیِ کیف‌پول از این مسیر خارج نمی‌شوند.
    """
    earned = _int(await s.scalar(select(sa_func.coalesce(sa_func.sum(P2PDeal.price), 0)).where(
        P2PDeal.bot_id == bot_id, P2PDeal.seller_id == user.id,
        P2PDeal.status.in_(("completed", "released")))))
    taken = _int(await s.scalar(select(sa_func.coalesce(sa_func.sum(P2PWithdrawal.amount), 0)).where(
        P2PWithdrawal.bot_id == bot_id, P2PWithdrawal.user_id == user.id,
        P2PWithdrawal.status.in_(("pending", "paid")))))
    bal = _int((await s.get(BotUser, user.id)).balance)
    return max(0, min(bal, earned - taken))


async def request_withdrawal(bot_id: int, user: BotUser, amount: int, card: str,
                             holder: str) -> tuple[bool, str, dict]:
    cfg = await get_cfg(bot_id)
    card = re.sub(r"\D", "", card or "")
    if len(card) != 16:
        return False, "bad_card", {}
    holder = (holder or "").strip()[:120]
    if len(holder) < 3:
        return False, "bad_holder", {}
    async with Session() as s:
        u = await _lock_user(s, user.id)
        avail = await withdrawable(s, bot_id, u)
        if amount <= 0 or amount > avail:
            return False, "over_limit", {"available_fa": _rial(avail)}
        if amount < cfg["min_withdraw"]:
            return False, "below_min", {"min_fa": _rial(cfg["min_withdraw"])}
        _credit(s, bot_id, u, -amount, "p2p_wd", "درخواستِ برداشتِ مبلغِ فروش")
        w = P2PWithdrawal(bot_id=bot_id, user_id=u.id, amount=amount,
                          card_number=card, card_holder=holder, status="pending")
        s.add(w)
        await s.commit()
        await s.refresh(w)
        wid, who = w.id, u
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ واریز شد", callback_data=f"p2p:wp:{wid}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد و برگشت", callback_data=f"p2p:wr:{wid}", style=ButtonStyle.DANGER)]])
    await _notify_admins(
        bot_id,
        f"💸 <b>درخواستِ برداشت</b> #{wid}\n\nکاربر: {_who(who)}\n"
        f"مبلغ: <b>{_rial(amount)}</b> تومان\n"
        f"کارت: <code>{card}</code>\nبه نامِ: <b>{_html.escape(holder)}</b>\n\n"
        "بعد از واریز دکمهٔ «واریز شد» را بزن؛ یا ردش کن تا مبلغ به موجودیِ کاربر برگردد.", kb)
    return True, "ok", {"id": wid, "amount_fa": _rial(amount)}


async def decide_withdrawal(bot_id: int, wid: int, action: str) -> tuple[bool, str]:
    async with Session() as s:
        w = await s.get(P2PWithdrawal, wid)
        if not w or w.bot_id != bot_id:
            return False, "not_found"
        new = "paid" if action == "pay" else "rejected"
        res = await s.execute(sa_update(P2PWithdrawal).where(
            P2PWithdrawal.id == wid, P2PWithdrawal.status == "pending").values(
                status=new, decided_at=_now()))
        if (res.rowcount or 0) == 0:
            return False, "bad_status"
        u = await _lock_user(s, w.user_id)
        amount = _int(w.amount)
        if new == "rejected" and u:
            _credit(s, bot_id, u, amount, "p2p_wd_refund", f"بازگشتِ برداشتِ ردشدهٔ #{wid}")
        tg = u.telegram_id if u else 0
        await s.commit()
    if new == "paid":
        await _send(bot_id, tg, f"✅ برداشتِ <b>{_rial(amount)}</b> تومانیِ تو (#{wid}) به کارتت واریز شد.")
    else:
        await _send(bot_id, tg, f"❌ درخواستِ برداشتِ #{wid} رد شد و <b>{_rial(amount)}</b> تومان به "
                                "موجودی‌ات برگشت. برایِ جزئیات با پشتیبانی تماس بگیر.")
    return True, "ok"


# ───────────────────────── زمان‌بند ─────────────────────────

async def expire_p2p_once() -> int:
    """لغوِ خودکارِ معامله‌هایِ بی‌پاسخ، ارجاعِ تأییدنشده‌ها به ادمین،
    و پایانِ اجاره‌ها. خروجی: تعدادِ تغییرها."""
    n = 0
    now = _now()
    async with Session() as s:
        bots = (await s.scalars(select(P2PDeal.bot_id).where(
            P2PDeal.status.in_(("escrow", "await_buyer"))).distinct())).all()
    for bid in bots:
        cfg = await get_cfg(bid)
        async with Session() as s:
            stale_s = (await s.scalars(select(P2PDeal.id).where(
                P2PDeal.bot_id == bid, P2PDeal.status == "escrow",
                P2PDeal.created_at <= now - _dt.timedelta(hours=cfg["seller_hours"])))).all()
            stale_b = (await s.scalars(select(P2PDeal.id).where(
                P2PDeal.bot_id == bid, P2PDeal.status == "await_buyer",
                sa_func.coalesce(P2PDeal.updated_at, P2PDeal.created_at)
                <= now - _dt.timedelta(hours=cfg["buyer_hours"])))).all()
        for did in stale_s:
            try:
                ok, _ = await cancel_deal(bid, did, by="timeout")
                n += int(ok)
            except Exception:                                # noqa: BLE001
                logger.exception("p2p: لغوِ خودکارِ #%s ناموفق", did)
        for did in stale_b:
            try:
                ok, _ = await open_dispute(bid, did, auto=True)
                n += int(ok)
            except Exception:                                # noqa: BLE001
                logger.exception("p2p: ارجاعِ خودکارِ #%s ناموفق", did)

    async with Session() as s:
        due = (await s.scalars(select(P2PListing).where(
            P2PListing.status == "rented", P2PListing.rent_until.isnot(None),
            P2PListing.rent_until <= now).limit(100))).all()
        todo = []
        for l in due:
            l.status = "available"
            l.rent_until = None
            d = await s.scalar(select(P2PDeal).where(
                P2PDeal.listing_id == l.id, P2PDeal.kind == "rent",
                P2PDeal.status.in_(("completed", "released"))).order_by(P2PDeal.id.desc()))
            seller = await s.get(BotUser, l.seller_id)
            renter = await s.get(BotUser, d.buyer_id) if d else None
            todo.append((l.bot_id, l.title, seller.telegram_id if seller else 0,
                         renter.telegram_id if renter else 0))
        await s.commit()
    for bid, title, stg, rtg in todo:
        await _send(bid, stg, f"⏳ مهلتِ اجارهٔ <b>{_html.escape(title)}</b> تمام شد. اگر آیتم برنگشته، "
                              "از مستأجر بخواه برگرداند؛ آگهی دوباره در بازار فعال شد.")
        await _send(bid, rtg, f"⏳ مهلتِ اجارهٔ <b>{_html.escape(title)}</b> تمام شد. لطفاً آیتم را به صاحبش برگردان.")
        n += 1
    return n


async def p2p_loop(interval_sec: int = 120):
    """حلقهٔ مستقلِ worker."""
    await asyncio.sleep(20)
    while True:
        try:
            c = await expire_p2p_once()
            if c:
                logger.info("p2p_loop: %s تغییرِ خودکار", c)
        except Exception:                                    # noqa: BLE001
            logger.exception("p2p_loop: دور ناموفق بود")
        try:
            from tenant.bots.virtual_service.nft_admin import expire_due_rentals
            await expire_due_rentals()
        except Exception:                                    # noqa: BLE001
            logger.exception("p2p_loop: پایانِ اجاره‌هایِ مارکت ناموفق بود")
        await asyncio.sleep(interval_sec)


# ───────────────────────── پیام‌هایِ خطا ─────────────────────────

ERR_FA = {
    "p2p_disabled": "بازارِ کاربران فعال نیست.",
    "not_found": "پیدا نشد.",
    "bad_status": "وضعیتِ این معامله اجازهٔ این کار را نمی‌دهد (شاید قبلاً انجام شده).",
    "item_unavailable": "این آگهی دیگر موجود نیست.",
    "own_listing": "آگهیِ خودت را نمی‌توانی بخری.",
    "not_for_sale": "این آگهی برایِ فروش نیست.",
    "not_for_rent": "این آگهی برایِ اجاره نیست.",
    "bad_days": "مدتِ اجاره خارج از بازهٔ مجاز است.",
    "insufficient": "موجودی کافی نیست.",
    "bad_link": "لینکِ گیفت معتبر نیست. نمونه: https://t.me/nft/PlushPepe-1841",
    "bad_username": "یوزرنیم معتبر نیست.",
    "bad_sale_price": f"قیمتِ فروش باید حداقل {MIN_PRICE:,} تومان باشد.",
    "bad_rent_price": f"قیمتِ اجارهٔ روزانه باید حداقل {MIN_PRICE:,} تومان باشد.",
    "duplicate": "این آیتم همین الان یک آگهیِ فعال دارد.",
    "too_many": f"حداکثر {MAX_LISTINGS_PER_USER} آگهیِ فعال می‌توانی داشته باشی.",
    "listing_busy": "این آگهی الان در حالِ معامله است.",
    "bad_method": "روشِ پرداختِ کارمزد معتبر نیست.",
    "invoice_failed": "ارسالِ فاکتور ناموفق بود. اول ربات را استارت کن و دوباره امتحان کن.",
    "bad_card": "شمارهٔ کارت باید ۱۶ رقم باشد.",
    "bad_holder": "نامِ صاحبِ کارت را وارد کن.",
    "over_limit": "مبلغ بیشتر از سقفِ قابلِ برداشت است.",
    "below_min": "مبلغ کمتر از حداقلِ برداشت است.",
}


# ═════════════════════ API — کاربرِ مینی‌اپ ═════════════════════

async def _auth(bot_id: int, x_init_data, x_kb_token):
    from tenant.bots.virtual_service.nft_market import _authed
    return await _authed(bot_id, x_init_data, x_kb_token)


async def _owner(bot_id: int, x_init_data, x_kb_token):
    from tenant.bots.virtual_service.nft_market import _owner_only
    return await _owner_only(bot_id, x_init_data, x_kb_token)


def _err(code: str, status: int = 409, **extra):
    return JSONResponse({"ok": False, "error": code, "message": ERR_FA.get(code, "انجام نشد."), **extra},
                        status_code=status)


@nft_p2p_api_router.get("/api/miniapp2/{bot_id}/p2p/bootstrap")
async def api_p2p_bootstrap(bot_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    row, u = await _auth(bot_id, x_init_data, x_kb_token)
    cfg = await get_cfg(bot_id)
    out = {"ok": True, "enabled": cfg["enabled"]}
    if cfg["enabled"]:
        async with Session() as s:
            avail = await withdrawable(s, bot_id, u)
            todo = await s.scalar(select(sa_func.count(P2PDeal.id)).where(
                P2PDeal.bot_id == bot_id,
                ((P2PDeal.seller_id == u.id) & (P2PDeal.status == "escrow"))
                | ((P2PDeal.buyer_id == u.id) & (P2PDeal.status == "await_buyer")))) or 0
        out.update({"fee_options": [{"method": m, "amount": a} for m, a in fee_options(cfg)],
                    "available": avail, "available_fa": _rial(avail),
                    "min_withdraw": cfg["min_withdraw"], "todo": todo,
                    "seller_hours": cfg["seller_hours"]})
    return JSONResponse(out)


@nft_p2p_api_router.get("/api/miniapp2/{bot_id}/p2p/items")
async def api_p2p_items(bot_id: int, tab: str = "buy", kind: str = "", collection: str = "",
                        color: str = "", sort: str = "new",
                        x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    if not (await get_cfg(bot_id))["enabled"]:
        return JSONResponse({"ok": True, "enabled": False, "items": []})
    tab = "rent" if tab == "rent" else "buy"
    q = select(P2PListing).where(P2PListing.bot_id == bot_id, P2PListing.status == "available",
                                 P2PListing.mode.in_([tab, "both"]))
    if kind in ("gift", "username"):
        q = q.where(P2PListing.kind == kind)
    if collection:
        q = q.where(P2PListing.collection == collection)
    if color:
        q = q.where(P2PListing.color == color)
    pc = P2PListing.sale_price if tab == "buy" else P2PListing.rent_daily
    q = q.order_by(pc.asc() if sort == "cheap" else pc.desc() if sort == "expensive"
                   else P2PListing.id.desc())
    async with Session() as s:
        ls = (await s.scalars(q.limit(200))).all()
        sellers = {x.id: x for x in (await s.scalars(select(BotUser).where(
            BotUser.id.in_({l.seller_id for l in ls} or {0})))).all()}
    return JSONResponse({"ok": True, "enabled": True, "tab": tab,
                         "items": [_listing_json(l, sellers.get(l.seller_id), u.id) for l in ls]})


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/preview")
async def api_p2p_preview(bot_id: int, payload: dict = Body(...),
                          x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    """پیش‌نمایشِ مشخصاتِ گیفت از روی لینک (قبل از ثبتِ آگهی)."""
    await _auth(bot_id, x_init_data, x_kb_token)
    meta = await fetch_gift_meta(payload.get("link") or "")
    if not meta:
        return _err("bad_link", 400)
    return JSONResponse({"ok": True, "meta": meta})


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/listing")
async def api_p2p_listing_save(bot_id: int, payload: dict = Body(...),
                               x_init_data: str | None = Header(default=None),
                               x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    ok, code, item = await save_listing(bot_id, u, payload)
    if not ok:
        return _err(code, 400 if code.startswith("bad_") else 409)
    return JSONResponse({"ok": True, "item": item})


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/listing/{listing_id}/remove")
async def api_p2p_listing_remove(bot_id: int, listing_id: int,
                                 x_init_data: str | None = Header(default=None),
                                 x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    ok, code = await remove_listing(bot_id, listing_id, u)
    return JSONResponse({"ok": True}) if ok else _err(code)


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/order")
async def api_p2p_order(bot_id: int, payload: dict = Body(...),
                        x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    ok, code, data = await create_deal(
        bot_id, u, _int(payload.get("item_id")),
        "buy" if payload.get("kind") == "buy" else "rent", _int(payload.get("days")))
    if not ok:
        return _err(code, 402 if code == "insufficient" else 409, **data)
    return JSONResponse({"ok": True, **data})


@nft_p2p_api_router.get("/api/miniapp2/{bot_id}/p2p/my")
async def api_p2p_my(bot_id: int, x_init_data: str | None = Header(default=None),
                     x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    cfg = await get_cfg(bot_id)
    async with Session() as s:
        ls = (await s.scalars(select(P2PListing).where(
            P2PListing.bot_id == bot_id, P2PListing.seller_id == u.id,
            P2PListing.status != "removed").order_by(P2PListing.id.desc()).limit(100))).all()
        ds = (await s.scalars(select(P2PDeal).where(
            P2PDeal.bot_id == bot_id, (P2PDeal.buyer_id == u.id) | (P2PDeal.seller_id == u.id))
            .order_by(P2PDeal.id.desc()).limit(100))).all()
        users = {x.id: x for x in (await s.scalars(select(BotUser).where(
            BotUser.id.in_({d.buyer_id for d in ds} | {d.seller_id for d in ds} or {0})))).all()}
        wds = (await s.scalars(select(P2PWithdrawal).where(
            P2PWithdrawal.bot_id == bot_id, P2PWithdrawal.user_id == u.id)
            .order_by(P2PWithdrawal.id.desc()).limit(20))).all()
        avail = await withdrawable(s, bot_id, u)
    deals = []
    for d in ds:
        role = "seller" if d.seller_id == u.id else "buyer"
        other = users.get(d.buyer_id if role == "seller" else d.seller_id)
        deals.append({
            "id": d.id, "role": role, "title": d.title_snapshot, "kind": d.kind, "days": d.days,
            "price_fa": _rial(d.price), "status": d.status,
            "status_fa": (_DEAL_FA_SELLER if role == "seller" else _DEAL_FA_BUYER).get(d.status, d.status),
            "actions": deal_actions(d, role),
            # فروشنده باید بداند آیتم را به چه کسی بدهد
            "buyer_name": (other.full_name or "") if role == "seller" and other else "",
            "buyer_username": (other.username or "") if role == "seller" and other else "",
            "buyer_id": other.telegram_id if role == "seller" and other else 0,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })
    return JSONResponse({
        "ok": True, "enabled": cfg["enabled"],
        "listings": [_listing_json(l, u, u.id) for l in ls],
        "deals": deals,
        "withdrawals": [{"id": w.id, "amount_fa": _rial(w.amount), "status": w.status,
                         "card": "•••• " + (w.card_number or "")[-4:]} for w in wds],
        "available": avail, "available_fa": _rial(avail),
        "min_withdraw": cfg["min_withdraw"],
        "fee_options": [{"method": m, "amount": a} for m, a in fee_options(cfg)],
    })


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/deal/{deal_id}/{action}")
async def api_p2p_deal_action(bot_id: int, deal_id: int, action: str,
                              payload: dict = Body(default={}),
                              x_init_data: str | None = Header(default=None),
                              x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    tg = int(u.telegram_id)
    if action == "seller_cancel":
        async with Session() as s:
            d, _u = await _get_deal_for(s, bot_id, deal_id, "seller", tg)
        if not d:
            return _err("not_found", 404)
        ok, code = await cancel_deal(bot_id, deal_id, by="seller")
        return JSONResponse({"ok": True}) if ok else _err(code)
    if action == "seller_done":
        method = payload.get("method")
        ok, code, data = await seller_done(bot_id, deal_id, tg, method if method else None)
        if not ok:
            return _err(code, 402 if code == "insufficient" else 409, **data)
        return JSONResponse({"ok": True, "state": code, **data})
    if action == "buyer_ok":
        ok, code = await complete_deal(bot_id, deal_id, by="buyer", buyer_tg=tg)
        return JSONResponse({"ok": True}) if ok else _err(code)
    if action == "buyer_problem":
        ok, code = await open_dispute(bot_id, deal_id, buyer_tg=tg)
        return JSONResponse({"ok": True}) if ok else _err(code)
    raise HTTPException(status_code=400, detail="bad_action")


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/p2p/withdraw")
async def api_p2p_withdraw(bot_id: int, payload: dict = Body(...),
                           x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    _row, u = await _auth(bot_id, x_init_data, x_kb_token)
    ok, code, data = await request_withdrawal(
        bot_id, u, _int(payload.get("amount")), str(payload.get("card") or ""),
        str(payload.get("holder") or ""))
    return JSONResponse({"ok": True, **data}) if ok else _err(code, 400, **data)


# ═════════════════════ API — ادمین ═════════════════════

@nft_p2p_api_router.get("/api/miniapp2/{bot_id}/admin/p2p")
async def api_admin_p2p(bot_id: int, x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    await _owner(bot_id, x_init_data, x_kb_token)
    cfg = await get_cfg(bot_id)
    async with Session() as s:
        ds = (await s.scalars(select(P2PDeal).where(P2PDeal.bot_id == bot_id)
                              .order_by(P2PDeal.id.desc()).limit(60))).all()
        ls = (await s.scalars(select(P2PListing).where(
            P2PListing.bot_id == bot_id, P2PListing.status != "removed")
            .order_by(P2PListing.id.desc()).limit(80))).all()
        wds = (await s.scalars(select(P2PWithdrawal).where(P2PWithdrawal.bot_id == bot_id)
                               .order_by(P2PWithdrawal.id.desc()).limit(40))).all()
        uids = ({d.buyer_id for d in ds} | {d.seller_id for d in ds}
                | {l.seller_id for l in ls} | {w.user_id for w in wds})
        users = {x.id: x for x in (await s.scalars(select(BotUser).where(
            BotUser.id.in_(uids or {0})))).all()}

    def nm(i):
        x = users.get(i)
        return ((x.full_name or str(x.telegram_id)) + (f" @{x.username}" if x.username else "")) if x else "؟"
    return JSONResponse({
        "ok": True, "cfg": cfg,
        "deals": [{"id": d.id, "title": d.title_snapshot, "buyer": nm(d.buyer_id), "seller": nm(d.seller_id),
                   "kind": d.kind, "days": d.days, "price_fa": _rial(d.price), "status": d.status,
                   "status_fa": _DEAL_FA_BUYER.get(d.status, d.status), "note": d.note or "",
                   "fee": (f"{d.fee_amount} {'⭐' if d.fee_method == 'stars' else 'ت'}" if d.fee_paid else "—")}
                  for d in ds],
        "listings": [{"id": l.id, "title": l.title, "seller": nm(l.seller_id), "mode": l.mode,
                      "status": l.status, "sale_price": _int(l.sale_price),
                      "rent_daily": _int(l.rent_daily), "link": l.link or ""} for l in ls],
        "withdrawals": [{"id": w.id, "user": nm(w.user_id), "amount_fa": _rial(w.amount),
                         "status": w.status, "card": w.card_number, "holder": w.card_holder or ""}
                        for w in wds],
    })


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/admin/p2p/settings")
async def api_admin_p2p_settings(bot_id: int, payload: dict = Body(...),
                                 x_init_data: str | None = Header(default=None),
                                 x_kb_token: str | None = Header(default=None)):
    await _owner(bot_id, x_init_data, x_kb_token)
    from tenant import set_setting
    if "enabled" in payload:
        await set_setting(bot_id, "p2p_enabled", "1" if payload.get("enabled") else "0")
    if payload.get("fee_mode") in ("stars", "wallet", "both"):
        await set_setting(bot_id, "p2p_fee_mode", payload["fee_mode"])
    for key, setting, lo in (("fee_stars", "p2p_fee_stars", 0), ("fee_wallet", "p2p_fee_wallet", 0),
                             ("seller_hours", "p2p_seller_hours", 1), ("buyer_hours", "p2p_buyer_hours", 1),
                             ("min_withdraw", "p2p_min_withdraw", 0)):
        if key in payload:
            await set_setting(bot_id, setting, str(max(lo, _int(payload.get(key)))))
    return JSONResponse({"ok": True, "cfg": await get_cfg(bot_id)})


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/admin/p2p/listing/{listing_id}/remove")
async def api_admin_p2p_listing_remove(bot_id: int, listing_id: int,
                                       x_init_data: str | None = Header(default=None),
                                       x_kb_token: str | None = Header(default=None)):
    await _owner(bot_id, x_init_data, x_kb_token)
    ok, code = await remove_listing(bot_id, listing_id, None, admin=True)
    return JSONResponse({"ok": True}) if ok else _err(code)


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/admin/p2p/deal/{deal_id}/{action}")
async def api_admin_p2p_deal(bot_id: int, deal_id: int, action: str,
                             x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
    await _owner(bot_id, x_init_data, x_kb_token)
    if action == "release":
        ok, code = await complete_deal(bot_id, deal_id, by="admin")
    elif action == "refund":
        ok, code = await cancel_deal(bot_id, deal_id, by="admin", to_status="refunded")
    else:
        raise HTTPException(status_code=400, detail="bad_action")
    return JSONResponse({"ok": True}) if ok else _err(code)


@nft_p2p_api_router.post("/api/miniapp2/{bot_id}/admin/p2p/withdrawal/{wid}/{action}")
async def api_admin_p2p_withdrawal(bot_id: int, wid: int, action: str,
                                   x_init_data: str | None = Header(default=None),
                                   x_kb_token: str | None = Header(default=None)):
    await _owner(bot_id, x_init_data, x_kb_token)
    if action not in ("pay", "reject"):
        raise HTTPException(status_code=400, detail="bad_action")
    ok, code = await decide_withdrawal(bot_id, wid, action)
    return JSONResponse({"ok": True}) if ok else _err(code)


# ═════════════════════ سمتِ ربات (دکمه‌هایِ اینلاین) ═════════════════════

async def _ctx(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    return row


async def _is_admin(row, uid: int) -> bool:
    try:
        from tenant import is_owner_or_admin
        return await is_owner_or_admin(row.id, uid, int(row.owner_id))
    except Exception:                                        # noqa: BLE001
        return int(row.owner_id) == int(uid)


def _did(cb: CallbackQuery) -> int | None:
    try:
        return int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        return None


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:                                   # noqa: BLE001
        if "not modified" not in str(e).lower():
            try:
                await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
            except Exception:                                # noqa: BLE001
                pass


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:sd:"))
async def cb_seller_done(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    ok, code, data = await seller_done(row.id, did, cb.from_user.id, None)
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    if code == "free":
        ok2, code2, _ = await seller_done(row.id, did, cb.from_user.id, "free")
        await cb.answer("✅ ثبت شد." if ok2 else ERR_FA.get(code2, "انجام نشد."), show_alert=not ok2)
        if ok2:
            await _edit(cb, f"🔒 معاملهٔ #{did} قفل شد؛ منتظرِ تأییدِ خریدار هستیم.")
        return
    rows = []
    for o in data["options"]:
        if o["method"] == "stars":
            rows.append([InlineKeyboardButton(text=f"⭐ پرداخت با استارز ({o['amount']} ⭐)",
                                              callback_data=f"p2p:fs:{did}", style=ButtonStyle.PRIMARY)])
        else:
            rows.append([InlineKeyboardButton(text=f"💰 کسر از کیف‌پول ({_rial(o['amount'])} ت)",
                                              callback_data=f"p2p:fw:{did}", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"p2p:bk:{did}",
                                      style=ButtonStyle.DANGER)])
    await _edit(cb,
                f"📤 <b>تأییدِ انتقال — معاملهٔ #{did}</b>\n\n"
                "برایِ ثبتِ «انتقال دادم» باید کارمزدِ ربات را بپردازی. "
                "بعد از پرداخت، معامله قفل می‌شود و دیگر نمی‌توانی لغوش کنی.\n\n"
                "روشِ پرداختِ کارمزد را انتخاب کن:", _kb(rows))
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:bk:"))
async def cb_back(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    async with Session() as s:
        d, _ = await _get_deal_for(s, row.id, did, "seller", cb.from_user.id)
        if not d or d.status != "escrow":
            await cb.answer("این معامله دیگر فعال نیست.", show_alert=True)
            return
        buyer = await s.get(BotUser, d.buyer_id)
        txt = _seller_card_text(d.id, d.title_snapshot, d.kind, d.days, _int(d.price), buyer)
    await _edit(cb, txt, _kb([
        [InlineKeyboardButton(text="📤 انتقال دادم", callback_data=f"p2p:sd:{did}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🚫 گیفت را از قبل فروخته‌ام", callback_data=f"p2p:sc:{did}",
                              style=ButtonStyle.DANGER)]]))
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:fw:"))
async def cb_fee_wallet(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    ok, code, data = await seller_done(row.id, did, cb.from_user.id, "wallet")
    if not ok:
        msg = ERR_FA.get(code, "انجام نشد.")
        if code == "insufficient":
            msg = f"موجودیِ کیف‌پولت کافی نیست (لازم: {data.get('need_fa')} — موجودی: {data.get('balance_fa')} تومان)."
        await cb.answer(msg, show_alert=True)
        return
    await _edit(cb, f"🔒 کارمزد از کیف‌پولت کم شد و معاملهٔ #{did} قفل شد.\nمنتظرِ تأییدِ خریدار هستیم.")
    await cb.answer("✅ انجام شد")


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:fs:"))
async def cb_fee_stars(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    ok, code, _ = await seller_done(row.id, did, cb.from_user.id, "stars")
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    await cb.answer("فاکتورِ پرداخت برایت ارسال شد ⭐")


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:sc:"))
async def cb_seller_cancel(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    async with Session() as s:
        d, _ = await _get_deal_for(s, row.id, did, "seller", cb.from_user.id)
    if not d or d.status != "escrow":
        await cb.answer("این معامله دیگر قابل لغو نیست.", show_alert=True)
        return
    await _edit(cb, f"🚫 <b>لغوِ معاملهٔ #{did}</b>\n\nمطمئنی آیتم را از قبل فروخته‌ای؟ "
                    "معامله لغو و مبلغ به خریدار برمی‌گردد و آگهی‌ات از بازار برداشته می‌شود.",
                _kb([[InlineKeyboardButton(text="بله، لغو شود", callback_data=f"p2p:sc2:{did}",
                                           style=ButtonStyle.DANGER)],
                     [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"p2p:bk:{did}")]]))
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:sc2:"))
async def cb_seller_cancel2(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    async with Session() as s:
        d, _ = await _get_deal_for(s, row.id, did, "seller", cb.from_user.id)
    if not d:
        await cb.answer("پیدا نشد.", show_alert=True)
        return
    ok, code = await cancel_deal(row.id, did, by="seller")
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    await _edit(cb, f"✅ معاملهٔ #{did} لغو شد و مبلغ به خریدار برگشت.")
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:bok:"))
async def cb_buyer_ok(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    await _edit(cb, f"✅ <b>تأیید نهایی — معاملهٔ #{did}</b>\n\n"
                    "با تأیید، مبلغ به فروشنده پرداخت می‌شود و دیگر قابل بازگشت نیست. "
                    "آیتم را دریافت کرده‌ای؟",
                _kb([[InlineKeyboardButton(text="✅ بله، پرداخت شود", callback_data=f"p2p:bok2:{did}",
                                           style=ButtonStyle.SUCCESS)],
                     [InlineKeyboardButton(text="⚠️ مشکلی هست", callback_data=f"p2p:bpr:{did}",
                                           style=ButtonStyle.DANGER)]]))
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:bok2:"))
async def cb_buyer_ok2(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    ok, code = await complete_deal(row.id, did, by="buyer", buyer_tg=cb.from_user.id)
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    await _edit(cb, f"🎉 معاملهٔ #{did} با موفقیت تکمیل شد. ممنون از خریدت!")
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.startswith("p2p:bpr:"))
async def cb_buyer_problem(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    ok, code = await open_dispute(row.id, did, buyer_tg=cb.from_user.id)
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    await _edit(cb, f"📨 پروندهٔ معاملهٔ #{did} به پشتیبانی ارسال شد. پول تا نتیجه نزدِ ربات امن است.")
    await cb.answer()


@nft_p2p_bot_router.callback_query(F.data.regexp(r"^p2p:(ar|af):\d+$"))
async def cb_admin_resolve(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    did = _did(cb)
    if not row or did is None:
        return
    if not await _is_admin(row, cb.from_user.id):
        await cb.answer("دسترسی نداری.", show_alert=True)
        return
    if cb.data.split(":")[1] == "ar":
        ok, code = await complete_deal(row.id, did, by="admin")
        label = "💸 به فروشنده آزاد شد"
    else:
        ok, code = await cancel_deal(row.id, did, by="admin", to_status="refunded")
        label = "↩️ به خریدار برگشت"
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    try:
        await cb.message.edit_text((cb.message.html_text or "") + f"\n\n<b>{label}</b>", reply_markup=None)
    except Exception:                                        # noqa: BLE001
        pass
    await cb.answer(label)


@nft_p2p_bot_router.callback_query(F.data.regexp(r"^p2p:(wp|wr):\d+$"))
async def cb_admin_withdraw(cb: CallbackQuery, bot: Bot):
    row = await _ctx(cb, bot)
    wid = _did(cb)
    if not row or wid is None:
        return
    if not await _is_admin(row, cb.from_user.id):
        await cb.answer("دسترسی نداری.", show_alert=True)
        return
    paid = cb.data.split(":")[1] == "wp"
    ok, code = await decide_withdrawal(row.id, wid, "pay" if paid else "reject")
    if not ok:
        await cb.answer(ERR_FA.get(code, "انجام نشد."), show_alert=True)
        return
    label = "✅ واریز شد" if paid else "❌ رد و برگشت داده شد"
    try:
        await cb.message.edit_text((cb.message.html_text or "") + f"\n\n<b>{label}</b>", reply_markup=None)
    except Exception:                                        # noqa: BLE001
        pass
    await cb.answer(label)


# ───────── پرداختِ کارمزد با استارز (از dispatcherِ سراسریِ tenant صدا زده می‌شود) ─────────

def _parse_fee_payload(payload: str):
    try:
        _, bid, did = payload.split(":")
        return int(bid), int(did)
    except (ValueError, AttributeError):
        return None


async def fee_precheck(pcq: PreCheckoutQuery) -> tuple[bool, str]:
    """قبل از گرفتنِ استارز: معامله هنوز باید منتظرِ کارمزد باشد و پرداخت‌کننده خودِ فروشنده."""
    parsed = _parse_fee_payload(pcq.invoice_payload or "")
    if not parsed:
        return False, "فاکتور نامعتبر است."
    bid, did = parsed
    async with Session() as s:
        d = await s.get(P2PDeal, did)
        if not d or d.bot_id != bid or d.status != "escrow":
            return False, "این معامله دیگر منتظرِ کارمزد نیست."
        seller = await s.get(BotUser, d.seller_id)
        if not seller or int(seller.telegram_id) != int(pcq.from_user.id):
            return False, "این فاکتور برایِ فروشنده است."
    return True, ""


async def handle_p2p_fee_payment(msg: Message, bot: Bot, sp, payload: str) -> None:
    parsed = _parse_fee_payload(payload)
    if not parsed:
        return
    bid, did = parsed
    ok = await _finalize_fee(bid, did, "stars", int(sp.total_amount or 0))
    if ok:
        await msg.answer(f"🔒 کارمزد پرداخت شد و معاملهٔ #{did} قفل شد.\nمنتظرِ تأییدِ خریدار هستیم.")
        return
    # معامله در فاصلهٔ فاکتور تا پرداخت لغو شده → استارز را برگردان
    try:
        await bot.refund_star_payment(user_id=msg.from_user.id,
                                      telegram_payment_charge_id=sp.telegram_payment_charge_id)
        await msg.answer("ℹ️ این معامله دیگر فعال نبود؛ استارزِ کارمزد به تو برگشت.")
    except Exception as e:                                   # noqa: BLE001
        logger.error("p2p: بازگشتِ استارزِ کارمزدِ معاملهٔ #%s ناموفق بود (%s): %s — شارژ: %s",
                     did, type(e).__name__, e, getattr(sp, "telegram_payment_charge_id", ""))
        await msg.answer("⚠️ معامله دیگر فعال نبود و بازگشتِ خودکارِ استارز ممکن نشد؛ پشتیبانی پیگیری می‌کند.")
