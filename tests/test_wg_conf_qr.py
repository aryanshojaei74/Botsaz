import pytest
from tenant.bots.vpn import vpn_user as V

CONF = ("# Name = X\n[Interface]\nPrivateKey = K\nAddress = 10.0.0.2/32\nDNS = 1.1.1.1\nMTU = 1300\n\n"
        "[Peer]\nPublicKey = P\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = 1.2.3.4:5\nPersistentKeepalive = 11")


class FakeBot:
    def __init__(self): self.photos = []
    async def send_photo(self, chat_id, photo, caption=None, **k): self.photos.append((chat_id, photo, caption))


@pytest.mark.asyncio
async def test_qr_sent_for_short_conf_only():
    b = FakeBot()
    assert await V._send_wg_qr(b, 5, CONF, "X") is True and len(b.photos) == 1
    assert "WireGuard" in b.photos[0][2]
    assert await V._send_wg_qr(b, 5, CONF + "\n# " + "a" * 2000, "X") is False
    assert await V._send_wg_qr(b, 5, "vless://abc", "X") is False
    assert len(b.photos) == 1


@pytest.mark.asyncio
async def test_qr_decodes_to_conf():
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    b = FakeBot()
    await V._send_wg_qr(b, 5, CONF, "X")
    data = b.photos[0][1].data
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    val, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    assert val.strip() == CONF.strip()


def test_uri_comment_line_added():
    uri = "wireguard://K@1.2.3.4:51820/?publickey=P&address=10.0.0.2/32#N"
    assert V._parse_wireguard_uri(uri).endswith("\n\n# URI: " + uri)
