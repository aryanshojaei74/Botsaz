"""
platform_community.py — بخشِ «انجمن بات‌ساز» جدا از platform_bot.py.

استخراج‌شده از platform_bot.py (خطوط ۱۴۶۲۷–۱۵۴۳۶ قدیمی) به فایلِ مستقل تا
platform_bot.py سبک‌تر بشه. رفتار ۱۰۰٪ حفظ شده — همون هندلرها با همون
callback_dataها، فقط توی روتر جدا (community_router) که در platform_bot.py
ثبت می‌شه.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import Session, PLATFORM_ADMIN_ID
from models import CommunityButton, CommunityStep
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger

# === PATCH_FIX_COMMUNITY_BTN_CANCEL_V1 ===
# قبلاً BTN_CANCEL هیچ‌جا تعریف/ایمپورت نشده بود و همهٔ چک‌های لغو NameError می‌دادند.
BTN_CANCEL = "❌ لغو"
logger = _get_logger(__name__)

# روتر مخصوصِ این ماژول — تو platform_bot.py با
# `router.include_router(community_router)` اضافه می‌شه.
community_router = Router()

# سازگاری با کدِ قدیمی: کمک‌تابع‌هایی که توی platform_bot.py تعریف شدن رو
# lazy import می‌کنیم داخلِ توابع تا ایمپورت دَورِ نداشته باشیم.

async def smart_cancel_kb():
    """Lazy wrapper — کیبورد «لغو» رو از platform_bot می‌گیره."""
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


async def smart_home_kb(uid: int, show_admin: bool = False):
    """Lazy wrapper — کیبورد اصلی رو از platform_bot می‌گیره."""
    from platform_bot import smart_home_kb as _fn
    return await _fn(uid, show_admin=show_admin)


# ======================= انجمن بات‌ساز =======================

class CommunityFSM(StatesGroup):
    # ساخت دکمه جدید
    btn_title    = State()
    btn_color    = State()
    step_content = State()    # دریافت عکس+متن یا فقط متن هر مرحله
    # ویرایش
    edit_pick    = State()    # انتخاب دکمه برای ویرایش
    edit_menu    = State()    # منوی ویرایش دکمه
    edit_step    = State()    # ویرایش محتوای یک مرحله خاص
    edit_rename  = State()    # دریافت اسم جدید (ویرایش)
    edit_recolor = State()    # انتخاب رنگ جدید (ویرایش)


BTN_COMMUNITY_CANCEL = "❌ لغو"

_COMM_COLOR_MAP = {
    "danger":  ButtonStyle.DANGER,
    "success": ButtonStyle.SUCCESS,
    "primary": ButtonStyle.PRIMARY,
}
_COMM_COLOR_LABEL = {
    "danger":  "🔴 قرمز",
    "success": "🟢 سبز",
    "primary": "🔵 آبی",
}


# ── نمایش انجمن به کاربر ──

async def community_list_kb() -> InlineKeyboardMarkup:
    async with Session() as s:
        btns = (await s.scalars(
            select(CommunityButton).order_by(CommunityButton.id)
        )).all()
    rows = []
    for b in btns:
        style = _COMM_COLOR_MAP.get(b.color or "")
        btn = hx_button(b.title,
            callback_data=f"comm:view:{b.id}",
            style=style,
        )
        rows.append([btn])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("هنوز آموزشی ثبت نشده", callback_data="noop")]
    ])


async def community_home(msg: Message):
    kb = await community_list_kb()
    await msg.answer(
        "🏛 <b>انجمن بات‌ساز</b>\n\n"
        "آموزش‌های مرحله‌به‌مرحله را انتخاب کن 👇",
        reply_markup=kb,
    )


@community_router.callback_query(F.data.startswith("comm:view:"))
async def community_view_start(cb: CallbackQuery):
    btn_id = int(cb.data.split(":")[2])
    await _send_community_step(cb.message, btn_id, step_num=1, edit=True)
    await cb.answer()


async def _send_community_step(message, btn_id: int, step_num: int, edit: bool = False):
    async with Session() as s:
        btn = await s.get(CommunityButton, btn_id)
        if not btn:
            await message.answer("آموزش یافت نشد.")
            return
        steps = (await s.scalars(
            select(CommunityStep)
            .where(CommunityStep.button_id == btn_id)
            .order_by(CommunityStep.step_num)
        )).all()
    total = len(steps)
    if not steps:
        await message.answer("این آموزش هنوز محتوایی ندارد.")
        return
    step_num = max(1, min(step_num, total))
    step = steps[step_num - 1]

    nav = []
    if step_num > 1:
        nav.append(hx_button("⬅️ قبلی", callback_data=f"comm:step:{btn_id}:{step_num-1}"))
    nav.append(hx_button(f"{step_num}/{total}", callback_data="noop"))
    if step_num < total:
        nav.append(hx_button("بعدی ➡️", callback_data=f"comm:step:{btn_id}:{step_num+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav, [
        hx_button("🔙 بازگشت به انجمن", callback_data="comm:back")
    ]])

    caption = f"📖 <b>{btn.title}</b> — مرحله {step_num} از {total}\n\n{step.text or ''}"

    if step.photo_file_id:
        try:
            if edit:
                await message.delete()
            await message.answer_photo(step.photo_file_id, caption=caption, reply_markup=kb)
        except Exception:
            await message.answer(caption, reply_markup=kb)
    else:
        try:
            if edit:
                await message.edit_text(caption, reply_markup=kb)
            else:
                await message.answer(caption, reply_markup=kb)
        except Exception:
            await message.answer(caption, reply_markup=kb)


@community_router.callback_query(F.data.startswith("comm:step:"))
async def community_step_nav(cb: CallbackQuery):
    parts = cb.data.split(":")
    btn_id, step_num = int(parts[2]), int(parts[3])
    await _send_community_step(cb.message, btn_id, step_num, edit=True)
    await cb.answer()


@community_router.callback_query(F.data == "comm:back")
async def community_back(cb: CallbackQuery):
    kb = await community_list_kb()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🏛 <b>انجمن بات‌ساز</b>\n\nآموزش‌های مرحله‌به‌مرحله را انتخاب کن 👇",
        reply_markup=kb,
    )
    await cb.answer()


# ── پنل ادمین انجمن ──

def community_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("➕ اضافه کردن دکمه", callback_data="cadm:add",
                              style=ButtonStyle.SUCCESS)],
        [hx_button("✏️ ویرایش دکمه", callback_data="cadm:edit_pick",
                              style=ButtonStyle.PRIMARY)],
        [hx_button("🗑 حذف دکمه", callback_data="cadm:del_pick",
                              style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت", callback_data="adm:back",
                              style=ButtonStyle.DANGER)],
    ])


@community_router.callback_query(F.data == "adm:community")
async def adm_community(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        btns = (await s.scalars(select(CommunityButton).order_by(CommunityButton.id))).all()
    lines = [f"{'  '.join([_COMM_COLOR_LABEL.get(b.color or '','⬜'), b.title])}" for b in btns]
    text = "🏛 <b>تنظیمات انجمن بات‌ساز</b>\n\n"
    text += ("\n".join(f"• {l}" for l in lines) if lines else "هنوز دکمه‌ای ثبت نشده.") + "\n\nعملیات مورد نظر را انتخاب کن:"
    try:
        await cb.message.edit_text(text, reply_markup=community_admin_kb())
    except Exception:
        await cb.message.answer(text, reply_markup=community_admin_kb())
    await cb.answer()


# ── اضافه کردن دکمه ──

@community_router.callback_query(F.data == "cadm:add")
async def cadm_add_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(CommunityFSM.btn_title)
    await state.update_data(comm_steps=[])
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "➕ <b>اضافه کردن دکمه جدید به انجمن</b>\n\n"
        "مرحله ۱: اسم دکمه را بنویس:",
        reply_markup=await smart_cancel_kb(),
    )
    await cb.answer()


@community_router.message(CommunityFSM.btn_title, F.text)
async def cadm_add_title(msg: Message, state: FSMContext):
    title = (msg.text or "").strip()
    if not title or title == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
        return
    await state.update_data(comm_title=title)
    await state.set_state(CommunityFSM.btn_color)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("🔴 قرمز", callback_data="cadm:color:danger",  style=ButtonStyle.DANGER),
        hx_button("🟢 سبز",  callback_data="cadm:color:success", style=ButtonStyle.SUCCESS),
        hx_button("🔵 آبی",  callback_data="cadm:color:primary", style=ButtonStyle.PRIMARY),
    ]])
    await msg.answer(
        f"✅ اسم: <b>{title}</b>\n\nمرحله ۲: رنگ دکمه را انتخاب کن:",
        reply_markup=kb,
    )


def _comm_step_kb(is_first: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not is_first:
        rows.append([
            hx_button("⬅️ بازگشت به مرحله قبل", callback_data="cadm:step:back",
                                 style=ButtonStyle.DANGER),
        ])
    rows.append([hx_button("➡️ مرحله بعدی", callback_data="cadm:step:next",
                                      style=ButtonStyle.SUCCESS)])
    rows.append([hx_button("✅ آموزش تموم شد — ذخیره", callback_data="cadm:step:done",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([hx_button("❌ لغو کل", callback_data="cadm:step:cancel",
                                      style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@community_router.message(CommunityFSM.step_content, F.photo | F.text)
async def cadm_step_receive(msg: Message, state: FSMContext):
    data = await state.get_data()
    steps = data.get("comm_steps", [])
    if msg.photo:
        file_id = msg.photo[-1].file_id
        text = msg.html_text  # حفظ قالب‌بندی تلگرام (بولد/ایتالیک/کوتیشن/...)
        plain = msg.caption or ""
        steps.append({"photo": file_id, "text": text})
        preview = f"🖼 عکس + متن: {plain[:40]}..." if len(plain) > 40 else f"🖼 عکس + متن: {plain}"
    else:
        plain = (msg.text or "").strip()
        if not plain or plain == BTN_CANCEL:
            await state.clear()
            await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
            return
        text = msg.html_text  # حفظ قالب‌بندی تلگرام (بولد/ایتالیک/کوتیشن/...)
        steps.append({"photo": None, "text": text})
        preview = f"📝 متن: {plain[:50]}..." if len(plain) > 50 else f"📝 متن: {plain}"

    await state.update_data(comm_steps=steps)
    step_num = len(steps)
    await msg.answer(
        f"✅ مرحله {step_num} ذخیره شد:\n{preview}\n\n"
        f"حالا چیکار کنم؟",
        reply_markup=_comm_step_kb(is_first=False),
    )


# === PATCH_OLD_BUGS_V1 ===
# استثنا لازم است، وگرنه این هندلر «cadm:step:done» را می‌بلعد و
# هندلرِ اختصاصی‌اش (پایین‌تر) هرگز اجرا نمی‌شود.
@community_router.callback_query(
    CommunityFSM.step_content,
    F.data.startswith("cadm:step:") & ~F.data.startswith("cadm:step:done"))
async def cadm_step_action(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":")[2]
    data = await state.get_data()
    steps = data.get("comm_steps", [])

    if action == "cancel":
        await state.clear()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer("لغو شد.", reply_markup=await smart_home_kb(cb.from_user.id))
        await cb.answer()
        return

    if action == "back":
        if steps:
            steps.pop()
            await state.update_data(comm_steps=steps)
        step_num = len(steps)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(
            f"↩️ مرحله {step_num + 1} حذف شد.\n"
            f"الان {step_num} مرحله داری.\n\n"
            "محتوای مرحله بعدی را بفرست:",
            reply_markup=_comm_step_kb(is_first=(step_num == 0)),
        )
        await cb.answer()
        return

    if action == "next":
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        step_num = len(steps)
        await cb.message.answer(
            f"مرحله {step_num + 1} را بفرست (عکس با کپشن یا متن):",
            reply_markup=_comm_step_kb(is_first=False),
        )
        await cb.answer()
        return

    if action == "done":
        if not steps:
            await cb.answer("⚠️ هنوز هیچ مرحله‌ای اضافه نکردی!", show_alert=True)
            return
        edit_id = data.get("cadm_edit_id")
        edit_mode = data.get("cadm_edit_mode")

        if edit_id and edit_mode == "rewrite":
            # بازنویسی: مراحل قدیمی پاک و جدید ذخیره می‌شوند
            async with Session() as s:
                old_steps = (await s.scalars(
                    select(CommunityStep).where(CommunityStep.button_id == edit_id)
                )).all()
                for os_ in old_steps:
                    await s.delete(os_)
                for i, st in enumerate(steps, 1):
                    s.add(CommunityStep(
                        button_id=edit_id,
                        step_num=i,
                        text=st["text"],
                        photo_file_id=st["photo"],
                    ))
                await s.commit()
                btn = await s.get(CommunityButton, edit_id)
                title = btn.title if btn else "دکمه"
            await state.clear()
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await cb.message.answer(
                f"✅ آموزش «{title}» با {len(steps)} مرحله بازنویسی شد!",
                reply_markup=await smart_home_kb(cb.from_user.id),
            )
            await cb.answer()
            return

        title = data.get("comm_title", "بدون عنوان")
        color = data.get("comm_color")
        async with Session() as s:
            new_btn = CommunityButton(title=title, color=color)
            s.add(new_btn)
            await s.flush()
            for i, st in enumerate(steps, 1):
                s.add(CommunityStep(
                    button_id=new_btn.id,
                    step_num=i,
                    text=st["text"],
                    photo_file_id=st["photo"],
                ))
            await s.commit()
        await state.clear()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer(
            f"✅ دکمه «{title}» با {len(steps)} مرحله در انجمن ثبت شد!",
            reply_markup=await smart_home_kb(cb.from_user.id),
        )
        await cb.answer()


# ── حذف دکمه ──

@community_router.callback_query(F.data == "cadm:del_pick")
async def cadm_del_pick(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        btns = (await s.scalars(select(CommunityButton).order_by(CommunityButton.id))).all()
    if not btns:
        await cb.answer("دکمه‌ای وجود ندارد.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for btn in btns:
        style = _COMM_COLOR_MAP.get(btn.color or "")
        b.button(text=btn.title, callback_data=f"cadm:del:{btn.id}", style=style)
    b.button(text="⬅️ بازگشت", callback_data="adm:community", style=ButtonStyle.DANGER)
    b.adjust(1)
    try:
        await cb.message.edit_text("🗑 کدام دکمه را حذف کنم؟", reply_markup=b.as_markup())
    except Exception:
        await cb.message.answer("🗑 کدام دکمه را حذف کنم؟", reply_markup=b.as_markup())
    await cb.answer()


@community_router.callback_query(F.data.startswith("cadm:del:"))
async def cadm_del_confirm(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    btn_id = int(cb.data.split(":")[2])
    async with Session() as s:
        btn = await s.get(CommunityButton, btn_id)
        if not btn:
            await cb.answer("یافت نشد", show_alert=True)
            return
        title = btn.title
        steps = (await s.scalars(
            select(CommunityStep).where(CommunityStep.button_id == btn_id)
        )).all()
        for st in steps:
            await s.delete(st)
        await s.delete(btn)
        await s.commit()
    try:
        await cb.message.edit_text(f"✅ دکمه «{title}» و {len(steps)} مرحله‌اش حذف شد.")
    except Exception:
        await cb.message.answer(f"✅ دکمه «{title}» و {len(steps)} مرحله‌اش حذف شد.")
    await cb.answer()
    await adm_community(cb)


# ── ویرایش دکمه ──

@community_router.callback_query(F.data == "cadm:edit_pick")
async def cadm_edit_pick(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        btns = (await s.scalars(select(CommunityButton).order_by(CommunityButton.id))).all()
    if not btns:
        await cb.answer("دکمه‌ای وجود ندارد.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for btn in btns:
        style = _COMM_COLOR_MAP.get(btn.color or "")
        b.button(text=btn.title, callback_data=f"cadm:edit_menu:{btn.id}", style=style)
    b.button(text="⬅️ بازگشت", callback_data="adm:community", style=ButtonStyle.DANGER)
    b.adjust(1)
    try:
        await cb.message.edit_text("✏️ کدام دکمه را ویرایش کنی؟", reply_markup=b.as_markup())
    except Exception:
        await cb.message.answer("✏️ کدام دکمه را ویرایش کنی؟", reply_markup=b.as_markup())
    await cb.answer()


async def _build_comm_edit_menu(btn_id: int):
    """متن و کیبورد منوی ویرایش یک دکمه را می‌سازد (قابل استفاده مجدد)."""
    async with Session() as s:
        btn = await s.get(CommunityButton, btn_id)
        if not btn:
            return None, None
        steps = (await s.scalars(
            select(CommunityStep)
            .where(CommunityStep.button_id == btn_id)
            .order_by(CommunityStep.step_num)
        )).all()
    color_lbl = _COMM_COLOR_LABEL.get(btn.color or "", "پیش‌فرض")

    # ردیف‌های مراحل — هر مرحله یک دکمه جداگانه
    step_rows = []
    for st in steps:
        preview = (st.text or "")[:22] + ("…" if len(st.text or "") > 22 else "")
        icon = "🖼" if st.photo_file_id else "📝"
        step_rows.append([hx_button(f"{icon} مرحله {st.step_num}: {preview}",
            callback_data=f"cadm:edit_step:{st.id}:{btn_id}",
            style=ButtonStyle.PRIMARY,
        )])

    action_rows = [
        [hx_button("📝 تغییر اسم دکمه", callback_data=f"cadm:ren:{btn_id}")],
        [hx_button("🎨 تغییر رنگ دکمه", callback_data=f"cadm:recolor:{btn_id}")],
        [hx_button("🔄 بازنویسی کامل آموزش", callback_data=f"cadm:rewrite:{btn_id}",
                              style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت", callback_data="cadm:edit_pick",
                              style=ButtonStyle.DANGER)],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=step_rows + action_rows)

    text = (
        f"✏️ ویرایش دکمه: <b>{btn.title}</b>\n"
        f"رنگ: {color_lbl} | مراحل: {len(steps)}\n\n"
        f"روی هر مرحله بزن تا محتوایش را ویرایش کنی:"
    )
    return text, kb


async def _show_comm_edit_menu(cb: CallbackQuery, state: FSMContext, btn_id: int):
    """بازگشت به منوی ویرایش دکمه (صفحه قبل)."""
    text, kb = await _build_comm_edit_menu(btn_id)
    if text is None:
        await cb.answer("یافت نشد", show_alert=True)
        return
    await state.set_state(CommunityFSM.edit_menu)
    await state.update_data(cadm_edit_id=btn_id, cadm_edit_mode=None,
                            pending_title=None, pending_color=None)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@community_router.callback_query(F.data.startswith("cadm:edit_menu:"))
async def cadm_edit_menu(cb: CallbackQuery, state: FSMContext):
    btn_id = int(cb.data.split(":")[2])
    await _show_comm_edit_menu(cb, state, btn_id)
    await cb.answer()


@community_router.callback_query(F.data.startswith("cadm:edit_step:"))
async def cadm_edit_step_start(cb: CallbackQuery, state: FSMContext):
    """شروع ویرایش یک مرحله خاص."""
    parts = cb.data.split(":")
    step_id, btn_id = int(parts[2]), int(parts[3])
    async with Session() as s:
        st = await s.get(CommunityStep, step_id)
        if not st:
            await cb.answer("مرحله یافت نشد", show_alert=True)
            return
        step_num = st.step_num
        has_photo = bool(st.photo_file_id)
        cur_text = st.text or ""

    await state.set_state(CommunityFSM.edit_step)
    await state.update_data(cadm_step_id=step_id, cadm_edit_id=btn_id)

    icon = "🖼" if has_photo else "📝"
    cur_preview = cur_text[:80] + ("…" if len(cur_text) > 80 else "")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"{icon} <b>ویرایش مرحله {step_num}</b>\n\n"
        f"محتوای فعلی:\n<i>{cur_preview or '(خالی)'}</i>\n\n"
        "محتوای جدید را بفرست:\n"
        "• فقط متن  <b>یا</b>\n"
        "• عکس با کپشن (متن روی عکس)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            hx_button("❌ لغو", callback_data=f"cadm:edit_menu:{btn_id}",
                                 style=ButtonStyle.DANGER)
        ]]),
    )
    await cb.answer()


@community_router.message(CommunityFSM.edit_step, F.photo | F.text)
async def cadm_edit_step_save(msg: Message, state: FSMContext):
    """ذخیره محتوای ویرایش‌شده یک مرحله."""
    data = await state.get_data()
    step_id = data["cadm_step_id"]
    btn_id  = data["cadm_edit_id"]

    if msg.text and (msg.text.strip() == BTN_CANCEL or msg.text.strip() == "❌ لغو"):
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
        return

    async with Session() as s:
        st = await s.get(CommunityStep, step_id)
        if not st:
            await state.clear()
            await msg.answer("مرحله یافت نشد.")
            return
        step_num = st.step_num
        if msg.photo:
            st.photo_file_id = msg.photo[-1].file_id
            st.text = msg.html_text  # حفظ قالب‌بندی تلگرام
        else:
            st.photo_file_id = None
            st.text = msg.html_text  # حفظ قالب‌بندی تلگرام
        await s.commit()

    await state.clear()
    await msg.answer(
        f"✅ مرحله {step_num} ویرایش شد.",
        reply_markup=await smart_home_kb(msg.from_user.id),
    )
    # بازگشت به منوی ویرایش همان دکمه
    class _FakeCB:
        data = f"cadm:edit_menu:{btn_id}"
        from_user = msg.from_user
        message = msg
        async def answer(self, *a, **kw): pass
    await cadm_edit_menu(_FakeCB(), state)


@community_router.callback_query(F.data.startswith("cadm:ren:"))
async def cadm_rename_start(cb: CallbackQuery, state: FSMContext):
    btn_id = int(cb.data.split(":")[2])
    await state.set_state(CommunityFSM.edit_rename)
    await state.update_data(cadm_edit_id=btn_id, cadm_edit_mode="rename")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("📝 اسم جدید دکمه را بنویس:")
    await cb.answer()


@community_router.message(CommunityFSM.edit_rename, F.text)
async def cadm_rename_input(msg: Message, state: FSMContext):
    """اسم جدید را می‌گیرد و قبل از اعمال، تأیید می‌خواهد."""
    title = (msg.text or "").strip()
    if not title:
        await msg.answer("اسم نمی‌تواند خالی باشد. دوباره بنویس:")
        return
    await state.update_data(pending_title=title)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("💾 ذخیره", callback_data="cadm:ren_ok",
                             style=ButtonStyle.SUCCESS),
        hx_button("🔙 بازگشت", callback_data="cadm:ren_no",
                             style=ButtonStyle.DANGER),
    ]])
    await msg.answer(
        f"اسم جدید: <b>{title}</b>\n\nتأیید می‌کنی؟",
        reply_markup=kb,
    )


@community_router.callback_query(CommunityFSM.edit_rename, F.data == "cadm:ren_ok")
async def cadm_rename_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cadm_edit_id")
    title = (data.get("pending_title") or "").strip()
    if not btn_id or not title:
        await cb.answer("اطلاعات ناقص است.", show_alert=True)
        return
    async with Session() as s:
        btn = await s.get(CommunityButton, btn_id)
        if btn:
            btn.title = title
            await s.commit()
    await cb.answer("✅ اسم تغییر کرد")
    await _show_comm_edit_menu(cb, state, btn_id)


@community_router.callback_query(CommunityFSM.edit_rename, F.data == "cadm:ren_no")
async def cadm_rename_cancel(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cadm_edit_id")
    await cb.answer("لغو شد")
    if btn_id:
        await _show_comm_edit_menu(cb, state, btn_id)
    else:
        await adm_community(cb)


@community_router.message(CommunityFSM.btn_title, F.text)
async def cadm_rename_or_create_title(msg: Message, state: FSMContext):
    title = (msg.text or "").strip()
    if not title or title == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
        return
    data = await state.get_data()
    edit_id = data.get("cadm_edit_id")
    edit_mode = data.get("cadm_edit_mode")

    if edit_id and edit_mode == "rename":
        async with Session() as s:
            btn = await s.get(CommunityButton, edit_id)
            if btn:
                btn.title = title
                await s.commit()
        await state.clear()
        await msg.answer(f"✅ اسم دکمه به «{title}» تغییر یافت.", reply_markup=await smart_home_kb(msg.from_user.id))
    else:
        # ساخت جدید — برو سراغ رنگ
        await state.update_data(comm_title=title)
        await state.set_state(CommunityFSM.btn_color)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            hx_button("🔴 قرمز", callback_data="cadm:color:danger",  style=ButtonStyle.DANGER),
            hx_button("🟢 سبز",  callback_data="cadm:color:success", style=ButtonStyle.SUCCESS),
            hx_button("🔵 آبی",  callback_data="cadm:color:primary", style=ButtonStyle.PRIMARY),
        ]])
        await msg.answer(
            f"✅ اسم: <b>{title}</b>\n\nرنگ دکمه را انتخاب کن:",
            reply_markup=kb,
        )


@community_router.callback_query(F.data.startswith("cadm:recolor:"))
async def cadm_recolor_start(cb: CallbackQuery, state: FSMContext):
    btn_id = int(cb.data.split(":")[2])
    await state.set_state(CommunityFSM.edit_recolor)
    await state.update_data(cadm_edit_id=btn_id, cadm_edit_mode="recolor")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("🔴 قرمز", callback_data="cadm:ecolor:danger",  style=ButtonStyle.DANGER),
        hx_button("🟢 سبز",  callback_data="cadm:ecolor:success", style=ButtonStyle.SUCCESS),
        hx_button("🔵 آبی",  callback_data="cadm:ecolor:primary", style=ButtonStyle.PRIMARY),
    ]])
    try:
        await cb.message.edit_text("🎨 رنگ جدید را انتخاب کن:", reply_markup=kb)
    except Exception:
        await cb.message.answer("🎨 رنگ جدید را انتخاب کن:", reply_markup=kb)
    await cb.answer()


@community_router.callback_query(CommunityFSM.edit_recolor, F.data.startswith("cadm:ecolor:"))
async def cadm_recolor_pick(cb: CallbackQuery, state: FSMContext):
    """رنگ انتخاب می‌شود و قبل از اعمال، تأیید می‌خواهد."""
    color = cb.data.split(":")[2]
    if color not in _COMM_COLOR_LABEL:
        await cb.answer("رنگ نامعتبر", show_alert=True)
        return
    await state.update_data(pending_color=color)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("💾 ذخیره", callback_data="cadm:rec_ok",
                             style=ButtonStyle.SUCCESS),
        hx_button("🔙 بازگشت", callback_data="cadm:rec_no",
                             style=ButtonStyle.DANGER),
    ]])
    try:
        await cb.message.edit_text(
            f"رنگ انتخابی: {_COMM_COLOR_LABEL[color]}\n\nتأیید می‌کنی؟",
            reply_markup=kb,
        )
    except Exception:
        await cb.message.answer(
            f"رنگ انتخابی: {_COMM_COLOR_LABEL[color]}\n\nتأیید می‌کنی؟",
            reply_markup=kb,
        )
    await cb.answer()


@community_router.callback_query(CommunityFSM.edit_recolor, F.data == "cadm:rec_ok")
async def cadm_recolor_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cadm_edit_id")
    color = data.get("pending_color")
    if not btn_id or color not in _COMM_COLOR_LABEL:
        await cb.answer("اطلاعات ناقص است.", show_alert=True)
        return
    async with Session() as s:
        btn = await s.get(CommunityButton, btn_id)
        if btn:
            btn.color = color
            await s.commit()
    await cb.answer("✅ رنگ تغییر کرد")
    await _show_comm_edit_menu(cb, state, btn_id)


@community_router.callback_query(CommunityFSM.edit_recolor, F.data == "cadm:rec_no")
async def cadm_recolor_cancel(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    btn_id = data.get("cadm_edit_id")
    await cb.answer("لغو شد")
    if btn_id:
        await _show_comm_edit_menu(cb, state, btn_id)
    else:
        await adm_community(cb)


@community_router.callback_query(CommunityFSM.btn_color, F.data.startswith("cadm:color:"))
async def cadm_color_handler(cb: CallbackQuery, state: FSMContext):
    color = cb.data.split(":")[2]
    if color not in _COMM_COLOR_LABEL:
        await cb.answer()
        return
    data = await state.get_data()
    edit_id = data.get("cadm_edit_id")
    edit_mode = data.get("cadm_edit_mode")

    if edit_id and edit_mode == "recolor":
        async with Session() as s:
            btn = await s.get(CommunityButton, edit_id)
            if btn:
                btn.color = color
                await s.commit()
        await state.clear()
        try:
            await cb.message.edit_text(f"✅ رنگ دکمه به «{_COMM_COLOR_LABEL[color]}» تغییر یافت.")
        except Exception:
            await cb.message.answer(f"✅ رنگ دکمه به «{_COMM_COLOR_LABEL[color]}» تغییر یافت.")
        await cb.answer()
        return

    # حالت ساخت جدید — برو سراغ محتوا
    await state.update_data(comm_color=color)
    await state.set_state(CommunityFSM.step_content)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"✅ رنگ: {_COMM_COLOR_LABEL[color]}\n\n"
        "مرحله ۳: محتوای آموزش را وارد کن.\n\n"
        "📌 <b>راهنما:</b>\n"
        "• فقط متن بفرست <b>یا</b>\n"
        "• یک عکس با کپشن (متن زیر عکس) بفرست\n\n"
        "بعد از هر مرحله یکی از دکمه‌های زیر را بزن:",
        reply_markup=_comm_step_kb(is_first=True),
    )
    await cb.answer()


@community_router.callback_query(F.data.startswith("cadm:rewrite:"))
async def cadm_rewrite_start(cb: CallbackQuery, state: FSMContext):
    btn_id = int(cb.data.split(":")[2])
    await state.set_state(CommunityFSM.step_content)
    await state.update_data(cadm_edit_id=btn_id, cadm_edit_mode="rewrite", comm_steps=[])
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "🔄 <b>بازنویسی آموزش</b>\n\n"
        "آموزش قدیمی پاک و جایگزین می‌شود.\n"
        "محتوای مرحله اول را بفرست:",
        reply_markup=_comm_step_kb(is_first=True),
    )
    await cb.answer()


@community_router.callback_query(CommunityFSM.step_content, F.data == "cadm:step:done")
async def cadm_step_done_edit(cb: CallbackQuery, state: FSMContext):
    # این handler برای حالت rewrite هم کار می‌کند — cadm_step_action اصلی را صدا می‌زند
    await cadm_step_action(cb, state)

