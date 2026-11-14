"""
nft_tgtools.py — خریدِ خودکارِ NFT از TGTools برایِ سفارش‌هایِ مارکتِ NFT.  PATCH_NFT_TGTOOLS_V1

جریان: مشتری در مینی‌اپ آدرسِ کیف‌پولِ TONِ خودش را می‌دهد ← پول از کیف‌پولِ ربات کسر می‌شود ←
(اینجا) ربات از TGTools «قیمتِ نهایی» را می‌گیرد، نگهبانِ ضرر را چک می‌کند، و با
POST /api/marketplace/buy/auto می‌خرد؛ NFT مستقیم به آدرسِ مشتری می‌رود. وضعیتِ سفارش با
GET /api/marketplace/orders/{id} پایش می‌شود: Complete ← تحویل‌شده؛ شکست ← عودتِ وجه به مشتری.

ایمنی (پایِ پولِ واقعی):
  • clientOrderId ثابت (b{bot}-n{order}) ⇒ تکرارِ درخواست سفارشِ دوم نمی‌سازد.
  • خریدِ POST هیچ‌وقت خودکار تکرار نمی‌شود؛ اگر جوابِ قطعی نگرفتیم، سفارش «نامشخص» علامت می‌خورد و
    فقط ادمین تصمیم می‌گیرد.
  • اگر قیمتِ نهایی (تومان) از مبلغی که مشتری پرداخته بیشتر شد، نمی‌خریم (ضرر نکنیم) ⇒ دستی/عودت.
  • هر شکستی فقط به مسیرِ دستیِ قبلی برمی‌گردد؛ سفارش گم نمی‌شود.
"""
from __future__ import annotations

import datetime as _dt
import re

from sqlalchemy import select

from config import Session
from models import NftItem, NftMarketSource, NftOrder

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_ADDR_RE = re.compile(r"^(?:(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_\-]{46}|-?\d+:[0-9a-fA-F]{64})$")
_OK_WORDS = ("complete", "success", "done", "deliver")
_FAIL_WORDS = ("fail", "refund", "cancel", "reject", "expire", "error")
_STALE_HOURS = 3


def valid_ton_address(addr: str | None) -> bool:
    return bool(addr and _ADDR_RE.match(addr.strip()))


def norm_status(st) -> str:
    s = str(st or "").lower()
    if any(w in s for w in _FAIL_WORDS):
        return "failed"
    if any(w in s for w in _OK_WORDS):
        return "done"
    return "pending"


async def is_tgtools_item(item) -> bool:
    if not item or not item.source_id:
        return False
    async with Session() as s:
        src = await s.get(NftMarketSource, item.source_id)
    return bool(src and src.provider == "tgtools")


async def tgtools_source_ids(bot_id: int) -> frozenset:
    async with Session() as s:
        rows = (await s.scalars(select(NftMarketSource.id).where(
            NftMarketSource.bot_id == bot_id, NftMarketSource.provider == "tgtools"))).all()
    return frozenset(rows)


async def _key(bot_id: int, src) -> str:
    from tenant.bots.virtual_service.nft_sources import _auth_for
    return await _auth_for(src)


async def _note(order_id: int, text: str, source_order_id: str | None = None) -> None:
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if o:
            o.transfer_note = text[:400]
            if source_order_id is not None:
                o.source_order_id = source_order_id
            await s.commit()


