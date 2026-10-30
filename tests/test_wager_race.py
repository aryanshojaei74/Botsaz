"""PATCH_WAGER_RACE_V1 — مسابقه‌هایِ پولیِ دونفره (حدس عدد/سرعت/ریاضی): پول‌سنجی، مهلت، لغو، دسترسی و هم‌زمانی با بازیِ رایگان."""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_game as G
import platform_group_money as M
import platform_wager as W
import platform_wager_race as R

CHAT = -100
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import platform_bot as PB
    for mod in (W, R, M, PB, G):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    async with mk() as s:
        for uid, bal in ((1, 100000), (2, 100000), (3, 500)):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", balance=bal))
        await s.commit()
    R.LIVE.clear(); R._LAST_GUESS.clear(); G._GAMES.clear(); G._RACES.clear()

    async def ga(chat_id):
        return chat_id == CHAT
    monkeypatch.setattr(R, "_group_allowed", ga)
    monkeypatch.setattr(G, "_group_allowed", ga)

    async def on():
        return True
    monkeypatch.setattr(R, "enabled", on)
    monkeypatch.setattr(R, "_bet_enabled", on)
    yield mk
    await eng.dispose()


async def bal(mk, uid):
    async with mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))
        return int(u.balance)


async def make(mk, game="speed", amount=10000):
    w, err = await W.create_wager(game, CHAT, (1, "u1", None), amount, None, CFG)
    assert err is None
    return w


def test_parse_commands():
    assert R.parse_race_command("حدس عدد 10000") == ("guess", 10000)
    assert R.parse_race_command("بازی سرعت ۵۰۰۰") == ("speed", 5000)
    assert R.parse_race_command("شرط بندی ریاضی 2,000") == ("math", 2000)
    assert R.parse_race_command("حدس عدد") is None and R.parse_race_command("سرعت 0") is None
    assert W.parse_command("حدس عدد 10000") is None                  # هسته و ربات‌هایِ مشتری آن را نمی‌شناسند
    assert not set(R.RACE_GAMES) & set(W.GAMES)


@pytest.mark.asyncio
async def test_full_flow_money_is_conserved_and_winner_gets_payout(env):
    mk = env
    w = await make(mk)
    assert await bal(mk, 1) == 90000                                  # رزرو
    r = await R.join_race(w.id, (2, "u2", None))
    assert r.err is None and r.w.status == "playing" and await bal(mk, 2) == 90000
    won = await R.settle_to(w.id, 2)
    assert won is not None and won.status == "settled" and won.winner_id == 2
    assert await bal(mk, 2) == 90000 + 19000 and await bal(mk, 1) == 90000        # ۲×۱۰۰۰۰ − ۵٪ کارمزد
    assert await R.settle_to(w.id, 1) is None                         # دوباره تسویه نمی‌شود
    assert await bal(mk, 1) + await bal(mk, 2) == 200000 - 1000       # فقط کارمزد از سیستم خارج شد


@pytest.mark.asyncio
async def test_timeout_refunds_both(env):
    mk = env
    w = await make(mk, "math")
    await R.join_race(w.id, (2, "u2", None))
    async with mk() as s:
        row = await s.get(models.PlatformWager, w.id)
        row.expires_at = W._now() - timedelta(seconds=1)
        await s.commit()
    done = await W.expire_one(w.id)
    assert done is not None and done.status == "expired"
    assert await bal(mk, 1) == 100000 and await bal(mk, 2) == 100000                   # هر دو کامل برگشت
    text, _ = W.render(done)                                                            # رندرِ هسته به رندرِ مسابقه می‌رود
    assert "برگشت" in text


@pytest.mark.asyncio
async def test_join_rules_creator_poor_user_target_and_cancel(env):
    mk = env
    w = await make(mk)
    assert (await R.join_race(w.id, (1, "u1", None))).err                                 # سازنده نه
    assert "موجودی" in (await R.join_race(w.id, (3, "u3", None))).err and await bal(mk, 3) == 500   # فقیر ⇒ کسر نشد
    r = await W.cancel_wager(w.id, 1)
    assert r.w.status == "cancelled" and await bal(mk, 1) == 100000
    assert (await R.join_race(w.id, (2, "u2", None))).err                                 # لغوشده دیگر پیوستنی نیست


