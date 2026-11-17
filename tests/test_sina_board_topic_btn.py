"""سینا: متنِ مرتبِ «برترین بازیکنان»، دکمهٔ منو با درصدِ کارمزد، و موضوعِ کوییز روی کارتِ منتظرِ حریف."""
import json

import pytest

import platform_wager_race as R
import wager_rank as K
from models import PlatformWager


def _w(st, game="quiz", status="pending"):
    return PlatformWager(id=1, bot_id=0, chat_id=-1, game=game, creator_id=10, creator_name="SinaFadayi",
                         opponent_id=None, amount=1000, status=status, state=json.dumps(st, ensure_ascii=False))


def test_quiz_pending_card_shows_creator_topic():
    t, _ = R.render_race(_w({"opt": "foot"}))
    assert "موضوعِ سؤال‌ها" in t and "فوتبال" in t
    t2, _ = R.render_race(_w({"opt": "chem"}))
    assert "شیمی" in t2 and "فوتبال" not in t2


def test_non_quiz_pending_card_has_no_topic_line():
    t, _ = R.render_race(_w({}, game="speed"))
    assert "موضوعِ سؤال‌ها" not in t


def test_board_lines_are_rtl_isolated_and_two_line_per_player():
    st = [{"uid": 1, "name": "onxy 16:54", "uname": "", "games": 297, "games_amt": 0, "wins": 164, "wins_amt": 0,
           "losses": 133, "losses_amt": 0, "fees": 0},
          {"uid": 2, "name": "- SINA", "uname": "", "games": 334, "games_amt": 0, "wins": 210, "wins_amt": 0,
           "losses": 124, "losses_amt": 0, "fees": 0}]
    out = K.public_board(st, K.parse_tiers("0 5\n300 3"), "games")
    lines = out.splitlines()
    assert all(not ln or ln.startswith("\u200f") for ln in lines)            # هر خط راست‌چین
    assert "\u2068<b>onxy 16:54</b>\u2069" in out                           # نامِ لاتین/عدد ایزوله
    assert "297 بازی" in out and "164 برد" in out and "133 باخت" in out
    assert out.count("کارمزد 5٪") == 1 and out.count("کارمزد 3٪") == 1      # ۲۹۷ بازی ⇒ ۵٪ ؛ ۳۳۴ بازی ⇒ ۳٪
    assert len([ln for ln in lines if "بازی ·" in ln]) == 2                 # هر بازیکن یک خطِ آمار


def test_tiers_footer_and_empty():
    assert K.tiers_footer([]) == ""
    f = K.tiers_footer(K.parse_tiers("0 5\n20 4"), "wins")
    assert "از 0 برد" in f and "5٪" in f and "از 20 برد" in f and "4٪" in f


def test_menu_button_action_registered_and_dispatched():
    import re
    from pathlib import Path
    src = Path("tenant/__init__.py").read_text(encoding="utf-8")
    assert '"top_players": ("🏆 برترین بازیکنان"' in src
    assert 'elif action == "top_players":' in src and "board_page_text" in src
