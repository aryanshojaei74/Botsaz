#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_botrow_cache_crossprocess.py — رفعِ کشِ کهنه‌مانده بعد از اقداماتِ
ادمینِ پلتفرم (بازگردانی از بایگانی، قفل/بازکردن، تغییرِ پلن و ...) رویِ
رباتی که سرویسِ tenant به‌صورتِ پروسه‌ی جدا اجرا می‌کند.

علتِ باگ — دو کشِ جدا و ناهماهنگ:
۱) main_tenant.py یک کشِ سبکِ «وضعیت» داره (`_tenant_status_cache`, سقفِ
   ۵ ثانیه) که فقط جلویِ ورودِ آپدیت به ربات‌هایِ غیرفعال/قفل رو می‌گیره.
   این یکی با tenant_status_bus (پیامِ NOTIFY از پروسه‌ی پلتفرم) درست
   invalidate می‌شه.
۲) tenant/__init__.py یک کشِ کاملاً جداگانه داره: `_BOT_ROW_CACHE`
   (سقفِ ۲۰ ثانیه) که خودِ ردیفِ کاملِ BotRow رو نگه می‌داره و پایهٔ
   تقریباً همه‌چیزِ پنلِ مدیریت (از جمله is_owner_or_admin) است. خودِ
   کدِ این کش صراحتاً می‌گه: «این فقط کشِ همین پروسه رو پاک می‌کنه؛
   برایِ پروسه‌هایِ دیگه باید tenant_status_bus هم صدا زده بشه» — ولی
   listenerِ tenant_status_bus در main_tenant.py
   (`_clear_local_tenant_cache`) فقط کشِ شماره‌یِ ۱ رو پاک می‌کنه، هیچ‌وقت
   `tenant.invalidate_bot_row_cache` رو صدا نمی‌زنه.

نتیجه: وقتی ادمینِ پلتفرم (در یک پروسه‌ی جدا: main_platform.py) رباتی رو
از بایگانی برمی‌گردونه/قفل‌بازش‌می‌کنه/پلنش رو عوض می‌کنه، پروسه‌ی
tenant صحیح باخبر می‌شه که «دیگه غیرفعال نیست» (کشِ ۱ پاک می‌شه)، پس
آپدیت‌ها رو رد نمی‌کنه — ولی get_bot_row همچنان ممکنه تا ۲۰ ثانیهٔ دیگه
(یا در موقعیت‌هایِ خاص، تا وقتی توکن دوباره از یه‌جایِ دیگه کوئری بشه)
نسخهٔ کهنهٔ ردیف رو برگردونه، و چیزهایی که رویِ آن ردیف حساب می‌کنن
(دسترسیِ پشتیبانی/is_owner_or_admin و کلِ پنلِ مدیریت) رفتارِ درستی
نشون نمی‌دن.

این پچ: `_clear_local_tenant_cache` در main_tenant.py رو طوری عوض
می‌کنه که علاوه‌بر کشِ خودش، `tenant.invalidate_bot_row_cache` رو هم
صدا بزنه — یعنی از این به بعد، هر NOTIFYِ tenant_status_bus هر دو کش
رو با هم پاک می‌کنه.

استفاده:
    cd /opt/botsaz
    python3 patch_botrow_cache_crossprocess.py           # dry-run
    python3 patch_botrow_cache_crossprocess.py --apply   # اعمال واقعی

بک‌آپ: main_tenant.py.bak_before_botrowcachefix
اجرایِ دوباره بعد از اعمال بی‌خطر است (idempotent).
"""
import sys

PATH = "main_tenant.py"
MARKER = "MARKER_BOTROW_CACHE_CROSSPROCESS_V1"

OLD_BLOCK = '''def _clear_local_tenant_cache(token: str | None = None) -> None:
    """کشِ وضعیت + Bot instanceِ کش‌شدهٔ این توکن (یا همه، اگر token=None) را
    پاک می‌کند. هم وقتی خودِ این پروسه یک بات را حذف/تغییر می‌دهد صدا زده
    می‌شود، هم به‌عنوانِ callbackِ listenerِ tenant_status_bus (وقتی
    main_platform.py سیگنالِ NOTIFY می‌فرستد)."""
    if token:
        _tenant_status_cache.pop(token, None)
        _tenant_bots.pop(token, None)
    else:
        _tenant_status_cache.clear()
        _tenant_bots.clear()'''

NEW_BLOCK = f'''def _clear_local_tenant_cache(token: str | None = None) -> None:
    """کشِ وضعیت + Bot instanceِ کش‌شدهٔ این توکن (یا همه، اگر token=None) را
    پاک می‌کند. هم وقتی خودِ این پروسه یک بات را حذف/تغییر می‌دهد صدا زده
    می‌شود، هم به‌عنوانِ callbackِ listenerِ tenant_status_bus (وقتی
    main_platform.py سیگنالِ NOTIFY می‌فرستد).

    {MARKER}: قبلاً فقط همین دو کشِ سبک پاک می‌شدن؛ کشِ کاملِ BotRow در
    tenant/__init__.py (`_BOT_ROW_CACHE` — پایهٔ is_owner_or_admin و کلِ
    پنلِ مدیریت) دست‌نخورده می‌موند، پس بعدِ اقداماتِ ادمینِ پلتفرم
    (بازگردانی از بایگانی و مشابه) تا ۲۰ ثانیه یا بیشتر همچنان نسخهٔ کهنه
    برمی‌گشت."""
    if token:
        _tenant_status_cache.pop(token, None)
        _tenant_bots.pop(token, None)
    else:
        _tenant_status_cache.clear()
        _tenant_bots.clear()
    try:
        from tenant import invalidate_bot_row_cache
        invalidate_bot_row_cache(token)
    except Exception:
        logger.exception("{MARKER}: باطل‌سازیِ کشِ BotRow ناموفق بود")'''


def main():
    apply = "--apply" in sys.argv
    with open(PATH, "r", encoding="utf-8") as f:
        text = f.read()

    if MARKER in text:
        print("✅ این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    count = text.count(OLD_BLOCK)
    if count != 1:
        print("❌ فایل با نسخه‌ای که این پچ روش تست شده فرق داره (count=%d)." % count)
        print("چیزی نوشته نشد. فایل را بفرست بررسی کنیم به‌جایِ اعمالِ کورکورانه.")
        return

    print("✅ فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    backup = PATH + ".bak_before_botrowcachefix"
    shutil.copy(PATH, backup)
    print("   بک‌آپ گرفته شد: %s" % backup)

    new_text = text.replace(OLD_BLOCK, NEW_BLOCK, 1)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("   نوشته شد: %s" % PATH)

    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
        print("   py_compile: OK ✅")
    except py_compile.PyCompileError as e:
        print("   ❌ py_compile خطا داد، برگرداندنِ بک‌آپ:")
        print(e)
        shutil.copy(backup, PATH)
        return

    print()
    print("بعدش سرویسِ tenant را ری‌استارت کن:")
    print("    systemctl restart botsaz-tenant")


if __name__ == "__main__":
    main()
