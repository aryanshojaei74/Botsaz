"""تست‌های رگرسیون برایِ زیرساختِ «ایموجیِ پرمیوم»:
  • premium_emoji_dynamic.py — منطقِ خالصِ پارس/جایگزینی + میان‌افزارِ
    عمومی (با providerِ فیک، بدونِ دیتابیس/شبکه)
  • tenant/premium_emoji_admin.py — providerِ سمتِ تننت (پلن‌گیت‌کردن)
  • platform_premium_emoji.py — providerِ سمتِ پلتفرم (تطبیقِ توکن)

همچنین یک تستِ صریح که وارد کردنِ platform_premium_emoji هیچ ماژولِ
tenant.* را import نمی‌کند (جداییِ سرویس‌ها).
"""
import sys
import types

import pytest
from aiogram.client.default import Default

from premium_emoji_dynamic import (
    plain_apply_map, hx_dynamic, _PremiumEmojiMiddleware,
    _utf16_len, register_emoji_map_provider, _set_button_premium_icon,
)

MAP = {
    "❤️": ("111", "❤️"),          # BMP + variation selector (طولِ ۲ در UTF-16)
    "🔥": ("222", "🔥"),          # خارج از BMP → surrogate pair (طولِ ۲ در UTF-16)
    "✅": ("333", "✅"),
}


# ─────────────────── plain_apply_map ───────────────────

def test_plain_apply_no_match_returns_unchanged():
    text, entities = plain_apply_map("سلام دنیا", MAP)
    assert text == "سلام دنیا"
    assert entities == []


def test_plain_apply_single_match_offset_and_length():
    text, entities = plain_apply_map("سلام ❤️ خوبی؟", MAP)
    assert text == "سلام ❤️ خوبی؟"
    assert len(entities) == 1
    ent = entities[0]
    assert ent.type == "custom_emoji"
    assert ent.custom_emoji_id == "111"
    prefix = "سلام "
    assert ent.offset == _utf16_len(prefix)
    assert ent.length == _utf16_len("❤️")


def test_plain_apply_astral_plane_emoji_surrogate_pair():
    text, entities = plain_apply_map("قبل 🔥 بعد", MAP)
    ent = entities[0]
    assert ent.custom_emoji_id == "222"
    assert ent.length == _utf16_len("🔥")
    assert ent.length == 2


def test_plain_apply_multiple_matches():
    text, entities = plain_apply_map("❤️ و ✅ و 🔥", MAP)
    ids = [e.custom_emoji_id for e in entities]
    assert ids == ["111", "333", "222"]


def test_plain_apply_longest_match_preferred():
    m = {"❤": ("aaa", "❤"), "❤️": ("bbb", "❤️")}
    text, entities = plain_apply_map("سلام ❤️", m)
    assert entities[0].custom_emoji_id == "bbb"


def test_plain_apply_empty_or_none_mapping():
    assert plain_apply_map("چیزی", {}) == ("چیزی", [])
    assert plain_apply_map("", MAP) == ("", [])


# ─────────────────── hx_dynamic ───────────────────

def test_hx_dynamic_no_match_returns_none_entities():
    text, entities = hx_dynamic("<b>سلام</b> دنیا", MAP)
    assert text == "<b>سلام</b> دنیا"
    assert entities is None


def test_hx_dynamic_preserves_bold_and_adds_custom_emoji():
    text, entities = hx_dynamic("<b>سلام</b> ❤️ خوبی؟", MAP)
    assert "<" not in text
    types_ = {e.type for e in entities}
    assert "bold" in types_
    assert "custom_emoji" in types_
    ce = next(e for e in entities if e.type == "custom_emoji")
    assert ce.custom_emoji_id == "111"


def test_hx_dynamic_link_and_emoji_together():
    text, entities = hx_dynamic('برو <a href="https://x.com">اینجا</a> ✅', MAP)
    link = next(e for e in entities if e.type == "text_link")
    assert link.url == "https://x.com"
    ce = next(e for e in entities if e.type == "custom_emoji")
    assert ce.custom_emoji_id == "333"


def test_hx_dynamic_empty_inputs():
    assert hx_dynamic("", MAP) == ("", None)
    assert hx_dynamic("متن", {}) == ("متن", None)


# ─────────────────── میان‌افزار (با providerِ فیکِ ثبت‌شده) ───────────────────

