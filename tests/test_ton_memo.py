"""فروشِ تون: گرفتنِ کامنت/ممو + مشخصاتِ خریدار در پیامِ ادمین (PATCH_TON_MEMO_V1)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
import tenant.bots.virtual_service.smm as S

WALLET = "UQC_dYB8JhXHJSAzSHbvQvwxEsECfpfySLEPESBmVSTdu2Kn"


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="علی", username="ali", balance=500_000))
        await s.commit()

    settings = {"ton_memo_ask": "1"}
    notices, answers = [], []

    async def get_bot_row(bot):
        return SimpleNamespace(id=5, owner_id=1, field="virtual_service", plan="pro")

    async def get_or_create_user(bot_id, tg_id, name, username, return_created=False):
        async with maker() as s:
            u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == tg_id))
        return (u, False) if return_created else u

    async def get_setting(bot_id, key, default=""):
        return settings.get(key, default)

    async def sell_settings(bot_id):
        return 400_000.0, 0.1, 100.0

    async def alloc(s, bot_id):
        return "T123"

    async def fp(bot_id, telegram_id, price, session=None):
        return SimpleNamespace(ok=False, discount=0, final_price=price)

    async def noop(*a, **k):
        return None

    async def notify(bot, row, text, kb=None, **kw):
        notices.append(text)

    import tenant.gateways.discount_engine as DE
    monkeypatch.setattr(S, "Session", maker)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "get_or_create_user", get_or_create_user)
    monkeypatch.setattr(S, "_get_setting", get_setting)
    monkeypatch.setattr(S, "_ton_sell_settings", sell_settings)
    monkeypatch.setattr(models, "alloc_tracking_in", alloc)
    monkeypatch.setattr(DE, "first_purchase_discount", fp)
    monkeypatch.setattr(S, "_notify_gift_order_trust_success", noop)
    monkeypatch.setattr(S, "_gift_support_kb", noop)
    monkeypatch.setattr(S, "_notify_manual_order", notify)
    import tenant as T

    async def scb(bot):
        return None

    monkeypatch.setattr(T, "smart_cancel_kb", scb)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=777, user_id=777))
    await state.set_state(S.SmmFSM.ton_wallet_in)
    await state.update_data(ton_amount=0.3)

    def msg(text):
        async def answer(t, **kw):
            answers.append(t)

        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=777, full_name="علی <b>", username="ali"),
                               answer=answer)

    yield SimpleNamespace(maker=maker, state=state, notices=notices, answers=answers, settings=settings, msg=msg)
    await engine.dispose()


async def orders(env):
    async with env.maker() as s:
        return (await s.scalars(select(models.GiftOrder))).all()


@pytest.mark.asyncio
async def test_wallet_step_asks_for_memo_and_creates_no_order(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    assert "کامنت" in env.answers[-1] and WALLET in env.answers[-1]
    assert await env.state.get_state() == S.SmmFSM.ton_memo_in.state
    assert (await env.state.get_data())["ton_wallet_pending"] == WALLET
    assert await orders(env) == []


@pytest.mark.asyncio
async def test_memo_is_saved_and_shown_to_admin_with_buyer_details(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    await S.ton_memo_in(env.msg("MEMO-998877"), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo == "MEMO-998877" and o.dest_label == WALLET and o.status == "awaiting_admin"
    notice = env.notices[-1]
    assert "MEMO-998877" in notice and WALLET in notice
    assert "777" in notice and "@ali" in notice and "علی &lt;b&gt;" in notice      # مشخصاتِ خریدار + escape
    assert await env.state.get_state() is None


@pytest.mark.asyncio
async def test_no_memo_keyword_leaves_memo_empty(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    await S.ton_memo_in(env.msg("ندارم"), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo is None and "کامنت/ممو: ندارد" in env.notices[-1]


@pytest.mark.asyncio
async def test_too_long_memo_rejected_and_state_kept(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    await S.ton_memo_in(env.msg("x" * 200), object(), env.state)
    assert "خیلی بلند" in env.answers[-1] and await orders(env) == []
    assert await env.state.get_state() == S.SmmFSM.ton_memo_in.state


@pytest.mark.asyncio
async def test_memo_step_can_be_disabled(env):
    env.settings["ton_memo_ask"] = "0"
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo is None and "👤 خریدار" in env.notices[-1]
