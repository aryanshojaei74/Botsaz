# SELFSaz_INBOT_LOGIN_UI_V1
# SELFSaz_ONE_DAY_TRIAL_UI_V25
"""SelfSaz UI for the main BotSaz platform bot.

The Telegram UI lives in the platform bot; account workers remain in the
independent /opt/selfsaz service. No tenant router or tenant database model is used.
"""
from __future__ import annotations
from html import escape as _esc_html

import os
import uuid
from pathlib import Path
from html import escape as _html_escape
import aiohttp
from aiogram import F, Router, Bot
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo, ReplyKeyboardRemove, InlineQuery, InlineQueryResultArticle, InlineQueryResultPhoto, InlineQueryResultCachedPhoto, InputTextMessageContent
from config import Session
from models import PlatformTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

router = Router()

_SELFSAZ_HOME_AUX: dict[tuple[int, int], int] = {}

@router.message(F.text.regexp(r"^/start(?:@\w+)?\s+selfsaz_connected$") )
async def selfsaz_deeplink_return(msg: Message):
    await msg.answer("ℹ️ صفحهٔ امن بسته شد. اتصال حساب جدید از داخل Web App تأیید نمی‌شود؛ وضعیت واقعی را از پنل سلف‌ساز بررسی کنید.")
    await show_selfsaz_panel(msg, msg.from_user.id)


@router.message(F.web_app_data)
async def selfsaz_webapp_return(msg: Message):
    data = (msg.web_app_data.data or '').strip() if msg.web_app_data else ''
    if data not in {'selfsaz_open', 'selfsaz_status'}:
        return
    await msg.answer('ℹ️ Web App بسته شد؛ وضعیت واقعی حساب از سرویس سلف‌ساز خوانده می‌شود.')
    await show_selfsaz_panel(msg, msg.from_user.id)


@router.message(F.text == 'پنل')
@router.message(F.text == '/panel')
async def selfsaz_panel_command(msg: Message):
    await show_selfsaz_panel(msg, msg.from_user.id)

SERVICE_URL = os.getenv("SELFSaz_SERVICE_URL", "http://127.0.0.1:8791").rstrip("/")
SERVICE_KEY = os.getenv("SELFSaz_API_KEY", "")
SELFSaz_LAST_ERROR = ""

# SELFSaz_PLATFORM_V2_FA_WALLET
# SELFSaz_REFERENCE_SLIDER_V2
_SELFSAZ_SLIDER_IMAGE = os.getenv('SELFSAZ_PANEL_IMAGE_URL', '').strip()
async def _selfsaz_branding() -> dict:
    defaults = {
        "name": "سلف‌ساز",
        "title": "پنل سلف‌ساز",
        "caption": "پنل مدیریت حساب و قابلیت‌های سلف‌ساز",
        "image_file_id": "",
    }
    try:
        from platform_bot import get_pset
        defaults["name"] = (await get_pset("selfsaz:brand_name", defaults["name"])).strip() or defaults["name"]
        defaults["title"] = (await get_pset("selfsaz:brand_title", defaults["title"])).strip() or defaults["title"]
        defaults["caption"] = (await get_pset("selfsaz:brand_caption", defaults["caption"])).strip() or defaults["caption"]
        defaults["image_file_id"] = (await get_pset("selfsaz:panel_image_file_id", "")).strip()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return defaults

_SELFSAZ_SLIDER_PAGES = {
    0: ('<b>PANEL SELF</b>', [('🤖 تنظیمات سلف', 'page:1'), ('📣 تنظیمات تپچی', 'page:2'), ('👤 حساب کاربری', 'account'), ('✖️ بستن پنل', 'close')]),
    1: ('⚙️ <b>تنظیمات سلف</b>', [('✍️ حالت متن', 'page:6'), ('⏰ ساعت', 'clock'), ('🛡 نگهبان چت', 'page:7'), ('⚡ اکشن', 'page:8'), ('🎨 لوگو', 'logo'), ('📝 اسم', 'name'), ('🧾 بیو', 'bio')]),
    2: ('📣 <b>تنظیمات تپچی</b>', [('🌐 ترجمه', 'translate'), ('💬 پاسخ خودکار', 'auto_reply'), ('🤝 دوست و دشمن', 'enemy'), ('😍 ریکت', 'reaction'), ('🎬 انیمیشن', 'animation'), ('💎 ایموجی پریمیوم', 'premium'), ('🎥 ساخت ویدیو گرد', 'video')]),
    3: ('🛡 <b>مدیریت گروه</b>', [('⛔ بن کاربر', 'group:بن'), ('👢 کیک کاربر', 'group:کیک'), ('🔇 سکوت کاربر', 'group:سکوت'), ('🔊 حذف سکوت', 'group:حذف سکوت'), ('📌 پین پیام', 'group:پین'), ('📍 آنپین پیام', 'group:آنپین')]),
    4: ('🔧 <b>ابزارهای حساب</b>', [('🔇 سکوت', 'mute'), ('ℹ️ اطلاعات', 'info'), ('🗣 تبدیل متن به ویس', 'tts'), ('🧮 ماشین حساب', 'calculator'), ('💱 قیمت ارز', 'currency'), ('💎 موجودی', 'balance')]),
    5: ('🎲 <b>ابزارهای بیشتر</b>', [('🎲 تقلب', 'dice'), ('🔍 سرچ', 'search'), ('📸 اسکرین', 'screen'), ('💬 کامنت اول', 'first_comment'), ('📨 اسپم', 'spam'), ('🚫 فیلتر کلمات', 'filter')]),
    6: ('✍️ <b>حالت متن</b>', [('بولد', 'format:بولد'), ('ایتالیک', 'format:ایتالیک'), ('زیرخط', 'format:زیر خط'), ('خط‌خورده', 'format:خط‌ خورده'), ('اسپویلر', 'format:اسپویلر'), ('کد', 'format:کد'), ('پیش‌فرمت', 'format:پیش‌ فرمت'), ('نقل‌قول', 'format:نقل ‌قول')]),
    7: ('🛡 <b>قفل‌های پیام</b>', [('همه', 'lock:همه'), ('مدیا', 'lock:مدیا'), ('استیکر', 'lock:استیکر'), ('فوروارد', 'lock:فوروارد'), ('ویس', 'lock:ویس'), ('پیام', 'lock:پیام'), ('فایل', 'lock:فایل')]),
    8: ('⚡ <b>اکشن‌ها</b>', [('تایپ', 'action:typing'), ('آپلود عکس', 'action:upload_photo'), ('آپلود ویدیو', 'action:upload_video'), ('ضبط صدا', 'action:record_audio'), ('پخش', 'action:playing'), ('موقعیت', 'action:find_location')]),
}

