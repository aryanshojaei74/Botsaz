"""
تست‌هایِ «متنِ مشخصاتِ شما» (PATCH_PROFILE_TEMPLATE_V1).

درخواستِ ادمین (فدایی): متنِ مشخصات را بشود کامل عوض کرد (نه فقط یک متنِ اضافه در پایین) و
متغیرهایِ کارتِ احرازشده، تلفن، سطح/وضعیتِ احراز، زیرمجموعه‌ها، تراکنشِ کل، خریدِ زیرمجموعه‌ها، پاداشِ
دریافتی و ... داشته باشد. تست‌ها show_profile و هندلرهایِ ادمین را واقعی اجرا می‌کنند (SQLiteِ موقت).
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import profile_stats as PS
from tenant.shared import text_vars as TV

OWNER = 1
ALI = 555
SARA = 556


def bad_request(msg="Bad Request: can't parse entities: Unsupported start tag"):
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=msg)


class Msg:
    """پیامی که ربات می‌فرستد؛ می‌توان تعیین کرد کدام ارسال‌ها خطا بدهند."""

    def __init__(self, text="", uid=OWNER, fail_on=()):
        self.text = text
        self.html_text = text
        self.from_user = SimpleNamespace(id=uid, is_bot=False, full_name="Ali", username="ali")
        self.sent: list[tuple[str, dict]] = []
        self._fail_on = set(fail_on)
        self._n = 0

    async def answer(self, text="", **kw):
        i = self._n
        self._n += 1
        if i in self._fail_on:
            raise bad_request()
        self.sent.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        await self.answer(text, **kw)


class Cb:
    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, is_bot=False, full_name="Ali", username="ali")
        self.message = Msg(uid=uid)
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


class State:
    def __init__(self):
        self.cur, self.data = None, {}

    async def clear(self): self.cur, self.data = None, {}
    async def set_state(self, s): self.cur = s
    async def update_data(self, **kw): self.data.update(kw)
    async def get_data(self): return dict(self.data)


class FakeBot:
    token = "123:AAA"


@pytest_asyncio.fixture
async def db(monkeypatch):
    import tenant
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._OWN_TG_ID_CACHE.clear()
    ids = {}
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:AAA", username="shop_bot", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        ali = models.BotUser(bot_id=b.id, telegram_id=ALI, full_name="Ali <b>Rez</b>", username="ali",
                             balance=132155, joined_at=datetime(2026, 9, 28, 20, 40, 22),
                             phone_number="09121234567", kyc_level=1, kyc_card="6037-99**-****-1234")
        sara = models.BotUser(bot_id=b.id, telegram_id=SARA, full_name="Sara", balance=0,
                              joined_at=datetime(2026, 9, 1, 10, 0, 0))
        s.add_all([ali, sara])
        await s.flush()
        s.add_all([
            models.Order(bot_id=b.id, user_id=ali.id, volume_gb=10, days=30, amount=50000, pay_method="wallet", status="paid"),
            models.Order(bot_id=b.id, user_id=ali.id, volume_gb=0, days=0, amount=100000, pay_method="zarinpal_wallet", status="paid"),
            models.Order(bot_id=b.id, user_id=ali.id, volume_gb=5, days=10, amount=9999, pay_method="wallet", status="pending"),
            models.PanelAccount(bot_id=b.id, user_telegram_id=ALI, email="a@x", status="active"),
            models.Config(bot_id=b.id, code="C1", volume_gb=1, days=1, payload="x", status="sold", owner_user_id=ali.id),
        ])
        link = models.ReferralLink(bot_id=b.id, user_id=ali.id, code="REF1", join_count=3)
        s.add(link)
        await s.flush()
        s.add_all([
            models.ReferralJoin(bot_id=b.id, referral_link_id=link.id, inviter_user_id=ali.id, referred_telegram_id=SARA),
            models.Order(bot_id=b.id, user_id=sara.id, volume_gb=10, days=30, amount=70000, pay_method="wallet", status="paid"),
        ])
        for st in ("sent", "completed", "partial", "refunded", "failed", "awaiting_admin"):
            s.add(models.SmmOrder(bot_id=b.id, user_telegram_id=ALI, service_id="1", link="l",
                                  quantity=10, price_toman=100, status=st))
        await s.commit()
        ids = {"bot": b.id, "ali": ali.id, "sara": sara.id}
        row = SimpleNamespace(id=b.id, username="shop_bot", token=b.token, owner_id=OWNER,
                              field="vpn_sales", plan="pro", status="active")

    async def fake_get_bot_row(_bot): return row
    monkeypatch.setattr(tenant, "get_bot_row", fake_get_bot_row)

    async def no_back_kb(_row): return None                    # صفحهٔ «بازگشت» به این تست‌ها ربطی ندارد
    monkeypatch.setattr(tenant, "_profile_footer_back_kb", no_back_kb)

    async def cancel_kb(_bot): return None
    monkeypatch.setattr(tenant, "smart_cancel_kb", cancel_kb)
    yield maker, ids, row
    tenant._SETTING_CACHE.clear()
    await engine.dispose()


async def user(maker, ids, key="ali"):
    async with maker() as s:
        return await s.get(models.BotUser, ids[key])


async def set_field(maker, bot_id, field):
    async with maker() as s:
        b = await s.get(models.Bot, bot_id)
        b.field = field
        await s.commit()


async def render(ids, u, text):
    return await TV.render_text(text, bot_id=ids["bot"], user=u)


# ═════════════════ ۱) آمار ═════════════════

@pytest.mark.asyncio
async def test_stats_for_a_normal_bot_are_the_old_numbers(db):
    maker, ids, _ = db
    st = await PS.compute(ids["bot"], await user(maker, ids), is_virtual_service=False)
    assert st.orders_count == 1                    # فقط سفارشِ حجم‌دارِ پرداخت‌شده
    assert st.total_spent == 150_000               # پرداخت‌شده‌ها (شارژِ کیف پول هم Order است)
    assert st.active_services == 2                 # اکانتِ پنل + کانفیگِ فروخته‌شده
    assert (st.ref_count, st.inv_orders, st.inv_spent) == (3, 1, 70_000)
    assert st.smm_orders == 4                      # sent, completed, partial, awaiting_admin
    assert st.account_active is True


@pytest.mark.asyncio
async def test_stats_for_virtual_service_count_smm_orders(db):
    """باگِ اسکرین‌شات: ۰ سفارش در برابرِ ۱۲ سفارشِ واقعی."""
    maker, ids, _ = db
    st = await PS.compute(ids["bot"], await user(maker, ids), is_virtual_service=True)
    assert st.orders_count == 1 + 4                # فروشگاه + SMMِ ثبت‌شده (نه refunded/failed)
    assert st.active_services == 2 + 2             # + sent و awaiting_admin
    sara = await PS.compute(ids["bot"], await user(maker, ids, "sara"), is_virtual_service=True)
    assert sara.active_services == 0 and sara.account_active is True    # مسدود نیست ⇒ فعال


@pytest.mark.asyncio
async def test_banned_virtual_service_user_is_inactive(db):
    maker, ids, _ = db
    async with maker() as s:
        u = await s.get(models.BotUser, ids["sara"])
        u.is_banned = True
        await s.commit()
    st = await PS.compute(ids["bot"], await user(maker, ids, "sara"), is_virtual_service=True)
    assert st.account_active is False


async def _pay_referral_rewards(maker, ids, u, n=3, amount=5000):
    """پاداشِ رفرال حالا از تراکنش‌هایِ واقعیِ کیف پول (kind=referral) جمع زده می‌شود
    (PATCH_MINIAPP_REF_REWARD_V1)، نه «تعدادِ زیرمجموعه × مبلغِ تنظیم»."""
    async with maker() as s:
        for _ in range(n):
            s.add(models.WalletTxn(bot_id=ids["bot"], user_id=u.id, amount=amount, kind="referral"))
        await s.commit()


@pytest.mark.asyncio
async def test_referral_reward_math(db):
    maker, ids, _ = db
    import tenant
    u = await user(maker, ids)
    assert (await PS.compute(ids["bot"], u)).ref_total_earned == 0                 # هنوز پاداشی واریز نشده
    await tenant.set_setting(ids["bot"], "referral_reward", "5000")
    tenant._SETTING_CACHE.clear()
    assert (await PS.compute(ids["bot"], u)).ref_total_earned == 0                 # تنظیم ≠ واریز
    await _pay_referral_rewards(maker, ids, u)
    assert (await PS.compute(ids["bot"], u)).ref_total_earned == 3 * 5000


@pytest.mark.asyncio
async def test_stats_are_isolated_per_user_and_per_bot(db):
    maker, ids, _ = db
    st = await PS.compute(ids["bot"], await user(maker, ids, "sara"))
    assert (st.orders_count, st.total_spent, st.ref_count, st.smm_orders) == (1, 70_000, 0, 0)
    other = await PS.compute(ids["bot"] + 1, await user(maker, ids))
    assert (other.orders_count, other.total_spent, other.active_services) == (0, 0, 0)


# ═════════════════ ۲) متغیرها ═════════════════

@pytest.mark.asyncio
async def test_new_variables_render_real_values(db):
    maker, ids, _ = db
    u = await user(maker, ids)
    got = await render(ids, u, "|".join("{{%s}}" % n for n in (
        "phone", "kyc_card", "kyc_level", "kyc_level_name", "account_status", "profile_orders",
        "active_services", "total_spent", "ref_count", "ref_orders", "ref_spent", "ref_earned",
        "smm_orders", "join_date", "join_time")))
    assert got.split("|") == ["09121234567", "6037-99**-****-1234", "1", "سطح ۱ (کارت تأییدشده)",
                              "✅ فعال", "1", "2", "150,000", "3", "1", "70,000", "0", "4",
                              got.split("|")[13], got.split("|")[14]]
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", got.split("|")[14])


@pytest.mark.asyncio
async def test_kyc_status_follows_the_bot_setting_and_user_state(db):
    maker, ids, _ = db
    import tenant
    u = await user(maker, ids)
    assert await render(ids, u, "{{kyc_status}}") == "—"                 # احراز خاموش ⇒ «—»
    await tenant.set_setting(ids["bot"], "kyc_enabled", "1")
    tenant._SETTING_CACHE.clear()
    assert await render(ids, u, "{{kyc_status}}") == "✅ احرازشده"          # سطح ۱
    sara = await user(maker, ids, "sara")
    assert await render(ids, sara, "{{kyc_status}}") == "❌ احراز نشده"       # سطح ۰
    async with maker() as s:
        x = await s.get(models.BotUser, ids["ali"])
        x.kyc_blocked = True
        await s.commit()
    assert await render(ids, await user(maker, ids), "{{kyc_status}}") == "⛔ محدود"


@pytest.mark.asyncio
async def test_missing_values_show_a_dash_not_none(db):
    maker, ids, _ = db
    sara = await user(maker, ids, "sara")
    out = await render(ids, sara, "{{phone}}|{{kyc_card}}|{{kyc_level}}|{{kyc_level_name}}")
    assert out == "—|—|0|بدون احراز"
    assert "None" not in out


@pytest.mark.asyncio
async def test_variable_values_are_html_escaped(db):
    maker, ids, _ = db
    async with maker() as s:
        u = await s.get(models.BotUser, ids["ali"])
        u.phone_number = "<b>1</b>"
        u.kyc_card = "5&6"
        await s.commit()
    out = await render(ids, await user(maker, ids), "{{phone}} {{kyc_card}}")
    assert out == "&lt;b&gt;1&lt;/b&gt; 5&amp;6"


@pytest.mark.asyncio
async def test_ref_summary_is_trusted_html_and_matches_stats(db):
    maker, ids, _ = db
    u = await user(maker, ids)
    out = await render(ids, u, "{{ref_summary}}")
    st = await PS.compute(ids["bot"], u)
    assert out == PS.ref_summary(st) and "<i>" in out                      # escape نشده


@pytest.mark.asyncio
async def test_unknown_names_are_left_untouched_and_stats_are_lazy(db, monkeypatch):
    maker, ids, _ = db
    calls = []
    real = PS.compute

    async def spy(*a, **k):
        calls.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(PS, "compute", spy)
    u = await user(maker, ids)
    assert await render(ids, u, "سلام {{user_name}} {{kyc_crad}}") == "سلام Ali {{kyc_crad}}"
    assert calls == []                                    # متنی که آمار نمی‌خواهد آمار حساب نمی‌کند
    await render(ids, u, "{{ref_count}} {{ref_spent}}")
    assert len(calls) == 1                                # چند متغیرِ آماری ⇒ یک‌بار محاسبه


def test_new_variables_are_documented():
    help_text = TV.variables_help_text()
    for n in ("phone", "kyc_card", "kyc_level", "kyc_level_name", "kyc_status", "account_status",
              "profile_orders", "active_services", "total_spent", "ref_count", "ref_orders",
              "ref_spent", "ref_earned", "ref_summary", "join_time", "smm_orders"):
        assert n in TV.VARS and TV.VARS[n][0] == "user"
        assert "{{%s}}" % n in help_text


# ═════════════════ ۳) متنِ پیش‌فرض = قالبِ پیش‌فرض ═════════════════

@pytest.mark.asyncio
async def test_default_template_renders_exactly_like_the_built_in_page(db):
    """قالبی که به ادمین نشان داده می‌شود باید همان صفحهٔ فعلی را بسازد؛ وگرنه «شروعِ آماده» دروغ بود."""
    maker, ids, _ = db
    import tenant
    u = await user(maker, ids)
    m = Msg()
    await tenant.show_profile(m, ids["bot"], u)
    builtin = m.sent[-1][0]
    assert await render(ids, u, PS.DEFAULT_TEMPLATE) == builtin


@pytest.mark.asyncio
async def test_default_template_equals_builtin_with_rewards_too(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "referral_reward", "5000")
    tenant._SETTING_CACHE.clear()
    u = await user(maker, ids)
    await _pay_referral_rewards(maker, ids, u)
    m = Msg()
    await tenant.show_profile(m, ids["bot"], u)
    assert await render(ids, u, PS.DEFAULT_TEMPLATE) == m.sent[-1][0]
    assert "🎁 اعتبار پاداش دریافتی تا به الان: 15,000 تومان" in m.sent[-1][0]     # ۳ نفر × ۵٬۰۰۰


def test_default_template_uses_only_known_variables():
    assert TV.find_names(PS.DEFAULT_TEMPLATE) == {
        "user_id", "username", "full_name", "account_status", "profile_orders", "active_services",
        "total_spent", "balance", "ref_summary", "join_date", "join_time"}
    used = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", PS.DEFAULT_TEMPLATE))
    assert used == TV.find_names(PS.DEFAULT_TEMPLATE)


# ═════════════════ ۴) show_profile ═════════════════

@pytest.mark.asyncio
async def test_without_settings_the_page_is_the_default(db):
    maker, ids, _ = db
    import tenant
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    txt = m.sent[-1][0]
    assert txt.startswith("👤 <b>مشخصات شما</b>") and "تعداد سفارشات : 1" in txt
    assert "سرویس‌های فعال : 2" in txt and "تراکنش کل : 150,000 تومان" in txt and "موجودی : 132,155 تومان" in txt


@pytest.mark.asyncio
async def test_custom_template_replaces_the_whole_page(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text",
                             "<b>حساب کاربری شما</b>\nنام: {{full_name}}\nموجودی: {{balance}}\nسطح: {{kyc_level}}")
    tenant._SETTING_CACHE.clear()
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    txt = m.sent[-1][0]
    assert txt == "<b>حساب کاربری شما</b>\nنام: Ali &lt;b&gt;Rez&lt;/b&gt;\nموجودی: 132,155\nسطح: 1"
    assert "مشخصات شما" not in txt and "زیرمجموعه" not in txt


@pytest.mark.asyncio
async def test_footer_still_works_alone_and_after_a_template(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_footer_text", "پایین {{user_name}}")
    tenant._SETTING_CACHE.clear()
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    assert m.sent[-1][0].startswith("👤 <b>مشخصات شما</b>") and m.sent[-1][0].endswith("\n\nپایین Ali")
    await tenant.set_setting(ids["bot"], "profile_template_text", "بالا")
    tenant._SETTING_CACHE.clear()
    m2 = Msg()
    await tenant.show_profile(m2, ids["bot"], await user(maker, ids))
    assert m2.sent[-1][0] == "بالا\n\nپایین Ali"


@pytest.mark.asyncio
async def test_blank_template_means_default(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "   \n ")
    tenant._SETTING_CACHE.clear()
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    assert m.sent[-1][0].startswith("👤 <b>مشخصات شما</b>")


@pytest.mark.asyncio
async def test_broken_template_falls_back_to_the_default_page(db):
    """اگر تلگرام قالب را نپذیرفت، کاربر خطا نمی‌بیند: همان لحظه صفحهٔ پیش‌فرض می‌رود."""
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "<b>باز مانده")
    tenant._SETTING_CACHE.clear()
    m = Msg(fail_on={0})
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    assert len(m.sent) == 1 and m.sent[0][0].startswith("👤 <b>مشخصات شما</b>")


@pytest.mark.asyncio
async def test_broken_footer_falls_back_to_page_without_it(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_footer_text", "<i>باز")
    tenant._SETTING_CACHE.clear()
    m = Msg(fail_on={0})
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    assert len(m.sent) == 1 and "باز" not in m.sent[0][0] and m.sent[0][0].startswith("👤 <b>مشخصات شما</b>")


@pytest.mark.asyncio
async def test_template_and_footer_both_broken_still_reaches_the_user(db):
    maker, ids, _ = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "<b>x")
    await tenant.set_setting(ids["bot"], "profile_footer_text", "<i>y")
    tenant._SETTING_CACHE.clear()
    m = Msg(fail_on={0, 1})                               # دو ارسالِ اول رد می‌شوند
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    assert len(m.sent) == 1 and m.sent[0][0].startswith("👤 <b>مشخصات شما</b>") and "y" not in m.sent[0][0]


@pytest.mark.asyncio
async def test_page_keeps_its_keyboard_in_all_variants(db):
    maker, ids, _ = db
    import tenant
    async with maker() as s:
        u = await s.get(models.BotUser, ids["ali"])
        u.api_access = True
        await s.commit()
    await tenant.set_setting(ids["bot"], "profile_template_text", "قالب")
    tenant._SETTING_CACHE.clear()
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    kb = m.sent[-1][1]["reply_markup"]
    assert any(b.callback_data == "pubapi:key" for r in kb.inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_virtual_service_page_uses_smm_numbers(db):
    maker, ids, _ = db
    import tenant
    await set_field(maker, ids["bot"], "virtual_service")
    m = Msg()
    await tenant.show_profile(m, ids["bot"], await user(maker, ids))
    txt = m.sent[-1][0]
    assert "تعداد سفارشات : 5" in txt and "سرویس‌های فعال : 4" in txt
    m2 = Msg()
    await tenant.show_profile(m2, ids["bot"], await user(maker, ids, "sara"))
    assert "وضعیت اکانت : ✅ فعال" in m2.sent[-1][0]


# ═════════════════ ۵) صفحهٔ مدیریت ═════════════════

def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_admin_menu_reflects_state(db):
    maker, ids, row = db
    import tenant
    cb = Cb("set:profile_footer")
    await tenant.set_profile_footer_start(cb, FakeBot(), State())
    text, kw = cb.message.sent[-1]
    cbs = _cbs(kw["reply_markup"])
    assert {"set:pf:tpl", "set:pf:def", "set:pf:footer", "set:pf:prev", "tvars:list"} <= set(cbs)
    assert not any(c.startswith("set:pf:rst:") for c in cbs)             # چیزی برایِ حذف نیست
    assert "متنِ پیش‌فرضِ" in text
    await tenant.set_setting(ids["bot"], "profile_template_text", "x")
    await tenant.set_setting(ids["bot"], "profile_footer_text", "y")
    tenant._SETTING_CACHE.clear()
    cb2 = Cb("set:profile_footer")
    await tenant.set_profile_footer_start(cb2, FakeBot(), State())
    cbs2 = _cbs(cb2.message.sent[-1][1]["reply_markup"])
    assert {"set:pf:rst:tpl", "set:pf:rst:footer"} <= set(cbs2)
    assert "متنِ کاملِ سفارشی" in cb2.message.sent[-1][0]


@pytest.mark.asyncio
async def test_admin_saves_a_valid_template_after_showing_a_preview(db):
    maker, ids, row = db
    import tenant
    st = State()
    await tenant.set_profile_template_ask(Cb("set:pf:tpl"), FakeBot(), st)
    assert st.cur == tenant.AdminFSM.profile_template
    m = Msg("<b>سلام {{user_name}}</b> موجودی {{balance}}")
    await tenant.set_profile_template_save(m, st, FakeBot())
    assert st.cur is None
    assert any("پیش‌نمایش" in t for t, _ in m.sent) and any("ذخیره شد" in t for t, _ in m.sent)
    assert "Ali" in m.sent[0][0] or "132,155" in m.sent[0][0]
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == "<b>سلام {{user_name}}</b> موجودی {{balance}}"


@pytest.mark.asyncio
async def test_admin_invalid_html_is_not_saved_and_stays_in_the_step(db):
    maker, ids, row = db
    import tenant
    st = State()
    st.cur = tenant.AdminFSM.profile_template
    m = Msg("<b>باز مانده", fail_on={0})                  # ارسالِ پیش‌نمایش رد می‌شود
    await tenant.set_profile_template_save(m, st, FakeBot())
    assert st.cur == tenant.AdminFSM.profile_template
    assert any("ذخیره نشد" in t for t, _ in m.sent)
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == ""


@pytest.mark.asyncio
async def test_admin_too_long_template_is_rejected(db):
    maker, ids, row = db
    import tenant
    st = State()
    st.cur = tenant.AdminFSM.profile_template
    m = Msg("ن" * 3600)
    await tenant.set_profile_template_save(m, st, FakeBot())
    assert st.cur == tenant.AdminFSM.profile_template
    assert any("بیش از حد بلند" in t for t, _ in m.sent)
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == ""


@pytest.mark.asyncio
async def test_admin_gets_a_warning_for_misspelled_variables(db):
    maker, ids, row = db
    import tenant
    st = State()
    st.cur = tenant.AdminFSM.profile_template
    m = Msg("کارت {{kyc_crad}} موجودی {{balance}} {{nothing_here}}")
    await tenant.set_profile_template_save(m, st, FakeBot())
    final = m.sent[-1][0]
    assert "kyc_crad" in final and "nothing_here" in final and "{{balance}}" not in final.split("⚠️")[-1]
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") != ""       # با هشدار ذخیره می‌شود


def test_unknown_variable_detector():
    from tenant import _unknown_profile_vars as f
    assert f("{{balance}} {name} {{kyc_crad}} {x}") == ["kyc_crad", "x"]
    assert f("بدون متغیر") == [] and f("") == []


@pytest.mark.asyncio
async def test_admin_skip_returns_to_the_default(db):
    maker, ids, row = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "قالب")
    st = State()
    st.cur = tenant.AdminFSM.profile_template
    await tenant.set_profile_template_save(Msg("/skip"), st, FakeBot())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == "" and st.cur is None


@pytest.mark.asyncio
async def test_admin_footer_flow_is_unchanged(db):
    maker, ids, row = db
    import tenant
    st = State()
    await tenant.set_profile_footer_ask(Cb("set:pf:footer"), FakeBot(), st)
    assert st.cur == tenant.AdminFSM.profile_footer
    m = Msg("قوانین")
    m.html_text = "<b>قوانین</b>"
    await tenant.set_profile_footer_save(m, st, FakeBot())
    assert await tenant.get_setting(ids["bot"], "profile_footer_text", "") == "<b>قوانین</b>"
    st.cur = tenant.AdminFSM.profile_footer
    await tenant.set_profile_footer_save(Msg("/skip"), st, FakeBot())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(ids["bot"], "profile_footer_text", "") == ""


@pytest.mark.asyncio
async def test_default_text_is_sent_as_plain_text_for_easy_copying(db):
    maker, ids, row = db
    import tenant
    cb = Cb("set:pf:def")
    await tenant.set_profile_default_show(cb, FakeBot())
    assert len(cb.message.sent) == 2
    assert cb.message.sent[1][0] == PS.DEFAULT_TEMPLATE and cb.message.sent[1][1].get("parse_mode") is None


@pytest.mark.asyncio
async def test_admin_preview_shows_the_real_page(db):
    maker, ids, row = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "قالبِ من {{user_id}}")
    tenant._SETTING_CACHE.clear()
    cb = Cb("set:pf:prev")
    await tenant.set_profile_preview(cb, FakeBot())
    assert cb.message.sent[-1][0] == f"قالبِ من {OWNER}"


@pytest.mark.asyncio
async def test_admin_resets_are_independent(db):
    maker, ids, row = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "قالب")
    await tenant.set_setting(ids["bot"], "profile_footer_text", "پایین")
    await tenant.set_profile_reset(Cb("set:pf:rst:tpl"), FakeBot())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == ""
    assert await tenant.get_setting(ids["bot"], "profile_footer_text", "") == "پایین"
    await tenant.set_profile_reset(Cb("set:pf:rst:footer"), FakeBot())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(ids["bot"], "profile_footer_text", "") == ""
    cb = Cb("set:pf:rst:bogus")
    await tenant.set_profile_reset(cb, FakeBot())                        # ورودیِ ناشناس: بی‌اثر
    assert cb.message.sent == []


@pytest.mark.asyncio
async def test_non_admin_cannot_use_the_profile_admin_pages(db, monkeypatch):
    maker, ids, row = db
    import tenant
    await tenant.set_setting(ids["bot"], "profile_template_text", "قالب")
    for handler, data in ((tenant.set_profile_reset, "set:pf:rst:tpl"),
                          (tenant.set_profile_template_ask, "set:pf:tpl"),
                          (tenant.set_profile_default_show, "set:pf:def")):
        cb = Cb(data, uid=999_999)                                         # نه مالک نه ادمین
        if handler is tenant.set_profile_template_ask or handler is tenant.set_profile_reset:
            args = (cb, FakeBot(), State()) if handler is tenant.set_profile_template_ask else (cb, FakeBot())
        else:
            args = (cb, FakeBot())
        await handler(*args)
        assert cb.message.sent == [], data
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(ids["bot"], "profile_template_text", "") == "قالب"


def test_callback_data_fit_telegram_limit():
    for c in ("set:pf:tpl", "set:pf:def", "set:pf:footer", "set:pf:prev", "set:pf:rst:tpl", "set:pf:rst:footer"):
        assert len(c.encode()) <= 64


# ═════════════════ ۶) ورودی‌هایِ قبلی همچنان به همین منو می‌رسند ═════════════════

@pytest.mark.asyncio
async def test_keyboard_mode_and_partner_hub_entry_open_the_new_menu(db):
    maker, ids, row = db
    import sys, types
    from tenant.bots.virtual_service import settings as vs_settings
    seen = {}

    async def fake_start(cb, bot, state):
        seen["called"] = True
    import tenant
    real = tenant.set_profile_footer_start
    tenant.set_profile_footer_start = fake_start
    try:
        item = next(i for i in vs_settings.settings_registry.registered_items("virtual_service")
                    if i.callback == "set:profile_footer") if hasattr(vs_settings, "settings_registry") else None
    except Exception:
        item = None
    tenant.set_profile_footer_start = real
    from tenant.settings_registry import registered_items
    item = next(i for i in registered_items("virtual_service") if i.callback == "set:profile_footer")
    assert item.needs_state and item.handler is not None
    src = open("tenant/__init__.py", encoding="utf-8").read()
    assert 'callback_data="set:profile_footer"' in src                     # دکمهٔ هابِ همکار هنوز همان callback


def test_profile_admin_handlers_are_registered_on_the_router():
    import tenant
    src = open("tenant/__init__.py", encoding="utf-8").read()
    for cb in ("set:profile_footer", "set:pf:tpl", "set:pf:def", "set:pf:footer", "set:pf:prev"):
        assert f'F.data == "{cb}"' in src, cb
    assert 'F.data.startswith("set:pf:rst:")' in src
    assert "AdminFSM.profile_template" in src
