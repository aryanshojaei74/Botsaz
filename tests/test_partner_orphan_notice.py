"""PATCH_PARTNER_ORPHAN_RENEW_NOTICE_V1 — خریدارِ رباتِ همکارِ غیرفعال: پیامِ «تمدید در بات‌سازِ اصلی»."""
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import partner_orphan_notice as PN


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        # رباتِ همکار (۱)، رباتِ خریدار (۲: sold)، (۳: trial)، (۴: sold ولی همکارش فعال)، (۵: مستقیم)
        for i, plan, status in ((1, "partner", "active"), (2, "growth", "active"), (3, "growth", "active"),
                                (4, "growth", "active"), (5, "growth", "active"), (6, "partner", "active")):
            s.add(models.Bot(id=i, owner_id=100 + i, token=f"{i}:T", username=f"b{i}", field="vpn_sales",
                             plan=plan, status=status))
        await s.flush()
        s.add(models.PartnerBot(id=10, bot_id=1, owner_id=101, status="paused"))
        s.add(models.PartnerBot(id=11, bot_id=6, owner_id=106, status="active"))
        await s.flush()
        s.add(models.PartnerSale(partner_bot_id=10, sold_bot_id=2, buyer_telegram_id=102, plan="growth", kind="sold"))
        s.add(models.PartnerSale(partner_bot_id=10, sold_bot_id=3, buyer_telegram_id=103, plan="growth", kind="trial"))
        s.add(models.PartnerSale(partner_bot_id=11, sold_bot_id=4, buyer_telegram_id=104, plan="growth", kind="sold"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_gone_partner_detection(db):
    async with db() as s:
        assert await PN.partner_is_gone(s, 2)            # همکار paused
        assert not await PN.partner_is_gone(s, 3)        # تست → مشمول نیست
        assert not await PN.partner_is_gone(s, 4)        # همکار فعال
        assert not await PN.partner_is_gone(s, 5)        # مستقیم از بات‌ساز
        pb = await s.get(models.PartnerBot, 11)
        pb.status = "active"
        prow = await s.get(models.Bot, 6)
        prow.archived_at = datetime.utcnow()             # رباتِ خودِ همکار بایگانی شد
        await s.commit()
        assert await PN.partner_is_gone(s, 4)


@pytest.mark.asyncio
async def test_not_sent_to_those_who_already_renewed_at_main(db):
    async with db() as s:
        b = await s.get(models.Bot, 2)
        assert await PN.notice_due(s, b)
        s.add(models.PlanOrder(user_telegram_id=102, target_bot_id=2, plan="growth", amount=50_000))
        await s.commit()
        assert not await PN.notice_due(s, b)             # قبلاً در بات‌سازِ اصلی تمدید کرده


def test_notice_text_variants():
    t = PN.build_notice(username="mybot", days_left=3)
    assert "@mybot" in t and "3" in t and "بات‌سازِ اصلی" in t and "فعال نیست" in t
    assert "امروز" in PN.build_notice(username="x", days_left=0)
    assert "بایگانی" in PN.build_notice(username="x", days_left=None, archived=True)


@pytest.mark.asyncio
async def test_send_uses_buyers_own_bot_with_glass_button(monkeypatch):
    sent = []

    class TB:
        async def send_message(self, chat, text, reply_markup=None):
            sent.append((chat, text, reply_markup))

    class Ctx:
        def __init__(self, token):
            sent.append(("token", token))

        async def __aenter__(self):
            return TB()

        async def __aexit__(self, *a):
            return False
    import bot_utils
    monkeypatch.setattr(bot_utils, "_temp_bot", Ctx)

    class PB:
        async def get_me(self):
            return SimpleNamespace(username="BotSazMain")
    b = SimpleNamespace(id=2, token="2:T", owner_id=102, username="b2")
    assert await PN.send_notice(b, PB(), days_left=2)
    assert sent[0] == ("token", "2:T") and sent[1][0] == 102
    btn = sent[1][2].inline_keyboard[0][0]
    assert btn.url == "https://t.me/BotSazMain" and "تمدید" in btn.text
