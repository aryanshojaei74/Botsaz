"""سناریوی سوءاستفاده: رشدِ ۳ ماهه ← ارتقا به حرفه‌ای ۱ ماهه."""
import datetime as d
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plan_bank import compute_purchase as cp  # noqa: E402

NOW = d.datetime(2026, 9, 30, 12, 0)


def _buy(**kw):
    base = dict(was_archived=False, old_sub_months=1, banked_plan=None,
                banked_seconds=None, banked_months=None, months=1, now=NOW)
    base.update(kw)
    return cp(**base)


def test_growth_to_pro_only_gives_purchased_month_and_banks_rest():
    r = _buy(old_plan="growth", new_plan="pro", old_expires=NOW + d.timedelta(days=90), old_sub_months=3)
    assert r["expires"] == NOW + d.timedelta(days=30)         # نه ۱۲۰ روز
    assert r["banked_plan"] == "growth" and r["banked_seconds"] == 90 * 86400


def test_renewing_pro_keeps_bank_untouched():
    r = _buy(old_plan="pro", new_plan="pro", old_expires=NOW + d.timedelta(days=20),
             banked_plan="growth", banked_seconds=90 * 86400, banked_months=3)
    assert r["expires"] == NOW + d.timedelta(days=50)
    assert r["banked_seconds"] == 90 * 86400
