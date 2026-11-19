"""PATCH_TRON_AUTOSEND_V1 — ارسالِ خودکارِ TRX/USDT: کیف‌پولِ رمزنگاری‌شده، شرط‌هایِ ایمنی، قفلِ ردیف، حالتِ نامشخص، و اتصال به فروشِ رمزارز."""
import asyncio
import types
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.crypto_shop as CX
import tenant.bots.virtual_service.tron_autosend as TA
import wallet_crypto
from tenant.bots.virtual_service import smm as S
from tenant.shared import tron_chain as TC

OWNER, BUYER, ADMIN2 = 1000, 111, 2000
BOT = 5
DEST = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    import sys
    orig = config.Session
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    TA._LOCKS.clear()
    TA._PAUSED_UNTIL.clear()
    monkeypatch.setattr(wallet_crypto, "WALLET_ENC_KEY", Fernet.generate_key().decode())
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="shop", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=BOT, telegram_id=BUYER, full_name="buyer", username="b", balance=Decimal(5_000_000)))
        await s.commit()

    async def get_bot_row(b):
        async with mk() as s:
            return await s.get(models.Bot, BOT)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    notes = []

    async def notify(bot, row, text, kb=None, photo_file_id=None): notes.append((text, kb))
    monkeypatch.setattr(S, "_notify_manual_order", notify)

    async def trust(*a, **k): return None
    monkeypatch.setattr(S, "_notify_gift_order_trust_success", trust)
    chain = types.SimpleNamespace(trx=150 * TC.SUN, usdt=1000 * TC.SUN, prepared=[], broadcasts=[], bc=(True, "broadcast"),
                                  confirm="success", prep_error=None, bc_exc=None, status="pending")

    async def trx_balance(addr, key=""): return chain.trx

    async def usdt_balance(addr, key=""): return chain.usdt

    async def prepare(coin, priv, to, centi, key=""):
        if chain.prep_error:
            raise chain.prep_error
        chain.prepared.append((coin, to, centi))
        return "ab" * 32, {"txID": "ab" * 32, "signature": ["00"]}

    async def broadcast(signed, key=""):
        chain.broadcasts.append(signed)
        if chain.bc_exc:
            raise chain.bc_exc
        return chain.bc

    async def wait_confirmed(txid, key="", **k): return chain.confirm

    async def tx_status(txid, key=""): return chain.status
    monkeypatch.setattr(TC, "trx_balance", trx_balance)
    monkeypatch.setattr(TC, "usdt_balance", usdt_balance)
    monkeypatch.setattr(TC, "prepare_transfer", prepare)
    monkeypatch.setattr(TC, "broadcast", broadcast)
    monkeypatch.setattr(TC, "wait_confirmed", wait_confirmed)
    monkeypatch.setattr(TC, "tx_status", tx_status)
    yield types.SimpleNamespace(mk=mk, notes=notes, chain=chain)
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


async def _row(env):
    async with env.mk() as s:
        return await s.get(models.Bot, BOT)


async def _make_order(env, coin="trx", centi=1234, net="TRC20", addr=DEST, status="awaiting_admin"):
    async with env.mk() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == BUYER))
        o = models.GiftOrder(bot_id=BOT, buyer_user_id=u.id, gift_id=f"cxsell_{coin}_{centi}", gift_emoji="🔴", star_count=centi,
                             price_toman=100_000, dest_telegram_id=BUYER, dest_label=addr, dest_memo=f"net:{net}",
                             status=status, pay_method="wallet", tracking_code="T1")
        s.add(o)
        await s.commit()
        return o.id


async def _order(env, oid):
    async with env.mk() as s:
        return await s.get(models.GiftOrder, oid)


async def _enable(auto=True, wallet=True):
    if wallet:
        ok, _ = await TA.create_wallet(BOT)
        assert ok
    if auto:
        await tenant.set_setting(BOT, "cx_tron_auto", "1")


# ───────────── کیف‌پول ─────────────
@pytest.mark.asyncio
async def test_wallet_key_is_encrypted_and_matches_the_address(env):
    ok, addr = await TA.create_wallet(BOT)
    assert ok and TC.is_valid_address(addr)
    stored = await tenant.get_setting(BOT, "cx_tron_key", "")
    key = await TA.reveal_private_key(BOT)
    assert stored and key not in stored and len(key) == 64                         # رمزنگاری‌شده؛ متنِ خام ذخیره نیست
    assert TC.address_from_private(int(key, 16)) == addr == await TA.wallet_address(BOT)
    ok2, msg = await TA.create_wallet(BOT)
    assert ok2 is False and "قبلاً" in msg and await TA.reveal_private_key(BOT) == key       # دوباره نمی‌سازد/رونویسی نمی‌کند


