"""PATCH_ADMIN_CB_GUARD_V1: callbackهایِ مدیریتی (sadm:/sla:/badm:/…) فقط برایِ
مالک/ادمینِ همان ربات اجرا می‌شوند؛ callback_data را تلگرام اعتبارسنجی نمی‌کند."""
from types import SimpleNamespace

import pytest

import tenant


class _Cb(SimpleNamespace):
    async def answer(self, *a, **k):
        self.answered = True


def _patch(monkeypatch, admin: bool):
    async def get_bot_row(_bot):
        return SimpleNamespace(id=7, owner_id=1)

    async def is_owner_or_admin(_bid, _uid, _owner):
        return admin

    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_owner_or_admin)


async def _run(data, admin, monkeypatch):
    _patch(monkeypatch, admin)
    called = []

    async def handler(event, d):
        called.append(event.data)

    await tenant._admin_cb_guard_mw(handler, _Cb(data=data, from_user=SimpleNamespace(id=99)),
                                    {"bot": object()})
    return called


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["sadm:del:5", "sla:del:3:9", "badm:slot:toggle:2",
                                  "poll:del:4", "review:approve:8", "tadm:menu", "bc:send"])
async def test_admin_prefixes_blocked_for_normal_users(monkeypatch, data):
    assert await _run(data, False, monkeypatch) == []
    assert await _run(data, True, monkeypatch) == [data]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["poll:vote:1:2", "shop:buy:wallet:3", "vsvc:4", "review:list"])
async def test_user_callbacks_pass_through(monkeypatch, data):
    assert await _run(data, False, monkeypatch) == [data]
