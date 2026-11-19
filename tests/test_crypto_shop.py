"""PATCH_CRYPTO_SHOP_V1 (Sajad): «فروشِ رمزارز» — ترون و تتر کنارِ تون؛ نرخ، آدرس، فاکتور، پرداخت از کیف‌پول، تحویل/بازگشتِ وجه."""
import time
import types
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
import tenant.bots.virtual_service.crypto_shop as CX
from tenant.bots.virtual_service import smm as S

OWNER, BUYER = 1000, 111
BOT = 5
TRC = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
EVM = "0x" + "ab" * 20
TONADDR = "UQBvW8Z5huBkMJYdnfAEM5JqTNkuWX3diqYENkWsIL0XggGG"


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
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="shop", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=BOT, telegram_id=BUYER, full_name="buyer", username="b", balance=Decimal(1_000_000)))
        s.add(models.BotUser(bot_id=BOT, telegram_id=OWNER, full_name="owner", username="o", balance=Decimal(0)))
        await s.commit()

    async def get_bot_row(b):
        async with mk() as s:
            return await s.get(models.Bot, BOT)
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    notes = []

    async def notify(bot, row, text, kb=None, photo_file_id=None):
        notes.append((text, kb))
    monkeypatch.setattr(S, "_notify_manual_order", notify)
    yield types.SimpleNamespace(mk=mk, notes=notes)
    await engine.dispose()
    tenant._SETTING_CACHE.clear()


class Bot:
    token = "5:T"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class Msg:
    def __init__(self, uid=BUYER, text=""):
        self.text, self.out = text, []
        self.from_user = types.SimpleNamespace(id=uid, full_name="buyer", username="b")
        self.chat = types.SimpleNamespace(id=uid, type="private")
        self.message_id = 1

    async def answer(self, text, **kw):
        self.out.append((text, kw))
        return types.SimpleNamespace(message_id=2)

    async def edit_text(self, text, **kw): self.out.append((text, kw))

    async def edit_reply_markup(self, **kw): pass

    async def delete(self): pass

    html_text = ""
    photo = document = None


class Cb:
    def __init__(self, data, uid=BUYER):
        self.data, self.message, self.answers = data, Msg(uid), []
        self.from_user = types.SimpleNamespace(id=uid, full_name="buyer", username="b")

    async def answer(self, text="", show_alert=False, **k): self.answers.append((text, show_alert))

    def model_copy(self, update): return self


def fsm(uid=BUYER):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=5, chat_id=uid, user_id=uid))


async def enable(coin="usdt", rate=60_000, **extra):
    await tenant.set_setting(BOT, f"cx_{coin}_on", "1")
    await tenant.set_setting(BOT, f"cx_{coin}_rate", str(rate))
    for k, v in extra.items():
        await tenant.set_setting(BOT, f"cx_{coin}_{k}", str(v))


async def bal(env, uid=BUYER):
    async with env.mk() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == uid))).balance)


async def orders(env):
    async with env.mk() as s:
        return (await s.scalars(select(models.GiftOrder))).all()


def test_amount_and_address_validation():
    assert CX.parse_amount("12.5") == 12.5 and CX.parse_amount("۱۲٫۵") == 12.5 and CX.parse_amount("1,000") == 1000
    assert CX.parse_amount("0") is None and CX.parse_amount("abc") is None and CX.parse_amount("1.234") is None
    assert CX.valid_address("TRC20", TRC) and not CX.valid_address("TRC20", EVM) and not CX.valid_address("TRC20", TRC[:-1])
    assert CX.valid_address("BEP20", EVM) and CX.valid_address("ERC20", EVM) and not CX.valid_address("BEP20", TRC)
    assert CX.valid_address("TON", TONADDR) and not CX.valid_address("TON", TRC)
    assert CX._g(12.5) == "12.5" and CX._g(1000000.0) == "1000000" and CX._g(10.0) == "10"           # بدونِ 1e+06


@pytest.mark.asyncio
async def test_without_other_coins_the_ton_shop_is_unchanged(env):
    t = Msg()
    await S.show_ton_shop_to_user(Bot(), t, BOT, fsm())
    assert "فروشِ تون فعلاً در دسترس نیست" in t.out[0][0]          # هیچ ارزِ دیگری روشن نیست ⇒ همان رفتارِ قبلی


