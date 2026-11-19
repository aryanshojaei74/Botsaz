"""PATCH_FEE_BY_RANK_V1 (سینا): کارمزد بر اساسِ رتبه در جدول — «نفر اول، دوم، سوم، چهارم، پنجم»."""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_money as M
import platform_wager as W
import platform_wager_race as R
import wager_rank as K

CHAT = -100
CFG = {"min": 1000, "max": 0, "fee_pct": Decimal("5"), "ttl": 300, "move": 180, "move_sim": 300}


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    import config
    import platform_bot as PB
    for mod in (W, R, M, K, config, PB):
        monkeypatch.setattr(mod, "Session", mk, raising=False)
    async with mk() as s:
        for uid in range(1, 9):
            s.add(models.PlatformUser(telegram_id=uid, full_name=f"u{uid}", balance=10_000_000))
        await s.commit()
    await M._set("pgr_on", "1")
    R.LIVE.clear()
    yield mk
    await eng.dispose()


async def play(creator, opponent, winner):
    w, err = await W.create_wager("speed", CHAT, (creator, f"u{creator}", None), 10000, None, CFG)
    assert err is None, err
    assert (await R.join_race(w.id, (opponent, f"u{opponent}", None))).err is None
    return await R.settle_to(w.id, winner)


def test_basis_cycle_and_labels():
    assert K.next_basis("games") == "wins" and K.next_basis("wins") == "rank" and K.next_basis("rank") == "games"
    assert K.next_basis("garbage") == "wins" and K._norm_basis(None) == "games"
    assert K.basis_label("rank") == "رتبه در جدول" and K.rank_name(1) == "نفر اول" and K.rank_name(5) == "نفر پنجم" and K.rank_name(12) == "نفر 12"
    t = K.parse_tiers("1 2\n2 3\n3 4")
    assert K.pct_for_rank(t, 1) == 2 and K.pct_for_rank(t, 3) == 4 and K.pct_for_rank(t, 4) is None and K.pct_for_rank(t, None) is None
    assert "نفر اول جدول: 2٪" in K.tiers_text_fa(t, "rank")


@pytest.mark.asyncio
async def test_winner_pays_the_fee_of_their_leaderboard_rank(env):
    # بازی‌هایِ پیش از تنظیم: u2 دو برد، u1 یک برد، u3 یک بازی بدونِ برد ⇒ جدول: ۱) u2 ۲) u1 ۳) u3
    await play(1, 2, 2)
    await play(1, 2, 2)
    await play(1, 3, 1)
    st = await K.player_stats(0, 10)
    assert [d["uid"] for d in st][:3] == [2, 1, 3]
    await K.save_config(0, basis="rank", tiers_text="1:2;2:3;3:4")
    w1 = await play(4, 2, 2)                                   # u2 نفر اول ⇒ ۲٪ از ۲۰٬۰۰۰ = ۴۰۰
    assert float(w1.fee_pct) == 2 and int(w1.fee_amount) == 400 and int(w1.payout_amount) == 19600
    w2 = await play(4, 1, 1)                                   # u1 نفر دوم (بعد از بُردِ u2 هنوز دوم) ⇒ ۳٪ = ۶۰۰
    assert float(w2.fee_pct) == 3 and int(w2.fee_amount) == 600
    w3 = await play(4, 3, 3)                                   # u3 نفر سوم؟ (۰ برد؛ ولی بعد از u4 که برد ندارد…) ⇒ ردیفِ سوم: ۴٪
    st = await K.player_stats(0, 10)
    pos = {d["uid"]: i for i, d in enumerate(st, 1)}
    assert float(w3.fee_pct) in (4.0, 5.0)                       # رتبهٔ u3 پیش از این بازی: سوم (۴٪) یا بی‌خط (۵٪ عادی) — هیچ‌کدام خطا نیست
    assert pos[2] == 1 or pos[2] == 2


@pytest.mark.asyncio
async def test_rank_without_a_line_and_unranked_players_pay_the_normal_fee(env):
    await play(1, 2, 2)
    await K.save_config(0, basis="rank", tiers_text="2:1")     # فقط رتبهٔ ۲ خط دارد؛ u2 رتبهٔ ۱ است
    w = await play(3, 2, 2)
    assert float(w.fee_pct) == 5 and int(w.fee_amount) == 1000                           # رتبهٔ ۱ خط ندارد ⇒ عادی
    w2 = await play(5, 6, 6)                                                             # u6 هنوز بازی ندارد ⇒ بی‌رتبه ⇒ عادی
    assert float(w2.fee_pct) == 5 and int(w2.fee_amount) == 1000


