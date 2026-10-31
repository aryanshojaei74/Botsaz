"""PATCH_TRIAL_DELETE_RETRY_V1 — باگ #190: تستِ رایگان باید واقعاً از پنل پاک شود؛ خطایِ پنل ⇒ دوباره تلاش، نه حذفِ ردیف."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import trial_cleanup as TC


class FakeClient:
    fail_delete = True
    deleted: list = []
    gone_ms = int((datetime.now() - timedelta(hours=10)).timestamp() * 1000)

    async def list_clients(self):
        return [{"email": "t1", "expiryTime": FakeClient.gone_ms, "traffic": {"up": 0, "down": 0}}]

    async def delete_account(self, email):
        if FakeClient.fail_delete:
            raise RuntimeError("429 Too Many Requests")
        FakeClient.deleted.append(email)

    async def close(self):
        pass


class FakeTBot:
    sent: list = []

    def __init__(self, token, default=None):
        class S:
            @staticmethod
            async def close():
                pass
        self.session = S

    async def send_message(self, chat, text, **k):
        FakeTBot.sent.append((chat, text))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, TC):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(TC, "_make_client", lambda pf: FakeClient())
    monkeypatch.setattr(TC, "Bot", FakeTBot)
    FakeClient.fail_delete, FakeClient.deleted, FakeTBot.sent = True, [], []
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.VpnPanel(id=1, bot_id=9, panel_key="marzban", base_url="https://p", username="u", password="p", enabled=True))
        s.add(models.PanelAccount(id=7, bot_id=9, user_telegram_id=55, email="t1", panel_key="marzban", plan_title="تست رایگان",
                                  status="active", expires_at=datetime.utcnow() - timedelta(hours=10),
                                  trial_warned_at=datetime.utcnow() - timedelta(hours=9),
                                  created_at=datetime.utcnow() - timedelta(days=1)))
        await s.commit()
    yield maker
    await engine.dispose()


async def _rows(maker):
    async with maker() as s:
        return (await s.scalars(select(models.PanelAccount))).all()


@pytest.mark.asyncio
async def test_panel_failure_keeps_row_and_user_not_told_then_retry_deletes(env):
    await TC._process_bot(9)
    assert len(await _rows(env)) == 1 and FakeTBot.sent == [] and FakeClient.deleted == []     # حذف نشد ⇒ ردیف می‌ماند، پیامِ دروغ نمی‌رود
    FakeClient.fail_delete = False
    await TC._process_bot(9)                                                                  # دورِ بعد (پنل برگشت)
    assert FakeClient.deleted == ["t1"] and await _rows(env) == []
    assert len(FakeTBot.sent) == 1 and "حذف شد" in FakeTBot.sent[0][1]


@pytest.mark.asyncio
async def test_account_whose_panel_is_not_enabled_is_not_deleted_from_another_panel(env):
    async with env() as s:
        s.add(models.VpnPanel(id=2, bot_id=9, panel_key="sanai", base_url="https://q", username="u", password="p", enabled=True))
        a = await s.get(models.PanelAccount, 7)
        a.panel_key = "pasargad#2"                                                           # پنلِ خودش ثبت/فعال نیست
        await s.commit()
    FakeClient.fail_delete = False
    await TC._process_bot(9)
    assert FakeClient.deleted == [] and len(await _rows(env)) == 1
