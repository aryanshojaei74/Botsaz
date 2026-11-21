import asyncio
import platform_wager as core
import platform_wager_race as race

_STORE = {}


async def _get(k, d=""): return _STORE.get(k, d)
async def _set(k, v): _STORE[k] = v


def _patch(monkeypatch):
    _STORE.clear()
    monkeypatch.setattr(core, "_pset", _get)
    monkeypatch.setattr(core, "_set", _set)


def test_legacy_master_flag_controls_when_game_key_unset(monkeypatch):
    _patch(monkeypatch)
    assert asyncio.run(race.game_on("speed")) is False
    _STORE["pgr_on"] = "1"
    assert asyncio.run(race.game_on("speed")) is True


def test_toggle_on_sets_master_and_off_is_per_game(monkeypatch):
    _patch(monkeypatch)
    assert asyncio.run(race.toggle_game("math")) is True
    assert _STORE["pgr_on"] == "1" and _STORE["platform_wager_math"] == "on"
    assert asyncio.run(race.game_on("speed")) is True      # کلیدِ اختصاصیِ سرعت خالی ⇒ از کلیدِ کلی
    assert asyncio.run(race.toggle_game("math")) is False
    assert asyncio.run(race.game_on("math")) is False
    assert asyncio.run(race.game_on("speed")) is True


def test_new_pattern_game_is_off_until_admin_enables(monkeypatch):
    _patch(monkeypatch)
    _STORE["pgr_on"] = "1"
    assert asyncio.run(race.game_on("speed")) is True
    assert asyncio.run(race.game_on("pattern")) is False
    assert asyncio.run(race.toggle_game("pattern")) is True
    assert asyncio.run(race.game_on("pattern")) is True


def test_pattern_questions_are_valid_and_unambiguous():
    import random
    from platform_wager_race import make_pattern, new_question, parse_race_command
    rnd = random.Random(7)

    class R:
        @staticmethod
        def randbelow(n): return rnd.randrange(n)
    for _ in range(500):
        q, a = make_pattern(R)
        nums = [int(x) for x in q.replace("؟", "").replace("،", " ").split()]
        assert a.isdigit() and 4 <= len(nums) <= 6 and int(a) > nums[-1] and q.endswith("؟")
    assert new_question("pattern")["a"].isdigit()
    assert parse_race_command("شرطبندی الگو 10000") == ("pattern", 10000)
    assert parse_race_command("الگو 5000") == ("pattern", 5000)


def test_tenants_get_pattern_behind_the_wager_addon():
    from platform_wager_race import RACE_GAMES, TENANT_RACE_GAMES
    assert "pattern" in RACE_GAMES and "pattern" in TENANT_RACE_GAMES
    import platform_wager as core
    text = core.help_text({"pattern": True}, bot_id=5, cfg={"min": 1000, "max": 0, "fee_pct": 0})
    assert "الگو" in text


def test_pattern_render_playing_and_settled():
    import json
    from models import PlatformWager
    from platform_wager_race import render_race
    w = PlatformWager(id=1, game="pattern", status="playing", chat_id=-1, creator_id=1, opponent_id=2,
                      creator_name="a", opponent_name="b", amount=1000, bot_id=0,
                      state=json.dumps({"q": "2 ، 4 ، 6 ، ؟", "a": "8"}, ensure_ascii=False))
    text, _ = render_race(w)
    assert "2 ، 4 ، 6 ، ؟" in text and "الگو" in text
    w.status, w.winner_id, w.payout_amount, w.fee_amount = "settled", 1, 1900, 100
    text, _ = render_race(w)
    assert "جواب: <b>8</b>" in text
