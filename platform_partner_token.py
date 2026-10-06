"""
platform_partner_token.py — بخشِ «تعویضِ توکن» و «انتقالِ مالکیتِ» رباتِ همکار،
جدا از platform_partner_admin.py.

=== PATCH_PARTNER_TOKEN_OWNER_V1 ===
چرا جدا: platform_partner_admin.py پنلِ نظارتیِ همکاران است و از قبل
۱۴۰۰+ خط شده؛ این دو جریان هر کدام FSM و چند مرحلهٔ خودشان را دارند.

چرا از `abotxfer:`/`abotswapreq:`ِ موجود مستقیم استفاده نشد:
  • مسیرِ `abotxfer:` در حالتِ توکنِ جدید، یک ردیفِ Bot تازه می‌سازد و ردیفِ
    قدیم را پاک می‌کند بدونِ redirect_all_bot_id_references — برایِ رباتِ
    عادی مشکلی ندارد ولی برایِ رباتِ همکار، `partner_bots.bot_id` را
    بی‌ارجاع می‌گذارد (کلِ آمار/انبار/فروش‌هایِ همکار گم می‌شود).
  • همچنین `owner_id`ِ خودِ ردیفِ PartnerBot را عوض نمی‌کند، پس همکار
    دیگر به پنلِ رباتِ همکارش دسترسی نداشت.
این‌جا هر دو نکته رعایت شده و قلبِ اعمالِ تعویض، همان تابعِ آزمایش‌شدهٔ
`_apply_token_migration` در platform_bot.py است (نه نسخهٔ دوباره).

جریان‌ها (فقط PLATFORM_ADMIN_ID):
  pat:tok:<pb>     → منویِ تعویضِ توکن
      pat:tokreq:<pb>  → درخواست به صاحبِ ربات همکار، داخلِ خودِ رباتِ او
                          (از جدولِ TokenSwapRequest و تاییدِ نهاییِ
                           `tkswapdecide:` استفاده می‌کند)
      pat:tokman:<pb>  → ادمین خودش توکنِ جدید را می‌فرستد
  pat:own:<pb>     → منویِ انتقالِ مالکیت
      pat:ownkeep:<pb> → همین توکن بماند، فقط مالک عوض شود
      pat:ownask:<pb>  → از مالکِ جدید خواسته شود توکنِ خودش را بفرستد
      pat:acc:<req>    → دکمه‌ای که برایِ مالکِ جدید فرستاده می‌شود
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from config import Session, PLATFORM_ADMIN_ID
from models import (
    Bot as BotRow, OwnerTransferRequest, PartnerBot, TokenSwapRequest,
)
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_token_router = Router()


# ────────── lazy-import wrapperها (شکستنِ importِ حلقوی) ──────────

def _temp_bot(token: str):
    from bot_utils import _temp_bot as _cls
    return _cls(token)


async def _apply_token_migration(old_bot_id: int, new_token: str):
    """قلبِ واقعیِ تعویضِ توکن — در platform_bot.py است، این‌جا فقط صدا زده می‌شود."""
    from platform_bot import _apply_token_migration as _fn
    return await _fn(old_bot_id, new_token)


async def _invalidate(token: str | None) -> None:
    """کشِ وضعیتِ تنانت را در همهٔ پروسه‌ها باطل کن (بدونِ کرش‌کردنِ جریان)."""
    if not token:
        return
    try:
        from tenant_status_bus import publish_tenant_invalidate
        await publish_tenant_invalidate(token)
    except Exception:
        logger.exception("انتشارِ سیگنالِ باطل‌سازی ناموفق بود")


class PartnerTokenFSM(StatesGroup):
    admin_token = State()    # ادمین توکنِ جدیدِ ربات همکار را می‌فرستد
    new_owner_id = State()   # شناسهٔ عددیِ مالکِ جدید
    owner_token = State()    # مالکِ جدید توکنِ رباتِ خودش را می‌فرستد


def _admin_only(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


def _back_kb(pb_id: int, label: str = "🔙 بازگشت") -> InlineKeyboardMarkup:
    """راهِ بیرون‌آمدن از هر مرحله‌ای که منتظرِ تایپ است — دقیقاً یک پله عقب."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"pat:cancel:{pb_id}",
                             style=ButtonStyle.DANGER)]])