@pytest.mark.asyncio
async def test_hub_shows_enabled_coins_and_ton_only_when_it_is_ready(env, monkeypatch):
    await enable("usdt")
    await enable("trx", 7000)
    t = Msg()
    await S.show_ton_shop_to_user(Bot(), t, BOT, fsm())
    text, kw = t.out[0]
    cbs = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert "خرید رمز ارز" in text and "cx:c:usdt" in cbs and "cx:c:trx" in cbs and "cx:ton" not in cbs     # تون نرخ ندارد ⇒ نیست

    async def ton_ok(_b): return (150_000.0, 1.0, 100.0)
    monkeypatch.setattr(S, "_ton_sell_settings", ton_ok)
    t2 = Msg()
    await S.show_ton_shop_to_user(Bot(), t2, BOT, fsm())
    assert "cx:ton" in [b.callback_data for r in t2.out[0][1]["reply_markup"].inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_full_purchase_flow_usdt_trc20(env):
    await enable("usdt", 60_000, min=2, max=500)
    st, bot = fsm(), Bot()
    cb = Cb("cx:c:usdt")
    await CX.cx_coin(cb, bot, st)
    page = cb.message.out[0][0]
    assert "60,000" in page and "TRC20" in page and "USDT" in page                       # قیمتِ روز + شبکه
    cb2 = Cb("cx:q:usdt:10")
    await CX.cx_pick(cb2, bot, st)
    assert "آدرسِ کیف‌پولِ" in cb2.message.out[0][0]
    bad = Msg(text="notanaddress")
    await CX.cx_addr_in(bad, bot, st)
    assert "معتبر نیست" in bad.out[0][0] and await bal(env) == 1_000_000
    ok = Msg(text=TRC)
    await CX.cx_addr_in(ok, bot, st)
    inv = ok.out[0][0]
    assert "600,000" in inv and TRC in inv                                              # ۱۰ × ۶۰٬۰۰۰
    pay = Cb("cx:pay")
    await CX.cx_pay(pay, bot, st)
    assert await bal(env) == 400_000
    (o,) = await orders(env)
    assert o.gift_id == "cxsell_usdt_1000" and o.status == "awaiting_admin" and o.price_toman == 600_000
    assert o.dest_label == TRC and o.dest_memo == "net:TRC20" and o.tracking_code
    assert "سفارشت ثبت شد" in pay.message.out[0][0]
    assert len(env.notes) == 1 and TRC in env.notes[0][0] and "TRC20" in env.notes[0][0]        # ادمین با آدرس و شبکه خبر شد
    cbs = [b.callback_data for r in env.notes[0][1].inline_keyboard for b in r]
    assert cbs == [f"cx:done:{o.id}", f"cx:refund:{o.id}"]
    async with env.mk() as s:
        t = (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.kind == "purchase"))).one()
    assert t.amount == -600_000


@pytest.mark.asyncio
async def test_insufficient_balance_creates_no_order(env):
    await enable("usdt", 60_000)
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(Cb("cx:q:usdt:50"), bot, st)                                       # ۳٬۰۰۰٬۰۰۰ > ۱٬۰۰۰٬۰۰۰
    await CX.cx_addr_in(Msg(text=TRC), bot, st)
    pay = Cb("cx:pay")
    await CX.cx_pay(pay, bot, st)
    assert await bal(env) == 1_000_000 and await orders(env) == [] and env.notes == []
    assert any("موجودی کافی نیست" in a[0] for a in pay.answers)


@pytest.mark.asyncio
async def test_range_is_enforced_for_picks_and_custom_amounts(env):
    await enable("usdt", 60_000, min=5, max=20)
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    low = Msg(text="1")
    await CX.cx_amount_in(low, bot, st)
    assert "باید بینِ" in low.out[0][0]
    high = Cb("cx:q:usdt:100")
    await CX.cx_pick(high, bot, st)
    assert "باید بینِ" in high.message.out[0][0] and "cx_amount" not in await st.get_data()
    good = Msg(text="7.5")
    await CX.cx_amount_in(good, bot, st)
    assert "7.5" in good.out[0][0] and (await st.get_data())["cx_amount"] == 7.5


@pytest.mark.asyncio
async def test_usdt_network_setting_changes_the_expected_address(env):
    await enable("usdt", 60_000)
    await tenant.set_setting(BOT, "cx_usdt_net", "BEP20")
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(Cb("cx:q:usdt:10"), bot, st)
    wrong = Msg(text=TRC)                                       # آدرسِ ترون برایِ BEP20 رد می‌شود
    await CX.cx_addr_in(wrong, bot, st)
    assert "BEP20" in wrong.out[0][0] and "معتبر نیست" in wrong.out[0][0]
    right = Msg(text=EVM)
    await CX.cx_addr_in(right, bot, st)
    assert "فاکتور" in right.out[0][0]


@pytest.mark.asyncio
async def test_trx_is_always_trc20(env):
    await enable("trx", 7_000, min=10, max=5000)
    await tenant.set_setting(BOT, "cx_usdt_net", "ERC20")      # شبکهٔ تتر روی ترون اثر ندارد
    assert await CX.network_of(BOT, "trx") == "TRC20"
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:trx"), bot, st)
    await CX.cx_pick(Cb("cx:q:trx:100"), bot, st)
    m = Msg(text=TRC)
    await CX.cx_addr_in(m, bot, st)
    assert "700,000" in m.out[0][0]
    await CX.cx_pay(Cb("cx:pay"), bot, st)
    (o,) = await orders(env)
    assert o.gift_id == "cxsell_trx_10000" and await bal(env) == 300_000


