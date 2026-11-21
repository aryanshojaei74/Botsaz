"""
manual_renew.py — «🔄 تمدید دستیِ سرویس» برایِ کانفیگ‌هایِ انبارِ دستی (PATCH_MANUAL_RENEW_V1).

کانفیگ‌هایِ دستی به پنل وصل نیستند، پس تمدیدشان خودکار ممکن نیست؛ ادمین باید دستی تمدید کند.
جریان:  دکمه (نام/قیمت/متن قابلِ‌ویرایش) → «از تمدیدِ همین سرویس مطمئن هستید؟» → کارت‌به‌کارت یا کیف پول
  • کیف پول: مبلغ کسر و فوراً به ادمین خبر می‌رود ← «تمدید کن».
  • کارت: رسید (عکس/متن) برایِ ادمین می‌رود ← تأیید ← «تمدید کن».
  • ادمین بعد از تمدید «✔️ تمدید شد» را می‌زند و به مشتری خبر می‌رسد (یا «↩️ برگشتِ مبلغ»).
تنظیمات (مالک): mrenew_enabled (پیش‌فرض خاموش)، mrenew_label، mrenew_price (۰ = قیمتِ خودِ سرویس)، mrenew_text.
"""
from __future__ import annotations

import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from config import Session
from models import Bot as BotRow, BotUser, Config, ManualRenewRequest, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

manual_renew_router = Router(name="vpn_manual_renew")

DEFAULT_LABEL = "🔄 تمدید دستی سرویس"
OPEN = ("awaiting_receipt", "pending", "paid")


class MrenewFSM(StatesGroup):
    receipt = State()
    cfg_label = State()
    cfg_price = State()
    cfg_text = State()


def _fa2en(s: str) -> str:
    return (s or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def _rial(n) -> str:
    return f"{int(round(float(n))):,} تومان"


async def is_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "mrenew_enabled", "0")) == "1"


async def button_label(bot_id: int) -> str:
    from tenant import get_setting
    return ((await get_setting(bot_id, "mrenew_label", DEFAULT_LABEL)) or "").strip()[:40] or DEFAULT_LABEL


async def price_for(bot_id: int, cfg) -> int:
    """قیمتِ تمدید: تنظیمِ مالک (اگر >۰) وگرنه قیمتِ خودِ سرویس."""
    from tenant import get_setting
    try:
        fixed = int(float(_fa2en(await get_setting(bot_id, "mrenew_price", "0")) or 0))
    except Exception:
        fixed = 0
    if fixed > 0:
        return fixed
    cur = await _current_spec_price(bot_id, cfg)
    return cur if cur > 0 else int(getattr(cfg, "price", 0) or 0)


async def _current_spec_price(bot_id: int, cfg) -> int:
    """PATCH_MANUAL_RENEW_CURRENT_PRICE_V1 — گزارشِ MidnightSoul: «قیمتِ سرویس عوض شده ولی تمدید با قیمتِ قبلیِ خرید حساب می‌شود».
    Config.price قیمتِ *همان ردیفِ فروخته‌شده* است و بعد از تغییرِ قیمت عوض نمی‌شود. «قیمتِ فعلی» = قیمتِ تازه‌ترین ردیفِ هم‌مشخصات
    (همان دسته/حجم/روز) که هنوز فروخته نشده؛ اگر موجودی نیست، تازه‌ترین ردیفِ هم‌مشخصات (فروخته‌شده یا نه) که قیمتش > ۰ است."""
    try:
        base = (Config.bot_id == bot_id, Config.volume_gb == cfg.volume_gb, Config.days == cfg.days,
                Config.price > 0, Config.id != cfg.id)
        cat = (Config.category == cfg.category) if cfg.category else (Config.category.is_(None))
        async with Session() as s:
            for extra in ((Config.status != "sold",), ()):
                p = await s.scalar(select(Config.price).where(*base, cat, *extra).order_by(Config.id.desc()).limit(1))
                if p:
                    return int(p)
    except Exception:      # noqa: BLE001
        logger.exception("manual_renew: خواندنِ قیمتِ فعلیِ هم‌مشخصات ناموفق بود (قیمتِ قبلی استفاده شد)")
    return 0


