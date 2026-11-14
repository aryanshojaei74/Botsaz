"""PATCH_TRUST_TEMPLATE_NO_VARS_GUARD_V1 — قالبِ کانالِ اعتمادسازی نباید به‌اشتباه اسمِ یک دکمهٔ منو شود (@INFIdorbot)."""
import pytest
from types import SimpleNamespace as NS

import tenant
import tenant.shared.trust_report as TR


def test_has_template_vars():
    assert TR.has_template_vars("سفارش {product} به مبلغ {amount}")
    assert not TR.has_template_vars("🏆 نفرات برتر")
    assert not TR.has_template_vars("")


class _St:
    def __init__(self): self.data, self.cleared = {}, False
    async def get_data(self): return dict(self.data)
    async def update_data(self, **k): self.data.update(k)
    async def clear(self): self.cleared = True


class _Msg:
    def __init__(self, text):
        self.text, self.html_text = text, text
        self.from_user = NS(id=1)
        self.out = []
    async def answer(self, t, **k): self.out.append(t)


@pytest.fixture
def env(monkeypatch):
    saved = {}

    async def gbr(bot): return NS(id=5, plan="pro", owner_id=1)

    async def soa(a, b, c): return True

    async def setset(bid, k, v): saved[k] = v

    async def mk(*a, **k): return None

    async def menu(bid, plan): return ("menu", None)

    async def labels(bid): return {"🔢شماره مجازی", "💰کیف پول"}

    class B:
        async def me(self): return NS(username="b")
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "is_owner_or_admin", soa)
    monkeypatch.setattr(tenant, "set_setting", setset)
    monkeypatch.setattr(tenant, "main_kb_or_remove", mk)
    monkeypatch.setattr(TR, "trust_report_menu_text_kb", menu)
    monkeypatch.setattr(TR, "_menu_labels", labels)
    return saved, B()


@pytest.mark.asyncio
async def test_menu_label_is_never_saved(env):
    saved, bot = env
    st, m = _St(), _Msg("🔢شماره مجازی")
    await TR.trust_edittext_save(m, bot, st)
    assert saved == {} and "اسمِ یک دکمهٔ منو" in m.out[-1]


@pytest.mark.asyncio
async def test_static_text_needs_a_second_confirming_send(env):
    saved, bot = env
    st = _St()
    m1 = _Msg("🏆 نفرات برتر")
    await TR.trust_edittext_save(m1, bot, st)
    assert saved == {} and "هیچ جاگذاری" in m1.out[-1]            # بار اول: ذخیره نشد
    m2 = _Msg("🏆 نفرات برتر")
    await TR.trust_edittext_save(m2, bot, st)
    assert saved.get("trust_channel_template") == "🏆 نفرات برتر"    # تأیید با ارسالِ دوباره


@pytest.mark.asyncio
async def test_text_with_variables_saves_immediately(env):
    saved, bot = env
    st, m = _St(), _Msg("✅ {product} — {amount} تومان")
    await TR.trust_edittext_save(m, bot, st)
    assert saved.get("trust_channel_template") == "✅ {product} — {amount} تومان"
