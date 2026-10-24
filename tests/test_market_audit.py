"""ممیزیِ «ربات اختصاصی» (فروشگاه/تست)، «سرور اختصاصی» و SFTP — PATCH_MARKET_AUDIT_V1."""
import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_market as PM
import sftp_admin as SA

pytestmark = pytest.mark.asyncio


def _proj(title="Twitter DL"):
    return SimpleNamespace(title=title)


async def test_app_names_are_unique_per_user_kind_and_purchase():
    a = PM._app_name(_proj(), 5293876827, True, 1)
    b = PM._app_name(_proj(), 5293976827, True, 2)    # همان 5 رقمِ آخر (76827)
    c = PM._app_name(_proj(), 5293876827, False, 7)
    d = PM._app_name(_proj(), 5293876827, False, 8)
    assert len({a, b, c, d}) == 4
    assert all(len(n) <= 40 and n.replace("-", "").isalnum() and n == n.lower() for n in (a, b, c, d))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(PM, "Session", maker)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=77, full_name="u", balance=0))
        s.add(models.MarketProject(title="P", price=1000, trial_minutes=20, is_active=True))
        await s.commit()
    yield maker
    await engine.dispose()


async def test_trial_claim_is_atomic_and_released_on_failure(db):
    first = await PM._claim_trial(77, 1)
    assert first
    assert await PM._claim_trial(77, 1) is None            # دومین درخواست رد می‌شود
    await PM._release_trial_claim(first)                    # نصب شکست خورد ⇒ دوباره می‌تواند
    assert await PM._claim_trial(77, 1)


async def _mk_app(maker, name, status, paid_until=None):
    async with maker() as s:
        a = models.HostedApp(owner_id=77, name=name, status=status, paid_until=paid_until)
        s.add(a)
        await s.flush()
        pid = a.id
        await s.commit()
    return pid


async def _mk_trial(maker, app_id, expires, ended=None):
    async with maker() as s:
        s.add(models.MarketPurchase(project_id=1, user_telegram_id=77, kind="trial", price_paid=0,
                                    hosted_app_id=app_id, trial_expires_at=expires, trial_ended_at=ended))
        await s.commit()


async def test_expired_trial_is_suspended_and_stopped_immediately(db, monkeypatch):
    stopped = []

    async def stop(app_id):
        stopped.append(app_id)
    monkeypatch.setattr(PM, "_stop_app_now", stop)
    now = dt.datetime.now()
    aid = await _mk_app(db, "t1", "running")
    await _mk_trial(db, aid, now - dt.timedelta(minutes=1))
    assert await PM.expire_market_trials_once(None) == 1
    async with db() as s:
        assert (await s.get(models.HostedApp, aid)).status == "suspended"
    assert stopped == [aid]


async def test_paid_trial_app_is_left_alone(db, monkeypatch):
    stopped = []

    async def stop(app_id):
        stopped.append(app_id)
    monkeypatch.setattr(PM, "_stop_app_now", stop)
    now = dt.datetime.now()
    aid = await _mk_app(db, "t2", "running", paid_until=now + dt.timedelta(days=30))
    await _mk_trial(db, aid, now - dt.timedelta(minutes=1))
    await PM.expire_market_trials_once(None)
    async with db() as s:
        assert (await s.get(models.HostedApp, aid)).status == "running"
    assert stopped == []


async def test_ended_trial_restarted_by_user_is_suspended_again(db, monkeypatch):
    stopped = []

    async def stop(app_id):
        stopped.append(app_id)
    monkeypatch.setattr(PM, "_stop_app_now", stop)
    now = dt.datetime.now()
    aid = await _mk_app(db, "t3", "running")            # تمام شده بود ولی دوباره روشن شده
    await _mk_trial(db, aid, now - dt.timedelta(hours=1), ended=now - dt.timedelta(minutes=30))
    await PM.expire_market_trials_once(None)
    async with db() as s:
        assert (await s.get(models.HostedApp, aid)).status == "suspended"
    assert stopped == [aid]


async def test_stale_pending_trial_claim_is_dropped(db, monkeypatch):
    async with db() as s:
        s.add(models.MarketPurchase(project_id=1, user_telegram_id=77, kind="trial", price_paid=0,
                                    hosted_app_id=PM._VPS_CLAIM_PENDING,
                                    created_at=dt.datetime.now() - dt.timedelta(minutes=30)))
        await s.commit()
    await PM._drop_stale_trial_claims()
    async with db() as s:
        assert (await s.scalar(select(models.MarketPurchase.id))) is None


# ───────────────────────── SFTP ─────────────────────────

class _St:
    async def clear(self):
        pass


def _msg(text):
    sent = []

    async def answer(t, **k):
        sent.append(t)
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=answer), sent


@pytest.fixture
def sftp_db(tmp_path, monkeypatch):
    f = tmp_path / "sftpvip.db"
    con = sqlite3.connect(f)
    con.execute("create table sftp_subscriptions(id integer primary key, telegram_id integer unique,"
                " expires_at text, is_blocked integer default 0, note text)")
    con.commit()
    con.close()
    monkeypatch.setattr(SA, "SFTP_DB", str(f))
    monkeypatch.setattr(SA, "PLATFORM_ADMIN_ID", 1)
    return str(f)


async def test_blocking_a_user_without_subscription_never_creates_unlimited_access(sftp_db):
    m, _ = _msg("555")
    await SA.sftp_block_do(m, _St())                       # مسدود
    row = sqlite3.connect(sftp_db).execute("select expires_at, is_blocked from sftp_subscriptions").fetchone()
    assert row[1] == 1 and row[0] is not None and SA._is_expired(row[0])
    m2, sent = _msg("555")
    await SA.sftp_block_do(m2, _St())                      # رفعِ مسدودی: نباید نامحدود شود
    row = sqlite3.connect(sftp_db).execute("select expires_at, is_blocked from sftp_subscriptions").fetchone()
    assert row[1] == 0 and SA._is_expired(row[0])
    assert "منقضی" in sent[-1]


async def test_unblock_warns_when_subscription_is_unlimited(sftp_db):
    con = sqlite3.connect(sftp_db)
    con.execute("insert into sftp_subscriptions(telegram_id, expires_at, is_blocked) values(9, NULL, 1)")
    con.commit()
    con.close()
    m, sent = _msg("9")
    await SA.sftp_block_do(m, _St())
    assert "نامحدود" in sent[-1]


def test_is_expired_handles_t_separator_and_null():
    assert not SA._is_expired(None)
    past = (dt.datetime.utcnow() - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (dt.datetime.utcnow() + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert SA._is_expired(past) and not SA._is_expired(future)
