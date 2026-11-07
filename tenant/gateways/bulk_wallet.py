"""
bulk_wallet.py — شارژ/کسرِ همگانیِ کیف پولِ کاربران (PATCH_BULK_WALLET_V1).

مالکِ ربات می‌تواند یک مبلغ را یک‌جا به کیف پولِ گروهی از کاربران اضافه (یا از
آن کسر) کند، با انتخابِ اینکه دقیقاً چه کسانی را شامل شود.

چرا این‌قدر محافظه‌کارانه نوشته شده:
  این عملیات **برگشت‌ناپذیر** است و مستقیماً پول توزیع می‌کند. ۱۰٬۰۰۰ تومان
  ضربدرِ ۵٬۰۰۰ کاربر یعنی ۵۰ میلیون تومان اعتبارِ قابلِ‌خرج‌کردن. برای همین:
    • قبل از اجرا، تعدادِ دقیقِ کاربران و جمعِ کلِ مبلغ نشان داده می‌شود
    • تاییدِ صریح لازم است (دکمهٔ قرمز)، و برایِ مبالغِ بزرگ تاییدِ دوم
    • اجرا دسته‌دسته و قابلِ لغو است
    • هر تغییر در WalletTxn ثبت می‌شود تا قابلِ ردیابی باشد
    • در «کسر»، موجودی هرگز منفی نمی‌شود (سقفِ کسر = موجودیِ فعلی)
"""
import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, Order, WalletTxn

logger = _get_logger(__name__)

bulk_wallet_router = Router(name="bulk_wallet")

# اگر جمعِ کلِ عملیات از این بیشتر شد، یک تاییدِ دومِ صریح گرفته می‌شود.
BIG_TOTAL_TOMAN = 5_000_000

AUDIENCES = {
    "all":       "👥 همهٔ کاربران",
    "active30":  "🟢 فعالانِ ۳۰ روزِ اخیر",
    "buyers":    "🛒 کسانی که خرید کرده‌اند",
    "nonbuyers": "🆕 کسانی که هنوز خرید نکرده‌اند",
    "zero":      "💤 کسانی که موجودی‌شان صفر است",
    "haspos":    "💰 کسانی که موجودیِ مثبت دارند",
}


class BulkFSM(StatesGroup):
    amount = State()
    reason = State()


# ─────────────────────────── انتخابِ مخاطب ───────────────────────────

async def _target_users(bot_id: int, audience: str) -> list[BotUser]:
    """کاربرانِ مشمول. کاربرانِ بن‌شده همیشه کنار گذاشته می‌شوند."""
    from datetime import datetime, timedelta
    async with Session() as s:
        q = select(BotUser).where(BotUser.bot_id == bot_id,
                                  BotUser.is_banned.is_(False))
        if audience == "active30":
            # ملاکِ «فعال»: در ۳۰ روزِ اخیر تراکنشِ کیف پول یا سفارشی داشته،
            # یا تازه عضو شده. (BotUser فیلدِ last_seen ندارد.)
            since = datetime.utcnow() - timedelta(days=30)
            recent_txn = select(WalletTxn.user_id).where(
                WalletTxn.bot_id == bot_id, WalletTxn.created_at >= since)
            recent_order = select(Order.user_id).where(
                Order.bot_id == bot_id, Order.created_at >= since)
            q = q.where(
                BotUser.id.in_(recent_txn)
                | BotUser.id.in_(recent_order)
                | (BotUser.joined_at >= since))
        elif audience == "buyers":
            paid = select(Order.user_id).where(
                Order.bot_id == bot_id, Order.status == "paid")
            q = q.where(BotUser.id.in_(paid))
        elif audience == "nonbuyers":
            paid = select(Order.user_id).where(
                Order.bot_id == bot_id, Order.status == "paid")
            q = q.where(~BotUser.id.in_(paid))
        elif audience == "zero":
            q = q.where(BotUser.balance <= 0)
        elif audience == "haspos":
            q = q.where(BotUser.balance > 0)
        return list((await s.scalars(q.order_by(BotUser.id))).all())


async def _count_users(bot_id: int, audience: str) -> int:
    return len(await _target_users(bot_id, audience))


# ─────────────────────────── منوها ───────────────────────────