async def renew_button(bot_id: int, cid: int) -> InlineKeyboardButton | None:
    """دکمهٔ تمدیدِ یک کانفیگِ دستی؛ None اگر قابلیت خاموش است."""
    try:
        if not await is_enabled(bot_id):
            return None
        return InlineKeyboardButton(text=await button_label(bot_id), callback_data=f"mrn:ask:{cid}",
                                    style=ButtonStyle.SUCCESS)
    except Exception:
        logger.exception("manual_renew: ساختِ دکمه ناموفق بود")
        return None


def _svc_name(c) -> str:
    vol = "نامحدود" if not (c.volume_gb or 0) else f"{c.volume_gb} گیگ"
    base = f"{vol} / {c.days} روز"
    lab = (getattr(c, "custom_label", None) or "").strip()
    return f"{lab} — {base}" if lab else base


async def _load_own(cb: CallbackQuery, bot: Bot, cid: int):
    """(row, cfg, user) یا None (با پاسخ به کاربر)."""
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        c = await s.get(Config, cid)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id)) if row else None
    if not row or not c or c.bot_id != row.id or not u or c.owner_user_id != u.id or c.kind == "panel":
        await cb.answer("این سرویس پیدا نشد یا متعلق به شما نیست.", show_alert=True)
        return None
    if not await is_enabled(row.id):
        await cb.answer("تمدید دستی فعلاً غیرفعال است.", show_alert=True)
        return None
    return row, c, u


async def _open_request(bot_id: int, cid: int):
    async with Session() as s:
        return await s.scalar(select(ManualRenewRequest).where(
            ManualRenewRequest.bot_id == bot_id, ManualRenewRequest.config_id == cid,
            ManualRenewRequest.status.in_(OPEN)).order_by(ManualRenewRequest.id.desc()))


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logger.exception("manual_renew: ارسالِ پیام ناموفق بود")


# ───────────────────────── مشتری ─────────────────────────

@manual_renew_router.callback_query(F.data.regexp(r"^mrn:ask:\d+$"))
async def mrn_ask(cb: CallbackQuery, bot: Bot):
    cid = int(cb.data.split(":")[2])
    got = await _load_own(cb, bot, cid)
    if not got:
        return
    row, c, _u = got
    if await _open_request(row.id, cid):
        await cb.answer("برایِ این سرویس یک درخواستِ تمدید در حالِ بررسی است.", show_alert=True)
        return
    price = await price_for(row.id, c)
    if price <= 0:
        await cb.answer("قیمتِ تمدیدِ این سرویس مشخص نشده؛ با پشتیبانی در تماس باش.", show_alert=True)
        return
    from tenant import get_setting
    extra = ((await get_setting(row.id, "mrenew_text", "")) or "").strip()
    text = (f"🔄 <b>تمدید دستی سرویس</b>\n\n📦 {_html.escape(_svc_name(c))}\n"
            f"💰 مبلغ تمدید: <b>{_rial(price)}</b>\n"
            + (f"\n{_html.escape(extra)}\n" if extra else "")
            + "\n<b>از تمدیدِ همین سرویس مطمئن هستید؟</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله، مطمئنم", callback_data=f"mrn:yes:{cid}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ خیر", callback_data="mrn:no", style=ButtonStyle.DANGER)]])
    await _edit(cb, text, kb)
    await cb.answer()


@manual_renew_router.callback_query(F.data == "mrn:no")
async def mrn_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("انصراف داده شد.")


@manual_renew_router.callback_query(F.data.regexp(r"^mrn:yes:\d+$"))
async def mrn_yes(cb: CallbackQuery, bot: Bot):
    cid = int(cb.data.split(":")[2])
    got = await _load_own(cb, bot, cid)
    if not got:
        return
    row, c, u = got
    if await _open_request(row.id, cid):
        await cb.answer("برایِ این سرویس یک درخواستِ تمدید در حالِ بررسی است.", show_alert=True)
        return
    price = await price_for(row.id, c)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"mrn:card:{cid}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"💰 کیف پول (موجودی: {_rial(u.balance or 0)})",
                              callback_data=f"mrn:wal:{cid}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="mrn:no", style=ButtonStyle.DANGER)]])
    await _edit(cb, f"💳 <b>روشِ پرداخت</b>\n\nمبلغ: <b>{_rial(price)}</b>\nروشِ پرداخت را انتخاب کن:", kb)
    await cb.answer()


