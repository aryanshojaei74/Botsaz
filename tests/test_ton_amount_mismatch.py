"""PATCH_TON_PROPORTIONAL_V1 + PATCH_TON_AMOUNT_ROUND_UP_V1 (SINA): کم/زیاد پرداخت‌شدنِ TON با memoِ درست."""
from decimal import Decimal

import pytest

from tenant.gateways import ton_direct as TD


def test_uniquify_never_goes_below_the_required_amount():
    for need in ("0.10149", "0.1013", "0.100001", "0.05", "1.23456789", "0.0511"):
        n = Decimal(need)
        for _ in range(300):
            assert TD._uniquify(n) >= n, need


def test_uniquify_still_unique_and_6_decimals():
    outs = {TD._uniquify(Decimal("0.2")) for _ in range(200)}
    assert len(outs) > 50 and all(o == o.quantize(Decimal("0.000001")) for o in outs)


def test_proportional_credit_rules():
    pc = TD.proportional_credit
    assert pc(100_000, "0.1", "0.1")[0] == 100_000                    # دقیق
    assert pc(100_000, "0.1", "0.0985")[0] == 100_000                 # ارفاقِ ۲٪
    assert pc(100_000, "0.1", "0.101")[0] == 100_000                  # ≤ +۲٪
    assert pc(100_000, "0.1", "0.15")[0] == 150_000                   # بیشتر ⇒ به‌نسبت
    assert pc(100_000, "0.1", "0.08")[0] == 80_000                    # کمتر ⇒ به‌نسبت
    assert pc(100_000, "0.1", "0.05")[0] == 50_000                    # دقیقاً ۵۰٪
    assert pc(100_000, "0.1", "0.049")[0] is None                     # زیرِ ۵۰٪ ⇒ دستی
    assert pc(100_000, "0.1", "0.31")[0] is None                      # بیش از ۳ برابر ⇒ دستی
    assert pc(100_000, "0", "0.1")[0] is None


def test_strict_match_charge_is_unchanged():
    import inspect
    src = inspect.getsource(TD._match_charge)
    assert "AMOUNT_TOLERANCE" in src and "PARTIAL" not in src


# ── ادغام: settle_bot با واریزِ کم/زیاد ──
import datetime as dt

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (config, tenant, TD):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        s.add(models.TonDirectCharge(bot_id=5, user_telegram_id=777, memo="BS111111", asset="TON", amount_toman=100_000,
                                     ton_amount=Decimal("0.1"), rate_toman=1_000_000, wallet_address="UQx", status="waiting",
                                     expires_at=dt.datetime.utcnow() + dt.timedelta(minutes=10)))
        await s.commit()

    async def addr(bot_id):
        return "UQx"
    monkeypatch.setattr(TD, "wallet_address", addr)
    sent = []

    class FakeBot:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))

        async def delete_message(self, *a, **k):
            pass
    yield maker, FakeBot(), sent
    await eng.dispose()


def _incoming(monkeypatch, value, comment="BS111111", h="tx1"):
    async def fetch(addr):
        return [{"hash": h, "comment": comment, "value_nano": int(Decimal(value) * TD.NANO)}]
    monkeypatch.setattr(TD, "fetch_incoming", fetch)


async def _bal(maker):
    async with maker() as s:
        return float((await s.get(models.BotUser, 1)).balance)


