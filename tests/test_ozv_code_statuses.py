import pytest
from tenant.bots.virtual_service import ozvinoo_vnum as Z

P = {"id": "ozv1", "kind": "ozvinoo", "token": "tok12345", "active": True}


def _patch(monkeypatch, status, payload):
    async def fake(provider, method, path, *, params=None, body=None):
        return status, payload
    monkeypatch.setattr(Z, "_oz_request", fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,expect", [
    (200, {"status": True, "status_code": 200, "data": {"order_id": 1, "code": "12345"}}, "ok"),
    (202, {"status": False, "status_code": 202, "message": "Waiting for code"}, "pending"),
    (402, {"status": False, "status_code": 402, "message": "Insufficient balance to complete the order"}, "nobalance"),
    (200, {"status": False, "status_code": 402, "message": "Insufficient balance"}, "nobalance"),
    (422, {"status": False, "status_code": 422, "message": "Account access denied"}, "noaccess"),
    (404, {"status": False, "status_code": 404, "message": "Order does not exist"}, "missing"),
    (200, {"status": False, "status_code": 404, "message": "Order does not exist"}, "missing"),      # خطای منطقی با HTTP 200
    (200, {"status": False, "status_code": 500, "message": "x"}, "error"),
    (500, {"message": "boom"}, "error"),
])
async def test_fetch_code_statuses(monkeypatch, status, payload, expect):
    _patch(monkeypatch, status, payload)
    kind, code, why = await Z._pv_fetch_code(P, 123)
    assert kind == expect and (code == "12345" if expect == "ok" else code == "")


@pytest.mark.asyncio
async def test_provider_alert_is_throttled(monkeypatch):
    import aiogram
    sent = []

    class S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, m, pk):
            from types import SimpleNamespace as NS
            return NS(token="1:T", owner_id=9)

    class B:
        def __init__(self, token):
            from types import SimpleNamespace as NS
            self.session = NS(close=self._c)
        async def _c(self): pass
        async def send_message(self, chat, text, **k): sent.append((chat, text))
    from types import SimpleNamespace as NS
    monkeypatch.setattr(Z, "Session", lambda: S())
    monkeypatch.setattr(aiogram, "Bot", B)
    Z._PROVIDER_ALERT_AT.clear()
    o = NS(bot_id=1, tracking_code="148", id=5, link="+573237189885")
    await Z._ozv_alert_provider(o, P, "nobalance", "Insufficient balance")
    await Z._ozv_alert_provider(o, P, "nobalance", "Insufficient balance")
    assert len(sent) == 1 and sent[0][0] == 9 and "اعتبارِ حسابِ شما کافی نیست" in sent[0][1] and "148" in sent[0][1]
