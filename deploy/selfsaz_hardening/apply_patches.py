#!/usr/bin/env python3
"""اعمال پچ‌های امنیتی روی کد موجود /opt/selfsaz (idempotent، همه‌یا‌هیچ).

هر پچ یک «لنگر» (متن دقیقِ قدیمی) دارد که باید دقیقاً یک‌بار در فایل باشد. اگر حتی یک لنگر
پیدا نشود، هیچ فایلی تغییر نمی‌کند. بعد از اعمال، هر فایل با compile بررسی می‌شود.

استفاده:  python3 apply_patches.py /opt/selfsaz [--dry-run]
"""
from __future__ import annotations

import py_compile
import shutil
import sys
import tempfile
from pathlib import Path

MARK = "SELFSAZ_HARDENING_V1"

# (فایل، لنگر قدیمی، جایگزین)
PATCHES: list[tuple[str, str, str]] = [
    # ───── app.py ─────
    ("selfsaz_service/app.py",
     '''def protect(value: str) -> str:
    key = os.getenv("SELFSaz_SESSION_KEY", "")
    if not key:
        raise HTTPException(503, "SELFSaz_SESSION_KEY is not configured")
    raw = value.encode()
    mask = hashlib.sha256(key.encode() + b"selfsaz").digest()
    enc = bytes(ch ^ mask[i % len(mask)] for i, ch in enumerate(raw)).hex()
    sig = hmac.new(key.encode(), enc.encode(), hashlib.sha256).hexdigest()
    return f"{sig}.{enc}"
''',
     '''def protect(value: str) -> str:  # SELFSAZ_HARDENING_V1: Fernet به‌جای XOR
    from selfsaz_service import vault
    try:
        return vault.protect(value)
    except vault.VaultError:
        raise HTTPException(503, "SELFSaz_SESSION_KEY is not configured")
'''),
    ("selfsaz_service/app.py",
     '''    return {"ok": True, "service": "selfsaz"}''',
     '''    age = None  # SELFSAZ_HARDENING_V1: سلامتِ supervisor
    try:
        age = int(time.time()) - int((ROOT / "data" / "supervisor.heartbeat").read_text().strip())
    except Exception:
        pass
    return {"ok": True, "service": "selfsaz", "supervisor_age": age}'''),
    # ───── secure_connect.py ─────
    ("selfsaz_service/secure_connect.py",
     '''_SESSIONS: dict[str, dict[str, Any]] = {}
''',
     '''_SESSIONS: dict[str, dict[str, Any]] = {}
import time as _time  # SELFSAZ_HARDENING_V1
from selfsaz_service import guards
'''),
    ("selfsaz_service/secure_connect.py",
     '''            raise HTTPException(400, "شماره تلفن معتبر نیست")
        api_id, api_hash = api_credentials()''',
     '''            raise HTTPException(400, "شماره تلفن معتبر نیست")
        if not guards.SEND_CODE_LIMIT.allow(token):  # SELFSAZ_HARDENING_V1
            raise HTTPException(429, "تعداد درخواست‌ها زیاد است؛ چند دقیقه بعد تلاش کنید")
        for _stale in guards.purge_sessions(_SESSIONS):
            try:
                await _stale["client"].disconnect()
            except Exception:
                pass
        guards.SEND_CODE_LIMIT.gc()
        if len(_SESSIONS) >= guards.MAX_PENDING_SESSIONS and token not in _SESSIONS:
            raise HTTPException(503, "سرور مشغول است؛ کمی بعد تلاش کنید")
        api_id, api_hash = api_credentials()'''),
    ("selfsaz_service/secure_connect.py",
     '''        await client.connect()
        sent = await client.send_code(phone)
        _SESSIONS[token] = {"owner_id": owner_id, "phone": phone, "client": client, "phone_code_hash": sent.phone_code_hash}
        return {"ok": True}''',
     '''        try:  # SELFSAZ_HARDENING_V1: کلاینت نیمه‌باز نشت نکند، خطا خام نمایش داده نشود
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise HTTPException(400, guards.connect_error(exc))
        _old = _SESSIONS.pop(token, None)
        if _old:
            try:
                await _old["client"].disconnect()
            except Exception:
                pass
        _SESSIONS[token] = {"owner_id": owner_id, "phone": phone, "client": client,
                            "phone_code_hash": sent.phone_code_hash, "created": _time.time()}
        return {"ok": True}'''),
    ("selfsaz_service/secure_connect.py",
     '''        owner_id = valid_token(token)
        state = _SESSIONS.get(token)''',
     '''        owner_id = valid_token(token)
        if not guards.FINISH_LIMIT.allow(token):  # SELFSAZ_HARDENING_V1
            raise HTTPException(429, "تلاش‌های ناموفق زیاد است؛ یک لینک جدید بگیرید")
        state = _SESSIONS.get(token)'''),
    ("selfsaz_service/secure_connect.py",
     '''            raise HTTPException(400, f"اتصال ناموفق بود: {str(exc)[:180]}")''',
     '''            raise HTTPException(400, guards.connect_error(exc))  # SELFSAZ_HARDENING_V1'''),
    # ───── selfsaz_worker.py ─────
    ("selfsaz_worker.py",
     '''SESSION_STRING = os.getenv("SELFSAZ_SESSION_STRING", "") or None
''',
     '''SESSION_STRING = os.getenv("SELFSAZ_SESSION_STRING", "") or None
# SELFSAZ_HARDENING_V1: سشن/API از stdin می‌آید؛ نه env (قابل‌دیدن در /proc) و نه فایل
if os.getenv("SELFSAZ_SECRET_STDIN") == "1":
    try:
        _sec = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _sec = {}
    API_ID = int(_sec.get("api_id") or API_ID or 0)
    API_HASH = _sec.get("api_hash") or API_HASH
    SESSION_STRING = _sec.get("session") or SESSION_STRING
    _sec = None
INSULTS_ENABLED = os.getenv("SELFSAZ_ENABLE_INSULTS") == "1"
SPAM_ENABLED = os.getenv("SELFSAZ_ENABLE_SPAM") == "1"
'''),
    ("selfsaz_worker.py",
     '''GENERAL_AUTO_REPLY_FILE = os.path.join(os.getenv('SELFSaz_HOME', '/opt/selfsaz'), 'data', 'general_auto_reply.json')''',
     '''GENERAL_AUTO_REPLY_FILE = os.path.join(os.getenv('SELFSaz_HOME', '/opt/selfsaz'), 'data', f'general_auto_reply.{USER_ID}.json')  # SELFSAZ_HARDENING_V1: برای هر حساب جدا'''),
    ("selfsaz_worker.py",
     '''async def spam_command(client: Client, message: Message):
''',
     '''async def spam_command(client: Client, message: Message):
    if not SPAM_ENABLED:  # SELFSAZ_HARDENING_V1
        return await message.edit_text("⛔️ این قابلیت برای حفظ امنیت حساب شما غیرفعال است.")
'''),
    ("selfsaz_worker.py",
     '''async def insult_command(client: Client, message: Message):
''',
     '''async def insult_command(client: Client, message: Message):
    if not INSULTS_ENABLED:  # SELFSAZ_HARDENING_V1
        return await message.edit("⛔️ این قابلیت برای حفظ امنیت حساب شما غیرفعال است.")
'''),
    ("selfsaz_worker.py",
     '''    if user_id in enemies and message_text.strip():''',
     '''    if INSULTS_ENABLED and user_id in enemies and message_text.strip():  # SELFSAZ_HARDENING_V1'''),
]


