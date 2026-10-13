"""
تست‌هایِ رجیستریِ تنظیمات (PATCH_SETTINGS_REGISTRY_V1).

بدونِ aiogram/دیتابیس اجرا می‌شوند: ماژول‌ها با مسیرِ فایل بارگذاری می‌شوند
تا __init__ِ سنگینِ tenant اجرا نشود، و sys.modules بعدِ هر تست برمی‌گردد.
هدف: «دکمه هست ولی هندلر ندارد» و «ترتیبِ منو عوض شد» زودتر از کاربر معلوم شود.
"""
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def reg(monkeypatch):
    mod = _load("_reg_under_test", "tenant/settings_registry.py")
    yield mod
    sys.modules.pop("_reg_under_test", None)


@pytest.fixture
def vs(monkeypatch):
    """settings.py خدماتِ مجازی، روی یک رجیستریِ تازه."""
    regmod = _load("_reg_for_vs", "tenant/settings_registry.py")
    monkeypatch.setitem(sys.modules, "tenant", types.ModuleType("tenant"))
    monkeypatch.setitem(sys.modules, "tenant.settings_registry", regmod)
    mod = _load("_vs_under_test", "tenant/bots/virtual_service/settings.py")
    yield regmod, mod
    sys.modules.pop("_vs_under_test", None)
    sys.modules.pop("_reg_for_vs", None)


async def _noop(cb, bot):
    return None


def _item(reg, label, cb, **kw):
    return reg.SettingsItem(label=label, callback=cb, handler=_noop, **kw)


# ───────────── خودِ رجیستری ─────────────

def test_duplicate_callback_rejected(reg):
    reg.register_settings_items("k", [_item(reg, "الف", "x:1")])
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [_item(reg, "ب", "x:1")])


def test_duplicate_label_rejected(reg):
    reg.register_settings_items("k", [_item(reg, "الف", "x:1")])
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [_item(reg, "الف", "x:2")])


def test_needs_state_requires_handler(reg):
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [reg.SettingsItem("الف", "x:1", None, needs_state=True)])


def test_merge_inserts_after_first_existing_anchor(reg):
    opts = [("A", "a"), ("B", "b"), ("C", "c")]
    it = _item(reg, "X", "x", after=("zzz", "b"))
    assert reg.merge_into(opts, [it]) == [("A", "a"), ("B", "b"), ("X", "x"), ("C", "c")]


def test_merge_appends_when_no_anchor(reg):
    it = _item(reg, "X", "x", after=("nope",))
    assert reg.merge_into([("A", "a")], [it])[-1] == ("X", "x")


def test_merge_does_not_duplicate_or_mutate(reg):
    opts = [("A", "a"), ("X-old", "x")]
    out = reg.merge_into(opts, [_item(reg, "X", "x")])
    assert out == opts and out is not opts


def test_chained_anchors(reg):
    a = _item(reg, "A2", "a2", after=("a1",))
    b = _item(reg, "A3", "a3", after=("a2",))
    out = reg.merge_into([("A1", "a1"), ("Z", "z")], [a, b])
    assert [cb for _, cb in out] == ["a1", "a2", "a3", "z"]


def test_visible_filter_and_broken_predicate_is_hidden(reg):
    def boom(plan):
        raise RuntimeError("x")
    reg.register_settings_items("k", [
        _item(reg, "ok", "o:1", visible=lambda p: p == "pro"),
        _item(reg, "bad", "o:2", visible=boom),
        _item(reg, "always", "o:3"),
    ])
    assert [i.callback for i in reg.visible_items("k", "pro")] == ["o:1", "o:3"]
    assert [i.callback for i in reg.visible_items("k", "free")] == ["o:3"]
    assert reg.visible_items("other-kind", "pro") == []


def test_keyboard_item_lookup(reg):
    reg.register_settings_items("k", [
        _item(reg, "الف", "x:1"),
        reg.SettingsItem("inline-only", "x:2", None),
    ])
    assert reg.keyboard_item("k", "الف").callback == "x:1"
    assert reg.keyboard_item("k", "inline-only") is None
    assert reg.keyboard_item("k", "نامعلوم") is None
    assert reg.keyboard_item("other", "الف") is None


# ───────────── آیتم‌هایِ واقعیِ خدماتِ مجازی ─────────────

# callback_dataهایِ زنده در چتِ کاربران — نباید تغییر کنند
LIVE_CALLBACKS = {
    "set:profile_footer", "tadm:apikey", "tadm:groupsettings", "miniapp:nft",
    "set:escrow",   # PATCH_ESCROW_V1
}


def test_virtual_items_keep_live_callbacks(vs):
    regmod, mod = vs
    got = {i.callback for i in regmod.registered_items("virtual_service")}
    assert got == LIVE_CALLBACKS


def test_virtual_menu_order_matches_previous_layout(vs):
    """ترتیبِ قبلی: homemenutext, footer, api, backup, group, miniapp:panel, nft."""
    regmod, mod = vs
    base = [("hm", "set:homemenutext"), ("bk", "tadm:backup:menu"), ("mp", "miniapp:panel"),
            ("ch", "tadm:chpost")]
    items = [i for i in regmod.registered_items("virtual_service")
             if i.callback != "miniapp:nft"] + \
            [i for i in regmod.registered_items("virtual_service") if i.callback == "miniapp:nft"]
    out = [cb for _, cb in regmod.merge_into(base, items)]
    assert out == ["set:homemenutext", "set:profile_footer", "tadm:apikey",
                   "tadm:backup:menu", "tadm:groupsettings", "miniapp:panel",
                   "miniapp:nft", "tadm:chpost", "set:escrow"]


