#!/usr/bin/env python3
"""
scripts/diag_discount.py — تشخیصِ فقط‌خواندنیِ «دکمهٔ کد تخفیف نیست» (PATCH_DIAG_DISCOUNT_V1).

برایِ رباتِ داده‌شده نشان می‌دهد: پلن/ویژگیِ کد تخفیف، تیکِ «استفاده از کد تخفیف در خرید»، و هر کدِ تخفیف
(فعال؟ منقضی؟ تمام‌شده؟ محدود به کاربر/دکمه؟). دکمهٔ «🏷 کد تخفیف» فقط وقتی در فاکتور می‌آید که تیک روشن
و دست‌کم یک کدِ is_active باشد.

    venv/bin/python scripts/diag_discount.py --bot-username <یوزرنیم یا بخشی از آن>
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow, DiscountCode, Setting  # noqa: E402
from plans import Feature, has_feature  # noqa: E402


async def main(q: str) -> None:
    now = dt.datetime.utcnow()
    async with Session() as s:
        bots = (await s.execute(select(BotRow).where(BotRow.username.ilike(f"%{q}%")))).scalars().all()
        if not bots:
            print("رباتی پیدا نشد.")
            return
        for b in bots:
            tog = await s.scalar(select(Setting.value).where(Setting.bot_id == b.id, Setting.key == "discount_purchase_enabled"))
            print(f"\n=== @{b.username} id={b.id} field={b.field} plan={b.plan} status={b.status} plan_end={b.plan_expires_at}")
            print(f"  ویژگیِ کد تخفیف در پلن: {'بله' if has_feature(b.plan, Feature.DISCOUNT_CODES) else 'خیر ⚠️'}")
            print(f"  تیکِ «استفاده از کد تخفیف در خرید»: {'روشن' if tog in (None, '1') else 'خاموش ⚠️'} (مقدار={tog!r})")
            codes = (await s.execute(select(DiscountCode).where(DiscountCode.bot_id == b.id).order_by(DiscountCode.id))).scalars().all()
            act = 0
            for c in codes:
                flags = []
                if not c.is_active:
                    flags.append("غیرفعال")
                if c.expires_at and c.expires_at < now:
                    flags.append("منقضی")
                if c.max_uses and c.used_count >= c.max_uses:
                    flags.append("تمام‌شده")
                if c.restricted_user_id:
                    flags.append(f"فقط کاربر {c.restricted_user_id}")
                if c.button_id:
                    flags.append(f"فقط دکمهٔ {c.button_id}")
                if c.is_active:
                    act += 1
                print(f"  • {c.code}: {c.used_count}/{c.max_uses or '∞'} {' | '.join(flags) or 'سالم'}")
            print(f"  کدِ is_active: {act} از {len(codes)} ⇒ دکمهٔ فاکتور {'می‌آید' if act and tog in (None, '1') else 'نمی‌آید ⚠️'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-username", required=True)
    asyncio.run(main(ap.parse_args().bot_username))
