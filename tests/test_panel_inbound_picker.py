"""PATCH_PANEL_INBOUND_PICKER_V1 — تیکت MidnightSoul:
۱) انتخاب دستی اینباند (مثلا فقط ترکیه) نباید با بروزرسانی خودکار/دستی دوباره به «همه» برگردد؛
۲) موقع اتصال پنل، لیست اینباندها از خود پنل گرفته شود و تیک‌دار انتخاب شود."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.shared.panel_clients.panel_autosync as PA
from tenant.bots.vpn import vpn_admin as VA
from tenant.shared.panel_clients import panel_inbounds as PI

INB = [
    {"id": 1, "remark": "Germany", "protocol": "vless", "port": 443, "enable": True},
    {"id": 2, "remark": "Netherlands", "protocol": "vmess", "port": 8443, "enable": True},
    {"id": 3, "remark": "Turkey", "protocol": "vless", "port": 2053, "enable": True},
    {"id": 4, "remark": "France", "protocol": "trojan", "port": 2083, "enable": True},
    {"id": 5, "remark": "OldDisabled", "protocol": "vless", "port": 1, "enable": False},
]


# ───────────────────────── منطق خالص ─────────────────────────
def test_items_and_labels_numeric_and_tag_panels():
    items = PI.inbound_items("sanai", INB)
    assert [i["id"] for i in items] == ["1", "2", "3", "4", "5"]
    assert items[2]["label"] == "#3 Turkey · vless:2053" and items[4]["enabled"] is False
    assert PI.active_ids("sanai", INB) == ["1", "2", "3", "4"]
    tag = PI.inbound_items("marzban", [{"id": "VLESS TCP", "tag": "VLESS TCP", "protocol": "vless", "enable": True},
                                       {"id": "VLESS TCP", "tag": "dup"}, {"id": "", "tag": "x"}, "junk"])
    assert [i["id"] for i in tag] == ["VLESS TCP"] and tag[0]["label"] == "VLESS TCP · vless"
    group = PI.inbound_items("pasargad", [{"id": 7, "tag": "vip", "remark": "vip", "protocol": "group"}])
    assert group[0]["label"] == "#7 vip"                       # پروتکل «group» نشان داده نمی‌شود
    assert PI.active_ids("sanai", [{"id": 3}, {"id": "5"}, {"id": "weird"}]) == ["3", "5", "weird"]


def test_custom_choice_survives_sync_the_ticket_scenario():
    # مالک فقط ترکیه (#3) را خواسته؛ پنل ۴ اینباند فعال دارد
    plan = PI.plan_inbound_sync("custom", "3", ["1", "2", "3", "4"])
    assert (plan.ids, plan.mode, plan.changed) == ("3", "custom", False)
    assert plan.fresh == ("1", "2", "4") and plan.missing == ()
    # هر چند بار هم که اجرا شود همان می‌ماند
    again = PI.plan_inbound_sync(plan.mode, plan.ids, ["1", "2", "3", "4"])
    assert again.ids == "3" and not again.changed


def test_all_mode_follows_the_panel():
    plan = PI.plan_inbound_sync("all", "1,2,3", ["1", "3", "4", "9"])
    assert (plan.ids, plan.mode, plan.changed) == ("1,3,4,9", "all", True)
    same = PI.plan_inbound_sync("all", "1,3", ["1", "3"])
    assert (same.ids, same.changed) == ("1,3", False)


def test_legacy_rows_are_guessed_conservatively():
    # لیست خالی یا شامل همهٔ فعال‌ها = «همه» (رفتار قبلی؛ حالت ثبت می‌شود)
    p = PI.plan_inbound_sync(None, "", ["1", "2"])
    assert (p.ids, p.mode, p.changed) == ("1,2", "all", True)
    p = PI.plan_inbound_sync(None, "1,2", ["1", "2"])
    assert (p.ids, p.mode, p.changed) == ("1,2", "all", True)          # فقط mode ثبت می‌شود
    p = PI.plan_inbound_sync(None, "1,2,9", ["1", "2"])
    assert (p.ids, p.mode) == ("1,2", "all")                          # شناسهٔ مرده حذف می‌شود
    # زیرمجموعهٔ واقعی = انتخاب دستی؛ دست نمی‌خورد
    p = PI.plan_inbound_sync(None, "3", ["1", "2", "3", "4"])
    assert (p.ids, p.mode, p.changed) == ("3", "custom", True) and p.fresh == ("1", "2", "4")


def test_sync_edge_cases():
    keep = PI.plan_inbound_sync("custom", "3", [])                    # پنل چیزی برنگرداند
    assert (keep.ids, keep.mode, keep.changed) == ("3", "custom", False)
    assert PI.plan_inbound_sync("custom", "", ["1"]).mode == "all"     # انتخاب خالی معنی ندارد
    gone = PI.plan_inbound_sync("custom", "3,7", ["1", "3"])
    assert gone.ids == "3,7" and gone.missing == ("7",)                # فقط هشدار، حذف نه
    assert PI.mode_for_choice(["1", "2"], ["1", "2"]) == "all"
    assert PI.mode_for_choice(["1"], ["1", "2"]) == "custom"
    assert PI.mode_for_choice([], []) == "custom"


def test_describe_and_picker_state():
    assert PI.describe(None, "") == "همهٔ فعال‌ها"
    assert "ثابت" in PI.describe("custom", "3") and "هماهنگ" in PI.describe("all", "1,2")
    assert PI.describe(None, "1,2") == "1,2"
    # پنل تازه: چیزی تیک‌خورده نیست و غیرفعال‌ها پنهان‌اند
    items, sel, hidden = PI.picker_state("sanai", INB)
    assert [i["id"] for i in items] == ["1", "2", "3", "4"] and sel == [] and hidden == 1
    # فقط یک اینباند فعال: از قبل تیک‌خورده تا مالک فقط «تایید» را بزند
    assert PI.picker_state("sanai", INB[:1])[1] == [0]
    # پنل موجود، انتخاب دستی + یک مورد گم‌شده → تیک‌خورده‌ها و هشدار
    items, sel, hidden = PI.picker_state("sanai", INB, "custom", "3,8", existing=True)
    assert sel == [2, 4] and items[4]["id"] == "8" and "⚠️" in items[4]["label"]
    # حالت «همه» → همهٔ فعال‌ها، بدون موارد گم‌شده
    items, sel, _ = PI.picker_state("sanai", INB, "all", "1,2,8", existing=True)
    assert sel == [0, 1, 2, 3] and len(items) == 4
    # لیست خالی روی پنل موجود = همه
    assert PI.picker_state("sanai", INB, None, "", existing=True)[1] == [0, 1, 2, 3]


# ───────────────────────── زیرساخت تست ─────────────────────────
@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PA, tenant, VA):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_STAFF_PERMS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


class FakeClient:
    def __init__(self, inbounds=None, fail=None):
        self.inbounds, self.fail, self.timeout = inbounds if inbounds is not None else INB, fail, 30
        self.closed = False

    async def login(self):
        if self.fail:
            raise self.fail

    async def list_inbounds(self):
        return self.inbounds

    async def close(self):
        self.closed = True

    async def get_client(self, email):
        return None

    def sub_link(self, sub_id):
        return ""


async def _add_panel(maker, key="sanai", **kw):
    async with maker() as s:
        p = models.VpnPanel(bot_id=1, panel_key=key, base_url="https://p.example:54321", username="u", password="p",
                            enabled=True, **kw)
        s.add(p)
        await s.commit()
        return p.id


async def _panel(maker, key="sanai"):
    async with maker() as s:
        return await s.scalar(select(models.VpnPanel).where(models.VpnPanel.bot_id == 1, models.VpnPanel.panel_key == key))


# ───────────────────────── بروزرسانی خودکار (ریشهٔ شکایت) ─────────────────────────
@pytest.mark.asyncio
async def test_autosync_keeps_the_owner_selection(db, monkeypatch):
    pid = await _add_panel(db, inbound_ids="3", inbound_mode="custom")
    fc = FakeClient()
    monkeypatch.setattr(PA, "make_panel_client", lambda p: fc)
    for _ in range(3):                                               # «هر ۲ ساعت» چند دور
        await PA.sync_panel_once(pid)
    p = await _panel(db)
    assert (p.inbound_ids, p.inbound_mode) == ("3", "custom")


@pytest.mark.asyncio
async def test_autosync_still_follows_the_panel_in_all_mode(db, monkeypatch):
    pid = await _add_panel(db, inbound_ids="1,2", inbound_mode="all")
    monkeypatch.setattr(PA, "make_panel_client", lambda p: FakeClient())
    await PA.sync_panel_once(pid)
    p = await _panel(db)
    assert (p.inbound_ids, p.inbound_mode) == ("1,2,3,4", "all")


@pytest.mark.asyncio
async def test_autosync_legacy_subset_is_treated_as_a_choice(db, monkeypatch):
    pid = await _add_panel(db, inbound_ids="3")                       # ردیف قدیمی بدون mode
    monkeypatch.setattr(PA, "make_panel_client", lambda p: FakeClient())
    await PA.sync_panel_once(pid)
    p = await _panel(db)
    assert (p.inbound_ids, p.inbound_mode) == ("3", "custom")


@pytest.mark.asyncio
async def test_autosync_warns_when_a_chosen_inbound_disappears(db, monkeypatch, caplog):
    pid = await _add_panel(db, inbound_ids="3,9", inbound_mode="custom")
    monkeypatch.setattr(PA, "make_panel_client", lambda p: FakeClient())
    import logging
    with caplog.at_level(logging.WARNING):
        await PA.sync_panel_once(pid)
    assert (await _panel(db)).inbound_ids == "3,9"                    # حذف نشد
    assert any("9" in r.getMessage() and "دیگر در پنل نیستند" in r.getMessage() for r in caplog.records)


# ───────────────────────── fake aiogram ─────────────────────────
class FakeMsg:
    def __init__(self, log, text="", uid=1):
        self.log, self.text = log, text
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")

    async def answer(self, text, reply_markup=None, **kw):
        m = FakeMsg(self.log)
        self.log.append(("answer", text, reply_markup))
        return m

    async def edit_text(self, text, reply_markup=None, **kw):
        self.log.append(("edit", text, reply_markup))
        return self


class FakeCb:
    def __init__(self, data, msg, uid=1):
        self.data, self.message, self.from_user = data, msg, SimpleNamespace(id=uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


@pytest.fixture
def env(monkeypatch, db):
    row = SimpleNamespace(id=1, owner_id=1, plan="pro", field="vpn_sales")

    async def owner(cb, bot):
        return row

    async def owner_msg(msg, bot):
        return row

    async def label(bot_id, key):
        return "پنل تست"
    monkeypatch.setattr(VA, "_owner", owner)
    monkeypatch.setattr(VA, "_owner_msg", owner_msg)
    monkeypatch.setattr(VA, "panel_label", label)

    async def no_tcp_error(url, timeout=8):
        return None
    monkeypatch.setattr(VA, "_tcp_precheck", no_tcp_error)
    log = []
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(row=row, log=log, state=state, bot=object(),
                           msg=FakeMsg(log), maker=db)


def _kb_data(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


def _kb_text(markup):
    return [b.text for r in markup.inline_keyboard for b in r]


def _last_markup(log):
    for kind, text, kb in reversed(log):
        if kb is not None and hasattr(kb, "inline_keyboard"):
            return text, kb
    raise AssertionError("no inline keyboard was sent")


async def _begin_setup(env, key="sanai", probe=None, sub="-"):
    """مراحل ۱ تا ۴ را با state واقعی می‌گذراند و به ورود sub_base می‌رسد."""
    await env.state.update_data(panel_key=key, base_url="https://p.example:54321", username="u", password="p")
    await env.state.set_state(VA.VpnPanelFSM.sub_base)
    VA._make_probe = lambda k, d: probe or FakeClient()
    await VA.vpn_setup_sub(FakeMsg(env.log, text=sub), env.bot, env.state)


@pytest.fixture(autouse=True)
def _restore_probe():
    orig = VA._make_probe
    yield
    VA._make_probe = orig


# ───────────────────────── ثبت پنل: صفحهٔ تیک‌دار ─────────────────────────
@pytest.mark.asyncio
async def test_setup_shows_the_panel_inbounds_to_tick_and_saves_the_choice(env):
    await _begin_setup(env)
    text, kb = _last_markup(env.log)
    assert "انتخاب اینباندها" in text and "انتخاب‌شده: <b>0</b> از 4" in text
    assert "1 مورد غیرفعال" in text
    data = _kb_data(kb)
    assert [d for d in data if d.startswith("tvpn:ibt:")] == [f"tvpn:ibt:{i}" for i in range(4)]
    assert "tvpn:ibok" in data and "tvpn:ibman" in data and "tvpn:iball" in data
    assert any("Turkey" in t and "vless:2053" in t for t in _kb_text(kb))
    assert (await env.state.get_state()) == "VpnPanelFSM:inbound_ids"

    # فقط ترکیه را تیک می‌زند
    cb = FakeCb("tvpn:ibt:2", env.msg)
    await VA.vpn_ib_toggle(cb, env.bot, env.state)
    text, kb = _last_markup(env.log)
    assert "انتخاب‌شده: <b>1</b> از 4" in text and "✅ #3 Turkey" in " ".join(_kb_text(kb))
    ok = FakeCb("tvpn:ibok", env.msg)
    await VA.vpn_ib_confirm(ok, env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode, p.enabled) == ("3", "custom", False)
    assert p.base_url == "https://p.example:54321" and p.username == "u"
    final = env.log[-1][1]
    assert "ذخیره شد" in final and "انتخاب دستی" in final
    assert (await env.state.get_state()) is None                      # جریان تمام شد


@pytest.mark.asyncio
async def test_setup_ticking_everything_means_follow_the_panel(env):
    await _begin_setup(env)
    await VA.vpn_ib_all_none(FakeCb("tvpn:iball", env.msg), env.bot, env.state)
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("1,2,3,4", "all")


@pytest.mark.asyncio
async def test_setup_needs_at_least_one_tick_and_toggle_is_reversible(env):
    await _begin_setup(env)
    cb = FakeCb("tvpn:ibok", env.msg)
    await VA.vpn_ib_confirm(cb, env.bot, env.state)
    assert cb.answers[-1][1] is True and "حداقل" in cb.answers[-1][0]
    assert await _panel(env.maker) is None
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:1", env.msg), env.bot, env.state)
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:1", env.msg), env.bot, env.state)
    assert (await env.state.get_data())["ibp"]["sel"] == []
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:99", env.msg), env.bot, env.state)     # خارج از محدوده: بی‌اثر
    assert (await env.state.get_data())["ibp"]["sel"] == []


@pytest.mark.asyncio
async def test_setup_falls_back_to_typing_when_the_panel_list_fails(env):
    from tenant.shared.panel_clients.vpn_panel import PanelError
    fc = FakeClient(fail=PanelError("HTTP 401: <b>bad</b>"))
    await _begin_setup(env, probe=fc)
    texts = [t for _k, t, _kb in env.log]
    assert any("نتوانستم خودکار بگیرم" in t and "&lt;b&gt;bad&lt;/b&gt;" in t for t in texts)   # HTML امن شد
    assert any("۵/۵" in t for t in texts)                                # مرحلهٔ تایپی قبلی
    assert fc.closed
    assert (await env.state.get_state()) == "VpnPanelFSM:inbound_ids"
    # تایپ دستی هنوز کار می‌کند و «انتخاب دستی» ثبت می‌شود
    await VA.vpn_setup_inbounds(FakeMsg(env.log, text="3"), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("3", "custom")


@pytest.mark.asyncio
async def test_setup_tcp_precheck_failure_also_falls_back(env, monkeypatch):
    async def blocked(url, timeout=8):
        return "سرور ربات اصلا نمی‌تواند به آدرس/پورت این پنل وصل شود"
    monkeypatch.setattr(VA, "_tcp_precheck", blocked)
    await _begin_setup(env)
    texts = [t for _k, t, _kb in env.log]
    assert any("اصلا نمی‌تواند به آدرس" in t for t in texts) and any("۵/۵" in t for t in texts)


@pytest.mark.asyncio
async def test_setup_when_no_inbound_is_active_falls_back(env):
    await _begin_setup(env, probe=FakeClient(inbounds=[{"id": 1, "remark": "x", "enable": False}]))
    texts = [t for _k, t, _kb in env.log]
    assert any("هیچ اینباند فعالی روی پنل پیدا نشد" in t for t in texts)
    assert any("۵/۵" in t for t in texts)


@pytest.mark.asyncio
async def test_typed_all_marks_follow_mode(env, monkeypatch):
    import asyncio

    class W:
        def close(self):
            pass

    async def fake_open(host, port):
        return object(), W()
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    from tenant.shared.panel_clients import vpn_panel_factory as F
    monkeypatch.setattr(F, "make_panel_client_from_fields", lambda *a, **k: FakeClient())
    await env.state.update_data(panel_key="sanai", base_url="https://p.example:54321", username="u", password="p",
                                sub_base="")
    await env.state.set_state(VA.VpnPanelFSM.inbound_ids)
    await VA.vpn_setup_inbounds(FakeMsg(env.log, text="همه"), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("1,2,3,4", "all")


@pytest.mark.asyncio
async def test_manual_button_switches_to_typed_prompt(env):
    await _begin_setup(env)
    cb = FakeCb("tvpn:ibman", env.msg)
    await VA.vpn_ib_manual(cb, env.bot, env.state)
    assert "۵/۵" in env.log[-1][1]
    assert (await env.state.get_data()).get("ibp") is None
    assert (await env.state.get_state()) == "VpnPanelFSM:inbound_ids"


@pytest.mark.asyncio
async def test_pagination_maps_buttons_to_the_right_items(env):
    many = [{"id": i, "remark": f"srv{i}", "protocol": "vless", "port": 1000 + i, "enable": True} for i in range(1, 46)]
    await _begin_setup(env, probe=FakeClient(inbounds=many))
    text, kb = _last_markup(env.log)
    assert "صفحه 1 از 3" in text
    assert sum(d.startswith("tvpn:ibt:") for d in _kb_data(kb)) == 20 and "tvpn:ibpg:1" in _kb_data(kb)
    await VA.vpn_ib_page(FakeCb("tvpn:ibpg:2", env.msg), env.bot, env.state)
    text, kb = _last_markup(env.log)
    assert "صفحه 3 از 3" in text and "tvpn:ibt:40" in _kb_data(kb) and "tvpn:ibt:44" in _kb_data(kb)
    assert "tvpn:ibpg:1" in _kb_data(kb) and "tvpn:ibpg:3" not in _kb_data(kb)
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:44", env.msg), env.bot, env.state)
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("45", "custom")           # آخرین آیتم (id=45)
    for d in _kb_data(kb):
        assert len(d.encode()) <= 64


@pytest.mark.asyncio
async def test_pasargad_uses_group_wording_and_hiddify_has_no_picker(env):
    await _begin_setup(env, key="pasargad",
                       probe=FakeClient(inbounds=[{"id": 1, "tag": "vip", "remark": "vip", "protocol": "group", "enable": True}]))
    text, _kb = _last_markup(env.log)
    assert "انتخاب گروه‌ها" in text
    env.log.clear()
    await env.state.clear()
    await _begin_setup(env, key="hiddify")
    assert not any(kb is not None and hasattr(kb, "inline_keyboard") and "tvpn:ibok" in _kb_data(kb)
                   for _k, _t, kb in env.log)
    assert any("۵/۵" in t for _k, t, _kb in env.log)


@pytest.mark.asyncio
async def test_pasargad_without_groups_explains_how_to_create_one(env):
    await _begin_setup(env, key="pasargad", probe=FakeClient(inbounds=[]))
    texts = [t for _k, t, _kb in env.log]
    assert any("هیچ گروه فعالی روی پنل پیدا نشد" in t and "Create Group" in t for t in texts)
    assert any("۵/۵" in t for t in texts)


@pytest.mark.asyncio
async def test_resetup_of_an_existing_panel_preselects_the_current_choice(env):
    await _add_panel(env.maker, inbound_ids="3", inbound_mode="custom")
    await _begin_setup(env)
    text, kb = _last_markup(env.log)
    assert "انتخاب‌شده: <b>1</b> از 4" in text and any(t.startswith("✅ #3") for t in _kb_text(kb))
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("3", "custom")


@pytest.mark.asyncio
async def test_expired_picker_state_is_reported_not_crashed(env):
    for fn, data in ((VA.vpn_ib_toggle, "tvpn:ibt:0"), (VA.vpn_ib_confirm, "tvpn:ibok"),
                     (VA.vpn_ib_all_none, "tvpn:iball"), (VA.vpn_ib_page, "tvpn:ibpg:1"), (VA.vpn_ib_manual, "tvpn:ibman")):
        cb = FakeCb(data, env.msg)
        await fn(cb, env.bot, env.state)
        assert cb.answers and cb.answers[-1][1] is True and "منقضی" in cb.answers[-1][0]


@pytest.mark.asyncio
async def test_setup_confirm_with_lost_form_state_does_not_write(env):
    await _begin_setup(env)
    data = await env.state.get_data()
    await env.state.set_data({"ibp": data["ibp"]})                       # ری‌استارت: مراحل قبلی گم شد
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:0", env.msg), env.bot, env.state)
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    assert await _panel(env.maker) is None
    assert "ناقص/منقضی" in env.log[-1][1]


# ───────────────────────── ویرایش بعد از ثبت ─────────────────────────
@pytest.mark.asyncio
async def test_edit_flow_changes_selection_from_the_panel_menu(env, monkeypatch):
    await _add_panel(env.maker, inbound_ids="1,2,3,4", inbound_mode="all")
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient())
    cb = FakeCb("tvpn:ibpick:sanai", env.msg)
    await VA.vpn_ib_open(cb, env.bot, env.state)
    text, kb = _last_markup(env.log)
    assert "انتخاب‌شده: <b>4</b> از 4" in text and "tvpn:ibman" not in _kb_data(kb)    # حالت «همه»: همه تیک‌خورده
    assert "tvpn:ibcancel" in _kb_data(kb)
    # فقط ترکیه می‌ماند
    await VA.vpn_ib_all_none(FakeCb("tvpn:ibnone", env.msg), env.bot, env.state)
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:2", env.msg), env.bot, env.state)
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("3", "custom")
    last = env.log[-1][1]
    assert "ذخیره شد" in last and "انتخاب دستی" in last and "آدرس پنل" in last        # منوی پنل هم نمایش داده شد
    assert (await env.state.get_data()).get("ibp") is None

    # و بروزرسانی خودکار بعدی آن را عوض نمی‌کند
    monkeypatch.setattr(PA, "make_panel_client", lambda p: FakeClient())
    await PA.sync_panel_once(p.id)
    assert (await _panel(env.maker)).inbound_ids == "3"


@pytest.mark.asyncio
async def test_edit_flow_shows_stale_choice_and_lets_owner_drop_it(env, monkeypatch):
    await _add_panel(env.maker, inbound_ids="3,9", inbound_mode="custom")
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient())
    await VA.vpn_ib_open(FakeCb("tvpn:ibpick:sanai", env.msg), env.bot, env.state)
    text, kb = _last_markup(env.log)
    assert "انتخاب‌شده: <b>2</b> از 5" in text
    assert any("⚠️ 9" in t and t.startswith("✅") for t in _kb_text(kb))
    await VA.vpn_ib_toggle(FakeCb("tvpn:ibt:4", env.msg), env.bot, env.state)       # تیک مورد گم‌شده را برمی‌دارد
    await VA.vpn_ib_confirm(FakeCb("tvpn:ibok", env.msg), env.bot, env.state)
    assert (await _panel(env.maker)).inbound_ids == "3"


@pytest.mark.asyncio
async def test_edit_flow_errors_and_cancel(env, monkeypatch):
    from tenant.shared.panel_clients.vpn_panel import PanelError
    await _add_panel(env.maker, inbound_ids="3", inbound_mode="custom")
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient(fail=PanelError("down")))
    cb = FakeCb("tvpn:ibpick:sanai", env.msg)
    await VA.vpn_ib_open(cb, env.bot, env.state)
    assert "نتوانستم بگیرم" in env.log[-1][1] and "down" in env.log[-1][1]
    assert (await _panel(env.maker)).inbound_ids == "3"
    # پنل بدون لیست / نامعتبر / ثبت‌نشده
    for data, needle in (("tvpn:ibpick:hiddify", "لیست اینباند ندارد"), ("tvpn:ibpick:zzz", "لیست اینباند ندارد"),
                         ("tvpn:ibpick:marzban", "اول تنظیمات")):
        c = FakeCb(data, env.msg)
        await VA.vpn_ib_open(c, env.bot, env.state)
        assert needle in c.answers[-1][0] and c.answers[-1][1] is True
    # لغو: چیزی نوشته نمی‌شود و منوی پنل برمی‌گردد
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient())
    await VA.vpn_ib_open(FakeCb("tvpn:ibpick:sanai", env.msg), env.bot, env.state)
    await VA.vpn_ib_all_none(FakeCb("tvpn:iball", env.msg), env.bot, env.state)
    await VA.vpn_ib_cancel(FakeCb("tvpn:ibcancel", env.msg), env.bot, env.state)
    assert (await _panel(env.maker)).inbound_ids == "3" and "آدرس پنل" in env.log[-1][1]
    assert (await env.state.get_data()).get("ibp") is None


# ───────────────────────── بروزرسانی دستی ─────────────────────────
@pytest.mark.asyncio
async def test_manual_refresh_keeps_the_choice_and_says_so(env, monkeypatch):
    await _add_panel(env.maker, inbound_ids="3", inbound_mode="custom")
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient())
    cb = FakeCb("tvpn:refresh_subs:sanai", env.msg)
    await VA.vpn_refresh_subs(cb, env.bot)
    assert (await _panel(env.maker)).inbound_ids == "3"
    blob = "\n".join(t for _k, t, _kb in env.log)
    assert "ثابت ماند" in blob and "<code>3</code>" in blob and "1,2,4" in blob and "انتخاب اینباندها" in blob


@pytest.mark.asyncio
async def test_manual_refresh_updates_list_in_all_mode_and_skips_eylan(env, monkeypatch):
    await _add_panel(env.maker, inbound_ids="1,2", inbound_mode="all")
    monkeypatch.setattr(VA, "make_panel_client", lambda panel: FakeClient())
    await VA.vpn_refresh_subs(FakeCb("tvpn:refresh_subs:sanai", env.msg), env.bot)
    p = await _panel(env.maker)
    assert (p.inbound_ids, p.inbound_mode) == ("1,2,3,4", "all")
    assert "اینباندها بروز شدند" in "\n".join(t for _k, t, _kb in env.log)

    await _add_panel(env.maker, key="eylan", inbound_ids="openvpn,wg1")
    env.log.clear()
    monkeypatch.setattr(VA, "make_panel_client",
                        lambda panel: FakeClient(inbounds=[{"id": "openvpn", "remark": "OpenVPN", "enable": True}]))
    await VA.vpn_refresh_subs(FakeCb("tvpn:refresh_subs:eylan", env.msg), env.bot)
    assert (await _panel(env.maker, "eylan")).inbound_ids == "openvpn,wg1"          # دست نخورد و کرش هم نکرد
    assert "ایلان" in "\n".join(t for _k, t, _kb in env.log)


# ───────────────────────── منوی پنل و بکاپ ─────────────────────────
@pytest.mark.asyncio
async def test_panel_menu_has_picker_button_only_for_pickable_panels(env):
    await _add_panel(env.maker, inbound_ids="3", inbound_mode="custom")
    await _add_panel(env.maker, key="hiddify")
    await _add_panel(env.maker, key="marzban", inbound_ids="VLESS TCP", inbound_mode="custom")
    info, kb = await VA._vpn_panel_menu_content(env.row, "sanai")
    assert "tvpn:ibpick:sanai" in _kb_data(kb) and "انتخاب دستی" in info and "ثابت" in info
    _info, kb = await VA._vpn_panel_menu_content(env.row, "hiddify")
    assert not [d for d in _kb_data(kb) if d.startswith("tvpn:ibpick:")]
    _info, kb = await VA._vpn_panel_menu_content(env.row, "marzban")
    assert "tvpn:ibpick:marzban" in _kb_data(kb)
    panels = await VA.get_all_panels(1)
    assert "انتخاب دستی" in await VA._panels_info_text(panels)


def test_backup_keeps_the_mode_and_column_is_nullable():
    from tenant.shared import backup_restore as BR
    assert "inbound_mode" in BR._VPN_PANEL_FIELDS
    col = models.VpnPanel.__table__.c.inbound_mode
    assert col.nullable and col.default is None


# ───────────────────────── ادمین «فقط اتصال پنل» ─────────────────────────
from aiogram.types import CallbackQuery  # noqa: E402


class _CbModel(CallbackQuery):
    async def answer(self, *a, **k):
        return None


async def _noop(*a, **k):
    return None


def _AMsg():
    return SimpleNamespace(answer=_noop, edit_text=_noop, chat=SimpleNamespace(type="private"))


def _RealCb(data, uid, message=None):
    return _CbModel.model_construct(id="1", chat_instance="c", data=data, from_user=SimpleNamespace(id=uid),
                                    message=message or _AMsg())


@pytest_asyncio.fixture
async def addonly(db, monkeypatch):
    async with db() as s:
        s.add(models.AdminSupport(bot_id=1, telegram_id=50, permissions=json.dumps(["panel_add"])))
        await s.commit()
    row = SimpleNamespace(id=1, owner_id=1, field="vpn_sales", plan="pro")

    async def get_bot_row(_bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    sent = []

    class B:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))
    return SimpleNamespace(db=db, row=row, sent=sent, bot=B())


async def _addonly_seen(ao, event, state):
    seen = []

    async def handler(ev, data):
        seen.append(await tenant.is_owner_or_admin(1, ev.from_user.id, 1))

    await tenant._addonly_mw(handler, event, {"bot": ao.bot, "state": state})
    return seen


def test_addonly_allowlist_names_are_real_handlers():
    names = {n for n in tenant._ADDONLY_SETUP_HANDLERS if n.startswith("vpn_ib_")}
    assert names == {"vpn_ib_toggle", "vpn_ib_all_none", "vpn_ib_page", "vpn_ib_confirm", "vpn_ib_manual"}
    for n in names:
        assert getattr(VA, n).__module__ == "tenant.bots.vpn.vpn_admin"
    # دکمه‌های ویرایش (باز کردن/لغو) جزو مجازها نیستند
    assert "vpn_ib_open" not in tenant._ADDONLY_SETUP_HANDLERS and "vpn_ib_cancel" not in tenant._ADDONLY_SETUP_HANDLERS


@pytest.mark.asyncio
async def test_addonly_admin_can_use_the_picker_only_inside_their_own_setup(addonly):
    ibp = {"key": "marzban", "mode": "setup", "items": [{"id": "a", "label": "a", "enabled": True}], "sel": [], "page": 0}
    ok_state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=50, user_id=50))
    await ok_state.update_data(panel_key="marzban", pa_addonly_key="marzban", ibp=ibp)
    for data in ("tvpn:ibt:0", "tvpn:iball", "tvpn:ibnone", "tvpn:ibpg:0", "tvpn:ibok", "tvpn:ibman"):
        assert await _addonly_seen(addonly, _RealCb(data, 50), ok_state) == [True], data

    # ویرایش پنل (باز کردن/لغو) یا ibp در حالت edit یا کلید ناهمخوان → بسته
    for data in ("tvpn:ibpick:marzban", "tvpn:ibcancel"):
        assert await _addonly_seen(addonly, _RealCb(data, 50), ok_state) == [], data
    for bad in ({"ibp": dict(ibp, mode="edit")}, {"ibp": dict(ibp, key="sanai")}, {"pa_addonly_key": None}, {"ibp": None}):
        st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=51, user_id=50))
        await st.update_data(panel_key="marzban", pa_addonly_key="marzban", ibp=ibp)
        await st.update_data(**bad)
        res = await _addonly_seen(addonly, _RealCb("tvpn:ibok", 50), st)
        assert res == [], bad


@pytest.mark.asyncio
async def test_addonly_confirm_registers_the_created_panel_and_notifies_the_owner(addonly):
    ibp = {"key": "marzban", "mode": "setup", "items": [{"id": "a", "label": "a", "enabled": True}], "sel": [0], "page": 0}
    st = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=50, user_id=50))
    await st.update_data(panel_key="marzban", pa_addonly_key="marzban", ibp=ibp)
    created = []

    async def handler(ev, data):
        async with addonly.db() as s:                                     # مثل vpn_ib_confirm، پنل ساخته می‌شود
            s.add(models.VpnPanel(bot_id=1, panel_key="marzban", base_url="https://m", username="u", password="p",
                                  inbound_ids="a", inbound_mode="all"))
            await s.commit()
        created.append(True)

    await tenant._addonly_mw(handler, _RealCb("tvpn:ibok", 50),
                             {"bot": addonly.bot, "state": st})
    assert created == [True]
    assert "marzban" in await tenant.get_setting(1, tenant._ADDONLY_KEYS_SETTING % 50, "")
    assert any(uid == 1 and "marzban" in text for uid, text in addonly.sent)

    # دکمه‌های میانی چیزی ثبت/اطلاع نمی‌دهند و اگر پنل در همین فاصله ساخته شده باشد، جریان بسته می‌شود
    st2 = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=52, user_id=50))
    await st2.update_data(panel_key="marzban", pa_addonly_key="marzban", ibp=ibp)
    assert await _addonly_seen(addonly, _RealCb("tvpn:ibt:0", 50), st2) == []
    assert await st2.get_data() == {}
