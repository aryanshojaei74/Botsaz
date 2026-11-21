import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import tenant, models


def test_targets_and_run(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(tenant, "Session", S)
        async with S() as s:
            s.add(models.BotUser(bot_id=1, telegram_id=10, phone_verified=True))
            s.add(models.BotUser(bot_id=1, telegram_id=11, phone_verified=False))
            s.add(models.BotUser(bot_id=1, telegram_id=12, phone_verified=False, is_banned=True))
            s.add(models.BotUser(bot_id=2, telegram_id=13, phone_verified=False))
            await s.commit()
        ids = await tenant._phone_collect_targets(1)
        assert ids == [11]
        import tenant.shared.sms_verify as sv
        async def no(b): return False
        monkeypatch.setattr(sv, "sms_mode", no)
        async def adm(*a): return False
        monkeypatch.setattr(tenant, "is_owner_or_admin", adm)
        sent = []
        class B:
            async def send_message(self, uid, text, **k): sent.append(uid)
        class R: id = 1; owner_id = 99
        ok, fail = await tenant._phone_collect_run(B(), R(), 99, ids)
        assert (ok, fail) == (1, 0) and sent == [11, 99]
    asyncio.run(go())
