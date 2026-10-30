import asyncio

import halloween as HW
import platform_candy as C
import platform_group_game as G


def _patch(monkeypatch, candy_on, theme_on):
    async def cfg():
        return {"on": candy_on, "goal": 100, "amount": 1, "max": 1, "cap": 25}

    async def is_on(now=None):
        return theme_on
    monkeypatch.setattr(C, "cfg", cfg)
    monkeypatch.setattr(HW, "is_on", is_on)


def test_candy_active_needs_both_switches(monkeypatch):
    for candy, theme, want in ((True, True, True), (False, True, False), (True, False, False), (False, False, False)):
        _patch(monkeypatch, candy, theme)
        assert asyncio.run(C.candy_active()) is want


def test_award_gives_nothing_when_mode_off(monkeypatch):
    _patch(monkeypatch, True, False)                      # تم تمام شده
    calls = []

    async def add_points(*a, **k):
        calls.append(a)
        return 5

    async def total(uid):
        return 7
    monkeypatch.setattr(G, "add_points", add_points)
    monkeypatch.setattr(G, "_total", total)
    assert asyncio.run(G._award(1, "x", 3)) == (0, 7, "")
    assert calls == []


def test_help_has_no_candy_when_off(monkeypatch):
    _patch(monkeypatch, True, False)
    txt = asyncio.run(G.help_text(bet_games=False))
    assert "🍬" not in txt and "رایگان" not in txt.replace("بازیِ رایگان", "") and "حدس عدد»؛" not in txt


def test_free_games_and_table_stop_when_candy_off(monkeypatch):
    """بعد از تمام‌شدنِ هالوین: «حدس عدد/ریاضی/سرعت» بازیِ رایگان شروع نمی‌کنند و راهِ شرط‌بندی را می‌گویند؛ «جدول» ساکت."""
    import pytest
    from aiogram.dispatcher.event.bases import SkipHandler
    _patch(monkeypatch, True, False)
    monkeypatch.setattr(G, "_group_allowed", lambda cid: _aw(True))
    monkeypatch.setattr(G, "enabled", lambda: _aw(True))
    out = []

    class M:
        text = "حدس عدد"
        chat = type("C", (), {"id": 5})()
        from_user = type("U", (), {"id": 1, "is_bot": False, "full_name": "u"})()
        sender_chat = None

        async def reply(self, text, **k):
            out.append(text)
    asyncio.run(G.gg_start(M()))
    asyncio.run(G._start_race(M(), "math"))
    assert len(out) == 2 and "شرطبندی حدس عدد" in out[0] and "شرطبندی ریاضی" in out[1]
    assert 5 not in G._GAMES and 5 not in G._RACES
    with pytest.raises(SkipHandler):
        asyncio.run(G.gg_top(M()))


async def _aw(v):
    return v
