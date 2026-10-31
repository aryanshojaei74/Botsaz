"""PATCH_GATEWAY_PAIR_ROWS_V1 — دو درگاه کنارِ هم در یک ردیف (درخواستِ Nexivo)."""
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import tenant.gateways.gateway_layout as GL


def _b(cb, text=None):
    return B(text=text or cb, callback_data=cb)


def _shape(rows):
    return [[b.callback_data for b in r] for r in rows]


def test_pair_merges_adjacent_gateway_with_previous():
    rows = [[_b("w:charge")], [_b("w:zibal")], [_b("w:zarinpal")], [_b("mbtn:home")]]
    out = GL.pair_rows(rows, {"zibal": True})
    assert _shape(out) == [["w:charge", "w:zibal"], ["w:zarinpal"], ["mbtn:home"]]


def test_max_two_per_row_and_chain_of_flags():
    rows = [[_b("w:charge")], [_b("w:zibal")], [_b("w:zarinpal")], [_b("w:apay")]]
    out = GL.pair_rows(rows, {"zibal": True, "zarinpal": True, "apay": True})
    # زیبال به کارت چسبید؛ زرین‌پال نباید سومی شود ⇒ ردیفِ جدید؛ آپی به زرین‌پال می‌چسبد
    assert _shape(out) == [["w:charge", "w:zibal"], ["w:zarinpal", "w:apay"]]


def test_non_gateway_row_breaks_chain_and_first_has_no_previous():
    rows = [[_b("w:zibal")], [_b("mbtn:home")], [_b("w:zarinpal")]]
    out = GL.pair_rows(rows, {"zibal": True, "zarinpal": True})
    assert _shape(out) == [["w:zibal"], ["mbtn:home"], ["w:zarinpal"]]


def test_unflagged_and_multi_button_rows_untouched():
    rows = [[_b("w:charge"), _b("w:zibal")], [_b("w:zarinpal")]]
    assert _shape(GL.pair_rows(rows, {"zarinpal": True})) == _shape(rows)        # ردیفِ قبلی دوتایی است ⇒ نمی‌چسبد
    rows2 = [[_b("w:charge")], [_b("w:zibal")]]
    assert _shape(GL.pair_rows(rows2, {})) == _shape(rows2)


def test_buttons_keep_their_text_and_style():
    z = B(text="زیبال", callback_data="w:zibal")
    out = GL.pair_rows([[_b("w:charge", "کارت")], [z]], {"zibal": True})
    assert [b.text for b in out[0]] == ["کارت", "زیبال"]


import pytest


@pytest.mark.asyncio
async def test_apply_pairs_uses_owner_settings(monkeypatch):
    store = {(1, "gw_pair_zibal"): "1"}
    async def _get(bid, k, d=""): return store.get((bid, k), d)
    monkeypatch.setattr(GL, "_get", _get)
    mk = M(inline_keyboard=[[_b("w:charge")], [_b("w:zibal")], [_b("mbtn:home")]])
    out = await GL.apply_pairs(1, mk)
    assert _shape(out.inline_keyboard) == [["w:charge", "w:zibal"], ["mbtn:home"]]
    assert await GL.apply_pairs(2, mk) is mk                                      # ربات دیگر: بدونِ تغییر
