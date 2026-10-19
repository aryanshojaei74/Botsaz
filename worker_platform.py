"""
worker_platform.py — پروسه‌ی مستقلِ حلقه‌های پس‌زمینه‌ی main_platform
(backup, subscription, inactivity, swap poll, ptc snapshot, crypto poll,
group-guard captcha/night, tonpays, blu, partner rules)

=== PATCH_PLATFORM_BG_LOOPS_MOVED_TO_WORKER_V1 ===
دقیقاً همون الگویی که رو main_tenant.py زدیم (PATCH_BG_LOOPS_MOVED_TO_WORKER_V1):
قبلاً این ۸ حلقه تو خودِ main_platform.py، هم‌زمان با پردازشِ webhookِ پلتفرم،
رو یه event loop اجرا می‌شدن. یه‌بار هم مستقیماً با `--workers 2` این حلقه‌ها
دوبار اجرا می‌شدن (که رفعش شد: حذفِ --workers از سرویس). ولی حتی با یه
ورکر، اگه یکی از این حلقه‌ها (مثلاً platform_swap_poll_loop یا
crypto_poll_loop_platform که با سرویس‌های بیرونی/پنل‌ها کار می‌کنن) کند
جواب بگیره، می‌تونه پاسخ‌دهیِ webhookِ platform_bot رو هم عقب بندازه.

این اسکریپت هیچ HTTP سرو نمی‌کنه؛ فقط main_platform رو import می‌کنه (تا
init_db، spawn، و تعریفِ حلقه‌ها در دسترس باشن)، init_db می‌زنه، همه‌ی
حلقه‌ها رو اسپان می‌کنه، و برای همیشه صبر می‌کنه. با سرویسِ systemدِ جدا
(botsaz-platform-worker.service) اجرا می‌شه — کاملاً مستقل از
botsaz-platform.service که فقط webhookِ platform رو سرو می‌کنه.

نکته: main_platform.py دیگه خودش این حلقه‌ها رو تو on_startup اسپان
نمی‌کنه (اون بخش با PATCH_PLATFORM_BG_LOOPS_MOVED_TO_WORKER_V1 حذف شده)؛
فقط کارهای غیرِحلقه‌ای (init_db، تنظیمِ webhook، get_me، ...) اونجا می‌مونه.

اجرا:
    python3 worker_platform.py
یا از طریقِ systemd (توصیه‌شده).
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("worker_platform")

# main_platform رو import می‌کنیم تا spawn، حلقه‌های محلی (backup_loop,
# inactivity_loop, ...) و platform_bot در دسترس باشن. این import خودِ
# FastAPI appِ main_platform رو هم می‌سازه، ولی چون uvicorn روی این
# پروسه اجرا نمی‌شه، اون app هیچ‌وقت چیزی سرو نمی‌کنه (بی‌ضرر).
import main_platform as mp

# حلقه‌هایی که تو main_platform فقط محلی (داخلِ خودِ on_startup) ایمپورت
# می‌شدن، این‌جا مستقیم از ماژول‌هاشون می‌گیریم.
from platform_group_guard import (
    group_guard_captcha_sweep_loop,
    group_guard_night_mode_loop,
)
from partner_loops import partner_rules_loop
# === PATCH_BOT_SELFHEAL_V1 ===
from bot_selfheal import bot_selfheal_loop
# === PATCH_TIMED_DISCOUNT_NOTIFY_V1 ===
from platform_bot import sched_discount_notify_loop
# === PATCH_TON_DIRECT_PLATFORM_V1 ===
from platform_ton_direct import pton_direct_loop as pton_direct_platform_loop
# === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===
from platform_bot import sms_expiry_notify_loop
# === MARKER_ZARINPAL_PLATFORM_RECONCILE_V1 ===
from main_platform import zarinpal_platform_reconcile_loop
# === PATCH_MARKET_TRIAL_EXPIRY_V1 ===
from platform_market import market_trial_expiry_loop
# === PATCH_PLATFORM_WAGER_V1 ===
from platform_wager import wager_sweep_loop


async def _worker_startup():
    await mp.init_db()

    for _coro in (
        mp.backup_loop(),
        mp.subscription_loop(),
        mp.inactivity_loop(),
        mp.brand_bio_loop(),
        # === PATCH_DUP_ARCHIVED_TWIN_V1 ===
        mp.dup_bot_watch_loop(),
        mp.platform_swap_poll_loop(),
        mp.ptc_daily_snapshot_loop(),
        mp.crypto_poll_loop_platform(),
        group_guard_captcha_sweep_loop(),
        group_guard_night_mode_loop(),
        mp.tonpays_platform_poll_loop(),
        pton_direct_platform_loop(),  # === PATCH_TON_DIRECT_PLATFORM_V1 ===
        mp.blu_poll_loop(),
        mp.apay_platform_poll_loop(),   # === PATCH_PLATFORM_APAY_V1 ===
        partner_rules_loop(platform_bot=mp.platform_bot),
        # === PATCH_BOT_SELFHEAL_V1 ===
        bot_selfheal_loop(platform_bot=mp.platform_bot),
        sched_discount_notify_loop(platform_bot=mp.platform_bot),
        # === PATCH_PLATFORM_SMS_EXPIRY_NOTIFY_V1 ===
        sms_expiry_notify_loop(),
        # === MARKER_ZARINPAL_PLATFORM_RECONCILE_V1 ===
        zarinpal_platform_reconcile_loop(),
        # === PATCH_MARKET_TRIAL_EXPIRY_V1 ===
        market_trial_expiry_loop(platform_bot=mp.platform_bot),
        # === PATCH_PLATFORM_WAGER_V1 === برگشت/تسویهٔ شرط‌های باز که مهلتشان گذشته
        wager_sweep_loop(platform_bot=mp.platform_bot),
    ):
        mp.spawn(_coro)

    logger.info("worker_platform: همه‌ی حلقه‌های پس‌زمینه‌ی platform اسپان شدن. در حالِ اجرا...")
    # برای همیشه صبر می‌کنیم؛ خودِ حلقه‌ها تسکِ پس‌زمینه‌ن و هیچ‌وقت خودشون
    # برنمی‌گردن. اگه یکیشون کرش کنه، خطا لاگ می‌شه ولی پروسه زنده می‌مونه.
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_worker_startup())
