# -*- coding: utf-8 -*-
"""Main BotSaz platform-bot sticker management.

This router is intentionally platform-only. Tenant routers do not import it.
It stores startup and home-button stickers in PlatformSetting / home_custom_btns.
"""
from __future__ import annotations

import json
from typing import Any

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import Setting

platform_sticker_admin_router = Router()

START_KEY = "platform_start_sticker"
DEFAULT_HOME_LABELS = {
    "create": "🏗 ساخت ربات",
    "plans": "🛒 خرید پلن",
    "trial": "🧪 درخواست تست",
    "wallet": "💰 کیف پول",
    "mybots": "🤖 ربات‌های من",
    "profile": "👤 پروفایل",
    "referral": "🎁 دعوت دوستان",
    "ticket": "🎫 پشتیبانی",
    "feedback": "💬 نظرات",
    "community": "🏛 انجمن",
    "top_customers": "🏆 نفرات برتر",
    "dice_game": "🎲 بازی کن پول بگیر",
    "partner_req": "🤝 درخواست همکاری",
    "partner_build": "🧱 ساخت ربات همکار",
    "self_saz": "🧩 سلف‌ساز",
}

class PlatformStickerFSM(StatesGroup):
    wait_sticker = State()

async def _admin(cb: CallbackQuery) -> bool:
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی دسترسی دارد.", show_alert=True)
        return False
    return True

async def _admin_msg(msg: Message) -> bool:
    return msg.from_user.id == PLATFORM_ADMIN_ID

async def _get(key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
    return row.value if row else default

async def _set(key: str, value: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()

async def _custom_buttons() -> list[dict[str, Any]]:
    try:
        raw = await _get("home_custom_btns", "[]")
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []

async def _home_items() -> list[tuple[str, str, bool]]:
    items: list[tuple[str, str, bool]] = []
    for key, default in DEFAULT_HOME_LABELS.items():
        label = (await _get(f"hbtn:{key}:label", default)) or default
        sticker = bool((await _get(f"hbtn:{key}:sticker", "")).strip())
        items.append((key, label, sticker))
    for row in await _custom_buttons():
        bid = str(row.get("id") or "").strip()
        if not bid:
            continue
        label = str(row.get("label") or f"دکمهٔ دلخواه {bid}").strip()
        items.append((f"custom:{bid}", label, bool(str(row.get("sticker_file_id") or "").strip())))
    return items

async def _has_sticker(target: str) -> bool:
    if target == "__start__":
        return bool((await _get(START_KEY, "")).strip())
    if target.startswith("custom:"):
        bid = target.split(":", 1)[1]
        return any(str(r.get("id") or "") == bid and str(r.get("sticker_file_id") or "").strip()
                   for r in await _custom_buttons())
    return bool((await _get(f"hbtn:{target}:sticker", "")).strip())

async def _save_custom_sticker(bid: str, file_id: str) -> bool:
    rows = await _custom_buttons()
    for row in rows:
        if str(row.get("id") or "") == bid:
            row["sticker_file_id"] = file_id
            await _set("home_custom_btns", json.dumps(rows, ensure_ascii=False))
            return True
    return False

async def _delete_custom_sticker(bid: str) -> bool:
    return await _save_custom_sticker(bid, "")

def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pst:menu", style=ButtonStyle.DANGER),
         InlineKeyboardButton(text="❌ بستن", callback_data="pst:close", style=ButtonStyle.DANGER)]
    ])

async def _render_menu(target: CallbackQuery | Message, edit: bool = True) -> None:
    start_status = "✅ دارد" if await _has_sticker("__start__") else "— ندارد"
    rows = [[InlineKeyboardButton(text=f"🚀 استیکر روی استارت  {start_status}", callback_data="pst:start")]]
    for key, label, has in await _home_items():
        shown = label[:45]
        rows.append([InlineKeyboardButton(
            text=f"🎟 {shown}  {'✅' if has else '—'}",
            callback_data=f"pst:button:{key}" )])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات", callback_data="adm:cat:settings", style=ButtonStyle.DANGER),
                 InlineKeyboardButton(text="❌ بستن", callback_data="pst:close", style=ButtonStyle.DANGER)])
    text = (
        "🎟 <b>مدیریت استیکرهای ربات اصلی</b>\n\n"
        "این بخش فقط روی ربات اصلی بات‌ساز اثر می‌گذارد و به ربات‌های مستأجر کاری ندارد.\n"
        "برای استارت، یک استیکر پیش از پیام خوش‌آمد ارسال می‌شود. برای هر دکمه نیز استیکر پیش از محتوای همان دکمه ارسال خواهد شد.\n\n"
        "🚀 استیکر استارت: " + start_status + "\n"
        "از فهرست زیر بخش موردنظر را انتخاب کن:"
    )
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery) and edit:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")

async def _ask(target: CallbackQuery, state: FSMContext, target_key: str, title: str) -> None:
    await state.update_data(platform_sticker_target=target_key)
    await state.set_state(PlatformStickerFSM.wait_sticker)
    await target.message.edit_text(
        f"🎟 <b>{title}</b>\n\nیک استیکر معمولی یا پرمیوم بفرست تا ذخیره شود.",
        reply_markup=_back_kb(), parse_mode="HTML")

