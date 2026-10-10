"""PATCH_EYLAN_PANEL_V1 — کلاینتِ پنلِ ایلان در برابرِ یک پنلِ ساختگی مطابقِ swagger.json."""
import datetime as dt
import time

import pytest

from tenant.shared.panel_clients import eylan_panel as EP
from tenant.shared.panel_clients.vpn_panel import PanelError

GB = 1024 ** 3


class FakeEylan:
    """پنلِ ساختگی: همان مسیرها و شکلِ پاسخ‌هایِ اسپک."""

    def __init__(self):
        self.users = {}
        self.calls = []

    async def __call__(self, method, path, *, auth=True, json=None, **kw):
        self.calls.append((method, path, json))
        if path == "/api/v1/status":
            return {"success": True, "message": "Panel is online"}
        if path == "/api/v1/users/stats":
            return {"success": True, "allowed_resources": {
                "nodes": [{"id": 1, "name": "eylan1", "status": "online"}],
                "wg_instances": [{"id": 1, "name": "wg1", "is_default": True},
                                 {"id": 2, "name": "wg2", "display_name": "36+"}],
                "singbox_inbounds": [{"id": 1, "tag": "reality-main", "type": "vless"}]}}
        if path == "/api/v1/users/list_all":
            return {"success": True, "users": [
                {"username": u, "data_limit": d["data_limit"], "download_bytes": d["used"],
                 "is_active": d["active"], "expiry_date": d["expiry"]} for u, d in self.users.items()]}
        if path == "/api/v1/users" and method == "POST":
            name = json["username"]
            if name in self.users:      # کلیدِ نماینده: عدد اضافه می‌شود
                name = name + "4821"
            days = json.get("expiry_days")
            exp = (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else ""
            self.users[name] = {"data_limit": json.get("data_limit", 0), "used": 0, "active": True,
                                "expiry": exp, "body": json}
            return {"success": True, "message": "1 user(s) created.", "created_users": [name]}
        parts = path.split("/")
        u = parts[4] if len(parts) > 4 else ""
        if u not in self.users:
            return None
        d = self.users[u]
        if path.endswith("/sub"):
            return {"success": True, "sub_url": f"https://p/sub/HASH/{u}"}
        if path.endswith("/revoke"):
            return {"success": True, "new_subscription_url": f"https://p/sub/NEW/{u}"}
        if path.endswith("/reset_traffic"):
            d["used"] = 0
            return {"success": True}
        if path.endswith("/all_ovpn_links"):
            return {"success": True,
                    "wireguard_config": {"enabled": True, "servers": [
                        {"name": "Main", "filename": f"{u}-Main.conf", "full_config_text": "[Interface]\nA"}]},
                    "wireguard_instances": [{"instance": "wg2", "enabled": True, "servers": [
                        {"name": "DE", "filename": f"{u}-DE-wg2.conf", "full_config_text": "[Interface]\nB"}]}],
                    "singbox_inbounds": [{"tag": "reality-main", "servers": [
                        {"share_link": f"vless://x@h:1#{u}"}]}]}
        if method == "GET":
            return {"success": True, "username": u, "is_active": d["active"],
                    "total_traffic_bytes": d["used"]}       # مثلِ اسپک: بدونِ حجم/انقضا
        if method == "PUT":
            for k, v in (json or {}).items():
                if k == "data_limit":
                    d["data_limit"] = v
                elif k == "expiry_date_str":
                    d["expiry"] = v
                elif k == "is_disabled":
                    d["active"] = not v
            return {"success": True}
        if method == "DELETE":
            del self.users[u]
            return {"success": True, "message": "User deleted."}
        return None


@pytest.fixture
def cl(monkeypatch):
    c = EP.EylanPanelClient("https://panel.example.com:2053/api/v1", "-", "KEY123")
    fake = FakeEylan()
    monkeypatch.setattr(c, "_request_timed", fake)
    c.fake = fake
    return c


def test_ctor_and_auth():
    c = EP.EylanPanelClient("https://p.example.com:2053/api/", "-", " KEY ")
    assert c.base_url == "https://p.example.com:2053" and c.api_key == "KEY"
    assert EP._safe_username("ali.reza@x") == "ali_reza_x"


@pytest.mark.asyncio
async def test_create_get_usage_and_protocols(cl):
    acc = await cl.create_account("buyer_1", 30, 30, limit_ip=2)
    assert acc["email"] == "buyer_1" and acc["sub_link"].endswith("/buyer_1")
    body = cl.fake.users["buyer_1"]["body"]
    assert body["data_limit"] == 30 and body["data_limit_unit"] == "GB" and body["expiry_days"] == 30
    assert body["enable_openvpn"] and body["wg_instances"] == ["wg2"] and body["enable_wg1"] is True
    assert body["singbox_inbounds"] == ["reality-main"] and body["max_clients"] == 2
    u = await cl.get_usage("buyer_1")             # حجم/انقضا از list_all تکمیل شد
    assert u["total"] == 30 * GB and u["expiryTime"] > time.time() * 1000 and u["enable"]


@pytest.mark.asyncio
async def test_duplicate_name_uses_panel_final_name(cl):
    await cl.create_account("same", 5, 10)
    acc = await cl.create_account("same", 5, 10)
    assert acc["email"] == "same4821"


@pytest.mark.asyncio
async def test_explicit_protocols_mb_and_on_hold(cl):
    await cl.create_account("tst1", 0, 2, mb=500, inbound_ids=["openvpn", "wg1", "node:1", "-"], on_hold=True)
    b = cl.fake.users["tst1"]["body"]
    assert b["data_limit"] == 500 and b["data_limit_unit"] == "MB"
    assert b["activation_type"] == "flexible_days" and b["pending_activation_days"] == 2
    assert b["enable_wg1"] and b["nodes"] == [1] and not b["enable_singbox"]


@pytest.mark.asyncio
async def test_renew_traffic_toggle_reset_delete(cl):
    await cl.create_account("ren1", 10, 10)
    await cl.add_traffic("ren1", 5)
    assert cl.fake.users["ren1"]["data_limit"] == 15
    before = cl.fake.users["ren1"]["expiry"]
    await cl.renew("ren1", 30, reset_usage=True)
    assert cl.fake.users["ren1"]["expiry"] > before
    assert any(p.endswith("/reset_traffic") for _m, p, _j in cl.fake.calls)
    await cl.set_enabled("ren1", False)
    assert cl.fake.users["ren1"]["active"] is False
    assert (await cl.reset_sub("ren1")).endswith("/NEW/ren1")
    assert await cl.rename_account("ren1", "r2") is False
    assert await cl.delete_account("ren1") and "ren1" not in cl.fake.users


@pytest.mark.asyncio
async def test_links_wireguard_list_and_test(cl):
    await cl.create_account("wgu1", 1, 1)
    assert await cl.get_client_links("wgu1") == ["vless://x@h:1#wgu1"]
    eps = await cl.get_wireguard_endpoints("wgu1")
    assert [e["file"] for e in eps] == ["wgu1-Main.conf", "wgu1-DE-wg2.conf"]
    assert await cl.fetch_wireguard_config_text("wgu1", 2) == "[Interface]\nB"
    lst = await cl.list_clients()
    assert lst[0]["email"] == "wgu1" and lst[0]["totalGB"] == GB
    t = await cl.test_connection()
    assert t["ok"] and t["nodes"] == 1


def test_registered_everywhere():
    import plans
    import tenant
    from tenant.bots.vpn import vpn_admin
    from tenant.shared.panel_clients.vpn_panel_factory import client_class_for, _parse_inbounds
    assert client_class_for("eylan#2") is EP.EylanPanelClient
    assert _parse_inbounds("eylan", "openvpn,reality-main") == ["openvpn", "reality-main"]
    assert "eylan" in vpn_admin.PANEL_LABELS and vpn_admin._PANEL_EXTRA_FEATURE_GATE["eylan"]
    assert plans.has_feature("pro", plans.Feature.EYLAN_PANEL)
    assert not plans.has_feature("growth", plans.Feature.EYLAN_PANEL)
    assert "eylan" in vpn_admin._panel_labels_for_plan("pro")
    assert "eylan" not in vpn_admin._panel_labels_for_plan("growth")
    src = open(tenant.__file__, encoding="utf-8").read()
    assert src.count('"buy_vpn_eylan"') >= 6


@pytest.mark.asyncio
async def test_default_protocols_include_all_online_nodes(cl):
    await cl.create_account("nodeusr", 5, 5)                    # بدونِ انتخابِ دستی → همهٔ نودها
    assert cl.fake.users["nodeusr"]["body"]["nodes"] == [1]
    await cl.create_account("nonode", 5, 5, inbound_ids=["openvpn"])   # انتخابِ صریح رعایت می‌شود
    assert "nodes" not in cl.fake.users["nonode"]["body"]


@pytest.mark.asyncio
async def test_second_eylan_panel_wizard_uses_api_key_prompts():
    """«eylan#2» (پنلِ دوم) باید همان پرسش‌هایِ «کلید API» را بگیرد، نه «نام کاربری»."""
    from types import SimpleNamespace
    from tenant.bots.vpn import vpn_admin as VA
    assert VA._bk("eylan#2") == "eylan"
    src = open(VA.__file__, encoding="utf-8").read()
    assert 'data.get("panel_key") == "eylan"' not in src


@pytest.mark.asyncio
async def test_wizard_steps_for_second_eylan_panel(monkeypatch):
    from types import SimpleNamespace
    from tenant.bots.vpn import vpn_admin as VA

    async def owner_ok(msg, bot):
        return SimpleNamespace(id=1, plan="pro")
    monkeypatch.setattr(VA, "_owner_msg", owner_ok)

    class St:
        def __init__(self):
            self.d = {"panel_key": "eylan#2"}
            self.state = None

        async def update_data(self, **kw):
            self.d.update(kw)

        async def get_data(self):
            return dict(self.d)

        async def set_state(self, s):
            self.state = s

    class Msg:
        def __init__(self, text):
            self.text = text
            self.out = []

        async def answer(self, text, **k):
            self.out.append(text)
    st = St()
    m = Msg("https://service.example.space:5000")
    await VA.vpn_setup_base(m, None, st)
    assert "«-»" in m.out[0] and "نام کاربری به پنل" not in m.out[0]      # مرحلهٔ ۲: «-» بفرست
    m2 = Msg("-")
    await VA.vpn_setup_user(m2, None, st)
    assert "کلیدِ API" in m2.out[0]                                        # مرحلهٔ ۳: کلیدِ API


# ── نامِ کوتاه: «mctWG528196579jrj-wg-TR» در اپِ وایرگارد خطا می‌داد (نامِ تونل ≤ ۱۵) ──
LONG = "mctWG528196579jrj"


@pytest.mark.asyncio
async def test_long_requested_name_becomes_short_and_consistent(cl):
    acc = await cl.create_account(LONG, 10, 10)
    name = acc["email"]
    assert len(name) <= 8 and name in cl.fake.users and LONG not in cl.fake.users
    # عملیاتِ بعدی با همان نامِ ذخیره‌شده در دیتابیس (بلند) همان کاربرِ کوتاه را پیدا می‌کنند
    await cl.add_traffic(LONG, 5)
    await cl.renew(LONG, 30)
    await cl.set_enabled(LONG, False)
    assert cl.fake.users[name]["data_limit"] == 15 and cl.fake.users[name]["active"] is False
    assert (await cl.get_usage(LONG))["total"] == 15 * GB
    assert await cl.delete_account(LONG) and name not in cl.fake.users


@pytest.mark.asyncio
async def test_legacy_long_account_still_found(cl):
    cl.fake.users[LONG] = {"data_limit": 7, "used": 0, "active": True, "expiry": "", "body": {}}
    await cl.add_traffic(LONG, 3)                  # حسابِ قدیمی (قبل از کوتاه‌سازی) → همان نامِ بلند
    assert cl.fake.users[LONG]["data_limit"] == 10


def test_wg_file_names_fit_tunnel_limit_and_are_unique():
    used = set()
    names = [EP._wg_file_name(f, LONG, used) for f in
             (f"{LONG}-DE.conf", f"{LONG}-wg-TR.conf", f"{LONG}-wg-TR.conf")]
    assert all(n.endswith(".conf") and len(n) - 5 <= 15 for n in names), names
    assert len(set(n.lower() for n in names)) == 3
    short = EP._wg_file_name("u1a2b3c4-wg-TR.conf", "u1a2b3c4", set())
    assert short == "u1a2b3c4-wg-TR.conf"            # کوتاه است؛ دست نمی‌خورد
