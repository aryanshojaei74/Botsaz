"""PATCH_GIFT_CEMOJI_V1 — نگاشتِ گیفت ← ایموجیِ پرمیوم (سراسری) و آیکون در فروشگاه."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_gift_emoji as PG
import tenant
from tenant.bots.virtual_service import gift_sync as _GSM  # noqa: F401
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
    for mod in (config, PG, tenant, SM, _GSM):
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
    b = FakeBot([gift("g1", "u1")], [])
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


@pytest.mark.asyncio
async def test_dump_lists_gifts_and_pack_for_diagnosis(env, monkeypatch):
    monkeypatch.setattr(PG, "PLATFORM_ADMIN_ID", 42)
    sent = []

    class B(FakeBot):
        pass

    def st(uid, cid=None, emoji="🧸"):
        return NS(file_unique_id=uid, custom_emoji_id=cid, emoji=emoji, file_size=1234, width=512, height=512, type="regular",
                  is_animated=True, is_video=False, set_name="s", thumbnail=NS(file_unique_id="th-" + uid))
    gifts = [NS(id="g1", star_count=50, total_count=None, sticker=st("u1"))]
    bot = B(gifts, [st("p1", "C1"), st("p2", "C2")])

    class M(_M):
        async def answer_document(self, doc, **k):
            sent.append(doc.data.decode() if hasattr(doc, "data") else doc)
    m = M("/giftdump", [], 42)
    m.bot = bot
    await PG.cmd_dump(m)
    txt = sent[0]
    assert "g1 | 50" in txt and "u1" in txt and "p2" in txt and "th-p1" in txt and "# پک" in txt
    m2 = M("/giftdump", [], 7)                                   # غیرِ مدیر
    m2.bot = bot
    sent.clear()
    await PG.cmd_dump(m2)
    assert sent == []


@pytest.mark.asyncio
async def test_sync_prefers_gift_own_custom_emoji_and_skips_pack_when_all_have_it(env):
    g1 = NS(id="g1", sticker=NS(file_unique_id="u1", custom_emoji_id="OWN1"))
    g2 = NS(id="g2", sticker=NS(file_unique_id="u2", custom_emoji_id="OWN2"))
    bot = FakeBot([g1, g2], [sticker("u1", "PACK1")])
    r = await PG.sync(bot)
    assert bot.asked is None                                           # همهٔ گیفت‌ها ایموجیِ خودشان را دارند ⇒ پک لازم نیست
    assert (r["matched"], r["own"], r["unmatched"]) == (2, 2, [])
    assert await PG.get_map(fresh=True) == {"g1": "OWN1", "g2": "OWN2"}


@pytest.mark.asyncio
async def test_pack_fallback_by_size_and_emoji(env):
    gift_no_cid = NS(id="g1", sticker=NS(file_unique_id="x", custom_emoji_id=None, file_size=62496, emoji="🧸"))
    pk = NS(file_unique_id="y", custom_emoji_id="PACK-TEDDY", file_size=62496, emoji="🧸")
    other = NS(file_unique_id="z", custom_emoji_id="PACK-OTHER", file_size=1, emoji="🎁")
    r = await PG.sync(FakeBot([gift_no_cid], [other, pk]))
    assert r["matched"] == 1 and r["own"] == 0
    assert await PG.get_map(fresh=True) == {"g1": "PACK-TEDDY"}


@pytest.mark.asyncio
async def test_tenant_sync_stores_own_custom_emoji_and_icon_uses_it(env):
    async with env() as s:
        s.add(models.Bot(id=5, owner_id=500, token="5:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.TelegramGiftConfig(bot_id=5, gift_id="teddy", star_count=50, emoji="🧸", active=True))
        await s.commit()
    from tenant.bots.virtual_service import gift_sync as GS
    monkey_gift = NS(id="teddy", star_count=50, sticker=NS(emoji="🧸", file_id="F", custom_emoji_id="CE-OWN"))
    await GS.sync_bot(FakeBot([monkey_gift], []), 5)
    tenant._SETTING_CACHE.clear()
    assert await tenant.get_setting(5, "gift_cemoji:teddy", "") == "CE-OWN"
    await tenant.set_setting(5, "gift_icons", "1")
    tenant._SETTING_CACHE.clear()
    PG._CACHE.update(t=0.0, map={})
    assert await SM._gift_icon_id(5, "teddy") == "CE-OWN"
