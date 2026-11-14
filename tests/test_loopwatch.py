import asyncio
import logging
import time

import loopwatch


def test_blocked_loop_is_reported(caplog, monkeypatch):
    monkeypatch.setenv("LOOPWATCH_MS", "300")
    monkeypatch.setattr(loopwatch, "_started", False)

    def _blocker():
        time.sleep(1.2)  # همان کدی که باید در stack دیده شود

    async def go():
        loopwatch.start("t")
        await asyncio.sleep(0.6)
        _blocker()
        await asyncio.sleep(0.8)

    with caplog.at_level(logging.WARNING, logger="loopwatch"):
        asyncio.run(go())
    txt = caplog.text
    assert "event loop blocked" in txt and "_blocker" in txt
    assert "unblocked" in txt


def test_healthy_loop_is_silent(caplog, monkeypatch):
    monkeypatch.setenv("LOOPWATCH_MS", "800")
    monkeypatch.setattr(loopwatch, "_started", False)

    async def go():
        loopwatch.start("t")
        await asyncio.sleep(1.5)

    with caplog.at_level(logging.WARNING, logger="loopwatch"):
        asyncio.run(go())
    assert "blocked" not in caplog.text