def plan(root: Path) -> tuple[dict[str, str], list[str]]:
    """متن جدید هر فایل را برمی‌گرداند (بدون نوشتن) + لیست خطاها."""
    texts: dict[str, str] = {}
    errors: list[str] = []
    for rel, old, new in PATCHES:
        path = root / rel
        if rel not in texts:
            if not path.exists():
                errors.append(f"{rel}: فایل وجود ندارد")
                texts[rel] = ""
                continue
            texts[rel] = path.read_text(encoding="utf-8")
        text = texts[rel]
        if not text:
            continue
        if new in text:
            continue                                        # قبلاً اعمال شده
        n = text.count(old)
        if n != 1:
            errors.append(f"{rel}: لنگر {n} بار پیدا شد (باید ۱): {old.strip().splitlines()[0][:70]!r}")
            continue
        texts[rel] = text.replace(old, new, 1)
    return texts, errors


def apply(root: Path, dry_run: bool = False) -> list[str]:
    texts, errors = plan(root)
    if errors:
        return errors
    with tempfile.TemporaryDirectory() as td:               # اول کامپایل در جای موقت
        for rel, text in texts.items():
            tmp = Path(td) / Path(rel).name
            tmp.write_text(text, encoding="utf-8")
            try:
                py_compile.compile(str(tmp), doraise=True)
            except py_compile.PyCompileError as exc:
                return [f"{rel}: خطای نحوی بعد از پچ: {exc.msg[:120]}"]
    if dry_run:
        return []
    for rel, text in texts.items():
        path = root / rel
        if path.read_text(encoding="utf-8") != text:
            shutil.copy2(path, str(path) + ".bak-hardening")
            path.write_text(text, encoding="utf-8")
    return []


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: apply_patches.py /opt/selfsaz [--dry-run]")
    errs = apply(Path(args[0]), dry_run="--dry-run" in sys.argv)
    if errs:
        print("❌ پچ اعمال نشد (هیچ فایلی تغییر نکرد):")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("✅ پچ‌ها " + ("بررسی شد (dry-run)" if "--dry-run" in sys.argv else "اعمال شد"))
