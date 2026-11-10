import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import tenant, models
from tenant.shared import withdraw as W, profile_stats as PS


def test_ref_summary_note():
    st = PS.ProfileStats() if hasattr(PS, "ProfileStats") else None
    assert st is not None
    assert PS.REF_NOTE_DEFAULT in PS.ref_summary(st)
    assert "غیرقابل برداشت" not in PS.ref_summary(st, "")
    assert PS.ref_summary(st, "سلام").endswith("سلام")


def test_flow(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(W, "Session", S)
        store = {}
        async def gs(b, k, d=""): return store.get(k, d)
        monkeypatch.setattr(W, "_gs", gs)
        async with S() as s:
            s.add(models.BotUser(id=1, bot_id=1, telegram_id=10, balance=500000))
            s.add(models.WalletTxn(bot_id=1, user_id=1, amount=200000, kind="referral"))
            await s.commit()
        assert await W.withdrawable(1, 1, 500000) == 200000      # فقط پاداش
        store["wd_source"] = "all"
        assert await W.withdrawable(1, 1, 500000) == 500000
        # یادداشت
        assert await W.wallet_note(1, "D") == "D"
        store["wallet_note_mode"] = "hidden"
        assert await W.wallet_note(1, "D") == ""
        store.update(wallet_note_mode="custom", wallet_note_text="X")
        assert await W.wallet_note(1, "D") == "X"
        # درخواست و رد/پرداخت
        res, w = await W.create_request(1, 1, 300000, "6037-xxxx")
        assert res == "ok"
        async with S() as s:
            assert int((await s.get(models.BotUser, 1)).balance) == 200000
        assert (await W.create_request(1, 1, 900000, "x"))[0] == "no_balance"
        st, _ = await W.decide(1, w.id, paid=False)
        assert st == "rejected"
        async with S() as s:
            assert int((await s.get(models.BotUser, 1)).balance) == 500000
        assert (await W.decide(1, w.id, paid=True))[0] == "already"
        res, w2 = await W.create_request(1, 1, 100000, "y")
        assert (await W.decide(1, w2.id, paid=True))[0] == "paid"
        async with S() as s:
            assert int((await s.get(models.BotUser, 1)).balance) == 400000
    asyncio.run(go())
