"""PATCH_ESCROW_BOT_KIND_V1 — «ربات واسطه‌گری» به‌عنوانِ نوعِ ربات مستقل؛ رباتِ خدماتِ مجازی دست‌نخورده."""
import platform_bot as PB
import tenant
from plans import Feature, FEATURE_BOT_TYPES, has_feature
from tenant.settings_registry import registered_items


def test_kind_is_listed_and_active_in_platform_bot():
    assert "escrow_bot" in PB.FIELDS and "escrow_bot" in PB.ACTIVE_FIELDS
    assert PB.FIELD_PITCH["escrow_bot"] and "واسطه" in PB.FIELDS["escrow_bot"]
    cbs = [b.callback_data for r in PB._action_kb_base("escrow_bot").inline_keyboard for b in r]
    assert "act:escrow" in cbs and "act:wallet" in cbs and "act:buy_config" not in cbs
    assert PB.ACTION_TITLE["escrow"]


def test_escrow_feature_for_both_kinds_and_pro_growth_only():
    assert FEATURE_BOT_TYPES[Feature.ESCROW] == frozenset({"virtual_service", "escrow_bot"})
    assert has_feature("pro", Feature.ESCROW) and has_feature("growth", Feature.ESCROW) and not has_feature("free", Feature.ESCROW)


def test_virtual_service_escrow_entry_is_unchanged():
    vs = [i.callback for i in registered_items("virtual_service")]
    assert "set:escrow" in vs
    eb = registered_items("escrow_bot")
    assert [i.callback for i in eb] == ["set:escrow"]
    assert tenant._SIMPLE_ACTIONS["escrow"][2] == frozenset({"virtual_service", "escrow_bot"})


def test_known_field_and_stats_name():
    from tenant.shared.field_pick import needs_field
    import platform_stats_share as SS
    assert needs_field("escrow_bot") is False and SS._FIELD_FA["escrow_bot"]


def test_escrow_gate_source_allows_both_kinds():
    import inspect
    from tenant.shared import escrow as E
    src = inspect.getsource(E)
    assert '("virtual_service", "escrow_bot")' in src
