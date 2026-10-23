"""
tenant/bots/giveaway/engine.py — هستهٔ رباتِ قرعه‌کشی (بدونِ aiogram؛ قابلِ تست) — PATCH_GIVEAWAY_BOT_V1.

  • کپچایِ ریاضی (جمع/تفریق/ضرب/تقسیمِ صحیح) برایِ تشخیصِ کاربرِ واقعی؛
  • شمارهٔ ایرانی (+98 موبایل)؛
  • ثبتِ شرکت‌کننده + شانسِ اضافه از دعوت (با ضدِ تقلب: خوددعوتی، شمارهٔ تکراری، سقف)؛
  • قرعهٔ وزنیِ بدونِ جایگزینی با SystemRandom (امن)، با حذفِ کسانی که از کانالِ اجباری لفت داده‌اند؛
  • ثبتِ برندگان و جایزه‌ها.
"""
from __future__ import annotations

import random
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

from sqlalchemy import func, select

from config import Session
from models import Giveaway, GiveawayEntry, GiveawayPrize, GiveawayWinner

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

MAX_WINNERS = 100
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_ascii_digits(s: str | None) -> str:
    return (s or "").translate(_FA)


# ═══════════ کپچا ═══════════

def make_captcha(rng: random.Random | None = None) -> tuple[str, int]:
    """(متنِ سؤال، پاسخِ عددی). همیشه جوابِ صحیح و مثبت/صفر؛ ضرب و تقسیم کوچک تا برایِ آدمِ واقعی ساده باشد."""
    r = rng or random.SystemRandom()
    op = r.choice(("+", "-", "×", "÷"))
    if op == "+":
        a, b = r.randint(3, 40), r.randint(3, 40)
        return f"{a} + {b}", a + b
    if op == "-":
        a = r.randint(15, 60)
        b = r.randint(2, a - 1)
        return f"{a} - {b}", a - b
    if op == "×":
        a, b = r.randint(2, 9), r.randint(2, 9)
        return f"{a} × {b}", a * b
    b = r.randint(2, 9)
    ans = r.randint(2, 12)
    return f"{b * ans} ÷ {b}", ans


def check_captcha(user_text: str | None, answer: int) -> bool:
    t = to_ascii_digits(user_text).strip()
    return bool(re.fullmatch(r"-?\d{1,6}", t)) and int(t) == int(answer)


# ═══════════ شماره ═══════════

def normalize_iran_phone(raw: str | None) -> str | None:
    """۰۹۱۲… / 98912… / +98912… ⇒ «+98912xxxxxxx»؛ غیرِ موبایلِ ایران ⇒ None."""
    d = re.sub(r"[^\d]", "", to_ascii_digits(raw))
    if d.startswith("0098"):
        d = d[2:]
    if d.startswith("98") and len(d) == 12:
        national = d[2:]
    elif d.startswith("0") and len(d) == 11:
        national = d[1:]
    elif len(d) == 10:
        national = d
    else:
        return None
    return f"+98{national}" if re.fullmatch(r"9\d{9}", national) else None


# ═══════════ کد و شانس ═══════════

def gen_code(taken: set[str] | None = None) -> str:
    taken = taken or set()
    while True:
        c = secrets.token_hex(4).upper()
        if c not in taken:
            return c


def chances(bonus: int | None) -> int:
    return 1 + max(0, int(bonus or 0))


def weighted_draw(weights: dict[int, int], k: int, rng: random.Random | None = None) -> list[int]:
    """k شناسه (بدونِ تکرار) به‌صورتِ وزنی. وزنِ ≤۰ ⇒ حذف. اگر کمتر از k نفر باشد همه برمی‌گردند."""
    r = rng or random.SystemRandom()
    pool = {i: int(w) for i, w in weights.items() if int(w) > 0}
    out: list[int] = []
    for _ in range(max(0, min(k, len(pool)))):
        total = sum(pool.values())
        x = r.uniform(0, total)
        acc = 0.0
        pick = None
        for i, w in pool.items():
            acc += w
            if x <= acc:
                pick = i
                break
        if pick is None:
            pick = next(iter(pool))
        out.append(pick)
        del pool[pick]
    return out


