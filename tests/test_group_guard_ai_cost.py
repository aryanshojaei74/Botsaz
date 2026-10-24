"""PATCH_GROUP_GUARD_AI_COST_V1 — کم‌کردنِ هزینهٔ هوش مصنوعیِ گروه."""
import pytest

import platform_group_guard as GG


def test_canned_only_for_pure_greetings_and_thanks():
    assert GG._canned_reply("سلام") and GG._canned_reply("ممنون!") and GG._canned_reply("مرسی از شما")
    assert GG._canned_reply("سلام چطور پلن بخرم؟") is None            # سوالِ واقعی
    assert GG._canned_reply("ممنون ولی کارت‌به‌کارت کار نمی‌کنه") is None
    assert GG._canned_reply("") is None


def test_cache_roundtrip_and_normalization():
    GG._AI_CACHE.clear()
    assert GG._ai_cache_get("چطور درگاه آپی را فعال کنم؟") is None
    GG._ai_cache_put("چطور درگاه آپی را فعال کنم؟", "جواب")
    assert GG._ai_cache_get("چطور درگاه آپی را فعال کنم؟") == "جواب"
    import time
    GG._AI_CACHE[GG._ai_cache_key("x")] = (time.monotonic() - GG._AI_CACHE_TTL - 1, "old")   # منقضی
    assert GG._ai_cache_get("x") is None


@pytest.mark.asyncio
async def test_smart_prompt_is_much_smaller_than_full(monkeypatch):
    async def pset(key, default=""):
        return {"group_guard_ai_prompt_mode": "smart"}.get(key, default)
    monkeypatch.setattr(GG, "get_pset", pset)
    smart = await GG._build_ai_system_prompt("درگاه آپی را چطور فعال کنم؟")

    async def pset_full(key, default=""):
        return {"group_guard_ai_prompt_mode": "full"}.get(key, default)
    monkeypatch.setattr(GG, "get_pset", pset_full)
    full = await GG._build_ai_system_prompt("درگاه آپی را چطور فعال کنم؟")
    assert len(smart) < len(full) * 0.6, (len(smart), len(full))
