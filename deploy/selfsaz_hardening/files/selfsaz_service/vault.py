"""رمزگذاری سشن‌های تلگرام (جایگزین XOR+HMAC قدیمی).

فرمت جدید: «v2:<Fernet token>» — AES-128-CBC + HMAC-SHA256 با کلید مشتق‌شده از
SELFSaz_SESSION_KEY (PBKDF2). فرمت قدیمی «<sig>.<hex>» فقط خوانده می‌شود تا مهاجرت
(migrate_vault.py) همه‌چیز را به فرمت جدید ببرد.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "v2:"
_SALT = b"selfsaz-vault-v2"


class VaultError(ValueError):
    pass


def _key(key: str | None) -> str:
    key = os.getenv("SELFSaz_SESSION_KEY", "") if key is None else key
    if not key or len(key) < 16:
        raise VaultError("SELFSaz_SESSION_KEY خالی یا کوتاه‌تر از ۱۶ کاراکتر است")
    return key


@lru_cache(maxsize=8)
def _fernet(key: str) -> Fernet:
    raw = hashlib.pbkdf2_hmac("sha256", key.encode(), _SALT, 200_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw))


def protect(value: str, key: str | None = None) -> str:
    return PREFIX + _fernet(_key(key)).encrypt(value.encode()).decode()


def is_v2(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def _legacy_decrypt(value: str, key: str) -> str:
    try:
        sig, enc = value.split(".", 1)
        ok = hmac.compare_digest(sig, hmac.new(key.encode(), enc.encode(), "sha256").hexdigest())
        raw = bytes.fromhex(enc)
    except ValueError as exc:
        raise VaultError("فرمت سشن نامعتبر است") from exc
    if not ok:
        raise VaultError("امضای سشن نامعتبر است (کلید اشتباه؟)")
    mask = hashlib.sha256(key.encode() + b"selfsaz").digest()
    return bytes(ch ^ mask[i % len(mask)] for i, ch in enumerate(raw)).decode()


def unprotect(value: str, key: str | None = None) -> str:
    key = _key(key) if key is not None else _key(None)
    if is_v2(value):
        try:
            return _fernet(key).decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken as exc:
            raise VaultError("رمزگشایی ناموفق (کلید اشتباه یا داده خراب)") from exc
    return _legacy_decrypt(value, key)


def rekey(value: str, old_key: str, new_key: str) -> str:
    return protect(unprotect(value, old_key), new_key)
