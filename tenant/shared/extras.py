"""
ماژول نظرسنجی، لینک دعوت و پیام‌های خودکار

قابلیت‌ها:
  • ساخت نظرسنجی / رای‌گیری
  • نمایش نتایج نظرسنجی به‌صورت نموداری (متنی)
  • لینک دعوت اختصاصی با ترکینگ
  • پاداش به ازای هر دعوت موفق
  • سیستم نظر و امتیازدهی (1-5 ستاره)
  • پیام خودکار خوش‌آمدگویی
"""
import datetime as _dt
import json
import secrets
import html as _html

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from config import Session
from models import (
    Bot as BotRow, BotReview, BotUser, Poll, PollVote,
    ReferralLink, ReferralJoin, WalletTxn, Setting,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

extras_router = Router()


async def _eget(bot_id: int, key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return row.value if row else default


async def _eset(bot_id: int, key: str, value: str):
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


async def get_bot_row_by_token(token: str) -> BotRow | None:
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == token))


def rial(n) -> str:
    return f"{int(n):,} تومان"


# ═══════════════════════════════════════════
#  نظرسنجی
# ═══════════════════════════════════════════

class PollFSM(StatesGroup):
    question = State()
    options = State()
    set_group = State()   # دریافت گروه نظرسنجی


@extras_router.callback_query(F.data.startswith("poll:vote:"))
async def poll_vote(cb: CallbackQuery, bot: Bot):
    from sqlalchemy.exc import IntegrityError

    parts = cb.data.split(":")
    poll_id, option_idx = int(parts[2]), int(parts[3])
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == cb.from_user.id))
        if not user:
            await cb.answer()
            return
        poll = await s.get(Poll, poll_id)
        if poll is not None and await _is_foreign(poll, bot):   # PATCH_TENANT_SCOPE_V1
            poll = None
        if not poll or not poll.is_active:
            await cb.answer("نظرسنجی بسته است.", show_alert=True)
            return
        # بررسی رای قبلی
        existing = await s.scalar(select(PollVote).where(
            PollVote.poll_id == poll_id,
            PollVote.user_id == user.id,
        ))
        if existing:
            if not poll.allow_multiple:
                await cb.answer("قبلاً رای داده‌ای!", show_alert=True)
                return
            # نظرسنجی چندرایی: گزینهٔ رای قبلی را به‌روزرسانی کن (نه ردیف جدید)
            existing.option_index = option_idx
        else:
            # ثبت رای؛ UniqueConstraint(poll_id, user_id) از رای تکراری حتی در
            # شرایط هم‌زمانی (دابل‌کلیک) جلوگیری می‌کند
            s.add(PollVote(poll_id=poll_id, user_id=user.id, option_index=option_idx))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            await cb.answer("قبلاً رای داده‌ای!", show_alert=True)
            return
    await cb.answer("✅ رای شما ثبت شد!")
    # به‌روزرسانی نتایج
    await _show_poll_results(cb.message, poll_id, bot_row.id, edit=True)


async def _show_poll_results(msg: Message, poll_id: int, bot_id: int, edit: bool = False):
    async with Session() as s:
        poll = await s.get(Poll, poll_id)
        if not poll:
            return
        options = json.loads(poll.options_json)
        total = await s.scalar(select(func.count(PollVote.id)).where(
            PollVote.poll_id == poll_id))
        vote_counts = []
        for i in range(len(options)):
            cnt = await s.scalar(select(func.count(PollVote.id)).where(
                PollVote.poll_id == poll_id,
                PollVote.option_index == i))
            vote_counts.append(cnt or 0)
    lines = [f"📊 <b>{poll.question}</b>\n"]
    for i, (opt, cnt) in enumerate(zip(options, vote_counts)):
        pct = (cnt / total * 100) if total else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        lines.append(f"{i+1}. {opt}\n   {bar} {cnt} رای ({pct:.0f}%)\n")
    lines.append(f"\n👥 مجموع: {total} رای")
    text = "\n".join(lines)
    kb_rows = []
    if poll.is_active:
        for i, opt in enumerate(options):
            kb_rows.append([InlineKeyboardButton(
                text=opt, callback_data=f"poll:vote:{poll_id}:{i}"
            )])
    if edit:
        try:
            await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