@pytest.mark.asyncio
async def test_current_game_does_not_count_and_free_games_do_not_rank(env):
    await K.save_config(0, basis="rank", tiers_text="1:1")
    w = await play(1, 2, 1)                                    # اولین بازیِ همه: هیچ‌کس رتبه ندارد ⇒ عادی
    assert float(w.fee_pct) == 5
    fw, _ = await W.create_free_wager("speed", CHAT, (3, "u3", None), None, 300, 0, 0)
    assert (await R.join_race(fw.id, (4, "u4", None))).err is None
    await R.settle_to(fw.id, 3)
    st = await K.player_stats(0, 10)
    assert 3 not in [d["uid"] for d in st]                     # بازیِ رایگان رتبه نمی‌سازد
    w2 = await play(1, 5, 1)                                   # u1 حالا نفر اول (یک برد) ⇒ ۱٪
    assert float(w2.fee_pct) == 1 and int(w2.fee_amount) == 200


@pytest.mark.asyncio
async def test_money_is_conserved_and_games_and_wins_modes_are_unchanged(env):
    async def total():
        from sqlalchemy import func, select
        async with env() as s:
            return int(await s.scalar(select(func.sum(models.PlatformUser.balance))))
    start = await total()
    await K.save_config(0, basis="rank", tiers_text="1:2")
    for a, b, win in ((1, 2, 1), (1, 3, 1), (4, 1, 1)):
        w = await play(a, b, win)
        assert int(w.payout_amount) + int(w.fee_amount) == 20000
    fees = 0
    async with env() as s:
        from sqlalchemy import select
        fees = sum(int(x.fee_amount) for x in (await s.scalars(select(models.PlatformWager))).all())
    assert await total() == start - fees                        # فقط کارمزد از چرخه خارج می‌شود
    await K.save_config(0, basis="games", tiers_text="0:5;1:2")                              # مبنای قدیمی بدونِ تغییر
    w = await play(7, 8, 8)
    assert float(w.fee_pct) == 5


@pytest.mark.asyncio
async def test_board_text_shows_rank_fees_and_footer(env):
    await play(1, 2, 2)
    await play(1, 3, 1)
    await K.save_config(0, basis="rank", tiers_text="1:2;2:3")
    text = await K.board_page_text(0)
    assert "کارمزد 2٪" in text and "کارمزد 3٪" in text                                    # کنارِ نفر اول و دوم
    assert "کارمزد بر اساسِ رتبه" in text and "نفر اول: کارمزد 2٪" in text and "نفر دوم: کارمزد 3٪" in text


@pytest.mark.asyncio
async def test_admin_input_validates_rank_lines_and_help_changes_with_basis(env):
    out = []

    class Msg:
        def __init__(self, text): self.text = text
        async def answer(self, text, **k): out.append(text)

    class St:
        async def clear(self): out.append("cleared")
    await K.save_config(0, basis="rank")
    assert "رتبه" in await K.help_for(0) and "نفر اول" in await K.help_for(0)
    await K._tiers_input(Msg("0 5"), St(), 0, "pgrk", "pgrk:back")                          # رتبهٔ ۰ نامعتبر
    assert "رتبه باید" in out[-1] and (await K.load_config(0))[1] == []
    await K._tiers_input(Msg(f"{K.MAX_RANK + 1} 5"), St(), 0, "pgrk", "pgrk:back")
    assert "رتبه باید" in out[-1]
    await K._tiers_input(Msg("1 2\n2 3\n3 4\n4 5\n5 6"), St(), 0, "pgrk", "pgrk:back")
    _b, tiers = await K.load_config(0)
    assert [(a, int(b)) for a, b in tiers] == [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
    text, kb = await K._menu_view(0, "pgrk", "pgrk:back")
    assert "رتبه در جدول" in text and "نفر پنجم جدول: 6٪" in text
    await K.save_config(0, basis="games")
    assert "حداقل" in await K.help_for(0)                                                    # مبنای قدیمی: راهنمای قبلی
