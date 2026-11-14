"""PATCH_PLAN_CUSTOM_TEXT_V1 (پیشنهاد #208) — هر پلن: حجم/مدتِ خودکار قابلِ پنهان‌شدن + توضیحِ دلخواهِ فاکتور."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant.bots.vpn.vpn_partners  # noqa: F401 — مدلِ vpn_partners باید قبل از create_all در metadata باشد
    import tenant.gateways.discount_engine  # noqa: F401
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import sys
    import config
    real_session = config.Session                          # قبل از حلقه ذخیره شود (خودِ config هم داخلِ حلقه عوض می‌شود)
    for mod in list(sys.modules.values()):                 # هر ماژولی که Session را از config گرفته (vpn_partners، discount_engine، ...)
        if getattr(mod, "Session", None) is real_session:
            monkeypatch.setattr(mod, "Session", mk)
    for mod in (tenant, VA, VU):
        monkeypatch.setattr(mod, "Session", mk)
    monkeypatch.setattr(config, "Session", mk)             # importهایِ تنبل (from config import Session داخلِ تابع)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.VpnPlan(id=7, bot_id=1, panel_key="pasarguard", title="نامحدود یک ماهه", volume_gb=0, days=30,
                             price=280000, active=True))
        await s.commit()
    return mk


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_plan_defaults_show_spec_and_no_note(env):
    async with env() as s:
        p = await s.get(models.VpnPlan, 7)
    assert p.hide_spec is False and p.invoice_note is None
    text, kb = VA._plan_menu_view(p, "pasarguard")
    assert "tvpn:planspec:pasarguard:7" in _cbs(kb) and "tvpn:planedit:pasarguard:note:7" in _cbs(kb)
    assert "✅ نمایش" in text and "توضیحِ فاکتور" not in text


class _CB:
    def __init__(self, data):
        self.data = data
        self.alerts = []
        self.message = NS(edit_text=self._e, answer=self._e, chat=NS(id=1), message_id=1)
        self.from_user = NS(id=9)

    async def _e(self, *a, **k):
        return NS(message_id=5)

    async def answer(self, text="", **k):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_toggle_hide_spec(env, monkeypatch):
    row = NS(id=1, owner_id=9, token="1:T", plan="growth", status="active", field="vpn_sales")

    async def plan_get(cb, bot, pid):
        async with env() as s:
            return row, await s.get(models.VpnPlan, pid)
    monkeypatch.setattr(VA, "_plan_get", plan_get)
    await VA.vpn_plan_spec_toggle(_CB("tvpn:planspec:pasarguard:7"), NS())
    async with env() as s:
        assert (await s.get(models.VpnPlan, 7)).hide_spec is True
    await VA.vpn_plan_spec_toggle(_CB("tvpn:planspec:pasarguard:7"), NS())
    async with env() as s:
        assert (await s.get(models.VpnPlan, 7)).hide_spec is False


@pytest.mark.asyncio
async def test_note_is_pro_only(env, monkeypatch):
    async def plan_get_growth(cb, bot, pid):
        async with env() as s:
            return NS(id=1, plan="growth"), await s.get(models.VpnPlan, pid)
    monkeypatch.setattr(VA, "_plan_get", plan_get_growth)
    cb = _CB("tvpn:planedit:pasarguard:note:7")
    await VA.vpn_plan_edit_ask(cb, NS(), NS())
    assert cb.alerts and "حرفه‌ای" in cb.alerts[0]                       # رشد: قفل
    from plans import has_feature, Feature
    assert has_feature("pro", Feature.PLAN_INVOICE_NOTE) and not has_feature("growth", Feature.PLAN_INVOICE_NOTE)


@pytest.mark.asyncio
async def test_buy_summary_hides_auto_spec_and_shows_plan_note(env, monkeypatch):
    async with env() as s:
        p = await s.get(models.VpnPlan, 7)
        p.hide_spec, p.invoice_note = True, "مصرف منصفانه ۱۰۰ گیگ"
        await s.commit()
    monkeypatch.setattr(VU, "_plan_allowed_for", lambda *a, **k: _true(), raising=False)
    out = await VU._build_buy_summary(NS(token="1:T"), 1, 7, 55, None)
    assert out is not None
    text = out[0]
    assert "نامحدود یک ماهه" in text                         # عنوانِ دلخواهِ مالک
    assert "📝 مصرف منصفانه ۱۰۰ گیگ" in text                # توضیحِ فاکتور
    assert "📦 حجم" not in text and "📅 مدت" not in text      # حجم/مدتِ خودکار پنهان


async def _true():
    return True


@pytest.mark.asyncio
async def test_buy_summary_unchanged_when_not_hidden(env, monkeypatch):
    monkeypatch.setattr(VU, "_plan_allowed_for", lambda *a, **k: _true(), raising=False)
    out = await VU._build_buy_summary(NS(token="1:T"), 1, 7, 55, None)
    text = out[0]
    assert "📦 حجم: نامحدود" in text and "📅 مدت: 30 روز" in text and "📝" not in text


from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402


class _Msg:
    def __init__(self, text):
        self.text = text
        self.chat = NS(id=1)
        self.message_id = 3
        self.answers = []

    async def answer(self, text, **k):
        self.answers.append(text)
        return NS(message_id=9)


class _FakeBot:
    async def delete_message(self, *a, **k):
        return None

    async def edit_message_text(self, *a, **k):
        return None


async def _edit(env, monkeypatch, field, text, plan="pro"):
    async def owner(msg, bot):
        return NS(id=1, owner_id=9, token="1:T", plan=plan, status="active", field="vpn_sales")
    monkeypatch.setattr(VA, "_owner_msg", owner)
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=9))
    await st.update_data(pe_field=field, pe_plan_id=7, pe_key="pasarguard", pe_prompt_id=None, pe_panel_chat=1, pe_panel_id=None)
    m = _Msg(text)
    await VA.vpn_plan_edit_value(m, _FakeBot(), st)
    async with env() as s:
        return m, await s.get(models.VpnPlan, 7)


@pytest.mark.asyncio
async def test_title_limit_is_20_normally_and_40_when_spec_is_hidden(env, monkeypatch):
    long_title = "نامحدود یک ماهه ۲۸۰ تومان مصرف عادلانه"          # ۳۹ کاراکتر
    m, p = await _edit(env, monkeypatch, "title", long_title)
    assert p.title == "نامحدود یک ماهه" and "حداکثر 20" in m.answers[0]    # مثل قبل
    async with env() as s:
        pl = await s.get(models.VpnPlan, 7)
        pl.hide_spec = True
        await s.commit()
    m, p = await _edit(env, monkeypatch, "title", long_title)
    assert p.title == long_title


@pytest.mark.asyncio
async def test_pro_can_set_and_clear_the_invoice_note_growth_cannot(env, monkeypatch):
    _m, p = await _edit(env, monkeypatch, "note", "مصرف منصفانه ۱۰۰ گیگ", plan="pro")
    assert p.invoice_note == "مصرف منصفانه ۱۰۰ گیگ"
    _m, p = await _edit(env, monkeypatch, "note", "-", plan="pro")
    assert p.invoice_note is None
    _m, p = await _edit(env, monkeypatch, "note", "x" * 301, plan="pro")
    assert p.invoice_note is None                                     # بلندتر از ۳۰۰ ⇒ رد
    m, p = await _edit(env, monkeypatch, "note", "متن", plan="growth")
    assert p.invoice_note is None and "حرفه‌ای" in m.answers[0]


# ── پیشنهاد #209 / #210 ──
def test_table_cell_shows_leading_emoji_or_whole_title_when_spec_hidden():
    p = NS(title="🔥 نامحدود", hide_spec=False)
    assert VU._table_title_cell(p, "30GB") == "🔥 30GB"
    assert VU._table_title_cell(NS(title="نامحدود", hide_spec=False), "30GB") == "30GB"
    assert VU._table_title_cell(NS(title="🔥 ویژه", hide_spec=True), "30GB") == "🔥 ویژه"
    assert "🔥" in VU._plan_table_row(NS(id=1, title="🔥 x", hide_spec=False, volume_gb=30, days=30, price=1000,
                                          limit_ip=0))[2].text


@pytest.mark.asyncio
async def test_my_services_button_uses_panel_name_when_enabled(env):
    import tenant
    async with env() as s:
        s.add(models.PanelAccount(id=1, bot_id=1, user_telegram_id=5, email="vik_ab12", panel_key="pasarguard",
                                  volume_gb=30, days=30, plan_title="پلن", status="active"))
        await s.commit()
    sent = []

    class M:
        chat = NS(id=5)

        async def answer(self, text, reply_markup=None, **k):
            sent.append(reply_markup)
            return NS(message_id=2)

    class B:
        async def delete_message(self, *a, **k):
            return None
    await VU.show_my_services(B(), M(), 1, 5)
    assert "30" in sent[-1].inline_keyboard[0][0].text and "vik_ab12" not in sent[-1].inline_keyboard[0][0].text
    await tenant.set_setting(1, "svc_btn_name", "1")
    await VU.show_my_services(B(), M(), 1, 5)
    b = sent[-1].inline_keyboard[0][0]
    assert "vik_ab12" in b.text and "30" not in b.text and b.style == "success"
