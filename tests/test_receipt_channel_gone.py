"""PATCH_RECEIPT_CHANNEL_GONE_V1 — کانال تایید رسید پیدا نشد: هشدار یک‌خطی، خبر روزانه به مالک، رسید به پیوی ادمین‌ها."""
import pytest

import tenant as T


class _Row:
    id, owner_id, username = 7, 70, "shopbot"


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, chat_id, *a, **k):
        raise Exception("Telegram server says - Bad Request: chat not found")

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_gone_channel_warns_owner_once_and_falls_back(monkeypatch):
    async def gs(bid, k, d=""):
        return {"receipt_channel_id": "-100123", "receipt_channel_enabled": "1"}.get(k, d)
    admins = []

    async def na(bot, row, text, kb, photo):
        admins.append(text)
    monkeypatch.setattr(T, "get_setting", gs)
    monkeypatch.setattr(T, "_notify_admins", na)
    T._RECEIPT_CH_WARNED.clear()
    bot = _Bot()
    await T._notify_receipt_admins(bot, _Row(), "رسید", None, "file1")
    await T._notify_receipt_admins(bot, _Row(), "رسید۲", None, "file2")
    assert admins == ["رسید", "رسید۲"]                                   # هیچ رسیدی گم نمی‌شود
    owner_msgs = [t for c, t in bot.sent if c == 70]
    assert len(owner_msgs) == 1 and "کانال تایید رسید" in owner_msgs[0]   # فقط یک‌بار در روز


def test_marker_detection():
    assert T._receipt_channel_gone(Exception("Bad Request: chat not found"))
    assert T._receipt_channel_gone(Exception("Forbidden: bot was kicked from the channel chat"))
    assert not T._receipt_channel_gone(Exception("Bad Request: wrong file identifier"))
