"""
bot_change_log.py — PATCH_BOT_CHANGE_LOG_V1

هر تغییرِ مهمِ یک ربات (پلن، نوع، وضعیت، سررسید، مالک، بازه، توکن، …) خودکار و از *یک نقطهٔ مرکزی* (رویدادِ ORM) در
bot_change_log ثبت می‌شود، با «علت» (از روی تابعی که تغییر را داده) و «عامل» (آیدیِ کسی که دکمه را زده).
بنابراین جاهایِ متعددِ تغییر (خریدِ مالک، ادمین، حلقهٔ سررسید، همکار، …) یکی‌یکی دست نمی‌خورند.

  • ثبت هرگز جلویِ تغییرِ اصلی را نمی‌گیرد: داخلِ SAVEPOINT و با try/except (در PostgreSQL خطا نباید تراکنش را بسوزاند).
  • مقدارِ توکن/کلیدِ API هرگز ذخیره نمی‌شود؛ فقط «تغییر کرد».
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

from sqlalchemy import event, inspect, insert

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SELF = {os.path.join(_ROOT, "models.py"), os.path.abspath(__file__)}

# ستون ← برچسبِ فارسی
TRACKED = {
    "plan": "پلن", "field": "نوع ربات", "status": "وضعیت", "lock_reason": "دلیل خاموشی",
    "plan_expires_at": "سررسید پلن", "owner_id": "مالک", "sub_months": "بازهٔ اشتراک (ماه)",
    "banked_plan": "پلنِ ذخیره‌شده", "miniapp_enabled": "مینی‌اپ", "archive_reason": "دلیل بایگانی",
    "username": "یوزرنیم ربات", "token": "توکن", "api_key": "کلید API",
}
_MASKED = {"token", "api_key"}

# تابعِ صدازننده ← علتِ خوانا
REASONS = {
    "_handle_change_field_payment": "تغییر نوع ربات (مالک؛ پرداختِ ⭐)",
    "edt_field_free_apply": "تغییر نوع ربات (مالک؛ رایگان)",
    "edt_field_pay_start": "تغییر نوع ربات (مالک)",
    "adm_bot_field_apply": "تغییر نوع ربات توسطِ ادمینِ پلتفرم",
    "buyplan_confirm": "خرید/تمدیدِ پلن توسطِ مالک",
    "adm_user_days_apply": "افزودن/کسرِ روز توسطِ ادمین (از پروفایلِ کاربر)",
    "adm_bot_days_apply": "افزودن/کسرِ روز توسطِ ادمین",
    "apply_days": "افزودن/کسرِ روز توسطِ ادمین",
    "add_days_to_bot": "افزودنِ روز (پنلِ مدیریتِ سروش)",
    "adm_user_bot_plan_set": "تغییر پلن توسطِ ادمین (از پروفایلِ کاربر)",
    "adm_bot_plan_set": "تغییر پلن توسطِ ادمین",
    "set_bot_plan": "تغییر پلن (پنلِ مدیریتِ سروش)",
    "adm_bots_bulk_revert_go": "برگشتِ گروهیِ ادمین",
    "adm_bot_toggle_duration": "تغییر بازهٔ اشتراک توسطِ ادمین",
    "adm_bot_resume": "فعال‌سازیِ ربات توسطِ ادمین",
    "subscription_loop": "خودکار: سررسید/بازگشت از پلنِ ذخیره‌شده",
    "save_token": "ثبت/تعویضِ توکن",
    "pa_days": "تغییرِ روز توسطِ ادمینِ همکار",
    "pa_days_manual_set": "تنظیمِ دستیِ سررسید توسطِ ادمینِ همکار",
    "adm_bot_miniapp_toggle": "روشن/خاموشِ مینی‌اپ توسطِ ادمین",
    "adm_bot_pause": "غیرفعال‌سازی توسطِ ادمین",
    "inactivity_loop": "خودکار: عدمِ فعالیت",
}

_ACTOR_NAMES = ("cb", "msg", "message", "call", "callback", "callback_query", "q", "m", "event")


def _fmt(col: str, v) -> str | None:
    if col in _MASKED:
        return None if v is None else "••••"
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)[:160]


def _frames() -> list:
    """فریم‌هایِ پایتونِ فعلی + زنجیرهٔ await (SQLAlchemy-async فلاش را داخلِ greenlet اجرا می‌کند؛ پس فریم‌هایِ هندلر در
    greenletِ والد معلق‌اند)."""
    out = []
    f = sys._getframe(2)
    while f is not None:
        out.append(f)
        f = f.f_back
    try:
        import greenlet
        g = greenlet.getcurrent().parent
        hops = 0
        while g is not None and hops < 6:
            f = g.gr_frame
            while f is not None:
                out.append(f)
                f = f.f_back
            g = g.parent
            hops += 1
    except Exception:
        logger.debug("bot_change_log: خواندنِ زنجیرهٔ greenlet ناموفق بود", exc_info=True)
    return out


def _is_mine(fn: str) -> bool:
    return fn.startswith(_ROOT) and fn not in _SELF and "site-packages" not in fn and f"{os.sep}tests{os.sep}" not in fn


def who_and_why() -> tuple[str | None, str | None, int | None]:
    """(علتِ فارسی، src فنی، آیدیِ عامل)."""
    try:
        frames = [f for f in _frames() if _is_mine(f.f_code.co_filename)]
    except Exception:
        return None, None, None
    reason = src = None
    for f in frames:
        name = f.f_code.co_name
        if name in REASONS:
            reason = REASONS[name]
            src = f"{name}@{os.path.basename(f.f_code.co_filename)}"
            break
    if src is None and frames:
        f = frames[0]
        src = f"{f.f_code.co_name}@{os.path.basename(f.f_code.co_filename)}"
        reason = "نامشخص (از مسیرِ " + src + ")"
    actor = None
    try:
        for f in frames:
            loc = f.f_locals
            for n in _ACTOR_NAMES:
                o = loc.get(n)
                u = getattr(o, "from_user", None)
                uid = getattr(u, "id", None)
                if isinstance(uid, int):
                    actor = uid
                    break
            if actor:
                break
    except Exception:
        logger.debug("bot_change_log: یافتنِ عامل ناموفق بود", exc_info=True)
        actor = None
    return reason, src, actor


def install(Bot, BotChangeLog) -> None:
    tbl = BotChangeLog.__table__

    def _write(connection, target, created: bool) -> None:
        try:
            st = inspect(target)
            rows = []
            now = datetime.utcnow()
            reason = src = actor = None
            resolved = False
            for col in TRACKED:
                h = st.attrs[col].history
                if not h.has_changes():
                    continue
                old = h.deleted[0] if h.deleted else None
                new = h.added[0] if h.added else getattr(target, col, None)
                if created:
                    if new in (None, "", False):
                        continue
                    old = None
                if _fmt(col, old) == _fmt(col, new):
                    continue
                if not resolved:
                    reason, src, actor = who_and_why()
                    if created:
                        reason = "ساختِ ربات"
                    resolved = True
                rows.append(dict(bot_id=target.id, ts=now, col=col, old=_fmt(col, old), new=_fmt(col, new),
                                 reason=(reason or "")[:200] or None, src=(src or "")[:100] or None, actor=actor))
            if rows:
                with connection.begin_nested():            # SAVEPOINT: خطایِ ثبت، تراکنشِ اصلی را نمی‌سوزاند
                    connection.execute(insert(tbl), rows)
        except Exception:
            logger.warning("bot_change_log: ثبتِ تغییر ناموفق بود (نادیده گرفته شد)", exc_info=True)

    @event.listens_for(Bot, "after_update")
    def _after_update(mapper, connection, target):          # noqa: ANN001
        _write(connection, target, False)

    @event.listens_for(Bot, "after_insert")
    def _after_insert(mapper, connection, target):          # noqa: ANN001
        _write(connection, target, True)
