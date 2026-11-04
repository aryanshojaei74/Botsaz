"""SINA («اکسا»): لیستِ سفارش‌ها نباید در کوتاه‌کردنِ context جا بماند؛ و بوست نباید به دکمهٔ استارز ارجاع شود."""
from tenant.bots.virtual_service import smm_ai as A


def test_order_lines_survive_tight_budget():
    ctx = "\n".join(["اطلاعات:"] + [f"- مسیر «دکمه {i}»: " + "x" * 120 for i in range(200)]
                    + ["آخرین سفارش‌هایِ همین مشتری:", "▪ #AB12: بوست × 10, وضعیت: done, مبلغ: 17,520 تومان"])
    out = A.fit_context(ctx, "لیست سفارشاتمو بفرست", 3000)
    assert "▪ #AB12" in out and "آخرین سفارش‌هایِ همین مشتری" in out


def test_system_prompt_forbids_stars_for_boost_and_requires_order_list():
    class R:
        field = "virtual_service"
    p = A._bot_system_prompt(R())
    assert "نگو اطلاعات در دسترس نیست" in p and "استارز/تون فقط" in p
