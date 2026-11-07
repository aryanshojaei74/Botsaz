"""تأییدِ رسید: خطایِ مرحلهٔ جانبی کلِ تأیید را نمی‌کُشد و شکستِ تحویل به ادمین اعلام می‌شود (PATCH_DELIVERY_GUARD_V1)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
import tenant.gateways.card_purchase as CP
import tenant.shared.delivery_guard as DG


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat, text, **kw):
        if chat in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat, text))


def make_cb(data, admin_id=1):
    edits, answers = [], []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    async def edit_text(text, **kw):
        edits.append(text)

    async def edit_caption(caption=None, **kw):
        edits.append(caption)

    async def edit_markup(**kw):
        edits.append("markup-removed")

    msg = SimpleNamespace(caption=None, text="رسید #1", chat=SimpleNamespace(type="private", id=1), message_id=5,
                          edit_text=edit_text, edit_caption=edit_caption, edit_reply_markup=edit_markup)
    cb = SimpleNamespace(data=data, from_user=SimpleNamespace(id=admin_id, username="adm", full_name="Admin"),
                         answer=answer, message=msg)
    cb.edits, cb.answers = edits, answers
    return cb


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (tenant, CP):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="Ali", username="ali", balance=0))
        s.add(models.Order(id=8, bot_id=5, user_id=1, volume_gb=10, days=30, pay_method="card", amount=120_000,
                           status="awaiting_admin"))
        await s.commit()

    async def get_bot_row(bot):
        return SimpleNamespace(id=5, owner_id=1, field="vpn_sales", plan="pro", username="x")

    async def is_admin(bot_id, tg, owner):
        return True

    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(CP, "get_bot_row", get_bot_row, raising=False)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_admin)
    yield maker
    await engine.dispose()


def test_alert_includes_buyer_error_and_sends_configs(monkeypatch):
    import asyncio
    got = []

    async def send_cfg(bot, chat, cfg):
        got.append((chat, cfg))

    monkeypatch.setattr(tenant, "send_config_to_user", send_cfg)
    bot = FakeBot()
    buyer = SimpleNamespace(full_name="Ali <b>", telegram_id=777, username="ali")
    asyncio.run(DG.alert_admin_delivery_failed(bot, 1, "سفارش #5", buyer, RuntimeError("boom"), cfgs=["C1", "C2"]))
    chat, text = bot.sent[0]
    assert chat == 1 and "ارسال به مشتری ناموفق" in text and "777" in text and "@ali" in text and "boom" in text
    assert "&lt;b&gt;" in text and got == [(1, "C1"), (1, "C2")]


@pytest.mark.asyncio
async def test_wallet_charge_approval_survives_blocked_customer_and_alerts_admin(env):
    bot = FakeBot(fail_for={777})                     # مشتری ربات را بلاک کرده
    cb = make_cb("chadm:ok:8")
    await tenant.chadm_decide(cb, bot, SimpleNamespace())
    async with env() as s:
        o = await s.get(models.Order, 8)
        u = await s.get(models.BotUser, 1)
    assert o.status == "paid" and float(u.balance) == 120_000                  # مالی درست ثبت شد
    assert cb.edits and "تایید شد توسط ادمین" in cb.edits[0]                   # دکمه‌ها برداشته شد
    alerts = [t for c, t in bot.sent if c == 1]
    assert any("ارسال به مشتری ناموفق" in t and "120,000" in t for t in alerts)


@pytest.mark.asyncio
async def test_stock_config_approval_survives_referral_and_delivery_failures(env, monkeypatch):
    import tenant.bots.vpn.stock_delivery as SD
    import tenant.shared.extras as EX
    async with env() as s:
        o = await s.get(models.Order, 8)
        o.pay_method = "mcfg_card"
        o.reserved_config_id = 3
        s.add(models.Config(id=3, bot_id=5, code=101, volume_gb=10, days=30, price=0, status="reserved",
                            payload="vless://x", kind="sublink"))
        await s.commit()

    async def off(bot_id):
        return False

    async def referral_boom(*a, **k):
        raise RuntimeError("referral exploded")

    delivered = []

    async def send_cfg(bot, chat, cfg):
        if chat == 777:
            raise RuntimeError("telegram timeout")
        delivered.append((chat, cfg.code))

    async def back_kb(bot_id):
        return None

    monkeypatch.setattr(SD, "is_on", off)
    monkeypatch.setattr(EX, "handle_referral_purchase", referral_boom)
    monkeypatch.setattr(tenant, "send_config_to_user", send_cfg)
    monkeypatch.setattr(tenant, "inline_back_kb", back_kb)
    bot = FakeBot()
    cb = make_cb("rcadm:ok:8")
    await CP._rcadm_decide_impl(cb, bot, SimpleNamespace())
    async with env() as s:
        o = await s.get(models.Order, 8)
        cfg = await s.get(models.Config, 3)
    assert o.status == "paid" and cfg.status == "sold"
    assert cb.edits and "تایید شد توسط ادمین" in cb.edits[0]                   # با وجودِ خطایِ پاداشِ دعوت دکمه‌ها رفت
    assert any(c == 1 and "ارسال به مشتری ناموفق" in t for c, t in bot.sent)  # ادمین خبردار شد
    assert delivered == [(1, 101)]                                             # کانفیگ برایِ خودِ ادمین رسید
