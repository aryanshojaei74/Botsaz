"""PATCH_MARZBAN_INBOUNDS_LIST_V1 — پنلِ هم‌خانوادهٔ مرزبان که /api/inbounds را «لیست» برمی‌گرداند (bot 1659 روی سرور)."""
import pytest

from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient


def _client(payload):
    c = MarzbanPanelClient("http://p", "u", "p")

    async def _req(method, path, **kw):
        assert (method, path) == ("GET", "/api/inbounds")
        return payload
    c._request = _req
    return c


@pytest.mark.asyncio
async def test_dict_shape_still_works_and_groups_by_protocol():
    c = _client({"vless": [{"tag": "VLESS TCP", "protocol": "vless"}], "trojan": [{"tag": "TR", "protocol": "trojan"}]})
    flat = await c.list_inbounds()
    assert {i["tag"] for i in flat} == {"VLESS TCP", "TR"}
    assert await c._inbounds_by_protocol([]) == {"vless": ["VLESS TCP"], "trojan": ["TR"]}


@pytest.mark.asyncio
async def test_list_of_dicts_does_not_crash_and_keeps_protocols():
    c = _client([{"tag": "A", "protocol": "vless"}, {"tag": "B", "protocol": "vmess"}])
    flat = await c.list_inbounds()
    assert {(i["tag"], i["protocol"]) for i in flat} == {("A", "vless"), ("B", "vmess")}
    assert await c._inbounds_by_protocol(["A"]) == {"vless": ["A"]}


@pytest.mark.asyncio
async def test_list_of_bare_tags_lists_them_but_never_guesses_protocol_for_user_creation():
    c = _client(["X", "Y"])
    flat = await c.list_inbounds()
    assert {i["tag"] for i in flat} == {"X", "Y"}
    assert await c._inbounds_by_protocol([]) == {}


@pytest.mark.asyncio
async def test_garbage_response_is_empty_not_a_crash():
    assert await _client("oops").list_inbounds() == []
    assert await _client(None).list_inbounds() == []
