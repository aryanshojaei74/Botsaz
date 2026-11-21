"""PATCH_AI_TOKEN_BUDGET_V1 — ورودیِ هوشِ مصنوعی (Groq ≈ ۸۰۰۰ توکن) نباید برایِ «سلام» هم ۱۰ هزار توکن شود."""
import pytest

import tenant.bots.virtual_service.smm_ai as AI


def _ctx(n=80):
    head = ["اطلاعات اختصاصی این ربات — منبع حقیقت فقط همین بخش است:", "", "فروش استارز تلگرام: هر ۱ استارز ۲٬۰۰۰ تومان", "", "سرویس‌های فعال:"]
    svc = [f"- سرویس شماره {i}؛ دسته عمومی؛ قیمت هر ۱۰۰۰ {i * 100} تومان؛ توضیح: " + ("متن توضیح طولانی " * 12) for i in range(n)]
    svc[37] = "- ممبر کانال تلگرام ارزان؛ حداقل ۱۰۰؛ قیمت هر ۱۰۰۰ ۵۰ تومان"
    return "\n".join(head + svc)


def test_char_budget_respects_provider_and_output_share():
    assert AI._char_budget("groq", "llama-3.3-70b") == int((8000 - 400 - 400) * 1.25)          # max_tokens=400
    assert AI._char_budget("groq", "openai/gpt-oss-20b") < AI._char_budget("groq", "llama-3.3-70b")   # خروجیِ بزرگ‌تر، ورودیِ کمتر
    assert AI._char_budget("openai", None) == AI._NO_LIMIT_CHARS
    assert AI._char_budget("groq", None, token_limit=6000) < AI._char_budget("groq", None)      # سقفِ دستیِ مالک


def test_fit_context_noop_when_it_fits_and_trims_when_not():
    small = "- الف\n- ب"
    assert AI.fit_context(small, "سلام", 1000) == small
    ctx = _ctx()
    out = AI.fit_context(ctx, "سلام", 3000)
    assert len(out) <= 3000 and len(out) < len(ctx)
    assert "فروش استارز تلگرام" in out and "سرویس‌های فعال:" in out              # تیتر/قیمتِ استارز همیشه می‌ماند


def test_relevant_lines_are_preferred():
    out = AI.fit_context(_ctx(), "قیمت ممبر کانال تلگرام چنده؟", 2500)
    assert "ممبر کانال تلگرام ارزان" in out                                       # خطِ مرتبط (سرویس ۳۷) هرچند وسطِ فهرست بود
    assert len(out) <= 2500


def test_header_only_overflow_hard_cuts():
    ctx = "\n".join(["تیتر " + "ا" * 200 for _ in range(30)])
    assert len(AI.fit_context(ctx, "x", 1000)) <= 1000


@pytest.mark.asyncio
async def test_ask_ai_sends_bounded_prompt_for_a_greeting_and_retries_when_too_large(monkeypatch):
    sent = []

    async def fake_groq(key, user_content, system_prompt, model):
        sent.append(len(system_prompt) + len(user_content))
        if len(sent) == 1:
            AI.last_error["msg"] = "HTTP 413: Request too large for model ... tokens per minute (TPM): Limit 8000"
            return None
        return "سلام! چطور کمک کنم؟"

    monkeypatch.setitem(AI._PROVIDER_FUNCS, "groq", fake_groq)

    async def _gs(bid, k, d=""): return d
    monkeypatch.setattr(AI, "_get_setting", _gs)
    monkeypatch.setattr(AI, "aiohttp", object())
    res = await AI.ask_ai("groq", "key", "سلام", _ctx(), "پرامپتِ سیستم " * 20, bot_id=1)
    assert res == "سلام! چطور کمک کنم؟"
    assert len(sent) == 2 and sent[0] <= AI._SHORT_Q_CHARS + 200 and sent[1] < sent[0]       # سلام ⇒ ورودیِ کوچک؛ تلاشِ دوم کوچک‌تر
    assert sent[0] < 8000 * 1.25                                                              # زیرِ سقفِ ۸۰۰۰ توکن
