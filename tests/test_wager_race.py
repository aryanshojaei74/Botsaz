"""PATCH_WAGER_RACE_V1 — مسابقه‌هایِ پولیِ دونفره (حدس عدد/سرعت/ریاضی): پول‌سنجی، مهلت، لغو، دسترسی و هم‌زمانی با بازیِ رایگان."""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_game as G
import platform_group_money as M
import platform_wager as W
import platform_wager_race as R

CHAT = -100
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import platform_bot as PB
    for mod in (W, R, M, PB, G):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    async with mk() as s:
        for uid, bal in ((1, 100000), (2, 100000), (3, 500)):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", balance=bal))
        await s.commit()
    R.LIVE.clear(); R._LAST_GUESS.clear(); G._GAMES.clear(); G._RACES.clear()

    async def ga(chat_id):
        return chat_id == CHAT
    monkeypatch.setattr(R, "_group_allowed", ga)
    monkeypatch.setattr(G, "_group_allowed", ga)

    async def on():
        return True
    monkeypatch.setattr(R, "enabled", on)
    monkeypatch.setattr(R, "_bet_enabled", on)
    yield mk
    await eng.dispose()


async def bal(mk, uid):
    async with mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))
        return int(u.balance)


async def make(mk, game="speed", amount=10000):
    w, err = await W.create_wager(game, CHAT, (1, "u1", None), amount, None, CFG)
    assert err is None
    return w


def test_parse_commands():
    assert R.parse_race_command("حدس عدد 10000") == ("guess", 10000)
    assert R.parse_race_command("بازی سرعت ۵۰۰۰") == ("speed", 5000)
    assert R.parse_race_command("شرط بندی ریاضی 2,000") == ("math", 2000)
    assert R.parse_race_command("حدس عدد") is None and R.parse_race_command("سرعت 0") is None
    assert W.parse_command("حدس عدد 10000") is None                  # هسته و ربات‌هایِ مشتری آن را نمی‌شناسند
    assert not set(R.RACE_GAMES) & set(W.GAMES)


@pytest.mark.asyncio
async def test_full_flow_money_is_conserved_and_winner_gets_payout(env):
    mk = env
    w = await make(mk)
    assert await bal(mk, 1) == 90000                                  # رزرو
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None and r.w.status == "playing" and await bal(mk, 2) == 90000
    won = await R.settle_to(w.id, 2)
    assert won is not None and won.status == "settled" and won.winner_id == 2
    assert await bal(mk, 2) == 90000 + 19000 and await bal(mk, 1) == 90000        # ۲×۱۰۰۰۰ − ۵٪ کارمزد
    assert await R.settle_to(w.id, 1) is None                         # دوباره تسویه نمی‌شود
    assert await bal(mk, 1) + await bal(mk, 2) == 200000 - 1000       # فقط کارمزد از سیستم خارج شد


@pytest.mark.asyncio
async def test_timeout_refunds_both(env):
    mk = env
    w = await make(mk, "math")
    await R.join_race(w.id, (2, "u2", None))
    async with mk() as s:
        row = await s.get(models.PlatformWager, w.id)
        row.expires_at = W._now() - timedelta(seconds=1)
        await s.commit()
    done = await W.expire_one(w.id)
    assert done is not None and done.status == "expired"
    assert await bal(mk, 1) == 100000 and await bal(mk, 2) == 100000                   # هر دو کامل برگشت
    text, _ = W.render(done)                                                            # رندرِ هسته به رندرِ مسابقه می‌رود
    assert "برگشت" in text


@pytest.mark.asyncio
async def test_join_rules_creator_poor_user_target_and_cancel(env):
    mk = env
    w = await make(mk)
    assert (await R.join_race(w.id, (1, "u1", None))).err                                 # سازنده نه
    assert "موجودی" in (await R.join_race(w.id, (3, "u3", None))).err and await bal(mk, 3) == 500   # فقیر ⇒ کسر نشد
    r = await W.cancel_wager(w.id, 1)
    assert r.w.status == "cancelled" and await bal(mk, 1) == 100000
    assert (await R.join_race(w.id, (2, "u2", None))).err                                 # لغوشده دیگر پیوستنی نیست


