"""
nft_market.py — «مینی‌اپ ۲»: مارکتِ خرید و اجارهٔ گیفتِ NFT و یوزرنیم.

=== PATCH_NFT_MARKET_V1 ===

مخصوصِ ربات‌هایِ «خدماتِ مجازی» (field == "virtual_service") و فقط پلنِ
حرفه‌ای (Feature.NFT_MARKET). مینی‌اپِ اولِ فروشگاهی (tenant/miniapp.py)
دست‌نخورده می‌ماند؛ این یک صفحهٔ جداست رویِ مسیرِ /miniapp2/{bot_id}.

موجودی مالِ خودِ ادمینِ مستأجر است (نه پلتفرم): او آیتم‌ها را از داخلِ
همین مینی‌اپ (تبِ «مدیریت») ثبت، قیمت‌گذاری و مدیریت می‌کند.

دو مسیرِ تحویل، هر دو پشتیبانی می‌شوند و در سطحِ هر سفارش انتخاب می‌شوند:
  • auto   — انتقالِ خودکارِ گیفت با Bot APIِ بیزینس (transferGift). فقط
             وقتی ادمین اکانتش را به‌عنوانِ Business به ربات وصل کرده و
             اجازهٔ انتقالِ گیفت داده باشد (مدلِ NftBizConn).
  • manual — ربات سفارش را ثبت و به ادمین اطلاع می‌دهد؛ ادمین دستی منتقل
             می‌کند و دکمهٔ «تحویل شد» را می‌زند.
اگر انتقالِ خودکار شکست بخورد، سفارش خودبه‌خود به حالتِ دستی برمی‌گردد
(transfer_note دلیلش را نگه می‌دارد) — یعنی هیچ سفارشی گم نمی‌شود.

پرداخت از کیف‌پولِ همان ربات کسر می‌شود (همان WalletTxn همیشگی)، پس هیچ
منطقِ پرداختی اینجا بازسازی نمی‌شود.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import time

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func as sa_func

from config import Session
from models import (
    Bot as BotRow, BotUser, WalletTxn,
    NftItem, NftOrder, NftBizConn, NftMarketSource,
)
from plans import Feature, has_feature

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

nft_market_router = APIRouter()

# کارمزدِ ثابتِ انتقالِ گیفتِ تلگرام (۲۵ استارز) — ادمین می‌تواند از پنل
# عوضش کند؛ این فقط پیش‌فرضِ اولیه است.
_DEFAULT_TRANSFER_FEE = 0


# ───────────────────────── کمکی‌ها ─────────────────────────

def _rial(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


async def _market_enabled(row) -> bool:
    """مارکت فقط برایِ رباتِ فعالِ خدماتِ مجازی با پلنِ حرفه‌ای باز است."""
    if not row or row.status != "active":
        return False
    if (row.field or "") != "virtual_service":
        return False
    return has_feature(row.plan, Feature.NFT_MARKET)


async def _authed(bot_id: int, x_init_data: str | None, x_kb_token: str | None):
    """احرازِ هویتِ کاربرِ مینی‌اپ + گیتِ مارکت.

    === PATCH_NFT_MARKET_STANDALONE_AUTH_V2 ===
    قبلاً مستقیم _authed_context مینی‌اپِ اول را صدا می‌زد، ولی آن تابع
    شرطِ `row.miniapp_enabled` را هم دارد — یعنی اگر مستأجر مینی‌اپِ
    فروشگاهی را روشن نکرده بود، مارکت هم ۴۰۴ می‌شد. این دو محصولِ جدا
    هستند و نباید به هم گره بخورند. حالا فقط *توابعِ اعتبارسنجی* از آنجا
    قرض گرفته می‌شوند (تا الگوریتمِ امنیتی یک نسخه بماند) و خودِ گیت
    این‌جا مستقل انجام می‌شود.
    """
    from tenant.shared.miniapp import _check_init_data, _check_kb_token

    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        if not await _market_enabled(row):
            raise HTTPException(status_code=403, detail="market_disabled")

        parsed = _check_init_data(x_init_data or "", row.token)
        if parsed and parsed.get("user"):
            tg_user = parsed["user"]
            tg_id = int(tg_user["id"])
        else:
            tg_id = _check_kb_token(x_kb_token or "", row.token, bot_id)
            if not tg_id:
                raise HTTPException(status_code=401, detail="invalid_init_data")
            tg_user = {"id": tg_id}

        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        if not u:
            _name = (tg_user.get("first_name", "")
                     + ((" " + tg_user["last_name"]) if tg_user.get("last_name") else ""))
            u = BotUser(bot_id=bot_id, telegram_id=tg_id,
                        full_name=_name or str(tg_id),
                        username=tg_user.get("username") or "")
            s.add(u)
            await s.commit()
            await s.refresh(u)
        return row, u


async def _is_owner(row, u) -> bool:
    """آیا این کاربر به تبِ «مدیریت» دسترسی دارد؟

    === PATCH_NFT_MARKET_ADMIN_ACCESS_V2 ===
    نسخهٔ اول فقط owner_id را چک می‌کرد، پس ادمین‌هایِ فرعیِ ربات (جدولِ
    AdminSupport) اصلاً تبِ مدیریت را نمی‌دیدند — در حالی که در کلِ بقیهٔ
    پنلِ مستأجر با is_owner_or_admin سنجیده می‌شوند. حالا همان تابعِ
    مشترک استفاده می‌شود تا رفتار همه‌جا یکی باشد.
    """
    if not row or not u:
        return False
    try:
        from tenant import is_owner_or_admin
        return await is_owner_or_admin(row.id, int(u.telegram_id), int(row.owner_id))
    except Exception:                                        # noqa: BLE001
        return int(row.owner_id) == int(u.telegram_id)


def _item_json(it: NftItem, *, owner: bool = False) -> dict:
    d = {
        "id": it.id,
        "kind": it.kind,
        "title": it.title,
        "ref": it.ref or "",
        "collection": it.collection or "",
        "model": it.model or "",
        "color": it.color or "",
        "symbol": it.symbol or "",
        "image": it.image_url or "",
        "mode": it.mode,
        "sale_price": int(it.sale_price or 0),
        "rent_daily": int(it.rent_daily or 0),
        "rent_min_days": it.rent_min_days or 1,
        "rent_max_days": it.rent_max_days or 180,
        "status": it.status,
    }
    if owner:
        # === PATCH_NFT_SOURCES_V1 === سود برایِ آیتم‌هایِ واسطه‌گری
        d["cost_price"] = int(it.cost_price or 0)
        d["source_id"] = it.source_id
        d["profit"] = int((it.sale_price or 0) - (it.cost_price or 0)) if it.cost_price else 0
        d["holder_user_id"] = it.holder_user_id
        d["rent_until"] = it.rent_until.isoformat() if it.rent_until else None
        d["owned_gift_id"] = it.owned_gift_id or ""
    return d


async def _ton_rate(bot_id: int) -> int:
    """نرخِ هر TON به تومان، برایِ نمایشِ دوگانهٔ قیمت (مثلِ خودِ مارکت‌هایِ
    NFT). صفر یعنی ادمین نرخ نگذاشته و قیمتِ TON اصلاً نمایش داده نمی‌شود."""
    from tenant import get_setting
    try:
        return int(float(await get_setting(bot_id, "nft_ton_rate", "0") or 0))
    except (TypeError, ValueError):
        return 0


async def _transfer_fee(bot_id: int) -> int:
    """کارمزدِ ثابتی که ادمین برایِ انتقالِ هر آیتم تعیین کرده (تومان)."""
    from tenant import get_setting
    try:
        return int(float(await get_setting(bot_id, "nft_transfer_fee", "0") or 0))
    except (TypeError, ValueError):
        return _DEFAULT_TRANSFER_FEE


# ───────────────────────── تمِ ظاهری ─────────────────────────
# === PATCH_NFT_THEME_V1 ===
# درخواستِ مستأجر: مینی‌اپ ۲ هم مثلِ مینی‌اپِ فروشگاهی «رنگِ دکمه‌ها»، «تمِ اصلی» (پس‌زمینه) و
# «رنگِ ثانویه» (کارت‌ها) داشته باشد. سه کلیدِ مستقل در Setting: nft_accent / nft_bg / nft_card
# (جدا از کلیدهایِ miniapp_* تا ظاهرِ دو مینی‌اپ به هم گره نخورد). خالی = پیش‌فرضِ قبلی.

_HEX_RE = None


def _clean_hex(v) -> str:
    import re
    v = (str(v or "")).strip()
    if v and not v.startswith("#"):
        v = "#" + v
    return v if re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", v or "") else ""


async def _theme_vals(bot_id: int) -> dict:
    from tenant import get_setting
    return {"accent": _clean_hex(await get_setting(bot_id, "nft_accent", "")),
            "bg": _clean_hex(await get_setting(bot_id, "nft_bg", "")),
            "card": _clean_hex(await get_setting(bot_id, "nft_card", ""))}


def build_theme_css(accent: str, bg: str, card: str) -> str:
    """CSSِ بازنویسیِ متغیرها؛ رشتهٔ خالی یعنی همان ظاهرِ پیش‌فرض."""
    if not (accent or bg or card):
        return ""
    from tenant.shared.miniapp import _lighten, _darken, _rgba, _is_dark
    root: list[str] = []
    extra: list[str] = []
    if accent:
        a2 = _lighten(accent, 0.22)
        root += [f"--p1:{accent}", f"--p2:{a2}"]
        extra += [
            "body::before{background:"
            f"radial-gradient(760px 380px at 82% -8%,{_rgba(accent, .28)},transparent 64%),"
            f"radial-gradient(620px 320px at 8% 4%,{_rgba(a2, .20)},transparent 62%),"
            "radial-gradient(700px 420px at 50% 104%,rgba(63,162,255,.14),transparent 66%)}",
            f".cta{{box-shadow:0 8px 26px {_rgba(accent, .42)}}}",
            f".btn.pri{{box-shadow:0 4px 16px {_rgba(accent, .38)}}}",
            f".seg div.on{{box-shadow:0 5px 20px {_rgba(accent, .42)}}}",
            f".slogo{{box-shadow:0 18px 56px {_rgba(accent, .5)}}}",
            f".hero-h,.me-hero{{background:linear-gradient(135deg,{_rgba(accent, .30)},{_rgba(a2, .20)} 52%,rgba(63,162,255,.16))}}",
            f".total{{background:linear-gradient(135deg,{_rgba(accent, .20)},{_rgba(a2, .13)})}}",
            f".wal{{background:linear-gradient(135deg,{_rgba(accent, .24)},{_rgba(a2, .18)})}}",
            f".bar div.on{{border-color:{accent};background:{_rgba(accent, .18)}}}",
            f"input.f:focus,select.f:focus{{border-color:{accent}}}",
        ]
    base = bg or "#07070a"
    dark = _is_dark(base)
    if bg:
        txt = "#f5f5f8" if dark else "#111114"
        dim = "#9292a2" if dark else "#6c6c75"
        faint = "#61616f" if dark else "#9a9aa2"
        line = "rgba(255,255,255,.08)" if dark else "rgba(10,10,20,.09)"
        line2 = "rgba(255,255,255,.14)" if dark else "rgba(10,10,20,.16)"
        root += [f"--bg:{bg}", f"--txt:{txt}", f"--dim:{dim}", f"--faint:{faint}",
                 f"--line:{line}", f"--line-2:{line2}"]
        extra += [
            f".top{{background:linear-gradient(to bottom,{_rgba(bg, .96)} 62%,{_rgba(bg, 0)})}}",
            f".nav{{background:{_rgba(bg, .92)}}}",
            f"#splash{{background:{bg}}}",
        ]
        if not card:
            card_c = _lighten(base, 0.07) if dark else "#ffffff"
            root += [f"--surface:{_rgba(card_c, .82)}", f"--solid:{card_c}",
                     f"--surface-2:{(_lighten(base, 0.11) if dark else '#f1f2f5')}"]
            extra.append(f".sbox{{background:linear-gradient(180deg,{(_lighten(card_c, 0.05) if dark else '#ffffff')},{card_c} 30%)}}")
    if card:
        cdark = _is_dark(card)
        c2 = _lighten(card, 0.06) if cdark else _darken(card, 0.04)
        root += [f"--surface:{_rgba(card, .88)}", f"--solid:{card}", f"--surface-2:{c2}"]
        extra.append(f".sbox{{background:linear-gradient(180deg,{c2},{card} 30%)}}")
        if not bg and not cdark:
            pass   # کارتِ روشن روی پس‌زمینهٔ تیره: متن‌هایِ داخلِ کارت باید تیره باشد
        if not cdark:
            extra.append(".card,.ord,.deal,.adm,.sbox,.row,.cname{color:#111114}")
    css = ":root{" + ";".join(root) + "}" if root else ""
    return "/* nft-theme */" + css + "".join(extra)


# ───────────────────────── صفحه ─────────────────────────

@nft_market_router.get("/miniapp2/{bot_id}", response_class=HTMLResponse)
async def miniapp2_page(bot_id: int):
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        enabled = await _market_enabled(row)
        bot_username = (row.username or "") if row else ""
    if not enabled:
        return HTMLResponse(content=_DISABLED_HTML, status_code=404,
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    _tv = await _theme_vals(bot_id)
    _css = build_theme_css(_tv["accent"], _tv["bg"], _tv["card"])
    html = (_PAGE_HTML
            .replace("__BOT_ID__", str(bot_id))
            .replace("__BOT_USERNAME__", _html.escape(("@" + bot_username) if bot_username else "ربات"))
            .replace("__BOT_LINK__", _html.escape(("https://t.me/" + bot_username) if bot_username else "#"))
            .replace("__BUILD_ID__", str(int(time.time()))))
    # === PATCH_NFT_THEME_V1 === استایلِ تم بعد از استایلِ اصلی می‌آید تا متغیرها را بازنویسی کند
    html = html.replace("</head>", f'<style id="thm">{_css}</style></head>', 1)
    if _tv["bg"]:
        html = html.replace("#08080a", _tv["bg"])
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0",
    })


# ───────────────────────── API — کاربرِ نهایی ─────────────────────────

@nft_market_router.get("/api/miniapp2/{bot_id}/bootstrap")
async def api_bootstrap(bot_id: int, x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    """اطلاعاتِ اولیه: موجودیِ کاربر، دسترسیِ مدیریت، کارمزد، فیلترها."""
    row, u = await _authed(bot_id, x_init_data, x_kb_token)
    try:
        return await _bootstrap_body(bot_id, row, u)
    except HTTPException:
        raise
    except Exception:
        logger.exception("miniapp2 bootstrap failed (bot_id=%s)", bot_id)
        raise HTTPException(status_code=500, detail="server_error")


async def _bootstrap_body(bot_id: int, row, u):
    async with Session() as s:
        cols = (await s.scalars(select(NftItem.collection).where(
            NftItem.bot_id == bot_id, NftItem.collection.isnot(None)).distinct())).all()
        colors = (await s.scalars(select(NftItem.color).where(
            NftItem.bot_id == bot_id, NftItem.color.isnot(None)).distinct())).all()
        my_orders = await s.scalar(select(sa_func.count(NftOrder.id)).where(
            NftOrder.bot_id == bot_id, NftOrder.user_id == u.id)) or 0
    return JSONResponse({
        "ok": True,
        "balance": int(float(u.balance or 0)),
        "balance_fa": _rial(u.balance or 0),
        "is_owner": await _is_owner(row, u),
        "fee": await _transfer_fee(bot_id),
        "ton_rate": await _ton_rate(bot_id),
        "collections": sorted([c for c in cols if c]),
        "colors": sorted([c for c in colors if c]),
        "my_orders": my_orders,
        "theme": await _theme_vals(bot_id),   # PATCH_NFT_THEME_V1
        "bot_link": (f"https://t.me/{row.username}" if row.username else ""),
    })


@nft_market_router.get("/api/miniapp2/{bot_id}/items")
async def api_items(bot_id: int, tab: str = "rent", kind: str = "",
                    collection: str = "", color: str = "", sort: str = "new",
                    x_init_data: str | None = Header(default=None),
                    x_kb_token: str | None = Header(default=None)):
    """کاتالوگِ آیتم‌هایِ در دسترس. tab: buy | rent"""
    await _authed(bot_id, x_init_data, x_kb_token)
    try:
        return await _items_body(bot_id, tab, kind, collection, color, sort)
    except HTTPException:
        raise
    except Exception:
        logger.exception("miniapp2 items failed (bot_id=%s tab=%s)", bot_id, tab)
        raise HTTPException(status_code=500, detail="server_error")


async def _items_body(bot_id: int, tab: str, kind: str, collection: str, color: str, sort: str):
    tab = "buy" if tab == "buy" else "rent"
    q = select(NftItem).where(NftItem.bot_id == bot_id,
                              NftItem.status == "available")
    q = q.where(NftItem.mode.in_([tab, "both"]))
    if kind in ("gift", "username"):
        q = q.where(NftItem.kind == kind)
    if collection:
        q = q.where(NftItem.collection == collection)
    if color:
        q = q.where(NftItem.color == color)

    price_col = NftItem.sale_price if tab == "buy" else NftItem.rent_daily
    if sort == "cheap":
        q = q.order_by(price_col.asc().nullslast())
    elif sort == "expensive":
        q = q.order_by(price_col.desc().nullslast())
    else:
        q = q.order_by(NftItem.sort_order.asc(), NftItem.id.desc())

    async with Session() as s:
        items = (await s.scalars(q.limit(200))).all()
    return JSONResponse({"ok": True, "tab": tab,
                         "items": [_item_json(i) for i in items]})


@nft_market_router.post("/api/miniapp2/{bot_id}/order")
async def api_order(bot_id: int, payload: dict = Body(...),
                    x_init_data: str | None = Header(default=None),
                    x_kb_token: str | None = Header(default=None)):
    """ثبتِ سفارشِ خرید یا اجاره + کسر از کیف‌پول.

    کلِ عملیات داخلِ یک تراکنش است و آیتم با شرطِ status == "available"
    آپدیت می‌شود، تا دو نفر نتوانند هم‌زمان یک آیتم را بگیرند.
    """
    row, u = await _authed(bot_id, x_init_data, x_kb_token)
    try:
        item_id = int(payload.get("item_id") or 0)
        kind = "buy" if payload.get("kind") == "buy" else "rent"
        days = int(payload.get("days") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_input")
    if not item_id:
        raise HTTPException(status_code=400, detail="bad_item")

    fee = await _transfer_fee(bot_id)

    async with Session() as s:
        it = await s.get(NftItem, item_id)
        if not it or it.bot_id != bot_id or it.status != "available":
            raise HTTPException(status_code=409, detail="item_unavailable")
        if kind == "buy" and it.mode not in ("buy", "both"):
            raise HTTPException(status_code=409, detail="not_for_sale")
        if kind == "rent" and it.mode not in ("rent", "both"):
            raise HTTPException(status_code=409, detail="not_for_rent")

        if kind == "buy":
            base = int(it.sale_price or 0)
            days = 0
            if base <= 0:
                raise HTTPException(status_code=409, detail="no_price")
        else:
            lo, hi = (it.rent_min_days or 1), (it.rent_max_days or 180)
            if days < lo or days > hi:
                raise HTTPException(status_code=400, detail="bad_days")
            daily = int(it.rent_daily or 0)
            if daily <= 0:
                raise HTTPException(status_code=409, detail="no_price")
            base = daily * days

        total = base + fee
        user = await s.get(BotUser, u.id)
        bal = int(float(user.balance or 0))
        if bal < total:
            return JSONResponse({"ok": False, "error": "insufficient",
                                 "need": total, "need_fa": _rial(total),
                                 "balance_fa": _rial(bal)}, status_code=402)

        # قفلِ خوش‌بینانه: فقط اگر هنوز available باشد رزرو می‌شود
        from sqlalchemy import update as _update
        res = await s.execute(
            _update(NftItem)
            .where(NftItem.id == it.id, NftItem.status == "available")
            .values(status="reserved", holder_user_id=user.id))
        if (res.rowcount or 0) == 0:
            raise HTTPException(status_code=409, detail="item_unavailable")

        user.balance = bal - total
        s.add(WalletTxn(bot_id=bot_id, user_id=user.id, amount=-total,
                        kind="purchase",
                        note=(f"خرید NFT: {it.title}" if kind == "buy"
                              else f"اجارهٔ NFT: {it.title} ({days} روز)")))
        expires = (_dt.datetime.utcnow() + _dt.timedelta(days=days)) if kind == "rent" else None
        order = NftOrder(bot_id=bot_id, user_id=user.id, item_id=it.id,
                         kind=kind, days=days, amount=total, fee=fee,
                         status="pending", transfer_mode="manual",
                         expires_at=expires)
        s.add(order)
        await s.commit()
        await s.refresh(order)
        order_id, item_title = order.id, it.title

    # تلاش برایِ انتقالِ خودکار؛ اگر نشد، دستی می‌ماند و ادمین خبردار می‌شود.
    await _deliver_or_notify(bot_id, order_id)

    return JSONResponse({"ok": True, "order_id": order_id,
                         "title": item_title, "amount_fa": _rial(total)})


@nft_market_router.get("/api/miniapp2/{bot_id}/my")
async def api_my(bot_id: int, x_init_data: str | None = Header(default=None),
                 x_kb_token: str | None = Header(default=None)):
    """سفارش‌هایِ خودِ کاربر."""
    row, u = await _authed(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        rows = (await s.execute(
            select(NftOrder, NftItem)
            .join(NftItem, NftItem.id == NftOrder.item_id)
            .where(NftOrder.bot_id == bot_id, NftOrder.user_id == u.id)
            .order_by(NftOrder.id.desc()).limit(100))).all()
    out = []
    for o, it in rows:
        out.append({
            "id": o.id, "kind": o.kind, "days": o.days,
            "title": it.title, "image": it.image_url or "",
            "amount_fa": _rial(o.amount), "status": o.status,
            "status_fa": _ORDER_STATUS_FA.get(o.status, o.status),
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
        })
    return JSONResponse({"ok": True, "orders": out})


_ORDER_STATUS_FA = {
    "pending": "در انتظارِ تحویل",
    "paid": "پرداخت‌شده",
    "delivered": "تحویل شد",
    "returned": "بازگشت داده شد",
    "rejected": "لغو و عودت شد",
    "expired": "مهلت تمام شد",
}


# ───────────────────────── API — مدیریت (داخلِ مینی‌اپ) ─────────────────────────

async def _owner_only(bot_id: int, x_init_data, x_kb_token):
    row, u = await _authed(bot_id, x_init_data, x_kb_token)
    if not await _is_owner(row, u):
        raise HTTPException(status_code=403, detail="not_owner")
    return row, u


@nft_market_router.get("/api/miniapp2/{bot_id}/admin/items")
async def api_admin_items(bot_id: int, x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    await _owner_only(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        items = (await s.scalars(select(NftItem).where(NftItem.bot_id == bot_id)
                                 .order_by(NftItem.id.desc()).limit(500))).all()
        conn = await s.scalar(select(NftBizConn).where(
            NftBizConn.bot_id == bot_id, NftBizConn.is_enabled.is_(True)))
    return JSONResponse({
        "ok": True,
        "items": [_item_json(i, owner=True) for i in items],
        "fee": await _transfer_fee(bot_id),
        "ton_rate": await _ton_rate(bot_id),
        "auto_ready": bool(conn and conn.can_transfer),
    })


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/item")
async def api_admin_item_save(bot_id: int, payload: dict = Body(...),
                              x_init_data: str | None = Header(default=None),
                              x_kb_token: str | None = Header(default=None)):
    """ساخت یا ویرایشِ یک آیتم (id خالی یعنی جدید)."""
    await _owner_only(bot_id, x_init_data, x_kb_token)
    title = (payload.get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(status_code=400, detail="no_title")
    mode = payload.get("mode") if payload.get("mode") in ("buy", "rent", "both") else "both"
    kind = "username" if payload.get("kind") == "username" else "gift"

    def _num(key, default=0):
        try:
            return max(0, int(float(payload.get(key) or default)))
        except (TypeError, ValueError):
            return default

    item_id = payload.get("id")

    # === PATCH_NFT_ITEM_LINK_DEDUP_V1 ===
    # ادمین لینکِ t.me/nft/... را در «شماره» یا «لینکِ تصویر» می‌گذاشت؛ همان‌طور ذخیره می‌شد، تصویر
    # لود نمی‌شد (کارتِ صورتی) و شماره هم لینک بود. حالا لینک تشخیص داده و مشخصات از خودِ لینک پر می‌شود.
    ref_in = (payload.get("ref") or "").strip()
    img_in = (payload.get("image") or "").strip()
    collection_in = (payload.get("collection") or "").strip()[:80]
    model_in = (payload.get("model") or "").strip()[:80]
    color_in = (payload.get("color") or "").strip()[:80]
    symbol_in = (payload.get("symbol") or "").strip()[:80]
    if kind == "gift":
        from tenant.bots.virtual_service.nft_p2p import parse_gift_link, fetch_gift_meta
        parsed = None
        for cand in (img_in, ref_in, (payload.get("link") or "").strip()):
            parsed = parse_gift_link(cand) if cand else None
            if parsed:
                break
        if parsed:
            norm, _slug, num = parsed
            if not ref_in or parse_gift_link(ref_in):
                ref_in = num
            need = (not img_in or not img_in.lower().startswith(("http://", "https://"))
                    or not (model_in and color_in and symbol_in and collection_in))
            if need:
                meta = await fetch_gift_meta(norm)
                if not img_in.lower().startswith(("http://", "https://")):
                    img_in = meta.get("image") or ""
                collection_in = collection_in or (meta.get("collection") or "")[:80]
                model_in = model_in or (meta.get("model") or "")[:80]
                color_in = color_in or (meta.get("color") or "")[:80]
                symbol_in = symbol_in or (meta.get("symbol") or "")[:80]
    if img_in and not img_in.lower().startswith(("http://", "https://")):
        img_in = ""      # متنِ بی‌معنی (نه URL) ذخیره نشود
    ref_in = ref_in[:120]

    async with Session() as s:
        if not item_id:
            # قفلِ اتمیکِ هر (ربات، آیتم): دو درخواستِ هم‌زمان (دوبار-زدنِ ذخیره) هر دو از بررسی رد نمی‌شوند
            if s.bind is not None and s.bind.dialect.name == "postgresql":
                import zlib as _zlib
                from sqlalchemy import text as _text
                await s.execute(_text("SELECT pg_advisory_xact_lock(:k)"), {
                    "k": _zlib.crc32(f"nftitem:{bot_id}:{kind}:{title.lower()}:{ref_in.lower()}".encode())})
            dup = await s.scalar(select(NftItem.id).where(
                NftItem.bot_id == bot_id, NftItem.kind == kind,
                sa_func.lower(NftItem.title) == title.lower(),
                (sa_func.lower(NftItem.ref) == ref_in.lower()) if ref_in else NftItem.ref.is_(None),
                NftItem.status.in_(("available", "reserved"))).limit(1))
            if dup:
                raise HTTPException(status_code=409, detail="duplicate")
        if item_id:
            it = await s.get(NftItem, int(item_id))
            if not it or it.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
        else:
            it = NftItem(bot_id=bot_id)
            s.add(it)
        it.kind = kind
        it.title = title
        it.ref = ref_in or None
        it.collection = collection_in or None
        it.model = model_in or None
        it.color = color_in or None
        it.symbol = symbol_in or None
        it.image_url = img_in[:500] or None
        it.owned_gift_id = (payload.get("owned_gift_id") or "").strip()[:120] or None
        it.mode = mode
        it.sale_price = _num("sale_price")
        it.rent_daily = _num("rent_daily")
        it.rent_min_days = max(1, _num("rent_min_days", 1))
        it.rent_max_days = max(it.rent_min_days, _num("rent_max_days", 180))
        if payload.get("status") in ("available", "hidden"):
            # فقط این دو را ادمین دستی عوض می‌کند؛ sold/rented را جریانِ
            # سفارش می‌سازد و دست‌کاریِ دستی‌اش وضعیت را خراب می‌کند.
            it.status = payload["status"]
        await s.commit()
        await s.refresh(it)
        return JSONResponse({"ok": True, "item": _item_json(it, owner=True)})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/item/{item_id}/delete")
async def api_admin_item_delete(bot_id: int, item_id: int,
                                x_init_data: str | None = Header(default=None),
                                x_kb_token: str | None = Header(default=None)):
    await _owner_only(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        it = await s.get(NftItem, item_id)
        if not it or it.bot_id != bot_id:
            raise HTTPException(status_code=404, detail="not_found")
        if it.status in ("reserved", "sold", "rented"):
            raise HTTPException(status_code=409, detail="item_busy")
        await s.delete(it)
        await s.commit()
    return JSONResponse({"ok": True})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/theme")
async def api_admin_theme(bot_id: int, payload: dict = Body(...),
                          x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    """ذخیرهٔ رنگ‌هایِ مینی‌اپ ۲. مقدارِ خالی = برگشت به پیش‌فرض. CSSِ تازه برگردانده می‌شود تا همان لحظه اعمال شود."""
    await _owner_only(bot_id, x_init_data, x_kb_token)
    from tenant import set_setting
    for key, setting in (("accent", "nft_accent"), ("bg", "nft_bg"), ("card", "nft_card")):
        if key in payload:
            await set_setting(bot_id, setting, _clean_hex(payload.get(key)))
    tv = await _theme_vals(bot_id)
    return JSONResponse({"ok": True, "theme": tv,
                         "css": build_theme_css(tv["accent"], tv["bg"], tv["card"])})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/fee")
async def api_admin_fee(bot_id: int, payload: dict = Body(...),
                        x_init_data: str | None = Header(default=None),
                        x_kb_token: str | None = Header(default=None)):
    await _owner_only(bot_id, x_init_data, x_kb_token)
    from tenant import set_setting
    try:
        fee = max(0, int(float(payload.get("fee") or 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_fee")
    await set_setting(bot_id, "nft_transfer_fee", str(fee))
    if payload.get("ton_rate") is not None:
        try:
            await set_setting(bot_id, "nft_ton_rate",
                              str(max(0, int(float(payload.get("ton_rate") or 0)))))
        except (TypeError, ValueError):
            pass
    return JSONResponse({"ok": True, "fee": fee,
                         "ton_rate": await _ton_rate(bot_id)})


# === PATCH_NFT_SOURCES_V1 === منابعِ مارکت (واسطه‌گری)

def _source_json(src: NftMarketSource) -> dict:
    return {
        "id": src.id,
        "provider": src.provider,
        "title": src.title or src.provider,
        "enabled": bool(src.is_enabled),
        "has_auth": bool(src.encrypted_auth),
        "markup": float(src.markup_percent or 0),
        "auto_hide": bool(src.auto_hide_missing),
        "collection": src.collection_filter or "",
        "max_items": src.max_items or 60,
        "state": src.token_state or "unknown",
        "error": src.last_error or "",
        "last_sync": src.last_sync_at.isoformat() if src.last_sync_at else None,
        "last_ok": src.last_ok_at.isoformat() if src.last_ok_at else None,
    }


@nft_market_router.get("/api/miniapp2/{bot_id}/admin/sources")
async def api_admin_sources(bot_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    await _owner_only(bot_id, x_init_data, x_kb_token)
    from tenant.bots.virtual_service.nft_sources import provider_list
    async with Session() as s:
        rows = (await s.scalars(select(NftMarketSource).where(
            NftMarketSource.bot_id == bot_id).order_by(NftMarketSource.id))).all()
    return JSONResponse({"ok": True,
                         "sources": [_source_json(r) for r in rows],
                         "providers": provider_list(),
                         "ton_rate": await _ton_rate(bot_id)})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/source")
async def api_admin_source_save(bot_id: int, payload: dict = Body(...),
                                x_init_data: str | None = Header(default=None),
                                x_kb_token: str | None = Header(default=None)):
    """ساخت/ویرایشِ منبع. توکن فقط وقتی نوشته می‌شود که مقدارِ تازه بیاید،
    تا ویرایشِ درصدِ سود توکنِ سالم را پاک نکند."""
    await _owner_only(bot_id, x_init_data, x_kb_token)
    from tenant.bots.virtual_service.nft_sources import get_provider

    provider = (payload.get("provider") or "tonnel").lower()
    prov = get_provider(provider)
    if not prov:
        raise HTTPException(status_code=400, detail="bad_provider")
    if not prov.ready:
        raise HTTPException(status_code=409, detail="provider_not_ready")

    def _num(key, default, lo, hi):
        try:
            return max(lo, min(hi, float(payload.get(key, default))))
        except (TypeError, ValueError):
            return default

    sid = payload.get("id")
    async with Session() as s:
        if sid:
            src = await s.get(NftMarketSource, int(sid))
            if not src or src.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
        else:
            src = NftMarketSource(bot_id=bot_id)
            s.add(src)
        src.provider = provider
        src.title = (payload.get("title") or prov.label)[:80]
        src.markup_percent = _num("markup", 15, 0, 500)
        src.max_items = int(_num("max_items", 60, 1, 200))
        src.auto_hide_missing = bool(payload.get("auto_hide", True))
        src.collection_filter = (payload.get("collection") or "").strip()[:300] or None
        if payload.get("enabled") is not None:
            src.is_enabled = bool(payload["enabled"])
        auth = (payload.get("auth") or "").strip()
        if auth:
            # === PATCH_NFT_AUTH_NORMALIZE_V1 === هر شکلی از توکن/لینک را بپذیر (کاربرِ گوشی)
            from tenant.bots.virtual_service.nft_sources import normalize_auth
            auth = normalize_auth(provider, auth)
        if auth:
            try:
                from wallet_crypto import encrypt_secret
                src.encrypted_auth = encrypt_secret(auth)
                src.token_state = "unknown"
                src.last_error = None
                src.expiry_warned_at = None
            except Exception:                                # noqa: BLE001
                logger.exception("nft_sources: رمزگذاریِ توکن نشد")
                raise HTTPException(status_code=500, detail="crypto_unavailable")
        await s.commit()
        await s.refresh(src)
        return JSONResponse({"ok": True, "source": _source_json(src)})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/source/{source_id}/{action}")
async def api_admin_source_action(bot_id: int, source_id: int, action: str,
                                  x_init_data: str | None = Header(default=None),
                                  x_kb_token: str | None = Header(default=None)):
    """test | sync | delete"""
    await _owner_only(bot_id, x_init_data, x_kb_token)
    if action == "delete":
        async with Session() as s:
            src = await s.get(NftMarketSource, source_id)
            if not src or src.bot_id != bot_id:
                raise HTTPException(status_code=404, detail="not_found")
            # آیتم‌هایِ واردشده از این منبع مخفی می‌شوند، نه حذف — ممکن
            # است سفارشِ در جریان به آن‌ها ارجاع داشته باشد.
            items = (await s.scalars(select(NftItem).where(
                NftItem.bot_id == bot_id, NftItem.source_id == source_id))).all()
            for it in items:
                it.source_id = None
                if it.status == "available":
                    it.status = "hidden"
            await s.delete(src)
            await s.commit()
        return JSONResponse({"ok": True})

    from tenant.bots.virtual_service.nft_sources import check_auth, sync_source
    if action == "test":
        ok, note = await check_auth(bot_id, source_id)
        return JSONResponse({"ok": ok, "note": note})
    if action == "sync":
        ok, note, stats = await sync_source(bot_id, source_id)
        return JSONResponse({"ok": ok, "note": note, "stats": stats})
    raise HTTPException(status_code=400, detail="bad_action")


@nft_market_router.get("/api/miniapp2/{bot_id}/admin/orders")
async def api_admin_orders(bot_id: int, x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    await _owner_only(bot_id, x_init_data, x_kb_token)
    async with Session() as s:
        rows = (await s.execute(
            select(NftOrder, NftItem, BotUser)
            .join(NftItem, NftItem.id == NftOrder.item_id)
            .join(BotUser, BotUser.id == NftOrder.user_id)
            .where(NftOrder.bot_id == bot_id)
            .order_by(NftOrder.id.desc()).limit(200))).all()
    out = []
    for o, it, bu in rows:
        out.append({
            "id": o.id, "kind": o.kind, "days": o.days,
            "title": it.title, "ref": it.ref or "",
            "amount_fa": _rial(o.amount), "status": o.status,
            "status_fa": _ORDER_STATUS_FA.get(o.status, o.status),
            "transfer_mode": o.transfer_mode,
            "note": o.transfer_note or "",
            "buyer": (bu.full_name or str(bu.telegram_id)),
            "buyer_id": bu.telegram_id,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
        })
    return JSONResponse({"ok": True, "orders": out})


@nft_market_router.post("/api/miniapp2/{bot_id}/admin/order/{order_id}/{action}")
async def api_admin_order_action(bot_id: int, order_id: int, action: str,
                                 x_init_data: str | None = Header(default=None),
                                 x_kb_token: str | None = Header(default=None)):
    """اقداماتِ ادمین رویِ یک سفارش: delivered / returned / reject."""
    await _owner_only(bot_id, x_init_data, x_kb_token)
    if action not in ("delivered", "returned", "reject"):
        raise HTTPException(status_code=400, detail="bad_action")
    ok, msg = await apply_order_action(bot_id, order_id, action)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return JSONResponse({"ok": True})


# ───────────────────────── منطقِ مشترکِ سفارش ─────────────────────────

async def apply_order_action(bot_id: int, order_id: int, action: str) -> tuple[bool, str]:
    """همان اقدامات، هم از مینی‌اپ و هم از دکمه‌هایِ اینلاینِ ربات صدا زده
    می‌شود — تا منطق دو نسخه نشود."""
    async with Session() as s:
        # FOR UPDATE: دو کلیکِ هم‌زمانِ «رد» (مینی‌اپ + دکمهٔ ربات) قبلاً هر دو
        # status را pending می‌دیدند و مبلغ دوبار به کیف پول برمی‌گشت.
        o = await s.get(NftOrder, order_id, with_for_update=True)
        if not o or o.bot_id != bot_id:
            return False, "not_found"
        it = await s.get(NftItem, o.item_id)
        user = await s.get(BotUser, o.user_id, with_for_update=True)

        if action == "delivered":
            if o.status not in ("pending", "paid"):
                return False, "bad_status"
            o.status = "delivered"
            o.delivered_at = _dt.datetime.utcnow()
            if it:
                it.status = "sold" if o.kind == "buy" else "rented"
                it.holder_user_id = o.user_id
                it.rent_until = o.expires_at if o.kind == "rent" else None
        elif action == "returned":
            if o.kind != "rent":
                return False, "not_rent"
            o.status = "returned"
            if it:
                it.status = "available"
                it.holder_user_id = None
                it.rent_until = None
        elif action == "reject":
            if o.status in ("rejected", "returned"):
                return False, "bad_status"
            # عودتِ کاملِ وجه به کیف‌پول
            if user:
                user.balance = int(float(user.balance or 0)) + int(float(o.amount or 0))
                s.add(WalletTxn(bot_id=bot_id, user_id=user.id,
                                amount=int(float(o.amount or 0)), kind="refund",
                                note=f"عودتِ سفارشِ NFT #{o.id}"))
            o.status = "rejected"
            if it and it.status in ("reserved", "sold", "rented"):
                it.status = "available"
                it.holder_user_id = None
                it.rent_until = None
        await s.commit()
    await _notify_user(bot_id, order_id, action)
    return True, "ok"


async def _deliver_or_notify(bot_id: int, order_id: int) -> None:
    """اول انتقالِ خودکار را امتحان می‌کند؛ اگر نشد، ادمین را خبر می‌کند.

    هرگز استثنا به بیرون نمی‌دهد: سفارش از قبل ثبت و پول کسر شده، پس یک
    خطایِ شبکه‌ای نباید پاسخِ مینی‌اپ را ۵۰۰ کند.
    """
    await _report_purchase(bot_id, order_id)
    try:
        from tenant.bots.virtual_service.nft_admin import try_auto_transfer, notify_admin_new_order
        done = await try_auto_transfer(bot_id, order_id)
        if not done:
            await notify_admin_new_order(bot_id, order_id)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("تحویلِ سفارشِ NFT #%s ناموفق بود (%s): %s",
                       order_id, type(e).__name__, e)


async def _report_purchase(bot_id: int, order_id: int) -> None:
    """گزارشِ خرید به «اطلاعِ خودکارِ خرید» و «کانالِ اعتمادسازی».

    هر مسیرِ فروشِ این پروژه باید از _purchase_alert رد شود، وگرنه این دو
    قابلیت برایِ آن مسیر خاموش می‌مانند (همان چیزی که scripts/audit.py
    چک می‌کند).
    """
    try:
        from aiogram import Bot as _AioBot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from tenant import _purchase_alert

        async with Session() as s:
            o = await s.get(NftOrder, order_id)
            if not o or o.bot_id != bot_id:
                return
            it = await s.get(NftItem, o.item_id)
            buyer = await s.get(BotUser, o.user_id)
            row = await s.get(BotRow, bot_id)
            if not row:
                return
            token = row.token
            detail = (it.title if it else "آیتمِ مارکت")
            if it and it.ref:
                detail += f" #{it.ref}"
            if o.kind == "rent":
                detail += f" — اجاره {o.days} روز"
            amount = int(float(o.amount or 0))

        tbot = _AioBot(token=token,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await _purchase_alert(tbot, row, buyer=buyer,
                                  section="مارکتِ NFT",
                                  detail=detail, amount=amount,
                                  order_id=order_id, count=1)
        finally:
            try:
                await tbot.session.close()
            except Exception:                                # noqa: BLE001
                pass
    except Exception as e:                                   # noqa: BLE001
        logger.warning("گزارشِ خریدِ NFT #%s ارسال نشد (%s): %s",
                       order_id, type(e).__name__, e)


async def _notify_user(bot_id: int, order_id: int, action: str) -> None:
    try:
        from tenant.bots.virtual_service.nft_admin import notify_user_order
        await notify_user_order(bot_id, order_id, action)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("اطلاع‌رسانیِ سفارشِ NFT #%s ناموفق بود (%s): %s",
                       order_id, type(e).__name__, e)


# ───────────────────────── HTML ─────────────────────────

_DISABLED_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>غیرفعال</title></head><body style="background:#000;color:#f2f2f4;
font-family:Tahoma,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;text-align:center"><div>مارکتِ این ربات فعال نیست.</div></body></html>"""


