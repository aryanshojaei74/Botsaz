"""PATCH_BOOKING_V2.3 — بیعانهٔ آنلاین (پیش‌پرداخت از کیف پول): نگه‌داشتنِ ساعت، پرداختِ اتمی، بازگشت طبقِ سیاست، انقضا، مالی."""
import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

import models
import tenant
from tenant.bots.booking import bk_admin as ADM
from tenant.bots.booking import bk_finance as FIN
from tenant.bots.booking import bk_manual as MAN
from tenant.bots.booking import bk_user as U
from tenant.bots.booking import engine as E
from tenant.bots.booking import loop as L
from tests.test_booking_flow import BOT, CUST, CUST2, NOW, OWNER, TMR, FCb, FMsg, _book_flow, env, fsm  # noqa: F401

DEP = 100_000


async def _setup(env, mode="fixed", value=DEP, balance=500_000, **extra):
    async with env.mk() as s:
        s.add(models.BotUser(bot_id=BOT, telegram_id=CUST, balance=Decimal(balance), full_name="Ali"))
        s.add(models.BotUser(bot_id=BOT, telegram_id=CUST2, balance=Decimal(balance), full_name="Sara"))
        await s.commit()
    for k, v in {"bk_deposit_mode": mode, "bk_deposit_value": str(value), **extra}.items():
        await tenant.set_setting(BOT, k, v)
    tenant._SETTING_CACHE.clear()


async def _bal(env, tg=CUST):
    async with env.mk() as s:
        return int(await s.scalar(select(models.BotUser.balance).where(models.BotUser.bot_id == BOT, models.BotUser.telegram_id == tg)))


async def _appt(env, i=0):
    async with env.mk() as s:
        return (await s.scalars(select(models.BkAppt).order_by(models.BkAppt.id))).all()[i]


async def _txns(env):
    async with env.mk() as s:
        return [(t.kind, int(t.amount)) for t in (await s.scalars(select(models.WalletTxn).order_by(models.WalletTxn.id))).all()]


async def _pay(env, aid, uid=CUST):
    cb = FCb(f"bk:pay:{aid}", uid)
    await U.bk_pay(cb, env.bot)
    return cb


# ───────────── محاسبه ─────────────

def test_deposit_for_modes_caps_and_zero_price():
    c = lambda m, v: {"dep_mode": m, "dep_value": v}      # noqa: E731
    assert E.deposit_for(300000, c("off", 5)) == 0
    assert E.deposit_for(300000, c("fixed", 100000)) == 100000
    assert E.deposit_for(50000, c("fixed", 100000)) == 50000            # سقف = قیمتِ خدمت
    assert E.deposit_for(0, c("fixed", 100000)) == 100000               # «توافقی» ⇒ مبلغِ ثابت
    assert E.deposit_for(300000, c("percent", 30)) == 90000
    assert E.deposit_for(0, c("percent", 30)) == 0
    assert E.deposit_for(300000, c("percent", 500)) == 300000           # بیش از ۱۰۰٪ ⇒ سقفِ ۱۰۰٪
    assert E.deposit_for(300000, c("fixed", -5)) == 0


# ───────────── نگه‌داشتنِ ساعت و پرداخت ─────────────

@pytest.mark.asyncio
async def test_booking_with_deposit_holds_slot_without_notifying_owner_or_charging(env):
    await _setup(env)
    await _book_flow(env)
    ap = await _appt(env)
    assert ap.status == "hold" and ap.deposit == DEP
    assert await _bal(env) == 500_000 and await _txns(env) == []                 # هنوز پولی نرفته
    assert env.notified == []                                                    # مالک هنوز خبر ندارد
    cb2 = FCb(f"bk:d:1:0:{TMR.strftime('%Y%m%d')}", CUST2)                       # ساعت برایِ دیگران بسته است
    await U.bk_pick_time(cb2, env.bot)
    assert f"bk:h:1:0:{TMR.strftime('%Y%m%d')}:540" not in cb2.buttons()


@pytest.mark.asyncio
async def test_pay_debits_exactly_once_even_on_double_click_and_notifies_owner(env):
    await _setup(env)
    await _book_flow(env)
    ap = await _appt(env)
    res = await asyncio.gather(_pay(env, ap.id), _pay(env, ap.id))
    assert await _bal(env) == 500_000 - DEP                                      # یک‌بار کسر
    assert await _txns(env) == [("booking_deposit", -DEP)]
    async with env.mk() as s:
        pays = (await s.scalars(select(models.BkPayment))).all()
    assert [(p.method, p.amount) for p in pays] == [("wallet", DEP)]
    ap = await _appt(env)
    assert ap.status == "pending"                                               # تأییدِ خودکار خاموش
    assert len(env.notified) == 1 and "بیعانهٔ 100,000" in env.notified[0][0]
    assert sum("قبلاً تعیین‌تکلیف" in a for r in res for a in r.alerts) == 1      # کلیکِ دوم رد شد


