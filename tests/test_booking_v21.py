"""PATCH_BOOKING_V2.1 — ورکرِ جدا/ضربان/ثبتِ اتمیِ یادآوری، پنلِ اختصاصی (خانه/راهنما/ورودها)، مالی و پروندهٔ مشتری."""
import datetime as dt

import pytest
from sqlalchemy import select

import models
import tenant
from tenant.bots.booking import bk_admin as ADM
from tenant.bots.booking import bk_finance as FIN
from tenant.bots.booking import bk_help as HELP
from tenant.bots.booking import bk_user as U
from tenant.bots.booking import engine as E
from tenant.bots.booking import loop as L
from tests.test_booking_flow import BOT, CUST, CUST2, NOW, OWNER, TMR, FCb, FMsg, _book_flow, env, fsm  # noqa: F401


async def _appt(env, status="done", price=300000, tg=CUST, name="علی", phone="0912"):
    async with env.mk() as s:
        a = models.BkAppt(bot_id=BOT, tg_id=tg, service_id=1, service_name="کوتاهی مو", date_str="2026-11-05", start_min=600, end_min=660,
                          price=price, status=status, customer_name=name, phone=phone)
        s.add(a)
        await s.commit()
        return a.id


# ───────────── ورکر / ضربان / ثبتِ اتمی ─────────────

@pytest.mark.asyncio
async def test_claim_is_atomic_and_unclaim_allows_retry(env):
    aid = await _appt(env, "confirmed")
    assert await L._claim(aid, 1) is True
    assert await L._claim(aid, 1) is False                     # بار دوم (پروسهٔ دیگر/ری‌استارت) نمی‌گیرد
    await L._unclaim(aid, 1)
    assert await L._claim(aid, 1) is True                      # ارسال نشده بود ⇒ دوباره
    assert await L._claim(aid, 2) is True and await L._claim(aid, 2) is False
    async with env.mk() as s:
        a = await s.get(models.BkAppt, aid)
    assert a.remind1_sent and a.remind2_sent


@pytest.mark.asyncio
async def test_two_workers_send_each_reminder_once_and_failed_send_retries(env):
    async with env.mk() as s:
        s.add(models.BkAppt(bot_id=BOT, tg_id=CUST, service_id=1, service_name="x", date_str="2026-11-06", start_min=540, end_min=600,
                            status="confirmed", customer_name="علی", phone="0912"))
        await s.commit()
    sent, fail = [], {"on": True}

    class B:
        async def send_message(self, chat_id, text, **k):
            if fail["on"]:
                raise RuntimeError("telegram down")
            sent.append(chat_id)
    now = dt.datetime(2026, 11, 5, 20, 0)
    assert await L.run_once(now, bot_factory=lambda r: B()) == 0       # ارسال شکست ⇒ پرچم برمی‌گردد
    fail["on"] = False
    import asyncio
    a, b = await asyncio.gather(L.run_once(now, bot_factory=lambda r: B()), L.run_once(now, bot_factory=lambda r: B()))
    assert a + b == 1 and sent == [CUST]                                # دو «ورکرِ هم‌زمان» ⇒ فقط یک پیام


@pytest.mark.asyncio
async def test_heartbeat_and_dashboard_warning(env):
    assert await L.worker_alive() is False
    cfg_home, _kb = await ADM.build_home(await tenant.get_bot_row(None))
    assert "موقتاً غیرفعال" in cfg_home
    await L.beat()
    assert await L.worker_alive() is True
    cfg_home, _kb = await ADM.build_home(await tenant.get_bot_row(None))
    assert "موقتاً غیرفعال" not in cfg_home
    async with env.mk() as s:
        r = await s.scalar(select(models.PlatformSetting).where(models.PlatformSetting.key == L.BEAT_KEY))
        r.value = (dt.datetime.utcnow() - dt.timedelta(minutes=30)).isoformat()
        await s.commit()
    assert await L.worker_alive() is False


def test_dedicated_worker_files_and_loop_not_in_shared_worker():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / "worker_booking.py").exists()
    unit = (root / "deploy" / "botsaz-booking-worker.service").read_text()
    assert "worker_booking.py" in unit and "Restart=always" in unit and "SIGTERM" in unit
    assert "booking" not in (root / "worker_tenant.py").read_text().lower()      # حلقه از ورکرِ مشترک جداست


# ───────────── پنلِ اختصاصی ─────────────

