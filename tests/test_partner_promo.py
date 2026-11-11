"""PATCH_PARTNER_PROMO_V1 — جشنوارهٔ خرید عمده: به‌ازای هر N ربات M ربات هدیه، تجمعی، فقط در بازهٔ زمانی."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import partner_promo as PP

T0 = dt.datetime(2026, 11, 12, 10, 0)
START, END = dt.datetime(2026, 11, 12, 0, 0), dt.datetime(2026, 11, 20, 0, 0)


def test_bonus_units_is_cumulative_and_never_double_counts():
    assert PP.bonus_units(0, 2, 2, 1) == 1
    assert PP.bonus_units(0, 1, 2, 1) == 0
    assert PP.bonus_units(1, 1, 2, 1) == 1           # دومین خریدِ تکی هدیه را کامل می‌کند
    assert PP.bonus_units(2, 1, 2, 1) == 0           # ۳ تا ⇒ هنوز فقط یک هدیه
    assert PP.bonus_units(0, 5, 2, 1) == 2
    assert PP.bonus_units(0, 6, 3, 2) == 4
    assert PP.bonus_units(0, 5, 0, 1) == 0 and PP.bonus_units(0, 5, 2, 0) == 0 and PP.bonus_units(0, 0, 2, 1) == 0


def test_is_active_window():
    cfg = {"on": True, "start": START, "end": END}
    assert PP.is_active(cfg, T0)
    assert not PP.is_active(cfg, START - dt.timedelta(seconds=1))
    assert not PP.is_active(cfg, END)                          # پایان ناشامل
    assert not PP.is_active({**cfg, "on": False}, T0)
    assert not PP.is_active({**cfg, "end": None}, T0)


def test_parse_jalali_datetime():
    d = PP.parse_jalali_datetime("1405/08/25 18:30")
    assert d is not None and (d.hour, d.minute) == (18, 30)
    assert PP.parse_jalali_datetime("۱۴۰۵-۰۸-۲۵ ۱۸:۳۰") == d
    assert PP.parse_jalali_datetime("1405/08/25").hour == 0
    for bad in ("", "abc", "1405/13/01", "1405/02/40", "25/08/1405"):
        assert PP.parse_jalali_datetime(bad) is None, bad


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(PP, "Session", mk)
    return mk


async def _cfg(mk, **kw):
    base = {"ppromo_on": "1", "ppromo_start": START.strftime("%Y-%m-%dT%H:%M"), "ppromo_end": END.strftime("%Y-%m-%dT%H:%M"),
            "ppromo_pro_buy": "2", "ppromo_pro_free": "1", "ppromo_growth_buy": "0", "ppromo_growth_free": "0"}
    base.update(kw)
    for k, v in base.items():
        await PP.set_value(k, v)


async def _order(mk, pid, g, p, when, status="approved"):
    async with mk() as s:
        o = models.PartnerStockOrder(partner_bot_id=pid, qty_growth=g, qty_pro=p, amount=1, method="card",
                                     status=status, created_at=when)
        s.add(o)
        await s.commit()
        return o.id


@pytest.mark.asyncio
async def test_two_pro_gives_one_free_and_singles_accumulate(env):
    await _cfg(env)
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 0, 2, T0) == (0, 1)
        assert await PP.bonus_for_order(s, 1, 0, 1, T0) == (0, 0)
    await _order(env, 1, 0, 1, T0)                                   # یک خریدِ تأییدشده در بازه
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 0, 1, T0) == (0, 1)      # دومی هدیه را کامل می‌کند
        assert await PP.bonus_for_order(s, 2, 0, 1, T0) == (0, 0)      # همکارِ دیگر جدا حساب می‌شود


@pytest.mark.asyncio
async def test_orders_outside_window_or_not_approved_do_not_count(env):
    await _cfg(env)
    await _order(env, 1, 0, 1, START - dt.timedelta(days=1))           # قبل از شروع
    await _order(env, 1, 0, 1, T0, status="pending")                   # هنوز تأیید نشده
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 0, 1, T0) == (0, 0)


@pytest.mark.asyncio
async def test_outside_window_or_off_gives_nothing_and_growth_rule(env):
    await _cfg(env, ppromo_growth_buy="3", ppromo_growth_free="1")
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 3, 2, T0) == (1, 1)      # رشد ۳⇒۱ و حرفه‌ای ۲⇒۱
        assert await PP.bonus_for_order(s, 1, 3, 2, END + dt.timedelta(hours=1)) == (0, 0)
    await PP.set_value("ppromo_on", "0")
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 3, 2, T0) == (0, 0)


@pytest.mark.asyncio
async def test_exclude_current_order(env):
    await _cfg(env)
    oid = await _order(env, 1, 0, 2, T0)
    async with env() as s:
        assert await PP.bonus_for_order(s, 1, 0, 2, T0, exclude_order_id=oid) == (0, 1)   # خودش دو بار شمرده نمی‌شود


def test_bonus_text_and_rule_text():
    assert PP.bonus_text(0, 0) == ""
    t = PP.bonus_text(1, 2)
    assert "حرفه‌ای: +2" in t and "رشد: +1" in t
    r = PP.rule_text({"pro_buy": 2, "pro_free": 1, "growth_buy": 0, "growth_free": 0})
    assert "هر 2 ربات حرفه‌ای" in r and "رشد" not in r
