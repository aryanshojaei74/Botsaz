"""PATCH_DICE_PRIZE_VS_DUEL_V1 (سینا): «تاس» در گروه هم «بازی کن و جایزه بگیر» است هم دوئلِ رایگان — هر دو باید کار کنند:
«تاس» ⇒ جایزه (ربات تاس می‌اندازد)، «بازی تاس» ⇒ دوئلِ رایگان. بدونِ بازیِ جایزه، «تاس» مثلِ قبل دوئل می‌سازد."""
import types

import pytest
import pytest_asyncio
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_wager_race as R
import platform_wager as core
from tenant.shared import dice_game as DG
from tenant.shared import wager_race as TWR


def test_free_command_accepts_play_prefix_only_for_duels():
    for t, g in (("بازی تاس", "dice"), ("بازی  دوز", "xo"), ("بازی سنگ کاغذ قیچی", "rps"), ("تاس", "dice")):
        assert R.parse_free_command(t) == g, t
    for t in ("بازی سرعت", "بازی ریاضی", "بازی", "بازی تاس 1000", "بازی 1000"):
        assert R.parse_free_command(t) is None, t
    assert R.has_play_prefix("بازی تاس") and not R.has_play_prefix("تاس")


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(DG, "Session", mk)
    async with mk() as s:
        b = models.Bot(owner_id=5, token="t:1", username="shop_bot", field="virtual_service", plan="pro")
        s.add(b)
        await s.flush()
        s.add(models.DiceGame(bot_id=b.id, emoji="🎲", title="تاس", active=True, in_groups=True, prizes='{"6":5000}'))
        await s.commit()
        bid = b.id
    yield mk, bid
    await eng.dispose()


def _msg(text, chat_type="supergroup", chat_id=-100):
    return types.SimpleNamespace(text=text, chat=types.SimpleNamespace(id=chat_id, type=chat_type),
                                 from_user=types.SimpleNamespace(id=1, full_name="u1", username=None, is_bot=False),
                                 sender_chat=None, reply_to_message=None)


@pytest.mark.asyncio
async def test_claims_text_only_for_the_exact_game_name_in_groups_it_serves(db):
    _mk, bid = db
    assert await DG.claims_text(_msg("تاس"), bid)
    assert await DG.claims_text(_msg("🎲"), bid)
    assert not await DG.claims_text(_msg("بازی تاس"), bid)
    assert not await DG.claims_text(_msg("تاس 1000"), bid)
    assert not await DG.claims_text(_msg("تاس"), bid + 99)               # ربات دیگر


@pytest.mark.asyncio
async def test_tenant_free_command_defers_to_the_prize_game_but_play_prefix_makes_the_duel(db, monkeypatch):
    _mk, bid = db
    row = types.SimpleNamespace(id=bid, field="virtual_service", plan="pro")

    async def row_admin(bot, uid):
        return row, False

    async def yes(*a, **k):
        return True

    async def user(*a, **k):
        return None
    created = []

    async def free_create(msg, bot, bot_id, game):
        created.append(game)
    import bot_addons
    import tenant
    monkeypatch.setattr(TWR, "_row_and_admin", row_admin)
    monkeypatch.setattr(bot_addons, "wager_eligible", lambda f, p: True)
    monkeypatch.setattr(bot_addons, "addon_active", yes)
    monkeypatch.setattr(core, "master_enabled", yes)
    monkeypatch.setattr(core, "game_enabled", yes)
    monkeypatch.setattr(tenant, "get_or_create_user", user)
    monkeypatch.setattr(R, "free_create", free_create)

    with pytest.raises(SkipHandler):                                    # «تاس» ⇒ بازیِ جایزه (فالبک) — دوئل ساخته نمی‌شود
        await TWR.twr_free_create(_msg("تاس"), types.SimpleNamespace())
    assert created == []
    await TWR.twr_free_create(_msg("بازی تاس"), types.SimpleNamespace())      # «بازی تاس» ⇒ دوئلِ رایگان
    assert created == ["dice"]
    await TWR.twr_free_create(_msg("دوز"), types.SimpleNamespace())           # بقیهٔ بازی‌ها بدونِ تغییر
    assert created == ["dice", "xo"]

    async with _mk() as s:                                              # بدونِ بازیِ جایزهٔ فعال ⇒ «تاس» مثلِ قبل دوئل می‌سازد
        g = (await s.scalars(__import__("sqlalchemy").select(models.DiceGame))).one()
        g.active = False
        await s.commit()
    await TWR.twr_free_create(_msg("تاس"), types.SimpleNamespace())
    assert created == ["dice", "xo", "dice"]
