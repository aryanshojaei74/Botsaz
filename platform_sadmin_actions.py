# -*- coding: utf-8 -*-
"""
platform_sadmin_actions.py — منطقِ کارهایِ «پنل مدیریتِ سروش» (PATCH_PLATFORM_SADMIN_V1).

فقط دیتابیس/تلگرام؛ هیچ‌چیز از سروش در این فایل نیست (رابطِ سروش: platform_sadmin.py).
همهٔ خروجی‌هایِ متنی «متنِ ساده»اند (سروش HTML نمی‌فهمد)؛ پیام‌هایی که به تلگرامِ کاربران می‌روند HTML‌اند.

کارها: آمار، جستجو/کارتِ ربات، قفل/رفعِ قفل، افزودنِ روز، حذفِ کاملِ ربات، جستجو/پروفایلِ کاربر،
تغییرِ کیف پول، بن/رفعِ بن، مخاطب‌هایِ پیامِ همگانی و اجرای ارسال (قابلِ ازسرگیری).
"""
from __future__ import annotations

import datetime
import html as _html
import re

from sqlalchemy import func, or_, select

from config import Session
from models import (Bot as BotRow, BotUser, PlanOrder, PlatformTxn, PlatformUser, PlatformUserDiscount)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

EXPIRING_DAYS = 5
ARCHIVED_RECENT_DAYS = 7
MAX_RESULTS = 8

PLAN_LABEL = {"free": "رایگان", "growth": "رشد", "pro": "حرفه‌ای", "partner": "همکار"}
STATUS_LABEL = {"active": "🟢 فعال", "paused": "⏸ متوقف", "locked": "🔒 قفل"}


def _n(v) -> str:
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return str(v)


def _d(dt: datetime.datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _ago(dt: datetime.datetime | None, now: datetime.datetime | None = None) -> str:
    if not dt:
        return "—"
    now = now or datetime.datetime.now()
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} دقیقه پیش"
    if mins < 1440:
        return f"{mins // 60} ساعت پیش"
    return f"{mins // 1440} روز پیش"


# ─────────────── آمار ───────────────

