"""PATCH_PSEL_MAX_USERS_ENFORCE_V1 (Whale) — سقفِ تعدادِ کاربرِ بسته/تست واقعاً اعمال می‌شود، نه فقط هشدار."""
from datetime import datetime

import pytest

from tests.test_panel_seller import FakeAPI as _Base, env, _sell  # noqa: F401
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller import adapter as AD


class API(_Base):
    rows: list | None = None
    off: list = []

    async def list_user_rows(self, u, expected=None):
        return API.rows

    async def set_user_status(self, name, status):
        API.off.append((name, status))


def _rows(n, owner=None, status="active"):
    out = []
    for i in range(n):
        r = {"username": f"u{i}", "status": status, "created_at": f"2026-11-0{1 + i % 9}T10:00:0{i % 10}"}
        if owner:
            r["admin"] = {"username": owner}
        out.append(r)
    return out


@pytest.mark.asyncio
async def test_extra_users_beyond_cap_are_disabled_oldest_kept(env):
    aid, uname = await _sell(env)
    async with env() as s:                                     # سقفِ ۳ کاربر
        from models import PanelSellerAccount
        a = await s.get(PanelSellerAccount, aid)
        a.max_users = 3
        await s.commit()
    _Base.users, API.rows, API.off = 5, _rows(5, owner=uname), []
    ev = await SV.enforce_once(7, API, datetime.utcnow())
    assert [x[0] for x in API.off] == ["u3", "u4"] and all(x[1] == "disabled" for x in API.off)     # دو تازه‌تر
    assert any(tg == 1001 and "خاموش شد" in t and "3" in t for tg, t in ev) and any(tg == 0 for tg, _ in ev)
    # دورِ بعد: همان وضعیت ⇒ پیامِ تکراری نمی‌رود
    API.off = []
    ev2 = await SV.enforce_once(7, API, datetime.utcnow())
    assert not [1 for tg, t in ev2 if "سقفِ" in t]


@pytest.mark.asyncio
async def test_unverifiable_listing_never_disables_anything_only_warns(env):
    aid, uname = await _sell(env)
    async with env() as s:
        from models import PanelSellerAccount
        a = await s.get(PanelSellerAccount, aid)
        a.max_users = 3
        await s.commit()
    # ردیفی که مالکش ادمینِ دیگری است ⇒ فیلتر معتبر نیست
    _Base.users, API.rows, API.off = 5, None, []                 # آداپتر «تأییدنشده» برگرداند
    ev = await SV.enforce_once(7, API, datetime.utcnow())
    assert API.off == [] and any("بیشتر است" in t for tg, t in ev if tg == 1001)
    # بدونِ فیلدِ مالک و تعدادِ نامتطابق
    async with env() as s:
        from models import PanelSellerAccount
        a = await s.get(PanelSellerAccount, aid)
        a.flags = None
        await s.commit()
    API.rows = None                                              # آداپتر «تأییدنشده» برگرداند (بدونِ مالک و تعدادِ نامتطابق)
    await SV.enforce_once(7, API, datetime.utcnow())
    assert API.off == []


@pytest.mark.asyncio
async def test_adapter_listing_validation_and_status_call():
    calls = []

    class C:
        async def _request(self, method, path, **kw):
            calls.append((method, path, kw))
            if method == "GET":
                return {"users": [{"username": "a", "admin": {"username": "Reseller1"}}], "total": 1}
    api = AD.PanelAdminAPI(C())
    assert [r["username"] for r in await api.list_user_rows("reseller1")] == ["a"]       # بدونِ حساسیت به حروف
    assert calls[0][2]["params"]["admin"] == "reseller1"
    await api.set_user_status("a b", "disabled")
    assert calls[-1][:2] == ("PUT", "/api/user/a%20b") and calls[-1][2]["json"] == {"status": "disabled"}

    class C2:
        async def _request(self, *a, **k):
            return {"users": [{"username": "a", "admin": {"username": "other"}}], "total": 1}
    assert await AD.PanelAdminAPI(C2()).list_user_rows("reseller1") is None
