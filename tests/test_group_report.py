"""PATCH_GROUP_REPORT_V1 — گزارش اعضا در گروه محافظ‌شدهٔ ربات اصلی."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import ForceReply
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_group_guard as GG
import platform_group_money as GM
import platform_group_report as PR

CHAT = -1001234567890
ADMIN = 1
REPORTER, TARGET, TARGET2, GADMIN = 101, 202, 203, 900


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PR, GG):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(PR, "PLATFORM_ADMIN_ID", ADMIN)
    store = {"platform_group_report_enabled": "on", "group_guard_warn_threshold": "3", "group_guard_mute_default_min": "10"}

    async def pset(key, default=""):
        return store.get(key, default)

    async def allowed(chat_id):
        return chat_id == CHAT
    monkeypatch.setattr(PR, "_pset", pset)
    monkeypatch.setattr(PR, "_group_allowed", allowed)
    monkeypatch.setattr(GG, "get_pset", pset)
    logs = []

    async def log_action(text):
        logs.append(text)
    monkeypatch.setattr(GG, "_log_action", log_action)

    async def is_admin(bot, chat_id, uid):
        return uid in (GADMIN, ADMIN)
    monkeypatch.setattr(GG, "_is_group_admin", is_admin)
    yield SimpleNamespace(maker=maker, store=store, logs=logs)
    await engine.dispose()


class FakeBot:
    def __init__(self, fail_admin=False, ban_status="kicked"):
        self.calls, self._n, self.fail_admin, self.ban_status = [], 1000, fail_admin, ban_status

    def _mid(self):
        self._n += 1
        return self._n

    async def send_message(self, chat_id, text, **kw):
        if self.fail_admin and chat_id == ADMIN:
            raise RuntimeError("blocked by user")
        self.calls.append(("send", chat_id, text, kw))
        return SimpleNamespace(message_id=self._mid())

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", chat_id, message_id, text, kw))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))

    async def restrict_chat_member(self, chat_id, uid, **kw):
        self.calls.append(("restrict", chat_id, uid, kw))

    async def ban_chat_member(self, chat_id, uid):
        self.calls.append(("ban", chat_id, uid))
        return True

    async def get_chat_member(self, chat_id, uid):
        return SimpleNamespace(status=self.ban_status)

    def of(self, kind, chat=None):
        return [c for c in self.calls if c[0] == kind and (chat is None or c[1] == chat)]


def _user(uid, bot=False):
    return SimpleNamespace(id=uid, full_name=f"u{uid}", username=f"u{uid}", is_bot=bot)


def _msg(text, uid=REPORTER, reply_to=-1, chat=CHAT, msg_id=50, anon=False):
    replies = []

    async def reply(t, **kw):
        replies.append((t, kw))
        return SimpleNamespace(message_id=7000 + len(replies))
    if reply_to == -1:
        reply_to = SimpleNamespace(message_id=33, text="متن توهین‌آمیز", caption=None, content_type="text", from_user=_user(TARGET),
                                   forum_topic_created=None, is_automatic_forward=False, sender_chat=None)
    m = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat), message_id=msg_id, reply_to_message=reply_to, reply=reply,
                        from_user=_user(uid), sender_chat=SimpleNamespace(id=-5) if anon else None)
    return m, replies


def _cb(data, uid, answers=None, edits=None):
    answers = answers if answers is not None else []
    edits = edits if edits is not None else []

    async def answer(t="", show_alert=False):
        answers.append((t, show_alert))

    async def edit_text(t, **kw):
        edits.append((t, kw.get("reply_markup")))
    return SimpleNamespace(data=data, from_user=_user(uid), answer=answer,
                           message=SimpleNamespace(edit_text=edit_text, message_id=1)), answers, edits


async def rows(db):
    async with db.maker() as s:
        return (await s.scalars(select(models.GroupReport).order_by(models.GroupReport.id))).all()


def test_parse_report_command():
    p = PR.parse_report_command
    assert p("گزارش") == (True, None) and p("  report ") == (True, None) and p("/report") == (True, None)
    assert p("گزارش‌") == (True, None)                                      # با نیم‌فاصله
    assert p("/report فحش داد") == (True, "فحش داد") and p("/گزارش اسپم") == (True, "اسپم")
    assert p("گزارش روزانه را بفرستید") is None and p("لطفا گزارش") is None and p("") is None and p(None) is None


def test_message_link():
    assert PR.message_link(CHAT, 33) == "https://t.me/c/1234567890/33"
    assert PR.message_link(-12345, 33) is None and PR.message_link(CHAT, 0) is None


@pytest.mark.asyncio
async def test_create_report_rate_limit_and_duplicates(db):
    a, b = (REPORTER, "r", "r"), (TARGET, "t", "t")
    row, err = await PR.create_report(CHAT, a, b, 10, "x", 1)
    assert row and not err
    dup, err = await PR.create_report(CHAT, a, b, 10, "x", 2)
    assert dup is None and "قبلا" in err
    for mid in range(11, 11 + PR.MAX_PER_HOUR - 1):
        r, err = await PR.create_report(CHAT, a, b, mid, "x", mid)
        assert r and not err
    r, err = await PR.create_report(CHAT, a, b, 99, "x", 99)
    assert r is None and "حداکثر" in err
    r, err = await PR.create_report(CHAT, (REPORTER + 1, "o", "o"), b, 10, "x", 1)           # گزارش‌دهندهٔ دیگر مجاز است
    assert r and not err


@pytest.mark.asyncio
async def test_cancelled_report_can_be_filed_again(db):
    a, b = (REPORTER, "r", "r"), (TARGET, "t", "t")
    row, _ = await PR.create_report(CHAT, a, b, 10, "x", 1)
    async with db.maker() as s:
        (await s.get(models.GroupReport, row.id)).status = "cancelled"
        await s.commit()
    again, err = await PR.create_report(CHAT, a, b, 10, "x", 2)
    assert again and not err


@pytest.mark.asyncio
async def test_command_creates_draft_and_ack_with_buttons(db):
    bot = FakeBot()
    m, replies = _msg("گزارش")
    await PR.report_command(m, bot)
    r = (await rows(db))[0]
    assert r.status == "draft" and r.reporter_id == REPORTER and r.target_id == TARGET and r.target_message_id == 33
    assert r.message_text == "متن توهین‌آمیز" and r.command_message_id == 50 and r.ack_message_id == 7001
    text, kw = replies[0]
    assert "ثبت شد" in text and "5 دقیقه" in text
    datas = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert datas == [f"grpt:reason:{r.id}", f"grpt:skip:{r.id}", f"grpt:cancel:{r.id}"]
    assert bot.of("send", ADMIN) == []                                                       # هنوز به مدیر نرسیده


@pytest.mark.asyncio
async def test_command_ignored_outside_guard_group_or_when_disabled(db):
    bot = FakeBot()
    m, replies = _msg("گزارش", chat=-100999)
    with pytest.raises(SkipHandler):
        await PR.report_command(m, bot)
    db.store["platform_group_report_enabled"] = "off"
    m, replies = _msg("گزارش")
    with pytest.raises(SkipHandler):
        await PR.report_command(m, bot)
    assert replies == [] and await rows(db) == []


@pytest.mark.asyncio
async def test_invalid_targets_are_refused(db):
    bot = FakeBot()
    cases = [
        (dict(reply_to=None), "ریپلای"),
        (dict(reply_to=SimpleNamespace(message_id=1, text="x", caption=None, content_type="text", from_user=_user(5, bot=True),
                                       forum_topic_created=None, is_automatic_forward=False, sender_chat=None)), "ربات"),
        (dict(reply_to=SimpleNamespace(message_id=1, text="x", caption=None, content_type="text", from_user=_user(REPORTER),
                                       forum_topic_created=None, is_automatic_forward=False, sender_chat=None)), "خودت"),
        (dict(reply_to=SimpleNamespace(message_id=1, text="x", caption=None, content_type="text", from_user=_user(GADMIN),
                                       forum_topic_created=None, is_automatic_forward=False, sender_chat=None)), "ادمین"),
        (dict(anon=True), "ناشناس"),
    ]
    for kw, needle in cases:
        m, replies = _msg("گزارش", **kw)
        await PR.report_command(m, bot)
        assert replies and needle in replies[0][0], (kw, replies)
    assert await rows(db) == []


@pytest.mark.asyncio
async def test_reason_flow_end_to_end(db):
    bot = FakeBot()
    m, _ = _msg("گزارش")
    await PR.report_command(m, bot)
    r = (await rows(db))[0]
    # فقط گزارش‌دهنده دکمه را می‌زند
    cb, answers, _ = _cb(f"grpt:reason:{r.id}", TARGET2)
    await PR.report_ask_reason(cb, bot)
    assert answers[-1][1] is True and bot.of("send", CHAT) == []
    cb, answers, _ = _cb(f"grpt:reason:{r.id}", REPORTER)
    await PR.report_ask_reason(cb, bot)
    prompt = bot.of("send", CHAT)[0]
    assert isinstance(prompt[3]["reply_markup"], ForceReply) and prompt[3]["reply_markup"].selective is True
    assert prompt[3]["reply_parameters"].message_id == 50                                    # ریپلای به پیام «گزارش» خودش ⇒ selective
    pid = (await rows(db))[0].prompt_message_id
    assert pid == 1001
    cb, answers, _ = _cb(f"grpt:reason:{r.id}", REPORTER)                                    # دوباره: پیام تکراری نمی‌فرستد
    await PR.report_ask_reason(cb, bot)
    assert len(bot.of("send", CHAT)) == 1
    # ریپلای شخص دیگر یا ریپلای عادی به ربات ⇒ نادیده
    other = SimpleNamespace(text="سلام", chat=SimpleNamespace(id=CHAT), message_id=60, from_user=_user(TARGET2),
                            reply_to_message=SimpleNamespace(message_id=pid, from_user=_user(5, bot=True)))
    with pytest.raises(SkipHandler):
        await PR.report_reason_reply(other, bot)
    plain = SimpleNamespace(text="سلام", chat=SimpleNamespace(id=CHAT), message_id=61, from_user=_user(REPORTER),
                            reply_to_message=SimpleNamespace(message_id=424242, from_user=_user(5, bot=True)))
    with pytest.raises(SkipHandler):
        await PR.report_reason_reply(plain, bot)
    assert bot.of("send", ADMIN) == []
    # ریپلای گزارش‌دهنده = ثبت دلیل
    reply = SimpleNamespace(text="  فحش می‌دهد و تهدید می‌کند  ", chat=SimpleNamespace(id=CHAT), message_id=62, from_user=_user(REPORTER),
                            reply_to_message=SimpleNamespace(message_id=pid, from_user=_user(5, bot=True)))
    await PR.report_reason_reply(reply, bot)
    adm = bot.of("send", ADMIN)
    assert len(adm) == 1
    text, kb = adm[0][2], adm[0][3]["reply_markup"]
    assert f"#{r.id}" in text and "فحش می‌دهد و تهدید می‌کند" in text and "متن توهین‌آمیز" in text
    assert "u202" in text and "u101" in text and "https://t.me/c/1234567890/33" in text
    assert "گزارش‌های دیگر علیه او: 0" in text
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [
        f"grpa:warn:{r.id}", f"grpa:mute:{r.id}", f"grpa:del:{r.id}", f"grpa:ban:{r.id}", f"grpa:ok:{r.id}"]
    assert {c[2] for c in bot.of("delete", CHAT)} == {pid, 62}                               # پیام «دلیل را بنویس» و پیام دلیل پاک شد
    edit = bot.of("edit", CHAT)[0]
    assert edit[2] == 7001 and "ارسال شد" in edit[3]
    final = (await rows(db))[0]
    assert final.status == "sent" and final.reason == "فحش می‌دهد و تهدید می‌کند" and final.admin_message_id


@pytest.mark.asyncio
async def test_inline_reason_with_slash_sends_immediately(db):
    bot = FakeBot()
    m, replies = _msg("/report اسپم تبلیغاتی")
    await PR.report_command(m, bot)
    assert "ارسال شد" in replies[0][0] and len(bot.of("send", ADMIN)) == 1
    assert "اسپم تبلیغاتی" in bot.of("send", ADMIN)[0][2]
    assert (await rows(db))[0].status == "sent"


@pytest.mark.asyncio
async def test_skip_cancel_and_sweeper(db):
    bot = FakeBot()
    # لغو ⇒ هرگز به مدیر نمی‌رسد
    m, _ = _msg("گزارش", msg_id=51)
    await PR.report_command(m, bot)
    r1 = (await rows(db))[0]
    cb, answers, _ = _cb(f"grpt:cancel:{r1.id}", TARGET2)
    await PR.report_cancel(cb, bot)
    assert answers[-1][1] is True
    cb, answers, _ = _cb(f"grpt:cancel:{r1.id}", REPORTER)
    await PR.report_cancel(cb, bot)
    assert (await rows(db))[0].status == "cancelled" and bot.of("send", ADMIN) == []
    # ارسال بدون دلیل
    m, _ = _msg("گزارش", msg_id=52, reply_to=SimpleNamespace(message_id=34, text="دیگر", caption=None, content_type="text",
                                                           from_user=_user(TARGET), forum_topic_created=None,
                                                           is_automatic_forward=False, sender_chat=None))
    await PR.report_command(m, bot)
    r2 = (await rows(db))[1]
    cb, answers, _ = _cb(f"grpt:skip:{r2.id}", REPORTER)
    await PR.report_skip(cb, bot)
    assert len(bot.of("send", ADMIN)) == 1 and "بدون دلیل" in bot.of("send", ADMIN)[0][2]
    cb, answers, _ = _cb(f"grpt:skip:{r2.id}", REPORTER)                                     # دوباره: دیگر در حال تکمیل نیست
    await PR.report_skip(cb, bot)
    assert answers[-1][1] is True and len(bot.of("send", ADMIN)) == 1
    # جاروب: پیش از مهلت هیچ، بعد از مهلت یک‌بار
    m, _ = _msg("گزارش", msg_id=53, reply_to=SimpleNamespace(message_id=35, text="سوم", caption=None, content_type="text",
                                                           from_user=_user(TARGET2), forum_topic_created=None,
                                                           is_automatic_forward=False, sender_chat=None))
    await PR.report_command(m, bot)
    assert await PR.report_sweep_once(bot) == 0
    async with db.maker() as s:
        r3 = (await s.scalars(select(models.GroupReport).where(models.GroupReport.status == "draft"))).one()
        r3.created_at = datetime.utcnow() - timedelta(seconds=PR.REASON_WINDOW_SECS + 5)
        await s.commit()
    assert await PR.report_sweep_once(bot) == 1 and await PR.report_sweep_once(bot) == 0
    assert len(bot.of("send", ADMIN)) == 2 and "بدون دلیل" in bot.of("send", ADMIN)[1][2]


@pytest.mark.asyncio
async def test_submit_is_single_shot_and_admin_unreachable_is_recorded(db):
    bot = FakeBot()
    row, _ = await PR.create_report(CHAT, (REPORTER, "r", "r"), (TARGET, "t", "t"), 10, "x", 1)
    assert (await PR.submit_report(row.id, "دلیل", bot)).status == "sent"
    assert await PR.submit_report(row.id, "دلیل", bot) is None and len(bot.of("send", ADMIN)) == 1
    bad = FakeBot(fail_admin=True)
    row2, _ = await PR.create_report(CHAT, (REPORTER, "r", "r"), (TARGET, "t", "t"), 11, "x", 2)
    out = await PR.submit_report(row2.id, "دلیل", bad)
    assert out.status == "failed" and (await rows(db))[1].status == "failed"


async def _sent_report(db, bot, reason="دلیل"):
    row, _ = await PR.create_report(CHAT, (REPORTER, "r", "r"), (TARGET, "t", "t"), 33, "پیام بد", 1)
    await PR.submit_report(row.id, reason, bot)
    return row.id


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _admin_reply(text, prompt_id, uid=ADMIN):
    out = []

    async def reply(t, **kw):
        out.append(t)
    return SimpleNamespace(text=text, from_user=_user(uid), chat=SimpleNamespace(id=uid, type="private"),
                           reply_to_message=SimpleNamespace(message_id=prompt_id), reply=reply), out


def _last_prompt(bot):
    p = [c for c in bot.of("send", ADMIN) if isinstance(c[3].get("reply_markup"), ForceReply)]
    return p[-1]


def _group_notice(bot):
    return [c for c in bot.of("send", CHAT)][-1]


@pytest.mark.asyncio
async def test_warn_asks_reason_then_announces_in_group_with_glass_counters(db):
    """PATCH_REPORT_ACTION_REASON_V1: اخطار ⇒ پرسیدنِ علت ⇒ اجرا ⇒ اعلام در گروه + دکمه‌هایِ شیشه‌ایِ اخطار/تا بن/گزارش‌ها."""
    PR._PENDING.clear()
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, answers, edits = _cb(f"grpa:warn:{rid}", 999)
    await PR.report_admin_action(cb, bot)
    assert answers[-1][1] is True and not edits                                              # غیر مدیر
    cb, answers, edits = _cb(f"grpa:warn:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    async with db.maker() as s:
        assert await s.scalar(select(models.GroupGuardWarn)) is None                          # هنوز اجرا نشده؛ منتظرِ علت
    assert f"grpa:go:{rid}:warn:0" in _datas(edits[-1][1]) and f"grpa:back:{rid}" in _datas(edits[-1][1])
    prompt = _last_prompt(bot)
    assert "علتِ اخطار" in prompt[2] and prompt[1] == ADMIN
    m, out = _admin_reply("فحاشی در گروه", bot._n)                                           # آخرین شناسهٔ ساخته‌شده = پیامِ prompt
    await PR.report_admin_reason_reply(m, bot)
    async with db.maker() as s:
        w = await s.scalar(select(models.GroupGuardWarn))
        assert w.count == 1 and "فحاشی در گروه" in w.last_reason and f"گزارش #{rid}" in w.last_reason
    assert out and "اخطار ثبت شد (1/3)" in out[-1]
    n = _group_notice(bot)
    assert n[1] == CHAT and "اخطار داده شد" in n[2] and "گزارش #" in n[2] and "علتِ تخلف: فحاشی در گروه" in n[2]
    labels = [b.text for row in n[3]["reply_markup"].inline_keyboard for b in row]
    assert any("اخطارها: ۱ از ۳" in x for x in labels) and any("گزارش‌ها: ۱" in x for x in labels)
    assert any("تا بن: ۲ اخطارِ دیگر" in x for x in labels)
    assert set(_datas(n[3]["reply_markup"])) == {"grpn:noop"}                                 # نمایشی
    assert n[3]["reply_parameters"].message_id == 33                                         # ریپلای به پیامِ متخلف
    assert (await rows(db))[0].admin_reason == "فحاشی در گروه"
    assert f"grpa:warn:{rid}" not in _datas(bot.of("edit", ADMIN)[-1][4]["reply_markup"])     # اقدام از کیبورد رفت
    assert "علتِ اقدام: فحاشی در گروه" in bot.of("edit", ADMIN)[-1][3]
    assert PR._PENDING == {}


@pytest.mark.asyncio
async def test_mute_picks_duration_then_reason_skip_uses_reporter_reason(db):
    PR._PENDING.clear()
    bot = FakeBot()
    rid = await _sent_report(db, bot, reason="اسپم")
    cb, answers, edits = _cb(f"grpa:mute:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert _datas(edits[-1][1])[:4] == [f"grpa:dur:{rid}:{m}" for m in (10, 60, 360, 1440)]
    assert bot.of("restrict") == []                                                          # هنوز سکوتی نیست
    cb, answers, edits = _cb(f"grpa:dur:{rid}:999", ADMIN)                                   # مدتِ نامعتبر
    await PR.report_admin_action(cb, bot)
    assert answers[-1][1] is True and bot.of("restrict") == []
    cb, answers, edits = _cb(f"grpa:dur:{rid}:60", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert f"grpa:go:{rid}:mute:60" in _datas(edits[-1][1]) and "سکوتِ ۶۰ دقیقه" in _last_prompt(bot)[2]
    cb, answers, edits = _cb(f"grpa:go:{rid}:mute:60", ADMIN)                                # «بدون دلیل»
    await PR.report_admin_action(cb, bot)
    r = bot.of("restrict")[0]
    assert r[1] == CHAT and r[2] == TARGET and r[3]["permissions"].can_send_messages is False
    delta = (r[3]["until_date"] - datetime.now()).total_seconds() / 60
    assert 59 <= delta <= 61
    n = _group_notice(bot)
    assert "به مدتِ <b>۶۰ دقیقه</b> سکوت شد" in n[2] and "علتِ تخلف: اسپم" in n[2]               # علتِ گزارش‌دهنده جایگزین
    assert PR._PENDING == {}                                                                 # پیامِ prompt پاک شد
    assert (await rows(db))[0].actions == "mute" and (await rows(db))[0].admin_reason is None


@pytest.mark.asyncio
async def test_mute_default_duration_without_choice_is_not_used_and_delete_is_immediate(db):
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, answers, _ = _cb(f"grpa:del:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert (("delete", CHAT, 33)) in bot.calls and not bot.of("send", CHAT)                  # حذفِ پیام: بدونِ علت و اعلام
    assert (await rows(db))[0].actions == "del" and (await rows(db))[0].status == "sent"


@pytest.mark.asyncio
async def test_ban_asks_reason_and_announces_banned(db):
    PR._PENDING.clear()
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, answers, edits = _cb(f"grpa:ban:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert bot.of("ban") == [] and f"grpa:go:{rid}:ban:0" in _datas(edits[-1][1])
    prompt_id = bot._n
    m, out = _admin_reply("تبلیغ مزاحم", prompt_id)
    await PR.report_admin_reason_reply(m, bot)
    assert bot.of("ban") == [("ban", CHAT, TARGET)] and "بن شد" in out[-1]
    async with db.maker() as s:
        b = await s.scalar(select(models.GroupGuardBan))
        assert b.user_id == TARGET and b.banned_by == ADMIN and f"گزارش #{rid}" in b.reason and "تبلیغ مزاحم" in b.reason
    n = _group_notice(bot)
    assert "از گروه بن شد" in n[2] and "علتِ تخلف: تبلیغ مزاحم" in n[2]
    assert any("بن‌شده" in b.text for row in n[3]["reply_markup"].inline_keyboard for b in row)
    assert (await rows(db))[0].status == "closed"
    cb, answers, _ = _cb(f"grpa:warn:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert answers[-1][1] is True                                                            # بسته است


@pytest.mark.asyncio
async def test_back_cancels_pending_prompt_and_nothing_happens(db):
    PR._PENDING.clear()
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, _a, _e = _cb(f"grpa:ban:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    pid = bot._n
    assert pid in PR._PENDING
    cb, answers, edits = _cb(f"grpa:back:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert PR._PENDING == {} and ("delete", ADMIN, pid) in bot.calls and bot.of("ban") == []
    assert f"grpa:ban:{rid}" in _datas(edits[-1][1])
    m, out = _admin_reply("دیر رسید", pid)                                                    # ریپلای به prompt منقضی‌شده ⇒ نادیده
    assert PR._is_admin_reason(m) is False
    m2, _o = _admin_reply("x", 5, uid=999)
    assert PR._is_admin_reason(m2) is False


@pytest.mark.asyncio
async def test_reason_reply_from_non_admin_is_ignored(db):
    PR._PENDING.clear()
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, _a, _e = _cb(f"grpa:warn:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    pid = bot._n
    m, _o = _admin_reply("من ادمین نیستم", pid, uid=999)
    assert PR._is_admin_reason(m) is False


@pytest.mark.asyncio
async def test_failed_ban_changes_nothing(db):
    bot = FakeBot(ban_status="member")                                                       # تلگرام بن را تأیید نکرد
    rid = await _sent_report(db, bot)
    cb, answers, edits = _cb(f"grpa:banyes:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert answers[-1][1] is True and "انجام نشد" in answers[-1][0]
    r = (await rows(db))[0]
    assert r.status == "sent" and r.actions == ""
    async with db.maker() as s:
        assert await s.scalar(select(models.GroupGuardBan)) is None


@pytest.mark.asyncio
async def test_warn_reaching_threshold_auto_bans_announces_ban_and_closes(db):
    PR._PENDING.clear()
    bot = FakeBot()
    db.store["group_guard_warn_threshold"] = "1"
    rid = await _sent_report(db, bot)
    cb, answers, edits = _cb(f"grpa:go:{rid}:warn:0", ADMIN)
    await PR.report_admin_action(cb, bot)
    assert "بن شد" in answers[-1][0] and bot.of("ban") == [("ban", CHAT, TARGET)]
    r = (await rows(db))[0]
    assert r.status == "closed" and "ban" in r.actions
    n = _group_notice(bot)
    assert "به‌خاطر رسیدن به سقفِ اخطارها" in n[2]
    assert any("بن‌شده" in b.text for row in n[3]["reply_markup"].inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_dismiss_closes_and_stats_count_other_reports(db):
    bot = FakeBot()
    rid = await _sent_report(db, bot)
    cb, answers, edits = _cb(f"grpa:ok:{rid}", ADMIN)
    await PR.report_admin_action(cb, bot)
    r = (await rows(db))[0]
    assert r.status == "closed" and r.actions == "ok" and edits[-1][1] is None
    # گزارش بعدی علیه همان نفر، گزارش قبلی را در سوابق می‌شمارد
    row2, _ = await PR.create_report(CHAT, (REPORTER + 5, "x", "x"), (TARGET, "t", "t"), 77, "دوباره", 3)
    await PR.submit_report(row2.id, "باز هم", bot)
    assert "گزارش‌های دیگر علیه او: 1" in bot.of("send", ADMIN)[-1][2]


@pytest.mark.asyncio
async def test_guard_menu_toggle_flips_setting(db, monkeypatch):
    called = []

    async def fake_menu(cb):
        called.append(1)
    store = {}

    async def gp(k, d=""):
        return store.get(k, d)

    async def sp(k, v):
        store[k] = v
    monkeypatch.setattr(GG, "gg_menu", fake_menu)
    monkeypatch.setattr(GG, "get_pset", gp)
    monkeypatch.setattr(GG, "set_pset", sp)
    monkeypatch.setattr(GG, "_require_admin", lambda cb: cb.from_user.id == ADMIN)
    cb, answers, _ = _cb("gg:report_toggle", 999)
    await GG.gg_report_toggle(cb)
    assert "platform_group_report_enabled" not in store and answers[-1][1] is True
    cb, _, _ = _cb("gg:report_toggle", ADMIN)
    await GG.gg_report_toggle(cb)
    assert store["platform_group_report_enabled"] == "off" and called
    await GG.gg_report_toggle(cb)
    assert store["platform_group_report_enabled"] == "on"