async def show_active_poll(bot: Bot, chat_id: int, bot_id: int):
    """ارسال نظرسنجی فعال به کاربر."""
    async with Session() as s:
        poll = await s.scalar(select(Poll).where(
            Poll.bot_id == bot_id, Poll.is_active == True))
    if not poll:
        return
    options = json.loads(poll.options_json)
    kb_rows = [[InlineKeyboardButton(
        text=opt, callback_data=f"poll:vote:{poll.id}:{i}"
    )] for i, opt in enumerate(options)]
    await bot.send_message(
        chat_id,
        f"📊 <b>{poll.question}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


# ─── پنل ادمین نظرسنجی ───

@extras_router.callback_query(F.data == "tadm:polls")
async def polls_admin(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    async with Session() as s:
        polls = (await s.scalars(
            select(Poll).where(Poll.bot_id == bot_row.id)
            .order_by(Poll.created_at.desc()).limit(10)
        )).all()
    rows = []
    for p in polls:
        status = "🟢" if p.is_active else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{status} {p.question[:30]}",
            callback_data=f"poll:adm:{p.id}"
        )])
    rows.append([InlineKeyboardButton(text="➕ نظرسنجی جدید", callback_data="poll:new")])
    rows.append([InlineKeyboardButton(text="👥 تنظیم گروه نظرسنجی", callback_data="poll:setgroup")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:admins", style=ButtonStyle.DANGER),
                 InlineKeyboardButton(text="🏠 پنل مدیریت", callback_data="tadm:menu", style=ButtonStyle.DANGER)])
    grp = await _eget(bot_row.id, "poll_group", "")
    await cb.message.edit_text(
        "📊 <b>مدیریت نظرسنجی‌ها</b>\n"
        f"گروه نظرسنجی: <code>{grp or '(ثبت نشده)'}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()


@extras_router.callback_query(F.data.startswith("poll:adm:"))
async def poll_admin_detail(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        poll = await s.get(Poll, pid)
        if poll is not None and await _is_foreign(poll, bot):   # PATCH_TENANT_SCOPE_V1
            poll = None
        total = await s.scalar(select(func.count(PollVote.id)).where(PollVote.poll_id == pid))
    if not poll:
        await cb.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 بستن" if poll.is_active else "🟢 بازکردن",
            callback_data=f"poll:toggle:{pid}"
        )],
        [InlineKeyboardButton(text="📤 ارسال به گروه نظرسنجی", callback_data=f"poll:send:{pid}")],
        [InlineKeyboardButton(text="📊 نتایج", callback_data=f"poll:results:{pid}")],
        [InlineKeyboardButton(text="🗑 حذف", callback_data=f"poll:del:{pid}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:polls", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(
        f"📊 {poll.question}\n👥 مجموع رای: {total}",
        reply_markup=kb
    )
    await cb.answer()


@extras_router.callback_query(F.data.startswith("poll:toggle:"))
async def poll_toggle(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        poll = await s.get(Poll, pid)
        if poll is not None and await _is_foreign(poll, bot):   # PATCH_TENANT_SCOPE_V1
            poll = None
        if poll:
            poll.is_active = not poll.is_active
            await s.commit()
    await poll_admin_detail(cb, bot)


@extras_router.callback_query(F.data.startswith("poll:results:"))
async def poll_results(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[2])
    bot_row = await get_bot_row_by_token(bot.token)
    await _show_poll_results(cb.message, pid, bot_row.id if bot_row else 0)
    await cb.answer()


@extras_router.callback_query(F.data.startswith("poll:del:"))
async def poll_delete(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    async with Session() as s:
        poll = await s.get(Poll, pid)
        if poll is not None and await _is_foreign(poll, cb.bot):   # PATCH_TENANT_SCOPE_V1
            poll = None
        if poll:
            await s.delete(poll)
            await s.commit()
    await cb.answer("🗑 نظرسنجی حذف شد", show_alert=True)


@extras_router.callback_query(F.data == "poll:new")
async def poll_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PollFSM.question)
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("سوال نظرسنجی را بنویس:")


@extras_router.message(PollFSM.question, F.text)
async def poll_question_save(msg: Message, state: FSMContext):
    await state.update_data(question=msg.text.strip())
    await state.set_state(PollFSM.options)
    await msg.answer(
        "گزینه‌ها را هر کدام در یک خط بنویس:\n"
        "مثال:\n"
        "بله\n"
        "خیر\n"
        "نمی‌دانم"
    )


@extras_router.message(PollFSM.options, F.text)
async def poll_options_save(msg: Message, state: FSMContext, bot: Bot):
    lines = [l.strip() for l in msg.text.split("\n") if l.strip()]
    if len(lines) < 2:
        await msg.answer("حداقل ۲ گزینه نیاز است.")
        return
    data = await state.get_data()
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        # غیرفعال کردن نظرسنجی‌های قبلی
        old_polls = (await s.scalars(
            select(Poll).where(Poll.bot_id == bot_row.id, Poll.is_active == True)
        )).all()
        for op in old_polls:
            op.is_active = False
        new_poll = Poll(
            bot_id=bot_row.id,
            question=data["question"],
            options_json=json.dumps(lines, ensure_ascii=False),
            is_active=True,
        )
        s.add(new_poll)
        await s.commit()
        new_id = new_poll.id
    await state.clear()
    grp = await _eget(bot_row.id, "poll_group", "")
    rows = [[InlineKeyboardButton(text="📤 ارسال به گروه نظرسنجی",
                                  callback_data=f"poll:send:{new_id}")],
            [InlineKeyboardButton(text="❌ فعلاً نه", callback_data=f"poll:adm:{new_id}")]]
    note = ""
    if not grp:
        note = ("\n\n⚠️ هنوز «گروه نظرسنجی» تنظیم نشده. برای ارسال، اول از پنل نظرسنجی "
                "گروه را تنظیم کن (ربات باید در آن گروه ادمین باشد).")
    await msg.answer(
        f"✅ نظرسنجی «{data['question']}» با {len(lines)} گزینه ساخته شد.\n\n"
        "می‌خواهی همین حالا برای رای‌گیری به گروه ارسال شود؟" + note,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@extras_router.callback_query(F.data.startswith("poll:send:"))
async def poll_send_group(cb: CallbackQuery, bot: Bot):
    """ارسال نظرسنجی به گروهِ تنظیم‌شده برای رای‌گیری."""
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    poll_id = int(cb.data.split(":")[2])
    grp = await _eget(bot_row.id, "poll_group", "")
    if not grp:
        await cb.answer("ابتدا گروه نظرسنجی را تنظیم کن.", show_alert=True)
        return
    async with Session() as s:
        poll = await s.get(Poll, poll_id)
        if poll is not None and await _is_foreign(poll, bot):   # PATCH_TENANT_SCOPE_V1
            poll = None
    if not poll:
        await cb.answer("نظرسنجی یافت نشد.", show_alert=True)
        return
    options = json.loads(poll.options_json)
    kb_rows = [[InlineKeyboardButton(text=opt, callback_data=f"poll:vote:{poll.id}:{i}")]
               for i, opt in enumerate(options)]
    try:
        await bot.send_message(
            int(grp) if str(grp).lstrip("-").isdigit() else grp,
            f"📊 <b>{poll.question}</b>\n\nنظر خود را ثبت کنید 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await cb.message.answer("✅ نظرسنجی به گروه ارسال شد.")
        await cb.answer("ارسال شد")
    except Exception as e:
        await cb.message.answer(
            "❌ ارسال به گروه ناموفق بود. مطمئن شو ربات در گروه عضو و ادمین است "
            "و آیدی گروه درست تنظیم شده.\n\n"
            f"<code>{type(e).__name__}: {str(e)[:120]}</code>")
        await cb.answer("خطا", show_alert=False)


@extras_router.callback_query(F.data == "poll:setgroup")
async def poll_setgroup_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    cur = await _eget(bot_row.id, "poll_group", "")
    await state.set_state(PollFSM.set_group)
    await cb.message.answer(
        "👥 <b>تنظیم گروه نظرسنجی</b>\n\n"
        f"گروه فعلی: <code>{cur or '(ثبت نشده)'}</code>\n\n"
        "۱) ابتدا ربات را در گروه <b>عضو و ادمین</b> کن.\n"
        "۲) سپس آیدی عددی گروه را بفرست (مثل <code>-1001234567890</code>)،\n"
        "یا یک پیام از همان گروه را برای ربات <b>فوروارد</b> کن تا آیدی‌اش را خودکار بردارم.")
    await cb.answer()


@extras_router.message(PollFSM.set_group)
async def poll_setgroup_save(msg: Message, state: FSMContext, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or msg.from_user.id != bot_row.owner_id:
        await state.clear()
        return
    gid = None
    # === PATCH_EXTRAS_FORWARD_ORIGIN_V1 ===
    fwd_origin = getattr(msg, "forward_origin", None)
    fwd_origin_type = getattr(fwd_origin, "type", None)
    if fwd_origin_type == "chat":
        gid = str(fwd_origin.sender_chat.id)
    elif fwd_origin_type == "channel":
        gid = str(fwd_origin.chat.id)
    else:
        # فوروارد از گروه (فیلدِ قدیمی — فقط برای سازگاری با نسخه‌های قدیمی)
        fc = getattr(msg, "forward_from_chat", None)
        if fc and getattr(fc, "id", None):
            gid = str(fc.id)
    if gid is None and msg.text:
        t = msg.text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        if t.lstrip("-").isdigit():
            gid = t
    if not gid:
        await msg.answer("❌ آیدی نامعتبر بود. یک آیدی عددی گروه بفرست یا پیامی از گروه فوروارد کن.")
        return
    await _eset(bot_row.id, "poll_group", gid)
    await state.clear()
    # تست دسترسی ربات به گروه
    try:
        await bot.send_message(int(gid), "✅ این گروه به‌عنوان «گروه نظرسنجی» تنظیم شد.")
        await msg.answer(f"✅ گروه نظرسنجی تنظیم شد: <code>{gid}</code>\nپیام تست به گروه ارسال شد.")
    except Exception as e:
        await msg.answer(
            f"⚠️ گروه ذخیره شد (<code>{gid}</code>) ولی ارسال پیام تست ناموفق بود.\n"
            "مطمئن شو ربات در گروه عضو و ادمین است.\n"
            f"<code>{type(e).__name__}: {str(e)[:120]}</code>")


# ═══════════════════════════════════════════
#  لینک دعوت با ترکینگ
# ═══════════════════════════════════════════

async def get_or_create_referral(bot_id: int, user: BotUser, bot_username: str, reward: float = 0) -> ReferralLink:
    from sqlalchemy.exc import IntegrityError
    async with Session() as s:
        ref = (await s.scalars(select(ReferralLink).where(
            ReferralLink.bot_id == bot_id,
            ReferralLink.user_id == user.id
        ).order_by(ReferralLink.id.asc()))).first()
        if not ref:
            code = secrets.token_urlsafe(8)
            ref = ReferralLink(
                bot_id=bot_id, user_id=user.id,
                code=code, reward_per_join=reward
            )
            s.add(ref)
            try:
                await s.commit()
            except IntegrityError:
                # === PATCH_RACE_SAFE_REFERRAL_V1 ===
                # اگر دو درخواستِ هم‌زمان (مثلاً به‌خاطر ریترایِ وبهوکِ تلگرام) هر دو
                # تلاش کنند لینکِ رفرال بسازند، فقط ردیفِ موجود را برمی‌گردانیم.
                await s.rollback()
            async with Session() as s2:
                ref = (await s2.scalars(select(ReferralLink).where(
                    ReferralLink.bot_id == bot_id,
                    ReferralLink.user_id == user.id
                ).order_by(ReferralLink.id.asc()))).first()
    return ref


async def build_referral_screen(bot: Bot, bot_row, user, back_rows: list | None = None):
    """=== PATCH_REFERRAL_SCREEN_POINTS_V1 === صفحهٔ «زیرمجموعه‌گیری»: تعدادِ دعوت‌شدگان
    و امتیاز، لینکِ دعوت، (در صورتِ فعال‌بودنِ امتیاز) دکمهٔ «🏅 تبدیل امتیاز»."""
    reward_raw = await _get_referral_reward(bot_row.id)
    me = await bot.get_me()
    ref = await get_or_create_referral(bot_row.id, user, me.username, reward_raw)
    link = f"https://t.me/{me.username}?start=ref_{ref.code}"
    # === PATCH_REFERRAL_SCREEN_POINTS_V1 === اول آمار (دعوت‌شدگان/امتیاز)، زیرش لینک
    _stats = (f"👥 دعوت‌شدگان: {ref.join_count}\n"
              f"👆 کلیک‌ها: {ref.click_count}\n")
    _link_block = ("\n🔗 <b>لینک دعوت شما</b> (برای کپی روی آن بزن):\n"
                   f"<code>{link}</code>\n\n")
    text = ""
    # === PATCH_REF_PERCENT_TEXT_V1 ===
    # در حالتِ درصدی، مبلغِ ثابت صفر است و شرطِ قبلی کلِ بخشِ پاداش را
    # پنهان می‌کرد؛ یعنی کاربر اصلاً نمی‌فهمید پاداشی در کار هست.
    _rtype = await _eget(bot_row.id, "referral_reward_type", "fixed")
    _rpct = await _eget(bot_row.id, "referral_reward_pct", "5")
    if _rtype == "percent_all":
        text += (f"🎁 پاداش: <b>{_rpct}٪</b> از هر خریدِ دوستت\n"
                 "💡 مادام‌العمر — از تمامِ خریدهایش سهم می‌گیری.\n")
    elif _rtype == "percent_first":
        text += (f"🎁 پاداش: <b>{_rpct}٪</b> از اولین خریدِ دوستت\n"
                 "💡 بعد از اولین خریدِ موفقِ او پرداخت می‌شود.\n")
    elif reward_raw > 0:
        _mode = await _eget(bot_row.id, "referral_reward_mode", "join")
        if _mode == "join":
            text += f"🎁 پاداش هر دعوت: {rial(reward_raw)}\n"
        else:
            text += (f"🎁 پاداش هر دعوت: {rial(reward_raw)}\n"
                     "💡 پاداش بعد از اولین خرید دوستت پرداخت می‌شود.\n")
    # امتیازِ رفرال + دکمهٔ تبدیل
    _rows = []
    try:
        from tenant.shared.ref_points import (points_enabled, get_points, per_join,
                                              convert_cost, reward_summary)
        if await points_enabled(bot_row.id):
            _pts = await get_points(bot_row.id, user.id)
            _pj = await per_join(bot_row.id)
            _cost = await convert_cost(bot_row.id)
            _stats += f"🏅 امتیازِ تو: <b>{_pts}</b>\n"
            text += f"⭐️ هر دعوت = {_pj} امتیاز\n"
            if _cost > 0:
                text += f"🔁 هر {_cost} امتیاز = {await reward_summary(bot_row.id)}\n"
                _rows.append([InlineKeyboardButton(
                    text=f"🏅 تبدیل امتیاز ({_cost} امتیاز)",
                    callback_data="rpt:convert", style=ButtonStyle.SUCCESS)])
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (امتیازِ رفرال در صفحهٔ زیرمجموعه‌گیری)")
    # === PATCH_REF_BANNER_V1 === متنِ دلخواهِ مالک + دکمهٔ بنر
    _custom = (await _eget(bot_row.id, "ref_page_text", "")).strip()
    if _custom:
        _cnt, _clk = str(ref.join_count), str(ref.click_count)
        _pts_s = "0"
        try:
            from tenant.shared.ref_points import get_points as _gp
            _pts_s = str(await _gp(bot_row.id, user.id))
        except Exception:
            logger.debug("خواندنِ امتیازِ رفرال ناموفق بود", exc_info=True)
        head = (_custom.replace("{link}", f"<code>{link}</code>").replace("{count}", _cnt)
                .replace("{clicks}", _clk).replace("{points}", _pts_s))
        if "{link}" not in _custom:
            head += "\n\n" + _link_block.strip()
        text = head + "\n\n" + text
    else:
        text = "🔗 <b>زیرمجموعه‌گیری</b>\n\n" + _stats + _link_block + text
    if (await _eget(bot_row.id, "ref_banner_text", "")).strip() or (await _eget(bot_row.id, "ref_banner_photo", "")).strip():
        _rows.append([InlineKeyboardButton(text="📣 دریافتِ بنرِ دعوت (برایِ فرستادن به دوستات)",
                                           callback_data="ref:banner", style=ButtonStyle.PRIMARY)])
    _rows.extend(back_rows if back_rows is not None else
                 [[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ref:back")]])
    return text, InlineKeyboardMarkup(inline_keyboard=_rows)


@extras_router.callback_query(F.data == "ref:mylink")
async def ref_my_link(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == cb.from_user.id))
    if not user:
        await cb.answer()
        return
    text, kb = await build_referral_screen(bot, bot_row, user)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@extras_router.callback_query(F.data == "ref:banner")
async def ref_banner_send(cb: CallbackQuery, bot: Bot):
    """=== PATCH_REF_BANNER_V1 === بنرِ دعوتِ مالک با لینکِ اختصاصیِ همین کاربر."""
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        return await cb.answer()
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id, BotUser.telegram_id == cb.from_user.id))
    if not user:
        return await cb.answer()
    photo = (await _eget(bot_row.id, "ref_banner_photo", "")).strip()
    btext = (await _eget(bot_row.id, "ref_banner_text", "")).strip()
    if not photo and not btext:
        return await cb.answer("بنری تنظیم نشده.", show_alert=True)
    reward_raw = await _get_referral_reward(bot_row.id)
    me = await bot.get_me()
    ref = await get_or_create_referral(bot_row.id, user, me.username, reward_raw)
    link = f"https://t.me/{me.username}?start=ref_{ref.code}"
    cap = btext.replace("{link}", link) if "{link}" in btext else (btext + ("\n\n" if btext else "") + link)
    await cb.answer()
    try:
        if photo:
            await cb.message.answer_photo(photo, caption=cap[:1000])
        else:
            await cb.message.answer(cap)
        await cb.message.answer("👆 همین پیام را برایِ دوستانت فوروارد کن؛ لینکِ بالا مخصوصِ خودته 🙌")
    except Exception:
        logger.exception("ارسالِ بنرِ دعوت ناموفق بود")
        await cb.message.answer(cap)


@extras_router.callback_query(F.data == "ref:back")
async def ref_back(cb: CallbackQuery, bot: Bot):
    """بازگشت از صفحهٔ زیرمجموعه‌گیری به خانه."""
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    from tenant import main_kb_or_remove, main_inline_kb
    menu_mode = await _eget(bot_row.id, "menu_mode", "reply")
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if menu_mode == "inline":
        ikb = await main_inline_kb(bot_row.id, bot_row.owner_id, cb.from_user.id)
        if ikb:
            await bot.send_message(cb.from_user.id, "🏠 منوی اصلی:", reply_markup=ikb)
            await cb.answer()
            return
    kb = await main_kb_or_remove(bot_row.id, bot_row.owner_id, cb.from_user.id)
    if kb:
        await bot.send_message(cb.from_user.id, "🏠 منوی اصلی:", reply_markup=kb)
    await cb.answer()


async def _get_referral_reward(bot_id: int) -> float:
    async with Session() as s:
        from models import Setting
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "referral_reward"))
        en = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "referral_reward_enabled"))
    # اگر پاداش غیرفعال شده باشد، صفر برگردان
    if en is not None and en.value == "0":
        return 0
    try:
        return float(row.value) if row else 0
    except (ValueError, AttributeError):
        return 0


async def handle_referral_join(bot_id: int, new_user: BotUser, ref_code: str, bot: Bot):
    """وقتی کاربر جدید از لینک دعوت وارد می‌شود — فقط ثبت می‌کند.

    پاداش فوری داده نمی‌شود. پاداش فقط بعد از اولین خرید موفق زیرمجموعه
    از طریق handle_referral_purchase پرداخت خواهد شد.

    محافظت‌ها:
      • self-referral: دعوت‌شده و دعوت‌کننده یکی نباشند.
      • یک‌بار به ازای هر کاربر: UniqueConstraint روی (bot_id, referred_telegram_id).
    """
    from sqlalchemy.exc import IntegrityError

    async with Session() as s:
        ref = await s.scalar(select(ReferralLink).where(
            ReferralLink.bot_id == bot_id,
            ReferralLink.code == ref_code
        ))
        if not ref:
            return
        if ref.user_id == new_user.id:
            return
        s.add(ReferralJoin(
            bot_id=bot_id, referral_link_id=ref.id, inviter_user_id=ref.user_id,
            referred_telegram_id=new_user.telegram_id,
            reward_paid=0,
            purchase_rewarded=False,
        ))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return  # قبلاً ثبت شده
        ref.join_count += 1
        ref.click_count += 1
        inviter_user_id = ref.user_id
        inviter_row = await s.get(BotUser, inviter_user_id)
        inviter_tg_for_notify = inviter_row.telegram_id if inviter_row else None
        await s.commit()

    # === PATCH_REFERRAL_POINTS_V1 ===
    # امتیازِ رفرال (مستقل از پاداشِ تومانی): اگر مالکِ ربات «امتیازگیری با
    # رفرال» را روشن کرده باشد، به کیفِ امتیازِ دعوت‌کننده اضافه می‌شود. این
    # امتیاز بعداً توسطِ دکمه‌های «گیتِ دسترسی با ملاکِ امتیاز» خرج می‌شود.
    try:
        from tenant.shared.ref_points import award_for_join   # === PATCH_REFPOINT_REQUIRE_ORDER_V1 ===
        await award_for_join(bot_id, inviter_user_id, new_user.telegram_id)
    except Exception:
        logger.exception("خطا در افزودنِ امتیازِ رفرال")

    # پرداخت پاداش هنگام عضویت (حالت پیش‌فرض) — مگر اینکه مالک حالت
    # «بعد از اولین خرید» را انتخاب کرده باشد.
    mode = await _eget(bot_id, "referral_reward_mode", "join")
    if mode != "join":
        # === PATCH_REFERRAL_JOIN_NOTIFY_V1 ===
        # حالتِ «پاداش فقط بعد از خرید»: دعوت‌کننده تا الان هیچ پیامی
        # دربارهٔ پیوستنِ زیرمجموعه‌اش نمی‌گرفت. حالا یک اطلاع‌رسانیِ سادهٔ
        # بدونِ پاداش فرستاده می‌شود تا فوراً از پیوستنِ دوستش باخبر شود.
        if inviter_tg_for_notify:
            try:
                await bot.send_message(
                    inviter_tg_for_notify,
                    "🎉 یک دوست با لینک دعوت شما وارد ربات شد!"
                )
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    reward = await _get_referral_reward(bot_id)
    if reward <= 0:
        # === PATCH_REFERRAL_JOIN_NOTIFY_V1 ===
        # پاداشِ رفرال غیرفعال است؛ همان اطلاع‌رسانیِ سادهٔ بدونِ پاداش.
        if inviter_tg_for_notify:
            try:
                await bot.send_message(
                    inviter_tg_for_notify,
                    "🎉 یک دوست با لینک دعوت شما وارد ربات شد!"
                )
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        rj = await s.scalar(select(ReferralJoin).where(
            ReferralJoin.bot_id == bot_id,
            ReferralJoin.referred_telegram_id == new_user.telegram_id,
            ReferralJoin.purchase_rewarded == False,  # noqa: E712
        ).with_for_update())
        if not rj:
            return
        inviter = await s.get(BotUser, rj.inviter_user_id, with_for_update=True)
        if not inviter:
            return
        inviter.balance = float(inviter.balance) + reward
        rj.reward_paid = reward
        rj.purchase_rewarded = True  # جلوگیری از پرداخت دوباره در حالت خرید
        s.add(WalletTxn(
            bot_id=bot_id, user_id=inviter.id,
            amount=reward, kind="referral",
            note=f"پاداش دعوت کاربر {new_user.telegram_id}"
        ))
        await s.commit()
        inviter_tg_id = inviter.telegram_id
        _log_inviter, _log_bal = inviter, float(inviter.balance)
    # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
    # پاداشِ دعوت هم یک افزایشِ موجودی است و باید در کانالِ لاگ بیاید.
    try:
        from tenant.shared.tx_channel import tx_log
        await tx_log(bot, bot_id, buyer=_log_inviter, amount=reward, gateway="referral",
                     old_balance=_log_bal - reward, new_balance=_log_bal,
                     note=f"دعوتِ کاربر {new_user.telegram_id}")
    except Exception:
        logger.exception("tx_log (referral join)")
    try:
        await bot.send_message(
            inviter_tg_id,
            f"🎉 یک نفر با لینک دعوتت وارد ربات شد!\n"
            f"🎁 {rial(reward)} به کیف پولت اضافه شد."
        )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _last_purchase_amount(bot_id: int, buyer_telegram_id: int) -> float:
    """آخرین کسرِ کیف‌پولِ خریدار (۱۰ دقیقهٔ اخیر) — برایِ فراخوان‌هایی که مبلغِ خرید را پاس نمی‌دهند."""
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_telegram_id))
        if not u:
            return 0.0
        tx = await s.scalar(select(WalletTxn).where(
            WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id, WalletTxn.amount < 0,
            WalletTxn.kind.notin_(("admin", "referral", "transfer")),
            WalletTxn.created_at >= _dt.datetime.utcnow() - _dt.timedelta(minutes=10),
        ).order_by(WalletTxn.id.desc()).limit(1))
    return abs(float(tx.amount)) if tx else 0.0


