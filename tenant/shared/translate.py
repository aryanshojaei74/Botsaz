"""
translate.py — ترجمهٔ زندهٔ متن‌هایِ ربات با هوشِ مصنوعی (PATCH_BOT_LANG_TOGGLE_V1).

ایده: به‌جایِ از‌پیش‌ترجمه‌کردنِ هزاران متنِ فارسیِ ثابتِ ربات (که عملاً
شدنی نیست)، وقتی کاربر زبانش رو رویِ انگلیسی گذاشته، متنِ آماده‌شده (همون
که برایِ کاربرِ فارسی‌زبان فرستاده می‌شد) لحظه‌ای ترجمه می‌شه.

=== PATCH_BOT_LANG_MULTI_PROVIDER_V1 ===
دیگه مستقیم به Anthropic وصل نمی‌شه — از همون سیستمِ چندارائه‌دهنده‌ایِ
smm_ai.py استفاده می‌کنه (Claude/Gemini/ChatGPT، هرکدوم که ادمین برایِ
«دستیارِ هوشِ مصنوعی»‌یِ خدماتِ مجازی تنظیم کرده). یعنی یه کلید/تنظیم،
هم دستیارِ مشتری هم ترجمهٔ زبان رو پوشش می‌ده — نیازی به تنظیمِ جداگانه نیست.

کش: چون متنِ منوها معمولاً ثابته، هر متن فقط یک‌بار (برایِ هر ربات) ترجمه
می‌شه و نتیجه تو حافظه نگه داشته می‌شه — نه هر بار که کاربر منو رو باز
می‌کنه دوباره هزینه/تأخیر داشته باشیم.
"""
from __future__ import annotations

# کشِ ساده: { (bot_id, متنِ اصلی): متنِ ترجمه‌شده }
_CACHE: dict[tuple[int, str], str] = {}
_CACHE_MAX = 2000  # سقف برایِ جلوگیری از رشدِ بی‌حدِ حافظه

# === PATCH_BOT_LANG_REPLY_BUTTON_FIX_V1 ===
# نگاشتِ معکوس: { (bot_id, متنِ ترجمه‌شده): متنِ اصلیِ فارسی }
#
# چرا لازم است: دکمه‌هایِ کیبوردی (ReplyKeyboardMarkup) هیچ callback_data
# ندارند — تلگرام فقط *متنِ* دکمه را به‌عنوانِ یک پیامِ معمولی می‌فرستد، و
# دیسپچرِ ربات آن متن را با MainButton.label مقایسه می‌کند. وقتی برچسب‌ها
# برایِ کاربرِ انگلیسی‌زبان ترجمه می‌شدند، متنِ رسیده دیگر با هیچ برچسبِ
# فارسی‌ای برابر نبود و همهٔ دکمه‌ها بی‌کار می‌شدند (باگِ گزارش‌شده:
# «فقط اسمِ دکمه‌ها عوض می‌شه و دیگه کار نمی‌کنن»). با این نگاشت، متنِ
# انگلیسیِ رسیده دوباره به برچسبِ فارسیِ اصلی برگردانده می‌شود و دیسپچر
# مثلِ قبل کار می‌کند.
_REVERSE: dict[tuple[int, str], str] = {}

# === PATCH_BOT_LANG_PERSIST_CACHE_V1 ===
# گزارشِ ادمین (فدایی): بعد از هر ری‌استارتِ سرویس (که با ورکرهایِ جدا
# حالا بیشتر هم پیش می‌آید)، همین _CACHE/_REVERSE که فقط در حافظهٔ همین
# پروسه‌اند خالی می‌شدند — یعنی «بازسازیِ منو بعدِ تغییرِ زبان»ی که پایین‌تر
# هست فقط یک وصله‌کاریِ لحظه‌ای بود، نه رفعِ ریشه‌ای. حالا هر بار ترجمه‌ای
# تازه محاسبه شود، در همان جدولِ Setting هم ذخیره می‌شود؛ با اولین
# استفاده از هر ربات در این پروسه، از دیتابیس بازخوانی می‌شود — یعنی
# دیگر با ری‌استارت گم نمی‌شود.
_LOADED_BOTS: set[int] = set()
_PERSIST_KEY = "lang_cache_v1"
_PERSIST_MAX_ENTRIES = 500  # سقفِ تعدادِ متنِ ذخیره‌شده در دیتابیس، برایِ هر ربات


