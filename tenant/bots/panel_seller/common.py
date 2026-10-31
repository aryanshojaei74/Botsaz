"""common.py — ابزارهایِ مشترکِ رباتِ پنل‌فروشی (PATCH_PANEL_SELLER_V1)."""
from __future__ import annotations

from datetime import datetime

from tenant.bots.giveaway.common import (HOME_ROW, back_row, btn, edit_or_send, esc, fa, fmt_time, kb,  # noqa: F401
                                         is_admin)

FIELD_KEY = "panel_seller"
STATUS_FA = {"active": "🟢 فعال", "suspended": "🔴 معلق", "deleted": "🗑 حذف‌شده"}
REASON_FA = {"expired": "پایانِ مهلت", "traffic": "اتمامِ حجم", "owner": "تعلیقِ مدیر"}


def n(v) -> str:
    return fa(f"{int(v or 0):,}")


def gb_text(v) -> str:
    return "نامحدود" if not v else f"{fa(int(v))} گیگ"


def users_text(v) -> str:
    return "نامحدود" if not v else f"{fa(int(v))} کاربر"


def pk_line(pk) -> str:
    return (f"📦 <b>{esc(pk.title)}</b> — {gb_text(pk.data_gb)} · {users_text(pk.max_users)} · "
            f"{fa(pk.days)} روز · {n(pk.price_toman)} تومان")


async def sales_gate(row) -> tuple[bool, str]:
    """فروش باز است؟ (پلنِ حرفه‌ای + دسترسیِ تستِ مالکِ ربات). متنِ دلیل برایِ نمایش."""
    from tenant.bots.panel_seller import gate
    from plans import Plan
    if (row.plan or "") != Plan.PRO.value:
        return False, "این نوعِ ربات فقط در پلنِ حرفه‌ای کار می‌کند."
    ok, why = await gate.status(row.owner_id)
    if ok:
        return True, ""
    if why == "expired":
        return False, "مدتِ تستِ این ربات تمام شده؛ برایِ ادامه با پشتیبانیِ بات‌ساز هماهنگ کن."
    return False, "این نوعِ ربات هنوز در مرحلهٔ تست است و برایِ این حساب فعال نشده."


async def status_line(row) -> str:
    from tenant.bots.panel_seller import gate
    from plans import Plan
    d = await gate.grants()
    exp = d.get(row.owner_id)
    ok, _ = await gate.status(row.owner_id)
    if ok:
        until = "دائمی" if not exp else fmt_time(datetime.fromisoformat(exp))
        beta = f"🧪 <b>در حالِ تست</b> — دسترسی تا: {until}"
    else:
        beta = "🧪 <b>در حالِ تست</b> — ❌ دسترسیِ فعالی ندارد (فروش بسته است)"
    plan = "✅ حرفه‌ای" if (row.plan or "") == Plan.PRO.value else "⚠️ نیازمندِ پلنِ حرفه‌ای"
    return f"{beta}\n💎 پلن: {plan}"
