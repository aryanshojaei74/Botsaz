"""تست‌های واحد برای tenant/crypto_pay.py::verify_ipn_signature — پایه‌ی امنیتی
همهٔ وبهوک‌های پرداختِ ارز دیجیتال (main.py:/nowpay/ipn). هرگونه رگرسیون اینجا
یعنی امکان جعل تأیید پرداخت."""
import hashlib
import hmac
import json

from tenant.gateways.crypto_pay import verify_ipn_signature

SECRET = "test-secret-123"
PAYLOAD = {"order_id": "ord1", "payment_status": "finished", "amount": 1000}


def _valid_sig(payload: dict, secret: str) -> str:
    sorted_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), sorted_str.encode("utf-8"),
                     hashlib.sha512).hexdigest()


def _raw(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_valid_signature_accepted():
    sig = _valid_sig(PAYLOAD, SECRET)
    assert verify_ipn_signature(_raw(PAYLOAD), sig, SECRET) is True


def test_wrong_secret_rejected():
    sig = _valid_sig(PAYLOAD, "different-secret")
    assert verify_ipn_signature(_raw(PAYLOAD), sig, SECRET) is False


def test_tampered_payload_rejected():
    sig = _valid_sig(PAYLOAD, SECRET)
    tampered = dict(PAYLOAD, amount=999999)
    assert verify_ipn_signature(_raw(tampered), sig, SECRET) is False


def test_empty_signature_rejected():
    assert verify_ipn_signature(_raw(PAYLOAD), "", SECRET) is False


def test_empty_secret_rejected():
    sig = _valid_sig(PAYLOAD, SECRET)
    assert verify_ipn_signature(_raw(PAYLOAD), sig, "") is False


def test_malformed_signature_rejected():
    assert verify_ipn_signature(_raw(PAYLOAD), "not-a-real-hex-sig", SECRET) is False


def test_malformed_json_body_rejected():
    assert verify_ipn_signature(b"{not json", "anything", SECRET) is False
