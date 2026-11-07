"""PATCH_PANELYAR_AUTHZ_V1: روترِ پنل‌یار فقط برایِ مالک/ادمینِ همان ربات است و
پنل‌ها به رباتِ جاری محدودند (قبلاً هر کاربری با callback_dataِ دست‌ساز
می‌توانست پنلِ رباتِ دیگری را حذف کند یا کاربرانش را ببیند)."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.shared.panel_clients import panelyar as PY


class _Cb(SimpleNamespace):
    async def answer(self, *a, **k):
        self.answered = True


def _patch(monkeypatch, *, admin: bool, bot_id: int = 7):
    async def get_bot_row(_bot):
        return SimpleNamespace(id=bot_id, owner_id=1)

    async def is_owner_or_admin(_bid, _uid, _owner):
        return admin

    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_owner_or_admin)


@pytest.mark.asyncio
async def test_non_admin_never_reaches_the_handler(monkeypatch):
    _patch(monkeypatch, admin=False)
    called = []

    async def handler(event, data):
        called.append(1)

    await PY._pyar_admin_only(handler, _Cb(from_user=SimpleNamespace(id=99)), {"bot": object()})
    assert called == []


@pytest.mark.asyncio
async def test_panel_lookup_is_scoped_to_the_current_bot(monkeypatch):
    _patch(monkeypatch, admin=True, bot_id=7)
    panels = {1: SimpleNamespace(id=1, bot_id=7), 2: SimpleNamespace(id=2, bot_id=8)}

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _model, pid):
            return panels.get(pid)

    monkeypatch.setattr(PY, "Session", _S)
    seen = {}

    async def handler(event, data):
        seen["own"] = await PY._panel_by_id(1)
        seen["foreign"] = await PY._panel_by_id(2)

    await PY._pyar_admin_only(handler, _Cb(from_user=SimpleNamespace(id=1)), {"bot": object()})
    assert seen["own"] is panels[1]
    assert seen["foreign"] is None
    assert await PY._panel_by_id(1) is None          # بیرون از هندلر: هیچ رباتی جاری نیست