async def handle_referral_purchase(bot_id: int, buyer_telegram_id: int, bot: Bot,
                                   amount: float | None = None):
    """بعد از اولین خرید موفق زیرمجموعه فراخوانی می‌شود و پاداش می‌دهد.

    اگر این کاربر از لینک دعوت وارد شده باشد و هنوز پاداش خریدش داده نشده،
    پاداش به دعوت‌کننده پرداخت می‌شود.
    """
    mode = await _eget(bot_id, "referral_reward_mode", "join")
    # === PATCH_REF_PERCENT_V1 ===
    # سه حالت: مبلغِ ثابت، درصدی از اولین خرید، درصدی از هر خرید.
    rtype = await _eget(bot_id, "referral_reward_type", "fixed")
    # === PATCH_REF_PERCENT_FIX_V2 === (Fadayi) پاداشِ درصدی همیشه «هنگامِ خرید» است و به حالتِ «عضویت/خرید» ربطی ندارد؛
    # قبلاً اگر مالک حالت را روی «عضویت» می‌گذاشت، درصدی هرگز پرداخت نمی‌شد. فقط «مبلغِ ثابت» تابعِ این حالت است.
    if rtype not in ("percent_first", "percent_all") and mode != "purchase":
        return  # در حالت «هنگام عضویت»، پاداش قبلاً در handle_referral_join پرداخت شده
    if await _eget(bot_id, "referral_reward_enabled", "1") != "1":
        return
    if rtype in ("percent_first", "percent_all") and not amount:
        amount = await _last_purchase_amount(bot_id, buyer_telegram_id)   # فراخوان‌هایی که مبلغ نمی‌دهند
    try:
        pct = float(await _eget(bot_id, "referral_reward_pct", "5") or 5)
    except Exception:
        pct = 5.0

    if rtype in ("percent_first", "percent_all"):
        if not amount or float(amount) <= 0 or pct <= 0:
            return  # بدونِ مبلغِ خرید، پاداشِ درصدی معنا ندارد
        reward = round(float(amount) * pct / 100.0)
    else:
        reward = await _get_referral_reward(bot_id)
    if reward <= 0:
        return

    _lifetime = (rtype == "percent_all")

    async with Session() as s:
        # === PATCH_WALLET_ROW_LOCK_V1 ===
        _q = select(ReferralJoin).where(
            ReferralJoin.bot_id == bot_id,
            ReferralJoin.referred_telegram_id == buyer_telegram_id)
        if not _lifetime:
            # حالتِ «یک‌بار»: فقط اگر هنوز پاداش نگرفته
            _q = _q.where(ReferralJoin.purchase_rewarded == False)  # noqa: E712
        rj = await s.scalar(_q.with_for_update())
        if not rj:
            return  # یا از لینک نیامده یا قبلاً پاداش داده شده
        # === PATCH_REF_DURATION_V1 === «مدتِ پاداش»: فقط تا N روز بعد از عضویتِ دعوت‌شده (۰ = بدونِ محدودیت)
        if rtype in ("percent_first", "percent_all"):
            try:
                _days = int(float(await _eget(bot_id, "referral_reward_days", "0") or 0))
            except Exception:
                _days = 0
            if _days > 0 and rj.created_at and (_dt.datetime.utcnow() - rj.created_at) > _dt.timedelta(days=_days):
                return

        inviter = await s.get(BotUser, rj.inviter_user_id, with_for_update=True)
        if not inviter:
            return

        inviter.balance = float(inviter.balance) + reward
        # PATCH_REF_PERCENT_V1 — در حالتِ مادام‌العمر مبلغ‌ها جمع می‌شوند
        rj.reward_paid = float(rj.reward_paid or 0) + reward if _lifetime else reward
        rj.purchase_rewarded = True
        _note = (f"پاداش {pct:g}٪ از خریدِ کاربر {buyer_telegram_id}"
                 if rtype in ("percent_first", "percent_all")
                 else f"پاداش دعوت کاربر {buyer_telegram_id} (اولین خرید)")
        s.add(WalletTxn(
            bot_id=bot_id, user_id=inviter.id,
            amount=reward, kind="referral", note=_note
        ))
        await s.commit()
        inviter_tg_id = inviter.telegram_id
        _log_inviter, _log_bal = inviter, float(inviter.balance)

    # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
    try:
        from tenant.shared.tx_channel import tx_log
        await tx_log(bot, bot_id, buyer=_log_inviter, amount=reward, gateway="referral",
                     old_balance=_log_bal - reward, new_balance=_log_bal, note=_note)
    except Exception:
        logger.exception("tx_log (referral purchase)")

    try:
        await bot.send_message(
            inviter_tg_id,
            f"🎉 زیرمجموعه‌ات اولین خریدش را انجام داد!\n"
            f"🎁 {rial(reward)} به کیف پولت اضافه شد."
        )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════════════════════════
