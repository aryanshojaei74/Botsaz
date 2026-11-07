"""tenant/bots/funstat/ — نوعِ ربات: «🔎 Fun Stat» (جست‌وجویِ آیدی ↔ یوزرنیم).

=== PATCH_FUNSTAT_BOT_V1 ===
ربات مستقلِ هر مشتری؛ با توکنِ خودش ساخته می‌شود و کاربرانش از طریقِ سرویسِ
Fun Stat (توکنِ پلتفرم) جست‌وجو می‌کنند. در مرحلهٔ تست فقط مالکانِ تأییدشده می‌توانند بسازند."""
from tenant.bots.funstat.handlers import funstat_bot_router, show_search  # noqa: F401
