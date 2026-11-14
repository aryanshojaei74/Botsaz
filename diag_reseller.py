#!/usr/bin/env python3
"""
تشخیصِ مستقیمِ باگِ «نمایندگیِ خودکار یهو قیمتِ اصلی شد».
این اسکریپت از داخلِ خودِ پوشهٔ /opt/botsaz اجرا می‌شه (به config/models/plans
خودِ اپ نیاز داره)، هیچ چیزی رو تغییر نمی‌ده — فقط می‌خونه.

اجرا:
    cd /opt/botsaz
    python3 diag_reseller.py <bot_id یا username یوزرنیمِ ربات در تلگرام>

اگه بات‌آیدی رو نمی‌دونی، بدونِ آرگومان اجرا کن تا لیستِ همهٔ ربات‌ها با
پلن‌شون رو نشون بده.
"""
import asyncio
import sys


async def main():
    from config import Session
    from models import Bot as BotRow
    from sqlalchemy import select
    from plans import Feature, has_feature
    from tenant.shared.reseller import get_config, parse_tiers
    from tenant import get_setting

    async with Session() as s:
        rows = (await s.scalars(select(BotRow))).all()

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg:
        target = None
        for r in rows:
            if str(r.id) == arg or (getattr(r, "username", "") or "").lower() == arg.lower().lstrip("@"):
                target = r
                break
        if not target:
            print(f"❌ رباتی با شناسه/یوزرنیمِ «{arg}» پیدا نشد.")
            return
        candidates = [target]
    else:
        candidates = rows

    print("=" * 70)
    for r in candidates:
        allows = has_feature(r.plan, Feature.RESELLER_TIERS)
        print(f"🤖 bot_id={r.id}  username=@{getattr(r, 'username', '?')}  plan={r.plan!r}")
        print(f"   RESELLER_TIERS مجاز است؟ → {allows}")
        if not allows:
            print("   ⚠️  اینجا مشکله: با این پلن، reseller.plan_allows() همیشه False "
                  "برمی‌گردونه و reseller_percent() هم برای همهٔ کاربرا 0 می‌شه — "
                  "یعنی تخفیفِ نمایندگی خاموش می‌شه، حتی اگه پله‌ها هنوز تو تنظیمات باشن.")
        enabled, tiers, basis = await get_config(r.id)
        raw_tiers = await get_setting(r.id, "reseller_tiers", "")
        print(f"   reseller_enabled (Setting) → {await get_setting(r.id, 'reseller_enabled', '0')}")
        print(f"   reseller_tiers (خام)       → {raw_tiers!r}")
        print(f"   get_config() نهایی         → enabled={enabled}, basis={basis}, tiers={tiers}")
        print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