def _selfsaz_slider_kb(page: int) -> InlineKeyboardMarkup:
    items = _SELFSAZ_SLIDER_PAGES.get(page, _SELFSAZ_SLIDER_PAGES[0])[1]
    rows = []
    if page == 0:
        rows.append([
            InlineKeyboardButton(text='🤖 تنظیمات سلف', callback_data='pss:slider:page:1', style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text='📣 تنظیمات تپچی', callback_data='pss:slider:page:2', style=ButtonStyle.SUCCESS),
        ])
        rows.append([InlineKeyboardButton(text='👤 حساب کاربری', callback_data='pss:slider:account', style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text='✖️ بستن پنل', callback_data='pss:slider:close', style=ButtonStyle.DANGER)])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    for index in range(0, len(items), 2):
        pair = items[index:index + 2]
        rows.append([InlineKeyboardButton(text=label, callback_data=f'pss:slider:cmd:{action}', style=ButtonStyle.PRIMARY) for label, action in pair])
    rows.append([InlineKeyboardButton(text='⬅️', callback_data=f'pss:slider:page:{max(1, page - 1)}', style=ButtonStyle.PRIMARY), InlineKeyboardButton(text=f'({page})', callback_data=f'pss:slider:page:{page}', style=ButtonStyle.PRIMARY), InlineKeyboardButton(text='➡️', callback_data=f'pss:slider:page:{1 if page == 8 else page + 1}', style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text='🔴 بستن پنل', callback_data='pss:slider:close', style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _selfsaz_tabchi_kb(owner_id: int) -> InlineKeyboardMarkup:
    """Compact, two-column Tabchi settings screen with live toggle states."""
    try:
        cfg = await _pss_control_get(owner_id)
    except Exception:
        cfg = {}
    translation_on = bool(cfg.get('translation_enabled', False))
    premium_on = bool(cfg.get('premium_emoji_enabled', False))
    rows = [
        [
            InlineKeyboardButton(text=('🟢 ترجمه' if translation_on else '🌐 ترجمه'), callback_data='pss:slider:cmd:translate', style=(ButtonStyle.SUCCESS if translation_on else ButtonStyle.PRIMARY)),
            InlineKeyboardButton(text=('🟢 ایموجی پریمیوم' if premium_on else '💎 ایموجی پریمیوم'), callback_data='pss:slider:cmd:premium', style=(ButtonStyle.SUCCESS if premium_on else ButtonStyle.PRIMARY)),
        ],
        [
            InlineKeyboardButton(text='💬 پاسخ خودکار', callback_data='pss:slider:cmd:auto_reply', style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text='🤝 دوست و دشمن', callback_data='pss:slider:cmd:enemy', style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text='😍 ریکت', callback_data='pss:slider:cmd:reaction', style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text='🎬 انیمیشن', callback_data='pss:slider:cmd:animation', style=ButtonStyle.PRIMARY),
        ],
        [InlineKeyboardButton(text='🎥 ساخت ویدیو گرد', callback_data='pss:slider:cmd:video', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='🏠 پنل اصلی', callback_data='pss:slider:page:0', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='✖️ بستن پنل', callback_data='pss:slider:close', style=ButtonStyle.DANGER)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _selfsaz_slider_text(page: int, brand: dict | None = None) -> str:
    brand = brand or {}
    page_text = _SELFSAZ_SLIDER_PAGES.get(page, _SELFSAZ_SLIDER_PAGES[0])[0]
    if page == 0:
        return '<b>PANEL SELF</b>'
    return page_text + '\n\nبرای بازکردن دوباره در هرجا بنویسید: <code>پنل</code>'

@router.inline_query(F.query == 'panel')
@router.inline_query(F.query == 'پنل')
async def selfsaz_inline_panel(query: InlineQuery):
    account = await selfsaz_api('GET', f'/v1/accounts/{query.from_user.id}')
    if not account or not account.get('configured'):
        await query.answer([], cache_time=0, is_personal=True)
        return
    brand = await _selfsaz_branding()
    caption = _selfsaz_slider_text(0, brand)
    markup = _selfsaz_slider_kb(0)
    results = []
    try:
        from platform_bot import get_pset
        cached_file_id = brand.get('image_file_id', '')
    except Exception:
        cached_file_id = ''
    if cached_file_id:
        results.append(InlineQueryResultCachedPhoto(id=f'selfsaz-panel-cached-{query.from_user.id}', photo_file_id=cached_file_id, caption=caption, reply_markup=markup))
    elif _SELFSAZ_SLIDER_IMAGE:
        results.append(InlineQueryResultPhoto(id=f'selfsaz-panel-photo-{query.from_user.id}', photo_url=_SELFSAZ_SLIDER_IMAGE, thumbnail_url=_SELFSAZ_SLIDER_IMAGE, caption=caption, reply_markup=markup))
    else:
        results.append(InlineQueryResultArticle(id=f'selfsaz-panel-{query.from_user.id}', title='پنل سلف‌ساز', description='بازکردن پنل حرفه‌ای سلف‌ساز', input_message_content=InputTextMessageContent(message_text=caption), reply_markup=markup))
    await query.answer(results, cache_time=0, is_personal=True)

async def _edit_slider_message(cb: CallbackQuery, bot: Bot, text: str, markup: InlineKeyboardMarkup):
    if cb.inline_message_id:
        try:
            await bot.edit_message_caption(inline_message_id=cb.inline_message_id, caption=text, reply_markup=markup)
            return True
        except Exception:
            try:
                await bot.edit_message_text(inline_message_id=cb.inline_message_id, text=text, reply_markup=markup)
                return True
            except Exception:
                return False
    if cb.message:
        try:
            await cb.message.edit_caption(caption=text, reply_markup=markup)
            return True
        except Exception:
            try:
                await cb.message.edit_text(text, reply_markup=markup)
                return True
            except Exception:
                return False
    return False


async def _close_slider_message(cb: CallbackQuery, bot: Bot):
    if cb.inline_message_id:
        await _edit_slider_message(cb, bot, '🔴 <b>پنل بسته شد</b>', InlineKeyboardMarkup(inline_keyboard=[]))
        return
    if cb.message:
        key = (cb.message.chat.id, cb.message.message_id)
        aux_id = _SELFSAZ_HOME_AUX.pop(key, None)
        if aux_id:
            try:
                await bot.delete_message(cb.message.chat.id, aux_id)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        try:
            await cb.message.delete()
        except Exception:
            try:
                await cb.message.edit_text('🔴 پنل بسته شد', reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _context_prompt(cb: CallbackQuery, bot: Bot, text: str, reply_markup=None):
    if cb.message:
        return await cb.message.answer(text, reply_markup=reply_markup)
    return await bot.send_message(cb.from_user.id, text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith('pss:slider:'))
async def selfsaz_inline_slider_callback(cb: CallbackQuery, bot: Bot, state: FSMContext):
    parts = (cb.data or '').split(':')
    target = ':'.join(parts[2:])
    if target.startswith('page:'):
        try:
            page = max(0, min(8, int(target.split(':', 1)[1])))
        except ValueError:
            page = 0
        markup = await _selfsaz_tabchi_kb(cb.from_user.id) if page == 2 else _selfsaz_slider_kb(page)
        await _edit_slider_message(cb, bot, _selfsaz_slider_text(page, await _selfsaz_branding()), markup)
        await cb.answer()
        return
    if target == 'close':
        await _close_slider_message(cb, bot)
        await cb.answer('پنل بسته شد')
        return
    if target == 'account':
        account = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}')
        if not account or not account.get('configured'):
            account_text = '👤 <b>حساب کاربری</b>\n\nهنوز حسابی متصل نشده است.'
        else:
            account_text = (f"👤 <b>حساب کاربری</b>\n\n"
                            f"شناسه: <code>{cb.from_user.id}</code>\n"
                            f"وضعیت: {_selfsaz_status_fa(account.get('status'))}\n"
                            f"اشتراک: {'فعال' if _selfsaz_subscription_active(account) else 'غیرفعال'}\n"
                            f"انقضا: {_selfsaz_jalali(account.get('expires_at')) if account.get('expires_at') else 'بدون اشتراک'}")
        account_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='⬅️ بازگشت به پنل', callback_data='pss:slider:page:0', style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text='🔴 بستن پنل', callback_data='pss:slider:close', style=ButtonStyle.DANGER)],
        ])
        await _edit_slider_message(cb, bot, account_text, account_markup)
        await cb.answer()
        return
    if target.startswith('cmd:'):
        action = target.split(':', 1)[1]
        if action == 'logo':
            await state.set_state(SelfSazContextFSM.profile_photo)
            await cb.answer()
            await _context_prompt(cb, bot, '🖼 عکس لوگو را همین‌جا ارسال کنید.\n\nبرای لغو: «🔴 بازگشت»')
            return
        if action == 'name':
            await state.set_state(SelfSazContextFSM.profile_name)
            await cb.answer()
            await _context_prompt(cb, bot, '📝 نام جدید پروفایل را بفرستید.\nحداکثر ۶۴ نویسه\n\nبرای لغو: «🔴 بازگشت»')
            return
        if action == 'bio':
            await state.set_state(SelfSazContextFSM.profile_bio)
            await cb.answer()
            await _context_prompt(cb, bot, '🧾 بیوی جدید پروفایل را بفرستید.\nحداکثر ۷۰ نویسه\n\nبرای پاک‌کردن بیو، فقط «-» بفرستید. برای لغو: «🔴 بازگشت»')
            return
        if action == 'reaction':
            await state.set_state(SelfSazContextFSM.reaction_emoji)
            await cb.answer()
            await _context_prompt(cb, bot, '😍 ایموجی ریکت را بفرستید؛ نمونه: 😊')
            return
        if action == 'video':
            await state.set_state(SelfSazContextFSM.video_note)
            await cb.answer()
            await _context_prompt(cb, bot, '🎥 یک ویدیو ارسال کنید تا به ویدیوی گرد تبدیل و در پیام‌های ذخیره‌شده ارسال شود.\n\nبرای لغو: «🔴 بازگشت»')
            return
        if action == 'content':
            await state.set_state(SelfSazContextFSM.content_link)
            await cb.answer()
            await _context_prompt(cb, bot, '📄 لینک پست تلگرام را ارسال کنید تا در پیام‌های ذخیره‌شده کپی شود.\n\nبرای لغو: «🔴 بازگشت»')
            return
        if action == 'translate':
            cfg = await _pss_control_get(cb.from_user.id)
            enabled = not bool(cfg.get('translation_enabled', False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'translation_enabled': enabled, 'translation_lang': 'fa'})
            if updated:
                await _edit_slider_message(cb, bot, f'🌐 <b>ترجمه خودکار</b>\n\nوضعیت: <b>{"روشن" if enabled else "خاموش"}</b>\nزبان مقصد: فارسی', await _selfsaz_tabchi_kb(cb.from_user.id))
                await cb.answer('ترجمه خودکار روشن شد' if enabled else 'ترجمه خودکار خاموش شد')
            else:
                await cb.answer(f'خطا در ذخیره ترجمه: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        if action == 'animation':
            await _edit_slider_message(cb, bot, '🎬 <b>انیمیشن و اکشن‌های رسانه</b>\n\nبرای فعال‌سازی نمایش وضعیت ارسال، از بخش «اکشن» یکی از گزینه‌های آپلود یا ضبط را روشن کنید.', _selfsaz_slider_kb(8))
            await cb.answer()
            return
        if action == 'premium':
            cfg = await _pss_control_get(cb.from_user.id)
            enabled = not bool(cfg.get('premium_emoji_enabled', False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'premium_emoji_enabled': enabled})
            if updated:
                await _edit_slider_message(cb, bot, f'💎 <b>ایموجی پریمیوم</b>\n\nوضعیت: <b>{"روشن" if enabled else "خاموش"}</b>\n\nبرای یادگیری ایموجی‌های انتخابی، آن‌ها را یک‌بار در پیام‌های ذخیره‌شده ارسال کنید؛ worker شناسهٔ واقعی را ذخیره و در پاسخ‌های خودکار استفاده می‌کند.', await _selfsaz_tabchi_kb(cb.from_user.id))
                await cb.answer('پشتیبانی ایموجی پریمیوم روشن شد' if enabled else 'پشتیبانی ایموجی پریمیوم خاموش شد')
            else:
                await cb.answer(f'خطا در ذخیره ایموجی پریمیوم: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        if action == 'auto_reply':
            await cb.answer()
            await _context_prompt(cb, bot,
                '💬 <b>پاسخ خودکار</b>\n\n'
                'برای افزودن پاسخ، پیام را با قالب زیر بفرستید:\n'
                '<code>کلمه کلیدی | متن پاسخ</code>\n\n'
                'نمونه: <code>سلام | سلام، در حال حاضر پاسخ‌گو نیستم.</code>\n'
                'برای فعال‌سازی پاسخ عمومی، عبارت <code>عمومی: متن پاسخ</code> را بفرستید.'
            )
            await state.set_state(SelfSazContextFSM.auto_reply)
            return
        if action == 'enemy':
            await state.set_state(SelfSazContextFSM.enemy_operation)
            await cb.answer()
            await _context_prompt(cb, bot, '🤝 عملیات دشمن را بفرستید: <code>افزودن</code> یا <code>حذف</code>؛ برای فهرست، <code>لیست</code> را بفرستید.')
            return
        if action.startswith('group:'):
            operation = action.split(':', 1)[1]
            await state.update_data(group_operation=operation)
            await state.set_state(SelfSazContextFSM.group_chat)
            await cb.answer()
            await _context_prompt(cb, bot, '🛡 شناسهٔ عددی گروه را بفرستید. نمونه: <code>-1001234567890</code>')
            return
        if action == 'clock':
            current = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}/control') or {}
            control = current.get('control') or {}
            enabled = not bool(control.get('time_enabled', False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'time_enabled': enabled})
            if updated:
                await cb.answer('ساعت روشن شد' if enabled else 'ساعت خاموش شد', show_alert=True)
            else:
                await cb.answer(f'خطا در تنظیم ساعت: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        if action.startswith('format:'):
            key = action.split(':', 1)[1]
            current = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}/control') or {}
            control = current.get('control') or {}
            formats = control.get('formats') or {}
            enabled = not bool(formats.get(key, False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'formats': {key: enabled}})
            await cb.answer(('✅ ' if enabled else '❌ ') + key + (' روشن شد' if enabled else ' خاموش شد'), show_alert=True) if updated else await cb.answer(f'خطا در حالت متن: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        if action == 'format':
            await cb.answer('برای هر فرمت، صفحهٔ حالت متن را باز کنید.', show_alert=True)
            return
        if action.startswith('lock:'):
            key = action.split(':', 1)[1]
            current = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}/control') or {}
            control = current.get('control') or {}
            locks = control.get('locks') or {}
            enabled = not bool(locks.get(key, False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/advanced-control', {'locks': {key: enabled}})
            await cb.answer(('✅ قفل ' if enabled else '❌ قفل ') + key + (' فعال شد' if enabled else ' غیرفعال شد'), show_alert=True) if updated else await cb.answer(f'خطا در قفل‌ها: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        if action.startswith('action:'):
            key = action.split(':', 1)[1]
            current = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}/control') or {}
            control = current.get('control') or {}
            actions = control.get('actions') or {}
            enabled = not bool(actions.get(key, False))
            updated = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/advanced-control', {'actions': {key: enabled}})
            await cb.answer(('✅ اکشن ' if enabled else '❌ اکشن ') + key + (' فعال شد' if enabled else ' غیرفعال شد'), show_alert=True) if updated else await cb.answer(f'خطا در اکشن: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}', show_alert=True)
            return
        await cb.answer('این قابلیت به ورودی متنی، ریپلای یا گروه هدف نیاز دارد و از همین دکمه راهنمای اجرای آن نمایش داده می‌شود.', show_alert=True)
        return
    await cb.answer()
def _selfsaz_status_fa(value):
    return {
        "configured": "متصل، آمادهٔ خرید",
        "queued": "در صف اجرا",
        "running": "در حال اجرا",
        "stopped": "متوقف",
        "error": "خطا",
        "disabled": "غیرفعال",
    }.get(str(value or "").lower(), "نامشخص")

def _selfsaz_subscription_active(account: dict) -> bool:
    from datetime import datetime, timezone
    raw = account.get("expires_at")
    if not raw:
        return bool(account.get("subscription_active"))
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value > datetime.now(timezone.utc)
    except Exception:
        return False

def _selfsaz_jalali(raw) -> str:
    from datetime import datetime, timezone
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        value = value.astimezone(ZoneInfo("Asia/Tehran"))
        gy, gm, gd = value.year, value.month, value.day
        gdm = [0,31,59,90,120,151,181,212,243,273,304,334]
        gy2 = gy + 1 if gm > 2 else gy
        days = 355666 + 365*gy + (gy2+3)//4 - (gy2+99)//100 + (gy2+399)//400 + gd + gdm[gm-1]
        jy = -1595 + 33*(days//12053); days %= 12053
        jy += 4*(days//1461); days %= 1461
        if days > 365:
            jy += (days-1)//365; days = (days-1)%365
        jm = 1 + days//31 if days < 186 else 7 + (days-186)//30
        jd = 1 + (days % 31 if days < 186 else (days-186)%30)
        return f"{jy:04d}/{jm:02d}/{jd:02d} - {value.hour:02d}:{value.minute:02d}"
    except Exception:
        return "اشتراک فعال نیست"


class SelfSazConnectFSM(StatesGroup):
    phone = State()
    code = State()
    password = State()


class SelfSazContextFSM(StatesGroup):
    auto_reply = State()
    general_reply = State()
    group_chat = State()
    group_target = State()
    enemy_operation = State()
    enemy_target = State()
    profile_name = State()
    profile_bio = State()
    profile_photo = State()
    reaction_emoji = State()
    reaction_target = State()
    content_link = State()
    video_note = State()


async def _queue_selfsaz_command(owner_id: int, command: str) -> bool:
    return bool(await selfsaz_api('POST', f'/v1/accounts/{owner_id}/command', {'command': command}))


@router.message(SelfSazContextFSM.enemy_operation, F.text)
async def selfsaz_context_enemy_operation(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if value == 'لیست':
        queued = await _queue_selfsaz_command(msg.from_user.id, 'سلف دشمن لیست')
        await state.clear(); await msg.answer('✅ درخواست فهرست در صف اجرا قرار گرفت.' if queued else '❌ سرویس در دسترس نیست.'); return
    if value not in {'افزودن', 'حذف'}:
        await msg.answer('⚠️ فقط یکی از گزینه‌های «افزودن»، «حذف» یا «لیست» را بفرستید.'); return
    await state.update_data(enemy_operation=value)
    await state.set_state(SelfSazContextFSM.enemy_target)
    await msg.answer('👤 username یا شناسهٔ عددی کاربر را بفرستید. نمونه: <code>@username</code>')


@router.message(SelfSazContextFSM.enemy_target, F.text)
async def selfsaz_context_enemy_target(msg: Message, state: FSMContext):
    value = _pss_normalize_digits((msg.text or '').strip())
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    data = await state.get_data()
    operation = data.get('enemy_operation')
    if operation not in {'افزودن', 'حذف'} or not value:
        await state.clear(); await msg.answer('❌ اطلاعات عملیات ناقص است.'); return
    queued = await _queue_selfsaz_command(msg.from_user.id, f'سلف دشمن {operation} {value}')
    await state.clear()
    await msg.answer('✅ عملیات دشمن در صف اجرا قرار گرفت.' if queued else f'❌ ارسال به worker ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.group_chat, F.text)
async def selfsaz_context_group_chat(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    try:
        chat_id = int(_pss_normalize_digits(value))
    except ValueError:
        await msg.answer('⚠️ شناسهٔ گروه باید عددی باشد؛ نمونه: <code>-1001234567890</code>'); return
    await state.update_data(group_chat_id=chat_id)
    data = await state.get_data()
    await state.set_state(SelfSazContextFSM.group_target)
    if data.get('group_operation') in {'پین', 'آنپین'}:
        await msg.answer('🧾 شناسهٔ عددی پیام هدف را بفرستید؛ پیام باید در همان گروه باشد.')
    else:
        await msg.answer('👤 username کاربر را با @ بفرستید یا شناسهٔ عددی کاربر را ارسال کنید.')


@router.message(SelfSazContextFSM.group_target, F.text)
async def selfsaz_context_group_target(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    data = await state.get_data()
    operation = data.get('group_operation', '')
    chat_id = data.get('group_chat_id')
    target = _pss_normalize_digits(value)
    if operation in {'پین', 'آنپین'} and not target.isdigit():
        await msg.answer('⚠️ برای پین و آنپین، شناسهٔ پیام باید عددی باشد.'); return
    if not target or not operation or chat_id is None:
        await state.clear(); await msg.answer('❌ اطلاعات عملیات ناقص است. دوباره از پنل شروع کنید.'); return
    if operation not in {'بن', 'کیک', 'سکوت', 'حذف سکوت', 'پین', 'آنپین'}:
        await state.clear(); await msg.answer('❌ عملیات نامعتبر است.'); return
    queued = await _queue_selfsaz_command(msg.from_user.id, f'سلف عملیات {operation} {chat_id} {target}')
    await state.clear()
    await msg.answer('✅ عملیات در صف اجرا قرار گرفت.' if queued else f'❌ ارسال به worker ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.auto_reply, F.text)
async def selfsaz_context_auto_reply(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear()
        await msg.answer('✅ لغو شد.')
        return
    if '|' not in value and not value.startswith('عمومی:'):
        await msg.answer('⚠️ قالب صحیح: <code>کلمه کلیدی | متن پاسخ</code> یا <code>عمومی: متن پاسخ</code>')
        return
    if value.startswith('عمومی:'):
        reply = value.split(':', 1)[1].strip()
        command = f'پاسخ عمومی تنظیم {reply}'
    else:
        trigger, reply = [part.strip() for part in value.split('|', 1)]
        if not trigger or not reply:
            await msg.answer('⚠️ کلمه کلیدی و متن پاسخ نباید خالی باشند.')
            return
        command = f'پاسخ افزودن {trigger}|{reply}'
    queued = await _queue_selfsaz_command(msg.from_user.id, command)
    await state.clear()
    await msg.answer('✅ پاسخ در صف اجرا قرار گرفت.' if queued else f'❌ ارسال به worker ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


async def _profile_upload_path(owner_id: int, suffix: str) -> Path:
    root = Path('/opt/selfsaz/data/profile_uploads')
    root.mkdir(parents=True, exist_ok=True)
    return root / f'{int(owner_id)}-{uuid.uuid4().hex}{suffix}'


@router.message(SelfSazContextFSM.profile_name, F.text)
async def selfsaz_context_profile_name(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if not value or len(value) > 64:
        await msg.answer('⚠️ نام باید بین ۱ تا ۶۴ نویسه باشد.'); return
    queued = await _queue_selfsaz_command(msg.from_user.id, f'تنظیم نام {value}')
    await state.clear()
    await msg.answer('✅ درخواست تغییر نام برای worker ارسال شد.' if queued else f'❌ ارسال ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.profile_bio, F.text)
async def selfsaz_context_profile_bio(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if len(value) > 70:
        await msg.answer('⚠️ بیو نباید بیشتر از ۷۰ نویسه باشد.'); return
    queued = await _queue_selfsaz_command(msg.from_user.id, f'تنظیم بیو {value}')
    await state.clear()
    await msg.answer('✅ درخواست تغییر بیو برای worker ارسال شد.' if queued else f'❌ ارسال ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.profile_photo, F.photo)
async def selfsaz_context_profile_photo(msg: Message, bot: Bot, state: FSMContext):
    target = await _profile_upload_path(msg.from_user.id, '.jpg')
    try:
        await bot.download(msg.photo[-1].file_id, destination=str(target))
        queued = await _queue_selfsaz_command(msg.from_user.id, f'__profile_photo__:{target}')
        if not queued:
            target.unlink(missing_ok=True)
        await state.clear()
        await msg.answer('✅ عکس برای تنظیم لوگوی پروفایل به worker ارسال شد.' if queued else f'❌ ارسال عکس ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')
    except Exception as exc:
        target.unlink(missing_ok=True)
        await state.clear()
        await msg.answer(f'❌ دریافت عکس ناموفق بود: {str(exc)[:180]}')


@router.message(SelfSazContextFSM.profile_photo, F.text)
async def selfsaz_context_profile_photo_invalid(msg: Message):
    await msg.answer('⚠️ لطفاً یک عکس ارسال کنید؛ برای لغو «🔴 بازگشت» را بفرستید.')


@router.message(SelfSazContextFSM.reaction_emoji, F.text)
async def selfsaz_context_reaction_emoji(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if not value or len(value) > 8:
        await msg.answer('⚠️ یک ایموجی کوتاه بفرستید؛ نمونه: 😊'); return
    await state.update_data(reaction_emoji=value)
    await state.set_state(SelfSazContextFSM.reaction_target)
    await msg.answer('👤 یوزرنیم هدف را با @ بفرستید؛ نمونه: <code>@username</code>')


@router.message(SelfSazContextFSM.reaction_target, F.text)
async def selfsaz_context_reaction_target(msg: Message, state: FSMContext):
    target = (msg.text or '').strip()
    if target == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if not target or (not target.startswith('@') and not target.lstrip('-').isdigit()):
        await msg.answer('⚠️ یوزرنیم را با @ یا شناسهٔ عددی بفرستید.'); return
    data = await state.get_data()
    command = f'ریکت {data.get("reaction_emoji", "😊")} {target}'
    queued = await _queue_selfsaz_command(msg.from_user.id, command)
    await state.clear()
    await msg.answer('✅ درخواست ریکت خودکار در صف worker قرار گرفت.' if queued else f'❌ ارسال ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.content_link, F.text)
async def selfsaz_context_content_link(msg: Message, state: FSMContext):
    value = (msg.text or '').strip()
    if value == '🔴 بازگشت':
        await state.clear(); await msg.answer('✅ لغو شد.'); return
    if not value.startswith('https://t.me/'):
        await msg.answer('⚠️ لینک پست تلگرام را با https://t.me/ بفرستید.'); return
    queued = await _queue_selfsaz_command(msg.from_user.id, f'دانلود {value}')
    await state.clear()
    await msg.answer('✅ لینک برای ذخیره در پیام‌های ذخیره‌شده به worker ارسال شد.' if queued else f'❌ ارسال ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')


@router.message(SelfSazContextFSM.video_note, F.video)
async def selfsaz_context_video_note(msg: Message, bot: Bot, state: FSMContext):
    target = await _profile_upload_path(msg.from_user.id, '.mp4')
    try:
        await bot.download(msg.video.file_id, destination=str(target))
        queued = await _queue_selfsaz_command(msg.from_user.id, f'__video_note__:{target}')
        if not queued:
            target.unlink(missing_ok=True)
        await state.clear()
        await msg.answer('✅ ویدیو برای تبدیل و ارسال به‌صورت ویدیوی گرد به worker ارسال شد.' if queued else f'❌ ارسال ویدیو ناموفق بود: {SELFSaz_LAST_ERROR or "سرویس در دسترس نیست"}')
    except Exception as exc:
        target.unlink(missing_ok=True)
        await state.clear()
        await msg.answer(f'❌ دریافت ویدیو ناموفق بود: {str(exc)[:180]}')


@router.message(SelfSazContextFSM.video_note, F.text)
async def selfsaz_context_video_note_invalid(msg: Message):
    await msg.answer('⚠️ لطفاً یک ویدیو ارسال کنید؛ برای لغو «🔴 بازگشت» را بفرستید.')


async def selfsaz_api(method: str, path: str, payload: dict | None = None):
    global SELFSaz_LAST_ERROR
    SELFSaz_LAST_ERROR = ""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers={"X-API-Key": os.getenv("SELFSaz_API_KEY", SERVICE_KEY)}) as session:
            async with session.request(method, SERVICE_URL + path, json=payload) as response:
                if response.status >= 400:
                    try:
                        body = await response.json()
                        SELFSaz_LAST_ERROR = str(body.get("detail") or body.get("message") or "")[:240]
                    except Exception:
                        SELFSaz_LAST_ERROR = (await response.text())[:240]
                    return None
                return await response.json()
    except Exception as exc:
        SELFSaz_LAST_ERROR = str(exc)[:240]
        return None


def _panel_kb(account: dict | None) -> InlineKeyboardMarkup:
    rows = []
    if account and account.get("configured"):
        active = _selfsaz_subscription_active(account)
        connected = bool(account.get("enabled")) and bool(account.get("configured")) and account.get("status") not in ("expired", "disabled", "stopped")
        if active and connected:
            rows.append([InlineKeyboardButton(text="🧰 قابلیت‌های سلف‌ساز", callback_data="pss:features", style=ButtonStyle.PRIMARY)])
            rows.append([InlineKeyboardButton(text="👤 حساب کاربری", callback_data="pss:account", style=ButtonStyle.PRIMARY)])
            rows.append([InlineKeyboardButton(text="⚙️ تنظیمات سلف‌ساز", callback_data="pss:controls", style=ButtonStyle.PRIMARY)])
            rows.append([InlineKeyboardButton(text="💬 پاسخ‌گویی و منشی خودکار", callback_data="pss:ctl:private", style=ButtonStyle.SUCCESS)])
            rows.append([InlineKeyboardButton(text="🛠 تنظیمات تپچی و رسانه", callback_data="pss:ctl:media", style=ButtonStyle.PRIMARY)])
            rows.append([InlineKeyboardButton(text="🧩 مدیریت سلف", callback_data="pss:manage", style=ButtonStyle.PRIMARY)])
        elif active and not connected:
            rows.append([InlineKeyboardButton(text="🔐 اتصال دوباره حساب تلگرام", callback_data="pss:secure_connect")])
        else:
            if not account.get("trial_used"):
                rows.append([InlineKeyboardButton(text="🎁 تست رایگان ۱ روزه", callback_data=f"pss:trial:{account.get('id', 0)}")])
            label = "🔄 تمدید اشتراک" if account.get("expires_at") else "💳 خرید اشتراک یک‌ماهه"
            rows.append([InlineKeyboardButton(text=f"{label} ({int(account.get('price', 0)):,} تومان)", callback_data=f"pss:buy:{account.get('id', 0)}")])
        rows.append([InlineKeyboardButton(text="🔄 بروزرسانی وضعیت", callback_data="pss:status")])
    else:
        rows.append([InlineKeyboardButton(text="🔐 اتصال حساب تلگرام", callback_data="pss:secure_connect")])
    rows.append([InlineKeyboardButton(text="❌ بستن پنل", callback_data="pss:close", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="hm:home_smart", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_selfsaz_panel(msg: Message, owner_id: int, edit: bool = False):
    data = await selfsaz_api("GET", f"/v1/accounts/{owner_id}")
    if data is None:
        text = "🧩 <b>سلف‌ساز</b>\n\n⚠️ سرویس مستقل سلف‌ساز در دسترس نیست."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="hm:home_smart", style=ButtonStyle.DANGER)]])
    elif not data.get("configured"):
        text = "🧩 <b>سلف‌ساز</b>\n\nهنوز حساب تلگرام شما متصل نشده است."
        kb = _panel_kb(data)
    else:
        text = ("🧩 <b>سلف‌ساز</b>\n\n"
                f"وضعیت: {_selfsaz_status_fa(data.get('status'))}\n"
                f"قیمت: {int(data.get('price', 0)):,} تومان\n"
                f"فعال: {'بله' if _selfsaz_subscription_active(data) and data.get('enabled') else 'خیر'}\n"
                f"انقضا: {_selfsaz_jalali(data.get('expires_at')) if data.get('expires_at') else 'اشتراک فعال نیست'}")
        kb = _panel_kb(data)
    if data and data.get('configured') and not edit:
        try:
            from platform_bot import get_pset
            brand = await _selfsaz_branding()
            cached_file_id = brand.get('image_file_id', '')
        except Exception:
            cached_file_id = ''
        if cached_file_id:
            try:
                await msg.answer_photo(cached_file_id, caption=_selfsaz_slider_text(0, brand), reply_markup=_selfsaz_slider_kb(0))
                return
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if _SELFSAZ_SLIDER_IMAGE:
            try:
                await msg.answer_photo(_SELFSAZ_SLIDER_IMAGE, caption=_selfsaz_slider_text(0, await _selfsaz_branding()), reply_markup=_selfsaz_slider_kb(0))
                return
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if edit:
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await msg.answer(text, reply_markup=kb)


# SELFSaz_INBOT_LOGIN_UI_V1

def _pss_normalize_digits(value: str) -> str:
    table = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
    return (value or '').translate(table)

def _pss_back_reply():
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔴 بازگشت")]], resize_keyboard=True, one_time_keyboard=False)

def _pss_code_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 ارسال دوبارهٔ کد", callback_data="pss:login:resend", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔴 بازگشت", callback_data="pss:login:back", style=ButtonStyle.DANGER)],
    ])

def _pss_password_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ رد کردن", callback_data="pss:login:skip", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔴 بازگشت", callback_data="pss:login:back", style=ButtonStyle.DANGER)],
    ])

def _pss_miniapp_keyboard(url: str) -> InlineKeyboardMarkup:
    rows = []
    if url and url.startswith('https://'):
        rows.append([InlineKeyboardButton(text="🌐 بازکردن صفحهٔ امن", web_app=WebAppInfo(url=url), style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
@router.callback_query(F.data == "pss:connect")
@router.callback_query(F.data == "pss:secure_connect")
async def selfsaz_connect_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    result = await selfsaz_api("POST", "/v1/connect/start", {"owner_id": cb.from_user.id})
    if not result or not result.get("url"):
        await cb.answer(SELFSaz_LAST_ERROR or "ساخت لینک اتصال ناموفق بود", show_alert=True)
        return
    await cb.answer("فرم ورود آماده شد", show_alert=False)
    text = (
        "🔐 <b>اتصال امن حساب سلف‌ساز</b>\n\n"
        "برای اتصال حساب، روی دکمهٔ زیر بزنید و شماره تلفن تلگرام را با کد کشور وارد کنید. "
        "سپس کد ورود و در صورت فعال بودن، رمز دومرحله‌ای را وارد کنید.\n\n"
        "این لینک یک‌بارمصرف است و پس از ۱۰ دقیقه منقضی می‌شود."
    )
    kb = _pss_miniapp_keyboard(result["url"])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer(text, reply_markup=kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

async def _pss_legacy_connect_blocked(message_or_callback, state=None):
    if state is not None:
        try:
            await state.clear()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    message = getattr(message_or_callback, "message", None) or message_or_callback
    try:
        await message.answer(
            "🔒 <b>اتصال مستقیم حساب غیرفعال است</b>\n\nبرای امنیت، شماره، کد ورود و رمز دوم در چت یا این سرویس دریافت نمی‌شود. sessionهای قبلی همچنان قابل مدیریت هستند.\n\nبرای ادامه به پنل سلف‌ساز برگردید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت به سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)],
            ]),
        )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

@router.message(SelfSazConnectFSM.phone, F.text)
@router.message(SelfSazConnectFSM.code, F.text)
@router.message(SelfSazConnectFSM.password, F.text)
async def selfsaz_legacy_login_message_blocked(msg: Message, state: FSMContext):
    await _pss_legacy_connect_blocked(msg, state)

@router.callback_query(F.data.startswith("pss:login:"))
async def selfsaz_legacy_login_callback_blocked(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("اتصال مستقیم حساب غیرفعال است", show_alert=True)
    try:
        await cb.message.edit_text(
            "🔒 <b>اتصال مستقیم حساب غیرفعال است</b>\n\nبرای امنیت، شماره، کد ورود و رمز دوم در چت یا این سرویس دریافت نمی‌شود. sessionهای قبلی همچنان قابل مدیریت هستند.\n\nبرای ادامه به پنل سلف‌ساز برگردید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت به سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)],
            ]),
        )
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

# SELFSaz_INBOT_LOGIN_UI_V1

@router.callback_query(F.data == "pss:close")
async def selfsaz_close_panel(cb: CallbackQuery):
    await cb.answer("پنل بسته شد")
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_text("✅ پنل سلف‌ساز بسته شد.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@router.callback_query(F.data == "pss:account")
async def selfsaz_account_panel(cb: CallbackQuery):
    data = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}") or {}
    if not data.get("configured"):
        await cb.answer("حساب هنوز متصل نشده است", show_alert=True)
        return
    expires = _selfsaz_jalali(data.get("expires_at")) if data.get("expires_at") else "بدون اشتراک فعال"
    wallet_balance = 0
    try:
        from sqlalchemy import select
        from models import PlatformUser
        async with Session() as s:
            user = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == cb.from_user.id))
            wallet_balance = int(float(user.balance or 0)) if user else 0
    except Exception:
        wallet_balance = 0
    brand = await _selfsaz_branding()
    brand_name = _html_escape(str(brand.get("name") or "سلف‌ساز"))
    text = ("👤 <b>حساب کاربری سلف‌ساز</b>\n\n"
            f"🧩 نام سلف‌ساز: <b>{brand_name}</b>\n"
            f"🆔 شناسه مالک: <code>{cb.from_user.id}</code>\n"
            f"🔗 وضعیت اتصال: {_selfsaz_status_fa(data.get('status'))}\n"
            f"💰 موجودی کیف پول: <b>{wallet_balance:,} تومان</b>\n"
            f"📅 اشتراک تا: {expires}\n"
            f"🎁 آزمایشی: {'استفاده شده' if data.get('trial_used') else 'قابل استفاده'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 {wallet_balance:,} تومان", callback_data="pss:account", style=ButtonStyle.SUCCESS),
         InlineKeyboardButton(text="👤 حساب کاربری", callback_data="pss:account", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="pss:account", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⚙️ تنظیمات سلف‌ساز", callback_data="pss:controls", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ پنل اصلی", callback_data="pss:status", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "pss:status")
async def selfsaz_status(cb: CallbackQuery):
    await cb.answer()
    await show_selfsaz_panel(cb.message, cb.from_user.id, edit=True)


@router.callback_query(F.data.startswith("pss:trial:"))
async def selfsaz_trial(cb: CallbackQuery):
    account_id = int(cb.data.rsplit(":", 1)[1])
    account = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
    if not account or int(account.get("id", 0)) != account_id or not account.get("configured"):
        await cb.answer("حساب سلف‌ساز پیدا نشد", show_alert=True)
        return
    if account.get("trial_used"):
        await cb.answer("دورهٔ آزمایشی قبلاً استفاده شده است", show_alert=True)
        return
    result = await selfsaz_api("POST", f"/v1/accounts/{account_id}/trial")
    if result:
        await cb.answer("✅ تست رایگان ۱ روزه فعال شد")
    else:
        await cb.answer("دورهٔ آزمایشی قبلاً استفاده شده یا سرویس در دسترس نیست", show_alert=True)
    await show_selfsaz_panel(cb.message, cb.from_user.id, edit=True)

@router.callback_query(F.data.regexp(r"^pss:buy:\d+$"))
async def selfsaz_buy(cb: CallbackQuery):
    account_id = int(cb.data.rsplit(":", 1)[1])
    account = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
    if not account or not account.get("configured") or int(account.get("id", 0)) != account_id:
        await cb.answer("حساب سلف‌ساز پیدا نشد", show_alert=True)
        return
    price = int(account.get("price", 0))
    if price <= 0:
        await cb.answer("قیمت سلف‌ساز هنوز تعیین نشده است", show_alert=True)
        return
    renew = bool(account.get("expires_at"))
    title = "تمدید اشتراک سلف‌ساز" if renew else "فعال‌سازی سلف‌ساز"
    text = (
        f"🧩 <b>{title}</b>\n\n"
        f"مبلغ اشتراک یک‌ماهه: <b>{price:,} تومان</b>\n"
        "پس از تأیید، مبلغ از کیف پول شما کسر می‌شود. "
        "اگر فعال‌سازی ناموفق باشد، مبلغ به‌صورت خودکار به کیف پول برمی‌گردد.\n\n"
        "برای ادامه روی دکمهٔ پرداخت بزنید یا برای لغو به پنل سلف‌ساز برگردید."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و پرداخت", callback_data=f"pss:buy:confirm:{account_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data="pss:status", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("pss:buy:confirm:"))
async def selfsaz_buy_confirm(cb: CallbackQuery):
    account_id = int(cb.data.rsplit(":", 1)[1])
    account = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
    if not account or not account.get("configured") or int(account.get("id", 0)) != account_id:
        await cb.answer("حساب سلف‌ساز پیدا نشد", show_alert=True)
        return
    price = int(account.get("price", 0))
    if price <= 0:
        await cb.answer("قیمت سلف‌ساز هنوز تعیین نشده است", show_alert=True)
        return
    from platform_bot import get_user, rial
    async with Session() as s:
        user = await get_user(s, cb.from_user.id, cb.from_user.full_name, for_update=True, username=cb.from_user.username)
        current_balance = float(user.balance or 0)
        if current_balance < price:
            await s.rollback()
            await cb.message.edit_text(
                f"⚠️ موجودی کیف پول کافی نیست.\n\nمبلغ لازم: <b>{rial(price)}</b>\nموجودی فعلی: <b>{rial(current_balance)}</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💰 رفتن به کیف پول", callback_data="go_wallet")],
                    [InlineKeyboardButton(text="⬅️ بازگشت به سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)],
                ]),
            )
            await cb.answer()
            return
        user.balance = float(user.balance) - price
        s.add(PlatformTxn(user_telegram_id=cb.from_user.id, amount=-price, kind="purchase", note="خرید و فعال‌سازی سلف‌ساز"))
        await s.commit()
    activated = await selfsaz_api("POST", f"/v1/accounts/{account_id}/activate", {"days": 30})
    ok = bool(activated and activated.get("enabled"))
    if not ok:
        # پاسخ نرسید (مثلاً تایم‌اوت) ولی شاید سرویس فعال کرده باشد؛ اگر تاریخِ
        # انقضا جلو رفته، بازپرداخت نکن — وگرنه ۳۰ روزِ رایگان می‌ماند
        after = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
        ok = bool(after and after.get("expires_at")
                  and after.get("expires_at") != account.get("expires_at"))
    if not ok:
        async with Session() as s:
            user = await get_user(s, cb.from_user.id, cb.from_user.full_name, for_update=True, username=cb.from_user.username)
            user.balance = float(user.balance or 0) + price
            s.add(PlatformTxn(user_telegram_id=cb.from_user.id, amount=price, kind="refund", note="بازگشت وجه خرید ناموفق سلف‌ساز"))
            await s.commit()
        await cb.answer("فعال‌سازی ناموفق بود؛ مبلغ به کیف پول برگشت داده شد", show_alert=True)
    else:
        await cb.answer("✅ خرید با کیف پول انجام شد و سلف‌ساز فعال شد")
    await show_selfsaz_panel(cb.message, cb.from_user.id, edit=True)
@router.callback_query(F.data.startswith("pss:toggle:"))
async def selfsaz_toggle(cb: CallbackQuery):
    account_id = int(cb.data.rsplit(":", 1)[1])
    # فقط حسابِ خودِ کاربر — قبلاً با شناسهٔ دلخواه، سلفِ دیگران روشن/خاموش می‌شد
    account = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
    if not account or int(account.get("id", 0) or 0) != account_id:
        await cb.answer("این حساب مالِ تو نیست.", show_alert=True)
        return
    result = await selfsaz_api("POST", f"/v1/accounts/{account_id}/toggle")
    await cb.answer("وضعیت تغییر کرد" if result else "سرویس در دسترس نیست", show_alert=not bool(result))
    await show_selfsaz_panel(cb.message, cb.from_user.id, edit=True)


# SELFSaz_REFERENCE_MANAGEMENT_V1
async def _selfsaz_manage_render(cb: CallbackQuery, data: dict, notice: str = ""):
    account_id = int(data.get("id") or 0)
    enabled = bool(data.get("enabled"))
    status = _selfsaz_status_fa(data.get("status"))
    state_label = "فعال" if enabled else "غیرفعال"
    text = (
        "🧩 <b>مدیریت سلف‌ساز</b>\n\n"
        f"وضعیت سرویس: <b>{status}</b>\n"
        f"وضعیت سلف: <b>{state_label}</b>\n"
        "\nاز این بخش می‌توانید سلف متصل‌شده را روشن/خاموش کنید یا از حساب خارج شوید."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    toggle_text = "🔴 خاموش کردن" if enabled else "🟢 روشن کردن"
    toggle_style = ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="pss:manage:toggle", style=toggle_style)],
        [InlineKeyboardButton(text="🔄 بروزرسانی وضعیت", callback_data="pss:manage", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🧰 قابلیت‌های سلف‌ساز", callback_data="pss:features", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🗑 حذف سلف / خروج از حساب", callback_data="pss:logout", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ پنل سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data == "pss:manage")
async def selfsaz_reference_manage_panel(cb: CallbackQuery):
    data = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}") or {}
    if not data.get("configured"):
        await cb.answer("حساب سلف‌ساز هنوز متصل نیست", show_alert=True)
        return
    await _selfsaz_manage_render(cb, data)
    await cb.answer()


@router.callback_query(F.data == "pss:manage:toggle")
async def selfsaz_reference_manage_toggle(cb: CallbackQuery):
    data = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}") or {}
    if not data.get("configured") or not data.get("id"):
        await cb.answer("حساب سلف‌ساز پیدا نشد", show_alert=True)
        return
    account_id = int(data["id"])
    result = await selfsaz_api("POST", f"/v1/accounts/{account_id}/toggle")
    if not result:
        await cb.answer("تغییر وضعیت انجام نشد؛ سرویس در دسترس نیست", show_alert=True)
        return
    fresh = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}") or data
    await _selfsaz_manage_render(cb, fresh, "✅ وضعیت سلف به‌روزرسانی شد")
    await cb.answer("✅ انجام شد")


# SELFSaz_REFERENCE_MANAGEMENT_V1

# SELFSaz_MINIAPP_CALLBACK_REMOVED_FOR_INBOT_LOGIN_V2
# SELFSaz_MINIAPP_UI_V1


# SELFSaz_FEATURES_PANEL_V1 — راهنمای فارسی دستورات worker واقعی؛ بدون تغییر worker
_SELFSaz_FEATURES = {
    "groups": (
        "👥 <b>مدیریت گروه</b>\n\n"
        "دستورها را از طریق همان حساب متصل‌شده در گروه ارسال کنید:\n\n"
        "• <code>بن</code> — بن‌کردن کاربر ریپلای‌شده\n"
        "• <code>آنبن @username</code> — رفع بن\n"
        "• <code>کیک</code> — اخراج کاربر ریپلای‌شده\n"
        "• <code>سکوت</code> و <code>حذف سکوت</code>\n"
        "• <code>ادمین</code> و <code>حذف ادمین</code>\n"
        "• <code>پاک 10</code> — حذف تعداد پیام مشخص\n"
        "• <code>پین</code> و <code>آنپین</code>\n"
        "• <code>تنظیم عنوان متن</code>\n"
        "• <code>تنظیم توضیحات متن</code>\n"
        "• <code>تنظیم عکس</code> با ریپلای روی عکس\n"
        "• <code>اهسته روشن</code> و <code>اهسته خاموش</code>"
    ),
    "profile": (
        "👤 <b>پروفایل و ساعت</b>\n\n"
        "• <code>تایم روشن</code> یا <code>تایم خاموش</code>\n"
        "• <code>لیست فونت</code>\n"
        "• <code>تنظیم فونت نام فونت</code>\n"
        "• <code>آنلاین روشن</code> یا <code>آنلاین خاموش</code>\n"
        "• <code>ایدی</code> برای دریافت اطلاعات حساب"
    ),
    "private": (
        "💬 <b>پیام خصوصی و پاسخ خودکار</b>\n\n"
        "• <code>پاسخ</code> برای مدیریت پاسخ‌های خودکار\n"
        "• <code>دشمن</code> و <code>لیست دشمن</code>\n"
        "• <code>دشمنان</code> و <code>پاک کردن دشمنان</code>\n"
        "• <code>فحش</code> و <code>لیست فحش</code>\n"
        "• <code>ترجمه</code> در صورت فعال‌بودن قابلیت worker\n"
        "• <code>ریکت متن</code> برای واکنش خودکار"
    ),
    "tools": (
        "🛠 <b>ابزارها و تنظیمات بیشتر</b>\n\n"
        "• <code>اطلاعات گروه</code>\n"
        "• <code>لیست ادمین</code>\n"
        "• <code>تعداد اعضا</code>\n"
        "• <code>همه روشن</code> و <code>همه خاموش</code>\n"
        "• <code>مدیا روشن/خاموش</code>\n"
        "• <code>استیکر روشن/خاموش</code>\n"
        "• <code>فوروارد روشن/خاموش</code>\n"
        "• <code>ویس روشن/خاموش</code>\n"
        "• <code>پیام روشن/خاموش</code>\n"
        "• <code>فایل روشن/خاموش</code>\n"
        "• <code>راهنمای قفل</code> و <code>وضعیت قفل</code>"
    ),
}


_SELFSaz_FEATURE_COMMANDS = {
    'groups': [('بن', 'بن'), ('آنبن', 'آنبن @username'), ('کیک', 'کیک'), ('سکوت', 'سکوت'), ('حذف سکوت', 'حذف سکوت'), ('ادمین', 'ادمین'), ('حذف ادمین', 'حذف ادمین'), ('پاک‌کردن پیام', 'پاک 10'), ('پین', 'پین'), ('آنپین', 'آنپین'), ('تنظیم عنوان', 'تنظیم عنوان متن'), ('تنظیم توضیحات', 'تنظیم توضیحات متن'), ('تنظیم عکس', 'تنظیم عکس'), ('حالت آهسته', 'اهسته روشن')],
    'profile': [('ساعت روشن/خاموش', 'تایم روشن / تایم خاموش'), ('لیست فونت', 'لیست فونت'), ('تنظیم فونت', 'تنظیم فونت نام فونت'), ('آنلاین روشن/خاموش', 'آنلاین روشن / آنلاین خاموش'), ('شناسه حساب', 'ایدی')],
    'private': [('پاسخ خودکار', 'پاسخ'), ('پاسخ عمومی', 'پاسخ عمومی روشن'), ('دشمن', 'دشمن'), ('لیست دشمن', 'لیست دشمن'), ('پاک‌کردن دشمنان', 'پاک کردن دشمنان'), ('فهرست فحش', 'فحش'), ('واکنش خودکار', 'ریکت متن'), ('ترجمه', 'ترجمه')],
    'tools': [('اطلاعات گروه', 'اطلاعات گروه'), ('لیست ادمین', 'لیست ادمین'), ('تعداد اعضا', 'تعداد اعضا'), ('همه روشن', 'همه روشن'), ('همه خاموش', 'همه خاموش'), ('مدیا', 'مدیا روشن/خاموش'), ('استیکر', 'استیکر روشن/خاموش'), ('فوروارد', 'فوروارد روشن/خاموش'), ('ویس', 'ویس روشن/خاموش'), ('فایل', 'فایل روشن/خاموش'), ('پینگ', 'پینگ')],
}


def _selfsaz_features_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 مدیریت گروه", callback_data="pss:feat:groups", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="👤 پروفایل و ساعت", callback_data="pss:feat:profile", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="💬 خصوصی و پاسخ خودکار", callback_data="pss:feat:private", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🛠 ابزارها و تنظیمات", callback_data="pss:feat:tools", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت به سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)],
    ])


