import re
import pytest
import tenant
from tenant.bots.vpn import vpn_user as vu


def test_random_name_shape_english_with_digits():
    for _ in range(300):
        n = vu._random_cfg_name()
        assert re.fullmatch(r"[A-Z][a-z][2-9]{2}[a-z]{4}", n), n      # مثل Dg87gtop
    assert len({vu._random_cfg_name() for _ in range(200)}) > 190


@pytest.mark.asyncio
async def test_auto_name_off_by_default(monkeypatch):
    async def _gs(b, k, d=""): return d
    monkeypatch.setattr(tenant, "get_setting", _gs)
    assert await vu._auto_cfg_name(1, "x") is None


@pytest.mark.asyncio
async def test_auto_name_when_enabled_is_random_english(monkeypatch):
    async def _gs(b, k, d=""): return "1" if k == "cfg_name_auto" else d
    monkeypatch.setattr(tenant, "get_setting", _gs)
    n = await vu._auto_cfg_name(1, "x")
    assert re.fullmatch(r"[A-Za-z0-9]{8}", n) and not re.search(r"[؀-ۿ]", n)
