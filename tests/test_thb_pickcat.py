import json, asyncio, pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import tenant, models
from tenant import thb_setsmmcategory_pick


class _Msg:
    async def edit_text(self, *a, **k): pass
    async def answer(self, *a, **k): pass


class _Cb:
    def __init__(self, data):
        self.data = data; self.message = _Msg(); self.alerts = []
    async def answer(self, t="", **k): self.alerts.append(t)


def test_pickcat(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(tenant, "Session", S)
        class R: id = 1
        async def ok(cb, bot): return R()
        async def dis(b): return set()
        monkeypatch.setattr(tenant, "_owner_ok", ok)
        monkeypatch.setattr(tenant, "_disabled_btn_ids", dis)
        async with S() as s:
            s.add(models.SmmCategory(id=5, bot_id=1, platform="x", name="c", is_active=True))
            s.add(models.MainButton(id=9, bot_id=1, row=0, col=0, label="b"))
            await s.commit()
        await thb_setsmmcategory_pick(_Cb("thb:pickcat:9:5"), None)
        async with S() as s:
            mb = await s.get(models.MainButton, 9)
        assert mb.action == "smm_direct_product"
        assert json.loads(mb.config) == {"smm_category_ids": [5]}
    asyncio.run(go())
