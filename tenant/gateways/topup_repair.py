"""
tenant/gateways/topup_repair.py — جابه‌جاییِ شارژهایی که به «حسابِ خودِ ربات» رفته‌اند.

=== PATCH_PAYER_IDENTITY_V1 ===
پیش از رفعِ باگِ `cb.message.from_user`، دکمه‌هایِ مبلغِ ثابتِ زرین‌پال/زیبال/حساب‌پی شارژ را به یک
BotUserِ ساختگی می‌بردند که شناسه‌اش همان آیدیِ عددیِ خودِ ربات است (مثلاً نام «Express | خدمات مجازی»،
یوزرنیم @ExpressPanel_Bot). مشتری «شارژ شد» می‌دید ولی موجودی‌اش صفر می‌ماند و پول در آن حسابِ ساختگی
جمع می‌شد.

این ماژول:
  • آن حساب‌های ساختگی و شارژهای جمع‌شده در آن‌ها را فهرست می‌کند (با حدسِ صاحبِ احتمالی از رویِ
    شمارهٔ کارتِ ثبت‌شده در توضیحِ تراکنش)،
  • و هر شارژ را — فقط با تأییدِ صریحِ ادمین — به کاربرِ درست منتقل می‌کند.

اصولِ ایمنی: پیش‌فرض dry-run؛ هر تراکنش فقط یک‌بار قابلِ انتقال است؛ موجودیِ حسابِ ساختگی هرگز منفی
نمی‌شود؛ مقصد باید کاربرِ واقعیِ *همان ربات* باشد؛ همه‌چیز در یک تراکنشِ دیتابیس و زیرِ قفلِ ردیف انجام
می‌شود؛ ردِ انتقال در دو WalletTxn و یادداشتِ تراکنشِ اصلی می‌ماند.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

MOVED_MARK = "[منتقل‌شده"
_CARD_RE = re.compile(r"card=([0-9*Xx\-]{8,})")


def own_id_from_token(token: str | None) -> int | None:
    """آیدیِ عددیِ تلگرامِ خودِ ربات = بخشِ قبل از «:» در توکن."""
    if not token or ":" not in token:
        return None
    head = token.split(":", 1)[0]
    return int(head) if head.isdigit() else None


def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


@dataclass
class Misdirected:
    txn_id: int
    bot_id: int
    bot_username: str | None
    phantom_user_id: int
    phantom_balance: float
    amount: float
    kind: str
    note: str
    created_at: object
    already_moved: bool
    card: str | None = None
    candidates: list[dict] = field(default_factory=list)


async def find_phantoms(s, bot_id: int | None = None):
    """[(BotRow, BotUser)] — کاربرانی که آیدی‌شان برابرِ آیدیِ خودِ ربات است."""
    from models import Bot as BotRow, BotUser
    q = select(BotRow)
    if bot_id is not None:
        q = q.where(BotRow.id == bot_id)
    out = []
    for b in (await s.scalars(q)).all():
        own = own_id_from_token(b.token)
        if own is None:
            continue
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == b.id, BotUser.telegram_id == own))
        if u is not None:
            out.append((b, u))
    return out


async def _candidates_for(s, bot_id: int, phantom_uid: int, card: str | None) -> list[dict]:
    """صاحبانِ احتمالیِ یک شارژ: (۱) کاربرانی که پیش‌تر با *همین کارت* شارژِ موفق داشته‌اند،
    (۲) کاربرانی که کارتِ احرازشدهٔ (KYC) آن‌ها به همان چهار رقم ختم می‌شود. فقط پیشنهاد است."""
    from models import BotUser, WalletTxn
    if not card:
        return []
    last4 = _digits(card)[-4:]
    found: dict[int, dict] = {}
    if last4:
        txns = (await s.scalars(select(WalletTxn).where(
            WalletTxn.bot_id == bot_id, WalletTxn.user_id != phantom_uid,
            WalletTxn.amount > 0, WalletTxn.note.like("%card=%")))).all()
        for t in txns:
            m = _CARD_RE.search(t.note or "")
            if m and _digits(m.group(1))[-4:] == last4:
                found.setdefault(t.user_id, {"user_id": t.user_id, "why": "شارژِ قبلی با همین کارت", "n": 0})["n"] += 1
        for u in (await s.scalars(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.kyc_card.isnot(None)))).all():
            if _digits(u.kyc_card)[-4:] == last4 and u.id != phantom_uid:
                found.setdefault(u.id, {"user_id": u.id, "why": "کارتِ احرازشده", "n": 0})["n"] += 1
    if not found:
        return []
    users = {u.id: u for u in (await s.scalars(select(BotUser).where(BotUser.id.in_(list(found))))).all()}
    res = []
    for uid, info in sorted(found.items(), key=lambda kv: -kv[1]["n"]):
        u = users.get(uid)
        if u is not None:
            res.append({"telegram_id": u.telegram_id, "name": u.full_name, "username": u.username,
                        "why": info["why"], "n": info["n"]})
    return res[:3]


async def list_misdirected(s, bot_id: int | None = None) -> list[Misdirected]:
    from models import WalletTxn
    out: list[Misdirected] = []
    for b, phantom in await find_phantoms(s, bot_id):
        txns = (await s.scalars(select(WalletTxn).where(
            WalletTxn.bot_id == b.id, WalletTxn.user_id == phantom.id,
            WalletTxn.amount > 0).order_by(WalletTxn.id))).all()
        for t in txns:
            m = _CARD_RE.search(t.note or "")
            card = m.group(1) if m else None
            out.append(Misdirected(
                txn_id=t.id, bot_id=b.id, bot_username=b.username, phantom_user_id=phantom.id,
                phantom_balance=float(phantom.balance or 0), amount=float(t.amount), kind=t.kind,
                note=t.note or "", created_at=t.created_at,
                already_moved=MOVED_MARK in (t.note or ""), card=card,
                candidates=await _candidates_for(s, b.id, phantom.id, card)))
    return out


class RepairError(Exception):
    """انتقال ممکن نیست؛ پیام برایِ ادمین قابلِ‌نمایش است."""


@dataclass
class MovePlan:
    txn_id: int
    amount: int
    bot_id: int
    to_telegram_id: int
    to_name: str | None
    phantom_balance_before: float
    phantom_balance_after: float
    target_balance_before: float
    target_balance_after: float
    applied: bool


async def move_credit(Session, txn_id: int, to_telegram_id: int, *, apply: bool = False) -> MovePlan:
    """شارژِ txn_id را از حسابِ ساختگیِ ربات به کاربرِ to_telegram_id منتقل می‌کند (dry-run مگر apply=True)."""
    from models import Bot as BotRow, BotUser, WalletTxn
    async with Session() as s:
        t = await s.get(WalletTxn, txn_id, with_for_update=True)
        if t is None:
            raise RepairError(f"تراکنش #{txn_id} پیدا نشد.")
        if float(t.amount or 0) <= 0:
            raise RepairError("فقط تراکنشِ شارژ (مبلغِ مثبت) قابلِ انتقال است.")
        if MOVED_MARK in (t.note or ""):
            raise RepairError("این تراکنش قبلاً منتقل شده است.")
        bot = await s.get(BotRow, t.bot_id)
        own = own_id_from_token(bot.token if bot else None)
        phantom = await s.scalar(select(BotUser).where(BotUser.id == t.user_id).with_for_update())
        if phantom is None or own is None or phantom.telegram_id != own:
            raise RepairError("این تراکنش مالِ حسابِ ساختگیِ خودِ ربات نیست؛ انتقال مجاز نیست.")
        if int(to_telegram_id) == own:
            raise RepairError("مقصد نمی‌تواند خودِ ربات باشد.")
        target = await s.scalar(select(BotUser).where(
            BotUser.bot_id == t.bot_id, BotUser.telegram_id == int(to_telegram_id)).with_for_update())
        if target is None:
            raise RepairError(f"کاربری با آیدیِ {to_telegram_id} در این ربات پیدا نشد "
                              "(باید حداقل یک‌بار /start زده باشد).")
        amount = int(t.amount)
        if float(phantom.balance or 0) < amount:
            raise RepairError(f"موجودیِ حسابِ ساختگی ({int(phantom.balance or 0):,}) از مبلغِ انتقال "
                              f"({amount:,}) کمتر است؛ برای جلوگیری از موجودیِ منفی انجام نشد.")
        plan = MovePlan(
            txn_id=t.id, amount=amount, bot_id=t.bot_id, to_telegram_id=int(to_telegram_id),
            to_name=target.full_name, phantom_balance_before=float(phantom.balance or 0),
            phantom_balance_after=float(phantom.balance or 0) - amount,
            target_balance_before=float(target.balance or 0),
            target_balance_after=float(target.balance or 0) + amount, applied=False)
        if not apply:
            await s.rollback()
            return plan
        phantom.balance = float(phantom.balance or 0) - amount
        target.balance = float(target.balance or 0) + amount
        s.add(WalletTxn(bot_id=t.bot_id, user_id=phantom.id, amount=-amount, kind="misdirect_fix_out",
                        note=f"انتقالِ شارژِ اشتباه #{t.id} به کاربر {to_telegram_id}"[:200]))
        s.add(WalletTxn(bot_id=t.bot_id, user_id=target.id, amount=amount, kind="misdirect_fix_in",
                        note=f"شارژِ اصلاح‌شده (تراکنش #{t.id}؛ {t.kind})"[:200]))
        # نشانهٔ «منتقل‌شده» هرگز نباید بریده شود (ستون ۲۰۰ نویسه است) وگرنه انتقالِ دوباره ممکن می‌شد
        mark = f" | {MOVED_MARK}→{to_telegram_id}]"
        t.note = (t.note or "")[: 200 - len(mark)] + mark
        await s.commit()
        plan.applied = True
        return plan
