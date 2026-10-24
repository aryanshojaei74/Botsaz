"""PATCH_VC_SIMPLE_MODE_V1 — حالتِ ساده‌یِ «حجم دلخواه»: فقط عددِ حجم، مدتِ ثابتِ ادمین."""
from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU


def _cfg(simple):
    c = dict(VA._VC_DEFAULTS)
    c.update(vc_min_gb="5", vc_max_gb="100", vc_step_gb="5", vc_min_days="30", vc_max_days="90",
             vc_max_count="5", vc_price_gb="2500", vc_price_day="0", vc_simple=simple)
    return c


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_defaults_and_panel_keys_registered():
    assert VA._VC_DEFAULTS["vc_simple"] == "0" and VA._VC_DEFAULTS["vc_simple_days"] == "30"
    assert "vc_simple" in VA._VC_PANEL_KEYS and "vc_simple_days" in VA._VC_PANEL_KEYS


def test_simple_keyboard_hides_days_and_count_rows():
    full = _cbs(VU._vc_cfg_kb(_cfg("0"), 10, 30, 1))
    simple = _cbs(VU._vc_cfg_kb(_cfg("1"), 10, 30, 1))
    assert "vc:d+" in full and "vc:c+" in full and "vc:gb+" in full
    assert "vc:d+" not in simple and "vc:c+" not in simple and "vc:gb+" not in simple
    assert "vc:gb:manual" in simple and any(c.startswith("vc:checkout:10:30:1") for c in simple)


def test_user_side_defaults_know_the_simple_keys():
    """باگِ واقعی: vpn_user نسخهٔ جداگانهٔ _VC_DEF/_VC_PANEL_KEYS_U دارد؛ بدونِ این کلیدها حالتِ ساده هیچ‌وقت خوانده نمی‌شد."""
    assert VU._VC_DEF["vc_simple"] == "0" and VU._VC_DEF["vc_simple_days"] == "30"
    assert "vc_simple" in VU._VC_PANEL_KEYS_U and "vc_simple_days" in VU._VC_PANEL_KEYS_U
    assert VU._pvc_key_u("pasargad", "vc_simple") == "vc_pasargad_simple"
    from tenant.bots.vpn import vpn_admin as VA2
    assert VA2._pvc_key("pasargad", "vc_simple") == "vc_pasargad_simple"      # ادمین و کاربر همان کلید را می‌خوانند/می‌نویسند
    assert {k for k in VA2._VC_DEFAULTS if k.startswith("vc_")} <= set(VU._VC_DEF)


import pytest
import pytest_asyncio
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


class _St:
    def __init__(self):
        self.d, self.s = {}, None

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d, self.s = {}, None


@pytest_asyncio.fixture
async def vcdb(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, VU):
        monkeypatch.setattr(mod, "Session", maker)
    import tenant.bots.vpn.vpn_admin as VAm
    monkeypatch.setattr(VAm, "Session", maker, raising=False)
    VU._VC_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", balance=0))
        await s.commit()
    for k, v in (("vc_pasargad_enabled", "1"), ("vc_pasargad_simple", "1"), ("vc_pasargad_simple_days", "45"),
                 ("vc_pasargad_min_gb", "5"), ("vc_pasargad_max_gb", "100"), ("vc_pasargad_step_gb", "5"),
                 ("vc_pasargad_price_gb", "3500")):
        await tenant.set_setting(9, k, v)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_vc_open_in_simple_mode_asks_only_for_gb_with_admin_days(vcdb):
    sent = []

    class M:
        message_id = 5
        chat = SimpleNamespace(id=1001)

        async def edit_text(self, text, reply_markup=None, **k):
            sent.append(text)
    cb = SimpleNamespace(data="vc:open:pasargad", from_user=SimpleNamespace(id=1001), message=M())

    async def _ans(*a, **k):
        pass
    cb.answer = _ans
    st = _St()
    await VU.vc_open(cb, SimpleNamespace(token="9:T"), st)
    assert st.s == VU.VpnConfiguratorFSM.entering_manual
    assert st.d["vc_days"] == 45 and st.d["vc_manual_field"] == "gb"
    assert sent and "3,500" in sent[0] and "5" in sent[0] and "100" in sent[0] and "45 روز" in sent[0]


@pytest.mark.asyncio
async def test_plans_page_without_fixed_plans_goes_straight_to_simple_prompt(vcdb, monkeypatch):
    import tenant.bots.vpn.vpn_admin as VAm
    for mod in (VAm,):
        monkeypatch.setattr(mod, "Session", vcdb, raising=False)
    async with vcdb() as s:
        s.add(models.VpnPanel(id=3, bot_id=9, panel_key="pasargad", base_url="https://x", username="u",
                              password="p", enabled=True))
        await s.commit()
    out = []

    class M:
        from_user = SimpleNamespace(id=1001)

        async def answer(self, text, reply_markup=None, **k):
            out.append(text)
            return SimpleNamespace(message_id=77)
    st = _St()
    await VU.show_vpn_plans(SimpleNamespace(token="9:T"), M(), 9, state=st)
    assert out and "حجمِ درخواستیِ خود" in out[0] and "3,500" in out[0] and "45 روز" in out[0]
    assert st.s == VU.VpnConfiguratorFSM.entering_manual and st.d["vc_manual_prompt_id"] == 77
