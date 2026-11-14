"""PATCH_API_RENEW_V1 (Weirdnet): تمدید از راهِ API — همان قیمت/قاعدهٔ داخلِ ربات."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

import models
import tenant
from tests.test_vpn_api_extended import TG, _call, _balance, env  # noqa: F401  (fixture)


class RenewClient:
    calls = []
    fail = False

    async def renew(self, email, days):
        if RenewClient.fail:
            raise RuntimeError("panel down")
        RenewClient.calls.append(("renew", email, days))

    async def add_traffic(self, email, gb):
        RenewClient.calls.append(("gb", email, gb))

    async def close(self):
        pass


@pytest.fixture
async def svc(env, monkeypatch):
    from tenant.shared.panel_clients import panel_pool
    RenewClient.calls, RenewClient.fail = [], False
    monkeypatch.setattr(panel_pool, "get_sync", lambda *a, **k: RenewClient())

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(env["A"], "_notify_sale", _noop)
    for k, v in {"renew_price_day": "1000", "renew_min_days": "30", "renew_max_days": "90", "renew_step_days": "30",
                 "renew_price_gb": "2000", "renew_min_gb": "10", "renew_max_gb": "50", "renew_step_gb": "10"}.items():
        await tenant.set_setting(env["bid"], k, v)
        await tenant.set_setting(env["bid"], f"{k}_sanai", v)
    tenant._SETTING_CACHE.clear()
    async with env["maker"]() as s:
        s.add(models.PanelAccount(bot_id=env["bid"], user_telegram_id=TG, email="vik_1", panel_key="sanai",
                                  volume_gb=10, days=30, status="active", svc_code="111222333",
                                  expires_at=datetime.utcnow() + timedelta(days=5)))
        await s.commit()
    return env


@pytest.mark.asyncio
async def test_renew_info_lists_prices(svc):
    r = await _call(svc, "renew_info", service="111222333")
    assert r["ok"] and r["days_options"] == [30, 60, 90] and r["gb_min"] == 10 and r["gb_max"] == 50
    o = next(x for x in r["options"] if x["days"] == 30 and x["gb"] == 20)
    assert o["price"] == 30 * 1000 + 20 * 2000


@pytest.mark.asyncio
async def test_renew_charges_applies_to_panel_and_updates_service(svc):
    r = await _call(svc, "renew", service="111222333", days="30", gb="20", ref="r1")
    assert r["ok"] and r["charged"] == 70_000 and r["balance"] == 930_000.0 and r["renewed"] == {"days": 30, "gb": 20}
    assert RenewClient.calls == [("renew", "vik_1", 30), ("gb", "vik_1", 20)]
    assert await _balance(svc) == 930_000
    async with svc["maker"]() as s:
        a = await s.scalar(select(models.PanelAccount))
    assert a.volume_gb == 30 and a.days == 60 and a.expires_at > datetime.utcnow() + timedelta(days=34)
    r2 = await _call(svc, "renew", service="111222333", days="30", gb="20", ref="r1")     # تکرارِ ref
    assert r2.get("duplicate") and await _balance(svc) == 930_000 and len(RenewClient.calls) == 2


@pytest.mark.asyncio
async def test_validation_errors_never_charge(svc):
    assert (await _call(svc, "renew", service="111222333", days="7"))["error"] == "invalid_days"
    r = await _call(svc, "renew", service="111222333", days="30", gb="99")
    assert r["error"] == "invalid_gb" and r["max"] == 50
    assert (await _call(svc, "renew", service="nope", days="30"))["error"] == "service_not_found"
    assert (await _call(svc, "renew", service="111222333", days="30", gb="13"))["ok"]       # دلخواهِ درونِ بازه مجاز
    assert await _balance(svc) == 1_000_000 - (30_000 + 13 * 2000)
    assert (await _call(svc, "renew", service="111222333", days="30", plan_id="999"))["error"] == "plan_not_found"


@pytest.mark.asyncio
async def test_panel_failure_refunds_fully(svc):
    RenewClient.fail = True
    r = await _call(svc, "renew", service="111222333", days="30", gb="10")
    assert r["ok"] is False and r["error"] == "panel_error" and r["refunded"] is True
    assert await _balance(svc) == 1_000_000
    async with svc["maker"]() as s:
        a = await s.scalar(select(models.PanelAccount))
    assert a.days == 30 and a.volume_gb == 10                                  # سرویسِ محلی دست‌نخورده


@pytest.mark.asyncio
async def test_insufficient_balance_and_other_users_service(svc):
    async with svc["maker"]() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == TG))
        u.balance = 100
        s.add(models.PanelAccount(bot_id=svc["bid"], user_telegram_id=777, email="other", panel_key="sanai",
                                  volume_gb=1, days=1, status="active", svc_code="999000111"))
        await s.commit()
    r = await _call(svc, "renew", service="111222333", days="30")
    assert r["error"] == "insufficient_balance" and RenewClient.calls == []
    assert (await _call(svc, "renew", service="999000111", days="30"))["error"] == "service_not_found"   # سرویسِ دیگران


@pytest.mark.asyncio
async def test_renew_by_plan_id(svc):
    async with svc["maker"]() as s:
        plan = await s.scalar(select(models.VpnPlan))
    r = await _call(svc, "renew", service="111222333", plan_id=str(plan.id))
    assert r["ok"] and r["charged"] == 100_000 and RenewClient.calls == [("renew", "vik_1", 30), ("gb", "vik_1", 10)]


def test_docs_mention_renew_and_drop_old_note():
    from tenant.shared.public_api import render_vpn_docs_html
    h = render_vpn_docs_html("https://x")
    assert "<code>renew</code>" in h and "<code>renew_info</code>" in h and "هنوز فعال نیست" not in h
