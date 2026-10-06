"""
tenant/partner_customers.py — «مشتریان و ربات‌هایشان» برایِ رباتِ همکار.

=== PATCH_PARTNER_CUSTOMERS_V1 ===
همکار تا حالا فقط `tadm:users` را داشت که با *جستجویِ آیدی* کار می‌کرد و
دربارهٔ ربات‌هایی که فروخته چیزی نمی‌دانست. این‌جا لایه‌ای اضافه می‌شود که:

    ۱) فهرستِ کسانی که از او ربات خریده‌اند را اینلاین نشان می‌دهد
    ۲) با زدن رویِ هر نفر، پروفایل + خریدهایش + ربات‌هایش می‌آید
    ۳) با زدن رویِ هر ربات، همکار می‌تواند مدیریتش کند:
       افزایش/کاهشِ روزِ اعتبار، توقف/فعال‌سازی، تعویضِ توکن، انتقالِ مالکیت

آنچه عمداً از نو نوشته نشده: شارژ/کسرِ کیف پول، بن/آن‌بن و ارسالِ پیام —
این‌ها از قبل در tenant/__init__.py (هندلرهایِ `usr:*`) وجود دارند و پلنِ
partner هم Feature.USER_LOOKUP را دارد، پس فقط به همان‌ها لینک می‌دهیم.
دوباره‌نویسی‌شان یعنی دو نسخهٔ واگرا از منطقِ پول و بن.

مرزِ امنیتی: همکار فقط به ربات‌هایی دسترسی دارد که *خودش فروخته*؛ هر
اکشن با _owned_sale دوباره بررسی می‌شود، نه فقط در لحظهٔ نمایشِ فهرست.
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, func

from config import Session
from models import (
    Bot as BotRow, BotUser, PartnerBot, PartnerSale, WalletTxn,
)
from partner_rules import jalali

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_customers_router = Router()

PAGE = 8


class PartnerCustFSM(StatesGroup):
    days = State()
    token = State()
    transfer = State()


async def _partner_ok(cb, bot: Bot):
    """همان بررسیِ partner_menu: این ربات، رباتِ همکارِ فعالِ همین کاربر است؟"""
    from tenant.partner_bot.partner_menu import _partner_ok as _ok
    return await _ok(cb, bot)


async def _owned_sale(pb_id: int, sold_bot_id: int) -> PartnerSale | None:
    """این ربات واقعاً توسطِ همین همکار فروخته شده؟ (مرزِ امنیتیِ همهٔ اکشن‌ها)"""
    async with Session() as s:
        return await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb_id,
            PartnerSale.sold_bot_id == sold_bot_id))



# === PATCH_OWNER_APPROVAL_SWAP_V1 ===
# درخواستِ ادمین: همکار (و ادمین‌هایش) فقط «با تاییدِ صاحبِ اصلیِ ربات» — همان کسی که ربات را
# خریده — می‌توانند توکن را عوض کنند یا مالکیت را منتقل کنند. صاحب در همین ربات یک پیامِ
# «✅ تایید / ❌ رد» می‌گیرد؛ بعد از تایید (یک‌بار مصرف، ۶۰ دقیقه اعتبار) همکار می‌تواند ادامه دهد.
import json as _json
import time as _time

_APPROVAL_PENDING_SEC = 30 * 60
_APPROVAL_VALID_SEC = 60 * 60
_ACT_LABEL = {"tok": "تعویض توکن", "tr": "انتقال مالکیت"}


async def _pb_row(pb_id: int):
    async with Session() as s:
        return await s.get(PartnerBot, pb_id)


async def _owned_bot_row(bot_id: int):
    async with Session() as s:
        return await s.get(BotRow, bot_id)


async def _grant_get(bot_id: int, act: str) -> dict | None:
    from tenant import get_setting
    raw = await get_setting(bot_id, f"pcu_approval:{act}", "")
    try:
        d = _json.loads(raw) if raw else None
    except Exception:
        d = None
    return d if isinstance(d, dict) else None


async def _grant_set(bot_id: int, act: str, d: dict | None) -> None:
    from tenant import set_setting
    await set_setting(bot_id, f"pcu_approval:{act}", _json.dumps(d) if d else "")


async def _grant_ok(bot_id: int, act: str) -> bool:
    d = await _grant_get(bot_id, act)
    return bool(d and d.get("status") == "approved"
                and _time.time() - float(d.get("at", 0)) < _APPROVAL_VALID_SEC)


async def _needs_owner_approval(pb: PartnerBot, br: BotRow) -> bool:
    """اگر صاحبِ ربات خودِ همکار باشد، تاییدِ جدا لازم نیست."""
    return int(br.owner_id) != int(pb.owner_id)


async def _request_owner_approval(cb: CallbackQuery, bot: Bot, pb: PartnerBot,
                                  br: BotRow, act: str) -> None:
    d = await _grant_get(br.id, act)
    now = _time.time()
    if d and d.get("status") == "pending" and now - float(d.get("at", 0)) < _APPROVAL_PENDING_SEC:
        await cb.answer("⏳ درخواست قبلاً برایِ صاحبِ ربات ارسال شده؛ منتظرِ تاییدِ او باشید.",
                        show_alert=True)
        return
    await _grant_set(br.id, act, {"status": "pending", "at": now, "by": cb.from_user.id,
                                  "chat": cb.message.chat.id, "pb": pb.id})
    label = _ACT_LABEL[act]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید می‌کنم", callback_data=f"pcua:ok:{br.id}:{act}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"pcua:no:{br.id}:{act}",
                             style=ButtonStyle.DANGER)]])
    text = (f"🔐 <b>درخواستِ {label}</b>\n\n"
            f"پشتیبانِ ربات از شما برایِ <b>{label}</b> رباتِ @{br.username or br.id} اجازه می‌خواهد.\n\n"
            "اگر خودتان این کار را خواسته‌اید «تایید» را بزنید؛ در غیر این صورت «رد» کنید. "
            "بدونِ تاییدِ شما هیچ تغییری انجام نمی‌شود.")
    sent = False
    try:
        await bot.send_message(br.owner_id, text, reply_markup=kb)
        sent = True
    except Exception:
        logger.info("ارسالِ درخواستِ تایید از رباتِ همکار ممکن نشد؛ تلاش با رباتِ خودِ مشتری")
    if not sent:
        try:
            from bot_utils import _temp_bot
            async with _temp_bot(br.token) as tb:
                await tb.send_message(br.owner_id, text, reply_markup=kb)
            sent = True
        except Exception:
            logger.exception("ارسالِ درخواستِ تاییدِ مالک ناموفق بود")
    if not sent:
        await _grant_set(br.id, act, None)
        await cb.answer("نتوانستم برایِ صاحبِ ربات پیام بفرستم (شاید ربات را استارت نکرده).",
                        show_alert=True)
        return
    await cb.answer("📨 درخواست برایِ صاحبِ ربات ارسال شد؛ بعد از تاییدِ او خبرتان می‌کنم.",
                    show_alert=True)


@partner_customers_router.callback_query(F.data.startswith(("pcua:ok:", "pcua:no:")))
async def pcua_decide(cb: CallbackQuery, bot: Bot):
    _, verdict, bot_id_s, act = cb.data.split(":")
    bot_id = int(bot_id_s)
    async with Session() as s:
        br = await s.get(BotRow, bot_id)
    if not br or act not in _ACT_LABEL or cb.from_user.id != int(br.owner_id):
        await cb.answer("این درخواست مالِ شما نیست.", show_alert=True)
        return
    d = await _grant_get(bot_id, act)
    if not d or d.get("status") != "pending":
        await cb.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("حذفِ دکمه‌هایِ درخواستِ منقضی ممکن نشد (بی‌اهمیت)")
        return
    label = _ACT_LABEL[act]
    ok = verdict == "ok"
    if ok:
        d.update(status="approved", at=_time.time())
        await _grant_set(bot_id, act, d)
    else:
        await _grant_set(bot_id, act, None)
    try:
        await cb.message.edit_text(
            f"{'✅ تایید شد' if ok else '❌ رد شد'} — {label} رباتِ @{br.username or bot_id}.",
            reply_markup=None)
    except Exception:
        logger.debug("ویرایشِ پیامِ درخواست ممکن نشد (بی‌اهمیت)")
    await cb.answer("ثبت شد")
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"▶️ ادامهٔ {label}" if ok else "🤖 بازگشت به ربات",
            callback_data=(f"pcu:{act}:{bot_id}" if ok else f"pcu:b:{bot_id}"),
            style=ButtonStyle.SUCCESS if ok else ButtonStyle.PRIMARY)]])
        await bot.send_message(
            d.get("chat") or d.get("by"),
            (f"✅ صاحبِ ربات @{br.username or bot_id} <b>{label}</b> را تایید کرد (۶۰ دقیقه اعتبار). "
             "برایِ ادامه دکمهٔ زیر را بزنید.") if ok else
            f"❌ صاحبِ ربات @{br.username or bot_id} درخواستِ {label} را رد کرد.",
            reply_markup=kb)
    except Exception:
        logger.exception("اطلاعِ نتیجهٔ تایید به همکار ناموفق بود")


# ═══════════════ فهرستِ مشتری‌ها ═══════════════

@partner_customers_router.callback_query(F.data.startswith("pcu:list"))
async def pcu_list(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    parts = cb.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 else 0

    async with Session() as s:
        total = await s.scalar(
            select(func.count(func.distinct(PartnerSale.buyer_telegram_id)))
            .where(PartnerSale.partner_bot_id == pb.id)) or 0
        buyers = (await s.scalars(
            select(PartnerSale.buyer_telegram_id)
            .where(PartnerSale.partner_bot_id == pb.id)
            .group_by(PartnerSale.buyer_telegram_id)
            .order_by(func.max(PartnerSale.id).desc())
            .offset(offset).limit(PAGE))).all()

        people = []
        for uid in buyers:
            n_sold = await s.scalar(
                select(func.count(PartnerSale.id))
                .where(PartnerSale.partner_bot_id == pb.id,
                       PartnerSale.buyer_telegram_id == uid,
                       PartnerSale.kind == "sold")) or 0
            n_trial = await s.scalar(
                select(func.count(PartnerSale.id))
                .where(PartnerSale.partner_bot_id == pb.id,
                       PartnerSale.buyer_telegram_id == uid,
                       PartnerSale.kind == "trial")) or 0
            bu = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == uid))
            people.append((uid, bu.full_name if bu else None,
                           bool(bu.is_banned) if bu else False, n_sold, n_trial))

    if not people:
        await cb.answer("هنوز کسی از تو ربات نخریده است.", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for uid, name, banned, n_sold, n_trial in people:
        mark = "🚫" if banned else "👤"
        who = name or str(uid)
        extra = f" · 🎁{n_trial}" if n_trial else ""
        b.button(text=f"{mark} {who} · 🤖{n_sold}{extra}",
                 callback_data=f"pcu:u:{uid}",
                 style=(ButtonStyle.DANGER if banned else ButtonStyle.SUCCESS))
    b.adjust(1)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"pcu:list:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(("بعدی ➡️", f"pcu:list:{offset + PAGE}"))
    for t, cbd in nav:
        b.button(text=t, callback_data=cbd, style=ButtonStyle.PRIMARY)
    if nav:
        b.adjust(*([1] * len(people)), len(nav))
    b.button(text="🔙 بازگشت", callback_data="pp:settings", style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        f"🧑‍💼 <b>مشتریان تو</b> ({total} نفر)\n"
        f"نمایشِ {offset + 1} تا {offset + len(people)}\n\n"
        "🤖 تعداد ربات خریده · 🎁 تست گرفته · 🚫 بن‌شده\n\n"
        "روی هر نفر بزن تا اطلاعات و ربات‌هایش بیاید.",
        reply_markup=b.as_markup())
    await cb.answer()


# ═══════════════ پروفایلِ یک مشتری ═══════════════

@partner_customers_router.callback_query(F.data.startswith("pcu:u:"))
async def pcu_user(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    row, pb = ok
    uid = int(cb.data.split(":")[2])

    async with Session() as s:
        bu = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == uid))
        sales = (await s.execute(
            select(PartnerSale, BotRow)
            .outerjoin(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == pb.id,
                   PartnerSale.buyer_telegram_id == uid)
            .order_by(PartnerSale.id.desc()))).all()
        spent = await s.scalar(
            select(func.coalesce(func.sum(PartnerSale.price), 0))
            .where(PartnerSale.partner_bot_id == pb.id,
                   PartnerSale.buyer_telegram_id == uid)) or 0

    if not sales:
        await cb.answer("این کاربر از تو خریدی نداشته.", show_alert=True)
        return

    n_sold = sum(1 for sa, _ in sales if sa.kind == "sold")
    n_trial = sum(1 for sa, _ in sales if sa.kind == "trial")
    text = (
        f"{'🚫 ' if (bu and bu.is_banned) else '👤 '}"
        f"<b>{(bu.full_name if bu else None) or '—'}</b>\n"
        f"🆔 <code>{uid}</code>"
        + (f" · @{bu.username}" if bu and bu.username else "") + "\n"
        f"👛 کیف پول: {int(bu.balance) if bu else 0:,} تومان\n"
        + ("⛔️ این کاربر <b>بن</b> است.\n" if bu and bu.is_banned else "")
        + f"\n🛒 خرید: <b>{n_sold}</b> ربات · 🎁 تست: <b>{n_trial}</b>\n"
        f"💰 مجموع پرداختی به تو: <b>{int(spent):,}</b> تومان\n\n"
        "🤖 <b>ربات‌هایش</b> — روی هرکدام بزن تا مدیریتش کنی:"
    )

    b = InlineKeyboardBuilder()
    for sa, br in sales:
        if not br:
            continue
        if br.archived_at is not None:
            mark = "🗑"
        elif br.status == "active":
            mark = "🟢"
        else:
            mark = "⏸"
        tag = "🎁" if sa.kind == "trial" else ""
        left = ""
        if br.plan_expires_at:
            d = (br.plan_expires_at - datetime.datetime.now()).days
            left = f" · {d} روز" if d >= 0 else " · منقضی"
        b.button(text=f"{mark}{tag} @{br.username or br.id}{left}",
                 callback_data=f"pcu:b:{br.id}",
                 style=(ButtonStyle.SUCCESS if br.status == "active"
                        else ButtonStyle.DANGER))
    b.adjust(1)

    # اکشن‌هایِ خودِ شخص → هندلرهایِ موجودِ tenant/__init__.py
    b.button(text="➕ شارژ کیف پول", callback_data=f"usr:add:{uid}",
             style=ButtonStyle.SUCCESS)
    b.button(text="➖ کسر کیف پول", callback_data=f"usr:sub:{uid}",
             style=ButtonStyle.DANGER)
    b.button(text="💬 ارسال پیام", callback_data=f"usr:dm:{uid}",
             style=ButtonStyle.PRIMARY)
    b.button(text=("✅ رفع بن" if (bu and bu.is_banned) else "🚫 بن کردن"),
             callback_data=f"usr:ban:{uid}",
             style=(ButtonStyle.SUCCESS if (bu and bu.is_banned) else ButtonStyle.DANGER))
    b.button(text="🧾 رسیدها و خریدها", callback_data=f"usr:receipts:{uid}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔙 فهرست مشتریان", callback_data="pcu:list:0",
             style=ButtonStyle.DANGER)
    b.adjust(*([1] * len([1 for _, br in sales if br])), 2, 1, 1, 1, 1)

    await cb.message.edit_text(text, reply_markup=b.as_markup())
    await cb.answer()


# ═══════════════ کارتِ یک رباتِ فروخته‌شده ═══════════════

async def _bot_card(pb_id: int, bot_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    sale = await _owned_sale(pb_id, bot_id)
    if not sale:
        return None
    async with Session() as s:
        br = await s.get(BotRow, bot_id)
    if not br:
        return None

    if br.archived_at is not None:
        st = "🗑 حذف‌شده"
    elif br.status == "active":
        st = "🟢 فعال"
    else:
        st = f"⏸ متوقف ({br.lock_reason or '—'})"
    left = "—"
    if br.plan_expires_at:
        d = (br.plan_expires_at - datetime.datetime.now()).days
        left = f"{d} روز" if d >= 0 else f"منقضی ({-d} روز پیش)"

    text = (
        f"🤖 <b>@{br.username or br.id}</b>\n\n"
        f"وضعیت: {st}\n"
        f"💼 پلن: {sale.plan}"
        + (" (تست)" if sale.kind == "trial" else "") + "\n"
        f"👑 مالک: <code>{br.owner_id}</code>\n"
        f"📅 اعتبار تا: {jalali(br.plan_expires_at)} ({left})\n"
        f"🗓 تاریخ فروش: {jalali(sale.created_at)}\n"
        f"💵 مبلغ: {int(sale.price or 0):,} تومان"
    )

    b = InlineKeyboardBuilder()
    b.button(text="➕ ۷ روز", callback_data=f"pcu:d:{bot_id}:7", style=ButtonStyle.SUCCESS)
    b.button(text="➕ ۳۰ روز", callback_data=f"pcu:d:{bot_id}:30", style=ButtonStyle.SUCCESS)
    b.button(text="➖ ۷ روز", callback_data=f"pcu:d:{bot_id}:-7", style=ButtonStyle.DANGER)
    b.button(text="➖ ۳۰ روز", callback_data=f"pcu:d:{bot_id}:-30", style=ButtonStyle.DANGER)
    b.button(text="⌨️ تعیین دستی روز", callback_data=f"pcu:dman:{bot_id}",
             style=ButtonStyle.PRIMARY)
    if br.status == "active":
        b.button(text="⏸ توقف ربات", callback_data=f"pcu:pause:{bot_id}",
                 style=ButtonStyle.DANGER)
    else:
        b.button(text="▶️ فعال‌سازی ربات", callback_data=f"pcu:resume:{bot_id}",
                 style=ButtonStyle.SUCCESS)
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 ===
    # رباتِ تست (kind="trial") و پلنِ رایگان: تا وقتی خریداری نشده، نه همکار
    # و نه خودِ کاربر نمی‌توانند توکن یا مالکیت را عوض کنند — فقط مدیرِ
    # بات‌ساز از پنلِ ادمین. وگرنه یک تستِ رایگان عملاً به رباتِ دائمی تبدیل
    # می‌شد.
    from bot_utils import owner_swap_lock_reason
    async with Session() as _s:
        _swap_locked = await owner_swap_lock_reason(_s, br)
    if not _swap_locked:
        b.button(text="🔑 تعویض توکن", callback_data=f"pcu:tok:{bot_id}",
                 style=ButtonStyle.PRIMARY)
        b.button(text="👑 انتقال مالکیت", callback_data=f"pcu:tr:{bot_id}",
                 style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت به کاربر", callback_data=f"pcu:u:{sale.buyer_telegram_id}",
             style=ButtonStyle.DANGER)
    if _swap_locked:
        b.adjust(2, 2, 1, 1, 1)
    else:
        b.adjust(2, 2, 1, 1, 1, 1, 1)
    return text, b.as_markup()


async def _show_bot_card(cb: CallbackQuery, pb_id: int, bot_id: int,
                         note: str = "") -> None:
    card = await _bot_card(pb_id, bot_id)
    if not card:
        await cb.answer("این ربات در فهرستِ فروش‌هایِ تو نیست.", show_alert=True)
        return
    text, kb = card
    try:
        await cb.message.edit_text((note + text) if note else text, reply_markup=kb)
    except Exception:
        await cb.message.answer((note + text) if note else text, reply_markup=kb)


@partner_customers_router.callback_query(F.data.startswith("pcu:b:"))
async def pcu_bot(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    await _show_bot_card(cb, pb.id, int(cb.data.split(":")[2]))
    await cb.answer()


# ── افزایش/کاهشِ روز ──

async def _shift_days(bot_id: int, delta: int) -> int:
    """روزِ اعتبار را جابه‌جا می‌کند و روزِ باقی‌ماندهٔ جدید را برمی‌گرداند.

    مبنا عمداً «هرکدام دیرتر است» می‌گیرد: اگر اعتبار قبلاً تمام شده،
    افزودنِ ۳۰ روز باید یعنی ۳۰ روز از *امروز*، نه ۳۰ روز از تاریخِ
    گذشته (وگرنه ادمین چند بار می‌زند و هیچ اتفاقی نمی‌افتد).
    """
    now = datetime.datetime.now()
    async with Session() as s:
        br = await s.get(BotRow, bot_id)
        base = br.plan_expires_at if (br.plan_expires_at and br.plan_expires_at > now) else now
        br.plan_expires_at = base + datetime.timedelta(days=delta)
        if br.plan_expires_at < now:
            br.plan_expires_at = now
        left = (br.plan_expires_at - now).days
        await s.commit()
    return left


@partner_customers_router.callback_query(F.data.startswith("pcu:d:"))
async def pcu_days(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    _, _, raw_id, raw_delta = cb.data.split(":")
    bot_id, delta = int(raw_id), int(raw_delta)
    if not await _owned_sale(pb.id, bot_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    left = await _shift_days(bot_id, delta)
    await cb.answer(f"✅ اعتبار {'+' if delta > 0 else ''}{delta} روز شد — "
                    f"الان {left} روز")
    await _show_bot_card(cb, pb.id, bot_id)


@partner_customers_router.callback_query(F.data.startswith("pcu:dman:"))
async def pcu_days_manual(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owned_sale(pb.id, bot_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(PartnerCustFSM.days)
    await state.update_data(pcu_bot=bot_id, pcu_pb=pb.id)
    await cb.message.answer(
        "⌨️ عدد روز را بفرست:\n"
        "• <code>30</code> یا <code>+30</code> → ۳۰ روز اضافه کن\n"
        "• <code>-15</code> → ۱۵ روز کم کن\n"
        "• <code>=60</code> → اعتبار را روی <b>۶۰ روز از امروز</b> بگذار",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 انصراف", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.DANGER)]]))
    await cb.answer()


@partner_customers_router.message(PartnerCustFSM.days, F.text)
async def pcu_days_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id, pb_id = data.get("pcu_bot"), data.get("pcu_pb")
    raw = (msg.text or "").strip().replace("٫", "").replace("،", "")
    for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        raw = raw.replace(fa, en)
    await state.clear()
    if not bot_id or not await _owned_sale(pb_id, bot_id):
        await msg.answer("این مرحله منقضی شده.")
        return
    try:
        if raw.startswith("="):
            n = int(raw[1:])
            async with Session() as s:
                br = await s.get(BotRow, bot_id)
                br.plan_expires_at = datetime.datetime.now() + datetime.timedelta(days=n)
                await s.commit()
            left = n
        else:
            left = await _shift_days(bot_id, int(raw))
    except ValueError:
        await msg.answer("عدد معتبر نیست. مثلاً <code>30</code> یا <code>-15</code> یا <code>=60</code>.")
        return
    await msg.answer(
        f"✅ اعتبار ربات تنظیم شد — الان <b>{left}</b> روز باقی مانده.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🤖 بازگشت به ربات", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.PRIMARY)]]))


# ── توقف / فعال‌سازی ──

@partner_customers_router.callback_query(F.data.startswith(("pcu:pause:", "pcu:resume:")))
async def pcu_toggle(cb: CallbackQuery, bot: Bot):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    parts = cb.data.split(":")
    action, bot_id = parts[1], int(parts[2])
    if not await _owned_sale(pb.id, bot_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        br = await s.get(BotRow, bot_id)
        if action == "pause":
            br.status = "paused"
            br.lock_reason = "توقف توسط فروشنده"
        else:
            br.status = "active"
            br.lock_reason = None
        await s.commit()
    await cb.answer("⏸ ربات متوقف شد." if action == "pause" else "▶️ ربات فعال شد.")
    await _show_bot_card(cb, pb.id, bot_id)


# ── تعویضِ توکن ──

@partner_customers_router.callback_query(F.data.startswith("pcu:tok:"))
async def pcu_token_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owned_sale(pb.id, bot_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 === چکِ سرورساید (دکمهٔ قدیمی هم بی‌اثر شود)
    from bot_utils import owner_swap_lock_reason
    async with Session() as _s:
        _lock = await owner_swap_lock_reason(_s, await _s.get(BotRow, bot_id))
    if _lock:
        await cb.answer(_lock.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    # === PATCH_OWNER_APPROVAL_SWAP_V1 ===
    _br = await _owned_bot_row(bot_id)
    if _br and await _needs_owner_approval(pb, _br) and not await _grant_ok(bot_id, "tok"):
        await _request_owner_approval(cb, bot, pb, _br, "tok")
        return
    await state.set_state(PartnerCustFSM.token)
    await state.update_data(pcu_bot=bot_id, pcu_pb=pb.id)
    await cb.message.answer(
        "🔑 توکنِ جدید را از <code>@BotFather</code> بگیر و همین‌جا بفرست.\n\n"
        "⚠️ بعد از تعویض، رباتِ قبلی دیگر کار نمی‌کند و همهٔ تنظیمات روی "
        "توکنِ جدید منتقل می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 انصراف", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.DANGER)]]))
    await cb.answer()


@partner_customers_router.message(PartnerCustFSM.token, F.text)
async def pcu_token_save(msg: Message, state: FSMContext):
    from bot_utils import _temp_bot, set_tenant_webhook, reclaim_token_if_archived

    data = await state.get_data()
    bot_id, pb_id = data.get("pcu_bot"), data.get("pcu_pb")
    token = (msg.text or "").strip()
    if ":" not in token or len(token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return
    if not bot_id or not await _owned_sale(pb_id, bot_id):
        await state.clear()
        await msg.answer("این مرحله منقضی شده.")
        return
    # === PATCH_OWNER_APPROVAL_SWAP_V1 === تاییدِ مالک هنگامِ ذخیره هم دوباره چک می‌شود
    _br0 = await _owned_bot_row(bot_id)
    _pb0 = await _pb_row(pb_id)
    if _br0 and _pb0 and await _needs_owner_approval(_pb0, _br0) and not await _grant_ok(bot_id, "tok"):
        await state.clear()
        await msg.answer("⌛️ تاییدِ صاحبِ ربات منقضی شده؛ دوباره درخواست بدهید.")
        return
    try:
        async with _temp_bot(token) as tmp:
            me = await tmp.get_me()
    except Exception:
        await msg.answer("نتونستم با این توکن وصل بشم. درست کپی شده؟")
        return

    async with Session() as s:
        br = await s.get(BotRow, bot_id)
        _freed, dup = await reclaim_token_if_archived(
            s, token, br.owner_id, exclude_bot_id=bot_id)
        if dup:
            await msg.answer("این توکن روی یک رباتِ فعالِ دیگر ثبت است. توکن دیگری بفرست.")
            return
        br.token = token
        br.username = me.username
        await s.commit()
    await _grant_set(bot_id, "tok", None)   # یک‌بار مصرف
    await state.clear()
    hook = await set_tenant_webhook(token)
    await msg.answer(
        f"✅ توکن عوض شد — ربات حالا @{me.username} است."
        + ("" if hook else "\n\n⚠️ اتصالِ وبهوک برقرار نشد؛ چند دقیقه بعد دوباره امتحان کن."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🤖 بازگشت به ربات", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.PRIMARY)]]))


# ── انتقالِ مالکیت ──

@partner_customers_router.callback_query(F.data.startswith("pcu:tr:"))
async def pcu_transfer_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    ok = await _partner_ok(cb, bot)
    if not ok:
        await cb.answer()
        return
    _row, pb = ok
    bot_id = int(cb.data.split(":")[2])
    if not await _owned_sale(pb.id, bot_id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    # === PATCH_SELF_OWNERSHIP_LOCK_V1 === چکِ سرورساید (دکمهٔ قدیمی هم بی‌اثر شود)
    from bot_utils import owner_swap_lock_reason
    async with Session() as _s:
        _lock = await owner_swap_lock_reason(_s, await _s.get(BotRow, bot_id))
    if _lock:
        await cb.answer(_lock.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    # === PATCH_OWNER_APPROVAL_SWAP_V1 ===
    _br = await _owned_bot_row(bot_id)
    if _br and await _needs_owner_approval(pb, _br) and not await _grant_ok(bot_id, "tr"):
        await _request_owner_approval(cb, bot, pb, _br, "tr")
        return
    await state.set_state(PartnerCustFSM.transfer)
    await state.update_data(pcu_bot=bot_id, pcu_pb=pb.id)
    await cb.message.answer(
        "👑 آیدیِ عددیِ تلگرامِ مالکِ جدید را بفرست.\n\n"
        "⚠️ بعد از انتقال، مالکِ فعلی دیگر به پنلِ این ربات دسترسی ندارد.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 انصراف", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.DANGER)]]))
    await cb.answer()


@partner_customers_router.message(PartnerCustFSM.transfer, F.text)
async def pcu_transfer_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    bot_id, pb_id = data.get("pcu_bot"), data.get("pcu_pb")
    raw = (msg.text or "").strip()
    for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
        raw = raw.replace(fa, en)
    if not raw.isdigit():
        await msg.answer("فقط آیدیِ عددی قابل‌قبول است.")
        return
    new_owner = int(raw)
    await state.clear()
    if not bot_id or not await _owned_sale(pb_id, bot_id):
        await msg.answer("این مرحله منقضی شده.")
        return
    # === PATCH_OWNER_APPROVAL_SWAP_V1 ===
    _br0 = await _owned_bot_row(bot_id)
    _pb0 = await _pb_row(pb_id)
    if _br0 and _pb0 and await _needs_owner_approval(_pb0, _br0) and not await _grant_ok(bot_id, "tr"):
        await msg.answer("⌛️ تاییدِ صاحبِ ربات منقضی شده؛ دوباره درخواست بدهید.")
        return
    await _grant_set(bot_id, "tr", None)   # یک‌بار مصرف

    async with Session() as s:
        br = await s.get(BotRow, bot_id)
        old_owner = br.owner_id
        br.owner_id = new_owner
        sale = await s.scalar(select(PartnerSale).where(
            PartnerSale.partner_bot_id == pb_id,
            PartnerSale.sold_bot_id == bot_id))
        if sale:
            # وگرنه ربات در پروفایلِ خریدارِ قبلی می‌ماند و در فهرستِ
            # مالکِ جدید اصلاً پیدا نمی‌شود.
            sale.buyer_telegram_id = new_owner
        uname = br.username
        await s.commit()

    for target, txt in (
        (old_owner, f"ℹ️ مالکیتِ ربات @{uname} از حسابِ شما منتقل شد."),
        (new_owner, f"👑 ربات @{uname} به شما منتقل شد. از «🤖 ربات‌های من» ببینش."),
    ):
        try:
            await bot.send_message(target, txt)
        except Exception:
            logger.exception("اطلاعِ انتقالِ مالکیت ارسال نشد")

    await msg.answer(
        f"✅ مالکیتِ @{uname} به <code>{new_owner}</code> منتقل شد.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🤖 بازگشت به ربات", callback_data=f"pcu:b:{bot_id}",
                                 style=ButtonStyle.PRIMARY)]]))
