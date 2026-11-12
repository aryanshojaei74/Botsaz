"""PATCH_WORD_GAMES_V1 (SINA): مسابقه‌هایِ پولیِ «کلمه‌سازی» و «درهم»."""
import json
import time
from collections import Counter
from types import SimpleNamespace as NS

import pytest

import platform_wager as core
import platform_wager_race as R
import word_bank as WB
from models import PlatformWager


# ── فرهنگ و پازل ──
def test_dictionary_is_clean_and_has_enough_short_words():
    assert all(all(c in WB._PERSIAN for c in w) for w in WB.DICT)
    assert len(WB.BY_LEN[2]) >= 30 and len(WB.BY_LEN[3]) >= 150 and len(WB.BY_LEN[4]) >= 100


def test_every_word_puzzle_is_solvable_from_the_dictionary():
    for _ in range(150):
        pz = WB.make_word_puzzle()
        assert pz and len(pz["need"]) == pz["total"]
        for L, n in Counter(pz["need"]).items():
            assert len(WB.candidates(pz["letters"], L)) >= n, (pz, L)


def test_scramble_is_a_real_permutation_and_never_identity():
    for _ in range(100):
        pz = WB.make_scramble()
        assert sorted(pz["q"].replace(" ", "")) == sorted(pz["a"]) and pz["q"].replace(" ", "") != pz["a"]
        assert pz["a"] in WB.DICT and all(sorted(x) == sorted(pz["a"]) for x in pz["alts"])


def test_normalisation_and_check_word_codes():
    assert WB.norm("كتاب‌ ") == "کتاب" and WB.norm("ماهَ") == "ماه"
    letters, need = list("ابریم"), [2, 3]
    assert WB.check_word("با", letters, need, []) == ("ok", "با")
    assert WB.check_word("ابر", letters, need, ["با"])[0] == "ok"
    assert WB.check_word("ابر", letters, need, ["با", "ابر"])[0] == "dup"
    assert WB.check_word("بار", letters, need, ["با", "ابر"])[0] == "slots_full"      # جایگاهِ ۳ حرفی پر است
    assert WB.check_word("کتاب", letters, need, [])[0] == "bad_letters"
    assert WB.check_word("برم", letters, need, [])[0] == "not_in_dict"              # قابل‌ساخت ولی در فرهنگ نیست
    assert WB.check_word("سلام ۱", letters, need, [])[0] == "bad_shape"
    assert WB.check_word("اا", list("ابر"), [2], [])[0] == "bad_letters"            # هر حرف فقط به‌اندازهٔ تعدادش


# ── دستورها ──
def test_commands_are_parsed_with_and_without_zwnj():
    for txt in ("کلمه سازی 10000", "کلمه‌سازی ۱۰۰۰۰", "شرطبندی کلمه‌سازی 10,000", "بازی کلمه سازی 10000"):
        assert R.parse_race_command(txt) == ("word", 10000), txt
    assert R.parse_race_command("درهم 5000") == ("scramble", 5000)
    assert R.parse_race_command("شرط بندی درهم 5000") == ("scramble", 5000)
    assert R.parse_race_command("سرعت 10000") == ("speed", 10000)               # قبلی‌ها سالم
    assert R.parse_race_command("کلمه سازی") is None


def test_games_are_registered_and_default_off_helper():
    assert "word" in R.RACE_GAMES and "scramble" in R.TENANT_RACE_GAMES
    assert R.SECS["word"] == 120 and R.SECS["scramble"] == 45
    assert core.GAME_FA["word"] == "کلمه‌سازی" and core.GAME_FA["scramble"] == "درهم"
    assert len("scramble") <= 8                                                   # ستونِ game (String(8))


# ── پرسش و نمایش ──
def _wager(game, st, status="playing"):
    return PlatformWager(id=1, bot_id=0, chat_id=-1, game=game, creator_id=10, creator_name="A", opponent_id=20,
                         opponent_name="B", amount=10000, status=status, state=json.dumps(st, ensure_ascii=False))


def test_render_word_and_scramble_playing_and_settled():
    st = R.new_question("word")
    t, _kb = R.render_race(_wager("word", st))
    assert "با این حروف کلمه بسازید" in t and st["q"] in t and "کلمهٔ درست" in t
    st2 = R.new_question("scramble")
    t, _ = R.render_race(_wager("scramble", st2))
    assert "درهم" in t and st2["q"] in t and st2["w"] not in t.replace(st2["q"], "")      # جواب لو نمی‌رود
    w = _wager("scramble", st2, "settled")
    w.winner_id, w.payout_amount, w.fee_amount = 10, 19000, 1000
    t, _ = R.render_race(w)
    assert st2["w"] in t and "برنده" in t


def test_need_text():
    assert "۳ کلمهٔ درست" in R._need_text([2, 3, 3]) and "یکی ۲ حرفی" in R._need_text([2, 3, 3]) and "دو تا ۳ حرفی" in R._need_text([2, 3, 3])


