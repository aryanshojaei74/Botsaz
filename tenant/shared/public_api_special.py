"""=== PATCH_API_SPECIAL_PRODUCTS_V1 ===
محصولاتِ «خدماتِ مجازی» که SmmService نیستند (استارز، پرمیوم، ری‌اکشنِ استارزی، گیفت)
در وب‌سرویسِ v2 هم عرضه می‌شوند — درخواستِ ادمینِ رباتِ خدماتِ مجازی.

• هر محصول فقط وقتی در API می‌آید که دکمه‌اش در صفحهٔ اصلیِ ربات باشد (همان معیارِ مینی‌اپ).
• شناسه‌ها در بازه‌ای جدا از SmmService.id هستند تا با سرویس‌هایِ SMM قاطی نشوند.
• قیمت‌ها از همان توابعِ ربات می‌آیند و قراردادِ SMM حفظ می‌شود: charge = rate × quantity / 1000
  (برایِ پرمیوم/گیفت که «تکی»اند، rate قیمتِ هزار عدد است؛ فیلدِ price قیمتِ یک عدد را هم می‌دهد).
• سفارش‌ها همان GiftOrderهایِ ربات‌اند، با همان دکمه‌هایِ ادمین/کانالِ سفارش و TGToolsِ خودکار؛
  شناسهٔ سفارشِ برگشتی = ORDER_BASE + GiftOrder.id.
• شمارهٔ مجازی عمداً اینجا نیست (دریافتِ کد از طریقِ وب‌سرویسِ SMM ممکن نیست).
"""
from __future__ import annotations

import re

from sqlalchemy import select

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, GiftOrder, MainButton, TelegramGiftConfig, WalletTxn

logger = _get_logger(__name__)

STARS_ID = 900_000_001
REACTION_ID = 900_000_002
PREMIUM_BASE = 900_000_100          # + ماه (3/6/12)
GIFT_BASE = 910_000_000             # + TelegramGiftConfig.id
ORDER_BASE = 1_000_000_000          # + GiftOrder.id

_GIFT_MAX_QTY = 20
_REACTION_MAX_QTY = 100_000
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_POST_RE = re.compile(r"^https?://t\.me/([A-Za-z0-9_]{4,32})/(\d+)/?$")

_V2_STATUS = {
    "pending": "In progress",
    "sending": "In progress",
    "auto_processing": "In progress",
    "awaiting_stars": "In progress",
    "awaiting_admin": "Pending",
    "sent": "Completed",
    "completed": "Completed",
    "partial": "Partial",
    "failed": "Canceled",
    "rejected": "Canceled",
    "refunded": "Canceled",
}


class SpecialOrderError(Exception):
    """پیامِ خطا در قالبِ {"error": ...} به مشتریِ API برمی‌گردد."""


def is_special_service(service_id: int) -> bool:
    return 900_000_000 <= service_id < ORDER_BASE


def is_special_order(order_id: int) -> bool:
    return order_id > ORDER_BASE


async def _enabled(bot_id: int, action: str) -> bool:
    async with Session() as s:
        return bool(await s.scalar(select(MainButton.id).where(
            MainButton.bot_id == bot_id, MainButton.action == action).limit(1)))


def _disc(price: float, discount_pct: float) -> int:
    p = round(price)
    if discount_pct:
        p = round(p * (100 - discount_pct) / 100)
    return int(p)


def _svc(sid: int, name: str, category: str, unit_price: int, qmin: int, qmax: int,
         desc: str, link_hint: str) -> dict:
    return {
        "service": sid,
        "name": name,
        "type": "Default",
        "category": category,
        "rate": str(unit_price * 1000),
        "price": str(unit_price),
        "min": str(qmin),
        "max": str(qmax),
        "refill": False,
        "cancel": False,
        "desc": desc,
        "description": desc,
        "link": link_hint,
    }


