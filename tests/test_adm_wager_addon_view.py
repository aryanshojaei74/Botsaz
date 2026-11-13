"""PATCH_ADM_WAGER_ADDON_VIEW_V1 — تاریخچهٔ خریدِ بازیِ گروهی و فعال/غیرفعال در پنلِ ادمین."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_addons
import config
import models
import platform_bot as pb


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, pb):
        monkeypatch.setattr(m, "Session", mk)

    async def _perm(*a, **k):
        return True
    monkeypatch.setattr(pb, "has_perm", _perm)
    async with mk() as s:
        s.add_all([
            models.Bot(id=1, owner_id=42, token="1:T", username="weirdnet_bot", field="vpn_sales", plan="pro"),
            models.Bot(id=2, owner_id=42, token="2:T", username="old_bot", field="vpn_sales", plan="pro"),
        ])
        await s.commit()
    yield mk
    await eng.dispose()


class _Msg:
    def __init__(self):
        self.texts = []

    async def edit_text(self, t, **k):
        self.texts.append((t, k.get("reply_markup")))


def _cb(data):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=9), message=_Msg(), answer=_ans)


async def _ans(*a, **k):
    return None


@pytest.mark.asyncio
async def test_not_bought_then_admin_activate_and_deactivate(db):
    c = _cb("abotgame:1:bots")
    await pb.adm_bot_game_history(c)
    t, kb = c.message.texts[-1]
    assert "ثبت نشده" in t
    assert any("abotgamet:1" in b.callback_data for r in kb.inline_keyboard for b in r)
    await pb.adm_bot_game_toggle(_cb("abotgamet:1:bots"))
    assert await bot_addons.addon_active(1)
    async with db() as s:
        row = await s.scalar(select(models.BotAddon).where(models.BotAddon.bot_id == 1))
    assert row.source == "admin" and row.paid_by == 9
    await pb.adm_bot_game_toggle(_cb("abotgamet:1:bots"))
    assert not await bot_addons.addon_active(1)


@pytest.mark.asyncio
async def test_history_shows_price_and_sibling_bot_addon(db):
    async with db() as s:
        s.add(models.BotAddon(bot_id=2, addon=bot_addons.WAGER_ADDON, amount=50000, paid_by=42, source="wallet"))
        s.add(models.PlatformTxn(user_telegram_id=42, amount=-50000, kind="purchase",
                                 note="فعال‌سازی بازی‌های گروهی برای @old_bot"))
        await s.commit()
    c = _cb("abotgame:1:bots")
    await pb.adm_bot_game_history(c)
    t, _ = c.message.texts[-1]
    assert "@old_bot" in t and "50,000" in t and "رباتِ دیگرِ همین مالک" in t
    c2 = _cb("abotgame:2:bots")
    await pb.adm_bot_game_history(c2)
    t2, _ = c2.message.texts[-1]
    assert "فعال" in t2 and "50,000" in t2 and "کیف پول" in t2
