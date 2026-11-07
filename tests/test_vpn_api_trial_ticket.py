"""PATCH_VPN_API_TRIAL_TICKET_V1 (Weirdnet) — دریافتِ تست و ثبت/پیگیری/پاسخِ تیکت از طریقِ API."""
import pytest
from sqlalchemy import select

import models
from tests.test_vpn_api_extended import env, _call, FakeClient, TG   # noqa: F401


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import tenant.shared.vpn_api as A

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(A, "_notify_ticket", _noop)
    A._RATE.clear()


@pytest.mark.asyncio
async def test_trial_disabled_then_created_once(env, monkeypatch):
    import tenant
    monkeypatch.setattr(tenant, "_trial_panel_client", lambda panel: FakeClient())
    assert (await _call(env, "trial"))["error"] == "trial_disabled"
    for k, v in {"trial_enabled": "1", "trial_gb": "2", "trial_days": "3", "trial_source": "panel"}.items():
        await tenant.set_setting(env["bid"], k, v)
    tenant._SETTING_CACHE.clear()
    r = await _call(env, "trial")
    assert r["ok"] and r["service"]["trial"] is True and r["service"]["volume_gb"] == 2 and r["service"]["days"] == 3
    assert FakeClient.last["gb"] == 2 and FakeClient.last["days"] == 3
    assert (await _call(env, "trial"))["error"] == "trial_already_used"          # یک‌بار به‌ازایِ هر کاربر


@pytest.mark.asyncio
async def test_trial_panel_failure_does_not_burn_the_trial(env, monkeypatch):
    import tenant
    monkeypatch.setattr(tenant, "_trial_panel_client", lambda panel: FakeClient())
    for k, v in {"trial_enabled": "1", "trial_gb": "1", "trial_days": "1"}.items():
        await tenant.set_setting(env["bid"], k, v)
    tenant._SETTING_CACHE.clear()
    FakeClient.fail_create = True
    assert (await _call(env, "trial"))["ok"] is False
    FakeClient.fail_create = False
    r2 = await _call(env, "trial")
    assert r2["ok"] is True, r2                                                   # برگشتِ ثبتِ مصرف ⇒ دوباره می‌شود


@pytest.mark.asyncio
async def test_ticket_create_list_view_reply_and_isolation(env):
    r = await _call(env, "ticket", text="کانفیگم وصل نمی‌شود", subject="مشکل اتصال")
    assert r["ok"] and r["ticket"]["id"]
    tid = r["ticket"]["id"]
    lst = await _call(env, "tickets")
    assert lst["count"] == 1 and lst["items"][0]["subject"] == "مشکل اتصال"
    # پاسخِ ادمین (مثلِ ربات) ⇒ در دیدنِ تیکت می‌آید
    async with env["maker"]() as s:
        s.add(models.TenantTicketReply(ticket_id=tid, sender="admin", text="چک شد، درست است"))
        await s.commit()
    v = await _call(env, "tickets", ticket_id=str(tid))
    assert [x["from"] for x in v["ticket"]["replies"]] == ["admin"]
    rr = await _call(env, "ticket_reply", ticket_id=str(tid), text="ممنون")
    assert rr["ok"]
    v = await _call(env, "tickets", ticket_id=str(tid))
    assert [x["from"] for x in v["ticket"]["replies"]] == ["admin", "user"]
    assert (await _call(env, "ticket", text=""))["error"] == "invalid_text"
    assert (await _call(env, "ticket_reply", ticket_id="9999", text="x"))["error"] == "ticket_not_found"
