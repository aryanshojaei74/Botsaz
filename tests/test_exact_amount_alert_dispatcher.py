"""رگرسیونِ باگِ واقعی: میان‌افزارِ هشدارِ مبلغِ دقیق در Dispatcherِ واقعیِ aiogram باید cb.answer() را به پاپ‌آپ تبدیل کند
(تستِ واحد با رویدادِ جعلی این را نمی‌گرفت)."""
import datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import tenant
from tenant.gateways import exact_amount_alert as ea

pytestmark = pytest.mark.asyncio


class _Sess(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *a, **k):
        yield b""


async def _feed(monkeypatch, data, setting="1"):
    async def gs(bid, key, default=""):
        return setting

    async def gbr(bot):
        return SimpleNamespace(id=1)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    sess = _Sess()
    bot = Bot("123:ABC", session=sess)
    dp, root, child = Dispatcher(), Router(), Router()
    root.callback_query.outer_middleware(ea.exact_amount_alert_mw)

    @child.callback_query(F.data.startswith("w:"))
    async def h(cb: CallbackQuery):
        await cb.answer()
    root.include_router(child)
    dp.include_router(root)
    u = User(id=5, is_bot=False, first_name="a")
    msg = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=5, type="private"), from_user=u, text="x")
    cb = CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=msg)
    await dp.feed_update(bot, Update(update_id=1, callback_query=cb))
    return [(m.text, m.show_alert) for m in sess.calls]


async def test_real_dispatcher_turns_answer_into_popup(monkeypatch):
    assert await _feed(monkeypatch, "w:uniquepay") == [(ea.ALERT_TEXT, True)]
    assert await _feed(monkeypatch, "w:apay:vpn:1:2") == [(ea.ALERT_TEXT, True)]


async def test_real_dispatcher_other_gateways_and_off_switch(monkeypatch):
    assert await _feed(monkeypatch, "w:charge") == [(None, None)]
    assert await _feed(monkeypatch, "w:apay", setting="0") == [(None, None)]
