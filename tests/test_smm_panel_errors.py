"""
تست‌هایِ سه خواستهٔ فدایی (PATCH_SMM_PANEL_ERROR_TEXT_V1 / ORDER_PLACED_TEXT_V1 / ALERT_TOGGLE_V1):

  ۱) به‌جایِ «پاسخ نامعتبر از پنل»، متنِ دقیقِ خطایی که پنل داده به ادمین برسد.
  ۲) پیامِ ثبتِ سفارش قابلِ تنظیم باشد و تنظیمش واقعاً اثر کند.
  ۳) کلیدِ خاموش‌کردنِ پیامِ «سفارش رویِ پنل رد شد» (با هشدارِ موجودیِ محدودشده).

بخشِ شبکه با یک سرورِ HTTPِ واقعیِ محلی (aiohttp) تست می‌شود، نه با mock — چون باگ در همین لایه بود.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import smm, smm_api, smm_texts


# ═════════════════ ۱) لایهٔ شبکه با سرورِ واقعی ═════════════════

def _panel_app(behavior):
    async def handler(request):
        form = await request.post()
        return await behavior(form)
    app = web.Application()
    app.router.add_post("/api/v2", handler)
    return app


@pytest_asyncio.fixture
async def panel():
    """سرورِ پنلِ ساختگی که رفتارش را هر تست عوض می‌کند."""
    box = {"fn": None}

    async def dispatch(form):
        return await box["fn"](form)
    server = TestServer(_panel_app(dispatch))
    await server.start_server()
    smm_api._session = None                     # سشنِ مشترکِ ماژول به لوپِ همین تست وصل شود
    url = str(server.make_url("/api/v2"))
    yield box, url
    if smm_api._session is not None:
        await smm_api._session.close()
        smm_api._session = None
    await server.close()


async def _add(url):
    return await smm_api.add_order(url, "KEY", "12", "https://t.me/x", 100)


@pytest.mark.asyncio
async def test_success_keeps_the_old_contract(panel):
    box, url = panel
    async def ok(form): return web.json_response({"order": 23501})
    box["fn"] = ok
    assert await _add(url) == {"order": 23501}


@pytest.mark.asyncio
async def test_http_200_error_text_passes_through_untouched(panel):
    box, url = panel
    async def err(form): return web.json_response({"error": "Not enough funds on balance"})
    box["fn"] = err
    assert await _add(url) == {"error": "Not enough funds on balance"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expect", [
    (400, {"error": "Incorrect service ID"}, "HTTP 400: Incorrect service ID"),
    (403, {"error": "Account banned"}, "HTTP 403: Account banned"),
    (429, {"message": "Too many requests, slow down"}, "HTTP 429: Too many requests, slow down"),
    (422, {"errors": {"quantity": ["min is 100"]}}, 'HTTP 422: {"quantity":["min is 100"]}'),
])
async def test_non_200_json_error_is_no_longer_thrown_away(panel, status, body, expect):
    """قبلاً: _post برایِ هر کدِ HTTP غیر از ۲۰۰ فقط None برمی‌گرداند ⇒ «پاسخ نامعتبر از پنل»."""
    box, url = panel
    async def fn(form): return web.json_response(body, status=status)
    box["fn"] = fn
    res = await _add(url)
    assert res == {"error": expect}


@pytest.mark.asyncio
async def test_html_error_page_is_shown_shortened(panel):
    box, url = panel
    async def fn(form):
        return web.Response(status=503, text="<html><body>  Service   Unavailable \n  try later </body></html>",
                            content_type="text/html")
    box["fn"] = fn
    res = await _add(url)
    assert res["error"].startswith("HTTP 503: ")
    assert "Service Unavailable" in res["error"] and "\n" not in res["error"]
    assert len(res["error"]) <= 330


@pytest.mark.asyncio
async def test_200_but_not_json_is_reported_with_the_body(panel):
    box, url = panel
    async def fn(form): return web.Response(text="Maintenance mode", content_type="text/plain")
    box["fn"] = fn
    res = await _add(url)
    assert res["error"].startswith("پاسخ غیرقابل‌فهم") and "Maintenance mode" in res["error"]


@pytest.mark.asyncio
async def test_200_json_without_order_or_error_is_not_treated_as_success(panel):
    box, url = panel
    async def fn(form): return web.json_response({"status": "ok"})
    box["fn"] = fn
    res = await _add(url)
    assert "order" not in res and res["error"].startswith("پاسخ بدون شناسه")
    assert '"status":"ok"' in res["error"]


@pytest.mark.asyncio
async def test_empty_body_and_network_errors_have_readable_text(panel):
    box, url = panel
    async def empty(form): return web.Response(text="")
    box["fn"] = empty
    assert "خالی" in (await _add(url))["error"]
    dead = "http://127.0.0.1:9/api/v2"                        # پورتِ بسته
    res = await smm_api.add_order(dead, "K", "1", "l", 1)
    assert res["error"].startswith("ارتباط با پنل برقرار نشد")


@pytest.mark.asyncio
async def test_timeout_is_reported_as_connection_problem(panel, monkeypatch):
    box, url = panel
    async def slow(form):
        await asyncio.sleep(2)
        return web.json_response({"order": 1})
    box["fn"] = slow
    monkeypatch.setattr(smm_api, "_TIMEOUT", 0.2)
    res = await _add(url)
    assert res["error"].startswith("ارتباط با پنل برقرار نشد") and "Timeout" in res["error"]


@pytest.mark.asyncio
async def test_other_callers_of_post_are_unchanged(panel):
    """_post قراردادِ قبلی را دارد: فقط برایِ ۲۰۰ داده می‌دهد، وگرنه None — بقیهٔ توابع (balance/services) به آن تکیه دارند."""
    box, url = panel
    async def fn(form): return web.json_response({"balance": "5"}, status=500)
    box["fn"] = fn
    assert await smm_api._post(url, {"key": "k", "action": "balance"}) is None
    async def ok(form): return web.json_response({"balance": "5", "currency": "USD"})
    box["fn"] = ok
    assert await smm_api._post(url, {"key": "k", "action": "balance"}) == {"balance": "5", "currency": "USD"}


def test_error_text_key_priority_and_shapes():
    R = smm_api.PanelResponse
    f = smm_api.panel_error_text
    assert f(R(status=200, data={"order": 1})) is None                          # موفق
    assert f(R(status=200, data={"error": "x", "message": "y"})) == "x"          # error مقدم بر message
    assert f(R(status=200, data={"error": {"code": 5, "text": "bad"}})) == '{"code":5,"text":"bad"}'
    assert f(R(status=500, data={"detail": "boom"})) == "HTTP 500: boom"
    assert f(R(status=502, data=None, text="")) == "HTTP 502"
    assert f(R(exc="ConnectionResetError")) == "ارتباط با پنل برقرار نشد (ConnectionResetError)"
    assert len(f(R(status=500, data=None, text="x" * 5000))) <= len("HTTP 500: ") + smm_api._ERR_SNIPPET


# ═════════════════ ۲) مشتری چه می‌بیند؟ ═════════════════

@pytest.mark.parametrize("note,expected", [
    ("HTTP 503: Service Unavailable", "مشکلِ موقت"),
    ("ارتباط با پنل برقرار نشد (TimeoutError)", "مشکلِ موقت"),
    ("پاسخ غیرقابل‌فهم از پنل: Maintenance", "مشکلِ موقت"),
    ("پاسخ بدون شناسه سفارش: {}", "مشکلِ موقت"),
    ("", "مشکلِ موقت"),
    ("Incorrect link", "Incorrect link"),                    # متنی که خودِ پنل دربارهٔ سفارش نوشته همان می‌ماند
    ("Quantity less than minimum", "Quantity less than minimum"),
])
def test_customer_only_sees_panel_text_not_technical_transport_errors(note, expected):
    assert smm._customer_fail_reason(note) == expected


# ═════════════════ ۳) دیتابیس: سفارش، بازگشتِ وجه، ادمین ═════════════════

@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    monkeypatch.setattr(smm, "Session", maker)
    monkeypatch.setattr(config, "Session", maker)
    monkeypatch.setattr(tenant, "Session", maker)
    tenant._SETTING_CACHE.clear()      # کشِ get_setting سراسری است و کلیدش (bot_id, key)؛ هر تست bot_id=1 دارد
    async with maker() as s:
        bot = models.Bot(owner_id=777, token="1:T", username="shop", field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        s.add(models.BotUser(bot_id=bot.id, telegram_id=555, full_name="Ali", username="ali", balance=1000))
        o = models.SmmOrder(bot_id=bot.id, user_telegram_id=555, service_id="12", service_name="ممبر 🚀",
                            link="https://t.me/x", quantity=4, price_toman=16110, status="awaiting_admin",
                            tracking_code="SMM-1942")
        s.add(o)
        await s.commit()
        ids = (bot.id, o.id)
    yield maker, ids
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_order_note_and_return_value_carry_the_exact_panel_text(db, monkeypatch):
    maker, (bot_id, oid) = db
    async def creds(b, p): return ("http://x/api/v2", "K", "P")
    async def add(url, key, sid, link, qty): return {"error": "HTTP 429: Too many requests"}
    async def notify(*a, **k): return None
    monkeypatch.setattr(smm, "_panel_creds", creds)
    monkeypatch.setattr(smm.smm_api, "add_order", add)
    monkeypatch.setattr(smm, "_notify_user", notify)

    ok, note = await smm._place_on_panel(bot_id, oid)
    assert ok is False and note == "HTTP 429: Too many requests"       # چیزی که ادمین در «بازم ناموفق بود» می‌بیند
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
        u = await s.scalar(select(models.BotUser))
    assert o.status == "refunded" and "HTTP 429: Too many requests" in o.note
    assert float(u.balance) == 1000 + 16110                             # پول مشتری برگشت


# ═════════════════ ۴) پیامِ ثبتِ سفارش ═════════════════

def test_manual_default_text_renders_exactly_like_the_old_hardcoded_message():
    """اگر ادمین چیزی تنظیم نکرده باشد فرقی نکرده؛ ولی اگر «پیش‌فرض» را در ویرایشگر ببیند همان باشد."""
    import re
    label, default = smm_texts.TEXTS["order_placed_manual"]
    rendered = (default.replace("{{product_name}}", "ممبر").replace("{{quantity}}", "4")
                .replace("{{amount}}", "16,110").replace("{{tracking_code}}", "SMM-1942"))
    old = (f"✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n"
           f"🛍 ممبر\n🔢 تعداد: 4\n💰 مبلغ: 16,110 تومان\n"
           f"🔖 کد پیگیری: <code>SMM-1942</code>\n\n"
           f"پس از تأیید و تکمیل، بهت اطلاع داده می‌شود. هر وقت خواستی از "
           f"«📦 سفارش‌های من» می‌تونی وضعیت و کدِ پیگیری‌شو دوباره ببینی.")
    assert rendered == old
    assert not re.search(r"\{\{[^}]*\}\}", rendered)


@pytest.mark.asyncio
async def test_get_custom_returns_empty_when_unset_and_value_when_set(db):
    maker, (bot_id, oid) = db
    import tenant
    assert await smm_texts.get_custom(bot_id, "order_placed_manual") == ""
    await tenant.set_setting(bot_id, "smm_txt_order_placed_manual", "  سفارشت ثبت شد ✅  ")
    assert await smm_texts.get_custom(bot_id, "order_placed_manual") == "سفارشت ثبت شد ✅"
    assert await smm_texts.get_text(bot_id, "sent") != ""                 # get_text قدیمی سالم


@pytest.mark.asyncio
async def test_render_order_text_fills_variables_for_the_real_order(db):
    maker, (bot_id, oid) = db
    out = await smm._render_order_text(bot_id, oid, "سفارش {{tracking_code}}: {{product_name}} × {{quantity}} = {{amount}} تومان")
    assert out == "سفارش SMM-1942: ممبر 🚀 × 4 = 16,110 تومان"
    assert await smm._render_order_text(bot_id, 424242, "بدون سفارش {{amount}}") == "بدون سفارش {{amount}}"


class _Msg:
    def __init__(self, fail_first_html=False):
        self.calls = []
        self.fail_first_html = fail_first_html

    async def answer(self, text, **kw):
        self.calls.append((text, kw))
        if self.fail_first_html and kw.get("parse_mode") == "HTML":
            raise RuntimeError("can't parse entities: Unsupported start tag")


@pytest.mark.asyncio
async def test_html_safe_answer_falls_back_to_plain_text_on_a_broken_tag():
    m = _Msg(fail_first_html=True)
    await smm._answer_html_safe(m, "سفارش <b>ثبت شد", reply_markup="KB")
    assert [c[1].get("parse_mode") for c in m.calls] == ["HTML", None]
    assert m.calls[1][0] == "سفارش <b>ثبت شد" and m.calls[1][1]["reply_markup"] == "KB"


@pytest.mark.asyncio
async def test_html_safe_answer_does_not_hide_unrelated_errors():
    class Boom(_Msg):
        async def answer(self, text, **kw):
            raise RuntimeError("Forbidden: bot was blocked by the user")
    with pytest.raises(RuntimeError, match="blocked"):
        await smm._answer_html_safe(Boom(), "x")


# ═════════════════ ۵) خاموش‌کردنِ پیامِ «رد شد» ═════════════════

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _ctx(bot_id, err):
    row = SimpleNamespace(id=bot_id, owner_id=777)
    buyer = SimpleNamespace(username="ak47", telegram_id=555)
    svc = SimpleNamespace(name="بوست 🚀")
    return row, buyer, svc, err


async def _alert(bot, bot_id, err):
    row, buyer, svc, e = _ctx(bot_id, err)
    await smm._notify_smm_order_failed(bot, row, buyer, svc, 4, 16110, e)


@pytest.mark.asyncio
async def test_alert_is_on_by_default_and_shows_the_exact_panel_text(db):
    maker, (bot_id, oid) = db
    bot = _Bot()
    await _alert(bot, bot_id, "HTTP 429: Too many requests")
    assert len(bot.sent) == 1 and bot.sent[0][0] == 777
    assert "HTTP 429: Too many requests" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_muted_alert_sends_nothing_for_ordinary_failures(db):
    maker, (bot_id, oid) = db
    import tenant
    await tenant.set_setting(bot_id, "smm_alert_panel_reject", "0")
    bot = _Bot()
    for _ in range(5):
        await _alert(bot, bot_id, "پاسخ نامعتبر از پنل")
        await _alert(bot, bot_id, "HTTP 500: boom")
    assert bot.sent == []


@pytest.mark.asyncio
async def test_muted_alert_still_warns_about_empty_panel_balance_but_at_most_every_6_hours(db, monkeypatch):
    maker, (bot_id, oid) = db
    import tenant, time
    await tenant.set_setting(bot_id, "smm_alert_panel_reject", "0")
    bot = _Bot()
    await _alert(bot, bot_id, "Not enough funds on balance")
    await _alert(bot, bot_id, "Not enough funds on balance")          # بلافاصله: محدود
    await _alert(bot, bot_id, "Insufficient balance")
    assert len(bot.sent) == 1 and "موجودیِ حسابت" in bot.sent[0][1]
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 6 * 3600 + 5)      # ۶ ساعت بعد
    await _alert(bot, bot_id, "Not enough funds on balance")
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_turning_it_back_on_restores_every_alert(db):
    maker, (bot_id, oid) = db
    import tenant
    await tenant.set_setting(bot_id, "smm_alert_panel_reject", "0")
    await tenant.set_setting(bot_id, "smm_alert_panel_reject", "1")
    bot = _Bot()
    await _alert(bot, bot_id, "HTTP 500: boom")
    await _alert(bot, bot_id, "HTTP 500: boom")
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_alert_failure_never_raises(db):
    maker, (bot_id, oid) = db
    class Dead(_Bot):
        async def send_message(self, *a, **k):
            raise RuntimeError("network down")
    await _alert(Dead(), bot_id, "HTTP 500: boom")                    # نباید exception بدهد


# ═════════════════ ۶) منویِ متن‌ها و کلید ═════════════════

@pytest.mark.asyncio
async def test_texts_menu_shows_the_new_text_and_the_alert_switch(db):
    maker, (bot_id, oid) = db
    from tenant.bots.virtual_service import smm_texts_admin as adm
    text, kb = await adm._menu(bot_id)
    cbs = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    # کلیدِ خاموش/روشن حالا (کارِ جلسهٔ دیگر) یکی به‌ازایِ هر نوع پیامِ ادمین است: smmtxt:al:<کلید>
    assert "smmtxt:e:order_placed_manual" in cbs and "smmtxt:al:smm_alert_panel_reject" in cbs
    assert "روشن" in cbs["smmtxt:al:smm_alert_panel_reject"]
    import tenant
    await tenant.set_setting(bot_id, "smm_alert_panel_reject", "0")
    _, kb2 = await adm._menu(bot_id)
    assert "خاموش" in {b.callback_data: b.text for row in kb2.inline_keyboard for b in row}["smmtxt:al:smm_alert_panel_reject"]


@pytest.mark.asyncio
async def test_toggle_handler_flips_the_setting_and_redraws(db, monkeypatch):
    maker, (bot_id, oid) = db
    import tenant
    from tenant.bots.virtual_service import smm_texts_admin as adm

    async def fake_owner(x, bot): return SimpleNamespace(id=bot_id, owner_id=777)
    monkeypatch.setattr(smm, "_owner", fake_owner)

    class Cb:
        def __init__(self):
            self.alerts = []
            self.message = SimpleNamespace(edit_text=self._edit, answer=self._edit)
            self.edited = 0
        async def _edit(self, *a, **k): self.edited += 1
        async def answer(self, text="", show_alert=False, **k): self.alerts.append(text)

    cb = Cb()
    await adm.smmtxt_alert_toggle(cb, None)
    assert await tenant.get_setting(bot_id, "smm_alert_panel_reject", "1") == "0"
    assert "خاموش" in cb.alerts[0] and cb.edited == 1
    await adm.smmtxt_alert_toggle(cb, None)
    assert await tenant.get_setting(bot_id, "smm_alert_panel_reject", "1") == "1"
    assert "روشن" in cb.alerts[1]
