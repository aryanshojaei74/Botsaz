"""PATCH_VC_DISCOUNT_ONLY_AT_PAYMENT_V1 / PATCH_VC_MANUAL_PROMPT_INPLACE_V1 / PATCH_VC_BACK_PREV_MENU_V1"""
from types import SimpleNamespace

import pytest

from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU


def _cfg():
    c = dict(VA._VC_DEFAULTS)
    c.update(vc_min_gb="5", vc_max_gb="100", vc_step_gb="5", vc_min_days="30", vc_max_days="90",
             vc_max_count="5", vc_price_gb="2500", vc_price_day="0")
    return c


def _datas(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_selection_page_has_no_discount_row_even_when_enabled():
    kb = VU._vc_cfg_kb(_cfg(), 10, 30, 1, discount_enabled=True)
    d = _datas(kb)
    assert "vc:disc:add" not in d and "vc:disc:del" not in d
    assert any(x.startswith("vc:checkout:") for x in d)


class _FakeState:
    def __init__(self, d):
        self.d, self.s = dict(d), None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **k):
        self.d.update(k)

    async def set_state(self, s):
        self.s = s


@pytest.mark.asyncio
async def test_manual_prompt_edits_page_in_place(monkeypatch):
    import config
    import models
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, VU):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()

    async def recover(*a, **k):
        return None

    async def allcfg(*a, **k):
        return _cfg()
    monkeypatch.setattr(VU, "_vc_recover_panel_key", recover)
    monkeypatch.setattr(VU, "_vc_all_for_user", allcfg)
    edits, sends = [], []

    class Msg:
        message_id = 11
        chat = SimpleNamespace(id=1001)

        async def edit_text(self, text, reply_markup=None, **k):
            edits.append((text, reply_markup))

        async def answer(self, *a, **k):
            sends.append(a)
    answered = []

    async def ans(*a, **k):
        answered.append(a)
    cb = SimpleNamespace(data="vc:gb:manual", from_user=SimpleNamespace(id=1001), message=Msg(), answer=ans)
    st = _FakeState({"vc_gb": 10, "vc_days": 30, "vc_count": 1, "vc_panel_key": ""})

    class B:
        token = "9:T"

        async def send_message(self, *a, **k):
            sends.append(a)
    await VU.vc_manual_prompt(cb, B(), st)
    assert edits and "بفرست" in edits[0][0] and _datas(edits[0][1]) == ["vc:manual:back"]
    assert not sends and st.d["vc_manual_inplace"] is True and st.d["vc_manual_prompt_id"] == 11
    await engine.dispose()


def test_edit_first_exposes_real_user_not_the_bot():
    """باگِ WHALE SHOP: موجودی صفر نمایش داده می‌شد چون from_user همان خودِ ربات بود."""
    bot_user, real = SimpleNamespace(id=8354597347, is_bot=True), SimpleNamespace(id=1001, is_bot=False)
    msg = SimpleNamespace(from_user=bot_user, chat=SimpleNamespace(id=5))
    assert VU._EditFirst(msg).from_user.id == 8354597347           # رفتارِ قدیمی (بدونِ کاربر)
    w = VU._EditFirst(msg, real)
    assert w.from_user.id == 1001 and w.chat.id == 5


@pytest.mark.asyncio
async def test_simple_mode_back_without_plans_goes_to_previous_menu_not_page(monkeypatch):
    import config
    import models
    import tenant
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, VU):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    simple = {"v": "1"}

    async def allcfg(*a, **k):
        c = _cfg()
        c["vc_simple"] = simple["v"]
        return c
    monkeypatch.setattr(VU, "_vc_all_for_user", allcfg)
    went = []

    async def fake_smopen(cb, bot, state):
        went.append(cb.data)
    monkeypatch.setattr(tenant, "smopen_handler", fake_smopen)
    monkeypatch.setattr(tenant, "_last_menu_for", lambda bot_id: 42)
    pages = []

    async def fake_plans(*a, **k):
        pages.append("plans")
    monkeypatch.setattr(VU, "show_vpn_plans", fake_plans)

    class St(_FakeState):
        async def clear(self):
            self.d = {}
    cleared = St({"vc_panel_key": ""})

    async def ans(*a, **k):
        return None

    class Msg:
        message_id = 3
        chat = SimpleNamespace(id=1001)
    cb = SimpleNamespace(data="vc:back", from_user=SimpleNamespace(id=1001), message=Msg(), answer=ans)
    cb.model_copy = lambda update: SimpleNamespace(**{**cb.__dict__, **update})
    await VU.vc_adjust(cb, SimpleNamespace(token="9:T"), cleared)
    assert went == ["smopen:42"] and pages == []
    simple["v"] = "0"                                              # حالتِ ساده خاموش ⇒ مسیرِ قبلی (لیستِ پلن‌ها)
    went.clear()
    await VU.vc_adjust(cb, SimpleNamespace(token="9:T"), St({"vc_panel_key": ""}))
    assert went == [] and pages == ["plans"]
    await engine.dispose()