@router.callback_query(F.data == "pss:features")
async def selfsaz_features_panel(cb: CallbackQuery):
    account = await selfsaz_api("GET", f"/v1/accounts/{cb.from_user.id}")
    if not account or not account.get("configured"):
        await cb.answer("ابتدا حساب تلگرام را متصل کنید", show_alert=True)
        return
    text = ("🧰 <b>پنل قابلیت‌های سلف‌ساز</b>\n\n"
            "قابلیت موردنظر را انتخاب کنید. این صفحه راهنمای دستورات worker متصل‌شده است؛ "
            "در مرحلهٔ بعد کنترل مستقیم تنظیمات منتخب نیز اضافه می‌شود.")
    try:
        await cb.message.edit_text(text, reply_markup=_selfsaz_features_kb())
    except Exception:
        await cb.message.answer(text, reply_markup=_selfsaz_features_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("pss:feat:"))
async def selfsaz_feature_detail(cb: CallbackQuery):
    key = cb.data.rsplit(":", 1)[1]
    text = _SELFSaz_FEATURES.get(key)
    if not text:
        await cb.answer("بخش پیدا نشد", show_alert=True)
        return
    commands = _SELFSaz_FEATURE_COMMANDS.get(key, [])
    rows = [[InlineKeyboardButton(text=f'🔘 {label}', callback_data=f'pss:cmd:{key}:{idx}', style=ButtonStyle.PRIMARY)] for idx, (label, _command) in enumerate(commands)]
    rows += [[InlineKeyboardButton(text="⚙️ کنترل‌های واقعی", callback_data="pss:controls", style=ButtonStyle.SUCCESS)], [InlineKeyboardButton(text="⬅️ دسته‌بندی قابلیت‌ها", callback_data="pss:features", style=ButtonStyle.DANGER)], [InlineKeyboardButton(text="🏠 سلف‌ساز", callback_data="pss:status", style=ButtonStyle.DANGER)]]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith('pss:cmd:'))
