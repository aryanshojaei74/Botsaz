"""PATCH_ACC_GIFT_V1 — گیفتِ خودکار با اکانتِ پشتیبانی (Telethon) در سفارش‌هایِ «تحویلِ دستی»."""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from tests.test_tgtools_gift import (GID, OWNER, USER, Cap, cb_update, env as _tg_env, order,  # noqa: F401
                                     set_comment, wait_status)
from tenant.bots.virtual_service import acc_gift as ACC


@pytest_asyncio.fixture
async def env(_tg_env, monkeypatch):
    e = _tg_env
    await e["tenant"].set_setting(e["bid"], "tgtools_gift", "0")          # فقط اکانت
    await e["tenant"].set_setting(e["bid"], "acc_gift", "1")
    e["tenant"]._SETTING_CACHE.clear()
    e["acc"] = {"sent": [], "exc": None, "fail_on": None}

    async def has_account(bot_id):
        return True

    async def send_gift(bot_id, *, username, tg_id, gift_id, text=None, hide_name=False):
        e["acc"]["sent"].append(dict(username=username, tg_id=tg_id, gift=gift_id, text=text, hide=hide_name))
        idx = len(e["acc"]["sent"]) - 1
        if e["acc"]["exc"] and (e["acc"]["fail_on"] is None or e["acc"]["fail_on"] == idx):
            raise e["acc"]["exc"]
    monkeypatch.setattr(ACC, "has_account", has_account)
    monkeypatch.setattr(ACC, "send_gift", send_gift)
    return e


@pytest.mark.asyncio
async def test_wallet_order_sent_per_unit_with_comment_and_anonymous(env):
    await set_comment(env, 2)
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:2:s"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and o.sent_qty == 2
    s = env["acc"]["sent"]
    assert len(s) == 2 and all(x["username"] == "aliuser" and x["gift"] == GID and x["hide"] is True
                               and x["text"] == "سلام" for x in s)
    assert env["calls"]["buy"] == []                       # TGTools صدا زده نشد
    assert any("با موفقیت" in t for t in env["cap"].texts(USER))


@pytest.mark.asyncio
async def test_definite_failure_goes_manual_and_keeps_partial(env):
    env["acc"]["exc"] = ACC.AccGiftError("موجودیِ استارزِ اکانتِ پشتیبانی کافی نیست.")
    env["acc"]["fail_on"] = 1
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:2:s"))
    for _ in range(60):
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "awaiting_admin" and o.sent_qty == 1:
            break
    o = await order(env)
    assert o.status == "awaiting_admin" and o.sent_qty == 1
    assert len(env["acc"]["sent"]) == 2                     # بدونِ retry
    txt = " ".join(env["cap"].texts(OWNER))
    assert "1 از 2" in txt and "کافی نیست" in txt


@pytest.mark.asyncio
async def test_ambiguous_error_warns_and_never_falls_back(env):
    env["acc"]["exc"] = ACC.AccGiftError("پاسخِ تلگرام نرسید", definite=False)
    await env["tenant"].set_setting(env["bid"], "tgtools_gift", "1")      # حتی با TGTools روشن، روی «نامشخص» fallback نیست
    env["tenant"]._SETTING_CACHE.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    await wait_status(env, "awaiting_admin")
    await asyncio.sleep(0.15)
    assert (await order(env)).status == "awaiting_admin"
    assert len(env["acc"]["sent"]) == 1 and env["calls"]["buy"] == []
    assert any("نامشخص" in t or "چک کن" in t for t in env["cap"].texts(OWNER))


@pytest.mark.asyncio
async def test_definite_failure_falls_back_to_tgtools_when_enabled(env):
    env["acc"]["exc"] = ACC.AccGiftError("کاربرِ مقصد پیدا نشد.")
    await env["tenant"].set_setting(env["bid"], "tgtools_gift", "1")
    env["tenant"]._SETTING_CACHE.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and len(env["calls"]["buy"]) == 1 and len(env["acc"]["sent"]) == 1


@pytest.mark.asyncio
async def test_manual_gift_never_goes_to_account(env):
    await env["dp"].feed_update(env["bot"], cb_update("gift:pay:manual1:%d:1:s" % USER))
    await asyncio.sleep(0.2)
    assert (await order(env)).status == "awaiting_admin" and env["acc"]["sent"] == []


