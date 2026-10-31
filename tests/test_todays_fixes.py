"""تست‌های رگرسیون برای فیکس‌های امروز — بخش‌هایی که منطقِ خالص (بدونِ
شبکه/دیتابیسِ واقعی) دارند، تا در آینده اگه کسی این توابع رو تغییر داد،
بدونِ نیاز به تستِ دستیِ زنده، خرابی زودتر معلوم بشه.
"""
import pytest

from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient
from main import _pg_conn_params_from_url
from tenant.gateways.auto_receipt import _parse_autonomous_response


# ─────────────────── حذفِ مسیرِ داشبورد از base_url (باگِ پنلِ ربکا) ───────────────────

@pytest.mark.parametrize("raw,expected", [
    ("https://panel.lornanet.ir/dashboard/lorna", "https://panel.lornanet.ir"),
    ("https://host/dashboard", "https://host"),
    ("https://host/dashboard/", "https://host"),
    ("https://host:8000/dashboard/xK9fQ2", "https://host:8000"),
    ("https://host.com", "https://host.com"),
    ("https://host.com/api", "https://host.com/api"),  # نباید دست بخوره
])
def test_dashboard_path_stripped(raw, expected):
    c = MarzbanPanelClient(raw, "u", "p")
    assert c.base_url == expected


# ─────────────────── پارسِ DATABASE_URL برای pg_dump ───────────────────

def test_pg_conn_params_basic():
    p = _pg_conn_params_from_url("postgresql://user:pass@localhost/botsaz")
    assert p == {"host": "localhost", "port": "5432", "user": "user",
                "password": "pass", "dbname": "botsaz"}


def test_pg_conn_params_asyncpg_driver_and_encoded_password():
    p = _pg_conn_params_from_url(
        "postgresql+asyncpg://botsaz_user:S3cr3t%40pass@127.0.0.1:5432/botsaz")
    assert p["host"] == "127.0.0.1"
    assert p["user"] == "botsaz_user"
    assert p["password"] == "S3cr3t@pass"  # باید decode شده باشه
    assert p["dbname"] == "botsaz"


# ─────────────────── پارسِ پاسخِ هوش مصنوعیِ تاییدِ رسید ───────────────────

def test_parse_autonomous_response_approved():
    text = (
        "مبلغ_عدد: 90000\n"
        "مبلغ_واحد: تومان\n"
        "کارت_مقصد: 1234\n"
        "شبا_مقصد: \n"
        "حساب_مقصد: \n"
        "کارت_مبدأ: 5678\n"
        "شبا_مبدأ: \n"
        "حساب_مبدأ: \n"
        "تاریخ: 1404/04/19\n"
        "ساعت: 14:20\n"
        "شماره_پیگیری: 998877\n"
        "تصمیم: تایید\n"
        "دلیل: همه موارد مطابقت داشت\n"
    )
    check = _parse_autonomous_response(text, expected_sender_last4="5678",
                                       expected_card_last4="1234")
    assert check.ok is True


def test_parse_autonomous_response_rejected():
    text = (
        "مبلغ_عدد: 50000\n"
        "مبلغ_واحد: تومان\n"
        "تصمیم: ارجاع\n"
        "دلیل: مبلغ مطابقت نداشت\n"
    )
    check = _parse_autonomous_response(text, expected_sender_last4=None,
                                       expected_card_last4=None)
    assert check.ok is False
    assert "مبلغ" in check.reason
