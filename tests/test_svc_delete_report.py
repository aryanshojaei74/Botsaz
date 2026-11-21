import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from tenant.shared import svc_delete_report as R

GB = 1024 ** 3


def _acc():
    return NS(user_telegram_id=55, email="abc123", svc_code="111222333", sub_link="https://x/sub/AAA",
              expires_at=datetime(2026, 12, 1), created_at=datetime(2026, 11, 1), paid_toman=120000)


def test_build_report_has_last_info():
    t = R.build_report(user_id=55, username="ali", full_name="Ali", email="abc123", svc_code="111222333", title="سرویس",
                       volume="30 گیگ", duration="30 روز", panel="پنل ۱", used_gb=9, total_gb=30,
                       expires_at=datetime(2026, 12, 1), link="https://x/sub/AAA", paid=120000)
    for s in ("#حذف_سرویس", "https://x/sub/AAA", "abc123", "111222333", "9 گیگ از 30 گیگ", "120,000", "پنل ۱", "55", "@ali"):
        assert s in t
    t2 = R.build_report(user_id=55, username=None, full_name=None, email="e", svc_code=None, title="", volume="نامحدود",
                        duration="بی‌انقضا", panel=None, show_link=False, link="https://x", ident_mode="username")
    assert "https://x" not in t2 and "<code>55</code>" in t2 and "(نامحدود)" not in t2


@pytest.mark.asyncio
async def test_report_posts_to_channel_and_never_raises(monkeypatch):
    import tenant
    store = {"svc_del_channel": "-1001", "svc_del_admins": "0", "svc_del_link": "1"}

    async def gs(bid, k, d=""):
        return store.get(k, d)
    monkeypatch.setattr(tenant, "get_setting", gs)
    sent = []

    class Bot:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))
    await R.report(Bot(), 1, _acc(), username="ali", full_name="Ali", usage={"up": 2 * GB, "down": 7 * GB, "total": 30 * GB},
                   panel_name="P", title="T", volume="30 گیگ", duration="30 روز")
    assert sent and sent[0][0] == -1001 and "9 گیگ از 30 گیگ" in sent[0][1] and "https://x/sub/AAA" in sent[0][1]

    class Boom:
        async def send_message(self, *a, **k):
            raise RuntimeError("no rights")
    await R.report(Boom(), 1, _acc())                 # نباید استثنا بیندازد
    store["svc_del_channel"] = ""
    sent.clear()
    await R.report(Bot(), 1, _acc())
    assert not sent                                   # هیچ مقصدی تنظیم نیست ⇒ چیزی نمی‌رود
