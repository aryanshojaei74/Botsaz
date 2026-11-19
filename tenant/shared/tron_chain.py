"""tron_chain.py — کلیدِ خصوصی، آدرس، امضا و ارتباط با TronGrid برایِ ارسالِ خودکارِ TRX و USDT-TRC20 (بدونِ وابستگیِ تازه).

=== PATCH_TRON_AUTOSEND_V1 ===
• secp256k1 + Keccak-256 + Base58Check به‌صورتِ خالص در پایتون (فقط hashlib/hmac/secrets)؛ روی سرورِ فعلی چیزِ تازه‌ای نصب نمی‌شود.
• امضا: ECDSA با nonceِ قطعیِ RFC 6979 و low-s؛ خروجی r‖s‖v (v = ۰/۱، همان قالبِ java-tron).
• تراکنش را TronGrid می‌سازد (createtransaction / triggersmartcontract)، ولی *قبل از امضا* محتوایش (مقصد، مبلغ، قرارداد، txID=sha256(raw_data_hex))
  با آنچه خودمان خواسته‌ایم تطبیق داده می‌شود؛ اگر یک بایت فرق داشته باشد امضا نمی‌شود.
• خطایِ «قبل از انتشار» با «ممکن است منتشر شده باشد» جدا برگردانده می‌شود (مثلِ ton_autosend): خطایِ شبکه بعد از broadcast = نامشخص.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# ───────────── Keccak-256 (نسخهٔ اصلیِ Keccak با padding=0x01؛ همان که اتریوم/ترون استفاده می‌کنند، نه SHA3) ─────────────
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
       0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
       0x000000000000800A, 0x800000008000000A, 0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _keccak_f(a: list) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % rate:
        msg.append(0)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out[:32]


# ───────────── secp256k1 ─────────────
_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        lam = (3 * p[0] * p[0]) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, pt):
    r, add = None, pt
    while k:
        if k & 1:
            r = _add(r, add)
        add = _add(add, add)
        k >>= 1
    return r


def new_private_key() -> int:
    while True:
        k = secrets.randbelow(_N)
        if 1 <= k < _N:
            return k


def pubkey_xy(priv: int) -> tuple[int, int]:
    if not 1 <= priv < _N:
        raise ValueError("invalid private key")
    return _mul(priv, _G)


def _rfc6979_k(priv: int, h: bytes) -> int:
    x = priv.to_bytes(32, "big")
    h1 = (int.from_bytes(h, "big") % _N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < _N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_hash(priv: int, h: bytes) -> bytes:
    """امضایِ ۳۲بایتیِ هش (txID) ⇒ r(32) ‖ s(32) ‖ v(1: ۰ یا ۱)."""
    if len(h) != 32:
        raise ValueError("hash must be 32 bytes")
    z = int.from_bytes(h, "big")
    k = _rfc6979_k(priv, h)
    while True:
        rx, ry = _mul(k, _G)
        r = rx % _N
        s = pow(k, -1, _N) * (z + r * priv) % _N
        if r and s:
            break
        k = (k + 1) % _N or 1
    v = (ry & 1) | (2 if rx >= _N else 0)
    if s > _N // 2:
        s, v = _N - s, v ^ 1
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v & 1])


def recover_pubkey(h: bytes, sig: bytes) -> tuple[int, int]:
    """بازیابیِ کلیدِ عمومی از (هش، امضا) — برایِ تستِ درستیِ v و برایِ اطمینان قبل از انتشار."""
    r, s, v = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"), sig[64] & 1
    x = r
    y2 = (pow(x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if (y * y) % _P != y2:
        raise ValueError("bad signature")
    if (y & 1) != v:
        y = _P - y
    z = int.from_bytes(h, "big")
    rinv = pow(r, -1, _N)
    p1 = _mul(s * rinv % _N, (x, y))
    p2 = _mul((-z * rinv) % _N, _G)
    q = _add(p1, p2)
    if q is None:
        raise ValueError("bad signature")
    return q


# ───────────── Base58Check و آدرس ─────────────
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check_encode(payload: bytes) -> str:
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def b58check_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("invalid base58")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    if len(raw) < 5 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        raise ValueError("bad checksum")
    return raw[:-4]


def address_from_private(priv: int) -> str:
    x, y = pubkey_xy(priv)
    h = keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))
    return b58check_encode(b"\x41" + h[-20:])


def address_from_pubkey(xy: tuple[int, int]) -> str:
    h = keccak256(xy[0].to_bytes(32, "big") + xy[1].to_bytes(32, "big"))
    return b58check_encode(b"\x41" + h[-20:])


def is_valid_address(addr: str) -> bool:
    try:
        raw = b58check_decode((addr or "").strip())
    except ValueError:
        return False
    return len(raw) == 21 and raw[0] == 0x41


def addr_hex(addr: str) -> str:
    """آدرسِ base58 ⇒ ۴۲ نویسهٔ hex (۴۱‖۲۰بایت)."""
    raw = b58check_decode(addr)
    if len(raw) != 21 or raw[0] != 0x41:
        raise ValueError("not a Tron address")
    return raw.hex()


# ───────────── TronGrid ─────────────
BASE = "https://api.trongrid.io"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SUN = 1_000_000
FEE_LIMIT_SUN = 35 * SUN
TIMEOUT_S = 12


class TronError(Exception):
    """خطایِ قطعیِ «قبل از انتشار» (برایِ این شبکه هیچ‌چیز منتشر نشده)."""


class TronAmbiguous(Exception):
    """خطایِ شبکه/تایم‌اوت بعد از ارسالِ درخواستِ انتشار — ممکن است تراکنش رفته باشد."""


async def _call(path: str, payload: dict | None, api_key: str = "", *, method: str = "POST") -> dict:
    import aiohttp
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    t = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=t) as s:
        async with s.request(method, BASE + path, json=payload, headers=headers) as r:
            txt = await r.text()
            if r.status >= 400:
                raise TronError(f"TronGrid HTTP {r.status}")
            try:
                return json.loads(txt) if txt else {}
            except ValueError:
                raise TronError("TronGrid response is not JSON")


def _pad32(hex_str: str) -> str:
    return hex_str.rjust(64, "0")


async def trx_balance(addr: str, api_key: str = "") -> int:
    """موجودیِ TRX به sun؛ حسابِ فعال‌نشده ⇒ ۰."""
    d = await _call("/wallet/getaccount", {"address": addr, "visible": True}, api_key)
    return int(d.get("balance") or 0)


async def usdt_balance(addr: str, api_key: str = "") -> int:
    """موجودیِ USDT-TRC20 (واحدِ کوچک؛ ۶ رقم اعشار)."""
    d = await _call("/wallet/triggerconstantcontract", {
        "owner_address": addr, "contract_address": USDT_CONTRACT, "function_selector": "balanceOf(address)",
        "parameter": _pad32(addr_hex(addr)[2:]), "visible": True}, api_key)
    res = d.get("constant_result") or []
    return int(res[0], 16) if res and res[0] else 0


def _expect_trx(tx: dict, frm: str, to: str, sun: int) -> None:
    try:
        c = tx["raw_data"]["contract"][0]
        val = c["parameter"]["value"]
        if c["type"] != "TransferContract" or int(val["amount"]) != sun:
            raise TronError("tx mismatch (amount/type)")

        if not (_addr_eq(val["to_address"], to) and _addr_eq(val["owner_address"], frm)):
            raise TronError("tx mismatch (address)")
    except TronError:
        raise
    except Exception as e:      # noqa: BLE001
        raise TronError(f"tx parse failed: {type(e).__name__}")


def _addr_eq(a: str, b_base58: str) -> bool:
    a = str(a)
    try:
        want = addr_hex(b_base58).lower()
        got = a.lower() if len(a) == 42 and a.lower().startswith("41") else addr_hex(a).lower()
        return got == want
    except Exception:      # noqa: BLE001
        return False


def _expect_usdt(tx: dict, frm: str, to: str, units: int) -> None:
    try:
        c = tx["raw_data"]["contract"][0]
        val = c["parameter"]["value"]
        data = str(val.get("data") or "").lower()
        want = "a9059cbb" + _pad32(addr_hex(to)[2:]) + _pad32(format(units, "x"))
        if c["type"] != "TriggerSmartContract" or data != want:
            raise TronError("tx mismatch (call data)")
        if not (_addr_eq(val["contract_address"], USDT_CONTRACT) and _addr_eq(val["owner_address"], frm)):
            raise TronError("tx mismatch (contract/owner)")
    except TronError:
        raise
    except Exception as e:      # noqa: BLE001
        raise TronError(f"tx parse failed: {type(e).__name__}")


def _check_txid(tx: dict) -> bytes:
    raw = tx.get("raw_data_hex")
    txid = str(tx.get("txID") or "")
    if not raw or len(txid) != 64:
        raise TronError("tx without raw_data_hex/txID")
    h = hashlib.sha256(bytes.fromhex(raw)).digest()
    if h.hex() != txid.lower():
        raise TronError("txID does not match raw_data_hex")
    return h


async def prepare_transfer(coin: str, priv: int, to: str, centi: int, api_key: str = "") -> tuple[str, dict]:
    """تراکنش را می‌سازد، با خواستهٔ خودمان تطبیق می‌دهد و امضا می‌کند. خروجی: (txid, تراکنشِ امضاشده). TronError ⇒ هیچ‌چیز منتشر نشده."""
    frm = address_from_private(priv)
    if not is_valid_address(to):
        raise TronError("invalid destination address")
    if centi <= 0:
        raise TronError("invalid amount")
    if coin == "trx":
        sun = centi * (SUN // 100)
        tx = await _call("/wallet/createtransaction", {"owner_address": frm, "to_address": to, "amount": sun, "visible": True}, api_key)
        if "Error" in tx or "raw_data" not in tx:
            raise TronError("createtransaction: " + str(tx.get("Error") or "no transaction")[:120])
        _expect_trx(tx, frm, to, sun)
    elif coin == "usdt":
        units = centi * 10_000                  # صدم‌واحد ⇒ ۶ رقم اعشارِ USDT
        d = await _call("/wallet/triggersmartcontract", {
            "owner_address": frm, "contract_address": USDT_CONTRACT, "function_selector": "transfer(address,uint256)",
            "parameter": _pad32(addr_hex(to)[2:]) + _pad32(format(units, "x")), "fee_limit": FEE_LIMIT_SUN, "call_value": 0,
            "visible": True}, api_key)
        tx = d.get("transaction") or {}
        if not (d.get("result") or {}).get("result") or "raw_data" not in tx:
            raise TronError("triggersmartcontract: " + str(d.get("result") or d)[:120])
        _expect_usdt(tx, frm, to, units)
    else:
        raise TronError("unsupported coin")
    h = _check_txid(tx)
    sig = sign_hash(priv, h)
    if address_from_pubkey(recover_pubkey(h, sig)) != frm:          # دفاعِ نهایی: امضا باید به همین کلید برگردد
        raise TronError("signature self-check failed")
    signed = dict(tx)
    signed["signature"] = [sig.hex()]
    return str(tx["txID"]).lower(), signed


async def broadcast(signed: dict, api_key: str = "") -> tuple[bool, str]:
    """(پذیرفته‌شد?, پیام). False فقط وقتی گره *صراحتاً* ردش کرده؛ خطایِ شبکه ⇒ TronAmbiguous."""
    try:
        d = await _call("/wallet/broadcasttransaction", signed, api_key)
    except TronError as e:
        # پاسخِ HTTP خطا = گره قبول نکرده؛ ولی «تایم‌اوت/قطعی» (زیر) نامشخص است
        return False, str(e)
    except Exception as e:      # noqa: BLE001
        raise TronAmbiguous(f"{type(e).__name__}: {e}")
    if d.get("result") is True:
        return True, "broadcast"
    code = str(d.get("code") or "")
    if code == "DUP_TRANSACTION_ERROR":
        return True, "duplicate (already known)"
    msg = d.get("message") or ""
    try:
        msg = bytes.fromhex(msg).decode("utf-8", "replace")
    except ValueError:
        pass
    return False, f"{code} {msg}".strip()[:160]


async def tx_status(txid: str, api_key: str = "") -> str:
    """"success" | "failed" | "pending" (هنوز در بلاکی نیست/پیدا نشد)."""
    try:
        d = await _call("/wallet/gettransactioninfobyid", {"value": txid}, api_key)
    except Exception:      # noqa: BLE001
        return "pending"
    if not d or "blockNumber" not in d:
        return "pending"
    res = str((d.get("receipt") or {}).get("result") or "SUCCESS").upper()
    if str(d.get("result") or "").upper() == "FAILED" or res not in ("SUCCESS",):
        return "failed"
    return "success"


async def wait_confirmed(txid: str, api_key: str = "", timeout_s: float = 60.0, poll_s: float = 3.0) -> str:
    end = time.monotonic() + timeout_s
    while True:
        st = await tx_status(txid, api_key)
        if st != "pending" or time.monotonic() >= end:
            return st
        await asyncio.sleep(poll_s)
