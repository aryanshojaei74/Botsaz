"""شرطبندی گروهی برای ربات‌های خدمات مجازی.

این ماژول فقط روی موجودی داخلی همان ربات کار می‌کند. مبلغ سازنده هنگام
ایجاد بازی رزرو می‌شود و پذیرش/تسویه در تراکنش اتمیک انجام می‌گیرد.
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import Bet, BetLedger, Bot as BotRow, BotUser, WalletTxn
from logging_setup import get_logger as _get_logger
from tenant.shared.group_wallet import _group_feature_allowed

logger = _get_logger(__name__)
betting_router = Router()

_BET_RE = re.compile(r"^(?:بازی|شرطبندی|شرط\s*بندی)\s+([۰-۹0-9,،]+)$", re.IGNORECASE)
_INCOMPLETE_BET_RE = re.compile(r"^(?:بازی|شرطبندی|شرط\s*بندی)$", re.IGNORECASE)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _digits(value: str) -> str:
    return value.translate(_DIGITS).replace(",", "").replace("،", "").strip()


def _money(value) -> int:
    return int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rial(value) -> str:
    return f"{_money(value):,} تومان"


def _name(user_id: int, username: str | None, full_name: str | None) -> str:
    if username:
        return "@" + html.escape(username)
    return html.escape(full_name or str(user_id))


async def _get_setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set_setting(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def _bot_for_message(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _is_admin(row, telegram_id: int) -> bool:
    from tenant import is_owner_or_admin
    return bool(row and await is_owner_or_admin(row.id, telegram_id, row.owner_id))


def _bet_keyboard(bet: Bet) -> InlineKeyboardMarkup:
    if bet.status == "pending":
        rows = [[
            InlineKeyboardButton(text="✅ قبول و پیوستن", callback_data=f"bet:join:{bet.id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="❌ لغو", callback_data=f"bet:cancel:{bet.id}", style=ButtonStyle.DANGER),
        ]]
    else:
        rows = []
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pending_text(bet: Bet) -> str:
    fee_line = f"💸 کارمزد: <b>{_money(bet.fee_pct):g}٪</b> — {_rial(bet.fee_amount)}\n" if _money(bet.fee_pct) > 0 else "💸 کارمزد: <b>غیرفعال</b>\n"
    payout = _money(bet.payout_amount)
    participant_line = (
        f"🎯 شرکت‌کنندهٔ تعیین‌شده: {_name(bet.target_telegram_id, bet.target_username, bet.target_name)}\n"
        if bet.target_telegram_id else
        "👥 حالت عمومی: اولین نفری که روی «✅ قبول و پیوستن» بزند، شرکت می‌کند.\n"
    )
    return (
        "🎲 <b>بازی</b>\n\n"
        f"👤 سازنده: {_name(bet.creator_telegram_id, bet.creator_username, bet.creator_name)}\n"
        f"{participant_line}"
        f"💰 مبلغ بازی هر نفر: <b>{_rial(bet.amount)}</b>\n"
        f"{fee_line}"
        f"🏆 جایزهٔ نهایی برنده: <b>{_rial(payout)}</b>\n\n"
        "⏳ وضعیت: در انتظار پیوستن"
    )


def _settled_text(bet: Bet) -> str:
    winner = _name(bet.winner_telegram_id, bet.winner_username, bet.winner_name)
    loser = _name(bet.loser_telegram_id, bet.loser_username, bet.loser_name)
    fee_line = f"💸 کارمزد بازی: <b>{_rial(bet.fee_amount)}</b> ({_money(bet.fee_pct):g}٪)\n" if _money(bet.fee_amount) else ""
    return (
        "🎲 <b>نتیجهٔ بازی مشخص شد</b> 🎉\n\n"
        f"🏆 برنده: <b>{winner}</b>\n"
        f"❌ بازنده: <b>{loser}</b>\n"
        f"💰 مبلغ بازی: <b>{_rial(bet.amount)}</b> برای هر نفر\n"
        f"{fee_line}"
        f"🎁 مبلغ نهایی دریافتی برنده: <b>{_rial(bet.payout_amount)}</b>\n\n"
        "✅ موجودی بازیکنان طبق نتیجهٔ ثبت‌شده تسویه شد."
    )


def _settled_keyboard(payout_amount, winner_balance, loser_balance) -> InlineKeyboardMarkup:
    """اطلاعات نتیجه به‌صورت دکمه‌های شیشه‌ای، مطابق نمونهٔ گروه."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁 جایزهٔ برنده", callback_data="bet:info", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text=_rial(payout_amount), callback_data="bet:info", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(text="💎 موجودیِ برنده", callback_data="bet:info", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=_rial(winner_balance), callback_data="bet:info", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="❌ موجودیِ بازنده", callback_data="bet:info", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text=_rial(loser_balance), callback_data="bet:info", style=ButtonStyle.DANGER),
        ],
    ])


