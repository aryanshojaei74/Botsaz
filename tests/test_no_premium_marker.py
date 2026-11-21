import pytest
from types import SimpleNamespace as NS
import premium_emoji_dynamic as P


@pytest.mark.asyncio
async def test_marker_skips_premium_and_is_stripped(monkeypatch):
    called = []

    async def _m(token):
        called.append(token)
        return {"🐟": ("1", "🐟")}
    monkeypatch.setattr(P, "_map_for_token", _m)
    from aiogram.methods import SendMessage
    m = SendMessage(chat_id=1, text="🐟🍚" + P.NO_PREMIUM, parse_mode="HTML")
    await P._PremiumEmojiMiddleware()._rewrite_text(NS(token="t", default=NS(parse_mode="HTML")), m)
    assert m.text == "🐟🍚" and not called and not m.entities


@pytest.mark.asyncio
async def test_inline_message_edits_never_get_premium_icons(monkeypatch):
    """PATCH_INLINE_NO_PREMIUM_V1 (سینا): صفحهٔ بازیِ اینلاین (دوز) با ⬜❌⭕ بدونِ آیکونِ پرمیوم برود؛ ویرایشِ عادی هنوز تبدیل می‌شود."""
    async def _m(token):
        return {"⬜": ("9", "⬜"), "❌": ("8", "❌")}
    monkeypatch.setattr(P, "_map_for_token", _m)
    monkeypatch.setattr(P, "_buttons_allowed", lambda t: _true(), raising=False)
    from aiogram.methods import EditMessageText
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def kb():
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬜", callback_data="twg:x:1:0"),
                                                     InlineKeyboardButton(text="❌", callback_data="twg:x:1:1")]])
    bot = NS(token="t", default=NS(parse_mode="HTML"))

    async def passthrough(b, m):
        return m
    inl = EditMessageText(inline_message_id="IM", text="⬜❌ بازی", parse_mode="HTML", reply_markup=kb())
    await P._PremiumEmojiMiddleware()(passthrough, bot, inl)
    row = inl.reply_markup.inline_keyboard[0]
    assert [b.text for b in row] == ["⬜", "❌"] and not any(getattr(b, "icon_custom_emoji_id", None) for b in row)
    assert inl.text == "⬜❌ بازی" and not inl.entities

    norm = EditMessageText(chat_id=1, message_id=2, text="⬜❌ بازی", parse_mode="HTML", reply_markup=kb())
    await P._PremiumEmojiMiddleware()(passthrough, bot, norm)
    assert any(getattr(b, "icon_custom_emoji_id", None) for b in norm.reply_markup.inline_keyboard[0])


async def _true():
    return True
