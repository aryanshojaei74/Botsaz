"""PATCH_WEBAPP_TME_LINK_V1 (Weirdnet) — دکمهٔ مینی‌اپ با لینکِ t.me در منو نمی‌افتد (به دکمهٔ متنی تبدیل می‌شود)."""
import json
from types import SimpleNamespace

import tenant


def _btn(url, action="web_app"):
    return SimpleNamespace(id=1, label="🧩 مینی‌اپ", action=action, config=json.dumps({"webapp_url": url}), icon_emoji_id=None)


def test_tme_link_becomes_plain_text_keyboard_button():
    b = _btn("https://t.me/Weirdnet_bot?startapp")
    assert tenant._webapp_url(b) is None                          # برایِ کیبوردِ پایین web_app نمی‌شود
    assert tenant._webapp_raw_url(b) == "https://t.me/Weirdnet_bot?startapp"
    kb = tenant._kbtn_for(b, None)
    assert kb.text == "🧩 مینی‌اپ" and kb.web_app is None          # دکمه می‌ماند (متنی)
    assert tenant._ibtn_for(b, None).callback_data == "mbtn:1"    # منویِ شیشه‌ای: callback ⇒ هندلر دکمهٔ لینک می‌فرستد


def test_real_https_webapp_unchanged():
    b = _btn("https://shop.example.com/app")
    assert tenant._webapp_url(b) == "https://shop.example.com/app"
    assert tenant._kbtn_for(b, None).web_app.url == "https://shop.example.com/app"
    assert tenant._is_tg_link("https://telegram.me/x") and not tenant._is_tg_link("https://example.com/t.me")


def test_inline_flag_makes_keyboard_button_plain_but_glass_button_stays_direct_webapp():
    """PATCH_WEBAPP_INLINE_AUTH_V1 (Weirdnet): وب‌اپِ کیبوردِ پایین initData ندارد ⇒ با پرچمِ webapp_inline دکمهٔ کیبورد متنی می‌شود
    و با زدنش دکمهٔ شیشه‌ایِ web_app می‌آید؛ در منویِ شیشه‌ایِ اصلی (که خودش initData دارد) همچنان مستقیم باز می‌شود."""
    url = "https://app.example.com/"
    b = SimpleNamespace(id=2, label="مینی‌اپ", action="web_app", icon_emoji_id=None,
                        config=json.dumps({"webapp_url": url, "webapp_inline": True}))
    assert tenant._webapp_inline_flag(b) is True
    assert tenant._webapp_url(b) is None and tenant._kbtn_for(b, None).web_app is None
    assert tenant._webapp_url_inline(b) == url
    assert tenant._ibtn_for(b, None).web_app.url == url
    plain = SimpleNamespace(id=3, label="x", action="web_app", icon_emoji_id=None, config=json.dumps({"webapp_url": url}))
    assert tenant._webapp_inline_flag(plain) is False and tenant._kbtn_for(plain, None).web_app.url == url
