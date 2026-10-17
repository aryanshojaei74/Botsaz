"""
برگشت وجهِ VPN با تأییدِ ادمین (PATCH_VPN_REFUND_APPROVAL_V1): کاربر دلیل می‌نویسد → درخواست برایِ ادمین
می‌رود → فقط با تأییدِ ادمین پول به کیف پول برمی‌گردد و سرویس از پنل حذف می‌شود.
"""
from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, ADMIN, BOT_TG = 555, 1, 8354597347


class Capture(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in ("SendMessage", "EditMessageText"):
            return Message(message_id=getattr(method, "message_id", None) or 900 + len(self.calls),
                           date=datetime.datetime.now(), chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", "x")).as_(bot)
        return True

    def sent(self, to):
        return [m for m in self.calls if type(m).__name__ == "SendMessage" and m.chat_id == to]


class FakePanel:
    deleted = []

    async def get_usage(self, email):
        return {"up": 0, "down": 2 * 1024 ** 3, "total": 10 * 1024 ** 3, "expiryTime": 0}

    async def delete_account(self, email):
        FakePanel.deleted.append(email)

    async def close(self): ...


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    from tenant.bots.vpn import vpn_user
    from tenant.shared import public_api as _pa
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa, vpn_user):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])

    async def fake_client(panel):
        return FakePanel()
    monkeypatch.setattr(vpn_user, "_panel_client", fake_client)
    FakePanel.deleted = []
    async with maker() as s:
        b = models.Bot(owner_id=ADMIN, token="123:TESTTOKEN", username="shop", field="vpn", plan="pro", status="active")
        s.add(b)
        await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", balance=0))
        s.add(models.VpnPanel(bot_id=b.id, panel_key="sanai", enabled=True, base_url="https://p", username="u", password="p"))
        acc = models.PanelAccount(bot_id=b.id, user_telegram_id=USER, email="e1", panel_key="sanai",
                                  paid_toman=100000, status="active", volume_gb=10, days=30)
        s.add(acc)
        await s.commit()
        bid, aid = b.id, acc.id
    await tenant.set_setting(bid, "refund_enabled", "1")
    await tenant.set_setting(bid, "refund_fee_pct", "10")
    monkeypatch.setattr(vpn_user.vpn_user_router, "_parent_router", None)   # روترِ واقعی؛ جدا از tenant_router
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(vpn_user.vpn_user_router)
    cap = Capture()
    bot = Bot("123:TESTTOKEN", session=cap)
    return SimpleNS(dp=dp, bot=bot, cap=cap, maker=maker, bid=bid, aid=aid)


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def cb_update(uid, data, uid_n=1):
    u = User(id=uid, is_bot=False, first_name="U")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
                text="x", from_user=User(id=BOT_TG, is_bot=True, first_name="bot"))
    return Update(update_id=uid_n, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=m))


def msg_update(uid, text, n=2):
    u = User(id=uid, is_bot=False, first_name="U")
    return Update(update_id=n, message=Message(message_id=7, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
                                                text=text, from_user=u))


async def balance(env):
    async with env.maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
        return float(u.balance)


@pytest.mark.asyncio
async def test_full_flow_admin_approves(env):
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrf:{env.aid}"))
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrf:req:{env.aid}", 3))
    await env.dp.feed_update(env.bot, msg_update(USER, "وصل نمیشه و سرعتش خیلی پایینه", 4))
    async with env.maker() as s:
        req = await s.scalar(select(models.VpnRefundRequest))
    assert req and req.status == "pending" and req.quoted_net == 72000   # 100000 × 80٪ × (۱-۱۰٪)
    assert await balance(env) == 0 and FakePanel.deleted == []           # هنوز چیزی برنگشته
    adm = [m.text for m in env.cap.sent(ADMIN) if "درخواستِ برگشت وجه" in (m.text or "")]
    assert adm and "20٪ مصرف‌شده" in adm[0] and "کارمزد (10٪)" in adm[0] and "کلِ مبلغِ تسویه: 72,000" in adm[0]
    await env.dp.feed_update(env.bot, cb_update(ADMIN, f"vrfa:ok:{req.id}", 5))
    assert await balance(env) == 72000 and FakePanel.deleted == ["e1"]
    async with env.maker() as s:
        assert (await s.get(models.PanelAccount, env.aid)).status == "refunded"
        assert (await s.get(models.VpnRefundRequest, req.id)).status == "approved"
    assert any("تأیید شد" in (m.text or "") for m in env.cap.sent(USER))
    # دوبار-کلیکِ ادمین دوباره پول نمی‌دهد
    await env.dp.feed_update(env.bot, cb_update(ADMIN, f"vrfa:ok:{req.id}", 6))
    assert await balance(env) == 72000


@pytest.mark.asyncio
async def test_reject_and_non_admin_and_duplicates(env):
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrf:req:{env.aid}"))
    await env.dp.feed_update(env.bot, msg_update(USER, "اشتباهی خریدم", 3))
    async with env.maker() as s:
        req = await s.scalar(select(models.VpnRefundRequest))
    # کاربرِ عادی نمی‌تواند تأیید کند
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrfa:ok:{req.id}", 4))
    assert await balance(env) == 0 and FakePanel.deleted == []
    # درخواستِ دوم برایِ همان سرویس ثبت نمی‌شود
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrf:req:{env.aid}", 5))
    await env.dp.feed_update(env.bot, msg_update(USER, "دوباره درخواست", 6))
    async with env.maker() as s:
        assert len((await s.scalars(select(models.VpnRefundRequest))).all()) == 1
    # رد
    await env.dp.feed_update(env.bot, cb_update(ADMIN, f"vrfa:no:{req.id}", 7))
    async with env.maker() as s:
        assert (await s.get(models.VpnRefundRequest, req.id)).status == "rejected"
        assert (await s.get(models.PanelAccount, env.aid)).status == "active"
    assert await balance(env) == 0 and FakePanel.deleted == []
    assert any("رد شد" in (m.text or "") for m in env.cap.sent(USER))


@pytest.mark.asyncio
async def test_short_reason_is_not_accepted(env):
    await env.dp.feed_update(env.bot, cb_update(USER, f"vrf:req:{env.aid}"))
    await env.dp.feed_update(env.bot, msg_update(USER, "نه", 3))
    async with env.maker() as s:
        assert (await s.scalar(select(models.VpnRefundRequest))) is None
