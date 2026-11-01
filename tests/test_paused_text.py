"""PATCH_PAUSED_TEXT_EDIT_V1 (DevelOper) — مالک متنِ «ربات خاموش است» را ویرایش می‌کند."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    async with m() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="vpn_sales", plan="growth", status="paused"))
        await s.commit()
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_then_custom_then_reset(env):
    assert await tenant.paused_notice(5) == tenant.DEFAULT_PAUSED_TEXT
    await tenant.set_setting(5, "paused_text", "  ربات تا ساعت ۱۲ خاموش است  ")
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(5) == "ربات تا ساعت ۱۲ خاموش است"
    await tenant.set_setting(5, "paused_text", "")
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(5) == tenant.DEFAULT_PAUSED_TEXT


def test_alert_text_strips_html_and_fits_telegram_limit():
    t = tenant.paused_alert_text("<b>سلام</b> &amp; <a href='x'>لینک</a>")
    assert t == "سلام & لینک"
    long = tenant.paused_alert_text("ا" * 500)
    assert len(long) <= 196 and long.endswith("…")
    assert tenant.paused_alert_text("") == tenant.DEFAULT_PAUSED_TEXT


class _Msg:
    def __init__(self, text, html=None):
        self.text, self.html_text, self.sent = text, html or text, []
        self.from_user = NS(id=1, full_name="o", username=None)

    async def answer(self, text, **k):
        self.sent.append(text)


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


@pytest.mark.asyncio
async def test_save_handler_stores_html_enforces_limit_and_skip_resets(env, monkeypatch):
    async def row(msg, bot): return NS(id=5, owner_id=1)
    monkeypatch.setattr(tenant, "_owner_ok_msg", row)
    st = _State()
    m = _Msg("متن <جدید>", html="<b>متن</b> جدید")
    await tenant.set_paused_text_save(m, st, None)
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(5) == "<b>متن</b> جدید" and st.cleared
    st2 = _State()
    big = _Msg("ا" * (tenant.PAUSED_TEXT_MAX + 1))
    await tenant.set_paused_text_save(big, st2, None)
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(5) == "<b>متن</b> جدید" and not st2.cleared and "کوتاه" in big.sent[0]
    sk = _Msg("/skip")
    await tenant.set_paused_text_save(sk, _State(), None)
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(5) == tenant.DEFAULT_PAUSED_TEXT


@pytest.mark.asyncio
async def test_button_is_in_settings_keyboard_and_dispatcher(env):
    kb = await tenant._sec_settings_kb(5, "growth", "inline", True)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "set:paused_text" in cbs
    kb2 = await tenant._sec_settings_kb(5, "growth", "reply", True)
    labels = [b.text for r in kb2.keyboard for b in r]
    assert "✏️ متن خاموش بودن ربات" in labels
    src = open(tenant.__file__, encoding="utf-8").read()
    assert '"✏️ متن خاموش بودن ربات": set_paused_text_start' in src       # حالتِ کیبوردی هم هندلر دارد
    assert src.count("await paused_notice(row.id)") >= 3                  # هر سه جایِ پیامِ «خاموش» از متنِ قابل‌ویرایش می‌خوانند


# ── لایهٔ وب‌هوک: همان متنِ سفارشی در پیام و هشدار ──
def test_webhook_alert_text_is_plain_and_short():
    import webhook_utils as W
    assert W._alert_plain("🗄 ربات شما <b>حذف نشده</b> &amp; ok") == "🗄 ربات شما حذف نشده & ok"
    t = W._alert_plain("ا" * 400)
    assert len(t) <= 196 and t.endswith("…")


def test_main_tenant_uses_custom_paused_text_for_owner_pause():
    import pathlib
    # main_tenant را import نمی‌کنیم: ساختنِ اپ روترِ tenant را به Dispatcher وصل می‌کند و تست‌هایِ دیگر را می‌شکند
    src = (pathlib.Path(__file__).resolve().parent.parent / "main_tenant.py").read_text(encoding="utf-8")
    assert 'get_setting as _gs_pt' in src and '"paused_text"' in src
