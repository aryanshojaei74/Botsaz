"""nitroteeam: لاگِ خطاها + اعلانِ خریدِ ناتمام با دکمهٔ عودت + تاریخ در لاگِ تراکنش‌ها."""
import datetime as dt
import sys
import types

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import err_log as EL
from tenant.shared import orphan_purchases as OP
from tenant.shared import tx_channel as TXC

pytestmark = pytest.mark.asyncio


def _clear_cache():
    for n in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        c = getattr(tenant, n, None)
        if hasattr(c, "clear"):
            c.clear()


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    _clear_cache()
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=500, full_name="u", username="u", balance=0))
        await s.commit()
    yield mk
    await eng.dispose()
    _clear_cache()


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, reply_markup=None, **k):
        self.sent.append((chat, text, reply_markup))


async def _txn(mk, minutes_ago, amount=-172500, note="خرید VPN: نامحدود - 1 کاربره", kind="purchase", tid=None):
    async with mk() as s:
        t = models.WalletTxn(bot_id=7, user_id=1, amount=amount, kind=kind, note=note,
                             created_at=dt.datetime.utcnow() - dt.timedelta(minutes=minutes_ago))
        s.add(t)
        await s.commit()
        return t.id


async def test_tx_log_and_purchase_log_now_carry_a_date_line(env, monkeypatch):
    async def gs(bid, key, default=""):
        return {"tx_log_channel": "-100", "tx_log_purchases": "1"}.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    bot = _Bot()
    await TXC.tx_log(bot, 7, buyer=None, amount=1000, gateway="tonpays", old_balance=0, new_balance=1000)
    await TXC.tx_log_purchase(bot, 7, buyer=None, section="سرویس", detail="d", amount=1000)
    assert len(bot.sent) == 2 and all("🕒 تاریخ:" in m[1] for m in bot.sent)


async def test_err_channel_posts_with_stamp_and_is_silent_when_unset(env, monkeypatch):
    store = {}

    async def gs(bid, key, default=""): return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    bot = _Bot()
    assert await EL.post(bot, 7, "x") is False and bot.sent == []              # وصل نیست ⇒ بی‌صدا
    store["err_log_channel"] = "-1001"
    assert await EL.post(bot, 7, "🚨 خطا") is True
    assert bot.sent[0][0] == -1001 and "🕒" in bot.sent[0][1] and "🚨 خطا" in bot.sent[0][1]

    class Boom(_Bot):
        async def send_message(self, *a, **k): raise RuntimeError("forbidden")
    assert await EL.post(Boom(), 7, "x") is False                               # خطایِ ارسال هرگز بالا نمی‌رود


async def test_orphan_detector_flags_only_real_orphans(env):
    orphan = await _txn(env, 30)                                              # ۳۰ دقیقه پیش، بدونِ اکانت/عودت ⇒ مشکوک
    await _txn(env, 1)                                                        # ۱ دقیقه پیش: در جریان است ⇒ نه
    await _txn(env, 60 * 30)                                                  # ۳۰ ساعت پیش: از پنجره بیرون ⇒ نه
    ok_txn = await _txn(env, 40, note="خرید VPN: حجمی")
    refunded = await _txn(env, 50, note="خرید VPN: مولتی")
    async with env() as s:
        now = dt.datetime.utcnow()
        s.add(models.PanelAccount(bot_id=7, user_telegram_id=500, email="e", panel_key="marzban", status="active",
                                  created_at=now - dt.timedelta(minutes=39)))      # بعدِ خریدِ ok_txn (۴۰ دقیقه پیش) ساخته شده
        s.add(models.WalletTxn(bot_id=7, user_id=1, amount=172500, kind="refund", note="عودت",
                               created_at=now - dt.timedelta(minutes=49)))          # عودتِ خریدِ refunded
        s.add(models.WalletTxn(bot_id=7, user_id=1, amount=-5000, kind="purchase", note="خرید محصول دیجیتال",
                               created_at=now - dt.timedelta(minutes=30)))           # خریدِ غیرِ VPN ⇒ نه
        await s.commit()
    ids = [t.id for t, _tg in await OP.find_orphans()]
    assert ids == [orphan] and ok_txn not in ids and refunded not in ids


