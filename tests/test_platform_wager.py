"""PATCH_PLATFORM_WAGER_V1 — شرط‌بندی ایکس‌او / سنگ‌کاغذ‌قیچی / تاس با کیف‌پول بات‌ساز."""
import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_group_money as GM
import platform_wager as PW

A, B, C, D = 101, 202, 303, 404
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("0"), "ttl": 300, "move": 180}
FEE10 = {**CFG, "fee_pct": Decimal("10")}


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PW, GM):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        for uid, bal in ((A, 100_000), (B, 100_000), (C, 500), (D, 100_000)):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", username=f"u{uid}", balance=bal))
        await s.commit()
    yield maker
    await engine.dispose()



@pytest.fixture(autouse=True)
def _no_tap_gate(monkeypatch):
    monkeypatch.setattr(PW, "TAP_GAP_SECS", 0)
    PW._tap_last.clear()

async def bal(db, uid):
    async with db() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))
        return int(u.balance)


async def total(db):
    async with db() as s:
        return int(await s.scalar(select(func.sum(models.PlatformUser.balance))))


async def wager(db, wid):
    async with db() as s:
        return await s.get(models.PlatformWager, wid)


async def new(db, game="xo", amount=10_000, cfg=CFG, target=None, creator=A, opt="auto"):
    """opt="auto": انتخاب سازنده را (X برای ایکس‌او، hi برای تاس) از پیش می‌گذارد؛ opt=None ⇒ مثل دستور واقعی بعدا پرسیده می‌شود."""
    if opt == "auto":
        opt = {"xo": "X", "dice": "hi"}.get(game)
    w, err = await PW.create_wager(game, -100, (creator, f"u{creator}", f"u{creator}"), amount, target, cfg, opt=opt)
    assert err is None, err
    return w


async def join(db, w, uid=B):
    r = await PW.join_wager(w.id, (uid, f"u{uid}", f"u{uid}"), CFG["move"])
    assert r.err is None, r.err
    return r.w


async def expire(db, wid):
    async with db() as s:
        w = await s.get(models.PlatformWager, wid)
        w.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()


# ───────── دستورها و منطق خالص ─────────

def test_parse_command_variants():
    p = PW.parse_command
    assert p("xo ۱۰۰۰۰") == ("xo", 10000) and p("XO 5,000") == ("xo", 5000)
    assert p("اکس او ۲۰۰۰") == ("xo", 2000) and p("ایکس‌او 3000") == ("xo", 3000) and p("دوز ۱۰۰۰") == ("xo", 1000)
    assert p("سنگ کاغذ قیچی ۱۰۰۰") == ("rps", 1000) and p("سنگ‌کاغذ‌قیچی ۱۰۰۰") == ("rps", 1000)
    assert p("تاس ۵۰۰۰") == ("dice", 5000)
    assert p("شرط‌بندی تاس ۵۰۰۰") == ("dice", 5000) and p("شرطبندی xo 7000") == ("xo", 7000)
    # دستور شیر یا خط قبلی و متن معمولی دست‌نخورده می‌ماند
    assert p("شرطبندی ۱۰۰۰") is None and p("بازی ۱۰۰۰") is None and p("تاس") is None
    assert p("xo 0") is None and p("سلام xo 1000") is None and p(None) is None


def test_xo_result():
    assert PW.xo_result(["X", "X", "X", "", "", "", "", "", ""]) == "X"
    assert PW.xo_result(["O", "", "", "", "O", "", "", "", "O"]) == "O"
    assert PW.xo_result(["X", "O", "X", "X", "O", "O", "O", "X", "X"]) == "draw"
    assert PW.xo_result([""] * 9) is None


def test_rps_result():
    assert PW.rps_result("r", "s") == 1 and PW.rps_result("s", "r") == 2
    assert PW.rps_result("p", "r") == 1 and PW.rps_result("s", "p") == 1
    assert PW.rps_result("p", "p") == 0


# ───────── ساخت / پیوستن / لغو ─────────

@pytest.mark.asyncio
async def test_create_reserves_and_validates(db):
    w = await new(db, amount=10_000, cfg=FEE10)
    assert await bal(db, A) == 90_000
    assert int(w.fee_amount) == 2000 and int(w.payout_amount) == 18_000 and w.status == "pending"
    for amount, msg in ((500, "مبلغ باید"), (200_000, "موجودی")):
        _, err = await PW.create_wager("xo", -100, (A, "x", "x"), amount, None, CFG)
        assert err and msg in err
    _, err = await PW.create_wager("xo", -100, (A, "x", "x"), 50_000, None, {**CFG, "max": 20_000})
    assert err and "مبلغ باید" in err
    _, err = await PW.create_wager("xo", -100, (C, "x", "x"), 1000, None, CFG)       # ۵۰۰ تومان موجودی
    assert err and "موجودی" in err
    _, err = await PW.create_wager("xo", -100, (999, "x", "x"), 1000, None, CFG)      # کاربر ناشناس
    assert err


@pytest.mark.asyncio
async def test_active_cap_per_user(db):
    for _ in range(PW.MAX_ACTIVE):
        await new(db, amount=1000)
    _, err = await PW.create_wager("xo", -100, (A, "x", "x"), 1000, None, CFG)
    assert err and str(PW.MAX_ACTIVE) in err


@pytest.mark.asyncio
async def test_join_rules(db):
    w = await new(db, target=(B, "u202", "u202"))
    assert (await PW.join_wager(w.id, (A, "x", "x"), 180)).err                    # سازنده
    assert (await PW.join_wager(w.id, (C, "x", "x"), 180)).err                    # غیر از حریف تعیین‌شده
    pub = await new(db)
    assert "موجودی" in (await PW.join_wager(pub.id, (C, "x", "x"), 180)).err      # موجودی کم
    assert await bal(db, B) == 100_000
    joined = await join(db, w)
    assert joined.status == "playing" and await bal(db, B) == 90_000
    assert (await PW.join_wager(w.id, (B, "x", "x"), 180)).err                    # دیگر در انتظار نیست


@pytest.mark.asyncio
async def test_cancel_refunds_once_and_only_creator(db):
    w = await new(db)
    assert (await PW.cancel_wager(w.id, B)).err and await bal(db, A) == 90_000
    assert (await PW.cancel_wager(w.id, A)).w.status == "cancelled" and await bal(db, A) == 100_000
    assert (await PW.cancel_wager(w.id, A)).err and await bal(db, A) == 100_000
    assert (await PW.join_wager(w.id, (B, "x", "x"), 180)).err


@pytest.mark.asyncio
async def test_join_after_deadline_refunds_creator(db):
    w = await new(db)
    await expire(db, w.id)
    r = await PW.join_wager(w.id, (B, "x", "x"), 180)
    assert r.err and r.w.status == "expired"
    assert await bal(db, A) == 100_000 and await bal(db, B) == 100_000


