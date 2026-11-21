from tenant.bots.vpn.vpn_user import _parse_wireguard_uri as P


def test_amnezia_params_kept():
    t = P("wireguard://PRIV+KEY=@1.2.3.4:51820?address=10.0.0.2/32&publickey=PUB+K=&jc=4&jmin=8&jmax=80&s1=15&h1=1")
    assert "Jc = 4" in t and "Jmin = 8" in t and "Jmax = 80" in t and "S1 = 15" in t and "H1 = 1" in t
    assert t.index("Jc = 4") < t.index("[Peer]")
    assert "PrivateKey = PRIV+KEY=" in t


def test_plain_wg_unchanged():
    t = P("wireguard://K@1.2.3.4:51820?address=10.0.0.2/32&publickey=PK")
    assert "Jc" not in t and "[Peer]" in t
