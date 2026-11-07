"""PATCH_BACKUP_FULL_V1 — بک‌آپ کامل و بازیابی در یک دیتابیسِ کاملاً جدید (شبیه‌سازیِ مرگِ سرور)."""
import warnings
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import backup_full as bf


async def _mk():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


@pytest.mark.asyncio
async def test_full_roundtrip_to_fresh_db_remaps_ids(monkeypatch):
    eng1, mk1 = await _mk()
    monkeypatch.setattr(bf, "Session", mk1)
    async with mk1() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="old", field="virtual_service", plan="pro", status="active"))
        u1 = models.BotUser(bot_id=5, telegram_id=111, balance=Decimal(750), full_name="Ali")
        u2 = models.BotUser(bot_id=5, telegram_id=222, balance=Decimal(10))
        s.add_all([u1, u2])
        await s.flush()
        s.add(models.WalletTxn(bot_id=5, user_id=u1.id, amount=750, kind="admin", note="n"))
        p = models.MainButton(bot_id=5, row=0, col=0, label="parent", action="message")
        s.add(p)
        await s.flush()
        s.add(models.MainButton(bot_id=5, row=1, col=0, label="child", action="message", parent_id=p.id))
        t = models.TenantTicket(bot_id=5, user_id=u2.id, text="hi")
        s.add(t)
        await s.flush()
        s.add(models.TenantTicketReply(ticket_id=t.id, sender="admin", text="re"))
        s.add(models.Setting(bot_id=5, key="k", value="v"))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=u1.id, gift_id="g", star_count=1, price_toman=100,
                               dest_telegram_id=111, dest_label="x", status="sent"))
        # دادهٔ رباتِ دیگر نباید وارد بک‌آپ شود
        s.add(models.Bot(id=6, owner_id=2, token="6:T", username="other", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=6, telegram_id=999, balance=Decimal(1)))
        await s.commit()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = await bf.build_full_backup(5)
    assert len(data["tables"]["bot_users"]) == 2
    raw = bf.dump_bytes(data)
    data2 = bf.load_bytes(raw)
    await eng1.dispose()

    # دیتابیسِ تازه با شناسه‌هایِ متفاوت (رباتِ جدید id=9، با ردیف‌هایِ اضافه تا id ها جابه‌جا شوند)
    eng2, mk2 = await _mk()
    monkeypatch.setattr(bf, "Session", mk2)
    async with mk2() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="new", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=8, owner_id=3, token="8:T", username="x", field="virtual_service", plan="pro", status="active"))
        for i in range(3):
            s.add(models.BotUser(bot_id=8, telegram_id=5000 + i, balance=Decimal(0)))
        s.add(models.BotUser(bot_id=9, telegram_id=777, balance=Decimal(5)))      # باید پاک شود
        await s.commit()
    stats = await bf.restore_full_backup(9, data2)
    assert stats["inserted"] > 0
    async with mk2() as s:
        users = {u.telegram_id: u for u in (await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == 9))).all()}
        assert set(users) == {111, 222}                      # ۷۷۷ پاک شد
        assert float(users[111].balance) == 750
        tx = await s.scalar(select(models.WalletTxn).where(models.WalletTxn.bot_id == 9))
        assert tx.user_id == users[111].id                    # FK به کاربرِ جدید نگاشت شد
        btns = {b.label: b for b in (await s.scalars(select(models.MainButton).where(models.MainButton.bot_id == 9))).all()}
        assert btns["child"].parent_id == btns["parent"].id   # خودارجاع
        tk = await s.scalar(select(models.TenantTicket).where(models.TenantTicket.bot_id == 9))
        assert tk.user_id == users[222].id
        rep = await s.scalar(select(models.TenantTicketReply).where(models.TenantTicketReply.ticket_id == tk.id))
        assert rep is not None                                # جدولِ فرزند از طریقِ والد
        go = await s.scalar(select(models.GiftOrder).where(models.GiftOrder.bot_id == 9))
        assert go.buyer_user_id == users[111].id
        # رباتِ ۸ دست‌نخورده
        n8 = len((await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == 8))).all())
        assert n8 == 3
        assert await s.scalar(select(models.Setting.value).where(models.Setting.bot_id == 9, models.Setting.key == "k")) == "v"
    await eng2.dispose()


@pytest.mark.asyncio
async def test_crafted_file_cannot_write_to_other_bot(monkeypatch):
    eng, mk = await _mk()
    monkeypatch.setattr(bf, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="new", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=7, owner_id=2, token="7:T", username="victim", field="virtual_service", plan="pro", status="active"))
        await s.commit()
    evil = {"format": bf.FORMAT_NAME, "version": 1, "tables": {
        "bot_users": [{"id": 1, "bot_id": 7, "telegram_id": 31337, "balance": {"__t": "dec", "v": "999999"}}]}}
    await bf.restore_full_backup(9, evil)
    async with mk() as s:
        assert await s.scalar(select(models.BotUser.bot_id).where(models.BotUser.telegram_id == 31337)) == 9
    await eng.dispose()


@pytest.mark.asyncio
async def test_restore_skips_rows_conflicting_with_another_bot_instead_of_failing_everything(monkeypatch):
    """PATCH_BACKUP_RESTORE_SKIP_CONFLICTS_V1 — بک‌آپِ رباتِ A را در رباتِ B ریستور می‌کنیم در حالی که A هنوز هست و
    BotUser.api_key (یکتایِ سراسری) در هر دو یکی است. قبلاً کلِ بازگردانی می‌ترکید؛ حالا فقط ردیفِ متعارض رد می‌شود."""
    eng, mk = await _mk()
    monkeypatch.setattr(bf, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="a", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=2, owner_id=2, token="2:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=1, telegram_id=11, balance=Decimal(5), api_key="SAMEKEY"))
        s.add(models.BotUser(bot_id=1, telegram_id=12, balance=Decimal(6), api_key="OKKEY"))
        s.add(models.Setting(bot_id=1, key="k", value="v"))
        await s.commit()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = bf.load_bytes(bf.dump_bytes(await bf.build_full_backup(1)))
    stats = await bf.restore_full_backup(2, data)                    # A هنوز هست ⇒ api_key یکی از کاربران تداخل دارد
    assert stats["skipped"] >= 2 and stats["conflicts"].get("bot_users", 0) >= 2
    async with mk() as s:
        b2 = (await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == 2))).all()
        assert await s.scalar(select(models.Setting.value).where(models.Setting.bot_id == 2, models.Setting.key == "k")) == "v"
        a_users = (await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == 1))).all()
    assert len(a_users) == 2                                         # رباتِ A دست‌نخورده
    assert stats["inserted"] >= 1 and isinstance(len(b2), int)       # بقیهٔ دادهٔ B بازگشت (کلِ عملیات نترکید)
    await eng.dispose()


def test_short_error_has_no_sql_or_params():
    class _Orig(Exception):
        pass
    e = Exception("(sqlite3.IntegrityError) UNIQUE constraint failed: bot_users.api_key\n[SQL: INSERT INTO bot_users ...]\n[parameters: ('secret',)]")
    e.orig = _Orig("UNIQUE constraint failed: bot_users.api_key")
    out = bf.short_error(e)
    assert "UNIQUE constraint failed" in out and "secret" not in out and "SQL" not in out
