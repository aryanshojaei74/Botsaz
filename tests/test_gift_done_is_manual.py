"""PATCH_GIFT_DONE_IS_MANUAL_V1 (SINA) — «✅ انجام شد» روی سفارشِ برگشته به دستی، گیفت را دوباره خودکار نمی‌فرستد؛ فقط دستی تکمیل می‌کند."""
import asyncio

import pytest
from sqlalchemy import select

import models
from tests.test_acc_gift import ACC, env  # noqa: F401
from tests.test_tgtools_gift import GID, OWNER, USER, cb_update, order, wait_status, env as _tg_env  # noqa: F401


@pytest.mark.asyncio
async def test_done_after_failed_auto_completes_manually_without_resending(env):
    env["acc"]["exc"] = ACC.AccGiftError("گیرنده برایِ اکانتِ پشتیبانی معتبر نبود")
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    for _ in range(200):                                                           # تا برگشتِ ارسالِ خودکار به «دستی»
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "awaiting_admin" and (o.fail_reason or "").strip():
            break
    assert o.status == "awaiting_admin" and (o.fail_reason or "").strip()          # دلیل ثبت شد
    attempts = len(env["acc"]["sent"])
    env["acc"]["exc"] = None                                                       # حتی اگر الان ارسال ممکن بود…
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:cardok:{o.id}", uid=OWNER, uname="owner"))
    o2 = await wait_status(env, "sent")
    assert o2.status == "sent"
    assert len(env["acc"]["sent"]) == attempts                                     # …دوباره خودکار نفرستاد
    assert any("ثبت شد" in t or "تکمیل" in t for t in env["cap"].texts(USER))       # مشتری خبر گرفت


@pytest.mark.asyncio
async def test_done_on_wallet_order_never_auto_sends(env):
    async with env["maker"]() as s:
        buyer = await s.scalar(select(models.BotUser))
        s.add(models.GiftOrder(bot_id=env["bid"], buyer_user_id=buyer.id, gift_id=GID, gift_emoji="💝", star_count=15,
                               price_toman=1000, dest_telegram_id=USER, dest_label="خودم\n👤 یوزرنیم: @aliuser",
                               status="awaiting_admin", pay_method="wallet", tracking_code="W1", quantity=1))
        await s.commit()
    o = await order(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:cardok:{o.id}", uid=OWNER, uname="owner"))
    o2 = await wait_status(env, "sent")
    assert o2.status == "sent" and env["acc"]["sent"] == []


@pytest.mark.asyncio
async def test_first_card_receipt_approval_still_auto_sends(env):
    async with env["maker"]() as s:
        buyer = await s.scalar(select(models.BotUser))
        s.add(models.GiftOrder(bot_id=env["bid"], buyer_user_id=buyer.id, gift_id=GID, gift_emoji="💝", star_count=15,
                               price_toman=1000, dest_telegram_id=USER, dest_label="خودم\n👤 یوزرنیم: @aliuser",
                               status="awaiting_admin", pay_method="card", tracking_code="C1", quantity=1))
        await s.commit()
    o = await order(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:cardok:{o.id}", uid=OWNER, uname="owner"))
    o2 = await wait_status(env, "sent")
    assert o2.status == "sent" and len(env["acc"]["sent"]) == 1
