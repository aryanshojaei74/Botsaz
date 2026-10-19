"""کیف‌پول و بازی گروهیِ بات‌ساز اصلی.

این ماژول از تنظیمات platform و مدل PlatformUser استفاده می‌کند و فقط روی
گروهی فعال می‌شود که به محافظ گروه وصل شده باشد و ادمین قابلیت را روشن کرده
باشد. AI اروان در ماژول محافظ گروه مستقل باقی می‌ماند.
"""
from __future__ import annotations

import asyncio
import html
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import PlatformBet, PlatformBetLedger, PlatformTxn, PlatformUser
from premium_emoji import hx_button

platform_group_money_router = Router()

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_TRANSFER_RE = re.compile(r"^انتقال\s+([۰-۹0-9,،]+)$")
_BET_RE = re.compile(r"^(?:بازی|شرط[\s\u200c]*بندی)\s+([۰-۹0-9,،]+)$", re.IGNORECASE)
_INCOMPLETE_BET_RE = re.compile(r"^(?:بازی|شرط[\s\u200c]*بندی)$", re.IGNORECASE)
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _lock(a: int, b: int) -> asyncio.Lock:
    key = (min(a, b), max(a, b))
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _digits(value: str) -> str:
    return value.translate(_DIGITS).replace(",", "").replace("،", "").strip()


def _int(value, default: int = 0) -> int:
    try:
        return int(Decimal(str(value or default)))
    except Exception:
        return default


def _money(value) -> int:
    return int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rial(value) -> str:
    return f"{_money(value):,} تومان"


def _name(tg_id: int, username: str | None, full_name: str | None) -> str:
    return "@" + html.escape(username) if username else html.escape(full_name or str(tg_id))


async def _pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(key, default)


async def _set(key: str, value: str) -> None:
    from platform_bot import set_pset
    await set_pset(key, value)


async def _group_allowed(chat_id: int) -> bool:
    guard_chat = (await _pset("group_guard_chat_id", "")).strip()
    return bool(guard_chat and str(chat_id) == guard_chat and (await _pset("group_guard_enabled", "off")) == "on")


async def _user(tg_id: int, full_name: str = "", username: str = "") -> PlatformUser:
    async with Session() as s:
        user = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == tg_id))
        if user is None:
            user = PlatformUser(telegram_id=tg_id, full_name=full_name or str(tg_id), username=username or None)
            s.add(user)
            await s.commit()
        elif full_name or username:
            user.full_name = full_name or user.full_name
            user.username = username or user.username
            await s.commit()
        return user


async def _wallet_enabled() -> bool:
    return (await _pset("platform_group_wallet_enabled", "off")) == "on"


async def _bet_enabled() -> bool:
    return (await _pset("platform_group_betting_enabled", "off")) == "on"



@platform_group_money_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: bool(_INCOMPLETE_BET_RE.match((m.text or "").strip()))),
)
async def platform_incomplete_bet_command(msg: Message):
    if not await _group_allowed(msg.chat.id):
        return
    if not await _bet_enabled():
        await msg.reply("❌ شرط‌بندی در بات‌ساز اصلی فعال نیست.")
        return
    from platform_wager import GAMES, game_enabled, help_text
    await msg.reply(help_text({g: await game_enabled(g) for g in GAMES}), parse_mode="HTML")

@platform_group_money_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: (m.text or "").strip() == "موجودی"),
)
async def platform_group_balance(msg: Message):
    if not await _group_allowed(msg.chat.id) or not await _wallet_enabled():
        return
    user = await _user(msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    async with Session() as s:
        user = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == msg.from_user.id))
        balance = user.balance if user else 0
    await msg.reply("💰 موجودیِ فعلی شما:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        hx_button(_rial(balance), callback_data="pgm:noop", style=ButtonStyle.PRIMARY)
    ]]))


@platform_group_money_router.callback_query(F.data == "pgm:noop")
async def platform_group_noop(cb: CallbackQuery):
    """دکمهٔ نمایشِ موجودی فقط نمایشی است؛ بی‌هندلر بودنش دکمه را در حالتِ
    «در حالِ بارگذاری» گیر می‌انداخت."""
    await cb.answer()


