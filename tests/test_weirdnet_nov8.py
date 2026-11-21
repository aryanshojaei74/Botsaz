"""Weirdnet 8 Nov: API — انتخابِ پنلِ تست، پروفایل/رفرال/کلیدِ API/تراکنش‌ها/پرداخت‌ها؛ تمدید = تنظیماتِ خریدِ همان پنل."""
import pytest

import models
from tests.test_vpn_api_extended import env, _call, FakeClient, TG   # noqa: F401


@pytest.fixture(autouse=True)
def _quiet():
    import tenant.shared.vpn_api as A
    A._RATE.clear()


async def _second_panel(env):
    async with env["maker"]() as s:
        s.add(models.VpnPanel(bot_id=env["bid"], panel_key="marzban", base_url="http://m", username="u", password="p", enabled=True))
        await s.commit()


async def _enable_trial(env):
    import tenant
    for k, v in {"trial_enabled": "1", "trial_gb": "2", "trial_days": "3", "trial_source": "panel"}.items():
        await tenant.set_setting(env["bid"], k, v)
    tenant._SETTING_CACHE.clear()


@pytest.mark.asyncio
async def test_panels_listing(env):
    await _second_panel(env)
    r = await _call(env, "panels")
    assert r["ok"] and r["count"] == 2 and {i["panel"] for i in r["items"]} == {"sanai", "marzban"}
    assert all("id" in i and "name" in i for i in r["items"]) and r["trial_enabled"] is False


@pytest.mark.asyncio
async def test_trial_can_choose_panel_by_key_or_id_and_default_is_first(env, monkeypatch):
    import tenant
    await _second_panel(env)
    await _enable_trial(env)
    seen = []

    def mk(panel):
        seen.append(panel.panel_key)
        return FakeClient()
    monkeypatch.setattr(tenant, "_trial_panel_client", mk)
    r = await _call(env, "trial", panel="marzban")
    assert r["ok"] and seen == ["marzban"]
    async with env["maker"]() as s:
        from sqlalchemy import select
        acc = await s.scalar(select(models.PanelAccount).where(models.PanelAccount.user_telegram_id == TG))
    assert acc.panel_key == "marzban"
    # کاربرِ دیگر با شناسهٔ عددی و بدونِ panel
    async with env["maker"]() as s:
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=777, full_name="B", balance=0))
        pid = (await s.scalar(select(models.VpnPanel.id).where(models.VpnPanel.panel_key == "sanai")))
        await s.commit()
    r2 = await _call(env, "trial", telegram_id="777", panel=str(pid))
    assert r2["ok"] and seen[-1] == "sanai"
    async with env["maker"]() as s:
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=778, full_name="C", balance=0))
        await s.commit()
    r3 = await _call(env, "trial", telegram_id="778")
    assert r3["ok"] and seen[-1] == "sanai"                                   # پیش‌فرض: اولین پنلِ فعال (قدیمی‌ترین)


@pytest.mark.asyncio
async def test_unknown_panel_does_not_burn_the_trial(env, monkeypatch):
    import tenant
    await _enable_trial(env)
    monkeypatch.setattr(tenant, "_trial_panel_client", lambda panel: FakeClient())
    r = await _call(env, "trial", panel="nope")
    assert r == {"ok": False, "error": "panel_not_found"}
    assert (await _call(env, "trial"))["ok"] is True                           # حقِ تست نسوخته بود


@pytest.mark.asyncio
async def test_profile_referral_apikey(env):
    r = await _call(env, "profile")
    p = r["profile"]
    assert r["ok"] and p["telegram_id"] == TG and p["balance"] == 1_000_000 and p["services_total"] == 0
    assert p["referral"]["link"].startswith("https://t.me/shop?start=ref_") and p["referral"]["joins"] == 0
    assert p["api"]["has_key"] is False
    r2 = await _call(env, "referral")
    assert r2["referral"]["code"] == p["referral"]["code"]                    # همان لینک (ساختِ دوباره ندارد)
    async with env["maker"]() as s:
        from sqlalchemy import select
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == TG))
        u.api_key = "usr_key_123"
        await s.commit()
    k = await _call(env, "apikey")
    assert k["has_key"] is True and k["api_key"] == "usr_key_123"
    # بدونِ telegram_id با کلیدِ کاملِ ربات: خطا (کلیدِ کسی لو نمی‌رود)
    r3 = await env["A"].dispatch(env["ctx"], "apikey", {})
    import json
    assert json.loads(r3.body)["error"] == "telegram_id_required"


