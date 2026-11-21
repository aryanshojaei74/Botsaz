import pytest
from tenant.bots.vpn import vpn_admin


class _St:
    async def get_data(self):
        return {"plan_panel_key": "sanai"}

    async def clear(self):
        raise AssertionError("نباید به clear برسد")


@pytest.mark.asyncio
async def test_empty_state_is_ignored_without_keyerror():
    await vpn_admin._finalize_vpn_plan(object(), 1, _St(), type("R", (), {"id": 1})(), None)
