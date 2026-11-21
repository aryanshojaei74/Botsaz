"""PATCH_BOOKING_V2.2 — ثبتِ نوبتِ دستیِ مالک (مشتریِ حضوری/تلفنی) با دقتِ بالا."""
import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

import models
import tenant
from tenant.bots.booking import bk_admin as ADM
from tenant.bots.booking import bk_manual as M
from tenant.bots.booking import engine as E
from tenant.bots.booking import loop as L
from tests.test_booking_flow import BOT, CUST, CUST2, NOW, OWNER, TMR, FCb, FMsg, _book_flow, env, fsm  # noqa: F401

DS = TMR.strftime("%Y%m%d")


async def _ui_manual(env, *, name="سارا احمدی", phone="09121112233", hm=600, tg=None, day=DS, uid=OWNER):
    """مسیرِ کاملِ دکمه‌ها تا ثبتِ نهایی؛ خروجی: callback آخر."""
    st = fsm(uid)
    cb = FCb("bka:m", uid)
    await M.bka_m_start(cb, env.bot, st)
    assert "bka:m:s:1" in cb.buttons()
    cb = FCb("bka:m:s:1", uid)
    await M.bka_m_service(cb, env.bot, st)
    assert f"bka:m:d:1:0:{day}" in cb.buttons()
    cb = FCb(f"bka:m:d:1:0:{day}", uid)
    await M.bka_m_day(cb, env.bot)
    assert f"bka:m:h:1:0:{day}:{hm}" in cb.buttons()
    cb = FCb(f"bka:m:h:1:0:{day}:{hm}", uid)
    await M.bka_m_time(cb, env.bot, st)
    assert "bka:m:new" in cb.buttons()
    if tg is None:
        await M.bka_m_new(FCb("bka:m:new", uid), env.bot, st)
        await M.bka_m_name(FMsg(uid, name), env.bot, st)
        bad = FMsg(uid, "12")
        await M.bka_m_phone(bad, env.bot, st)
        assert "معتبر نیست" in bad.out[-1][0]
        await M.bka_m_phone(FMsg(uid, phone), env.bot, st)
    else:
        await M.bka_m_pick_customer(FCb(f"bka:m:c:{tg}", uid), env.bot, st)
    cb = FCb("bka:m:nonote", uid)
    await M.bka_m_nonote(cb, env.bot, st)
    assert "bka:m:ok" in cb.buttons()
    cb = FCb("bka:m:ok", uid)
    await M.bka_m_confirm(cb, env.bot, st)
    return cb


async def _all(env, model):
    async with env.mk() as s:
        return (await s.scalars(select(model))).all()


@pytest.mark.asyncio
async def test_manual_walkin_customer_is_confirmed_without_telegram_side_effects(env):
    cb = await _ui_manual(env)
    assert "نوبت ثبت شد" in cb.texts() and "بدونِ تلگرام" in cb.texts() and "tg://" not in cb.texts()
    ap = (await _all(env, models.BkAppt))[0]
    assert ap.status == "confirmed" and ap.tg_id < 0 and ap.phone == "09121112233" and (ap.start_min, ap.end_min) == (600, 660)
    cu = (await _all(env, models.BkCustomer))[0]
    assert cu.tg_id == ap.tg_id and cu.name == "سارا احمدی" and cu.blocked is False
    assert env.bot.sent == []                                          # هیچ پیامی به تلگرام نرفت
    # مالک‌ها خبر ندارند لازم نیست؛ اما اکشن‌هایِ نوبت (انجام شد/لغو) بدونِ خطا و بدونِ پیامِ تلگرام کار می‌کنند
    done = FCb(f"bka:done:{ap.id}", OWNER)
    await ADM.bka_act(done, env.bot)
    assert (await E.paid_of(BOT, ap.id)) == 0 and env.bot.sent == []
    cx = FCb(f"bka:cx:{ap.id}", OWNER)                                  # done قابلِ لغو نیست؛ خطا نمی‌دهد
    await ADM.bka_act(cx, env.bot)
    # یادآوری: هیچ‌وقت برایِ مشتریِ بدونِ تلگرام نمی‌رود
    ap2 = (await _ui_manual(env, name="رضا", phone="09125556677", hm=660), (await _all(env, models.BkAppt)))[1][-1]
    sent = []

    class B:
        async def send_message(self, chat_id, text, **k):
            sent.append(chat_id)
    assert await L.run_once(dt.datetime(2026, 11, 5, 20, 0), bot_factory=lambda r: B()) == 0 and sent == []
    assert ap2.tg_id < 0


