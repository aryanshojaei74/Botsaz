"""PATCH_SMM_BACK_HOME_FLAG_V1 / PATCH_SMM_QL_BACK_V1 / PATCH_SMM_BULK_TOGGLE_V1 / PATCH_SMM_AI_CLEANUP_V1 / PATCH_SMM_AI_BUTTON_ACTION_V1
(SINA — خدماتِ مجازی: بازگشتِ صفحهٔ محصول، خاموش/روشنِ سفارشِ انبوه، پاک‌شدنِ پیام‌هایِ موقتِ دستیار، دستیار به‌عنوانِ عملکردِ دکمه)."""
from types import SimpleNamespace as NS

import pytest

import models
import tenant
from tenant.bots.virtual_service import smm, smm_ai
from tests.test_smm_partial import db  # noqa: F401


async def _shop(db, monkeypatch):
    async with db() as s:
        bot = models.Bot(owner_id=1, token="9:T", username="shop9", field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        svc = models.SmmService(bot_id=bot.id, service_id="12", name="ممبر", min_qty=10, max_qty=1000, sell_rate=1000, is_active=True)
        s.add(svc)
        await s.commit()
        bid, sid = bot.id, svc.id

    async def fake_row(b):
        return NS(id=bid, plan="pro", field="virtual_service", owner_id=1)
    monkeypatch.setattr(tenant, "get_bot_row", fake_row)
    return bid, sid


class Msg:
    def __init__(self):
        self.shown = []

    async def edit_text(self, text, **kw):
        self.shown.append((text, kw.get("reply_markup")))

    async def answer(self, text, **kw):
        self.shown.append((text, kw.get("reply_markup")))
        return NS(chat=NS(id=1), message_id=len(self.shown) + 100)


class CB(NS):
    async def answer(self, *a, **k):
        pass


class St:
    def __init__(self):
        self.d, self.state = {}, None

    async def set_state(self, s=None, *a, **k): self.state = s
    async def update_data(self, **k): self.d.update(k)
    async def get_data(self): return dict(self.d)
    async def clear(self): self.d.clear(); self.state = None


def cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_shortcut_back_goes_home_not_global_menus(db, monkeypatch):
    bid, sid = await _shop(db, monkeypatch)
    monkeypatch.setattr(smm, "CallbackQuery", CB)
    m = Msg()
    assert await smm.send_smm_product_page(CB(message=m), None, sid, St())
    back = [c for c in cbs(m.shown[-1][1]) if c.startswith("smmu:type:")][0]
    assert back.endswith(":1h")                                                  # فلگِ «h»: از میانبر آمده
    tree, _ = await smm._user_active_tree(bid)
    pkey = next(iter(tree))
    tkey = next(iter(tree[pkey]))
    # لیست با فلگِ h: دکمهٔ آخر «منویِ اصلی» است و محصول‌ها فلگ را نگه می‌دارند
    m2 = Msg()
    await smm.smmu_pick_type(CB(data=f"smmu:type:{pkey}:{tkey}:1h", message=m2), None)
    kb = m2.shown[-1][1]
    assert cbs(kb)[-1] == "mbtn:home" and "smmu:plat:" + pkey not in cbs(kb)
    assert any(c.startswith("smmu:svc:") and c.endswith("h") for c in cbs(kb))
    # لیست بدونِ فلگ (مرورِ عادی): مثلِ قبل به لیستِ نوع‌هایِ همان پلتفرم برمی‌گردد
    m3 = Msg()
    await smm.smmu_pick_type(CB(data=f"smmu:type:{pkey}:{tkey}:1", message=m3), None)
    assert cbs(m3.shown[-1][1])[-1] == f"smmu:plat:{pkey}"
    # صفحهٔ محصول از لیستِ فلگ‌دار: back_cb همان لیست با فلگ
    got = {}

    async def fake_page(cb, bot, vid, state, breadcrumb=None, back_cb=None):
        got["back"] = back_cb
        return True
    monkeypatch.setattr(smm, "send_smm_product_page", fake_page)
    await smm.smmu_pick_service(CB(data=f"smmu:svc:{sid}:{pkey}:{tkey}:2h", message=Msg()), None, St())
    assert got["back"] == f"smmu:type:{pkey}:{tkey}:2h"


@pytest.mark.asyncio
async def test_quicklink_product_has_back_to_its_list(db, monkeypatch):
    bid, sid = await _shop(db, monkeypatch)
    got = {}

    async def fake_page(cb, bot, vid, state, breadcrumb=None, back_cb=None):
        got.update(back=back_cb, crumb=breadcrumb)
        return True
    monkeypatch.setattr(smm, "send_smm_product_page", fake_page)
    await smm.smmu_pick_service_breadcrumb(CB(data=f"smmu:svcb:{sid}:telegram:members:77", message=Msg()), None, St())
    assert got["back"] == "smm:qlu:77:typ:telegram:members" and got["crumb"]
    await smm.smmu_pick_service_breadcrumb(CB(data=f"smmu:svcb:{sid}:telegram:members", message=Msg()), None, St())   # دکمهٔ قدیمی
    assert got["back"] is None


@pytest.mark.asyncio
async def test_bulk_toggle_hides_button_command_and_blocks_flow(db, monkeypatch):
    bid, sid = await _shop(db, monkeypatch)
    monkeypatch.setattr(smm, "CallbackQuery", CB)
    m = Msg()
    await smm.send_smm_product_page(CB(message=m), None, sid, St())
    assert f"smmu:bulk:{sid}" in cbs(m.shown[-1][1]) and f"/multi_{sid}" in m.shown[-1][0]            # پیش‌فرض روشن
    await smm._set_setting(bid, "smm_bulk_enabled", "0")
    tenant._SETTING_CACHE.clear()
    m = Msg()
    await smm.send_smm_product_page(CB(message=m), None, sid, St())
    assert f"smmu:bulk:{sid}" not in cbs(m.shown[-1][1]) and "/multi_" not in m.shown[-1][0]
    async with db() as s:
        v = await s.get(models.SmmService, sid)
    t, st = Msg(), St()
    await smm._bulk_start_prompt(t, st, v)                                                            # دستورِ /multi_ هم بسته است
    assert "غیرفعال" in t.shown[-1][0] and st.state is None
    await smm._set_setting(bid, "smm_bulk_enabled", "1")
    tenant._SETTING_CACHE.clear()
    t2, st2 = Msg(), St()
    await smm._bulk_start_prompt(t2, st2, v)
    assert st2.state is not None and "ثبتِ سفارشِ انبوه" in t2.shown[-1][0]


@pytest.mark.asyncio
async def test_admin_menu_has_bulk_toggle(db, monkeypatch):
    bid, _ = await _shop(db, monkeypatch)
    text, kb = await smm.smm_admin_content(bid)
    assert "smm:bulktoggle" in cbs(kb)


@pytest.mark.asyncio
async def test_ai_prompt_and_thinking_messages_are_deleted(db, monkeypatch):
    bid, _ = await _shop(db, monkeypatch)
    deleted = []

    class Bot:
        async def delete_message(self, chat_id, message_id):
            deleted.append(message_id)
    settings = {"smm_ai_enabled": "1", "smm_ai_api_key": "K"}

    async def get_setting(bot_id, key, default=""):
        return settings.get(key, default)
    monkeypatch.setattr(smm_ai, "_get_setting", get_setting)

    async def noctx(*a, **k):
        return ""

    async def fake_ask(*a, **k):
        return "جواب تستی"
    for fn in ("_order_lookup_context", "_recent_orders_context", "_user_facts_context", "_live_bot_context"):
        monkeypatch.setattr(smm_ai, fn, noctx)
    monkeypatch.setattr(smm_ai, "ask_ai", fake_ask)

    async def prov(b):
        return "openai"
    monkeypatch.setattr(smm_ai, "_get_provider", prov)
    st, bot = St(), Bot()
    prompt_target = Msg()
    await smm_ai.begin_ask(prompt_target, bot, st, cancel_cb="mbtn:home")
    pid = st.d["ai_prompt"][1]
    assert st.d["ai_cancel"] == "mbtn:home"
    q = Msg()
    q.text, q.from_user = "سفارشم کو؟", NS(id=5)
    await smm_ai.smmai_ask_answer(q, bot, st)
    assert pid in deleted and len(deleted) == 2                                     # راهنما + «در حالِ فکر کردن»
    assert "smmai:askb" in cbs(q.shown[-1][1]) and "mbtn:home" in cbs(q.shown[-1][1])   # از دکمه ⇒ بازگشت به منویِ اصلی


def test_ai_assistant_button_action_registered_for_virtual_service_only():
    a = tenant._SIMPLE_ACTIONS["ai_assistant"]
    assert a[2] == frozenset({"virtual_service"}) and "دستیار" in a[0]
    import inspect
    assert 'action == "ai_assistant"' in inspect.getsource(tenant._run_button_action)