async def stats_text(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    d1, d7, d30 = (now - datetime.timedelta(days=k) for k in (1, 7, 30))
    async with Session() as s:
        by_status = dict((await s.execute(select(BotRow.status, func.count()).where(
            BotRow.archived_at.is_(None)).group_by(BotRow.status))).all())
        archived = int(await s.scalar(select(func.count()).select_from(BotRow).where(
            BotRow.archived_at.is_not(None))) or 0)
        by_plan = dict((await s.execute(select(BotRow.plan, func.count()).where(
            BotRow.archived_at.is_(None)).group_by(BotRow.plan))).all())
        by_field = dict((await s.execute(select(BotRow.field, func.count()).where(
            BotRow.archived_at.is_(None)).group_by(BotRow.field))).all())
        expiring = int(await s.scalar(select(func.count()).select_from(BotRow).where(
            BotRow.archived_at.is_(None), BotRow.plan_expires_at.is_not(None),
            BotRow.plan_expires_at >= now,
            BotRow.plan_expires_at <= now + datetime.timedelta(days=EXPIRING_DAYS))) or 0)
        new_bots = [int(await s.scalar(select(func.count()).select_from(BotRow).where(BotRow.created_at >= t)) or 0)
                    for t in (d1, d7)]
        users = int(await s.scalar(select(func.count()).select_from(PlatformUser)) or 0)
        banned = int(await s.scalar(select(func.count()).select_from(PlatformUser).where(
            PlatformUser.is_banned.is_(True))) or 0)
        new_users = [int(await s.scalar(select(func.count()).select_from(PlatformUser).where(
            PlatformUser.created_at >= t)) or 0) for t in (d1, d7)]
        active24 = int(await s.scalar(select(func.count()).select_from(PlatformUser).where(
            PlatformUser.last_active_at >= d1)) or 0)
        wallets = await s.scalar(select(func.coalesce(func.sum(PlatformUser.balance), 0)))
        tenant_users = int(await s.scalar(select(func.count()).select_from(BotUser)) or 0)
        rev = []
        for t in (d1, d7, d30):
            c, a = (await s.execute(select(func.count(), func.coalesce(func.sum(PlanOrder.amount), 0)).where(
                PlanOrder.status == "paid", PlanOrder.created_at >= t))).one()
            rev.append((int(c or 0), a))
    total_bots = sum(by_status.values()) + archived
    lines = ["📊 آمار بات‌ساز", f"🕒 {_d(now)}", "",
             f"🤖 ربات‌ها: {total_bots} (بایگانی: {archived})",
             "   " + " · ".join(f"{STATUS_LABEL.get(k, k)} {v}" for k, v in sorted(by_status.items())),
             "   پلن: " + " · ".join(f"{PLAN_LABEL.get(k or 'free', k)} {v}" for k, v in sorted(by_plan.items(), key=lambda x: str(x[0]))),
             "   زمینه: " + " · ".join(f"{k} {v}" for k, v in sorted(by_field.items(), key=lambda x: str(x[0]))),
             f"   ⏳ در آستانهٔ انقضا ({EXPIRING_DAYS} روز): {expiring}",
             f"   🆕 ساخته‌شده: ۲۴ساعت {new_bots[0]} · ۷روز {new_bots[1]}", "",
             f"👥 کاربرانِ بات‌ساز: {users} (بن: {banned})",
             f"   🆕 ۲۴ساعت {new_users[0]} · ۷روز {new_users[1]} · فعالِ ۲۴ساعت {active24}",
             f"   👤 کاربرانِ نهاییِ ربات‌ها: {tenant_users}", "",
             f"💰 مجموعِ موجودیِ کیف‌پول‌ها: {_n(wallets)} تومان", "",
             "🧾 فروشِ پلن (پرداخت‌شده):",
             f"   ۲۴ساعت: {rev[0][0]} سفارش · {_n(rev[0][1])} تومان",
             f"   ۷روز: {rev[1][0]} سفارش · {_n(rev[1][1])} تومان",
             f"   ۳۰روز: {rev[2][0]} سفارش · {_n(rev[2][1])} تومان"]
    return "\n".join(lines)


# ─────────────── ربات‌ها ───────────────

def bot_line(b: BotRow, now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    st = "📦 بایگانی" if b.archived_at else STATUS_LABEL.get(b.status, b.status)
    exp = ""
    if b.plan_expires_at:
        left = (b.plan_expires_at - now).days
        exp = f" · {left}روز" if left >= 0 else " · منقضی"
    return f"#{b.id} @{b.username or '—'} · {PLAN_LABEL.get(b.plan or 'free', b.plan)} · {st}{exp}"


async def search_bots(q: str, limit: int = MAX_RESULTS) -> list[BotRow]:
    q = (q or "").strip().lstrip("@")
    if not q:
        return []
    async with Session() as s:
        if q.startswith("#") and q[1:].isdigit():
            cond = BotRow.id == int(q[1:])
        elif q.isdigit():
            cond = or_(BotRow.id == int(q), BotRow.owner_id == int(q))
        else:
            cond = BotRow.username.ilike(f"%{q}%")
        return list((await s.scalars(select(BotRow).where(cond).order_by(BotRow.id.desc()).limit(limit))).all())


async def preset_bots(kind: str, now: datetime.datetime | None = None, limit: int = 10) -> list[BotRow]:
    now = now or datetime.datetime.now()
    async with Session() as s:
        q = select(BotRow)
        if kind == "expiring":
            q = q.where(BotRow.archived_at.is_(None), BotRow.plan_expires_at.is_not(None),
                        BotRow.plan_expires_at >= now,
                        BotRow.plan_expires_at <= now + datetime.timedelta(days=EXPIRING_DAYS)
                        ).order_by(BotRow.plan_expires_at.asc())
        elif kind == "archived":
            q = q.where(BotRow.archived_at.is_not(None)).order_by(BotRow.archived_at.desc())
        elif kind == "locked":
            q = q.where(BotRow.status == "locked").order_by(BotRow.id.desc())
        else:
            q = q.order_by(BotRow.id.desc())
        return list((await s.scalars(q.limit(limit))).all())


async def bot_card(bot_id: int, now: datetime.datetime | None = None) -> str | None:
    now = now or datetime.datetime.now()
    async with Session() as s:
        b = await s.get(BotRow, bot_id)
        if not b:
            return None
        owner = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == b.owner_id))
        users = int(await s.scalar(select(func.count()).select_from(BotUser).where(BotUser.bot_id == b.id)) or 0)
    lines = [f"🤖 ربات #{b.id} — @{b.username or '—'}",
             f"زمینه: {b.field} · پلن: {PLAN_LABEL.get(b.plan or 'free', b.plan)}",
             f"وضعیت: {'📦 بایگانی (' + str(b.archive_reason or '—') + ')' if b.archived_at else STATUS_LABEL.get(b.status, b.status)}"]
    if b.status == "locked" and b.lock_reason:
        lines.append(f"دلیلِ قفل: {b.lock_reason}")
    if b.plan_expires_at:
        left = (b.plan_expires_at - now).days
        lines.append(f"سررسید: {_d(b.plan_expires_at)} ({'منقضی' if left < 0 else f'{left} روز مانده'})")
    else:
        lines.append("سررسید: ندارد")
    lines += [f"ساخته‌شده: {_d(b.created_at)} · آخرین فعالیت: {_ago(b.last_activity_at, now)}",
              f"👤 کاربرانِ ربات: {users}",
              f"مالک: {b.owner_id}" + (f" — {owner.full_name or ''} @{owner.username}" if owner and owner.username
                                       else (f" — {owner.full_name}" if owner and owner.full_name else ""))]
    return "\n".join(lines)


