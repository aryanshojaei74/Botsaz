"""PATCH_FREE_DUEL_V1 (سینا): «برای رایگان نمیشه دوز و سنگ کاغذ قیچی و تاس بازی کرد» — این سه بازی هم فقط با اسمشان رایگان ساخته می‌شوند."""
import types

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_money as M
import platform_wager as W
import platform_wager_race as R
import wager_rank as K

CHAT = -100


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import config
    import platform_bot as PB
    for mod in (W, R, M, K, config, PB):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    async with mk() as s:
        for uid in (1, 2):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", balance=1_000_000))
        await s.commit()
    await M._set("pgr_on", "1")
    R.LIVE.clear()
    yield mk
    await eng.dispose()


async def _pick(wid, game, uid=1):
    """دوز/تاس: سازنده پیش از پیوستنِ حریف انتخابش را (نماد / قانون) ثبت می‌کند."""
    if game in ("xo", "dice"):
        r = await W.set_option(wid, uid, "X" if game == "xo" else "hi")
        assert r.err is None, r.err


async def _bal(mk, uid):
    async with mk() as s:
        return int((await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))).balance)


def test_free_command_covers_duel_games():
    for t, g in (("دوز", "xo"), ("xo", "xo"), ("XO", "xo"), ("اکس او", "xo"), ("ایکس‌او", "xo"),
                 ("سنگ کاغذ قیچی", "rps"), ("rps", "rps"), ("تاس", "dice")):
        assert R.parse_free_command(t) == g, t
    assert R.parse_free_command("دوز 1000") is None        # با مبلغ ⇒ بازیِ پولیِ قبلی، نه رایگان
    assert W.parse_command("دوز 1000") == ("xo", 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["xo", "rps", "dice"])
async def test_free_duel_joins_without_stake_and_charges_only_entry_fee(env, game):
    mk = env
    w, err = await W.create_free_wager(game, CHAT, (1, "u1", None), None, 300, 500, 0)
    assert err is None and int(w.amount) == 0
    text, kb = W.render(w)
    assert "بازیِ رایگان" in text
    await _pick(w.id, game)
    r = await W.join_wager(w.id, (2, "u2", None), 180)
    assert r.err is None and r.w.status == "playing"
    assert await _bal(mk, 1) == 999_500 and await _bal(mk, 2) == 999_500      # فقط هزینهٔ ورود، نه شرط
    async with mk() as s:
        kinds = [t.kind for t in (await s.scalars(select(models.PlatformTxn))).all()]
    assert kinds.count("wager_fee") == 2 and "wager_stake" not in kinds


@pytest.mark.asyncio
async def test_free_xo_full_game_pays_no_prize_and_no_wallet_change_without_fee(env):
    mk = env
    w, _ = await W.create_free_wager("xo", CHAT, (1, "u1", None), None, 300, 0, 0)
    await _pick(w.id, "xo")
    assert (await W.join_wager(w.id, (2, "u2", None), 180)).err is None
    async with mk() as s:
        row = await s.get(models.PlatformWager, w.id)
        first = row.turn_id
    other = 2 if first == 1 else 1
    moves = [(first, 0), (other, 3), (first, 1), (other, 4), (first, 2)]       # نفرِ اول ردیفِ بالا را می‌برد
    res = None
    for uid, cell in moves:
        res = await W.xo_move(w.id, uid, cell, 180)
        assert res.err is None, res.err
    assert res.w.status == "settled" and res.w.winner_id == first
    assert await _bal(mk, 1) == 1_000_000 and await _bal(mk, 2) == 1_000_000
    async with mk() as s:
        assert (await s.scalars(select(models.PlatformTxn))).all() == []


@pytest.mark.asyncio
async def test_free_duel_daily_limit_and_entry_fee_balance(env):
    w, err = await W.create_free_wager("rps", CHAT, (1, "u1", None), None, 300, 2_000_000, 0)
    assert w is None and "هزینهٔ ورود" in err
    for _ in range(2):
        w, err = await W.create_free_wager("dice", CHAT, (1, "u1", None), None, 300, 0, 2)
        assert err is None
        await _pick(w.id, "dice")
        assert (await W.join_wager(w.id, (2, "u2", None), 180)).err is None
        async with env() as s:
            ww = await s.get(models.PlatformWager, w.id)
            await W._settle_win(s, ww, 1)
            await s.commit()
    w, err = await W.create_free_wager("xo", CHAT, (1, "u1", None), None, 300, 0, 2)
    assert w is None and "سقف" in err                      # سقفِ روزانه بینِ همهٔ بازی‌هایِ رایگان مشترک است


@pytest.mark.asyncio
async def test_paid_game_still_requires_balance_and_charges_stake(env):
    mk = env
    cfg = {"min": 1000, "max": 0, "fee_pct": W.Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}
    w, err = await W.create_wager("xo", CHAT, (1, "u1", None), 10_000, None, cfg)
    assert err is None
    await _pick(w.id, "xo")
    r = await W.join_wager(w.id, (2, "u2", None), 180)
    assert r.err is None
    assert await _bal(mk, 2) == 990_000                      # شرطِ عادی هنوز کم می‌شود


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["xo", "rps", "dice"])
async def test_free_create_posts_the_duel_board_with_normal_buttons(env, game):
    """مسیرِ واقعیِ فرمان (free_create) برایِ بازیِ دونفره: صفحهٔ همان بازیِ پولی (نه مسابقهٔ LIVE) ساخته و message_id ذخیره می‌شود."""
    mk = env
    await K.save_kv(0, "free_on", "1")
    sent = []

    class Msg:
        chat = types.SimpleNamespace(id=CHAT)
        from_user = types.SimpleNamespace(id=1, full_name="u1", username=None, is_bot=False)
        reply_to_message = None
        sender_chat = None

        async def reply(self, text, **k):
            sent.append((text, k.get("reply_markup")))
            return types.SimpleNamespace(message_id=777)

    bot = types.SimpleNamespace()
    await R.free_create(Msg(), bot, 0, game)
    assert sent and "بازیِ رایگان" in sent[0][0]
    cbs = [b.callback_data for r in sent[0][1].inline_keyboard for b in r]
    assert any((c or "").startswith(("pgw:", "twg:")) for c in cbs), cbs     # دکمه‌هایِ بازیِ دونفره
    assert not R.LIVE                                        # مسابقهٔ LIVE نیست
    async with mk() as s:
        row = (await s.scalars(select(models.PlatformWager))).one()
    assert row.game == game and row.message_id == 777 and int(row.amount) == 0