@pytest.mark.asyncio
async def test_non_player_cannot_win_and_answers_are_matched_in_memory(env):
    mk = env
    w = await make(mk, "speed")
    r = await R.join_race(w.id, (2, "u2", None))
    st = json.loads(r.w.state)
    R.LIVE[w.id] = {"wid": w.id, "chat": CHAT, "kind": "speed", "players": {1, 2}, "answer": st["a"], "n": None, "until": 10 ** 12}
    outsider = NS(text=st["q"], chat=NS(id=CHAT), from_user=NS(id=9, is_bot=False))
    wrong = NS(text="چیزِ دیگر", chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False))
    right = NS(text=st["q"], chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False))
    assert R._is_answer(outsider) is False and R._is_answer(wrong) is False and R._is_answer(right) is True
    assert await R.settle_to(w.id, 9) is None                                              # غیرِ بازیکن هرگز


def _gmsg(sent, uid, text):
    async def reply(t, **k):
        sent.append(t)

    async def edit_message_text(*a, **k):
        return None
    bot = NS(edit_message_text=edit_message_text)
    return NS(text=text, chat=NS(id=CHAT), from_user=NS(id=uid, is_bot=False, full_name=f"u{uid}"), reply=reply, bot=bot)


async def _start_guess(mk, monkeypatch):
    monkeypatch.setattr(R, "GUESS_GAP", 0.0)

    async def no_timer(*a, **k):
        return None
    monkeypatch.setattr(R, "_expire_later", no_timer)
    w = await make(mk, "guess")
    r = await R.join_race(w.id, (2, "u2", None))
    n = json.loads(r.w.state)["n"]
    first = r.w.turn_id
    assert first in (1, 2)                                              # قرعه: یکی از دو نفر
    other = 2 if first == 1 else 1
    R.LIVE[w.id] = {"wid": w.id, "chat": CHAT, "bot": 0, "kind": "guess", "players": {1, 2}, "answer": None, "n": n, "until": 10 ** 12}
    return w, n, first, other


async def _row(mk, wid):
    async with mk() as s:
        return await s.get(models.PlatformWager, wid)


@pytest.mark.asyncio
async def test_platform_result_has_glass_buttons_with_both_balances_under_edit_and_winner_reply(env, monkeypatch):
    mk = env
    w, n, first, other = await _start_guess(mk, monkeypatch)
    kb_seen = []

    async def reply(t, **k):
        kb_seen.append((t, k.get("reply_markup")))
    edits = []

    async def edit_message_text(text, **k):
        edits.append(k.get("reply_markup"))
    bot = NS(edit_message_text=edit_message_text)
    async with mk() as s:
        row = await s.get(models.PlatformWager, w.id)
        row.message_id = 55
        await s.commit()
    msg = NS(text=str(n), chat=NS(id=CHAT), from_user=NS(id=other if other == first else first, is_bot=False, full_name="W"),
             reply=reply, bot=bot)
    # برنده = نفرِ نوبت‌دار
    msg.from_user.id = first
    await R.race_answer(msg)
    rows = lambda kb: [[(b.text, b.callback_data) for b in r] for r in kb.inline_keyboard]
    expected = [[("🎁 جایزهٔ برنده", "pgb:info"), ("19,000 تومان", "pgb:info")],
                [("💎 موجودیِ برنده", "pgb:info"), ("109,000 تومان", "pgb:info")],
                [("❌ موجودیِ بازنده", "pgb:info"), ("90,000 تومان", "pgb:info")]]
    assert rows(edits[-1]) == expected                                   # پیامِ بازی ادیت شد
    assert "برنده شد" in kb_seen[-1][0] and rows(kb_seen[-1][1]) == expected   # زیرِ پیامِ «برنده شد» هم


