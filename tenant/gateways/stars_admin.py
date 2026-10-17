"""
stars_admin.py — هندلرهای aiogram مربوط به شارژ کیف پول با «استارز تلگرام» (Telegram Stars):
  • تنظیمات ادمین (فعال/غیرفعال، نرخ تومان به ازای هر استارز)
  • جریان شارژ کیف پول کاربر (دکمهٔ اینلاین + نسخهٔ کیبورد-ساده + ارسال فاکتور)

نکتهٔ مهم دربارهٔ کوپلینگ: تلگرام برای هر نوع پرداخت با Stars (چه شارژ کیف
پول، چه خرید مستقیم از فروشگاه) از همان دو رویدادِ سراسری استفاده می‌کند:
``pre_checkout_query`` و ``F.successful_payment``. چون این دو رویداد باید
بینِ حوزه‌های مختلف (کیف پول اینجا، فروشگاه در tenant/__init__.py) تقسیم
شوند، خودِ dispatcherِ ``stars_successful_payment`` در tenant/__init__.py
می‌ماند و بر اساس پیشوندِ payload به این ماژول یا به هندلرِ فروشگاه ارجاع
می‌دهد. اینجا فقط شاخهٔ «شارژ کیف پول» است.
"""
from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler  # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, ReplyKeyboardRemove,
)

from config import Session
from models import BotUser, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

stars_admin_router = Router()


class StarsFSM(StatesGroup):
    amount = State()        # شارژ کاربر: انتظارِ مبلغ به تومان
    rate_edit = State()      # تنظیم ادمین: ویرایش نرخ تومان به ازای هر استارز
    bot_topup = State()      # PATCH_BOT_STARS_TOPUP_V1: تعدادِ استارز برایِ شارژِ موجودیِ خودِ ربات
    cut_edit = State()       # تنظیم ادمین: درصدِ کسر از نرخِ خودکار


# ═══════════════════════ شارژ کیف پول کاربر ═══════════════════════

async def wallet_stars_text(msg: Message, bot: Bot, state: FSMContext):
    """نسخهٔ حالتِ کیبوردیِ ساده برای شروع شارژ با استارز."""
    from tenant import get_bot_row, get_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return
    if await get_setting(row.id, "stars_enabled", "0") != "1":
        await msg.answer("⭐ پرداخت با استارز تلگرام هنوز توسط مدیر ربات فعال نشده است.")
        return
    await state.set_state(StarsFSM.amount)
    rate = str(await _stars_rate_value(row.id))  # PATCH_AUTO_RATE_STARS_V1
    await msg.answer(
        "⭐ <b>شارژ با استارز تلگرام</b>\n\n"
        f"نرخ فعلی: هر ⭐ = {int(float(rate)):,} تومان\n\n"
        "مبلغی که می‌خوای کیف پولت شارژ بشه رو به <b>تومان</b> بفرست (فقط عدد):",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@stars_admin_router.callback_query(F.data == "w:stars")
async def w_stars(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "stars_enabled", "0") != "1":
        await cb.answer("⭐ پرداخت با استارز تلگرام هنوز توسط مدیر ربات فعال نشده است.", show_alert=True)
        return
    await state.set_state(StarsFSM.amount)
    rate = str(await _stars_rate_value(row.id))  # PATCH_AUTO_RATE_STARS_V1
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if await get_setting(row.id, "menu_mode", "reply") == "inline":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ بازگشت به کیف پول", callback_data="w:back", style=ButtonStyle.DANGER)
        ]])
    else:
        kb = await smart_cancel_kb(bot)
    await cb.message.answer(
        "⭐ <b>شارژ با استارز تلگرام</b>\n\n"
        f"نرخ فعلی: هر ⭐ = {int(float(rate)):,} تومان\n\n"
        "مبلغی که می‌خوای کیف پولت شارژ بشه رو به <b>تومان</b> بفرست (فقط عدد):",
        reply_markup=kb, parse_mode="HTML")


