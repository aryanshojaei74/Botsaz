"""🎃 تم هالوین: کلیدِ سراسری با مهلتِ خودکار، بازیِ روزی‌یک‌بار، جدول، هوکِ /start و رنگِ مینی‌اپ (مالکِ رنگ‌گذار دست‌نخورده)."""
import random
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import halloween as HW
import models


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(HW, "Session", mk)
    HW._CACHE.update(v=None, t=0.0)
    async with mk() as s:
        s.add(models.Bot(id=5, owner_id=9, token="5:T", username="b", field="vpn", plan="pro", status="active"))
        for uid, nm in ((11, "Ali Reza"), (12, "Sara"), (13, "Omid")):
            s.add(models.BotUser(bot_id=5, telegram_id=uid, full_name=nm, balance=0))
        await s.commit()
    yield mk
    await eng.dispose()


NOW = datetime(2026, 10, 30, 10, 0)


@pytest.mark.asyncio
async def test_switch_expires_automatically_and_off_is_instant(env):
    assert await HW.is_on(NOW) is False
    await HW.set_on(True, days=7, now=NOW)
    assert await HW.is_on(NOW + timedelta(days=3)) is True
    assert await HW.is_on(NOW + timedelta(days=8)) is False          # خودکار خاموش
    await HW.set_on(False, now=NOW)
    assert await HW.is_on(NOW + timedelta(days=1)) is False


@pytest.mark.asyncio
async def test_one_play_per_tehran_day_and_candies_accumulate(env):
    rnd = random.Random(1)
    r1 = await HW.play(5, 11, NOW, rnd)
    assert r1["played"] and r1["candies"] == r1["gain"]
    r2 = await HW.play(5, 11, NOW + timedelta(hours=2), rnd)
    assert r2["played"] is False and r2["candies"] == r1["candies"]    # همان روز دوباره نه
    r3 = await HW.play(5, 11, NOW + timedelta(days=1), rnd)
    assert r3["played"] and r3["candies"] == r1["candies"] + r3["gain"]


@pytest.mark.asyncio
async def test_tehran_midnight_boundary(env):
    late = datetime(2026, 10, 30, 20, 0)          # ۲۳:۳۰ تهران
    await HW.play(5, 12, late, random.Random(2))
    assert (await HW.play(5, 12, late + timedelta(minutes=29), random.Random(2)))["played"] is False
    assert (await HW.play(5, 12, late + timedelta(minutes=31), random.Random(2)))["played"] is True   # بعد از نیمه‌شب تهران


def test_outcome_weights_sum_and_no_money_prize():
    assert sum(o[0] for o in HW.OUTCOMES) == 100
    seen = {HW.pick_outcome(random.Random(i))[2] for i in range(300)}
    assert len(seen) == 4 and all(o[1] <= 10 for o in HW.OUTCOMES)


@pytest.mark.asyncio
async def test_leaderboard_sorted_first_names_only(env):
    for uid, gain in ((11, 4), (12, 10), (13, 2)):
        async with env() as s:
            s.add(models.Setting(bot_id=5, key=f"hw:u:{uid}", value=f"{gain}|2026-10-30|1"))
            await s.commit()
    top = await HW.leaderboard(5)
    assert [t[0] for t in top] == [12, 11, 13] and top[1][2] == "Ali"       # فقط نامِ کوچک
    st = await HW.stats()
    assert st["players"] == 3 and st["bots"] == 1 and st["plays"] == 3


@pytest.mark.asyncio
async def test_start_hook_only_when_on_and_not_played_today(env):
    sent = []

    async def answer(t, **k):
        sent.append((t, k))
    msg = NS(from_user=NS(id=11), answer=answer)
    await HW.start_hook(msg, None, NS(id=5))
    assert sent == []                                                        # خاموش ⇒ هیچ
    await HW.set_on(True, now=datetime.utcnow())
    HW._CACHE.update(v=None, t=0.0)
    await HW.start_hook(msg, None, NS(id=5))
    assert len(sent) == 1 and "Trick or Treat" in sent[0][1]["reply_markup"].inline_keyboard[0][0].text
    await HW.play(5, 11)
    await HW.start_hook(msg, None, NS(id=5))
    assert len(sent) == 1                                                    # امروز بازی کرده ⇒ دعوت نه


@pytest.mark.asyncio
async def test_miniapp_theme_respects_owner_colors(env, monkeypatch):
    from tenant.shared import miniapp as MA
    import tenant
    store = {}

    async def gs(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    assert await MA._miniapp_bg(5) == ""
    await HW.set_on(True, now=datetime.utcnow())
    HW._CACHE.update(v=None, t=0.0)
    assert await MA._miniapp_bg(5) == HW.THEME_BG and await MA._miniapp_card(5) == HW.THEME_CARD
    store["miniapp_bg_color"] = "#112233"                                   # مالک رنگ گذاشته ⇒ دست‌نخورده
    assert await MA._miniapp_bg(5) == "#112233"


@pytest.mark.asyncio
async def test_platform_panel_toggle_is_admin_only(env, monkeypatch):
    import platform_halloween as PH
    import platform_bot as PB
    monkeypatch.setattr(PB, "Session", env)
    monkeypatch.setattr(PH, "PLATFORM_ADMIN_ID", 1)
    out = {}

    async def answer(*a, **k):
        out["a"] = (a, k)

    async def edit_text(t, reply_markup=None):
        out["t"], out["kb"] = t, reply_markup
    cb = NS(data="adm:halloween:on", from_user=NS(id=2), answer=answer, message=NS(edit_text=edit_text))
    await PH.adm_halloween(cb)
    assert out["a"][1].get("show_alert") and await HW.is_on() is False        # غیرمدیر ⇒ رد
    cb.from_user.id = 1
    HW._CACHE.update(v=None, t=0.0)
    await PH.adm_halloween(cb)
    HW._CACHE.update(v=None, t=0.0)
    assert await HW.is_on() is True and "روشن" in out["t"]
    assert any((b.callback_data or "") == "adm:halloween:off" for r in out["kb"].inline_keyboard for b in r)
