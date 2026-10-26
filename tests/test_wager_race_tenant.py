"""PATCH_WAGER_RACE_TENANT_V1 — مسابقه‌هایِ پولیِ حدس عدد/سرعت/ریاضی در گروهِ ربات‌هایِ مستأجر (کیف‌پولِ همان ربات)."""
import time
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import select

import models
import platform_wager as PW
import platform_wager_race as R
import tenant
import tenant.shared.wager_games as WG
import tenant.shared.wager_race as WR
from tests.test_wager_tenant import (BOT, BOT2, OWNER, U1, U2, U3, _bot, _cb, _msg, activate, bu, db,  # noqa: F401
                                     enable_betting, expire, pu, txns, wager)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    R.LIVE.clear()
    R._LAST_GUESS.clear()
    monkeypatch.setattr(R, "GUESS_GAP", 0.0)
    for mod in (WR, R):
        monkeypatch.setattr(mod, "Session", WR.Session, raising=False)
    yield
    R.LIVE.clear()


class FakeBot(SimpleNamespace):
    def __init__(self, token="5:T", id=5):
        super().__init__(token=token, id=id, edited=[])

    async def edit_message_text(self, text, **kw):
        self.edited.append((text, kw.get("reply_markup")))


async def setup(db, monkeypatch):
    await activate(db)
    await enable_betting()
    # Session در ماژول‌هایِ تازه هم به دیتابیسِ تست وصل شود
    monkeypatch.setattr(WR, "Session", db)
    monkeypatch.setattr(R, "Session", db)

    async def noexp(*a, **k):
        return None
    monkeypatch.setattr(R, "_expire_later", noexp)


@pytest.mark.asyncio
async def test_not_activated_only_owner_is_told(db, monkeypatch):
    await enable_betting()
    monkeypatch.setattr(WR, "Session", db)
    m, replies = _msg("سرعت 5000", uid=U1)
    with pytest.raises(SkipHandler):
        await WR.twr_create(m, FakeBot())
    assert replies == [] and await bu(db, U1) == 100_000
    m, replies = _msg("سرعت 5000", uid=OWNER)
    await WR.twr_create(m, FakeBot())
    assert replies and "فعال نشده" in replies[0]


@pytest.mark.asyncio
async def test_many_races_at_once_use_bot_wallet_and_twr_buttons(db, monkeypatch):
    await setup(db, monkeypatch)
    m1, r1 = _msg("سرعت 5000", uid=U1)
    await WR.twr_create(m1, FakeBot())
    m2, r2 = _msg("شرطبندی حدس عدد ۱۰,۰۰۰", uid=U2)
    await WR.twr_create(m2, FakeBot())
    assert "منتظر حریف" in r1[0] and "منتظر حریف" in r2[0]
    assert "بازی" in r1[0] and "شرط‌بندی" not in r1[0] and "ریسک" in r1[0]               # واژه‌نامهٔ ربات مشتری
    assert await bu(db, U1) == 95_000 and await bu(db, U2) == 90_000 and await pu(db, U1) == 100_000
    assert len(R._live_chat(-100, BOT)) == 2 and R._live_chat(-100, 0) == [] and R._live_chat(-100, BOT2) == []
    async with db() as s:
        ws = (await s.scalars(select(models.PlatformWager).order_by(models.PlatformWager.id))).all()
    assert [w.bot_id for w in ws] == [BOT, BOT] and all(w.message_id == 900 for w in ws)
    kb = R.render_race(ws[0])[1]
    assert [b.callback_data for b in kb.inline_keyboard[0]] == [f"twr:join:{ws[0].id}", f"twr:cancel:{ws[0].id}"]


