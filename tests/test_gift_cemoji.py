"""PATCH_GIFT_CEMOJI_V1 — نگاشتِ گیفت ← ایموجیِ پرمیوم (سراسری) و آیکون در فروشگاه."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_gift_emoji as PG
import tenant
from tenant.bots.virtual_service import smm as SM


def gift(gid, uid):
    return NS(id=gid, sticker=NS(file_unique_id=uid))


def sticker(uid, cid):
    return NS(file_unique_id=uid, custom_emoji_id=cid)


class FakeBot:
    def __init__(self, gifts, stickers, fail=False):
        self.g, self.s, self.fail = gifts, stickers, fail
        self.asked = None

    async def get_available_gifts(self):
        return NS(gifts=self.g)

    async def get_sticker_set(self, name):
        self.asked = name
        if self.fail:
            raise RuntimeError("STICKERSET_INVALID")
        return NS(stickers=self.s)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PG, tenant, SM):
        monkeypatch.setattr(mod, "Session", maker)
    PG._CACHE.update(t=0.0, map={})
    tenant._SETTING_CACHE.clear()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_matches_by_unique_id_and_reports_unmatched(env):
    bot = FakeBot([gift("g1", "u1"), gift("g2", "u2"), gift("g3", "u3")], [sticker("u1", "C1"), sticker("u3", "C3"), sticker("zz", "CZ")])
    r = await PG.sync(bot)
    assert bot.asked == "gifts_emoji_by_gifts_changes_bot"
    assert (r["matched"], r["total"], r["pack_size"], r["unmatched"]) == (2, 3, 3, ["g2"])
    assert await PG.get_map(fresh=True) == {"g1": "C1", "g3": "C3"}


@pytest.mark.asyncio
async def test_manual_entries_win_and_pack_name_configurable(env):
    await PG.sync(FakeBot([gift("g1", "u1")], [sticker("u1", "AUTO")]))
    await PG._set(PG.KEY_MANUAL, '{"g1": "MANUAL", "g9": "X"}')
    assert await PG.get_map(fresh=True) == {"g1": "MANUAL", "g9": "X"}
    await PG._set(PG.KEY_PACK, "my_pack")
    b = FakeBot([], [])
    await PG.sync(b)
    assert b.asked == "my_pack"


@pytest.mark.asyncio
async def test_sync_error_is_reported_not_raised(env):
    r = await PG.sync(FakeBot([gift("g1", "u1")], [], fail=True))
    assert "STICKERSET_INVALID" in r["error"] and r["matched"] == 0


@pytest.mark.asyncio
async def test_shop_icon_only_when_owner_enabled_and_mapped(env):
    await PG._set(PG.KEY_MANUAL, '{"teddy": "C-TEDDY"}')
    PG._CACHE["t"] = 0.0
    assert await SM._gift_icon_id(5, "teddy") is None                  # تیکِ مالک خاموش (پیش‌فرض)
    await tenant.set_setting(5, "gift_icons", "1")
    tenant._SETTING_CACHE.clear()
    assert await SM._gift_icon_id(5, "teddy") == "C-TEDDY"
    assert await SM._gift_icon_id(5, "unknown") is None                # بدونِ نگاشت ⇒ مثلِ قبل


class _M:
    def __init__(self, text, entities, uid):
        self.text, self.entities, self.from_user = text, entities, NS(id=uid)
        self.chat, self.out = NS(type="private"), []

    async def answer(self, t, **k):
        self.out.append(t)


@pytest.mark.asyncio
async def test_manual_command_reads_custom_emoji_entity_and_is_admin_only(env, monkeypatch):
    monkeypatch.setattr(PG, "PLATFORM_ADMIN_ID", 42)
    ent = NS(type="custom_emoji", custom_emoji_id="CE-1")
    m = _M("/gifte 555 🎃", [ent], 7)                                   # غیرِ مدیر
    await PG.cmd_manual(m)
    assert m.out == [] and await PG.get_map(fresh=True) == {}
    m = _M("/gifte 555 🧸", [], 42)                                    # ایموجیِ پرمیوم نیست
    await PG.cmd_manual(m)
    assert "فرمت" in m.out[-1]
    m = _M("/gifte 555 🎃", [ent], 42)
    await PG.cmd_manual(m)
    assert "ثبت شد" in m.out[-1] and await PG.get_map(fresh=True) == {"555": "CE-1"}
