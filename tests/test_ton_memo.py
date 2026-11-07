"""فروشِ تون: گرفتنِ کامنت/ممو + فاکتور + مشخصاتِ خریدار در پیامِ ادمین (PATCH_TON_MEMO_V1 / PATCH_TON_INVOICE_V1)."""
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

    # PATCH_TON_INVOICE_V1: فاکتور (روشِ پرداخت/کد تخفیف) و ارسالِ خودکار در این تست‌ها نقشی ندارند
    from aiogram.types import InlineKeyboardButton
    import tenant.shared.vs_pay as VP
    import tenant.bots.virtual_service.ton_autosend as TAS

    async def pay_rows(bot_id, tg_id, price, pay_cb, *a, **k):
        return [[InlineKeyboardButton(text="pay", callback_data=pay_cb)]]

    async def wallet_ok(bot_id):
        return True

    async def no_rows(*a, **k):
        return []

    async def no_auto(bot, row, oid, force=False):
        return "disabled", ""
    monkeypatch.setattr(VP, "pay_rows", pay_rows)
    monkeypatch.setattr(VP, "wallet_pay_allowed", wallet_ok)
    monkeypatch.setattr(S, "_vsd_rows_lazy", no_rows)
    monkeypatch.setattr(TAS, "deliver_order", no_auto)

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

    def cb(data="ton:pay"):
        async def answer(t, **kw):
            answers.append(t)

        async def edit_rm(**kw):
            return None

        async def cb_answer(*a, **k):
            return None
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=777, full_name="علی <b>", username="ali"),
                               message=SimpleNamespace(message_id=55, answer=answer, edit_reply_markup=edit_rm), answer=cb_answer)

    yield SimpleNamespace(maker=maker, state=state, notices=notices, answers=answers, settings=settings, msg=msg, cb=cb)
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
async def test_memo_then_invoice_then_pay_creates_order_with_memo(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    await S.ton_memo_in(env.msg("MEMO-998877"), object(), env.state)
    inv = env.answers[-1]
    assert "فاکتورِ خرید" in inv and "MEMO-998877" in inv and WALLET in inv and "120,000" in inv
    assert await orders(env) == [] and await env.state.get_state() is None                 # هنوز پرداخت نشده
    d = await env.state.get_data()
    assert d["ton_wallet_final"] == WALLET and d["ton_memo_final"] == "MEMO-998877"
    await S.ton_pay(env.cb(), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo == "MEMO-998877" and o.dest_label == WALLET and o.status == "awaiting_admin"
    notice = env.notices[-1]
    assert "MEMO-998877" in notice and WALLET in notice
    assert "777" in notice and "@ali" in notice and "علی &lt;b&gt;" in notice      # مشخصاتِ خریدار + escape
    reg = [a for a in env.answers if "سفارشت ثبت شد" in a][-1]                    # پیامِ ثبتِ سفارش به مشتری (آدرس + ممو + کد)
    assert "MEMO-998877" in reg and "T123" in reg and WALLET in reg
    assert await env.state.get_state() is None


@pytest.mark.asyncio
async def test_no_memo_keyword_or_button_leaves_memo_empty(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    await S.ton_memo_in(env.msg("ندارم"), object(), env.state)
    await S.ton_pay(env.cb(), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo is None and "کامنت/ممو: ندارد" in env.notices[-1]


@pytest.mark.asyncio
async def test_no_memo_glass_button(env):
    await S.ton_wallet_in(env.msg(WALLET), object(), env.state)
    # در پیامِ آدرس، دکمهٔ شیشه‌ای «🚫 ممو ندارم» هست
    await S.ton_nomemo(env.cb("ton:nomemo"), object(), env.state)
    assert "فاکتورِ خرید" in env.answers[-1] and "کامنت/ممو: ندارد" in env.answers[-1]
    assert (await env.state.get_data())["ton_memo_final"] == ""


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
    assert "فاکتورِ خرید" in env.answers[-1] and await orders(env) == []
    await S.ton_pay(env.cb(), object(), env.state)
    (o,) = await orders(env)
    assert o.dest_memo is None and "👤 خریدار" in env.notices[-1]


@pytest.mark.asyncio
async def test_discount_code_no_longer_accepted_instead_of_address_and_prompt_has_no_hint(env):
    await S.ton_wallet_in(env.msg("OFF20"), object(), env.state)               # کد تخفیف به‌جایِ آدرس: دیگر پذیرفته نمی‌شود
    assert "معتبر به‌نظر نمی‌رسد" in env.answers[-1] and await env.state.get_state() == S.SmmFSM.ton_wallet_in.state
    got = []

    async def answer(t, **k):
        got.append(t)
    await S._ton_amount_chosen(answer, object(), env.state, SimpleNamespace(id=5), 1.0, 400_000.0)
    assert "قبل از آدرس" not in got[-1] and "کد تخفیف" not in got[-1]


@pytest.mark.asyncio
async def test_pay_is_refused_without_invoice_data_and_insufficient_balance(env):
    await env.state.clear()
    await S.ton_pay(env.cb(), object(), env.state)                              # فاکتوری نیست
    assert await orders(env) == []
