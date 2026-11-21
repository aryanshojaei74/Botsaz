"""
help_ai.py — PATCH_HELP_ASSISTANT_V1

دستیارِ راهنمایِ بات‌ساز برایِ مالکانِ ربات.

دو لایه، برایِ اینکه هزینهٔ API الکی مصرف نشود:
  ۱) جست‌وجویِ متنی در دانش‌نامه — بیشترِ سؤال‌ها («فلان قابلیت کجاست؟»)
     همین‌جا جواب می‌گیرند، بدونِ هیچ تماسِ شبکه‌ای.
  ۲) اگر جوابِ خوبی پیدا نشد، سؤال با دانش‌نامه به مدل فرستاده می‌شود.

سقفِ روزانه هم دارد تا یک نفر کلِ سهمیه را مصرف نکند.
"""
from __future__ import annotations

import re
from datetime import date

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from tenant.shared.help_kb import (FAQ, HOWTO, SECTIONS, compact_for_prompt,
                           expand_query)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

help_ai_router = Router()

DAILY_LIMIT = 20          # سقفِ پرسشِ هوشِ مصنوعی برایِ هر کاربر در روز

_SYS = """تو راهنمایِ «بات‌ساز» هستی — پلتفرمی که کاربران با آن رباتِ تلگرامیِ
فروشگاهی می‌سازند (فروشِ VPN، خدماتِ مجازی/SMM، فروشگاهِ دیجیتال، رزرو و…).

قواعد:
- فقط فارسی، کوتاه و عملی. حداکثر ۵ جمله.
- همیشه «مسیرِ دقیق» را بگو، مثل: پنل مدیریت ← 💰 مالی و کیف پول ← ...
- اگر جوابش در دانشِ زیر نیست، صادقانه بگو نمی‌دانی و بگو از پشتیبانی بپرسد.
- چیزی از خودت نساز؛ فقط از دانشِ زیر استفاده کن.

دانشِ بات‌ساز:
"""


class HelpFSM(StatesGroup):
    ask = State()


# ═══════════ لایهٔ ۱: جست‌وجویِ متنی ═══════════
def _norm(t: str) -> str:
    t = (t or "").replace("\u200c", "")   # نیم‌فاصله حذف، نه تبدیل به فاصله
    t = t.replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"[^\w\s]", " ", t).lower()


def local_search(q: str) -> str | None:
    """پاسخِ محلی از دانش‌نامه؛ None یعنی چیزی پیدا نشد."""
    qn = _norm(expand_query(q))
    words = [w for w in qn.split() if len(w) > 2]
    if not words:
        return None

    best, best_score = None, 0
    pool: list[tuple[str, str]] = list(HOWTO) + list(FAQ)
    for name, sec in ((n, s) for s in SECTIONS.values() for n, _d in s["items"]):
        pass  # فقط برایِ خوانایی؛ آیتم‌ها پایین اضافه می‌شوند
    for s in SECTIONS.values():
        for name, desc in s["items"]:
            pool.append((name, f"{desc}\n\n📍 {s['title']}"))

    for title, body in pool:
        hay = _norm(title + " " + body)
        score = sum(2 if w in _norm(title) else (1 if w in hay else 0)
                    for w in words)
        if score > best_score:
            best, best_score = (title, body), score

    # آستانه: برایِ پرسشِ تک‌کلمهای یک تطبیق کافی است، وگرنه دو تا
    need = 1 if len(words) == 1 else 2
    if best and best_score >= need:
        return f"<b>{best[0]}</b>\n\n{best[1]}"
    return None


# ═══════════ لایهٔ ۲: هوشِ مصنوعی ═══════════
async def _quota_ok(bot_id: int, uid: int) -> bool:
    from tenant import get_setting, set_setting
    key = f"helpai_q_{uid}"
    today = date.today().isoformat()
    raw = await get_setting(bot_id, key, "")
    d, _, c = (raw or "").partition("|")
    n = int(c) if (d == today and c.isdigit()) else 0
    if n >= DAILY_LIMIT:
        return False
    await set_setting(bot_id, key, f"{today}|{n + 1}")
    return True


async def ask_ai(bot_id: int, question: str) -> str | None:
    """پرسش از مدل با دانشِ بات‌ساز. None یعنی ناموفق."""
    try:
        from tenant.bots.virtual_service import smm_ai
        from tenant import get_setting
        provider = await get_setting(bot_id, "smm_ai_provider", "groq")
        api_key = await get_setting(bot_id, "smm_ai_key", "")
        if not api_key:
            return None
        system = _SYS + compact_for_prompt()
        fn = {
            "anthropic": smm_ai._ask_anthropic,
            "groq": smm_ai._ask_groq,
            "openai": smm_ai._ask_openai,
            "gemini": smm_ai._ask_gemini,
        }.get(provider)
        if not fn:
            return None
        # از همان توابعِ smm_ai استفاده می‌کنیم ولی با systemِ خودمان
        old = smm_ai._SYSTEM_PROMPT
        try:
            smm_ai._SYSTEM_PROMPT = system
            return await fn(api_key, question)
        finally:
            smm_ai._SYSTEM_PROMPT = old
    except Exception:
        logger.exception("help_ai: پرسش از مدل ناموفق بود")
        return None


