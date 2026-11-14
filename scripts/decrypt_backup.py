#!/usr/bin/env python3
"""
scripts/decrypt_backup.py — بازگشایی بک‌آپِ رمزگذاری‌شدهٔ بیرونِ سرور (PATCH_OFFSITE_BACKUP_V1).

فقط به پایتون و پکیجِ cryptography نیاز دارد (روی هر سرور/کامپیوتری؛ بدونِ نصبِ پروژه):

    pip install cryptography
    python3 decrypt_backup.py botsaz-20261022-0400-dump.enc.part01of02 botsaz-20261022-0400-dump.enc.part02of02 -o botsaz.dump
    python3 decrypt_backup.py botsaz-20261022-0400-env.enc -o env-backup        # فایل .env

(تکه‌ها را به ترتیب یا هر ترتیبی بده؛ خودش مرتب می‌کند.) رمز با پرسش (بدونِ نمایش) گرفته می‌شود.
بعد از گرفتنِ dump روی سرورِ جدید:

    createdb botsaz && pg_restore --no-owner --no-privileges -d botsaz botsaz.dump
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import backup_crypto as BC
except ImportError:                       # فقط خودِ فایل را کنارِ اسکریپت گذاشته باشند
    sys.exit("backup_crypto.py کنارِ این اسکریپت (یا در ریشهٔ پروژه) لازم است.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="فایل(هایِ) .enc یا تکه‌هایِ .partNNofMM")
    ap.add_argument("-o", "--out", required=True, help="مسیرِ خروجیِ بازگشایی‌شده")
    a = ap.parse_args()
    pw = os.environ.get("BACKUP_PASSPHRASE") or getpass.getpass("رمزِ بک‌آپ: ")
    with tempfile.TemporaryDirectory() as tmp:
        src = a.files[0]
        if len(a.files) > 1 or ".part" in os.path.basename(src):
            src = os.path.join(tmp, "joined.enc")
            BC.join_parts(a.files, src)
        try:
            BC.decrypt_file(src, a.out, pw)
        except BC.BadPassphraseOrCorrupt as e:
            print("❌", e)
            return 1
    print(f"✅ بازگشایی شد: {a.out} ({os.path.getsize(a.out) / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