#  سیستم نظر و امتیاز
# ═══════════════════════════════════════════

class ReviewFSM(StatesGroup):
    comment = State()


@extras_router.callback_query(F.data == "review:back")
async def review_back(cb: CallbackQuery, bot: Bot):
    """بازگشت از صفحات امتیازدهی/نظرات به منوی اصلیِ ربات."""
    from tenant import get_bot_row, main_inline_kb, main_kb_or_remove, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "menu_mode", "reply") == "inline":
        ikb = await main_inline_kb(row.id, row.owner_id, cb.from_user.id)
        if ikb:
            try:
                await cb.message.edit_text("🏠 منوی اصلی:", reply_markup=ikb)
            except Exception:
                await cb.message.answer("🏠 منوی اصلی:", reply_markup=ikb)
    else:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
        await cb.message.answer("🏠 منوی اصلی:", reply_markup=kb)
    await cb.answer()


@extras_router.callback_query(F.data.startswith("review:rate:"))
async def review_rate(cb: CallbackQuery, state: FSMContext, bot: Bot):
    rating = int(cb.data.split(":")[2])
    await state.update_data(rating=rating)
    await state.set_state(ReviewFSM.comment)
    from tenant import smart_cancel_kb
    await cb.message.answer(
        f"{'⭐' * rating} امتیاز {rating} ثبت شد.\n"
        f"نظر یا توضیحی داری؟ (یا /skip):",
        reply_markup=await smart_cancel_kb(bot),
    )
    await cb.answer()


