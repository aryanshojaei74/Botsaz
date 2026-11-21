"""nitroteeam: «پول کم شد، نه اکانت ساخته شد نه لینک آمد» — هر خطا بعد از کسرِ موجودی (حتی قبل از رسیدن به پنل) باید پول را برگرداند."""
import sys
import types

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import vpn_user as VU

pytestmark = pytest.mark.asyncio


class _Msg:
    def __init__(self):
        self.answers = []
        self.photo = self.video = self.document = self.animation = None

    async def edit_text(self, *a, **k): pass

    async def edit_caption(self, *a, **k): pass

    async def edit_reply_markup(self, *a, **k): pass

    async def answer(self, text, **k): self.answers.append(text)


class _State:
    async def get_data(self): return {}

    async def update_data(self, **k): pass

    async def clear(self): pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for mod in list(sys.modules.values()):                      # هر ماژولی که Session را import کرده
        try:
            if getattr(mod, "Session", None) is orig:
                monkeypatch.setattr(mod, "Session", mk)
        except Exception:
            pass
    getattr(tenant, "_SETTING_CACHE", {}).clear()
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=500, full_name="u", username="u", balance=100_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p.test", username="a", password="b", enabled=True))
        s.add(models.VpnPlan(id=9, bot_id=7, panel_key="marzban", title="۱۰ گیگ", volume_gb=10, days=30, price=40_000, active=True))
        await s.commit()
    yield mk
    await eng.dispose()


async def _balance(mk):
    async with mk() as s:
        return int((await s.scalar(select(models.BotUser))).balance)


async def test_failure_before_panel_call_refunds_and_tells_the_customer(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("srvname cfg exploded")               # قبلاً بیرونِ try بود ⇒ بدونِ عودت و بدونِ پیام
    monkeypatch.setattr(VU, "_make_config_email", boom)

    async def noop(*a, **k): return None
    monkeypatch.setattr(VU, "_progress_edit", noop)
    import tenant as T
    monkeypatch.setattr(T, "report_user_error", noop, raising=False)
    msg = _Msg()
    cb = types.SimpleNamespace(from_user=types.SimpleNamespace(id=500, username="u", full_name="u"), message=msg,
                               data="vpay:9", answer=noop, bot=None)
    bot = types.SimpleNamespace(token="7:T")
    await VU._vpn_pay_impl(cb, bot, _State(), 9, None, None, 0)
    assert await _balance(env) == 100_000                        # پول برگشت (۴۰هزار کم شده بود)
    assert any("بازگشت" in t for t in msg.answers)               # به مشتری گفته شد
    async with env() as s:
        kinds = [t.kind for t in (await s.scalars(select(models.WalletTxn))).all()]
    assert kinds == ["purchase", "refund"]


async def test_service_restart_cancellation_mid_build_refunds_the_customer(env, monkeypatch):
    """Nitro_Skybot ۲۲:۴۴: ریستارتِ سرویس وسطِ ساخت ⇒ CancelledError از `except Exception` رد می‌شد ⇒ پول رفته، اکانت و عودتی نبود."""
    import asyncio

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(VU, "_make_config_email", cancelled)

    async def noop(*a, **k): return None
    monkeypatch.setattr(VU, "_progress_edit", noop)
    msg = _Msg()
    cb = types.SimpleNamespace(from_user=types.SimpleNamespace(id=500, username="u", full_name="u"), message=msg,
                               data="vpay:9", answer=noop, bot=None)
    with pytest.raises(asyncio.CancelledError):          # کنسل‌شدن باید بالا برود (خاموشیِ تمیزِ سرویس)
        await VU._vpn_pay_impl(cb, types.SimpleNamespace(token="7:T"), _State(), 9, None, None, 0)
    assert await _balance(env) == 100_000                # ولی پول برگشت
    async with env() as s:
        txns = (await s.scalars(select(models.WalletTxn))).all()
    assert [t.kind for t in txns] == ["purchase", "refund"] and "توقف" in txns[1].note


def test_custom_purchase_path_handles_cancel_and_missing_panel_in_source():
    from pathlib import Path
    src = Path("tenant/bots/vpn/vpn_user.py").read_text(encoding="utf-8")
    assert src.count("PATCH_PURCHASE_REFUND_ON_CANCEL_V1") >= 3
    i = src.index("[VC_PAY] هیچ پنل فعالی پیدا نشد")
    seg = src[i:i + 1700]
    assert 'kind="refund"' in seg and "return" in seg                   # پنل نبود ⇒ اول عودت، بعد return
