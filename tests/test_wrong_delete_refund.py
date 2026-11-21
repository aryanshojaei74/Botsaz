"""عودتِ حذفِ اشتباهِ سرویس با تأییدِ ادمینِ ربات — PATCH_WRONG_DELETE_REFUND_V1."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.shared import wrong_delete_refund as W

pytestmark = pytest.mark.asyncio

DELETED = dt.datetime(2026, 10, 19, 11, 19, 21)
EMAIL = "taziyanteam7375002338h6m"
TG = 7375002338


@pytest_asyncio.fixture
async def maker(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(W, "Session", m)
    async with m() as s:
        b = models.Bot(id=313, owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        s.add(models.BotUser(bot_id=313, telegram_id=TG, full_name="A", balance=0))
        await s.commit()
    yield m
    await eng.dispose()


async def _uid(m):
    async with m() as s:
        return await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == TG))


async def _add_txn(m, kind, amount, when):
    uid = await _uid(m)
    async with m() as s:
        s.add(models.WalletTxn(bot_id=313, user_id=uid, amount=amount, kind=kind, note="x", created_at=when))
        await s.commit()


async def test_manual_admin_credit_after_deletion_is_respected(maker):
    # شارژِ دستیِ ادمین بعد از حذف: کاملِ مبلغ ⇒ پیشنهاد نمی‌شود
    await _add_txn(maker, "admin", 29600, DELETED + dt.timedelta(hours=1))
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    assert r["status"] == "manual_done"


async def test_partial_manual_credit_only_proposes_the_difference(maker):
    await _add_txn(maker, "admin", 10000, DELETED + dt.timedelta(minutes=30))
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    assert r["status"] == "created" and r["amount"] == 19600 and r["credited"] == 10000


async def test_other_credits_do_not_count_as_manual_compensation(maker):
    await _add_txn(maker, "admin", 50000, DELETED - dt.timedelta(days=1))          # قبل از حذف
    await _add_txn(maker, "zarinpal_charge", 50000, DELETED + dt.timedelta(hours=1))  # شارژِ خودِ کاربر
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    assert r["status"] == "created" and r["amount"] == 29600


async def test_proposal_is_created_once(maker):
    assert (await W.create_proposal(313, TG, EMAIL, 29600, DELETED))["status"] == "created"
    assert (await W.create_proposal(313, TG, EMAIL, 29600, DELETED))["status"] == "exists"


class _Cb:
    def __init__(self, data):
        self.data = data
        self.alerts, self.answers = [], []

        async def answer(text="", **k):
            self.alerts.append(text)

        async def edit_reply_markup(**k):
            self.answers.append("markup_removed")

        async def msg_answer(text, **k):
            self.answers.append(text)
        self.answer = answer
        self.message = SimpleNamespace(edit_reply_markup=edit_reply_markup, answer=msg_answer)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))


async def _balance(m):
    async with m() as s:
        return float(await s.scalar(select(models.BotUser.balance).where(models.BotUser.telegram_id == TG)))


async def _setup(monkeypatch, owner_ok=True):
    async def ok(cb, bot):
        return SimpleNamespace(id=313) if owner_ok else None
    monkeypatch.setattr(tenant, "_owner_ok", ok)


async def test_approve_credits_wallet_once_and_notifies_user(maker, monkeypatch):
    await _setup(monkeypatch)
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    bot, cb = _Bot(), _Cb(f"wdr:ok:{r['pid']}")
    await W.wdr_decide(cb, bot)
    assert await _balance(maker) == 29600
    assert bot.sent and bot.sent[0][0] == TG and "29,600" in bot.sent[0][1]
    async with maker() as s:
        refunds = (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.kind == "refund"))).all()
        assert len(refunds) == 1 and float(refunds[0].amount) == 29600
    cb2 = _Cb(f"wdr:ok:{r['pid']}")                           # دوباره زدن
    await W.wdr_decide(cb2, bot)
    assert await _balance(maker) == 29600 and "قبلاً بررسی شده" in cb2.alerts[-1]
    assert len(bot.sent) == 1


async def test_reject_never_credits(maker, monkeypatch):
    await _setup(monkeypatch)
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    cb = _Cb(f"wdr:no:{r['pid']}")
    await W.wdr_decide(cb, _Bot())
    assert await _balance(maker) == 0
    cb2 = _Cb(f"wdr:ok:{r['pid']}")                           # بعد از رد، تأیید اثری ندارد
    await W.wdr_decide(cb2, _Bot())
    assert await _balance(maker) == 0


async def test_only_the_bot_owner_or_admin_can_decide(maker, monkeypatch):
    await _setup(monkeypatch, owner_ok=False)
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    await W.wdr_decide(_Cb(f"wdr:ok:{r['pid']}"), _Bot())
    assert await _balance(maker) == 0


async def test_amount_comes_from_server_not_from_callback(maker, monkeypatch):
    await _setup(monkeypatch)
    r = await W.create_proposal(313, TG, EMAIL, 29600, DELETED)
    await W.wdr_decide(_Cb(f"wdr:ok:{r['pid']}:999999999"), _Bot())   # مبلغِ تحمیلی نادیده گرفته می‌شود
    assert await _balance(maker) in (0, 29600)
    assert await _balance(maker) <= 29600
