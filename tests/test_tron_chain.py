"""PATCH_TRON_AUTOSEND_V1 — رمزنگاریِ ترون (Keccak/secp256k1/Base58Check/امضا) با نمونه‌هایِ شناخته‌شده + ساختِ تراکنشِ امن با TronGrid ساختگی."""
import hashlib

import pytest

from tenant.shared import tron_chain as T


def test_keccak256_known_vectors_and_matches_pycryptodome_when_available():
    assert T.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert T.keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    try:
        from Cryptodome.Hash import keccak
    except ImportError:
        return
    for data in (b"a" * 135, b"a" * 136, b"a" * 137, bytes(range(256)) * 3):          # مرزِ بلوک (rate=136)
        k = keccak.new(digest_bits=256)
        k.update(data)
        assert T.keccak256(data).hex() == k.hexdigest()


def test_address_derivation_known_vector():
    # کلیدِ خصوصیِ ۱ ⇒ آدرسِ اتریومِ 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf ⇒ همان ۲۰بایت با پیشوندِ ۴۱ در ترون
    assert T.address_from_private(1) == "TMVQGm1qAQYVdetCeGRRkTWYYrLXuHK2HC"
    x, y = T.pubkey_xy(1)
    assert T.keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:].hex() == "7e5f4552091a69125d5dfcb7b8c2659029395bdf"


def test_base58check_and_address_validation():
    usdt = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    assert T.is_valid_address(usdt) and T.addr_hex(usdt) == "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert T.b58check_encode(T.b58check_decode(usdt)) == usdt
    assert not T.is_valid_address(usdt[:-1] + "s") and not T.is_valid_address("T123") and not T.is_valid_address("")
    assert not T.is_valid_address("0x" + "ab" * 20)


@pytest.mark.parametrize("priv", [1, 2, 0x1234567890ABCDEF, T._N - 1, int.from_bytes(hashlib.sha256(b"x").digest(), "big") % T._N])
def test_signature_recovers_to_the_signer_and_verifies_independently(priv):
    h = hashlib.sha256(b"tx-" + str(priv).encode()).digest()
    sig = T.sign_hash(priv, h)
    assert len(sig) == 65 and sig[64] in (0, 1)
    assert T.address_from_pubkey(T.recover_pubkey(h, sig)) == T.address_from_private(priv)           # v درست است
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big")
    assert 0 < s <= T._N // 2                                                                       # low-s
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, encode_dss_signature
    x, y = T.pubkey_xy(priv)
    pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256K1()).public_key()
    pub.verify(encode_dss_signature(r, s), h, ec.ECDSA(Prehashed(hashes.SHA256())))                 # کتابخانهٔ مستقل هم تأیید می‌کند


def test_signing_is_deterministic_rfc6979_and_rejects_bad_input():
    h = hashlib.sha256(b"m").digest()
    assert T.sign_hash(7, h) == T.sign_hash(7, h)
    with pytest.raises(ValueError):
        T.sign_hash(7, b"short")
    with pytest.raises(ValueError):
        T.pubkey_xy(0)
    with pytest.raises(ValueError):
        T.pubkey_xy(T._N)


def test_new_private_keys_are_unique_and_in_range():
    keys = {T.new_private_key() for _ in range(20)}
    assert len(keys) == 20 and all(1 <= k < T._N for k in keys)


# ───────────── ساختِ تراکنش با تطبیق ─────────────
PRIV = 0xA1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F90 % T._N
FRM = T.address_from_private(PRIV)
TO = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"


def _trx_tx(to=TO, amount=12_340_000, frm=FRM, raw=b"\x0a\x02\x01\x02" + b"x" * 40):
    return {"txID": hashlib.sha256(raw).hexdigest(), "raw_data_hex": raw.hex(),
            "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
                "amount": amount, "owner_address": frm, "to_address": to}}}]}}


def _usdt_tx(to=TO, units=12_340_000, frm=FRM, raw=b"\x0a\x02\x03\x04" + b"y" * 60, data=None):
    data = data or ("a9059cbb" + T._pad32(T.addr_hex(to)[2:]) + T._pad32(format(units, "x")))
    return {"result": {"result": True}, "transaction": {
        "txID": hashlib.sha256(raw).hexdigest(), "raw_data_hex": raw.hex(),
        "raw_data": {"contract": [{"type": "TriggerSmartContract", "parameter": {"value": {
            "data": data, "owner_address": frm, "contract_address": T.USDT_CONTRACT}}}]}}}


async def _prepare(monkeypatch, coin, response, centi=1234, to=TO):
    calls = []

    async def fake(path, payload, api_key="", **k):
        calls.append((path, payload))
        return response
    monkeypatch.setattr(T, "_call", fake)
    return await T.prepare_transfer(coin, PRIV, to, centi, "key"), calls


@pytest.mark.asyncio
async def test_trx_transfer_is_built_checked_and_signed(monkeypatch):
    (txid, signed), calls = await _prepare(monkeypatch, "trx", _trx_tx())
    assert calls[0][0] == "/wallet/createtransaction" and calls[0][1]["amount"] == 12_340_000 and calls[0][1]["to_address"] == TO
    h = bytes.fromhex(txid)
    assert len(signed["signature"]) == 1
    sig = bytes.fromhex(signed["signature"][0])
    assert T.address_from_pubkey(T.recover_pubkey(h, sig)) == FRM                     # امضا واقعاً از کلیدِ ما
    assert signed["txID"] == txid