@platform_sticker_admin_router.callback_query(F.data == "pst:menu")
async def pst_menu(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    await state.clear()
    await _render_menu(cb)
    await cb.answer()

@platform_sticker_admin_router.callback_query(F.data == "pst:start")
async def pst_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    has = await _has_sticker("__start__")
    rows = [[InlineKeyboardButton(text="➕ افزودن استیکر روی استارت", callback_data="pst:add:start", style=ButtonStyle.SUCCESS)]]
    if has:
        rows.append([InlineKeyboardButton(text="🗑 حذف استیکر استارت", callback_data="pst:del:start", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pst:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text("🚀 <b>استیکر روی استارت</b>\n\nاین استیکر قبل از پیام خوش‌آمدگویی ارسال می‌شود.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()

@platform_sticker_admin_router.callback_query(F.data == "pst:add:start")
async def pst_add_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    await _ask(cb, state, "__start__", "افزودن استیکر روی استارت")
    await cb.answer()

@platform_sticker_admin_router.callback_query(F.data.startswith("pst:button:"))
async def pst_button(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    key = cb.data.split(":", 2)[2]
    label = dict((k, v) for k, v, _ in await _home_items()).get(key, key)
    has = await _has_sticker(key)
    rows = [[InlineKeyboardButton(text="➕ افزودن استیکر", callback_data=f"pst:add:{key}", style=ButtonStyle.SUCCESS)]]
    if has:
        rows.append([InlineKeyboardButton(text="🗑 حذف استیکر", callback_data=f"pst:del:{key}", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="pst:menu", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(f"🎟 <b>استیکر دکمه</b>\n\n{label}\n\nاستیکر پیش از محتوای این دکمه ارسال می‌شود.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()

@platform_sticker_admin_router.callback_query(F.data.startswith("pst:add:"))
async def pst_add_button(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    key = cb.data.split(":", 2)[2]
    label = dict((k, v) for k, v, _ in await _home_items()).get(key, key)
    await _ask(cb, state, key, f"افزودن استیکر برای {label}")
    await cb.answer()

@platform_sticker_admin_router.message(PlatformStickerFSM.wait_sticker, F.sticker)
async def pst_save(msg: Message, state: FSMContext):
    if not await _admin_msg(msg): return
    data = await state.get_data()
    target = str(data.get("platform_sticker_target") or "")
    file_id = msg.sticker.file_id
    if target == "__start__":
        await _set(START_KEY, file_id)
    elif target.startswith("custom:"):
        if not await _save_custom_sticker(target.split(":", 1)[1], file_id):
            await msg.answer("❌ دکمهٔ دلخواه پیدا نشد.")
            await state.clear(); return
    else:
        await _set(f"hbtn:{target}:sticker", file_id)
    await state.clear()
    await msg.answer("✅ استیکر با موفقیت ذخیره شد.")
    await _render_menu(msg, edit=False)

# === PATCH_STICKER_WAIT_ESCAPE_V1 ===
# همان دو مشکلِ نسخهٔ مستأجر (tenant/home_sticker_admin.py): این هندلر همهٔ
# پیام‌ها را می‌بلعید و ادمین در حالتِ انتظارِ استیکر گیر می‌کرد، و پیامِ
# خطا نمی‌گفت چه چیزی دریافت شده.
_ESCAPE_WORDS = {
    "بازگشت", "⬅️ بازگشت", "لغو", "❌ لغو", "انصراف", "بستن", "❌ بستن",
    "منوی اصلی", "🏠 منوی اصلی", "خانه", "/cancel", "/start",
}


def _looks_like_escape(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t in _ESCAPE_WORDS:
        return True
    bare = "".join(ch for ch in t if ch.isalpha() or ch.isspace()).strip()
    return bare in _ESCAPE_WORDS


def _received_kind_fa(msg: Message) -> str:
    if msg.photo:
        return ("یک <b>عکس</b> فرستادی، نه استیکر. عکس با استیکر فرق دارد؛ "
                "استیکر را باید از پنلِ استیکرهایِ تلگرام انتخاب کنی.")
    if msg.animation:
        return "یک <b>گیف</b> فرستادی، نه استیکر."
    if msg.document:
        return ("یک <b>فایل</b> فرستادی. اگر تصویرِ استیکر را به‌صورتِ فایل "
                "می‌فرستی کار نمی‌کند؛ خودِ استیکر را بفرست.")
    if msg.video or msg.video_note:
        return "یک <b>ویدیو</b> فرستادی، نه استیکر."
    if msg.text:
        return "یک <b>متن</b> فرستادی، نه استیکر."
    return "چیزی که فرستادی استیکر نبود."


@platform_sticker_admin_router.message(PlatformStickerFSM.wait_sticker)
async def pst_invalid(msg: Message, state: FSMContext):
    if not await _admin_msg(msg):
        return
    if _looks_like_escape(msg.text or ""):
        await state.clear()
        await msg.answer("لغو شد؛ استیکری ذخیره نشد.")
        return
    await msg.answer(
        f"❌ {_received_kind_fa(msg)}\n\n"
        "یک استیکر بفرست، یا از دکمهٔ «بازگشت» بالا استفاده کن "
        "(یا کلمهٔ <code>لغو</code> را بفرست).",
        parse_mode="HTML")

@platform_sticker_admin_router.callback_query(F.data.startswith("pst:del:"))
async def pst_delete(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    key = cb.data.split(":", 2)[2]
    if key == "start":
        await _set(START_KEY, "")
    elif key.startswith("custom:"):
        await _delete_custom_sticker(key.split(":", 1)[1])
    else:
        await _set(f"hbtn:{key}:sticker", "")
    await cb.answer("✅ حذف شد")
    await _render_menu(cb)

@platform_sticker_admin_router.callback_query(F.data == "pst:close")
async def pst_close(cb: CallbackQuery, state: FSMContext):
    if not await _admin(cb): return
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        await cb.message.edit_text("✅ بسته شد.")
    await cb.answer()
