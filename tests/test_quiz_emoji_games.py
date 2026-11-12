"""PATCH_QUIZ_EMOJI_GAMES_V1 (SINA): مسابقه‌هایِ پولیِ «کوییز» و «حدس ایموجی»."""
import json
import time
from types import SimpleNamespace as NS

import pytest

import platform_wager_race as R
import quiz_bank as QB
from models import PlatformWager


def test_bank_quality():
    assert len(QB.QUIZ) >= 60 and len(QB.EMOJI) >= 55
    for q, opts in QB.QUIZ:
        assert len(opts) == 4 and len(set(opts)) == 4 and q.strip().endswith("؟"), q
    assert len({q for q, _ in QB.QUIZ}) == len(QB.QUIZ) and len({e for e, _ in QB.EMOJI}) == len(QB.EMOJI)
    for _e, ans in QB.EMOJI:
        assert ans and all(a.strip() for a in ans)


def test_make_quiz_shuffles_but_keeps_the_right_answer_index_consistent():
    seen = set()
    for _ in range(200):
        z = QB.make_quiz()
        assert z["opts"][int(z["a"]) - 1] == z["right"] and len(z["opts"]) == 4
        seen.add(z["a"])
    assert seen == {"1", "2", "3", "4"}                         # جایگاهِ درست یک‌نواخت نیست


def test_commands_and_registration():
    for txt in ("کوییز 10000", "کویز 10000", "کوئیز ۱۰۰۰۰", "شرطبندی کوییز 5000", "بازی کوییز 5000"):
        assert R.parse_race_command(txt) == ("quiz", int(R._digits(txt.split()[-1]).replace(",", ""))), txt
    for txt in ("ایموجی 10000", "حدس ایموجی 10000", "شرطبندی حدس ایموجی 10000"):
        assert R.parse_race_command(txt)[0] == "emoji", txt
    assert R.parse_race_command("حدس عدد 10000")[0] == "guess"                    # قبلی‌ها سالم
    assert {"quiz", "emoji"} <= set(R.RACE_GAMES) and R.SECS["quiz"] == 30 and R.SECS["emoji"] == 45


def _wager(game, st, status="playing"):
    return PlatformWager(id=1, bot_id=0, chat_id=-1, game=game, creator_id=10, creator_name="A", opponent_id=20,
                         opponent_name="B", amount=10000, status=status, state=json.dumps(st, ensure_ascii=False))


def test_render_quiz_and_emoji():
    st = R.new_question("quiz")
    t, _ = R.render_race(_wager("quiz", st))
    assert st["q"] in t and all(o in t for o in st["opts"]) and "فقط" in t
    assert f"{st['right']}" not in t.split("✍️")[0].split("\n\n")[0]               # جواب جدا از متنِ سؤال نیست ولی علامت نخورده
    t, _ = R.render_race(_wager("emoji", R.new_question("emoji")))
    assert "ایموجی" in t
    w = _wager("quiz", st, "settled")
    w.winner_id, w.payout_amount, w.fee_amount = 10, 19000, 1000
    t, _ = R.render_race(w)
    assert "پاسخِ درست" in t and st["right"] in t


class _M:
    def __init__(self, text, uid=10):
        self.text, self.from_user, self.chat = text, NS(id=uid, full_name="P"), NS(id=-1)
        self.replies = []

    async def reply(self, text, **k):
        self.replies.append(text)


def _live(game, st):
    lv = {"wid": 1, "chat": -1, "bot": 0, "kind": game, "players": {10, 20}, "answer": st.get("a"), "n": None,
          "st": st, "alts": st.get("alts"), "until": time.monotonic() + 100}
    R.LIVE.clear()
    R.LIVE[1] = lv
    return lv


