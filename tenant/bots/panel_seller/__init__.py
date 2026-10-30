"""tenant/bots/panel_seller/ — نوعِ ربات «🧩 پنل‌فروشی» (PATCH_PANEL_SELLER_V1).

مالک پنلِ خودش (خانوادهٔ مرزبان) را وصل می‌کند و به مشتری‌ها «ادمینِ پنل» با سقفِ حجم/کاربر/مدت می‌فروشد.
فقط پلنِ حرفه‌ای + مرحلهٔ تست (دسترسیِ دستیِ مدیرِ اصلی). نگاه کن به gate.py / service.py.
"""
from aiogram import Router

from tenant.bots.panel_seller.admin import psel_admin_router
from tenant.bots.panel_seller.user import psel_user_router

panel_seller_router = Router(name="panel_seller")
panel_seller_router.include_router(psel_admin_router)
panel_seller_router.include_router(psel_user_router)

from tenant.bots.panel_seller import products_menu as _products_menu  # noqa: E402,F401
