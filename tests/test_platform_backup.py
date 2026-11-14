"""PATCH_PLATFORM_BACKUP_V1."""
import os

import pytest

import platform_backup as pb


def test_split_file_parts_and_reassemble(tmp_path):
    p = tmp_path / "x.dump"
    data = os.urandom(1000)
    p.write_bytes(data)
    parts = pb.split_file(str(p), part_size=300)
    assert len(parts) == 4 and parts[0].endswith(".part001")
    assert b"".join(open(x, "rb").read() for x in parts) == data
    assert pb.split_file(str(p), part_size=5000) == [str(p)]


def test_pg_params_strips_driver():
    prm = pb._pg_params("postgresql+asyncpg://u:p%40ss@db.host:6543/botsaz")
    assert prm == {"host": "db.host", "port": "6543", "user": "u", "password": "p@ss", "dbname": "botsaz"}


def test_owner_kb_buttons_target_tenant_callbacks():
    cbs = [b.callback_data for r in pb.owner_kb().inline_keyboard for b in r]
    assert cbs == ["tadm:bkfull:export", "tadm:bkfull:auto7"]


@pytest.mark.asyncio
async def test_announce_goes_through_each_bot_and_counts_failures(monkeypatch):
    class B:
        def __init__(self, i, owner):
            self.id, self.owner_id, self.token = i, owner, f"{i}:T"

    async def bots():
        return [B(1, 11), B(2, 22), B(3, 33)]
    monkeypatch.setattr(pb, "_owner_bots", bots)
    sent = []

    class FakeBot:
        def __init__(self, token, default=None):
            self.token = token
            self.session = type("S", (), {"close": staticmethod(lambda: _noop())})()

        async def send_message(self, chat_id, text, reply_markup=None):
            if chat_id == 22:
                raise RuntimeError("blocked")
            sent.append((self.token, chat_id, reply_markup))
    monkeypatch.setattr(pb, "Bot", FakeBot)
    res = await pb.announce_to_owners()
    assert res == {"sent": 2, "failed": 1, "total": 3}
    assert sent[0][0] == "1:T" and sent[0][1] == 11 and sent[0][2] is not None


async def _noop():
    return None
