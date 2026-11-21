from tenant import _trial_fix_view


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_reset_button_shown_when_button_has_explicit_values():
    cfg = {"trial_mode": "auto", "trial_gb": "0.25", "trial_days": "3"}
    _, kb = _trial_fix_view(cfg, 7)
    assert "thb:trglobal:7" in _cbs(kb)


def test_reset_button_hidden_when_everything_from_global():
    cfg = {"trial_mode": "auto", "trial_gb": "0.3", "trial_days": "3", "_from_global": ["gb", "duration"]}
    _, kb = _trial_fix_view(cfg, 7)
    assert "thb:trglobal:7" not in _cbs(kb)


def test_top_menu_handler_registered():
    import tenant
    assert callable(tenant.thb_trial_use_global_top) and callable(tenant._trial_top_render)