def _admin_kb(rid: int, state: str) -> InlineKeyboardMarkup:
    if state == "pending":
        rows = [[InlineKeyboardButton(text="✅ تأیید پرداخت", callback_data=f"mrna:ok:{rid}", style=ButtonStyle.SUCCESS),
                 InlineKeyboardButton(text="❌ رد", callback_data=f"mrna:no:{rid}", style=ButtonStyle.DANGER)]]
    else:  # paid
        rows = [[InlineKeyboardButton(text="✔️ تمدید شد", callback_data=f"mrna:done:{rid}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="↩️ برگشتِ مبلغ به کیف پول", callback_data=f"mrna:ref:{rid}",
                                      style=ButtonStyle.DANGER)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _admin_text(row, req, cfg, user_tg: int, head: str) -> str:
    uname = f"<code>{user_tg}</code>"
    async with Session() as s:
        bu = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == user_tg))
    if bu and getattr(bu, "username", None):
        uname = f"@{_html.escape(bu.username)} ({user_tg})"
    return (f"{head}\n\n👤 کاربر: {uname}\n📦 سرویس: <b>{_html.escape(_svc_name(cfg))}</b> — کد <code>{cfg.code}</code>\n"
            f"💰 مبلغ: <b>{_rial(req.amount)}</b> — {'کیف پول' if req.method == 'wallet' else 'کارت به کارت'}\n"
            f"🆔 درخواست #{req.id}")


@manual_renew_router.callback_query(F.data.regexp(r"^mrn:wal:\d+$"))
async def mrn_wallet(cb: CallbackQuery, bot: Bot):
    cid = int(cb.data.split(":")[2])
    got = await _load_own(cb, bot, cid)
    if not got:
        return
    row, c, u = got
    if await _open_request(row.id, cid):
        await cb.answer("برایِ این سرویس یک درخواستِ تمدید در حالِ بررسی است.", show_alert=True)
        return
    price = await price_for(row.id, c)
    if price <= 0:
        await cb.answer("قیمتِ تمدید مشخص نشده.", show_alert=True)
        return
    async with Session() as s:
        # کسرِ اتمی: فقط اگر موجودی کافی باشد
        r = await s.execute(update(BotUser).where(BotUser.id == u.id, BotUser.balance >= price)
                            .values(balance=BotUser.balance - price))
        if not r.rowcount:
            await s.rollback()
            bal = int(u.balance or 0)
            await cb.answer(f"موجودی کافی نیست. موجودی: {bal:,} — مبلغ: {price:,} تومان", show_alert=True)
            return
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=-price, kind="manual_renew",
                        note=f"تمدید دستیِ سرویس {c.code}"))
        req = ManualRenewRequest(bot_id=row.id, config_id=cid, user_telegram_id=cb.from_user.id,
                                 amount=price, method="wallet", status="paid")
        s.add(req)
        await s.commit()
        rid = req.id
    await _edit(cb, "✅ مبلغ از کیف پولت کسر شد و درخواستِ تمدید برایِ ادمین ارسال شد.\n"
                    "بعد از تمدید همین‌جا خبرت می‌کنم.")
    await cb.answer()
    from tenant import _notify_admins
    await _notify_admins(bot, row, await _admin_text(row, req, c, cb.from_user.id,
                         "🔄 <b>درخواستِ تمدیدِ دستی (پرداخت از کیف پول ✅)</b>\nمبلغ کسر شده؛ سرویس را دستی تمدید کن."),
                         _admin_kb(rid, "paid"))