class _FakeBot:
    def __init__(self, token, default_parse_mode="HTML"):
        self.token = token
        self.default = types.SimpleNamespace(parse_mode=default_parse_mode)


class _FakeMethod:
    # وانمود می‌کند مثلِ SendMessage/EditMessageCaption واقعی این فیلدها را
    # دارد (میان‌افزار قبل از دست‌زدن، وجودِ این فیلدها را چک می‌کند).
    model_fields = {"text", "caption", "parse_mode", "entities", "caption_entities"}

    def __init__(self, text=None, caption=None, parse_mode=None, entities=None, caption_entities=None):
        self.text = text
        self.caption = caption
        self.parse_mode = parse_mode
        self.entities = entities
        self.caption_entities = caption_entities


@pytest.mark.asyncio
async def test_middleware_rewrites_html_text():
    mw = _PremiumEmojiMiddleware()
    token = "test-token-mw-1"

    async def fake_provider(t):
        return MAP if t == token else None

    register_emoji_map_provider(fake_provider)

    bot = _FakeBot(token)
    method = _FakeMethod(text="<b>سلام</b> ❤️", parse_mode=Default("parse_mode"))

    async def passthrough(b, m):
        return m

    result = await mw(passthrough, bot, method)
    assert result is method
    assert method.parse_mode is None
    assert method.entities
    assert any(e.type == "custom_emoji" and e.custom_emoji_id == "111" for e in method.entities)
    assert "<" not in method.text


@pytest.mark.asyncio
async def test_middleware_skips_when_no_provider_matches():
    mw = _PremiumEmojiMiddleware()
    bot = _FakeBot("no-such-token-xyz")
    method = _FakeMethod(text="سلام ❤️", parse_mode=None)
    orig_text = method.text

    async def passthrough(b, m):
        return m

    await mw(passthrough, bot, method)
    assert method.text == orig_text
    assert not method.entities


@pytest.mark.asyncio
async def test_middleware_respects_manual_entities():
    mw = _PremiumEmojiMiddleware()
    token = "test-token-mw-2"
    called = False

    async def fake_provider(t):
        nonlocal called
        called = True
        return MAP

    register_emoji_map_provider(fake_provider)
    bot = _FakeBot(token)
    method = _FakeMethod(text="سلام ❤️", parse_mode=None, entities=["already-set"])

    async def passthrough(b, m):
        return m

    await mw(passthrough, bot, method)
    assert method.entities == ["already-set"]
    assert not called  # باید قبل از پرسیدن از providerها زودتر برگرده


@pytest.mark.asyncio
async def test_middleware_ignores_answer_callback_query_toast():
    """AnswerCallbackQuery (متنِ کوچکِ toast/alert از cb.answer(...)) فیلدِ
    .text دارد ولی parse_mode/entities ندارد — میان‌افزار نباید دست بزند،
    وگرنه یک فیلدِ entities نامعتبر به بدنهٔ درخواستِ answerCallbackQuery
    اضافه می‌شود (باگی که واقعاً پیش آمد و اینجا قفل شده)."""
    from aiogram.methods.answer_callback_query import AnswerCallbackQuery

    mw = _PremiumEmojiMiddleware()
    token = "test-token-toast"

    async def fake_provider(t):
        return MAP if t == token else None

    register_emoji_map_provider(fake_provider)

    class FakeBotWithDefault:
        token = "test-token-toast"
        default = types.SimpleNamespace(parse_mode="HTML")

    method = AnswerCallbackQuery(callback_query_id="1", text="قابلیت ❤️ فعال شد")

    async def passthrough(b, m):
        return m

    result = await mw(passthrough, FakeBotWithDefault(), method)
    assert result.text == "قابلیت ❤️ فعال شد"
    dumped = result.model_dump(exclude_none=True)
    assert "entities" not in dumped


@pytest.mark.asyncio
async def test_middleware_skips_markdown_parse_mode():
    mw = _PremiumEmojiMiddleware()
    bot = _FakeBot("any-token")
    method = _FakeMethod(text="سلام ❤️", parse_mode="MarkdownV2")

    async def passthrough(b, m):
        return m

    await mw(passthrough, bot, method)
    assert method.text == "سلام ❤️"


# ─────────────────── providerِ سمتِ تننت (پلن‌گیت‌کردن) ───────────────────

