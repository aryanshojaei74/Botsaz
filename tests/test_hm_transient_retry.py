"""ABOL: خطاهایِ گذرایِ HMPanel (۵۰۲ در GET و «update already in progress») یک‌بار با صبر تکرار شوند."""
import asyncio

import pytest

from tenant.shared.panel_clients import hmpanel_panel as hm

pytestmark = pytest.mark.asyncio


def test_transient_classifier():
    assert hm._is_transient_hm("GET", 502, "<html>502 Bad Gateway</html>")
    assert hm._is_transient_hm("get", 503, "")
    assert not hm._is_transient_hm("POST", 502, "")           # نوشتن بی‌خطر نیست؛ تکرار نمی‌شود
    assert hm._is_transient_hm("PATCH", 400, '{"message":"Client update is already in progress"}')
    assert not hm._is_transient_hm("PATCH", 400, '{"message":"invalid body"}')
    assert not hm._is_transient_hm("GET", 500, "")


class _Resp:
    def __init__(self, status, text):
        self.status, self._t = status, text

    async def text(self):
        return self._t

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    def request(self, method, url, **kw):
        self.calls += 1
        return _Resp(*self.seq.pop(0))


def _client(monkeypatch, seq):
    c = hm.HMPanelClient.__new__(hm.HMPanelClient)
    c.api_key, c.base_url, c.timeout = "k", "http://x", 10
    sess = _Sess(seq)

    async def ens():
        return sess
    c._ensure_session = ens
    async def _nosleep(*_a, **_k):
        return None
    monkeypatch.setattr(hm.asyncio, "sleep", _nosleep)
    return c, sess


async def test_get_502_then_ok_is_retried(monkeypatch):
    c, sess = _client(monkeypatch, [(502, "<html>bad</html>"), (200, '{"a":1}')])
    assert await c._request("GET", "/v1/inbounds") == {"a": 1}
    assert sess.calls == 2


async def test_patch_in_progress_is_retried_but_post_502_is_not(monkeypatch):
    c, sess = _client(monkeypatch, [(400, "Client update is already in progress"), (200, "{}")])
    assert await c._request("PATCH", "/v1/clients/x", json={}) == {}
    assert sess.calls == 2
    c, sess = _client(monkeypatch, [(502, "bad")])
    with pytest.raises(hm.PanelError):
        await c._request("POST", "/v1/clients", json={})
    assert sess.calls == 1


async def test_persistent_failure_still_raises_after_three(monkeypatch):
    c, sess = _client(monkeypatch, [(502, "x")] * 3)
    with pytest.raises(hm.PanelError):
        await c._request("GET", "/v1/inbounds")
    assert sess.calls == 3
