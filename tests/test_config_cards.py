"""کانفیگ‌های تکی: هر کارت (QR + متنِ کانفیگ در کپشن) جدا فرستاده می‌شود، نه آلبوم (که کپشن‌ها را پنهان می‌کند)."""
import pytest

from tenant.bots.vpn import vpn_user


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, chat_id, photo, caption=None, **k):
        self.calls.append(("photo", caption))

    async def send_message(self, chat_id, text, **k):
        self.calls.append(("msg", text))

    async def send_media_group(self, *a, **k):
        self.calls.append(("album", None))


@pytest.mark.asyncio
async def test_each_card_is_sent_separately_in_order_with_its_text(monkeypatch):
    async def qr(bot_id, link):
        if link == "bad":
            raise RuntimeError("qr failed")
        return b"PNG" + link.encode()
    monkeypatch.setattr(vpn_user, "_qr_png", qr)
    cards = [(i, f"vless://{i}", f"config {i}") for i in range(1, 13)]
    cards[4] = (5, "bad", "config 5")          # QR ناموفق ⇒ فقط متن
    cards[6] = (7, "vless://7", "x" * 1100)    # کپشنِ خیلی بلند ⇒ فقط متن
    bot = FakeBot()
    await vpn_user._send_config_cards(bot, 1, 99, cards)
    assert not any(k == "album" for k, _ in bot.calls)
    assert [t for _, t in bot.calls][:4] == ["config 1", "config 2", "config 3", "config 4"]   # به‌ترتیب
    assert len(bot.calls) == 12
    assert bot.calls[4] == ("msg", "config 5") and bot.calls[6][0] == "msg"
    assert sum(1 for k, _ in bot.calls if k == "photo") == 10
