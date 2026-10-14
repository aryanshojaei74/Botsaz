"""PATCH_WG_SPLIT_SUB_BLOB_V1 — ساب با چند [Interface] باید چند فایلِ دقیق بدهد (نه بازسازی از URI)."""
from tenant.shared.panel_clients import sub_fetch as SF
from tenant.bots.vpn import vpn_user as VU

CONF = "# Name: {n}\n[Interface]\nPrivateKey = k\nAddress = 10.0.0.{i}/32\nMTU = 1420\n\n[Peer]\nPublicKey = p\nEndpoint = h{i}.x.ir:{i}\n"


def _blob(n=4):
    return "\n".join(CONF.format(n=f"L{i}", i=i) for i in range(1, n + 1))


def test_multi_interface_body_is_split_and_keeps_names():
    out = SF.parse_sub_body(_blob())
    assert len(out) == 4
    assert out[0].startswith("# Name: L1") and out[3].startswith("# Name: L4")
    assert all(o.count("[Interface]") == 1 and "[Peer]" in o for o in out)
    assert "Endpoint = h2.x.ir:2" in out[1] and "Endpoint = h2.x.ir:2" not in out[2]


def test_single_interface_unchanged():
    one = CONF.format(n="A", i=1)
    assert SF.parse_sub_body(one) == [one.strip()]


def test_extract_accepts_name_comment_before_interface():
    out = VU._extract_wireguard_configs(SF.parse_sub_body(_blob(2)))
    assert len(out) == 2 and VU._wg_conf_name(out[1]) == "L2"


def test_split_ignores_crlf_and_junk_prefix():
    out = SF.split_wg_blob(_blob(2).replace("\n", "\r\n"))
    assert len(out) == 2 and all("\r" not in o for o in out)


def test_rebuilt_uri_matches_sub_file_format():
    """URIِ wireguard:// باید همان فایلی را بدهد که خودِ ساب می‌دهد (فاصله بعد از ویرگول، «# Name = »)."""
    uri = ("wireguard://PRIV@wg.example.ir:9191/?publickey=PUB&address=172.1.4.40/32&mtu=1280"
           "&dns=8.8.8.8,1.1.1.1&allowedips=0.0.0.0/0,::/0&keepalive=25&presharedkey=PSK#Turkey")
    out = VU._parse_wireguard_uri(uri)
    assert out == ("# Name = Turkey\n[Interface]\nPrivateKey = PRIV\nAddress = 172.1.4.40/32\n"
                   "DNS = 8.8.8.8, 1.1.1.1\nMTU = 1280\n\n[Peer]\nPublicKey = PUB\nPresharedKey = PSK\n"
                   "AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = wg.example.ir:9191\nPersistentKeepalive = 25")
    assert VU._wg_conf_name(out) == "Turkey"
