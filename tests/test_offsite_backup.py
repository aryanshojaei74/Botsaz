"""بک‌آپِ رمزگذاری‌شدهٔ بیرونِ سرور: رمزنگاری، تکه‌کردن، ارسال، و قواعدِ ایمنی."""
import asyncio
import datetime as dt
import os

import pytest

import backup_crypto as BC
import platform_offsite_backup as OB


def test_roundtrip_and_wrong_passphrase(tmp_path):
    src = tmp_path / "a.bin"
    data = os.urandom(BC.CHUNK * 2 + 123)            # بیش از ۲ تکه
    src.write_bytes(data)
    enc, out = tmp_path / "a.enc", tmp_path / "a.out"
    BC.encrypt_file(str(src), str(enc), "correct horse battery")
    assert enc.read_bytes()[:5] == BC.MAGIC and data[:32] not in enc.read_bytes()
    BC.decrypt_file(str(enc), str(out), "correct horse battery")
    assert out.read_bytes() == data
    with pytest.raises(BC.BadPassphraseOrCorrupt):
        BC.decrypt_file(str(enc), str(tmp_path / "bad.out"), "wrong passphrase!!")
    assert not (tmp_path / "bad.out").exists() and not (tmp_path / "bad.out.part").exists()


def test_empty_file_and_tamper_and_truncation(tmp_path):
    src, enc = tmp_path / "e.bin", tmp_path / "e.enc"
    src.write_bytes(b"")
    BC.encrypt_file(str(src), str(enc), "pass-pass-pass")
    BC.decrypt_file(str(enc), str(tmp_path / "e.out"), "pass-pass-pass")
    assert (tmp_path / "e.out").read_bytes() == b""
    src.write_bytes(b"hello world" * 1000)
    BC.encrypt_file(str(src), str(enc), "pass-pass-pass")
    raw = bytearray(enc.read_bytes())
    raw[60] ^= 0xFF                                   # دست‌کاری
    (tmp_path / "t.enc").write_bytes(bytes(raw))
    with pytest.raises(BC.BadPassphraseOrCorrupt):
        BC.decrypt_file(str(tmp_path / "t.enc"), str(tmp_path / "t.out"), "pass-pass-pass")
    (tmp_path / "c.enc").write_bytes(enc.read_bytes()[:-40])      # بریده‌شدنِ انتها
    with pytest.raises(BC.BadPassphraseOrCorrupt):
        BC.decrypt_file(str(tmp_path / "c.enc"), str(tmp_path / "c.out"), "pass-pass-pass")


def test_split_and_join(tmp_path):
    f = tmp_path / "x.enc"
    data = os.urandom(10_000)
    f.write_bytes(data)
    assert BC.split_file(str(f), 20_000) == [str(f)]
    parts = BC.split_file(str(f), 4_000)
    assert [os.path.basename(p) for p in parts] == ["x.enc.part01of03", "x.enc.part02of03", "x.enc.part03of03"]
    BC.join_parts(list(reversed(parts)), str(tmp_path / "joined"))            # ترتیبِ ورودی مهم نیست
    assert (tmp_path / "joined").read_bytes() == data


class FakeBot:
    def __init__(self):
        self.docs, self.msgs = [], []

    async def send_document(self, chat, doc, caption=None, **kw):
        self.docs.append((chat, os.path.basename(doc.path), os.path.getsize(doc.path), caption, open(doc.path, "rb").read()))

    async def send_message(self, chat, text, **kw):
        self.msgs.append((chat, text))


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = {}
    bdir = tmp_path / "backups"
    bdir.mkdir()
    dump = bdir / "botsaz-pg-20261022-0300.dump"
    dump.write_bytes(os.urandom(50_000))
    envf = tmp_path / ".env"
    envf.write_text("SECRET=1\n")

    async def _get(k, d=""):
        return store.get(k, d)

    async def _set(k, v):
        store[k] = v

    monkeypatch.setattr(OB, "_get", _get)
    monkeypatch.setattr(OB, "_set", _set)
    monkeypatch.setattr(OB, "BACKUP_DIR", str(bdir))
    monkeypatch.setattr(OB, "ENV_PATH", str(envf))
    monkeypatch.setattr(OB, "PLATFORM_ADMIN_ID", 42)
    return store, dump


