"""PATCH_TON_DIRECT_MANAGE_V1 (Royal): پرداخت TON مستقیم — تأیید/ردِ دستی، نمایشِ کاملِ تراکنش (مبدأ/TXID/Memo/زمان/وضعیت)، جلوگیری از ثبتِ تکراری."""
import base64
import types
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.gateways import ton_direct as TD

OWNER, USER = 1000, 111
BOT = 5
HEX1 = "ab" * 32
HEX2 = "cd" * 32
B64_1 = base64.b64encode(bytes.fromhex(HEX1)).decode()


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
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=BOT, telegram_id=USER, full_name="کاربر", username="u1", balance=Decimal(0)))
        s.add(models.TonDirectCharge(id=1, bot_id=BOT, user_telegram_id=USER, memo="AAAA0001", amount_toman=500_000,
                                     ton_amount=Decimal("1.234"), status="waiting", created_at=datetime.utcnow(),
                                     expires_at=datetime.utcnow() + timedelta(hours=1)))
        s.add(models.TonDirectCharge(id=2, bot_id=BOT, user_telegram_id=USER, memo="BBBB0002", amount_toman=300_000,
                                     ton_amount=Decimal("0.8"), status="expired", created_at=datetime.utcnow(),
                                     expires_at=datetime.utcnow() - timedelta(hours=5)))
        await s.commit()

    async def gbr(b):
        async with mk() as s:
            return await s.get(models.Bot, BOT)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    r = await gbr(None)

    async def ok(cb, bot): return r if cb.from_user.id == OWNER else None
    monkeypatch.setattr(tenant, "_owner_ok", ok)
    yield types.SimpleNamespace(mk=mk)
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


class Bot_:
    def __init__(self): self.sent = []

    async def send_message(self, chat_id, text, **kw): self.sent.append((chat_id, text))


class Msg:
    def __init__(self, text="", uid=OWNER):
        self.text, self.out = text, []
        self.from_user = types.SimpleNamespace(id=uid)

    async def answer(self, text, **kw):
        self.out.append((text, kw))

    async def edit_text(self, text, **kw): self.out.append((text, kw))


class Cb:
    def __init__(self, data, uid=OWNER):
        self.data, self.message, self.answers = data, Msg(), []
        self.from_user = types.SimpleNamespace(id=uid)

    async def answer(self, text="", show_alert=False, **k): self.answers.append((text, show_alert))


def fsm(uid=OWNER):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=5, chat_id=uid, user_id=uid))


async def charge(env, cid):
    async with env.mk() as s:
        return await s.get(models.TonDirectCharge, cid)


async def balance(env):
    async with env.mk() as s:
        return int((await s.scalar(select(models.BotUser))).balance)


def kb_cbs(kb):
    return [(b.callback_data or b.url) for r in kb.inline_keyboard for b in r]


def test_normalize_txid_accepts_hex_and_base64_into_one_canonical_form():
    assert TD.normalize_txid(HEX1) == B64_1 and TD.normalize_txid(HEX1.upper()) == B64_1
    assert TD.normalize_txid(B64_1) == B64_1
    url_safe = B64_1.replace("+", "-").replace("/", "_")
    assert TD.normalize_txid(url_safe) == B64_1
    for bad in ("", "abc", "zz" * 32, HEX1[:-1], "g" * 64, None):
        assert TD.normalize_txid(bad) is None


@pytest.mark.asyncio
async def test_invoice_list_has_a_detail_button_per_invoice(env):
    cb = Cb("tond:invoices")
    await TD.tond_invoices(cb, Bot_(), fsm())
    cbs = kb_cbs(cb.message.out[-1][1]["reply_markup"])
    assert "tond:inv:1" in cbs and "tond:inv:2" in cbs


@pytest.mark.asyncio
async def test_detail_shows_amount_source_full_txid_memo_time_status(env):
    async with env.mk() as s:
        c = await s.get(models.TonDirectCharge, 1)
        c.status, c.tx_hash, c.from_address = "paid", B64_1, "UQAbc123sourceAddress"
        c.paid_at, c.paid_via = datetime(2026, 11, 19, 20, 5), "auto"
        await s.commit()
    cb = Cb("tond:inv:1")
    await TD.tond_invoice_detail(cb, Bot_(), fsm())
    text, kw = cb.message.out[-1]
    for need in ("AAAA0001", "500,000", "1.234", "UQAbc123sourceAddress", B64_1, "2026-11-19 20:05", "خودکار", "پرداخت‌شده"):
        assert need in text, need
    urls = kb_cbs(kw["reply_markup"])
    assert f"https://tonviewer.com/transaction/{HEX1}" in urls and "https://tonviewer.com/UQAbc123sourceAddress" in urls
    assert not any(str(u).startswith("tond:man") or str(u).startswith("tond:rej") for u in urls)       # پرداخت‌شده: تأیید/ردِ دستی ندارد


