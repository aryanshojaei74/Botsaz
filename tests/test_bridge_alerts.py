"""PATCH_BRIDGE_ALERT_QUIET_V1 — هشدارِ پل‌هایِ روبیکا/سروش فقط برایِ قطعیِ پیوسته."""
from bridge_alerts import COOLDOWN_SECS, OUTAGE_SECS, OutageGate, short_reason


def test_gate_stays_quiet_for_transient_errors_and_alerts_once_per_cooldown():
    g = OutageGate()
    t = 1000.0
    assert not g.failed(t)                         # اولین خطا
    assert not g.failed(t + 60) and not g.failed(t + OUTAGE_SECS - 1)
    g.ok()                                         # یک موفقیت ⇒ شمارنده صفر
    assert not g.failed(t + OUTAGE_SECS + 5)       # دوباره از صفر شروع شد
    t2 = t + OUTAGE_SECS + 5
    assert g.failed(t2 + OUTAGE_SECS)              # قطعیِ پیوسته ⇒ یک هشدار
    assert not g.failed(t2 + OUTAGE_SECS + 10)     # بلافاصله دوباره نه
    assert not g.failed(t2 + OUTAGE_SECS + COOLDOWN_SECS - 100)
    assert g.failed(t2 + OUTAGE_SECS + COOLDOWN_SECS + 100)


def test_short_reason_hides_html_error_pages():
    html = "HTTP 502: <!DOCTYPE html>\n<html>\n<head>\n<title>Error</title><style>x</style></head></html>"
    assert short_reason(html) == "HTTP 502 (خطای سرورِ آن سرویس)"
    assert short_reason("ClientConnectorError: Cannot connect [Connection reset by peer]").startswith("ClientConnectorError")
    assert "<" not in short_reason("<b>boom</b> text")
