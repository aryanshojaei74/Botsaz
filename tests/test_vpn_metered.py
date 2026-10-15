"""PATCH_METERED_PLAN_V1 — بستهٔ اعتباری: پرداخت به‌ازایِ مصرف از کیف پول."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import vpn_metered as M

GIB = 1024 ** 3


def test_pure_math():
    assert M.affordable_gb(10_000, 2000) == 5 and M.affordable_gb(1999, 2000) == 0 and M.affordable_gb(0, 2000) == 0
    assert M.charge_for(GIB, 2000) == 2000 and M.charge_for(GIB // 2, 2000) == 1000 and M.charge_for(0, 2000) == 0
    assert M.charge_for(100, 2000) == 0                     # مصرفِ ناچیز جمع می‌شود، نه گرد به ۱ تومان
    assert M.bytes_for_charge(2000, 2000) == GIB
    # سقفِ پنل ۵گیگ، مصرف ۱گیگ، موجودی ۸٬۰۰۰ (۴ گیگ): headroom=۴گیگ رزرو است ⇒ چیزی اضافه نمی‌شود
    assert M.plan_topup(8000, 2000, GIB, 5 * GIB) == 0
    # شارژ تا ۱۴٬۰۰۰ (۷ گیگ) ⇒ ۳ گیگِ دیگر
    assert M.plan_topup(14000, 2000, GIB, 5 * GIB) == 3
    assert M.plan_topup(14000, 2000, 0, 0) == 0


def test_feature_is_pro_only_and_vpn_only():
    from plans import Feature, has_feature
    assert has_feature("pro", Feature.METERED_PLAN) and not has_feature("growth", Feature.METERED_PLAN)
    assert not has_feature("free", Feature.METERED_PLAN)


class FakeClient:
    def __init__(self, used=0, cap=5 * GIB):
        self.used, self.cap, self.enabled, self.added, self.toggles = used, cap, True, 0, []

    async def get_usage(self, email):
        return {"up": 0, "down": self.used, "total": self.cap, "enable": self.enabled}

    async def add_traffic(self, email, gb):
        self.cap += int(gb) * GIB
        self.added += int(gb)

    async def set_enabled(self, email, v):
        self.enabled = v
        self.toggles.append(v)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, M):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", balance=10_000))
        await s.flush()
        s.add(models.PanelAccount(id=5, bot_id=9, user_telegram_id=1001, email="e1", panel_key="x", volume_gb=5,
                                  days=30, status="active", expires_at=dt.datetime.utcnow() + dt.timedelta(days=30)))
        s.add(models.MeteredAccount(id=1, bot_id=9, account_id=5, user_id=1, price_per_gb=2000))
        await s.commit()
    yield maker
    await engine.dispose()


async def _state(db):
    async with db() as s:
        u = await s.get(models.BotUser, 1)
        m = await s.get(models.MeteredAccount, 1)
        a = await s.get(models.PanelAccount, 5)
        txns = (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.kind == "metered"))).all()
    return u, m, a, txns


class Notes:
    def __init__(self):
        self.msgs = []

    async def __call__(self, tg, text):
        self.msgs.append((tg, text))


@pytest.mark.asyncio
async def test_usage_is_charged_from_wallet_and_logged(db):
    c, n = FakeClient(used=GIB + GIB // 2), Notes()           # ۱٫۵ گیگ مصرف
    assert await M.process_one(1, c, n) == "ok"
    u, m, a, txns = await _state(db)
    assert float(u.balance) == 7000 and m.billed_toman == 3000 and m.last_used_bytes == GIB + GIB // 2
    assert len(txns) == 1 and float(txns[0].amount) == -3000
    assert await M.process_one(1, c, n) == "ok"               # دورِ تکراری بدونِ مصرفِ جدید ⇒ کسرِ دوباره ندارد
    u2, *_ = await _state(db)
    assert float(u2.balance) == 7000


@pytest.mark.asyncio
async def test_cap_never_exceeds_wallet_and_service_cut_when_money_ends(db):
    c, n = FakeClient(used=5 * GIB - 1024), Notes()           # تقریباً همهٔ ۵ گیگ مصرف شد
    assert await M.process_one(1, c, n) == "no_credit"
    u, m, a, _ = await _state(db)
    assert float(u.balance) <= 1 and m.status == "no_credit" and a.status == "disabled"
    assert c.toggles == [False] and n.msgs and "تمام شد" in n.msgs[0][1]
    assert await M.process_one(1, c, n) == "ok"               # پیامِ دوباره/قطعِ دوباره نیست
    assert c.toggles == [False] and len(n.msgs) == 1


@pytest.mark.asyncio
async def test_recharge_adds_whole_gb_and_reactivates(db):
    c, n = FakeClient(used=5 * GIB - 1024), Notes()
    await M.process_one(1, c, n)
    async with db() as s:                                    # کاربر ۷٬۰۰۰ تومان شارژ می‌کند ⇒ ۳ گیگ
        u = await s.get(models.BotUser, 1)
        u.balance = float(u.balance) + 7000
        await s.commit()
    assert await M.process_one(1, c, n) == "topup_revived"
    u, m, a, _ = await _state(db)
    assert c.added == 3 and c.enabled is True and c.toggles == [False, True]
    assert m.status == "active" and a.status == "active" and a.volume_gb == 8
    assert any("دوباره فعال" in t for _, t in n.msgs)
    # بعد از احیا، مصرف تا سقفِ جدید از همان موجودی پوشش داده می‌شود
    assert await M.process_one(1, c, n) == "ok"


@pytest.mark.asyncio
async def test_small_recharge_below_one_gb_does_not_reactivate(db):
    c, n = FakeClient(used=5 * GIB - 1024), Notes()
    await M.process_one(1, c, n)
    async with db() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = float(u.balance) + 1000                  # کمتر از قیمتِ یک گیگ
        await s.commit()
    assert await M.process_one(1, c, n) == "ok"
    _, m, _, _ = await _state(db)
    assert c.added == 0 and m.status == "no_credit" and c.enabled is False


@pytest.mark.asyncio
async def test_balance_never_goes_negative_when_overrun(db):
    async with db() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = 1000
        await s.commit()
    c, n = FakeClient(used=3 * GIB, cap=0), Notes()          # مصرفِ بیشتر از موجودی (مثلاً سقفِ پنل نبوده)
    await M.process_one(1, c, n)
    u, m, _, _ = await _state(db)
    assert float(u.balance) == 0 and m.billed_toman == 1000


@pytest.mark.asyncio
async def test_panel_counter_reset_does_not_charge_or_break(db):
    async with db() as s:
        m = await s.get(models.MeteredAccount, 1)
        m.last_used_bytes = 3 * GIB
        await s.commit()
    c, n = FakeClient(used=GIB // 4), Notes()
    await M.process_one(1, c, n)
    u, m, _, _ = await _state(db)
    assert float(u.balance) == 10_000 and m.last_used_bytes == GIB // 4


@pytest.mark.asyncio
async def test_user_paused_service_is_not_touched_and_expired_or_deleted_end(db):
    async with db() as s:
        a = await s.get(models.PanelAccount, 5)
        a.status = "disabled"                                # کاربر خودش متوقف کرده
        await s.commit()
    c, n = FakeClient(used=5 * GIB - 1024), Notes()
    assert await M.process_one(1, c, n) == "ok"              # no_credit نمی‌شود، دوباره قطع هم نمی‌کند
    assert c.toggles == []
    async with db() as s:
        a = await s.get(models.PanelAccount, 5)
        a.expires_at = dt.datetime.utcnow() - dt.timedelta(days=1)
        await s.commit()
    assert await M.process_one(1, c, n) == "expired"
    async with db() as s:
        await s.delete(await s.get(models.PanelAccount, 5))
        m = await s.get(models.MeteredAccount, 1)
        m.status = "active"
        await s.commit()
    assert await M.process_one(1, c, n) == "ended"           # سرویس حذف شده ⇒ بایگانی


@pytest.mark.asyncio
async def test_config_defaults_and_entry_row(db):
    cfg = await M.get_cfg(9)
    assert cfg["enabled"] is False and cfg["price"] == 2000 and cfg["days"] == 30 and cfg["panel_key"] == ""
    assert cfg["plans"] == [{"id": 0, "title": "بستهٔ ویژه", "price": 2000, "days": 30, "max_gb": 0}] and not cfg["custom_plans"]
    assert await M.user_entry_row(9) is None
    await tenant.set_setting(9, "mtr_enabled", "1")
    await tenant.set_setting(9, "mtr_price_gb", "3500")
    tenant._SETTING_CACHE.clear()
    row = await M.user_entry_row(9)
    assert row and "3,500" in row[0].text and row[0].callback_data == "mtr:open"


class _Msg:
    def __init__(self):
        self.out = []

    async def edit_text(self, text, **k):
        self.out.append(text)


def _cb(answers):
    from types import SimpleNamespace

    async def ans(text=None, show_alert=False, **k):
        answers.append(text)
    return SimpleNamespace(data="mtr:go", from_user=SimpleNamespace(id=1001, username="ali"), message=_Msg(), answer=ans)


@pytest.fixture
def create_env(monkeypatch, db):
    from types import SimpleNamespace
    from tenant.bots.vpn import vpn_user as VU
    import tenant.bots.vpn.vpn_admin as VA
    made = []

    class _C:
        async def create_account(self, email, gb, days, **k):
            made.append((email, gb, days))
            return {"uuid": "u", "sub_id": "s", "sub_link": "https://x/s", "expiry_ms": int((dt.datetime.utcnow().timestamp() + 86400 * days) * 1000)}

    async def panel(_b):
        return SimpleNamespace(panel_key="x")

    async def email(*a, **k):
        return "mail1"

    async def deliver(*a, **k):
        return None

    async def err(*a, **k):
        return None
    monkeypatch.setattr(VA, "get_any_enabled_panel", panel)
    monkeypatch.setattr(VU, "_client_from_panel", lambda p: _C())
    monkeypatch.setattr(VU, "_make_config_email", email)
    monkeypatch.setattr(VU, "_send_config_delivery", deliver)

    async def code(s, b):
        return "123456789"
    monkeypatch.setattr(VU, "new_svc_code", code)
    return made


@pytest.mark.asyncio
async def test_activation_creates_service_with_affordable_gb_and_charges_nothing(db, create_env):
    from types import SimpleNamespace
    async with db() as s:                       # حسابِ قبلیِ فیکسچر را کنار بگذار
        m = await s.get(models.MeteredAccount, 1)
        m.status = "ended"
        await s.commit()
    await tenant.set_setting(9, "mtr_enabled", "1")
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    sent = []

    class B(SimpleNamespace):
        token = "9:T"

        async def send_message(self, *a, **k):
            sent.append(a)
    answers = []
    await M.mtr_go(_cb(answers), B())
    assert create_env == [("mail1", 5, 30)]
    async with db() as s:
        rows = (await s.scalars(select(models.MeteredAccount).where(models.MeteredAccount.status == "active"))).all()
        u = await s.get(models.BotUser, 1)
    assert len(rows) == 1 and rows[0].price_per_gb == 2000 and float(u.balance) == 10_000
    answers.clear()
    await M.mtr_go(_cb(answers), B())            # بارِ دوم: حسابِ فعال دارد
    assert len(create_env) == 1 and any("فعال داری" in (a or "") for a in answers)


@pytest.mark.asyncio
async def test_activation_blocked_when_balance_below_one_gb(db, create_env):
    from types import SimpleNamespace
    async with db() as s:
        m = await s.get(models.MeteredAccount, 1)
        m.status = "ended"
        u = await s.get(models.BotUser, 1)
        u.balance = 1500
        await s.commit()
    await tenant.set_setting(9, "mtr_enabled", "1")
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    answers = []
    await M.mtr_go(_cb(answers), SimpleNamespace(token="9:T"))
    assert create_env == [] and any("موجودی" in (a or "") for a in answers)


@pytest.mark.asyncio
async def test_chosen_panel_is_used_and_falls_back_when_disabled(db, create_env, monkeypatch):
    from types import SimpleNamespace
    import tenant.bots.vpn.vpn_admin as VA
    from tenant.bots.vpn import vpn_user as VU
    used = []

    class _B(SimpleNamespace):
        token = "9:T"

        async def send_message(self, *a, **k):
            return None

    async def get_panel(bot_id, key):
        used.append(key)
        return SimpleNamespace(panel_key=key, enabled=(key == "pasargad"))
    monkeypatch.setattr(VA, "get_panel", get_panel)
    seen = []

    async def email(bot_id, panel_key, *a, **k):
        seen.append(panel_key)
        return "mail1"
    monkeypatch.setattr(VU, "_make_config_email", email)
    async with db() as s:
        m = await s.get(models.MeteredAccount, 1)
        m.status = "ended"
        await s.commit()
    await tenant.set_setting(9, "mtr_enabled", "1")
    await tenant.set_setting(9, "mtr_panel_key", "pasargad")
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    await M.mtr_go(_cb([]), _B())
    assert seen == ["pasargad"]
    async with db() as s:                                    # حالا پنلِ انتخابی غیرفعال است ⇒ اولین پنلِ فعال (x)
        for m in (await s.scalars(select(models.MeteredAccount))).all():
            m.status = "ended"
        await s.commit()
    await tenant.set_setting(9, "mtr_panel_key", "marzban")
    tenant._SETTING_CACHE.clear()
    await M.mtr_go(_cb([]), _B())
    assert seen[-1] == "x"


def test_button_action_registered_and_gated_in_picker():
    fields = tenant._SIMPLE_ACTIONS["buy_metered"][2]
    assert fields == frozenset({"vpn_sales"})
    from types import SimpleNamespace
    mb = SimpleNamespace(id=7)

    def datas(plan, field):
        kb = tenant._thb_kb_action_pick(mb, bot_field=field, plan=plan, category="sell")
        return [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "thb:setsimple:buy_metered:7" in datas("pro", "vpn_sales")
    assert "thb:setsimple:buy_metered:7" not in datas("growth", "vpn_sales")
    assert "thb:setsimple:buy_metered:7" not in datas("pro", "virtual_service")
    kb_tool = tenant._thb_kb_action_pick(mb, bot_field="vpn_sales", plan="pro", category="tool")
    assert all("buy_metered" not in b.callback_data for r in kb_tool.inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_button_action_shows_page_and_respects_toggle_and_plan(db):
    from types import SimpleNamespace
    sent = []

    class Msg:
        from_user = SimpleNamespace(id=1001)

        async def answer(self, text, reply_markup=None, **k):
            sent.append((text, reply_markup))
    async with db() as s:
        row = await s.get(models.Bot, 9)
    await M.show_for_message(Msg(), None, row, header="سرتیتر\n\n")
    assert "فعال نیست" in sent[-1][0] and sent[-1][1] is None            # خاموش
    await tenant.set_setting(9, "mtr_enabled", "1")
    tenant._SETTING_CACHE.clear()
    await M.show_for_message(Msg(), None, row, header="سرتیتر\n\n")
    txt, kb = sent[-1]
    assert txt.startswith("سرتیتر") and "2,000" in txt
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "mtr:mine" in datas and "w:back" in datas                      # فیکسچر یک بستهٔ فعال دارد
    async with db() as s:
        (await s.get(models.MeteredAccount, 1)).status = "ended"
        await s.commit()
    await M.show_for_message(Msg(), None, row)
    assert "mtr:go:0" in [b.callback_data for r in sent[-1][1].inline_keyboard for b in r]
    row.plan = "growth"                                                    # پلن پایین آمد
    await M.show_for_message(Msg(), None, row)
    assert "در دسترس نیست" in sent[-1][0] and sent[-1][1] is None


# ───────────── چند پلن + سقفِ حجم (پیشنهادِ مشتری) ─────────────
import json as _json


@pytest.mark.asyncio
async def test_multi_plans_list_pick_and_activation_uses_chosen_plan(db, create_env):
    from types import SimpleNamespace
    plans = [{"id": 1, "title": "اقتصادی", "price": 1500, "days": 15, "max_gb": 0},
             {"id": 2, "title": "طلایی", "price": 3000, "days": 60, "max_gb": 3}]
    await tenant.set_setting(9, "mtr_enabled", "1")
    await tenant.set_setting(9, "mtr_plans", _json.dumps(plans, ensure_ascii=False))
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    async with db() as s:
        (await s.get(models.MeteredAccount, 1)).status = "ended"
        await s.commit()
        row = await s.get(models.Bot, 9)
    r = await M.user_entry_row(9)
    assert "بسته‌هایِ اعتباری" in r[0].text
    txt, kb = await M._open_view(row, 1001)
    datas = [b.callback_data for rw in kb.inline_keyboard for b in rw]
    assert "mtr:pick:1" in datas and "mtr:pick:2" in datas and "mtr:go:1" not in datas
    txt2, kb2 = await M._open_view(row, 1001, pid="2")
    assert "طلایی" in txt2 and "3,000" in txt2 and "3 گیگ" in txt2
    d2 = [b.callback_data for rw in kb2.inline_keyboard for b in rw]
    assert "mtr:go:2" in d2 and "mtr:open" in d2
    answers = []
    cb = _cb(answers)
    cb.data = "mtr:go:2"

    class B(SimpleNamespace):
        token = "9:T"

        async def send_message(self, *a, **k):
            return None
    await M.mtr_go(cb, B())
    assert create_env == [("mail1", 3, 60)]
    async with db() as s:
        m = (await s.scalars(select(models.MeteredAccount).where(models.MeteredAccount.status == "active"))).one()
    assert m.price_per_gb == 3000 and m.max_gb == 3


@pytest.mark.asyncio
async def test_plan_max_gb_caps_topups_and_ends_package(db):
    async with db() as s:
        m = await s.get(models.MeteredAccount, 1)
        m.max_gb = 5
        u = await s.get(models.BotUser, 1)
        u.balance = 100_000
        await s.commit()
    c, n = FakeClient(used=GIB, cap=5 * GIB), Notes()
    assert await M.process_one(1, c, n) == "ok" and c.added == 0
    c2 = FakeClient(used=5 * GIB - 1024, cap=5 * GIB)
    assert await M.process_one(1, c2, n) == "plan_done"
    _, m, a, _ = await _state(db)
    assert m.status == "ended" and c2.toggles == [False] and any("پایان یافت" in t for _, t in n.msgs)


def test_norm_plans_drops_garbage():
    raw = _json.dumps([{"id": 1, "title": "a", "price": 10, "days": 5}, {"id": 1, "title": "dup", "price": 1, "days": 1},
                       {"id": 2, "title": "bad", "price": 0, "days": 5}, {"id": "x"}, "junk"])
    assert [p["id"] for p in M._norm_plans(raw)] == [1]
    assert M._norm_plans("not json") == [] and M._norm_plans(None) == []
