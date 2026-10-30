import asyncio
from types import SimpleNamespace as NS

import tenant
from tenant.shared import tx_channel as TX


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))


def _setup(monkeypatch, store):
    async def gs(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)


def _go(bot):
    buyer = NS(full_name="Ali", username="ali", telegram_id=12)
    asyncio.run(TX.tx_log_purchase(bot, 1, buyer=buyer, section="سرویس VPN", detail="۳۰ گیگ", amount=150000,
                                   tracking_code="T1", source="پنل A"))


def test_purchase_logged_only_when_owner_turned_it_on(monkeypatch):
    store = {"tx_log_channel": "-1001"}
    _setup(monkeypatch, store)
    b = Bot()
    _go(b)
    assert b.sent == []                                  # پیش‌فرض خاموش
    store["tx_log_purchases"] = "1"
    _go(b)
    chat, text = b.sent[0]
    assert chat == -1001 and "#خرید" in text and "سرویس VPN" in text and "150" in text and "T1" in text and "پنل A" in text
    assert "@ali" in text and "12" in text


def test_no_channel_means_no_post(monkeypatch):
    _setup(monkeypatch, {"tx_log_purchases": "1"})
    b = Bot()
    _go(b)
    assert b.sent == []