# ═══════════ دسترسی به داده ═══════════

async def get_giveaway(gid: int, bot_id: int | None = None) -> Optional[Giveaway]:
    async with Session() as s:
        g = await s.get(Giveaway, gid)
    if g is None or (bot_id is not None and g.bot_id != bot_id):
        return None
    return g


async def active_giveaways(bot_id: int) -> list[Giveaway]:
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(Giveaway).where(
            Giveaway.bot_id == bot_id, Giveaway.status == "active").order_by(Giveaway.id.desc()))).all()
    return [g for g in rows if not (g.starts_at and g.starts_at > now)]


async def recent_giveaways(bot_id: int, limit: int = 15, statuses=("active", "ended", "cancelled")) -> list[Giveaway]:
    async with Session() as s:
        return list((await s.scalars(select(Giveaway).where(
            Giveaway.bot_id == bot_id, Giveaway.status.in_(statuses)).order_by(Giveaway.id.desc()).limit(limit))).all())


async def prizes_of(gid: int) -> list[GiveawayPrize]:
    async with Session() as s:
        return list((await s.scalars(select(GiveawayPrize).where(
            GiveawayPrize.giveaway_id == gid).order_by(GiveawayPrize.rank, GiveawayPrize.id))).all())


async def entry_of(gid: int, user_tg: int) -> Optional[GiveawayEntry]:
    async with Session() as s:
        return await s.scalar(select(GiveawayEntry).where(
            GiveawayEntry.giveaway_id == gid, GiveawayEntry.user_tg == user_tg))


async def count_entries(gid: int) -> int:
    async with Session() as s:
        return int(await s.scalar(select(func.count(GiveawayEntry.id)).where(GiveawayEntry.giveaway_id == gid)) or 0)


async def total_chances(gid: int) -> int:
    async with Session() as s:
        n = int(await s.scalar(select(func.count(GiveawayEntry.id)).where(GiveawayEntry.giveaway_id == gid)) or 0)
        b = int(await s.scalar(select(func.coalesce(func.sum(GiveawayEntry.bonus), 0)).where(
            GiveawayEntry.giveaway_id == gid)) or 0)
    return n + b


@dataclass
class NewGiveaway:
    title: str
    winners_count: int = 1
    description: str = ""
    require_phone: bool = False
    captcha: bool = True
    ref_bonus: int = 1
    ref_max: int = 0
    hide_winners: bool = True
    notify_all: bool = True
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    created_by: int | None = None


@dataclass
class NewPrize:
    title: str
    kind: str = "title"
    file_id: str | None = None
    text: str | None = None
    amount: int = 0


async def create_giveaway(bot_id: int, data: NewGiveaway, prizes: list[NewPrize]) -> Giveaway:
    """prizes: یک جایزه برایِ هر رتبه (به‌ترتیب)؛ اگر کمتر از تعدادِ برندگان بود، آخری برایِ بقیه تکرار می‌شود."""
    n = max(1, min(int(data.winners_count), MAX_WINNERS))
    async with Session() as s:
        g = Giveaway(
            bot_id=bot_id, title=data.title.strip()[:160], description=(data.description or "").strip() or None,
            winners_count=n, status="active", require_phone=data.require_phone, captcha=data.captcha,
            ref_bonus=max(0, int(data.ref_bonus)), ref_max=max(0, int(data.ref_max)),
            hide_winners=data.hide_winners, notify_all=data.notify_all, starts_at=data.starts_at,
            ends_at=data.ends_at, created_by=data.created_by)
        s.add(g)
        await s.flush()
        for rank in range(1, n + 1):
            p = prizes[rank - 1] if rank - 1 < len(prizes) else (prizes[-1] if prizes else NewPrize(title="جایزه"))
            s.add(GiveawayPrize(bot_id=bot_id, giveaway_id=g.id, rank=rank, title=p.title[:200], kind=p.kind,
                                file_id=p.file_id, text=p.text, amount=int(p.amount or 0)))
        await s.commit()
        return g


