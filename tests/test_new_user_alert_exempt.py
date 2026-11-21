"""مدیرِ اصلی (حالتِ پشتیبانی) و مالکِ ربات اعلانِ «کاربرِ جدید» نمی‌سازند."""
from types import SimpleNamespace as NS

import pytest

import tenant


@pytest.mark.asyncio
async def test_new_user_alert_skips_platform_admin_and_owner_but_not_customers(monkeypatch):
    sent = []

    async def gs(bot_id, key, default=""):
        return "1" if key == "new_user_notify" else default

    async def notify(bot, row, text, kb=None):
        sent.append(text)

    async def is_staff(bot_id, uid, owner_id):
        return uid in (500, 600)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "_notify_admins", notify)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_staff)
    monkeypatch.setattr(tenant, "_is_platform_admin", lambda uid: uid == 999)
    row = NS(id=1, owner_id=500)
    mk = lambda tg: NS(telegram_id=tg, username="u", full_name="N")
    await tenant._new_user_alert(None, row, mk(999))       # مدیرِ اصلی
    await tenant._new_user_alert(None, row, mk(500))       # مالک
    await tenant._new_user_alert(None, row, mk(600))       # ادمینِ پشتیبانِ ربات
    assert sent == []
    await tenant._new_user_alert(None, row, mk(12345))     # مشتریِ واقعی
    assert len(sent) == 1 and "کاربرِ جدید" in sent[0]