async def _pb_info(pb_id: int) -> tuple[int, int, str | None, str | None] | None:
    """(bot_id, owner_id, username, token) رباتِ همکار."""
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            return None
        br = await s.get(BotRow, pb.bot_id)
        if not br:
            return None
        return pb.bot_id, pb.owner_id, br.username, br.token


async def _token_taken(token: str) -> bool:
    async with Session() as s:
        return bool(await s.scalar(select(BotRow.id).where(BotRow.token == token)))


# ────────── بازگشت / انصراف ──────────

@partner_token_router.callback_query(F.data.startswith("pat:cancel:"))
async def pat_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pb_id = int(cb.data.split(":")[2])
    from platform_partner_admin import pa_view_by_id
    await pa_view_by_id(cb, pb_id)


# ═══════════════════ تعویضِ توکن ═══════════════════

@partner_token_router.callback_query(F.data.startswith("pat:tok:"))
async def pat_token_menu(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    info = await _pb_info(pb_id)
    if not info:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    _bot_id, owner, uname, _tok = info
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("📨 درخواست از صاحب ربات", callback_data=f"pat:tokreq:{pb_id}",
                   style=ButtonStyle.PRIMARY)],
        [hx_button("✍️ توکن جدید را خودم وارد می‌کنم",
                   callback_data=f"pat:tokman:{pb_id}", style=ButtonStyle.PRIMARY)],
        [hx_button("🔙 بازگشت", callback_data=f"pa:view:{pb_id}",
                   style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(
        f"🔁 <b>تعویض توکن ربات همکار</b>\n\n"
        f"ربات: @{uname or '—'}\n"
        f"صاحب: <code>{owner}</code>\n\n"
        "• «درخواست از صاحب ربات»: پیامی داخلِ خودِ ربات همکار برایش می‌رود؛ "
        "توکن جدیدش را می‌فرستد و پس از تاییدِ تو جابه‌جا می‌شود.\n"
        "• همهٔ داده‌ها (انبار، فروش‌ها، مشتریان) روی توکن جدید منتقل می‌شود.",
        reply_markup=kb)
    await cb.answer()


@partner_token_router.callback_query(F.data.startswith("pat:tokreq:"))
async def pat_token_request(cb: CallbackQuery):
    """درخواستِ تعویضِ توکن برایِ صاحبِ ربات همکار — داخلِ خودِ رباتِ او."""
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    info = await _pb_info(pb_id)
    if not info:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    bot_id, owner, uname, token = info

    async with Session() as s:
        existing = await s.scalar(select(TokenSwapRequest).where(
            TokenSwapRequest.old_bot_id == bot_id,
            TokenSwapRequest.status.in_(["pending_token", "pending_admin"]),
        ))
        if existing:
            await cb.answer("یک درخواست تعویض توکن برای این ربات از قبل در جریان است.",
                            show_alert=True)
            return
        swap = TokenSwapRequest(old_bot_id=bot_id, requested_by=owner,
                                status="pending_token")
        s.add(swap)
        await s.commit()
        swap_id = swap.id

    sent = True
    try:
        async with _temp_bot(token) as tb:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                hx_button("🔁 ارسال توکن جدید", callback_data=f"tkswap:start:{swap_id}")
            ]])
            await tb.send_message(
                owner,
                "🔁 <b>درخواست تعویض توکن ربات همکار</b>\n\n"
                f"مدیریت بات‌ساز درخواست کرده توکن ربات @{uname} با توکن جدیدی "
                "جایگزین شود (مثلاً بعد از ساخت ربات تازه در بات‌فادر).\n\n"
                "اگر موافقی، دکمهٔ زیر را بزن و توکن جدید را بفرست. تا تاییدِ "
                "نهاییِ مدیریت، ربات فعلی فعال می‌ماند و هیچ داده‌ای از بین نمی‌رود.",
                reply_markup=kb)
    except Exception:
        sent = False
        logger.exception("pat_token_request: ارسال به صاحب ربات همکار ناموفق")

    if not sent:
        async with Session() as s:
            row = await s.get(TokenSwapRequest, swap_id)
            if row:
                await s.delete(row)
                await s.commit()
        await cb.answer("ارسال پیام به صاحب ربات ناموفق بود "
                        "(شاید ربات همکار متوقف است یا او آن را استارت نکرده).",
                        show_alert=True)
        return
    await cb.answer("درخواست برای صاحب ربات ارسال شد ✅", show_alert=True)


