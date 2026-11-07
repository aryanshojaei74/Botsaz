# -*- coding: utf-8 -*-
"""
platform_add_days.py — «افزودنِ روز به همهٔ ربات‌ها» (PATCH_PLATFORM_ADD_DAYS_V1).

برایِ جبرانِ قطعی: مدیرِ پلتفرم در رباتِ اصلی /addays را می‌زند، تعدادِ روز را می‌دهد، پیش‌نمایشِ
دقیق (چند ربات، چند بایگانی‌شده) را می‌بیند و با یک دکمه اعمال می‌کند.

قواعد:
  • فقط رباتِ فعالِ دارایِ سررسید (plan_expires_at) — رباتِ بدونِ سررسید هرگز منقضی نمی‌شود، دست نمی‌خورد.
  • سررسیدِ جدید = max(سررسیدِ فعلی، همین‌الان) + روز (همان قاعدهٔ scripts/expiry_report.py --grant).
  • اختیاری: رباتی که «به‌خاطرِ عدمِ پرداخت/تست» در N ساعتِ اخیر بایگانی شده (احتمالاً همین قطعی) از بایگانی
    برمی‌گردد (فعال + وب‌هوک)، با سررسیدِ «از همین‌الان + روز».
  • اختیاری: خبر به مالکِ هر ربات.
  • ربات‌هایِ ادغام‌شده (merged_duplicate) و بایگانیِ دستیِ مدیر/قفل‌شده هرگز تغییر نمی‌کنند.
فقط PLATFORM_ADMIN_ID.
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import Bot as BotRow, PlatformSetting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_add_days_router = Router()

MAX_DAYS = 365
RESTORE_HOURS = 120                    # بایگانیِ ۵ روزِ اخیر
RESTORE_REASONS = ("nonpayment", "trial_expired")
LOG_KEY = "adddays_last_run"
DOUBLE_RUN_MINUTES = 10


class AddDaysFSM(StatesGroup):
    days = State()


def _skip(b: BotRow) -> bool:
    return b.archive_reason == "merged_duplicate" or "#merged" in (b.username or "")


async def plan_targets(now: datetime.datetime, restore_hours: int = RESTORE_HOURS) -> dict:
    """{active: [Bot…] (فعال و دارایِ سررسید)، restore: [Bot…] (بایگانیِ اخیرِ عدمِ پرداخت)}."""
    async with Session() as s:
        bots = (await s.scalars(select(BotRow))).all()
    active, restore = [], []
    cutoff = now - datetime.timedelta(hours=restore_hours)
    for b in bots:
        if _skip(b):
            continue
        if b.archived_at is None:
            if b.plan_expires_at is not None and b.status in ("active", "paused"):
                active.append(b)
        elif b.archived_at >= cutoff and b.archive_reason in RESTORE_REASONS and b.status != "locked":
            restore.append(b)
    return {"active": active, "restore": restore}


async def apply_days(days: int, include_restore: bool, now: datetime.datetime | None = None,
                     restore_hours: int = RESTORE_HOURS) -> dict:
    """روزها را اعمال می‌کند؛ {"extended": n, "restored": n, "tokens_restored": [...], "owners": [(owner, username, days)]}."""
    now = now or datetime.datetime.now()
    plan = await plan_targets(now, restore_hours)
    delta = datetime.timedelta(days=days)
    out = {"extended": 0, "restored": 0, "tokens_restored": [], "tokens": [], "owners": []}
    async with Session() as s:
        for t in plan["active"]:
            b = await s.get(BotRow, t.id)
            if b is None or b.plan_expires_at is None or b.archived_at is not None:
                continue
            base = b.plan_expires_at if b.plan_expires_at > now else now
            b.plan_expires_at = base + delta
            b.last_expiry_notice = None
            out["extended"] += 1
            out["tokens"].append(b.token)
            out["owners"].append((b.owner_id, b.username, days))
        if include_restore:
            for t in plan["restore"]:
                b = await s.get(BotRow, t.id)
                if b is None or b.archived_at is None:
                    continue
                b.archived_at = None
                b.archive_reason = None
                b.status = "active"
                b.lock_reason = None
                b.plan_expires_at = now + delta
                b.last_expiry_notice = None
                out["restored"] += 1
                out["tokens_restored"].append(b.token)
                out["tokens"].append(b.token)
                out["owners"].append((b.owner_id, b.username, days))
        await s.commit()
    return out


async def _after_apply(res: dict) -> None:
    """کشِ وضعیتِ مستأجرها را باطل می‌کند و وب‌هوکِ رباتِ برگشته از بایگانی را دوباره ست می‌کند."""
    try:
        import tenant_status_bus
        for tok in res["tokens"]:
            await tenant_status_bus.publish_tenant_invalidate(tok)
    except Exception:
        logger.exception("add_days: باطل‌کردنِ کشِ وضعیت ناموفق بود")
    try:
        from bot_utils import set_tenant_webhook
        for tok in res["tokens_restored"]:
            try:
                await set_tenant_webhook(tok)
            except Exception:
                logger.exception("add_days: ست‌کردنِ وب‌هوک ناموفق بود")
    except Exception:
        logger.exception("add_days: ایمپورتِ set_tenant_webhook ناموفق بود")


async def _notify_owners(bot, owners: list[tuple], days: int) -> tuple[int, int]:
    ok = fail = 0
    seen = set()
    for owner_id, username, _ in owners:
        key = (owner_id, username)
        if key in seen:
            continue
        seen.add(key)
        try:
            await bot.send_message(
                owner_id,
                f"🎁 به‌دلیلِ اختلالِ اخیر، <b>{days} روز</b> به پلنِ ربات @{_html.escape(username or '')} اضافه شد. "
                "از صبوریتان سپاسگزاریم 🙏", parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


# ─────────────── رابطِ مدیر ───────────────

def _is_admin(user_id: int | None) -> bool:
    return bool(PLATFORM_ADMIN_ID) and user_id == PLATFORM_ADMIN_ID


def _kb(restore: bool, notify: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if restore else '⬜️'} برگرداندنِ بایگانیِ ۵ روزِ اخیر", callback_data="adays:restore")],
        [InlineKeyboardButton(text=f"{'✅' if notify else '⬜️'} اطلاع به مالکِ ربات‌ها", callback_data="adays:notify")],
        [InlineKeyboardButton(text="🚀 اعمال", callback_data="adays:go", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="✖️ لغو", callback_data="adays:cancel", style=ButtonStyle.DANGER)],
    ])


async def _preview(days: int, restore: bool, notify: bool) -> tuple[str, InlineKeyboardMarkup]:
    plan = await plan_targets(datetime.datetime.now())
    text = (f"➕ <b>افزودنِ {days} روز به ربات‌ها</b>\n\n"
            f"• رباتِ فعالِ دارایِ سررسید: <b>{len(plan['active'])}</b> ← سررسید +{days} روز\n"
            f"• بایگانی‌شدهٔ ۵ روزِ اخیر (عدمِ پرداخت/تست): <b>{len(plan['restore'])}</b>"
            + (" ← برمی‌گردند و از امروز " + str(days) + " روز اعتبار می‌گیرند" if restore else " (دست‌نخورده می‌مانند)")
            + f"\n• اطلاع به مالک‌ها: {'بله' if notify else 'خیر'}\n\n"
            "ربات‌هایِ بدونِ سررسید، قفل‌شده و ادغام‌شده تغییر نمی‌کنند.\nبعد از «اعمال» برگشتی نیست.")
    return text, _kb(restore, notify)


@platform_add_days_router.message(Command("addays"), F.func(lambda m: _is_admin(getattr(m.from_user, "id", None))))
async def ad_cmd(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddDaysFSM.days)
    await msg.answer(f"چند روز به همهٔ ربات‌ها اضافه شود؟ (عددِ ۱ تا {MAX_DAYS})\nلغو: /cancel")


@platform_add_days_router.message(AddDaysFSM.days, F.text, F.func(lambda m: _is_admin(m.from_user.id)))
async def ad_days(msg: Message, state: FSMContext):
    t = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if t == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if not t.isdigit() or not (1 <= int(t) <= MAX_DAYS):
        return await msg.answer(f"❌ یک عددِ ۱ تا {MAX_DAYS} بفرست (یا /cancel).")
    days = int(t)
    await state.update_data(days=days, restore=False, notify=False)
    text, kb = await _preview(days, False, False)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_add_days_router.callback_query(F.data.startswith("adays:"), F.func(lambda c: _is_admin(c.from_user.id)))
async def ad_cb(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":", 1)[1]
    data = await state.get_data()
    days = data.get("days")
    if act == "cancel" or not days:
        await state.clear()
        try:
            await cb.message.edit_text("لغو شد." if act == "cancel" else "جلسه منقضی شد؛ دوباره /addays بزن.")
        except Exception:
            pass
        return await cb.answer()
    restore, notify = bool(data.get("restore")), bool(data.get("notify"))
    if act in ("restore", "notify"):
        restore = (not restore) if act == "restore" else restore
        notify = (not notify) if act == "notify" else notify
        await state.update_data(restore=restore, notify=notify)
        text, kb = await _preview(days, restore, notify)
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        return await cb.answer()
    if act == "go":
        last = await _get_log()
        if last and datetime.datetime.now() - last < datetime.timedelta(minutes=DOUBLE_RUN_MINUTES):
            await cb.answer(f"⚠️ کمتر از {DOUBLE_RUN_MINUTES} دقیقه پیش یک بار اعمال شد؛ جلوگیری از دوباره‌زدنِ اشتباهی.",
                            show_alert=True)
            return
        await state.clear()
        await _set_log(datetime.datetime.now())
        await cb.answer("در حال اعمال…")
        res = await apply_days(days, restore)
        await _after_apply(res)
        summary = (f"✅ <b>انجام شد</b>\n\n• سررسیدِ {res['extended']} ربات {days} روز تمدید شد\n"
                   f"• {res['restored']} ربات از بایگانی برگشت")
        if notify:
            ok, fail = await _notify_owners(cb.bot, res["owners"], days)
            summary += f"\n• اطلاع به مالک‌ها: {ok} موفق، {fail} ناموفق"
        try:
            await cb.message.edit_text(summary, parse_mode="HTML")
        except Exception:
            await cb.message.answer(summary, parse_mode="HTML")


async def _get_log() -> datetime.datetime | None:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == LOG_KEY))
    try:
        return datetime.datetime.fromisoformat(row.value) if row and row.value else None
    except ValueError:
        return None


async def _set_log(ts: datetime.datetime) -> None:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == LOG_KEY))
        if row:
            row.value = ts.isoformat()
        else:
            s.add(PlatformSetting(key=LOG_KEY, value=ts.isoformat()))
        await s.commit()
