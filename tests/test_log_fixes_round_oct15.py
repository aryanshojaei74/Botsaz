"""رفع‌هایِ لاگِ ۱۵ مهر: InaccessibleMessage، رقابتِ upsertِ Setting و ساختِ کیفِ Fragment."""
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, InaccessibleMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config  # noqa: F401  (پچ را اعمال می‌کند)
import models
import tenant


def _im(bot=None):
    m = InaccessibleMessage(chat=Chat(id=5, type="private"), message_id=7, date=0)
    if bot is not None:
        object.__setattr__(m, "_bot", bot)
    return m


@pytest.mark.asyncio
async def test_inaccessible_message_has_safe_delete_edit_and_empty_fields():
    m = _im()
    assert await m.delete() is False                 # قبلاً AttributeError
    for call in (lambda: m.edit_text("x"), lambda: m.edit_caption(caption="x"), lambda: m.edit_reply_markup()):
        with pytest.raises(TelegramBadRequest):       # تا fallbackِ هندلرها (پیامِ تازه) اجرا شود
            await call()
    assert m.text is None and m.caption is None and m.reply_markup is None and m.photo is None


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_setting_survives_concurrent_first_insert(db, monkeypatch):
    """شبیه‌سازیِ رقابت: بین «نبود» و «درج» ردیف توسطِ پروسهٔ دیگر ساخته می‌شود ⇒ نباید IntegrityError بالا بیاید."""
    orig_scalar = None
    calls = {"n": 0}
    from sqlalchemy.ext.asyncio import AsyncSession
    orig_scalar = AsyncSession.scalar

    async def racing_scalar(self, stmt, *a, **k):
        res = await orig_scalar(self, stmt, *a, **k)
        calls["n"] += 1
        if calls["n"] == 1 and res is None:          # درست بعد از «ردیف نیست»، پروسهٔ دیگر آن را می‌سازد
            async with db() as other:
                other.add(models.Setting(bot_id=9, key="k1", value="theirs"))
                await other.commit()
        return res
    monkeypatch.setattr(AsyncSession, "scalar", racing_scalar)
    await tenant.set_setting(9, "k1", "mine")
    monkeypatch.undo()
    async with db() as s:
        rows = (await s.scalars(select(models.Setting).where(models.Setting.key == "k1"))).all()
    assert len(rows) == 1 and rows[0].value == "mine"      # آخرین نوشتن می‌ماند
