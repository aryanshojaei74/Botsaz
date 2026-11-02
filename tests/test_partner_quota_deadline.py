"""PATCH_PARTNER_QUOTA_AT_EXPIRY_V1 (فدایی) — حدِ نصاب تا پایانِ روزِ اعتبارِ داده‌شده سنجیده شود، و فعال‌سازیِ دستی دوباره خاموش نشود."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import partner_loops as PL
import partner_rules as PR
import platform_partner_admin as PA


def test_deadline_is_the_later_of_grace_and_end_of_granted_day():
    now = dt.datetime(2026, 11, 2, 9, 0)
    low = now - dt.timedelta(days=60)                         # مهلتِ ۳۰ روزه ۳۰ روز پیش تمام شده
    exp = now + dt.timedelta(days=11, hours=3)
    d = PR.quota_deadline(low, exp)
    assert d == (exp.replace(hour=23, minute=59, second=59, microsecond=0))       # پایانِ روزِ اعتبار
    assert PR.quota_deadline(low, low + dt.timedelta(days=5)) == low + dt.timedelta(days=PR.LOW_STOCK_GRACE_DAYS)   # اعتبارِ زودتر از مهلتِ ۳۰ روزه اثری ندارد
    past = PR.quota_deadline(low, now - dt.timedelta(days=5))
    assert past < now                                                                  # اعتبارِ گذشته ⇒ مهلت هم گذشته (خاموش می‌شود)
    assert PR.quota_deadline(now, now + dt.timedelta(days=3)) == now + dt.timedelta(days=PR.LOW_STOCK_GRACE_DAYS)  # مهلتِ ۳۰ روزه بلندتر است
    assert PR.quota_deadline(None, exp) is None
    assert PR.quota_deadline(low, None) == low + dt.timedelta(days=PR.LOW_STOCK_GRACE_DAYS)


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (config, PL, PA):
        monkeypatch.setattr(mod, "Session", mk, raising=False)

    async def minact(): return 10
    monkeypatch.setattr(PL, "min_active_sales", minact)

    async def term(): return False
    monkeypatch.setattr(PL, "partner_term_enabled", term)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=500, token="1:T", username="pbot", field="general", plan="partner", status="active"))
        await s.commit()
    yield mk
    await eng.dispose()


async def _mk_partner(mk, *, low_days_ago, expires_in_days, status="active"):
    now = dt.datetime.now()
    async with mk() as s:
        s.add(models.PartnerBot(id=7, bot_id=1, owner_id=500, status=status,
                                low_stock_since=now - dt.timedelta(days=low_days_ago) if low_days_ago is not None else None,
                                expires_at=(now + dt.timedelta(days=expires_in_days)) if expires_in_days is not None else None))
        await s.commit()


async def _status(mk):
    async with mk() as s:
        pb = await s.get(models.PartnerBot, 7)
        br = await s.get(models.Bot, 1)
        return pb.status, br.status, pb.low_stock_since


@pytest.mark.asyncio
async def test_partner_with_time_left_is_not_deactivated_for_quota(env):
    await _mk_partner(env, low_days_ago=40, expires_in_days=10)               # مهلتِ ۳۰ روزه گذشته ولی ۱۰ روز وقت دارد
    await PL._run_once(None)
    assert (await _status(env))[:2] == ("active", "active")


@pytest.mark.asyncio
async def test_partner_is_deactivated_after_the_granted_day_ends(env):
    await _mk_partner(env, low_days_ago=40, expires_in_days=-2)               # وقتِ داده‌شده هم تمام شده
    await PL._run_once(None)
    st = await _status(env)
    assert st[0] == "expired" and st[1] == "paused"


@pytest.mark.asyncio
async def test_expiry_later_today_still_counts_until_end_of_day(env):
    now = dt.datetime.now()
    async with env() as s:
        s.add(models.PartnerBot(id=7, bot_id=1, owner_id=500, status="active", low_stock_since=now - dt.timedelta(days=40),
                                expires_at=now - dt.timedelta(minutes=1)))      # امروز، چند لحظه پیش
        await s.commit()
    await PL._run_once(None)
    exp_today_end = now.replace(hour=23, minute=59, second=59)
    if dt.datetime.now() < exp_today_end:                                       # هنوز «پایانِ روز» نرسیده
        assert (await _status(env))[0] == "active"


@pytest.mark.asyncio
async def test_manual_resume_gives_a_fresh_quota_window(env):
    await _mk_partner(env, low_days_ago=40, expires_in_days=-2, status="expired")
    owner = await PA._set_partner_status(7, "active")
    assert owner == 500
    pb_status, bot_status, low = await _status(env)
    assert pb_status == "active" and bot_status == "active" and low is None     # مهلتِ قدیمی پاک شد
    await PL._run_once(None)                                                    # دورِ بعدِ حلقه: دوباره خاموش نمی‌شود
    assert (await _status(env))[:2] == ("active", "active")


@pytest.mark.asyncio
async def test_pa_days_revives_an_expired_partner_when_time_is_added(env, monkeypatch):
    await _mk_partner(env, low_days_ago=40, expires_in_days=-1, status="expired")
    async with env() as s:
        (await s.get(models.Bot, 1)).status = "paused"
        await s.commit()

    async def admin_only(cb): return True
    monkeypatch.setattr(PA, "_admin_only", admin_only)
    monkeypatch.setattr(PA, "_self_dealing", lambda uid, pb: False)

    async def detail(pb_id): return {"pb_id": pb_id, "bot_id": 1, "owner": 500, "username": "p", "bot_status": "active",
                                      "status": "active", "discount": 30, "expires_at": None, "low_since": None, "stock_g": 0,
                                      "stock_p": 0, "bought_g": 0, "bought_p": 0, "sold": 0, "trials": 0, "active": 0,
                                      "revenue": 0, "paid": 0, "users": 0, "owner_name": "x", "owner_balance": 0}
    monkeypatch.setattr(PA, "_partner_detail", detail)
    monkeypatch.setattr(PA, "_detail_kb", lambda d: None)

    class Msg:
        async def edit_text(self, *a, **k): return None

    class Cb:
        data = "pa:days:7:7"
        message = Msg()
        from_user = type("U", (), {"id": 1})()
        answers = []

        async def answer(self, text=None, **k): Cb.answers.append(text)
    await PA.pa_days(Cb())
    pb_status, bot_status, _low = await _status(env)
    assert pb_status == "active" and bot_status == "active"                     # قبلاً با «➕ ۷» expired می‌ماند