async def selfsaz_feature_command_hint(cb: CallbackQuery):
    parts = cb.data.split(':')
    if len(parts) != 4:
        await cb.answer('دستور نامعتبر', show_alert=True)
        return
    key, idx_text = parts[2], parts[3]
    try:
        label, command = _SELFSaz_FEATURE_COMMANDS[key][int(idx_text)]
    except (KeyError, IndexError, ValueError):
        await cb.answer('دستور پیدا نشد', show_alert=True)
        return
    context = 'این دستور را در گروه یا گفت‌وگوی هدف، از حساب متصل‌شده ارسال کنید.' if key == 'groups' else 'این دستور را از حساب متصل‌شده در Saved Messages ارسال کنید.'
    await cb.answer(f'{label}: {command}', show_alert=True)
    try:
        await cb.message.edit_text(f'🔘 <b>{label}</b>\n\nدستور: <code>{command}</code>\n\n{context}', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ بازگشت به قابلیت‌ها', callback_data=f'pss:feat:{key}', style=ButtonStyle.DANGER)], [InlineKeyboardButton(text='🏠 سلف‌ساز', callback_data='pss:status', style=ButtonStyle.DANGER)]]))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

# SELFSaz_FEATURES_PANEL_V1

# SELFSaz_FEATURES_MARKER_FIX_V1


# SELFSaz_CONTROLS_PLATFORM_V1 — کنترل امن ساعت، فونت و فرمت
async def _pss_control_get(owner_id: int):
    data = await selfsaz_api('GET', f'/v1/accounts/{owner_id}/control') or {}
    return data.get('control', {})


async def _pss_control_kb(owner_id: int):
    cfg = await _pss_control_get(owner_id)
    time_on = bool(cfg.get('time_enabled', False))
    font = int(cfg.get('font', 1))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=('🟢 ساعت: فعال' if time_on else '🔴 ساعت: غیرفعال'), callback_data='pss:ctl:time', style=(ButtonStyle.SUCCESS if time_on else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f'🔤 فونت فعلی: {font}', callback_data='pss:ctl:font', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='🎨 فرمت پیام‌ها', callback_data='pss:ctl:formats', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='🔒 قفل‌های گروه', callback_data='pss:ctl:locks', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='⚡ اکشن‌های تلگرام', callback_data='pss:ctl:actions', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='💬 پاسخ خودکار و دشمن', callback_data='pss:ctl:private', style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text='🛠 ابزارهای خصوصی و رسانه', callback_data='pss:ctl:media', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='📊 وضعیت worker و گزارش', callback_data='pss:ctl:status', style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text='🚪 خروج از حساب', callback_data='pss:logout', style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text='⬅️ بازگشت به پنل قابلیت‌ها', callback_data='pss:features', style=ButtonStyle.DANGER)],
    ])


