"""PATCH_BOOKING_V2.4 — کدِ پیگیریِ ترتیبیِ هر ربات (از ۱) + «پیگیری نوبت» با کد + سابقه + جستجویِ ادمین."""
import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

import models
import tenant
from tenant.bots.booking import bk_admin as ADM
from tenant.bots.booking import bk_user as U
from tenant.bots.booking import engine as E
from tests.test_booking_flow import BOT, CUST, CUST2, NOW, OWNER, TMR, FCb, FMsg, _book_flow, env, fsm  # noqa: F401
from tests.test_booking_deposit import _bal, _pay, _setup


async def _other_bot_with_appts(env, n=3):
    """ربات دیگر که قبل از ما نوبت دارد ⇒ شناسهٔ داخلیِ ما از ۱ شروع نمی‌شود (کد باید مستقل بماند)."""
    async with env.mk() as s:
        s.add(models.Bot(id=22, owner_id=2, token="2:T", username="other", field="booking", plan="pro", status="active"))
        s.add(models.BkService(id=9, bot_id=22, name="x", duration_min=30, price=0))
        await s.commit()
    svc = models.BkService(id=9, bot_id=22, name="x", duration_min=30, price=0)
    async with env.mk() as s:
        for i in range(n):
            s.add(models.BkAppt(bot_id=22, tg_id=1, service_id=9, service_name="x", date_str="2026-12-01", start_min=60 * i,
                                end_min=60 * i + 30, status="confirmed"))
        await s.commit()


async def _appts(env):
    async with env.mk() as s:
        return (await s.scalars(select(models.BkAppt).where(models.BkAppt.bot_id == BOT).order_by(models.BkAppt.id))).all()


async def _book_n(env, starts, by_owner=True, tg0=900):
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    return await asyncio.gather(*[E.book(BOT, tg0 + i, svc, TMR, st, None, f"n{i}", "0912", "", cfg, NOW, by_owner=by_owner) for i, st in enumerate(starts)])


# ───────────── کد ─────────────

@pytest.mark.asyncio
async def test_codes_start_at_one_per_bot_and_are_independent_of_internal_ids(env):
    await _other_bot_with_appts(env, 3)                                      # شناسه‌هایِ داخلیِ ۱ تا ۳ مالِ ربات دیگر
    await _book_flow(env)
    ap = (await _appts(env))[0]
    assert ap.id == 4 and ap.seq == 1 and E.appt_code(ap) == 1               # ربات ما: کدِ ۱ (نه ۴)
    cb = FCb("bk:confirm")                                                   # متنِ پیام: کدِ پیگیری ۱
    await _book_flow(env, uid=CUST2, hm=600)
    ap2 = (await _appts(env))[1]
    assert ap2.seq == 2
    async with env.mk() as s:
        c = await s.get(models.BkCounter, BOT)
    assert c.last_seq == 2
    # ربات دیگر شمارندهٔ خودش را دارد
    cfg = await E.get_cfg(22)
    async with env.mk() as s:
        svc = await s.get(models.BkService, 9)
        s.add(models.BkHours(bot_id=22, staff_id=0, weekday=E.fa_weekday(TMR), start_min=0, end_min=1440))
        await s.commit()
    ap3, err = await E.book(22, 5, svc, TMR, 600, None, "z", "09", "", cfg, NOW, by_owner=True)
    assert err is None and ap3.seq == 4                                       # ۳ نوبتِ قدیمیِ بدونِ کد ⇒ ۱ تا ۳ گرفتند؛ جدید ۴
    async with env.mk() as s:
        old = (await s.scalars(select(models.BkAppt).where(models.BkAppt.bot_id == 22, models.BkAppt.id <= 3).order_by(models.BkAppt.id))).all()
    assert [a.seq for a in old] == [1, 2, 3]


@pytest.mark.asyncio
async def test_concurrent_bookings_get_unique_gapless_codes_and_failures_do_not_burn_codes(env):
    res = await _book_n(env, [540, 570 + 30, 660, 720, 780, 840, 900, 960, 1000 - 40, 1020 - 120])
    got = sorted(ap.seq for ap, e in res if ap)
    assert got == list(range(1, len(got) + 1)) and len(got) >= 8              # یکتا و بدونِ حفره
    # رزروِ ناموفق (ساعتِ پر) کد نمی‌سوزاند
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    n_before = len(got)
    bad, err = await E.book(BOT, 12345, svc, TMR, 540, None, "x", "09", "", cfg, NOW, by_owner=True)
    assert bad is None and err == "taken"
    async with env.mk() as s:
        assert (await s.get(models.BkCounter, BOT)).last_seq == n_before
    # نوبتِ لغوشده هم کدش را نگه می‌دارد (کد دوباره استفاده نمی‌شود)
    first = (await _appts(env))[0]
    await E.set_status(BOT, first.id, "cancelled", "x")
    nxt, _e = await E.book(BOT, 777, svc, TMR, 540, None, "n", "09", "", cfg, NOW, by_owner=True)
    assert nxt.seq == n_before + 1


