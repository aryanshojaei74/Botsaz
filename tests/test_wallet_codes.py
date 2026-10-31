"""PATCH_WALLET_CODES_V1 — کدِ شارژ کیف‌پول."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import wallet_codes as WC


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, WC):
        monkeypatch.setattr(mod, "Session", maker)
    WC._FAILS.clear()
    async with maker() as s:
        s.add(models.Bot(id=6, owner_id=500, token="6:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=6, telegram_id=11, full_name="A", balance=1000))
        s.add(models.BotUser(id=2, bot_id=6, telegram_id=12, full_name="B", balance=0))
        await s.commit()
    yield maker
    await engine.dispose()


async def _mk(maker, **kw):
    d = dict(bot_id=6, code="GIFT-1", amount_toman=50_000, max_uses=2, per_user=1, active=True)
    d.update(kw)
    async with maker() as s:
        c = models.WalletCode(**d)
        s.add(c)
        await s.commit()
        return c.id


async def _bal(maker, tg=11):
    async with maker() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == tg))).balance)


def test_code_helpers():
    assert WC.norm_code(" ab-12 cd ") == "AB-12CD" and WC.norm_code("۱۲ab") == "12AB"
    c = WC.gen_code()
    assert len(c) == 9 and c[4] == "-" and not set(c.replace("-", "")) & set("OIL01")


@pytest.mark.asyncio
async def test_redeem_credits_once_per_user_and_respects_total_cap(env):
    await _mk(env)
    r = await WC.redeem(6, 11, "gift-1")
    assert r["status"] == "ok" and r["amount"] == 50_000 and await _bal(env) == 51_000
    assert (await WC.redeem(6, 11, "GIFT-1"))["status"] == "already" and await _bal(env) == 51_000   # هر کاربر یک‌بار
    assert (await WC.redeem(6, 12, "GIFT-1"))["status"] == "ok"
    async with env() as s:
        n = len((await s.scalars(select(models.WalletTxn))).all())
        c = await s.scalar(select(models.WalletCode))
    assert n == 2 and c.used_count == 2
    # ظرفیتِ کل تمام شد (کاربرِ سوم)
    async with env() as s:
        s.add(models.BotUser(id=3, bot_id=6, telegram_id=13, full_name="C"))
        await s.commit()
    assert (await WC.redeem(6, 13, "GIFT-1"))["status"] == "used_up"


@pytest.mark.asyncio
async def test_inactive_expired_and_unlimited_per_user(env):
    await _mk(env, code="OFF", active=False)
    await _mk(env, code="OLD", expires_at=dt.datetime.utcnow() - dt.timedelta(days=1))
    await _mk(env, code="FREE", max_uses=0, per_user=0, amount_toman=1000)
    assert (await WC.redeem(6, 11, "OFF"))["status"] == "inactive"
    assert (await WC.redeem(6, 11, "OLD"))["status"] == "expired"
    for _ in range(3):
        assert (await WC.redeem(6, 11, "FREE"))["status"] == "ok"
    assert await _bal(env) == 4000


@pytest.mark.asyncio
async def test_bruteforce_lock_and_isolation_between_bots(env):
    await _mk(env, code="REAL")
    for i in range(WC.MAX_FAILS):
        assert (await WC.redeem(6, 11, f"BAD{i}"))["status"] == "invalid"
    r = await WC.redeem(6, 11, "REAL")                       # حتی کدِ درست هم در قفل رد می‌شود
    assert r["status"] == "locked" and r["wait"] > 0 and await _bal(env) == 1000
    assert (await WC.redeem(6, 12, "REAL"))["status"] == "ok"           # کاربرِ دیگر آزاد است
    assert (await WC.redeem(99, 12, "REAL"))["status"] == "invalid"     # کدِ ربات‌های دیگر دیده نمی‌شود


@pytest.mark.asyncio
async def test_button_visibility_depends_on_active_codes(env):
    assert await WC.any_active(6) is False
    await _mk(env, code="X", active=False)
    assert await WC.any_active(6) is False
    cid = await _mk(env, code="Y")
    assert await WC.any_active(6) is True
    async with env() as s:
        c = await s.get(models.WalletCode, cid)
        c.used_count = 2
        await s.commit()
    assert await WC.any_active(6) is False                              # ظرفیتِ همهٔ کدها تمام شد


class _Msg:
    def __init__(self, text, uid=500):
        self.text, self.from_user, self.out, self.docs = text, NS(id=uid, full_name="x", username="x"), [], []

    async def answer(self, text, **k):
        self.out.append(text)

    async def answer_document(self, doc, **k):
        self.docs.append(doc)


class _St:
    def __init__(self):
        self.d, self.s = {}, None

    async def set_state(self, s):
        self.s = s

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def clear(self):
        self.d, self.s = {}, None


@pytest.mark.asyncio
async def test_owner_wizards_single_custom_and_batch(env, monkeypatch):
    async def guard(msg, bot):
        return NS(id=6, owner_id=500)
    monkeypatch.setattr(WC, "_guard_msg", guard)
    st = _St()
    await st.update_data(wc_batch=False)
    for fn, val in ((WC.s_amount, "۲۰,۰۰۰"), (WC.s_uses, "5"), (WC.s_per_user, "1"), (WC.s_days, "30")):
        await fn(_Msg(val), None, st)
    assert st.s is WC.WcFSM.code
    m = _Msg("nowruz")
    await WC.s_code(m, None, st)
    assert "NOWRUZ" in m.out[-1]
    async with env() as s:
        c = await s.scalar(select(models.WalletCode).where(models.WalletCode.code == "NOWRUZ"))
    assert c.amount_toman == 20_000 and c.max_uses == 5 and c.expires_at is not None
    m = _Msg("nowruz")                                                  # تکراری
    st2 = _St()
    await st2.update_data(wc_amount=1, wc_uses=1, wc_per=1, wc_days=0)
    await WC.s_code(m, None, st2)
    assert "قبلاً" in m.out[-1]
    # گروهی
    st3 = _St()
    await st3.update_data(wc_amount=7000, wc_uses=1, wc_per=1, wc_days=0, wc_batch=True)
    m = _Msg("12")
    await WC.s_count(m, None, st3)
    assert len(m.docs) == 1 and "12" in m.out[-1].replace("۱۲", "12")
    async with env() as s:
        n = len((await s.scalars(select(models.WalletCode).where(models.WalletCode.amount_toman == 7000))).all())
    assert n == 12
    m = _Msg("500")
    await WC.s_count(m, None, st3)
    assert "۱ تا ۲۰۰" in m.out[-1]
