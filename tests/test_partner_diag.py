"""PATCH_PARTNER_SILENT_FAIL_V1 / PATCH_PARTNER_DIAG_V1 — ربات با پلنِ «همکار» ولی بدونِ ردیفِ همکار ساکت نماند."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.partner_bot.partner_sales as PS


class _Msg:
    def __init__(self): self.sent = []
    async def answer(self, text, **k): self.sent.append(text)
    async def edit_text(self, text, **k): self.sent.append(text)


class _Cb:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg()
        self.answered = 0
    async def answer(self, *a, **k): self.answered += 1


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    for mod in (config, PS, tenant):
        monkeypatch.setattr(mod, "Session", m)
    PS._PB_CACHE.clear()
    async with m() as s:
        s.add(models.Bot(id=1, owner_id=10, token="1:AAAA", username="pbot", field="partner", plan="partner", status="active"))
        await s.commit()
    async def _row(bot):
        async with m() as s:
            return await s.get(models.Bot, 1)
    monkeypatch.setattr("tenant.get_bot_row", _row)
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_diag_names_missing_partner_row(maker):
    assert "partner_bots" in await PS._partner_diag(object())


@pytest.mark.asyncio
async def test_ps_menu_is_not_silent_for_customer_and_shows_reason_to_owner(maker):
    cust = _Cb(999)
    await PS.ps_menu(cust, object())
    assert cust.message.sent and "در دسترس نیست" in cust.message.sent[0] and "partner_bots" not in cust.message.sent[0]
    owner = _Cb(10)
    await PS.ps_menu(owner, object())
    assert "partner_bots" in owner.message.sent[0]


@pytest.mark.asyncio
async def test_diag_reports_inactive_partner(maker):
    async with maker() as s:
        s.add(models.PartnerBot(bot_id=1, owner_id=10, status="paused"))
        await s.commit()
    assert "paused" in await PS._partner_diag(object())
