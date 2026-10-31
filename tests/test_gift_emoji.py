"""PATCH_GIFT_EMOJI_V1 — ایموجی/استیکرِ واقعیِ هر گیفت + هم‌گام‌سازیِ خودکار."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import gift_sync as GS
from tenant.bots.virtual_service import smm as SM


class FakeBot:
    def __init__(self, gifts):
        self._g = gifts

    async def get_available_gifts(self):
        return NS(gifts=self._g)


def G(gid, emoji, stars, fid):
    return NS(id=gid, star_count=stars, sticker=NS(emoji=emoji, file_id=fid))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, GS, SM):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=500, token="5:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.TelegramGiftConfig(bot_id=5, gift_id="teddy", star_count=50, emoji="🧸", active=True))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_adds_new_gift_inactive_and_stores_real_stickers(env):
    maker = env
    bot = FakeBot([G("teddy", "🧸", 55, "FILE-T"), G("halloween_teddy", "🧸", 100, "FILE-H")])
    new = await GS.sync_bot(bot, 5)
    assert new == [("halloween_teddy", "🧸", 100)]
    async with maker() as s:
        cfgs = {c.gift_id: c for c in (await s.scalars(select(models.TelegramGiftConfig))).all()}
    assert cfgs["teddy"].star_count == 55 and cfgs["teddy"].active is True          # قیمتِ قدیمی به‌روز، فعال می‌ماند
    assert cfgs["halloween_teddy"].active is False                                    # گیفتِ تازه غیرفعال
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(5, "gift_sticker:halloween_teddy", "") == "FILE-H"
    assert await tenant.get_setting(5, "gift_sticker:teddy", "") == "FILE-T"
    assert await GS.sync_bot(bot, 5) == []                                            # بارِ دوم چیزِ تازه‌ای نیست


@pytest.mark.asyncio
async def test_auto_activate_setting_and_notice_marks_duplicate_emoji(env):
    await tenant.set_setting(5, "gift_auto_activate", "1")
    tenant._SETTING_CACHE.clear()
    new = await GS.sync_bot(FakeBot([G("teddy", "🧸", 50, "a"), G("h", "🧸", 100, "b")]), 5)
    async with env() as s:
        h = await s.scalar(select(models.TelegramGiftConfig).where(models.TelegramGiftConfig.gift_id == "h"))
    assert h.active is True
    txt = GS.notice(new, ["🧸", "🧸"], True)
    assert "ایموجیِ تکراری" in txt and "خودکار فعال شد" in txt and "100" in txt
    assert "غیرفعال" in GS.notice(new, ["🧸"], False)


@pytest.mark.asyncio
async def test_owner_emoji_override_changes_label_everywhere(env):
    maker = env
    async with maker() as s:
        g = await s.scalar(select(models.TelegramGiftConfig))
    price, label = await SM._gift_price_and_label(g, 5)
    assert "🧸" in label and "🎃" not in label
    await tenant.set_setting(5, "gift_emoji:teddy", "🎃🧸")
    tenant._SETTING_CACHE.clear()
    price, label = await SM._gift_price_and_label(g, 5)
    assert "🎃🧸" in label and "50 استارز" in label


class _Msg:
    def __init__(self, text):
        self.text, self.out = text, []
        self.from_user = NS(id=500)

    async def answer(self, t, **k):
        self.out.append(t)


class _St:
    def __init__(self):
        self.d = {"gifte_gid": "teddy", "gifte_page": 0}

    async def get_data(self):
        return dict(self.d)

    async def clear(self):
        self.d = {}


@pytest.mark.asyncio
async def test_emoji_save_validation_and_reset(env, monkeypatch):
    async def owner(msg, bot):
        return NS(id=5, owner_id=500)
    monkeypatch.setattr(SM, "_owner", owner)
    m = _Msg("abc")
    await SM.gift_emoji_save(m, None, _St())
    assert "فقط ایموجی" in m.out[-1]
    assert await tenant.get_setting(5, "gift_emoji:teddy", "") == ""
    m = _Msg("🎃🧸")
    await SM.gift_emoji_save(m, None, _St())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(5, "gift_emoji:teddy", "") == "🎃🧸"
    m = _Msg("-")
    await SM.gift_emoji_save(m, None, _St())
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(5, "gift_emoji:teddy", "") == ""


@pytest.mark.asyncio
async def test_run_once_notifies_owner_once_for_gift_sellers_only(env, monkeypatch):
    sent = []

    class B(FakeBot):
        def __init__(self, token):
            super().__init__([G("teddy", "🧸", 50, "a"), G("new1", "🎃", 70, "b")])

        async def send_message(self, chat, text, **k):
            sent.append((chat, text))

        class session:
            @staticmethod
            async def close():
                pass
    import aiogram
    monkeypatch.setattr(aiogram, "Bot", B)
    assert await GS.run_once() == 1
    assert sent[0][0] == 500 and "گیفتِ تازه" in sent[0][1]
    assert await GS.run_once() == 0                                                  # بارِ دوم خبرِ تکراری نمی‌رود
