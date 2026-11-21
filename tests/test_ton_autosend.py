"""PATCH_TON_AUTOSEND_V1 — فروشِ تون با ارسالِ خودکار (TON Center API v2) از کیف‌پولِ حضانتیِ ربات؛ ایمنی: سقف، قفلِ ردیف، بدونِ تکرار."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import fragment_stars as FS
from tenant.bots.virtual_service import ton_autosend as TA

BOT = 7
GOOD = "UQCUSUNaebeBTjd7egmGB85cu15e5xW3Qq4fzeuOI_dHaHxE"


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, TA):
        monkeypatch.setattr(mod, "Session", mk)
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    mn, _p, _k, w = Wallets.create(WalletVersionEnum.v4r2, workchain=0)
    import wallet_crypto
    monkeypatch.setattr(wallet_crypto, "decrypt_secret", lambda x: " ".join(mn))
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=500, token="7:T", username="vs", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=BOT, telegram_id=1001, full_name="Ali", balance=0))
        s.add(models.FragmentWallet(bot_id=BOT, address=w.address.to_string(True, True, False), encrypted_mnemonic="x", enabled=True))
        await s.commit()
    cfg = {"ton_autosend": "1", "ton_autosend_max": "10"}

    async def get_setting(bot_id, key, default=""):
        return cfg.get(key, default)
    monkeypatch.setattr(TA, "_get_setting", get_setting)
    TA._PAUSED_UNTIL.clear()
    TA._LOCKS.clear()
    monkeypatch.setattr(TA, "CONFIRM_POLL_S", 0)
    monkeypatch.setattr(TA, "CONFIRM_WAIT_S", 0.05)
    state = {"balance": 5.0, "seqno": 5, "sent": [], "send_ok": True, "advance": True}

    async def bal(addr):
        return state["balance"]

    async def seqno(addr):
        return state["seqno"]

    async def send_boc(boc):
        state["sent"].append(boc)
        if state["send_ok"] and state["advance"]:
            state["seqno"] += 1
        return (True, "ok") if state["send_ok"] else (False, "timeout")
    monkeypatch.setattr(FS, "fetch_balance_ton", bal)
    monkeypatch.setattr(FS, "_get_seqno", seqno)
    monkeypatch.setattr(FS, "_send_boc", send_boc)
    yield NS(mk=mk, cfg=cfg, state=state)
    await engine.dispose()


async def mk_order(env, amount=1.5, addr=GOOD, memo=None, status="awaiting_admin"):
    async with env.mk() as s:
        o = models.GiftOrder(bot_id=BOT, buyer_user_id=1, gift_id=f"tonsell_{int(amount * 100)}", gift_emoji="💎", star_count=int(amount * 100),
                             price_toman=100_000, dest_telegram_id=1001, dest_label=addr, dest_memo=memo, status=status,
                             pay_method="wallet", tracking_code="T1")
        s.add(o)
        await s.commit()
        return o.id


async def st(env, oid):
    async with env.mk() as s:
        o = await s.get(models.GiftOrder, oid)
        return o.status, o.deliver_via, o.fail_reason


ROW = NS(id=BOT, owner_id=500)


def test_address_validation():
    assert TA.valid_address(GOOD) and not TA.valid_address("name.ton") and not TA.valid_address("junk") and not TA.valid_address("")


@pytest.mark.asyncio
async def test_disabled_does_nothing(env):
    env.cfg["ton_autosend"] = "0"
    oid = await mk_order(env)
    assert (await TA.deliver_order(None, ROW, oid))[0] == "disabled" and not env.state["sent"]
    assert (await st(env, oid))[0] == "awaiting_admin"


@pytest.mark.asyncio
async def test_success_sends_once_and_marks_sent(env):
    oid = await mk_order(env, 1.5, memo="memo123")
    res, note = await TA.deliver_order(None, ROW, oid)
    assert res == "sent" and len(env.state["sent"]) == 1
    assert (await st(env, oid))[:2] == ("sent", "auto")
    assert (await TA.deliver_order(None, ROW, oid))[0] == "manual" and len(env.state["sent"]) == 1      # دوباره نمی‌رود


@pytest.mark.asyncio
async def test_safe_fallbacks_to_manual_without_sending(env):
    big = await mk_order(env, 11)                                                   # بیشتر از سقف ۱۰
    assert (await TA.deliver_order(None, ROW, big))[0] == "manual"
    dom = await mk_order(env, 1, addr="alice.ton")
    assert (await TA.deliver_order(None, ROW, dom))[0] == "manual"
    env.state["balance"] = 1.0                                                       # موجودیِ کم
    low = await mk_order(env, 2)
    res, note = await TA.deliver_order(None, ROW, low)
    assert res == "manual" and "کافی نیست" in note
    assert not env.state["sent"]
    assert (await st(env, low))[0] == "awaiting_admin" and "کافی" in (await st(env, low))[2]
    for oid in (big, dom):
        assert (await st(env, oid))[0] == "awaiting_admin"


@pytest.mark.asyncio
async def test_unconfirmed_keeps_sending_status_and_pauses(env):
    env.state["advance"] = False                                                     # ارسال شد ولی seqno جلو نرفت
    a = await mk_order(env, 1)
    res, _ = await TA.deliver_order(None, ROW, a)
    assert res == "unconfirmed" and len(env.state["sent"]) == 1 and (await st(env, a))[0] == "sending"
    assert "نامشخص" in (await st(env, a))[2] and TA.paused(BOT)
    b = await mk_order(env, 1)
    res2, note2 = await TA.deliver_order(None, ROW, b)                               # توقفِ موقت: دوباره نمی‌فرستد
    assert res2 == "manual" and len(env.state["sent"]) == 1 and (await st(env, b))[0] == "awaiting_admin"


@pytest.mark.asyncio
async def test_sendboc_error_is_unconfirmed_not_retried(env):
    env.state["send_ok"] = False
    a = await mk_order(env, 1)
    res, _ = await TA.deliver_order(None, ROW, a)
    assert res == "unconfirmed" and len(env.state["sent"]) == 1 and (await st(env, a))[0] == "sending"


@pytest.mark.asyncio
async def test_already_sending_order_is_never_resent(env):
    oid = await mk_order(env, 1, status="sending")
    assert (await TA.deliver_order(None, ROW, oid))[0] == "manual" and not env.state["sent"]


@pytest.mark.asyncio
async def test_missing_wallet_falls_back(env):
    async with env.mk() as s:
        w = (await s.scalars(__import__("sqlalchemy").select(models.FragmentWallet))).one()
        await s.delete(w)
        await s.commit()
    oid = await mk_order(env, 1)
    res, note = await TA.deliver_order(None, ROW, oid)
    assert res == "manual" and "ساخته" in note and (await st(env, oid))[0] == "awaiting_admin"


@pytest.mark.asyncio
async def test_admin_can_resolve_sending_order(env, monkeypatch):
    from tenant.bots.virtual_service import smm
    monkeypatch.setattr(smm, "Session", env.mk)
    oid = await mk_order(env, 1, status="sending")
    notified = []

    async def fake_owner(cb, bot):
        return ROW

    async def fake_notify(bot, row, o, buyer_tg, amount, trk):
        notified.append((o, buyer_tg, amount))
    monkeypatch.setattr(smm, "_owner", fake_owner)
    monkeypatch.setattr(smm, "_ton_sent_notify", fake_notify)

    class CB:
        data = f"ton:done:{oid}"
        alerts = []
        message = NS(edit_reply_markup=None)

        async def answer(self, t=None, show_alert=False):
            self.alerts.append(t)
    CB.message = NS(edit_reply_markup=lambda **k: _noop())
    await smm.ton_order_done(CB(), None)
    assert (await st(env, oid))[0] == "sent" and notified and notified[0][2] == 1.0


async def _noop():
    return None


@pytest.mark.asyncio
async def test_admin_screen_shows_toggle_and_wallet(env, monkeypatch):
    from tenant.bots.virtual_service import smm
    monkeypatch.setattr(smm, "Session", env.mk)

    async def gs(bot_id, key, default=""):
        return {"ton_sell_rate": "250000"}.get(key, default)
    monkeypatch.setattr(smm, "_get_setting", gs)
    text, kb = await smm.ton_sell_admin_content(BOT)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "ton:asend" in cbs and "ton:acap" in cbs and "کیف‌پولِ ارسال" in text and "ارسالِ خودکار روشن" in text
    env.cfg["ton_autosend"] = "0"
    text, kb = await smm.ton_sell_admin_content(BOT)
    assert "ton:acap" not in [b.callback_data for r in kb.inline_keyboard for b in r] and "حالتِ دستی" in text
