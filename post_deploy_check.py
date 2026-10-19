#!/usr/bin/env python3
"""
post_deploy_check.py — چکِ سریعِ بعدِ دیپلوی برایِ همه‌ی تغییراتِ «ارائه‌دهندگان
و محصولاتِ SMM» (نسخه‌ی به‌روز — شاملِ چکِ هم‌نسخگیِ فایل‌ها).

فقط می‌خونه، هیچ‌چیزی رو تغییر نمی‌ده (به‌جز اجرایِ خودِ ensure_smm_columns
که خودش idempotent و امنه). روی سروری که .env واقعی داره اجرا کن، از همون
پوشه‌ای که main.py توشه:

    python3 post_deploy_check.py

خروجی: لیستی از ✅/❌ برایِ هر چک. اگه همه ✅ بود، دیپلوی سالمه.
"""
import asyncio
import sys


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


async def check_imports() -> tuple[bool, str]:
    try:
        import tenant  # noqa: F401
        import tenant.bots.virtual_service.smm  # noqa: F401
        import tenant.shared.panel_clients.vpn_panel  # noqa: F401
        import tenant.shared.panel_clients.nodex_panel  # noqa: F401
        import tenant.shared.panel_clients.degeris_panel  # noqa: F401
        import tenant.shared.panel_clients.eylan_panel  # noqa: F401
        import tenant.shared.panel_clients.heimdall_panel  # noqa: F401
        import tenant.shared.panel_clients.marzban_panel  # noqa: F401
        import tenant.shared.panel_clients.marzneshin_panel  # noqa: F401
        import models  # noqa: F401
        import plans  # noqa: F401
        return True, "همه‌ی ماژول‌های تغییریافته بدونِ خطا import شدن"
    except Exception as e:
        return False, f"ایمپورت شکست خورد: {e!r}"


async def check_file_versions_match() -> tuple[bool, str]:
    """=== PATCH_DEPLOY_CHECK_VERSION_SKEW_V1 ===
    گزارشِ واقعی: کلیک رویِ «مدیریتِ محصولات» با ERR-XXXXXX کرش می‌کرد.
    علتِ محتمل‌ترین: tenant/smm.py و tenant/__init__.py از یه راندِ جدید
    دیپلوی شده بودن که به Feature.SMM_API_ACCESS نیاز دارن، ولی plans.py
    از یه نسخه‌ی قدیمی‌تر (بدونِ اون Feature) مونده بود — یعنی سه فایل
    باید همیشه با هم دیپلوی بشن، نه تک‌تک."""
    try:
        from plans import Feature, has_feature
        feat = Feature.SMM_API_ACCESS
        free_ok = has_feature("free", feat)
        growth_ok = has_feature("growth", feat)
        if free_ok:
            return False, "پلنِ رایگان نباید SMM_API_ACCESS داشته باشه ولی داره — plans.py درست تنظیم نشده"
        if not growth_ok:
            return False, "پلنِ رشد باید SMM_API_ACCESS داشته باشه ولی نداره — plans.py درست تنظیم نشده"
        import inspect
        from tenant.bots.virtual_service import smm as smm_mod
        src = inspect.getsource(smm_mod._require_smm_api_access)
        if "SMM_API_ACCESS" not in src:
            return False, "tenant/smm.py قدیمیه — تابعِ _require_smm_api_access از این Feature استفاده نمی‌کنه"
        return True, "plans.py و tenant/smm.py هم‌نسخه‌ن (Feature.SMM_API_ACCESS درست کار می‌کنه)"
    except AttributeError as e:
        return False, (
            f"دقیقاً همون کرشِ گزارش‌شده! plans.py قدیمیه و Feature.SMM_API_ACCESS "
            f"رو نداره: {e!r} — plans.py رو هم با tenant/smm.py و tenant/__init__.py "
            f"با هم جایگزین کن و ری‌استارت کن."
        )
    except Exception as e:
        return False, f"چک شکست خورد: {e!r}"


