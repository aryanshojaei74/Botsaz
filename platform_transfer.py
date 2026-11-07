"""
platform_transfer.py — بخشِ «انتقال ربات به مالک دیگر» جدا از platform_bot.py.

استخراج‌شده از platform_bot.py (~۳۲۵ خطِ سکشن + کلاسِ Transfer). فقط ادمین
پلتفرم می‌تونه از این استفاده کنه. رفتار ۱۰۰٪ حفظ شده — همون هندلرها،
همون callback_dataها، همون FSM.
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove,
)
from sqlalchemy import select, update as sa_update

from config import Session, WEBHOOK_BASE
from models import Bot as BotRow, OwnerTransferRequest
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

transfer_router = Router()


# ────────── lazy-import wrappers از platform_bot ──────────

async def smart_cancel_kb():
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


async def smart_home_kb(uid: int, show_admin: bool = False):
    from platform_bot import smart_home_kb as _fn
    return await _fn(uid, show_admin=show_admin)


async def has_perm(uid: int, perm: str) -> bool:
    from platform_bot import has_perm as _fn
    return await _fn(uid, perm)


# === PATCH_TRANSFER_MISSING_HELPERS_V1 ===
# دو helper مهم که هنگام استخراج فراموش شدن:
# - migrate_bot_data (توی config.py) — انتقالِ داده‌های ربات به مالک جدید
# - _temp_bot (توی platform_bot.py) — context managerِ Bot موقت

async def migrate_bot_data(old_bot_id: int, new_bot_id: int):
    """Lazy wrapper — انتقال داده‌های ربات به مالک جدید."""
    from config import migrate_bot_data as _fn
    return await _fn(old_bot_id, new_bot_id)


def _temp_bot(token: str):
    """Lazy wrapper — context managerِ Bot موقت."""
    from platform_bot import _temp_bot as _cls
    return _cls(token)


BTN_CANCEL = "❌ لغو"


class Transfer(StatesGroup):
    """انتقال یک ربات به مالک تلگرامی دیگر — فقط از پنل ادمین پلتفرم قابل شروع است."""
    new_owner_id = State()   # دریافت شناسهٔ عددی مالک جدید
    choose_mode = State()    # انتظار برای انتخاب روش انتقال (دکمه‌ای)
    new_token = State()      # دریافت توکن جدید (توسط ادمین یا مالک جدید)



# ======================= انتقال ربات به مالک دیگر (فقط ادمین) =======================
@transfer_router.callback_query(F.data.startswith("abotxfer:"))
async def adm_bot_transfer_start(cb: CallbackQuery, state: FSMContext):
    if not await has_perm(cb.from_user.id, "adm:bots"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    bot_id = int(cb.data.split(":")[1])
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            await cb.answer("یافت نشد", show_alert=True)
            return
    await state.set_state(Transfer.new_owner_id)
    await state.update_data(xfer_bot_id=bot_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔁 <b>انتقال ربات به کاربر دیگر</b>\n\n"
        "شناسهٔ عددی تلگرام (Telegram ID) مالک جدید را همینجا بفرست.",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


@transfer_router.message(Transfer.new_owner_id, F.text)
async def adm_bot_transfer_owner_id(msg: Message, state: FSMContext):
    if not await has_perm(msg.from_user.id, "adm:bots"):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        await msg.answer("شناسهٔ نامعتبر است. فقط عدد بفرست (یا «❌ لغو» را بزن).")
        return
    new_owner_id = int(raw)
    data = await state.get_data()
    bot_id = data.get("xfer_bot_id")
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            await state.clear()
            await msg.answer("ربات یافت نشد.", reply_markup=ReplyKeyboardRemove())
            return
        uname, cur_owner = r.username, r.owner_id
    if new_owner_id == cur_owner:
        await msg.answer("این که همان مالک فعلی است! شناسهٔ کاربر دیگری را بفرست.")
        return
    await state.set_state(Transfer.choose_mode)
    await state.update_data(xfer_new_owner=new_owner_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("♻️ همین توکن فعلی بماند (فقط مالکیت عوض شود)",
                              callback_data="abotxfermode:keep")],
        [hx_button("✍️ توکن جدید را خودم وارد می‌کنم",
                              callback_data="abotxfermode:mytoken")],
        [hx_button("📨 از کاربر جدید بخواه توکن خودش را بفرستد",
                              callback_data="abotxfermode:selftoken")],
        [hx_button("❌ انصراف", callback_data="abotxfermode:abort", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"مالک فعلی: <code>{cur_owner}</code>\nمالک جدید: <code>{new_owner_id}</code>\nربات: @{uname}\n\n"
        "روش انتقال را انتخاب کن:",
        reply_markup=kb,
    )


@transfer_router.callback_query(F.data.startswith("abotxfermode:"), Transfer.choose_mode)
async def adm_bot_transfer_mode(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not await has_perm(cb.from_user.id, "adm:bots"):
        await state.clear()
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    mode = cb.data.split(":")[1]
    data = await state.get_data()
    bot_id = data.get("xfer_bot_id")
    new_owner_id = data.get("xfer_new_owner")

    if mode == "abort":
        await state.clear()
        try:
            await cb.message.edit_text("انصراف داده شد.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.answer()
        return

    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            await state.clear()
            await cb.answer("ربات یافت نشد", show_alert=True)
            return
        uname, cur_owner = r.username, r.owner_id

    if mode == "keep":
        async with Session() as s:
            r = await s.get(BotRow, bot_id)
            if not r:
                await state.clear()
                await cb.answer("ربات یافت نشد", show_alert=True)
                return
            # === PATCH_BOT_AUDIT_LOG_V1 ===
            from models import BotAuditLog as _BAL
            s.add(_BAL(bot_id=bot_id, username=r.username, plan=r.plan,
                       action="transfer", from_owner=cur_owner,
                       to_owner=new_owner_id, note="حفظ توکن"))
            r.owner_id = new_owner_id
            await s.commit()
        await state.clear()
        text = f"✅ مالکیت ربات @{uname} به کاربر <code>{new_owner_id}</code> منتقل شد."
        try:
            await cb.message.edit_text(text)
        except Exception:
            await cb.message.answer(text)
        try:
            await bot.send_message(cur_owner, f"ℹ️ مدیریت ربات @{uname} به کاربر دیگری منتقل شد.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        try:
            await bot.send_message(
                new_owner_id,
                f"🎁 ربات @{uname} توسط مدیریت بات‌ساز به شما منتقل شد.\n"
                "از «🗂 ربات‌های من» می‌تونی مدیریتش کنی.",
            )
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.answer("انجام شد ✅")
        return

    if mode == "mytoken":
        await state.set_state(Transfer.new_token)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(
            "توکن ربات جدید را همینجا بفرست\n"
            "(همان توکنی که از کاربر جدید گرفتی، یا توکن ربات تازهٔ او در بات‌فادر).",
            reply_markup=await smart_cancel_kb(),
        )
        await cb.answer()
        return

    if mode == "selftoken":
        async with Session() as s:
            existing = await s.scalar(select(OwnerTransferRequest).where(
                OwnerTransferRequest.old_bot_id == bot_id,
                OwnerTransferRequest.status == "pending_token",
            ))
            if existing:
                await cb.answer("یک درخواست انتقال برای این ربات از قبل در جریان است.", show_alert=True)
                return
            req = OwnerTransferRequest(old_bot_id=bot_id, requested_by=cb.from_user.id,
                                        new_owner_id=new_owner_id, status="pending_token")
            s.add(req)
            await s.commit()
            req_id = req.id
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            hx_button("🔁 ارسال توکن ربات من", callback_data=f"abotxferself:{req_id}")
        ]])
        sent = True
        try:
            await bot.send_message(
                new_owner_id,
                "🔁 <b>درخواست انتقال ربات</b>\n\n"
                f"مدیریت بات‌ساز می‌خواهد ربات @{uname} با توکن ربات خودت جایگزین و به تو منتقل شود.\n\n"
                "اگر قبول داری، روی دکمهٔ زیر بزن و توکن ربات خودت (از @BotFather) را بفرست.",
                reply_markup=kb,
            )
        except Exception:
            sent = False
        await state.clear()
        if sent:
            try:
                await cb.message.edit_text(
                    "✅ درخواست برای کاربر جدید ارسال شد.\n"
                    "به‌محض ارسال توکن توسط او، انتقال به‌طور خودکار انجام می‌شود."
                )
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await cb.answer()
        else:
            async with Session() as s:
                req2 = await s.get(OwnerTransferRequest, req_id)
                if req2:
                    await s.delete(req2)
                    await s.commit()
            await cb.answer(
                "نتونستم به کاربر جدید پیام بدم؛ او باید اول همین ربات (بات‌ساز) را با /start شروع کرده باشد. "
                "از او بخواه این کار را انجام بدهد و دوباره امتحان کن.",
                show_alert=True,
            )
        return


@transfer_router.callback_query(F.data.startswith("abotxferself:"))
async def adm_bot_transfer_self_start(cb: CallbackQuery, state: FSMContext):
    req_id = int(cb.data.split(":")[1])
    async with Session() as s:
        req = await s.get(OwnerTransferRequest, req_id)
        if not req or req.status != "pending_token":
            await cb.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
            return
        if cb.from_user.id != req.new_owner_id:
            await cb.answer("این درخواست برای شما نیست.", show_alert=True)
            return
        old_bot_id, new_owner_id = req.old_bot_id, req.new_owner_id
    await state.set_state(Transfer.new_token)
    await state.update_data(xfer_bot_id=old_bot_id, xfer_new_owner=new_owner_id, xfer_req_id=req_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "توکن ربات خودت را از @BotFather بگیر و همینجا بفرست.\n"
        "(چیزی شبیه <code>123456:ABC-...</code>)",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


@transfer_router.message(Transfer.new_token, F.text)
async def adm_bot_transfer_new_token(msg: Message, state: FSMContext, bot: Bot):
    new_token = (msg.text or "").strip()
    if ":" not in new_token or len(new_token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return

    data = await state.get_data()
    bot_id = data.get("xfer_bot_id")
    new_owner_id = data.get("xfer_new_owner")
    req_id = data.get("xfer_req_id")

    async with Session() as s:
        r = await s.get(BotRow, bot_id)
        if not r:
            await state.clear()
            await msg.answer("ربات یافت نشد.", reply_markup=ReplyKeyboardRemove())
            return
        old_uname, old_owner_id, old_field, old_plan, old_expires = (
            r.username, r.owner_id, r.field, r.plan, r.plan_expires_at,
        )
        is_admin_caller = await has_perm(msg.from_user.id, "adm:bots")

    if req_id:
        async with Session() as s:
            req = await s.get(OwnerTransferRequest, req_id)
            if not req or req.status != "pending_token" or msg.from_user.id != req.new_owner_id:
                await state.clear()
                await msg.answer("این درخواست دیگر معتبر نیست.", reply_markup=ReplyKeyboardRemove())
                return
    elif not is_admin_caller:
        await state.clear()
        await msg.answer("اجازهٔ این کار را نداری.", reply_markup=ReplyKeyboardRemove())
        return

    try:
        async with _temp_bot(new_token) as test_bot:
            me = await test_bot.get_me()
    except Exception:
        await msg.answer("❌ این توکن معتبر نیست یا ربات در دسترس نیست. دوباره بفرست.")
        return

    async with Session() as s:
        if await s.scalar(select(BotRow).where(BotRow.token == new_token)):
            await msg.answer("این توکن قبلاً برای ربات دیگری ثبت شده. توکن دیگری بفرست.")
            return

    async with Session() as s:
        if req_id:
            # تصاحبِ اتمیِ درخواست: دو پیامِ توکنِ هم‌زمان دو ربات نسازند
            claim = await s.execute(
                sa_update(OwnerTransferRequest)
                .where(OwnerTransferRequest.id == req_id,
                       OwnerTransferRequest.status == "pending_token")
                .values(status="processing"))
            if claim.rowcount == 0:
                await s.rollback()
                await state.clear()
                await msg.answer("این درخواست دیگر معتبر نیست.", reply_markup=ReplyKeyboardRemove())
                return
        new_row = BotRow(
            owner_id=new_owner_id, token=new_token, username=me.username,
            field=old_field, plan=old_plan, status="active",
            plan_expires_at=old_expires,
        )
        s.add(new_row)
        await s.flush()
        new_bot_id = new_row.id
        await s.commit()

    await migrate_bot_data(bot_id, new_bot_id)

    old_token = None
    async with Session() as s:
        old_row2 = await s.get(BotRow, bot_id)
        if old_row2:
            old_token = old_row2.token
            # === PATCH_BOT_AUDIT_LOG_V1 ===
            # حالتِ «توکنِ جدید»: رباتِ قدیمی حذف و رکوردِ تازه ساخته
            # می‌شود، پس بدونِ این ثبت هیچ ردی از انتقال نمی‌ماند —
            # در پروندهٔ مالکِ قبلی فقط یک «حذف» دیده می‌شد.
            from models import BotAuditLog as _BAL
            s.add(_BAL(bot_id=new_bot_id, username=me.username,
                       plan=old_plan, action="transfer",
                       from_owner=old_owner_id, to_owner=new_owner_id,
                       note=f"توکن جدید (قبلاً @{old_uname or '؟'})"))
            await s.delete(old_row2)
        if req_id:
            req2 = await s.get(OwnerTransferRequest, req_id)
            if req2:
                req2.status = "done"
                req2.new_bot_id = new_bot_id
                req2.new_token = new_token
                req2.new_username = me.username
                req2.decided_at = datetime.datetime.now()
        await s.commit()

    if old_token:
        try:
            async with _temp_bot(old_token) as ob:
                await ob.delete_webhook()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        async with _temp_bot(new_token) as nb:
            await nb.set_webhook(f"{WEBHOOK_BASE}/webhook/{new_token}", drop_pending_updates=True,
                                 allowed_updates=__import__("webhook_utils").TENANT_ALLOWED_UPDATES)
    except Exception as e:
        print("adm_bot_transfer_new_token: activate new bot failed:", e)

    await state.clear()
    await msg.answer(
        f"✅ ربات با موفقیت به کاربر <code>{new_owner_id}</code> منتقل و با توکن جدید "
        f"(@{me.username}) فعال شد.",
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        if old_owner_id != msg.from_user.id:
            await bot.send_message(old_owner_id, f"ℹ️ ربات @{old_uname} به کاربر دیگری منتقل شد.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        if new_owner_id != msg.from_user.id:
            await bot.send_message(
                new_owner_id,
                f"🎁 ربات @{old_uname} (اکنون @{me.username}) به شما منتقل شد.\n"
                "از «🗂 ربات‌های من» می‌تونی مدیریتش کنی.",
            )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