async def bulk_wallet_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ورودیِ «شارژ/کسر همگانی» از منویِ مالی."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(BotUser).where(
            BotUser.bot_id == row.id, BotUser.is_banned.is_(False))) or 0
    txt = (
        "💸 <b>شارژ / کسرِ همگانیِ کیف پول</b>\n\n"
        f"کاربرانِ فعالِ ربات (بدونِ بن‌شده‌ها): <b>{total}</b> نفر\n\n"
        "اول انتخاب کن عملیات رویِ چه کسانی انجام شود. در مرحلهٔ بعد مبلغ را "
        "می‌گیرم و <b>قبل از اجرا</b>، تعدادِ دقیق و جمعِ کل را نشانت می‌دهم.\n\n"
        "⚠️ این عملیات برگشت‌ناپذیر است."
    )
    # === PATCH_PARTNER_V17 ===
    # رنگ برایِ خوانایی: انتخابِ مخاطب آبی، بازگشت قرمز.
    rows = [[InlineKeyboardButton(text=lbl, callback_data=f"bw:aud:{key}",
                                  style=ButtonStyle.PRIMARY)]
            for key, lbl in AUDIENCES.items()]
    # بازگشت باید به جایی برود که در پنلِ همان ربات وجود دارد:
    # ربات همکار پنلِ مالیِ خودش را دارد (pp:finance)، بقیه زیربخشِ
    # عمومیِ tadm:sec:finance.
    _back_cb = "tadm:sec:finance"
    try:
        from tenant import get_bot_row
        _row = await get_bot_row(bot)
        if _row and _row.plan == "partner":
            _back_cb = "pp:finance"
    except Exception:
        logger.exception("bulk_wallet: تشخیصِ نوعِ ربات برایِ دکمهٔ بازگشت")
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back_cb,
                                      style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    try:
        await cb.answer()
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")


@bulk_wallet_router.callback_query(F.data.in_({"tadm:bulkwallet", "bw:menu"}))
async def bw_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await bulk_wallet_start(cb, bot, state)


@bulk_wallet_router.callback_query(F.data.startswith("bw:aud:"))
async def bw_audience(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    aud = cb.data.split(":")[2]
    if aud not in AUDIENCES:
        await cb.answer("نامعتبر.", show_alert=True)
        return
    await state.update_data(bw_aud=aud)
    n = await _count_users(row.id, aud)
    if n == 0:
        await cb.answer("هیچ کاربری با این شرط پیدا نشد.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🎯 مخاطب: <b>{AUDIENCES[aud]}</b> — <b>{n}</b> نفر\n\n"
        "حالا انتخاب کن می‌خواهی به کیف پولشان اضافه کنی یا از آن کم کنی:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ شارژ (افزایش موجودی)",
                                  callback_data="bw:op:add",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="➖ کسر (کاهش موجودی)",
                                  callback_data="bw:op:sub",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="bw:menu",
                                  style=ButtonStyle.DANGER)],  # === PATCH_PARTNER_V17 ===
        ]))
    await cb.answer()


