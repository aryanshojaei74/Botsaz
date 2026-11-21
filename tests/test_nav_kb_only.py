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
    staff["v"] = True                                           # مالک/ادمین: صفحهٔ منویِ مشتری هم مثلِ مشتری
    await bot.session.make_request(bot, SendMessage(chat_id=500, text="x", reply_markup=mk))
    assert [b.text for r in sent[-1].reply_markup.inline_keyboard for b in r] == ["پرداخت"]
    adm = _kb([("تنظیمات", "tadm:sec:x")], [("⬅️ بازگشت", "mbtn:home")])      # صفحهٔ مدیریتی: دست‌نخورده
    await bot.session.make_request(bot, SendMessage(chat_id=500, text="x", reply_markup=adm))
    assert len([b for r in sent[-1].reply_markup.inline_keyboard for b in r]) == 2


def test_plan_table_uses_owner_title_for_user_count_label():
    import inspect
    src = inspect.getsource(VU)
    assert src.count("PATCH_PLAN_TABLE_TITLE_LABEL_V1") == 2
    row = VU._plan_table_row(type("P", (), dict(id=1, volume_gb=400, days=30, price=85000, limit_ip=1,
                                                   title="یک کاربر"))())
    assert "یک کاربر" in row[2].text and "تک‌کاربره" not in row[2].text
    row2 = VU._plan_table_row(type("P", (), dict(id=2, volume_gb=400, days=30, price=1, limit_ip=2, title="پلن ۴۰۰"))())
    assert "2 کاربره" in row2[2].text


def test_plan_table_row_can_hide_volume_and_show_only_users():
    P = lambda **k: type("P", (), dict(id=1, volume_gb=400, days=30, price=85000, limit_ip=1, title="یک کاربر", **k))()
    full = VU._plan_table_row(P())
    only = VU._plan_table_row(P(), True, False)
    assert "400GB" in full[2].text and "400GB" not in only[2].text and only[2].text == "یک کاربر"
    # بدونِ تعدادِ کاربر (limit_ip=0) حجم نشان داده می‌شود تا خانه خالی نماند
    nolimit = type("P", (), dict(id=2, volume_gb=50, days=30, price=1, limit_ip=0, title="x"))()
    assert "50GB" in VU._plan_table_row(nolimit, True, False)[2].text
    # قیمت و زمان دست‌نخورده
    assert only[0].text and only[1].text == "30 روز"


def test_admin_has_volume_toggle_handler_and_setting_key():
    import inspect
    assert hasattr(VA, "vpn_plan_volume_toggle")
    src = inspect.getsource(VA.vpn_plans)
    assert "tvpn:planvol:" in src and "plan_vol_" in src


def test_keyboard_mode_settings_button_has_a_text_handler():
    """در منویِ کیبوردی دکمهٔ تنظیمات فقط متن می‌فرستد؛ برچسب و پیشوندِ هندلرِ متنی باید دقیقاً یکی باشد
    (باگِ گزارش‌شدهٔ ایلیا: دکمه هیچ واکنشی نداشت)."""
    import inspect
    src = inspect.getsource(tenant)
    label_prefix = "↩️ دکمه‌هایِ بازگشت: "
    assert f'opts.append(("{label_prefix}"' in src
    assert f'startswith("{label_prefix.strip()}")' in src
