"""PATCH_ARVAN_TIMEOUT_RETRY_V1 — تایم‌اوتِ آروان دیگر شکستِ فوری نیست؛ تلاشِ بعدی با کلیدِ دیگر و عکسِ کوچک‌تر."""
import asyncio
import io

import pytest

from tenant.gateways import auto_receipt as AR


class _Resp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return {"choices": [{"message": {"content": "تصمیم: تایید"}}]}


def _session_factory(calls, fail_first):
    class S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            import copy
            calls.append((copy.deepcopy(json), headers))
            if len(calls) <= fail_first:
                class _T:
                    async def __aenter__(self_):
                        raise asyncio.TimeoutError()

                    async def __aexit__(self_, *a):
                        return False
                return _T()
            return _Resp()
    return S


def _big_jpeg():
    from PIL import Image
    import random
    im = Image.frombytes("RGB", (2400, 1800), bytes(random.getrandbits(8) for _ in range(2400 * 1800 * 3)))
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=95)
    return b.getvalue()


@pytest.mark.asyncio
async def test_timeout_is_retried_with_other_key_and_smaller_image(monkeypatch):
    calls = []
    monkeypatch.setattr(AR.aiohttp, "ClientSession", _session_factory(calls, fail_first=1))
    monkeypatch.setattr(AR, "ARVAN_AI_BASE_URL", "https://x.test/v1")
    monkeypatch.setattr(AR, "ARVAN_AI_API_KEYS", ["k1", "k2"])
    text, err = await AR._arvan_autonomous_call("p", _big_jpeg())
    assert err is None and text == "تصمیم: تایید"
    assert len(calls) == 2
    img1 = calls[0][0]["messages"][0]["content"][1]["image_url"]["url"]
    img2 = calls[1][0]["messages"][0]["content"][1]["image_url"]["url"]
    assert len(img2) < len(img1)                                   # تلاشِ دوم عکسِ کوچک‌تر


@pytest.mark.asyncio
async def test_all_attempts_timing_out_gives_clear_message(monkeypatch):
    calls = []
    monkeypatch.setattr(AR.aiohttp, "ClientSession", _session_factory(calls, fail_first=99))
    monkeypatch.setattr(AR, "ARVAN_AI_BASE_URL", "https://x.test/v1")
    monkeypatch.setattr(AR, "ARVAN_AI_API_KEYS", ["k1"])
    text, err = await AR._arvan_autonomous_call("p", None)
    assert text is None and "جواب نداد" in err and len(calls) >= 3
