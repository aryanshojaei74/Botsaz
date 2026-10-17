"""
nft_admin.py — سمتِ رباتِ «مینی‌اپ ۲ / مارکتِ NFT».

=== PATCH_NFT_MARKET_V1 ===

سه کارِ این ماژول:
  ۱) گرفتنِ اتصالِ «حسابِ کاری» (Business) ادمین و نگه‌داشتنش در NftBizConn —
     این تنها راهِ مجازِ تلگرام برایِ انتقالِ خودکارِ گیفتِ NFTـه؛ ربات
     نمی‌تواند گیفتِ یک کاربرِ عادی را منتقل کند.
  ۲) تلاش برایِ انتقالِ خودکار (transferGift) و در صورتِ شکست، برگرداندنِ
     سفارش به مسیرِ دستی به‌جایِ خطا دادن.
  ۳) اطلاع‌رسانی به ادمین و خریدار + دکمه‌هایِ اینلاینِ «تحویل شد / لغو و
     عودت / برگشت خورد»، معادلِ همان دکمه‌هایِ داخلِ مینی‌اپ.

پیشوندِ callback: nftm:  (در callback-map آزاد بود)
"""
from __future__ import annotations
from html import escape as _esc_html

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import (
    BusinessConnection, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
)
from sqlalchemy import select

from config import Session
from models import (Bot as BotRow, BotUser, NftBizConn, NftItem, NftOrder,
                    NftMarketSource)
from plans import Feature, has_feature

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

nft_admin_router = Router()