def test_match_emoji_accepts_variants_and_quiz_only_digits():
    st = {"q": "💻", "w": "لپ تاپ", "a": R.norm_answer("لپ تاپ"), "alts": [R.norm_answer(x) for x in ["لپتاپ", "کامپیوتر"]]}
    _live("emoji", st)
    assert R._match(_M("لپ‌تاپ")) and R._match(_M("کامپیوتر")) and R._match(_M("لپ تاپ"))
    assert R._match(_M("موبایل")) is None
    _live("quiz", {"q": "x", "opts": list("abcd"), "a": "3", "right": "c"})
    assert R._match(_M("۲")) and R._match(_M("4")) and R._match(_M("5")) is None and R._match(_M("سلام")) is None


@pytest.mark.asyncio
async def test_quiz_step_right_wrong_and_single_chance():
    lv = _live("quiz", {"q": "x", "opts": list("abcd"), "a": "3", "right": "c"})
    assert await R.quiz_step(None, _M("۳", uid=20), lv) == "win"
    m = _M("1", uid=10)
    assert await R.quiz_step(None, m, lv) == "done" and "حذف" in m.replies[0] and 10 in lv["locked"]
    R._WORD_NOTE.clear()
    m2 = _M("3", uid=10)                                         # حذف‌شده دیگر جواب درست هم نمی‌تواند بدهد
    assert await R.quiz_step(None, m2, lv) == "done" and "یک فرصت" in m2.replies[0]


@pytest.mark.asyncio
async def test_quiz_both_wrong_ends_game_early(monkeypatch):
    lv = _live("quiz", {"q": "x", "opts": list("abcd"), "a": "3", "right": "c"})
    ended = []

    async def end(bot, wid):
        ended.append(wid)
    monkeypatch.setattr(R, "_end_now", end)
    await R.quiz_step(None, _M("1", uid=10), lv)
    assert ended == []
    await R.quiz_step(None, _M("2", uid=20), lv)
    assert ended == [1]


# ── پول‌سنجیِ واقعی (دیتابیس): هر چهار بازیِ جدید ──
from datetime import timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_game as G
import platform_group_money as M
import platform_wager as W

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
        for uid in (1, 2):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", balance=100000))
        await s.commit()
    R.LIVE.clear()
    yield mk
    await eng.dispose()


async def _bal(mk, uid):
    async with mk() as s:
        return int((await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))).balance)


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["word", "scramble", "quiz", "emoji"])
async def test_new_games_create_join_settle_and_conserve_money(env, game):
    mk = env
    w, err = await W.create_wager(game, CHAT, (1, "u1", None), 10000, None, CFG)
    assert err is None and await _bal(mk, 1) == 90000
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None and r.w.status == "playing" and json.loads(r.w.state)
    won = await R.settle_to(w.id, 2)
    assert won.status == "settled" and await _bal(mk, 2) == 90000 + 19000 and await _bal(mk, 1) == 90000
    text, _ = W.render(won)
    assert "برنده" in text


@pytest.mark.asyncio
async def test_quiz_both_wrong_refunds_both_immediately(env):
    mk = env
    w, _ = await W.create_wager("quiz", CHAT, (1, "u1", None), 10000, None, CFG)
    r = await R.join_race(w.id, (2, "u2", None))
    st = json.loads(r.w.state)
    wrong = [d for d in "1234" if d != st["a"]]
    lv = {"wid": w.id, "chat": CHAT, "bot": 0, "kind": "quiz", "players": {1, 2}, "answer": st["a"], "n": None,
          "st": st, "alts": None, "until": time.monotonic() + 100}
    R.LIVE[w.id] = lv

    class FakeBot:
        sent = []

        async def edit_message_text(self, *a, **k):
            pass

        async def send_message(self, chat, text, **k):
            FakeBot.sent.append(text)
    await R.quiz_step(FakeBot(), _M(wrong[0], uid=1), lv)
    assert await _bal(mk, 1) == 90000                           # هنوز بازی جاری است
    await R.quiz_step(FakeBot(), _M(wrong[1], uid=2), lv)
    assert await _bal(mk, 1) == 100000 and await _bal(mk, 2) == 100000      # هر دو کامل برگشت
    async with mk() as s:
        assert (await s.get(models.PlatformWager, w.id)).status == "expired"
    assert any("هر دو" in t for t in FakeBot.sent) and w.id not in R.LIVE
