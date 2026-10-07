"""
public_api.py — APIِ عمومیِ هر ربات، رویِ دامنهٔ اختصاصیِ خودش (PATCH_PUBLIC_API_V1).

هر کاربرِ نهاییِ یک ربات می‌تونه از «👤 پروفایلِ من ← 🔑 دریافتِ API Key» یه
کلیدِ شخصی بگیره و با اون به محصولات/سفارش‌ها/موجودیِ *خودش* (نه بقیه‌ی
کاربرها) دسترسیِ برنامه‌نویسی داشته باشه.

احرازِ هویت: هدرِ X-API-Key. تشخیصِ اینکه این درخواست مالِ کدوم رباته، دو
جوره:
  ۱) از رویِ Host هدر — اگه دامنه از قبل با tenant/custom_domain.py
     تأیید شده باشه (status="verified")، همون bot_id استفاده می‌شه. این
     مسیرِ اصلیه — همون‌جوری که تو اسکرین‌شاتِ نمونه‌ربات دیده شد.
  ۲) از رویِ مسیر (/api/v1/{bot_id}/...) — یه راهِ جایگزین رویِ دامنهٔ
     خودِ پلتفرم، برایِ وقتی که دامنهٔ اختصاصی هنوز آماده نشده (مثلاً
     Caddy هنوز رویِ سرور نصب نشده) — یعنی این قابلیت از روزِ اول کار
     می‌کنه، حتی قبل از راه‌اندازیِ کاملِ دامنهٔ اختصاصی.

نکتهٔ امنیتی: کلید همیشه اسکوپ‌شده به یک BotUserِ مشخص است — یعنی حتی اگه
کسی کلیدِ یه کاربر رو داشته باشه، فقط به دیتایِ همون یک کاربر می‌رسه، نه
کلِ ربات یا کاربرهایِ دیگه.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from config import Session
from models import BotUser, SmmCategory, SmmService, SmmOrder

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

public_api_router = APIRouter()


async def _bot_id_from_host(host: str) -> int | None:
    from models import BotCustomDomain
    domain = (host or "").split(":")[0].lower()
    async with Session() as s:
        d = await s.scalar(select(BotCustomDomain).where(
            BotCustomDomain.domain == domain, BotCustomDomain.status == "verified"))
    return d.bot_id if d else None


async def _authed_user(request: Request, x_api_key: str | None, bot_id_path: int | None) -> tuple[int, BotUser]:
    """bot_id رو (از مسیر یا از Host) و کاربرِ صاحبِ کلید رو برمی‌گردونه؛
    اگه هرکدوم نامعتبر بود 401/404 می‌ده."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing_api_key")
    bot_id = bot_id_path
    if bot_id is None:
        bot_id = await _bot_id_from_host(request.headers.get("host", ""))
    if not bot_id:
        raise HTTPException(status_code=404, detail="unknown_domain_or_bot")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.api_key == x_api_key))
    if not u:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    if u.is_banned:
        raise HTTPException(status_code=403, detail="banned")
    return bot_id, u


async def _svc_payload(bot_id: int, v: SmmService) -> dict:
    from tenant.bots.virtual_service.smm import compute_sell_price
    price_1000 = await compute_sell_price(bot_id, v, 1000)
    cat_name = None
    if v.category_pk:
        async with Session() as s:
            c = await s.get(SmmCategory, v.category_pk)
        if c:
            cat_name = c.name
    return {
        "id": v.id,
        "name": v.name,
        "description": v.description,
        "category": cat_name,
        "price_per_1000": price_1000,
        "min_qty": v.min_qty,
        "max_qty": v.max_qty,
        "is_active": v.is_active,
    }


# ═══════════════════ محصولات ═══════════════════

@public_api_router.get("/api/v1/products")
async def api_products_host(request: Request, x_api_key: str | None = Header(default=None)):
    bot_id, _u = await _authed_user(request, x_api_key, None)
    return await _list_products(bot_id)


@public_api_router.get("/api/v1/{bot_id}/products")
async def api_products_path(bot_id: int, request: Request, x_api_key: str | None = Header(default=None)):
    bot_id, _u = await _authed_user(request, x_api_key, bot_id)
    return await _list_products(bot_id)


async def _list_products(bot_id: int) -> JSONResponse:
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == bot_id, SmmService.is_active == True).order_by(SmmService.id))).all()
    items = [await _svc_payload(bot_id, v) for v in svcs]
    return JSONResponse({"ok": True, "count": len(items), "items": items})


# ═══════════════════ کیف‌پول ═══════════════════

@public_api_router.get("/api/v1/wallet")
async def api_wallet_host(request: Request, x_api_key: str | None = Header(default=None)):
    _bot_id, u = await _authed_user(request, x_api_key, None)
    return JSONResponse({"ok": True, "balance": float(u.balance)})


@public_api_router.get("/api/v1/{bot_id}/wallet")
async def api_wallet_path(bot_id: int, request: Request, x_api_key: str | None = Header(default=None)):
    _bot_id, u = await _authed_user(request, x_api_key, bot_id)
    return JSONResponse({"ok": True, "balance": float(u.balance)})


# ═══════════════════ سفارش‌ها ═══════════════════