async def _invalidate(token: str | None) -> None:
    if not token:
        return
    try:
        import tenant_status_bus
        await tenant_status_bus.publish_tenant_invalidate(token)
    except Exception:
        logger.exception("sadmin: باطل‌کردنِ کشِ وضعیتِ ربات ناموفق بود")


async def lock_bot(bot_id: int, reason: str, tg_bot=None) -> tuple[bool, str]:
    reason = (reason or "").strip()
    if not reason:
        return False, "دلیل نمی‌تواند خالی باشد."
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            return False, "ربات پیدا نشد."
        r.status, r.lock_reason = "locked", reason
        owner, uname, token = r.owner_id, r.username, r.token
        await s.commit()
    await _invalidate(token)
    await _tg_notify(tg_bot, owner, f"🔒 ربات شما (@{_html.escape(uname or '')}) توسط مدیریتِ بات‌ساز قفل شد.\n"
                                    f"📝 دلیل: {_html.escape(reason)}\n\nتا رفعِ قفل، این ربات برایِ کاربرانش در دسترس نیست.")
    return True, f"ربات @{uname} قفل شد."


async def unlock_bot(bot_id: int, tg_bot=None) -> tuple[bool, str]:
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            return False, "ربات پیدا نشد."
        r.status, r.lock_reason = "active", None
        owner, uname, token = r.owner_id, r.username, r.token
        await s.commit()
    await _invalidate(token)
    await _tg_notify(tg_bot, owner, f"🔓 قفلِ ربات شما (@{_html.escape(uname or '')}) برداشته شد و دوباره فعال است.")
    return True, f"قفلِ @{uname} برداشته شد."


