"""
partner_restore_expired.py — بازگرداندنِ ربات‌های همکاری که بابتِ «انقضایِ
دوره‌ای» (نه حدِ نصاب) خوابانده شده‌اند.

=== PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
تا پیش از این، رباتِ همکار در لحظهٔ فعال‌سازی یک plan_expires_atِ ۳۰ روزه
می‌گرفت و سرِ همان تاریخ توسطِ subscription_loop بایگانی می‌شد یا توسطِ
partner_rules_loop غیرفعال. حالا که مدلِ همکاری بی‌انقضا شده، این اسکریپت
قربانی‌هایِ همان قاعده را برمی‌گرداند.

عمداً محافظه‌کار است:
  • فقط ردیف‌هایی که plan == "partner" هستند
  • فقط آن‌هایی که دلیلِ بایگانی‌شان nonpayment/انقضا بوده
  • همکارانی که مدیر دستی paused کرده (lock_reason دربارهٔ حدِ نصاب) دست
    نمی‌خورند

اجرا:
    python3 scripts/partner_restore_expired.py --dry     # فقط گزارش
    python3 scripts/partner_restore_expired.py --apply   # اعمال
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from config import Session, init_db  # noqa: E402
from models import Bot as BotRow, PartnerBot  # noqa: E402


async def main(apply: bool) -> None:
    await init_db()
    fixed_bots, fixed_pbs = [], []
    async with Session() as s:
        pbs = (await s.scalars(select(PartnerBot))).all()
        for pb in pbs:
            br = await s.get(BotRow, pb.bot_id)
            if not br or (br.plan or "") != "partner":
                continue

            # ربات همکاری که بابتِ حدِ نصاب خوابیده — دست نمی‌زنیم
            reason = (br.lock_reason or "")
            if "حد نصاب" in reason or "کمتر از" in reason:
                continue

            changed = False
            if br.plan_expires_at is not None:
                br.plan_expires_at = None
                changed = True
            if br.archived_at is not None and (br.archive_reason or "") in (
                    "nonpayment", "expired", None, ""):
                br.archived_at = None
                br.archive_reason = None
                changed = True
            if br.status != "active" and pb.status != "paused":
                br.status = "active"
                br.lock_reason = None
                changed = True
            if changed:
                fixed_bots.append((br.id, br.username, br.owner_id))

            if pb.expires_at is not None:
                pb.expires_at = None
                if pb.status == "expired":
                    pb.status = "active"
                fixed_pbs.append(pb.id)

        if apply:
            await s.commit()
        else:
            await s.rollback()

    print(f"ربات‌های همکارِ بازگردانده‌شده: {len(fixed_bots)}")
    for bid, uname, owner in fixed_bots:
        print(f"  • #{bid} @{uname} (مالک {owner})")
    print(f"ردیف‌های PartnerBot پاک‌شده از انقضا: {len(fixed_pbs)}")
    if not apply:
        print("\n(حالتِ آزمایشی — چیزی ذخیره نشد. برای اعمال: --apply)")


if __name__ == "__main__":
    _apply = "--apply" in sys.argv
    asyncio.run(main(_apply))