@extras_router.message(ReviewFSM.comment, F.text)
async def review_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    rating = data.get("rating", 5)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        from tenant import get_bot_row, main_kb_or_remove
        row = await get_bot_row(bot)
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    comment = "" if raw == "/skip" else msg.html_text  # حفظ قالب‌بندی تلگرام
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == msg.from_user.id))
        if not user:
            await state.clear()
            return
        # بررسی وجود نظر قبلی
        existing = await s.scalar(select(BotReview).where(
            BotReview.bot_id == bot_row.id,
            BotReview.user_id == user.id))
        # === PATCH_REVIEW_MANUAL_APPROVE_REVERT_V1 ===
        # برگشت به رفتارِ قبلی: نظر تا تاییدِ دستیِ ادمینِ ربات نمایش داده
        # نمی‌شود (نه در میانگین، نه در لیستِ نظرات).
        if existing:
            existing.rating = rating
            existing.comment = comment
            existing.is_approved = False
        else:
            s.add(BotReview(
                bot_id=bot_row.id, user_id=user.id,
                rating=rating, comment=comment
            ))
        await s.commit()
    await state.clear()
    from tenant import inline_back_kb
    await msg.answer("✅ نظر شما ثبت شد.",
                     reply_markup=await inline_back_kb(bot_row.id))
    # اطلاع به ادمین برای تایید
    await bot.send_message(
        bot_row.owner_id,
        f"⭐ <b>نظر جدید</b>\n"
        f"امتیاز: {'⭐' * rating}\n"
        f"نظر: {comment or '—'}\n"
        f"کاربر: {msg.from_user.full_name}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ تایید", callback_data=f"review:approve:{user.id}"),
        ]])
    )