async def add_days_to_bot(bot_id: int, days: int, now: datetime.datetime | None = None,
                          tg_bot=None) -> tuple[bool, str]:
    """سررسید = max(سررسیدِ فعلی، الان) + روز؛ رباتِ بایگانی‌شده برمی‌گردد (فعال + وب‌هوک)."""
    if not (1 <= days <= 365):
        return False, "تعدادِ روز باید ۱ تا ۳۶۵ باشد."
    now = now or datetime.datetime.now()
    restored = False
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            return False, "ربات پیدا نشد."
        if r.plan_expires_at is None and r.archived_at is None:
            return False, "این ربات سررسید ندارد (هرگز منقضی نمی‌شود)."
        if r.archived_at is not None:
            r.archived_at, r.archive_reason, r.status, r.lock_reason = None, None, "active", None
            r.plan_expires_at = now + datetime.timedelta(days=days)
            restored = True
        else:
            base = r.plan_expires_at if r.plan_expires_at > now else now
            r.plan_expires_at = base + datetime.timedelta(days=days)
        r.last_expiry_notice = None
        owner, uname, token, new_exp = r.owner_id, r.username, r.token, r.plan_expires_at
        await s.commit()
    await _invalidate(token)
    if restored:
        try:
            from bot_utils import set_tenant_webhook
            await set_tenant_webhook(token)
        except Exception:
            logger.exception("sadmin: ست‌کردنِ وب‌هوکِ رباتِ برگشته ناموفق بود")
    await _tg_notify(tg_bot, owner, f"🎁 {days} روز به پلنِ ربات شما (@{_html.escape(uname or '')}) اضافه شد."
                                    + (" ربات از بایگانی خارج و فعال شد." if restored else ""))
    return True, f"{days} روز به @{uname} اضافه شد → سررسید {_d(new_exp)}" + (" (از بایگانی برگشت)" if restored else "")


async def set_bot_plan(bot_id: int, plan: str, tg_bot=None) -> tuple[bool, str]:
    """تغییرِ دستیِ پلن (بدونِ پرداخت) — مثلِ «🎚 تغییر پلن ربات» در رباتِ تلگرام. رباتِ بایگانی‌شده برمی‌گردد."""
    if plan not in ("free", "growth", "pro"):
        return False, "پلن نامعتبر."
    from plans import PLAN_INFO, Plan
    restored = False
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            return False, "ربات پیدا نشد."
        old = r.plan
        r.plan = plan
        r.banked_plan = r.banked_seconds = r.banked_sub_months = None
        if r.archived_at is not None:
            r.archived_at, r.archive_reason, r.status = None, None, "active"
            r.inactivity_warned_at = None
            restored = True
        owner, uname, token = r.owner_id, r.username, r.token
        await s.commit()
    await _invalidate(token)
    if restored:
        try:
            from bot_utils import set_tenant_webhook
            await set_tenant_webhook(token)
        except Exception:
            logger.exception("sadmin: ست‌کردنِ وب‌هوکِ رباتِ برگشته ناموفق بود")
    if old != plan:
        await _tg_notify(tg_bot, owner, f"🎚 پلنِ ربات @{_html.escape(uname or '')} توسطِ مدیریتِ بات‌ساز از «"
                                        f"{PLAN_INFO[Plan(old)]['title']}» به «{PLAN_INFO[Plan(plan)]['title']}» تغییر کرد."
                                        + ("\n✅ ربات از بایگانی هم خارج و فعال شد." if restored else ""))
    return True, f"پلنِ @{uname} شد «{PLAN_INFO[Plan(plan)]['title']}»" + (" (از بایگانی برگشت)" if restored else "")


