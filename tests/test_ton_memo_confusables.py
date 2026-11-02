"""PATCH_TON_MEMO_CONFUSABLES_V1 (تیکت #906) — کامنتِ واریزِ TON که کاربر «O» حرف را به‌جایِ «0» صفر تایپ کرده هم فاکتور را پیدا می‌کند."""
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_ton_direct as P
from tenant.gateways import ton_direct as T

MEMO = "PB7C0DF0"


def test_pure_matching_rules():
    assert T.memo_in_comment(MEMO, "PB7C0DF0")
    assert T.memo_in_comment(MEMO, "pb7codfo")                      # حرفِ O و حروفِ کوچک
    assert T.memo_in_comment(MEMO, "کد: PB7C0DF0 ممنون")             # داخلِ متنِ بلندتر
    assert not T.memo_in_comment(MEMO, "PB7C0DF1")                  # یک نویسهٔ دیگر ⇒ نه
    assert not T.memo_in_comment(MEMO, "")
    assert not T.memo_in_comment("", "PB7C0DF0")
    assert T.memo_in_comment("PBA1B2C3", "PBAIB2C3") and T.memo_in_comment("PBA1B2C3", "PBALB2C3")


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(P, "Session", maker)
    async with maker() as s:
        s.add(models.PlatformTonDirectCharge(
            user_telegram_id=7188602563, memo=MEMO, amount_toman=100_000, ton_amount=Decimal("0.657"),
            rate_toman=150_000, status="expired", expires_at=dt.datetime.utcnow() - dt.timedelta(hours=1)))
        await s.commit()
    yield maker
    await eng.dispose()


def _tx(comment, ton):
    return {"hash": "h1", "comment": comment, "value_nano": int(Decimal(ton) * T.NANO)}


@pytest.mark.asyncio
async def test_platform_matches_late_payment_with_letter_o_typo(db):
    assert await P._match_charge(_tx("PB7CODFO", "0.657")) is not None          # همان مورد: O به‌جایِ 0
    assert await P._match_charge(_tx("PB7C0DF0", "0.657")) is not None          # کامنتِ درست هم مثلِ قبل
    assert await P._match_charge(_tx("PB7CODFO", "0.5")) is None                # مبلغِ کم هنوز رد می‌شود
    assert await P._match_charge(_tx("PB7CODFX", "0.657")) is None              # memoِ دیگر هنوز رد می‌شود
