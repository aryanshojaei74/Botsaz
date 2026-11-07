"""
تست‌هایِ ابزارِ جابه‌جاییِ شارژهایِ اشتباه (PATCH_PAYER_IDENTITY_V1 — tenant/gateways/topup_repair.py).

سناریوی واقعی: ربات Express؛ حسابِ ساختگیِ ربات (tg=8354597347) با ۲۰۰٬۰۰۰ تومان که مالِ دو مشتری است.
پول در این ابزار است، پس همهٔ مرزها تست می‌شوند: dry-run، انتقالِ دوباره، موجودیِ منفی، مقصدِ نامعتبر.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.gateways import topup_repair as tr

BOT_TG = 8354597347
MELI = 555001
OTHER = 555002


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    ids = {}
    async with mk() as s:
        b = models.Bot(owner_id=1, token=f"{BOT_TG}:AAA", username="ExpressPanel_Bot",
                       field="virtual_service", plan="pro")
        s.add(b)
        await s.flush()
        phantom = models.BotUser(bot_id=b.id, telegram_id=BOT_TG, full_name="Express | خدمات مجازی",
                                 username="ExpressPanel_Bot", balance=200_000)
        meli = models.BotUser(bot_id=b.id, telegram_id=MELI, full_name="Meli", balance=0, kyc_card="6037-9912-3456-9210")
        other = models.BotUser(bot_id=b.id, telegram_id=OTHER, full_name="Other", balance=5_000)
        s.add_all([phantom, meli, other])
        await s.flush()
        # شارژ قبلیِ درست (Meli با همان کارت) — برایِ حدسِ صاحب
        s.add(models.WalletTxn(bot_id=b.id, user_id=meli.id, amount=20_000, kind="zarinpal_charge",
                               note="زرین‌پال؛ ref_id=111؛ card=4598******589210"))
        t1 = models.WalletTxn(bot_id=b.id, user_id=phantom.id, amount=100_000, kind="zarinpal_charge",
                              note="زرین‌پال؛ ref_id=92779779201؛ card=4598******589210")
        t2 = models.WalletTxn(bot_id=b.id, user_id=phantom.id, amount=100_000, kind="zarinpal_charge",
                              note="زرین‌پال؛ ref_id=92778986230؛ card=5022******001122")
        s.add_all([t1, t2])
        await s.commit()
        ids.update(bot=b.id, phantom=phantom.id, meli=meli.id, other=other.id, t1=t1.id, t2=t2.id)
    yield mk, ids
    await eng.dispose()


async def _balances(mk, ids):
    async with mk() as s:
        return {k: float((await s.get(models.BotUser, ids[k])).balance) for k in ("phantom", "meli", "other")}


# ───────────────────────── فهرست ─────────────────────────

def test_own_id_from_token():
    assert tr.own_id_from_token("8354597347:AAH-xyz") == 8354597347
    assert tr.own_id_from_token("bad") is None and tr.own_id_from_token(None) is None
    assert tr.own_id_from_token("abc:def") is None


@pytest.mark.asyncio
async def test_finds_the_phantom_user_and_only_it(world):
    mk, ids = world
    async with mk() as s:
        ph = await tr.find_phantoms(s)
    assert [(b.id, u.telegram_id) for b, u in ph] == [(ids["bot"], BOT_TG)]


@pytest.mark.asyncio
async def test_lists_only_credits_of_the_phantom_with_owner_suggestions(world):
    mk, ids = world
    async with mk() as s:
        items = await tr.list_misdirected(s)
    assert [m.txn_id for m in items] == [ids["t1"], ids["t2"]]     # شارژ درستِ Meli در فهرست نیست
    by = {m.txn_id: m for m in items}
    # کارتِ 4598…9210 با شارژِ قبلیِ Meli و کارتِ KYC-اش (…9210) می‌خواند
    c1 = by[ids["t1"]].candidates
    assert c1 and c1[0]["telegram_id"] == MELI
    assert by[ids["t2"]].candidates == []                           # کارتِ ناشناخته ⇒ حدسی نمی‌زند
    assert not by[ids["t1"]].already_moved


# ───────────────────────── انتقال ─────────────────────────

@pytest.mark.asyncio
async def test_dry_run_changes_nothing(world):
    mk, ids = world
    before = await _balances(mk, ids)
    plan = await tr.move_credit(mk, ids["t1"], MELI)                # apply=False
    assert plan.applied is False and plan.amount == 100_000
    assert (plan.phantom_balance_after, plan.target_balance_after) == (100_000, 100_000)
    assert await _balances(mk, ids) == before
    async with mk() as s:
        assert (await s.scalars(select(models.WalletTxn).where(
            models.WalletTxn.kind.like("misdirect_fix%")))).all() == []


@pytest.mark.asyncio
async def test_apply_moves_exactly_once_and_leaves_an_audit_trail(world):
    mk, ids = world
    plan = await tr.move_credit(mk, ids["t1"], MELI, apply=True)
    assert plan.applied is True
    assert await _balances(mk, ids) == {"phantom": 100_000, "meli": 100_000, "other": 5_000}
    async with mk() as s:
        fixes = (await s.scalars(select(models.WalletTxn).where(
            models.WalletTxn.kind.like("misdirect_fix%")).order_by(models.WalletTxn.id))).all()
        orig = await s.get(models.WalletTxn, ids["t1"])
    assert [(f.kind, float(f.amount)) for f in fixes] == [("misdirect_fix_out", -100_000), ("misdirect_fix_in", 100_000)]
    assert fixes[0].user_id == ids["phantom"] and fixes[1].user_id == ids["meli"]
    assert tr.MOVED_MARK in orig.note and str(MELI) in orig.note
    assert all(len(f.note) <= 200 and len(f.kind) <= 20 for f in fixes)      # حدِ ستون‌هایِ Postgres

    with pytest.raises(tr.RepairError, match="قبلاً منتقل"):
        await tr.move_credit(mk, ids["t1"], MELI, apply=True)                 # انتقالِ دوباره ممنوع
    assert await _balances(mk, ids) == {"phantom": 100_000, "meli": 100_000, "other": 5_000}


@pytest.mark.asyncio
async def test_moved_txn_is_flagged_in_the_listing(world):
    mk, ids = world
    await tr.move_credit(mk, ids["t1"], MELI, apply=True)
    async with mk() as s:
        items = {m.txn_id: m for m in await tr.list_misdirected(s)}
    assert items[ids["t1"]].already_moved and not items[ids["t2"]].already_moved


@pytest.mark.asyncio
async def test_never_makes_the_phantom_balance_negative(world):
    mk, ids = world
    async with mk() as s:
        ph = await s.get(models.BotUser, ids["phantom"])
        ph.balance = 50_000                                                    # کمتر از ۱۰۰٬۰۰۰
        await s.commit()
    with pytest.raises(tr.RepairError, match="کمتر"):
        await tr.move_credit(mk, ids["t1"], MELI, apply=True)
    assert (await _balances(mk, ids))["phantom"] == 50_000 and (await _balances(mk, ids))["meli"] == 0


@pytest.mark.asyncio
async def test_rejects_invalid_targets(world):
    mk, ids = world
    with pytest.raises(tr.RepairError, match="پیدا نشد"):
        await tr.move_credit(mk, ids["t1"], 999_999_999, apply=True)          # کاربرِ ناموجود
    with pytest.raises(tr.RepairError, match="خودِ ربات"):
        await tr.move_credit(mk, ids["t1"], BOT_TG, apply=True)               # مقصد = خودِ ربات
    assert await _balances(mk, ids) == {"phantom": 200_000, "meli": 0, "other": 5_000}


@pytest.mark.asyncio
async def test_target_must_belong_to_the_same_bot(world):
    mk, ids = world
    async with mk() as s:
        b2 = models.Bot(owner_id=1, token="777:BBB", username="other_bot", field="vpn_sales", plan="pro")
        s.add(b2)
        await s.flush()
        s.add(models.BotUser(bot_id=b2.id, telegram_id=888_001, full_name="Stranger", balance=0))
        await s.commit()
    with pytest.raises(tr.RepairError, match="پیدا نشد"):
        await tr.move_credit(mk, ids["t1"], 888_001, apply=True)              # کاربرِ رباتِ دیگر


@pytest.mark.asyncio
async def test_refuses_transactions_that_do_not_belong_to_the_phantom(world):
    """ابزار نباید تراکنشِ یک مشتریِ عادی (یا غیرِ شارژ) را جابه‌جا کند."""
    mk, ids = world
    async with mk() as s:
        real = models.WalletTxn(bot_id=ids["bot"], user_id=ids["other"], amount=1_000, kind="zarinpal_charge", note="x")
        debit = models.WalletTxn(bot_id=ids["bot"], user_id=ids["phantom"], amount=-500, kind="purchase", note="y")
        s.add_all([real, debit])
        await s.commit()
        real_id, debit_id = real.id, debit.id
    with pytest.raises(tr.RepairError, match="ساختگیِ خودِ ربات"):
        await tr.move_credit(mk, real_id, MELI, apply=True)
    with pytest.raises(tr.RepairError, match="مبلغِ مثبت"):
        await tr.move_credit(mk, debit_id, MELI, apply=True)
    with pytest.raises(tr.RepairError, match="پیدا نشد"):
        await tr.move_credit(mk, 424242, MELI, apply=True)


@pytest.mark.asyncio
async def test_moved_marker_survives_a_long_note(world):
    """ستونِ note ۲۰۰ نویسه است؛ نشانهٔ «منتقل‌شده» نباید بریده شود وگرنه انتقالِ دوباره ممکن می‌شد."""
    mk, ids = world
    async with mk() as s:
        t = await s.get(models.WalletTxn, ids["t1"])
        t.note = "ن" * 199
        await s.commit()
    await tr.move_credit(mk, ids["t1"], MELI, apply=True)
    async with mk() as s:
        note = (await s.get(models.WalletTxn, ids["t1"])).note
    assert len(note) <= 200 and tr.MOVED_MARK in note
    with pytest.raises(tr.RepairError, match="قبلاً منتقل"):
        await tr.move_credit(mk, ids["t1"], OTHER, apply=True)


@pytest.mark.asyncio
async def test_two_moves_drain_the_phantom_exactly(world):
    mk, ids = world
    await tr.move_credit(mk, ids["t1"], MELI, apply=True)
    await tr.move_credit(mk, ids["t2"], OTHER, apply=True)
    assert await _balances(mk, ids) == {"phantom": 0, "meli": 100_000, "other": 105_000}


@pytest.mark.asyncio
async def test_no_phantom_no_findings():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    async with mk() as s:
        s.add(models.Bot(owner_id=1, token="1234:AAA", username="clean", field="vpn_sales", plan="pro"))
        await s.commit()
        assert await tr.find_phantoms(s) == [] and await tr.list_misdirected(s) == []
    await eng.dispose()


def test_cli_script_is_importable_and_documented():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "repair_misdirected_topups.py").read_text(encoding="utf-8")
    assert "--apply" in src and "--notify" in src and "list" in src and "move" in src
    compile(src, "repair_misdirected_topups.py", "exec")
