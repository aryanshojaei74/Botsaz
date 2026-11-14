"""فدایی: پیگیریِ سفارش — تیکتِ کامل با متنِ مشتری + مشخصاتِ سفارش؛ بستنِ خودکار؛ خاموش‌کردنِ دکمه."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm as S
from tenant.shared import support_ticket as ST

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (config, S, ST, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.flush()
        s.add(models.BotUser(id=1, bot_id=4, telegram_id=555, full_name="Ali"))
        await s.flush()
        s.add(models.GiftOrder(bot_id=4, buyer_user_id=1, gift_id="starsell_100", gift_emoji="⭐", star_count=100,
                               price_toman=20000, dest_telegram_id=555, dest_label="خودم", status="sent",
                               tracking_code="TRK12345", quantity=1))
        await s.commit()
    yield maker
    await eng.dispose()


async def test_ticket_text_has_user_message_and_order_details(db):
    txt = await S.build_order_ticket_text(4, "TRK12345", "سفارشم کی میرسه؟")
    assert "TRK12345" in txt and "سفارشم کی میرسه؟" in txt and "مشخصاتِ سفارش" in txt
    assert "پیدا نشد" not in txt                                  # مشخصاتِ واقعی خوانده شد
    unknown = await S.build_order_ticket_text(4, "NOPE9999", "سلام")
    assert "پیدا نشد" in unknown


async def test_autoclose_closes_only_stale_answered_without_user_reply(db, monkeypatch):
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(ST.Bot, "send_message", noop, raising=False)
    old = dt.datetime.utcnow() - dt.timedelta(hours=30)
    async with db() as s:
        s.add(models.Setting(bot_id=4, key="ticket_autoclose_hours", value="24"))
        for i, (st, sender, when) in enumerate((
                ("answered", "admin", old),                                 # باید بسته شود
                ("answered", "admin", dt.datetime.utcnow()),                # تازه
                ("answered", "user", old),                                  # آخرین پیام مالِ کاربر
                ("open", "admin", old)), 1):                                # باز است
            t = models.TenantTicket(id=i, bot_id=4, user_id=1, text="x", status=st)
            s.add(t)
            await s.flush()
            s.add(models.TenantTicketReply(ticket_id=i, sender=sender, text="r", created_at=when))
        await s.commit()
    n = await ST.auto_close_answered_tickets_once()
    assert n == 1
    async with db() as s:
        st = {t.id: t.status for t in (await s.scalars(select(models.TenantTicket))).all()}
    assert st == {1: "closed", 2: "answered", 3: "answered", 4: "open"}


async def test_autoclose_off_by_default(db):
    async with db() as s:
        t = models.TenantTicket(id=9, bot_id=4, user_id=1, text="x", status="answered")
        s.add(t)
        await s.flush()
        s.add(models.TenantTicketReply(ticket_id=9, sender="admin", text="r",
                                       created_at=dt.datetime.utcnow() - dt.timedelta(days=9)))
        await s.commit()
    assert await ST.auto_close_answered_tickets_once() == 0
