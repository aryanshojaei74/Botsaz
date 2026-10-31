"""PATCH_PENDING_LISTS_FIX_V1 (فدایی): «سفارش‌هایِ در انتظار» استارز/تون/ری‌اکشن/گیفت جواب می‌دهند و دکمهٔ تأیید/رد دارند."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.virtual_service import smm as S

ROW = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="virtual_service")


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(S, "Session", mk)
    monkeypatch.setattr(models, "Session", mk, raising=False)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=11, full_name="A", balance=0))
        await s.commit()

    async def owner(cb, bot):
        return ROW
    monkeypatch.setattr(S, "_owner", owner)
    yield mk
    await eng.dispose()


async def _order(mk, gift_id, status="awaiting_admin", **kw):
    async with mk() as s:
        o = models.GiftOrder(bot_id=1, buyer_user_id=1, gift_id=gift_id, star_count=kw.pop("star_count", 100),
                             price_toman=kw.pop("price", 450_000), dest_telegram_id=0,
                             dest_label=kw.pop("dest", "@user<1>"), status=status,
                             tracking_code=kw.pop("trk", "TRK1"), **kw)
        s.add(o)
        await s.flush()
        oid = o.id
        await s.commit()
    return oid


def _cb(data):
    out = {"edits": [], "answers": [], "alerts": []}

    async def edit_text(t, **k):
        out["edits"].append((t, k))

    async def answer(t, **k):
        out["answers"].append((t, k))

    async def cb_answer(t="", show_alert=False, **k):
        out["alerts"].append((t, show_alert))
    msg = NS(edit_text=edit_text, answer=answer, edit_reply_markup=lambda **k: _none(), chat=NS(id=1), message_id=5)
    cb = NS(data=data, from_user=NS(id=9), message=msg, answer=cb_answer,
            model_copy=lambda update: _cb_copy(cb, update))
    cb.out = out
    return cb


async def _none():
    return None


def _cb_copy(cb, update):
    n = NS(**{**cb.__dict__, **update})
    n.model_copy = lambda u: _cb_copy(n, u)
    return n


def _text(cb):
    t = [x[0] for x in cb.out["answers"]] + [x[0] for x in cb.out["edits"]]
    return t[-1] if t else ""


def _markup(cb):
    for coll in (cb.out["answers"], cb.out["edits"]):
        for _t, k in reversed(coll):
            if k.get("reply_markup") is not None:
                return k["reply_markup"]
    return None


def _datas(cb):
    mk = _markup(cb)
    return [b.callback_data for r in mk.inline_keyboard for b in r if b.callback_data] if mk else []


@pytest.mark.asyncio
async def test_stars_pending_list_renders_with_memo_and_buttons(env):
    oid = await _order(env, "starsell_50", star_count=50, dest_memo="memo<x>")
    cb = _cb("ssl:orders:0")
    await S.ssl_orders(cb, NS())
    t = _text(cb)
    assert "TRK1" in t and "50" in t and "memo&lt;x&gt;" in t and "@user&lt;1&gt;" in t
    assert f"star:done:{oid}" in _datas(cb) and f"star:reject:{oid}" in _datas(cb)


@pytest.mark.asyncio
async def test_ton_pending_list_renders(env):
    oid = await _order(env, "tonsell_1", star_count=250, price=900_000, dest="UQ-wallet", dest_memo="m1")
    cb = _cb("ton:orders:0")
    await S.ton_orders(cb, NS())
    assert "2.5 TON" in _text(cb) and "m1" in _text(cb)
    assert f"ton:done:{oid}" in _datas(cb) and f"ton:refund:{oid}" in _datas(cb)


@pytest.mark.asyncio
async def test_srx_pending_list_and_detail_use_html(env):
    oid = await _order(env, "srx_1", star_count=30, dest="https://t.me/c/1/2", anonymous=True)
    cb = _cb("srx:orders:0")
    await S.srx_orders_list(cb, NS())
    assert "TRK1" in _text(cb) and f"srx:detail:{oid}" in _datas(cb)
    cb2 = _cb(f"srx:detail:{oid}")
    await S.srx_order_detail(cb2, NS())
    assert cb2.out["edits"][-1][1].get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_gift_orders_list_shows_pending_section_with_approve_reject(env):
    a = await _order(env, "starsell_50", star_count=50, trk="TS")
    b = await _order(env, "tonsell_1", star_count=100, trk="TT")
    c = await _order(env, "manual_x", star_count=0, trk="TM")
    d = await _order(env, "gift_5", status="sent", trk="TD", gift_emoji="🧸")
    cb = _cb("gift:orders:0")
    await S.gift_orders(cb, NS())
    t = _text(cb)
    assert "در انتظارِ تأیید شما: 3" in t and "استارز" in t and "TON" in t and "گیفتِ سفارشی" in t
    ds = _datas(cb)
    for oid in (a, b, c):
        assert f"gift:lp:ok:{oid}:0" in ds and f"gift:lp:no:{oid}:0" in ds
    assert f"gift:lp:ok:{d}:0" not in ds                       # سفارشِ ارسال‌شده دکمه ندارد


@pytest.mark.asyncio
async def test_gift_list_pending_action_routes_to_matching_handler_and_refreshes(env, monkeypatch):
    a = await _order(env, "starsell_50", trk="TS")
    c = await _order(env, "manual_x", trk="TM")
    called = []

    async def sd(cb, bot):
        called.append(cb.data)

    async def ca(cb, bot):
        called.append(cb.data)
    monkeypatch.setattr(S, "star_admin_reject", sd)
    monkeypatch.setattr(S, "gift_cardpay_approve", ca)
    cb = _cb(f"gift:lp:no:{a}:0")
    await S.gift_list_pending_action(cb, NS())
    cb = _cb(f"gift:lp:ok:{c}:0")
    await S.gift_list_pending_action(cb, NS())
    assert called == [f"star:reject:{a}", f"gift:cardok:{c}"]
