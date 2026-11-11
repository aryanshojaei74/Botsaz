"""PATCH_AI_NO_NAMES_V1 — هوشِ مصنوعیِ گروه اسمِ کاربران/برندها را به‌جایِ «تیمِ پشتیبانی» معرفی نکند و «فرگمنت» را بشناسد."""
from types import SimpleNamespace as NS

import pytest

import platform_ai_kb as KB
import platform_group_guard as G


def _msg(body, from_user):
    return NS(reply_to_message=NS(text=body, caption=None, photo=None, video=None, document=None, from_user=from_user))


def test_reply_context_never_contains_the_senders_display_name():
    ctx = G._reply_context(_msg("API فرگمنت چطور؟", NS(full_name="ARTAVPN | SUPPORT", is_bot=False)))
    assert "ARTAVPN" not in ctx and "SUPPORT" not in ctx
    assert "API فرگمنت چطور؟" in ctx                      # خودِ متنِ پیام می‌ماند


def test_reply_context_marks_bots_without_naming_them():
    ctx = G._reply_context(_msg("سلام", NS(full_name="SomeBrandBot", is_bot=True)))
    assert "یک ربات" in ctx and "SomeBrandBot" not in ctx


def test_prompt_allows_only_botsaz_support_referral():
    a = KB.ALWAYS
    assert "فقط به «پشتیبانی بات‌ساز» ارجاع بده" in a
    assert "هیچ ربات، تیم، برند" in a
    assert len(a) < 6500


@pytest.mark.parametrize("q", ["API فرگمنت", "api فرگمنت چطور وصل میشه", "فرگمنت", "توکن فرگمنت رو کجا بزنم", "فرگمنت ای پی آی"])
def test_fragment_questions_find_the_fragment_topic(q):
    text = KB.strip_marks(G._gg_select_relevant_topics(q))
    assert "fragment-api.com" in text and "اتصال به Fragment API" in text, q
