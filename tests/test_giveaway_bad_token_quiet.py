import time
import pytest
from types import SimpleNamespace as NS
from tenant.bots.giveaway import loop as L


def test_token_ok():
    assert L._token_ok("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    for bad in ("", "released#12345", "abc", "123:"):
        assert not L._token_ok(bad)


@pytest.mark.asyncio
async def test_invalid_token_is_skipped_quietly_and_not_retried(monkeypatch, caplog):
    L._unauth_logged.clear(); L._skip_until.clear()
    calls = []

    async def due():
        return [(28, 7)]

    class S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, model, pk): return NS(token="released#xyz", status="active", owner_id=1)
    monkeypatch.setattr(L.E, "due_for_auto_end", due)
    monkeypatch.setattr(L, "Session", lambda: S())
    monkeypatch.setattr(L, "Bot", lambda *a, **k: calls.append(1))
    import logging
    with caplog.at_level(logging.WARNING):
        await L.end_due_once()
        await L.end_due_once()
    assert calls == []                                            # هرگز Bot ساخته نشد ⇒ هیچ traceback
    warns = [r for r in caplog.records if "معتبر نیست" in r.getMessage()]
    assert len(warns) == 1 and all(r.exc_info is None for r in caplog.records)
    assert L._skip_until[28] > time.monotonic() + 5 * 3600
