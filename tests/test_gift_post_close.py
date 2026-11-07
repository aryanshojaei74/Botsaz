"""PATCH_GIFT_POST_CLOSE_V1 (MoBiN) — بعد از تحویلِ خودکار، پستِ سفارش در «کانالِ سفارشات» دکمهٔ ✅/❌ نمی‌ماند."""
from types import SimpleNamespace

import pytest

from tests.test_paused_text_e2e import env  # noqa: F401


@pytest.mark.asyncio
async def test_channel_post_buttons_replaced_with_done_label(env):
    if env["field"] != "virtual_service":
        pytest.skip("فقط ربات خدمات مجازی")
    from tenant.bots.virtual_service import smm
    tenant, bot, cap = env["tenant"], env["bot"], env["cap"]
    fake_post = SimpleNamespace(chat=SimpleNamespace(type="channel", id=-1001234), message_id=77)
    await smm._gift_post_remember(env["bid"], 5, fake_post)
    await smm._gift_post_remember(env["bid"], 6, SimpleNamespace(chat=SimpleNamespace(type="private", id=9), message_id=1))
    tenant._SETTING_CACHE.clear()
    await smm._gift_post_close(bot, env["bid"], 5, "✅ تحویل شد (اکانتِ پشتیبانی)")
    edits = [c for c in cap.calls if type(c).__name__ == "EditMessageReplyMarkup"]
    assert len(edits) == 1 and edits[0].chat_id == -1001234 and edits[0].message_id == 77
    btn = edits[0].reply_markup.inline_keyboard[0][0]
    assert "تحویل شد (اکانتِ پشتیبانی)" in btn.text and btn.callback_data == "noop"
    # بار دوم/سفارشِ بدونِ پستِ کانال: بی‌اثر و بدونِ خطا
    tenant._SETTING_CACHE.clear()
    await smm._gift_post_close(bot, env["bid"], 5, "x")
    await smm._gift_post_close(bot, env["bid"], 6, "x")
    assert len([c for c in cap.calls if type(c).__name__ == "EditMessageReplyMarkup"]) == 1


@pytest.mark.asyncio
async def test_notify_manual_order_returns_the_channel_message(env):
    if env["field"] != "virtual_service":
        pytest.skip("فقط ربات خدمات مجازی")
    tenant, bot = env["tenant"], env["bot"]
    await tenant.set_setting(env["bid"], "smm_manual_channel_id", "-1001234")
    await tenant.set_setting(env["bid"], "smm_manual_channel_enabled", "1")
    tenant._SETTING_CACHE.clear()
    row = await tenant.get_bot_row_by_id(env["bid"])
    m = await tenant._notify_manual_order(bot, row, "سفارش")
    assert m is not None and getattr(m, "message_id", None)
