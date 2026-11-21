"""PATCH_WAGER_RANK_V1 + PATCH_RANDOM_GAME_V1 — رندوم، کارمزدِ پلکانی، آمارِ بازیکنان."""
import json
from decimal import Decimal

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
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}


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


def test_parse_random_command():
    for t in ("رندوم 10000", "تصادفی 10000", "شرطبندی رندوم 10,000", "بازی رندوم ۱۰۰۰۰"):
        assert R.parse_race_command(t) == ("random", 10000), t


@pytest.mark.asyncio
async def test_random_game_picks_an_enabled_game_on_join(env, monkeypatch):
    mk = env
    w, err = await W.create_wager("random", CHAT, (1, "u1", None), 10000, None, CFG)
    assert err is None and w.game == "random"
    monkeypatch.setattr(R.secrets, "choice", lambda seq: "emoji" if "emoji" in seq else seq[0])
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None and r.w.game == "emoji" and r.note and "حدس ایموجی" in r.note
    assert json.loads(r.w.state)["q"]
    # پولِ رزرو شد و تسویه عادی است
    won = await R.settle_to(w.id, 2)
    assert won.status == "settled"


@pytest.mark.asyncio
async def test_random_needs_some_enabled_game(env):
    for g in R.RACE_GAMES:
        await M._set(R._game_key(g), "off")
    assert await R.game_on("random") is False
    w, _ = await W.create_wager("random", CHAT, (1, "u1", None), 10000, None, CFG)
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err and "هیچ بازی" in r.err


def test_tiers_parse_and_pick():
    t = K.parse_tiers("0 5\n20:4\n۵۰ ۳٪")
    assert K.pct_for(t, 0) == 5 and K.pct_for(t, 19) == 5 and K.pct_for(t, 20) == 4 and K.pct_for(t, 99) == 3
    assert K.pct_for([], 5) is None and K.pct_for(K.parse_tiers("10 2"), 3) is None
    for bad in ("abc", "1 2 3", "5 99", "-1 3"):
        with pytest.raises(ValueError):
            K.parse_tiers(bad)
    with pytest.raises(ValueError):
        K.parse_tiers("\n".join(f"{i} 1" for i in range(9)))


async def _play(mk, winner):
    w, _ = await W.create_wager("speed", CHAT, (1, "u1", None), 10000, None, CFG)
    r = await R.join_race(w.id, (2, "u2", None))
    return await R.settle_to(w.id, winner)


@pytest.mark.asyncio
async def test_rank_fee_applies_by_winner_games_and_wins(env):
    mk = env
    # بدونِ پله ⇒ کارمزدِ عادی ۵٪ از ۲۰٬۰۰۰ = ۱٬۰۰۰
    w1 = await _play(mk, 2)
    assert int(w1.fee_amount) == 1000 and int(w1.payout_amount) == 19000
    # پله: از ۱ بازی ⇒ ۲٪؛ (برنده تا الان ۱ بازیِ تمام‌شده دارد)
    await K.save_config(0, basis="games", tiers_text="0:5;1:2")
    w2 = await _play(mk, 2)
    assert float(w2.fee_pct) == 2 and int(w2.fee_amount) == 400 and int(w2.payout_amount) == 19600
    async with mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 2))
    # پولِ برنده: ۱٬۰۰۰٬۰۰۰ − ۱۰٬۰۰۰ + ۱۹٬۰۰۰ − ۱۰٬۰۰۰ + ۱۹٬۶۰۰
    assert int(u.balance) == 1_000_000 + 9000 + 9600
    # مبنا = برد: نفرِ ۱ هیچ بردی ندارد ⇒ اولین پله (۰) = ۵٪
    await K.save_config(0, basis="wins", tiers_text="0:5;3:1")
    w3 = await _play(mk, 1)
    assert float(w3.fee_pct) == 5


@pytest.mark.asyncio
async def test_player_stats_and_boards(env):
    mk = env
    for win in (2, 2, 1):
        await _play(mk, win)
    st = await K.player_stats(0, 10)
    top = st[0]
    assert top["uid"] == 2 and top["games"] == 3 and top["wins"] == 2 and top["losses"] == 1
    assert top["games_amt"] == 30000 and top["wins_amt"] == 20000 and top["fees"] == 2000
    pub = K.public_board(st, K.parse_tiers("0 5"), "games")
    assert "برترین بازیکنان" in pub and "3 بازی" in pub and "2 برد" in pub and "5٪" in pub
    adm = K.admin_board(st)
    assert "30,000" in adm and "کارمزدِ پرداختی" in adm
    assert "هنوز" in K.public_board([], None)


@pytest.mark.asyncio
async def test_tiers_input_flow_and_menu(env):
    from types import SimpleNamespace as NS
    out = []

    class Msg:
        def __init__(self, text): self.text = text
        async def answer(self, text, **k): out.append(text)

    class St:
        async def clear(self): out.append("cleared")
    await K._tiers_input(Msg("bad line"), St(), 0, "pgrk", "pgrk:back")
    assert "نامعتبر" in out[-1] or "فرمت" in out[-1]
    await K._tiers_input(Msg("0 5\n20 4"), St(), 0, "pgrk", "pgrk:back")
    basis, tiers = await K.load_config(0)
    assert [t[0] for t in tiers] == [0, 20]
    text, kb = await K._menu_view(0, "pgrk", "pgrk:back")
    assert "از 20 بازی" in text and any(b.callback_data == "pgrk:stats" for r in kb.inline_keyboard for b in r)
    await K._tiers_input(Msg("/clear"), St(), 0, "pgrk", "pgrk:back")
    assert (await K.load_config(0))[1] == []


