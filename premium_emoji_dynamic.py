# -*- coding: utf-8 -*-
"""
premium_emoji_dynamic.py — هستهٔ مشترکِ «جایگزینیِ خودکارِ ایموجیِ
معمولی → پرمیوم» (PATCH_PLATFORM_PREMIUM_EMOJI_V1).

این فایل عمداً در ریشهٔ پروژه است، نه داخلِ tenant/ — چون هم سرویسِ
تننت (tenant/premium_emoji_admin.py) و هم سرویسِ پلتفرم
(platform_premium_emoji.py) باید بتوانند این منطق را import کنند. اگر
این کد داخلِ پکیجِ tenant می‌ماند، importِ آن از سمتِ پلتفرم یعنی
اجرایِ کاملِ tenant/__init__.py (۲۰هزار خط) داخلِ پروسسِ main_platform.py
— دقیقاً همان جفت‌شدنِ سرویس‌ها که architecture.md آن را ممنوع کرده.

این فایل شاملِ:
  • پارسِ HTML و جایگزینیِ ایموجی (نسخهٔ پویا/دیکشنری‌محورِ همان الگویِ
    premium_emoji.py، ولی برایِ استفادهٔ دوباره در چند سرویس)
  • یک میان‌افزارِ سراسریِ سطحِ aiogram Session که رویِ *همهٔ*
    درخواست‌هایِ خروجیِ Bot API (این پروژه) اجرا می‌شود
  • یک رجیستریِ ساده (`register_emoji_map_provider`) که هر سرویس
    (تننت، پلتفرم، …) نگاشتِ خودش را بدونِ وابستگیِ متقابل به آن وصل
    می‌کند.

استفاده:
    from premium_emoji_dynamic import register_emoji_map_provider

    async def my_provider(token: str):
        # اگر این token مالِ من نیست: None برگردان
        # وگرنه: {"❤️": ("123456", "❤️"), ...}
        ...

    register_emoji_map_provider(my_provider)
"""
from __future__ import annotations

import time
from html.parser import HTMLParser
from html import unescape as _unescape

from aiogram.types import MessageEntity

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


# ═══════════════ ابزارهایِ پایه ═══════════════

def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def utf16_slice(text: str, offset: int, length: int) -> str:
    """زیررشتهٔ متن را بر اساسِ آفستِ UTF-16 (همان چیزی که تلگرام در
    MessageEntity می‌فرستد) استخراج می‌کند — ایندکس‌گذاریِ پایتون برایِ
    کاراکترهایِ خارج از BMP (اکثرِ ایموجی‌ها) با UTF-16 یکی نیست."""
    raw = text.encode("utf-16-le")
    piece = raw[offset * 2: (offset + length) * 2]
    return piece.decode("utf-16-le", errors="ignore")


def extract_custom_emoji(msg) -> tuple[str, str] | None:
    """اگر یک پیامِ aiogram یک custom_emoji entity داشته باشد، اولین
    (premium_emoji_id, premium_display_char) را برمی‌گرداند؛ وگرنه None."""
    text = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            display = utf16_slice(text, ent.offset, ent.length) or "⭐"
            return ent.custom_emoji_id, display
    return None


def plain_apply_map(text: str, mapping: dict[str, tuple[str, str]]):
    """جایگزینیِ ایموجی‌هایِ معمولیِ نگاشت‌شده با entityِ custom_emoji در
    یک متنِ ساده (بدونِ تگِ HTML). خروجی: (plain_text, entities)."""
    if not mapping or not text:
        return text, []
    keys = sorted(mapping.keys(), key=len, reverse=True)
    out: list[str] = []
    entities: list[MessageEntity] = []
    utf16_pos = 0
    i = 0
    matched_any = False
    while i < len(text):
        matched = None
        for k in keys:
            if text.startswith(k, i):
                matched = k
                break
        if matched:
            premium_id, display = mapping[matched]
            out.append(display)
            length = _utf16_len(display)
            entities.append(MessageEntity(
                type="custom_emoji", offset=utf16_pos, length=length,
                custom_emoji_id=premium_id))
            utf16_pos += length
            i += len(matched)
            matched_any = True
        else:
            ch = text[i]
            out.append(ch)
            utf16_pos += _utf16_len(ch)
            i += 1
    if not matched_any:
        return text, []
    return "".join(out), entities


