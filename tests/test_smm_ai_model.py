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


def test_groq_retries_without_reasoning_effort_on_400_and_records_errors(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status, data):
            self.status, self._d = status, data

        async def json(self, content_type=None):
            return self._d

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            calls.append(dict(json))
            if "reasoning_effort" in json:
                return Resp(400, {"error": {"message": "reasoning_effort not supported"}})
            return Resp(200, {"choices": [{"message": {"content": "سلام"}}]})

    monkeypatch.setattr(ai.aiohttp, "ClientSession", Sess)
    assert asyncio.run(ai._ask_groq("k", "hi", "sys", "openai/gpt-oss-120b")) == "سلام"
    assert len(calls) == 2 and "reasoning_effort" not in calls[1]


def test_failure_reason_is_exposed(monkeypatch):
    class Resp:
        status = 401

        async def json(self, content_type=None):
            return {"error": {"message": "Invalid API Key"}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            return Resp()

    monkeypatch.setattr(ai.aiohttp, "ClientSession", Sess)
    assert asyncio.run(ai.ask_ai("groq", "gsk_x", "hi", model="m")) is None
    assert "401" in ai.last_error["msg"] and "Invalid API Key" in ai.last_error["msg"]
