"""
تست‌هایِ بایگانیِ کیف‌پولِ Fragment قبل از حذفِ ربات (PATCH_FRAGMENT_WALLET_ARCHIVE_V1).

روی یک SQLiteِ درون‌حافظه‌ای اجرا می‌شوند و خودِ purge_bot را *واقعاً* صدا می‌زنند:
  • حذفِ رباتِ دارایِ کیف‌پول ⇒ ردیفِ بایگانی می‌ماند، کیف‌پولِ اصلی و ربات پاک می‌شوند.
  • اگر بایگانی خطا بدهد ⇒ ربات و کیف‌پول دست‌نخورده می‌مانند (کلید هرگز از بین نمی‌رود).
  • رباتِ بدونِ کیف‌پول ⇒ همان رفتارِ قبلی.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", maker)
    yield maker
    await engine.dispose()


async def _make_bot(maker, *, token="111:AAA", with_wallet=True, owner=42):
    async with maker() as s:
        bot = models.Bot(owner_id=owner, token=token, username="shop_bot",
                         field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        if with_wallet:
            s.add(models.FragmentWallet(
                bot_id=bot.id, address="UQ-test-address", wallet_version="v4r2",
                encrypted_mnemonic="ENCRYPTED-MNEMONIC-BLOB", last_balance_ton=12.5,
                encrypted_fragment_cookies="COOKIES", encrypted_tonapi_key="TONAPI"))
        await s.commit()
        return bot.id


@pytest.mark.asyncio
async def test_purge_archives_wallet_then_deletes_everything(db):
    bot_id = await _make_bot(db)

    token = await config.purge_bot(bot_id)
    assert token == "111:AAA"

    async with db() as s:
        assert await s.get(models.Bot, bot_id) is None
        assert (await s.scalars(select(models.FragmentWallet))).all() == []
        arch = (await s.scalars(select(models.FragmentWalletArchive))).all()
    assert len(arch) == 1
    a = arch[0]
    assert (a.old_bot_id, a.owner_id, a.bot_username) == (bot_id, 42, "shop_bot")
    assert a.address == "UQ-test-address"
    assert a.encrypted_mnemonic == "ENCRYPTED-MNEMONIC-BLOB"     # همان متنِ رمزنگاری‌شده، بدونِ رمزگشایی
    assert float(a.last_balance_ton) == 12.5
    assert a.reason == "bot_deleted"


@pytest.mark.asyncio
async def test_purge_does_not_archive_credentials_that_can_be_recreated(db):
    bot_id = await _make_bot(db)
    await config.purge_bot(bot_id)
    cols = {c.name for c in models.FragmentWalletArchive.__table__.columns}
    assert "encrypted_fragment_cookies" not in cols and "encrypted_tonapi_key" not in cols


@pytest.mark.asyncio
async def test_purge_is_blocked_if_archiving_fails(db, monkeypatch):
    bot_id = await _make_bot(db)

    async def boom(s, bot_row):
        raise OSError("disk full")
    monkeypatch.setattr(config, "archive_fragment_wallets", boom)

    with pytest.raises(RuntimeError) as ei:
        await config.purge_bot(bot_id)
    assert "ربات حذف نشد" in str(ei.value)

    async with db() as s:
        assert await s.get(models.Bot, bot_id) is not None                        # ربات هنوز هست
        assert len((await s.scalars(select(models.FragmentWallet))).all()) == 1   # کیف‌پول هنوز هست
        assert (await s.scalars(select(models.FragmentWalletArchive))).all() == []


@pytest.mark.asyncio
async def test_bot_without_wallet_purges_as_before(db):
    bot_id = await _make_bot(db, with_wallet=False)
    assert await config.purge_bot(bot_id) == "111:AAA"
    async with db() as s:
        assert await s.get(models.Bot, bot_id) is None
        assert (await s.scalars(select(models.FragmentWalletArchive))).all() == []


@pytest.mark.asyncio
async def test_other_bots_wallets_are_untouched(db):
    a = await _make_bot(db, token="1:A")
    b = await _make_bot(db, token="2:B")
    await config.purge_bot(a)
    async with db() as s:
        left = (await s.scalars(select(models.FragmentWallet))).all()
        arch = (await s.scalars(select(models.FragmentWalletArchive))).all()
    assert [w.bot_id for w in left] == [b]
    assert [x.old_bot_id for x in arch] == [a]


@pytest.mark.asyncio
async def test_migrate_bot_data_now_moves_the_wallet_with_the_bot(db):
    """تعویضِ توکن: کیف‌پول باید به رباتِ جدید برود، نه جا بماند و بعداً پاک شود."""
    old = await _make_bot(db, token="1:OLD")
    new = await _make_bot(db, token="2:NEW", with_wallet=False)
    await config.migrate_bot_data(old, new)
    async with db() as s:
        w = (await s.scalars(select(models.FragmentWallet))).one()
    assert w.bot_id == new
    # و حالا حذفِ رباتِ قدیم چیزی را بایگانی نمی‌کند (کیف‌پول دیگر مالِ آن نیست)
    await config.purge_bot(old)
    async with db() as s:
        assert (await s.scalars(select(models.FragmentWalletArchive))).all() == []
        assert len((await s.scalars(select(models.FragmentWallet))).all()) == 1