@pytest.mark.asyncio
async def test_exact_underpaid_and_overpaid_payments(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.1")
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 100_000.0       # دقیق: مثلِ قبل


@pytest.mark.asyncio
async def test_underpaid_is_credited_proportionally_not_ignored(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.08")                      # ۸۰٪ (قبلاً بی‌صدا رد می‌شد)
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 80_000.0
    assert any("80,000" in t for _c, t in sent)


@pytest.mark.asyncio
async def test_overpaid_is_credited_proportionally(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.15")                      # قبلاً فقط ۱۰۰٬۰۰۰ و بقیه می‌سوخت
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 150_000.0


@pytest.mark.asyncio
async def test_far_off_amount_is_not_credited_and_owner_is_told_once(env, monkeypatch):
    maker, bot, sent = env
    TD._OFF_AMOUNT_NOTIFIED.clear()
    _incoming(monkeypatch, "0.03")                      # ۳۰٪
    assert await TD.settle_bot(5, bot) == 0 and await _bal(maker) == 0
    assert await TD.settle_bot(5, bot) == 0
    owner_msgs = [t for c, t in sent if c == 1]
    assert len(owner_msgs) == 1 and "BS111111" in owner_msgs[0]


@pytest.mark.asyncio
async def test_same_tx_never_credits_twice(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.08")
    await TD.settle_bot(5, bot)
    await TD.settle_bot(5, bot)
    assert await _bal(maker) == 80_000.0


# ── PATCH_TON_WALLET_FRIENDLY_V1: مبلغِ لینک رند (۲ رقمِ اعشار) و اعتبارِ دقیق ──
def test_wallet_friendly_rounds_up_to_two_decimals_and_never_below():
    for need, expect in (("0.10194", "0.11"), ("0.101", "0.11"), ("0.10", "0.10"), ("0.0501", "0.06"),
                         ("1.2301", "1.24"), ("3", "3.00")):
        out = TD.wallet_friendly(Decimal(need))
        assert out == Decimal(expect) and out >= Decimal(need), need
    for _ in range(300):
        n = Decimal(str(round(0.05 + (secrets_random() * 5), 6)))
        out = TD.wallet_friendly(n)
        assert out >= n and out - n < Decimal("0.01") and out == out.quantize(Decimal("0.01"))


def secrets_random():
    import random
    return random.random()


def test_link_carries_the_exact_round_amount_in_nano():
    deep, web = TD.payment_links("UQabc", TD.wallet_friendly(Decimal("0.10194")), "BS123456")
    assert "amount=110000000" in deep and "amount=110000000" in web and "text=BS123456" in deep


def test_rounded_invoice_credit_matches_exact_payment():
    ton, amount = Decimal("0.10194"), 32000
    final = TD.wallet_friendly(ton)
    credit = int(Decimal(amount) * (final / ton))
    assert final == Decimal("0.11") and credit > amount                       # برایِ اضافهٔ گردکردن اعتبار می‌گیرد
    cr, ratio = TD.proportional_credit(credit, final, final)                  # پرداختِ دقیقِ مبلغِ لینک ⇒ همان اعتبار
    assert cr == credit and ratio == 1


def test_invoice_text_has_no_raw_ton_link_and_is_compact():
    import inspect
    src = inspect.getsource(TD.tond_amount)
    assert "لینک مستقیم" not in src and "{deep}" not in src            # لینکِ خام فقط پشتِ دکمهٔ شیشه‌ای
    assert "💎 پرداخت با Tonkeeper" in src and "callback_data=f\"tond:check:{memo}\"" in src


# ── PATCH_TON_CREDIT_NO_USER_V1 + PATCH_TON_DIAG_V1 ──
@pytest.mark.asyncio
async def test_missing_user_keeps_invoice_open_instead_of_silently_paying(env, monkeypatch):
    maker, bot, sent = env
    async with maker() as s:
        u = await s.get(models.BotUser, 1)
        await s.delete(u)
        await s.commit()
    _incoming(monkeypatch, "0.1")
    assert await TD.settle_bot(5, bot) == 0
    async with maker() as s:
        ch = (await s.scalars(select(models.TonDirectCharge))).first()
    assert ch.status == "waiting" and not ch.tx_hash             # فاکتور «paid» نشد؛ بعداً قابلِ تسویه است


@pytest.mark.asyncio
async def test_unmatched_memo_like_tx_is_logged_with_reason(env, monkeypatch, caplog):
    maker, bot, sent = env
    async with maker() as s:
        ch = (await s.scalars(select(models.TonDirectCharge))).first()
        ch.status = "expired"
        ch.expires_at = dt.datetime.utcnow() - dt.timedelta(hours=TD.LATE_GRACE_HOURS + 5)   # خارج از مهلتِ دیرهنگام
        s.add(models.TonDirectCharge(bot_id=5, user_telegram_id=777, memo="BS222222", asset="TON", amount_toman=1,
                                     ton_amount=Decimal("0.1"), rate_toman=1, wallet_address="UQx", status="waiting",
                                     expires_at=dt.datetime.utcnow() + dt.timedelta(minutes=5)))
        await s.commit()
    TD._UNMATCHED_LOGGED.clear()
    _incoming(monkeypatch, "0.19", comment="BS111111", h="txdiag1")
    import logging
    with caplog.at_level(logging.WARNING):
        assert await TD.settle_bot(5, bot) == 0
    assert any("BS111111" in r.getMessage() and "expired" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_late_expired_invoice_is_polled_and_credited(env, monkeypatch):
    """PATCH_TON_LATE_POLL_V1: فاکتورِ منقضی (در مهلتِ ۲۴ ساعته) که بعد از فعال‌شدنِ قابلیت منقضی شده، بررسی و شارژ شود؛
    فاکتورهایِ قبل از آن (احتمالاً دستی شارژ شده) نه."""
    maker, bot, sent = env
    async with maker() as s:
        ch = (await s.scalars(select(models.TonDirectCharge))).first()
        ch.status = "expired"
        ch.expires_at = dt.datetime.utcnow() - dt.timedelta(hours=3)
        await s.commit()
    assert await TD.late_bot_ids() == []                       # پیش‌فرض خاموش است (بدونِ شارژِ خودکارِ تازه)
    async with maker() as s:
        s.add(models.Setting(bot_id=0, key="ton_late_poll_enabled", value="1"))
        await s.commit()
    # بار اولِ فعال‌شدن: نشانه همین حالا ثبت می‌شود ⇒ فاکتورِ ۳ ساعت پیش عمداً نادیده گرفته می‌شود (ممکن است دستی شارژ شده باشد)
    assert await TD.late_bot_ids() == []
    async with maker() as s:
        row = await s.scalar(select(models.Setting).where(models.Setting.bot_id == 0, models.Setting.key == TD.LATE_POLL_MARKER))
        assert row is not None
        row.value = (dt.datetime.utcnow() - dt.timedelta(hours=5)).isoformat()   # فعال‌سازی ۵ ساعت پیش ⇒ این فاکتور بعد از آن منقضی شده
        await s.commit()
    assert await TD.late_bot_ids() == [5]
    _incoming(monkeypatch, "0.1", h="latetx")
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 100_000.0
    assert await TD.late_bot_ids() == []                       # بعد از تسویه دیگر لازم نیست
    async with maker() as s:
        ch = (await s.scalars(select(models.TonDirectCharge))).first()
        ch.status, ch.tx_hash = "expired", None
        ch.expires_at = dt.datetime.utcnow() - dt.timedelta(hours=TD.LATE_GRACE_HOURS + 1)
        await s.commit()
    assert await TD.late_bot_ids() == []                       # خارج از مهلتِ ۲۴ ساعته ⇒ نه


def test_extract_comment_fallbacks():
    import base64
    ex = TD._extract_comment
    assert ex({"message": "BS5760C0"}) == "BS5760C0"                                  # حالتِ عادی
    assert ex({"message": "", "msg_data": {"@type": "msg.dataText", "text": base64.b64encode(b"BS5760C0").decode()}}) == "BS5760C0"
    boc = b"\xb5\xee\x9c\x72\x01\x01\x01\x00\x00\x00\x00\x00BS5760C0\x00"            # بدنهٔ خام که حاویِ متن است
    assert ex({"message": "", "msg_data": {"@type": "msg.dataRaw", "body": base64.b64encode(boc).decode()}}) == "BS5760C0"
    assert ex({"message": "سلام", "msg_data": {"text": base64.b64encode(b"BS1A2B3C").decode()}}) == "سلام BS1A2B3C"
    assert ex({"message": ""}) == "" and ex({}) == ""
    assert ex({"message": "hello", "msg_data": {"body": "!!notbase64!!"}}) == "hello"


def test_toncenter_response_with_invalid_utf8_is_still_parsed():
    """علتِ واقعیِ شارژ نشدنِ امیر/سجاد: بایتِ 0xEC نامعتبر در کامنتِ یک تراکنشِ اسپم ⇒ UnicodeDecodeError ⇒ هیچ واریزی دیده نمی‌شد."""
    import json
    good = {"ok": True, "result": [
        {"transaction_id": {"hash": "h1"}, "in_msg": {"value": "1070000000", "message": "BS5760C0", "source": "EQx"}},
        {"transaction_id": {"hash": "spam"}, "in_msg": {"value": "1", "message": "JUNK", "source": "EQy"}}]}
    raw = json.dumps(good, ensure_ascii=False).encode("utf-8").replace(b"JUNK", b"\xec\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")                         # همان خطای لاگِ سرور
    data = TD._json_lenient(raw)
    assert data["ok"] and data["result"][0]["in_msg"]["message"] == "BS5760C0"
    assert TD._extract_comment(data["result"][0]["in_msg"]) == "BS5760C0"