# ═══════════════ نسخهٔ HTML-آگاهِ جایگزینی (برایِ parse_mode="HTML") ═══════════════
# همان الگویِ premium_emoji.py (تگ‌هایِ فرمت‌بندی → entity) ولی با نگاشتِ
# پویا به‌جایِ دیکشنریِ ثابتِ توسعه‌دهنده.

_TAG_TO_ENTITY = {
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic",
    "u": "underline",
    "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "tg-spoiler": "spoiler", "spoiler": "spoiler",
}


class _DynHXParser(HTMLParser):
    def __init__(self, mapping: dict[str, tuple[str, str]]):
        super().__init__(convert_charrefs=True)
        self.mapping = mapping
        self.keys = sorted(mapping.keys(), key=len, reverse=True)
        self.out: list[str] = []
        self.utf16_pos = 0
        self.entities: list[MessageEntity] = []
        self._open: list[dict] = []
        self.matched_any = False
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        self._tg_emoji_depth = 0

    def _append_text(self, data: str):
        if not data:
            return
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        # داخلِ <tg-emoji> نباید نگاشتِ خودکار اجرا شود، وگرنه دو entityِ
        # روی‌هم‌افتاده custom_emoji ساخته می‌شود و تلگرام کلِ پیام را رد می‌کند.
        if self._tg_emoji_depth > 0:
            self.out.append(data)
            self.utf16_pos += _utf16_len(data)
            return
        i = 0
        while i < len(data):
            matched = None
            for k in self.keys:
                if data.startswith(k, i):
                    matched = k
                    break
            if matched:
                premium_id, display = self.mapping[matched]
                start = self.utf16_pos
                self.out.append(display)
                length = _utf16_len(display)
                self.utf16_pos += length
                self.entities.append(MessageEntity(
                    type="custom_emoji", offset=start, length=length,
                    custom_emoji_id=premium_id))
                self.matched_any = True
                i += len(matched)
                continue
            j = i
            while j < len(data) and not any(data.startswith(k, j) for k in self.keys):
                j += 1
            if j == i:
                j += 1
            chunk = data[i:j]
            self.out.append(chunk)
            self.utf16_pos += _utf16_len(chunk)
            i = j

    def handle_data(self, data):
        self._append_text(data)

    def handle_entityref(self, name):
        self._append_text(_unescape(f"&{name};"))

    def handle_charref(self, name):
        self._append_text(_unescape(f"&#{name};"))

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self._append_text("\n")
            return
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        # ایموجیِ پرمیومی که خودِ ادمین دستی توی متن گذاشته (aiogram آن را
        # به‌صورتِ <tg-emoji emoji-id="..."> در html_text می‌دهد) باید حفظ شود.
        if tag == "tg-emoji":
            cid = dict(attrs).get("emoji-id", "")
            self._open.append({"type": "custom_emoji", "start": self.utf16_pos, "cid": cid})
            self._tg_emoji_depth += 1
            self.matched_any = True
            return
        etype = _TAG_TO_ENTITY.get(tag)
        if tag == "a":
            href = dict(attrs).get("href", "")
            self._open.append({"type": "text_link", "start": self.utf16_pos, "url": href})
            return
        if etype:
            self._open.append({"type": etype, "start": self.utf16_pos})

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "br":
            return
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        if tag == "tg-emoji":
            for idx in range(len(self._open) - 1, -1, -1):
                frame = self._open[idx]
                if frame["type"] != "custom_emoji":
                    continue
                length = self.utf16_pos - frame["start"]
                if length > 0 and frame.get("cid"):
                    self.entities.append(MessageEntity(
                        type="custom_emoji", offset=frame["start"], length=length,
                        custom_emoji_id=frame["cid"]))
                del self._open[idx]
                self._tg_emoji_depth = max(0, self._tg_emoji_depth - 1)
                break
            return
        for idx in range(len(self._open) - 1, -1, -1):
            frame = self._open[idx]
            if (tag == "a" and frame["type"] == "text_link") or \
               (_TAG_TO_ENTITY.get(tag) == frame["type"]):
                start = frame["start"]
                length = self.utf16_pos - start
                if length > 0:
                    if frame["type"] == "text_link":
                        self.entities.append(MessageEntity(
                            type="text_link", offset=start, length=length, url=frame["url"]))
                    else:
                        self.entities.append(MessageEntity(
                            type=frame["type"], offset=start, length=length))
                del self._open[idx]
                break


