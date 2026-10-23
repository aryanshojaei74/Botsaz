"""PATCH_BUTTON_CHANNEL_LINK_V1 — عملکردِ «لینکِ مستقیم (کانال/گروه/سایت)» برایِ دکمه‌هایِ منو."""
import json
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


def test_normalize_link_url_accepts_telegram_forms_and_rejects_junk():
    n = tenant.normalize_link_url
    assert n("@my_channel") == "https://t.me/my_channel"
    assert n("t.me/my_channel") == "https://t.me/my_channel"
    assert n("telegram.me/my_channel") == "https://telegram.me/my_channel"
    assert n("https://t.me/+AbCdEfGh12") == "https://t.me/+AbCdEfGh12"
    assert n("http://example.com/x") == "https://example.com/x"
    assert n("  https://t.me/chan  ") == "https://t.me/chan"
    for bad in ("", "کانال من", "@ab", "ftp://x.com", "https://t.me/ca nal", "https://", "javascript:alert(1)"):
        assert n(bad) is None, bad


def _btn(action="channel_link", url="https://t.me/chan", label="📢 کانال"):
    return NS(id=7, label=label, action=action, config=json.dumps({"link_url": url}), icon_emoji_id=None)


def test_inline_menu_button_is_a_direct_url_button_and_reply_button_is_plain_text():
    ib = tenant._ibtn_for(_btn(), None)
    assert ib.url == "https://t.me/chan" and ib.callback_data is None
    kb = tenant._kbtn_for(_btn(), None)
    assert kb.text == "📢 کانال" and kb.web_app is None
    # لینکِ خراب ⇒ دکمهٔ معمولیِ callback (نه URLِ نامعتبر که /start را بشکند)
    bad = tenant._ibtn_for(_btn(url="not a url"), None)
    assert bad.url is None and bad.callback_data == "mbtn:7"
    # دکمهٔ غیرِ لینک دست‌نخورده
    assert tenant._ibtn_for(NS(id=3, label="x", action=None, config=None, icon_emoji_id=None), None).callback_data == "mbtn:3"


def test_action_label_text_and_keyboard_for_channel_link():
    mb = _btn()
    assert "لینکِ مستقیم" in tenant._thb_action_label(mb)
    cbs = [b.callback_data for r in tenant._thb_kb_action(mb, "virtual_service", "pro").inline_keyboard
           for b in r if b.callback_data]
    assert f"thb:setlink:{mb.id}" in cbs and f"thb:unsetlink:{mb.id}" in cbs
    # در دستهٔ «ابزارها» هم گزینهٔ تبدیل هست
    plain = NS(id=8, label="x", action=None, config=None, parent_id=None, icon_emoji_id=None)
    tool = [b.callback_data for r in tenant._thb_kb_action_pick(plain, "virtual_service", "pro", "tool").inline_keyboard
            for b in r if b.callback_data]
    assert "thb:setlink:8" in tool


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.MainButton(id=5, bot_id=1, label="📢 کانال ما", row=0, col=0))
        await s.commit()
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="virtual_service")

    async def gbr(bot):
        return row

    async def ioa(bot_id, uid, owner):
        return uid == owner
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "is_owner_or_admin", ioa)

    async def dis(bot_id):
        return set()

    async def mk_kb(*a, **k):
        return None
    monkeypatch.setattr(tenant, "_disabled_btn_ids", dis)
    monkeypatch.setattr(tenant, "main_kb_or_remove", mk_kb)
    yield mk
    await eng.dispose()


@pytest.mark.asyncio
async def test_owner_sets_and_unsets_channel_link(env):
    mk = env
    st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=9, user_id=9))
    answers = []

    async def answer(t, **k):
        answers.append(t)
    cbans = []

    async def cb_answer(t="", **k):
        cbans.append(t)
    cb = NS(data="thb:setlink:5", from_user=NS(id=9), message=NS(answer=answer, edit_text=answer), answer=cb_answer)
    await tenant.thb_set_link(cb, NS(), st)
    assert await st.get_state() == tenant.HomeBtnFSM.set_link_url.state

    bad = NS(text="کانال من", from_user=NS(id=9), answer=answer)
    await tenant.thb_set_link_url_save(bad, NS(), st)
    assert "نامعتبر" in answers[-1] and await st.get_state() == tenant.HomeBtnFSM.set_link_url.state

    ok = NS(text="@my_channel", from_user=NS(id=9), answer=answer)
    await tenant.thb_set_link_url_save(ok, NS(), st)
    async with mk() as s:
        mb = await s.get(models.MainButton, 5)
    assert mb.action == "channel_link" and json.loads(mb.config)["link_url"] == "https://t.me/my_channel"
    assert await st.get_state() is None

    cb2 = NS(data="thb:unsetlink:5", from_user=NS(id=9), message=NS(answer=answer, edit_text=answer), answer=cb_answer)
    await tenant.thb_unset_link(cb2, NS())
    async with mk() as s:
        mb = await s.get(models.MainButton, 5)
    assert mb.action is None


@pytest.mark.asyncio
async def test_non_owner_cannot_set_link(env):
    mk = env
    st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=5, user_id=5))
    await st.set_state(tenant.HomeBtnFSM.set_link_url)
    await st.update_data(link_mb_id=5)

    async def answer(t, **k):
        return None
    await tenant.thb_set_link_url_save(NS(text="@evil_chan", from_user=NS(id=555), answer=answer), NS(), st)
    async with mk() as s:
        assert (await s.get(models.MainButton, 5)).action is None
