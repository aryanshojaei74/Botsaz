#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_zarinpal_reconcile.py — رفعِ «پول کم شد، کیف‌پول شارژ نشد» در شارژِ
زرین‌پالِ پلتفرم (کیف‌پولِ خودِ بات‌ساز).

علتِ باگِ گزارش‌شده (نمونه: حاج عَمیر):
مسیرِ فعلیِ شارژِ زرین‌پال (main_platform.py: /zarinpal/platform/callback)
فقط از طریقِ ریدایرکتِ مرورگرِ کاربر کار می‌کند، نه IPN/وبهوکِ واقعی از
سمتِ زرین‌پال. یعنی verify_payment فقط وقتی صدا زده می‌شود که مرورگرِ
کاربر بعدِ پرداخت واقعاً به این callback برگردد. اگر این ریدایرکت به هر
دلیلی انجام نشود (بستنِ زودهنگامِ صفحه، برگشتِ دستی به تلگرام قبل از
تکمیلِ ریدایرکت، مرورگرِ داخلیِ اپِ بانک که گاهی درست ریدایرکت نمی‌کند،
قطعیِ لحظه‌ایِ شبکه)، پول از کاربر کسر می‌شود ولی سرور هیچ‌وقت خبردار
نمی‌شود — نه پیامی به کاربر می‌رود، نه کیف‌پول شارژ می‌شود. تنها راهِ
نجات، دکمهٔ دستیِ «🔍 بررسی پرداخت» است که کاربر باید خودش رویش بزند.

این پچ یک حلقهٔ پس‌زمینه اضافه می‌کند که هر ۵ دقیقه، فاکتورهای
«zarinpal_pending»یِ معلق‌مانده را (حداقل ۲ دقیقه از ساختشان گذشته — تا
فرصتِ ریدایرکتِ طبیعی داده شود، و حداکثر ۲۴ ساعت — چون authorityِ
زرین‌پال بعدِ مدتی منقضی می‌شود) خودش نزدِ زرین‌پال استعلام می‌گیرد و اگر
واقعاً پرداخت شده، خودکار شارژ می‌کند و به کاربر پیام می‌دهد. دقیقاً همان
منطقِ verify + credit که دکمهٔ «بررسی پرداخت» و خودِ callback استفاده
می‌کنند (idempotent: قبل از verify چک می‌کند «zarinpal_charge»یِ همان
authority از قبل ثبت نشده باشد؛ اگر ثبت شده، رد می‌شود بدونِ دوباره‌شارژ).

دو فایل را تغییر می‌دهد:
  ۱) main_platform.py   → تابعِ حلقهٔ جدید: zarinpal_platform_reconcile_loop
  ۲) worker_platform.py → ایمپورت و اسپان‌کردنِ همین حلقه در استارتاپِ ورکر

استفاده:
    cd /opt/botsaz
    python3 patch_zarinpal_reconcile.py           # فقط چک می‌کند (dry-run)
    python3 patch_zarinpal_reconcile.py --apply   # واقعاً می‌نویسد

قبلش خودش بک‌آپِ .bak_before_zrp_reconcile از هر دو فایل می‌سازد. اجرا
کردنش دوباره (بعد از اعمال) بی‌خطر است — دنبالِ MARKER می‌گردد و اگر
پیدا کرد کاری نمی‌کند.

