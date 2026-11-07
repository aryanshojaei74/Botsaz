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
from models import (BetLedger, GiftOrder, NftOrder, Order, ProductOrder, ReferralJoin, SmmOrder, WalletTxn)

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
    "uniquepay_charge", "apay_charge", "hooshpay_charge", "blupal_charge", "ton_direct", "tron_direct", "admin",
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


# === PATCH_FEE_PROFIT_STATS_V1 === (SINA) آمارِ کارمزد (انتقال/بازی) و سود به‌تفکیکِ بازه
_FEE_NOTE_RE = None


def _fee_from_note(note: str | None) -> int:
    """مبلغِ کارمزد از یادداشتِ «کارمزدِ انتقال (۲٪): 1,234 تومان» (ردیفِ WalletTxn مبلغِ صفر دارد و عدد فقط در یادداشت است)."""
    import re
    global _FEE_NOTE_RE
    if _FEE_NOTE_RE is None:
        _FEE_NOTE_RE = re.compile(r"([\d,٬]+)\s*تومان")
    m = _FEE_NOTE_RE.findall(note or "")
    if not m:
        return 0
    try:
        return int(m[-1].replace(",", "").replace("٬", ""))
    except ValueError:
        return 0


async def fees_report(bot_id: int, now_utc=None) -> list[tuple[str, int, int]]:
    """[(برچسب، کارمزدِ انتقال، کارمزدِ بازی‌ها)]"""
    out = []
    async with Session() as s:
        for label, since in period_starts(now_utc):
            q = select(WalletTxn.note).where(WalletTxn.bot_id == bot_id, WalletTxn.kind == "group_transfer_fee")
            if since is not None:
                q = q.where(WalletTxn.created_at >= since)
            transfer = sum(_fee_from_note(n) for n in (await s.scalars(q.limit(100000))).all())
            q2 = select(func.coalesce(func.sum(-BetLedger.amount), 0)).where(BetLedger.bot_id == bot_id, BetLedger.kind == "fee")
            if since is not None:
                q2 = q2.where(BetLedger.created_at >= since)
            game = int((await s.execute(q2)).scalar() or 0)
            out.append((label, int(transfer), game))
    return out


async def profit_report(bot_id: int, now_utc=None) -> list[tuple[str, int, int, int]]:
    """[(برچسب، سودِ فروش، تعدادِ سفارشِ دارایِ هزینه، تعدادِ سفارشِ بدونِ هزینهٔ ثبت‌شده)]
    سود فقط برایِ سفارش‌هایِ خدماتِ مجازی که هزینهٔ خرید (cost_toman) ثبت دارند = قیمتِ فروش − هزینه."""
    out = []
    ok_status = SmmOrder.status.in_(["sent", "completed", "partial", "awaiting_admin", "sending"])
    async with Session() as s:
        for label, since in period_starts(now_utc):
            base = [SmmOrder.bot_id == bot_id, ok_status]
            if since is not None:
                base.append(SmmOrder.created_at >= since)
            r = (await s.execute(select(
                func.coalesce(func.sum(SmmOrder.price_toman - SmmOrder.cost_toman), 0), func.count()
            ).where(*base, SmmOrder.cost_toman.is_not(None), SmmOrder.cost_toman > 0))).one()
            miss = int((await s.execute(select(func.count()).where(
                *base, (SmmOrder.cost_toman.is_(None)) | (SmmOrder.cost_toman <= 0)))).scalar() or 0)
            out.append((label, int(r[0] or 0), int(r[1] or 0), miss))
    return out


def _fees_block(rows: list[tuple[str, int, int]]) -> str:
    lines = ["<b>آمار کارمزد:</b>"]
    for label, tr, gm in rows:
        lines.append(f"• {label}: <b>{_fa(tr + gm)}</b> تومان — انتقال: {_fa(tr)} · بازی‌ها: {_fa(gm)}")
    return "\n".join(lines)


def _profit_block(rows: list[tuple[str, int, int, int]]) -> str:
    lines = ["<b>آمار سود (فروشِ خدماتِ مجازی):</b>"]
    for label, p, n, miss in rows:
        extra = f" (از {_fa(n)} سفارش)" if n else ""
        lines.append(f"• {label}: <b>{_fa(p)}</b> تومان{extra}")
    if any(m for *_a, m in rows):
        lines.append("<i>سفارش‌هایِ بدونِ هزینهٔ ثبت‌شده در سود حساب نمی‌شوند.</i>")
    return "\n".join(lines)


def _block(title: str, rows: list[tuple[str, int, int]], unit: str, money_label: str) -> str:
    lines = [f"<b>{title}:</b>"]
    for label, n, tot in rows:
        extra = f" — {money_label}: {_fa(tot)} تومان" if tot else ""
        lines.append(f"• {label}: <b>{_fa(n)}</b> {unit}{extra}")
    return "\n".join(lines)


async def build_text(bot_id: int, now_utc=None) -> str:
    o, w, r = (await orders_report(bot_id, now_utc), await topups_report(bot_id, now_utc),
               await referrals_report(bot_id, now_utc))
    try:
        f, pr = await fees_report(bot_id, now_utc), await profit_report(bot_id, now_utc)
        fp = "\n\n💸 " + _fees_block(f) + "\n\n💹 " + _profit_block(pr)
    except Exception:
        logger.exception("period_stats: آمارِ کارمزد/سود ناموفق بود (بقیهٔ گزارش نمایش داده شد)")
        fp = ""
    return ("📈 <b>آمار زمانی</b>\n\n"
            + "📋 " + _block("آمار سفارشات", o, "سفارش", "فروش") + "\n\n"
            + "👛 " + _block("آمار افزایش موجودی‌ها", w, "شارژ", "مجموع") + "\n\n"
            + "👥 " + _block("آمار زیرمجموعه‌گیری", r, "نفر", "پاداشِ پرداخت‌شده") + fp)


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
        [InlineKeyboardButton(text="🔻 قیفِ خرید (ریزشِ کاربران)", callback_data="fnl:30", style=ButtonStyle.PRIMARY)],   # PATCH_FUNNEL_V1
        [InlineKeyboardButton(text="📅 گزارشِ روزانه به تلگرام", callback_data="drep:menu", style=ButtonStyle.SUCCESS)],   # PATCH_DAILY_REPORT_V1
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:stats", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()