@betting_router.callback_query(F.data == "bet:info")
async def bet_info_button(cb: CallbackQuery):
    await cb.answer()


def _cancelled_text(bet: Bet, expired: bool = False) -> str:
    reason = "⏰ این بازی منقضی شد و مبلغ رزروشده به سازنده برگشت." if expired else "❌ بازی توسط سازنده لغو شد و مبلغ رزروشده برگشت."
    return f"🎲 <b>بازی لغو شد</b>\n\n{reason}"



async def expire_pending_bets_once() -> int:
    """بازی‌هایِ درانتظارِ گذشته از زمان را یک‌بار اتمیک منقضی و مسترد می‌کند."""
    now = datetime.utcnow()
    async with Session() as s:
        ids = (await s.scalars(
            select(Bet.id).where(
                Bet.status == "pending",
                Bet.expires_at.isnot(None),
                Bet.expires_at <= now,
            ).limit(200)
        )).all()

    if not ids:
        return 0

    expired_rows: list[dict] = []
    for bet_id in ids:
        async with Session() as s:
            bet = await s.get(Bet, bet_id, with_for_update=True)
            if not bet or bet.status != "pending" or not bet.expires_at or bet.expires_at > now:
                continue
            creator = await s.get(BotUser, bet.creator_user_id, with_for_update=True)
            bet.status = "expired"
            if creator:
                creator.balance = Decimal(str(creator.balance)) + Decimal(str(bet.amount))
                s.add(BetLedger(
                    bot_id=bet.bot_id, bet_id=bet.id, user_id=creator.id,
                    telegram_id=creator.telegram_id, amount=bet.amount, kind="refund"))
                s.add(WalletTxn(
                    bot_id=bet.bot_id, user_id=creator.id, amount=bet.amount,
                    kind="bet_refund", note=f"انقضای بازی #{bet.id}"))
            expired_rows.append({
                "id": bet.id, "bot_id": bet.bot_id, "chat_id": bet.chat_id,
                "message_id": bet.message_id, "amount": bet.amount,
                "creator_telegram_id": bet.creator_telegram_id,
            })
            await s.commit()

    if not expired_rows:
        return 0

    bot_ids = {r["bot_id"] for r in expired_rows}
    async with Session() as s:
        bot_rows = (await s.scalars(select(BotRow).where(BotRow.id.in_(bot_ids)))).all()
    bot_by_id = {row.id: row for row in bot_rows}

    for item in expired_rows:
        row = bot_by_id.get(item["bot_id"])
        if not row or not row.token:
            continue
        bot = Bot(token=row.token)
        try:
            if item["message_id"]:
                await bot.edit_message_text(
                    chat_id=item["chat_id"], message_id=item["message_id"],
                    text=("🎲 <b>بازی لغو شد</b>\n\n"
                          "⏰ زمان بازی به پایان رسید و مبلغ رزروشده به سازنده برگشت."),
                    parse_mode="HTML", reply_markup=None)
            try:
                from tenant_group_guard import tenant_group_log
                await tenant_group_log(
                    bot, row, item["chat_id"], "bet_cancelled",
                    actor=item["creator_telegram_id"],
                    details=f"بازی #{item['id']} به‌دلیل پایان زمان لغو شد؛ مبلغ {_rial(item['amount'])} مسترد شد")
            except Exception:
                logger.exception("ثبت لاگ انقضای بازی ناموفق بود؛ bet_id=%s", item["id"])
        except Exception:
            logger.exception("ویرایش پیام بازی منقضی‌شده ناموفق بود؛ bet_id=%s", item["id"])
        finally:
            await bot.session.close()
    return len(expired_rows)


