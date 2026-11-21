"""PATCH_PSEL_ROLE_BUTTON_V1 — خطایِ «نقشِ ادمین انتخاب نشده» (پاسارگاردِ جدید) دکمهٔ مستقیمِ انتخابِ نقش می‌دهد (تیکتِ ساجد)."""
from types import SimpleNamespace as NS

import pytest

import tenant
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller import user as U


class FakeApi:
    def __init__(self, roles):
        self.roles = roles

    async def list_roles(self):
        return self.roles


@pytest.mark.asyncio
async def test_service_error_contains_marker_with_role_names(monkeypatch):
    async def get_setting(bot_id, key, default=""):
        return ""
    monkeypatch.setattr(tenant, "get_setting", get_setting)
    api = FakeApi([{"id": 1, "name": "owner", "is_owner": True}, {"id": 2, "name": "administrator"}, {"id": 3, "name": "operator"}])
    with pytest.raises(SV.PanelError) as e:
        await SV.role_for(api, NS(bot_id=1, id=7))
    assert U.ROLE_NOT_CHOSEN_MARK in str(e.value) and "administrator#2" in str(e.value) and "operator#3" in str(e.value)


@pytest.mark.asyncio
async def test_owner_notice_has_role_button_only_for_that_error(monkeypatch):
    sent = []

    async def notify(bot, row, text, kb=None, photo_file_id=None):
        sent.append((text, kb))
    monkeypatch.setattr(tenant, "_notify_admins", notify)
    import log_throttle
    monkeypatch.setattr(log_throttle, "should_log", lambda *a, **k: True)
    row = NS(id=9)
    await U.tell_owner_failure(None, row, 123, "ساختِ پنلِ تست", f"{U.ROLE_NOT_CHOSEN_MARK}؛ از «🛡 نقشِ ادمینِ پنل» … نقش‌هایِ موجود: administrator#2، operator#3")
    text, kb = sent[-1]
    assert kb is not None and kb.inline_keyboard[0][0].callback_data == "psel:role"
    assert "Operator" in text and "اتصال به پنل برقرار است" in text and "123" in text
    await U.tell_owner_failure(None, row, 123, "ساختِ پنلِ تست", "Cannot connect to host x:8000")
    text2, kb2 = sent[-1]
    assert kb2 is None and "بررسی کن" in text2                                   # خطایِ عادی مثلِ قبل
