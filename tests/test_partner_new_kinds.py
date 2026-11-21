import asyncio
from tenant.partner_bot import partner_build as pbld


def test_new_kinds_listed():
    keys = {k for k, _ in pbld.FIELDS}
    assert {"giveaway", "escrow_bot", "panel_seller"} <= keys
    assert {"giveaway", "escrow_bot", "panel_seller"} <= pbld.ACTIVE_FIELDS


def test_labels_match_main_platform():
    import platform_bot as pb
    for k in ("giveaway", "escrow_bot"):
        assert pbld.FIELD_LABEL[k] == pb.FIELDS[k]