def test_virtual_menu_without_backup_or_miniapp(vs):
    regmod, mod = vs
    base = [("hm", "set:homemenutext"), ("ch", "tadm:chpost")]
    out = [cb for _, cb in regmod.merge_into(base, regmod.registered_items("virtual_service"))]
    assert out.index("set:profile_footer") == 1
    assert out.index("tadm:apikey") == 2
    assert out.index("tadm:groupsettings") == 3
    assert out[-2:] == ["tadm:chpost", "set:escrow"]


def test_api_item_is_owner_only_and_others_are_not(vs):
    regmod, mod = vs
    by_cb = {i.callback: i for i in regmod.registered_items("virtual_service")}
    assert by_cb["tadm:apikey"].owner_only
    assert by_cb["tadm:apikey"].deny_text.startswith("🔒")
    assert not by_cb["set:profile_footer"].owner_only
    assert by_cb["set:profile_footer"].needs_state


def test_every_virtual_item_has_a_keyboard_handler(vs):
    regmod, mod = vs
    for it in regmod.registered_items("virtual_service"):
        assert it.handler is not None, it.callback
        assert regmod.keyboard_item("virtual_service", it.label) is it


# ───────────── هر callbackِ ثبت‌شده باید هندلرِ واقعی داشته باشد ─────────────

def _all_handled_callbacks() -> str:
    """همهٔ متنِ سورسِ tenant/ — تا ببینیم callback در یک decoratorِ callback_query هست."""
    chunks = []
    for p in (ROOT / "tenant").rglob("*.py"):
        if "core/__init__" in p.as_posix():
            continue
        chunks.append(p.read_text(encoding="utf-8"))
    return "\n".join(chunks)


@pytest.mark.parametrize("cb", sorted(LIVE_CALLBACKS))
def test_registered_callback_has_a_router_handler(cb):
    src = _all_handled_callbacks()
    pat = re.compile(r"callback_query\([^)\n]*['\"]" + re.escape(cb) + r"['\"]")
    assert pat.search(src), f"دکمهٔ «{cb}» هندلرِ callback_query ندارد (دکمهٔ مرده)"


# ───────────── آیتم‌هایِ واقعیِ VPN (PATCH_SETTINGS_REGISTRY_V2_VPN) ─────────────

VPN_CALLBACKS = {
    "tadm:edit_guides", "tadm:cfgguide", "tadm:cfgnameprompt", "set:trial",
    "set:buy_onhold", "qrbg:menu", "tadm:groupsettings",
    "vtl:menu",   # PATCH_VPN_TITLES_IN_SETTINGS_V1
    "vrf:cfg",    # PATCH_VPN_REFUND_V1
    "stk:menu",   # PATCH_STOCK_ALARM_V1
}


@pytest.fixture
def vpn(monkeypatch):
    regmod = _load("_reg_for_vpn", "tenant/settings_registry.py")
    monkeypatch.setitem(sys.modules, "tenant", types.ModuleType("tenant"))
    monkeypatch.setitem(sys.modules, "tenant.settings_registry", regmod)
    mod = _load("_vpn_under_test", "tenant/bots/vpn/settings.py")
    yield regmod, mod
    sys.modules.pop("_vpn_under_test", None)
    sys.modules.pop("_reg_for_vpn", None)


def test_vpn_items_keep_live_callbacks(vpn):
    regmod, mod = vpn
    assert {i.callback for i in regmod.registered_items("vpn_sales")} == VPN_CALLBACKS


def test_vpn_menu_order_matches_previous_layout(vpn):
    """ترتیبِ قبلی: btn_colors, edit_guides, cfgguide, cfgnameprompt, trial, gwname,
    buy_onhold, ..., dga, qr, ..., homemenutext, backup, group, miniapp:panel."""
    regmod, mod = vpn
    base = [("h", "tadm:homebtns"), ("c", "tadm:btn_colors"), ("g", "gwname:menu"),
            ("w", "set:welcome"), ("d", "dga:menu"), ("p", "pemj:menu:0"),
            ("hm", "set:homemenutext"), ("bk", "tadm:backup:menu"), ("mp", "miniapp:panel")]
    items = regmod.visible_items("vpn_sales", "pro")
    out = [cb for _, cb in regmod.merge_into(base, items)]
    assert out == ["tadm:homebtns", "tadm:btn_colors", "tadm:edit_guides", "tadm:cfgguide",
                   "tadm:cfgnameprompt", "set:trial", "gwname:menu", "set:buy_onhold",
                   "set:welcome", "dga:menu", "qrbg:menu", "pemj:menu:0",
                   "set:homemenutext", "tadm:backup:menu", "tadm:groupsettings", "miniapp:panel", "vtl:menu", "vrf:cfg", "stk:menu"]


def test_vpn_group_settings_only_on_paid_plans(vpn):
    regmod, mod = vpn
    def cbs(plan):
        return {i.callback for i in regmod.visible_items("vpn_sales", plan)}
    assert "tadm:groupsettings" not in cbs("free")
    assert "tadm:groupsettings" in cbs("growth")
    assert "tadm:groupsettings" in cbs("pro")