async def test_scan_announces_once_with_buttons_to_channel_and_staff(env, monkeypatch):
    t = await _txn(env, 30)
    store = {"err_log_channel": "-1009"}

    async def gs(bid, key, default=""): return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    sent = []

    class FakeBot(_Bot):
        def __init__(self, *a, **k):
            super().__init__()
            self.session = types.SimpleNamespace(close=lambda: _noop())

        async def send_message(self, chat, text, reply_markup=None, **k):
            sent.append((chat, text, reply_markup))

    async def _noop(): return None

    async def notify(bot, row, text, kb=None): sent.append(("staff", text, kb))
    monkeypatch.setattr(OP, "Bot", FakeBot)
    monkeypatch.setattr(tenant, "_notify_admins", notify)
    assert await OP.scan_once() == 1
    targets = [x[0] for x in sent]
    assert -1009 in targets and "staff" in targets
    cbs = [b.callback_data for r in sent[0][2].inline_keyboard for b in r]
    assert cbs == [f"orph:ref:{t}", f"orph:ok:{t}"] and "172,500" in sent[0][1]
    assert await OP.scan_once() == 0                                          # دومین بار اعلام نمی‌شود


async def test_refund_is_atomic_one_time_and_validates_the_transaction(env):
    t = await _txn(env, 30)
    st, tg, amt = await OP.refund_txn(7, t, 1)
    assert (st, tg, amt) == ("ok", 500, 172500)
    st2, _tg, _a = await OP.refund_txn(7, t, 1)                               # دوباره: عودتِ دوم ممنوع
    assert st2 == "done"
    async with env() as s:
        u = await s.scalar(select(models.BotUser))
        kinds = [x.kind for x in (await s.scalars(select(models.WalletTxn))).all()]
    assert int(u.balance) == 172500 and kinds.count("refund") == 1
    assert (await OP.refund_txn(7, 99999, 1))[0] == "missing"                  # تراکنشِ ناموجود
    ref_id = [x.id for x in (await _all(env)) if x.kind == "refund"][0]
    assert (await OP.refund_txn(7, ref_id, 1))[0] == "missing"                  # فقط «purchase» منفی عودت می‌شود
    assert (await OP.refund_txn(8, t, 1))[0] == "missing"                       # ربات دیگر


async def _all(mk):
    async with mk() as s:
        return (await s.scalars(select(models.WalletTxn))).all()


async def test_callback_requires_staff_and_refund_then_blocks_a_second_press(env, monkeypatch):
    t = await _txn(env, 30)
    row = types.SimpleNamespace(id=7, owner_id=1)

    async def gbr(bot): return row

    async def staff(bid, tg, owner): return tg == 1
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "is_owner_or_admin", staff)

    class CB:
        def __init__(self, uid, data):
            self.from_user = types.SimpleNamespace(id=uid)
            self.data, self.alerts, self.edits = data, [], []
            outer = self

            class M:
                html_text = "متن"
                async def edit_text(self, text, **k): outer.edits.append(text)
            self.message = M()

        async def answer(self, text=None, show_alert=False, **k): self.alerts.append(text)

    bot = _Bot()
    stranger = CB(999, f"orph:ref:{t}")
    await OP.orph_action(stranger, bot)
    assert stranger.alerts == ["فقط مالک/ادمین."] and int((await _bal(env))) == 0       # غریبه نمی‌تواند عودت بزند
    ok = CB(1, f"orph:ref:{t}")
    await OP.orph_action(ok, bot)
    assert int(await _bal(env)) == 172500 and any("برگشت" in e for e in ok.edits)
    assert bot.sent and bot.sent[0][0] == 500                                  # به مشتری هم اطلاع داده شد
    again = CB(1, f"orph:ref:{t}")
    await OP.orph_action(again, bot)
    assert int(await _bal(env)) == 172500 and "قبلاً رسیدگی" in again.alerts[0]


async def _bal(mk):
    async with mk() as s:
        return (await s.scalar(select(models.BotUser))).balance


def test_wiring_hook_menu_button_and_worker_loop():
    from pathlib import Path
    assert "from tenant.shared.err_log import post as _el_post" in Path("tenant/__init__.py").read_text(encoding="utf-8")
    assert 'callback_data="errch:menu"' in Path("tenant/shared/tx_channel_admin.py").read_text(encoding="utf-8")
    w = Path("worker_tenant.py").read_text(encoding="utf-8")
    assert "_orphan_purchase_loop()" in w
    assert "include_router(orphan_router)" in Path("tenant/__init__.py").read_text(encoding="utf-8")
