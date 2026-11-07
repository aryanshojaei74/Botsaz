"""دانشِ هوشِ مصنوعیِ گروهِ بات‌ساز — آپدیتِ ۴۴۰ تا ۴۷۶ (آبان ۱۴۰۵): سؤالِ هر قابلیت بخشِ درستِ دانش را بالا می‌آورد."""
import pytest

import platform_ai_kb as KB
import platform_group_guard as G

CASES = [
    ("چطور قرعه کشی حرفه ای بسازم و تو کانال بذارم؟", "قرعه‌کشیِ حرفه‌ای در ربات اصلی"),
    ("مگه هر کی میتونه قرعه کشی بسازه؟ ساخت قرعه کشی برای همه", "ساختِ قرعه‌کشی برایِ همهٔ کاربران"),
    ("تست رایگان رو یکبار گرفتم میتونم برای ربات دیگه هم تست بگیرم", "برایِ هر نوعِ ربات یک"),
    ("پنل وایرگارد نماینده رو چطور وصل کنم؟ کلید api", "پنلِ وایرگاردِ نماینده"),
    ("ربات پنل فروشی بدون اتصال به پنل هم میشه فروخت؟ فروش دستی", "فروشِ دستی (بدونِ اتصال به پنل)"),
    ("نقش ادمین در این پنل مشخص نیست پاسارگارد", "Operator"),
    ("تو ربات همکار کد تخفیف کار نمیکنه", "کد تخفیف دارم"),
    ("سفارش انبوه رو چطور خاموش کنم multi_", "سفارشِ انبوه (چندلینکی)"),
    ("دستیار هوشمند رو چطور به دکمه وصل کنم", "عملکردِ دکمه"),
    ("ارسال خودکار تون چطور فعال میشه؟", "TON Center API v2"),
    ("لینک مینی اپ رباتم رو کجا ثبت کنم؟", "Menu Button"),
    ("دکمه شارژ بلوپال کیف پول جواب نمیده", "آپدیتِ ۴۶۸"),
    ("تو کانال اعتماد اسم پنل دوبار میاد {panel} {product}", "پیشوندِ تکراریِ نامِ پنل"),
]


@pytest.mark.parametrize("question,needle", CASES)
def test_question_finds_new_topic(question, needle):
    text = G._gg_select_relevant_topics(question)
    assert KB.strip_marks(needle) in KB.strip_marks(text), (question, needle)


def test_new_topics_are_registered_and_plain_text():
    assert all(t in G._GG_AI_TOPICS for t in KB.TOPICS_2026_11A)
    for kws, text in KB.TOPICS_2026_11A:
        assert kws and text and "**" not in text
        assert KB.strip_marks(text) == text                       # اعراب برداشته شده


def test_no_secrets_or_internal_names_leak_in_new_topics():
    blob = "\n".join(t for _k, t in KB.TOPICS_2026_11A)
    assert KB.redact_secrets(blob) == blob                        # توکن/کلید/آی‌پی ندارد
