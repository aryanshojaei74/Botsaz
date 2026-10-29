from datetime import datetime
import importlib.util, pathlib
p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "halloween_until.py"
spec = importlib.util.spec_from_file_location("hu", p); hu = importlib.util.module_from_spec(spec); spec.loader.exec_module(hu)


def test_today_1500_tehran_is_1130_utc():
    assert hu.until_utc("15:00", datetime(2026, 10, 29, 5, 0)) == datetime(2026, 10, 29, 11, 30)


def test_past_time_rolls_to_tomorrow():
    assert hu.until_utc("15:00", datetime(2026, 10, 29, 12, 0)) == datetime(2026, 10, 30, 11, 30)
