"""PATCH_VNUM_LOGOUT_V1 (Nexivo): دکمهٔ «🚪 خروج ربات از حساب» — فقط OzVinoo، با تأیید، بدونِ تداخلِ پیشوندِ callback."""
import re

import pytest

from tenant.bots.virtual_service import ozvinoo_vnum as oz


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]


def test_code_keyboard_has_logout_for_ozvinoo_but_not_when_unsupported():
    assert "vnum:ozlout:5" in _cbs(oz._code_kb(5, "12345"))
    assert "vnum:ozlout:5" not in _cbs(oz._code_kb(5, "12345", logout=False))
    assert "vnum:ozre:5" in _cbs(oz._code_kb(5, "12345", logout=False))     # دکمه‌هایِ قبلی سالم


def test_logout_callback_prefixes_do_not_shadow_each_other():
    src = open(oz.__file__, encoding="utf-8").read()
    prefixes = re.findall(r'F\.data\.startswith\("(vnum:oz[a-z]*:)"\)', src)
    for a in prefixes:
        for b in prefixes:
            assert a == b or not a.startswith(b), (a, b)


@pytest.mark.asyncio
async def test_provider_logout_maps_statuses(monkeypatch):
    calls = []

    def fake(status, payload=None):
        async def _r(provider, method, path, **kw):
            calls.append((method, path, kw.get("body")))
            return status, payload or {}
        return _r
    prov = {"id": "p", "token": "t"}
    monkeypatch.setattr(oz, "_oz_request", fake(200))
    assert await oz._pv_logout(prov, "123") == ("ok", "")
    assert calls[-1] == ("POST", "/telegram-numbers/number-services/", {"order_id": 123})
    monkeypatch.setattr(oz, "_oz_request", fake(202))
    assert (await oz._pv_logout(prov, "123"))[0] == "pending"
    monkeypatch.setattr(oz, "_oz_request", fake(404, {"message": "nf"}))
    assert (await oz._pv_logout(prov, "123"))[0] == "missing"
    monkeypatch.setattr(oz, "_oz_request", fake(422))
    assert (await oz._pv_logout(prov, "123"))[0] == "error"
    assert (await oz._pv_logout({"kind": "tnumber", "token": "t"}, "09"))[0] == "unsupported"