def _rial(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


async def _bot_row(bot_id: int):
    async with Session() as s:
        return await s.get(BotRow, bot_id)


def _tenant_bot(token: str) -> Bot:
    """آبجکتِ Bot برایِ ارسالِ پیام از مسیرهایِ غیرِ هندلر (مینی‌اپ/زمان‌بند).

    همان الگویِ tenant/abangateway.py؛ صدا زننده موظف است session را ببندد.
    """
    return Bot(token=token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


# ───────────────────── اتصالِ حسابِ کاری (Business) ─────────────────────

@nft_admin_router.business_connection()
async def on_business_connection(conn: BusinessConnection, bot: Bot):
    """ادمین اکانتش را به ربات وصل/قطع کرد.

    فقط اتصالِ خودِ مالکِ ربات پذیرفته می‌شود؛ اگر کاربرِ دیگری رباتِ
    مستأجر را به حسابِ کاریِ خودش وصل کند نباید بتواند موجودیِ مارکت را
    از اکانتِ خودش منتقل کند.
    """
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or int(row.owner_id) != int(conn.user.id):
        return
    bot_id = row.id

    rights = getattr(conn, "rights", None)
    can_transfer = bool(getattr(rights, "can_transfer_and_upgrade_gifts", False))

    async with Session() as s:
        rec = await s.scalar(select(NftBizConn).where(
            NftBizConn.bot_id == bot_id, NftBizConn.connection_id == conn.id))
        if not rec:
            rec = NftBizConn(bot_id=bot_id, connection_id=conn.id,
                             user_chat_id=conn.user_chat_id)
            s.add(rec)
        rec.user_chat_id = conn.user_chat_id
        rec.is_enabled = bool(conn.is_enabled)
        rec.can_transfer = can_transfer
        await s.commit()

    if not has_feature(row.plan, Feature.NFT_MARKET):
        return
    try:
        if conn.is_enabled and can_transfer:
            await bot.send_message(conn.user.id,
                "✅ حسابِ کاری وصل شد و اجازهٔ انتقالِ گیفت داده شده.\n\n"
                "از این به بعد سفارش‌هایِ مارکت به‌صورتِ خودکار تحویل داده می‌شوند.")
        elif conn.is_enabled:
            await bot.send_message(conn.user.id,
                "⚠️ حسابِ کاری وصل شد، ولی اجازهٔ «انتقال و ارتقاءِ گیفت» داده نشده.\n\n"
                "تا وقتی این دسترسی را ندهی، سفارش‌ها باید دستی تحویل داده شوند.")
        else:
            await bot.send_message(conn.user.id,
                "🔌 اتصالِ حسابِ کاری قطع شد. سفارش‌ها از این به بعد دستی تحویل می‌شوند.")
    except Exception as e:                                   # noqa: BLE001
        # === PATCH_AUDIT_PRECISION_V1 === قبلاً بی‌صدا بلعیده می‌شد
        logger.warning("اطلاعِ وضعیتِ اتصالِ حسابِ کاری NFT نرفت (%s): %s", type(e).__name__, e)


# ───────────────────── انتقالِ خودکار ─────────────────────

async def try_auto_transfer(bot_id: int, order_id: int) -> bool:
    """تلاش برایِ انتقالِ خودکارِ گیفت. True یعنی تحویل انجام شد.

    هر شکستی (نبودِ اتصال، نداشتنِ شناسهٔ گیفت، خطایِ تلگرام) فقط باعث
    برگشت به مسیرِ دستی می‌شود، نه خراب‌شدنِ سفارش.
    """
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if not o or o.bot_id != bot_id or o.status not in ("pending", "paid"):
            return False
        it = await s.get(NftItem, o.item_id)
        buyer = await s.get(BotUser, o.user_id)
        conn = await s.scalar(select(NftBizConn).where(
            NftBizConn.bot_id == bot_id, NftBizConn.is_enabled.is_(True),
            NftBizConn.can_transfer.is_(True)))
        gift_id = (it.owned_gift_id or "") if it else ""
        buyer_chat = buyer.telegram_id if buyer else None
        conn_id = conn.connection_id if conn else ""

    # یوزرنیم اصلاً از مسیرِ Bot API منتقل نمی‌شود (Fragment/TON است)
    if not conn_id or not gift_id or not buyer_chat or (it and it.kind != "gift"):
        return False

    row = await _bot_row(bot_id)
    if not row:
        return False
    tbot = _tenant_bot(row.token)
    try:
        await tbot.transfer_gift(business_connection_id=conn_id,
                                 owned_gift_id=gift_id,
                                 new_owner_chat_id=int(buyer_chat))
    except Exception as e:                                   # noqa: BLE001
        logger.warning("انتقالِ خودکارِ گیفت برایِ سفارشِ #%s نشد (%s): %s",
                       order_id, type(e).__name__, e)
        async with Session() as s:
            o = await s.get(NftOrder, order_id)
            if o:
                o.transfer_note = f"{type(e).__name__}: {str(e)[:300]}"
                await s.commit()
        return False
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass

    from tenant.bots.virtual_service.nft_market import apply_order_action
    ok, _ = await apply_order_action(bot_id, order_id, "delivered")
    if ok:
        async with Session() as s:
            o = await s.get(NftOrder, order_id)
            if o:
                o.transfer_mode = "auto"
                await s.commit()
    return ok


# ───────────────────── اطلاع‌رسانی ─────────────────────

def _order_kb(order: NftOrder) -> InlineKeyboardMarkup:
    rows = []
    if order.status in ("pending", "paid"):
        rows.append([
            InlineKeyboardButton(text="✅ تحویل شد",
                                 callback_data=f"nftm:ord:{order.id}:delivered",
                                 style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="↩️ لغو و عودت",
                                 callback_data=f"nftm:ord:{order.id}:reject",
                                 style=ButtonStyle.DANGER),
        ])
    elif order.status == "delivered" and order.kind == "rent":
        rows.append([InlineKeyboardButton(
            text="🔙 برگشت خورد", callback_data=f"nftm:ord:{order.id}:returned",
            style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_admin_new_order(bot_id: int, order_id: int) -> None:
    """سفارشِ جدیدی که باید دستی تحویل داده شود."""
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if not o or o.bot_id != bot_id:
            return
        it = await s.get(NftItem, o.item_id)
        buyer = await s.get(BotUser, o.user_id)
        row = await s.get(BotRow, bot_id)
        if not row:
            return
        owner_id, row_token = int(row.owner_id), row.token
        kb = _order_kb(o)
        who = (buyer.full_name or str(buyer.telegram_id)) if buyer else "نامشخص"
        who_id = buyer.telegram_id if buyer else 0
        txt = (
            f"🖼 <b>سفارشِ جدیدِ مارکت</b> — #{o.id}\n\n"
            f"آیتم: <b>{(it.title if it else '؟')}</b>"
            + (f" #{it.ref}" if (it and it.ref) else "") + "\n"
            + (f"نوع: <b>خرید</b>\n" if o.kind == "buy"
               else f"نوع: <b>اجاره</b> — {o.days} روز\n")
            + f"مبلغِ پرداختی: <b>{_rial(o.amount)}</b> تومان\n"
            f"خریدار: {who} (<code>{who_id}</code>)\n"
            + (f"\n⏳ پایانِ اجاره: {o.expires_at:%Y-%m-%d}\n" if o.expires_at else "")
            + (f"\n⚠️ انتقالِ خودکار نشد: <code>{o.transfer_note}</code>\n"
               if o.transfer_note else "")
            + "\nپس از انتقالِ آیتم، دکمهٔ «تحویل شد» را بزن."
        )

    tbot = _tenant_bot(row_token)
    try:
        await tbot.send_message(owner_id, txt, reply_markup=kb)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("اطلاعِ سفارشِ NFT به ادمین نرسید (%s): %s", type(e).__name__, e)
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass


async def notify_user_order(bot_id: int, order_id: int, action: str) -> None:
    """اطلاع به خریدار پس از تغییرِ وضعیتِ سفارش."""
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        if not o or o.bot_id != bot_id:
            return
        it = await s.get(NftItem, o.item_id)
        buyer = await s.get(BotUser, o.user_id)
        if not buyer:
            return
        row = await s.get(BotRow, bot_id)
        if not row:
            return
        row_token = row.token
        uid = buyer.telegram_id
        title = it.title if it else "آیتم"
        if action == "delivered":
            txt = (f"✅ سفارشِ #{o.id} تحویل داده شد.\n\n<b>{title}</b>"
                   + (f"\n⏳ تا {o.expires_at:%Y-%m-%d} در اختیارِ توست."
                      if (o.kind == "rent" and o.expires_at) else ""))
        elif action == "returned":
            txt = f"🔙 مهلتِ اجارهٔ <b>{title}</b> (سفارشِ #{o.id}) تمام شد و آیتم برگشت داده شد."
        elif action == "reject":
            txt = (f"↩️ سفارشِ #{o.id} (<b>{title}</b>) لغو شد و مبلغِ "
                   f"<b>{_rial(o.amount)}</b> تومان به کیف‌پولت برگشت.")
        else:
            return

    tbot = _tenant_bot(row_token)
    try:
        await tbot.send_message(uid, txt)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("اطلاعِ سفارشِ NFT به کاربر نرسید (%s): %s", type(e).__name__, e)
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass


# ───────────────────── دکمه‌هایِ اینلاینِ ادمین ─────────────────────

@nft_admin_router.callback_query(F.data.startswith("nftm:ord:"))
async def nft_order_action(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or int(row.owner_id) != int(cb.from_user.id):
        await cb.answer("دسترسی نداری.", show_alert=True)
        return
    bot_id = row.id
    try:
        _, _, oid, action = cb.data.split(":", 3)
        order_id = int(oid)
    except (ValueError, AttributeError):
        await cb.answer("داده نامعتبر.", show_alert=True)
        return
    if action not in ("delivered", "reject", "returned"):
        await cb.answer("نامعتبر.", show_alert=True)
        return

    from tenant.bots.virtual_service.nft_market import apply_order_action
    ok, msg = await apply_order_action(bot_id, order_id, action)
    if not ok:
        await cb.answer({"not_found": "سفارش پیدا نشد.",
                         "bad_status": "وضعیتِ این سفارش اجازه نمی‌دهد.",
                         "not_rent": "این سفارش اجاره‌ای نیست."}.get(msg, "انجام نشد."),
                        show_alert=True)
        return

    label = {"delivered": "✅ تحویل شد", "reject": "↩️ لغو و عودت شد",
             "returned": "🔙 برگشت خورد"}[action]
    try:
        await cb.message.edit_text((cb.message.html_text or "") + f"\n\n<b>{label}</b>",
                                   reply_markup=None)
    except Exception:                                        # noqa: BLE001
        pass
    await cb.answer(label)


# ───────────────────── پایانِ خودکارِ اجاره ─────────────────────

async def expire_due_rentals() -> int:
    """اجاره‌هایی که مهلتشان تمام شده را علامت می‌زند و به ادمین یادآوری
    می‌کند که آیتم را پس بگیرد. از حلقهٔ زمان‌بندِ tenant صدا زده می‌شود."""
    now = _dt.datetime.utcnow()
    async with Session() as s:
        due = (await s.scalars(select(NftOrder).where(
            NftOrder.kind == "rent", NftOrder.status == "delivered",
            NftOrder.expires_at.isnot(None), NftOrder.expires_at <= now)
            .limit(100))).all()
        pairs = [(o.bot_id, o.id) for o in due]
        for o in due:
            o.status = "expired"
        await s.commit()

    for bot_id, oid in pairs:
        try:
            await _notify_expired(bot_id, oid)
        except Exception as e:                               # noqa: BLE001
            logger.warning("یادآوریِ پایانِ اجارهٔ #%s نشد (%s)", oid, type(e).__name__)
    return len(pairs)


async def _notify_expired(bot_id: int, order_id: int) -> None:
    async with Session() as s:
        o = await s.get(NftOrder, order_id)
        row = await s.get(BotRow, bot_id)
        if not o or not row:
            return
        it = await s.get(NftItem, o.item_id)
        buyer = await s.get(BotUser, o.user_id)
        owner_id, row_token = int(row.owner_id), row.token
        title = it.title if it else "آیتم"
        who = (buyer.full_name or str(buyer.telegram_id)) if buyer else "؟"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔙 برگشت خورد", callback_data=f"nftm:ord:{o.id}:returned",
            style=ButtonStyle.PRIMARY)]])

    tbot = _tenant_bot(row_token)
    try:
        await tbot.send_message(
            owner_id,
            f"⏳ <b>مهلتِ اجاره تمام شد</b> — سفارشِ #{order_id}\n\n"
            f"آیتم: <b>{title}</b>\nمستأجر: {who}\n\n"
            "آیتم را پس بگیر و بعد دکمهٔ زیر را بزن تا دوباره در مارکت قرار بگیرد.",
            reply_markup=kb)
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass


# ───────────────────── هشدارِ انقضایِ توکنِ منبع ─────────────────────
# === PATCH_NFT_SOURCES_V1 ===
# توکنِ مارکت‌ها هر ۲۴ تا ۷۲ ساعت باطل می‌شود و «تازه‌سازیِ خودکار» واقعاً
# ممکن نیست (نیاز به سشنِ تلگرامِ خودِ کاربر دارد). پس بهترین کاری که
# می‌شود کرد این است: به‌محضِ باطل‌شدن، ادمین را خبر کن — یک بار، نه هر
# دور — تا همگام‌سازی بی‌سروصدا نخوابد.

async def notify_source_expired(bot_id: int, source_id: int) -> None:
    import datetime as _d
    async with Session() as s:
        src = await s.get(NftMarketSource, source_id)
        row = await s.get(BotRow, bot_id)
        if not src or not row or src.bot_id != bot_id:
            return
        if src.expiry_warned_at:            # قبلاً هشدار داده شده
            return
        src.expiry_warned_at = _d.datetime.utcnow()
        await s.commit()
        owner_id, token = int(row.owner_id), row.token
        title = src.title or src.provider
        err = src.last_error or ""

    txt = (
        f"⚠️ <b>توکنِ منبعِ «{title}» منقضی شد</b>\n\n"
        "همگام‌سازیِ قیمت‌ها متوقف شده و آیتم‌ها دیگر به‌روز نمی‌شوند.\n"
        + (f"\n<code>{_esc_html(str(err))}</code>\n" if err else "")
        + "\nبرایِ رفعش: مینی‌اپ ۲ → تبِ «⚙️ مدیریت» → «🔌 منابع» → "
        "همین منبع → توکنِ تازه را بگذار.\n\n"
        "<i>توکنِ این مارکت‌ها هر ۲ تا ۳ روز باطل می‌شود؛ این طبیعی است.</i>"
    )
    tbot = _tenant_bot(token)
    try:
        await tbot.send_message(owner_id, txt)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("هشدارِ انقضایِ منبع نرفت (%s): %s", type(e).__name__, e)
    finally:
        try:
            await tbot.session.close()
        except Exception:                                    # noqa: BLE001
            pass
