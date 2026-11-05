"""PATCH_TRIAL_BTN_HOURS_V1 (تیکت #912) — مدتِ تستِ «دکمه» هم ساعت بپذیرد (نه فقط روز)."""
import json
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        b = models.Bot(owner_id=5, token="t:1", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        mb = models.MainButton(bot_id=b.id, row=0, col=0, label="تست", action="vpn_trial",
                               config=json.dumps({"trial_days": "3", "trial_gb": "1"}))
        s.add(mb)
        await s.commit()
        mb_id, row = mb.id, b

    async def _row(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", _row)
    monkeypatch.setattr(tenant, "_owner_ok_msg", lambda msg, bot: _row(bot))
    yield SimpleNamespace(mk=mk, mb_id=mb_id)
    await eng.dispose()


class Msg:
    def __init__(self, text):
        self.text, self.chat, self.message_id, self.out = text, SimpleNamespace(id=1), 7, []
        self.from_user = SimpleNamespace(id=5)

    async def answer(self, text, **k):
        self.out.append(text)


class FBot:
    async def delete_message(self, *a, **k): pass
    async def edit_message_text(self, *a, **k): pass


@pytest.mark.asyncio
async def test_button_trial_accepts_hours_and_shows_label(env):
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=5, user_id=5))
    await st.update_data(fix_field="days", trial_mb_id=env.mb_id)
    await st.set_state(tenant._TrialBtnFSM.fix_value)
    await tenant.thb_trial_fix_value(Msg("6 ساعت"), FBot(), st)
    async with env.mk() as s:
        cfg = json.loads((await s.get(models.MainButton, env.mb_id)).config)
    assert cfg["trial_days"] == "0" and cfg["trial_hours"] == "6"
    text, kb = tenant._trial_fix_view(cfg, env.mb_id)
    assert "6 ساعت" in str([b.text for r in kb.inline_keyboard for b in r])
    # ورودیِ عددیِ ساده هنوز «روز» است و ساعتِ قبلی پاک می‌شود
    await st.update_data(fix_field="days", trial_mb_id=env.mb_id)
    await st.set_state(tenant._TrialBtnFSM.fix_value)
    await tenant.thb_trial_fix_value(Msg("2"), FBot(), st)
    async with env.mk() as s:
        cfg = json.loads((await s.get(models.MainButton, env.mb_id)).config)
    assert cfg["trial_days"] == "2" and "trial_hours" not in cfg