def test_vpn_keyboard_handlers_and_state_flags(vpn):
    regmod, mod = vpn
    for it in regmod.registered_items("vpn_sales"):
        assert regmod.keyboard_item("vpn_sales", it.label) is it, it.callback
    by_cb = {i.callback: i for i in regmod.registered_items("vpn_sales")}
    assert by_cb["tadm:cfgnameprompt"].needs_state
    assert by_cb["qrbg:menu"].needs_state
    assert not by_cb["set:trial"].needs_state


def test_vpn_buy_onhold_dynamic_label_matches_by_prefix(vpn):
    regmod, mod = vpn
    it = next(i for i in regmod.registered_items("vpn_sales") if i.callback == "set:buy_onhold")
    for suffix in ("▶️ از لحظهٔ خرید", "⏸ از اولین اتصال"):
        assert regmod.keyboard_item("vpn_sales", mod._BUY_ONHOLD_PREFIX + ": " + suffix) is it
    assert regmod.keyboard_item("virtual_service", mod._BUY_ONHOLD_PREFIX + ": x") is None


@pytest.mark.asyncio
async def test_vpn_buy_onhold_label_follows_setting(vpn, monkeypatch):
    regmod, mod = vpn
    state = {"v": "0"}
    async def fake_get_setting(bot_id, key, default=""):
        return state["v"]
    fake_tenant = types.ModuleType("tenant")
    fake_tenant.get_setting = fake_get_setting
    monkeypatch.setitem(sys.modules, "tenant", fake_tenant)
    items = regmod.visible_items("vpn_sales", "pro")
    assert "از لحظهٔ خرید" in await _label(regmod, items)
    state["v"] = "1"
    assert "از اولین اتصال" in await _label(regmod, items)


async def _label(regmod, items):
    out = await regmod.resolve_labels(items, 1)
    return next(i.label for i in out if i.callback == "set:buy_onhold")


@pytest.mark.asyncio
async def test_resolve_labels_survives_label_fn_error(reg):
    async def boom(bot_id):
        raise RuntimeError("x")
    it = reg.SettingsItem("پیش‌فرض: الف", "x:1", _noop, label_fn=boom, label_prefix="پیش‌فرض")
    out = await reg.resolve_labels([it], 1)
    assert out[0].label == "پیش‌فرض: الف"


def test_label_fn_requires_prefix_and_prefix_must_match(reg):
    async def f(bot_id):
        return "x"
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [reg.SettingsItem("الف", "x:1", _noop, label_fn=f)])
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [reg.SettingsItem("الف", "x:2", _noop, label_prefix="ب")])


def test_cfg_guide_label_equals_central_constant(vpn):
    """BTN_CFG_GUIDE در tenant/__init__.py تعریف می‌شود؛ متنِ تکراری باید یکی بماند."""
    regmod, mod = vpn
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^BTN_CFG_GUIDE\s*=\s*"([^"]+)"', src, re.M)
    assert m and m.group(1) == mod._CFG_GUIDE_LABEL


@pytest.mark.parametrize("cb", sorted(VPN_CALLBACKS))
def test_vpn_registered_callback_has_a_router_handler(cb):
    src = _all_handled_callbacks()
    pat = re.compile(r"callback_query\([^)\n]*['\"]" + re.escape(cb) + r"['\"]")
    assert pat.search(src), f"دکمهٔ «{cb}» هندلرِ callback_query ندارد (دکمهٔ مرده)"


def test_no_kind_specific_if_left_in_central_settings_menu():
    """رگرسیون: کسی دوباره `if _is_vpn/_is_virtual` را به منویِ مشترک برنگرداند."""
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("async def _sec_settings_kb(")
    j = src.index("async def _gate(", i)
    body = src[i:j]
    assert "_is_vpn" not in body and "_is_virtual" not in body


# ───────────── منویِ «📦 سرویس‌ها و محصولات» (PATCH_SETTINGS_REGISTRY_V3_PRODUCTS) ─────────────

PRODUCT_KINDS = ["vpn_sales", "digital_shop", "booking", "virtual_service",
                 "community_mgmt", "ad_bot"]


@pytest.fixture
def prod(monkeypatch):
    """همهٔ ماژول‌هایِ products_menu روی یک رجیستریِ تازه (بدونِ اجرایِ __init__ِ سنگینِ tenant)."""
    regmod = _load("_reg_for_prod", "tenant/settings_registry.py")
    monkeypatch.setitem(sys.modules, "tenant", types.ModuleType("tenant"))
    monkeypatch.setitem(sys.modules, "tenant.settings_registry", regmod)
    loaded = []
    for i, kind_dir in enumerate(["vpn", "digital_shop", "booking", "virtual_service",
                                  "community_mgmt", "ad_bot"]):
        name = f"_pm_{i}"
        _load(name, f"tenant/bots/{kind_dir}/products_menu.py")
        loaded.append(name)
    yield regmod
    for name in loaded + ["_reg_for_prod"]:
        sys.modules.pop(name, None)


def test_every_kind_has_a_products_menu(prod):
    for kind in PRODUCT_KINDS:
        assert prod.products_menu_for(kind) is not None, kind
    assert prod.products_menu_for(None) is None
    assert prod.products_menu_for("unknown") is None