def rate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{i}⭐", callback_data=f"review:rate:{i}")
        for i in range(1, 6)
    ]])


@extras_router.callback_query(F.data == "review:show")
async def review_show_form(cb: CallbackQuery, bot: Bot):
    """PATCH_REVIEW_UNIFIED_PAGE_V1 — همون صفحهٔ امتیازدهی، هم میانگینِ فعلی رو
    نشون می‌ده هم دکمهٔ «نظراتِ کاربران» رو زیرِ ستاره‌ها می‌ذاره، تا کاربر
    مجبور نباشه برای دیدنِ میانگین به صفحهٔ جدا بره."""
    bot_row = await get_bot_row_by_token(bot.token)
    avg = count = None
    if bot_row:
        async with Session() as s:
            avg = await s.scalar(
                select(func.avg(BotReview.rating)).where(
                    BotReview.bot_id == bot_row.id, BotReview.is_approved == True
                )
            )
            count = await s.scalar(
                select(func.count()).select_from(BotReview).where(
                    BotReview.bot_id == bot_row.id, BotReview.is_approved == True
                )
            ) or 0
    if avg:
        avg_line = f"⭐ میانگین امتیاز: <b>{avg:.1f}</b> (از {count} نظر)\n\n"
    else:
        avg_line = "⭐ میانگین امتیاز: هنوز نظری ثبت نشده\n\n"
    kb_rows = list(rate_kb().inline_keyboard)
    kb_rows.append([InlineKeyboardButton(text="💬 نظرات کاربران", callback_data="review:list")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="review:back")])
    await cb.message.answer(
        f"⭐ <b>امتیازدهی به ربات</b>\n\n{avg_line}چند ستاره می‌دی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await cb.answer()


@extras_router.callback_query(F.data.startswith("review:approve:"))
async def review_approve(cb: CallbackQuery, bot: Bot):
    user_id = int(cb.data.split(":")[2])
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        review = await s.scalar(select(BotReview).where(
            BotReview.bot_id == bot_row.id,
            BotReview.user_id == user_id
        ))
        if review:
            review.is_approved = True
            await s.commit()
    await cb.answer("✅ نظر تایید شد", show_alert=True)
    await cb.message.edit_reply_markup()


@extras_router.callback_query(F.data == "review:list")
async def review_list(cb: CallbackQuery, bot: Bot):
    """PATCH_REVIEW_UNIFIED_PAGE_V1 — لیستِ نظراتِ کاربران، هرکدام با نامِ
    ثبت‌کننده کنارِ نظرش (میانگین دیگه این‌جا تکرار نمی‌شه، تویِ صفحهٔ
    امتیازدهی نشون داده می‌شه)."""
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        rows = (await s.execute(
            select(BotReview, BotUser).join(BotUser, BotReview.user_id == BotUser.id).where(
                BotReview.bot_id == bot_row.id,
                BotReview.is_approved == True
            ).order_by(BotReview.created_at.desc()).limit(10)
        )).all()
    if not rows:
        await cb.message.edit_text(
            "هنوز نظری ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="review:show")]
            ])
        )
        return
    lines = ["💬 <b>نظرات کاربران</b>\n┈┅━━⊷⊰✨⊱⊶━━┅┈"]
    for review, user in rows:
        name = user.full_name or user.username or "کاربر"
        lines.append(f"{'⭐' * review.rating}  —  <b>{_html.escape(name)}</b>\n{review.comment or '—'}\n")
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="review:show")]
        ])
    )
    await cb.answer()


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id


