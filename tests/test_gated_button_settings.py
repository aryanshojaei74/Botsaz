"""PATCH_GATE_SETTINGS_V1 — دکمهٔ گیت‌شده با زیرمجموعه: تنظیماتِ عملکردِ اصلی در دسترس می‌ماند و گیت پاک نمی‌شود."""
import json
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


def _gated(keep, **extra):
    cfg = {"mode": "access", "req": 3, "keep_action": keep, "src": "join", **extra}
    return NS(id=7, label="x", action="referral_gate", config=json.dumps(cfg), parent_id=None,
              icon_emoji_id=None, rules=None)


def _cbs(mb):
    return [b.callback_data for r in tenant._thb_kb_action(mb, "vpn_sales", "pro").inline_keyboard
            for b in r if b.callback_data]


def test_gated_buy_config_shows_its_settings_but_not_remove_action():
    cbs = _cbs(_gated("buy_config", config_mode="packages"))
    assert "thb:cfgask:7" in cbs and "thb:cfgcat:7" in cbs and "thb:cfgkind:7" in cbs and "thb:rng:vols:7" in cbs
    assert not any(c.startswith("thb:unset") for c in cbs)


def test_gated_free_trial_and_link_show_settings():
    assert "thb:trialmode:7" in _cbs(_gated("free_trial"))
    assert "thb:setlink:7" in _cbs(_gated("channel_link", link_url="https://t.me/chan"))
    assert "thb:unliask:7" in _cbs(_gated("buy_unlimited", unlimited_mode="range"))


def test_plain_button_unchanged_and_helpers():
    mb = NS(id=1, action="buy_config", config=None)
    assert tenant._act(mb) == "buy_config"
    tenant._set_act(mb, "buy_unlimited")
    assert mb.action == "buy_unlimited"
    g = _gated("buy_config")
    assert tenant._act(g) == "buy_config"
    tenant._set_act(g, "buy_config")
    assert g.action == "referral_gate"                      # گیت برداشته نمی‌شود
    nokeep = NS(id=2, action="referral_gate", config=json.dumps({"mode": "access"}))
    assert tenant._act(nokeep) == "referral_gate"


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", mk)
    tenant._SETTING_CACHE.clear()
    cfg = {"mode": "access", "req": 3, "keep_action": "buy_config", "src": "join"}
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.MainButton(id=5, bot_id=1, label="خرید", row=0, col=0, action="referral_gate",
                                config=json.dumps(cfg)))
        await s.commit()
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="vpn_sales")

    async def owner_ok(cb, bot):
        return row
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    return mk


class _CB:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.message = NS(edit_text=self._noop, answer=self._noop, chat=NS(id=1))
        self.from_user = NS(id=9)

    async def _noop(self, *a, **k):
        return None

    async def answer(self, *a, **k):
        self.answers.append((a, k))


@pytest.mark.asyncio
async def test_changing_mode_keeps_the_gate(env):
    await tenant.thb_cfgmode_set(_CB("thb:cfgmode:packages:5"), object())
    async with env() as s:
        mb = await s.get(models.MainButton, 5)
    cfg = json.loads(mb.config)
    assert mb.action == "referral_gate" and cfg["keep_action"] == "buy_config" and cfg["req"] == 3
    assert cfg["config_mode"] == "packages"