@pytest.mark.asyncio
async def test_guess_is_turn_based_hints_pass_turn_and_win_on_own_turn(env, monkeypatch):
    mk = env
    w, n, first, other = await _start_guess(mk, monkeypatch)
    sent = []
    wrong = n - 1 if n > 1 else n + 1
    await R.race_answer(_gmsg(sent, other, str(wrong)))                  # نوبتِ او نیست
    assert "نوبتِ" in sent[-1] and (await _row(mk, w.id)).turn_id == first
    sent.clear()
    await R.race_answer(_gmsg(sent, other, str(wrong)))                  # پشتِ سر هم ⇒ اخطارِ تکراری ساکت (۸ ثانیه)
    assert sent == []
    await R.race_answer(_gmsg(sent, first, str(wrong)))                  # حدسِ غلطِ نوبت‌دار ⇒ نوبت می‌رود به حریف
    assert sent[-1].startswith("⬆️ بالاتر" if wrong < n else "⬇️ پایین‌تر") and "۳ دقیقه" in sent[-1]
    row = await _row(mk, w.id)
    assert row.turn_id == other and 170 <= (row.expires_at - W._now()).total_seconds() <= 180   # مهلتِ تازهٔ ۳ دقیقه‌ای
    await R.race_answer(_gmsg(sent, first, str(n)))                      # دیگر نوبتِ او نیست ⇒ حتی حدسِ درست بی‌اثر
    assert (await _row(mk, w.id)).status == "playing"
    await R.race_answer(_gmsg(sent, other, str(n)))                      # نوبتِ او + حدسِ درست
    assert "برنده شد" in sent[-1] and w.id not in R.LIVE
    done = await _row(mk, w.id)
    assert done.status == "settled" and done.winner_id == other
    assert await bal(mk, other) == 90000 + 19000


@pytest.mark.asyncio
async def test_guess_idle_turn_holder_forfeits_and_money_is_conserved(env, monkeypatch):
    mk = env
    w, n, first, other = await _start_guess(mk, monkeypatch)
    async with mk() as s:
        row = await s.get(models.PlatformWager, w.id)
        row.expires_at = W._now() - timedelta(seconds=1)               # ۳ دقیقه گذشت و نوبت‌دار حدس نزد
        await s.commit()
    done = await W.expire_one(w.id)
    assert done.status == "settled" and done.end_reason == "forfeit" and done.winner_id == other
    assert await bal(mk, other) == 90000 + 19000 and await bal(mk, first) == 90000
    assert await bal(mk, 1) + await bal(mk, 2) == 200000 - 1000
    text, _ = R.render_race(done)
    assert "در مهلتِ نوبتش حدس نزد" in text and "برنده" in text
    # بازیِ قدیمی بدونِ نوبت (قبل از آپدیت) همچنان برگشتِ هر دو است
    R.LIVE.clear()                                                     # بازیِ اول تمام شده؛ حافظهٔ زنده خالی
    w2 = await make(mk, "guess")
    assert (await R.join_race(w2.id, (2, "u2", None))).err is None
    before = (await bal(mk, 1), await bal(mk, 2))                      # هر دو ۱۰۰۰۰ رزرو کرده‌اند
    async with mk() as s:
        row = await s.get(models.PlatformWager, w2.id)
        row.turn_id, row.expires_at = None, W._now() - timedelta(seconds=1)
        await s.commit()
    legacy = await W.expire_one(w2.id)
    assert legacy.status == "expired" and (await bal(mk, 1), await bal(mk, 2)) == (before[0] + 10000, before[1] + 10000)


@pytest.mark.asyncio
async def test_guess_resumes_from_db_after_restart(env, monkeypatch):
    mk = env
    w, n, first, other = await _start_guess(mk, monkeypatch)
    R.LIVE.clear()                                                       # ری‌استارتِ ربات: حافظه پاک
    R._NO_RACE.clear()
    sent = []
    ok = _gmsg(sent, first, str(n))
    assert R._is_answer(ok)                                              # عددِ ۱–۱۰۰ از فیلتر رد می‌شود
    await R.race_answer(ok)                                              # از DB بازسازی و برنده می‌شود
    assert "برنده شد" in sent[-1] and (await _row(mk, w.id)).winner_id == first
    # کسی که بازیِ زنده ندارد ⇒ SkipHandler (پیام به هندلرِ بعدی می‌رسد) و کشِ منفی
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await R.race_answer(_gmsg([], 7, "50"))
    assert R._NO_RACE[(CHAT, 7, 0)] > 0