async def list_services(bot_id: int, discount_pct: float = 0.0) -> list[dict]:
    from tenant.bots.virtual_service import smm as _smm
    out: list[dict] = []
    uname_hint = "@username"
    try:
        if await _enabled(bot_id, "stars_shop"):
            rate, qmin, qmax = await _smm._stars_sell_settings(bot_id)
            unit = _disc(rate, discount_pct)
            if unit > 0:
                out.append(_svc(STARS_ID, "⭐ استارزِ تلگرام", "تلگرام - استارز", unit, qmin, qmax,
                                "ارسالِ استارز به اکانتِ تلگرام. link = یوزرنیمِ گیرنده (@username).",
                                uname_hint))
    except Exception:
        logger.exception("API: فهرستِ استارز")
    try:
        if await _enabled(bot_id, "star_reaction_shop"):
            rate, qmin = await _smm._srx_settings(bot_id)
            unit = _disc(rate, discount_pct)
            if unit > 0:
                out.append(_svc(REACTION_ID, "⭐️ ری‌اکشنِ استارزیِ پست", "تلگرام - ری‌اکشن استارزی",
                                unit, qmin, _REACTION_MAX_QTY,
                                "ری‌اکشنِ ⭐️ رویِ پستِ کانالِ عمومی. link = لینکِ پست "
                                "(https://t.me/channel/123). پارامترِ اختیاریِ anonymous=1 برایِ ثبتِ ناشناس.",
                                "https://t.me/channel/123"))
    except Exception:
        logger.exception("API: فهرستِ ری‌اکشنِ استارزی")
    try:
        if await _enabled(bot_id, "premium_shop"):
            rate, margin = await _smm._premium_settings(bot_id)
            for months, stars in _smm._PREMIUM_TIERS:
                price, _ = await _smm._premium_effective_price(bot_id, months, stars, rate, margin)
                unit = _disc(price, discount_pct)
                if unit > 0:
                    out.append(_svc(PREMIUM_BASE + months, f"🌟 پرمیومِ تلگرام {months} ماهه",
                                    "تلگرام - پرمیوم", unit, 1, 1,
                                    f"فعال‌سازیِ پرمیومِ {months} ماهه رویِ اکانتِ گیرنده. "
                                    "link = یوزرنیمِ گیرنده (@username)، quantity = 1.",
                                    uname_hint))
    except Exception:
        logger.exception("API: فهرستِ پرمیوم")
    try:
        if await _enabled(bot_id, "gift_shop"):
            async with Session() as s:
                gifts = (await s.scalars(select(TelegramGiftConfig).where(
                    TelegramGiftConfig.bot_id == bot_id,
                    TelegramGiftConfig.active == True,   # noqa: E712
                    TelegramGiftConfig.is_manual == False,   # noqa: E712  گیفتِ NFT/سفارشی تک‌نسخه است
                ).order_by(TelegramGiftConfig.star_count))).all()
            for g in gifts:
                price, _label = await _smm._gift_price_and_label(g, bot_id)
                unit = _disc(price, discount_pct)
                if unit > 0:
                    out.append(_svc(GIFT_BASE + g.id, f"🎁 گیفتِ {g.emoji or ''} ({g.star_count} استارز)".replace("  ", " "),
                                    "تلگرام - گیفت", unit, 1, _GIFT_MAX_QTY,
                                    "ارسالِ گیفتِ تلگرام به گیرنده. link = یوزرنیمِ گیرنده (@username)، "
                                    f"quantity = تعدادِ گیفت (۱ تا {_GIFT_MAX_QTY}).",
                                    uname_hint))
    except Exception:
        logger.exception("API: فهرستِ گیفت")
    return out


def _parse_username(link: str) -> str | None:
    raw = (link or "").strip()
    raw = re.sub(r"^(https?://)?(t|telegram)\.me/", "", raw, flags=re.I).strip("/").lstrip("@")
    return raw if _USERNAME_RE.match(raw) else None


async def _resolve_dest(bot_id: int, link: str) -> tuple[int, str, str]:
    """(dest_telegram_id یا 0، برچسبِ مقصد، یوزرنیم) — مقصد باید یوزرنیم یا آیدیِ عددی باشد."""
    from tenant.bots.virtual_service.smm import _resolve_dest_label
    raw = (link or "").strip()
    if raw.isdigit() and 5 <= len(raw) <= 15:
        dest_id = int(raw)
        return dest_id, await _resolve_dest_label(bot_id, dest_id), ""
    uname = _parse_username(raw)
    if not uname:
        raise SpecialOrderError("Incorrect link: send the recipient's @username")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.username.ilike(uname)).limit(1))
    if u:
        label = await _resolve_dest_label(bot_id, u.telegram_id)
        if f"@{uname}".lower() not in label.lower():
            label += f"\n👤 یوزرنیم: @{uname}"
        return u.telegram_id, label, uname
    return 0, (f"👤 یوزرنیم: @{uname}\n"
               "⚠️ این شخص ربات را استارت نکرده — ارسال دستی است."), uname


