"""🎯 حدس عدد — بازیِ رایگانِ گروهِ بات‌ساز اصلی: فقط گروهِ محافظ، راهنما، برنده/امتیاز، سقفِ سرعت، جدول، خاموش/روشن."""
import asyncio
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_game as G
import platform_group_money as M


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import platform_bot as PB
    import platform_candy as PC
    import halloween as HWm
    for mod in (G, M, PB, PC, HWm):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    HWm._CACHE.update(v=None, t=0.0)
    G._GAMES.clear(); G._LAST_END.clear(); G._LAST_GUESS.clear()
    allowed = {"v": True}

    async def ga(chat_id):
        return allowed["v"] and chat_id == -100
    monkeypatch.setattr(G, "_group_allowed", ga)
    monkeypatch.setattr(G, "GUESS_GAP", 0.0)
    yield allowed
    await eng.dispose()


async def _noop_expire(*a, **k):
    return None


def mk_msg(text, uid=1, chat=-100, name="Ali"):
    sent = []

    async def reply(t, **k):
        sent.append(t)
        return NS(message_id=77)
    m = NS(text=text, chat=NS(id=chat), from_user=NS(id=uid, is_bot=False, full_name=name), sender_chat=None,
           reply=reply, bot=NS())
    m.sent = sent
    return m


def test_parse_guess_digits_and_range():
    assert G.parse_guess("۴۲") == 42 and G.parse_guess(" 7 ") == 7 and G.parse_guess("100") == 100
    assert G.parse_guess("0") is None and G.parse_guess("101") is None and G.parse_guess("abc") is None
    assert G.parse_guess("12 34") is None and G.parse_guess(None) is None


def test_points_fast_bonus():
    assert G.points_for(10) == 6 and G.points_for(31) == 4


@pytest.mark.asyncio
async def test_start_hint_and_win_flow_awards_candies(env, monkeypatch):
    monkeypatch.setattr(G.secrets, "randbelow", lambda n: 41)             # عدد = ۴۲
    monkeypatch.setattr(G, "_expire", _noop_expire)   # مهلت در تست لازم نیست
    m = mk_msg("حدس عدد")
    await G.gg_start(m)
    assert -100 in G._GAMES and "حدس عدد" in m.sent[0]
    g1 = mk_msg("۱۰", uid=2, name="Sara"); await G.gg_guess(g1)
    g2 = mk_msg("90", uid=2, name="Sara"); await G.gg_guess(g2)
    assert g1.sent == ["⬆️ بالاتر"] and g2.sent == ["⬇️ پایین‌تر"]
    w = mk_msg("42", uid=3, name="Omid"); await G.gg_guess(w)
    assert -100 not in G._GAMES and "درست حدس زد" in w.sent[0] and "+6" in w.sent[0]      # سریع ⇒ ۴+۲
    assert (await G.top())[0] == ("Omid", 6)
    t = mk_msg("جدول"); await G.gg_top(t)
    assert "Omid" in t.sent[0]


@pytest.mark.asyncio
async def test_cooldown_and_single_active_game(env, monkeypatch):
    monkeypatch.setattr(G, "_expire", _noop_expire)
    monkeypatch.setattr(G.secrets, "randbelow", lambda n: 4)
    await G.gg_start(mk_msg("/guess"))
    again = mk_msg("حدس عدد"); await G.gg_start(again)
    assert "جریان دارد" in again.sent[0]
    await G.gg_guess(mk_msg("5"))                                         # برنده (۵)
    soon = mk_msg("حدس عدد"); await G.gg_start(soon)
    assert "ثانیهٔ دیگر" in soon.sent[0] and -100 not in G._GAMES


@pytest.mark.asyncio
async def test_other_groups_and_disabled_are_skipped(env, monkeypatch):
    with pytest.raises(SkipHandler):
        await G.gg_start(mk_msg("حدس عدد", chat=-555))                    # گروهِ دیگر
    env["v"] = True
    from platform_bot import set_pset
    await set_pset(G.KEY_ON, "0")
    with pytest.raises(SkipHandler):
        await G.gg_start(mk_msg("حدس عدد"))                               # خاموش
    assert G._is_guess(mk_msg("42")) is False                             # بازیِ فعالی نیست ⇒ فیلتر رد می‌کند