@pytest.mark.asyncio
async def test_referral_counts_joins_and_rewards(env):
    r0 = await _call(env, "referral")
    code = r0["referral"]["code"]
    async with env["maker"]() as s:
        from sqlalchemy import select
        ref = await s.scalar(select(models.ReferralLink).where(models.ReferralLink.code == code))
        s.add(models.ReferralJoin(bot_id=env["bid"], referral_link_id=ref.id, inviter_user_id=ref.user_id,
                                  referred_telegram_id=9001, reward_paid=5000))
        s.add(models.ReferralJoin(bot_id=env["bid"], referral_link_id=ref.id, inviter_user_id=ref.user_id,
                                  referred_telegram_id=9002, reward_paid=7000))
        await s.commit()
    r = (await _call(env, "referral"))["referral"]
    assert r["joins"] == 2 and r["reward_earned"] == 12000


@pytest.mark.asyncio
async def test_transactions_and_payments_history(env):
    async with env["maker"]() as s:
        from sqlalchemy import select
        uid = await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == TG))
        for amt, kind, note in ((200000, "charge", "کارت"), (-100000, "purchase", "پلن"), (50000, "apay_charge", "آپی"),
                                (30000, "admin", "هدیه"), (-1000, "renew", "تمدید")):
            s.add(models.WalletTxn(bot_id=env["bid"], user_id=uid, amount=amt, kind=kind, note=note))
        # تراکنشِ کاربرِ دیگر نباید دیده شود
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=999, full_name="X", balance=0))
        await s.flush()
        other = await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == 999))
        s.add(models.WalletTxn(bot_id=env["bid"], user_id=other, amount=1, kind="charge", note="other"))
        await s.commit()
    t = await _call(env, "transactions")
    assert t["ok"] and t["total"] == 5 and [i["kind"] for i in t["items"]] == ["renew", "admin", "apay_charge", "purchase", "charge"]
    pg = await _call(env, "transactions", limit="2", offset="1")
    assert len(pg["items"]) == 2 and pg["total"] == 5 and pg["items"][0]["kind"] == "admin"
    pay = await _call(env, "payments")
    assert pay["total"] == 2 and {i["kind"] for i in pay["items"]} == {"charge", "apay_charge"} and all(i["amount"] > 0 for i in pay["items"])
    only = await _call(env, "transactions", kind="purchase")
    assert only["total"] == 1 and only["items"][0]["amount"] == -100000
    assert (await _call(env, "transactions", limit="9999"))["limit"] == 100


@pytest.mark.asyncio
async def test_renew_same_as_buy_mirrors_prices_and_ranges(env):
    import tenant
    from tenant.bots.vpn import vpn_user as V
    for k, v in {"renew_price_gb": "7", "renew_price_day": "3", "renew_min_days": "7", "renew_max_days": "21",
                 "renew_step_days": "7", "renew_min_gb": "10", "renew_max_gb": "40", "renew_step_gb": "10"}.items():
        await tenant.set_setting(env["bid"], k, v)
    for k, v in {"vc_step_gb": "5", "vc_step_days": "30"}.items():
        await tenant.set_setting(env["bid"], k, v)
    tenant._SETTING_CACHE.clear()
    V._VC_CACHE.clear()
    bid = env["bid"]
    assert await V._vc(bid, "renew_price_gb", "sanai") == "7"                                   # جدا: مقدارِ خودش
    await tenant.set_setting(bid, V.renew_same_key("sanai"), "1")
    V._VC_CACHE.clear()
    assert await V._vc(bid, "renew_price_gb", "sanai") == "1000"                                # همان قیمتِ خرید
    assert await V._vc(bid, "renew_price_day", "sanai") == "500"
    assert (await V._vc(bid, "renew_min_gb", "sanai"), await V._vc(bid, "renew_max_gb", "sanai"),
            await V._vc(bid, "renew_step_gb", "sanai")) == ("5", "100", "5")
    assert (await V._vc(bid, "renew_min_days", "sanai"), await V._vc(bid, "renew_max_days", "sanai"),
            await V._vc(bid, "renew_step_days", "sanai")) == ("30", "90", "30")
    assert await V._vc(bid, "renew_enabled", "sanai") == "1"                                    # فعال/غیرفعالِ دکمه‌ها جدا می‌ماند
    assert await V._vc(bid, "renew_price_gb", "marzban") == "7"                                 # پنلِ دیگر دست‌نخورده
    await tenant.set_setting(bid, V.renew_same_key("sanai"), "0")
    V._VC_CACHE.clear()
    assert await V._vc(bid, "renew_price_gb", "sanai") == "7"                                   # خاموش ⇒ برمی‌گردد


