"""
platform_stats_share.py — «📣 آمارِ قابلِ‌اشتراک» برایِ کانالِ بات‌ساز (PATCH_STATS_SHARE_V1).

درخواستِ ادمین: یک دکمه در صفحهٔ «📊 آمار» که بگوید تا الان چند ربات ساخته شده، چند تست، چند حرفه‌ای، چند رشد
و چند رایگان — با قالبی که مستقیم در کانال شیر شود. متنِ نتیجه یک پیامِ تمیز و بدونِ دکمه است که
بشود فوروارد/کپی کرد؛ دکمه‌هایِ «بروزرسانی» و «بازگشت» در پیامِ جدا می‌آیند تا فورواردِ آن، شلوغ نشود.

تعریف‌ها (همه از دیتابیسِ زنده):
  • ربات‌هایِ ساخته‌شده = همهٔ ردیف‌هایِ ربات (فعال + بایگانی؛ رباتی که کاملاً حذف شده دیگر در دیتابیس نیست).
  • حرفه‌ای / رشد / رایگان = پلنِ «فعلیِ» ربات؛ کنارِ هرکدام تعدادِ «خریدِ پلن تا امروز» هم می‌آید چون
    رباتِ منقضی‌شده به پلنِ پایه برمی‌گردد و شمارشِ فعلی تنها، خریدهایِ گذشته را کم نشان می‌دهد.
  • تست = درخواست‌هایِ تستِ پلن که به ساختِ ربات رسیده (یا هنوز فعال است).
"""
from __future__ import annotations

import datetime
import html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, PlanOrder, PlatformUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

stats_share_router = Router(name="stats_share")

_FIELD_FA = {
    "vpn_sales": "فروش VPN",
    "funstat": "Fun Stat",
    "escrow_bot": "واسطه‌گری",   # PATCH_ESCROW_BOT_KIND_V1
    "virtual_service": "خدمات مجازی",
    "digital_shop": "فروشگاه دیجیتال",
    "booking": "نوبت‌دهی",
    "community_mgmt": "مدیریت گروه و کانال",
    "ad_bot": "تبلیغات",
}


def _n(v) -> str:
    return f"{int(v or 0):,}"


async def build_share_text(bot_username: str | None) -> str:
    from plan_trials import PlanTrialRequest, ensure_trials_table
    await ensure_trials_table()
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(BotRow)) or 0)
        active = int(await s.scalar(select(func.count()).select_from(BotRow).where(
            BotRow.status == "active", BotRow.archived_at.is_(None))) or 0)
        by_plan = dict((p or "free", int(c)) for p, c in (await s.execute(
            select(BotRow.plan, func.count()).group_by(BotRow.plan))).all())
        by_field = [(f, int(c)) for f, c in (await s.execute(
            select(BotRow.field, func.count()).group_by(BotRow.field).order_by(func.count().desc()))).all()]
        users = int(await s.scalar(select(func.count()).select_from(PlatformUser)) or 0)
        orders = dict((p, int(c)) for p, c in (await s.execute(
            select(PlanOrder.plan, func.count()).group_by(PlanOrder.plan))).all())
        trials_built = int(await s.scalar(select(func.count()).select_from(PlanTrialRequest).where(
            PlanTrialRequest.bot_id.is_not(None))) or 0)
        trials_total = int(await s.scalar(select(func.count()).select_from(PlanTrialRequest).where(
            PlanTrialRequest.status.in_(("approved", "used", "expired")))) or 0)
    try:
        import jdatetime
        today = jdatetime.datetime.fromgregorian(
            datetime=datetime.datetime.utcnow() + datetime.timedelta(hours=3, minutes=30)).strftime("%Y/%m/%d")
    except Exception:
        today = datetime.date.today().isoformat()

    pro, growth, free = by_plan.get("pro", 0), by_plan.get("growth", 0), by_plan.get("free", 0)
    lines = [
        "📊 <b>آمارِ بات‌ساز</b>",
        f"🗓 {today}",
        "",
        f"🤖 ربات‌هایِ ساخته‌شده: <b>{_n(total)}</b>",
        f"🟢 فعال در همین لحظه: <b>{_n(active)}</b>",
        "",
        f"💼 پلنِ حرفه‌ای: <b>{_n(pro)}</b> ربات  ·  خرید تا امروز: {_n(orders.get('pro', 0))}",
        f"🌱 پلنِ رشد: <b>{_n(growth)}</b> ربات  ·  خرید تا امروز: {_n(orders.get('growth', 0))}",
        f"🆓 پلنِ رایگان: <b>{_n(free)}</b> ربات",
        f"🧪 تست‌هایِ داده‌شده: <b>{_n(trials_total)}</b>"
        + (f"  ·  تبدیل‌شده به ربات: {_n(trials_built)}" if trials_built else ""),
    ]
    if by_field:
        lines += ["", "🧩 به تفکیکِ نوعِ ربات:"]
        for f, c in by_field[:6]:
            lines.append(f"▫️ {_FIELD_FA.get(f, f or 'سایر')}: {_n(c)}")
    lines += ["", f"👥 کاربرانِ بات‌ساز: <b>{_n(users)}</b>"]
    if bot_username:
        lines += ["", f"🔗 @{html.escape(bot_username)}"]
    return "\n".join(lines)


@stats_share_router.callback_query(F.data.in_({"adm:share_stats", "adm:share_stats:r"}))
async def adm_share_stats(cb: CallbackQuery, bot: Bot):
    from platform_bot import has_perm
    if not await has_perm(cb.from_user.id, "adm:stats"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await cb.answer("⏳")
    me = await bot.get_me()
    text = await build_share_text(me.username)
    # پیامِ ۱: متنِ تمیزِ قابلِ فوروارد (بدونِ دکمه) — پیامِ ۲: کنترل‌ها
    await cb.message.answer(text)
    await cb.message.answer(
        "☝️ همین پیام را فوروارد کن یا کپی کن و در کانال بگذار.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 بروزرسانی آمار", callback_data="adm:share_stats:r",
                                  style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⬅️ بازگشت به آمار", callback_data="adm:stats",
                                  style=ButtonStyle.DANGER)]]))