@pytest.mark.asyncio
async def test_wallet_not_created_without_encryption_key(env, monkeypatch):
    monkeypatch.setattr(wallet_crypto, "WALLET_ENC_KEY", "")
    ok, msg = await TA.create_wallet(BOT)
    assert ok is False and "WALLET_ENC_KEY" in msg
    assert await TA.wallet_address(BOT) is None and await tenant.get_setting(BOT, "cx_tron_key", "") == ""      # هیچ‌چیز نیمه‌کاره ذخیره نشد


# ───────────── تحویلِ خودکار ─────────────
@pytest.mark.asyncio
async def test_trx_is_sent_and_marked_sent_with_the_tx_hash(env):
    await _enable()
    oid = await _make_order(env, "trx", 1234)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "sent"
    assert env.chain.prepared == [("trx", DEST, 1234)] and len(env.chain.broadcasts) == 1
    o = await _order(env, oid)
    assert o.status == "sent" and o.tx_hash == "ab" * 32 and o.deliver_via == "auto" and o.fail_reason is None


@pytest.mark.asyncio
async def test_usdt_needs_usdt_balance_and_trx_for_energy(env):
    await _enable()
    oid = await _make_order(env, "usdt", 50_00)                                     # ۵۰٫۰۰ USDT
    env.chain.usdt = 10 * TC.SUN
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "manual" and "USDT کافی نیست" in note and env.chain.prepared == []
    env.chain.usdt = 1000 * TC.SUN
    env.chain.trx = 5 * TC.SUN
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "manual" and "TRX" in note and env.chain.prepared == []
    env.chain.trx = 100 * TC.SUN
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "sent" and env.chain.prepared == [("usdt", DEST, 5000)]
    assert (await _order(env, oid)).status == "sent"


@pytest.mark.asyncio
async def test_insufficient_trx_for_trx_sale_falls_back_to_manual_without_touching_the_chain(env):
    await _enable()
    oid = await _make_order(env, "trx", 50_000)                                      # ۵۰۰ TRX
    env.chain.trx = 400 * TC.SUN
    res, note = await TA.deliver_order(None, await _row(env), oid)
    o = await _order(env, oid)
    assert res == "manual" and o.status == "awaiting_admin" and "کافی نیست" in (o.fail_reason or "")
    assert env.chain.prepared == [] and env.chain.broadcasts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,expect", [
    ({"net": "BEP20"}, "TRC20"), ({"addr": "0x" + "ab" * 20}, "معتبر نیست"), ({"centi": 900_000}, "سقف")])
async def test_safety_conditions_keep_the_order_manual(env, kw, expect):
    await _enable()
    oid = await _make_order(env, **{"coin": "trx", **kw})
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "manual" and expect in note
    assert (await _order(env, oid)).status == "awaiting_admin" and env.chain.prepared == []


@pytest.mark.asyncio
async def test_disabled_by_default_and_force_overrides_only_the_switch(env):
    await _enable(auto=False)
    oid = await _make_order(env)
    assert (await TA.deliver_order(None, await _row(env), oid))[0] == "disabled" and env.chain.prepared == []
    res, _ = await TA.deliver_order(None, await _row(env), oid, force=True)
    assert res == "sent"
    oid2 = await _make_order(env, "trx", 900_000)
    res2, note2 = await TA.deliver_order(None, await _row(env), oid2, force=True)                  # force سقف را دور نمی‌زند
    assert res2 == "manual" and "سقف" in note2


@pytest.mark.asyncio
async def test_no_wallet_means_manual_not_a_crash(env):
    await _enable(wallet=False)
    oid = await _make_order(env)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "manual" and "ساخته" in note and (await _order(env, oid)).status == "awaiting_admin"


@pytest.mark.asyncio
async def test_prepare_failure_is_safe_to_retry_manually(env):
    await _enable()
    env.chain.prep_error = TC.TronError("tx mismatch (amount/type)")
    oid = await _make_order(env)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    o = await _order(env, oid)
    assert res == "manual" and "mismatch" in note and o.status == "awaiting_admin" and o.tx_hash is None
    assert env.chain.broadcasts == []


