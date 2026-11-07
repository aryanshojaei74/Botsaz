"""PATCH_RENEW_DAYS_VALIDATE_V1: روز/گیگِ تمدید و افزایشِ حجم از callback_data
می‌آید؛ مقدارِ دست‌ساز (منفی یا خارج از منو) نباید پذیرفته شود. قبلاً با
«vrend:<aid>:-100:0» قیمت منفی می‌شد و کیف پول *شارژ* می‌شد."""
from __future__ import annotations

import pytest

from tests.test_vpn_refund_approval import env, cb_update, balance  # noqa: F401  (fixture)


async def _setup(env, bal=1_000_000):
    import tenant
    from sqlalchemy import update
    import models
    async with env.maker() as s:
        await s.execute(update(models.BotUser).values(balance=bal))
        await s.commit()
    for k, v in {"renew_price_day": "1000", "renew_min_days": "30", "renew_max_days": "90",
                 "renew_step_days": "30", "renew_price_gb": "2000", "renew_min_gb": "10",
                 "renew_max_gb": "50", "renew_step_gb": "10"}.items():
        await tenant.set_setting(env.bid, k, v)
        await tenant.set_setting(env.bid, f"{k}_sanai", v)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "vrend:{aid}:-100:0",        # روزِ منفی ⇒ قیمتِ منفی
    "vrend:{aid}:7:0",           # روزِ خارج از منو
    "vrend:{aid}:30:-5",         # گیگِ منفی
    "vrend:{aid}:30:13",         # گیگِ خارج از گام
    "vadd:pay:{aid}:-10",
    "vadd:pay:{aid}:1000",       # بیشتر از سقف
])
async def test_tampered_values_are_rejected_and_wallet_untouched(env, data):
    await _setup(env)
    before = await balance(env)
    await env.dp.feed_update(env.bot, cb_update(555, data.format(aid=env.aid)))
    assert await balance(env) == before
    # حتی کسر+عودتِ موقت هم نباید رخ دهد (اگر پنل خطا دهد عودت می‌شود و موجودی برابر می‌ماند)
    assert await _txns(env) == 0


async def _txns(env):
    from sqlalchemy import func, select
    import models
    async with env.maker() as s:
        return int(await s.scalar(select(func.count()).select_from(models.WalletTxn)) or 0)
