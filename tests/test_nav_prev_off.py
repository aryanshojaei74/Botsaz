from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M
import tenant


def _mk():
    return M(inline_keyboard=[[B(text="x", callback_data="a:1")]])


def _cbs(mk):
    return [b.callback_data for r in mk.inline_keyboard for b in r]


def test_prev_button_injected_by_default():
    assert "nav:back" in _cbs(tenant._nav_inject(_mk(), True, False))


def test_prev_button_suppressed_keeps_home():
    c = _cbs(tenant._nav_inject(_mk(), True, False, True))
    assert "nav:back" not in c and "mbtn:home" in c
