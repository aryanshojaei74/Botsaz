"""
wallet_crypto.py — رمزنگاری/رمزگشاییِ mnemonicِ کیف‌پول‌هایِ حضانتی
(فعلاً فقط برایِ FragmentWallet در tenant/fragment_stars.py).

=== PATCH_FRAGMENT_WALLET_V1 ===
پروژه تا قبل از این هیچ کیف‌پولی حضانت نمی‌کرد (TonDirect/TonPays فقط آدرسِ
خودِ فروشنده را رصد می‌کنند). این فایل تنها نقطه‌ای است که یک secretِ
واقعاً حساس (کلیدِ خصوصیِ پول) رمزنگاری/رمزگشایی می‌شود.

کلیدِ رمزنگاری (WALLET_ENC_KEY) در دیتابیس نیست، در .env است — چون اگر
کلید همراهِ داده در یک منبع باشد، رمزنگاری عملاً بی‌فایده می‌شود.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from config import WALLET_ENC_KEY

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


class WalletCryptoNotConfigured(Exception):
    """WALLET_ENC_KEY در .env تنظیم نشده — عمداً fail-closed."""


def _fernet() -> Fernet:
    if not WALLET_ENC_KEY:
        raise WalletCryptoNotConfigured(
            "WALLET_ENC_KEY در .env تنظیم نشده. با دستور زیر بساز:\n"
            "python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"")
    return Fernet(WALLET_ENC_KEY.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("wallet_crypto: توکنِ نامعتبر یا کلیدِ عوض‌شده")
        raise
