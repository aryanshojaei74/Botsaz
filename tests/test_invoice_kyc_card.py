"""PATCH_INVOICE_KYC_CARD_V1 (SINA) — زیرِ فاکتورِ زرین‌پال/زیبال/بلوپال: کارتِ ثبت‌شده + دکمهٔ «تغییر کارت بانکی»."""
import pytest

import config
import models
from tests.test_paused_text_e2e import env, OWNER, USER  # noqa: F401

TG = 700123


async def _user(env, card="6219861234569157", blocked=False, level=1):
    async with config.Session() as s:
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=TG, full_name="Ali", balance=0,
                             kyc_card=card, kyc_level=level, kyc_blocked=blocked))
        await s.commit()


async def _kyc_on(env):
    await env["tenant"].set_setting(env["bid"], "kyc_enabled", "1")
    env["tenant"]._SETTING_CACHE.clear()


@pytest.mark.asyncio
async def test_registered_card_shown_with_change_button_and_warning(env):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    from tenant.shared.kyc_user import invoice_card_block
    await _user(env)
    await _kyc_on(env)
    text, rows = await invoice_card_block(env["bid"], TG)
    assert "6219" in text and "9157" in text and "861234" not in text          # ماسک‌شده
    assert "با همین کارت پرداخت کنید" in text and "سطح ۲ محدود می‌گردد" in text
    assert rows[0][0].callback_data == "kycu:cc" and "تغییر کارت بانکی" in rows[0][0].text


@pytest.mark.asyncio
async def test_nothing_for_users_without_card_blocked_or_kyc_off(env):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    from tenant.shared.kyc_user import invoice_card_block
    await _user(env, card=None, level=0)
    await _kyc_on(env)
    assert await invoice_card_block(env["bid"], TG) == ("", [])                # بدونِ کارت
    assert await invoice_card_block(env["bid"], 999) == ("", [])               # کاربرِ ناشناس
    async with config.Session() as s:
        u = await s.get(models.BotUser, 1)
        u.kyc_card, u.kyc_level, u.kyc_blocked = "6219861234569157", 1, True
        await s.commit()
    assert await invoice_card_block(env["bid"], TG) == ("", [])                # محدودشده
    async with config.Session() as s:
        u = await s.get(models.BotUser, 1)
        u.kyc_blocked = False
        await s.commit()
    await env["tenant"].set_setting(env["bid"], "kyc_enabled", "0")
    env["tenant"]._SETTING_CACHE.clear()
    assert await invoice_card_block(env["bid"], TG) == ("", [])                # احراز خاموش