@dataclass
class JoinResult:
    ok: bool
    reason: str = ""            # closed | not_started | phone_needed | phone_used | exists
    entry: GiveawayEntry | None = None
    inviter_tg: int | None = None    # اگر شانسِ اضافه گرفت، برایِ اطلاع‌رسانی
    inviter_chances: int = 0


async def join(gid: int, bot_id: int, user_tg: int, name: str = "", username: str = "",
               phone: str | None = None, invited_by: int | None = None) -> JoinResult:
    now = datetime.utcnow()
    async with Session() as s:
        g = await s.get(Giveaway, gid)
        if g is None or g.bot_id != bot_id or g.status != "active":
            return JoinResult(False, "closed")
        if g.ends_at and g.ends_at <= now:
            return JoinResult(False, "closed")
        if g.starts_at and g.starts_at > now:
            return JoinResult(False, "not_started")
        ex = await s.scalar(select(GiveawayEntry).where(
            GiveawayEntry.giveaway_id == gid, GiveawayEntry.user_tg == user_tg))
        if ex is not None:
            return JoinResult(False, "exists", ex)
        if g.require_phone:
            if not phone:
                return JoinResult(False, "phone_needed")
            used = await s.scalar(select(GiveawayEntry.id).where(
                GiveawayEntry.giveaway_id == gid, GiveawayEntry.phone == phone))
            if used is not None:
                return JoinResult(False, "phone_used")
        taken = set((await s.scalars(select(GiveawayEntry.code).where(GiveawayEntry.giveaway_id == gid))).all())
        inviter_entry = None
        if invited_by and invited_by != user_tg and g.ref_bonus > 0:
            inviter_entry = await s.scalar(select(GiveawayEntry).where(
                GiveawayEntry.giveaway_id == gid, GiveawayEntry.user_tg == invited_by))
        e = GiveawayEntry(bot_id=bot_id, giveaway_id=gid, user_tg=user_tg, name=(name or "")[:120],
                          username=(username or "")[:64] or None, phone=phone,
                          invited_by=invited_by if (invited_by and invited_by != user_tg) else None,
                          bonus=0, code=gen_code(taken))
        s.add(e)
        inviter_tg, inviter_ch = None, 0
        if inviter_entry is not None:
            cap = int(g.ref_max or 0)
            room = (cap - inviter_entry.bonus) if cap > 0 else int(g.ref_bonus)
            add = max(0, min(int(g.ref_bonus), room))
            if add > 0:
                inviter_entry.bonus = int(inviter_entry.bonus or 0) + add
                inviter_tg, inviter_ch = inviter_entry.user_tg, chances(inviter_entry.bonus)
        await s.commit()
        return JoinResult(True, "", e, inviter_tg, inviter_ch)


async def referrals_count(gid: int, user_tg: int) -> int:
    async with Session() as s:
        return int(await s.scalar(select(func.count(GiveawayEntry.id)).where(
            GiveawayEntry.giveaway_id == gid, GiveawayEntry.invited_by == user_tg)) or 0)


MemberCheck = Callable[[int], Awaitable[Optional[bool]]]


@dataclass
class DrawResult:
    ok: bool
    reason: str = ""
    winners: list[tuple[GiveawayWinner, GiveawayEntry, GiveawayPrize | None]] | None = None
    participants: int = 0
    left: int = 0


