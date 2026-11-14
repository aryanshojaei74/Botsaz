import asyncio, json
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import models
from tenant.bots.virtual_service import smm


class Cb:
    def __init__(self, data):
        self.data = data; self.alerts = []
        self.message = type("M", (), {"edit_text": staticmethod(lambda *a, **k: _noop()),
                                      "answer": staticmethod(lambda *a, **k: _noop())})()
        self.from_user = type("U", (), {"id": 1})()
    async def answer(self, t="", **k): self.alerts.append(t)


async def _noop(): return None


def test_attach_and_detach(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(smm, "Session", S)
        class R: id = 1
        async def own(cb, bot): return R()
        monkeypatch.setattr(smm, "_owner", own)
        async def gs(b, k, d=""): return d
        monkeypatch.setattr(smm, "_get_setting", gs)
        async with S() as s:
            s.add(models.SmmService(id=3, bot_id=1, service_id="man1", name="x", is_manual=True))
            s.add(models.SmmService(id=4, bot_id=1, service_id="man2", name="y", is_manual=True))
            s.add(models.MainButton(id=9, bot_id=1, row=0, col=0, label="b"))
            await s.commit()
        await smm.smm_manbtn_apply(Cb("smm:manbtn:set:3:9"), None)
        await smm.smm_manbtn_apply(Cb("smm:manbtn:set:4:9"), None)
        async with S() as s:
            mb = await s.get(models.MainButton, 9)
        assert mb.action == "smm_direct_product"
        assert json.loads(mb.config) == {"smm_service_ids": [3, 4]}
        await smm.smm_manbtn_apply(Cb("smm:manbtn:off:3:9"), None)
        await smm.smm_manbtn_apply(Cb("smm:manbtn:off:4:9"), None)
        async with S() as s:
            mb = await s.get(models.MainButton, 9)
        assert mb.action is None
        t, kb = await smm._manbtn_content(1, 3)
        assert any("smm:manbtn:set:3:9" in b.callback_data for r in kb.inline_keyboard for b in r)
        t, kb = await smm._smm_myservice_detail_content(1, 3, 0)
        assert any(b.callback_data == "smm:manlist" for r in kb.inline_keyboard for b in r)
    asyncio.run(go())


def test_fields_and_delivery(monkeypatch, tmp_path):
    from aiogram.fsm.context import FSMContext
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(smm, "Session", S)
        class R: id = 1
        async def own(cb, bot): return R()
        monkeypatch.setattr(smm, "_owner", own)
        async def gs(b, k, d=""): return d
        monkeypatch.setattr(smm, "_get_setting", gs)
        sent = []
        async def nu(bid, oid, text, bot=None): sent.append(text)
        monkeypatch.setattr(smm, "_notify_user", nu)
        async with S() as s:
            s.add(models.SmmService(id=3, bot_id=1, service_id="man1", name="x", is_manual=True,
                                    delivery_text="کد شما", input_prompt="آیدی؟"))
            s.add(models.SmmOrder(id=7, bot_id=1, smm_service_pk=3, service_id="man1", service_name="x", link="l", user_telegram_id=5,
                                  quantity=1, price_toman=1, status="awaiting_admin"))
            await s.commit()
        await smm._send_delivery_text(1, 7)
        assert sent == ["📨 کد شما"]
        t, kb = await smm._smm_myservice_detail_content(1, 3, 0)
        cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
        assert "smm:mf:img:3:0" in cbs and "smm:mf:prompt:3:0" in cbs and "smm:mf:deliv:3:0" in cbs
    asyncio.run(go())