@pytest.mark.asyncio
async def test_usdt_transfer_is_built_checked_and_signed(monkeypatch):
    (txid, signed), calls = await _prepare(monkeypatch, "usdt", _usdt_tx())
    path, payload = calls[0]
    assert path == "/wallet/triggersmartcontract" and payload["function_selector"] == "transfer(address,uint256)"
    assert payload["contract_address"] == T.USDT_CONTRACT and payload["fee_limit"] == T.FEE_LIMIT_SUN
    assert payload["parameter"].endswith(format(12_340_000, "x").rjust(64, "0"))
    assert T.address_from_pubkey(T.recover_pubkey(bytes.fromhex(txid), bytes.fromhex(signed["signature"][0]))) == FRM


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    _trx_tx(amount=99_000_000),                                                      # مبلغِ دیگر
    _trx_tx(to="TMVQGm1qAQYVdetCeGRRkTWYYrLXuHK2HC"),                                # مقصدِ دیگر
    _trx_tx(frm="TMVQGm1qAQYVdetCeGRRkTWYYrLXuHK2HC"),                               # فرستندهٔ دیگر
    {**_trx_tx(), "txID": "00" * 32},                                                # txID با raw_data_hex نمی‌خواند
    {**_trx_tx(), "raw_data_hex": ""},
    {"Error": "this account does not exist"},
])
async def test_tampered_or_wrong_trx_transaction_is_never_signed(monkeypatch, bad):
    with pytest.raises(T.TronError):
        await _prepare(monkeypatch, "trx", bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    _usdt_tx(units=99_000_000),                                                      # مبلغِ دیگر داخلِ data
    _usdt_tx(data="a9059cbb" + T._pad32(T.addr_hex("TMVQGm1qAQYVdetCeGRRkTWYYrLXuHK2HC")[2:]) + T._pad32(format(12_340_000, "x"))),   # مقصدِ دیگر
    _usdt_tx(data="095ea7b3" + "0" * 128),                                           # تابعِ دیگر (approve)
    {"result": {"result": False}, "transaction": {}},
])
async def test_tampered_or_wrong_usdt_transaction_is_never_signed(monkeypatch, bad):
    with pytest.raises(T.TronError):
        await _prepare(monkeypatch, "usdt", bad)


@pytest.mark.asyncio
async def test_invalid_destination_or_amount_is_refused_before_any_network_call(monkeypatch):
    called = []

    async def fake(*a, **k): called.append(1)
    monkeypatch.setattr(T, "_call", fake)
    for to, centi in (("T123", 100), (TO, 0), (TO, -5), ("0x" + "ab" * 20, 100)):
        with pytest.raises(T.TronError):
            await T.prepare_transfer("trx", PRIV, to, centi)
    with pytest.raises(T.TronError):
        await T.prepare_transfer("btc", PRIV, TO, 100)
    assert called == []


@pytest.mark.asyncio
async def test_broadcast_result_mapping(monkeypatch):
    async def ok(*a, **k): return {"result": True, "txid": "ab"}

    async def dup(*a, **k): return {"result": False, "code": "DUP_TRANSACTION_ERROR"}

    async def rejected(*a, **k): return {"result": False, "code": "SIGERROR", "message": "5369676e6174757265206572726f72"}
    monkeypatch.setattr(T, "_call", ok)
    assert (await T.broadcast({}))[0] is True
    monkeypatch.setattr(T, "_call", dup)
    assert (await T.broadcast({}))[0] is True                                         # دوباره‌فرستادنِ همان تراکنش = قبلاً رفته
    monkeypatch.setattr(T, "_call", rejected)
    okk, note = await T.broadcast({})
    assert okk is False and "SIGERROR" in note and "Signature error" in note

    async def boom(*a, **k): raise TimeoutError("slow")
    monkeypatch.setattr(T, "_call", boom)
    with pytest.raises(T.TronAmbiguous):                                              # تایم‌اوت = نامشخص، نه «نرفته»
        await T.broadcast({})

    async def http_err(*a, **k): raise T.TronError("TronGrid HTTP 400")
    monkeypatch.setattr(T, "_call", http_err)
    assert (await T.broadcast({}))[0] is False


@pytest.mark.asyncio
async def test_tx_status_mapping(monkeypatch):
    async def make(resp):
        async def f(*a, **k): return resp
        monkeypatch.setattr(T, "_call", f)
        return await T.tx_status("ab" * 32)
    assert await make({}) == "pending"
    assert await make({"blockNumber": 5, "receipt": {"result": "SUCCESS"}}) == "success"
    assert await make({"blockNumber": 5}) == "success"                                # انتقالِ TRX: receipt خالی
    assert await make({"blockNumber": 5, "receipt": {"result": "OUT_OF_ENERGY"}}) == "failed"
    assert await make({"blockNumber": 5, "result": "FAILED"}) == "failed"

    async def err(*a, **k): raise TimeoutError
    monkeypatch.setattr(T, "_call", err)
    assert await T.tx_status("ab" * 32) == "pending"


@pytest.mark.asyncio
async def test_balances_parse(monkeypatch):
    async def fake(path, payload, api_key="", **k):
        if path.endswith("getaccount"):
            return {"balance": 12_500_000}
        return {"constant_result": [format(34_500_000, "x").rjust(64, "0")]}
    monkeypatch.setattr(T, "_call", fake)
    assert await T.trx_balance(TO) == 12_500_000 and await T.usdt_balance(TO) == 34_500_000

    async def empty(*a, **k): return {}
    monkeypatch.setattr(T, "_call", empty)
    assert await T.trx_balance(TO) == 0 and await T.usdt_balance(TO) == 0               # حسابِ فعال‌نشده
