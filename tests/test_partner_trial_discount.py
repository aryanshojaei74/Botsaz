"""PATCH_PARTNER_TRIAL_PER_TYPE_V1 + PATCH_PARTNER_DISCOUNT_CODE_V1 — تیکتِ Nexivo: تست برایِ هر نوعِ ربات + کدِ تخفیف در ربات همکار."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.gateways import discount_engine as DE
from tenant.partner_bot import partner_sales as ps

PBID, UID = 5, 77


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import plan_trials
    for mod in (config, ps, DE, plan_trials):
        monkeypatch.setattr(mod, "Session", mk)
    async with eng.begin() as c:
        await c.run_sync(plan_trials.PlanTrialRequest.__table__.create, checkfirst=True)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=10, token="1:P", username="partnerbot", field="partner", plan="partner", status="active"))
        for i, f in ((21, "giveaway"), (22, "escrow_bot"), (23, "vpn_sales")):
            s.add(models.Bot(id=i, owner_id=UID, token=f"{i}:T", username=f"b{i}", field=f, plan="growth", status="active"))
        await s.flush()
        s.add(models.PartnerBot(id=PBID, bot_id=1, owner_id=10, status="active", stock_growth=3, stock_pro=3, trial_enabled=True))
        s.add(models.BotUser(bot_id=1, telegram_id=UID, balance=100000))
        s.add(models.Setting(bot_id=1, key="sale_price_growth", value="50000"))
        s.add(models.DiscountCode(bot_id=1, code="OFF20", percent=20, disc_type="percent", is_active=True))
        await s.commit()

    async def partner_of(bot):
        async with mk() as s:
            return await s.get(models.Bot, 1), await s.get(models.PartnerBot, PBID)
    monkeypatch.setattr(ps, "_partner_of", partner_of)
    import tenant
    tenant._SETTING_CACHE.clear()
    monkeypatch.setattr(tenant, "Session", mk)
    yield mk
    await eng.dispose()


class St:
    def __init__(self, **d): self.d, self.state = dict(d), None
    async def get_data(self): return dict(self.d)
    async def update_data(self, **k): self.d.update(k)
    async def set_state(self, s=None): self.state = s
    async def clear(self): self.d.clear()


class Cb:
    def __init__(self, data):
        self.data, self.from_user, self.alerts, self.edits = data, NS(id=UID), [], []
        async def edit_text(text, reply_markup=None, **k): self.edits.append((text, reply_markup))
        self.message = NS(edit_text=edit_text)
    async def answer(self, text=None, show_alert=False): self.alerts.append(text)


class Msg:
    def __init__(self, text):
        self.text, self.from_user, self.out = text, NS(id=UID), []
        async def answer(t, reply_markup=None, **k): self.out.append((t, reply_markup))
        self.answer = answer


def cbs(mk):
    return [b.callback_data for r in mk.inline_keyboard for b in r]


# ═══════════ تست برای هر نوع ═══════════

@pytest.mark.asyncio
async def test_trial_is_per_bot_type(env):
    mk = env
    assert not await ps.trial_used_for_field(UID, "giveaway")
    async with mk() as s:
        s.add(models.PartnerSale(partner_bot_id=PBID, sold_bot_id=21, buyer_telegram_id=UID, plan="growth", price=0, kind="trial"))
        await s.commit()
    assert await ps.trial_used_for_field(UID, "giveaway")                   # همین نوع گرفته شده
    assert not await ps.trial_used_for_field(UID, "escrow_bot")             # نوعِ دیگر آزاد است
    assert not await ps.trial_used_for_field(UID, "panel_seller")
    assert not await ps.trial_used_for_field(999, "giveaway")               # کاربرِ دیگر
    async with mk() as s:                                                    # تستِ پلنِ بات‌سازِ اصلی هم فقط برایِ نوعِ خودش
        from plan_trials import PlanTrialRequest
        s.add(PlanTrialRequest(user_telegram_id=UID, plan="pro", status="used", bot_id=23))
        await s.commit()
    assert await ps.trial_used_for_field(UID, "vpn_sales") and not await ps.trial_used_for_field(UID, "escrow_bot")


@pytest.mark.asyncio
async def test_trial_button_blocks_same_type_only(env):
    mk = env
    async with mk() as s:
        s.add(models.PartnerSale(partner_bot_id=PBID, sold_bot_id=21, buyer_telegram_id=UID, plan="growth", price=0, kind="trial"))
        await s.commit()
    c = Cb("ps:pick:trial")
    await ps.ps_pick(c, NS(), St(ps_field="giveaway"))
    assert c.alerts and "این نوعِ ربات" in c.alerts[0] and not c.edits
    c = Cb("ps:pick:trial")
    await ps.ps_pick(c, NS(), St(ps_field="escrow_bot"))
    assert not [a for a in c.alerts if a] and c.edits and "ps:trialplan:growth" in cbs(c.edits[0][1])


# ═══════════ کد تخفیف ═══════════

@pytest.mark.asyncio
async def test_code_entry_and_price_screen(env):
    st = St(ps_field="giveaway")
    m = Msg("off20")
    await ps.ps_code_in(m, NS(), St(ps_code_kind="growth"))
    assert "اعمال شد" in m.out[0][0] and "10,000" in m.out[0][0] and "40,000" in m.out[0][0]
    bad = Msg("NOPE")
    await ps.ps_code_in(bad, NS(), St(ps_code_kind="growth"))
    assert "نامعتبر" in bad.out[0][0]
    st = St(ps_code="OFF20")
    c = Cb("ps:pick:growth")
    await ps.ps_pick(c, NS(), st)
    assert st.d["ps_price"] == 40000 and st.d["ps_base_price"] == 50000 and st.d["ps_discount"] == 10000 and st.d["ps_code_id"]
    assert "ps:code:growth" in cbs(c.edits[0][1]) and "OFF20" in c.edits[0][0]


@pytest.mark.asyncio
async def test_wallet_pays_discounted_price_and_records_usage_once(env):
    mk = env
    st = St(ps_plan="growth", ps_price=40000, ps_base_price=50000, ps_discount=10000, ps_code="OFF20", ps_code_id=1)
    await ps.ps_pay_wallet(Cb("ps:pay:wallet"), NS(), st)
    async with mk() as s:
        bu = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == UID))
        dc = await s.get(models.DiscountCode, 1)
        o = await s.scalar(select(models.PartnerCardOrder))
        use = (await s.scalars(select(models.DiscountUsage))).all()
    assert int(bu.balance) == 60000 and dc.used_count == 1 and len(use) == 1 and use[0].saved_amount == 10000
    assert int(o.price) == 40000 and o.discount_code_id == 1
    # کدِ یک‌بارمصرف: دومین خرید با همان کد رد می‌شود و پولی کم نمی‌شود
    c = Cb("ps:pay:wallet")
    await ps.ps_pay_wallet(c, NS(), St(ps_plan="pro", ps_price=40000, ps_base_price=50000, ps_discount=10000, ps_code="OFF20", ps_code_id=1))
    assert c.alerts and "دیگر معتبر نیست" in c.alerts[0]
    async with mk() as s:
        assert int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == UID))).balance) == 60000


@pytest.mark.asyncio
async def test_card_order_records_usage_only_on_approval(env):
    mk = env
    async with mk() as s:
        for oid in (31, 32):
            s.add(models.PartnerCardOrder(id=oid, partner_bot_id=PBID, buyer_telegram_id=UID, plan="growth", price=40000,
                                          status="pending", discount_code_id=1, discount_amount=10000))
        await s.commit()
    bot = NS(send_message=lambda *a, **k: _noop())

    async def _noop():
        return None
    owner_cb = Cb("psord:32:n")
    owner_cb.from_user = NS(id=10)
    owner_cb.message = NS(edit_caption=_noop2, edit_text=_noop2, answer=_noop2)
    await ps.ps_order_decide(owner_cb, bot)
    async with mk() as s:
        assert (await s.get(models.DiscountCode, 1)).used_count == 0          # رد ⇒ مصرف ثبت نمی‌شود
    ok_cb = Cb("psord:31:y")
    ok_cb.from_user = NS(id=10)
    ok_cb.message = NS(edit_caption=_noop2, edit_text=_noop2, answer=_noop2)
    await ps.ps_order_decide(ok_cb, bot)
    async with mk() as s:
        assert (await s.get(models.DiscountCode, 1)).used_count == 1


async def _noop2(*a, **k):
    return None


def test_partner_plan_has_discount_codes_and_menu_button():
    import inspect
    from plans import Feature, has_feature
    from tenant.partner_bot import partner_menu
    assert has_feature("partner", Feature.DISCOUNT_CODES)
    assert "tadm:discounts" in inspect.getsource(partner_menu)