@pytest.mark.asyncio
async def test_ambiguous_broadcast_keeps_sending_status_hash_and_pauses_further_auto_sends(env):
    await _enable()
    env.chain.bc_exc = TC.TronAmbiguous("TimeoutError: slow")
    oid = await _make_order(env)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    o = await _order(env, oid)
    assert res == "unconfirmed" and o.status == "sending" and o.tx_hash == "ab" * 32            # هش برایِ چکِ ادمین ذخیره شد
    assert "نامشخص" in (o.fail_reason or "")
    env.chain.bc_exc = None
    oid2 = await _make_order(env)
    res2, note2 = await TA.deliver_order(None, await _row(env), oid2)
    assert res2 == "manual" and "متوقف" in note2 and len(env.chain.broadcasts) == 1             # ۱۵ دقیقه ارسالِ خودکارِ جدید نداریم
    res3, _ = await TA.deliver_order(None, await _row(env), oid)                                   # خودِ سفارشِ نامشخص دوباره نمی‌رود
    assert res3 == "manual" and len(env.chain.broadcasts) == 1


@pytest.mark.asyncio
async def test_node_rejection_falls_back_to_manual_unless_the_hash_exists_on_chain(env):
    await _enable()
    env.chain.bc = (False, "SIGERROR Signature error")
    oid = await _make_order(env)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    o = await _order(env, oid)
    assert res == "manual" and "نپذیرفت" in note and o.status == "awaiting_admin" and o.tx_hash is None
    env.chain.status = "success"                                                                    # رد شد ولی هش روی زنجیره هست ⇒ رفته
    oid2 = await _make_order(env)
    res2, _ = await TA.deliver_order(None, await _row(env), oid2)
    assert res2 == "sent" and (await _order(env, oid2)).status == "sent"


@pytest.mark.asyncio
async def test_onchain_failure_vs_not_confirmed(env):
    await _enable()
    env.chain.confirm = "failed"
    oid = await _make_order(env)
    res, note = await TA.deliver_order(None, await _row(env), oid)
    assert res == "manual" and "ناموفق" in note and (await _order(env, oid)).status == "awaiting_admin"
    env.chain.confirm = "pending"
    oid2 = await _make_order(env)
    res2, _ = await TA.deliver_order(None, await _row(env), oid2)
    assert res2 == "unconfirmed" and (await _order(env, oid2)).status == "sending"


@pytest.mark.asyncio
async def test_concurrent_deliveries_of_one_order_send_only_once(env):
    await _enable()
    oid = await _make_order(env)
    row = await _row(env)
    results = await asyncio.gather(*[TA.deliver_order(None, row, oid) for _ in range(5)])
    assert [r[0] for r in results].count("sent") == 1 and len(env.chain.prepared) == 1 and len(env.chain.broadcasts) == 1


@pytest.mark.asyncio
async def test_already_handled_orders_and_foreign_orders_are_not_sent(env):
    await _enable()
    for st in ("sent", "refunded", "sending"):
        oid = await _make_order(env, status=st)
        assert (await TA.deliver_order(None, await _row(env), oid))[0] == "manual"
    async with env.mk() as s:
        u = await s.scalar(select(models.BotUser))
        o = models.GiftOrder(bot_id=BOT, buyer_user_id=u.id, gift_id="tonsell_100", star_count=100, price_toman=1,
                             dest_telegram_id=1, dest_label=DEST, status="awaiting_admin")
        s.add(o)
        await s.commit()
        tid = o.id
    assert (await TA.deliver_order(None, await _row(env), tid))[0] == "manual"
    assert env.chain.prepared == []


# ───────────── اتصال به فروشِ رمزارز ─────────────
class _Bot:
    token = "5:T"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw): self.sent.append((chat_id, text, kw))


class _Msg:
    def __init__(self, uid=BUYER):
        self.out = []
        self.from_user = types.SimpleNamespace(id=uid, full_name="buyer", username="b")
        self.chat = types.SimpleNamespace(id=uid, type="private")
        self.message_id = 1
        self.text = ""

    async def answer(self, text, **kw):
        self.out.append((text, kw))
        return types.SimpleNamespace(message_id=2, delete=_noop)

    async def edit_text(self, text, **kw): self.out.append((text, kw))

    async def edit_reply_markup(self, **kw): pass

    async def delete(self): pass
    html_text = ""
    photo = document = None


async def _noop(): return None


class _Cb:
    def __init__(self, data, uid=BUYER):
        self.data, self.message, self.answers = data, _Msg(uid), []
        self.from_user = types.SimpleNamespace(id=uid, full_name="buyer", username="b")

    async def answer(self, text="", show_alert=False, **k): self.answers.append((text, show_alert))

    def model_copy(self, update): return self


def _fsm(uid=BUYER):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=5, chat_id=uid, user_id=uid))


