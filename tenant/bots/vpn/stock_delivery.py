"""stock_delivery.py — «تحویل دستیِ کانفیگ‌هایِ آماده» (PATCH_STOCK_MANUAL_DELIVERY_V1).

مشکل (گزارشِ Rocket): کانفیگِ آمادهٔ انبار از لحظهٔ ساختش روز از دستش می‌رود؛ مشتری‌ای که چند روز بعد می‌خرد
کانفیگی با ۲۷ روز باقی‌مانده می‌گیرد. راهِ‌حل: ادمین «تحویل دستی» را روشن می‌کند؛ بعد از پرداخت (کیف پول یا تاییدِ رسیدِ
کارت) انبار دست نمی‌خورد و کانفیگ خودکار فرستاده نمی‌شود. به مشتری «در صفِ تحویل» می‌گوییم، ادمین کانفیگِ تازه را خودش
می‌فرستد (هر پیام/فایل؛ ربات عیناً برایِ مشتری کپی می‌کند) و «پایان تحویل» را می‌زند.

فقط «کانفیگ آمادهٔ انبار» (نه پلن‌هایِ خودکار از پنل). هیچ پولی دوبار کم/اضافه نمی‌شود: سفارش همان لحظه paid است؛ برگشتِ وجه
اتمیک (فقط از pending).
"""
from __future__ import annotations

import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, Order, StockDelivery, WalletTxn

logger = _get_logger(__name__)
sd_router = Router(name="stock_delivery")

QUEUED_TEXT = ("✅ پرداختت تایید شد.\n\n📦 سفارشت در <b>صفِ تحویل</b> قرار گرفت و به‌زودی برایت ارسال خواهد شد. "
               "اگر سؤالی داشتی به پشتیبانی پیام بده.")


class SdFSM(StatesGroup):
    sending = State()
    hours = State()


async def deadline_hours(bot_id: int) -> int:
    """مهلتِ پیش‌فرضِ تحویل (ساعت) که به مشتری گفته می‌شود و سفارش‌هایِ دیرکرده را در فهرست علامت می‌زند."""
    from tenant import get_setting
    try:
        return max(1, min(720, int(float(await get_setting(bot_id, "stock_delivery_hours", "24") or 24))))
    except (TypeError, ValueError):
        return 24


def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


async def is_on(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "stock_manual_delivery", "0")) == "1"


def queue(s, *, bot_id: int, user_id: int, order_id: int | None, volume_gb: int, days: int, count: int,
          amount, label: str | None = None) -> StockDelivery:
    """داخلِ همان تراکنشِ پرداخت صدا زده می‌شود (s = Session)؛ بعد از commit باید announce() صدا زده شود."""
    d = StockDelivery(bot_id=bot_id, order_id=order_id, user_id=user_id, volume_gb=int(volume_gb or 0),
                      days=int(days or 0), count=max(1, int(count or 1)), amount=int(float(amount or 0)),
                      label=label, status="pending")
    s.add(d)
    return d


def _spec(d: StockDelivery) -> str:
    from tenant import fmt_days
    from tenant.bots.vpn.vpn_user import fmt_vol
    return f"{fmt_vol(d.volume_gb)} · {fmt_days(d.days)}" + (f" × {d.count}" if d.count > 1 else "")


async def announce(bot: Bot, delivery_id: int) -> None:
    """به مشتری «در صفِ تحویل» + به ادمین‌ها سفارش با دکمهٔ ارسال. هرگز استثنا بالا نمی‌دهد."""
    try:
        from tenant import _notify_manual_order
        async with Session() as s:
            d = await s.get(StockDelivery, delivery_id)
            if not d:
                return
            u = await s.get(BotUser, d.user_id)
            from models import Bot as BotRow
            row = await s.get(BotRow, d.bot_id)
            ono = None
            if d.order_id:
                try:
                    from models import order_no
                    ono = await order_no(Order, d.order_id)
                except Exception:
                    logger.info("sd: شمارهٔ سفارش گرفته نشد")
        if u:
            try:
                _h = await deadline_hours(d.bot_id)
                await bot.send_message(u.telegram_id,
                                       QUEUED_TEXT + f"\n⏱ زمانِ تقریبیِ تحویل: حداکثر <b>{_h} ساعت</b>.", parse_mode="HTML")
            except Exception:
                logger.exception("sd: پیامِ صفِ تحویل به مشتری نرسید")
        if row:
            who = f"<a href=\"tg://user?id={u.telegram_id}\">{_html.escape(u.full_name or str(u.telegram_id))}</a>" if u else "—"
            txt = (f"📦 <b>سفارشِ کانفیگ آماده — منتظرِ تحویلِ دستی</b>"
                   + (f" (سفارش #{ono})" if ono else "") + "\n\n"
                   f"🎁 بسته: <b>{_spec(d)}</b>\n💰 مبلغ: {d.amount:,} تومان\n👤 مشتری: {who} (<code>{u.telegram_id if u else ''}</code>)\n"
                   + (f"🏷 نامِ دلخواهِ مشتری: {_html.escape(d.label)}\n" if d.label else "")
                   + "\nکانفیگِ تازه را خودت بفرست؛ بعد «پایان تحویل» را بزن.")
            await _notify_manual_order(bot, row, txt, _kb([
                [_btn("📦 ارسالِ کانفیگ", f"sd:send:{d.id}", ButtonStyle.SUCCESS)],
                [_btn("↩️ لغو و برگشتِ وجه", f"sd:refundask:{d.id}", ButtonStyle.DANGER)]]))
    except Exception:
        logger.exception("sd: announce ناموفق (delivery=%s)", delivery_id)


