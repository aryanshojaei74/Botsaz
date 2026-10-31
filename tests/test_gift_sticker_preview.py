"""PATCH_GIFT_STICKER_PREVIEW_V2 — استیکرِ گیفت: اگر ذخیره نبود از تلگرام گرفته شود؛ خطا بی‌صدا نماند."""
from types import SimpleNamespace as NS

import pytest

import tenant.bots.virtual_service.smm as SMM


class _Msg:
    def __init__(self, fail=False): self.sent, self.fail = [], fail
    async def answer_sticker(self, fid):
        if self.fail:
            raise RuntimeError("Bad Request: wrong file identifier")
        self.sent.append(fid)


class _Bot:
    def __init__(self, gifts): self._g, self.calls = gifts, 0
    async def get_available_gifts(self):
        self.calls += 1
        return NS(gifts=self._g)


@pytest.fixture
def store(monkeypatch):
    d = {}
    async def _get(bid, k, default=""): return d.get((bid, k), default)
    async def _set(bid, k, v): d[(bid, k)] = v
    monkeypatch.setattr(SMM, "_get_setting", _get)
    monkeypatch.setattr(SMM, "_set_setting", _set)
    return d


GIFTS = [NS(id="g1", sticker=NS(file_id="FID1")), NS(id="g2", sticker=NS(file_id="FID2"))]


@pytest.mark.asyncio
async def test_uses_stored_sticker_without_calling_telegram(store):
    store[(1, "gift_sticker:g1")] = "STORED"
    bot, m = _Bot(GIFTS), _Msg()
    assert await SMM._send_gift_sticker_preview(m, bot, 1, "g1") is True
    assert m.sent == ["STORED"] and bot.calls == 0


@pytest.mark.asyncio
async def test_fetches_and_stores_when_not_synced_yet(store):
    bot, m = _Bot(GIFTS), _Msg()
    assert await SMM._send_gift_sticker_preview(m, bot, 1, "g1") is True
    assert m.sent == ["FID1"] and store[(1, "gift_sticker:g2")] == "FID2"             # بقیهٔ گیفت‌ها هم ذخیره شد


@pytest.mark.asyncio
async def test_send_failure_returns_false_and_logs_reason(store, caplog):
    store[(1, "gift_sticker:g1")] = "BAD"
    with caplog.at_level("WARNING"):
        assert await SMM._send_gift_sticker_preview(_Msg(fail=True), _Bot(GIFTS), 1, "g1") is False
    assert any("wrong file identifier" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_disabled_setting_and_unknown_gift(store):
    store[(1, "gift_sticker_preview")] = "0"
    m = _Msg()
    assert await SMM._send_gift_sticker_preview(m, _Bot(GIFTS), 1, "g1") is False and m.sent == []
    store[(1, "gift_sticker_preview")] = "1"
    assert await SMM._send_gift_sticker_preview(m, _Bot(GIFTS), 1, "nope") is False