@manual_renew_router.callback_query(F.data.regexp(r"^mrn:card:\d+$"))
async def mrn_card(cb: CallbackQuery, bot: Bot, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    got = await _load_own(cb, bot, cid)
    if not got:
        return
    row, c, _u = got
    if await _open_request(row.id, cid):
        await cb.answer("برایِ این سرویس یک درخواستِ تمدید در حالِ بررسی است.", show_alert=True)
        return
    from tenant import get_setting
    if (await get_setting(row.id, "card_enabled", "1")) != "1":
        await cb.answer("پرداختِ کارت‌به‌کارت فعلاً غیرفعال است؛ از کیف پول استفاده کن.", show_alert=True)
        return
    price = await price_for(row.id, c)
    card = await get_setting(row.id, "card_number", "")
    holder = await get_setting(row.id, "card_holder", "")
    if not card:
        await cb.answer("شمارهٔ کارت ثبت نشده؛ با پشتیبانی تماس بگیر.", show_alert=True)
        return
    async with Session() as s:
        req = ManualRenewRequest(bot_id=row.id, config_id=cid, user_telegram_id=cb.from_user.id,
                                 amount=price, method="card", status="awaiting_receipt")
        s.add(req)
        await s.commit()
        rid = req.id
    await state.set_state(MrenewFSM.receipt)
    await state.update_data(mrn_rid=rid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ انصراف", callback_data=f"mrn:cancel:{rid}", style=ButtonStyle.DANGER)]])
    await _edit(cb, "💳 <b>پرداختِ کارت به کارت</b>\n\n"
                    f"<code>{_html.escape(card)}</code>\n"
                    + (f"👤 به نام: <b>{_html.escape(holder)}</b>\n" if holder else "")
                    + f"\n💵 مبلغ: <code>{price:,} تومان</code>\n\n"
                      "📸 بعد از واریز، <b>عکس یا متنِ رسید</b> را همین‌جا بفرست.", kb)
    await cb.answer()


@manual_renew_router.callback_query(F.data.regexp(r"^mrn:cancel:\d+$"))
async def mrn_cancel(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[2])
    await state.clear()
    async with Session() as s:
        await s.execute(update(ManualRenewRequest).where(
            ManualRenewRequest.id == rid, ManualRenewRequest.user_telegram_id == cb.from_user.id,
            ManualRenewRequest.status == "awaiting_receipt").values(status="cancelled"))
        await s.commit()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("انصراف داده شد.")


@manual_renew_router.message(StateFilter(MrenewFSM.receipt), F.chat.type == "private")
async def mrn_receipt(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    rid = data.get("mrn_rid")
    photo = msg.photo[-1].file_id if msg.photo else None
    text = (msg.text or msg.caption or "").strip()
    if not photo and not text:
        await msg.answer("لطفاً عکس یا متنِ رسید را بفرست (یا «انصراف» را بزن).")
        return
    async with Session() as s:
        req = await s.get(ManualRenewRequest, rid) if rid else None
        if not req or req.user_telegram_id != msg.from_user.id or req.status != "awaiting_receipt":
            await state.clear()
            await msg.answer("این درخواست دیگر فعال نیست.")
            return
        req.status = "pending"
        req.receipt_file_id = photo
        req.receipt_text = text[:500] or None
        c = await s.get(Config, req.config_id)
        row = await s.get(BotRow, req.bot_id)
        await s.commit()
    await state.clear()
    await msg.answer("✅ رسیدت برایِ ادمین ارسال شد. بعد از تأییدِ پرداخت، تمدید انجام و خبرت می‌کنم.")
    body = await _admin_text(row, req, c, msg.from_user.id, "🔄 <b>درخواستِ تمدیدِ دستی (کارت به کارت) — رسید</b>")
    if req.receipt_text:
        body += f"\n\n📝 متنِ رسید: {_html.escape(req.receipt_text)}"
    from tenant import _notify_admins
    await _notify_admins(bot, row, body, _admin_kb(req.id, "pending"), photo_file_id=photo)


# ───────────────────────── ادمین ─────────────────────────

async def _admin_gate(cb: CallbackQuery, bot: Bot):
    from tenant import is_owner_or_admin
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط ادمین‌ها.", show_alert=True)
        return None
    return row


async def _finish_admin_msg(cb: CallbackQuery, note: str, kb: InlineKeyboardMarkup | None = None):
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=(cb.message.html_text or "") + "\n\n" + note,
                                          reply_markup=kb, parse_mode="HTML")
        else:
            await cb.message.edit_text((cb.message.html_text or "") + "\n\n" + note,
                                       reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("manual_renew: ویرایشِ پیامِ ادمین")


async def _move(rid: int, frm: tuple, to: str, by: int) -> "ManualRenewRequest | None":
    import datetime as _dtm
    async with Session() as s:
        r = await s.execute(update(ManualRenewRequest).where(
            ManualRenewRequest.id == rid, ManualRenewRequest.status.in_(frm))
            .values(status=to, decided_at=_dtm.datetime.utcnow(), decided_by=by))
        await s.commit()
        if not r.rowcount:
            return None
        return await s.get(ManualRenewRequest, rid)


@manual_renew_router.callback_query(F.data.regexp(r"^mrna:(ok|no|done|ref):\d+$"))
async def mrna_decide(cb: CallbackQuery, bot: Bot):
    row = await _admin_gate(cb, bot)
    if not row:
        return
    _, act, rid_s = cb.data.split(":")
    rid = int(rid_s)
    who = f"@{cb.from_user.username}" if cb.from_user.username else str(cb.from_user.id)
    async with Session() as s:
        req0 = await s.get(ManualRenewRequest, rid)
        cfg = await s.get(Config, req0.config_id) if req0 else None
    if not req0 or req0.bot_id != row.id:
        await cb.answer("درخواست پیدا نشد.", show_alert=True)
        return
    name = _html.escape(_svc_name(cfg)) if cfg else "سرویس"
    if act == "ok":
        req = await _move(rid, ("pending",), "paid", cb.from_user.id)
        if not req:
            await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
            return
        await cb.answer("پرداخت تأیید شد")
        await _finish_admin_msg(cb, f"✅ پرداخت تأیید شد ({who}). حالا سرویس را دستی تمدید کن و «تمدید شد» را بزن.",
                                _admin_kb(rid, "paid"))
        try:
            await bot.send_message(req.user_telegram_id,
                                   f"✅ پرداختت برایِ تمدیدِ «{name}» تأیید شد؛ ادمین به‌زودی تمدید می‌کند و خبرت می‌دهم.")
        except Exception:
            logger.exception("manual_renew: پیام به کاربر")
    elif act == "no":
        req = await _move(rid, ("pending",), "rejected", cb.from_user.id)
        if not req:
            await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
            return
        await cb.answer("رد شد")
        await _finish_admin_msg(cb, f"❌ رد شد ({who})")
        try:
            await bot.send_message(req.user_telegram_id,
                                   f"❌ رسیدِ تمدیدِ «{name}» تأیید نشد. اگر واریز کرده‌ای با پشتیبانی تماس بگیر.")
        except Exception:
            logger.exception("manual_renew: پیام به کاربر")
    elif act == "done":
        req = await _move(rid, ("paid",), "done", cb.from_user.id)
        if not req:
            await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
            return
        await cb.answer("ثبت شد")
        await _finish_admin_msg(cb, f"✔️ تمدید انجام شد ({who})")
        try:
            await bot.send_message(req.user_telegram_id, f"✅ سرویس «{name}» تمدید شد. ممنون از خریدت 🌹")
        except Exception:
            logger.exception("manual_renew: پیام به کاربر")
    else:  # ref — فقط وقتی مبلغ از کیف پول کسر شده یا ادمین رسید را پذیرفته؛ در هر دو حالت به کیف پول برمی‌گردد
        req = await _move(rid, ("paid",), "refunded", cb.from_user.id)
        if not req:
            await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
            return
        async with Session() as s:
            await s.execute(update(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == req.user_telegram_id)
                .values(balance=BotUser.balance + req.amount))
            bu = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id, BotUser.telegram_id == req.user_telegram_id))
            if bu:
                s.add(WalletTxn(bot_id=row.id, user_id=bu.id, amount=req.amount, kind="refund",
                                note=f"برگشتِ تمدید دستیِ سرویس {cfg.code if cfg else ''}"))
            await s.commit()
        await cb.answer("برگشت خورد")
        await _finish_admin_msg(cb, f"↩️ مبلغ به کیف پولِ کاربر برگشت ({who})")
        try:
            await bot.send_message(req.user_telegram_id,
                                   f"↩️ تمدیدِ «{name}» انجام نشد و {_rial(req.amount)} به کیف پولت برگشت.")
        except Exception:
            logger.exception("manual_renew: پیام به کاربر")


# ───────────────────────── تنظیماتِ مالک ─────────────────────────

async def _cfg_screen(bot_id: int):
    from tenant import get_setting
    on = await is_enabled(bot_id)
    price = (await get_setting(bot_id, "mrenew_price", "0")) or "0"
    txt = ((await get_setting(bot_id, "mrenew_text", "")) or "").strip()
    text = ("🔄 <b>تمدید دستیِ سرویس</b>\n\n"
            "برایِ کانفیگ‌هایِ انبارِ دستی (که به پنل وصل نیستند) یک دکمهٔ تمدید کنار سرویسِ مشتری نمایش داده می‌شود؛ "
            "مشتری با کارت‌به‌کارت یا کیف پول پرداخت می‌کند و درخواست برایِ شما می‌آید تا دستی تمدید کنید.\n\n"
            f"• وضعیت: {'✅ روشن' if on else '⬜️ خاموش'}\n"
            f"• نامِ دکمه: {_html.escape(await button_label(bot_id))}\n"
            f"• مبلغ: {'قیمتِ خودِ هر سرویس' if str(price).strip() in ('0', '') else _rial(price)}\n"
            f"• متنِ توضیح: {_html.escape(txt) if txt else '—'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬜️ خاموش کردن" if on else "✅ روشن کردن", callback_data="mrn:cfgt")],
        [InlineKeyboardButton(text="✏️ نامِ دکمه", callback_data="mrn:cfgl"),
         InlineKeyboardButton(text="💰 مبلغِ ثابت", callback_data="mrn:cfgp")],
        [InlineKeyboardButton(text="📝 متنِ توضیح", callback_data="mrn:cfgx")]])
    return text, kb