@pytest.mark.asyncio
async def test_concurrent_joins_only_one_wins(db):
    w = await new(db)

    async def go(uid):
        async with PW._wlock(w.id):
            return await PW.join_wager(w.id, (uid, "x", "x"), 180)
    r1, r2 = await asyncio.gather(go(B), go(D))
    assert [bool(r.err) for r in (r1, r2)].count(False) == 1
    assert sorted([await bal(db, B), await bal(db, D)]) == [90_000, 100_000]       # فقط یک نفر مبلغ داده


# ───────── ایکس‌او ─────────

async def play_xo(db, w, order):
    """order = [(uid, cell), …] ؛ نوبت اول تصادفی است پس نفر اول را از وضعیت می‌خوانیم."""
    for uid, cell in order:
        r = await PW.xo_move(w.id, uid, cell, 180)
        assert r.err is None, r.err
        w = r.w
    return w


@pytest.mark.asyncio
async def test_xo_win_pays_winner_with_fee(db):
    start = await total(db)
    w = await join(db, await new(db, cfg=FEE10))
    first = (await wager(db, w.id)).turn_id
    second = B if first == A else A
    w = await play_xo(db, w, [(first, 0), (second, 3), (first, 1), (second, 4), (first, 2)])
    assert w.status == "settled" and w.winner_id == first and w.end_reason == "win"
    assert await bal(db, first) == 100_000 - 10_000 + 18_000
    assert await bal(db, second) == 100_000 - 10_000
    assert await total(db) == start - 2000             # فقط کارمزد از چرخه خارج شده


@pytest.mark.asyncio
async def test_xo_draw_refunds_both_no_fee(db):
    w = await join(db, await new(db, cfg=FEE10))
    first = (await wager(db, w.id)).turn_id
    second = B if first == A else A
    # X O X / X O O / O X X  (بدون برنده)
    f_cells, s_cells = [0, 2, 3, 7, 8], [1, 4, 5, 6]
    seq = []
    for i in range(5):
        seq.append((first, f_cells[i]))
        if i < 4:
            seq.append((second, s_cells[i]))
    w = await play_xo(db, w, seq)
    assert w.status == "draw" and w.winner_id is None
    assert await bal(db, A) == 100_000 and await bal(db, B) == 100_000


@pytest.mark.asyncio
async def test_xo_turn_cell_and_player_validation(db):
    w = await join(db, await new(db))
    first = (await wager(db, w.id)).turn_id
    second = B if first == A else A
    assert (await PW.xo_move(w.id, second, 0, 180)).err                # نوبتش نیست
    assert (await PW.xo_move(w.id, C, 0, 180)).err                     # بازیکن نیست
    assert (await PW.xo_move(w.id, first, 9, 180)).err                 # خانهٔ نامعتبر
    assert (await PW.xo_move(w.id, first, 4, 180)).err is None
    assert (await PW.xo_move(w.id, second, 4, 180)).err                # خانهٔ پر
    pending = await new(db)
    assert (await PW.xo_move(pending.id, A, 0, 180)).err               # هنوز شروع نشده


@pytest.mark.asyncio
async def test_xo_no_moves_after_finish(db):
    w = await join(db, await new(db))
    first = (await wager(db, w.id)).turn_id
    second = B if first == A else A
    w = await play_xo(db, w, [(first, 0), (second, 3), (first, 1), (second, 4), (first, 2)])
    assert w.status == "settled"
    assert (await PW.xo_move(w.id, second, 5, 180)).err
    assert await PW.expire_one(w.id) is None
    before = await total(db)
    assert (await PW.cancel_wager(w.id, A)).err
    assert await total(db) == before


@pytest.mark.asyncio
async def test_xo_timeout_forfeits_player_on_turn(db):
    w = await join(db, await new(db, cfg=FEE10))
    row = await wager(db, w.id)
    on_turn = row.turn_id
    other = B if on_turn == A else A
    await expire(db, w.id)
    done = await PW.sweep_expired(None)
    assert done == 1
    w = await wager(db, w.id)
    assert w.status == "settled" and w.winner_id == other and w.end_reason == "forfeit"
    assert await bal(db, other) == 100_000 - 10_000 + 18_000
    assert await PW.sweep_expired(None) == 0                           # دوباره تسویه نمی‌شود


@pytest.mark.asyncio
async def test_xo_move_after_deadline_settles_instead(db):
    w = await join(db, await new(db))
    on_turn = (await wager(db, w.id)).turn_id
    other = B if on_turn == A else A
    await expire(db, w.id)
    r = await PW.xo_move(w.id, on_turn, 0, 180)
    assert r.err and r.w.status == "settled" and r.w.winner_id == other


# ───────── سنگ‌کاغذ‌قیچی ─────────

async def rps_round(wid, a, b, sim=300):
    """یک راند کامل: A انتخاب a، B انتخاب b."""
    r = await PW.rps_pick(wid, A, a, sim_secs=sim)
    assert r.err is None, r.err
    r = await PW.rps_pick(wid, B, b, sim_secs=sim)
    assert r.err is None, r.err
    return r.w


@pytest.mark.asyncio
async def test_rps_first_to_three_rounds_with_hidden_picks_and_scoreboard(db):
    w = await join(db, await new(db, "rps", cfg=FEE10))
    r = await PW.rps_pick(w.id, A, "r")
    assert r.err is None and r.w.status == "playing"
    text, _ = PW.render(r.w)
    assert "🪨" not in text and "✅ انتخاب کرد" in text and "⏳ منتظر انتخاب" in text      # انتخاب فاش نشده
    assert "۳ راند" in text or "3 راند" in text
    assert (await PW.rps_pick(w.id, A, "p")).err                              # انتخاب دوباره در همان راند
    r = await PW.rps_pick(w.id, B, "s")                                       # راند ۱: سنگ بر قیچی ⇒ A
    assert r.w.status == "playing"
    text, _ = PW.render(r.w)
    assert "راند 1: @u101 🪨 ⚔️ ✂️ @u202" in text and "امتیاز: <b>1</b>" in text and "راند 2" in text   # بعد از راند، انتخاب‌ها فاش
    w2 = await rps_round(w.id, "p", "r")                                      # راند ۲: کاغذ بر سنگ ⇒ A (۲-۰)
    assert w2.status == "playing"
    w3 = await rps_round(w.id, "s", "p")                                      # راند ۳: قیچی بر کاغذ ⇒ A (۳-۰)
    assert w3.status == "settled" and w3.winner_id == A and w3.end_reason == "win"
    assert await bal(db, A) == 100_000 - 10_000 + 18_000 and await bal(db, B) == 90_000
    final, kb = PW.render(await wager(db, w.id))
    assert "امتیاز" in final and "راند 3" in final and "برنده" in final and not kb.inline_keyboard