async def delete_bot(bot_id: int, reason: str, tg_bot=None) -> tuple[bool, str]:
    """حذفِ کاملِ ربات (برگشت‌ناپذیر). رباتِ همکار/دارایِ فروشِ همکار از اینجا حذف نمی‌شود."""
    from sqlalchemy import text as _text
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            return False, "ربات پیدا نشد."
        owner, uname, token, plan = r.owner_id, r.username, r.token, r.plan
        try:
            pb = (await s.execute(_text("SELECT id FROM partner_bots WHERE bot_id = :b"), {"b": bot_id})).scalar()
            ps = (await s.execute(_text("SELECT id FROM partner_sales WHERE sold_bot_id = :b LIMIT 1"),
                                  {"b": bot_id})).scalar()
        except Exception:
            logger.exception("sadmin: بررسیِ وابستگیِ همکار ناموفق بود")
            pb = ps = None
    if pb is not None or plan == "partner":
        return False, "این ربات همکار است؛ حذفش را از خودِ رباتِ تلگرامیِ بات‌ساز انجام بده (اثرِ زنجیره‌ایِ روی ربات‌هایِ زیرمجموعه دارد)."
    if ps is not None:
        return False, "این ربات به‌عنوانِ فروشِ همکار ثبت شده؛ حذفش را از تلگرام انجام بده."
    try:
        from config import purge_bot
        token = await purge_bot(bot_id) or token
    except Exception as e:
        logger.exception("sadmin: purge_bot ناموفق")
        return False, f"حذف ناموفق بود: {type(e).__name__}: {str(e)[:150]}"
    await _invalidate(token)
    try:
        from bot_utils import _temp_bot
        async with _temp_bot(token) as tb:
            await tb.delete_webhook()
    except Exception:
        logger.exception("sadmin: حذفِ وب‌هوکِ رباتِ حذف‌شده ناموفق بود")
    note = f"🗑 ربات شما (@{_html.escape(uname or '')}) توسط مدیریتِ بات‌ساز حذف شد."
    if reason and reason != "-":
        note += f"\n📝 دلیل: {_html.escape(reason)}"
    await _tg_notify(tg_bot, owner, note)
    return True, f"ربات @{uname} برایِ همیشه حذف شد."


# ─────────────── کاربران و کیف پول ───────────────

async def search_users(q: str, limit: int = MAX_RESULTS) -> list[PlatformUser]:
    q = (q or "").strip().lstrip("@")
    if not q:
        return []
    async with Session() as s:
        if q.lstrip("-").isdigit():
            cond = PlatformUser.telegram_id == int(q)
        else:
            like = f"%{q}%"
            cond = or_(PlatformUser.username.ilike(like), PlatformUser.full_name.ilike(like))
        return list((await s.scalars(select(PlatformUser).where(cond).order_by(PlatformUser.id.desc()).limit(limit))).all())


def user_line(u: PlatformUser) -> str:
    return (f"{u.telegram_id} · {u.full_name or '—'}" + (f" · @{u.username}" if u.username else "")
            + f" · {_n(u.balance)} ت" + (" · 🚫" if u.is_banned else ""))


async def user_card(tg_id: int, now: datetime.datetime | None = None) -> str | None:
    now = now or datetime.datetime.now()
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == tg_id))
        if not u:
            return None
        bots = (await s.scalars(select(BotRow).where(BotRow.owner_id == tg_id).order_by(BotRow.id.desc()).limit(15))).all()
        disc = await s.scalar(select(PlatformUserDiscount).where(
            PlatformUserDiscount.user_telegram_id == tg_id, PlatformUserDiscount.active.is_(True)))
        txns = (await s.scalars(select(PlatformTxn).where(PlatformTxn.user_telegram_id == tg_id)
                                .order_by(PlatformTxn.id.desc()).limit(5))).all()
        refs = int(await s.scalar(select(func.count()).select_from(PlatformUser).where(
            PlatformUser.referrer_id == tg_id)) or 0)
    lines = [f"👤 {u.full_name or '—'}" + (f" (@{u.username})" if u.username else ""),
             f"🆔 {u.telegram_id}",
             f"💰 موجودی: {_n(u.balance)} تومان",
             f"وضعیت: {'🚫 بن‌شده' if u.is_banned else '✅ عادی'}",
             f"عضویت: {_d(u.created_at)} · آخرین فعالیت: {_ago(u.last_active_at, now)}",
             f"معرف: {u.referrer_id or '—'} · دعوت‌شده‌ها: {refs} · اسلاتِ اضافه: {u.extra_bot_slots or 0}"]
    if disc:
        lines.append(f"🏷 تخفیفِ دائمی: {disc.percent}%")
    lines.append(f"\n🤖 ربات‌ها ({len(bots)}):" if bots else "\n🤖 ربات ندارد.")
    lines += [f"  {bot_line(b, now)}" for b in bots]
    if txns:
        lines.append("\n💳 آخرین تراکنش‌ها:")
        lines += [f"  {_d(t.created_at)} · {'+' if float(t.amount) > 0 else ''}{_n(t.amount)} · {t.kind}"
                  + (f" · {t.note}" if t.note else "") for t in txns]
    return "\n".join(lines)


