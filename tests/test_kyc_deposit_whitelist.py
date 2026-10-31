"""PATCH_KYC_DEPOSIT_WHITELIST_V1 / PATCH_COIN_FLIP_COMMAND_V1 (فدایی): فقط شارژِ واقعیِ حساب سقفِ احراز را مصرف می‌کند."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.shared import dice_game as DG
from tenant.shared import kyc as K
from tenant.shared import group_betting as GB
from tenant.shared import kyc_lock as KL


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (K, DG, tenant):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=11, full_name="A", balance=0))
        await s.commit()
    yield mk
    await eng.dispose()


async def _add(mk, rows):
    async with mk() as s:
        for kind, amount, note in rows:
            s.add(models.WalletTxn(bot_id=1, user_id=1, amount=amount, kind=kind, note=note))
        await s.commit()


def test_is_deposit_txn_only_real_charges():
    ok = [("apay_charge", 10, "x"), ("zarinpal_charge", 10, ""), ("swap_charge", 5, None), ("ton_direct", 5, ""),
          ("charge", 10, "شارژ سفارش #12"), ("charge", 10, "AUTO:شارژ خودکار سفارش #3"),
          ("charge", 10, "شارژ با استارز تلگرام (50 ⭐)")]
    bad = [("bet_prize", 10, "جایزهٔ بازی #5"), ("group_transfer_in", 10, ""), ("referral", 10, ""),
           ("giveaway_prize", 10, ""), ("charge", 10, "بازگشتِ وجهِ گیفتِ ناموفق"), ("charge", 10, "جایزهٔ بازی تاس (نتیجه 6)"),
           ("charge", 10, "جایزهٔ گردونهٔ شانس"), ("smm_refund", 10, ""), ("admin", 10, ""), ("welcome_bonus", 10, ""),
           ("apay_charge", -10, ""), ("purchase", -10, "")]
    assert all(K.is_deposit_txn(r[0], r[2], r[1]) for r in ok)
    assert not any(K.is_deposit_txn(r[0], r[2], r[1]) for r in bad)


@pytest.mark.asyncio
async def test_charged_today_ignores_games_transfers_bonuses_and_refunds(env):
    await _add(env, [("apay_charge", 100_000, "x"), ("charge", 50_000, "شارژ سفارش #1"),
                     ("bet_prize", 900_000, "جایزهٔ بازی #5"), ("group_transfer_in", 400_000, "انتقال"),
                     ("referral", 70_000, "دعوت"), ("charge", 30_000, "بازگشتِ وجهِ سفارشِ استارزِ ردشده"),
                     ("charge", 20_000, "جایزهٔ بازی تاس"), ("giveaway_prize", 10_000, "قرعه‌کشی")])
    assert await K.charged_today(1, 1) == 150_000


@pytest.mark.asyncio
async def test_dice_topup_gate_needs_a_real_deposit(env, monkeypatch):
    async def gate(bot_id):
        return "topup"
    monkeypatch.setattr(DG, "_get_gate", gate)
    await _add(env, [("bet_prize", 5_000, "جایزهٔ بازی"), ("referral", 5_000, "")])
    ok, _ = await DG.gate_check(1, 11)
    assert not ok                                   # فقط جایزه/پاداش ⇒ هنوز «شارژ نکرده»
    await _add(env, [("zarinpal_charge", 10_000, "شارژ")])
    ok, _ = await DG.gate_check(1, 11)
    assert ok


def test_coin_flip_command_words():
    for rx in (GB._BET_RE, KL._BET_RE):
        assert rx.match("شیر یا خط 1000") and rx.match("شیر یا خط ۵۰۰۰") and rx.match("بازی 1000")
        assert not rx.match("شرطبندی 1000") and not rx.match("شرط بندی 1000")
    assert GB._INCOMPLETE_BET_RE.match("شیر یا خط") and not GB._INCOMPLETE_BET_RE.match("شرطبندی")
