"""PATCH_REFPOINT_PRIZES_V1 — گیفت/استارز به‌عنوانِ جایزهٔ امتیازِ رفرال + فهرستِ جایزه‌ها (انتخابِ کاربر)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.shared.ref_points as RP


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, RP):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=500, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=1001, full_name="Ali", username="ali"))
        await s.commit()
    yield maker
    await engine.dispose()


class FakeBot:
    token = "1:T"

    def __init__(self, fail_gift=False):
        self.sent, self.gifts, self.fail_gift = [], [], fail_gift

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text, k.get("reply_markup")))

    async def send_gift(self, user_id, gift_id, **k):
        if self.fail_gift:
            raise RuntimeError("BALANCE_TOO_LOW")
        self.gifts.append((user_id, gift_id))


class Msg:
    def __init__(self):
        self.out = []

    async def answer(self, text, reply_markup=None, **k):
        self.out.append((text, reply_markup))


class Cb:
    def __init__(self, data, uid):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=uid), Msg(), []
        self.message.edit_reply_markup = self._noop

    async def _noop(self, **k):
        pass

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)


async def _user(maker):
    async with maker() as s:
        return await s.get(models.BotUser, 1)


@pytest.mark.asyncio
async def test_pool_with_two_prizes_lets_user_choose_exactly_one(env):
    maker = env
    bot = FakeBot()
    u = await _user(maker)
    await RP._set(1, "refpoint_reward_kind", "text")
    await RP._set(1, "refpoint_reward_text", "جایزهٔ الف")
    pool = [{"label": "متن الف", "cfg": await RP.snapshot_current_cfg(1)}]
    await RP._set(1, "refpoint_reward_text", "جایزهٔ ب")
    pool.append({"label": "متن ب", "cfg": await RP.snapshot_current_cfg(1)})
    await RP._pool_set(1, pool)
    m = Msg()
    assert await RP.deliver_reward(bot, 1, u, m) is True
    kb = m.out[0][1]
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert len(cbs) == 2 and cbs[0].startswith("rpz:pick:")
    await RP._set(1, "refpoint_reward_text", "تغییرِ بعدیِ ادمین")        # نباید روی جایزهٔ فهرست اثر بگذارد
    c1 = Cb(cbs[1], 1001)
    await RP.rpz_pick(c1, bot)
    assert any("جایزهٔ ب" in t for t, _ in c1.message.out)
    c2 = Cb(cbs[0], 1001)                                                    # انتخابِ دوباره رد می‌شود
    await RP.rpz_pick(c2, bot)
    assert any("قبلاً" in a for a in c2.alerts) and not c2.message.out
    c3 = Cb(cbs[0], 999)                                                     # کاربرِ دیگر
    await RP.rpz_pick(c3, bot)
    assert c3.alerts and not c3.message.out


@pytest.mark.asyncio
async def test_single_pool_entry_is_delivered_directly(env):
    maker = env
    u = await _user(maker)
    await RP._set(1, "refpoint_reward_kind", "text")
    await RP._set(1, "refpoint_reward_text", "فقط یکی")
    await RP._pool_set(1, [{"label": "x", "cfg": await RP.snapshot_current_cfg(1)}])
    await RP._set(1, "refpoint_reward_text", "عوض‌شده")
    m = Msg()
    assert await RP.deliver_reward(FakeBot(), 1, u, m)
    assert "فقط یکی" in m.out[0][0]


@pytest.mark.asyncio
async def test_gift_manual_notifies_admin_and_done_informs_user(env):
    maker = env
    bot = FakeBot()
    u = await _user(maker)
    await RP._set(1, "refpoint_reward_kind", "gift")
    await RP._set(1, "refpoint_reward_gift_id", "g1")
    await RP._set(1, "refpoint_reward_gift_stars", "50")
    m = Msg()
    assert await RP.deliver_reward(bot, 1, u, m)
    assert any(c == 500 and "ارسالِ دستی" in t for c, t, _ in bot.sent)             # ادمین خبردار شد
    async with maker() as s:
        c = (await s.scalars(select(models.RefPrizeClaim))).first()
        assert c.status == "pending" and c.kind == "gift"
    cb = Cb(f"rpg:done:{c.id}", 500)
    await RP.rpg_done(cb, bot)
    async with maker() as s:
        assert (await s.get(models.RefPrizeClaim, c.id)).status == "done"
    assert any(chat == 1001 and "ارسال شد" in t for chat, t, _ in bot.sent)
    cb2 = Cb(f"rpg:done:{c.id}", 500)
    await RP.rpg_done(cb2, bot)
    assert any("قبلاً" in a for a in cb2.alerts)


@pytest.mark.asyncio
async def test_gift_auto_sends_or_falls_back_to_manual(env):
    maker = env
    u = await _user(maker)
    await RP._set(1, "refpoint_reward_kind", "gift")
    await RP._set(1, "refpoint_reward_gift_id", "g1")
    await RP._set(1, "refpoint_reward_delivery", "auto")
    ok_bot = FakeBot()
    assert await RP.deliver_reward(ok_bot, 1, u, Msg()) and ok_bot.gifts == [(1001, "g1")]
    bad_bot = FakeBot(fail_gift=True)                                          # استارزِ ربات کم است
    m = Msg()
    assert await RP.deliver_reward(bad_bot, 1, u, m)
    assert not bad_bot.gifts and any(c == 500 for c, _t, _k in bad_bot.sent)    # به ادمین رفت


@pytest.mark.asyncio
async def test_stars_prize_is_manual_with_amount(env):
    maker = env
    bot = FakeBot()
    u = await _user(maker)
    await RP._set(1, "refpoint_reward_kind", "stars")
    await RP._set(1, "refpoint_reward_stars", "150")
    assert await RP.deliver_reward(bot, 1, u, Msg())
    assert any("150" in t and "ارسالِ دستی" in t for _c, t, _k in bot.sent)
    await RP._set(1, "refpoint_reward_stars", "0")
    m = Msg()
    assert await RP.deliver_reward(bot, 1, u, m) is False