@pytest.mark.asyncio
async def test_auto_rate_uses_live_prices_and_falls_back_to_the_manual_rate(env, monkeypatch):
    import tenant.gateways.stars_rate as SR
    await enable("trx", 7_000, auto=1, margin=10)
    await enable("usdt", 58_000, auto=1, margin=5)
    CX._PRICE_CACHE.clear()

    async def usd(): return 60_000.0

    async def trx_usd(): return 0.12
    monkeypatch.setattr(SR, "usd_toman", usd)
    monkeypatch.setattr(CX, "_fetch_trx_usd", trx_usd)
    assert round(await CX.rate_of(BOT, "usdt")) == 63_000                    # ۶۰٬۰۰۰ × ۱٫۰۵
    assert round(await CX.rate_of(BOT, "trx")) == 7_920                      # ۰٫۱۲ × ۶۰٬۰۰۰ × ۱٫۱

    async def usd_down(): return 0.0
    monkeypatch.setattr(SR, "usd_toman", usd_down)
    assert await CX.rate_of(BOT, "usdt") == 58_000 and await CX.rate_of(BOT, "trx") == 7_000   # شکستِ API ⇒ نرخِ دستی


@pytest.mark.asyncio
async def test_coin_without_a_rate_is_not_sellable(env):
    await tenant.set_setting(BOT, "cx_usdt_on", "1")                         # روشن ولی نرخ صفر
    assert await CX.enabled_coins(BOT) == []
    cb = Cb("cx:c:usdt")
    await CX.cx_coin(cb, Bot(), fsm())
    assert cb.answers[0][1] and "در دسترس نیست" in cb.answers[0][0]


@pytest.mark.asyncio
async def test_expired_invoice_is_rejected_and_nothing_is_charged(env):
    await enable("usdt", 60_000)
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(Cb("cx:q:usdt:10"), bot, st)
    await CX.cx_addr_in(Msg(text=TRC), bot, st)
    await st.update_data(cx_ts=time.time() - CX.INVOICE_TTL - 5)
    pay = Cb("cx:pay")
    await CX.cx_pay(pay, bot, st)
    assert "منقضی" in pay.answers[0][0] and await bal(env) == 1_000_000 and await orders(env) == []


@pytest.mark.asyncio
async def test_invoice_price_is_locked_even_if_the_rate_moves(env):
    await enable("usdt", 60_000)
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(Cb("cx:q:usdt:10"), bot, st)
    await CX.cx_addr_in(Msg(text=TRC), bot, st)
    await tenant.set_setting(BOT, "cx_usdt_rate", "99000")                   # نرخ بعد از صدورِ فاکتور عوض شد
    await CX.cx_pay(Cb("cx:pay"), bot, st)
    assert (await orders(env))[0].price_toman == 600_000 and await bal(env) == 400_000


async def _placed_order(env):
    await enable("usdt", 60_000)
    st, bot = fsm(), Bot()
    await CX.cx_coin(Cb("cx:c:usdt"), bot, st)
    await CX.cx_pick(Cb("cx:q:usdt:10"), bot, st)
    await CX.cx_addr_in(Msg(text=TRC), bot, st)
    await CX.cx_pay(Cb("cx:pay"), bot, st)
    return (await orders(env))[0]


@pytest.mark.asyncio
async def test_admin_marks_sent_once_and_the_buyer_is_notified(env, monkeypatch):
    o = await _placed_order(env)

    async def trust(*a, **k): return None
    monkeypatch.setattr(S, "_notify_gift_order_trust_success", trust)
    bot = Bot()
    cb = Cb(f"cx:done:{o.id}", uid=OWNER)
    await CX.cx_done(cb, bot)
    assert (await orders(env))[0].status == "sent"
    assert bot.sent and bot.sent[0][0] == BUYER and "ارسال شد" in bot.sent[0][1] and TRC in bot.sent[0][1]
    again = Cb(f"cx:done:{o.id}", uid=OWNER)
    await CX.cx_done(again, bot)
    assert "قبلاً رسیدگی" in again.answers[0][0] and len(bot.sent) == 1               # دوباره‌زدن اثری ندارد
    ref = Cb(f"cx:refund:{o.id}", uid=OWNER)
    await CX.cx_refund(ref, bot)
    assert "قبلاً رسیدگی" in ref.answers[0][0] and await bal(env) == 400_000          # بعد از ارسال، بازپرداخت ممکن نیست


