import pytest
pytestmark = pytest.mark.asyncio
import asyncio
from types import SimpleNamespace
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage


def _exc(msg):
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=msg)


async def test_chat_gone_swallowed():
    from tenant.shared import chat_gone as m
    ev = SimpleNamespace(exception=_exc("Bad Request: chat not found"), update=SimpleNamespace(callback_query=None))
    assert await m.swallow_chat_gone(ev) is True


async def test_other_errors_pass():
    from tenant.shared import chat_gone as m
    ev = SimpleNamespace(exception=_exc("Bad Request: wrong file id"), update=SimpleNamespace(callback_query=None))
    assert await m.swallow_chat_gone(ev) is False
    ev = SimpleNamespace(exception=ValueError("chat not found"), update=None)
    assert await m.swallow_chat_gone(ev) is False
