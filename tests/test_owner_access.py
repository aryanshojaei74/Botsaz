import pytest
from types import SimpleNamespace as NS

from tenant.shared import owner_access as OA


def test_is_owner_uid(monkeypatch):
    import config
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 999)
    assert OA.is_owner_uid(5, 5)
    assert OA.is_owner_uid(999, 5)             # ادمینِ پلتفرم (پشتیبانی) = مالک
    assert not OA.is_owner_uid(6, 5)
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 0)
    assert not OA.is_owner_uid(0, 5)           # ۰ یعنی تنظیم نشده؛ هیچ‌کس مالک نمی‌شود
    assert not OA.is_owner_uid(None, 5)


@pytest.mark.asyncio
async def test_tgt_owner_strict_lets_platform_admin_through(monkeypatch):
    import config
    from tenant.bots.virtual_service import smm as S
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 999)
    row = NS(id=1, owner_id=5)

    async def own(cb, bot):
        return row
    monkeypatch.setattr(S, "_owner", own)

    class Cb:
        def __init__(self, uid):
            self.from_user = NS(id=uid)
            self.alerts = []

        async def answer(self, *a, **k):
            self.alerts.append(a)
    assert await S._tgt_owner_strict(Cb(999), None) is row
    assert await S._tgt_owner_strict(Cb(5), None) is row
    c = Cb(77)
    assert await S._tgt_owner_strict(c, None) is None and c.alerts
