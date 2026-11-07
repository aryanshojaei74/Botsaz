"""degeris_panel.py — کلاینتِ پنلِ DEGERIS («پنلِ ثنایی جدید»).

=== PATCH_DEGERIS_PANEL_V1 ===
DEGERIS یک پنلِ ۳X-UI نسلِ جدید است: همان API ِ «clients» (ساخت/گرفتن/ترافیک/bulkAdjust/resetTraffic/del/
subLinks/links) که PanelClient پیاده کرده؛ تنها تفاوت‌ها:
  • فرمِ ورود فیلدِ twoFactorCode هم دارد (در OpenAPI «required»؛ بدونِ 2FA خالی فرستاده می‌شود).
  • کوکیِ نشست «degerist» است (نیازی به تغییر نیست؛ cookie jar خودکار).
  • آدرسِ پنل معمولاً شاملِ «مسیرِ مخفی» است: https://host:port/<secret>  — همان‌طور وارد شود.
بقیهٔ رفتار عیناً PanelClient است؛ هر تفاوتِ آینده فقط در همین فایل اعمال می‌شود.
"""
from __future__ import annotations

from tenant.shared.panel_clients.vpn_panel import PanelClient, PanelError, GB, gen_sub_id, gen_uuid

__all__ = ["DegerisPanelClient"]


class DegerisPanelClient(PanelClient):
    """کلاینتِ پنلِ DEGERIS (ثنایی جدید)."""
    _LOGIN_EXTRA = {"twoFactorCode": ""}


# === PATCH_DEGERIS_RBAC_FALLBACK_V1 ===
# DEGERIS نقش‌محور (RBAC) است: ادمینی که مجوزِ «inbounds.view» ندارد از /inbounds/list خطای ۴۰۳ می‌گیرد
# («inbounds.view permission required») درحالی‌که لاگینش درست است. اینجا به‌ترتیب /inbounds/list، /inbounds/options
# (انتخاب‌گرِ سبک) و /inbounds/list/slim را امتحان می‌کنیم؛ اگر هر سه رد شد و ادمین inboundها را دستی داده بود،
# همان‌ها استفاده می‌شود (بدونِ نیاز به لیست).
_RBAC_HINT = ("نقشِ این ادمین در پنلِ DEGERIS مجوزِ «مشاهدهٔ inboundها» (inbounds.view) را ندارد.\n"
              "راه‌حل: در پنل ← ادمین‌ها/نقش‌ها، به نقشِ این ادمین مجوزهایِ inbounds.view و کلاینت‌ها (clients) را بده، "
              "یا با ادمینِ اصلی (owner) وصل کن؛ یا شناسهٔ inboundها را در مرحلهٔ ۵ دستی بفرست (به‌جایِ «همه»).")


async def _deg_list_inbounds(self):
    last = None
    for path in ("/panel/api/inbounds/list", "/panel/api/inbounds/options", "/panel/api/inbounds/list/slim"):
        try:
            res = await self._request("GET", path)
        except PanelError as e:
            last = e
            continue
        if res.get("success"):
            return res.get("obj", []) or []
        last = PanelError(str(res.get("msg") or res))
    msg = str(last or "")
    if "permission" in msg.lower() or "403" in msg:
        raise PanelError(_RBAC_HINT) from last
    raise PanelError(f"خطا در گرفتن لیست inbound: {msg}") from last


async def _deg_active_inbound_ids(self):
    try:
        return await PanelClient.active_inbound_ids(self)
    except PanelError:
        if self.inbound_ids:      # لیست در دسترس نیست ولی ادمین شناسه‌ها را دستی داده
            return list(self.inbound_ids)
        raise


DegerisPanelClient.list_inbounds = _deg_list_inbounds            # type: ignore[assignment]
DegerisPanelClient.active_inbound_ids = _deg_active_inbound_ids  # type: ignore[assignment]
