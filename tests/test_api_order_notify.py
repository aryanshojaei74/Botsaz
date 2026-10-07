"""PATCH_API_ORDER_NOTIFY_V1 — اعلانِ وضعیتِ سفارش‌هایِ وب‌سرویس فقط با روشن‌بودنِ کلید و با #وبسرویس."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


class _FakeBot:
    sent = []

    async def send_message(self, chat_id, text, **kw):
        _FakeBot.sent.append((chat_id, text))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    _FakeBot.sent = []
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        for oid, src in ((1, "api"), (2, None)):
            s.add(models.SmmOrder(id=oid, bot_id=5, user_telegram_id=777, smm_service_pk=1, service_id="1",
                                  service_name="x", quantity=10, link="l", price_toman=1000,
                                  status="completed", source=src))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_api_order_notify_toggle(db):
    bot = _FakeBot()
    await smm._notify_user(5, 1, "✅ تکمیل شد", bot=bot)
    await smm._notify_user(5, 2, "✅ تکمیل شد", bot=bot)
    assert _FakeBot.sent == [(777, "✅ تکمیل شد")]          # سفارشِ API با کلیدِ خاموش (پیش‌فرض) نمی‌آید
    await smm.set_api_notify(5, 777, True)
    assert await smm.api_notify_enabled(5, 777)
    await smm._notify_user(5, 1, "✅ تکمیل شد", bot=bot)
    assert _FakeBot.sent[-1] == (777, "#وبسرویس\n✅ تکمیل شد")


@pytest.mark.asyncio
async def test_api_receipt_only_when_enabled(db, monkeypatch):
    """PATCH_API_ORDER_RECEIPT_V1"""
    import aiogram
    from tenant.shared import public_api as pa
    monkeypatch.setattr(pa, "Session", db)
    sent = []

    class _Sess:
        async def close(self):
            pass

    class _B:
        def __init__(self, token, default=None):
            self.session = _Sess()

        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))
    monkeypatch.setattr(aiogram, "Bot", _B)
    kw = dict(name="❤️ری اکشن [+🎁 بازدید]", platform="تلگرام", qty=100, price=380,
              link="https://t.me/x/6", order_id=4194758,
              unit_line="💸 قیمت هر 1000 عدد: <b>3,800</b> تومان")
    await pa._send_api_receipt(5, 777, **kw)
    assert sent == []
    await smm.set_api_notify(5, 777, True)
    await pa._send_api_receipt(5, 777, **kw)
    txt = sent[0][1]
    assert txt.startswith("🔗 #وبسرویس") and "4194758" in txt and "تلگرام" in txt
    assert "📍 موجودی جدید: <b>0</b> تومان" in txt and "https://t.me/x/6" in txt
