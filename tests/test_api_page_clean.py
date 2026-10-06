"""
تست‌هایِ صفحهٔ تمیزِ «API» برایِ کاربر (PATCH_API_PAGE_CLEAN_V1).

درخواستِ ادمین (فدایی): «این خیلی شلوغ و نامعلومه؛ آدرسِ استاندارد باید باشه با کلید، بقیه چیه این همه؟»
و نمونه‌ای که فرستاد: آدرس، موجودی، کلید، وضعیت، شمارندهٔ سفارش‌هایِ API، لینکِ مستندات + دکمه‌ها.
"""
from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import public_api as pa

CUSTOMER = 555
OTHER = 556
KEY = "f58b96de919523fff6d5019609386688caa35698947aa203"


class Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text="", **kw): self.sent.append((text, kw))
    async def edit_text(self, text="", **kw): self.sent.append((text, kw))
    async def delete(self): ...


class Cb:
    def __init__(self, data, uid=CUSTOMER):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=uid, is_bot=False), Msg(), []

    async def answer(self, text="", show_alert=False, **kw): self.alerts.append((text, show_alert))


@pytest_asyncio.fixture
async def db(monkeypatch):
    import tenant
    from tenant.gateways import custom_domain
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, pa):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b)
        await s.flush()
        u = models.BotUser(bot_id=b.id, telegram_id=CUSTOMER, full_name="Ali", balance=1000,
                           api_access=True, api_key=KEY, api_expires_at=dt.datetime.utcnow() + dt.timedelta(days=30))
        s.add(u)
        s.add(models.BotUser(bot_id=b.id, telegram_id=OTHER, full_name="Sara", balance=0))
        await s.commit()
        row = SimpleNamespace(id=b.id, field="virtual_service", owner_id=1, plan="pro", status="active", username="shop")

    async def get_row(_bot): return row
    async def base(_bid): return "https://expresspanel.ir"
    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    monkeypatch.setattr(custom_domain, "get_bot_webhook_base", base)
    yield maker, row
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


async def user(maker, tg=CUSTOMER):
    async with maker() as s:
        return await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == tg))


async def add_order(maker, row, *, source, status="sent", tg=CUSTOMER, qty=100):
    async with maker() as s:
        s.add(models.SmmOrder(bot_id=row.id, user_telegram_id=tg, service_id="1", service_name="ممبر تلگرام",
                              link="l", quantity=qty, price_toman=500, status=status, source=source))
        await s.commit()


# ═════════════ ۱) متنِ صفحه ═════════════

@pytest.mark.asyncio
async def test_page_shows_exactly_the_requested_fields(db):
    import tenant
    maker, row = db
    text, kb = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    assert "https://expresspanel.ir/api/v2" in text                # آدرسِ استاندارد
    assert "موجودی شما: <b>1,000</b> تومان" in text
    assert f"<code>{KEY}</code>" in text and "کلید API شما" in text
    assert "وضعیت کلید: فعال" in text and "سفارش‌هایِ API: <b>0</b>" in text
    assert "اعتبارِ دسترسی تا" in text
    assert "https://expresspanel.ir/api/docs" in text


@pytest.mark.asyncio
async def test_page_no_longer_has_the_cluttered_parts(db):
    """چیزهایی که فدایی «شلوغ و نامعلوم» خواند."""
    import tenant
    maker, row = db
    text, _ = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    for junk in ("api/v1", "X-API-Key", "(نه هدر)", "آدرسِ پایه", "فرمتِ استانداردِ SMM", "به اشتراک نذار",
                 "key=" + KEY, "دیتایِ خودِ شما"):
        assert junk not in text, junk
    assert len(text.splitlines()) <= 16                            # کوتاه


@pytest.mark.asyncio
async def test_page_is_short_compared_to_the_old_one(db):
    import tenant
    maker, row = db
    text, _ = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    assert len(text) < 420


@pytest.mark.asyncio
async def test_permanent_and_discount_lines(db):
    import tenant
    maker, row = db
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == CUSTOMER))
        u.api_expires_at = None
        u.api_discount_pct = 12.5
        await s.commit()
    text, _ = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    assert "دسترسیِ دائمی" in text and "اعتبارِ دسترسی تا" not in text
    assert "12.5٪" in text


@pytest.mark.asyncio
async def test_html_is_valid_for_telegram(db):
    import tenant
    maker, row = db
    text, _ = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    opens = re.findall(r"<(b|code)>", text)
    closes = re.findall(r"</(b|code)>", text)
    assert sorted(opens) == sorted(closes)


# ═════════════ ۲) دکمه‌ها ═════════════