@bulk_wallet_router.callback_query(F.data.startswith("bw:op:"))
async def bw_op(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    op = cb.data.split(":")[2]
    await state.update_data(bw_op=op)
    await state.set_state(BulkFSM.amount)
    verb = "اضافه" if op == "add" else "کسر"
    await cb.message.answer(
        f"💵 مبلغی که می‌خواهی از کیف پولِ <b>هر نفر</b> {verb} شود را به تومان "
        "بفرست (مثلاً 10000):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@bulk_wallet_router.message(BulkFSM.amount, F.text)
async def bw_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, fa_to_int, rial
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    amount = fa_to_int(msg.text)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عددِ مثبت به تومان بفرست:")
        return
    data = await state.get_data()
    aud, op = data.get("bw_aud", "all"), data.get("bw_op", "add")
    await state.update_data(bw_amount=amount)

    if op == "sub":
        # برایِ کسر، دلیل می‌گیریم چون عیناً به خودِ کاربر نشان داده می‌شود
        await state.set_state(BulkFSM.reason)
        await msg.answer(
            "📝 دلیلِ کسر را بفرست (عیناً به کاربران نشان داده می‌شود).\n"
            "اگر دلیلی نداری، <code>-</code> بفرست:")
        return
    await state.clear()
    await _preview(msg, row.id, aud, op, amount, None)


@bulk_wallet_router.message(BulkFSM.reason, F.text)
async def bw_reason(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    reason = (msg.text or "").strip()
    reason = None if reason == "-" else reason[:200]
    await state.clear()
    await _preview(msg, row.id, data.get("bw_aud", "all"), "sub",
                   int(data.get("bw_amount", 0)), reason)


# ─────────────────────── پیش‌نمایش و تایید ───────────────────────

# پیش‌نمایش‌هایِ در انتظارِ تایید (کلید: شناسهٔ کوتاه) — چون callback_data
# محدودیتِ ۶۴ بایت دارد و نمی‌شود دلیل و مبلغ را داخلش گذاشت.
_pending: dict[str, dict] = {}


async def _preview(msg: Message, bot_id: int, aud: str, op: str,
                   amount: int, reason: str | None):
    from tenant import rial
    import secrets
    users = await _target_users(bot_id, aud)
    n = len(users)
    if n == 0:
        await msg.answer("هیچ کاربری با این شرط پیدا نشد؛ عملیات لغو شد.")
        return

    if op == "add":
        total = amount * n
        head = f"➕ <b>شارژِ همگانی</b>"
        effect = (f"به هر نفر <b>{rial(amount)}</b> اضافه می‌شود.\n"
                  f"جمعِ کلِ اعتبارِ توزیع‌شده: <b>{rial(total)}</b>")
        warn = ("⚠️ این اعتبار قابلِ خرج‌کردن است و برگشتِ آن ممکن نیست. "
                "قبل از تایید، جمعِ کل را با دقت نگاه کن.")
    else:
        # کسرِ واقعی هرگز از موجودی بیشتر نمی‌شود
        total = sum(min(amount, max(0, int(u.balance or 0))) for u in users)
        affected = sum(1 for u in users if int(u.balance or 0) > 0)
        head = f"➖ <b>کسرِ همگانی</b>"
        effect = (f"از هر نفر تا سقفِ <b>{rial(amount)}</b> کسر می‌شود "
                  f"(موجودی هیچ‌کس منفی نمی‌شود).\n"
                  f"کاربرانی که واقعاً کسر می‌شوند: <b>{affected}</b> نفر\n"
                  f"جمعِ کلِ کسر: <b>{rial(total)}</b>")
        warn = "⚠️ این عملیات برگشت‌ناپذیر است و به کاربران هم اطلاع داده می‌شود."

    token = secrets.token_hex(4)
    _pending[token] = {"bot_id": bot_id, "aud": aud, "op": op,
                       "amount": amount, "reason": reason, "n": n, "total": total}

    big = total >= BIG_TOTAL_TOMAN
    confirm_cb = f"bw:big:{token}" if big else f"bw:go:{token}"
    lines = [
        head + "\n",
        f"🎯 مخاطب: <b>{AUDIENCES[aud]}</b>",
        f"👥 تعداد: <b>{n}</b> نفر\n",
        effect,
    ]
    if reason:
        lines.append(f"\n📝 دلیل: {reason}")
    lines.append("\n" + warn)
    lines.append("\n📨 به همهٔ این افراد پیامِ اطلاع‌رسانی فرستاده می‌شود.")
    await msg.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تایید و اجرا", callback_data=confirm_cb,
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="bw:menu", style=ButtonStyle.DANGER)],
        ]))


@bulk_wallet_router.callback_query(F.data.startswith("bw:big:"))
async def bw_big_confirm(cb: CallbackQuery, bot: Bot):
    """تاییدِ دوم برایِ مبالغِ بزرگ — یک ترمزِ عمدی."""
    from tenant import _owner_ok, rial
    row = await _owner_ok(cb, bot)
    if not row:
        return
    token = cb.data.split(":")[2]
    job = _pending.get(token)
    if not job or job["bot_id"] != row.id:
        await cb.answer("این عملیات منقضی شده؛ از اول شروع کن.", show_alert=True)
        return
    await cb.message.edit_text(
        "🛑 <b>یک لحظه صبر کن</b>\n\n"
        f"جمعِ کلِ این عملیات <b>{rial(job['total'])}</b> است — مبلغِ بزرگی است.\n\n"
        f"با تایید، رویِ <b>{job['n']}</b> کاربر اعمال می‌شود و "
        "<b>هیچ راهی برایِ برگرداندنش نیست</b>.\n\n"
        "اگر کاملاً مطمئنی، دکمهٔ زیر را بزن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 بله، مطمئنم — اجرا کن",
                                  callback_data=f"bw:go:{token}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ نه، منصرف شدم", callback_data="bw:menu", style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


# ─────────────────────────── اجرا ───────────────────────────

_cancelled: set[str] = set()


@bulk_wallet_router.callback_query(F.data.startswith("bw:stop:"))
async def bw_stop(cb: CallbackQuery):
    _cancelled.add(cb.data.split(":")[2])
    await cb.answer("لغو شد؛ ادامهٔ عملیات متوقف می‌شود.", show_alert=True)


@bulk_wallet_router.callback_query(F.data.startswith("bw:go:"))
async def bw_go(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    token = cb.data.split(":")[2]
    job = _pending.pop(token, None)
    if not job or job["bot_id"] != row.id:
        await cb.answer("این عملیات منقضی شده؛ از اول شروع کن.", show_alert=True)
        return
    await cb.answer("شروع شد…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    status = await cb.message.answer(
        f"⏳ در حال اجرا رویِ {job['n']} کاربر…",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 لغو ادامهٔ عملیات",
                                  callback_data=f"bw:stop:{token}", style=ButtonStyle.DANGER)]]))
    asyncio.create_task(_run(bot, row.id, token, job, status))