@router.callback_query(F.data == 'pss:controls')
async def selfsaz_controls_panel(cb: CallbackQuery):
    account = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}') or {}
    if not account.get('configured'):
        await cb.answer('ابتدا حساب را متصل کنید', show_alert=True); return
    kb = await _pss_control_kb(cb.from_user.id)
    await cb.message.edit_text('⚙️ <b>کنترل‌های واقعی سلف‌ساز</b>\n\nتنظیمات مستقیماً برای worker همین حساب ذخیره و اعمال می‌شوند.', reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == 'pss:ctl:time')
async def selfsaz_control_time(cb: CallbackQuery):
    cfg = await _pss_control_get(cb.from_user.id)
    result = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'time_enabled': not bool(cfg.get('time_enabled', False))})
    await cb.answer('تنظیم ساعت ذخیره شد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result:
        await cb.message.edit_text('⚙️ <b>کنترل‌های واقعی سلف‌ساز</b>\n\nتنظیمات ذخیره و برای worker ارسال شد.', reply_markup=await _pss_control_kb(cb.from_user.id))


@router.callback_query(F.data == 'pss:ctl:font')
async def selfsaz_control_font(cb: CallbackQuery):
    cfg = await _pss_control_get(cb.from_user.id); current = int(cfg.get('font', 1)); nxt = 1 if current >= 6 else current + 1
    result = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'font': nxt})
    await cb.answer(f'فونت به {nxt} تغییر کرد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result:
        await cb.message.edit_text('⚙️ <b>کنترل‌های واقعی سلف‌ساز</b>\n\nتنظیمات ذخیره و برای worker ارسال شد.', reply_markup=await _pss_control_kb(cb.from_user.id))


@router.callback_query(F.data == 'pss:ctl:formats')
async def selfsaz_control_formats(cb: CallbackQuery):
    cfg = await _pss_control_get(cb.from_user.id)
    formats = cfg.get('formats', {})
    names = [('بولد','بولد'),('ایتالیک','ایتالیک'),('زیر خط','زیر خط'),('خط‌ خورده','خط‌ خورده'),('اسپویلر','اسپویلر'),('کد','کد'),('پیش‌ فرمت','پیش‌ فرمت'),('نقل‌ قول','نقل‌ قول')]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('🟢 ' if formats.get(key) else '🔴 ') + label, callback_data='pss:fmt:' + key, style=(ButtonStyle.SUCCESS if formats.get(key) else ButtonStyle.DANGER))] for key, label in names] + [[InlineKeyboardButton(text='⬅️ کنترل‌ها', callback_data='pss:controls')]])
    await cb.message.edit_text('🎨 <b>فرمت پیام‌ها</b>\n\nبرای روشن یا خاموش‌کردن هر فرمت روی آن بزنید.', reply_markup=kb)
    await cb.answer()

