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


def plan_map(d) -> dict[str, int]:
    """«growth:13,pro:20» → {"growth": 13, "pro": 20}. اگر خالی بود از percent/plansِ قدیمی ساخته می‌شود."""
    out: dict[str, int] = {}
    for part in (getattr(d, "plan_percents", None) or "").split(","):
        k, _, v = part.partition(":")
        if k in dict(PLAN_CHOICES) and v.isdigit() and int(v) > 0:
            out[k] = int(v)
    if not out and d is not None and int(getattr(d, "percent", 0) or 0) > 0:
        for k in (plans_list(d) or [c for c, _ in PLAN_CHOICES]):
            out[k] = int(d.percent)
    return out


def _dump_map(m: dict[str, int]) -> str | None:
    return ",".join(f"{k}:{v}" for k, v in m.items() if v > 0) or None


async def percent_for(uid: int, plan: str | None = None) -> int:
    """درصدِ تخفیفِ اختصاصیِ فعالِ این کاربر برایِ این پلن (۰ اگر ندارد/خاموش است/برایِ این پلن نیست).
    plan=None یعنی فقط «آیا اصلاً تخفیفی دارد» (بزرگ‌ترینِ درصد)."""
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
    if not (d and d.active):
        return 0
    m = plan_map(d)
    if plan is None:
        return max(m.values(), default=0)
    return m.get(str(plan), 0)


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
            d.plans, d.plan_percents = pl, None
            if note:
                d.note = note[:80]
            await s.commit()
            return "updated"
        s.add(PlatformUserDiscount(user_telegram_id=uid, percent=percent, active=True, note=(note or None),
                                   plans=pl))
        await s.commit()
        return "created"


async def set_plan_percent(uid: int, plan: str, percent: int) -> str:
    """درصدِ یک پلن را برایِ کاربر می‌گذارد (۰ = حذفِ همان پلن). plan="all" یعنی همهٔ پلن‌ها."""
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
        m = plan_map(d) if d else {}
        for k in ([c for c, _ in PLAN_CHOICES] if plan == "all" else [plan]):
            if k in dict(PLAN_CHOICES):
                if percent > 0:
                    m[k] = percent
                else:
                    m.pop(k, None)
        if not m:
            if not d:
                return "none"
            await s.delete(d)
            await s.commit()
            return "removed"
        if not d:
            d = PlatformUserDiscount(user_telegram_id=uid, active=True)
            s.add(d)
        d.plan_percents, d.percent, d.plans, d.active = _dump_map(m), max(m.values()), None, True
        await s.commit()
        return "updated"


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
        kb.append([_hx(f"{'🟢' if r.active else '⚪️'} {label} — " + "، ".join(f"{v}٪" for v in plan_map(r).values()), callback_data=f"ud:v:{r.id}")])
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
    await state.update_data(ud_uid=uid)
    await state.set_state(UDiscFSM.plans)
    await _plan_screen(msg, uid)


async def _plan_screen(target, uid: int):
    async with Session() as s:
        d = await s.scalar(select(PlatformUserDiscount).where(PlatformUserDiscount.user_telegram_id == uid))
        label = await _label(s, uid)
    m = plan_map(d) if d else {}
    rows = [[_hx(f"{lbl} — {('%d٪' % m[k]) if k in m else 'بدونِ تخفیف'}", callback_data=f"ud:p:{k}")]
            for k, lbl in PLAN_CHOICES]
    rows.append([_hx("🎁 یک درصد برایِ همهٔ پلن‌ها", callback_data="ud:p:all")])
    rows.append([_hx("✅ پایان", callback_data="ud:menu", style=ButtonStyle.SUCCESS)])
    txt = (f"👤 {label} (<code>{uid}</code>)\n\n"
           "📦 <b>پلن را انتخاب کن</b> تا درصدِ تخفیفِ همان پلن را بدهی؛ هر پلن درصدِ جدا دارد "
           "(مثلاً رشد ۱۳٪ و حرفه‌ای ۲۰٪). برایِ حذفِ تخفیفِ یک پلن، درصدش را <b>۰</b> بفرست.")
    await (getattr(target, "message", None) or target).answer(
        txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@udisc_router.callback_query(UDiscFSM.plans, F.data.startswith("ud:p:"))
async def ud_plan_pick(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    key = cb.data.split(":")[2]
    if key != "all" and key not in dict(PLAN_CHOICES):
        return await cb.answer()
    await state.update_data(ud_plan=key)
    await state.set_state(UDiscFSM.percent)
    name = "همهٔ پلن‌ها" if key == "all" else dict(PLAN_CHOICES)[key]
    await cb.message.answer(f"🎁 درصدِ تخفیفِ دائمی برایِ <b>{name}</b> را بفرست (۱ تا {MAX_PERCENT}؛ "
                            "۰ = حذف).", reply_markup=_cancel_kb())
    await cb.answer()


@udisc_router.message(UDiscFSM.percent, F.text)
async def ud_percent(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    pct = parse_percent(msg.text)
    if pct is None:
        return await msg.answer(f"❌ عددی بینِ ۰ تا {MAX_PERCENT} بفرست.", reply_markup=_cancel_kb())
    data = await state.get_data()
    uid, key = int(data.get("ud_uid") or 0), data.get("ud_plan") or "all"
    await set_plan_percent(uid, key, pct)
    await state.set_state(UDiscFSM.plans)
    name = "همهٔ پلن‌ها" if key == "all" else dict(PLAN_CHOICES)[key]
    await msg.answer(f"✅ {name}: " + (f"<b>{pct}٪</b> ثبت شد." if pct else "تخفیف حذف شد."))
    if pct:
        try:
            await msg.bot.send_message(
                uid, f"🎁 <b>تخفیفِ اختصاصیِ {pct}٪</b> برایِ پلنِ {name} برایِ شما فعال شد!\n"
                     "از این به بعد روی هر خرید و تمدیدِ آن اعمال می‌شود.")
        except Exception:
            logger.info("udisc: اطلاع به کاربر %s ممکن نشد (ربات را استارت نکرده؟)", uid)
    await _plan_screen(msg, uid)


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
    _pl = "، ".join(f"{_t[k]} {v}٪" for k, v in plan_map(d).items()) or "—"
    txt = (f"🏷 <b>{label}</b>\n🆔 <code>{d.user_telegram_id}</code>\n\n"
           f"📦 تخفیفِ دائمی به‌ازایِ هر پلن: {_pl}\n"
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