async def _ensure_loaded(bot_id: int) -> None:
    """اولین باری که این ربات در همین پروسه استفاده می‌شود، کشِ ذخیره‌شده
    در دیتابیس را در حافظه بارگذاری می‌کند — idempotent و ارزان (فقط
    یک‌بار برایِ هر ربات، در هر پروسه)."""
    if bot_id in _LOADED_BOTS:
        return
    _LOADED_BOTS.add(bot_id)
    try:
        from tenant import get_setting
        import json as _json
        raw = await get_setting(bot_id, _PERSIST_KEY, "")
        if not raw:
            return
        data = _json.loads(raw)
        for orig, out in data.items():
            _CACHE[(bot_id, orig)] = out
            _REVERSE[(bot_id, out.strip())] = orig
    except Exception:
        logger.exception("بارگذاریِ کشِ ذخیره‌شدهٔ ترجمه ناموفق بود")


async def _persist_new_entry(bot_id: int, orig: str, out: str) -> None:
    """یک ترجمهٔ تازه را در دیتابیس هم ذخیره می‌کند تا با ری‌استارت گم نشود.
    فقط همان دو دیکشنریِ درون‌حافظه‌ای را serialize می‌کند — چیزِ تازه‌ای
    محاسبه نمی‌شود، پس هزینه‌اش فقط یک نوشتنِ کوچکِ دیتابیسی است، آن‌هم
    فقط بارِ اولی که هر متن ترجمه می‌شود (نه هر بار که کاربر منو را می‌بیند)."""
    try:
        from tenant import get_setting, set_setting
        import json as _json
        raw = await get_setting(bot_id, _PERSIST_KEY, "")
        data = _json.loads(raw) if raw else {}
        data[orig] = out
        if len(data) > _PERSIST_MAX_ENTRIES:
            # قدیمی‌ترین‌ها را کنار بگذار (ترتیبِ درج در dict حفظ می‌شود)
            data = dict(list(data.items())[-_PERSIST_MAX_ENTRIES:])
        await set_setting(bot_id, _PERSIST_KEY, _json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.exception("ذخیره‌سازیِ کشِ ترجمه در دیتابیس ناموفق بود")

_TRANSLATE_SYSTEM_PROMPT = (
    "You translate Telegram bot UI text from Persian to English. "
    "Keep all emoji, HTML tags (like <b>, <code>), numbers, and line breaks exactly as they are — "
    "only translate the Persian words. Output ONLY the translated text, nothing else, no explanation, "
    "no quotes around it."
)


async def translate_text(bot_id: int, text: str, target_lang: str = "en") -> str:
    """متنِ فارسی رو به target_lang ترجمه می‌کنه. اگه کلید/ارائه‌دهنده تنظیم
    نشده بود یا خطا خورد، همون متنِ اصلی رو برمی‌گردونه (بدونِ کرش‌کردن یا
    خالی‌گذاشتنِ پیام).
    === PATCH_BOT_LANG_MULTI_PROVIDER_V1 ===
    از تنظیماتِ ارائه‌دهنده/کلیدِ همون smm_ai.py استفاده می‌کنه."""
    if not text or not text.strip():
        return text
    await _ensure_loaded(bot_id)  # === PATCH_BOT_LANG_PERSIST_CACHE_V1 ===
    key = (bot_id, text)
    if key in _CACHE:
        return _CACHE[key]
    try:
        from tenant.bots.virtual_service.smm_ai import ask_ai, _get_provider, _get_setting
        provider = await _get_provider(bot_id)
        api_key = (await _get_setting(bot_id, "smm_ai_api_key", "")).strip()
        if not api_key:
            return text
        # از ask_ai به‌عنوانِ یه «سؤال» با سیستم‌پرامپتِ ترجمه استفاده می‌کنیم —
        # چون ask_ai قبلاً سیستم‌پرامپتِ خودشو داره (پشتیبانیِ مشتری)، اینجا
        # به‌جاش خودِ محتوا رو با دستورالعملِ ترجمه می‌فرستیم.
        out = await ask_ai(provider, api_key, f"{_TRANSLATE_SYSTEM_PROMPT}\n\nText:\n{text}")
    except Exception:
        return text
    if not out:
        return text
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
        _REVERSE.clear()   # PATCH_BOT_LANG_REPLY_BUTTON_FIX_V1 — هم‌زمان با کشِ اصلی
    _CACHE[key] = out
    # === PATCH_BOT_LANG_REPLY_BUTTON_FIX_V1 ===
    _REVERSE[(bot_id, out.strip())] = text
    # === PATCH_BOT_LANG_PERSIST_CACHE_V1 ===
    await _persist_new_entry(bot_id, text, out)
    return out


async def original_label(bot_id: int, translated: str) -> str | None:
    """برچسبِ فارسیِ اصلیِ یک متنِ ترجمه‌شده را برمی‌گرداند (یا None اگر این
    متن هیچ‌وقت ترجمهٔ چیزی نبوده).

    === PATCH_BOT_LANG_REPLY_BUTTON_FIX_V1 ===
    دیسپچرِ دکمه‌هایِ کیبوردی از این استفاده می‌کند تا وقتی کاربر زبانش را
    رویِ انگلیسی گذاشته و متنِ انگلیسیِ دکمه را می‌فرستد، همچنان دکمهٔ
    درست پیدا شود.

    === PATCH_BOT_LANG_PERSIST_CACHE_V1 ===
    async شد تا قبل از چک‌کردنِ نگاشتِ درون‌حافظه‌ای، کشِ ذخیره‌شدهٔ همین
    ربات را از دیتابیس بارگذاری کند — وگرنه اولین پیامِ کاربر بعدِ هر
    ری‌استارت باز هم بی‌جواب می‌ماند."""
    if not translated:
        return None
    await _ensure_loaded(bot_id)
    return _REVERSE.get((bot_id, translated.strip()))


async def translate_kb_labels(bot_id: int, kb, target_lang: str = "en"):
    """یه InlineKeyboardMarkup/ReplyKeyboardMarkup می‌گیره و یه نسخهٔ جدید
    با برچسبِ دکمه‌هایِ ترجمه‌شده برمی‌گردونه (callback_data/url دست‌نخورده
    می‌مونه، فقط متنِ نمایشی عوض می‌شه)."""
    if kb is None:
        return None
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
    except Exception:
        return kb
    if isinstance(kb, InlineKeyboardMarkup):
        new_rows = []
        for row in kb.inline_keyboard:
            new_row = []
            for btn in row:
                label = await translate_text(bot_id, btn.text, target_lang)
                new_row.append(InlineKeyboardButton(
                    text=label, url=btn.url, callback_data=btn.callback_data,
                    web_app=btn.web_app, login_url=btn.login_url,
                    switch_inline_query=btn.switch_inline_query))
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    if isinstance(kb, ReplyKeyboardMarkup):
        new_rows = []
        for row in kb.keyboard:
            new_row = []
            for btn in row:
                label = await translate_text(bot_id, btn.text, target_lang)
                new_row.append(KeyboardButton(text=label))
            new_rows.append(new_row)
        return ReplyKeyboardMarkup(
            keyboard=new_rows, resize_keyboard=kb.resize_keyboard,
            is_persistent=getattr(kb, "is_persistent", None), one_time_keyboard=kb.one_time_keyboard)
    return kb


async def user_lang(bot_id: int, telegram_id: int) -> str:
    """قابلیتِ «تغییر زبان با هوش مصنوعی» حذف شد؛ همهٔ کاربران همیشه فارسی (پیش‌فرض)
    هستند، پس هیچ ترجمه‌ای انجام نمی‌شود و هیچ درخواستی به هوش مصنوعی نمی‌رود."""
    return "fa"


async def toggle_user_lang(bot_id: int, telegram_id: int) -> str:
    """بدون اثر (قابلیت حذف شده)."""
    return "fa"
