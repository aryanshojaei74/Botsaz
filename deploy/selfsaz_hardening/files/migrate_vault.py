#!/usr/bin/env python3
"""مهاجرت سشن‌های ذخیره‌شده از فرمت قدیمی (XOR) به v2 (Fernet) — و در صورت نیاز تعویض کلید.

  SELFSaz_SESSION_KEY=<کلید فعلی/جدید>  [SELFSaz_SESSION_KEY_OLD=<کلید قبلی>]  \
      python3 migrate_vault.py /opt/selfsaz [--dry-run]

• قبل از هر تغییر از دیتابیس بکاپ ۰۶۰۰ گرفته می‌شود.
• ردیفی که رمزگشایی نشود دست‌نخورده می‌ماند و گزارش می‌شود (مهاجرت متوقف نمی‌شود).
• بعد از نوشتن، هر ردیف دوباره رمزگشایی و با مقدار اصلی مقایسه می‌شود.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from selfsaz_service import vault  # noqa: E402


def migrate(db_path: Path, new_key: str, old_key: str | None = None, dry_run: bool = False) -> dict:
    old_key = old_key or new_key
    res = {"total": 0, "migrated": 0, "already": 0, "failed": [], "backup": None}
    if not dry_run:
        bak = db_path.with_name(db_path.name + f".pre-vault-{int(time.time())}")
        shutil.copy2(db_path, bak)
        os.chmod(bak, 0o600)
        res["backup"] = str(bak)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, session_secret FROM accounts WHERE session_secret IS NOT NULL AND session_secret != ''").fetchall()
        res["total"] = len(rows)
        updates = []
        for r in rows:
            secret = r["session_secret"]
            same_key = old_key == new_key
            if vault.is_v2(secret) and same_key:
                res["already"] += 1
                continue
            try:
                plain = vault.unprotect(secret, old_key)
                fresh = vault.protect(plain, new_key)
                if vault.unprotect(fresh, new_key) != plain:
                    raise vault.VaultError("roundtrip mismatch")
            except vault.VaultError as exc:
                res["failed"].append((r["id"], str(exc)))
                continue
            updates.append((fresh, r["id"]))
        if not dry_run:
            con.execute("BEGIN")
            for fresh, rid in updates:
                con.execute("UPDATE accounts SET session_secret=? WHERE id=?", (fresh, rid))
            con.commit()
        res["migrated"] = len(updates)
    finally:
        con.close()
    return res


def read_env_file(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("\"'")
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: migrate_vault.py /opt/selfsaz [--dry-run]")
    root = Path(args[0])
    for k, v in read_env_file(root / ".env").items():       # کلیدها فقط در حافظهٔ همین پروسه
        os.environ.setdefault(k, v)
    if os.getenv("SELFSAZ_OLD_KEY_FILE"):                    # فایل ۰۶۰۰ حاوی کلید قبلی (اختیاری)
        os.environ["SELFSaz_SESSION_KEY_OLD"] = Path(os.environ["SELFSAZ_OLD_KEY_FILE"]).read_text().strip()
    new_key = os.getenv("SELFSaz_SESSION_KEY", "")
    if len(new_key) < 16:
        sys.exit("SELFSaz_SESSION_KEY باید تنظیم شده و حداقل ۱۶ کاراکتر باشد")
    out = migrate(root / "data" / "selfsaz.sqlite3", new_key,
                  os.getenv("SELFSaz_SESSION_KEY_OLD") or None, "--dry-run" in sys.argv)
    print(f"کل: {out['total']} | مهاجرت: {out['migrated']} | قبلاً v2: {out['already']} | ناموفق: {len(out['failed'])}")
    for rid, why in out["failed"]:
        print(f"  ⚠️ حساب {rid}: {why}")
    if out["backup"]:
        print("بکاپ دیتابیس:", out["backup"])
    sys.exit(1 if out["failed"] else 0)
