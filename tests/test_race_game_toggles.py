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