# SELFSaz_CONTROLS_PLATFORM_V1


# SELFSaz_ADVANCED_CONTROLS_PLATFORM_V2
_FORMAT_KEYS = {'بولد','ایتالیک','زیر خط','خط‌ خورده','اسپویلر','کد','پیش‌ فرمت','نقل‌ قول'}
_LOCK_KEYS = {'همه','مدیا','استیکر','فوروارد','ویس','پیام','فایل'}
_ACTION_KEYS = {'typing','upload_photo','record_audio','upload_video','upload_document','record_video','upload_audio','upload_video_note','record_video_note','playing','choose_contact','find_location','choose_sticker'}


@router.callback_query(F.data.startswith('pss:fmt:'))
async def selfsaz_toggle_format(cb: CallbackQuery):
    key = cb.data.split(':', 2)[2]
    if key not in _FORMAT_KEYS:
        await cb.answer('فرمت نامعتبر', show_alert=True); return
    cfg = await _pss_control_get(cb.from_user.id); state = bool(cfg.get('formats', {}).get(key, False))
    result = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/control', {'formats': {key: not state}})
    await cb.answer('تغییر ذخیره شد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result: await selfsaz_control_formats(cb)


@router.callback_query(F.data == 'pss:ctl:locks')
async def selfsaz_control_locks(cb: CallbackQuery):
    cfg = await _pss_control_get(cb.from_user.id); locks = cfg.get('locks', {})
    labels = [('همه','همه'),('مدیا','مدیا'),('استیکر','استیکر'),('فوروارد','فوروارد'),('ویس','ویس'),('پیام','پیام'),('فایل','فایل')]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('🟢 ' if locks.get(k) else '🔴 ') + label, callback_data='pss:lock:' + k, style=(ButtonStyle.SUCCESS if locks.get(k) else ButtonStyle.DANGER))] for k, label in labels] + [[InlineKeyboardButton(text='⬅️ کنترل‌ها', callback_data='pss:controls')]])
    await cb.message.edit_text('🔒 <b>قفل‌های گروه</b>\\n\\nقفل موردنظر را روشن یا خاموش کنید.', reply_markup=kb); await cb.answer()