@pytest.mark.asyncio
async def test_legacy_rows_without_code_are_backfilled_in_order_and_remain_findable(env):
    async with env.mk() as s:
        for i in range(3):
            s.add(models.BkAppt(bot_id=BOT, tg_id=CUST, service_id=1, service_name="قدیمی", date_str="2026-11-20", start_min=60 * (i + 1),
                                end_min=60 * (i + 1) + 30, status="confirmed", customer_name="a", phone="0912"))
        await s.commit()
    legacy = await _appts(env)
    assert [E.appt_code(a) for a in legacy] == [a.id for a in legacy]         # قبل از تخصیص: شناسهٔ داخلی
    found = await E.find_by_code(BOT, legacy[1].id)
    assert found.id == legacy[1].id
    await _book_n(env, [540])
    rows = await _appts(env)
    assert [a.seq for a in rows] == [1, 2, 3, 4]                              # قدیمی‌ها ۱..۳، جدید ۴
    assert (await E.find_by_code(BOT, 2)).id == legacy[1].id


def test_parse_code_variants():
    assert E.parse_code("12") == 12 and E.parse_code("#12") == 12 and E.parse_code(" ۱۲ ") == 12 and E.parse_code("#۰۰۷") == 7
    for bad in ("", "abc", "0", "-3", "12a", "1.5", "99999999999999"):
        assert E.parse_code(bad) is None


# ───────────── پیگیری ─────────────

@pytest.mark.asyncio
async def test_customer_tracks_own_code_with_payments_and_cannot_see_others(env):
    await _other_bot_with_appts(env, 2)
    await _setup(env)
    await _book_flow(env)                                                       # CUST: کدِ ۱، بیعانه ۱۰۰٬۰۰۰
    ap = (await _appts(env))[0]
    await _pay(env, ap.id)
    st = fsm(CUST)
    await U.bk_track_start(FCb("book_track", CUST), env.bot, st)
    assert await st.get_state() == U.BkFSM.track.state
    bad = FMsg(CUST, "abc")
    await U.bk_track_in(bad, env.bot, st)
    assert "فقط یک عدد" in bad.out[-1][0] and await st.get_state() == U.BkFSM.track.state       # حالت می‌ماند
    ok = FMsg(CUST, "#۱")
    await U.bk_track_in(ok, env.bot, st)
    t = ok.out[-1][0]
    assert "نوبت #1" in t and "کدِ پیگیری: <b>#1</b>" in t and "👛 کیف پول: 100,000 تومان" in t and "خالصِ پرداخت‌شده: <b>100,000</b> از 300,000" in t
    assert await st.get_state() is None
    # مشتریِ دیگر: «کدِ مالِ دیگری» و «کدِ ناموجود» پیامِ یکسان می‌گیرند (لو نرفتنِ نوبت)
    other = FMsg(CUST2, "1")
    st2 = fsm(CUST2)
    await U.bk_track_start(FCb("book_track", CUST2), env.bot, st2)
    await U.bk_track_in(other, env.bot, st2)
    ghost = FMsg(CUST2, "999")
    await U.bk_track_in(ghost, env.bot, st2)
    assert other.out[-1][0] == ghost.out[-1][0] and "پیدا نشد" in other.out[-1][0] and "علی" not in other.out[-1][0]
    # کدِ ربات دیگر هم در این ربات پیدا نمی‌شود
    async with env.mk() as s:
        foreign = (await s.scalars(select(models.BkAppt).where(models.BkAppt.bot_id == 22))).first()
    assert (await E.find_by_code(BOT, 1)).bot_id == BOT
    assert await E.find_by_code(BOT, foreign.id) is None or (await E.find_by_code(BOT, foreign.id)).bot_id == BOT   # هرگز نوبتِ ربات دیگر