def test_refuses_without_passphrase(env):
    store, _ = env
    bot = FakeBot()
    ok, msg = asyncio.run(OB.run_backup(bot))
    assert not ok and "رمز" in msg and not bot.docs


def test_sends_encrypted_parts_and_env_and_records_last(env, tmp_path):
    store, dump = env
    store[OB.K_PASS] = "a-long-secret-pass"
    bot = FakeBot()
    now = dt.datetime.now()
    os.utime(dump, (now.timestamp(), now.timestamp()))       # dumpِ تازه ⇒ pg_dump جدید لازم نیست
    ok, info = asyncio.run(OB.run_backup(bot, part_bytes=20_000, now=now))
    assert ok and store[OB.K_LAST] == now.isoformat() and "MB" in info
    names = [d[1] for d in bot.docs]
    dump_parts = [n for n in names if "-dump" in n]
    assert len(dump_parts) >= 3 and all(".part" in n for n in dump_parts)    # ~50KB/20KB
    env_docs = [d for d in bot.docs if "-env" in d[1]]
    assert len(env_docs) == 1 and all(d[0] == 42 for d in bot.docs)
    # محتوا واقعاً رمزشده است و با رمز باز می‌شود
    assert b"SECRET=1" not in env_docs[0][4]
    (tmp_path / "e.enc").write_bytes(env_docs[0][4])
    BC.decrypt_file(str(tmp_path / "e.enc"), str(tmp_path / "e.out"), "a-long-secret-pass")
    assert (tmp_path / "e.out").read_text() == "SECRET=1\n"
    # تکه‌هایِ dump دوباره سرِ هم می‌شوند و بازشدنی‌اند
    joined = tmp_path / "joined.enc"
    parts = []
    for i, d in enumerate(d for d in bot.docs if "-dump" in d[1]):
        p = tmp_path / f"p{i:02d}"
        p.write_bytes(d[4])
        parts.append(str(p))
    BC.join_parts(parts, str(joined))
    BC.decrypt_file(str(joined), str(tmp_path / "d.out"), "a-long-secret-pass")
    assert (tmp_path / "d.out").read_bytes() == dump.read_bytes()
    assert "SHA256" in bot.docs[0][3]


def test_env_can_be_excluded_and_custom_destination(env):
    store, dump = env
    store.update({OB.K_PASS: "a-long-secret-pass", OB.K_ENV: "0", OB.K_CHAT: "-100777"})
    now = dt.datetime.now()
    os.utime(dump, (now.timestamp(), now.timestamp()))
    bot = FakeBot()
    ok, _ = asyncio.run(OB.run_backup(bot, now=now))
    assert ok and all(d[0] == -100777 for d in bot.docs) and not [d for d in bot.docs if "-env" in d[1]]


def test_due_logic(env):
    store, _ = env
    now = dt.datetime(2026, 10, 22, 12, 0)
    assert not asyncio.run(OB.due(now))                       # خاموش
    store[OB.K_EN] = "1"
    assert asyncio.run(OB.due(now))                           # هرگز نرفته
    store[OB.K_LAST] = (now - dt.timedelta(hours=3)).isoformat()
    assert not asyncio.run(OB.due(now))                       # بازهٔ ۲۴ساعته نرسیده
    store[OB.K_LAST] = (now - dt.timedelta(hours=25)).isoformat()
    assert asyncio.run(OB.due(now))
    store[OB.K_HOURS] = "6"
    store[OB.K_LAST] = (now - dt.timedelta(hours=7)).isoformat()
    assert asyncio.run(OB.due(now))


def test_no_local_dump(monkeypatch, env, tmp_path):
    store, dump = env
    store[OB.K_PASS] = "a-long-secret-pass"
    dump.unlink()
    monkeypatch.setattr(OB, "_ensure_fresh_dump", lambda: asyncio.sleep(0, result=None))
    ok, msg = asyncio.run(OB.run_backup(FakeBot()))
    assert not ok and "بک‌آپِ محلی" in msg
