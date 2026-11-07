"""
qr_inline.py — PATCH_QR_INLINE_V1

QR را روی خودِ سرور می‌سازد و به‌شکلِ data-URI برمی‌گرداند. قبلاً مینی‌اپ و صفحهٔ کانفیگ، لینکِ اشتراکِ مشتری (که یک «رازِ» قابلِ‌استفاده است)
را برایِ ساختنِ QR به سرویسِ عمومیِ api.qrserver.com می‌فرستادند؛ هم لینک به بیرون درز می‌کرد، هم در ایران با فیلترینگ QR نمی‌آمد.
خروجی None یعنی payload از ظرفیتِ استانداردِ QR بیشتر است ⇒ فراخوان باید QR نشان ندهد (و لینک را به بیرون نفرستد).
"""
from __future__ import annotations

import base64
import io
from functools import lru_cache

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


@lru_cache(maxsize=256)
def data_uri(payload: str, box_size: int = 6) -> str | None:
    p = (payload or "").strip()
    if not p:
        return None
    try:
        from tenant.shared.qr_brand import _make_qr_image
        img = _make_qr_image(p, box_size=box_size, border=2)
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:      # noqa: BLE001
        logger.exception("qr_inline: ساختِ QR ناموفق بود")
        return None
