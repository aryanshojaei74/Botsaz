"""دسترسیِ «مالک» — شاملِ ادمینِ اصلیِ پلتفرم (دسترسیِ پشتیبانی).

وقتی ادمینِ پلتفرم برایِ پشتیبانی خودش را در رباتِ مشتری ادمین می‌کند، باید همهٔ بخش‌هایِ «فقط مالک» (توکنِ Fragment API،
TGTools، بک‌آپ، دامنه، درگاه‌ها و…) را مثلِ مالک ببیند؛ قبلاً با «این بخش فقط برایِ مالکِ ربات است» رد می‌شد."""
from __future__ import annotations


def is_owner_uid(tg_id, owner_id) -> bool:
    try:
        if int(tg_id) == int(owner_id):
            return True
        from config import PLATFORM_ADMIN_ID as _pa
        return bool(_pa) and int(tg_id) == int(_pa)
    except (TypeError, ValueError):
        return False
