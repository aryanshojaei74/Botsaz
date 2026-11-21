"""Weirdnet: API انتقالِ موجودی + مستنداتِ برداشت/انتقال + حجمِ دلخواه/گامِ ۱۰ گیگ در تمدید."""
import json

import pytest
from sqlalchemy import select

import models
import tenant
from tests.test_vpn_api_extended import TG, _call, _balance, env  # noqa: F401  (fixture)


async def _add_user(env, tg=555002, bal=0, username="sara"):
    async with env["maker"]() as s:
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=tg, full_name="Sara", username=username, balance=bal))
        await s.commit()


async def _bal_of(env, tg):
    async with env["maker"]() as s:
        return float(await s.scalar(select(models.BotUser.balance).where(models.BotUser.telegram_id == tg)))


@pytest.mark.asyncio
async def test_transfer_disabled_by_default_and_info(env):
    r = await _call(env, "transfer_info")
    assert r == {"ok": True, "enabled": False, "mode": "off", "min_amount": 1000, "needs_admin_approval": False}
    await _add_user(env)
    r = await _call(env, "transfer", to="@sara", amount="50000")
    assert r == {"ok": False, "error": "transfer_disabled"} and await _balance(env) == 1_000_000


@pytest.mark.asyncio
async def test_auto_transfer_by_username_and_by_id(env, monkeypatch):
    await tenant.set_setting(env["bid"], "btr_mode", "auto")
    tenant._SETTING_CACHE.clear()
    await _add_user(env)
    r = await _call(env, "transfer", to="@Sara", amount="50000")
    assert r["ok"] and r["status"] == "done" and r["transfer"]["to"] == 555002 and r["balance"] == 950_000.0
    assert await _bal_of(env, 555002) == 50_000.0
    r = await _call(env, "transfer", to="555002", amount="۲۰٬۰۰۰")           # آیدیِ عددی + ارقامِ فارسی
    assert r["ok"] and await _bal_of(env, 555002) == 70_000.0 and await _balance(env) == 930_000


@pytest.mark.asyncio
async def test_transfer_validation_errors_do_not_move_money(env):
    await tenant.set_setting(env["bid"], "btr_mode", "auto")
    tenant._SETTING_CACHE.clear()
    await _add_user(env)
    assert (await _call(env, "transfer", to="@sara", amount="10"))["error"] == "invalid_amount"
    assert (await _call(env, "transfer", to="@nobody", amount="5000"))["error"] == "recipient_not_found"
    assert (await _call(env, "transfer", to=str(TG), amount="5000"))["error"] == "same_user"
    assert (await _call(env, "transfer", to="@sara", amount="99999999"))["error"] == "insufficient_balance"
    assert await _balance(env) == 1_000_000 and await _bal_of(env, 555002) == 0


@pytest.mark.asyncio
async def test_approve_mode_holds_money_pending(env):
    await tenant.set_setting(env["bid"], "btr_mode", "approve")
    tenant._SETTING_CACHE.clear()
    await _add_user(env)
    r = await _call(env, "transfer", to="@sara", amount="40000")
    assert r["ok"] and r["status"] == "pending" and r["balance"] == 960_000.0
    assert await _bal_of(env, 555002) == 0                                     # تا تأیید به مقصد نمی‌رسد


def test_docs_list_withdraw_and_transfer():
    from tenant.shared.public_api import render_vpn_docs_html
    html = render_vpn_docs_html("https://x")
    for a in ("withdraw_info", "withdraw", "withdraws", "transfer_info", "transfer"):
        assert f"<code>{a}</code>" in html, a


def test_renew_gb_options_up_to_ten_and_custom_range():
    from tenant.bots.vpn import vpn_user as V
    rc = V._RenewCfg(False, 500, 1000, 30, 90, 30, 10, 100, 10)
    assert V._renew_gb_options(rc) == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]      # قبلاً فقط ۶ تا (۱۰..۶۰)
    assert V._renew_gb_allowed(rc, 0) and V._renew_gb_allowed(rc, 40) and V._renew_gb_allowed(rc, 37)   # دلخواه درونِ بازه
    assert not V._renew_gb_allowed(rc, 5) and not V._renew_gb_allowed(rc, 101)
    off = V._RenewCfg(False, 500, 0, 30, 90, 30, 10, 100, 10)                          # قیمتِ گیگ صفر ⇒ حجمِ همراه خاموش
    assert V._renew_gb_options(off) == [] and not V._renew_gb_allowed(off, 20)