async def _buy_trx(env, bot, amount="100"):
    await tenant.set_setting(BOT, "cx_trx_on", "1")
    await tenant.set_setting(BOT, "cx_trx_rate", "7000")
    st = _fsm()
    await CX.cx_coin(_Cb("cx:c:trx"), bot, st)
    await CX.cx_pick(_Cb(f"cx:q:trx:{amount}"), bot, st)
    m = _Msg()
    m.text = DEST
    await CX.cx_addr_in(m, bot, st)
    pay = _Cb("cx:pay")
    await CX.cx_pay(pay, bot, st)
    return pay


@pytest.mark.asyncio
async def test_purchase_with_autosend_on_delivers_without_the_admin(env):
    await _enable()
    bot = _Bot()
    await _buy_trx(env, bot)
    async with env.mk() as s:
        o = (await s.scalars(select(models.GiftOrder))).one()
    assert o.status == "sent" and o.tx_hash == "ab" * 32 and env.chain.prepared == [("trx", DEST, 10000)]
    buyer_msgs = [m for m in bot.sent if m[0] == BUYER]
    assert buyer_msgs and "ارسال شد" in buyer_msgs[0][1]
    kb = buyer_msgs[0][2]["reply_markup"]
    assert kb.inline_keyboard[0][0].url == "https://tronscan.org/#/transaction/" + "ab" * 32        # دکمهٔ هشِ تراکنش
    assert len(env.notes) == 1 and "خودکار ارسال شد" in env.notes[0][0]                             # فقط خبرِ اطلاعی؛ دکمهٔ تأیید لازم نیست
    assert env.notes[0][1] is None


@pytest.mark.asyncio
async def test_purchase_with_failed_autosend_gives_the_admin_reason_and_retry(env):
    await _enable()
    env.chain.trx = 1 * TC.SUN
    bot = _Bot()
    await _buy_trx(env, bot)
    async with env.mk() as s:
        o = (await s.scalars(select(models.GiftOrder))).one()
    assert o.status == "awaiting_admin"
    text, kb = env.notes[0]
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "ارسالِ خودکار انجام نشد" in text and f"cx:retry:{o.id}" in cbs and f"cx:done:{o.id}" in cbs and f"cx:refund:{o.id}" in cbs
    assert [m for m in bot.sent if m[0] == BUYER] == []                                              # به مشتری «ارسال شد» نرفت
    env.chain.trx = 150 * TC.SUN                                                                     # شارژ شد ⇒ «ارسالِ خودکارِ مجدد»
    retry = _Cb(f"cx:retry:{o.id}", uid=OWNER)
    await CX.cx_retry(retry, bot)
    async with env.mk() as s:
        assert (await s.get(models.GiftOrder, o.id)).status == "sent"
    assert any(m[0] == BUYER and "ارسال شد" in m[1] for m in bot.sent)


@pytest.mark.asyncio
async def test_unconfirmed_autosend_shows_hash_and_no_retry_button(env):
    await _enable()
    env.chain.bc_exc = TC.TronAmbiguous("TimeoutError")
    bot = _Bot()
    await _buy_trx(env, bot)
    async with env.mk() as s:
        o = (await s.scalars(select(models.GiftOrder))).one()
    assert o.status == "sending"
    text, kb = env.notes[0]
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    urls = [b.url for r in kb.inline_keyboard for b in r if b.url]
    assert "نامشخص" in text and f"cx:retry:{o.id}" not in cbs and urls == ["https://tronscan.org/#/transaction/" + "ab" * 32]
    # ادمین بعد از چکِ بلاک‌چین: «ارسال شد» ⇒ تأیید
    done = _Cb(f"cx:done:{o.id}", uid=OWNER)
    await CX.cx_done(done, bot)
    async with env.mk() as s:
        assert (await s.get(models.GiftOrder, o.id)).status == "sent"


@pytest.mark.asyncio
async def test_unconfirmed_order_can_be_refunded_by_the_admin_after_checking_the_chain(env):
    await _enable()
    env.chain.bc_exc = TC.TronAmbiguous("TimeoutError")
    bot = _Bot()
    await _buy_trx(env, bot)
    async with env.mk() as s:
        o = (await s.scalars(select(models.GiftOrder))).one()
        before = int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == BUYER))).balance)
    await CX.cx_refund(_Cb(f"cx:refund:{o.id}", uid=OWNER), bot)
    async with env.mk() as s:
        assert (await s.get(models.GiftOrder, o.id)).status == "refunded"
        assert int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == BUYER))).balance) == before + 700_000


