"""PATCH_AI_ROUTER_V1 — روترِ چندکلیدیِ چندارائه‌دهنده با فیل‌اوورِ فوری."""
import httpx
import pytest

import ai_router as R


def test_parse_routes_formats():
    rs = R.parse_routes("groq=gsk_a, groq=gsk_b\nopenrouter:meta/x:free=sk-or-c;https://h.ir/v1|my-model=KEY=1;bogus=zzz;gemini=")
    assert [(r.name, r.model, r.key) for r in rs] == [
        ("groq", "llama-3.3-70b-versatile", "gsk_a"), ("groq", "llama-3.3-70b-versatile", "gsk_b"),
        ("openrouter", "meta/x:free", "sk-or-c"), ("custom", "my-model", "KEY=1")]
    assert rs[3].base_url == "https://h.ir/v1" and rs[0].base_url.endswith("/openai/v1")


def _router(handler, spec):
    r = R.Router(R.parse_routes(spec))
    r.transport = httpx.MockTransport(handler)
    return r


def _ok(text, route_hint=""):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})


@pytest.mark.asyncio
async def test_failover_to_next_route_immediately_on_429_and_5xx():
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        key = req.headers["authorization"][7:]
        if key == "k1":
            return httpx.Response(429, headers={"retry-after": "30"})
        if key == "k2":
            return httpx.Response(503)
        return _ok("جواب از k3")
    r = _router(handler, "groq=k1,cerebras=k2,mistral=k3")
    text, usage, route = await r.chat("sys", "q")
    assert text == "جواب از k3" and route.startswith("mistral") and usage["total_tokens"] == 13
    assert calls == ["Bearer k1", "Bearer k2", "Bearer k3"]
    # مسیرهایِ خراب خنک شدند؛ درخواستِ بعدی مستقیم سراغِ مسیرِ سالم می‌رود
    calls.clear()
    await r.chat("sys", "q2")
    assert calls == ["Bearer k3"]


@pytest.mark.asyncio
async def test_round_robin_spreads_load_over_healthy_routes():
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"][7:])
        return _ok("ok")
    r = _router(handler, "groq=a,groq=b,groq=c")
    for _ in range(6):
        await r.chat("s", "q")
    assert sorted(set(seen)) == ["a", "b", "c"] and seen.count("a") == 2


@pytest.mark.asyncio
async def test_invalid_key_is_cooled_long_and_all_fail_raises():
    def handler(req):
        return httpx.Response(401)
    r = _router(handler, "groq=bad1,mistral=bad2")
    with pytest.raises(RuntimeError) as e:
        await r.chat("s", "q")
    assert "HTTP 401" in str(e.value)
    assert all(rt.cool_until - __import__("time").monotonic() > 3000 for rt in r.routes)


@pytest.mark.asyncio
async def test_timeout_and_empty_answer_fall_through():
    def handler(req):
        k = req.headers["authorization"][7:]
        if k == "t":
            raise httpx.ReadTimeout("slow")
        if k == "e":
            return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
        return _ok("fine")
    r = _router(handler, "groq=t,mistral=e,gemini=g")
    text, _u, route = await r.chat("s", "q")
    assert text == "fine" and route.startswith("gemini")


@pytest.mark.asyncio
async def test_group_guard_ask_ai_uses_router_when_configured(monkeypatch):
    import platform_group_guard as GG

    def handler(req):
        body = req.read().decode()
        assert "system" in body
        k = req.headers["authorization"][7:]
        return httpx.Response(429) if k == "x" else _ok("<think>فکر</think>جوابِ نهایی")
    r = _router(handler, "groq=x,gemini=y")
    monkeypatch.setattr(R, "_ROUTER", r)

    async def pset(key, default=""):
        return default
    monkeypatch.setattr(GG, "get_pset", pset)
    assert await GG._ask_ai("درگاه آپی چطور فعال میشه؟") == "جوابِ نهایی"


def test_key_prefix_autocorrects_wrong_provider_name():
    rs = R.parse_routes("groq=xai-AAA, groq=gsk_BBB, gemini=sk-or-CCC, groq='AIzaDDD'")
    assert [(r.name, r.key) for r in rs] == [("xai", "xai-AAA"), ("groq", "gsk_BBB"),
                                               ("openrouter", "sk-or-CCC"), ("gemini", "AIzaDDD")]
    assert rs[0].base_url == "https://api.x.ai/v1" and rs[0].model == "grok-3-mini"


@pytest.mark.asyncio
async def test_paid_provider_is_used_only_after_free_ones():
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"][7:])
        k = seen[-1]
        return httpx.Response(429) if k == "gsk_free" else _ok("ok")
    r = _router(handler, "xai=xai-paid,groq=gsk_free")
    await r.chat("s", "q")
    assert seen == ["gsk_free", "xai-paid"]                # اول رایگان، وقتی ۴۲۹ داد پولی
    seen.clear()
    await r.chat("s", "q2")
    assert seen == ["xai-paid"]                             # رایگان خنک است