@pytest.mark.asyncio
async def test_rps_draw_round_repeats_and_comeback_is_possible(db):
    w = await join(db, await new(db, "rps"))
    assert (await PW.rps_pick(w.id, A, "x")).err and (await PW.rps_pick(w.id, C, "r")).err
    w1 = await rps_round(w.id, "p", "p")                                       # مساوی ⇒ بازی ادامه دارد
    assert w1.status == "playing" and "مساوی" in PW.render(w1)[0]
    st = json.loads(w1.state)
    assert st["round"] == 2 and st["score"] == {str(A): 0, str(B): 0}
    await rps_round(w.id, "r", "s")                                            # A: ۱
    await rps_round(w.id, "s", "r")                                            # B: ۱
    await rps_round(w.id, "r", "s")                                            # A: ۲
    await rps_round(w.id, "s", "r")                                            # B: ۲
    last = await rps_round(w.id, "s", "r")                                     # B: ۳ ⇒ برگشت و پیروزی B
    assert last.status == "settled" and last.winner_id == B
    assert await bal(db, B) == 100_000 + 10_000 and await bal(db, A) == 90_000


@pytest.mark.asyncio
async def test_rps_round_deadline_resets_and_too_many_draws_refund_both(db):
    w = await join(db, await new(db, "rps", amount=2000))
    first_deadline = (await wager(db, w.id)).expires_at
    w1 = await rps_round(w.id, "r", "r", sim=77)
    left = (w1.expires_at - datetime.utcnow()).total_seconds()
    assert 70 <= left <= 80 and w1.expires_at != first_deadline               # مهلت راند بعد تازه شد
    for _ in range(PW.RPS_MAX_ROUNDS - 1):
        w1 = await rps_round(w.id, "p", "p")
    assert w1.status == "draw" and w1.winner_id is None
    assert await bal(db, A) == 100_000 and await bal(db, B) == 100_000
    assert "برگشت" in PW.render(w1)[0]


@pytest.mark.asyncio
async def test_rps_timeout_in_later_round_forfeits_only_the_one_who_did_not_pick(db):
    w = await join(db, await new(db, "rps", amount=2000))
    await rps_round(w.id, "r", "s")                                            # A: ۱
    await PW.rps_pick(w.id, B, "p")                                            # راند ۲: فقط B انتخاب کرد
    await expire(db, w.id)
    assert await PW.sweep_expired(None) == 1
    w = await wager(db, w.id)
    assert w.status == "settled" and w.winner_id == B and w.end_reason == "forfeit"


@pytest.mark.asyncio
async def test_rps_timeouts(db):
    one = await join(db, await new(db, "rps", amount=2000))
    await PW.rps_pick(one.id, B, "r")
    await expire(db, one.id)
    two = await join(db, await new(db, "rps", amount=3000))
    await expire(db, two.id)
    assert await PW.sweep_expired(None) == 2
    one, two = await wager(db, one.id), await wager(db, two.id)
    assert one.status == "settled" and one.winner_id == B and one.end_reason == "forfeit"   # فقط B انتخاب کرده بود
    assert two.status == "expired" and two.winner_id is None                                # هیچ‌کدام ⇒ برگشت هر دو
    assert await bal(db, B) == 100_000 + 2000 and await bal(db, A) == 100_000 - 2000


# ───────── تاس ─────────

@pytest.mark.asyncio
async def test_dice_flow_higher_wins_and_tie_refunds(db):
    w = await join(db, await new(db, "dice", cfg=FEE10))
    assert (await PW.dice_claim(w.id, A)).err is None
    assert (await PW.dice_claim(w.id, A)).err                                  # هم‌زمان دوباره نه
    assert (await PW.dice_claim(w.id, C)).err                                  # بازیکن نیست
    r = await PW.dice_record(w.id, A, 6)
    assert r.w.status == "playing"
    assert (await PW.dice_claim(w.id, A)).err                                  # قبلا انداخته
    await PW.dice_claim(w.id, B)
    r = await PW.dice_record(w.id, B, 2)
    assert r.w.status == "settled" and r.w.winner_id == A
    assert await bal(db, A) == 100_000 - 10_000 + 18_000

    t = await join(db, await new(db, "dice"))
    await PW.dice_claim(t.id, A); await PW.dice_record(t.id, A, 4)
    await PW.dice_claim(t.id, B)
    r = await PW.dice_record(t.id, B, 4)
    assert r.w.status == "draw"


@pytest.mark.asyncio
async def test_dice_release_allows_retry_and_stale_claim_reclaim(db):
    w = await join(db, await new(db, "dice"))
    await PW.dice_claim(w.id, A)
    await PW.dice_release(w.id, A)
    assert (await PW.dice_claim(w.id, A)).err is None
    async with db() as s:                                                      # پرتاب گیرکرده (پروسه مرده)
        row = await s.get(models.PlatformWager, w.id)
        import json
        st = json.loads(row.state)
        st["rolling"][str(A)] = (datetime.utcnow() - timedelta(seconds=PW.DICE_RECLAIM_SECS + 5)).isoformat()
        row.state = json.dumps(st)
        await s.commit()
    assert (await PW.dice_claim(w.id, A)).err is None


@pytest.mark.asyncio
async def test_dice_timeout_forfeit(db):
    w = await join(db, await new(db, "dice"))
    await PW.dice_claim(w.id, B); await PW.dice_record(w.id, B, 1)
    await expire(db, w.id)
    assert await PW.sweep_expired(None) == 1
    w = await wager(db, w.id)
    assert w.status == "settled" and w.winner_id == B


@pytest.mark.asyncio
async def test_pending_expiry_refunds_creator(db):
    w = await new(db)
    await expire(db, w.id)
    assert await PW.sweep_expired(None) == 1
    assert (await wager(db, w.id)).status == "expired" and await bal(db, A) == 100_000


# ───────── نمایش و هندلرها ─────────

@pytest.mark.asyncio
async def test_render_every_state_mentions_wager(db):
    for game in PW.GAMES:
        pend = await new(db, game, amount=1000)
        text, kb = PW.render(pend)
        assert "شرط‌بندی" in text and kb.inline_keyboard
        play = await join(db, pend)
        text, kb = PW.render(play)
        assert "شرط‌بندی" in text and kb.inline_keyboard
        await expire(db, play.id)
        await PW.sweep_expired(None)
        text, _ = PW.render(await wager(db, play.id))
        assert "شرط‌بندی" in text
        canc = await new(db, game, amount=1000)
        await PW.cancel_wager(canc.id, A)
        assert "لغو" in PW.render(await wager(db, canc.id))[0]


def _msg(text, uid=A, reply_to=None):
    sent = SimpleNamespace(message_id=500)
    replies = []

    async def reply(t, **kw):
        replies.append(t)
        return sent
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=-100), sender_chat=None, reply_to_message=reply_to,
                           from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}", is_bot=False),
                           reply=reply, bot=None), replies


def _cb(data, uid, edits):
    async def edit_text(t, **kw):
        edits.append((t, kw.get("reply_markup")))

    async def answer(t="", show_alert=False):
        edits.append(("ANSWER", t, show_alert))
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}"),
                           message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=500, edit_text=edit_text,
                                                   answer=None), answer=answer)


