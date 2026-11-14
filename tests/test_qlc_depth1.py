import asyncio
from tenant.bots.virtual_service import smm


class V:
    def __init__(self, i): self.id = i; self.name = f"s{i}"


def test_all_content():
    t, kb = asyncio.run(smm._qlc_all_content(1, 7, [V(2), V(1)]))
    cbs = [r[0].callback_data for r in kb.inline_keyboard]
    assert cbs[:2] == ["smmu:svcbraw:1:7", "smmu:svcbraw:2:7"]
    assert cbs[-1] == "mbtn:home"
    assert smm._QLC_DEPTH_TXT["1"] == "فقط سرویس"