@pytest.mark.asyncio
async def test_tenant_provider_blocks_free_and_growth(monkeypatch):
    from tenant.shared.premium_emoji_admin import _tenant_map_provider

    async def fake_bot_info(token):
        return (7, "free")

    monkeypatch.setattr("tenant.shared.premium_emoji_admin._bot_info_for_token", fake_bot_info)
    result = await _tenant_map_provider("tok")
    assert result is None

    async def fake_bot_info_growth(token):
        return (7, "growth")

    monkeypatch.setattr("tenant.shared.premium_emoji_admin._bot_info_for_token", fake_bot_info_growth)
    result = await _tenant_map_provider("tok")
    assert result is None


@pytest.mark.asyncio
async def test_tenant_provider_allows_pro_and_partner(monkeypatch):
    from tenant.shared.premium_emoji_admin import _tenant_map_provider

    async def fake_get_map(bot_id):
        return MAP

    monkeypatch.setattr("tenant.shared.premium_emoji_admin.get_premium_map", fake_get_map)

    for plan in ("pro", "partner"):
        async def fake_bot_info(token, _plan=plan):
            return (7, _plan)
        monkeypatch.setattr("tenant.shared.premium_emoji_admin._bot_info_for_token", fake_bot_info)
        result = await _tenant_map_provider("tok")
        assert result == MAP


@pytest.mark.asyncio
async def test_tenant_provider_none_when_bot_not_found(monkeypatch):
    from tenant.shared.premium_emoji_admin import _tenant_map_provider

    async def fake_bot_info(token):
        return None

    monkeypatch.setattr("tenant.shared.premium_emoji_admin._bot_info_for_token", fake_bot_info)
    result = await _tenant_map_provider("tok")
    assert result is None


# ─────────────────── providerِ سمتِ پلتفرم (تطبیقِ توکن) ───────────────────

@pytest.mark.asyncio
async def test_platform_provider_matches_only_platform_token(monkeypatch):
    import platform_premium_emoji as ppe

    monkeypatch.setattr(ppe, "PLATFORM_BOT_TOKEN", "1234:platform-token")

    async def fake_get_map():
        return MAP

    monkeypatch.setattr(ppe, "get_platform_premium_map", fake_get_map)

    assert await ppe._platform_map_provider("1234:platform-token") == MAP
    assert await ppe._platform_map_provider("other-token") is None


@pytest.mark.asyncio
async def test_platform_provider_none_when_no_platform_token_configured(monkeypatch):
    import platform_premium_emoji as ppe

    monkeypatch.setattr(ppe, "PLATFORM_BOT_TOKEN", "")
    assert await ppe._platform_map_provider("anything") is None


# ─────────────────── جداییِ سرویس‌ها ───────────────────