def _btns(kb):
    return [b for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_buttons(db):
    import tenant
    maker, row = db
    await tenant.set_setting(row.id, "api_price", "50000")
    tenant._SETTING_CACHE.clear()
    _, kb = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    cbs = [b.callback_data for b in _btns(kb) if b.callback_data]
    assert cbs == ["pubapi:regen", "pubapi:orders", "pubapi:buy", "mbtn:home"]
    url_btns = [b for b in _btns(kb) if b.url]
    assert [b.url for b in url_btns] == ["https://expresspanel.ir/api/docs"]
    assert "50,000" in next(b.text for b in _btns(kb) if b.callback_data == "pubapi:buy")
    assert [b.text for b in _btns(kb) if b.callback_data == "pubapi:regen"] == ["🔄 تغییر کلید"]


@pytest.mark.asyncio
async def test_no_renew_button_for_free_or_permanent_access(db):
    import tenant
    maker, row = db
    _, kb = await tenant._pubapi_render(row, await user(maker), CUSTOMER)          # قیمت صفر
    assert "pubapi:buy" not in [b.callback_data for b in _btns(kb)]


@pytest.mark.asyncio
async def test_docs_button_is_skipped_when_the_address_is_unusable(db, monkeypatch):
    import tenant
    from tenant.gateways import custom_domain
    maker, row = db

    async def bad(_bid): return "localhost:8001"           # طرحِ http/https ندارد
    monkeypatch.setattr(custom_domain, "get_bot_webhook_base", bad)
    _, kb = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    assert not [b for b in _btns(kb) if b.url]             # دکمهٔ لینک نامعتبر تلگرام را می‌شکند؛ نباید ساخته شود


@pytest.mark.asyncio
async def test_users_without_access_still_get_the_purchase_page(db):
    """مسیرِ خریدِ دسترسی (کارِ جلسهٔ قبل) دست‌نخورده است."""
    import tenant
    maker, row = db
    await tenant.set_setting(row.id, "api_price", "50000")
    tenant._SETTING_CACHE.clear()
    text, kb = await tenant._pubapi_render(row, await user(maker, OTHER), OTHER)
    assert "خرید دسترسیِ API" in [b.text for b in _btns(kb)][0] or "pubapi:buy" in [b.callback_data for b in _btns(kb)]
    assert KEY not in text


# ═════════════ ۳) شمارنده و لیستِ سفارش‌هایِ API ═════════════

@pytest.mark.asyncio
async def test_counter_counts_only_api_orders_of_this_user(db):
    import tenant
    maker, row = db
    await add_order(maker, row, source="api")
    await add_order(maker, row, source="api", status="completed")
    await add_order(maker, row, source=None)                    # سفارشِ عادیِ ربات
    await add_order(maker, row, source="api", tg=OTHER)
    text, _ = await tenant._pubapi_render(row, await user(maker), CUSTOMER)
    assert "سفارش‌هایِ API: <b>2</b>" in text


@pytest.mark.asyncio
async def test_orders_list_shows_only_my_api_orders(db):
    import tenant
    maker, row = db
    await add_order(maker, row, source="api", qty=111)
    await add_order(maker, row, source=None, qty=222)
    await add_order(maker, row, source="api", tg=OTHER, qty=333)
    text, kb = await tenant._pubapi_orders_content(row.id, CUSTOMER)
    assert "111" in text and "222" not in text and "333" not in text
    assert [b.callback_data for b in _btns(kb)] == ["pubapi:key"]


@pytest.mark.asyncio
async def test_orders_list_empty_state_and_limit(db):
    import tenant
    maker, row = db
    text, _ = await tenant._pubapi_orders_content(row.id, CUSTOMER)
    assert "هنوز سفارشی" in text
    for i in range(tenant._API_ORDERS_PAGE + 5):
        await add_order(maker, row, source="api", qty=i + 1)
    text2, _ = await tenant._pubapi_orders_content(row.id, CUSTOMER)
    assert f"{tenant._API_ORDERS_PAGE + 5:,} سفارش" in text2 and f"آخرین {tenant._API_ORDERS_PAGE}" in text2
    assert len([l for l in text2.splitlines() if l.startswith("<code>")]) == tenant._API_ORDERS_PAGE


@pytest.mark.asyncio
async def test_orders_handler_requires_access(db):
    import tenant
    maker, row = db
    cb = Cb("pubapi:orders", uid=OTHER)                         # OTHER دسترسی ندارد
    await tenant.pubapi_orders(cb, None)
    assert cb.alerts and cb.alerts[0][1] is True and cb.message.sent == []
    cb2 = Cb("pubapi:orders")
    await tenant.pubapi_orders(cb2, None)
    assert "سفارش‌هایِ API" in cb2.message.sent[-1][0]


@pytest.mark.asyncio
async def test_api_orders_get_marked_with_source_api(db, monkeypatch):
    """سفارشی که از راهِ API ثبت می‌شود باید source='api' بگیرد تا در لیست/شمارنده بیاید."""
    maker, row = db
    from tenant.bots.virtual_service import smm
    monkeypatch.setattr(smm, "Session", maker)
    async with maker() as s:
        svc = models.SmmService(bot_id=row.id, service_id="12", name="ممبر", sell_rate=1000, min_qty=1, max_qty=100000,
                                is_active=True, is_manual=True)
        s.add(svc)
        await s.commit()
        pk = svc.id
    async def price(*a, **k): return 500
    async def mode(*a, **k): return "manual"
    monkeypatch.setattr(smm, "compute_sell_price", price)
    monkeypatch.setattr(smm, "_panel_order_mode", mode)
    res = await pa.api_new_order(pa.NewOrderBody(service_id=pk, link="https://t.me/x", quantity=10), x_api_key=KEY)
    assert res.status_code == 201
    async with maker() as s:
        o = (await s.scalars(select(models.SmmOrder))).one()
    assert o.source == "api" and o.user_telegram_id == CUSTOMER


# ═════════════ ۴) تغییر کلید ═════════════

@pytest.mark.asyncio
async def test_regen_invalidates_the_old_key_and_tells_the_user(db):
    import tenant
    maker, row = db
    cb = Cb("pubapi:regen")
    await tenant.pubapi_key_regen(cb, None)
    u = await user(maker)
    assert u.api_key != KEY and len(u.api_key) == 48
    assert any("باطل" in t and shown for t, shown in cb.alerts)
    assert u.api_key in cb.message.sent[-1][0]                    # صفحهٔ تازه کلیدِ جدید را نشان می‌دهد


@pytest.mark.asyncio
async def test_regen_refused_without_access(db):
    import tenant
    maker, row = db
    cb = Cb("pubapi:regen", uid=OTHER)
    await tenant.pubapi_key_regen(cb, None)
    assert cb.message.sent == [] and (await user(maker, OTHER)).api_key is None


# ═════════════ ۵) صفحهٔ مستندات ═════════════

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pa.public_api_router)
    return TestClient(app)