@pytest.mark.asyncio
async def test_manual_booking_ignores_lead_time_and_blocked_but_not_conflicts(env):
    from tenant import set_setting
    await set_setting(BOT, "bk_lead_min", "600")                          # مشتری حداقل ۱۰ ساعت قبل
    tenant._SETTING_CACHE.clear()
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    today = NOW.date()
    ap, err = await E.book(BOT, 999, svc, today, 9 * 60, None, "x", "09", "", cfg, NOW)                # مشتریِ عادی: زیرِ lead
    assert ap is None and err == "taken"
    ap, err = await E.book(BOT, 998, svc, today, 9 * 60, None, "علی", "0912", "", cfg, NOW, by_owner=True)   # مالک: می‌شود
    assert err is None and ap.status == "confirmed"
    # مسدود: مالک می‌تواند، خودِ مشتری نه
    async with env.mk() as s:
        s.add(models.BkCustomer(bot_id=BOT, tg_id=777, name="b", phone="0911", blocked=True))
        await s.commit()
    assert (await E.book(BOT, 777, svc, TMR, 600, None, "b", "0911", "", cfg, NOW))[1] == "blocked"
    assert (await E.book(BOT, 777, svc, TMR, 600, None, "b", "0911", "", cfg, NOW, by_owner=True))[0] is not None
    # تداخل همچنان کنترل می‌شود
    assert (await E.book(BOT, 555, svc, TMR, 630, None, "c", "0913", "", cfg, NOW, by_owner=True))[1] == "taken"
    # تعطیلی
    async with env.mk() as s:
        s.add(models.BkOff(bot_id=BOT, staff_id=0, date_str=dt.date(2026, 11, 8).isoformat()))
        await s.commit()
    assert (await E.book(BOT, 555, svc, dt.date(2026, 11, 8), 600, None, "c", "0913", "", cfg, NOW, by_owner=True))[1] == "taken"


@pytest.mark.asyncio
async def test_same_phone_reuses_customer_file_and_ids_are_unique_negative(env):
    c1 = await E.find_or_create_manual_customer(BOT, "علی", "09120000001")
    c2 = await E.find_or_create_manual_customer(BOT, "علیِ دوم", "09120000001")        # همان شماره ⇒ همان پرونده
    c3 = await E.find_or_create_manual_customer(BOT, "بدونِ شماره ۱", "")
    c4 = await E.find_or_create_manual_customer(BOT, "بدونِ شماره ۲", "")                # بدونِ شماره: هر بار پروندهٔ تازه
    assert c1.id == c2.id and c1.tg_id == c2.tg_id < 0
    assert len({c1.tg_id, c3.tg_id, c4.tg_id}) == 3 and all(x < 0 for x in (c1.tg_id, c3.tg_id, c4.tg_id))
    # شمارهٔ مشتریِ واقعی (تلگرام‌دار) هم همان پرونده را برمی‌گرداند و تلگرامش حفظ می‌شود
    async with env.mk() as s:
        s.add(models.BkCustomer(bot_id=BOT, tg_id=4242, name="واقعی", phone="09129998877"))
        await s.commit()
    r = await E.find_or_create_manual_customer(BOT, "x", "09129998877")
    assert r.tg_id == 4242


@pytest.mark.asyncio
async def test_existing_telegram_customer_is_notified_and_listed_first(env):
    await _book_flow(env)                                                  # CUST با تلگرام نوبت گرفته
    env.bot.sent.clear()
    cb = await _ui_manual(env, tg=CUST, hm=660)
    assert "نوبت ثبت شد" in cb.texts()
    aps = await _all(env, models.BkAppt)
    mine = [a for a in aps if a.start_min == 660][0]
    assert mine.tg_id == CUST and mine.status == "confirmed" and mine.customer_name == "علی رضایی"
    assert any(c == CUST and "برایِ شما ثبت شد" in t for c, t in env.bot.sent)           # به مشتری خبر رفت