def test_platform_premium_emoji_does_not_pull_in_tenant_package():
    """importِ ماژولِ پلتفرم نباید هیچ ماژولِ tenant.* را بارگذاری کند —
    وگرنه main_platform.py کلِ tenant/__init__.py (۲۰هزار خط) را هم
    import می‌کرد. این تست در یک پروسسِ کاملاً تازه اجرا می‌شود چون
    تست‌هایِ قبلیِ همین فایل از قصد tenant.shared.premium_emoji_admin را
    import می‌کنند و sys.modules را برایِ بقیهٔ سشن آلوده می‌کنند."""
    import subprocess
    import sys as _sys
    code = (
        "import sys; "
        "import platform_premium_emoji; "
        "bad = [m for m in sys.modules if m == 'tenant' or m.startswith('tenant.')]; "
        "print(len(bad)); "
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([_sys.executable, "-c", code], cwd=".", capture_output=True, text=True)
    assert result.returncode == 0, f"tenant modules leaked in: {result.stdout} {result.stderr}"


# ─────────────────── آیکونِ پرمیوم رویِ دکمه‌ها ───────────────────
# === PATCH_PREMIUM_EMOJI_BUTTON_ICON_EMPTY_TEXT_FIX_V1 ===
# باگِ گزارش‌شده توسطِ کاربر: دکمه‌ای که متنش *فقط* همان ایموجیِ
# نگاشت‌شده بود (مثلاً یک دکمهٔ تنها با «🔻»)، بعدِ اعمالِ آیکونِ پرمیوم
# باز هم شکلِ معمولیِ ایموجی را نشان می‌داد — چون کدِ قدیمی وقتی متنِ
# باقی‌مانده بعدِ حذف خالی می‌شد، اصلاً btn.text را آپدیت نمی‌کرد.

from aiogram.types import InlineKeyboardButton  # noqa: E402


def test_button_icon_full_emoji_text_becomes_space_not_left_unchanged():
    """رگرسیونِ دقیقِ باگِ گزارش‌شده: دکمه‌ای که متنش کاملاً همان ایموجیِ
    نگاشت‌شده است (مثلاً فقط «🔻») باید متنش عوض شود (نه اینکه دست‌نخورده
    با همان ایموجیِ معمولی باقی بماند)، و icon_custom_emoji_id هم ست شود."""
    btn = InlineKeyboardButton(text="🔻", callback_data="x")
    ok = _set_button_premium_icon(btn, MAP_ARROWS, ["🔻"])
    assert ok is True
    assert btn.icon_custom_emoji_id == "999"
    # نکتهٔ اصلیِ باگ: متن دیگر نباید همان ایموجیِ معمولیِ قدیمی باشد
    assert btn.text != "🔻"
    assert btn.text.strip() == ""  # جای‌گزینِ خالی (فاصله) — فقط آیکون دیده می‌شود


def test_button_icon_partial_text_strips_emoji_normally():
    """وقتی ایموجی بخشی از متنِ دکمه است (نه کلِ آن)، رفتارِ قبلی حفظ
    می‌شود: بقیهٔ متن باقی می‌ماند، فقط خودِ ایموجی حذف می‌شود."""
    btn = InlineKeyboardButton(text="🔻 کاهش", callback_data="x")
    ok = _set_button_premium_icon(btn, MAP_ARROWS, ["🔻"])
    assert ok is True
    assert btn.icon_custom_emoji_id == "999"
    assert btn.text == "کاهش"


# === PATCH_PREMIUM_EMOJI_BUTTON_MULTI_MATCH_FIX_V1 ===
MAP_TWO = {"📱": ("111", "📱"), "✅": ("222", "✅")}


def test_button_icon_picks_leftmost_match_deterministically():
    """رگرسیونِ دقیقِ باگِ گزارش‌شده: دکمه‌ای با دو ایموجیِ نگاشت‌شدهٔ
    متفاوت («... 📱 ✅»)، باید همیشه همان ایموجیِ *چپ‌تر* (📱) را آیکون
    کند — نه هرکدام که در دیکشنری زودتر بیاید. کلیدها را عمداً به دو
    ترتیبِ متفاوت پاس می‌دهیم تا مطمئن شویم نتیجه به ترتیبِ ورودی
    وابسته نیست."""
    btn1 = InlineKeyboardButton(text="بنگلادش — 121,000 تومان 📱 ✅", callback_data="x")
    ok1 = _set_button_premium_icon(btn1, MAP_TWO, ["📱", "✅"])
    assert ok1 is True
    assert btn1.icon_custom_emoji_id == "111"  # همان 📱، چون چپ‌تر است

    btn2 = InlineKeyboardButton(text="بنگلادش — 121,000 تومان 📱 ✅", callback_data="x")
    ok2 = _set_button_premium_icon(btn2, MAP_TWO, ["✅", "📱"])  # ترتیبِ برعکس
    assert ok2 is True
    assert btn2.icon_custom_emoji_id == "111"  # نتیجه باید یکی بماند


def test_button_icon_removes_all_matched_emoji_not_just_chosen_one():
    """رگرسیونِ دقیقِ باگِ گزارش‌شده («دوتا شکلک نشون می‌ده»): وقتی دو
    ایموجیِ نگاشت‌شده در یک دکمه هستند ولی فقط یکی می‌تواند آیکون شود
    (تلگرام فقط یک اسلاتِ آیکون دارد)، آن یکیِ دیگر هم نباید به‌شکلِ
    معمولی در متن باقی بماند — وگرنه کنارِ آیکونِ پرمیوم، یک شکلِ
    معمولیِ دیگر هم دیده می‌شود که مثلِ یک آیکونِ دومِ ناقص به‌نظر
    می‌رسد."""
    btn = InlineKeyboardButton(text="بنگلادش — 121,000 تومان 📱 ✅", callback_data="x")
    ok = _set_button_premium_icon(btn, MAP_TWO, ["📱", "✅"])
    assert ok is True
    assert "📱" not in btn.text
    assert "✅" not in btn.text
    assert btn.text == "بنگلادش — 121,000 تومان"


MAP_ARROWS = {"🔻": ("999", "🔻")}
