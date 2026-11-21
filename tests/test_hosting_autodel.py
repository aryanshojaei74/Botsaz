"""PATCH_HOSTING_AUTODEL_V1 — حذفِ خودکارِ سرورِ فعال‌سازیِ تمام‌شده N روز بعد از پایانِ مهلت."""
import datetime as dt
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb
import platform_hosting_autodel as HA

NOW = dt.datetime(2026, 11, 6, 12, 0)


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
    yield mk
    await eng.dispose()


def _app(**kw):
    base = dict(owner_id=7, runtime="python", mem_mb=512, cpu_percent=50, disk_mb=1024)
    base.update(kw)
    return models.HostedApp(**base)


def test_is_due_rules():
    d3, d1 = NOW - dt.timedelta(days=3), NOW - dt.timedelta(days=1)
    assert HA.is_due(_app(name="a", status="suspended", paid_until=d3), 2, NOW)
    assert not HA.is_due(_app(name="b", status="suspended", paid_until=d1), 2, NOW)              # هنوز ۲ روز نشده
    assert not HA.is_due(_app(name="c", status="running", paid_until=d3), 2, NOW)                # روشن است
    assert not HA.is_due(_app(name="d", status="suspended", paid_until=NOW + dt.timedelta(days=5)), 2, NOW)   # تمدید شده
    assert not HA.is_due(_app(name="e", status="suspended"), 2, NOW)                              # بدونِ مبنایِ زمان
    assert not HA.is_due(_app(name="f", status="suspended", paid_until=d3), 0, NOW)               # خاموش
    # هیچ‌وقت پولی نداده: first_started_at + ۱ ساعتِ رایگان
    assert HA.is_due(_app(name="g", status="suspended", first_started_at=NOW - dt.timedelta(days=3, hours=2)), 2, NOW)
    assert not HA.is_due(_app(name="h", status="suspended", first_started_at=NOW - dt.timedelta(days=2, minutes=30)), 2, NOW)


@pytest.mark.asyncio
async def test_autodel_once_destroys_only_due_and_keeps_failed(db, monkeypatch):
    async with db() as s:
        s.add_all([_app(name="old1", status="suspended", paid_until=NOW - dt.timedelta(days=5)),
                   _app(name="old2", status="suspended", paid_until=NOW - dt.timedelta(days=9)),
                   _app(name="fresh", status="suspended", paid_until=NOW - dt.timedelta(hours=30)),
                   _app(name="live", status="running", paid_until=NOW - dt.timedelta(days=9))])
        await s.commit()
        ids = {a.name: a.id for a in (await s.scalars(models.HostedApp.__table__.select())).all()} if False else None
    await pb.set_pset(HA.KEY, "2")
    calls = []

    async def fake_api(method, path, **kw):
        calls.append(path)
        return (False, "boom") if "old2" in str(kw) or path.endswith("/2/destroy") else (True, {"name": "x"})
    monkeypatch.setattr(pb, "_vip_api", fake_api)

    class B:
        sent = []
        async def send_message(self, chat, text, **k): self.sent.append((chat, text))
    b = B()
    done = await HA.autodel_once(now=NOW, bot=b)
    assert [n for _i, n in done] == ["old1"] and len(calls) == 2           # old1 حذف شد؛ old2 شکست خورد (می‌ماند)
    assert all(p.endswith("/destroy") for p in calls)
    assert any("حذف شد" in t for _c, t in b.sent)                           # مالک خبر گرفت
    await pb.set_pset(HA.KEY, "0")
    calls.clear()
    assert await HA.autodel_once(now=NOW, bot=b) == [] and calls == []        # خاموش ⇒ هیچ‌چیز