def _reply_target(msg: Message):
    """کاربری که واقعاً به او ریپلای شده؛ None برایِ ریپلایِ ضمنیِ تاپیکِ
    فوروم (پیامِ «ساختِ تاپیک» = سازندهٔ تاپیک)، پستِ خودکارِ کانال (۷۷۷۰۰۰)
    یا پیامی که «از طرفِ کانال/گروه» فرستاده شده."""
    r = msg.reply_to_message
    if not r or not r.from_user:
        return None
    if r.forum_topic_created or r.is_automatic_forward or r.sender_chat:
        return None
    if r.from_user.id == 777000:
        return None
    return r.from_user


@platform_group_money_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: (m.text or "").strip().startswith("انتقال")),
)
async def platform_group_transfer(msg: Message):
    if not await _group_allowed(msg.chat.id) or not await _wallet_enabled():
        return
    m = _TRANSFER_RE.match((msg.text or "").strip())
    if not m or msg.sender_chat:
        return
    target = _reply_target(msg)
    if not target:
        return
    if target.is_bot or target.id == msg.from_user.id:
        await msg.reply("❌ انتقال به خودت یا ربات امکان‌پذیر نیست.")
        return
    amount = _int(_digits(m.group(1)))
    minimum = max(1, _int(await _pset("platform_group_transfer_min", "1000"), 1000))
    maximum = _int(await _pset("platform_group_transfer_max", "0"))
    if amount < minimum:
        await msg.reply(f"❌ حداقل انتقال {_rial(minimum)} است.")
        return
    if maximum and amount > maximum:
        await msg.reply(f"❌ حداکثر انتقال {_rial(maximum)} است.")
        return
    fee_enabled = (await _pset("platform_group_transfer_fee_enabled", "off")) == "on"
    fee_pct = Decimal(str(await _pset("platform_group_transfer_fee_pct", "0") or 0)) if fee_enabled else Decimal("0")
    fee = int((Decimal(amount) * fee_pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    src = await _user(msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    dst = await _user(target.id, target.full_name or "", target.username or "")
    async with _lock(msg.from_user.id, target.id):
        async with Session() as s:
            src = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == msg.from_user.id).with_for_update())
            dst = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == target.id).with_for_update())
            total = amount + fee
            if not src or _money(src.balance) < total:
                await msg.reply(f"❌ موجودی کافی نیست. نیاز: {_rial(total)}")
                return
            src.balance = Decimal(str(src.balance)) - Decimal(total)
            dst.balance = Decimal(str(dst.balance or 0)) + Decimal(amount)
            s.add(PlatformTxn(user_telegram_id=src.telegram_id, amount=-total, kind="group_transfer", note=f"انتقال به {dst.telegram_id}"))
            s.add(PlatformTxn(user_telegram_id=dst.telegram_id, amount=amount, kind="group_transfer", note=f"دریافت از {src.telegram_id}"))
            if fee:
                s.add(PlatformTxn(user_telegram_id=src.telegram_id, amount=-fee, kind="group_fee", note="کارمزد انتقال گروهی"))
            new_src, new_dst = _money(src.balance), _money(dst.balance)
            await s.commit()
    await msg.reply(f"✅ انتقال انجام شد.\n\nمبلغ انتقال: {_rial(amount)}\nکارمزد: {_rial(fee)}\nموجودی شما: {_rial(new_src)}\nموجودی مقصد: {_rial(new_dst)}")


def _bet_kb(bet: PlatformBet) -> InlineKeyboardMarkup:
    if bet.status != "pending":
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("✅ قبول و پیوستن", callback_data=f"pgb:join:{bet.id}", style=ButtonStyle.SUCCESS),
        hx_button("❌ لغو", callback_data=f"pgb:cancel:{bet.id}", style=ButtonStyle.DANGER),
    ]])


