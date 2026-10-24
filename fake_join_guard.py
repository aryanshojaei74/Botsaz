"""
fake_join_guard.py — شناساییِ حمله‌ی جوینِ انبوه (burst join) در کانال/گروهِ
محافظت‌شده و کیکِ دسته‌جمعی با یک دکمه.

منطق:
    هر بار عضوِ جدیدی می‌آد، timestamp‌اش ثبت می‌شه. اگه یک زنجیره از
    جوین‌ها پیدا شه که فاصله‌ی هرکدوم تا بعدی حداکثر BURST_GAP_MAX_SEC
    ثانیه باشه (پشتِ سرِ هم، بدونِ وقفه‌ی طولانی) و تعدادِ کلِ زنجیره از
    BURST_MIN_COUNT بیشتر بشه → همه‌ی اعضایِ اون زنجیره «مشکوک به فیک»
    علامت می‌خورن و در دیتابیس ذخیره می‌شن.

    وقتی زنجیره برایِ اولین بار به آستانه رسید، یک پیام به ادمین
    (PLATFORM_ADMIN_ID) با دکمه‌ی inline فرستاده می‌شه: «🚨 حذف N عضو فیک»
    که با یک تپ همه‌شونو بن+آنبن (= کیک) می‌کنه.

نکته‌ی مهم دربارهٔ کانال‌ها:
    برایِ کانال (channel)، آپدیتِ جوین به‌صورتِ پیامِ «new_chat_members»
    نمی‌آد؛ باید هندلرِ chat_member رو داشته باشیم و در setWebhook مقدارِ
    allowed_updates حتماً شاملِ "chat_member" باشه (پیش‌فرضِ تلگرام این
    آپدیت رو نمی‌فرسته). این فایل هردو حالت (گروه + کانال) رو پوشش می‌ده.

نصب (این نسخه از قبل کاملاً سیم‌کشی شده — فقط برایِ مستندسازی):
    1) مدلِ FakeJoinCandidate در models.py اضافه شده (کنارِ GroupGuardCaptcha).
    2) روتر در platform_bot.py include شده:
           from fake_join_guard import fake_join_router
           router.include_router(fake_join_router)
    3) در main.py و main_platform.py، set_webhook پلتفرم حالا با
       allowed_updates=PLATFORM_ALLOWED_UPDATES (تعریف‌شده در
       webhook_utils.py) صدا زده می‌شه تا آپدیتِ "chat_member" (لازم برایِ
       شناساییِ جوینِ کانال) هم برسه.
    4) این ماژول از همون pset هایِ group_guard استفاده می‌کنه، یعنی فقط
       رویِ چتی که با /فعال_محافظ وصل شده فعاله (group_guard_chat_id).
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import time
from collections import deque

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from config import PLATFORM_ADMIN_ID, Session
from models import FakeJoinCandidate

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

fake_join_router = Router()

# ── تنظیماتِ آستانه (خواستِ شما: ۱ تا ۱۰ ثانیه، بالایِ ۲۰۰ نفر) ──
BURST_GAP_MAX_SEC = 10       # حداکثر فاصله‌ی مجاز بینِ دو جوینِ پیاپی
BURST_MIN_COUNT = 200        # حداقلِ تعداد برایِ اینکه «قطعاً فیک» باشه
BURST_ALERT_COOLDOWN_SEC = 120  # بعدِ هر هشدار، حداقل این‌قدر صبر کن قبلِ هشدارِ بعدی

# chat_id -> deque[(ts, user_id, full_name, username)]
_join_times: dict[int, deque] = {}
_last_alert_at: dict[int, float] = {}


async def _get_pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


def _guard_kb(chat_id: int, count: int) -> InlineKeyboardMarkup:
    from premium_emoji import hx_button
    return InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(f"🚨 حذف {count} عضو فیک", callback_data=f"fjg:kick:{chat_id}")],
        [hx_button("نادیده بگیر", callback_data=f"fjg:dismiss:{chat_id}")],
    ])


async def _is_guarded_chat(chat_id: int) -> bool:
    guard_chat_id = await _get_pset("group_guard_chat_id", "")
    if not guard_chat_id or str(chat_id) != guard_chat_id:
        return False
    return (await _get_pset("group_guard_enabled", "off")) == "on"


async def _register_join(bot: Bot, chat_id: int, user_id: int,
                          full_name: str, username: str | None) -> None:
    now = time.monotonic()
    dq = _join_times.setdefault(chat_id, deque())
    dq.append((now, user_id, full_name, username))

    # حذفِ ورودی‌هایی که خیلی قدیمی‌ان (بیشتر از یک پنجره‌ی معقول، مثلاً ۱۵ دقیقه)
    while dq and now - dq[0][0] > 900:
        dq.popleft()

    # زنجیره رو از انتها به اول می‌گردیم: تا وقتی فاصله‌ی پیاپی‌ها <= BURST_GAP_MAX_SEC
    chain = [dq[-1]]
    for i in range(len(dq) - 2, -1, -1):
        if chain[-1][0] - dq[i][0] <= BURST_GAP_MAX_SEC:
            chain.append(dq[i])
        else:
            break

    if len(chain) < BURST_MIN_COUNT:
        return

    last = _last_alert_at.get(chat_id, 0)
    if now - last < BURST_ALERT_COOLDOWN_SEC:
        # قبلاً برایِ همین بحران هشدار دادیم؛ فقط کاندیدهایِ جدید رو به دیتابیس اضافه کن
        await _save_candidates(chat_id, chain)
        return
    _last_alert_at[chat_id] = now

    await _save_candidates(chat_id, chain)
    n = len(chain)
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            f"⚠️ <b>حمله‌ی جوینِ انبوه شناسایی شد</b>\n"
            f"چت: <code>{chat_id}</code>\n"
            f"تعدادِ اعضایِ مشکوک (جوینِ پشتِ‌سرِهم با فاصله‌ی ≤{BURST_GAP_MAX_SEC} ثانیه): "
            f"<b>{n}</b>\n\n"
            f"می‌تونی با دکمه‌ی زیر همه‌شونو یک‌جا حذف کنی:",
            reply_markup=_guard_kb(chat_id, n),
        )
    except Exception:
        logger.exception("خطا در ارسالِ هشدارِ فیک‌جوین به ادمین")


async def _save_candidates(chat_id: int, chain: list[tuple]) -> None:
    async with Session() as s:
        for _, uid, full_name, username in chain:
            existing = await s.scalar(select(FakeJoinCandidate).where(
                FakeJoinCandidate.chat_id == chat_id, FakeJoinCandidate.user_id == uid))
            if existing:
                continue
            s.add(FakeJoinCandidate(
                chat_id=chat_id, user_id=uid,
                full_name=(full_name or "")[:255],
                username=(username or None),
            ))
        await s.commit()


# ── هندلرِ عضویت در گروه (پیامِ سرویسیِ new_chat_members) ──
@fake_join_router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
async def fjg_group_join(msg: Message, bot: Bot):
    if not await _is_guarded_chat(msg.chat.id):
        return
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        await _register_join(bot, msg.chat.id, member.id, member.full_name, member.username)


# ── هندلرِ عضویت در کانال/سوپرگروه (آپدیتِ chat_member) ──
@fake_join_router.chat_member()
async def fjg_chat_member_join(event: ChatMemberUpdated, bot: Bot):
    if not await _is_guarded_chat(event.chat.id):
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    became_member = old_status in ("left", "kicked", "restricted") and new_status in ("member", "restricted")
    if not became_member:
        return
    u = event.new_chat_member.user
    if u.is_bot:
        return
    await _register_join(bot, event.chat.id, u.id, u.full_name, u.username)


# ── دکمه‌ی کیکِ دسته‌جمعی ──
@fake_join_router.callback_query(F.data.startswith("fjg:kick:"))
async def fjg_kick(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("این دکمه فقط برایِ مدیرِ اصلیه.", show_alert=True)
        return
    chat_id = int(cb.data.split(":")[2])
    async with Session() as s:
        rows = (await s.scalars(select(FakeJoinCandidate).where(
            FakeJoinCandidate.chat_id == chat_id))).all()
        ids = [r.user_id for r in rows]
        await s.execute(delete(FakeJoinCandidate).where(FakeJoinCandidate.chat_id == chat_id))
        await s.commit()

    if not ids:
        await cb.answer("چیزی برای حذف نمونده.", show_alert=True)
        return

    await cb.answer(f"در حالِ حذفِ {len(ids)} نفر... (ممکنه چند دقیقه طول بکشه)")
    ok, fail = 0, 0
    for uid in ids:
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # رعایتِ ریت‌لیمیتِ تلگرام

    try:
        await cb.message.edit_text(
            f"✅ <b>حذف انجام شد</b>\nموفق: {ok}\nناموفق: {fail}"
        )
    except Exception:
        logger.exception("خطا در ادیتِ پیامِ نتیجه")


@fake_join_router.callback_query(F.data.startswith("fjg:dismiss:"))
async def fjg_dismiss(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("این دکمه فقط برایِ مدیرِ اصلیه.", show_alert=True)
        return
    chat_id = int(cb.data.split(":")[2])
    async with Session() as s:
        await s.execute(delete(FakeJoinCandidate).where(FakeJoinCandidate.chat_id == chat_id))
        await s.commit()
    await cb.answer("لیست پاک شد.")
    try:
        await cb.message.edit_text("لغو شد؛ لیستِ مشکوک‌ها پاک شد.")
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")

