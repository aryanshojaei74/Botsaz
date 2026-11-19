"""لاگِ سرور: «vget:single» تا ۷۴۶ثانیه روی پنلِ بی‌پاسخ گیر کرد ⇒ سقفِ کلیِ ثابت + پیامِ خطا به مشتری."""
import asyncio
import sys
import types

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import vpn_user as VU

pytestmark = pytest.mark.asyncio


class _Msg:
    def __init__(self): self.answers = []

    async def answer(self, text, **k): self.answers.append(text)


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    getattr(tenant, "_SETTING_CACHE", {}).clear()
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.PanelAccount(id=4, bot_id=7, user_telegram_id=500, email="e1", panel_key="marzban", sub_link=""))
        await s.commit()
    yield mk
    await eng.dispose()


async def test_hanging_panel_gives_up_and_tells_customer(env, monkeypatch):
    closed = []

    class Hang:
        async def get_client_links(self, email):
            await asyncio.sleep(3600)

        async def close(self): closed.append(1)

    async def noop(*a, **k): return 0

    async def panel_for(s, bid, a): return types.SimpleNamespace(panel_key="marzban")
    monkeypatch.setattr(VU, "_panel_for_account", panel_for)
    monkeypatch.setattr(VU, "_client_from_panel", lambda p: Hang())
    monkeypatch.setattr(VU, "_send_sub_wg_files", noop)
    monkeypatch.setattr(VU, "_send_native_wg_files", noop)
    monkeypatch.setattr(VU, "_SINGLE_CONFIG_DEADLINE", 0.2)
    msg = _Msg()

    async def ans(*a, **k): return None
    cb = types.SimpleNamespace(data="vget:single:4", from_user=types.SimpleNamespace(id=500), message=msg, answer=ans)
    bot = types.SimpleNamespace(token="7:T", send_message=ans)
    await asyncio.wait_for(VU.vget_single(cb, bot), timeout=5)       # قبلاً تا ساعت‌ها (۷۴۶ث) گیر می‌کرد
    assert closed == [1]                                              # کلاینت بسته شد
    assert any("در دسترس نیست" in t for t in msg.answers)             # مشتری پیام گرفت
