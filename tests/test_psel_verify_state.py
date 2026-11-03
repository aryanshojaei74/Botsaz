"""PATCH_PSEL_VERIFY_STATE_V1 (Whale) — «ربات می‌گوید غیرفعال شد ولی در پنل هنوز فعال است»: بعد از هر خاموش/روشن، وضعیتِ واقعی خوانده و اصلاح می‌شود."""
import pytest

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import manage as MG
from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI as _Base, _Cb, _St, env, ui, _sell  # noqa: F401
from tests.test_psel_manage import FC, _user, mg  # noqa: F401


class SilentPanel:
    """پنلی که اندپوینتِ دسته‌جمعی را «بی‌صدا» می‌پذیرد ولی اثری ندارد؛ تغییرِ تکیِ کاربر کار می‌کند (یا نمی‌کند)."""

    def __init__(self, statuses, per_user_works=True):
        self.rows = [{"username": n, "status": s, "admin": {"username": "adm"}} for n, s in statuses.items()]
        self.per_user_works = per_user_works
        self.calls = []

    async def _request(self, method, path, **kw):
        self.calls.append((method, path))
        if method == "GET":
            return {"users": self.rows, "total": len(self.rows)}
        if method == "PUT" and self.per_user_works:
            name = path.rsplit("/", 1)[-1]
            for r in self.rows:
                if r["username"] == name:
                    r["status"] = kw["json"]["status"]
        return {}


class A(AD.PanelAdminAPI):
    async def user_count(self, username):
        return len(self.c.rows)


@pytest.mark.asyncio
async def test_ensure_fixes_users_the_bulk_call_silently_ignored():
    p = SilentPanel({"a": "active", "b": "active", "c": "disabled"})
    assert await A(p).ensure_users_state("adm", False) == 0                     # ⇒ a و b تکی خاموش شدند
    assert {r["username"]: r["status"] for r in p.rows} == {"a": "disabled", "b": "disabled", "c": "disabled"}
    assert await A(p).ensure_users_state("adm", True) == 0
    assert all(r["status"] == "active" for r in p.rows)


@pytest.mark.asyncio
async def test_ensure_reports_remaining_when_panel_refuses_and_none_when_unverifiable():
    p = SilentPanel({"a": "active", "b": "active"}, per_user_works=False)
    assert await A(p).ensure_users_state("adm", False) == 2
    p2 = SilentPanel({"a": "active"})
    p2.rows[0]["admin"] = {"username": "someone_else"}                         # فیلتر معتبر نیست
    assert await A(p2).ensure_users_state("adm", False) is None


class VApi(_Base):
    remaining = 0

    async def ensure_users_state(self, u, enabled):
        return VApi.remaining


@pytest.mark.asyncio
async def test_service_reports_partial_and_refuses_to_mark_suspended_when_users_stay_on(env):
    aid, uname = await _sell(env)
    VApi.remaining = 3
    r = await SV.set_users_enabled(7, aid, None, False, api_factory=VApi)
    assert r == {"status": "partial", "remaining": 3}
    assert await SV.suspend(aid, "owner", VApi) is False
    async with env() as s:
        from models import PanelSellerAccount
        assert (await s.get(PanelSellerAccount, aid)).status == "active"          # معلق «نشد»
    VApi.remaining = 0
    assert (await SV.set_users_enabled(7, aid, None, False, api_factory=VApi))["status"] == "ok"
    assert (await SV.set_users_enabled(7, aid, None, True, api_factory=_Base))["status"] == "ok"      # تأییدنشد/بدونِ تأیید ⇒ مثلِ قبل
    assert await SV.suspend(aid, "owner", VApi) is True


@pytest.mark.asyncio
async def test_manage_toggle_tells_the_truth_when_panel_did_not_apply(mg, monkeypatch):
    orig = FC.set_enabled

    async def noop(self, n, on):
        FC.log.append(("enabled-ignored", n, on))
    monkeypatch.setattr(FC, "set_enabled", noop)
    cb = _Cb("psel:mgt:3:user05", uid=500)
    await MG.mg_toggle(cb, None)
    assert "اعمال نکرد" in cb.alerts[-1] and FC.users["user05"]["status"] == "active"
    monkeypatch.setattr(FC, "set_enabled", orig)
    await MG.mg_toggle(_Cb("psel:mgt:3:user05", uid=500), None)
    assert FC.users["user05"]["status"] == "disabled"
