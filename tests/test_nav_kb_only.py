"""PATCH_NAV_KB_ONLY_V1 / PATCH_WG_BTN_PANEL_FLAG_V1 (ایلیا)."""
import pytest
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import tenant
from tenant.bots.vpn import vpn_admin as VA, vpn_user as VU


def _kb(*rows):
    return M(inline_keyboard=[[B(text=t, callback_data=c) for t, c in r] for r in rows])


def test_strip_inline_nav_removes_back_and_home_but_keeps_real_buttons():
    mk = _kb([("💳 پرداخت", "pay:1")],
             [("⬅️ بازگشت", "vsvcback"), ("🏠 خانه", "vsvc:home")],
             [("🏠 منوی اصلی", "x1")], [("⬅️ بازگشت به منو", "mbtn:home")],
             [("🏠 بازگشت به خانه", "y")], [("⬅️ بازگشت به منوی قبل", "nav:back")],
             [("❌ لغو", "cancel"), ("💸 برگشت وجه", "vrf:1")])
    out = tenant.strip_inline_nav(mk)
    left = [b.text for r in out.inline_keyboard for b in r]
    assert left == ["💳 پرداخت", "❌ لغو", "💸 برگشت وجه"]


def test_strip_inline_nav_all_removed_gives_none_and_non_inline_untouched():
    assert tenant.strip_inline_nav(_kb([("⬅️ بازگشت", "vsvcback")])) is None
    sentinel = object()
    assert tenant.strip_inline_nav(sentinel) is sentinel


@pytest.mark.asyncio
async def test_nav_kb_only_requires_reply_mode_and_flag(monkeypatch):
    store = {"menu_mode": "reply", "nav_kb_only": "1"}

    async def gs(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    assert await tenant.nav_kb_only(1) is True
    store["menu_mode"] = "inline"
    assert await tenant.nav_kb_only(1) is False
    store.update(menu_mode="reply", nav_kb_only="0")
    assert await tenant.nav_kb_only(1) is False


@pytest.mark.asyncio
async def test_submenu_back_row_has_home_button_only_when_enabled(monkeypatch):
    store = {"menu_mode": "reply", "nav_kb_only": "0"}

    async def gs(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    assert [b.text for b in await tenant._back_kb_row(1)] == [tenant.BTN_BACK_MENU]
    store["nav_kb_only"] = "1"
    assert [b.text for b in await tenant._back_kb_row(1)] == [tenant.BTN_BACK_MENU, tenant.BTN_HOME_FLOW]


def test_wireguard_button_flag_is_per_panel_and_default_on():
    assert VU._VC_DEF["wg_cfg_btn"] == "1" and VA._VC_DEFAULTS["wg_cfg_btn"] == "1"
    assert "wg_cfg_btn" in VU._VC_PANEL_KEYS_U and "wg_cfg_btn" in VA._VC_PANEL_KEYS
    import inspect
    src = inspect.getsource(VU)
    seg = src[src.index("PATCH_SVC_PAGE_TIDY_V1"):src.index("PATCH_SVC_PAGE_TIDY_V1") + 2500]
    assert "تغییر نام کانفیگ" not in seg and "نمایش اطلاعات کانفیگ در وب" not in seg
    assert '"wg_cfg_btn"' in seg


@pytest.mark.asyncio
async def test_send_layer_strips_nav_for_customers_not_staff(monkeypatch):
    from types import SimpleNamespace as NS
    from aiogram.methods import SendMessage
    sent = []

    class Sess:
        async def make_request(self, bot, method, timeout=None):
            sent.append(method)
            return NS(message_id=1)
    bot = NS(session=Sess(), token="1:T")

    async def gbr(b):
        return NS(id=7, owner_id=1, plan="pro")

    async def flag(bid):
        return True
    staff = {"v": False}

    async def is_staff(b, uid):
        return staff["v"]
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "nav_kb_only", flag)
    monkeypatch.setattr(tenant, "_nav_is_staff", is_staff)
    tenant._nav_install(bot)
    mk = _kb([("پرداخت", "pay:1")], [("⬅️ بازگشت", "vsvcback"), ("🏠 خانه", "vsvc:home")])
    await bot.session.make_request(bot, SendMessage(chat_id=500, text="x", reply_markup=mk))
    assert [b.text for r in sent[-1].reply_markup.inline_keyboard for b in r] == ["پرداخت"]
    staff["v"] = True                                           # مالک/ادمین: دست‌نخورده
    await bot.session.make_request(bot, SendMessage(chat_id=500, text="x", reply_markup=mk))
    assert len([b for r in sent[-1].reply_markup.inline_keyboard for b in r]) >= 3
