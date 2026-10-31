"""PATCH_DAILY_REPORT_V1 — گزارشِ روزانهٔ مالک."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import daily_report as DR
from tenant.shared import period_stats as PS


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, DR, PS):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    now = datetime(2026, 10, 31, 6, 0)               # ۹:۳۰ به وقتِ ایران
    async with maker() as s:
        s.add(models.Bot(id=2, owner_id=500, token="2:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=2, telegram_id=11, full_name="A", balance=40_000, joined_at=now - timedelta(hours=3)))
        s.add(models.BotUser(id=2, bot_id=2, telegram_id=12, full_name="B", balance=10_000, joined_at=now - timedelta(days=5)))
        s.add(models.WalletTxn(bot_id=2, user_id=1, amount=100_000, kind="zarinpal_charge", created_at=now - timedelta(hours=2)))
        s.add(models.WalletTxn(bot_id=2, user_id=1, amount=50_000, kind="charge", created_at=now - timedelta(days=3)))
        s.add(models.SmmOrder(bot_id=2, user_telegram_id=11, service_id=1, service_name="s", link="l", quantity=10, price_toman=30_000, status="completed", created_at=now - timedelta(hours=1)))
        s.add(models.PanelAccount(bot_id=2, user_telegram_id=11, email="e", panel_key="marzban", status="active",
                                  expires_at=now + timedelta(days=2)))
        s.add(models.PanelAccount(bot_id=2, user_telegram_id=12, email="e2", panel_key="marzban", status="active",
                                  expires_at=now + timedelta(days=20)))
        await s.commit()
    yield maker, now
    await engine.dispose()


@pytest.mark.asyncio
async def test_metrics_cover_last_24h_only(env):
    maker, now = env
    m = await DR.metrics(2, now)
    assert m["new_users"] == "1" and m["charges"] == "1" and m["charge_total"] == "100,000"   # شارژِ ۳روز پیش نیست
    assert m["orders"] == "1" and m["sales"] == "30,000"
    assert m["wallet_total"] == "50,000" and m["expiring"] == "1"


def test_render_custom_template_and_unknown_vars_kept():
    out = DR.render("سلام {{date}} | {{new_users}} | {{nope}}", {"date": "۱۴۰۵/۰۸/۱۰", "new_users": "3"})
    assert out == "سلام ۱۴۰۵/۰۸/۱۰ | 3 | {{nope}}"
    assert "{{" not in DR.render("", {v: "0" for v in DR.VARS})                                # پیش‌فرض کامل پر می‌شود


@pytest.mark.asyncio
async def test_default_off_then_sends_once_per_tehran_day_at_hour(env):
    maker, now = env
    sent = []

    class B:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))
    assert await DR.due(2, now) is False                                   # پیش‌فرض خاموش
    await tenant.set_setting(2, "daily_report_on", "1")
    await tenant.set_setting(2, "daily_report_hour", "10")
    tenant._SETTING_CACHE.clear()
    assert await DR.due(2, now) is False                                   # هنوز ساعتِ ۱۰ نشده (۹:۳۰)
    later = now + timedelta(hours=1)                                       # ۱۰:۳۰
    assert await DR.run_bot_once(2, B(), 500, later) is True
    assert sent[0][0] == 500 and "گزارشِ روزانه" in sent[0][1] and "100,000" in sent[0][1]
    tenant._SETTING_CACHE.clear()
    assert await DR.run_bot_once(2, B(), 500, later + timedelta(minutes=5)) is False   # همان روز تکرار نمی‌شود
    assert len(sent) == 1
    tenant._SETTING_CACHE.clear()
    assert await DR.run_bot_once(2, B(), 500, later + timedelta(days=1)) is True        # روزِ بعد دوباره
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_owner_view_and_custom_text(env):
    maker, now = env
    text, kb = await DR._view(2)
    assert "خاموش" in text and "پیش‌فرض" in text
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"drep:toggle", "drep:hour", "drep:edit", "drep:reset", "drep:now", "pst:menu"} <= set(cbs)
    await tenant.set_setting(2, "daily_report_text", "فروش: {{sales}}")
    tenant._SETTING_CACHE.clear()
    assert await DR.build(2, now) == "فروش: 30,000"
