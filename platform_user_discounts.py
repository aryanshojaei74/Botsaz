"""
platform_user_discounts.py — PATCH_PLATFORM_USER_DISCOUNT_V1

تخفیفِ دائمیِ اختصاصیِ هر کاربر در رباتِ مادر: مدیر آیدیِ عددیِ شخص و درصد را می‌دهد؛
از آن به بعد روی «هر» خرید/تمدید/ارتقایِ پلنِ او اعمال می‌شود تا مدیر خاموش کند یا درصد را صفر بگذارد.
با تخفیف‌هایِ دیگر (زیرمجموعه‌گیری، نفراتِ برتر، معرفِ خریدِ اول) جمع نمی‌شود؛ بزرگ‌ترین اعمال می‌شود.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import PlatformUser, PlatformUserDiscount

logger = _get_logger(__name__)

udisc_router = Router(name="platform_user_discounts")
MAX_PERCENT = 90
PLAN_CHOICES = (("growth", "🚀 رشد"), ("pro", "💎 حرفه‌ای"))


class UDiscFSM(StatesGroup):
    user_id = State()
    percent = State()
    plans = State()


def _hx(*a, **k):
    from premium_emoji import hx_button
    return hx_button(*a, **k)


def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


def parse_percent(txt: str) -> int | None:
    t = _fa(txt).replace("%", "").replace("٪", "").strip()
    if not t.isdigit():
        return None
    v = int(t)
    return v if 0 <= v <= MAX_PERCENT else None


# ═══════════ منطق ═══════════
def plans_list(d) -> list[str]:
    return [x for x in (getattr(d, "plans", None) or "").split(",") if x]


async def percent_for(uid: int, plan: str | None = None) -> int:
    """درصدِ تخفیفِ اختصاصیِ فعالِ این کاربر (۰ اگر ندارد/خاموش است/برایِ این پلن نیست).
    plan=None یعنی فقط «آیا اصلاً تخفیفی دارد» (بدونِ فیلترِ پلن)."""
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
    if not (d and d.active and int(d.percent or 0) > 0):
        return 0
    allowed = plans_list(d)
    if plan is not None and allowed and str(plan) not in allowed:
        return 0
    return int(d.percent)


async def resolve_user(text: str, bot=None) -> int | None:
    """آیدیِ عددی یا @یوزرنیم → آیدیِ عددی. یوزرنیم اول از دیتابیسِ خودِ ربات، بعد از تلگرام."""
    t = _fa(text)
    if t.isdigit():
        return int(t) if int(t) > 0 else None
    name = t.lstrip("@").strip()
    if not name or not all(c.isalnum() or c == "_" for c in name):
        return None
    async with Session() as s:
        from sqlalchemy import func as _f
        u = await s.scalar(select(PlatformUser).where(_f.lower(PlatformUser.username) == name.lower()))
    if u:
        return int(u.telegram_id)
    if bot is not None:
        try:
            ch = await bot.get_chat("@" + name)
            return int(ch.id) if getattr(ch, "type", "private") == "private" else None
        except Exception:
            logger.info("udisc: یوزرنیمِ @%s پیدا نشد", name)
    return None


async def set_discount(uid: int, percent: int, note: str | None = None,
                       plans: list[str] | None = None) -> str:
    """درصد را ثبت/ویرایش می‌کند؛ صفر = حذف. خروجی: created | updated | removed | none"""
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
        if percent <= 0:
            if not d:
                return "none"
            await s.delete(d)
            await s.commit()
            return "removed"
        pl = ",".join(p for p in (plans or []) if p in dict(PLAN_CHOICES)) or None
        if d:
            d.percent, d.active = percent, True
            d.plans = pl
            if note:
                d.note = note[:80]
            await s.commit()
            return "updated"
        s.add(PlatformUserDiscount(user_telegram_id=uid, percent=percent, active=True, note=(note or None),
                                   plans=pl))
        await s.commit()
        return "created"


async def _label(s, uid: int) -> str:
    u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid))
    if not u:
        return f"{uid}"
    nm = (u.full_name or "").strip()
    return f"{nm} (@{u.username})" if u.username else (nm or str(uid))


# ═══════════ پنلِ مدیر ═══════════
def _is_admin(uid: int) -> bool:
    return bool(PLATFORM_ADMIN_ID) and uid == PLATFORM_ADMIN_ID


async def _list_view() -> tuple[str, InlineKeyboardMarkup]:
    async with Session() as s:
        rows = (await s.scalars(select(PlatformUserDiscount).order_by(PlatformUserDiscount.id.desc()))).all()
        items = [(r, await _label(s, r.user_telegram_id)) for r in rows[:40]]
    txt = ("🏷 <b>تخفیفِ اختصاصیِ کاربر</b>\n\n"
           "آیدیِ عددیِ یک نفر و درصد را بده؛ از آن به بعد روی <b>هر خرید و تمدیدِ پلنِ</b> او اعمال می‌شود "
           "تا خودت خاموش کنی یا درصد را صفر بگذاری.\n"
           "با تخفیف‌هایِ دیگر جمع نمی‌شود؛ بزرگ‌ترین‌شان اعمال می‌شود.\n\n"
           f"تعدادِ کاربرانِ دارایِ تخفیف: <b>{len(rows)}</b>")
    kb = [[_hx("➕ افزودن / تغییرِ تخفیفِ یک کاربر", callback_data="ud:add", style=ButtonStyle.SUCCESS)]]
    for r, label in items:
        kb.append([_hx(f"{'🟢' if r.active else '⚪️'} {label} — {r.percent}٪", callback_data=f"ud:v:{r.id}")])
    kb.append([_hx("⬅️ بازگشت", callback_data="adm:settings", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=kb)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_hx("❌ لغو", callback_data="ud:menu")]])


async def _show(target, txt, kb):
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb)
    except Exception:
        await m.answer(txt, reply_markup=kb)


@udisc_router.callback_query(F.data.in_({"set:udisc", "ud:menu"}))
async def ud_menu(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.clear()
    txt, kb = await _list_view()
    await _show(cb, txt, kb)
    await cb.answer()


@udisc_router.callback_query(F.data == "ud:add")
async def ud_add(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.set_state(UDiscFSM.user_id)
    await _show(cb, "🆔 <b>آیدیِ عددی یا @یوزرنیمِ کاربر</b> را بفرست:", _cancel_kb())
    await cb.answer()


@udisc_router.message(UDiscFSM.user_id, F.text)
async def ud_user(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    uid = await resolve_user(msg.text, msg.bot)
    if not uid:
        return await msg.answer("❌ کاربر پیدا نشد. آیدیِ عددی یا @یوزرنیم را بفرست "
                                "(یوزرنیم فقط برایِ کسی که ربات را استارت کرده کار می‌کند).",
                                reply_markup=_cancel_kb())
    cur = await percent_for(uid)
    async with Session() as s:
        label = await _label(s, uid)
    await state.update_data(ud_uid=uid)
    await state.set_state(UDiscFSM.percent)
    await msg.answer(
        f"👤 {label} (<code>{uid}</code>)\n"
        f"تخفیفِ فعلی: <b>{cur}٪</b>\n\n"
        f"🎁 <b>درصدِ تخفیفِ دائمی</b> را بفرست (۱ تا {MAX_PERCENT}). برایِ حذفِ تخفیف <b>۰</b> بفرست.",
        reply_markup=_cancel_kb())


def _plans_kb(sel: list[str]) -> InlineKeyboardMarkup:
    rows = [[_hx(("✅ " if k in sel else "⬜️ ") + lbl, callback_data=f"ud:p:{k}")] for k, lbl in PLAN_CHOICES]
    rows.append([_hx(("✅ " if not sel else "⬜️ ") + "همهٔ پلن‌ها", callback_data="ud:pall")])
    rows.append([_hx("💾 ذخیره", callback_data="ud:psave", style=ButtonStyle.SUCCESS)])
    rows.append([_hx("❌ لغو", callback_data="ud:menu", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_PLANS_TXT = ("📦 <b>روی کدام پلن‌ها فعال باشد؟</b>\n\n"
              "یک یا چند پلن را تیک بزن، یا «همهٔ پلن‌ها» (پیش‌فرض). بعد «💾 ذخیره».")


@udisc_router.message(UDiscFSM.percent, F.text)
async def ud_percent(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    pct = parse_percent(msg.text)
    if pct is None:
        return await msg.answer(f"❌ عددی بینِ ۰ تا {MAX_PERCENT} بفرست.", reply_markup=_cancel_kb())
    uid = int((await state.get_data()).get("ud_uid") or 0)
    if pct == 0:                                    # صفر = حذف؛ پرسیدنِ پلن لازم نیست
        await state.clear()
        res = await set_discount(uid, 0)
        await msg.answer(f"🗑 تخفیفِ اختصاصیِ <code>{uid}</code> حذف شد." if res == "removed"
                         else "ℹ️ این کاربر تخفیفِ اختصاصی نداشت؛ چیزی تغییر نکرد.")
        txt, kb = await _list_view()
        return await msg.answer(txt, reply_markup=kb)
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
    cur_plans = plans_list(d) if d else []
    await state.update_data(ud_pct=pct, ud_plans=cur_plans)
    await state.set_state(UDiscFSM.plans)
    await msg.answer(_PLANS_TXT, reply_markup=_plans_kb(cur_plans))


@udisc_router.callback_query(UDiscFSM.plans, F.data.startswith("ud:p:"))
async def ud_plan_toggle(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    key = cb.data.split(":")[2]
    if key not in dict(PLAN_CHOICES):
        return await cb.answer()
    sel = list((await state.get_data()).get("ud_plans") or [])
    sel = [x for x in sel if x != key] if key in sel else sel + [key]
    await state.update_data(ud_plans=sel)
    await cb.message.edit_reply_markup(reply_markup=_plans_kb(sel))
    await cb.answer()


@udisc_router.callback_query(UDiscFSM.plans, F.data == "ud:pall")
async def ud_plan_all(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    await state.update_data(ud_plans=[])
    await cb.message.edit_reply_markup(reply_markup=_plans_kb([]))
    await cb.answer()


@udisc_router.callback_query(UDiscFSM.plans, F.data == "ud:psave")
async def ud_save(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    data = await state.get_data()
    uid, pct, sel = int(data.get("ud_uid") or 0), int(data.get("ud_pct") or 0), list(data.get("ud_plans") or [])
    await state.clear()
    res = await set_discount(uid, pct, plans=sel)
    titles = dict(PLAN_CHOICES)
    where = "همهٔ پلن‌ها" if not sel else "، ".join(titles[k] for k in sel if k in titles)
    await cb.message.answer(f"✅ تخفیفِ دائمیِ <b>{pct}٪</b> برایِ <code>{uid}</code> "
                            f"{'ثبت' if res == 'created' else 'به‌روزرسانی'} شد.\n📦 پلن‌ها: {where}")
    try:
        await cb.bot.send_message(
            uid, f"🎁 <b>تخفیفِ اختصاصیِ {pct}٪</b> برایِ شما فعال شد!\n"
                 f"از این به بعد روی هر خرید و تمدیدِ پلن ({where}) اعمال می‌شود.")
    except Exception:
        logger.info("udisc: اطلاع به کاربر %s ممکن نشد (ربات را استارت نکرده؟)", uid)
    txt, kb = await _list_view()
    await cb.message.answer(txt, reply_markup=kb)
    await cb.answer("ذخیره شد ✅")


@udisc_router.callback_query(F.data.startswith("ud:v:"))
async def ud_view(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    rid = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(PlatformUserDiscount, rid)
        label = await _label(s, d.user_telegram_id) if d else ""
    if not d:
        return await cb.answer("پیدا نشد.", show_alert=True)
    _t = dict(PLAN_CHOICES)
    _pl = "، ".join(_t[k] for k in plans_list(d) if k in _t) or "همهٔ پلن‌ها"
    txt = (f"🏷 <b>{label}</b>\n🆔 <code>{d.user_telegram_id}</code>\n\n"
           f"تخفیفِ دائمی: <b>{d.percent}٪</b>\n📦 پلن‌ها: {_pl}\n"
           f"وضعیت: {'🟢 فعال' if d.active else '⚪️ خاموش'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_hx("⛔️ خاموش‌کردن" if d.active else "✅ روشن‌کردن", callback_data=f"ud:t:{rid}",
             style=(ButtonStyle.DANGER if d.active else ButtonStyle.SUCCESS))],
        [_hx("🎁 تغییرِ درصد / پلن‌ها", callback_data="ud:add")],
        [_hx("🗑 حذف", callback_data=f"ud:d:{rid}", style=ButtonStyle.DANGER)],
        [_hx("⬅️ بازگشت", callback_data="ud:menu", style=ButtonStyle.DANGER)]])
    await _show(cb, txt, kb)
    await cb.answer()


@udisc_router.callback_query(F.data.startswith("ud:t:"))
async def ud_toggle(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    rid = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(PlatformUserDiscount, rid)
        if not d:
            return await cb.answer("پیدا نشد.", show_alert=True)
        d.active = not d.active
        await s.commit()
    cb.data = f"ud:v:{rid}"
    await ud_view(cb)


@udisc_router.callback_query(F.data.startswith("ud:d:"))
async def ud_delete(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    rid = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(PlatformUserDiscount, rid)
        if d:
            await s.delete(d)
            await s.commit()
    txt, kb = await _list_view()
    await _show(cb, txt, kb)
    await cb.answer("حذف شد")
