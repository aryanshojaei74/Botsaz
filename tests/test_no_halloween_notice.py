"""PATCH_NO_HALLOWEEN_NOTICE_V1 — بعد از هالوین: نه پیامِ «هالوین تمام شد» و نه تبلیغِ «بازیِ رایگان با شیرینی» در گروه."""
from types import SimpleNamespace as NS

import pytest

import platform_candy as CA
import platform_group_game as GG
import platform_wager_race as WR


class _Msg:
    def __init__(self, text="شرطبندی سرعت"):
        self.text = text
        self.from_user = NS(id=5, is_bot=False)
        self.sender_chat = None
        self.chat = NS(id=-100)
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)


def _candy(monkeypatch, on):
    async def active():
        return on
    monkeypatch.setattr(CA, "candy_active", active)


@pytest.mark.asyncio
async def test_free_game_word_is_silently_ignored_when_halloween_is_over(monkeypatch):
    _candy(monkeypatch, False)
    m = _Msg("سرعت")
    assert await GG._free_ended(m, "سرعت") is True          # هندلر متوقف می‌شود
    assert m.replies == []                                   # ولی هیچ پیامی در گروه نمی‌آید


@pytest.mark.asyncio
async def test_free_game_still_runs_while_halloween_is_on(monkeypatch):
    _candy(monkeypatch, True)
    m = _Msg("سرعت")
    assert await GG._free_ended(m, "سرعت") is False
    assert m.replies == []


async def _setup_race(monkeypatch):
    async def yes(*a, **k):
        return True

    async def cfg():
        return {"min": 20000, "max": 1000000, "fee_pct": 0}
    monkeypatch.setattr(WR, "_group_allowed", yes)
    monkeypatch.setattr(WR, "enabled", yes)
    monkeypatch.setattr(WR, "_bet_enabled", yes)
    monkeypatch.setattr(WR.core, "load_cfg", cfg)
    monkeypatch.setattr(WR.core, "example_amount", lambda c: 20000)


@pytest.mark.asyncio
async def test_paid_hint_has_no_free_candy_line_after_halloween(monkeypatch):
    await _setup_race(monkeypatch)
    _candy(monkeypatch, False)
    m = _Msg("شرطبندی سرعت")
    await WR.race_paid_hint(m)
    assert len(m.replies) == 1
    assert "شرطبندی سرعت 20000" in m.replies[0]
    assert "بازیِ رایگان" not in m.replies[0] and "شیرینی" not in m.replies[0]


@pytest.mark.asyncio
async def test_paid_hint_keeps_free_line_during_halloween(monkeypatch):
    await _setup_race(monkeypatch)
    _candy(monkeypatch, True)
    m = _Msg("شرطبندی سرعت")
    await WR.race_paid_hint(m)
    assert "بازیِ رایگان با شیرینی" in m.replies[0]


@pytest.mark.asyncio
async def test_help_section_mentions_free_candy_only_while_on(monkeypatch):
    await _setup_race(monkeypatch)

    async def on(game):
        return True
    monkeypatch.setattr(WR, "game_on", on)
    _candy(monkeypatch, False)
    off_txt = await WR.help_section()
    _candy(monkeypatch, True)
    on_txt = await WR.help_section()
    assert off_txt and "شیرینی" not in off_txt
    assert "بازیِ رایگان با شیرینی" in on_txt
