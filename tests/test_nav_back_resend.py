"""«بازگشت به منوی قبل»: اگر ویرایشِ پیام ممکن نبود، صفحهٔ قبلی پیامِ تازه می‌شود (نه پرتاب به منوی اصلی)."""
from __future__ import annotations

import datetime

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from tests.test_nav_rules_staff import BACK, HOME, USER, env, kb  # noqa: F401


def _cb(bot, mid, data):
    msg = Message(message_id=mid, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x").as_(bot)
    return CallbackQuery(id="1", from_user=User(id=USER, is_bot=False, first_name="A"), chat_instance="c",
                         message=msg, data=data).as_(bot)


@pytest.mark.asyncio
async def test_back_edits_previous_page_in_place(env):
    tenant, bot, cap, bid, _ = env
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        for t in ("cats", "list", "product"):
            await bot.edit_message_text(t, chat_id=USER, message_id=m.message_id, reply_markup=kb([(t, t)], BACK))
        await tenant.nav_back(_cb(bot, m.message_id, "nav:back"), bot, state)
        assert cap.calls[-2].text == "list"          # (آخرین call = answerCallbackQuery)
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_back_resends_previous_page_when_edit_fails(env):
    tenant, bot, cap, bid, _ = env
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        for t in ("cats", "list", "product"):
            await bot.edit_message_text(t, chat_id=USER, message_id=m.message_id, reply_markup=kb([(t, t)], BACK))
        cap.fail_edit = True
        n = len(cap.calls)
        await tenant.nav_back(_cb(bot, m.message_id, "nav:back"), bot, state)
        sent = [c for c in cap.calls[n:] if type(c).__name__ == "SendMessage"]
        assert sent and sent[-1].text == "list"      # صفحهٔ «لیست» دوباره فرستاده شد، نه منوی اصلی
        assert not any(getattr(c, "text", None) == "home" for c in sent)
    finally:
        tenant._CUR_UID.reset(tok)
