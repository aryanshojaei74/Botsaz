"""#856 — پنلِ دلاری با نرخِ دستیِ ثبت‌نشده نباید ۸ دلار را ۸ تومان نشان دهد."""
import pytest

from tenant.bots.virtual_service import smm

pytestmark = pytest.mark.asyncio


def _patch(monkeypatch, currency, mode, manual, live):
    async def cur(b, p): return currency
    async def rm(b, p): return mode
    async def man(b, p): return manual

    async def lv(*a, **k): return live
    monkeypatch.setattr(smm, "_panel_currency", cur)
    monkeypatch.setattr(smm, "_panel_rate_mode", rm)
    monkeypatch.setattr(smm, "_panel_toman_per_unit", man)
    import tenant.gateways.crypto_pay as cp
    monkeypatch.setattr(cp, "get_usd_to_toman_rate", lv)


async def test_manual_mode_without_rate_uses_live(monkeypatch):
    _patch(monkeypatch, "usd", "manual", 0.0, 120000.0)
    assert await smm._get_toman_unit_rate(1, 2) == 120000.0


async def test_manual_rate_wins(monkeypatch):
    _patch(monkeypatch, "usd", "manual", 130000.0, 120000.0)
    assert await smm._get_toman_unit_rate(1, 2) == 130000.0


async def test_toman_panel_unchanged(monkeypatch):
    _patch(monkeypatch, "toman", "manual", 0.0, 120000.0)
    assert await smm._get_toman_unit_rate(1, 2) == 1.0