@pytest.mark.asyncio
async def test_conflict_and_session_loss_and_permissions(env):
    await _ui_manual(env, hm=600)
    # همان ساعت دوباره: در انتخابِ ساعت دیگر نیست
    cb = FCb(f"bka:m:d:1:0:{DS}", OWNER)
    await M.bka_m_day(cb, env.bot)
    assert f"bka:m:h:1:0:{DS}:600" not in cb.buttons() and f"bka:m:h:1:0:{DS}:660" in cb.buttons()
    # شکارِ ساعتِ پر (race): کاربر قبل از ثبت، ساعت را از دست می‌دهد
    st = fsm(OWNER)
    await M.bka_m_time(FCb(f"bka:m:h:1:0:{DS}:660", OWNER), env.bot, st)
    await M.bka_m_new(FCb("bka:m:new", OWNER), env.bot, st)
    await M.bka_m_name(FMsg(OWNER, "تست"), env.bot, st)
    await M.bka_m_phone(FMsg(OWNER, "-"), env.bot, st)
    await M.bka_m_nonote(FCb("bka:m:nonote", OWNER), env.bot, st)
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    await E.book(BOT, 31337, svc, TMR, 660, None, "دیگری", "0914", "", cfg, NOW, by_owner=True)    # همین لحظه پر شد
    late = FCb("bka:m:ok", OWNER)
    await M.bka_m_confirm(late, env.bot, st)
    assert any("همین الان پر شد" in a for a in late.alerts)
    assert len([a for a in await _all(env, models.BkAppt) if a.start_min == 660]) == 1
    # نشستِ منقضی (state خالی)
    lost = FCb("bka:m:ok", OWNER)
    await M.bka_m_confirm(lost, env.bot, fsm(OWNER))
    assert "نشست منقضی" in lost.alerts[-1]
    # غیرِ ادمین هیچ‌کدام از مراحل را نمی‌بیند
    for fn, data in ((M.bka_m_start, "bka:m"), (M.bka_m_confirm, "bka:m:ok")):
        stranger = FCb(data, 9999)
        await fn(stranger, env.bot, fsm(9999))
        assert not stranger.message.out
    no = FMsg(9999, "علی")
    await M.bka_m_name(no, env.bot, fsm(9999))
    assert not no.out


@pytest.mark.asyncio
async def test_concurrent_bookings_of_same_slot_only_one_wins(env):
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    res = await asyncio.gather(*[E.book(BOT, 100 + i, svc, TMR, 600, None, f"n{i}", "0912", "", cfg, NOW, by_owner=(i % 2 == 0)) for i in range(8)])
    assert sum(1 for ap, _e in res if ap) == 1 and sum(1 for _ap, e in res if e == "taken") == 7
    assert len([a for a in await _all(env, models.BkAppt) if a.start_min == 600]) == 1


@pytest.mark.asyncio
async def test_hub_has_manual_button_and_customer_file_handles_negative_ids(env):
    from tenant.bots.booking import bk_finance as FIN
    hub = FCb("bka:ap", OWNER)
    await ADM.bka_hub_appts(hub, env.bot, fsm(OWNER))
    assert "bka:m" in hub.buttons()
    await _ui_manual(env)
    cu = (await _all(env, models.BkCustomer))[0]
    assert cu.tg_id < 0
    lst = FCb("bka:cu:0", OWNER)
    await FIN.bka_customers(lst, env.bot, fsm(OWNER))
    assert f"bka:cuv:{cu.tg_id}" in lst.buttons()
    v = FCb(f"bka:cuv:{cu.tg_id}", OWNER)
    await FIN.bka_customer_view(v, env.bot, fsm(OWNER))
    assert "کلِ نوبت‌ها: <b>1</b>" in v.texts() and "tg://" not in v.texts() and "بدونِ تلگرام" in v.texts()
    # یادداشت/مسدودی هم روی شناسهٔ منفی کار می‌کند
    st = fsm(OWNER)
    await FIN.bka_customer_note(FCb(f"bka:cun:{cu.tg_id}", OWNER), env.bot, st)
    await FIN.bka_customer_note_in(FMsg(OWNER, "مشتریِ حضوری"), env.bot, st)
    await FIN.bka_customer_block(FCb(f"bka:cub:{cu.tg_id}", OWNER), env.bot, fsm(OWNER))
    async with env.mk() as s:
        c2 = await s.get(models.BkCustomer, cu.id)
    assert c2.note == "مشتریِ حضوری" and c2.blocked is True