@manual_renew_router.callback_query(F.data == "mrn:cfg")
async def mrn_cfg(cb: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    row = await _admin_gate(cb, bot)
    if not row:
        return
    if state is not None:
        await state.clear()
    text, kb = await _cfg_screen(row.id)
    await _edit(cb, text, kb)
    await cb.answer()



@manual_renew_router.callback_query(F.data == "mrn:cfgt")
async def mrn_cfg_toggle(cb: CallbackQuery, bot: Bot):
    row = await _admin_gate(cb, bot)
    if not row:
        return
    from tenant import set_setting
    await set_setting(row.id, "mrenew_enabled", "0" if await is_enabled(row.id) else "1")
    text, kb = await _cfg_screen(row.id)
    await _edit(cb, text, kb)
    await cb.answer("✅ تغییر کرد")


@manual_renew_router.callback_query(F.data.regexp(r"^mrn:cfg[lpx]$"))
async def mrn_cfg_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_gate(cb, bot)
    if not row:
        return
    which = cb.data[-1]
    st, prompt = {
        "l": (MrenewFSM.cfg_label, "نامِ دکمه را بفرست (حداکثر ۴۰ حرف). مثال: 🔄 تمدید دستی سرویس"),
        "p": (MrenewFSM.cfg_price, "مبلغِ ثابتِ تمدید را به تومان بفرست. «0» یعنی قیمتِ خودِ هر سرویس."),
        "x": (MrenewFSM.cfg_text, "متنِ توضیح (زیرِ مبلغ، موقعِ تأیید) را بفرست. «-» یعنی بدونِ متن."),
    }[which]
    await state.set_state(st)
    await cb.message.answer(prompt + "\n\nبرایِ انصراف /cancel بفرست.")
    await cb.answer()


@manual_renew_router.message(StateFilter(MrenewFSM.cfg_label, MrenewFSM.cfg_price, MrenewFSM.cfg_text), F.text)
async def mrn_cfg_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    cur = await state.get_state()
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو", "❌ لغو"):
        await state.clear()
        text, kb = await _cfg_screen(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    if cur == MrenewFSM.cfg_label.state:
        await set_setting(row.id, "mrenew_label", raw[:40])
    elif cur == MrenewFSM.cfg_price.state:
        v = _fa2en(raw).replace(",", "").replace("٬", "").strip()
        if not v.isdigit():
            await msg.answer("فقط عدد (تومان) بفرست، مثلاً 150000 — یا 0.")
            return
        await set_setting(row.id, "mrenew_price", str(int(v)))
    else:
        await set_setting(row.id, "mrenew_text", "" if raw == "-" else raw[:400])
    await state.clear()
    text, kb = await _cfg_screen(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
