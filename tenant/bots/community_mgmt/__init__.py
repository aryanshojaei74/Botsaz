"""tenant/bots/community_mgmt/ — نوعِ ربات: مدیریتِ گروه/کانال.

=== PATCH_TENANT_SPLIT_PHASE1_COMMUNITY_V1 ===
کدِ اصلی در handlers.py است (قبلاً tenant/community.py).
برایِ سازگاری با importهایِ احتمالیِ دیگر، همان نام‌ها را دوباره صادر می‌کنیم."""
from tenant.bots.community_mgmt.handlers import *  # noqa: F401,F403
from tenant.bots.community_mgmt.handlers import community_router, cmty_menu  # noqa: F401