@partner_token_router.callback_query(F.data.startswith("pat:tokman:"))
async def pat_token_manual_start(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    if not await _pb_info(pb_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await state.set_state(PartnerTokenFSM.admin_token)
    await state.update_data(pat_pb_id=pb_id)
    await cb.message.answer(
        "توکن جدید ربات همکار را همین‌جا بفرست\n"
        "(چیزی شبیه <code>123456:ABC-...</code>).",
        reply_markup=_back_kb(pb_id, "🔙 انصراف"))
    await cb.answer()


@partner_token_router.message(PartnerTokenFSM.admin_token, F.text)
async def pat_token_manual_do(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    data = await state.get_data()
    pb_id = data.get("pat_pb_id")
    new_token = (msg.text or "").strip()
    if ":" not in new_token or len(new_token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return
    if await _token_taken(new_token):
        await msg.answer("این توکن قبلاً برای ربات دیگری ثبت شده. توکن دیگری بفرست.")
        return
    info = await _pb_info(pb_id)
    if not info:
        await state.clear()
        await msg.answer("ربات همکار یافت نشد.")
        return
    bot_id, owner, uname, old_token = info

    await state.clear()
    ok, err, new_bot_id, new_uname = await _apply_token_migration(bot_id, new_token)
    if not ok:
        await msg.answer(f"❌ {err or 'تعویض توکن انجام نشد.'}")
        return
    await _invalidate(old_token)
    await _invalidate(new_token)
    await msg.answer(
        f"✅ توکن ربات همکار عوض شد.\n@{uname or '—'} → @{new_uname}")
    try:
        async with _temp_bot(new_token) as nb:
            await nb.send_message(
                owner,
                f"✅ توکن ربات همکار شما توسط مدیریت بات‌ساز به @{new_uname} تغییر کرد.\n"
                "همهٔ اطلاعات، انبار و مشتریان شما منتقل شده‌اند.")
    except Exception:
        logger.exception("اطلاع‌رسانی به صاحب ربات همکار ناموفق بود")


# ═══════════════════ انتقالِ مالکیت ═══════════════════

@partner_token_router.callback_query(F.data.startswith("pat:own:"))
async def pat_owner_menu(cb: CallbackQuery):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    info = await _pb_info(pb_id)
    if not info:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    _bot_id, owner, uname, _tok = info
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("♻️ همین توکن بماند، فقط مالک عوض شود",
                   callback_data=f"pat:ownkeep:{pb_id}", style=ButtonStyle.PRIMARY)],
        [hx_button("📨 مالک جدید توکن ربات خودش را بفرستد",
                   callback_data=f"pat:ownask:{pb_id}", style=ButtonStyle.PRIMARY)],
        [hx_button("🔙 بازگشت", callback_data=f"pa:view:{pb_id}",
                   style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(
        f"👤 <b>انتقال مالکیت ربات همکار</b>\n\n"
        f"ربات: @{uname or '—'}\n"
        f"مالک فعلی: <code>{owner}</code>\n\n"
        "انبار، فروش‌ها، مشتریان و تخفیفِ همکاری همگی روی مالک جدید منتقل می‌شوند.",
        reply_markup=kb)
    await cb.answer()


@partner_token_router.callback_query(
    F.data.startswith("pat:ownkeep:") | F.data.startswith("pat:ownask:"))
async def pat_owner_ask_id(cb: CallbackQuery, state: FSMContext):
    if not _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    mode = "keep" if cb.data.startswith("pat:ownkeep:") else "ask"
    pb_id = int(cb.data.split(":")[2])
    if not await _pb_info(pb_id):
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await state.set_state(PartnerTokenFSM.new_owner_id)
    await state.update_data(pat_pb_id=pb_id, pat_mode=mode)
    await cb.message.answer(
        "👤 شناسهٔ عددی تلگرام (Telegram ID) مالک جدید را بفرست.",
        reply_markup=_back_kb(pb_id, "🔙 انصراف"))
    await cb.answer()


@partner_token_router.message(PartnerTokenFSM.new_owner_id, F.text)
async def pat_owner_got_id(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        await msg.answer("شناسهٔ نامعتبر است. فقط عدد بفرست.")
        return
    new_owner = int(raw)
    data = await state.get_data()
    pb_id, mode = data.get("pat_pb_id"), data.get("pat_mode")
    info = await _pb_info(pb_id)
    if not info:
        await state.clear()
        await msg.answer("ربات همکار یافت نشد.")
        return
    bot_id, cur_owner, uname, token = info
    if new_owner == cur_owner:
        await msg.answer("این که همان مالک فعلی است. شناسهٔ دیگری بفرست.")
        return
    await state.clear()

    if mode == "keep":
        async with Session() as s:
            br = await s.get(BotRow, bot_id)
            pb = await s.get(PartnerBot, pb_id)
            if not br or not pb:
                await msg.answer("ربات همکار یافت نشد.")
                return
            br.owner_id = new_owner
            pb.owner_id = new_owner          # بدونِ این، همکار به پنلش دسترسی ندارد
            await s.commit()
        await _invalidate(token)
        await msg.answer(
            f"✅ مالکیت ربات همکار @{uname or '—'} به کاربر "
            f"<code>{new_owner}</code> منتقل شد.")
        for uid, text in (
            (cur_owner, f"ℹ️ ربات همکار @{uname} از این پس در اختیار کاربر دیگری است."),
            (new_owner, f"🤝 ربات همکار @{uname} توسط مدیریت بات‌ساز به شما منتقل شد."),
        ):
            try:
                async with _temp_bot(token) as tb:
                    await tb.send_message(uid, text)
            except Exception:
                logger.exception("اطلاع‌رسانیِ انتقال مالکیت ناموفق بود")
        return

    # ── mode == "ask": از مالکِ جدید توکنِ خودش خواسته می‌شود ──
    async with Session() as s:
        existing = await s.scalar(select(OwnerTransferRequest).where(
            OwnerTransferRequest.old_bot_id == bot_id,
            OwnerTransferRequest.status == "pending_token"))
        if existing:
            await msg.answer("یک درخواست انتقال برای این ربات از قبل در جریان است.")
            return
        req = OwnerTransferRequest(old_bot_id=bot_id, requested_by=msg.from_user.id,
                                   new_owner_id=new_owner, status="pending_token")
        s.add(req)
        await s.commit()
        req_id = req.id

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("🔁 ارسال توکن ربات من", callback_data=f"pat:acc:{req_id}")
    ]])
    try:
        await bot.send_message(
            new_owner,
            "🤝 <b>انتقال ربات همکار به شما</b>\n\n"
            f"مدیریت بات‌ساز می‌خواهد ربات همکار @{uname} با توکن ربات خودت "
            "جایگزین و به تو منتقل شود.\n\n"
            "اگر قبول داری، دکمهٔ زیر را بزن و توکن ربات خودت (از @BotFather) را بفرست.",
            reply_markup=kb)
    except Exception:
        async with Session() as s:
            row = await s.get(OwnerTransferRequest, req_id)
            if row:
                await s.delete(row)
                await s.commit()
        await msg.answer(
            "نتونستم به مالک جدید پیام بدم؛ او باید اول همین ربات (بات‌ساز) را "
            "با /start شروع کرده باشد.")
        return
    await msg.answer("✅ درخواست برای مالک جدید ارسال شد.\n"
                     "به‌محض ارسال توکن توسط او، انتقال خودکار انجام می‌شود.")


@partner_token_router.callback_query(F.data.startswith("pat:acc:"))
async def pat_owner_accept(cb: CallbackQuery, state: FSMContext):
    """دکمه‌ای که مالکِ جدید در ربات مادر می‌بیند — این‌جا ادمین نیست."""
    req_id = int(cb.data.split(":")[2])
    async with Session() as s:
        req = await s.get(OwnerTransferRequest, req_id)
        if not req or req.status != "pending_token":
            await cb.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
            return
        if cb.from_user.id != req.new_owner_id:
            await cb.answer("این درخواست برای شما نیست.", show_alert=True)
            return
    await state.set_state(PartnerTokenFSM.owner_token)
    await state.update_data(pat_req_id=req_id)
    await cb.message.answer(
        "توکن ربات خودت را از @BotFather بگیر و همین‌جا بفرست.\n"
        "(چیزی شبیه <code>123456:ABC-...</code>)")
    await cb.answer()


@partner_token_router.message(PartnerTokenFSM.owner_token, F.text)
async def pat_owner_token_do(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data.get("pat_req_id")
    new_token = (msg.text or "").strip()
    if ":" not in new_token or len(new_token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return
    if await _token_taken(new_token):
        await msg.answer("این توکن قبلاً برای ربات دیگری ثبت شده. توکن دیگری بفرست.")
        return

    async with Session() as s:
        req = await s.get(OwnerTransferRequest, req_id) if req_id else None
        if not req or req.status != "pending_token" or msg.from_user.id != req.new_owner_id:
            await state.clear()
            await msg.answer("این درخواست دیگر معتبر نیست.")
            return
        bot_id, new_owner = req.old_bot_id, req.new_owner_id

    async with Session() as s:
        pb = await s.scalar(select(PartnerBot).where(PartnerBot.bot_id == bot_id))
        br = await s.get(BotRow, bot_id)
        if not pb or not br:
            await state.clear()
            await msg.answer("ربات همکار یافت نشد.")
            return
        pb_id, old_owner, old_uname, old_token = pb.id, pb.owner_id, br.username, br.token

    await state.clear()
    ok, err, new_bot_id, new_uname = await _apply_token_migration(bot_id, new_token)
    if not ok:
        await msg.answer(f"❌ {err or 'انتقال انجام نشد.'} دوباره تلاش کن.")
        return

    # مالکیت — هم روی ردیفِ ربات، هم روی ردیفِ همکار (این دومی فراموش‌شدنی است)
    async with Session() as s:
        nb_row = await s.get(BotRow, new_bot_id)
        if nb_row:
            nb_row.owner_id = new_owner
        pb2 = await s.get(PartnerBot, pb_id)
        if pb2:
            pb2.owner_id = new_owner
        req2 = await s.get(OwnerTransferRequest, req_id)
        if req2:
            req2.status = "done"
            req2.new_bot_id = new_bot_id
            req2.new_token = new_token
            req2.new_username = new_uname
            req2.decided_at = datetime.datetime.now()
        await s.commit()

    await _invalidate(old_token)
    await _invalidate(new_token)

    await msg.answer(
        f"✅ ربات همکار @{old_uname or '—'} (اکنون @{new_uname}) به شما منتقل و فعال شد.")
    try:
        await bot.send_message(
            old_owner, f"ℹ️ ربات همکار @{old_uname} به کاربر دیگری منتقل شد.")
    except Exception:
        logger.exception("اطلاع‌رسانی به مالک قبلی ناموفق بود")
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            f"✅ انتقال مالکیت ربات همکار انجام شد.\n"
            f"@{old_uname} → @{new_uname}\nمالک جدید: <code>{new_owner}</code>")
    except Exception:
        logger.exception("اطلاع‌رسانی به مدیر ناموفق بود")