def test_products_menu_duplicate_registration_rejected(prod):
    with pytest.raises(ValueError):
        prod.register_products_menu("booking", prod.ProductsMenu(opts=_noop))


def test_products_menu_rejects_bad_style_name(prod):
    with pytest.raises(ValueError):
        prod.register_products_menu("new_kind", prod.ProductsMenu(opts=_noop, styles={"a:b": "red"}))


@pytest.mark.asyncio
async def test_virtual_service_products_layout_by_plan(prod):
    pm = prod.products_menu_for("virtual_service")
    free = [cb for _, cb in await pm.opts(1, "free")]
    pro = [cb for _, cb in await pm.opts(1, "pro")]
    assert "smm:panels" not in free and "tadm:sec:products:lockedinfo" in free
    assert "smm:panels" in pro and "smm:products:menu" in pro
    assert free[0] == pro[0] == "tadm:vs_orders"          # «مشاهده سفارشات» همیشه اول
    assert free[-1] == pro[-1] == "tadm:products:guide"
    assert pm.row_pairs == {"smm:products:menu": "smm:cat:menu:top", "tadm:vs_prices": "smm:manlist"}


@pytest.mark.asyncio
async def test_vpn_products_always_end_with_guide(prod):
    pm = prod.products_menu_for("vpn_sales")
    for plan in ("free", "growth", "pro"):
        opts = await pm.opts(1, plan)
        assert opts and opts[-1][1] == "tadm:products:guide"


@pytest.mark.asyncio
async def test_products_opts_are_unique_and_styles_reference_real_callbacks(prod):
    """رنگی که به callbackِ ناموجود اشاره کند یعنی یک دکمه جابه‌جا/حذف شده و رنگش جا مانده."""
    for kind in PRODUCT_KINDS:
        pm = prod.products_menu_for(kind)
        seen_any = set()
        for plan in ("free", "growth", "pro"):
            cbs = [cb for _, cb in await pm.opts(1, plan)]
            assert len(cbs) == len(set(cbs)), (kind, plan, "callback تکراری")
            seen_any.update(cbs)
        # smm:menu قدیمی است و عمداً رنگ دارد ولی دکمه‌اش حذف شده؛ استثنایِ شناخته‌شده
        orphan = set(pm.styles) - seen_any - {"smm:menu"}
        assert not orphan, (kind, orphan)
        for a, b in pm.row_pairs.items():
            assert a in seen_any and b in seen_any, (kind, a, b)


def test_products_description_texts_kept(prod):
    assert prod.products_menu_for("virtual_service").description == "مدیریت سفارش‌های خدمات مجازی تلگرام:"
    assert prod.products_menu_for("ad_bot").description.startswith("مدیریت تبلیغِ")
    assert prod.products_menu_for("community_mgmt").description == ""   # متنِ عمومیِ پیش‌فرض


PRODUCTS_CALLBACK_PATTERNS = {
    # callbackهایی که با F.data == "..." یا startswith/in روتر ثبت شده‌اند
    "tadm:cfg", "tvpn:menu", "pnlmkt:menu", "pyar:menu", "tadm:products:guide",
    "cmty:menu", "adb:menu", "tadm:polls", "smmai:menu:cmty", "tadm:shop", "tadm:booking",
    "tadm:vs_orders", "smm:panels", "ozv:menu", "smm:products:menu", "smm:cat:menu:top",
    "tadm:vs_prices", "smm:manlist", "tadm:sec:products:lockedinfo",
}


@pytest.mark.parametrize("cb", sorted(PRODUCTS_CALLBACK_PATTERNS))
def test_products_callback_has_some_handler(cb):
    """هر دکمهٔ منویِ محصولات باید جایی در tenant/ یک فیلترِ callback_query داشته باشد
    (== دقیق، یا startswith/in یا استفاده در یک F.data)."""
    src = _all_handled_callbacks()
    assert f'"{cb}"' in src or f"'{cb}'" in src, f"«{cb}» هیچ‌جا در کد نیست"
    exact = re.search(r"callback_query\([^)\n]*['\"]" + re.escape(cb) + r"['\"]", src)
    prefixed = any(
        re.search(r"F\.data\.(?:startswith|in_)\(", line) and cb.split(":")[0] + ":" in line
        for line in src.splitlines()
    )
    assert exact or prefixed, f"دکمهٔ «{cb}» هندلرِ callback_query ندارد (دکمهٔ مرده)"


def test_no_kind_specific_if_left_in_central_products_menu():
    """رگرسیون: `if bot_field == "<kind>"` نباید به _sec_products_kb برگردد."""
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("async def _sec_products_kb(")
    j = src.index("\nasync def ", i + 10)
    body = src[i:j]
    for kind in PRODUCT_KINDS:
        assert f'bot_field == "{kind}"' not in body, kind


# ───────── هندلرهایِ حالتِ کیبوردیِ منویِ محصولات (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────

@pytest.mark.asyncio
async def test_every_products_button_has_a_keyboard_handler(prod):
    """هر دکمه‌ای که منو می‌سازد باید در حالتِ کیبوردی هم هندلر داشته باشد
    (یا صریحاً در keyboard_shared اعلام شده باشد) — ریشهٔ «دکمهٔ مرده»."""
    for kind in PRODUCT_KINDS:
        pm = prod.products_menu_for(kind)
        handled = set(pm.keyboard) | set(pm.keyboard_shared)
        for plan in ("free", "growth", "pro"):
            for label, cb in await pm.opts(1, plan):
                assert label in handled, f"[{kind}/{plan}] «{label}» ({cb}) در حالتِ کیبوردی هندلر ندارد"


