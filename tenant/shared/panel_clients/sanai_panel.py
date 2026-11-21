"""sanai_panel.py — کلاینت مستقلِ پنل سنایی (3X-UI).

پنل سنایی از نظر فنی همان نرم‌افزار 3X-UI است، بنابراین این کلاینت از
PanelClient (تعریف‌شده در tenant/vpn_panel.py) ارث می‌برد. جداکردنش در
فایل مستقل باعث می‌شود این پنل — مثل پاسارگاد و مرزبان — کاملاً مستقل از
بقیهٔ پنل‌ها به ربات وصل و کانفیگ ارائه بدهد، و اگر در آینده رفتار سنایی
با بقیهٔ پنل‌های 3X-UI فرق کرد (مثلاً نسخهٔ API جدید)، تغییرش فقط همین
فایل را تحت‌تأثیر قرار می‌دهد، نه پنل علیرضا یا هر پنل دیگری.
"""
from __future__ import annotations

from tenant.shared.panel_clients.vpn_panel import PanelClient, PanelError, GB, gen_sub_id, gen_uuid

__all__ = ["SanaiPanelClient"]


class SanaiPanelClient(PanelClient):
    """کلاینتِ اختصاصیِ پنل سنایی — فعلاً دقیقاً همان رفتار 3X-UI پایه را دارد."""
    pass
