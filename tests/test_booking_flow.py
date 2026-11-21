"""PATCH_BOOKING_V2 — جریانِ کاملِ مشتری + پنلِ مالک روی هندلرهایِ واقعی (با دیتابیسِ حافظه‌ای)."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.booking import bk_admin as ADM
from tenant.bots.booking import bk_user as U
from tenant.bots.booking import engine as E
from tenant.bots.booking import loop as _bk_loop  # noqa: F401  (باید قبل از fixture ایمپورت شود تا Session آن هم جایگزین شود؛ وگرنه test_reminder_loop تنها اجرا که شود «no such table»)

OWNER, CUST, CUST2, BOT = 1, 500, 501, 21
NOW = dt.datetime(2026, 11, 5, 8, 0)           # پنج‌شنبه ۱۴۰۵/۰۸/۱۴
TMR = dt.date(2026, 11, 6)                      # جمعه


class _Sent:
    async def delete(self):
        return None


class FMsg:
    def __init__(self, uid=CUST, text=None, contact=None):
        self.from_user = SimpleNamespace(id=uid, full_name="Ali", username=None)
        self.text, self.contact, self.out = text, contact, []
        self.bot = None

    async def answer(self, text="", **k):
        self.out.append((text, k.get("reply_markup")))
        return _Sent()

    edit_text = answer


class FCb:
    def __init__(self, data, uid=CUST):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=uid, full_name="Ali"), FMsg(uid), []

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)

    def texts(self):
        return "\n".join(t for t, _ in self.message.out)

    def buttons(self):
        out = []
        for _t, kb in self.message.out:
            for row in (kb.inline_keyboard if kb else []):
                out += [b.callback_data for b in row if b.callback_data]
        return out


class FBot:
    token = "1:T"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))


def fsm(uid=CUST):
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))


@pytest_asyncio.fixture
async def env(monkeypatch):
    import sys
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    tenant._SETTING_CACHE.clear()
    monkeypatch.setattr(E, "iran_now", lambda: NOW)
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="1:T", username="salon", field="booking", plan="pro", status="active"))
        s.add(models.BkService(id=1, bot_id=BOT, name="کوتاهی مو", duration_min=60, price=300000, description="شامل شستشو"))
        for wd in range(7):
            s.add(models.BkHours(bot_id=BOT, staff_id=0, weekday=wd, start_min=540, end_min=1020))
        await s.commit()
        row = await s.get(models.Bot, BOT)

    async def _row(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", _row)
    notified = []

    async def _notify(bot, r, text, kb=None, photo_file_id=None):
        notified.append((text, kb))
    monkeypatch.setattr(tenant, "_notify_admins", _notify)

    async def _mk(*a, **k):
        return None
    monkeypatch.setattr(tenant, "main_kb_or_remove", _mk)
    yield SimpleNamespace(mk=mk, notified=notified, bot=FBot())
    await eng.dispose()


async def _book_flow(env, uid=CUST, hm=540):
    st = fsm(uid)
    cb = FCb("bk:s:1", uid)
    await U.bk_pick_service(cb, env.bot)
    ds = TMR.strftime("%Y%m%d")
    assert f"bk:d:1:0:{ds}" in cb.buttons()
    cb = FCb(f"bk:d:1:0:{ds}", uid)
    await U.bk_pick_time(cb, env.bot)
    assert f"bk:h:1:0:{ds}:{hm}" in cb.buttons()
    cb = FCb(f"bk:h:1:0:{ds}:{hm}", uid)
    await U.bk_pick_slot(cb, env.bot, st)
    assert "نام" in cb.texts()
    m = FMsg(uid, "علی رضایی")
    await U.bk_name_in(m, st)
    bad = FMsg(uid, "123")
    await U.bk_phone_text(bad, st)
    assert "معتبر نیست" in bad.out[-1][0]
    ph = FMsg(uid, "۰۹۱۲۳۴۵۶۷۸۹")
    await U.bk_phone_text(ph, st)
    cb = FCb("bk:nonote", uid)
    await U.bk_nonote(cb, env.bot, st)
    assert "bk:confirm" in cb.buttons() and "علی رضایی" in cb.texts()
    cb = FCb("bk:confirm", uid)
    await U.bk_confirm(cb, env.bot, st)
    return cb


@pytest.mark.asyncio
async def test_services_list_and_empty_state(env):
    cb = FCb("book:show")
    await U.bk_start(cb, env.bot)
    assert "کوتاهی مو" in cb.texts() and "300,000" in cb.texts() and "bk:s:1" in cb.buttons()
    async with env.mk() as s:
        for h in (await s.scalars(select(models.BkHours))).all():
            await s.delete(h)
        await s.commit()
    cb = FCb("bk:s:1")
    await U.bk_pick_service(cb, env.bot)
    assert "زمانِ خالی وجود ندارد" in cb.texts()


@pytest.mark.asyncio
async def test_full_booking_flow_admin_confirm_and_customer_cancel(env):
    cb = await _book_flow(env)
    assert "ثبت شد" in cb.texts() and "#" in cb.texts()
    async with env.mk() as s:
        ap = await s.scalar(select(models.BkAppt))
    assert ap.status == "pending" and ap.phone == "09123456789" and ap.customer_name == "علی رضایی" and ap.start_min == 540
    # مالک با مشخصاتِ مشتری و دکمهٔ تأیید/رد خبردار شد
    text, kb = env.notified[-1]
    assert "09123456789" in text and "علی رضایی" in text
    assert {b.callback_data for r in kb.inline_keyboard for b in r} >= {f"bka:ok:{ap.id}", f"bka:no:{ap.id}"}
    # همان ساعت برایِ مشتریِ دوم دیگر نیست
    cb2 = FCb(f"bk:d:1:0:{TMR.strftime('%Y%m%d')}", CUST2)
    await U.bk_pick_time(cb2, env.bot)
    assert f"bk:h:1:0:{TMR.strftime('%Y%m%d')}:540" not in cb2.buttons() and f"bk:h:1:0:{TMR.strftime('%Y%m%d')}:600" in cb2.buttons()
    # تأییدِ مالک ⇒ مشتری خبردار می‌شود
    adm = FCb(f"bka:ok:{ap.id}", OWNER)
    await ADM.bka_act(adm, env.bot)
    assert any(c == CUST and "تأیید شد" in t for c, t in env.bot.sent)
    # نوبت‌هایِ من + لغو (۲۵ ساعت مانده، مهلتِ ۱۲ ساعته)
    my = FCb("bk:my")
    await U.bk_my(my, env.bot)
    assert f"bk:v:{ap.id}" in my.buttons()
    v = FCb(f"bk:v:{ap.id}")
    await U.bk_view(v, env.bot)
    assert f"bk:x:{ap.id}" in v.buttons()
    x = FCb(f"bk:xy:{ap.id}")
    await U.bk_cancel_do(x, env.bot)
    async with env.mk() as s:
        ap = await s.get(models.BkAppt, ap.id)
    assert ap.status == "cancelled"
    assert any("لغو شد" in t for t, _ in env.notified)
    # ساعت دوباره آزاد شد
    cb3 = FCb(f"bk:d:1:0:{TMR.strftime('%Y%m%d')}", CUST2)
    await U.bk_pick_time(cb3, env.bot)
    assert f"bk:h:1:0:{TMR.strftime('%Y%m%d')}:540" in cb3.buttons()


@pytest.mark.asyncio
async def test_customer_cannot_touch_others_and_non_admin_blocked(env):
    await _book_flow(env)
    async with env.mk() as s:
        ap = await s.scalar(select(models.BkAppt))
    other = FCb(f"bk:xy:{ap.id}", CUST2)
    await U.bk_cancel_do(other, env.bot)
    assert other.alerts and "پیدا نشد" in other.alerts[-1]
    # غیرِ ادمین نمی‌تواند تأیید کند
    stranger = FCb(f"bka:ok:{ap.id}", 9999)
    await ADM.bka_act(stranger, env.bot)
    async with env.mk() as s:
        assert (await s.get(models.BkAppt, ap.id)).status == "pending"


@pytest.mark.asyncio
async def test_admin_setup_services_hours_offs_and_settings(env):
    st = fsm(OWNER)
    # خدمتِ جدید (ویزارد)
    await ADM.bka_sv_add(FCb("bka:sva", OWNER), env.bot, st)
    await ADM.bka_sv_name(FMsg(OWNER, "ویزیت"), env.bot, st)
    await ADM.bka_sv_dur(FMsg(OWNER, "۴۵"), env.bot, st)
    await ADM.bka_sv_price(FMsg(OWNER, "۲۰۰٬۰۰۰"), env.bot, st)
    await ADM.bka_sv_desc(FMsg(OWNER, "-"), env.bot, st)
    async with env.mk() as s:
        sv = await s.scalar(select(models.BkService).where(models.BkService.name == "ویزیت"))
    assert sv.duration_min == 45 and sv.price == 200000 and sv.description is None
    # ساعتِ کاری: تنظیمِ سریع شنبه تا چهارشنبه ۱۰–۱۴
    await ADM.bka_hours_quick_days(FCb("bka:hqs:0:a", OWNER), env.bot, st)
    await ADM.bka_quick_start(FMsg(OWNER, "10:00"), env.bot, st)
    await ADM.bka_quick_end(FMsg(OWNER, "14:00"), env.bot, st)
    async with env.mk() as s:
        hs = (await s.scalars(select(models.BkHours).where(models.BkHours.weekday == 0))).all()
        thu = (await s.scalars(select(models.BkHours).where(models.BkHours.weekday == 5))).all()
    assert [(h.start_min, h.end_min) for h in hs] == [(600, 840)] and len(thu) == 1        # جمعه/پنج‌شنبه دست‌نخورده
    # تعطیلی
    await ADM.bka_off_add(FCb("bka:ofa", OWNER), env.bot, st)
    await ADM.bka_off_date(FMsg(OWNER, "۱۴۰۵/۰۸/۱۶"), env.bot, st)       # = 2026-11-07
    await ADM.bka_off_reason(FMsg(OWNER, "تعطیل"), env.bot, st)
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    assert dt.date(2026, 11, 7) not in await E.open_days(BOT, svc, None, cfg, NOW)
    # تنظیمات: تأییدِ خودکار + عدد
    await ADM.bka_cfg_toggle(FCb("bka:cfgt:bk_auto_confirm", OWNER), env.bot, st)
    await ADM.bka_cfg_value(FCb("bka:cfgv:bk_cancel_hours", OWNER), env.bot, st)
    await ADM.bka_cfg_save(FMsg(OWNER, "۶"), env.bot, st)
    tenant._SETTING_CACHE.clear()
    cfg = await E.get_cfg(BOT)
    assert cfg["auto_confirm"] is True and cfg["cancel_h"] == 6
    # داشبورد و آمار بدونِ خطا
    d = FCb("bka:home", OWNER)
    await ADM.bk_dashboard(d, env.bot)
    assert "پنل رزرو" in d.texts()
    s_ = FCb("bka:stats", OWNER)
    await ADM.bka_stats(s_, env.bot)
    assert "آمار" in s_.texts()


@pytest.mark.asyncio
async def test_reminder_loop_sends_once_and_expires_stale_pending(env):
    from tenant.bots.booking import loop as L
    cfg_now = NOW
    async with env.mk() as s:
        s.add(models.BkAppt(bot_id=BOT, tg_id=CUST, service_id=1, service_name="کوتاهی", date_str="2026-11-06", start_min=540,
                            end_min=600, status="confirmed", customer_name="علی", phone="0912"))
        s.add(models.BkAppt(bot_id=BOT, tg_id=CUST2, service_id=1, service_name="x", date_str="2026-11-05", start_min=60,
                            end_min=120, status="pending", customer_name="b", phone="0912"))
        await s.commit()
    sent = []

    class B:
        async def send_message(self, chat_id, text, **k):
            sent.append((chat_id, text))
    now = dt.datetime(2026, 11, 5, 20, 0)                  # ۱۳ ساعت تا نوبتِ جمعه ۹:۰۰ ⇒ یادآوریِ ۲۴ ساعته
    assert await L.run_once(now, bot_factory=lambda row: B()) == 1
    assert sent and sent[0][0] == CUST and "یادآوری" in sent[0][1]
    assert await L.run_once(now, bot_factory=lambda row: B()) == 0                      # دوباره نمی‌فرستد
    async with env.mk() as s:
        stale = await s.scalar(select(models.BkAppt).where(models.BkAppt.tg_id == CUST2))
    assert stale.status == "cancelled"                                                 # نوبتِ منتظرِ گذشته


@pytest.mark.asyncio
async def test_seed_menu_buttons_is_idempotent(env):
    await ADM.bka_seed_buttons(FCb("bka:seedbtn", OWNER), env.bot)
    async with env.mk() as s:
        acts = sorted(b.action for b in (await s.scalars(select(models.MainButton).where(models.MainButton.bot_id == BOT))).all())
    assert acts == ["book_my", "book_show", "book_track"]
    await ADM.bka_seed_buttons(FCb("bka:seedbtn", OWNER), env.bot)
    async with env.mk() as s:
        assert len((await s.scalars(select(models.MainButton).where(models.MainButton.bot_id == BOT))).all()) == 3