@pytest.mark.asyncio
async def test_admin_refund_returns_the_money_exactly_once(env):
    o = await _placed_order(env)
    assert await bal(env) == 400_000
    bot = Bot()
    await CX.cx_refund(Cb(f"cx:refund:{o.id}", uid=OWNER), bot)
    assert await bal(env) == 1_000_000 and (await orders(env))[0].status == "refunded"
    assert bot.sent and "بازگشت" in bot.sent[0][1]
    await CX.cx_refund(Cb(f"cx:refund:{o.id}", uid=OWNER), bot)
    assert await bal(env) == 1_000_000                                              # دوبار برنگشت
    async with env.mk() as s:
        kinds = [t.kind for t in (await s.scalars(select(models.WalletTxn).order_by(models.WalletTxn.id))).all()]
    assert kinds == ["purchase", "charge"]


@pytest.mark.asyncio
async def test_customers_cannot_use_admin_handlers(env):
    o = await _placed_order(env)
    bot = Bot()
    for fn, data in ((CX.cx_done, f"cx:done:{o.id}"), (CX.cx_refund, f"cx:refund:{o.id}"), (CX.cx_orders, "cx:orders:0")):
        cb = Cb(data, uid=BUYER)
        await fn(cb, bot)                                                           # (در تلگرامِ واقعی «⛔️ فقط ادمین» نشان می‌دهد)
        assert cb.message.out == []
    assert (await orders(env))[0].status == "awaiting_admin" and await bal(env) == 400_000 and bot.sent == []
    cb = Cb("cx:adm", uid=BUYER)
    await CX.cx_adm(cb, Bot(), fsm(BUYER))
    assert cb.message.out == []
    await CX.cx_toggle(Cb("cx:t:usdt:on", uid=BUYER), bot)
    assert (await CX.coin_cfg(BOT, "usdt"))["manual"] == 60_000 and (await _gs_on()) is True      # مشتری نرخ/وضعیت را عوض نکرد


async def _gs_on():
    return (await CX.coin_cfg(BOT, "usdt"))["on"]


@pytest.mark.asyncio
async def test_admin_panel_toggle_set_values_and_pending_list(env):
    admin, bot = fsm(OWNER), Bot()
    home = Cb("cx:adm", uid=OWNER)
    await CX.cx_adm(home, bot, admin)
    assert "ترون" in home.message.out[0][0] and "تتر" in home.message.out[0][0]
    tog = Cb("cx:t:usdt:on", uid=OWNER)
    await CX.cx_toggle(tog, bot)
    assert (await CX.coin_cfg(BOT, "usdt"))["on"] is True
    start = Cb("cx:s:usdt:rate", uid=OWNER)
    await CX.cx_set_start(start, bot, admin)
    bad = Msg(uid=OWNER, text="abc")
    await CX.cx_set_save(bad, bot, admin)
    assert "فقط عدد" in bad.out[0][0]
    good = Msg(uid=OWNER, text="۶۱٬۵۰۰")
    await CX.cx_set_save(good, bot, admin)
    assert (await CX.coin_cfg(BOT, "usdt"))["manual"] == 61_500
    await CX.cx_set_start(Cb("cx:s:usdt:margin", uid=OWNER), bot, admin)
    over = Msg(uid=OWNER, text="500")
    await CX.cx_set_save(over, bot, admin)
    assert "باید بینِ" in over.out[0][0] and (await CX.coin_cfg(BOT, "usdt"))["margin"] == 5.0
    nets = []
    for _ in range(4):
        await CX.cx_net(Cb("cx:net", uid=OWNER), bot)
        nets.append(await CX.network_of(BOT, "usdt"))
    assert nets == ["BEP20", "ERC20", "TON", "TRC20"]                                # چرخه
    await _placed_order(env)
    lst = Cb("cx:orders:0", uid=OWNER)
    await CX.cx_orders(lst, bot)
    assert "سفارش‌هایِ در انتظار" in lst.message.out[0][0] and "TRC20" in lst.message.out[0][0]


@pytest.mark.asyncio
async def test_generic_gift_screens_do_not_treat_crypto_orders_as_card_payments(env):
    """cxsell_ باید در همهٔ جاهایی که تون/استارز را از «گیفتِ معمولی» جدا می‌کنند هم جدا شود، وگرنه ادمین دکمهٔ تأییدِ کارت می‌گیرد."""
    import inspect
    src = inspect.getsource(S)
    assert 'not gid.startswith(("premium_", "starsell_", "srx_", "tonsell_", "cxsell_"))' in src
    assert src.count('"srx_", "tonsell_"') == src.count('"srx_", "tonsell_", "cxsell_"')      # هیچ فهرستِ بدونِ cxsell_ نمانده
    o = types.SimpleNamespace(gift_id="cxsell_trx_5000", star_count=5000, gift_emoji="🔴")
    assert "50" in S._gift_item_label(o) and "TRX" in S._gift_item_label(o)
