"""PATCH_HOSTED_MINIAPP_V1 — ثبتِ مینی‌اپ برایِ رباتِ میزبانی‌شده."""
import pytest

import hosted_miniapp as M

TOK = "123456789:AAH" + "x" * 32


def test_validate_url():
    assert M.validate_url("https://app.example.com/x?a=1") == "https://app.example.com/x?a=1"
    for bad in ("http://a.com", "https://t.me/bot?startapp", "https://1.2.3.4/x", "https://localhost/x", "app.com", "", None, "https://a b.com", "https://x"):
        assert M.validate_url(bad) is None, bad


def test_find_bot_token():
    assert M.find_bot_token({"BOT_TOKEN": TOK}) == TOK
    assert M.find_bot_token({"x": "1", "MY_KEY": f'"{TOK}"'}) == TOK
    assert M.find_bot_token([("token", TOK)]) == TOK
    assert M.find_bot_token({"A": "nope"}) is None and M.find_bot_token({}) is None


class FakeCall:
    def __init__(self, me_ok=True, set_ok=True):
        self.calls, self.me_ok, self.set_ok = [], me_ok, set_ok

    async def __call__(self, token, method, payload=None, **kw):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"ok": self.me_ok, "result": {"username": "MyBot"}}
        return {"ok": self.set_ok, "description": "Bad Request"}


@pytest.mark.asyncio
async def test_set_for_app_success_and_payload():
    c = FakeCall()
    r = await M.set_for_app({"BOT_TOKEN": TOK}, "https://app.example.com", "🌐 مینی‌اپ", call=c)
    assert r.ok and r.bot_username == "MyBot" and "@MyBot" in r.message
    m, p = c.calls[-1]
    assert m == "setChatMenuButton" and p["menu_button"]["type"] == "web_app" and p["menu_button"]["web_app"]["url"] == "https://app.example.com"
    assert TOK not in r.message


@pytest.mark.asyncio
async def test_set_for_app_failures():
    c = FakeCall()
    assert not (await M.set_for_app({}, "https://a.example.com", call=c)).ok and not c.calls            # توکن نیست
    assert "https" in (await M.set_for_app({"BOT_TOKEN": TOK}, "http://a.com", call=c)).message and not c.calls
    assert not (await M.set_for_app({"BOT_TOKEN": TOK}, "https://a.example.com", call=FakeCall(me_ok=False))).ok
    r = await M.set_for_app({"BOT_TOKEN": TOK}, "https://a.example.com", call=FakeCall(set_ok=False))
    assert not r.ok and "قبول نکرد" in r.message


@pytest.mark.asyncio
async def test_clear_and_guide():
    c = FakeCall()
    assert (await M.clear_menu_button(TOK, call=c)).ok and c.calls[-1][1] == {"menu_button": {"type": "default"}}
    g = M.guide_text()
    assert "BotFather" in g and "https" in g and "Menu Button" in g


def test_find_bot_tokens_multi():
    import hosted_miniapp as HM
    t1 = "111111111:" + "A" * 35
    t2 = "222222222:" + "B" * 35
    t3 = "333333333:" + "C" * 35
    assert HM.find_bot_tokens({"BOT_TOKEN": t1}) == [t1]
    assert HM.find_bot_tokens({"bot_tokens": f"{t1}, {t2}\n{t3}"}) == [t1, t2, t3]
    assert HM.find_bot_tokens({"BOT_TOKEN": t1, "BOT_TOKENS": f"{t2},{t1}"}) == [t2, t1]      # بدونِ تکرار
    assert HM.find_bot_tokens({"PORT": "3000", "X": "nope"}) == []
    assert HM.find_bot_token({"BOT_TOKEN": t1}) == t1          # رفتارِ قبلی دست‌نخورده


def test_set_for_app_multi_bots():
    import asyncio
    import hosted_miniapp as HM
    t1 = "111111111:" + "A" * 35
    t2 = "222222222:" + "B" * 35
    calls = []

    async def fake_call(token, method, payload=None, **k):
        calls.append((token, method))
        if method == "getMe":
            return {"ok": True, "result": {"username": "bot" + token[:1]}}
        return {"ok": True}
    r = asyncio.run(HM.set_for_app({"BOT_TOKENS": f"{t1},{t2}"}, "https://game.example.com", call=fake_call))
    assert r.ok and "@bot1" in r.message and "@bot2" in r.message
    assert [c for c in calls if c[1] == "setChatMenuButton"] == [(t1, "setChatMenuButton"), (t2, "setChatMenuButton")]