async def check_products_menu_no_crash() -> tuple[bool, str]:
    """خودِ صفحه‌ای که واقعاً کرش کرد (smm_products_content) رو با یه
    رباتِ فرضی صدا می‌زنه — اگه اینجا هم استثنا بگیره، همون‌جاست."""
    try:
        from config import Session, engine
        from models import Base, Bot as BotRow
        from tenant.bots.virtual_service.smm import ensure_smm_columns, smm_products_content, _require_smm_api_access

        async with Session() as s:
            conn = await s.connection()
            await conn.run_sync(Base.metadata.create_all)
            await s.commit()
        await ensure_smm_columns(force=True)

        async with Session() as s:
            b = BotRow(owner_id=-999999, token="__post_deploy_check__",
                       username="__post_deploy_check__", field="virtual_service", plan="pro")
            s.add(b)
            await s.commit()
            bot_id = b.id

        class _FakeCB:
            async def answer(self, *a, **k):
                pass

        allowed = await _require_smm_api_access(_FakeCB(), b)
        text, kb = await smm_products_content(bot_id)

        async with Session() as s:
            row = await s.get(BotRow, bot_id)
            if row:
                await s.delete(row)
                await s.commit()

        await engine.dispose()
        if not allowed:
            return False, "پلنِ حرفه‌ای باید مجاز باشه ولی رد شد"
        return True, "صفحه‌ی «مدیریتِ محصولات» بدونِ خطا ساخته شد"
    except Exception as e:
        import traceback
        return False, f"دقیقاً همون کرش! {e!r}\n   {traceback.format_exc()[-500:]}"


async def check_db_migration() -> tuple[bool, str]:
    try:
        from sqlalchemy import inspect as sa_inspect
        from config import Session
        from tenant.bots.virtual_service.smm import ensure_smm_columns

        await ensure_smm_columns(force=True)

        wanted = {
            "smm_services": {"sort_order", "is_manual"},
            "smm_panels": {
                "auto_check_enabled", "last_check_ok",
                "last_check_at", "last_balance", "last_currency",
            },
        }
        async with Session() as s:
            conn = await s.connection()

            def _cols(sync_conn, table):
                insp = sa_inspect(sync_conn)
                if table not in insp.get_table_names():
                    return None
                return {c["name"] for c in insp.get_columns(table)}

            missing = []
            for table, need in wanted.items():
                existing = await conn.run_sync(_cols, table)
                if existing is None:
                    missing.append(f"جدولِ {table} اصلاً وجود نداره")
                    continue
                gap = need - existing
                if gap:
                    missing.append(f"{table}: {', '.join(sorted(gap))}")
        if missing:
            return False, "این ستون‌ها هنوز روی دیتابیس نیستن: " + " | ".join(missing)
        return True, "همه‌ی ستون‌های جدید (sort_order, auto_check_enabled, ...) روی دیتابیس هستن"
    except Exception as e:
        return False, f"چکِ دیتابیس شکست خورد: {e!r}"


async def check_frontend_404_hint() -> tuple[bool, str]:
    try:
        from tenant.shared.panel_clients.vpn_panel import frontend_404_hint
        real_case = frontend_404_hint(
            '{"error":true,"url":"https://x.example.com/api/admins/token",'
            '"statusCode":404,"statusMessage":"Page not found: /api/admins/token"}'
        )
        normal_404 = frontend_404_hint('{"detail":"Not Found"}')
        non_json = frontend_404_hint("plain text response")
        if real_case and not normal_404 and not non_json:
            return True, "تشخیصِ ۴۰۴-فرانت‌اند درست کار می‌کنه"
        return False, "رفتارِ غیرمنتظره"
    except Exception as e:
        return False, f"چکِ تابعِ تشخیص شکست خورد: {e!r}"