@pytest.mark.asyncio
async def test_non_player_cannot_win_and_answers_are_matched_in_memory(env):
    mk = env
    w = await make(mk, "speed")
    r = await R.join_race(w.id, (2, "u2", None))
    st = json.loads(r.w.state)
    R.LIVE[CHAT] = {"wid": w.id, "kind": "speed", "players": {1, 2}, "answer": st["a"], "n": None, "until": 10 ** 12}
    outsider = NS(text=st["q"], chat=NS(id=CHAT), from_user=NS(id=9, is_bot=False))
    wrong = NS(text="چیزِ دیگر", chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False))
    right = NS(text=st["q"], chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False))
    assert R._is_answer(outsider) is False and R._is_answer(wrong) is False and R._is_answer(right) is True
    assert await R.settle_to(w.id, 9) is None                                              # غیرِ بازیکن هرگز


@pytest.mark.asyncio
async def test_guess_flow_hints_and_win_via_handler(env, monkeypatch):
    mk = env
    w = await make(mk, "guess")
    r = await R.join_race(w.id, (2, "u2", None))
    n = json.loads(r.w.state)["n"]
    monkeypatch.setattr(R, "GUESS_GAP", 0.0)
    R.LIVE[CHAT] = {"wid": w.id, "kind": "guess", "players": {1, 2}, "answer": None, "n": n, "until": 10 ** 12}
    sent = []

    async def reply(t, **k):
        sent.append(t)

    async def edit_message_text(*a, **k):
        return None
    bot = NS(edit_message_text=edit_message_text)

    def msg(text, uid):
        return NS(text=text, chat=NS(id=CHAT), from_user=NS(id=uid, is_bot=False, full_name=f"u{uid}"), reply=reply, bot=bot)
    low = 1 if n > 1 else 2
    if n > 1:
        await R.race_answer(msg(str(n - 1), 1))
        assert sent[-1] == "⬆️ بالاتر"
    if n < 100:
        await R.race_answer(msg(str(n + 1), 2))
        assert sent[-1] == "⬇️ پایین‌تر"
    await R.race_answer(msg(str(n), 2))
    assert "برنده شد" in sent[-1] and CHAT not in R.LIVE
    assert await bal(mk, 2) == 90000 + 19000


@pytest.mark.asyncio
async def test_create_handler_gates_one_at_a_time_and_free_game_conflict(env, monkeypatch):
    mk = env
    out = []

    async def reply(t, **k):
        out.append(t)
        return NS(message_id=55)
    bot = NS()

    async def sweep(b):
        return 0
    def msg(text, uid=1, chat=CHAT):
        return NS(text=text, chat=NS(id=chat), from_user=NS(id=uid, is_bot=False, full_name=f"u{uid}", username=None),
                  sender_chat=None, reply=reply, bot=bot, reply_to_message=None)
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await R.race_create(msg("سرعت 10000", chat=-5))                                    # گروهِ دیگر
    monkeypatch.setattr(W, "sweep_expired", sweep)
    await R.race_create(msg("سرعت 10000"))
    assert "منتظر حریف" in out[-1] and CHAT in R.LIVE and await bal(mk, 1) == 90000
    await R.race_create(msg("حدس عدد 10000", uid=2))
    assert "جریان دارد" in out[-1] and await bal(mk, 2) == 100000                          # دومی ساخته نشد
    free = NS(text="حدس عدد", chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False, full_name="u2"), sender_chat=None,
              reply=reply, bot=bot)
    await G.gg_start(free)
    assert "پولی" in out[-1] and CHAT not in G._GAMES                                      # بازیِ رایگان وسطِ مسابقهٔ پولی شروع نمی‌شود


@pytest.mark.asyncio
async def test_disabled_flag_skips_handlers(env, monkeypatch):
    async def off():
        return False
    monkeypatch.setattr(R, "enabled", off)
    from aiogram.dispatcher.event.bases import SkipHandler
    m = NS(text="سرعت 10000", chat=NS(id=CHAT), from_user=NS(id=1, is_bot=False), sender_chat=None)
    with pytest.raises(SkipHandler):
        await R.race_create(m)