@router.callback_query(F.data.startswith('pss:lock:'))
async def selfsaz_toggle_lock(cb: CallbackQuery):
    key = cb.data.split(':', 2)[2]
    if key not in _LOCK_KEYS: await cb.answer('قفل نامعتبر', show_alert=True); return
    cfg = await _pss_control_get(cb.from_user.id); state = bool(cfg.get('locks', {}).get(key, False))
    result = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/advanced-control', {'locks': {key: not state}})
    await cb.answer('قفل تغییر کرد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result: await selfsaz_control_locks(cb)


@router.callback_query(F.data == 'pss:ctl:actions')
async def selfsaz_control_actions(cb: CallbackQuery):
    cfg = await _pss_control_get(cb.from_user.id); actions = cfg.get('actions', {})
    labels = [('typing','تایپ'),('upload_photo','آپلود عکس'),('record_audio','ضبط ویس'),('upload_video','آپلود ویدیو'),('upload_document','آپلود فایل'),('playing','بازی'),('choose_sticker','انتخاب استیکر')]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('🟢 ' if actions.get(k) else '🔴 ') + label, callback_data='pss:act:' + k, style=(ButtonStyle.SUCCESS if actions.get(k) else ButtonStyle.DANGER))] for k, label in labels] + [[InlineKeyboardButton(text='⬅️ کنترل‌ها', callback_data='pss:controls')]])
    await cb.message.edit_text('⚡ <b>اکشن‌های تلگرام</b>\\n\\nاکشن موردنظر را روشن یا خاموش کنید.', reply_markup=kb); await cb.answer()


