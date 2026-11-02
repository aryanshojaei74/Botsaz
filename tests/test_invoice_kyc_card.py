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


def _last_msg_call(cap):
    return [type(c).__name__ for c in cap.calls if type(c).__name__ in ("EditMessageText", "SendMessage")][-1]


@pytest.mark.asyncio
async def test_invoice_text_shows_selected_card_without_any_button(env):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    from tenant.shared.kyc_user import invoice_card_block
    await _user(env)
    await _kyc_on(env)
    text, rows = await invoice_card_block(env["bid"], TG)
    assert "6219" in text and "9157" in text and "861234" not in text          # ماسک‌شده
    assert "فقط با همین کارت پرداخت کنید" in text and "سطح ۲ محدود می‌گردد" in text
    assert rows == []                                                           # دکمهٔ تغییر کارت روی فاکتور نیست


@pytest.mark.asyncio
async def test_zarinpal_flow_select_card_first_then_message_edits_into_invoice(env, monkeypatch):
    """SINA: مبلغ ← انتخابِ کارت (+ تغییر کارت) ← همان پیام ویرایش و می‌شود فاکتور؛ تغییر کارت روی فاکتور نیست."""
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    from tests.test_paused_text_e2e import cb_update
    from tenant.gateways import zarinpal_wallet as ZW
    await _user(env)
    await _kyc_on(env)
    calls = {"pay": 0}

    async def _cfg(bot_id):
        return True

    async def _req(merchant, amount, description, callback):
        calls["pay"] += 1
        return True, "AUTH123"

    async def _url(bot_id, authority):
        return "https://pay.example/" + authority
    monkeypatch.setattr(ZW, "_configured", _cfg)
    monkeypatch.setattr(ZW.platform_zarinpal, "request_payment", _req)
    monkeypatch.setattr(ZW, "startpay_url_for_bot", _url)
    await env["tenant"].set_setting(env["bid"], "zarinpal_merchant_id", "m")
    env["tenant"]._SETTING_CACHE.clear()
    cap, dp, bot = env["cap"], env["dp"], env["bot"]

    await dp.feed_update(bot, cb_update("wzp:amt:100000", uid=TG))
    shown = cap.shown()
    assert calls["pay"] == 0                                                    # هنوز فاکتوری ساخته نشده
    t, btns = shown[-1]
    assert "شمارهٔ کارتِ مورد نظر خود را انتخاب کنید" in t
    assert "kcsel:zp:100000" in btns and "kycu:cc" in btns
    assert _last_msg_call(cap) == "EditMessageText"                             # همان پیام ویرایش شد، نه پیامِ جدید

    cap.calls.clear()
    await dp.feed_update(bot, cb_update("kcsel:zp:100000", uid=TG))
    assert calls["pay"] == 1
    t2, btns2 = cap.shown()[-1]
    assert "فاکتور شارژ کیف پول" in t2 and "کارتِ انتخاب‌شدهٔ شما" in t2 and "فقط با همین کارت پرداخت کنید" in t2
    assert "kycu:cc" not in btns2 and "wzp:verify" in " ".join(str(b) for b in btns2)
    assert _last_msg_call(cap) == "EditMessageText"                             # باز هم همان پیام


@pytest.mark.asyncio
async def test_user_without_registered_card_goes_straight_to_invoice(env, monkeypatch):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    from tests.test_paused_text_e2e import cb_update
    from tenant.gateways import zarinpal_wallet as ZW
    await _user(env, card=None, level=0)
    await _kyc_on(env)

    async def _cfg(bot_id):
        return True

    async def _req(merchant, amount, description, callback):
        return True, "AUTH9"

    async def _url(bot_id, authority):
        return "https://pay.example/" + authority
    monkeypatch.setattr(ZW, "_configured", _cfg)
    monkeypatch.setattr(ZW.platform_zarinpal, "request_payment", _req)
    monkeypatch.setattr(ZW, "startpay_url_for_bot", _url)
    await env["tenant"].set_setting(env["bid"], "zarinpal_merchant_id", "m")
    env["tenant"]._SETTING_CACHE.clear()
    await env["dp"].feed_update(env["bot"], cb_update("wzp:amt:100000", uid=TG))
    t, _b = env["cap"].shown()[-1]
    assert "فاکتور شارژ کیف پول" in t and "انتخاب کنید" not in t


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
