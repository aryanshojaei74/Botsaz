"""هشدارِ نودهایِ خطادارِ پنلِ پاسارگارد (PATCH_PANEL_NODES_WATCHDOG_V1) — گزارشِ WHALE SHOP."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.shared.panel_clients.panel_watchdog as W

BAD = [{"id": 1, "name": "G1Turkey-WG", "status": "error", "address": "1.2.3.4", "message": "[Errno 111] Connection refused"},
       {"id": 2, "name": "T2", "status": "error", "address": "5.6.7.8", "message": "Request timed out"}]
GOOD = [{"id": 1, "name": "G1Turkey-WG", "status": "connected", "address": "1.2.3.4", "message": None},
        {"id": 2, "name": "T2", "status": "disabled", "address": "5.6.7.8", "message": None}]


class FakeClient:
    def __init__(self, box):
        self.box = box

    async def _request(self, method, path, **kw):
        if self.box.get("exc"):
            raise self.box["exc"]
        return self.box["nodes"]


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, W):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        b = models.Bot(owner_id=77, token="123:T", username="vpn", field="vpn", plan="pro", status="active")
        s.add(b); await s.flush()
        pn = models.VpnPanel(bot_id=b.id, panel_key="pasarguard", base_url="https://p.example", username="u", password="p", enabled=True)
        s.add(pn); await s.commit()
        ids = (b.id, pn.id)
    box = {"nodes": BAD}
    from tenant.shared.panel_clients import panel_pool
    monkeypatch.setattr(panel_pool, "get_sync", lambda *a, **k: FakeClient(box))
    sent = []

    class FakeBot:
        def __init__(self, token, default=None):
            self.session = type("S", (), {"close": staticmethod(lambda: _noop())})()

        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    async def _noop():
        return None
    monkeypatch.setattr(W, "Bot", FakeBot)
    monkeypatch.setattr(W, "_NODE_ALERT_AFTER_SEC", 0)
    return dict(box=box, sent=sent, bid=ids[0], pid=ids[1], maker=maker)


@pytest.mark.asyncio
async def test_alert_once_with_names_and_hints_then_no_spam_then_recovery(env):
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 1
    chat, text = env["sent"][0]
    assert chat == 77 and "G1Turkey-WG" in text and "Connection refused" in text
    assert "سرویسِ نود خاموش" in text and "فایروال" in text and "2 نود" in text
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 1                       # تکرار نشد
    env["box"]["nodes"] = GOOD
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 2 and "دوباره وصل شدند" in env["sent"][1][1]
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 2                       # بعد از وصل‌شدن دیگر پیام نیست


@pytest.mark.asyncio
async def test_no_alert_before_threshold_and_healthy_panel_is_silent(env, monkeypatch):
    monkeypatch.setattr(W, "_NODE_ALERT_AFTER_SEC", 3600)
    await W._check_nodes_once(env["pid"])
    assert env["sent"] == []                           # هنوز به آستانه نرسیده
    env["box"]["nodes"] = GOOD
    await W._check_nodes_once(env["pid"])
    assert env["sent"] == []                           # سالم و هیچ هشداری نرفته بود ⇒ خبرِ «وصل شد» هم نه


@pytest.mark.asyncio
async def test_reminder_after_long_time_and_unreadable_nodes_are_ignored(env, monkeypatch):
    await W._check_nodes_once(env["pid"])
    monkeypatch.setattr(W, "_NODE_REMIND_AFTER_SEC", 0)
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 2 and "یادآوری" in env["sent"][1][1]
    env["box"]["exc"] = RuntimeError("HTTP 403")        # ادمینِ غیر sudo ⇒ بی‌صدا رد می‌شود
    await W._check_nodes_once(env["pid"])
    assert len(env["sent"]) == 2


@pytest.mark.asyncio
async def test_non_pasarguard_panels_are_skipped(env):
    async with env["maker"]() as s:
        p = await s.get(models.VpnPanel, env["pid"])
        p.panel_key = "sanai"
        await s.commit()
    await W._check_nodes_once(env["pid"])
    assert env["sent"] == []
