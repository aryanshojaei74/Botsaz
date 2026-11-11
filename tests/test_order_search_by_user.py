"""PATCH_ORDER_SEARCH_BY_USER_V1 — «سفارش‌ها و رسیدها»: جستجو با آیدیِ عددی/یوزرنیم ⇒ همهٔ فاکتورهایِ کاربر (پرداخت‌شده و نشده)."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
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

    async def ok_cb(cb, bot):
        return row
    monkeypatch.setattr(tenant, "_owner_ok", ok_cb)

    async def rec(cb, text, reply_markup=None):
        cb.edited.append((text, reply_markup))
    monkeypatch.setattr(tenant, "_safe_edit", rec)
    return mk


async def _view(mk, q):
    async with mk() as s:
        return await tenant._user_invoices_view(s, 1, q)


def _btns(kb):
    return [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_by_numeric_id_lists_all_statuses_and_types_as_buttons(env):
    text, kb = await _view(env, str(UID))
    texts = " | ".join(t for t, _c in _btns(kb))
    for code in ("T-PAID", "T-UNPAID", "T-REJ", "S-PEND", "G-SENT"):
        assert code in texts, code
    assert "T-OTHER" not in texts                                  # فاکتورِ کاربرِ دیگر نیست
    assert "پرداخت‌شده: <b>2</b>" in text and "پرداخت‌نشده/در انتظار: <b>2</b>" in text
    assert "&lt;b&gt;" in text                                     # نامِ کاربر escape شده
    cbs = [c for _t, c in _btns(kb)]
    assert "tadm:orders" in cbs and "tadm:order_search" in cbs
    assert any(c.startswith("tadm:order_detail:") for c in cbs)
    assert any(c.startswith("tadm:ud:s:") for c in cbs) and any(c.startswith("tadm:ud:g:") for c in cbs)


@pytest.mark.asyncio
async def test_pagination(env):
    async with env() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == UID))
        for i in range(15):
            s.add(models.Order(bot_id=1, user_id=u.id, volume_gb=1, days=1, amount=1000 + i, pay_method="card", status="paid",
                               tracking_code=f"P{i:02d}", created_at=dt.datetime(2026, 11, 1) + dt.timedelta(minutes=i)))
        await s.commit()
    t1, k1 = await _view(env, str(UID))
    assert "صفحهٔ 1 از" in t1 and any(c.startswith("tadm:ulist:") and c.endswith(":1") for _t, c in _btns(k1))
    async with env() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == UID))
    cb = _CB(f"tadm:ulist:{u.id}:1")
    await tenant.tadm_user_invoices_page(cb, NS())
    assert cb.edited and "صفحهٔ 2" in cb.edited[0][0]
    assert sum(1 for _t, c in _btns(cb.edited[0][1]) if c and (c.startswith("tadm:order_detail") or c.startswith("tadm:ud:") or c.startswith("tadm:bpl"))) <= 10


@pytest.mark.asyncio
async def test_by_username_with_and_without_at_and_case_insensitive(env):
    a = await _view(env, "@ali_user")
    b = await _view(env, "ALI_USER")
    c = await _view(env, "https://t.me/Ali_User")
    for r in (a, b, c):
        assert r and any("T-PAID" in t for t, _c in _btns(r[1]))


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
    assert "فاکتورهای کاربر" in m2.answers[0][0] and any("T-UNPAID" in t for t, _c in _btns(m2.answers[0][1]["reply_markup"]))
    m3 = _M("@nobody")
    await tenant.tadm_order_search_exec(m3, NS(), st)
    assert "یافت نشد" in m3.answers[0][0]


class _CB:
    def __init__(self, data):
        self.data = data
        self.from_user = NS(id=9)
        self.edited = []
        self.message = NS(chat=NS(id=1))

    async def answer(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_detail_pages_for_smm_gift_and_product_have_back_to_list(env, monkeypatch):
    async with env() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == UID))
        smm_o = await s.scalar(select(models.SmmOrder))
        gift_o = await s.scalar(select(models.GiftOrder))
        prod = models.Product(bot_id=1, title="محصول تون", price=1000, category="x")
        s.add(prod)
        await s.flush()
        po = models.ProductOrder(bot_id=1, user_id=u.id, product_id=prod.id, amount=4000, pay_method="wallet",
                                 status="paid", tracking_code="P-1", created_at=dt.datetime(2026, 11, 10))
        s.add(po)
        await s.commit()
        upk, sid, gid, pid = u.id, smm_o.id, gift_o.id, po.id
    import tenant.bots.virtual_service.smm as smm_mod
    monkeypatch.setattr(smm_mod, "Session", env)
    for kind, oid in (("s", sid), ("g", gid), ("p", pid)):
        cb = _CB(f"tadm:ud:{kind}:{oid}:{upk}")
        await tenant.tadm_user_invoice_detail(cb, NS())
        assert cb.edited, kind
        text, kb = cb.edited[0]
        assert f"tadm:ulist:{upk}:0" in [c for _t, c in _btns(kb)], kind            # بازگشت به همان لیستِ کاربر
        assert UNAME in text or "555001" in text, kind                              # یوزرنیم/آیدیِ کاربر در مشخصات
    assert "محصول تون" in cb.edited[0][0] and "4,000" in cb.edited[0][0]