# ═══════════ رابطِ کاربری ═══════════
def _menu_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=s["title"],
                                  callback_data=f"hlp:sec:{k}")]
            for k, s in SECTIONS.items()]
    rows.append([InlineKeyboardButton(text="❓ سؤالت را بپرس",
                                      callback_data="hlp:ask",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="📖 کارهای پرتکرار",
                                      callback_data="hlp:howto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_HELP_ONLY_VIRTUAL_SERVICE_V1 ===
# راهنما فقط برایِ ربات‌هایِ «خدماتِ مجازی» است. این نگهبان روی خودِ صفحه
# است نه فقط رویِ دکمه، چون دکمهٔ شیشه‌ایِ قدیمی در پیام‌هایِ گذشته و
# کیبوردِ ذخیره‌شده رویِ گوشیِ ادمین همچنان قابلِ زدن است.
async def _is_virtual_service(cb: CallbackQuery) -> bool:
    try:
        from tenant import get_bot_row
        row = await get_bot_row(cb.bot)
        return bool(row and row.field == "virtual_service")
    except Exception:
        logger.exception("help_ai: تشخیصِ نوعِ ربات ناموفق بود")
        return False


@help_ai_router.callback_query(F.data == "hlp:menu")
async def hlp_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_virtual_service(cb):
        await cb.answer("این بخش فقط در ربات‌هایِ خدماتِ مجازی فعال است.", show_alert=True)
        return
    await state.clear()
    txt = ("📘 <b>راهنمای بات‌ساز</b>\n\n"
           "یکی از بخش‌ها را انتخاب کن، یا مستقیم سؤالت را بپرس.")
    try:
        await cb.message.edit_text(txt, reply_markup=_menu_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=_menu_kb(), parse_mode="HTML")
    await cb.answer()


@help_ai_router.callback_query(F.data.startswith("hlp:sec:"))
async def hlp_sec(cb: CallbackQuery):
    key = cb.data.split(":", 2)[2]
    sec = SECTIONS.get(key)
    if not sec:
        return await cb.answer()
    lines = [f"<b>{sec['title']}</b>\n"]
    for name, desc in sec["items"]:
        lines.append(f"▫️ <b>{name}</b>\n   {desc}\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="hlp:menu",
                             style=ButtonStyle.DANGER)]])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb,
                               parse_mode="HTML")
    await cb.answer()


@help_ai_router.callback_query(F.data == "hlp:howto")
async def hlp_howto(cb: CallbackQuery):
    rows = [[InlineKeyboardButton(text=q, callback_data=f"hlp:h:{i}")]
            for i, (q, _a) in enumerate(HOWTO)]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="hlp:menu",
                                      style=ButtonStyle.DANGER)])
    await cb.message.edit_text("📖 <b>کارهای پرتکرار</b>\n\nکدام را می‌خواهی؟",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               parse_mode="HTML")
    await cb.answer()


@help_ai_router.callback_query(F.data.startswith("hlp:h:"))
async def hlp_howto_item(cb: CallbackQuery):
    try:
        q, a = HOWTO[int(cb.data.rsplit(":", 1)[1])]
    except Exception:
        return await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="hlp:howto",
                             style=ButtonStyle.DANGER)]])
    await cb.message.edit_text(f"<b>{q}</b>\n\n{a}", reply_markup=kb,
                               parse_mode="HTML")
    await cb.answer()


@help_ai_router.callback_query(F.data == "hlp:ask")
async def hlp_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    await state.set_state(HelpFSM.ask)
    await cb.message.answer(
        "❓ سؤالت را بنویس.\n\n"
        "مثال: «چطور درگاه پرداخت وصل کنم؟» یا «کانال لاگ کجاست؟»",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@help_ai_router.message(HelpFSM.ask, F.text)
async def hlp_answer(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    await state.clear()
    q = (msg.text or "").strip()
    if not q:
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ سؤال دیگر", callback_data="hlp:ask")],
        [InlineKeyboardButton(text="📘 فهرست راهنما", callback_data="hlp:menu",
                              style=ButtonStyle.DANGER)],
    ])

    # لایهٔ ۱ — بدونِ هزینه
    local = local_search(q)
    if local:
        return await msg.answer(local, reply_markup=kb, parse_mode="HTML")

    # لایهٔ ۲ — هوشِ مصنوعی
    if not row:
        return await msg.answer("جوابی پیدا نکردم.", reply_markup=kb)
    if not await _quota_ok(row.id, msg.from_user.id):
        return await msg.answer(
            f"سهمیهٔ پرسشِ امروزت ({DAILY_LIMIT} سؤال) تمام شد. "
            "از فهرستِ راهنما استفاده کن یا فردا دوباره بپرس.",
            reply_markup=kb)

    await msg.answer("⏳ بگذار ببینم…")
    ans = await ask_ai(row.id, q)
    if ans:
        await msg.answer(ans, reply_markup=kb)
    else:
        await msg.answer(
            "جوابِ دقیقی پیدا نکردم. از فهرستِ راهنما پایین استفاده کن "
            "یا از پشتیبانی بپرس.", reply_markup=kb)
