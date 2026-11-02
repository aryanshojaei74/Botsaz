"""PATCH_MANUAL_RENEW_V1 (MidnightSoul) — «تمدید دستی سرویس» برایِ کانفیگ‌هایِ دستی."""
import pytest
from sqlalchemy import select

import config
import models
from tests.test_paused_text_e2e import cb_update, msg_update, env, OWNER  # noqa: F401

USER = 800222


async def _setup(env, balance=100000, enabled=True, price=0):
    import tenant
    async with config.Session() as s:
        u = models.BotUser(bot_id=env["bid"], telegram_id=USER, full_name="Ali", username="ali", balance=balance)
        s.add(u)
        await s.flush()
        s.add(models.Config(bot_id=env["bid"], code=91, volume_gb=10, days=30, payload="vless://x", status="sold",
                            owner_user_id=u.id, price=60000))
        await s.commit()
        cid = await s.scalar(select(models.Config.id).where(models.Config.code == 91))
    if enabled:
        await tenant.set_setting(env["bid"], "mrenew_enabled", "1")
    if price:
        await tenant.set_setting(env["bid"], "mrenew_price", str(price))
    tenant._SETTING_CACHE.clear()
    return cid


def _vpn(env):
    if env["field"] != "vpn_sales":
        pytest.skip("فقط رباتِ VPN")


def _texts(cap):
    return " ".join((getattr(c, "text", None) or getattr(c, "caption", None) or "") for c in cap.calls)


@pytest.mark.asyncio
async def test_button_hidden_when_disabled_and_shown_when_enabled(env):
    _vpn(env)
    from tenant.bots.vpn.manual_renew import renew_button
    cid = await _setup(env, enabled=False)
    assert await renew_button(env["bid"], cid) is None
    import tenant
    await tenant.set_setting(env["bid"], "mrenew_enabled", "1")
    await tenant.set_setting(env["bid"], "mrenew_label", "تمدید ویژه")
    tenant._SETTING_CACHE.clear()
    b = await renew_button(env["bid"], cid)
    assert b.text == "تمدید ویژه" and b.callback_data == f"mrn:ask:{cid}"


@pytest.mark.asyncio
async def test_confirm_question_then_payment_choices(env):
    _vpn(env)
    cid = await _setup(env)
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:ask:{cid}", uid=USER))
    assert "از تمدیدِ همین سرویس مطمئن هستید" in _texts(cap) and "60,000" in _texts(cap)
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:yes:{cid}", uid=USER))
    shown = [b for _t, bs in cap.shown() for b in bs]
    assert f"mrn:card:{cid}" in shown and f"mrn:wal:{cid}" in shown


@pytest.mark.asyncio
async def test_wallet_pays_notifies_admin_and_admin_completes(env):
    _vpn(env)
    cid = await _setup(env, price=50000)
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:wal:{cid}", uid=USER))
    async with config.Session() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
        req = await s.scalar(select(models.ManualRenewRequest))
    assert int(u.balance) == 50000 and req.status == "paid" and req.amount == 50000
    sent_to = [getattr(c, "chat_id", None) for c in cap.calls if type(c).__name__ == "SendMessage"]
    assert OWNER in sent_to                                   # ادمین خبر گرفت
    # درخواستِ تکراری برایِ همان سرویس رد می‌شود
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:wal:{cid}", uid=USER))
    async with config.Session() as s:
        assert len((await s.scalars(select(models.ManualRenewRequest))).all()) == 1
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
    assert int(u.balance) == 50000
    await env["dp"].feed_update(env["bot"], cb_update(f"mrna:done:{req.id}"))
    async with config.Session() as s:
        assert (await s.get(models.ManualRenewRequest, req.id)).status == "done"
    assert any(getattr(c, "chat_id", None) == USER and "تمدید شد" in (getattr(c, "text", "") or "") for c in cap.calls)


@pytest.mark.asyncio
async def test_wallet_insufficient_does_not_create_request(env):
    _vpn(env)
    cid = await _setup(env, balance=1000)
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:wal:{cid}", uid=USER))
    async with config.Session() as s:
        assert (await s.scalars(select(models.ManualRenewRequest))).all() == []
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
    assert int(u.balance) == 1000


@pytest.mark.asyncio
async def test_card_receipt_goes_to_admin_then_approve_and_refund_guard(env):
    _vpn(env)
    import tenant
    cid = await _setup(env)
    await tenant.set_setting(env["bid"], "card_number", "6037991111111111")
    tenant._SETTING_CACHE.clear()
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:card:{cid}", uid=USER))
    assert "6037991111111111" in _texts(cap)
    await env["dp"].feed_update(env["bot"], msg_update("واریز شد ساعت ۱۰", uid=USER))
    async with config.Session() as s:
        req = await s.scalar(select(models.ManualRenewRequest))
    assert req.status == "pending" and req.receipt_text
    await env["dp"].feed_update(env["bot"], cb_update(f"mrna:ok:{req.id}"))
    async with config.Session() as s:
        assert (await s.get(models.ManualRenewRequest, req.id)).status == "paid"
    # کاربرِ عادی نمی‌تواند تصمیمِ ادمین را بزند
    await env["dp"].feed_update(env["bot"], cb_update(f"mrna:done:{req.id}", uid=USER))
    async with config.Session() as s:
        assert (await s.get(models.ManualRenewRequest, req.id)).status == "paid"


@pytest.mark.asyncio
async def test_other_users_config_is_refused(env):
    _vpn(env)
    cid = await _setup(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"mrn:wal:{cid}", uid=999001))
    async with config.Session() as s:
        assert (await s.scalars(select(models.ManualRenewRequest))).all() == []
