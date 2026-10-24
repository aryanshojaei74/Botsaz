# -*- coding: utf-8 -*-
"""
PATCH_PREMIUM_EMOJI_HX_V1
==========================
تبدیلِ متنِ HTML + شکلک‌های معمولی به (plain_text, entities) برای ارسال با
aiogram، طوری که:

  1) همون فرمت‌بندیِ HTML قبلی (b/i/u/s/code/pre/a/blockquote/spoiler/tg-spoiler)
     دقیقاً مثل parse_mode="HTML" حفظ می‌شه — چون تلگرام اجازه نمی‌ده parse_mode
     و entities با هم داده بشن، این تابع خودش HTML رو پارس و به entity تبدیل می‌کنه.
  2) هر شکلکِ شناخته‌شده‌ای که توی PREMIUM_EMOJI_IDS مقدارِ غیرخالی داشته باشه،
     به یک custom_emoji entity تبدیل می‌شه (یعنی ایموجی پرمیومِ متحرک).
  3) تا وقتی یک شکلک هنوز ID نداره (مقدارش "" هست)، همون شکلکِ معمولی/استاتیک
     بدون تغییر رفتار قبلی نمایش داده می‌شه — پس پر کردنِ تدریجیِ ID‌ها کاملاً امنه
     و نیازی به تغییرِ همزمانِ همه‌چیز نیست.

نحوهٔ استفاده (جایگزینِ الگوی قدیمی):
    قبلاً:
        await msg.answer("✅ <b>سلام</b>", parse_mode="HTML", reply_markup=kb)
    حالا:
        _t, _e = hx("✅ <b>سلام</b>")
        await msg.answer(_t, entities=_e, reply_markup=kb)
    یا با هلپرِ کوتاه:
        await hx_answer(msg, "✅ <b>سلام</b>", reply_markup=kb)

پر کردنِ ID شکلک پرمیوم:
    فقط مقدارِ رشته‌ایِ جلوی هر شکلک رو توی PREMIUM_EMOJI_IDS پر کن، مثلاً:
        "🛡": "5368324170671202286",
    بقیهٔ کد نیازی به تغییر نداره.
"""
from __future__ import annotations

from html.parser import HTMLParser
from html import unescape as _unescape

from aiogram.types import MessageEntity

