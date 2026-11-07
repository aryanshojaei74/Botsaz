"""kyc_review.py — PATCH_KYC_REVIEW_GROUP_V1 (فدایی)

گروهِ بررسیِ احرازِ هویت: هر درخواستِ احراز (مدرک/کارت + مشخصاتِ کاربر) با دکمهٔ «✅ تأیید / ❌ رد» در گروهی که مالک
انتخاب کرده می‌آید. تأیید ⇒ همان پیام ادیت می‌شود («احرازِ هویتِ کاربر با سطح X تأیید شد» + مشخصات). رد ⇒ ربات دلیل را
می‌پرسد، بعد همان پیام ادیت می‌شود («درخواستِ احرازِ سطح X رد شد» + مشخصات + دلیل) و دلیل برایِ کاربر هم می‌رود.

کلیدِ تنظیمات: kyc_review_chat (آیدیِ عددیِ گروه/کانال). خالی ⇒ رفتارِ قبلی (پیوی/اعلانِ ادمین‌ها).
"""
from __future__ import annotations

import html as _html

from aiogram import Bot

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

KEY = "kyc_review_chat"


async def review_chat(bot_id: int):
    from tenant import get_setting
    v = ((await get_setting(bot_id, KEY, "")) or "").strip()
    if not v:
        return None
    return int(v) if v.lstrip("-").isdigit() else v


def _mask(card: str | None) -> str:
    c = "".join(ch for ch in str(card or "") if ch.isdigit())
    return (c[:4] + " •••• •••• " + c[-4:]) if len(c) >= 12 else (c or "—")


def user_block(u) -> str:
    uname = f"@{u.username}" if getattr(u, "username", None) else "—"
    return (f"👤 {_html.escape(getattr(u, 'full_name', None) or '—')}\n"
            f"🆔 <code>{getattr(u, 'telegram_id', '—')}</code>\n"
            f"🔗 {_html.escape(uname)}")


def kind_title(level: int, card_change: bool) -> str:
    return "تغییرِ کارت" if card_change else f"احرازِ هویتِ سطح {level}"


def request_caption(u, req) -> str:
    cc = getattr(req, "kind", None) == "card_change"
    cap = (f"🛡 <b>درخواستِ {kind_title(int(req.level or 1), cc)}</b>  (#{req.id})\n\n{user_block(u)}")
    if int(req.level or 1) == 1 and getattr(req, "card", None):
        cap += f"\n💳 کارت: <code>{_mask(req.card)}</code>"
    return cap


def result_text(u, level: int, card_change: bool, approved: bool, reason: str = "", by: str = "") -> str:
    if approved:
        head = (f"✅ <b>تغییرِ کارتِ کاربر تأیید شد</b>" if card_change
                else f"✅ <b>احرازِ هویتِ کاربر با سطح {level} تأیید شد</b>")
    else:
        head = (f"❌ <b>درخواستِ تغییرِ کارت رد شد</b>" if card_change
                else f"❌ <b>درخواستِ احرازِ هویتِ سطح {level} رد شد</b>")
    out = f"{head}\n\n{user_block(u)}"
    if not approved:
        out += f"\n\n📝 <b>دلیل:</b> {_html.escape(reason)}" if reason else "\n\n📝 بدونِ دلیل"
    if by:
        out += f"\n\n👮 بررسی‌کننده: {_html.escape(by)}"
    return out


async def edit_review_message(bot: Bot, chat_id, message_id, text: str) -> bool:
    """متن/کپشنِ پیامِ درخواست را عوض و دکمه‌ها را برمی‌دارد (پیام می‌تواند عکس/ویدیو یا متن باشد)."""
    for fn in ("edit_message_caption", "edit_message_text"):
        try:
            if fn == "edit_message_caption":
                await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text,
                                               parse_mode="HTML", reply_markup=None)
            else:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                            parse_mode="HTML", reply_markup=None)
            return True
        except Exception as e:      # noqa: BLE001
            low = str(e).lower()
            if "not modified" in low:
                return True
            logger.debug("kyc_review: %s ناموفق: %s", fn, e)
    return False


async def send_to_review_group(bot: Bot, row, u, req) -> bool:
    """درخواست را به گروهِ بررسی می‌فرستد. True = فرستاده شد (پس اعلانِ پیوی لازم نیست)."""
    chat = await review_chat(row.id)
    if chat is None:
        return False
    from aiogram.enums import ButtonStyle
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید", callback_data=f"kyc:ok:{req.id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ رد", callback_data=f"kyc:no:{req.id}", style=ButtonStyle.DANGER)]])
    cap = request_caption(u, req)
    try:
        ft = req.file_type
        if ft == "photo" and req.file_id:
            await bot.send_photo(chat, req.file_id, caption=cap, reply_markup=kb, parse_mode="HTML")
        elif ft == "video" and req.file_id:
            await bot.send_video(chat, req.file_id, caption=cap, reply_markup=kb, parse_mode="HTML")
        elif ft == "video_note" and req.file_id:
            await bot.send_video_note(chat, req.file_id)
            await bot.send_message(chat, cap, reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(chat, cap, reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as e:      # noqa: BLE001
        logger.warning("kyc_review: ارسال به گروهِ بررسی ناموفق بود (bot=%s chat=%s): %s", row.id, chat, e)
        return False
