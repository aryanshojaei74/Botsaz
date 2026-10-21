"""PATCH_SMM_DESC_TRANSLATE_V1 / PATCH_VS_CUSTOM_TEXTS_HOLD_V1 / PATCH_PAUSED_NOTICE_THROTTLE_V1."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.shared import fa_translate as FT
from tenant.shared import vs_msg_override as VO


def test_needs_translation():
    assert FT.needs_translation("High quality, 30 days refill guarantee, instant start")
    assert not FT.needs_translation("کیفیت بالا و ریفیل ۳۰ روزه")
    assert not FT.needs_translation("🔥🔥 ok")                      # لاتین خیلی کم
    assert not FT.needs_translation("کیفیت HQ بسیار بالا و سرعت عالی Fast")


def test_split_chunks_respects_size_and_lines():
    t = "a" * 10 + "\n" + "b" * 10 + "\n" + "c" * 25
    ch = FT.split_chunks(t, 12)
    assert all(len(c) <= 12 for c in ch) and "".join(ch).replace("\n", "") == "a" * 10 + "b" * 10 + "c" * 25


@pytest.mark.asyncio
async def test_translate_fa_uses_fetch_and_negative_cache(monkeypatch):
    calls = []

    async def ok(chunk):
        calls.append(chunk)
        return "ترجمه:" + chunk[:3]
    FT._NEG.clear()
    out = await FT.translate_fa("Instant start, high quality account, refill 30 days", fetch=ok)
    assert out and out.startswith("ترجمه:") and calls

    async def bad(chunk):
        return None
    FT._NEG.clear()
    assert await FT.translate_fa("Another english description text here", fetch=bad) is None
    assert await FT.translate_fa("Another english description text here", fetch=ok) is None      # منفی کش شد
    assert await FT.translate_fa("فارسی است و لازم نیست", fetch=ok) is None


@pytest.mark.asyncio
async def test_provider_text_fa_caches_on_object(monkeypatch):
    from tenant.bots.virtual_service import smm

    async def fake(text, fetch=None):
        return "فارسی " + text[:5]
    monkeypatch.setattr(FT, "translate_fa", fake)
    svc = SimpleNamespace(id=None, provider_desc_fa=None, provider_desc_fa_hash=None)
    t = "Fast delivery with lifetime refill guarantee"
    assert (await smm._provider_text_fa(svc, t)).startswith("فارسی")
    assert svc.provider_desc_fa and svc.provider_desc_fa_hash == FT.text_hash(t)
    monkeypatch.setattr(FT, "translate_fa", None)           # دوباره صدا نمی‌زند: از کش می‌خواند
    assert (await smm._provider_text_fa(svc, t)).startswith("فارسی")
    assert await smm._provider_text_fa(SimpleNamespace(id=None), "متن فارسی") == "متن فارسی"


def test_hold_messages_are_overridable():
    a = "⏳ سفارشت ثبت شد؛ به‌محضِ آماده‌شدنِ موجودیِ ربات، گیفت ارسال می‌شود.\n🔖 کد پیگیری: <code>AB12</code>"
    b = "⏳ پرداختت تأیید شد؛ گیفت به‌محضِ آماده‌شدنِ موجودیِ ربات ارسال می‌شود.\n🔖 کد پیگیری: <code>AB12</code>"
    assert VO.rule_keys(a) == VO._PLACED and VO.rule_keys(b) == VO._PLACED


def test_paused_notice_throttle():
    tenant._PAUSED_NOTICE.clear()
    assert tenant._paused_notice_due(1, 10) is True
    assert tenant._paused_notice_due(1, 10) is False
    assert tenant._paused_notice_due(1, 11) is True and tenant._paused_notice_due(2, 10) is True
