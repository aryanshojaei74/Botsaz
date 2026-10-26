"""SelfSaz pro: پلن‌های چندماهه، قفل دابل‌تپ، قوانین قبل از اتصال، یادآور انقضا/هشدار قطعی، آمار."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot
import platform_selfsaz as PS
import platform_selfsaz_pro as PRO

UID = 777
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


# ───────────── منطق خالص ─────────────
def test_discount_parsing_and_price():
    assert PRO.parse_discounts("") == PRO.DEFAULT_DISCOUNT
    assert PRO.parse_discounts("3:10,6:20,12:30") == {1: 0, 3: 10, 6: 20, 12: 30}
    assert PRO.parse_discounts("3:999,6:-1,12:x") [3] == 5          # خارج از بازه ⇒ پیش‌فرض
    assert PRO.parse_discounts("garbage")[12] == 15
    assert PRO.parse_discounts('{"1": 50, "3": 7}')[1] == 0           # یک‌ماهه همیشه بدون تخفیف
    assert PRO.plan_price(100000, 1, 0) == 100000
    assert PRO.plan_price(100000, 3, 5) == 285000
    assert PRO.plan_price(99999, 12, 15) == round(99999 * 12 * 0.85)
    opts = PRO.plan_options(50000, PRO.DEFAULT_DISCOUNT)
    assert [o["months"] for o in opts] == [1, 3, 6, 12]
    assert opts[3]["days"] == 365 and opts[3]["price"] < opts[3]["full"]


def test_reminder_tags():
    t = lambda h: (NOW + timedelta(hours=h)).isoformat()
    assert PRO.reminder_tag(t(100), NOW) is None
    assert PRO.reminder_tag(t(60), NOW) == "3d"
    assert PRO.reminder_tag(t(10), NOW) == "1d"
    assert PRO.reminder_tag(t(-5), NOW) == "exp"
    assert PRO.reminder_tag(t(-100), NOW) is None
    assert PRO.reminder_tag(None, NOW) is None
    assert PRO.reminder_tag("junk", NOW) is None


def test_stats_and_friendly_error():
    t = lambda d: (NOW + timedelta(days=d)).isoformat()
    st = PRO.compute_stats([
        {"expires_at": t(3), "status": "running"},
        {"expires_at": t(30), "status": "running"},
        {"expires_at": t(-2), "status": "error", "trial_used": 1},
        {"expires_at": None, "status": "configured"},
    ], NOW)
    assert st == {"total": 4, "active": 2, "expired": 1, "errors": 1, "never_paid": 1,
                  "expiring7": 1, "trial_ended": 1}
    assert "trial" not in PRO.friendly_error("trial already used").lower()
    # مسیر/stack داخلی هرگز لو نمی‌رود
    leaked = PRO.friendly_error("Traceback File /opt/selfsaz/app.py line 3 sqlite3.OperationalError")
    assert "/opt" not in leaked and "sqlite" not in leaked.lower()


def test_user_lock():
    assert PRO.try_acquire(1) is True
    assert PRO.try_acquire(1) is False
    PRO.release(1)
    assert PRO.try_acquire(1) is True
    PRO.release(1)


# ───────────── جریان خرید ─────────────
class FakeMsg:
    def __init__(self):
        self.edits, self.answers = [], []

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class FakeCB:
    def __init__(self, data, uid=UID):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, full_name="U", username="u")
        self.message = FakeMsg()
        self.alerts = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append((text, show_alert))


def cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, platform_bot, PS):
        monkeypatch.setattr(mod, "Session", maker, raising=False)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=UID, full_name="U", balance=1_000_000))
        await s.commit()
    acct = {"configured": True, "id": 9, "price": 100_000, "enabled": False, "status": "configured",
            "expires_at": None, "trial_used": 0}
    calls = []

    async def fake_api(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET" and path.startswith("/v1/accounts/"):
            return dict(acct)
        if path.endswith("/activate"):
            await asyncio.sleep(0.01)
            acct["expires_at"] = (NOW + timedelta(days=payload["days"])).isoformat()
            acct["enabled"] = True
            return {"ok": True, "enabled": True, "expires_at": acct["expires_at"]}
        return {"ok": True}

    monkeypatch.setattr(PS, "selfsaz_api", fake_api)

    async def noop_panel(*a, **k):
        return None
    monkeypatch.setattr(PS, "show_selfsaz_panel", noop_panel)
    PRO._BUSY.clear()
    yield SimpleNamespace(maker=maker, acct=acct, calls=calls)
    await engine.dispose()


async def _balance(maker):
    async with maker() as s:
        return float((await s.scalar(select(models.PlatformUser).where(
            models.PlatformUser.telegram_id == UID))).balance)


@pytest.mark.asyncio
async def test_plan_list_shows_four_plans_with_discount(env):
    cb = FakeCB("pss:buy:9")
    await PS.selfsaz_buy(cb)
    text, kw = cb.message.edits[-1]
    data = cbs(kw["reply_markup"])
    assert [f"pss:buy:pick:9:{m}" for m in (1, 3, 6, 12)] == [d for d in data if "pick" in d]
    assert "285,000" in text and "5٪" in text


@pytest.mark.asyncio
async def test_confirm_charges_discounted_price_and_adds_days(env):
    cb = FakeCB("pss:buy:confirm:9:3")
    await PS.selfsaz_buy_confirm(cb)
    assert await _balance(env.maker) == 1_000_000 - 285_000
    act = [c for c in env.calls if c[1].endswith("/activate")]
    assert act and act[0][2] == {"days": 90}


@pytest.mark.asyncio
async def test_legacy_confirm_button_is_one_month(env):
    await PS.selfsaz_buy_confirm(FakeCB("pss:buy:confirm:9"))
    assert await _balance(env.maker) == 900_000
    assert [c for c in env.calls if c[1].endswith("/activate")][0][2] == {"days": 30}


@pytest.mark.asyncio
async def test_double_tap_charges_once(env):
    a, b = FakeCB("pss:buy:confirm:9:1"), FakeCB("pss:buy:confirm:9:1")
    await asyncio.gather(PS.selfsaz_buy_confirm(a), PS.selfsaz_buy_confirm(b))
    assert await _balance(env.maker) == 900_000                      # فقط یک بار کسر شد
    assert len([c for c in env.calls if c[1].endswith("/activate")]) == 1
    assert any("در حال پردازش" in t for t, _ in (a.alerts + b.alerts))
    assert PRO._BUSY == set()                                          # قفل آزاد شد


@pytest.mark.asyncio
async def test_failed_activation_refunds_and_lock_released(env, monkeypatch):
    real = PS.selfsaz_api

    async def failing(method, path, payload=None):
        if path.endswith("/activate"):
            return None
        return await real(method, path, payload)
    monkeypatch.setattr(PS, "selfsaz_api", failing)
    cb = FakeCB("pss:buy:confirm:9:1")
    await PS.selfsaz_buy_confirm(cb)
    assert await _balance(env.maker) == 1_000_000
    assert any("برگشت" in t for t, _ in cb.alerts)
    assert PRO._BUSY == set()


@pytest.mark.asyncio
async def test_insufficient_balance_no_charge(env):
    env.acct["price"] = 5_000_000
    cb = FakeCB("pss:buy:confirm:9:1")
    await PS.selfsaz_buy_confirm(cb)
    assert await _balance(env.maker) == 1_000_000
    assert not [c for c in env.calls if c[1].endswith("/activate")]


@pytest.mark.asyncio
async def test_admin_discount_changes_price(env):
    await platform_bot.set_pset(PRO.DISC_KEY, "3:50,6:50,12:50")
    cb = FakeCB("pss:buy:confirm:9:12")
    await PS.selfsaz_buy_confirm(cb)
    assert await _balance(env.maker) == 1_000_000 - 600_000


@pytest.mark.asyncio
async def test_revenue_counts_purchases_minus_refunds(env):
    await PS.selfsaz_buy_confirm(FakeCB("pss:buy:confirm:9:1"))
    async with env.maker() as s:
        s.add(models.PlatformTxn(user_telegram_id=UID, amount=40_000, kind="refund",
                                 note="بازگشت وجه خرید ناموفق سلف‌ساز"))
        await s.commit()
    r30, rall, cnt = await PRO.revenue(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5))
    assert (r30, rall, cnt) == (60_000, 60_000, 1)


# ───────────── قوانین قبل از اتصال ─────────────
@pytest.mark.asyncio
async def test_connect_requires_tos_once(env, monkeypatch):
    cb = FakeCB("pss:secure_connect")
    state = SimpleNamespace(clear=lambda: asyncio.sleep(0))
    await PS.selfsaz_connect_start(cb, state)
    text, kw = cb.message.edits[-1]
    assert "قوانین" in text and "pss:tos:yes" in cbs(kw["reply_markup"])
    assert not [c for c in env.calls if c[1] == "/v1/connect/start"]     # هنوز لینکی ساخته نشده

    async def fake_api(method, path, payload=None):
        env.calls.append((method, path, payload))
        return {"url": "https://x.example/connect/tok"}
    monkeypatch.setattr(PS, "selfsaz_api", fake_api)
    if True:
        cb2 = FakeCB("pss:tos:yes")
        await PS.selfsaz_tos_yes(cb2)
        assert [c for c in env.calls if c[1] == "/v1/connect/start"]
        assert await PRO.tos_accepted(UID)
        env.calls.clear()
        await PS.selfsaz_connect_start(FakeCB("pss:secure_connect"), state)   # بار دوم دیگر قوانین نمی‌پرسد
        assert [c for c in env.calls if c[1] == "/v1/connect/start"]


# ───────────── نگهبان: یادآور و قطعی ─────────────
class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_watch_reminders_sent_once_and_error_notice(env):
    PRO._state.update(fails=0, down_alerted=False, sup_alerted=False)
    soon = (NOW + timedelta(hours=50)).isoformat()
    accounts = {"accounts": [
        {"id": 1, "owner_id": 11, "expires_at": soon, "status": "running"},
        {"id": 2, "owner_id": 12, "expires_at": None, "status": "error"},
    ]}

    async def api(method, path, payload=None):
        return accounts
    bot = Bot()
    out = await PRO.watch_once(bot, api, 1, now=NOW)
    assert out["reminders"] == 1 and out["errors"] == 1
    assert any(c == 11 and "۳ روز" in t for c, t in bot.sent)
    assert any(c == 12 and "متوقف" in t for c, t in bot.sent)
    n = len(bot.sent)
    out2 = await PRO.watch_once(bot, api, 1, now=NOW + timedelta(minutes=5))
    assert out2["reminders"] == 0 and len(bot.sent) == n              # تکراری نمی‌فرستد
    accounts["accounts"][1]["status"] = "running"                      # برگشت به حالت عادی ⇒ علامت پاک می‌شود
    await PRO.watch_once(bot, api, 1, now=NOW)
    accounts["accounts"][1]["status"] = "error"
    out3 = await PRO.watch_once(bot, api, 1, now=NOW)
    assert out3["errors"] == 1                                         # خطای تازه دوباره اطلاع می‌گیرد


@pytest.mark.asyncio
async def test_watch_service_down_alert_once_and_recovery(env):
    PRO._state.update(fails=0, down_alerted=False, sup_alerted=False)
    state = {"up": False}

    async def api(method, path, payload=None):
        return {"accounts": []} if state["up"] else None
    bot = Bot()
    for _ in range(PRO.DOWN_AFTER_FAILS - 1):
        await PRO.watch_once(bot, api, 1, now=NOW)
    assert bot.sent == []
    out = await PRO.watch_once(bot, api, 1, now=NOW)
    assert out["down"] and len(bot.sent) == 1 and "پاسخ نمی‌دهد" in bot.sent[0][1]
    await PRO.watch_once(bot, api, 1, now=NOW)
    assert len(bot.sent) == 1                                          # هشدار تکراری نمی‌رود
    state["up"] = True
    out = await PRO.watch_once(bot, api, 1, now=NOW)
    assert out["recovered"] and "دوباره" in bot.sent[-1][1]


def test_spam_and_insult_removed_from_panel_ui():
    src = open(PS.__file__, encoding="utf8").read()
    assert "اسپم" not in src and "فحش" not in src


@pytest.mark.asyncio
async def test_watch_supervisor_stale_alert_once_and_recovery(env):
    PRO._state.update(fails=0, down_alerted=False, sup_alerted=False)
    health = {"supervisor_age": 500}

    async def api(method, path, payload=None):
        return health if path == "/health" else {"accounts": []}
    bot = Bot()
    out = await PRO.watch_once(bot, api, 1, now=NOW)
    assert out.get("supervisor_stale") and "supervisor" in bot.sent[0][1]
    await PRO.watch_once(bot, api, 1, now=NOW)
    assert len(bot.sent) == 1
    health["supervisor_age"] = 5
    await PRO.watch_once(bot, api, 1, now=NOW)
    assert "دوباره فعال" in bot.sent[-1][1]
    health["supervisor_age"] = None                       # سرویس قدیمی بدون فیلد ⇒ هشدار الکی نمی‌دهد
    await PRO.watch_once(bot, api, 1, now=NOW)
    assert len(bot.sent) == 2
