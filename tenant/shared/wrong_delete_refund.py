"""
wrong_delete_refund.py — «جبرانِ حذفِ اشتباهِ سرویس» با تأییدِ ادمینِ ربات (PATCH_WRONG_DELETE_REFUND_V1).

زمینه: باگِ traffic_cleanup (رفع‌شده) سرویسِ کاربرانی را که ظرفِ مهلتِ حذف تمدید/حجم خریده بودند حذف می‌کرد.
مبلغ به‌خودیِ‌خود برنمی‌گردد؛ اسکریپتِ `scripts/find_wrongly_deleted.py --propose` برایِ هر موردِ مستند، یک «پیشنهادِ
عودت» برایِ مالکِ همان ربات می‌فرستد و فقط وقتی ادمینِ ربات «✅ تأیید» را بزند، مبلغ به کیف پولِ کاربر برمی‌گردد.

  • پیشنهاد در جدولِ Setting (کلیدِ wdr:{شناسه}) نگه داشته می‌شود؛ مبلغ سمتِ سرور ثابت است و از callback خوانده نمی‌شود.
  • تأیید اتمیک است: وضعیتِ پیشنهاد، موجودی و تراکنش در یک تراکنشِ دیتابیس عوض می‌شوند؛ دوبار زدن دوبار پول نمی‌دهد.
  • کسانی که ادمین قبلاً دستی (kind="admin") بعد از حذف کیفشان را شارژ کرده، فقط مابه‌التفاوت (یا هیچ) پیشنهاد می‌گیرند.
"""
from __future__ import annotations

import hashlib
import json

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, BotUser, Setting, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

wdr_router = Router(name="wrong_delete_refund")

KEY_PREFIX = "wdr:"
NOTE_PREFIX = "عودتِ حذفِ اشتباهِ سرویس "


def proposal_id(email: str) -> str:
    return hashlib.sha1(email.encode("utf-8")).hexdigest()[:10]


def refund_note(email: str) -> str:
    return (NOTE_PREFIX + email)[:200]


async def manual_admin_credit_since(s, bot_id: int, user_id: int, since) -> int:
    """مجموعِ شارژهایِ دستیِ ادمین (kind="admin"، مبلغِ مثبت) بعد از «since» — یعنی مالک خودش جبران کرده."""
    v = await s.scalar(select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
        WalletTxn.bot_id == bot_id, WalletTxn.user_id == user_id, WalletTxn.kind == "admin",
        WalletTxn.amount > 0, WalletTxn.created_at >= since))
    return int(float(v or 0))


async def evaluate(bot_id: int, telegram_id: int, email: str, paid: int, deleted_at) -> dict:
    """فقط می‌خواند: {status: new|exists|manual_done|no_user|already_refunded, pid, amount, credited}."""
    pid = proposal_id(email)
    key = KEY_PREFIX + pid
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        if not u:
            return {"status": "no_user", "pid": pid}
        if await s.scalar(select(Setting.id).where(Setting.bot_id == bot_id, Setting.key == key)):
            return {"status": "exists", "pid": pid}
        if await s.scalar(select(WalletTxn.id).where(
                WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id, WalletTxn.kind == "refund",
                WalletTxn.note == refund_note(email)).limit(1)):
            return {"status": "already_refunded", "pid": pid}
        credited = await manual_admin_credit_since(s, bot_id, u.id, deleted_at)
    net = paid - credited
    if net <= 0:
        return {"status": "manual_done", "pid": pid, "credited": credited}
    return {"status": "new", "pid": pid, "amount": net, "credited": credited}


async def create_proposal(bot_id: int, telegram_id: int, email: str, paid: int, deleted_at) -> dict:
    """پیشنهادِ عودت می‌سازد (idempotent). status=created یعنی ردیفِ جدید نوشته شد؛ بقیه یعنی رد شد (دلیل در status)."""
    ev = await evaluate(bot_id, telegram_id, email, paid, deleted_at)
    if ev["status"] != "new":
        return ev
    data = {"tg": telegram_id, "email": email, "amount": ev["amount"], "paid": paid, "credited": ev["credited"],
            "deleted_at": deleted_at.strftime("%Y-%m-%d %H:%M"), "status": "pending"}
    async with Session() as s:
        s.add(Setting(bot_id=bot_id, key=KEY_PREFIX + ev["pid"], value=json.dumps(data, ensure_ascii=False)))
        try:
            await s.commit()
        except Exception:  # noqa: BLE001 — رقابتِ هم‌زمان: دیگری زودتر ساخت
            await s.rollback()
            return {"status": "exists", "pid": ev["pid"]}
    return {"status": "created", "pid": ev["pid"], "amount": ev["amount"], "credited": ev["credited"], "data": data}


