"""
tenant/shared/profile_stats.py — آمارِ صفحهٔ «👤 مشخصاتِ شما» (یک منبعِ واحد).

=== PATCH_PROFILE_TEMPLATE_V1 ===
قبلاً این محاسبه‌ها داخلِ show_profile (tenant/__init__.py) بود. حالا هم متنِ پیش‌فرض و هم
متغیرهایِ متنِ سفارشی ({{ref_count}}، {{ref_spent}}، …) از همین‌جا می‌خوانند تا عددها هرگز با هم
اختلاف پیدا نکنند. منطقِ شمارش بدونِ تغییر از show_profile منتقل شده؛ تنها افزوده:
سفارش‌هایِ SMM برایِ رباتِ خدماتِ مجازی (پیش‌تر «تعداد سفارشات» آن‌جا همیشه صفر بود چون فقط
سفارش‌هایِ فروشگاه/VPN شمرده می‌شد؛ در اسکرین‌شاتِ فدایی ۰ در برابرِ ۱۲ سفارشِ واقعی).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# سفارش‌هایِ SMM که «واقعاً ثبت شده‌اند» (رد/لغو/برگشت‌خورده شمرده نمی‌شوند)
SMM_COUNTED = ("awaiting_admin", "sent", "completed", "partial")
# سفارش‌هایِ SMM که هنوز در جریان‌اند (= «سرویسِ فعال»)
SMM_ACTIVE = ("awaiting_admin", "sent")


@dataclass
class ProfileStats:
    orders_count: int = 0        # سفارش‌هایِ پرداخت‌شدهٔ حجم‌دار (+ SMM برایِ خدماتِ مجازی)
    total_spent: int = 0         # مجموعِ سفارش‌هایِ پرداخت‌شده (Order.amount) — همان «تراکنش کل»
    active_services: int = 0     # اکانتِ فعالِ پنل + کانفیگِ فروخته‌شده (+ سفارشِ SMMِ در جریان)
    ref_count: int = 0           # تعدادِ افرادی که با لینکِ کاربر عضو شده‌اند
    inv_orders: int = 0          # سفارش‌هایِ پرداخت‌شدهٔ زیرمجموعه‌ها
    inv_spent: int = 0           # مجموعِ خریدِ زیرمجموعه‌ها (تومان)
    ref_reward_amount: int = 0
    ref_reward_enabled: bool = True
    ref_total_earned: int = 0    # پاداشِ دریافتیِ تا الان (تومان)
    smm_orders: int = 0          # فقط سفارش‌هایِ SMM (برایِ استفاده در متن‌هایِ سفارشی)
    account_active: bool = False


async def compute(bot_id: int, u, *, is_virtual_service: bool = False) -> ProfileStats:
    """آمارِ کاربر u (ردیفِ BotUser). is_virtual_service: شمردنِ سفارش‌هایِ SMM."""
    from models import (BotUser, Config, Order, PanelAccount, ReferralJoin, ReferralLink, SmmOrder)
    from config import Session
    from tenant import get_setting

    st = ProfileStats()
    tg_id = u.telegram_id
    async with Session() as s:
        st.orders_count = int(await s.scalar(select(func.count(Order.id)).where(
            Order.bot_id == bot_id, Order.user_id == u.id,
            Order.status == "paid", Order.volume_gb > 0)) or 0)
        st.total_spent = int(await s.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(
            Order.bot_id == bot_id, Order.user_id == u.id, Order.status == "paid")) or 0)
        panel_active = int(await s.scalar(select(func.count(PanelAccount.id)).where(
            PanelAccount.bot_id == bot_id, PanelAccount.user_telegram_id == tg_id,
            PanelAccount.status == "active")) or 0)
        cfg_active = int(await s.scalar(select(func.count(Config.id)).where(
            Config.bot_id == bot_id, Config.owner_user_id == u.id,
            Config.status == "sold")) or 0)
        st.active_services = panel_active + cfg_active

        st.smm_orders = int(await s.scalar(select(func.count(SmmOrder.id)).where(
            SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == tg_id,
            SmmOrder.status.in_(SMM_COUNTED))) or 0)
        if is_virtual_service:
            st.orders_count += st.smm_orders
            st.active_services += int(await s.scalar(select(func.count(SmmOrder.id)).where(
                SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == tg_id,
                SmmOrder.status.in_(SMM_ACTIVE))) or 0)

        ref_link = (await s.scalars(select(ReferralLink).where(
            ReferralLink.bot_id == bot_id, ReferralLink.user_id == u.id
        ).order_by(ReferralLink.id.asc()))).first()
        st.ref_count = int(ref_link.join_count) if ref_link else 0
        invited_tg_ids = (await s.scalars(select(ReferralJoin.referred_telegram_id).where(
            ReferralJoin.bot_id == bot_id, ReferralJoin.inviter_user_id == u.id))).all()
        invited_ids = []
        if invited_tg_ids:
            invited_ids = (await s.scalars(select(BotUser.id).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id.in_(list(invited_tg_ids))))).all()
        if invited_ids:
            st.inv_orders = int(await s.scalar(select(func.count(Order.id)).where(
                Order.bot_id == bot_id, Order.user_id.in_(list(invited_ids)),
                Order.status == "paid")) or 0)
            st.inv_spent = int(await s.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(
                Order.bot_id == bot_id, Order.user_id.in_(list(invited_ids)),
                Order.status == "paid")) or 0)

    try:
        st.ref_reward_amount = int(float(await get_setting(bot_id, "referral_reward", "0") or 0))
    except (TypeError, ValueError):
        st.ref_reward_amount = 0
    st.ref_reward_enabled = (await get_setting(bot_id, "referral_reward_enabled", "1")) == "1"
    # === PATCH_MINIAPP_REF_REWARD_V1 === مجموعِ واقعیِ پاداش از تراکنش‌ها (درصدی/بعد از خرید هم درست شود)
    try:
        from models import WalletTxn
        async with Session() as _s3:
            st.ref_total_earned = int(float(await _s3.scalar(
                select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
                    WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id,
                    WalletTxn.kind == "referral")) or 0))
    except Exception:
        st.ref_total_earned = (st.ref_count * st.ref_reward_amount
                               if (st.ref_reward_enabled and st.ref_reward_amount) else 0)
    if is_virtual_service:
        # «وضعیتِ اکانت» برایِ خدماتِ مجازی: سرویسِ اشتراکی نداریم؛ فعال یعنی مسدود/بن نیست
        st.account_active = not (getattr(u, "is_banned", False) or getattr(u, "is_blocked", False))
    else:
        st.account_active = st.active_services > 0
    return st


def ref_summary(st: ProfileStats) -> str:
    """بلوکِ «زیرمجموعه‌گیری» — همان متنِ قبلیِ show_profile، کلمه‌به‌کلمه."""
    lines = (
        f"🔗 زیرمجموعه‌های دعوت‌شده: {st.ref_count} نفر\n"
        f"🛍 خریدهای زیرمجموعه‌ها: {st.inv_orders} سفارش\n"
        f"💵 مجموع خرید زیرمجموعه‌ها: {st.inv_spent:,} تومان\n"
    )
    if st.ref_reward_enabled and (st.ref_reward_amount > 0 or st.ref_total_earned > 0):
        lines += f"🎁 اعتبار پاداش دریافتی تا به الان: {st.ref_total_earned:,} تومان\n"
    lines += ("⚠️ <i>این مبلغ‌ها به‌صورت خودکار به موجودی‌ات اضافه شده و فقط "
              "داخل ربات قابل خرج است (غیرقابل برداشت نقدی).</i>")
    return lines


def account_status_text(active: bool) -> str:
    return "✅ فعال" if active else "❌ غیرفعال"


# متنِ پیش‌فرض به‌شکلِ «قالب با متغیر» — ادمین این را می‌بیند و ویرایش می‌کند. رندرِ آن با
# متنِ سخت‌کدشدهٔ قبلی یکی است (تست دارد).
DEFAULT_TEMPLATE = (
    "👤 <b>مشخصات شما</b>\n\n"
    "🛡 شناسه کاربری : <code>{{user_id}}</code>\n"
    "🏖 نام کاربری : {{username}}\n"
    "🧑‍💼 نام : {{full_name}}\n"
    "☑️ وضعیت اکانت : {{account_status}}\n"
    "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
    "🛍 <b>تراکنش‌ها</b>\n\n"
    "📑 تعداد سفارشات : {{profile_orders}}\n"
    "🟢 سرویس‌های فعال : {{active_services}}\n"
    "💵 تراکنش کل : {{total_spent}} تومان\n"
    "💰 موجودی : {{balance}} تومان\n"
    "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
    "👥 <b>زیرمجموعه‌گیری</b>\n\n"
    "{{ref_summary}}\n"
    "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
    "📅 <b>تاریخ عضویت</b>\n\n"
    "🗓 تاریخ : {{join_date}}\n"
    "🕐 ساعت : {{join_time}}\n"
    "┈┅━━⊷⊰✨⊱⊶━━┅┈"
)