@pytest.mark.asyncio
async def test_join_answer_win_flow_and_cross_bot_isolation(db, monkeypatch):
    await setup(db, monkeypatch)
    m, _ = _msg("سرعت 10000", uid=U1)
    await WR.twr_create(m, FakeBot())
    async with db() as s:
        w = await s.scalar(select(models.PlatformWager))
    edits = []
    bot = FakeBot()
    await WR.twr_join(_cb(f"twr:join:{w.id}", U1, edits), bot)                       # سازنده نمی‌تواند
    assert edits[-1][0] == "ANSWER" and edits[-1][2] is True
    await WR.twr_join(_cb(f"twr:join:{w.id}", U2, edits), bot)
    assert await bu(db, U2) == 90_000 and bot.edited and (await wager(db, w.id)).status == "playing"
    live = R.LIVE[w.id]
    assert live["bot"] == BOT and live["players"] == {U1, U2}
    # یک نفرِ بیرونی نمی‌تواند وسط بپرد (پیوستنِ همان کاربر به مسابقهٔ دوم هم رد می‌شود)
    m2, _ = _msg("حدس عدد 10000", uid=U3)
    await WR.twr_create(m2, FakeBot())
    async with db() as s:
        w2 = (await s.scalars(select(models.PlatformWager).where(models.PlatformWager.game == "guess"))).one()
    await WR.twr_join(_cb(f"twr:join:{w2.id}", U2, edits), bot)
    assert edits[-1][0] == "ANSWER" and "مسابقهٔ فعلی" in edits[-1][1] and await bu(db, U2) == 90_000
    # پاسخِ غلط/بیرونی بی‌اثر؛ ربات دیگر (BOT2) پاسخ را نمی‌بیند
    ans = live["answer"]
    outsider, _ = _msg(ans, uid=U3)
    assert WR._maybe_answer(outsider) is False
    right, replies = _msg(ans, uid=U2)
    assert WR._maybe_answer(right) is True
    with pytest.raises(SkipHandler):
        await WR.twr_answer(right, FakeBot(token="6:T", id=6))
    assert (await wager(db, w.id)).status == "playing"
    await WR.twr_answer(right, FakeBot())
    done = await wager(db, w.id)
    assert done.status == "settled" and done.winner_id == U2 and w.id not in R.LIVE
    assert await bu(db, U2) == 90_000 + 20_000 and await bu(db, U1) == 90_000          # برنده کلِ ۲۰٬۰۰۰ را گرفت
    assert "برنده شد" in replies[-1]
    kinds = await txns(db)
    assert "wager_reserve" in kinds and "wager_stake" in kinds and len(kinds) >= 4          # دفترِ WalletTxnِ همین ربات


@pytest.mark.asyncio
async def test_guess_hints_and_game_switch_and_hint_for_missing_amount(db, monkeypatch):
    await setup(db, monkeypatch)
    m, r = _msg("بازی ریاضی", uid=U1)
    assert WR._is_hint(m) and not WR._is_create(m)
    await WR.twr_hint(m, FakeBot())
    assert "ریاضی" in r[0] and "1000" in r[0] and "تومان" in r[0]
    await tenant.set_setting(BOT, "wager_game_speed", "off")
    m, r = _msg("سرعت 5000", uid=U1)
    await WR.twr_create(m, FakeBot())
    assert "سرعت" in r[0] and "فعال نیست" in r[0] and await bu(db, U1) == 100_000
    await tenant.set_setting(BOT, "group_betting_enabled", "0")
    m, r = _msg("ریاضی 5000", uid=U1)
    await WR.twr_create(m, FakeBot())
    assert "فعال نیست" in r[0] and await bu(db, U1) == 100_000


@pytest.mark.asyncio
async def test_guess_flow_between_two_players(db, monkeypatch):
    await setup(db, monkeypatch)
    m, _ = _msg("حدس عدد 5000", uid=U1)
    await WR.twr_create(m, FakeBot())
    async with db() as s:
        w = await s.scalar(select(models.PlatformWager))
    edits = []
    await WR.twr_join(_cb(f"twr:join:{w.id}", U2, edits), FakeBot())
    n = R.LIVE[w.id]["n"]
    hi, rp = _msg(str(min(100, n + 1)) if n < 100 else "100", uid=U1)
    if n < 100:
        await WR.twr_answer(hi, FakeBot())
        assert rp[-1] == "⬇️ پایین‌تر"
    ok, rp2 = _msg(str(n), uid=U2)
    await WR.twr_answer(ok, FakeBot())
    assert (await wager(db, w.id)).winner_id == U2 and "برنده شد" in rp2[-1]


@pytest.mark.asyncio
async def test_expiry_sweep_refunds_and_final_text_for_tenant(db, monkeypatch):
    await setup(db, monkeypatch)
    m, _ = _msg("سرعت 5000", uid=U1)
    await WR.twr_create(m, FakeBot())
    async with db() as s:
        w = await s.scalar(select(models.PlatformWager))
    await expire(db, w.id)
    class FB:
        edits = []
        def __init__(self, token): pass
        async def edit_message_text(self, text, **kw): FB.edits.append(text)
        class session:
            @staticmethod
            async def close(): pass
    monkeypatch.setattr(WG, "Bot", FB)
    assert await WG.expire_wagers_once() == 1
    assert await bu(db, U1) == 100_000 and "حریفی پیدا نشد" in FB.edits[-1] and "بازی" in FB.edits[-1]


