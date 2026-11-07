"""PATCH_WGRES_PANEL_V1 — پنلِ وایرگاردِ نماینده (Reseller API، X-API-Key): کلاینت + ثبت در فکتوری/پلن/منو."""
import time

import pytest

from plans import Feature, has_feature
from tenant.shared.panel_clients import vpn_panel_factory as F
from tenant.shared.panel_clients.vpn_panel import PanelError
from tenant.shared.panel_clients.wgres_panel import WgResellerPanelClient, _short_name, parse_size_bytes

GB = 1024 ** 3
CONF = "[Interface]\nPrivateKey = x\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = y\nEndpoint = 1.2.3.4:51820\n"


class FakePanel:
    """شبیه‌سازِ پنل؛ روی _request نصب می‌شود."""
    def __init__(self):
        self.peers, self.calls, self.fail_export = {}, [], False

    async def __call__(self, method, path, *, raw=False, json=None, **kw):
        self.calls.append((method, path, json))
        if path.startswith("/api/developer/reseller-info"):
            return {"success": True, "username": "reseller1", "interface": "wg0", "capacity_gb": 500, "used_gb": 12.5,
                    "remaining_gb": 487.5, "active_peers_count": len(self.peers)}
        if path.startswith("/api/peers"):
            return {"peers": list(self.peers.values()), "total_pages": 1}
        if path.startswith("/api/export-peer"):
            if self.fail_export:
                raise PanelError("HTTP 404")
            return CONF
        n = (json or {}).get("peerName")
        if path == "/api/create-peer":
            days = int(json["expiryDays"])
            self.peers[n] = {"peer_name": n, "peer_ip": "10.0.0.2", "config": "wg0.conf", "limit": json["dataLimit"], "used": 0,
                             "used_human": "0 MiB", "remaining_time": days * 1440 if days else None,
                             "first_usage": json["firstUsage"], "short_link": f"https://p.example/s/{n}",
                             "monitor_blocked": False, "expiry_blocked": False, "status": "active"}
            return {"message": "ok"}
        if n not in self.peers:
            return None
        p = self.peers[n]
        if path == "/api/edit-peer":
            if json.get("dataLimit"):
                p["limit"] = json["dataLimit"]
            if json.get("expiryDays") is not None:
                p["remaining_time"] = int(json["expiryDays"]) * 1440
        elif path == "/api/reset-traffic":
            p["used"] = 0
        elif path == "/api/toggle-peer":
            p["monitor_blocked"] = bool(json["blocked"])
        elif path == "/api/delete-peer":
            del self.peers[n]
        return {"message": "ok"}


@pytest.fixture
def cl(monkeypatch):
    c = WgResellerPanelClient("https://panel.example.com:2096/dashboard", "-", "KEY123")
    fp = FakePanel()
    monkeypatch.setattr(c, "_request", fp)
    c.fp = fp
    return c


def test_helpers():
    assert parse_size_bytes("50GiB") == 50 * GB and parse_size_bytes("500MiB") == 500 * 1024 ** 2 and parse_size_bytes("1.5 GB") == int(1.5 * GB)
    assert parse_size_bytes("unlimited") == 0 and parse_size_bytes(None) == 0 and parse_size_bytes("") == 0 and parse_size_bytes(1024) == 1024
    assert 3 <= len(_short_name("mctWG528196579jrj-wg-TR")) <= 10 and _short_name("mctWG528196579jrj-wg-TR") == _short_name("mctWG528196579jrj-wg-TR")
    assert _short_name("abcd") == "abcd"


def test_constructor_normalises_url_and_uses_password_as_key():
    c = WgResellerPanelClient("https://panel.example.com:2096/dashboard/", "-", " KEY ")
    assert c.base_url == "https://panel.example.com:2096" and c.api_key == "KEY"