@pytest.mark.asyncio
async def test_create_handler_allows_many_races_at_once_and_free_game_runs_beside_them(env, monkeypatch):
    mk = env
    out = []

    async def reply(t, **k):
        out.append(t)
        return NS(message_id=55)
    bot = NS()

    async def sweep(b):
        return 0
    def msg(text, uid=1, chat=CHAT):
        return NS(text=text, chat=NS(id=chat), from_user=NS(id=uid, is_bot=False, full_name=f"u{uid}", username=None),
                  sender_chat=None, reply=reply, bot=bot, reply_to_message=None)
    from aiogram.dispatcher.event.bases import SkipHandler
    with pytest.raises(SkipHandler):
        await R.race_create(msg("سرعت 10000", chat=-5))                                    # گروهِ دیگر
    monkeypatch.setattr(W, "sweep_expired", sweep)
    await R.race_create(msg("سرعت 10000"))
    assert "منتظر حریف" in out[-1] and len(R._live_chat(CHAT)) == 1 and await bal(mk, 1) == 90000
    await R.race_create(msg("حدس عدد 10000", uid=2))                                       # دومی هم ساخته می‌شود
    assert "منتظر حریف" in out[-1] and len(R._live_chat(CHAT)) == 2 and await bal(mk, 2) == 90000
    free = NS(text="حدس عدد", chat=NS(id=CHAT), from_user=NS(id=3, is_bot=False, full_name="u3"), sender_chat=None,
              reply=reply, bot=bot)
    async def noexp(*a, **k):
        return None
    monkeypatch.setattr(G, "_expire", noexp)
    import platform_candy as _PC

    async def _on():
        return True
    monkeypatch.setattr(_PC, "candy_active", _on)                                          # مودِ شیرینی روشن
    await G.gg_start(free)
    assert CHAT in G._GAMES                                                                # بازیِ رایگان وسطِ مسابقهٔ پولی هم شروع می‌شود


@pytest.mark.asyncio
async def test_two_parallel_races_route_answers_by_player(env):
    mk = env
    w1 = await make(mk, "speed")
    r1 = await R.join_race(w1.id, (2, "u2", None))
    st1 = json.loads(r1.w.state)
    R.LIVE[w1.id] = {"wid": w1.id, "chat": CHAT, "kind": "speed", "players": {1, 2}, "answer": st1["a"], "n": None, "until": 10 ** 12}
    # بازیکنِ مسابقهٔ اول نمی‌تواند هم‌زمان به مسابقهٔ دیگری بپیوندد؛ سازنده‌ای که در بازی است هم همین‌طور
    async with mk() as s:
        s.add(models.PlatformUser(telegram_id=4, full_name="u4", balance=100000))
        await s.commit()
    w2, err = await W.create_wager("guess", CHAT, (4, "u4", None), 10000, None, CFG)
    assert err is None
    j = await R.join_race(w2.id, (2, "u2", None))
    assert j.err and "مسابقهٔ فعلی" in j.err
    w3, err = await W.create_wager("math", CHAT, (1, "u1", None), 10000, None, CFG)
    assert err is None
    j2 = await R.join_race(w3.id, (4, "u4", None))
    assert j2.err and "سازنده" in j2.err
    m = lambda t, uid: NS(text=t, chat=NS(id=CHAT), from_user=NS(id=uid, is_bot=False))
    assert R._is_answer(m(st1["q"], 2)) and not R._is_answer(m(st1["q"], 4))
    # بازیکنِ پولی عددش به بازیِ رایگان نمی‌رسد
    G._GAMES[CHAT] = {"n": 50, "t0": 0, "guesses": 0}
    gm = NS(text="50", chat=NS(id=CHAT), from_user=NS(id=2, is_bot=False))
    other = NS(text="50", chat=NS(id=CHAT), from_user=NS(id=9, is_bot=False))
    assert G._is_guess(gm) is False and G._is_guess(other) is True