@pytest.mark.asyncio
async def test_help_and_settings_show_race_games_for_tenant(db, monkeypatch):
    await setup(db, monkeypatch)
    enabled = {g: True for g in (*PW.GAMES, *R.RACE_GAMES)}
    txt = PW.help_text(enabled, bot_id=BOT, cfg={"min": 1000, "max": 0, "fee_pct": 0, "ttl": 300, "move": 180, "move_sim": 60})
    assert "حدس عدد ۱۰۰۰۰" in txt and "سرعت ۱۰۰۰۰" in txt and "ریاضی ۱۰۰۰۰" in txt and len(txt) < 4000
    only = PW.help_text({**enabled, "speed": False}, bot_id=BOT, cfg={})
    assert "سرعت ۱۰۰۰۰" not in only and "حدس عدد ۱۰۰۰۰" in only
    main = PW.help_text({g: True for g in PW.GAMES}, bot_id=0, cfg={})
    assert "حدس عدد ۱۰۰۰۰" not in main                                                   # ربات اصلی: بخشِ جدا (help_section)
    text, rows = await WG.settings_extra(BOT)
    assert "سرعت" in text and "ریاضی" in text and len(rows) == 2
    cbs = [b.callback_data for r in rows for b in r]
    assert cbs == ["twg:set:xo", "twg:set:rps", "twg:set:dice", "twg:set:guess", "twg:set:speed", "twg:set:math"]


def _rows(kb):
    return [[(b.text, b.callback_data) for b in r] for r in kb.inline_keyboard]


@pytest.mark.asyncio
async def test_result_message_is_edited_with_prize_and_balance_buttons_for_every_game(db, monkeypatch):
    """فدایی: نتیجهٔ همهٔ بازی‌ها همان پیامِ بازی را ادیت می‌کند و دکمه‌هایِ جایزه/موجودیِ برنده/بازنده می‌آید."""
    await setup(db, monkeypatch)
    # ۱) مسابقهٔ سرعت
    m, _ = _msg("سرعت 10000", uid=U1)
    await WR.twr_create(m, FakeBot())
    async with db() as s:
        w = await s.scalar(select(models.PlatformWager))
    edits = []
    await WR.twr_join(_cb(f"twr:join:{w.id}", U2, edits), FakeBot())
    bot = FakeBot()
    right, replies = _msg(R.LIVE[w.id]["answer"], uid=U2)
    await WR.twr_answer(right, bot)
    text, kb = bot.edited[-1]
    assert "برنده" in text
    assert _rows(kb) == [[("🎁 جایزهٔ برنده", "twg:noop"), ("20,000 تومان", "twg:noop")],
                         [("💎 موجودیِ برنده", "twg:noop"), ("110,000 تومان", "twg:noop")],
                         [("❌ موجودیِ بازنده", "twg:noop"), ("90,000 تومان", "twg:noop")]]
    assert "برنده شد" in replies[-1]                                  # ریپلایِ «برنده شدید» برایِ پیامِ بازیکن هم می‌ماند
    # ۲) تاس/ایکس‌او/سنگ‌کاغذ‌قیچی (هستهٔ مشترک) از مسیرِ _show
    w2 = await PW_new(db)
    async with db() as s:
        row = await s.get(models.PlatformWager, w2.id, with_for_update=True)
        await PW._settle_win(s, row, U1, "win")
        await s.commit()
    done = await wager(db, w2.id)
    ed = []
    await WG._show(_cb(f"twg:x:{w2.id}:0", U1, ed), done)
    assert ed and _rows(ed[-1][1])[0][0] == ("🎁 جایزهٔ برنده", "twg:noop") and "موجودیِ بازنده" in str(_rows(ed[-1][1]))
    # ۳) مساوی/لغو: بدونِ دکمه‌هایِ نتیجه
    assert await PW.result_kb(await wager(db, w.id)) is not None
    w3 = await PW_new(db, amount=5_000)
    async with db() as s:
        r3 = await s.get(models.PlatformWager, w3.id, with_for_update=True)
        await PW._refund_both(s, r3, "draw", "draw")
        await s.commit()
    assert await PW.result_kb(await wager(db, w3.id)) is None
    # ربات اصلی: بدونِ تغییر
    plat = SimpleNamespace(bot_id=0, status="settled", winner_id=1, creator_id=1, opponent_id=2, payout_amount=1)
    assert await PW.result_kb(plat) is None


async def PW_new(db, amount=10_000):
    from tests.test_wager_tenant import join, new
    w = await new(db, "dice", amount=amount)
    return await join(w)
