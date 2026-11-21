"""موتور تخفیف متمرکز (نسخهٔ کامل).

تمام قوانین کد تخفیف یک‌جا اینجا اعتبارسنجی و محاسبه می‌شوند تا منطق در
چند فایل پخش نشود. این ماژول هم در فروشگاه اصلی و هم در فلو خرید VPN
قابل‌استفاده است.

قابلیت‌ها:
  • تخفیف درصدی یا مبلغ ثابت (تومان)
  • تاریخ انقضا
  • محدودسازی به یک پنل خاص و/یا یک پلن خاص
  • حداقل مبلغ خرید
  • سقف کل استفاده (max_uses) و «هر کاربر فقط یک‌بار» (DiscountUsage)

الگوی استفادهٔ درست (مهم):
  ۱) موقع نمایش خلاصهٔ خرید → `validate_discount(...)` فقط محاسبه می‌کند (بدون ثبت).
  ۲) موقع پرداخت → دوباره `validate_discount(...)` صدا زده می‌شود (چون کدِ توی
     state ممکن است بین این دو لحظه منقضی/پر شده باشد) و در همان تراکنشِ کسرِ
     کیف‌پول، `record_usage(...)` به‌صورت اتمیک ثبت می‌شود.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Session
from models import DiscountCode, DiscountUsage


@dataclass
class DiscountResult:
    """نتیجهٔ اعتبارسنجی/محاسبهٔ یک کد تخفیف."""
    ok: bool
    discount: int = 0            # مبلغ تخفیف برای این خرید (تومان)
    final_price: int = 0         # قیمت نهایی پس از تخفیف (تومان)
    code_id: int | None = None
    code: str | None = None
    disc_type: str | None = None  # "percent" | "amount"
    reason: str = ""             # پیام خطای فارسی وقتی ok=False

    @property
    def label(self) -> str:
        """متن کوتاه برای نمایش کنار خلاصهٔ خرید."""
        if not self.ok:
            return ""
        if self.disc_type == "amount":
            return f"کد {self.code}: −{_rial(self.discount)} تومان"
        return f"کد {self.code}: −{self.discount_percent}٪"

    @property
    def discount_percent(self) -> int:
        if self.final_price + self.discount <= 0:
            return 0
        return round(self.discount * 100 / (self.final_price + self.discount))


def _rial(n) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except Exception:
        return str(n)


def _calc_discount(disc_type: str, percent: int, amount: float, price: int) -> int:
    """مبلغ تخفیف را حساب می‌کند و هیچ‌وقت بیشتر از خود قیمت نمی‌شود."""
    if disc_type == "amount":
        d = int(round(float(amount)))
    else:  # percent
        p = max(0, min(100, int(percent)))
        d = int(round(price * p / 100))
    return max(0, min(d, price))   # نه منفی، نه بیشتر از قیمت


async def validate_discount(
    *,
    bot_id: int,
    code: str,
    telegram_id: int,
    price: int,
    panel_key: str | None = None,
    plan_id: int | None = None,
    button_id: int | None = None,
    product_key: str | None = None,   # === PATCH_VS_DISCOUNT_PRODUCTS_V1 === stars/ton/gift/premium/vnum/smm
    session: AsyncSession | None = None,
) -> DiscountResult:
    """کد تخفیف را اعتبارسنجی و مبلغ تخفیف را محاسبه می‌کند (بدون ثبت مصرف).

    اگر `session` داده شود از همان نشست استفاده می‌کند (برای فراخوانی اتمیک
    داخل تراکنش پرداخت)، در غیر این صورت نشست موقتِ خودش را باز می‌کند.

    `button_id`: اگر خریدِ جاری از یک دکمهٔ مشخص (main_buttons.id) انجام می‌شود
    همان را بده. کدهایی که به یک دکمهٔ خاص محدود شده‌اند (DiscountCode.button_id)
    فقط وقتی همین مقدار برابرش باشد معتبرند — یعنی «کد تخفیف اختصاصیِ همان دکمه».
    """
    price = int(round(float(price)))
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return DiscountResult(ok=False, final_price=price, reason="کد خالی است.")

    own_session = session is None
    s = session or Session()
    try:
        # === PATCH_DISCOUNT_RACE_LOCK_V1 ===
        stmt = select(DiscountCode).where(
            DiscountCode.bot_id == bot_id,
            func.upper(DiscountCode.code) == code_norm,
        )
        if not own_session:
            stmt = stmt.with_for_update()
        dc = await s.scalar(stmt)
        if dc is None:
            return DiscountResult(ok=False, final_price=price, reason="کد تخفیف نامعتبر است.")
        if not dc.is_active:
            return DiscountResult(ok=False, final_price=price, reason="این کد غیرفعال شده است.")
        if dc.button_id is not None and dc.button_id != button_id:
            return DiscountResult(ok=False, final_price=price,
                                  reason="این کد مخصوص یک دکمهٔ دیگر است و اینجا معتبر نیست.")
        if dc.restricted_user_id is not None and dc.restricted_user_id != telegram_id:
            return DiscountResult(ok=False, final_price=price,
                                  reason="این کد مخصوص کاربر دیگری است.")

        # === PATCH_VS_DISCOUNT_PRODUCTS_V1 ===
        # کدِ محدود به دسته‌هایِ محصول: فقط وقتی معتبر است که خریدِ جاری از همان دسته‌ها باشد.
        _pk = {k.strip() for k in (dc.product_keys or "").split(",") if k.strip()}
        if _pk and (product_key or "") not in _pk:
            return DiscountResult(ok=False, final_price=price,
                                  reason="این کد برای این محصول معتبر نیست.")

        # انقضا
        if dc.expires_at is not None and dc.expires_at < datetime.utcnow():
            return DiscountResult(ok=False, final_price=price, reason="این کد منقضی شده است.")

        # سقف کل استفاده
        if dc.max_uses and dc.used_count >= dc.max_uses:
            return DiscountResult(ok=False, final_price=price, reason="ظرفیت استفاده از این کد تمام شده است.")

        # محدود به پنل (اگر کد محدود است، خریدِ بدون/با پنلِ متفاوت رد می‌شود)
        if dc.panel_key and dc.panel_key != panel_key:
            return DiscountResult(ok=False, final_price=price, reason="این کد برای این پنل معتبر نیست.")

        # محدود به پلن (کدِ مخصوصِ یک پلن، روی کانفیگ آماده/پلن دیگر اعمال نمی‌شود)
        if dc.plan_id and dc.plan_id != plan_id:
            return DiscountResult(ok=False, final_price=price, reason="این کد برای این پلن معتبر نیست.")

        # حداقل خرید
        min_amt = int(round(float(dc.min_amount or 0)))
        if min_amt and price < min_amt:
            return DiscountResult(
                ok=False, final_price=price,
                reason=f"حداقل مبلغ خرید برای این کد {_rial(min_amt)} تومان است.")

        # هر کاربر فقط یک‌بار
        used_before = await s.scalar(
            select(DiscountUsage.id).where(
                DiscountUsage.discount_code_id == dc.id,
                DiscountUsage.telegram_id == telegram_id,
            ).limit(1)
        )
        if used_before:
            return DiscountResult(ok=False, final_price=price, reason="شما قبلاً از این کد استفاده کرده‌اید.")

        disc_type = dc.disc_type or "percent"
        discount = _calc_discount(disc_type, dc.percent or 0, dc.amount or 0, price)
        return DiscountResult(
            ok=True, discount=discount, final_price=price - discount,
            code_id=dc.id, code=dc.code, disc_type=disc_type,
        )
    finally:
        if own_session:
            await s.close()


async def record_usage(
    session: AsyncSession, *, code_id: int, bot_id: int, telegram_id: int,
    saved_amount: int = 0,
) -> None:
    """ثبت مصرف کد + افزایش شمارندهٔ used_count.

    saved_amount: مبلغِ صرفه‌جویی‌شده (تومان) در همین خرید — برای گزارشِ «کل
    مبلغِ تخفیفِ استفاده‌شده» در آمارِ ربات. اگر داده نشود، ۰ ثبت می‌شود
    (سازگاری با فراخوانی‌های قدیمی).

    باید داخل همان تراکنشِ پرداخت صدا زده شود (قبل از commit). خودش commit
    نمی‌کند تا اتمیک بودن با کسر کیف‌پول حفظ شود.
    """
    session.add(DiscountUsage(
        discount_code_id=code_id, bot_id=bot_id, telegram_id=telegram_id,
        saved_amount=saved_amount))
    dc = await session.get(DiscountCode, code_id)
    if dc is not None:
        dc.used_count = (dc.used_count or 0) + 1


async def revert_usage(
    session: AsyncSession, *, code_id: int, telegram_id: int
) -> None:
    """برگرداندن مصرف کد (هنگام عودت وجه): حذف ردیف DiscountUsage این کاربر
    برای این کد و کم‌کردن used_count. باید قبل از commit صدا زده شود."""
    usage = await session.scalar(
        select(DiscountUsage).where(
            DiscountUsage.discount_code_id == code_id,
            DiscountUsage.telegram_id == telegram_id,
        ).order_by(DiscountUsage.id.desc()).limit(1)
    )
    if usage is not None:
        await session.delete(usage)
    dc = await session.get(DiscountCode, code_id)
    if dc is not None and (dc.used_count or 0) > 0:
        dc.used_count = dc.used_count - 1


# ═══════════════════════════════════════════
#  تخفیف پلکانیِ زیرمجموعه‌ای (خودکار، بدون کد)
# ═══════════════════════════════════════════
#
# ادمینِ ربات می‌تواند چند پله تعریف کند: «به‌ازای X زیرمجموعه، Y درصد تخفیف».
# این تخفیف به‌صورت خودکار (بدون نیاز به کد) روی خرید VPN اعمال می‌شود و با کد
# تخفیف ترکیب نمی‌شود (وقتی کاربر کد دارد، مسیر کد اولویت دارد).
#
# تنظیمات در Setting با کلید "referral_disc_tiers" به‌صورت رشتهٔ "req:pct,req:pct"
# ذخیره می‌شود، مثلاً: "3:5,10:10,20:20".


def parse_referral_tiers(raw: str | None) -> list[tuple[int, int]]:
    """رشتهٔ تنظیمات را به لیست مرتبِ [(req, percent), ...] تبدیل می‌کند."""
    out: list[tuple[int, int]] = []
    if not raw:
        return out
    for part in str(raw).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, _, b = part.partition(":")
        try:
            req = int(a.strip())
            pct = int(b.strip())
        except ValueError:
            continue
        if req > 0 and 1 <= pct <= 100:
            out.append((req, min(100, pct)))
    # مرتب بر اساس تعداد لازم
    out.sort(key=lambda x: x[0])
    return out


def format_referral_tiers(tiers: list[tuple[int, int]]) -> str:
    return ",".join(f"{req}:{pct}" for req, pct in tiers)


def referral_percent_for(tiers: list[tuple[int, int]], have: int) -> int:
    """بزرگ‌ترین درصدی که کاربر با «have» زیرمجموعه واجد آن است (پلکانی)."""
    best = 0
    for req, pct in tiers:
        if have >= req and pct > best:
            best = pct
    return best


async def get_referral_tiers(bot_id: int) -> list[tuple[int, int]]:
    """پله‌های تخفیف زیرمجموعه‌ای این ربات را از تنظیمات می‌خواند."""
    from models import Setting
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "referral_disc_tiers"))
        en = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "referral_disc_enabled"))
    if en is not None and en.value == "0":
        return []
    return parse_referral_tiers(row.value if row else None)


async def referral_discount(
    *, bot_id: int, telegram_id: int, price: int,
    src: str = "join",
) -> "DiscountResult":
    """تخفیف خودکارِ زیرمجموعه‌ای را برای این کاربر/قیمت محاسبه می‌کند.

    اگر پله‌ای تعریف نشده باشد یا کاربر واجد هیچ پله‌ای نباشد، ok=False با
    discount=0 برمی‌گرداند. این تابع چیزی در دیتابیس ثبت نمی‌کند.
    """
    from models import BotUser, ReferralJoin, ReferralLink
    from sqlalchemy import func as _func

    price = int(round(float(price)))
    tiers = await get_referral_tiers(bot_id)
    if not tiers:
        return DiscountResult(ok=False, final_price=price, reason="بدون پلهٔ تخفیف")

    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        if not u:
            return DiscountResult(ok=False, final_price=price, reason="کاربر یافت نشد")
        if src == "real":
            have = await s.scalar(
                select(_func.count()).select_from(ReferralJoin).where(
                    ReferralJoin.bot_id == bot_id,
                    ReferralJoin.inviter_user_id == u.id,
                )
            )
            have = int(have or 0)
        else:
            jc = await s.scalar(select(ReferralLink.join_count).where(
                ReferralLink.bot_id == bot_id, ReferralLink.user_id == u.id))
            have = int(jc or 0)

    pct = referral_percent_for(tiers, have)
    if pct <= 0:
        return DiscountResult(ok=False, final_price=price,
                              reason="هنوز به اولین پلهٔ تخفیف نرسیده‌ای")
    discount = _calc_discount("percent", pct, 0, price)
    return DiscountResult(
        ok=True, discount=discount, final_price=price - discount,
        code=None, disc_type="percent",
    )


# ═══════════════════════════════════════════════════════════════════════
# === PATCH_FIRST_PURCHASE_DISCOUNT_V1 ===
# «تخفیفِ اولین خریدِ کاربر از ربات» — درخواستِ ادمین‌ها.
#
# با کدِ تخفیف فرق دارد و عمداً جدا پیاده شده:
#   • کاربر چیزی وارد نمی‌کند؛ خودکار در خلاصهٔ خرید می‌نشیند.
#   • فقط یک‌بار، رویِ نخستین خریدِ هر کاربر در آن ربات.
#   • مالک فقط یک درصد یا یک مبلغ تعیین می‌کند (بدونِ ساختنِ کد).
#
# تنظیمات در جدولِ Setting نگه داشته می‌شود (نه DiscountCode)، چون این یک
# «کد» نیست و ساختنِ ردیفِ ساختگی در جدولِ کدها، فهرستِ کدهایِ مالک را
# شلوغ و گیج‌کننده می‌کرد:
#     firstbuy_on     "1" / ""      فعال/غیرفعال
#     firstbuy_type   "percent" | "amount"
#     firstbuy_value  عدد           درصد (۱..۹۰) یا مبلغ به تومان
#     firstbuy_max    عدد           سقفِ تخفیف برایِ حالتِ درصدی (۰ = بی‌سقف)
# ═══════════════════════════════════════════════════════════════════════

FIRST_BUY_KEYS = ("firstbuy_on", "firstbuy_type", "firstbuy_value", "firstbuy_max")


async def _setting(s: AsyncSession, bot_id: int, key: str, default: str = "") -> str:
    from models import Setting
    r = await s.scalar(select(Setting).where(
        Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def has_previous_purchase(bot_id: int, telegram_id: int,
                                session: AsyncSession | None = None) -> bool:
    """آیا این کاربر قبلاً از این ربات خریدی داشته؟

    دو منبع چک می‌شود چون هیچ‌کدام به‌تنهایی کامل نیست:
      • WalletTxn(kind="purchase") — همهٔ خریدهایِ کیف‌پولی
      • PanelAccount — سرویس‌هایی که واقعاً ساخته شده‌اند (پوششِ خریدهایی
        که به هر دلیل تراکنشِ کیف‌پول ثبت نکرده‌اند)
    محافظه‌کارانه است: هر ردّی از خریدِ قبلی یعنی «اولین خرید نیست»، تا
    تخفیف دوباره به کسی داده نشود.
    """
    from models import BotUser, PanelAccount, WalletTxn

    own = session is None
    s = session or Session()
    try:
        uid = await s.scalar(select(BotUser.id).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        if uid is not None:
            # === PATCH_FIRSTBUY_COUNT_SMM_ORDERS_V1 ===
            # سفارش‌هایِ SMM با kind="smm_order" ثبت می‌شوند (نه "purchase")، پس
            # قبلاً خریدارِ ربات‌هایِ خدمات مجازی هیچ‌وقت «دارایِ خریدِ قبلی» حساب
            # نمی‌شد و تخفیفِ «اولین خرید» رویِ *هر* سفارشش می‌نشست.
            n = await s.scalar(select(func.count()).select_from(WalletTxn).where(
                WalletTxn.bot_id == bot_id, WalletTxn.user_id == uid,
                WalletTxn.kind.in_(("purchase", "smm_order"))))
            if n:
                return True
        n2 = await s.scalar(select(func.count()).select_from(PanelAccount).where(
            PanelAccount.bot_id == bot_id,
            PanelAccount.user_telegram_id == telegram_id))
        if n2:
            return True
        try:
            from models import SmmOrder, GiftOrder
            n3 = await s.scalar(select(func.count()).select_from(SmmOrder).where(
                SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == telegram_id,
                SmmOrder.status.notin_(("rejected", "cancelled", "refunded", "awaiting_receipt"))))
            if n3:
                return True
            if uid is not None:
                # سفارشِ رهاشده/ردشده/برگشتی («در انتظارِ رسید»، ناموفق، …) خرید حساب نمی‌شود؛ وگرنه کسی که یک‌بار
                # کارت‌به‌کارت را شروع و رها کرده بود دیگر هیچ‌وقت تخفیفِ اولین خرید را نمی‌دید.
                n4 = await s.scalar(select(func.count()).select_from(GiftOrder).where(
                    GiftOrder.bot_id == bot_id, GiftOrder.buyer_user_id == uid,
                    GiftOrder.status.notin_(("awaiting_receipt", "failed", "refunded", "cancelled",
                                             "canceled", "rejected"))))
                if n4:
                    return True
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).exception("firstbuy: بررسیِ سفارش‌هایِ SMM/گیفت ناموفق بود")
        return False
    finally:
        if own:
            await s.close()


async def first_purchase_discount(
    *, bot_id: int, telegram_id: int, price: int,
    session: AsyncSession | None = None,
) -> DiscountResult:
    """تخفیفِ خودکارِ اولین خرید را حساب می‌کند (بدونِ ثبتِ مصرف).

    اگر فعال نباشد یا کاربر قبلاً خرید کرده باشد، `ok=False` برمی‌گردد و
    فراخوان باید آن را بی‌سروصدا نادیده بگیرد — این خطا نیست.
    """
    price = int(round(float(price)))
    own = session is None
    s = session or Session()
    try:
        if await _setting(s, bot_id, "firstbuy_on", "") != "1":
            return DiscountResult(ok=False, final_price=price, reason="غیرفعال")
        try:
            value = int(float(await _setting(s, bot_id, "firstbuy_value", "0") or 0))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return DiscountResult(ok=False, final_price=price, reason="مقدار تنظیم نشده")
        dtype = await _setting(s, bot_id, "firstbuy_type", "percent") or "percent"

        if await has_previous_purchase(bot_id, telegram_id, session=s):
            return DiscountResult(ok=False, final_price=price, reason="اولین خرید نیست")

        if dtype == "amount":
            discount = min(value, price)
        else:
            discount = int(price * min(value, 90) / 100)
            try:
                cap = int(float(await _setting(s, bot_id, "firstbuy_max", "0") or 0))
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                discount = min(discount, cap)
        discount = max(0, min(discount, price))
        if discount <= 0:
            return DiscountResult(ok=False, final_price=price, reason="تخفیف صفر شد")
        return DiscountResult(
            ok=True, discount=discount, final_price=price - discount,
            code="اولین خرید", disc_type=dtype)
    finally:
        if own:
            await s.close()
