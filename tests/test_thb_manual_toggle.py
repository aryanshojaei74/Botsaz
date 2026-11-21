import asyncio, json
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import tenant, models


class Cb:
    def __init__(self, d):
        self.data = d
        self.message = type("M", (), {"edit_text": staticmethod(lambda *a, **k: _n())})()
    async def answer(self, *a, **k): pass


async def _n(): return None


def test_toggle(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(tenant, "Session", S)
        class R: id = 1
        async def ok(cb, bot): return R()
        monkeypatch.setattr(tenant, "_owner_ok", ok)
        async with S() as s:
            s.add(models.SmmService(id=3, bot_id=1, service_id="m", name="x", is_manual=True))
            s.add(models.MainButton(id=9, bot_id=1, row=0, col=0, label="b"))
            await s.commit()
        await tenant.thb_manual_toggle(Cb("thb:mantog:9:3"), None)
        async with S() as s:
            mb = await s.get(models.MainButton, 9)
        assert json.loads(mb.config) == {"smm_service_ids": [3]}
        await tenant.thb_manual_toggle(Cb("thb:mantog:9:3"), None)
        async with S() as s:
            mb = await s.get(models.MainButton, 9)
        assert mb.action is None
    asyncio.run(go())
