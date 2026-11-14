# -*- coding: utf-8 -*-
"""
backup_crypto.py — رمزگذاریِ بک‌آپ برایِ فرستادن به بیرونِ سرور (PATCH_OFFSITE_BACKUP_V1).

فقط به پکیجِ `cryptography` وابسته است (نه config/aiogram/دیتابیس)، تا اسکریپتِ بازیابی
(scripts/decrypt_backup.py) روی هر سرورِ تازه‌ای بدونِ نصبِ کلِ پروژه کار کند.

قالبِ فایل:  b"BSBK1" + salt(16) + [ len(4, big-endian) + nonce(12) + AES-GCM(chunk) ]…
  • کلید: scrypt(passphrase, salt)  — بدونِ رمز غیرقابل‌بازگشایی است.
  • هر تکه (۴ مگابایت) جدا با AES-256-GCM رمز می‌شود؛ دست‌کاری/خرابیِ هر تکه موقعِ بازگشایی
    خطا می‌دهد، و شماره‌ی تکه در AAD است تا تکه‌ها جابه‌جا/حذف نشوند.
  • فایل‌هایِ بزرگ تکه‌تکه خوانده می‌شوند (حافظه ثابت).
"""
from __future__ import annotations

import hashlib
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"BSBK1"
CHUNK = 4 * 1024 * 1024
_SCRYPT_N = 2 ** 15


class BadPassphraseOrCorrupt(Exception):
    """رمز اشتباه است یا فایل خراب/دست‌کاری شده."""


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1).derive(passphrase.encode("utf-8"))


def encrypt_file(src: str, dst: str, passphrase: str) -> None:
    if not passphrase:
        raise ValueError("passphrase is empty")
    salt = os.urandom(16)
    aes = AESGCM(_key(passphrase, salt))
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(MAGIC + salt)
        idx = 0
        while True:
            block = fi.read(CHUNK)
            if not block:
                break
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, block, struct.pack(">Q", idx))
            fo.write(struct.pack(">I", len(ct)) + nonce + ct)
            idx += 1
        # تکهٔ پایانیِ خالی: بریده‌شدنِ فایل (حذفِ تکه‌هایِ آخر) قابلِ تشخیص شود
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, b"", struct.pack(">Q", idx) + b"END")
        fo.write(struct.pack(">I", len(ct)) + nonce + ct)


def decrypt_file(src: str, dst: str, passphrase: str) -> None:
    with open(src, "rb") as fi:
        head = fi.read(len(MAGIC) + 16)
        if len(head) < len(MAGIC) + 16 or head[:len(MAGIC)] != MAGIC:
            raise BadPassphraseOrCorrupt("فایل قالبِ بک‌آپِ بات‌ساز ندارد")
        aes = AESGCM(_key(passphrase, head[len(MAGIC):]))
        tmp = dst + ".part"
        idx, ended = 0, False
        try:
            with open(tmp, "wb") as fo:
                while True:
                    lb = fi.read(4)
                    if not lb:
                        break
                    if ended or len(lb) < 4:
                        raise BadPassphraseOrCorrupt("ساختارِ فایل خراب است")
                    (n,) = struct.unpack(">I", lb)
                    nonce, ct = fi.read(12), fi.read(n)
                    if len(nonce) < 12 or len(ct) < n:
                        raise BadPassphraseOrCorrupt("فایل ناقص است (بریده شده)")
                    try:
                        pt = aes.decrypt(nonce, ct, struct.pack(">Q", idx))
                    except InvalidTag:
                        try:
                            aes.decrypt(nonce, ct, struct.pack(">Q", idx) + b"END")
                            ended = True
                            continue
                        except InvalidTag:
                            raise BadPassphraseOrCorrupt("رمز اشتباه است یا فایل خراب/دست‌کاری شده") from None
                    fo.write(pt)
                    idx += 1
            if not ended:
                raise BadPassphraseOrCorrupt("فایل ناقص است (تکهٔ پایانی نیست)")
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def split_file(path: str, part_bytes: int) -> list[str]:
    """فایل را به تکه‌هایِ <= part_bytes می‌شکند: path.part01of03 …؛ اگر کوچک باشد خودِ path برمی‌گردد."""
    size = os.path.getsize(path)
    if size <= part_bytes:
        return [path]
    total = -(-size // part_bytes)
    parts = []
    with open(path, "rb") as fi:
        for i in range(1, total + 1):
            p = f"{path}.part{i:02d}of{total:02d}"
            with open(p, "wb") as fo:
                left = part_bytes
                while left > 0:
                    b = fi.read(min(left, 8 * 1024 * 1024))
                    if not b:
                        break
                    fo.write(b)
                    left -= len(b)
            parts.append(p)
    return parts


def join_parts(parts: list[str], dst: str) -> None:
    with open(dst, "wb") as fo:
        for p in sorted(parts):
            with open(p, "rb") as fi:
                while True:
                    b = fi.read(8 * 1024 * 1024)
                    if not b:
                        break
                    fo.write(b)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()
