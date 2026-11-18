"""PATCH_ORPHAN_PURCHASE_V1 — آشکارسازِ «خریدِ ناتمام» + دکمهٔ عودتِ یک‌ضربه (nitroteeam / Nitro_Skybot).

ماجرا: خریدِ VPN اول پولِ کیف را کم و commit می‌کند و بعد روی پنل اکانت می‌سازد. اگر پروسه در این فاصله بمیرد (ریستارتِ سرویس،
قطعِ برق، …) هیچ کدی برایِ عودت اجرا نمی‌شود. اصلاحِ build 652 کنسل‌شدنِ عادی را پوشش می‌دهد، ولی kill -9 یا خاموشیِ سرور را نه.
این ماژول آن حالت را «بعد از وقوع» پیدا می‌کند:

  هر چند دقیقه، خریدهایِ ۳ دقیقه تا ۲۴ ساعت پیش را (kind=purchase با متنِ «خرید VPN…» یا «خرید N کانفیگ…») می‌خواند؛ اگر نه اکانتی
  (PanelAccount) برایِ همان کاربر در ۱۵ دقیقهٔ بعدش ساخته شده و نه عودتی ثبت شده، یک اعلان می‌فرستد: به کانالِ لاگِ خطاها (اگر وصل است) و
  به مالک/ادمین‌ها، با دکمه‌هایِ «↩️ عودتِ وجه» و «✅ مشکلی نبود». هیچ عودتی خودکار انجام نمی‌شود — تصمیم با مالک است
  (ممکن است اکانت ساخته شده و فقط ثبت نشده باشد؛ مثلاً خریدِ کانفیگِ آماده از انبار PanelAccount نمی‌سازد).

هر خرید فقط یک‌بار اعلام می‌شود (کلیدِ orph_seen:<id> در Setting).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from html import escape as _esc

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select

from config import Session
from models import Bot as BotRow
from models import BotUser, PanelAccount, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

orphan_router = Router(name="orphan_purchases")

MIN_AGE = _dt.timedelta(minutes=3)       # خریدِ در جریان را دست نزن
MAX_AGE = _dt.timedelta(hours=24)
WINDOW = _dt.timedelta(minutes=15)       # اکانت/عودت باید تا این‌قدر بعد از خرید ثبت شده باشد


def _is_vpn_note():
    return or_(WalletTxn.note.like("خرید VPN%"), WalletTxn.note.like("خرید % کانفیگ %"))


async def find_orphans(now: _dt.datetime | None = None) -> list[tuple]:
    """[(WalletTxn، telegram_id)] خریدهایِ مشکوکِ اعلام‌نشده. فقط می‌خواند."""
    from tenant.shared.extras import _eget
    now = now or _dt.datetime.utcnow()
    out = []
    async with Session() as s:
        rows = (await s.execute(
            select(WalletTxn, BotUser.telegram_id).join(BotUser, BotUser.id == WalletTxn.user_id).where(
                WalletTxn.kind == "purchase", WalletTxn.created_at >= now - MAX_AGE,
                WalletTxn.created_at <= now - MIN_AGE, _is_vpn_note()))).all()
        for t, tg in rows:
            if await _eget(t.bot_id, f"orph_seen:{t.id}", ""):
                continue
            acc = await s.scalar(select(PanelAccount.id).where(
                PanelAccount.bot_id == t.bot_id, PanelAccount.user_telegram_id == tg,
                PanelAccount.created_at >= t.created_at - _dt.timedelta(minutes=1),
                PanelAccount.created_at <= t.created_at + WINDOW).limit(1))
            if acc:
                continue
            ref = await s.scalar(select(WalletTxn.id).where(
                WalletTxn.bot_id == t.bot_id, WalletTxn.user_id == t.user_id, WalletTxn.kind == "refund",
                WalletTxn.created_at >= t.created_at, WalletTxn.created_at <= t.created_at + WINDOW).limit(1))
            if ref:
                continue
            out.append((t, tg))
    return out


def _kb(txn_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ عودتِ وجه به کیف پولِ مشتری", callback_data=f"orph:ref:{txn_id}", style=ButtonStyle.DANGER),
        InlineKeyboardButton(text="✅ مشکلی نبود", callback_data=f"orph:ok:{txn_id}", style=ButtonStyle.SUCCESS)]])


def _text(t: WalletTxn, tg: int) -> str:
    from tenant import _jalali_date_time
    d, tm = _jalali_date_time(t.created_at)
    return ("⚠️ <b>#خرید_ناتمام؟</b>\n\n"
            f"🆔 مشتری: <code>{tg}</code>\n💰 مبلغِ کم‌شده: <b>{int(-float(t.amount)):,}</b> تومان\n"
            f"📝 {_esc(str(t.note or ''))}\n🕒 زمانِ خرید: {d} — {tm}\n\n"
            "بعد از این خرید، نه سرویسی برای او ثبت شده و نه عودتی. معمولاً یعنی ربات وسطِ ساختِ سرویس قطع شده (ریستارت/خاموشیِ سرور).\n"
            "اگر مشتری سرویس را نگرفته، «عودتِ وجه» را بزن؛ اگر گرفته، «مشکلی نبود».")


async def scan_once() -> int:
    from tenant.shared.extras import _eset
    n = 0
    for t, tg in await find_orphans():
        await _eset(t.bot_id, f"orph_seen:{t.id}", "1")            # اول علامت بزن؛ دو بار اعلام نشود
        try:
            async with Session() as s:
                brow = await s.get(BotRow, t.bot_id)
            if not brow or not brow.token:
                continue
            tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                kb = _kb(t.id)
                from tenant.shared.err_log import post as _el_post
                sent_channel = await _el_post(tbot, t.bot_id, _text(t, tg), kb)
                from tenant import _notify_admins
                await _notify_admins(tbot, brow, _text(t, tg), kb=kb)      # مالک/ادمین‌ها هم (دکمه‌ها همان‌جا کار می‌کنند)
                logger.warning("orphan purchase: bot=%s txn=%s user=%s amount=%s (channel=%s)", t.bot_id, t.id, tg, t.amount, sent_channel)
                n += 1
            finally:
                await tbot.session.close()
        except Exception:
            logger.exception("orphan purchase: اعلان ناموفق بود (txn=%s)", t.id)
    return n


async def orphan_purchase_loop(interval: int = 300) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await scan_once()
        except Exception:
            logger.exception("orphan_purchase_loop error")
        await asyncio.sleep(interval)


# ───────────── دکمه‌ها (در پروسهٔ اصلیِ ربات) ─────────────
async def refund_txn(bot_id: int, txn_id: int, admin_tg: int) -> tuple[str, int | None, int]:
    """('ok'|'done'|'missing', telegramِ مشتری، مبلغ). عودتِ یک‌بارِ اتمیک: قفلِ ردیفِ کاربر + کلیدِ orph_done."""
    from models import Setting
    async with Session() as s:
        t = await s.get(WalletTxn, txn_id)
        if not t or t.bot_id != bot_id or t.kind != "purchase" or float(t.amount) >= 0:
            return "missing", None, 0
        u = await s.scalar(select(BotUser).where(BotUser.id == t.user_id).with_for_update())
        if u is None:
            return "missing", None, 0
        done = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == f"orph_done:{txn_id}"))
        if done is not None:
            return "done", u.telegram_id, int(-float(t.amount))
        amount = -float(t.amount)
        u.balance = float(u.balance or 0) + amount
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=amount, kind="refund",
                        note=f"عودتِ خریدِ ناتمام (تأییدِ مالک/ادمین {admin_tg}) — تراکنش #{txn_id}"))
        s.add(Setting(bot_id=bot_id, key=f"orph_done:{txn_id}", value="refund"))
        await s.commit()
        return "ok", u.telegram_id, int(amount)


async def _is_staff(bot_id: int, tg: int, owner_id: int) -> bool:
    from tenant import is_owner_or_admin
    return await is_owner_or_admin(bot_id, tg, owner_id)


@orphan_router.callback_query(F.data.startswith("orph:"))
async def orph_action(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await _is_staff(row.id, cb.from_user.id, row.owner_id):
        return await cb.answer("فقط مالک/ادمین.", show_alert=True)
    try:
        _p, act, tid = cb.data.split(":")
        tid = int(tid)
    except ValueError:
        return await cb.answer()
    if act == "ok":
        from models import Setting
        async with Session() as s:
            if await s.scalar(select(Setting).where(Setting.bot_id == row.id, Setting.key == f"orph_done:{tid}")) is None:
                s.add(Setting(bot_id=row.id, key=f"orph_done:{tid}", value="dismissed"))
                await s.commit()
        tail, ans = "\n\n✅ <b>بررسی شد؛ مشکلی نبود.</b>", "ثبت شد."
    else:
        st, tg, amount = await refund_txn(row.id, tid, cb.from_user.id)
        if st == "missing":
            return await cb.answer("تراکنش پیدا نشد.", show_alert=True)
        if st == "done":
            return await cb.answer("این مورد قبلاً رسیدگی شده است.", show_alert=True)
        tail, ans = f"\n\n↩️ <b>{amount:,} تومان به کیف پولِ مشتری برگشت.</b>", "عودت انجام شد."
        try:
            await bot.send_message(tg, f"↩️ مبلغ <b>{amount:,}</b> تومان بابتِ یک خریدِ ناتمام به کیف پولت برگشت. ببخشید از این مشکل 🙏",
                                   parse_mode="HTML")
        except Exception:
            logger.info("orphan refund: پیامِ اطلاع به مشتری نرسید")
    try:
        await cb.message.edit_text((cb.message.html_text or "") + tail, reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.debug("orphan: ویرایشِ پیام ممکن نشد")
    await cb.answer(ans, show_alert=True)