def _bet_pending_text(bet: PlatformBet) -> str:
    fee = _money(bet.fee_amount)
    participant_line = (
        f"🎯 شرکت‌کنندهٔ تعیین‌شده: {_name(bet.target_telegram_id, bet.target_username, bet.target_name)}\n"
        if bet.target_telegram_id else
        "👥 حالت عمومی: اولین نفری که روی «✅ قبول و پیوستن» بزند، شرکت می‌کند.\n"
    )
    return (
        "🎲 <b>شرط‌بندی شانسی (شیر یا خط)</b>\n\n"
        f"👤 سازنده: {_name(bet.creator_telegram_id, bet.creator_username, bet.creator_name)}\n"
        f"{participant_line}"
        f"💰 مبلغ بازی هر نفر: <b>{_rial(bet.amount)}</b>\n"
        f"💸 کارمزد: <b>{_rial(fee)}</b> ({_money(bet.fee_pct):g}٪)\n"
        f"🎁 جایزهٔ نهایی: <b>{_rial(bet.payout_amount)}</b>\n\n"
        "⏳ وضعیت: در انتظار پیوستن"
    )


def _bet_result_text(bet: PlatformBet) -> str:
    return (
        "🎲 <b>نتیجهٔ شرط‌بندی مشخص شد</b> 🎉\n\n"
        f"🏆 برنده: <b>{_name(bet.winner_telegram_id, bet.winner_username, bet.winner_name)}</b>\n"
        f"❌ بازنده: <b>{_name(bet.loser_telegram_id, bet.loser_username, bet.loser_name)}</b>\n"
        f"💰 مبلغ بازی: <b>{_rial(bet.amount)}</b> برای هر نفر\n"
        f"💸 کارمزد: <b>{_rial(bet.fee_amount)}</b> ({_money(bet.fee_pct):g}٪)\n"
        f"🎁 مبلغ نهایی دریافتی برنده: <b>{_rial(bet.payout_amount)}</b>"
    )


def _bet_result_kb(bet: PlatformBet, winner_balance: int, loser_balance: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🎁 جایزهٔ برنده", callback_data="pgb:info", style=ButtonStyle.SUCCESS), hx_button(_rial(bet.payout_amount), callback_data="pgb:info", style=ButtonStyle.SUCCESS)],
        [hx_button("💎 موجودیِ برنده", callback_data="pgb:info", style=ButtonStyle.PRIMARY), hx_button(_rial(winner_balance), callback_data="pgb:info", style=ButtonStyle.PRIMARY)],
        [hx_button("❌ موجودیِ بازنده", callback_data="pgb:info", style=ButtonStyle.DANGER), hx_button(_rial(loser_balance), callback_data="pgb:info", style=ButtonStyle.DANGER)],
    ])


@platform_group_money_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(lambda m: bool(_BET_RE.match((m.text or "").strip()))),
)
async def platform_create_bet(msg: Message):
    if not await _group_allowed(msg.chat.id) or not await _bet_enabled():
        return
    if msg.sender_chat:
        return
    target = _reply_target(msg)
    if target and (target.is_bot or target.id == msg.from_user.id):
        await msg.reply("❌ سازنده نباید خودش یا یک ربات را هدف بگیرد.")
        return
    match = _BET_RE.match((msg.text or "").strip())
    amount = _int(_digits(match.group(1)))
    minimum = max(1, _int(await _pset("platform_group_betting_min", "1000"), 1000))
    maximum = _int(await _pset("platform_group_betting_max", "0"))
    if amount < minimum or (maximum and amount > maximum):
        await msg.reply(f"❌ مبلغ باید بین {_rial(minimum)} و {(_rial(maximum) if maximum else 'بدون سقف')} باشد.")
        return
    fee_on = (await _pset("platform_group_betting_fee_enabled", "off")) == "on"
    fee_pct = Decimal(str(await _pset("platform_group_betting_fee_pct", "0") or 0)) if fee_on else Decimal("0")
    fee = int((Decimal(amount * 2) * fee_pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    payout = amount * 2 - fee
    if payout <= 0:
        await msg.reply("❌ کارمزد باید کمتر از مجموع مبلغ دو نفر باشد.")
        return
    ttl = max(60, min(_int(await _pset("platform_group_betting_ttl", "300"), 300), 86400))
    await _user(msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    async with Session() as s:
        creator = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == msg.from_user.id).with_for_update())
        if not creator or _money(creator.balance) < amount:
            await msg.reply(f"❌ موجودی کافی نیست. نیاز: {_rial(amount)}")
            return
        creator.balance = Decimal(str(creator.balance)) - Decimal(amount)
        bet = PlatformBet(chat_id=msg.chat.id, message_id=0, creator_telegram_id=msg.from_user.id,
                          creator_name=msg.from_user.full_name or "", creator_username=msg.from_user.username or None,
                          target_telegram_id=target.id if target else 0,
                          target_name=target.full_name if target else "", target_username=target.username if target else None,
                          amount=amount, fee_pct=fee_pct, fee_amount=fee, payout_amount=payout,
                          status="pending", expires_at=datetime.utcnow() + timedelta(seconds=ttl))
        s.add(bet)
        await s.flush()
        s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=creator.telegram_id, amount=-amount, kind="reserve"))
        s.add(PlatformTxn(user_telegram_id=creator.telegram_id, amount=-amount, kind="group_bet_reserve", note=f"رزرو بازی #{bet.id}"))
        await s.commit()
        bet_id = bet.id
    sent = await msg.reply("⏳ در حال ساخت بازی...")
    async with Session() as s:
        bet = await s.get(PlatformBet, bet_id)
        bet.message_id = sent.message_id
        await s.commit()
        await sent.edit_text(_bet_pending_text(bet), reply_markup=_bet_kb(bet), parse_mode="HTML")


