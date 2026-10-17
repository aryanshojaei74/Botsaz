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


@pytest.mark.asyncio
async def test_with_home_row_adds_once_in_reply_mode_only(monkeypatch):
    import tenant
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from tenant.bots.vpn import vpn_user

    async def gs(bid, key, default=""):
        return mode["v"]
    mode = {"v": "reply"}
    monkeypatch.setattr(tenant, "get_setting", gs)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]])
    out = await vpn_user._with_home_row(kb, 1)
    assert out.inline_keyboard[-1][0].callback_data == "mbtn:home"
    assert len((await vpn_user._with_home_row(out, 1)).inline_keyboard) == 2
    mode["v"] = "inline"
    assert await vpn_user._with_home_row(kb, 1) is kb
