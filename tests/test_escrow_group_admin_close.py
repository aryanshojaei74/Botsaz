"""فدایی: «مالکِ گروه هم می‌زند می‌گوید فقط واسط» — ادمین/سازندهٔ گروهِ معامله باید بتواند پایان/لغو کند."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.shared import escrow as E

pytestmark = pytest.mark.asyncio


def _cb(uid, status):
    async def gcm(chat_id, user_id):
        if status is None:
            raise RuntimeError("no")
        return SimpleNamespace(status=status)
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, username=None), bot=SimpleNamespace(get_chat_member=gcm))


def _deal():
    # خریدار 11، فروشنده 22
    return SimpleNamespace(bot_id=1, initiator_role="buyer", initiator_tg=11, counterparty_tg=22, group_chat_id=-1001)


async def _setup(monkeypatch, mediators=()):
    async def cfg(bot_id):
        return {"mediators": list(mediators)}

    async def gbr(bid):
        return SimpleNamespace(id=1, owner_id=999)

    async def ioa(bid, uid, owner):
        return uid == owner
    monkeypatch.setattr(E, "_cfg", cfg)
    monkeypatch.setattr(tenant, "get_bot_row_by_id", gbr)
    monkeypatch.setattr(tenant, "is_owner_or_admin", ioa)


async def test_group_creator_and_admin_can_close(monkeypatch):
    await _setup(monkeypatch, mediators=["@someone_else"])
    assert await E._can_close(_cb(500, "creator"), _deal()) is True
    assert await E._can_close(_cb(501, "administrator"), _deal()) is True


async def test_plain_member_and_parties_cannot(monkeypatch):
    await _setup(monkeypatch)
    assert await E._can_close(_cb(502, "member"), _deal()) is False
    assert await E._can_close(_cb(11, "creator"), _deal()) is False      # خریدار حتی اگر ادمینِ گروه باشد نه
    assert await E._can_close(_cb(22, "administrator"), _deal()) is False
    assert await E._can_close(_cb(503, None), _deal()) is False          # خطایِ API ⇒ مثلِ قبل رد


async def test_bot_owner_still_allowed(monkeypatch):
    await _setup(monkeypatch)
    assert await E._can_close(_cb(999, "member"), _deal()) is True
