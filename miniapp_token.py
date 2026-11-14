"""
miniapp_token.py — توکنِ امضاشده برایِ مینی‌اپ‌هایی که از دکمهٔ کیبورد
باز می‌شوند.

=== PATCH_HOSTING_KB_TOKEN_V1 ===
طبقِ مستنداتِ رسمیِ تلگرام، وب‌اپی که از یک KeyboardButton باز شود
initDataِ امضاشده دریافت نمی‌کند (خالی است). پس راهِ همیشگیِ احرازِ هویت
کار نمی‌کند و کاربر فقط «invalid_init_data» می‌بیند.

راهکار: کیبورد برایِ هر کاربر جداگانه ساخته و فرستاده می‌شود، پس همان
موقعِ ساختِ دکمه یک توکنِ امضاشدهٔ مخصوصِ همان کاربر داخلِ آدرس گذاشته
می‌شود و مینی‌اپ در نبودِ initData از آن استفاده می‌کند.

امضا با یک کلیدِ سمتِ سرور انجام می‌شود (نه توکنِ ربات) تا حتی کسی که
توکنِ رباتی را دارد نتواند به‌جایِ کاربرِ دیگری توکن بسازد.

این ماژول عمداً در ریشهٔ /opt/botsaz است تا هم platform_bot (سازنده) و
هم hosting/api.py (تأییدکننده) بتوانند از آن استفاده کنند.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import secrets
import time

MAX_AGE = 7 * 24 * 3600  # ثانیه — کیبورد مدت‌ها در چت می‌ماند

_SECRET_CACHE = ""


def server_secret() -> str:
    """کلیدِ امضا. اولویت با متغیرِ محیطی، بعد فایلِ روی دیسک."""
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    env = os.environ.get("MINIAPP_SECRET", "").strip()
    if env:
        _SECRET_CACHE = env
        return _SECRET_CACHE
    path = pathlib.Path(__file__).resolve().parent / ".miniapp_secret"
    try:
        if path.exists():
            _SECRET_CACHE = path.read_text().strip()
        if not _SECRET_CACHE:
            _SECRET_CACHE = secrets.token_urlsafe(48)
            path.write_text(_SECRET_CACHE)
            try:
                path.chmod(0o600)
            except OSError:
                pass
    except OSError:
        # اگر نوشتن ممکن نبود، کلیدِ درون‌حافظه‌ای؛ با هر ری‌استارت عوض
        # می‌شود و کاربران باید یک‌بار /start بزنند.
        _SECRET_CACHE = secrets.token_urlsafe(48)
    return _SECRET_CACHE


def make_token(tg_id: int, scope: str = "vip", issued_at: int | None = None) -> str:
    """توکنِ «tgid.timestamp.hmac»."""
    ts = int(issued_at if issued_at is not None else time.time())
    payload = f"{int(tg_id)}.{ts}"
    sig = hmac.new(server_secret().encode(),
                   f"{scope}|{payload}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def check_token(token: str, scope: str = "vip") -> int | None:
    """در صورتِ اعتبار، آی‌دیِ تلگرامِ کاربر را برمی‌گرداند.

    === PATCH_RESTART_ARG_FIX_V1 ===
    ورودیِ غیررشته‌ای هم بی‌سروصدا رد می‌شود. یک‌بار به‌خاطرِ فراخوانیِ
    داخلی که پارامتر را جا انداخته بود، آبجکتِ Header به اینجا رسید و
    کلِ درخواست با AttributeError و خطایِ ۵۰۰ شکست. احرازِ هویت نباید
    با یک اشتباهِ تایپی کرش کند؛ باید فقط «نامعتبر» بگوید.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    tg_raw, ts_raw, sig = parts
    try:
        tg_id, ts = int(tg_raw), int(ts_raw)
    except ValueError:
        return None
    if time.time() - ts > MAX_AGE:
        return None
    expected = hmac.new(server_secret().encode(),
                        f"{scope}|{tg_id}.{ts}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    return tg_id