def test_keyboard_labels_belong_to_their_own_menu_only(prod):
    """یک handler فقط زیرِ نوعی ثبت شود که همان دکمه را در منویش دارد."""
    import asyncio
    for kind in PRODUCT_KINDS:
        pm = prod.products_menu_for(kind)
        shown = set()
        for plan in ("free", "growth", "pro"):
            shown |= {lbl for lbl, _ in asyncio.run(pm.opts(1, plan))}
        stray = set(pm.keyboard) - shown
        assert not stray, (kind, stray)


def test_products_keyboard_handler_lookup(prod):
    assert prod.products_keyboard_handler("booking", "📅 سیستم رزرو و نوبت") is not None
    assert prod.products_keyboard_handler("booking", "🗂 مشاهده سفارشات") is None
    assert prod.products_keyboard_handler("virtual_service", "🗂 مشاهده سفارشات") is not None
    assert prod.products_keyboard_handler(None, "x") is None
    assert prod.products_keyboard_handler("unknown", "x") is None


class _Msg:
    def __init__(self):
        self.sent = []
    async def answer(self, text="", **kw):
        self.sent.append((text, kw))


def _stub_module(monkeypatch, dotted, **attrs):
    mod = types.ModuleType(dotted)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, dotted, mod)
    return mod


def _row(plan, field="virtual_service"):
    return types.SimpleNamespace(id=7, plan=plan, field=field, owner_id=1)


@pytest.mark.asyncio
async def test_vs_locked_button_answers_instead_of_being_dead(prod):
    """دکمهٔ قفلِ پلنِ رایگان در حالتِ کیبوردی قبلاً هیچ هندلری نداشت."""
    h = prod.products_keyboard_handler(
        "virtual_service", "🔒 ارائه‌دهنده و مدیریتِ محصولات (پلنِ رشد به بالا)")
    msg = _Msg()
    await h(msg, _row("free"), None, None)
    assert msg.sent and "پلنِ رشد" in msg.sent[0][0]


@pytest.mark.asyncio
async def test_vs_provider_buttons_are_plan_gated_and_call_content(prod, monkeypatch):
    calls = []
    async def panels(bot_id):
        calls.append(("panels", bot_id)); return "T", "KB"
    _stub_module(monkeypatch, "tenant.bots.virtual_service.smm", smm_panels_content=panels)
    h = prod.products_keyboard_handler("virtual_service", "🏢 ارائه‌دهندگان")

    free_msg = _Msg()
    await h(free_msg, _row("free"), None, None)
    assert calls == [] and "پلنِ رشد" in free_msg.sent[0][0]      # قفل؛ محتوا صدا نخورد

    pro_msg = _Msg()
    await h(pro_msg, _row("pro"), None, None)
    assert calls == [("panels", 7)]
    assert pro_msg.sent == [("T", {"reply_markup": "KB", "parse_mode": "HTML"})]


@pytest.mark.asyncio
async def test_vs_categories_sets_origin_before_showing(prod, monkeypatch):
    order = []
    async def cats(bot_id):
        order.append("content"); return "T", "KB"
    async def set_setting(bot_id, key, value):
        order.append(("set", key, value))
    _stub_module(monkeypatch, "tenant.bots.virtual_service.smm",
                 smm_categories_content=cats, _set_setting=set_setting)
    h = prod.products_keyboard_handler("virtual_service", "📁 مدیریتِ دسته‌ها")
    await h(_Msg(), _row("pro"), None, None)
    assert order == [("set", "smm_cat_origin", "top"), "content"]


@pytest.mark.asyncio
async def test_vpn_gated_buttons_fall_through_when_feature_missing(prod):
    """اگر پلن قابلیت را ندارد، handler باید False بدهد تا زنجیرهٔ مرکزی ادامه پیدا کند
    (رفتارِ قبلی: شرطِ `and has_feature(...)`)."""
    for label in ("🏷 ثبت و فروش پنل", "🗂 پنل‌یار"):
        h = prod.products_keyboard_handler("vpn_sales", label)
        assert await h(_Msg(), _row("free", "vpn_sales"), None, None) is False


@pytest.mark.asyncio
async def test_community_ai_button_opens_the_inline_page(prod, monkeypatch):
    """«🤖 دستیارِ هوشِ مصنوعیِ گروه» در حالتِ کیبوردی قبلاً بی‌واکنش بود."""
    seen = {}
    class FakeCb:
        def __init__(self, msg, data=""):
            seen["data"] = data
    async def smmai_menu(cb, bot):
        seen["called"] = True
    _stub_module(monkeypatch, "tenant", _FakeCb=FakeCb)
    _stub_module(monkeypatch, "tenant.bots.virtual_service.smm_ai", smmai_menu=smmai_menu)
    h = prod.products_keyboard_handler("community_mgmt", "🤖 دستیارِ هوشِ مصنوعیِ گروه")
    await h(_Msg(), _row("pro", "community_mgmt"), None, None)
    assert seen == {"data": "smmai:menu:cmty", "called": True}


