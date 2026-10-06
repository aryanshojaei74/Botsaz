"""
public_api.py — APIِ عمومیِ هر ربات، رویِ دامنهٔ اختصاصیِ خودش (PATCH_PUBLIC_API_V1).

=== PATCH_PUBLIC_API_PER_USER_V1 ===
دو نوع کلید پشتیبانی می‌شه:
  ۱) کلیدِ سطحِ ربات (Bot.api_key) — از پنلِ مدیریت ساخته می‌شه، دسترسیِ
     کامل به همه‌چیزِ ربات (همه‌ی سفارش‌ها، ثبتِ سفارش برایِ هر مشتری‌ای
     با دادنِ telegram_id).
  ۲) کلیدِ شخصیِ هر مشتری (BotUser.api_key) — فقط وقتی ادمین از پروفایلِ
     همون کاربر «دسترسیِ API» رو براش فعال کرده باشه. این کلید فقط به
     دیتایِ خودِ همون کاربر دسترسی داره، و اگه ادمین برایِ اون کاربر
     درصدِ تخفیفِ اختصاصی گذاشته باشه، رویِ قیمتِ سفارش‌هاش خودکار اعمال
     می‌شه.

احرازِ هویت: هدرِ X-API-Key، اول با Bot.api_key چک می‌شه (دسترسیِ کامل)،
اگه نبود با BotUser.api_key (دسترسیِ محدود به خودش).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser, SmmCategory, SmmService, SmmOrder

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

public_api_router = APIRouter()

# رباتِ‌هایی که «API» دارند: خدماتِ مجازی (SMM v1/v2) و فروشِ VPN (/api/vpn)  — PATCH_VPN_API_V1
API_FIELDS = ("virtual_service", "vpn_sales")


class AuthCtx:
    """نتیجه‌ی احرازِ هویت — یا دسترسیِ کامل (bot-level) یا محدود به یه
    کاربرِ خاص (user-level، با تخفیفِ اختصاصیِ احتمالی)."""
    __slots__ = ("bot_id", "user", "discount_pct", "full_access")

    def __init__(self, bot_id: int, user: BotUser | None, discount_pct: float, full_access: bool):
        self.bot_id = bot_id
        self.user = user
        self.discount_pct = discount_pct
        self.full_access = full_access


# === PATCH_API_OPEN_TO_ALL_V1 ===
# سوئیچِ سطحِ ربات: «API برایِ همهٔ کاربران باز باشه» (Setting: api_open_to_all=1).
# وقتی روشنه، هر کاربری که کلیدِ شخصی داره (از «پروفایلِ من») بدونِ نیاز به
# فعال‌کردنِ دستیِ ادمین کار می‌کنه؛ وقتی خاموشه رفتارِ قبلی (فقط کاربرانِ
# انتخاب‌شده) حفظ می‌شه.
async def api_open_to_all(bot_id: int) -> bool:
    from models import Setting
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "api_open_to_all"))
    return bool(row and (row.value or "").strip() == "1")


# === PATCH_API_PAID_ACCESS_V1 ===
# دسترسیِ API می‌تواند پولی باشد (Setting: api_price به تومان، api_period_days:
# «0» = دائمی، «30» = ماهانه). دسترسیِ دستیِ ادمین (api_expires_at=None) دائمی است.
async def api_price(bot_id: int) -> int:
    from models import Setting
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "api_price"))
    try:
        return max(0, int(float((row.value if row else "0") or 0)))
    except (TypeError, ValueError):
        return 0


async def api_period_days(bot_id: int) -> int:
    from models import Setting
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "api_period_days"))
    try:
        return max(0, int(float((row.value if row else "0") or 0)))
    except (TypeError, ValueError):
        return 0


def api_expired(u) -> bool:
    import datetime as _d
    exp = getattr(u, "api_expires_at", None)
    return exp is not None and exp <= _d.datetime.utcnow()


async def user_api_allowed(u) -> bool:
    """آیا این کاربر الان حق دارد از API استفاده کند؟ (تنها نقطهٔ تصمیم)"""
    if u is None:
        return False
    if u.api_access and not api_expired(u):
        return True
    # «باز برایِ همه» فقط وقتی معنی دارد که API رایگان باشد
    return await api_open_to_all(u.bot_id) and await api_price(u.bot_id) == 0


async def _authed(x_api_key: str | None) -> AuthCtx:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing_api_key")
    async with Session() as s:
        b = await s.scalar(select(BotRow).where(BotRow.api_key == x_api_key))
        if b:
            if b.status != "active":
                raise HTTPException(status_code=403, detail="bot_not_active")
            return AuthCtx(bot_id=b.id, user=None, discount_pct=0.0, full_access=True)
        u = await s.scalar(select(BotUser).where(BotUser.api_key == x_api_key))
    if not u or not await user_api_allowed(u):
        raise HTTPException(status_code=401, detail="invalid_api_key")
    if u.is_banned:
        raise HTTPException(status_code=403, detail="banned")
    return AuthCtx(bot_id=u.bot_id, user=u, discount_pct=float(u.api_discount_pct or 0), full_access=False)


async def _svc_payload(bot_id: int, v: SmmService, discount_pct: float = 0.0,
                       _cats: dict | None = None) -> dict:
    from tenant.bots.virtual_service.smm import compute_sell_price
    price_1000 = await compute_sell_price(bot_id, v, 1000)
    if discount_pct:
        price_1000 = round(price_1000 * (100 - discount_pct) / 100)
    cat_name = None
    if v.category_pk:
        if _cats is not None:
            cat_name = _cats.get(v.category_pk)   # PATCH_API_CATS_PRELOAD_V1: بدونِ یک کوئری به‌ازایِ هر سرویس
        else:
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


async def _load_cats(bot_id: int) -> dict:
    async with Session() as s:
        rows = (await s.execute(select(SmmCategory.id, SmmCategory.name).where(
            SmmCategory.bot_id == bot_id))).all()
    return {r[0]: r[1] for r in rows}


# ═══════════════════ محصولات ═══════════════════

@public_api_router.get("/api/v1/products")
async def api_products(x_api_key: str | None = Header(default=None)):
    ctx = await _authed(x_api_key)
    async with Session() as s:
        svcs = (await s.scalars(select(SmmService).where(
            SmmService.bot_id == ctx.bot_id, SmmService.is_active == True).order_by(SmmService.id))).all()
    cats = await _load_cats(ctx.bot_id)
    items = [await _svc_payload(ctx.bot_id, v, ctx.discount_pct, cats) for v in svcs]
    return JSONResponse({"ok": True, "count": len(items), "items": items})


# ═══════════════════ کیف‌پول ═══════════════════

@public_api_router.get("/api/v1/wallet")
async def api_wallet(telegram_id: int | None = None, x_api_key: str | None = Header(default=None)):
    ctx = await _authed(x_api_key)
    if not ctx.full_access:
        # کلیدِ شخصی: فقط موجودیِ خودش، telegram_id (اگه دادن) نادیده گرفته می‌شه
        u = ctx.user
        return JSONResponse({"ok": True, "telegram_id": u.telegram_id, "balance": float(u.balance)})
    if telegram_id is None:
        raise HTTPException(status_code=422, detail="telegram_id_required")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == ctx.bot_id, BotUser.telegram_id == telegram_id))
    if not u:
        raise HTTPException(status_code=404, detail="user_not_found")
    return JSONResponse({"ok": True, "telegram_id": telegram_id, "balance": float(u.balance)})


# ═══════════════════ سفارش‌ها ═══════════════════

def _order_payload(o: SmmOrder) -> dict:
    return {
        "id": o.id,
        "tracking_code": o.tracking_code,
        "user_telegram_id": o.user_telegram_id,
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
async def api_orders(telegram_id: int | None = None, x_api_key: str | None = Header(default=None)):
    """کلیدِ کامل: بدونِ telegram_id یعنی همه‌ی سفارش‌هایِ ربات؛ با
    telegram_id یعنی فقط همون مشتری. کلیدِ شخصی: همیشه فقط سفارش‌هایِ
    خودش (telegram_id اگه دادن نادیده گرفته می‌شه)."""
    ctx = await _authed(x_api_key)
    async with Session() as s:
        q = select(SmmOrder).where(SmmOrder.bot_id == ctx.bot_id)
        if not ctx.full_access:
            q = q.where(SmmOrder.user_telegram_id == ctx.user.telegram_id)
        elif telegram_id is not None:
            q = q.where(SmmOrder.user_telegram_id == telegram_id)
        orders = (await s.scalars(q.order_by(SmmOrder.id.desc()).limit(200))).all()
    return JSONResponse({"ok": True, "count": len(orders), "items": [_order_payload(o) for o in orders]})


class NewOrderBody(BaseModel):
    service_id: int  # همون id که از /products برمی‌گرده (SmmService.id، نه شناسهٔ پنل)
    link: str
    quantity: int
    telegram_id: int | None = None  # فقط برایِ کلیدِ سطحِ ربات لازمه؛ کلیدِ شخصی نادیده می‌گیرتش


@public_api_router.post("/api/v1/orders")
async def api_new_order(body: NewOrderBody, x_api_key: str | None = Header(default=None)):
    """=== PATCH_PUBLIC_API_V1 ===
    عمداً منطقِ اصلیِ خرید (که تویِ tenant/smm.py و مسیرِ تلگرامیِ ربات
    هست) رو از نو بازنویسی نکردیم — همون توابعِ آمادهٔ compute_sell_price/
    _panel_order_mode/_place_on_panel رو صدا می‌زنیم. رفتار (خودکار/تأییدِ
    ادمین، سیاستِ خطا و...) دقیقاً همون چیزیه که ادمین از تنظیماتِ پنل
    گذاشته."""
    ctx = await _authed(x_api_key)
    if ctx.full_access:
        if body.telegram_id is None:
            raise HTTPException(status_code=422, detail="telegram_id_required")
        target_tid = body.telegram_id
        discount_pct = 0.0
    else:
        target_tid = ctx.user.telegram_id
        discount_pct = ctx.discount_pct

    from tenant.bots.virtual_service.smm import compute_sell_price, _panel_order_mode, _place_on_panel
    from tenant import _gen_tracking
    async with Session() as s:
        v = await s.get(SmmService, body.service_id)
    if not v or v.bot_id != ctx.bot_id or not v.is_active:
        raise HTTPException(status_code=404, detail="service_not_found")
    if body.quantity < v.min_qty or (v.max_qty and body.quantity > v.max_qty):
        raise HTTPException(status_code=422, detail=f"quantity_out_of_range min={v.min_qty} max={v.max_qty}")
    if not body.link or len(body.link) > 500:
        raise HTTPException(status_code=422, detail="invalid_link")
    price = await compute_sell_price(ctx.bot_id, v, body.quantity)
    if discount_pct:
        price = round(price * (100 - discount_pct) / 100)
    if price <= 0:
        raise HTTPException(status_code=422, detail="service_has_no_price")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == ctx.bot_id, BotUser.telegram_id == target_tid).with_for_update())
        if not u:
            raise HTTPException(status_code=404, detail="user_not_found")
        if float(u.balance) < price:
            raise HTTPException(status_code=402, detail="insufficient_balance")
        u.balance = float(u.balance) - price
        from models import alloc_tracking_in as _alloc_in  # PATCH_SEQ_TRACKING_V1
        trk = await _alloc_in(s, ctx.bot_id)
        o = SmmOrder(
            bot_id=ctx.bot_id, user_telegram_id=target_tid, smm_service_pk=v.id,
            service_id=v.service_id, service_name=v.name, quantity=body.quantity, link=body.link,
            price_toman=price, status="awaiting_admin", tracking_code=trk,
            panel_pk=v.panel_pk, updated_at=datetime.utcnow(),
            source="api",   # PATCH_API_PAGE_CLEAN_V1
        )
        s.add(o)
        from models import WalletTxn
        s.add(WalletTxn(bot_id=ctx.bot_id, user_id=u.id, amount=-price,
                        kind="smm_order_api", note=f"سفارشِ API: {v.name[:40]} × {body.quantity}"))
        await s.commit()
        order_pk = o.id
    # === PATCH_API_PLACE_SAFE_V1 ===
    # پولِ مشتری همین‌جا کسر شده و سفارش ثبت شده؛ اگر ارسال به پنلِ بالادستی خطا بدهد نباید به مشتری
    # HTTP 500 بدهیم (سفارش وجود دارد، فقط ادمین/سیاستِ خطایِ همان پنل رسیدگی می‌کند).
    try:
        order_mode = await _panel_order_mode(ctx.bot_id, v.panel_pk)
        if order_mode == "auto" and not v.is_manual:
            await _place_on_panel(ctx.bot_id, order_pk)
    except Exception:
        logger.exception("API: ارسالِ خودکارِ سفارش %s به پنل ناموفق بود (سفارش ثبت شده است)", order_pk)
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
    return JSONResponse({"ok": True, "order": _order_payload(o)}, status_code=201)


# ═══════════════ APIِ استانداردِ SMM (v2) ═══════════════
# === PATCH_PUBLIC_API_SMM_V2_V1 ===
# تا اینجا API این ربات قراردادِ اختصاصیِ خودش را داشت (/api/v1، هدرِ
# X-API-Key، بدنهٔ JSON). مشکلِ عملی: مشتری‌هایِ ربات‌هایِ «خدماتِ مجازی»
# با اکوسیستمِ SMM کار می‌کنند و همه‌ی ابزارها/پنل‌هایشان قراردادِ
# استانداردِ SMM را می‌فهمند — یک اندپوینتِ واحد با key/action. پس همان
# منطقِ v1 اینجا با قالبِ استاندارد هم عرضه می‌شود تا ربات بتواند نقشِ
# «پنل» را برای مشتری‌هایش بازی کند.
#
# عمداً هیچ منطقِ خریدی از نو نوشته نشده: add دقیقاً همان مسیرِ
# api_new_order را صدا می‌زند (کسر از کیف‌پول، تخفیفِ اختصاصیِ کاربر،
# حالتِ خودکار/تأییدِ ادمینِ همان پنل).
#
# واحدِ پول تومان است (نه دلار) — چون قیمت‌هایِ خودِ ربات تومانی‌اند.

_V2_STATUS_MAP = {
    "awaiting_admin": "Pending",
    "sent": "In progress",
    "completed": "Completed",
    "failed": "Canceled",
    "rejected": "Canceled",
    "canceled": "Canceled",
    "refunded": "Refunded",
    "partial": "Partial",   # === PATCH_SMM_PARTIAL_V1 ===
}


def _v2_err(message: str, status_code: int = 200) -> JSONResponse:
    """خطاها در قراردادِ SMM با HTTP 200 و بدنهٔ {"error": ...} برمی‌گردند."""
    return JSONResponse({"error": message}, status_code=status_code)


async def _v2_authed(key: str | None) -> AuthCtx | None:
    try:
        return await _authed(key)
    except HTTPException:
        return None


async def _v2_params(request: Request) -> dict:
    """پارامترها از query string و از بدنهٔ POST (urlencoded / JSON / multipart).

    === PATCH_API_PARSE_NO_MULTIPART_V1 ===
    قالبِ استانداردِ SMM یک POSTِ application/x-www-form-urlencoded است. قبلاً بدنه فقط با
    `request.form()` خوانده می‌شد که به پکیجِ python-multipart وابسته است (و در requirements.txt
    نبود): وقتی نصب نبود، خطا بلعیده می‌شد و کلید/اکشن خالی می‌ماند → «Incorrect API key» برایِ
    همهٔ ابزارها. حالا urlencoded و JSON بدونِ هیچ وابستگیِ اضافه خوانده می‌شوند؛ فقط multipart
    (که SMM استاندارد استفاده نمی‌کند) به آن پکیج نیاز دارد و اگر نبود، بی‌صدا رد می‌شود.
    """
    from urllib.parse import parse_qsl
    data = dict(request.query_params)
    if request.method != "POST":
        return data
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        raw = await request.body()
    except Exception:
        logger.exception("خواندنِ بدنهٔ درخواستِ API ناموفق بود")
        return data
    if not raw:
        return data
    if "multipart/form-data" in ctype:
        try:
            form = await request.form()
            for k, v in form.items():
                data.setdefault(k, str(v))
        except Exception:
            logger.warning("multipart در API پشتیبانی نمی‌شود (python-multipart نصب نیست؟)")
        return data
    text = raw.decode("utf-8", "replace").strip()
    if "json" in ctype or text.startswith("{"):
        try:
            import json as _json
            body = _json.loads(text)
            if isinstance(body, dict):
                for k, v in body.items():
                    data.setdefault(k, "" if v is None else str(v))
                return data
        except Exception:
            logger.debug("بدنهٔ JSON نامعتبر بود؛ به‌عنوانِ urlencoded امتحان می‌شود")
    for k, v in parse_qsl(text, keep_blank_values=True):
        data.setdefault(k, v)
    return data


async def _v2_order_status_payload(o: SmmOrder) -> dict:
    remains = o.remaining if o.remaining is not None else (
        0 if o.status == "completed" else o.quantity)
    # === PATCH_SMM_PARTIAL_V1 === در سفارشِ نیمه‌تمام، charge همان مبلغِ نهاییِ سفارش است (پس از بازگشتِ مابقی)
    charge = float(o.price_toman or 0)
    if o.status == "partial":
        charge = max(0.0, charge - float(getattr(o, "refunded_toman", None) or 0))
    return {
        "charge": str(charge),
        "start_count": "0",
        "status": _V2_STATUS_MAP.get(o.status, "Pending"),
        "remains": str(remains),
        "currency": "TOMAN",
    }


@public_api_router.api_route("/v2", methods=["GET", "POST"])
@public_api_router.api_route("/api/v2", methods=["GET", "POST"])
async def smm_v2(request: Request):
    p = await _v2_params(request)
    ctx = await _v2_authed((p.get("key") or "").strip())
    if not ctx:
        return _v2_err("Incorrect API key")
    action = (p.get("action") or "").strip().lower()

    # ─── services ───
    if action == "services":
        async with Session() as s:
            svcs = (await s.scalars(select(SmmService).where(
                SmmService.bot_id == ctx.bot_id,
                SmmService.is_active == True))).all()  # noqa: E712
        out = []
        _cats = await _load_cats(ctx.bot_id)
        for v in svcs:
            d = await _svc_payload(ctx.bot_id, v, ctx.discount_pct, _cats)
            out.append({
                "service": v.id,
                "name": d["name"],
                "type": "Default",
                "category": d["category"] or "General",
                "rate": str(d["price_per_1000"]),
                "min": str(v.min_qty or 1),
                "max": str(v.max_qty or 0),
                "refill": False,
                "cancel": False,
            })
        return JSONResponse(out)

    # ─── balance ───
    if action == "balance":
        if ctx.full_access:
            return JSONResponse({"balance": "0", "currency": "TOMAN"})
        return JSONResponse({"balance": str(float(ctx.user.balance)), "currency": "TOMAN"})

    # ─── add ───
    if action == "add":
        if any(p.get(k) for k in ("dripfeed", "runs", "interval")):
            return _v2_err("Dripfeed is not supported")
        try:
            service_id = int(p.get("service") or 0)
            quantity = int(p.get("quantity") or 0)
        except ValueError:
            return _v2_err("Incorrect service or quantity")
        link = (p.get("link") or "").strip()
        body = NewOrderBody(service_id=service_id, link=link, quantity=quantity,
                            telegram_id=int(p["telegram_id"]) if p.get("telegram_id") else None)
        try:
            res = await api_new_order(body, x_api_key=(p.get("key") or "").strip())
        except HTTPException as e:
            return _v2_err(str(e.detail))
        import json as _json
        payload = _json.loads(bytes(res.body).decode("utf-8"))
        return JSONResponse({"order": payload["order"]["id"]})

    # ─── status ───
    if action == "status":
        ids_raw = (p.get("orders") or "").strip()
        single = (p.get("order") or "").strip()
        if not ids_raw and not single:
            return _v2_err("Incorrect order ID")
        wanted = [i.strip() for i in (ids_raw or single).split(",") if i.strip()][:100]
        # === PATCH_API_STATUS_SQL_V1 === قبلاً «همهٔ» سفارش‌هایِ ربات بارگذاری می‌شد؛ حالا فقط شناسه‌هایِ خواسته‌شده
        # (کدِ پیگیری هم پذیرفته می‌شود).
        _ints = [int(i) for i in wanted if i.isdigit()]
        _codes = [i.upper() for i in wanted if not i.isdigit()]
        async with Session() as s:
            from sqlalchemy import or_ as _or
            q = select(SmmOrder).where(SmmOrder.bot_id == ctx.bot_id)
            if not ctx.full_access:
                q = q.where(SmmOrder.user_telegram_id == ctx.user.telegram_id)
            _conds = []
            if _ints:
                _conds.append(SmmOrder.id.in_(_ints))
                _conds.append(SmmOrder.tracking_code.in_([str(i) for i in _ints]))
            if _codes:
                _conds.append(SmmOrder.tracking_code.in_(_codes))
            orders = (await s.scalars(q.where(_or(*_conds)))).all() if _conds else []
        by_id = {str(o.id): o for o in orders}
        for _o in orders:   # جست‌وجو با کدِ پیگیری هم کار کند
            if _o.tracking_code:
                by_id.setdefault(str(_o.tracking_code).upper(), _o)
                by_id.setdefault(str(_o.tracking_code), _o)
        if single and not ids_raw:
            o = by_id.get(single)
            if not o:
                return _v2_err("Incorrect order ID")
            return JSONResponse(await _v2_order_status_payload(o))
        out = {}
        for oid in wanted:
            o = by_id.get(oid) or by_id.get(oid.upper())
            out[oid] = (await _v2_order_status_payload(o)) if o else {"error": "Incorrect order ID"}
        return JSONResponse(out)

    # ─── refill / cancel ───
    if action in ("refill", "refill_status"):
        return _v2_err("Refill is not supported")
    if action == "cancel":
        return _v2_err("Cancel is not supported")

    return _v2_err("Incorrect action")


# ═══════════════ صفحهٔ مستنداتِ API ═══════════════
# === PATCH_API_PAGE_CLEAN_V1 ===
# درخواستِ ادمین (فدایی): صفحهٔ API شلوغ و نامعلوم بود؛ فقط «آدرسِ استاندارد + کلید» و یک لینکِ مستندات.
# این صفحهٔ ایستا دقیقاً همان اکشن‌هایی را شرح می‌دهد که smm_v2 واقعاً دارد
# (services / balance / add / status)؛ تست، هماهنگیِ این فهرست با کد را نگه می‌دارد.
# آدرسِ نمونه از هاستی ساخته می‌شود که کاربر با آن آمده (پشتِ nginx: X-Forwarded-*).

DOCS_ACTIONS = ("services", "balance", "add", "status")


def _docs_base(request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
    if not host or not all(c.isalnum() or c in ".-:" for c in host):
        return "https://YOUR-API-HOST"
    return f"{proto if proto in ('http', 'https') else 'https'}://{host}"


def render_docs_html(base: str) -> str:
    api = f"{base}/api/v2"
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>مستنداتِ API</title>
<style>
body{{font-family:Tahoma,Vazirmatn,system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;line-height:1.9;color:#1b1f23}}
code,pre{{direction:ltr;text-align:left;background:#f3f4f6;border-radius:6px;padding:2px 6px}}
pre{{display:block;padding:12px;overflow:auto}} h1,h2{{margin-top:1.6em}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #d0d7de;padding:6px 10px;text-align:right}} .note{{background:#fff8e1;border-radius:6px;padding:8px 12px}}
</style></head><body>
<h1>📘 مستنداتِ API</h1>
<p>این API با فرمتِ <b>استانداردِ پنل‌هایِ SMM</b> کار می‌کند؛ پس در هر ابزار یا پنلی که «API پنل» می‌پذیرد، فقط آدرس و کلید را وارد کن.</p>
<h2>اتصال</h2>
<p>آدرس: <code>{api}</code><br>روش: <code>POST</code> یا <code>GET</code> — پارامترها به‌صورتِ <code>form</code> یا query.<br>
کلید: پارامترِ <code>key</code> (کلیدی که داخلِ ربات، در صفحهٔ «API» می‌بینی).<br>
واحدِ پول: <b>تومان</b>. خطاها با کدِ HTTP ۲۰۰ و بدنهٔ <code>{{"error": "..."}}</code> برمی‌گردند.</p>
<h2>لیستِ سرویس‌ها — <code>action=services</code></h2>
<pre>curl -d "key=API_KEY&amp;action=services" {api}</pre>
<p>هر سرویس: <code>service</code> (شناسه)، <code>name</code>، <code>category</code>، <code>rate</code> (قیمتِ هر ۱۰۰۰ عدد به تومان)، <code>min</code>، <code>max</code>.</p>
<h2>موجودی — <code>action=balance</code></h2>
<pre>curl -d "key=API_KEY&amp;action=balance" {api}
{{"balance":"50000.0","currency":"TOMAN"}}</pre>
<h2>ثبتِ سفارش — <code>action=add</code></h2>
<table><tr><th>پارامتر</th><th>توضیح</th></tr>
<tr><td><code>service</code></td><td>شناسهٔ سرویس (از لیستِ سرویس‌ها)</td></tr>
<tr><td><code>link</code></td><td>لینکِ هدف</td></tr>
<tr><td><code>quantity</code></td><td>تعداد (بینِ min و max)</td></tr></table>
<pre>curl -d "key=API_KEY&amp;action=add&amp;service=12&amp;link=https://t.me/example&amp;quantity=1000" {api}
{{"order": 19098}}</pre>
<p>مبلغ از موجودیِ کیف پولت کم می‌شود. اگر موجودی کافی نباشد خطای <code>insufficient_balance</code> می‌گیری.</p>
<h2>وضعیتِ سفارش — <code>action=status</code></h2>
<pre>curl -d "key=API_KEY&amp;action=status&amp;order=19098" {api}
{{"charge":"1200.0","start_count":"0","status":"In progress","remains":"1000","currency":"TOMAN"}}</pre>
<p>چند سفارش یک‌جا: <code>orders=19098,19099</code> (حداکثر ۱۰۰).<br>
وضعیت‌ها: <code>Pending</code> · <code>In progress</code> · <code>Completed</code> · <code>Partial</code> · <code>Canceled</code> · <code>Refunded</code>.
در <code>Partial</code> مبلغِ بخشِ تحویل‌نشده خودکار به کیف پولت برمی‌گردد و <code>charge</code> مبلغِ نهایی است.</p>
<h2>پشتیبانی‌نشده</h2>
<p class="note">ریفیل (<code>refill</code>)، لغو (<code>cancel</code>) و دریپ‌فید از راهِ API فعال نیستند و پیامِ خطا می‌گیرند.
برایِ ریفیل از داخلِ ربات، بخشِ «سفارشاتِ گارانتی‌دار» را بزن.</p>
<h2>نمونهٔ پایتون</h2>
<pre>import requests
r = requests.post("{api}", data={{"key": "API_KEY", "action": "add",
                                  "service": 12, "link": "https://t.me/example", "quantity": 1000}})
print(r.json())</pre>
<p class="note">⚠️ کلید را با کسی به اشتراک نگذار. اگر لو رفت، از داخلِ ربات «تغییر کلید» را بزن؛ کلیدِ قبلی همان لحظه باطل می‌شود.</p>
</body></html>"""


