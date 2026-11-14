"""محافظِ BUTTON_URL_INVALID در لایهٔ ارسال (PATCH_URL_BUTTON_GUARD_V1)."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (InlineKeyboardButton as IB, InlineKeyboardMarkup as IM, KeyboardButton as KB,
                           ReplyKeyboardMarkup as RM, WebAppInfo)

import tenant


def kb(*buttons_rows):
    return IM(inline_keyboard=[list(r) for r in buttons_rows])


def test_url_validator():
    ok = tenant._url_ok
    assert ok("https://example.com/x?y=1") and ok("https://t.me/support") and ok("tg://resolve?domain=x")
    assert not ok("example.com") and not ok("https://localhost") and not ok("https://a b.com")
    assert not ok("ftp://x.com") and not ok("") and not ok(None) and not ok("javascript:alert(1)")


def test_strip_inline_only_bad_then_all():
    good, bad, cbk = IB(text="ok", url="https://a.com/x"), IB(text="bad", url="a.com"), IB(text="cb", callback_data="x")
    m = kb([good, bad], [cbk])
    out = tenant._strip_url_buttons(m, aggressive=False)
    assert [b.text for r in out.inline_keyboard for b in r] == ["ok", "cb"]
    out2 = tenant._strip_url_buttons(m, aggressive=True)
    assert [b.text for r in out2.inline_keyboard for b in r] == ["cb"]
    assert tenant._strip_url_buttons(kb([bad]), aggressive=False) is None            # خالی ⇒ بدونِ کیبورد


def test_strip_webapp_and_reply_keyboards():
    wa_bad = IB(text="app", web_app=WebAppInfo(url="http://insecure"))
    wa_ok = IB(text="app2", web_app=WebAppInfo(url="https://ok.example.com/app"))
    out = tenant._strip_url_buttons(kb([wa_bad, wa_ok]), aggressive=False)
    assert [b.text for r in out.inline_keyboard for b in r] == ["app2"]
    rk = RM(keyboard=[[KB(text="a"), KB(text="app", web_app=WebAppInfo(url="nope"))]], resize_keyboard=True)
    out = tenant._strip_url_buttons(rk, aggressive=False)
    assert [b.text for r in out.keyboard for b in r] == ["a"]


def _bot_with_session(raise_on):
    """سشنِ جعلی: اگر کیبوردِ پیام دکمه‌ای با متنِ داخلِ raise_on داشته باشد BUTTON_URL_INVALID می‌دهد."""
    calls = []

    async def make_request(bot, method, timeout=None):
        calls.append(method)
        mk = getattr(method, "reply_markup", None)
        if isinstance(mk, IM) and any(b.text in raise_on for r in mk.inline_keyboard for b in r):
            raise TelegramBadRequest(method=method, message="Bad Request: BUTTON_URL_INVALID")
        return "sent"

    bot = SimpleNamespace(session=SimpleNamespace(make_request=make_request))
    tenant._nav_install(bot)
    return bot, calls


def test_bad_button_is_stripped_and_message_still_sent(monkeypatch):
    warned = []

    async def warn(bot_):
        warned.append(1)

    monkeypatch.setattr(tenant, "_warn_owner_bad_url_button", warn)
    bot, calls = _bot_with_session(raise_on={"bad"})
    m = SendMessage(chat_id=5, text="x", reply_markup=kb([IB(text="bad", url="a.com"), IB(text="cb", callback_data="x")]))
    res = asyncio.run(bot.session.make_request(bot, m))
    assert res == "sent" and len(calls) == 2 and warned == [1]
    assert [b.text for r in calls[1].reply_markup.inline_keyboard for b in r] == ["cb"]


def test_second_stage_strips_all_url_buttons(monkeypatch):
    async def warn(bot_):
        return None

    monkeypatch.setattr(tenant, "_warn_owner_bad_url_button", warn)
    # دکمهٔ «good» ظاهراً معتبر است ولی تلگرام باز هم رد می‌کند ⇒ مرحلهٔ دوم همهٔ لینک‌ها را برمی‌دارد
    bot, calls = _bot_with_session(raise_on={"bad", "good"})
    m = SendMessage(chat_id=5, text="x", reply_markup=kb(
        [IB(text="bad", url="a.com"), IB(text="good", url="https://ok.com/x"), IB(text="cb", callback_data="x")]))
    assert asyncio.run(bot.session.make_request(bot, m)) == "sent" and len(calls) == 3
    assert [b.text for r in calls[2].reply_markup.inline_keyboard for b in r] == ["cb"]


def test_other_errors_and_clean_messages_untouched():
    bot, calls = _bot_with_session(raise_on=set())
    m = SendMessage(chat_id=5, text="x", reply_markup=kb([IB(text="cb", callback_data="x")]))
    assert asyncio.run(bot.session.make_request(bot, m)) == "sent" and len(calls) == 1

    async def other(bot_, method, timeout=None):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")

    b2 = SimpleNamespace(session=SimpleNamespace(make_request=other))
    tenant._nav_install(b2)
    with pytest.raises(TelegramBadRequest):
        asyncio.run(b2.session.make_request(b2, m))
