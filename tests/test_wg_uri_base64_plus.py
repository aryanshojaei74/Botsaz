"""PATCH_WG_URI_BASE64_PLUS_V1 (Whale): «+» در کلیدهایِ بیس‌۶۴ نباید به فاصله تبدیل شود و «/» خام netloc را نبُرد."""
from tenant.bots.vpn import vpn_user as V


def _conf(uri):
    out = V._parse_wireguard_uri(uri)
    assert out
    return dict(line.split(" = ", 1) for line in out.splitlines() if " = " in line and not line.startswith("#"))


def test_raw_plus_slash_equals_in_keys_are_preserved():
    c = _conf("wireguard://PRIV+ke/y=@tr.example.com:51820?address=10.0.0.2/32&publickey=abc+def/ghi==&presharedkey=ps+1/2=&mtu=1280#Turkey")
    assert c["PrivateKey"] == "PRIV+ke/y=" and c["PublicKey"] == "abc+def/ghi==" and c["PresharedKey"] == "ps+1/2="
    assert c["Endpoint"] == "tr.example.com:51820" and c["MTU"] == "1280"


def test_percent_encoded_keys_still_decode():
    c = _conf("wireguard://PRIV%2Bke%2Fy%3D@h.example.com:51820/?address=10.0.0.2%2F32&publickey=abc%2Bdef%3D#T")
    assert c["PrivateKey"] == "PRIV+ke/y=" and c["PublicKey"] == "abc+def=" and c["Address"] == "10.0.0.2/32"


def test_text_fields_and_ipv6_endpoint_and_aliases():
    c = _conf("wireguard://K@[2001:db8::1]:51820?address=10.0.0.2/32&publicKey=P+Q=&allowedIPs=0.0.0.0/0,::/0"
              "&dns=1.1.1.1,8.8.8.8&PersistentKeepalive=25")
    assert c["Endpoint"] == "[2001:db8::1]:51820" and c["PublicKey"] == "P+Q="
    assert c["AllowedIPs"] == "0.0.0.0/0, ::/0" and c["DNS"] == "1.1.1.1, 8.8.8.8" and c["PersistentKeepalive"] == "25"


def test_invalid_uris_return_none_and_name_is_kept():
    assert V._parse_wireguard_uri("vless://x@h:1") is None
    assert V._parse_wireguard_uri("wireguard://K@h:51820?address=10.0.0.2/32") is None     # بدونِ publickey
    assert V._parse_wireguard_uri("wireguard://@h:51820?address=1.1.1.1/32&publickey=P") is None
    assert V._parse_wireguard_uri("wireguard://K@h:51820?address=1.1.1.1/32&publickey=P#Turkey%201").startswith("# Name = Turkey 1\n")
