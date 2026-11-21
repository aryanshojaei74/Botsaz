import platform_ai_kb as K
import platform_group_guard as G
import platform_bot as PB


def test_policy_topic_is_registered_and_selected_for_refund_questions():
    assert any("پول برگشت" in kws for kws, _t in G._GG_AI_TOPICS)
    for q in ("پول پلنم برگشت نمیخوره؟", "میخوام برگشت پول پلن بگیرم", "پشیمونم از خرید پلن"):
        picked = G._gg_select_relevant_topics(q)
        assert "وجهِ پلن برگشت یا عودت داده نمی" in picked or "وجه پلن برگشت یا عودت داده نمی" in picked, q


def test_policy_matches_the_actual_purchase_terms():
    assert "برگشت یا عودت" in PB.PURCHASE_TERMS and "تست" in PB.PURCHASE_TERMS
    assert "برگشت یا عودت" in K.TOPICS_2026_11C[0][1] or "برگشت یا عودت" in K.TOPICS_2026_11C[0][1].replace("‌", "")
    assert "قوانین بات ساز" in K.TOPICS_2026_11C[0][1]
