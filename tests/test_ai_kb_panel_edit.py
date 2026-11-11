"""سوالِ «آدرس/رمزِ پنل رو چطور عوض کنم» باید مسیرِ واقعی را بیاورد، و مسیرهای ساختگی در پرامپت ممنوع باشند."""
import pytest

import platform_ai_kb as KB
import platform_group_guard as G

QUESTIONS = [
    "ادرس پنل رو چطوری تغییر بدم ربات ساز",
    "آدرس پنل رو عوض کنم",
    "دامنه پنل عوض شده چیکار کنم",
    "رمز پنلم رو عوض کردم ربات وصل نمیشه",
    "پورت پنل عوض شد",
]


@pytest.mark.parametrize("q", QUESTIONS)
def test_panel_edit_question_gets_the_real_path(q):
    text = KB.strip_marks(G._gg_select_relevant_topics(q))
    assert "ویرایش تنظیمات" in text and "پنل VPN خودکار" in text, q


def test_prompt_forbids_made_up_paths_and_names_the_fake_one():
    a = KB.ALWAYS
    assert "جلوگیری از جواب ساختگی" in a
    assert "تنظیمات ربات ← آدرس پنل" in a                      # به‌عنوانِ مسیرِ ممنوع
    assert "فقط وقتی بگو که تو دانش اومده" in a


def test_topic_says_the_fake_path_does_not_exist():
    blob = " ".join(t for _k, t in KB.TOPICS_2026_11B)
    assert "وجود نداره" in blob and "✏️ ویرایش تنظیمات" in blob
