"""
secret_box.py — رمزنگاریِ شفافِ رمزِ پنل‌هایِ VPN در دیتابیس (PATCH_SECRET_BOX_V1).

• ستونِ VpnPanel.password / api_key از نوعِ EncryptedStr است: موقعِ نوشتن (اگر فعال باشد) Fernet می‌شود و موقعِ خواندن
  خودکار برمی‌گردد؛ مقدارِ قدیمیِ متنِ ساده بی‌تغییر خوانده می‌شود، پس هیچ مهاجرتِ اجباری‌ای لازم نیست.
• نوشتنِ رمزشده **پیش‌فرض خاموش** است: فقط با BOTSAZ_ENCRYPT_SECRETS=1 در .env. خواندن همیشه کار می‌کند.
• کلید: BOTSAZ_SECRET_KEY (پیشنهادی؛ ثابت و پشتیبان‌گیری‌شده) وگرنه از PLATFORM_BOT_TOKEN مشتق می‌شود.
  ⚠️ اگر کلید گم یا عوض شود، رمزهایِ رمزشده خوانده نمی‌شوند و پنل‌ها وصل نمی‌شوند؛ قبل از روشن‌کردن کلید را نگه دار.
• مقدارِ رمزشده با «enc1:» شروع می‌شود؛ اگر از طولِ ستون بیشتر شود، همان متنِ ساده ذخیره می‌شود (خطایِ دیتابیس نمی‌دهد).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from sqlalchemy.types import String, TypeDecorator

logger = logging.getLogger(__name__)

PREFIX = "enc1:"
_warned = False


def _secret() -> bytes:
    raw = (os.environ.get("BOTSAZ_SECRET_KEY") or "").strip()
    if not raw:
        try:
            from config import PLATFORM_BOT_TOKEN
            raw = PLATFORM_BOT_TOKEN or ""
        except Exception:      # noqa: BLE001
            raw = ""
    return raw.encode()


def _fernet():
    s = _secret()
    if not s:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"botsaz-secret-box-v1|" + s).digest()))


def write_enabled() -> bool:
    return os.environ.get("BOTSAZ_ENCRYPT_SECRETS", "").strip() in ("1", "true", "yes", "on")


def encrypt(value: str) -> str:
    f = _fernet()
    if f is None:
        return value
    return PREFIX + f.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """متنِ ساده را همان‌طور برمی‌گرداند؛ رمزشده را باز می‌کند؛ اگر نشد (کلیدِ نادرست) مقدارِ خام را + یک هشدار."""
    global _warned
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    f = _fernet()
    try:
        if f is None:
            raise ValueError("کلید در دسترس نیست")
        return f.decrypt(value[len(PREFIX):].encode()).decode()
    except Exception as e:      # noqa: BLE001
        if not _warned:
            _warned = True
            logger.error("secret_box: بازکردنِ رمزِ ذخیره‌شده ممکن نشد (کلید عوض شده؟): %s", type(e).__name__)
        return value


class EncryptedStr(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or not write_enabled() or (isinstance(value, str) and value.startswith(PREFIX)):
            return value
        enc = encrypt(value)
        limit = getattr(self.impl, "length", None)
        if limit and len(enc) > limit:
            logger.warning("secret_box: مقدارِ رمزشده از طولِ ستون بیشتر است؛ متنِ ساده ذخیره شد")
            return value
        return enc

    def process_result_value(self, value, dialect):
        return decrypt(value) if value is not None else None
