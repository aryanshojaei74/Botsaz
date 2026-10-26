"""کیت سخت‌سازی SelfSaz (deploy/selfsaz_hardening): vault، guards، supervisor، migrate و پچ‌ها."""
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent / "deploy" / "selfsaz_hardening"
sys.path.insert(0, str(KIT / "files"))
sys.path.insert(0, str(KIT))

from selfsaz_service import guards, supervisor as SV, vault  # noqa: E402
import apply_patches as AP  # noqa: E402
import migrate_vault as MV  # noqa: E402

KEY = "k" * 32


def legacy_protect(value: str, key: str) -> str:                   # الگوریتم قدیمیِ app.protect
    mask = hashlib.sha256(key.encode() + b"selfsaz").digest()
    enc = bytes(ch ^ mask[i % len(mask)] for i, ch in enumerate(value.encode())).hex()
    return f"{hmac.new(key.encode(), enc.encode(), hashlib.sha256).hexdigest()}.{enc}"


# ───────────── vault ─────────────
def test_vault_roundtrip_and_legacy_and_rekey():
    s = "1BVtsOHgBu" * 8
    v2 = vault.protect(s, KEY)
    assert v2.startswith("v2:") and s not in v2 and vault.unprotect(v2, KEY) == s
    assert vault.protect(s, KEY) != v2                              # IV تصادفی
    assert vault.unprotect(legacy_protect(s, KEY), KEY) == s         # مهاجرت از فرمت قدیم
    new = "n" * 32
    assert vault.unprotect(vault.rekey(v2, KEY, new), new) == s
    with pytest.raises(vault.VaultError):
        vault.unprotect(v2, "x" * 32)
    with pytest.raises(vault.VaultError):
        vault.unprotect(v2[:-4] + "AAAA", KEY)                      # دست‌کاری
    with pytest.raises(vault.VaultError):
        vault.unprotect(legacy_protect(s, KEY), "x" * 32)
    with pytest.raises(vault.VaultError):
        vault.protect("a", "short")


def test_vault_uses_env_key(monkeypatch):
    monkeypatch.setenv("SELFSaz_SESSION_KEY", KEY)
    assert vault.unprotect(vault.protect("hello")) == "hello"
    monkeypatch.delenv("SELFSaz_SESSION_KEY")
    with pytest.raises(vault.VaultError):
        vault.protect("hello")


