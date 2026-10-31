"""
funnel.py — «🔻 قیفِ خرید» (PATCH_FUNNEL_V1): مسیرِ کاربرانِ جدید تا خرید و تکرارِ خرید.

بدونِ ثبتِ رویدادِ جدید، فقط از دادهٔ موجود (کاربران، تراکنش‌هایِ کیف‌پول، سفارش‌ها) ساخته می‌شود؛ برایِ کاربرانی که در بازهٔ
انتخابی (۷ روز / ۳۰ روز / کل) عضو شده‌اند:
  عضو شدند ← شمارهٔ تاییدشده (اگر گیتش بوده) ← حداقل یک شارژِ کیف‌پول ← حداقل یک خرید ← دو خرید یا بیشتر
و بزرگ‌ترین ریزش را نشان می‌دهد. «خرید» = بیشینهٔ (تعدادِ کسرِ خرید/تمدید از کیف‌پول، تعدادِ سفارش‌هایِ ثبت‌شده) برایِ هر کاربر
تا خریدِ کیف‌پولی دو بار شمرده نشود؛ خریدِ مستقیمِ کارت‌به‌کارت در سفارش‌ها دیده می‌شود.
مسیر: آمار و گزارشات ← 📈 آمار زمانی ← 🔻 قیفِ خرید.  callbackها: fnl:*
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from config import Session
from models import BotUser, GiftOrder, Order, PanelAccount, SmmOrder, WalletTxn

from tenant.shared.period_stats import CHARGE_KINDS

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

funnel_router = Router(name="funnel")

WINDOWS = (7, 30, 0)


def _since(days: int, now: _dt.datetime | None = None):
    return None if not days else (now or _dt.datetime.utcnow()) - _dt.timedelta(days=days)


async def compute(bot_id: int, days: int, now: _dt.datetime | None = None) -> dict:
    since = _since(days, now)
    async with Session() as s:
        q = select(BotUser.id, BotUser.telegram_id, BotUser.phone_verified).where(BotUser.bot_id == bot_id)
        if since is not None:
            q = q.where(BotUser.joined_at >= since)
        users = (await s.execute(q)).all()
        uid_by_tg = {tg: uid for uid, tg, _pv in users}
        ids = {uid for uid, _tg, _pv in users}
        phone = {uid for uid, _tg, pv in users if pv}
        phone_gate_used = bool(await s.scalar(select(func.count(BotUser.id)).where(
            BotUser.bot_id == bot_id, BotUser.phone_verified == True)))   # noqa: E712
        chargers = {r[0] for r in (await s.execute(select(WalletTxn.user_id).where(
            WalletTxn.bot_id == bot_id, WalletTxn.kind.in_(CHARGE_KINDS), WalletTxn.amount > 0).distinct())).all()} & ids
        wallet_cnt = {r[0]: int(r[1]) for r in (await s.execute(select(WalletTxn.user_id, func.count()).where(
            WalletTxn.bot_id == bot_id, WalletTxn.kind.in_(["purchase", "renew"]), WalletTxn.amount < 0)
            .group_by(WalletTxn.user_id))).all() if r[0] in ids}
        order_cnt: dict[int, int] = {}

        def add(uid, n):
            if uid in ids:
                order_cnt[uid] = order_cnt.get(uid, 0) + int(n)
        for uid, n in (await s.execute(select(Order.user_id, func.count()).where(
                Order.bot_id == bot_id, Order.status == "paid").group_by(Order.user_id))).all():
            add(uid, n)
        for tg, n in (await s.execute(select(SmmOrder.user_telegram_id, func.count()).where(
                SmmOrder.bot_id == bot_id, SmmOrder.status.in_(["sent", "completed", "partial", "awaiting_admin", "sending"]))
                .group_by(SmmOrder.user_telegram_id))).all():
            add(uid_by_tg.get(tg), n)
        for uid, n in (await s.execute(select(GiftOrder.buyer_user_id, func.count()).where(
                GiftOrder.bot_id == bot_id, GiftOrder.status.in_(["sent", "awaiting_admin", "auto_processing", "completed", "done"]))
                .group_by(GiftOrder.buyer_user_id))).all():
            add(uid, n)
        for tg, n in (await s.execute(select(PanelAccount.user_telegram_id, func.count()).where(
                PanelAccount.bot_id == bot_id, PanelAccount.paid_toman > 0).group_by(PanelAccount.user_telegram_id))).all():
            add(uid_by_tg.get(tg), n)
    buys = {u: max(wallet_cnt.get(u, 0), order_cnt.get(u, 0)) for u in ids}
    bought = {u for u, n in buys.items() if n >= 1}
    repeat = {u for u, n in buys.items() if n >= 2}
    stages = [("👥 عضو شدند", len(ids))]
    if phone_gate_used:
        stages.append(("📱 شماره تأیید شد", len(phone)))
    stages += [("👛 حداقل یک بار شارژ کرد", len(chargers)), ("🛒 حداقل یک خرید", len(bought)), ("🔁 دو خرید یا بیشتر", len(repeat))]
    return {"stages": stages, "days": days}


def analyse(stages: list[tuple[str, int]]) -> tuple[int, float] | None:
    """(اندیسِ مرحلهٔ بعد از بزرگ‌ترین ریزش، درصدِ ریزش) یا None."""
    worst, pct = None, 0.0
    for i in range(1, len(stages)):
        prev, cur = stages[i - 1][1], stages[i][1]
        if prev > 0:
            drop = (prev - cur) * 100.0 / prev
            if drop > pct:
                worst, pct = i, drop
    return (worst, pct) if worst is not None else None


def render(res: dict) -> str:
    st = res["stages"]
    label = "کل زمان" if not res["days"] else f"{res['days']} روزِ گذشته"
    lines = [f"🔻 <b>قیفِ خرید</b> — کاربرانی که عضو شدند: {label}\n"]
    base = st[0][1]
    for i, (name, n) in enumerate(st):
        pct = f" — {n * 100 // base}٪ از کل" if base and i else ""
        step = ""
        if i and st[i - 1][1]:
            step = f" (↘️ {100 - n * 100 // st[i - 1][1]}٪ ریزش)"
        lines.append(f"{name}: <b>{n:,}</b>{pct}{step}")
    if not base:
        lines.append("\nدر این بازه کاربرِ جدیدی نبوده.")
    else:
        w = analyse(st)
        if w and w[1] > 0:
            lines.append(f"\n💡 بزرگ‌ترین ریزش: «{st[w[0] - 1][0]}» ← «{st[w[0]][0]}» ({int(w[1])}٪). همان‌جا را (متن، قیمت، روشِ پرداخت، سرعتِ تحویل) بهبود بده.")
        lines.append("\nℹ️ فقط از دادهٔ ثبت‌شده ساخته می‌شود؛ بازدیدِ صفحه‌ها شمرده نمی‌شود. بازهٔ کوتاه و اخیر را ببین.")
    return "\n".join(lines)


async def _view(bot_id: int, days: int):
    text = render(await compute(bot_id, days))
    rows = [[InlineKeyboardButton(text=("✅ " if d == days else "") + ("کل" if not d else f"{d} روز"), callback_data=f"fnl:{d}",
                                  style=ButtonStyle.SUCCESS if d == days else ButtonStyle.PRIMARY) for d in WINDOWS],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pst:menu", style=ButtonStyle.DANGER)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@funnel_router.callback_query(F.data.regexp(r"^fnl:(7|30|0)$"))
async def funnel_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _gate
    from plans import Feature
    row = await _gate(cb, bot, Feature.BOT_STATS)
    if not row:
        return
    text, kb = await _view(row.id, int(cb.data.split(":")[1]))
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()
