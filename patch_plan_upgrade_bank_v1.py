#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_plan_upgrade_bank_v1.py — بستنِ باگِ «رشد ۲ ماهه ← ارتقا به حرفه‌ای = ۳ ماه حرفه‌ای».

علت: در buyplan_confirm (platform_bot.py) موقعِ ارتقا، سررسیدِ پلنِ قبلی
(base_time = row.plan_expires_at) با ۳۰×ماهِ پلنِ جدید جمع می‌شد؛ یعنی
باقی‌ماندهٔ پلنِ «رشد» به‌شکلِ زمانِ «حرفه‌ای» تبدیل می‌شد.

رفتارِ جدید:
  • ارتقا از رشد به حرفه‌ای (وقتی رشد هنوز اعتبار دارد): حرفه‌ای فقط به‌اندازهٔ
    ماه(های) خریداری‌شده (از همین لحظه) اعمال می‌شود و باقی‌ماندهٔ رشد
    «نگه‌داری» می‌شود (banked_plan / banked_seconds / banked_sub_months).
  • وقتی حرفه‌ای تمام شد، subscription_loop به‌جای بایگانی، ربات را به رشد با
    همان زمانِ نگه‌داری‌شده برمی‌گرداند (و به مالک پیام می‌دهد).
  • تمدیدِ همان پلن، خریدِ پلنِ هم‌سطح/پایین‌تر و ... مثلِ قبل کار می‌کند؛ فقط
    زمانِ نگه‌داری‌شده گم نمی‌شود و دوبار هم حساب نمی‌شود.
  • تغییرِ دستیِ پلن توسطِ ادمین، زمانِ نگه‌داری‌شده را پاک می‌کند.

ستون‌های جدید را init_db خودکار اضافه می‌کند (ALTER TABLE ... ADD COLUMN).

استفاده (داخلِ پوشهٔ پروژه، کنارِ platform_bot.py):
    python3 patch_plan_upgrade_bank_v1.py           # dry-run
    python3 patch_plan_upgrade_bank_v1.py --apply   # اعمال + بک‌آپ (*.bak_before_plan_bank_v1)