@pytest.mark.asyncio
async def test_full_lifecycle(cl):
    r = await cl.create_account("longname_bot123_user", 30, 30)
    name = r["email"]
    assert name == _short_name("longname_bot123_user") and r["sub_link"].endswith(name)
    body = [c for c in cl.fp.calls if c[1] == "/api/create-peer"][0][2]
    assert body == {"peerName": name, "dataLimit": "30GiB", "expiryDays": 30, "firstUsage": False}
    u = await cl.get_client("longname_bot123_user")
    assert u["data_limit"] == 30 * GB and u["used_traffic"] == 0 and u["status"] == "active" and u["expire"] > time.time() + 29 * 86400
    await cl.add_traffic("longname_bot123_user", 10)
    assert (await cl.get_client("longname_bot123_user"))["data_limit"] == 40 * GB
    await cl.renew("longname_bot123_user", 15, reset_usage=True)
    u = await cl.get_client("longname_bot123_user")
    assert 44 * 86400 < u["expire"] - time.time() <= 45 * 86400 + 60
    await cl.set_enabled("longname_bot123_user", False)
    assert (await cl.get_client("longname_bot123_user"))["status"] == "disabled"
    usage = await cl.get_usage("longname_bot123_user")
    assert usage["enable"] is False and usage["total"] == 40 * GB
    assert (await cl.list_clients())[0]["email"] == name
    assert await cl.delete_account("longname_bot123_user") and await cl.get_client("longname_bot123_user") is None


@pytest.mark.asyncio
async def test_on_hold_unlimited_and_mb(cl):
    await cl.create_account("user01", 0, 30, on_hold=True)
    p = cl.fp.peers["user01"]
    assert p["first_usage"] is True
    r = await cl.create_account("user02", None, 0, mb=500)
    assert cl.fp.peers["user02"]["limit"] == "500MiB" and r["expiry_ms"] == 0
    cl.fp.peers["user03"] = {"peer_name": "user03", "limit": "unlimited", "remaining_time": 0, "used": 5 * GB, "monitor_blocked": False}
    u = await cl.get_client("user03")
    assert u["data_limit"] == 0 and u["expire"] < time.time() and u["used_traffic"] == 5 * GB
    assert (await cl.add_traffic("user03", 5))["data_limit"] == 0                  # نامحدود دست‌نخورده


@pytest.mark.asyncio
async def test_native_wireguard_file_and_missing_export(cl):
    await cl.create_account("wguser", 5, 10)
    eps = await cl.get_wireguard_endpoints("wguser")
    assert eps and eps[0]["file"] == "wguser.conf"
    assert "[Interface]" in await cl.fetch_wireguard_config_text("wguser", 1)
    cl2 = cl
    cl2._cfg_cache.clear()
    cl2.fp.fail_export = True
    assert await cl2.get_wireguard_endpoints("wguser") == []                    # export ممکن نشد ⇒ مسیرِ قدیمی/ساب‌لینک


@pytest.mark.asyncio
async def test_test_connection_and_unsupported(cl):
    res = await cl.test_connection()
    assert res["ok"] and res["username"] == "reseller1" and res["capacity_gb"] == 500 and res["inbounds"] == []
    with pytest.raises(PanelError):
        await cl.reset_sub("x")
    assert await cl.rename_account("a", "b") is False and await cl.set_ip_limit("a", 2) is False
    bad = WgResellerPanelClient("https://x.example.com", "-", "")
    with pytest.raises(PanelError):
        await bad.login()


def test_registered_everywhere():
    assert F._CLIENT_BY_PANEL_KEY["wgres"] is WgResellerPanelClient and F.client_class_for("wgres#2") is WgResellerPanelClient
    from tenant.bots.vpn.vpn_admin import PANEL_LABELS, _panel_labels_for_plan, _SUB_BASE_AUTO
    assert "wgres" in PANEL_LABELS and "wgres" in _SUB_BASE_AUTO
    assert "wgres" in _panel_labels_for_plan("pro") and "wgres" not in _panel_labels_for_plan("growth")
    assert has_feature("pro", Feature.WGRES_PANEL) and not has_feature("growth", Feature.WGRES_PANEL) and not has_feature("free", Feature.WGRES_PANEL)
    class P:
        panel_key, base_url, username, password, sub_base, inbound_ids, api_key = "wgres", "https://p.example.com:2096", "-", "K", "", "", None
    c = F.make_panel_client(P())
    assert isinstance(c, WgResellerPanelClient) and c.api_key == "K"