async def draw(gid: int, bot_id: int, is_member: MemberCheck | None = None, rng: random.Random | None = None) -> DrawResult:
    """پایانِ قرعه‌کشی: لفت‌دهنده‌ها حذف، برندگان وزنی انتخاب، وضعیت ended. فقط یک‌بار (idempotent)."""
    async with Session() as s:
        g = await s.get(Giveaway, gid)
        if g is None or g.bot_id != bot_id:
            return DrawResult(False, "notfound")
        if g.status != "active":
            return DrawResult(False, "not_active")
        entries = list((await s.scalars(select(GiveawayEntry).where(GiveawayEntry.giveaway_id == gid))).all())
    left = 0
    if is_member is not None:
        for e in entries:
            try:
                ok = await is_member(e.user_tg)
            except Exception:       # noqa: BLE001 — خطا ⇒ حذفش نمی‌کنیم (بی‌گناه حذف نشود)
                logger.exception("giveaway draw: چکِ عضویتِ %s", e.user_tg)
                ok = True
            if ok is False:
                e.left = True
                left += 1
    eligible = {e.id: chances(e.bonus) for e in entries if not e.left}
    picked = weighted_draw(eligible, int(g.winners_count), rng)
    async with Session() as s:
        g2 = await s.get(Giveaway, gid, with_for_update=True)
        if g2 is None or g2.status != "active":          # رقابتِ دو پایان
            return DrawResult(False, "not_active")
        for e in entries:
            if e.left:
                row = await s.get(GiveawayEntry, e.id)
                if row is not None:
                    row.left = True
        prizes = list((await s.scalars(select(GiveawayPrize).where(
            GiveawayPrize.giveaway_id == gid).order_by(GiveawayPrize.rank))).all())
        by_rank = {p.rank: p for p in prizes}
        by_id = {e.id: e for e in entries}
        winners = []
        for i, eid in enumerate(picked, start=1):
            prize = by_rank.get(i) or (prizes[-1] if prizes else None)
            w = GiveawayWinner(bot_id=bot_id, giveaway_id=gid, entry_id=eid, prize_id=prize.id if prize else None, rank=i)
            s.add(w)
            winners.append((w, by_id[eid], prize))
        g2.status = "ended"
        g2.ended_at = datetime.utcnow()
        g2.seed_note = f"{len(eligible)}/{sum(eligible.values())}"[:80]
        await s.commit()
    return DrawResult(True, "", winners, participants=len(entries), left=left)


async def winners_of(gid: int) -> list[tuple[GiveawayWinner, GiveawayEntry, GiveawayPrize | None]]:
    async with Session() as s:
        ws = list((await s.scalars(select(GiveawayWinner).where(
            GiveawayWinner.giveaway_id == gid).order_by(GiveawayWinner.rank))).all())
        out = []
        for w in ws:
            e = await s.get(GiveawayEntry, w.entry_id)
            p = await s.get(GiveawayPrize, w.prize_id) if w.prize_id else None
            out.append((w, e, p))
    return out


async def mark_delivered(winner_id: int, note: str = "") -> None:
    async with Session() as s:
        w = await s.get(GiveawayWinner, winner_id)
        if w is not None:
            w.delivered = True
            w.delivered_at = datetime.utcnow()
            w.note = (note or None) and note[:200]
            await s.commit()


async def cancel(gid: int, bot_id: int) -> bool:
    async with Session() as s:
        g = await s.get(Giveaway, gid)
        if g is None or g.bot_id != bot_id or g.status != "active":
            return False
        g.status = "cancelled"
        g.ended_at = datetime.utcnow()
        await s.commit()
        return True


async def due_for_auto_end(now: datetime | None = None) -> list[tuple[int, int]]:
    """[(giveaway_id, bot_id)] قرعه‌کشی‌هایِ فعالی که ends_at‌شان رسیده."""
    now = now or datetime.utcnow()
    async with Session() as s:
        rows = (await s.execute(select(Giveaway.id, Giveaway.bot_id).where(
            Giveaway.status == "active", Giveaway.ends_at.isnot(None), Giveaway.ends_at <= now))).all()
    return [(r[0], r[1]) for r in rows]