@pytest.mark.asyncio
async def test_shop_and_ad_panel_handlers_answer(prod, monkeypatch):
    _stub_module(monkeypatch, "tenant.bots.digital_shop.handlers", shop_admin_kb=lambda field: f"KB:{field}")
    async def render(bot_id):
        return "PANEL", "PKB"
    _stub_module(monkeypatch, "tenant.shared.ad_broadcast", _render_panel=render)

    msg = _Msg()
    await prod.products_keyboard_handler("digital_shop", "🛒 فروشگاه دیجیتال")(
        msg, _row("pro", "digital_shop"), None, None)
    assert msg.sent == [("🛒 <b>مدیریت فروشگاه</b>", {"reply_markup": "KB:digital_shop"})]

    msg = _Msg()
    await prod.products_keyboard_handler("ad_bot", "📢 پنل تبلیغِ زمان‌بندی‌شده")(
        msg, _row("pro", "ad_bot"), None, None)
    assert msg.sent[0][0] == "PANEL" and msg.sent[0][1]["parse_mode"] == "HTML"


def test_central_dispatcher_uses_registry_hook_and_lost_the_moved_blocks():
    """رگرسیون: هوکِ رجیستری هست، و بلوک‌هایِ منتقل‌شده به زنجیرهٔ مرکزی برنگشته‌اند."""
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    assert "products_keyboard_handler" in src
    exclusive = [
        "🏷 ثبت و فروش پنل", "🗂 پنل‌یار", "📦 انبار کانفیگ‌های دستی", "🤖 پنل VPN خودکار (X-UI)",
        "🛒 فروشگاه دیجیتال (محصولات)", "📅 سیستم رزرو و نوبت", "🗂 مشاهده سفارشات",
        "💰 تنظیم قیمت سرویس‌ها", "🏢 ارائه‌دهندگان", "✍️ سرویس‌های دستی",
        "📦 مدیریتِ محصولات", "📁 مدیریتِ دسته‌ها", "🛡 مدیریت گروه و کانال", "📅 زمان‌بندیِ پست",
    ]
    for label in exclusive:
        assert f'msg.text == "{label}"' not in src, f"«{label}» دوباره در زنجیرهٔ مرکزی است"
    # برچسبِ مشترک باید همچنان مرکزی بماند (در منویِ «ادمین‌ها» برایِ همهٔ انواع هست)
    assert 'msg.text == "📋 نظرسنجی‌ها"' in src


# ───────── بخش‌هایِ «مالی» و «ادمین‌ها» (PATCH_SETTINGS_REGISTRY_V5_SECTIONS) ─────────

def _plans():
    from plans import Plan
    return [p.value for p in Plan]


def test_section_key_format(reg):
    assert reg.section_key("finance", "virtual_service") == "finance:virtual_service"
    assert reg.SECTIONS == ("finance", "admins", "home", "hub")


def test_virtual_finance_items_registered(vs):
    regmod, mod = vs
    items = regmod.registered_items(regmod.section_key("finance", "virtual_service"))
    assert {i.callback for i in items} == {"tadm:smmorderchannel", "set:hesabpay", "btr:admin"}
    for i in items:
        assert i.handler is not None and not i.owner_only and not i.needs_state


def test_vpn_admins_item_registered(vpn):
    regmod, mod = vpn
    items = regmod.registered_items(regmod.section_key("admins", "vpn_sales"))
    assert [i.callback for i in items] == ["rsl:menu"]
    assert items[0].handler is not None


def test_finance_items_keep_previous_positions(vs):
    """ترتیبِ قبلی: ... receiptchannel, txlog, kyc, uniquepay, abangateway, [smm], receipt_buttons,
    phonelog, payphone, crypto, [hesabpay], swap ..."""
    regmod, mod = vs
    base = [("a", "tadm:receiptchannel"), ("t", "set:txlog"), ("k", "set:kyc"),
            ("wr", "set:wallet_receipt_buttons"), ("pl", "set:phonelog"),
            ("pp", "set:toggle_payphone"), ("c", "set:crypto"), ("s", "set:swap")]
    items = regmod.visible_items(regmod.section_key("finance", "virtual_service"), "pro")
    out = [cb for _, cb in regmod.merge_into(base, items)]
    assert out == ["tadm:receiptchannel", "set:txlog", "set:kyc", "tadm:smmorderchannel",
                   "set:wallet_receipt_buttons", "set:phonelog", "set:toggle_payphone",
                   "set:crypto", "set:hesabpay", "set:swap", "btr:admin"]


def test_finance_items_when_optional_neighbours_missing(vs):
    regmod, mod = vs
    base = [("a", "tadm:receiptchannel"), ("s", "set:swap")]        # نه crypto، نه payphone، …
    items = regmod.visible_items(regmod.section_key("finance", "virtual_service"), "pro")
    out = [cb for _, cb in regmod.merge_into(base, items)]
    assert out == ["tadm:receiptchannel", "tadm:smmorderchannel", "set:hesabpay", "set:swap", "btr:admin"]


@pytest.mark.parametrize("plan", _plans())
def test_hesabpay_visibility_follows_plan_feature(vs, plan):
    from plans import Feature, has_feature
    regmod, mod = vs
    shown = {i.callback for i in regmod.visible_items(regmod.section_key("finance", "virtual_service"), plan)}
    assert ("set:hesabpay" in shown) == has_feature(plan, Feature.GATEWAY_HESABPAY)
    assert "tadm:smmorderchannel" in shown                         # همیشه برایِ خدماتِ مجازی


