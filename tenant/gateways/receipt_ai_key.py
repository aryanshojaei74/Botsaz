"""receipt_ai_key.py — توکنِ هوش مصنوعیِ آروانِ خودِ هر ربات (=== PATCH_RECEIPT_AI_OWN_KEY_V1 ===)

هزینهٔ تشخیصِ رسید با هوش مصنوعی را می‌شود به مالکِ هر ربات سپرد: مالک
از پنلِ آروان یک endpoint (آدرسِ API) و کلید می‌گیرد و اینجا ثبت می‌کند؛ از
آن لحظه رسیدهای همان ربات با کلیدِ خودش بررسی می‌شود و هزینه از حسابِ آروانِ
خودش کم می‌شود.

قاعده (پیش‌فرض): رباتی که کلیدِ خودش را ثبت نکرده، تأییدِ خودکارِ رسیدش به
بررسیِ دستیِ ادمین می‌رود و هیچ هزینه‌ای از کلیدِ پلتفرم کم نمی‌شود. فقط با
RECEIPT_AI_REQUIRE_OWN_KEY=0 می‌شود به رفتارِ قدیمیِ «کلیدِ پلتفرم» برگشت.
"""
from __future__ import annotations

import os

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from config import Session
from models import Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

receipt_ai_router = Router()

# پیش‌فرض: الزامی. هیچ رباتِ مستأجری از کلیدِ پلتفرم استفاده نمی‌کند؛ بدونِ کلیدِ
# شخصیِ مالک، رسید به بررسیِ دستیِ ادمین می‌رود. فقط با RECEIPT_AI_REQUIRE_OWN_KEY=0
# (مثلاً برایِ آزمایش) می‌شود به رفتارِ قدیمی برگشت.
REQUIRE_OWN_KEY = os.getenv("RECEIPT_AI_REQUIRE_OWN_KEY", "1").strip() != "0"


class RaiFSM(StatesGroup):
    url = State()
    key = State()


async def _get(bot_id: int, key: str) -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return (r.value or "").strip() if r else ""