@pytest.mark.asyncio
async def test_admin_toggle_flips_game_switch(env, monkeypatch):
    import platform_halloween as PH
    import halloween as HW
    monkeypatch.setattr(PH, "PLATFORM_ADMIN_ID", 1)
    monkeypatch.setattr(HW, "Session", G.Session)

    async def answer(*a, **k):
        return None

    async def edit_text(t, reply_markup=None):
        return None
    cb = NS(data="adm:halloween:gg", from_user=NS(id=1), answer=answer, message=NS(edit_text=edit_text))
    assert await G.enabled() is True
    await PH.adm_halloween(cb)
    assert await G.enabled() is False
    await PH.adm_halloween(cb)
    assert await G.enabled() is True


# === PATCH_GROUP_RACE_GAMES_V1 ===
def test_math_questions_have_consistent_answers():
    for _ in range(200):
        q, a = G.make_math()
        expr = q.replace("×", "*").replace("−", "-")
        assert str(eval(expr)) == a and int(a) > 0


def test_answer_normalization_is_forgiving():
    assert G._norm_answer(" کدو‌تقلبی ") == G._norm_answer("کدوتقلبی")
    assert G._norm_answer("۱۲") == "12" and G._norm_answer("TrickOrTreat") == G._norm_answer("trickortreat")


@pytest.mark.asyncio
async def test_word_race_first_correct_wins_wrong_is_silent(env, monkeypatch):
    monkeypatch.setattr(G, "_expire_race", _noop_expire)
    monkeypatch.setattr(G.secrets, "choice", lambda seq: "جادوگر")
    m = mk_msg("سرعت")
    await G.gg_speed(m)
    assert G._RACES[-100]["answer"] == "جادوگر" and "جادوگر" in m.sent[0]
    wrong = mk_msg("اسکلت", uid=2)
    assert G._is_race_answer(wrong) is False                                # پاسخِ غلط اصلاً هندلر را فعال نمی‌کند
    win = mk_msg("جادوگر", uid=3, name="Omid")
    assert G._is_race_answer(win) is True
    await G.gg_race_answer(win)
    assert -100 not in G._RACES and "اول شد" in win.sent[0] and "+2" in win.sent[0]
    late = mk_msg("جادوگر", uid=4)
    assert G._is_race_answer(late) is False                                 # بعد از برنده، دیگر جواب نمی‌گیرد
    assert (await G.top())[0] == ("Omid", 2)


@pytest.mark.asyncio
async def test_math_race_fast_bonus_and_one_game_at_a_time(env, monkeypatch):
    monkeypatch.setattr(G, "_expire_race", _noop_expire)
    monkeypatch.setattr(G, "make_math", lambda: ("3 + 4", "7"))
    await G.gg_math(mk_msg("ریاضی"))
    busy = mk_msg("حدس عدد"); await G.gg_start(busy)
    assert "مسابقهٔ سرعت جریان دارد" in busy.sent[0] and -100 not in G._GAMES
    busy2 = mk_msg("سرعت"); await G.gg_speed(busy2)
    assert "جریان دارد" in busy2.sent[0]
    w = mk_msg("۷", uid=5); await G.gg_race_answer(w)
    assert "+3" in w.sent[0]                                                # ریاضیِ سریع ⇒ ۲+۱


# === PATCH_GROUP_GAMES_HELP_V1 ===
@pytest.mark.asyncio
async def test_help_lists_free_games_and_wager_only_when_enabled(env, monkeypatch):
    import platform_wager as W
    G._LAST_HELP.clear()

    async def off():
        return False

    async def on():
        return True

    async def ge(game, bot_id=0):
        return game != "dice"
    monkeypatch.setattr(W, "_bet_enabled", off)
    m = mk_msg("بازی")
    assert G._is_help(m) and G._is_help(mk_msg("بازی‌ها")) and G._is_help(mk_msg("/games"))
    assert not G._is_help(mk_msg("بازی 10000")) and not G._is_help(mk_msg("بازی تاس 5000"))     # دستورِ شرط‌بندی را نمی‌گیرد
    await G.gg_help(m)
    t = m.sent[0]
    assert "حدس عدد" in t and "سرعت" in t and "ریاضی" in t and "جدول" in t and "دونفره" not in t
    G._LAST_HELP.clear()
    monkeypatch.setattr(W, "_bet_enabled", on)
    monkeypatch.setattr(W, "game_enabled", ge)
    m2 = mk_msg("بازی"); await G.gg_help(m2)
    assert "xo 10000" in m2.sent[0] and "تاس 10000" not in m2.sent[0] and "ریسک" in m2.sent[0]


