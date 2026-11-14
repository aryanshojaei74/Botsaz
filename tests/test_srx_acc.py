"""PATCH_ACC_STAR_REACTION_V1 — ثبتِ خودکارِ ری‌اکشنِ استارزی با اکانتِ پشتیبانی."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
import pytest_asyncio
from sqlalchemy import select

import models
from tests.test_tgtools_gift import OWNER, USER, Cap, cb_update, env as _tg_env, wait_status  # noqa: F401
from tenant.bots.virtual_service import acc_gift as ACC


LINK = "https://t.me/mychannel/77"


@pytest_asyncio.fixture
async def env(_tg_env, monkeypatch):
    e = _tg_env
    e["rx"] = {"calls": [], "exc": None, "chunks": None}

    async def has_account(bot_id):
        return True

    async def send_paid_reaction(bot_id, *, link, count, anonymous=False, on_chunk=None):
        e["rx"]["calls"].append(dict(link=link, count=count, anon=anonymous))
        if e["rx"]["exc"]:
            if e["rx"]["chunks"] and on_chunk:
                await on_chunk(e["rx"]["chunks"])
            raise e["rx"]["exc"]
        if on_chunk:
            await on_chunk(count)

    monkeypatch.setattr(ACC, "has_account", has_account)
    monkeypatch.setattr(ACC, "send_paid_reaction", send_paid_reaction)
    return e


async def _mk_order(env, qty=30, anon=True):
    async with env["maker"]() as s:
        buyer = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
        o = models.GiftOrder(bot_id=env["bid"], buyer_user_id=buyer.id, gift_id="srx_T1", gift_emoji="⭐️",
                             star_count=qty, price_toman=qty * 1200, quantity=1, dest_telegram_id=0,
                             dest_label=LINK, status="awaiting_admin", pay_method="wallet", tracking_code="T1",
                             anonymous=anon)
        s.add(o)
        await s.commit()
        return o.id


async def _row(env):
    async with env["maker"]() as s:
        return await s.get(models.Bot, env["bid"])


async def _get(env, oid):
    async with env["maker"]() as s:
        return await s.get(models.GiftOrder, oid)


@pytest.mark.asyncio
async def test_success_marks_sent_and_notifies_buyer(env):
    oid = await _mk_order(env, 30, anon=True)
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    o = await _get(env, oid)
    assert o.status == "sent" and o.sent_qty == 30
    assert env["rx"]["calls"] == [dict(link=LINK, count=30, anon=True)]
    assert any("با موفقیت" in t for t in env["cap"].texts(USER))


@pytest.mark.asyncio
async def test_definite_failure_goes_back_to_manual_with_partial(env):
    env["rx"]["exc"] = ACC.AccGiftError("موجودیِ استارزِ اکانتِ پشتیبانی کافی نیست.")
    env["rx"]["chunks"] = 10
    oid = await _mk_order(env, 30)
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    o = await _get(env, oid)
    assert o.status == "awaiting_admin" and o.sent_qty == 10
    txt = " ".join(env["cap"].texts(OWNER))
    assert "کافی نیست" in txt and "10 از 30" in txt.replace("۱۰", "10").replace("۳۰", "30") or "10" in txt
    assert not any("با موفقیت" in t for t in env["cap"].texts(USER))


@pytest.mark.asyncio
async def test_resume_only_sends_the_remaining_after_partial(env):
    env["rx"]["exc"] = ACC.AccGiftError("x")
    env["rx"]["chunks"] = 10
    oid = await _mk_order(env, 30)
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    env["rx"]["exc"] = None
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    assert [c["count"] for c in env["rx"]["calls"]] == [30, 20]
    assert (await _get(env, oid)).status == "sent"


@pytest.mark.asyncio
async def test_ambiguous_error_warns_and_does_not_retry(env):
    env["rx"]["exc"] = ACC.AccGiftError("پاسخِ تلگرام نرسید", definite=False)
    oid = await _mk_order(env, 5)
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    assert (await _get(env, oid)).status == "awaiting_admin"
    assert len(env["rx"]["calls"]) == 1
    assert any("چک کن" in t for t in env["cap"].texts(OWNER))


@pytest.mark.asyncio
async def test_not_double_processed_and_only_srx_orders(env):
    oid = await _mk_order(env, 5)
    await asyncio.gather(env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid),
                         env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid))
    assert len(env["rx"]["calls"]) == 1
    async with env["maker"]() as s:
        o = await s.get(models.GiftOrder, oid)
        o.gift_id, o.status = "starsell_X", "awaiting_admin"
        await s.commit()
    await env["smm"].srx_acc_fulfil(env["bot"], await _row(env), oid)
    assert len(env["rx"]["calls"]) == 1


@pytest.mark.asyncio
async def test_admin_button_and_toggle(env):
    oid = await _mk_order(env, 5)
    kb = await env["smm"]._srx_admin_kb(env["bid"], oid)
    assert f"srx:acc:{oid}" in [b.callback_data for r in kb.inline_keyboard for b in r]
    await env["dp"].feed_update(env["bot"], cb_update(f"srx:acc:{oid}", uid=OWNER, uname="owner"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and len(env["rx"]["calls"]) == 1


def test_parse_post_link():
    assert ACC.parse_post_link("https://t.me/my_chan/123/") == ("my_chan", 123)
    assert ACC.parse_post_link("https://t.me/c/123/4") is None
    assert ACC.parse_post_link("hello") is None


@pytest.mark.asyncio
async def test_real_function_chunks_at_2500_and_flags(monkeypatch):
    sent = []

    class Req:
        def __init__(self, **kw):
            sent.append(kw)

    fake = types.ModuleType("telethon.tl.functions.messages")
    fake.SendPaidReactionRequest = Req
    for name in ("telethon", "telethon.tl", "telethon.tl.functions"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "telethon.tl.functions.messages", fake)

    class Conn:
        def __init__(self, creds): pass
        async def __aenter__(self):
            class C:
                async def get_input_entity(self, ch): return ("peer", ch)
                async def __call__(self, req): return True
            return C()
        async def __aexit__(self, *a): return False

    async def creds(bot_id): return (1, "h", "s")
    monkeypatch.setattr(ACC, "_creds", creds)
    monkeypatch.setattr(ACC, "_Conn", Conn)
    progress = []

    async def on_chunk(n): progress.append(n)
    await ACC.send_paid_reaction(1, link="https://t.me/mychannel/9", count=6000, anonymous=True, on_chunk=on_chunk)
    assert [r["count"] for r in sent] == [2500, 2500, 1000]
    assert progress == [2500, 5000, 6000]
    assert all(r["msg_id"] == 9 and r["peer"] == ("peer", "mychannel") for r in sent)
    import time
    assert len({r["random_id"] for r in sent}) == 3
    assert all(abs(r["random_id"] / 2 ** 32 - time.time()) < 60 for r in sent)
    with pytest.raises(ACC.AccGiftError):
        await ACC.send_paid_reaction(1, link="bad", count=3)