@pytest.mark.asyncio
async def test_example_amount_follows_min_max_and_race_help(env, monkeypatch):
    assert W.example_amount({"min": 1000, "max": 0}) == 10000
    assert W.example_amount({"min": 50000, "max": 0}) == 50000
    assert W.example_amount({"min": 1000, "max": 5000}) == 5000
    txt = W.help_text({"xo": True, "rps": True, "dice": True}, cfg={"min": 50000, "max": 0})
    assert "۵۰٬۰۰۰" not in txt and "50,000".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) in txt
    sec = await R.help_section()
    assert "حدس عدد" in sec and "سرعت" in sec and "ریاضی" in sec and "تومان" in sec and "هم‌زمان" in sec


@pytest.mark.asyncio
async def test_disabled_flag_skips_handlers(env, monkeypatch):
    async def off():
        return False
    monkeypatch.setattr(R, "enabled", off)
    from aiogram.dispatcher.event.bases import SkipHandler
    m = NS(text="سرعت 10000", chat=NS(id=CHAT), from_user=NS(id=1, is_bot=False), sender_chat=None)
    with pytest.raises(SkipHandler):
        await R.race_create(m)


@pytest.mark.asyncio
async def test_paid_prefix_without_amount_asks_for_amount_and_plain_name_is_free(env):
    out = []

    async def reply(t, **k):
        out.append(t)
        return NS(message_id=1)

    def msg(text, uid=1):
        return NS(text=text, chat=NS(id=CHAT), from_user=NS(id=uid, is_bot=False, full_name=f"u{uid}", username=None),
                  sender_chat=None, reply=reply, bot=NS(), reply_to_message=None)
    for t in ("شرطبندی حدس عدد", "شرط‌بندی حدس عدد", "شرط بندی سرعت", "شرطبندی ریاضی"):
        assert R._is_paid_hint(msg(t)) and not R._is_create(msg(t)) and not G._is_start(msg(t))
        await R.race_paid_hint(msg(t))
        assert "مبلغ" in out[-1] and "شرطبندی" in out[-1] and "رایگان" in out[-1]
    for t in ("حدس عدد", "بازی حدس عدد", "سرعت", "بازی سرعت", "ریاضی", "بازی ریاضی"):
        assert not R._is_paid_hint(msg(t)) and not R._is_create(msg(t))
    assert G._is_start(msg("حدس عدد")) and G._is_start(msg("بازی حدس عدد"))
    assert G._is_speed(msg("بازی سرعت")) and G._is_math(msg("بازی ریاضی"))
    assert R._is_create(msg("شرطبندی حدس عدد 10000")) and not G._is_start(msg("شرطبندی حدس عدد 10000"))


def test_speed_words_do_not_repeat_in_the_same_chat():
    """PATCH_SPEED_WORDS_V1 (فدایی): کلمه‌هایِ سرعت تکراری می‌آمد."""
    import platform_group_game as G
    assert len(G.WORDS) >= 200 and len(set(G.WORDS)) == len(G.WORDS)
    assert all(5 <= G._wlen(w) <= 10 for w in G.WORDS)                 # فدایی: حداقل ۵ حرف
    G._RECENT_WORDS.clear()
    first = [G.pick_word((0, 111)) for _ in range(150)]
    assert len(set(first)) == 150                                   # تا ۱۵۰ بار پشتِ‌هم هیچ تکراری نیست
    other = {G.pick_word((0, 222)) for _ in range(30)}              # گروهِ دیگر حافظهٔ جدا دارد
    assert len(other) == 30
    G._RECENT_WORDS.clear()
    qs = [R.new_question("speed", (7, 333))["q"] for _ in range(100)]
    assert len(set(qs)) == 100                                      # مسیرِ مسابقهٔ پولی هم همان را می‌خواند
