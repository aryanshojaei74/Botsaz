"""
تست‌هایِ ریفیل (جبرانی/گارانتی) — PATCH_SMM_REFILL_V1.

درخواستِ ادمین (فدایی): ریفیل فقط برایِ سرویسی که ارائه‌دهنده ریفیل دارد ثبت شود، نتیجه (تکمیل/رد) به
مشتری برسد، و گزینهٔ «سفارشاتِ گارانتی‌دار» در لیستِ سفارش‌ها باشد.

بخشِ شبکه با یک سرورِ HTTPِ واقعیِ محلی (aiohttp) تست می‌شود که پنلِ SMM را شبیه‌سازی می‌کند.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import smm, smm_api, smm_refill as RF, smm_texts

CUSTOMER = 555
OTHER = 556


# ═════════════════ پنلِ ساختگی ═════════════════

class FakePanel:
    def __init__(self):
        self.services = [
            {"service": "12", "name": "ممبر", "rate": "1", "min": "10", "max": "1000", "category": "T", "refill": True},
            {"service": "13", "name": "ویو", "rate": "1", "min": "10", "max": "1000", "category": "T", "refill": False},
        ]
        self.refill_response = ("json", 200, {"refill": "48211"})
        self.status_response = ("json", 200, {"status": "Pending"})
        self.bulk_response = None          # پاسخِ استعلامِ گروهی (refills=...) ؛ None ⇒ همان status_response
        self.calls: list[tuple[str, dict]] = []
        self.services_delay = 0.0

    async def handle(self, request):
        form = dict(await request.post())
        action = form.get("action")
        self.calls.append((action, form))
        if action == "services":
            if self.services_delay:
                await asyncio.sleep(self.services_delay)
            return web.json_response(self.services)
        if action == "refill":
            return self._respond(self.refill_response)
        if action == "refill_status":
            if "refills" in form and self.bulk_response is not None:
                return self._respond(self.bulk_response)
            return self._respond(self.status_response)
        return web.json_response({"error": "unknown action"})

    @staticmethod
    def _respond(spec):
        kind, status, body = spec
        if kind == "json":
            return web.json_response(body, status=status)
        return web.Response(status=status, text=body, content_type="text/plain")

    def count(self, action):
        return sum(1 for a, _ in self.calls if a == action)


@pytest_asyncio.fixture
async def panel():
    fp = FakePanel()
    app = web.Application()
    app.router.add_post("/api/v2", fp.handle)
    server = TestServer(app)
    await server.start_server()
    smm_api._session = None
    RF.clear_cache()
    fp.url = str(server.make_url("/api/v2"))
    yield fp
    if smm_api._session is not None:
        await smm_api._session.close()
        smm_api._session = None
    RF.clear_cache()
    await server.close()


# ═════════════════ ۱) لایهٔ API ═════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("spec,expected", [
    (("json", 200, {"refill": "48211"}), {"refill": "48211"}),
    (("json", 200, {"refill": 48211}), {"refill": "48211"}),
    (("json", 200, [{"order": 1, "refill": 5}]), {"refill": "5"}),
    (("json", 200, {"refill": {"id": 7}}), {"refill": "7"}),
])
async def test_refill_order_success_shapes(panel, spec, expected):
    panel.refill_response = spec
    assert await smm_api.refill_order(panel.url, "K", 99) == expected
    action, form = panel.calls[-1]
    assert action == "refill" and form["order"] == "99" and form["key"] == "K"


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [
    ("json", 200, {"refill": 0}),
    ("json", 200, {"refill": ""}),
    ("json", 200, {"refill": "0"}),
    ("json", 200, {"refill": False}),
    ("json", 200, {"refill": None}),
    ("json", 200, {}),
    ("json", 200, {"status": "ok"}),
    ("json", 200, []),
])
async def test_refill_order_without_a_valid_id_is_a_failure_not_a_success(panel, spec):
    """باگِ گزارش‌شده: هر پاسخی «✅ ثبت شد» حساب می‌شد؛ حتی پاسخِ خالی یا بدونِ شناسه."""
    panel.refill_response = spec
    res = await smm_api.refill_order(panel.url, "K", 1)
    assert "refill" not in res and res["error"]
    # لیستِ خالی از دیدِ پنل «پاسخِ غیرقابل‌فهم» است؛ بقیه «شناسه نداد». هر دو شکست با متنِ خوانا هستند.
    assert ("شناسهٔ ریفیل نداد" in res["error"]) or ("غیرقابل‌فهم" in res["error"])


@pytest.mark.asyncio
@pytest.mark.parametrize("spec,expect", [
    (("json", 200, {"error": "Refill is not available for this service"}), "Refill is not available for this service"),
    (("json", 400, {"error": "Order is not eligible"}), "HTTP 400: Order is not eligible"),
    (("json", 429, {"message": "Too many requests"}), "HTTP 429: Too many requests"),
    (("text", 503, "Service Unavailable"), "HTTP 503: Service Unavailable"),
    (("text", 200, "maintenance"), "پاسخ غیرقابل‌فهم"),
])
async def test_refill_order_reports_the_panels_exact_error(panel, spec, expect):
    panel.refill_response = spec
    res = await smm_api.refill_order(panel.url, "K", 1)
    assert expect in res["error"] and "refill" not in res


@pytest.mark.asyncio
async def test_refill_order_network_failure_and_paystars(panel):
    res = await smm_api.refill_order("http://127.0.0.1:9/api/v2", "K", 1)
    assert res["error"].startswith("ارتباط با پنل برقرار نشد")
    res2 = await smm_api.refill_order("https://paystars.uz/api", "K", 1)
    assert "پشتیبانی نمی‌کند" in res2["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("spec,expected", [
    (("json", 200, {"status": "Completed"}), {"status": "Completed"}),
    (("json", 200, {"status": {"status": "Rejected"}}), {"status": "Rejected"}),
    (("json", 200, [{"status": "In progress"}]), {"status": "In progress"}),
])
async def test_get_refill_status_shapes(panel, spec, expected):
    panel.status_response = spec
    assert await smm_api.get_refill_status(panel.url, "K", "48211") == expected
    assert panel.calls[-1][1]["refill"] == "48211"


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [
    ("json", 200, {}), ("json", 200, {"status": ""}), ("json", 200, {"error": "Incorrect refill ID"}),
    ("json", 404, {"error": "nope"}), ("text", 500, "boom"),
])
async def test_get_refill_status_failures_have_text(panel, spec):
    panel.status_response = spec
    res = await smm_api.get_refill_status(panel.url, "K", "1")
    assert "status" not in res and res["error"]


def test_get_services_marks_whether_the_panel_sends_a_refill_flag():
    src = open("tenant/bots/virtual_service/smm_api.py", encoding="utf-8").read()
    assert '"refill_known": "refill" in item' in src


@pytest.mark.parametrize("raw,expected", [
    ("Completed", "completed"), ("complete", "completed"), ("SUCCESS", "completed"),
    ("Rejected", "rejected"), ("canceled", "rejected"), ("Cancelled", "rejected"), ("error", "rejected"), ("failed", "rejected"),
    ("In progress", "in_progress"), ("in_progress", "in_progress"), ("Processing", "in_progress"),
    ("Pending", "pending"), ("", "pending"), (None, "pending"), ("something new", "pending"),
])
def test_normalize_status(raw, expected):
    assert RF.normalize_status(raw) == expected


# ═════════════════ ۲) پرچمِ پشتیبانیِ ارائه‌دهنده ═════════════════

@pytest.mark.asyncio
async def test_support_reads_the_flag_live_and_caches(panel):
    assert await RF.support(panel.url, "K", "12") is True
    assert await RF.support(panel.url, "K", "13") is False
    assert await RF.support(panel.url, "K", "13") is False
    assert panel.count("services") == 1                        # فقط یک تماس؛ بقیه از کش


@pytest.mark.asyncio
async def test_support_is_unknown_when_the_panel_sends_no_flag_or_service_is_gone(panel):
    panel.services = [{"service": "12", "name": "x", "rate": "1", "min": "1", "max": "9", "category": "T"}]   # بدونِ refill
    assert await RF.support(panel.url, "K", "12") is None
    RF.clear_cache()
    panel.services = [{"service": "12", "name": "x", "rate": "1", "min": "1", "max": "9", "category": "T", "refill": True}]
    assert await RF.support(panel.url, "K", "999") is None      # سرویس دیگر در فهرست نیست


@pytest.mark.asyncio
async def test_cached_support_never_touches_the_network(panel):
    assert RF.cached_support(panel.url, "K", "12") is None       # کشِ سرد
    assert panel.calls == []
    await RF.support(panel.url, "K", "12")
    n = len(panel.calls)
    assert RF.cached_support(panel.url, "K", "12") is True and RF.cached_support(panel.url, "K", "13") is False
    assert len(panel.calls) == n


@pytest.mark.asyncio
async def test_concurrent_lookups_make_one_request(panel):
    panel.services_delay = 0.2
    res = await asyncio.gather(*[RF.support(panel.url, "K", "12") for _ in range(8)])
    assert res == [True] * 8 and panel.count("services") == 1


@pytest.mark.asyncio
async def test_cache_expires_and_stale_value_survives_a_down_panel(panel, monkeypatch):
    assert await RF.support(panel.url, "K", "12") is True
    key = RF._panel_key(panel.url, "K")
    ts, table, flag = RF._SUPPORT[key]
    RF._SUPPORT[key] = (ts - RF._SUPPORT_TTL - 1, table, flag)                # منقضی
    async def dead(*a, **k): raise OSError("down")
    monkeypatch.setattr(RF, "_fetch_support", dead)
    assert await RF.support(panel.url, "K", "12") is True                      # پنل قطع: از کشِ کهنه
    RF.clear_cache()
    assert await RF.support(panel.url, "K", "12") is None                      # کشی هم نیست: نامعلوم


@pytest.mark.asyncio
async def test_slow_panel_times_out_to_unknown(panel):
    panel.services_delay = 1.0
    t0 = time.monotonic()
    assert await RF.support(panel.url, "K", "12", timeout=0.2) is None
    assert time.monotonic() - t0 < 0.9


@pytest.mark.asyncio
async def test_warm_fills_the_cache_in_the_background_and_never_raises(panel):
    RF.warm(panel.url, "K")
    for _ in range(50):
        if RF.cached_support(panel.url, "K", "12") is not None:
            break
        await asyncio.sleep(0.02)
    assert RF.cached_support(panel.url, "K", "12") is True
    RF.warm("", "")                                                            # ورودیِ خالی: بی‌اثر
    RF.warm("http://127.0.0.1:9/api/v2", "K")                                  # پنلِ مرده: بی‌خطا
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_cache_is_keyed_per_panel_and_key(panel):
    await RF.support(panel.url, "K1", "12")
    await RF.support(panel.url, "K2", "12")
    assert panel.count("services") == 2


# ═════════════════ ۳) پایگاه‌داده + ثبتِ درخواست ═════════════════

@pytest_asyncio.fixture
async def db(monkeypatch, panel):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    for mod in (config, tenant, smm, RF):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()

    async def creds(bot_id, panel_pk):
        return (panel.url, "KEY", "P")
    monkeypatch.setattr(smm, "_panel_creds", creds)

    sent: list[tuple[int, int, str]] = []
    async def notify(bot_id, order_pk, text, bot=None):
        sent.append((bot_id, order_pk, text))
    monkeypatch.setattr(smm, "_notify_user", notify)

    async with maker() as s:
        bot = models.Bot(owner_id=1, token="1:T", username="shop", field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        s.add(models.BotUser(bot_id=bot.id, telegram_id=CUSTOMER, full_name="Ali", balance=0))
        s.add(models.BotUser(bot_id=bot.id, telegram_id=OTHER, full_name="Sara", balance=0))
        await s.commit()
        bot_id = bot.id
    yield maker, bot_id, sent
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


async def _order(maker, bot_id, *, service_id="12", status="completed", panel_oid="777", user=CUSTOMER, link="https://t.me/x"):
    async with maker() as s:
        o = models.SmmOrder(bot_id=bot_id, user_telegram_id=user, service_id=service_id, service_name="ممبر",
                            link=link, quantity=100, price_toman=5000, status=status, panel_order_id=panel_oid,
                            tracking_code="SMM-1")
        s.add(o)
        await s.commit()
        return o.id


async def _refills(maker, order_pk=None):
    async with maker() as s:
        q = select(models.SmmRefill).order_by(models.SmmRefill.id)
        if order_pk:
            q = q.where(models.SmmRefill.order_pk == order_pk)
        return (await s.scalars(q)).all()


@pytest.mark.asyncio
async def test_supported_service_is_registered_and_tracked(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert res.ok and res.code == "ok" and res.refill_id == "48211"
    (rf,) = await _refills(maker, oid)
    assert rf.status == "pending" and rf.panel_refill_id == "48211" and rf.user_telegram_id == CUSTOMER
    assert panel.count("refill") == 1
    assert panel.calls[[a for a, _ in panel.calls].index("refill")][1]["order"] == "777"
    async with maker() as s:
        assert "refill_id=48211" in (await s.get(models.SmmOrder, oid)).note


@pytest.mark.asyncio
async def test_unsupported_service_is_refused_without_calling_the_panel(db, panel):
    """اصلِ درخواستِ فدایی: ارائه‌دهنده ریفیل ندارد ⇒ اصلاً درخواستی ثبت نمی‌شود."""
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id, service_id="13")
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not res.ok and res.code == "unsupported" and "ریفیل (جبرانی) ندارد" in res.text
    assert panel.count("refill") == 0
    (rf,) = await _refills(maker, oid)
    assert rf.status == "rejected" and rf.notified_status == "rejected"          # ردیفِ ناقص فعال نمی‌ماند


@pytest.mark.asyncio
async def test_unknown_support_lets_the_panel_decide(db, panel):
    maker, bot_id, _ = db
    panel.services = [{"service": "12", "name": "x", "rate": "1", "min": "1", "max": "9", "category": "T"}]   # پنل پرچم نمی‌دهد
    oid = await _order(maker, bot_id)
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert res.ok and panel.count("refill") == 1


@pytest.mark.asyncio
async def test_panel_rejection_shows_the_exact_text_and_allows_a_retry(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    panel.refill_response = ("json", 200, {"error": "You can refill once every 24 hours"})
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not res.ok and res.code == "panel_error" and "۲۴ ساعت" in res.text
    (rf,) = await _refills(maker, oid)
    assert rf.status == "rejected"
    panel.refill_response = ("json", 200, {"refill": "555"})
    res2 = await RF.request_refill(bot_id, oid, CUSTOMER)                      # بعد از شکست دوباره می‌شود
    assert res2.ok and res2.refill_id == "555"
    assert [r.status for r in await _refills(maker, oid)] == ["rejected", "pending"]


@pytest.mark.asyncio
async def test_a_response_without_an_id_is_not_registered(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    panel.refill_response = ("json", 200, {})
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not res.ok and res.code == "panel_error"
    (rf,) = await _refills(maker, oid)
    assert rf.status == "rejected" and rf.panel_refill_id is None


@pytest.mark.asyncio
async def test_duplicate_active_refill_is_blocked(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    assert (await RF.request_refill(bot_id, oid, CUSTOMER)).ok
    again = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not again.ok and again.code == "duplicate" and "در جریان" in again.text
    assert panel.count("refill") == 1 and len(await _refills(maker, oid)) == 1


@pytest.mark.asyncio
async def test_after_a_finished_refill_a_new_one_is_allowed(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    await RF.request_refill(bot_id, oid, CUSTOMER)
    async with maker() as s:
        r = (await s.scalars(select(models.SmmRefill))).one()
        r.status = "completed"
        await s.commit()
    panel.refill_response = ("json", 200, {"refill": "9999"})
    assert (await RF.request_refill(bot_id, oid, CUSTOMER)).refill_id == "9999"


@pytest.mark.asyncio
async def test_stale_requesting_row_does_not_block_forever(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    async with maker() as s:
        s.add(models.SmmRefill(bot_id=bot_id, order_pk=oid, user_telegram_id=CUSTOMER, status="requesting",
                               updated_at=datetime.utcnow() - timedelta(minutes=10)))
        await s.commit()
    assert (await RF.request_refill(bot_id, oid, CUSTOMER)).ok
    # ولی یکی که همین الان در حالِ تماس است، مانع می‌شود
    oid2 = await _order(maker, bot_id, panel_oid="778")
    async with maker() as s:
        s.add(models.SmmRefill(bot_id=bot_id, order_pk=oid2, user_telegram_id=CUSTOMER, status="requesting",
                               updated_at=datetime.utcnow()))
        await s.commit()
    assert (await RF.request_refill(bot_id, oid2, CUSTOMER)).code == "duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["refunded", "failed", "canceled", "awaiting_admin", "partial"])
async def test_only_sent_or_completed_orders_are_refillable(db, panel, status):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id, status=status)
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not res.ok and res.code == "bad_status" and panel.count("refill") == 0
    assert await _refills(maker, oid) == []


@pytest.mark.asyncio
async def test_manual_order_without_panel_id_and_foreign_orders_are_refused(db, panel):
    maker, bot_id, _ = db
    manual = await _order(maker, bot_id, panel_oid=None)
    assert (await RF.request_refill(bot_id, manual, CUSTOMER)).code == "bad_status"
    mine = await _order(maker, bot_id)
    assert (await RF.request_refill(bot_id, mine, OTHER)).code == "not_found"          # مالِ دیگری
    assert (await RF.request_refill(bot_id + 5, mine, CUSTOMER)).code == "not_found"   # رباتِ دیگر
    assert (await RF.request_refill(bot_id, 424242, CUSTOMER)).code == "not_found"
    assert panel.count("refill") == 0 and await _refills(maker) == []


@pytest.mark.asyncio
async def test_unreachable_panel_credentials(db, panel, monkeypatch):
    maker, bot_id, _ = db
    async def none(b, p): return ("", "", "—")
    monkeypatch.setattr(smm, "_panel_creds", none)
    oid = await _order(maker, bot_id)
    res = await RF.request_refill(bot_id, oid, CUSTOMER)
    assert not res.ok and res.code == "no_panel" and (await _refills(maker, oid))[0].status == "rejected"


# ═════════════════ ۴) پیگیریِ پس‌زمینه ═════════════════

async def _pending_refill(maker, bot_id, oid, *, rid="48211", status="pending", age_s=120, created_days=0):
    async with maker() as s:
        r = models.SmmRefill(bot_id=bot_id, order_pk=oid, user_telegram_id=CUSTOMER, status=status,
                             panel_refill_id=rid,
                             created_at=datetime.utcnow() - timedelta(days=created_days, seconds=age_s),
                             updated_at=datetime.utcnow() - timedelta(seconds=age_s))
        s.add(r)
        await s.commit()
        return r.id


@pytest.mark.asyncio
async def test_poll_pending_and_in_progress_do_not_notify(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    pk = await _pending_refill(maker, bot_id, oid)
    panel.status_response = ("json", 200, {"status": "In progress"})
    st = await RF.poll_once()
    assert st["checked"] == 1 and sent == []
    assert (await _refills(maker, oid))[0].status == "in_progress"
    assert panel.calls[-1][0] == "refill_status" and panel.calls[-1][1]["refill"] == "48211"


@pytest.mark.asyncio
async def test_poll_completed_notifies_the_customer_exactly_once(db, panel):
    """اصلِ دومِ درخواستِ فدایی: پیامِ «تکمیل شد» به مشتری می‌رسد."""
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid)
    panel.status_response = ("json", 200, {"status": "Completed"})
    st = await RF.poll_once()
    assert st["completed"] == 1 and st["notified"] == 1
    assert len(sent) == 1 and sent[0][:2] == (bot_id, oid) and "انجام شد" in sent[0][2]
    rf = (await _refills(maker, oid))[0]
    assert rf.status == "completed" and rf.notified_status == "completed" and rf.panel_status == "Completed"
    await RF.poll_once()                                       # دوباره: دیگر استعلام و اعلان نیست
    assert len(sent) == 1 and panel.count("refill_status") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["Rejected", "Canceled", "error"])
async def test_poll_rejected_notifies_with_the_rejection_text(db, panel, raw):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid)
    panel.status_response = ("json", 200, {"status": raw})
    st = await RF.poll_once()
    assert st["rejected"] == 1 and len(sent) == 1 and "انجام نداد" in sent[0][2]
    assert (await _refills(maker, oid))[0].status == "rejected"


@pytest.mark.asyncio
async def test_poll_error_keeps_waiting_and_does_not_notify(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid)
    panel.status_response = ("text", 500, "boom")
    st = await RF.poll_once()
    assert st["errors"] == 1 and sent == []
    assert (await _refills(maker, oid))[0].status == "pending"


@pytest.mark.asyncio
async def test_poll_skips_recently_checked_and_finished_refills(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid, age_s=5)                     # همین الان چک شده
    await _pending_refill(maker, bot_id, oid, rid="2", status="completed")
    await _pending_refill(maker, bot_id, oid, rid="3", status="rejected")
    await RF.poll_once()
    assert panel.count("refill_status") == 0


@pytest.mark.asyncio
async def test_poll_expires_old_refills_silently(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid, created_days=11)
    st = await RF.poll_once()
    assert st["expired"] == 1 and sent == [] and panel.count("refill_status") == 0
    assert (await _refills(maker, oid))[0].status == "expired"


@pytest.mark.asyncio
async def test_poll_cleans_up_abandoned_requesting_rows(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    async with maker() as s:
        s.add(models.SmmRefill(bot_id=bot_id, order_pk=oid, user_telegram_id=CUSTOMER, status="requesting",
                               updated_at=datetime.utcnow() - timedelta(minutes=30)))
        await s.commit()
    await RF.poll_once()
    assert (await _refills(maker, oid))[0].status == "rejected" and sent == []


@pytest.mark.asyncio
async def test_poll_handles_many_refills_and_isolates_one_failure(db, panel, monkeypatch):
    maker, bot_id, sent = db
    ids = []
    for i in range(3):
        oid = await _order(maker, bot_id, panel_oid=str(800 + i))
        await _pending_refill(maker, bot_id, oid, rid=str(100 + i))
        ids.append(oid)
    panel.status_response = ("json", 200, {"status": "Completed"})
    calls = {"n": 0}
    real_notify = smm._notify_user

    async def flaky(bot_id_, order_pk, text, bot=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telegram down")
        await real_notify(bot_id_, order_pk, text, bot=bot)
    monkeypatch.setattr(smm, "_notify_user", flaky)
    st = await RF.poll_once()
    assert st["completed"] == 3 and st["errors"] == 1                        # یکی خطا داد، دو تای دیگر رسید
    assert len(sent) == 2
    assert all(r.status == "completed" for r in await _refills(maker))


@pytest.mark.asyncio
async def test_status_poll_once_runs_refill_polling_and_survives_its_failure(db, monkeypatch):
    maker, bot_id, _ = db
    called = []
    async def boom():
        called.append(1)
        raise RuntimeError("x")
    monkeypatch.setattr(RF, "poll_once", boom)
    await smm.smm_status_poll_once()                                         # نباید exception بدهد
    assert called == [1]


# ═════════════════ ۵) صفحه‌هایِ مشتری ═════════════════

def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def _seed_support(panel, value_by_sid):
    key = RF._panel_key(panel.url, "KEY")
    RF._SUPPORT[key] = (time.monotonic(), dict(value_by_sid), True)


@pytest.mark.asyncio
async def test_detail_shows_the_refill_button_only_when_it_makes_sense(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True, "13": False})
    ok = await _order(maker, bot_id, service_id="12")
    no = await _order(maker, bot_id, service_id="13")
    text, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, ok, 1)
    assert any(c.startswith(f"smmu:refill:{ok}:") for c in _cbs(kb))
    text2, kb2 = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, no, 1)
    assert not any(c.startswith("smmu:refill:") for c in _cbs(kb2))              # ریفیل ندارد ⇒ دکمه نیست


@pytest.mark.asyncio
async def test_detail_shows_the_button_when_support_is_unknown(db, panel):
    maker, bot_id, _ = db
    RF.clear_cache()
    oid = await _order(maker, bot_id)
    text, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 1)
    assert any(c.startswith("smmu:refill:") for c in _cbs(kb))                  # نامعلوم ⇒ لحظهٔ کلیک دقیق چک می‌شود


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["refunded", "failed", "canceled", "awaiting_admin", "partial"])
async def test_detail_has_no_refill_button_for_other_statuses(db, panel, status):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    oid = await _order(maker, bot_id, status=status)
    _, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 1)
    assert not any(c.startswith("smmu:refill:") for c in _cbs(kb))


@pytest.mark.asyncio
async def test_detail_hides_the_button_and_shows_status_while_a_refill_is_active(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid, status="in_progress", age_s=1)
    text, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 1)
    assert "♻️ ریفیل: 🔄 در حالِ انجام" in text and "48211" in text
    assert not any(c.startswith("smmu:refill:") for c in _cbs(kb))
    async with maker() as s:
        r = (await s.scalars(select(models.SmmRefill))).one()
        r.status = "completed"
        await s.commit()
    text2, kb2 = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 1)
    assert "✅ انجام شد" in text2 and any(c.startswith("smmu:refill:") for c in _cbs(kb2))   # تمام شد ⇒ دوباره ممکن


@pytest.mark.asyncio
async def test_detail_callbacks_keep_page_and_warranty_context(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    oid = await _order(maker, bot_id)
    _, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 3, home_cb="mbtn:home", warranty=True)
    cbs = _cbs(kb)
    assert f"smmu:refill:{oid}:3:w" in cbs
    assert "smmu:warr:3:mbtn" in cbs                                          # بازگشت به همان لیستِ گارانتی
    _, kb2 = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 2)
    assert "smmu:myorders:2" in _cbs(kb2) and f"smmu:refill:{oid}:2" in _cbs(kb2)


@pytest.mark.asyncio
async def test_warranty_list_contains_only_supported_orders(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True, "13": False})
    a = await _order(maker, bot_id, service_id="12")
    b = await _order(maker, bot_id, service_id="13")
    c = await _order(maker, bot_id, service_id="12", status="refunded")            # ریفیل‌پذیر نیست
    d = await _order(maker, bot_id, service_id="12", user=OTHER)                   # مالِ دیگری
    e = await _order(maker, bot_id, service_id="12", panel_oid=None)               # دستی
    text, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1, warranty=True)
    ids = [int(x.split(":")[2]) for x in _cbs(kb) if x.startswith("smmu:oview:")]
    assert sorted(set(ids)) == [a]
    assert all(x.endswith(":w") for x in _cbs(kb) if x.startswith("smmu:oview:"))
    assert "سفارشاتِ گارانتی‌دار" in text


@pytest.mark.asyncio
async def test_warranty_list_empty_state_and_unknown_orders_are_included(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"13": False})
    await _order(maker, bot_id, service_id="13")
    text, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1, warranty=True)
    assert "پیدا نشد" in text and not any(c.startswith("smmu:oview:") for c in _cbs(kb))
    RF.clear_cache()
    panel.services = [{"service": "13", "name": "x", "rate": "1", "min": "1", "max": "9", "category": "T"}]   # نامعلوم
    text2, kb2 = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1, warranty=True)
    assert any(c.startswith("smmu:oview:") for c in _cbs(kb2))


@pytest.mark.asyncio
async def test_warranty_shortcut_button_appears_only_when_useful(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True, "13": False})
    await _order(maker, bot_id, service_id="13")
    _, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1)
    assert not any(c.startswith("smmu:warr") for c in _cbs(kb))                  # فقط سفارشِ بی‌ریفیل
    await _order(maker, bot_id, service_id="12")
    _, kb2 = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1)
    assert "smmu:warr:1" in _cbs(kb2)
    _, kb3 = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1, home_cb="mbtn:home")
    assert "smmu:warr:1:mbtn" in _cbs(kb3)                                       # ورودیِ مستقیم حفظ می‌شود


@pytest.mark.asyncio
async def test_normal_orders_list_is_unchanged_without_refillable_orders(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"13": False})
    await _order(maker, bot_id, service_id="13", status="sent")
    text, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1)
    assert "سفارش‌های من" in text and "smmu:warr:1" not in _cbs(kb)
    assert _cbs(kb)[-1] == "smmu:home"                                            # دکمهٔ بازگشت مثلِ قبل آخر است


@pytest.mark.asyncio
async def test_warranty_pagination_keeps_the_warranty_context(db, panel):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    for i in range(smm._SMM_MYORDERS_PAGE_SIZE + 3):
        await _order(maker, bot_id, service_id="12", panel_oid=str(1000 + i))
    _, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1, home_cb="mbtn:home", warranty=True)
    assert "smmu:warr:2:mbtn" in _cbs(kb)
    _, kb2 = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 2, warranty=True)
    assert "smmu:warr:1" in _cbs(kb2)


# ═════════════════ ۶) هندلرِ دکمه ═════════════════

class _Msg:
    def __init__(self):
        self.calls = []

    async def answer(self, text="", **kw):
        self.calls.append((text, kw))

    async def edit_text(self, text="", **kw):
        self.calls.append((text, kw))


class _Cb:
    def __init__(self, data, uid=CUSTOMER):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=uid), _Msg(), []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


@pytest.fixture
def as_row(monkeypatch, db):
    maker, bot_id, _ = db
    import tenant
    async def row(_bot): return SimpleNamespace(id=bot_id, owner_id=1, plan="pro")
    monkeypatch.setattr(tenant, "get_bot_row", row)
    return bot_id


@pytest.mark.asyncio
async def test_handler_success_message_uses_the_editable_text_and_shows_the_refill_id(db, panel, as_row):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    cb = _Cb(f"smmu:refill:{oid}:1")
    await smm.smmu_request_refill(cb, None)
    text = cb.message.calls[-1][0]
    assert "ثبت شد" in text and "48211" in text and "SMM-1" in text and "{{" not in text


@pytest.mark.asyncio
async def test_handler_uses_the_admins_custom_registered_text(db, panel, as_row):
    maker, bot_id, _ = db
    import tenant
    await tenant.set_setting(bot_id, "smm_txt_refill_registered", "گارانتی ثبت شد ✅ کد: {{refill_id}}")
    oid = await _order(maker, bot_id)
    cb = _Cb(f"smmu:refill:{oid}:1")
    await smm.smmu_request_refill(cb, None)
    assert cb.message.calls[-1][0] == "گارانتی ثبت شد ✅ کد: 48211"


@pytest.mark.asyncio
async def test_handler_refuses_unsupported_service_and_shows_why(db, panel, as_row):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id, service_id="13")
    cb = _Cb(f"smmu:refill:{oid}:1")
    await smm.smmu_request_refill(cb, None)
    assert "ندارد" in cb.message.calls[-1][0] and panel.count("refill") == 0


@pytest.mark.asyncio
async def test_handler_shows_the_panels_own_rejection(db, panel, as_row):
    maker, bot_id, _ = db
    panel.refill_response = ("json", 200, {"error": "Refill period has expired"})
    oid = await _order(maker, bot_id)
    cb = _Cb(f"smmu:refill:{oid}:1")
    await smm.smmu_request_refill(cb, None)
    assert "مهلتِ" in cb.message.calls[-1][0]


@pytest.mark.asyncio
async def test_handler_blocks_the_second_click_and_foreign_orders(db, panel, as_row):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    await smm.smmu_request_refill(_Cb(f"smmu:refill:{oid}:1"), None)
    cb2 = _Cb(f"smmu:refill:{oid}:1")
    await smm.smmu_request_refill(cb2, None)
    assert "در جریان" in cb2.message.calls[-1][0] and panel.count("refill") == 1
    cb3 = _Cb(f"smmu:refill:{oid}:1", uid=OTHER)
    await smm.smmu_request_refill(cb3, None)
    assert "پیدا نشد" in cb3.message.calls[-1][0] and panel.count("refill") == 1


@pytest.mark.asyncio
async def test_handler_ignores_garbage_callbacks(db, panel, as_row):
    cb = _Cb("smmu:refill:abc:1")
    await smm.smmu_request_refill(cb, None)
    assert cb.message.calls == [] and panel.calls == []


@pytest.mark.asyncio
async def test_warranty_handler_renders_and_keeps_direct_entry(db, panel, as_row):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    await _order(maker, bot_id, service_id="12")
    cb = _Cb("smmu:warr:1:mbtn")
    await smm.smmu_warranty_orders(cb, None)
    text, kw = cb.message.calls[-1]
    assert "سفارشاتِ گارانتی‌دار" in text and "mbtn:home" in _cbs(kw["reply_markup"])


@pytest.mark.asyncio
async def test_order_view_handler_parses_both_flags(db, panel, as_row):
    maker, bot_id, _ = db
    _seed_support(panel, {"12": True})
    oid = await _order(maker, bot_id)
    cb = _Cb(f"smmu:oview:{oid}:2:mbtn:w")
    await smm.smmu_order_view(cb, None)
    cbs = _cbs(cb.message.calls[-1][1]["reply_markup"])
    assert f"smmu:refill:{oid}:2:w" in cbs and "smmu:warr:2:mbtn" in cbs
    cb2 = _Cb(f"smmu:oview:{oid}:2")
    await smm.smmu_order_view(cb2, None)
    assert "smmu:myorders:2" in _cbs(cb2.message.calls[-1][1]["reply_markup"])


# ═════════════════ ۷) ادمین ═════════════════

@pytest.mark.asyncio
async def test_admin_order_detail_shows_the_refill_status(db, panel):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid, status="completed")
    text, _ = await smm._smm_vs_order_detail_content(bot_id, oid, "smm:vs:list:1")
    assert "ریفیل" in text and "انجام شد" in text and "48211" in text


# ═════════════════ ۸) متن‌ها و متغیرها ═════════════════

def test_refill_texts_exist_and_use_known_variables():
    from tenant.shared import text_vars
    for key in ("refill_registered", "refill_done", "refill_rejected"):
        assert key in smm_texts.TEXTS
        names = text_vars.find_names(smm_texts.TEXTS[key][1])
        import re
        used = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", smm_texts.TEXTS[key][1]))
        assert used == names, key


@pytest.mark.asyncio
async def test_refill_variables_render_from_the_latest_refill(db, panel):
    maker, bot_id, _ = db
    from tenant.shared import text_vars
    oid = await _order(maker, bot_id)
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
    assert await text_vars.render_text("{{refill_id}}|{{refill_status}}", bot_id=bot_id, order=o) == "—|—"
    await _pending_refill(maker, bot_id, oid, rid="1", status="rejected")
    await _pending_refill(maker, bot_id, oid, rid="2", status="in_progress")
    assert await text_vars.render_text("{{refill_id}}|{{refill_status}}", bot_id=bot_id, order=o) == "2|در حال انجام"


def test_refill_variables_are_documented():
    from tenant.shared import text_vars
    help_text = text_vars.variables_help_text()
    assert "{{refill_id}}" in help_text and "{{refill_status}}" in help_text


# ═════════════════ ۹) حذفِ ربات و خطِ‌مشیِ داده ═════════════════

def test_refills_are_deleted_before_orders_when_a_bot_is_purged():
    lst = models.TENANT_OWNED_MODELS
    assert models.SmmRefill in lst and lst.index(models.SmmRefill) < lst.index(models.SmmOrder)


@pytest.mark.asyncio
async def test_purge_bot_removes_refills_too(db, panel, monkeypatch):
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid)
    assert await config.purge_bot(bot_id) == "1:T"
    async with maker() as s:
        assert (await s.scalars(select(models.SmmRefill))).all() == []
        assert (await s.scalars(select(models.SmmOrder))).all() == []


# ═════════════════ ۱۰) بخشِ ریفیل هرگز صفحهٔ سفارش را نمی‌شکند ═════════════════

@pytest.mark.asyncio
async def test_order_detail_survives_a_failure_in_the_refill_section(db, panel, monkeypatch):
    """اگر خواندنِ ریفیل خطا بدهد (مثلاً جدول هنوز ساخته نشده)، جزئیاتِ سفارش بدونِ آن بخش نمایش داده می‌شود."""
    maker, bot_id, _ = db
    oid = await _order(maker, bot_id)

    async def boom(_pk):
        raise RuntimeError("no such table: smm_refills")
    monkeypatch.setattr(RF, "latest", boom)
    text, kb = await smm._smm_myorder_detail_content(bot_id, CUSTOMER, oid, 1)
    assert "جزئیاتِ سفارش" in text and "ممبر" in text
    assert not any((b.callback_data or "").startswith("smmu:refill:") for r in kb.inline_keyboard for b in r)
    assert any((b.callback_data or "").startswith("smmu:myorders:") for r in kb.inline_keyboard for b in r)   # بازگشت هست


@pytest.mark.asyncio
async def test_warranty_shortcut_failure_does_not_break_the_orders_list(db, panel, monkeypatch):
    maker, bot_id, _ = db
    await _order(maker, bot_id)

    async def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(smm, "_has_refillable_orders", boom)
    text, kb = await smm._smm_myorders_paginated_content(bot_id, CUSTOMER, 1)
    assert "سفارش‌های من" in text and "smmu:home" in [b.callback_data for r in kb.inline_keyboard for b in r]


# ═════════════ ۵) استعلامِ گروهیِ ریفیل (PATCH_SMM_REFILL_BULK_V1) ═════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    [{"refill": 1, "status": "Completed"}, {"refill": 2, "status": {"error": "Refill not found"}},
     {"refill": 3, "status": "Rejected"}],
    {"1": {"status": "Completed"}, "2": {"error": "Refill not found"}, "3": {"status": {"status": "Rejected"}}},
])
async def test_bulk_refill_status_shapes(panel, body):
    panel.bulk_response = ("json", 200, body)
    res = await smm_api.get_refills_status_bulk(panel.url, "K", [1, 2, 3])
    assert res == {"1": {"status": "Completed"}, "2": {"error": "Refill not found"}, "3": {"status": "Rejected"}}
    assert panel.calls[-1][1]["refills"] == "1,2,3" and "refill" not in panel.calls[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [("json", 200, {"error": "bad"}), ("json", 404, {}), ("text", 500, "boom"),
                                  ("json", 200, [])])
async def test_bulk_refill_status_unsupported_returns_none(panel, spec):
    panel.bulk_response = spec
    assert await smm_api.get_refills_status_bulk(panel.url, "K", [1, 2]) is None
    assert await smm_api.get_refills_status_bulk("https://paystars.uz/api", "K", [1, 2]) is None


@pytest.mark.asyncio
async def test_poll_uses_one_bulk_request_for_many_refills_and_notifies_each(db, panel):
    maker, bot_id, sent = db
    for rid in ("11", "12", "13"):
        oid = await _order(maker, bot_id)
        await _pending_refill(maker, bot_id, oid, rid=rid)
    panel.bulk_response = ("json", 200, [{"refill": 11, "status": "Completed"},
                                         {"refill": 12, "status": "In progress"},
                                         {"refill": 13, "status": "Rejected"}])
    st = await RF.poll_once()
    assert panel.count("refill_status") == 1 and panel.calls[-1][1]["refills"] == "11,12,13"
    assert st["checked"] == 3 and st["completed"] == 1 and st["rejected"] == 1 and len(sent) == 2


@pytest.mark.asyncio
async def test_poll_falls_back_to_single_requests_when_bulk_unsupported_or_partial(db, panel):
    maker, bot_id, sent = db
    for rid in ("21", "22"):
        oid = await _order(maker, bot_id)
        await _pending_refill(maker, bot_id, oid, rid=rid)
    panel.bulk_response = ("json", 200, [{"refill": 21, "status": "Completed"}])      # ۲۲ جا افتاده
    panel.status_response = ("json", 200, {"status": "Rejected"})
    st = await RF.poll_once()
    singles = [f for a, f in panel.calls if a == "refill_status" and "refill" in f]
    assert [f["refill"] for f in singles] == ["22"] and st["completed"] == 1 and st["rejected"] == 1


@pytest.mark.asyncio
async def test_single_pending_refill_still_uses_single_request(db, panel):
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    await _pending_refill(maker, bot_id, oid, rid="31")
    panel.status_response = ("json", 200, {"status": "Pending"})
    await RF.poll_once()
    assert panel.calls[-1][1].get("refill") == "31" and "refills" not in panel.calls[-1][1]


def test_cancel_parser_prefers_the_row_of_this_order():
    d = [{"order": 5, "cancel": {"error": "Incorrect order ID"}}, {"order": 7, "cancel": {"status": "Completed"}}]
    assert smm._parse_cancel_response(d, 7) == (True, "Completed")
    assert smm._parse_cancel_response(d, 5)[0] is False


@pytest.mark.asyncio
async def test_status_error_row_does_not_overwrite_the_order(db):
    """مستندات: در استعلامِ گروهی ردیفِ بدشناسه {"error": "Incorrect order ID"} است؛ سفارش نباید خراب شود."""
    maker, bot_id, sent = db
    oid = await _order(maker, bot_id)
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
        o.panel_status, o.remaining = "in progress", 40
        await s.commit()
    await smm._apply_order_status(bot_id, oid, {"error": "Incorrect order ID"})
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
    assert o.panel_status == "in progress" and o.remaining == 40


# ═══ نمونه‌هایِ دقیقِ مستندِ جدیدِ WhaleSMM (whalesmm.com/fa/api، 2026-10-24) ═══
def test_whalesmm_doc_examples_cancel_and_refill_status():
    from tenant.bots.virtual_service import smm as S, smm_api as A
    doc_cancel = [{"order": 9, "cancel": {"error": "Incorrect order ID"}}, {"order": 2, "cancel": 1}]
    assert S._parse_cancel_response(doc_cancel, 2) == (True, "requested")
    assert S._parse_cancel_response(doc_cancel, 9) == (False, "Incorrect order ID")
    assert A._refill_status_value("Completed") == {"status": "Completed"}
    assert A._refill_status_value({"error": "Refill not found"}) == {"error": "Refill not found"}
    assert A._valid_refill_id("1") == "1" and A._valid_refill_id({"error": "Incorrect order ID"}) is None
