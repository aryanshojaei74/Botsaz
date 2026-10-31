"""
tenant/shared/glass_generic.py — «ویرایشِ درجا» برایِ *همهٔ* دکمه‌هایِ کاربر در منویِ شیشه‌ای.

=== PATCH_GLASS_EDIT_GENERIC_V1 ===
گزارشِ ادمین (فدایی): «پنل شیشه‌ای همان پیام را ویرایش نمی‌کند و هی یک پیامِ جدید می‌فرستد؛ پیامِ قبلی هم پاک
نمی‌شود و پیوی شلوغ می‌شود. رویِ هر دکمه‌ای که می‌زنند همان پیام ویرایش و تبدیل شود؛ رویِ بازگشت هم همین‌طور.»

جلسهٔ قبلی (PATCH_GLASS_EDIT_IN_PLACE_V1) این را برایِ پنلِ ادمین و دکمه‌هایِ *منویِ اصلی* (mbtn:) حل کرد. ولی
صفحه‌هایی که *داخلِ* آن‌ها هستی (کیف پول و درگاه‌ها، مشخصات، سفارش‌ها، پرداخت، ...) هنوز با
`cb.message.answer(...)` پیامِ تازه می‌فرستادند — ۴۵۰ هندلرِ callback در پروژه این الگو را دارند و بازنویسیِ
تک‌تکشان ممکن و ایمن نیست. پس یک میان‌افزارِ مرکزی: برایِ هر callbackِ کاربر در منویِ شیشه‌ای، اولین
*صفحه* (پیامِ متنیِ دارایِ دکمهٔ اینلاین) که هندلر می‌فرستد روی همان پیامی که دکمه رویش زده شد ویرایش می‌شود
(قاعده‌ها: tenant/__init__.py ← _glass_generic_step).

ایمنی:
  • فقط چتِ خصوصی، فقط پیامِ متنی (مدیا/کپشن دست‌نخورده)، فقط ربات‌هایی که menu_mode=inline دارند.
  • هندلرهایی که *بعد از* ارسال پیامِ قبلی را دست می‌زنند (SKIP_HANDLERS، با تست پاس می‌شود) اصلاً دخالتی ندارند.
  • کلیدِ خاموش: متغیرِ محیطیِ GLASS_EDIT_GENERIC=0 (کلِ سرویس) یا Settingِ glass_edit_generic=0 (یک ربات).
  • هر خطایی در این لایه فقط به رفتارِ قبلی برمی‌گردد؛ هرگز callback را نمی‌اندازد.
"""
from __future__ import annotations

import os

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# هندلرهایی که پس از ارسالِ صفحهٔ تازه پیامِ قبلی را ویرایش/حذف می‌کنند (با AST استخراج شده؛
# tests/test_glass_generic.py تازه‌بودنِ این فهرست را بررسی می‌کند و هندلرِ جدیدِ پرریسک را می‌گیرد).
SKIP_HANDLERS = frozenset({
    "tenant.bots.booking.handlers.book_back",
    "tenant.bots.digital_shop.handlers.sadm_gifts_list",
    "tenant.bots.digital_shop.handlers.shop_list",
    "tenant.bots.digital_shop.handlers.shop_my_orders",
    "tenant.bots.digital_shop.handlers.shop_order_detail",
    "tenant.bots.virtual_service.fragment_stars.fragment_create",
    "tenant.bots.virtual_service.fragment_stars.fragment_menu",
    "tenant.bots.virtual_service.fragment_stars.fragment_refresh",
    "tenant.bots.virtual_service.smm.smm_approve",
    "tenant.bots.virtual_service.smm.smm_balance",
    "tenant.bots.virtual_service.smm.smm_panel_set_toggle",
    "tenant.bots.virtual_service.smm.sauto_toggle",
    "tenant.bots.virtual_service.smm.smm_qlc_nav",
    "tenant.bots.virtual_service.smm.smm_svc_setcat",
    "tenant.bots.virtual_service.smm.smm_vs_retry",
    "tenant.bots.vpn.vpn_admin.vpn_followup_toggle",
    "tenant.shared.escrow.esc_accept",
    "tenant.bots.vpn.vpn_admin.vpn_setup_authmode_pick",
    "tenant.bots.vpn.vpn_user.vpn_help_show",
    "tenant.buy_ok",
    "tenant.buy_unlimited_ok",
    "tenant.fsm_cancel_inline",
    "tenant.gateways.card_purchase.manual_cfg_paycard",
    "tenant.gateways.crypto_admin.w_crypto",
    "tenant.gateways.ton_direct.tond_rate",
    "tenant.pay_method",
    "tenant.set_auto_approve_toggle",
    "tenant.set_menu_mode",
    "tenant.shared.ad_broadcast.edit_interval_pick",
    "tenant.shared.extras.review_back",
    "tenant.shared.miniapp_admin.miniapp_panel_entry",
    "tenant.shared.support_ticket.ticket_view",
    "tenant.tadm_order_del",
    "tenant.thb_exdisc",
    "tenant.set_auto_approve_confirm",
    "tenant.bots.virtual_service.smm.smmu_reorder",
})


def handler_key(handler_obj) -> str:
    cb = getattr(handler_obj, "callback", None)
    if cb is None:
        return ""
    return f"{getattr(cb, '__module__', '')}.{getattr(cb, '__qualname__', '')}"


async def _maybe_begin(event, data):
    if os.environ.get("GLASS_EDIT_GENERIC", "1") == "0":
        return None
    if not isinstance(event, CallbackQuery):
        return None
    msg = event.message
    if not isinstance(msg, Message) or getattr(msg.chat, "type", None) != "private" or not msg.text:
        return None
    if (event.data or "").startswith("mbtn:"):
        return None                                      # مسیرِ منویِ اصلی قاعدهٔ خودش را دارد
    if handler_key(data.get("handler")) in SKIP_HANDLERS:
        return None
    bot = data.get("bot")
    if bot is None:
        return None
    from tenant import get_bot_row, get_setting, _glass_edit_begin
    row = await get_bot_row(bot)
    if row is None:
        return None
    if await get_setting(row.id, "menu_mode", "reply") != "inline":
        return None
    if (await get_setting(row.id, "glass_edit_generic", "1")) == "0":
        return None
    return _glass_edit_begin(bot, msg, generic=True)


class GlassEditGenericMiddleware(BaseMiddleware):
    """میان‌افزارِ *داخلی* (بعد از پیدا شدنِ هندلر) رویِ callback_queryِ روتر."""

    async def __call__(self, handler, event, data):
        started = None
        try:
            started = await _maybe_begin(event, data)
        except Exception:  # noqa: BLE001
            logger.exception("[glass] شروعِ ویرایشِ درجا خطا داد؛ رفتارِ قبلی اجرا شد")
        try:
            return await handler(event, data)
        finally:
            if started is not None:
                from tenant import _glass_edit_end
                _glass_edit_end(*started)


def install_glass_generic(router) -> None:
    """یک‌بار رویِ روترِ tenant نصب می‌شود (idempotent)."""
    if getattr(router, "_glass_generic_installed", False):
        return
    router.callback_query.middleware(GlassEditGenericMiddleware())
    router._glass_generic_installed = True
