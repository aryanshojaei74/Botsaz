"""PATCH_GUARD_LOG_EVIDENCE_V1 — لاگِ «⚠️ خطا» دقیقاً می‌گوید چه چیزی باعثش شد (متن، لینک/منشن، فوروارد)."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_group_guard as G


def ent(t, off, ln, **k):
    return NS(type=t, offset=off, length=ln, **k)


def test_mention_and_link_are_quoted_exactly_with_message_text():
    m = NS(text="سلام بیایید کانال @spam_channel و https://evil.example/x", caption=None,
           entities=[ent("mention", 18, 13), ent("url", 34, 22)], caption_entities=None, forward_origin=None,
           sticker=None, animation=None)
    e = G._evidence(m)
    assert "@spam_channel" in e and "https://evil.example/x" in e
    assert "<blockquote>" in e and "سلام بیایید کانال" in e
    assert "🔗 مورد:" in e


def test_text_link_shows_hidden_target_and_html_is_escaped():
    m = NS(text="<b>کلیک</b>", caption=None, entities=[ent("text_link", 3, 4, url="https://x.test/?a=1&b=2")],
           caption_entities=None, forward_origin=None, sticker=None, animation=None)
    e = G._evidence(m)
    assert "https://x.test/?a=1&amp;b=2" in e and "&lt;b&gt;" in e and "<b>" not in e.replace("<blockquote>", "")


def test_forward_origin_channel_user_and_hidden():
    base = dict(text=None, caption="فوروارد", entities=None, caption_entities=None, sticker=None, animation=None)
    ch = G._evidence(NS(**base, forward_origin=NS(chat=NS(title="کانال تبلیغ", username="adch"), sender_user=None)))
    assert "↪️ فوروارد از: کانال تبلیغ (@adch)" in ch
    us = G._evidence(NS(**base, forward_origin=NS(chat=None, sender_chat=None, sender_user=NS(full_name="Ali R", username="ali"))))
    assert "Ali R (@ali)" in us
    hid = G._evidence(NS(**base, forward_origin=NS(chat=None, sender_chat=None, sender_user=None, sender_user_name="مخفی")))
    assert "مخفی" in hid


def test_long_text_is_truncated_and_media_kinds_are_named():
    m = NS(text="ا" * 5000, caption=None, entities=None, caption_entities=None, forward_origin=None,
           sticker=NS(), animation=NS())
    e = G._evidence(m)
    assert "…" in e and len(e) < 900 and "🎭 استیکر" in e and "🎞 گیف" in e


def test_never_raises_on_garbage():
    assert isinstance(G._evidence(object()), str)
    assert G._evidence(NS()) == ""


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(G, "Session", mk)
    logs = []

    async def log(text): logs.append(text)
    monkeypatch.setattr(G, "_log_action", log)

    async def pset(key, default=""): return "3" if key == "group_guard_warn_threshold" else default
    monkeypatch.setattr(G, "get_pset", pset)
    yield logs
    await eng.dispose()


@pytest.mark.asyncio
async def test_warn_log_contains_reason_and_evidence_under_it(env):
    u = NS(id=77, full_name="SINA", username="s")
    ev = G._evidence(NS(text="join @spam_ch", caption=None, entities=[ent("mention", 5, 8)], caption_entities=None,
                        forward_origin=None, sticker=None, animation=None))
    cnt, thr, banned = await G._apply_warn(None, -100, u, "ارسالِ لینک/منشن", 0, evidence=ev)
    assert (cnt, thr, banned) == (1, 3, False)
    t = env[0]
    assert t.index("📝 دلیل: ارسالِ لینک/منشن") < t.index("🔗 مورد: @spam_ch") < t.index("💬 متنِ پیام")
    assert "join @spam_ch" in t and "خودکار (فیلتر)" in t


@pytest.mark.asyncio
async def test_warn_without_evidence_keeps_the_old_log_shape(env):
    await G._apply_warn(None, -100, NS(id=1, full_name="A", username=None), "دلیل", 5)
    assert "💬" not in env[0] and "📝 دلیل: دلیل" in env[0]