@pytest.mark.asyncio
async def test_auto_confirm_confirms_after_payment(env):
    await _setup(env, bk_auto_confirm="1")
    await _book_flow(env)
    ap = await _appt(env)
    await _pay(env, ap.id)
    assert (await _appt(env)).status == "confirmed"


@pytest.mark.asyncio
async def test_insufficient_balance_changes_nothing_and_offers_wallet_charge(env):
    await _setup(env, balance=30_000)
    await _book_flow(env)
    ap = await _appt(env)
    cb = await _pay(env, ap.id)
    assert any("کافی نیست" in a for a in cb.alerts)
    assert await _bal(env) == 30_000 and await _txns(env) == [] and (await _appt(env)).status == "hold"
    assert "w:back" in cb.buttons() and "70,000" in cb.texts()                  # کسری + دکمهٔ شارژ
    # بعد از شارژ، از «نوبت‌هایِ من» می‌تواند پرداخت کند
    async with env.mk() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == CUST))
        u.balance = Decimal(200_000)
        await s.commit()
    v = FCb(f"bk:v:{ap.id}")
    await U.bk_view(v, env.bot)
    assert f"bk:pay:{ap.id}" in v.buttons()
    await _pay(env, ap.id)
    assert await _bal(env) == 100_000 and (await _appt(env)).status == "pending"


@pytest.mark.asyncio
async def test_hold_expiry_frees_slot_without_charge_lazily_and_in_loop(env):
    await _setup(env, bk_hold_min="15")
    await _book_flow(env)
    ap = await _appt(env)
    async with env.mk() as s:
        a = await s.get(models.BkAppt, ap.id)
        a.created_at = dt.datetime.utcnow() - dt.timedelta(minutes=30)
        await s.commit()
    # بدونِ اجرایِ حلقه هم ساعت برایِ دیگران آزاد است
    cb2 = FCb(f"bk:d:1:0:{TMR.strftime('%Y%m%d')}", CUST2)
    await U.bk_pick_time(cb2, env.bot)
    assert f"bk:h:1:0:{TMR.strftime('%Y%m%d')}:540" in cb2.buttons()
    # پرداختِ دیرهنگام رد می‌شود و پولی نمی‌رود
    late = await _pay(env, ap.id)
    assert any("مهلتِ پرداخت تمام شد" in a for a in late.alerts)
    assert await _bal(env) == 500_000 and await _txns(env) == [] and (await _appt(env)).status == "cancelled"
    # حلقه: holdِ منقضی ⇒ لغو
    await _book_flow(env, uid=CUST2)
    async with env.mk() as s:
        h = (await s.scalars(select(models.BkAppt).where(models.BkAppt.tg_id == CUST2))).all()[0]
        h.created_at = dt.datetime.utcnow() - dt.timedelta(minutes=40)
        await s.commit()
    await L.run_once(dt.datetime(2026, 11, 5, 8, 0), bot_factory=lambda r: None)
    assert (await _appt(env, 1)).status == "cancelled"


@pytest.mark.asyncio
async def test_release_hold_by_customer_and_cross_user_isolation(env):
    await _setup(env)
    await _book_flow(env)
    ap = await _appt(env)
    other = FCb(f"bk:xh:{ap.id}", CUST2)
    await U.bk_release_hold(other, env.bot)
    assert "پیدا نشد" in other.alerts[-1] and (await _appt(env)).status == "hold"
    steal = await _pay(env, ap.id, uid=CUST2)                                   # دیگری نمی‌تواند بپردازد
    assert await _bal(env, CUST2) == 500_000 and (await _appt(env)).status == "hold"
    await U.bk_release_hold(FCb(f"bk:xh:{ap.id}"), env.bot)
    assert (await _appt(env)).status == "cancelled" and await _bal(env) == 500_000


# ───────────── بازگشتِ بیعانه ─────────────

async def _paid_appt(env, **extra):
    await _setup(env, **extra)
    await _book_flow(env)
    ap = await _appt(env)
    await _pay(env, ap.id)
    return ap.id