@platform_group_money_router.callback_query(F.data.startswith("pgb:join:"))
async def platform_join_bet(cb: CallbackQuery):
    bet_id = _int(cb.data.rsplit(":", 1)[1])
    if not await _group_allowed(cb.message.chat.id if cb.message else 0) or not await _bet_enabled():
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return
    await _user(cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    async with _lock(cb.from_user.id, bet_id):
        async with Session() as s:
            bet = await s.get(PlatformBet, bet_id, with_for_update=True)
            if not bet or bet.status != "pending":
                await cb.answer("این بازی دیگر در انتظار نیست.", show_alert=True)
                return
            if bet.creator_telegram_id == cb.from_user.id:
                await cb.answer("سازنده نمی‌تواند خودش به بازی بپیوندد.", show_alert=True)
                return
            if bet.target_telegram_id and bet.target_telegram_id != cb.from_user.id:
                await cb.answer("این بازی برای کاربری است که پیامش ریپلای شده.", show_alert=True)
                return
            if bet.expires_at and bet.expires_at <= datetime.utcnow():
                bet.status = "expired"
                creator = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == bet.creator_telegram_id).with_for_update())
                if creator:
                    creator.balance = Decimal(str(creator.balance)) + Decimal(str(bet.amount))
                    s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=creator.telegram_id, amount=bet.amount, kind="refund"))
                    s.add(PlatformTxn(user_telegram_id=creator.telegram_id, amount=bet.amount, kind="group_bet_refund", note=f"انقضای بازی #{bet.id}"))
                await s.commit()
                await cb.message.edit_text("⏰ بازی منقضی شد و مبلغ سازنده برگشت.")
                await cb.answer("بازی منقضی شده است.", show_alert=True)
                return
            creator = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == bet.creator_telegram_id).with_for_update())
            opponent = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == cb.from_user.id).with_for_update())
            if not opponent or _money(opponent.balance) < _money(bet.amount):
                await cb.answer(f"موجودی کافی نیست؛ نیاز به {_rial(bet.amount)} داری.", show_alert=True)
                return
            opponent.balance = Decimal(str(opponent.balance)) - Decimal(str(bet.amount))
            winner_is_creator = secrets.randbelow(2) == 0
            winner = creator if winner_is_creator else opponent
            loser = opponent if winner_is_creator else creator
            bet.status = "settled"
            bet.opponent_telegram_id = opponent.telegram_id
            bet.opponent_name = opponent.full_name or ""
            bet.opponent_username = opponent.username
            bet.accepted_at = datetime.utcnow()
            bet.settled_at = datetime.utcnow()
            bet.winner_telegram_id = winner.telegram_id
            bet.winner_name = winner.full_name or ""
            bet.winner_username = winner.username
            bet.loser_telegram_id = loser.telegram_id
            bet.loser_name = loser.full_name or ""
            bet.loser_username = loser.username
            winner.balance = Decimal(str(winner.balance)) + Decimal(str(bet.payout_amount))
            if _money(bet.fee_amount):
                s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=winner.telegram_id, amount=-bet.fee_amount, kind="fee"))
                s.add(PlatformTxn(user_telegram_id=winner.telegram_id, amount=0, kind="group_bet_fee", note=f"کارمزد بازی #{bet.id}"))
            s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=opponent.telegram_id, amount=-bet.amount, kind="stake"))
            s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=winner.telegram_id, amount=bet.payout_amount, kind="prize"))
            s.add(PlatformTxn(user_telegram_id=opponent.telegram_id, amount=-bet.amount, kind="group_bet_stake", note=f"پیوستن به بازی #{bet.id}"))
            s.add(PlatformTxn(user_telegram_id=winner.telegram_id, amount=bet.payout_amount, kind="group_bet_prize", note=f"جایزهٔ بازی #{bet.id}"))
            wb, lb = _money(winner.balance), _money(loser.balance)
            await s.commit()
            text, kb = _bet_result_text(bet), _bet_result_kb(bet, wb, lb)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer("بازی تسویه شد.")