@pytest.mark.asyncio
async def test_non_tron_network_orders_stay_manual_even_with_autosend_on(env):
    await _enable()
    await tenant.set_setting(BOT, "cx_usdt_on", "1")
    await tenant.set_setting(BOT, "cx_usdt_rate", "60000")
    await tenant.set_setting(BOT, "cx_usdt_net", "BEP20")
    bot, st = _Bot(), _fsm()
    await CX.cx_coin(_Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(_Cb("cx:q:usdt:10"), bot, st)
    m = _Msg()
    m.text = "0x" + "ab" * 20
    await CX.cx_addr_in(m, bot, st)
    await CX.cx_pay(_Cb("cx:pay"), bot, st)
    async with env.mk() as s:
        o = (await s.scalars(select(models.GiftOrder))).one()
    assert o.status == "awaiting_admin" and env.chain.prepared == []
    cbs = [b.callback_data for r in env.notes[0][1].inline_keyboard for b in r]
    assert cbs == [f"cx:done:{o.id}", f"cx:refund:{o.id}"]                                           # بدونِ پیامِ «خودکار انجام نشد»


# ───────────── پنلِ مالک ─────────────
@pytest.mark.asyncio
async def test_wallet_panel_create_toggle_caps_and_key_visibility(env):
    bot, st = _Bot(), _fsm(OWNER)
    home = _Cb("cx:adm", uid=OWNER)
    await CX.cx_adm(home, bot, st)
    cbs = [b.callback_data for r in home.message.out[0][1]["reply_markup"].inline_keyboard for b in r]
    assert "cx:w" in cbs and "ton:menu" in cbs                                                       # «یک جا»: تون + ترون/تتر + کیف‌پول
    assert "تون" in home.message.out[0][0]
    w = _Cb("cx:w", uid=OWNER)
    await CX.cx_wallet(w, bot, st)
    assert any("ساختِ" in b.text for r in w.message.out[0][1]["reply_markup"].inline_keyboard for b in r)
    no_wallet_toggle = _Cb("cx:w:auto", uid=OWNER)
    await CX.cx_wallet_auto(no_wallet_toggle, bot)
    assert no_wallet_toggle.answers[0][1] and not (await TA.settings(BOT))["enabled"]
    new = _Cb("cx:w:new", uid=OWNER)
    await CX.cx_wallet_new(new, bot)
    addr = await TA.wallet_address(BOT)
    assert addr and addr in new.message.out[0][0]
    await CX.cx_wallet_auto(_Cb("cx:w:auto", uid=OWNER), bot)
    assert (await TA.settings(BOT))["enabled"] is True
    again = _Cb("cx:w:new", uid=OWNER)
    await CX.cx_wallet_new(again, bot)
    assert again.answers[0][1] and await TA.wallet_address(BOT) == addr                               # دوباره نساخت
    await CX.cx_wallet_field(_Cb("cx:w:cap:usdt", uid=OWNER), bot, st)
    m = _Msg(OWNER)
    m.text = "۲۵۰"
    await CX.cx_set_save(m, bot, st)
    assert (await TA.settings(BOT))["cap_usdt"] == 250
    await CX.cx_wallet_field(_Cb("cx:w:apikey", uid=OWNER), bot, st)
    bad = _Msg(OWNER)
    bad.text = "not a key!"
    await CX.cx_set_save(bad, bot, st)
    assert "نامعتبر" in bad.out[0][0] and (await TA.settings(BOT))["api_key"] == ""
    good = _Msg(OWNER)
    good.text = "abcdef12-3456-7890-abcd-ef1234567890"
    await CX.cx_set_save(good, bot, st)
    assert (await TA.settings(BOT))["api_key"] == good.text


@pytest.mark.asyncio
async def test_private_key_is_shown_only_to_the_real_owner(env):
    await _enable(auto=False)
    async with env.mk() as s:
        s.add(models.AdminSupport(bot_id=BOT, telegram_id=ADMIN2, full_name="a"))
        await s.commit()
    bot = _Bot()
    sub_admin = _Cb("cx:w:key", uid=ADMIN2)
    await CX.cx_wallet_key(sub_admin, bot)
    assert sub_admin.message.out == [] and sub_admin.answers and sub_admin.answers[0][1]             # ادمینِ فرعی نمی‌بیند
    stranger = _Cb("cx:w:key", uid=BUYER)
    await CX.cx_wallet_key(stranger, bot)
    assert stranger.message.out == []
    owner = _Cb("cx:w:key", uid=OWNER)
    await CX.cx_wallet_key(owner, bot)
    key = await TA.reveal_private_key(BOT)
    assert key in owner.message.out[0][0]
