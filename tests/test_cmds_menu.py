import datetime
import glob
import re
from types import SimpleNamespace as NS

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Chat, Message, User

import platform_cmds_menu as CM
import platform_admin_menu as AM


def test_menu_lists_every_command_and_fits_telegram_limits():
    txt, kb = CM._menu()
    for c in CM.COMMANDS:
        assert f"/{c[1]}" in txt
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "adm:cat:settings" in datas and all(len(d.encode()) <= 64 for d in datas)
    assert len(txt) < 4000


def test_every_listed_command_exists_in_code():
    src = "\n".join(open(f, encoding="utf8").read() for f in glob.glob("*.py") if not f.startswith("test_"))
    for c in CM.COMMANDS:
        assert re.search(r'Command\("%s"\)' % re.escape(c[1]), src), c[1]
        for _lbl, text in c[5]:
            assert text.split()[0] == "/" + c[1]


def test_detail_pages_and_admin_menu_entry():
    for i in range(len(CM.COMMANDS)):
        txt, kb = CM._detail(i)
        assert CM.COMMANDS[i][1] in txt and kb.inline_keyboard[-1][0].callback_data == "cmds:menu"
    items = [it for sec in AM.ADMIN_SECTIONS if sec["key"] == "settings" for it in sec["items"]]
    assert ("🧰 دستورهایِ مدیر (/)", "cmds:menu") == tuple(items[-1][:2])


@pytest.mark.asyncio
async def test_run_button_feeds_the_command_through_the_dispatcher(monkeypatch):
    monkeypatch.setattr(CM, "PLATFORM_ADMIN_ID", 5)
    bot = Bot("123456:" + "A" * 35)
    dp = Dispatcher()
    seen = []
    r = Router()

    @r.message(Command("addays"))
    async def h(m: Message):
        seen.append((m.text, m.from_user.id, m.chat.id))
    parent = Router()
    parent.include_router(CM.cmds_router)
    dp.include_router(parent)
    dp.include_router(r)
    answered = []

    async def ans(*a, **k):
        answered.append(a)
    chat, user = Chat(id=5, type="private"), User(id=5, is_bot=False, first_name="A")
    cb = NS(data="cmds:r:0:0", from_user=user, answer=ans,
            message=NS(message_id=10, chat=chat, answer=ans))
    assert CM.COMMANDS[0][1] == "addays"
    await CM.cmds_run(cb, bot)
    assert seen == [("/addays", 5, 5)]
    cb2 = NS(data="cmds:r:0:0", from_user=User(id=9, is_bot=False, first_name="X"), answer=ans, message=cb.message)
    seen.clear()
    await CM.cmds_run(cb2, bot)
    assert seen == []                                    # غیرِ مدیرِ اصلی ⇒ اجرا نمی‌شود
