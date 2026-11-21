"""tenant/bots/digital_shop/ — نوعِ ربات: فروشگاهِ دیجیتال.

=== PATCH_TENANT_SPLIT_PHASE3_DIGITAL_SHOP_V1 ===
کدِ اصلی در handlers.py است (قبلاً tenant/shop.py).
برایِ سازگاری با importهایِ احتمالیِ دیگر، همان نام‌ها را دوباره صادر می‌کنیم."""
from tenant.bots.digital_shop.handlers import *  # noqa: F401,F403
from tenant.bots.digital_shop.handlers import (shop_router, show_shop, send_product_page, shop_admin_kb, Product, ProductOrder, _gen_tracking, _notify_admin_gift_manual, _check_low_stock)  # noqa: F401