def hx_dynamic(html_text: str, mapping: dict[str, tuple[str, str]]):
    """مثلِ premium_emoji.hx() ولی با نگاشتِ پویا: خروجیِ (plain_text,
    entities) که هم فرمت‌بندیِ HTML قبلی حفظ می‌شود و هم ایموجی‌هایِ
    نگاشت‌شده به custom_emoji تبدیل می‌شوند. اگر هیچ ایموجیِ نگاشت‌شده‌ای
    در متن نبود، (html_text, None) برمی‌گرداند تا فراخوان بفهمد نیازی
    به دست‌کاریِ parse_mode نیست."""
    if not html_text or not mapping:
        return html_text, None
    p = _DynHXParser(mapping)
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        logger.exception("پارسِ HTML برایِ نگاشتِ ایموجیِ پرمیوم ناموفق بود")
        return html_text, None
    if not p.matched_any:
        return html_text, None
    text = "".join(p.out)
    entities = sorted(p.entities, key=lambda e: (e.offset, -e.length))
    return text, entities


def _expand_variation_selector_aliases(mapping: dict | None) -> dict | None:
    """Support emoji keys with or without U+FE0F variation selector.

    Telegram/clients may deliver ✅ and ✅️ (or ❌ and ❌️) as different
    Unicode strings. Preserve the original mapping and add only missing
    aliases, so existing mappings keep their original precedence.
    """
    if not mapping:
        return mapping
    expanded = dict(mapping)
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            continue
        bare = key.replace("\ufe0f", "")
        if bare:
            expanded.setdefault(bare, value)
            expanded.setdefault(bare + "\ufe0f", value)
    return expanded


# ═══════════════ رجیستریِ providerها ═══════════════
#
# هر سرویس (tenant، platform، …) یک تابعِ async(token) -> dict|None ثبت
# می‌کند. میان‌افزار به‌ترتیب همه را امتحان می‌کند تا اولین نگاشتِ
# غیرخالی را پیدا کند. این‌طوری هیچ سرویسی مجبور نیست از پکیجِ سرویسِ
# دیگر import کند.

_PROVIDERS: list = []


def register_emoji_map_provider(fn) -> None:
    if fn not in _PROVIDERS:
        _PROVIDERS.append(fn)


# فلگِ «آیکونِ پرمیوم روی دکمه‌ها»: هر سرویس می‌تواند برایِ یک توکن False برگرداند تا دکمه‌ها دست نخورند
# (ایموجیِ ساده همان‌جایِ متنِ دکمه بماند). آیکونِ دکمه همیشه در گوشهٔ دکمه می‌نشیند، نه وسطِ متن.
_BTN_PROVIDERS: list = []


def register_button_flag_provider(fn) -> None:
    if fn not in _BTN_PROVIDERS:
        _BTN_PROVIDERS.append(fn)


async def _buttons_allowed(token: str) -> bool:
    for fn in _BTN_PROVIDERS:
        try:
            if (await fn(token)) is False:
                return False
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده در providerِ فلگِ ایموجیِ دکمه")
    return True


async def _map_for_token(token: str) -> dict | None:
    for fn in _PROVIDERS:
        try:
            mapping = await fn(token)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده در providerِ نگاشتِ ایموجیِ پرمیوم")
            continue
        if mapping:
            return _expand_variation_selector_aliases(mapping)
    return None