@platform_group_money_router.callback_query(F.data.startswith("pgb:cancel:"))
async def platform_cancel_bet(cb: CallbackQuery):
    bet_id = _int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        bet = await s.get(PlatformBet, bet_id, with_for_update=True)
        if not bet or bet.status != "pending":
            await cb.answer("این بازی قبلاً تعیین‌تکلیف شده است.", show_alert=True)
            return
        if bet.creator_telegram_id != cb.from_user.id:
            await cb.answer("فقط سازنده می‌تواند لغو کند.", show_alert=True)
            return
        bet.status = "cancelled"
        creator = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == bet.creator_telegram_id).with_for_update())
        if creator:
            creator.balance = Decimal(str(creator.balance)) + Decimal(str(bet.amount))
            s.add(PlatformBetLedger(bet_id=bet.id, telegram_id=creator.telegram_id, amount=bet.amount, kind="refund"))
            s.add(PlatformTxn(user_telegram_id=creator.telegram_id, amount=bet.amount, kind="group_bet_refund", note=f"لغو بازی #{bet.id}"))
        await s.commit()
    await cb.message.edit_text("❌ بازی لغو شد و مبلغ رزروشده به سازنده برگشت.")
    await cb.answer("لغو شد")


@platform_group_money_router.callback_query(F.data == "pgb:info")
async def platform_bet_info(cb: CallbackQuery):
    await cb.answer()


class PlatformGroupSettingsFSM(StatesGroup):
    transfer_fee = State()
    transfer_min = State()
    transfer_max = State()
    bet_fee = State()
    bet_min = State()
    bet_max = State()
    bet_ttl = State()