@pytest.mark.asyncio
async def test_home_hubs_help_and_generic_tools(env):
    row = await tenant.get_bot_row(None)
    text, kb = await ADM.build_home(row)
    datas = {b.callback_data for r in kb.inline_keyboard for b in r}
    assert {"bka:ap", "bka:cu:0", "bka:cat", "bka:tm", "bka:fin", "bka:stats", "bka:cfg", "bka:gen", "bka:h:home"} <= datas
    for hub in ("bka:ap", "bka:cat", "bka:tm"):
        cb = FCb(hub, OWNER)
        await getattr(ADM, {"bka:ap": "bka_hub_appts", "bka:cat": "bka_hub_catalog", "bka:tm": "bka_hub_time"}[hub])(cb, env.bot, fsm(OWNER))
        assert cb.buttons() and any(d.startswith("bka:h:") for d in cb.buttons())
    for key in HELP.HELP:
        cb = FCb(f"bka:h:{key}", OWNER)
        await ADM.bka_help(cb, env.bot)
        assert "راهنما" in cb.texts()
    g = FCb("bka:gen", OWNER)
    await ADM.bka_generic_tools(g, env.bot, fsm(OWNER))
    assert "bka:home" in g.buttons() and "ابزارهایِ عمومی" in g.texts()
    # PATCH_BOOKING_TOOLS_HUB_V1: دسته‌ها واقعاً پر هستند (قبلاً در منویِ کیبوردی خالی بود)
    assert {"bka:gc:pay", "bka:gc:ui", "bka:gc:usr", "bka:gc:bot"} <= set(g.buttons()), g.buttons()
    gc = FCb("bka:gc:pay", OWNER)
    await ADM.bka_generic_cat(gc, env.bot)
    assert "set:card" in gc.buttons() and "gwname:menu" in gc.buttons()
    # غیرِ ادمین هیچ‌چیز نمی‌بیند
    bad = FCb("bka:home", 9999)
    await ADM.bk_dashboard(bad, env.bot)
    assert not bad.message.out


@pytest.mark.asyncio
async def test_admin_entry_points_open_the_booking_panel(env):
    ADM._LAST_CAT.clear()          # حافظهٔ «آخرین دستهٔ ابزارها» بینِ تست‌ها نشت نکند
    cb = FCb("tadm:menu", OWNER)
    await tenant.tadm_menu(cb, env.bot)
    assert "bka:ap" in cb.buttons() and "bka:fin" in cb.buttons()
    m = FMsg(OWNER, "/admin")
    await tenant.tenant_admin(m, env.bot)
    assert m.out and "bka:cat" in {b.callback_data for r in m.out[0][1].inline_keyboard for b in r}
    kb = await tenant._admin_panel_kb(await tenant.get_bot_row(None))
    assert "bka:cu:0" in {b.callback_data for r in kb.inline_keyboard for b in r}


# ───────────── مالی ─────────────

@pytest.mark.asyncio
async def test_payments_receivables_and_finance_pages(env):
    aid = await _appt(env, "done", 300000)
    aid2 = await _appt(env, "done", 100000, tg=CUST2, name="سارا")
    assert (await E.finance_summary(BOT, NOW))["owed_total"] == 400000
    # ثبتِ پرداخت از مسیرِ دکمه‌ها: روش ← کلِ مانده
    await FIN.bka_pay_start(FCb(f"bka:pay:{aid}", OWNER), env.bot)
    cb = FCb(f"bka:pm:{aid}:cash", OWNER)
    await FIN.bka_pay_method(cb, env.bot)
    assert f"bka:pa:{aid}:cash:300000" in cb.buttons()
    done = FCb(f"bka:pa:{aid}:cash:300000", OWNER)
    await FIN.bka_pay_all(done, env.bot)
    assert "پرداخت‌شده: <b>300,000</b>" in done.texts() and "✅" in done.texts()
    # پرداختِ ناقص با مبلغِ دلخواه
    st = fsm(OWNER)
    await FIN.bka_pay_custom(FCb(f"bka:pc:{aid2}:card", OWNER), env.bot, st)
    bad = FMsg(OWNER, "abc")
    await FIN.bka_pay_custom_in(bad, env.bot, st)
    assert "فقط یک عدد" in bad.out[-1][0]
    ok = FMsg(OWNER, "۴۰٬۰۰۰")
    await FIN.bka_pay_custom_in(ok, env.bot, st)
    assert "مانده: <b>60,000</b>" in ok.out[-1][0]
    f = await E.finance_summary(BOT, dt.datetime.utcnow() + dt.timedelta(hours=3, minutes=30))
    assert f["d30"] == 340000 and f["owed_total"] == 60000 and f["by_method"] == {"cash": 300000, "card": 40000}
    for h, needle in (("bka:fin", "مطالبات"), ("bka:fd", "۷ روزِ اخیر"), ("bka:fb", "به‌تفکیکِ خدمت"), ("bka:fo:0", "مطالبات")):
        cb = FCb(h, OWNER)
        fn = {"bka:fin": FIN.bka_finance, "bka:fd": FIN.bka_fin_daily, "bka:fb": FIN.bka_fin_breakdown, "bka:fo:0": FIN.bka_fin_owed}[h]
        await (fn(cb, env.bot, fsm(OWNER)) if h == "bka:fin" else fn(cb, env.bot))
        assert needle in cb.texts(), h