async def adjust_wallet(tg_id: int, delta: int, note: str, tg_bot=None) -> tuple[bool, str]:
    if delta == 0:
        return False, "مبلغ صفر است."
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == tg_id).with_for_update())
        if not u:
            return False, "کاربر پیدا نشد."
        new = int(float(u.balance)) + int(delta)
        if new < 0:
            return False, f"موجودی کافی نیست (موجودی {_n(u.balance)}؛ کسرِ {_n(-delta)} ممکن نیست)."
        u.balance = new
        s.add(PlatformTxn(user_telegram_id=tg_id, amount=delta, kind="admin", note=(note or "تنظیمِ دستیِ مدیر")[:200]))
        await s.commit()
    why = f"\n📝 {_html.escape(note)}" if note else ""
    if delta > 0:
        msg = f"✅ <b>{_n(delta)} تومان</b> به کیف پولت اضافه شد.\n👛 موجودیِ فعلی: <b>{_n(new)} تومان</b>{why}"
    else:
        msg = f"⚠️ <b>{_n(-delta)} تومان</b> از کیف پولت کسر شد.\n👛 موجودیِ فعلی: <b>{_n(new)} تومان</b>{why}"
    await _tg_notify(tg_bot, tg_id, msg)
    return True, f"موجودی {_n(new)} تومان شد ({'+' if delta > 0 else ''}{_n(delta)})."


async def set_ban(tg_id: int, banned: bool) -> tuple[bool, str]:
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == tg_id))
        if not u:
            return False, "کاربر پیدا نشد."
        u.is_banned = banned
        await s.commit()
    return True, "کاربر بن شد." if banned else "بنِ کاربر برداشته شد."


async def _tg_notify(tg_bot, chat_id: int, text: str) -> None:
    if tg_bot is None:
        return
    try:
        await tg_bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.warning("sadmin: پیام به %s ارسال نشد (احتمالاً ربات را بلاک کرده)", chat_id)


# ─────────────── پیامِ همگانی ───────────────

AUDIENCES = [
    ("all", "همهٔ کاربرانِ بات‌ساز"),
    ("owners", "مالکانِ ربات (همهٔ وضعیت‌ها)"),
    ("owners_active", "مالکانِ رباتِ فعال"),
    ("expiring", f"مالکانِ ربات در آستانهٔ انقضا (≤{EXPIRING_DAYS} روز)"),
    ("archived", f"مالکانِ رباتِ بایگانی‌شده ({ARCHIVED_RECENT_DAYS} روزِ اخیر)"),
    ("recent30", "فعال در ۳۰ روزِ اخیر"),
    ("plan:free", "مالکانِ پلنِ رایگان"),
    ("plan:growth", "مالکانِ پلنِ رشد"),
    ("plan:pro", "مالکانِ پلنِ حرفه‌ای"),
]
AUDIENCE_LABEL = dict(AUDIENCES)


