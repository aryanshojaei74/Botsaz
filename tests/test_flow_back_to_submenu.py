"""ایلیا: «بازگشت به منو» در صفحه‌هایِ خریدِ منویِ کیبوردی باید به همان زیرمنو برگردد، نه صفحهٔ اصلی."""
import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_user

pytestmark = pytest.mark.asyncio


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


async def _home_row(monkeypatch, mode, last_menu):
    async def gs(bid, key, default=""):
        return mode
    monkeypatch.setattr(tenant, "get_setting", gs)
    tenant._LAST_MENU.clear()
    tok = tenant._CUR_UID.set(5)
    try:
        if last_menu:
            tenant._set_last_menu(1, 5, last_menu)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]])
        out = await vpn_user._with_home_row(kb, 1)
        again = await vpn_user._with_home_row(out, 1)
        return out, again
    finally:
        tenant._CUR_UID.reset(tok)
        tenant._LAST_MENU.clear()


async def test_reply_mode_from_submenu_returns_to_that_submenu_and_keeps_home(monkeypatch):
    out, again = await _home_row(monkeypatch, "reply", 77)
    assert _cbs(out)[-2:] == ["rsub:77", "mbtn:home"]
    assert out.inline_keyboard[-1][0].text == "⬅️ بازگشت به منو"
    assert _cbs(again) == _cbs(out)                              # تکرار نمی‌شود


async def test_without_submenu_it_is_the_old_single_home_button(monkeypatch):
    out, _ = await _home_row(monkeypatch, "reply", None)
    assert _cbs(out)[-1] == "mbtn:home" and not any(c.startswith("rsub:") for c in _cbs(out))


async def test_existing_home_button_is_replaced_by_the_pair_not_duplicated(monkeypatch):
    async def gs(bid, key, default=""):
        return "reply"
    monkeypatch.setattr(tenant, "get_setting", gs)
    tenant._LAST_MENU.clear()
    tok = tenant._CUR_UID.set(5)
    try:
        tenant._set_last_menu(1, 5, 9)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")],
                                                   [InlineKeyboardButton(text="🏠", callback_data="mbtn:home")]])
        out = await vpn_user._with_home_row(kb, 1)
    finally:
        tenant._CUR_UID.reset(tok)
        tenant._LAST_MENU.clear()
    assert _cbs(out).count("mbtn:home") == 1 and "rsub:9" in _cbs(out)


async def test_inline_back_row_uses_rsub_in_keyboard_mode_and_smopen_in_glass_mode(monkeypatch):
    for mode, expect in (("reply", "rsub:9"), ("inline", "smopen:9")):
        async def gs(bid, key, default="", _m=mode):
            return _m
        monkeypatch.setattr(tenant, "get_setting", gs)
        tenant._LAST_MENU.clear()
        tok = tenant._CUR_UID.set(5)
        try:
            tenant._set_last_menu(1, 5, 9)
            row = await tenant.inline_back_row(1)
        finally:
            tenant._CUR_UID.reset(tok)
            tenant._LAST_MENU.clear()
        assert row[0].callback_data == expect


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        sub = models.MainButton(bot_id=b.id, row=0, col=0, label="خرید", action="submenu")
        plain = models.MainButton(bot_id=b.id, row=1, col=0, label="سایر", action="free_trial")
        s.add_all([sub, plain])
        await s.commit()
        ids = (b.id, sub.id, plain.id)

    async def gbr(_b):
        return SimpleNamespace(id=ids[0], owner_id=1, plan="pro")

    async def no_disabled(_bid):
        return set()
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "_disabled_btn_ids", no_disabled)
    yield ids
    await eng.dispose()


def _cb(data):
    user = User(id=5, is_bot=False, first_name="a")
    msg = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=5, type="private"),
                  from_user=User(id=9, is_bot=True, first_name="bot"), text="صفحهٔ خرید")
    calls = []

    async def delete():
        calls.append("deleted")

    async def ans(*a, **k):
        calls.append("answered")
    object.__setattr__(msg, "delete", delete)
    cb = CallbackQuery(id="1", from_user=user, chat_instance="c", data=data, message=msg)
    object.__setattr__(cb, "answer", ans)
    return cb, calls


class _St:
    cleared = False

    async def clear(self):
        _St.cleared = True


async def test_rsub_handler_deletes_page_clears_state_and_renders_the_submenu(env, monkeypatch):
    bid, sub_id, _ = env
    rendered = []

    async def fake_render(msg, bot_id, btn, state):
        rendered.append((msg.from_user.id, bot_id, btn.id))
    monkeypatch.setattr(tenant, "_render_reply_submenu", fake_render)
    cb, calls = _cb(f"rsub:{sub_id}")
    _St.cleared = False
    bot = Bot(token="123456789:AAEabcdefghijklmnopqrstuvwxyz012345")
    await tenant.rsub_open(cb, bot, _St())
    assert rendered == [(5, bid, sub_id)]                         # با کاربرِ واقعی، نه خودِ ربات
    assert "deleted" in calls and _St.cleared


async def test_rsub_falls_back_to_home_when_target_is_not_a_submenu(env, monkeypatch):
    bid, _, plain_id = env
    went_home = []

    async def fake_tap(cb, bot, state):
        went_home.append(cb.data)
    monkeypatch.setattr(tenant, "mbtn_tap", fake_tap)
    cb, _ = _cb(f"rsub:{plain_id}")
    await tenant.rsub_open(cb, Bot(token="123456789:AAEabcdefghijklmnopqrstuvwxyz012345"), _St())
    assert went_home == ["mbtn:home"]