@pytest.mark.asyncio
async def test_admin_renew_screen_shows_mirror_and_hides_separate_editors(env):
    import tenant
    from tenant.bots.vpn import vpn_admin as AD, vpn_user as V
    bid = env["bid"]
    await tenant.set_setting(bid, V.renew_same_key("sanai"), "1")
    await tenant.set_setting(bid, "vc_pricing_mode", "tiers")
    tenant._SETTING_CACHE.clear()
    V._VC_CACHE.clear()
    cfg = await AD._renew_cfg(bid, "sanai")
    assert cfg["_same"] == "1" and cfg["renew_price_gb"] == "1000" and "تعرفه" in AD._renew_text(cfg, "x")
    kb = await AD._renew_kb(cfg, "sanai", bid)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "tvpn:renew_same:sanai" in datas and not any(d.startswith(("tvpn:renew_gb", "tvpn:renew_days", "tvpn:renew_price")) for d in datas)
    await tenant.set_setting(bid, V.renew_same_key("sanai"), "0")
    V._VC_CACHE.clear()
    cfg2 = await AD._renew_cfg(bid, "sanai")
    datas2 = [b.callback_data for r in (await AD._renew_kb(cfg2, "sanai", bid)).inline_keyboard for b in r]
    assert "tvpn:renew_price:sanai" in datas2


@pytest.mark.asyncio
async def test_renew_mirror_uses_tier_pricing_when_base_day_price_is_zero(env):
    """باگِ Weirdnet: پنل‌هایِ حجمی با «تعرفه‌هایِ مدت‌دار» قیمتِ پایهٔ روز = ۰ دارند؛ تمدیدِ هم‌ردیفِ خرید «فعال نیست» می‌داد."""
    import tenant
    from tenant.bots.vpn import vpn_user as V
    from tenant.bots.vpn import vpn_admin as AD
    bid = env["bid"]
    for k, v in {"vc_price_day": "0", "vc_price_gb": "0", "vc_pricing_mode": "tiers"}.items():
        await tenant.set_setting(bid, V._pvc_key_u("sanai", k) if k in V._VC_PANEL_KEYS_U else k, v)
    await AD.vc_tiers_set(bid, "sanai", [
        {"days_min": 30, "days_max": 30, "gb_min": 10, "gb_max": 100, "gb_step": 10, "price_gb": 2000, "price_day": 100},
        {"days_min": 60, "days_max": 60, "gb_min": 10, "gb_max": 100, "gb_step": 10, "price_gb": 1500, "price_day": 80},
    ])
    await tenant.set_setting(bid, V.renew_same_key("sanai"), "1")
    tenant._SETTING_CACHE.clear()
    V._VC_CACHE.clear()
    rc = await V._renew_cfg(bid, TG, "sanai")
    assert rc.mirror and rc.mn_d == 30 and rc.mx_d == 60
    assert await V._renew_quote(bid, TG, "sanai", rc, 30) == 30 * 100
    assert await V._renew_quote(bid, TG, "sanai", rc, 60, 20) == 60 * 80 + 20 * 1500
    assert V._renew_day_options(rc) == [30, 60]
