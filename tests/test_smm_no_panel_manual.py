import pytest
from tenant.bots.virtual_service import smm


@pytest.mark.asyncio
async def test_panel_configured_cases(monkeypatch):
    creds = {}

    async def fake(bot_id, pk): return creds[pk]
    monkeypatch.setattr(smm, "_panel_creds", fake)
    creds[None] = ("", "", "پنل پیش‌فرض")                 # پنلِ پیش‌فرضِ حذف‌شده
    creds[3] = ("", "", "—")                               # پنل پاک شده (panel_pk هنوز ثبت است)
    creds[4] = ("https://p.example/api/v2", "KEY", "x")
    creds[5] = ("https://p.example/api/v2", "  ", "x")     # کلیدِ خالی
    assert await smm._panel_configured(1, None) is False
    assert await smm._panel_configured(1, 3) is False
    assert await smm._panel_configured(1, 4) is True
    assert await smm._panel_configured(1, 5) is False


def test_both_auto_sites_check_panel_first():
    src = open("tenant/bots/virtual_service/smm.py", encoding="utf-8").read()
    assert src.count("await _panel_configured(row.id, ") == 2