@stars_admin_router.message(StarsFSM.amount, F.text)
async def w_stars_amount(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, get_setting, fa_to_int, main_kb_or_remove
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    await state.clear()
    if await get_setting(row.id, "stars_enabled", "0") != "1":
        await msg.answer("⭐ پرداخت با استارز تلگرام دیگر فعال نیست.")
        return
    amount_toman = fa_to_int(raw)
    if not amount_toman or amount_toman <= 0:
        # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
        # باگِ گزارش‌شده: قبلاً اگر متنِ ورودی عدد نبود (مثلاً کاربر منصرف
        # شده و دکمه‌ای از منویِ اصلی زده بود)، همیشه «مبلغ نامعتبر است»
        # نشان داده می‌شد و استیت هرگز پاک نمی‌شد — کاربر برایِ همیشه همان‌جا
        # گیر می‌کرد. حالا اگر واقعاً یک عدد نیست، فرض می‌کنیم کاربر دکمه‌ای
        # از منو زده و اجازه می‌دهیم همان پیام از مسیرِ عادیِ منو رد شود.
        if raw and not any(ch.isdigit() for ch in raw):
            await state.clear()
            raise SkipHandler
        await msg.answer("مبلغ نامعتبر است. یک عدد بفرست (مثلاً 50000).")
        return
    # === PATCH_KYC_GATE_ALL_PATHS_V1 === سقفِ احرازِ هویت
    from tenant.shared.kyc import gate as _kyc_gate
    _why = await _kyc_gate(row.id, msg.from_user.id, amount_toman)
    if _why:
        await msg.answer(_why)
        return
    rate = str(await _stars_rate_value(row.id))  # PATCH_AUTO_RATE_STARS_V1
    try:
        rate_v = float(rate)
    except Exception:
        rate_v = 1000.0
    if rate_v <= 0:
        rate_v = 1000.0
    stars = max(1, round(amount_toman / rate_v))
    try:
        await bot.send_invoice(
            chat_id=msg.chat.id,
            title="شارژ کیف پول",
            description=f"شارژ کیف پول به مبلغ {amount_toman:,} تومان (≈ {stars} ⭐)",
            payload=f"wallet_stars:{row.id}:{amount_toman}",
            provider_token="",  # برای پرداخت با استارز تلگرام، provider_token خالی است
            currency="XTR",
            prices=[LabeledPrice(label="شارژ کیف پول", amount=stars)],
        )
    except Exception as e:
        await msg.answer(f"❌ ارسال فاکتور پرداخت با خطا مواجه شد:\n{e}")


async def handle_wallet_stars_payment(msg: Message, bot: Bot, sp, payload: str):
    """از dispatcherِ سراسریِ ``stars_successful_payment`` (در tenant/__init__.py)
    وقتی payload با ``wallet_stars:`` شروع شود صدا زده می‌شود."""
    from tenant import get_bot_row, main_kb_or_remove, _charge_alert
    try:
        _, bot_id_str, amount_str = payload.split(":")
        bot_id = int(bot_id_str)
        amount_toman = int(amount_str)
    except Exception:
        return
    row = await get_bot_row(bot)
    if not row or row.id != bot_id:
        return
    async with Session() as s:
        from sqlalchemy import select
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id).with_for_update())
        if not u:
            return
        u.balance = float(u.balance) + amount_toman
        s.add(WalletTxn(bot_id=row.id, user_id=u.id, amount=amount_toman,
                        kind="charge", note=f"شارژ با استارز تلگرام ({sp.total_amount} ⭐)"))
        _kyc_uid = u.id
        await s.commit()
        _new_bal = float(u.balance)
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    from tenant.shared.kyc import after_charge as _ac
    await _ac(row.id, _kyc_uid)
    try:
        await _charge_alert(bot, row, buyer=u, amount=amount_toman,
                            method="استارز تلگرام ⭐", new_balance=_new_bal)
    except Exception:
        logger.exception("خطای نادیدهگرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_TX_LOG_CHANNEL_V1 ===
    try:
        from tenant.shared.tx_channel import tx_log
        await tx_log(bot, row.id, buyer=u, amount=amount_toman, gateway="stars",
                     old_balance=(_new_bal - float(amount_toman)),
                     new_balance=_new_bal,
                     ref=str(getattr(sp, "telegram_payment_charge_id", "") or ""))
    except Exception:
        logger.exception("tx_log (stars)")
    from tenant.gateways.wallet_receipt import build_wallet_charge_message
    wtext, wkb = build_wallet_charge_message(amount_toman, _new_bal, "stars")
    await msg.answer(wtext, reply_markup=wkb)


# ═══════════════════════ تنظیمات ادمین ═══════════════════════

# === PATCH_AUTO_RATE_STARS_V1 ===
# اگر «نرخ خودکار» روشن باشد، نرخِ پذیرشِ استارز از بازار می‌آید و
# درصدِ کسر (پیش‌فرض ۳۰٪) بابتِ کمیسیونِ تلگرام از آن کم می‌شود.
async def _stars_rate_value(bot_id: int) -> float:
    from tenant import get_setting
    if (await get_setting(bot_id, "stars_rate_auto", "0")) == "1":
        try:
            from tenant.gateways.stars_rate import star_toman
            cut = float(await get_setting(bot_id, "stars_rate_cut", "30") or 30)
            v = await star_toman(-cut, bot_id)
            if v > 0:
                return v
        except Exception:
            logger.exception("نرخِ خودکارِ استارز ناموفق بود")
    try:
        return float(await get_setting(bot_id, "stars_toman_per_star", "1000"))
    except Exception:
        return 1000.0


async def _stars_admin_content(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    enabled = await get_setting(bot_id, "stars_enabled", "0") == "1"
    _auto = await get_setting(bot_id, "stars_rate_auto", "0") == "1"
    _cut = float(await get_setting(bot_id, "stars_rate_cut", "30") or 30)
    rate = str(int(round(await _stars_rate_value(bot_id))))
    try:
        rate_disp = f"{int(float(rate)):,}"
    except Exception:
        rate_disp = rate
    txt = (
        "⭐ <b>مدیریت پرداخت با استارز تلگرام</b>\n\n"
        f"وضعیت: {'✅ فعال' if enabled else '❌ غیرفعال'}\n"
        f"نرخ فعلی: هر ⭐ = {rate_disp} تومان"
        + ("\n<i>(خودکار از بازار، منهایِ " + f"{_cut:g}" + "٪ کمیسیون)</i>"
           if _auto else "")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if enabled else "✅ فعال‌کردن"),
            callback_data="stars:toggle", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="💱 تغییر نرخ", callback_data="stars:rate", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=("🔄 نرخ خودکار: روشن ✅" if _auto else "🔄 نرخ خودکار: خاموش"),
            callback_data="stars:autotoggle",
            style=(ButtonStyle.SUCCESS if _auto else ButtonStyle.PRIMARY))],
        [InlineKeyboardButton(text=f"📉 کسرِ کمیسیون ({_cut:g}٪)",
                              callback_data="stars:setcut", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    return txt, kb


@stars_admin_router.callback_query(F.data == "set:stars")
async def set_stars_start(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await cb.answer()
    text, kb = await _stars_admin_content(row.id)
    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_AUTO_RATE_STARS_V1 — هندلرهایِ نرخِ خودکار ===
@stars_admin_router.callback_query(F.data == "stars:autotoggle")
async def stars_auto_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, "stars_rate_auto", "0")) == "1"
    await set_setting(row.id, "stars_rate_auto", "0" if cur else "1")
    if not cur:
        v = await _stars_rate_value(row.id)
        if v <= 0:
            await set_setting(row.id, "stars_rate_auto", "0")
            await cb.answer("نرخِ بازار در دسترس نیست؛ فعلاً دستی بماند.",
                            show_alert=True)
        else:
            await cb.answer(f"روشن شد — هر ⭐ ≈ {int(v):,} تومان", show_alert=True)
    else:
        await cb.answer("خاموش شد؛ نرخِ دستی استفاده می‌شود.")
    text, kb = await _stars_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@stars_admin_router.callback_query(F.data == "stars:setcut")
async def stars_setcut_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(StarsFSM.cut_edit)
    await cb.message.answer(
        "📉 چند درصد از نرخِ بازار کسر شود؟ (کمیسیونِ تلگرام حدودِ ۳۰٪ است)\n"
        "فقط عدد، مثال: 30",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@stars_admin_router.message(StarsFSM.cut_edit, F.text)
async def stars_setcut_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > 90:
        await msg.answer("عددِ نامعتبر (۰ تا ۹۰). دوباره از منو امتحان کن.")
        return
    await set_setting(row.id, "stars_rate_cut", str(val))
    text, kb = await _stars_admin_content(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@stars_admin_router.callback_query(F.data == "stars:toggle")
async def stars_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "stars_enabled", "0")
    new_val = "0" if cur == "1" else "1"
    await set_setting(row.id, "stars_enabled", new_val)
    await cb.answer("✅ فعال شد" if new_val == "1" else "❌ غیرفعال شد")
    text, kb = await _stars_admin_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@stars_admin_router.callback_query(F.data == "stars:rate")
async def stars_rate_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(StarsFSM.rate_edit)
    rate = str(await _stars_rate_value(row.id))  # PATCH_AUTO_RATE_STARS_V1
    await cb.answer()
    await cb.message.answer(
        f"💵 نرخِ فعلی: هر ⭐ = {rate} تومان\n\n"
        "نرخِ جدید را به تومان بفرست (فقط عدد، مثلاً 1200):",
        reply_markup=await smart_cancel_kb(bot))


@stars_admin_router.message(StarsFSM.rate_edit, F.text)
async def stars_rate_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting, smart_cancel_kb, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    if not row:
        return
    raw = msg.text.strip()
    # === PATCH_SMM_TEXT_INPUT_CANCEL_FIX_V1 ===
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    n = fa_to_int(raw)
    if n is None or n <= 0:
        if raw and not any(ch.isdigit() for ch in raw):
            await state.clear()
            raise SkipHandler
        await msg.answer("یک عدد معتبر و مثبت بفرست (مثلاً 1200):", reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "stars_toman_per_star", str(n))
    await state.clear()
    text, kb = await _stars_admin_content(row.id)
    await msg.answer("✅ نرخ ذخیره شد.\n\n" + text, reply_markup=kb)


# ═══════════════════════ شارژِ موجودیِ استارزِ خودِ ربات (ادمین) ═══════════════════════
# === PATCH_BOT_STARS_TOPUP_V1 ===
# گزارشِ Mike: دکمهٔ «💰 افزایشِ موجودیِ استارزِ ربات» (گیفت/پرمیوم) پیامِ «پرداخت با استارز هنوز توسطِ مدیر
# فعال نشده» می‌داد. آن دکمه به «شارژِ کیف‌پولِ مشتری با استارز» (w:stars) وصل بود که به درگاهِ استارزِ
# مشتری‌ها (stars_enabled) وابسته است و در عوض کیف‌پولِ تومانیِ ادمین را هم شارژ می‌کرد. حالا مسیرِ جدا:
# ادمین تعدادِ استارز را انتخاب می‌کند، فاکتورِ XTR می‌آید و پرداخت مستقیم به موجودیِ استارزِ ربات اضافه
# می‌شود (همان موجودی که sendGift / پرمیوم از آن خرج می‌کنند) — بدونِ هیچ شارژِ کیف‌پول.
_BOT_TOPUP_PRESETS = (100, 250, 500, 1000, 2500, 5000)


async def _is_staff(row, tg_id: int) -> bool:
    from tenant import is_owner_or_admin
    try:
        return await is_owner_or_admin(row.id, tg_id, row.owner_id)
    except Exception:
        return tg_id == row.owner_id


async def _send_bot_topup_invoice(bot: Bot, chat_id: int, row, stars: int) -> None:
    await bot.send_invoice(
        chat_id=chat_id,
        title="شارژِ موجودیِ استارزِ ربات",
        description=f"افزودنِ {stars:,} ⭐ به موجودیِ ربات (برایِ ارسالِ گیفت/پرمیوم)",
        payload=f"bot_stars:{row.id}:{stars}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="موجودیِ استارزِ ربات", amount=stars)],
    )


@stars_admin_router.callback_query(F.data == "w:botstars")
async def bot_stars_topup_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await _is_staff(row, cb.from_user.id):
        await cb.answer("فقط مدیرِ ربات.", show_alert=True)
        return
    bal = None
    try:
        bal = (await bot.get_my_star_balance()).amount
    except Exception:
        logger.debug("خواندنِ موجودیِ استارزِ ربات ممکن نشد")
    btns = [InlineKeyboardButton(text=f"{n:,} ⭐", callback_data=f"w:botstars:{n}") for n in _BOT_TOPUP_PRESETS]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton(text="✍️ تعدادِ دلخواه", callback_data="w:botstars:custom",
                                      style=ButtonStyle.PRIMARY)])
    await state.clear()
    await cb.answer()
    await cb.message.answer(
        "💰 <b>شارژِ موجودیِ استارزِ ربات</b>\n\n"
        + (f"موجودیِ فعلیِ ربات: <b>{int(bal):,} ⭐</b>\n\n" if bal is not None else "")
        + "تعدادِ استارز را انتخاب کن؛ فاکتور برایت می‌آید و بعد از پرداخت، استارزها مستقیم به موجودیِ "
          "ربات اضافه می‌شوند (برایِ ارسالِ خودکارِ گیفت و پرمیوم). کیف‌پولِ تومانی شارژ نمی‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@stars_admin_router.callback_query(F.data.startswith("w:botstars:"))
async def bot_stars_topup_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await _is_staff(row, cb.from_user.id):
        await cb.answer("فقط مدیرِ ربات.", show_alert=True)
        return
    arg = cb.data.split(":", 2)[2]
    await cb.answer()
    if arg == "custom":
        await state.set_state(StarsFSM.bot_topup)
        await cb.message.answer("تعدادِ استارز را بفرست (۱ تا ۱۰۰٬۰۰۰):", reply_markup=await smart_cancel_kb(bot))
        return
    try:
        n = int(arg)
    except ValueError:
        return
    try:
        await _send_bot_topup_invoice(bot, cb.message.chat.id, row, n)
    except Exception as e:
        await cb.message.answer(f"❌ ارسالِ فاکتور ناموفق بود:\n{e}")


@stars_admin_router.message(StarsFSM.bot_topup, F.text)
async def bot_stars_topup_custom(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int
    row = await get_bot_row(bot)
    raw = (msg.text or "").strip()
    if not row or raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    n = fa_to_int(raw)
    if not n:
        if raw and not any(ch.isdigit() for ch in raw):
            await state.clear()
            raise SkipHandler
        await msg.answer("یک عدد بفرست (مثلاً 300):")
        return
    if n < 1 or n > 100_000:
        await msg.answer("بینِ ۱ تا ۱۰۰٬۰۰۰ بفرست:")
        return
    await state.clear()
    try:
        await _send_bot_topup_invoice(bot, msg.chat.id, row, n)
    except Exception as e:
        await msg.answer(f"❌ ارسالِ فاکتور ناموفق بود:\n{e}")


async def handle_bot_stars_payment(msg: Message, bot: Bot, sp, payload: str):
    """پرداختِ موفقِ «bot_stars:» — استارز همین حالا در موجودیِ ربات است؛ فقط اطلاع می‌دهیم."""
    stars = int(sp.total_amount or 0)
    bal_line = ""
    try:
        bal_line = f"\nموجودیِ فعلیِ ربات: <b>{(await bot.get_my_star_balance()).amount:,} ⭐</b>"
    except Exception:
        logger.debug("خواندنِ موجودیِ استارزِ ربات بعد از شارژ ممکن نشد")
    await msg.answer(f"✅ {stars:,} ⭐ به موجودیِ استارزِ ربات اضافه شد.{bal_line}\n\n"
                     "اگر سفارشی منتظرِ موجودی است، از پیامِ همان سفارش «🔄 ارسال» را بزن.",
                     parse_mode="HTML")
