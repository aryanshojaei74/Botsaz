"""pasarguard_panel.py — panel_key="pasarguard" (PasarGuard رسمی).

=== PATCH_MERGE_PASARGUARD_INTO_PASARGAD_V1 ===
این فایل قبلاً یک پیاده‌سازیِ کامل و مستقل داشت که از نظر کد با
tenant/pasargad_panel.py (panel_key="pasargad") ۱۰۰٪ یکسان بود — همان
endpointها (POST /api/admin/token، POST /api/user با group_ids، GET
/api/groups)، همان منطق، فقط با نام‌ها/کامنت‌های متفاوت. برای حذفِ کدِ
تکراری، این فایل حالا فقط کلاسِ pasargad_panel.py را دوباره export
می‌کند؛ panel_key هر دو ("pasargad" و "pasarguard") بدونِ هیچ تغییری در
رفتار به همین یک پیاده‌سازی وصل می‌مانند — پس بات‌های موجود با هرکدوم از
این دو panel_key نمی‌شکنند.

⚠️ نکته برای آینده: اگر API نسخهٔ رسمیِ PasarGuard در یک آپدیتِ بعدی از
پاسارگادِ قدیمی واقعاً جدا شد (endpoint یا قرارداد متفاوت شد)، این فایل
باید دوباره به یک پیاده‌سازیِ مستقل تبدیل شود (این‌همانی دیگر برقرار
نیست). تا آن موقع، نگه‌داریِ یک نسخه به‌جای دو نسخهٔ هم‌سان امن‌تر و
ساده‌تر است.
"""
from __future__ import annotations

from tenant.shared.panel_clients.pasargad_panel import PasarGuardPanelClient

__all__ = ["PasarGuardPanelClient"]
