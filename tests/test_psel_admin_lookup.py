"""PATCH_PSEL_ADMIN_LOOKUP_V1 — جستجو و مدیریتِ یک پنل توسطِ مدیر (حجم ±، بدونِ کسرِ کیف‌پول)."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, SV, tenant):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    FakeAPI.log, FakeAPI.fail_create, FakeAPI.new_style = [], False, True
    async with m() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.Bot(id=8, owner_id=501, token="8:T", username="other", field="panel_seller", plan="pro", status="active"))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p", username="s", password="x", enabled=True))
        s.add(models.PanelSellerAccount(id=11, bot_id=7, buyer_tg=1001, panel_id=3, title="t", admin_username="Shop_Ali",
                                        data_gb=100, max_users=10, status="active", tracking_code="TRK1",
                                        expires_at=datetime.utcnow() + timedelta(days=10)))
        s.add(models.PanelSellerAccount(id=12, bot_id=7, buyer_tg=1001, panel_id=3, title="u", admin_username="unl",
                                        data_gb=0, max_users=0, status="active"))
        s.add(models.PanelSellerAccount(id=13, bot_id=8, buyer_tg=1001, panel_id=3, title="o", admin_username="shop_ali",
                                        data_gb=10, max_users=1, status="active"))
        await s.commit()
    yield m
    FakeAPI.new_style = False
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_by_username_code_buyer_is_case_insensitive_and_bot_scoped(env):
    assert [a.id for a in await SV.find_accounts(7, "shop_ali")] == [11]          # حروف مهم نیست و ربات دیگر نمی‌آید
    assert [a.id for a in await SV.find_accounts(7, "@SHOP_ALI")] == [11]
    assert [a.id for a in await SV.find_accounts(7, "trk1")] == [11]
    assert [a.id for a in await SV.find_accounts(7, "1001")] == [12, 11]          # آیدیِ خریدار ⇒ همهٔ پنل‌هایش
    assert await SV.find_accounts(7, "nope") == [] and await SV.find_accounts(7, "  ") == []


@pytest.mark.asyncio
async def test_deleted_accounts_are_not_found(env):
    async with env() as s:
        (await s.get(models.PanelSellerAccount, 11)).status = "deleted"
        await s.commit()
    assert await SV.find_accounts(7, "shop_ali") == []


@pytest.mark.asyncio
async def test_admin_adds_and_reduces_volume_without_touching_wallet(env):
    r = await SV.admin_adjust_volume(7, 11, 20, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["new_gb"] == 120 and r["buyer_tg"] == 1001
    assert ("limit", "Shop_Ali", 120) in FakeAPI.log                               # سقفِ بومی هم رفت
    r = await SV.admin_adjust_volume(7, 11, -50, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["new_gb"] == 70
    async with env() as s:
        assert (await s.get(models.PanelSellerAccount, 11)).data_gb == 70
        assert (await s.scalar(select(models.WalletTxn))) is None          # هیچ تراکنشِ کیف‌پولی ثبت نشد


@pytest.mark.asyncio
async def test_reduce_below_one_unlimited_and_other_bot_are_rejected(env):
    assert (await SV.admin_adjust_volume(7, 11, -100, api_factory=FakeAPI))["status"] == "below_min"
    assert (await SV.admin_adjust_volume(7, 12, 5, api_factory=FakeAPI))["status"] == "unlimited"
    assert (await SV.admin_adjust_volume(7, 13, 5, api_factory=FakeAPI))["status"] == "not_found"   # پنلِ ربات دیگر
    assert (await SV.admin_adjust_volume(7, 11, 0, api_factory=FakeAPI))["status"] == "bad_amount"
    async with env() as s:
        assert (await s.get(models.PanelSellerAccount, 11)).data_gb == 100


@pytest.mark.asyncio
async def test_increase_reopens_traffic_suspended_account(env):
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.status, a.suspend_reason = "suspended", "traffic"
        await s.commit()
    r = await SV.admin_adjust_volume(7, 11, 10, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["reopened"] and ("on", "Shop_Ali") in FakeAPI.log
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        assert a.status == "active" and a.suspend_reason is None


@pytest.mark.asyncio
async def test_panel_failure_changes_nothing(env):
    class Boom(FakeAPI):
        async def apply_data_limit(self, u, gb):
            raise RuntimeError("down")
    r = await SV.admin_adjust_volume(7, 11, 10, api_factory=Boom)
    assert r["status"] == "ok"            # سقفِ بومی بهترین‌تلاش است (apply_native_limit خطا را قورت می‌دهد)
    class Boom2(FakeAPI):
        async def activate_users(self, u):
            raise RuntimeError("down")
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.status, a.suspend_reason = "suspended", "traffic"
        await s.commit()
    r = await SV.admin_adjust_volume(7, 11, 10, api_factory=Boom2)
    assert r["status"] == "panel_failed"
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        assert a.status == "suspended" and a.data_gb == 110     # قبلاً بالا رفته بود (تست قبلی)؛ این‌بار تغییری نکرد


def test_notice_text_respects_ident_mode_and_never_has_password():
    from types import SimpleNamespace
    b = SimpleNamespace(full_name="Ali <b>", username="ali", telegram_id=42)
    both = SV.test_notice_text(b, "u1", 24, 1, 5, "T1")
    assert "@ali" in both and "<code>42</code>" in both and "Ali &lt;b&gt;" in both
    only_id = SV.test_notice_text(b, "u1", 24, 1, 5, "T1", ident_mode="id")
    assert "@ali" not in only_id and "Ali" not in only_id and "<code>42</code>" in only_id
    only_un = SV.test_notice_text(b, "u1", 24, 1, 5, "T1", ident_mode="username")
    assert "@ali" in only_un and "<code>42</code>" not in only_un
    no_un = SV.test_notice_text(SimpleNamespace(full_name="X", username=None, telegram_id=7), "u1", 1, 1, 0, None, ident_mode="username")
    assert "<code>7</code>" in no_un                                # یوزرنیم نداشت ⇒ آیدی، نه پیامِ بی‌هویت
    assert "رمز" not in both


@pytest.mark.asyncio
async def test_notify_uses_saved_ident_mode(env):
    tenant._SETTING_CACHE.clear()
    from types import SimpleNamespace
    await tenant.set_setting(7, "psel_test_log_channel", "@chan_x1")
    await tenant.set_setting(7, "report_buyer_mode", "id")
    sent = []

    class B:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))
    row = SimpleNamespace(id=7, owner_id=500)
    ok = await SV.notify_test_taken(B(), row, SimpleNamespace(full_name="Ali", username="ali", telegram_id=9), "u", 1, 1, 0, "T")
    assert ok and sent[0][0] == "@chan_x1" and "@ali" not in sent[0][1] and "<code>9</code>" in sent[0][1]


# ───────── بازگشتِ وجه ─────────
@pytest.mark.asyncio
async def test_refund_credits_wallet_deletes_panel_and_cannot_run_twice(env):
    async with env() as s:
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=5_000))
        (await s.get(models.PanelSellerAccount, 11)).price_toman = 40_000
        await s.commit()
    r = await SV.refund_account(7, 11, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["amount"] == 40_000 and r["balance"] == 45_000
    assert ("del", "Shop_Ali") in FakeAPI.log
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        txn = (await s.scalars(select(models.WalletTxn))).all()
    assert a.status == "deleted" and len(txn) == 1 and txn[0].amount == 40_000
    assert (await SV.refund_account(7, 11, api_factory=FakeAPI))["status"] == "not_found"      # دوباره پول نمی‌دهد
    async with env() as s:
        assert (await s.get(models.BotUser, 1)).balance == 45_000


@pytest.mark.asyncio
async def test_refund_panel_failure_returns_no_money(env):
    async with env() as s:
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=0))
        (await s.get(models.PanelSellerAccount, 11)).price_toman = 40_000
        await s.commit()

    class Boom(FakeAPI):
        async def delete_admin(self, u):
            raise RuntimeError("down")
    r = await SV.refund_account(7, 11, api_factory=Boom)
    assert r["status"] == "panel_failed"
    async with env() as s:
        assert (await s.get(models.BotUser, 1)).balance == 0 and (await s.get(models.PanelSellerAccount, 11)).status == "active"


@pytest.mark.asyncio
async def test_refund_rejects_free_other_bot_and_missing_buyer(env):
    assert (await SV.refund_account(7, 12, api_factory=FakeAPI))["status"] == "nothing_to_refund"      # تست/رایگان
    assert (await SV.refund_account(7, 13, api_factory=FakeAPI))["status"] == "not_found"               # ربات دیگر
    async with env() as s:
        (await s.get(models.PanelSellerAccount, 11)).price_toman = 40_000
        await s.commit()
    assert (await SV.refund_account(7, 11, api_factory=FakeAPI))["status"] == "not_found"               # خریدار کیف‌پول ندارد


def test_notes_have_settings_and_are_shown_to_customer():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "tenant" / "bots" / "panel_seller"
    u = (root / "user.py").read_text(encoding="utf-8")
    assert u.count("psel_note_delivery") == 3 and u.count("psel_note_invoice") == 1       # تحویلِ خرید + تحویلِ تست + تأییدِ سفارشِ دستی (PATCH_PSEL_MANUAL_SALE_V1)، و پیش‌فاکتور
    from tenant.bots.panel_seller import admin as A
    assert {v[0] for v in A.NOTE_KEYS.values()} == {"psel_note_invoice", "psel_note_delivery"}
