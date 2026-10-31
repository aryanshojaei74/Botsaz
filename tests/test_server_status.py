"""PATCH_SERVER_STATUS_V1 — مینی‌اپِ وضعیتِ سرورها: اندازه‌گیری، جمع‌بندی، مسیرها و نوعِ دکمه."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import server_status as SS
from server_status_page import PAGE


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, SS):
        monkeypatch.setattr(mod, "Session", m)
    SS._CACHE.update(t=0.0, v=None)
    yield m
    await engine.dispose()


async def _seed(m, key, now, oks, ms=120, step=60):
    async with m() as s:
        for i, ok in enumerate(oks):
            s.add(models.StatusSample(ts=now - timedelta(seconds=step * (len(oks) - i)), key=key, ok=ok, ms=ms if ok else None))
        await s.commit()


def test_state_rules():
    assert SS._state([], 100, "x") == "unknown"
    assert SS._state([(True, 100)] * 5, 100, "x") == "up"
    assert SS._state([(True, 100), (False, None), (False, None)], 100, "x") == "down"
    assert SS._state([(True, 100), (False, None)], 100, "x") == "degraded"        # یک خطا هنوز قطع نیست
    assert SS._state([(True, 5000)] * 5, 100, "x") == "degraded"                  # افتِ شدیدِ سرعت
    assert SS._state([(True, 2000)] * 5, 600, "telegram") == "up"                 # آستانهٔ تلگرام بالاتر است


@pytest.mark.asyncio
async def test_report_hides_never_up_components_and_flags_down(maker):
    now = datetime.utcnow()
    await _seed(maker, "platform", now, [True] * 20)
    await _seed(maker, "tenant", now, [True] * 10 + [False] * 3)
    await _seed(maker, "booking", now, [False] * 5)                     # هرگز زنده نبوده ⇒ نمایش داده نمی‌شود
    rep = await SS.build_report(force=True)
    by = {c["key"]: c for c in rep["components"]}
    assert "booking" not in by
    assert by["platform"]["state"] == "up" and by["tenant"]["state"] == "down"
    assert rep["overall"] == "down"
    assert len(by["platform"]["series"]) == 144 and len(by["platform"]["days"]) == 30
    assert by["platform"]["uptime24"] == 100.0 and by["tenant"]["uptime24"] < 100


@pytest.mark.asyncio
async def test_probe_once_stores_one_row_per_component(maker, monkeypatch):
    async def ok(*a, **k): return True, 42
    monkeypatch.setattr(SS, "_probe_http", ok)
    monkeypatch.setattr(SS, "_probe_telegram", ok)
    monkeypatch.setattr(SS, "_probe_db", ok)
    n = await SS.probe_once()
    assert n == len(SS.ORDER)
    await SS.prune()


def test_page_is_self_contained_no_external_resources():
    import re
    assert not re.search(r"(src|href)=[\"']https?://", PAGE)
    assert "googleapis" not in PAGE and "telegram.org" not in PAGE and "cdn" not in PAGE.lower()
    assert "/status/api" in PAGE


def test_server_status_button_type_registered():
    import platform_bot as PB
    assert "server_status" in PB._HCB_TYPE_LABEL
    assert PB._status_url() == "" or PB._status_url().startswith("https://")