async def _set(bot_id: int, key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


async def get_creds(bot_id: int | None) -> dict | None:
    """{"url":..., "key":...} اگر مالکِ ربات کلیدِ خودش را ثبت کرده؛ وگرنه None."""
    if not bot_id:
        return None
    url, key = await _get(bot_id, "receipt_ai_url"), await _get(bot_id, "receipt_ai_key")
    return {"url": url, "key": key} if (url and key) else None


async def needs_key_first(bot_id: int) -> bool:
    return REQUIRE_OWN_KEY and not await get_creds(bot_id)


def _mask(k: str) -> str:
    return (k[:4] + "…" + k[-4:]) if len(k) > 10 else "…"


async def _menu(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cr = await get_creds(bot_id)
    lines = ["🔑 <b>هوش مصنوعیِ تشخیصِ رسید (توکنِ آروانِ خودت)</b>", ""]
    if cr:
        lines += ["وضعیت: ✅ <b>ثبت شده</b>", f"آدرس: <code>{cr['url'][:60]}</code>",
                  f"کلید: <code>{_mask(cr['key'])}</code>", "",
                  "رسیدهای این ربات با کلیدِ خودت بررسی می‌شود و هزینه‌اش از حسابِ آروانِ خودت کم می‌شود."]
    else:
        lines += ["وضعیت: ⛔️ <b>ثبت نشده</b>", "",
                  ("تا کلیدِ خودت را ثبت نکنی، رسیدها خودکار تایید نمی‌شوند و به بررسیِ دستیِ ادمین می‌روند. "
                   "هزینهٔ هوش مصنوعی فقط از حسابِ آروانِ خودتِ تو کم می‌شود، نه از پلتفرم."
                   if REQUIRE_OWN_KEY else
                   "الان رسیدها با کلیدِ پلتفرم بررسی می‌شود؛ اگر کلیدِ خودت را ثبت کنی هزینه با خودت می‌شود.")]
    lines += ["", "<b>روشِ گرفتنِ کلید:</b>",
              "۱) در سایتِ آروان‌کلاود (arvancloud.ir) وارد بخشِ «هوش مصنوعی / AI Services» شو.",
              "۲) یک مدل با <b>پشتیبانی از عکس</b> (مثلاً GPT-4.1 یا Gemini) را فعال کن.",
              "۳) آدرسِ API (مثلاً <code>https://arvancloudai.ir/gateway/models/GPT-4.1/XXXX/v1</code>) و "
              "کلیدِ API را از همان صفحه کپی کن و اینجا بفرست."]
    rows = [[InlineKeyboardButton(text=("✏️ تغییرِ کلید" if cr else "➕ ثبتِ آدرس و کلید"),
                                  callback_data="rai:set", style=ButtonStyle.SUCCESS)]]
    if cr:
        rows.append([InlineKeyboardButton(text="🗑 حذفِ کلید", callback_data="rai:del", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:card", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _owner(cb_or_msg, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    uid = cb_or_msg.from_user.id
    if not row or not await is_owner_or_admin(row.id, uid, row.owner_id):
        return None
    return row


@receipt_ai_router.callback_query(F.data == "rai:menu")
async def rai_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return await cb.answer()
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@receipt_ai_router.callback_query(F.data == "rai:set")
async def rai_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return await cb.answer()
    await state.clear()
    await state.set_state(RaiFSM.url)
    await cb.message.answer("۱) <b>آدرسِ API</b> آروان را بفرست (باید با https:// شروع شود):\n(لغو: /cancel)")
    await cb.answer()


@receipt_ai_router.message(RaiFSM.url, F.text)
async def rai_url(msg: Message, bot: Bot, state: FSMContext):
    if not await _owner(msg, bot):
        return
    t = (msg.text or "").strip()
    if t == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if not t.startswith("https://") or len(t) < 20 or " " in t:
        return await msg.answer("⚠️ آدرس معتبر نیست؛ باید با https:// شروع شود. دوباره بفرست:")
    await state.update_data(rai_url=t.rstrip("/"))
    await state.set_state(RaiFSM.key)
    await msg.answer("۲) <b>کلیدِ API</b> را بفرست (پیامت بعد از ذخیره پاک می‌شود):")


@receipt_ai_router.message(RaiFSM.key, F.text)
async def rai_key(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner(msg, bot)
    if not row:
        return
    key = (msg.text or "").strip()
    if key == "/cancel":
        await state.clear()
        return await msg.answer("لغو شد.")
    if len(key) < 10 or " " in key:
        return await msg.answer("⚠️ کلید معتبر به نظر نمی‌رسد. دوباره بفرست:")
    data = await state.get_data()
    url = data.get("rai_url")
    await state.clear()
    try:
        await msg.delete()   # کلید در چت نماند
    except Exception:
        pass
    wait = await msg.answer("⏳ در حالِ آزمایشِ اتصال…")
    from tenant.gateways.auto_receipt import _arvan_autonomous_call
    text, err = await _arvan_autonomous_call("فقط کلمهٔ OK را جواب بده.", None,
                                             creds={"url": url, "key": key})
    if not text:
        return await wait.edit_text(
            "❌ اتصال با این آدرس/کلید جواب نداد، پس ذخیره نشد.\n"
            f"دلیل: {err or 'نامشخص'}\n\nآدرس و کلید را از پنلِ آروان دوباره بررسی کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔁 دوباره", callback_data="rai:set")]]))
    await _set(row.id, "receipt_ai_url", url)
    await _set(row.id, "receipt_ai_key", key)
    await wait.edit_text(
        "✅ <b>ذخیره شد و اتصال برقرار است.</b>\n\nاز این پس رسیدهای این ربات با کلیدِ خودت بررسی می‌شود. "
        "حالا می‌توانی «🤖 تایید خودکار رسید» را روشن کنی.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🤖 رفتن به تایید خودکار رسید", callback_data="set:auto_approve_toggle")]]))


@receipt_ai_router.callback_query(F.data == "rai:del")
async def rai_del(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return await cb.answer()
    await _set(row.id, "receipt_ai_url", "")
    await _set(row.id, "receipt_ai_key", "")
    await cb.answer("🗑 حذف شد", show_alert=True)
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
