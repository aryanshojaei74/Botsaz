"""PATCH_ADMIN_PANEL_ADD_ONLY_V1 — ادمینِ «فقط اتصالِ پنلِ جدید» و اعمالِ دسترسیِ بخش‌ها."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_admin


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    monkeypatch.setattr(vpn_admin, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE", "_STAFF_PERMS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.AdminSupport(bot_id=7, telegram_id=50, permissions=json.dumps(["panel_add"])))
        s.add(models.AdminSupport(bot_id=7, telegram_id=60, permissions=""))
        s.add(models.AdminSupport(bot_id=7, telegram_id=70, permissions=json.dumps(["stats"])))
        s.add(models.VpnPanel(bot_id=7, panel_key="sanai", base_url="https://secret.example",
                              username="u", password="p", enabled=True))
        await s.commit()
    row = SimpleNamespace(id=7, owner_id=1, field="vpn_sales", plan="pro")

    async def get_bot_row(_bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    yield maker
    await engine.dispose()


from aiogram.types import CallbackQuery


class _CbModel(CallbackQuery):
    async def answer(self, *a, **k):
        return None


def _Cb(data, from_user, message=None):
    return _CbModel.model_construct(id="1", chat_instance="c", data=data,
                                    from_user=from_user, message=message)


class _Msg(SimpleNamespace):
    async def answer(self, text, **k):
        self.sent = getattr(self, "sent", []) + [(text, k.get("reply_markup"))]
        return self


class _State:
    def __init__(self, state=None, data=None):
        self._s, self._d = state, dict(data or {})

    async def get_state(self):
        return self._s

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def clear(self):
        self._s, self._d = None, {}


async def _admin_seen(event, state=None):
    """هندلری که مثلِ هندلرهایِ واقعی is_owner_or_admin را صدا می‌زند."""
    seen = []

    async def handler(ev, data):
        seen.append(await tenant.is_owner_or_admin(7, ev.from_user.id, 1))

    res = await tenant._addonly_mw(handler, event, {"bot": object(), "state": state or _State()})
    return seen, res


@pytest.mark.asyncio
async def test_identity_and_access(db):
    assert await tenant.is_owner_or_admin(7, 1, 1)
    assert await tenant.is_owner_or_admin(7, 60, 1)          # ادمینِ کامل
    assert not await tenant.is_owner_or_admin(7, 50, 1)      # add-only بیرون از مراحلِ مجاز
    assert await tenant.is_staff_member(7, 50, 1)            # ولی دکمهٔ پنل را می‌بیند
    tok = tenant._ADDONLY_ALLOW.set((7, 50))
    try:
        assert await tenant.is_owner_or_admin(7, 50, 1)
    finally:
        tenant._ADDONLY_ALLOW.reset(tok)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["tvpn:plans:sanai", "tvpn:panel:sanai", "tvpn:menu",
                                  "tadm:sec:products", "tvpn:test:sanai", "tvpn:planedit:1"])
async def test_addonly_has_no_admin_power_elsewhere(db, data):
    cb = _Cb(data=data, from_user=SimpleNamespace(id=50), message=_Msg())
    seen, _ = await _admin_seen(cb)
    assert seen == [False]


@pytest.mark.asyncio
async def test_addonly_can_start_new_panel_but_not_overwrite(db):
    st = _State()
    cb = _Cb(data="tvpn:setup:marzban", from_user=SimpleNamespace(id=50), message=_Msg())
    seen, _ = await _admin_seen(cb, st)
    assert seen == [True]
    assert (await st.get_data())["pa_addonly_key"] == "marzban"

    cb2 = _Cb(data="tvpn:setup:sanai", from_user=SimpleNamespace(id=50), message=_Msg())
    seen2, _ = await _admin_seen(cb2)
    assert seen2 == []                                       # پنلِ موجود: اصلاً به هندلر نمی‌رسد


@pytest.mark.asyncio
async def test_setup_steps_allowed_only_for_its_own_key(db):
    st = _State("VpnPanelFSM:base_url", {"panel_key": "marzban", "pa_addonly_key": "marzban"})
    msg = _Msg(text="https://new.example", from_user=SimpleNamespace(id=50))
    seen, _ = await _admin_seen(msg, st)
    assert seen == [True]
    st2 = _State("VpnPanelFSM:base_url", {"panel_key": "sanai"})   # بدونِ علامتِ add-only
    seen2, _ = await _admin_seen(_Msg(text="x", from_user=SimpleNamespace(id=50)), st2)
    assert seen2 == [False]


@pytest.mark.asyncio
async def test_admin_button_shows_addonly_menu_without_existing_panels(db, monkeypatch):
    async def label(bot_id, key):
        return "سرور آلمان"
    monkeypatch.setattr(vpn_admin, "panel_label", label)
    msg = _Msg(text=tenant.BTN_TADMIN, from_user=SimpleNamespace(id=50))
    seen, _ = await _admin_seen(msg)
    assert seen == []
    text, kb = msg.sent[0]
    blob = text + json.dumps([[b.callback_data + b.text for b in r] for r in kb.inline_keyboard],
                             ensure_ascii=False)
    assert "secret.example" not in blob and "سرور آلمان" not in blob
    assert "tvpn:setup:marzban" in blob
    assert "tvpn:setup:sanai#2" in blob                      # پلنِ حرفه‌ای: نمونهٔ جدید، نه رونویسی


@pytest.mark.asyncio
async def test_inner_mw_drops_power_for_non_setup_handlers(db):
    seen = []

    async def handler(ev, data):
        seen.append(await tenant.is_owner_or_admin(7, 50, 1))

    def h(fn_name, module="tenant.bots.vpn.vpn_admin"):
        f = lambda: None  # noqa: E731
        f.__name__, f.__module__ = fn_name, module
        return SimpleNamespace(callback=f)

    tok = tenant._ADDONLY_ALLOW.set((7, 50))
    try:
        ev = _Msg(text="x", from_user=SimpleNamespace(id=50))
        await tenant._staff_section_mw(handler, ev, {"bot": object(), "handler": h("vpn_setup_base")})
        await tenant._staff_section_mw(handler, ev, {"bot": object(), "handler": h("tenant_buttons", "tenant")})
    finally:
        tenant._ADDONLY_ALLOW.reset(tok)
    assert seen == [True, False]


@pytest.mark.asyncio
async def test_section_guard_for_restricted_admin(db):
    called = []

    async def handler(ev, data):
        called.append(ev.from_user.id)

    f = lambda: None  # noqa: E731
    f.__name__, f.__module__ = "vpn_plan_edit", "tenant.bots.vpn.vpn_admin"
    data = {"bot": object(), "handler": SimpleNamespace(callback=f)}
    for uid in (70, 60, 1, 999):
        await tenant._staff_section_mw(handler, _Cb(data="tvpn:plans:sanai", from_user=SimpleNamespace(id=uid)), data)
    # 70 فقط «آمار» دارد → رد؛ ادمینِ کامل، مالک و کاربرِ عادی (که هندلر خودش ردش می‌کند) دست‌نخورده
    assert called == [60, 1, 999]


@pytest.mark.asyncio
async def test_notify_admins_skips_addonly(db):
    sent = []

    class _B:
        async def send_message(self, tid, *a, **k):
            sent.append(tid)
    await tenant._notify_admins(_B(), SimpleNamespace(id=7, owner_id=1), "سفارش")
    assert sorted(sent) == [1, 60, 70]