async def betting_expiry_loop(interval_sec: int = 15):
    """loop مستقلِ worker برای تعیین‌تکلیف خودکار بازی‌هایِ منقضی‌شده."""
    while True:
        try:
            count = await expire_pending_bets_once()
            if count:
                logger.info("betting_expiry_loop: %s بازی منقضی و مسترد شد", count)
        except Exception:
            logger.exception("betting_expiry_loop: اجرای دور انقضا ناموفق بود")
        await asyncio.sleep(interval_sec)


@betting_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: bool(_INCOMPLETE_BET_RE.match((m.text or "").strip()))),
)
async def incomplete_bet_command(msg: Message, bot: Bot):
    row = await _bot_for_message(bot)
    if not _group_feature_allowed(row):
        return
    if (await _get_setting(row.id, "group_betting_enabled", "0")) != "1":
        await msg.reply("❌ قابلیت بازی در این ربات فعال نیست.")
        return
    await msg.reply("برای ساخت بازی عمومی، مبلغ را هم بنویس؛ مثلاً: «بازی ۱۰۰۰۰»\nبرای بازی روی یک نفر، همین دستور را روی پیام او ریپلای کن.")

@betting_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: bool(_BET_RE.match((m.text or "").strip()))),
)
async def create_bet(msg: Message, bot: Bot):
    row = await _bot_for_message(bot)
    if not _group_feature_allowed(row):
        return
    if (await _get_setting(row.id, "group_betting_enabled", "0")) != "1":
        await msg.reply("❌ قابلیت بازی در این ربات فعال نیست.")
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else None
    if target and target.is_bot:
        await msg.reply("❌ نمی‌شود با ربات بازی کرد.")
        return
    if target and target.id == msg.from_user.id:
        await msg.reply("❌ نمی‌شود روی پیام خودت بازی ساخت.")
        return
    match = _BET_RE.match((msg.text or "").strip())
    amount = int(_digits(match.group(1))) if match else 0
    min_amount = int(_digits(await _get_setting(row.id, "group_betting_min", "1000")) or 1000)
    max_raw = _digits(await _get_setting(row.id, "group_betting_max", "0"))
    max_amount = int(max_raw) if max_raw else 0
    if amount < min_amount:
        await msg.reply(f"❌ حداقل مبلغ بازی {_rial(min_amount)} است.")
        return
    if max_amount and amount > max_amount:
        await msg.reply(f"❌ حداکثر مبلغ بازی {_rial(max_amount)} است.")
        return
    fee_enabled = (await _get_setting(row.id, "group_betting_fee_enabled", "0")) == "1"
    fee_pct = Decimal(str(await _get_setting(row.id, "group_betting_fee_pct", "0") or 0)) if fee_enabled else Decimal("0")
    fee_amount = int((Decimal(amount * 2) * fee_pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    payout = amount * 2 - fee_amount
    if payout <= 0:
        await msg.reply("❌ کارمزد تنظیم‌شده باید کمتر از کل مبلغ دو طرف باشد.")
        return
    ttl_seconds = int(_digits(await _get_setting(row.id, "group_betting_ttl", "300")) or 300)
    ttl_seconds = max(60, min(ttl_seconds, 86400))
    from tenant import get_or_create_user
    creator = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    now = datetime.utcnow()
    async with Session() as s:
        locked = await s.get(BotUser, creator.id, with_for_update=True)
        if not locked or _money(locked.balance) < amount:
            await msg.reply(f"❌ موجودی کافی نیست. موجودی شما: {_rial(locked.balance if locked else 0)}")
            return
        locked.balance = Decimal(str(locked.balance)) - Decimal(amount)
        bet = Bet(
            bot_id=row.id, chat_id=msg.chat.id, message_id=0,
            creator_user_id=locked.id, creator_telegram_id=msg.from_user.id,
            creator_name=msg.from_user.full_name or "", creator_username=msg.from_user.username or "",
            target_telegram_id=target.id if target else 0,
            target_name=target.full_name if target else "",
            target_username=target.username if target else None,
            amount=amount, fee_pct=fee_pct, fee_amount=fee_amount, payout_amount=payout,
            status="pending", created_at=now, expires_at=now + timedelta(seconds=ttl_seconds),
        )
        s.add(bet)
        await s.flush()
        s.add(BetLedger(bot_id=row.id, bet_id=bet.id, user_id=locked.id, telegram_id=msg.from_user.id, amount=-amount, kind="reserve"))
        s.add(WalletTxn(bot_id=row.id, user_id=locked.id, amount=-amount, kind="bet_reserve", note=f"رزرو بازی #{bet.id}"))
        await s.commit()
        bet_id = bet.id
    sent = await msg.reply("⏳ در حال ساخت بازی...")
    async with Session() as s:
        b = await s.get(Bet, bet_id)
        if b:
            b.message_id = sent.message_id
            await s.commit()
    async with Session() as s:
        b = await s.get(Bet, bet_id)
        if b:
            try:
                await sent.edit_text(_pending_text(b), reply_markup=_bet_keyboard(b), parse_mode="HTML")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    try:
        from tenant_group_guard import tenant_group_log
        await tenant_group_log(bot, row, msg.chat.id, "bet_created", actor=msg.from_user.id,
                               target=(target.id if target else None),
                               details=f"بازی #{bet_id}؛ مبلغ {_rial(amount)}؛ {'عمومی' if not target else 'با شرکت‌کنندهٔ تعیین‌شده'}")
    except Exception:
        logger.exception("ثبت لاگ ایجاد بازی ناموفق بود")


@betting_router.callback_query(F.data.startswith("bet:join:"))
async def join_bet(cb: CallbackQuery, bot: Bot):
    bet_id = int(cb.data.rsplit(":", 1)[1])
    row = await _bot_for_message(bot)
    if not _group_feature_allowed(row):
        await cb.answer()
        return
    from tenant import get_or_create_user
    joiner = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    winner_tg = loser_tg = None
    async with Session() as s:
        bet = await s.get(Bet, bet_id, with_for_update=True)
        if not bet or bet.bot_id != row.id:
            await cb.answer("این بازی پیدا نشد.", show_alert=True)
            return
        if bet.status != "pending":
            await cb.answer("این بازی دیگر در انتظار پیوستن نیست.", show_alert=True)
            return
        if bet.creator_telegram_id == cb.from_user.id:
            await cb.answer("سازنده نمی‌تواند خودش به بازی بپیوندد.", show_alert=True)
            return
        if bet.target_telegram_id and bet.target_telegram_id != cb.from_user.id:
            await cb.answer("این بازی برای کاربری است که پیامش ریپلای شده.", show_alert=True)
            return
        if bet.expires_at and bet.expires_at <= datetime.utcnow():
            bet.status = "expired"
            creator = await s.get(BotUser, bet.creator_user_id, with_for_update=True)
            if creator:
                creator.balance = Decimal(str(creator.balance)) + Decimal(str(bet.amount))
                s.add(BetLedger(bot_id=bet.bot_id, bet_id=bet.id, user_id=creator.id, telegram_id=creator.telegram_id, amount=bet.amount, kind="refund"))
                s.add(WalletTxn(bot_id=bet.bot_id, user_id=creator.id, amount=bet.amount, kind="bet_refund", note=f"انقضای بازی #{bet.id}"))
            await s.commit()
            try:
                await cb.message.edit_text(_cancelled_text(bet, True), reply_markup=None, parse_mode="HTML")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await cb.answer("بازی منقضی شده است.", show_alert=True)
            return
        opponent = await s.get(BotUser, joiner.id, with_for_update=True)
        if not opponent or _money(opponent.balance) < _money(bet.amount):
            await cb.answer(f"موجودی کافی نیست؛ نیاز به {_rial(bet.amount)} داری.", show_alert=True)
            return
        creator = await s.get(BotUser, bet.creator_user_id, with_for_update=True)
        if not creator:
            await cb.answer("سازندهٔ بازی پیدا نشد.", show_alert=True)
            return
        opponent.balance = Decimal(str(opponent.balance)) - Decimal(str(bet.amount))
        winner_is_creator = __import__("secrets").randbelow(2) == 0
        winner_tg = bet.creator_telegram_id if winner_is_creator else cb.from_user.id
        loser_tg = cb.from_user.id if winner_is_creator else bet.creator_telegram_id
        bet.status = "settled"
        bet.opponent_user_id = opponent.id
        bet.opponent_telegram_id = cb.from_user.id
        bet.opponent_name = cb.from_user.full_name or ""
        bet.opponent_username = cb.from_user.username or ""
        bet.winner_telegram_id = winner_tg
        bet.loser_telegram_id = loser_tg
        if winner_is_creator:
            bet.winner_name, bet.winner_username = bet.creator_name, bet.creator_username
            bet.loser_name, bet.loser_username = cb.from_user.full_name or "", cb.from_user.username or ""
            winner_user, loser_user = creator, opponent
        else:
            bet.winner_name, bet.winner_username = cb.from_user.full_name or "", cb.from_user.username or ""
            bet.loser_name, bet.loser_username = bet.creator_name, bet.creator_username
            winner_user, loser_user = opponent, creator
        winner_user.balance = Decimal(str(winner_user.balance)) + Decimal(str(bet.payout_amount))
        bet.accepted_at = datetime.utcnow()
        bet.settled_at = datetime.utcnow()
        s.add(BetLedger(bot_id=bet.bot_id, bet_id=bet.id, user_id=opponent.id, telegram_id=cb.from_user.id, amount=-bet.amount, kind="stake"))
        s.add(BetLedger(bot_id=bet.bot_id, bet_id=bet.id, user_id=winner_user.id, telegram_id=winner_tg, amount=bet.payout_amount, kind="prize"))
        s.add(WalletTxn(bot_id=bet.bot_id, user_id=opponent.id, amount=-bet.amount, kind="bet_stake", note=f"پیوستن به بازی #{bet.id}"))
        s.add(WalletTxn(bot_id=bet.bot_id, user_id=winner_user.id, amount=bet.payout_amount, kind="bet_prize", note=f"جایزهٔ بازی #{bet.id}"))
        if _money(bet.fee_amount):
            s.add(BetLedger(bot_id=bet.bot_id, bet_id=bet.id, user_id=winner_user.id, telegram_id=winner_tg, amount=-bet.fee_amount, kind="fee"))
            s.add(WalletTxn(bot_id=bet.bot_id, user_id=winner_user.id, amount=0, kind="bet_fee", note=f"کارمزد بازی #{bet.id}: {_money(bet.fee_amount):,} تومان"))
        winner_balance_after = _money(winner_user.balance)
        loser_balance_after = _money(loser_user.balance)
        await s.commit()
        text = _settled_text(bet)
        result_kb = _settled_keyboard(bet.payout_amount, winner_balance_after, loser_balance_after)
    try:
        await cb.message.edit_text(text, reply_markup=result_kb, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        from tenant_group_guard import tenant_group_log
        await tenant_group_log(bot, row, bet.chat_id, "bet_settled", actor=cb.from_user.id,
                               target=bet.winner_telegram_id,
                               details=f"بازی #{bet.id}؛ برنده {bet.winner_telegram_id}؛ جایزه {_rial(bet.payout_amount)}")
    except Exception:
        logger.exception("ثبت لاگ تسویه بازی ناموفق بود")
    await cb.answer("بازی تسویه شد.")


@betting_router.callback_query(F.data.startswith("bet:cancel:"))
async def cancel_bet(cb: CallbackQuery, bot: Bot):
    bet_id = int(cb.data.rsplit(":", 1)[1])
    row = await _bot_for_message(bot)
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        bet = await s.get(Bet, bet_id, with_for_update=True)
        if not bet or bet.bot_id != row.id:
            await cb.answer("این بازی پیدا نشد.", show_alert=True)
            return
        if bet.creator_telegram_id != cb.from_user.id:
            await cb.answer("فقط سازنده می‌تواند بازی را لغو کند.", show_alert=True)
            return
        if bet.status != "pending":
            await cb.answer("این بازی قبلاً تعیین‌تکلیف شده است.", show_alert=True)
            return
        creator = await s.get(BotUser, bet.creator_user_id, with_for_update=True)
        bet.status = "cancelled"
        if creator:
            creator.balance = Decimal(str(creator.balance)) + Decimal(str(bet.amount))
            s.add(BetLedger(bot_id=bet.bot_id, bet_id=bet.id, user_id=creator.id, telegram_id=creator.telegram_id, amount=bet.amount, kind="refund"))
            s.add(WalletTxn(bot_id=bet.bot_id, user_id=creator.id, amount=bet.amount, kind="bet_refund", note=f"لغو بازی #{bet.id}"))
        await s.commit()
        text = _cancelled_text(bet)
    try:
        await cb.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        from tenant_group_guard import tenant_group_log
        await tenant_group_log(bot, row, bet.chat_id, "bet_cancelled", actor=cb.from_user.id,
                               details=f"بازی #{bet.id} لغو و مبلغ {_rial(bet.amount)} مسترد شد")
    except Exception:
        logger.exception("ثبت لاگ لغو بازی ناموفق بود")
    await cb.answer("بازی لغو شد.")


class BetSettingsFSM(StatesGroup):
    fee_pct = State()
    min_amount = State()
    max_amount = State()
    ttl = State()


async def _bet_settings_view(bot_id: int):
    enabled = (await _get_setting(bot_id, "group_betting_enabled", "0")) == "1"
    fee_enabled = (await _get_setting(bot_id, "group_betting_fee_enabled", "0")) == "1"
    fee = await _get_setting(bot_id, "group_betting_fee_pct", "0")
    minimum = int(_digits(await _get_setting(bot_id, "group_betting_min", "1000")) or 1000)
    maximum = int(_digits(await _get_setting(bot_id, "group_betting_max", "0")) or 0)
    ttl = int(_digits(await _get_setting(bot_id, "group_betting_ttl", "300")) or 300)
    text = (
        "🎲 <b>تنظیمات بازی گروه</b>\n\n"
        f"وضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}\n"
        f"کارمزد: {'✅ ' + str(fee) + '٪' if fee_enabled else '❌ غیرفعال'}\n"
        f"حداقل مبلغ: {_rial(minimum)}\n"
        f"حداکثر مبلغ: {_rial(maximum) if maximum else 'نامحدود'}\n"
        f"زمان انتظار: <b>{ttl}</b> ثانیه"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش‌کردن بازی" if enabled else "🟢 روشن‌کردن بازی"), callback_data="gset:bet_toggle")],
        [InlineKeyboardButton(text=f"💸 درصد کارمزد ({fee}٪)", callback_data="gset:bet_fee")],
        [InlineKeyboardButton(text="🔢 حداقل مبلغ", callback_data="gset:bet_min"), InlineKeyboardButton(text="🔢 حداکثر مبلغ", callback_data="gset:bet_max")],
        [InlineKeyboardButton(text=f"⏱ زمان انتظار ({ttl} ثانیه)", callback_data="gset:bet_ttl")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:groupsettings", style=ButtonStyle.DANGER)],
    ])
    return text, kb


async def _show_bet_settings(message, bot_id: int):
    text, kb = await _bet_settings_view(bot_id)
    await message.edit_text(text, reply_markup=kb, parse_mode="HTML") if hasattr(message, "edit_text") else await message.answer(text, reply_markup=kb, parse_mode="HTML")


@betting_router.callback_query(F.data == "gset:betmenu")
async def betting_settings_menu(cb: CallbackQuery, bot: Bot):
    row = await _bot_for_message(bot)
    if not row or not await _is_admin(row, cb.from_user.id):
        await cb.answer()
        return
    text, kb = await _bet_settings_view(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@betting_router.callback_query(F.data == "gset:bet_toggle")
async def betting_toggle(cb: CallbackQuery, bot: Bot):
    row = await _bot_for_message(bot)
    if not row or not await _is_admin(row, cb.from_user.id):
        await cb.answer()
        return
    cur = (await _get_setting(row.id, "group_betting_enabled", "0")) == "1"
    await _set_setting(row.id, "group_betting_enabled", "0" if cur else "1")
    text, kb = await _bet_settings_view(row.id)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer("✅ ذخیره شد")


async def _start_setting(cb: CallbackQuery, bot: Bot, state: FSMContext, target_state: State, prompt: str):
    row = await _bot_for_message(bot)
    if not row or not await _is_admin(row, cb.from_user.id):
        await cb.answer()
        return
    await state.set_state(target_state)
    await cb.message.answer(prompt)
    await cb.answer()


@betting_router.callback_query(F.data == "gset:bet_fee")
async def betting_fee_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _start_setting(cb, bot, state, BetSettingsFSM.fee_pct, "💸 درصد کارمزد بازی را بفرست (۰ تا ۹۹؛ برای خاموشی ۰):")


@betting_router.callback_query(F.data == "gset:bet_min")
async def betting_min_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _start_setting(cb, bot, state, BetSettingsFSM.min_amount, "🔢 حداقل مبلغ بازی را به تومان بفرست:")


@betting_router.callback_query(F.data == "gset:bet_max")
async def betting_max_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _start_setting(cb, bot, state, BetSettingsFSM.max_amount, "🔢 حداکثر مبلغ بازی را به تومان بفرست (۰ یعنی نامحدود):")


@betting_router.callback_query(F.data == "gset:bet_ttl")
async def betting_ttl_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _start_setting(cb, bot, state, BetSettingsFSM.ttl, "⏱ زمان انتظار بازی را به ثانیه بفرست (۶۰ تا ۸۶۴۰۰):")


async def _save_setting_input(msg: Message, bot: Bot, state: FSMContext, state_obj: State, key: str, parse, error: str):
    current = await state.get_state()
    if current != state_obj.state:
        return
    row = await _bot_for_message(bot)
    await state.clear()
    if not row or not await _is_admin(row, msg.from_user.id):
        return
    raw = _digits((msg.text or "").strip())
    try:
        value = parse(raw)
    except Exception:
        await msg.answer(error)
        return
    await _set_setting(row.id, key, str(value))
    if key == "group_betting_fee_pct":
        await _set_setting(row.id, "group_betting_fee_enabled", "1" if value > 0 else "0")
    text, kb = await _bet_settings_view(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@betting_router.message(BetSettingsFSM.fee_pct, F.text)
async def betting_fee_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_setting_input(msg, bot, state, BetSettingsFSM.fee_pct, "group_betting_fee_pct", lambda x: int(x) if 0 <= int(x) < 100 else (_ for _ in ()).throw(ValueError()), "❌ درصد باید بین ۰ تا ۹۹ باشد.")


@betting_router.message(BetSettingsFSM.min_amount, F.text)
async def betting_min_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_setting_input(msg, bot, state, BetSettingsFSM.min_amount, "group_betting_min", lambda x: int(x) if int(x) >= 1 else (_ for _ in ()).throw(ValueError()), "❌ حداقل باید عدد مثبت باشد.")


@betting_router.message(BetSettingsFSM.max_amount, F.text)
async def betting_max_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_setting_input(msg, bot, state, BetSettingsFSM.max_amount, "group_betting_max", lambda x: int(x) if int(x) >= 0 else (_ for _ in ()).throw(ValueError()), "❌ حداکثر باید صفر یا عدد مثبت باشد.")


@betting_router.message(BetSettingsFSM.ttl, F.text)
async def betting_ttl_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_setting_input(msg, bot, state, BetSettingsFSM.ttl, "group_betting_ttl", lambda x: int(x) if 60 <= int(x) <= 86400 else (_ for _ in ()).throw(ValueError()), "❌ زمان باید بین ۶۰ تا ۸۶۴۰۰ ثانیه باشد.")