@pytest.mark.asyncio
async def test_customer_cancel_early_refunds_fully_once(env):
    aid = await _paid_appt(env)                                                  # نوبت ۲۵ ساعت دیگر؛ مهلتِ بازگشت ۲۴
    ask = FCb(f"bk:x:{aid}")
    await U.bk_cancel_ask(ask, env.bot)
    assert "به کیف پولت برمی‌گردد" in ask.texts()
    cb = FCb(f"bk:xy:{aid}")
    await U.bk_cancel_do(cb, env.bot)
    assert await _bal(env) == 500_000 and (await _appt(env)).status == "cancelled"
    assert await _txns(env) == [("booking_deposit", -DEP), ("booking_refund", DEP)]
    assert await E.net_deposit(BOT, aid) == 0
    assert "برگشت" in cb.message.out[0][0]
    assert await E.refund_deposit(BOT, aid, await E.get_cfg(BOT), force_full=True) == 0      # idempotent
    assert await _bal(env) == 500_000


@pytest.mark.asyncio
async def test_customer_cancel_late_keeps_deposit(env):
    aid = await _paid_appt(env, bk_refund_hours="48", bk_cancel_hours="1")      # ۲۵ ساعت مانده < ۴۸ ⇒ بدونِ بازگشت
    ask = FCb(f"bk:x:{aid}")
    await U.bk_cancel_ask(ask, env.bot)
    assert "برگردانده نمی‌شود" in ask.texts()
    cb = FCb(f"bk:xy:{aid}")
    await U.bk_cancel_do(cb, env.bot)
    assert (await _appt(env)).status == "cancelled" and await _bal(env) == 500_000 - DEP
    assert "بیعانه برگردانده نمی‌شود" in cb.message.out[0][0]
    assert await E.net_deposit(BOT, aid) == DEP


@pytest.mark.asyncio
async def test_owner_reject_and_owner_cancel_always_refund_fully_and_notify(env):
    aid = await _paid_appt(env, bk_refund_hours="9999")                           # حتی با مهلتِ بازگشتِ غیرممکن
    await ADM.bka_act(FCb(f"bka:no:{aid}", OWNER), env.bot)
    assert await _bal(env) == 500_000 and (await _appt(env)).status == "rejected"
    assert any(c == CUST and "برگشت" in t for c, t in env.bot.sent)
    # لغوِ مالک برای نوبتِ تأییدشده
    env.bot.sent.clear()
    await _book_flow(env, uid=CUST2)
    ap2 = (await _appt(env, 1)).id
    await _pay(env, ap2, uid=CUST2)
    await ADM.bka_act(FCb(f"bka:ok:{ap2}", OWNER), env.bot)
    assert await _bal(env, CUST2) == 500_000 - DEP
    await ADM.bka_act(FCb(f"bka:cx:{ap2}", OWNER), env.bot)
    assert await _bal(env, CUST2) == 500_000 and (await _appt(env, 1)).status == "cancelled"
    assert any(c == CUST2 and "برگشت" in t for c, t in env.bot.sent)


@pytest.mark.asyncio
async def test_noshow_keeps_deposit_and_finance_nets_it(env):
    aid = await _paid_appt(env, bk_auto_confirm="1")
    await ADM.bka_act(FCb(f"bka:ns:{aid}", OWNER), env.bot)                     # نیامد
    assert (await _appt(env)).status == "noshow" and await _bal(env) == 500_000 - DEP
    # انجام‌شده: بیعانه از مبلغِ نوبت کم می‌شود
    await _book_flow(env, uid=CUST2, hm=600)
    ap2 = (await _appt(env, 1)).id
    await _pay(env, ap2, uid=CUST2)
    async with env.mk() as s:
        a = await s.get(models.BkAppt, ap2)
        a.status, a.date_str, a.price = "done", "2026-11-05", 300000
        await s.commit()
    f = await E.finance_summary(BOT, dt.datetime.utcnow() + dt.timedelta(hours=3, minutes=30))
    assert f["owed_total"] == 200000 and f["by_method"].get("wallet") == 2 * DEP
    card = FCb(f"bka:a:{ap2}:up:0", OWNER)
    await ADM.bka_view(card, env.bot)
    assert "پرداخت‌شده: <b>100,000</b> از 300,000" in card.texts() and "مانده: <b>200,000</b>" in card.texts()


@pytest.mark.asyncio
async def test_stale_pending_is_cancelled_and_refunded_by_loop(env):
    aid = await _paid_appt(env)
    sent = []

    class B:
        async def send_message(self, chat_id, text, **k):
            sent.append((chat_id, text))
    later = dt.datetime(2026, 11, 6, 12, 0)                                      # بعد از شروعِ نوبت (۹:۰۰) بدونِ تأیید
    await L.run_once(later, bot_factory=lambda r: B())
    assert (await _appt(env)).status == "cancelled" and await _bal(env) == 500_000
    assert any(c == CUST and "برگشت" in t for c, t in sent)


