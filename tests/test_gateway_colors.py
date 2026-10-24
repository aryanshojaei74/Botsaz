"""PATCH_GATEWAY_COLORS_V1 (فرزاد): رنگِ دکمهٔ هر درگاه را مالک تعیین می‌کند."""
from types import SimpleNamespace as NS

import pytest
from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import tenant
from tenant.gateways import gateway_layout as GL


def _kb():
    return M(inline_keyboard=[[B(text="💰 از کیف‌پول", callback_data="pay:1", style=ButtonStyle.SUCCESS)],
                              [B(text="کارت به کارت آنی", callback_data="w:apay")],
                              [B(text="آپی-مستقیم", callback_data="w:apay:vpn:3:100000")],
                              [B(text="زرین‌پال", callback_data="w:zarinpal")],
                              [B(text="⬅️ بازگشت", callback_data="back", style=ButtonStyle.DANGER)]])


@pytest.fixture
def settings(monkeypatch):
    store = {"gw_color_apay": "primary"}

    async def g(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(GL, "_get", g)
    allowed = {"v": True}

    async def ok(bot_id):
        return allowed["v"]
    monkeypatch.setattr(tenant, "_ibtn_colored_allowed", ok)
    return store, allowed


def _styles(mk):
    return {b.callback_data: b.style for r in mk.inline_keyboard for b in r}


@pytest.mark.asyncio
async def test_only_the_chosen_gateway_changes_color_including_direct_pay_variants(settings):
    out = await GL.apply_gateway_colors(1, _kb())
    st = _styles(out)
    assert st["w:apay"] == ButtonStyle.PRIMARY and st["w:apay:vpn:3:100000"] == ButtonStyle.PRIMARY
    assert st["w:zarinpal"] is None and st["pay:1"] == ButtonStyle.SUCCESS and st["back"] == ButtonStyle.DANGER


@pytest.mark.asyncio
async def test_no_change_when_unset_or_plan_has_no_colored_buttons(settings):
    store, allowed = settings
    kb = _kb()
    allowed["v"] = False
    assert await GL.apply_gateway_colors(1, kb) is kb
    allowed["v"] = True
    store["gw_color_apay"] = ""
    assert await GL.apply_gateway_colors(1, kb) is kb
    plain = M(inline_keyboard=[[B(text="x", callback_data="pay:1")]])
    assert await GL.apply_gateway_colors(1, plain) is plain


@pytest.mark.asyncio
async def test_send_layer_applies_gateway_colors(settings, monkeypatch):
    from aiogram.methods import SendMessage
    sent = []

    class Sess:
        async def make_request(self, bot, method, timeout=None):
            sent.append(method)
            return NS(message_id=1)
    bot = NS(session=Sess(), token="1:T")

    async def gbr(b):
        return NS(id=7, owner_id=1, plan="pro")

    async def no_nav(bid):
        return False

    async def staff(b, uid):
        return True       # از مسیرِ نگهدارِ nav رد شود؛ فقط رنگ را می‌سنجیم
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "nav_kb_only", no_nav)
    monkeypatch.setattr(tenant, "_nav_is_staff", staff)
    tenant._nav_install(bot)
    await bot.session.make_request(bot, SendMessage(chat_id=500, text="x", reply_markup=_kb()))
    assert _styles(sent[-1].reply_markup)["w:apay"] == ButtonStyle.PRIMARY


@pytest.mark.asyncio
async def test_menu_has_color_button_for_each_gateway_and_cycle_order(settings):
    text, kb = await GL._menu_content(1)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert all(f"gwl:col:{k}" in cbs for k, *_ in GL.ITEMS)
    assert GL._COLOR_CYCLE == ["", "success", "primary", "danger"]
