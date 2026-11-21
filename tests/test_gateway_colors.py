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


@pytest.mark.asyncio
async def test_menu_layout_is_two_rows_per_gateway_name_then_tools(settings):
    text, kb = await GL._menu_content(1)
    rows = kb.inline_keyboard
    n = len(GL.ITEMS)
    assert len(rows) == 2 * n + 3                                   # نام + ابزار برایِ هر درگاه + «مستقیم بالا» + بازنشانی + بازگشت
    name_row, tools = rows[0], rows[1]
    assert len(name_row) == 1 and name_row[0].callback_data == "gwl:noop" and name_row[0].text.startswith("1.")
    assert [b.text[:2] for b in tools][:2] == ["⬆️", "⬇️"] and tools[-1].callback_data.startswith("gwl:col:")
    apay_head = next(r[0] for r in rows if "آپی" in r[0].text)
    assert apay_head.style == ButtonStyle.PRIMARY                    # رنگِ انتخابی روی نامِ درگاه پیش‌نمایش می‌شود


@pytest.mark.asyncio
async def test_direct_pay_button_goes_first_only_when_enabled(settings):
    store, _ = settings
    kb = _kb()
    assert await GL.apply_direct_first(1, kb) is kb                       # پیش‌فرض: بدونِ تغییر
    store["gw_direct_first"] = "1"
    out = await GL.apply_direct_first(1, kb)
    cbs = [r[0].callback_data for r in out.inline_keyboard]
    assert cbs[0] == "w:apay:vpn:3:100000"                                # مستقیم اول؛ «شارژ» (w:apay) جابه‌جا نشد
    assert cbs[1:] == ["pay:1", "w:apay", "w:zarinpal", "back"]
    again = await GL.apply_direct_first(1, out)
    assert again is out                                                    # از قبل بالاست ⇒ همان شیء


@pytest.mark.asyncio
async def test_menu_has_direct_first_toggle(settings):
    text, kb = await GL._menu_content(1)
    t = next(b for r in kb.inline_keyboard for b in r if b.callback_data == "gwl:first")
    assert "❌" in t.text


@pytest.mark.asyncio
async def test_card_to_card_buttons_get_color_and_custom_name_everywhere(settings):
    store, _ = settings
    store["gw_color_card"] = "success"
    store["card_display_name"] = "💳 کارت به کارت (تأیید آنی و خودکار با هوش مصنوعی)"
    mk = M(inline_keyboard=[[B(text="💳 شارژ کارت‌به‌کارت", callback_data="w:charge")],
                            [B(text="💳 کارت‌به‌کارت", callback_data="pay:card:5")],
                            [B(text="💳 پرداخت کارت‌به‌کارت", callback_data="vpay:card:9")],
                            [B(text="🏷 کد تخفیف", callback_data="vdisc:add:9")]])
    out = await GL.apply_card_rename(1, await GL.apply_gateway_colors(1, mk))
    flat = [b for r in out.inline_keyboard for b in r]
    assert [b.text for b in flat[:3]] == [store["card_display_name"]] * 3
    assert all(b.style == ButtonStyle.SUCCESS for b in flat[:3]) and flat[3].text == "🏷 کد تخفیف"
    store["card_display_name"] = ""
    assert await GL.apply_card_rename(1, mk) is mk                       # بدونِ اسمِ سفارشی دست نمی‌خورد


def test_card_is_renamable_and_in_gateway_labels():
    assert GL._BY_KEY["card"][2] is True
    assert "card" in tenant.GATEWAY_LABELS and tenant.GATEWAY_LABELS["card"][1] == "card_display_name"
    assert GL.has_gateway_buttons(M(inline_keyboard=[[B(text="x", callback_data="vpay:card:1")]]))
    assert not GL.has_gateway_buttons(M(inline_keyboard=[[B(text="x", callback_data="set:card")]]))


@pytest.mark.asyncio
async def test_direct_crypto_deposit_is_renamable_colorable_and_orderable(settings):
    store, _ = settings
    store.update(gw_color_cdirect="danger", cdirect_display_name="💎 واریز مستقیم (TON/USDT)", gw_order="cdirect,card")
    assert GL._BY_KEY["cdirect"][2] is True and tenant.GATEWAY_LABELS["cdirect"][1] == "cdirect_display_name"
    mk = M(inline_keyboard=[[B(text="x", callback_data="w:charge")], [B(text="y", callback_data="trxd:ent:USDT")],
                            [B(text="z", callback_data="w:ton_direct")], [B(text="h", callback_data="w:history")]])
    out = await GL.apply_card_rename(1, await GL.apply_gateway_colors(1, mk))
    flat = [b for r in out.inline_keyboard for b in r]
    assert [b.text for b in flat[1:3]] == ["💎 واریز مستقیم (TON/USDT)"] * 2          # هر دو شکلِ دکمه
    assert all(b.style == ButtonStyle.DANGER for b in flat[1:3]) and flat[3].text == "h"
    assert GL.has_gateway_buttons(M(inline_keyboard=[[B(text="y", callback_data="trxd:ent:TRX")]]))
    ordered = await GL.apply_order_rows(1, [[B(text="card", callback_data="w:charge")], [B(text="cd", callback_data="w:cdirect")]])
    assert [r[0].callback_data for r in ordered] == ["w:cdirect", "w:charge"]            # ترتیب شاملِ واریزِ مستقیم
    assert not GL._is_direct_gw_cb("w:ton_direct")                                       # شارژِ کیف پول است، نه پرداختِ مستقیمِ فاکتور