async def try_tgtools_buy(bot_id: int, order_id: int) -> str | None:
    """"queued" اگر خرید ثبت شد؛ None یعنی مسیرِ خودکار نشد (ادمین دستی ادامه می‌دهد)."""
    from tenant.bots.virtual_service.tgtools_api import _request, TgToolsError, TgToolsAmbiguous
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if not o or o.bot_id != bot_id or o.kind != "buy" or o.status != "pending" or o.source_order_id:
            return None
        it = await s.get(NftItem, o.item_id)
        src = await s.get(NftMarketSource, it.source_id) if it and it.source_id else None
        if not (src and src.provider == "tgtools"):
            return None
        addr, paid, fee, nft = (o.recipient_ton or "").strip(), int(float(o.amount or 0)), \
            int(float(o.fee or 0)), (it.source_ref or "")
    if not valid_ton_address(addr) or not nft:
        await _note(order_id, "آدرسِ TONِ مشتری یا شناسهٔ NFT معتبر نیست؛ تحویلِ دستی.")
        return None
    key = await _key(bot_id, src)
    if not key:
        await _note(order_id, "کلیدِ TGTools ثبت نشده؛ تحویلِ دستی.")
        return None

    try:
        q = await _request(key, "GET", "/api/marketplace/quote", params={"address": nft})
    except Exception as e:                                   # noqa: BLE001
        await _note(order_id, f"استعلامِ قیمت از TGTools ناموفق: {str(e)[:200]}")
        return None
    if not isinstance(q, dict) or q.get("canBuy") is False:
        await _hide_item(it.id)
        await _note(order_id, "این NFT دیگر در TGTools قابلِ خرید نیست.")
        return None
    from tenant.bots.virtual_service.nft_market import _ton_rate
    rate = await _ton_rate(bot_id)
    total_ton = float(q.get("totalPriceTon") or 0)
    if rate <= 0 or total_ton <= 0 or int(round(total_ton * rate)) > paid - fee:
        await _note(order_id, f"قیمتِ نهاییِ مبدأ ({total_ton:g} TON) از مبلغِ پرداختیِ مشتری بیشتر شد؛ "
                              "برایِ جلوگیری از ضرر خریدِ خودکار انجام نشد.")
        return None

    try:
        r = await _request(key, "POST", "/api/marketplace/buy/auto", json_body={
            "nftAddress": nft, "recipientTonAddress": addr,
            "clientOrderId": f"b{bot_id}-n{order_id}"}, sent_is_ambiguous=True)
    except TgToolsAmbiguous:
        await _note(order_id, "⚠️ جوابِ قطعی از TGTools نرسید؛ ممکن است خرید انجام شده باشد. "
                              "قبل از هر اقدام از پنلِ TGTools بررسی کن.", source_order_id="?")
        return None
    except TgToolsError as e:
        await _note(order_id, f"TGTools خرید را رد کرد: {e.message[:200]}")
        return None
    oid = str((r or {}).get("orderId") or "")
    if not oid:
        await _note(order_id, "پاسخِ TGTools شناسهٔ سفارش نداشت؛ از پنلِ TGTools بررسی کن.", source_order_id="?")
        return None
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if o:
            o.source_order_id, o.transfer_mode = oid, "auto"
            o.transfer_note = f"خریدِ خودکار از TGTools ثبت شد (سفارشِ مبدأ #{oid})."
            await s.commit()
    return "queued"


async def _hide_item(item_id: int) -> None:
    async with Session() as s:
        it = await s.get(NftItem, item_id)
        if it and it.status in ("available", "reserved"):
            it.status = "hidden"
            await s.commit()


async def poll_pending(bot_id: int | None = None) -> int:
    """وضعیتِ سفارش‌هایِ در انتظارِ TGTools را می‌خواند و تحویل/عودت می‌دهد. خروجی: تعدادِ تغییرِ وضعیت."""
    from tenant.bots.virtual_service.tgtools_api import _request
    from tenant.bots.virtual_service.nft_market import apply_order_action
    async with Session() as s:
        q = select(NftOrder).where(NftOrder.status == "pending", NftOrder.source_order_id.is_not(None),
                                   NftOrder.source_order_id != "?")
        if bot_id is not None:
            q = q.where(NftOrder.bot_id == bot_id)
        orders = [(o.id, o.bot_id, o.item_id, o.source_order_id, o.created_at)
                  for o in (await s.scalars(q.limit(100))).all()]
    changed = 0
    for oid, bid, item_id, soid, created in orders:
        try:
            async with Session() as s:
                it = await s.get(NftItem, item_id)
                src = await s.get(NftMarketSource, it.source_id) if it and it.source_id else None
            if not src:
                continue
            key = await _key(bid, src)
            r = await _request(key, "GET", f"/api/marketplace/orders/{soid}")
            st = norm_status((r or {}).get("status"))
        except Exception as e:                               # noqa: BLE001
            logger.info("nft_tgtools: پایشِ سفارشِ %s ناموفق (%s)", soid, type(e).__name__)
            continue
        if st == "done":
            ok, _ = await apply_order_action(bid, oid, "delivered")
            changed += 1 if ok else 0
        elif st == "failed":
            await _hide_item(item_id)
            ok, _ = await apply_order_action(bid, oid, "reject")
            await _note(oid, f"خریدِ TGTools ناموفق شد ({(r or {}).get('status')}); وجه به مشتری برگشت.")
            changed += 1 if ok else 0
        elif created and _dt.datetime.utcnow() - created > _dt.timedelta(hours=_STALE_HOURS):
            await _note(oid, f"بیش از {_STALE_HOURS} ساعت در وضعیتِ «{(r or {}).get('status')}» مانده؛ "
                             "از پنلِ TGTools پیگیری کن.")
    return changed


async def nft_sources_loop():
    """پایشِ سفارش‌هایِ TGTools هر ۲ دقیقه + همگام‌سازیِ دوره‌ایِ همهٔ منابعِ مارکت (هر ≥۶۰ دقیقه برایِ هر منبع).
    (sync_all_due تا قبل از این هیچ‌جا زمان‌بندی نشده بود؛ فقط دکمهٔ دستیِ «همگام‌سازی» کار می‌کرد.)"""
    import asyncio
    await asyncio.sleep(120)
    tick = 0
    while True:
        try:
            await poll_pending()
            if tick % 15 == 0:
                from tenant.bots.virtual_service.nft_sources import sync_all_due
                await sync_all_due()
        except Exception:
            logger.exception("nft_sources_loop error")
        tick += 1
        await asyncio.sleep(120)