def test_docs_page_is_served_as_html_with_the_callers_host(client):
    r = client.get("/api/docs", headers={"host": "expresspanel.ir", "x-forwarded-proto": "https"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "https://expresspanel.ir/api/v2" in r.text and 'dir="rtl"' in r.text


def test_docs_page_documents_exactly_the_implemented_actions(client):
    """مستندات نباید اکشنی را وعده بدهد که کد ندارد، و اکشنِ واقعی را جا نیندازد."""
    import inspect
    src = inspect.getsource(pa.smm_v2)
    implemented = set(re.findall(r'action == "(\w+)"', src))
    assert implemented >= set(pa.DOCS_ACTIONS)
    html = client.get("/api/docs", headers={"host": "x.test"}).text
    for a in pa.DOCS_ACTIONS:
        assert f"action={a}" in html, a
    assert set(re.findall(r"action=(\w+)", html)) == set(pa.DOCS_ACTIONS)


def test_docs_page_parameter_names_are_the_ones_the_endpoint_reads(client):
    """نام پارامترهایِ مستندات باید همان‌هایی باشد که smm_v2 واقعاً از درخواست می‌خواند."""
    import inspect
    src = inspect.getsource(pa.smm_v2)
    html = client.get("/api/docs", headers={"host": "x.test"}).text
    for name in ("key", "action", "service", "link", "quantity", "order", "orders"):
        assert f'p.get("{name}")' in src, name
        assert f"<code>{name}</code>" in html or f"{name}=" in html, name


def test_docs_host_is_sanitized(client):
    r = client.get("/api/docs", headers={"host": "evil.com/<script>alert(1)</script>"})
    assert "<script>alert" not in r.text and "YOUR-API-HOST" in r.text


def test_docs_are_static_and_leak_nothing(client):
    html = client.get("/api/docs", headers={"host": "x.test"}).text
    assert "API_KEY" in html and not re.search(r"[0-9a-f]{40,}", html)


def test_docs_mention_the_partial_status_and_unsupported_actions(client):
    html = client.get("/api/docs", headers={"host": "x.test"}).text
    assert "Partial" in html and "refill" in html and "cancel" in html