# ───────────── guards ─────────────
def test_rate_limiter_window():
    t = [0.0]
    rl = guards.RateLimiter(2, 10, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")                                             # کلید دیگر مستقل
    t[0] = 11
    assert rl.allow("a")
    rl.gc()


def test_purge_sessions_and_safe_errors():
    sess = {"old": {"created": 0, "client": 1}, "new": {"created": 1000, "client": 2}}
    gone = guards.purge_sessions(sess, now=1000)
    assert [g["client"] for g in gone] == [1] and list(sess) == ["new"]

    class PhoneCodeInvalid(Exception): ...
    class Weird(Exception): ...
    assert "اشتباه" in guards.connect_error(PhoneCodeInvalid("/opt/x secret"))
    msg = guards.connect_error(Weird("Traceback /opt/selfsaz/app.py sqlite"))
    assert "/opt" not in msg and "sqlite" not in msg


# ───────────── supervisor ─────────────
def test_backoff_and_worker_env():
    assert [SV.backoff(n) for n in (1, 2, 3)] == [15, 30, 60] and SV.backoff(50) == 900
    env = SV.worker_env({"PATH": "/bin", "SELFSaz_SESSION_KEY": "s", "SELFSaz_API_KEY": "k",
                         "SELFSAZ_API_HASH": "h", "SELFSaz_API_ID": "1", "SELFSAZ_HELPER_BOT": "bot",
                         "DATABASE_PASSWORD": "p", "SELFSAZ_ENABLE_SPAM": "1"})
    assert env["PATH"] == "/bin" and env["SELFSAZ_HELPER_BOT"] == "bot" and env["SELFSAZ_ENABLE_SPAM"] == "1"
    assert env["SELFSAZ_SECRET_STDIN"] == "1"
    for bad in ("SELFSaz_SESSION_KEY", "SELFSaz_API_KEY", "SELFSAZ_API_HASH", "DATABASE_PASSWORD"):
        assert bad not in env


def test_rotate_log(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"x" * 100)
    assert not SV.rotate_log(p, max_bytes=200)
    with open(p, "ab", buffering=0) as w:                            # نویسندهٔ O_APPEND زنده می‌ماند
        w.write(b"y" * 200)
        assert SV.rotate_log(p, max_bytes=200)
        w.write(b"after")
    assert (tmp_path / "a.log.1").stat().st_size == 300 and p.read_bytes() == b"after"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SELFSaz_SESSION_KEY", KEY)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    SV.init_db(con)
    return con


def _add(con, owner=1, enabled=1, secret="auto", expires=None, status="queued"):
    if secret == "auto":
        secret = vault.protect("sess" * 10, KEY)
    con.execute("INSERT INTO accounts(owner_id,session_secret,api_id,api_hash,enabled,status,expires_at) VALUES(?,?,?,?,?,?,?)",
                (owner, secret, 1, "hash", enabled, status, expires))
    con.commit()
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _row(con, aid):
    return con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()


class Harness:
    def __init__(self):
        self.spawns, self.killed, self.alive_pids, self.rss = 0, [], set(), {}
        self.next_pid = 1000

    def spawn(self, row):
        self.spawns += 1
        self.next_pid += 1
        return self.next_pid

    def alive(self, pid):
        return bool(pid) and pid in self.alive_pids

    def run(self, con, fleet, now):
        return SV.reconcile(con, fleet, now, spawn=self.spawn, alive=self.alive,
                            rss=lambda pid: self.rss.get(pid, 10), kill=lambda pid, sig: self.killed.append(pid))


def test_spawn_once_and_stay(db):
    aid, h, f = _add(db), Harness(), SV.Fleet()
    h.run(db, f, 0)
    r = _row(db, aid)
    assert h.spawns == 1 and r["status"] == "running" and r["worker_pid"]
    h.alive_pids.add(r["worker_pid"])
    for t in (10, 20, 200):
        h.run(db, f, t)
    assert h.spawns == 1                                             # زنده است ⇒ دوباره spawn نمی‌شود


def test_crash_loop_backs_off_then_gives_up_then_recovers(db):
    aid, h, f = _add(db), Harness(), SV.Fleet()
    t = 0.0
    for _ in range(400):                                             # worker همیشه فوراً می‌میرد
        h.run(db, f, t)
        t += 10
        if _row(db, aid)["status"] == "error":
            break
    assert _row(db, aid)["status"] == "error" and h.spawns == SV.MAX_FAILS
    assert "reconnect" in _row(db, aid)["last_error"]
    n = h.spawns
    for _ in range(50):
        h.run(db, f, t)
        t += 100
    assert h.spawns == n                                             # تسلیم‌شده ⇒ دیگر تلاش نمی‌کند
    # مدت زمانی که صرف شد باید با backoff بزرگ‌تر از ۶×۱۰ ثانیه باشد (نه حلقهٔ ۱۰ثانیه‌ای)
    assert t > sum(SV.backoff(i) for i in range(1, SV.MAX_FAILS))
    db.execute("UPDATE accounts SET status='queued' WHERE id=?", (aid,))   # کاربر دوباره وصل کرد
    db.commit()
    h.run(db, f, t + 1)
    assert h.spawns == n + 1


def test_stable_worker_resets_failures(db):
    aid, h, f = _add(db), Harness(), SV.Fleet()
    h.run(db, f, 0)
    pid = _row(db, aid)["worker_pid"]
    h.alive_pids.add(pid)
    f.fails[aid] = 3
    h.run(db, f, SV.QUICK_DEATH_SEC + 5)
    assert aid not in f.fails


def test_expired_account_stopped(db):
    aid = _add(db, expires="2020-01-01T00:00:00+00:00")
    h, f = Harness(), SV.Fleet()
    db.execute("UPDATE accounts SET worker_pid=555 WHERE id=?", (aid,))
    h.alive_pids.add(555)
    h.run(db, f, 0)
    r = _row(db, aid)
    assert r["status"] == "expired" and r["enabled"] == 0 and 555 in h.killed and h.spawns == 0


def test_memory_limit_kills_and_requeues(db):
    aid, h, f = _add(db), Harness(), SV.Fleet()
    h.run(db, f, 0)
    pid = _row(db, aid)["worker_pid"]
    h.alive_pids.add(pid)
    h.rss[pid] = SV.RSS_LIMIT_MB + 100
    out = h.run(db, f, 10)
    assert out["oom"] == 1 and pid in h.killed and _row(db, aid)["status"] == "queued"


def test_capacity_cap(db, monkeypatch):
    monkeypatch.setattr(SV, "MAX_ACCOUNTS", 1)
    a, b, h, f = _add(db, 1), _add(db, 2), Harness(), SV.Fleet()
    h.run(db, f, 0)
    assert h.spawns == 1
    assert {_row(db, a)["status"], _row(db, b)["status"]} == {"running", "queued"}


def test_undecryptable_or_missing_secret_not_retried(db):
    a = _add(db, 1, secret="v2:garbage")
    b = _add(db, 2, secret=None)
    h, f = Harness(), SV.Fleet()

    def bad_spawn(row):
        h.spawns += 1
        vault.unprotect(row["session_secret"])
    for t in (0, 10, 20):
        SV.reconcile(db, f, t, spawn=bad_spawn, alive=h.alive)
    assert h.spawns == 1 and _row(db, a)["status"] == "error" and _row(db, b)["status"] == "error"
    assert "Traceback" not in (_row(db, a)["last_error"] or "")


WORKER_SRC = '''
import json, os, sys
data = json.loads(sys.stdin.read())
out = {"cwd": os.getcwd(), "env": dict(os.environ), "stdin": data}
open(os.environ["SELFSaz_HOME"] + "/probe.json", "w").write(json.dumps(out))
'''


def test_real_spawn_passes_secret_via_stdin_only_in_own_dir(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "selfsaz_worker.py").write_text(WORKER_SRC)
    monkeypatch.setattr(SV, "ROOT", root)
    monkeypatch.setattr(SV, "WORKER", root / "selfsaz_worker.py")
    monkeypatch.setenv("SELFSaz_SESSION_KEY", KEY)
    monkeypatch.setenv("SELFSaz_API_KEY", "svc-api-key")
    monkeypatch.setenv("SELFSAZ_HELPER_BOT", "mybot")
    row = {"id": 5, "owner_id": 4242, "api_id": 777, "api_hash": "abcdef",
           "session_secret": vault.protect("THE-SESSION-STRING", KEY)}
    pid = SV.spawn_worker(row)
    for _ in range(100):
        if (root / "probe.json").exists():
            break
        time.sleep(0.05)
    probe = json.loads((root / "probe.json").read_text())
    assert probe["stdin"] == {"session": "THE-SESSION-STRING", "api_id": 777, "api_hash": "abcdef"}
    assert probe["cwd"] == str(root / "data" / "accounts" / "4242")
    assert (root / "data" / "accounts" / "4242").stat().st_mode & 0o777 == 0o700
    blob = json.dumps(probe["env"])
    assert "THE-SESSION-STRING" not in blob and "svc-api-key" not in blob and KEY not in blob
    assert probe["env"]["SELFSAZ_HELPER_BOT"] == "mybot" and probe["env"]["SELFSAZ_USER_ID"] == "4242"
    os.waitpid(pid, 0) if False else None


# ───────────── migrate ─────────────
def test_migrate_legacy_to_v2_with_backup_and_failures(tmp_path):
    dbp = tmp_path / "s.sqlite3"
    con = sqlite3.connect(dbp)
    SV.init_db(con)
    legacy = legacy_protect("session-A" * 5, KEY)
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(1,?)", (legacy,))
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(2,?)", (vault.protect("B", KEY),))
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(3,?)", ("garbage",))
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(4,NULL)")
    con.commit()
    con.close()
    dry = MV.migrate(dbp, KEY, dry_run=True)
    assert dry["migrated"] == 1 and dry["backup"] is None
    out = MV.migrate(dbp, KEY)
    assert (out["total"], out["migrated"], out["already"]) == (3, 1, 1) and len(out["failed"]) == 1
    assert Path(out["backup"]).exists() and Path(out["backup"]).stat().st_mode & 0o777 == 0o600
    rows = sqlite3.connect(dbp).execute("SELECT owner_id, session_secret FROM accounts ORDER BY owner_id").fetchall()
    assert vault.unprotect(rows[0][1], KEY) == "session-A" * 5 and rows[0][1].startswith("v2:")
    assert rows[2][1] == "garbage"                                   # خراب دست‌نخورده


def test_migrate_key_rotation(tmp_path):
    dbp = tmp_path / "s.sqlite3"
    con = sqlite3.connect(dbp)
    SV.init_db(con)
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(1,?)", (vault.protect("S", KEY),))
    con.execute("INSERT INTO accounts(owner_id,session_secret) VALUES(2,?)", (legacy_protect("T", KEY),))
    con.commit()
    con.close()
    new = "z" * 32
    out = MV.migrate(dbp, new, old_key=KEY)
    assert out["migrated"] == 2 and not out["failed"]
    vals = [r[0] for r in sqlite3.connect(dbp).execute("SELECT session_secret FROM accounts ORDER BY id")]
    assert [vault.unprotect(v, new) for v in vals] == ["S", "T"]


# ───────────── پچ‌ها ─────────────
def _tree(tmp_path, only_files=None):
    """درخت مصنوعی که هر لنگر را یک‌بار دارد (فقط برای مکانیکِ پچ)."""
    for rel, old, _ in AP.PATCHES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((p.read_text() if p.exists() else "") + old + "\n", encoding="utf-8")
    return tmp_path


def test_patch_plan_mechanics(tmp_path):
    root = _tree(tmp_path)
    texts, errs = AP.plan(root)
    assert errs == [] and all(AP.MARK in t for t in texts.values())
    # idempotent: روی متنِ پچ‌شده دوباره plan هیچ خطایی نمی‌دهد و چیزی عوض نمی‌شود
    for rel, t in texts.items():
        (root / rel).write_text(t, encoding="utf-8")
    texts2, errs2 = AP.plan(root)
    assert errs2 == [] and texts2 == texts


def test_patch_fails_atomically_when_anchor_missing_or_duplicated(tmp_path):
    root = _tree(tmp_path)
    app = root / "selfsaz_service/app.py"
    app.write_text(app.read_text().replace("def protect", "def protect_changed"), encoding="utf-8")
    before = {p: p.read_text() for p in root.rglob("*.py")}
    errs = AP.apply(root)
    assert errs and any("لنگر 0" in e for e in errs)
    assert {p: p.read_text() for p in root.rglob("*.py")} == before   # هیچ فایلی تغییر نکرد
    root2 = _tree(tmp_path / "dup")
    w = root2 / "selfsaz_worker.py"
    w.write_text(w.read_text() * 2, encoding="utf-8")
    assert any("لنگر 2" in e for e in AP.plan(root2)[1])


def test_patch_rejects_syntax_errors(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text("X = 1\n")
    monkeypatch.setattr(AP, "PATCHES", [("m.py", "X = 1\n", "X = (\n")])
    errs = AP.apply(tmp_path)
    assert errs and "نحوی" in errs[0] and f.read_text() == "X = 1\n"


SRC = os.getenv("SELFSAZ_SRC")


@pytest.mark.skipif(not SRC, reason="SELFSAZ_SRC (کدِ واقعیِ سرویس) تنظیم نشده")
def test_patches_on_real_source_and_app_smoke(tmp_path):
    root = tmp_path / "selfsaz"
    shutil.copytree(SRC, root)
    assert AP.apply(root) == []
    assert AP.apply(root) == []                                      # idempotent
    shutil.copy(KIT / "files/selfsaz_service/vault.py", root / "selfsaz_service/vault.py")
    shutil.copy(KIT / "files/selfsaz_service/guards.py", root / "selfsaz_service/guards.py")
    shutil.copy(KIT / "files/selfsaz_service/supervisor.py", root / "selfsaz_service/supervisor.py")
    code = f'''
import os, json
os.environ.update(SELFSaz_HOME={str(root)!r}, SELFSaz_API_KEY="k", SELFSaz_SESSION_KEY="{KEY}")
from fastapi.testclient import TestClient
from selfsaz_service.app import app
c = TestClient(app); h = {{"X-API-Key": "k"}}
assert c.get("/health").json()["service"] == "selfsaz"
r = c.post("/v1/accounts", headers=h, json={{"owner_id": 5, "session_string": "S" * 40, "api_id": 1, "api_hash": "h" * 12}})
assert r.status_code == 200, r.text
import sqlite3
v = sqlite3.connect({str(root / "data" / "selfsaz.sqlite3")!r}).execute("select session_secret from accounts").fetchone()[0]
assert v.startswith("v2:"), v
print("OK")
'''
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(root)})
    assert "OK" in out.stdout, out.stdout + out.stderr
