#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_toncenter_ratelimit.py — رفعِ خطایِ «Ratelimit exceed» در برداشتِ
TON از کیف‌پولِ Fragment.

علتِ باگ:
سه تابعی که با toncenter.com/api/v2 حرف می‌زنند (fetch_balance_ton،
_get_seqno، _send_boc) هیچ‌کدام API-key نمی‌فرستند و هیچ retry‌ای هم
ندارند. toncenter بدونِ کلید، محدودیتِ نرخِ خیلی پایینی داره (و چون این
پلتفرم چندمستأجریه، همهٔ ربات‌ها از یک IPِ سرور به آن درخواست می‌زنند)،
پس زیر بارِ کمی هم زود «Ratelimit exceed» برمی‌گردونه — دقیقاً همون
چیزی که کاربرِ SPARKO STORE موقعِ برداشت دید. یک بارِ خطا هم کلِ
برداشت را (بدونِ هیچ تلاشِ دوباره‌ای) شکست می‌داد.

این پچ:
  ۱) به config.py یک متغیرِ محیطیِ اختیاری اضافه می‌کند: TONCENTER_API_KEY.
     اگر ست شود (کلیدِ رایگان از @tonapibot در تلگرام)، به‌صورتِ هدرِ
     X-Api-Key به هر سه درخواست اضافه می‌شود و سقفِ نرخ به‌شدت بالاتر
     می‌رود. اگر ست نشود، رفتار مثلِ قبل (بدونِ کلید) است.
  ۲) هر سه تابع را با ۳ بار تلاشِ خودکار (backoff نمایی: ۱.۵، ۳، ۶ ثانیه)
     مشخصاً برایِ پاسخِ «Ratelimit»/HTTP 429 عوض می‌کند — یعنی یک
     ریت‌لیمیتِ گذرا دیگر کلِ برداشت را خراب نمی‌کند.

برایِ رفعِ کاملِ ریشه‌ای (نه فقط کم‌کردنِ احتمالش)، یک کلیدِ رایگانِ
toncenter از ربات @tonapibot بگیر و در فایلِ .env سرویسِ tenant این
خط را اضافه/ویرایش کن:
    TONCENTER_API_KEY=...

استفاده:
    cd /opt/botsaz
    python3 patch_toncenter_ratelimit.py           # فقط چک می‌کند (dry-run)
    python3 patch_toncenter_ratelimit.py --apply   # واقعاً می‌نویسد

