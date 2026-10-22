"""گزینهٔ «تغییرِ مدل» برایِ دستیارِ هوشِ مصنوعی + تنظیمِ مدل‌هایِ reasoning (Groq gpt-oss)."""
import asyncio

import tenant.bots.virtual_service.smm_ai as ai


def test_groq_default_is_gpt_oss_120b():
    assert ai._PROVIDERS["groq"][1] == "openai/gpt-oss-120b"


def test_reasoning_models_get_low_effort_and_room():
    t = ai._openai_compat_tuning("openai/gpt-oss-120b")
    assert t["reasoning_effort"] == "low" and t["max_tokens"] >= 1000
    assert ai._openai_compat_tuning("qwen/qwen3-32b")["reasoning_effort"] == "none"
    assert ai._openai_compat_tuning("llama-3.3-70b-versatile") == {"max_tokens": 400}


def test_strip_think():
    assert ai._strip_think("<think>hmm\nhmm</think>سلام") == "سلام"


def _patch(monkeypatch, setting):
    seen = {}

    async def fake(api_key, content, system, model=None):
        seen["model"] = model
        return "ok"

    async def gs(bot_id, key, default=""):
        return setting if key == "smm_ai_model" else default

    monkeypatch.setitem(ai._PROVIDER_FUNCS, "groq", fake)
    monkeypatch.setattr(ai, "_get_setting", gs)
    return seen


def test_admin_model_setting_is_used(monkeypatch):
    seen = _patch(monkeypatch, "llama-3.1-8b-instant")
    assert asyncio.run(ai.ask_ai("groq", "gsk_x", "hi", bot_id=5)) == "ok"
    assert seen["model"] == "llama-3.1-8b-instant"


def test_empty_setting_falls_back_to_provider_default(monkeypatch):
    seen = _patch(monkeypatch, "")
    asyncio.run(ai.ask_ai("groq", "gsk_x", "hi", bot_id=5))
    assert seen["model"] is None          # _ask_groq خودش پیش‌فرض را می‌گذارد


def test_explicit_model_beats_setting(monkeypatch):
    seen = _patch(monkeypatch, "from-setting")
    asyncio.run(ai.ask_ai("groq", "gsk_x", "hi", bot_id=5, model="explicit"))
    assert seen["model"] == "explicit"


def test_settings_page_shows_model_and_button(monkeypatch):
    async def gs(bot_id, key, default=""):
        return {"smm_ai_model": "my/model", "smm_ai_provider": "groq"}.get(key, default)

    monkeypatch.setattr(ai, "_get_setting", gs)
    text, kb = asyncio.run(ai.smm_ai_settings_content(1, "smm"))
    assert "my/model" in text
    assert any(b.callback_data == "smmai:setmodel:smm" for r in kb.inline_keyboard for b in r)
