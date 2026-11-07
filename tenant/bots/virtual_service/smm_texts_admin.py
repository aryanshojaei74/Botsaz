"""
smm_texts_admin.py — PATCH_SMM_CUSTOM_TEXTS_V1
پنلِ ویرایشِ متن‌هایِ اطلاع‌رسانیِ وضعیتِ سفارش.
"""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from tenant.bots.virtual_service.smm_texts import TEXTS, get_text

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

smm_texts_router = Router()


class TxtFSM(StatesGroup):
    edit = State()


# === PATCH_TEXT_FORMATTING_V1 ===
# گزارش فدایی: «می‌توانیم توی متن‌های اطلاع‌رسانی سفارش از حالت‌های متن تلگرام (بولد و …) استفاده کنیم؟»
# پیام‌ها با HTML فرستاده می‌شوند، پس تگ‌های HTML کار می‌کردند، ولی متن بولد‌شده با خود تلگرام فقط به‌صورت
# متن ساده ذخیره می‌شد (قالب گم می‌شد) و یک تگ ناقص باعث ارسال‌نشدن کل پیام می‌شد.
_FORMAT_ENTITIES = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link",
                    "blockquote", "expandable_blockquote"}
_TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler",
            "blockquote", "tg-emoji"}
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)(?:\s[^<>]*)?>")


def html_problem(text: str) -> str | None:
    """None اگر متن برای ارسال با HTML تلگرام سالم است؛ وگرنه توضیح فارسی مشکل."""
    stack: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(text):
        if "<" in text[pos:m.start()]:
            return "علامت «<» تنها یا تگ ناشناخته دارد (برای نوشتن خود علامت از &lt; استفاده کن)."
        pos = m.end()
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name not in _TG_TAGS:
            return f"تگ «{name}» را تلگرام نمی‌شناسد (مجاز: b, i, u, s, code, pre, a, blockquote, tg-spoiler)."
        if closing:
            if not stack or stack[-1] != name:
                return f"تگ بستن «{name}» جای درستی نیست."
            stack.pop()
        else:
            stack.append(name)
    if "<" in text[pos:]:
        return "علامت «<» تنها یا تگ ناشناخته دارد (برای نوشتن خود علامت از &lt; استفاده کن)."
    if stack:
        return f"تگ «{stack[-1]}» بسته نشده."
    return None


# === PATCH_SMM_ADMIN_ALERT_TOGGLES_V1 === (کلیدِ تنظیم، برچسبِ دکمه)
_ALERTS = (
    ("smm_alert_purchase",      "پیامِ «خرید/ثبتِ سفارش» به من"),
    ("smm_alert_provider_info", "پیامِ «جزئیاتِ ارائه‌دهنده» به من"),
    ("smm_alert_panel_reject",  "پیامِ «سفارش رویِ پنل رد/لغو شد» به من"),
    ("smm_alert_manual_order",  "پیامِ «سفارشِ دستیِ جدید» به من"),
)
_ALERT_KEYS = {k for k, _ in _ALERTS}


