"""SelfSaz supervisor (نسخهٔ مقاوم).

نسبت به نسخهٔ قبلی:
  • backoff نمایی + توقف پس از چند کرشِ پیاپی (به‌جای راه‌اندازیِ مجدد هر ۱۰ ثانیه تا ابد)
  • سشن از طریق stdin (pipe) به worker می‌رسد؛ نه env و نه فایل
  • env کارگر whitelist است؛ کلیدهای سرویس (SESSION_KEY/API_KEY) به worker نمی‌رسند
  • هر حساب پوشهٔ کاریِ جدا دارد (insults/enemies/reactions/backups دیگر بین مشتری‌ها مشترک نیست)
  • سقف تعداد worker و سقف RSS هر worker
  • چرخشِ لاگ‌ها و heartbeat برای /health
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from selfsaz_service import vault
except ImportError:  # اجرا مستقیم: python selfsaz_service/supervisor.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from selfsaz_service import vault

ROOT = Path(os.getenv("SELFSaz_HOME", "/opt/selfsaz"))
DB = Path(os.getenv("SELFSaz_DB", str(ROOT / "data" / "selfsaz.sqlite3")))
WORKER = Path(os.getenv("SELFSaz_WORKER", str(ROOT / "selfsaz_worker.py")))
HEARTBEAT = ROOT / "data" / "supervisor.heartbeat"

LOOP_SECONDS = 10
QUICK_DEATH_SEC = 90          # مرگ زودتر از این = کرش
MAX_FAILS = 6                 # بعد از این‌همه کرشِ پیاپی دیگر راه نمی‌اندازیم تا کاربر دوباره وصل کند
MAX_ACCOUNTS = int(os.getenv("SELFSAZ_MAX_WORKERS", "80"))
RSS_LIMIT_MB = int(os.getenv("SELFSAZ_WORKER_RSS_MB", "700"))
LOG_MAX_BYTES = 8 * 1024 * 1024

_SECRET_WORDS = ("session_key", "api_key", "api_hash", "secret", "password", "token")
_KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONIOENCODING", "PYTHONUNBUFFERED")


def backoff(fails: int) -> float:
    return min(900.0, 15.0 * (2 ** max(0, fails - 1)))


def worker_env(base: dict | None = None) -> dict:
    """env حداقلی برای worker: فقط تنظیمات غیرمحرمانهٔ SELFSAZ_* و چند متغیرِ سیستمی."""
    base = dict(os.environ if base is None else base)
    out = {k: base[k] for k in _KEEP_ENV if k in base}
    for k, v in base.items():
        if k.lower().startswith("selfsaz") and not any(w in k.lower() for w in _SECRET_WORDS):
            out[k] = v
    out["SELFSaz_HOME"] = str(ROOT)
    out["SELFSAZ_SECRET_STDIN"] = "1"
    return out


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """copytruncate: کارگر با O_APPEND می‌نویسد، پس truncate امن است. یک نسخهٔ .1 نگه می‌داریم."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            shutil.copyfile(path, str(path) + ".1")
            os.truncate(path, 0)
            return True
    except OSError:
        pass
    return False


def rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{int(pid)}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    return 0.0


def is_alive(pid) -> bool:
    if not pid:
        return False
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
        return bool(cmd and "selfsaz_worker.py" in cmd)
    except (OSError, ValueError):
        return False