async def _debit_and_create(bot_id: int, buyer_tid: int, price: int, note: str, **order_kw) -> tuple[int, str]:
    from models import alloc_tracking_in as _alloc_in
    async with Session() as s:
        b = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tid).with_for_update())
        if not b:
            raise SpecialOrderError("user_not_found")
        if float(b.balance) < price:
            raise SpecialOrderError("Not enough funds on balance")
        b.balance = float(b.balance) - price
        s.add(WalletTxn(bot_id=bot_id, user_id=b.id, amount=-price, kind="purchase",
                        note=f"{note} (API)"))
        trk = await _alloc_in(s, bot_id)
        o = GiftOrder(bot_id=bot_id, buyer_user_id=b.id, tracking_code=trk,
                      pay_method="wallet", **order_kw)
        s.add(o)
        await s.commit()
        return o.id, trk


def _spawn_and_close(tbot, coro) -> None:
    """کارِ خودکارِ TGTools در پس‌زمینه؛ نشستِ HTTPِ ربات بعد از پایانش بسته می‌شود."""
    import asyncio
    from tenant.bots.virtual_service import smm as _smm

    async def _run():
        try:
            await coro
        except Exception:
            logger.exception("API: خریدِ خودکارِ TGTools")
        finally:
            try:
                await tbot.session.close()
            except Exception:
                logger.debug("API: بستنِ نشستِ ربات بعد از TGTools ممکن نشد", exc_info=True)
    t = asyncio.create_task(_run())
    _smm._tgt_tasks.add(t)
    t.add_done_callback(_smm._tgt_tasks.discard)


