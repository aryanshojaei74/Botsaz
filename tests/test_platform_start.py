"""/start در رباتِ مادر — بدونِ آرگومان (عادی)، با ?start=pr_کد، و با رفرال."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as PB
import platform_promoters as PR


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PB, PR):
        monkeypatch.setattr(mod, "Session", maker)
    store = {}

    async def gp(k, d=""):
        return store.get(k, d)
    monkeypatch.setattr(PB, "get_pset", gp)

    async def noop(*a, **k):
        return None
    for name in ("ensure_home_loaded", "sweep_trials"):
        monkeypatch.setattr(PB, name, noop)

    async def kb(*a, **k):
        return None
    monkeypatch.setattr(PB, "smart_home_kb", kb)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=7, full_name="U", balance=0, rules_accepted=True))
        s.add(models.PlatformPromoter(id=1, code="ali", name="Ali", percent=20, active=True))
        await s.commit()
    yield maker
    await engine.dispose()


class Msg:
    def __init__(self):
        self.from_user = SimpleNamespace(id=7, full_name="U", first_name="U", username="u")
        self.chat = SimpleNamespace(type="private", id=7)
        self.out = []

    async def answer(self, text, **k):
        self.out.append(text)

    async def answer_sticker(self, *a, **k):
        pass

    async def react(self, *a, **k):
        pass


class St:
    async def clear(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [None, "", "pr_ali", "12345"])
async def test_start_replies_with_home_menu(env, args):
    m = Msg()
    await PB.start(m, SimpleNamespace(args=args), St(), SimpleNamespace())
    assert any("بات‌ساز" in t or "منوی اصلی" in t for t in m.out), m.out
    if args == "pr_ali":
        assert any("تخفیفِ خریدِ اول" in t for t in m.out)