# ═══════════════ میان‌افزارِ سراسری: جایگزینیِ خودکار رویِ هر پیامِ خروجی ═══════════════
#
# به‌جایِ اینکه هر ماژولِ ارسال‌کنندهٔ پیام (در tenant/ یا platform_*.py)
# را تک‌تک ویرایش کنیم، یک میان‌افزارِ سطحِ aiogram Session ثبت می‌شود که
# خودکار روی *همهٔ* درخواست‌هایِ خروجیِ API (sendMessage، editMessageText،
# caption عکس/ویدیو/فایل و…) اجرا می‌شود؛ دقیقاً همان الگویِ «قلاب‌هایِ
# سراسری» که در architecture.md برایِ _purchase_alert توضیح داده شده.
#
# نکاتِ مهم:
#   • فقط رباتی که یکی از providerهایِ ثبت‌شده برایش نگاشتِ غیرخالی
#     برگرداند تحتِ تأثیر قرار می‌گیرد؛ بقیه بدونِ سربارِ اضافه رد
#     می‌شوند (فست‌پث).
#   • اگر خودِ فراخوان از قبل entities/caption_entities دستی داده باشد
#     (یعنی خودش دقیقاً می‌داند چه entityهایی می‌خواهد)، میان‌افزار
#     دست‌نمی‌زند.
#   • فقط parse_mode های None و HTML پشتیبانی می‌شود؛ Markdown/MarkdownV2
#     دست‌نخورده رد می‌شود (ریسکِ به‌هم‌ریختنِ سینتکسِ مارک‌داون).

from aiogram.client.session.middlewares.base import BaseRequestMiddleware


class _PremiumEmojiMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            await self._rewrite(bot, method)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده در میان‌افزارِ ایموجیِ پرمیوم")
        return await make_request(bot, method)

    async def _rewrite(self, bot, method) -> None:
        await self._rewrite_text(bot, method)
        # === PATCH_PREMIUM_EMOJI_BUTTON_ICON_V1 ===
        # جایگزینیِ خودکارِ ایموجیِ روی دکمه‌ها (کیبوردِ پایینِ صفحه و
        # دکمه‌هایِ شیشه‌ای/اینلاین): مستقل از رشتهٔ متن/کپشنِ پیام اجرا
        # می‌شود چون reply_markup روی متدهایی هم می‌آید که اصلاً text/caption
        # ندارند (مثلِ EditMessageReplyMarkup).
        await self._rewrite_buttons(bot, method)

    async def _rewrite_text(self, bot, method) -> None:
        field = entities_field = None
        if getattr(method, "text", None):
            field, entities_field = "text", "entities"
        elif getattr(method, "caption", None):
            field, entities_field = "caption", "caption_entities"
        if not field:
            return
        # برخی متدها (مثلاً AnswerCallbackQuery — متنِ کوچکِ toast/alert)
        # فیلدِ .text دارند ولی اصلاً parse_mode/entities ندارند؛ تلگرام در
        # آن‌ها فرمت‌بندی/entity را پشتیبانی نمی‌کند. بدونِ این چک، مقداری
        # entities به‌صورتِ فیلدِ اضافه (extra) رویِ آن مدل ست می‌شد که در
        # سریالایزیشن نهایی هم به بدنهٔ درخواست می‌رفت — درخواستی که
        # تلگرام آن فیلد را برایش تعریف نکرده.
        declared = getattr(type(method), "model_fields", {})
        if "parse_mode" not in declared or entities_field not in declared:
            return
        if getattr(method, entities_field, None):
            return  # فراخوان خودش entity دستی داده — دست نمی‌زنیم

        pm = getattr(method, "parse_mode", None)
        from aiogram.client.default import Default
        if isinstance(pm, Default):
            pm = getattr(bot.default, "parse_mode", None)
        if pm not in (None, "HTML"):
            return

        mapping = await _map_for_token(bot.token)
        if not mapping:
            return

        raw = getattr(method, field)
        if pm == "HTML":
            new_text, new_entities = hx_dynamic(raw, mapping)
            if new_entities is None:
                return
            setattr(method, "parse_mode", None)
        else:
            new_text, new_entities = plain_apply_map(raw, mapping)
            if not new_entities:
                return
        setattr(method, field, new_text)
        setattr(method, entities_field, new_entities)

    # === PATCH_PREMIUM_EMOJI_BUTTON_ICON_V1 ===
    async def _rewrite_buttons(self, bot, method) -> None:
        markup = getattr(method, "reply_markup", None)
        if markup is None:
            return
        # === PATCH_REPLY_LABEL_RESTORE_V1 === کیبوردِ پایینِ صفحه برداشته شد ⇒ یادداشتِ آن چت هم پاک شود
        if getattr(markup, "remove_keyboard", None):
            from reply_label_registry import forget
            await forget(bot.token, getattr(method, "chat_id", None))
            return
        rows = getattr(markup, "inline_keyboard", None)
        is_reply = False
        if rows is None:
            rows = getattr(markup, "keyboard", None)
            is_reply = rows is not None
        if not rows:
            return

        mapping = await _map_for_token(bot.token)
        if not mapping:
            return
        if not await _buttons_allowed(bot.token):
            return
        keys = sorted(mapping.keys(), key=len, reverse=True)

        touched = False
        # === PATCH_REPLY_LABEL_RESTORE_V1 ===
        # وقتی ایموجیِ متنِ دکمهٔ کیبوردِ پایینِ صفحه به آیکون تبدیل می‌شود، تلگرام موقعِ زدنِ دکمه فقط
        # متنِ بدونِ ایموجی را می‌فرستد؛ برایِ برگرداندنش «بدونِ‌ایموجی ← اصلی» را یادداشت می‌کنیم.
        pairs: dict[str, str] = {}
        ambiguous: set[str] = set()
        plain_texts: set[str] = set()
        for row in rows:
            for btn in row:
                orig = getattr(btn, "text", None)
                if _set_button_premium_icon(btn, mapping, keys):
                    touched = True
                    new = getattr(btn, "text", None)
                    if is_reply and orig and new and new != orig:
                        if new in pairs and pairs[new] != orig:
                            ambiguous.add(new)        # دو دکمهٔ متفاوت بعد از حذفِ ایموجی هم‌متن شدند
                        else:
                            pairs[new] = orig
                elif orig:
                    plain_texts.add(orig)
        if touched:
            logger.debug("ایموجیِ پرمیوم روی دکمه(ها) اعمال شد.")
        if is_reply:
            for k in ambiguous | plain_texts:
                pairs.pop(k, None)                    # ابهام یا دکمهٔ سادهٔ هم‌متن ⇒ بازیابی نکن
            pairs.pop(" ", None)                      # دکمه‌ای که فقط آیکون است
            from reply_label_registry import remember
            await remember(bot.token, getattr(method, "chat_id", None), pairs)