# ───────────── امنیت و تنظیمات ─────────────

@pytest.mark.asyncio
async def test_owner_cannot_forge_wallet_payment_and_manual_booking_has_no_deposit(env):
    await _setup(env)
    aid = await _appt_manual(env)
    cb = FCb(f"bka:pa:{aid}:wallet:50000", OWNER)
    await FIN.bka_pay_all(cb, env.bot)
    async with env.mk() as s:
        pays = (await s.scalars(select(models.BkPayment))).all()
    assert [(p.method, p.amount) for p in pays] == [("other", 50000)]           # «wallet» پذیرفته نشد
    assert await E.net_deposit(BOT, aid) == 0


async def _appt_manual(env):
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    ap, err = await E.book(BOT, CUST, svc, TMR, 780, None, "علی", "0912", "", cfg, NOW, by_owner=True)
    assert err is None and ap.status == "confirmed" and ap.deposit == 0         # ثبتِ دستی: بدونِ بیعانه
    return ap.id


@pytest.mark.asyncio
async def test_settings_page_cycle_modes_and_validation(env):
    st = fsm(OWNER)
    page = FCb("bka:dep", OWNER)
    await ADM.bka_deposit(page, env.bot, st)
    assert "بیعانه" in page.texts() and "bka:depm" in page.buttons()
    await ADM.bka_deposit_mode(FCb("bka:depm", OWNER), env.bot, st)              # off → fixed
    tenant._SETTING_CACHE.clear()
    assert (await E.get_cfg(BOT))["dep_mode"] == "fixed"
    await ADM.bka_cfg_value(FCb("bka:cfgv:bk_deposit_value", OWNER), env.bot, st)
    await ADM.bka_cfg_save(FMsg(OWNER, "۱۵۰٬۰۰۰"), env.bot, st)
    tenant._SETTING_CACHE.clear()
    assert (await E.get_cfg(BOT))["dep_value"] == 150000
    await ADM.bka_deposit_mode(FCb("bka:depm", OWNER), env.bot, st)              # fixed → percent (مقدارِ بزرگ ⇒ ۳۰)
    tenant._SETTING_CACHE.clear()
    c = await E.get_cfg(BOT)
    assert c["dep_mode"] == "percent" and c["dep_value"] == 30
    await ADM.bka_cfg_value(FCb("bka:cfgv:bk_deposit_value", OWNER), env.bot, st)
    bad = FMsg(OWNER, "250")
    await ADM.bka_cfg_save(bad, env.bot, st)
    assert "۰ تا ۱۰۰" in bad.out[-1][0]
    await ADM.bka_cfg_value(FCb("bka:cfgv:bk_hold_min", OWNER), env.bot, st)
    bad2 = FMsg(OWNER, "2")
    await ADM.bka_cfg_save(bad2, env.bot, st)
    assert "بینِ" in bad2.out[-1][0]
    await ADM.bka_deposit_mode(FCb("bka:depm", OWNER), env.bot, st)              # percent → off
    tenant._SETTING_CACHE.clear()
    assert (await E.get_cfg(BOT))["dep_mode"] == "off"
    h = FCb("bka:h:dep", OWNER)
    await ADM.bka_help(h, env.bot)
    assert "بیعانه" in h.texts()


@pytest.mark.asyncio
async def test_concurrent_customer_and_owner_cancel_refund_exactly_once(env):
    aid = await _paid_appt(env)
    await asyncio.gather(U.bk_cancel_do(FCb(f"bk:xy:{aid}"), env.bot), ADM.bka_act(FCb(f"bka:cx:{aid}", OWNER), env.bot),
                         ADM.bka_act(FCb(f"bka:no:{aid}", OWNER), env.bot))
    assert await _bal(env) == 500_000                                          # نه کمتر نه بیشتر
    assert (await _txns(env)).count(("booking_refund", DEP)) == 1
    assert await E.net_deposit(BOT, aid) == 0 and (await _appt(env)).status in ("cancelled", "rejected")


@pytest.mark.asyncio
async def test_status_transitions_are_compare_and_swap(env):
    await _setup(env, mode="off")
    await _book_flow(env)
    ap = await _appt(env)
    res = await asyncio.gather(*[E.set_status(BOT, ap.id, "confirmed", only_from=("pending",)) for _ in range(5)])
    assert sum(1 for r in res if r) == 1                                        # فقط یک برنده
    assert await E.set_status(BOT, ap.id, "confirmed", only_from=("pending",)) is None
    assert await E.set_status(BOT + 1, ap.id, "cancelled") is None               # ربات دیگر نمی‌تواند دست بزند
