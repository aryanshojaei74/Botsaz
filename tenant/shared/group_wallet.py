"""
group_wallet.py — دستورهایِ کیف‌پول داخلِ گروه (PATCH_GROUP_WALLET_V1).

دو قابلیت:
  ۱) «موجودی» — کاربر داخلِ گروه می‌فرسته، ربات موجودیِ کیفِ‌پولِ خودش
     (در همین ربات) رو با یه دکمهٔ اینلاین نشون می‌ده.
  ۲) «انتقال <عدد>» — کاربر رویِ پیامِ یه کاربرِ دیگه ریپلای می‌کنه و
     می‌فرسته «انتقال 1000»؛ همون مقدار از موجودیِ خودش کم و به اون یکی
     اضافه می‌شه (بدونِ هیچ شانس/برد‌وباختی — این فقط جابه‌جاییِ مستقیمه،
     نه بازی).

این دو دستور فقط داخلِ گروه/سوپرگروه کار می‌کنن (نه پی‌وی)، و فقط وقتی
ربات بتونه صاحبِ گروه رو به یه ربات ثبت‌شده رو پلتفرم map کنه (یعنی خودِ
همین رباتِ مستأجر باید عضوِ گروه باشه).

=== PATCH_GROUP_SETTINGS_V1 ===
یه صفحه‌ی «⚙️ تنظیماتِ گروه» هم اینجا اضافه شده (زیرِ «دکمه‌ها و
تنظیمات») که سه‌تا چیز رو کنارِ هم می‌ذاره:
  - دستیارِ هوشِ مصنوعیِ گروه (اسم/اتصال/قطعِ اتصال — منطقش تویِ
    smm_ai.py می‌مونه، فقط دکمه‌هاش از اونجا به اینجا منتقل شدن).
  - کارمزدِ انتقالِ موجودی (اختیاری، با درصدِ دلخواه).
  - حداقل/حداکثرِ مبلغِ قابلِ‌انتقال.
"""
from __future__ import annotations

import asyncio
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser, WalletTxn
from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

group_wallet_router = Router()


def _group_feature_allowed(row) -> bool:
    """گروه و کیف‌پول برای خدمات مجازی و پلن‌های پولی VPN."""
    return bool(row and (row.field == "virtual_service" or (row.field == "vpn_sales" and row.plan in ("growth", "pro"))))

_MIN_TRANSFER_DEFAULT = 1000  # کمترین مبلغِ قابلِ‌انتقال (تومان) — جلویِ سوءاستفاده‌هایِ ریز رو می‌گیره

# === PATCH_GROUP_WALLET_V1 ===
# اگه چند نفر هم‌زمان تو یه گروه «انتقال» بفرستن، ممکنه با هم برخورد کنن؛
# یه قفلِ per-(bot,user) مثلِ همون الگویِ dice_game جلویِ ریس‌کاندیشن رو
# می‌گیره.
_transfer_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _get_lock(bot_id: int, telegram_id: int) -> asyncio.Lock:
    key = (bot_id, telegram_id)
    lock = _transfer_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _transfer_locks[key] = lock
    return lock


def _rial(n) -> str:
    return f"{int(n):,} تومان"


def _transfer_person(user, fallback: str = "کاربر") -> str:
    """نام نمایشی کوتاه برای پیام نتیجهٔ انتقال؛ شناسهٔ داخلی نمایش داده نمی‌شود."""
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    return (getattr(user, "full_name", None) or fallback).strip()[:80]


