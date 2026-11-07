"""بعد از خریدِ وایرگارد، اول فایل‌هایِ .conf و بعد کارتِ سرویس (PATCH_WG_FILES_FIRST_V1) — درخواستِ ادمین."""
import asyncio
import datetime

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER = 555
WG1 = "wireguard://PRIVKEY1111111111111111111111111111111111%3D@t1.example.ir:58531/?publickey=PUB1%3D&address=10.0.0.2%2F32&mtu=1300&dns=1.1.1.1#Turkey1"
WG2 = "wireguard://PRIVKEY1111111111111111111111111111111111%3D@t2.example.ir:58532/?publickey=PUB2%3D&address=10.0.1.2%2F32&mtu=1300&dns=1.1.1.1#Germany1"
VLESS = "vless://aaaa@host.example.com:443?security=tls#x"


class Cap(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(type(method).__name__)
        if type(method).__name__ in ("SendMessage", "SendPhoto", "SendDocument", "EditMessageReplyMarkup"):
            return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                           chat=Chat(id=USER, type="private"), text="x").as_(bot)
        return True


class FakeClient:
    def __init__(self, links, delay=0.0):
        self.links, self.delay = links, delay

    async def get_wireguard_endpoints(self, email):
        return []

    async def get_client_links(self, email):
        if self.delay:
            await asyncio.sleep(self.delay)
        return list(self.links)

    async def close(self): ...


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    import tenant.bots.vpn.vpn_user as V
    from tenant.shared.panel_clients import panel_pool
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, V):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="123:TESTTOKEN", username="vpn", field="vpn", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.VpnPanel(bot_id=b.id, panel_key="pasarguard", base_url="https://p.example", username="u", password="p", enabled=True))
        s.add(models.PanelAccount(bot_id=b.id, user_telegram_id=USER, email="a@t", panel_key="pasarguard", plan_title="p",
                                  volume_gb=10, days=30, status="active", sub_link="https://x/sub/1"))
        await s.commit()
    box = {"client": FakeClient([WG1, WG2])}
    monkeypatch.setattr(panel_pool, "get_sync", lambda *a, **k: box["client"])

    async def fake_png(bot_id, link):
        return b"PNG"
    monkeypatch.setattr(V, "_qr_png", fake_png)

    async def no_links(client, email, sub_link):     # فقط لینک‌هایِ خودِ کلاینت
        return await client.get_client_links(email)
    monkeypatch.setattr(V, "_links_with_sub_fallback", no_links)
    cap = Cap()
    return dict(V=V, box=box, cap=cap, bot=Bot("123:TESTTOKEN", session=cap))


@pytest.mark.asyncio
async def test_wireguard_files_are_sent_before_the_service_card(env):
    await env["V"]._send_config_delivery(env["bot"], USER, 1)
    calls = env["cap"].calls
    assert calls.count("SendDocument") == 2                       # دو لوکیشن ⇒ دو فایل
    first_card = min(i for i, c in enumerate(calls) if c in ("SendPhoto", "SendMessage"))
    assert max(i for i, c in enumerate(calls) if c == "SendDocument") < first_card   # فایل‌ها اول


@pytest.mark.asyncio
async def test_non_wireguard_service_gets_only_the_card(env):
    env["box"]["client"] = FakeClient([VLESS])
    await env["V"]._send_config_delivery(env["bot"], USER, 1)
    assert "SendDocument" not in env["cap"].calls
    assert any(c in ("SendPhoto", "SendMessage") for c in env["cap"].calls)


@pytest.mark.asyncio
async def test_slow_detection_sends_card_first_then_files(env):
    env["box"]["client"] = FakeClient([WG1], delay=3.0)          # از آستانهٔ ۲٫۵ثانیه دیرتر
    await env["V"]._send_config_delivery(env["bot"], USER, 1)
    for _ in range(80):                                          # فایل‌ها در پس‌زمینه می‌رسند
        await asyncio.sleep(0.1)
        if "SendDocument" in env["cap"].calls:
            break
    calls = env["cap"].calls
    assert "SendDocument" in calls
    assert min(i for i, c in enumerate(calls) if c in ("SendPhoto", "SendMessage")) < calls.index("SendDocument")