بک‌آپ‌ها: config.py.bak_before_toncenterfix و
tenant/fragment_stars.py.bak_before_toncenterfix
اجرایِ دوباره بعد از اعمال بی‌خطر است (idempotent).
"""
import sys

MARKER = "MARKER_TONCENTER_RATELIMIT_V1"

CONFIG_PATH = "config.py"
CONFIG_OLD = (
    '# برای شروع SQLite کافی است؛ برای مقیاس به Postgres سوییچ کن\n'
    'DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///botsaz.db")'
)
CONFIG_NEW = (
    f'# === {MARKER} ===\n'
    '# کلیدِ رایگانِ toncenter (از @tonapibot در تلگرام). اختیاری — اگر خالی\n'
    '# بماند، بدونِ کلید کار می‌کند ولی سقفِ نرخِ پایین‌تری دارد.\n'
    'TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")\n\n'
    '# برای شروع SQLite کافی است؛ برای مقیاس به Postgres سوییچ کن\n'
    'DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///botsaz.db")'
)

FRAG_PATH = "tenant/fragment_stars.py"

FRAG_IMPORT_OLD = 'from config import Session'
FRAG_IMPORT_NEW = (
    f'from config import Session\n'
    f'from config import TONCENTER_API_KEY  # === {MARKER} ==='
)

FRAG_HELPER_NEW = f'''
# === {MARKER} ===
def _toncenter_headers() -> dict:
    return {{"X-Api-Key": TONCENTER_API_KEY}} if TONCENTER_API_KEY else {{}}


async def _toncenter_call(method: str, url: str, **kwargs):
    """یک درخواست به toncenter با ۳ بار تلاشِ خودکار وقتی جواب
    «Ratelimit» یا HTTP 429 باشد (backoff: ۱.۵، ۳، ۶ ثانیه).
    خروجی: (data_dict_or_None, last_error_str)."""
    import asyncio
    delays = (1.5, 3.0, 6.0)
    last_err = ""
    for attempt, delay in enumerate((0.0,) + delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            t = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.request(method, url, headers=_toncenter_headers(), **kwargs) as r:
                    status = r.status
                    data = await r.json(content_type=None)
        except Exception as e:                               # noqa: BLE001
            last_err = f"{{type(e).__name__}}"
            continue
        is_ratelimited = status == 429 or (
            isinstance(data, dict) and "ratelimit" in str(data.get("error") or "").lower())
        if is_ratelimited:
            last_err = "Ratelimit exceed"
            continue
        return data, ""
    return None, last_err or "Ratelimit exceed"

'''

FRAG_BAL_OLD = '''async def fetch_balance_ton(address: str) -> float | None:
    """موجودیِ زنده از toncenter (به TON). خطا → None (نه صفر، تا با
    «واقعاً صفره» قاطی نشود)."""
    if aiohttp is None:
        return None
    url = f"{TONCENTER_BASE}/getAddressBalance"
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(url, params={"address": address}) as r:
                data = await r.json(content_type=None)
    except Exception:
        logger.warning("fragment_stars: toncenter پاسخ نداد (%s)", address, exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int(data.get("result") or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None'''
FRAG_BAL_NEW = f'''async def fetch_balance_ton(address: str) -> float | None:
    """موجودیِ زنده از toncenter (به TON). خطا → None (نه صفر، تا با
    «واقعاً صفره» قاطی نشود). {MARKER}: با retry رویِ ریت‌لیمیت."""
    if aiohttp is None:
        return None
    url = f"{{TONCENTER_BASE}}/getAddressBalance"
    data, err = await _toncenter_call("GET", url, params={{"address": address}})
    if data is None:
        logger.warning("fragment_stars: toncenter پاسخ نداد (%s): %s", address, err)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int(data.get("result") or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None'''

FRAG_SEQNO_OLD = '''async def _get_seqno(address: str) -> int | None:
    """seqno فعلیِ کیف‌پول — بدونِ آن نمی‌شود تراکنشِ معتبر ساخت."""
    if aiohttp is None:
        return None
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{TONCENTER_BASE}/getWalletInformation",
                             params={"address": address}) as r:
                data = await r.json(content_type=None)
    except Exception:                                        # noqa: BLE001
        logger.warning("fragment_withdraw: getWalletInformation نشد", exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int((data.get("result") or {}).get("seqno") or 0)
    except (TypeError, ValueError):
        return None'''
FRAG_SEQNO_NEW = f'''async def _get_seqno(address: str) -> int | None:
    """seqno فعلیِ کیف‌پول — بدونِ آن نمی‌شود تراکنشِ معتبر ساخت.
    {MARKER}: با retry رویِ ریت‌لیمیت."""
    if aiohttp is None:
        return None
    data, err = await _toncenter_call(
        "GET", f"{{TONCENTER_BASE}}/getWalletInformation", params={{"address": address}})
    if data is None:
        logger.warning("fragment_withdraw: getWalletInformation نشد: %s", err)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    try:
        return int((data.get("result") or {{}}).get("seqno") or 0)
    except (TypeError, ValueError):
        return None'''

FRAG_SEND_OLD = '''async def _send_boc(boc_b64: str) -> tuple[bool, str]:
    if aiohttp is None:
        return False, "aiohttp نصب نیست."
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(f"{TONCENTER_BASE}/sendBoc",
                              json={"boc": boc_b64}) as r:
                data = await r.json(content_type=None)
    except Exception as e:                                   # noqa: BLE001
        return False, f"ارسال به شبکه نشد: {type(e).__name__}"
    if isinstance(data, dict) and data.get("ok"):
        return True, "ارسال شد."
    err = ""
    if isinstance(data, dict):
        err = str(data.get("error") or data.get("result") or "")[:200]
    return False, err or "شبکه تراکنش را نپذیرفت."'''
FRAG_SEND_NEW = f'''async def _send_boc(boc_b64: str) -> tuple[bool, str]:
    """{MARKER}: با retry رویِ ریت‌لیمیت — قبلاً یک ریت‌لیمیتِ گذرا کلِ
    برداشت را شکست می‌داد."""
    if aiohttp is None:
        return False, "aiohttp نصب نیست."
    data, err = await _toncenter_call(
        "POST", f"{{TONCENTER_BASE}}/sendBoc", json={{"boc": boc_b64}})
    if data is None:
        return False, f"ارسال به شبکه نشد: {{err}}"
    if isinstance(data, dict) and data.get("ok"):
        return True, "ارسال شد."
    err2 = ""
    if isinstance(data, dict):
        err2 = str(data.get("error") or data.get("result") or "")[:200]
    return False, err2 or "شبکه تراکنش را نپذیرفت."'''


def main():
    apply = "--apply" in sys.argv

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config_text = f.read()
    with open(FRAG_PATH, "r", encoding="utf-8") as f:
        frag_text = f.read()

    if MARKER in config_text and MARKER in frag_text:
        print("✅ این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    problems = []
    checks = [
        (config_text, CONFIG_OLD, "config.py: بلوکِ DATABASE_URL"),
        (frag_text, FRAG_IMPORT_OLD, "fragment_stars.py: ایمپورتِ config"),
        (frag_text, FRAG_BAL_OLD, "fragment_stars.py: fetch_balance_ton"),
        (frag_text, FRAG_SEQNO_OLD, "fragment_stars.py: _get_seqno"),
        (frag_text, FRAG_SEND_OLD, "fragment_stars.py: _send_boc"),
    ]
    for text, needle, label in checks:
        c = text.count(needle)
        if c != 1:
            problems.append(f"{label} پیدا نشد یا تکراریه (count={c})")

    if problems:
        print("❌ فایل‌ها با نسخه‌ای که این پچ روش تست شده فرق دارن:")
        for p in problems:
            print("   -", p)
        print()
        print("چیزی نوشته نشد. فایل‌ها را بفرست بررسی کنیم به‌جایِ اعمالِ کورکورانه.")
        return

    print("✅ فایل‌ها دقیقاً مطابقِ انتظارن؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    cfg_backup = CONFIG_PATH + ".bak_before_toncenterfix"
    frag_backup = FRAG_PATH + ".bak_before_toncenterfix"
    shutil.copy(CONFIG_PATH, cfg_backup)
    shutil.copy(FRAG_PATH, frag_backup)
    print("   بک‌آپ گرفته شد: %s" % cfg_backup)
    print("   بک‌آپ گرفته شد: %s" % frag_backup)

    new_config = config_text.replace(CONFIG_OLD, CONFIG_NEW, 1)

    new_frag = frag_text.replace(FRAG_IMPORT_OLD, FRAG_IMPORT_NEW, 1)
    # هلپرِ retry را درست قبلِ fetch_balance_ton اضافه می‌کنیم
    new_frag = new_frag.replace(FRAG_BAL_OLD, FRAG_HELPER_NEW + "\n" + FRAG_BAL_NEW, 1)
    new_frag = new_frag.replace(FRAG_SEQNO_OLD, FRAG_SEQNO_NEW, 1)
    new_frag = new_frag.replace(FRAG_SEND_OLD, FRAG_SEND_NEW, 1)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_config)
    with open(FRAG_PATH, "w", encoding="utf-8") as f:
        f.write(new_frag)
    print("   نوشته شد: %s" % CONFIG_PATH)
    print("   نوشته شد: %s" % FRAG_PATH)

    import py_compile
    try:
        py_compile.compile(CONFIG_PATH, doraise=True)
        py_compile.compile(FRAG_PATH, doraise=True)
        print("   py_compile: OK ✅")
    except py_compile.PyCompileError as e:
        print("   ❌ py_compile خطا داد، برگرداندنِ بک‌آپ‌ها:")
        print(e)
        shutil.copy(cfg_backup, CONFIG_PATH)
        shutil.copy(frag_backup, FRAG_PATH)
        return

    print()
    print("بعدش سرویسِ tenant را ری‌استارت کن:")
    print("    systemctl restart botsaz-tenant")
    print()
    print("برایِ رفعِ ریشه‌ای (نه فقط retry): یک کلیدِ رایگان از @tonapibot")
    print("بگیر و در .env این خط را اضافه کن، بعد دوباره ری‌استارت کن:")
    print("    TONCENTER_API_KEY=...")


if __name__ == "__main__":
    main()
