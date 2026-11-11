"""PATCH_ORDER_SEARCH_BY_USER_V1 — «سفارش‌ها و رسیدها»: جستجو با آیدیِ عددی/یوزرنیم ⇒ همهٔ فاکتورهایِ کاربر (پرداخت‌شده و نشده)."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant

UID, UNAME = 555001, "Ali_User"


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", mk)
    tenant._SETTING_CACHE.clear()
    t0 = dt.datetime(2026, 11, 10, 12, 0)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        u = models.BotUser(bot_id=1, telegram_id=UID, full_name="علی <b>", username=UNAME)
        other = models.BotUser(bot_id=1, telegram_id=777, full_name="دیگری", username="other")
        s.add_all([u, other])
        await s.flush()
        s.add_all([
            models.Order(bot_id=1, user_id=u.id, volume_gb=10, days=30, amount=100000, pay_method="card", status="paid",
                         tracking_code="T-PAID", created_at=t0),
            models.Order(bot_id=1, user_id=u.id, volume_gb=5, days=30, amount=50000, pay_method="card", status="awaiting_receipt",
                         tracking_code="T-UNPAID", created_at=t0 + dt.timedelta(hours=1)),
            models.Order(bot_id=1, user_id=u.id, volume_gb=0, days=0, amount=200000, pay_method="card_charge", status="rejected",
                         tracking_code="T-REJ", created_at=t0 + dt.timedelta(hours=2)),
            models.Order(bot_id=1, user_id=other.id, volume_gb=1, days=1, amount=1, pay_method="card", status="paid",
                         tracking_code="T-OTHER", created_at=t0),
            models.SmmOrder(bot_id=1, user_telegram_id=UID, service_id="1", service_name="لایک", link="x", quantity=100,
                            price_toman=7000, status="pending", tracking_code="S-PEND", created_at=t0 + dt.timedelta(hours=3)),
            models.GiftOrder(bot_id=1, buyer_user_id=u.id, gift_id="g1", gift_emoji="🎁", star_count=15, price_toman=90000,
                             quantity=1, dest_telegram_id=0, dest_label="@x", status="sent", pay_method="wallet",
                             tracking_code="G-SENT", created_at=t0 + dt.timedelta(hours=4)),
        ])
        await s.commit()
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="virtual_service")

    async def ok_msg(msg, bot):
        return row
    monkeypatch.setattr(tenant, "_owner_ok_msg", ok_msg)
    return mk


async def _view(mk, q):
    async with mk() as s:
        return await tenant._user_invoices_view(s, 1, q)


@pytest.mark.asyncio
async def test_by_numeric_id_lists_all_statuses_and_types(env):
    text, kb = await _view(env, str(UID))
    for code in ("T-PAID", "T-UNPAID", "T-REJ", "S-PEND", "G-SENT"):
        assert code in text, code
    assert "T-OTHER" not in text                                   # فاکتورِ کاربرِ دیگر نیست
    assert "پرداخت‌شده: <b>2</b>" in text                           # سفارشِ پرداخت‌شده + گیفتِ ارسال‌شده
    assert "پرداخت‌نشده/در انتظار: <b>2</b>" in text
    assert "&lt;b&gt;" in text                                     # نامِ کاربر escape شده
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "tadm:orders" in cbs and "tadm:order_search" in cbs
    assert any(c.startswith("tadm:order_detail:") for c in cbs)


@pytest.mark.asyncio
async def test_by_username_with_and_without_at_and_case_insensitive(env):
    a = await _view(env, "@ali_user")
    b = await _view(env, "ALI_USER")
    c = await _view(env, "https://t.me/Ali_User")
    assert a and b and c and "T-PAID" in a[0] and "T-PAID" in b[0] and "T-PAID" in c[0]


@pytest.mark.asyncio
async def test_unknown_user_returns_none(env):
    assert await _view(env, "999999") is None
    assert await _view(env, "@nobody") is None
    assert await _view(env, "کد پیگیری!!") is None
    assert await _view(env, "") is None


def _msg(text):
    return NS(text=text, from_user=NS(id=9), answers=[])


class _M:
    def __init__(self, text):
        self.text = text
        self.from_user = NS(id=9)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


@pytest.mark.asyncio
async def test_exec_tracking_code_still_wins_and_user_search_is_fallback(env):
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
    m = _M("T-PAID")
    await tenant.tadm_order_search_exec(m, NS(), st)
    assert "نتیجه جستجو" in m.answers[0][0] and "T-PAID" in m.answers[0][0]
    m2 = _M(str(UID))
    await tenant.tadm_order_search_exec(m2, NS(), st)
    assert "فاکتورهای کاربر" in m2.answers[0][0] and "T-UNPAID" in m2.answers[0][0]
    m3 = _M("@nobody")
    await tenant.tadm_order_search_exec(m3, NS(), st)
    assert "یافت نشد" in m3.answers[0][0]
