"""گزارشِ فدایی (Express): تیک‌هایِ کیف‌پول در فاکتورِ خدماتِ مجازی، «شارژ حساب و ثبت سفارش»،
سقفِ احرازِ هویت روی کارت‌به‌کارتِ مستقیم، و دلیلِ ردِ احراز (PATCH_VS_PAY_V1 / PATCH_KYC_REJECT_REASON_V1)."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import kyc as K
from tenant.shared import kyc_admin as KA
from tenant.shared import kyc_user as KU
from tenant.shared import vs_pay as VP

pytestmark = pytest.mark.asyncio

BOT_ID, TG = 3, 555


class FakeBot:
    def __init__(self, *a, **k):
        self.sent = []
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        pass

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class Target:
    def __init__(self):
        self.answers = []
        self.chat = SimpleNamespace(id=TG)

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


def cbs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, VP, K, KA, KU):
        monkeypatch.setattr(mod, "Session", maker, raising=False)
    monkeypatch.setattr(KA, "_S", maker)
    async with maker() as s:
        s.add(models.Bot(id=BOT_ID, owner_id=1, token="3:TOKEN", username="b", field="virtual_service",
                         plan="pro", status="active"))
        await s.flush()
        s.add(models.BotUser(id=1, bot_id=BOT_ID, telegram_id=TG, full_name="Ali", balance=0))
        await s.commit()
    settings: dict = {}

    async def gs(bid, key, default=""):
        return settings.get(key, default)

    async def no_ab(_bid):
        return False

    async def bot_row(_bot):
        return SimpleNamespace(id=BOT_ID, plan="pro", owner_id=1)

    async def topup_rows(bot_id, colored=False, in_purchase=True, **kw):
        return [[models_btn("💳 شارژ کارت‌به‌کارت", "w:charge")],
                [models_btn("🔐 شارژ با زرین‌پال", "w:zarinpal")],
                [models_btn("💠 شارژ با تون‌پیز", "w:tonpays")]]

    async def limits(_bid, gw):
        return 10_000, 0

    async def kyc_on(_bid):
        return True

    from tenant.gateways import wallet_limits as WL
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "_ab_replaces_card", no_ab)
    monkeypatch.setattr(tenant, "get_bot_row", bot_row)
    monkeypatch.setattr(tenant, "wallet_topup_rows", topup_rows)
    monkeypatch.setattr(WL, "get_limits", limits)
    monkeypatch.setattr(K, "enabled", kyc_on)
    monkeypatch.setattr(VP, "Bot", FakeBot)
    yield SimpleNamespace(maker=maker, settings=settings)
    await engine.dispose()


def models_btn(text, cb):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=text, callback_data=cb)


async def _set_balance(env, v):
    async with env.maker() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = v
        await s.commit()


# ───────────── ۲) تیک‌هایِ کیف‌پول ─────────────

async def test_default_invoice_has_wallet_and_card_only(env):
    rows = await VP.pay_rows(BOT_ID, TG, 217_950, "star:pay:1:50", "star:cardpay:1:50")
    assert [b.callback_data for r in rows for b in r] == ["star:pay:1:50", "star:cardpay:1:50"]


async def test_wallet_pay_toggle_off_hides_wallet_button(env):
    env.settings["wallet_pay_purchase_enabled"] = "0"
    rows = await VP.pay_rows(BOT_ID, TG, 217_950, "star:pay:1:50", "star:cardpay:1:50")
    assert [b.callback_data for r in rows for b in r] == ["star:cardpay:1:50"]


async def test_card_toggles_hide_card_button(env):
    env.settings["card_purchase_enabled"] = "0"
    assert [b.callback_data for r in await VP.pay_rows(BOT_ID, TG, 1000, "p", "c") for b in r] == ["p"]
    env.settings["card_purchase_enabled"] = "1"
    env.settings["card_enabled"] = "0"
    assert [b.callback_data for r in await VP.pay_rows(BOT_ID, TG, 1000, "p", "c") for b in r] == ["p"]


async def test_everything_off_gives_no_buttons(env):
    env.settings.update(wallet_pay_purchase_enabled="0", card_enabled="0", wallet_charge_in_purchase_enabled="1")
    assert await VP.pay_rows(BOT_ID, TG, 1000, "p", "c") == []


async def test_charge_toggle_adds_charge_account_button(env):
    env.settings["wallet_charge_in_purchase_enabled"] = "1"
    rows = await VP.pay_rows(BOT_ID, TG, 217_950, "star:pay:1:50", "star:cardpay:1:50")
    flat = [b for r in rows for b in r]
    assert flat[-1].callback_data.startswith("vsc:o:") and "شارژ حساب" in flat[-1].text
    assert "ثبت سفارش" in flat[-1].text            # موجودی ۰ < قیمت
    await _set_balance(env, 500_000)
    flat = [b for r in await VP.pay_rows(BOT_ID, TG, 217_950, "star:pay:1:50") for b in r]
    assert flat[-1].text == "🔋 شارژ حساب"           # موجودی کافی است؛ فقط شارژ


async def test_charge_toggle_off_and_wallet_off_have_no_charge_button(env):
    flat = [b for r in await VP.pay_rows(BOT_ID, TG, 217_950, "p", "c") for b in r]
    assert not any((b.callback_data or "").startswith("vsc:") for b in flat)
    env.settings.update(wallet_charge_in_purchase_enabled="1", wallet_pay_purchase_enabled="0")
    flat = [b for r in await VP.pay_rows(BOT_ID, TG, 217_950, "p", "c") for b in r]
    assert not any((b.callback_data or "").startswith("vsc:") for b in flat)


# ───────────── ۳ و ۴) شارژ حساب و ثبت سفارش ─────────────

async def test_insufficient_balance_offers_charge_and_chooser_prefills_invoice_amount(env):
    await _set_balance(env, 17_950)
    t = Target()
    await VP.insufficient_notice(t, BOT_ID, TG, 217_950, "star:pay:555:50")
    text, kw = t.answers[0]
    assert "200,000" in text                       # کمبود
    open_cb = [c for c in cbs(kw["reply_markup"]) if c.startswith("vsc:o:")][0]

    shown = []

    class Msg:
        async def answer(self, text, **kw):
            shown.append((text, kw))

    answered = []

    async def ans(*a, **k):
        answered.append((a, k))
    cb = SimpleNamespace(data=open_cb, from_user=SimpleNamespace(id=TG), message=Msg(), answer=ans)
    await VP.vsc_open(cb, object())
    text, kw = shown[0]
    got = cbs(kw["reply_markup"])
    assert "wcamt:200000" in got and "wzp:amt:200000" in got   # درگاه‌هایِ مبلغ‌ثابت: مبلغِ لازم از قبل پر است
    assert "w:tonpays" in got                                   # بقیه همان ورودیِ عمومی
    assert "200,000" in text
    d = await VP._load(BOT_ID, TG)
    assert d["armed"] == open_cb.rsplit(":", 1)[1]              # بعد از شارژ باید ادامه بدهد


async def test_chooser_amount_never_below_gateway_minimum(env):
    await _set_balance(env, 0)
    rows = await VP.chooser_rows(BOT_ID, 4_000, False)
    assert "wcamt:10000" in [b.callback_data for r in rows for b in r]


async def test_topup_after_arming_sends_continue_button(env, monkeypatch):
    t = Target()
    await VP.insufficient_notice(t, BOT_ID, TG, 217_950, "star:pay:555:50")
    iid = [c for c in cbs(t.answers[0][1]["reply_markup"]) if c.startswith("vsc:o:")][0].rsplit(":", 1)[1]
    d = await VP._load(BOT_ID, TG)
    d["armed"] = iid
    await VP._save(BOT_ID, TG, d)

    seen = []

    async def fake_notify(bot_id, tg):
        seen.append((bot_id, tg))
    monkeypatch.setattr(VP, "_notify_resume", fake_notify)

    async with env.maker() as s:               # رد/برگشتِ وجه و خرید (منفی) نباید فعال کنند
        s.add(models.WalletTxn(bot_id=BOT_ID, user_id=1, amount=5000, kind="refund", note="x"))
        s.add(models.WalletTxn(bot_id=BOT_ID, user_id=1, amount=-100, kind="purchase", note="x"))
        await s.commit()
    await asyncio.sleep(0)
    assert seen == []
    async with env.maker() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = 250_000
        s.add(models.WalletTxn(bot_id=BOT_ID, user_id=1, amount=250_000, kind="charge", note="شارژ کارت‌به‌کارت"))
        await s.commit()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [(BOT_ID, TG)]


async def test_notify_resume_gives_original_pay_button_when_balance_enough(env):
    t = Target()
    await VP.insufficient_notice(t, BOT_ID, TG, 217_950, "star:pay:555:50")
    iid = [c for c in cbs(t.answers[0][1]["reply_markup"]) if c.startswith("vsc:o:")][0].rsplit(":", 1)[1]
    d = await VP._load(BOT_ID, TG)
    d["armed"] = iid
    await VP._save(BOT_ID, TG, d)
    await _set_balance(env, 300_000)
    sent = []

    class Spy(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text, kw))
    VP.Bot = Spy
    try:
        await VP._notify_resume(BOT_ID, TG)
    finally:
        VP.Bot = FakeBot
    chat, text, kw = sent[0]
    assert chat == TG and "شارژ شد" in text
    assert "star:pay:555:50" in cbs(kw["reply_markup"])        # همان دکمهٔ پرداختِ فاکتور
    assert (await VP._load(BOT_ID, TG))["armed"] is None        # فقط یک‌بار اعلام می‌شود


async def test_notify_resume_without_armed_invoice_sends_nothing(env):
    sent = []

    class Spy(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            sent.append(chat_id)
    VP.Bot = Spy
    try:
        await VP._notify_resume(BOT_ID, TG)
    finally:
        VP.Bot = FakeBot
    assert sent == []


# ───────────── ۱ و ۵) سقفِ احراز روی کارت‌به‌کارتِ مستقیم ─────────────

async def _card_order(env, price, status, qty=1):
    async with env.maker() as s:
        s.add(models.GiftOrder(bot_id=BOT_ID, buyer_user_id=1, gift_id="starsell_50", star_count=50,
                               price_toman=price, quantity=qty, dest_telegram_id=TG, dest_label="x",
                               status=status, pay_method="card", tracking_code=f"T{price}{status}"))
        await s.commit()


async def test_direct_card_orders_count_toward_daily_cap(env):
    env.settings["kyc_cap_l0"] = "500000"
    assert await VP.kyc_block(BOT_ID, TG, 300_000) is None                 # زیرِ سقف
    await _card_order(env, 400_000, "awaiting_admin")
    blocked = await VP.kyc_block(BOT_ID, TG, 217_950, "star:back_shop")
    assert blocked is not None and "سقف" in blocked[0]
    assert "kycu:menu" in cbs(blocked[1])
    assert await K.charged_today(BOT_ID, 1) == 400_000


async def test_abandoned_or_rejected_card_orders_do_not_count(env):
    env.settings["kyc_cap_l0"] = "500000"
    await _card_order(env, 400_000, "awaiting_receipt")
    await _card_order(env, 410_000, "rejected")
    assert await K.charged_today(BOT_ID, 1) == 0
    assert await VP.kyc_block(BOT_ID, TG, 450_000) is None


async def test_kyc_disabled_never_blocks(env, monkeypatch):
    async def off(_b):
        return False
    monkeypatch.setattr(K, "enabled", off)
    await _card_order(env, 9_000_000, "awaiting_admin")
    assert await VP.kyc_block(BOT_ID, TG, 9_000_000) is None


async def _run_cardpay(env, monkeypatch, smm, data="star:cardpay:555:50"):
    async def rate(_bid):
        return 4359, 1, 100000

    async def get_user(*a, **k):
        async with env.maker() as s:
            return await s.get(models.BotUser, 1), False
    monkeypatch.setattr(smm, "_stars_sell_settings", rate)
    monkeypatch.setattr(smm, "Session", env.maker)
    monkeypatch.setattr(tenant, "get_or_create_user", get_user)
    env.settings["card_number"] = "6037000000000000"
    out = []
    msg = SimpleNamespace(message_id=1, answer=lambda *a, **k: _rec(out, a, k))
    answered = []

    async def ans(*a, **k):
        answered.append((a, k))
    cb = SimpleNamespace(data=data, message=msg, answer=ans,
                         from_user=SimpleNamespace(id=TG, full_name="Ali", username="ali"))

    class St:
        async def get_data(self):
            return {}

        async def set_state(self, *a):
            pass

        async def update_data(self, **k):
            pass
    await smm.star_cardpay_start(cb, FakeBot(), St())
    return out, answered


async def _rec(out, a, k):
    out.append((a, k))


async def test_star_cardpay_is_blocked_by_kyc_before_card_number_and_order(env, monkeypatch):
    from tenant.bots.virtual_service import smm
    env.settings["kyc_cap_l0"] = "500000"
    await _card_order(env, 400_000, "awaiting_admin")
    out, answered = await _run_cardpay(env, monkeypatch, smm)
    assert out and "سقف" in out[0][0][0] and "6037000000000000" not in out[0][0][0]
    async with env.maker() as s:
        n = await s.scalar(select(func.count()).select_from(models.GiftOrder).where(
            models.GiftOrder.status == "awaiting_receipt"))
    assert n == 0                                                  # سفارشِ کارتیِ جدیدی ساخته نشد


async def test_star_cardpay_respects_card_purchase_toggle(env, monkeypatch):
    from tenant.bots.virtual_service import smm
    env.settings["card_purchase_enabled"] = "0"
    out, answered = await _run_cardpay(env, monkeypatch, smm)
    assert out == [] and answered and "غیرفعال" in answered[0][0][0]


async def test_star_pay_refused_when_wallet_pay_is_off(env, monkeypatch):
    from tenant.bots.virtual_service import smm
    env.settings["wallet_pay_purchase_enabled"] = "0"
    answered = []

    async def ans(*a, **k):
        answered.append((a, k))
    cb = SimpleNamespace(data="star:pay:555:50", message=SimpleNamespace(message_id=1), answer=ans,
                         from_user=SimpleNamespace(id=TG, full_name="Ali", username="ali"))

    class St:
        async def get_data(self):
            return {}
    await smm.star_pay(cb, FakeBot(), St())
    assert answered and "غیرفعال" in answered[0][0][0]


# ───────────── ۶) دلیلِ ردِ احراز ─────────────

async def _pending_kyc(env, level=1):
    async with env.maker() as s:
        r = models.KycRequest(bot_id=BOT_ID, user_id=1, level=level, card="6037997000000000", status="pending")
        s.add(r)
        await s.commit()
        return r.id


async def test_reject_with_reason_stores_and_tells_user(env):
    rid = await _pending_kyc(env)
    bot = FakeBot()
    res = await KA._do_reject(bot, SimpleNamespace(id=BOT_ID), rid, "عکس کارت ناخوانا <b>بود</b>")
    assert res == "ok"
    chat, text, _ = bot.sent[0]
    assert chat == TG and "دلیل" in text and "عکس کارت ناخوانا" in text
    assert "<b>بود</b>" not in text and "&lt;b&gt;" in text        # HTML اسکیپ شده
    async with env.maker() as s:
        r = await s.get(models.KycRequest, rid)
    assert r.status == "rejected" and r.admin_note == "عکس کارت ناخوانا <b>بود</b>"


async def test_reject_without_reason_has_no_reason_line(env):
    rid = await _pending_kyc(env)
    bot = FakeBot()
    assert await KA._do_reject(bot, SimpleNamespace(id=BOT_ID), rid, "") == "ok"
    assert "دلیل" not in bot.sent[0][1]
    async with env.maker() as s:
        assert (await s.get(models.KycRequest, rid)).admin_note is None


async def test_reject_twice_or_other_bot_is_refused(env):
    rid = await _pending_kyc(env)
    assert await KA._do_reject(FakeBot(), SimpleNamespace(id=BOT_ID), rid, "x") == "ok"
    assert await KA._do_reject(FakeBot(), SimpleNamespace(id=BOT_ID), rid, "y") == "gone"
    rid2 = await _pending_kyc(env)
    assert await KA._do_reject(FakeBot(), SimpleNamespace(id=999), rid2, "y") == "gone"


async def test_user_menu_shows_last_rejection_reason(env):
    rid = await _pending_kyc(env)
    await KA._do_reject(FakeBot(), SimpleNamespace(id=BOT_ID), rid, "نامِ روی کارت نمی‌خواند")
    txt, _kb = await KU.user_content(BOT_ID, TG)
    assert "تأیید نشد" in txt and "نامِ روی کارت نمی‌خواند" in txt
    await _pending_kyc(env)                                          # درخواستِ تازه: دلیلِ قبلی دیگر نشان داده نمی‌شود
    txt, _kb = await KU.user_content(BOT_ID, TG)
    assert "نامِ روی کارت نمی‌خواند" not in txt and "در انتظارِ بررسی" in txt


# ───────────── صفحهٔ فاکتورِ استارز و کمبودِ موجودی (یکپارچه) ─────────────

async def test_stars_invoice_follows_owner_toggles(env, monkeypatch):
    from tenant.bots.virtual_service import smm

    async def rate(_bid):
        return 4359, 1, 100000
    monkeypatch.setattr(smm, "_stars_sell_settings", rate)
    env.settings.update(wallet_pay_purchase_enabled="0")
    t = Target()
    await smm._stars_show_invoice(t, FakeBot(), TG, "خودم", 50)
    text, kw = t.answers[0]
    got = cbs(kw["reply_markup"])
    assert "star:cardpay:555:50" in got and "star:pay:555:50" not in got
    assert "217,950" in text
    env.settings.update(card_enabled="0")
    t = Target()
    await smm._stars_show_invoice(t, FakeBot(), TG, "خودم", 50)
    text, kw = t.answers[0]
    assert "star:pay:555:50" not in cbs(kw["reply_markup"]) and "هیچ روشِ پرداختی" in text


async def test_star_pay_with_low_balance_offers_charge_and_order_instead_of_plain_alert(env, monkeypatch):
    from tenant.bots.virtual_service import smm

    async def rate(_bid):
        return 4359, 1, 100000

    async def get_user(*a, **k):
        async with env.maker() as s:
            return await s.get(models.BotUser, 1), False
    monkeypatch.setattr(smm, "_stars_sell_settings", rate)
    monkeypatch.setattr(smm, "Session", env.maker)
    monkeypatch.setattr(tenant, "get_or_create_user", get_user)
    shown, answered = [], []

    async def ans(*a, **k):
        answered.append((a, k))
    msg = SimpleNamespace(message_id=1, answer=lambda *a, **k: _rec(shown, a, k))
    cb = SimpleNamespace(data="star:pay:555:50", message=msg, answer=ans,
                         from_user=SimpleNamespace(id=TG, full_name="Ali", username="ali"))

    class St:
        async def get_data(self):
            return {}
    await smm.star_pay(cb, FakeBot(), St())
    assert shown, "باید پیامِ شارژ حساب و ثبت سفارش بیاید"
    text, kw = shown[0][0][0], shown[0][1]
    assert "217,950" in text and any(c.startswith("vsc:o:") for c in cbs(kw["reply_markup"]))
    assert not any(a[0] and "پرداخت شد" in str(a[0][0]) for a in answered)


async def test_charge_row_is_not_resumable_and_uses_shortfall(env):
    rows = await VP.charge_row(BOT_ID, TG, 80_000)
    open_cb = rows[0][0].callback_data
    assert open_cb.startswith("vsc:o:")
    shown = []

    class Msg:
        async def answer(self, text, **kw):
            shown.append((text, kw))

    async def ans(*a, **k):
        pass
    await _set_balance(env, 30_000)
    await VP.vsc_open(SimpleNamespace(data=open_cb, from_user=SimpleNamespace(id=TG), message=Msg(), answer=ans), object())
    text, kw = shown[0]
    assert "wcamt:50000" in cbs(kw["reply_markup"])             # کمبود = ۸۰٬۰۰۰ − ۳۰٬۰۰۰
    assert (await VP._load(BOT_ID, TG)).get("armed") is None     # ادامهٔ خودکار ندارد
