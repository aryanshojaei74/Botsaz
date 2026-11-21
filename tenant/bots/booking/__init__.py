"""tenant/bots/booking/ — نوعِ ربات: رزرو/نوبت‌دهی.

=== PATCH_TENANT_SPLIT_PHASE2_BOOKING_V1 ===
کدِ اصلی در handlers.py است (قبلاً tenant/booking.py).
برایِ سازگاری با importهایِ احتمالیِ دیگر، همان نام‌ها را دوباره صادر می‌کنیم."""
from tenant.bots.booking.handlers import *  # noqa: F401,F403
from tenant.bots.booking.handlers import (booking_router, book_show_slots, book_my_appointments, booking_admin_kb)  # noqa: F401