async def check_smm_menu_layout() -> tuple[bool, str]:
    """چیدمانِ منو رو با پلنِ حرفه‌ای (که همه‌چی بازه) شبیه‌سازی می‌کنه —
    شاخه‌ی قفل‌شده (فقط رایگان) رو عمداً نادیده می‌گیره چون اون یه شاخه‌ی
    جایگزینه، نه بخشی از چیدمانِ کاملِ موردِ انتظار."""
    try:
        import re
        src = open("tenant/__init__.py", encoding="utf-8").read()
        m = re.search(
            r'if bot_field == "virtual_service":(.*?)if bot_field == "ad_bot":',
            src, re.S)
        if not m:
            return False, "بلوکِ virtual_service تو tenant/__init__.py پیدا نشد"
        block = m.group(1)
        # شاخه‌ی else (قفل‌شده، مالِ رایگان) رو حذف کن تا فقط شاخه‌ی باز بمونه
        block_no_locked = re.sub(r"else:\s*\n\s*opts\.append\(\(\"🔒[^\n]*\n", "", block)
        opts = re.findall(r'opts\.append\(\("([^"]+)", "([^"]+)"\)\)', block_no_locked)
        expected = [
            ("🗂 مشاهده سفارشات", "tadm:vs_orders"),
            ("🏢 ارائه‌دهندگان", "smm:panels"),
            ("📦 مدیریتِ محصولات", "smm:products:menu"),
            ("📁 مدیریتِ دسته‌ها", "smm:cat:menu:top"),
            ("💰 تنظیم قیمت سرویس‌ها", "tadm:vs_prices"),
            ("✍️ سرویس‌های دستی", "smm:manlist"),
            ("📘 راهنما", "tadm:products:guide"),
        ]
        if opts == expected:
            return True, "چیدمانِ منویِ «سرویس‌ها و محصولات» (پلنِ باز) دقیقاً همونیه که تأیید شد"
        return False, f"چیدمان با انتظار فرق داره: {opts}"
    except Exception as e:
        return False, f"چکِ منو شکست خورد: {e!r}"


