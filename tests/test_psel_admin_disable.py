"""PATCH_PSEL_ADMIN_DISABLE_V1 (Whale) — «پنلِ تست منقضی و خاموش شد ولی غیرفعالش نمی‌کند»: خودِ ادمین در پاسارگاردِ جدید غیرفعال می‌شود."""
import pytest

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI as _Base, env, _sell  # noqa: F401


class P:
    """کلاینتِ ساختگیِ پاسارگاردِ جدید: GET /api/admins ⇒ {"admins": [...]}؛ PUT /api/admin/{u} ⇒ is_disabled را می‌نویسد."""

    def __init__(self, admin_has_flag=True, apply=True, new_style=True):
        self.admin = {"username": "adm", "used_traffic": 0, "total_users": 1}
        if admin_has_flag:
            self.admin["is_disabled"] = False
        self.apply, self.new_style, self.calls = apply, new_style, []

    async def _request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if method == "GET" and path == "/api/admins":
            return {"admins": [self.admin], "total": 1} if self.new_style else [self.admin]
        if method == "PUT" and path == "/api/admin/adm" and self.apply and "is_disabled" in self.admin:
            self.admin["is_disabled"] = kw["json"]["is_disabled"]
        return {}


@pytest.mark.asyncio
async def test_adapter_disables_and_verifies_on_new_style_panel():
    p = P()
    assert await AD.PanelAdminAPI(p).set_admin_disabled("adm", True) is True
    assert ("PUT", "/api/admin/adm", {"is_disabled": True}) in p.calls and p.admin["is_disabled"] is True
    assert await AD.PanelAdminAPI(p).set_admin_disabled("adm", False) is True and p.admin["is_disabled"] is False


@pytest.mark.asyncio
async def test_adapter_reports_false_when_panel_ignored_it_and_none_when_unsupported():
    assert await AD.PanelAdminAPI(P(apply=False)).set_admin_disabled("adm", True) is False       # پنل نپذیرفت
    assert await AD.PanelAdminAPI(P(admin_has_flag=False)).set_admin_disabled("adm", True) is None   # فیلد را نمی‌شناسد
    old = P(new_style=False)
    assert await AD.PanelAdminAPI(old).set_admin_disabled("adm", True) is None                     # مرزبانِ قدیمی
    assert not any(c[0] == "PUT" for c in old.calls)                                              # دست نمی‌زند


class DApi(_Base):
    admin_calls: list = []

    async def set_admin_disabled(self, u, disabled):
        DApi.admin_calls.append((u, disabled))
        return True


@pytest.mark.asyncio
async def test_expiry_suspend_disables_admin_resume_and_renew_enable_it_traffic_suspend_keeps_admin_open(env):
    DApi.admin_calls = []
    aid, uname = await _sell(env)
    assert await SV.suspend(aid, "expired", DApi) is True
    assert (uname, True) in DApi.admin_calls                                    # ادمین غیرفعال شد
    DApi.admin_calls.clear()
    r = await SV.resume(7, aid, api_factory=DApi)
    assert r["status"] == "ok" and (uname, False) in DApi.admin_calls            # دوباره فعال
    # تعلیقِ حجم: ورودِ مشتری باز می‌ماند ⇒ خودِ ادمین غیرفعال نمی‌شود
    DApi.admin_calls.clear()
    assert await SV.suspend(aid, "traffic", DApi) is True
    assert DApi.admin_calls == []
    # تمدیدِ پنلِ منقضی ⇒ ادمین دوباره فعال
    aid2, uname2 = await _sell(env)
    assert await SV.suspend(aid2, "expired", DApi) is True
    DApi.admin_calls.clear()
    assert (await SV.renew(7, aid2, 1001, api_factory=DApi))["status"] == "ok"
    assert (uname2, False) in DApi.admin_calls


@pytest.mark.asyncio
async def test_panel_that_cannot_disable_admins_does_not_break_suspension(env):
    class Broken(DApi):
        async def set_admin_disabled(self, u, disabled):
            raise RuntimeError("boom")
    aid, uname = await _sell(env)
    assert await SV.suspend(aid, "expired", Broken) is True                     # کاربران خاموش و رمز تصادفی شد؛ فقط لاگ
