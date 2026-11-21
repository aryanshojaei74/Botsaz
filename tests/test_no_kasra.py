"""PATCH_NO_KASRA_V1: کسره (ِ) در هیچ پیامِ خروجی نباشد؛ ولی کیبوردِ پایین و entityهایِ دستی خراب نشوند."""
import pytest
from aiogram.methods import AnswerCallbackQuery, AnswerInlineQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent,
                           KeyboardButton, MessageEntity, ReplyKeyboardMarkup)

import no_kasra as NK
import premium_emoji_dynamic as P

K = "ِ"


def test_clean_only_removes_the_kasra():
    assert NK.clean(f"مدیریت{K} پنل‌های{K} متصل") == "مدیریت پنل‌های متصل"
    assert NK.clean("بدون کسره") == "بدون کسره" and NK.clean(None) is None
    assert "َ" in NK.clean("فتحهَ و کسرهِ")                      # فتحه می‌ماند؛ فقط کسره برداشته می‌شود


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"مدیریت{K} پنل", callback_data="x")]])


def test_text_caption_alert_and_inline_buttons_are_cleaned():
    m = SendMessage(chat_id=1, text=f"پنل‌های{K} متصل", reply_markup=_kb())
    NK.strip_method(m)
    assert K not in m.text and K not in m.reply_markup.inline_keyboard[0][0].text and m.reply_markup.inline_keyboard[0][0].callback_data == "x"
    p = SendPhoto(chat_id=1, photo="x", caption=f"توضیح{K}")
    NK.strip_method(p)
    assert p.caption == "توضیح"
    a = AnswerCallbackQuery(callback_query_id="1", text=f"ذخیره{K} شد")
    NK.strip_method(a)
    assert a.text == "ذخیره شد"
    e = EditMessageText(inline_message_id="i", text=f"صفحه{K}", reply_markup=_kb())
    NK.strip_method(e)
    assert K not in e.text


def test_reply_keyboard_and_manual_entities_are_left_alone():
    rk = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"📦 مدیریت{K} محصولات")]], resize_keyboard=True)
    m = SendMessage(chat_id=1, text="x", reply_markup=rk)
    NK.strip_method(m)
    assert K in m.reply_markup.keyboard[0][0].text                           # هندلرها این متن را عیناً مقایسه می‌کنند
    ent = SendMessage(chat_id=1, text=f"بولد{K} شد", entities=[MessageEntity(type="bold", offset=0, length=4)])
    NK.strip_method(ent)
    assert K in ent.text                                                     # offset جابه‌جا نشود


def test_inline_query_results_are_cleaned():
    art = InlineQueryResultArticle(id="1", title=f"دوز{K}", description=f"بازی{K}",
                                   input_message_content=InputTextMessageContent(message_text=f"میزبان{K}"), reply_markup=_kb())
    m = AnswerInlineQuery(inline_query_id="q", results=[art])
    NK.strip_method(m)
    r = m.results[0]
    assert (r.title, r.description, r.input_message_content.message_text) == ("دوز", "بازی", "میزبان")
    assert K not in r.reply_markup.inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_middleware_strips_before_premium_conversion_and_sends_clean_text(monkeypatch):
    monkeypatch.setenv("NO_KASRA", "1")
    seen = {}

    async def make_request(bot, method):
        seen["m"] = method
        return "ok"
    from types import SimpleNamespace as NS
    m = SendMessage(chat_id=1, text=f"سلام{K} دنیا", parse_mode="HTML", reply_markup=_kb())
    assert await P._PremiumEmojiMiddleware()(make_request, NS(token="t", default=NS(parse_mode="HTML")), m) == "ok"
    assert K not in seen["m"].text and K not in seen["m"].reply_markup.inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_kill_switch_keeps_text_untouched(monkeypatch):
    monkeypatch.setenv("NO_KASRA", "0")
    seen = {}

    async def make_request(bot, method):
        seen["m"] = method
        return "ok"
    from types import SimpleNamespace as NS
    m = SendMessage(chat_id=1, text=f"سلام{K}", parse_mode="HTML")
    await P._PremiumEmojiMiddleware()(make_request, NS(token="t", default=NS(parse_mode="HTML")), m)
    assert K in seen["m"].text
