"""
تست‌هایِ حداقل/حداکثرِ واریزِ هر درگاه (PATCH_WALLET_LIMITS_V1).

درخواستِ ادمین (فدایی): «گزینه حداقل واریز و حداکثر واریز برای هر درگاه نیست که بتونیم تنظیم کنیم».
هندلرهایِ واقعیِ هر درگاه با آپدیتِ ساختگی و دیتابیسِ موقت اجرا می‌شوند؛ نکتهٔ اصلی هم این است که
بدونِ تنظیمِ ادمین هیچ رفتاری نسبت به قبل عوض نشده باشد.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.gateways import wallet_limits as WL

BOT_TG = 8354597347
CUSTOMER = 555001
ADMIN = 777


# ═════════════════════════ ابزارهایِ تست ═════════════════════════

def tg_user(uid, *, is_bot=False, name="Ali"):
    return SimpleNamespace(id=uid, is_bot=is_bot, full_name=name, first_name=name, username=None)


class Msg:
    def __init__(self, from_user, text=""):
        self.from_user = from_user
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text="", **kw):
        self.sent.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        self.sent.append((text, kw))

    async def delete(self): ...


class Cb:
    def __init__(self, data, presser):
        self.data = data
        self.from_user = presser
        self.message = Msg(tg_user(BOT_TG, is_bot=True))
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


class State:
    def __init__(self):
        self.cur, self.data = None, {}

    async def clear(self): self.cur, self.data = None, {}
    async def set_state(self, s): self.cur = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)


class FakeAiogramBot:
    token = f"{BOT_TG}:AAA"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    from tenant.gateways import (zarinpal_wallet, zibal_wallet, hesabpay, card_wallet, tonpays,
                                 crypto_admin, wallet_limits_admin)
    for mod in (config, tenant, zarinpal_wallet, zibal_wallet, hesabpay, tonpays):
        if hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._OWN_TG_ID_CACHE.clear()
    async with maker() as s:
        bot = models.Bot(owner_id=ADMIN, token=f"{BOT_TG}:AAA", username="ExpressPanel_Bot",
                         field="virtual_service", plan="pro")
        s.add(bot)
        await s.flush()
        s.add(models.BotUser(bot_id=bot.id, telegram_id=CUSTOMER, full_name="Meli", balance=0))
        await s.commit()
        row = SimpleNamespace(id=bot.id, username="ExpressPanel_Bot", token=bot.token, owner_id=ADMIN,
                              field="virtual_service", plan="pro", status="active")

    async def fake_get_bot_row(_bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", fake_get_bot_row)
    yield maker, row
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


async def _orders(maker):
    async with maker() as s:
        return (await s.scalars(select(models.Order))).all()


async def _set(bot_id, **kv):
    import tenant
    for k, v in kv.items():
        await tenant.set_setting(bot_id, k, str(v))


def _texts(msg_or_cb):
    m = getattr(msg_or_cb, "message", msg_or_cb)
    return " ".join(t for t, _ in m.sent)


def _preset_amounts(kb, prefix):
    out = []
    for row in kb.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith(prefix):
                tail = b.callback_data.rsplit(":", 1)[1]
                if tail.isdigit():                       # «custom» و مانندِ آن مبلغ نیستند
                    out.append(int(tail))
    return out


# ═════════════════════════ ۱) پیش‌فرض‌ها = رفتارِ قبلی ═════════════════════════

def test_defaults_equal_the_old_hardcoded_minimums():
    """اگر ادمین چیزی تنظیم نکند، حداقلِ هر درگاه دقیقاً همان عددِ ثابتِ قبلیِ کد است."""
    from tenant.gateways import tonpays, uniquepay, abangateway
    expected = {"card": 10_000, "zarinpal": 20_000, "zibal": 10_000, "hesabpay": 1_000,
                "crypto": 100_000, "swap": 10_000,
                "tonpays": tonpays.MIN_AMOUNT, "uniquepay": uniquepay.MIN_AMOUNT,
                "abangateway": abangateway.MIN_AMOUNT}
    assert {k: WL.GATEWAYS[k].default_min for k in expected} == expected
    assert uniquepay.MIN_AMOUNT == 51_000 and tonpays.MIN_AMOUNT == 20_000


def test_gateways_that_already_had_settings_reuse_the_same_keys():
    """دو منبعِ حقیقت نداشته باشیم: صفحهٔ خودِ درگاه و صفحهٔ مشترک یک کلید را می‌خوانند."""
    assert WL.GATEWAYS["uniquepay"].min_key == "uniquepay_min_amount"
    assert WL.GATEWAYS["uniquepay"].max_key == "uniquepay_max_amount"
    assert WL.GATEWAYS["abangateway"].min_key == "abangateway_min_amount"
    assert WL.GATEWAYS["abangateway"].max_key == "abangateway_max_amount"
    assert WL.GATEWAYS["swap"].min_key == "swap_min_toman" and WL.GATEWAYS["swap"].max_key == "swap_max_toman"
    assert WL.GATEWAYS["tonpays"].min_key == "tonpays_min_amount"


@pytest.mark.asyncio
async def test_unconfigured_bot_has_default_min_and_no_cap(db):
    maker, row = db
    for key, g in WL.GATEWAYS.items():
        assert await WL.get_limits(row.id, key) == (g.default_min, 0), key
        assert await WL.customized(row.id, key) is False, key


# ═════════════════════════ ۲) منطقِ خالص ═════════════════════════

@pytest.mark.asyncio
async def test_check_boundaries(db):
    maker, row = db
    await _set(row.id, wl_min_zarinpal=30_000, wl_max_zarinpal=200_000)
    chk = lambda a: WL.check(row.id, "zarinpal", a)
    assert "30,000" in await chk(29_999) and "حداقل" in await chk(29_999)
    assert await chk(30_000) is None and await chk(200_000) is None
    assert "200,000" in await chk(200_001) and "حداکثر" in await chk(200_001)
    assert await chk("abc") == "❌ مبلغ نامعتبر است."
    assert await chk(None) == "❌ مبلغ نامعتبر است."


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_min,stored_max,expected", [
    ("", "0", (20_000, 0)),
    ("abc", "xyz", (20_000, 0)),          # مقدارِ خراب ⇒ پیش‌فرض
    ("-5", "-9", (20_000, 0)),
    ("50000.0", "300000.9", (50_000, 300_000)),
    ("50000", "40000", (50_000, 0)),       # سقفِ کمتر از کف: نادیده (وگرنه همهٔ واریزها بسته می‌شد)
])
async def test_get_limits_is_robust_against_bad_stored_values(db, stored_min, stored_max, expected):
    maker, row = db
    await _set(row.id, wl_min_zarinpal=stored_min, wl_max_zarinpal=stored_max)
    assert await WL.get_limits(row.id, "zarinpal") == expected


@pytest.mark.asyncio
async def test_set_min_max_validation(db):
    maker, row = db
    assert await WL.set_min(row.id, "zarinpal", "50000") == 50_000
    assert await WL.set_max(row.id, "zarinpal", 500_000) == 500_000
    with pytest.raises(WL.LimitError, match="کمتر"):
        await WL.set_min(row.id, "zarinpal", 500)                        # زیرِ کفِ فنی
    with pytest.raises(WL.LimitError, match="بیشتر"):
        await WL.set_min(row.id, "zarinpal", 600_000)                    # بالاتر از حداکثر
    with pytest.raises(WL.LimitError, match="کمتر"):
        await WL.set_max(row.id, "zarinpal", 40_000)                     # پایین‌تر از حداقل
    with pytest.raises(WL.LimitError):
        await WL.set_min(row.id, "zarinpal", -1)
    assert await WL.get_limits(row.id, "zarinpal") == (50_000, 500_000)  # ورودیِ بد چیزی را عوض نکرد
    assert await WL.set_max(row.id, "zarinpal", 0) == 0                  # برداشتنِ سقف
    assert await WL.set_min(row.id, "zarinpal", 0) == 20_000             # برگشت به پیش‌فرض
    assert await WL.get_limits(row.id, "zarinpal") == (20_000, 0)


@pytest.mark.asyncio
async def test_reset_restores_defaults(db):
    maker, row = db
    await WL.set_min(row.id, "card", 25_000)
    await WL.set_max(row.id, "card", 300_000)
    await WL.reset(row.id, "card")
    assert await WL.get_limits(row.id, "card") == (10_000, 0)


@pytest.mark.asyncio
async def test_limits_are_per_bot_and_per_gateway(db):
    maker, row = db
    await WL.set_min(row.id, "zarinpal", 70_000)
    assert (await WL.get_limits(row.id, "zibal"))[0] == 10_000
    assert (await WL.get_limits(row.id + 1, "zarinpal"))[0] == 20_000


@pytest.mark.asyncio
async def test_allowed_presets_filter(db):
    maker, row = db
    presets = (50_000, 100_000, 200_000, 500_000, 1_000_000)
    assert await WL.allowed_presets(row.id, "zarinpal", presets) == list(presets)
    await WL.set_min(row.id, "zarinpal", 100_000)
    await WL.set_max(row.id, "zarinpal", 500_000)
    assert await WL.allowed_presets(row.id, "zarinpal", presets) == [100_000, 200_000, 500_000]


@pytest.mark.asyncio
async def test_shared_keys_stay_in_sync_with_the_gateways_own_readers(db):
    """آنچه صفحهٔ مشترک می‌نویسد، خودِ درگاه‌ها (که کدشان را عوض نکردیم) همان را می‌خوانند."""
    maker, row = db
    from tenant.gateways import uniquepay, abangateway, tonpays
    await WL.set_min(row.id, "uniquepay", 80_000)
    await WL.set_max(row.id, "uniquepay", 900_000)
    assert await uniquepay.get_min_amount(row.id) == 80_000 and await uniquepay.get_max_amount(row.id) == 900_000
    await WL.set_min(row.id, "abangateway", 15_000)
    await WL.set_max(row.id, "abangateway", 400_000)
    assert await abangateway.get_min_amount(row.id) == 15_000 and await abangateway.get_max_amount(row.id) == 400_000
    await WL.set_min(row.id, "tonpays", 25_000)
    assert await tonpays.get_min_amount(row.id) == 25_000
    await WL.set_min(row.id, "tonpays", 0)                                # برگشت به پیش‌فرض ⇒ خواننده هم پیش‌فرض می‌بیند
    assert await tonpays.get_min_amount(row.id) == tonpays.MIN_AMOUNT
    await WL.reset(row.id, "uniquepay")
    assert await uniquepay.get_min_amount(row.id) == uniquepay.MIN_AMOUNT
    assert await uniquepay.get_max_amount(row.id) == 0


# ═════════════════════════ ۳) زرین‌پال (هندلرهایِ واقعی) ═════════════════════════

def _patch_zarinpal(monkeypatch):
    from tenant.gateways import zarinpal_wallet as zw
    calls = {"n": 0}

    async def request_payment(merchant, amount, description, callback_url):
        calls["n"] += 1
        return True, "A00000000000000000000000000012345678"

    async def configured(_): return True
    async def setting(_b, key, default=""): return "merchant-uuid" if "merchant" in key else default
    async def cbase(_): return "https://example.test"
    async def startpay(_b, a): return f"https://pay.test/{a}"
    monkeypatch.setattr(zw.platform_zarinpal, "request_payment", request_payment)
    monkeypatch.setattr(zw, "_configured", configured)
    monkeypatch.setattr(zw, "_setting", setting)
    monkeypatch.setattr(zw, "_callback_base", cbase)
    monkeypatch.setattr(zw, "startpay_url_for_bot", startpay)
    return zw, calls


@pytest.mark.asyncio
async def test_zarinpal_default_behaviour_is_unchanged(db, monkeypatch):
    maker, row = db
    zw, calls = _patch_zarinpal(monkeypatch)
    cb = Cb("wzp:amt:50000", tg_user(CUSTOMER))
    await zw.zarinpal_fixed_amount(cb, FakeAiogramBot(), State())
    assert len(await _orders(maker)) == 1 and calls["n"] == 1
    start = Cb("w:zarinpal", tg_user(CUSTOMER))
    await zw.zarinpal_start(start, FakeAiogramBot(), State())
    kb = start.message.sent[-1][1]["reply_markup"]
    assert _preset_amounts(kb, "wzp:amt:") == [50_000, 100_000, 200_000, 500_000, 1_000_000]
    assert "حداقل" not in start.message.sent[-1][0]                       # متن مثلِ قبل: بدونِ توضیحِ اضافه


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,mn,mx,word", [
    (50_000, 60_000, 0, "حداقل"),
    (500_000, 20_000, 400_000, "حداکثر"),
])
async def test_zarinpal_preset_outside_the_range_is_refused_before_touching_the_gateway(db, monkeypatch, amount, mn, mx, word):
    maker, row = db
    zw, calls = _patch_zarinpal(monkeypatch)
    await WL.set_min(row.id, "zarinpal", mn)
    if mx:
        await WL.set_max(row.id, "zarinpal", mx)
    cb = Cb(f"wzp:amt:{amount}", tg_user(CUSTOMER))
    await zw.zarinpal_fixed_amount(cb, FakeAiogramBot(), State())
    assert word in _texts(cb)
    assert await _orders(maker) == [] and calls["n"] == 0                 # نه فاکتور، نه تماس با درگاه


@pytest.mark.asyncio
async def test_zarinpal_presets_outside_the_range_are_hidden(db, monkeypatch):
    maker, row = db
    zw, _ = _patch_zarinpal(monkeypatch)
    await WL.set_min(row.id, "zarinpal", 100_000)
    await WL.set_max(row.id, "zarinpal", 500_000)
    start = Cb("w:zarinpal", tg_user(CUSTOMER))
    await zw.zarinpal_start(start, FakeAiogramBot(), State())
    text, kw = start.message.sent[-1]
    assert _preset_amounts(kw["reply_markup"], "wzp:amt:") == [100_000, 200_000, 500_000]
    assert "100,000" in text and "500,000" in text                        # توضیحِ بازه وقتی تنظیم شده
    custom = [b for r in kw["reply_markup"].inline_keyboard for b in r if b.callback_data == "wzp:custom"]
    assert custom                                                          # مبلغِ دلخواه همیشه هست


@pytest.mark.asyncio
async def test_zarinpal_custom_amount_boundaries(db, monkeypatch):
    maker, row = db
    zw, calls = _patch_zarinpal(monkeypatch)
    await WL.set_min(row.id, "zarinpal", 30_000)
    await WL.set_max(row.id, "zarinpal", 100_000)
    st = State()
    for bad in ("29999", "100001", "۲۹۹۹۹", "abc", "0"):
        m = Msg(tg_user(CUSTOMER), bad)
        await zw.zarinpal_custom_amount(m, st, FakeAiogramBot())
        assert m.sent and calls["n"] == 0, bad
    assert await _orders(maker) == []
    for good in ("30000", "100000", "۵۰۰۰۰"):
        m = Msg(tg_user(CUSTOMER), good)
        await zw.zarinpal_custom_amount(m, st, FakeAiogramBot())
    assert calls["n"] == 3 and len(await _orders(maker)) == 3


@pytest.mark.asyncio
async def test_zarinpal_custom_prompt_shows_the_range_only_when_customized(db, monkeypatch):
    maker, row = db
    zw, _ = _patch_zarinpal(monkeypatch)
    cb = Cb("wzp:custom", tg_user(CUSTOMER))
    await zw.zarinpal_custom(cb, State(), FakeAiogramBot())
    assert "حداقل ۲۰٬۰۰۰ تومان" in cb.message.sent[-1][0]               # متنِ قبلی، کلمه‌به‌کلمه
    await WL.set_max(row.id, "zarinpal", 90_000)
    cb2 = Cb("wzp:custom", tg_user(CUSTOMER))
    await zw.zarinpal_custom(cb2, State(), FakeAiogramBot())
    assert "90,000" in cb2.message.sent[-1][0]


# ═════════════════════════ ۴) زیبال ═════════════════════════

def _patch_zibal(monkeypatch):
    from tenant.gateways import zibal_wallet as zb
    calls = {"n": 0}

    async def request_payment(merchant, amount, description, callback_url):
        calls["n"] += 1
        return True, "track-1"

    async def configured(_): return True
    async def setting(_b, key, default=""): return "zibal" if "merchant" in key else default
    async def cbase(_): return "https://example.test"
    monkeypatch.setattr(zb.platform_zibal, "request_payment", request_payment)
    monkeypatch.setattr(zb, "_configured", configured)
    monkeypatch.setattr(zb, "_setting", setting)
    monkeypatch.setattr(zb, "_callback_base", cbase)
    if hasattr(zb, "startpay_url_for_bot"):
        async def sp(*a, **k): return "https://pay.test/zb"
        monkeypatch.setattr(zb, "startpay_url_for_bot", sp)
    return zb, calls


@pytest.mark.asyncio
async def test_zibal_default_and_limits(db, monkeypatch):
    maker, row = db
    zb, calls = _patch_zibal(monkeypatch)
    await zb.zibal_fixed_amount(Cb("wzb:amt:50000", tg_user(CUSTOMER)), FakeAiogramBot(), State())
    assert calls["n"] == 1
    await WL.set_max(row.id, "zibal", 80_000)
    cb = Cb("wzb:amt:100000", tg_user(CUSTOMER))
    await zb.zibal_fixed_amount(cb, FakeAiogramBot(), State())
    assert "حداکثر" in _texts(cb) and calls["n"] == 1
    start = Cb("w:zibal", tg_user(CUSTOMER))
    await zb.zibal_start(start, FakeAiogramBot(), State())
    assert _preset_amounts(start.message.sent[-1][1]["reply_markup"], "wzb:amt:") == [50_000]


# ═════════════════════════ ۵) حساب‌پی ═════════════════════════

def _patch_hesabpay(monkeypatch):
    from decimal import Decimal
    from tenant.gateways import hesabpay as hp
    made = {"n": 0}

    async def available(_): return True
    async def rate(_): return Decimal("2000")
    async def setting(_b, key, default=""): return default

    async def create_session(_b, charge):
        made["n"] += 1
        raise RuntimeError("stop-here")            # بعد از ساختِ ردیفِ شارژ
    monkeypatch.setattr(hp, "is_available", available)
    monkeypatch.setattr(hp, "_rate", rate)
    monkeypatch.setattr(hp, "_setting", setting)
    monkeypatch.setattr(hp, "_create_session", create_session)
    return hp, made


@pytest.mark.asyncio
async def test_hesabpay_menu_and_limits(db, monkeypatch):
    maker, row = db
    hp, made = _patch_hesabpay(monkeypatch)
    menu = Cb("w:hesabpay", tg_user(CUSTOMER))
    await hp.hesabpay_start(menu, FakeAiogramBot(), State())
    kb = menu.message.sent[-1][1]["reply_markup"]
    assert _preset_amounts(kb, "hb:amount:") == [50_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000]
    # چیدمانِ قبلی: جفت‌جفت
    rows_of_presets = [[b for b in r if b.callback_data.startswith("hb:amount:")] for r in kb.inline_keyboard
                       if r and r[0].callback_data and r[0].callback_data.startswith("hb:amount:")]
    assert [len(r) for r in rows_of_presets] == [2, 2, 2]

    await WL.set_min(row.id, "hesabpay", 100_000)
    await WL.set_max(row.id, "hesabpay", 500_000)
    menu2 = Cb("w:hesabpay", tg_user(CUSTOMER))
    await hp.hesabpay_start(menu2, FakeAiogramBot(), State())
    assert _preset_amounts(menu2.message.sent[-1][1]["reply_markup"], "hb:amount:") == [100_000, 200_000, 500_000]

    for amt in (50_000, 1_000_000):
        cb = Cb(f"hb:amount:{amt}", tg_user(CUSTOMER))
        await hp.hesabpay_amount(cb, FakeAiogramBot(), State())
        assert made["n"] == 0 and ("حداقل" in _texts(cb) or "حداکثر" in _texts(cb)), amt
    m = Msg(tg_user(CUSTOMER), "50000")
    await hp.hesabpay_amount_text(m, FakeAiogramBot(), State())
    assert made["n"] == 0 and "حداقل" in _texts(m)


# ═════════════════════════ ۶) کارت‌به‌کارت ═════════════════════════

@pytest.mark.asyncio
async def test_card_keyboard_is_identical_to_the_old_one_by_default(db):
    """کیبوردِ قبلی: ۲×۲ مبلغ + دلخواه + بازگشت، با همین callback_dataها."""
    maker, row = db
    from tenant.gateways import card_wallet as cw
    text, kb = await cw._charge_prompt_and_kb(row.id, colored=False)
    assert text == "💳 مبلغ شارژ را انتخاب کن یا «✏️ مبلغ دلخواه» را بزن:"
    rows = [[b.callback_data for b in r] for r in kb.inline_keyboard]
    assert rows == [["wcamt:50000", "wcamt:100000"], ["wcamt:200000", "wcamt:500000"],
                    ["wcamt:custom"], ["w:back"]]
    assert [len(r) for r in cw.wallet_charge_kb(False).inline_keyboard] == [2, 2, 1, 1]      # بدونِ آرگومان هم همان


@pytest.mark.asyncio
async def test_card_presets_filtered_and_range_shown(db):
    maker, row = db
    from tenant.gateways import card_wallet as cw
    await WL.set_min(row.id, "card", 100_000)
    await WL.set_max(row.id, "card", 200_000)
    text, kb = await cw._charge_prompt_and_kb(row.id, colored=True)
    assert _preset_amounts(kb, "wcamt:") == [100_000, 200_000]
    assert "100,000" in text and "200,000" in text
    assert any(b.callback_data == "wcamt:custom" for r in kb.inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_card_custom_amount_and_preset_are_both_guarded(db, monkeypatch):
    maker, row = db
    from tenant.gateways import card_wallet as cw
    await WL.set_min(row.id, "card", 30_000)
    await WL.set_max(row.id, "card", 100_000)
    proceeded = []

    async def kyc(*a, **k): return True, ""
    import tenant.shared.kyc as kycmod
    monkeypatch.setattr(kycmod, "check_charge", kyc)
    async def gate(*a, **k): return None
    real_proceed = cw._wallet_charge_proceed

    # مسیرِ دلخواه
    for bad in ("29999", "100001", "abc"):
        st = State()
        m = Msg(tg_user(CUSTOMER), bad)
        await cw.w_charge_amount(m, st, FakeAiogramBot())
        assert m.sent and "amount" not in st.data, bad                     # به مرحلهٔ بعد نرفته
    # مسیرِ دکمهٔ ثابت (نقطهٔ مشترک)
    st = State()
    msg = Msg(tg_user(BOT_TG, is_bot=True))
    await cw._wallet_charge_proceed(msg, st, FakeAiogramBot(), 50_000 * 3, CUSTOMER)
    assert "amount" not in st.data and "حداکثر" in _texts(msg)


@pytest.mark.asyncio
async def test_card_custom_prompt_text_unchanged_by_default(db):
    maker, row = db
    from tenant.gateways import card_wallet as cw
    cb = Cb("wcamt:custom", tg_user(CUSTOMER))
    await cw.w_charge_pick(cb, State(), FakeAiogramBot())
    assert "حداقل ۱۰٬۰۰۰" in cb.message.sent[-1][0]


# ═════════════════════════ ۷) تون‌پیز و ارز دیجیتال ═════════════════════════

@pytest.mark.asyncio
async def test_tonpays_min_and_new_max(db):
    maker, row = db
    from tenant.gateways import tonpays as tp
    st = State()
    m = Msg(tg_user(CUSTOMER), "19999")
    await tp.tonpays_amount(m, FakeAiogramBot(), st)
    assert "حداقل" in _texts(m) and "20,000" in _texts(m) and st.cur is not None
    await WL.set_max(row.id, "tonpays", 500_000)
    m2 = Msg(tg_user(CUSTOMER), "600000")
    await tp.tonpays_amount(m2, FakeAiogramBot(), State())
    assert "حداکثر" in _texts(m2) and "500,000" in _texts(m2)


@pytest.mark.asyncio
async def test_crypto_toman_input_uses_the_limits_before_any_network_call(db, monkeypatch):
    maker, row = db
    from tenant.gateways import crypto_admin as ca
    from tenant.gateways import crypto_pay
    await _set(row.id, nowpay_api_key="KEY")

    async def boom(*a, **k):
        raise AssertionError("نباید به شبکه برسد")
    monkeypatch.setattr(crypto_pay, "get_rate_smart", boom)

    st = State()
    st.data = {"crypto_currency": "toman"}
    m = Msg(tg_user(CUSTOMER), "99999")
    await ca.w_crypto_amount(m, st, FakeAiogramBot())
    assert "حداقل" in _texts(m) and "100,000" in _texts(m)
    await WL.set_max(row.id, "crypto", 1_000_000)
    st2 = State()
    st2.data = {"crypto_currency": "toman"}
    m2 = Msg(tg_user(CUSTOMER), "2000000")
    await ca.w_crypto_amount(m2, st2, FakeAiogramBot())
    assert "حداکثر" in _texts(m2)


# ═════════════════════════ ۸) صفحهٔ ادمین ═════════════════════════

def _enable(monkeypatch, *names):
    """فقط درگاه‌هایی که اسم‌شان آمده فعال باشند."""
    from dataclasses import replace
    patched = {}
    for k, g in WL.GATEWAYS.items():
        async def _f(_bid, _on=(k in names)): return _on
        patched[k] = replace(g, enabled=_f)
    monkeypatch.setattr(WL, "GATEWAYS", patched)


@pytest.mark.asyncio
async def test_admin_menu_lists_only_active_gateways(db, monkeypatch):
    maker, row = db
    from tenant.gateways import wallet_limits_admin as adm
    _enable(monkeypatch, "card", "zarinpal")
    text, kb = await adm._menu_content(row.id)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "wlim:g:card" in cbs and "wlim:g:zarinpal" in cbs and "wlim:g:zibal" not in cbs
    assert "tadm:sec:finance" in cbs
    assert "کارت‌به‌کارت" in text and "زیبال" not in text
    _enable(monkeypatch)
    text2, kb2 = await adm._menu_content(row.id)
    assert "هیچ درگاهِ شارژی" in text2 and [b.callback_data for r in kb2.inline_keyboard for b in r] == ["tadm:sec:finance"]


@pytest.mark.asyncio
async def test_admin_menu_shows_current_ranges(db, monkeypatch):
    maker, row = db
    from tenant.gateways import wallet_limits_admin as adm
    _enable(monkeypatch, "zarinpal")
    await WL.set_min(row.id, "zarinpal", 40_000)
    await WL.set_max(row.id, "zarinpal", 300_000)
    text, kb = await adm._menu_content(row.id)
    assert "40,000 تا 300,000" in text
    assert any("40,000 تا 300,000" in b.text for r in kb.inline_keyboard for b in r)


def test_admin_callbacks_fit_telegram_limit():
    for k in WL.GATEWAYS:
        for prefix in ("wlim:g:", "wlim:mn:", "wlim:mx:", "wlim:rs:"):
            assert len((prefix + k).encode()) <= 64


@pytest.fixture
def as_owner(monkeypatch, db):
    maker, row = db
    import tenant

    async def owner_ok(cb, bot): return row
    async def owner_ok_msg(msg, bot): return row
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "_owner_ok_msg", owner_ok_msg)

    async def cancel_kb(bot): return None
    monkeypatch.setattr(tenant, "smart_cancel_kb", cancel_kb)
    return row


@pytest.mark.asyncio
async def test_admin_full_edit_flow(db, as_owner, monkeypatch):
    maker, row = db
    from tenant.gateways import wallet_limits_admin as adm
    _enable(monkeypatch, "zarinpal")
    st = State()

    # باز کردنِ درگاه
    cb = Cb("wlim:g:zarinpal", tg_user(ADMIN))
    await adm.wlim_gateway(cb, FakeAiogramBot(), st)
    assert "20,000" in _texts(cb) and "پیش‌فرض" in _texts(cb)

    # حداقل
    cb = Cb("wlim:mn:zarinpal", tg_user(ADMIN))
    await adm.wlim_ask(cb, FakeAiogramBot(), st)
    assert st.cur == adm.WlimFSM.min_amount and st.data["wlim_key"] == "zarinpal"
    m = Msg(tg_user(ADMIN), "۵۰۰۰۰")
    await adm.wlim_save_min(m, FakeAiogramBot(), st)
    assert st.cur is None and "50,000" in _texts(m)
    assert await WL.get_limits(row.id, "zarinpal") == (50_000, 0)

    # حداکثر
    await adm.wlim_ask(Cb("wlim:mx:zarinpal", tg_user(ADMIN)), FakeAiogramBot(), st)
    m = Msg(tg_user(ADMIN), "400000")
    await adm.wlim_save_max(m, FakeAiogramBot(), st)
    assert await WL.get_limits(row.id, "zarinpal") == (50_000, 400_000)

    # ورودیِ نامعتبر در همان مرحله می‌ماند و چیزی عوض نمی‌شود
    await adm.wlim_ask(Cb("wlim:mn:zarinpal", tg_user(ADMIN)), FakeAiogramBot(), st)
    for bad, word in (("abc", "نامعتبر"), ("500", "کمتر"), ("999999", "بیشتر")):
        m = Msg(tg_user(ADMIN), bad)
        await adm.wlim_save_min(m, FakeAiogramBot(), st)
        assert word in _texts(m) and st.cur == adm.WlimFSM.min_amount, bad
    assert await WL.get_limits(row.id, "zarinpal") == (50_000, 400_000)

    # صفر = پیش‌فرض
    m = Msg(tg_user(ADMIN), "0")
    await adm.wlim_save_min(m, FakeAiogramBot(), st)
    assert (await WL.get_limits(row.id, "zarinpal"))[0] == 20_000

    # ریست
    cb = Cb("wlim:rs:zarinpal", tg_user(ADMIN))
    await adm.wlim_reset(cb, FakeAiogramBot(), st)
    assert await WL.get_limits(row.id, "zarinpal") == (20_000, 0)


@pytest.mark.asyncio
async def test_admin_flow_ignores_unknown_gateway_keys(db, as_owner):
    maker, row = db
    from tenant.gateways import wallet_limits_admin as adm
    st = State()
    cb = Cb("wlim:g:hacker", tg_user(ADMIN))
    await adm.wlim_gateway(cb, FakeAiogramBot(), st)
    assert cb.message.sent == []
    m = Msg(tg_user(ADMIN), "5000")
    st.data = {"wlim_key": "hacker"}
    await adm.wlim_save_min(m, FakeAiogramBot(), st)
    assert m.sent == [] and st.cur is None


@pytest.mark.asyncio
async def test_non_admin_cannot_change_limits(db, monkeypatch):
    maker, row = db
    import tenant
    from tenant.gateways import wallet_limits_admin as adm

    async def deny_cb(cb, bot):
        await cb.answer()
        return None
    async def deny_msg(msg, bot): return None
    monkeypatch.setattr(tenant, "_owner_ok", deny_cb)
    monkeypatch.setattr(tenant, "_owner_ok_msg", deny_msg)
    st = State()
    st.data = {"wlim_key": "zarinpal"}
    m = Msg(tg_user(CUSTOMER), "500000")
    await adm.wlim_save_max(m, FakeAiogramBot(), st)
    assert await WL.get_limits(row.id, "zarinpal") == (20_000, 0)
    await adm.wlim_reset(Cb("wlim:rs:zarinpal", tg_user(CUSTOMER)), FakeAiogramBot(), st)
    await adm.wlim_ask(Cb("wlim:mx:zarinpal", tg_user(CUSTOMER)), FakeAiogramBot(), st)
    assert await WL.get_limits(row.id, "zarinpal") == (20_000, 0)


# ═════════════════════════ ۹) پیوند به منویِ مالی ═════════════════════════

def test_finance_menu_and_keyboard_mode_are_wired():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "tenant" / "__init__.py").read_text(encoding="utf-8")
    assert '"wlim:menu"' in src and "wallet_limits_admin_router" in src
    assert '"wlim:menu": ("tenant.gateways.wallet_limits_admin", "wlim_menu")' in src
    i = src.index("async def _sec_finance_opts(")
    body = src[i:src.index("\nasync def _sec_finance_kb(", i)]
    assert "📏 حداقل و حداکثر واریز هر درگاه" in body


def test_every_gateway_handler_patched_uses_the_shared_check():
    """رگرسیون: هرکس حداقلِ ثابتِ جدید داخلِ این درگاه‌ها نگذارد؛ همه از wallet_limits رد شوند."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "tenant" / "gateways"
    for name in ("zarinpal_wallet", "zibal_wallet", "hesabpay", "card_wallet", "crypto_admin", "tonpays"):
        src = (root / f"{name}.py").read_text(encoding="utf-8")
        assert "wallet_limits" in src, name
    assert "amount < 20_000" not in (root / "zarinpal_wallet.py").read_text(encoding="utf-8")
    assert "amount < 10_000" not in (root / "zibal_wallet.py").read_text(encoding="utf-8")
    assert "amount < 10_000" not in (root / "card_wallet.py").read_text(encoding="utf-8")
    assert "amount_toman < 100000" not in (root / "crypto_admin.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_active_gateways_survives_one_broken_detector(db, monkeypatch):
    maker, row = db
    from dataclasses import replace

    async def boom(_bid): raise RuntimeError("x")
    async def yes(_bid): return True
    async def no(_bid): return False
    patched = dict(WL.GATEWAYS)
    for k in patched:
        patched[k] = replace(patched[k], enabled=no)
    patched["zarinpal"] = replace(patched["zarinpal"], enabled=boom)
    patched["card"] = replace(patched["card"], enabled=yes)
    monkeypatch.setattr(WL, "GATEWAYS", patched)
    keys = [g.key for g in await WL.active_gateways(row.id)]
    assert "card" in keys and "zarinpal" not in keys