# ───────────── PATCH_FREE_GAME_V1: بازیِ رایگان ─────────────
def test_parse_free_command():
    for t, g in (("سرعت", "speed"), ("ریاضی", "math"), ("حدس عدد", "guess"), ("رندوم", "random"), ("تصادفی", "random"),
                 ("حدس ایموجی", "emoji"), ("ایموجی", "emoji"), ("کوییز", "quiz"), ("درهم", "scramble"), ("الگو", "pattern")):
        assert R.parse_free_command(t) == g, t
    for t in ("سرعت 1000", "شرطبندی سرعت", "سلام", "بازی سرعت"):
        assert R.parse_free_command(t) is None, t


@pytest.mark.asyncio
async def test_free_cfg_defaults_and_save(env):
    fc = await K.free_cfg(0)
    assert fc == {"on": False, "limit": 5, "fee": 0}
    await K.save_kv(0, "free_on", "1")
    await K.save_kv(0, "free_limit", "2")
    await K.save_kv(0, "free_fee", "500")
    assert await K.free_cfg(0) == {"on": True, "limit": 2, "fee": 500}


async def _bal(mk, uid):
    async with mk() as s:
        return int((await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))).balance)


@pytest.mark.asyncio
async def test_free_game_charges_entry_fee_only_and_pays_no_prize(env):
    mk = env
    w, err = await W.create_free_wager("speed", CHAT, (1, "u1", None), None, 300, 500, 5)
    assert err is None and int(w.amount) == 0 and await _bal(mk, 1) == 1_000_000       # هنوز چیزی کم نشده
    t, kb = R.render_race(w)
    assert "بازیِ رایگان" in t and "500" in t
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None and r.w.status == "playing"
    assert await _bal(mk, 1) == 999_500 and await _bal(mk, 2) == 999_500               # هزینهٔ ورودِ هر دو
    won = await R.settle_to(w.id, 2)
    assert won.status == "settled" and await _bal(mk, 2) == 999_500 and await _bal(mk, 1) == 999_500   # جایزهٔ پولی نیست
    assert "جایزهٔ پولی ندارد" in R.prize_line(won)
    async with mk() as s:
        kinds = [r_.kind for r_ in (await s.scalars(select(models.PlatformTxn))).all()]
    assert kinds.count("wager_fee") == 2 and "wager_prize" not in kinds and "wager_stake" not in kinds


@pytest.mark.asyncio
async def test_free_game_without_fee_touches_no_wallet(env):
    mk = env
    w, _ = await W.create_free_wager("math", CHAT, (1, "u1", None), None, 300, 0, 0)
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None
    await R.settle_to(w.id, 1)
    assert await _bal(mk, 1) == 1_000_000 and await _bal(mk, 2) == 1_000_000
    async with mk() as s:
        assert (await s.scalars(select(models.PlatformTxn))).all() == []


@pytest.mark.asyncio
async def test_free_daily_limit_and_insufficient_fee(env):
    mk = env
    for _ in range(2):
        w, err = await W.create_free_wager("math", CHAT, (1, "u1", None), None, 300, 0, 2)
        assert err is None
        assert (await R.join_race(w.id, (2, "u2", None))).err is None
        await R.settle_to(w.id, 1)
    w, err = await W.create_free_wager("math", CHAT, (1, "u1", None), None, 300, 0, 2)
    assert w is None and "سقف" in err
    w, err = await W.create_free_wager("math", CHAT, (2, "u2", None), None, 300, 0, 2)    # حریف هم همین سقف را دارد
    assert w is None and "سقف" in err
    w, err = await W.create_free_wager("math", CHAT, (1, "u1", None), None, 300, 2_000_000, 0)
    assert w is None and "هزینهٔ ورود" in err


@pytest.mark.asyncio
async def test_free_games_do_not_count_in_stats_or_rank(env):
    mk = env
    w, _ = await W.create_free_wager("math", CHAT, (1, "u1", None), None, 300, 0, 0)
    await R.join_race(w.id, (2, "u2", None))
    await R.settle_to(w.id, 2)
    assert await K.player_stats(0, 10) == []
    async with mk() as s:
        assert await K.rank_count(s, 0, 2, "games") == 0


@pytest.mark.asyncio
async def test_free_settings_screen_and_input(env):
    out = []

    class Msg:
        def __init__(self, text): self.text = text
        async def answer(self, text, **k): out.append(text)

    class St:
        async def clear(self): pass
    text, kb = await K._free_view(0, "pgrk")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "خاموش" in text and {"pgrk:fon", "pgrk:flim", "pgrk:ffee", "pgrk:menu"} <= set(cbs)
    await K._free_input(Msg("abc"), St(), 0, "free_limit", "pgrk")
    assert "عدد" in out[-1]
    await K._free_input(Msg("۳"), St(), 0, "free_limit", "pgrk")
    await K._free_input(Msg("1,500"), St(), 0, "free_fee", "pgrk")
    assert await K.free_cfg(0) == {"on": False, "limit": 3, "fee": 1500}
    menu_text, menu_kb = await K._menu_view(0, "pgrk", "pgrk:back")
    assert any(b.callback_data == "pgrk:free" for r in menu_kb.inline_keyboard for b in r)
