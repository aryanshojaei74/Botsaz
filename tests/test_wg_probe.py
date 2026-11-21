import wg_probe as W

CONF = ("[Interface]\nPrivateKey = ABCDEFGH1234567890=\nAddress = 10.0.0.2/32\nDNS = 1.1.1.1\nMTU = 1280\n\n"
        "[Peer]\nPublicKey = PUBKEY==\nPresharedKey = psk123456789\nAllowedIPs = 0.0.0.0/0\nEndpoint = 1.2.3.4:51820\n")


def test_keys_are_masked_and_summary_has_no_secret():
    m = W.mask_keys(CONF)
    assert "1234567890" not in m and "psk123456789" not in m
    assert "Address = 10.0.0.2/32" in m and "PublicKey = PUBKEY==" in m   # بقیه دست‌نخورده
    s = W.summarize_conf(CONF)
    assert s["Endpoint"] == "1.2.3.4:51820" and s["has_PrivateKey"] and s["has_[Peer]"]
    assert "ABCDEFGH" not in str(s)


def test_incomplete_conf_is_flagged():
    s = W.summarize_conf("[Interface]\nAddress = 10.0.0.2/32\n")
    assert not s["has_PrivateKey"] and not s["has_[Peer]"] and s["Endpoint"] == "—"


def test_describe_wg_settings_never_leaks_private_key_and_flags_empty():
    d = W.describe_wg_settings({"wireguard": {"private_key": "SECRETKEY1234567890=", "public_key": "pub", "peer_ips": ["10.0.0.5/32"]}})
    assert "SECRETKEY" not in d and "SECR" in d and "10.0.0.5/32" in d
    assert "خالی" in W.describe_wg_settings({"wireguard": {"private_key": None, "peer_ips": []}})
    assert "ساخته نشده" in W.describe_wg_settings({"vless": {}})
    assert "نیست" in W.describe_wg_settings(None)


def test_describe_wg_uri_hides_keys_and_lists_param_names():
    uri = ("wireguard://SECRETPRIVATEKEY1234567890abcdefghijklmnop%3D@t1.example.ir:58531/?publickey=PUB%3D"
           "&address=10.0.0.2%2F32&mtu=1300&dns=1.1.1.1&allowedips=0.0.0.0%2F0%2C10.0.0.0%2F8#Turkey1")
    d = W.describe_wg_uri(uri)
    assert "SECRETPRIVATE" not in d and "PUB" not in d.replace("publickey", "")
    assert "t1.example.ir:58531" in d and "dns=1.1.1.1" in d and "allowedips: 2 ردیف" in d
    assert "'dns'" in d and "Turkey1" in d


def test_describe_nodes_and_usage():
    lines = W.describe_nodes({"nodes": [
        {"id": 1, "name": "t1wire", "status": "connected", "address": "1.2.3.4", "message": None},
        {"id": 2, "name": "mb1wire", "status": "error", "address": "5.6.7.8", "message": "connection refused"}]})
    assert lines[0].startswith("✅") and lines[1].startswith("❌") and "connection refused" in lines[1]
    assert W.describe_nodes([])[0].startswith("(هیچ")
    u = W.describe_usage({"up": 1024 * 1024, "down": 2 * 1024 * 1024, "last_online_ms": 1700000000000, "client_app": "WireGuard"})
    assert "3.0MB" in u and "2023" in u
    assert "هرگز" in W.describe_usage({"up": 0, "down": 0})
