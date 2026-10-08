"""PATCH_SMM_PRICE_AUTOSYNC_V1 — بروزرسانیِ خودکارِ قیمتِ سرویس‌ها از ارائه‌دهنده + گزارش."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm as SM
from tenant.bots.virtual_service import smm_price_sync as PS


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SM, PS):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="f", field="virtual_service",
                         plan="pro", status="active"))
        rows = [("101", "لایک", 10.0, 0, False, None), ("102", "ویو", 5.0, 0, False, None),
                ("103", "ممبر", 20.0, 90_000, False, None), ("104", "حذف‌شده", 3.0, 0, False, None),
                ("man1", "دستی", 0, 5000, True, None),
                ("oz1", "شماره", 2.0, 1000, False, "شمارهٔ مجازیِ تلگرام")]
        for sid, name, rate, sell, manual, cat in rows:
            s.add(models.SmmService(bot_id=9, service_id=sid, name=name, panel_rate=rate, sell_rate=sell,
                                    is_manual=manual, category=cat, is_active=True, min_qty=10, max_qty=1000))
        await s.commit()

    async def fetch(bot_id, panel_pk=None, force=False):
        return [{"service": "101", "rate": "12.5", "min": 50, "max": 5000},
                {"service": "102", "rate": "5", "min": 10, "max": 1000},
                {"service": "103", "rate": "15", "min": 10, "max": 1000}]

    async def creds(bot_id, panel_pk):
        return "u", "k", "پنلِ تست"

    async def sell(bot_id, svc, qty):
        return int(float(svc.sell_rate or 0)) or int(float(svc.panel_rate) * 1000)
    monkeypatch.setattr(SM, "_fetch_services_cached", fetch)
    monkeypatch.setattr(SM, "_panel_creds", creds)
    monkeypatch.setattr(SM, "compute_sell_price", sell)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_updates_rates_and_reports(db):
    reps = await PS.sync_bot(9)
    assert len(reps) == 1
    r = reps[0]
    assert r["ok"] and [c["sid"] for c in r["up"]] == ["101"] and [c["sid"] for c in r["down"]] == ["103"]
    assert r["missing"] == ["حذف‌شده"]
    async with db() as s:
        like = await s.get(models.SmmService, 1)
        oz = await s.get(models.SmmService, 6)
    assert float(like.panel_rate) == 12.5 and like.min_qty == 50 and like.max_qty == 5000
    assert float(oz.panel_rate) == 2.0                      # اوزوینو دست نخورد
    t = PS.report_text(r)
    assert "بررسیِ خودکارِ سرویس‌هایِ پنلِ پنلِ تست" in t
    assert "لایک — قیمتِ هر ۱۰۰۰ از 10,000 به 12,500 تومان" in t and "(افزایش)" in t
    assert "قیمتِ فروشِ دستیِ شما ثابت ماند" in t and "(کاهش)" in t
    # دورِ دوم: تغییری نیست
    r2 = (await PS.sync_bot(9))[0]
    assert not r2["up"] and not r2["down"]
    assert r2["missing"] == ["حذف‌شده"]


@pytest.mark.asyncio
async def test_no_change_text_and_notify_modes(db, monkeypatch):
    rep = {"panel": "X", "ok": True, "checked": 3, "up": [], "down": [], "missing": []}
    assert "تغییری در قیمت‌ها نبود" in PS.report_text(rep)
    sent = []

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = self

        async def send_message(self, chat, text):
            sent.append((chat, text))

        async def close(self):
            pass
    import aiogram
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    await PS.send_reports(9, [rep], "changes")
    assert sent == []
    await PS.send_reports(9, [rep], "all")
    assert sent and sent[0][0] == 1
    await PS.send_reports(9, [rep], "off")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_run_due_respects_interval_and_toggle(db, monkeypatch):
    calls = []

    async def fake_sync(bid, **kw):
        calls.append(bid)
        await tenant.set_setting(bid, "smm_price_sync_last", str(10**10))
        return []
    monkeypatch.setattr(PS, "sync_bot", fake_sync)
    assert await PS.run_due_once() == 1
    assert await PS.run_due_once() == 0          # هنوز ۳۰ دقیقه نگذشته
    await tenant.set_setting(9, "smm_price_sync_last", "0")
    await tenant.set_setting(9, "smm_price_autosync", "0")
    assert await PS.run_due_once() == 0 and calls == [9]


def test_ton_done_posts_trust_report():
    """PATCH_TON_TRUST_REPORT_V1 — مثلِ بقیهٔ تأییدهایِ دستی، تأییدِ تون هم گزارشِ کانال را می‌فرستد."""
    import inspect
    src = inspect.getsource(SM.ton_order_done)
    assert "_notify_gift_order_trust_success(bot, row, oid)" in src
    assert src.index('o.status = "sent"') < src.index("_notify_gift_order_trust_success")


@pytest.mark.asyncio
async def test_per_panel_toggle_only_affects_auto(db):
    await PS.set_panel_auto(9, None, False)
    assert await PS.sync_bot(9, auto=True) == []            # پنل خاموش: دورِ خودکار ردش می‌کند
    assert len(await PS.sync_bot(9)) == 1                   # بروزرسانیِ دستی همچنان کار می‌کند
    assert await PS.sync_bot(9, only_pk=5) == []            # فقط پنلِ ۵ (سرویسی ندارد)
