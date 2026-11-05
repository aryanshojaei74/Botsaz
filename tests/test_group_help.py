"""PATCH_GROUP_HELP_V1 — «راهنما گروه»: متنِ پویا از تنظیماتِ همان ربات."""
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import group_help as GH


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    tenant._SETTING_CACHE.clear()
    yield mk
    await eng.dispose()


def test_trigger_variants():
    ok = ["راهنما گروه", "راهنمای گروه", "راهنما  گروه", "📖 راهنما گروه"]
    for t in ok:
        assert GH._TRIGGER_RE.match(t), t
    for t in ("راهنما", "سلام راهنما گروه", "راهنما گروه چیه؟"):
        assert not GH._TRIGGER_RE.match(t), t


@pytest.mark.asyncio
async def test_help_reflects_settings_and_hides_disabled_sections(db):
    async with db() as s:
        b = models.Bot(owner_id=5, token="t:1", username="shop_bot", field="virtual_service", plan="pro")
        s.add(b)
        await s.flush()
        s.add(models.DiceGame(bot_id=b.id, emoji="🎲", title="تاس", active=True, in_groups=True,
                              prizes='{"1":0,"6":5000}', tries_limit=3, tries_period="day"))
        await s.commit()
        row = b
    N = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    base = (await GH.build_group_help(row, -100)).translate(N)
    assert "انتقال 5000" in base and "موجودی" in base and "@shop_bot" in base
    assert "1,000 تومان" in base                                   # حداقلِ انتقالِ پیش‌فرض
    assert "بازیِ دونفره" not in base                              # شرط‌بندی خاموش ⇒ بخشش نیست
    assert "تاس" in base and "3 بار در هر روز" in base and "5,000 تومان" in base
    await tenant.set_setting(row.id, "group_betting_enabled", "1")
    await tenant.set_setting(row.id, "group_betting_min", "2000")
    await tenant.set_setting(row.id, "group_transfer_fee_enabled", "1")
    await tenant.set_setting(row.id, "group_transfer_fee_pct", "3")
    full = (await GH.build_group_help(row, -100)).translate(N)
    assert "بازیِ دونفره" in full and "شیر یا خط 10000" in full and "2,000 تومان" in full
    assert "کارمزدِ انتقال: <b>3٪" in full
    assert len(full) < 4000                                        # در یک پیامِ تلگرام جا می‌شود