def init_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("""CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        bot_id INTEGER,
        session_secret TEXT,
        api_id INTEGER,
        api_hash TEXT,
        price INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 0,
        worker_pid INTEGER,
        status TEXT NOT NULL DEFAULT 'stopped',
        last_error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    for col in ("expires_at TEXT", "trial_used INTEGER NOT NULL DEFAULT 0"):
        try:
            con.execute(f"ALTER TABLE accounts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.commit()


def spawn_worker(row) -> int:
    secret = vault.unprotect(row["session_secret"])
    workdir = ROOT / "data" / "accounts" / str(int(row["owner_id"]))
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(workdir, 0o700)
    log_dir = ROOT / "data" / "worker_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"account_{row['id']}.log"
    rotate_log(log_path)
    env = worker_env()
    env["SELFSAZ_USER_ID"] = str(int(row["owner_id"]))
    with open(log_path, "ab", buffering=0) as log_file:
        p = subprocess.Popen([sys.executable, str(WORKER)], cwd=str(workdir), env=env,
                             stdin=subprocess.PIPE, stdout=log_file, stderr=subprocess.STDOUT,
                             start_new_session=True)
    payload = json.dumps({"session": secret, "api_id": int(row["api_id"] or 0),
                          "api_hash": row["api_hash"] or ""}).encode()
    try:
        p.stdin.write(payload)
        p.stdin.close()
    except OSError:
        pass
    return p.pid


class Fleet:
    """حافظهٔ کوتاه‌مدتِ supervisor: زمان شروع، تعداد کرش، زمان تلاش بعدی."""

    def __init__(self):
        self.started: dict[int, float] = {}
        self.fails: dict[int, int] = {}
        self.next_try: dict[int, float] = {}
        self.gave_up: set[int] = set()


def _expired(raw, now_dt: datetime) -> bool:
    if not raw:
        return False
    try:
        exp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= now_dt
    except ValueError:
        return False


def _set(con, aid, status, err=None, pid="keep"):
    if pid == "keep":
        con.execute("UPDATE accounts SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, err, aid))
    else:
        con.execute("UPDATE accounts SET status=?,last_error=?,worker_pid=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, err, pid, aid))


def reconcile(con, fleet: Fleet, now: float | None = None, spawn=spawn_worker, alive=is_alive,
              rss=rss_mb, kill=os.kill) -> dict:
    now = time.monotonic() if now is None else now
    now_dt = datetime.now(timezone.utc)
    stats = {"spawned": 0, "crashed": 0, "gave_up": 0, "oom": 0, "expired": 0}
    rows = con.execute("SELECT * FROM accounts").fetchall()
    running = sum(1 for r in rows if alive(r["worker_pid"]))
    for r in rows:
        aid, pid = r["id"], r["worker_pid"]
        if _expired(r["expires_at"], now_dt):
            if pid and alive(pid):
                try: kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError): pass
            if r["enabled"] or r["status"] != "expired":
                con.execute("UPDATE accounts SET enabled=0,worker_pid=NULL,status='expired',last_error='subscription expired',"
                            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
                stats["expired"] += 1
            continue
        live = alive(pid)
        if r["status"] != "error":
            fleet.gave_up.discard(aid)            # ادمین/کاربر وضعیت را عوض کرده ⇒ دوباره فرصت
        if live:
            if rss(pid) > RSS_LIMIT_MB:
                try: kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError): pass
                _set(con, aid, "queued", f"restarted: memory over {RSS_LIMIT_MB}MB", None)
                fleet.next_try[aid] = now + 30
                stats["oom"] += 1
            elif now - fleet.started.get(aid, now) > QUICK_DEATH_SEC:
                fleet.fails.pop(aid, None)        # پایدار شد
            if not r["enabled"]:
                try: kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError): pass
                _set(con, aid, "stopped", None, None)
            continue
        if not r["enabled"]:
            continue
        if aid in fleet.gave_up:
            continue
        # کارگر باید زنده باشد ولی نیست
        started = fleet.started.get(aid)
        if started is not None and now - started < QUICK_DEATH_SEC and pid:
            fleet.fails[aid] = fleet.fails.get(aid, 0) + 1
            fleet.next_try[aid] = now + backoff(fleet.fails[aid])
            stats["crashed"] += 1
            fleet.started.pop(aid, None)          # این مرگ فقط یک‌بار شمرده شود
            if fleet.fails[aid] < MAX_FAILS:
                _set(con, aid, "queued", f"worker exited early ({fleet.fails[aid]}); retrying", None)
            if fleet.fails[aid] >= MAX_FAILS:
                fleet.gave_up.add(aid)
                _set(con, aid, "error", "worker keeps crashing; reconnect the account", None)
                stats["gave_up"] += 1
                continue
        if now < fleet.next_try.get(aid, 0):
            continue
        if not r["session_secret"]:
            fleet.gave_up.add(aid)
            _set(con, aid, "error", "no session; reconnect the account", None)
            continue
        if running >= MAX_ACCOUNTS:
            _set(con, aid, "queued", "waiting for capacity")
            continue
        try:
            new_pid = spawn(r)
        except vault.VaultError:
            fleet.gave_up.add(aid)
            _set(con, aid, "error", "session cannot be decrypted; reconnect the account", None)
            continue
        except Exception as exc:                  # خطای داخلی ⇒ متن خام به کاربر نمی‌رسد
            fleet.fails[aid] = fleet.fails.get(aid, 0) + 1
            fleet.next_try[aid] = now + backoff(fleet.fails[aid])
            _set(con, aid, "error", f"spawn failed ({type(exc).__name__})", None)
            continue
        fleet.started[aid] = now
        running += 1
        stats["spawned"] += 1
        _set(con, aid, "running", None, new_pid)
    con.commit()
    return stats


def heartbeat():
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(str(int(time.time())))
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def main():
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    fleet = Fleet()
    DB.parent.mkdir(parents=True, exist_ok=True)
    while not stop["v"]:
        try:
            con = sqlite3.connect(DB, timeout=10)
            con.row_factory = sqlite3.Row
            init_db(con)
            reconcile(con, fleet)
            con.close()
        except sqlite3.Error as exc:
            print(f"supervisor db error: {exc}", flush=True)
        heartbeat()
        for _ in range(LOOP_SECONDS):
            if stop["v"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
