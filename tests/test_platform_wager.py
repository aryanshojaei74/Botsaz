"""PATCH_PLATFORM_WAGER_V1 — شرط‌بندی ایکس‌او / سنگ‌کاغذ‌قیچی / تاس با کیف‌پول بات‌ساز."""
import asyncio
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


async def new(db, game="xo", amount=10_000, cfg=CFG, target=None, creator=A):
    w, err = await PW.create_wager(game, -100, (creator, f"u{creator}", f"u{creator}"), amount, target, cfg)
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

@pytest.mark.asyncio
async def test_rps_hidden_until_both_then_win(db):
    w = await join(db, await new(db, "rps", cfg=FEE10))
    r = await PW.rps_pick(w.id, A, "r")
    assert r.err is None and r.w.status == "playing"
    text, _ = PW.render(r.w)
    assert "سنگ" not in text.split("انتخاب کرد")[0] or "🪨" not in text      # انتخاب فاش نشده
    assert (await PW.rps_pick(w.id, A, "p")).err                              # انتخاب دوباره
    r = await PW.rps_pick(w.id, B, "s")                                       # سنگ بر قیچی
    assert r.w.status == "settled" and r.w.winner_id == A
    assert await bal(db, A) == 100_000 - 10_000 + 18_000 and await bal(db, B) == 90_000


@pytest.mark.asyncio
async def test_rps_draw_refunds_and_invalid_inputs(db):
    w = await join(db, await new(db, "rps"))
    assert (await PW.rps_pick(w.id, A, "x")).err and (await PW.rps_pick(w.id, C, "r")).err
    await PW.rps_pick(w.id, A, "p")
    r = await PW.rps_pick(w.id, B, "p")
    assert r.w.status == "draw" and await bal(db, A) == 100_000 and await bal(db, B) == 100_000


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
    edits = []
    await PW.wager_join(_cb(f"pgw:join:{wid}", A, edits))                               # سازنده نمی‌تواند
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    await PW.wager_join(_cb(f"pgw:join:{wid}", B, edits))
    row = await wager(db, wid)
    assert row.status == "playing" and await bal(db, B) == 99_000
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
    await PW.rps_pick(w.id, A, "r"); await PW.rps_pick(w.id, B, "s")
    fees += 800
    # مساوی
    w = await join(db, await new(db, "rps", amount=4000, cfg=FEE10))
    await PW.rps_pick(w.id, A, "r"); await PW.rps_pick(w.id, B, "r")
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


@pytest.mark.asyncio
async def test_shirt_bandi_with_zwnj_reaches_coin_flip_regex():
    assert GM._BET_RE.match("شرط‌بندی ۱۰۰۰") and GM._INCOMPLETE_BET_RE.match("شرط‌بندی")
    assert GM._BET_RE.match("بازی ۱۰۰۰") and not GM._BET_RE.match("شرط‌بندی xo ۱۰۰۰")