@pytest.mark.parametrize("plan", _plans())
def test_reseller_visibility_follows_plan_feature(vpn, plan):
    from plans import Feature, has_feature
    regmod, mod = vpn
    shown = {i.callback for i in regmod.visible_items(regmod.section_key("admins", "vpn_sales"), plan)}
    assert ("rsl:menu" in shown) == has_feature(plan, Feature.RESELLER_TIERS)


def test_reseller_sits_between_forcejoin_and_broadcast(vpn):
    """چیدمانِ منویِ ادمین‌ها به این بسته است که دو گزینهٔ آخر broadcast/polls باشند."""
    regmod, mod = vpn
    base = [("a", "tadm:admins"), ("t", "tadm:tickets"), ("s", "supp:contact"), ("f", "tadm:forcejoin")]
    items = regmod.visible_items(regmod.section_key("admins", "vpn_sales"), "pro")
    out = [cb for _, cb in regmod.merge_into(base, items)]
    assert out == ["tadm:admins", "tadm:tickets", "supp:contact", "tadm:forcejoin", "rsl:menu"]
    out2 = [cb for _, cb in regmod.merge_into(base, items)] + ["tadm:broadcast", "tadm:polls"]
    assert out2[-2:] == ["tadm:broadcast", "tadm:polls"]


def test_other_kinds_get_no_section_items(vs, vpn):
    vs_reg, _ = vs
    vpn_reg, _ = vpn
    assert vs_reg.registered_items(vs_reg.section_key("admins", "virtual_service")) == []
    assert [i.callback for i in vpn_reg.registered_items(vpn_reg.section_key("finance", "vpn_sales"))] == ["btr:admin"]
    assert vs_reg.registered_items(vs_reg.section_key("finance", "digital_shop")) == []


def test_section_keyboard_lookup_is_kind_scoped(vs):
    regmod, mod = vs
    key = regmod.section_key("finance", "virtual_service")
    assert regmod.keyboard_item(key, "🇦🇫 درگاه حساب‌پی (پرداخت افغانی)").callback == "set:hesabpay"
    assert regmod.keyboard_item(regmod.section_key("finance", "vpn_sales"),
                                "🇦🇫 درگاه حساب‌پی (پرداخت افغانی)") is None


SECTION_CALLBACKS = {"tadm:smmorderchannel", "set:hesabpay", "rsl:menu"}


@pytest.mark.parametrize("cb", sorted(SECTION_CALLBACKS))
def test_section_callback_has_a_router_handler(cb):
    src = _all_handled_callbacks()
    pat = re.compile(r"callback_query\([^)\n]*['\"]" + re.escape(cb) + r"['\"]")
    assert pat.search(src), f"دکمهٔ «{cb}» هندلرِ callback_query ندارد (دکمهٔ مرده)"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind_dir,section,kind,callback,module,attr", [
    ("virtual_service", "finance", "virtual_service", "tadm:smmorderchannel",
     "tenant.gateways.receipt_channel", "smm_order_channel_menu"),
    ("virtual_service", "finance", "virtual_service", "set:hesabpay",
     "tenant.gateways.hesabpay", "hesabpay_admin_start"),
    ("vpn", "admins", "vpn_sales", "rsl:menu", "tenant.shared.reseller", "rsl_menu"),
])
async def test_section_handlers_call_the_real_target(monkeypatch, kind_dir, section, kind, callback, module, attr):
    regmod = _load("_reg_sec_kb", "tenant/settings_registry.py")
    monkeypatch.setitem(sys.modules, "tenant", types.ModuleType("tenant"))
    monkeypatch.setitem(sys.modules, "tenant.settings_registry", regmod)
    _load("_plug_sec_kb", f"tenant/bots/{kind_dir}/settings.py")
    calls = []
    async def target(cb, bot):
        calls.append((cb, bot))
    _stub_module(monkeypatch, module, **{attr: target})
    item = next(i for i in regmod.registered_items(regmod.section_key(section, kind)) if i.callback == callback)
    await item.handler("CB", "BOT")
    assert calls == [("CB", "BOT")]
    sys.modules.pop("_reg_sec_kb", None); sys.modules.pop("_plug_sec_kb", None)


def test_central_finance_and_admin_menus_lost_kind_branches():
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    for name, nxt in (("async def _sec_finance_opts(", "\nasync def _sec_finance_kb("),
                      ("async def _sec_admins_kb(", "\nasync def _sec_settings_kb(")):
        i = src.index(name)
        body = src[i:src.index(nxt, i)]
        assert '.field == "virtual_service"' not in body, name
        assert '.field == "vpn_sales"' not in body, name
    assert "🇦🇫 درگاه حساب‌پی (پرداخت افغانی)\": hesabpay_admin_start" not in src
    assert "_rsl_menu_entry" not in src
    assert '_sk("finance", row.field)' in src and '_sk("admins", row.field)' in src


# ───────── پاک‌سازیِ کپیِ مردهٔ tenant/core (PATCH_REMOVE_DEAD_TENANT_CORE_V1) ─────────

