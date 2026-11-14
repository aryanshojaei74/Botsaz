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
    import log_throttle                                           # throttle فایلیِ مشترک بینِ اجراها ⇒ برایِ تست جدا و در حافظه
    _seen: dict = {}

    def _fresh_should_log(key, every_seconds):
        if key in _seen:
            return False
        _seen[key] = True
        return True
    monkeypatch.setattr(log_throttle, "should_log", _fresh_should_log)
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
    assert len(await _rows(env)) == 1 and FakeClient.deleted == []                              # حذف نشد ⇒ ردیف می‌ماند
    assert [c for c, _t in FakeTBot.sent if c == 55] == []                                      # به کاربر پیامِ دروغِ «حذف شد» نمی‌رود
    assert [c for c, _t in FakeTBot.sent if c == 1] and "علت" in FakeTBot.sent[0][1]            # PATCH_TRIAL_DELETE_VERIFY_V1: مالک علت را می‌بیند
    FakeTBot.sent.clear()
    FakeClient.fail_delete = False
    await TC._process_bot(9)                                                                  # دورِ بعد (پنل برگشت)
    assert FakeClient.deleted == ["t1"] and await _rows(env) == []
    assert [c for c, _t in FakeTBot.sent if c == 55] and "حذف شد" in [t for c, t in FakeTBot.sent if c == 55][0]


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


# ───────── PATCH_TRIAL_DELETE_VERIFY_V1 (باگِ #191) ─────────
class _ClientFalse(FakeClient):
    """پنلی که بدونِ خطا False می‌دهد و حساب را واقعاً نگه می‌دارد."""
    still_there = True

    async def list_clients(self):
        return [{"email": "t1", "expiryTime": FakeClient.gone_ms, "traffic": {"up": 0, "down": 0}}] if _ClientFalse.still_there else []

    async def delete_account(self, email):
        return False


@pytest.mark.asyncio
async def test_false_return_with_account_still_on_panel_is_not_success(env, monkeypatch):
    monkeypatch.setattr(TC, "_make_client", lambda pf: _ClientFalse())
    _ClientFalse.still_there = True
    ok, why = await TC._delete_from_panel(("marzban", "https://p", "u", "p", "", None, None), "t1", 9)
    assert ok is False and "نپذیرفت" in why
    await TC._process_bot(9)
    assert len(await _rows(env)) == 1                                       # ردیف نمی‌ماند-نمی‌پرد: حساب هنوز روی پنل است
    assert [c for c, _t in FakeTBot.sent if c == 55] == []                  # و «حذف شد» هم به کاربر نرفت


@pytest.mark.asyncio
async def test_false_return_but_account_already_gone_counts_as_success(env, monkeypatch):
    monkeypatch.setattr(TC, "_make_client", lambda pf: _ClientFalse())
    _ClientFalse.still_there = False                                        # بدنهٔ خالی ⇒ False ولی حساب رفته
    ok, _w = await TC._delete_from_panel(("marzban", "https://p", "u", "p", "", None, None), "t1", 9)
    assert ok is True


class _ClientNotFound(FakeClient):
    async def delete_account(self, email):
        raise RuntimeError("HTTP 404: User not found")


@pytest.mark.asyncio
async def test_not_found_error_means_already_deleted_and_row_is_removed(env, monkeypatch):
    monkeypatch.setattr(TC, "_make_client", lambda pf: _ClientNotFound())
    await TC._process_bot(9)
    assert await _rows(env) == []                                           # قبلاً برایِ همیشه «ناموفق» می‌ماند


@pytest.mark.asyncio
async def test_owner_notice_is_throttled_per_account(env):
    FakeTBot.sent.clear()
    tb = FakeTBot("9:T")
    await TC._notify_delete_failed(tb, 1, 9, "t_throttle_x", "marzban", "HTTP 429")
    await TC._notify_delete_failed(tb, 1, 9, "t_throttle_x", "marzban", "HTTP 429")   # همان روز ⇒ دوباره نمی‌فرستد
    owner_msgs = [t for c, t in FakeTBot.sent if c == 1]
    assert len(owner_msgs) == 1 and "HTTP 429" in owner_msgs[0] and "t_throttle_x" in owner_msgs[0]


def test_decide_covers_every_state():
    import datetime as dt
    now = dt.datetime.utcnow()
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    past, future = now_ms - 3_600_000, now_ms + 3_600_000
    d = TC.decide
    assert d(now, now_ms, past, None, 5, None, False) == "panel_down"
    assert d(now, now_ms, 0, None, 5, False, False) == "orphan"
    assert d(now, now_ms, 0, None, 5, True, False) == "no_expiry"
    assert d(now, now_ms, future, None, 5, True, False) == "running"
    assert d(now, now_ms, past, None, 5, True, False) == "warn_due"
    assert d(now, now_ms, past, now - dt.timedelta(hours=1), 5, True, False) == "waiting"
    assert d(now, now_ms, past, now - dt.timedelta(hours=9), 5, True, False) == "delete_due"


@pytest.mark.asyncio
async def test_status_report_matches_what_cleanup_would_do(env):
    rep = await TC.trial_status_report(9)
    assert len(rep) == 1 and rep[0]["email"] == "t1" and rep[0]["decision"] == "delete_due"