_PAGE_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl"><head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover"/>
<title>مارکت NFT</title>
<script src="/miniapp/_assets/tgwebapp.js"></script><script>window.Telegram||document.write('<scr'+'ipt src="https://telegram.org/js/telegram-web-app.js"></scr'+'ipt>')</script>
<style>
:root{
  --bg:#07070a; --surface:rgba(23,23,30,.82); --solid:#15151c; --surface-2:#1b1b24;
  --line:rgba(255,255,255,.08); --line-2:rgba(255,255,255,.14);
  --txt:#f5f5f8; --dim:#9292a2; --faint:#61616f;
  --gold:#f5b62c; --gold-bg:rgba(245,182,44,.13);
  --ton:#3fa2ff; --ton-bg:rgba(63,162,255,.13);
  --ok:#28d07b; --no:#f2545b;
  --p1:#6d5cf6; --p2:#c04bf0; --p3:#3fa2ff;
  --r:19px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{overscroll-behavior-y:none}
body{margin:0;background:var(--bg);color:var(--txt);
  font-family:Tahoma,system-ui,-apple-system,sans-serif;
  padding-bottom:calc(92px + env(safe-area-inset-bottom));
  -webkit-font-smoothing:antialiased;position:relative;min-height:100vh}
/* هالهٔ رنگیِ ثابتِ پس‌زمینه */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(760px 380px at 82% -8%, rgba(109,92,246,.28), transparent 64%),
    radial-gradient(620px 320px at 8% 4%, rgba(192,75,240,.20), transparent 62%),
    radial-gradient(700px 420px at 50% 104%, rgba(63,162,255,.14), transparent 66%)}

