"""
receipt_channel.py — «کانالِ تاییدِ رسید» برای ربات‌های مستأجر.
(PATCH_RECEIPT_CHANNEL_V1)

هدف: ادمینِ یک ربات مستأجر می‌تونه یه کانال (که خودِ ربات توش ادمینه) وصل
کنه؛ با فعال‌کردنِ این قابلیت، از اون به بعد رسیدهای پرداختِ کارت‌به‌کارت
(هم خریدِ کانفیگ، هم شارژِ کیف‌پول) به‌جایِ پیامِ خصوصیِ جدا به تک‌تکِ
ادمین‌ها، فقط یک پیام (با دکمه‌هایِ تایید/رد) توی همون کانال پست می‌شه —
تا همهٔ ادمین‌هایی که عضوِ کانالن با هم ببینن و هرکدوم زودتر رسیدگی کنه.

نکتهٔ امنیتی: زدنِ دکمه‌هایِ تایید/رد همچنان از همون تابعِ همیشگیِ
`is_owner_or_admin` رد می‌شه (رو تعریف شده تویِ tenant/__init__.py) —
یعنی فقط مالکِ ربات یا ادمین‌هایِ پشتیبانِ ثبت‌شده می‌تونن تصمیم بگیرن،
نه هر کسی که توی اون کانال ادمینه. وصل‌کردنِ کانال فقط مسیرِ رسیدنِ
پیام رو عوض می‌کنه، نه اینکه چه کسی مجازه تایید/رد کنه.

اگه کانال وصل نباشه یا فعال نباشه (یا ارسال بهش به هر دلیلی خطا بده)،
رفتارِ قبلی (DM به مالک + ادمین‌هایِ پشتیبانی — همون `_notify_admins`)
دست‌نخورده باقی می‌مونه؛ این یک قابلیتِ جانبیه و نباید هیچ‌وقت خودِ
فرآیندِ رسیدگی به رسید رو مختل کنه.
"""
from __future__ import annotations

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

receipt_channel_router = Router()


class ReceiptChannelFSM(StatesGroup):
    setchat = State()


# ═══════════════════════ پنلِ مدیریت: اتصال و تنظیمات ═══════════════════════

