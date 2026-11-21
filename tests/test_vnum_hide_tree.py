import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


@pytest.mark.asyncio
async def test_hide_vnum_in_customer_tree(monkeypatch):
    svcs = [NS(id=1, service_id="ozv:ozv1:US", name="🇺🇸 آمریکا", category="شمارهٔ مجازیِ تلگرام",
               type_key="virtual_number", platform_key="telegram", is_active=True),
            NS(id=2, service_id="55", name="ممبر", category="x", type_key="member",
               platform_key="telegram", is_active=True)]

    class Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def scalars(self, *a, **k): return NS(all=lambda: svcs)
    monkeypatch.setattr(smm, "Session", lambda: Sess())

    async def _ens(): return None

    async def _cls(bot_id, items):
        return {"n": [v.id for v in items]}
    monkeypatch.setattr(smm, "ensure_smm_columns", _ens)
    monkeypatch.setattr(smm, "classified_tree", _cls)
    flag = {"v": "0"}

    async def _gs(b, k, d=""): return flag["v"]
    monkeypatch.setattr(smm, "_get_setting", _gs)
    tree, by_pk = await smm._user_active_tree(1)
    assert tree["n"] == [1, 2]
    flag["v"] = "1"
    tree, by_pk = await smm._user_active_tree(1)
    assert tree["n"] == [2] and set(by_pk) == {2}