# ───────────── مشتریان ─────────────

@pytest.mark.asyncio
async def test_customer_file_note_block_search_and_blocked_cannot_book(env):
    await _book_flow(env)                                   # پرونده با اولین نوبت ساخته می‌شود
    async with env.mk() as s:
        c = await s.scalar(select(models.BkCustomer))
    assert c.tg_id == CUST and c.phone == "09123456789" and c.name == "علی رضایی"
    cb = FCb("bka:cu:0", OWNER)
    await FIN.bka_customers(cb, env.bot, fsm(OWNER))
    assert f"bka:cuv:{CUST}" in cb.buttons()
    st = fsm(OWNER)
    await FIN.bka_customer_note(FCb(f"bka:cun:{CUST}", OWNER), env.bot, st)
    await FIN.bka_customer_note_in(FMsg(OWNER, "حساسیت به رنگ"), env.bot, st)
    v = FCb(f"bka:cuv:{CUST}", OWNER)
    await FIN.bka_customer_view(v, env.bot, fsm(OWNER))
    assert "حساسیت به رنگ" in v.texts() and "کلِ نوبت‌ها: <b>1</b>" in v.texts()
    # جستجو با شمارهٔ فارسی/بخشی از آن
    s = fsm(OWNER)
    await FIN.bka_customer_search(FCb("bka:cus", OWNER), env.bot, s)
    r = FMsg(OWNER, "۰۹۱۲۳۴")
    await FIN.bka_customer_search_in(r, env.bot, s)
    assert "1 نتیجه" in r.out[-1][0]
    # مسدودکردن ⇒ نوبتِ جدید رد می‌شود
    blk = FCb(f"bka:cub:{CUST}", OWNER)
    await FIN.bka_customer_block(blk, env.bot, fsm(OWNER))
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    ap, err = await E.book(BOT, CUST, svc, TMR, 600, None, "x", "09", "", cfg, NOW)
    assert ap is None and err == "blocked"
    await FIN.bka_customer_block(FCb(f"bka:cub:{CUST}", OWNER), env.bot, fsm(OWNER))       # رفعِ مسدودی
    assert (await E.book(BOT, CUST, svc, TMR, 600, None, "x", "09", "", cfg, NOW))[0] is not None


@pytest.mark.asyncio
async def test_quick_hours_update_rows_in_place_instead_of_delete_and_insert(env):
    async with env.mk() as s:
        before = {h.weekday: h.id for h in (await s.scalars(select(models.BkHours).where(models.BkHours.staff_id == 0))).all()}
    st = fsm(OWNER)
    await ADM.bka_hours_quick_days(FCb("bka:hqs:0:a", OWNER), env.bot, st)
    await ADM.bka_quick_start(FMsg(OWNER, "10:00"), env.bot, st)
    await ADM.bka_quick_end(FMsg(OWNER, "14:00"), env.bot, st)
    async with env.mk() as s:
        after = {h.weekday: (h.id, h.start_min, h.end_min) for h in (await s.scalars(select(models.BkHours).where(models.BkHours.staff_id == 0))).all()}
    for wd in range(5):                                        # شنبه تا چهارشنبه: همان ردیف، ساعتِ جدید
        assert after[wd] == (before[wd], 600, 840)
    assert after[6][1:] == (540, 1020)                        # جمعه دست‌نخورده