بعدِ اعمال، فقط سرویسِ ورکر (که حلقه‌ها در آن اسپان می‌شوند) نیاز به
ری‌استارت دارد؛ سرویسِ platform (که uvicorn/webhook را سرو می‌کند) هم
چون main_platform.py عوض شده باید ری‌استارت شود تا تابعِ جدید در دسترسِ
ورکر باشد وقتی آن را ایمپورت می‌کند — هر دو سرویس در پایینِ همین اسکریپت
لیست شده‌اند.
"""
import sys

MARKER = "MARKER_ZARINPAL_PLATFORM_RECONCILE_V1"

MP_PATH = "main_platform.py"
WP_PATH = "worker_platform.py"

# ───────────────────────── main_platform.py ─────────────────────────

MP_OLD = (
    '        except Exception as e:\n'
    '            print("platform swap poll error:", e)\n'
    '        await asyncio.sleep(interval)\n'
    '\n'
    '\n'
    'async def _setting_get(bot_id: int, key: str, default: str = "") -> str:\n'
)

MP_NEW_LOOP = f'''        except Exception as e:
            print("platform swap poll error:", e)
        await asyncio.sleep(interval)


# === {MARKER} ===
async def zarinpal_platform_reconcile_loop(interval: int = 300):
    """جبرانِ فاکتورهایِ زرین‌پالی که پول کسر شده ولی چون مرورگرِ کاربر به
    /zarinpal/platform/callback برنگشته، کیف‌پول خودکار شارژ نشده. هر
    ``interval`` ثانیه فاکتورهایِ «zarinpal_pending»ِ بینِ ۲ دقیقه تا ۲۴
    ساعتِ پیش را دوباره نزدِ زرین‌پال استعلام می‌گیرد. idempotent: مثلِ
    دکمهٔ «بررسی پرداخت» و خودِ callback، قبل از verify چک می‌کند
    «zarinpal_charge»یِ همان authority از قبل ثبت نشده باشد."""
    import datetime as _dt
    from sqlalchemy import select as _select
    import platform_zarinpal
    from models import PlatformTxn
    from platform_bot import get_pset, _platform_zarinpal_credit

    await asyncio.sleep(30)
    while True:
        try:
            mid = await get_pset("platform_zarinpal_merchant_id", "")
            if not mid:
                await asyncio.sleep(interval)
                continue
            now = _dt.datetime.now()
            cutoff_new = now - _dt.timedelta(minutes=2)
            cutoff_old = now - _dt.timedelta(hours=24)
            async with Session() as s:
                pendings = (await s.scalars(
                    _select(PlatformTxn).where(
                        PlatformTxn.kind == "zarinpal_pending",
                        PlatformTxn.created_at <= cutoff_new,
                        PlatformTxn.created_at >= cutoff_old,
                    ))).all()
            for p in pendings:
                try:
                    note = p.note or ""
                    if "authority=" not in note:
                        continue
                    authority = note.split("authority=", 1)[1].split("|", 1)[0]
                    async with Session() as s:
                        already = await s.scalar(
                            _select(PlatformTxn).where(
                                PlatformTxn.kind == "zarinpal_charge",
                                PlatformTxn.note.contains(f"authority={{authority}}"),
                            ))
                    if already:
                        continue
                    amount_toman = int(float(p.amount))
                    ok, res = await platform_zarinpal.verify_payment(mid, amount_toman, authority)
                    if not ok:
                        continue
                    new_bal = await _platform_zarinpal_credit(
                        p.user_telegram_id, amount_toman,
                        f"authority={{authority}}|ref_id={{res}}|auto_reconcile=1")
                    async with Session() as s:
                        p2 = await s.scalar(
                            _select(PlatformTxn).where(
                                PlatformTxn.kind == "zarinpal_pending",
                                PlatformTxn.note.contains(f"authority={{authority}}"),
                            ).with_for_update())
                        if p2:
                            p2.kind = "zarinpal_pending_done"
                            await s.commit()
                    try:
                        if platform_bot:
                            await platform_bot.send_message(
                                p.user_telegram_id,
                                "✅ <b>کیف پولت با موفقیت شارژ شد!</b>\\n"
                                f"💰 مبلغ واریزی: <b>{{amount_toman:,}} تومان</b>\\n"
                                f"👛 موجودی فعلی: <b>{{new_bal:,.0f}} تومان</b>\\n"
                                "🧾 روش پرداخت: 🏦 زرین‌پال")
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                except Exception:
                    logger.exception(
                        "zarinpal reconcile: خطا روی یک فاکتورِ pending (بقیه ادامه پیدا می‌کند)")
        except Exception:
            logger.exception("zarinpal platform reconcile loop error")
        await asyncio.sleep(interval)


async def _setting_get(bot_id: int, key: str, default: str = "") -> str:
'''

# ───────────────────────── worker_platform.py ─────────────────────────

WP_OLD_IMPORT = (
    '# === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===\n'
    'from platform_bot import sms_expiry_notify_loop\n'
    '\n'
    '\n'
    'async def _worker_startup():\n'
)
WP_NEW_IMPORT = (
    '# === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===\n'
    'from platform_bot import sms_expiry_notify_loop\n'
    f'# === {MARKER} ===\n'
    'from main_platform import zarinpal_platform_reconcile_loop\n'
    '\n'
    '\n'
    'async def _worker_startup():\n'
)

WP_OLD_SPAWN = (
    '        # === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===\n'
    '        sms_expiry_notify_loop(),\n'
    '    ):\n'
)
WP_NEW_SPAWN = (
    '        # === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===\n'
    '        sms_expiry_notify_loop(),\n'
    f'        # === {MARKER} ===\n'
    '        zarinpal_platform_reconcile_loop(),\n'
    '    ):\n'
)


def _patch_one(path, replacements, marker_check_text):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if marker_check_text in text:
        print(f"✅ {path}: این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return None

    problems = []
    for old, _new in replacements:
        c = text.count(old)
        if c != 1:
            problems.append(f"یک بلوک پیدا نشد یا تکراریه (count={c}):\n{old[:120]}...")
    if problems:
        print(f"❌ {path}: با نسخه‌ای که این پچ روش تست شده فرق داره:")
        for p in problems:
            print("   -", p)
        return False

    new_text = text
    for old, new in replacements:
        new_text = new_text.replace(old, new, 1)
    return new_text


def main():
    apply = "--apply" in sys.argv

    mp_new = _patch_one(MP_PATH, [(MP_OLD, MP_NEW_LOOP)], MARKER)
    wp_new = _patch_one(WP_PATH, [(WP_OLD_IMPORT, WP_NEW_IMPORT),
                                   (WP_OLD_SPAWN, WP_NEW_SPAWN)], MARKER)

    if mp_new is False or wp_new is False:
        print()
        print("چیزی نوشته نشد. فایل‌ها را بفرست بررسی کنیم به‌جایِ اعمالِ کورکورانه.")
        return
    if mp_new is None and wp_new is None:
        return  # هر دو قبلاً پچ شده‌اند

    print("✅ هر دو فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    import py_compile

    for path, new_text in ((MP_PATH, mp_new), (WP_PATH, wp_new)):
        if new_text is None:
            continue
        backup = path + ".bak_before_zrp_reconcile"
        shutil.copy(path, backup)
        print(f"   بک‌آپ گرفته شد: {backup}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        print(f"   نوشته شد: {path}")
        try:
            py_compile.compile(path, doraise=True)
            print(f"   py_compile ({path}): OK ✅")
        except py_compile.PyCompileError as e:
            print(f"   ❌ py_compile خطا داد روی {path}، برگرداندنِ بک‌آپ:")
            print(e)
            shutil.copy(backup, path)
            return

    print()
    print("بعدش هر دو سرویس را ری‌استارت کن:")
    print("    systemctl restart botsaz-platform")
    print("    systemctl restart botsaz-platform-worker")


if __name__ == "__main__":
    main()