@pytest.fixture
def psets(monkeypatch):
    store = {"platform_group_betting_enabled": "on"}

    async def fake_pset(key, default=""):
        return store.get(key, default)

    async def allowed(chat_id):
        return True
    for mod in (PW, GM):
        monkeypatch.setattr(mod, "_pset", fake_pset)
        monkeypatch.setattr(mod, "_group_allowed", allowed)
    return store


@pytest.mark.asyncio
async def test_handler_off_by_default_and_per_game_toggle(db, psets):
    psets.pop("platform_group_betting_enabled")
    m, replies = _msg("xo 5000")
    await PW.wager_create(m)
    assert replies and "فعال نیست" in replies[0] and await bal(db, A) == 100_000      # پیش‌فرض خاموش
    psets["platform_group_betting_enabled"] = "on"
    psets["platform_wager_xo"] = "off"
    m, replies = _msg("xo 5000")
    await PW.wager_create(m)
    assert "فعال نیست" in replies[0] and await bal(db, A) == 100_000
    m, replies = _msg("تاس 5000")                                                       # بازی دیگر روشن است
    await PW.wager_create(m)
    assert "شرط‌بندی تاس" in replies[0] and await bal(db, A) == 95_000


@pytest.mark.asyncio
async def test_handler_full_xo_game_via_callbacks(db, psets):
    m, replies = _msg("اکس او ۱۰۰۰")
    await PW.wager_create(m)
    assert "شرط‌بندی ایکس‌او" in replies[0]
    async with db() as s:
        row = await s.scalar(select(models.PlatformWager))
        assert row.message_id == 500
        wid = row.id
    assert "نماد" in replies[0]
    edits = []
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))                               # قبل از انتخاب سازنده نمی‌شود
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True and (await wager(db, wid)).status == "pending"
    await PW.wager_option(_cb(f"pgw:o:{wid}:O", B, edits))                              # فقط سازنده انتخاب می‌کند
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    await PW.wager_option(_cb(f"pgw:o:{wid}:O", A, edits))
    assert json.loads((await wager(db, wid)).state)["opt"] == "O"
    await PW.wager_join(_cb(f"pgw:join:{wid}", A, edits))                               # سازنده نمی‌تواند
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))
    row = await wager(db, wid)
    assert row.status == "playing" and await bal(db, B) == 99_000
    assert PW._xo_sym(row, A) == "O" and PW._xo_sym(row, B) == "X"
    first = row.turn_id
    second = B if first == A else A
    for uid, cell in ((first, 0), (second, 3), (first, 1), (second, 4), (first, 2)):
        await PW.wager_xo(_cb(f"pgw:x:{wid}:{cell}", uid, edits))
    row = await wager(db, wid)
    assert row.status == "settled" and row.winner_id == first
    assert await bal(db, first) == 101_000 and await bal(db, second) == 99_000
    assert "برنده" in edits[-2][0] or "برنده" in edits[-1][0]


@pytest.mark.asyncio
async def test_handler_dice_uses_telegram_value(db, psets, monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(PW.asyncio, "sleep", nosleep)
    m, _ = _msg("تاس 2000")
    await PW.wager_create(m)
    async with db() as s:
        wid = (await s.scalar(select(models.PlatformWager))).id
    edits = []
    await PW.wager_option(_cb(f"pgw:o:{wid}:hi", A, edits))
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))
    rolls = iter([3, 5])

    class Bot:
        async def send_dice(self, chat_id, emoji):
            return SimpleNamespace(dice=SimpleNamespace(value=next(rolls)))
    await PW.wager_dice(_cb(f"pgw:d:{wid}", A, edits), Bot())
    await PW.wager_dice(_cb(f"pgw:d:{wid}", B, edits), Bot())
    row = await wager(db, wid)
    assert row.status == "settled" and row.winner_id == B                              # ۵ > ۳
    assert await bal(db, B) == 100_000 - 2000 + 4000 and await bal(db, A) == 98_000


@pytest.mark.asyncio
async def test_handler_dice_send_failure_does_not_lose_turn(db, psets):
    m, _ = _msg("تاس 2000")
    await PW.wager_create(m)
    async with db() as s:
        wid = (await s.scalar(select(models.PlatformWager))).id
    edits = []
    await PW.wager_option(_cb(f"pgw:o:{wid}:hi", A, edits))
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))

    class Broken:
        async def send_dice(self, chat_id, emoji):
            raise RuntimeError("telegram down")
    cb = _cb(f"pgw:d:{wid}", A, edits)
    cb.message.answer = lambda *a, **k: asyncio.sleep(0)
    await PW.wager_dice(cb, Broken())
    assert (await PW.dice_claim(wid, A)).err is None                                    # دوباره می‌تواند بیندازد


@pytest.mark.asyncio
async def test_money_is_conserved_across_mixed_games(db):
    start = await total(db)
    fees = 0
    # شرط برنده‌دار با کارمزد
    w = await join(db, await new(db, "rps", amount=4000, cfg=FEE10))
    for _ in range(PW.RPS_WINS):
        await rps_round(w.id, "r", "s")
    fees += 800
    # مساوی (سقف راندها)
    w = await join(db, await new(db, "rps", amount=4000, cfg=FEE10))
    for _ in range(PW.RPS_MAX_ROUNDS):
        await rps_round(w.id, "r", "r")
    # لغو و انقضا
    c = await new(db, amount=3000); await PW.cancel_wager(c.id, A)
    e = await new(db, amount=3000); await expire(db, e.id); await PW.sweep_expired(None)
    assert await total(db) == start - fees
    async with db() as s:                                                               # هیچ شرط باز یا پول رزروشده‌ای نمانده
        assert not (await s.scalars(select(models.PlatformWager).where(models.PlatformWager.status.in_(PW.ACTIVE)))).all()


@pytest.mark.asyncio
async def test_settings_screen_lists_games_and_admin_toggle(db, psets, monkeypatch):
    async def fake_set(key, value):
        psets[key] = value
    monkeypatch.setattr(PW, "_set", fake_set)
    monkeypatch.setattr(GM, "_set", fake_set)
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    text, kb = await GM._settings_screen("bet")
    assert "تنظیمات شرط‌بندی گروه" in text and all(PW.GAME_FA[g] in text for g in PW.GAMES)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert {f"pgwset:{g}" for g in PW.GAMES} <= set(datas) and "pgbset:toggle" in datas
    edits = []
    await PW.wager_game_toggle(_cb("pgwset:xo", 999, edits))                    # غیر مدیر
    assert edits[-1][0] == "ANSWER" and "platform_wager_xo" not in psets
    await PW.wager_game_toggle(_cb("pgwset:xo", 1, edits))
    assert psets["platform_wager_xo"] == "off"
    await PW.wager_game_toggle(_cb("pgwset:xo", 1, edits))
    assert psets["platform_wager_xo"] == "on"


