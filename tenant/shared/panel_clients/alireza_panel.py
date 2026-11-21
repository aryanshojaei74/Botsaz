"""alireza_panel.py — کلاینت مستقلِ پنل علیرضا (3X-UI).

پنل علیرضا هم از نظر فنی 3X-UI است، بنابراین این کلاینت از PanelClient
(تعریف‌شده در tenant/vpn_panel.py) ارث می‌برد. جداکردنش در فایل مستقل
باعث می‌شود این پنل — مثل پاسارگاد و مرزبان — کاملاً مستقل از بقیهٔ
پنل‌ها به ربات وصل و کانفیگ ارائه بدهد، و اگر در آینده رفتار علیرضا با
بقیهٔ پنل‌های 3X-UI فرق کرد، تغییرش فقط همین فایل را تحت‌تأثیر قرار
می‌دهد، نه پنل سنایی یا هر پنل دیگری.
"""
from __future__ import annotations

from tenant.shared.panel_clients.vpn_panel import PanelClient, PanelError, GB, gen_sub_id, gen_uuid

__all__ = ["AlirezaPanelClient"]


class AlirezaPanelClient(PanelClient):
    """کلاینتِ اختصاصیِ پنل علیرضا — فعلاً دقیقاً همان رفتار 3X-UI پایه را دارد."""
    pass
