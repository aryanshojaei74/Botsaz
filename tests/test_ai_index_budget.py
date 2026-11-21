"""PATCH_AI_INDEX_BUDGET_V1: فهرستِ مرحلهٔ تحلیلِ هوش مصنوعیِ گروه از سقفِ درخواستِ Groq رد نشود (HTTP 413)."""
import platform_ai_kb as K


def _topics(n=300):
    return [((f"کلید{i}", f"واژه{i}", "پیام سفارش" if i == 250 else f"x{i}"), f"عنوانِ بخشِ شماره {i}: توضیح") for i in range(n)]


def test_index_respects_budget_and_keeps_original_numbers():
    t = _topics()
    idx = K.topic_index(t)
    assert len(idx) <= K.INDEX_BUDGET
    nums = [int(l.split(":", 1)[0]) for l in idx.splitlines()]
    assert nums == sorted(nums) and nums[0] == 0                 # شماره‌ها همان اندیسِ اصلی، مرتب


def test_matching_topics_are_included_even_if_far_down_the_list():
    t = _topics()
    assert "250:" not in "\n" + K.topic_index(t)                 # بدونِ سوال، بخشِ ۲۵۰ از بودجه بیرون می‌ماند
    idx = K.topic_index(t, "پیام سفارش رو چطور عوض کنم")
    assert any(l.startswith("250:") for l in idx.splitlines()) and len(idx) <= K.INDEX_BUDGET


def test_real_knowledge_index_fits():
    import platform_group_guard as G
    assert len(K.topic_index(G._GG_AI_TOPICS, "پیام سفارش شما انجام شد")) <= K.INDEX_BUDGET
