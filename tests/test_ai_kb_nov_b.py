import pytest

import platform_ai_kb as KB
import platform_group_guard as G

CASES = [
    ("چطور محصول دستی خودم رو اضافه کنم و توضیحات بذارم؟", "ساخت سرویس دستی"),
    ("میخوام همه محصولات یه دسته رو به دکمه وصل کنم", "اتصالِ کلِ این دسته به دکمه"),
    ("مسیر خرید دسته نوع سرویس رو چطور فقط سرویس کنم", "فقط سرویس"),
    ("چطور محصولات رو تیک بزنم و قیمت گروهی تغییر بدم؟", "همهٔ این صفحه"),
    ("متن کیف پول که قابل برداشت نیست رو حذف کنم", "حذفِ متنِ هشدار"),
    ("میخوام کاربرا برداشت وجه بزنن", "حداقل برداشت"),
    ("شماره موبایل کاربران قدیمی رو چطور بگیرم", "درخواستِ شماره از کاربرانِ قبلی"),
    ("بعد از حذف پنل پلن هاش مونده چطور پاک کنم", "پلن‌هایِ بدونِ پنل"),
    ("قیمت افزایش حجم سرویس های من رو چطور عوض کنم", "تنظیمات تمدید / افزایش حجم"),
    ("اسم پنل رو عوض کردم ولی تو خرید سرویس عوض نشد دسته بندی", "تغییرِ نامِ دسته"),
    ("مینی اپ میگه پلنی در این دسته نیست", "کانفیگ‌هایِ VPN آماده (انبار)"),
    ("فوروارد همگانی چطوره", "فوروارد"),
    ("قرعه کشی حرفه ای رو تو ربات همکار چطور فعال کنم", "قرعه‌کشیِ حرفه‌ای بات‌ساز"),
    ("رنگ دکمه های خرید تون رو چطور عوض کنم", "خریدِ تون / استارز"),
    ("دکمه پرداخت فاکتور آپی رو بردارم", "نمایش/مخفی"),
    ("پنل hm رو چطور وصل کنم", "هنوز به عنوانِ نوعِ پنل"),
]


@pytest.mark.parametrize("question,needle", CASES)
def test_question_finds_exact_path_topic(question, needle):
    text = G._gg_select_relevant_topics(question)
    assert KB.strip_marks(needle) in KB.strip_marks(text), (question, needle)


def test_new_topics_registered_plain_and_safe():
    assert all(t in G._GG_AI_TOPICS for t in KB.TOPICS_2026_11B)
    for kws, text in KB.TOPICS_2026_11B:
        assert kws and text and "**" not in text and "`" not in text
        assert KB.strip_marks(text) == text
    blob = "\n".join(t for _k, t in KB.TOPICS_2026_11B)
    assert KB.redact_secrets(blob) == blob


def test_prompt_has_exact_path_rules():
    assert "مسیر و آموزشِ دقیق" in KB.strip_marks(KB.ALWAYS) or "مسیر و آموزش" in KB.ALWAYS
