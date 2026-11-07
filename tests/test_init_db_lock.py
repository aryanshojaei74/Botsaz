"""
تست‌هایِ قفلِ init_db (PATCH_INIT_DB_ADVISORY_LOCK_V1).

ریشه: چند سرویس هم‌زمان ری‌استارت می‌شدند و همه CREATE TABLE می‌زدند ⇒ همه‌شان به‌جز یکی با
«duplicate key ... pg_type_typname_nsp_index» می‌افتادند. (روی Postgresِ واقعی بازتولید و
رفع شد: ۲۴ شکست از ۲۸ راه‌اندازی ⇒ صفر.) این‌جا بدونِ Postgres، ترتیبِ دستورها را چک
می‌کنیم تا کسی قفل را برنگرداند یا بعد از create_all نبرد.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import config
import models


class _FakeConn:
    def __init__(self, dialect: str):
        self.dialect = SimpleNamespace(name=dialect)
        self.calls: list[str] = []

    async def exec_driver_sql(self, sql, *a, **k):
        self.calls.append(("sql", sql))

    async def run_sync(self, fn, *a, **k):
        self.calls.append(("run_sync", getattr(fn, "__name__", str(fn))))
        return None


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def begin(self):
        conn = self._conn

        @contextlib.asynccontextmanager
        async def _cm():
            yield conn
        return _cm()


@pytest.mark.asyncio
async def test_postgres_takes_advisory_lock_before_any_ddl(monkeypatch):
    conn = _FakeConn("postgresql")
    monkeypatch.setattr(config, "engine", _FakeEngine(conn))
    await config.init_db()

    kinds = [c for c in conn.calls]
    sqls = [c[1] for c in kinds if c[0] == "sql"]
    lock_idx = next(i for i, c in enumerate(kinds) if c[0] == "sql" and "pg_advisory_xact_lock" in c[1])
    first_ddl = next(i for i, c in enumerate(kinds) if c[0] == "run_sync")
    assert lock_idx < first_ddl, "قفل باید قبل از create_all/مهاجرت‌ها گرفته شود"
    assert f"pg_advisory_xact_lock({config._INIT_DB_LOCK_KEY})" in " ".join(sqls)


@pytest.mark.asyncio
async def test_lock_timeout_applies_only_to_acquiring_the_lock(monkeypatch):
    """سقفِ انتظار باید قبل از قفل گذاشته و *بعد از آن* برداشته شود تا ALTER/CREATEهایِ
    بعدی (که ممکن است پشتِ یک قفلِ عادی معطل شوند) با آن محدود نشوند."""
    conn = _FakeConn("postgresql")
    monkeypatch.setattr(config, "engine", _FakeEngine(conn))
    await config.init_db()
    sqls = [c[1] for c in conn.calls if c[0] == "sql"]
    i_set = next(i for i, q in enumerate(sqls) if "lock_timeout = '" in q)
    i_lock = next(i for i, q in enumerate(sqls) if "pg_advisory_xact_lock" in q)
    i_reset = next(i for i, q in enumerate(sqls) if "lock_timeout = 0" in q)
    assert i_set < i_lock < i_reset


@pytest.mark.asyncio
async def test_lock_is_transaction_scoped_not_session_scoped():
    """xact_lock خودکار با پایانِ تراکنش آزاد می‌شود؛ نسخهٔ session-level اگر unlock
    فراموش شود کانکشنِ poolشده را برایِ همیشه قفل نگه می‌داشت."""
    import inspect
    src = inspect.getsource(config.init_db)
    assert "pg_advisory_xact_lock" in src
    assert "pg_advisory_lock(" not in src


@pytest.mark.asyncio
async def test_sqlite_path_is_untouched(monkeypatch):
    conn = _FakeConn("sqlite")
    monkeypatch.setattr(config, "engine", _FakeEngine(conn))
    await config.init_db()
    assert not any("advisory" in c[1] or "lock_timeout" in c[1]
                   for c in conn.calls if c[0] == "sql")
    assert any(c[0] == "run_sync" for c in conn.calls)      # create_all هنوز اجرا می‌شود


@pytest.mark.asyncio
async def test_init_db_still_creates_schema_on_a_real_sqlite_engine(monkeypatch, tmp_path):
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.ext.asyncio import create_async_engine
    url = f"sqlite+aiosqlite:///{tmp_path/'t.db'}"
    eng = create_async_engine(url)
    monkeypatch.setattr(config, "engine", eng)
    monkeypatch.setattr(config, "DATABASE_URL", url)
    await config.init_db()
    async with eng.connect() as c:
        names = await c.run_sync(lambda sc: set(sa_inspect(sc).get_table_names()))
    await eng.dispose()
    assert "fragment_wallet_archive" in names and "bots" in names
    assert len(names) >= len(models.Base.metadata.tables) - 2
