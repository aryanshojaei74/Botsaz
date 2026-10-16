"""گزارشِ ایلیا: کیبوردِ منویِ قبلی در صفحه‌هایِ مرحله‌ایِ خرید نماند."""
import datetime
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message, ReplyKeyboardRemove, User

import tenant
from tenant.bots.vpn import vpn_user as VU

pytestmark = pytest.mark.asyncio


def _msg(is_bot=False, text="(C) برنزی"):
    m = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=5, type="private"),
                from_user=User(id=5 if not is_bot else 9, is_bot=is_bot, first_name="a"), text=text)
    sent = []

    async def answer(t, **k):
        sent.append((t, k.get("reply_markup")))

        async def delete():
            sent.append("deleted")
        return SimpleNamespace(delete=delete)
    object.__setattr__(m, "answer", answer)
    return m, sent


async def _run(monkeypatch, msg, mode):
    async def gs(bid, key, default=""):
        return mode
    monkeypatch.setattr(tenant, "get_setting", gs)
    await VU._drop_reply_kb(msg, 1)


async def test_keyboard_mode_user_press_removes_then_deletes(monkeypatch):
    m, sent = _msg()
    await _run(monkeypatch, m, "reply")
    assert isinstance(sent[0][1], ReplyKeyboardRemove) and sent[1] == "deleted"


async def test_skipped_for_inline_mode_and_bot_messages(monkeypatch):
    m, sent = _msg()
    await _run(monkeypatch, m, "inline")
    assert sent == []
    m2, sent2 = _msg(is_bot=True)
    await _run(monkeypatch, m2, "reply")
    assert sent2 == []
