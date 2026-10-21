"""PATCH_ARVAN_AI_AUTOSETUP_V1 — اتصالِ خودکارِ هوش مصنوعیِ رسید با کلیدِ حسابِ آروان (منطقِ تجزیه و ساخت)."""
import pytest

from tenant.gateways import arvan_ai_setup as AS


def test_auth_header_normalises_prefix():
    assert AS.auth_header("abc-123")["Authorization"] == "Apikey abc-123"
    assert AS.auth_header("Bearer abc")["Authorization"] == "Apikey abc"
    assert AS.auth_header("apikey  abc")["Authorization"] == "Apikey abc"


def test_parse_endpoints_keeps_only_https_urls():
    resp = {"data": [
        {"id": "1", "name": "e1", "endpoint": "https://arvancloudai.ir/gateway/models/GPT-4.1/AAA/v1/", "model": {"name": "GPT-4.1"}},
        {"id": "2", "name": "e2", "endpoint": "", "model": {"name": "x"}},
        {"id": "3", "name": "e3", "endpoint": "http://insecure", "model": {}},
        "junk",
    ], "error": None}
    got = AS.parse_endpoints(resp)
    assert len(got) == 1 and got[0]["url"].endswith("/AAA/v1") and got[0]["model"] == "GPT-4.1"
    assert AS.parse_endpoints({"data": None}) == [] and AS.parse_endpoints("x") == []


def test_parse_vision_models_prefers_gpt41_and_skips_disabled():
    resp = {"data": [
        {"id": "a", "name": "Llama-3-text", "tags": ["chat"]},
        {"id": "b", "name": "Gemini-2.5", "tags": []},
        {"id": "c", "name": "GPT-4.1", "tags": ["vision"], "provider": "openai"},
        {"id": "d", "name": "GPT-4o", "disabled": True},
        {"id": "e", "name": "SomeModel", "tags": ["Vision"]},
    ]}
    got = AS.parse_vision_models(resp)
    assert [m["id"] for m in got] == ["c", "b", "e"] and got[0]["provider"] == "openai"


def test_endpoint_from_create_and_slug():
    assert AS.endpoint_from_create({"data": {"endpoint": "https://x.ir/v1/"}}) == "https://x.ir/v1"
    assert AS.endpoint_from_create({"data": {"endpoint": "relative/path"}}) == ""
    assert AS.slug_name(12) == "botsaz-receipt-12" and AS.slug_name(12).isascii()


@pytest.mark.asyncio
async def test_create_endpoint_reuses_existing_by_name(monkeypatch):
    calls = []

    async def fake(method, path, key, json=None, params=None):
        calls.append((method, path))
        if method == "GET":
            return 200, {"data": [{"id": "9", "name": AS.slug_name(5), "endpoint": "https://g.ir/m/OLD/v1"}]}
        return 200, {"data": {"endpoint": "https://g.ir/m/NEW/v1"}}
    monkeypatch.setattr(AS, "_req", fake)
    url, err = await AS.create_endpoint("k", 5, "model-id")
    assert url.endswith("/OLD/v1") and not err and ("POST", "/endpoints") not in calls


@pytest.mark.asyncio
async def test_create_endpoint_posts_when_missing_and_maps_errors(monkeypatch):
    seen = {}

    async def fake(method, path, key, json=None, params=None):
        if method == "GET":
            return 200, {"data": []}
        seen["body"] = json
        return 200, {"data": {"endpoint": "https://g.ir/m/NEW/v1"}}
    monkeypatch.setattr(AS, "_req", fake)
    url, err = await AS.create_endpoint("k", 5, "mid")
    assert url.endswith("/NEW/v1") and seen["body"]["modelID"] == "mid" and seen["body"]["ratelimit"]["unit"] == "minute"
    assert seen["body"]["name"].isascii()

    async def denied(method, path, key, json=None, params=None):
        return 401, {"message": "bad"}
    monkeypatch.setattr(AS, "_req", denied)
    eps, err = await AS.list_endpoints("k")
    assert eps == [] and "کلید" in err
    async def down(method, path, key, json=None, params=None):
        return 0, "ClientConnectorError"
    monkeypatch.setattr(AS, "_req", down)
    assert "برقرار نشد" in (await AS.list_endpoints("k"))[1]


@pytest.mark.asyncio
async def test_finish_setup_tries_bearer_then_apikey_and_saves_only_on_success(monkeypatch):
    from types import SimpleNamespace
    from tenant.gateways import receipt_ai_key as RK
    from tenant.gateways import auto_receipt as AR
    saved, msgs, tried = {}, [], []

    async def fake_call(prompt, img, creds=None):
        tried.append(creds["key"])
        return ("OK", None) if creds["key"].startswith("Apikey ") else (None, "HTTP 401")

    async def fake_set(bot_id, key, value):
        saved[key] = value

    class M:
        async def answer(self, t, **kw):
            msgs.append(t)
    monkeypatch.setattr(AR, "_arvan_autonomous_call", fake_call)
    monkeypatch.setattr(RK, "_set", fake_set)
    await RK._finish_setup(M(), SimpleNamespace(id=7), "https://g.ir/m/X/v1", "KEY123456789")
    assert tried == ["KEY123456789", "Apikey KEY123456789"]
    assert saved == {"receipt_ai_url": "https://g.ir/m/X/v1", "receipt_ai_key": "Apikey KEY123456789"}
    assert "ذخیره شد" in msgs[-1]

    saved.clear(); msgs.clear()

    async def always_fail(prompt, img, creds=None):
        return None, "HTTP 500"
    monkeypatch.setattr(AR, "_arvan_autonomous_call", always_fail)
    await RK._finish_setup(M(), SimpleNamespace(id=7), "https://g.ir/m/X/v1", "KEY123456789")
    assert saved == {} and "ذخیره نشد" in msgs[-1] and "HTTP 500" in msgs[-1]
