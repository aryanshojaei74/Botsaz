"""
kyc_admin.py — PATCH_KYC_V1
پنلِ «🛡 احرازِ هویت» برایِ مالکِ رباتِ مستأجر.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from tenant.shared import kyc as K

from logging_setup import get_logger as _get_logger
from sqlalchemy import func as _func
logger = _get_logger(__name__)

kyc_admin_router = Router()

_DEF_L2_TEXT = (
    "برایِ احرازِ هویتِ سطحِ ۲:\n\n"
    "۱) کارتِ بانکیای که با آن پرداخت کرده‌ای و کارتِ ملی را کنارِ هم بگیر.\n"
    "۲) روی یک برگه بنویس: «این احرازِ هویت برایِ [نامِ مجموعه] است و "
    "قوانینِ مجموعه را پذیرفتم.» به‌همراهِ تاریخ و امضا.\n"
    "۳) هر سه را جلوی صورتت بگیر و همان متن را بخوان.\n\n"
    "هر سه مورد باید واضح و خوانا باشند."
)


class KycFSM(StatesGroup):
    cc_policy = State()   # PATCH_KYC_CARD_CHANGE_V1
    cap0 = State()
    cap1 = State()
    l2text = State()
    user_lookup = State()
    user_card = State()
    reject_reason = State()   # PATCH_KYC_REJECT_REASON_V1


async def _content(bot_id: int):
    from tenant import get_setting
    on = await K.enabled(bot_id)
    c0 = await K.cap_for(bot_id, 0)
    c1 = await K.cap_for(bot_id, 1)
    media = await get_setting(bot_id, "kyc_l2_media", "video_note")
    win = await get_setting(bot_id, "kyc_window", "rolling")
    cc_max, cc_days = await K.card_change_policy(bot_id)

    txt = ("🛡 <b>احرازِ هویتِ کاربران</b>\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n\n"
           f"• سطح ۰ (بدونِ احراز): سقفِ روزانه <b>{c0:,}</b> تومان\n"
           f"• سطح ۱ (کارتِ بانکی): سقفِ روزانه <b>{c1:,}</b> تومان\n"
           "  عبور از این سقف ⇒ حساب تا احرازِ سطح ۲ مسدود می‌شود.\n"
           "  (سطح ۰: فقط همان روز نمی‌تواند شارژ کند و بعدِ پایانِ بازه دوباره باز می‌شود.)\n"
           "• سطح ۲ (مدارک): بدونِ محدودیت\n"
           f"  نوعِ فایلِ درخواستی: <b>{K.media_label(media)}</b>\n\n"
           f"📆 بازهٔ «روزانه»: <b>{'از نیمه‌شبِ تهران (۰۰:۰۰ صفر می‌شود)' if win == 'calendar' else '۲۴ ساعتِ گذشته (غلتان)'}</b>\n"
           f"🔄 تغییرِ کارت: <b>{cc_max} بار در {cc_days} روز</b>" + (" (ممنوع)" if cc_max <= 0 else ""))

    rows = [
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"),
            callback_data="kyc:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text=f"💵 سقفِ سطح ۰ ({c0:,})",
                              callback_data="kyc:cap0", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"💳 سقفِ روزانهٔ سطح ۱ ({c1:,})",
                              callback_data="kyc:cap1", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=("📆 بازه: نیمه‌شبِ تهران ← تبدیل به ۲۴ ساعتِ غلتان" if win == "calendar"
                                    else "📆 بازه: ۲۴ ساعتِ غلتان ← تبدیل به نیمه‌شبِ تهران"),
                              callback_data="kyc:window", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔄 محدودیتِ تغییرِ کارت ({cc_max} بار / {cc_days} روز)",
                              callback_data="kyc:ccpol", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="👥 مدیریتِ کاربران و کارت‌هایِ احراز‌شده",
                              callback_data="kyc:users", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🎬 نوعِ فایلِ سطح ۲ ({K.media_label(media)})",
                              callback_data="kyc:media", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📝 متنِ دستورالعملِ سطح ۲",
                              callback_data="kyc:l2text", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📋 درخواست‌های در انتظار",
                              callback_data="kyc:pend:0", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance",
                              style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id):
    txt, kb = await _content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@kyc_admin_router.callback_query(F.data == "set:kyc")
async def kyc_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


def _norm_card(raw: str) -> str:
    tr = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return "".join(ch for ch in str(raw or "").translate(tr) if ch.isdigit())


def _level_label(level: int) -> str:
    return {
        0: "سطح ۰ — بدونِ احراز",
        1: "سطح ۱ — کارتِ بانکیِ تأییدشده",
        2: "سطح ۲ — احرازِ کامل",
    }.get(int(level or 0), "سطح نامشخص")


async def _user_manage_view(bot_id: int, user_id: int):
    """متن و کیبوردِ مدیریتِ یک کاربر؛ هیچ داده‌ای را تغییر نمی‌دهد."""
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(
            _BU.id == user_id, _BU.bot_id == bot_id))
        if not u:
            return "❌ کاربرِ موردنظر در این ربات پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[])
        pending = await s.scalar(_sel(_KR).where(
            _KR.bot_id == bot_id, _KR.user_id == u.id,
            _KR.status == "pending").order_by(_KR.id.desc()))

    level = int(u.kyc_level or 0)
    cap = await K.cap_for(bot_id, level)
    next_cap = await K.cap_for(bot_id, 1 if level == 0 else 2)
    cap_text = "بدونِ محدودیت" if cap <= 0 else f"{cap:,} تومان در ۲۴ ساعت"
    next_text = "بدونِ محدودیت" if next_cap <= 0 else f"{next_cap:,} تومان در ۲۴ ساعت"
    uname = f"@{u.username}" if u.username else "ثبت نشده"
    card = _mask(u.kyc_card) if u.kyc_card else "ثبت نشده"
    blocked = "🔒 محدودشده" if u.kyc_blocked else "✅ عادی"
    pending_text = f"⏳ درخواستِ سطح {pending.level} در انتظارِ بررسی" if pending else "✅ درخواستِ در انتظار ندارد"
    text = (
        "🛡 <b>مدیریتِ احرازِ هویتِ کاربر</b>\n\n"
        f"👤 نام: {u.full_name or '—'}\n"
        f"🆔 شناسهٔ تلگرام: <code>{u.telegram_id}</code>\n"
        f"🔗 نام‌کاربری: {uname}\n"
        f"📌 وضعیت حساب: {blocked}\n\n"
        f"🎚 سطح فعلی: <b>{_level_label(level)}</b>\n"
        f"💵 سقفِ فعلی: <b>{cap_text}</b>\n"
        f"📈 سقفِ سطح بعدی: <b>{next_text}</b>\n"
        f"💳 کارتِ تأییدشده: <code>{card}</code>\n"
        f"{pending_text}\n\n"
        "تغییر یا حذف کارت فقط اطلاعات کارتِ فعال را تغییر می‌دهد؛ درخواست‌هایِ تاریخی و رسیدهایِ قبلی دست‌نخورده می‌مانند."
    )
    rows = []
    if u.kyc_card:
        rows.append([
            InlineKeyboardButton(text="✏️ تغییر کارتِ تأییدشده",
                                 callback_data=f"kyc:user:card:{u.id}",
                                 style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🗑 حذف کارت",
                                 callback_data=f"kyc:user:carddel:{u.id}",
                                 style=ButtonStyle.DANGER),
        ])
    # === PATCH_KYC_ADMIN_LEVEL_V1 === تعیینِ دستیِ سطح + قفل/بازکردنِ حساب
    rows.append([InlineKeyboardButton(text="🎚 تعیینِ دستیِ سطحِ کاربر",
                                      callback_data=f"kyc:user:lvl:{u.id}",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=("🔓 رفعِ محدودیتِ حساب" if u.kyc_blocked else "🔒 محدودکردنِ حساب"),
        callback_data=f"kyc:user:blk:{u.id}",
        style=(ButtonStyle.SUCCESS if u.kyc_blocked else ButtonStyle.DANGER))])
    rows.append([InlineKeyboardButton(text="🔎 جست‌وجوی کاربر دیگر",
                                      callback_data="kyc:users",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به احراز هویت",
                                      callback_data="set:kyc",
                                      style=ButtonStyle.DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_user_manage(target, bot_id: int, user_id: int):
    text, kb = await _user_manage_view(bot_id, user_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(text, reply_markup=kb, parse_mode="HTML")


async def _ask_user_lookup(target, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    await state.set_state(KycFSM.user_lookup)
    m = getattr(target, "message", None) or target
    await m.answer(
        "👤 شناسهٔ عددیِ تلگرام یا نام‌کاربریِ کاربر را بفرستید.\n"
        "مثال: <code>123456789</code> یا <code>@username</code>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@kyc_admin_router.callback_query(F.data == "kyc:users")
async def kyc_users_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _ask_user_lookup(cb, bot, state)
    await cb.answer()


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:manage:"))
async def kyc_user_manage(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        user_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("شناسهٔ کاربر نامعتبر است.", show_alert=True)
    await state.clear()
    await _show_user_manage(cb, row.id, user_id)
    await cb.answer()


@kyc_admin_router.message(KycFSM.user_lookup, F.text)
async def kyc_user_lookup_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    tr = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    normalized = raw.translate(tr).strip()
    async with _S() as s:
        if normalized.lstrip("+").isdigit():
            try:
                tg_id = int(normalized.lstrip("+"))
            except ValueError:
                tg_id = 0
            users = (await s.scalars(_sel(_BU).where(
                _BU.bot_id == row.id, _BU.telegram_id == tg_id).limit(10))).all()
        else:
            username = raw.lstrip("@").strip().lower()
            users = (await s.scalars(_sel(_BU).where(
                _BU.bot_id == row.id,
                _func.lower(_BU.username) == username).order_by(_BU.id).limit(10))).all()
    if not users:
        await msg.answer("❌ کاربری با این شناسه یا نام‌کاربری پیدا نشد. دوباره تلاش کنید.")
        return
    await state.clear()
    if len(users) == 1:
        return await _show_user_manage(msg, row.id, users[0].id)
    rows = [[InlineKeyboardButton(
        text=f"{u.full_name or 'کاربر'} — {u.telegram_id}",
        callback_data=f"kyc:user:manage:{u.id}", style=ButtonStyle.PRIMARY)]
        for u in users]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:kyc",
                                      style=ButtonStyle.DANGER)])
    await msg.answer("چند کاربر پیدا شد؛ کاربر موردنظر را انتخاب کنید:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:card:"))
async def kyc_user_card_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        user_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("شناسهٔ کاربر نامعتبر است.", show_alert=True)
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(
            _BU.id == user_id, _BU.bot_id == row.id))
    if not u or not u.kyc_card:
        return await cb.answer("این کاربر کارتِ تأییدشدهٔ فعالی ندارد.", show_alert=True)
    await state.update_data(kyc_admin_user_id=user_id)
    await state.set_state(KycFSM.user_card)
    await cb.message.answer(
        "✏️ شمارهٔ جدیدِ ۱۶ رقمیِ کارتِ تأییدشده را بفرستید.\n"
        "کارتِ قبلی تا زمانِ ثبتِ مقدارِ معتبر تغییر نمی‌کند.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@kyc_admin_router.message(KycFSM.user_card, F.text)
async def kyc_user_card_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    if not row:
        return
    card = _norm_card(msg.text)
    if len(card) != 16:
        return await msg.answer("شمارهٔ کارت باید دقیقاً ۱۶ رقم باشد؛ دوباره ارسال کنید.")
    data = await state.get_data()
    try:
        user_id = int(data.get("kyc_admin_user_id"))
    except (TypeError, ValueError):
        await state.clear()
        return await msg.answer("جلسهٔ تغییر کارت منقضی شد؛ دوباره از پنل شروع کنید.")
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(
            _BU.id == user_id, _BU.bot_id == row.id).with_for_update())
        if not u or not u.kyc_card:
            await state.clear()
            return await msg.answer("کارتِ فعالِ این کاربر پیدا نشد؛ تغییری انجام نشد.")
        old_tail = _mask(u.kyc_card)[-4:]
        u.kyc_card = card
        if not u.kyc_blocked:
            u.kyc_note = None
        tg_id = u.telegram_id
        await s.commit()
    await state.clear()
    await _tell_user(bot, tg_id,
                     "✅ کارتِ احرازِ هویتِ شما توسط مدیریت تغییر کرد.\n"
                     f"چهار رقمِ پایانیِ کارتِ جدید: <code>{card[-4:]}</code>")
    await msg.answer(f"✅ کارت تغییر کرد. کارتِ قبلی با پایانِ {old_tail} جایگزین شد.")
    await _show_user_manage(msg, row.id, user_id)


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:lvl:"))
async def kyc_user_level_ask(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    uid = int(cb.data.rsplit(":", 1)[1])
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if False else ''}{_level_label(n)}",
                                  callback_data=f"kyc:user:setlvl:{uid}:{n}",
                                  style=ButtonStyle.PRIMARY)] for n in (0, 1, 2)]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"kyc:user:manage:{uid}",
                                      style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🎚 <b>سطحِ جدیدِ کاربر را انتخاب کن</b>\n\n"
        "• ارتقا به سطح ۱/۲ بدونِ مدرک هم ممکن است (مثلاً کاربرِ مورد‌اعتماد).\n"
        "• <b>کاهشِ سطح</b> (مثلاً حذفِ سطح ۲) یعنی کاربر باید دوباره احراز کند؛ "
        "به او هم خبر داده می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:setlvl:"))
async def kyc_user_level_set(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, _, uid_s, lvl_s = cb.data.split(":")
    uid, new = int(uid_s), max(0, min(2, int(lvl_s)))
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(_BU.id == uid, _BU.bot_id == row.id).with_for_update())
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        old = int(u.kyc_level or 0)
        tg_id = u.telegram_id
        u.kyc_level = new
        u.kyc_blocked = False
        u.kyc_note = None
        if new == 0:
            u.kyc_card = None   # دوباره باید کارت ثبت و احراز کند
        await s.commit()
    if new < old:
        await _tell_user(bot, tg_id,
                         f"⚠️ سطحِ احرازِ هویتِ حسابِ شما توسط مدیریت به «{_level_label(new)}» تغییر کرد.\n"
                         "برایِ برگشتِ سقفِ قبلی، از «🛡 احرازِ هویت» دوباره احراز را انجام بده.")
    elif new > old:
        await _tell_user(bot, tg_id, f"✅ سطحِ حسابِ شما به «{_level_label(new)}» ارتقا یافت.")
    await cb.answer("سطح تغییر کرد.", show_alert=True)
    await _show_user_manage(cb, row.id, uid)


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:blk:"))
async def kyc_user_block_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    uid = int(cb.data.rsplit(":", 1)[1])
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(_BU.id == uid, _BU.bot_id == row.id).with_for_update())
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        u.kyc_blocked = not bool(u.kyc_blocked)
        u.kyc_note = "محدودسازیِ دستیِ مدیر" if u.kyc_blocked else None
        now_blocked, tg_id = bool(u.kyc_blocked), u.telegram_id
        await s.commit()
    await _tell_user(bot, tg_id, "🔒 حسابِ شما توسط مدیریت محدود شد." if now_blocked
                     else "🔓 محدودیتِ حسابِ شما برداشته شد.")
    await cb.answer("انجام شد.", show_alert=True)
    await _show_user_manage(cb, row.id, uid)


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:carddel:"))
async def kyc_user_card_delete_ask(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        user_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("شناسهٔ کاربر نامعتبر است.", show_alert=True)
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(
            _BU.id == user_id, _BU.bot_id == row.id))
    if not u or not u.kyc_card:
        return await cb.answer("کارتِ فعالی برای حذف وجود ندارد.", show_alert=True)
    level = int(u.kyc_level or 0)
    consequence = (
        "سطح ۱ این کاربر به سطح ۰ برمی‌گردد و سقفِ سطح ۰ اعمال می‌شود."
        if level == 1 else
        "سطح ۲ کاربر حفظ می‌شود و فقط اطلاعات کارت حذف خواهد شد."
        if level >= 2 else
        "فقط اطلاعات کارت حذف خواهد شد."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود",
                              callback_data=f"kyc:user:carddel_yes:{user_id}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ انصراف",
                              callback_data=f"kyc:user:manage:{user_id}",
                              style=ButtonStyle.PRIMARY)],
    ])
    await cb.message.edit_text(
        "⚠️ <b>تأیید حذف کارتِ احراز‌شده</b>\n\n"
        f"کارتِ <code>{_mask(u.kyc_card)}</code> حذف شود؟\n"
        f"{consequence}\n\n"
        "درخواست‌ها و تراکنش‌هایِ قبلی تغییر نمی‌کنند.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@kyc_admin_router.callback_query(F.data.startswith("kyc:user:carddel_yes:"))
async def kyc_user_card_delete_yes(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        user_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        return await cb.answer("شناسهٔ کاربر نامعتبر است.", show_alert=True)
    async with _S() as s:
        u = await s.scalar(_sel(_BU).where(
            _BU.id == user_id, _BU.bot_id == row.id).with_for_update())
        if not u or not u.kyc_card:
            return await cb.answer("کارت قبلاً حذف شده یا وجود ندارد.", show_alert=True)
        level_before = int(u.kyc_level or 0)
        tg_id = u.telegram_id
        u.kyc_card = None
        if level_before == 1:
            u.kyc_level = 0
            u.kyc_blocked = False
            u.kyc_note = None
        await s.commit()
    if level_before == 1:
        user_text = ("⚠️ کارتِ احرازِ هویتِ شما توسط مدیریت حذف شد و سطحِ حسابِ شما به سطح ۰ برگشت.\n"
                     "برای استفاده از سقفِ بالاتر، دوباره احرازِ سطح ۱ را انجام دهید.")
    else:
        user_text = "✅ اطلاعات کارتِ احرازِ هویتِ شما حذف شد؛ سطحِ فعلیِ حساب حفظ شده است."
    await _tell_user(bot, tg_id, user_text)
    await cb.answer("کارت حذف شد.", show_alert=True)
    await _show_user_manage(cb, row.id, user_id)


@kyc_admin_router.callback_query(F.data == "kyc:toggle")
async def kyc_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, "kyc_enabled", "0")) == "1"
    await set_setting(row.id, "kyc_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


# === PATCH_KYC_CARD_CHANGE_V1 ===
@kyc_admin_router.callback_query(F.data == "kyc:window")
async def kyc_window_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "kyc_window", "rolling")
    await set_setting(row.id, "kyc_window", "rolling" if cur == "calendar" else "calendar")
    await cb.answer("✅ تغییر کرد")
    await _show(cb, row.id)


@kyc_admin_router.callback_query(F.data == "kyc:ccpol")
async def kyc_ccpol_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mx, days = await K.card_change_policy(row.id)
    await state.set_state(KycFSM.cc_policy)
    await cb.message.answer(
        f"🔄 <b>محدودیتِ تغییرِ کارت</b>\nفعلاً: {mx} بار در {days} روز\n\n"
        "دو عدد بفرست: <b>تعدادِ دفعات</b> و <b>تعدادِ روز</b>، با فاصله.\n"
        "مثال: <code>1 15</code> ← هر کاربر در هر ۱۵ روز یک‌بار.\n"
        "برایِ ممنوع‌کردنِ تغییرِ کارت: <code>0 15</code>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@kyc_admin_router.message(KycFSM.cc_policy, F.text)
async def kyc_ccpol_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int, set_setting
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    parts = (msg.text or "").replace("،", " ").replace("/", " ").split()
    nums = [fa_to_int(p) for p in parts[:2]]
    if len(nums) != 2 or any(n is None or n < 0 for n in nums) or nums[1] < 1:
        return await msg.answer("فرمتِ نامعتبر. مثال: 1 15")
    await set_setting(row.id, "kyc_card_change_max", str(nums[0]))
    await set_setting(row.id, "kyc_card_change_days", str(nums[1]))
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


@kyc_admin_router.callback_query(F.data.in_({"kyc:cap0", "kyc:cap1"}))
async def kyc_cap_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    lvl = 0 if cb.data.endswith("0") else 1
    await state.set_state(KycFSM.cap0 if lvl == 0 else KycFSM.cap1)
    await cb.message.answer(
        f"سقفِ شارژِ روزانهٔ سطح {lvl} چند تومان باشد؟ (فقط عدد)\n"
        "عددِ ۰ یعنی بدونِ محدودیت.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@kyc_admin_router.message(KycFSM.cap0, F.text)
@kyc_admin_router.message(KycFSM.cap1, F.text)
async def kyc_cap_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int, set_setting
    row = await _owner_ok(msg, bot)
    cur = await state.get_state()
    await state.clear()
    if not row:
        return
    v = fa_to_int(msg.text)
    if v is None or v < 0:
        return await msg.answer("عددِ نامعتبر.")
    key = "kyc_cap_l0" if cur == KycFSM.cap0.state else "kyc_cap_l1"
    await set_setting(row.id, key, str(v))
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


@kyc_admin_router.callback_query(F.data == "kyc:media")
async def kyc_media(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    rows = [[InlineKeyboardButton(text=K.media_label(k),
                                  callback_data=f"kyc:media:{k}")]
            for k in ("video_note", "video", "photo", "any")]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:kyc",
                                      style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🎬 کاربر برایِ سطح ۲ چه نوع فایلی بفرستد؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@kyc_admin_router.callback_query(F.data.startswith("kyc:media:"))
async def kyc_media_set(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "kyc_l2_media", cb.data.rsplit(":", 1)[1])
    await cb.answer("ذخیره شد.")
    await _show(cb, row.id)


@kyc_admin_router.callback_query(F.data == "kyc:l2text")
async def kyc_l2text_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "kyc_l2_text", "") or _DEF_L2_TEXT
    await state.set_state(KycFSM.l2text)
    await cb.message.answer(
        "متنِ دستورالعملِ سطح ۲ را بفرست (همان چیزی که کاربر می‌بیند).\n\n"
        f"<b>متنِ فعلی:</b>\n{cur}",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@kyc_admin_router.message(KycFSM.l2text, F.text)
async def kyc_l2text_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    await set_setting(row.id, "kyc_l2_text", (msg.text or "")[:3000])
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


# ═════════════ صفِ بررسیِ درخواست‌ها ═════════════
from sqlalchemy import select as _sel

from config import Session as _S
from models import BotUser as _BU, KycRequest as _KR


def _mask(card):
    c = "".join(ch for ch in str(card or "") if ch.isdigit())
    return f"{c[:6]}******{c[-4:]}" if len(c) == 16 else (c or "—")


@kyc_admin_router.callback_query(F.data.startswith("kyc:pend:"))
async def kyc_pend(cb: CallbackQuery, bot: Bot):
    """درخواست‌هایِ در انتظار، یکی‌یکی با دکمه‌هایِ تأیید/رد."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        idx = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        idx = 0

    async with _S() as s:
        reqs = (await s.scalars(_sel(_KR).where(
            _KR.bot_id == row.id, _KR.status == "pending"
        ).order_by(_KR.id))).all()

    if not reqs:
        await cb.answer("درخواستِ در انتظاری نیست.", show_alert=True)
        return
    idx = max(0, min(idx, len(reqs) - 1))
    r = reqs[idx]

    async with _S() as s:
        u = await s.get(_BU, r.user_id)

    uname = f"@{u.username}" if (u and u.username) else "—"
    cap = "\n".join([
        (f"🔄 <b>درخواستِ تغییرِ کارت</b>  ({idx + 1} از {len(reqs)})"
         if getattr(r, "kind", None) == "card_change"
         else f"🛡 <b>درخواستِ احرازِ سطح {r.level}</b>  ({idx + 1} از {len(reqs)})"),
        "",
        f"👤 {(u.full_name if u else None) or '—'}",
        f"🆔 <code>{u.telegram_id if u else '—'}</code>",
        f"🔗 {uname}",
    ])
    if r.level == 1:
        cap += f"\n💳 کارت: <code>{_mask(r.card)}</code>"

    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی",
                                        callback_data=f"kyc:pend:{idx - 1}"))
    if idx < len(reqs) - 1:
        nav.append(InlineKeyboardButton(text="بعدی ➡️",
                                        callback_data=f"kyc:pend:{idx + 1}"))
    rows = [
        [InlineKeyboardButton(text="✅ تأیید", callback_data=f"kyc:ok:{r.id}",
                              style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="❌ رد", callback_data=f"kyc:no:{r.id}",
                              style=ButtonStyle.DANGER)],
    ]
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:kyc",
                                      style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        if r.file_type == "photo":
            await cb.message.answer_photo(r.file_id, caption=cap,
                                          reply_markup=kb, parse_mode="HTML")
        elif r.file_type == "video":
            await cb.message.answer_video(r.file_id, caption=cap,
                                          reply_markup=kb, parse_mode="HTML")
        elif r.file_type == "video_note":
            await cb.message.answer_video_note(r.file_id)
            await cb.message.answer(cap, reply_markup=kb, parse_mode="HTML")
        else:
            await cb.message.answer(cap, reply_markup=kb, parse_mode="HTML")
    except Exception:
        logger.exception("kyc: نمایشِ فایلِ درخواست ناموفق بود")
        await cb.message.answer(cap + "\n\n⚠️ فایل قابلِ نمایش نبود.",
                                reply_markup=kb, parse_mode="HTML")
    await cb.answer()