# ═══════════════════════════════════════════
#  === PATCH_REF_VS_RECONCILE_V1 === پاداشِ دعوت برایِ خریدهایِ «خدماتِ مجازی»
# ═══════════════════════════════════════════
# گزارشِ Fadayi: پاداشِ درصدیِ زیرمجموعه‌گیری اصلاً کار نمی‌کرد. در ربات‌هایِ خدماتِ مجازی (استارز/تون/گیفت/پریمیوم/
# ری‌اکشن/SMM) هیچ‌جا handle_referral_purchase صدا زده نمی‌شد. به‌جایِ دست‌بردن در ده‌ها مسیرِ پرداخت (کیف‌پول/کارت/
# درگاه/دستی)، این حلقه سفارش‌هایِ «تحویل‌شده» را می‌بیند و برایِ هر سفارش دقیقاً یک‌بار (ReferralOrderReward یکتا) پاداش می‌دهد.

_VS_DONE = {"gift": ("sent", "completed"), "smm": ("completed", "partial")}


async def referral_vs_reconcile_once() -> int:
    from sqlalchemy.exc import IntegrityError
    from models import (Bot as BotRow, GiftOrder, SmmOrder, ReferralJoin, ReferralOrderReward, Setting)
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    paid = 0
    async with Session() as s:
        bots = (await s.scalars(select(BotRow).where(
            BotRow.status == "active", BotRow.field == "virtual_service"))).all()
    for brow in bots:
        try:
            if await _eget(brow.id, "referral_reward_enabled", "1") != "1":
                continue
            rtype = await _eget(brow.id, "referral_reward_type", "fixed")
            mode = await _eget(brow.id, "referral_reward_mode", "join")
            if rtype == "fixed" and mode != "purchase":
                continue
            # فقط سفارش‌هایِ ساخته‌شده بعد از فعال‌شدنِ این قابلیت (بدونِ پاداشِ عطفِ‌به‌ماسبق)
            since_raw = await _eget(brow.id, "ref_vs_since", "")
            if not since_raw:
                await _eset(brow.id, "ref_vs_since", _dt.datetime.utcnow().isoformat())
                continue
            since = _dt.datetime.fromisoformat(since_raw)
            async with Session() as s:
                g_orders = (await s.execute(
                    select(GiftOrder, BotUser.telegram_id).join(BotUser, BotUser.id == GiftOrder.buyer_user_id)
                    .where(GiftOrder.bot_id == brow.id, GiftOrder.status.in_(_VS_DONE["gift"]),
                           GiftOrder.created_at >= since)
                    .order_by(GiftOrder.id.desc()).limit(200))).all()
                s_orders = (await s.scalars(select(SmmOrder).where(
                    SmmOrder.bot_id == brow.id, SmmOrder.status.in_(_VS_DONE["smm"]),
                    SmmOrder.created_at >= since).order_by(SmmOrder.id.desc()).limit(200))).all()
            items = [("gift", o.id, tg, float(o.price_toman or 0) * max(1, int(o.quantity or 1)))
                     for o, tg in g_orders]
            items += [("smm", o.id, int(o.user_telegram_id), float(o.price_toman or 0)) for o in s_orders]
            if not items:
                continue
            tb = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                for kind, oid, tg, amount in items:
                    async with Session() as s:
                        has_join = await s.scalar(select(ReferralJoin.id).where(
                            ReferralJoin.bot_id == brow.id, ReferralJoin.referred_telegram_id == tg))
                        if not has_join or amount <= 0:
                            continue
                        s.add(ReferralOrderReward(bot_id=brow.id, kind=kind, order_id=oid))
                        try:
                            await s.commit()
                        except IntegrityError:
                            await s.rollback()
                            continue      # قبلاً پردازش شده
                    await handle_referral_purchase(brow.id, tg, tb, amount=amount)
                    paid += 1
            finally:
                await tb.session.close()
        except Exception:
            logger.exception("referral reconcile failed (bot=%s)", brow.id)
    return paid


async def referral_vs_reconcile_loop() -> None:
    import asyncio
    while True:
        try:
            await referral_vs_reconcile_once()
        except Exception:
            logger.exception("referral reconcile loop failed")
        await asyncio.sleep(90)
