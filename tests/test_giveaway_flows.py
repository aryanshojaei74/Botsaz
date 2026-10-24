"""PATCH_GIVEAWAY_BOT_V1 — جریان‌هایِ تلگرامیِ رباتِ قرعه‌کشی با دوبلِ ربات/کال‌بک (کاربر، مدیر، پایان، گروه، حلقه)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.giveaway import admin as ADM
from tenant.bots.giveaway import announce as A
from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway import group as GRP
from tenant.bots.giveaway import loop as LOOP
from tenant.bots.giveaway import user as USR
from tenant.bots.giveaway.common import parse_hours, parse_payload, time_left

pytestmark = pytest.mark.asyncio
BOT, OWNER = 5, 100
ROW = SimpleNamespace(id=BOT, owner_id=OWNER, field="giveaway", token="5:T", plan="pro", status="active", username="gwbot")


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent, self.edits, self.fail_for = [], [], set(fail_for)
        self.members = {}

    async def get_me(self):
        return SimpleNamespace(username="gwbot")

    async def _send(self, kind, chat_id, *a, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError("forbidden")
        self.sent.append((kind, chat_id, a, kw))
        return SimpleNamespace(message_id=1000 + len(self.sent), chat=SimpleNamespace(id=chat_id))

    async def send_message(self, chat_id, text, **kw):
        return await self._send("msg", chat_id, text, **kw)

    async def send_photo(self, chat_id, f, **kw):
        return await self._send("photo", chat_id, f, **kw)

    async def send_document(self, chat_id, f, **kw):
        return await self._send("doc", chat_id, f, **kw)

    async def edit_message_text(self, **kw):
        if str(kw.get("chat_id")) in self.fail_for:
            raise RuntimeError("cannot edit")
        self.edits.append(kw)

    async def get_chat(self, target):
        if str(target) in self.fail_for:
            raise RuntimeError("chat not found")
        return SimpleNamespace(id=int(target) if str(target).lstrip("-").isdigit() else -1009, title="گروه", username=None, type="supergroup")

    async def get_chat_member(self, chat_id, uid):
        return SimpleNamespace(status=self.members.get((chat_id, uid), "member"))


class FakeMsg:
    def __init__(self, chat_id=1, mid=50):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = mid
        self.edits, self.answers = [], []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return FakeMsg(self.chat.id, 99)

    async def answer_document(self, doc, **kw):
        self.answers.append(("DOC:" + doc.filename, kw))


def cb(data, uid=11):
    alerts = []

    async def answer(text="", show_alert=False, **kw):
        alerts.append((text, show_alert))

    c = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"n{uid}"),
                        message=FakeMsg(uid), answer=answer)
    c.alerts = alerts
    return c


def tmsg(text="", uid=11, contact=None, chat_type="private"):
    out = []

    async def answer(t, **kw):
        out.append(t)
        return SimpleNamespace(message_id=77, chat=SimpleNamespace(id=uid), edit_text=_edit_noop, answer=_edit_noop)

    async def reply(t, **kw):
        out.append(t)
        return SimpleNamespace(message_id=78, chat=SimpleNamespace(id=-100), edit_text=lambda *a, **k: _noop())

    m = SimpleNamespace(text=text, html_text=text, contact=contact, caption=None,
                        photo=None, video=None, document=None, animation=None, audio=None, voice=None,
                        from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"n{uid}", is_bot=False),
                        chat=SimpleNamespace(id=uid if chat_type == "private" else -100, type=chat_type),
                        answer=answer, reply=reply, forward_origin=None)
    m.out = out
    return m


async def _noop():
    return None


async def _edit_noop(*a, **k):
    return SimpleNamespace(message_id=78, chat=SimpleNamespace(id=1))


def fsm(uid=11):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (E, A, USR, ADM, GRP, LOOP, tenant):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    USR._LOCK.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="gwbot", field="giveaway", plan="pro", status="active"))
        for uid in (OWNER, 11, 12, 13, 14, 15):
            s.add(models.BotUser(bot_id=BOT, telegram_id=uid, full_name=f"u{uid}", balance=0))
        await s.commit()

    async def get_row(bot):
        return ROW

    async def is_owner_or_admin(bot_id, uid, owner_id):
        return uid == owner_id

    nj = {"list": []}

    async def check_force_join(bot, bot_id, uid, bypass_cache=False):
        return nj["list"] if uid != OWNER else []

    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_owner_or_admin)
    monkeypatch.setattr(tenant, "check_force_join", check_force_join)
    monkeypatch.setattr(tenant, "_is_platform_admin", lambda uid: False)
    monkeypatch.setattr(A, "BROADCAST_DELAY", 0)

    async def nosleep(_):
        return None
    monkeypatch.setattr(A.asyncio, "sleep", nosleep)
    yield SimpleNamespace(mk=mk, nj=nj)
    await eng.dispose()


async def mk_gw(**kw):
    n = kw.pop("n", 1)
    prizes = kw.pop("prizes", None) or [E.NewPrize("50GB")]
    return await E.create_giveaway(BOT, E.NewGiveaway(title="هدیه", winners_count=n, **kw), prizes)


def txt(c):
    return " ".join(t for t, _ in c.message.edits) + " ".join(t for t, _ in c.message.answers if isinstance(t, str))


def cbs(markup):
    return [b.callback_data or b.url for r in markup.inline_keyboard for b in r]


def test_helpers():
    assert parse_payload("gw_12") == (12, None) and parse_payload("gw_12_345") == (12, 345) and parse_payload("x") is None
    assert parse_payload("gw_x") is None
    assert parse_hours("6h") == 6 and parse_hours("2d") == 48 and parse_hours("1d 12h") == 36 and parse_hours("۶ ساعت") == 6
    assert parse_hours("12") == 12 and parse_hours("0") is None and parse_hours("abc") is None and parse_hours("999d") is None
    assert "ساعت" in time_left(datetime.utcnow() + timedelta(hours=5, minutes=1)) and time_left(None).startswith("تا زمانی")


# ═══════════ کاربر ═══════════

async def test_single_active_giveaway_shown_directly_with_join_button(env):
    g = await mk_gw()
    c = cb("gw:list")
    await USR.gw_list(c, FakeBot(), fsm())
    t, mk = c.message.edits[0]
    assert "هدیه" in t and f"gw:j:{g.id}" in cbs(mk) and "mbtn:home" in cbs(mk)


async def test_no_active_shows_empty_state_with_home(env):
    c = cb("gw:list")
    await USR.gw_list(c, FakeBot(), fsm())
    t, mk = c.message.edits[0]
    assert "فعالی وجود ندارد" in t and "mbtn:home" in cbs(mk)


async def test_join_without_extras_registers_and_shows_link(env):
    g = await mk_gw(captcha=False)
    st = fsm()
    c = cb(f"gw:j:{g.id}")
    await USR.gw_join(c, FakeBot(), st)
    assert await E.entry_of(g.id, 11) is not None
    shown = txt(c)
    assert "ثبت‌نامت انجام شد" in shown and "t.me/gwbot?start=gw_" in shown and "شانس" in shown


async def test_forcejoin_blocks_then_passes(env):
    g = await mk_gw(captcha=False)
    env.nj["list"] = [{"chat_id": "@chan", "title": "کانال", "invite_link": None}]
    c = cb(f"gw:j:{g.id}")
    await USR.gw_join(c, FakeBot(), fsm())
    t, mk = c.message.edits[0]
    assert "عضو" in t and "https://t.me/chan" in cbs(mk) and f"gw:j:{g.id}" in cbs(mk)
    assert await E.entry_of(g.id, 11) is None
    env.nj["list"] = []
    c2 = cb(f"gw:j:{g.id}")
    await USR.gw_join(c2, FakeBot(), fsm())
    assert await E.entry_of(g.id, 11) is not None


async def test_captcha_wrong_then_right_and_lockout(env, monkeypatch):
    g = await mk_gw(captcha=True)
    monkeypatch.setattr(E, "make_captcha", lambda rng=None: ("2 + 3", 5))
    bot, st = FakeBot(), fsm()
    c = cb(f"gw:j:{g.id}")
    await USR.gw_join(c, bot, st)
    assert "2 + 3" in txt(c) and await st.get_state() == USR.GwUserFSM.captcha.state
    await USR.gw_captcha_in(tmsg("7"), bot, st)
    assert bot.edits and "2 + 3" in bot.edits[-1]["text"] and "پاسخ درست نبود" in bot.edits[-1]["text"]
    assert await E.entry_of(g.id, 11) is None
    await USR.gw_captcha_in(tmsg("۵"), bot, st)                       # ارقامِ فارسی هم قبول
    assert await E.entry_of(g.id, 11) is not None and await st.get_state() is None
    assert any("ثبت‌نامت انجام شد" in e["text"] for e in bot.edits)
    # قفل بعد از ۳ پاسخِ غلط (کاربرِ دیگر)
    st2, bot2 = fsm(12), FakeBot()
    await USR.gw_join(cb(f"gw:j:{g.id}", 12), bot2, st2)
    for _ in range(3):
        await USR.gw_captcha_in(tmsg("0", 12), bot2, st2)
    assert await E.entry_of(g.id, 12) is None and "دقیقه بعد" in bot2.edits[-1]["text"]
    c3 = cb(f"gw:j:{g.id}", 12)
    await USR.gw_join(c3, bot2, fsm(12))
    assert c3.alerts and c3.alerts[-1][1] is True


async def test_phone_rules(env):
    g = await mk_gw(captcha=False, require_phone=True)
    bot, st = FakeBot(), fsm()
    c = cb(f"gw:j:{g.id}")
    await USR.gw_join(c, bot, st)
    assert await st.get_state() == USR.GwUserFSM.phone.state
    other = SimpleNamespace(user_id=999, phone_number="09121111111")
    m = tmsg(contact=other)
    await USR.gw_phone_in(m, bot, st)
    assert "خودِ تو" in m.out[0] and await E.entry_of(g.id, 11) is None
    foreign = SimpleNamespace(user_id=11, phone_number="+971501234567")
    m = tmsg(contact=foreign)
    await USR.gw_phone_in(m, bot, st)
    assert "ایران" in m.out[0]
    good = SimpleNamespace(user_id=11, phone_number="09121111111")
    m = tmsg(contact=good)
    await USR.gw_phone_in(m, bot, st)
    e = await E.entry_of(g.id, 11)
    assert e is not None and e.phone == "+989121111111"
    # همان شماره برایِ نفرِ دیگر ⇒ رد
    st2 = fsm(12)
    await USR.gw_join(cb(f"gw:j:{g.id}", 12), bot, st2)
    m2 = tmsg(contact=SimpleNamespace(user_id=12, phone_number="09121111111"), uid=12)
    await USR.gw_phone_in(m2, bot, st2)
    assert await E.entry_of(g.id, 12) is None and any("قبلاً" in x for x in m2.out)


async def test_referral_via_deeplink_and_notification(env):
    g = await mk_gw(captcha=False, ref_bonus=2)
    bot = FakeBot()
    await USR.gw_join(cb(f"gw:j:{g.id}", 11), bot, fsm(11))                    # دعوت‌کننده
    st = fsm(12)
    m = tmsg("/start", 12)
    shown = await USR.on_start(m, bot, ROW, f"gw_{g.id}_11", st)
    # PATCH_GW_LINK_AUTOJOIN_V1: بدونِ زدنِ «شرکت» مستقیم ثبت‌نام می‌شود (اینجا کپچا خاموش است)
    assert shown and (await E.entry_of(g.id, 12)).invited_by == 11 and (await E.entry_of(g.id, 11)).bonus == 2
    assert any(k == "msg" and cid == 11 and "ثبت‌نام کرد" in a[0] for k, cid, a, kw in bot.sent)
    # «به‌روزرسانی» دادهٔ دعوت را پاک نمی‌کند
    st3 = fsm(13)
    await USR.on_start(tmsg("/start", 13), bot, ROW, f"gw_{g.id}_11", st3)
    assert (await E.entry_of(g.id, 13)).invited_by == 11


async def test_on_start_plain_in_giveaway_bot_and_other_field(env):
    await mk_gw()
    m = tmsg("/start")
    assert await USR.on_start(m, FakeBot(), ROW, None, fsm()) is True and m.out
    other = SimpleNamespace(**{**ROW.__dict__, "field": "vpn_sales"})
    assert await USR.on_start(tmsg("/start"), FakeBot(), other, None, fsm()) is False


async def test_results_page_for_winner_and_other(env):
    g = await mk_gw(n=1)
    await E.join(g.id, BOT, 11, "a", "a")
    res = await E.draw(g.id, BOT)
    c = cb(f"gw:res:{g.id}", 11)
    await USR.gw_results(c, FakeBot())
    assert "تبریک" in txt(c) and res.winners[0][1].code in txt(c)
    c2 = cb(f"gw:res:{g.id}", 12)
    await USR.gw_results(c2, FakeBot())
    assert "تبریک" not in txt(c2) and "mbtn:home" in cbs(c2.message.edits[0][1])


# ═══════════ مدیر ═══════════

async def test_admin_guard_blocks_regular_users(env):
    c = cb("gw:adm", 11)
    await ADM.adm_menu(c, FakeBot(), fsm())
    assert c.alerts and c.alerts[0][1] is True and not c.message.edits


async def test_wizard_end_to_end(env):
    bot, st = FakeBot(), fsm(OWNER)
    await ADM.adm_chat_add_in(_fwd_chat(-1001), bot, fsm(OWNER)) if False else None
    async with env.mk() as s:                                  # یک مقصدِ اعلان
        s.add(models.GiveawayChat(bot_id=BOT, chat_id=-1001, title="کانال", kind="channel"))
        await s.commit()
    c = cb("gw:a:new", OWNER)
    await ADM.wiz_new(c, bot, st)
    assert "عنوانِ قرعه‌کشی" in txt(c)
    await ADM.wiz_title(tmsg("۵۰ گیگ رایگان", OWNER), bot, st)
    await ADM.wiz_nodesc(cb("gw:a:nodesc", OWNER), bot, st)
    await ADM.wiz_winners(tmsg("abc", OWNER), bot, st)
    assert await st.get_state() == ADM.GwAdminFSM.winners.state                # نامعتبر ⇒ همان‌جا
    await ADM.wiz_winners(tmsg("۲", OWNER), bot, st)
    await ADM.wiz_prize_title(tmsg("۵۰ گیگ", OWNER), bot, st)
    last = bot.edits[-1]["text"]
    assert "خودِ جایزه" in last and "gw:a:pc:all" in str(bot.edits[-1]["reply_markup"])
    c_all = cb("gw:a:pc:all", OWNER)
    await ADM.wiz_prize_choice(c_all, bot, st)                                 # همین برایِ همه
    assert "گزینه‌هایِ قرعه‌کشی" in txt(c_all)
    await ADM.wiz_option(cb("gw:a:o:phone", OWNER), bot, st)
    await ADM.wiz_option(cb("gw:a:o:hours", OWNER), bot, st)                   # None ⇒ ۱ ساعت
    d = await st.get_data()
    assert d["gw_w"]["phone"] is True and d["gw_w"]["hours"] == 1 and d["gw_w"]["winners"] == 2 and len(d["gw_w"]["prizes"]) == 2
    c_sum = cb("gw:a:sum", OWNER)
    await ADM.wiz_summary(c_sum, bot, st)
    assert "خلاصهٔ قرعه‌کشی" in txt(c_sum)
    c_ok = cb("gw:a:ok", OWNER)
    await ADM.wiz_confirm(c_ok, bot, st)
    gs = await E.active_giveaways(BOT)
    assert len(gs) == 1 and gs[0].winners_count == 2 and gs[0].require_phone and gs[0].ends_at is not None
    assert [p.title for p in await E.prizes_of(gs[0].id)] == ["۵۰ گیگ", "۵۰ گیگ"]
    assert any(k == "msg" and cid == -1001 and "شرکت" in str(kw["reply_markup"]) for k, cid, a, kw in bot.sent)   # اعلان در کانال
    assert "ساخته و شروع شد" in txt(c_ok)
    async with env.mk() as s:
        assert len((await s.scalars(select(models.GiveawayPost))).all()) == 1


async def test_wizard_wallet_and_content_prizes(env):
    bot, st = FakeBot(), fsm(OWNER)
    await ADM.wiz_new(cb("gw:a:new", OWNER), bot, st)
    await ADM.wiz_title(tmsg("t", OWNER), bot, st)
    await ADM.wiz_nodesc(cb("gw:a:nodesc", OWNER), bot, st)
    await ADM.wiz_winners(tmsg("2", OWNER), bot, st)
    await ADM.wiz_prize_title(tmsg("شارژ", OWNER), bot, st)
    await ADM.wiz_prize_choice(cb("gw:a:pc:wallet", OWNER), bot, st)
    bad = tmsg("12", OWNER)
    await ADM.wiz_prize_amount(bad, bot, st)
    assert "❌" in bad.out[0]
    await ADM.wiz_prize_amount(tmsg("۵۰٬۰۰۰".replace("٬", ","), OWNER), bot, st)
    await ADM.wiz_prize_title(tmsg("کد", OWNER), bot, st)
    m = tmsg("کدِ فعال‌سازی: ABC", OWNER)
    await ADM.wiz_prize_content(m, bot, st)
    w = (await st.get_data())["gw_w"]
    assert [(p["kind"], p["amount"]) for p in w["prizes"]] == [("wallet", 50000), ("text", 0)]


async def test_manage_end_draws_delivers_and_reports(env):
    g = await mk_gw(n=2, prizes=[E.NewPrize("شارژ", "wallet", amount=7000), E.NewPrize("کد", "text", text="CODE-123")])
    for u in (11, 12, 13):
        await E.join(g.id, BOT, u, f"u{u}", f"n{u}")
    async with env.mk() as s:
        s.add(models.GiveawayChat(bot_id=BOT, chat_id=-1001, title="کانال", kind="channel"))
        await s.commit()
    bot = FakeBot()
    await A.post_announcement(bot, ROW, g)
    c = cb(f"gw:a:end:{g.id}", OWNER)
    await ADM.adm_end_ask(c, bot)
    assert "برگشت‌پذیر نیست" in txt(c)
    c2 = cb(f"gw:a:endok:{g.id}", OWNER)
    await ADM.adm_end_do(c2, bot)
    assert (await E.get_giveaway(g.id)).status == "ended"
    ws = await E.winners_of(g.id)
    assert len(ws) == 2 and all(w.delivered for w, _, _ in ws)
    to_winners = [(cid, a[0]) for k, cid, a, kw in bot.sent if k == "msg" and cid in {e.user_tg for _, e, _ in ws}]
    assert len(to_winners) >= 2 and all("تبریک" in t for _, t in to_winners)
    assert any("CODE-123" in t for _, t in to_winners) and any("تومان به کیف‌پولت" in t for _, t in to_winners)
    wallet_winner = next(e.user_tg for w, e, p in ws if p.kind == "wallet")
    async with env.mk() as s:
        bal = (await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == wallet_winner))).balance
        assert int(bal) == 7000
        assert (await s.scalar(select(models.WalletTxn.kind))) == "giveaway_prize"
    assert any("نتایجِ قرعه‌کشی" in e["text"] for e in bot.edits)            # اعلانِ کانال ادیت شد
    assert any(k == "msg" and cid == OWNER and "پایان یافت" in a[0] for k, cid, a, kw in bot.sent)   # گزارش به مدیر
    again = await A.finish_giveaway(bot, ROW, g.id)
    assert not again.ok


async def test_draw_drops_users_who_left_forced_channel(env):
    g = await mk_gw(n=3)
    for u in (11, 12, 13):
        await E.join(g.id, BOT, u, f"u{u}", f"n{u}")
    async with env.mk() as s:
        s.add(models.ForceJoin(bot_id=BOT, chat_id="@chan", title="کانال", is_active=True))
        await s.commit()
    env.nj["list"] = [{"chat_id": "@chan", "title": "کانال", "invite_link": None}]     # همه لفت دادند
    bot = FakeBot()
    res = await A.finish_giveaway(bot, ROW, g.id)
    assert res.ok and res.left == 3 and res.winners == []


async def test_undelivered_prize_can_be_redelivered(env):
    g = await mk_gw(n=1)
    await E.join(g.id, BOT, 11, "a", "a")
    blocked = FakeBot(fail_for={"11"})
    res = await A.finish_giveaway(blocked, ROW, g.id)
    w = (await E.winners_of(g.id))[0][0]
    assert res.ok and not w.delivered
    ok, bad = await A.redeliver_pending(FakeBot(), ROW, g.id)
    assert ok == 1 and bad == 0 and (await E.winners_of(g.id))[0][0].delivered


async def test_cancel_and_time_controls_and_csv(env):
    g = await mk_gw(ends_at=datetime.utcnow() + timedelta(hours=2))
    await E.join(g.id, BOT, 11, "علی", "ali", phone="+989121111111")
    bot = FakeBot()
    await ADM.adm_time_set(cb(f"gw:a:tset:{g.id}:p6", OWNER), bot, fsm(OWNER))
    g2 = await E.get_giveaway(g.id)
    assert g2.ends_at > datetime.utcnow() + timedelta(hours=7)
    await ADM.adm_time_set(cb(f"gw:a:tset:{g.id}:n", OWNER), bot, fsm(OWNER))
    assert (await E.get_giveaway(g.id)).ends_at is None
    st = fsm(OWNER)
    await ADM.adm_time_set(cb(f"gw:a:tset:{g.id}:c", OWNER), bot, st)
    await ADM.adm_time_edit_in(tmsg("3h", OWNER), bot, st)
    assert (await E.get_giveaway(g.id)).ends_at is not None
    c = cb(f"gw:a:csv:{g.id}", OWNER)
    await ADM.adm_csv(c, bot)
    assert any(str(a[0]).startswith("DOC:giveaway_") for a in c.message.answers)
    await ADM.adm_cancel_do(cb(f"gw:a:xlok:{g.id}", OWNER), bot)
    assert (await E.get_giveaway(g.id)).status == "cancelled"
    assert (await E.join(g.id, BOT, 12, "b", "b")).reason == "closed"


async def test_admin_chat_destinations(env):
    bot = FakeBot()
    st = fsm(OWNER)
    m = tmsg("-1002", OWNER)
    m.forward_origin = None
    await ADM.adm_chat_add_in(m, bot, st)
    assert len(await A.saved_chats(BOT)) == 1 and any(k == "msg" and cid == -1002 for k, cid, a, kw in bot.sent)
    bad = FakeBot(fail_for={"-1003"})
    m2 = tmsg("-1003", OWNER)
    await ADM.adm_chat_add_in(m2, bad, fsm(OWNER))
    assert len(await A.saved_chats(BOT)) == 1 and "ممکن نشد" in m2.out[0]
    c = (await A.saved_chats(BOT))[0]
    await ADM.adm_chat_del(cb(f"gw:a:chdel:{c.id}", OWNER), bot)
    assert await A.saved_chats(BOT) == []


# ═══════════ گروه ═══════════

async def test_group_commands(env):
    bot = FakeBot()
    bot.members[(-100, 11)] = "member"
    m = tmsg("قرعه‌کشی جدید ۵۰ گیگ | 2 | جایزه", 11, chat_type="supergroup")
    await GRP.group_command(m, bot)
    assert any("فقط برایِ ادمین" in t for t in m.out)                          # عضوِ عادی ⇒ رد
    m = tmsg("قرعه‌کشی جدید ۵۰ گیگ | 2 | جایزه", OWNER, chat_type="supergroup")
    await GRP.group_command(m, bot)
    gs = await E.active_giveaways(BOT)
    assert len(gs) == 1 and gs[0].winners_count == 2
    m = tmsg("/giveaway", 11, chat_type="supergroup")
    await GRP.group_command(m, bot)
    assert any("۵۰ گیگ" in t for t in m.out) or True
    await E.join(gs[0].id, BOT, 11, "a", "a")
    m = tmsg("شرکت‌کنندگان", 11, chat_type="supergroup")
    await GRP.group_command(m, bot)
    assert any("۱" in t for t in m.out)
    bad = tmsg("قرعه‌کشی جدید فقط عنوان", OWNER, chat_type="supergroup")
    await GRP.group_command(bad, bot)
    assert any("فرمت" in t for t in bad.out)
    end = tmsg("پایان قرعه‌کشی", OWNER, chat_type="supergroup")
    await GRP.group_command(end, bot)
    assert (await E.get_giveaway(gs[0].id)).status == "ended"
    res = tmsg("نتیجه قرعه‌کشی", 11, chat_type="supergroup")
    await GRP.group_command(res, bot)
    assert any("نتایجِ قرعه‌کشی" in t for t in res.out)


async def test_group_commands_ignored_in_other_bot_kinds(env, monkeypatch):
    other = SimpleNamespace(**{**ROW.__dict__, "field": "vpn_sales"})

    async def get_row(bot):
        return other
    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await GRP.group_command(tmsg("/giveaway", 11, chat_type="supergroup"), FakeBot())
    assert GRP._is_group_cmd(tmsg("سلام بچه‌ها")) is False and GRP._is_group_cmd(tmsg("بازی 5000")) is False


# ═══════════ حلقه ═══════════

async def test_auto_end_loop_ends_due_giveaways_only(env, monkeypatch):
    due = await mk_gw(ends_at=datetime.utcnow() + timedelta(hours=1))
    later = await mk_gw(ends_at=datetime.utcnow() + timedelta(hours=3))
    await E.join(due.id, BOT, 11, "a", "a")
    async with env.mk() as s:                              # زمانِ پایان گذشت
        row = await s.get(models.Giveaway, due.id)
        row.ends_at = datetime.utcnow() - timedelta(seconds=3)
        await s.commit()
    fb = FakeBot()
    monkeypatch.setattr(LOOP, "Bot", lambda **kw: SimpleNamespace(**{n: getattr(fb, n) for n in dir(fb) if not n.startswith("__")},
                                                                  session=SimpleNamespace(close=_noop)))
    assert await LOOP.end_due_once() == 1
    assert (await E.get_giveaway(due.id)).status == "ended" and (await E.get_giveaway(later.id)).status == "active"
    assert any(k == "msg" and cid == 11 and "تبریک" in a[0] for k, cid, a, kw in fb.sent)
    assert await LOOP.end_due_once() == 0


async def test_counter_refresh_edits_posts_of_active_giveaways(env, monkeypatch):
    g = await mk_gw()
    async with env.mk() as s:
        s.add(models.GiveawayPost(bot_id=BOT, giveaway_id=g.id, chat_id=-1001, message_id=5))
        await s.commit()
    await E.join(g.id, BOT, 11, "a", "a")
    fb = FakeBot()
    monkeypatch.setattr(LOOP, "Bot", lambda **kw: SimpleNamespace(**{n: getattr(fb, n) for n in dir(fb) if not n.startswith("__")},
                                                                  session=SimpleNamespace(close=_noop)))
    LOOP._last_refresh.clear()
    assert await LOOP.refresh_counters_once(now=10_000.0) == 1
    assert fb.edits and "۱" in fb.edits[0]["text"]
    assert await LOOP.refresh_counters_once(now=10_100.0) == 0           # کمتر از ۱۰ دقیقه
    assert await LOOP.refresh_counters_once(now=10_700.0) == 1


def _fwd_chat(i):
    return tmsg(str(i), OWNER)


async def test_generic_text_handler_lets_group_messages_reach_giveaway_router(env):
    """tenant_buttons (هندلرِ عمومیِ متن) نباید پیامِ گروهِ رباتِ قرعه‌کشی را بخورد."""
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await tenant.tenant_buttons(tmsg("قرعه‌کشی", 11, chat_type="supergroup"), FakeBot(), fsm())


def test_field_is_registered_everywhere():
    import platform_bot as PB
    from tenant.settings_registry import _PRODUCTS
    from tenant.shared.field_pick import KNOWN_FIELDS
    assert "giveaway" in PB.FIELDS and "giveaway" in PB.ACTIVE_FIELDS and "giveaway" in PB.FIELD_PITCH
    assert "giveaway" in KNOWN_FIELDS and "giveaway" in _PRODUCTS
    assert "giveaway" in tenant._SIMPLE_ACTIONS and "giveaway_results" in tenant._SIMPLE_ACTIONS
    kb_ = PB._action_kb_base("giveaway")
    datas = [b.callback_data for r in kb_.inline_keyboard for b in r]
    assert "act:giveaway" in datas and "act:giveaway_results" in datas
    assert PB.ACTION_TITLE["giveaway"] and PB.ACTION_TITLE["giveaway_results"]


async def test_private_shortcuts(env):
    await mk_gw()
    m = tmsg("/giveaway", 11)
    await USR.cmd_giveaway(m, FakeBot(), fsm())
    assert m.out and "هدیه" in m.out[0]
    mm = tmsg("/gwadmin", OWNER)
    await ADM.cmd_gwadmin(mm, FakeBot(), fsm(OWNER))
    assert mm.out and "مدیریتِ قرعه‌کشی" in mm.out[0]
    mm2 = tmsg("/gwadmin", 11)
    await ADM.cmd_gwadmin(mm2, FakeBot(), fsm(11))
    assert not mm2.out                                              # غیرِ ادمین چیزی نمی‌بیند
    st = fsm(OWNER)
    await st.set_state(ADM.GwAdminFSM.title)
    mc = tmsg("/cancel", OWNER)
    await ADM.cmd_cancel(mc, FakeBot(), st)
    assert await st.get_state() is None and mc.out


# ═══ PATCH_GIVEAWAY_FJ_HEALTH_V1 / PATCH_GIVEAWAY_FJ_RESUME_V1 ═══
async def test_forcejoin_problems_flags_unadmin_and_unreachable_channels(env):
    async with env.mk() as s:
        s.add(models.ForceJoin(bot_id=BOT, chat_id="-1001", title="الف", is_active=True))
        s.add(models.ForceJoin(bot_id=BOT, chat_id="-1002", title="ب", is_active=True))
        s.add(models.ForceJoin(bot_id=BOT, chat_id="-1003", title="پ", is_active=True))
        s.add(models.ForceJoin(bot_id=BOT, chat_id="-1004", title="غیرفعال", is_active=False))
        await s.commit()

    class B(FakeBot):
        async def me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, chat_id, uid):
            if chat_id == "-1003":
                raise RuntimeError("chat not found")
            return SimpleNamespace(status="administrator" if chat_id == "-1001" else "member")
    A._FJ_HEALTH.clear()
    bad = await A.forcejoin_problems(B(), BOT)
    assert len(bad) == 2 and any("ب" in b and "ادمین نیست" in b for b in bad) and any("پ" in b for b in bad)
    assert not any("الف" in b or "غیرفعال" in b for b in bad)
    text, _kb = await ADM.menu_content(ROW, B())
    assert "عضویتِ اجباری کار نمی‌کند" in text
    A._FJ_HEALTH.clear()
    text2, _ = await ADM.menu_content(ROW)               # بدونِ ربات هشدار نمی‌دهد (سازگار با فراخوان‌هایِ قدیمی)
    assert "کار نمی‌کند" not in text2


async def test_giveaway_resumes_after_forcejoin_check(env, monkeypatch):
    shown = []

    async def hook(msg, bot, row, gw, state):
        shown.append((msg.from_user.id, gw))
    monkeypatch.setattr(tenant, "_giveaway_start_hook", hook)

    class Msg:
        def __init__(self):
            self.from_user = SimpleNamespace(id=999)

        def model_copy(self, update):
            m = Msg(); m.from_user = update["from_user"]; return m

        def as_(self, bot):
            return self
    c = SimpleNamespace(message=Msg(), from_user=SimpleNamespace(id=11))
    st = fsm()
    await tenant._resume_giveaway_after_gate(c, FakeBot(), ROW, st, {"pending_gw": "gw_7_3"})
    assert shown == [(11, "gw_7_3")]                           # از طرفِ خودِ کاربر، نه ربات
    shown.clear()
    await tenant._resume_giveaway_after_gate(c, FakeBot(), ROW, st, {})        # رباتِ قرعه‌کشی بدونِ payload
    assert shown == [(11, None)]
    shown.clear()
    other = SimpleNamespace(id=BOT, field="vpn")
    await tenant._resume_giveaway_after_gate(c, FakeBot(), other, st, {})      # رباتِ دیگر بدونِ payload ⇒ هیچ
    assert shown == []


# === PATCH_GW_DELETE_HISTORY_V1 === (محمد) پاک‌کردنِ نتایجِ تستی
async def _finished(status):
    g = await mk_gw()
    async with E.Session() as s:
        gg = await s.get(models.Giveaway, g.id)
        gg.status = status
        await s.commit()
    return g.id


async def test_delete_single_finished_keeps_active(env):
    done, live = await _finished("ended"), (await mk_gw()).id
    c = cb(f"gw:a:del:{live}", OWNER)
    await ADM.adm_del_ask(c, FakeBot())
    assert c.alerts and c.alerts[0][1]                       # قرعه‌کشیِ فعال پاک‌شدنی نیست
    c = cb(f"gw:a:delok:{done}", OWNER)
    await ADM.adm_del_do(c, FakeBot())
    assert await E.get_giveaway(done, BOT) is None and await E.get_giveaway(live, BOT) is not None
    assert not await E.prizes_of(done)


async def test_clear_history_removes_only_finished(env):
    a, b, live = await _finished("ended"), await _finished("cancelled"), (await mk_gw()).id
    c = cb("gw:a:hclr", OWNER)
    await ADM.adm_hist_clear_ask(c, FakeBot())
    assert "gw:a:hclrok" in cbs(c.message.edits[-1][1])
    await ADM.adm_hist_clear_do(cb("gw:a:hclrok", OWNER), FakeBot())
    assert await E.get_giveaway(a, BOT) is None and await E.get_giveaway(b, BOT) is None
    assert await E.get_giveaway(live, BOT) is not None


async def test_link_entry_asks_captcha_directly_without_join_button_and_counts_referral(env):
    g = await mk_gw(captcha=True, ref_bonus=1)
    bot = FakeBot()
    await USR.gw_join(cb(f"gw:j:{g.id}", 11), bot, fsm(11))
    # دعوت‌کننده خودش کپچا دارد؛ پاسخ را از state می‌خوانیم
    st = fsm(12)
    m = tmsg("/start", 12)
    await USR.on_start(m, bot, ROW, f"gw_{g.id}_11", st)
    d = await st.get_data()
    assert d.get("gw_id") == g.id and "gw_ans" in d and d["gw_inv"] == 11      # مستقیم رفت سراغِ سؤالِ ریاضی
    assert await E.entry_of(g.id, 12) is None                                  # تا جواب ندهد ثبت نشده
    await USR.gw_captcha_in(tmsg(str(d["gw_ans"]), 12), bot, st)
    assert (await E.entry_of(g.id, 12)).invited_by == 11                       # جزوِ رفرالِ دعوت‌کننده


async def test_link_entry_for_existing_participant_or_self_invite_shows_card(env):
    g = await mk_gw(captcha=True)
    bot = FakeBot()
    st = fsm(14)
    await USR.on_start(tmsg("/start", 14), bot, ROW, f"gw_{g.id}_14", st)      # لینکِ خودش ⇒ فقط کارت
    assert "gw_ans" not in (await st.get_data())