# ── پاسخ‌ها ──
class _M:
    def __init__(self, text, uid=10, chat=-1):
        self.text, self.from_user, self.chat = text, NS(id=uid, full_name="P"), NS(id=chat)
        self.replies = []

    async def reply(self, text, **k):
        self.replies.append(text)


def _live(game, st):
    lv = {"wid": 1, "chat": -1, "bot": 0, "kind": game, "players": {10, 20}, "answer": st.get("a"), "n": None,
          "st": st, "alts": st.get("alts"), "until": time.monotonic() + 100}
    R.LIVE.clear()
    R.LIVE[1] = lv
    return lv


def test_match_scramble_accepts_answer_and_anagram_only():
    st = {"q": "ا ی س ه", "a": "سیاه", "w": "سیاه", "alts": ["سایه"]}
    _live("scramble", st)
    assert R._match(_M("سیاه")) and R._match(_M("سایه")) and R._match(_M("سیاه‌"))
    assert R._match(_M("ماه")) is None and R._match(_M("سلام"), 0) is None


def test_match_word_only_for_buildable_attempts():
    st = {"q": "ی ا ر ب م", "letters": list("یاربم"), "need": [2, 3]}
    _live("word", st)
    assert R._match(_M("ابر")) and R._match(_M("با"))
    assert R._match(_M("سلام")) is None and R._match(_M("hello")) is None and R._match(_M("ابر ابر")) is None


@pytest.mark.asyncio
async def test_word_step_progress_then_win_per_player_independently():
    st = {"q": "ی ا ر ب م", "letters": list("یاربم"), "need": [2, 3]}
    lv = _live("word", st)
    m1 = _M("با", uid=10)
    assert await R.word_step(None, m1, lv) == "done" and "۱ از ۲" in m1.replies[0]
    m2 = _M("بار", uid=20)                                       # بازیکنِ دیگر جدا پیش می‌رود
    assert await R.word_step(None, m2, lv) == "done" and "۱ از ۲" in m2.replies[0]
    assert await R.word_step(None, _M("ابر", uid=10), lv) == "win"
    assert lv["prog"][10] == ["با", "ابر"] and lv["prog"][20] == ["بار"]


@pytest.mark.asyncio
async def test_word_step_rejections_are_rate_limited_and_informative():
    st = {"q": "ی ا ر ب م", "letters": list("یاربم"), "need": [2, 3]}
    lv = _live("word", st)
    R._WORD_NOTE.clear()
    await R.word_step(None, _M("با"), lv)
    m = _M("با")
    await R.word_step(None, m, lv)
    assert m.replies and "قبلاً" in m.replies[0]
    m2 = _M("با")
    await R.word_step(None, m2, lv)                                # کمتر از ۴ ثانیه ⇒ ساکت
    assert m2.replies == []
    R._WORD_NOTE.clear()
    m3 = _M("برم")
    await R.word_step(None, m3, lv)
    assert m3.replies and "فرهنگ" in m3.replies[0]


@pytest.mark.asyncio
async def test_main_bot_new_games_default_on_with_master_pattern_still_off(monkeypatch):
    store = {"pgr_on": "1"}

    async def pset(key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(core, "_pset", pset)
    for g in ("word", "scramble", "quiz", "emoji", "guess", "speed", "math"):
        assert await R.game_on(g) is True, g
    assert await R.game_on("pattern") is False
    store["platform_wager_quiz"] = "off"
    assert await R.game_on("quiz") is False
    store["pgr_on"] = "0"
    assert await R.game_on("word") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["word", "scramble", "quiz", "emoji"])
async def test_new_games_in_customer_bots_require_the_purchased_addon(monkeypatch, game):
    """فقط خریدارانِ افزونهٔ «بازی‌هایِ گروهی» (و بات‌سازِ اصلی که افزونه نمی‌خواهد)؛ حتی ربات حرفه‌ای بدونِ خرید نه."""
    import tenant.shared.wager_race as WR
    from aiogram.dispatcher.event.bases import SkipHandler

    async def ra(bot, uid):
        return NS(id=5, field="vpn_sales", plan="pro"), False                 # پلنِ حرفه‌ای ولی غیرِ ادمین
    replies = []

    class Msg:
        from_user = NS(id=9, is_bot=False)
        sender_chat = None

        async def reply(self, text, **k):
            replies.append(text)

    async def no_addon(bot_id, addon=None):
        return False
    monkeypatch.setattr(WR, "_row_and_admin", ra)
    monkeypatch.setattr(WR, "addon_active", no_addon)
    with pytest.raises(SkipHandler):
        await WR._gate(Msg(), NS(), game)
    assert replies == []                                                    # عضوِ عادی: سکوت (بازی وجود ندارد)

    async def yes(bot_id, addon=None):
        return True

    async def master(bot_id=0):
        return True

    async def ge(g, bot_id=0):
        return True
    monkeypatch.setattr(WR, "addon_active", yes)
    monkeypatch.setattr(WR.core, "master_enabled", master)
    monkeypatch.setattr(WR.core, "game_enabled", ge)
    row, _adm = await WR._gate(Msg(), NS(), game)
    assert row is not None                                                  # با خریدِ افزونه باز می‌شود
