#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_partner_panel_support_access.py — رفعِ باگِ «پنلِ مدیریتِ رباتِ
همکار برایِ ادمینِ پشتیبانی نشون داده نمی‌شه».

علتِ باگ:
برایِ ربات‌هایِ معمولی، همه‌جایِ پنلِ مدیریت با تابعِ `is_owner_or_admin`
چک می‌شه که هم مالک و هم هر کسی که در جدولِ AdminSupport ثبت شده باشه
(یعنی همون «دسترسیِ پشتیبانی» که از پنلِ پلتفرم فعال می‌کنی) رو قبول
می‌کنه.

اما ربات‌هایی که با پلنِ «partner» ساخته می‌شن (رباتِ همکار/نمایندگی) یک
پنلِ کاملاً جدا دارن (tenant/partner_menu.py و tenant/partner_panel.py)
که هر دو یک تابعِ `_partner_ok` جداگونه دارن — و اون تابع فقط این رو چک
می‌کنه: `uid != row.owner_id`. یعنی هیچ‌وقت AdminSupport رو نگاه نمی‌کنه.

نتیجه: وقتی «دسترسیِ پشتیبانی» رو برایِ یه رباتِ همکار فعال می‌کنی و وارد
اون ربات می‌شی، پیامِ اولیهٔ «پنل مدیریت» رو می‌بینی (چون اون بخش درست از
is_owner_or_admin استفاده می‌کنه)، ولی رویِ هر دکمه‌ای که بزنی (تنظیمات،
مالی، آمار، تیکت و ...) هیچ اتفاقی نمی‌افته — چون خودِ اون دکمه‌ها از
`_partner_ok` استفاده می‌کنن که تو رو مالک نمی‌شناسه. از دیدِ کاربر انگار
اصلاً پنل باز نمی‌شه.

این پچ هر دو نسخهٔ `_partner_ok` رو طوری عوض می‌کنه که مثلِ بقیهٔ پنل، از
`is_owner_or_admin` استفاده کنن — یعنی هم مالک و هم هر کسی که دسترسیِ
پشتیبانی براش فعال شده، بتونه وارد بشه.

استفاده:
    cd /opt/botsaz
    python3 patch_partner_panel_support_access.py           # dry-run
    python3 patch_partner_panel_support_access.py --apply   # اعمال واقعی

بک‌آپ‌ها: tenant/partner_menu.py.bak_before_supportfix و
tenant/partner_panel.py.bak_before_supportfix
اجرایِ دوباره بعد از اعمال بی‌خطر است (idempotent).
"""
import sys

MARKER = "MARKER_PARTNER_PANEL_SUPPORT_ACCESS_V1"

MENU_PATH = "tenant/partner_menu.py"
MENU_OLD = '''async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    if cb_or_msg.from_user.id != row.owner_id:
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb:
        return None
    return row, pb'''
MENU_NEW = f'''async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    # === {MARKER} ===
    # قبلاً فقط مالکِ دقیق قبول می‌شد؛ ادمینِ پشتیبانیِ ثبت‌شده در
    # AdminSupport (همون «دسترسیِ پشتیبانی» که از پنلِ پلتفرم فعال
    # می‌شه) هیچ‌وقت رد نمی‌شد — برخلافِ بقیهٔ پنلِ مدیریت که با
    # is_owner_or_admin چک می‌شه.
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    if not await is_owner_or_admin(row.id, cb_or_msg.from_user.id, row.owner_id):
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb:
        return None
    return row, pb'''

PANEL_PATH = "tenant/partner_panel.py"
PANEL_OLD = '''async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    """فقط مالکِ یک رباتِ همکارِ فعال اجازهٔ ورود دارد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    uid = cb_or_msg.from_user.id
    if uid != row.owner_id:
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb or pb.status != "active":
        return None
    return row, pb'''
PANEL_NEW = f'''async def _partner_ok(cb_or_msg, bot: Bot) -> tuple[BotRow, PartnerBot] | None:
    """مالکِ یک رباتِ همکارِ فعال، یا ادمینِ پشتیبانیِ ثبت‌شده براش،
    اجازهٔ ورود دارد. {MARKER}: قبلاً فقط مالکِ دقیق قبول می‌شد و
    AdminSupport («دسترسیِ پشتیبانی») هیچ‌وقت رد نمی‌شد."""
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or row.plan != "partner":
        return None
    uid = cb_or_msg.from_user.id
    if not await is_owner_or_admin(row.id, uid, row.owner_id):
        return None
    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == row.id))
    if not pb or pb.status != "active":
        return None
    return row, pb'''


def _apply_one(path, old, new, backup_suffix):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if MARKER in text:
        return "already", None
    c = text.count(old)
    if c != 1:
        return "mismatch", c
    return "ok", text.replace(old, new, 1)


def main():
    apply = "--apply" in sys.argv

    results = {}
    for label, path, old, new in (
        ("menu", MENU_PATH, MENU_OLD, MENU_NEW),
        ("panel", PANEL_PATH, PANEL_OLD, PANEL_NEW),
    ):
        status, payload = _apply_one(path, old, new, None)
        results[label] = (status, payload, path, old, new)

    if all(r[0] == "already" for r in results.values()):
        print("✅ این پچ قبلاً اعمال شده؛ کاری برای انجام نیست.")
        return

    problems = []
    for label, (status, payload, path, old, new) in results.items():
        if status == "mismatch":
            problems.append(f"{path}: بلوکِ _partner_ok پیدا نشد یا تکراریه (count={payload})")
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
    import py_compile

    written = []
    for label, (status, payload, path, old, new) in results.items():
        if status == "already":
            continue
        backup = path + ".bak_before_supportfix"
        shutil.copy(path, backup)
        print("   بک‌آپ گرفته شد: %s" % backup)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        print("   نوشته شد: %s" % path)
        written.append((path, backup))

    try:
        for path, _ in written:
            py_compile.compile(path, doraise=True)
        print("   py_compile: OK ✅")
    except py_compile.PyCompileError as e:
        print("   ❌ py_compile خطا داد، برگرداندنِ بک‌آپ‌ها:")
        print(e)
        for path, backup in written:
            shutil.copy(backup, path)
        return

    print()
    print("بعدش سرویسِ tenant را ری‌استارت کن:")
    print("    systemctl restart botsaz-tenant")


if __name__ == "__main__":
    main()
