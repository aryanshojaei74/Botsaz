#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_dup_autofix_patch.py — پچِ dup_bot_watch_loop رو رویِ main_platform.py
خودِ سرور اعمال می‌کنه، فقط اگه متنِ فعلیِ فایل دقیقاً همونیه که این پچ
روش تست شده. اگه یه کاراکتر هم فرق داشته باشه، هیچی نمی‌نویسه و می‌گه
چرا (safe: مثلِ str_replace).

استفاده:
    cd /opt/botsaz
    python3 apply_dup_autofix_patch.py           # فقط چک می‌کنه (dry-run)
    python3 apply_dup_autofix_patch.py --apply   # واقعاً می‌نویسه

قبلش خودش یه بک‌آپِ main_platform.py.bak_before_dupfix می‌سازه.
"""
import sys

OLD_IMPORT = 'from models import Bot as BotRow\nfrom models import Setting'
NEW_IMPORT = 'from models import Bot as BotRow\nfrom models import Setting\nfrom models import PlatformSetting  # === PATCH_DUP_BOT_AUTOFIX_V1 ==='
OLD_BLOCK = '# === PATCH_DUP_ARCHIVED_TWIN_V1 ===\nasync def dup_bot_watch_loop(interval_min: int = 60):\n    """نگهبانِ ربات\u200cهایِ تکراری.\n\n    گاردِ ساخت جلویِ ساختِ ردیفِ دوم را می\u200cگیرد، ولی اگر از مسیرِ دیگری\n    (مهاجرتِ توکن، بازیابیِ بکاپ، دستکاریِ دستی) باز هم تکراری ساخته شود،\n    این حلقه ظرفِ یک ساعت پیدایش می\u200cکند و به مدیر خبر می\u200cدهد — به\u200cجای\n    اینکه ماه\u200cها بماند تا کاربر شکایت کند.\n\n    عمداً خودکار ادغام نمی\u200cکند: ادغام باید تصمیمِ آگاهانه باشد (کدام منو\n    بماند)، و ادغامِ خودکارِ اشتباه از خودِ تکراری بدتر است.\n    """\n    await asyncio.sleep(90)\n    _seen: set[str] = set()\n    while True:\n        try:\n            async with Session() as s:\n                rows = (await s.execute(\n                    select(func.lower(BotRow.username), func.count(BotRow.id))\n                    .where(BotRow.username.is_not(None),\n                           func.coalesce(BotRow.archive_reason, "") != "merged_duplicate")\n                    .group_by(func.lower(BotRow.username))\n                    .having(func.count(BotRow.id) > 1))).all()\n            for uname, cnt in rows:\n                if uname in _seen:\n                    continue\n                _seen.add(uname)\n                async with Session() as s:\n                    dups = (await s.scalars(select(BotRow).where(\n                        func.lower(BotRow.username) == uname,\n                        func.coalesce(BotRow.archive_reason, "") != "merged_duplicate"\n                    ).order_by(BotRow.id))).all()\n                    detail = "\\n".join(\n                        f"• id={b.id} — {\'🗄 بایگانی\' if b.archived_at else \'🟢 فعال\'}"\n                        f" — پلن {b.plan} — ساخت {b.created_at:%Y-%m-%d}"\n                        for b in dups)\n                logger.warning("ربات تکراری: @%s (%s ردیف)", uname, cnt)\n                if platform_bot:\n                    try:\n                        await platform_bot.send_message(\n                            PLATFORM_ADMIN_ID,\n                            f"⚠️ <b>رباتِ تکراری پیدا شد</b>\\n\\n"\n                            f"🤖 @{uname} — {cnt} ردیف\\n{detail}\\n\\n"\n                            f"برایِ ادغام:\\n"\n                            f"<code>venv/bin/python scripts/dupbots.py show "\n                            f"--keep {dups[0].id} --drop {dups[-1].id}</code>",\n                            parse_mode="HTML")\n                    except Exception:\n                        logger.exception("اطلاعِ رباتِ تکراری به مدیر ناموفق بود")\n        except Exception:\n            logger.exception("dup_bot_watch_loop error")\n        await asyncio.sleep(interval_min * 60)'
NEW_BLOCK = '# === PATCH_DUP_ARCHIVED_TWIN_V1 ===\n# === PATCH_DUP_BOT_AUTOFIX_V1 ===\n# گزارشِ ادمین: این هشدار هر بار که سرویس ری\u200cاستارت می\u200cشد (چون _seen فقط\n# تویِ حافظه بود) و هر ساعت هم که سرویس بالا می\u200cماند، برایِ همون\n# موردهایِ حل\u200cنشده دوباره می\u200cاومد — و چون ابزارِ ادغام (dupbots.py)\n# دستوریه، ادمین مجبور بود دستی رو سرور بزنه.\n#\n# حالا این حلقه اول خودش تلاش می\u200cکنه: اگه دقیقاً یکی از توکن\u200cهایِ گروه\n# زنده باشه (و مالکِ همه\u200cشون یکی باشه)، خودکار با dupbots.py ادغام\n# می\u200cکنه — همون گاردهایِ امنیتیِ خودِ dupbots.py (چک\u200cکردنِ توکنِ زنده،\n# چک\u200cکردنِ یکی\u200cبودنِ مالک) رعایت می\u200cشه، فقط دیگه نیازی به دستِ ادمین\n# نیست. فقط برایِ موردهایِ واقعاً مبهم (هیچ توکنی زنده نیست، یا بیش از\n# یکی زنده\u200cست) به ادمین خبر می\u200cده — و اون\u200cوقت هم، به\u200cجایِ حافظه\u200cیِ\n# ناپایدار، تویِ platform_settings ثبت می\u200cکنه که آخرین\u200cبار کِی خبر داده،\n# تا با هر ری\u200cاستارت دوباره اسپم نشه (کول\u200cداونِ ۲۴ ساعته).\n_DUP_ALERT_COOLDOWN_HOURS = 24\n\n\nasync def _dup_autofix_group(uname: str, ids: list[int]) -> str | None:\n    """اگه گروه بدونِ ابهامه، خودکار ادغام می\u200cکنه و خلاصه\u200cاش را برمی\u200cگرداند.\n    اگه مبهم بود (نیاز به تصمیمِ دستی)، None برمی\u200cگرداند."""\n    import os as _os\n    import sys as _sys\n    _scripts_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "scripts")\n    if _scripts_dir not in _sys.path:\n        _sys.path.insert(0, _scripts_dir)\n    import dupbots as _D  # noqa: E402\n\n    async with Session() as s:\n        conn = await s.connection()\n        rows = {bid: await _D.bot_row(conn, bid) for bid in ids}\n    if len(rows) < 2 or any(r is None for r in rows.values()):\n        return None\n    if len({r[2] for r in rows.values()}) > 1:\n        return None  # مالک\u200cها فرق دارن — دستی بررسی شود\n    alive = {bid: await _D.token_alive(r[3]) for bid, r in rows.items()}\n    if any(a is None for a in alive.values()):\n        return None  # شبکه جواب نداد — بعداً دوباره امتحان می\u200cشود\n    alive_ids = [bid for bid, a in alive.items() if a]\n    if len(alive_ids) != 1:\n        return None  # صفر یا بیش از یکی زنده — نیازِ تصمیمِ آدمیزاد\n    keep = alive_ids[0]\n    drops = [bid for bid in ids if bid != keep]\n    for d in drops:\n        await _D.cmd_merge(keep, d, menu_from=keep, apply=True)\n    return f"@{uname}: خودکار ادغام شد (keep={keep}, drop={drops})"\n\n\nasync def _dup_alert_recently_sent(uname: str) -> bool:\n    async with Session() as s:\n        row = await s.scalar(select(PlatformSetting).where(\n            PlatformSetting.key == f"dup_alert:{uname}"))\n        if not row:\n            return False\n        try:\n            last = datetime.datetime.fromisoformat(row.value)\n        except Exception:\n            return False\n        return (datetime.datetime.utcnow() - last) < datetime.timedelta(\n            hours=_DUP_ALERT_COOLDOWN_HOURS)\n\n\nasync def _dup_alert_mark_sent(uname: str) -> None:\n    async with Session() as s:\n        row = await s.scalar(select(PlatformSetting).where(\n            PlatformSetting.key == f"dup_alert:{uname}"))\n        now_iso = datetime.datetime.utcnow().isoformat()\n        if row:\n            row.value = now_iso\n        else:\n            s.add(PlatformSetting(key=f"dup_alert:{uname}", value=now_iso))\n        await s.commit()\n\n\nasync def dup_bot_watch_loop(interval_min: int = 60):\n    """نگهبانِ ربات\u200cهایِ تکراری.\n\n    گاردِ ساخت جلویِ ساختِ ردیفِ دوم را می\u200cگیرد، ولی اگر از مسیرِ دیگری\n    (مهاجرتِ توکن، بازیابیِ بکاپ، دستکاریِ دستی) باز هم تکراری ساخته شود،\n    این حلقه ظرفِ یک ساعت پیدایش می\u200cکند.\n\n    موردهایِ بدونِ ابهام (دقیقاً یک توکنِ زنده، مالکِ یکسان) را خودش\n    خودکار ادغام می\u200cکند. فقط موردهایِ واقعاً مبهم را به مدیر خبر می\u200cدهد —\n    چون ادغامِ خودکارِ حدسی از خودِ تکراری بدتر است.\n    """\n    await asyncio.sleep(90)\n    while True:\n        try:\n            async with Session() as s:\n                rows = (await s.execute(\n                    select(func.lower(BotRow.username), func.count(BotRow.id))\n                    .where(BotRow.username.is_not(None),\n                           func.coalesce(BotRow.archive_reason, "") != "merged_duplicate")\n                    .group_by(func.lower(BotRow.username))\n                    .having(func.count(BotRow.id) > 1))).all()\n            for uname, cnt in rows:\n                async with Session() as s:\n                    dups = (await s.scalars(select(BotRow).where(\n                        func.lower(BotRow.username) == uname,\n                        func.coalesce(BotRow.archive_reason, "") != "merged_duplicate"\n                    ).order_by(BotRow.id))).all()\n                ids = [b.id for b in dups]\n\n                try:\n                    fixed = await _dup_autofix_group(uname, ids)\n                except Exception:\n                    fixed = None\n                    logger.exception("ادغامِ خودکارِ @%s ناموفق بود", uname)\n                if fixed:\n                    logger.warning("ربات تکراری خودکار ادغام شد: %s", fixed)\n                    continue\n\n                logger.warning("ربات تکراری (نیازِ تصمیمِ دستی): @%s (%s ردیف)", uname, cnt)\n                if await _dup_alert_recently_sent(uname):\n                    continue  # === PATCH_DUP_BOT_AUTOFIX_V1 === کول\u200cداون، اسپم نشه\n                detail = "\\n".join(\n                    f"• id={b.id} — {\'🗄 بایگانی\' if b.archived_at else \'🟢 فعال\'}"\n                    f" — پلن {b.plan} — ساخت {b.created_at:%Y-%m-%d}"\n                    for b in dups)\n                if platform_bot:\n                    try:\n                        await platform_bot.send_message(\n                            PLATFORM_ADMIN_ID,\n                            f"⚠️ <b>رباتِ تکراریِ مبهم</b> (نتونستم خودکار حلش کنم)\\n\\n"\n                            f"🤖 @{uname} — {cnt} ردیف\\n{detail}\\n\\n"\n                            f"یا هیچ توکنی زنده نیست، یا بیش از یکی زنده\u200cست، یا "\n                            f"مالک\u200cها فرق دارن — این\u200cها رو فقط دستی می\u200cشه تصمیم گرفت.\\n"\n                            f"برایِ بررسی:\\n"\n                            f"<code>venv/bin/python scripts/dupbots.py show "\n                            f"--keep {dups[0].id} --drop {dups[-1].id}</code>\\n\\n"\n                            f"<i>تا {_DUP_ALERT_COOLDOWN_HOURS} ساعتِ دیگه دوباره یادآوری نمی\u200cشه.</i>",\n                            parse_mode="HTML")\n                        await _dup_alert_mark_sent(uname)\n                    except Exception:\n                        logger.exception("اطلاعِ رباتِ تکراری به مدیر ناموفق بود")\n        except Exception:\n            logger.exception("dup_bot_watch_loop error")\n        await asyncio.sleep(interval_min * 60)\n'

def main():
    apply = "--apply" in sys.argv
    path = "main_platform.py"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    problems = []
    if text.count(OLD_IMPORT) != 1:
        problems.append("خطِ ایمپورت پیدا نشد یا تکراریه (count=%d)" % text.count(OLD_IMPORT))
    if text.count(OLD_BLOCK) != 1:
        problems.append("بلوکِ dup_bot_watch_loop دقیقاً مطابقِ نسخه‌ی موردِ انتظار نیست (count=%d)" % text.count(OLD_BLOCK))

    if problems:
        print("❌ فایل با نسخه‌ای که این پچ روش تست شده فرق داره:")
        for p in problems:
            print("   -", p)
        print()
        print("چیزی نوشته نشد. این یعنی حتماً یه ویرایشِ دیگه هم رویِ")
        print("main_platform.py انجام شده که من ندیدمش — بفرست بررسی کنیم،")
        print("به‌جایِ اینکه کورکورانه جایگزین کنیم.")
        return

    print("✅ فایل دقیقاً مطابقِ انتظاره؛ پچ قابلِ اعماله.")
    if not apply:
        print("این dry-run بود. برایِ نوشتنِ واقعی: --apply")
        return

    import shutil
    shutil.copy(path, path + ".bak_before_dupfix")
    print("   بک‌آپ گرفته شد: %s.bak_before_dupfix" % path)

    new_text = text.replace(OLD_IMPORT, NEW_IMPORT, 1)
    new_text = new_text.replace(OLD_BLOCK, NEW_BLOCK, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("   نوشته شد: %s" % path)
    print()
    print("حالا:")
    print('   ./venv/bin/python3 -c "import main_platform"   # تست importِ خشک')
    print("   systemctl restart botsaz-platform")
    print("   systemctl status botsaz-platform --no-pager")

if __name__ == "__main__":
    main()
