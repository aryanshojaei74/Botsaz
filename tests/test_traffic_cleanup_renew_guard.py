"""تیکتِ تعزیان: «تمدید کردم، چرا سرویس حذف شد» — PATCH_TRAFFIC_RENEW_GUARD_V1."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.shared import traffic_cleanup as TC

pytestmark = pytest.mark.asyncio
GB = 1024 ** 3


class FakeClient:
    def __init__(self, store):
        self.store = store

    async def list_clients(self):
        if self.store.get("fail"):
            raise RuntimeError("panel down")
        return self.store["clients"]

    async def delete_account(self, email):
        self.store.setdefault("deleted", []).append(email)

    async def close(self):
        pass


class FakeBot:
    sent = []

    def __init__(self, *a, **k):
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        pass

    async def send_message(self, chat_id, text, **k):
        FakeBot.sent.append((chat_id, text))


@pytest_asyncio.fixture
async def env(monkeypatch):
    FakeBot.sent = []
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(TC, "Session", maker)
    store = {"clients": []}
    monkeypatch.setattr(TC, "_make_client", lambda pf: FakeClient(store))
    monkeypatch.setattr(TC, "Bot", FakeBot)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro", status="active")
        s.add(b)
        await s.flush()
        s.add(models.VpnPanel(bot_id=b.id, panel_key="sanai", base_url="http://p", username="u",
                              password="p", enabled=True))
        s.add(models.PanelAccount(bot_id=b.id, user_telegram_id=555, email="taz1", panel_key="sanai",
                                  volume_gb=9, days=0, svc_code="670865388", plan_title="5 گیگ / بی‌انقضا",
                                  traffic_out_at=dt.datetime.utcnow() - dt.timedelta(hours=10)))
        await s.commit()
        bid = b.id
    yield maker, bid, store
    await eng.dispose()


def _client(email, cap_gb, used_gb):
    return {"email": email, "totalGB": int(cap_gb * GB), "up": int(used_gb * GB / 2), "down": int(used_gb * GB / 2)}


async def _acct(maker):
    async with maker() as s:
        return await s.scalar(select(models.PanelAccount))


async def test_renewed_account_with_new_volume_is_not_deleted_and_stamp_is_cleared(env):
    maker, bid, store = env
    store["clients"] = [_client("taz1", 9, 5)]        # تمدید شد: سقف ۹ گیگ، ۵ گیگ مصرف‌شده
    await TC._process_bot(bid)
    a = await _acct(maker)
    assert a is not None and a.traffic_out_at is None
    assert store.get("deleted") is None and FakeBot.sent == []


async def test_still_exhausted_account_is_deleted_and_user_is_told(env):
    maker, bid, store = env
    store["clients"] = [_client("taz1", 5, 5)]        # هنوز تمام‌شده
    await TC._process_bot(bid)
    assert await _acct(maker) is None
    assert store["deleted"] == ["taz1"] and "سرویس شما حذف شد" in FakeBot.sent[0][1]


async def test_panel_read_failure_defers_deletion(env):
    maker, bid, store = env
    store["fail"] = True
    await TC._process_bot(bid)
    a = await _acct(maker)
    assert a is not None and a.traffic_out_at is not None      # دستنخورده؛ دورِ بعد دوباره امتحان می‌شود
    assert store.get("deleted") is None and FakeBot.sent == []


async def test_unlimited_after_renewal_cancels_deletion(env):
    maker, bid, store = env
    store["clients"] = [_client("taz1", 0, 7)]        # سقف ۰ = نامحدود
    await TC._process_bot(bid)
    a = await _acct(maker)
    assert a is not None and a.traffic_out_at is None


async def test_client_missing_on_panel_is_still_cleaned_from_db(env):
    maker, bid, store = env
    store["clients"] = []                              # روی پنل پاک شده بود
    await TC._process_bot(bid)
    assert await _acct(maker) is None


async def test_clear_traffic_out_helper(env):
    maker, bid, store = env
    a = await _acct(maker)
    await TC.clear_traffic_out(a.id)
    assert (await _acct(maker)).traffic_out_at is None
    await TC.clear_traffic_out(999999)                 # حسابِ ناموجود ⇒ بی‌صدا
