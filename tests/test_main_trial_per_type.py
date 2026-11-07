"""PATCH_MAIN_TRIAL_PER_TYPE_V1 — تستِ ربات اصلی برایِ هر نوعِ ربات یک‌بار (به‌جایِ یک تست در کلِ عمر)."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import plan_trials as PT

UID = 77


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
        await c.run_sync(PT.PlanTrialRequest.__table__.create, checkfirst=True)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (config, PT):
        monkeypatch.setattr(mod, "Session", mk)
    monkeypatch.setattr(PT, "_table_ready", True)
    monkeypatch.setattr(PT, "trial_types", lambda: ("giveaway", "escrow_bot", "panel_seller"))
    async with mk() as s:
        for i, f in ((11, "giveaway"), (12, "escrow_bot"), (13, "panel_seller")):
            s.add(models.Bot(id=i, owner_id=UID, token=f"{i}:T", username=f"b{i}", field=f, plan="growth", status="active"))
        s.add(models.Bot(id=1, owner_id=5, token="1:P", username="pp", field="partner", plan="partner", status="active"))
        await s.flush()
        s.add(models.PartnerBot(id=5, bot_id=1, owner_id=5, status="active"))
        await s.commit()
    yield mk
    await eng.dispose()


async def take_trial(bot_id: int, plan="growth"):
    st, rid = await PT.create_request(UID, "u", plan)
    assert st == "ok", st
    assert await PT.approve_request(rid, 2)
    await PT.consume_trial(rid, bot_id)
    return rid


@pytest.mark.asyncio
async def test_second_trial_for_other_type_allowed_same_type_blocked(env):
    await take_trial(11)                                            # giveaway
    assert await PT.tried_fields(UID) == {"giveaway"}
    assert await PT.trial_blocked_for_field(UID, "giveaway") and not await PT.trial_blocked_for_field(UID, "escrow_bot")
    assert not await PT.trial_blocked_for_field(UID, None)
    await take_trial(12)                                            # تستِ دومِ نوعِ دیگر مجاز (قبلاً already_used)
    assert await PT.tried_fields(UID) == {"giveaway", "escrow_bot"}


@pytest.mark.asyncio
async def test_unconsumed_active_trial_still_blocks_new_request(env):
    st, rid = await PT.create_request(UID, "u", "pro")
    await PT.approve_request(rid, 2)
    assert (await PT.create_request(UID, "u", "pro"))[0] == "exists_active"


@pytest.mark.asyncio
async def test_total_cap_equals_number_of_types(env):
    for b in (11, 12, 13):
        await take_trial(b)
    assert await PT.trial_count(UID) == 3
    assert (await PT.create_request(UID, "u", "growth"))[0] == "already_used"
    assert (await PT.create_request(999, "other", "growth"))[0] == "ok"           # کاربرِ دیگر


@pytest.mark.asyncio
async def test_unused_trials_cannot_be_farmed(env):
    for _ in range(3):                                              # تایید ولی نساختنِ ربات ⇒ سقفِ کل
        st, rid = await PT.create_request(UID, "u", "growth")
        assert st == "ok"
        await PT.approve_request(rid, 2)
        async with env() as s:
            r = await s.get(PT.PlanTrialRequest, rid)
            r.status, r.expires_at = "expired", dt.datetime.now() - dt.timedelta(days=1)
            await s.commit()
    assert (await PT.create_request(UID, "u", "growth"))[0] == "already_used"


@pytest.mark.asyncio
async def test_partner_trial_counts_for_its_type(env):
    async with env() as s:
        s.add(models.PartnerSale(partner_bot_id=5, sold_bot_id=13, buyer_telegram_id=UID, plan="growth", price=0, kind="trial"))
        await s.commit()
    assert await PT.tried_fields(UID) == {"panel_seller"} and await PT.trial_count(UID) == 1
    assert (await PT.create_request(UID, "u", "growth"))[0] == "ok"


def test_platform_bot_enforces_per_type_and_auto_approve_default():
    import inspect
    import platform_bot as PB
    src = inspect.getsource(PB)
    assert src.count("trial_blocked_for_field") >= 2                 # لیستِ پلن‌ها + choose_plan
    assert 'get_pset("trial_auto_approve", "off")' not in src
    assert "هر نوعِ ربات یک‌بار" in inspect.getsource(PB.trial_request_start)