/* ── splash ── */
#splash{position:fixed;inset:0;z-index:200;background:var(--bg);
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;
  transition:opacity .5s ease, visibility .5s}
#splash.gone{opacity:0;visibility:hidden}
.orb{position:absolute;border-radius:50%;filter:blur(46px);opacity:.55;animation:drift 7s ease-in-out infinite}
.orb.a{width:200px;height:200px;background:var(--p1);top:20%;inset-inline-start:6%}
.orb.b{width:160px;height:160px;background:var(--p2);bottom:22%;inset-inline-end:8%;animation-delay:-3.5s}
.orb.c{width:130px;height:130px;background:var(--p3);top:52%;inset-inline-start:52%;animation-delay:-1.8s}
@keyframes drift{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(18px,-24px) scale(1.16)}}
.slogo{width:98px;height:98px;border-radius:28px;display:flex;align-items:center;justify-content:center;
  font-size:46px;z-index:1;background:linear-gradient(145deg,var(--p1),var(--p2));
  box-shadow:0 18px 56px rgba(109,92,246,.5);
  animation:pop .9s cubic-bezier(.2,1.3,.4,1) both, float 3.4s ease-in-out 1s infinite}
@keyframes pop{0%{transform:scale(.3) rotate(-18deg);opacity:0}100%{transform:scale(1) rotate(0);opacity:1}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-11px)}}
.stitle{font-size:25px;font-weight:800;letter-spacing:.6px;z-index:1;
  background:linear-gradient(90deg,#fff,#a9c4ff,#fff);background-size:200% auto;
  -webkit-background-clip:text;background-clip:text;color:transparent;
  animation:shine 2.6s linear infinite, up .5s .2s both}
@keyframes shine{to{background-position:200% center}}
.sbar{width:132px;height:3px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden;z-index:1}
.sbar i{display:block;height:100%;width:40%;border-radius:99px;
  background:linear-gradient(90deg,transparent,var(--p3),transparent);animation:load 1.1s ease-in-out infinite}
@keyframes load{0%{transform:translateX(-120%)}100%{transform:translateX(320%)}}

@keyframes up{from{opacity:0;transform:translateY(15px)}to{opacity:1;transform:none}}
.rv{animation:up .46s cubic-bezier(.2,.8,.3,1) both}

/* ── top ── */
.top{position:sticky;top:0;z-index:30;padding:10px 12px;display:flex;align-items:center;gap:8px;
  background:linear-gradient(to bottom,rgba(7,7,10,.96) 62%,rgba(7,7,10,0));backdrop-filter:blur(16px)}
.ic{width:39px;height:39px;border-radius:50%;background:var(--surface);border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;font-size:16px;cursor:pointer;
  transition:transform .15s;position:relative;flex:none;backdrop-filter:blur(12px)}
.ic:active{transform:scale(.9)}
.dot{position:absolute;top:-3px;inset-inline-end:-3px;min-width:18px;height:18px;border-radius:99px;
  background:linear-gradient(135deg,var(--p2),var(--no));color:#fff;font-size:10px;font-weight:800;
  display:flex;align-items:center;justify-content:center;padding:0 4px;border:2px solid var(--bg)}
.wal{margin-inline-start:auto;display:flex;align-items:center;gap:8px;
  background:linear-gradient(135deg,rgba(109,92,246,.24),rgba(192,75,240,.18));
  border:1px solid rgba(255,255,255,.14);border-radius:99px;padding:7px 8px 7px 14px;
  font-size:13px;font-weight:800;cursor:pointer;transition:transform .15s;backdrop-filter:blur(12px)}
.wal:active{transform:scale(.96)}
.plus{width:25px;height:25px;border-radius:50%;background:var(--txt);color:#0b0b12;
  display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:700;line-height:1}

/* ── hero ── */
.hero-h{margin:2px 12px 12px;padding:15px 16px;border-radius:20px;position:relative;overflow:hidden;
  background:linear-gradient(135deg,rgba(109,92,246,.30),rgba(192,75,240,.20) 52%,rgba(63,162,255,.16));
  border:1px solid var(--line-2)}
.hero-h::after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;
  background:rgba(255,255,255,.09);top:-72px;inset-inline-end:-42px}