@public_api_router.get("/api/docs")
async def api_docs(request: Request):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(render_docs_html(_docs_base(request)))

def render_vpn_docs_html(base: str) -> str:
    """مستنداتِ APIِ فروشِ VPN (جدا از مستنداتِ SMM تا هر ربات فقط مستنداتِ مربوطِ خودش را ببیند)."""
    api_vpn = f"{base}/api/vpn"
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>مستنداتِ API فروشِ VPN</title>
<style>
body{{font-family:Tahoma,Vazirmatn,system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;line-height:1.9;color:#1b1f23}}
code,pre{{direction:ltr;text-align:left;background:#f3f4f6;border-radius:6px;padding:2px 6px}}
pre{{display:block;padding:12px;overflow:auto}} h1,h2{{margin-top:1.6em}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #d0d7de;padding:6px 10px;text-align:right}} .note{{background:#fff8e1;border-radius:6px;padding:8px 12px}}
</style></head><body>
<h1>🛡 APIِ فروشِ VPN (فقط رباتِ فروش VPN)</h1>
<p>آدرس: <code>{api_vpn}</code> — پارامترهایِ <code>key</code> و <code>action</code> (GET یا POST: form / JSON).
پاسخ همیشه JSON است: موفق <code>{{"ok": true, ...}}</code>، خطا <code>{{"ok": false, "error": "کد"}}</code> (با HTTP 200).
با کلیدِ شخصی فقط به حسابِ خودت دسترسی داری و تخفیفِ اختصاصی‌ات خودکار اعمال می‌شود.
با کلیدِ کاملِ ربات برایِ هر عملیات باید <code>telegram_id</code> کاربر را هم بدهی.</p>
<table><tr><th>action</th><th>توضیح</th></tr>
<tr><td><code>plans</code></td><td>پلن‌هایِ قابلِ خرید (id، title، volume_gb، days، price)</td></tr>
<tr><td><code>balance</code></td><td>موجودیِ کیف پول (تومان)</td></tr>
<tr><td><code>buy</code></td><td>خریدِ پلن از کیف پول: <code>plan_id</code> (اختیاری: <code>ref</code> برایِ جلوگیری از خریدِ تکراری هنگامِ تکرارِ درخواست)</td></tr>
<tr><td><code>services</code></td><td>فهرستِ سرویس‌هایِ کاربر</td></tr>
<tr><td><code>status</code></td><td>وضعیتِ زنده: <code>service</code> = کدِ سرویس یا نام کاربریِ پنل</td></tr></table>
<pre>curl -d "key=API_KEY&amp;action=plans" {api_vpn}
curl -d "key=API_KEY&amp;action=buy&amp;plan_id=3&amp;ref=order-1001" {api_vpn}
{{"ok":true,"charged":150000,"balance":50000.0,"service":{{"code":"123456789","email":"...","sub_link":"https://..."}}}}
curl -d "key=API_KEY&amp;action=status&amp;service=123456789" {api_vpn}</pre>
<p>خطاهایِ رایج: <code>insufficient_balance</code> · <code>plan_not_found</code> · <code>invalid_api_key</code> ·
<code>panel_error</code> (پنل جواب نداد؛ مبلغ خودکار برگشت داده می‌شود) · <code>rate_limited</code>.</p>
<p class="note">تمدید از راهِ API هنوز فعال نیست؛ از داخلِ ربات انجام می‌شود.</p>
<p class="note">⚠️ کلید را با کسی به اشتراک نگذار. اگر لو رفت، از داخلِ ربات «تغییر کلید» را بزن؛ کلیدِ قبلی همان لحظه باطل می‌شود.</p>
</body></html>"""


@public_api_router.get("/api/docs/vpn")
async def api_docs_vpn(request: Request):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(render_vpn_docs_html(_docs_base(request)))



# === PATCH_VPN_API_V1 === مسیرهایِ APIِ رباتِ فروشِ VPN (/api/vpn، /api/v1/vpn/{action})
from tenant.shared import vpn_api as _vpn_api  # noqa: E402,F401