def _tbot(row):
    from aiogram import Bot as _AioBot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    return _AioBot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def place_order(bot_id: int, buyer_tid: int, service_id: int, link: str, quantity: int,
                      discount_pct: float = 0.0, anonymous: bool = False) -> int:
    """سفارشِ API برایِ محصولاتِ ویژه؛ خروجی: شناسهٔ سفارشِ API (ORDER_BASE + GiftOrder.id)."""
    from models import Bot as _BotRow, order_no
    from tenant.bots.virtual_service import smm as _smm
    async with Session() as s:
        row = await s.get(_BotRow, bot_id)
    if not row or not row.token:
        raise SpecialOrderError("Service unavailable")

    # ─── استارز: همان place_stars_order رباتِ (کسر/ثبت/کانال/TGTools) ───
    if service_id == STARS_ID:
        if not await _enabled(bot_id, "stars_shop"):
            raise SpecialOrderError("Incorrect service ID")
        rate, qmin, qmax = await _smm._stars_sell_settings(bot_id)
        if quantity < qmin or quantity > qmax:
            raise SpecialOrderError(f"Quantity must be between {qmin} and {qmax}")
        dest_id, _label, uname = await _resolve_dest(bot_id, link)
        price = _disc(quantity * rate, discount_pct)
        async with Session() as s:
            buyer = await s.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tid))
        if not buyer:
            raise SpecialOrderError("user_not_found")
        tbot = _tbot(row)
        try:
            res = await _smm.place_stars_order(
                tbot, row, buyer_tg_id=buyer_tid, buyer_name=buyer.full_name or "",
                buyer_username=buyer.username or "", dest_id=dest_id, qty=quantity,
                dest_username=uname if dest_id == 0 else "", price_override=price)
        except _smm.StarsOrderError as e:
            raise SpecialOrderError("Not enough funds on balance" if "موجودی" in str(e) else str(e))
        finally:
            await tbot.session.close()
        return ORDER_BASE + int(res["order_id"])

    # ─── ری‌اکشنِ استارزی ───
    if service_id == REACTION_ID:
        if not await _enabled(bot_id, "star_reaction_shop"):
            raise SpecialOrderError("Incorrect service ID")
        rate, qmin = await _smm._srx_settings(bot_id)
        if quantity < qmin or quantity > _REACTION_MAX_QTY:
            raise SpecialOrderError(f"Quantity must be between {qmin} and {_REACTION_MAX_QTY}")
        post = (link or "").strip()
        if not _POST_RE.match(post):
            raise SpecialOrderError("Incorrect link: send a public post link like https://t.me/channel/123")
        price = _disc(quantity * rate, discount_pct)
        oid, trk = await _debit_and_create(
            bot_id, buyer_tid, price, f"خریدِ {quantity:,} ری‌اکشنِ استارزی",
            gift_id=f"srx_api_{quantity}"[:40], gift_emoji="⭐️", star_count=quantity,
            price_toman=price, quantity=1, dest_telegram_id=0, dest_label=post[:120],
            status="awaiting_admin", anonymous=bool(anonymous))
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ انجام شد", callback_data=f"srx:ok:{oid}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"srx:no:{oid}"),
        ]])
        tbot = _tbot(row)
        try:
            await _smm._notify_gift_order_trust_success(tbot, row, oid)
            await _smm._notify_manual_order(
                tbot, row,
                f"⭐️ سفارشِ ری‌اکشنِ استارزی (وب‌سرویس) — #{await order_no(GiftOrder, oid)}\n"
                f"تعداد: {quantity:,} ری‌اکشن\n💰 {price:,} تومان\n🔗 پست: {post}\n"
                + ("🕶 ثبت: ناشناس\n" if anonymous else "") +
                f"🔖 کد: {trk}\n\nباید خودت دستی ری‌اکشن‌ها را رویِ این پست ثبت کنی، بعد «✅ انجام شد» را بزن.",
                kb)
        except Exception:
            logger.exception("API: اطلاعِ سفارشِ ری‌اکشن %s", oid)
        finally:
            await tbot.session.close()
        return ORDER_BASE + oid

    # ─── پرمیوم ───
    months = service_id - PREMIUM_BASE
    tiers = dict(_smm._PREMIUM_TIERS)
    if months in tiers:
        if not await _enabled(bot_id, "premium_shop"):
            raise SpecialOrderError("Incorrect service ID")
        if quantity != 1:
            raise SpecialOrderError("Quantity must be 1")
        stars = tiers[months]
        rate, margin = await _smm._premium_settings(bot_id)
        base, _ = await _smm._premium_effective_price(bot_id, months, stars, rate, margin)
        price = _disc(base, discount_pct)
        dest_id, label, _uname = await _resolve_dest(bot_id, link)
        oid, trk = await _debit_and_create(
            bot_id, buyer_tid, price, f"خریدِ پرمیومِ تلگرام ({months} ماهه)",
            gift_id=f"premium_{months}", gift_emoji="🌟", star_count=stars, price_toman=price,
            dest_telegram_id=dest_id, dest_label=label[:120], status="awaiting_admin")
        tbot = _tbot(row)
        try:
            auto = await _smm._tgt_premium_on(bot_id)
            await _smm._notify_manual_order(
                tbot, row,
                f"🌟 سفارشِ پرمیوم (وب‌سرویس{'، خریدِ خودکار با TGTools' if auto else ''}) — "
                f"#{await order_no(GiftOrder, oid)}\n"
                f"پلن: {months} ماهه ({stars:,} استارز)\n💰 {price:,} تومان\n🎯 مقصد: {label}\n🔖 کد: {trk}\n\n"
                + ("خریدِ خودکار شروع شد؛ نتیجه با پیامِ جداگانه می‌آید."
                   if auto else "این پرمیوم را خودت هدیه بده، بعد «✅ انجام شد» را بزن."),
                await _smm._prm_admin_kb(oid))
            if auto:
                _spawn_and_close(tbot, _smm.tgtools_fulfil_premium(tbot, row, oid))
                tbot = None   # کارِ پس‌زمینه از همین نمونه استفاده می‌کند
        except Exception:
            logger.exception("API: اطلاعِ سفارشِ پرمیوم %s", oid)
        finally:
            if tbot is not None:
                await tbot.session.close()
        return ORDER_BASE + oid

    # ─── گیفت ───
    if service_id > GIFT_BASE:
        if not await _enabled(bot_id, "gift_shop"):
            raise SpecialOrderError("Incorrect service ID")
        async with Session() as s:
            g = await s.get(TelegramGiftConfig, service_id - GIFT_BASE)
        if not g or g.bot_id != bot_id or not g.active or g.is_manual:
            raise SpecialOrderError("Incorrect service ID")
        if quantity < 1 or quantity > _GIFT_MAX_QTY:
            raise SpecialOrderError(f"Quantity must be between 1 and {_GIFT_MAX_QTY}")
        unit, item_label = await _smm._gift_price_and_label(g, bot_id)
        unit = _disc(unit, discount_pct)
        price = unit * quantity
        dest_id, label, _uname = await _resolve_dest(bot_id, link)
        # مقصدِ ناشناخته (استارت‌نکرده) فقط دستی تحویل می‌شود؛ مقصدِ شناخته با «✅» ادمین
        # (یا خودکار با TGTools) همان مسیرِ ربات را می‌رود.
        oid, trk = await _debit_and_create(
            bot_id, buyer_tid, price, f"خریدِ {item_label} × {quantity}",
            gift_id=g.gift_id, gift_emoji=g.emoji, star_count=g.star_count, price_toman=unit,
            quantity=quantity, dest_telegram_id=dest_id, dest_label=label[:120],
            status="awaiting_admin", deliver_via=("support" if dest_id == 0 else ""))
        tbot = _tbot(row)
        try:
            auto = await _smm._tgt_gift_on(bot_id)
            await _smm._notify_manual_order(
                tbot, row,
                f"🎁 سفارشِ گیفت (وب‌سرویس) — #{await order_no(GiftOrder, oid)}\n"
                f"{item_label}" + (f"\n🔢 تعداد: {quantity}" if quantity > 1 else "") +
                f"\n💰 {price:,} تومان\n🎯 مقصد: {label}\n🔖 کد: {trk}\n\n"
                + ("خریدِ خودکار با TGTools شروع شد؛ نتیجه با پیامِ جداگانه می‌آید."
                   if auto else "گیفت را بفرست و «✅ انجام شد» را بزن."),
                await _smm._gift_admin_kb(bot_id, oid, g.gift_id))
            if auto:
                _spawn_and_close(tbot, _smm.tgtools_fulfil_gift(tbot, row, oid))
                tbot = None
        except Exception:
            logger.exception("API: اطلاعِ سفارشِ گیفت %s", oid)
        finally:
            if tbot is not None:
                await tbot.session.close()
        return ORDER_BASE + oid

    raise SpecialOrderError("Incorrect service ID")


