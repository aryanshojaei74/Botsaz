"""#863 — مخاطبِ پیامِ همگانی بر اساسِ سرویس + حذفِ دستیِ سرویس‌هایِ منقضی‌شده."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import svc_cleanup as sc
from tenant.shared import broadcast_runner as br


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, br, sc, tenant):
        monkeypatch.setattr(m, "Session", mk)
    now = dt.datetime.utcnow()
    async with mk() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="b", field="vpn_sales", plan="pro", status="active"))
        for tg in (10, 20, 30, 40):
            s.add(models.BotUser(bot_id=4, telegram_id=tg, balance=Decimal(0)))
        s.add(models.BotUser(bot_id=4, telegram_id=50, balance=Decimal(0), is_banned=True))
        s.add(models.PanelAccount(bot_id=4, user_telegram_id=10, email="a@x", panel_key="sanai", volume_gb=0,
                                  expires_at=now + dt.timedelta(days=5), plan_title="نامحدود"))
        s.add(models.PanelAccount(bot_id=4, user_telegram_id=20, email="b@x", panel_key="marzban", volume_gb=30,
                                  expires_at=now - dt.timedelta(days=10), plan_title="۳۰ گیگ"))
        s.add(models.PanelAccount(bot_id=4, user_telegram_id=20, email="c@x", panel_key="sanai", volume_gb=0,
                                  expires_at=now - dt.timedelta(days=1)))
        s.add(models.PanelAccount(bot_id=4, user_telegram_id=50, email="d@x", panel_key="sanai", volume_gb=10))  # بن‌شده
        await s.commit()
    yield mk
    await eng.dispose()


@pytest.mark.asyncio
async def test_audience_counts(db):
    assert await br.audience_count(4, "") == 4                       # بن‌شده کنار گذاشته می‌شود
    assert await br.audience_count(4, br.AUD_ANY) == 2               # 10 و 20
    assert await br.audience_count(4, br.AUD_UNLIMITED) == 2         # 10 و 20 (c@x نامحدود)
    assert await br.audience_count(4, "panel:marzban") == 1
    assert await br.audience_count(4, "panel:sanai") == 2
    assert await br.audience_count(4, br.AUD_NONE) == 2              # 30 و 40


@pytest.mark.asyncio
async def test_audience_options_hidden_without_services(db):
    opts = await br.audience_options(4)
    keys = [k for k, _l, _n in opts]
    assert keys[:3] == ["", "svc_any", "svc_unlimited"] and "panel:marzban" in keys and keys[-1] == "svc_none"
    assert await br.audience_options(999) == []                      # ربات بدونِ سرویس: مرحله رد می‌شود


@pytest.mark.asyncio
async def test_expired_list_and_silent_bot_only_delete(db):
    accs = await sc.expired_accounts(4)
    assert [a.email for a in accs] == ["b@x", "c@x"]                 # قدیمی‌ترین اول؛ فعال‌ها نیستند
    assert [a.email for a in await sc.expired_accounts(4, 7)] == ["b@x"]
    ok, why = await sc.delete_one(4, accs[0].id, panel_too=False)
    assert ok and why == ""
    async with db() as s:
        assert await s.scalar(select(models.PanelAccount.id).where(models.PanelAccount.email == "b@x")) is None
        assert await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == 20)) is not None   # کاربر پاک نشد


@pytest.mark.asyncio
async def test_panel_delete_failure_keeps_row_and_gone_counts_as_ok(db, monkeypatch):
    calls = {"n": 0}

    class C:
        async def delete_account(self, email):
            calls["n"] += 1
            raise RuntimeError("HTTP 500 boom" if calls["n"] == 1 else "404 not found")

        async def close(self):
            return None

    async def cl(aid, bot_id):
        return C(), "x"
    monkeypatch.setattr(tenant, "_admin_svc_client", cl)
    accs = await sc.expired_accounts(4)
    ok, why = await sc.delete_one(4, accs[0].id, True)
    assert not ok and "500" in why
    ok2, _ = await sc.delete_one(4, accs[1].id, True)                # «پیدا نشد» = قبلاً پاک شده
    assert ok2
    assert len(await sc.expired_accounts(4)) == 1


@pytest.mark.asyncio
async def test_bulk_by_age_threshold(db):
    res = await sc.delete_many(4, 7, panel_too=False)
    assert res["deleted"] == 1 and res["failed"] == 0
    assert [a.email for a in await sc.expired_accounts(4)] == ["c@x"]


def test_render_list_text_mentions_no_user_message(db=None):
    kb = sc.entry_button()
    assert kb.callback_data == "tvpn:exp:0"


@pytest.mark.asyncio
async def test_runner_sends_only_to_audience(db, monkeypatch):
    sent = []

    class FakeBot:
        session = SimpleNamespace(close=lambda: _noop())

        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append(chat_id)

        async def edit_message_text(self, *a, **k):
            return None
    monkeypatch.setattr(br, "_make_bot", lambda token: FakeBot())
    monkeypatch.setattr(br, "_SEND_GAP", 0)
    async with db() as s:
        bc = models.Broadcast(bot_id=4, text="hi", status="sending", chat_id=1, msg_id=1, cursor_uid=0,
                              total_count=2, audience="panel:marzban")
        s.add(bc)
        await s.commit()
        bid = bc.id
    await br.run_broadcast(bid)
    assert sent == [20]                       # فقط دارندهٔ سرویسِ marzban؛ نه همه
    async with db() as s:
        b = await s.get(models.Broadcast, bid)
        assert b.status == "done" and b.sent_count == 1


async def _noop():
    return None
