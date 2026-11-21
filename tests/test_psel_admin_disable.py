"""PATCH_PSEL_ADMIN_DISABLE_V1 (Whale) — «پنلِ تست منقضی و خاموش شد ولی غیرفعالش نمی‌کند»: خودِ ادمین در پاسارگاردِ جدید غیرفعال می‌شود."""
import pytest

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI as _Base, env, _sell  # noqa: F401


class P:
    """کلاینتِ ساختگیِ **وفادار به کدِ رسمیِ پاسارگارد**: AdminModify فقط «status» ("active"|"disabled") را می‌پذیرد و فیلدِ ناشناخته
    (مثلِ is_disabled) را بی‌صدا نادیده می‌گیرد؛ پاسخِ GET هم status و computed_field «is_disabled» دارد."""

    def __init__(self, apply=True, new_style=True, has_status=True):
        self.admin = {"username": "adm", "used_traffic": 0, "total_users": 1}
        if has_status:
            self.admin["status"] = "active"
            self.admin["is_disabled"] = False
        self.apply, self.new_style, self.calls = apply, new_style, []

    async def _request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if method == "GET" and path == "/api/admins":
            return {"admins": [self.admin], "total": 1} if self.new_style else [self.admin]
        if method == "PUT" and path == "/api/admin/adm" and self.apply and "status" in self.admin:
            st = (kw.get("json") or {}).get("status")
            if st in ("active", "disabled"):
                self.admin["status"] = st
                self.admin["is_disabled"] = st == "disabled"
        return {}


@pytest.mark.asyncio
async def test_adapter_disables_and_verifies_on_new_style_panel():
    p = P()
    assert await AD.PanelAdminAPI(p).set_admin_disabled("adm", True) is True
    assert ("PUT", "/api/admin/adm", {"status": "disabled"}) in p.calls and p.admin["status"] == "disabled"
    assert await AD.PanelAdminAPI(p).set_admin_disabled("adm", False) is True and p.admin["status"] == "active"
    # رگرسیون (Whale): بدنهٔ PUT نباید «is_disabled» باشد (پاسارگارد نادیده‌اش می‌گیرد ⇒ ادمین فعال می‌ماند)
    assert not any("is_disabled" in (c[2] or {}) for c in p.calls if c[0] == "PUT")


@pytest.mark.asyncio
async def test_adapter_reports_false_when_panel_ignored_it_and_none_when_unsupported():
    assert await AD.PanelAdminAPI(P(apply=False)).set_admin_disabled("adm", True) is False       # پنل نپذیرفت
    assert await AD.PanelAdminAPI(P(has_status=False)).set_admin_disabled("adm", True) is None   # فیلد را نمی‌شناسد
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


@pytest.mark.asyncio
async def test_enforce_heals_previously_suspended_admin_still_active(env):
    """حسابِ قبلاً-معلق که ادمینش هنوز «فعال» مانده، در دورِ بعدی غیرفعال می‌شود؛ حجمی دست نمی‌خورد."""
    from datetime import datetime
    calls = []

    class H(DApi):
        async def admin_is_disabled(self, u):
            return False

        async def set_admin_disabled(self, u, disabled):
            calls.append((u, disabled))
            return True

    aid, uname = await _sell(env)
    assert await SV.suspend(aid, "expired", DApi) is True
    await SV.enforce_once(7, api_factory=H)
    assert (uname, True) in calls
    calls.clear()

    class Done(H):
        async def admin_is_disabled(self, u):
            return True
    await SV.enforce_once(7, api_factory=Done)
    assert calls == []                                    # قبلاً غیرفعال ⇒ دست نمی‌زند
