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


# === PATCH_API_SVC_EXPORT_V1 ===
# • شمارهٔ مجازی (OzVinoo/TNumber «ozv:»، SMSPool «sp:») سرویسِ SMM نیست و از API فروخته نمی‌شود
#   (سفارشِ API برایش به پنلِ SMM با شناسهٔ بی‌معنی فرستاده می‌شد).
# • توضیحات: متنِ ادمین، وگرنه توضیحِ ارائه‌دهنده — قبلاً در v2 اصلاً فرستاده نمی‌شد.
# • دسته با نامِ پلتفرم («تلگرام - ممبر کانال») تا دسته‌هایِ هم‌نامِ پلتفرم‌هایِ مختلف
#   (تلگرام/روبیکا) در طرفِ گیرنده یکی نشوند.
_VNUM_CAT_MARKERS = ("شمارهٔ مجازی", "شماره مجازی", "شماره‌ی مجازی", "virtual number")


def _api_sellable(v: SmmService) -> bool:
    sid = str(v.service_id or "")
    if sid.startswith(("ozv:", "sp:")) or (v.type_key or "") == "virtual_number":
        return False
    # PATCH_API_NO_GENERAL_V1: شمارهٔ مجازیِ دستی (service_id=man…، دستهٔ متنیِ «شمارهٔ مجازیِ تلگرام»)
    cat = (v.category or "").lower()
    return not any(m in cat for m in _VNUM_CAT_MARKERS)


def _api_fallback_category(v: SmmService) -> tuple[str, str]:
    """=== PATCH_API_NO_GENERAL_V1 ===
    سرویسِ بی‌دسته قبلاً در API با دستهٔ «General» می‌رفت (همهٔ سرویس‌هایِ بی‌دسته یک‌جا، بی‌ربط به هم).
    حالا مثلِ فروشگاهِ ربات: «پلتفرم - نوع» از رویِ همان طبقه‌بندیِ ذخیره‌شده/تشخیصی."""
    try:
        from tenant.bots.virtual_service import smm_api as _sa
        from tenant.bots.virtual_service.smm import _svc_platform_type
        p, t = _svc_platform_type(v)
    except Exception:
        return "سایر", ""
    plat = _platform_label(p) if p != "other" else ""
    typ = ""
    if t and t not in ("other", "__all__"):
        typ = _sa.service_type_label(t).split(" ", 1)[-1].strip()
    if plat and typ:
        return f"{plat} - {typ}", p
    return (plat or typ or "سایر"), p


def _api_desc(v: SmmService) -> str:
    return ((v.description or "").strip() or (v.provider_desc or "").strip())[:2000]


_PLAT_LABELS: dict | None = None


def _platform_label(key: str | None) -> str:
    global _PLAT_LABELS
    if _PLAT_LABELS is None:
        try:
            from tenant.bots.virtual_service.smm_api import PLATFORMS
            _PLAT_LABELS = {k: lbl.split(" ", 1)[-1].strip() for k, lbl, *_ in PLATFORMS}
        except Exception:
            _PLAT_LABELS = {}
    return _PLAT_LABELS.get((key or "").strip(), "")


