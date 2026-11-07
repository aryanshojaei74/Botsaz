"""PATCH_CANDY_REWARD_V1 — جایزهٔ ۱۰۰ شیرینی: سقفِ روزانه، یک‌بار برایِ هر نفر، سقفِ نفرات، فقط با تمِ هالوین، کیف‌پولِ واقعی."""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import halloween as HW
import models
import platform_candy as C
import platform_group_game as G
import platform_group_money as M


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import platform_bot as PB
    for mod in (C, G, M, PB, HW):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    HW._CACHE.update(v=None, t=0.0)
    async with mk() as s:
        s.add(models.PlatformUser(telegram_id=1, full_name="Ali", balance=0))
        s.add(models.PlatformUser(telegram_id=2, full_name="Sara", balance=500))
        await s.commit()
    await HW.set_on(True, now=datetime.utcnow())
    HW._CACHE.update(v=None, t=0.0)
    yield mk
    await eng.dispose()


async def bal(mk, uid):
    async with mk() as s:
        return int((await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))).balance)


@pytest.mark.asyncio
async def test_defaults_are_7000_toman_for_100_candies_daily_cap_25(env):
    c = await C.cfg()
    assert (c["amount"], c["goal"], c["max"], c["cap"], c["on"]) == (7000, 100, 20, 25, True)


@pytest.mark.asyncio
async def test_daily_cap_blocks_farming_and_resets_next_day(env):
    assert await C.apply_cap(1, 6, "2026-10-30", 25) == 6
    for _ in range(3):
        await C.apply_cap(1, 6, "2026-10-30", 25)                       # مجموع ۲۴
    assert await C.apply_cap(1, 6, "2026-10-30", 25) == 1               # فقط ۱ تا جا مانده
    assert await C.apply_cap(1, 6, "2026-10-30", 25) == 0               # پر شد
    assert await C.used_today(1, "2026-10-30") == 25
    assert await C.apply_cap(1, 6, "2026-10-31", 25) == 6               # روزِ بعد از نو


@pytest.mark.asyncio
async def test_reward_paid_once_to_real_wallet_with_ledger(env):
    mk = env
    assert (await C.try_reward(1, 99))["status"] == "below"
    r = await C.try_reward(1, 100)
    assert r["status"] == "paid" and r["amount"] == 7000 and await bal(mk, 1) == 7000
    assert (await C.try_reward(1, 130))["status"] == "already" and await bal(mk, 1) == 7000      # دوباره نه
    async with mk() as s:
        t = (await s.scalars(select(models.PlatformTxn).where(models.PlatformTxn.user_telegram_id == 1))).all()
    assert len(t) == 1 and t[0].kind == "halloween_reward" and int(t[0].amount) == 7000
    assert await C.paid_count() == 1


@pytest.mark.asyncio
async def test_budget_cap_theme_off_switch_off_and_missing_wallet(env):
    mk = env
    await C.set_numbers(7000, 100, 1, 25)                               # فقط یک نفر
    assert (await C.try_reward(1, 100))["status"] == "paid"
    assert (await C.try_reward(2, 100))["status"] == "budget" and await bal(mk, 2) == 500
    await C.set_numbers(7000, 100, 20, 25)
    assert (await C.try_reward(3, 100))["status"] == "no_user"           # هنوز ربات اصلی را استارت نکرده
    await C.toggle()                                                       # خاموشِ جایزه
    assert (await C.try_reward(2, 100))["status"] == "off"
    await C.toggle()
    await HW.set_on(False, now=datetime.utcnow()); HW._CACHE.update(v=None, t=0.0)
    assert (await C.try_reward(2, 100))["status"] == "off" and await bal(mk, 2) == 500   # تم خاموش ⇒ جایزه نه


@pytest.mark.asyncio
async def test_award_flow_message_cap_note_and_payout(env, monkeypatch):
    mk = env
    await C.set_numbers(7000, 10, 20, 25)                               # هدفِ ۱۰ برایِ تست
    got, total, extra = await G._award(1, "Ali", 6)
    assert (got, total) == (6, 6) and extra == ""
    got, total, extra = await G._award(1, "Ali", 6)
    assert total == 12 and "تبریک" in extra and "7,000" in extra and await bal(mk, 1) == 7000
    await C.set_numbers(7000, 10, 20, 8)                                # سقف ۸ ⇒ دفعهٔ بعد فقط ۲ تا (۱۲ تا الان مصرف شده ⇒ ۰)
    got, total, extra = await G._award(2, "Sara", 6)
    got2, total2, extra2 = await G._award(2, "Sara", 6)
    assert got2 == 2 and "سقفِ شیرینیِ امروزت" in extra2


@pytest.mark.asyncio
async def test_my_candy_command_and_admin_command(env, monkeypatch):
    async def ga(chat_id):
        return chat_id == -100
    monkeypatch.setattr(G, "_group_allowed", ga)
    await G.add_points(2, "Sara", 40)
    out = []

    async def reply(t, **k):
        out.append(t)
    m = NS(text="شیرینی من", chat=NS(id=-100), from_user=NS(id=2, is_bot=False, full_name="Sara"), sender_chat=None, reply=reply)
    assert G._is_myc(m)
    await G.gg_mycandy(m)
    assert "40" in out[-1] and "60" in out[-1] and "7,000" in out[-1]    # ۶۰ تا مانده
    import config
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 77)
    sent = []

    async def answer(t, **k):
        sent.append(t)
    adm = NS(text="/hwreward 9000 120 5 30", from_user=NS(id=77), answer=answer)
    await G.hw_reward_cmd(adm)
    c = await C.cfg()
    assert (c["amount"], c["goal"], c["max"], c["cap"]) == (9000, 120, 5, 30) and "9,000" in sent[-1]
    other = NS(text="/hwreward 1 1 1 1", from_user=NS(id=5), answer=answer)
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await G.hw_reward_cmd(other)