@router.callback_query(F.data.startswith('pss:act:'))
async def selfsaz_toggle_action(cb: CallbackQuery):
    key = cb.data.split(':', 2)[2]
    if key not in _ACTION_KEYS: await cb.answer('اکشن نامعتبر', show_alert=True); return
    cfg = await _pss_control_get(cb.from_user.id); state = bool(cfg.get('actions', {}).get(key, False))
    result = await selfsaz_api('PUT', f'/v1/accounts/{cb.from_user.id}/advanced-control', {'actions': {key: not state}})
    await cb.answer('اکشن تغییر کرد' if result else 'سرویس در دسترس نیست', show_alert=not bool(result))
    if result: await selfsaz_control_actions(cb)

# SELFSaz_ADVANCED_CONTROLS_PLATFORM_V2


# SELFSaz_PRIVATE_PANEL_V3 — راهنمای فارسی قابلیت‌های worker واقعی
@router.callback_query(F.data == 'pss:ctl:private')
async def selfsaz_private_panel(cb: CallbackQuery):
    text = (
        '💬 <b>پاسخ خودکار و دشمن</b>\n\n'
        'دستورهای این بخش را از همان حساب متصل‌شده ارسال کنید:\n\n'
        '• <code>پاسخ</code> — نمایش راهنمای پاسخ خودکار\n'
        '• <code>دشمن</code> — مدیریت فهرست دشمنان\n'
        '• <code>لیست دشمن</code> — نمایش دشمنان\n'
        '• <code>پاک کردن دشمنان</code> — پاک‌کردن فهرست\n'
        '• <code>فحش</code> و <code>لیست فحش</code> — مدیریت فهرست کلمات\n'
        '• <code>ریکت متن</code> — تنظیم واکنش خودکار\n\n'
        'حساب متصل باید این دستورها را در Saved Messages یا چت موردنظر اجرا کند.'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⬅️ کنترل‌های واقعی', callback_data='pss:controls')],
        [InlineKeyboardButton(text='⬅️ پنل قابلیت‌ها', callback_data='pss:features')],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()


@router.callback_query(F.data == 'pss:ctl:media')
async def selfsaz_media_panel(cb: CallbackQuery):
    text = (
        '🛠 <b>ابزارهای خصوصی و رسانه</b>\n\n'
        '• <code>مدیا روشن/خاموش</code>\n'
        '• <code>استیکر روشن/خاموش</code>\n'
        '• <code>فوروارد روشن/خاموش</code>\n'
        '• <code>ویس روشن/خاموش</code>\n'
        '• <code>پیام روشن/خاموش</code>\n'
        '• <code>فایل روشن/خاموش</code>\n'
        '• <code>همه روشن</code> و <code>همه خاموش</code>\n'
        '• <code>سیو @username</code> — پشتیبان‌گیری از گفت‌وگو\n'
        '• <code>پینگ</code> — بررسی سرعت worker\n'
        '• <code>اینستا لینک</code> — دانلود پست و ریل'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⬅️ کنترل‌های واقعی', callback_data='pss:controls')],
        [InlineKeyboardButton(text='⬅️ پنل قابلیت‌ها', callback_data='pss:features')],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()

# SELFSaz_PRIVATE_PANEL_V3


# SELFSaz_STATUS_DASHBOARD_V4
@router.callback_query(F.data == 'pss:ctl:status')
async def selfsaz_status_dashboard(cb: CallbackQuery):
    data = await selfsaz_api('GET', f'/v1/accounts/{cb.from_user.id}/runtime-status')
    if not data:
        await cb.answer('سرویس سلف‌ساز در دسترس نیست', show_alert=True); return
    account = data.get('account', {}); control = data.get('control', {})
    enabled = 'فعال' if account.get('enabled') else 'غیرفعال'
    alive = 'در حال اجرا' if data.get('worker_alive') else 'worker اجرا نیست'
    status_map = {'running':'در حال اجرا','stopped':'متوقف','error':'خطا','configured':'متصل، آمادهٔ خرید'}
    status = status_map.get(account.get('status'), account.get('status') or 'نامشخص')
    time_state = 'روشن' if control.get('time_enabled') else 'خاموش'
    formats = [k for k, v in control.get('formats', {}).items() if v]
    locks = [k for k, v in control.get('locks', {}).items() if v]
    error = account.get('last_error') or 'بدون خطا'
    text = (f'📊 <b>وضعیت SelfSaz</b>\n\nوضعیت حساب: <b>{status}</b>\n'
            f'فعال‌سازی: <b>{enabled}</b>\nworker: <b>{alive}</b>\n'
            f'ساعت: <b>{time_state}</b>\nفرمت‌های روشن: <b>{", ".join(formats) if formats else "هیچ‌کدام"}</b>\n'
            f'قفل‌های روشن: <b>{", ".join(locks) if locks else "هیچ‌کدام"}</b>\n'
            f'آخرین خطا: <code>{_esc_html(str(error))}</code>')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔄 بروزرسانی', callback_data='pss:ctl:status')],
        [InlineKeyboardButton(text='⬅️ کنترل‌های واقعی', callback_data='pss:controls')],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()

# SELFSaz_STATUS_DASHBOARD_V4


# SELFSaz_SAFE_LOGOUT_PLATFORM_V5
@router.callback_query(F.data == 'pss:logout')
async def selfsaz_logout_confirm(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ بله، خروج از حساب', callback_data='pss:logout:yes')],
        [InlineKeyboardButton(text='❌ انصراف', callback_data='pss:controls')],
    ])
    await cb.message.edit_text('🚪 <b>خروج از حساب</b>\n\nبا تأیید، session این حساب حذف و worker متوقف می‌شود. رسیدها، قیمت و تنظیمات مدیریتی حذف نمی‌شوند. برای استفادهٔ دوباره باید دوباره از Mini App وارد شوید.', reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == 'pss:logout:yes')
async def selfsaz_logout_execute(cb: CallbackQuery):
    result = await selfsaz_api('POST', f'/v1/accounts/{cb.from_user.id}/logout')
    if not result:
        await cb.answer('خروج انجام نشد؛ سرویس در دسترس نیست', show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔐 اتصال دوباره', callback_data='pss:secure_connect')], [InlineKeyboardButton(text='⬅️ سلف‌ساز', callback_data='pss:status')]])
    await cb.message.edit_text('✅ از حساب SelfSaz خارج شدید. session حذف شد و worker متوقف می‌شود. برای اتصال دوباره روی دکمهٔ زیر بزنید.', reply_markup=kb)
    await cb.answer('خروج با موفقیت انجام شد')

# SELFSaz_SAFE_LOGOUT_PLATFORM_V5

# SELFSaz_PLATFORM_SUBSCRIPTION_V6

# SELFSaz_SUBSCRIPTION_UI_V1

# SELFSaz_EXPIRY_RENEWAL_V1

# SELFSaz_WEBAPP_RETURN_V1

# SELFSaz_DEEPLINK_RETURN_V2

# SELFSaz_CONTACT_AND_ADMIN_V1
