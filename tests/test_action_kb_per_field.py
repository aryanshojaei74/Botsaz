"""PATCH_ACTION_KB_NO_VPN_LEAK_V1 — مرحلهٔ «عملکردِ دکمه‌ها» در ساختِ ربات: هر نوعِ ربات فقط عملکردهایِ خودش را ببیند (قبلاً رزرو/مدیریتِ گروه/… لیستِ VPN می‌گرفتند)."""
import platform_bot as pb
import tenant

_SPECIAL = {"skip", "submenu", "web_app"}
_VPN = {"buy_config", "buy_vpn", "my_services", "free_trial", "vpn_help", "buy_vpn_sanai", "buy_vpn_pasargad", "buy_vpn_pasarguard",
        "buy_vpn_marzban", "buy_vpn_marzneshin", "buy_vpn_hiddify", "buy_vpn_rebecca", "buy_vpn_alireza", "buy_vpn_upanel_mizban",
        "buy_vpn_shahan"}


def _acts(field):
    kb = pb._action_kb_base(field)
    return [b.callback_data.split(":", 1)[1] for r in kb.inline_keyboard for b in r if (b.callback_data or "").startswith("act:")]


def test_only_vpn_sales_gets_vpn_actions():
    for f in pb.FIELDS:
        got = set(_acts(f))
        if f == "vpn_sales":
            assert _VPN & got, "فروش VPN باید عملکردهایِ VPN داشته باشد"
        else:
            assert not (_VPN & got), f"{f}: عملکردِ VPN نشت کرده: {sorted(_VPN & got)}"


def test_booking_has_its_own_actions_only():
    got = _acts("booking")
    assert "book_show" in got and "book_my" in got and "wallet" in got and "ticket" in got
    assert not [a for a in got if a.startswith(("vs_", "psel", "giveaway", "escrow", "funstat", "pyar", "pnr", "buy", "smm", "vnum"))]


def test_wizard_actions_are_allowed_for_that_field_in_the_runtime_menu():
    """هر عملکردی که ویزارد نشان می‌دهد و ربات هم می‌شناسد باید برایِ همان نوع در منویِ مالک مجاز باشد (دو لیست قاطی نشوند)."""
    for f in pb.FIELDS:
        for a in _acts(f):
            if a in tenant._SIMPLE_ACTIONS:
                allowed = tenant._SIMPLE_ACTIONS[a][2]
                assert allowed is None or f in allowed, f"{f}: «{a}» فقط برایِ {sorted(allowed)} مجاز است"


def test_every_wizard_action_has_a_title_and_unknown_or_legacy_fields_are_safe():
    for f in pb.FIELDS:
        for a in _acts(f):
            assert a in _SPECIAL or a in pb.ACTION_TITLE, f"{f}: «{a}» عنوان ندارد"
    assert _VPN & set(_acts("vpn_sales"))
    assert _VPN & set(_acts(None)) and _VPN & set(_acts(""))            # رباتِ قدیمیِ بدونِ زمینه ⇒ رفتارِ قبلی
    assert not (_VPN & set(_acts("some_future_kind")))                 # زمینهٔ تازهٔ بدونِ شاخه ⇒ عمومی، نه VPN
    assert "profile" in _acts("some_future_kind")