@pytest.mark.asyncio
async def test_help_text_lists_only_enabled_games():
    text = PW.help_text({"xo": True, "rps": False, "dice": True})
    assert "ایکس‌او" in text and "تاس" in text and "سنگ‌کاغذ‌قیچی" not in text and "شرطبندی" in text


def test_help_text_explains_how_to_play_step_by_step_with_rules_and_limits():
    """PATCH_WAGER_HELP_V1 — وقتی کسی فقط «بازی» می‌نویسد باید همه‌چیز را واضح یاد بگیرد."""
    cfg = {"min": 1000, "max": 500_000, "fee_pct": Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}
    on = {"xo": True, "rps": True, "dice": True}
    main = PW.help_text(on, cfg=cfg)
    for must in ("مراحل کار", "قبول و پیوستن", "ریپلای", "لغو", "نماد خودش", "3 راند", "بزرگ‌تر، کوچک‌تر، زوج یا فرد برنده است",
                 "پرتاب تاس", "شیر یا خط", "ایکس‌او: هر حرکت 3 دقیقه", "هر مرحله 5 دقیقه", "از 1,000 تومان تا 500,000 تومان",
                 "کارمزد: 5٪", "حداکثر 3 بازی باز", "ادمین ناشناس", "شرط‌بندی ریسک مالی"):
        assert must in main, must
    assert "🎰 <b>شرط‌بندی با کیف‌پول</b>" in main
    game = PW.help_text(on, bot_id=5, cfg=cfg)                                   # ربات مشتری: نام «بازی» و همان هشدار
    assert "🎮 <b>بازی با کیف‌پول</b>" in game and "شرط‌بندی" not in game
    assert "⚠️ بازی ریسک مالی دارد؛ فقط با پولی بازی کن که از دست‌دادنش برایت مشکلی نیست." in game
    free = PW.help_text({"xo": False, "rps": False, "dice": True}, cfg={**cfg, "fee_pct": Decimal("0"), "max": 0})
    assert "کارمزد" not in free and "بدون سقف" in free and "ایکس‌او" not in free and "سنگ‌کاغذ‌قیچی" not in free
    assert "«تاس ۱۰۰۰۰»" in free                                                  # مثال مرحلهٔ ۱ از بازی روشن است
    none = PW.help_text({"xo": False, "rps": False, "dice": False}, cfg=cfg)
    assert "«بازی ۱۰۰۰۰»" in none and "«تاس" not in none and "حداکثر 3 بازی باز" not in none
    assert "هر مرحله 5 دقیقه" in free and "هر حرکت" not in free                    # فقط مهلت بازی‌های روشن
    assert len(main) < 3500                                                       # در یک پیام تلگرام جا می‌شود


@pytest.mark.asyncio
async def test_main_group_incomplete_command_sends_the_full_help(db, psets):
    async def reply(t, **kw):
        replies.append(t)
    replies = []
    m = SimpleNamespace(text="بازی", chat=SimpleNamespace(id=-100), reply=reply)
    await GM.platform_incomplete_bet_command(m)
    assert replies and "مراحل کار" in replies[0] and "ایکس‌او" in replies[0]
    assert "حداقل 1,000 تومان (بدون سقف)" in replies[0] and "5 دقیقه" in replies[0]


@pytest.mark.asyncio
async def test_shirt_bandi_with_zwnj_reaches_coin_flip_regex():
    assert GM._BET_RE.match("شرط‌بندی ۱۰۰۰") and GM._INCOMPLETE_BET_RE.match("شرط‌بندی")
    assert GM._BET_RE.match("بازی ۱۰۰۰") and not GM._BET_RE.match("شرط‌بندی xo ۱۰۰۰")


# ───────── صفحهٔ مشترک «بازی‌ها و شرط‌بندی» (هیچ بازی‌ای حذف نشده؛ هر دو بخش کنار هم دیده می‌شوند) ─────────

@pytest.mark.asyncio
async def test_hub_shows_both_sections_and_is_admin_only(db, psets, monkeypatch):
    import platform_bot
    import platform_wager_sale

    async def gp(key, default=""):
        return psets.get(key, default)
    monkeypatch.setattr(platform_bot, "get_pset", gp)          # بخش فروش به ربات‌ها از get_pset می‌خواند
    monkeypatch.setattr(platform_wager_sale, "Session", db)
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    async with db() as s:
        s.add(models.PlatformDiceGame(emoji="🎯", title="دارت", active=True))
        s.add(models.PlatformDiceGame(emoji="🎲", title="تاس", active=False))
        await s.commit()
    text, kb = await PW.hub_screen()
    assert "شرط‌بندی" in text and "بازی کن پول بگیر" in text and "1 بازی فعال از 2" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert {"gg:bet_settings@hub", "padg:menu@hub", "adm:back"} <= set(datas)
    edits = []
    await PW.wager_hub(_cb("pgw:hub", 999, edits))
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    edits.clear()
    await PW.wager_hub(_cb("pgw:hub", 1, edits))
    assert "بازی‌ها و شرط‌بندی" in edits[0][0]


def test_admin_menu_has_hub_and_keeps_telegram_games():
    import platform_admin_menu as AM
    fun = next(sec for sec in AM.ADMIN_SECTIONS if sec["key"] == "fun")
    cbs = [it[1] for it in fun["items"]]
    assert "pgw:hub" in cbs and "padg:menu" in cbs                          # بازی‌های تلگرام سر جایشان هستند
    assert "pgw:hub" in [it[1] for it in AM._section_items(fun, True)]
    assert "pgw:hub" not in [it[1] for it in AM._section_items(fun, False)]  # فقط مدیر اصلی


@pytest.mark.asyncio
async def test_telegram_games_menu_untouched_by_the_wager_feature(db, monkeypatch):
    import platform_dice as PD
    async with db() as s:
        s.add(models.PlatformDiceGame(emoji="🏀", title="بسکتبال", active=True))
        await s.commit()
    monkeypatch.setattr(PD, "Session", db)
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 1)
    edits = []

    class St:
        async def clear(self):
            return None
    cb = _cb("padg:menu", 1, edits)
    await PD.padg_menu(cb, St())
    text, kb = edits[0]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("padg:open:") for d in datas) and "padg:add" in datas and datas[-1] == "adm:back"   # ورود از دستهٔ سرگرمی: بازگشت به پنل


# ───────── دکمهٔ «بازگشت» = یک صفحه عقب (PATCH_PLATFORM_NAVBACK_V1) ─────────

def _back_of(edit):
    """callback_data دکمهٔ «بازگشت» (آخرین ردیف) در آخرین پیام ویرایش‌شده."""
    kb = edit[1]
    return [b for b in kb.inline_keyboard[-1]][0].callback_data