def _set_button_premium_icon(btn, mapping: dict, keys: list[str]) -> bool:
    """اگر متنِ دکمه حاویِ یکی از ایموجی‌هایِ نگاشت‌شده باشد، آیدیِ
    ایموجیِ پرمیومِ متناظر را روی خودِ دکمه (icon_custom_emoji_id) ست
    می‌کند — دقیقاً همان چیزی که تلگرام برایِ نمایشِ آیکونِ پرمیوم کنارِ
    دکمهٔ کیبورد/شیشه‌ای استفاده می‌کند. اگر دکمه از قبل یک آیکونِ
    دستی داشته باشد (مثلاً از رویِ ابزارِ سازندهٔ منو)، دست نمی‌زنیم —
    انتخابِ صریحِ ادمین اولویت دارد.

    === PATCH_PREMIUM_EMOJI_BUTTON_MULTI_MATCH_FIX_V1 ===
    باگِ گزارش‌شده: وقتی متنِ یک دکمه چند ایموجیِ نگاشت‌شدهٔ متفاوت
    داشت (مثلاً «... 📱 ✅» که هر دو نگاشت شده بودند)، کدِ قبلی اولین
    کلیدِ *keys* (که بر اساسِ طولِ رشته مرتب شده، نه موقعیتِ واقعی‌اش
    در متن) را انتخاب می‌کرد — یعنی انتخاب عملاً به ترتیبِ نامشخصِ
    دیکشنری بستگی داشت، نه به اینکه ادمین کدام ایموجی را واقعاً عوض
    کرده. نتیجه: گاهی ایموجی‌ای که ادمین *نمی‌خواست* عوض شود آیکون
    می‌گرفت، و ایموجیِ دیگر (که واقعاً برایش پرمیوم تنظیم شده بود)
    دست‌نخورده و به‌شکلِ معمولی کنارِ آیکون باقی می‌ماند — دقیقاً همان
    «دوتا شکلک» که ادمین گزارش داد.

    فیکس: اول *چپ‌ترین* تطبیق در خودِ متن انتخاب می‌شود (نه بر اساسِ
    ترتیبِ کلیدها) — این با شهودِ ادمین (اولین ایموجیِ دیده‌شده) یکی
    است. بعد، چون یک دکمه فقط یک اسلاتِ آیکون دارد، *همهٔ* ایموجی‌هایِ
    نگاشت‌شدهٔ دیگر هم از متن حذف می‌شوند — نه فقط همانی که آیکون شد —
    تا شکلِ معمولیِ باقی‌ماندهٔ آن‌ها کنارِ آیکونِ پرمیوم، مثلِ یک آیکونِ
    دومِ ناقص به نظر نرسد."""
    text = getattr(btn, "text", None)
    if not text:
        return False
    if getattr(btn, "icon_custom_emoji_id", None):
        return False
    declared = getattr(type(btn), "model_fields", {})
    if "icon_custom_emoji_id" not in declared:
        return False
    best_pos, best_key = None, None
    for k in keys:
        pos = text.find(k)
        if pos == -1:
            continue
        if best_pos is None or pos < best_pos or (pos == best_pos and len(k) > len(best_key)):
            best_pos, best_key = pos, k
    if best_key is None:
        return False
    premium_id, _display = mapping[best_key]
    # === PATCH_PREMIUM_EMOJI_BUTTON_ICON_DEDUPE_V1 / MULTI_MATCH_FIX_V1 ===
    # آیکونِ پرمیوم خودش کنارِ متنِ دکمه رندر می‌شود. اگر همان ایموجی (یا
    # هر ایموجیِ نگاشت‌شدهٔ دیگری) داخلِ متن باقی بماند، هم شکلِ معمولی
    # هم آیکونِ پرمیوم با هم دیده می‌شوند — برایِ همین نسخهٔ متنیِ *همهٔ*
    # کلیدهایِ نگاشت‌شده از متنِ دکمه حذف می‌شوند، نه فقط همانی که به
    # آیکون تبدیل شد.
    new_text = text
    for k in keys:
        new_text = new_text.replace(k, "")
    new_text = " ".join(new_text.split())
    try:
        btn.icon_custom_emoji_id = premium_id
        # === PATCH_PREMIUM_EMOJI_BUTTON_ICON_EMPTY_TEXT_FIX_V1 ===
        # باگِ گزارش‌شده: وقتی کلِ متنِ دکمه فقط همان ایموجیِ نگاشت‌شده بود
        # (مثلاً دکمه‌ای که فقط «🔻» یا «🔺» است)، new_text بعدِ حذف خالی
        # می‌شد و شرطِ `if new_text` رد می‌شد — یعنی icon_custom_emoji_id
        # ست می‌شد ولی متنِ قدیمی (همان ایموجیِ معمولی) دست‌نخورده باقی
        # می‌ماند، پس کاربر همچنان شکلِ معمولی را می‌دید، نه آیکونِ پرمیوم.
        # حالا در این حالت، متن را با یک فاصله (نه خالی، که تلگرام رد
        # می‌کند) جایگزین می‌کنیم تا فقط آیکونِ پرمیوم دیده شود.
        btn.text = new_text if new_text else " "
    except Exception:
        return False
    return True


def install_premium_emoji_middleware() -> None:
    """میان‌افزار را یک‌بار، سراسری، رویِ کلاسِ پایهٔ سشنِ aiogram نصب
    می‌کند — یعنی هر Bot(...) ای که در این پروسس ساخته می‌شود (بدونِ
    نیاز به ویرایشِ فایل‌هایِ main_tenant.py/main_platform.py/bot_utils.py)
    خودکار این میان‌افزار را رویِ سشنِ خودش خواهد داشت. هر پروسس
    (تننت/پلتفرم) این را جدا و مستقل صدا می‌زند (idempotent است)."""
    from aiogram.client.session.base import BaseSession
    if getattr(BaseSession, "_pemj_middleware_installed", False):
        return
    _orig_init = BaseSession.__init__

    def _patched_init(self, *a, **kw):
        _orig_init(self, *a, **kw)
        self.middleware(_PremiumEmojiMiddleware())

    BaseSession.__init__ = _patched_init
    BaseSession._pemj_middleware_installed = True
    logger.info("میان‌افزارِ ایموجیِ پرمیوم رویِ سشن‌هایِ aiogram نصب شد.")


install_premium_emoji_middleware()
