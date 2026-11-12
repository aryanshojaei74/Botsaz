"""PATCH_SMM_PRODUCT_LINK_RULE_V1 + PATCH_SMM_TEXT_LINK_TYPE_V1 (SINA): نوع/نمونهٔ لینک برایِ یک محصول + نوعِ «متنِ ساده»."""
from types import SimpleNamespace as NS

import pytest

from tenant.bots.virtual_service import smm, smm_link_types as lt


def test_plain_text_type_accepts_persian_and_english_letters_and_digits():
    for ok in ("کانال من", "Channel123", "کانال۱۲۳", "abc_def", "ایران 2024"):
        assert lt.classify("telegram", ok) == {"text"}, ok
    for bad in ("https://t.me/x", "@name", "a/b", "hello!", ""):
        assert "text" not in lt.classify("telegram", bad), bad


def test_existing_telegram_link_types_are_unchanged():
    assert lt.classify("telegram", "https://t.me/channel") == {"public"}
    assert lt.classify("telegram", "https://t.me/channel/12") == {"post"}
    assert lt.classify("telegram", "@channel") == {"at"}


@pytest.fixture
def store(monkeypatch):
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)
    monkeypatch.setattr(smm, "_get_setting", gs)
    return st


CAT = NS(id=1, bot_id=1, platform="telegram", link_pattern=None)
V = NS(id=7, bot_id=1)


@pytest.mark.asyncio
async def test_product_rule_overrides_category_rule(store):
    store["smm_cat_links:1"] = "public"
    wrong, _ = await smm._check_links_for_service(V, CAT, ["https://t.me/channel/5"])
    assert wrong == ["https://t.me/channel/5"]                         # دسته: فقط عمومی
    store["smm_svc_links:7"] = "post"
    wrong, hint = await smm._check_links_for_service(V, CAT, ["https://t.me/channel/5"])
    assert wrong == [] and "لینک پست" in hint                         # محصول: پست
    wrong, _ = await smm._check_links_for_service(V, CAT, ["https://t.me/channel"])
    assert wrong == ["https://t.me/channel"]


@pytest.mark.asyncio
async def test_product_pattern_and_fallback_to_category(store):
    assert await smm._check_links_for_service(V, CAT, ["whatever"]) == ([], "")       # بدونِ هیچ قاعده‌ای
    store["smm_svc_pattern:7"] = "https://t.me/yourchannel/123"
    wrong, hint = await smm._check_links_for_service(V, CAT, ["https://instagram.com/p/x"])
    assert wrong and "t.me/yourchannel/123" in hint
    wrong, _ = await smm._check_links_for_service(V, CAT, ["https://t.me/other/9"])
    assert wrong == []


@pytest.mark.asyncio
async def test_plain_text_allowed_only_when_type_enabled(store):
    assert await smm._plain_text_allowed(V, CAT) is False
    store["smm_cat_links:1"] = "text"
    assert await smm._plain_text_allowed(V, CAT) is True
    store["smm_svc_links:7"] = "public"                                  # محصول override می‌کند
    assert await smm._plain_text_allowed(V, CAT) is False
    store["smm_svc_links:7"] = "public,text"
    assert await smm._plain_text_allowed(V, CAT) is True
    wrong, _ = await smm._check_links_for_service(V, CAT, ["کانال من ۱۲۳"])
    assert wrong == []


@pytest.mark.asyncio
async def test_product_link_screen_renders_and_toggles(monkeypatch):
    import models
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(smm, "Session", mk)
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def ss(bot_id, key, val):
        st[key] = val
    monkeypatch.setattr(smm, "_get_setting", gs)
    monkeypatch.setattr(smm, "_set_setting", ss)
    async with mk() as s:
        s.add(models.SmmCategory(id=1, bot_id=1, name="تلگرام", platform="telegram"))
        s.add(models.SmmService(id=7, bot_id=1, service_id=1, name="ممبر", category_pk=1))
        await s.commit()
    text, kb = await smm._svc_link_screen(1, 7, 0)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "smm:svt:7:text:0" in cbs and "smm:svp:7:0" in cbs and "smm:svcview:7:0" in cbs
    assert all(len(c.encode()) <= 64 for c in cbs)

    class CB:
        data = "smm:svt:7:post:0"
        message = NS(edit_text=None, answer=None)

        async def answer(self, *a, **k):
            pass
    async def _o(cb, bot):
        return NS(id=1)
    monkeypatch.setattr(smm, "_owner", _o)

    async def noop(*a, **k):
        return None
    cb = CB()
    cb.message = NS(edit_text=noop, answer=noop)
    await smm.smm_svt(cb, NS())
    assert st["smm_svc_links:7"] == "post"
