import asyncio
from types import SimpleNamespace
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
import tenant
from tenant.bots.virtual_service import smm


class Target:
    def __init__(self): self.sent = []
    async def answer(self, text, **kw): self.sent.append((text, kw))


def _run(monkeypatch, scb):
    async def settings(_b): return (4666, 1)
    async def cancel(_bot): return scb
    monkeypatch.setattr(smm, "_srx_settings", settings)
    monkeypatch.setattr(tenant, "smart_cancel_kb", cancel)
    t = Target()
    asyncio.run(smm.srx_show_shop(SimpleNamespace(), t, 1, None))
    return t.sent


def test_inline_mode_single_message_with_cancel(monkeypatch):
    cancel = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="fsm:cancel")]])
    sent = _run(monkeypatch, cancel)
    assert len(sent) == 1
    cbs = [b.callback_data for r in sent[0][1]["reply_markup"].inline_keyboard for b in r]
    assert cbs == ["srx:calc", "fsm:cancel"]


def test_reply_mode_keeps_second_message(monkeypatch):
    sent = _run(monkeypatch, ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True))
    assert len(sent) == 2