def _api_category(cat_name: str | None, platform_key: str | None) -> str:
    name = (cat_name or "").strip() or "General"
    plat = _platform_label(platform_key)
    if not plat or plat in name:
        return name
    return f"{plat} - {name}"


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
    if not cat_name:
        cat_name = _api_fallback_category(v)[0]   # PATCH_API_NO_GENERAL_V1
    return {
        "id": v.id,
        "name": v.name,
        "description": _api_desc(v) or None,
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


async def _load_cat_platforms(bot_id: int) -> dict:
    async with Session() as s:
        rows = (await s.execute(select(SmmCategory.id, SmmCategory.platform).where(
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
    items = [await _svc_payload(ctx.bot_id, v, ctx.discount_pct, cats) for v in svcs if _api_sellable(v)]
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
    if not v or v.bot_id != ctx.bot_id or not v.is_active or not _api_sellable(v):
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
        else:
            # === PATCH_MANUAL_ORDER_CHANNEL_V1 === سفارشِ دستیِ API هم به کانال/ادمین اعلام شود
            await _announce_manual_api_order(ctx.bot_id, order_pk)
    except Exception:
        logger.exception("API: ارسالِ خودکارِ سفارش %s به پنل ناموفق بود (سفارش ثبت شده است)", order_pk)
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk)
    # PATCH_API_ORDER_RECEIPT_V1
    try:
        from tenant.bots.virtual_service.smm import _svc_platform_type
        from tenant.bots.virtual_service.smm_api import platform_label as _pl
        _cat_plat = None
        if v.category_pk:
            async with Session() as s:
                _c = await s.get(SmmCategory, v.category_pk)
            _cat_plat = _c.platform if _c and _c.platform != "other" else None
        _pk = _cat_plat or _svc_platform_type(v)[0]
        _plat = "" if _pk == "other" else _pl(_pk).split(" ", 1)[-1].strip()
        _st = (o.status or "") if o else ""
        _tkey = "sent" if _st == "sent" else ("order_placed_manual" if _st == "awaiting_admin" else None)
        if _st in ("failed", "refunded", "canceled", "rejected"):
            raise _SkipReceipt   # پیامِ خطا/بازگشتِ وجه جدا از _notify_user می‌رود
        spawn_api_receipt(ctx.bot_id, target_tid, name=v.name, platform=_plat, qty=body.quantity,
                          price=price, link=body.link, order_id=order_pk,
                          tracking=(o.tracking_code if o else None), smm_order_pk=order_pk, text_key=_tkey,
                          unit_line=f"💸 قیمت هر 1000 عدد: <b>{round(price * 1000 / max(1, body.quantity)):,}</b> تومان")
    except _SkipReceipt:
        pass
    except Exception:
        logger.exception("API: آماده‌سازیِ رسیدِ سفارش ناموفق بود")
    return JSONResponse({"ok": True, "order": _order_payload(o)}, status_code=201)


# === PATCH_API_ORDER_RECEIPT_V1 ===
# درخواستِ فدایی: وقتی مشتری «🟢 اعلان سفارشات وبسرویس» را روشن کرده، با هر سفارشی که از
# وب‌سرویس ثبت می‌کند یک رسید (محصول، پلتفرم، تعداد، قیمت، لینک، کدِ سفارش، مبلغ، موجودیِ جدید)
# در ربات برایش بیاید. ارسال در پس‌زمینه است تا پاسخِ API کند نشود؛ خطایش هرگز سفارش را خراب نمی‌کند.
_RECEIPT_TASKS: set = set()


class _SkipReceipt(Exception):
    pass


async def _send_api_receipt(bot_id: int, tg_id: int, *, name: str, platform: str, qty: int,
                            price: int, link: str, order_id, unit_line: str,
                            tracking: str | None = None, smm_order_pk: int | None = None,
                            text_key: str | None = None) -> None:
    try:
        from tenant.bots.virtual_service.smm import api_notify_enabled
        if not await api_notify_enabled(bot_id, tg_id):
            return
        from html import escape as _esc
        from tenant.shared.trust_report import _tehran_now_parts
        async with Session() as s:
            row = await s.get(BotRow, bot_id)
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
        if not row or not row.token or not u:
            return
        text = None
        # PATCH_API_RECEIPT_CUSTOM_TEXT_V1 — متنِ «ثبت سفارش» که ادمین در «✍️ متن‌هایِ سفارش» گذاشته
        # (خودکار: «🚀 ثبت روی پنل»، دستی: «⏳ ثبت سفارش») مقدم است؛ رسیدِ پیش‌فرض فقط وقتی تنظیم نشده.
        if smm_order_pk and text_key:
            from tenant.bots.virtual_service.smm_texts import get_custom
            custom = await get_custom(bot_id, text_key)
            if custom:
                from tenant.bots.virtual_service.smm import _render_order_text
                text = "#وبسرویس\n" + await _render_order_text(bot_id, smm_order_pk, custom)
        if text is None:
            text = _default_receipt(name=name, platform=platform, qty=qty, price=price, link=link,
                                    order_id=order_id, unit_line=unit_line, tracking=tracking,
                                    balance=float(u.balance or 0))
        from aiogram import Bot as _AioBot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        tbot = _AioBot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            try:
                await tbot.send_message(tg_id, text, disable_web_page_preview=True)
            except Exception:
                # متنِ دلخواهِ ادمین با HTMLِ ناقص → همان متن بدونِ قالب
                import re as _re
                await tbot.send_message(tg_id, _re.sub(r"<[^>]+>", "", text),
                                        parse_mode=None, disable_web_page_preview=True)
        finally:
            await tbot.session.close()
    except Exception:
        logger.exception("API: ارسالِ رسیدِ سفارشِ وب‌سرویس ناموفق بود")


def _default_receipt(*, name, platform, qty, price, link, order_id, unit_line, tracking, balance) -> str:
    from html import escape as _esc
    from tenant.shared.trust_report import _tehran_now_parts
    jd, hm, sec = _tehran_now_parts()
    lines = ["🔗 #وبسرویس", "",
             f"📝 نام محصول: <b>{_esc(name or '—')}</b>"]
    if platform:
        lines.append(f"🈸 نوع اپلیکیشن: <b>{_esc(platform)}</b>")
    lines += [f"💎 تعداد سفارش: <b>{int(qty):,}</b>",
              unit_line,
              "🔗 آدرس انجام سفارش:", _esc(link or "—"),
              # PATCH_API_RECEIPT_CUSTOM_TEXT_V1 — «کد سفارش» همان کدِ پیگیریِ لیستِ سفارش‌هایِ ادمین؛
              # شناسه‌ای که API برگردانده (در پنلِ مشتری) جدا نوشته می‌شود.
              f"📊 کد سفارش: <code>{tracking or order_id}</code>"]
    if tracking and str(tracking) != str(order_id):
        lines.append(f"🆔 شناسهٔ وب‌سرویس: <code>{order_id}</code>")
    lines += [f"💰 مبلغ پرداختی: <b>{int(price):,}</b> تومان",
              f"📍 موجودی جدید: <b>{int(balance):,}</b> تومان",
              f"🕰 {hm}:{sec} - {jd}"]
    return "\n".join(lines)


def spawn_api_receipt(bot_id: int, tg_id: int, **kw) -> None:
    import asyncio
    try:
        t = asyncio.get_running_loop().create_task(_send_api_receipt(bot_id, tg_id, **kw))
    except RuntimeError:
        return
    _RECEIPT_TASKS.add(t)
    t.add_done_callback(_RECEIPT_TASKS.discard)


async def _announce_manual_api_order(bot_id: int, order_pk: int) -> None:
    from aiogram import Bot as _AioBot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from models import Bot as _BotRow
    from tenant.bots.virtual_service.smm import announce_manual_smm_order
    async with Session() as s:
        row = await s.get(_BotRow, bot_id)
    if not row or not row.token:
        return
    tbot = _AioBot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await announce_manual_smm_order(tbot, row, order_pk)
    finally:
        await tbot.session.close()


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
        _plats = await _load_cat_platforms(ctx.bot_id)
        for v in svcs:
            if not _api_sellable(v):
                continue
            d = await _svc_payload(ctx.bot_id, v, ctx.discount_pct, _cats)
            _desc = _api_desc(v)
            out.append({
                "service": v.id,
                "name": d["name"],
                "type": "Default",
                "category": _api_category(d["category"], _plats.get(v.category_pk) or v.platform_key
                                          or _api_fallback_category(v)[1]),
                "rate": str(d["price_per_1000"]),
                "min": str(v.min_qty or 1),
                "max": str(v.max_qty or 0),
                "refill": False,
                "cancel": False,
                # PATCH_API_SVC_EXPORT_V1 — هر دو نامِ رایج تا پنل‌هایِ مختلف بخوانند
                "desc": _desc,
                "description": _desc,
            })
        # === PATCH_API_SPECIAL_PRODUCTS_V1 === استارز/پرمیوم/ری‌اکشنِ استارزی/گیفت
        from tenant.shared import public_api_special as _sp
        out.extend(await _sp.list_services(ctx.bot_id, ctx.discount_pct))
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
        # === PATCH_API_SPECIAL_PRODUCTS_V1 ===
        from tenant.shared import public_api_special as _sp
        if _sp.is_special_service(service_id):
            if ctx.full_access:
                if not p.get("telegram_id"):
                    return _v2_err("telegram_id_required")
                try:
                    buyer_tid = int(p["telegram_id"])
                except ValueError:
                    return _v2_err("telegram_id_required")
                disc = 0.0
            else:
                buyer_tid, disc = ctx.user.telegram_id, ctx.discount_pct
            if quantity <= 0:
                return _v2_err("Incorrect service or quantity")
            try:
                oid = await _sp.place_order(
                    ctx.bot_id, buyer_tid, service_id, link, quantity, disc,
                    anonymous=str(p.get("anonymous") or "").strip().lower() in ("1", "true", "yes"))
            except _sp.SpecialOrderError as e:
                return _v2_err(str(e))
            except Exception:
                logger.exception("API: سفارشِ محصولِ ویژه ناموفق بود")
                return _v2_err("Order failed")
            try:   # PATCH_API_ORDER_RECEIPT_V1
                _ri = await _sp.receipt_info(oid, service_id)
                if _ri:
                    spawn_api_receipt(ctx.bot_id, buyer_tid, name=_ri[0], platform="تلگرام", qty=quantity,
                                      price=_ri[1], link=link, order_id=oid, tracking=_ri[2],
                                      unit_line=f"💸 قیمت هر عدد: <b>{round(_ri[1] / max(1, quantity)):,}</b> تومان")
            except Exception:
                logger.exception("API: آماده‌سازیِ رسیدِ سفارشِ ویژه ناموفق بود")
            return JSONResponse({"order": oid})
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
        # === PATCH_API_SPECIAL_PRODUCTS_V1 === شناسه‌هایِ ≥ ORDER_BASE مالِ استارز/پرمیوم/گیفت/ری‌اکشن‌اند
        from tenant.shared import public_api_special as _sp
        _special = {}
        for i in wanted:
            if i.isdigit() and _sp.is_special_order(int(i)):
                _st = await _sp.order_status(
                    ctx.bot_id, int(i), None if ctx.full_access else ctx.user.telegram_id)
                _special[i] = _st or {"error": "Incorrect order ID"}
        wanted_smm = [i for i in wanted if i not in _special]
        if single and not ids_raw and single in _special:
            _st = _special[single]
            return _v2_err("Incorrect order ID") if "error" in _st else JSONResponse(_st)
        _ints = [int(i) for i in wanted_smm if i.isdigit()]
        _codes = [i.upper() for i in wanted_smm if not i.isdigit()]
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
            if oid in _special:
                out[oid] = _special[oid]
                continue
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
<h2>استارز، پرمیوم، گیفت و ری‌اکشنِ استارزی</h2>
<p>این محصولات (اگر در ربات فعال باشند) با دسته‌هایِ «تلگرام - استارز / پرمیوم / گیفت / ری‌اکشن استارزی» در همان لیستِ سرویس‌ها می‌آیند و با همان <code>action=add</code> سفارش داده می‌شوند:</p>
<table><tr><th>محصول</th><th><code>link</code></th><th><code>quantity</code></th></tr>
<tr><td>استارز</td><td>یوزرنیمِ گیرنده (<code>@username</code>)</td><td>تعدادِ استارز</td></tr>
<tr><td>پرمیوم</td><td>یوزرنیمِ گیرنده</td><td>همیشه <code>1</code></td></tr>
<tr><td>گیفت</td><td>یوزرنیمِ گیرنده</td><td>تعدادِ گیفت</td></tr>
<tr><td>ری‌اکشنِ استارزی</td><td>لینکِ پستِ کانالِ عمومی (<code>https://t.me/channel/123</code>)</td><td>تعدادِ ری‌اکشن — با <code>anonymous=1</code> ناشناس ثبت می‌شود</td></tr></table>
<p>مبلغ = <code>rate × quantity ÷ 1000</code> (فیلدِ <code>price</code> قیمتِ یک عدد است). وضعیتِ این سفارش‌ها هم با <code>action=status</code> گرفته می‌شود.
شمارهٔ مجازی از راهِ API ارائه نمی‌شود.</p>
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