@pytest.mark.asyncio
async def test_tracking_shows_refund_lines_and_my_list_history_use_codes(env):
    await _setup(env)
    await _book_flow(env)
    ap = (await _appts(env))[0]
    await _pay(env, ap.id)
    await U.bk_cancel_do(FCb(f"bk:xy:{ap.id}"), env.bot)                       # لغوِ زودهنگام ⇒ بازگشتِ بیعانه
    assert await _bal(env) == 500_000
    st = fsm(CUST)
    await U.bk_track_start(FCb("book_track", CUST), env.bot, st)
    m = FMsg(CUST, "1")
    await U.bk_track_in(m, env.bot, st)
    t = m.out[-1][0]
    assert "↩️ بازگشت 100,000" in t and "خالصِ پرداخت‌شده: <b>0</b>" in t and "لغو توسطِ مشتری" in t
    hist = FCb("bk:hist:0")
    await U.bk_history(hist, env.bot)
    assert "#1 ·" in " ".join(b.text for _t, kb in hist.message.out for r in kb.inline_keyboard for b in r)
    # «نوبت‌هایِ من» (نوبتِ فعال) برچسبِ کد دارد
    await _book_flow(env, hm=600)
    my = FCb("bk:my")
    await U.bk_my(my, env.bot)
    labels = " ".join(b.text for _t, kb in my.message.out for r in kb.inline_keyboard for b in r)
    assert "#2 ·" in labels and "bk:trk" in my.buttons() and "bk:hist:0" in my.buttons()


@pytest.mark.asyncio
async def test_notifications_and_wallet_notes_use_the_code_not_the_internal_id(env):
    await _other_bot_with_appts(env, 5)                                          # id داخلی ≠ کد
    await _setup(env)
    await _book_flow(env)
    ap = (await _appts(env))[0]
    assert ap.id == 6 and ap.seq == 1
    await _pay(env, ap.id)
    assert "#1" in env.notified[-1][0] and "#6" not in env.notified[-1][0]
    async with env.mk() as s:
        notes = [t.note for t in (await s.scalars(select(models.WalletTxn))).all()]
    assert notes == ["بیعانهٔ نوبت #1"]
    card = FCb(f"bka:a:{ap.id}:up:0", OWNER)
    await ADM.bka_view(card, env.bot)
    assert "نوبت #1" in card.texts() and "#6" not in card.texts()


# ───────────── ادمین / منو ─────────────

@pytest.mark.asyncio
async def test_admin_search_by_code_and_list_labels(env):
    await _other_bot_with_appts(env, 2)
    await _book_flow(env)
    hub = FCb("bka:ap", OWNER)
    await ADM.bka_hub_appts(hub, env.bot, fsm(OWNER))
    assert "bka:code" in hub.buttons()
    st = fsm(OWNER)
    await ADM.bka_code_start(FCb("bka:code", OWNER), env.bot, st)
    bad = FMsg(OWNER, "x")
    await ADM.bka_code_in(bad, env.bot, st)
    assert "فقط یک عدد" in bad.out[-1][0]
    miss = FMsg(OWNER, "55")
    await ADM.bka_code_start(FCb("bka:code", OWNER), env.bot, st)
    await ADM.bka_code_in(miss, env.bot, st)
    assert "پیدا نشد" in miss.out[-1][0]
    await ADM.bka_code_start(FCb("bka:code", OWNER), env.bot, st)
    hit = FMsg(OWNER, "۱")
    await ADM.bka_code_in(hit, env.bot, st)
    assert "نوبت #1" in hit.out[-1][0] and "علی رضایی" in hit.out[-1][0]
    lst = FCb("bka:l:pend:0", OWNER)
    await ADM.bka_list(lst, env.bot)
    assert any("#1 ·" in b.text for _t, kb in lst.message.out for r in kb.inline_keyboard for b in r)
    # غیرِ ادمین
    no = FMsg(9999, "1")
    await ADM.bka_code_in(no, env.bot, fsm(9999))
    assert not no.out


def test_menu_action_and_wizard_and_generic_track_order_hidden_for_booking():
    import platform_bot as pb
    assert tenant._SIMPLE_ACTIONS["book_track"][2] == frozenset({"booking"})
    kb = pb._action_kb_base("booking")
    assert "act:book_track" in [b.callback_data for r in kb.inline_keyboard for b in r]
    for f in pb.FIELDS:
        if f != "booking":
            assert "act:book_track" not in [b.callback_data for r in pb._action_kb_base(f).inline_keyboard for b in r]
    assert "book_track" in pb.ACTION_TITLE

    class MB:
        id = 1
        action = None
    def datas(field):
        out = []
        for cat in ("sell", "tool", "action"):
            try:
                kb = tenant._thb_kb_action_pick(MB(), bot_field=field, plan="pro", category=cat)
            except Exception:      # noqa: BLE001
                continue
            out += [b.callback_data or "" for r in kb.inline_keyboard for b in r]
        return out
    b = datas("booking")
    assert any(d.startswith("thb:setsimple:book_track:") for d in b)
    assert not any(d.startswith("thb:setsimple:track_order:") for d in b)
    assert any(d.startswith("thb:setsimple:track_order:") for d in datas("virtual_service"))      # برایِ بقیه سرِ جایش