async def check_rate_cache_health() -> tuple[bool, str]:
    """=== PATCH_RATE_CACHE_DIAGNOSTIC_V1 ===
    گزارشِ واقعی (با فیلم!): بعدِ دیپلویِ فیکسِ کشِ دیتابیسی، انتخابِ یک
    سرویس هنوز ~۱۱ ثانیه طول می‌کشید. این چک مستقیم می‌ره سراغِ خودِ
    کشِ دیتابیسی (PlatformSetting) و سنِ آخرین مقدارِ ذخیره‌شده رو نشون
    می‌ده — اگه چیزی توش نبود یا خیلی قدیمی بود، یعنی حلقه‌ی worker
    (smm_rate_prewarm_loop) اصلاً موفق نشده یا اجرا نمی‌شه. همچنین یه
    فراخوانیِ واقعی و زمان‌سنجی‌شده هم می‌زنه تا سرعتِ واقعیِ همین لحظه
    رو نشون بده (نه فرضی)."""
    try:
        import time
        from config import Session
        from models import PlatformSetting
        from sqlalchemy import select
        import json as _json

        async with Session() as s:
            row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == "usd_toman_rate:main"))

        db_status = "هیچ مقداری تویِ دیتابیس نیست — یعنی هیچ‌وقت هیچ پروسه‌ای (نه worker نه سرویسِ اصلی) تا الان نتونسته نرخ رو با موفقیت بگیره و ذخیره کنه."
        db_fresh = False
        if row:
            try:
                payload = _json.loads(row.value)
                age_min = (time.time() - payload["ts"]) / 60
                db_fresh = age_min < 60
                db_status = f"آخرین مقدارِ ذخیره‌شده: {payload['rate']:,.0f} تومان، سنش {age_min:.1f} دقیقه‌ست ({'تازه' if db_fresh else 'کهنه — بیشتر از ۱ ساعت، یعنی worker دیگه به‌روزش نمی‌کنه'})."
            except Exception:
                db_status = "یه مقدار تو دیتابیس هست ولی فرمتش نامعتبره."

        from tenant.gateways.crypto_pay import get_usd_to_toman_rate
        t0 = time.monotonic()
        live_rate = await get_usd_to_toman_rate()
        dt = time.monotonic() - t0

        detail = (
            f"{db_status}\n"
            f"   یه فراخوانیِ واقعیِ همین الان: {dt:.2f} ثانیه طول کشید "
            f"(نرخ: {live_rate:,.0f} تومان).\n"
            f"   اگه dt بالا کمتر از ۰.۵ ثانیه‌ست ولی تویِ ربات هنوز کند می‌بینی، "
            f"یعنی سرویسِ اصلی (نه این اسکریپت) داره از یه DATABASE_URL دیگه "
            f"می‌خونه — چک کن .env سرویسِ اصلی و اسکریپت یکی باشه."
        )
        if dt > 3:
            # === PATCH_RATE_FAIL_CACHE_V1 ===
            # اگه نرخ صفره، یعنی هیچ منبعی جواب نداد — ولی الان این شکست
            # هم کش می‌شه (۵ دقیقه)، پس این اسکریپت رو یه‌بارِ دیگه همین
            # الان اجرا کن؛ اگه دفعه‌ی دوم فوری شد، یعنی خودِ فیکس درست
            # کار می‌کنه و فقط اولین مشتریِ هر ۵ دقیقه این تاخیر رو می‌بینه؛
            # اگه بازم کند بود، یعنی مشکلِ دیگه‌ای (مثلاً DATABASE_URL) هست.
            return False, (
                detail + "\n   ❌ این کند بود. اگه نرخ صفر شد، یعنی هیچ‌کدوم از منابع "
                "از این سرور جواب ندادن (شبکه/فیلترینگ) — این یه‌بار طبیعیه، ولی "
                "الان همین اسکریپت رو یه‌بارِ دیگه فوری اجرا کن: اگه دفعه‌ی دوم "
                "زیرِ نیم‌ثانیه شد، فیکس درست کار می‌کنه (فقط هر ۵ دقیقه یه مشتری "
                "این تاخیر رو می‌بینه، نه همه). اگه بازم کند بود، worker یا "
                "DATABASE_URL رو چک کن: `systemctl status botsaz-tenant-worker`."
            )
        return True, detail
    except Exception as e:
        import traceback
        return False, f"چک شکست خورد: {e!r}\n   {traceback.format_exc()[-500:]}"


async def main():
    checks = [
        ("ایمپورتِ ماژول‌ها", check_imports),
        ("هم‌نسخگیِ plans.py با tenant/smm.py", check_file_versions_match),
        ("صفحه‌ی «مدیریتِ محصولات» بدونِ کرش", check_products_menu_no_crash),
        ("مهاجرتِ دیتابیس (ستون‌های جدید)", check_db_migration),
        ("تشخیصِ ۴۰۴ پنل‌های VPN", check_frontend_404_hint),
        ("چیدمانِ منویِ ارائه‌دهندگان/محصولات/دسته‌ها", check_smm_menu_layout),
        ("سلامتِ کشِ نرخِ ارز (worker)", check_rate_cache_health),
    ]
    results = []
    print("🔎 در حال بررسیِ دیپلوی...\n")
    for title, fn in checks:
        ok, detail = await fn()
        results.append(ok)
        print(f"{_mark(ok)} {title}\n   {detail}\n")

    if all(results):
        print("🎉 همه‌چیز سالمه — دیپلوی تأیید شد.")
        sys.exit(0)
    else:
        print("⚠️ حداقل یه مورد مشکل داره — قبلِ اعلامِ عمومی به کاربرا، بالا رو بخون.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
