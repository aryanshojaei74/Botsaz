"""platform_vip_addall.py — «➕ افزودنِ روز به همهٔ سرورهایِ فعال‌سازی» (جبرانِ قطعی)."""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import HostedApp, PlatformSetting
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

vip_addall_router = Router()

MAX_DAYS = 365
GUARD_KEY = "vip_addall_last"
GUARD_MINUTES = 10


class VipAddAllFSM(StatesGroup):
    days = State()


def _is_super(uid) -> bool:
    return bool(PLATFORM_ADMIN_ID) and uid is not None and int(uid) == int(PLATFORM_ADMIN_ID)


def _parse_days(raw: str) -> int | None:
    t = (raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = "".join(ch for ch in t if ch.isdigit())
    if not digits:
        return None
    n = int(digits)
    return n if 1 <= n <= MAX_DAYS else None


def _extend(cur: _dt.datetime | None, now: _dt.datetime, days: int) -> _dt.datetime:
    base = cur if (cur and cur > now) else now
    return base + _dt.timedelta(days=days)


async def plan(now: _dt.datetime) -> dict:
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp))).all()
    servers = [a for a in apps if a.paid_until is not None and a.status != "archived"]
    resume = [a for a in servers if a.paid_until <= now and a.status == "suspended"]
    minis = [a for a in apps if a.miniapp_sub and a.miniapp_until is not None and a.status != "archived"]
    return {"servers": len(servers), "resume": len(resume), "minis": len(minis), "free": sum(1 for a in apps if a.paid_until is None)}


async def apply(now: _dt.datetime, days: int) -> dict:
    owners: dict[int, int] = {}
    n_srv = n_resume = n_mini = 0
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp))).all()
        for a in apps:
            if a.status == "archived":
                continue
            changed = False
            if a.paid_until is not None:
                expired = a.paid_until <= now
                a.paid_until = _extend(a.paid_until, now, days)
                a.notice_sent = None
                if expired and a.status == "suspended":
                    a.status = "stopped"
                    a.last_error = None
                    n_resume += 1
                n_srv += 1
                changed = True
            if a.miniapp_sub and a.miniapp_until is not None:
                a.miniapp_until = _extend(a.miniapp_until, now, days)
                n_mini += 1
                changed = True
            if changed:
                owners[a.owner_id] = owners.get(a.owner_id, 0) + 1
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == GUARD_KEY))
        stamp = now.isoformat(timespec="seconds")
        if row is None:
            s.add(PlatformSetting(key=GUARD_KEY, value=stamp))
        else:
            row.value = stamp
        await s.commit()
    return {"servers": n_srv, "resume": n_resume, "minis": n_mini, "owners": list(owners)}


async def _recent_run(now: _dt.datetime) -> bool:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == GUARD_KEY))
    if row is None or not row.value:
        return False
    try:
        last = _dt.datetime.fromisoformat(row.value)
    except ValueError:
        return False
    return (now - last) < _dt.timedelta(minutes=GUARD_MINUTES)


@vip_addall_router.callback_query(F.data == "vip:addall")
async def vip_addall_start(cb: CallbackQuery, state: FSMContext):
    if not _is_super(cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی", show_alert=True)
        return
    await state.set_state(VipAddAllFSM.days)
    await cb.message.answer(
        "➕ <b>افزودنِ روز به همهٔ سرورهایِ فعال‌سازی</b>\n\nچند روز اضافه شود؟ (۱ تا %d) فقط عدد بفرست، مثلاً <code>2</code>.\n"
        "برایِ لغو: /cancel" % MAX_DAYS, parse_mode="HTML")
    await cb.answer()


@vip_addall_router.message(StateFilter(VipAddAllFSM.days), F.text)
async def vip_addall_days(msg: Message, state: FSMContext):
    if not _is_super(msg.from_user.id):
        await state.clear()
        return
    if (msg.text or "").strip().lower() in ("/cancel", "لغو", "❌ لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    days = _parse_days(msg.text)
    if days is None:
        await msg.answer("عددِ معتبر بفرست (۱ تا %d)." % MAX_DAYS)
        return
    now = _dt.datetime.now()
    p = await plan(now)
    await state.clear()
    guard = "\n\n⚠️ <b>در ۱۰ دقیقهٔ اخیر یک‌بار اجرا شده است؛ مطمئنی دوباره می‌خواهی؟</b>" if await _recent_run(now) else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(f"✅ اعمال +{days} روز و اطلاع به مالک‌ها", callback_data=f"vip:addall:go:{days}:1", style=ButtonStyle.SUCCESS)],
        [hx_button(f"✅ اعمال +{days} روز (بدونِ اطلاع)", callback_data=f"vip:addall:go:{days}:0", style=ButtonStyle.PRIMARY)],
        [hx_button("❌ لغو", callback_data="vip:menu", style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        f"🔎 <b>پیش‌نمایش: +{days} روز</b>\n\n"
        f"🖥 سرورهایِ پرداخت‌شده (تمدید می‌شوند): <b>{p['servers']}</b>\n"
        f"▶️ از این‌ها، سرورهایی که به‌خاطرِ پایانِ مهلت متوقف شده‌اند و دوباره اجازهٔ روشن‌شدن می‌گیرند: <b>{p['resume']}</b>\n"
        f"🌐 اشتراکِ مینی‌اپ (ساب‌دامین): <b>{p['minis']}</b>\n"
        f"🎁 سرورهایِ تستِ رایگان (بدونِ پرداخت، دست‌نمی‌خورند): <b>{p['free']}</b>\n\n"
        "قاعده: سررسیدِ جدید = بزرگ‌تر از «سررسیدِ فعلی» و «همین حالا»، به‌اضافهٔ روزها. سرورِ بایگانی‌شده دست نمی‌خورد."
        + guard, parse_mode="HTML", reply_markup=kb)


@vip_addall_router.callback_query(F.data.regexp(r"^vip:addall:go:\d+:[01]$"))
async def vip_addall_go(cb: CallbackQuery, bot: Bot):
    if not _is_super(cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی", show_alert=True)
        return
    _, _, _, d_s, n_s = cb.data.split(":")
    days = int(d_s)
    if not 1 <= days <= MAX_DAYS:
        await cb.answer("نامعتبر", show_alert=True)
        return
    await cb.answer("در حال اعمال…")
    res = await apply(_dt.datetime.now(), days)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("vip_addall: حذفِ دکمه‌ها ممکن نشد", exc_info=True)
    sent = 0
    if n_s == "1":
        for uid in res["owners"]:
            try:
                await bot.send_message(uid, f"⚡️ به‌خاطرِ قطعیِ اخیر، <b>{days} روز</b> به سرورِ فعال‌سازیِ شما اضافه شد. ممنون از صبوری‌تان 🙏", parse_mode="HTML")
                sent += 1
            except Exception:
                logger.info("vip_addall: اطلاع به %s ممکن نشد", uid)
    await cb.message.answer(
        f"✅ <b>انجام شد (+{days} روز)</b>\n\n🖥 سرور تمدید شد: <b>{res['servers']}</b>\n"
        f"▶️ دوباره اجازهٔ روشن‌شدن گرفتند: <b>{res['resume']}</b>\n🌐 مینی‌اپ تمدید شد: <b>{res['minis']}</b>\n"
        + (f"📩 به <b>{sent}</b> مالک خبر داده شد." if n_s == "1" else "📩 اطلاعی فرستاده نشد."), parse_mode="HTML")