def _admin(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


def _bet_back_button(uid: int | None):
    """=== PATCH_PLATFORM_NAVBACK_V1 === بازگشت تنظیمات شرط‌بندی: به صفحه‌ای که از آن آمده‌ای (هوش مصنوعی / صفحهٔ مشترک)."""
    from platform_navback import back
    target = back(uid, "bet", "gg:ai")
    label = "⬅️ بازگشت به بازی‌ها و شرط‌بندی" if target == "pgw:hub" else "⬅️ بازگشت به هوش مصنوعی"
    return label, {"callback_data": target, "style": ButtonStyle.DANGER}


async def _settings_screen(kind: str, uid: int | None = None):
    if kind == "wallet":
        enabled = (await _pset("platform_group_wallet_enabled", "off")) == "on"
        fee_on = (await _pset("platform_group_transfer_fee_enabled", "off")) == "on"
        fee = await _pset("platform_group_transfer_fee_pct", "0")
        mn = _int(await _pset("platform_group_transfer_min", "1000"), 1000)
        mx = _int(await _pset("platform_group_transfer_max", "0"))
        text = f"💰 <b>تنظیمات انتقال گروهی</b>\n\nوضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}\nکارمزد: {'✅ ' + fee + '٪' if fee_on else '❌ غیرفعال'}\nحداقل: {_rial(mn)}\nحداکثر: {_rial(mx) if mx else 'نامحدود'}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("🔴 خاموش‌کردن" if enabled else "🟢 روشن‌کردن", callback_data="pgmset:wallet_toggle", style=ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS)],
            [hx_button("💸 کارمزد انتقال", callback_data="pgmset:transfer_fee")],
            [hx_button("🔢 حداقل انتقال", callback_data="pgmset:transfer_min"), hx_button("🔢 حداکثر انتقال", callback_data="pgmset:transfer_max")],
            [hx_button("⬅️ بازگشت به هوش مصنوعی", callback_data="gg:ai", style=ButtonStyle.DANGER)],
        ])
    else:
        enabled = (await _pset("platform_group_betting_enabled", "off")) == "on"
        fee_on = (await _pset("platform_group_betting_fee_enabled", "off")) == "on"
        fee = await _pset("platform_group_betting_fee_pct", "0")
        mn = _int(await _pset("platform_group_betting_min", "1000"), 1000)
        mx = _int(await _pset("platform_group_betting_max", "0"))
        ttl = _int(await _pset("platform_group_betting_ttl", "300"), 300)
        text = f"🎲 <b>تنظیمات شرط‌بندی گروه</b>\n\nوضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}\nکارمزد: {'✅ ' + fee + '٪' if fee_on else '❌ غیرفعال'}\nحداقل: {_rial(mn)}\nحداکثر: {_rial(mx) if mx else 'نامحدود'}\nزمان انتظار: {ttl} ثانیه"
        # === PATCH_PLATFORM_WAGER_V1 === بازی‌های دونفره (ایکس‌او / سنگ‌کاغذ‌قیچی / تاس) روی همین کلید اصلی و همین کارمزد/حداقل/حداکثر
        from platform_wager import GAMES, GAME_FA, game_enabled
        flags = {g: await game_enabled(g) for g in GAMES}
        text += "\n\n🎮 بازی‌ها (در کنار شیر یا خط):\n" + "\n".join(
            f"{'✅' if flags[g] else '❌'} {GAME_FA[g]}" for g in GAMES)
        back_label, back_kw = _bet_back_button(uid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("🔴 خاموش‌کردن" if enabled else "🟢 روشن‌کردن", callback_data="pgbset:toggle", style=ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS)],
            [hx_button(f"{'🔴 خاموش' if flags[g] else '🟢 روشن'}: {GAME_FA[g]}", callback_data=f"pgwset:{g}") for g in GAMES],
            [hx_button("💸 کارمزد شرط‌بندی", callback_data="pgbset:fee")],
            [hx_button("🔢 حداقل شرط", callback_data="pgbset:min"), hx_button("🔢 حداکثر شرط", callback_data="pgbset:max")],
            [hx_button("⏱ زمان انتظار", callback_data="pgbset:ttl")],
            [hx_button("🎲 بازی کن پول بگیر (بازی‌های تلگرام)", callback_data="padg:menu@bet")],
            [hx_button(back_label, **back_kw)],
        ])
    return text, kb


async def _edit_settings(cb: CallbackQuery, kind: str):
    text, kb = await _settings_screen(kind, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@platform_group_money_router.callback_query(F.data == "gg:wallet_settings")
async def main_wallet_settings(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await _edit_settings(cb, "wallet")


@platform_group_money_router.callback_query(F.data.startswith("gg:bet_settings"))
async def main_bet_settings(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    # === PATCH_PLATFORM_NAVBACK_V1 === gg:bet_settings = از هوش مصنوعی، @hub = از صفحهٔ مشترک، @keep = برگشت از «بازی کن پول بگیر»
    from platform_navback import enter, split
    enter(cb.from_user.id, "bet", split(cb.data)[1], {"hub": "pgw:hub"})
    await _edit_settings(cb, "bet")


@platform_group_money_router.callback_query(F.data == "pgmset:wallet_toggle")
async def main_wallet_toggle(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await _pset("platform_group_wallet_enabled", "off")
    await _set("platform_group_wallet_enabled", "off" if cur == "on" else "on")
    await _edit_settings(cb, "wallet")


@platform_group_money_router.callback_query(F.data == "pgbset:toggle")
async def main_bet_toggle(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await _pset("platform_group_betting_enabled", "off")
    await _set("platform_group_betting_enabled", "off" if cur == "on" else "on")
    await _edit_settings(cb, "bet")


async def _start_input(cb: CallbackQuery, state: FSMContext, target: State, prompt: str):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(target)
    await cb.message.answer(prompt)
    await cb.answer()


@platform_group_money_router.callback_query(F.data == "pgmset:transfer_fee")
async def main_transfer_fee_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.transfer_fee, "💸 درصد کارمزد انتقال را بفرست (۰ تا ۹۹؛ صفر یعنی خاموش):")


@platform_group_money_router.callback_query(F.data == "pgmset:transfer_min")
async def main_transfer_min_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.transfer_min, "🔢 حداقل مبلغ انتقال را به تومان بفرست:")


@platform_group_money_router.callback_query(F.data == "pgmset:transfer_max")
async def main_transfer_max_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.transfer_max, "🔢 حداکثر مبلغ انتقال را به تومان بفرست (۰ یعنی نامحدود):")


@platform_group_money_router.callback_query(F.data == "pgbset:fee")
async def main_bet_fee_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.bet_fee, "💸 درصد کارمزد بازی را بفرست (۰ تا ۹۹؛ صفر یعنی خاموش):")


@platform_group_money_router.callback_query(F.data == "pgbset:min")
async def main_bet_min_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.bet_min, "🔢 حداقل مبلغ بازی را به تومان بفرست:")