def _transfer_result_text(sender, receiver, amount: int, fee_amount: int,
                          fee_pct: float, receive_amount: int,
                          sender_balance: float, receiver_balance: float,
                          fee_by_sender: bool = False) -> str:
    sender_name = _transfer_person(sender, "فرستنده")
    receiver_name = _transfer_person(receiver, "گیرنده")
    if fee_amount > 0 and fee_by_sender:   # PATCH_GROUP_FEE_PAYER_V1
        fee_line = (f"💸 کارمزد انتقال: <b>{_rial(fee_amount)}</b> ({fee_pct:g}٪) — از حسابِ فرستنده\n"
                    f"💳 کسرشده از فرستنده: <b>{_rial(amount + fee_amount)}</b>\n")
    else:
        fee_line = (f"💸 کارمزد انتقال: <b>{_rial(fee_amount)}</b> ({fee_pct:g}٪)\n"
                    if fee_amount > 0 else "💸 کارمزد انتقال: <b>غیرفعال</b>\n")
    return (
        "💸 <b>انتقال موجودی با موفقیت انجام شد</b> ✅\n\n"
        f"👤 فرستنده: <b>{sender_name}</b>\n"
        f"🎯 گیرنده: <b>{receiver_name}</b>\n"
        f"💰 مبلغ انتقال: <b>{_rial(amount)}</b>\n"
        f"{fee_line}"
        f"📥 مبلغ دریافتی گیرنده: <b>{_rial(receive_amount)}</b>\n\n"
        "✅ تراکنش با موفقیت ثبت شد."
    )