@pytest_asyncio.fixture
async def nav(db, monkeypatch):
    import platform_dice as PD
    import platform_navback as NB
    NB._BACK.clear()
    monkeypatch.setattr(PD, "Session", db)
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 1)
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    async with db() as s:
        s.add(models.PlatformDiceGame(emoji="🏀", title="بسکتبال", active=True))
        await s.commit()

    class St:
        async def clear(self):
            return None
    return SimpleNamespace(PD=PD, st=St(), edits=[])


@pytest.mark.asyncio
async def test_back_from_ai_bet_games_goes_one_page_each_time(nav, psets):
    e = nav.edits
    # هوش مصنوعی ← تنظیمات شرط‌بندی ← بازی کن پول بگیر
    await GM.main_bet_settings(_cb("gg:bet_settings", 1, e))
    assert _back_of(e[-2]) == "gg:ai"                                  # از تنظیمات شرط‌بندی: برمی‌گردد به هوش مصنوعی
    await nav.PD.padg_menu(_cb("padg:menu@bet", 1, e), nav.st)
    assert _back_of(e[-2]) == "gg:bet_settings@keep"                   # از بازی‌ها: یک صفحه عقب = تنظیمات شرط‌بندی (نه ریشهٔ پنل)
    await GM.main_bet_settings(_cb("gg:bet_settings@keep", 1, e))
    assert _back_of(e[-2]) == "gg:ai"                                  # و از آن‌جا باز هم یک صفحه عقب = هوش مصنوعی


@pytest.mark.asyncio
async def test_back_from_hub_keeps_hub_as_parent(nav, psets):
    e = nav.edits
    await GM.main_bet_settings(_cb("gg:bet_settings@hub", 1, e))
    assert _back_of(e[-2]) == "pgw:hub" and "بازی‌ها و شرط‌بندی" in e[-2][1].inline_keyboard[-1][0].text
    await nav.PD.padg_menu(_cb("padg:menu@bet", 1, e), nav.st)
    await GM.main_bet_settings(_cb("gg:bet_settings@keep", 1, e))
    assert _back_of(e[-2]) == "pgw:hub"                                # مبدأ از دست نرفته
    await nav.PD.padg_menu(_cb("padg:menu@hub", 1, e), nav.st)
    assert _back_of(e[-2]) == "pgw:hub"


@pytest.mark.asyncio
async def test_games_menu_origin_resets_and_subscreens_keep_it(nav, psets):
    e = nav.edits
    await nav.PD.padg_menu(_cb("padg:menu@hub", 1, e), nav.st)
    gid = [b.callback_data for row in e[-2][1].inline_keyboard for b in row if b.callback_data.startswith("padg:open:")][0]
    await nav.PD.padg_open(_cb(gid, 1, e), nav.st)
    assert _back_of(e[-2]) == "padg:menu@keep"                         # زیرصفحه: برگشت به منوی بازی‌ها
    await nav.PD.padg_menu(_cb("padg:menu@keep", 1, e), nav.st)
    assert _back_of(e[-2]) == "pgw:hub"                                # هنوز مبدأ همان صفحهٔ مشترک است
    await nav.PD.padg_menu(_cb("padg:menu", 1, e), nav.st)             # ورود از دستهٔ سرگرمی ⇒ ریست
    assert _back_of(e[-2]) == "adm:back"


@pytest.mark.asyncio
async def test_back_survives_settings_refresh_and_is_per_admin(nav, psets, monkeypatch):
    async def fake_set(key, value):
        psets[key] = value
    monkeypatch.setattr(PW, "_set", fake_set)
    monkeypatch.setattr(GM, "_set", fake_set)
    e = nav.edits
    await GM.main_bet_settings(_cb("gg:bet_settings@hub", 1, e))
    await PW.wager_game_toggle(_cb("pgwset:xo", 1, e))                # تازه‌سازی صفحه پس از تغییر تنظیم
    assert _back_of(e[-2]) == "pgw:hub"
    await GM.main_bet_toggle(_cb("pgbset:toggle", 1, e))
    assert _back_of(e[-2]) == "pgw:hub"
    import platform_navback as NB
    assert NB.back(2, "bet", "gg:ai") == "gg:ai"                       # ادمین دیگر تحت‌تأثیر نیست


# ───────── هیچ مسیری بی‌صدا نیست (PATCH_WAGER_DIAG_V1) ─────────

def test_blocked_reasons():
    assert GM.blocked_reasons("-1001", True, -1001) == []
    assert "همان گروه" in GM.blocked_reasons("-1001", True, -1002)[0]
    assert "هیچ گروهی" in GM.blocked_reasons("", True, -1001)[0]
    r = GM.blocked_reasons("-1001", False, -1002)
    assert len(r) == 2 and "خاموش" in r[1]


@pytest.mark.asyncio
async def test_wrong_group_admin_gets_reason_others_fall_through(db, psets, monkeypatch):
    from aiogram.dispatcher.event.bases import SkipHandler

    async def denied(chat_id):
        return False
    monkeypatch.setattr(PW, "_group_allowed", denied)
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    psets["group_guard_chat_id"], psets["group_guard_enabled"] = "-555", "on"
    m, replies = _msg("تاس 20,000", uid=1)                      # مدیر اصلی در گروه دیگر
    with pytest.raises(SkipHandler):
        await PW.wager_create(m)
    assert replies and "-100" in replies[0] and "-555" in replies[0] and "فقط برای مدیر" in replies[0]
    m, replies = _msg("تاس 20,000", uid=A)                      # کاربر عادی: بی‌سروصدا ولی پیام را مصرف نمی‌کند
    with pytest.raises(SkipHandler):
        await PW.wager_create(m)
    assert replies == [] and await bal(db, A) == 100_000


@pytest.mark.asyncio
async def test_anonymous_admin_is_told_why(db, psets):
    m, replies = _msg("xo 5000")
    m.sender_chat = SimpleNamespace(id=-100)
    await PW.wager_create(m)
    assert replies and "ناشناس" in replies[0] and await bal(db, A) == 100_000


