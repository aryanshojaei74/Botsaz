"""#2413 — تیکتِ «پیگیریِ سفارش» از منویِ تیکت باید مشخصاتِ آخرین سفارش‌ها را داشته باشد."""
import pytest
import pytest_asyncio
from decimal import Decimal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, smm):
        monkeypatch.setattr(m, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        u = models.BotUser(bot_id=3, telegram_id=200, balance=Decimal(0))
        s.add(u)
        await s.flush()
        s.add(models.GiftOrder(bot_id=3, buyer_user_id=u.id, gift_id="starsell_50", star_count=50, price_toman=10000,
                               dest_telegram_id=200, dest_label="x", status="sent", quantity=1, tracking_code="AB12"))
        await s.commit()
    yield
    await eng.dispose()


@pytest.mark.asyncio
async def test_block_lists_buyer_orders(env, monkeypatch):
    async def det(bot_id, trk):
        return f"details-of-{trk}"
    monkeypatch.setattr(smm, "_order_details_html", det)
    out = await smm.recent_orders_ticket_block(3, 200)
    assert "AB12" in out and "details-of-AB12" in out


@pytest.mark.asyncio
async def test_block_without_orders_says_so(env):
    out = await smm.recent_orders_ticket_block(3, 999)
    assert "سفارشِ ثبت‌شده" in out


def test_compact_details_copies_values_only_in_expandable_quote():
    det = ("🔴 <b>وضعیت سفارش:</b> تکمیل\n\n📋 <b>مشخصات سفارش:</b>\n<code>\n≡ شناسه سفارش : AB12\n"
           "≡ لینک : https://t.me/x?a=1&b=2\n≡ باقیمانده : —\n</code>\n🗓 تاریخ ثبت: 1405")
    out = smm.compact_order_details(det)
    assert out.startswith("<blockquote expandable>") and out.endswith("</blockquote>")
    assert "شناسه سفارش: <code>AB12</code>" in out
    assert "لینک: <code>https://t.me/x?a=1&amp;b=2</code>" in out
    assert "باقیمانده: —" in out and "≡" not in out
    assert "<code>\n" not in out                      # بلوکِ کپیِ کامل برداشته شد