def _transfer_result_keyboard(amount: int, sender_balance: float,
                              receiver_balance: float) -> InlineKeyboardMarkup:
    """اطلاعات انتقال را مانند نتیجهٔ بازی در دکمه‌های شیشه‌ای نشان می‌دهد."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💸 مبلغ انتقال", callback_data="group_transfer:info", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text=_rial(amount), callback_data="group_transfer:info", style=ButtonStyle.SUCCESS),
        ],
        [
            InlineKeyboardButton(text="💎 موجودی فرستنده", callback_data="group_transfer:info", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=_rial(sender_balance), callback_data="group_transfer:info", style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="📥 موجودی گیرنده", callback_data="group_transfer:info", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=_rial(receiver_balance), callback_data="group_transfer:info", style=ButtonStyle.PRIMARY),
        ],
    ])


async def _gset(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _gset_set(bot_id: int, key: str, value: str):
    from tenant import set_setting
    await set_setting(bot_id, key, value)


@group_wallet_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                             F.func(lambda m: (m.text or "").strip() == "موجودی"))
async def group_show_balance(msg: Message, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not _group_feature_allowed(row):
        return
    u = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "",
                                 msg.from_user.username or "")
    await msg.reply(
        "💰 موجودیِ فعلیِ شما:",
        # === PATCH_NOOP_HEADER_UNSTYLED_V1 ===
        # سرتیترها (callback_data="noop") باید بی‌رنگ بمانند؛ این دکمه یک
        # برچسبِ نمایشِ موجودی است، نه دکمهٔ قابلِ کلیک.
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            # === PATCH_GROUP_BALANCE_BLUE_V1 === به‌درخواستِ ادمین: دکمهٔ موجودی در گروه آبی باشد
            InlineKeyboardButton(text=_rial(u.balance), callback_data="noop", style=ButtonStyle.PRIMARY)]]))


_TRANSFER_RE = re.compile(r"^انتقال\s+([۰-۹0-9,،]+)$")


def _fa_digits_to_en(s: str) -> str:
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    return s.translate(table)


@group_wallet_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                             F.func(lambda m: (m.text or "").strip().startswith("انتقال")))
async def group_transfer_balance(msg: Message, bot: Bot):
    from tenant import get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not _group_feature_allowed(row):
        return
    m = _TRANSFER_RE.match((msg.text or "").strip())
    if not m:
        return
    raw = _fa_digits_to_en(m.group(1)).replace(",", "").replace("،", "")
    try:
        amount = int(raw)
    except ValueError:
        return

    # === PATCH_GROUP_SETTINGS_V1 ===
    min_amt = int(await _gset(row.id, "group_transfer_min", str(_MIN_TRANSFER_DEFAULT)) or _MIN_TRANSFER_DEFAULT)
    max_raw = await _gset(row.id, "group_transfer_max", "0")
    max_amt = int(max_raw) if max_raw and max_raw != "0" else 0
    if amount < min_amt:
        await msg.reply(f"❌ حداقلِ مبلغِ قابلِ‌انتقال {_rial(min_amt)} است.")
        return
    if max_amt and amount > max_amt:
        await msg.reply(f"❌ حداکثرِ مبلغِ قابلِ‌انتقال {_rial(max_amt)} است.")
        return
    # ریپلایِ ضمنیِ تاپیکِ فوروم (= سازندهٔ تاپیک)، پستِ خودکارِ کانال (۷۷۷۰۰۰) یا پیامِ «از طرفِ
    # کانال» گیرندهٔ واقعی نیستند — قبلاً پول بی‌صدا به سازندهٔ تاپیک یا یک حسابِ بی‌صاحب می‌رفت
    _r = msg.reply_to_message
    if (msg.sender_chat or not _r or not _r.from_user or _r.forum_topic_created
            or _r.is_automatic_forward or _r.sender_chat or _r.from_user.id == 777000):
        await msg.reply("❌ برایِ انتقالِ موجودی، رویِ پیامِ همون کاربر ریپلای کن و بنویس «انتقال [مبلغ]».")
        return
    target = _r.from_user
    if target.is_bot:
        await msg.reply("❌ نمی‌شه به یه ربات موجودی انتقال داد.")
        return
    if target.id == msg.from_user.id:
        await msg.reply("❌ نمی‌شه به خودت انتقال بدی.")
        return

    # === PATCH_GROUP_SETTINGS_V1 ===
    # کارمزدِ اختیاری — کاملِ مبلغ از فرستنده کم می‌شه، ولی فقط
    # (مبلغ - کارمزد) به گیرنده می‌رسه؛ خودِ کارمزد به هیچ کیف‌پولی
    # واریز نمی‌شه (فقط برایِ حسابرسی به‌عنوانِ یه تراکنشِ جدا ثبت می‌شه).
    fee_enabled = (await _gset(row.id, "group_transfer_fee_enabled", "0")) == "1"
    fee_pct = float(await _gset(row.id, "group_transfer_fee_pct", "0") or 0) if fee_enabled else 0.0
    fee_amount = int(round(amount * fee_pct / 100)) if fee_pct > 0 else 0
    # === PATCH_GROUP_FEE_PAYER_V1 === «از فرستنده»: گیرنده کلِ مبلغ را می‌گیرد و کارمزد
    # جدا از حسابِ فرستنده کم می‌شود؛ «از مبلغ» (پیش‌فرض، رفتارِ قبلی): از مبلغ کم می‌شود.
    fee_by_sender = fee_amount > 0 and (await _gset(row.id, "group_transfer_fee_payer", "amount")) == "sender"
    receive_amount = amount if fee_by_sender else amount - fee_amount
    debit = amount + fee_amount if fee_by_sender else amount

    async with _get_lock(row.id, msg.from_user.id):
        sender = await get_or_create_user(row.id, msg.from_user.id, msg.from_user.full_name or "",
                                          msg.from_user.username or "")
        receiver = await get_or_create_user(row.id, target.id, target.full_name or "",
                                            target.username or "")
        async with Session() as s:
            su = await s.get(BotUser, sender.id, with_for_update=True)
            if not su or float(su.balance) < debit:
                bal = float(su.balance) if su else 0
                _need = (f"\nلازم: {_rial(debit)} (مبلغ + کارمزد)" if fee_by_sender else "")
                await msg.reply(f"❌ موجودیِ کافی نداری.\nموجودیِ تو: {_rial(bal)}{_need}")
                return
            ru = await s.get(BotUser, receiver.id, with_for_update=True)
            if not ru:
                await msg.reply("❌ گیرنده پیدا نشد.")
                return
            su.balance = float(su.balance) - debit
            ru.balance = float(ru.balance) + receive_amount
            new_receiver_bal = float(ru.balance)
            note = f"انتقالِ موجودی در گروه؛ به {target.id}"
            if fee_by_sender:
                note += f" (+ کارمزد {fee_amount:,})"
            s.add(WalletTxn(bot_id=row.id, user_id=su.id, amount=-debit,
                            kind="group_transfer_out", note=note[:200]))
            s.add(WalletTxn(bot_id=row.id, user_id=ru.id, amount=receive_amount,
                            kind="group_transfer_in", note=f"انتقالِ موجودی در گروه؛ از {msg.from_user.id}"[:200]))
            if fee_amount > 0:
                s.add(WalletTxn(bot_id=row.id, user_id=su.id, amount=0,
                                kind="group_transfer_fee", note=f"کارمزدِ انتقال ({fee_pct:g}٪): {fee_amount:,} تومان"[:200]))
            new_sender_bal = float(su.balance)
            await s.commit()

    try:
        from tenant_group_guard import tenant_group_log
        await tenant_group_log(bot, row, msg.chat.id, "wallet_transfer", actor=msg.from_user.id,
                               target=target.id,
                               details=f"مبلغ {_rial(amount)}؛ کارمزد {_rial(fee_amount)}")
    except Exception:
        logger.exception("ثبت لاگ انتقال گروهی ناموفق بود")
    result_text = _transfer_result_text(
        msg.from_user, target, amount, fee_amount, fee_pct, receive_amount,
        new_sender_bal, new_receiver_bal, fee_by_sender=fee_by_sender)
    await msg.reply(
        result_text,
        reply_markup=_transfer_result_keyboard(amount, new_sender_bal, new_receiver_bal),
        parse_mode="HTML")


@group_wallet_router.callback_query(F.data == "group_transfer:info")
async def group_transfer_info(cb: CallbackQuery):
    # دکمه‌ها اطلاعاتی هستند و فقط برای بستن حالت انتظار تلگرام پاسخ می‌دهند.
    await cb.answer("اطلاعات انتقال در متن و دکمه‌ها نمایش داده شده است.")


# ═══════════════════ ⚙️ تنظیماتِ گروه (صفحه‌ی مدیریتی) ═══════════════════
# === PATCH_GROUP_SETTINGS_V1 ===

class GroupSettingsFSM(StatesGroup):
    fee_pct = State()
    min_amount = State()
    max_amount = State()


async def _group_settings_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant.bots.virtual_service.smm_ai import _get_setting as _ai_get
    ai_enabled = (await _ai_get(bot_id, "smm_ai_enabled", "0")) == "1"
    ai_group_name = (await _ai_get(bot_id, "smm_ai_group_name", "")).strip()
    ai_group_title = (await _ai_get(bot_id, "smm_ai_group_chat_title", "")).strip()
    ai_group_id = (await _ai_get(bot_id, "smm_ai_group_chat_id", "")).strip()

    # === PATCH_GROUP_SETTINGS_SAFE_NUMBERS_V1 ===
    # این سه مقدار رشته‌ان و از ورودیِ مدیر می‌آیند؛ اگر یکی‌شان خالی یا
    # با جداکنندهٔ هزارگان ذخیره شده بود، int()/float()ِ خام اینجا استثنا
    # می‌داد، هندلر وسطِ کار می‌مُرد و callback هم بی‌جواب می‌ماند — یعنی
    # از بیرون دقیقاً شبیهِ «دکمه کار نمی‌کند» دیده می‌شد.
    def _num(raw, default=0.0) -> float:
        try:
            return float(str(raw).replace(",", "").replace("،", "").strip())
        except (TypeError, ValueError):
            return default

    fee_enabled = (await _gset(bot_id, "group_transfer_fee_enabled", "0")) == "1"
    fee_pct = _num(await _gset(bot_id, "group_transfer_fee_pct", "0"))
    min_amt = _num(await _gset(bot_id, "group_transfer_min", str(_MIN_TRANSFER_DEFAULT)),
                   float(_MIN_TRANSFER_DEFAULT))
    max_amt = _num(await _gset(bot_id, "group_transfer_max", "0"))
    max_txt = f"{int(max_amt):,} تومان" if max_amt > 0 else "نامحدود"

    ai_line = (f"✅ فعال — اسم: «{ai_group_name or '—'}» — گروه: {('✅ «' + ai_group_title + '»') if ai_group_id else '⚠️ وصل نشده'}"
              if ai_enabled else "❌ غیرفعال (از «🤖 دستیارِ هوشِ مصنوعیِ مشتری» فعالش کن)")
    fee_line = f"✅ فعال ({fee_pct:g}٪)" if (fee_enabled and fee_pct > 0) else "❌ غیرفعال"
    fee_by_sender = (await _gset(bot_id, "group_transfer_fee_payer", "amount")) == "sender"   # PATCH_GROUP_FEE_PAYER_V1
    if fee_enabled and fee_pct > 0:
        fee_line += (" — از حسابِ فرستنده (گیرنده کلِ مبلغ را می‌گیرد)" if fee_by_sender
                     else " — از خودِ مبلغ (گیرنده مبلغ منهایِ کارمزد را می‌گیرد)")

    text = (
        "⚙️ <b>تنظیماتِ گروه</b>\n\n"
        "همه‌ی چیزهایی که این ربات تویِ گروه‌ها انجام می‌ده رو از همین‌جا مدیریت کن.\n\n"
        f"🤖 دستیارِ هوشِ مصنوعیِ گروه: {ai_line}\n"
        f"💸 کارمزدِ انتقالِ موجودی: {fee_line}\n"
        f"🔢 حداقلِ انتقال: {int(min_amt):,} تومان\n"
        f"🔢 حداکثرِ انتقال: {max_txt}\n\n"
        "📚 برای فعال‌سازی در گروه، ابتدا تنظیمات هوش مصنوعی یا محافظ گروه را باز کنید و راهنمای مرحله‌به‌مرحله همان بخش را بخوانید."
    )
    rows = [
        [InlineKeyboardButton(text="🤖 راه‌اندازی و تنظیمات هوش مصنوعی",
                              callback_data="smmai:menu:group")],
        [InlineKeyboardButton(text=("✏️ تغییرِ اسمِ دستیار برایِ گروه" if ai_group_name else "✏️ تعیینِ اسمِ دستیار برایِ گروه"),
                              callback_data="smmai:setgroupname")],
    ]
    async with Session() as s:
        from models import Bot as _BotRow
        _brow = await s.get(_BotRow, bot_id)
    _uname = (_brow.username if _brow else "") or ""
    if not ai_group_id and _uname:
        rows.append([InlineKeyboardButton(text="🔗 اتصال هوش مصنوعی به گروه",
                                          url=f"https://t.me/{_uname}?startgroup=connectai")])
    if ai_group_id:
        rows.append([InlineKeyboardButton(text="🔌 قطعِ اتصالِ دستیار از گروه", callback_data="smmai:disconnectgroup",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🛡 محافظ گروه، دستورات و گزارش‌ها", callback_data="tgg:menu")])
    rows.append([InlineKeyboardButton(text="🎲 تنظیمات بازی", callback_data="gset:betmenu")])
    rows.append([InlineKeyboardButton(
        text=("💸 غیرفعال‌کردنِ کارمزدِ انتقال" if fee_enabled else "💸 فعال‌کردنِ کارمزدِ انتقال"),
        callback_data="gset:feetoggle")])
    # === PATCH_GROUP_FEE_PCT_BUTTON_ALWAYS_VISIBLE_V1 ===
    # باگِ گزارش‌شده: این دکمه فقط وقتی fee_enabled=True نشون داده می‌شد،
    # ولی fee_enabled فقط وقتی می‌شه True بشه (gset:feetoggle) که از قبل
    # درصدِ کارمزد > 0 باشه — یعنی تنها راهِ رسیدن به همین دکمه، پشتِ یه
    # شرطی قفل شده بود که خودش وابسته به همین دکمه‌ست. نتیجه: ادمین هرگز
    # نمی‌تونست از منو درصد رو تنظیم کنه (همون چیزی که تو اسکرین‌شات دیده
    # می‌شد: پیامِ «اول درصد رو تنظیم کن» ولی هیچ دکمه‌ای برایِ تنظیمش نبود).
    rows.append([InlineKeyboardButton(text=f"✏️ درصدِ کارمزد ({fee_pct:g}٪)", callback_data="gset:feepct")])
    rows.append([InlineKeyboardButton(   # PATCH_GROUP_FEE_PAYER_V1
        text=("👤 کارمزد از: حسابِ فرستنده (زدن = از خودِ مبلغ)" if fee_by_sender
              else "👤 کارمزد از: خودِ مبلغ (زدن = از حسابِ فرستنده)"),
        callback_data="gset:feepayer")])
    rows.append([
        InlineKeyboardButton(text="🔢 حداقلِ انتقال", callback_data="gset:minamt"),
        InlineKeyboardButton(text="🔢 حداکثرِ انتقال", callback_data="gset:maxamt"),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@group_wallet_router.callback_query(F.data == "tadm:groupsettings")
async def tadm_groupsettings_show(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    text, kb = await _group_settings_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@group_wallet_router.callback_query(F.data == "gset:feetoggle")
async def gset_fee_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    cur = (await _gset(row.id, "group_transfer_fee_enabled", "0")) == "1"
    if not cur:
        pct = await _gset(row.id, "group_transfer_fee_pct", "0")
        if not pct or float(pct) <= 0:
            await cb.answer("اول از «✏️ درصدِ کارمزد» یه عدد بزرگ‌تر از صفر تنظیم کن.", show_alert=True)
            return
    await _gset_set(row.id, "group_transfer_fee_enabled", "0" if cur else "1")
    await cb.answer("✅ ذخیره شد")
    text, kb = await _group_settings_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_GROUP_FEE_PAYER_V1 ===
@group_wallet_router.callback_query(F.data == "gset:feepayer")
async def gset_fee_payer_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    cur = (await _gset(row.id, "group_transfer_fee_payer", "amount")) == "sender"
    await _gset_set(row.id, "group_transfer_fee_payer", "amount" if cur else "sender")
    await cb.answer("کارمزد از خودِ مبلغ کم می‌شود" if cur else "کارمزد از حسابِ فرستنده کم می‌شود",
                    show_alert=True)
    text, kb = await _group_settings_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@group_wallet_router.callback_query(F.data == "gset:feepct")
async def gset_feepct_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(GroupSettingsFSM.fee_pct)
    await cb.message.answer(
        "💸 چند درصد از هر انتقال به‌عنوانِ کارمزد کم بشه؟ (فقط عدد، مثلاً 10)\n\n"
        "مثال: یکی 100,000 تومان منتقل کنه و کارمزد 10٪ باشه، از فرستنده 100,000 "
        "کم می‌شه ولی گیرنده فقط 90,000 دریافت می‌کنه.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@group_wallet_router.message(GroupSettingsFSM.fee_pct, F.text)
async def gset_feepct_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    await state.clear()
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        text, kb = await _group_settings_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    try:
        pct = float(raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
    except ValueError:
        await msg.answer("❌ فقط عدد بفرست.")
        return
    if pct < 0 or pct > 100:
        await msg.answer("❌ درصد باید بینِ ۰ تا ۱۰۰ باشه.")
        return
    await _gset_set(row.id, "group_transfer_fee_pct", str(pct))
    if pct > 0:
        await _gset_set(row.id, "group_transfer_fee_enabled", "1")
    text, kb = await _group_settings_content(row.id)
    await msg.answer(f"✅ درصدِ کارمزد {pct:g}٪ شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@group_wallet_router.callback_query(F.data == "gset:minamt")
async def gset_minamt_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(GroupSettingsFSM.min_amount)
    await cb.message.answer("🔢 حداقلِ مبلغِ قابلِ‌انتقال رو بفرست (فقط عدد، تومان):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@group_wallet_router.message(GroupSettingsFSM.min_amount, F.text)
async def gset_minamt_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    await state.clear()
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        text, kb = await _group_settings_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    raw_digits = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("،", "")
    if not raw_digits.isdigit() or int(raw_digits) < 1:
        await msg.answer("❌ فقط عددِ مثبت بفرست.")
        return
    await _gset_set(row.id, "group_transfer_min", raw_digits)
    text, kb = await _group_settings_content(row.id)
    await msg.answer(f"✅ حداقلِ انتقال {int(raw_digits):,} تومان شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


@group_wallet_router.callback_query(F.data == "gset:maxamt")
async def gset_maxamt_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(GroupSettingsFSM.max_amount)
    await cb.message.answer("🔢 حداکثرِ مبلغِ قابلِ‌انتقال رو بفرست (فقط عدد، تومان؛ برایِ نامحدود عددِ 0 بفرست):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@group_wallet_router.message(GroupSettingsFSM.max_amount, F.text)
async def gset_maxamt_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    await state.clear()
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        text, kb = await _group_settings_content(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    raw_digits = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).replace(",", "").replace("،", "")
    if not raw_digits.isdigit():
        await msg.answer("❌ فقط عدد بفرست (یا 0 برایِ نامحدود).")
        return
    await _gset_set(row.id, "group_transfer_max", raw_digits)
    text, kb = await _group_settings_content(row.id)
    max_txt = f"{int(raw_digits):,} تومان" if raw_digits != "0" else "نامحدود"
    await msg.answer(f"✅ حداکثرِ انتقال {max_txt} شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
