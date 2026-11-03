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

    async def create_admin(self, username: str, password: str, role_id: int | None = None,
                           data_limit_bytes: int | None = None) -> None:
        body = {"username": username, "password": password, "is_sudo": False}
        if role_id is not None:             # PATCH_PSEL_PASARGUARD_ROLE_V1 — پاسارگاردِ جدید «role_id» اجباری می‌خواهد
            body["role_id"] = int(role_id)
            if data_limit_bytes and data_limit_bytes > 0:
                body["data_limit"] = int(data_limit_bytes)     # PATCH_PSEL_NATIVE_LIMIT_V1 — سقفِ حجمِ بومیِ پنل
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

    async def _admin_row(self, username: str) -> tuple[dict | None, bool]:
        """PATCH_PSEL_PASARGUARD_USAGE_V1 — (ردیفِ ادمین از GET /api/admins، «پنلِ جدید است؟»).
        پاسارگاردِ جدید: {"admins": [...], "total": ...} و هر ادمین used_traffic/total_users دارد؛ مرزبان/قدیمی: لیستِ ساده.
        فیلترِ username سمتِ سرور تأیید نشده، پس سمتِ ما هم دقیق مقایسه می‌شود."""
        res = await self.c._request("GET", "/api/admins", params={"username": username, "limit": 500})
        new_style = isinstance(res, dict) and isinstance(res.get("admins"), list)
        items = res.get("admins") if new_style else (res if isinstance(res, list) else [])
        for it in items or []:
            if isinstance(it, dict) and str(it.get("username") or "").lower() == username.lower():
                return it, new_style
        return None, new_style

    # ── PATCH_PSEL_NATIVE_LIMIT_V1 / PATCH_PSEL_CONTACT_FIELDS_V1: قابلیت‌هایِ پاسارگاردِ جدید (مستنداتِ رسمیِ AdminModify) ──
    async def is_new_style(self, username: str) -> bool:
        try:
            _row, new_style = await self._admin_row(username)
        except PanelError:
            return False
        return new_style

    async def put_admin(self, username: str, **fields) -> None:
        """PUT /api/admin/{username} — به‌روزرسانیِ جزئی (فقط فیلدهایِ غیر-None اعمال می‌شود؛ طبقِ کدِ رسمی)."""
        await self.c._request("PUT", f"/api/admin/{quote(username)}", json={k: v for k, v in fields.items() if v is not None})

    async def set_admin_disabled(self, username: str, disabled: bool) -> bool | None:
        """PATCH_PSEL_ADMIN_DISABLE_V1 (Whale: «پنلِ تستِ رایگان منقضی و خاموش شد ولی غیرفعالش نمی‌کند») — خودِ ادمین در پاسارگاردِ جدید
        «غیرفعال» می‌شود (is_disabled؛ در پنلِ «مدیرانِ غیرفعال» دیده می‌شود)، نه فقط کاربرانش. خروجی: True = اعمال و تأیید شد،
        False = پنل نپذیرفت، None = این پنل مفهومِ غیرفعال‌شدنِ ادمین ندارد (مرزبانِ قدیمی) یا تأییدش ممکن نبود."""
        try:
            if not await self.is_new_style(username):
                return None
            await self.put_admin(username, is_disabled=bool(disabled))
            row, _ns = await self._admin_row(username)
        except PanelError:
            return False
        if row is None or "is_disabled" not in row:
            return None
        return bool(row.get("is_disabled")) == bool(disabled)

    async def apply_data_limit(self, username: str, gb: int) -> bool:
        """سقفِ حجمِ بومیِ ادمین (بایت؛ ۰ = نامحدود). فقط روی پنلِ جدید؛ خروجی: True اگر اعمال شد."""
        if not await self.is_new_style(username):
            return False
        await self.put_admin(username, data_limit=(int(gb) * (1024 ** 3) if int(gb or 0) > 0 else 0))
        return True

    async def reset_usage(self, username: str) -> None:
        if await self.is_new_style(username):
            await self.c._request("POST", f"/api/admin/{quote(username)}/reset")
        else:
            await self.c._request("POST", f"/api/admin/usage/reset/{quote(username)}")

    async def snapshot(self, username: str) -> dict:
        """{used_bytes, users, new_style} با یک درخواست (نمایشِ صفحهٔ پنل)."""
        try:
            row, new_style = await self._admin_row(username)
        except PanelError:
            row, new_style = None, False
        if new_style and row is not None:
            return {"used_bytes": int(float(row.get("used_traffic") or 0)), "users": int(row.get("total_users") or 0), "new_style": True}
        return {"used_bytes": await self.usage_bytes(username), "users": await self.user_count(username), "new_style": new_style}

    async def usage_bytes(self, username: str) -> int:
        """مصرفِ کلِ کاربرانِ این ادمین (بایت). پنلِ جدید: used_traffic از لیستِ ادمین‌ها (یکایِ بایت تأییدِ رسمی ندارد)."""
        try:
            row, new_style = await self._admin_row(username)
        except PanelError:                                # پنلِ قدیمی بدونِ /api/admins ⇒ مسیرِ قدیمی (رفتارِ قبلی)
            row, new_style = None, False
        if new_style:
            if row is None or row.get("used_traffic") is None:
                raise PanelError("مصرفِ ادمین از پنلِ جدید خوانده نشد (ادمین در فهرست نبود یا used_traffic نداشت).")
            return int(float(row["used_traffic"]))
        res = await self.c._request("GET", f"/api/admin/usage/{quote(username)}")
        if isinstance(res, dict):
            res = res.get("raw", res.get("usage", 0))
        try:
            return int(float(res or 0))
        except (TypeError, ValueError):
            return 0

    async def user_count(self, username: str) -> int:
        try:
            row, new_style = await self._admin_row(username)
        except PanelError:
            row, new_style = None, False
        if new_style:                                    # پاسارگاردِ جدید: فیلترِ کاربرِ مالک تأیید نشده؛ total_users خودِ ادمین
            if row is None or row.get("total_users") is None:
                raise PanelError("تعدادِ کاربرانِ ادمین از پنلِ جدید خوانده نشد.")
            return int(row["total_users"])
        res = await self.c._request("GET", "/api/users", params={"admin": username, "limit": 1})
        if isinstance(res, dict):
            try:
                return int(res.get("total") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    # === PATCH_PSEL_MAX_USERS_ENFORCE_V1 === (Whale: «تست رایگان را ۳ کاربر گذاشتم ولی هرچقدر خواستند کاربر می‌سازند»)
    async def list_user_rows(self, username: str, expected: int | None = None) -> list[dict] | None:
        """کاربرانِ همین ادمین (GET /api/users?admin=…). None یعنی مطمئن نیستیم فیلتر واقعاً اعمال شده (هرگز حدس نمی‌زنیم):
        ردیفی که مالکش ادمینِ دیگری است، یا — وقتی ردیف‌ها مالک ندارند — تعدادِ ردیف‌ها با تعدادِ اعلامیِ ادمین نمی‌خواند."""
        rows: list[dict] = []
        offset = 0
        for _ in range(20):
            res = await self.c._request("GET", "/api/users", params={"admin": username, "limit": 500, "offset": offset})
            batch = res.get("users") if isinstance(res, dict) else (res if isinstance(res, list) else [])
            batch = [b for b in (batch or []) if isinstance(b, dict)]
            rows.extend(batch)
            total = res.get("total") if isinstance(res, dict) else None
            if not batch or len(batch) < 500 or (isinstance(total, int) and len(rows) >= total):
                break
            offset += 500
        low = username.lower()
        saw_owner = False
        for r in rows:
            adm = r.get("admin")
            owner = adm.get("username") if isinstance(adm, dict) else (adm if isinstance(adm, str) else r.get("owner_username"))
            if owner is None:
                continue
            saw_owner = True
            if str(owner).lower() != low:
                return None
        if not saw_owner and expected is not None and len(rows) != int(expected):
            return None
        return rows

    async def set_user_status(self, panel_username: str, status: str) -> None:
        """status: active | disabled — تغییرِ وضعیتِ یک کاربرِ پنل (PUT /api/user/{username})."""
        await self.c._request("PUT", f"/api/user/{quote(panel_username)}", json={"status": status})

    async def ensure_users_state(self, username: str, enabled: bool) -> int | None:
        """PATCH_PSEL_VERIFY_STATE_V1 (Whale: «ربات می‌گوید غیرفعال شد ولی در پنل هنوز فعال است»).
        اندپوینتِ دسته‌جمعیِ خاموش/روشن بعضی پنل‌ها بی‌صدا اثر نمی‌گذارد (پاسِ خالی = «موفق» حساب می‌شد). این‌جا وضعیتِ واقعیِ
        کاربران خوانده می‌شود، هر کاربرِ هنوز اشتباه تکی اصلاح می‌شود و دوباره خوانده می‌شود.
        خروجی: تعدادِ کاربرانِ باقی‌مانده در وضعیتِ اشتباه (۰ = موفق)؛ None = نتوانستیم تأیید کنیم."""
        want = "active" if enabled else "disabled"

        def wrong(r: dict) -> bool:
            st = str(r.get("status") or "active")
            return st == "disabled" if enabled else st in ("active", "on_hold")
        try:
            expected = await self.user_count(username)
            rows = await self.list_user_rows(username, expected)
        except PanelError:
            return None
        if rows is None:
            return None
        bad = [r for r in rows if wrong(r) and r.get("username")]
        if not bad:
            return 0
        for r in bad:
            try:
                await self.set_user_status(str(r["username"]), want)
            except PanelError:
                continue
        try:
            rows2 = await self.list_user_rows(username, expected)
        except PanelError:
            return None
        if rows2 is None:
            return None
        return sum(1 for r in rows2 if wrong(r))

    async def restart_core(self) -> None:
        await self.c._request("POST", "/api/core/restart")
