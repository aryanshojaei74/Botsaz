"""بررسی لاگ ۲۹ آبان: ارسال عکس با کپشن بلند، هشدار تکراری مدل groq، traceback منبع نرخ."""
import pytest

from tenant.shared import price_lookup as pl


class _M:
    def __init__(self, fail_photo=False):
        self.text, self.calls, self.fail = "تون", [], fail_photo

    async def reply_photo(self, photo, caption=None, **kw):
        if self.fail:
            raise RuntimeError("Bad Request: message caption is too long")
        self.calls.append(("photo", caption))

    async def reply(self, text, **kw):
        self.calls.append(("text", text))


async def _run(monkeypatch, text, fail=False):
    async def af(bid, q, uname=""):
        return text, b"\xff\xd8x"
    monkeypatch.setattr(pl, "answer_for", af)
    m = _M(fail)
    assert await pl.answer_core(m, None, 1, ("asset", 1.0, "ton"), False, None)
    return m.calls


@pytest.mark.asyncio
async def test_short_caption_with_photo(monkeypatch):
    calls = await _run(monkeypatch, "<b>قیمت</b> کوتاه")
    assert calls == [("photo", "<b>قیمت</b> کوتاه")]


@pytest.mark.asyncio
async def test_long_text_photo_then_text(monkeypatch):
    long = "<b>x</b>" + "📈" * 600                          # ۱۲۰۰ واحد UTF-16
    calls = await _run(monkeypatch, long)
    assert calls[0] == ("photo", None) and calls[1] == ("text", long)


@pytest.mark.asyncio
async def test_photo_failure_falls_back_to_text(monkeypatch):
    calls = await _run(monkeypatch, "کوتاه", fail=True)
    assert calls == [("text", "کوتاه")]


def test_groq_model_memo_and_quiet_rate_logs():
    import inspect
    import ai_router
    from tenant.gateways import crypto_pay
    assert "_DISCOVERED[(r.name, r.model)] = new_model" in inspect.getsource(ai_router)
    assert 'logger.exception("wallex rate failed")' not in inspect.getsource(crypto_pay)