async def _run(bot: Bot, bot_id: int, token: str, job: dict, status: Message):
    from tenant import rial
    from tenant.gateways.wallet_receipt import (build_wallet_charge_message,
                                       build_wallet_deduct_message)
    op, amount, reason = job["op"], int(job["amount"]), job.get("reason")
    users = await _target_users(bot_id, job["aud"])
    note = ("شارژِ همگانی توسطِ مدیر" if op == "add"
            else ("کسرِ همگانی توسطِ مدیر" + (f" — دلیل: {reason}" if reason else "")))

    done = applied_total = 0
    notified = failed_notify = 0
    for i, u0 in enumerate(users, 1):
        if token in _cancelled:
            break
        try:
            async with Session() as s:
                u = await s.scalar(select(BotUser).where(
                    BotUser.id == u0.id).with_for_update())
                if not u:
                    continue
                # ستون‌هایِ balance و amount از نوعِ Numeric(12,0) هستند (تومانِ
                # صحیح). رویِ PostgreSQL این‌ها Decimal برمی‌گردند، پس عمداً با
                # int کار می‌کنیم تا نه اعشارِ شناور واردِ محاسبهٔ پول شود و نه
                # هنگامِ نوشتن گرد شدنِ ناخواسته رخ دهد.
                cur = int(u.balance or 0)
                delta = amount if op == "add" else -min(amount, max(0, cur))
                if delta == 0:
                    continue          # چیزی برایِ کسر نمانده
                u.balance = cur + delta
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=delta,
                                kind="admin", note=note))
                await s.commit()
                new_bal, tg_id = int(u.balance), int(u.telegram_id)
                _log_user = u
            done += 1
            applied_total += abs(delta)
            # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
            # شارژ/کسرِ گروهیِ ادمین هم جابه‌جاییِ کیفِ پول است. اگر کانالِ
            # لاگ تنظیم نشده باشد، خودِ tx_log بی‌صدا برمی‌گردد؛ پس این
            # حلقه برایِ ربات‌هایِ بدونِ کانال هیچ هزینه‌ای اضافه نمی‌کند.
            try:
                from tenant.shared.tx_channel import tx_log
                await tx_log(bot, bot_id, buyer=_log_user, amount=delta,
                             gateway=("bulk" if delta > 0 else "admin_deduct"),
                             old_balance=new_bal - delta, new_balance=new_bal,
                             note=note or "")
            except Exception:
                logger.exception("tx_log (bulk wallet)")
        except Exception:
            logger.exception("bulk_wallet: apply failed for user %s", u0.id)
            continue

        # اطلاع‌رسانی به کاربر (شکستش نباید جلویِ ادامهٔ کار را بگیرد)
        try:
            if op == "add":
                text, kb = build_wallet_charge_message(abs(delta), new_bal, "admin")
            else:
                text, kb = build_wallet_deduct_message(abs(delta), new_bal, reason)
            await bot.send_message(tg_id, text, reply_markup=kb)
            notified += 1
        except Exception:
            failed_notify += 1
        # نرخِ ارسال: تلگرام حدودِ ۳۰ پیام در ثانیه اجازه می‌دهد
        await asyncio.sleep(0.05)
        if i % 25 == 0:
            try:
                await status.edit_text(
                    f"⏳ {i} از {len(users)} … (اعمال‌شده: {done})",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🛑 لغو ادامهٔ عملیات",
                                              callback_data=f"bw:stop:{token}", style=ButtonStyle.DANGER)]]))
            except Exception:
                logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")

    _cancelled.discard(token)
    verb = "شارژ" if op == "add" else "کسر"
    logger.warning("bulk_wallet: bot=%s op=%s amount=%s users=%s total=%s",
                   bot_id, op, amount, done, applied_total)
    try:
        await status.edit_text(
            f"✅ <b>{verb}ِ همگانی تمام شد</b>\n\n"
            f"👥 اعمال‌شده رویِ: <b>{done}</b> نفر\n"
            f"💵 جمعِ کل: <b>{rial(applied_total)}</b>\n"
            f"📨 اطلاع‌رسانی: {notified} موفق"
            + (f" / {failed_notify} ناموفق (ربات را بلاک کرده‌اند)" if failed_notify else ""),
            reply_markup=None)
    except Exception:
        await status.answer(f"✅ {verb}ِ همگانی تمام شد — {done} نفر.")