@pytest.mark.asyncio
async def test_auto_off_shows_account_button_and_admin_press_sends(env):
    await env["tenant"].set_setting(env["bid"], "acc_gift", "0")
    env["tenant"]._SETTING_CACHE.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    o = await wait_status(env, "awaiting_admin")
    await asyncio.sleep(0.1)
    assert env["acc"]["sent"] == []
    kb = [m for m in env["cap"].markups(OWNER) if m][-1]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"gift:acc:{o.id}" in datas
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:acc:{o.id}", uid=OWNER, uname="owner"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and len(env["acc"]["sent"]) == 1


def test_error_classification_messages():
    assert "کافی نیست" in ACC._classify(Exception("BALANCE_TOO_LOW"))
    assert "تمام شده" in ACC._classify(Exception("STARGIFT_USAGE_LIMITED"))
    assert "پیدا نشد" in ACC._classify(Exception("USERNAME_NOT_OCCUPIED"))


@pytest.mark.asyncio
async def test_send_gift_rejects_non_numeric_gift_and_missing_account(monkeypatch):
    with pytest.raises(ACC.AccGiftError) as e:
        await ACC.send_gift(1, username="x", tg_id=None, gift_id="manual1")
    assert e.value.definite

    async def none(_b):
        return None
    monkeypatch.setattr(ACC, "_creds", none)
    with pytest.raises(ACC.AccGiftError):
        await ACC.send_gift(1, username="x", tg_id=None, gift_id="123")


class _FakeClient:
    def __init__(self, fail_send=None, no_entity=False):
        self.reqs, self.fail_send, self.no_entity = [], fail_send, no_entity

    async def get_input_entity(self, target):
        if self.no_entity:
            raise ValueError("Could not find the input entity")
        from telethon.tl.types import InputPeerUser
        return InputPeerUser(user_id=7, access_hash=1)

    async def __call__(self, req):
        self.reqs.append(req)
        if type(req).__name__ == "GetPaymentFormRequest":
            from types import SimpleNamespace
            return SimpleNamespace(form_id=99)
        if self.fail_send:
            raise self.fail_send
        return object()


def _patch_conn(monkeypatch, client):
    class _C:
        def __init__(self, creds): ...
        async def __aenter__(self): return client
        async def __aexit__(self, *a): ...

    async def creds(_b):
        return (1, "h", "s")
    monkeypatch.setattr(ACC, "_Conn", _C)
    monkeypatch.setattr(ACC, "_creds", creds)


@pytest.mark.asyncio
async def test_send_gift_builds_star_gift_invoice_and_pays(monkeypatch):
    c = _FakeClient()
    _patch_conn(monkeypatch, c)
    await ACC.send_gift(1, username="aliuser", tg_id=5, gift_id="5170145012310081615", text="سلام", hide_name=True)
    form_req, send_req = c.reqs
    inv = form_req.invoice
    assert inv.gift_id == 5170145012310081615 and inv.hide_name is True and inv.message.text == "سلام"
    assert send_req.form_id == 99 and send_req.invoice is inv


@pytest.mark.asyncio
async def test_send_gift_errors_definite_vs_ambiguous(monkeypatch):
    _patch_conn(monkeypatch, _FakeClient(no_entity=True))
    with pytest.raises(ACC.AccGiftError) as e:
        await ACC.send_gift(1, username=None, tg_id=5, gift_id="1")
    assert e.value.definite and "یوزرنیم ندارد" in e.value.message
    _patch_conn(monkeypatch, _FakeClient(fail_send=asyncio.TimeoutError()))
    with pytest.raises(ACC.AccGiftError) as e2:
        await ACC.send_gift(1, username="x", tg_id=None, gift_id="1")
    assert e2.value.definite is False
    from telethon.errors import RPCError
    _patch_conn(monkeypatch, _FakeClient(fail_send=RPCError(None, "BALANCE_TOO_LOW", 400)))
    with pytest.raises(ACC.AccGiftError) as e3:
        await ACC.send_gift(1, username="x", tg_id=None, gift_id="1")
    assert e3.value.definite and "کافی نیست" in e3.value.message
