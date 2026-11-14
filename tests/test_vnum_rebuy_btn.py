from tenant.bots.virtual_service import ozvinoo_vnum as Z


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]


def test_rebuy_only_for_ozvinoo():
    assert "vnum:ozrb:7" in _cbs(Z._code_kb(7, "123", logout=True))
    assert "vnum:ozrb:7" not in _cbs(Z._code_kb(7, "123", logout=False))
