"""شارژِ کیف‌پولِ بات‌ساز با رسیدِ کارت‌به‌کارت (تأییدِ خودکار): رسیدِ بدونِ اثرِ انگشت نباید پذیرفته شود."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as PB
import tenant.gateways.auto_receipt as AR


class _Bot:
    """هر متدی (send_message/send_photo/...) یک no-op ناهمگام است؛ get_file شناسهٔ یکتا برمی‌گرداند."""
    async def get_file(self, file_id):
        return type("F", (), {"file_unique_id": "U" + file_id})()

    def __getattr__(self, name):
        async def _noop(*a, **k):
            return None
        return _noop


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", maker)
    monkeypatch.setattr(PB, "Session", maker)

    async def cards():
        return [("6037991111111111", "")]
    monkeypatch.setattr(PB, "_platform_cards", cards)
    monkeypatch.setattr(PB, "PLATFORM_ADMIN_ID", 0)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=42, full_name="u", balance=0))
        for i, fid in enumerate(("fA", "fB", "fC"), 1):
            s.add(models.WalletCharge(user_telegram_id=42, amount=100_000, receipt_file_id=fid, status="pending"))
        await s.commit()
    yield maker, monkeypatch
    await engine.dispose()


def _fake_eval(monkeypatch, **kw):
    async def ev(*a, **k):
        return AR.ReceiptCheck(ok=True, reason="ok", amount=100_000, card_last4="1111", **kw)
    monkeypatch.setattr(AR, "evaluate_receipt", ev)


async def _balance(maker):
    async with maker() as s:
        return float((await s.scalar(select(models.PlatformUser))).balance)


@pytest.mark.asyncio
async def test_receipt_without_tracking_or_datetime_is_sent_to_manual_review(env):
    maker, mp = env
    _fake_eval(mp)                                              # نه پیگیری نه تاریخ/ساعت (مثلاً عکسِ برش‌خورده)
    ok, why = await PB._try_auto_approve_platform_charge(_Bot(), 1)
    assert not ok and "بررسیِ دستی" in why and await _balance(maker) == 0
    ok2, _ = await PB._try_auto_approve_platform_charge(_Bot(), 2)   # هر چند بار هم بفرستد شارژ نمی‌شود
    assert not ok2 and await _balance(maker) == 0


@pytest.mark.asyncio
async def test_same_receipt_cannot_charge_twice_even_with_other_file(env):
    maker, mp = env
    _fake_eval(mp, tracking_no="123456789")
    assert (await PB._try_auto_approve_platform_charge(_Bot(), 1))[0]
    assert await _balance(maker) == 100_000
    ok, why = await PB._try_auto_approve_platform_charge(_Bot(), 2)      # همان پیگیری، فایلِ دیگر
    assert not ok and "تکراری" in why and await _balance(maker) == 100_000


@pytest.mark.asyncio
async def test_same_photo_cannot_charge_twice_even_if_tracking_differs(env):
    maker, mp = env
    async with maker() as s:                                    # هر دو درخواست همان عکس (همان file_unique_id)
        for c in (await s.scalars(select(models.WalletCharge))).all():
            c.receipt_file_id = "fSAME"
        await s.commit()
    seq = iter(["111111", "222222"])

    async def ev(*a, **k):
        return AR.ReceiptCheck(ok=True, reason="ok", amount=100_000, card_last4="1111", tracking_no=next(seq))
    mp.setattr(AR, "evaluate_receipt", ev)
    assert (await PB._try_auto_approve_platform_charge(_Bot(), 1))[0]
    ok, _w = await PB._try_auto_approve_platform_charge(_Bot(), 2)       # OCR این بار پیگیریِ دیگری خوانده
    assert not ok and await _balance(maker) == 100_000
