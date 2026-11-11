import asyncio
import datetime
import os
import subprocess
import zipfile

import pytest

import backup_crypto as BC
import platform_server_backup as SB


def _tree(root):
    (root / "tenant").mkdir()
    (root / "tenant" / "a.py").write_text("print(1)")
    (root / "assets").mkdir()
    (root / "assets" / "logo.png").write_bytes(b"png")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"x")
    (root / "backups").mkdir()
    (root / "backups" / "old.dump").write_bytes(b"d")
    (root / "venv").mkdir()
    (root / "venv" / "lib.py").write_text("x")
    (root / "error.log").write_text("log")
    (root / "botsaz.db").write_bytes(b"db")
    (root / ".env").write_text("TOKEN=secret")
    (root / "requirements.txt").write_text("aiogram")


def test_collect_excludes_junk_and_respects_env_flag(tmp_path):
    _tree(tmp_path)
    files, _ = SB.collect_app_files(str(tmp_path), include_env=True)
    names = {a for _f, a in files}
    assert names == {"app/tenant/a.py", "app/assets/logo.png", "app/.env", "app/requirements.txt"}
    files, _ = SB.collect_app_files(str(tmp_path), include_env=False)
    assert "app/.env" not in {a for _f, a in files}


def test_zip_contains_db_app_and_restore_files(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _tree(root)
    dump = tmp_path / "botsaz-pg-1.dump"
    dump.write_bytes(b"PGDMP")
    out = tmp_path / "s.zip"
    count, _ = SB._build_zip(str(dump), str(out), True, root=str(root))
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert {"db/botsaz-pg-1.dump", "app/tenant/a.py", "app/.env", "restore.sh", "RESTORE.txt"} <= names
    assert count == len([n for n in names if n not in ("restore.sh", "RESTORE.txt")])


def test_restore_script_is_valid_bash(tmp_path):
    p = tmp_path / "restore.sh"
    p.write_text(SB.RESTORE_SH)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.asyncio
async def test_run_sends_encrypted_parts_and_records_info(tmp_path, monkeypatch):
    store = {SB.K_PASS: "x" * 12, SB.K_ENV: "1"}

    async def _get(k, d=""):
        return store.get(k, d)

    async def _set(k, v):
        store[k] = v
    monkeypatch.setattr(SB, "_get", _get)
    monkeypatch.setattr(SB, "_set", _set)
    monkeypatch.setattr(SB, "PLATFORM_ADMIN_ID", 777)
    root = tmp_path / "proj"
    root.mkdir()
    _tree(root)
    monkeypatch.setattr(SB, "BASE_DIR", str(root))

    import platform_backup

    async def fake_dump(dir_path):
        p = os.path.join(dir_path, "botsaz-pg-1.dump")
        open(p, "wb").write(b"PGDMP" * 10)
        return p
    monkeypatch.setattr(platform_backup, "make_dump", fake_dump)
    sent = []

    class Bot:
        async def send_document(self, dest, f, caption=None, parse_mode=None, request_timeout=None):
            sent.append((dest, open(f.path, "rb").read(), caption))
    ok, info = await SB.run_server_backup(Bot(), now=datetime.datetime(2026, 11, 11, 12, 0), part_bytes=10 ** 9)
    assert ok, info
    assert len(sent) == 1 and sent[0][0] == 777
    assert sent[0][1][:5] == BC.MAGIC and b"secret" not in sent[0][1]
    assert "SHA256" in sent[0][2]
    assert store[SB.K_LAST].startswith("2026-11-11T12:00")
    enc = tmp_path / "got.enc"
    enc.write_bytes(sent[0][1])
    plain = tmp_path / "got.zip"
    BC.decrypt_file(str(enc), str(plain), "x" * 12)
    with zipfile.ZipFile(plain) as z:
        assert "db/botsaz-pg-1.dump" in z.namelist() and z.read("app/.env") == b"TOKEN=secret"


@pytest.mark.asyncio
async def test_no_passphrase_sends_nothing(monkeypatch):
    async def _get(k, d=""):
        return d

    monkeypatch.setattr(SB, "_get", _get)
    ok, msg = await SB.run_server_backup(object())
    assert not ok and "رمز" in msg


@pytest.mark.asyncio
async def test_due_logic(monkeypatch):
    store = {SB.K_EN: "1", SB.K_HOURS: "48"}

    async def _get(k, d=""):
        return store.get(k, d)
    monkeypatch.setattr(SB, "_get", _get)
    now = datetime.datetime(2026, 11, 11, 12, 0)
    assert await SB.due(now) is True                     # هنوز نرفته
    store[SB.K_LAST] = (now - datetime.timedelta(hours=47)).isoformat()
    assert await SB.due(now) is False
    store[SB.K_LAST] = (now - datetime.timedelta(hours=49)).isoformat()
    assert await SB.due(now) is True
    store[SB.K_EN] = "0"
    assert await SB.due(now) is False
    store[SB.K_EN], store[SB.K_HOURS] = "1", "99999"
    assert await SB._hours() == 720


def test_interval_text():
    assert SB._fmt_interval(24) == "1 روز" and SB._fmt_interval(48) == "2 روز" and SB._fmt_interval(12) == "12 ساعت"
