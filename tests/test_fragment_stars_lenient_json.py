import json
import pytest


class _R:
    status = 200

    def __init__(self, raw): self._raw = raw
    async def read(self): return self._raw
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _S:
    def __init__(self, raw): self._raw = raw
    def request(self, *a, **k): return _R(self._raw)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
@pytest.mark.parametrize("modname", ["tenant.bots.virtual_service.fragment_stars"])
async def test_toncenter_call_survives_invalid_utf8(monkeypatch, modname):
    import importlib
    FS = importlib.import_module(modname)
    raw = json.dumps({"ok": True, "result": [{"m": "JUNK"}]}).encode().replace(b"JUNK", b"\xec\xff")
    monkeypatch.setattr(FS.aiohttp, "ClientSession", lambda *a, **k: _S(raw))
    data, err = await FS._toncenter_call("GET", "http://x/getTransactions")
    assert err == "" and data["ok"] is True
