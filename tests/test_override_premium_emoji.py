"""PATCH_VS_OVERRIDE_PREMIUM_EMOJI_V1 (SINA) — متنِ دلخواهِ ثبت/تکمیل/لغو باید ایموجیِ پرمیوم بگیرد، نه entityِ کهنهٔ متنِ پیش‌فرض."""
import pytest

import premium_emoji_dynamic as PED
from tests.test_paused_text_e2e import env, OWNER, USER  # noqa: F401

DEFAULT = "✅ سفارش شما با موفقیت ثبت شد! 🎉\n\n🔖 کد پیگیری: TRK1"
CUSTOM = "✅ سفارشت ثبت شد 🙏"


@pytest.mark.asyncio
async def test_custom_order_text_gets_premium_emoji_and_no_stale_entities(env, monkeypatch):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    tenant, bot, cap = env["tenant"], env["bot"], env["cap"]
    await tenant.set_setting(env["bid"], "smm_txt_order_placed_manual", CUSTOM)
    tenant._SETTING_CACHE.clear()

    async def provider(token):
        return {"✅": ("5368324170671202286", "✅"), "🎉": ("5111111111111111111", "🎉")}
    monkeypatch.setattr(PED, "_PROVIDERS", [provider])
    PED._MAP_CACHE.clear() if hasattr(PED, "_MAP_CACHE") else None
    tenant._nav_install(bot)
    await bot.send_message(USER, DEFAULT, parse_mode="HTML")
    sent = [m for m in cap.calls if type(m).__name__ == "SendMessage"][-1]
    assert sent.text.startswith("✅ سفارشت ثبت شد") and "TRK1" not in sent.text      # متنِ دلخواه رفت
    ents = sent.entities or []
    kinds = [(e.type, e.custom_emoji_id) for e in ents]
    assert ("custom_emoji", "5368324170671202286") in kinds                           # ✅ پرمیوم شد
    assert all(e.offset + e.length <= len(sent.text.encode("utf-16-le")) // 2 for e in ents)   # entityِ کهنه نمانده
    assert not any(e.custom_emoji_id == "5111111111111111111" for e in ents)          # 🎉 فقط در متنِ پیش‌فرض بود


@pytest.mark.asyncio
async def test_custom_refund_text_applies_even_when_premium_emoji_changed_the_leading_emoji(env, monkeypatch):
    """SINA (#15769): مالک برایِ ↩️ ایموجیِ پرمیومی با نویسهٔ نمایشیِ 🔙 گذاشته؛ پیامِ پیش‌فرض با 🔙 به هوک می‌رسید و متنِ دلخواه جایگزین نمی‌شد."""
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    tenant, bot, cap = env["tenant"], env["bot"], env["cap"]
    await tenant.set_setting(env["bid"], "smm_txt_refunded", "💸 مبلغ برگشت داده شد")
    tenant._SETTING_CACHE.clear()

    async def provider(token):
        return {"↩️": ("5444444444444444444", "🔙")}
    monkeypatch.setattr(PED, "_PROVIDERS", [provider])
    tenant._nav_install(bot)
    await bot.send_message(USER, "↩️ سفارشِ تونِ تو (#15769) لغو شد و مبلغِ 36,623 تومان به کیف‌پولت بازگشت.", parse_mode="HTML")
    sent = [m for m in cap.calls if type(m).__name__ == "SendMessage"][-1]
    assert "مبلغ برگشت داده شد" in sent.text and "15769" not in sent.text        # متنِ دلخواه رفت، نه پیش‌فرض