# ---------------------------------------------------------------------------
# نگاشتِ شکلک -> custom_emoji_id (پرمیوم، متحرک). مقدارها رو خودت پر کن؛
# تا وقتی خالیه، همون شکلکِ معمولی بدون تغییر رفتار نمایش داده می‌شه.
# ---------------------------------------------------------------------------
PREMIUM_EMOJI_IDS: dict[str, str] = {
    "✅": "5427009714745517609",  # WHITE_HEAVY_CHECK_MARK
    "️": "",  # VARIATION_SELECTOR-16 (نادیده گرفته می‌شود)
    "❌": "5210952531676504517",  # CROSS_MARK
    "⬅": "5253997076169115797",  # LEFTWARDS_BLACK_ARROW
    "🟢": "",  # LARGE_GREEN_CIRCLE
    "🔴": "",  # LARGE_RED_CIRCLE
    "⚠": "",  # WARNING_SIGN
    "🗑": "",  # WASTEBASKET
    "🤖": "5352899869369446268",  # ROBOT_FACE
    "💰": "5255981634527704754",  # MONEY_BAG
    "🔒": "",  # LOCK
    "🏠": "",  # HOUSE_BUILDING
    "🔗": "5375129357373165375",  # LINK_SYMBOL
    "📝": "",  # MEMO
    "✏": "",  # PENCIL
    "🎁": "",  # WRAPPED_PRESENT
    "👤": "5373012449597335010",  # BUST_IN_SILHOUETTE
    "👇": "",  # WHITE_DOWN_POINTING_BACKHAND_INDEX
    "→": "",  # RIGHTWARDS_ARROW
    "⭐": "5226928895189598791",  # WHITE_MEDIUM_STAR
    "🎫": "5203960621271363538",  # TICKET
    "💬": "5443038326535759644",  # SPEECH_BALLOON
    "🔄": "",  # ANTICLOCKWISE_DOWNWARDS_AND_UPWARDS_OPEN_CIRCLE_ARROWS
    "💳": "",  # CREDIT_CARD
    "🌐": "",  # GLOBE_WITH_MERIDIANS
    "👥": "5372926953978341366",  # BUSTS_IN_SILHOUETTE
    "🛒": "5312361253610475399",  # SHOPPING_TROLLEY
    "🗂": "",  # CARD_INDEX_DIVIDERS
    "➕": "",  # HEAVY_PLUS_SIGN
    "🗓": "5274055917766202507",  # SPIRAL_CALENDAR_PAD
    "📅": "5249393975034407291",  # CALENDAR
    "🔵": "",  # LARGE_BLUE_CIRCLE
    "💵": "",  # BANKNOTE_WITH_DOLLAR_SIGN
    "⚙": "",  # GEAR
    "🪙": "",  # COIN
    "🗄": "",  # FILE_CABINET
    "📊": "",  # BAR_CHART
    "🚫": "",  # NO_ENTRY_SIGN
    "💡": "5422439311196834318",  # ELECTRIC_LIGHT_BULB
    "📜": "",  # SCROLL
    "📁": "",  # FILE_FOLDER
    "⛔": "",  # NO_ENTRY
    "🎨": "",  # ARTIST_PALETTE
    "←": "",  # LEFTWARDS_ARROW
    "🏆": "",  # TROPHY
    "📦": "",  # PACKAGE
    "💎": "",  # GEM_STONE
    "📋": "",  # CLIPBOARD
    "✨": "",  # SPARKLES
    "✍": "",  # WRITING_HAND
    "♻": "",  # BLACK_UNIVERSAL_RECYCLING_SYMBOL
    "📢": "",  # PUBLIC_ADDRESS_LOUDSPEAKER
    "😎": "",  # SMILING_FACE_WITH_SUNGLASSES
    "⬜": "",  # WHITE_MEDIUM_SQUARE
    "🖼": "",  # FRAME_WITH_PICTURE
    "📌": "",  # ROUND_PUSHPIN
    "🛠": "",  # HAMMER_AND_WRENCH
    "📨": "",  # INCOMING_ENVELOPE
    "🟦": "",  # BLUE_SQUARE
    "🔍": "",  # LEFT_POINTING_MAGNIFYING_GLASS
    "🔁": "",  # CLOCKWISE_RIGHTWARDS_AND_LEFTWARDS_OPEN_CIRCLE_ARROWS
    "⚡": "",  # HIGH_VOLTAGE_SIGN
    "🛡": "",  # SHIELD
    "🎯": "",  # DIRECT_HIT
    "🟠": "",  # ORANGE_CIRCLE
    "📡": "",  # SATELLITE_ANTENNA
    "🎉": "",  # PARTY_POPPER
    "➡": "",  # BLACK_RIGHTWARDS_ARROW
    "💱": "",  # CURRENCY_EXCHANGE
    "🔑": "",  # KEY
    "🏅": "",  # SPORTS_MEDAL
    "🏛": "",  # CLASSICAL_BUILDING
    "👁": "",  # EYE
    "🚀": "",  # ROCKET
    "🏷": "",  # LABEL
    "🔎": "",  # RIGHT_POINTING_MAGNIFYING_GLASS
    "👑": "",  # CROWN
    "✯": "",  # PINWHEEL_STAR
    "🔧": "",  # WRENCH
    "📷": "",  # CAMERA
    "🎟": "",  # ADMISSION_TICKETS
    "➖": "",  # HEAVY_MINUS_SIGN
    "☑": "",  # BALLOT_BOX_WITH_CHECK
    "🟣": "",  # PURPLE_CIRCLE
    "🟡": "",  # YELLOW_CIRCLE
    "📱": "",  # MOBILE_PHONE
    "🧑": "",  # ADULT
    "💸": "5197434882321567830",  # MONEY_WITH_WINGS
    "↩": "",  # LEFTWARDS_ARROW_WITH_HOOK
    "🙏": "",  # PERSON_WITH_FOLDED_HANDS
    "📣": "",  # CHEERING_MEGAPHONE
    "🔓": "",  # OPEN_LOCK
    "🔙": "",  # BACK_WITH_LEFTWARDS_ARROW_ABOVE
    "❤": "",  # HEAVY_BLACK_HEART
    "👍": "",  # THUMBS_UP_SIGN
    "🧩": "",  # PUZZLE_PIECE
    "🛰": "",  # SATELLITE
    "🟤": "",  # BROWN_CIRCLE
    "🎞": "",  # FILM_FRAMES
    "🏁": "",  # CHEQUERED_FLAG
    "↕": "",  # UP_DOWN_ARROW
    "🔘": "",  # RADIO_BUTTON
    "💲": "",  # HEAVY_DOLLAR_SIGN
    "🔐": "",  # CLOSED_LOCK_WITH_KEY
    "↔": "",  # LEFT_RIGHT_ARROW
    "🏦": "",  # BANK
    "📸": "",  # CAMERA_WITH_FLASH
    "📩": "",  # ENVELOPE_WITH_DOWNWARDS_ARROW_ABOVE
    "🩺": "",  # STETHOSCOPE
    "🚪": "",  # DOOR
    "💤": "",  # SLEEPING_SYMBOL
    "🔌": "",  # ELECTRIC_PLUG
    "🐛": "5314507981344220324",  # BUG
    "🔔": "",  # BELL
    "🤝": "",  # HANDSHAKE
    "💠": "",  # DIAMOND_SHAPE_WITH_A_DOT_INSIDE
    "⬆": "",  # UPWARDS_BLACK_ARROW
    "🔖": "",  # BOOKMARK
    "🔻": "",  # DOWN-POINTING_RED_TRIANGLE
    "📤": "",  # OUTBOX_TRAY
    "🥇": "",  # FIRST_PLACE_MEDAL
    "🥈": "",  # SECOND_PLACE_MEDAL
    "🥉": "",  # THIRD_PLACE_MEDAL
    "🕒": "",  # CLOCK_FACE_THREE_OCLOCK
    "📛": "",  # NAME_BADGE
    "🚦": "",  # HORIZONTAL_TRAFFIC_LIGHT
    "🧾": "",  # RECEIPT
    "✉": "",  # ENVELOPE
    "💾": "",  # FLOPPY_DISK
    "🛑": "",  # OCTAGONAL_SIGN
    "🎧": "",  # HEADPHONE
    "🛍": "",  # SHOPPING_BAGS
    "🎛": "",  # CONTROL_KNOBS
    "⬇": "",  # DOWNWARDS_BLACK_ARROW
    "⚪": "",  # MEDIUM_WHITE_CIRCLE
    "✓": "",  # CHECK_MARK
    "😄": "",  # SMILING_FACE_WITH_OPEN_MOUTH_AND_SMILING_EYES
    "😉": "",  # WINKING_FACE
    "🏗": "",  # BUILDING_CONSTRUCTION
    "📮": "",  # POSTBOX
    "🧪": "",  # TEST_TUBE
    "📥": "",  # INBOX_TRAY
    "🔺": "",  # UP-POINTING_RED_TRIANGLE
    "🔷": "",  # LARGE_BLUE_DIAMOND
    "💼": "",  # BRIEFCASE
    "🔥": "",  # FIRE
    "🎗": "",  # REMINDER_RIBBON
    "😶": "",  # FACE_WITHOUT_MOUTH
    "📍": "",  # ROUND_PUSHPIN (round variant)
    "❔": "",  # WHITE_QUESTION_MARK_ORNAMENT
    "🕓": "",  # CLOCK_FACE_FOUR_OCLOCK
    "↳": "",  # DOWNWARDS_ARROW_WITH_TIP_RIGHTWARDS
    "♾": "",  # INFINITY
    "📈": "",  # CHART_WITH_UPWARDS_TREND
    "🚨": "",  # POLICE_CARS_REVOLVING_LIGHT
    "🔢": "",  # INPUT_SYMBOL_FOR_NUMBERS
    "📬": "",  # OPEN_MAILBOX_WITH_RAISED_FLAG
    "👀": "",  # EYES
    "👌": "",  # OK_HAND_SIGN
    "🔕": "",  # BELL_WITH_CANCELLATION_STROKE
    "✔": "",  # HEAVY_CHECK_MARK
    "🔤": "",  # INPUT_SYMBOL_FOR_LATIN_LETTERS
    "👋": "",  # WAVING_HAND_SIGN
    "📖": "",  # OPEN_BOOK
}

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


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class _HXParser(HTMLParser):
    """پارسرِ سبکِ HTML که هم‌زمان entityهای فرمت‌بندی و entityِ شکلکِ پرمیوم را
    (بر اساسِ PREMIUM_EMOJI_IDS) روی متنِ خروجی می‌سازد."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.utf16_pos = 0
        self.entities: list[MessageEntity] = []
        self._open: list[dict] = []  # stack: {type, start, extra}
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        self._tg_emoji_depth = 0

    def _append_text(self, data: str):
        if not data:
            return
        # === PATCH_PREMIUM_EMOJI_TG_EMOJI_TAG_V1 ===
        if self._tg_emoji_depth > 0:
            self.out.append(data)
            self.utf16_pos += _utf16_len(data)
            return
        i = 0
        while i < len(data):
            ch = data[i]
            emoji_id = PREMIUM_EMOJI_IDS.get(ch)
            if emoji_id:
                start = self.utf16_pos
                self.out.append(ch)
                length = _utf16_len(ch)
                self.utf16_pos += length
                self.entities.append(MessageEntity(
                    type="custom_emoji", offset=start, length=length,
                    custom_emoji_id=emoji_id,
                ))
                i += 1
                continue
            # بلوکِ متنِ معمولی تا کاراکترِ شکلکِ بعدی را یکجا اضافه کن
            j = i
            while j < len(data) and not PREMIUM_EMOJI_IDS.get(data[j]):
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
        if tag == "tg-emoji":
            cid = dict(attrs).get("emoji-id", "")
            self._open.append({"type": "custom_emoji", "start": self.utf16_pos, "cid": cid})
            self._tg_emoji_depth += 1
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
                            type="text_link", offset=start, length=length,
                            url=frame["url"],
                        ))
                    else:
                        self.entities.append(MessageEntity(
                            type=frame["type"], offset=start, length=length,
                        ))
                del self._open[idx]
                break


def hx(html_text: str) -> tuple[str, list[MessageEntity]]:
    """(plain_text, entities) رو از یک رشتهٔ HTML مثلِ قبل می‌سازد؛ فرمت‌بندیِ
    b/i/u/s/code/pre/a/blockquote/spoiler حفظ می‌شود و شکلک‌هایی که توی
    PREMIUM_EMOJI_IDS مقدار دارند به custom_emoji تبدیل می‌شوند."""
    if html_text is None:
        return "", []
    p = _HXParser()
    p.feed(html_text)
    p.close()
    text = "".join(p.out)
    entities = sorted(p.entities, key=lambda e: (e.offset, -e.length))
    return text, entities


async def hx_answer(msg, html_text: str, **kwargs):
    """میان‌بر: جایگزینِ ``msg.answer(text, parse_mode="HTML", ...)``."""
    text, entities = hx(html_text)
    kwargs.pop("parse_mode", None)
    return await msg.answer(text, entities=entities, **kwargs)


async def hx_edit_text(msg, html_text: str, **kwargs):
    """میان‌بر: جایگزینِ ``msg.edit_text(text, parse_mode="HTML", ...)``."""
    text, entities = hx(html_text)
    kwargs.pop("parse_mode", None)
    return await msg.edit_text(text, entities=entities, **kwargs)


def hx_button(text_html: str, **kwargs):
    """میان‌بر برای InlineKeyboardButton: اگر متنِ دکمه با یک شکلکِ دارایِ ID
    شروع بشه، اون شکلک به‌عنوانِ icon_custom_emoji_id ست می‌شه و از ابتدایِ
    متن حذف می‌گردد (تلگرام آیکونِ دکمه رو جدا از متن نشون می‌ده)."""
    from aiogram.types import InlineKeyboardButton
    plain, _ = hx(text_html)
    icon_id = None
    if plain:
        first = plain[0]
        emoji_id = PREMIUM_EMOJI_IDS.get(first)
        if emoji_id:
            icon_id = emoji_id
            plain = plain[1:].lstrip()
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
    return InlineKeyboardButton(text=plain, **kwargs)
