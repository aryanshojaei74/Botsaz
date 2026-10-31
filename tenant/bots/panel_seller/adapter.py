"""
adapter.py — ادمینِ پنل (PATCH_PANEL_SELLER_V1).

«ربات پنل‌فروشی» به مشتری یک **ادمینِ بدونِ sudo** روی پنلِ خودِ مالک می‌فروشد. فقط پنل‌هایی که مفهومِ «ادمین» دارند
پشتیبانی می‌شوند: خانوادهٔ مرزبان (مرزبان، ربکا، پاسارگادِ قدیمی، ریک‌پنل) که کلاینتِ آن‌ها همان
MarzbanPanelClient است. پنل‌هایِ دیگر (سنایی/۳X-UI، هیدیفای، ...) حسابِ «ادمین» ندارند و در لیست «پشتیبانی‌نشده» می‌مانند.

مرزبانِ اصلی برایِ ادمین سقفِ حجم/تعدادِ کاربر ندارد؛ این سقف‌ها را service.enforce_once با خواندنِ مصرف و خاموش‌کردنِ
اشتراک‌هایِ ادمین اعمال می‌کند (تأخیر دارد، قطعی نیست).
"""
from __future__ import annotations

from urllib.parse import quote

from tenant.shared.panel_clients.vpn_panel import PanelError

SUPPORTED_BASES = frozenset({"marzban", "rebecca", "pasargad", "rickpanel"})


def base_key(panel_key: str | None) -> str:
    return (panel_key or "").split("#", 1)[0]


def supports(panel_key: str | None) -> bool:
    return base_key(panel_key) in SUPPORTED_BASES


class PanelAdminAPI:
    """نمای ساده بالای کلاینتِ پنل. client باید _request و close داشته باشد (MarzbanPanelClient یا شبیهِ آن)."""

    def __init__(self, client):
        self.c = client

    @classmethod
    def for_panel(cls, panel) -> "PanelAdminAPI":
        from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
        if not supports(getattr(panel, "panel_key", None)):
            raise PanelError("این نوع پنل مفهومِ «ادمین» ندارد (فقط خانوادهٔ مرزبان).")
        return cls(make_panel_client(panel))

    async def close(self) -> None:
        try:
            await self.c.close()
        except Exception:        # noqa: BLE001
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()

    async def test(self) -> list[dict]:
        """لیستِ ادمین‌ها؛ اگر اتصال یا دسترسیِ sudo مشکل داشته باشد PanelError می‌دهد."""
        return await self.c.list_admins()

    async def create_admin(self, username: str, password: str, role_id: int | None = None) -> None:
        body = {"username": username, "password": password, "is_sudo": False}
        if role_id is not None:             # PATCH_PSEL_PASARGUARD_ROLE_V1 — پاسارگاردِ جدید «role_id» اجباری می‌خواهد
            body["role_id"] = int(role_id)
        res = await self.c._request("POST", "/api/admin", json=body)
        if res is None:
            raise PanelError("ساختِ ادمین روی پنل جواب نداد (دسترسیِ sudo را بررسی کن).")

    async def list_roles(self) -> list[dict]:
        """نقش‌هایِ ادمین (پاسارگاردِ جدید): GET /api/admin-roles/simple → [{id, name, is_owner}]. پنلِ بدونِ نقش ⇒ []."""
        try:
            res = await self.c._request("GET", "/api/admin-roles/simple")
        except PanelError:
            return []
        if isinstance(res, dict):
            res = res.get("roles") or []
        return [r for r in (res or []) if isinstance(r, dict) and r.get("id") is not None]

    async def set_password(self, username: str, password: str) -> None:
        try:
            await self.c.modify_admin_password(username, password)
        except PanelError as e:
            # پاسارگاردِ جدید فیلدِ is_sudo ندارد (نقش دارد) و بدنهٔ مرزبانی را رد می‌کند؛ یک‌بار فقط با رمز
            if "422" not in str(e) and "is_sudo" not in str(e):
                raise
            await self.c._request("PUT", f"/api/admin/{quote(username)}", json={"password": password})

    async def delete_admin(self, username: str) -> None:
        await self.c.delete_admin(username)

    async def disable_users(self, username: str) -> None:
        await self.c._request("POST", f"/api/admin/{quote(username)}/users/disable")

    async def activate_users(self, username: str) -> None:
        await self.c._request("POST", f"/api/admin/{quote(username)}/users/activate")

    async def usage_bytes(self, username: str) -> int:
        """مصرفِ کلِ کاربرانِ این ادمین (بایت)."""
        res = await self.c._request("GET", f"/api/admin/usage/{quote(username)}")
        if isinstance(res, dict):
            res = res.get("raw", res.get("usage", 0))
        try:
            return int(float(res or 0))
        except (TypeError, ValueError):
            return 0

    async def user_count(self, username: str) -> int:
        res = await self.c._request("GET", "/api/users", params={"admin": username, "limit": 1})
        if isinstance(res, dict):
            try:
                return int(res.get("total") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    async def restart_core(self) -> None:
        await self.c._request("POST", "/api/core/restart")
