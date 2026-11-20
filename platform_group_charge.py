"""شارژ/کسر کیف پول از داخل گروه بات‌ساز (PATCH_GROUP_ADMIN_CHARGE_V1، درخواست مدیر).

مدیر روی پیام یک نفر ریپلای می‌کند و می‌نویسد «50000 شارژ» یا «شارژ 50000» (یا «کسر 20000»)؛ یا بدون ریپلای:
«شارژ 50000 @username» / «شارژ 50000 123456789». کیف پول بات‌ساز همان شخص تغییر می‌کند.

چه کسی مجاز است (عمداً سخت‌گیرانه، چون پول ساخته می‌شود):
  • خود مدیر اصلی با اکانت شخصی (PLATFORM_ADMIN_ID)،
  • یا پیامی که «به اسم کانال» فرستاده شده و آن کانال در پنل (🛡 محافظ گروه ← 💳 شارژ از گروه) مجاز شده باشد.
ادمین ناشناس گروه (sender_chat = خود گروه) مجاز نیست، چون معلوم نیست کدام ادمین است. بقیه بی‌صدا نادیده گرفته می‌شوند؛
اگر کانالی که مجاز نیست دستور بدهد، فقط به پیوی مدیر خبر می‌رود (با آیدی‌اش برای مجازکردن).
هر شارژ دکمهٔ «↩️ برگشت» دارد (فقط مدیر اصلی، تا ۲۴ ساعت، یک‌بار).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import PlatformTxn, PlatformUser
from premium_emoji import hx_button

logger = _get_logger(__name__)
platform_group_charge_router = Router(name="platform_group_charge")

CH_KEY = "pgc_channels"
MAX_KEY = "pgc_max"
DEFAULT_MAX = 50_000_000
NOTE_PREFIX = "شارژ دستی در گروه"
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_NUM = r"([0-9][0-9,،]*)"
_CMD = re.compile(r"^(?:(شارژ|کسر)\s+" + _NUM + r"|" + _NUM + r"\s+(شارژ|کسر))(?:\s+(@[A-Za-z0-9_]{4,32}|[0-9]{5,15}))?$")


def parse(text: str | None):
    """⇒ (sign, amount, target_ref) یا None. sign: +1 شارژ، -1 کسر."""
    t = " ".join((text or "").translate(_FA).split())
    m = _CMD.match(t)
    if not m:
        return None
    word = m.group(1) or m.group(4)
    num = (m.group(2) or m.group(3)).replace(",", "").replace("،", "")
    if not num.isdigit() or int(num) <= 0:
        return None
    return (1 if word == "شارژ" else -1), int(num), m.group(5)


async def _gp(k: str, d: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(k, d)


async def _sp(k: str, v: str) -> None:
    from platform_bot import set_pset
    await set_pset(k, v)


async def allowed_channels() -> list[str]:
    return [x for x in (await _gp(CH_KEY, "")).replace(" ", "").split(",") if x]


async def can_charge(msg: Message) -> bool:
    sc = getattr(msg, "sender_chat", None)
    if sc is not None:
        if sc.id == msg.chat.id:                       # ادمین ناشناس: نامعلوم کدام ادمین
            return False
        return str(sc.id) in await allowed_channels()
    return bool(msg.from_user and msg.from_user.id == PLATFORM_ADMIN_ID)


def _looks(m: Message) -> bool:
    return parse(m.text) is not None


async def _resolve_target(msg: Message, ref: str | None):
    """⇒ (telegram_id, نام) یا None."""
    if ref:
        async with Session() as s:
            if ref.startswith("@"):
                u = await s.scalar(select(PlatformUser).where(func.lower(PlatformUser.username) == ref[1:].lower()))
            else:
                u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == int(ref)))
        return (u.telegram_id, u.full_name or (f"@{u.username}" if u.username else str(u.telegram_id))) if u else None
    from platform_group_money import _reply_target
    t = _reply_target(msg)
    if not t or t.is_bot:
        return None
    return t.id, (t.full_name or (f"@{t.username}" if t.username else str(t.id)))


def _rial(n) -> str:
    return f"{int(n):,} تومان"


@platform_group_charge_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_looks))
async def group_charge(msg: Message, bot: Bot):
    from aiogram.dispatcher.event.bases import SkipHandler
    if not await can_charge(msg):
        sc = getattr(msg, "sender_chat", None)
        if sc is not None and sc.id != msg.chat.id:
            try:
                await bot.send_message(PLATFORM_ADMIN_ID,
                                       f"⚠️ دستور «{(msg.text or '')[:40]}» از کانال «{sc.title}» (<code>{sc.id}</code>) در گروه "
                                       f"«{msg.chat.title}» اجرا نشد چون این کانال برای شارژ مجاز نیست.\n"
                                       "برای مجازکردن: پنل ← محافظ گروه ← 💳 شارژ از گروه ← افزودن کانال.", parse_mode="HTML")
            except Exception:  # noqa: BLE001
                logger.info("group charge: admin notice skipped")
        raise SkipHandler
    sign, amount, ref = parse(msg.text)
    cap = int((await _gp(MAX_KEY, str(DEFAULT_MAX))) or DEFAULT_MAX)
    if amount > cap:
        await msg.reply(f"❌ بیشتر از سقف مجاز ({_rial(cap)}). سقف را از پنل ← محافظ گروه ← 💳 شارژ از گروه عوض کن.")
        return
    target = await _resolve_target(msg, ref)
    if target is None:
        await msg.reply("❌ روی پیام همان شخص ریپلای کن و بنویس «50000 شارژ»؛ یا «شارژ 50000 @username».\n"
                        "(اگر با یوزرنیم/آیدی زدی، آن شخص باید قبلا ربات بات‌ساز را استارت کرده باشد.)")
        return
    uid, name = target
    who = msg.sender_chat.title if getattr(msg, "sender_chat", None) else "مدیر"
    async with Session() as s:
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid).with_for_update())
        if u is None:
            u = PlatformUser(telegram_id=uid, full_name=name, balance=0)
            s.add(u)
            await s.flush()
        if sign < 0 and float(u.balance or 0) < amount:
            have = float(u.balance or 0)
            await s.rollback()
            await msg.reply(f"❌ موجودی {name} فقط {_rial(have)} است.")
            return
        u.balance = float(u.balance or 0) + sign * amount
        txn = PlatformTxn(user_telegram_id=uid, amount=sign * amount, kind="admin",
                          note=f"{NOTE_PREFIX} ({'شارژ' if sign > 0 else 'کسر'}، {who})"[:200])
        s.add(txn)
        await s.commit()
        new_bal, txn_id = float(u.balance), txn.id
    verb = "شارژ" if sign > 0 else "کسر"
    kb = InlineKeyboardMarkup(inline_keyboard=[[hx_button("↩️ برگشت", callback_data=f"pgc:undo:{txn_id}",
                                                          style=ButtonStyle.DANGER)]])
    await msg.reply(f"✅ کیف پول {name} {_rial(amount)} {verb} شد.\n💰 موجودی جدید: {_rial(new_bal)}", reply_markup=kb)
    try:
        await bot.send_message(uid, f"💳 کیف پول بات‌ساز شما {_rial(amount)} {verb} شد.\n💰 موجودی: {_rial(new_bal)}")
    except Exception:  # noqa: BLE001
        logger.info("group charge: user %s has not started the bot", uid)


@platform_group_charge_router.callback_query(F.data.startswith("pgc:undo:"))
async def undo(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    tid = int(cb.data.split(":")[2])
    mark = f"REVOKE#{tid}"
    async with Session() as s:
        t = await s.get(PlatformTxn, tid)
        if t is None or not str(t.note or "").startswith(NOTE_PREFIX):
            await cb.answer("تراکنش پیدا نشد", show_alert=True)
            return
        if await s.scalar(select(PlatformTxn.id).where(PlatformTxn.note == mark)):
            await cb.answer("قبلا برگشت خورده است", show_alert=True)
            return
        if t.created_at and datetime.utcnow() - t.created_at > timedelta(hours=24):
            await cb.answer("بیش از ۲۴ ساعت گذشته؛ از پنل اصلاح کن.", show_alert=True)
            return
        u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == t.user_telegram_id).with_for_update())
        amt = float(t.amount)
        u.balance = float(u.balance or 0) - amt
        s.add(PlatformTxn(user_telegram_id=t.user_telegram_id, amount=-amt, kind="admin", note=mark))
        await s.commit()
        new_bal = float(u.balance)
    try:
        await cb.message.edit_text((cb.message.text or "") + f"\n\n↩️ برگشت خورد. موجودی: {_rial(new_bal)}")
    except Exception:  # noqa: BLE001
        logger.info("group charge undo: edit skipped")
    await cb.answer("برگشت خورد")


# ───────────────────────── تنظیمات (پنل ← محافظ گروه ← 💳 شارژ از گروه) ─────────────────────────
class PgcFSM(StatesGroup):
    add = State()
    cap = State()


async def menu_content():
    chs = await allowed_channels()
    cap = int((await _gp(MAX_KEY, str(DEFAULT_MAX))) or DEFAULT_MAX)
    text = ("💳 <b>شارژ کیف پول از داخل گروه</b>\n\n"
            "روی پیام یک نفر ریپلای کن و بنویس <code>50000 شارژ</code> یا <code>شارژ 50000</code> (کم‌کردن: <code>کسر 20000</code>).\n"
            "بدون ریپلای: <code>شارژ 50000 @username</code> یا <code>شارژ 50000 123456789</code>.\n"
            "زیر هر شارژ دکمهٔ «↩️ برگشت» هست (فقط تو، تا 24 ساعت).\n\n"
            "مجاز: اکانت شخصی تو، و کانال‌های زیر وقتی «به اسم کانال» در گروه می‌نویسی. ادمین ناشناس گروه مجاز نیست.\n\n"
            f"📢 کانال‌های مجاز: {', '.join(f'<code>{c}</code>' for c in chs) if chs else 'هیچ'}\n"
            f"🔢 سقف هر دستور: {_rial(cap)}")
    rows = [[hx_button("➕ افزودن کانال (آیدی یا فوروارد پست)", callback_data="pgc:add", style=ButtonStyle.SUCCESS)]]
    for c in chs:
        rows.append([hx_button(f"🗑 حذف {c}", callback_data=f"pgc:del:{c}", style=ButtonStyle.DANGER)])
    rows += [[hx_button(f"🔢 سقف هر دستور ({_rial(cap)})", callback_data="pgc:cap")],
             [hx_button("⬅️ بازگشت به محافظ گروه", callback_data="adm:group_guard", style=ButtonStyle.DANGER)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_group_charge_router.callback_query(F.data.startswith("pgc:") & ~F.data.startswith("pgc:undo:"))
async def pgc_cb(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if cb.data == "pgc:add":
        await state.set_state(PgcFSM.add)
        await cb.message.answer("آیدی عددی کانال (مثل -1001234567890) را بفرست، یا یک پست از همان کانال را اینجا فوروارد کن.\n"
                                "ربات لازم نیست عضو کانال باشد. انصراف: /cancel")
        await cb.answer()
        return
    if cb.data == "pgc:cap":
        await state.set_state(PgcFSM.cap)
        await cb.message.answer("سقف مبلغ هر دستور شارژ را به تومان بفرست (مثلا 50000000):")
        await cb.answer()
        return
    if cb.data.startswith("pgc:del:"):
        cid = cb.data.split(":", 2)[2]
        await _sp(CH_KEY, ",".join(c for c in await allowed_channels() if c != cid))
    text, kb = await menu_content()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_group_charge_router.message(StateFilter(PgcFSM.add, PgcFSM.cap), F.chat.type == "private")
async def pgc_input(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    cur = await state.get_state()
    raw = (msg.text or "").translate(_FA).strip()
    if raw in ("/cancel", "لغو"):
        await state.clear()
    elif cur == PgcFSM.add.state:
        fwd = getattr(getattr(msg, "forward_origin", None), "chat", None) or getattr(msg, "forward_from_chat", None)
        cid = str(fwd.id) if fwd is not None else raw
        if not re.fullmatch(r"-100[0-9]{6,14}", cid):
            await msg.answer("❌ آیدی کانال باید با -100 شروع شود، یا یک پست کانال را فوروارد کن.")
            return
        chs = await allowed_channels()
        if cid not in chs:
            await _sp(CH_KEY, ",".join(chs + [cid]))
        await state.clear()
        await msg.answer(f"✅ کانال <code>{cid}</code> مجاز شد.", parse_mode="HTML")
    else:
        v = raw.replace(",", "")
        if not v.isdigit() or int(v) <= 0:
            await msg.answer("❌ یک عدد معتبر بفرست.")
            return
        await _sp(MAX_KEY, str(int(v)))
        await state.clear()
    text, kb = await menu_content()
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")
