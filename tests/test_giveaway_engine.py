"""PATCH_GIVEAWAY_BOT_V1 — هستهٔ قرعه‌کشی: کپچا، شماره، شانس، قرعهٔ وزنی، ثبتِ شرکت‌کننده/دعوت، پایان."""
import random
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.bots.giveaway import engine as E

BOT = 5


def test_captcha_always_integer_and_checkable():
    r = random.Random(1)
    for _ in range(300):
        q, a = E.make_captcha(r)
        assert isinstance(a, int) and a >= 0
        left, op, right = q.split(" ")
        l, rr = int(left), int(right)
        assert {"+": l + rr, "-": l - rr, "×": l * rr, "÷": l // rr}[op] == a
        if op == "÷":
            assert l % rr == 0
    assert E.check_captcha("۱۲", 12) and E.check_captcha(" 12 ", 12) and E.check_captcha("١٢", 12)
    assert not E.check_captcha("13", 12) and not E.check_captcha("abc", 12) and not E.check_captcha("", 12)
    assert not E.check_captcha("12 و", 12) and not E.check_captcha(None, 12)


@pytest.mark.parametrize("raw,exp", [
    ("09123456789", "+989123456789"), ("+989123456789", "+989123456789"), ("989123456789", "+989123456789"),
    ("۰۹۱۲۳۴۵۶۷۸۹", "+989123456789"), ("00989123456789", "+989123456789"), ("9123456789", "+989123456789"),
    ("+971501234567", None), ("0212345678", None), ("12345", None), ("", None), (None, None),
])
def test_iran_phone(raw, exp):
    assert E.normalize_iran_phone(raw) == exp


def test_weighted_draw_properties():
    r = random.Random(7)
    w = {1: 1, 2: 1, 3: 1, 4: 50}
    res = E.weighted_draw(w, 2, r)
    assert len(res) == 2 and len(set(res)) == 2
    assert sorted(E.weighted_draw({1: 1, 2: 1}, 5, r)) == [1, 2]            # کمتر از k نفر ⇒ همه
    assert E.weighted_draw({1: 0, 2: -3}, 2, r) == [] and E.weighted_draw({}, 3, r) == []
    first = sum(1 for _ in range(400) if E.weighted_draw(w, 1, r)[0] == 4)   # وزنِ ۵۰ از ۵۳ ⇒ تقریباً همیشه
    assert first > 330


def test_codes_unique_and_shape():
    taken = set()
    for _ in range(200):
        c = E.gen_code(taken)
        assert len(c) == 8 and c == c.upper() and c not in taken
        taken.add(c)
    assert E.chances(0) == 1 and E.chances(3) == 4 and E.chances(None) == 1 and E.chances(-2) == 1


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(E, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=1, token="5:T", username="gw", field="giveaway", plan="pro", status="active"))
        s.add(models.Bot(id=6, owner_id=1, token="6:T", username="gw2", field="giveaway", plan="pro", status="active"))
        await s.commit()
    yield mk
    await eng.dispose()


async def mk_gw(**kw):
    n = kw.pop("n", 2)
    prizes = kw.pop("prizes", [E.NewPrize("50GB"), E.NewPrize("100GB")])
    return await E.create_giveaway(BOT, E.NewGiveaway(title="هدیه", winners_count=n, **kw), prizes)


@pytest.mark.asyncio
async def test_create_makes_one_prize_per_rank_repeating_last(db):
    g = await mk_gw(n=3, prizes=[E.NewPrize("A"), E.NewPrize("B")])
    ps = await E.prizes_of(g.id)
    assert [(p.rank, p.title) for p in ps] == [(1, "A"), (2, "B"), (3, "B")]
    g2 = await mk_gw(n=500)
    assert g2.winners_count == E.MAX_WINNERS


@pytest.mark.asyncio
async def test_join_rules(db):
    g = await mk_gw(require_phone=True)
    r = await E.join(g.id, BOT, 11, "A", "a")
    assert not r.ok and r.reason == "phone_needed"
    r = await E.join(g.id, BOT, 11, "A", "a", phone="+989120000001")
    assert r.ok and len(r.entry.code) == 8
    assert (await E.join(g.id, BOT, 11, "A", "a", phone="+989120000001")).reason == "exists"
    assert (await E.join(g.id, BOT, 12, "B", "b", phone="+989120000001")).reason == "phone_used"
    assert (await E.join(g.id, 6, 13, "C", "c", phone="+989120000003")).reason == "closed"      # ربات دیگر
    assert await E.count_entries(g.id) == 1


@pytest.mark.asyncio
async def test_join_closed_states(db):
    g = await mk_gw()
    await E.cancel(g.id, BOT)
    assert (await E.join(g.id, BOT, 1, "A", "a")).reason == "closed"
    past = await mk_gw(ends_at=datetime.utcnow() - timedelta(minutes=1))
    assert (await E.join(past.id, BOT, 1, "A", "a")).reason == "closed"
    fut = await mk_gw(starts_at=datetime.utcnow() + timedelta(hours=1))
    assert (await E.join(fut.id, BOT, 1, "A", "a")).reason == "not_started"
    assert fut.id not in [x.id for x in await E.active_giveaways(BOT)]