@pytest.mark.asyncio
async def test_help_is_rate_limited_and_group_only(env):
    G._LAST_HELP.clear()
    a = mk_msg("بازی"); await G.gg_help(a)
    b = mk_msg("بازی"); await G.gg_help(b)
    assert len(a.sent) == 1 and b.sent == []                                  # بار دوم بی‌صدا
    with pytest.raises(SkipHandler):
        await G.gg_help(mk_msg("بازی", chat=-555))


# === PATCH_GAMES_GROUP_ALLOW_V1 ===
@pytest.mark.asyncio
async def test_games_allowed_by_guard_chat_even_if_guard_off_or_by_gameson(env, monkeypatch):
    from platform_bot import set_pset
    assert await G.allowed_chat(-777) is False
    await set_pset("group_guard_chat_id", "-777")           # محافظ وصل است، حتی اگر خاموش
    assert await G.allowed_chat(-777) is True and await G.allowed_chat(-888) is False
    await set_pset(G.KEY_CHAT, "-888")
    assert await G.allowed_chat(-888) is True


@pytest.mark.asyncio
async def test_gameson_is_admin_only_and_blocked_hint_for_admin(env, monkeypatch):
    import config
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 77)
    sent = []

    async def reply(t, **k):
        sent.append(t)

    def msg(text, uid, chat=-555):
        return NS(text=text, chat=NS(id=chat), from_user=NS(id=uid, is_bot=False, full_name="x"), sender_chat=None, reply=reply, bot=NS())
    with pytest.raises(SkipHandler):
        await G.gg_admin_switch(msg("/gameson", 5))                          # غیرمدیر
    await G.gg_admin_switch(msg("/gameson", 77))
    assert "فعال شد" in sent[-1]
    from platform_bot import get_pset
    assert await get_pset(G.KEY_CHAT, "") == "-555"
    await G.gg_admin_switch(msg("/gamesoff", 77))
    assert await get_pset(G.KEY_CHAT, "x") in ("", "x")
    # گروهِ غیرمجاز: فقط مدیر راهنما می‌بیند
    async def deny(chat_id):
        return False
    monkeypatch.setattr(G, "_group_allowed", deny)
    n = len(sent)
    with pytest.raises(SkipHandler):
        await G.gg_start(msg("حدس عدد", 5, chat=-999))
    assert len(sent) == n
    with pytest.raises(SkipHandler):
        await G.gg_start(msg("حدس عدد", 77, chat=-999))
    assert "/gameson" in sent[-1]


@pytest.mark.asyncio
async def test_gameson_also_turns_main_switch_on_and_off_hint(env, monkeypatch):
    import config
    from platform_bot import set_pset
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 77)
    sent = []

    async def reply(t, **k):
        sent.append(t)

    def msg(text, uid, chat=-555):
        return NS(text=text, chat=NS(id=chat), from_user=NS(id=uid, is_bot=False, full_name="x"), sender_chat=None, reply=reply, bot=NS())
    monkeypatch.setattr(G, "_group_allowed", G.allowed_chat)
    await set_pset(G.KEY_CHAT, "-555")
    await set_pset(G.KEY_ON, "0")
    assert await G.enabled() is False
    with pytest.raises(SkipHandler):
        await G.gg_start(msg("حدس عدد", 5))
    assert not sent                                      # کاربر عادی چیزی نمی‌بیند
    with pytest.raises(SkipHandler):
        await G.gg_start(msg("حدس عدد", 77))
    assert "خاموش" in sent[-1] and "/gameson" in sent[-1]
    await G.gg_admin_switch(msg("/gameson", 77))
    assert await G.enabled() is True