بعدش: سرویسِ platform را ری‌استارت کن (main_platform + worker). idempotent است.
"""
import ast
import shutil
import sys

MARKER = "PATCH_PLAN_UPGRADE_BANK_V1"
APPLY = "--apply" in sys.argv

PLAN_BANK_PY = '''# -*- coding: utf-8 -*-
"""
plan_bank.py — === PATCH_PLAN_UPGRADE_BANK_V1 ===
منطقِ خالصِ «نگه‌داریِ زمانِ پلنِ قبلی هنگامِ ارتقا» (بدونِ وابستگی به دیتابیس).
"""
import datetime

_RANK = {"free": 0, "growth": 1, "pro": 2}


def compute_purchase(*, old_plan, new_plan, old_expires, was_archived,
                     old_sub_months, banked_plan, banked_seconds,
                     banked_months, months, now):
    """نتیجهٔ خرید/تمدید/ارتقا را برمی‌گرداند (dict).

    کلیدها: expires, banked_plan, banked_seconds, banked_months, banked_now
    (banked_now=True یعنی همین خرید یک زمانِ تازه نگه‌داری کرده).
    """
    old_plan = old_plan or "free"
    step = datetime.timedelta(days=30 * months)
    old_valid = bool(old_expires and old_expires > now and not was_archived)

    # ارتقا از پلنِ پولیِ در حالِ اعتبار به پلنِ بالاتر → باقی‌مانده نگه‌داری شود
    if old_valid and _RANK.get(old_plan, 0) >= 1 \\
            and _RANK.get(new_plan, 0) > _RANK.get(old_plan, 0):
        remaining = max(int((old_expires - now).total_seconds()), 0)
        return {"expires": now + step, "banked_plan": old_plan,
                "banked_seconds": remaining,
                "banked_months": int(old_sub_months or 1), "banked_now": True}

    base = old_expires if old_valid else now
    extra = 0
    b_plan, b_sec, b_mon = banked_plan, banked_seconds, banked_months
    if b_plan and (b_sec or 0) > 0:
        if new_plan == old_plan:
            pass  # تمدیدِ همان پلن: زمانِ نگه‌داری‌شده دست‌نخورده می‌ماند
        elif b_plan == new_plan:
            extra = int(b_sec)  # پلنِ نگه‌داری‌شده را دوباره می‌خرد: جمع می‌شود
            b_plan = b_sec = b_mon = None
        else:
            b_plan = b_sec = b_mon = None
    else:
        b_plan = b_sec = b_mon = None
    return {"expires": base + step + datetime.timedelta(seconds=extra),
            "banked_plan": b_plan, "banked_seconds": b_sec,
            "banked_months": b_mon, "banked_now": False}
'''

# ---------------------------------------------------------------- edits
EDITS = [
    # ---- models.py: ستون‌های جدید
    ("models.py", 1,
'''    sub_months: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
''',
'''    sub_months: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # === PATCH_PLAN_UPGRADE_BANK_V1 ===
    # وقتی ربات از رشد به حرفه‌ای ارتقا می‌یابد، باقی‌ماندهٔ رشد (ثانیه) اینجا
    # نگه‌داری می‌شود و بعد از تمام‌شدنِ حرفه‌ای به رشد برمی‌گردد.
    banked_plan: Mapped[str | None] = mapped_column(String(20))
    banked_seconds: Mapped[int | None] = mapped_column(Integer)
    banked_sub_months: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
'''),

    # ---- platform_bot.py: خرید/ارتقا
    ("platform_bot.py", 1,
'''        was_archived = row.archived_at is not None
        row.plan = plan
        # تمدید سررسید: اگر پلن فعلی هنوز اعتبار دارد، از انتهای همان ادامه پیدا می‌کند
        now = datetime.datetime.now()
        base_time = row.plan_expires_at if (row.plan_expires_at and row.plan_expires_at > now
                                            and not was_archived) else now
        new_expiry = base_time + datetime.timedelta(days=30 * months)
        row.plan_expires_at = new_expiry
''',
'''        was_archived = row.archived_at is not None
        # === PATCH_PLAN_UPGRADE_BANK_V1 ===
        # قبلاً: سررسیدِ پلنِ قبلی + ۳۰×ماهِ پلنِ جدید → باقی‌ماندهٔ «رشد» تبدیل به
        # زمانِ «حرفه‌ای» می‌شد (سوءاستفاده). حالا در ارتقا فقط ماهِ خریداری‌شده
        # حرفه‌ای است و باقی‌ماندهٔ پلنِ قبلی نگه‌داری می‌شود.
        from plan_bank import compute_purchase as _cp
        now = datetime.datetime.now()
        _res = _cp(old_plan=row.plan, new_plan=plan,
                   old_expires=row.plan_expires_at, was_archived=was_archived,
                   old_sub_months=row.sub_months, banked_plan=row.banked_plan,
                   banked_seconds=row.banked_seconds,
                   banked_months=row.banked_sub_months, months=months, now=now)
        row.plan = plan
        new_expiry = _res["expires"]
        row.plan_expires_at = new_expiry
        row.banked_plan = _res["banked_plan"]
        row.banked_seconds = _res["banked_seconds"]
        row.banked_sub_months = _res["banked_months"]
        _bank_note = ""
        if _res["banked_now"] and (_res["banked_seconds"] or 0) > 0:
            _bd = max(1, round(_res["banked_seconds"] / 86400))
            _bank_note = (
                f"\\n\\n⏳ {_bd} روز از پلنِ «{PLAN_INFO[Plan(_res['banked_plan'])]['title']}» "
                "شما نگه‌داری شد و بعد از پایانِ این دوره خودکار برمی‌گردد.")
'''),
    ("platform_bot.py", 1,
'''        f"{restored}📅 سررسید بعدی: {to_jalali(new_expiry)}{ptc_note}{_admin_block_note}"
''',
'''        f"{restored}📅 سررسید بعدی: {to_jalali(new_expiry)}{ptc_note}{_bank_note}{_admin_block_note}"
'''),

    # ---- platform_bot.py: تغییرِ دستیِ پلن توسطِ ادمین (۲ هندلر)
    ("platform_bot.py", 2,
'''        was_archived = r.archived_at is not None
        r.plan = new_plan
''',
'''        was_archived = r.archived_at is not None
        r.plan = new_plan
        # === PATCH_PLAN_UPGRADE_BANK_V1 === تغییرِ دستیِ ادمین، زمانِ نگه‌داری‌شده را پاک می‌کند
        r.banked_plan = None
        r.banked_seconds = None
        r.banked_sub_months = None
'''),

    # ---- main_platform.py: برگشت به پلنِ نگه‌داری‌شده به‌جای بایگانی
    ("main_platform.py", 1,
'''                          if now > deadline:
                              b.status = "paused"
                              b.archived_at = now
                              # === PATCH_TRIAL_ARCHIVE_V1 ===
''',
'''                          # === PATCH_PLAN_UPGRADE_BANK_V1 ===
                          # حرفه‌ای تمام شد و زمانِ رشد نگه‌داری شده → بایگانی نکن؛
                          # ربات را به رشد با زمانِ باقی‌مانده برگردان.
                          if now > deadline and b.banked_plan and (b.banked_seconds or 0) > 0:
                              _from_plan = b.plan
                              b.plan = b.banked_plan
                              b.plan_expires_at = now + datetime.timedelta(seconds=int(b.banked_seconds))
                              b.sub_months = b.banked_sub_months or 1
                              _left_days = max(1, round(int(b.banked_seconds) / 86400))
                              b.banked_plan = None
                              b.banked_seconds = None
                              b.banked_sub_months = None
                              b.last_expiry_notice = None
                              token = b.token
                              _to_plan = b.plan
                              await s.commit()
                              await tenant_status_bus.publish_tenant_invalidate(token)
                              try:
                                  from plans import Plan as _Pl, PLAN_INFO as _PI
                                  await platform_bot.send_message(
                                      b.owner_id,
                                      f"ℹ️ دورهٔ پلنِ «{_PI[_Pl(_from_plan)]['title']}» ربات "
                                      f"@{b.username} تمام شد و به پلنِ «{_PI[_Pl(_to_plan)]['title']}» "
                                      f"برگشت؛ {_left_days} روز از آن باقی مانده است.")
                              except Exception:
                                  logger.exception("اعلانِ برگشت به پلنِ نگه‌داری‌شده ناموفق بود")
                              continue
                          if now > deadline:
                              b.status = "paused"
                              b.archived_at = now
                              # === PATCH_TRIAL_ARCHIVE_V1 ===
'''),
]


def main():
    plan = {}
    for path, expect, old, new in EDITS:
        src = plan.get(path)
        if src is None:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        if new in src:
            continue  # همین ویرایش قبلاً اعمال شده (idempotent)
        n = src.count(old)
        if n != expect:
            sys.exit(f"❌ {path}: الگو {n} بار پیدا شد (انتظار {expect}). "
                     "فایلِ سرور با نسخهٔ تحلیل‌شده فرق دارد؛ چیزی نوشته نشد.")
        plan[path] = src.replace(old, new)

    if not plan:
        print("✅ قبلاً اعمال شده؛ کاری نمانده.")
        return
    for path, src in plan.items():
        ast.parse(src, filename=path)  # اعتبارسنجیِ سینتکس قبل از نوشتن
    print("✔ ast.parse برای", ", ".join(plan), "OK")
    if not APPLY:
        print("dry-run: هیچ فایلی نوشته نشد. برای اعمال --apply بزن.")
        return
    ast.parse(PLAN_BANK_PY)
    with open("plan_bank.py", "w", encoding="utf-8") as f:
        f.write(PLAN_BANK_PY)
    for path, src in plan.items():
        shutil.copy2(path, path + ".bak_before_plan_bank_v1")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
    print("✅ اعمال شد. سرویسِ platform را ری‌استارت کن.")


if __name__ == "__main__":
    main()