async def receipt_channel_menu_text_kb(bot_id: int):
    from tenant import get_setting
    channel_id = await get_setting(bot_id, "receipt_channel_id", "")
    channel_title = await get_setting(bot_id, "receipt_channel_title", "")
    enabled = await get_setting(bot_id, "receipt_channel_enabled", "0") == "1"

    conn_line = (f"📢 کانال: <b>{channel_title or channel_id}</b>" if channel_id
                else "📢 کانال: ⚠️ وصل نشده")
    conn_btn_txt = "✏️ تغییرِ کانال" if channel_id else "🔗 اتصالِ کانال"
    rows = [[InlineKeyboardButton(text=conn_btn_txt, callback_data="rcpt_ch:setchat", style=ButtonStyle.PRIMARY)]]
    if channel_id:
        rows.append([InlineKeyboardButton(
            text=("🟢 ارسالِ رسیدها به کانال: فعال" if enabled else "🔴 ارسالِ رسیدها به کانال: غیرفعال"),
            callback_data="rcpt_ch:toggle", style=ButtonStyle.DANGER)])
        rows.append([InlineKeyboardButton(text="🗑 قطعِ اتصالِ کانال", callback_data="rcpt_ch:disconnect", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])

    status_line = ""
    if channel_id:
        status_line = ("\n\n✅ فعاله: رسیدهای پرداخت (خریدِ کانفیگ و شارژِ کیف‌پول) به این کانال ارسال می‌شن."
                       if enabled else
                       "\n\n⏸ کانال وصله ولی غیرفعاله؛ رسیدها فعلاً طبقِ روالِ قبلی برای ادمین‌ها پیام خصوصی می‌شن.")
    text = (
        "🧾 <b>کانالِ تاییدِ رسید</b>\n\n"
        f"{conn_line}{status_line}\n\n"
        "با وصل‌کردنِ یه کانال و فعال‌کردنِ این قابلیت، از این به بعد هر رسیدِ "
        "پرداختِ کارت‌به‌کارتی (خریدِ کانفیگ یا شارژِ کیف‌پول) به‌جایِ پیامِ خصوصیِ "
        "جدا به هر ادمین، یک‌بار توی همین کانال پست می‌شه — با دکمه‌هایِ "
        "«تایید» / «رد» — تا هر ادمینِ عضوِ کانال زودتر رسیدگی کنه.\n\n"
        "⚠️ ربات باید توی اون کانال <b>ادمین</b> باشه (با دسترسیِ ارسالِ پیام)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@receipt_channel_router.callback_query(F.data == "tadm:receiptchannel")
async def receipt_channel_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    text, kb = await receipt_channel_menu_text_kb(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@receipt_channel_router.callback_query(F.data == "rcpt_ch:toggle")
async def receipt_channel_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    channel_id = await get_setting(row.id, "receipt_channel_id", "")
    if not channel_id:
        await cb.answer("⚠️ اول یه کانال وصل کن.", show_alert=True)
        return
    cur = await get_setting(row.id, "receipt_channel_enabled", "0")
    await set_setting(row.id, "receipt_channel_enabled", "0" if cur == "1" else "1")
    text, kb = await receipt_channel_menu_text_kb(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("✅ ذخیره شد")


@receipt_channel_router.callback_query(F.data == "rcpt_ch:disconnect")
async def receipt_channel_disconnect(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await set_setting(row.id, "receipt_channel_id", "")
    await set_setting(row.id, "receipt_channel_title", "")
    await set_setting(row.id, "receipt_channel_enabled", "0")
    text, kb = await receipt_channel_menu_text_kb(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("🗑 قطع شد")


@receipt_channel_router.callback_query(F.data == "rcpt_ch:setchat")
async def receipt_channel_setchat_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    await state.set_state(ReceiptChannelFSM.setchat)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔗 <b>اتصالِ کانالِ تاییدِ رسید</b>\n\n"
        "یه پیام از همون کانال برام فوروارد کن (یا یوزرنیمش رو با @ بفرست).\n\n"
        "⚠️ قبلش مطمئن شو ربات توی اون کانال <b>ادمین</b>ه (با دسترسیِ ارسالِ پیام).",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@receipt_channel_router.message(ReceiptChannelFSM.setchat)
async def receipt_channel_setchat_recv(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, main_kb_or_remove, is_owner_or_admin

    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    if (msg.text or "").strip() in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb)
        return

    # === PATCH_RECEIPT_CHANNEL_FORWARD_ORIGIN_V1 ===
    target = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target = fwd_origin.chat.id
    elif msg.forward_from_chat:
        target = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        if raw.startswith("@") and len(raw) > 1:
            target = raw
        elif raw.lstrip("-").isdigit():
            target = int(raw)

    if target is None:
        await msg.answer("❌ این یک فورواردِ معتبر یا یوزرنیم نبود. یه پیام از همون کانال "
                         "فوروارد کن، یا یوزرنیمش رو با @ بفرست، یا «❌ لغو» رو بزن.")
        return

    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await msg.answer(f"❌ به این کانال دسترسی ندارم: {e}")
        return
    if chat.type != "channel":
        await msg.answer(f"❌ این یه «{chat.type}»ه، نه کانال. یه پیام از خودِ کانال فوروارد کن.")
        return
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        await msg.answer(f"❌ نتونستم عضویتِ ربات توی این کانال رو چک کنم: {e}")
        return
    if member.status not in ("administrator", "creator"):
        await msg.answer("❌ ربات توی این کانال ادمین نیست. اول ربات رو با دسترسیِ ادمین "
                         "(حتماً با دسترسیِ «ارسالِ پیام») به کانال اضافه کن، بعد دوباره امتحان کن.")
        return
    can_post = getattr(member, "can_post_messages", True)
    if can_post is False:
        await msg.answer("❌ ربات توی این کانال ادمینه ولی دسترسیِ «ارسالِ پیام» نداره. "
                         "این دسترسی رو از تنظیماتِ ادمینِ کانال بهش بده و دوباره امتحان کن.")
        return

    from tenant import set_setting
    await set_setting(row.id, "receipt_channel_id", str(chat.id))
    await set_setting(row.id, "receipt_channel_title", chat.title or chat.username or str(chat.id))
    # وصل‌کردنِ کانال خودش به‌معنیِ فعال‌سازی هم هست — کاربر می‌تونه بعداً از دکمهٔ
    # «فعال/غیرفعال» خاموشش کنه بدون قطعِ اتصال.
    await set_setting(row.id, "receipt_channel_enabled", "1")
    await state.clear()
    kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
    await msg.answer(f"✅ «{chat.title}» به‌عنوانِ کانالِ تاییدِ رسید وصل و فعال شد.", reply_markup=kb)
    text, ikb = await receipt_channel_menu_text_kb(row.id)
    await msg.answer(text, reply_markup=ikb)


# ═══════════════════════════════════════════════════════════════════════
#  PATCH_SMM_ORDER_CHANNEL_V1 — کانالِ انجامِ سفارشاتِ خدماتِ مجازی
# ═══════════════════════════════════════════════════════════════════════
# درخواستِ ادمین‌ها: سفارش‌ها در یک کانال بیایند تا تیم ببیند چه سفارشی
# ثبت شده و برایِ چه لینکی — نه اینکه هرکس جدا در دی‌ام دنبالش بگردد.
#
# عمداً *دو* کانالِ جدا: سفارشِ خودکار (که پنل خودش انجام می‌دهد و فقط
# جنبهٔ اطلاع دارد) با سفارشِ دستی (که باید کسی برود انجامش دهد و تایید
# کند) دو جریانِ کاریِ متفاوت‌اند؛ ریختنشان در یک کانال یعنی گم‌شدنِ
# کارهایِ دستی لایِ انبوهِ اطلاعیه‌هایِ خودکار. اگر ادمین یکی‌شان را
# وصل نکند، همان مسیرِ قبلی (دی‌ام به ادمین‌ها) دست‌نخورده کار می‌کند.

_SMM_CH_KINDS = {
    "auto":   ("🤖 سفارشاتِ خودکار (پنل)", "smm_order_channel"),
    "manual": ("✋ سفارشاتِ دستی", "smm_manual_channel"),
}


class SmmChannelFSM(StatesGroup):
    setchat = State()


async def smm_order_channel_text_kb(bot_id: int):
    from tenant import get_setting
    rows, lines = [], []
    for kind, (label, key) in _SMM_CH_KINDS.items():
        cid = await get_setting(bot_id, f"{key}_id", "")
        title = await get_setting(bot_id, f"{key}_title", "")
        on = await get_setting(bot_id, f"{key}_enabled", "0") == "1"
        mark = "🟢" if (cid and on) else ("⏸" if cid else "⚠️")
        lines.append(f"{mark} <b>{label}</b>\n"
                     f"    {title or cid or 'وصل نشده'}")
        rows.append([InlineKeyboardButton(
            text=("✏️ تغییرِ کانالِ " + label if cid else "🔗 اتصالِ " + label),
            callback_data=f"smm_ch:set:{kind}", style=ButtonStyle.PRIMARY)])
        if cid:
            rows.append([
                InlineKeyboardButton(
                    text=("🟢 فعال" if on else "🔴 غیرفعال"),
                    callback_data=f"smm_ch:toggle:{kind}",
                    style=ButtonStyle.SUCCESS if on else ButtonStyle.DANGER),
                InlineKeyboardButton(text="🗑 قطعِ اتصال",
                                     callback_data=f"smm_ch:off:{kind}",
                                     style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance",
                                      style=ButtonStyle.DANGER)])
    text = (
        "🛰 <b>کانالِ انجامِ سفارشات</b>\n\n"
        + "\n".join(lines) + "\n\n"
        "هر سفارشی که ثبت شود، با مشخصاتِ کامل در کانال پست می‌شود: "
        "نامِ سرویس، <b>لینکِ هدف</b>، تعداد، مبلغ و مشخصاتِ خریدار.\n\n"
        "چرا دو کانالِ جدا: سفارشِ خودکار را خودِ پنل انجام می‌دهد و فقط "
        "برایِ اطلاع است؛ سفارشِ دستی باید کسی برود انجامش دهد. اگر در یک "
        "کانال بریزند، کارهایِ دستی لایِ اطلاعیه‌ها گم می‌شوند.\n\n"
        "⚠️ ربات باید در آن کانال <b>ادمین</b> باشد (با دسترسیِ ارسالِ پیام)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@receipt_channel_router.callback_query(F.data == "tadm:smmorderchannel")
async def smm_order_channel_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    text, kb = await smm_order_channel_text_kb(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@receipt_channel_router.callback_query(F.data.startswith("smm_ch:toggle:"))
async def smm_order_channel_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    kind = cb.data.split(":")[2]
    key = _SMM_CH_KINDS[kind][1]
    cur = await get_setting(row.id, f"{key}_enabled", "0") == "1"
    await set_setting(row.id, f"{key}_enabled", "0" if cur else "1")
    await cb.answer("غیرفعال شد" if cur else "فعال شد")
    text, kb = await smm_order_channel_text_kb(row.id)
    await cb.message.edit_text(text, reply_markup=kb)


@receipt_channel_router.callback_query(F.data.startswith("smm_ch:off:"))
async def smm_order_channel_disconnect(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    kind = cb.data.split(":")[2]
    key = _SMM_CH_KINDS[kind][1]
    for suffix in ("_id", "_title", "_enabled"):
        await set_setting(row.id, f"{key}{suffix}", "")
    await cb.answer("قطع شد")
    text, kb = await smm_order_channel_text_kb(row.id)
    await cb.message.edit_text(text, reply_markup=kb)


@receipt_channel_router.callback_query(F.data.startswith("smm_ch:set:"))
async def smm_order_channel_set_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer()
        return
    kind = cb.data.split(":")[2]
    await state.set_state(SmmChannelFSM.setchat)
    await state.update_data(smm_ch_kind=kind)
    await cb.answer()
    await cb.message.answer(
        f"🔗 <b>اتصالِ کانالِ {_SMM_CH_KINDS[kind][0]}</b>\n\n"
        "یک پیام از همان کانال برایم فوروارد کن (یا یوزرنیمش را با @ بفرست).\n\n"
        "⚠️ قبلش مطمئن شو ربات در آن کانال <b>ادمین</b> است.",
        reply_markup=await smart_cancel_kb(bot))


@receipt_channel_router.message(SmmChannelFSM.setchat)
async def smm_order_channel_set_recv(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, main_kb_or_remove, set_setting, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        await state.clear()
        return
    if (msg.text or "").strip() in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.",
                         reply_markup=await main_kb_or_remove(row.id, row.owner_id,
                                                              msg.from_user.id))
        return
    kind = (await state.get_data()).get("smm_ch_kind", "auto")
    key = _SMM_CH_KINDS[kind][1]

    # همان منطقِ کانالِ رسید — فورواردِ کانال، یوزرنیم، یا آیدیِ عددی
    target = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target = fwd_origin.chat.id
    elif msg.forward_from_chat:
        target = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        if raw.startswith("@") and len(raw) > 1:
            target = raw
        elif raw.lstrip("-").isdigit():
            target = int(raw)
    if target is None:
        await msg.answer("❌ فورواردِ معتبر یا یوزرنیم نبود. دوباره امتحان کن یا «❌ لغو» بزن.")
        return
    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await msg.answer(f"❌ به این کانال دسترسی ندارم: {e}")
        return
    if chat.type != "channel":
        await msg.answer(f"❌ این یک «{chat.type}» است، نه کانال.")
        return
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        await msg.answer(f"❌ عضویتِ ربات را نتوانستم بررسی کنم: {e}")
        return
    if member.status not in ("administrator", "creator"):
        await msg.answer("❌ ربات در این کانال ادمین نیست.")
        return
    if getattr(member, "can_post_messages", True) is False:
        await msg.answer("❌ ربات دسترسیِ «ارسالِ پیام» ندارد.")
        return

    await set_setting(row.id, f"{key}_id", str(chat.id))
    await set_setting(row.id, f"{key}_title", chat.title or chat.username or str(chat.id))
    await set_setting(row.id, f"{key}_enabled", "1")
    await state.clear()
    await msg.answer(
        f"✅ «{chat.title}» به‌عنوانِ کانالِ {_SMM_CH_KINDS[kind][0]} وصل و فعال شد.",
        reply_markup=await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id))
    text, ikb = await smm_order_channel_text_kb(row.id)
    await msg.answer(text, reply_markup=ikb)


# ═══════════════ ارسالِ سفارش به کانال ═══════════════

async def post_smm_order(bot, bot_id: int, *, manual: bool, service_name: str,
                         link: str, quantity, price, user=None, order_id=None,
                         status: str = "", note: str = "", pk: int | None = None) -> bool:
    """پستِ یک سفارش در کانالِ مربوطه. خروجی: آیا ارسال شد.

    هیچ‌وقت استثنا بیرون نمی‌دهد — ارسال‌نشدنِ اعلان نباید جلویِ ثبتِ
    سفارشِ کاربر را بگیرد.

    === PATCH_SMM_CHANNEL_APPROVE_BUTTONS_V1 ===
    `pk`: شناسهٔ عددیِ واقعیِ SmmOrder برایِ ساختنِ دکمه‌هایِ تأیید/رد.
    """
    from tenant import get_setting
    key = _SMM_CH_KINDS["manual" if manual else "auto"][1]
    try:
        chat = (await get_setting(bot_id, f"{key}_id", "")).strip()
        if not chat or await get_setting(bot_id, f"{key}_enabled", "0") != "1":
            return False
        import html as _h
        who = "—"
        if user is not None:
            uname = getattr(user, "username", None)
            who = (f"@{uname}" if uname else "") or str(getattr(user, "telegram_id", "") or "")
            full = getattr(user, "full_name", None)
            if full:
                who = f"{_h.escape(str(full))} ({who})" if who else _h.escape(str(full))
        head = "✋ <b>سفارشِ دستی — نیاز به انجام</b>" if manual \
            else "🤖 <b>سفارشِ خودکار ثبت شد</b>"
        lines = [head, ""]
        if order_id:
            lines.append(f"🔢 شمارهٔ سفارش: <code>{order_id}</code>")
        lines.append(f"🛰 سرویس: <b>{_h.escape(str(service_name or '—'))}</b>")
        # لینکِ هدف مهم‌ترین بخش است — داخلِ code تا کامل و قابلِ کپی بماند
        lines.append(f"🔗 لینکِ هدف:\n<code>{_h.escape(str(link or '—'))}</code>")
        lines.append(f"🔢 تعداد: <b>{int(quantity or 0):,}</b>")
        try:
            lines.append(f"💰 مبلغ: <b>{int(float(price or 0)):,}</b> تومان")
        except (TypeError, ValueError):
            pass
        lines.append(f"👤 خریدار: {who}")
        if status:
            lines.append(f"📊 وضعیت: {_h.escape(str(status))}")
        if note:
            lines.append(f"📝 {_h.escape(str(note))}")
        kb = None
        if manual and pk and status == "در انتظارِ انجام و تایید":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"✅ تأیید #{pk}", callback_data=f"smm:approve:{pk}"),
                InlineKeyboardButton(text=f"❌ رد #{pk}", callback_data=f"smm:reject:{pk}"),
            ]])
        await bot.send_message(chat, "\n".join(lines), reply_markup=kb)
        return True
    except Exception:
        logger.exception("ارسالِ سفارشِ SMM به کانال ناموفق بود (bot_id=%s)", bot_id)
        return False