@platform_group_money_router.callback_query(F.data == "pgbset:max")
async def main_bet_max_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.bet_max, "🔢 حداکثر مبلغ بازی را به تومان بفرست (۰ یعنی نامحدود):")


@platform_group_money_router.callback_query(F.data == "pgbset:ttl")
async def main_bet_ttl_start(cb: CallbackQuery, state: FSMContext):
    await _start_input(cb, state, PlatformGroupSettingsFSM.bet_ttl, "⏱ زمان انتظار بازی را به ثانیه بفرست (۶۰ تا ۸۶۴۰۰):")


async def _save_input(msg: Message, state: FSMContext, state_obj: State, key: str, min_value: int, max_value: int, unit: str):
    if await state.get_state() != state_obj.state:
        return
    await state.clear()
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    raw = _digits(msg.text or "")
    try:
        val = int(raw)
        if val < min_value or val > max_value:
            raise ValueError
    except Exception:
        await msg.answer(f"❌ مقدار باید بین {min_value} و {max_value} باشد.")
        return
    await _set(key, str(val))
    if key in ("platform_group_transfer_fee_pct", "platform_group_betting_fee_pct"):
        await _set(key.replace("_pct", "_enabled"), "on" if val > 0 else "off")
    await msg.answer(f"✅ ذخیره شد: {val} {unit}")


@platform_group_money_router.message(PlatformGroupSettingsFSM.transfer_fee, F.text)
async def main_transfer_fee_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.transfer_fee, "platform_group_transfer_fee_pct", 0, 99, "درصد")


@platform_group_money_router.message(PlatformGroupSettingsFSM.transfer_min, F.text)
async def main_transfer_min_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.transfer_min, "platform_group_transfer_min", 1, 10**12, "تومان")


@platform_group_money_router.message(PlatformGroupSettingsFSM.transfer_max, F.text)
async def main_transfer_max_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.transfer_max, "platform_group_transfer_max", 0, 10**12, "تومان")


@platform_group_money_router.message(PlatformGroupSettingsFSM.bet_fee, F.text)
async def main_bet_fee_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.bet_fee, "platform_group_betting_fee_pct", 0, 99, "درصد")


@platform_group_money_router.message(PlatformGroupSettingsFSM.bet_min, F.text)
async def main_bet_min_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.bet_min, "platform_group_betting_min", 1, 10**12, "تومان")


@platform_group_money_router.message(PlatformGroupSettingsFSM.bet_max, F.text)
async def main_bet_max_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.bet_max, "platform_group_betting_max", 0, 10**12, "تومان")


@platform_group_money_router.message(PlatformGroupSettingsFSM.bet_ttl, F.text)
async def main_bet_ttl_save(msg: Message, state: FSMContext):
    await _save_input(msg, state, PlatformGroupSettingsFSM.bet_ttl, "platform_group_betting_ttl", 60, 86400, "ثانیه")