async def _admin_row(cb_or_msg, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    uid = cb_or_msg.from_user.id
    if not row or not await is_owner_or_admin(row.id, uid, row.owner_id):
        if not isinstance(cb_or_msg, Message):
            await cb_or_msg.answer("دسترسی ندارید", show_alert=True)
        return None
    return row


async def _load(row, did: int):
    async with Session() as s:
        d = await s.get(StockDelivery, did)
    if not d or d.bot_id != row.id:
        return None
    return d


@sd_router.callback_query(F.data.regexp(r"^sd:send:\d+$"))
async def sd_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    d = await _load(row, int(cb.data.split(":")[2]))
    if not d or d.status != "pending":
        await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        return
    await state.clear()
    await state.update_data(sd_id=d.id)
    await state.set_state(SdFSM.sending)
    await bot.send_message(cb.from_user.id,
                           f"📦 کانفیگ‌ِ سفارش (<b>{_spec(d)}</b>) را بفرست — هر پیام/فایلی که بفرستی عیناً برایِ مشتری کپی می‌شود"
                           + (f" (این سفارش {d.count} عدد است؛ همه را بفرست)" if d.count > 1 else "")
                           + ".\nبعد از آخرین کانفیگ «✅ پایان تحویل» را بزن.",
                           parse_mode="HTML",
                           reply_markup=_kb([[_btn("✅ پایان تحویل", f"sd:done:{d.id}", ButtonStyle.SUCCESS)],
                                             [_btn("❌ انصراف", f"sd:cancel:{d.id}", ButtonStyle.DANGER)]]))
    await cb.answer()


@sd_router.message(SdFSM.sending)
async def sd_receive(msg: Message, bot: Bot, state: FSMContext):
    row = await _admin_row(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    d = await _load(row, int(data.get("sd_id") or 0))
    if not d or d.status != "pending":
        await state.clear()
        await msg.answer("این سفارش قبلاً رسیدگی شده.")
        return
    async with Session() as s:
        u = await s.get(BotUser, d.user_id)
    if not u:
        await msg.answer("کاربر پیدا نشد.")
        return
    try:
        await bot.copy_message(u.telegram_id, msg.chat.id, msg.message_id)
    except Exception as e:
        logger.warning("sd: کپی به مشتری ناموفق: %s", e)
        await msg.answer(f"❌ ارسال به مشتری ناموفق بود: <code>{_html.escape(str(e)[:150])}</code>\nدوباره بفرست.",
                         parse_mode="HTML")
        return
    async with Session() as s:
        await s.execute(update(StockDelivery).where(StockDelivery.id == d.id, StockDelivery.status == "pending")
                        .values(sent_count=StockDelivery.sent_count + 1))
        await s.commit()
    await msg.answer(f"✅ برایِ مشتری ارسال شد. (تعداد تا الان: {d.sent_count + 1})",
                     reply_markup=_kb([[_btn("✅ پایان تحویل", f"sd:done:{d.id}", ButtonStyle.SUCCESS)]]))


@sd_router.callback_query(F.data.regexp(r"^sd:done:\d+$"))
async def sd_done(cb: CallbackQuery, bot: Bot, state: FSMContext):
    import datetime as _dt
    row = await _admin_row(cb, bot)
    if not row:
        return
    did = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(StockDelivery, did, with_for_update=True)
        if not d or d.bot_id != row.id or d.status != "pending":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        if int(d.sent_count or 0) < 1:
            await cb.answer("هنوز چیزی برایِ مشتری نفرستاده‌ای.", show_alert=True)
            return
        d.status = "delivered"
        d.delivered_by = cb.from_user.id
        d.done_at = _dt.datetime.utcnow()
        u = await s.get(BotUser, d.user_id)
        tg = u.telegram_id if u else None
        await s.commit()
    await state.clear()
    if tg:
        try:
            await bot.send_message(tg, "✅ سفارشت تحویل داده شد. ممنون از خریدت! 🌹")
        except Exception:
            logger.exception("sd: پیامِ تحویل به مشتری نرسید")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.info("sd: پاک‌کردنِ دکمه‌ها ممکن نشد (بی‌اهمیت)")
    await bot.send_message(cb.from_user.id, "✅ تحویل ثبت شد.")
    await cb.answer("ثبت شد")


@sd_router.callback_query(F.data.regexp(r"^sd:cancel:\d+$"))
async def sd_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("انصراف شد؛ از «📦 تحویل دستی» دوباره می‌توانی ادامه بدهی.", show_alert=True)


@sd_router.callback_query(F.data.regexp(r"^sd:refundask:\d+$"))
async def sd_refund_ask(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    d = await _load(row, int(cb.data.split(":")[2]))
    if not d or d.status != "pending":
        await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
        return
    await bot.send_message(cb.from_user.id,
                           f"↩️ مبلغ <b>{d.amount:,} تومان</b> به کیف پولِ مشتری برگردد و سفارش لغو شود؟",
                           parse_mode="HTML",
                           reply_markup=_kb([[_btn("✅ بله، برگردان", f"sd:refund:{d.id}", ButtonStyle.DANGER)],
                                             [_btn("⬅️ انصراف", f"sd:cancel:{d.id}")]]))
    await cb.answer()


@sd_router.callback_query(F.data.regexp(r"^sd:refund:\d+$"))
async def sd_refund(cb: CallbackQuery, bot: Bot):
    import datetime as _dt
    row = await _admin_row(cb, bot)
    if not row:
        return
    did = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(StockDelivery, did, with_for_update=True)
        if not d or d.bot_id != row.id or d.status != "pending":          # اتمیک: فقط یک‌بار
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        u = await s.get(BotUser, d.user_id, with_for_update=True)
        d.status = "refunded"
        d.delivered_by = cb.from_user.id
        d.done_at = _dt.datetime.utcnow()
        if u and d.amount > 0:
            u.balance = float(u.balance or 0) + d.amount
            s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=d.amount, kind="refund",
                            note="برگشتِ وجهِ سفارشِ لغوشدهٔ تحویل دستی"))
        if d.order_id:
            o = await s.get(Order, d.order_id)
            if o and o.status == "paid":
                o.status = "refunded"
        tg = u.telegram_id if u else None
        await s.commit()
    if tg:
        try:
            await bot.send_message(tg, f"↩️ سفارشت لغو شد و {d.amount:,} تومان به کیف پولت برگشت.")
        except Exception:
            logger.exception("sd: پیامِ برگشت به مشتری نرسید")
    await bot.send_message(cb.from_user.id, "✅ لغو شد و وجه به کیف پولِ مشتری برگشت.")
    await cb.answer("انجام شد")


# ─────────────── منویِ ادمین ───────────────
def _age_h(d: StockDelivery) -> float:
    import datetime as _dt
    return max(0.0, (_dt.datetime.utcnow() - (d.created_at or _dt.datetime.utcnow())).total_seconds() / 3600)


async def _counts(bot_id: int) -> tuple[int, int]:
    async with Session() as s:
        pend = await s.scalar(select(func.count()).select_from(StockDelivery).where(
            StockDelivery.bot_id == bot_id, StockDelivery.status == "pending")) or 0
        done = await s.scalar(select(func.count()).select_from(StockDelivery).where(
            StockDelivery.bot_id == bot_id, StockDelivery.status == "delivered")) or 0
    return int(pend), int(done)


async def _menu_view(bot_id: int):
    on = await is_on(bot_id)
    hrs = await deadline_hours(bot_id)
    pend_n, done_n = await _counts(bot_id)
    txt = ("📬 <b>صفِ تحویل — کانفیگ‌هایِ آماده</b>\n\n"
           "وقتی تحویلِ دستی روشن باشد، بعد از پرداختِ «کانفیگ آمادهٔ انبار» (کیف پول یا تاییدِ رسیدِ کارت) کانفیگ خودکار فرستاده نمی‌شود؛ "
           "به مشتری «در صفِ تحویل» می‌گوییم و کانفیگِ تازه را خودت می‌فرستی. انبار دست‌نخورده می‌ماند، پس مشتری کانفیگی با روزِ کم‌شده "
           "نمی‌گیرد.\n\n"
           f"وضعیت: <b>{'🟢 دستی (روشن)' if on else '⚪️ خودکار (خاموش)'}</b>\n"
           f"⏱ مهلتِ پیش‌فرضِ تحویل: <b>{hrs} ساعت</b>\n"
           f"📬 در انتظار: <b>{pend_n}</b> | 📁 انجام‌شده: <b>{done_n}</b>\n"
           "<i>(پلن‌هایِ خودکار از پنل همچنان فوری تحویل می‌شوند.)</i>")
    rows = [[_btn("🟢 تحویل دستی: روشن (بزن تا خودکار شود)" if on else "⚪️ تحویل خودکار (بزن تا دستی شود)", "sdl:toggle",
                  ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY)],
            [_btn(f"📬 در انتظار تحویل ({pend_n})", "sdl:pend", ButtonStyle.PRIMARY),
             _btn(f"📁 انجام‌شده‌ها ({done_n})", "sdl:done")],
            [_btn(f"⏱ مهلتِ پیش‌فرض تحویل: {hrs} ساعت", "sdl:hours")],
            [_btn("⬅️ بازگشت", "tadm:sec:settings", ButtonStyle.DANGER)]]
    return txt, _kb(rows)


async def _list_view(bot_id: int, status: str):
    hrs = await deadline_hours(bot_id)
    async with Session() as s:
        items = (await s.execute(select(StockDelivery, BotUser).join(BotUser, BotUser.id == StockDelivery.user_id).where(
            StockDelivery.bot_id == bot_id, StockDelivery.status == status)
            .order_by(StockDelivery.id.desc() if status != "pending" else StockDelivery.id.asc()).limit(20))).all()
    rows, lines = [], [("📬 <b>در انتظار تحویل</b>" if status == "pending" else "📁 <b>انجام‌شده‌ها (۲۰ موردِ آخر)</b>") + "\n"]
    for d, u in items:
        who = u.full_name or str(u.telegram_id)
        if status == "pending":
            late = _age_h(d) > hrs
            lines.append(f"{'🔴' if late else '🟡'} #{d.id} — {_spec(d)} — {d.amount:,} ت — {_html.escape(who)} — {int(_age_h(d))} ساعت پیش"
                         + (" ⚠️ دیرکرد" if late else ""))
            rows.append([_btn(f"📦 #{d.id} — {_spec(d)} — ارسال", f"sd:send:{d.id}", ButtonStyle.DANGER if late else ButtonStyle.PRIMARY)])
        else:
            lines.append(f"✅ #{d.id} — {_spec(d)} — {d.amount:,} ت — {_html.escape(who)} — {d.sent_count} کانفیگ")
    if not items:
        lines.append("✅ چیزی در انتظارِ تحویل نیست." if status == "pending" else "هنوز تحویلی ثبت نشده.")
    rows.append([_btn("⬅️ بازگشت", "sdl:menu", ButtonStyle.DANGER)])
    return "\n".join(lines), _kb(rows)


async def admin_menu(cb, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    txt, kb = await _menu_view(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(cb.from_user.id, txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@sd_router.callback_query(F.data == "sdl:menu")
async def sdl_menu(cb: CallbackQuery, bot: Bot):
    await admin_menu(cb, bot)


@sd_router.callback_query(F.data == "sdl:toggle")
async def sdl_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    await set_setting(row.id, "stock_manual_delivery", "0" if await is_on(row.id) else "1")
    await admin_menu(cb, bot)


@sd_router.callback_query(F.data.in_({"sdl:pend", "sdl:done"}))
async def sdl_list(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    txt, kb = await _list_view(row.id, "pending" if cb.data == "sdl:pend" else "delivered")
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(cb.from_user.id, txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@sd_router.callback_query(F.data == "sdl:hours")
async def sdl_hours_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    await state.clear()
    await state.set_state(SdFSM.hours)
    await bot.send_message(cb.from_user.id, "⏱ مهلتِ پیش‌فرضِ تحویل را به ساعت بفرست (۱ تا ۷۲۰؛ مثلاً 1 یا 24):",
                           reply_markup=_kb([[_btn("❌ لغو", "sdl:menu", ButtonStyle.DANGER)]]))
    await cb.answer()


@sd_router.message(SdFSM.hours, F.text)
async def sdl_hours_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _admin_row(msg, bot)
    if not row:
        await state.clear()
        return
    t = _fa(msg.text or "")
    if not t.isdigit() or not (1 <= int(t) <= 720):
        await msg.answer("❌ عددِ معتبر بین ۱ تا ۷۲۰ بفرست.")
        return
    await set_setting(row.id, "stock_delivery_hours", str(int(t)))
    await state.clear()
    txt, kb = await _menu_view(row.id)
    await msg.answer("✅ ذخیره شد.\n\n" + txt, reply_markup=kb, parse_mode="HTML")
