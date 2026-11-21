"""PATCH_VS_DISCOUNT_V1 — کد تخفیف برایِ استارز/تون/گیفت/پرمیوم/شماره + محدودیتِ دسته‌هایِ محصول."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.smm as SMM
import tenant.bots.virtual_service.vs_discount as VSD
import tenant.gateways.discount_engine as DE


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SMM, VSD, DE):
        monkeypatch.setattr(mod, "Session", maker)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(tenant, "_notify_admins", _noop)
    monkeypatch.setattr(SMM, "_notify_manual_order", _noop)
    monkeypatch.setattr(SMM, "_notify_gift_order_trust_success", _noop)
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="vs", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=500, balance=100_000))
        s.add(models.DiscountCode(id=1, bot_id=7, code="STAR10", percent=10, disc_type="percent",
                                  product_keys="stars"))
        s.add(models.DiscountCode(id=2, bot_id=7, code="ALL10", percent=10, disc_type="percent"))
        await s.commit()
    yield maker
    await engine.dispose()


def test_keys_roundtrip_and_label():
    assert VSD.parse_keys("stars,gift,bogus") == {"stars", "gift"}
    assert VSD.dump_keys({"gift", "stars"}) == "stars,gift"       # ترتیبِ ثابتِ نمایش
    assert VSD.dump_keys(set()) is None
    assert VSD.keys_label(None) == "همهٔ محصولات"
    assert "استارز" in VSD.keys_label("stars")


@pytest.mark.asyncio
async def test_engine_respects_product_keys(db):
    r = await DE.validate_discount(bot_id=7, code="STAR10", telegram_id=500, price=60_000, product_key="stars")
    assert r.ok and r.final_price == 54_000
    for bad in ("gift", "vnum", None):                    # دستهٔ دیگر / جریانی که دسته را نمی‌داند (VPN/فروشگاه)
        r = await DE.validate_discount(bot_id=7, code="STAR10", telegram_id=500, price=60_000, product_key=bad)
        assert not r.ok, bad
    # کدِ بدونِ دسته رویِ همه کار می‌کند (رفتارِ قبلی)
    for k in ("stars", "ton", "gift", "premium", "vnum", "smm", None):
        r = await DE.validate_discount(bot_id=7, code="ALL10", telegram_id=500, price=60_000, product_key=k)
        assert r.ok, k


@pytest.mark.asyncio
async def test_stars_wallet_order_applies_code_atomically_and_revert(db):
    row = await _row(db)
    res = await SMM.place_stars_order(
        None, row, buyer_tg_id=500, buyer_name="u", buyer_username="u", dest_id=500, qty=50,
        disc_code="STAR10")
    async with db() as s:
        o = await s.get(models.GiftOrder, res["order_id"])
        b = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 500))
        dc = await s.get(models.DiscountCode, 1)
    assert o.price_toman == 54_000 and o.discount_code_id == 1 and o.discount_amount == 6_000
    assert int(b.balance) == 100_000 - 54_000
    assert dc.used_count == 1
    async with db() as s:                                  # ردِ ادمین ⇒ مصرفِ کد برمی‌گردد
        o = await s.get(models.GiftOrder, res["order_id"])
        await VSD.vsd_revert_order(s, o, 500)
        await s.commit()
    async with db() as s:
        assert (await s.get(models.DiscountCode, 1)).used_count == 0


@pytest.mark.asyncio
async def test_stars_wrong_category_code_is_rejected_without_charging(db):
    row = await _row(db)
    with pytest.raises(SMM.StarsOrderError):
        await SMM.place_stars_order(None, row, buyer_tg_id=500, buyer_name="u", buyer_username="u",
                                    dest_id=500, qty=50, disc_code="NOPE")
    async with db() as s:
        b = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 500))
    assert int(b.balance) == 100_000


def test_vsd_pick_only_matches_same_message_and_kind():
    d = {"vsd": {"kind": "gift", "code": "X", "msg_id": 10}}
    assert VSD.vsd_pick(d, 10, "gift") == "X"
    assert VSD.vsd_pick(d, 11, "gift") is None          # فاکتورِ دیگر
    assert VSD.vsd_pick(d, 10, "stars") is None         # دستهٔ دیگر
    assert VSD.vsd_pick({}, 10, "gift") is None


async def _row(db):
    async with db() as s:
        return await s.get(models.Bot, 7)


# ═══════════ PATCH_FRAGMENT_AUTO_STARS_V1 — فروشِ خودکارِ استارز با ولتِ فرگمنت ═══════════
import asyncio
import types

import pyfragment
import tenant.bots.virtual_service.fragment_stars as FG


class _FakeClient:
    """جایگزینِ pyfragment.FragmentClient؛ behaviour = تابعی که نتیجه/خطا می‌دهد."""
    calls = []
    behaviour = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def purchase_stars(self, username, amount, show_sender=True, **k):
        _FakeClient.calls.append((username, amount, show_sender))
        return await _FakeClient.behaviour(username, amount)


async def _fg_setup(db, monkeypatch, *, dest_label="👤 یوزرنیم: @alice", qty=100, behaviour=None):
    _FakeClient.calls = []
    _FakeClient.behaviour = behaviour

    async def _client(_w):
        return _FakeClient()
    monkeypatch.setattr(FG, "_get_fragment_client", _client)
    sent = []

    class _Bot:
        async def send_message(self, chat_id, text, **k):
            sent.append((chat_id, text))
    async with db() as s:
        s.add(models.FragmentWallet(bot_id=7, address="UQx", encrypted_mnemonic="m",
                                    encrypted_fragment_cookies="c", encrypted_tonapi_key="k", enabled=True))
        s.add(models.GiftOrder(id=11, bot_id=7, buyer_user_id=1, gift_id=f"starsell_{qty}", gift_emoji="⭐",
                               star_count=qty, price_toman=1000, dest_telegram_id=500, dest_label=dest_label,
                               status="awaiting_admin", pay_method="wallet", tracking_code="T1"))
        await s.commit()
    async with db() as s:
        row = await s.get(models.Bot, 7)
    return row, _Bot(), sent


async def _status(db):
    async with db() as s:
        return (await s.get(models.GiftOrder, 11)).status


@pytest.mark.asyncio
async def test_fragment_auto_success_marks_sent_and_hides_sender(db, monkeypatch):
    async def ok(u, a):
        return types.SimpleNamespace(transaction_id="TX1", username=u, amount=a, confirmed=True)
    row, bot, sent = await _fg_setup(db, monkeypatch, behaviour=ok)
    await SMM.fragment_fulfil_order(bot, row, 11)
    assert _FakeClient.calls == [("alice", 100, False)]          # فرستنده مخفی
    assert await _status(db) == "sent"
    assert sent and "تکمیل" in sent[0][1]                         # خبر به مشتری


@pytest.mark.asyncio
async def test_fragment_auto_clean_failure_reverts_to_manual(db, monkeypatch):
    async def nf(u, a):
        raise pyfragment.UserNotFoundError("nope")
    row, bot, sent = await _fg_setup(db, monkeypatch, behaviour=nf)
    await SMM.fragment_fulfil_order(bot, row, 11)
    assert await _status(db) == "awaiting_admin"                  # دستی؛ هیچ پولی خرج نشده
    assert not sent


@pytest.mark.asyncio
async def test_fragment_auto_ambiguous_failure_never_double_sends(db, monkeypatch):
    async def boom(u, a):
        raise pyfragment.TransactionError("broadcast?")
    row, bot, sent = await _fg_setup(db, monkeypatch, behaviour=boom)
    notes = []

    async def _n(bot_, row_, text, kb=None, **k):
        notes.append(text)
    monkeypatch.setattr(SMM, "_notify_manual_order", _n)
    await SMM.fragment_fulfil_order(bot, row, 11)
    assert await _status(db) == "awaiting_admin"
    assert any("نامشخص" in t and "تراکنش" in t for t in notes)    # هشدارِ «اول ولت را چک کن»
    assert len(_FakeClient.calls) == 1                            # هیچ retryِ خودکاری


@pytest.mark.asyncio
async def test_fragment_auto_claim_prevents_second_run(db, monkeypatch):
    async def slow(u, a):
        await asyncio.sleep(0.05)
        return types.SimpleNamespace(transaction_id="TX", username=u, amount=a, confirmed=True)
    row, bot, _ = await _fg_setup(db, monkeypatch, behaviour=slow)
    await asyncio.gather(SMM.fragment_fulfil_order(bot, row, 11), SMM.fragment_fulfil_order(bot, row, 11))
    assert len(_FakeClient.calls) == 1                            # فقط یک خرید
    assert await _status(db) == "sent"


@pytest.mark.asyncio
async def test_fragment_auto_guards_no_username_and_limits(db, monkeypatch):
    async def ok(u, a):
        raise AssertionError("نباید خرید صدا زده شود")
    row, bot, _ = await _fg_setup(db, monkeypatch, dest_label="🆔 123456", behaviour=ok)   # بدونِ یوزرنیم
    await SMM.fragment_fulfil_order(bot, row, 11)
    assert await _status(db) == "awaiting_admin" and not _FakeClient.calls


@pytest.mark.asyncio
async def test_fragment_auto_respects_max_and_min(db, monkeypatch):
    async def ok(u, a):
        raise AssertionError("نباید خرید صدا زده شود")
    row, bot, _ = await _fg_setup(db, monkeypatch, qty=SMM._FG_AUTO_MAX_STARS + 1, behaviour=ok)
    await SMM.fragment_fulfil_order(bot, row, 11)
    assert await _status(db) == "awaiting_admin" and not _FakeClient.calls


@pytest.mark.asyncio
async def test_fragment_auto_toggle_requires_connected_wallet(db, monkeypatch):
    row, _bot, _ = await _fg_setup(db, monkeypatch, behaviour=None)
    assert await SMM._fg_auto_on(7) is False                      # خاموش (پیش‌فرض)
    await tenant.set_setting(7, "fragment_auto", "1")
    assert await SMM._fg_auto_on(7) is True
    async with db() as s:                                         # ولتِ نیمه‌وصل ⇒ هرگز خودکار نمی‌شود
        w = await s.scalar(select(models.FragmentWallet))
        w.encrypted_fragment_cookies = None
        await s.commit()
    assert await SMM._fg_auto_on(7) is False


# ═══════════ تخفیفِ اولین خرید روی استارز (قبلاً فقط SMM/تون/پریمیوم/ری‌اکشن داشتند) ═══════════
@pytest.mark.asyncio
async def test_stars_wallet_first_purchase_discount_applies_once(db):
    async with db() as s:
        for k, v in (("firstbuy_on", "1"), ("firstbuy_type", "percent"), ("firstbuy_value", "10")):
            s.add(models.Setting(bot_id=7, key=k, value=v))
        (await s.get(models.BotUser, 1)).balance = 5_000_000
        await s.commit()
    row = await _row(db)
    r1 = await SMM.place_stars_order(None, row, buyer_tg_id=500, buyer_name="u", buyer_username="u",
                                     dest_id=500, qty=50)
    r2 = await SMM.place_stars_order(None, row, buyer_tg_id=500, buyer_name="u", buyer_username="u",
                                     dest_id=500, qty=50)
    assert r1["price"] < r2["price"] and r1["price"] == round(r2["price"] * 0.9)     # فقط خریدِ اول
    # قیمتِ API (override) هرگز تخفیفِ اولین خرید نمی‌گیرد
    async with db() as s:
        s.add(models.BotUser(id=9, bot_id=7, telegram_id=600, balance=100_000))
        await s.commit()
    r3 = await SMM.place_stars_order(None, row, buyer_tg_id=600, buyer_name="v", buyer_username="v",
                                     dest_id=600, qty=50, price_override=20_000)
    assert r3["price"] == 20_000


@pytest.mark.asyncio
async def test_abandoned_card_order_does_not_cancel_first_purchase_discount(db):
    async with db() as s:
        s.add(models.GiftOrder(bot_id=7, buyer_user_id=1, gift_id="starsell_50", star_count=50, price_toman=50000,
                               dest_telegram_id=500, dest_label="x", status="awaiting_receipt", pay_method="card"))
        await s.commit()
    assert await DE.has_previous_purchase(7, 500) is False
    async with db() as s:
        s.add(models.GiftOrder(bot_id=7, buyer_user_id=1, gift_id="starsell_50", star_count=50, price_toman=50000,
                               dest_telegram_id=500, dest_label="x", status="awaiting_admin", pay_method="card"))
        await s.commit()
    assert await DE.has_previous_purchase(7, 500) is True
