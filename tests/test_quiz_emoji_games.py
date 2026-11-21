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
    t, kb = R.render_race(_wager("quiz", st))
    assert st["q"] in t and not any(o in t for o in st["opts"])              # PATCH_QUIZ_V2: در متن فقط سؤال
    btns = [b for r in kb.inline_keyboard for b in r]
    assert [b.text for b in btns] == st["opts"] and btns[0].callback_data == "pgr:qz:1:0"   # گزینه‌ها دکمهٔ شیشه‌ای
    t, _ = R.render_race(_wager("emoji", R.new_question("emoji")))
    assert "ایموجی" in t and "\u2063" in t                                  # نشانهٔ «بدونِ پرمیوم»
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
    assert R._match(_M("2")) is None and R._match(_M("سلام")) is None          # کوییز فقط با دکمه جواب می‌گیرد


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
@pytest.mark.parametrize("game", ["scramble", "quiz", "emoji"])
async def test_new_games_create_join_settle_and_conserve_money(env, game):
    mk = env
    w, err = await W.create_wager(game, CHAT, (1, "u1", None), 10000, None, CFG)
    assert err is None and await _bal(mk, 1) == 90000
    if game == "quiz":                                  # PATCH_QUIZ_CATS_V1: سازنده موضوع را انتخاب می‌کند
        assert "هنوز انتخابش" in (await R.join_race(w.id, (2, "u2", None))).err
        assert (await W.set_option(w.id, 1, "bio")).err is None
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
    await W.set_option(w.id, 1, "chem")
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


def test_emoji_questions_are_two_to_five_emojis():
    import quiz_bank as QB
    import re
    for q, ans in QB.EMOJI:
        n = len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", q))
        assert 2 <= n <= 7, q            # (حدِ بالا برایِ ZWJ/نشانه‌ها)
        assert ans


def test_quiz_pool_is_big_and_has_bio_chem_and_no_repeats_in_a_row():
    import quiz_bank as QB
    assert len(QB.QUIZ_ALL) >= 250
    assert any("یاخته" in q or "DNA" in "".join(o) for q, o in QB.QUIZ_ALL)
    assert any("شیمیایی" in q for q, _ in QB.QUIZ_ALL)
    seen = [QB.make_quiz(scope=(9, 9))["q"] for _ in range(200)]
    assert len(set(seen)) == 200


@pytest.mark.asyncio
async def test_quiz_button_right_wrong_and_single_chance(monkeypatch):
    from types import SimpleNamespace as NS
    lv = _live("quiz", {"q": "x", "opts": list("abcd"), "a": "3", "right": "c"})
    sent, ended = [], []

    class Bot_:
        async def send_message(self, chat, text, **k): sent.append(text)

    async def end(bot, wid): ended.append(wid)
    monkeypatch.setattr(R, "_end_now", end)
    answers = []

    class CB:
        def __init__(self, uid, idx):
            self.data = f"pgr:qz:1:{idx}"
            self.from_user = NS(id=uid, full_name="P")
            self.message = NS(chat=NS(id=-1))
            self.bot = Bot_()

        async def answer(self, text="", show_alert=False): answers.append(text)
    await R.quiz_press(CB(10, 0), 0)                                   # غلط ⇒ حذف + تگ
    assert 10 in lv["locked"] and "اشتباه زدی و از بازی حذف شدی" in sent[0] and "tg://user?id=10" in sent[0]
    await R.quiz_press(CB(10, 2), 0)                                   # حذف‌شده دیگر نمی‌تواند
    assert "یک فرصت" in answers[-1]
    await R.quiz_press(CB(99, 2), 0)                                   # غیرِ بازیکن
    assert "فقط دو بازیکن" in answers[-1]
    await R.quiz_press(CB(20, 1), 0)                                   # دومی هم غلط ⇒ پایانِ زودهنگام
    assert ended == [1]


# ── PATCH_QUIZ_CATS_V1: موضوعِ انتخابیِ سازنده، بدونِ تکرارِ پایدار، پیامِ گزینهٔ غلط ──
def test_every_category_has_questions_and_picker_buttons():
    import quiz_bank as QB
    assert set(QB.CATS) == {"foot", "general", "bio", "chem", "cinema", "riddle"}
    for k in QB.CATS:
        assert len(QB.POOLS[k]) >= 20, k
        q = QB.make_quiz(cat=k)
        assert q["cat"] == k and len(q["opts"]) == 4 and q["opts"][int(q["a"]) - 1] == q["right"]
    w = PlatformWager(id=7, bot_id=0, chat_id=-1, game="quiz", creator_id=10, creator_name="A", amount=1000,
                      status="pending", state="{}")
    t, kb = R.render_race(w)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "موضوع" in t and "pgw:o:7:foot" in cbs and "pgw:o:7:riddle" in cbs and "pgr:cancel:7" in cbs
    assert not any(":join:" in c for c in cbs)                      # تا انتخابِ موضوع، پیوستن نیست


def test_no_repeat_within_a_category_until_exhausted():
    import quiz_bank as QB
    used, seen = {}, set()
    n = len(QB.POOLS["riddle"])
    for _ in range(n):
        q = QB.make_quiz(cat="riddle", used=used)
        assert q["id"] not in seen
        seen.add(q["id"])
    assert len(seen) == n                                           # همهٔ سؤال‌ها یک‌بار بدونِ تکرار
    again = QB.make_quiz(cat="riddle", used=used)                   # بعد از اتمام دورِ تازه
    assert again["id"] in seen and len(used["riddle"]) == 1


@pytest.mark.asyncio
async def test_used_questions_persist_across_games_in_the_same_chat(env, monkeypatch):
    mk = env
    import config
    monkeypatch.setattr(config, "Session", mk)
    import quiz_bank as QB
    ids = []
    for _ in range(3):
        w, _e = await W.create_wager("quiz", CHAT, (1, "u1", None), 1000, None, CFG)
        await W.set_option(w.id, 1, "foot")
        r = await R.join_race(w.id, (2, "u2", None))
        ids.append(json.loads(r.w.state)["id"])
        await R.settle_to(w.id, 1)
    assert len(set(ids)) == 3                                       # بینِ بازی‌هایِ پشت‌سرهم تکرار نشد
    saved = await QB.load_used(0, CHAT)
    assert set(ids) <= set(saved.get("foot", []))
    assert json.loads(r.w.state)["cat"] == "foot"


@pytest.mark.asyncio
async def test_wrong_answer_message_names_the_chosen_option(monkeypatch):
    from types import SimpleNamespace as NS
    lv = _live("quiz", {"q": "x", "opts": ["الف", "ب", "ج", "د"], "a": "3", "right": "ج"})
    sent = []

    class Bot_:
        async def send_message(self, chat, text, **k): sent.append(text)

    async def noend(bot, wid): pass
    monkeypatch.setattr(R, "_end_now", noend)

    class CB:
        data = "pgr:qz:1:1"
        from_user = NS(id=10, full_name="Amir")
        message = NS(chat=NS(id=-1))
        bot = Bot_()
        async def answer(self, *a, **k): pass
    await R.quiz_press(CB(), 0)
    assert "اشتباه زدی" in sent[0] and "گزینهٔ انتخابی: «ب»" in sent[0]
