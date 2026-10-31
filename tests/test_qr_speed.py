import asyncio
import io
import os

import numpy as np
from PIL import Image

from tenant.shared import qr_brand as Q

LINK = ("vless://" + "a1b2c3d4-" * 4 + "@host.example.com:443?security=reality&sni=x.com&fp=chrome&pbk="
        + "K" * 43 + "&sid=ab&type=tcp&flow=xtls-rprx-vision#name")


def _bg_bytes():
    rng = np.random.default_rng(3)
    x = np.linspace(0, 255, 1000)
    a = np.add.outer(x, x) / 2
    arr = np.stack([a, 255 - a, a * 0.5], -1) + rng.normal(0, 25, (1000, 1000, 3))
    b = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).save(b, "JPEG", quality=85)
    return b.getvalue()


def test_bg_render_is_light_and_qr_area_unchanged(monkeypatch):
    bg = _bg_bytes()
    monkeypatch.setenv("TG_QR_BG_PNG", "1")
    png = Q.render(LINK, bg)
    monkeypatch.setenv("TG_QR_BG_PNG", "0")
    jpg = Q.render(LINK, bg)
    assert jpg[:2] == b"\xff\xd8" and png[:4] == b"\x89PNG"
    assert len(jpg) < len(png) * 0.5
    # ناحیهٔ بارکد (کارتِ سفید وسط): بعد از دودویی‌کردن باید تقریباً یکسان باشد ⇒ اسکن‌پذیری حفظ شده
    a = np.asarray(Image.open(io.BytesIO(png)).convert("L"))[300:700, 300:700] < 128
    b = np.asarray(Image.open(io.BytesIO(jpg)).convert("L"))[300:700, 300:700] < 128
    assert (a == b).mean() > 0.995


def test_get_bg_reads_database_once_per_ttl(monkeypatch):
    calls = {"n": 0}

    class _Row:
        value = "QUJD"   # base64("ABC")

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def scalar(self, *_a, **_k):
            calls["n"] += 1
            return _Row()

    import config
    monkeypatch.setattr(config, "Session", lambda: _S())
    Q._BG_CACHE.clear()

    async def go():
        return await asyncio.gather(*[Q.get_bg(77) for _ in range(19)])

    res = asyncio.run(go())
    # هم‌زمان‌ها ممکن است چند بار بخوانند، ولی بعدش کش است: فراخوانیِ بعدی صفر خوانش دارد
    before = calls["n"]
    assert asyncio.run(Q.get_bg(77)) == b"ABC" and calls["n"] == before
    assert all(r == b"ABC" for r in res)


def test_config_cards_limit_concurrency_and_send_first_card_early(monkeypatch):
    import tenant.bots.vpn.vpn_user as V

    state = {"cur": 0, "max": 0, "finished": 0, "sent_when_finished": []}

    async def fake_qr(bot_id, link):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.15)
        state["cur"] -= 1
        state["finished"] += 1
        return b"PNGDATA"

    class Bot:
        async def send_photo(self, chat, file, caption=None):
            state["sent_when_finished"].append(state["finished"])

        async def send_message(self, chat, text):
            state["sent_when_finished"].append(state["finished"])

    monkeypatch.setattr(V, "_qr_png", fake_qr)
    cards = [(i, f"vless://x{i}", f"body{i}") for i in range(1, 9)]
    asyncio.run(V._send_config_cards(Bot(), 1, 5, cards))
    assert state["max"] <= 3
    assert len(state["sent_when_finished"]) == 8
    assert state["sent_when_finished"][0] < 8     # کارتِ اول قبل از ساختِ همهٔ QRها رفت
