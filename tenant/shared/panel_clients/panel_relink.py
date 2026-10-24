"""=== PATCH_PANEL_RELINK_V1 ===

«بازسازیِ لینک‌ها پس از تعویضِ پنل» — ویژهٔ پلنِ حرفه‌ای.

چرا جدا از «🔄 بروزرسانی اینباندها و ساب‌لینک کاربران» (tvpn:refresh_subs):
آن دکمه لینک‌ها را در دیتابیس بروز می‌کند ولی کاربران خبردار نمی‌شوند —
یعنی بعد از تعویضِ پنل، کاربر تا وقتی خودش وارد «کانفیگ‌های من» نشود با
لینکِ مرده می‌ماند. این ماژول همان بازخوانی را انجام می‌دهد و بعد به
تک‌تکِ کاربرانی که لینکشان *واقعاً عوض شده* پیامِ لینکِ جدید می‌فرستد.

نکتهٔ مهمِ سناریو: ربات اکانت‌ها را با `email` (شناسهٔ کلاینت) می‌شناسد،
نه آیدیِ داخلیِ پنل. پس اگر روی پنلِ جدید کلاینت‌ها با همان emailها
ساخته شوند، همین‌جا دوباره پیدا و وصل می‌شوند.
"""

import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import PanelAccount
from plans import Feature, has_feature, upgrade_hint
from tenant.bots.vpn.vpn_admin import (
    PANEL_LABELS, _bk, _owner, get_panel, is_panel_key,
)
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

panel_relink_router = Router()

# فاصلهٔ بینِ پیام‌ها تا به محدودیتِ تلگرام نخوریم (۳۰ پیام در ثانیه)
_NOTIFY_SLEEP = 0.05

from tenant.shared.panel_clients.rate_limit import (   # PATCH_RELINK_RATE_LIMIT_V1
    _DEFAULT_RETRY_WAIT, _MAX_RETRIES, _MAX_RETRY_WAIT, _PACE_SLEEP, _get_client_retry, _retry_wait_from,
)


def relink_button(key: str, plan: str) -> list[InlineKeyboardButton] | None:
    """دکمهٔ این قابلیت برای کیبوردِ مدیریتِ پنل. برایِ پلن‌های پایین‌تر
    دکمهٔ قفل‌شده برمی‌گردد (نه حذفِ بی‌صدا) تا مدیر بداند وجود دارد."""
    if has_feature(plan, Feature.PANEL_RELINK):
        return [InlineKeyboardButton(
            text="🔁 بازسازیِ لینک‌ها پس از تعویضِ پنل",
            callback_data=f"tvpn:relink:{key}", style=ButtonStyle.PRIMARY)]
    return [InlineKeyboardButton(
        text="🔒 بازسازیِ لینک‌ها (ویژهٔ پلن حرفه‌ای)",
        callback_data="tvpn:relink_locked", style=ButtonStyle.DANGER)]


@panel_relink_router.callback_query(F.data == "tvpn:relink_locked")
async def relink_locked(cb: CallbackQuery):
    await cb.answer(upgrade_hint(Feature.PANEL_RELINK), show_alert=True)


