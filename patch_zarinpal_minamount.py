#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_zarinpal_minamount.py — رفعِ باگِ «حداقلِ مبلغ» در پرداختِ زرین‌پال.

علتِ خطای «The amount must be at least 15000»:
زرین‌پال اخیراً حداقلِ مبلغِ تراکنش را به ۱۵٬۰۰۰ تومان افزایش داده، اما دو
جا توی کد هنوز ۱۰٬۰۰۰ تومان هاردکد بود:
  ۱) دکمهٔ «🧪 تست اتصال زرین‌پال» با مبلغِ آزمایشیِ ۱۰٬۰۰۰ تومان درخواست
     می‌داد → همیشه با همین خطا رد می‌شد (چیزی که الان دیدی).
  ۲) حداقلِ «مبلغِ دلخواه»ِ شارژِ کیف‌پول هم ۱۰٬۰۰۰ تومان بود، یعنی کاربر
     می‌توانست ۱۰٬۰۰۰ تا ۱۴٬۹۹۹ تومان وارد کند و باز هم موقعِ درخواستِ
     واقعی از زرین‌پال همین ارور را بگیرد.

این پچ هر دو را به ۲۰٬۰۰۰ تومان می‌رساند (کمی بالاتر از حداقلِ ۱۵٬۰۰۰
زرین‌پال، برای حاشیهٔ اطمینان اگر باز هم تغییر کرد). دکمه‌هایِ مبلغِ
ثابت (۵۰٬۰۰۰ به بالا) از قبل مشکلی نداشتند و دست نمی‌خورند.

استفاده:
    cd /opt/botsaz
    python3 patch_zarinpal_minamount.py           # فقط چک می‌کند (dry-run)
    python3 patch_zarinpal_minamount.py --apply   # واقعاً می‌نویسد

قبلش خودش یک بک‌آپِ tenant/zarinpal_wallet.py.bak_before_minamount می‌سازد.
اجرا کردنش دوباره (بعد از اعمال) بی‌خطر است: می‌بیند پچ قبلاً خورده و
کاری نمی‌کند (idempotent — دنبالِ MARKER_ZARINPAL_MINAMOUNT_V1 می‌گردد).
"""
import sys

PATH = "tenant/zarinpal_wallet.py"
MARKER = "MARKER_ZARINPAL_MINAMOUNT_V1"

OLD_CHECK = (
    '    amount = fa_to_int(msg.text)\n'
    '    if not amount or amount < 10_000:\n'
    '        await msg.answer("❌ مبلغ نامعتبر است؛ حداقل مبلغ ۱۰٬۰۰۰ تومان است.")\n'
)
NEW_CHECK = (
    f'    # === {MARKER} ===\n'
    '    amount = fa_to_int(msg.text)\n'
    '    if not amount or amount < 20_000:\n'
    '        await msg.answer("❌ مبلغ نامعتبر است؛ حداقل مبلغ ۲۰٬۰۰۰ تومان است.")\n'
)

OLD_PROMPT = '    await cb.message.answer("مبلغ شارژ را به تومان و به عدد بفرستید (حداقل ۱۰٬۰۰۰ تومان):")\n'
NEW_PROMPT = '    await cb.message.answer("مبلغ شارژ را به تومان و به عدد بفرستید (حداقل ۲۰٬۰۰۰ تومان):")\n'

OLD_TEST_CALL = (
    '    ok, result = await platform_zarinpal.request_payment(\n'
    '        merchant,\n'
    '        10_000,\n'
    '        f"تستِ اتصالِ درگاهِ زرین‌پال — ربات {bot_label} (لطفاً پرداخت نکنید)",\n'
)
NEW_TEST_CALL = (
    f'    # {MARKER}: زرین‌پال حداقل ۱۵٬۰۰۰ تومان می‌خواهد؛ ۱۰٬۰۰۰ همیشه رد می‌شد\n'
    '    ok, result = await platform_zarinpal.request_payment(\n'
    '        merchant,\n'
    '        20_000,\n'
    '        f"تستِ اتصالِ درگاهِ زرین‌پال — ربات {bot_label} (لطفاً پرداخت نکنید)",\n'
)


def main():
    apply = "--apply" in sys.argv
    with open(PATH, "r", encoding="utf-8") as f:
        text = f.read()

    if MARKER in text:
        print("✅ این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    problems = []
    if text.count(OLD_CHECK) != 1:
        problems.append("بلوکِ چکِ حداقلِ مبلغ پیدا نشد یا تکراریه (count=%d)" % text.count(OLD_CHECK))
    if text.count(OLD_PROMPT) != 1:
        problems.append("متنِ پیامِ «مبلغ دلخواه» پیدا نشد یا تکراریه (count=%d)" % text.count(OLD_PROMPT))
    if text.count(OLD_TEST_CALL) != 1:
        problems.append("بلوکِ فراخوانیِ تستِ اتصال پیدا نشد یا تکراریه (count=%d)" % text.count(OLD_TEST_CALL))

    if problems:
        print("❌ فایل با نسخه‌ای که این پچ روش تست شده فرق داره:")
        for p in problems:
            print("   -", p)
        print()
        print("چیزی نوشته نشد. فایل را بفرست بررسی کنیم به‌جایِ اعمالِ کورکورانه.")
        return

    print("✅ فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    backup = PATH + ".bak_before_minamount"
    shutil.copy(PATH, backup)
    print("   بک‌آپ گرفته شد: %s" % backup)

    new_text = text.replace(OLD_CHECK, NEW_CHECK, 1)
    new_text = new_text.replace(OLD_PROMPT, NEW_PROMPT, 1)
    new_text = new_text.replace(OLD_TEST_CALL, NEW_TEST_CALL, 1)

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