async def _tell_user(bot: Bot, tg_id: int, text: str) -> None:
    try:
        await bot.send_message(tg_id, text, parse_mode="HTML")
    except Exception:
        logger.info("kyc: اطلاع به کاربر ممکن نشد (tg=%s)", tg_id)


@kyc_admin_router.callback_query(F.data.startswith("kyc:ok:"))
async def kyc_approve(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    rid = int(cb.data.rsplit(":", 1)[1])
    async with _S() as s:
        r = await s.get(_KR, rid)
        if not r or r.bot_id != row.id or r.status != "pending":
            return await cb.answer("این درخواست دیگر در انتظار نیست.",
                                   show_alert=True)
        u = await s.get(_BU, r.user_id)
        if not u:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        r.status = "approved"
        u.kyc_level = max(int(u.kyc_level or 0), int(r.level))
        if getattr(r, "kind", None) != "card_change":   # PATCH_KYC_CARD_CHANGE_V1: تغییرِ کارت قفلِ حساب را باز نمی‌کند
            u.kyc_blocked = False
            u.kyc_note = None
        if r.level == 1 and r.card:
            u.kyc_card = r.card
        tg_id = u.telegram_id
        lvl = int(u.kyc_level)
        await s.commit()

    cap = await K.cap_for(row.id, lvl)
    msg = f"✅ احرازِ هویتِ شما تأیید شد.\n\nسطحِ فعلی: <b>{lvl}</b>\n"
    msg += (f"سقفِ شارژِ روزانه: <b>{cap:,}</b> تومان"
            if cap > 0 else "شارژِ شما دیگر محدودیتی ندارد.")
    if lvl == 1:
        msg += "\n\n⚠️ از این پس پرداخت فقط با همان کارتِ ثبت‌شده ممکن است."
    await _tell_user(bot, tg_id, msg)

    await cb.answer("تأیید شد.", show_alert=True)
    await _show(cb, row.id)


# === PATCH_KYC_REJECT_REASON_V1 ===
# گزارشِ فدایی: «برای رد کردنِ احراز هویت دلیل بذار بتونیم براش دلیل بنویسیم.» قبلاً «❌ رد» یک‌کلیکی و بدونِ دلیل
# بود. حالا اول دلیل پرسیده می‌شود (یا «بدونِ دلیل»)؛ دلیل در ستونِ admin_note ذخیره و برایِ کاربر فرستاده می‌شود
# و در صفحهٔ احرازِ خودِ کاربر هم تا درخواستِ بعدی دیده می‌شود.
_REASON_MAX = 200
_CANCEL_WORDS = {"انصراف", "لغو", "/cancel", "❌ لغو", "❌ انصراف"}


async def _do_reject(bot: Bot, row, rid: int, reason: str) -> str:
    """درخواست را رد می‌کند و به کاربر خبر می‌دهد. نتیجه: ok | gone."""
    import html as _html
    async with _S() as s:
        r = await s.get(_KR, rid)
        if not r or r.bot_id != row.id or r.status != "pending":
            return "gone"
        u = await s.get(_BU, r.user_id)
        r.status = "rejected"
        r.admin_note = (reason or None)
        tg_id = u.telegram_id if u else None
        lvl = r.level
        await s.commit()
    if tg_id:
        why = f"\n\n📝 <b>دلیل:</b> {_html.escape(reason)}" if reason else ""
        await _tell_user(bot, tg_id, (
            f"❌ درخواستِ احرازِ سطح {lvl} شما تأیید نشد.{why}\n\n"
            "لطفاً مواردِ خواسته‌شده را با دقت رعایت کن و دوباره اقدام کن. "
            "اگر سؤالی داری با پشتیبانی در تماس باش."))
    return "ok"


@kyc_admin_router.callback_query(F.data.startswith("kyc:no:"))
async def kyc_reject(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    rid = int(cb.data.rsplit(":", 1)[1])
    async with _S() as s:
        r = await s.get(_KR, rid)
        if not r or r.bot_id != row.id or r.status != "pending":
            return await cb.answer("این درخواست دیگر در انتظار نیست.",
                                   show_alert=True)
    await state.set_state(KycFSM.reject_reason)
    await state.update_data(kyc_reject_rid=rid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 رد بدونِ دلیل", callback_data=f"kyc:nr:{rid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="↩️ انصراف", callback_data="kyc:nc", style=ButtonStyle.PRIMARY)],
    ])
    await cb.message.answer(
        "✍️ <b>دلیلِ ردِ این درخواست را بنویس.</b>\n\n"
        "همین متن برایِ کاربر فرستاده می‌شود (حداکثر ۲۰۰ حرف)؛ مثلاً: «عکسِ کارت ناخوانا بود» یا "
        "«نامِ روی کارت با حسابِ شما نمی‌خواند».\n"
        "اگر نمی‌خواهی دلیل بنویسی «رد بدونِ دلیل» را بزن.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@kyc_admin_router.callback_query(F.data.startswith("kyc:nr:"))
async def kyc_reject_noreason(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    rid = int(cb.data.rsplit(":", 1)[1])
    res = await _do_reject(bot, row, rid, "")
    await cb.answer("رد شد." if res == "ok" else "این درخواست دیگر در انتظار نیست.", show_alert=True)
    await _show(cb, row.id)


@kyc_admin_router.callback_query(F.data == "kyc:nc")
async def kyc_reject_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        logger.debug("kyc: بستنِ پیامِ دلیل ممکن نشد")
    await cb.answer("منصرف شدی؛ درخواست هنوز در انتظار است.")


@kyc_admin_router.message(KycFSM.reject_reason, F.text)
async def kyc_reject_reason_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    data = await state.get_data()
    await state.clear()
    if not row:
        return
    reason = (msg.text or "").strip()
    if reason in _CANCEL_WORDS:
        return await msg.answer("منصرف شدی؛ درخواست هنوز در انتظار است.")
    rid = int(data.get("kyc_reject_rid") or 0)
    res = await _do_reject(bot, row, rid, reason[:_REASON_MAX])
    await msg.answer("✅ رد شد و دلیل برایِ کاربر فرستاده شد." if res == "ok"
                     else "این درخواست دیگر در انتظار نیست.")
    await _show(msg, row.id)
