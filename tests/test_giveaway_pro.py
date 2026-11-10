"""PATCH_GIVEAWAY_PRO_V1 — قرعه‌کشیِ حرفه‌ای: موتور (ظرفیت/حداقل)، ابزارها، کارت، منو، ویزارد ۹ مرحله‌ای، شرکت، مدیریت، پروفایل."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.giveaway import announce as A
from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway import pro as P
from tenant.bots.giveaway import user as USR

pytestmark = pytest.mark.asyncio
BOT, OWNER = 5, 100
ROW = SimpleNamespace(id=BOT, owner_id=OWNER, field="vpn_sales", token="5:T", plan="partner", status="active", username="gwbot")


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.members, self.bad = [], [], {}, set()

    async def get_me(self):
        return SimpleNamespace(id=999, username="gwbot")

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.bad:
            raise RuntimeError("forbidden")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1000 + len(self.sent), chat=SimpleNamespace(id=chat_id))

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    async def get_chat(self, target):
        if target in ("@nope", -100999):
            raise RuntimeError("not found")
        return SimpleNamespace(id=-1001234 if isinstance(target, str) else target, title="چنل", username=str(target).lstrip("@") if isinstance(target, str) else None, type="channel")

    async def get_chat_member(self, chat_id, uid):
        return SimpleNamespace(status=self.members.get((chat_id, uid), "member"))


class FakeMsg:
    def __init__(self, chat_id=1):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = 50
        self.edits, self.answers = [], []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return FakeMsg(self.chat.id)

    async def delete(self):
        return None


def cb(data, uid=11, premium=False):
    alerts = []

    async def answer(text="", show_alert=False, **kw):
        alerts.append((text, show_alert))

    c = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"n{uid}", is_premium=premium),
                        message=FakeMsg(uid), answer=answer)
    c.alerts = alerts
    return c


def tmsg(text, uid=OWNER):
    out = []

    async def answer(t, **kw):
        out.append((t, kw.get("reply_markup")))
        return FakeMsg(uid)

    m = SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"n{uid}", is_premium=False),
                        chat=SimpleNamespace(id=uid, type="private"), answer=answer)
    m.out = out
    return m


def fsm(uid=OWNER):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def cbs(markup):
    return [b.callback_data or b.url for r in markup.inline_keyboard for b in r]


def labels(markup):
    return [b.text for r in markup.inline_keyboard for b in r]


def alltext(c):
    return " ".join(t for t, _ in c.message.edits) + " ".join(t for t, _ in c.message.answers)


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (E, A, USR, P, tenant):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    USR._LOCK.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=OWNER, token="5:T", username="gwbot", field="vpn_sales", plan="partner", status="active"))
        for uid in (OWNER, 11, 12, 13):
            s.add(models.BotUser(bot_id=BOT, telegram_id=uid, full_name=f"u{uid}", balance=0))
        await s.commit()

    async def get_row(bot):
        return ROW

    async def is_owner_or_admin(bot_id, uid, owner_id):
        return uid == owner_id

    async def check_force_join(bot, bot_id, uid, bypass_cache=False):
        return []

    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    monkeypatch.setattr(tenant, "is_owner_or_admin", is_owner_or_admin)
    monkeypatch.setattr(tenant, "check_force_join", check_force_join)
    monkeypatch.setattr(tenant, "_is_platform_admin", lambda uid: False)
    yield SimpleNamespace(mk=mk)
    await eng.dispose()


# ═══════════ ابزارهای خالص ═══════════

def test_parse_duration():
    f = P.parse_duration_minutes
    assert f("45") == 45 and f("2h") == 120 and f("۲ ساعت") == 120 and f("1d") == 1440 and f("۱ روز") == 1440
    assert f("1d 12h") == 2160 and f("30m") == 30 and f("0") is None and f("abc") is None and f("99999") is None and f("") is None


def test_parse_int_and_bar():
    assert P.parse_int("۱۲۳") == 123 and P.parse_int("1,000") == 1000 and P.parse_int("x") is None and P.parse_int("0", 1) is None
    assert P.progress_bar(5, 10, 10) == "█████░░░░░" and P.progress_bar(0, 0, 4) == "░░░░" and P.progress_bar(99, 10, 4) == "████"


def test_labels_and_i18n():
    assert P.duration_label(60, "fa").endswith("ساعت") and P.duration_label(1440, "en") == "1 day(s)" and P.duration_label(5, "en") == "5 min"
    now = datetime(2026, 1, 1)
    assert "ساعت" in P.left_label(now + timedelta(hours=2, minutes=5), "fa", now) and P.left_label(now - timedelta(1), "fa", now) == "0"
    assert P.tr("fa", "b_lang") == "🌐 Language" and P.tr("en", "b_lang") == "🌐 فارسی"
    assert set(P.T["fa"]) == set(P.T["en"])          # هر کلیدِ فارسی معادلِ انگلیسی دارد


# ═══════════ موتور ═══════════

async def mk_pro(**kw):
    d = dict(title="گیفت", winners_count=1, ends_at=datetime.utcnow() + timedelta(hours=1), pro=True)
    d.update(kw)
    return await E.create_giveaway(BOT, E.NewGiveaway(**d), [E.NewPrize("100 استارز")])


async def test_capacity_full(env):
    g = await mk_pro(capacity=2)
    assert (await E.join(g.id, BOT, 11, "a", "a")).ok and (await E.join(g.id, BOT, 12, "b", "b")).ok
    assert (await E.join(g.id, BOT, 13, "c", "c")).reason == "full"


async def test_min_participants_cancels(env):
    g = await mk_pro(min_participants=3)
    await E.join(g.id, BOT, 11, "a", "a")
    res = await E.draw(g.id, BOT)
    assert not res.ok and res.reason == "min_not_met" and res.participants == 1
    assert (await E.get_giveaway(g.id, BOT)).status == "cancelled"
    g2 = await mk_pro(min_participants=1)
    await E.join(g2.id, BOT, 11, "a", "a")
    assert (await E.draw(g2.id, BOT)).ok


async def test_finish_giveaway_min_not_met_notifies_and_edits(env):
    g = await mk_pro(min_participants=5)
    bot = FakeBot()
    res = await A.finish_giveaway(bot, ROW, g.id, notify_admin_chat=OWNER)
    assert res.reason == "min_not_met" and any(c == OWNER and "لغو" in t for c, t in bot.sent)


async def test_member_check_includes_own_channels(env):
    g = await mk_pro(channels=json.dumps([{"id": -1001, "u": "ch"}]))
    bot = FakeBot()
    chk = await A.make_member_check(bot, ROW, g=g)
    assert chk is not None and await chk(11) is True
    bot.members[(-1001, 11)] = "left"
    assert await chk(11) is False
    assert await A.make_member_check(bot, ROW) is None


# ═══════════ کارت ═══════════

async def test_card_text_pro(env):
    g = await mk_pro(capacity=100, min_participants=10, access="premium", organizer="@owner", winners_count=3,
                     channels=json.dumps([{"id": -1, "u": "Gifty"}]))
    ps = await E.prizes_of(g.id)
    t = P.card_text_pro(g, ps, 25, "fa")
    for needle in ("گیفت", "100 استارز", "ظرفیت", "حداقل", "پریمیوم", "@owner", "@Gifty", "█", "25".translate(P._FA), "باقی‌مانده"):
        assert needle in t, needle
    te = P.card_text_pro(g, ps, 25, "en")
    assert "Capacity" in te and "Time left" in te and "Premium" in te
    from tenant.bots.giveaway.common import card_text
    assert card_text(g, ps, 25) == t                   # اعلانِ گروه/کانال هم کارتِ حرفه‌ای است


# ═══════════ منو ═══════════

async def test_hub_six_buttons_for_admin_and_language_toggle(env):
    c = cb("gwp:home", uid=OWNER)
    await P.gwp_home(c, FakeBot(), fsm())
    t, mk = c.message.edits[0]
    assert cbs(mk)[:7] == ["gwp:new", "gwp:mgr", "gwp:act", "gwp:prof", "gwp:sup", "gwp:lang", "gwp:cfg"]
    assert "🌐 Language" in labels(mk)
    assert "gwp:main" not in cbs(mk)                      # ربات مشتری: دکمهٔ بازگشتِ مادر نیست
    mrow = SimpleNamespace(id=BOT, owner_id=OWNER, status="platform")
    _, mk_m = await P.hub_content(FakeBot(), mrow, OWNER)
    assert cbs(mk_m)[-1] == "gwp:main"
    c2 = cb("gwp:lang", uid=OWNER)
    await P.gwp_lang(c2, FakeBot(), fsm())
    t2, mk2 = c2.message.edits[0]
    assert "🌐 فارسی" in labels(mk2) and "Create new giveaway" in " ".join(labels(mk2)) and "Pro Giveaways" in t2
    assert await P.get_lang(BOT, OWNER) == "en"


async def test_hub_for_regular_user_hides_create_and_manage(env):
    c = cb("gwp:home", uid=11)
    await P.gwp_home(c, FakeBot(), fsm(11))
    cs = cbs(c.message.edits[0][1])
    assert "gwp:new" not in cs and "gwp:mgr" not in cs and "gwp:act" in cs and "gwp:prof" in cs
    c2 = cb("gwp:new", uid=11)
    await P.gwp_new(c2, FakeBot(), fsm(11))
    assert c2.alerts and c2.alerts[0][1] is True


# ═══════════ ویزارد ═══════════

async def run_wizard(bot, st, chips=True):
    c = cb("gwp:new", uid=OWNER)
    await P.gwp_new(c, bot, st)
    m = tmsg("گیفتِ هفته")
    await P.gwp_name_in(m, bot, st)
    for data in ("gwp:v:d:30", "gwp:v:c:100", "gwp:v:w:2", "gwp:v:m:5", "gwp:v:a:premium", "gwp:v:h:0", "gwp:v:r:1", "gwp:v:p:stars"):
        c = cb(data, uid=OWNER)
        await (P.gwp_value(c, bot, st))
    return c


async def test_wizard_full_flow_creates_pro_giveaway(env):
    bot, st = FakeBot(), fsm()
    c = await run_wizard(bot, st)
    gs = await E.active_giveaways(BOT)
    assert len(gs) == 1
    g = gs[0]
    assert g.pro and g.title == "گیفتِ هفته" and g.capacity == 100 and g.winners_count == 2 and g.min_participants == 5
    assert g.access == "premium" and g.ref_bonus == 1 and g.created_by == OWNER and g.organizer == f"@n{OWNER}"
    assert (g.ends_at - datetime.utcnow()) < timedelta(minutes=31)
    assert (await E.prizes_of(g.id))[0].title == "استارزِ تلگرام"
    t = alltext(c)
    assert "ساخته شد" in t and f"gw_{g.id}" in t
    assert f"gwp:post:{g.id}" in cbs(c.message.edits[-1][1])
    assert await st.get_state() is None


async def test_wizard_step_navigation_and_manual_entry(env):
    bot, st = FakeBot(), fsm()
    c = cb("gwp:new", uid=OWNER)
    await P.gwp_new(c, bot, st)
    await P.gwp_name_in(tmsg("x"), bot, st)                 # نامِ کوتاه رد می‌شود
    m = tmsg("نامِ خوب")
    await P.gwp_name_in(m, bot, st)
    c = cb("gwp:man:d", uid=OWNER)
    await P.gwp_manual(c, bot, st)
    m = tmsg("2h")
    await P.gwp_dur_in(m, bot, st)
    assert (await st.get_data())["gwp"]["dur"] == 120
    m = tmsg("abc")
    await P.gwp_dur_in(m, bot, st)                          # هنوز ۱۲۰ (حالتِ دستی تمام شده؛ هندلر فقط در حالتِ dur) 
    c = cb("gwp:bk", uid=OWNER)
    await P.gwp_back(c, bot, st)
    assert (await st.get_data())["gwp"]["step"] == 2
    c = cb("gwp:cx", uid=OWNER)
    await P.gwp_cancel(c, bot, st)
    assert await st.get_state() is None and not await E.active_giveaways(BOT)


async def test_wizard_channels_validated(env):
    bot, st = FakeBot(), fsm()
    await P.gwp_new(cb("gwp:new", uid=OWNER), bot, st)
    await P.gwp_name_in(tmsg("چنل‌دار"), bot, st)
    for d in ("gwp:v:d:30", "gwp:v:c:0", "gwp:v:w:1", "gwp:v:m:0", "gwp:v:a:all", "gwp:v:h:2"):
        await P.gwp_value(cb(d, uid=OWNER), bot, st)
    assert await st.get_state() == P.GwpFSM.chan.state
    m = tmsg("@nope")
    await P.gwp_channel_in(m, bot, st)
    assert "پیدا نشد" in m.out[0][0]
    bot.members[(-1001234, 999)] = "member"                  # ربات ادمین نیست
    m = tmsg("@chan_one")
    await P.gwp_channel_in(m, bot, st)
    assert "ادمین نیست" in m.out[0][0]
    bot.members[(-1001234, 999)] = "administrator"
    m = tmsg("@chan_one")
    await P.gwp_channel_in(m, bot, st)
    assert (await st.get_data())["gwp"]["chs"][0]["u"] == "chan_one"
    m = tmsg("@chan_one")
    await P.gwp_channel_in(m, bot, st)
    assert "قبلاً" in m.out[0][0]


# ═══════════ شرکت ═══════════

async def test_join_blocks_premium_full_and_channels(env):
    g = await mk_pro(access="premium", capacity=1, channels=json.dumps([{"id": -1001, "u": "ch"}]))
    bot, st = FakeBot(), fsm(11)
    c = cb(f"gw:j:{g.id}", uid=11, premium=False)
    await USR.gw_join(c, bot, st)
    assert c.alerts and "پریمیوم" in c.alerts[0][0]
    c = cb(f"gw:j:{g.id}", uid=11, premium=True)
    bot.members[(-1001, 11)] = "left"
    await USR.gw_join(c, bot, st)
    assert c.alerts and "عضو" in c.alerts[0][0] and f"gw:j:{g.id}" in cbs(c.message.edits[0][1])
    bot.members[(-1001, 11)] = "member"
    await E.join(g.id, BOT, 12, "z", "z")
    c = cb(f"gw:j:{g.id}", uid=11, premium=True)
    await USR.gw_join(c, bot, st)
    assert c.alerts and "ظرفیت" in c.alerts[0][0]


async def test_show_card_buttons(env):
    g = await mk_pro(ref_bonus=1)
    c = cb(f"gwp:view:{g.id}", uid=11)
    await P.gwp_view(c, FakeBot(), fsm(11))
    cs = cbs(c.message.edits[0][1])
    assert f"gw:j:{g.id}" in cs and f"gwp:me:{g.id}" in cs and f"gwp:refs:{g.id}" in cs
    await E.join(g.id, BOT, 11, "u", "u")
    c = cb(f"gwp:me:{g.id}", uid=11)
    await P.gwp_me(c, FakeBot())
    assert "شانس" in c.alerts[0][0]


# ═══════════ مدیریت / پروفایل / ارسال ═══════════

async def test_manage_end_cancel_delete(env):
    g = await mk_pro()
    bot = FakeBot()
    c = cb("gwp:mgr", uid=OWNER)
    await P.gwp_manage(c, bot, fsm())
    assert f"gwp:md:{g.id}" in cbs(c.message.edits[0][1])
    c = cb(f"gwp:md:{g.id}", uid=OWNER)
    await P.gwp_manage_detail(c, bot)
    assert f"gwp:end:{g.id}" in cbs(c.message.edits[0][1])
    c = cb(f"gwp:end:{g.id}", uid=11)                       # غیرمالک
    await P.gwp_actions(c, bot)
    assert (await E.get_giveaway(g.id, BOT)).status == "active"
    await E.join(g.id, BOT, 11, "u", "u")
    c = cb(f"gwp:end:{g.id}", uid=OWNER)
    await P.gwp_actions(c, bot)
    assert (await E.get_giveaway(g.id, BOT)).status == "ended"
    g2 = await mk_pro()
    await P.gwp_actions(cb(f"gwp:cn:{g2.id}", uid=OWNER), bot)
    assert (await E.get_giveaway(g2.id, BOT)).status == "cancelled"
    await P.gwp_actions(cb(f"gwp:del:{g2.id}", uid=OWNER), bot)
    assert await E.get_giveaway(g2.id, BOT) is None


async def test_profile_stats(env):
    g = await mk_pro()
    await E.join(g.id, BOT, 11, "u", "u")
    await E.join(g.id, BOT, 12, "v", "v", invited_by=11)
    st = await P.profile_stats(BOT, 11)
    assert st["joins"] == 1 and st["refs"] == 1 and st["wins"] == 0
    c = cb("gwp:prof", uid=11, premium=True)
    await P.gwp_profile(c, FakeBot())
    t = c.message.edits[0][0]
    assert "پریمیوم" in t and "11" in t


async def test_post_to_channel_requires_user_admin(env):
    g = await mk_pro()
    bot, st = FakeBot(), fsm()
    await P.gwp_post_ask(cb(f"gwp:post:{g.id}", uid=OWNER), bot, st)
    assert await st.get_state() == P.GwpFSM.post.state
    bot.members[(-1001234, OWNER)] = "member"
    m = tmsg("@mychan")
    await P.gwp_post_in(m, bot, st)
    assert "ادمین یا مالک" in m.out[0][0]
    bot.members[(-1001234, OWNER)] = "administrator"
    bot.members[(-1001234, 999)] = "administrator"
    m = tmsg("@mychan")
    await P.gwp_post_in(m, bot, st)
    assert "منتشر شد" in m.out[0][0] and bot.sent and bot.sent[0][0] == -1001234


async def test_support_button(env):
    tenant._SETTING_CACHE.clear()
    await tenant.set_setting(BOT, "support_contact", "@helper")
    c = cb("gwp:sup", uid=11)
    await P.gwp_support(c, FakeBot())
    assert "https://t.me/helper" in cbs(c.message.edits[0][1])


def test_offered_only_to_partner_bots():
    assert "gw_pro" in tenant._SIMPLE_ACTIONS
    mb = SimpleNamespace(id=1, action=None, parent_id=None)

    def cbs_for(plan):
        kb = tenant._thb_kb_action_pick(mb, "vpn_sales", plan, "tool")
        return [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "thb:setsimple:gw_pro:1" in cbs_for("partner")
    for plan in ("free", "growth", "pro"):
        assert "thb:setsimple:gw_pro:1" not in cbs_for(plan), plan
    import platform_bot as PB
    for f in ("vpn_sales", "virtual_service", "digital_shop", "booking", "giveaway", "general", "partner"):
        cs = [b.callback_data for r in PB.action_kb(f).inline_keyboard for b in r]
        assert "act:gw_pro" not in cs, f
    assert "gw_pro" not in PB.ACTION_TITLE


async def test_mother_row_and_routing(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "PLATFORM_BOT_TOKEN", "999:MOTHER")
    row = await P.mother_row()
    assert row.status == "platform" and row.token == P.MOTHER_TOKEN and row.id != BOT
    assert (await P.mother_row()).id == row.id                 # idempotent
    mb = SimpleNamespace(token="999:MOTHER")
    assert P.is_mother_bot(mb) and not P.is_mother_bot(SimpleNamespace(token="5:T"))
    assert (await P._row(mb)).id == row.id and (await USR._row(mb)).id == row.id
    assert (await P._row(SimpleNamespace(token="5:T"))).id == BOT


async def test_mother_loops_use_platform_token(env, monkeypatch):
    import config
    from tenant.bots.giveaway import loop as LOOP
    monkeypatch.setattr(config, "PLATFORM_BOT_TOKEN", "999:MOTHER")
    mrow = SimpleNamespace(status="platform", token=P.MOTHER_TOKEN)
    assert LOOP._token_for(mrow) == "999:MOTHER" and LOOP._token_for(ROW) == "5:T" and LOOP._token_for(None) == ""


def test_mother_routers_attach_without_conflict():
    from aiogram import Router
    parent = Router()
    rs = P.mother_routers()
    for r in rs:
        parent.include_router(r)
    parent2 = Router()
    for r in P.mother_routers():              # دوباره هم کپیِ تازه می‌دهد
        parent2.include_router(r)
    assert sum(len(r.callback_query.handlers) for r in rs) > 20


def test_platform_home_button_type_gw_pro():
    import platform_bot as PB
    assert "gw_pro" in PB._HCB_TYPE_LABEL


async def test_platform_home_button_opens_hub_for_mother(env, monkeypatch):
    import platform_bot as PB
    out = []

    async def answer(t, **kw):
        out.append((t, kw.get("reply_markup")))

    msg = SimpleNamespace(bot=FakeBot(), chat=SimpleNamespace(id=OWNER), from_user=SimpleNamespace(id=OWNER), answer=answer)
    monkeypatch.setattr(PB, "PLATFORM_ADMIN_ID", OWNER, raising=False)
    import config
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", OWNER)
    await PB._run_custom_home_btn({"type": "gw_pro", "label": "ق"}, msg, None, None)
    assert out and "gwp:new" in cbs(out[0][1]) and "gwp:lang" in cbs(out[0][1])


async def test_non_partner_tenant_row_blocked(env, monkeypatch):
    other = SimpleNamespace(id=BOT, owner_id=OWNER, field="vpn_sales", token="5:T", plan="pro", status="active")
    async def get_row(bot):
        return other
    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    assert await P._row(FakeBot()) is None
    gw_field = SimpleNamespace(id=BOT, owner_id=OWNER, field="giveaway", token="5:T", plan="free", status="active")
    async def get_row2(bot):
        return gw_field
    monkeypatch.setattr(tenant, "get_bot_row", get_row2)
    assert (await P._row(FakeBot())).field == "giveaway"
