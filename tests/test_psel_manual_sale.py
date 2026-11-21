"""PATCH_PSEL_MANUAL_SALE_V1 — ربات پنل‌فروشی: فروشِ دستی (بدونِ اتصال به پنل)؛ مالک مشخصات را خودش می‌فرستد (تیکتِ ساجد/آرشیا)."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import admin as ADM
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller import user as U

BOT, OWNER, BUYER = 7, 500, 1001


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    import tenant.gateways.discount_engine as DE
    for mod in (config, SV, tenant, DE, U, ADM):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    async with m() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=BOT, telegram_id=BUYER, full_name="Ali", balance=100_000))
        s.add(models.PanelSellerPackage(id=5, bot_id=BOT, panel_id=0, title="پنل دستی ۱۰۰ گیگ", data_gb=100, max_users=10, days=30,
                                        price_toman=40_000, active=True, manual=True))
        s.add(models.PanelSellerPackage(id=6, bot_id=BOT, panel_id=3, title="خودکار", data_gb=50, max_users=5, days=30,
                                        price_toman=10_000, active=True))
        s.add(models.DiscountCode(id=1, bot_id=BOT, code="OFF25", percent=25, disc_type="percent", is_active=True))
        await s.commit()
    yield m
    await engine.dispose()


async def balance(m):
    async with m() as s:
        return int((await s.get(models.BotUser, 1)).balance)


@pytest.mark.asyncio
async def test_buy_deliver_flow(env):
    r = await SV.buy_manual(BOT, BUYER, 5)
    assert r["status"] == "ok" and r["paid"] == 40_000 and r["tracking"] and await balance(env) == 60_000
    assert [o.id for o in await SV.manual_orders(BOT)] == [r["order_id"]]
    o = await SV.deliver_manual(BOT, r["order_id"], "آدرس: https://p.example.com\nیوزر: u1\nرمز: p1")
    assert o.status == "delivered" and "u1" in o.delivered_text
    assert await SV.deliver_manual(BOT, r["order_id"], "x") is None and await SV.refund_manual(BOT, r["order_id"]) is None
    assert await SV.manual_orders(BOT) == [] and await balance(env) == 60_000


@pytest.mark.asyncio
async def test_guards_and_insufficient(env):
    assert (await SV.buy_manual(BOT, BUYER, 6))["status"] == "not_manual"          # بستهٔ خودکار از این مسیر نمی‌گذرد
    assert (await SV.buy_manual(BOT, BUYER, 999))["status"] == "no_package"
    assert (await SV.buy_manual(99, BUYER, 5))["status"] == "no_package"           # ربات دیگر
    async with env() as s:
        (await s.get(models.BotUser, 1)).balance = 1000
        await s.commit()
    r = await SV.buy_manual(BOT, BUYER, 5)
    assert r["status"] == "insufficient" and r["need"] == 40_000 and await SV.manual_orders(BOT) == []


@pytest.mark.asyncio
async def test_discount_recorded_and_reverted_on_refund(env):
    r = await SV.buy_manual(BOT, BUYER, 5, discount_code="off25")
    assert r["status"] == "ok" and r["paid"] == 30_000 and r["discount"] == 10_000 and await balance(env) == 70_000
    async with env() as s:
        assert (await s.get(models.DiscountCode, 1)).used_count == 1
    o = await SV.refund_manual(BOT, r["order_id"])
    assert o.status == "refunded" and await balance(env) == 100_000
    async with env() as s:
        assert (await s.get(models.DiscountCode, 1)).used_count == 0
    assert (await SV.buy_manual(BOT, BUYER, 5, discount_code="NOPE"))["status"] == "bad_discount"


class Msg:
    def __init__(self):
        self.out, self.edits = [], []

    async def edit_text(self, text, reply_markup=None, **k):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **k):
        self.out.append((text, reply_markup))


class Cb:
    def __init__(self, data, uid=BUYER):
        self.data, self.from_user, self.message, self.alerts = data, NS(id=uid, full_name="Ali", username="ali"), Msg(), []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_user_buy_screen_lists_manual_package_and_notifies_owner(env, monkeypatch):
    notes = []

    async def notify(bot, row, text, kb=None, photo_file_id=None):
        notes.append((text, kb))
    monkeypatch.setattr(tenant, "_notify_admins", notify)
    row = NS(id=BOT, owner_id=OWNER, plan="pro", field="panel_seller")
    cb = Cb("psel:buy")
    await U.show_buy(cb, NS(), row, BUYER)
    kbs = [b.callback_data for r in cb.message.edits[0][1].inline_keyboard for b in r]
    assert "psel:b:5" in kbs and "psel:b:6" not in kbs                              # پنلِ خودکار (panel_id=3 بدونِ ردیفِ پنل) نمایش داده نمی‌شود
    cb2 = Cb("psel:bok:5")
    r = await U._manual_buy(cb2, NS(), row, BUYER, 5, None)
    assert r["status"] == "ok" and "سفارشت ثبت شد" in cb2.message.edits[0][0]
    text, kb = notes[0]
    assert "سفارشِ جدیدِ پنل" in text and kb.inline_keyboard[0][0].callback_data == f"psel:mod:{r['order_id']}"
    my = Cb("psel:my")
    await U.show_my(my, NS(), row, BUYER)
    assert any(f"psel:myo:{r['order_id']}" == b.callback_data for rr in my.message.edits[0][1].inline_keyboard for b in rr)


class St:
    def __init__(self, **d):
        self.d, self.state = dict(d), None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **k):
        self.d.update(k)

    async def set_state(self, s=None):
        self.state = s

    async def clear(self):
        self.d.clear()


@pytest.mark.asyncio
async def test_admin_deliver_and_refund_handlers(env, monkeypatch):
    sent = []
    bot = NS(send_message=None)

    async def send_message(chat, text, **k):
        sent.append((chat, text))
    bot.send_message = send_message

    async def guard(event, b):
        return NS(id=BOT, owner_id=OWNER, field="panel_seller")
    monkeypatch.setattr(ADM, "_guard", guard)
    r1 = await SV.buy_manual(BOT, BUYER, 5)
    r2 = await SV.buy_manual(BOT, BUYER, 5)
    lst = Cb("psel:mo", OWNER)
    await ADM.mo_list(lst, bot, St())
    assert f"psel:mod:{r1['order_id']}" in [b.callback_data for rr in lst.message.edits[0][1].inline_keyboard for b in rr]
    ask = Cb(f"psel:mdl:{r1['order_id']}", OWNER)
    st = St()
    await ADM.mo_deliver_ask(ask, bot, st)
    assert st.state == ADM.PselFSM.deliver and st.d["mo_id"] == r1["order_id"]
    msg = NS(text="آدرس https://p.example.com\nuser u1\npass p1", from_user=NS(id=OWNER), out=[])

    async def answer(t, reply_markup=None, **k):
        msg.out.append(t)
    msg.answer = answer
    await ADM.mo_deliver_in(msg, bot, st)
    assert sent and sent[0][0] == BUYER and "u1" in sent[0][1] and "ارسال شد" in msg.out[0]
    rf = Cb(f"psel:mrfy:{r2['order_id']}", OWNER)
    await ADM.mo_refund_do(rf, bot)
    assert await balance(env) == 100_000 - 80_000 + 40_000 and any("لغو شد" in t for _, t in sent)
    my = Cb(f"psel:myo:{r1['order_id']}")

    async def get_row(bot):
        return NS(id=BOT, owner_id=OWNER, plan="pro", field="panel_seller")
    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    await U.my_order(my, NS(token="7:T"))
    assert "u1" in my.message.edits[0][0] and "مشخصاتِ پنل" in my.message.edits[0][0]
    other = Cb(f"psel:myo:{r1['order_id']}", uid=999)                       # سفارشِ دیگران دیده نمی‌شود
    await U.my_order(other, NS(token="7:T"))
    assert not other.message.edits and other.alerts


@pytest.mark.asyncio
async def test_manual_package_creation_flow(env, monkeypatch):
    async def guard(event, b):
        return NS(id=BOT, owner_id=OWNER, field="panel_seller")
    monkeypatch.setattr(ADM, "_guard", guard)
    st = St()
    cb = Cb("psel:pkm", OWNER)
    cb.message = NS(chat=NS(id=1), message_id=2, edit_text=Msg().edit_text, answer=Msg().answer)
    await ADM.pk_manual(cb, NS(), st)
    assert st.d["psel_manual"] == 1 and st.d["psel_panel"] == 0 and st.state == ADM.PselFSM.title
    st.d.update(psel_title="دستی جدید", psel_gb=20, psel_users=3, psel_days=30)
    out = []
    m = NS(text="55000", from_user=NS(id=OWNER))

    async def answer(t, reply_markup=None, **k):
        out.append(t)
    m.answer = answer
    await ADM.pk_price(m, NS(), st)
    async with env() as s:
        p = (await s.scalars(select(models.PanelSellerPackage).where(models.PanelSellerPackage.title == "دستی جدید"))).one()
    assert p.manual is True and p.panel_id == 0 and p.price_toman == 55_000


@pytest.mark.asyncio
async def test_admin_menu_shows_pending_orders_button(env):
    await SV.buy_manual(BOT, BUYER, 5)
    text, markup = await ADM.menu_content(NS(id=BOT, owner_id=OWNER, plan="pro"))
    labels = [b.text for r in markup.inline_keyboard for b in r]
    assert any(l.startswith("📨 سفارش‌هایِ دستی") and "(" in l for l in labels) and "منتظرِ ارسالِ مشخصات" in text