@pytest.mark.asyncio
async def test_referral_bonus_rules(db):
    g = await mk_gw(ref_bonus=2, ref_max=3)
    await E.join(g.id, BOT, 100, "Inviter", "i")
    r = await E.join(g.id, BOT, 101, "X", "x", invited_by=100)
    assert r.inviter_tg == 100 and r.inviter_chances == 3                       # ۱ + ۲
    r = await E.join(g.id, BOT, 102, "Y", "y", invited_by=100)
    assert r.inviter_chances == 4                                               # سقفِ ۳ ⇒ فقط ۱ تای دیگر
    r = await E.join(g.id, BOT, 103, "Z", "z", invited_by=100)
    assert r.inviter_tg is None                                                 # به سقف رسید
    assert (await E.entry_of(g.id, 100)).bonus == 3 and await E.referrals_count(g.id, 100) == 3
    r = await E.join(g.id, BOT, 104, "S", "s", invited_by=104)                  # خوددعوتی
    assert r.inviter_tg is None and r.entry.invited_by is None
    r = await E.join(g.id, BOT, 105, "G", "g", invited_by=999)                  # دعوت‌کننده‌ای که شرکت نکرده
    assert r.ok and r.inviter_tg is None
    g0 = await mk_gw(ref_bonus=0)
    await E.join(g0.id, BOT, 1, "A", "a")
    assert (await E.join(g0.id, BOT, 2, "B", "b", invited_by=1)).inviter_tg is None


@pytest.mark.asyncio
async def test_draw_excludes_left_users_assigns_prizes_and_is_idempotent(db):
    g = await mk_gw(n=2)
    for u in range(1, 6):
        await E.join(g.id, BOT, u, f"u{u}", f"n{u}")

    async def member(uid):                       # ۱ و ۲ لفت دادند، ۳ خطای چک (نباید حذف شود)
        if uid in (1, 2):
            return False
        if uid == 3:
            raise RuntimeError("tg down")
        return True

    res = await E.draw(g.id, BOT, member, random.Random(3))
    assert res.ok and res.participants == 5 and res.left == 2 and len(res.winners) == 2
    tgs = {e.user_tg for _, e, _ in res.winners}
    assert tgs <= {3, 4, 5} and [w.rank for w, _, _ in res.winners] == [1, 2]
    assert [p.title for _, _, p in res.winners] == ["50GB", "100GB"]
    g2 = await E.get_giveaway(g.id)
    assert g2.status == "ended" and g2.ended_at and g2.seed_note == "3/3"
    again = await E.draw(g.id, BOT, member)
    assert not again.ok and again.reason == "not_active"
    assert len(await E.winners_of(g.id)) == 2
    async with db() as s:
        flags = {e.user_tg: e.left for e in (await s.scalars(select(models.GiveawayEntry))).all()}
    assert flags == {1: True, 2: True, 3: False, 4: False, 5: False}


@pytest.mark.asyncio
async def test_draw_fewer_participants_than_winners_and_other_bot(db):
    g = await mk_gw(n=5)
    await E.join(g.id, BOT, 1, "a", "a")
    assert not (await E.draw(g.id, 6)).ok
    res = await E.draw(g.id, BOT)
    assert res.ok and len(res.winners) == 1


@pytest.mark.asyncio
async def test_draw_with_zero_entries_ends_cleanly(db):
    g = await mk_gw()
    res = await E.draw(g.id, BOT)
    assert res.ok and res.winners == [] and res.participants == 0


@pytest.mark.asyncio
async def test_bonus_raises_win_probability(db):
    g = await mk_gw(n=1, ref_bonus=1)
    for u in range(1, 11):
        await E.join(g.id, BOT, u, f"u{u}", f"n{u}")
    async with db() as s:
        e1 = await s.scalar(select(models.GiveawayEntry).where(models.GiveawayEntry.user_tg == 1))
        e1.bonus = 90                                               # ۹۱ از ۱۰۰ شانس
        await s.commit()
    wins = 0
    for seed in range(60):
        g2 = await E.create_giveaway(BOT, E.NewGiveaway(title="t", winners_count=1), [E.NewPrize("p")])
        async with db() as s:
            for e in (await s.scalars(select(models.GiveawayEntry).where(models.GiveawayEntry.giveaway_id == g.id))).all():
                s.add(models.GiveawayEntry(bot_id=BOT, giveaway_id=g2.id, user_tg=e.user_tg, code=f"C{e.user_tg}{seed}",
                                           bonus=e.bonus))
            await s.commit()
        res = await E.draw(g2.id, BOT, None, random.Random(seed))
        wins += 1 if res.winners[0][1].user_tg == 1 else 0
    assert wins > 40


@pytest.mark.asyncio
async def test_auto_end_due_and_mark_delivered(db):
    due = await mk_gw(ends_at=datetime.utcnow() - timedelta(seconds=5))
    later = await mk_gw(ends_at=datetime.utcnow() + timedelta(hours=1))
    none = await mk_gw()
    d = await E.due_for_auto_end()
    assert (due.id, BOT) in d and (later.id, BOT) not in d and (none.id, BOT) not in d
    await E.join(none.id, BOT, 1, "a", "a")
    res = await E.draw(none.id, BOT)
    w = res.winners[0][0]
    await E.mark_delivered(w.id, "ok")
    assert (await E.winners_of(none.id))[0][0].delivered is True
    assert not await E.cancel(none.id, BOT)                          # قبلاً پایان یافته