def _order_payload(o: SmmOrder) -> dict:
    return {
        "id": o.id,
        "tracking_code": o.tracking_code,
        "service_id": o.service_id,
        "service_name": o.service_name,
        "quantity": o.quantity,
        "link": o.link,
        "price_toman": float(o.price_toman or 0),
        "status": o.status,
        "panel_status": o.panel_status,
        "remaining": o.remaining,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


@public_api_router.get("/api/v1/orders")
async def api_orders_host(request: Request, x_api_key: str | None = Header(default=None)):
    bot_id, u = await _authed_user(request, x_api_key, None)
    return await _list_orders(bot_id, u)


@public_api_router.get("/api/v1/{bot_id}/orders")
async def api_orders_path(bot_id: int, request: Request, x_api_key: str | None = Header(default=None)):
    bot_id, u = await _authed_user(request, x_api_key, bot_id)
    return await _list_orders(bot_id, u)


async def _list_orders(bot_id: int, u: BotUser) -> JSONResponse:
    async with Session() as s:
        orders = (await s.scalars(select(SmmOrder).where(
            SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == u.telegram_id
        ).order_by(SmmOrder.id.desc()).limit(200))).all()
    return JSONResponse({"ok": True, "count": len(orders), "items": [_order_payload(o) for o in orders]})


class NewOrderBody(BaseModel):
    service_id: int  # همون id که از /products برمی‌گرده (SmmService.id، نه شناسهٔ پنل)
    link: str
    quantity: int


async def _create_order(bot_id: int, u: BotUser, body: NewOrderBody) -> JSONResponse:
    """=== PATCH_PUBLIC_API_V1 ===
    عمداً منطقِ اصلیِ خرید (که تویِ tenant/smm.py و مسیرِ تلگرامیِ ربات
    هست) رو از نو بازنویسی نکردیم — همون توابعِ آمادهٔ compute_sell_price/
    _panel_order_mode/_place_on_panel رو صدا می‌زنیم، فقط جایِ FSM/پیامِ
    تلگرام، مستقیم از بدنه‌ی درخواست می‌خونیم. یعنی رفتار (خودکار/تأییدِ
    ادمین، سیاستِ خطا و...) دقیقاً همون چیزیه که ادمین از تنظیماتِ پنل
    گذاشته."""
    from tenant.bots.virtual_service.smm import compute_sell_price, _panel_order_mode, _place_on_panel
    from tenant import _gen_tracking
    async with Session() as s:
        v = await s.get(SmmService, body.service_id)
    if not v or v.bot_id != bot_id or not v.is_active:
        raise HTTPException(status_code=404, detail="service_not_found")
    # min_qty ممکن است NULL باشد (قبلاً TypeError → خطای ۵۰۰)؛ تعدادِ صفر/منفی هم رد می‌شود
    if (body.quantity <= 0 or body.quantity < (v.min_qty or 0)
            or (v.max_qty and body.quantity > v.max_qty)):
        raise HTTPException(status_code=422, detail=f"quantity_out_of_range min={v.min_qty} max={v.max_qty}")
    if not body.link or len(body.link) > 500:
        raise HTTPException(status_code=422, detail="invalid_link")
    price = await compute_sell_price(bot_id, v, body.quantity)
    if price <= 0:
        raise HTTPException(status_code=422, detail="service_has_no_price")
    async with Session() as s:
        uu = await s.get(BotUser, u.id, with_for_update=True)
        if not uu or float(uu.balance) < price:
            raise HTTPException(status_code=402, detail="insufficient_balance")
        uu.balance = float(uu.balance) - price
        trk = _gen_tracking()
        o = SmmOrder(
            bot_id=bot_id, user_telegram_id=u.telegram_id, smm_service_pk=v.id,
            service_id=v.service_id, service_name=v.name, quantity=body.quantity, link=body.link,
            price_toman=price, status="awaiting_admin", tracking_code=trk,
            panel_pk=v.panel_pk, updated_at=datetime.utcnow(),
        )
        s.add(o)
        from models import WalletTxn
        s.add(WalletTxn(bot_id=bot_id, user_id=uu.id, amount=-price,
                        kind="smm_order_api", note=f"سفارشِ API: {v.name[:40]} × {body.quantity}"))
        await s.commit()
        order_pk = o.id
    order_mode = await _panel_order_mode(bot_id, v.panel_pk)
    if order_mode == "auto" and not v.is_manual:
        await _place_on_panel(bot_id, order_pk)
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
    return JSONResponse({"ok": True, "order": _order_payload(o)}, status_code=201)


@public_api_router.post("/api/v1/orders")
async def api_new_order_host(body: NewOrderBody, request: Request, x_api_key: str | None = Header(default=None)):
    bot_id, u = await _authed_user(request, x_api_key, None)
    return await _create_order(bot_id, u, body)


@public_api_router.post("/api/v1/{bot_id}/orders")
async def api_new_order_path(bot_id: int, body: NewOrderBody, request: Request,
                              x_api_key: str | None = Header(default=None)):
    bot_id, u = await _authed_user(request, x_api_key, bot_id)
    return await _create_order(bot_id, u, body)