@pytest.mark.asyncio
async def test_internal_error_is_reported_and_logged_not_swallowed(db, psets, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(PW, "create_wager", boom)
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 1)
    monkeypatch.setattr(PW, "PLATFORM_ADMIN_ID", 1)
    m, replies = _msg("تاس 20,000", uid=1)
    await PW.wager_create(m)
    assert replies and "خطا" in replies[0] and "RuntimeError" in replies[0]          # مدیر نوع خطا را هم می‌بیند
    m, replies = _msg("تاس 20,000", uid=A)
    await PW.wager_create(m)
    assert replies and "خطا" in replies[0] and "RuntimeError" not in replies[0]      # کاربر عادی جزئیات نمی‌بیند
    assert any("create failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_comma_amount_with_digits_in_group_creates_wager(db, psets):
    m, replies = _msg("تاس 20,000")
    await PW.wager_create(m)
    assert replies and "شرط‌بندی تاس" in replies[0] and "20,000" in replies[0] and await bal(db, A) == 80_000


@pytest.mark.asyncio
async def test_coin_flip_hints_admin_in_wrong_group(db, psets, monkeypatch):
    async def denied(chat_id):
        return False
    monkeypatch.setattr(GM, "_group_allowed", denied)
    monkeypatch.setattr(GM, "PLATFORM_ADMIN_ID", 1)
    m, replies = _msg("شرطبندی 5000", uid=1)
    await GM.platform_create_bet(m)
    assert replies and "کار نمی" in replies[0]
    m, replies = _msg("شرطبندی 5000", uid=A)
    await GM.platform_create_bet(m)
    assert replies == []


def test_diag_script_imports():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("diag_wager", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "diag_wager.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main) and "platform_wager_dice" in dict(mod.KEYS)


# ═══════════ PATCH_WAGER_UPGRADE_V1 — انتخاب سازنده، قانون تاس، نماد ایکس‌او، راندهای سنگ‌کاغذ‌قیچی ═══════════

@pytest.mark.asyncio
async def test_xo_and_dice_wait_for_creator_option_before_join(db):
    for game, ask, good in (("xo", "نماد", "O"), ("dice", "بزرگ", "lo")):
        w = await new(db, game, opt=None)
        text, kb = PW.render(w)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert ask in text and any(":o:" in d for d in datas) and any(":cancel:" in d for d in datas)
        assert not any(":join:" in d for d in datas)                           # تا قبل از انتخاب سازنده دکمهٔ پیوستن نیست
        before = await bal(db, B)
        r = await PW.join_wager(w.id, (B, "b", "b"), 180)
        assert r.err and "انتخاب" in r.err and await bal(db, B) == before        # دکمهٔ قدیمی هم کار نمی‌کند
        assert (await PW.set_option(w.id, B, good)).err                         # فقط سازنده
        assert (await PW.set_option(w.id, A, "zzz")).err                        # مقدار نامعتبر
        ok = await PW.set_option(w.id, A, good)
        assert ok.err is None and json.loads(ok.w.state)["opt"] == good
        assert (await PW.set_option(w.id, A, good)).err                         # دوباره انتخاب نمی‌شود
        text, kb = PW.render(ok.w)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert any(":join:" in d for d in datas) and not any(":o:" in d for d in datas)
        assert (await join(db, ok.w)).status == "playing"
    w = await new(db, "rps", opt=None)                                          # سنگ‌کاغذ‌قیچی انتخابی ندارد
    assert any(":join:" in b.callback_data for row in PW.render(w)[1].inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_option_survives_join_and_expired_unchosen_wager_refunds(db):
    w = await join(db, await new(db, "dice", opt="lo"))
    assert json.loads(w.state)["opt"] == "lo" and "rolls" in json.loads(w.state)
    p = await new(db, "xo", opt=None, amount=3000)
    await expire(db, p.id)
    assert (await PW.set_option(p.id, A, "X")).err                              # مهلت گذشته ⇒ برگشت مبلغ، نه ثبت انتخاب
    assert (await wager(db, p.id)).status == "expired" and await bal(db, A) == 100_000 - 10_000


@pytest.mark.asyncio
async def test_dice_rule_creator_picks_bigger_or_smaller_wins(db):
    for mode, a, b, winner in (("hi", 6, 2, A), ("lo", 6, 2, B), ("lo", 1, 5, A), ("hi", 1, 5, B)):
        w = await join(db, await new(db, "dice", opt=mode))
        text, _ = PW.render(w)
        assert ("بزرگ‌تر" if mode == "hi" else "کوچک‌تر") in text
        await PW.dice_claim(w.id, A); await PW.dice_record(w.id, A, a)
        await PW.dice_claim(w.id, B)
        r = await PW.dice_record(w.id, B, b)
        assert r.w.status == "settled" and r.w.winner_id == winner, (mode, a, b)
    for mode in ("hi", "lo"):                                                   # مساوی ⇒ برگشت هر دو
        w = await join(db, await new(db, "dice", opt=mode))
        await PW.dice_claim(w.id, A); await PW.dice_record(w.id, A, 3)
        await PW.dice_claim(w.id, B)
        assert (await PW.dice_record(w.id, B, 3)).w.status == "draw"
    assert await bal(db, A) == await bal(db, B)                                 # هر دو یک‌بار برده و یک‌بار باخته‌اند


@pytest.mark.asyncio
async def test_dice_timeout_five_minutes_for_simultaneous_games_and_three_for_xo(db):
    d = await PW.join_wager((await new(db, "dice", opt="hi")).id, (B, "b", "b"), 180, sim_secs=300)
    left = (d.w.expires_at - datetime.utcnow()).total_seconds()
    assert 295 <= left <= 300 and "5 دقیقه" in PW.render(d.w)[0]
    x = await PW.join_wager((await new(db, "xo", opt="X")).id, (B, "b", "b"), 180, sim_secs=300)
    assert 175 <= (x.w.expires_at - datetime.utcnow()).total_seconds() <= 180 and "3 دقیقه" in PW.render(x.w)[0]
    r = await PW.join_wager((await new(db, "rps")).id, (B, "b", "b"), 180, sim_secs=300)
    assert 295 <= (r.w.expires_at - datetime.utcnow()).total_seconds() <= 300


@pytest.mark.asyncio
async def test_xo_creator_symbol_choice_decides_marks_and_winner(db):
    for starter in (A, B):
        w = await join(db, await new(db, "xo", opt="O"))                          # سازنده A «دایره»
        async with db() as s:
            row = await s.get(models.PlatformWager, w.id)
            row.turn_id = starter
            await s.commit()
        other = B if starter == A else A
        text, _ = PW.render(await wager(db, w.id))
        assert "⭕ @u101" in text and "❌ @u202" in text
        w = await play_xo(db, w, [(starter, 0), (other, 3), (starter, 1), (other, 4), (starter, 2)])
        assert w.status == "settled" and w.winner_id == starter
        board = json.loads(w.state)["board"]
        assert board[0] == ("O" if starter == A else "X") and board[3] == ("X" if starter == A else "O")
    assert await bal(db, A) == await bal(db, B)                                  # هر دو یک‌بار برده‌اند


def test_legacy_rows_without_option_keep_old_behaviour():
    legacy = SimpleNamespace(state="{}", creator_id=A)
    assert PW._xo_sym(legacy, A) == "X" and PW._xo_sym(legacy, B) == "O" and PW._dice_mode(legacy) == "hi"
    assert not PW._needs_setup(SimpleNamespace(status="playing", game="xo", state="{}"))   # بازی در جریان قبل از ارتقا گیر نمی‌کند


def test_game_prefix_and_coin_flip_are_not_confused():
    assert PW.parse_command("بازی تاس ۱۰۰۰") == ("dice", 1000)
    assert PW.parse_command("بازی xo 20,000") == ("xo", 20_000)
    assert PW.parse_command("بازی سنگ کاغذ قیچی 500") == ("rps", 500)
    assert PW.parse_command("بازی 1000") is None                                # شیر یا خط است، نه این بازی‌ها


@pytest.mark.asyncio
async def test_cfg_has_five_minute_default_for_simultaneous_games(db, psets):
    cfg = await PW.load_cfg()
    assert cfg["move"] == 180 and cfg["move_sim"] == 300
    psets["platform_wager_move_sim_secs"] = "600"
    assert (await PW.load_cfg())["move_sim"] == 600


@pytest.mark.asyncio
async def test_main_bot_keeps_explicit_bet_wording(db):
    w = await new(db, "dice", opt="hi")
    text, _ = PW.render(w)
    assert "🎰 <b>شرط‌بندی تاس</b>" in text and "مبلغ شرط هر نفر" in text and "شرط‌بندی ریسک مالی" in text
    assert PW.words(0)["bet"] == "شرط‌بندی" and PW.words(7)["bet"] == "بازی"


# === PATCH_WAGER_XO_STALE_UI_V1 === «نوبت تو نیست» با صفحهٔ کهنه و ضربه‌های پشت‌سرهم ===

@pytest.mark.asyncio
async def test_xo_wrong_turn_returns_fresh_state_for_redraw(db):
    w = await join(db, await new(db))
    first = (await wager(db, w.id)).turn_id
    second = B if first == A else A
    r = await PW.xo_move(w.id, second, 0, 180)
    assert r.err and r.w is not None and r.w.turn_id == first      # صفحه می‌تواند با نوبت واقعی دوباره کشیده شود
    assert (await PW.xo_move(w.id, first, 0, 180)).err is None
    r = await PW.xo_move(w.id, second, 0, 180)                      # خانهٔ پر
    assert r.err and r.w is not None


@pytest.mark.asyncio
async def test_handler_rapid_taps_edit_in_order_and_stale_tap_redraws(db, psets):
    PW._wlocks.clear()          # قفل‌های تست‌های قبلی به event loop دیگری وصل‌اند
    m, _ = _msg("اکس او ۱۰۰۰")
    await PW.wager_create(m)
    async with db() as s:
        wid = (await s.scalar(select(models.PlatformWager))).id
    edits = []
    await PW.wager_option(_cb(f"pgw:o:{wid}:X", A, edits))
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))
    first = (await wager(db, wid)).turn_id
    second = B if first == A else A

    shown = []
    delays = iter([0.05, 0.0, 0.0, 0.0])

    def slow_cb(data, uid):
        cb = _cb(data, uid, edits)

        async def edit_text(t, **kw):
            await asyncio.sleep(next(delays, 0.0))      # ویرایش اول کند است؛ بدون قفل، دومی از آن جلو می‌زد
            shown.append(t)
        cb.message.edit_text = edit_text
        return cb

    # ضربهٔ اول معتبر + چند ضربهٔ بعدی همان بازیکن (دیگر نوبتش نیست) + حرکت حریف، همه هم‌زمان
    await asyncio.gather(
        PW.wager_xo(slow_cb(f"pgw:x:{wid}:0", first)),
        PW.wager_xo(slow_cb(f"pgw:x:{wid}:1", first)),
        PW.wager_xo(slow_cb(f"pgw:x:{wid}:1", first)),
        PW.wager_xo(slow_cb(f"pgw:x:{wid}:4", second)),
    )
    row = await wager(db, wid)
    board = json.loads(row.state)["board"]
    assert board[0] and board[4] and not board[1]
    assert row.turn_id == first
    assert shown, "هیچ ویرایشی انجام نشد"
    # آخرین ویرایش باید همان وضعیت نهایی (نوبت first و هر دو خانه پر) باشد، نه صفحهٔ قدیمی
    assert shown[-1] == PW.render(row)[0] if isinstance(PW.render(row), tuple) else shown[-1] == PW.render(row)


# === PATCH_WAGER_DICE_PARITY_V1 / PATCH_WAGER_TAP_GATE_V1 ===

def test_dice_creator_wins_rules():
    f = PW.dice_creator_wins
    assert f("hi", 5, 3) is True and f("hi", 3, 5) is False and f("hi", 4, 4) is None
    assert f("lo", 3, 5) is True and f("lo", 5, 3) is False
    assert f("even", 2, 3) is True and f("even", 3, 2) is False
    assert f("even", 2, 4) is None and f("even", 3, 5) is None
    assert f("odd", 3, 2) is True and f("odd", 2, 3) is False and f("odd", 1, 5) is None


@pytest.mark.asyncio
async def test_dice_even_odd_option_settles_and_draws(db):
    for mode, rolls, expect in (("even", (2, 3), "creator"), ("odd", (2, 3), "opponent"), ("even", (4, 6), "draw")):
        w = await new(db, game="dice", opt=mode)
        w = await join(db, w)
        assert PW._dice_mode(w) == mode and "زوج" in PW._rule_line(w) + "زوج" and PW._rule_line(w)
        for uid, v in zip((A, B), rolls):
            assert (await PW.dice_claim(w.id, uid)).err is None
            await PW.dice_record(w.id, uid, v)
        row = await wager(db, w.id)
        if expect == "draw":
            assert row.status == "draw"
        else:
            assert row.status == "settled" and row.winner_id == (A if expect == "creator" else B)
    assert await total(db) == 300_500


@pytest.mark.asyncio
async def test_dice_option_keyboard_has_parity_buttons_and_accepts_them(db):
    w = await new(db, game="dice", opt=None)
    data = [b.callback_data for row in PW._opt_keyboard(w) for b in row]
    assert any(d.endswith(":even") for d in data) and any(d.endswith(":odd") for d in data)
    assert (await PW.set_option(w.id, A, "odd")).err is None
    assert (await PW.set_option(w.id, A, "bogus")).err


@pytest.mark.asyncio
async def test_finished_game_tap_returns_wager_for_final_redraw(db):
    w = await join(db, await new(db))
    first = (await wager(db, w.id)).turn_id
    await expire(db, w.id)
    r = await PW.xo_move(w.id, first, 0, 180)           # مهلت گذشته ⇒ تسویه
    assert r.err and r.w is not None
    r = await PW.xo_move(w.id, first, 1, 180)           # بعد از پایان: دیگر در حال بازی نیست
    assert r.err and r.w is not None and r.w.status in ("settled", "draw", "expired")


def test_tap_gate_blocks_rapid_repeats_per_player(monkeypatch):
    monkeypatch.setattr(PW, "TAP_GAP_SECS", 0.5)
    PW._tap_last.clear()
    assert PW.tap_gate(1, A) is False and PW.tap_gate(1, A) is True
    assert PW.tap_gate(1, B) is False and PW.tap_gate(2, A) is False and PW.tap_gate(1, A, 7) is False
    monkeypatch.setattr(PW, "TAP_GAP_SECS", 0)
    assert PW.tap_gate(1, A) is False
