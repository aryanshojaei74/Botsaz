"""tenant/bots/escrow_bot/ — نوعِ ربات: «🤝 ربات واسطه‌گری» (معامله با واسطه، مستقل).

=== PATCH_ESCROW_BOT_KIND_V1 ===
واسطه‌گری (tenant/shared/escrow.py) پیش‌تر فقط داخلِ رباتِ «خدماتِ مجازی» بود. طبقِ خواستِ مشتری حالا یک
«نوعِ ربات» مستقل هم دارد؛ منطقِ معامله همان کدِ مشترک است (بدونِ کپی)، و رباتِ خدماتِ مجازی دست‌نخورده
واسطه‌گریِ خودش را نگه می‌دارد."""
from tenant.bots.escrow_bot import settings as _settings  # noqa: F401
from tenant.bots.escrow_bot import products_menu as _products_menu  # noqa: F401