async def audience_ids(key: str, now: datetime.datetime | None = None) -> list[int]:
    """آیدیِ تلگرامیِ مخاطب‌ها (بدونِ تکرار و بدونِ بن‌شده‌ها)."""
    now = now or datetime.datetime.now()
    async with Session() as s:
        banned = set((await s.scalars(select(PlatformUser.telegram_id).where(PlatformUser.is_banned.is_(True)))).all())
        if key == "all":
            ids = (await s.scalars(select(PlatformUser.telegram_id))).all()
        elif key == "recent30":
            ids = (await s.scalars(select(PlatformUser.telegram_id).where(
                PlatformUser.last_active_at >= now - datetime.timedelta(days=30)))).all()
        else:
            q = select(BotRow.owner_id)
            if key == "owners_active":
                q = q.where(BotRow.archived_at.is_(None), BotRow.status == "active")
            elif key == "expiring":
                q = q.where(BotRow.archived_at.is_(None), BotRow.plan_expires_at.is_not(None),
                            BotRow.plan_expires_at >= now,
                            BotRow.plan_expires_at <= now + datetime.timedelta(days=EXPIRING_DAYS))
            elif key == "archived":
                q = q.where(BotRow.archived_at >= now - datetime.timedelta(days=ARCHIVED_RECENT_DAYS))
            elif key.startswith("plan:"):
                q = q.where(BotRow.archived_at.is_(None), BotRow.plan == key.split(":", 1)[1])
            elif key != "owners":
                return []
            ids = (await s.scalars(q)).all()
    return sorted({int(i) for i in ids if i is not None and int(i) not in banned})


_MD_RULES = [(re.compile(r"\*\*(.+?)\*\*", re.S), r"<b>\1</b>"), (re.compile(r"__(.+?)__", re.S), r"<i>\1</i>"),
             (re.compile(r"~~(.+?)~~", re.S), r"<s>\1</s>"), (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
             (re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)"), r'<a href="\2">\1</a>')]


def render_text(raw: str) -> str:
    """متنِ سروش → HTMLِ تلگرام. «html:» اولِ متن = تگ‌هایِ تلگرام عیناً؛ وگرنه فرمتِ ساده:
    **بولد**  __ایتالیک__  ~~خط‌خورده~~  `کد`  [متن](https://لینک)."""
    raw = raw or ""
    if raw.startswith("html:"):
        return raw[5:].lstrip()
    t = _html.escape(raw, quote=False)
    for rx, rep in _MD_RULES:
        t = rx.sub(rep, t)
    return t


def parse_button(line: str) -> tuple[str, str] | None:
    """«متنِ دکمه | https://…» ⇒ (متن، لینک)."""
    if "|" not in (line or ""):
        return None
    label, url = (x.strip() for x in line.split("|", 1))
    if label and re.match(r"^https?://\S+$", url) and len(label) <= 40:
        return label, url
    return None


async def run_broadcast(job: dict, tg_bot, save, should_stop, progress=None, sleep=None,
                        batch: int = 20) -> dict:
    """job: {ids, idx, sent, failed, src_chat, src_msg, button:[label,url]|None, status}.
    پیام را با copy_message (پشتیبانیِ همهٔ انواعِ رسانه) برایِ هر نفر می‌فرستد؛ از idx ادامه می‌دهد
    (برایِ ازسرگیری بعد از ری‌استارت). save(job) هر batch ذخیره می‌کند؛ should_stop() لغو را می‌گوید."""
    import asyncio
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    sleep = sleep or asyncio.sleep
    kb = None
    if job.get("button"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=job["button"][0], url=job["button"][1])]])
    ids = job["ids"]
    while job["idx"] < len(ids):
        if await should_stop():
            job["status"] = "cancelled"
            break
        uid = ids[job["idx"]]
        for attempt in (1, 2):
            try:
                await tg_bot.copy_message(chat_id=uid, from_chat_id=job["src_chat"], message_id=job["src_msg"],
                                          reply_markup=kb)
                job["sent"] += 1
                break
            except TelegramRetryAfter as e:
                await sleep(min(int(getattr(e, "retry_after", 5) or 5), 60))
                if attempt == 2:
                    job["failed"] += 1
            except Exception:
                job["failed"] += 1
                break
        job["idx"] += 1
        await sleep(0.05)
        if job["idx"] % batch == 0:
            await save(job)
            if progress:
                await progress(job)
    else:
        job["status"] = "done"
    await save(job)
    return job