def test_dead_tenant_core_copy_is_gone():
    """tenant/core/__init__.py کپیِ ۳۰هزارخطیِ فایلِ مرکزی بود که هیچ‌جا import نمی‌شد و
    از فایلِ زنده جدا افتاده بود (یک پچ فقط در آن بود). نباید برگردد."""
    assert not (ROOT / "tenant" / "core").exists()


def test_no_code_imports_tenant_core():
    for p in ROOT.rglob("*.py"):
        if "__pycache__" in p.parts or p.name.startswith("test_settings_registry"):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        assert "tenant.core" not in txt and "tenant/core" not in txt, p


def test_api_open_to_all_reaches_user_side_in_live_file():
    """سوئیچِ «API برایِ همه» باید در هر سه نقطهٔ سمتِ کاربر (دکمهٔ مشخصات، نمایشِ کلید،
    ساختِ کلیدِ جدید) و خودِ API از یک تصمیمِ مشترک (user_api_allowed) عبور کند."""
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    api = (ROOT / "tenant" / "shared" / "public_api.py").read_text(encoding="utf-8")
    assert "api_open_to_all(u.bot_id)" in api        # تصمیمِ مشترک، فقط یک‌جا
    assert "user_api_allowed" in api.split("async def _authed")[1]
    i = src.index("async def _pubapi_render(")
    j = src.index("\nasync def do_free_trial(", i)
    body = src[i:j]
    assert body.count("user_api_allowed") >= 3       # render + regen + (import)
    assert "_uaa(u)" in src                          # دکمهٔ مشخصات


# ───────── خانهٔ پنل و هابِ همکار (PATCH_SETTINGS_REGISTRY_V6_HOME_HUB) ─────────

def test_home_and_hub_are_registered_sections(reg):
    assert "home" in reg.SECTIONS and "hub" in reg.SECTIONS


def test_style_is_validated(reg):
    with pytest.raises(ValueError):
        reg.register_settings_items("k", [reg.SettingsItem("الف", "x:1", None, style="red")])
    reg.register_settings_items("k", [reg.SettingsItem("ب", "x:2", None, style="success")])
    assert reg.inline_style_name(reg.registered_items("k")[0]) == "success"
    assert reg.inline_style_name(reg.SettingsItem("پ", "x:3")) is None


def test_virtual_help_button_in_home_section(vs):
    regmod, mod = vs
    items = regmod.visible_items(regmod.section_key("home", "virtual_service"), "free")
    assert [i.callback for i in items] == ["hlp:menu"]
    it = items[0]
    assert it.label == "📘 راهنما و دستیارِ هوشمند" and it.style == "success"
    # کیبوردِ قدیمیِ گوشیِ ادمین با متنِ کوتاه‌تر هم باید کار کند (label_prefix)
    assert regmod.keyboard_item(regmod.section_key("home", "virtual_service"), "📘 راهنما و دستیار قدیمی") is it
    assert it.needs_state


def test_help_button_is_only_for_virtual_service(vs, vpn):
    vs_reg, _ = vs
    vpn_reg, _ = vpn
    for kind in ("vpn_sales", "digital_shop", "booking", "community_mgmt", "ad_bot"):
        assert vs_reg.registered_items(vs_reg.section_key("home", kind)) == []
    assert vpn_reg.registered_items(vpn_reg.section_key("home", "vpn_sales")) == []


def test_vpn_titles_row_in_hub_section(vpn):
    regmod, mod = vpn
    items = regmod.visible_items(regmod.section_key("hub", "vpn_sales"), "partner")
    assert [i.callback for i in items] == ["vtl:menu"]
    assert items[0].style == "primary" and items[0].handler is None   # هابِ شیشه‌ایِ همکار؛ کیبوردی ندارد


@pytest.mark.parametrize("cb", ["hlp:menu", "vtl:menu"])
def test_home_hub_callbacks_have_router_handlers(cb):
    src = _all_handled_callbacks()
    pat = re.compile(r"callback_query\([^)\n]*['\"]" + re.escape(cb) + r"['\"]")
    assert pat.search(src), f"دکمهٔ «{cb}» هندلرِ callback_query ندارد (دکمهٔ مرده)"


@pytest.mark.asyncio
async def test_help_handler_forwards_state_to_hlp_menu(vs, monkeypatch):
    seen = {}
    async def hlp_menu(cb, state):
        seen["args"] = (cb, state)
    _stub_module(monkeypatch, "tenant.shared.help_ai", hlp_menu=hlp_menu)
    regmod, mod = vs
    it = regmod.visible_items(regmod.section_key("home", "virtual_service"), "pro")[0]
    await it.handler("CB", "BOT", "STATE")
    assert seen["args"] == ("CB", "STATE")          # hlp_menu(cb, state) — bot پاس داده نمی‌شود


def test_central_home_and_hub_lost_kind_branches():
    src = (ROOT / "tenant" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("async def tadmin_kb(")
    body = src[i:src.index("\n# === PATCH_PARTNER_ADMINKB_FIX_V1", i)]
    assert 'bot_field == "virtual_service"' not in body
    j = src.index("async def _tn_general_hub_kb(")
    hub = src[j:src.index("\nasync def _tn_general_hub_text(", j)]
    assert '.field == "vpn_sales"' not in hub
    assert 'startswith("📘 راهنما و دستیار")' not in src
    assert '_sk("home", row.field)' in src
