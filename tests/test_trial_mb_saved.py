import re, pathlib

SRC = {p: pathlib.Path(p).read_text() for p in ("tenant/__init__.py", "tenant/shared/vpn_api.py")}


def test_every_free_trial_account_saves_mb():
    """هر جایی که PanelAccount «تست رایگان» ساخته می‌شود باید volume_mb (حجمِ زیرِ ۱ گیگ) را هم ذخیره کند."""
    for path, text in SRC.items():
        for m in re.finditer(r'plan_title="تست رایگان"', text):
            window = text[max(0, m.start() - 900): m.start() + 200]
            assert "volume_mb=" in window, f"{path}: volume_mb missing near offset {m.start()}"