.hero-h h1{margin:0 0 5px;font-size:17px;font-weight:800;position:relative}
.hero-h p{margin:0;font-size:11.5px;color:rgba(255,255,255,.66);position:relative;line-height:1.8}
.hstat{display:flex;gap:16px;margin-top:11px;position:relative}
.hstat div{font-size:11px;color:rgba(255,255,255,.62)}
.hstat b{display:block;font-size:15px;color:#fff;font-weight:800;margin-bottom:1px}

/* ── segmented ── */
.seg{display:flex;gap:4px;margin:0 12px 11px;background:var(--surface);border:1px solid var(--line);
  border-radius:16px;padding:4px;backdrop-filter:blur(12px)}
.seg div{flex:1;text-align:center;padding:11px;border-radius:12px;font-size:13.5px;font-weight:800;
  cursor:pointer;color:var(--dim);transition:all .24s cubic-bezier(.3,1,.4,1)}
.seg div.on{background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;
  box-shadow:0 5px 20px rgba(109,92,246,.42)}

/* ── chips ── */
.chips{display:flex;gap:7px;overflow-x:auto;padding:2px 12px 12px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;padding:8px 15px;border-radius:99px;background:var(--surface);
  border:1px solid var(--line);font-size:12.5px;cursor:pointer;color:var(--dim);
  transition:all .2s;display:flex;align-items:center;gap:6px;flex:none;backdrop-filter:blur(10px)}
.chip:active{transform:scale(.94)}
.chip.on{background:var(--txt);color:#0b0b12;border-color:var(--txt);font-weight:800}
.sw{width:11px;height:11px;border-radius:50%;border:1px solid rgba(255,255,255,.3);flex:none}

/* ── grid ── */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:11px;padding:0 12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  overflow:hidden;display:flex;flex-direction:column;backdrop-filter:blur(12px);
  transition:transform .18s, border-color .18s, box-shadow .18s}
.card:active{transform:scale(.978);border-color:var(--line-2);box-shadow:0 8px 28px rgba(0,0,0,.45)}
.thumb{aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:54px;
  position:relative;overflow:hidden}
.thumb::before{content:"";position:absolute;inset:0;opacity:.30;
  background-image:radial-gradient(rgba(255,255,255,.55) 1px,transparent 1px);
  background-size:15px 15px}
.thumb::after{content:"";position:absolute;inset:0;
  background:radial-gradient(circle at 50% 122%,rgba(255,255,255,.26),transparent 60%)}
.thumb img{width:100%;height:100%;object-fit:cover;position:relative;z-index:1}
.thumb .em{position:relative;z-index:1;filter:drop-shadow(0 6px 16px rgba(0,0,0,.45))}
.thumb .uname{font-size:17px;font-weight:800;color:#fff;z-index:1;direction:ltr;
  text-shadow:0 2px 16px rgba(0,0,0,.55);padding:0 8px;text-align:center;word-break:break-all}
.tag{position:absolute;top:8px;inset-inline-start:8px;z-index:2;background:rgba(0,0,0,.42);
  backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.16);border-radius:8px;
  padding:3px 8px;font-size:9.5px;font-weight:800;color:#fff}
.cbody{padding:10px;display:flex;flex-direction:column;flex:1}
.cname{font-size:12.5px;font-weight:800;margin-bottom:7px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;direction:ltr;text-align:right}
.pr{display:flex;align-items:center;justify-content:center;gap:4px;border-radius:9px;
  padding:4.5px 8px;font-size:11px;font-weight:800;margin-bottom:4px;direction:ltr}
.pr.t{background:var(--gold-bg);color:var(--gold);border:1px solid rgba(245,182,44,.2)}
.pr.n{background:var(--ton-bg);color:var(--ton);border:1px solid rgba(63,162,255,.2)}
.attrs{margin-top:6px;padding-top:6px;border-top:1px solid var(--line)}
.attr{display:flex;justify-content:space-between;font-size:10.5px;color:var(--faint);padding:1.5px 0}
.attr b{color:var(--dim);font-weight:700;direction:ltr}
.btns{display:flex;gap:5px;margin-top:9px}
.btn{flex:1;padding:9.5px 4px;border-radius:11px;font-size:11.5px;font-weight:800;text-align:center;
  cursor:pointer;border:1px solid var(--line);background:var(--surface-2);color:var(--dim);
  transition:transform .15s}
.btn:active{transform:scale(.94)}
.btn.pri{background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;border-color:transparent;
  box-shadow:0 4px 16px rgba(109,92,246,.38)}

/* ── skeleton ── */
.sk{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.sk .b{background:linear-gradient(90deg,rgba(255,255,255,.04) 25%,rgba(255,255,255,.10) 50%,rgba(255,255,255,.04) 75%);
  background-size:200% 100%;animation:sh 1.3s infinite}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
.sk .t{aspect-ratio:1}
.sk .l{height:11px;border-radius:5px;margin:9px 10px}
.sk .l.s{width:55%}

.empty{text-align:center;color:var(--faint);padding:56px 24px;font-size:13.5px;line-height:2.1}
.empty .e{font-size:52px;display:block;margin-bottom:14px;opacity:.55;
  filter:drop-shadow(0 8px 22px rgba(109,92,246,.35))}

/* ── sheet ── */
.sheet{position:fixed;inset:0;background:rgba(0,0,0,.66);display:flex;align-items:flex-end;z-index:100;
  opacity:0;visibility:hidden;transition:opacity .28s;backdrop-filter:blur(5px)}
.sheet.on{opacity:1;visibility:visible}
.sbox{background:linear-gradient(180deg,#1c1c26,var(--solid) 30%);width:100%;
  border-radius:26px 26px 0 0;padding:8px 16px 20px;max-height:92vh;overflow-y:auto;
  transform:translateY(100%);transition:transform .34s cubic-bezier(.2,.9,.25,1);
  border-top:1px solid var(--line-2);padding-bottom:calc(20px + env(safe-area-inset-bottom))}
.sheet.on .sbox{transform:none}
.grab{width:40px;height:4px;border-radius:99px;background:rgba(255,255,255,.16);margin:0 auto 14px}
.shead{display:flex;align-items:center;margin-bottom:14px}
.shead b{font-size:15.5px}
.x{margin-inline-start:auto;width:32px;height:32px;border-radius:50%;background:var(--surface-2);
  display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:15px;color:var(--dim)}
.row{display:flex;justify-content:space-between;align-items:center;padding:11px 0;
  border-bottom:1px solid var(--line);font-size:13px}
.row span{color:var(--dim)}
.row b{direction:ltr}
.hero{width:112px;height:112px;border-radius:24px;margin:16px auto;display:flex;align-items:center;
  justify-content:center;font-size:52px;overflow:hidden;position:relative;
  box-shadow:0 14px 40px rgba(0,0,0,.5)}
.hero::before{content:"";position:absolute;inset:0;opacity:.28;
  background-image:radial-gradient(rgba(255,255,255,.55) 1px,transparent 1px);background-size:15px 15px}
.hero img{width:100%;height:100%;object-fit:cover;position:relative;z-index:1}
.hero .em,.hero .uname{position:relative;z-index:1}
.stepper{display:flex;align-items:center;gap:11px;margin:12px 0}
.stepper button{width:47px;height:47px;border-radius:50%;background:var(--surface-2);
  border:1px solid var(--line);color:var(--txt);font-size:21px;cursor:pointer;transition:transform .14s}
.stepper button:active{transform:scale(.88)}
.stepper input{flex:1;height:47px;border-radius:14px;background:var(--surface-2);
  border:1px solid var(--line);color:var(--txt);text-align:center;font-size:17px;font-weight:800;
  font-family:inherit;direction:ltr}
.total{background:linear-gradient(135deg,rgba(109,92,246,.20),rgba(192,75,240,.13));
  border:1px solid var(--line-2);border-radius:15px;padding:14px;margin:12px 0;
  font-size:13.5px;line-height:2}
.cta{width:100%;padding:15px;border-radius:15px;border:none;font-size:14.5px;font-weight:800;
  cursor:pointer;font-family:inherit;transition:transform .15s;color:#fff;
  background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 8px 26px rgba(109,92,246,.42)}
.cta:active{transform:scale(.975)}
.cta:disabled{opacity:.45}
.note{font-size:11.5px;color:var(--faint);background:var(--surface-2);border-radius:12px;
  padding:10px;margin-top:10px;line-height:1.95}
input.f,select.f{width:100%;height:46px;border-radius:14px;background:var(--surface-2);
  border:1px solid var(--line);color:var(--txt);padding:0 12px;font-size:13px;
  font-family:inherit;margin-bottom:9px}
input.f:focus,select.f:focus{outline:none;border-color:var(--p1)}
label.l{font-size:11.5px;color:var(--dim);display:block;margin:7px 0 5px}

/* ── nav ── */
.nav{position:fixed;bottom:0;left:0;right:0;z-index:50;display:flex;
  background:rgba(10,10,14,.90);backdrop-filter:blur(22px);border-top:1px solid var(--line);
  padding:8px 0 calc(10px + env(safe-area-inset-bottom))}
.nav div{flex:1;text-align:center;font-size:10.5px;color:var(--faint);cursor:pointer;
  padding:3px 0;transition:color .2s;position:relative}
.nav div.on{color:var(--txt);font-weight:800}
.nav div.on::before{content:"";position:absolute;top:-8px;left:50%;transform:translateX(-50%);
  width:26px;height:3px;border-radius:99px;background:linear-gradient(90deg,var(--p1),var(--p2))}
.nav i{display:block;font-size:20px;font-style:normal;margin-bottom:3px;transition:transform .25s}
.nav div.on i{transform:translateY(-2px) scale(1.14)}

.ord{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:13px;
  margin-bottom:9px;backdrop-filter:blur(12px)}
.badge{display:inline-block;border-radius:8px;padding:3px 9px;font-size:10.5px;font-weight:800}
.b-wait{background:var(--gold-bg);color:var(--gold)}
.b-ok{background:rgba(40,208,123,.14);color:var(--ok)}
.b-no{background:rgba(242,84,91,.14);color:var(--no)}
.adm{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:11px;
  margin-bottom:8px;display:flex;align-items:center;gap:10px;backdrop-filter:blur(12px)}
.adm .t{flex:1;font-size:12.5px;font-weight:800;min-width:0}
.mini{padding:8px 12px;border-radius:11px;font-size:11px;font-weight:800;cursor:pointer;
  background:var(--surface-2);border:1px solid var(--line);flex:none;transition:transform .15s}
.mini:active{transform:scale(.93)}
h3.sec{font-size:13.5px;margin:14px 12px 10px}
.pad{padding:0 12px}
</style></head><body>

<div id="splash">
  <div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
  <div class="slogo">🖼</div>
  <div class="stitle">NFT Market</div>
  <div class="sbar"><i></i></div>
</div>

<div class="top">
  <div class="ic" id="cartIc" onclick="go('my')">🧾</div>
  <div class="ic" onclick="refresh()">⟳</div>
  <div class="wal" onclick="openBot()">
    <span id="bal">…</span>
    <div class="plus">+</div>
  </div>
</div>

<div id="view"></div>

<div class="nav">
  <div id="nv-market" class="on" onclick="go('market')"><i>🛍</i>مارکت</div>
  <div id="nv-my" onclick="go('my')"><i>📦</i>سفارش‌ها</div>
  <div id="nv-me" onclick="go('me')" style="display:none"><i>👤</i>من</div>
  <div id="nv-admin" onclick="go('admin')" style="display:none"><i>⚙️</i>مدیریت</div>
</div>

<div class="sheet" id="sheet"><div class="sbox" id="sbox"></div></div>

<script>
const BOT_ID="__BOT_ID__", BOT_LINK="__BOT_LINK__";
const tg=window.Telegram?.WebApp;
try{ tg?.ready(); tg?.expand(); tg?.setHeaderColor?.("#08080a"); tg?.setBackgroundColor?.("#08080a"); }catch(e){}
function hap(t){ try{ tg?.HapticFeedback?.impactOccurred(t||"light"); }catch(e){} }
function qp(n){ try{ return new URLSearchParams(location.search).get(n)||""; }catch(e){ return ""; } }
const HDRS={"Content-Type":"application/json","X-Init-Data":(tg?.initData||""),"X-Kb-Token":qp("t")};

let ST={tab:"rent",kind:"",collection:"",color:"",sort:"new",
        fee:0,tonRate:0,balance:0,isOwner:false,items:[],page:"market",adminTab:"items",
        collections:[],colors:[],src:"shop",p2p:{enabled:false},me:null,meTab:"deals"};

/* === PATCH_MINIAPP_RETRY_V1 ===
   گزارشِ MD04: «خطا در اتصال / خطا در دریافتِ لیست» روی اینترنتِ موبایلِ ایران (فیلتر خاموش). اتصال‌هایِ ناپایدار یا یک
   ۵۰۰ گذرا کلِ صفحه را می‌ترکاند. حالا درخواست‌هایِ خواندنی (GET) تا ۳ بار با مکثِ کوتاه تکرار می‌شوند؛
   خطایِ نهایی هم کد/علتش را نشان می‌دهد و دکمهٔ «تلاش دوباره» دارد. POSTها (ثبتِ سفارش) هرگز تکرار نمی‌شوند. */
const _sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function api(p,o){
  const isGet=!(o&&o.method&&String(o.method).toUpperCase()!=="GET");
  const tries=isGet?3:1; let last;
  for(let i=0;i<tries;i++){
    try{
      const r=await fetch("/api/miniapp2/"+BOT_ID+p,Object.assign({headers:HDRS},o||{}));
      let j=null; try{ j=await r.json(); }catch(e){}
      if(!r.ok){
        const e=new Error((j&&j.detail)||r.status); e.data=j; e.code=r.status;
        if(isGet&&(r.status>=500||r.status===429)&&i<tries-1){ last=e; await _sleep(500*(i+1)); continue; }
        throw e;
      }
      return j;
    }catch(e){
      if(e&&e.code&&e.code<500&&e.code!==429) throw e;
      last=e; if(i<tries-1) await _sleep(500*(i+1));
    }
  }
  throw last;
}
function errBox(e,retryFn,msg){
  const code=(e&&e.code)?("HTTP "+e.code):"شبکه";
  window.__retry=retryFn;
  return '<div class="empty"><span class="e">⚠️</span>'+msg+'<br/><small style="opacity:.6;direction:ltr;display:inline-block;margin-top:6px">'+code+'</small>'+
         '<br/><div class="btn pri" style="display:inline-block;margin-top:10px;padding:8px 18px" onclick="window.__retry()">🔄 تلاش دوباره</div></div>';
}
function fa(n){ try{ return Number(n||0).toLocaleString("en-US"); }catch(e){ return n; } }
function ton(t){ if(!ST.tonRate||!t) return ""; const v=t/ST.tonRate;
  return v<0.001?v.toFixed(4):v.toFixed(3); }
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,c=>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function sheet(h){ document.getElementById("sbox").innerHTML=h;
  document.getElementById("sheet").classList.add("on"); hap(); }
function closeSheet(){ document.getElementById("sheet").classList.remove("on"); }
document.getElementById("sheet").onclick=e=>{ if(e.target.id==="sheet") closeSheet(); };
function toast(m){ try{ tg?.showAlert(m); }catch(e){ alert(m); } }
function openBot(){ try{ tg?.openTelegramLink(BOT_LINK); }catch(e){ location.href=BOT_LINK; } }

/* رنگِ پس‌زمینهٔ کارت از رویِ نامِ آیتم — پایدار و بدونِ نیاز به تصویر */
const PALETTE=[["#20c5a8","#0d7a86"],["#f0517a","#8c1f4a"],["#4d8cf5","#1f3f8c"],
  ["#f5a524","#8c5a15"],["#9d5cf6","#4a1f8c"],["#2fb6e8","#14567a"],
  ["#f06ab0","#8c2160"],["#3fd07b","#146b3f"],["#7c6bf6","#33248c"],["#f07a3f","#8c3f15"]];
function pal(s){ const k=String(s||"x"); let h=0;
  for(let i=0;i<k.length;i++) h=(h*31+k.charCodeAt(i))|0;
  return PALETTE[Math.abs(h)%PALETTE.length]; }
function hue(s){ return pal(s)[0]; }
function grad(s){ const p=pal(s);
  return `linear-gradient(145deg,${p[0]},${p[1]})`; }

async function boot(){
  try{
    const b=await api("/bootstrap");
    ST.fee=b.fee; ST.tonRate=b.ton_rate||0; ST.balance=b.balance; ST.isOwner=b.is_owner;
    ST.collections=b.collections||[]; ST.colors=b.colors||[];
    ST.theme=b.theme||{accent:"",bg:"",card:""};   /* PATCH_NFT_THEME_V1 */
    document.getElementById("bal").textContent="💰 "+b.balance_fa;
    if(b.is_owner) document.getElementById("nv-admin").style.display="block";
    if(b.my_orders) document.getElementById("cartIc").innerHTML='🧾<div class="dot">'+b.my_orders+'</div>';
    await p2pBoot();   /* PATCH_P2P_MARKET_V1 */
    if(ST.page==="me"&&!ST.p2p.enabled) ST.page="market";
    go(ST.page);
  }catch(e){
    document.getElementById("view").innerHTML=(e.code===403)
      ? '<div class="empty"><span class="e">🔒</span>این بخش برایِ این ربات فعال نیست.</div>'
      : errBox(e,function(){ location.reload(); },"خطا در اتصال.");
  }finally{
    setTimeout(()=>document.getElementById("splash").classList.add("gone"),420);
  }
}
function refresh(){ hap("medium"); boot(); }

function go(p){
  ST.page=p;
  ["market","my","me","admin"].forEach(k=>{
    const el=document.getElementById("nv-"+k); if(el) el.classList.toggle("on",k===p); });
  hap();
  if(p==="market") renderMarket(); else if(p==="my") renderMy(); else if(p==="me") renderMe(); else renderAdmin();
}

function skel(n){ let h='<div class="grid">';
  for(let i=0;i<(n||4);i++) h+='<div class="sk rv" style="animation-delay:'+(i*60)+
    'ms"><div class="b t"></div><div class="b l"></div><div class="b l s"></div></div>';
  return h+'</div>'; }

/* ───── مارکت ───── */
function renderMarket(){
  const n=ST.items.length;
  const hero=`<div class="hero-h rv">
      <h1>🖼 مارکتِ NFT</h1>
      <p>گیفت‌هایِ NFT و یوزرنیم‌هایِ تلگرام — بخر یا روزانه اجاره کن.</p>
      <div class="hstat">
        <div><b>${ST.tab==="rent"?"اجاره":"خرید"}</b>حالتِ فعلی</div>
        <div><b>${fa(ST.balance)}</b>موجودیِ تو (ت)</div>
      </div></div>`;
  const cc=ST.collections.length?`<div class="chip ${ST.collection?"on":""}" onclick="pickCol()">🗂 ${ST.collection?esc(ST.collection):"کالکشن"}</div>`:"";
  const ck=ST.colors.length?`<div class="chip ${ST.color?"on":""}" onclick="pickColor()">🎨 ${ST.color?esc(ST.color):"رنگ"}</div>`:"";
  document.getElementById("view").innerHTML=hero+srcSwitchHtml()+`
    <div class="seg rv" style="animation-delay:40ms">
      <div class="${ST.tab==="rent"?"on":""}" onclick="setTab('rent')">🔑 اجاره</div>
      <div class="${ST.tab==="buy"?"on":""}" onclick="setTab('buy')">🛒 خرید</div>
    </div>
    <div class="chips rv" style="animation-delay:90ms">
      <div class="chip ${ST.sort==="new"?"on":""}" onclick="setF('sort','new')">✨ جدیدترین</div>
      <div class="chip ${ST.sort==="cheap"?"on":""}" onclick="setF('sort','cheap')">↓ ارزان‌ترین</div>
      <div class="chip ${ST.sort==="expensive"?"on":""}" onclick="setF('sort','expensive')">↑ گران‌ترین</div>
      <div class="chip ${ST.kind==="gift"?"on":""}" onclick="setF('kind',ST.kind==='gift'?'':'gift')">🎁 گیفت</div>
      <div class="chip ${ST.kind==="username"?"on":""}" onclick="setF('kind',ST.kind==='username'?'':'username')">@ یوزرنیم</div>
      ${cc}${ck}
    </div>
    <div id="grid">${skel(4)}</div>`;
  loadItems();
}
function setTab(t){ hap(); ST.tab=t; renderMarket(); }
function setF(k,v){ hap(); ST[k]=v; renderMarket(); }
function pickCol(){
  sheet(`<div class="grab"></div><div class="shead"><b>🗂 انتخابِ کالکشن</b><div class="x" onclick="closeSheet()">✕</div></div>
    <div class="chips" style="flex-wrap:wrap;overflow:visible;padding:0">
      <div class="chip ${!ST.collection?"on":""}" onclick="setSheetF('collection','')">همه</div>
      ${ST.collections.map(c=>`<div class="chip ${ST.collection===c?"on":""}" onclick="setSheetF('collection','${esc(c)}')">${esc(c)}</div>`).join("")}
    </div>`);
}
function pickColor(){
  sheet(`<div class="grab"></div><div class="shead"><b>🎨 انتخابِ رنگ</b><div class="x" onclick="closeSheet()">✕</div></div>
    <div class="chips" style="flex-wrap:wrap;overflow:visible;padding:0">
      <div class="chip ${!ST.color?"on":""}" onclick="setSheetF('color','')">همه</div>
      ${ST.colors.map(c=>`<div class="chip ${ST.color===c?"on":""}" onclick="setSheetF('color','${esc(c)}')">
        <span class="sw" style="background:${hue(c)}"></span>${esc(c)}</div>`).join("")}
    </div>`);
}
function setSheetF(k,v){ ST[k]=v; closeSheet(); renderMarket(); }

async function loadItems(){
  const g=document.getElementById("grid");
  try{
    const q=new URLSearchParams({tab:ST.tab,kind:ST.kind,collection:ST.collection,
                                color:ST.color,sort:ST.sort});
    const users=ST.src==="users"&&ST.p2p.enabled;
    const r=await api((users?"/p2p/items?":"/items?")+q.toString());
    ST.items=r.items;
    if(!r.items.length){ g.innerHTML='<div class="empty"><span class="e">🗃</span>'+(users?"فعلاً آگهیِ کاربری در این بخش نیست.<br/>اولین نفر باش: از تبِ «👤 من» آگهی بگذار.":"فعلاً آیتمی در این بخش نیست.")+'</div>'; return; }
    g.className="grid";
    g.innerHTML=r.items.map((it,ix)=>{
      const price=ST.tab==="buy"?it.sale_price:it.rent_daily;
      const suf=ST.tab==="buy"?"":" / روز";
      const tn=ton(price);
      const bg=grad(it.color||it.collection||it.title);
      const inner=it.image?'<img referrerpolicy="no-referrer" onerror="this.remove()" src="'+esc(it.image)+'" loading="lazy"/>'
        :(it.kind==="username"?'<div class="uname">@'+esc(it.ref||it.title)+'</div>'
                              :'<span class="em">🎁</span>');
      const tag=it.p2p?`<div class="tag ptag">🤝 کاربر</div>`
                      :(it.collection?`<div class="tag">${esc(it.collection)}</div>`
                             :(it.kind==="username"?'<div class="tag">USERNAME</div>':""));
      return `<div class="card rv" style="animation-delay:${Math.min(ix,10)*55}ms">
        <div class="thumb" style="background:${bg}">${tag}${inner}</div>
        <div class="cbody">
          <div class="cname">${esc(it.title)}${it.ref&&it.kind!=="username"&&!it.p2p?" #"+esc(it.ref):""}</div>
          ${it.p2p?`<div class="seller">👤 <b>${esc(it.seller)}</b>${it.mine?" · آگهیِ تو":""}</div>`:""}
          <div class="pr t">${fa(price)} ت${suf}</div>
          ${tn?`<div class="pr n">TON ${tn}${suf}</div>`:""}
          ${(it.model||it.color||it.symbol)?`<div class="attrs">
            ${it.model?`<div class="attr"><span>مدل</span><b>${esc(it.model)}</b></div>`:""}
            ${it.color?`<div class="attr"><span>رنگ</span><b>${esc(it.color)}</b></div>`:""}
            ${it.symbol?`<div class="attr"><span>سمبل</span><b>${esc(it.symbol)}</b></div>`:""}
          </div>`:""}
          <div class="btns">
            <div class="btn pri ${it.mine?"off":""}" onclick="openOrder(${it.id})">${it.mine?"آگهیِ توست":ST.tab==="buy"?"خرید":"اجاره"}</div>
          </div>
        </div></div>`;
    }).join("");
  }catch(e){ g.innerHTML=errBox(e,loadItems,"خطا در دریافتِ لیست."); }
}

function heroOf(it){
  const bg=grad(it.color||it.collection||it.title);
  const inner=it.image?'<img referrerpolicy="no-referrer" onerror="this.remove()" src="'+esc(it.image)+'"/>'
    :(it.kind==="username"?'<div class="uname" style="font-size:13px;color:#fff;font-weight:800">@'
        +esc(it.ref||it.title)+'</div>':'<span class="em">🎁</span>');
  return `<div class="hero" style="background:${bg}">${inner}</div>`;
}

function openOrder(id){
  const it=ST.items.find(x=>x.id===id); if(!it||it.mine) return; hap("medium");
  const FEE=it.p2p?0:ST.fee;   /* PATCH_P2P_MARKET_V1: در بازارِ کاربران کارمزد از فروشنده گرفته می‌شود */
  if(ST.tab==="buy"){
    const tot=it.sale_price+FEE, tn=ton(it.sale_price);
    sheet(`<div class="grab"></div>
      <div class="shead"><b>🛒 خریدِ NFT</b><div class="x" onclick="closeSheet()">✕</div></div>
      ${heroOf(it)}
      <div class="row"><span>نام</span><b>${esc(it.title)}</b></div>${it.p2p?`<div class="row"><span>فروشنده</span><b>${esc(it.seller)}</b></div>`:""}
      <div class="row"><span>قیمت</span><b>${fa(it.sale_price)} ت${tn?" · TON "+tn:""}</b></div>
      ${FEE?`<div class="row"><span>کارمزدِ انتقال</span><b>${fa(FEE)} ت</b></div>`:""}
      <div class="total">مبلغ نهایی: <b>${fa(tot)} تومان</b></div>
      ${FEE?`<div class="note">💡 ${fa(FEE)} تومان کارمزدِ ثابتِ تلگرام برایِ انتقالِ NFT است.</div>`:""}${it.p2p?p2pEscrowNote()+(it.note?`<div class="note">📝 ${esc(it.note)}</div>`:""):""}
      <button class="cta" style="margin-top:10px" onclick="submitOrder(${it.id},'buy',0)">پرداخت و ثبتِ سفارش</button>`);
  } else {
    const tn=ton(it.rent_daily);
    sheet(`<div class="grab"></div>
      <div class="shead"><b>🔑 اجارهٔ NFT</b><div class="x" onclick="closeSheet()">✕</div></div>
      ${heroOf(it)}
      <div class="row"><span>نام</span><b>${esc(it.title)}</b></div>${it.p2p?`<div class="row"><span>فروشنده</span><b>${esc(it.seller)}</b></div>`:""}
      <div class="row"><span>قیمتِ روزانه</span><b>${fa(it.rent_daily)} ت${tn?" · TON "+tn:""}</b></div>
      <label class="l">مدت (روز)</label>
      <div class="stepper">
        <button onclick="stepDays(1)">+</button>
        <input id="days" type="number" value="${it.rent_min_days}" oninput="calcRent()"/>
        <button onclick="stepDays(-1)">−</button>
      </div>
      <div style="font-size:11.5px;color:var(--faint);text-align:center">
        حداقل ${it.rent_min_days} روز — حداکثر ${it.rent_max_days} روز</div>
      <div class="total" id="rtotal"></div>
      ${FEE?`<div class="note">💡 ${fa(FEE)} تومان کارمزدِ ثابتِ تلگرام برایِ انتقالِ NFT است.</div>`:""}${it.p2p?p2pEscrowNote()+(it.note?`<div class="note">📝 ${esc(it.note)}</div>`:""):""}
      <button class="cta" style="margin-top:10px" id="rcta">پرداخت و ثبتِ سفارش</button>`);
    window._rent=it; calcRent();
    document.getElementById("rcta").onclick=()=>
      submitOrder(it.id,"rent",parseInt(document.getElementById("days").value||"0",10));
  }
}
function stepDays(d){ hap();
  const it=window._rent, el=document.getElementById("days");
  let v=(parseInt(el.value||"0",10)||it.rent_min_days)+d;
  v=Math.max(it.rent_min_days,Math.min(it.rent_max_days,v)); el.value=v; calcRent();
}
function calcRent(){
  const it=window._rent; if(!it) return;
  let d=parseInt(document.getElementById("days").value||"0",10)||it.rent_min_days;
  d=Math.max(it.rent_min_days,Math.min(it.rent_max_days,d));
  const tot=it.rent_daily*d+(it.p2p?0:ST.fee);
  document.getElementById("rtotal").innerHTML=
    `مدت: <b>${d} روز</b><br/>مبلغ نهایی: <b>${fa(tot)} تومان</b>`;
}

async function submitOrder(id,kind,days){
  const btn=document.querySelector(".sbox .cta"); if(btn){ btn.disabled=true; btn.textContent="در حال ثبت…"; }
  try{
    const users=ST.src==="users"&&ST.p2p.enabled;
    await api(users?"/p2p/order":"/order",{method:"POST",body:JSON.stringify({item_id:id,kind:kind,days:days})});
    closeSheet(); hap("heavy");
    toast(users?"✅ سفارش ثبت شد و پولت امن نزدِ ربات است. بعد از انتقالِ فروشنده، از تو تأیید می‌گیریم."
               :"✅ سفارش ثبت شد. پس از تحویل به شما اطلاع داده می‌شود.");
    boot();
  }catch(e){
    if(btn){ btn.disabled=false; btn.textContent="پرداخت و ثبتِ سفارش"; }
    if(e.code===402&&e.data){
      closeSheet();
      sheet(`<div class="grab"></div>
        <div class="shead"><b>موجودی کافی نیست</b><div class="x" onclick="closeSheet()">✕</div></div>
        <div class="row"><span>مبلغِ لازم</span><b>${e.data.need_fa} ت</b></div>
        <div class="row"><span>موجودیِ شما</span><b>${e.data.balance_fa} ت</b></div>
        <div class="note">برایِ افزایشِ موجودی از طریقِ خودِ ربات اقدام کن.</div>
        <button class="cta" style="margin-top:10px" onclick="openBot()">افزایشِ موجودی</button>`);
    } else if(e.code===409){ toast((e.data&&e.data.message)||"این آیتم دیگر موجود نیست."); closeSheet(); loadItems(); }
    else toast("خطا در ثبتِ سفارش.");
  }
}

/* ───── سفارش‌های من ───── */
async function renderMy(){
  const v=document.getElementById("view");
  v.innerHTML='<h3 class="sec">📦 سفارش‌های من</h3>'+skel(2);
  try{
    const r=await api("/my");
    if(!r.orders.length){ v.innerHTML='<div class="empty"><span class="e">📭</span>هنوز سفارشی ثبت نکردی.</div>'; return; }
    v.innerHTML='<h3 class="sec">📦 سفارش‌های من</h3><div class="pad">'+r.orders.map((o,i)=>{
      const cls=o.status==="delivered"?"b-ok":(["rejected","expired"].includes(o.status)?"b-no":"b-wait");
      return `<div class="ord rv" style="animation-delay:${Math.min(i,8)*55}ms">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:7px">
          <b style="font-size:13px">${esc(o.title)}</b>
          <span class="badge ${cls}" style="margin-inline-start:auto">${esc(o.status_fa)}</span></div>
        <div class="attr"><span>${o.kind==="buy"?"خرید":"اجاره ("+o.days+" روز)"}</span><b>${o.amount_fa} ت</b></div>
        ${o.expires_at?`<div class="attr"><span>پایانِ اجاره</span><b>${o.expires_at.slice(0,10)}</b></div>`:""}
      </div>`; }).join("")+'</div>';
  }catch(e){ v.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا در دریافتِ سفارش‌ها.</div>'; }
}

/* ───── مدیریت ───── */
function renderAdmin(){
  document.getElementById("view").innerHTML=`
    <div class="seg rv">
      <div class="${ST.adminTab==="items"?"on":""}" onclick="setAdm('items')">🖼 آیتم‌ها</div>
      <div class="${ST.adminTab==="orders"?"on":""}" onclick="setAdm('orders')">📋 سفارش‌ها</div>
      <div class="${ST.adminTab==="src"?"on":""}" onclick="setAdm('src')">🔌 منابع</div>
      <div class="${ST.adminTab==="p2p"?"on":""}" onclick="setAdm('p2p')">🤝 کاربران</div>
      <div class="${ST.adminTab==="theme"?"on":""}" onclick="setAdm('theme')">🎨 ظاهر</div>
    </div><div id="ab" class="pad">`+skel(2)+`</div>`;
  if(ST.adminTab==="items") loadAdminItems();
  else if(ST.adminTab==="src") loadSources();
  else if(ST.adminTab==="p2p") loadAdminP2P();
  else if(ST.adminTab==="theme") loadAdminTheme();
  else loadAdminOrders();
}
function setAdm(t){ hap(); ST.adminTab=t; renderAdmin(); }

async function loadAdminItems(){
  const b=document.getElementById("ab");
  try{
    const r=await api("/admin/items"); ST.fee=r.fee; ST.tonRate=r.ton_rate||0;
    b.innerHTML=
      `<div class="note rv" style="margin:0 0 10px">انتقالِ خودکار: <b>${r.auto_ready?"✅ فعال":"❌ غیرفعال"}</b>
        ${r.auto_ready?"":"<br/><b>۱)</b> در @BotFather → /mybots → همین ربات → Bot Settings → <b>Secretary Mode</b> → Turn on. <i>(در بعضی نسخه‌ها Business Mode نوشته شده — هر دو یکی‌اند.)</i>"
          +"<br/><b>۲)</b> تلگرامِ خودت → Settings → Telegram Business → Chatbots → یوزرنیمِ ربات."
          +"<br/><b>۳)</b> تیکِ «Transfer and upgrade gifts» را بزن."
          +"<br/><br/>اگر تلگرام گفت «این ربات از حالتِ منشی پشتیبانی نمی‌کند»، یعنی قدمِ ۱ انجام نشده."}</div>
       <div style="display:flex;gap:7px;margin-bottom:11px">
         <button class="cta" style="flex:1;padding:12px;font-size:13px" onclick="editItem(null)">➕ آیتمِ جدید</button>
         <button class="mini" style="padding:12px 15px" onclick="editFee()">💸 نرخ‌ها</button>
       </div>`+
      (r.items.length?r.items.map((it,i)=>`
        <div class="adm rv" style="animation-delay:${Math.min(i,8)*45}ms">
          <div style="width:38px;height:38px;border-radius:11px;background:${grad(it.color||it.collection||it.title)};
               display:flex;align-items:center;justify-content:center;font-size:18px;overflow:hidden;flex:none">
            ${it.image?'<img referrerpolicy="no-referrer" onerror="this.remove()" src="'+esc(it.image)+'" style="width:100%;height:100%;object-fit:cover"/>'
              :(it.kind==="username"?"@":"🎁")}</div>
          <div class="t"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(it.title)}</div>
            <span style="color:var(--faint);font-weight:400;font-size:10.5px">
            ${it.mode==="buy"?"فروش":it.mode==="rent"?"اجاره":"فروش و اجاره"} · ${esc(it.status)}</span></div>
          <div class="mini" onclick='editItem(${esc(JSON.stringify(it)).replace(/&#39;/g,"\\u0027")})'>✏️</div>
          <div class="mini" onclick="delItem(${it.id})">🗑</div>
        </div>`).join("")
        :'<div class="empty"><span class="e">🖼</span>هنوز آیتمی ثبت نکردی.</div>');
  }catch(e){ b.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا.</div>'; }
}

function editItem(it){
  it=it||{mode:"both",kind:"gift",rent_min_days:1,rent_max_days:180,status:"available"};
  sheet(`<div class="grab"></div>
    <div class="shead"><b>${it.id?"✏️ ویرایشِ آیتم":"➕ آیتمِ جدید"}</b><div class="x" onclick="closeSheet()">✕</div></div>
    <label class="l">نوع</label>
    <select class="f" id="f_kind"><option value="gift" ${it.kind==="gift"?"selected":""}>🎁 گیفتِ NFT</option>
      <option value="username" ${it.kind==="username"?"selected":""}>@ یوزرنیم</option></select>
    <label class="l">نام *</label><input class="f" id="f_title" value="${esc(it.title||"")}"/>
    <label class="l">شماره / یوزرنیم</label><input class="f" id="f_ref" dir="ltr" value="${esc(it.ref||"")}"/>
    <label class="l">لینکِ تصویر</label><input class="f" id="f_image" dir="ltr" value="${esc(it.image||"")}"/>
    <label class="l">کالکشن</label><input class="f" id="f_collection" value="${esc(it.collection||"")}"/>
    <label class="l">مدل</label><input class="f" id="f_model" value="${esc(it.model||"")}"/>
    <label class="l">رنگ</label><input class="f" id="f_color" value="${esc(it.color||"")}"/>
    <label class="l">سمبل</label><input class="f" id="f_symbol" value="${esc(it.symbol||"")}"/>
    <label class="l">حالتِ عرضه</label>
    <select class="f" id="f_mode">
      <option value="both" ${it.mode==="both"?"selected":""}>فروش و اجاره</option>
      <option value="buy" ${it.mode==="buy"?"selected":""}>فقط فروش</option>
      <option value="rent" ${it.mode==="rent"?"selected":""}>فقط اجاره</option></select>
    <label class="l">قیمتِ فروش (تومان)</label><input class="f" id="f_sale" type="number" value="${it.sale_price||0}"/>
    <label class="l">اجارهٔ روزانه (تومان)</label><input class="f" id="f_rent" type="number" value="${it.rent_daily||0}"/>
    <div style="display:flex;gap:8px">
      <div style="flex:1"><label class="l">حداقل روز</label><input class="f" id="f_min" type="number" value="${it.rent_min_days||1}"/></div>
      <div style="flex:1"><label class="l">حداکثر روز</label><input class="f" id="f_max" type="number" value="${it.rent_max_days||180}"/></div>
    </div>
    <label class="l">شناسهٔ گیفت برایِ انتقالِ خودکار (اختیاری)</label>
    <input class="f" id="f_gift" dir="ltr" value="${esc(it.owned_gift_id||"")}"/>
    <label class="l">وضعیت</label>
    <select class="f" id="f_status">
      <option value="available" ${it.status==="available"?"selected":""}>در دسترس</option>
      <option value="hidden" ${it.status==="hidden"?"selected":""}>مخفی</option></select>
    <button class="cta" style="margin-top:6px" onclick="saveItem(${it.id||"null"})">💾 ذخیره</button>`);
}
async function saveItem(id){
  const g=i=>document.getElementById(i).value;
  const body={id:id,kind:g("f_kind"),title:g("f_title"),ref:g("f_ref"),image:g("f_image"),
    collection:g("f_collection"),model:g("f_model"),color:g("f_color"),symbol:g("f_symbol"),
    mode:g("f_mode"),sale_price:g("f_sale"),rent_daily:g("f_rent"),
    rent_min_days:g("f_min"),rent_max_days:g("f_max"),
    owned_gift_id:g("f_gift"),status:g("f_status")};
  if(!body.title.trim()){ toast("نام را وارد کن."); return; }
  if(window.__itemSaving) return;      // جلوگیری از دوبار-زدنِ «ذخیره» (آیتمِ تکراری)
  window.__itemSaving=true;
  const btn=document.querySelector("#sbox .cta"); if(btn){ btn.style.opacity=".5"; btn.textContent="⏳ در حال ذخیره…"; }
  try{ await api("/admin/item",{method:"POST",body:JSON.stringify(body)});
    closeSheet(); hap("medium"); loadAdminItems(); }
  catch(e){ toast(e&&e.message==="duplicate"?"این آیتم قبلاً ثبت شده (تکراری).":"ذخیره نشد.");
    if(btn){ btn.style.opacity="1"; btn.textContent="💾 ذخیره"; } }
  finally{ window.__itemSaving=false; }
}
async function delItem(id){
  try{ await api("/admin/item/"+id+"/delete",{method:"POST"}); hap(); loadAdminItems(); }
  catch(e){ toast(e.code===409?"این آیتم درگیرِ یک سفارش است.":"حذف نشد."); }
}
function editFee(){
  sheet(`<div class="grab"></div>
    <div class="shead"><b>💸 نرخ‌ها</b><div class="x" onclick="closeSheet()">✕</div></div>
    <div class="note">کارمزد به هر سفارش اضافه می‌شود. نرخِ TON فقط برایِ نمایشِ قیمتِ دوم است؛ صفر یعنی نمایش داده نشود.</div>
    <label class="l">کارمزدِ انتقال (تومان)</label><input class="f" id="f_fee" type="number" value="${ST.fee}"/>
    <label class="l">نرخِ هر TON (تومان)</label><input class="f" id="f_ton" type="number" value="${ST.tonRate}"/>
    <button class="cta" onclick="saveFee()">💾 ذخیره</button>`);
}
async function saveFee(){
  try{ const r=await api("/admin/fee",{method:"POST",body:JSON.stringify({
        fee:document.getElementById("f_fee").value,
        ton_rate:document.getElementById("f_ton").value})});
    ST.fee=r.fee; ST.tonRate=r.ton_rate||0; closeSheet(); loadAdminItems(); }
  catch(e){ toast("ذخیره نشد."); }
}

async function loadAdminOrders(){
  const b=document.getElementById("ab");
  try{
    const r=await api("/admin/orders");
    if(!r.orders.length){ b.innerHTML='<div class="empty"><span class="e">📋</span>هنوز سفارشی ثبت نشده.</div>'; return; }
    b.innerHTML=r.orders.map((o,i)=>{
      const cls=o.status==="delivered"?"b-ok":(["rejected","expired"].includes(o.status)?"b-no":"b-wait");
      const acts=(o.status==="pending"||o.status==="paid")
        ?`<div class="mini" style="flex:1;text-align:center" onclick="ordAct(${o.id},'delivered')">✅ تحویل شد</div>
          <div class="mini" style="flex:1;text-align:center" onclick="ordAct(${o.id},'reject')">↩️ لغو و عودت</div>`
        :(o.status==="delivered"&&o.kind==="rent"
          ?`<div class="mini" style="flex:1;text-align:center" onclick="ordAct(${o.id},'returned')">🔙 برگشت خورد</div>`:"");
      return `<div class="ord rv" style="animation-delay:${Math.min(i,8)*45}ms">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:7px">
          <b style="font-size:13px">#${o.id} · ${esc(o.title)}</b>
          <span class="badge ${cls}" style="margin-inline-start:auto">${esc(o.status_fa)}</span></div>
        <div class="attr"><span>خریدار</span><b>${esc(o.buyer)}</b></div>
        <div class="attr"><span>${o.kind==="buy"?"خرید":"اجاره ("+o.days+" روز)"}</span><b>${o.amount_fa} ت</b></div>
        ${o.note?`<div class="attr"><span>یادداشت</span><b>${esc(o.note)}</b></div>`:""}
        ${acts?`<div class="btns">${acts}</div>`:""}</div>`;
    }).join("");
  }catch(e){ b.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا.</div>'; }
}
async function ordAct(id,a){
  try{ await api("/admin/order/"+id+"/"+a,{method:"POST"}); hap("medium"); loadAdminOrders(); }
  catch(e){ toast("انجام نشد."); }
}

/* ───── منابع (واسطه‌گری) ───── */
const SRC_STATE={ok:["b-ok","✅ سالم"],expired:["b-no","⛔ توکن منقضی"],
                 error:["b-no","⚠️ خطا"],unknown:["b-wait","❔ تست نشده"]};

async function loadSources(){
  const b=document.getElementById("ab");
  try{
    const r=await api("/admin/sources");
    ST.providers=r.providers||[]; ST.tonRate=r.ton_rate||0;
    let h="";
    if(!ST.tonRate){
      h+='<div class="note rv" style="margin:0 0 10px;border:1px solid var(--no)">'+
         '⚠️ اول نرخِ هر TON را در تبِ «🖼 آیتم‌ها» → «💸 نرخ‌ها» تنظیم کن، '+
         'وگرنه قیمتِ تومانی قابلِ محاسبه نیست و همگام‌سازی کار نمی‌کند.</div>';
    }
    h+='<div class="note rv" style="margin:0 0 10px">'+
       'آیتم‌ها از مارکتِ بیرونی خوانده و با درصدِ سودِ تو قیمت‌گذاری می‌شوند. '+
       'لازم نیست گیفت را از قبل داشته باشی — سفارش که آمد، از مبدأ می‌خری و تحویل می‌دهی.'+
       '<br/><br/>⚠️ توکنِ این مارکت‌ها هر ۲ تا ۳ روز باطل می‌شود؛ وقتی شد، ربات خبرت می‌کند.</div>'+
       '<button class="cta" style="padding:12px;font-size:13px;margin-bottom:11px" '+
       'onclick="editSource(null)">➕ منبعِ جدید</button>';
    h+=(r.sources.length?r.sources.map(function(x,i){
      const st=SRC_STATE[x.state]||SRC_STATE.unknown;
      return '<div class="ord rv" style="animation-delay:'+(Math.min(i,8)*45)+'ms">'+
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:7px">'+
        '<b style="font-size:13px">'+esc(x.title)+'</b>'+
        '<span class="badge '+st[0]+'" style="margin-inline-start:auto">'+st[1]+'</span></div>'+
        '<div class="attr"><span>درصدِ سود</span><b>'+x.markup+'٪</b></div>'+
        '<div class="attr"><span>سقفِ آیتم</span><b>'+x.max_items+'</b></div>'+
        (x.collection?'<div class="attr"><span>کالکشن</span><b>'+esc(x.collection)+'</b></div>':"")+
        '<div class="attr"><span>مخفی‌کردنِ ناموجود</span><b>'+(x.auto_hide?"بله":"خیر")+'</b></div>'+
        '<div class="attr"><span>آخرین همگام‌سازی</span><b>'+
          (x.last_sync?x.last_sync.slice(0,16).replace("T"," "):"—")+'</b></div>'+
        (x.error?'<div class="note" style="margin-top:7px">'+esc(x.error)+'</div>':"")+
        '<div class="btns">'+
          '<div class="mini" style="flex:1;text-align:center" onclick="srcAct('+x.id+',\'sync\')">🔄 همگام‌سازی</div>'+
          '<div class="mini" style="flex:1;text-align:center" onclick="srcAct('+x.id+',\'test\')">🧪 تست</div>'+
          '<div class="mini" onclick=\'editSource('+esc(JSON.stringify(x))+')\'>✏️</div>'+
          '<div class="mini" onclick="srcAct('+x.id+',\'delete\')">🗑</div>'+
        '</div></div>';}).join("")
      :'<div class="empty"><span class="e">🔌</span>هنوز منبعی اضافه نکردی.</div>');
    b.innerHTML=h;
  }catch(e){ b.innerHTML='<div class="empty"><span class="e">⚠️</span>خطا.</div>'; }
}

function editSource(x){
  x=x||{provider:"tonnel",markup:15,max_items:60,auto_hide:true,enabled:true,collection:""};
  const provs=(ST.providers||[]);
  const cur=provs.find(function(p){return p.key===x.provider;})||provs[0]||{auth_help:""};
  sheet('<div class="grab"></div>'+
    '<div class="shead"><b>'+(x.id?"✏️ ویرایشِ منبع":"➕ منبعِ جدید")+
    '</b><div class="x" onclick="closeSheet()">✕</div></div>'+
    '<label class="l">مارکت</label>'+
    '<select class="f" id="s_prov" onchange="srcHelp()">'+
    provs.map(function(p){return '<option value="'+p.key+'" '+(x.provider===p.key?"selected":"")+
      ' '+(p.ready?"":"disabled")+'>'+esc(p.label)+(p.ready?"":" (به‌زودی)")+'</option>';}).join("")+
    '</select>'+
    '<div class="note" id="s_help">'+(cur.auth_help||"")+'</div>'+
    '<label class="l">توکن (authData)'+(x.has_auth?" — ثبت شده، خالی بگذار تا عوض نشود":"")+'</label>'+
    '<input class="f" id="s_auth" dir="ltr" placeholder="'+(x.has_auth?"••••••••":"user=...")+'"/>'+
    '<label class="l">نامِ نمایشی</label><input class="f" id="s_title" value="'+esc(x.title||"")+'"/>'+
    '<label class="l">درصدِ سود (٪)</label><input class="f" id="s_markup" type="number" value="'+x.markup+'"/>'+
    '<label class="l">حداکثر آیتم</label><input class="f" id="s_max" type="number" value="'+x.max_items+'"/>'+
    '<label class="l">فقط این کالکشن (خالی = همه)</label>'+
    '<input class="f" id="s_coll" value="'+esc(x.collection||"")+'"/>'+
    '<label class="l">آیتمِ ناموجود در مبدأ</label>'+
    '<select class="f" id="s_hide"><option value="1" '+(x.auto_hide?"selected":"")+'>خودکار مخفی شود</option>'+
    '<option value="0" '+(x.auto_hide?"":"selected")+'>دست نخورد</option></select>'+
    '<label class="l">وضعیت</label>'+
    '<select class="f" id="s_on"><option value="1" '+(x.enabled?"selected":"")+'>فعال</option>'+
    '<option value="0" '+(x.enabled?"":"selected")+'>غیرفعال</option></select>'+
    '<button class="cta" onclick="saveSource('+(x.id||"null")+')">💾 ذخیره</button>');
}
function srcHelp(){
  const k=document.getElementById("s_prov").value;
  const p=(ST.providers||[]).find(function(p){return p.key===k;});
  document.getElementById("s_help").innerHTML=(p&&p.auth_help)||"";
}
async function saveSource(id){
  const g=function(i){return document.getElementById(i).value;};
  try{
    await api("/admin/source",{method:"POST",body:JSON.stringify({
      id:id, provider:g("s_prov"), auth:g("s_auth"), title:g("s_title"),
      markup:g("s_markup"), max_items:g("s_max"), collection:g("s_coll"),
      auto_hide:g("s_hide")==="1", enabled:g("s_on")==="1"})});
    closeSheet(); hap("medium"); loadSources();
  }catch(e){ toast(e.code===409?"این مارکت هنوز پشتیبانی نمی‌شود.":"ذخیره نشد."); }
}
async function srcAct(id,a){
  if(a==="delete"){
    sheet('<div class="grab"></div>'+
      '<div class="shead"><b>حذفِ منبع</b><div class="x" onclick="closeSheet()">✕</div></div>'+
      '<div class="note">آیتم‌هایی که از این منبع آمده‌اند حذف نمی‌شوند، فقط مخفی '+
      'می‌شوند و دیگر همگام‌سازی نخواهند شد.</div>'+
      '<button class="cta" style="margin-top:10px" onclick="srcDel('+id+')">🗑 حذف کن</button>');
    return;
  }
  const b=document.getElementById("ab");
  if(b) b.insertAdjacentHTML("afterbegin",
    '<div class="note">'+(a==="sync"?"در حال همگام‌سازی…":"در حال تست…")+'</div>');
  try{
    const r=await api("/admin/source/"+id+"/"+a,{method:"POST"});
    hap("medium"); toast((r.ok?"✅ ":"❌ ")+(r.note||""));
  }catch(e){ toast("انجام نشد."); }
  loadSources();
}
async function srcDel(id){
  try{ await api("/admin/source/"+id+"/delete",{method:"POST"});
    closeSheet(); loadSources(); }
  catch(e){ toast("حذف نشد."); }
}


/*P2P_JS*/
boot();
</script></body></html>"""

# === PATCH_P2P_MARKET_V1 === ظاهرِ «بازارِ کاربران» از ماژولِ جدا تزریق می‌شود
from tenant.bots.virtual_service.nft_p2p_ui import P2P_CSS as _P2P_CSS, P2P_JS as _P2P_JS  # noqa: E402
assert "</style></head>" in _PAGE_HTML and "/*P2P_JS*/" in _PAGE_HTML
_PAGE_HTML = (_PAGE_HTML.replace("</style></head>", _P2P_CSS + "</style></head>", 1)
              .replace("/*P2P_JS*/", _P2P_JS, 1))