@pytest.mark.asyncio
async def test_auto_settlement_records_the_source_address_and_time(env, monkeypatch):
    async def addr(bid): return "UQwallet"

    async def incoming(address, limit=100):
        return [{"hash": B64_1, "value_nano": 1_234_000_000, "comment": "AAAA0001", "source": "UQpayerSource"}]
    monkeypatch.setattr(TD, "wallet_address", addr)
    monkeypatch.setattr(TD, "fetch_incoming", incoming)
    assert await TD.settle_bot(BOT, None) == 1
    c = await charge(env, 1)
    assert c.status == "paid" and c.from_address == "UQpayerSource" and c.paid_via == "auto" and c.paid_at and c.tx_hash == B64_1
    assert await balance(env) == 500_000


@pytest.mark.asyncio
async def test_manual_approval_with_txid_credits_once_and_notifies_the_user(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_start(Cb("tond:man:1"), bot, st)
    msg = Msg(HEX1)
    await TD.tond_manual_txid(msg, bot, st)
    assert "تأییدِ نهایی" in msg.out[-1][0] and "tond:manok:1" in kb_cbs(msg.out[-1][1]["reply_markup"])
    assert await balance(env) == 0                                              # هنوز تأیید نشده
    ok = Cb("tond:manok:1")
    await TD.tond_manual_ok(ok, bot, st)
    c = await charge(env, 1)
    assert c.status == "paid" and c.tx_hash == B64_1 and c.paid_via == "manual" and c.paid_at
    assert await balance(env) == 500_000
    assert bot.sent and bot.sent[0][0] == USER and "تایید شد" in bot.sent[0][1]
    again = Cb("tond:manok:1")
    await TD.tond_manual_ok(again, bot, st)
    assert await balance(env) == 500_000                                         # دوبار نیامد (جلسه پاک شده)
    async with env.mk() as s:
        kinds = [t.kind for t in (await s.scalars(select(models.WalletTxn))).all()]
    assert kinds == ["ton_direct"]


@pytest.mark.asyncio
async def test_the_same_txid_can_never_be_registered_twice(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_start(Cb("tond:man:1"), bot, st)
    await TD.tond_manual_txid(Msg(HEX1), bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:1"), bot, st)
    # همان تراکنش (هم‌قالب یا با قالبِ دیگر) برایِ فاکتورِ دیگر: رد
    await TD.tond_manual_start(Cb("tond:man:2"), bot, st)
    for same in (HEX1, B64_1, HEX1.upper()):
        m = Msg(same)
        await TD.tond_manual_txid(m, bot, st)
        assert "قبلاً" in m.out[0][0] and "AAAA0001" in m.out[0][0]
    assert await balance(env) == 500_000 and (await charge(env, 2)).status == "expired"
    other = Msg(HEX2)                                                              # TXIDِ دیگر مجاز است
    await TD.tond_manual_txid(other, bot, st)
    assert "تأییدِ نهایی" in other.out[0][0]


@pytest.mark.asyncio
async def test_auto_loop_never_reuses_a_txid_that_was_approved_manually(env, monkeypatch):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_start(Cb("tond:man:2"), bot, st)
    await TD.tond_manual_txid(Msg(HEX1), bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:2"), bot, st)                           # فاکتور ۲ دستی با HEX1
    assert await balance(env) == 300_000

    async def addr(bid): return "UQwallet"

    async def incoming(address, limit=100):                                        # همان واریز، این‌بار با memoِ فاکتور ۱
        return [{"hash": B64_1, "value_nano": 1_234_000_000, "comment": "AAAA0001", "source": "UQx"}]
    monkeypatch.setattr(TD, "wallet_address", addr)
    monkeypatch.setattr(TD, "fetch_incoming", incoming)
    assert await TD.settle_bot(BOT, None) == 0
    assert (await charge(env, 1)).status == "waiting" and await balance(env) == 300_000


@pytest.mark.asyncio
async def test_manual_approval_without_a_txid_uses_a_per_invoice_marker(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_notx(Cb("tond:mannotx:1"), bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:1"), bot, st)
    c = await charge(env, 1)
    assert c.status == "paid" and c.tx_hash == "manual-1" and await balance(env) == 500_000
    urls = kb_cbs(TD.invoice_kb(c))
    assert not any("tonviewer.com/transaction" in str(u) for u in urls)              # TXIDِ ساختگی لینکِ بلاک‌چین نمی‌شود


@pytest.mark.asyncio
async def test_invalid_txid_is_rejected_and_nothing_is_credited(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_start(Cb("tond:man:1"), bot, st)
    m = Msg("not-a-hash")
    await TD.tond_manual_txid(m, bot, st)
    assert "نامعتبر" in m.out[0][0] and await balance(env) == 0 and (await charge(env, 1)).status == "waiting"
    done = Cb("tond:manok:1")
    await TD.tond_manual_ok(done, bot, st)                                          # بدونِ txِ معتبر در جلسه
    assert done.answers[0][1] and await balance(env) == 0


@pytest.mark.asyncio
async def test_expired_invoices_can_be_approved_manually_and_paid_ones_cannot(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_notx(Cb("tond:mannotx:2"), bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:2"), bot, st)
    assert (await charge(env, 2)).status == "paid" and await balance(env) == 300_000
    again = Cb("tond:man:2")
    await TD.tond_manual_start(again, bot, st)
    assert again.answers[0][1] and "قبلاً" in again.answers[0][0]


@pytest.mark.asyncio
async def test_reject_marks_the_invoice_notifies_the_user_and_blocks_later_auto_matching(env, monkeypatch):
    bot = Bot_()
    ask = Cb("tond:rej:1")
    await TD.tond_reject_ask(ask, bot)
    assert "tond:rejok:1" in kb_cbs(ask.message.out[-1][1]["reply_markup"]) and (await charge(env, 1)).status == "waiting"
    ok = Cb("tond:rejok:1")
    await TD.tond_reject_ok(ok, bot)
    assert (await charge(env, 1)).status == "rejected"
    assert bot.sent and bot.sent[0][0] == USER and "رد شد" in bot.sent[0][1]
    assert "ردشده" in ok.message.out[-1][0]

    async def addr(bid): return "UQwallet"

    async def incoming(address, limit=100):
        return [{"hash": B64_1, "value_nano": 1_234_000_000, "comment": "AAAA0001", "source": "UQx"}]
    monkeypatch.setattr(TD, "wallet_address", addr)
    monkeypatch.setattr(TD, "fetch_incoming", incoming)
    await TD.settle_bot(BOT, None)
    assert (await charge(env, 1)).status == "rejected" and await balance(env) == 0     # ردشده با واریزِ بعدی خودکار شارژ نمی‌شود
    twice = Cb("tond:rejok:1")
    await TD.tond_reject_ok(twice, bot)
    assert twice.answers[0][1] and len(bot.sent) == 1


@pytest.mark.asyncio
async def test_paid_invoice_cannot_be_rejected(env):
    st, bot = fsm(), Bot_()
    await TD.tond_manual_notx(Cb("tond:mannotx:1"), bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:1"), bot, st)
    ask = Cb("tond:rej:1")
    await TD.tond_reject_ask(ask, bot)
    assert ask.answers[0][1] and ask.message.out == []
    ok = Cb("tond:rejok:1")
    await TD.tond_reject_ok(ok, bot)
    assert (await charge(env, 1)).status == "paid" and await balance(env) == 500_000


@pytest.mark.asyncio
async def test_non_owner_cannot_approve_or_reject(env):
    st, bot = fsm(USER), Bot_()
    for fn, data in ((TD.tond_manual_start, "tond:man:1"), (TD.tond_manual_notx, "tond:mannotx:1"),
                     (TD.tond_manual_ok, "tond:manok:1"), (TD.tond_reject_ask, "tond:rej:1"), (TD.tond_reject_ok, "tond:rejok:1")):
        cb = Cb(data, uid=USER)
        await (fn(cb, bot, st) if fn in (TD.tond_manual_start, TD.tond_manual_notx, TD.tond_manual_ok) else fn(cb, bot))
    assert (await charge(env, 1)).status == "waiting" and await balance(env) == 0 and bot.sent == []


@pytest.mark.asyncio
async def test_another_bots_invoice_is_not_reachable(env):
    async with env.mk() as s:
        s.add(models.Bot(id=9, owner_id=7, token="9:T", username="b2", field="vpn_sales", plan="pro", status="active"))
        s.add(models.TonDirectCharge(id=9, bot_id=9, user_telegram_id=USER, memo="OTHER009", amount_toman=1, ton_amount=Decimal("1"),
                                     status="waiting", created_at=datetime.utcnow()))
        await s.commit()
    st, bot = fsm(), Bot_()
    cb = Cb("tond:man:9")
    await TD.tond_manual_start(cb, bot, st)
    assert cb.answers[0][1] and (await charge(env, 9)).status == "waiting"
    notx = Cb("tond:mannotx:9")
    await TD.tond_manual_notx(notx, bot, st)
    await TD.tond_manual_ok(Cb("tond:manok:9"), bot, st)
    rej = Cb("tond:rejok:9")
    await TD.tond_reject_ok(rej, bot)
    assert (await charge(env, 9)).status == "waiting" and await balance(env) == 0
