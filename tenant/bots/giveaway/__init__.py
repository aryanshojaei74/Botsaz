"""tenant/bots/giveaway/ — نوعِ ربات: «🎰 قرعه‌کشی» (PATCH_GIVEAWAY_BOT_V1).

قرعه‌کشیِ حرفه‌ای با: عضویتِ اجباری (سیستمِ موجودِ ربات)، سؤالِ ریاضیِ ضدِ فیک، شمارهٔ ایرانی، شانسِ دعوت، قرعهٔ وزنی با
حذفِ لفت‌دهنده‌ها، جایزهٔ متن/مدیا/شارژِ کیف‌پول، اعلان به گروه/کانال/کاربران، دستورهایِ گروه و پایانِ خودکار.
"""
from aiogram import Router

from tenant.bots.giveaway.admin import giveaway_admin_router
from tenant.bots.giveaway.group import giveaway_group_router
from tenant.bots.giveaway.user import giveaway_user_router, on_start  # noqa: F401

giveaway_router = Router(name="giveaway")
giveaway_router.include_router(giveaway_admin_router)
giveaway_router.include_router(giveaway_user_router)
giveaway_router.include_router(giveaway_group_router)

from tenant.bots.giveaway import products_menu as _products_menu  # noqa: E402,F401
