"""
worker_tenant.py — پروسه‌ی مستقلِ حلقه‌های پس‌زمینه (VPN expiry, panel polling, ...)

=== PATCH_BG_LOOPS_MOVED_TO_WORKER_V1 ===
قبلاً این ۱۲ حلقه (vpn_expiry_loop و بقیه) تو خودِ main_tenant.py، هم‌زمان با
پردازشِ webhookِ کاربرها، رو یه event loop اجرا می‌شدن. مشکل: بعضی پنل‌های VPN
کند جواب می‌دن یا IPv6ِ غیرقابل‌دسترس دارن (که خودش قبل از fallback به IPv4
چند صد میلی‌ثانیه طول می‌کشه)، و وقتی این تاخیرها جمع می‌شدن، کل event loop
—و در نتیجه پاسخ‌دهی به کاربرهای واقعی— کند می‌شد.

این اسکریپت هیچ HTTP سرو نمی‌کنه؛ فقط main_tenant رو import می‌کنه (تا به
تعریفِ حلقه‌ها و کمک‌تابع‌هاش دسترسی داشته باشه)، init_db می‌زنه، ۱۲ حلقه رو
اسپان می‌کنه، و برای همیشه صبر می‌کنه. با یه سرویسِ systemd جدا
(botsaz-tenant-worker.service) اجرا می‌شه — کاملاً مستقل از پروسه‌ی
botsaz-tenant.service که فقط webhookها رو سرو می‌کنه.

اجرا:
    python3 worker_tenant.py
یا از طریقِ systemd (توصیه‌شده، فایلِ سرویس رو جدا فرستادم).
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("worker_tenant")

# main_tenant رو import می‌کنیم تا حلقه‌ها، spawn، init_db و بقیه‌ی چیزهایی
# که لازم داریم در دسترس باشن. توجه: این import خودِ FastAPI app و
# aiogram Dispatcherِ main_tenant رو هم می‌سازه (چون تو همون فایل تعریف
# شدن) — ولی چون uvicorn روی این پروسه اجرا نمی‌شه، اون app هیچ‌وقت واقعاً
# چیزی سرو نمی‌کنه؛ فقط سربارِ importِ ماژول‌هاست (بی‌ضرر).
import main_tenant as mt

# === PATCH_TRIAL_EXPIRE_CLEANUP_V1 ===
from tenant.shared.trial_cleanup import trial_cleanup_loop as _trial_cleanup_loop
from tenant.shared.broadcast_runner import broadcast_loop as _broadcast_loop  # PATCH_BROADCAST_DURABLE_V1
from tenant.shared.stock_alarm import stock_alarm_loop as _stock_alarm_loop  # PATCH_STOCK_ALARM_V1
from tenant.bots.virtual_service.smm import vnum_recover_loop as _vnum_recover_loop  # PATCH_VNUM_RECOVER_V1
from tenant.bots.giveaway.loop import giveaway_loop as _giveaway_loop  # PATCH_GIVEAWAY_BOT_V1

# === PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
# همان چرخه، این‌بار برایِ سرویس‌هایِ پولی‌ای که حجمشان تمام شده.
from tenant.shared.traffic_cleanup import traffic_cleanup_loop as _traffic_cleanup_loop

# === PATCH_TON_DIRECT_V1 ===
from tenant.gateways.ton_direct import ton_direct_loop as _ton_direct_loop

# === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
from tenant.gateways.tron_direct import tron_direct_loop as _tron_direct_loop

# === PATCH_PANEL_AUTOSYNC_V1 ===
from tenant.shared.panel_clients.panel_autosync import panel_autosync_loop as _panel_autosync_loop
from tenant.shared.panel_clients.panel_tools import vpn_backup_loop as _vpn_backup_loop  # PATCH_PANEL_TOOLS_V1
# === PATCH_PANEL_WATCHDOG_V1 ===
from tenant.shared.panel_clients.panel_watchdog import panel_watchdog_loop as _panel_watchdog_loop
# === PATCH_UNIQUEPAY_AUTO_V1 ===
from tenant.gateways.uniquepay import uniquepay_poll_loop as _uniquepay_loop
# === PATCH_HOOSHPAY_GATEWAY_V1 ===
from tenant.gateways.hooshpay import hooshpay_poll_loop as _hooshpay_loop
# === PATCH_BLUPAL_TENANT_V1 ===
from tenant.gateways.blupal import blupal_poll_loop as _blupal_loop
# === PATCH_APAY_GATEWAY_V1 ===
from tenant.gateways.apay import apay_poll_loop as _apay_loop
# === PATCH_SMM_PRICE_AUTOSYNC_V1 ===
from tenant.bots.virtual_service.smm_price_sync import smm_price_sync_loop as _smm_price_sync_loop
# === PATCH_MANUAL_LINK_LIMITS_V1 === اصلاحِ یک‌بارهٔ سرویس‌هایِ دستیِ ۰/۰
from tenant.shared.panel_clients.panel_tools import repair_manual_accounts_once as _repair_manual_once
# === PATCH_ABANGATEWAY_V1 ===
from tenant.gateways.abangateway import abangateway_poll_loop as _abangateway_loop
# === PATCH_GROUP_BETTING_EXPIRY_LOOP_V1 ===
from tenant.shared.group_betting import betting_expiry_loop as _betting_expiry_loop
from tenant.shared.wager_games import wager_expiry_loop as _wager_expiry_loop  # === PATCH_WAGER_TENANT_V1 ===
from tenant.bots.vpn.vpn_metered import metered_loop as _metered_loop   # PATCH_METERED_PLAN_V1
from tenant.shared.backup_full import full_backup_auto_loop as _full_backup_loop   # PATCH_BACKUP_FULL_V1
from tenant.shared.extras import referral_vs_reconcile_loop as _referral_vs_loop   # PATCH_REF_VS_RECONCILE_V1
from tenant.shared.support_ticket import ticket_autoclose_loop as _ticket_autoclose_loop   # PATCH_TICKET_ORDER_SETTINGS_V1
from tenant.bots.virtual_service.ozvinoo_vnum import ozvinoo_poll_loop as _ozvinoo_poll_loop
# === PATCH_P2P_MARKET_V1 === لغوِ خودکارِ معامله‌هایِ بی‌پاسخ + پایانِ اجاره‌هایِ مارکت
from tenant.bots.virtual_service.nft_p2p import p2p_loop as _p2p_loop
# === PATCH_NFT_TGTOOLS_V1 === پایشِ خریدهایِ TGTools + همگام‌سازیِ دوره‌ایِ منابعِ مارکت
from tenant.bots.virtual_service.nft_tgtools import nft_sources_loop as _nft_sources_loop
# === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
# حلقهٔ بالا فقط وضعیتِ سفارش‌ها را چک می‌کند؛ این یکی قیمت‌ها را تازه نگه
# می‌دارد تا ربات با نرخِ کهنه نفروشد.
from tenant.bots.virtual_service.ozvinoo_vnum import ozvinoo_price_sync_loop as _ozvinoo_price_sync_loop


async def _worker_startup():
    # همون تنظیمِ executorِ main_tenant رو اینجا هم می‌زنیم، چون حلقه‌های
    # VPN از همین پروسه DNS lookup/اتصال به پنل‌ها انجام می‌دن.
    try:
        asyncio.get_running_loop().set_default_executor(
            mt._TPE(max_workers=64, thread_name_prefix="botsaz-worker-io"))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (تنظیمِ executor بزرگ‌تر)")

    await mt.init_db()
    try:
        from tenant.bots.virtual_service.smm import ensure_smm_columns
        await ensure_smm_columns()
    except Exception as e:
        print("ensure_smm_columns error:", e)

    for _coro in (
        mt._warm_tenant_bots(), mt._warm_tenant_bots_loop(), mt.vpn_expiry_loop(),
        mt.vpn_first_connect_loop(), mt.card_order_expiry_loop(), mt.smm_poll_loop(),
        _ozvinoo_poll_loop(),
        # === PATCH_OZVINOO_AUTO_PRICE_SYNC_V1 ===
        _ozvinoo_price_sync_loop(),
        mt.swap_poll_loop(), mt.ad_broadcast_loop(), mt.ad_scheduled_posts_loop(),
        mt.crypto_poll_loop_tenant(),
        mt.product_rating_loop(),
        # === PATCH_TRIAL_EXPIRE_CLEANUP_V1 ===
        _trial_cleanup_loop(),
        _metered_loop(),   # PATCH_METERED_PLAN_V1
        _ticket_autoclose_loop(),   # PATCH_TICKET_ORDER_SETTINGS_V1
        _referral_vs_loop(),   # PATCH_REF_VS_RECONCILE_V1
        _full_backup_loop(),   # PATCH_BACKUP_FULL_V1
        _stock_alarm_loop(),
        _vnum_recover_loop(),
        # === PATCH_TRAFFIC_OUT_CLEANUP_V1 ===
        _traffic_cleanup_loop(),
        # === PATCH_TON_DIRECT_V1 ===
        _ton_direct_loop(),
        # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
        _tron_direct_loop(),
        mt.tonpays_poll_loop(),
        # === PATCH_ZARINPAL_PENDING_POLL_V1 ===
        mt.zarinpal_wallet_poll_loop(),
        # === PATCH_ZIBAL_GATEWAY_V1 ===
        mt.zibal_wallet_poll_loop(),
        # === PATCH_SMM_PROVIDER_DETAIL_V1 ===
        mt.smm_panel_health_loop(),
        # === PATCH_SMM_RATE_PREWARM_V1 ===
        mt.smm_rate_prewarm_loop(),
        # === PATCH_PANEL_AUTOSYNC_V1 ===
        _panel_autosync_loop(),
        _vpn_backup_loop(),
        # === PATCH_PANEL_WATCHDOG_V1 ===
        _panel_watchdog_loop(),
        _uniquepay_loop(),
        _hooshpay_loop(),   # PATCH_HOOSHPAY_GATEWAY_V1
        _blupal_loop(),     # PATCH_BLUPAL_TENANT_V1
        _apay_loop(),
        _smm_price_sync_loop(),
        _repair_manual_once(),
        # === PATCH_ABANGATEWAY_V1 ===
        _abangateway_loop(),
        # === PATCH_GROUP_BETTING_EXPIRY_LOOP_V1 ===
        _betting_expiry_loop(),
        _wager_expiry_loop(),  # === PATCH_WAGER_TENANT_V1 ===
        # === PATCH_P2P_MARKET_V1 ===
        _p2p_loop(),
        _nft_sources_loop(),
        # === PATCH_BROADCAST_DURABLE_V1 === ادامهٔ ارسال‌هایِ همگانیِ قطع‌شده بعد از ری‌استارت
        _broadcast_loop(),
        _giveaway_loop(),   # PATCH_GIVEAWAY_BOT_V1: پایانِ خودکارِ قرعه‌کشی‌ها + شمارندهٔ اعلان‌ها
    ):
        mt.spawn(_coro)

    logger.info("worker_tenant: همه‌ی ۲۰ حلقه‌ی پس‌زمینه اسپان شدن. در حالِ اجرا...")
    # برای همیشه صبر می‌کنیم؛ خودِ حلقه‌ها تسکِ پس‌زمینه‌ن و هیچ‌وقت خودشون
    # برنمی‌گردن. اگه یکیشون کرش کنه، خطا لاگ می‌شه ولی پروسه زنده می‌مونه
    # (رفتارِ همونی که قبلاً هم تو main_tenant.py داشتیم).
    try:
        import loopwatch
        loopwatch.start("worker")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (loopwatch)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_worker_startup())