def proposal_text(d: dict) -> str:
    extra = (f"\n(قبلاً {d['credited']:,} تومان دستی شارژ کرده‌اید؛ مابه‌التفاوت پیشنهاد می‌شود.)"
             if d.get("credited") else "")
    return (
        "⚠️ <b>جبرانِ حذفِ اشتباهِ سرویس</b>\n\n"
        "به‌خاطرِ یک باگِ داخلیِ بات‌ساز (که اصلاح شد)، سرویسی که کاربر ظرفِ مهلتِ حذف برایش پول داده بود، "
        "حذف شد:\n\n"
        f"👤 کاربر: <code>{d['tg']}</code>\n"
        f"💎 سرویس: <code>{d['email']}</code>\n"
        f"🗓 زمانِ حذف: {d['deleted_at']}\n"
        f"💵 پرداختِ او: <b>{d['paid']:,} تومان</b>{extra}\n\n"
        f"پیشنهاد: برگشتِ <b>{d['amount']:,} تومان</b> به کیف پولِ کاربر.\n"
        "اگر خودتان جبران کرده‌اید (مثلاً سرویسِ جدید ساخته‌اید)، «رد» را بزنید. "
        "با «تأیید» مبلغ همان لحظه به کیف پولش برمی‌گردد و برایش پیام می‌رود.")


def proposal_kb(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و برگشتِ مبلغ", callback_data=f"wdr:ok:{pid}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ رد (جبران شده/لازم نیست)", callback_data=f"wdr:no:{pid}", style=ButtonStyle.DANGER)],
    ])


async def send_proposal(bot_id: int, pid: str, d: dict) -> bool:
    """پیشنهاد را با خودِ رباتِ مشتری برایِ مالکش می‌فرستد."""
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow or not brow.token or not brow.owner_id:
        return False
    tb = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await tb.send_message(brow.owner_id, proposal_text(d), reply_markup=proposal_kb(pid))
        return True
    except Exception:  # noqa: BLE001 — مالک رباتش را استارت نکرده/بلاک کرده
        logger.info("wdr: پیشنهادِ %s برایِ مالکِ ربات %s نرفت", pid, bot_id)
        return False
    finally:
        try:
            await tb.session.close()
        except Exception:  # noqa: BLE001
            logger.debug("wdr: بستنِ سشن ناموفق")


async def _decide(bot_id: int, pid: str, approve: bool) -> tuple[str, dict | None, float]:
    """وضعیت را اتمیک عوض می‌کند. (نتیجه: applied|rejected|done_before|missing|no_user|error، داده، موجودیِ جدید)."""
    key = KEY_PREFIX + pid
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key).with_for_update())
        if not row:
            return "missing", None, 0.0
        d = json.loads(row.value or "{}")
        if d.get("status") != "pending":
            return "done_before", d, 0.0
        if not approve:
            d["status"] = "rejected"
            row.value = json.dumps(d, ensure_ascii=False)
            await s.commit()
            return "rejected", d, 0.0
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == int(d["tg"])).with_for_update())
        if not u:
            return "no_user", d, 0.0
        note = refund_note(d["email"])
        if await s.scalar(select(WalletTxn.id).where(
                WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id, WalletTxn.kind == "refund",
                WalletTxn.note == note).limit(1)):
            d["status"] = "applied"            # قبلاً از راهِ دیگری برگشته؛ فقط وضعیت را ببند
            row.value = json.dumps(d, ensure_ascii=False)
            await s.commit()
            return "done_before", d, float(u.balance or 0)
        amount = int(d["amount"])
        u.balance = float(u.balance or 0) + amount
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=amount, kind="refund", note=note))
        d["status"] = "applied"
        row.value = json.dumps(d, ensure_ascii=False)
        new_bal = float(u.balance)
        await s.commit()
        return "applied", d, new_bal


@wdr_router.callback_query(F.data.startswith("wdr:"))
async def wdr_decide(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        _, act, pid = cb.data.split(":", 2)
    except ValueError:
        await cb.answer()
        return
    if act not in ("ok", "no"):
        await cb.answer()
        return
    res, d, bal = await _decide(row.id, pid, approve=(act == "ok"))
    if res == "missing":
        await cb.answer("این پیشنهاد پیدا نشد.", show_alert=True)
        return
    if res == "done_before":
        await cb.answer("این مورد قبلاً بررسی شده است.", show_alert=True)
        return
    if res == "no_user":
        await cb.answer("کاربر در این ربات پیدا نشد؛ دستی بررسی کنید.", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        logger.debug("wdr: پاک‌کردنِ دکمه‌ها ممکن نشد")
    if res == "rejected":
        await cb.answer("رد شد.")
        await cb.message.answer(f"❌ رد شد؛ مبلغی برنگشت. (سرویسِ {d['email']})")
        return
    await cb.answer("✅ برگشت داده شد.")
    await cb.message.answer(
        f"✅ مبلغِ <b>{int(d['amount']):,} تومان</b> به کیف پولِ کاربرِ <code>{d['tg']}</code> برگشت.")
    try:
        await bot.send_message(
            int(d["tg"]),
            "🙏 <b>عذرخواهی بابت یک خطای فنی</b>\n\n"
            f"سرویسِ <code>{d['email']}</code> پس از تمدید/افزایشِ حجم به‌اشتباه حذف شد. "
            f"مبلغِ <b>{int(d['amount']):,} تومان</b> به کیف پولت برگردانده شد "
            f"(موجودی: {int(bal):,} تومان). می‌توانی دوباره سرویس تهیه کنی.")
    except Exception:  # noqa: BLE001 — کاربر ربات را بلاک کرده
        logger.info("wdr: اطلاعِ عودت به کاربر %s نرسید", d.get("tg"))