@panel_relink_router.callback_query(F.data.startswith("tvpn:relink:"))
async def relink_confirm(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if not is_panel_key(key):
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    if not has_feature(row.plan, Feature.PANEL_RELINK):
        await cb.answer(upgrade_hint(Feature.PANEL_RELINK), show_alert=True)
        return
    label = PANEL_LABELS.get(key, key)
    async with Session() as s:
        n = await s.scalar(
            select(PanelAccount.id).where(
                PanelAccount.bot_id == row.id, PanelAccount.panel_key == key,
                PanelAccount.status == "active").limit(1))
        cnt = len((await s.scalars(select(PanelAccount.id).where(
            PanelAccount.bot_id == row.id, PanelAccount.panel_key == key,
            PanelAccount.status == "active"))).all())
    if not n:
        await cb.answer("هیچ اکانتِ فعالی برای این پنل ثبت نشده.", show_alert=True)
        return
    await cb.message.answer(
        f"🔁 <b>بازسازیِ لینک‌ها — {label}</b>\n\n"
        f"برایِ <b>{cnt}</b> اکانتِ فعال، لینکِ ساب دوباره از پنل خوانده و ذخیره می‌شود.\n\n"
        "این کار وقتی به درد می‌خورد که پنل را عوض/بازسازی کرده باشی و "
        "کلاینت‌ها را با <b>همان شناسه‌ها (email)</b> روی پنلِ جدید ساخته باشی.\n\n"
        "⚠️ اکانت‌هایی که روی پنلِ جدید پیدا نشوند دست‌نخورده می‌مانند و "
        "فهرستشان را به تو می‌دهم.\n\n"
        "می‌خواهی به کاربرانی که لینکشان عوض شد پیام بدهم؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📣 بازسازی + اطلاع به کاربران",
                                  callback_data=f"tvpn:relinkgo:1:{key}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="🤫 فقط بازسازی (بی‌صدا)",
                                  callback_data=f"tvpn:relinkgo:0:{key}")],
            [InlineKeyboardButton(text="❌ لغو", callback_data=f"tvpn:panel:{key}",
                                  style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


@panel_relink_router.callback_query(F.data.startswith("tvpn:relinkgo:"))
async def relink_run(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    _, _, notify_raw, key = cb.data.split(":", 3)
    if not is_panel_key(key):
        await cb.answer("پنل نامعتبر", show_alert=True)
        return
    if not has_feature(row.plan, Feature.PANEL_RELINK):
        await cb.answer(upgrade_hint(Feature.PANEL_RELINK), show_alert=True)
        return
    notify = notify_raw == "1"
    label = PANEL_LABELS.get(key, key)
    panel = await get_panel(row.id, key)
    if not panel:
        await cb.answer("اول تنظیمات پنل را ثبت کن.", show_alert=True)
        return

    await cb.answer("⏳ شروع شد…")
    await cb.message.answer(f"⏳ در حال بازخوانیِ لینک‌ها از <b>{label}</b>…")

    async with Session() as s:
        accts = (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == row.id, PanelAccount.panel_key == key,
            PanelAccount.status == "active"))).all()
        work = [(a.id, a.email, a.user_telegram_id, a.sub_link, a.sub_id) for a in accts]

    client = make_panel_client(panel)
    changed: list[tuple[int, str, str]] = []   # (telegram_id, email, new_link)
    same = 0
    missing: list[str] = []
    failed: list[str] = []                     # PATCH_RELINK_RATE_LIMIT_V1: خطایِ پنل (۴۲۹/تایم‌اوت) — «پیدا نشد» نیست
    try:
        await client.login()
        for aid, email, tg_id, old_link, sub_id in work:
            try:
                await asyncio.sleep(_PACE_SLEEP)
                cur, _err = await _get_client_retry(client, email)
                if _err is not None:
                    logger.warning("relink: خطایِ پنل برایِ %s: %s", email, _err)
                    failed.append(f"{email}: {str(_err)[:90]}")
                    continue
                new_link = (cur or {}).get("sub_link") or ""
                new_sub_id = (cur or {}).get("subId") or sub_id
                new_uuid = (cur or {}).get("uuid") or (cur or {}).get("id")
                if not new_link and new_sub_id:
                    # پنل‌هایِ خانوادهٔ سنایی لینک را برنمی‌گردانند؛ از sub_base ساخته می‌شود
                    new_link = client.sub_link(new_sub_id) or ""
                if not new_link:
                    missing.append(email)
                    continue
                async with Session() as s:
                    a2 = await s.get(PanelAccount, aid)
                    if not a2:
                        continue
                    a2.sub_link = new_link
                    if new_sub_id:
                        a2.sub_id = new_sub_id
                    if new_uuid and isinstance(new_uuid, str):
                        a2.uuid = new_uuid
                    await s.commit()
                if new_link != (old_link or ""):
                    changed.append((tg_id, email, new_link))
                else:
                    same += 1
            except Exception as e:
                logger.exception("relink failed for %s", email)
                missing.append(f"{email}: {e}")
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    lines = [
        f"✅ <b>بازسازیِ لینک‌ها تمام شد — {label}</b>",
        f"• کلِ اکانت‌های فعال: {len(work)}",
        f"• لینکِ عوض‌شده: {len(changed)}",
        f"• بدون تغییر: {same}",
        f"• پیدا نشد روی پنل: {len(missing)}",
    ]
    if failed:
        lines.append(f"• ⚠️ خطایِ موقتِ پنل (محدودیتِ نرخ/تایم‌اوت): {len(failed)}")
    if missing:
        preview = "\n".join(f"  – <code>{m}</code>" for m in missing[:15])
        lines.append("\n<b>پیدا نشدند:</b>\n" + preview)
        if len(missing) > 15:
            lines.append(f"  … و {len(missing) - 15} مورد دیگر")
        lines.append(
            "\nℹ️ اینها روی پنلِ جدید با همین شناسه ساخته نشده‌اند. اگر "
            "کلاینت‌ها را با همان emailها بسازی و دوباره این دکمه را بزنی، وصل می‌شوند.")
    if failed:
        lines.append("\n<b>به‌خاطرِ خطایِ پنل بازسازی نشدند:</b>\n"
                     + "\n".join(f"  – <code>{m}</code>" for m in failed[:10])
                     + (f"\n  … و {len(failed) - 10} مورد دیگر" if len(failed) > 10 else "")
                     + "\n\nℹ️ چند دقیقه صبر کن و همین دکمه را دوباره بزن؛ فقط همین‌ها دوباره بررسی می‌شوند (بقیه «بدون تغییر» می‌مانند).")
    await cb.message.answer("\n".join(lines))

    if not notify or not changed:
        return

    await cb.message.answer(f"📣 در حال اطلاع به {len(changed)} کاربر…")
    sent = failed = 0
    for tg_id, email, link in changed:
        try:
            await bot.send_message(
                tg_id,
                "🔄 <b>لینکِ اتصالِ سرویس شما بروزرسانی شد</b>\n\n"
                "به‌دلیلِ تغییراتِ فنیِ سرور، لینکِ ساب شما عوض شده است. "
                "لطفاً لینکِ زیر را در برنامه‌تان جایگزین کنید:\n\n"
                f"🔗 <code>{link}</code>\n\n"
                "سرویس و حجم و تاریخِ انقضای شما تغییری نکرده است. 🙏")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(_NOTIFY_SLEEP)
    await cb.message.answer(
        f"📣 <b>اطلاع‌رسانی تمام شد.</b>\n• ارسال‌شده: {sent}\n• ناموفق (بلاک/حذف): {failed}")
