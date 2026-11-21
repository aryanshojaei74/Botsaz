"""PATCH_MANUAL_ORDER_CHANNEL_V1 — سفارشِ دستی به کانالِ سفارشاتِ دستی برود (وگرنه DM)."""
import asyncio
from types import SimpleNamespace

import tenant


class _FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat, text, **kw):
        if self.fail:
            raise RuntimeError("no rights")
        self.sent.append(("msg", chat, text))

    async def send_photo(self, chat, photo, **kw):
        if self.fail:
            raise RuntimeError("no rights")
        self.sent.append(("photo", chat, photo))


def _run(settings, bot, photo=None):
    dm = []

    async def fake_get_setting(bot_id, key, default=""):
        return settings.get(key, default)

    async def fake_notify_admins(b, row, text, kb=None, photo_file_id=None):
        dm.append(text)

    orig = tenant.get_setting, tenant._notify_admins
    tenant.get_setting, tenant._notify_admins = fake_get_setting, fake_notify_admins
    try:
        row = SimpleNamespace(id=7, owner_id=1)
        asyncio.run(tenant._notify_manual_order(bot, row, "سفارش", None, photo_file_id=photo))
    finally:
        tenant.get_setting, tenant._notify_admins = orig
    return dm


def test_goes_to_channel_when_connected():
    bot = _FakeBot()
    dm = _run({"smm_manual_channel_id": "-1001", "smm_manual_channel_enabled": "1"}, bot)
    assert bot.sent == [("msg", "-1001", "سفارش")] and dm == []


def test_photo_receipt_goes_to_channel():
    bot = _FakeBot()
    _run({"smm_manual_channel_id": "-1001", "smm_manual_channel_enabled": "1"}, bot, photo="F1")
    assert bot.sent == [("photo", "-1001", "F1")]


def test_falls_back_to_dm_when_not_connected_or_disabled():
    for st in ({}, {"smm_manual_channel_id": "-1001", "smm_manual_channel_enabled": "0"}):
        bot = _FakeBot()
        dm = _run(st, bot)
        assert bot.sent == [] and dm == ["سفارش"]


def test_falls_back_to_dm_when_channel_send_fails():
    bot = _FakeBot(fail=True)
    dm = _run({"smm_manual_channel_id": "-1001", "smm_manual_channel_enabled": "1"}, bot)
    assert dm == ["سفارش"]