async def order_status(bot_id: int, api_order_id: int, buyer_tid: int | None) -> dict | None:
    """وضعیت در قالبِ SMM؛ None یعنی «سفارش پیدا نشد»."""
    async with Session() as s:
        o = await s.get(GiftOrder, api_order_id - ORDER_BASE)
        if not o or o.bot_id != bot_id:
            return None
        if buyer_tid is not None:
            b = await s.get(BotUser, o.buyer_user_id)
            if not b or b.telegram_id != buyer_tid:
                return None
    qty = int(o.quantity or 1)
    total = float(o.price_toman or 0) * (qty if not str(o.gift_id or "").startswith(
        ("premium_", "starsell_", "srx_")) else 1)
    status = _V2_STATUS.get(o.status or "", "Pending")
    done = status == "Completed"
    return {
        "charge": str(total),
        "start_count": "0",
        "status": status,
        "remains": "0" if done else str(o.star_count if str(o.gift_id or "").startswith(
            ("starsell_", "srx_")) else qty),
        "currency": "TOMAN",
    }


async def receipt_info(api_order_id: int, service_id: int) -> tuple[str, int, str | None] | None:
    """PATCH_API_ORDER_RECEIPT_V1 — (نامِ محصول، مبلغِ پرداختی) برایِ رسیدِ سفارشِ وب‌سرویس."""
    async with Session() as s:
        o = await s.get(GiftOrder, api_order_id - ORDER_BASE)
    if not o:
        return None
    gid = str(o.gift_id or "")
    if service_id == STARS_ID:
        name = "⭐ استارزِ تلگرام"
    elif service_id == REACTION_ID:
        name = "⭐️ ری‌اکشنِ استارزیِ پست"
    elif gid.startswith("premium_"):
        name = f"🌟 پرمیومِ تلگرام {gid.split('_')[-1]} ماهه"
    else:
        name = f"🎁 گیفتِ {o.gift_emoji or ''} ({o.star_count} استارز)".replace("  ", " ")
    total = int(o.price_toman or 0)
    if not gid.startswith(("premium_", "starsell_", "srx_")):
        total *= int(o.quantity or 1)
    return name, total, o.tracking_code
