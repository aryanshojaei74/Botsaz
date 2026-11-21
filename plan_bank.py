# -*- coding: utf-8 -*-
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
    if old_valid and _RANK.get(old_plan, 0) >= 1 \
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
