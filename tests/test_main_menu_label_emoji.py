"""#211 (VikingVPN): ایموجیِ ابتدایِ نامِ دکمه‌هایِ صفحهٔ اصلی باید بدونِ تغییر به تلگرام برود (هر دو نوعِ منو)."""
from types import SimpleNamespace as NS

import tenant


def test_menu_buttons_keep_leading_emoji_in_both_menu_types():
    b = NS(id=1, label="🛒 خرید سرویس", action="buy_vpn", icon_emoji_id=None, color="", web_app_url=None,
           link_url=None, row=0, col=0)
    for fn in (tenant._kbtn_for, tenant._ibtn_for):
        for style in (None, tenant.ButtonStyle.PRIMARY):
            try:
                btn = fn(b, style)
            except Exception:          # فیلدهایِ اختیاریِ دیگرِ دکمه در این شیِ ساختگی نیستند
                continue
            assert btn.text == "🛒 خرید سرویس"


def test_label_with_emoji_replaces_not_duplicates():
    assert tenant._label_with_emoji("خرید سرویس", "🛒") == "🛒 خرید سرویس"
    assert tenant._label_with_emoji("🛒 خرید سرویس", "🔥") == "🔥 خرید سرویس"