async def _menu(bot_id: int):
    from tenant import get_setting
    rows = []
    for key, (label, _d) in TEXTS.items():
        custom = (await get_setting(bot_id, f"smm_txt_{key}", "") or "").strip()
        mark = "✏️" if custom else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {label}", callback_data=f"smmtxt:e:{key}")])
    # === PATCH_SMM_ADMIN_ALERT_TOGGLES_V1 ===
    # پیام‌هایی که *به خودِ ادمین/مالک* می‌آید — هرکدام یک کلیدِ خاموش/روشن (پیش‌فرض روشن).
    for _k, _lbl in _ALERTS:
        _on = (await get_setting(bot_id, _k, "1")) != "0"
        rows.append([InlineKeyboardButton(
            text=(f"🔔 {_lbl}: ✅ روشن" if _on else f"🔕 {_lbl}: ⬜️ خاموش"),
            callback_data=f"smmtxt:al:{_k}",
            style=(ButtonStyle.SUCCESS if _on else ButtonStyle.PRIMARY))])
    # === PATCH_TEXT_VARS_V1 ===
    rows.append([InlineKeyboardButton(
        text="🔤 لیستِ متغیرها", callback_data="tvars:list", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="♻️ بازگرداندنِ همه به پیش‌فرض",
        callback_data="smmtxt:resetall", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data="smm:menu", style=ButtonStyle.DANGER)])
    txt = ("✍️ <b>متن‌هایِ اطلاع‌رسانیِ سفارش</b>\n\n"
           "این‌ها پیام‌هایی هستند که با تغییرِ وضعیتِ سفارش به مشتری "
           "فرستاده می‌شوند. روی هرکدام بزن تا متنش را عوض کنی.\n\n"
           "✏️ = متنِ دلخواهِ تو · ▫️ = متنِ پیش‌فرض\n\n"
           "🔔 دکمه‌هایِ سبز/خاکستریِ پایین، پیام‌هایی را که <b>به خودت</b> می‌آید روشن/خاموش می‌کنند "
           "(زدن = تغییر). خاموش‌کردنشان روی پیامِ مشتری و کانالِ اعتمادسازی اثری ندارد.\n\n"
           "🔤 داخلِ هر متن می‌توانی متغیر بگذاری (مثلاً <code>{{product_name}}</code>، "
           "<code>{{quantity}}</code>، <code>{{order_id}}</code>)؛ لیستِ کاملش با دکمهٔ پایین.")
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id):
    txt, kb = await _menu(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@smm_texts_router.callback_query(F.data == "smm:texts")
async def smmtxt_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


# === PATCH_SMM_ALERT_TOGGLE_V1 ===
@smm_texts_router.callback_query(F.data == "smmtxt:alert")
async def smmtxt_alert_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        return
    now_on = (await get_setting(row.id, "smm_alert_panel_reject", "1")) != "0"
    await set_setting(row.id, "smm_alert_panel_reject", "0" if now_on else "1")
    await cb.answer(
        "🔕 خاموش شد. دیگر برایِ هر سفارشِ ردشده پیام نمی‌گیری؛ فقط اگر موجودیِ پنل تمام شود "
        "(حداکثر هر ۶ ساعت یک‌بار) خبرت می‌کنم." if now_on
        else "🔔 روشن شد. هر سفارشِ ردشده رویِ پنل را برایت می‌فرستم.",
        show_alert=True)
    await _show(cb, row.id)


# === PATCH_SMM_ADMIN_ALERT_TOGGLES_V1 ===
@smm_texts_router.callback_query(F.data.startswith("smmtxt:al:"))
async def smmtxt_alert_toggle_any(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in _ALERT_KEYS:
        return await cb.answer()
    now_on = (await get_setting(row.id, key, "1")) != "0"
    await set_setting(row.id, key, "0" if now_on else "1")
    extra = ""
    if now_on and key == "smm_alert_panel_reject":
        extra = " (فقط هشدارِ «موجودیِ پنل تمام شد» حداکثر هر ۶ ساعت یک‌بار می‌آید)"
    await cb.answer(("🔕 خاموش شد" + extra) if now_on else "🔔 روشن شد", show_alert=bool(extra))
    await _show(cb, row.id)


@smm_texts_router.callback_query(F.data.startswith("smmtxt:e:"))
async def smmtxt_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in TEXTS:
        return await cb.answer()
    label, default = TEXTS[key]
    cur = await get_text(row.id, key)
    await state.update_data(smmtxt_key=key)
    await state.set_state(TxtFSM.edit)
    await cb.message.answer(
        f"✍️ <b>{label}</b>\n\n"
        f"<b>متنِ فعلی:</b>\n{cur}\n\n"
        "متنِ تازه را بفرست. متغیرها مجازند، مثلاً <code>{{product_name}}</code> "
        "<code>{{quantity}}</code> <code>{{amount}}</code> <code>{{link}}</code> "
        "<code>{{order_id}}</code> (لیستِ کامل در منویِ قبلی).\n"
        "🎨 قالب‌بندی: می‌توانی متن را در خود تلگرام بولد/ایتالیک/… کنی و بفرستی (یا از تگ استفاده کنی: "
        "<code>&lt;b&gt;پررنگ&lt;/b&gt;</code> <code>&lt;i&gt;کج&lt;/i&gt;</code> <code>&lt;u&gt;زیرخط&lt;/u&gt;</code> "
        "<code>&lt;code&gt;کپی‌شدنی&lt;/code&gt;</code> <code>&lt;blockquote&gt;نقل‌قول&lt;/blockquote&gt;</code>).\n"
        "برایِ برگشت به پیش‌فرض، کلمهٔ <code>پیشفرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@smm_texts_router.message(TxtFSM.edit, F.text)
async def smmtxt_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(msg, bot)
    data = await state.get_data()
    key = data.get("smmtxt_key")
    if not row or key not in TEXTS:
        await state.clear()
        return
    t = (msg.text or "").strip()
    if t not in ("پیشفرض", "پیش‌فرض", "default"):
        # === PATCH_TEXT_FORMATTING_V1 === بولد/ایتالیک/… که با خود تلگرام زده شده به HTML تبدیل می‌شود
        if any(getattr(e, "type", None) in _FORMAT_ENTITIES for e in (msg.entities or [])):
            t = (msg.html_text or t).strip()
        _prob = html_problem(t)
        if _prob:
            await msg.answer(f"❌ قالب متن ایراد دارد: {_prob}\nاصلاحش کن و دوباره بفرست (یا لغو کن).")
            return                      # حالت ویرایش باز می‌ماند
    await state.clear()
    if t in ("پیشفرض", "پیش‌فرض", "default"):
        await set_setting(row.id, f"smm_txt_{key}", "")
        await msg.answer("♻️ به متنِ پیش‌فرض برگشت.")
    else:
        await set_setting(row.id, f"smm_txt_{key}", t[:2000])
        await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


@smm_texts_router.callback_query(F.data == "smmtxt:resetall")
async def smmtxt_reset(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    from tenant.bots.virtual_service.smm import _owner
    row = await _owner(cb, bot)
    if not row:
        return
    for key in TEXTS:
        await set_setting(row.id, f"smm_txt_{key}", "")
    await cb.answer("همه به پیش‌فرض برگشتند.", show_alert=True)
    await _show(cb, row.id)
