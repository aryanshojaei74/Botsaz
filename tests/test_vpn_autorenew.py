"""PATCH_VPN_AUTORENEW_V1 — تمدیدِ خودکار از کیف‌پول."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import autorenew as AR
from tenant.bots.vpn import vpn_user as VU


class FakeClient:
    calls: list = []
    fail = False

    async def renew(self, email, days):
        if FakeClient.fail:
            raise RuntimeError("panel down")
        FakeClient.calls.append(("renew", email, days))

    async def add_traffic(self, email, gb):
        FakeClient.calls.append(("gb", email, gb))

    async def close(self):
        pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, AR, VU):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    FakeClient.calls, FakeClient.fail = [], False
    now = datetime.utcnow()
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=500, token="4:T", username="vbot", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=4, telegram_id=1001, full_name="Ali", username="ali", balance=500_000))
        s.add(models.VpnPanel(id=2, bot_id=4, panel_key="marzban", base_url="https://p.x", username="u", password="p", enabled=True))
        s.add(models.VpnPlan(id=10, bot_id=4, panel_key="marzban", title="۳۰ گیگ", volume_gb=30, days=30, price=200_000, active=True))
        s.add(models.VpnPlan(id=11, bot_id=4, panel_key="marzban", title="۱۰۰ گیگ", volume_gb=100, days=30, price=450_000, active=True))
        s.add(models.PanelAccount(id=5, bot_id=4, user_telegram_id=1001, email="e5", panel_key="marzban", volume_gb=30, days=30,
                                  plan_title="۳۰ گیگ", status="active", expires_at=now + timedelta(hours=6)))
        s.add(models.VpnAutoRenew(bot_id=4, account_id=5, user_tg=1001, plan_id=10, enabled=True))
        await s.commit()
    await tenant.set_setting(4, "autorenew_enabled", "1")
    yield maker, now
    await engine.dispose()


async def _bal(maker):
    async with maker() as s:
        return int((await s.scalar(select(models.BotUser))).balance)


@pytest.mark.asyncio
async def test_renews_near_expiry_once_per_cycle(env):
    maker, now = env
    out = await AR.run_bot_once(4, None, now, client_factory=FakeClient)
    assert len(out) == 1 and "خودکار تمدید شد" in out[0][1] and out[0][0] == 1001
    assert ("renew", "e5", 30) in FakeClient.calls and ("gb", "e5", 30) in FakeClient.calls
    assert await _bal(maker) == 300_000
    async with maker() as s:
        a = await s.get(models.PanelAccount, 5)
    assert a.expires_at > now + timedelta(days=29) and a.volume_gb == 60
    # دورِ بعد: انقضا جلو رفته ⇒ تمدیدِ دوباره نمی‌شود
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []
    assert await _bal(maker) == 300_000


@pytest.mark.asyncio
async def test_no_funds_warns_once_without_charging(env):
    maker, now = env
    async with maker() as s:
        u = await s.scalar(select(models.BotUser))
        u.balance = 1000
        await s.commit()
    out = await AR.run_bot_once(4, None, now, client_factory=FakeClient)
    assert len(out) == 1 and "کافی نیست" in out[0][1] and not FakeClient.calls and await _bal(maker) == 1000
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []        # همان چرخه تکرار نمی‌شود


@pytest.mark.asyncio
async def test_panel_failure_refunds_and_retries_next_round(env):
    maker, now = env
    FakeClient.fail = True
    out = await AR.run_bot_once(4, None, now, client_factory=FakeClient)
    assert len(out) == 1 and "برگشت" in out[0][1] and await _bal(maker) == 500_000
    FakeClient.fail = False
    out = await AR.run_bot_once(4, None, now, client_factory=FakeClient)               # خطای گذرا ⇒ دورِ بعد دوباره
    assert len(out) == 1 and "خودکار تمدید شد" in out[0][1] and await _bal(maker) == 300_000


@pytest.mark.asyncio
async def test_skips_far_future_long_expired_disabled_or_feature_off(env):
    maker, now = env
    async with maker() as s:
        a = await s.get(models.PanelAccount, 5)
        a.expires_at = now + timedelta(days=5)
        await s.commit()
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []        # هنوز زود است
    async with maker() as s:
        a = await s.get(models.PanelAccount, 5)
        a.expires_at = now - timedelta(days=10)
        await s.commit()
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []        # خیلی وقت پیش تمام شده
    async with maker() as s:
        a = await s.get(models.PanelAccount, 5)
        a.expires_at, a.status = now + timedelta(hours=2), "disabled"
        await s.commit()
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []        # مشتری غیرفعالش کرده
    async with maker() as s:
        a = await s.get(models.PanelAccount, 5)
        a.status = "active"
        await s.commit()
    await tenant.set_setting(4, "autorenew_enabled", "0")
    tenant._SETTING_CACHE.clear()
    assert await AR.run_bot_once(4, None, now, client_factory=FakeClient) == []        # مالک خاموش کرده
    assert not FakeClient.calls


@pytest.mark.asyncio
async def test_pick_plan_prefers_same_volume_then_nearest(env):
    maker, now = env
    async with maker() as s:
        acc = await s.get(models.PanelAccount, 5)
        assert (await AR.pick_plan(s, 4, acc, "marzban", 1001)).id == 10
        acc.volume_gb = 90
        assert (await AR.pick_plan(s, 4, acc, "marzban", 1001)).id == 11                 # نزدیک‌ترین حجم
        acc.volume_gb = 0
        assert (await AR.pick_plan(s, 4, acc, "marzban", 1001)).id in (10, 11)           # نامحدود ⇒ پلنِ هم‌نوعِ موجود
    assert await AR.pick_plan(s, 4, NS(volume_gb=30, days=30), "nonexistent", 1001) is None


class _Cb:
    def __init__(self, data, uid=1001):
        self.data, self.from_user, self.alerts = data, NS(id=uid), []
        outer = self

        class M:
            out = []

            async def edit_text(self, text, **k):
                self.out.append((text, k.get("reply_markup")))

            answer = edit_text
        self.message = M()
        self.message.out = []

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_customer_toggle_flow_and_visibility(env):
    maker, now = env
    bot = NS(token="4:T")
    btn = await AR.button_for(4, 5)
    assert "روشن" in btn.text
    cb = _Cb("vautoset:5:0")
    await AR.auto_set(cb, bot)
    async with maker() as s:
        assert (await s.scalar(select(models.VpnAutoRenew))).enabled is False
    assert "خاموش" in (await AR.button_for(4, 5)).text
    cb = _Cb("vautoset:5:1")
    await AR.auto_set(cb, bot)
    async with maker() as s:
        r = await s.scalar(select(models.VpnAutoRenew))
    assert r.enabled and r.plan_id == 10
    # سرویسِ شخصِ دیگر قابلِ دست‌کاری نیست
    cb = _Cb("vautoset:5:0", uid=999)
    await AR.auto_set(cb, bot)
    async with maker() as s:
        assert (await s.scalar(select(models.VpnAutoRenew))).enabled is True
    # مالک خاموش کند ⇒ دکمه نمی‌آید
    await tenant.set_setting(4, "autorenew_enabled", "0")
    tenant._SETTING_CACHE.clear()
    assert await AR.button_for(4, 5) is None
