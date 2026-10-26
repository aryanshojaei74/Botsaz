"""
period_stats.py — PATCH_PERIOD_STATS_V1

«📈 آمار زمانی» در بخشِ آمار و گزارشات: سه گزارشِ هم‌شکل — سفارش‌ها، افزایشِ موجودی (شارژ) و
زیرمجموعه‌گیری — هرکدام با: کل / ۱ ساعتِ گذشته / امروز (تا همین لحظه) / ۷ روزِ گذشته / ۱ ماهِ گذشته.

سفارش‌ها از همهٔ جدول‌هایِ سفارشِ ربات جمع می‌شوند (VPN، SMM، گیفت/استارز/پریمیوم/TON، محصول، NFT) تا برایِ
هر نوعِ ربات درست باشد؛ فقط سفارش‌هایِ «واقعی» شمرده می‌شود (نه در انتظارِ پرداخت، لغو، ردشده، ناموفق).
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger as _get_logger
from models import (GiftOrder, NftOrder, Order, ProductOrder, ReferralJoin, SmmOrder, WalletTxn)

logger = _get_logger(__name__)

pst_router = Router(name="period_stats")

_IR = _dt.timedelta(hours=3, minutes=30)

# سفارشِ موفق/در جریان (پولش گرفته شده)، برایِ هر جدول: (مدل، ستونِ زمان، ستونِ مبلغ، وضعیت‌هایِ شمرده‌شده)
def _order_sources():
    return [
        (Order, Order.created_at, Order.amount, Order.status.in_(["paid"])),
        (SmmOrder, SmmOrder.created_at, SmmOrder.price_toman,
         SmmOrder.status.in_(["sent", "completed", "partial", "awaiting_admin", "sending"])),
        (GiftOrder, GiftOrder.created_at, GiftOrder.price_toman,
         GiftOrder.status.in_(["sent", "awaiting_admin", "auto_processing", "completed", "done"])),
        (ProductOrder, ProductOrder.created_at, ProductOrder.amount,
         ProductOrder.status.in_(["paid", "delivered"])),
        (NftOrder, NftOrder.created_at, NftOrder.amount, NftOrder.status.in_(["paid", "delivered"])),
    ]


# همهٔ انواعِ «شارژِ کیف‌پول» (همان فهرستِ آمارِ کلی در tenant/__init__.py)
CHARGE_KINDS = [
    "charge", "aqp_charge", "swap_charge", "crypto_charge", "nowpay_charge",
    "tonpays_charge", "zarinpal_charge", "zibal_charge", "idpay_charge",
    "hesabpay_charge", "abangateway_charge", "abangateway_manual_admin", "abangw_manual",
    "uniquepay_charge", "apay_charge", "hooshpay_charge", "ton_direct", "tron_direct", "admin",
]


def period_starts(now_utc: _dt.datetime | None = None) -> list[tuple[str, _dt.datetime | None]]:
    """[(برچسب، شروعِ بازه به UTC)] — None یعنی «کل». «امروز» به وقتِ ایران."""
    now = now_utc or _dt.datetime.utcnow()
    now_ir = now + _IR
    today_start = now_ir.replace(hour=0, minute=0, second=0, microsecond=0) - _IR
    hhmm = now_ir.strftime("%H:%M")
    return [
        ("کل", None),
        ("۱ ساعتِ گذشته", now - _dt.timedelta(hours=1)),
        (f"امروز (تا {hhmm})", today_start),
        ("۷ روزِ گذشته", now - _dt.timedelta(days=7)),
        ("۱ ماهِ گذشته", now - _dt.timedelta(days=30)),
    ]


def _fa(n) -> str:
    return f"{int(n):,}"


async def _count_sum(s, model, ts_col, amount_col, where, bot_col, bot_id: int, since):
    q = select(func.count(), func.coalesce(func.sum(amount_col), 0)).select_from(model).where(bot_col == bot_id, where)
    if since is not None:
        q = q.where(ts_col >= since)
    r = (await s.execute(q)).one()
    return int(r[0] or 0), int(r[1] or 0)


async def orders_report(bot_id: int, now_utc=None) -> list[tuple[str, int, int]]:
    """[(برچسب، تعداد، مبلغ)]"""
    out = []
    async with Session() as s:
        for label, since in period_starts(now_utc):
            n = tot = 0
            for model, ts, amt, where in _order_sources():
                bot_col = getattr(model, "bot_id")
                c, t = await _count_sum(s, model, ts, amt, where, bot_col, bot_id, since)
                n, tot = n + c, tot + t
            out.append((label, n, tot))
    return out


async def topups_report(bot_id: int, now_utc=None) -> list[tuple[str, int, int]]:
    out = []
    async with Session() as s:
        for label, since in period_starts(now_utc):
            c, t = await _count_sum(s, WalletTxn, WalletTxn.created_at, WalletTxn.amount,
                                    (WalletTxn.kind.in_(CHARGE_KINDS)) & (WalletTxn.amount > 0),
                                    WalletTxn.bot_id, bot_id, since)
            out.append((label, c, t))
    return out


async def referrals_report(bot_id: int, now_utc=None) -> list[tuple[str, int, int]]:
    out = []
    async with Session() as s:
        for label, since in period_starts(now_utc):
            c, t = await _count_sum(s, ReferralJoin, ReferralJoin.created_at, ReferralJoin.reward_paid,
                                    ReferralJoin.id > 0, ReferralJoin.bot_id, bot_id, since)
            out.append((label, c, t))
    return out


def _block(title: str, rows: list[tuple[str, int, int]], unit: str, money_label: str) -> str:
    lines = [f"<b>{title}:</b>"]
    for label, n, tot in rows:
        extra = f" — {money_label}: {_fa(tot)} تومان" if tot else ""
        lines.append(f"• {label}: <b>{_fa(n)}</b> {unit}{extra}")
    return "\n".join(lines)


async def build_text(bot_id: int, now_utc=None) -> str:
    o, w, r = (await orders_report(bot_id, now_utc), await topups_report(bot_id, now_utc),
               await referrals_report(bot_id, now_utc))
    return ("📈 <b>آمار زمانی</b>\n\n"
            + "📋 " + _block("آمار سفارشات", o, "سفارش", "فروش") + "\n\n"
            + "👛 " + _block("آمار افزایش موجودی‌ها", w, "شارژ", "مجموع") + "\n\n"
            + "👥 " + _block("آمار زیرمجموعه‌گیری", r, "نفر", "پاداشِ پرداخت‌شده"))


@pst_router.callback_query(F.data == "pst:menu")
async def pst_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _gate
    from plans import Feature
    row = await _gate(cb, bot, Feature.BOT_STATS)
    if not row:
        return
    text = await build_text(row.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="pst:menu", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:stats", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()
